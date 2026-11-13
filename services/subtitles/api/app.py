"""S38 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）里没有 S38 的路径；方案 §7 定义了
它们，`service.yaml` 的 `api` 段也写了 `POST /subtitle-jobs` /
`GET /subtitle-tracks/{id}`。本层按方案实现，不改契约。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id`（顶层或
`execution_context.tenant_id`）**只用于比对**，不一致即 403。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

五个实现细节：

1. **`request_hash` 不含 trace / 时间戳。** 同一个请求重投时 trace_id 变了，
   裸 `canonical_request_hash` 会算出不同摘要，然后把一次合法重投判成
   "同键不同 body"。本层用 `stable_request_hash`（与 S37/S41 同一份清单）。
2. **幂等占位与结果 UPDATE 在事务之外。** SDK 的 `begin()` 先落占位、
   `finish()` 只 UPDATE。事务回滚时 `abort()` 删掉占位，重投仍能成功；
   `abort()` 本身写不进去时吞掉它的 `OperationalError`，不盖住真正的错误。
3. **幂等写库失败是 503。** 内核的 `Transaction` 只映射它自己的
   `BEGIN IMMEDIATE`；占位与落结果在事务之外。不映射就是未捕获异常 → 500，
   而"写不进去"不等于"请求有错"。
4. **`GET /subtitle-tracks/{id}/revisions/{rev}` 只给签名 URL。** 字节在
   S28（§3.0），本服务不渲染下载、不流式返回 `.srt` 正文。
5. **两个开关默认关闭**（`S38_ENABLED` / `S38_CHECKS`）。关着时命令 403，
   **只读面照常开放**：审计可见性不该被一个生产开关收回（S38-C13）。
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

from subtitles import schema
from subtitles.deps import (
    build_alignment_source,
    build_archive_source,
    build_artifact_url_source,
    build_dialogue_source,
    build_lexicon_source,
    build_timeline_source,
    build_translation_source,
)
from subtitles.schema import CONTRACT_GAPS
from subtitles.service import ServiceError, SubtitleService, stable_request_hash

DB_PATH = os.environ.get("S38_DB", ":memory:")

app = FastAPI(title="S38 subtitles", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row


def _checks_from_env() -> tuple[str, ...]:
    raw = os.environ.get("S38_CHECKS", "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


service = SubtitleService(
    _conn,
    alignment_source=build_alignment_source(os.environ.get("S37_BASE_URL")),
    timeline_source=build_timeline_source(os.environ.get("S41_BASE_URL")),
    dialogue_source=build_dialogue_source(os.environ.get("S06_BASE_URL")),
    lexicon_source=build_lexicon_source(os.environ.get("S15_BASE_URL")),
    archive_source=build_archive_source(os.environ.get("S28_BASE_URL")),
    artifact_url_source=build_artifact_url_source(
        os.environ.get("S28_BASE_URL")),
    # 存在但从不调用：stale 译文不自动重译（付费 S26，需人验收）。
    translation_source=build_translation_source(os.environ.get("S26_BASE_URL")),
    enabled=os.environ.get("S38_ENABLED", "0").lower() in ("1", "true", "yes"),
    enabled_checks=_checks_from_env(),
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
    context = payload.get("execution_context")
    for candidate in (payload.get("tenant_id"),
                      context.get("tenant_id")
                      if isinstance(context, dict) else None):
        if candidate is not None and candidate != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放**不重新渲染**：第一次的字节已交 S28、事件已发；再算一遍可能得到
    另一个 `subtitle_revision`，而下游已经消费了第一个。
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
            503, "SUBTITLES_UNAVAILABLE",
            f"subtitles database is not writable: {exc}",
            retryable=True) from exc


def _tenant(tenant_id: str) -> str:
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return tenant_id


# ------------------------------------------------------------------ 命令

@app.post("/subtitle-jobs", status_code=202)
def run_subtitles(payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """202 表示"已接受执行"，**不表示已通过**；结论看 `qc_decision`。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.check(
                       x_tenant_id, command_id, body, request_hash))


# ------------------------------------------------------------------ 查询

@app.get("/subtitle-tracks/{track_id}")
def get_track(track_id: str,
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """track + cue 列表 + 三组版本引用 + 当前 QCReport 引用（方案 §7）。"""
    return service.get_track(_tenant(x_tenant_id), track_id)


@app.get("/subtitle-tracks/{track_id}/revisions/{revision}")
def get_rendition(track_id: str, revision: str,
                  x_tenant_id: str = Header(alias="X-Tenant-Id"),
                  format: str = Query(default="srt")):
    """返回**指向 S28 的签名 URL**，不是字节（§3.0）。"""
    return service.get_rendition_url(_tenant(x_tenant_id), track_id, revision,
                                     format)


@app.post("/subtitle-tracks/{track_id}/archive-retries")
def retry_archive(track_id: str,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """`rendered_pending_archive` 的重试入口。

    **不重新渲染**只能是"重渲染逐字节相同"：库里没有字节，重试时与
    `pending_render.content_hash` 比对，不一致即 500（S38-C10）。
    """
    return service.retry_pending_archive(_tenant(x_tenant_id), track_id)


# ------------------------------------------------------------------ 事件

@app.post("/events:consume")
def consume_event(payload: dict,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    """S06 对白 revision / S41 timeline revision 的投递入口。

    inbox 自己是幂等。**这不是权威判定**：S41 的 clip 参数仍实时回读；
    原句 revision 用事件里的值，因为正文本来就不在 S38。
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
    event_type = str(envelope.get("type") or "")
    if event_type == schema.EVENT_TIMELINE:
        return service.consume_timeline_revision_event(x_tenant_id, envelope)
    if event_type == schema.EVENT_SCRIPT:
        return service.consume_script_revision_event(x_tenant_id, envelope)
    raise ServiceError(
        422, "UNSUPPORTED_EVENT",
        f"S38 consumes {schema.EVENT_SCRIPT!r} and "
        f"{schema.EVENT_TIMELINE!r}; got {event_type!r}")


@app.get("/health/readiness")
def readiness():
    row = _conn.execute("SELECT 1").fetchone()
    return {"ready": row is not None,
            "enabled": service.enabled,
            "enabled_checks": list(service.enabled_checks),
            "check_registry_version": schema.REGISTRY_VERSION,
            "contract_gaps": CONTRACT_GAPS}
