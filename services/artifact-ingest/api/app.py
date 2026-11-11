"""S28 HTTP 层。契约见 plans/S28-artifact-ingest.md §6。

与 openapi.v2.yaml 的差异：该文件目前没有任何 S28 路径（方案 §11.1 要求先补
契约再写业务代码）。本层按方案落地，缺口已登记，不在此处自行改契约。
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

from artifact_ingest.schema import SCHEMA
from artifact_ingest.service import ArtifactIngestService, ServiceError

DB_PATH = os.environ.get("S28_DB", ":memory:")

# host allowlist 来自配置；为空时 §4.2 要求全部拒绝（fail closed）
_HOST_ALLOWLIST = tuple(
    h.strip() for h in os.environ.get("S28_HOST_ALLOWLIST", "").split(",") if h.strip()
)

app = FastAPI(title="S28 artifact-ingest", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.executescript(SCHEMA)
_idem = SqliteIdempotencyStore(_conn)
service = ArtifactIngestService(_conn, host_allowlist=_HOST_ALLOWLIST)


def _err(status: int, code: str, message: str, retryable: bool = False,
         details: dict | None = None, correlation_id: str = ""):
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "retryable": retryable,
                 "correlation_id": correlation_id, "details": details or {}})


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
    # 头缺失在 FastAPI 里会变 422；§6.7 要求结构问题一律 400（所以头声明为可选，
    # 在这里自己判）。
    if not isinstance(payload, dict):
        raise ServiceError(400, "INVALID_REQUEST", "body must be a JSON object")
    if not tenant_id:
        raise ServiceError(400, "INVALID_REQUEST", "X-Tenant-Id is required")
    if not command_id:
        raise ServiceError(400, "INVALID_REQUEST", "Idempotency-Key is required")
    inside = payload.get("command_id")
    if inside is not None and inside != command_id:
        raise ServiceError(400, "INVALID_REQUEST",
                           "Idempotency-Key does not match body command_id")
    return {**payload, "command_id": command_id, "tenant_id": tenant_id}


def command(body: dict, execute):
    """幂等占位 → 执行 → 落结果；DONE 同 hash 原样重放。

    §7.1：导入是异步的，所以重放的是**受理响应**（202 + import_id），不是终态。
    幂等 = 受理幂等，不是结果幂等。
    """
    tenant_id = body["tenant_id"]
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

@app.post("/artifact-imports")
def create_import(payload: dict,
                  command_id: str = Header(default="", alias="Idempotency-Key"),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return JSONResponse(status_code=202,
                        content=command(body,
                                        lambda: service.create_import(x_tenant_id, body)))


@app.post("/artifacts/{artifact_id}:tombstone")
def tombstone(artifact_id: str, payload: dict,
              command_id: str = Header(default="", alias="Idempotency-Key"),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.tombstone(
        x_tenant_id, artifact_id, body, requested_by=x_tenant_id))


@app.post("/media-access-tickets")
def media_access_ticket(payload: dict,
                        command_id: str = Header(default="", alias="Idempotency-Key"),
                        x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(body, lambda: service.issue_media_access_ticket(
        x_tenant_id, body, principal_id=x_tenant_id))


# ---- 查询 ----------------------------------------------------------------

@app.get("/artifact-imports/{import_id}")
def get_import(import_id: str, x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_import(x_tenant_id, import_id)


@app.get("/artifacts")
def list_artifacts(scope_ref: str = Query(...), limit: int = 50,
                   x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    try:
        parsed = json.loads(scope_ref)
    except json.JSONDecodeError:
        raise ServiceError(400, "INVALID_REQUEST", "scope_ref must be JSON")
    return service.list_artifacts(x_tenant_id, parsed, limit)


@app.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_artifact(x_tenant_id, artifact_id)


@app.get("/artifacts/{artifact_id}/revisions/{revision}")
def get_revision(artifact_id: str, revision: int,
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_revision(x_tenant_id, artifact_id, revision)


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}
