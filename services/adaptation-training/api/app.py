"""S48 HTTP 层。**只做协议转换**：校验、租户绑定、幂等、错误映射。

业务判断全在内核。这里不做任何"看起来合理就放行"的补偿：内核说 409，
这里就是 409。三道必须在这里做的边界：

1. `Idempotency-Key` 必填，且与 body 的 `command_id` 一致。不一致是 400，
   不是"以 header 为准"——那会让两套键各自重放。
2. `X-Tenant-Id` 必填，且 body 里的 `tenant_id` / `context.tenant_id` 若存在
   必须与它相等（403 TENANT_SCOPE_DENIED）。**租户上下文由平台签名绑定**，
   body 里的一律视为可疑输入，只用来发现冲突，绝不用来赋值。
3. 生产默认关：`S48_ENABLED` 未设置时 `/training-jobs` 返回 403
   `TRAINING_NOT_ENABLED`。读接口照常可用，便于运维核对历史。
"""

from __future__ import annotations

import json
import os
import sqlite3

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from idempotency_sdk import (CommandConflict, CommandInProgress,
                             SqliteIdempotencyStore)

from adaptation_training import (CONTRACT_GAPS, AdaptationTrainingService,
                                 ServiceError, stable_request_hash)

DB_PATH = os.environ.get("S48_DB", ":memory:")
# 生产开关默认关（docs/16 §3）。测试与验收会话显式打开。
TRAINING_ENABLED = os.environ.get("S48_ENABLED", "0") == "1"
# 上游地址未配置时，下面的 builder 会返回一个抛 503 的 callable。
# **不是静默降级**：这个服务没有上游就回答不了自己的问题。
S28_URL = os.environ.get("S28_URL")
S24_URL = os.environ.get("S24_URL")
S23_URL = os.environ.get("S23_URL")
S22_URL = os.environ.get("S22_URL")
S11_URL = os.environ.get("S11_URL")
S15_URL = os.environ.get("S15_URL")
HOME_CELL = os.environ.get("S48_HOME_CELL", "cell_a1")
HOME_REGION = os.environ.get("S48_HOME_REGION")

app = FastAPI(title="S48 adaptation-training", version="0.1.0")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


_conn = _connect()
_idem = SqliteIdempotencyStore(_conn)


def _unconfigured(code: str):
    def call(*_args, **_kwargs):
        raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                           f"upstream for {code} is not configured",
                           retryable=True)
    return call


service = AdaptationTrainingService(
    _conn,
    rights_source=_unconfigured("rights"),
    base_model_source=_unconfigured("base_model"),
    budget_source=_unconfigured("budget"),
    permit_source=_unconfigured("permit"),
    artifact_source=_unconfigured("artifact"),
    registry_sink=None,
    training_enabled=TRAINING_ENABLED,
    home_cell=HOME_CELL,
)


def _err(status: int, code: str, message: str, *, retryable: bool = False,
         details: dict | None = None, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message,
                           "retryable": retryable, "details": details or {}}},
        headers=headers)


@app.exception_handler(ServiceError)
async def _service_error(_request: Request, exc: ServiceError):
    headers = None
    if exc.status == 429:
        retry = (exc.details or {}).get("retry_after_seconds", 60)
        headers = {"Retry-After": str(retry)}
    return _err(exc.status, exc.code, str(exc), retryable=exc.retryable,
                details=exc.details, headers=headers)


@app.exception_handler(CommandConflict)
async def _key_reused(_request: Request, exc: CommandConflict):
    # 同键不同 body：**拒绝**，不覆盖（AGENTS.md 的幂等纪律）。
    return _err(409, "IDEMPOTENCY_KEY_REUSED",
                "this idempotency key was used with a different body",
                details={"command_id": exc.command_id,
                         "stored_hash": exc.stored_hash,
                         "incoming_hash": exc.incoming_hash,
                         "sdk_code": exc.code})


@app.exception_handler(CommandInProgress)
async def _in_progress(_request: Request, exc: CommandInProgress):
    return _err(409, "COMMAND_IN_PROGRESS",
                "the same command is still running", retryable=True,
                headers={"Retry-After": "1"},
                details={"sdk_code": exc.code})


def _tenant(header_value: str | None) -> str:
    if not header_value:
        raise ServiceError(403, "TENANT_SCOPE_DENIED",
                           "X-Tenant-Id is required: tenant context comes from "
                           "the platform, never from the body")
    return header_value


def _body(body: dict, tenant_id: str, idempotency_key: str) -> dict:
    if not isinstance(body, dict):
        raise ServiceError(400, "MALFORMED_REQUEST", "body must be a JSON object")
    if not idempotency_key:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key header is required")
    body_command = body.get("command_id")
    if body_command is not None and body_command != idempotency_key:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "body.command_id must equal the Idempotency-Key header",
                           details={"command_id": body_command,
                                    "header": idempotency_key})
    # 租户只用于**发现冲突**，不用于赋值。
    for candidate in (body.get("tenant_id"),
                      (body.get("context") or {}).get("tenant_id")
                      if isinstance(body.get("context"), dict) else None):
        if candidate is not None and candidate != tenant_id:
            raise ServiceError(403, "TENANT_SCOPE_DENIED",
                               "body tenant disagrees with the signed context")
    return body


