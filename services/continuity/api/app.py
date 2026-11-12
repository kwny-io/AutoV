"""S17 HTTP 层。

`contracts/openapi.v2.yaml` 里**没有任何 S17 路径**（21 条 path 无一是 continuity），
而 `services/continuity/service.md` 与方案 §4.1 / §5.1 承诺了
`POST /continuity-checks` 与 `GET /continuity-reports/{id}`。这是契约缺口，不是
实现自由度 —— 所以本文件按方案落两条路径，并在 `plans/S17-continuity.md` 的
契约缺口一节登记，不自行改 `contracts/`。

租户一律来自 `X-Tenant-Id`（平台签名上下文）。请求体里的 `tenant_id` 只用于比对，
不一致即 403 —— AGENTS.md 明确禁止把请求体里的 `tenant_id` 当可信输入。
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

from continuity.service import ContinuityService, ServiceError
from continuity.deps import build_memory_query, build_shot_source

DB_PATH = os.environ.get("S17_DB", ":memory:")

app = FastAPI(title="S17 continuity", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
service = ContinuityService(
    _conn,
    memory_query=build_memory_query(os.environ.get("S16_BASE_URL")),
    shot_source=build_shot_source(os.environ.get("S06_BASE_URL")),
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


@app.post("/continuity-checks", status_code=202)
def create_continuity_check(payload: dict,
                            command_id: str = Header(alias="Idempotency-Key"),
                            x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.run_check(x_tenant_id, body))


@app.get("/continuity-reports/{report_id}")
def get_continuity_report(report_id: str,
                          x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§5.1。跨租户 404 —— 不泄漏报告是否存在。"""
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.get_report(x_tenant_id, report_id)


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}
