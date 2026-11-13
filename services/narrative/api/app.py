"""S04 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）里没有 S04 的任何路径
（C04-4）。本层按方案 §5.1/§5.2 实现，不改契约。

三处与方案原文有出入，都是**契约缺口而不是实现自由度**：

1. 方案 §5.2 没有 `GET /scripts/{id}`，但前端/调用方需要拿到 `head_revision`
   才能发 CAS；不提供它，调用方只能靠猜或每写一次就 409 一次。补上且随身
   带出 `x-contract-gaps`。
2. `:extract-claims` 的请求体在方案里没有定义（S04-G6），本层只转发
   `task_request`，不构造 S19 需要的 `context/role/limits`。
3. `POST /briefs` 是补的（S04-G7），否则 `brief_revision` 的外键没有可指
   的对象。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。
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

from narrative.deps import (
    build_body_store,
    build_catalog_source,
    build_task_source,
)
from narrative.schema import CONTRACT_GAPS
from narrative.service import NarrativeService, ServiceError

DB_PATH = os.environ.get("S04_DB", ":memory:")

app = FastAPI(title="S04 narrative", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

# 正文存储未配置时是 503，不是"没有正文也能写"（S04-G1）。这里把构造异常
# 收成 None，让服务起来、让每个写命令如实报 BODY_STORE_UNAVAILABLE。
try:
    _body_store = build_body_store(os.environ.get("S04_BODY_STORE"))
except ServiceError:
    _body_store = None

service = NarrativeService(
    _conn,
    task_source=build_task_source(os.environ.get("S19_BASE_URL")),
    body_store=_body_store,
    catalog_source=build_catalog_source(os.environ.get("S03_BASE_URL")),
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
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    `request_hash` 对去掉 command_id 的完整 body 求（S04-G11：方案希望正文
    先 hash 再进；不改共享 SDK，语义不受影响）。
    """
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
        # §5.4 `503 NARRATIVE_UNAVAILABLE`。内核的 `Transaction` 只映射它自己
        # 的 `BEGIN IMMEDIATE`；幂等占位与落结果发生在事务之外，不在这里
        # 映射就是未捕获异常 → 500 —— 与"写不进去不等于请求有错"相悖。
        raise ServiceError(
            503, "NARRATIVE_UNAVAILABLE",
            f"narrative database is not writable: {exc}",
            retryable=True) from exc


def _tenant(tenant_id: str) -> str:
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return tenant_id


def _principal(body: dict, principal_id: str, principal_type: str) -> dict:
    """`author` 的来源（S04-G13）。

    方案 §4.1 第 2 步要求"从签名上下文取 tenant/principal"，但本仓库的 HTTP
    层还没有 TCT 验签中间件。这里的顺序是：`X-Principal-Id` / `X-Principal-Type`
    优先，缺失时回落到请求体 `author`；两者都在且不一致 → 403。

    **这是过渡态，不是安全边界。** 没有中间件时，"我是 human"只能由调用方
    如实上报，`seal` 的 `PERMISSION_DENIED` 因此挡不住一个把
    `principal_type` 写成 `human` 的 agent。已登记 S04-G13。
    """
    inside = body.get("author")
    if principal_id:
        derived = {"principal_id": principal_id,
                   "principal_type": principal_type or "human"}
        if isinstance(inside, dict) and inside.get("principal_id") not in (
                None, principal_id):
            raise ServiceError(
                403, "PERMISSION_DENIED",
                "body author disagrees with the signed principal",
                details={"gap": "S04-G13"})
        body = {**body, "author": derived}
    return body


# ------------------------------------------------------------------ 命令

@app.post("/scripts", status_code=201)
def create_script(payload: dict,
                  command_id: str = Header(default="", alias="Idempotency-Key"),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                  x_principal: str = Header(default="", alias="X-Principal-Id"),
                  x_principal_type: str = Header(default="",
                                                 alias="X-Principal-Type")):
    body = _principal(_body(payload, command_id, x_tenant_id),
                      x_principal, x_principal_type)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.create_script(
                       x_tenant_id, body, command_id=command_id,
                       request_hash=request_hash))


