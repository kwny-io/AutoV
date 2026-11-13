"""S39 HTTP 层。

方案 §8 定义的四条入口 + 事件投递：

| 方法 | 路径 | 语义 |
|---|---|---|
| POST | `/repair-plans` | 出计划（202；`dry_run` 只预览） |
| GET | `/repair-plans/{id}` | 计划全文 + 已消耗计数 |
| GET | `/repair-plans?shot_lineage_id=` | 跨 Run 的尝试历史（E26 取证） |
| POST | `/repair-plans/{id}:escalate` | 强制升级（幂等） |
| POST | `/repair-plans/{id}:recheck` | 投递复检结论（C18） |
| POST | `/events:consume` | `generation.job.state-changed` / `review.decision.recorded` |

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id`（顶层或
`execution_context.tenant_id`）**只用于比对**，不一致即 403。AGENTS.md：
平台签名之外没有可信 tenant。

五个实现细节：

1. **`request_hash` 不含 trace / 时间戳。** 同一个请求重投时 `trace_id` 变了，
   裸 `canonical_request_hash` 会算出不同摘要，然后把一次合法重投判成"同键
   不同 body"。用 `stable_request_hash`（与 S37/S41 同一份易变键清单）。
2. **幂等占位与结果 UPDATE 在事务之外。** SDK 的 `begin()` 先落占位、
   `finish()` 只 UPDATE；执行失败 `abort()` 删掉占位，重投仍能成功。
   `abort()` 本身写不进去时吞掉它的 `OperationalError`，不盖住真正的错误。
3. **幂等写库失败映射成 503。** 内核 `Transaction` 只映射它自己的
   `BEGIN IMMEDIATE`；占位与落结果在事务之外。不映射就是未捕获异常 → 500，
   而"写不进去"不等于"请求有错"。
4. **注册表在导入期加载。** 不合格的注册表 / 动作族直接让进程起不来
   （`RegistryError` 上抛），不是记一条日志继续跑：一条错登记会让 S39 花错
   钱、改坏已验收内容。
5. **两个开关默认关闭**（`REPAIR_PLANNING_ENABLED` 与 registry 路径）。
   关着时命令 503，**绝不出空计划**：一份没有上限、没有路由的计划比没有
   计划更危险。

本层不执行修复、不提交付费任务、不推进任何接受指针。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
)

from repair_planning import registry as registry_mod
from repair_planning.deps import (
    build_capability_source,
    build_continuity_source,
    build_policy_source,
    build_report_source,
    build_run_source,
)
from repair_planning.schema import CONTRACT_GAPS
from repair_planning.service import (
    SERVICE_LAYER,
    RepairPlanningService,
    ServiceError,
    stable_request_hash,
)

DB_PATH = os.environ.get("S39_DB", ":memory:")

# 仓库根：api/app.py → api → repair-planning → services → 根
ROOT = Path(__file__).resolve().parents[3]


def _contract_path(env_name: str, relative: str) -> str | None:
    """环境变量优先；否则取仓库里的契约文件（存在才算数）。"""
    configured = os.environ.get(env_name)
    if configured:
        return configured
    candidate = ROOT / relative
    return str(candidate) if candidate.is_file() else None


def _load_json(path: str | None) -> dict | None:
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


app = FastAPI(title="S39 repair-planning", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

# 导入期加载：不合格即上抛，进程不启动（`registry.RegistryError`）。
_registry = registry_mod.load_registry(
    _contract_path("CHECK_REGISTRY_PATH", "contracts/quality/check-registry.v2.json"),
    _contract_path("ACTION_FAMILIES_PATH",
                   "contracts/quality/action-families.v2.json"))

service = RepairPlanningService(
    _conn,
    policy_source=build_policy_source(os.environ.get("S02_BASE_URL")),
    report_sources={
        service_id: build_report_source(
            os.environ.get(f"{service_id}_BASE_URL"), service_id)
        for service_id in ("S35", "S36", "S37", "S38")},
    capability_source=build_capability_source(
        os.environ.get("S24_BASE_URL") or os.environ.get("S25_BASE_URL")),
    continuity_source=build_continuity_source(os.environ.get("S17_BASE_URL")),
    run_source=build_run_source(os.environ.get("S21_BASE_URL")),
    registry=_registry,
    qc_report_schema=_load_json(_contract_path(
        "QC_REPORT_SCHEMA_PATH", "contracts/quality/qc-report.v2.schema.json")),
    execution_context_schema=_load_json(_contract_path(
        "EXECUTION_CONTEXT_SCHEMA_PATH",
        "contracts/execution-context.v2.schema.json")),
    enabled=os.environ.get("REPAIR_PLANNING_ENABLED", "false").lower() == "true",
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
    return _err(409, exc.code, str(exc), False,
                {"stored_request_hash": exc.stored_hash,
                 "incoming_request_hash": exc.incoming_hash})


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(
        status_code=409, headers={"Retry-After": "1"},
        content={"code": "COMMAND_IN_FLIGHT", "message": str(exc),
                 "retryable": True, "correlation_id": "", "details": {}})


def _body(payload, command_id: str | None, tenant_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    inside = payload.get("command_id")
    if inside is not None and command_id and inside != command_id:
        raise ServiceError(400, "INVALID_BODY",
                           "Idempotency-Key does not match body command_id")
    # 两处都查：命令体的租户在 `execution_context` 里；漏掉它就等于相信了
    # 请求体（AGENTS.md：平台签名之外没有可信 tenant）。
    context = payload.get("execution_context")
    for candidate in (payload.get("tenant_id"),
                      context.get("tenant_id") if isinstance(context, dict)
                      else None):
        if candidate is not None and candidate != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放**不重新出计划**：第一次的计划已落库并发过事件；再算一遍可能得到
    另一批 target_id，而 S21 已经拿第一份去派单了。
    """
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
            try:
                _idem.abort(tenant_id, command_id)
            except sqlite3.OperationalError:
                pass
            raise
        _idem.finish(tenant_id, command_id,
                     json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}
    except sqlite3.OperationalError as exc:
        raise ServiceError(
            503, "REPAIR_PLANNING_UNAVAILABLE",
            f"repair-planning database is not writable: {exc}",
            retryable=True) from exc


