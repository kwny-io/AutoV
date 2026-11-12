"""S26 HTTP 层。语义见 `plans/S26-generation-jobs.md` §12 与 §10.9。

## 契约里有什么、没有什么

`contracts/openapi.v2.yaml` 为 S26 只声明了**两条**路径：

- `POST /generation-jobs` → `createGenerationJob`（202，`Operation`）
- `GET  /admissions/{admission_id}` → `getAdmission`（200，`admission-record.v2`）

其余入口不在契约里，但方案明文要求，且**没有它们这个服务无法工作**：

| 入口 | 方案依据 | 登记 |
|---|---|---|
| `GET /generation-jobs/{id}` | §12.2（14 字段查询体） | C26-14 |
| `POST /dispatch-grants/{id}:claim` | §12.4 / §10.9（S27 领 grant，同步） | C26-14 |
| `POST /dispatch-grants/{id}:outcome` | §7.1（S27 报四个结论） | C26-14 |
| `POST /generation-jobs/{id}:advance` | §4（准入 Saga 往前推） | C26-14 |
| `POST /generation-jobs/{id}:settle` | §4 步骤 7 | C26-14 |
| `POST /generation-jobs/{id}:cancel` | §9.2 / §13 | C26-14 |
| `POST /generation-jobs/{id}:ingest-gap` | §8.2 第五步 | C26-14 |
| `POST /generation-jobs/{id}:resubmission-approval` | §7.3B | C26-14 |
| `POST /internal/events/{topic}` | §10.4（S27/S28 的观测） | C26-14 |
| `POST /janitor:sweep` | §9.1（八条巡检） | C26-14 |
| `POST /internal/fence-bumped` / `node-epoch-bumped` / `failover-recover` | §9.2 / §9.3 | C26-14 |

**不在这里偷改契约**：上面这些路径在 openapi 里补上属于契约层的工作，
本文件只保证行为正确并把缺口显式登记。

## 两处形状约束

1. `Operation` 是 `additionalProperties: false`，只有 `{operation_id, state, run_id}`
   （`operation_id`/`state` 必填）。所以 `create_job` 返回的
   `provider_job_id` / `attempt_id` / `admission_id` **必须剥掉**（C26-22）。
   方案 §12.1 说「`operation_id` 即 `provider_job_id` 的包装」——调用方要的那个
   id 就在 `operation_id` 里，没有信息损失。
2. `POST /generation-jobs` 的 body 是 `additionalProperties: false`，但 §12.1
   第 3 条要求校验调用方回显的 `route_decision_ref.content_hash`，而契约里
   **没有这个字段**。严格拒收会让一条强制校验永远执行不到，所以这里容忍它
   （C26-14），其余未知字段仍然 400。

## 依赖端口

S25（路由）与 S27（连接器）都是 `design_only`，本仓库也没有服务发出站 HTTP。
`create_app()` 接受全部端口 callable；不传就是**未接线**，用到时返回 503
`DEPENDENCY_UNAVAILABLE`（`retryable=true`），不静默降级 —— 这是 AGENTS.md
「用 mock 按契约走」的落点。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from typing import Any, Callable

from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from generation_jobs.schema import SCHEMA
from generation_jobs.service import GenerationJobsService, ServiceError

DB_PATH = os.environ.get("S26_DB", ":memory:")

# §10.4 / §9.1：观测摄入、claim、巡检、栅栏提升都只给内部 principal。
INTERNAL_PRINCIPALS = {"S26", "S21", "S22", "S23", "S24", "S25", "S27", "S28",
                       "S47", "janitor", "ops", "test"}

# §10.4 的两个入站 topic → 服务方法。
INBOUND_TOPICS = ("connector.observation.received", "artifact.ingested")


def _err(status: int, code: str, message: str, retryable: bool = False,
         details: dict | None = None, correlation_id: str = ""):
    """openapi `Error`：扁平，`code/message/retryable/correlation_id` 必填。"""
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "retryable": retryable,
                 "correlation_id": correlation_id or "s26",
                 "details": details or {}})


def create_app(*, conn: sqlite3.Connection | None = None,
               db_path: str | None = None, **ports: Callable) -> FastAPI:
    """按端口装配。测试传 fake，生产由引导代码注入真实客户端。"""
    app = FastAPI(title="S26 generation-jobs", version="2.0.0")

    if conn is None:
        conn = sqlite3.connect(db_path or DB_PATH, check_same_thread=False,
                               isolation_level=None)
    conn.executescript(SCHEMA)
    app.state.conn = conn
    app.state.idem = SqliteIdempotencyStore(conn)
    app.state.service = GenerationJobsService(conn, **ports)

    @app.exception_handler(ServiceError)
    async def _service_error(_request, exc: ServiceError):
        return _err(exc.status, exc.code, exc.message, exc.retryable,
                    exc.details)

    @app.exception_handler(CommandConflict)
    async def _conflict(_request, exc: CommandConflict):
        # 同 (tenant, command_id) 不同 body：拒，不覆盖（AGENTS.md）。
        return _err(409, "IDEMPOTENCY_KEY_REUSED", str(exc))

    @app.exception_handler(CommandInProgress)
    async def _in_progress(_request, exc: CommandInProgress):
        return JSONResponse(status_code=429, headers={"Retry-After": "1"},
                            content={"code": "COMMAND_IN_PROGRESS",
                                     "message": str(exc), "retryable": True,
                                     "correlation_id": "s26", "details": {}})

    # ---- 共用校验 --------------------------------------------------------

    def _tenant(x_tenant_id: str) -> str:
        """租户上下文必须由平台签名绑定（AGENTS.md 红线）；缺失即 400。"""
        if not x_tenant_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "X-Tenant-Id is required")
        return x_tenant_id

    def _internal(principal: str) -> None:
        if principal not in INTERNAL_PRINCIPALS:
            raise ServiceError(
                403, "TENANT_SCOPE_DENIED",
                "this endpoint is internal; caller identity is not an "
                "internal principal",
                details={"principal": principal or None})

    def _body(payload: Any, command_id: str, tenant_id: str, *,
              required: tuple[str, ...] = (),
              allowed: tuple[str, ...] | None = None) -> dict:
        """结构校验 + 头/体一致性。

        头缺失在 FastAPI 里会变 422；S26 的错误面只用 400 表示"请求本身
        不成形"，422 留给"请求成形但业务上不可行"（如 `APPROVAL_INCOMPLETE`）。
        """
        if not isinstance(payload, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "body must be a JSON object")
        if not tenant_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
        if not command_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "Idempotency-Key is required")
        missing = [k for k in required if payload.get(k) is None]
        if missing:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "body is missing required fields",
                               details={"missing": missing})
        if allowed is not None:
            unknown = [k for k in payload if k not in allowed]
            if unknown:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "body has unknown fields",
                                   details={"unknown": unknown})
        inside = payload.get("command_id")
        if inside is not None and inside != command_id:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                "body command_id does not match Idempotency-Key",
                details={"body_command_id": inside})
        inside_tenant = payload.get("tenant_id")
        if inside_tenant is not None and inside_tenant != tenant_id:
            # LLM/客户端提供的 tenant_id 不可信；跨租户不是 400，是伪造
            # 租户上下文 → 403（AGENTS.md）。
            raise ServiceError(
                403, "TENANT_SCOPE_DENIED",
                "body tenant_id conflicts with the authenticated tenant",
                details={"body_tenant_id": inside_tenant})
        return {**payload, "command_id": command_id, "tenant_id": tenant_id}

    def _idempotent(idem, tenant_id: str, command_id: str, body: dict, execute):
        """`(tenant_id, command_id)` + `request_hash`；同 hash 原样重放。"""
        request_hash = canonical_request_hash(body)
        record = idem.begin(tenant_id, command_id, request_hash)
        if record.result is not None:
            return json.loads(record.result)
        try:
            result = execute()
        except Exception:
            idem.abort(tenant_id, command_id)
            raise
        idem.finish(tenant_id, command_id,
                    json.dumps(result, ensure_ascii=False, default=str))
        return result

    # ---- 契约路由 --------------------------------------------------------

    @app.post("/generation-jobs", status_code=202)
    def create_generation_job(
            payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        """§12.1。返回 `Operation`（C26-22：剥掉多余的字段）。

        `route_decision_ref` 是契约缺口（C26-14）：§12.1 第 3 条要用，
        但 openapi 的 body 是 `additionalProperties: false` 且没列它。
        """
        body = _body(payload, command_id, x_tenant_id,
                     required=("route_decision_id", "generation_command"),
                     allowed=("route_decision_id", "route_decision_ref",
                              "generation_command", "command_id", "tenant_id"))
        result = _idempotent(
            app.state.idem, x_tenant_id, command_id, body,
            lambda: app.state.service.create_job(
                x_tenant_id, body,
                actor={"principal_id": "api", "principal_type": "service"},
                correlation_id=command_id))
        # `operation_id` 就是 `provider_job_id`（§12.1 末段）。
        return {"operation_id": result["operation_id"], "state": "accepted"}

    @app.get("/admissions/{admission_id}")
    def get_admission(admission_id: str,
                      x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        return app.state.service.get_admission(_tenant(x_tenant_id),
                                               admission_id)

    # ---- §12.2 查询（C26-14：不在 openapi）--------------------------------

    @app.get("/generation-jobs/{provider_job_id}")
    def get_generation_job(
            provider_job_id: str,
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        return app.state.service.get_job(_tenant(x_tenant_id), provider_job_id)

    # ---- §10.9 S27 同步入口（C26-14）--------------------------------------

    @app.post("/dispatch-grants/{dispatch_id}:claim")
    def claim_dispatch_grant(
            dispatch_id: str, payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
            x_principal: str = Header(default="", alias="X-Principal")):
        """付款保护第一原则的入口：没有成功 claim，S27 就不许发 HTTP。

        Idempotency-Key 由 S27 按 `dispatch_id` 生成；同一个 key 重放返回同一份
        派单载荷，**换 key 再来一次**才会撞上 `GATE_ALREADY_CLAIMED`
        —— 这正是"领两次"与"重试同一次领取"的区别。
        """
        _internal(x_principal or "S27")
        body = _body(payload, command_id, x_tenant_id,
                     required=("executor_id", "fencing_token"),
                     allowed=("executor_id", "fencing_token", "request_hash",
                              "command_id", "tenant_id"))
        return _idempotent(
            app.state.idem, x_tenant_id, command_id, body,
            lambda: app.state.service.claim(
                x_tenant_id, dispatch_id, body, correlation_id=command_id))

    @app.post("/dispatch-grants/{dispatch_id}:outcome")
    def report_submit_outcome(
            dispatch_id: str, payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
            x_principal: str = Header(default="", alias="X-Principal")):
        """§7.1：S27 只报 `not_sent | unknown | rejected | accepted`，
        **状态由 S26 定**。"""
        _internal(x_principal or "S27")
        body = _body(payload, command_id, x_tenant_id,
                     required=("outcome",),
                     allowed=("outcome", "provider_job_ref", "error_class",
                              "retry_after_seconds", "command_id", "tenant_id"))
        retry_after = body.get("retry_after_seconds")
        return _idempotent(
            app.state.idem, x_tenant_id, command_id, body,
            lambda: app.state.service.record_submit_outcome(
                x_tenant_id, dispatch_id, body["outcome"],
                provider_job_ref=body.get("provider_job_ref"),
                error_class=body.get("error_class"),
                retry_after_seconds=(None if retry_after is None
                                     else float(retry_after)),
                correlation_id=command_id))

    # ---- 准入 Saga 与收尾（C26-14）----------------------------------------

    @app.post("/generation-jobs/{provider_job_id}:advance")
    def advance(provider_job_id: str, payload: dict | None = None,
                command_id: str = Header(default="", alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                x_principal: str = Header(default="", alias="X-Principal")):
        _internal(x_principal or "S26")
        body = _body(payload or {}, command_id, x_tenant_id,
                     allowed=("command_id", "tenant_id"))
        return _idempotent(
            app.state.idem, x_tenant_id, command_id, body,
            lambda: app.state.service.advance(x_tenant_id, provider_job_id,
                                              correlation_id=command_id))

    @app.post("/generation-jobs/{provider_job_id}:settle")
    def settle(provider_job_id: str, payload: dict | None = None,
               command_id: str = Header(default="", alias="Idempotency-Key"),
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
               x_principal: str = Header(default="", alias="X-Principal")):
        """§4 步骤 7。**取消 ≠ 退款**：结算按实际发生额走。"""
        _internal(x_principal or "S26")
        body = _body(payload or {}, command_id, x_tenant_id,
                     allowed=("actual_micro", "billing_unit", "basis",
                              "command_id", "tenant_id"))
        actual = body.get("actual_micro")
        return _idempotent(
            app.state.idem, x_tenant_id, command_id, body,
            lambda: app.state.service.settle_job(
                x_tenant_id, provider_job_id,
                actual_micro=None if actual is None else int(actual),
                billing_unit=str(body.get("billing_unit") or "request"),
                basis=str(body.get("basis") or "PROVIDER_INVOICE"),
                correlation_id=command_id))

    @app.post("/generation-jobs/{provider_job_id}:cancel")
    def cancel(provider_job_id: str, payload: dict | None = None,
               command_id: str = Header(default="", alias="Idempotency-Key"),
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
               x_principal: str = Header(default="", alias="X-Principal")):
        """§9.2：`cancelled` / `not_cancellable` / `unknown` 三态。"""
        _internal(x_principal or "S21")
        body = _body(payload or {}, command_id, x_tenant_id,
                     allowed=("discard_on_completion", "command_id",
                              "tenant_id"))
        return _idempotent(
            app.state.idem, x_tenant_id, command_id, body,
            lambda: app.state.service.cancel_job(
                x_tenant_id, provider_job_id,
                discard_on_completion=bool(
                    body.get("discard_on_completion")),
                correlation_id=command_id))

    @app.post("/generation-jobs/{provider_job_id}:ingest-gap")
    def ingest_gap(provider_job_id: str, payload: dict,
                   command_id: str = Header(default="", alias="Idempotency-Key"),
                   x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                   x_principal: str = Header(default="", alias="X-Principal")):
        """§8.2 第五步：重试耗尽 → `INGEST_FAILED`，**宁可报缺口**（E23）。"""
        _internal(x_principal or "S28")
        body = _body(payload, command_id, x_tenant_id, required=("reason",),
                     allowed=("reason", "command_id", "tenant_id"))
        return _idempotent(
            app.state.idem, x_tenant_id, command_id, body,
            lambda: app.state.service.report_ingest_gap(
                x_tenant_id, provider_job_id, reason=str(body["reason"]),
                correlation_id=command_id))

    @app.post("/generation-jobs/{provider_job_id}:resubmission-approval")
    def resubmission_approval(
            provider_job_id: str, payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
            x_principal: str = Header(default="", alias="X-Principal")):
        """§7.3B：只**记**批准；重提仍要逐条过 §7.4 的五条。"""
        _internal(x_principal or "ops")
        body = _body(payload, command_id, x_tenant_id,
                     required=("approved_by", "reason", "evidence_ref"),
                     allowed=("approved_by", "reason", "evidence_ref",
                              "command_id", "tenant_id"))
        return _idempotent(
            app.state.idem, x_tenant_id, command_id, body,
            lambda: app.state.service.approve_resubmission(
                x_tenant_id, provider_job_id,
                approved_by=str(body["approved_by"]),
                reason=str(body["reason"]),
                evidence_ref=str(body["evidence_ref"]),
                correlation_id=command_id))

    # ---- 入站事件（§10.4，C26-14）-----------------------------------------

    @app.post("/internal/events/{topic}")
    def ingest_event(topic: str, event: dict,
                     x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                     x_principal: str = Header(default="", alias="X-Principal")):
        """S27 观测与 S28 归档回执的同进程入口。

        事件体的 `tenant_id` 必须与头部一致 —— 否则就是把别人的事件塞进来。
        `artifact.ingested` 没有顶层 `provider_job_id`（C26-18），关联在服务里做。
        """
        _internal(x_principal or "S27")
        tenant_id = _tenant(x_tenant_id)
        if not isinstance(event, dict) or not event.get("id"):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "event envelope needs `id`")
        if event.get("tenant_id") != tenant_id:
            raise ServiceError(
                403, "TENANT_SCOPE_DENIED",
                "event tenant_id conflicts with the authenticated tenant")
        if topic == "connector.observation.received":
            return app.state.service.consume_observation(
                tenant_id, event,
                correlation_id=str(event.get("correlation_id") or ""))
        if topic == "artifact.ingested":
            return app.state.service.on_artifact_ingested(
                tenant_id, event,
                correlation_id=str(event.get("correlation_id") or ""))
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"unknown inbound topic {topic!r}",
                           details={"topic": topic,
                                    "expected": list(INBOUND_TOPICS)})

    # ---- 栅栏与灾备（§9.2 / §9.3，C26-14）---------------------------------

    @app.post("/internal/fence-bumped")
    def fence_bumped(
            payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
            x_principal: str = Header(default="", alias="X-Principal")):
        """`execution_fence` 是 run/branch 级 —— 只碰这一条分支。"""
        _internal(x_principal or "S21")
        body = _body(payload, command_id, x_tenant_id,
                     required=("run_id", "branch_id", "execution_fence"),
                     allowed=("run_id", "branch_id", "execution_fence",
                              "command_id", "tenant_id"))
        return _idempotent(
            app.state.idem, x_tenant_id, command_id, body,
            lambda: app.state.service.on_execution_fence_bumped(
                x_tenant_id, run_id=str(body["run_id"]),
                branch_id=str(body["branch_id"]),
                execution_fence=int(body["execution_fence"]),
                correlation_id=command_id))

    @app.post("/internal/node-epoch-bumped")
    def node_epoch_bumped(
            payload: dict,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
            x_principal: str = Header(default="", alias="X-Principal")):
        """`node_epoch` 是单 `logical_node` 级 —— 其他节点不许动。"""
        _internal(x_principal or "S21")
        body = _body(payload, command_id, x_tenant_id,
                     required=("node_id", "node_epoch"),
                     allowed=("node_id", "node_epoch", "command_id",
                              "tenant_id"))
        return _idempotent(
            app.state.idem, x_tenant_id, command_id, body,
            lambda: app.state.service.on_node_epoch_bumped(
                x_tenant_id, node_id=str(body["node_id"]),
                node_epoch=int(body["node_epoch"]),
                correlation_id=command_id))

    @app.post("/internal/failover-recover")
    def failover_recover(
            payload: dict | None = None,
            command_id: str = Header(default="", alias="Idempotency-Key"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
            x_principal: str = Header(default="", alias="X-Principal")):
        """§9.3：所有 `SUBMITTING` 进 `RECONCILING`，**不重提**。"""
        _internal(x_principal or "ops")
        body = _body(payload or {}, command_id, x_tenant_id,
                     allowed=("command_id", "tenant_id"))
        return _idempotent(
            app.state.idem, x_tenant_id, command_id, body,
            lambda: app.state.service.recover_after_failover(x_tenant_id))

    # ---- §9.1 巡检（C26-14）-----------------------------------------------

    @app.post("/janitor:sweep")
    def janitor_sweep(payload: dict | None = None,
                      command_id: str = Header(default="", alias="Idempotency-Key"),
                      x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                      x_principal: str = Header(default="", alias="X-Principal")):
        """八条巡检里**能一次跑完的七条**；`fence_mismatch` 是实时的，不在这里。

        租户集合取自 S26 自己的 `provider_job`（有 job 的租户才需要扫）——
        不是第二份租户目录，也没有跨库读写。
        """
        _internal(x_principal or "janitor")
        tenant_id = x_tenant_id
        if not tenant_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
        if not command_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "Idempotency-Key is required")
        if payload is not None and not isinstance(payload, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "body must be a JSON object")
        svc = app.state.service
        body = {**(payload or {}), "command_id": command_id,
                "tenant_id": tenant_id}

        def run():
            limit = int(body.get("limit") or 100)
            return {
                "due_jobs": svc.scan_due_jobs(tenant_id, limit=limit),
                "stuck_submitting": svc.scan_stuck_submitting(tenant_id),
                "unknown_submissions": svc.scan_unknown_submissions(
                    tenant_id, limit=limit),
                "expired_grants": svc.scan_expired_grants(tenant_id),
                "ingest_ttl_warning": svc.scan_ingest_ttl_warning(tenant_id),
                "ingest_backlog": svc.scan_ingest_backlog(tenant_id),
                "orphan_intents": svc.scan_orphan_intents(tenant_id),
                "reservation_leak_check": svc.scan_reservation_leak(tenant_id),
            }

        return _idempotent(app.state.idem, tenant_id, command_id, body, run)

    # ---- 运维 ------------------------------------------------------------

    @app.get("/health/readiness")
    def readiness():
        with closing(app.state.conn.execute("SELECT 1")) as cur:
            cur.fetchone()
        return {"status": "ready"}

    return app


# 模块级实例供 `uvicorn generation_jobs.api.app:app`。端口未接线时用到就
# 返回 503 `DEPENDENCY_UNAVAILABLE`，这是契约行为，不是崩溃。
app = create_app()
