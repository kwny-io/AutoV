"""S44 HTTP 层。

`contracts/openapi.v2.yaml` 与 `service.yaml` 只声明两个面：命令
`POST /evaluation-runs`、查询 `GET /metrics`。其余路由来自方案 §6.2 的命令表
（cohort / snapshot / blind-review / experiment）与 §6.5 的导出。本层按方案
实现，**不改生成物**。

租户一律来自平台签名的 `X-Tenant-Id`。请求体里的 `tenant_id`（顶层、
`context.tenant_id`、`execution_context.tenant_id`）**只用于比对**，不一致
即 403 —— AGENTS.md 禁止把请求体提供的 `tenant_id` 当可信输入。

六个实现细节：

1. **`Idempotency-Key` 必须等于 body 的 `command_id`。** 不相等就是 400，
   不是「以 header 为准」；否则调用方以为自己在重投，其实换了一个键。
2. **幂等摘要用 `stable_request_hash`。** 重投时 trace_id / 时间戳变了不该
   被算成「同键不同 body」（那个 409 只留给真正的不同 body）。
3. **占位与落结果在事务之外。** `begin()` 先落占位、`finish()` 只 UPDATE；
   内核事务回滚时 `abort()` 删掉占位，重投仍能成功。`abort()` 自己写不进去
   时吞掉它的 `OperationalError`，不盖住真正的错误。
4. **评测执行开关默认关闭。** `EVALUATION_EXECUTION_ENABLED` 没开时
   `POST /evaluation-runs` 是 503 `EVALUATION_EXECUTION_DISABLED`，**没有**
   「假装跑过了」的回退路径（AGENTS.md 的停止条件：付费生成要显式授权）。
5. **上游默认是 503 桩。** deps 的 `build_*(None)` 返回必然抛
   `ServiceError(503, "..._UNAVAILABLE")` 的桩；本层不提供任何降级实现。
   这是「不编数字」的落点：读不到就是 503，不是 0，也不是上次的值。
6. **`/health/readiness` 不是 `SELECT 1`。** 它把每个桩自报的
   `_unavailable_code` 收上来，如实报 `ready: false` 与缺哪些源。

`GET /metrics` 的响应形状按方案 §6.1 的提案（`data_as_of` + `staleness`），
而不是 openapi 里那个只有 `projection_watermark` 的贫瘠版本（S44-2/S44-6）。
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

from evaluation_analytics.deps import (
    build_accepted_source,
    build_cost_source,
    build_delivery_source,
    build_evaluator_source,
    build_job_source,
    build_qc_source,
    build_review_source,
    validate_execution_context,
)
from evaluation_analytics.schema import CONTRACT_GAPS
from evaluation_analytics.service import (
    EvaluationAnalyticsService,
    ServiceError,
    stable_request_hash,
)

DB_PATH = os.environ.get("S44_DB", ":memory:")

app = FastAPI(title="S44 evaluation-analytics", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row


def _qc_base_urls() -> dict:
    return {sid: os.environ.get(f"{sid}_BASE_URL")
            for sid in ("S35", "S36", "S37")}


service = EvaluationAnalyticsService(
    _conn,
    cost_source=build_cost_source(os.environ.get("S23_BASE_URL")),
    qc_source=build_qc_source(_qc_base_urls()),
    review_source=build_review_source(os.environ.get("S40_BASE_URL")),
    delivery_source=build_delivery_source(os.environ.get("S43_BASE_URL")),
    job_source=build_job_source(os.environ.get("S26_BASE_URL")),
    accepted_source=build_accepted_source(os.environ.get("S08_BASE_URL")),
    evaluator_source=build_evaluator_source(os.environ.get("EVALUATOR_BASE_URL")),
    # 平台级评测执行开关，默认关闭（AGENTS.md：付费生成要显式授权）。
    evaluation_execution_enabled=(
        os.environ.get("EVALUATION_EXECUTION_ENABLED", "false").lower() == "true"),
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
    """库不可写时命令状态**不可知**：报 409 会让调用方以为「已经做过了」，
    报 500 会让它以为「肯定没做」。两者都可能造成重复付费评测，按 5xx 可重试。"""
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
        # 契约把 command_id 放在 body 里。缺了就用 header 补，但不能反过来：
        # header 与 body 不一致是错误，不是「以 header 为准」。
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

    重放**不重新执行**：第一次已经算了快照/开了评测/发了事件；再跑一遍会
    产生第二个快照或第二次付费评测，而 `(tenant, command_id)` 的语义是
    「这件事只发生一次」（方案 §7）。
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


def _context_of(body: dict) -> dict | None:
    """调用方若带了 `execution_context`，就按契约校验；没带则不凭空要求。"""
    context = body.get("execution_context")
    if context is None:
        return None
    validate_execution_context(context)
    return context


# ------------------------------------------------------------------ 命令

@app.post("/cohorts")
def freeze_cohort(payload: dict,
                  command_id: str = Header(default="", alias="Idempotency-Key"),
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """冻结 cohort（§6.2）。冻结后成员集合不再变；改定义要新 cohort_id。"""
    body = _body(payload, command_id, x_tenant_id)
    _context_of(body)
    return _json(201, command(
        x_tenant_id, body,
        lambda request_hash: service.freeze_cohort(
            x_tenant_id, command_id, body, request_hash)))


@app.post("/metric-snapshots")
def compute_snapshot(payload: dict,
                     command_id: str = Header(default="",
                                              alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    """计算快照（§6.2）。同天然键重算是重放；上游不可达是 503，不是 0。"""
    body = _body(payload, command_id, x_tenant_id)
    _context_of(body)
    result = command(x_tenant_id, body,
                     lambda request_hash: service.compute_snapshot(
                         x_tenant_id, command_id, body, request_hash))
    return _json(200, result)


@app.post("/evaluation-runs")
def create_evaluation_run(payload: dict,
                          command_id: str = Header(default="",
                                                   alias="Idempotency-Key"),
                          x_tenant_id: str = Header(default="",
                                                    alias="X-Tenant-Id")):
    """启动评测（契约面）。

    201 = 已受理进队列；**202 不在这里出现** —— 评测要么被受理（有
    evaluation_run 记录），要么因为开关没开/评测器没配而 503。重复启动
    返回同一条 run（`replayed: true`），不会再排一次付费。
    """
    body = _body(payload, command_id, x_tenant_id)
    _context_of(body)
    return _json(201, command(
        x_tenant_id, body,
        lambda request_hash: service.create_evaluation_run(
            x_tenant_id, command_id, body, request_hash)))


@app.post("/blind-reviews")
def create_blind_review(payload: dict,
                        command_id: str = Header(default="",
                                                 alias="Idempotency-Key"),
                        x_tenant_id: str = Header(default="",
                                                  alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return _json(201, command(
        x_tenant_id, body,
        lambda request_hash: service.create_blind_review(
            x_tenant_id, command_id, body, request_hash)))


@app.post("/blind-reviews/{review_id}/votes")
def cast_votes(review_id: str, payload: dict,
               command_id: str = Header(default="", alias="Idempotency-Key"),
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """批量投票会被去重（`(reviewer,item,dimension)` 主键），最后一次覆盖。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda request_hash: service.vote(
                       x_tenant_id, command_id, review_id, body, request_hash))


