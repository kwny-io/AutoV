"""S01 HTTP 层。契约见 plans/S01-tenant-access.md §2。

写路径统一走 `command()`：先做 `(tenant_id, command_id)` 幂等占位，
再在**同一个事务**里写业务事实与 Outbox。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)
from auth_sdk import TokenSigner

from tenant_access.schema import SCHEMA
from tenant_access.service import ServiceError, TenantAccessService

DB_PATH = os.environ.get("S01_DB", ":memory:")
SIGNING_SECRET = os.environ.get("S01_SIGNING_SECRET", "dev-only-not-a-secret").encode()

app = FastAPI(title="S01 tenant-access", version="2.0.0")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.executescript(SCHEMA)
_idem = SqliteIdempotencyStore(_conn)
signer = TokenSigner({"k_dev": SIGNING_SECRET})
service = TenantAccessService(_conn, signer)


def _err(status: int, code: str, message: str, retryable: bool = False):
    return JSONResponse(status_code=status,
                        content={"code": code, "message": message,
                                 "retryable": retryable})


@app.exception_handler(ServiceError)
async def _service_error(_request, exc: ServiceError):
    return _err(exc.status, exc.code, str(exc), exc.retryable)


@app.exception_handler(CommandConflict)
async def _conflict(_request, exc: CommandConflict):
    return _err(409, exc.code, str(exc))


@app.exception_handler(CommandInProgress)
async def _in_progress(_request, exc: CommandInProgress):
    return _err(409, exc.code, str(exc), retryable=True)


def command(tenant_id: str, body: dict[str, Any], execute):
    """幂等占位 → 执行 → 落结果。

    幂等记录先以「进行中」落盘（autocommit 立即可见，并发调用看到的是
    COMMAND_IN_PROGRESS 而不是重复执行）；业务事实与 Outbox 由服务方法自己的
    `with conn:` 原子提交；失败时删掉占位，成功时补上结果。
    """
    request_hash = canonical_request_hash(body)
    command_id = body["command_id"]
    record = _idem.begin(tenant_id, command_id, request_hash)
    if record.result is not None:
        return json.loads(record.result)
    try:
        result = execute()
    except Exception:
        _idem.abort(tenant_id, command_id)
        raise
    _idem.finish(tenant_id, command_id, json.dumps(result, ensure_ascii=False))
    return result


def _body(payload: dict, command_id: str, tenant_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ServiceError(400, "INVALID_SCHEMA", "body must be a JSON object")
    if not command_id:
        raise ServiceError(400, "INVALID_SCHEMA", "command_id is required")
    return {**payload, "command_id": command_id, "tenant_id": tenant_id}


# ---- 命令 ----------------------------------------------------------------

@app.post("/tenants")
def create_tenant(payload: dict, command_id: str = Header(alias="Idempotency-Key"),
                  x_tenant_id: str = Header(default="platform", alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, lambda: service.provision_tenant(body))


@app.post("/tenants/{tenant_id}:suspend")
def suspend_tenant(tenant_id: str, payload: dict,
                   command_id: str = Header(alias="Idempotency-Key"),
                   x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    expected = payload.get("expected_revision", 1)
    return command(x_tenant_id, body,
                   lambda: service.suspend_tenant(tenant_id, body, expected))


@app.post("/org-units")
def create_org_unit(payload: dict, command_id: str = Header(alias="Idempotency-Key"),
                    x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, lambda: service.create_org_unit(x_tenant_id, body))


@app.post("/roles")
def create_role(payload: dict, command_id: str = Header(alias="Idempotency-Key"),
                x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, lambda: service.create_role(x_tenant_id, body))


@app.post("/access-grants")
def grant_role(payload: dict, command_id: str = Header(alias="Idempotency-Key"),
               x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    return command(x_tenant_id, body, lambda: service.grant_role(x_tenant_id, body))


@app.post("/access-grants/{grant_id}:revoke")
def revoke_grant(grant_id: str, payload: dict,
                 command_id: str = Header(alias="Idempotency-Key"),
                 x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = _body(payload, command_id, x_tenant_id)
    expected = payload.get("expected_revision", 1)
    return command(x_tenant_id, body,
                   lambda: service.revoke_grant(x_tenant_id, grant_id, body, expected))


@app.post("/permission-checks")
def permission_checks(payload: dict, x_tenant_id: str = Header(alias="X-Tenant-Id"),
                      x_principal_id: str = Header(alias="X-Principal-Id")):
    return service.check_permissions(x_tenant_id, x_principal_id,
                                     payload.get("checks", []), payload.get("purpose"))


@app.post("/tenant-contexts")
def tenant_contexts(payload: dict, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    body = {**payload, "tenant_id": x_tenant_id}
    return service.issue_tenant_context(body, int(time.time()))


# ---- 查询 ----------------------------------------------------------------

@app.get("/authorization-context")
def authorization_context(response: Response,
                          scope_kind: str = Query(...), scope_id: str = Query(...),
                          principal_id: str | None = None,
                          purpose: str | None = None,
                          x_tenant_id: str = Header(alias="X-Tenant-Id"),
                          x_principal_id: str = Header(alias="X-Principal-Id")):
    response.headers["Cache-Control"] = "no-store"
    return service.authorization_context(
        x_tenant_id, principal_id or x_principal_id, scope_kind, scope_id, purpose)


@app.get("/principals/{principal_id}")
def get_principal(principal_id: str, x_tenant_id: str = Header(alias="X-Tenant-Id")):
    return service.get_principal(x_tenant_id, principal_id)


@app.get("/health/readiness")
def readiness():
    with closing(_conn.execute("SELECT 1")) as cur:
        cur.fetchone()
    return {"status": "ready"}


class PrincipalIn(dict):
    pass