@app.post("/scripts/{script_id}/revisions", status_code=201)
def append_revision(script_id: str, payload: dict,
                    command_id: str = Header(default="", alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                    x_principal: str = Header(default="", alias="X-Principal-Id"),
                    x_principal_type: str = Header(default="",
                                                   alias="X-Principal-Type")):
    body = _principal(_body(payload, command_id, x_tenant_id),
                      x_principal, x_principal_type)
    home_cell = body.get("home_cell") or "cell_a1"
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.append_revision(
                       x_tenant_id, script_id, body, command_id=command_id,
                       request_hash=request_hash, home_cell=home_cell))


@app.post("/scripts/{script_id}/revisions/{revision}:seal")
def seal(script_id: str, revision: int, payload: dict,
         command_id: str = Header(default="", alias="Idempotency-Key"),
         x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
         x_principal: str = Header(default="", alias="X-Principal-Id"),
         x_principal_type: str = Header(default="", alias="X-Principal-Type")):
    body = _principal(_body(payload, command_id, x_tenant_id),
                      x_principal, x_principal_type)
    home_cell = body.get("home_cell") or "cell_a1"
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.seal(
                       x_tenant_id, script_id, revision, body,
                       command_id=command_id, request_hash=request_hash,
                       home_cell=home_cell))


@app.post("/scripts/{script_id}/revisions/{revision}:withdraw")
def withdraw(script_id: str, revision: int, payload: dict,
             command_id: str = Header(default="", alias="Idempotency-Key"),
             x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
             x_principal: str = Header(default="", alias="X-Principal-Id"),
             x_principal_type: str = Header(default="",
                                            alias="X-Principal-Type")):
    body = _principal(_body(payload, command_id, x_tenant_id),
                      x_principal, x_principal_type)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.withdraw(
                       x_tenant_id, script_id, revision, body,
                       command_id=command_id, request_hash=request_hash))


@app.post("/lines/{line_id}/revisions", status_code=201)
def append_line_revision(line_id: str, payload: dict,
                         command_id: str = Header(default="", alias="Idempotency-Key"),
                         x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                         x_principal: str = Header(default="",
                                                   alias="X-Principal-Id"),
                         x_principal_type: str = Header(default="",
                                                        alias="X-Principal-Type")):
    body = _principal(_body(payload, command_id, x_tenant_id),
                      x_principal, x_principal_type)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.append_line_revision(
                       x_tenant_id, line_id, body, command_id=command_id,
                       request_hash=request_hash))


@app.post("/lines/{line_id}:retire")
def retire_line(line_id: str, payload: dict,
                command_id: str = Header(default="", alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.retire_line(
                       x_tenant_id, line_id, body, command_id=command_id,
                       request_hash=request_hash))


@app.post("/briefs", status_code=201)
def create_brief(payload: dict,
                 command_id: str = Header(default="", alias="Idempotency-Key"),
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.create_brief(
                       x_tenant_id, body, command_id=command_id,
                       request_hash=request_hash))


@app.post("/briefs/{brief_id}/revisions", status_code=201)
def append_brief_revision(brief_id: str, payload: dict,
                          command_id: str = Header(default="", alias="Idempotency-Key"),
                          x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                          x_principal: str = Header(default="",
                                                    alias="X-Principal-Id"),
                          x_principal_type: str = Header(default="",
                                                         alias="X-Principal-Type")):
    body = _principal(_body(payload, command_id, x_tenant_id),
                      x_principal, x_principal_type)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.append_brief_revision(
                       x_tenant_id, brief_id, body, command_id=command_id,
                       request_hash=request_hash))


