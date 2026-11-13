"""S15 HTTP 层。

契约面：`contracts/openapi.v2.yaml` **完全没有 `/voices` 路径**——S15 的整个
HTTP 面不在契约里，比"形状对不上"更进一步。这是契约缺口，不是本层可以裁决的
事，登记为 C15-1；本层按 `plans/S15-voice-assets.md` §5.1/§5.2 实现。

租户一律来自 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对（403）。
AGENTS.md：LLM 提供的 `tenant_id` 不能当可信输入。

两条与"拒绝语义"相关的约定，在本层显式写出来：

1. `POST /voices/{id}:check-eligibility` 对**任何不可用结论都返回 200**，
   `eligible=false` + `deny_reason`。它不抛 4xx（§5.4 末段、反模式 #11）：
   "这个音色现在不能用"是正常答案，调用方要靠它决定记录/重试/换预案。
   只有请求本身坏（缺字段、revision 不存在、跨租户）才 4xx。
2. 该接口**不走幂等记录**：只读语义的 POST，每次调用都必须读最新状态。
   给它套幂等就等于把"撤销之后仍然 eligible"缓存到重放里——恰好是这个服务
   存在的理由的反面（§4.2 第 1 条、§6.1）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse

from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from voice_assets.deps import build_authorizer, build_sample_scanner
from voice_assets.schema import CONTRACT_GAPS
from voice_assets.service import VoiceAssetService, ServiceError

DB_PATH = os.environ.get("S15_DB", ":memory:")

app = FastAPI(title="S15 voice-assets", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

service = VoiceAssetService(
    _conn,
    artifact_scanner=build_sample_scanner(os.environ.get("S28_BASE_URL")),
    authorizer=build_authorizer(os.environ.get("S01_BASE_URL")),
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
    # 同键不同 body：幂等键复用，409，不是覆盖。
    return _err(409, exc.code, str(exc), False)


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return JSONResponse(
        status_code=409, headers={"Retry-After": "1"},
        content={"code": "COMMAND_IN_FLIGHT", "message": str(exc),
                 "retryable": True, "correlation_id": "", "details": {}})


def _body(payload, command_id: str, tenant_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "body must be a JSON object")
    if not command_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key is required")
    if not tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    inside = payload.get("command_id")
    if inside is not None and inside != command_id:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "Idempotency-Key does not match body command_id")
    candidate = payload.get("tenant_id")
    if candidate is not None and candidate != tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return payload


def command(tenant_id: str, body: dict, command_id: str, execute) -> dict:
    """幂等占位 → 执行 → 落结果。同 hash 原样重放，异 hash 409。

    重放路径**不重新执行**：撤销/续约这类状态转移重放一次就多一个 `state_seq`，
    而下游（S02/S26）手里的 `if_state_seq` 会立刻变陈旧。
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


def _tenant(x_tenant_id: str) -> str:
    if not x_tenant_id:
        raise ServiceError(400, "MALFORMED_REQUEST", "X-Tenant-Id is required")
    return x_tenant_id


# ------------------------------------------------------------------ 命令

