"""S29 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）里**没有 image 的路径**，所以这一层按
`plans/S29-image-production.md` §6.1–§6.3 实现。契约与方案不一致的地方一律登记
在 `CONTRACT_GAPS`，不在这里自行裁决，也不改契约。

**租户一律来自 `X-Tenant-Id`**；请求体里的 `tenant_id` 只用于比对（不一致
403）。AGENTS.md：LLM 提供的 `tenant_id` 不是可信输入。

三条与 S33/S34 同构、但这里更要紧的规矩：

1. **一个付费命令一个事务**：`create_job` 内部的 `_command` 把结果写回同一个
   `idempotency_record` 行，提交与"记下已提交"原子成立。否则存在"业务已提交、
   幂等未记录"的窗口，重试会再花一次钱。
2. **重放不重新路由、不重新提交**：S25 的报价会过期，重放时再路由一次等于把
   一个新报价塞进旧 attempt；再提交一次就是第二次付费。
3. **上游未配置时一律 503**，没有"忘了配就放行"的分支（见 `deps.build_all`）。
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

from image_production.deps import build_all
from image_production.schema import CONTRACT_GAPS
from image_production.service import ImageProductionService, ServiceError

DB_PATH = os.environ.get("S29_DB", ":memory:")

app = FastAPI(title="S29 image-production", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = ImageProductionService(_conn, **build_all({
    "S25": os.environ.get("S25_BASE_URL"),
    "S26": os.environ.get("S26_BASE_URL"),
    "S28": os.environ.get("S28_BASE_URL"),
}))
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
    # 同键不同 body：409，不是覆盖。
    return _err(409, exc.code, str(exc), False)


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(
        status_code=409, headers={"Retry-After": "1"},
        content={"code": "COMMAND_IN_FLIGHT", "message": str(exc),
                 "retryable": True, "correlation_id": "", "details": {}})


def _tenant(tenant_id: str) -> str:
    if not tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return tenant_id


def _body(payload, command_id: str, tenant_id: str) -> dict:
    """头缺失在 FastAPI 里会变 422；这里自己判成 400，与 §6.1 一致。"""
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
    declared = payload.get("tenant_id")
    if declared is not None and declared != tenant_id:
        # 403 而不是 400：这是在报"你声明的身份与凭证不符"。
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放路径**不重新执行**：一次图像生成是付费的，重放一次就是再花一次钱；
    而且 S25 的报价会过期、S26 的 attempt 已经存在，重跑只会得到一串 409。
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
    _idem.finish(tenant_id, command_id, json.dumps(result, ensure_ascii=False))
    return {**result, "replayed": False}


# ------------------------------------------------------------------ 命令

@app.post("/image-jobs", status_code=202)
def create_image_job(payload: dict,
                     command_id: str | None = Header(default=None, alias="Idempotency-Key"),
                     x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id")):
    """§6.1。**付费入口**：内部只走 S26，不直连任何模型。

    202 而不是 200：受理不等于完成。响应里的 `state` 是**此刻**的状态
    （`SUBMITTED`/`WAITING_PROVIDER`/`REJECTED_LOCAL`/…），不是承诺。
    """
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _rh: service.create_job(body, x_tenant_id, command_id))


@app.post("/image-jobs/{job_id}:cancel")
def cancel_job(job_id: str, payload: dict | None = None,
               command_id: str | None = Header(default=None, alias="Idempotency-Key"),
               x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id")):
    """§6.3。只向 S26 发取消；**不承诺退款**（T29-34）。

    `WAITING_UNKNOWN` 期间 409 `MUST_RECONCILE_FIRST`，且不向 S26 发任何东西。
    """
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _rh: service.cancel_job(x_tenant_id, job_id, body,
                                                  command_id))


@app.post("/image-jobs/{job_id}:retry")
def retry_job(job_id: str, payload: dict | None = None,
              command_id: str | None = Header(default=None, alias="Idempotency-Key"),
              x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id")):
    """只允许 `{FAILED, RECONCILED_FAILED}`；`WAITING_UNKNOWN` → 409（§6.3）。"""
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _rh: service.retry_job(x_tenant_id, job_id, body,
                                                 command_id))


# ------------------------------------------------------------------ 查询

@app.get("/image-jobs")
def list_image_jobs(x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
                    shot_lineage_id: str | None = Query(default=None),
                    node_kind: str | None = Query(default=None),
                    state: str | None = Query(default=None),
                    limit: int = Query(default=100, ge=1, le=500)):
    return service.list_jobs(_tenant(x_tenant_id),
                             shot_lineage_id=shot_lineage_id,
                             node_kind=node_kind, state=state, limit=limit)


@app.get("/image-jobs/{job_id}")
def get_image_job(job_id: str,
                  x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id")):
    return service.get_job(_tenant(x_tenant_id), job_id)


@app.get("/shots/{shot_lineage_id}/image-checkpoint")
def get_image_checkpoint(shot_lineage_id: str,
                         x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id")):
    """S30 的主读口。**不含 accepted 语义**——只回答"该拿哪张图"。"""
    return service.get_checkpoint(_tenant(x_tenant_id), shot_lineage_id)


@app.get("/keyframe-candidates/{candidate_id}")
def get_keyframe_candidate(candidate_id: str,
                           x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id")):
    """可追溯验收的读口：候选 + 完整参考链（§6.3）。"""
    return service.get_candidate(_tenant(x_tenant_id), candidate_id)


@app.get("/health/readiness")
def readiness():
    row = _conn.execute("SELECT 1").fetchone()
    return {"ready": row is not None, "contract_gaps": CONTRACT_GAPS}
