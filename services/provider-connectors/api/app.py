"""S27 HTTP 层。

## 契约里有什么、没有什么

`contracts/service-catalog.v2.json` 为 S27 只声明了两条：

| 路径 | catalog 字段 |
|---|---|
| `POST /connector-commands` | `command_api` |
| `GET  /connector-receipts/{id}` | `query_api` |

其余入口方案明文要求，且**没有它们这个服务无法工作**，逐条登记为 C27-11：

| 入口 | 方案依据 |
|---|---|
| `POST /callbacks/{channel_route}` | §6.1 / §6.2（供应商回调的落地点） |
| `POST /internal/events/{topic}` | §9.1（S26 的四个命令事件） |
| `GET  /channels/{channel_route}/capabilities` | §3.1 |
| `POST /channels/{channel_route}:validate` | §3.2 |
| `POST /channels/{channel_route}:estimate` | §3.3 |
| `POST /janitor:sweep` | §5.3 崩溃恢复 + §6.4 UNMAPPED 超时 |

**不在这里偷改契约**：上面这些路径该不该进 openapi 是契约层的事，本文件只保证
行为正确并把缺口显式登记。同一个 S26 的做法（C26-14）。

## 回调为什么是一个"裸"入口

供应商不知道平台的 `Idempotency-Key` 是什么，也不知道 `X-Tenant-Id`。所以
`/callbacks/{channel_route}` **不带租户头、不带幂等键**，它唯一的身份来源是
HMAC 签名（§6.3）。验签失败必须 401 且不入库；验签通过先落 Inbox 再 ACK，
映射在响应之后（ACK-first，§6.2）—— 供应商的超时窗口不以我们的映射速度为准。

## 依赖端口

`transport`（出站 HTTP）、`claim_grant`（S26 的 claim）、`secret_for`（回调
secret）三个。不传就是**未接线**，用到时 503 `DEPENDENCY_UNAVAILABLE`
（`retryable=true`），不静默降级。
"""

from __future__ import annotations

import json
import os
import sqlite3

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from provider_connectors.schema import SCHEMA
from provider_connectors.service import (
    COMMAND_EVENTS,
    ClaimUnavailable,
    ProviderConnectorsService,
    ServiceError,
    UnknownChannel,
)

DB_PATH = os.environ.get("S27_DB", ":memory:")

# 命令事件与内部入口只给内部 principal（同 S26）。
INTERNAL_PRINCIPALS = {"S26", "S21", "S22", "S23", "S24", "S25", "S27", "S28",
                       "S47", "janitor", "ops", "test"}

INBOUND_TOPICS = COMMAND_EVENTS


def _err(status: int, code: str, message: str, retryable: bool = False,
         details: dict | None = None, correlation_id: str = ""):
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "retryable": retryable,
                 "correlation_id": correlation_id or "s27",
                 "details": details or {}})


