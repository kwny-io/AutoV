"""S14 HTTP 层。

契约面（`contracts/service-catalog.v2.json` 派生出的 `service.yaml`）对本服务
只声明了两条路径：`POST /props/{id}/revisions` 与
`GET /props/{id}/revisions/{revision}`。方案 §5.1 的命令/查询表则是另外八条。
**这是契约与方案的冲突，不是我该裁决的事**，已登记为 C14-1；本层按方案实现，
不改契约。`contracts/openapi.v2.yaml` 里根本没有 S14 的路由，这是同一处冲突的
另一面。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

敏感文字的读取作用域只认平台签名注入的 `X-Principal-Scopes`（逗号分隔）。
**请求体里的任何 `is_admin` / `redact=false` 都不是授权依据**——一个调用方能
给自己发权限，脱敏就只是装饰（C14-6）。
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

from prop_assets.deps import (
    build_artifact_source,
    build_authorizer,
    build_quality_gate_source,
)
from prop_assets.schema import CONTRACT_GAPS
from prop_assets.service import PropAssetsService, ServiceError

DB_PATH = os.environ.get("S14_DB", ":memory:")

app = FastAPI(title="S14 prop-assets", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = PropAssetsService(
    _conn,
    artifact_source=build_artifact_source(os.environ.get("S28_BASE_URL")),
    quality_gate_source=build_quality_gate_source(
        os.environ.get("QUALITY_GATE_BASE_URL")),
    authorizer=build_authorizer(os.environ.get("S01_BASE_URL")),
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
        # 读路径上不存在与无权同形 404（F14）。
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


def _scopes(raw: str) -> tuple:
    return tuple(part.strip() for part in (raw or "").split(",") if part.strip())


# ------------------------------------------------------------------ 命令

@app.post("/props", status_code=201)
def create_prop(payload: dict,
                command_id: str = Header(default="", alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.create_prop(
                       x_tenant_id, command_id, body))


@app.post("/props/{prop_id}/candidates")
def draft_candidate(prop_id: str, payload: dict,
                    command_id: str = Header(default="",
                                             alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.draft_candidate(
                       x_tenant_id, command_id, prop_id, body))


@app.post("/props/{prop_id}/candidates/{candidate_id}:accept")
def accept_candidate(prop_id: str, candidate_id: str, payload: dict,
                     command_id: str = Header(default="",
                                              alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.accept_candidate(
                       x_tenant_id, command_id, prop_id, candidate_id, body))


@app.post("/props/{prop_id}/candidates/{candidate_id}:reject")
def reject_candidate(prop_id: str, candidate_id: str, payload: dict,
                     command_id: str = Header(default="",
                                              alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.reject_candidate(
                       x_tenant_id, command_id, prop_id, candidate_id, body))


@app.post("/props/{prop_id}:set-canonical")
def set_canonical(prop_id: str, payload: dict,
                  command_id: str = Header(default="",
                                           alias="Idempotency-Key"),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.set_canonical(
                       x_tenant_id, command_id, prop_id, body))


@app.post("/props/{prop_id}:deprecate")
def deprecate(prop_id: str, payload: dict,
              command_id: str = Header(default="", alias="Idempotency-Key"),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.change_status(
                       x_tenant_id, command_id, prop_id, "deprecated", body))


@app.post("/props/{prop_id}:tombstone")
def tombstone(prop_id: str, payload: dict,
              command_id: str = Header(default="", alias="Idempotency-Key"),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.change_status(
                       x_tenant_id, command_id, prop_id, "tombstoned", body))


@app.post("/props/{prop_id}/required-variants")
def register_required_variant(prop_id: str, payload: dict,
                              command_id: str = Header(
                                  default="", alias="Idempotency-Key"),
                              x_tenant_id: str = Header(
                                  default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.register_required_variant(
                       x_tenant_id, command_id, prop_id, body))


# ------------------------------------------------------------------ 查询

@app.get("/props")
def list_props(x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
               series_scope: str = Query(...),
               status: str | None = Query(default=None),
               limit: int = Query(default=50)):
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.list_props(x_tenant_id, series_scope=series_scope,
                              status=status, limit=limit)


@app.get("/props:lookup")
def lookup(x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
           series_scope: str = Query(...), alias: str = Query(...)):
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.lookup(x_tenant_id, series_scope=series_scope, alias=alias)


@app.get("/props/{prop_id}")
def get_prop(prop_id: str,
             x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.get_prop(x_tenant_id, prop_id)


@app.get("/props/{prop_id}/canonical")
def get_canonical(prop_id: str,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.get_canonical(x_tenant_id, prop_id)


@app.get("/props/{prop_id}/revisions/{revision}")
def get_revision(prop_id: str, revision: int,
                 variant: str | None = Query(default=None),
                 x_tenant_id: str = Header(default="",
                                           alias="X-Tenant-Id"),
                 x_principal_id: str = Header(default="",
                                              alias="X-Principal-Id"),
                 x_principal_scopes: str = Header(default="",
                                                  alias="X-Principal-Scopes")):
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.get_revision(
        x_tenant_id, prop_id, revision, variant=variant,
        principal=x_principal_id or None,
        scopes=_scopes(x_principal_scopes))


@app.get("/props/{prop_id}/revisions/{revision}/text")
def read_text(prop_id: str, revision: int,
              surface: str | None = Query(default=None),
              redact: bool = Query(default=True),
              x_tenant_id: str = Header(default="",
                                        alias="X-Tenant-Id"),
              x_principal_id: str = Header(default="",
                                           alias="X-Principal-Id"),
              x_principal_scopes: str = Header(default="",
                                               alias="X-Principal-Scopes")):
    # `redact` 只是个提示，**不构成授权**：`redact=false` 也不会给明文。
    # 参数留着是因为方案 §4.3 的读路径写了它，去掉会破坏已写下的调用方。
    _ = redact
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.read_text(
        x_tenant_id, prop_id, revision, surface=surface,
        principal=x_principal_id or None,
        scopes=_scopes(x_principal_scopes))


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
