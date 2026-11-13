"""S16 HTTP 层。

两条路径按 `contracts/openapi.v2.yaml` 的路径与**请求**形状实现：

    POST /series/{series_id}/memory/commits
    POST /series/{series_id}/memory/query

但两份契约与 `plans/S16-story-memory.md` 在响应形状上不一致，这里按「契约与文档
冲突 → 停下报告，不自行选边」处理：**不裁剪** plan 承诺的字段，把 openapi 允许的
子集原样保留，多出来的键放在响应顶层、并在 `x-contract-gaps` 里逐条写明。

- query 响应：openapi 只允许 `{checkpoint, claims}`（`additionalProperties: false`），
  plan §6 还承诺 `checkpoint_hash` / `withheld_claim_ids` / `existence_checks` /
  `unresolved_conflicts`。少一个 S18 就无法生成 `prohibited_assumptions`，所以
  四个键都返回；**需要 owner 决定是改 openapi 还是改 plan**。
- commits 请求：openapi 的 `claim_refs` 是裸 `type: object`（无字段定义）、body 里
  没有 `branch_id`，`POST /branches` 也不在 openapi 里。服务照 openapi 的键名收
  `claim_refs`，同时接受 `branch_id` / `batch_id` / `claim_revisions`，以便实现
  plan §3.5 / §4.1 / §4.2。

其余写命令强制 `Idempotency-Key` + `X-Tenant-Id`；跨租户一律 404/403（E28）。
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

from story_memory.service import ServiceError, StoryMemoryService

DB_PATH = os.environ.get("S16_DB", ":memory:")

CONTRACT_GAPS = [
    "query response allows only {checkpoint, claims}; plan §6 also requires "
    "checkpoint_hash, withheld_claim_ids, existence_checks, unresolved_conflicts",
    "commits request has claim_refs as a bare object array with no branch_id; "
    "plan §3.5/§4.1 need branch_id, batch_id, claim_revisions",
    "plan §4.1 POST /branches and §4.2 merge have no openapi path",
]

app = FastAPI(title="S16 story-memory", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
service = StoryMemoryService(_conn)
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
    if payload.get("command_id") not in (None, command_id):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key does not match body command_id")
    if payload.get("tenant_id") not in (None, tenant_id):
        # 租户上下文由平台签名绑定，请求体里的 tenant_id 不可信（AGENTS.md）。
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return {**payload, "command_id": command_id, "tenant_id": tenant_id}


def command(tenant_id: str, body: dict, execute):
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


# ---- 查询 ---------------------------------------------------------------

@app.post("/series/{series_id}/memory/query")
def query_memory(series_id: str, payload: dict,
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§6：查询是纯读，无副作用，不需要 Idempotency-Key。"""
    _body(payload, "query", x_tenant_id)
    result = service.query(x_tenant_id, series_id, payload)
    return {**result, "x-contract-gaps": CONTRACT_GAPS}


# ---- 命令：提交 ---------------------------------------------------------

@app.post("/series/{series_id}/memory/commits", status_code=202)
def commit_memory(series_id: str, payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.commit(x_tenant_id, series_id, body))


# ---- 命令：分支（plan §4.1，openapi 尚无这条路径） ----------------------

@app.post("/series/{series_id}/branches", status_code=201)
def fork_branch(series_id: str, payload: dict,
                command_id: str = Header(alias="Idempotency-Key"),
                x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.fork_branch(x_tenant_id, series_id, body))


@app.post("/series/{series_id}/merge-preview")
def merge_preview(series_id: str, payload: dict,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """纯读：只算冲突清单，不落正史。"""
    _body(payload, "preview", x_tenant_id)
    return service.preview_merge(x_tenant_id, series_id, payload)


# ---- 命令：候选、注册、失效下界 -----------------------------------------

@app.post("/series/{series_id}/claim-candidates", status_code=202)
def submit_candidates(series_id: str, payload: dict,
                      command_id: str = Header(alias="Idempotency-Key"),
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.submit_candidates(x_tenant_id, series_id, body))


@app.post("/series/{series_id}/clocks", status_code=201)
def register_clock(series_id: str, payload: dict,
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    _body(payload, "clock", x_tenant_id)
    return service.register_clock(x_tenant_id, series_id, payload)


@app.post("/series/{series_id}/episode-windows", status_code=201)
def register_episode_window(series_id: str, payload: dict,
                            x_tenant_id: str = Header(alias="X-Tenant-Id")):
    _body(payload, "window", x_tenant_id)
    return service.register_episode_window(x_tenant_id, series_id, payload)


@app.post("/series/{series_id}/entities", status_code=201)
def register_entity(series_id: str, payload: dict,
                    x_tenant_id: str = Header(alias="X-Tenant-Id")):
    _body(payload, "entity", x_tenant_id)
    return service.register_entity(x_tenant_id, series_id, payload)


@app.post("/series/{series_id}/invalidation-bound")
def invalidation_bound(series_id: str, payload: dict,
                       x_tenant_id: str = Header(alias="X-Tenant-Id")):
    _body(payload, "bound", x_tenant_id)
    return service.invalidation_bound(x_tenant_id, series_id, payload)


@app.post("/series", status_code=201)
def create_series(payload: dict, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    _body(payload, "series", x_tenant_id)
    return service.create_series(x_tenant_id, payload.get("series_id") or "",
                                 payload.get("branch_id", "canon"))


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}
