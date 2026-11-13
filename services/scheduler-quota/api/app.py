"""S22 HTTP 层。语义见 plans/S22-scheduler-quota.md §2。

与 `contracts/openapi.v2.yaml` 的差异（C22-1 / C22-2 / C22-3，已登记，
不在此处自行改契约）：openapi 里**完全没有** S22 的 permits / quota-pools
路径，也没有 `index` 与 `acquire` 的请求体定义。这里按 service.yaml 的
命令入口 + 方案 §2 落地。

`GET /quota-pools/{id}` 的响应是方案 §2.5 的形状，**包含** `source_revision`
与 `reserved_not_dispatched`：运维必须能一眼看出"占着槽还没发出去"的量。

本文件里有两处刻意的越界校验，都对应 T22-41：

- `:expire` / `:suspend` / `:resume` / `:drain` / `:reconcile` / `:cache-rebuild`
  是内部端点。没有平台签名的内部身份头一律 403 `TENANT_SCOPE_DENIED`。
- `:resume` 必须带 `authorization_ref`（S02 的授权引用），缺了同样 403。
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

from scheduler_quota.schema import SCHEMA
from scheduler_quota.service import SchedulerQuotaService, ServiceError

DB_PATH = os.environ.get("S22_DB", ":memory:")

app = FastAPI(title="S22 scheduler-quota", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.executescript(SCHEMA)
_idem = SqliteIdempotencyStore(_conn)
service = SchedulerQuotaService(_conn)

# §2.4 / §2.6：这些端点只给内部 principal。
INTERNAL_PRINCIPALS = {"S22", "S02", "S26", "janitor", "ops", "test"}


def _err(status: int, code: str, message: str, retryable: bool = False,
         details: dict | None = None, correlation_id: str = ""):
    # 形状匹配 openapi 的 Error：扁平，correlation_id 必填 minLength 1。
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "retryable": retryable,
                 "correlation_id": correlation_id or "s22", "details": details or {}})


@app.exception_handler(ServiceError)
async def _service_error(_request, exc: ServiceError):
    return _err(exc.status, exc.code, exc.message, exc.retryable, exc.details)


@app.exception_handler(CommandConflict)
async def _conflict(_request, exc: CommandConflict):
    # 同 (tenant, command_id) 不同 body：拒，不覆盖（AGENTS.md 幂等键约定）。
    return _err(409, "IDEMPOTENCY_KEY_REUSED", str(exc))


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(status_code=429, headers={"Retry-After": "1"},
                        content={"code": "COMMAND_IN_PROGRESS",
                                 "message": str(exc), "retryable": True,
                                 "correlation_id": "s22", "details": {}})


def _body(payload: Any, command_id: str, tenant_id: str) -> dict:
    """头缺失在 FastAPI 里会变 422；§2.7 要求结构问题一律 400。

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


def _internal(principal: str) -> None:
    if principal not in INTERNAL_PRINCIPALS:
        raise ServiceError(
            403, "TENANT_SCOPE_DENIED",
            "this endpoint is internal; caller identity is not an internal principal",
            details={"principal": principal or None})


def command(body: dict, execute):
    """幂等占位 → 执行 → 落结果；同 hash 原样重放。"""
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


# ---- 池 ------------------------------------------------------------------

@app.post("/quota-pools")
def create_pool(payload: dict,
                command_id: str = Header(default="", alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.create_pool(
        x_tenant_id, body, actor={"service": "api"}, correlation_id=command_id))


@app.post("/pools/{pool_id}:set-limits")
def set_limits(pool_id: str, payload: dict,
               command_id: str = Header(default="", alias="Idempotency-Key"),
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
               x_principal: str = Header(default="", alias="X-Principal")):
    _internal(x_principal)
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.set_pool_limits(
        x_tenant_id, pool_id, body, actor={"service": x_principal or "ops"},
        correlation_id=command_id))


@app.post("/pools/{pool_id}:suspend")
def suspend(pool_id: str, payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
            x_principal: str = Header(default="", alias="X-Principal")):
    _internal(x_principal)
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.suspend_pool(
        x_tenant_id, pool_id, body, actor={"service": x_principal},
        correlation_id=command_id))


@app.post("/pools/{pool_id}:resume")
def resume(pool_id: str, payload: dict,
           command_id: str = Header(default="", alias="Idempotency-Key"),
           x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
           x_principal: str = Header(default="", alias="X-Principal")):
    _internal(x_principal)
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.resume_pool(
        x_tenant_id, pool_id, body, actor={"service": x_principal},
        correlation_id=command_id))