@app.post("/blind-reviews/{review_id}:close")
def close_blind_review(review_id: str, payload: dict | None = None,
                       command_id: str = Header(default="",
                                                alias="Idempotency-Key"),
                       x_tenant_id: str = Header(default="",
                                                 alias="X-Tenant-Id")):
    """关评并算 alpha。`α < 0.667` 或样本不足时**不给校准结论**（§4.6）。"""
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda request_hash: service.close_blind_review(
                       x_tenant_id, command_id, review_id, body, request_hash))


@app.post("/experiments")
def create_experiment(payload: dict,
                      command_id: str = Header(default="",
                                               alias="Idempotency-Key"),
                      x_tenant_id: str = Header(default="",
                                                alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return _json(201, command(
        x_tenant_id, body,
        lambda request_hash: service.create_experiment(
            x_tenant_id, command_id, body, request_hash)))


@app.post("/experiments/{experiment_id}:start")
def start_experiment(experiment_id: str, payload: dict | None = None,
                     command_id: str = Header(default="",
                                              alias="Idempotency-Key"),
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda request_hash: service.start_experiment(
                       x_tenant_id, command_id, experiment_id, body,
                       request_hash))


@app.post("/experiments/{experiment_id}:stop")
def stop_experiment(experiment_id: str, payload: dict | None = None,
                    command_id: str = Header(default="",
                                             alias="Idempotency-Key"),
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id")):
    """止损。STOPPED 保留全部数据；已经停过的不重写时间戳（幂等）。"""
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda request_hash: service.stop_experiment(
                       x_tenant_id, command_id, experiment_id, body,
                       request_hash))


@app.post("/datasets")
def put_dataset(payload: dict,
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """数据集版本写一次就冻结。不是命令语义（没有副作用窗口），不走幂等表。"""
    tenant = _tenant(x_tenant_id)
    body = payload if isinstance(payload, dict) else None
    if body is None:
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    if body.get("tenant_id") is not None and body["tenant_id"] != tenant:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return _json(201, service.put_dataset(tenant, body))


@app.post("/allocation-policies")
def put_allocation_policy(payload: dict,
                          x_tenant_id: str = Header(default="",
                                                    alias="X-Tenant-Id")):
    """分摊策略。同版本不可改写：旧快照必须仍然可解释（§2.1 规则 2）。"""
    tenant = _tenant(x_tenant_id)
    body = payload if isinstance(payload, dict) else None
    if body is None:
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    if body.get("tenant_id") is not None and body["tenant_id"] != tenant:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return _json(201, service.put_allocation_policy(tenant, body))


@app.post("/cost-projections")
def compute_projection(payload: dict,
                       command_id: str = Header(default="",
                                                alias="Idempotency-Key"),
                       x_tenant_id: str = Header(default="",
                                                 alias="X-Tenant-Id")):
    """成本预测。区间必给、窗口必标；**不能用于任何放行判定**（§2.1）。"""
    body = _body(payload, command_id, x_tenant_id)
    return _json(201, command(
        x_tenant_id, body,
        lambda request_hash: service.compute_projection(
            x_tenant_id, command_id, body, request_hash)))


@app.post("/customer-roi")
def compute_roi(payload: dict,
                command_id: str = Header(default="", alias="Idempotency-Key"),
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """客户 ROI。收入是**外部输入**，必须显式给出（S44-1）。"""
    body = _body(payload, command_id, x_tenant_id)
    return _json(201, command(
        x_tenant_id, body,
        lambda request_hash: service.compute_roi(
            x_tenant_id, command_id, body, request_hash)))


# ------------------------------------------------------------------ 查询

@app.get("/metrics")
def get_metrics(x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                series_id: str | None = Query(default=None),
                cohort_id: str | None = Query(default=None),
                project_id: str | None = Query(default=None),
                limit: int = Query(default=50)):
    """§6.1 的扩展响应：每个指标最新一条快照 + `data_as_of` + `staleness`。

    没有快照的指标不出现在列表里 —— **不是**一条 `value: 0`。
    """
    return service.get_metrics(_tenant(x_tenant_id), series_id=series_id,
                               cohort_id=cohort_id, project_id=project_id,
                               limit=limit)


@app.get("/cohorts/{cohort_id}")
def get_cohort(cohort_id: str,
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """跨租户与不存在同样是 404。"""
    return service.get_cohort(_tenant(x_tenant_id), cohort_id)


@app.get("/metric-snapshots")
def list_snapshots(x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                   cohort_id: str | None = Query(default=None),
                   metric_key: str | None = Query(default=None),
                   limit: int = Query(default=50)):
    return service.list_snapshots(_tenant(x_tenant_id), cohort_id=cohort_id,
                                  metric_key=metric_key, limit=limit)


@app.get("/metric-snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str,
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_snapshot(_tenant(x_tenant_id), snapshot_id)


@app.get("/metric-snapshots/{snapshot_id}/export")
def export_snapshot(snapshot_id: str,
                    x_tenant_id: str = Header(default="",
                                              alias="X-Tenant-Id"),
                    format: str = Query(default="csv")):
    """导出的不是一列数字，是**带口径的表格**（§6.5）。空单元格 + note，不是 0。"""
    return service.export_snapshot(_tenant(x_tenant_id), snapshot_id, format)


@app.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str,
                x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
                version: int | None = Query(default=None)):
    return service.get_dataset(_tenant(x_tenant_id), dataset_id, version)


@app.get("/blind-reviews/{review_id}")
def get_blind_review(review_id: str,
                     x_tenant_id: str = Header(default="",
                                               alias="X-Tenant-Id")):
    """响应**永不**带 `model_id` / `prompt_revision` / `attempt_no` / `is_retry`。"""
    return service.get_blind_review(_tenant(x_tenant_id), review_id)


@app.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str,
                   x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id")):
    return service.get_experiment(_tenant(x_tenant_id), experiment_id)


@app.get("/experiments/{experiment_id}/assign")
def assign_subject(experiment_id: str,
                   subject_id: str = Query(...),
                   x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id")):
    """确定性分配：同一个 subject 永远落在同一臂。"""
    return service.assign(_tenant(x_tenant_id), experiment_id, subject_id)


@app.get("/experiments/{experiment_id}/results")
def experiment_results(experiment_id: str,
                       x_tenant_id: str = Header(default="",
                                                 alias="X-Tenant-Id")):
    return service.experiment_results(_tenant(x_tenant_id), experiment_id)


@app.get("/cost-projections/{projection_id}")
def get_projection(projection_id: str,
                   x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id")):
    return service.get_projection(_tenant(x_tenant_id), projection_id)


# ------------------------------------------------------------------ 事件

@app.post("/events:consume")
def consume_event(payload: dict,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    """上游事件入口。inbox 自己就是幂等。

    **事件只推进水位与归属投影**，不改任何生产事实（S44 是读数，不是账本）。
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
    """`ready` 是「现在能不能算出一个不编数字的指标」，不是「库连不连得上」。

    每个没配的上游都自报一个 `_unavailable_code`；这里把它们收上来。默认
    部署里 S23/S26/S35–S37/S40/S43 都没配，所以如实报 `ready: false` 并
    列出缺哪些 —— 把「起得来但一律 503」报成 ready，会让编排层等到第一次
    要报表时才发现。
    """
    reachable = _conn.execute("SELECT 1").fetchone() is not None
    blockers = [] if reachable else ["DATABASE_UNREACHABLE"]
    blockers.extend(service.unavailable_dependencies())
    return {"ready": not blockers,
            "service_id": "S44",
            "blockers": blockers,
            "evaluation_execution_enabled": service.evaluation_execution_enabled,
            "contract_gaps": CONTRACT_GAPS}