def _tenant(tenant_id: str) -> str:
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return tenant_id


# ------------------------------------------------------------------ 命令

@app.post("/repair-plans", status_code=202)
def create_plan(payload: dict,
                command_id: str = Header(alias="Idempotency-Key"),
                x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """202 = "已接受出计划"，**不表示可执行**；能否派单看 `state`。

    `dry_run=true` 只做归因/打分/上限检查：返回计划但**不发布事件**、
    **不预留计数**（`_authorize` 里的 dry_run 分支）。
    """
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.create_plan(
                       x_tenant_id, command_id, body, request_hash))


@app.post("/repair-plans/{plan_id}:escalate")
def escalate_plan(plan_id: str,
                  payload: dict | None = None,
                  command_id: str | None = Header(default=None,
                                                  alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """人工/系统强制升级。**幂等**：第二次调用 `changed=false`，不重发事件。"""
    tenant = _tenant(x_tenant_id)
    body = payload if isinstance(payload, dict) else {}
    body = _body(body, command_id, tenant)
    reason = str(body.get("reason") or "manual_escalation")
    return service.escalate(tenant, plan_id, reason)


@app.post("/repair-plans/{plan_id}:recheck")
def submit_recheck(plan_id: str,
                   payload: dict,
                   command_id: str | None = Header(default=None,
                                                   alias="Idempotency-Key"),
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """复检结论投递（C18：没有"这是复检"的事件，所以给一个显式入口）。

    复检**不是**修复失败，也不是放行：`pass` 只把计划标 `resolved` 并交出
    "候选可接受"，接受指针仍在 S08。
    """
    tenant = _tenant(x_tenant_id)
    body = _body(payload, command_id, tenant)
    if not command_id:
        return service.submit_recheck(tenant, plan_id, body)
    return command(tenant, body, command_id,
                   lambda _hash: service.submit_recheck(tenant, plan_id, body))


# ------------------------------------------------------------------ 查询

@app.get("/repair-plans")
def list_plans(shot_lineage_id: str = Query(default=""),
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.list_plans(_tenant(x_tenant_id), shot_lineage_id)


@app.get("/repair-plans/{plan_id}")
def get_plan(plan_id: str,
             x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_plan(_tenant(x_tenant_id), plan_id)


# ------------------------------------------------------------------ 事件

_CONSUMERS = {
    "generation.job.state-changed": "consume_generation_event",
    "review.decision.recorded": "consume_review_decision",
}


@app.post("/events:consume")
def consume_event(payload: dict,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    """`generation.job.state-changed` / `review.decision.recorded` 的投递入口。

    inbox 自己就是幂等（D5）。两种事件都**不改接受状态**：
    `generation` 只记账，`review.decision.recorded` 只把计划标
    `human_resolved`——`recorded` ≠ `accepted`，推进接受指针是 S08 的 CAS。

    `quality.*.completed` 这类 ingest 事件**不在这里消费**：方案 §9 的订阅表
    没有给出「事件 → `qc_snapshot`」的映射（报告读面不统一，见 C20/C21），
    出计划走 `POST /repair-plans` 的显式快照。伪装成"已 ingest"会让一份迟到
    的报告悄悄改掉计划，所以这里明确 422。
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
    event_type = str(envelope.get("type") or envelope.get("event_type") or "")
    consumer = _CONSUMERS.get(event_type)
    if consumer is None:
        raise ServiceError(
            422, "UNSUPPORTED_EVENT_TYPE",
            f"S39 consumes {sorted(_CONSUMERS)}; {event_type!r} is not one of "
            f"them. QC ingest is command-driven (see CONTRACT_GAPS C27)",
            details={"event_type": event_type})
    return getattr(service, consumer)(x_tenant_id, envelope)


@app.get("/health/readiness")
def readiness():
    """`ready` 是"现在能不能出计划"，不是"库连不连得上"。

    默认状态下 `contracts/quality/check-registry.v2.json` 不存在（C5，
    见 `delivery/acceptance/check-registry-missing-fields-2026-11-05.md`：
    99 条 check 没有一条能机械提取 `window_scope`），所以这里报
    `ready: false` 并给出原因——把"起得来但一律 503"报成 ready 会让
    编排层等到第一次派单才发现。
    """
    reachable = _conn.execute("SELECT 1").fetchone() is not None
    blockers = []
    if not reachable:
        blockers.append("DATABASE_UNREACHABLE")
    if not service.enabled:
        blockers.append("REPAIR_PLANNING_DISABLED")
    if _registry is None:
        blockers.append("CHECK_REGISTRY_UNAVAILABLE")
    if service.qc_report_schema is None:
        blockers.append("QC_REPORT_SCHEMA_UNAVAILABLE")
    if service.execution_context_schema is None:
        blockers.append("EXECUTION_CONTEXT_SCHEMA_UNAVAILABLE")
    # 上游没配就有一道门判不了，命令整体 503。少了这一段，开 registry 但
    # 没配 S02 之类时会报 ready 而每条命令都 503。
    blockers.extend(service.unavailable_dependencies())
    return {"ready": not blockers,
            "enabled": service.enabled,
            "blockers": blockers,
            "registry_version": (_registry.version if _registry is not None
                                 else None),
            "report_layers": sorted(SERVICE_LAYER),
            "contract_gaps": CONTRACT_GAPS}
