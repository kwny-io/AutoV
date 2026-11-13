"""S03 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）**一条 S03 路径都没有**：18 条路径里唯一
和项目沾边的是 S02 的 `/projects/{project_id}/effective-policy`。catalog 也只
声明了 `POST /projects/{id}/episodes` 与 `GET /projects/{id}/episodes`。所以
下面六条命令、七条查询里的绝大多数都是契约缺口（C03-1），不是实现走样。

租户一律来自 `X-Tenant-Id`（平台签名上下文）。请求体里的 `tenant_id` 只用于
比对，不一致即 403 —— AGENTS.md 禁止把请求体里的 `tenant_id` 当可信输入。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from project_catalog.deps import (
    build_authorizer,
    build_clock_validator,
    build_run_probe,
)
from project_catalog.schema import CONTRACT_GAPS
from project_catalog.service import ProjectCatalogService, ServiceError

DB_PATH = os.environ.get("S03_DB", ":memory:")

app = FastAPI(title="S03 project-catalog", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = ProjectCatalogService(
    _conn,
    authorizer=build_authorizer(os.environ.get("S01_BASE_URL")),
    run_probe=build_run_probe(os.environ.get("S21_BASE_URL")),
    clock_validator=build_clock_validator(os.environ.get("S16_BASE_URL")),
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
    return _err(409, exc.code, str(exc), False)


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    # 平台 SDK 叫 COMMAND_IN_PROGRESS，方案 §5.4 叫 COMMAND_IN_FLIGHT（缺口 G6）。
    # 对外用方案的名字：契约面是这里决定的，不是共享 SDK 的继承物。
    return JSONResponse(
        status_code=409, headers={"Retry-After": "1"},
        content={"code": "COMMAND_IN_FLIGHT", "message": str(exc),
                 "retryable": True, "correlation_id": "", "details": {}})


def _principal(payload: dict) -> dict:
    principal = payload.get("principal")
    if not isinstance(principal, dict) or not principal.get("principal_id"):
        raise ServiceError(400, "INVALID_BODY",
                           "principal.principal_id is required")
    return principal


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
    return {**payload, "command_id": command_id, "tenant_id": tenant_id}


def command(tenant_id: str, body: dict, execute):
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    内核在同一个事务里已经写了一次 `result`；这里的 `finish` 是同值覆盖，
    存在只是为了走完 SDK 的状态机（`result IS NULL` = 进行中）。
    """
    request_hash = canonical_request_hash(body)
    record = _idem.begin(tenant_id, body["command_id"], request_hash)
    if record.result is not None:
        replay = json.loads(record.result)
        replay["replayed"] = True
        return replay
    try:
        result = execute()
    except Exception:
        # 内核事务已回滚，IN_PROGRESS 行留着会让同 command_id 永远 409。
        _idem.abort(tenant_id, body["command_id"])
        raise
    _idem.finish(tenant_id, body["command_id"],
                 json.dumps(result, ensure_ascii=False))
    return {**result, "replayed": False}


# ------------------------------------------------------------------ 命令

@app.post("/projects", status_code=201)
def create_project(payload: dict,
                   command_id: str = Header(alias="Idempotency-Key"),
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    principal = _principal(body)
    return command(x_tenant_id, body,
                   lambda: service.create_project(x_tenant_id, body,
                                                  principal=principal))


@app.post("/projects/{project_id}/episodes", status_code=201)
def append_episodes(project_id: str, payload: dict,
                    command_id: str = Header(alias="Idempotency-Key"),
                    x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    principal = _principal(body)
    return command(x_tenant_id, body,
                   lambda: service.append_episodes(x_tenant_id, project_id,
                                                   body, principal=principal))


@app.post("/series/{series_id}:reorder-episodes")
def reorder_episodes(series_id: str, payload: dict,
                     command_id: str = Header(alias="Idempotency-Key"),
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    principal = _principal(body)
    return command(x_tenant_id, body,
                   lambda: service.reorder_episodes(x_tenant_id, series_id,
                                                    body, principal=principal))


@app.post("/series/{series_id}/spec-revisions", status_code=201)
def publish_spec_revision(series_id: str, payload: dict,
                          command_id: str = Header(alias="Idempotency-Key"),
                          x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    principal = _principal(body)
    return command(x_tenant_id, body,
                   lambda: service.publish_spec_revision(
                       x_tenant_id, series_id, body, principal=principal))


@app.post("/projects/{project_id}:archive")
def archive_project(project_id: str, payload: dict,
                    command_id: str = Header(alias="Idempotency-Key"),
                    x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    principal = _principal(body)
    return command(x_tenant_id, body,
                   lambda: service.archive_project(x_tenant_id, project_id,
                                                   body, principal=principal))


@app.post("/projects/{project_id}/members", status_code=201)
def add_member(project_id: str, payload: dict,
               command_id: str = Header(alias="Idempotency-Key"),
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    principal = _principal(body)
    return command(x_tenant_id, body,
                   lambda: service.add_member(x_tenant_id, project_id, body,
                                              principal=principal))


# ------------------------------------------------------------------ 查询

@app.get("/projects")
def list_projects(org_unit_id: str | None = Query(None),
                  lifecycle: str | None = Query(None),
                  limit: int = Query(50),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.list_projects(x_tenant_id, org_unit_id=org_unit_id,
                                 lifecycle=lifecycle, limit=limit)


@app.get("/projects/{project_id}")
def get_project(project_id: str,
                x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.get_project(x_tenant_id, project_id)


@app.get("/projects/{project_id}/episodes")
def list_episodes(project_id: str,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.list_episodes(x_tenant_id, project_id)


@app.get("/episodes/{episode_id}")
def get_episode(episode_id: str,
                x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§4.4：全链路最热。不返回 scene_ids。"""
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.get_episode_context(x_tenant_id, episode_id)


@app.get("/episodes/{episode_id}/scenes")
def get_episode_scenes(episode_id: str,
                       x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """代理 S05，响应里带 `authoritative_source`。"""
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.get_episode_scenes(x_tenant_id, episode_id)


@app.get("/series/{series_id}/orders")
def get_series_orders(series_id: str,
                      order_kind: str = Query("release"),
                      window_key: str = Query(""),
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.get_series_orders(x_tenant_id, series_id,
                                     order_kind=order_kind,
                                     window_key=window_key)


@app.get("/series/{series_id}/spec-revisions/{revision}")
def get_spec_revision(series_id: str, revision: int,
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.get_spec_revision(x_tenant_id, series_id, revision)


@app.get("/episodes/{episode_id}/acceptance-projection")
def acceptance_projection_status(episode_id: str,
                                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """只读：让"这一集为什么是 completed"可解释。

    写入路径是 S08 的投影回调，不是这个端点 —— 这里不给外部触发投影的能力。
    """
    context = service.get_episode_context(x_tenant_id, episode_id)
    return {
        "episode_id": episode_id,
        "lifecycle": context["lifecycle"],
        "note": "episode.lifecycle='completed' is a projection of an S08 "
                "accepted master node and is reversible; only 'archived' is "
                "terminal in S03",
        "x-contract-gaps": CONTRACT_GAPS,
    }


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}
