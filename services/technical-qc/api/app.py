"""S35 HTTP 层。

契约为 `POST /technical-checks` + `GET /technical-reports/{id}`（§6）。
`GET /technical-reports?artifact_sha256=&manifest_id=` 是给 S39 / S08 投影的
内部查询面（§6.3）。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

两个开关默认关闭（§11）：`TECHNICAL_QC_ENABLED`、`TECHNICAL_QC_CHECKS`。
关着的时候命令返回 503/422，**不产出一份空报告**——一份没有任何检查的
`pass` 比没有报告更危险。
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

from technical_qc.deps import (
    build_artifact_source,
    build_policy_source,
    build_probe_source,
    build_shot_source,
)
from technical_qc.schema import CONTRACT_GAPS
from technical_qc.service import ServiceError, TechnicalQcService

DB_PATH = os.environ.get("S35_DB", ":memory:")

app = FastAPI(title="S35 technical-qc", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row


def _checks_from_env() -> tuple[str, ...]:
    raw = os.environ.get("TECHNICAL_QC_CHECKS", "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


service = TechnicalQcService(
    _conn,
    artifact_source=build_artifact_source(os.environ.get("S28_BASE_URL")),
    probe_source=build_probe_source(os.environ.get("MEDIA_PROBE_BASE_URL")),
    shot_source=build_shot_source(os.environ.get("S06_BASE_URL")),
    policy_source=build_policy_source(os.environ.get("S02_BASE_URL")),
    enabled=os.environ.get("TECHNICAL_QC_ENABLED", "false").lower() == "true",
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
    # 同键不同 body：§8 要求 409，不是覆盖。
    return _err(409, exc.code, str(exc), False)


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
    candidate = payload.get("tenant_id")
    if candidate is not None and candidate != tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放**不重新测量**：第一次的判定已经落库并发了事件，第二次再算一遍可能
    得到不同的 `report_generation`，而 S39 已经消费了第一份。
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

@app.post("/technical-checks", status_code=202)
def run_checks(payload: dict,
               command_id: str = Header(alias="Idempotency-Key"),
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.check(
                       x_tenant_id, command_id, body, request_hash))


# ------------------------------------------------------------------ 查询

@app.get("/technical-reports/{report_id}")
def get_report(report_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id"),
               generation: str | None = Query(default="latest")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.get_report(x_tenant_id, report_id, generation)


@app.get("/technical-reports")
def list_reports(x_tenant_id: str = Header(alias="X-Tenant-Id"),
                 artifact_sha256: str | None = Query(default=None),
                 manifest_id: str | None = Query(default=None),
                 limit: int = Query(default=50)):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.list_reports(x_tenant_id,
                                artifact_sha256=artifact_sha256,
                                manifest_id=manifest_id, limit=limit)


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
