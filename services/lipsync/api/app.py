"""S32 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）没有 S32 的路径，catalog 只声明了
`POST /lipsync-jobs`（命令）与 `GET /lipsync-jobs/{id}`（查询）。其余路由
来自方案 §6.3，是本服务自己提出、尚未进入 OpenAPI 文档的（缺口 G12）。
按 `service.yaml` 走，不改生成物。

租户一律来自 `X-Tenant-Id`（平台签名上下文）。请求体里的 `tenant_id` 与
`context.tenant_id` 只用于**比对**，不一致即 403/400 —— AGENTS.md 禁止把
请求体里的 `tenant_id` 当可信输入。

`POST /lipsync-jobs` 的三种成功状态码各有语义：

- 202 = 新建了一个 job（或同输入已有在途 job，被抑制）
- 200 = 命中已有 take，**没有创建付费任务**，`lipsync_job_id` 为 null
- 409 = 输入不合格（`INPUTS_INELIGIBLE`）。**job 已经落库**，重放时拿到
  的仍是这一条，不会重新问一遍 S37
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

from lipsync.deps import build_all
from lipsync.schema import CONTRACT_GAPS
from lipsync.service import LipsyncService, ServiceError

DB_PATH = os.environ.get("S32_DB", os.environ.get("SNN_DB", ":memory:"))

app = FastAPI(title="S32 lipsync", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = LipsyncService(_conn, **build_all())
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
    # 方案里的命名放进 details 让两种叫法都对得上。
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
    """幂等表自己的写入撞上锁时，命令状态**不可知**。

    409 会让调用方以为"已经做过了"，500 会让它以为"肯定没做"——两者都能
    变成重复付费或漏付费。按 5xx 可重试处理（E16 的同一逻辑）。
    """
    return _err(503, "STORE_UNAVAILABLE", f"sqlite: {exc}", retryable=True)


def _body(payload, command_id: str, tenant_id: str) -> dict:
    if payload is None:
        payload = {}
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

@app.post("/lipsync-jobs")
def create_lipsync_job(payload: dict,
                       command_id: str = Header(alias="Idempotency-Key"),
                       x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)

    def execute():
        status, result = service.create_job(
            x_tenant_id, command_id, body, canonical_request_hash(body))
        return {"_status": status, **result}

    result = command(x_tenant_id, body, execute)
    status = int(result.pop("_status", 202))
    if status == 409:
        # `INPUTS_INELIGIBLE`：job 已落库，响应体同时是错误体与 job 引用。
        return JSONResponse(status_code=409, content=result)
    # 复用是 200（§6.1）：语义是"给你一个已有 take"，不是"新开一次生产"。
    if status == 200 or result.get("reused"):
        return JSONResponse(status_code=200, content=result)
    return JSONResponse(status_code=202, content=result)


@app.post("/lipsync-jobs/{job_id}:cancel")
def cancel_job(job_id: str, payload: dict | None = None,
               command_id: str = Header(alias="Idempotency-Key"),
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.cancel_job(x_tenant_id, job_id))


@app.post("/lipsync-jobs/{job_id}:retry")
def retry_job(job_id: str, payload: dict | None = None,
              command_id: str = Header(alias="Idempotency-Key"),
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """只有 `{FAILED, RECONCILED_FAILED}` 成功；其余 409（**不是 422**）。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.retry_job(x_tenant_id, job_id))


@app.post("/lipsync-jobs/{job_id}:reconcile")
def reconcile_job(job_id: str, payload: dict | None = None,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """对账 `WAITING_UNKNOWN`。**本路由不会重新提交付费任务**（E16）。

    不传 `provider_outcome` 就只读一次 S26；查不到就问下一个人。
    """
    body = _body(payload, command_id, x_tenant_id)
    outcome = body.get("provider_outcome")
    if outcome is None and service.generation_state is not None:
        job = service._job(x_tenant_id, job_id)
        if job.get("provider_job_id"):
            try:
                outcome = service.generation_state(
                    x_tenant_id, job["provider_job_id"])
            except (ServiceError, ConnectionError):
                outcome = None
    return command(x_tenant_id, body,
                   lambda: service.reconcile_unknown(x_tenant_id, job_id,
                                                     outcome))


# ------------------------------------------------------------------ 查询

@app.get("/lipsync-jobs/{job_id}")
def get_job(job_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_job(x_tenant_id, job_id)


@app.get("/lipsync-jobs")
def list_jobs(x_tenant_id: str = Header(alias="X-Tenant-Id"),
              shot_lineage_id: str | None = Query(None),
              speaker_character_id: str | None = Query(None),
              state: str | None = Query(None)):
    return service.list_jobs(x_tenant_id, shot_lineage_id=shot_lineage_id,
                             speaker_character_id=speaker_character_id,
                             state=state)


@app.get("/shots/{shot_lineage_id}/lipsync-checkpoint")
def get_shot_checkpoint(shot_lineage_id: str,
                        x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """S41 / S42 的主读口。一行一个说话人。"""
    return service.get_shot_checkpoint(x_tenant_id, shot_lineage_id)


@app.get("/lipsync-takes/{take_id}")
def get_take(take_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_take(x_tenant_id, take_id)


@app.get("/lipsync-takes/{take_id}/validity")
def validity(take_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id"),
             video_hash: str | None = Query(None),
             audio_hash: str | None = Query(None)):
    """**现场**判有效期：三路合取，其中两路要实时问 S31 / S28（§4.2）。

    客户端自己比 hash 不够——授权撤销与 tombstone 都不改 hash。
    """
    return service.validity(x_tenant_id, take_id, video_hash=video_hash,
                            audio_hash=audio_hash)


# ------------------------------------------------------------------ 巡检

@app.post("/internal/sweep:deadlines")
def sweep_deadlines(x_tenant_id: str = Header(default="",
                                             alias="X-Tenant-Id")):
    """`WAITING_UNKNOWN` 过了死线 → `FAILED(DEADLINE)`。**不重提。**"""
    return service.sweep_deadlines(x_tenant_id or None)


@app.post("/internal/recover")
def recover(x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    """§7.3 重启矩阵：按状态分派，不统一重试，不重提付费任务。"""
    return service.recover(x_tenant_id or None)


# ------------------------------------------------------------------ 事件

@app.post("/events/{event_type}")
def consume_event(event_type: str, envelope: dict,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    """事件入口。按 `event_type` 分派到内核的消费方法。

    **不做幂等去重**：每个消费方法自己按状态判断（终态不再被推动、第二次
    void 成为 no-op、同 hash 的重复 ready 不作废任何东西），所以重复投递
    天然无害，而一份额外的去重表会成为第三个权威。生产环境这里应接平台
    Inbox；本层只做路由。
    """
    tenant = envelope.get("tenant_id") or x_tenant_id
    event = {**envelope, "tenant_id": tenant}
    handlers = {
        "generation.job.state-changed":
            service.on_generation_state_changed,
        "artifact.ingested": service.on_artifact_ingested,
        "production.speech.ready": service.on_speech_ready,
        "speech.voice-authorization-lapsed":
            service.on_voice_authorization_lapsed,
        "production.video.ready": service.on_video_ready,
        "quality.alignment.completed": service.on_alignment_completed,
        "quality.repair.planned": service.on_repair_planned,
        "post.timeline.revision.created": service.on_timeline_revision,
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
    return {"ready": True, "service_id": "S32",
            "contract_gaps": CONTRACT_GAPS}
