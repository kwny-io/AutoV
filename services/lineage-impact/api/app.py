"""S09 HTTP 层。契约见 plans/S09-lineage-impact.md §5。

与 openapi.v2.yaml 的差异（C09-1/3/4/7）已在实现方案 §10 登记，尚未裁决；
本层按方案落地：catalog 的两条命令 + §5.3 的查询入口都补齐，全部强制
`X-Tenant-Id`（跨租户一律 404，E28）。
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

from lineage_impact.service import LineageImpactService, ServiceError

DB_PATH = os.environ.get("S09_DB", ":memory:")

app = FastAPI(title="S09 lineage-impact", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
service = LineageImpactService(_conn)
_idem = SqliteIdempotencyStore(_conn)


def _err(status: int, code: str, message: str, retryable: bool = False,
         details: dict | None = None):
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "retryable": retryable,
                 "correlation_id": "", "details": details or {}})


@app.exception_handler(ServiceError)
async def _service_error(_request, exc: ServiceError):
    # CONSERVATIVE_REQUIRED 的 details 要带 would_be_mode，S07 靠它决定
    # 「接受更大成本区间」还是「放弃这次修改」——不能吞掉。
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
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return {**payload, "command_id": command_id, "tenant_id": tenant_id}


def command(tenant_id: str, body: dict, execute):
    """幂等占位 → 执行 → 落结果；DONE 同 hash 原样重放。"""
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


# ---- 命令 ----------------------------------------------------------------

@app.post("/lineage-edges")
def write_edges(payload: dict,
                command_id: str = Header(alias="Idempotency-Key"),
                x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.write_edges(x_tenant_id, body))


@app.post("/impact-queries")
def query_impact(payload: dict,
                 command_id: str = Header(alias="Idempotency-Key"),
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    # C09-7：POST 但归类为 query。因为算一次有成本、且 analysis_id 要被复用，
    # 这里同样要求幂等头——与 ccd 的 Operation 语义差异已在方案里登记。
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.query_impact(x_tenant_id, body))


@app.post("/field-projections")
def project_fields(payload: dict,
                   command_id: str = Header(alias="Idempotency-Key"),
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.project_fields(
                       x_tenant_id, body.get("branch_id") or "main", body))


@app.post("/events:consume")
def consume_event(payload: dict, side_effects: bool = True,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """事件入口。inbox 自己就是幂等，不需要 Idempotency-Key。"""
    envelope = payload.get("event", payload)
    if isinstance(envelope, dict) and envelope.get("tenant_id") is None:
        envelope = {**envelope, "tenant_id": x_tenant_id}
    elif isinstance(envelope, dict) and envelope["tenant_id"] != x_tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "event tenant disagrees with the signed context")
    return service.consume(envelope, side_effects=side_effects)


@app.post("/projection/watermarks:observe-lag")
def observe_lag(payload: dict,
                command_id: str = Header(alias="Idempotency-Key"),
                x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """对账写入口：把某个源当前的落后量记下来。"""
    body = _body(payload, command_id, x_tenant_id)
    branch_id = body.get("branch_id") or "main"
    return command(x_tenant_id, body, lambda: (
        service.observe_lag(x_tenant_id, branch_id, body["source_service"],
                            int(body.get("lag_ms", 0)),
                            budget_ms=body.get("budget_ms")),
        {"observed": True})[1])


# ---- 查询 ----------------------------------------------------------------

@app.get("/impact-analyses/{analysis_id}")
def get_analysis(analysis_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_analysis(x_tenant_id, analysis_id)


@app.get("/impact-analyses")
def list_analyses(branch_id: str | None = None, after: str | None = None,
                  limit: int = 50,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.list_analyses(x_tenant_id, branch_id, after, limit)


@app.get("/lineage/edges")
def list_edges(kind: str | None = None, field_path: str | None = None,
               target_service_id: str | None = None, target_id: str | None = None,
               target_revision: int | None = None, limit: int = 200,
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    target_ref = None
    if target_id is not None:
        target_ref = {"service_id": target_service_id, "id": target_id,
                      "revision": target_revision}
    return service.edges(x_tenant_id, target_ref=target_ref, kind=kind,
                         field_path=field_path, limit=limit)


@app.get("/lineage/upstream")
def upstream(service_id: str, id: str, revision: int, depth: int = 1,
             branch_id: str = "main",
             x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.traverse(
        x_tenant_id, branch_id=branch_id,
        ref={"service_id": service_id, "id": id, "revision": revision},
        direction="upstream", depth=depth)


@app.get("/lineage/downstream")
def downstream(service_id: str, id: str, revision: int, depth: int = 1,
               branch_id: str = "main",
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.traverse(
        x_tenant_id, branch_id=branch_id,
        ref={"service_id": service_id, "id": id, "revision": revision},
        direction="downstream", depth=depth)


@app.get("/projection/watermarks")
def watermarks(branch_id: str = "main",
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.watermarks(x_tenant_id, branch_id)


@app.get("/lineage-gaps")
def gaps(branch_id: str = "main", severity: str | None = Query(None),
         x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return {"items": service.open_gaps(x_tenant_id, branch_id, severity)}


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}
