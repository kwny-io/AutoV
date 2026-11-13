"""S07 HTTP 层。

**响应体是 `enterprise-change-plan.v2` 的实例，没有包装。** 那份 schema 是
`additionalProperties: false`，装不下 `state` / `replayed` / `clarification`
（C07-5）。所以：

- 契约层看到的是纯 plan；
- 状态走 `X-Plan-State`；
- 重放标记走 `X-Idempotent-Replay`；
- Saga 逐步状态走 §5.3 的 `GET /change-plans/{id}/saga`。

`CLARIFICATION_REQUIRED` 不是 2xx（C07-6）：`patches.minItems: 1`，一份
"待确认"的计划填不出合法的 plan，所以 409 + `details.clarification`。

租户一律来自 `X-Tenant-Id`；body 里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 或调用方给的 `tenant_id` 不是可信输入。

`POST /change-plans/{id}:recover` 是 §4.3 的操作入口：方案要求"重启后只能去
S08 查"，但没写由谁触发。它不改任何外部事实，只在本地补齐状态。
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

from change_management.deps import (
    build_committer,
    build_content_preparer,
    build_cost_source,
    build_impact_source,
    build_manifest_source,
    build_memory_committer,
    build_memory_preview,
    build_policy_source,
    build_revision_detail_reader,
    build_revision_lister,
    build_revision_reader,
    build_shot_preparer,
    build_shot_source,
)
from change_management.schema import CONTRACT_GAPS
from change_management.service import (
    ChangeManagementService,
    ServiceError,
)

DB_PATH = os.environ.get("S07_DB", ":memory:")

app = FastAPI(title="S07 change-management", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

_s06 = os.environ.get("S06_BASE_URL")
_s08 = os.environ.get("S08_BASE_URL")
_s16 = os.environ.get("S16_BASE_URL")

service = ChangeManagementService(
    _conn,
    shot_resolver=build_shot_source(_s06),
    shot_preparer=build_shot_preparer(_s06),
    revision_reader=build_revision_reader(_s06),
    content_preparer=build_content_preparer(_s06),
    impact_source=build_impact_source(os.environ.get("S09_BASE_URL")),
    policy_source=build_policy_source(os.environ.get("S02_BASE_URL")),
    cost_source=build_cost_source(os.environ.get("S25_BASE_URL")),
    manifest_source=build_manifest_source(_s08),
    manifest_committer=build_committer(_s08),
    revision_lister=build_revision_lister(_s08),
    manifest_revision_reader=build_revision_detail_reader(_s08),
    memory_preview=build_memory_preview(_s16),
    memory_committer=build_memory_committer(_s16),
    ttl_seconds=int(os.environ.get("S07_PLAN_TTL_SECONDS", "1800")),
)
_idem = SqliteIdempotencyStore(_conn)

# 契约实例的 16 个键。响应只发这些——多一个就是违反 additionalProperties。
PLAN_KEYS = (
    "schema_version", "plan_id", "plan_hash", "context", "base_manifest_ref",
    "instruction", "prepared_revision_refs", "patches", "derived_adjustments",
    "impact", "memory_migration_required", "required_policy_decisions",
    "blocking_issues", "estimated_cost_upper_micro", "currency", "expires_at",
)


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
    # 同键不同 body：409，不是覆盖（§8）。
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

    重放路径**不重新生成计划**：上游的 manifest 版本与政策在两次调用之间会
    变，重放一次就得到另一份 hash，而用户手里确认的还是旧的那份。
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


def _plan_response(result: dict, status: int = 200):
    """把内核的 `{**plan, state, replayed}` 拆成 body + header（C07-5）。"""
    meta = {}
    for key in ("state", "replayed", "clarification"):
        if key in result:
            meta[key] = result.pop(key)
    body = {k: result.get(k) for k in PLAN_KEYS}
    headers = {"X-Plan-State": str(meta.get("state") or "PLANNED")}
    if meta.get("replayed"):
        headers["X-Idempotent-Replay"] = "true"
    return JSONResponse(status_code=status, content=body, headers=headers)


def _tenant(x_tenant_id: str) -> str:
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return x_tenant_id


# ------------------------------------------------------------------ 命令

@app.post("/change-plans")
def create_plan(payload: dict,
                command_id: str = Header(alias="Idempotency-Key"),
                x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    result = command(x_tenant_id, body, command_id,
                     lambda _hash: service.create_plan(
                         x_tenant_id, command_id, body))
    return _plan_response(dict(result))


@app.post("/change-plans/{plan_id}:apply", status_code=202)
def apply_plan(plan_id: str, payload: dict,
               command_id: str = Header(alias="Idempotency-Key"),
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    result = command(x_tenant_id, body, command_id,
                     lambda _hash: service.apply(
                         x_tenant_id, plan_id, command_id, body))
    return result


@app.post("/change-plans/{plan_id}/clarifications/{request_id}:answer")
def answer_clarification(plan_id: str, request_id: str, payload: dict,
                         command_id: str = Header(alias="Idempotency-Key"),
                         x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.answer_clarification(
                       x_tenant_id, plan_id, request_id, body))


@app.post("/change-plans/{plan_id}:recover")
def recover_plan(plan_id: str,
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§4.3：重启后去 S08 查，不在本地猜。只读上游、只改本地状态。"""
    return service.recover_apply(_tenant(x_tenant_id), plan_id)


@app.post("/events:consume")
def consume_event(payload: dict,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """事件入口。inbox 自己就是幂等，不需要 Idempotency-Key。"""
    envelope = payload.get("event", payload)
    if not isinstance(envelope, dict):
        raise ServiceError(422, "INVALID_CONTEXT", "event must be an object")
    if envelope.get("tenant_id") is None:
        envelope = {**envelope, "tenant_id": _tenant(x_tenant_id)}
    elif envelope["tenant_id"] != x_tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "event tenant disagrees with the signed context")
    return service.consume(envelope)


# ------------------------------------------------------------------ 查询

@app.get("/change-plans/{plan_id}")
def get_plan(plan_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    tenant_id = _tenant(x_tenant_id)
    body = service.get_plan(tenant_id, plan_id)
    state = service.plan_state(tenant_id, plan_id)
    return _plan_response({**body, "state": state["state"]})


@app.get("/change-plans")
def list_plans(x_tenant_id: str = Header(alias="X-Tenant-Id"),
               series_id: str | None = Query(default=None),
               episode_id: str | None = Query(default=None),
               state: str | None = Query(default=None),
               after: str | None = Query(default=None),
               limit: int = Query(default=50)):
    return service.list_plans(_tenant(x_tenant_id), series_id=series_id,
                              episode_id=episode_id, state=state, after=after,
                              limit=limit)


@app.get("/change-plans/{plan_id}/saga")
def get_saga(plan_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_saga(_tenant(x_tenant_id), plan_id)


@app.get("/change-plans/{plan_id}/impact-summary")
def get_impact_summary(plan_id: str,
                       x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_impact_summary(_tenant(x_tenant_id), plan_id)


@app.get("/change-plans/{plan_id}/clarifications")
def get_clarifications(plan_id: str,
                       x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_clarifications(_tenant(x_tenant_id), plan_id)


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