@app.post("/voices")
def register_voice(payload: dict,
                   command_id: str = Header(alias="Idempotency-Key"),
                   x_tenant_id: str = Header(alias="X-Tenant-Id"),
                   x_principal_id: str | None = Header(
                       default=None, alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.register_voice(
                       x_tenant_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}/revisions")
def add_revision(voice_id: str, payload: dict,
                 command_id: str = Header(alias="Idempotency-Key"),
                 x_tenant_id: str = Header(alias="X-Tenant-Id"),
                 x_principal_id: str | None = Header(
                     default=None, alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.add_revision(
                       x_tenant_id, voice_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}:check-eligibility")
def check_eligibility(voice_id: str, payload: dict,
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """**不走幂等**，也不调 S01：每次都必须读最新授权状态。"""
    if not isinstance(payload, dict):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "body must be a JSON object")
    tenant_id = _tenant(x_tenant_id)
    candidate = payload.get("tenant_id")
    if candidate is not None and candidate != tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "body tenant_id disagrees with the signed context")
    return service.check_eligibility(tenant_id, voice_id, payload)


@app.post("/voices/{voice_id}:revoke")
def revoke(voice_id: str, payload: dict,
           command_id: str = Header(alias="Idempotency-Key"),
           x_tenant_id: str = Header(alias="X-Tenant-Id"),
           x_principal_id: str | None = Header(default=None,
                                               alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.revoke(
                       x_tenant_id, voice_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}:suspend")
def suspend(voice_id: str, payload: dict,
            command_id: str = Header(alias="Idempotency-Key"),
            x_tenant_id: str = Header(alias="X-Tenant-Id"),
            x_principal_id: str | None = Header(default=None,
                                                alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.suspend(
                       x_tenant_id, voice_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}:resume")
def resume(voice_id: str, payload: dict,
           command_id: str = Header(alias="Idempotency-Key"),
           x_tenant_id: str = Header(alias="X-Tenant-Id"),
           x_principal_id: str | None = Header(default=None,
                                               alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.resume(
                       x_tenant_id, voice_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}:renew")
def renew(voice_id: str, payload: dict,
          command_id: str = Header(alias="Idempotency-Key"),
          x_tenant_id: str = Header(alias="X-Tenant-Id"),
          x_principal_id: str | None = Header(default=None,
                                              alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.renew(
                       x_tenant_id, voice_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}/candidates")
def add_candidate(voice_id: str, payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id"),
                  x_principal_id: str | None = Header(default=None,
                                                      alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.add_candidate(
                       x_tenant_id, voice_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}/candidates/{candidate_id}:accept")
def accept_candidate(voice_id: str, candidate_id: str, payload: dict,
                     command_id: str = Header(alias="Idempotency-Key"),
                     x_tenant_id: str = Header(alias="X-Tenant-Id"),
                     x_principal_id: str | None = Header(
                         default=None, alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.accept_candidate(
                       x_tenant_id, voice_id, candidate_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}/candidates/{candidate_id}:reject")
def reject_candidate(voice_id: str, candidate_id: str, payload: dict,
                     command_id: str = Header(alias="Idempotency-Key"),
                     x_tenant_id: str = Header(alias="X-Tenant-Id"),
                     x_principal_id: str | None = Header(
                         default=None, alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.reject_candidate(
                       x_tenant_id, voice_id, candidate_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}:set-canonical")
def set_canonical(voice_id: str, payload: dict,
                  command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(alias="X-Tenant-Id"),
                  x_principal_id: str | None = Header(default=None,
                                                      alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.set_canonical(
                       x_tenant_id, voice_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}/provider-mappings")
def add_provider_mapping(voice_id: str, payload: dict,
                         command_id: str = Header(alias="Idempotency-Key"),
                         x_tenant_id: str = Header(alias="X-Tenant-Id"),
                         x_principal_id: str | None = Header(
                             default=None, alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.add_provider_mapping(
                       x_tenant_id, voice_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}/provider-mappings/{mapping_key}:{action}")
def change_mapping_status(voice_id: str, mapping_key: str, action: str,
                          payload: dict | None = None,
                          command_id: str = Header(alias="Idempotency-Key"),
                          x_tenant_id: str = Header(alias="X-Tenant-Id"),
                          x_principal_id: str | None = Header(
                              default=None, alias="X-Principal-Id")):
    # S15-6：映射主键是 7 列，路径里用 `~` 连接。`urllib.parse.unquote`
    # 让调用方可以把带 `~`/`:` 的 snapshot 名编码后传进来。
    token = urllib.parse.unquote(mapping_key)
    body = _body(payload if payload is not None else {}, command_id,
                 x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.change_mapping_status(
                       x_tenant_id, voice_id, command_id, token, action, body,
                       principal=x_principal_id))


@app.post("/lexicons")
def create_lexicon(payload: dict,
                   command_id: str = Header(alias="Idempotency-Key"),
                   x_tenant_id: str = Header(alias="X-Tenant-Id"),
                   x_principal_id: str | None = Header(default=None,
                                                       alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.create_lexicon(
                       x_tenant_id, command_id, body,
                       principal=x_principal_id))


@app.post("/lexicons/{lexicon_id}/revisions")
def add_lexicon_revision(lexicon_id: str, payload: dict,
                         command_id: str = Header(alias="Idempotency-Key"),
                         x_tenant_id: str = Header(alias="X-Tenant-Id"),
                         x_principal_id: str | None = Header(
                             default=None, alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.add_lexicon_revision(
                       x_tenant_id, lexicon_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices/{voice_id}/lexicon-bindings")
def bind_lexicon(voice_id: str, payload: dict,
                 command_id: str = Header(alias="Idempotency-Key"),
                 x_tenant_id: str = Header(alias="X-Tenant-Id"),
                 x_principal_id: str | None = Header(default=None,
                                                     alias="X-Principal-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.bind_lexicon(
                       x_tenant_id, voice_id, command_id, body,
                       principal=x_principal_id))


@app.post("/voices:expire-sweep")
def expire_sweep(payload: dict | None = None,
                 command_id: str = Header(alias="Idempotency-Key"),
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    """维护动作，不是资格判定的前提（§7）。"""
    body = _body(payload if payload is not None else {}, command_id,
                 x_tenant_id)
    return command(x_tenant_id, body, command_id,
                   lambda _hash: service.expire_sweep(x_tenant_id))


# ------------------------------------------------------------------ 查询

@app.get("/voices/{voice_id}")
def get_voice(voice_id: str,
              x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_voice(_tenant(x_tenant_id), voice_id)


@app.get("/voices/{voice_id}/revisions/{revision}")
def get_revision(voice_id: str, revision: int,
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_revision(_tenant(x_tenant_id), voice_id, revision)


@app.get("/voices/{voice_id}/bindings")
def get_bindings(voice_id: str,
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_bindings(_tenant(x_tenant_id), voice_id)


@app.get("/voices/{voice_id}/authorization")
def get_authorization(voice_id: str,
                      x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_authorization(_tenant(x_tenant_id), voice_id)


@app.get("/voices/{voice_id}/canonical")
def get_canonical(voice_id: str,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_canonical(_tenant(x_tenant_id), voice_id)


@app.get("/voices/{voice_id}/provider-mappings")
def list_mappings(voice_id: str,
                  x_tenant_id: str = Header(alias="X-Tenant-Id"),
                  provider_id: str | None = Query(default=None),
                  model_snapshot: str | None = Query(default=None)):
    return service.list_mappings(_tenant(x_tenant_id), voice_id,
                                 provider_id=provider_id,
                                 model_snapshot=model_snapshot)


@app.get("/voices/{voice_id}/candidates/{candidate_id}")
def get_candidate(voice_id: str, candidate_id: str,
                  x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_candidate(_tenant(x_tenant_id), voice_id, candidate_id)


@app.get("/lexicons/{lexicon_id}/revisions/{revision}")
def get_lexicon_revision(lexicon_id: str, revision: int,
                         x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_lexicon_revision(_tenant(x_tenant_id), lexicon_id,
                                        revision)


@app.get("/voices:lookup")
def lookup_voice(x_tenant_id: str = Header(alias="X-Tenant-Id"),
                 series_scope: str = Query(...),
                 alias: str = Query(...)):
    return service.lookup_voice(_tenant(x_tenant_id),
                                series_scope=series_scope, alias=alias)


@app.get("/health/readiness")
def readiness():
    row = _conn.execute("SELECT 1").fetchone()
    return {"ready": row is not None, "contract_gaps": CONTRACT_GAPS}
