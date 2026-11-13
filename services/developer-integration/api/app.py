"""S46 developer-integration 的 HTTP 面。

薄。所有判定都在 `DeveloperIntegrationService` 里；这一层只做三件事：
把 HTTP 头翻成 `AuthContext`，把 `ServiceError` 翻成 §4.6 的响应体，把
幂等 SDK 的两种 409 翻成本服务自己的错误码。

**这里没有 `tenant_id` 的来源。** 它只出现在 `X-Tenant-Id`（由网关按
`AuthContext` 注入）和 `X-Expected-Tenant`（仅供一致性校验，不参与取数）。
外部命令体里出现 `tenant_id` 会被内核按 `MALFORMED_REQUEST` 拒掉。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from developer_integration import (
    CONTRACT_GAPS,
    SCHEMA_VERSION,
    DeveloperIntegrationService,
    LocalTokenBuckets,
    ServiceError,
    Transaction,
)
from developer_integration.service import (
    COMMAND_IN_BODY_KEYS_ABSENT_FROM_HASH,
    UnconfiguredUpstream,
    build_dependencies,
)

DB_PATH = os.environ.get("S46_DB", ":memory:")

app = FastAPI(title="S46 developer-integration", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
service = build_dependencies(_conn)
_idem = SqliteIdempotencyStore(_conn)


def _err(status: int, code: str, message: str, retryable: bool = False,
         details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "retryable": retryable,
                 "correlation_id": "", "details": details or {}})


@app.exception_handler(ServiceError)
async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
    headers = {}
    if exc.status == 429:
        retry_after = (exc.details or {}).get("retry_after")
        if retry_after:
            headers["Retry-After"] = str(int(retry_after))
    return JSONResponse(
        status_code=exc.status,
        content={"code": exc.code, "message": str(exc),
                 "retryable": exc.retryable, "correlation_id": "",
                 "details": exc.details or {}},
        headers=headers or None)


@app.exception_handler(CommandConflict)
async def _conflict(_: Request, exc: CommandConflict) -> JSONResponse:
    """同 `(tenant, command_id)` 不同 body。**409，不是覆盖**（AGENTS.md）。"""
    return _err(409, "IDEMPOTENCY_KEY_REUSED",
                "command_id was already used with a different request body",
                details={"sdk_code": getattr(exc, "code", "COMMAND_CONFLICT")})


@app.exception_handler(CommandInProgress)
async def _in_progress(_: Request, exc: CommandInProgress) -> JSONResponse:
    response = _err(409, "COMMAND_IN_PROGRESS",
                    "the same command_id is still being processed",
                    retryable=True)
    response.headers["Retry-After"] = "1"
    return response


# ------------------------------------------------------------------ 认证

def _credential(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _auth(request: Request):
    """`verify(credential)`。**不读请求体**（§2.1）。"""
    return service.verify_credential(_credential(request))


def _tenant(request: Request, auth) -> str:
    """`X-Expected-Tenant` 只做一致性校验。返回的永远是 `auth.tenant_id`。"""
    expected = request.headers.get("x-expected-tenant")
    if expected is not None and expected != auth.tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "expected tenant does not match credential")
    header = request.headers.get("x-tenant-id")
    if header is not None and header != auth.tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "tenant header does not match credential")
    return auth.tenant_id


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "body must be a JSON object")
    return payload


def _require(auth, scope: str) -> None:
    if not auth.has(scope):
        raise ServiceError(
            403, "INSUFFICIENT_SCOPE",
            f"this endpoint requires scope {scope}",
            details={"required_scope": scope,
                     "granted_scopes": sorted(auth.scopes)})


def _command(request: Request, auth, body: dict, execute) -> dict:
    """幂等外壳。SDK 的记录是快速路径；回执的 `icr_cmd_uniq` 是持久兜底。

    两者都按 `(tenant_id, command_id, request_hash)` 判定，同键异 body 都是
    409。**不**把 SDK 的记录当业务事实——它只存响应，回执才是可审计的那份。
    """
    command_id = (body.get("command_id")
                  or request.headers.get("idempotency-key") or "").strip()
    # 用内核同一把尺子算键。SDK 的 `canonical_request_hash` 只排除
    # `command_id` / `request_hash`，这里还要排除 `client_trace_id`；
    # 混用会让「改 trace_id 重试」在 SDK 是 409、在回执是回放。
    request_hash = service.request_hash(body)
    try:
        record = _idem.begin(auth.tenant_id, command_id, request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = execute(command_id, request_hash)
        except Exception:
            try:
                _idem.abort(auth.tenant_id, command_id)
            except sqlite3.OperationalError:
                pass
            raise
        _idem.finish(auth.tenant_id, command_id,
                     json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}
    except sqlite3.OperationalError as exc:
        # SDK 的幂等写入在 `Transaction` 之外：SQLite 的写锁被占时它抛的是
        # 裸 OperationalError。必须变成 503，否则 FastAPI 会回 500 无 code。
        raise ServiceError(503, "DEVELOPER_INTEGRATION_UNAVAILABLE",
                           "storage is temporarily unavailable",
                           retryable=True) from exc


# ------------------------------------------------------------------ 命令

@app.post("/integrations/commands", status_code=202)
async def post_command(request: Request) -> JSONResponse:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    body = await _body(request)

    def execute(command_id: str, request_hash: str) -> dict:
        # `x-expected-tenant` 已在上方校验；内核再校验一次是刻意的——内核
        # 可能被 MCP/agent-runtime 直接调用，不能指望 HTTP 层。
        receipt, outcome = service.submit_command(
            auth, body, command_id=command_id,
            expected_tenant=request.headers.get("x-expected-tenant"))
        view = receipt
        if outcome is not None:
            view = {**view,
                    "state": receipt["state"],
                    "forward_state": outcome.state,
                    "upstream_ref": outcome.upstream_ref,
                    "upstream_error": (
                        {"code": outcome.code, "message": outcome.message,
                         "retryable": outcome.retryable}
                        if outcome.code else None)}
        return {**view, "poll_url": f"/operations/{receipt['operation_id']}",
                "tenant_id_derived": tenant}

    result = _command(request, auth, body, execute)
    status = 202
    if result.get("replayed"):
        status = 202
    elif result.get("forward_state") in ("REJECTED",):
        status = 202
    return JSONResponse(status_code=status, content=result)


@app.get("/operations/{operation_id}")
async def get_operation(operation_id: str, request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    service._rate_limit(auth, "query", None)
    return service.get_operation(tenant, operation_id)


@app.get("/operations")
async def list_operations(request: Request, state: str | None = None,
                          command: str | None = None,
                          limit: int = 50) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    service._rate_limit(auth, "query", None)
    return service.list_operations(tenant, state=state, command=command,
                                   limit=limit)


@app.post("/operations/{operation_id}:cancel")
async def cancel_operation(operation_id: str, request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    return service.cancel_operation(tenant, operation_id)


@app.post("/operations/{operation_id}:reconcile")
async def reconcile_operation(operation_id: str, request: Request) -> dict:
    """单条对账。**只处理 `FORWARD_UNKNOWN`**，不做别的状态迁移。"""
    auth = _auth(request)
    tenant = _tenant(request, auth)
    _require(auth, "admin")
    receipt = service._load_receipt(tenant, operation_id)
    if receipt["state"] != "FORWARD_UNKNOWN":
        return {"operation_id": operation_id,
                "resolution": "not_applicable", "state": receipt["state"]}
    service._bump("s46_admin_manual_reconcile_total")
    return {"operation_id": operation_id,
            "resolution": service.reconcile_one(operation_id)}


# ------------------------------------------------------------------ Webhook

@app.post("/webhook-subscriptions", status_code=201)
async def create_subscription(request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    _require(auth, "admin")
    service._rate_limit(auth, "webhook_admin", None)
    body = await _body(request)
    if not body.get("url"):
        raise ServiceError(400, "MALFORMED_REQUEST", "url is required")
    if not body.get("secret"):
        raise ServiceError(400, "MALFORMED_REQUEST", "secret is required")
    return service.create_subscription(
        tenant, url=body["url"], event_types=body.get("event_types"),
        secret=body["secret"], api_client_id=auth.api_client_id,
        description=body.get("description"))


@app.get("/webhook-subscriptions")
async def list_subscriptions(request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    service._rate_limit(auth, "webhook_admin", None)
    return service.list_subscriptions(tenant)


@app.get("/webhook-subscriptions/{subscription_id}")
async def get_subscription(subscription_id: str, request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    service._rate_limit(auth, "webhook_admin", None)
    return service.get_subscription(tenant, subscription_id)


@app.patch("/webhook-subscriptions/{subscription_id}")
async def update_subscription(subscription_id: str, request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    _require(auth, "admin")
    body = await _body(request)
    if "secret" in body or "secret_ref" in body:
        raise ServiceError(
            400, "MALFORMED_REQUEST",
            "secret cannot be changed in place; rotate it instead")
    return service.update_subscription(
        tenant, subscription_id, url=body.get("url"),
        event_types=body.get("event_types"), active=body.get("active"),
        description=body.get("description"))


@app.post("/webhook-subscriptions/{subscription_id}:rotate-secret")
async def rotate_secret(subscription_id: str, request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    _require(auth, "admin")
    body = await _body(request)
    if not body.get("secret"):
        raise ServiceError(400, "MALFORMED_REQUEST", "secret is required")
    return service.rotate_subscription_secret(tenant, subscription_id,
                                              body["secret"])


@app.delete("/webhook-subscriptions/{subscription_id}")
async def delete_subscription(subscription_id: str, request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    _require(auth, "admin")
    return service.delete_subscription(tenant, subscription_id)


@app.get("/webhook-deliveries")
async def list_deliveries(request: Request,
                          subscription_id: str | None = None,
                          state: str | None = None,
                          limit: int = 50) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    service._rate_limit(auth, "webhook_admin", None)
    return service.list_deliveries(tenant, subscription_id=subscription_id,
                                   state=state, limit=limit)


# ------------------------------------------------------------------ 凭据

@app.get("/api-keys")
async def list_api_keys(request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    # 只列**自己** client 的 key：跨 client 列 key 会让一个泄漏的 admin key
    # 变成枚举面。
    return service.list_api_keys(tenant, auth.api_client_id)


@app.post("/api-keys", status_code=201)
async def create_api_key(request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    _require(auth, "admin")
    body = await _body(request)
    client_id = body.get("api_client_id") or auth.api_client_id
    return service.create_api_key(tenant, client_id)


@app.post("/api-keys/{kid}:retire")
async def retire_api_key(kid: str, request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    _require(auth, "admin")
    return service.retire_api_key(tenant, kid)


@app.delete("/api-keys/{kid}")
async def revoke_api_key(kid: str, request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    _require(auth, "admin")
    return service.revoke_api_key(tenant, kid)


@app.get("/api-clients/{api_client_id}")
async def get_client(api_client_id: str, request: Request) -> dict:
    auth = _auth(request)
    tenant = _tenant(request, auth)
    if api_client_id != auth.api_client_id:
        _require(auth, "admin")
    return service.get_client(tenant, api_client_id)


@app.get("/readiness")
async def readiness() -> dict:
    """未配置的依赖要显式报出来，**不能**假装 ready（§7.1）。"""
    return {
        "ready": True,
        "service": "S46",
        "schema_version": SCHEMA_VERSION,
        "upstream_configured": not isinstance(service.upstream,
                                              UnconfiguredUpstream),
        "authorization_source": service.authorization_source is not None,
        "webhook_sender": service.webhook_sender is not None,
        "resolver": service.resolver is not None,
        "contract_gaps": CONTRACT_GAPS,
    }
