"""S41 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）里没有 S41 的任何路径；方案 §6.1/§6.2
定义了它们（§11 C3 已登记「OpenAPI 无 S41 的命令入口」）。本层按方案实现，
不改契约。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id`（无论顶层还是
`context.tenant_id`）**只用于比对**，不一致即 403。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

三处实现必须解释的地方：

1. **同键不同 body 的码。** 方案 §3.3 叫 `IDEMPOTENCY_MISMATCH`，平台
   SDK 的 `CommandConflict.code` 是 `IDEMPOTENCY_KEY_REUSED`。本层返回方案
   点名的那个码，SDK 原名放 `details.sdk_code`（CONTRACT_GAPS E41-IDEM-CODE）。
2. **幂等占位与落结果在事务之外。** 方案 §7.1 要求「幂等记录与 revision
   同事务」。SDK 的 `begin()` 先落一行占位、`finish()` 只做 UPDATE，所以
   只要结果与 revision 在同一次提交里可见，语义就成立；事务回滚时本层
   `abort()` 删掉占位行，重投仍能成功（E12-S41-04）。
3. **`request_hash` 不含 trace/时间戳**（§7.1）。本层调 `stable_request_hash`
   而不是 SDK 的裸 `canonical_request_hash`（CONTRACT_GAPS E41-TRACE-HASH）。
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
)

from editorial.deps import build_artifact_source, build_authorization_source
from editorial.schema import CONTRACT_GAPS
from editorial.service import (
    IDEMPOTENCY_MISMATCH,
    ServiceError,
    TimelineService,
    stable_request_hash,
)

DB_PATH = os.environ.get("S41_DB", ":memory:")

app = FastAPI(title="S41 editorial", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = TimelineService(
    _conn,
    artifact_source=build_artifact_source(os.environ.get("S28_BASE_URL")),
)
# 授权源是可选的。没有它时本服务不自行解释权限（CONTRACT_GAPS E41-AUTHZ），
# 也不把「没配置」读成「允许」——它只是不做那一步，租户仍只来自 header。
authorization_source = build_authorization_source(os.environ.get("S01_BASE_URL"))
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
    # 同键不同 body：409，不是覆盖（AGENTS.md）。码按方案 §3.3。
    return _err(409, IDEMPOTENCY_MISMATCH, str(exc), False,
                {"sdk_code": exc.code,
                 "stored_request_hash": exc.stored_hash,
                 "incoming_request_hash": exc.incoming_hash,
                 "gap": "E41-IDEM-CODE"})


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
    # 顶层与 context 里的 tenant_id 都只用于比对。两者都查，是因为命令体
    # 的租户在 context 里；漏掉它就等于相信了请求体。
    for candidate in (payload.get("tenant_id"),
                      (payload.get("context") or {}).get("tenant_id")
                      if isinstance(payload.get("context"), dict) else None):
        if candidate is not None and candidate != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。"""
    request_hash = stable_request_hash(body)
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
        # 内核的 `Transaction` 只映射它自己的 BEGIN IMMEDIATE；幂等占位与
        # 落结果在事务之外。不在这里映射就是未捕获异常 → 500，与「写不进去
        # 不等于请求有错」相悖（§3.3：503 可重试）。
        raise ServiceError(
            503, "EDITORIAL_UNAVAILABLE",
            f"editorial database is not writable: {exc}",
            retryable=True) from exc


def _tenant(tenant_id: str) -> str:
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return tenant_id


# ------------------------------------------------------------------ 命令

@app.post("/timelines/{timeline_id}/revisions", status_code=201)
def commit_revision(timeline_id: str, payload: dict,
                    command_id: str = Header(default="",
                                             alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id")):
    """201 只表示「新 revision 已提交」，**不表示已接受**（§6.1 末段）。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.commit_revision(
                       x_tenant_id, timeline_id, body, command_id,
                       request_hash))


# ------------------------------------------------------------------ 事件

@app.post("/events:consume")
def consume_event(payload: dict,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    """`snapshot.manifest.committed` 的投递入口。inbox 自己就是幂等。

    **这不是接受判定**：它只把 S08 的 fence 投影到本地（§3.2），
    并明确**不写** `node_epoch`。
    """
    envelope = payload.get("event", payload)
    if not isinstance(envelope, dict):
        raise ServiceError(400, "INVALID_BODY", "event must be an object")
    if envelope.get("tenant_id") is None:
        envelope = {**envelope, "tenant_id": _tenant(x_tenant_id)}
    elif envelope["tenant_id"] != x_tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "event tenant disagrees with the signed context")
    return service.consume_manifest_event(x_tenant_id, envelope)


@app.post("/timelines/{timeline_id}/revisions/{revision}:retire")
def retire_revision(timeline_id: str, revision: int, payload: dict,
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id")):
    """COMMITTED → SUPERSEDED / VOIDED。终态不回退（§3.1）。

    不是剪辑命令：它不改内容，也不是接受决策。VOIDED 只影响本服务，
    S08 那边是否接受由 S08 决定（§7.3）。
    """
    body = payload if isinstance(payload, dict) else {}
    target = body.get("target")
    return service.retire_revision(_tenant(x_tenant_id), timeline_id,
                                   revision, target)


# ------------------------------------------------------------------ 查询

@app.get("/timelines/{timeline_id}/revisions/{revision}")
def get_revision(timeline_id: str, revision: int,
                 x_tenant_id: str = Header(default="",
                                           alias="X-Tenant-Id")):
    return service.get_revision(_tenant(x_tenant_id), timeline_id, revision)


@app.get("/timelines/{timeline_id}/diff")
def get_diff(timeline_id: str,
             from_revision: int = Query(alias="from"),
             to_revision: int = Query(alias="to"),
             x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_diff(_tenant(x_tenant_id), timeline_id, from_revision,
                            to_revision)


@app.get("/timelines/{timeline_id}/edl/{revision}")
def get_edl(timeline_id: str, revision: int,
            format: str = Query(default="json"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_edl(_tenant(x_tenant_id), timeline_id, revision,
                           format=format)


@app.get("/timelines")
def list_timelines(x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id"),
                   episode_id: str | None = Query(default=None),
                   branch_id: str | None = Query(default=None),
                   timeline_kind: str | None = Query(default=None),
                   limit: int = Query(default=50)):
    return service.list_timelines(_tenant(x_tenant_id), episode_id=episode_id,
                                  branch_id=branch_id,
                                  timeline_kind=timeline_kind, limit=limit)


@app.get("/timelines/{timeline_id}/revisions/{revision}/eligibility")
def render_eligibility(timeline_id: str, revision: int,
                       x_tenant_id: str = Header(default="",
                                                 alias="X-Tenant-Id")):
    """S42 渲染前的回查。VOIDED / SUPERSEDED / incomplete 一律 422。"""
    return service.render_eligibility(_tenant(x_tenant_id), timeline_id,
                                      revision)


@app.get("/health/readiness")
def readiness():
    row = _conn.execute("SELECT 1").fetchone()
    return {"ready": row is not None,
            "authorization_source": authorization_source is not None,
            "contract_gaps": CONTRACT_GAPS}
