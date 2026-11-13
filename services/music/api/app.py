"""S34 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）里**没有 music 的路径**（缺口 MUS-11），
所以这一层按 `plans/S34-music.md` §6.1–§6.3 实现。契约与方案不一致的地方一律
登记在 `CONTRACT_GAPS`，不在这里自行裁决，也不改契约。

**租户一律来自 `X-Tenant-Id`**；请求体里的 `tenant_id` 只用于比对（不一致
403）。AGENTS.md：LLM 提供的 `tenant_id` 不是可信输入。

三条与 S33/S25 同构、但这里更要紧的规矩：

1. **一个付费命令一个事务**：`create_job` 内部的 `_command` 把结果写回同一个
   `idempotency_record` 行，提交与"记下已提交"原子成立。否则存在"业务已提交、
   幂等未记录"的窗口，重试会再花一次钱。
2. **重放不重新路由**：S25 的报价两分钟就变，重放时再路由一次等于把一个新报价
   塞进旧 attempt。
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

from music.deps import build_all
from music.schema import CONTRACT_GAPS
from music.service import MusicService, ServiceError

DB_PATH = os.environ.get("S34_DB", ":memory:")

app = FastAPI(title="S34 music", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = MusicService(_conn, **build_all({
    "S02": os.environ.get("S02_BASE_URL"),
    "S06": os.environ.get("S06_BASE_URL"),
    "S23": os.environ.get("S23_BASE_URL"),
    "S25": os.environ.get("S25_BASE_URL"),
    "S26": os.environ.get("S26_BASE_URL"),
    "S28": os.environ.get("S28_BASE_URL"),
    "S41": os.environ.get("S41_BASE_URL"),
    "S42": os.environ.get("S42_BASE_URL"),
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

    重放路径**不重新执行**：音乐生成是付费的，重放一次就是再花一次钱；而且
    S25 的报价会过期、S26 的 attempt 已经存在，重跑只会得到一串 409。
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

@app.post("/music-jobs")
def create_music_job(payload: dict,
                     command_id: str = Header(alias="Idempotency-Key"),
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """§6.1。**付费入口**：内部只走 S26，不直连任何模型。"""
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _rh: service.create_job(body, x_tenant_id,
                                                  command_id))


@app.post("/music-selections")
def create_selection(payload: dict,
                     command_id: str = Header(alias="Idempotency-Key"),
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _rh: service.create_selection(x_tenant_id, body,
                                                        command_id))


@app.post("/music-selections/{selection_id}:reject-all")
def reject_all(selection_id: str, payload: dict | None = None,
               command_id: str = Header(alias="Idempotency-Key"),
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _rh: service.reject_all(x_tenant_id, selection_id,
                                                  body, command_id))


@app.post("/music-gaps/{gap_id}:waive")
def waive_gap(gap_id: str, payload: dict | None = None,
              command_id: str = Header(alias="Idempotency-Key"),
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _rh: service.waive_gap(x_tenant_id, gap_id, body,
                                                  command_id))


@app.post("/music-jobs/{job_id}:cancel")
def cancel_job(job_id: str, payload: dict | None = None,
               command_id: str = Header(alias="Idempotency-Key"),
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _rh: service.cancel_job(x_tenant_id, job_id, body,
                                                  command_id))


@app.post("/music-jobs/{job_id}:retry")
def retry_job(job_id: str, payload: dict | None = None,
              command_id: str = Header(alias="Idempotency-Key"),
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """只允许 `{FAILED, RECONCILED_FAILED}`；其余 409（§6.3）。"""
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _rh: service.retry_job(x_tenant_id, job_id, body,
                                                 command_id))


@app.post("/music-cues/{cue_id}/cut-plan:recompute")
def recompute_cut_plan(cue_id: str,
                       edl_revision: int | None = Query(default=None),
                       x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """**免费**、不碰 S26。剪辑发新版 EDL 就重算一次（§4.2 第 1/2 层）。"""
    return service.recompute_cut_plan(_tenant(x_tenant_id), cue_id,
                                      edl_revision)


# ------------------------------------------------------------------ 查询

@app.get("/music-cues")
def list_cues(x_tenant_id: str = Header(alias="X-Tenant-Id"),
              scene_lineage_id: str | None = Query(default=None),
              episode_id: str | None = Query(default=None),
              has_gap: bool | None = Query(default=None)):
    return service.list_cues(_tenant(x_tenant_id),
                             scene_lineage_id=scene_lineage_id,
                             episode_id=episode_id, has_gap=has_gap)


@app.get("/music-cues/{cue_id}")
def get_cue(cue_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id"),
            cue_revision: int | None = Query(default=None),
            edl_revision: int | None = Query(default=None)):
    return service.get_cue(_tenant(x_tenant_id), cue_id,
                           cue_revision=cue_revision,
                           edl_revision=edl_revision)


@app.get("/scenes/{scene_lineage_id}/music-checkpoint")
def get_scene_checkpoint(scene_lineage_id: str,
                         x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """S42 的主读口：该场景全部 cue 的基线（§6.3）。"""
    return service.get_scene_checkpoint(_tenant(x_tenant_id), scene_lineage_id)


@app.get("/music-takes/{take_id}")
def get_take(take_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_take(_tenant(x_tenant_id), take_id)


@app.get("/music-takes/{take_id}/license")
def get_take_license(take_id: str,
                     x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """S43 的专用读口。**不含音乐意图**——权限分离（§6.3 末段）。"""
    return service.get_take_license(_tenant(x_tenant_id), take_id)


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
