"""S31 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）没有 S31 的路径，catalog 只声明了
`POST /speech-jobs`（命令）与 `GET /speech-jobs/{id}`（查询）。其余路由来自
方案 §6.3，是本服务自己提出、尚未进入 OpenAPI 文档的（缺口 C1）。按
`service.yaml` 走，不改生成物。

租户一律来自 `X-Tenant-Id`（平台签名上下文）。请求体里的 `tenant_id` 与
`context.tenant_id` 只用于**比对**，不一致即 403/400 —— AGENTS.md 禁止把
请求体里的 `tenant_id` 当可信输入。

`POST /speech-jobs` 的两种成功状态码是有语义的：202 = 新建了一个 job，
200 = 命中已有 take、**没有创建付费任务**。200 的 `speech_job_id` 是 null，
调用方若按 202 处理会去轮询一个不存在的 job。
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
    canonical_request_hash,
)

from speech_production.deps import build_all
from speech_production.schema import CONTRACT_GAPS
from speech_production.service import SpeechService, ServiceError

DB_PATH = os.environ.get("S31_DB", os.environ.get("SNN_DB", ":memory:"))

app = FastAPI(title="S31 speech-production", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = SpeechService(_conn, **build_all())
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
    # 同键不同 body：**409，不是覆盖**（E25）。SDK 的 code 保留原样，
    # 方案里的 `IDEMPOTENCY_CONFLICT` 放进 details 以便两条命名都对得上。
    return _err(409, exc.code, str(exc), False,
                {"also_known_as": "IDEMPOTENCY_CONFLICT"})


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(
        status_code=409, headers={"Retry-After": "1"},
        content={"code": "COMMAND_IN_FLIGHT", "message": str(exc),
                 "retryable": True, "correlation_id": "", "details": {}})


@app.exception_handler(sqlite3.OperationalError)
async def _operational(_request, exc: sqlite3.OperationalError):
    """幂等表自己的写入撞上锁（或库不可用）时，命令状态**不可知**。

    报 409 会让调用方以为"已经做过了"；报 500 会让它以为"肯定没做"。
    两者都可能造成重复付费或漏付费，所以按 5xx 可重试处理（E16 的同一逻辑：
    不确定就不动钱）。
    """
    return _err(503, "STORE_UNAVAILABLE",
                f"sqlite: {exc}", retryable=True)


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
    candidate = payload.get("tenant_id")
    if candidate is not None and candidate != tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return {**payload, "command_id": command_id, "tenant_id": tenant_id}


def command(tenant_id: str, body: dict, execute):
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    内核在同一个事务里已经写了一次 `result`；这里的 `finish` 是同值覆盖。
    """
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


# ------------------------------------------------------------------ 命令

@app.post("/speech-jobs")
def create_speech_job(payload: dict,
                      command_id: str = Header(alias="Idempotency-Key"),
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)

    def execute():
        status, result = service.create_job(
            x_tenant_id, command_id, body, canonical_request_hash(body))
        return {"_status": status, **result}

    result = command(x_tenant_id, body, execute)
    status = int(result.pop("_status", 202))
    # 复用是 200（§6.1）：语义上是"给你一个已有 take"，不是"新开一次生产"。
    if status == 200 or result.get("reused"):
        return JSONResponse(status_code=200, content=result)
    return JSONResponse(status_code=202, content=result)


@app.post("/speech-jobs/{job_id}:cancel")
def cancel_job(job_id: str, payload: dict | None = None,
               command_id: str = Header(alias="Idempotency-Key"),
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.cancel_job(x_tenant_id, job_id))


@app.post("/speech-jobs/{job_id}:retry")
def retry_job(job_id: str, payload: dict | None = None,
              command_id: str = Header(alias="Idempotency-Key"),
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """只有 `{FAILED, RECONCILED_FAILED}` 成功；其余 409（**不是 422**）。"""
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.retry_job(x_tenant_id, job_id))


