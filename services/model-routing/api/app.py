"""S25 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）对这个服务只有 `POST /route-decisions`，
而它的响应体声明成 `additionalProperties: false` + 三个字段，与方案 §5.1 的
响应不相容。**这是契约与文档的冲突，不是我可以裁决的事**，已登记为 MRT-17；
本层按方案实现，不改契约。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

`GET /route-decisions` 必须带 `attempt_id` 或 `node_id`——无范围的列表在数据
长大后就是事故，而且这个服务的查询几乎都是"这个节点后来怎么了"。
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

from model_routing.deps import (
    build_budget_source,
    build_capability_source,
    build_combination_source,
    build_policy_source,
    build_snapshot_source,
    build_telemetry_source,
    build_validity_source,
)
from model_routing.schema import CONTRACT_GAPS
from model_routing.service import RoutingService, ServiceError

DB_PATH = os.environ.get("S25_DB", ":memory:")

app = FastAPI(title="S25 model-routing", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

_s24 = os.environ.get("S24_BASE_URL")
service = RoutingService(
    _conn,
    capability_source=build_capability_source(_s24),
    snapshot_source=build_snapshot_source(_s24),
    combination_source=build_combination_source(_s24),
    validity_source=build_validity_source(_s24),
    policy_source=build_policy_source(os.environ.get("S02_BASE_URL")),
    budget_source=build_budget_source(os.environ.get("S23_BASE_URL")),
    telemetry_source=build_telemetry_source(os.environ.get("S24_TELEMETRY_URL")),
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
    # 同键不同 body：§8 要求 409，不是覆盖。
    return _err(409, exc.code, str(exc), False)


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(
        status_code=409, headers={"Retry-After": "1"},
        content={"code": "COMMAND_IN_FLIGHT", "message": str(exc),
                 "retryable": True, "correlation_id": "", "details": {}})


def _body(payload, command_id: str, tenant_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    if not command_id:
        raise ServiceError(400, "INVALID_BODY", "Idempotency-Key is required")
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    inside = payload.get("command_id")
    if inside is not None and inside != command_id:
        raise ServiceError(400, "INVALID_BODY",
                           "Idempotency-Key does not match body command_id")
    candidate = payload.get("tenant_id")
    if candidate is not None and candidate != tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放路径**不重新路由**：上游的余额和能力在两分钟之间会变，重放一次就
    等于把"当时给出的报价"换成了一个新报价，而 S26 手里拿的还是旧的那条。
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


# ------------------------------------------------------------------ 命令

@app.post("/route-decisions")
def estimate_route(payload: dict,
                   command_id: str = Header(alias="Idempotency-Key"),
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.route(
                       x_tenant_id, command_id, body, request_hash))


# ------------------------------------------------------------------ 查询

@app.get("/route-decisions/{route_id}")
def get_route(route_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.get_route(x_tenant_id, route_id)


@app.get("/route-decisions")
def list_routes(x_tenant_id: str = Header(alias="X-Tenant-Id"),
                attempt_id: str | None = Query(default=None),
                node_id: str | None = Query(default=None),
                limit: int = Query(default=50)):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.list_routes(x_tenant_id, attempt_id=attempt_id,
                               node_id=node_id, limit=limit)


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
