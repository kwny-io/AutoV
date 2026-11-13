"""S42 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）里只有 `POST /render-jobs`；方案 §6
定义了完整的一组，`service.yaml` 仅声明了那一个。本层按方案实现，**不改
契约也不改 service.yaml**（那是 catalog 派生出来的，见 AGENTS.md）。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id`（顶层或
`context.tenant_id`）**只用于比对**，不一致即 403。AGENTS.md：LLM 提供的
`tenant_id` 不能当可信输入。

五个实现细节：

1. **跨租户与不存在完全同一个响应。** `_not_found` 在两个分支里都构造
   `404 NOT_FOUND`，`details` 恒为空 —— 任何差异（哪怕多一个键）都会让
   攻击者区分"这个 id 存在但属于别人"。
2. **`request_hash` 不含 trace / 时间戳。** 重投时 trace_id 变了，裸
   `canonical_request_hash` 会算出不同摘要，把一次合法重投判成"同键不同
   body"。本层用 `stable_request_hash`（与 S37/S38/S41 同一份清单）。
3. **幂等占位与结果 UPDATE 在事务之外。** SDK 的 `begin()` 先落占位、
   `finish()` 只 UPDATE。事务回滚时 `abort()` 删掉占位，重投仍能成功。
4. **幂等写库失败是 503。** 内核的 `Transaction` 只映射它自己的
   `BEGIN IMMEDIATE`；占位与落结果在事务之外。不映射就是未捕获异常 → 500。
5. **事件入口不信任事件里的租户**：有 `tenant_id` 就必须等于签名租户，
   没有则补上签名的那个。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
)

from composition import schema
from composition.deps import build_all
from composition.schema import CONTRACT_GAPS
from composition.service import (
    CompositionService,
    ServiceError,
    render_env_hash,
    stable_request_hash,
)

DB_PATH = os.environ.get("S42_DB", ":memory:")

app = FastAPI(title="S42 composition", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = CompositionService(_conn, **build_all())
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


@app.exception_handler(sqlite3.OperationalError)
async def _db_busy(_request, exc: sqlite3.OperationalError):
    return _err(503, "COMPOSITION_UNAVAILABLE",
                f"composition database is not writable: {exc}",
                retryable=True)


def _body(payload, command_id: str, tenant_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    if not command_id:
        raise ServiceError(400, "INVALID_BODY", "Idempotency-Key is required")
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    inside = payload.get("command_id")
    if inside is not None and inside != command_id:
        # AGENTS.md：Idempotency-Key 必须等于 body.command_id。
        raise ServiceError(400, "INVALID_BODY",
                           "Idempotency-Key does not match body command_id")
    context = payload.get("context") or payload.get("execution_context")
    for candidate in (payload.get("tenant_id"),
                      context.get("tenant_id")
                      if isinstance(context, dict) else None):
        if candidate is not None and candidate != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "body tenant_id disagrees with the signed context")
    return payload


#: 占位多久没落结果就算「创建事务中途崩了」。要长于最长的一次上游往返。
STALE_PLACEHOLDER_SECONDS = 120


def _age_seconds(stamp) -> float:
    """占位的时间戳年龄。解析不了就当作 0（**不**当作过期而误删）。"""
    if not isinstance(stamp, str):
        return 0.0
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def _sweep_placeholder(tenant_id: str, command_id: str) -> bool:
    """清掉超时未落结果的占位（E13-S42-01）。

    创建事务崩溃时业务写已经回滚 —— 没有 job、没有 outbox —— 但占位是在
    事务**之外**落的。不清掉它，重投永远 409 `COMMAND_IN_FLIGHT`，一个
    业务上从未发生过的命令变成永久卡死。

    年龄在 Python 里算而不是在 SQL 里比字符串：SDK 写的是
    `isoformat()`（微秒为 0 时会被省略），定宽字符串比较在那一秒上会错判。
    """
    row = _conn.execute(
        "SELECT created_at FROM idempotency_record WHERE tenant_id=?"
        " AND command_id=? AND result IS NULL",
        (tenant_id, command_id)).fetchone()
    if row is None or _age_seconds(row[0]) < STALE_PLACEHOLDER_SECONDS:
        return False
    cur = _conn.execute(
        "DELETE FROM idempotency_record WHERE tenant_id=? AND command_id=?"
        " AND result IS NULL", (tenant_id, command_id))
    return cur.rowcount == 1


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放**不重新渲染**：第一次的 job / 事件已经落库，再走一遍会多申请一次
    许可、多写一个 job。
    """
    request_hash = stable_request_hash(body)
    try:
        try:
            record = _idem.begin(tenant_id, command_id, request_hash)
        except CommandInProgress:
            if not _sweep_placeholder(tenant_id, command_id):
                raise
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
            503, "COMPOSITION_UNAVAILABLE",
            f"composition database is not writable: {exc}",
            retryable=True) from exc


def _tenant(tenant_id: str) -> str:
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return tenant_id


# ------------------------------------------------------------------ 命令

