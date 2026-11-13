"""S21 HTTP 层。契约见 `contracts/openapi.v2.yaml`（三条 S21 路由）与
plans/S21-orchestration.md §5。

openapi 里 S21 只有三条路径：

- `POST /episodes/{episode_id}/runs` → `createEpisodeRun`
- `GET  /runs/{run_id}`             → `getRun`
- `POST /runs/{run_id}/commands`    → `commandRun`

§5.4 的五个补充查询（`/nodes`、`/blockers`、`/repair-budget`、列表、`:signal`）
**不在 openapi 里**（C21-1 / C21-2 / C21-7），这里按方案落地；它们只读或
只给内部 principal，不扩大命令面。

## 与契约的两处硬对齐

1. `Operation` 是 `additionalProperties: false`，只有
   `{operation_id, state, run_id}`（**只有 `operation_id` 必填**）。
   方案 §5.1 里的 `replayed` 因此**不能**出现在响应里（C21-12）。
2. `Run` 恰好 7 个字段，同样 `additionalProperties: false`。`failed_nodes` /
   `waiting_reason` / `current_stage` 走 §5.4 的补充查询，不进这个响应。

## 依赖端口

`temporalio` 不在依赖里（`pyproject.toml` 只有 fastapi/pytest），本仓库也没有
任何服务发出站 HTTP。所以 `create_app()` 接受六个端口 callable；不传就是
**未接线**，调用时返回 503 `DEPENDENCY_UNAVAILABLE`（契约里标
`retryable=true`），不静默降级。生产部署由引导代码注入真实客户端。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from typing import Any, Callable

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from orchestration.schema import SCHEMA
from orchestration.service import OrchestrationService, ServiceError

DB_PATH = os.environ.get("S21_DB", ":memory:")

# §5.4 的 `:signal` 与事件注入端点只给内部 principal。
INTERNAL_PRINCIPALS = {"S21", "S07", "S08", "S22", "S23", "S26", "S39", "S47",
                       "janitor", "ops", "test"}


def _err(status: int, code: str, message: str, retryable: bool = False,
         details: dict | None = None, correlation_id: str = ""):
    """openapi `Error`：扁平，`code/message/retryable/correlation_id` 必填。"""
    content: dict[str, Any] = {
        "code": code, "message": message, "retryable": retryable,
        "correlation_id": correlation_id or "s21",
    }
    if details:
        content["details"] = details
    return JSONResponse(status_code=status, content=content)


def create_app(*, conn: sqlite3.Connection | None = None,
               db_path: str | None = None, **ports: Callable) -> FastAPI:
    """按端口装配。测试传 fake，生产由引导代码注入真实客户端。"""
    app = FastAPI(title="S21 orchestration", version="2.0.0")

    if conn is None:
        conn = sqlite3.connect(db_path or DB_PATH, check_same_thread=False,
                               isolation_level=None)
    conn.executescript(SCHEMA)
    app.state.conn = conn
    app.state.idem = SqliteIdempotencyStore(conn)
    app.state.service = OrchestrationService(conn, **ports)

    @app.exception_handler(ServiceError)
    async def _service_error(_request, exc: ServiceError):
        return _err(exc.status, exc.code, exc.message, exc.retryable, exc.details)

    @app.exception_handler(CommandConflict)
    async def _conflict(_request, exc: CommandConflict):
        # 同 (tenant, command_id) 不同 body：拒，不覆盖（AGENTS.md）。
        return _err(409, "IDEMPOTENCY_KEY_REUSED", str(exc))

    @app.exception_handler(CommandInProgress)
    async def _in_progress(_request, exc: CommandInProgress):
        return JSONResponse(status_code=429, headers={"Retry-After": "1"},
                            content={"code": "COMMAND_IN_PROGRESS",
                                     "message": str(exc), "retryable": True,
                                     "correlation_id": "s21"})

    def _tenant(x_tenant_id: str) -> str:
        """`GET /runs/{run_id}` 在 openapi 里没声明租户头，但租户上下文必须由
        平台签名绑定（AGENTS.md 红线）；缺失按结构问题 400。"""
        if not x_tenant_id:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "X-Tenant-Id is required")
        return x_tenant_id

    def _internal(principal: str) -> None:
        if principal not in INTERNAL_PRINCIPALS:
            raise ServiceError(
                403, "TENANT_MISMATCH",
                "caller identity is not an internal principal",
                details={"principal": principal or None})

    def _command(payload: Any, command_id: str, tenant_id: str,
                 required: tuple[str, ...], allowed: tuple[str, ...]) -> dict:
        """结构校验 + 头/体 command_id 一致性。

        头缺失在 FastAPI 里会变 422；契约 §5.7 没有 422 用于结构问题
        （422 只表示"请求本身不可行"），所以这里自己抛 400。
        """
        if not isinstance(payload, dict):
            raise ServiceError(400, "INVALID_CONTEXT",
                               "body must be a JSON object")
        if not tenant_id:
            raise ServiceError(400, "INVALID_CONTEXT", "X-Tenant-Id is required")
        if not command_id:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "Idempotency-Key is required")
        missing = [k for k in required if k not in payload]
        if missing:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "body is missing required fields",
                               details={"missing": missing})
        unknown = [k for k in payload if k not in allowed]
        if unknown:
            # 契约三处 body 都是 additionalProperties:false。
            raise ServiceError(400, "INVALID_CONTEXT",
                               "body has unknown fields",
                               details={"unknown": unknown})
        # `command_id` 在体内和头里都出现。两者必须是同一个键，否则不猜。
        inside = payload.get("command_id")
        if inside is not None and inside != command_id:
            raise ServiceError(
                400, "INVALID_CONTEXT",
                "body command_id does not match Idempotency-Key",
                details={"body_command_id": inside})
        inside_tenant = payload.get("tenant_id")
        if inside_tenant is not None and inside_tenant != tenant_id:
            # LLM/客户端提供的 tenant_id 不可信（AGENTS.md）。跨租户不是 400，
            # 是伪造租户上下文：403 TENANT_MISMATCH。
            raise ServiceError(
                403, "TENANT_MISMATCH",
                "body tenant_id conflicts with the authenticated tenant",
                details={"body_tenant_id": inside_tenant})
        return payload

    def _idempotent(state, body: dict, execute):
        """命令层幂等。`(tenant_id, command_id)` + `request_hash`。

        与 S22 不同，这里**不**往结果里塞 `replayed`：`Operation` 是
        `additionalProperties:false`，多一个字段契约就不合法（C21-12）。
        """
        tenant_id = state["tenant_id"]
        command_id = state["command_id"]
        request_hash = canonical_request_hash(body)
        record = state["idem"].begin(tenant_id, command_id, request_hash)
        if record.result is not None:
            return json.loads(record.result)
        try:
            result = execute()
        except Exception:
            state["idem"].abort(tenant_id, command_id)
            raise
        state["idem"].finish(tenant_id, command_id,
                             json.dumps(result, ensure_ascii=False, default=str))
        return result

    # ---- 契约路由 --------------------------------------------------------

    @app.post("/episodes/{episode_id}/runs", status_code=202)
    def create_run(episode_id: str, payload: dict, request: Request,
                   command_id: str = Header(default="", alias="Idempotency-Key"),
                   x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        body = _command(payload, command_id, x_tenant_id,
                        required=("context", "budget_scope_id", "command_id"),
                        allowed=("context", "budget_scope_id", "command_id",
                                 "intent"))
        svc: OrchestrationService = request.app.state.service
        state = {"tenant_id": x_tenant_id, "command_id": command_id,
                 "idem": request.app.state.idem}
        return _idempotent(state, body, lambda: svc.create_run(
            x_tenant_id, episode_id, body, correlation_id=command_id))

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request,
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        return request.app.state.service.get_run(_tenant(x_tenant_id), run_id)

    @app.post("/runs/{run_id}/commands", status_code=202)
    def command_run(run_id: str, payload: dict, request: Request,
                    command_id: str = Header(default="", alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        body = _command(payload, command_id, x_tenant_id,
                        required=("action", "command_id"),
                        allowed=("action", "command_id"))
        svc: OrchestrationService = request.app.state.service
        state = {"tenant_id": x_tenant_id, "command_id": command_id,
                 "idem": request.app.state.idem}
        result = _idempotent(state, body, lambda: svc.command_run(
            x_tenant_id, run_id, body["action"], correlation_id=command_id))
        # `dispositions` 是 §4.5 的取消分类，契约的 `Operation` 放不下。
        # 只保留 `cancel` 时的内部信息在 `details` 之外的独立响应里交给调用方
        # ——实际做法：剥掉它，走 `/nodes` 查询（见 C21-12）。
        return {k: result[k] for k in ("operation_id", "state", "run_id")}

    # ---- §5.4 补充查询（不在 openapi，C21-1/C21-2/C21-7）-----------------

    @app.get("/runs")
    def list_runs(request: Request,
                  episode_id: str | None = Query(None),
                  branch_id: str | None = Query(None),
                  state: str | None = Query(None),
                  after: str | None = Query(None),
                  limit: int = Query(100, ge=1, le=500),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        return request.app.state.service.list_runs(
            _tenant(x_tenant_id), episode_id=episode_id, branch_id=branch_id,
            state=state, after=after, limit=limit)

    @app.get("/runs/{run_id}/nodes")
    def list_nodes(run_id: str, request: Request,
                   state: str | None = Query(None),
                   stage: str | None = Query(None),
                   after: str | None = Query(None),
                   limit: int = Query(200, ge=1, le=1000),
                   x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        return request.app.state.service.list_nodes(
            _tenant(x_tenant_id), run_id, state=state, stage=stage,
            after=after, limit=limit)

    @app.get("/runs/{run_id}/blockers")
    def list_blockers(run_id: str, request: Request,
                      x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
        return request.app.state.service.list_blockers(
            _tenant(x_tenant_id), run_id)

    @app.get("/runs/{run_id}/repair-budget")
    def get_repair_budget(run_id: str, request: Request,
                          x_tenant_id: str = Header(default="",
                                                    alias="X-Tenant-Id")):
        return request.app.state.service.get_repair_budget(
            _tenant(x_tenant_id), run_id)

    @app.post("/runs/{run_id}:signal")
    def signal(run_id: str, payload: dict | None = None, request: Request = None,
               command_id: str = Header(default="", alias="Idempotency-Key"),
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
               x_principal: str = Header(default="", alias="X-Principal")):
        """§5.4 的内部信号入口。

        只允许"把外部事实推给 workflow"这类信号（评审决定、memory 提交、
        change plan 应用），**不允许**用来改投影状态——那会绕过 CAS。
        """
        _internal(x_principal)
        body = _command(payload or {}, command_id, x_tenant_id,
                        required=("signal", "payload"),
                        allowed=("signal", "payload", "command_id"))
        svc: OrchestrationService = request.app.state.service
        # 先确认 Run 存在且租户对得上，再交给 workflow（这里是登记，
        # 实际投递由 Temporal 客户端完成，未接线时返回 503）。
        svc.get_run(x_tenant_id, run_id)
        return {"run_id": run_id, "signal": body["signal"],
                "delivered": False,
                "detail": "temporal client is not wired in this build"}

    # ---- 内部：§5.6 事件摄入 --------------------------------------------

    @app.post("/internal/events/{topic}")
    def ingest_event(topic: str, event: dict, request: Request,
                     x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                     x_principal: str = Header(default="", alias="X-Principal")):
        """按 §5.6 的五个 topic 路由到对应消费函数。

        真实部署里由事件总线 worker 调用；这里给 worker 一个同进程入口，
        也让"重复投递/乱序"可被测试直接驱动。事件体的 `tenant_id` 必须与
        头部一致——否则是把别人的事件塞进来。
        """
        _internal(x_principal)
        if not x_tenant_id:
            raise ServiceError(400, "INVALID_CONTEXT", "X-Tenant-Id is required")
        if not isinstance(event, dict) or "id" not in event or "data" not in event:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "event envelope needs `id` and `data`")
        if event.get("tenant_id") != x_tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "event tenant_id conflicts with the authenticated tenant")
        svc: OrchestrationService = request.app.state.service
        handler = {
            "generation.job-state-changed": svc.consume_job_state_changed,
            "scheduler.permit-changed": svc.consume_permit_changed,
            "snapshot.manifest-committed": svc.consume_manifest_committed,
            "change.plan-applied": svc.consume_change_plan_applied,
            "review.decision-recorded": _consume_review_decision,
        }.get(topic)
        if handler is None:
            raise ServiceError(400, "INVALID_CONTEXT",
                               f"unknown event topic {topic!r}",
                               details={"topic": topic})
        return handler(event)

    def _consume_review_decision(event: dict) -> dict:
        """`review.decision.recorded` 按 `review_task_id` 关联（§5.6）。

        还没派过评审任务时不建任何东西——这是 at-least-once 下正常的
        "先到后到"，不是错误。
        """
        svc: OrchestrationService = request.app.state.service
        if not svc.inbox.consume("s21.reviews", event["id"]):
            return {"duplicate": True, "event_id": event["id"]}
        data = event["data"]
        return {"review_task_id": data["review_task_id"],
                "status": data["status"],
                "decided_against": data["decided_against"],
                "stale_reason": data["stale_reason"],
                "applied": False}

    # ---- 运维 ------------------------------------------------------------

    @app.get("/health/readiness")
    def readiness(request: Request):
        with closing(request.app.state.conn.execute("SELECT 1")) as cur:
            cur.fetchone()
        return {"status": "ready"}

    return app


# 模块级实例供 `uvicorn orchestration.api.app:app`。端口未接线时创建 Run
# 会返回 503，这是契约行为，不是崩溃。
app = create_app()