def _home_region(body: dict, header_value: str | None) -> str | None:
    """C48-23：body.context.home_region → X-Home-Region → 不猜。

    地域进了事件与审计，猜错会让数据落错辖区。都没有就落 `unspecified`，
    由内核写进 NOT NULL 列，而不是从别的字段推。
    """
    context = body.get("context")
    if isinstance(context, dict) and isinstance(context.get("home_region"), str):
        return context["home_region"]
    return header_value or HOME_REGION


def _command(tenant_id: str, idempotency_key: str, body: dict, produce):
    digest = stable_request_hash(body)
    try:
        record = _idem.begin(tenant_id, idempotency_key, digest)
    except sqlite3.OperationalError:
        # 幂等表锁竞争在这里发生：命令还没开始，重试是安全的。
        raise ServiceError(503, "S48_UNAVAILABLE",
                           "idempotency store unavailable", retryable=True)
    if record.result is not None:
        # `begin` 返回 `CommandRecord`；重放的是它 `.result` 里的 JSON 文本，
        # 不是 record 本身。
        return {**json.loads(record.result), "replayed": True}
    try:
        result = produce()
    except BaseException:
        # 内核事务已回滚；这里只清掉"进行中"的占位，让同键可以重试。
        try:
            _idem.abort(tenant_id, idempotency_key)
        except sqlite3.OperationalError:
            pass
        raise
    try:
        # `finish` 收的是 JSON 文本，不是 dict——传 dict 会在绑定参数时抛
        # ProgrammingError，而那时业务事务已经提交，看上去像 500 假失败。
        _idem.finish(tenant_id, idempotency_key,
                     json.dumps(result, ensure_ascii=False))
    except sqlite3.OperationalError:
        raise ServiceError(503, "S48_UNAVAILABLE",
                           "idempotency store unavailable after commit; "
                           "retry with the same key", retryable=True)
    return {**result, "replayed": False}


# ---------------------------------------------------------------- 命令


@app.post("/training-jobs", status_code=201)
async def create_job(
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
    x_home_region: str = Header(default="", alias="X-Home-Region"),
):
    tenant_id = _tenant(x_tenant_id)
    body = _body(await request.json(), tenant_id, idempotency_key)
    return _command(
        tenant_id, idempotency_key, body,
        lambda: service.create_job(
            tenant_id, body, idempotency_key, stable_request_hash(body),
            requested_by=body.get("context") or {"actor": "unknown"},
            home_region=_home_region(body, x_home_region)))