@app.post("/render-jobs", status_code=202)
def create_render_job(payload: dict,
                      command_id: str = Header(alias="Idempotency-Key"),
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """202 表示"已接受执行"，**不表示已成片**；候选由异步收尾产生。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.create_render_job(
                       x_tenant_id, command_id, body, request_hash))


@app.post("/mix-jobs", status_code=202)
def create_mix_job(payload: dict,
                   command_id: str = Header(alias="Idempotency-Key"),
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§4.4 的 remix 入口。service.yaml 未声明（S42-C5）。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.create_mix_job(
                       x_tenant_id, command_id, body, request_hash))


@app.post("/render-jobs/{job_id}:cancel")
def cancel_render_job(job_id: str, payload: dict | None = None,
                      command_id: str = Header(default="",
                                               alias="Idempotency-Key"),
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """`CANCELLING`，不承诺立即停止，不退款（E22）。"""
    tenant = _tenant(x_tenant_id)
    body = _body(payload if payload is not None else {}, command_id, tenant)
    return command(tenant, body, command_id,
                   lambda _hash: service.cancel_render_job(tenant, job_id))


@app.post("/mix-jobs/{mix_job_id}:cancel")
def cancel_mix_job(mix_job_id: str, payload: dict | None = None,
                   command_id: str = Header(default="",
                                            alias="Idempotency-Key"),
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    tenant = _tenant(x_tenant_id)
    body = _body(payload if payload is not None else {}, command_id, tenant)
    return command(tenant, body, command_id,
                   lambda _hash: service.cancel_mix_job(tenant, mix_job_id))


# ------------------------------------------------------------------ 段回报

@app.post("/render-jobs/{job_id}/segments/{segment_no}:uploaded")
def segment_uploaded(job_id: str, segment_no: int, payload: dict,
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """worker 回报：段已上传且 S28 已确认。**不是幂等命令**，但重复回报同一
    段会 409 `SEGMENT_TERMINAL`，不会覆盖已有产物。"""
    tenant = _tenant(x_tenant_id)
    body = payload if isinstance(payload, dict) else {}
    return service.record_segment_uploaded(
        tenant, job_id, segment_no=int(segment_no),
        output_ref=body.get("output_artifact_ref")
        or body.get("output_ref"),
        consumed_gpu_ms=int(body.get("consumed_gpu_ms") or 0),
        execution_fence=_int(body.get("execution_fence"),
                             "execution_fence"),
        node_epoch=_int(body.get("node_epoch"), "node_epoch"))


@app.post("/render-jobs/{job_id}/segments/{segment_no}:retry")
def segment_retry(job_id: str, segment_no: int, payload: dict,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    tenant = _tenant(x_tenant_id)
    body = payload if isinstance(payload, dict) else {}
    return service.mark_segment_retry(
        tenant, job_id, segment_no=int(segment_no),
        attempts=_int(body.get("attempts", 1), "attempts"),
        consumed_gpu_ms=int(body.get("consumed_gpu_ms") or 0))


@app.post("/render-jobs/{job_id}:finish")
def finish_render(job_id: str, payload: dict,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """收尾：自检 → 响度门 → 候选 → `post.master.rendered`。"""
    tenant = _tenant(x_tenant_id)
    body = payload if isinstance(payload, dict) else {}
    return service.finish_render_job(
        tenant, job_id,
        video_ref=body.get("video_artifact_ref"),
        audio_ref=body.get("audio_artifact_ref"),
        measurement=body.get("measurement"),
        measurement_failed=bool(body.get("measurement_failed")),
        frame_count=body.get("frame_count"),
        small=body.get("artifacts") if isinstance(body.get("artifacts"),
                                                  dict) else None)


@app.post("/mix-jobs/{mix_job_id}:finish")
def finish_mix(mix_job_id: str, payload: dict,
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    tenant = _tenant(x_tenant_id)
    body = payload if isinstance(payload, dict) else {}
    return service.finish_mix_job(
        tenant, mix_job_id,
        output_audio_ref=body.get("output_audio_ref"),
        measurement=body.get("measurement"),
        measurement_failed=bool(body.get("measurement_failed")),
        consumed_cpu_ms=int(body.get("consumed_cpu_ms") or 0))


@app.post("/render-jobs/{job_id}:claim")
def claim_render(job_id: str, payload: dict | None = None,
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    tenant = _tenant(x_tenant_id)
    body = payload if isinstance(payload, dict) else {}
    worker_id = body.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id:
        raise ServiceError(422, "SCHEMA_INVALID", "worker_id is required")
    return service.claim_render_job(tenant, job_id, worker_id)


@app.post("/render-jobs:recover")
def recover(x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """租约过期的 job → ORPHANED。可由 reaper 或运维触发。"""
    return service.recover_expired_leases(_tenant(x_tenant_id))


@app.post("/render-jobs/{job_id}:reconcile")
def reconcile(job_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """ORPHANED 对账：**查 S28**（§7.2），不查本地临时文件。"""
    return service.reconcile_render_job(_tenant(x_tenant_id), job_id)


@app.post("/render-jobs/{job_id}:retry")
def retry_render(job_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.retry_render_job(_tenant(x_tenant_id), job_id)


@app.post("/usage-facts:flush")
def flush_usage(x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """把未上报的用量事实交给 S23。S23 不可达时整体 503。"""
    result = service.report_usage(_tenant(x_tenant_id))
    if result.get("error"):
        raise ServiceError(
            503, "USAGE_REPORT_UNAVAILABLE",
            "S23 is unreachable; usage facts stay local and unreported",
            retryable=True, details={"pending": result["pending"]})
    return result


# ------------------------------------------------------------------ 查询

@app.get("/render-jobs/{job_id}")
def get_render_job(job_id: str,
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_render_job(_tenant(x_tenant_id), job_id)


@app.get("/render-jobs")
def list_render_jobs(x_tenant_id: str = Header(alias="X-Tenant-Id"),
                     episode_id: str | None = Query(default=None),
                     branch_id: str | None = Query(default=None),
                     limit: int = Query(default=50, ge=1, le=200)):
    return service.list_render_jobs(_tenant(x_tenant_id),
                                    episode_id=episode_id,
                                    branch_id=branch_id, limit=limit)


@app.get("/mix-jobs/{mix_job_id}")
def get_mix_job(mix_job_id: str,
                x_tenant_id: str = Header(alias="X-Tenant-Id")):
    tenant = _tenant(x_tenant_id)
    row = _conn.execute(
        "SELECT * FROM mix_job WHERE tenant_id=? AND mix_job_id=?",
        (tenant, mix_job_id)).fetchone()
    if row is None:
        # 与不存在同形（架构红线）。
        raise ServiceError(404, "NOT_FOUND", "mix job not found")
    job = dict(row)
    return {**job,
            "source_video_ref": json.loads(job["source_video_ref"]),
            "stems": json.loads(job["stems"]),
            "execution_context": json.loads(job["execution_context"]),
            "repair_plan_ref": json.loads(job["repair_plan_ref"])
            if job["repair_plan_ref"] else None,
            "output_audio_ref": json.loads(job["output_audio_ref"])
            if job["output_audio_ref"] else None}


@app.get("/master-candidates/{candidate_id}")
def get_candidate(candidate_id: str,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_candidate(_tenant(x_tenant_id), candidate_id)


@app.get("/master-candidates")
def list_candidates(x_tenant_id: str = Header(alias="X-Tenant-Id"),
                    episode_id: str | None = Query(default=None),
                    branch_id: str | None = Query(default=None),
                    deliverable: bool | None = Query(default=None),
                    limit: int = Query(default=50, ge=1, le=200)):
    return service.list_candidates(_tenant(x_tenant_id),
                                   episode_id=episode_id,
                                   branch_id=branch_id,
                                   deliverable=deliverable, limit=limit)


@app.get("/master-candidates/{candidate_id}/evidence")
def candidate_evidence(candidate_id: str,
                       x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """给 S43 / S45 的渲染证据包（§6.2 的核心接口）。"""
    return service.candidate_evidence(_tenant(x_tenant_id), candidate_id)


# ------------------------------------------------------------------ 事件

@app.post("/events:consume")
def consume_event(payload: dict,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    """四个消费入口。inbox + 按 aggregate 水位都在这层之下。"""
    envelope = payload.get("event", payload)
    if not isinstance(envelope, dict):
        raise ServiceError(400, "INVALID_BODY", "event must be an object")
    tenant = envelope.get("tenant_id")
    if tenant is None:
        envelope = {**envelope, "tenant_id": _tenant(x_tenant_id)}
    elif tenant != x_tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "event tenant disagrees with the signed context")
    event_type = str(envelope.get("type") or "")
    if event_type == schema.EVENT_TIMELINE:
        return service.consume_timeline_revision(x_tenant_id, envelope)
    if event_type == schema.EVENT_REPAIR_PLANNED:
        return service.consume_repair_planned(x_tenant_id, envelope)
    if event_type == schema.EVENT_SNAPSHOT:
        return service.consume_snapshot_committed(x_tenant_id, envelope)
    if event_type == schema.EVENT_ARTIFACT_INGESTED:
        return service.consume_artifact_ingested(x_tenant_id, envelope)
    raise ServiceError(
        422, "UNSUPPORTED_EVENT",
        f"S42 consumes {schema.EVENT_TIMELINE!r}, "
        f"{schema.EVENT_REPAIR_PLANNED!r}, {schema.EVENT_SNAPSHOT!r} and "
        f"{schema.EVENT_ARTIFACT_INGESTED!r}; got {event_type!r}")


@app.get("/health/readiness")
def readiness():
    row = _conn.execute("SELECT 1").fetchone()
    return {"ready": row is not None,
            "topic": schema.TOPIC,
            "graph_version": schema.GRAPH_VERSION,
            "render_env_hash": render_env_hash(service.render_env),
            "contract_gaps": CONTRACT_GAPS}


def _int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be an integer",
                           details={"field": field})
    return value
