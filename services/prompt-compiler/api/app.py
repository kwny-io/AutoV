"""S20 HTTP 层。

契约面与方案的差异登记在 `schema.CONTRACT_GAPS`；本层按方案 §2 实现，不改
契约。`openapi.v2.yaml` 没有 S20 的路径，所以路由名取自方案表格。

租户一律来自签名的 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

两处与 S10 不同、都是方案点名的：

- 在途重复用 `COMMAND_IN_PROGRESS` + `Retry-After: 2`（F22）。编译比建资产慢，
  1 秒的重试间隔会让调用方在真正完成前打出三次无谓请求。
- `POST /prompt-compilations` 是**只读上游、只写自己**的命令；它没有下游副作用，
  但仍然是命令 —— 幂等键与第二层去重都要走。
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

from prompt_compiler.deps import (
    build_alignment_source,
    build_binding_source,
    build_capability_source,
    build_regression_source,
    build_shot_source,
)
from prompt_compiler.schema import CONTRACT_GAPS
from prompt_compiler.service import PromptCompilerService, ServiceError

DB_PATH = os.environ.get("S20_DB", ":memory:")

app = FastAPI(title="S20 prompt-compiler", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = PromptCompilerService(
    _conn,
    shot_source=build_shot_source(os.environ.get("S06_BASE_URL")),
    binding_source=build_binding_source(os.environ.get("S10_BASE_URL")),
    capability_source=build_capability_source(os.environ.get("S24_BASE_URL")),
    alignment_source=build_alignment_source(os.environ.get("S37_BASE_URL")),
    regression_source=build_regression_source(os.environ.get("S20_BASE_URL")),
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
    headers = {}
    if exc.code in ("CAPABILITY_SOURCE_UNAVAILABLE",
                    "BINDING_SOURCE_UNAVAILABLE",
                    "SHOT_SOURCE_UNAVAILABLE",
                    "ALIGNMENT_SOURCE_UNAVAILABLE",
                    "KMS_UNAVAILABLE"):
        # F19/F20 要求 503 带 Retry-After，否则调用方只能靠猜。
        headers["Retry-After"] = "2"
    return JSONResponse(
        status_code=exc.status,
        headers=headers or None,
        content={"code": exc.code, "message": str(exc),
                 "retryable": exc.retryable, "correlation_id": "",
                 "details": exc.details or {}})


@app.exception_handler(CommandConflict)
async def _conflict(_request, exc: CommandConflict):
    # 同键不同 body：409，不是覆盖（AGENTS.md）。
    return _err(409, exc.code, str(exc), False)


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(
        status_code=409, headers={"Retry-After": "2"},
        content={"code": "COMMAND_IN_PROGRESS", "message": str(exc),
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
    if payload.get("tenant_id") is not None and \
            payload["tenant_id"] != tenant_id:
        # T20-49：模型/调用方自报的租户与签名上下文不符，一律拒绝。
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
        # 库忙 / 锁超时是基础设施问题，不是 500。不 abort 会留下 IN_PROGRESS
        # 的幂等行，同一 command_id 之后永远 409 COMMAND_IN_PROGRESS。
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


# ------------------------------------------------------------------ 模板

@app.post("/templates", status_code=201)
def create_template(payload: dict,
                    command_id: str = Header(default="", alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.create_template(
                       x_tenant_id, command_id, body, request_hash))


@app.get("/templates/{template_id}/revisions")
def list_revisions(template_id: str,
                   x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id")):
    return service.list_template_revisions(_tenant(x_tenant_id), template_id)


@app.get("/templates/{template_id}/revisions/{revision}")
def get_revision(template_id: str, revision: int,
                 x_tenant_id: str = Header(default="",
                                           alias="X-Tenant-Id")):
    """S19 读 `payload["template"]`；顶层同时平铺同样的字段。"""
    return service.get_template_revision(
        _tenant(x_tenant_id), template_id, revision)


@app.post("/templates/{template_id}/revisions", status_code=201)
def publish_revision(template_id: str, payload: dict,
                     command_id: str = Header(default="", alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.publish_revision(
                       x_tenant_id, command_id, template_id, body,
                       request_hash))


@app.post("/templates/{template_id}/revisions/{revision}:retire")
def retire_revision(template_id: str, revision: int,
                    payload: dict | None = None,
                    command_id: str = Header(default="", alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.retire_revision(
                       x_tenant_id, command_id, template_id, revision, body,
                       request_hash))


@app.get("/templates/{template_id}/revisions/{revision}/regression-report")
def regression_report(template_id: str, revision: int,
                      x_tenant_id: str = Header(default="",
                                                alias="X-Tenant-Id")):
    return service.get_regression_report(
        _tenant(x_tenant_id), template_id, revision)


# ------------------------------------------------------------------ adapter

@app.put("/adapters/{adapter_id}/revisions/{revision}")
def put_adapter(adapter_id: str, revision: int, payload: dict,
                command_id: str = Header(default="", alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """路径参数是权威：body 里的同名字段只用来比对。

    路径与 body 不一致时不静默采用其一 —— 那会让调用方以为写的是 A、
    实际写的是 B。
    """
    body = _body(payload, command_id, x_tenant_id)
    if body.get("adapter_id") not in (None, adapter_id):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "path adapter_id disagrees with the body")
    if body.get("revision") not in (None, revision):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "path revision disagrees with the body")
    body = {**body, "adapter_id": adapter_id, "revision": revision}
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.put_adapter(
                       x_tenant_id, command_id, body, request_hash))


@app.get("/adapters/{adapter_id}/revisions/{revision}")
def get_adapter(adapter_id: str, revision: int,
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_adapter(_tenant(x_tenant_id), adapter_id, revision)


# ------------------------------------------------------------------ 编译

@app.post("/prompt-compilations", status_code=201)
def compile_prompt(payload: dict,
                   command_id: str = Header(default="", alias="Idempotency-Key"),
                   x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.compile(
                       x_tenant_id, command_id, body, request_hash))


@app.get("/prompt-compilations/{manifest_id}")
def get_manifest(manifest_id: str,
                 x_tenant_id: str = Header(default="",
                                           alias="X-Tenant-Id")):
    return service.get_manifest(_tenant(x_tenant_id), manifest_id)


@app.post("/prompt-compilations:probe")
def probe_post(payload: dict,
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """**纯读**：不占幂等键、不写 manifest、不出事件。"""
    return service.probe(_tenant(x_tenant_id), payload or {})


@app.get("/prompt-compilations:probe")
def probe_get(template_id: str = Query(...),
              revision: int = Query(...),
              model_snapshot: list[str] = Query(...),
              requirement_set_id: str | None = Query(default=None),
              requirement_set_revision: int | None = Query(default=None),
              requirement_set_content_hash: str | None = Query(default=None),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """GET 形态。方案 §2.6 写 `GET|POST`；GET 用重复的 `model_snapshot`
    参数表达数组（`?model_snapshot=a&model_snapshot=b`）。"""
    body: dict = {"template_ref": {"id": template_id, "revision": revision},
                  "model_snapshots": list(model_snapshot)}
    if requirement_set_id:
        body["requirement_set_ref"] = {"id": requirement_set_id}
        if requirement_set_revision is not None:
            body["requirement_set_ref"]["revision"] = requirement_set_revision
        if requirement_set_content_hash:
            body["requirement_set_ref"]["content_hash"] = \
                requirement_set_content_hash
    return service.probe(_tenant(x_tenant_id), body)


# ------------------------------------------------------------------ 健康

@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
