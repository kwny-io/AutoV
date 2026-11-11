"""S08 HTTP 层。契约见 plans/S08-revision-snapshot.md §2。

与 openapi.v2.yaml 的差异（C08-1/2/3/6/7）已在实现方案 §8 登记，尚未裁决；
本层按方案落地，路径用 `{id}:commit` 形态，并补齐 openapi 缺的三条入口。
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

from revision_snapshot.schema import SCHEMA
from revision_snapshot.service import RevisionSnapshotService, ServiceError

DB_PATH = os.environ.get("S08_DB", ":memory:")

app = FastAPI(title="S08 revision-snapshot", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.executescript(SCHEMA)
_idem = SqliteIdempotencyStore(_conn)
service = RevisionSnapshotService(_conn)


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
    if not isinstance(payload, dict):
        raise ServiceError(400, "MALFORMED_REQUEST", "body must be a JSON object")
    if not command_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "Idempotency-Key is required")
    # C08-4：头与 body 的 command_id 必须一致，否则无法判断重放的是哪一笔。
    inside = payload.get("command_id")
    if inside is not None and inside != command_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key does not match body command_id")
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
    # 业务方法自己开 `with conn:`；失败时删占位，保证「无半写、可重试」。
    try:
        result = execute()
    except Exception:
        _idem.abort(tenant_id, command_id)
        raise
    _idem.finish(tenant_id, command_id, json.dumps(result, ensure_ascii=False))
    return {**result, "replayed": False}


# ---- 命令 ----------------------------------------------------------------

@app.post("/manifests/{manifest_id}:commit")
def commit(manifest_id: str, payload: dict,
           command_id: str = Header(alias="Idempotency-Key"),
           x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.commit(x_tenant_id, manifest_id, body))


@app.post("/manifests/{manifest_id}:accept")
def accept(manifest_id: str, payload: dict,
           command_id: str = Header(alias="Idempotency-Key"),
           x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.accept(x_tenant_id, manifest_id, body))


@app.post("/branches/{series_id}/{branch_id}:fence")
def fence(series_id: str, branch_id: str, payload: dict,
          command_id: str = Header(alias="Idempotency-Key"),
          x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.fence(x_tenant_id, series_id, branch_id, body))


@app.post("/branches")
def register_branch(payload: dict,
                    command_id: str = Header(alias="Idempotency-Key"),
                    x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.register_branch(x_tenant_id, body))


@app.post("/evidence/quality")
def ingest_quality(payload: dict,
                   command_id: str = Header(alias="Idempotency-Key"),
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.ingest_quality(x_tenant_id, body))


@app.post("/evidence/review")
def ingest_review(payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.ingest_review(x_tenant_id, body))


# ---- 查询 ----------------------------------------------------------------

@app.get("/manifests/{manifest_id}")
def get_manifest(manifest_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_manifest(x_tenant_id, manifest_id)


@app.get("/manifests/{manifest_id}/revisions/{revision}")
def get_revision(manifest_id: str, revision: int,
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_revision(x_tenant_id, manifest_id, revision)


@app.get("/manifests/{manifest_id}/revisions")
def list_revisions(manifest_id: str, state: str | None = None,
                   after_revision: int = 0, limit: int = 50,
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.list_revisions(x_tenant_id, manifest_id, state, after_revision, limit)


@app.get("/manifests/{manifest_id}/accepted-pointers")
def accepted_pointers(manifest_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.accepted_pointers(x_tenant_id, manifest_id)


@app.get("/manifests/{manifest_id}/node-diff")
def node_diff(manifest_id: str, from_revision: int = Query(...),
              to_revision: int | None = None, node_id: str | None = None,
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.node_diff(x_tenant_id, manifest_id, from_revision, to_revision, node_id)


@app.get("/episodes/{episode_id}/manifests")
def episode_manifests(episode_id: str, branch_id: str | None = None,
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.episode_manifests(x_tenant_id, episode_id, branch_id)


@app.get("/acceptance-commits")
def acceptance_commits(manifest_id: str | None = None, result: str | None = None,
                       limit: int = 50,
                       x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.acceptance_commits(x_tenant_id, manifest_id, result, limit)


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}
