"""S33 HTTP 层。

契约面（`contracts/openapi.v2.yaml`）没有 S33 的路径，catalog 只声明了
`POST /sfx-jobs`（命令）与 `GET /sfx-cues/{id}`（查询）。其余都是方案 §6.3
自己提的（缺口 C8：命令面与查询面**故意不对称**——生产的入口是"给一条 cue
意图"，读的入口是"这条 cue 现在什么样"）。按 `service.yaml` 走，不改生成物。

租户一律来自 `X-Tenant-Id`（平台签名上下文）。请求体里的 `tenant_id` 只用于
比对，不一致即 403 —— AGENTS.md 禁止把请求体里的 `tenant_id` 当可信输入。
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

from sound_effects.deps import (
    build_artifact_check,
    build_canceler,
    build_edl_provider,
    build_loop_probe,
    build_rights_resolver,
    build_route_resolver,
    build_submitter,
)
from sound_effects.schema import CONTRACT_GAPS
from sound_effects.service import SfxService, ServiceError

DB_PATH = os.environ.get("S33_DB", ":memory:")

app = FastAPI(title="S33 sound-effects", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = SfxService(
    _conn,
    rights_resolver=build_rights_resolver(os.environ.get("S02_BASE_URL")),
    route_resolver=build_route_resolver(os.environ.get("S25_BASE_URL")),
    submitter=build_submitter(os.environ.get("S26_BASE_URL")),
    artifact_check=build_artifact_check(os.environ.get("S28_BASE_URL")),
    edl_provider=build_edl_provider(os.environ.get("S41_BASE_URL")),
    loop_probe=build_loop_probe(os.environ.get("S42_BASE_URL")),
    canceler=build_canceler(os.environ.get("S26_BASE_URL")),
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

@app.post("/sfx-jobs", status_code=202)
def create_sfx_job(payload: dict,
                   command_id: str = Header(alias="Idempotency-Key"),
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    result = command(x_tenant_id, body,
                     lambda: service.create_job(body, x_tenant_id,
                                                command_id=command_id))
    # 复用走的不是"新生产"，语义上已经是 200；这里按 §6.1 区分。
    if result.get("reused"):
        return JSONResponse(status_code=200, content=result)
    return result


@app.post("/sfx-cues/{cue_id}/placement:recompute")
def recompute_placement(cue_id: str,
                        payload: dict | None = None,
                        edl_revision: int = Query(..., alias="edl_revision"),
                        command_id: str = Header(default="",
                                                 alias="Idempotency-Key"),
                        x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    # 纯计算，不落业务事实、不花钱。仍走幂等外壳是为了让重放的响应一致。
    body = _body({**(payload or {}), "edl_revision": edl_revision},
                 command_id or f"recompute-{cue_id}-{edl_revision}", x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.recompute_placement(
                       x_tenant_id, cue_id, edl_revision))


@app.post("/sfx-gaps/{gap_id}:waive")
def waive_gap(gap_id: str, payload: dict,
              command_id: str = Header(alias="Idempotency-Key"),
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.waive_gap(x_tenant_id, gap_id, body,
                                             command_id=command_id))


@app.post("/sfx-jobs/{job_id}:cancel")
def cancel_job(job_id: str, payload: dict | None = None,
               command_id: str = Header(alias="Idempotency-Key"),
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.cancel_job(x_tenant_id, job_id, body,
                                              command_id=command_id))


@app.post("/sfx-jobs/{job_id}:retry")
def retry_job(job_id: str, payload: dict | None = None,
              command_id: str = Header(alias="Idempotency-Key"),
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload or {}, command_id, x_tenant_id)
    return command(x_tenant_id, body,
                   lambda: service.retry_job(x_tenant_id, job_id, body,
                                             command_id=command_id))


# ------------------------------------------------------------------ 查询

@app.get("/sfx-cues/{cue_id}")
def get_cue(cue_id: str,
            x_tenant_id: str = Header(alias="X-Tenant-Id"),
            cue_revision: int | None = Query(default=None),
            edl_revision: int | None = Query(default=None)):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.get_cue(x_tenant_id, cue_id, cue_revision=cue_revision,
                           edl_revision=edl_revision)


@app.get("/sfx-cues")
def list_cues(x_tenant_id: str = Header(alias="X-Tenant-Id"),
              shot_lineage_id: str | None = Query(default=None),
              episode_id: str | None = Query(default=None),
              family: str | None = Query(default=None),
              has_gap: bool | None = Query(default=None),
              edl_revision: int | None = Query(default=None)):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    if not shot_lineage_id and not episode_id:
        # 无范围的全表扫描在数据长大后就是事故；强制收窄。
        raise ServiceError(422, "QUERY_SCOPE_REQUIRED",
                           "one of shot_lineage_id or episode_id is required")
    return service.list_cues(x_tenant_id, shot_lineage_id=shot_lineage_id,
                             episode_id=episode_id, family=family,
                             has_gap=has_gap, edl_revision=edl_revision)


@app.get("/shots/{shot_lineage_id}/sfx-checkpoint")
def shot_checkpoint(shot_lineage_id: str,
                    x_tenant_id: str = Header(alias="X-Tenant-Id")):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    return service.get_shot_checkpoint(x_tenant_id, shot_lineage_id)


@app.get("/sfx-takes/{take_id}")
def get_take(take_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id"),
             recheck_rights: bool = Query(default=False)):
    if not x_tenant_id:
        raise ServiceError(400, "INVALID_BODY", "X-Tenant-Id is required")
    if recheck_rights:
        # §4.5：每一次使用都复查许可。这是知情的副作用，且只会收紧。
        return service.recheck_take_rights(x_tenant_id, take_id)
    return service.get_take(x_tenant_id, take_id)


@app.get("/health/readiness")
def readiness():
    rows = _conn.execute("SELECT 1").fetchone()
    return {"ready": rows is not None, "contract_gaps": CONTRACT_GAPS}
