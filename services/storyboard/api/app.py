"""S06 HTTP 层。

契约面（`contracts/openapi.v2.yaml` / `services/storyboard/service.yaml`）只声明
`POST /storyboards/{id}/revisions` 与 `GET /shots/{id}/revisions/{revision}`
（S06 §11 C1）。本层按方案 §2.2 / §2.3 实现全部六条命令与六条查询，**不改
catalog、不改 openapi**——补契约是维护方的事，不是实现方顺手改的事。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

`X-Principal-Id` / `X-Principal-Type` 是过渡态的 actor 来源（与 S05-G22 同）。
本仓还没有 TCT 验签中间件，缺失时如实记 `unspecified`，**不编一个像真人的
ID**。这是过渡态，不是安全边界。
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

from storyboard.deps import (
    build_agent_task_source,
    build_asset_source,
    build_binding_set_source,
    build_breakdown_source,
    build_context_pack_source,
    build_policy_source,
    build_script_source,
)
from storyboard.schema import CONTRACT_GAPS
from storyboard.service import ServiceError, StoryboardService

DB_PATH = os.environ.get("S06_DB", ":memory:")

app = FastAPI(title="S06 storyboard", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

# S05 与 S04 是硬依赖：没有分场无从分镜，没有剧本文本无从防漂移。
# S10 / S18 / S19 缺失走方案允许的降级（内联绑定、partial 上下文、人工分镜）。
service = StoryboardService(
    _conn,
    breakdown_source=build_breakdown_source(os.environ.get("S05_BASE_URL")),
    script_source=build_script_source(os.environ.get("S04_BASE_URL")),
    context_pack_source=build_context_pack_source(
        os.environ.get("S18_BASE_URL")),
    agent_task_source=build_agent_task_source(os.environ.get("S19_BASE_URL")),
    binding_set_source=build_binding_set_source(os.environ.get("S10_BASE_URL")),
    asset_source=build_asset_source(os.environ.get("S10_BASE_URL")),
    policy_source=build_policy_source(os.environ.get("S02_BASE_URL")),
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
    # 同键不同 body：409，不是覆盖（AGENTS.md）。
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
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。"""
    request_hash = canonical_request_hash(body)
    try:
        record = _idem.begin(tenant_id, command_id, request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = execute(request_hash)
        except Exception:
            # 占位回滚失败不能盖住真正的错误：只读库上 abort 同样写不进去。
            try:
                _idem.abort(tenant_id, command_id)
            except sqlite3.OperationalError:
                pass
            raise
        _idem.finish(tenant_id, command_id,
                     json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}
    except sqlite3.OperationalError as exc:
        # 内核的 `Transaction` 只映射它自己的 `BEGIN IMMEDIATE`；幂等占位与
        # 落结果在事务之外，不在这里映射就是未捕获异常 → 500。
        raise ServiceError(
            503, "STORYBOARD_UNAVAILABLE",
            f"storyboard database is not writable: {exc}",
            retryable=True) from exc


def _tenant(tenant_id: str) -> str:
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return tenant_id


def _actor(body: dict, principal_id: str, principal_type: str) -> dict:
    """`produced_by` 的 actor 来源（与 S05-G22 同构的过渡态）。

    `X-Principal-Id` 优先，缺失时回落到请求体 `actor`；两者都在且不一致 → 403。
    都缺失时**不加** `actor`，让内核如实记 `unspecified`。
    """
    inside = body.get("actor")
    if not principal_id:
        return body
    derived_type = principal_type if principal_type in ("human", "agent") \
        else "human"
    if isinstance(inside, dict) and inside.get("principal_id") not in (
            None, principal_id):
        raise ServiceError(
            403, "PERMISSION_DENIED",
            "body actor disagrees with the signed principal",
            details={"gap": "S06-G20"})
    merged = dict(inside or {})
    merged["principal_id"] = principal_id
    merged["principal_type"] = derived_type
    return {**body, "actor": merged}


# ------------------------------------------------------------------ 命令

@app.post("/storyboards/{storyboard_id}/revisions", status_code=201)
def create_revision(storyboard_id: str, payload: dict,
                    command_id: str = Header(default="",
                                             alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id"),
                    x_principal: str = Header(default="",
                                              alias="X-Principal-Id"),
                    x_principal_type: str = Header(default="",
                                                   alias="X-Principal-Type")):
    body = _actor(_body(payload, command_id, x_tenant_id),
                  x_principal, x_principal_type)
    home_cell = body.get("home_cell") or "cell_a1"
    # 路径里的 storyboard_id 可能是占位（首次创建）；内核以 body 的
    # options.parent_storyboard_id / 集与分支来决定容器。这里传 `None` 的条件
    # 是路径给的是 "new"——一个显式的「还没有 ID」标记。
    explicit = None if storyboard_id in ("new", "-") else storyboard_id
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.create_revision(
                       x_tenant_id, explicit, body, command_id=command_id,
                       request_hash=request_hash, home_cell=home_cell))


@app.post("/shots/{shot_id}/revisions")
def revise_shot(shot_id: str, payload: dict,
                branch_id: str | None = Query(default=None),
                command_id: str = Header(default="", alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                x_principal: str = Header(default="", alias="X-Principal-Id"),
                x_principal_type: str = Header(default="",
                                               alias="X-Principal-Type")):
    body = _actor(_body(payload, command_id, x_tenant_id),
                  x_principal, x_principal_type)
    branch = branch_id or body.get("branch_id")
    home_cell = body.get("home_cell") or "cell_a1"
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.revise_shot(
                       x_tenant_id, shot_id, body, branch_id=branch,
                       command_id=command_id, request_hash=request_hash,
                       home_cell=home_cell))


@app.post("/storyboards/{storyboard_id}/revisions/{revision}:seal")
def seal(storyboard_id: str, revision: int, payload: dict,
         command_id: str = Header(default="", alias="Idempotency-Key"),
         x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    home_cell = body.get("home_cell") or "cell_a1"
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.seal(
                       x_tenant_id, storyboard_id, revision, body,
                       command_id=command_id, request_hash=request_hash,
                       home_cell=home_cell))


@app.post("/shots/{shot_id}:retire")
def retire_shot(shot_id: str, payload: dict,
                branch_id: str | None = Query(default=None),
                command_id: str = Header(default="", alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    branch = branch_id or body.get("branch_id")
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.retire_shot(
                       x_tenant_id, shot_id, body, branch_id=branch,
                       command_id=command_id, request_hash=request_hash))


@app.post("/storyboards/{storyboard_id}/revisions/{revision}/shot-order")
def reorder(storyboard_id: str, revision: int, payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.reorder(
                       x_tenant_id, storyboard_id, revision, body,
                       command_id=command_id, request_hash=request_hash))


@app.post("/shots/{shot_id}:split")
def split_shot(shot_id: str, payload: dict,
               branch_id: str | None = Query(default=None),
               command_id: str = Header(default="", alias="Idempotency-Key"),
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    branch = branch_id or body.get("branch_id")
    home_cell = body.get("home_cell") or "cell_a1"
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.split_shot(
                       x_tenant_id, shot_id, body, branch_id=branch,
                       command_id=command_id, request_hash=request_hash,
                       home_cell=home_cell))


# ------------------------------------------------------------------ 查询

@app.get("/storyboards/{storyboard_id}")
def get_storyboard(storyboard_id: str,
                   x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id")):
    return service.get_storyboard(_tenant(x_tenant_id), storyboard_id)


@app.get("/storyboards/{storyboard_id}/revisions/{revision}")
def get_storyboard_revision(storyboard_id: str, revision: int,
                            x_tenant_id: str = Header(default="",
                                                      alias="X-Tenant-Id")):
    return service.get_storyboard_revision(_tenant(x_tenant_id),
                                           storyboard_id, revision)


@app.get("/shots/{shot_id}")
def get_shot(shot_id: str,
             branch_id: str | None = Query(default=None),
             x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_shot(_tenant(x_tenant_id), shot_id, branch_id=branch_id)


@app.get("/shots/{shot_id}/revisions/{revision}")
def get_shot_revision(shot_id: str, revision: int,
                      branch_id: str | None = Query(default=None),
                      x_tenant_id: str = Header(default="",
                                                alias="X-Tenant-Id")):
    return service.get_shot_revision(_tenant(x_tenant_id), shot_id, revision,
                                     branch_id=branch_id)


@app.get("/scenes/{scene_id}/shots")
def list_scene_shots(scene_id: str,
                     branch_id: str | None = Query(default=None),
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    return service.list_scene_shots(_tenant(x_tenant_id), scene_id,
                                    branch_id=branch_id)


@app.get("/shots/{shot_id}/lineage")
def get_lineage(shot_id: str,
                branch_id: str | None = Query(default=None),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_lineage(_tenant(x_tenant_id), shot_id,
                               branch_id=branch_id)


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
