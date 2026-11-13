"""S11 HTTP 层。

契约面（`service.yaml`）对本服务只声明了两条路径：
`POST /characters/{id}/revisions` 与 `GET /characters/{id}/revisions/{revision}`。
方案 §5.1/§5.2 的命令/查询表则是另外十一条。**这是契约与方案的冲突，不是
我该裁决的事**，已登记为 C11-1；本层按方案实现，不改契约。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

**本层没有 `X-Principal-Scopes`。** S11 不存敏感文字，也没有"脱敏后仍可读"
的中间态——身份特征在这里是全量返回的，因为它就是调用的目的；能不能看
某个角色归 S01/S10 的授权面。`rights_recheck_required` 恒为 true 才是 S11
自己的表达：权利结论每次读都要回 S02 复查。
"""

from __future__ import annotations

import json
import os
import sqlite3

from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from character_assets.deps import (
    build_artifact_source,
    build_quality_gate_source,
)
from character_assets.schema import CONTRACT_GAPS
from character_assets.service import CharacterAssetsService, ServiceError

DB_PATH = os.environ.get("S11_DB", ":memory:")

app = FastAPI(title="S11 character-assets", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = CharacterAssetsService(
    _conn,
    artifact_source=build_artifact_source(os.environ.get("S28_BASE_URL")),
    quality_gate_source=build_quality_gate_source(
        os.environ.get("QUALITY_GATE_BASE_URL")),
)
_idem = SqliteIdempotencyStore(_conn)


def _err(status: int, code: str, message: str, retryable: bool = False,
         details: dict | None = None):
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "retryable": retryable,
                 "correlation_id": "", "details": details or {}})


@app.exception_handler(ServiceError)
async def _service_error(_request, exc: ServiceError):
    return _err(exc.status, exc.code, str(exc), exc.retryable, exc.details)


@app.exception_handler(CommandConflict)
async def _conflict(_request, exc: CommandConflict):
    # 同键不同 body：§6.1 要求 409，不是覆盖。
    return _err(409, exc.code, str(exc), False)


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(
        status_code=409, headers={"Retry-After": "1"},
        content={"code": "COMMAND_IN_FLIGHT", "message": str(exc),
                 "retryable": True, "correlation_id": "", "details": {}})


def _body(payload, command_id: str, tenant_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "body must be a JSON object")
    if not command_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key is required")
    if not tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    inside = payload.get("command_id")
    if inside is not None and inside != command_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key does not match body command_id")
    candidate = payload.get("tenant_id")
    if candidate is not None and candidate != tenant_id:
        # 跨租户**请求**与跨租户**读**分开：这里是 403（凭据与请求体不一致），
        # 读路径上不存在与无权同形 404（F11）。
        raise ServiceError(403, "TENANT_SCOPE_DENIED",
                           "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放**不重新校验上游**：S28 的 artifact 可能在两次之间被 tombstone，
    重放一次就等于用一个新结论回答一个旧问题；而调用方手里拿的还是旧结果。
    """
    request_hash = canonical_request_hash(body)
    record = _idem.begin(tenant_id, command_id, request_hash)
    if record.result is not None:
        replay = json.loads(record.result)
        replay["replayed"] = True
        return replay
    try:
        result = execute(request_hash)
    except Exception:
        _idem.abort(tenant_id, command_id)
        raise
    _idem.finish(tenant_id, command_id,
                 json.dumps(result, ensure_ascii=False))
    return {**result, "replayed": False}


def _tenant(x_tenant_id: str) -> str:
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return x_tenant_id


# ------------------------------------------------------------------ 命令

@app.post("/characters", status_code=201)
def create_character(payload: dict,
                     command_id: str = Header(default="",
                                              alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.create_character(
                       x_tenant_id, command_id, body))


@app.post("/characters/{character_id}/candidates")
def draft_candidate(character_id: str, payload: dict,
                    command_id: str = Header(default="",
                                             alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.draft_candidate(
                       x_tenant_id, command_id, character_id, body))


@app.post("/characters/{character_id}/candidates/{candidate_id}:accept")
def accept_candidate(character_id: str, candidate_id: str, payload: dict,
                     command_id: str = Header(default="",
                                              alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.accept_candidate(
                       x_tenant_id, command_id, character_id, candidate_id,
                       body))


@app.post("/characters/{character_id}/candidates/{candidate_id}:reject")
def reject_candidate(character_id: str, candidate_id: str, payload: dict,
                     command_id: str = Header(default="",
                                              alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.reject_candidate(
                       x_tenant_id, command_id, character_id, candidate_id,
                       body))


@app.post("/characters/{character_id}:set-canonical")
def set_canonical(character_id: str, payload: dict,
                  command_id: str = Header(default="",
                                           alias="Idempotency-Key"),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.set_canonical(
                       x_tenant_id, command_id, character_id, body))


@app.post("/characters/{character_id}:deprecate")
def deprecate(character_id: str, payload: dict,
              command_id: str = Header(default="", alias="Idempotency-Key"),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.change_status(
                       x_tenant_id, command_id, character_id, "deprecated",
                       body))


@app.post("/characters/{character_id}:tombstone")
def tombstone(character_id: str, payload: dict,
              command_id: str = Header(default="", alias="Idempotency-Key"),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.change_status(
                       x_tenant_id, command_id, character_id, "tombstoned",
                       body))


@app.post("/characters/{character_id}/aliases")
def add_alias(character_id: str, payload: dict,
              command_id: str = Header(default="", alias="Idempotency-Key"),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.add_alias(
                       x_tenant_id, command_id, character_id, body))


# ------------------------------------------------------------------ 查询

@app.get("/characters:lookup")
def lookup(x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
           series_scope: str = Query(...), alias: str = Query(...)):
    return service.lookup(_tenant(x_tenant_id), series_scope=series_scope,
                          alias=alias)


@app.get("/characters/{character_id}")
def get_character(character_id: str,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    return service.get_character(_tenant(x_tenant_id), character_id)


@app.get("/characters/{character_id}/canonical")
def get_canonical(character_id: str,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    return service.get_canonical(_tenant(x_tenant_id), character_id)


@app.get("/characters/{character_id}/drift")
def drift(character_id: str,
          from_revision: int = Query(..., alias="from"),
          to_revision: int = Query(..., alias="to"),
          x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    # 查询参数名是 `from` / `to`（§5.2），但 `from` 是 Python 关键字，
    # 所以内部名加后缀、alias 保留契约拼写。
    return service.drift(_tenant(x_tenant_id), character_id,
                         from_revision=from_revision,
                         to_revision=to_revision)


@app.get("/characters/{character_id}/candidates")
def list_candidates(character_id: str,
                    state: str | None = Query(default=None),
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id")):
    return service.list_candidates(_tenant(x_tenant_id), character_id,
                                   state=state)


@app.get("/characters/{character_id}/revisions")
def list_revisions(character_id: str,
                   after: int = Query(default=0),
                   limit: int = Query(default=50),
                   x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id")):
    return service.list_revisions(_tenant(x_tenant_id), character_id,
                                  after=after, limit=limit)


@app.get("/characters/{character_id}/revisions/{revision}")
def get_revision(character_id: str, revision: int,
                 x_tenant_id: str = Header(default="",
                                           alias="X-Tenant-Id")):
    # §6.4：响应带 ETag = content_hash。用响应头回，不用 body 里的字段冒充。
    result = service.get_revision(_tenant(x_tenant_id), character_id, revision)
    return JSONResponse(content=result,
                        headers={"ETag": result["etag"]})


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
