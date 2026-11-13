"""S19 HTTP 层。

契约面情况（C3）：`service-catalog.v2.json` 对 S19 **只声明** `POST
/agent-tasks`，`contracts/openapi.v2.yaml` 里一个 S19 路径都没有。本层按方案
§2.2 / §2.3 实现完整命令与查询面，**不动 catalog、不新增 openapi 文件**——
那是契约决定，不是实现者能裁决的。

租户一律来自 `X-Tenant-Id`；`body.context.tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入，所以此处**必须**与签名头
一致，否则拒绝创建。

`/agent-tasks/{id}:cancel` 的 `command_id` 走 `Idempotency-Key`，不走 body：
同一个取消重复投递必须是幂等重放，而不是第二次状态变更。
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

from agent_runtime.deps import (
    build_audit_sink,
    build_generation_source,
    build_pack_source,
    build_permission_source,
    build_policy_source,
    build_schema_source,
    build_template_source,
    build_tool_source,
)
from agent_runtime.schema import CONTRACT_GAPS
from agent_runtime.service import AgentRuntimeService, ServiceError

DB_PATH = os.environ.get("S19_DB", ":memory:")

app = FastAPI(title="S19 agent-runtime", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = AgentRuntimeService(
    _conn,
    permission_source=build_permission_source(os.environ.get("S01_BASE_URL")),
    policy_source=build_policy_source(os.environ.get("S02_BASE_URL")),
    pack_source=build_pack_source(os.environ.get("S18_BASE_URL")),
    template_source=build_template_source(os.environ.get("S20_BASE_URL")),
    generation_source=build_generation_source(os.environ.get("S26_BASE_URL")),
    schema_source=build_schema_source(os.environ.get("S19_SCHEMA_BASE_URL")),
    tool_source=build_tool_source(os.environ.get("S19_TOOL_BROKER_URL")),
    # §7.5：唯一可降级的上游。未配置时是 no-op，不是 503。
    audit_sink=build_audit_sink(os.environ.get("S45_BASE_URL")),
)
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
    # 同键不同 body：409 拒绝，不覆盖。
    return _err(409, getattr(exc, "code", None) or "IDEMPOTENCY_KEY_REUSED",
                str(exc), False)


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(
        status_code=409, headers={"Retry-After": "1"},
        content={"code": "COMMAND_IN_PROGRESS", "message": str(exc),
                 "retryable": True, "correlation_id": "", "details": {}})


def _body(payload, command_id: str, tenant_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ServiceError(400, "INVALID_SCHEMA",
                           "body must be a JSON object")
    if not command_id:
        raise ServiceError(400, "INVALID_SCHEMA", "Idempotency-Key is required")
    if not tenant_id:
        raise ServiceError(400, "INVALID_SCHEMA", "X-Tenant-Id is required")
    inside = payload.get("command_id")
    if inside is not None and inside != command_id:
        raise ServiceError(400, "INVALID_SCHEMA",
                           "Idempotency-Key does not match body command_id")
    candidate = payload.get("tenant_id")
    if candidate is not None and candidate != tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    context = payload.get("context")
    if isinstance(context, dict):
        inner = context.get("tenant_id")
        if inner is not None and inner != tenant_id:
            raise ServiceError(
                403, "TENANT_MISMATCH",
                "context.tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放路径**不重新做准入**：权限、策略、pack 水位在两分钟之间都可能变，
    重新判定会让同一个 `command_id` 得到一个不同的任务（违反 E12）。
    """
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


# ------------------------------------------------------------------ 定义

@app.post("/agent-definitions")
def create_definition(payload: dict,
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """容器创建不是幂等命令之外的业务事实，但仍要求租户头。

    §2.2 把它列为 `agent.definition.manage` 命令，所以这里同样要求
    `Idempotency-Key`——重复投递必须得到同一个容器，而不是 409 库唯一冲突。
    """
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_SCHEMA", "X-Tenant-Id is required")
    body = _body(payload, payload.get("command_id") or "", x_tenant_id)
    return service.create_definition(x_tenant_id, body)


@app.post("/agent-definitions/{definition_id}/revisions")
def publish_definition(definition_id: str, payload: dict,
                       command_id: str = Header(alias="Idempotency-Key"),
                       x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda: service.publish_definition(
                       x_tenant_id, definition_id, body))


# ------------------------------------------------------------------ 命令

@app.post("/agent-tasks")
def create_task(payload: dict,
                command_id: str = Header(alias="Idempotency-Key"),
                x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda: service.create_task(x_tenant_id, body))


@app.post("/agent-tasks/{task_id}:cancel")
def cancel_task(task_id: str, payload: dict | None = None,
                command_id: str = Header(alias="Idempotency-Key"),
                x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda: service.cancel_task(x_tenant_id, task_id, body))


# ------------------------------------------------------------------ 查询

@app.get("/agent-tasks/{task_id}")
def get_task(task_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_SCHEMA", "X-Tenant-Id is required")
    return service.get_task(x_tenant_id, task_id)


@app.get("/agent-tasks/{task_id}/result")
def get_task_result(task_id: str,
                    x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_SCHEMA", "X-Tenant-Id is required")
    return service.get_result(x_tenant_id, task_id)


@app.get("/agent-tasks/{task_id}/steps")
def get_task_steps(task_id: str,
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_SCHEMA", "X-Tenant-Id is required")
    return service.get_steps(x_tenant_id, task_id)


@app.get("/agent-definitions/{definition_id}/revisions/{revision}")
def get_definition(definition_id: str, revision: int,
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_SCHEMA", "X-Tenant-Id is required")
    return service.get_definition(x_tenant_id, definition_id, revision)


@app.get("/agent-roles")
def list_roles(x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_SCHEMA", "X-Tenant-Id is required")
    return service.list_roles(x_tenant_id)


# ------------------------------------------------------------------ 执行面

@app.post("/agent-tasks:acquire")
def acquire_task(payload: dict,
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """worker 抢任务。返回 204 语义用 `{"task": null}` 表示队列空。

    这条路径不在 §2.2 的命令清单里，因为它是**执行面**而非契约命令：
    worker 与 S19 是同进程/同集群内部调用，不经租户幂等键。
    """
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_SCHEMA", "X-Tenant-Id is required")
    role = payload.get("role")
    owner = payload.get("owner")
    if not role or not owner:
        raise ServiceError(400, "INVALID_SCHEMA", "role and owner are required")
    return {"task": service.acquire_task(x_tenant_id, role, owner)}


@app.get("/health/readiness")
def readiness():
    row = _conn.execute("SELECT 1").fetchone()
    return {"ready": row is not None, "contract_gaps": CONTRACT_GAPS}
