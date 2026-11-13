"""S10 HTTP 层。

契约面与方案的差异已经登记在 `schema.CONTRACT_GAPS`（C10-1 / C10-2）：
catalog 只登记了一个命令、`openapi.v2.yaml` 里没有 S10 的任何路径。本层按方案
§5 实现，不改契约。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

`GET /assets` 的检索参数与 `GET /binding-sets` 的 `series_id` 都是各自服务
自己加的范围约束（S10-22），不是契约里的东西 —— 一个无范围的资产列表在数据
长大后就是事故。
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

from asset_catalog.deps import (
    build_asset_status_source,
    build_manifest_source,
    build_revision_source,
)
from asset_catalog.schema import CONTRACT_GAPS
from asset_catalog.service import AssetCatalogService, ServiceError

DB_PATH = os.environ.get("S10_DB", ":memory:")

app = FastAPI(title="S10 asset-catalog", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

_OWNER_URLS = {
    "S11": os.environ.get("S11_BASE_URL"),
    "S12": os.environ.get("S12_BASE_URL"),
    "S13": os.environ.get("S13_BASE_URL"),
    "S14": os.environ.get("S14_BASE_URL"),
    "S15": os.environ.get("S15_BASE_URL"),
}

service = AssetCatalogService(
    _conn,
    revision_source=build_revision_source(_OWNER_URLS),
    asset_status_source=build_asset_status_source(_OWNER_URLS),
    manifest_source=build_manifest_source(os.environ.get("S08_BASE_URL")),
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
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "body must be a JSON object")
    if not command_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key is required")
    if not tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "X-Tenant-Id is required")
    inside = payload.get("command_id")
    if inside is not None and inside != command_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key does not match body command_id")
    if payload.get("tenant_id") is not None and payload["tenant_id"] != tenant_id:
        raise ServiceError(
            403, "TENANT_SCOPE_DENIED",
            "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。"""
    request_hash = canonical_request_hash(body)
    record = _idem.begin(tenant_id, command_id, request_hash)
    if record.result is not None:
        replay = json.loads(record.result)
        replay["replayed"] = True
        return replay
    try:
        result = execute(request_hash)
    except sqlite3.OperationalError as exc:
        # 锁等待超时 / 库不可写：这是可重试的基础设施问题，不是 500。
        # 不 abort 的话幂等行会留成 IN_PROGRESS，同一 command_id 之后永远
        # 拿到 409 COMMAND_IN_FLIGHT。
        _idem.abort(tenant_id, command_id)
        raise ServiceError(
            503, "WRITE_BACKPRESSURE",
            f"database is busy: {exc}", retryable=True) from exc
    except Exception:
        _idem.abort(tenant_id, command_id)
        raise
    _idem.finish(tenant_id, command_id,
                 json.dumps(result, ensure_ascii=False))
    return {**result, "replayed": False}


def _tenant(x_tenant_id: str) -> str:
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return x_tenant_id


# ------------------------------------------------------------------ 资产命令

@app.post("/assets", status_code=201)
def mint_asset(payload: dict,
               command_id: str = Header(alias="Idempotency-Key"),
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.mint_asset(
                       x_tenant_id, command_id, body, request_hash))