@app.post("/pools/{pool_id}:drain")
def drain(pool_id: str, payload: dict | None = None,
          command_id: str = Header(default="", alias="Idempotency-Key"),
          x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
          x_principal: str = Header(default="", alias="X-Principal")):
    _internal(x_principal)
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(body, lambda: service.drain_pool(
        x_tenant_id, pool_id, body, actor={"service": x_principal},
        correlation_id=command_id))


# ---- permit 命令 ---------------------------------------------------------

@app.post("/permits:acquire")
def acquire(payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.acquire(
        x_tenant_id, body, actor={"service": "api"}, correlation_id=command_id))


@app.post("/permits/{permit_id}:confirm-dispatch")
def confirm_dispatch(permit_id: str, payload: dict,
                     command_id: str = Header(default="", alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.confirm_dispatch(
        x_tenant_id, permit_id, body, actor={"service": "api"},
        correlation_id=command_id))


@app.post("/permits/{permit_id}:release")
def release(permit_id: str, payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.release(
        x_tenant_id, permit_id, body, actor={"service": "api"},
        correlation_id=command_id))


@app.post("/permits/{permit_id}:expire")
def expire(permit_id: str, payload: dict | None = None,
           command_id: str = Header(default="", alias="Idempotency-Key"),
           x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
           x_principal: str = Header(default="", alias="X-Principal")):
    """§2.4：只能由 S22 自己的 janitor 调用。"""
    _internal(x_principal)
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(body, lambda: service.expire(
        permit_id, actor={"service": x_principal}, correlation_id=command_id,
        tenant_id=x_tenant_id))


# ---- 内部运维 ------------------------------------------------------------

@app.post("/reconcile")
def reconcile(payload: dict | None = None,
              command_id: str = Header(default="", alias="Idempotency-Key"),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
              x_principal: str = Header(default="", alias="X-Principal")):
    _internal(x_principal)
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(body, lambda: service.reconcile())


@app.post("/cache:rebuild")
def cache_rebuild(payload: dict | None = None,
                  command_id: str = Header(default="", alias="Idempotency-Key"),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                  x_principal: str = Header(default="", alias="X-Principal")):
    _internal(x_principal)
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(body, lambda: service.rebuild_cache())


@app.post("/cache:degrade")
def cache_degrade(payload: dict | None = None,
                  command_id: str = Header(default="", alias="Idempotency-Key"),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                  x_principal: str = Header(default="", alias="X-Principal")):
    """测试/演练入口：进入 §5.5 的保守窗口。真实实现由 Redis 探针触发。"""
    _internal(x_principal)
    body = _body(payload or {}, command_id, x_tenant_id)
    seconds = float(body.get("seconds") or 2.0)
    return command(body, lambda: service.mark_cache_degraded(seconds))


@app.post("/janitor:sweep")
def janitor_sweep(payload: dict | None = None,
                  command_id: str = Header(default="", alias="Idempotency-Key"),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                  x_principal: str = Header(default="", alias="X-Principal")):
    _internal(x_principal)
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(body, lambda: service.sweep_expired(
        limit=int(body.get("limit") or 200), correlation_id=command_id))


# ---- 查询 ----------------------------------------------------------------

@app.get("/quota-pools")
def list_pools(provider: str | None = Query(None),
               region: str | None = Query(None),
               modality: str | None = Query(None),
               state: str | None = Query(None),
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return {"items": service.list_pools(
        x_tenant_id, provider=provider, region=region, modality=modality,
        state=state)}


@app.get("/quota-pools/{pool_id}")
def get_pool(pool_id: str,
             x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_pool(x_tenant_id, pool_id)


@app.get("/permits")
def list_permits(attempt_id: str | None = Query(None),
                 state: str | None = Query(None),
                 run_id: str | None = Query(None),
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return {"items": service.list_permits(
        x_tenant_id, attempt_id=attempt_id, state=state, run_id=run_id)}


@app.get("/permits/{permit_id}")
def get_permit(permit_id: str,
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_permit(x_tenant_id, permit_id)


@app.get("/queues/{pool_id}")
def get_queue(pool_id: str,
              state: str = Query("WAITING"),
              limit: int = Query(100, ge=1, le=1000),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_queue(x_tenant_id, pool_id, state=state, limit=limit)


@app.get("/tenants/{tenant_id}/share")
def get_share(tenant_id: str, pool_id: str = Query(...),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    if tenant_id != x_tenant_id:
        # 别人的份额不是秘密，但也没有理由给；统一 404 不泄漏存在性。
        raise ServiceError(404, "NOT_FOUND", "tenant share not found",
                           details={"tenant_id": tenant_id},)
    return service.get_share(x_tenant_id, pool_id=pool_id)


@app.get("/capacity")
def capacity(provider: str | None = Query(None),
             region: str | None = Query(None),
             x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_capacity(x_tenant_id, provider=provider, region=region)


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready",
            "cache_degraded": service.is_degraded()}
