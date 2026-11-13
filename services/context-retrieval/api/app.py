"""S18 HTTP 层。

契约只覆盖了一部分，且覆盖的那部分与本服务实际要的 body 不一致：

- `POST /context-packs` **在** `contracts/openapi.v2.yaml:404`，body 是
  `{context: execution-context.v2, task_role, token_budget, command_id}`，
  `additionalProperties: false`。
- `GET /context-packs/{id}` 不在契约里。

`execution-context.v2` 的 18 个字段全部 required 且 `additionalProperties:
false`，装不下方案 §2 的 `episode_order` / `perspective` / `story_clock_id` /
`story_tick` / `focus` / `include_candidates` 六个字段 —— 用哪个形状都会丢信息
或伪造字段。这是 C09-4 同类冲突，不在本服务内选边。实现按方案 §2 落专用 body，
收到契约信封形状时返回 `CONTRACT_BODY_UNSUPPORTED` 而不是猜字段，两条缺口都登记在
`plans/S18-context-retrieval.md` §12，不改 `contracts/`。

租户一律来自 `X-Tenant-Id`（平台签名上下文）。请求体里的 `tenant_id` 只用于比对，
不一致即 403 —— AGENTS.md 禁止把请求体里的 `tenant_id` 当可信输入。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from context_retrieval.deps import build_memory_query
from context_retrieval.schema import CONTRACT_GAPS
from context_retrieval.service import ContextRetrievalService, ServiceError

DB_PATH = os.environ.get("S18_DB", ":memory:")

app = FastAPI(title="S18 context-retrieval", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
service = ContextRetrievalService(
    _conn, memory_query=build_memory_query(os.environ.get("S16_BASE_URL")))
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
    return _err(409, exc.code, str(exc), False)


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(status_code=409, headers={"Retry-After": "1"},
                        content={"code": exc.code, "message": str(exc),
                                 "retryable": True, "correlation_id": "",
                                 "details": {}})


def _body(payload, command_id: str, tenant_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ServiceError(400, "MALFORMED_REQUEST", "body must be a JSON object")
    # openapi 把 body 定义成 {context: execution-context.v2, ...}。那个信封装不下
    # 方案 §2 的六个字段（见模块 docstring），所以这里不拆 animation，宁可显式
    # 拒绝也不猜 —— 猜出来的 episode_order/perspective 会静默改变可见性。
    if isinstance(payload.get("context"), dict):
        raise ServiceError(
            400, "CONTRACT_BODY_UNSUPPORTED",
            "openapi.v2's `context: execution-context.v2` envelope cannot carry "
            "episode_order/perspective/story_clock_id/story_tick/focus/"
            "include_candidates; send plan §2's flat body instead (S18 C18-1)")
    if not command_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "Idempotency-Key is required")
    if not tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    inside = payload.get("command_id")
    if inside is not None and inside != command_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key does not match body command_id")
    if payload.get("tenant_id") is not None and payload["tenant_id"] != tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return {**payload, "command_id": command_id, "tenant_id": tenant_id}


def command(tenant_id: str, body: dict, execute):
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。"""
    request_hash = canonical_request_hash(body)
    record = _idem.begin(tenant_id, body["command_id"], request_hash)
    if record.result is not None:
        replay = json.loads(record.result)
        replay["replayed"] = True
        return replay
    try:
        result = execute()
    except Exception:
        _idem.abort(tenant_id, body["command_id"])
        raise
    _idem.finish(tenant_id, body["command_id"],
                 json.dumps(result, ensure_ascii=False))
    return {**result, "replayed": False}


@app.post("/context-packs", status_code=201)
def create_context_pack(payload: dict,
                        command_id: str = Header(alias="Idempotency-Key"),
                        x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    result = command(x_tenant_id, body,
                     lambda: service.create_pack(x_tenant_id, body))
    return {**result, "x-contract-gaps": CONTRACT_GAPS}


@app.get("/context-packs/{pack_id}")
def get_context_pack(pack_id: str, revision: int | None = None,
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§1.5。契约对象 + 结构化扩展视图。跨租户 404。"""
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.get_pack(x_tenant_id, pack_id, revision)


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}
