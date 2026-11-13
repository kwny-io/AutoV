"""S43 HTTP 层。

`contracts/openapi.v2.yaml` 只定义了 `POST /delivery-packages` 与
`POST /delivery-packages/{id}/publish`（S43-5 / S43-7），`service.yaml` 只声明
`POST /delivery-packages` 与 `GET /delivery-packages/{id}`。其余路由来自方案
§6.1/§6.2 与本服务自定的两张补充（S43-26 渠道配置、S43-36 对账与回执）。
本层按方案实现，**不改生成物**。

租户一律来自平台签名的 `X-Tenant-Id`。请求体里的 `tenant_id`（顶层或
`execution_context.tenant_id` / `context.tenant_id`）**只用于比对**，不一致
即 403 —— AGENTS.md 禁止把 LLM/请求体提供的 `tenant_id` 当可信输入。

六个实现细节：

1. **`Idempotency-Key` 必须等于 body 的 `command_id`。** 不相等就是 400，
   不是"以 header 为准"。否则调用方以为自己在重投，实际上在换一个键。
2. **幂等摘要用 `stable_request_hash`。** 重投时 trace_id / 时间戳变了不该
   被算成"同键不同 body"（那个 409 只留给真正的不同 body）。
3. **占位与落结果在事务之外。** SDK 的 `begin()` 先落占位、`finish()` 只
   UPDATE；内核事务回滚时 `abort()` 删掉占位，重投仍能成功。`abort()` 自己
   写不进去时吞掉它的 `OperationalError`，不盖住真正的错误。
4. **两个开关默认关闭。** `EXTERNAL_PUBLISH_ENABLED` 与
   `channel.enabled` 都没开时 publish 是 503 `CHANNEL_DISABLED`，**没有**
   "假装发过了"的回退路径（方案 §10 末条）。
5. **外发相关的上游默认是 503 桩。** deps 的 `build_*(None)` 返回必然抛
   `ServiceError(503, "..._UNAVAILABLE")` 的桩；本层不提供任何降级实现，
   因为"没配置就当通过"正是 E37-S43-04 要防的事。
6. **状态码有语义。** create 的 201 = 已定稿（SEALED）或已判 BLOCKED；
   202 = 仍在 DRAFTING（签名/依赖不可用，`retryable` 会说明）。publish 的
   200 只在"包已发过/同一渠道已有在飞 attempt，未重发"时出现。
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

from delivery.deps import (
    build_accepted_master_source,
    build_artifact_source,
    build_audit_sink,
    build_binding_source,
    build_candidate_source,
    build_connector,
    build_cost_source,
    build_memory_source,
    build_policy_source,
    build_qc_source,
    build_receipt_verifier,
    build_rights_source,
    build_signer,
    build_subtitle_source,
    build_timeline_source,
)
from delivery.schema import CONTRACT_GAPS
from delivery.service import DeliveryService, ServiceError, stable_request_hash

DB_PATH = os.environ.get("S43_DB", ":memory:")

app = FastAPI(title="S43 delivery", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row


def _qc_base_urls() -> dict:
    return {sid: os.environ.get(f"{sid}_BASE_URL")
            for sid in ("S35", "S36", "S37")}


service = DeliveryService(
    _conn,
    accepted_master_source=build_accepted_master_source(
        os.environ.get("S08_BASE_URL")),
    artifact_source=build_artifact_source(os.environ.get("S28_BASE_URL")),
    timeline_source=build_timeline_source(os.environ.get("S41_BASE_URL")),
    candidate_source=build_candidate_source(os.environ.get("S42_BASE_URL")),
    qc_source=build_qc_source(_qc_base_urls()),
    subtitle_source=build_subtitle_source(os.environ.get("S38_BASE_URL")),
    rights_source=build_rights_source(os.environ.get("S15_BASE_URL")),
    policy_source=build_policy_source(os.environ.get("S02_BASE_URL")),
    memory_source=build_memory_source(os.environ.get("S16_BASE_URL")),
    binding_source=build_binding_source(os.environ.get("S09_BASE_URL")),
    cost_source=build_cost_source(os.environ.get("S23_BASE_URL")),
    audit_sink=build_audit_sink(os.environ.get("S45_BASE_URL")),
    signer=build_signer(os.environ.get("SIGNER_BASE_URL")),
    connector=build_connector(os.environ.get("CONNECTOR_BASE_URL")),
    receipt_verifier=build_receipt_verifier(
        os.environ.get("RECEIPT_VERIFIER_BASE_URL")),
    # 平台级外发开关，默认关闭（方案 §10 末条）。
    external_publish_enabled=(
        os.environ.get("EXTERNAL_PUBLISH_ENABLED", "false").lower() == "true"),
    unknown_ttl_hours=float(os.environ.get("S43_UNKNOWN_TTL_HOURS", "24")),
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
    # 同键不同 body：**409，不是覆盖**（AGENTS.md）。两个 hash 都回给调用方，
    # 否则它无法判断自己是不是真的换了 body。
    return _err(409, exc.code, str(exc), False,
                {"stored_request_hash": exc.stored_hash,
                 "incoming_request_hash": exc.incoming_hash})


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(
        status_code=409, headers={"Retry-After": "1"},
        content={"code": "COMMAND_IN_FLIGHT", "message": str(exc),
                 "retryable": True, "correlation_id": "", "details": {}})


@app.exception_handler(sqlite3.OperationalError)
async def _operational(_request, exc: sqlite3.OperationalError):
    """库不可写时命令状态**不可知**：报 409 会让调用方以为"已经做过了"，
    报 500 会让它以为"肯定没做"。两者都可能造成重复外发，按 5xx 可重试。"""
    return _err(503, "STORE_UNAVAILABLE", f"sqlite: {exc}", retryable=True)


def _body(payload, command_id: str, tenant_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    if not command_id:
        raise ServiceError(400, "INVALID_BODY", "Idempotency-Key is required")
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    inside = payload.get("command_id")
    if inside is None:
        # 契约（S43-5）把 command_id 放在 body 里。缺了就用 header 补，但
        # 不能反过来：header 与 body 不一致是错误，不是"以 header 为准"。
        payload = {**payload, "command_id": command_id}
    elif inside != command_id:
        raise ServiceError(400, "INVALID_BODY",
                           "Idempotency-Key does not match body command_id")
    contexts = [payload.get("context"), payload.get("execution_context")]
    for context in contexts:
        if not isinstance(context, dict):
            continue
        candidate = context.get("tenant_id")
        if candidate is not None and candidate != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "body context tenant_id disagrees with the "
                               "signed context")
    candidate = payload.get("tenant_id")
    if candidate is not None and candidate != tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放**不重新执行**：第一次已经建了包/开了 attempt/发了事件；再跑一遍会
    产生第二个 attempt 或第二条事件，而 `(tenant, command_id)` 的语义是
    "这件事只发生一次"（方案 §7.1）。
    """
    request_hash = stable_request_hash(body)
    record = _idem.begin(tenant_id, body["command_id"], request_hash)
    if record.result is not None:
        replay = json.loads(record.result)
        replay["replayed"] = True
        return replay
    try:
        result = execute(request_hash)
    except Exception:
        try:
            _idem.abort(tenant_id, body["command_id"])
        except sqlite3.OperationalError:
            pass
        raise
    _idem.finish(tenant_id, body["command_id"],
                 json.dumps(result, ensure_ascii=False))
    return {**result, "replayed": False}


