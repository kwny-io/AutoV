"""S02 HTTP 层。

`contracts/openapi.v2.yaml` 里 S02 有两条 path：`POST /policies/{policy_id}/revisions`
（命令）与 `GET /projects/{project_id}/effective-policy`（查询）。后者按方案 §5
行 317 的明文——「路径参数不够，必须带 epoch 上下文」——在本层扩展成
`POST /policy-decisions:resolve-effective`；路径版保留给 UI 粗查，两者共用同一个
解析内核，不各写一份。

所有写命令强制 `Idempotency-Key` + `X-Tenant-Id`；跨租户一律 403/404（E28）。
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

from governance_policy.service import GovernancePolicyService, ServiceError

DB_PATH = os.environ.get("S02_DB", ":memory:")

app = FastAPI(title="S02 governance-policy", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
service = GovernancePolicyService(_conn)
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
    tenant_in_body = payload.get("tenant_id")
    if tenant_in_body is None and isinstance(payload.get("context"), dict):
        tenant_in_body = payload["context"].get("tenant_id")
    if tenant_in_body is not None and tenant_in_body != tenant_id:
        # AGENTS.md：LLM/请求体给的 tenant_id 不可信，租户上下文由平台签名绑定。
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


# ---- 命令：登记一版策略 --------------------------------------------------

@app.post("/policies/{policy_id}/revisions", status_code=202)
def create_policy_revision(policy_id: str, payload: dict,
                           command_id: str = Header(alias="Idempotency-Key"),
                           x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.create_revision(x_tenant_id, policy_id, body))


# ---- 查询：有效策略 ------------------------------------------------------

@app.post("/policy-decisions:resolve-effective")
def resolve_effective(payload: dict, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§5：解析是纯函数、可缓存、无副作用，所以**不要** `Idempotency-Key`。

    它和 `:evaluate` 的区别就在这：`evaluate` 签一次性凭证（要幂等），
    `resolve` 只回答"规则是什么"。给解析加幂等键会让人以为它可以被重放成决定。
    """
    _body(payload, "resolve", x_tenant_id)
    if not isinstance(payload.get("context"), dict):
        raise ServiceError(400, "MALFORMED_REQUEST", "context is required")
    return service.resolve_effective(x_tenant_id, payload)


@app.get("/projects/{project_id}/effective-policy")
def effective_policy(project_id: str,
                     episode_id: str | None = None,
                     stage_id: str | None = None,
                     node_id: str | None = None,
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """UI 粗查版。真实派单必须走 `:resolve-effective`：这条路径带不了
    `node_epoch` / `execution_fence`，所以 `evaluated_for` 三个字段都是 null。"""
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.resolve_effective(x_tenant_id, {"context": {
        "tenant_id": x_tenant_id, "project_id": project_id,
        "episode_id": episode_id, "stage_id": stage_id, "node_id": node_id}})


@app.get("/policies/{policy_id}/revisions/{revision}")
def get_policy_revision(policy_id: str, revision: int,
                        x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """按 `policy_ref` 回源取文档正文。`policy.revision.published` 事件不带
    `document`（`data` 只有四个键），消费方要正文就靠这条。"""
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    row = service._revision(x_tenant_id, policy_id, revision)
    return {"policy_ref": {"service_id": "S02", "id": policy_id,
                           "revision": revision,
                           "content_hash": row["content_hash"]},
            "policy": json.loads(row["document"])}


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}