@app.post("/assets/{asset_id}:deprecate")
def deprecate_asset(asset_id: str, payload: dict | None = None,
                    command_id: str = Header(alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.deprecate_asset(
                       x_tenant_id, command_id, asset_id, body, request_hash))


@app.post("/assets/{asset_id}:tombstone")
def tombstone_asset(asset_id: str, payload: dict | None = None,
                    command_id: str = Header(alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.tombstone_asset(
                       x_tenant_id, command_id, asset_id, body, request_hash))


@app.post("/assets/{asset_id}:merge")
def merge_asset(asset_id: str, payload: dict | None = None,
                command_id: str = Header(alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.merge_asset(
                       x_tenant_id, command_id, asset_id, body, request_hash))


# ------------------------------------------------------------------ 资产查询

@app.get("/assets:lookup")
def lookup_asset(owner_service: str = Query(...),
                 owner_local_id: str = Query(...),
                 x_tenant_id: str = Header(default="",
                                           alias="X-Tenant-Id")):
    return service.lookup_asset(_tenant(x_tenant_id), owner_service,
                                owner_local_id)


@app.get("/assets")
def search_assets(alias_text: str | None = Query(default=None),
                  asset_type: str | None = Query(default=None),
                  series_scope: str | None = Query(default=None),
                  status: str | None = Query(default=None),
                  limit: int = Query(default=50),
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    return service.search_assets(_tenant(x_tenant_id), alias_text=alias_text,
                                 asset_type=asset_type,
                                 series_scope=series_scope, status=status,
                                 limit=limit)


@app.get("/assets/{asset_id}/resolve")
def resolve_asset(asset_id: str,
                  series_id: str | None = Query(default=None),
                  episode_id: str | None = Query(default=None),
                  branch_id: str | None = Query(default=None),
                  scene_id: str | None = Query(default=None),
                  shot_id: str | None = Query(default=None),
                  binding_set_id: str | None = Query(default=None),
                  binding_set_revision: int | None = Query(default=None),
                  variant_role: str | None = Query(default=None),
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    return service.resolve(
        _tenant(x_tenant_id), asset_id, series_id=series_id,
        episode_id=episode_id, branch_id=branch_id, scene_id=scene_id,
        shot_id=shot_id, binding_set_id=binding_set_id,
        binding_set_revision=binding_set_revision, variant_role=variant_role)


@app.get("/assets/{asset_id}")
def get_asset(asset_id: str,
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_asset(_tenant(x_tenant_id), asset_id)


# -------------------------------------------------------------- BindingSet

@app.post("/binding-sets", status_code=201)
def create_binding_set(payload: dict,
                       command_id: str = Header(alias="Idempotency-Key"),
                       x_tenant_id: str = Header(default="",
                                                 alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.create_binding_set(
                       x_tenant_id, command_id, body, request_hash))


@app.get("/binding-sets")
def list_binding_sets(series_id: str | None = Query(default=None),
                      episode_id: str | None = Query(default=None),
                      branch_id: str | None = Query(default=None),
                      limit: int = Query(default=50),
                      x_tenant_id: str = Header(default="",
                                                alias="X-Tenant-Id")):
    return service.list_binding_sets(_tenant(x_tenant_id),
                                     series_id=series_id,
                                     episode_id=episode_id,
                                     branch_id=branch_id, limit=limit)


@app.post("/binding-sets/{binding_set_id}/entries", status_code=201)
def write_entry(binding_set_id: str, payload: dict,
                command_id: str = Header(alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.write_entry(
                       x_tenant_id, command_id, binding_set_id, body,
                       request_hash))


@app.delete("/binding-sets/{binding_set_id}/entries/{scope_kind}/{scope_id}/"
            "{role_key}")
def delete_entry(binding_set_id: str, scope_kind: str, scope_id: str,
                 role_key: str, payload: dict | None = None,
                 command_id: str = Header(alias="Idempotency-Key"),
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.delete_entry(
                       x_tenant_id, command_id, binding_set_id, scope_kind,
                       scope_id, role_key, body, request_hash))


@app.post("/binding-sets/{binding_set_id}/defaults")
def set_default(binding_set_id: str, payload: dict,
                command_id: str = Header(alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.set_default(
                       x_tenant_id, command_id, binding_set_id, body,
                       request_hash))


@app.post("/binding-sets/{binding_set_id}/revisions", status_code=201)
def seal(binding_set_id: str, payload: dict,
         command_id: str = Header(alias="Idempotency-Key"),
         x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.seal(
                       x_tenant_id, command_id, binding_set_id, body,
                       request_hash))


@app.post("/binding-sets/{binding_set_id}:archive")
def archive(binding_set_id: str, payload: dict | None = None,
            command_id: str = Header(alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.archive(
                       x_tenant_id, command_id, binding_set_id, body,
                       request_hash))


@app.get("/binding-sets/{binding_set_id}")
def get_binding_set(binding_set_id: str,
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id")):
    return service.get_binding_set(_tenant(x_tenant_id), binding_set_id)


@app.get("/binding-sets/{binding_set_id}/revisions/{revision}")
def get_revision(binding_set_id: str, revision: int,
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_revision(_tenant(x_tenant_id), binding_set_id, revision)


@app.get("/binding-sets/{binding_set_id}/revisions/{revision}/usage")
def revision_usage(binding_set_id: str, revision: int,
                   x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id")):
    return service.usage(_tenant(x_tenant_id), binding_set_id, revision)


# ------------------------------------------------------------------ 事件入口

@app.post("/internal/events/snapshot-manifest-committed")
def ingest_manifest_committed(envelope: dict):
    """S08 → usage 投影。**消费不走幂等头**：至少一次投递由 inbox 去重。

    `tenant_id` 来自签名信封而不是请求头 —— 这是事件消费，不是命令。
    """
    return service.ingest_manifest_committed(envelope)


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
