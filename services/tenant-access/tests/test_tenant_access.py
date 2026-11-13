"""S01 验收测试：E28（跨租户不泄漏）、E27（撤销即时生效）、幂等、TCT 绑定。

跑法（仓库根目录）：
    PYTHONPATH="platform/auth-sdk/src;platform/event-sdk/src;platform/idempotency-sdk/src;services/tenant-access/src;services/tenant-access"
    pytest services/tenant-access/tests -q
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from tenant_access.schema import SCHEMA
from tenant_access.service import ServiceError, TenantAccessService
from auth_sdk import TokenSigner, verify_tenant_context, TenantContextError


def _svc() -> TenantAccessService:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return TenantAccessService(conn, TokenSigner({"k1": b"test-secret"}))


def _tenant(svc, slug="acme") -> str:
    return svc.provision_tenant(
        {"command_id": f"cmd-{slug}", "payload": {"slug": slug}})["tenant_id"]


def _principal(svc, tenant_id, pid="prn_1", status="active", ptype="human"):
    svc.conn.execute(
        "INSERT INTO principal (tenant_id, principal_id, principal_type, status,"
        " personal_revocation_epoch, created_at) VALUES (?, ?, ?, ?, 1, 'now')",
        (tenant_id, pid, ptype, status))
    svc.conn.commit()
    return pid


def _role(svc, tenant_id, key="editor", perms=("creative.script.seal",),
          max_scope="episode"):
    svc.create_role(tenant_id, {"command_id": f"role-{key}-{max_scope}",
                                "payload": {"role_key": key,
                                            "max_scope": max_scope,
                                            "permissions": list(perms)}})
    return key


def _grant(svc, tenant_id, pid, role="editor", scope_kind="episode",
           scope_id="ep_1", effect="allow"):
    return svc.grant_role(tenant_id, {
        "command_id": f"grant-{pid}-{scope_id}-{effect}",
        "payload": {"principal_id": pid, "role_key": role,
                    "scope_kind": scope_kind, "scope_id": scope_id,
                    "effect": effect}})["grant_id"]


# ---- 跨租户 -------------------------------------------------------------

def test_cross_tenant_object_is_not_found():
    """E28：不存在与属于别人必须是同一个 404，不能泄漏存在性。"""
    svc = _svc()
    t1, t2 = _tenant(svc, "a"), _tenant(svc, "b")
    _principal(svc, t1, "prn_a")
    with pytest.raises(ServiceError) as exc:
        svc.authorization_context(t2, "prn_a", "tenant", t2, None)
    assert exc.value.status == 404 and exc.value.code == "NOT_FOUND"


def test_grant_in_one_tenant_does_not_apply_in_another():
    svc = _svc()
    t1, t2 = _tenant(svc, "a"), _tenant(svc, "b")
    _principal(svc, t1, "prn_a")
    _principal(svc, t2, "prn_a2")
    _role(svc, t1)
    _grant(svc, t1, "prn_a")
    ctx = svc.authorization_context(t2, "prn_a2", "episode", "ep_1", None)
    assert ctx["permissions"] == []


# ---- 撤销 ---------------------------------------------------------------

def test_offboarded_principal_is_denied_immediately():
    svc = _svc()
    t = _tenant(svc)
    _principal(svc, t, "prn_1", status="offboarded")
    _role(svc, t)
    _grant(svc, t, "prn_1")
    ctx = svc.authorization_context(t, "prn_1", "episode", "ep_1", None)
    assert ctx["permissions"] == []
    res = svc.check_permissions(t, "prn_1", [{
        "permission_key": "creative.script.seal",
        "scope_kind": "episode", "scope_id": "ep_1"}], None)
    assert res[0]["allowed"] is False
    assert res[0]["reason"] == "PRINCIPAL_OFFBOARDED"


def test_suspended_tenant_revokes_everything():
    svc = _svc()
    t = _tenant(svc)
    _principal(svc, t, "prn_1")
    _role(svc, t)
    _grant(svc, t, "prn_1")
    _principal(svc, "platform", "prn_admin") if False else None
    svc.suspend_tenant(t, {"command_id": "sus-1"}, expected_revision=1)
    res = svc.check_permissions(t, "prn_1", [{
        "permission_key": "creative.script.seal",
        "scope_kind": "episode", "scope_id": "ep_1"}], None)
    assert res[0]["allowed"] is False and res[0]["reason"] == "TENANT_SUSPENDED"


# ---- 幂等 ---------------------------------------------------------------

def test_grant_without_role_is_422_not_403():
    svc = _svc()
    t = _tenant(svc)
    _principal(svc, t, "prn_1")
    with pytest.raises(ServiceError) as exc:
        svc.grant_role(t, {"command_id": "g1",
                           "payload": {"principal_id": "prn_1",
                                       "role_key": "nope",
                                       "scope_kind": "episode",
                                       "scope_id": "ep_1"}})
    assert exc.value.status == 422


def test_scope_kind_prefix_mismatch_is_422():
    svc = _svc()
    t = _tenant(svc)
    _principal(svc, t, "prn_1")
    _role(svc, t)
    with pytest.raises(ServiceError) as exc:
        svc.grant_role(t, {"command_id": "g2",
                           "payload": {"principal_id": "prn_1",
                                       "role_key": "editor",
                                       "scope_kind": "project",
                                       "scope_id": "ep_1"}})
    assert exc.value.code == "SCOPE_KIND_MISMATCH"


def test_role_max_scope_is_enforced():
    svc = _svc()
    t = _tenant(svc)
    _principal(svc, t, "prn_1")
    _role(svc, t, max_scope="series")
    with pytest.raises(ServiceError) as exc:
        svc.grant_role(t, {"command_id": "g3",
                           "payload": {"principal_id": "prn_1",
                                       "role_key": "editor",
                                       "scope_kind": "episode",
                                       "scope_id": "ep_1"}})
    assert exc.value.code == "SCOPE_EXCEEDS_ROLE_MAX"


# ---- 判定优先级 ---------------------------------------------------------

def test_explicit_deny_beats_allow():
    svc = _svc()
    t = _tenant(svc)
    _principal(svc, t, "prn_1")
    _role(svc, t, key="editor", max_scope="episode")
    _role(svc, t, key="blocked", max_scope="episode")
    _grant(svc, t, "prn_1", role="editor")
    _grant(svc, t, "prn_1", role="blocked", effect="deny")
    res = svc.check_permissions(t, "prn_1", [{
        "permission_key": "creative.script.seal",
        "scope_kind": "episode", "scope_id": "ep_1"}], None)
    assert res[0]["allowed"] is False
    assert res[0]["reason"] == "EXPLICIT_DENY"


def test_tenant_scope_grant_covers_child_scopes():
    svc = _svc()
    t = _tenant(svc)
    _principal(svc, t, "prn_1")
    _role(svc, t, key="admin", max_scope="tenant")
    _grant(svc, t, "prn_1", role="admin", scope_kind="tenant", scope_id=t)
    ctx = svc.authorization_context(t, "prn_1", "episode", "ep_1", None)
    assert "creative.script.seal" in ctx["permissions"]


# ---- TCT ----------------------------------------------------------------

def test_tct_is_bound_to_request_hash_and_revocation_epoch():
    svc = _svc()
    t = _tenant(svc)
    _principal(svc, t, "prn_1")
    _role(svc, t)
    _grant(svc, t, "prn_1")
    now = int(time.time())
    out = svc.issue_tenant_context({
        "tenant_id": t, "principal_id": "prn_1",
        "audience": "s08.revision-snapshot", "request_hash": "aa" * 32,
        "scope_kind": "episode", "scope_id": "ep_1",
        "purpose": "manifest.commit"}, now)
    ctx = verify_tenant_context(svc.signer, out["token"], now=now,
                                expected_audience="s08.revision-snapshot",
                                request_hash="aa" * 32, header_tenant_id=t)
    assert ctx.tenant_id == t and "creative.script.seal" in ctx.permissions

    # 拿它去重放另一个 body 必须失败。
    with pytest.raises(TenantContextError) as exc:
        verify_tenant_context(svc.signer, out["token"], now=now,
                              expected_audience="s08.revision-snapshot",
                              request_hash="bb" * 32)
    assert exc.value.code == "REQUEST_HASH_MISMATCH"

    # 撤销后旧 token 立刻失效（不等到 exp）。
    svc.suspend_tenant(t, {"command_id": "sus"}, expected_revision=1)
    with pytest.raises(TenantContextError) as exc:
        verify_tenant_context(svc.signer, out["token"], now=now,
                              expected_audience="s08.revision-snapshot",
                              current_revocation_epoch=2)
    assert exc.value.code == "TENANT_REVOKED"


def test_unknown_audience_is_422():
    svc = _svc()
    t = _tenant(svc)
    _principal(svc, t, "prn_1")
    with pytest.raises(ServiceError) as exc:
        svc.issue_tenant_context({
            "tenant_id": t, "principal_id": "prn_1",
            "audience": "s99.not-a-service", "request_hash": "x",
            "scope_kind": "episode", "scope_id": "ep_1"}, int(time.time()))
    assert exc.value.code == "UNKNOWN_AUDIENCE"


# ---- Outbox -------------------------------------------------------------

def test_grant_emits_outbox_event():
    svc = _svc()
    t = _tenant(svc)
    _principal(svc, t, "prn_1")
    _role(svc, t)
    _grant(svc, t, "prn_1")
    events = [e for e in svc.outbox.unpublished() if e["type"] == "access.grant.changed"]
    assert len(events) == 1
    assert events[0]["tenant_id"] == t
    assert events[0]["data"]["state"] == "active"


# ---- HTTP 幂等 ----------------------------------------------------------

def test_http_same_key_same_body_replays_and_different_body_is_409():
    import importlib
    import app as app_module
    importlib.reload(app_module)
    client = TestClient(app_module.app)
    body = {"payload": {"slug": f"acme-{time.time_ns()}"}}
    h = {"Idempotency-Key": "cmd-http-1", "X-Tenant-Id": "platform"}
    r1 = client.post("/tenants", json=body, headers=h)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/tenants", json=body, headers=h)
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    h404 = {"Idempotency-Key": "cmd-http-1", "X-Tenant-Id": "platform"}
    r3 = client.post("/tenants", json={"payload": {"slug": "other"}}, headers=h404)
    assert r3.status_code == 409
    assert r3.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