def _tenant(tenant_id: str) -> str:
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return tenant_id


def _json(status: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=payload)


# ------------------------------------------------------------------ 命令

@app.post("/delivery-packages")
def create_package(payload: dict,
                   command_id: str = Header(default="", alias="Idempotency-Key"),
                   x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """201 = 已定稿（SEALED）或已判 BLOCKED；202 = 仍在 DRAFTING（可重试）。

    **不是 201 就代表可交付**：BLOCKED 也是 201（包确实建成了，只是缺东西），
    能不能交付看 `deliverable` 与 `gate_results`。
    """
    body = _body(payload, command_id, x_tenant_id)
    result = command(x_tenant_id, body,
                     lambda request_hash: service.create(
                         x_tenant_id, command_id, body, request_hash))
    if result.get("retryable"):
        return _json(202, result)
    return _json(201, result)


@app.post("/delivery-packages/{package_id}:seal")
def seal_package(package_id: str, payload: dict | None = None,
                 command_id: str = Header(default="", alias="Idempotency-Key"),
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """重复封包返回冻结的清单，不重算 hash（E12-S43-03）。"""
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda request_hash: service.seal(
                       x_tenant_id, command_id, package_id, request_hash))


@app.post("/delivery-packages/{package_id}/publish")
def publish_package(package_id: str, payload: dict | None = None,
                    command_id: str = Header(default="", alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """202 = 已尝试外发，结果看 attempt 的 `state`（**不是**"已送达"）。

    `state=PENDING`（人工交接）与 `state=UNKNOWN`（连接断）都不是成功；
    客户签收在 `receipt` 里。
    """
    body = _body(payload or {}, command_id, x_tenant_id)
    result = command(x_tenant_id, body,
                     lambda request_hash: service.publish(
                         x_tenant_id, command_id, package_id, body,
                         request_hash))
    # 只有**这一次真的调了连接器**才是 202；复用既有 attempt（已发过 /
    # 同渠道有在飞）时内核标 `dispatched=False`，返回 200。不能用 `note`
    # 判断：人工交接、结果未知、可重试失败都带 note，但它们确实尝试过外发。
    if result.get("dispatched") is False:
        return _json(200, result)
    return _json(202, result)


@app.post("/delivery-packages/{package_id}:reconcile")
def reconcile_package(package_id: str, payload: dict | None = None,
                      command_id: str = Header(default="", alias="Idempotency-Key"),
                      x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """`UNKNOWN` 的唯一出口。**本路由不会重新外发**（S43-36 / §3.2）。"""
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda request_hash: service.reconcile(
                       x_tenant_id, command_id, package_id, body,
                       request_hash))


@app.post("/receipts")
def ingest_receipt(payload: dict,
                   command_id: str = Header(default="", alias="Idempotency-Key"),
                   x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """渠道回调入口（S43-36）。验不了签名就不是送达证据（§4.6）。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda request_hash: service.ingest_receipt(
                       x_tenant_id, command_id, body, request_hash))


# ------------------------------------------------------------------ 查询

@app.get("/delivery-packages")
def list_packages(x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                  episode_id: str | None = Query(default=None),
                  package_type: str | None = Query(default=None),
                  state: str | None = Query(default=None),
                  limit: int = Query(default=50)):
    return service.list_packages(_tenant(x_tenant_id),
                                 episode_id=episode_id,
                                 package_type=package_type, state=state,
                                 limit=limit)


@app.get("/delivery-packages/{package_id}")
def get_package(package_id: str,
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """跨租户与不存在同样是 404（E28-S43-01）。"""
    return service.get_package(_tenant(x_tenant_id), package_id)


@app.get("/delivery-packages/{package_id}/manifest")
def get_manifest(package_id: str,
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                 signed: bool = Query(default=False)):
    return service.get_manifest(_tenant(x_tenant_id), package_id,
                                signed=signed)


@app.get("/delivery-packages/{package_id}/manifest/verify")
def verify_manifest(package_id: str,
                    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                    canonical_version: str | None = Query(default=None)):
    """重算 hash 与冻结值比对（C-S43-01/03/04）。不认识的角色直接拒绝。"""
    return service.verify_manifest(_tenant(x_tenant_id), package_id,
                                   canonical_version=canonical_version)


@app.get("/delivery-packages/{package_id}/evidence")
def get_evidence(package_id: str,
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_evidence(_tenant(x_tenant_id), package_id)


@app.get("/delivery-packages/{package_id}/attempts")
def get_attempts(package_id: str,
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_attempts(_tenant(x_tenant_id), package_id)


# ------------------------------------------------------------------ 渠道

@app.post("/channels")
def put_channel(payload: dict,
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """渠道配置（S43-26）。`enabled` 默认 false —— **不预填 true**。

    写配置不是命令语义（同一份配置重复写结果相同），所以不走幂等表；
    仍然要 `X-Tenant-Id`，渠道按租户隔离（E28-S43-04）。
    """
    tenant = _tenant(x_tenant_id)
    body = payload if isinstance(payload, dict) else None
    if body is None:
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    channel_id = body.get("channel_id")
    if not channel_id:
        raise ServiceError(422, "CHANNEL_REQUIRED", "channel_id is required")
    if body.get("tenant_id") is not None \
            and body["tenant_id"] != tenant:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return service.put_channel(tenant, str(channel_id), body)


@app.get("/channels")
def list_channels(x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.list_channels(_tenant(x_tenant_id))


# ------------------------------------------------------------------ 事件

@app.post("/events:consume")
def consume_event(payload: dict,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    """上游事件入口。inbox 自己就是幂等。

    **投影永不放行任何门**（S43-25）：G6/G7 仍然实时回查 S38/S15。
    """
    envelope = payload.get("event", payload)
    if not isinstance(envelope, dict):
        raise ServiceError(400, "INVALID_BODY", "event must be an object")
    tenant = envelope.get("tenant_id")
    if tenant is None:
        envelope = {**envelope, "tenant_id": _tenant(x_tenant_id)}
    elif tenant != x_tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "event tenant disagrees with the signed context")
    return service.consume_event(x_tenant_id, envelope)


# ------------------------------------------------------------------ 巡检

@app.get("/health/readiness")
def readiness():
    """`ready` 是"现在能不能建一个 formal 包"，不是"库连不连得上"。

    门是 fail-closed 的：任何一个上游没配，对应的门就判不了，formal 包
    整体 503（§2.3「无法判定 ≠ 通过」）。默认部署里 S08/S28/S41 等都没配，
    所以这里如实报 `ready: false` 并列出缺哪些——把"起得来但一律 503"报成
    ready，会让编排层等到第一次派单才发现。
    """
    reachable = _conn.execute("SELECT 1").fetchone() is not None
    blockers = [] if reachable else ["DATABASE_UNREACHABLE"]
    blockers.extend(service.unavailable_dependencies())
    return {"ready": not blockers,
            "service_id": "S43",
            "blockers": blockers,
            "external_publish_enabled": service.external_publish_enabled,
            "contract_gaps": CONTRACT_GAPS}