@app.post("/speech-jobs/{job_id}:reconcile")
def reconcile_job(job_id: str, payload: dict | None = None,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """对账 `WAITING_UNKNOWN`。**本路由不会重新提交付费任务**（E16）。

    不传 `provider_outcome` 就只读一次 S26；查不到就问下一个人。
    """
    body = _body(payload or {}, command_id, x_tenant_id)
    outcome = body.get("provider_outcome")
    if outcome is None and service.generation_state is not None:
        job = service._job(x_tenant_id, job_id)
        pid = job.get("provider_job_id")
        if pid:
            try:
                outcome = service.generation_state(x_tenant_id, pid)
            except (ServiceError, ConnectionError):
                outcome = None
    return command(x_tenant_id, body,
                   lambda: service.reconcile_unknown(x_tenant_id, job_id,
                                                     outcome))


# ------------------------------------------------------------------ 查询

@app.get("/speech-jobs/{job_id}")
def get_job(job_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_job(x_tenant_id, job_id)


@app.get("/speech-jobs")
def list_jobs(x_tenant_id: str = Header(alias="X-Tenant-Id"),
              shot_lineage_id: str | None = Query(None),
              utterance_id: str | None = Query(None),
              locale: str | None = Query(None),
              state: str | None = Query(None)):
    return service.list_jobs(x_tenant_id, shot_lineage_id=shot_lineage_id,
                             utterance_id=utterance_id, locale=locale,
                             state=state)


@app.get("/shots/{shot_lineage_id}/speech-checkpoint")
def get_shot_checkpoint(shot_lineage_id: str,
                        x_tenant_id: str = Header(alias="X-Tenant-Id"),
                        locale: str | None = Query(None)):
    """S30 / S41 / S32 的主读口。"""
    return service.get_shot_checkpoint(x_tenant_id, shot_lineage_id,
                                       locale=locale)


@app.get("/voice-takes/{take_id}")
def get_take(take_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_take(x_tenant_id, take_id)


@app.get("/voice-takes/{take_id}/reuse-eligibility")
def reuse_eligibility(take_id: str,
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.reuse_eligibility(x_tenant_id, take_id)


# ------------------------------------------------------------------ 巡检

@app.post("/internal/sweep:deadlines")
def sweep_deadlines(x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id")):
    """T31-30 / E16：`WAITING_UNKNOWN` 过了死线 → `FAILED(DEADLINE)`。

    这里是唯一"不需要知道供应商怎么答"就能收尾的出口：结果不明时既不能
    重提（可能已收单），也不能永远挂着。**本路由不提交任何付费任务。**
    """
    return service.sweep_deadlines(x_tenant_id or None)


# ------------------------------------------------------------------ 事件

@app.post("/events/{event_type}")
def consume_event(event_type: str, envelope: dict,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    """事件入口。按 `event_type` 分派到内核的消费方法。

    **不做幂等去重**：每个消费方法自己按状态判断（终态不再被推动、
    第二次 void 成为 no-op），所以重复投递天然无害，而一份额外的去重表
    会成为第三个权威。生产环境这里应接平台 Inbox；本层只做路由。
    """
    tenant = envelope.get("tenant_id") or x_tenant_id
    event = {**envelope, "tenant_id": tenant}
    handlers = {
        "generation.job.state-changed":
            service.on_generation_state_changed,
        "artifact.ingested": service.on_artifact_ingested,
        "asset.voice.authorization.changed":
            service.on_voice_authorization_changed,
        "quality.alignment.completed": service.on_alignment_completed,
        "post.timeline.revision.created": service.on_timeline_revision,
        "quality.repair.planned": service.on_repair_planned,
        "snapshot.manifest.committed": service.on_manifest_committed,
    }
    handler = handlers.get(event_type)
    if handler is None:
        return {"ignored": True, "reason": f"unsubscribed:{event_type}"}
    with _Txn(service.conn):
        return handler(event)


class _Txn:
    """一个事件一个事务：状态转移、复查日志、outbox 要么全成，要么全无。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


@app.get("/healthz")
def readiness():
    return {"ready": True, "service_id": "S31",
            "contract_gaps": CONTRACT_GAPS}
