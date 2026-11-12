"""S40 HTTP 层。

契约面（`contracts/openapi.v2.yaml:621`）只覆盖**一个**入口：

    POST /review-tasks/{review_task_id}/decisions        operationId: decideReview
      body: {context: execution-context.v2, decision: enum[accept,reject,
             request_repair], reason, command_id}        additionalProperties: false
      202  → Operation

三处与方案不符，都不在本服务内选边（AGENTS.md：契约与文档冲突 → 报告，不自行选边）：

1. **`context` 是 `execution-context.v2`**（18 个必填字段、`additionalProperties:
   false`）。它含 `manifest_ref` / `node_id` / `node_epoch` / `execution_fence` /
   `policy_ref`，但**装不下** `lock_version`、`artifact.content_hash`、
   `pack_hash` —— 而 §6.3 的硬绑定里这七个字段一个都不能少。收契约信封就是放弃
   三道绑定检查（C40-2）。
2. **`decision` 只有三值**，docs/06 §3 要的 `replace` / `change_spec` /
   例外接受得走 `option_id`，而契约 `additionalProperties: false` 不许这个字段
   （C40-1）。
3. **响应是 `Operation`（异步 202）**，本服务是同步落库后返回决定本体。

处理方式与 S18 C18-1、S09 C09-4 同构：**两种形状都收，但契约信封形状只借它
真的有的字段，缺的绑定维度显式报出来**，不假装校验过。

- 契约信封 → `context` 里的 `manifest_ref`/`node_epoch`/`execution_fence`/
  `policy_ref` 参与比较；`lock_version`/`artifact.content_hash`/`pack_hash` 缺失，
  响应带 `binding_dimensions_unchecked`，并把 `C40-2` 放进 `x-contract-gaps`。
  这**不是**"通过了绑定校验"，是"只校验了契约允许的那几维"。
- 本服务扁平形状 → 七维全查，`pack_hash` 由建任务时算好、调用方原样带回。

`POST /review-tasks` 与两个 GET 不在契约里（G2），按方案 §2 提供。

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

from human_review.deps import (
    build_acceptance_cas,
    build_authz_source,
    build_node_diff,
    build_policy_validator,
)
from human_review.schema import CONTRACT_GAPS
from human_review.service import HumanReviewService, ServiceError

DB_PATH = os.environ.get("S40_DB", ":memory:")

# §6.3 的七维硬绑定。契约信封装不下后三个（C40-2）。
BINDING_DIMENSIONS = (
    "manifest_ref.id", "node_epoch", "execution_fence", "policy_ref.content_hash",
    "lock_version", "artifact.content_hash", "pack_hash",
)
ENVELOPE_DIMENSIONS = BINDING_DIMENSIONS[:4]

app = FastAPI(title="S40 human-review", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

_s08_base = os.environ.get("S08_BASE_URL")
service = HumanReviewService(
    _conn,
    authz_source=build_authz_source(os.environ.get("S01_BASE_URL")),
    policy_validator=build_policy_validator(os.environ.get("S02_BASE_URL")),
    acceptance_cas=build_acceptance_cas(_s08_base),
    node_diff=build_node_diff(_s08_base),
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
    return JSONResponse(status_code=409, headers={"Retry-After": "1"},
                        content={"code": exc.code, "message": str(exc),
                                 "retryable": True, "correlation_id": "",
                                 "details": {}})


def _norm_context(payload: dict) -> tuple[dict, list[str], str]:
    """把两种 body 形状统一成内核要的 ctx。返回 (ctx, 未校验维度, 形状)。

    契约信封里的 `context` 是完整 `execution-context.v2`；它自带 `tenant_id`，
    那个字段同样只能用于比对（见 `_body`）。
    """
    context = payload.get("context")
    if isinstance(context, dict):
        # 信封和本服务的扁平 ctx 是同一个 dict 形状，单凭"有 context"分不开。
        # 用 `schema_version` 认契约信封：那是 execution-context.v2 的必填字段，
        # 而扁平形状由 S40 自己定义、从不带它。
        if "schema_version" not in context:
            return dict(context), [], "flat"
        unchecked = [d for d in BINDING_DIMENSIONS if _missing(context, d)]
        # `pack_hash` 不可能出现在信封里：它由 S40 建任务时算，不是上游协议字段。
        return dict(context), unchecked, "contract_envelope"
    return dict(payload.get("context") or {}), [], "flat"


def _missing(mapping: dict, dotted: str) -> bool:
    value = mapping
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return True
        value = value[part]
    return value is None


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

    # 信封里的 context 自带 tenant_id。它和 body 顶层的 tenant_id 一样不可信，
    # 只用于比对 —— 平台签名的是 header。
    for candidate in (payload.get("tenant_id"),
                      (payload.get("context") or {}).get("tenant_id")
                      if isinstance(payload.get("context"), dict) else None):
        if candidate is not None and candidate != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "body tenant_id disagrees with the signed context")
    return {**payload, "command_id": command_id, "tenant_id": tenant_id}


def command(tenant_id: str, body: dict, execute):
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。"""
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


@app.post("/review-tasks", status_code=201)
def create_review_task(payload: dict,
                       command_id: str = Header(alias="Idempotency-Key"),
                       x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """方案 §2。不在契约内（G2）。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.create_task(x_tenant_id, body))


@app.post("/review-tasks/{review_task_id}/decisions", status_code=202)
def decide_review(review_task_id: str, payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """契约入口。两种 body 形状都收，见模块 docstring。"""
    body = _body(payload, command_id, x_tenant_id)
    ctx, unchecked, shape = _norm_context(payload)
    body["context"] = ctx
    result = command(
        x_tenant_id, body,
        lambda: service.submit_decision(x_tenant_id, review_task_id, body,
                                        command_id=command_id))
    if shape == "contract_envelope":
        # 契约允许的绑定维度已查；其余维度不是"通过"，是"契约不允许我们查"。
        result = {**result, "context_shape": shape,
                  "binding_dimensions_checked": [d for d in BINDING_DIMENSIONS
                                                 if d not in unchecked],
                  "binding_dimensions_unchecked": unchecked,
                  "x-contract-gaps": CONTRACT_GAPS}
    return result


@app.get("/review-tasks/{review_task_id}")
def get_review_task(review_task_id: str,
                    x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.get_task(x_tenant_id, review_task_id)


@app.get("/review-tasks")
def list_review_tasks(state: str | None = Query(None), limit: int = Query(50),
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.list_tasks(x_tenant_id, state=state, limit=limit)


@app.get("/decisions/{decision_id}")
def get_decision(decision_id: str,
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return service.get_decision(x_tenant_id, decision_id)


@app.get("/acceptance-retries")
def acceptance_retries(limit: int = Query(50),
                       x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§5.1 的本地重试队列。S08 不可达期间的决定靠这里补做，不靠内存。"""
    return service.pending_acceptance(x_tenant_id, limit=limit)


@app.post("/review-tasks/{review_task_id}:retry-acceptance", status_code=202)
def retry_acceptance(review_task_id: str,
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.retry_pending(x_tenant_id, review_task_id)


@app.post("/review-tasks/{review_task_id}:expire-due", status_code=202)
def expire_due(review_task_id: str,
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§7。只过期，**不生成 accept**。"""
    return service.expire_due(x_tenant_id)


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}