def create_app(*, conn: sqlite3.Connection | None = None,
               db_path: str | None = None, **ports) -> FastAPI:
    app = FastAPI(title="S27 provider-connectors", version="2.0.0")

    if conn is None:
        conn = sqlite3.connect(db_path or DB_PATH, check_same_thread=False,
                               isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    app.state.conn = conn
    app.state.idem = SqliteIdempotencyStore(conn)
    app.state.service = ProviderConnectorsService(conn, **ports)

    @app.on_event("startup")
    def _recover():
        # §5.3：进程起来第一件事是把上次崩溃遗留的 SENDING 判成 unknown。
        # 这一步在启动时做，不在请求路径上 —— 否则第一条命令就会先撞它。
        try:
            app.state.service.recover_stale_sending()
        except Exception:                    # 恢复失败不该阻止服务起来
            pass

    @app.exception_handler(ServiceError)
    async def _service_error(_request, exc: ServiceError):
        return _err(exc.status, exc.code, exc.message, exc.retryable, exc.details)

    @app.exception_handler(CommandConflict)
    async def _conflict(_request, exc: CommandConflict):
        return _err(409, "IDEMPOTENCY_KEY_REUSED", str(exc))

    @app.exception_handler(CommandInProgress)
    async def _in_progress(_request, exc: CommandInProgress):
        return JSONResponse(status_code=429, headers={"Retry-After": "1"},
                            content={"code": "COMMAND_IN_PROGRESS",
                                     "message": str(exc), "retryable": True,
                                     "correlation_id": "s27", "details": {}})

    @app.exception_handler(UnknownChannel)
    async def _unknown_channel(_request, exc: UnknownChannel):
        # 不 ACK、不发事件、不猜：让上游重投直到 dead-letter（§2.1）。
        return _err(404, "CHANNEL_NOT_FOUND",
                    "no adapter is registered for this channel",
                    details={"channel_route": str(exc)})

    @app.exception_handler(ClaimUnavailable)
    async def _claim_unavailable(_request, exc: ClaimUnavailable):
        # 走到这里说明服务内部吞掉了它 —— 正常路径在 service 里已经翻译成
        # `not_sent` 观测。保留处理器是为了不把未知异常泄漏成 500。
        return _err(exc.status, exc.code, "grant is not available",
                    retryable=False, details=exc.details)

    # ---- 共用校验 --------------------------------------------------------

    def _tenant(x_tenant_id: str) -> str:
        if not x_tenant_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
        return x_tenant_id

    def _internal(principal: str) -> None:
        if principal not in INTERNAL_PRINCIPALS:
            raise ServiceError(
                403, "TENANT_SCOPE_DENIED",
                "this endpoint is internal; caller identity is not an "
                "internal principal", details={"principal": principal or None})

    # ---- catalog: command_api --------------------------------------------

    @app.post("/connector-commands", status_code=202)
    def connector_command(
            payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
            x_principal: str = Header(default="", alias="X-Principal")):
        """同步命令面：`capabilities` / `validate` / `estimate` 走这里。

        **`submit` 不走这里。** 提交必须由 `connector.submit.requested` 事件
        驱动（§9.1），因为它前面有两道门（grant + 回执）。给 HTTP 留一个直接
        提交的口子等于多一条能绕过 claim 的付费路径。
        """
        _internal(x_principal or "S27")
        tenant_id = _tenant(x_tenant_id)
        if not command_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "Idempotency-Key is required")
        if not isinstance(payload, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "body must be a JSON object")
        op = payload.get("op")
        channel_route = payload.get("channel_route")
        if op not in ("capabilities", "validate", "estimate"):
            raise ServiceError(
                400, "UNSUPPORTED_OPERATION",
                "connector-commands accepts capabilities|validate|estimate;"
                " submit is event-driven only",
                details={"op": op})
        if not channel_route:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "channel_route is required")
        inside_tenant = payload.get("tenant_id")
        if inside_tenant is not None and inside_tenant != tenant_id:
            raise ServiceError(403, "TENANT_SCOPE_DENIED",
                               "body tenant_id conflicts with the authenticated"
                               " tenant",
                               details={"body_tenant_id": inside_tenant})

        body = {**payload, "command_id": command_id, "tenant_id": tenant_id}
        idem = app.state.idem
        request_hash = canonical_request_hash(body)
        record = idem.begin(tenant_id, command_id, request_hash)
        if record.result is not None:
            return json.loads(record.result)
        svc = app.state.service
        try:
            if op == "capabilities":
                result = svc.capabilities(channel_route)
            elif op == "validate":
                result = svc.validate(channel_route, payload.get("submit_payload") or {})
            else:
                result = svc.estimate(channel_route, payload.get("submit_payload") or {})
        except Exception:
            idem.abort(tenant_id, command_id)
            raise
        idem.finish(tenant_id, command_id,
                    json.dumps(result, ensure_ascii=False, default=str))
        return result

    # ---- catalog: query_api ----------------------------------------------

    @app.get("/connector-receipts/{receipt_id}")
    def get_receipt(receipt_id: str,
                    x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        return app.state.service.get_receipt(_tenant(x_tenant_id), receipt_id)

    # ---- §3.1 / §3.2 / §3.3（C27-11）-------------------------------------

    @app.get("/channels/{channel_route:path}/capabilities")
    def get_capabilities(channel_route: str,
                        x_tenant_id: str = Header(default="",
                                                 alias="X-Tenant-Id")):
        _tenant(x_tenant_id)
        return app.state.service.capabilities(channel_route)

    @app.post("/channels/{channel_route:path}:validate")
    def post_validate(channel_route: str, payload: dict,
                      x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        _tenant(x_tenant_id)
        return app.state.service.validate(channel_route, payload)

    @app.post("/channels/{channel_route:path}:estimate")
    def post_estimate(channel_route: str, payload: dict,
                      x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        _tenant(x_tenant_id)
        return app.state.service.estimate(channel_route, payload)

    # ---- §6.1 回调（C27-11）----------------------------------------------

    @app.post("/callbacks/{channel_route:path}")
    async def receive_callback(channel_route: str, request: Request):
        """**不带租户头、不带幂等键。** 身份的唯一来源是 HMAC（§6.3）。

        `raw_body` 必须是原始字节：先解码再重编码会改变字节，验签必然失败。
        """
        raw = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        result = app.state.service.receive_callback(channel_route, raw, headers)
        # ACK 之后才映射（ACK-first，§6.2）。映射失败不回退这个 200：
        # 回调已入库，重投只会重复映射，而 Inbox 已经收下了。
        try:
            app.state.service.map_pending_callbacks(channel_route)
        except Exception:
            pass
        return result

    # ---- §9.1 事件摄入（C27-11）------------------------------------------

    @app.post("/internal/events/{topic}")
    def ingest_event(topic: str, event: dict,
                     x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                     x_principal: str = Header(default="", alias="X-Principal")):
        _internal(x_principal or "S26")
        tenant_id = _tenant(x_tenant_id)
        if not isinstance(event, dict) or not event.get("id"):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "event envelope needs `id`")
        if topic not in INBOUND_TOPICS:
            raise ServiceError(422, "UNSUPPORTED_EVENT",
                               f"{topic} is not an inbound connector topic",
                               details={"topic": topic})
        if event.get("type") != topic:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "event type does not match the topic",
                               details={"type": event.get("type"),
                                        "topic": topic})
        if event.get("tenant_id") != tenant_id:
            raise ServiceError(403, "TENANT_SCOPE_DENIED",
                               "event tenant_id conflicts with the authenticated"
                               " tenant")
        return app.state.service.consume(event)

    # ---- §5.3 / §6.4 巡检（C27-11）---------------------------------------

    @app.post("/janitor:sweep")
    def janitor(x_principal: str = Header(default="", alias="X-Principal")):
        _internal(x_principal or "janitor")
        svc = app.state.service
        recovered = svc.recover_stale_sending()
        callbacks = svc.map_pending_callbacks()
        return {"recovered": recovered["recovered"],
                "receipt_ids": recovered["receipt_ids"],
                "mapped": callbacks["mapped"],
                "unmapped": callbacks["unmapped"],
                "dead_letter": callbacks["dead_letter"]}

    return app


app = create_app()