@app.post("/training-jobs/{job_id}:cancel")
async def cancel_job(
    job_id: str, request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    tenant_id = _tenant(x_tenant_id)
    body = _body(await request.json(), tenant_id, idempotency_key)
    return _command(
        tenant_id, idempotency_key, body,
        lambda: service.cancel(tenant_id, job_id, reason=body.get("reason"),
                               correlation_id=idempotency_key))


@app.post("/training-jobs/{job_id}:accept")
async def accept_job(
    job_id: str, request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    tenant_id = _tenant(x_tenant_id)
    body = _body(await request.json(), tenant_id, idempotency_key)
    return _command(
        tenant_id, idempotency_key, body,
        lambda: service.accept(tenant_id, job_id,
                               decision_ref=body.get("decision_ref"),
                               correlation_id=idempotency_key))


@app.post("/training-jobs/{job_id}:reject")
async def reject_job(
    job_id: str, request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    tenant_id = _tenant(x_tenant_id)
    body = _body(await request.json(), tenant_id, idempotency_key)
    return _command(
        tenant_id, idempotency_key, body,
        lambda: service.reject_candidate(tenant_id, job_id,
                                         reason=body.get("reason"),
                                         correlation_id=idempotency_key))


# 下面是 runner 的推进端点（C48-17）。它们也在命令通道里：重复投递要幂等。
@app.post("/training-jobs/{job_id}:admit")
async def admit_job(
    job_id: str, request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    tenant_id = _tenant(x_tenant_id)
    body = _body(await request.json(), tenant_id, idempotency_key)
    return _command(tenant_id, idempotency_key, body,
                    lambda: service.admit(tenant_id, job_id,
                                          correlation_id=idempotency_key))


@app.post("/training-jobs/{job_id}:prepare")
async def prepare_job(
    job_id: str, request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    tenant_id = _tenant(x_tenant_id)
    body = _body(await request.json(), tenant_id, idempotency_key)
    return _command(tenant_id, idempotency_key, body,
                    lambda: service.prepare(tenant_id, job_id,
                                            correlation_id=idempotency_key))


@app.post("/training-jobs/{job_id}:start")
async def start_job(
    job_id: str, request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    tenant_id = _tenant(x_tenant_id)
    body = _body(await request.json(), tenant_id, idempotency_key)
    return _command(tenant_id, idempotency_key, body,
                    lambda: service.start(tenant_id, job_id,
                                          correlation_id=idempotency_key))


@app.post("/training-jobs/{job_id}:progress")
async def progress_job(
    job_id: str, request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    tenant_id = _tenant(x_tenant_id)
    body = _body(await request.json(), tenant_id, idempotency_key)
    return _command(
        tenant_id, idempotency_key, body,
        lambda: service.progress(tenant_id, job_id,
                                 gpu_seconds=int(body.get("gpu_seconds") or 0),
                                 checkpoint_ref=body.get("checkpoint_ref")))


@app.post("/training-jobs/{job_id}:resume")
async def resume_job(
    job_id: str, request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    tenant_id = _tenant(x_tenant_id)
    body = _body(await request.json(), tenant_id, idempotency_key)
    return _command(
        tenant_id, idempotency_key, body,
        lambda: service.resume(tenant_id, job_id,
                               checkpoint_ref=body.get("checkpoint_ref") or "",
                               expected_checkpoint=body.get("expected_checkpoint"),
                               correlation_id=idempotency_key))


@app.post("/training-jobs/{job_id}:finish")
async def finish_job(
    job_id: str, request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    tenant_id = _tenant(x_tenant_id)
    body = _body(await request.json(), tenant_id, idempotency_key)
    return _command(
        tenant_id, idempotency_key, body,
        lambda: service.finish(
            tenant_id, job_id,
            outcome=body.get("outcome") or "trained",
            weights_blob_ref=body.get("weights_blob_ref"),
            weights_hash=body.get("weights_hash"),
            size_bytes=body.get("size_bytes"),
            gpu_seconds=int(body.get("gpu_seconds") or 0),
            checkpoint_ref=body.get("checkpoint_ref"),
            reason=body.get("reason"),
            correlation_id=idempotency_key))


@app.post("/training-jobs/{job_id}:evaluate")
async def evaluate_job(
    job_id: str, request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    tenant_id = _tenant(x_tenant_id)
    body = _body(await request.json(), tenant_id, idempotency_key)
    return _command(
        tenant_id, idempotency_key, body,
        lambda: service.evaluate(
            tenant_id, job_id,
            metrics=body.get("metrics") or {},
            passed=body.get("passed"),
            thresholds=body.get("thresholds"),
            infrastructure_failure=bool(body.get("infrastructure_failure")),
            correlation_id=idempotency_key))


@app.post("/events/rights")
async def rights_event(
    request: Request,
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    """S11–S15 的撤销/暂停投放。**至少一次**，靠 inbox 去重。"""
    tenant_id = _tenant(x_tenant_id)
    envelope = await request.json()
    return service.on_rights_revoked(tenant_id, envelope)


@app.post("/events/base-model")
async def base_event(
    request: Request,
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    """S24 的基座退役投放。"""
    tenant_id = _tenant(x_tenant_id)
    envelope = await request.json()
    return service.on_base_retired(tenant_id, envelope)


# ---------------------------------------------------------------- 查询


@app.get("/training-jobs/{job_id}")
async def get_job(job_id: str,
                  x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_job(_tenant(x_tenant_id), job_id)


@app.get("/training-jobs")
async def list_jobs(
    state: str | None = Query(default=None),
    created_from: str | None = Query(default=None, alias="from"),
    created_to: str | None = Query(default=None, alias="to"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    return service.list_jobs(_tenant(x_tenant_id), state=state,
                             created_from=created_from, created_to=created_to,
                             cursor=cursor, limit=limit)


@app.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: str,
                      x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.get_dataset(_tenant(x_tenant_id), dataset_id)


@app.get("/datasets/{dataset_id}/items")
async def get_dataset_items(
    dataset_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    return service.get_dataset_items(_tenant(x_tenant_id), dataset_id,
                                     cursor=cursor, limit=limit)


@app.get("/adaptation-artifacts/{artifact_id}")
async def get_artifact(artifact_id: str,
                       x_tenant_id: str = Header(default="",
                                                 alias="X-Tenant-Id")):
    return service.get_artifact(_tenant(x_tenant_id), artifact_id)


@app.get("/adaptation-artifacts/{artifact_id}/compatibility")
async def get_compatibility(artifact_id: str,
                            x_tenant_id: str = Header(default="",
                                                      alias="X-Tenant-Id")):
    return service.get_compatibility(_tenant(x_tenant_id), artifact_id)


@app.get("/retention-checks")
async def retention_check(
    kind: str = Query(...), identifier: str = Query(..., alias="id"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
):
    """§10.3：S28 删除候选字节前问本服务。"""
    return service.retention_check(_tenant(x_tenant_id), kind=kind,
                                   identifier=identifier)


@app.get("/training-jobs/{job_id}/audit")
async def job_audit(job_id: str,
                    x_tenant_id: str = Header(default="", alias="X-Tenant-Id")):
    return service.list_audit(_tenant(x_tenant_id), job_id)


@app.get("/health/readiness")
async def readiness() -> Response:
    payload = service.readiness()
    return JSONResponse(content={**payload,
                                 "contract_gaps": CONTRACT_GAPS,
                                 "production_switch_on": TRAINING_ENABLED})
