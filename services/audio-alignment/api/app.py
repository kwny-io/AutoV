"""S37 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）里没有 S37 的路径；方案 §6 定义了
它们，`service.yaml` 的 `api` 段也写了 `POST /alignment-jobs` /
`GET /alignment-reports/{id}`。本层按方案实现，不改契约。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id`（顶层或
`execution_context.tenant_id`）**只用于比对**，不一致即 403。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

四个实现细节：

1. **`request_hash` 不含 trace / 时间戳。** 同一个请求重投时 trace_id 变了，
   裸 `canonical_request_hash` 会算出不同摘要，然后把一次合法重投判成
   "同键不同 body"。本层用 `stable_request_hash`（与 S41 同一份易变键清单）。
2. **幂等占位与结果 UPDATE 在事务之外。** SDK 的 `begin()` 先落占位、
   `finish()` 只 UPDATE。事务回滚时 `abort()` 删掉占位，重投仍能成功；
   `abort()` 本身写不进去时吞掉它的 `OperationalError`，不盖住真正的错误。
3. **幂等写库失败是 503。** 内核的 `Transaction` 只映射它自己的
   `BEGIN IMMEDIATE`；占位与落结果在事务之外。不映射就是未捕获异常 → 500，
   而"写不进去"不等于"请求有错"。
4. **两个开关默认关闭**（`AUDIO_ALIGNMENT_ENABLED` /
   `AUDIO_ALIGNMENT_CHECKS`）。关着时命令 503 / 422，**绝不产出空报告**：
   一份没有任何检查的 `pass` 比没有报告更危险（S37-19）。
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

from audio_alignment import registry
from audio_alignment.deps import (
    build_analysis_source,
    build_artifact_source,
    build_lexicon_source,
    build_shot_source,
    build_voice_source,
)
from audio_alignment.schema import CONTRACT_GAPS
from audio_alignment.service import (
    AudioAlignmentService,
    ServiceError,
    stable_request_hash,
)

DB_PATH = os.environ.get("S37_DB", ":memory:")

app = FastAPI(title="S37 audio-alignment", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row


def _checks_from_env() -> tuple[str, ...]:
    raw = os.environ.get("AUDIO_ALIGNMENT_CHECKS", "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


service = AudioAlignmentService(
    _conn,
    artifact_source=build_artifact_source(os.environ.get("S28_BASE_URL")),
    analysis_source=build_analysis_source(os.environ.get("S26_BASE_URL")),
    shot_source=build_shot_source(os.environ.get("S06_BASE_URL")),
    lexicon_source=build_lexicon_source(os.environ.get("S15_BASE_URL")),
    voice_source=build_voice_source(os.environ.get("S15_BASE_URL")),
    enabled=os.environ.get("AUDIO_ALIGNMENT_ENABLED", "false").lower() == "true",
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
        raise ServiceError(400, "INVALID_BODY",
                           "Idempotency-Key does not match body command_id")
    # 两处都查：命令体的租户在 execution_context 里；漏掉它就等于相信了
    # 请求体（AGENTS.md：平台签名之外没有可信 tenant）。
    for candidate in (payload.get("tenant_id"),
                      (payload.get("execution_context") or {}).get("tenant_id")
                      if isinstance(payload.get("execution_context"), dict)
                      else None):
        if candidate is not None and candidate != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放**不重新测量**：第一次的判定已落库并发了事件；再算一遍可能得到
    另一个 `report_generation`，而 S39 已经消费了第一份。
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
            503, "ALIGNMENT_UNAVAILABLE",
            f"audio-alignment database is not writable: {exc}",
            retryable=True) from exc


def _tenant(tenant_id: str) -> str:
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return tenant_id


# ------------------------------------------------------------------ 命令

@app.post("/alignment-jobs", status_code=202)
def run_alignment(payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """202 表示"已接受执行"，**不表示已通过**；结论看报告。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.check(
                       x_tenant_id, command_id, body, request_hash))


# ------------------------------------------------------------------ 查询

@app.get("/alignment-reports/{report_id}")
def get_report(report_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id"),
               generation: str | None = Query(default="latest")):
    return service.get_report(_tenant(x_tenant_id), report_id, generation)


@app.get("/alignment-reports/{report_id}/units")
def get_units(report_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id"),
              generation: str | None = Query(default="latest"),
              start: float | None = Query(default=None),
              end: float | None = Query(default=None)):
    """S38 的**唯一**时间码来源（I2）。时间基准 presentation_pts。"""
    return service.get_units(_tenant(x_tenant_id), report_id,
                             generation=generation, start=start, end=end)


# ------------------------------------------------------------------ 事件

@app.post("/events:consume")
def consume_event(payload: dict,
                  x_tenant_id: str = Header(default="",
                                            alias="X-Tenant-Id")):
    """S15 音色授权事件的投递入口。inbox 自己就是幂等。

    **这不是授权判定**：它只把最近一次状态投影到本地；判定前仍实时回查
    S15（`deps.build_voice_source`）。投影只用于发现"读到的比事件旧"。
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
    return service.consume_voice_authorization_event(x_tenant_id, envelope)


@app.get("/health/readiness")
def readiness():
    row = _conn.execute("SELECT 1").fetchone()
    return {"ready": row is not None,
            "enabled": service.enabled,
            "enabled_checks": list(service.enabled_checks),
            "check_registry_version": registry.REGISTRY_VERSION,
            "contract_gaps": CONTRACT_GAPS}
