"""S45 · 审计与证据保全 · HTTP 入口。方案：`plans/S45-audit.md` §4。

路由分三类，权限语义各不相同：

- **写入**（`POST /audit-records` 与内部事件入口）：**不问 S01**。§2.4 说
  写入方自己已经判定过动作是否被允许，S45 只记录它。这也是本服务被称为
  "可降级依赖"的落点——S01 抖动不该让所有业务写入停摆。
- **读取 / 导出 / 销毁 / 改保留策略**：必须过 S01。缺口 NEW-1 意味着真实
  S01 上这四个动作现在一律 503 `PERMISSION_KEY_UNREGISTERED`——那是契约
  现状，不是 403 的另一种写法。
- **健康**：`/health/readiness` 带出 `contract_gaps`，免得"能响应"被读成
  "契约完整"。

`POST /audit-records` 的幂等生命周期放在 `command()` 里（与 S25 同构）：
占位 → 执行 → 落结果；同 hash 重放首次结果、异 hash 409。重放**不重新
计算链**——记录已经在链上了，再算一遍等于想追加第二条。
"""

from __future__ import annotations

import json
import os
import sqlite3

from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from idempotency_sdk import (CommandConflict, CommandInProgress,
                             SqliteIdempotencyStore, canonical_request_hash)

from audit.deps import build_authorization_source, build_redaction_scanner
from audit.schema import CONTRACT_GAPS, SCHEMA  # noqa: F401
from audit.service import AuditService, ServiceError

DB_PATH = os.environ.get("S45_DB", ":memory:")

