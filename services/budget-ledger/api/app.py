"""S23 HTTP 层。语义见 plans/S23-budget-ledger.md §2。

与 `contracts/openapi.v2.yaml` 的差异（C23-1 / C23-2，已登记，不在此处自行改契约）：

- openapi 只有 `GET /budgets/{budget_id}`，**没有** `POST /budget-authorizations`。
  命令入口按 service.yaml 与方案 §2.1 落地。
- `GET /budgets/{budget_id}` 的响应是 `additionalProperties: false` 的六字段，
  **不含 available_micro**，所以可用额单独走 `/availability`。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from typing import Any

from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from budget_ledger.schema import SCHEMA
from budget_ledger.service import BudgetLedgerService, ServiceError

DB_PATH = os.environ.get("S23_DB", ":memory:")

app = FastAPI(title="S23 budget-ledger", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.executescript(SCHEMA)
_idem = SqliteIdempotencyStore(_conn)
service = BudgetLedgerService(_conn)


def _err(status: int, code: str, message: str, retryable: bool = False,
         details: dict | None = None, correlation_id: str = ""):
    # 形状必须匹配 openapi 的 Error：**扁平**，无 `additionalProperties`，
    # 且 correlation_id 是必填的 minLength: 1。
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "retryable": retryable,
                 "correlation_id": correlation_id or "s23", "details": details or {}})


@app.exception_handler(ServiceError)
async def _service_error(_request, exc: ServiceError):
    return _err(exc.status, exc.code, exc.message, exc.retryable, exc.details)


@app.exception_handler(CommandConflict)
async def _conflict(_request, exc: CommandConflict):
    # 同 (tenant, command_id) 不同 body：拒，不覆盖（AGENTS.md 幂等键约定）
    return _err(409, "IDEMPOTENCY_KEY_REUSED", str(exc))


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(status_code=429, headers={"Retry-After": "1"},
                        content={"code": "LEDGER_BACKPRESSURE",
                                 "message": str(exc), "retryable": True,
                                 "correlation_id": "s23", "details": {}})


def _body(payload: Any, command_id: str, tenant_id: str) -> dict:
    """头缺失在 FastAPI 里会变 422；§2.7 要求结构问题一律 400，所以头声明为可选、
    在这里自己判。

    body 里的 `tenant_id` **不能**覆盖头：租户上下文由平台签名绑定，
    请求体里的 tenant_id 是不可信输入（AGENTS.md 红线）。
    """
    if not isinstance(payload, dict):
        raise ServiceError(400, "MALFORMED_REQUEST", "body must be a JSON object")
    if not tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    if not command_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "Idempotency-Key is required")
    inside = payload.get("tenant_id")
    if inside is not None and inside != tenant_id:
        raise ServiceError(
            403, "TENANT_SCOPE_DENIED",
            "body tenant_id conflicts with the authenticated tenant",
            details={"body_tenant_id": inside})
    return {**payload, "command_id": command_id, "tenant_id": tenant_id}


def command(body: dict, execute):
    """幂等占位 → 执行 → 落结果；DONE 同 hash 原样重放。

    这层与账本层 `business_key` 的唯一约束**是两回事**：这里管"客户端重试"，
    那里管"内部重复记账"。第二层不能替代第一层（§1.10）。
    """
    tenant_id = body["tenant_id"]
    command_id = body["command_id"]
    request_hash = canonical_request_hash(body)
    record = _idem.begin(tenant_id, command_id, request_hash)
    if record.result is not None:
        replay = json.loads(record.result)
        replay["replayed"] = True
        return replay
    try:
        result = execute()
    except Exception:
        _idem.abort(tenant_id, command_id)
        raise
    _idem.finish(tenant_id, command_id,
                 json.dumps(result, ensure_ascii=False, default=str))
    return {**result, "replayed": False}


# ---- 预算 ----------------------------------------------------------------

@app.post("/budgets")
def create_budget(payload: dict,
                  command_id: str = Header(default="", alias="Idempotency-Key"),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.create_budget(
        x_tenant_id, body, actor={"service": "api"}, correlation_id=command_id))


@app.post("/budgets/{budget_id}:set-limit")
def set_limit(budget_id: str, payload: dict,
              command_id: str = Header(default="", alias="Idempotency-Key"),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.set_limit(
        x_tenant_id, budget_id, body, actor={"service": "api"},
        correlation_id=command_id))


@app.post("/escrows")
def escrow(payload: dict,
           command_id: str = Header(default="", alias="Idempotency-Key"),
           x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """`action` 区分发放与退回：掺两个字段比掺两个路径更不容易用错。"""
    body = _body(payload, command_id, x_tenant_id)
    action = body.get("action") or "grant"
    if action == "grant":
        return command(body, lambda: service.grant_escrow(
            x_tenant_id, body, actor={"service": "api"}, correlation_id=command_id))
    if action == "return":
        return command(body, lambda: service.return_escrow(
            x_tenant_id, body, actor={"service": "api"}, correlation_id=command_id))
    raise ServiceError(400, "MALFORMED_REQUEST", "action must be grant or return",
                       details={"action": action})


@app.post("/budget-freezes")
def freeze(payload: dict,
           command_id: str = Header(default="", alias="Idempotency-Key"),
           x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    budget_id = body.get("budget_id")
    if not budget_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "budget_id is required")
    frozen = bool(body.get("frozen", True))
    return command(body, lambda: service.set_frozen(
        x_tenant_id, budget_id, body, frozen=frozen, actor={"service": "api"},
        correlation_id=command_id))


# ---- 准入 ----------------------------------------------------------------

@app.post("/budget-authorizations")
def budget_authorization(payload: dict,
                         command_id: str = Header(default="", alias="Idempotency-Key"),
                         x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """准入第 1 步：预留。intent 只接受 RESERVE —— 其余步骤有各自路径。"""
    body = _body(payload, command_id, x_tenant_id)
    intent = body.get("intent") or "RESERVE"
    if intent != "RESERVE":
        raise ServiceError(
            400, "MALFORMED_REQUEST",
            "this path only accepts intent RESERVE; use the reservation"
            " sub-resources for later steps", details={"intent": intent})
    return command(body, lambda: service.reserve(
        x_tenant_id, body, actor={"service": "api"}, correlation_id=command_id))


@app.post("/reservations/{reservation_id}:authorize")
def authorize(reservation_id: str, payload: dict,
              command_id: str = Header(default="", alias="Idempotency-Key"),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.authorize(
        x_tenant_id, reservation_id, body, actor={"service": "api"},
        correlation_id=command_id))


@app.post("/reservations/{reservation_id}:release")
def release(reservation_id: str, payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.release(
        x_tenant_id, reservation_id, body, actor={"service": "api"},
        correlation_id=command_id))


@app.post("/reservations/{reservation_id}:accrue")
def accrue(reservation_id: str, payload: dict,
           command_id: str = Header(default="", alias="Idempotency-Key"),
           x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.accrue(
        x_tenant_id, reservation_id, body, actor={"service": "api"},
        correlation_id=command_id))


@app.post("/reservations/{reservation_id}:settle")
def settle(reservation_id: str, payload: dict,
           command_id: str = Header(default="", alias="Idempotency-Key"),
           x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.settle(
        x_tenant_id, reservation_id, body, actor={"service": "api"},
        correlation_id=command_id))


# ---- 调整 ----------------------------------------------------------------

@app.post("/refunds")
def refund(payload: dict,
           command_id: str = Header(default="", alias="Idempotency-Key"),
           x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.record_refund(
        x_tenant_id, body, actor={"service": "api"}, correlation_id=command_id))


@app.post("/fx-snapshots")
def fx_snapshot(payload: dict,
                command_id: str = Header(default="", alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.register_fx_snapshot(x_tenant_id, body))


# ---- 查询 ----------------------------------------------------------------

@app.get("/budgets")
def list_budgets(scope_type: str | None = Query(None),
                 scope_ref: str | None = Query(None),
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return {"items": service.list_budgets(x_tenant_id, scope_type=scope_type,
                                          scope_ref=scope_ref)}


@app.get("/budgets/{budget_id}")
def get_budget(budget_id: str,
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """严格六字段（openapi `additionalProperties: false`）。"""
    return service.get_budget(x_tenant_id, budget_id)


@app.get("/budgets/{budget_id}/availability")
def availability(budget_id: str,
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.availability(x_tenant_id, budget_id)


@app.get("/budgets/{budget_id}/tree")
def tree(budget_id: str,
         x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.tree(x_tenant_id, budget_id)


@app.get("/reservations")
def list_reservations(attempt_id: str | None = Query(None),
                      state: str | None = Query(None),
                      run_id: str | None = Query(None),
                      since: str | None = Query(None),
                      until: str | None = Query(None),
                      x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return {"items": service.list_reservations(
        x_tenant_id, attempt_id=attempt_id, state=state, run_id=run_id,
        since=since, until=until)}


@app.get("/reservations/{reservation_id}")
def get_reservation(reservation_id: str,
                    x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_reservation(x_tenant_id, reservation_id)


@app.get("/ledger-entries")
def ledger_entries(budget_id: str | None = Query(None),
                   from_entry_seq: int | None = Query(None),
                   limit: int = Query(200, ge=1, le=1000),
                   x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return {"items": service.ledger_entries(
        x_tenant_id, budget_id=budget_id, from_entry_seq=from_entry_seq,
        limit=limit)}


@app.get("/attempts/{attempt_id}/cost")
def attempt_cost(attempt_id: str,
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.attempt_cost(x_tenant_id, attempt_id)


@app.get("/episodes/{episode_id}/cost")
def episode_cost(episode_id: str,
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.episode_cost(x_tenant_id, episode_id)


@app.get("/reconciliation-queue")
def reconciliation_queue(x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return {"items": service.reconciliation_queue(x_tenant_id)}


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}
