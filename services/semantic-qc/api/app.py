"""S36 HTTP 层。

契约面：`contracts/openapi.v2.yaml` 里没有 `/semantic-checks` 与
`/semantic-reports/{id}`（SQC-16），本层按方案 §9 实现，不改契约。

三条与安全有关的决定都落在这一层：

1. 租户只来自 `X-Tenant-Id`。请求体里的 `tenant_id` 只用于比对（403），
   因为 body 是不可信输入（AGENTS.md：LLM 提供的 tenant_id 不能当真相）。
2. 未配置上游时**先 503 再解析**：`SemanticQcService.create_check` 第一行就
   检查依赖闭包的 `configured`，所以一次"没有 S26"的调用不会留下半份报告，
   也不会退回"没有模型意见那就通过"。
3. `GET /semantic-reports/{id}` 按租户过滤，跨租户一律 404——不确认存在性
   （E24/F3）。
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

from semantic_qc.deps import build_default_deps
from semantic_qc.schema import CONTRACT_GAPS
from semantic_qc.service import SemanticQcService, ServiceError

DB_PATH = os.environ.get("S36_DB", ":memory:")

app = FastAPI(title="S36 semantic-qc", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = SemanticQcService(_conn, **build_default_deps())
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
    # SQC-14：方案 §8.1 把这个错误叫 `COMMAND_PAYLOAD_MISMATCH`，SDK 的码是
    # `IDEMPOTENCY_KEY_REUSED`。对外用方案的名字（它出现在本服务的文档里），
    # 把 SDK 的原值放进 details，不吞掉。
    return _err(409, "COMMAND_PAYLOAD_MISMATCH", str(exc), False,
                details={"sdk_code": exc.code,
                         "stored_hash": exc.stored_hash,
                         "incoming_hash": exc.incoming_hash})


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    # SQC-13：方案 §8.1 想要 202 + 同一 operation_id，但 SDK 只抛异常、
    # 拿不到在途记录的 result。与 S25/S33 同侪一致：409 + Retry-After。
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
    candidate = payload.get("tenant_id")
    if candidate is not None and candidate != tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 重放，异 hash 409。

    重放**不重跑评估器**：一次语义质检要调几十次 VLM（方案 §8.1 明说
    `evaluator_calls: 41` 是常见量级），网络重试一次就是几十次重复付费。
    """
    request_hash = canonical_request_hash(body)
    record = _idem.begin(tenant_id, command_id, request_hash)
    if record.result is not None:
        replay = json.loads(record.result)
        replay["replayed"] = True
        return replay
    try:
        result = execute(request_hash)
    except Exception:
        _idem.abort(tenant_id, command_id)
        raise
    _idem.finish(tenant_id, command_id,
                 json.dumps(result, ensure_ascii=False))
    return {**result, "replayed": False}


# ------------------------------------------------------------------ 命令

@app.post("/semantic-checks", status_code=202)
def create_check(payload: dict,
                 # `default=""` 而不是必填：缺头要落到 `_body` 的 400
                 # `INVALID_BODY`，与 S33/S06/S11 同侪一致。用必填 Header
                 # 的话 FastAPI 会在进函数前抛 422，客户端拿不到本服务的
                 # 错误信封（code / retryable / correlation_id）。
                 command_id: str = Header(default="", alias="Idempotency-Key"),
                 x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.create_check(
                       x_tenant_id, command_id, body, request_hash))


# ------------------------------------------------------------------ 查询

@app.get("/semantic-reports/{report_id}")
def get_report(report_id: str,
               x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
               include: str | None = Query(default=None)):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.get_report(x_tenant_id, report_id, include=include)


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None,
            # 上游是否就绪是运行期事实，不是"服务活着"的同义词。
            "upstreams": {
                "s26": getattr(service.submit_source, "configured", False),
                "s28": getattr(service.artifact_source, "configured", False),
                "s17": getattr(service.expected_state_source, "configured", False),
            },
            "contract_gaps": CONTRACT_GAPS}
