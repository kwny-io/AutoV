"""S47 HTTP 层。方案 §4.1 的端点表 + openapi 已有的 `streamRun`。

三件与"缓存"身份直接相关的事，在这一层落实：

1. **没有写回权威的路径。** 这个文件里没有一个出站 POST/PUT/PATCH/DELETE；
   `deps._get` 是唯一的上游原语，方法写在 `urllib.request.Request` 里。
   §8.1 的 `s47_writeback_attempts_total` 恒为 0 是结构性的。
2. **`POST /events:consume` 是平台投递入口**，不是业务命令：它带
   `Idempotency-Key` 但语义是"把这条事件交给我"，重复投递由 inbox 的
   `(consumer, event_id)` 挡。**它不接受来自外部的 tenant 覆盖**——
   `X-Tenant-Id` 必须与信封一致，否则 403。
3. **SSE 走 `StreamingResponse`**。内核给的是"游标之后的帧"，这一层负责拼成
   `id:` / `event:` / `data:` 三段 + 15s `: keepalive`（§4.3）。

契约冲突（C47-1）：openapi 只有 `GET /runs/{run_id}/events`，方案 §4.1 的其余
路径都在契约外。按方案实现，不改契约——登记在 `CONTRACT_GAPS`。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from typing import AsyncIterator

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from realtime_projection.deps import (
    build_authorization_source,
    build_run_source,
)
from realtime_projection.schema import CONTRACT_GAPS
from realtime_projection.service import RealtimeProjectionService, ServiceError

DB_PATH = os.environ.get("S47_DB", ":memory:")

app = FastAPI(title="S47 realtime-projection", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = RealtimeProjectionService(
    _conn,
    authorization_source=build_authorization_source(
        os.environ.get("S01_BASE_URL")),
    run_source=build_run_source(os.environ.get("S21_BASE_URL")),
    # 跨实例稳定的脱敏根钥。没配就用进程内默认值，并且该事实已登记 C47-22。
    redaction_secret=(os.environ.get("S47_REDACTION_KEY") or "").encode("utf-8")
    or None,
)
_idem = SqliteIdempotencyStore(_conn)


def _err(status: int, code: str, message: str, retryable: bool = False,
         details: dict | None = None, headers: dict | None = None):
    return JSONResponse(
        status_code=status, headers=headers or {},
        content={"code": code, "message": message, "retryable": retryable,
                 "correlation_id": "", "details": details or {}})


@app.exception_handler(ServiceError)
async def _service_error(_request, exc: ServiceError):
    headers = {}
    if exc.retryable:
        headers["Retry-After"] = "1"
    return _err(exc.status, exc.code, str(exc), exc.retryable, exc.details,
                headers)


@app.exception_handler(CommandConflict)
async def _conflict(_request, exc: CommandConflict):
    # §4.5：同键不同 body 是 409，不是覆盖（订阅创建的幂等语义）。
    return _err(409, exc.code, str(exc), False)


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return _err(409, "COMMAND_IN_PROGRESS", str(exc), True,
                headers={"Retry-After": "1"})


def _tenant(x_tenant_id: str | None) -> str:
    if not x_tenant_id:
        # AGENTS.md：租户上下文必须由平台签名绑定；缺了就拒绝，不猜。
        raise ServiceError(403, "TENANT_SCOPE_DENIED",
                           "X-Tenant-Id is required")
    return x_tenant_id


def _body(payload, tenant_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "body must be a JSON object")
    inside = payload.get("tenant_id")
    if inside is not None and inside != tenant_id:
        raise ServiceError(403, "TENANT_SCOPE_DENIED",
                           "body tenant_id disagrees with the signed context")
    return payload


def _command(tenant_id: str, body: dict, command_id: str | None, execute):
    """订阅创建走幂等：同键同 body 原样重放，同键异 body 409。"""
    if not command_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key is required for POST")
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
    _idem.finish(tenant_id, command_id, json.dumps(result, ensure_ascii=False))
    return {**result, "replayed": False}


# ------------------------------------------------------------------ 事件入口

@app.post("/events:consume")
def consume(payload: dict,
            x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id")):
    """平台投递入口。`data` 是完整的 EventEnvelope。

    重复投递是**正常路径**，所以这里回 200 + `{consumed:false, reason}`，
    不回 409：投递方不该因为重投而被判失败（§7.5）。
    """
    tenant = _tenant(x_tenant_id)
    if not isinstance(payload, dict):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "body must be a JSON object")
    envelope = payload.get("event", payload)
    if not isinstance(envelope, dict):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "event must be an object")
    if envelope.get("tenant_id") != tenant:
        # 事件信封的租户是被签名绑定的那一个；头不同就是越界投递。
        raise ServiceError(403, "TENANT_SCOPE_DENIED",
                           "event tenant_id disagrees with X-Tenant-Id")
    return service.consume(envelope)


# ------------------------------------------------------------------ 投影查询

@app.get("/runs/{run_id}/snapshot")
def run_snapshot(run_id: str,
                 x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id")):
    """§4.2。必带 `as_of` / `projection_watermark` / `projection_state`。

    没有余额字段：`remaining_ref` 只是一个指针（§1.2）。
    """
    return service.snapshot(_tenant(x_tenant_id), run_id)


@app.get("/runs")
def list_runs(x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id"),
              state: str | None = Query(default=None),
              before: str | None = Query(default=None),
              limit: int = Query(default=50)):
    return service.list_runs(_tenant(x_tenant_id), state=state,
                             before=before, limit=limit)


@app.get("/dashboard/summary")
def dashboard_summary(x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id")):
    return service.dashboard_summary(_tenant(x_tenant_id))


@app.post("/runs/{run_id}:resync")
def resync_run(run_id: str,
               x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id")):
    """§3.8：只读回源 S21。**不是**写回——S21 只收到一个 GET。"""
    return service.resync(_tenant(x_tenant_id), run_id)


# ------------------------------------------------------------------ SSE

def _sse_frame(frame: dict) -> str:
    """§4.3：`id` / `event` / `data` 三段。`data` 里必须有 watermark。"""
    payload = frame["data"]
    if "watermark" not in payload:  # 提前炸，别在线上发出坏帧
        raise ServiceError(500, "PROJECTION_INVARIANT",
                           "SSE frame without watermark")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {frame['id']}\nevent: {frame['event']}\ndata: {body}\n\n"


@app.get("/runs/{run_id}/events")
def stream_run(run_id: str,
               request: Request,
               x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id"),
               x_principal_id: str = Header(default="", alias="X-Principal-Id"),
               last_event_id: str | None = Header(default=None,
                                                   alias="Last-Event-ID"),
               subscription_id: str | None = Query(default=None)):
    """SSE（openapi 的 `streamRun`）。客户端带 `Last-Event-ID` 续连。

    内核按**轮询**给帧（仓库里没有 Redis 扇出层，§6.4 的降级路径），这一层
    把首帧立刻吐出去，然后按 15s 心跳保持连接。帧的 `id` 单调——客户端拿它
    当 `Last-Event-ID` 回来即可。
    """
    tenant = _tenant(x_tenant_id)
    if not x_principal_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "X-Principal-Id is required for a stream")
    opened = service.open_stream(
        tenant, x_principal_id, run_id,
        subscription_id=subscription_id, last_event_id=last_event_id)
    frames = opened["frames"]

    async def _generate() -> AsyncIterator[str]:
        try:
            for frame in frames:
                yield _sse_frame(frame)
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(opened["keepalive_seconds"])
                # §4.3：心跳是注释行，不是事件。
                yield ": keepalive\n\n"
        except asyncio.CancelledError:  # 客户端断开不是错误
            return

    return StreamingResponse(
        _generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no",
                 "X-S47-Cursor": opened["cursor"] or "",
                 "X-S47-Watermark": str(opened["watermark"])})


# ------------------------------------------------------------------ 订阅

@app.post("/subscriptions")
def create_subscription(payload: dict,
                        command_id: str | None = Header(default=None,
                                                         alias="Idempotency-Key"),
                        x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id"),
                        x_principal_id: str = Header(default="",
                                                      alias="X-Principal-Id")):
    tenant = _tenant(x_tenant_id)
    if not x_principal_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "X-Principal-Id is required")
    body = _body(payload, tenant)
    return _command(tenant, body, command_id,
                    lambda: service.create_subscription(
                        tenant, x_principal_id, body))


@app.delete("/subscriptions/{subscription_id}")
def close_subscription(subscription_id: str,
                       x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id")):
    return service.close_subscription(_tenant(x_tenant_id), subscription_id)


# ------------------------------------------------------------------ 通知

@app.get("/notification-preferences")
def get_preference(x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id"),
                   x_principal_id: str = Header(default="",
                                                 alias="X-Principal-Id")):
    return service.get_preference(_tenant(x_tenant_id), x_principal_id)


@app.post("/notification-preferences")
def put_preference(payload: dict,
                   x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id"),
                   x_principal_id: str = Header(default="",
                                                 alias="X-Principal-Id")):
    tenant = _tenant(x_tenant_id)
    return service.put_preference(tenant, x_principal_id,
                                  _body(payload, tenant))


@app.patch("/notification-preferences")
def patch_preference(payload: dict,
                     x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id"),
                     x_principal_id: str = Header(default="",
                                                   alias="X-Principal-Id")):
    # POST 与 PATCH 都是 last-write-wins（§6.3）；PATCH 只是未给字段沿用现值。
    tenant = _tenant(x_tenant_id)
    return service.put_preference(tenant, x_principal_id,
                                  _body(payload, tenant))


@app.get("/notifications")
def list_notifications(x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id"),
                       x_principal_id: str = Header(default="",
                                                     alias="X-Principal-Id"),
                       unread: bool = Query(default=False),
                       limit: int = Query(default=50)):
    return service.list_notifications(_tenant(x_tenant_id), x_principal_id,
                                      unread_only=unread, limit=limit)


@app.post("/notifications/{notification_id}:read")
def mark_read(notification_id: str,
              x_tenant_id: str | None = Header(default=None,
                                       alias="X-Tenant-Id"),
              x_principal_id: str = Header(default="",
                                            alias="X-Principal-Id")):
    return service.mark_read(_tenant(x_tenant_id), x_principal_id,
                             notification_id)


# ------------------------------------------------------------------ 运维

@app.post("/maintenance:prune")
def prune():
    """清理 15 分钟前的 change_log 与过期游标（§5.2 / T47-39）。"""
    return service.prune()


@app.get("/health/readiness")
def readiness():
    try:
        ready = _conn.execute("SELECT 1").fetchone() is not None
    except sqlite3.Error:
        # §4.5 / T47-35：存储不可用时**不**降级去读 S21（那会打挂权威）。
        return _err(503, "PROJECTION_UNAVAILABLE",
                    "projection store unavailable; S47 will not read S21 "
                    "directly", retryable=True)
    return {"ready": ready, "contract_gaps": CONTRACT_GAPS}
