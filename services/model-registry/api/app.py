"""S24 HTTP 层。契约见 plans/S24-model-registry.md §3 与 §5。

`contracts/openapi.v2.yaml` 里 S24 目前**没有任何 path**（C24-4），所以本层照
方案的 §3.1–§3.4、§5.3 落地，并在方案 §10 登记差异。所有写命令强制
`Idempotency-Key` + `X-Tenant-Id`；跨租户一律 403/404（E28）。
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

from model_registry.service import ModelRegistryService, ServiceError

DB_PATH = os.environ.get("S24_DB", ":memory:")

app = FastAPI(title="S24 model-registry", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
service = ModelRegistryService(_conn)
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


def _body(payload: Any, command_id: str, tenant_id: str) -> dict:
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
        # AGENTS.md：LLM/请求体给的 tenant_id 不可信，租户上下文由平台签名绑定
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return {**payload, "command_id": command_id, "tenant_id": tenant_id}


def command(tenant_id: str, body: dict, execute):
    """幂等占位 → 执行 → 落结果；DONE 同 hash 原样重放，异 hash 409。"""
    request_hash = canonical_request_hash(body)
    command_id = body["command_id"]
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


# ---- 写命令 --------------------------------------------------------------

@app.post("/channels")
def register_channel(payload: dict,
                     command_id: str = Header(alias="Idempotency-Key"),
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.register_channel(body))


@app.post("/account-pools")
def register_pool(payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, lambda: service.register_pool(body))


@app.post("/account-pools/{pool_id}/health")
def set_pool_health(pool_id: str, payload: dict,
                    command_id: str = Header(alias="Idempotency-Key"),
                    x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    health = body.get("health")
    if not isinstance(health, str):
        raise ServiceError(400, "INVALID_REQUEST", "health is required")
    return command(x_tenant_id, body, lambda: service.set_pool_health(
        pool_id, health, body.get("reason")))


@app.post("/account-pools/{pool_id}/probe-outcomes")
def record_probe_outcome(pool_id: str, payload: dict,
                         command_id: str = Header(alias="Idempotency-Key"),
                         x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§4.3 的探测结论落点。`status` 是探测拿到的 HTTP 状态码，不是健康态——
    401/403 → `unauthorized`、连续 3 次失败 → `suspect` 由服务推导。"""
    body = _body(payload, command_id, x_tenant_id)
    status = body.get("status")
    if not isinstance(status, int) or isinstance(status, bool):
        raise ServiceError(400, "INVALID_REQUEST",
                           "status must be the integer HTTP status of the probe")
    return command(x_tenant_id, body, lambda: service.record_probe_outcome(
        pool_id, status=status, method=body.get("method") or "account_probe",
        reason=body.get("reason")))


@app.post("/capability-snapshots")
def register_snapshot(payload: dict,
                      command_id: str = Header(alias="Idempotency-Key"),
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, lambda: service.register_snapshot(body))


@app.post("/capability-snapshots/{snapshot_id}/transitions")
def transition(snapshot_id: str, payload: dict,
               command_id: str = Header(alias="Idempotency-Key"),
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, lambda: service.transition(
        x_tenant_id, {**body, "snapshot_id": snapshot_id}))


@app.post("/price-snapshots")
def publish_price(payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§3.4 的价格发布。价格随快照提交，单独发布走这里补登。"""
    body = _body(payload, command_id, x_tenant_id)
    snap = body.get("snapshot")
    if not isinstance(snap, dict):
        raise ServiceError(400, "INVALID_REQUEST", "snapshot is required")
    return command(x_tenant_id, body, lambda: {
        "price_snapshot_ids": [service._write_price(p, snap)
                               for p in (body.get("prices") or [])]})


@app.post("/capability-checks")
def check_capability(payload: dict,
                     command_id: str = Header(alias="Idempotency-Key"),
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§3.3。判定本身不写库，但结果会进 S25 的估价与 S26 的准入，所以同样
    要求幂等头——同一次派单的重试必须读到同一份结论。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.check_capability(x_tenant_id, body))


@app.post("/capability-validity")
def validity(payload: dict, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§5.3：批量体，POST 只因为参数长，不是命令，不要 command_id。"""
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    if not isinstance(payload, dict):
        raise ServiceError(400, "MALFORMED_REQUEST", "body must be a JSON object")
    return service.validity(x_tenant_id, payload)


@app.post("/authorization-projections")
def apply_projection(payload: dict,
                     command_id: str = Header(alias="Idempotency-Key"),
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """S01 授权投影的落点。写入者只应是 S01 的事件消费者；S24 不自己算授权。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, lambda: service.apply_authorization_projection(
        body.get("subject_tenant_id") or x_tenant_id,
        home_region=body.get("home_region"),
        allowed_residency_tags=body.get("allowed_residency_tags") or [],
        source_revision=body.get("source_revision"),
        resolved_at=body.get("resolved_at")))


@app.post("/events:consume")
def consume_event(payload: dict, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """S01 事件入口的最小实现：只收授权投影类事件，其余原样记录后忽略。

    inbox 幂等由 SDK 负责。本轮**没有**接 S01 的真实事件契约（S01 的
    `tenant.authorization.changed` 尚未锁定，C24-2），所以不解析 data 字段，
    只承认 `authorization_projection` 一种形状。
    """
    envelope = payload.get("event", payload)
    if not isinstance(envelope, dict):
        raise ServiceError(400, "MALFORMED_REQUEST", "event must be an object")
    if envelope.get("tenant_id") not in (None, x_tenant_id):
        raise ServiceError(403, "TENANT_MISMATCH",
                           "event tenant disagrees with the signed context")
    data = envelope.get("data") or {}
    if envelope.get("type") == "tenant.authorization.projected" \
            and isinstance(data, dict) and data.get("home_region") is not None:
        service.apply_authorization_projection(
            data.get("tenant_id") or x_tenant_id,
            home_region=data.get("home_region"),
            allowed_residency_tags=data.get("allowed_residency_tags") or [],
            source_revision=data.get("source_revision"),
            resolved_at=data.get("resolved_at"))
        return {"applied": True}
    return {"applied": False}


# ---- 读 ----------------------------------------------------------------

@app.get("/model-candidates")
def candidates(tenant_id: str,
               modality: str | None = None,
               home_region: str | None = None,
               required_capabilities: str | None = None,
               mode: str | None = None,
               aspect_ratio: str | None = None,
               duration_seconds: float | None = None,
               residency: str | None = None,
               allow_byok: str = "true",
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§3.2。`home_region` 是普通查询参数，不是签名上下文的一部分。"""
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    if tenant_id != x_tenant_id:
        raise ServiceError(403, "CROSS_TENANT_DENIED",
                           "query tenant_id disagrees with the signed context")
    return service.candidates(x_tenant_id, {
        "modality": modality,
        "home_region": home_region,
        "required_capabilities": required_capabilities,
        "mode": mode,
        "aspect_ratio": aspect_ratio,
        "duration_seconds": duration_seconds,
        "residency": residency,
        "allow_byok": allow_byok,
    })


@app.get("/capability-snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str,
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.snapshot(x_tenant_id, snapshot_id)


@app.get("/capability-snapshots/{snapshot_id}/lifecycle-history")
def lifecycle_history(snapshot_id: str,
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.lifecycle_history(x_tenant_id, snapshot_id)


@app.get("/price-snapshots/{price_snapshot_id}")
def get_price(price_snapshot_id: str,
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.price_snapshot(x_tenant_id, price_snapshot_id)


@app.get("/registry/bundle-revision")
def bundle_revision(x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return {"capability_bundle_revision": service.bundle_revision()}


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}