app = FastAPI(title="S45 audit", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

_KEY = os.environ.get("S45_REDACT_KEY")
# readiness 要报"现在用的是哪个扫描器"；nil 判断在这里定，不在请求里重算。
_SCANNER_IS_LOCAL = not os.environ.get("S45_SCANNER_URL")
service = AuditService(
    _conn,
    authorization_source=build_authorization_source(
        os.environ.get("S01_BASE_URL")),
    scanner=build_redaction_scanner(os.environ.get("S45_SCANNER_URL")),
    # C45-7 / R-1 未裁决：没有 KMS 派生键时给 None，脱敏退化为
    # `REDACTED:<class>`（不可关联，但也不可观测量）。测试用固定键。
    redact_key=_KEY.encode("utf-8") if _KEY else None,
)
_idem = SqliteIdempotencyStore(_conn)


def _err(status: int, code: str, message: str, retryable: bool = False,
         details: dict | None = None, retry_after: int | None = None):
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse(
        status_code=status, headers=headers,
        content={"code": code, "message": message, "retryable": retryable,
                 "correlation_id": "", "details": details or {}})


@app.exception_handler(ServiceError)
async def _service_error(_request, exc: ServiceError):
    return _err(exc.status, exc.code, str(exc), exc.retryable, exc.details,
                exc.retry_after)


@app.exception_handler(CommandConflict)
async def _conflict(_request, exc: CommandConflict):
    # 同键不同 body：409，不是覆盖（AGENTS.md）。
    return _err(409, exc.code, str(exc), False)


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return _err(409, "COMMAND_IN_PROGRESS", str(exc), True, retry_after=1)


def _body(payload, command_id: str, tenant_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ServiceError(400, "MALFORMED_REQUEST", "body must be a JSON object")
    if not command_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "Idempotency-Key is required")
    if not tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    inside = payload.get("command_id")
    if inside is not None and inside != command_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key does not match body command_id")
    # 租户上下文必须由平台签名绑定；body 里的 tenant_id 只是自报值。
    candidate = payload.get("tenant_id")
    if candidate is not None and candidate != tenant_id:
        raise ServiceError(403, "TENANT_SCOPE_DENIED",
                           "body tenant_id disagrees with the signed context")
    return payload


def _principal(x_principal_id: str | None) -> dict:
    if not x_principal_id:
        raise ServiceError(401, "UNAUTHENTICATED",
                           "X-Principal-Id is required")
    return {"principal_id": x_principal_id, "principal_type": "human"}


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放路径**不重新追加**：重复投递同一条意图时，链上已有的那一行就是答案；
    再算一次等价于把同一事实记两遍。
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
        # 占位行必须撤掉，否则下一次同键重试会看到 NULL result 而报"进行中"。
        _idem.abort(tenant_id, command_id)
        raise
    _idem.finish(tenant_id, command_id, json.dumps(result, ensure_ascii=False))
    return {**result, "replayed": False}


# ------------------------------------------------------------------ 写入

@app.post("/audit-records", status_code=201)
def append_record(payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.record(
                       x_tenant_id, command_id, body, request_hash))


@app.post("/internal/audit-intents")
def consume_audit_intent(payload: dict,
                         x_producer_id: str | None = Header(
                             default=None, alias="X-Producer-Id")):
    """内部事件入口（§2.1 主路径）。

    `X-Producer-Id` 是本服务**能拿到的全部** producer 身份线索；NEW-4 说明
    它既不在 `EventEnvelope` 里，也没有签名。给了就比对，没给就如实返回
    `producer_verified: false`——不假装核对过。
    """
    result = service.consume_intent(payload, producer_id=x_producer_id)
    return {**result, "producer_verified_gap": "NEW-4"}


@app.post("/audit-exports", status_code=202)
def create_export(payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id"),
                  x_principal_id: str | None = Header(default=None,
                                                      alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    principal = _principal(x_principal_id)
    return command(x_tenant_id, body, command_id,
                   lambda request_hash: service.create_export(
                       x_tenant_id, command_id, body, request_hash,
                       principal=principal))


@app.post("/audit-exports/{export_id}:verify")
def verify_export(export_id: str,
                  x_tenant_id: str = Header(alias="X-Tenant-Id"),
                  x_principal_id: str | None = Header(default=None,
                                                      alias="X-Principal-Id")):
    return service.verify_export(x_tenant_id, export_id,
                                 principal=_principal(x_principal_id))


@app.post("/retention-policies/{tenant_id}")
def put_retention_policy(tenant_id: str, payload: dict,
                         x_tenant_id: str = Header(alias="X-Tenant-Id"),
                         x_principal_id: str | None = Header(
                             default=None, alias="X-Principal-Id")):
    if tenant_id != x_tenant_id:
        raise ServiceError(403, "TENANT_SCOPE_DENIED",
                           "path tenant_id disagrees with the signed context")
    return service.put_retention_policy(x_tenant_id, payload,
                                        principal=_principal(x_principal_id))


@app.post("/legal-holds", status_code=201)
def create_legal_hold(payload: dict,
                      x_tenant_id: str = Header(alias="X-Tenant-Id"),
                      x_principal_id: str | None = Header(
                          default=None, alias="X-Principal-Id")):
    return service.create_legal_hold(x_tenant_id, payload,
                                     principal=_principal(x_principal_id))


@app.post("/legal-holds/{hold_id}:release")
def release_legal_hold(hold_id: str,
                       x_tenant_id: str = Header(alias="X-Tenant-Id"),
                       x_principal_id: str | None = Header(
                           default=None, alias="X-Principal-Id")):
    return service.release_legal_hold(x_tenant_id, hold_id,
                                      principal=_principal(x_principal_id))


@app.post("/destruction-orders", status_code=202)
def create_destruction_order(payload: dict,
                             x_tenant_id: str = Header(alias="X-Tenant-Id"),
                             x_principal_id: str | None = Header(
                                 default=None, alias="X-Principal-Id")):
    return service.create_destruction_order(
        x_tenant_id, payload, principal=_principal(x_principal_id))


@app.post("/destruction-orders/{order_id}:execute")
def execute_destruction_order(order_id: str,
                              x_tenant_id: str = Header(alias="X-Tenant-Id"),
                              x_principal_id: str | None = Header(
                                  default=None, alias="X-Principal-Id")):
    return service.execute_destruction_order(
        x_tenant_id, order_id, principal=_principal(x_principal_id))


# ------------------------------------------------------------------ 查询

@app.get("/audit-records")
def query_records(x_tenant_id: str = Header(alias="X-Tenant-Id"),
                  x_principal_id: str | None = Header(default=None,
                                                      alias="X-Principal-Id"),
                  stream_key: str | None = Query(default=None),
                  from_seq: int | None = Query(default=None),
                  to_seq: int | None = Query(default=None),
                  action: str | None = Query(default=None),
                  actor: str | None = Query(default=None),
                  outcome: str | None = Query(default=None),
                  occurred_from: str | None = Query(default=None),
                  occurred_to: str | None = Query(default=None),
                  purpose: str | None = Query(default=None),
                  limit: int = Query(default=50),
                  cursor: str | None = Query(default=None)):
    principal = _principal(x_principal_id)
    service._require(x_tenant_id, principal["principal_id"], "tenant",
                     x_tenant_id, "audit.read")
    return service.query_records(
        x_tenant_id, principal=principal, stream_key=stream_key,
        from_seq=from_seq, to_seq=to_seq, action=action, actor=actor,
        outcome=outcome, occurred_from=occurred_from,
        occurred_to=occurred_to, limit=limit, cursor=cursor, purpose=purpose)


@app.get("/audit-records/{record_id}")
def get_record(record_id: str,
               x_tenant_id: str = Header(alias="X-Tenant-Id"),
               x_principal_id: str | None = Header(default=None,
                                                   alias="X-Principal-Id"),
               purpose: str | None = Query(default=None)):
    principal = _principal(x_principal_id)
    service._require(x_tenant_id, principal["principal_id"], "tenant",
                     x_tenant_id, "audit.read")
    return service.get_record(x_tenant_id, record_id, principal=principal,
                              purpose=purpose)


@app.get("/records/by-target")
def by_target(service_id: str = Query(...), kind: str = Query(...),
              target_id: str = Query(...),
              occurred_from: str | None = Query(default=None),
              occurred_to: str | None = Query(default=None),
              limit: int = Query(default=50),
              x_tenant_id: str = Header(alias="X-Tenant-Id"),
              x_principal_id: str | None = Header(default=None,
                                                  alias="X-Principal-Id")):
    principal = _principal(x_principal_id)
    service._require(x_tenant_id, principal["principal_id"], "tenant",
                     x_tenant_id, "audit.read")
    return service.by_target(x_tenant_id, service_id=service_id, kind=kind,
                             target_id=target_id,
                             occurred_from=occurred_from,
                             occurred_to=occurred_to, principal=principal,
                             limit=limit)


@app.get("/audit-exports/{export_id}")
def get_export(export_id: str,
               x_tenant_id: str = Header(alias="X-Tenant-Id"),
               x_principal_id: str | None = Header(default=None,
                                                   alias="X-Principal-Id")):
    principal = _principal(x_principal_id)
    service._require(x_tenant_id, principal["principal_id"], "tenant",
                     x_tenant_id, "audit.export")
    return service.get_export(x_tenant_id, export_id, principal=principal)


@app.get("/segments")
def list_segments(stream_key: str | None = Query(default=None),
                  state: str | None = Query(default=None),
                  limit: int = Query(default=100),
                  x_tenant_id: str = Header(alias="X-Tenant-Id"),
                  x_principal_id: str | None = Header(default=None,
                                                      alias="X-Principal-Id")):
    principal = _principal(x_principal_id)
    service._require(x_tenant_id, principal["principal_id"], "tenant",
                     x_tenant_id, "audit.read")
    return service.list_segments(x_tenant_id, stream_key=stream_key,
                                 state=state, limit=limit)


@app.get("/segments/{segment_id}/proof")
def segment_proof(segment_id: str,
                  x_tenant_id: str = Header(alias="X-Tenant-Id"),
                  x_principal_id: str | None = Header(default=None,
                                                      alias="X-Principal-Id")):
    principal = _principal(x_principal_id)
    service._require(x_tenant_id, principal["principal_id"], "tenant",
                     x_tenant_id, "audit.read")
    return service.segment_proof(x_tenant_id, segment_id)


@app.get("/retention-checks")
def retention_check(service_id: str = Query(...), kind: str = Query(...),
                    target_id: str = Query(...),
                    x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """S28 删除前的 `REFERENCE_CHECK`。服务间调用，不带 principal。

    这里不过 S01：调用方是 S28 自己的编排，它已经过了自己的授权；本接口
    只回答"审计里还有没有引用"。回的是事实，不是权限判定。
    """
    return service.retention_check(x_tenant_id, service_id=service_id,
                                   kind=kind, target_id=target_id)


@app.get("/health/readiness")
def readiness():
    checks = {"schema": True, "sink": True}
    try:
        _conn.execute("SELECT 1 FROM audit_record LIMIT 1").fetchone()
    except sqlite3.Error:
        checks["schema"] = False
    # 扫描器**总有**一个：上游配了用上游，没配用内建规则。所以这里不报
    # 布尔值——那个值恒为真，等于没检查。报的是"现在用的是哪一个"，
    # 上游掉线时会从 `upstream` 变回 `local_fallback`，可被告警抓到。
    checks["redaction_scanner"] = (
        "local_fallback" if _SCANNER_IS_LOCAL else "upstream")
    checks["authorization_source"] = service.authorization_source is not None
    return {
        "ready": checks["schema"] and checks["sink"],
        "checks": checks,
        "contract_gaps": CONTRACT_GAPS,
    }