@app.post("/story-arcs", status_code=201)
def submit_story_arc(payload: dict,
                     command_id: str = Header(default="", alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.submit_story_arc(
                       x_tenant_id, body, command_id=command_id,
                       request_hash=request_hash))


@app.post("/story-arcs/{proposal_id}/revisions/{revision}:decide")
def decide_story_arc(proposal_id: str, revision: int, payload: dict,
                     command_id: str = Header(default="", alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                     x_principal: str = Header(default="", alias="X-Principal-Id"),
                     x_principal_type: str = Header(default="",
                                                    alias="X-Principal-Type")):
    body = _principal(_body(payload, command_id, x_tenant_id),
                      x_principal, x_principal_type)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.decide_story_arc(
                       x_tenant_id, proposal_id, revision, body,
                       command_id=command_id, request_hash=request_hash))


@app.post("/scripts/{script_id}/revisions/{revision}:extract-claims",
          status_code=201)
def extract_claims(script_id: str, revision: int, payload: dict,
                   command_id: str = Header(default="", alias="Idempotency-Key"),
                   x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.extract_claims(
                       x_tenant_id, script_id, revision, body,
                       command_id=command_id, request_hash=request_hash))


# ------------------------------------------------------------------ 事件

@app.post("/events:consume")
def consume_event(payload: dict,
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """`snapshot.manifest.committed` 的投递入口。inbox 自己就是幂等。

    **这不是"接受判定"**：它只维护撤回守卫用的引用投影（§4.4 最后一段）。
    """
    envelope = payload.get("event", payload)
    if not isinstance(envelope, dict):
        raise ServiceError(400, "INVALID_BODY", "event must be an object")
    if envelope.get("tenant_id") is None:
        envelope = {**envelope, "tenant_id": _tenant(x_tenant_id)}
    elif envelope["tenant_id"] != x_tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "event tenant disagrees with the signed context")
    return service.consume_manifest_event("s04.manifest.committed", envelope)


# ------------------------------------------------------------------ 查询

@app.get("/scripts/{script_id}")
def get_script(script_id: str,
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_script(_tenant(x_tenant_id), script_id)


@app.get("/scripts/{script_id}/revisions/{revision}")
def get_revision(script_id: str, revision: int,
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_revision(_tenant(x_tenant_id), script_id, revision)


@app.get("/scripts/{script_id}/revisions/{revision}/body")
def get_body(script_id: str, revision: int,
             chunk: int | None = Query(default=None),
             x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_body(_tenant(x_tenant_id), script_id, revision,
                            chunk=chunk)


@app.get("/scripts/{script_id}/revisions/{revision}/lines")
def list_revision_lines(script_id: str, revision: int,
                        after_line_id: str | None = Query(default=None),
                        limit: int = Query(default=50),
                        x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.list_revision_lines(
        _tenant(x_tenant_id), script_id, revision,
        after_line_id=after_line_id, limit=limit)


@app.get("/scripts")
def list_scripts(x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                 series_id: str | None = Query(default=None),
                 episode_id: str | None = Query(default=None),
                 state: str | None = Query(default=None),
                 limit: int = Query(default=50)):
    return service.list_scripts(_tenant(x_tenant_id), series_id=series_id,
                                episode_id=episode_id, state=state,
                                limit=limit)


@app.get("/lines:batch")
def batch_lines(ids: str = Query(...),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """`ids` 是逗号分隔。超过 500 个拒绝，不截断。

    用 query 字符串而不是数组参数：`/lines:batch?ids=a,b` 是方案写的形状，
    数组参数会变成 `ids=a&ids=b`，与方案不一致。
    """
    wanted = [item for item in ids.split(",") if item]
    return service.batch_lines(_tenant(x_tenant_id), wanted)


@app.get("/lines/{line_id}")
def get_line(line_id: str,
             x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_line(_tenant(x_tenant_id), line_id)


@app.get("/lines/{line_id}/revisions/{revision}")
def get_line_revision(line_id: str, revision: int,
                      x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_line_revision(_tenant(x_tenant_id), line_id, revision)


@app.get("/story-arcs/{proposal_id}")
def get_story_arc(proposal_id: str,
                  revision: int | None = Query(default=None),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_story_arc(_tenant(x_tenant_id), proposal_id,
                                 revision=revision)


@app.get("/briefs/{brief_id}")
def get_brief(brief_id: str,
              revision: int | None = Query(default=None),
              x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_brief(_tenant(x_tenant_id), brief_id, revision=revision)


@app.get("/extractions/{extraction_id}")
def get_extraction(extraction_id: str,
                   x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_extraction(_tenant(x_tenant_id), extraction_id)


@app.get("/reference-health")
def reference_health(x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """撤回守卫的依据是否新鲜。运维要能看清"为什么撤不掉"。"""
    return service.reference_health(_tenant(x_tenant_id))


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
