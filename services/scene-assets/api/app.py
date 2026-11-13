"""S13 HTTP 层。

契约面（`contracts/openapi.v2.yaml` / `services/scene-assets/service.yaml`）
只声明了两个 revision 端点，而方案 §5 有一整套候选/接受/生命周期命令与五个
读路径。**这是契约与文档的冲突，不是我可以裁决的事**，已登记为 C13-1；
本层按方案实现，不改契约、不改 service.yaml。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

`:lookup` 注册在 `/{scene_id}` 之前不是必需的（路径里没有那个斜杠），但顺序
仍然照方案 §4.3 写，避免以后有人把 lookup 改成子资源时踩到匹配顺序。
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

from scene_assets.deps import build_artifact_source, build_authorizer
from scene_assets.schema import CONTRACT_GAPS
from scene_assets.service import SceneService, ServiceError

DB_PATH = os.environ.get("S13_DB", ":memory:")

app = FastAPI(title="S13 scene-assets", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = SceneService(
    _conn,
    # 两个都是注入式 callback。S28 未配置时 draft 路径整体 503（F16），
    # 不是跳过校验；S01 今天不在主路径上（S13-5）。
    artifact_source=build_artifact_source(os.environ.get("S28_BASE_URL")),
    authorizer=build_authorizer(os.environ.get("S01_BASE_URL")),
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
    # 同键不同 body：§6.1 要求 409，不是覆盖。
    return _err(409, exc.code, str(exc), False)


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(
        status_code=409, headers={"Retry-After": "1"},
        content={"code": "COMMAND_IN_FLIGHT", "message": str(exc),
                 "retryable": True, "correlation_id": "", "details": {}})


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
    candidate = payload.get("tenant_id")
    if candidate is not None and candidate != tenant_id:
        raise ServiceError(403, "TENANT_SCOPE_DENIED",
                           "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放路径**不重跑执行体**：接受一次 revision 会推 `current_revision` 并发
    事件，重放等于再接受一遍，第二次会撞 `VERSION_CONFLICT`，而调用方手里
    拿的仍然是第一次的结果——这正好是 E12 要证明的反面。
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
    _idem.finish(tenant_id, command_id, json.dumps(result, ensure_ascii=False))
    return {**result, "replayed": False}


def _tenant(x_tenant_id: str) -> str:
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return x_tenant_id


# ------------------------------------------------------------------ 命令

@app.post("/scene-assets")
def create_scene(payload: dict,
                 command_id: str = Header(alias="Idempotency-Key"),
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.create_scene(
                       x_tenant_id, command_id, body,
                       request_hash=request_hash))


@app.post("/scene-assets/{scene_id}/candidates")
def draft_candidate(scene_id: str, payload: dict,
                    command_id: str = Header(alias="Idempotency-Key"),
                    x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.draft_candidate(
                       x_tenant_id, command_id, scene_id, body,
                       request_hash=request_hash))


@app.post("/scene-assets/{scene_id}/candidates/{candidate_id}:accept")
def accept_candidate(scene_id: str, candidate_id: str, payload: dict,
                     command_id: str = Header(alias="Idempotency-Key"),
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.accept_candidate(
                       x_tenant_id, command_id, scene_id, candidate_id, body,
                       request_hash=request_hash))


@app.post("/scene-assets/{scene_id}/candidates/{candidate_id}:reject")
def reject_candidate(scene_id: str, candidate_id: str, payload: dict,
                     command_id: str = Header(alias="Idempotency-Key"),
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.reject_candidate(
                       x_tenant_id, command_id, scene_id, candidate_id, body,
                       request_hash=request_hash))


@app.post("/scene-assets/{scene_id}:set-canonical")
def set_canonical(scene_id: str, payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.set_canonical(
                       x_tenant_id, command_id, scene_id, body,
                       request_hash=request_hash))


@app.post("/scene-assets/{scene_id}:deprecate")
def deprecate(scene_id: str, payload: dict | None = None,
              command_id: str = Header(alias="Idempotency-Key"),
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.deprecate(
                       x_tenant_id, command_id, scene_id, body,
                       request_hash=request_hash))


@app.post("/scene-assets/{scene_id}:tombstone")
def tombstone(scene_id: str, payload: dict | None = None,
              command_id: str = Header(alias="Idempotency-Key"),
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.tombstone(
                       x_tenant_id, command_id, scene_id, body,
                       request_hash=request_hash))


@app.post("/scene-assets/{scene_id}:merge")
def merge(scene_id: str, payload: dict,
          command_id: str = Header(alias="Idempotency-Key"),
          x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.merge(
                       x_tenant_id, command_id, scene_id, body,
                       request_hash=request_hash))


# ------------------------------------------------------------------ 查询

@app.get("/scene-assets:lookup")
def lookup(x_tenant_id: str = Header(alias="X-Tenant-Id"),
           series_scope: str = Query(...), alias: str = Query(...)):
    return service.lookup(_tenant(x_tenant_id), series_scope, alias)


@app.get("/scene-assets/{scene_id}")
def get_scene(scene_id: str,
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_scene(_tenant(x_tenant_id), scene_id)


@app.get("/scene-assets/{scene_id}/canonical")
def get_canonical(scene_id: str,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_canonical(_tenant(x_tenant_id), scene_id)


@app.get("/scene-assets/{scene_id}/revisions/{revision}")
def get_revision(scene_id: str, revision: int,
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_revision(_tenant(x_tenant_id), scene_id, revision)


@app.get("/scene-assets/{scene_id}/revisions/{revision}/anchors")
def get_anchors(scene_id: str, revision: int,
                x_tenant_id: str = Header(alias="X-Tenant-Id"),
                direction: str | None = Query(default=None),
                lighting: str | None = Query(default=None)):
    return service.get_anchors(_tenant(x_tenant_id), scene_id, revision,
                               direction=direction, lighting=lighting)


@app.get("/scene-assets/{scene_id}/revisions/{revision}/topology")
def get_topology(scene_id: str, revision: int,
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_topology(_tenant(x_tenant_id), scene_id, revision)


@app.get("/scene-assets/{scene_id}/revisions/{revision}/fixed-objects")
def get_fixed_objects(scene_id: str, revision: int,
                      x_tenant_id: str = Header(alias="X-Tenant-Id"),
                      unit_id: str | None = Query(default=None)):
    return service.get_fixed_objects(_tenant(x_tenant_id), scene_id, revision,
                                     unit_id=unit_id)


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
