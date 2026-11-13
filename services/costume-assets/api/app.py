"""S12 HTTP 层。

契约面（`contracts/service-catalog.v2.json` 派生的 `service.yaml`）对本服务
只声明了 `POST /costumes/{id}/revisions` 与
`GET /costumes/{id}/revisions/{revision}` 两条。方案 §5.1 的命令/查询表是另外
九条。**这是契约与方案的冲突，不是我该裁决的事**，已登记为 C12-1；本层按方案
实现，不改契约。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。跨租户的**读**是 404
（存在与无权同形，不泄漏别的租户有什么），跨租户的**请求**是 403。
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

from costume_assets.deps import (
    build_artifact_source,
    build_quality_gate_source,
)
from costume_assets.schema import CONTRACT_GAPS
from costume_assets.service import CostumeAssetsService, ServiceError

DB_PATH = os.environ.get("S12_DB", ":memory:")

app = FastAPI(title="S12 costume-assets", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = CostumeAssetsService(
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
        raise ServiceError(403, "TENANT_SCOPE_DENIED",
                           "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放**不重新校验上游**：S28 的 artifact 可能在两次之间被 tombstone。
    重放等于用新结论回答一个旧问题，而调用方手里拿的还是旧结果。
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

@app.post("/costumes", status_code=201)
def create_costume(payload: dict,
                   command_id: str = Header(default="",
                                            alias="Idempotency-Key"),
                   x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.create_costume(
                       x_tenant_id, command_id, body))


@app.post("/costumes/{costume_id}/candidates")
def draft_candidate(costume_id: str, payload: dict,
                    command_id: str = Header(default="",
                                             alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.draft_candidate(
                       x_tenant_id, command_id, costume_id, body))


@app.post("/costumes/{costume_id}/candidates/{candidate_id}:accept")
def accept_candidate(costume_id: str, candidate_id: str, payload: dict,
                     command_id: str = Header(default="",
                                              alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.accept_candidate(
                       x_tenant_id, command_id, costume_id, candidate_id, body))


@app.post("/costumes/{costume_id}/candidates/{candidate_id}:reject")
def reject_candidate(costume_id: str, candidate_id: str, payload: dict,
                     command_id: str = Header(default="",
                                              alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.reject_candidate(
                       x_tenant_id, command_id, costume_id, candidate_id, body))


@app.post("/costumes/{costume_id}:set-canonical")
def set_canonical(costume_id: str, payload: dict,
                  command_id: str = Header(default="",
                                           alias="Idempotency-Key"),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.set_canonical(
                       x_tenant_id, command_id, costume_id, body))


@app.post("/costumes/{costume_id}:deprecate")
def deprecate(costume_id: str, payload: dict,
              command_id: str = Header(default="", alias="Idempotency-Key"),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.change_status(
                       x_tenant_id, command_id, costume_id, "deprecated", body))


@app.post("/costumes/{costume_id}:tombstone")
def tombstone(costume_id: str, payload: dict,
              command_id: str = Header(default="", alias="Idempotency-Key"),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.change_status(
                       x_tenant_id, command_id, costume_id, "tombstoned", body))


@app.post("/costumes/{costume_id}/revision-references")
def add_revision_references(costume_id: str, payload: dict,
                            command_id: str = Header(
                                default="", alias="Idempotency-Key"),
                            x_tenant_id: str = Header(
                                default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.add_revision_references(
                       x_tenant_id, command_id, costume_id, body))


# ------------------------------------------------------------------ 查询

@app.get("/costumes:lookup")
def lookup(x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
           series_scope: str = Query(...), alias: str = Query(...)):
    return service.lookup(_tenant(x_tenant_id), series_scope=series_scope,
                          alias=alias)


@app.get("/costumes/{costume_id}")
def get_costume(costume_id: str,
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_costume(_tenant(x_tenant_id), costume_id)


@app.get("/costumes/{costume_id}/canonical")
def get_canonical(costume_id: str,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    return service.get_canonical(_tenant(x_tenant_id), costume_id)


@app.get("/costumes/{costume_id}/revisions/{revision}")
def get_revision(costume_id: str, revision: int,
                 variant: str | None = Query(default=None),
                 x_tenant_id: str = Header(default="",
                                           alias="X-Tenant-Id")):
    # `?variant=damage:torn_shoulder,wetness:damp`。缺档位是 422，不是回退默认。
    return service.get_revision(_tenant(x_tenant_id), costume_id, revision,
                                variant=variant)


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
