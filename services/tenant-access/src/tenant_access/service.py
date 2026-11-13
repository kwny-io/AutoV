"""S01 业务逻辑：租户生命周期、主体、角色授予、授权判定、TCT 签发。

全部写路径都要经过 `command()`，它保证：
  * `(tenant_id, command_id)` 幂等，同键不同 body → 409
  * 业务事实与 Outbox 事件在同一个事务里提交
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from auth_sdk import TokenSigner, mint_tenant_context
from event_sdk import Outbox, new_event

from .schema import AUDIENCES, PERMISSION_KEYS, SCOPE_PREFIX, SCOPE_RANK

TOPIC = "drama.tenant-access.v2"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


class TenantAccessService:
    def __init__(self, conn: sqlite3.Connection, signer: TokenSigner):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.signer = signer
        self.outbox = Outbox(conn)

    # ---- 内部：事务 + 幂等 + 事件 ------------------------------------------

    def _emit(self, tenant_id: str, event_type: str, aggregate_id: str,
              sequence: int, correlation_id: str, data: dict) -> None:
        self.outbox.append(new_event(
            source="s01.tenant-access",
            type=event_type,
            subject=f"{aggregate_id}",
            tenant_id=tenant_id,
            home_cell="global",
            aggregate_id=aggregate_id,
            sequence=sequence,
            correlation_id=correlation_id,
            data=data,
        ))

    def _tenant(self, tenant_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM tenant WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        if row is None:
            # E28：不存在与属于别人不区分。
            raise ServiceError(404, "NOT_FOUND", "tenant does not exist")
        return row

    def _require_active_tenant(self, tenant_id: str) -> sqlite3.Row:
        row = self._tenant(tenant_id)
        if row["lifecycle"] != "active":
            raise ServiceError(409, "TENANT_SUSPENDED",
                               f"tenant is {row['lifecycle']}")
        return row

    # ---- 命令 ---------------------------------------------------------------

    def provision_tenant(self, body: dict) -> dict:
        slug = body["payload"]["slug"]
        tenant_id = _new_id("tnt_")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO tenant (tenant_id, slug, lifecycle, revocation_epoch, created_at)"
                " VALUES (?, ?, 'active', 1, ?)", (tenant_id, slug, now))
            self._emit(tenant_id, "access.tenant.provisioned", tenant_id, 1,
                       body["command_id"], {"tenant_id": tenant_id, "slug": slug})
        return {"tenant_id": tenant_id, "slug": slug, "lifecycle": "active"}

    def suspend_tenant(self, tenant_id: str, body: dict, expected_revision: int) -> dict:
        with self.conn:
            row = self._tenant(tenant_id)
            if row["lifecycle"] == "terminated":
                raise ServiceError(409, "TENANT_TERMINATED", "tenant is terminated")
            new_epoch = row["revocation_epoch"] + 1
            self.conn.execute(
                "UPDATE tenant SET lifecycle = 'suspended', revocation_epoch = ?"
                " WHERE tenant_id = ?", (new_epoch, tenant_id))
            self._emit(tenant_id, "access.grant.changed", tenant_id, new_epoch,
                       body["command_id"],
                       {"reason": "TENANT_SUSPENDED", "revocation_epoch": new_epoch})
        return {"tenant_id": tenant_id, "lifecycle": "suspended",
                "revocation_epoch": new_epoch}

    def create_org_unit(self, tenant_id: str, body: dict) -> dict:
        payload = body["payload"]
        org_unit_id = _new_id("org_")
        with self.conn:
            self._require_active_tenant(tenant_id)
            self.conn.execute(
                "INSERT INTO org_unit (tenant_id, org_unit_id, parent_id, name)"
                " VALUES (?, ?, ?, ?)",
                (tenant_id, org_unit_id, payload.get("parent_id"), payload["name"]))
        return {"org_unit_id": org_unit_id}

    def create_role(self, tenant_id: str, body: dict) -> dict:
        payload = body["payload"]
        unknown = set(payload["permissions"]) - PERMISSION_KEYS
        if unknown:
            raise ServiceError(422, "UNKNOWN_PERMISSION_KEY",
                               f"not in the registry: {sorted(unknown)}")
        with self.conn:
            self._require_active_tenant(tenant_id)
            self.conn.execute(
                "INSERT INTO role (tenant_id, role_key, max_scope, permissions)"
                " VALUES (?, ?, ?, ?)",
                (tenant_id, payload["role_key"], payload["max_scope"],
                 json.dumps(sorted(set(payload["permissions"])))))
        return {"role_key": payload["role_key"],
                "max_scope": payload["max_scope"],
                "permissions": sorted(set(payload["permissions"]))}

    def grant_role(self, tenant_id: str, body: dict) -> dict:
        payload = body["payload"]
        scope_kind, scope_id = payload["scope_kind"], payload["scope_id"]
        if not scope_id.startswith(SCOPE_PREFIX[scope_kind]):
            raise ServiceError(422, "SCOPE_KIND_MISMATCH",
                               f"{scope_kind} needs a {SCOPE_PREFIX[scope_kind]} id")
        with self.conn:
            self._require_active_tenant(tenant_id)
            role = self.conn.execute(
                "SELECT * FROM role WHERE tenant_id = ? AND role_key = ?",
                (tenant_id, payload["role_key"])).fetchone()
            if role is None:
                raise ServiceError(422, "UNKNOWN_PERMISSION_KEY",
                                   f"unknown role {payload['role_key']!r}")
            if SCOPE_RANK[scope_kind] > SCOPE_RANK[role["max_scope"]]:
                raise ServiceError(422, "SCOPE_EXCEEDS_ROLE_MAX",
                                   f"role allows up to {role['max_scope']}")
            grant_id = _new_id("grt_")
            self.conn.execute(
                "INSERT INTO role_grant (tenant_id, grant_id, principal_id, role_key,"
                " scope_kind, scope_id, effect, state, revision, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 1, ?)",
                (tenant_id, grant_id, payload["principal_id"], payload["role_key"],
                 scope_kind, scope_id, payload.get("effect", "allow"), _now()))
            seq = self._next_grant_seq(tenant_id)
            self._emit(tenant_id, "access.grant.changed", grant_id, seq,
                       body["command_id"],
                       {"grant_id": grant_id, "state": "active",
                        "principal_id": payload["principal_id"],
                        "role_key": payload["role_key"],
                        "scope_kind": scope_kind, "scope_id": scope_id})
        return {"grant_id": grant_id, "state": "active", "revision": 1}

    def revoke_grant(self, tenant_id: str, grant_id: str, body: dict,
                     expected_revision: int) -> dict:
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM role_grant WHERE tenant_id = ? AND grant_id = ?",
                (tenant_id, grant_id)).fetchone()
            if row is None:
                raise ServiceError(404, "NOT_FOUND", "grant does not exist")
            if row["state"] == "revoked":
                return {"grant_id": grant_id, "state": "revoked",
                        "revision": row["revision"]}
            if row["revision"] != expected_revision:
                raise ServiceError(409, "GRANT_REVISION_CONFLICT",
                                   f"current revision is {row['revision']}",
                                   retryable=True)
            revision = row["revision"] + 1
            self.conn.execute(
                "UPDATE role_grant SET state = 'revoked', revision = ?"
                " WHERE tenant_id = ? AND grant_id = ?",
                (revision, tenant_id, grant_id))
            seq = self._next_grant_seq(tenant_id)
            self._emit(tenant_id, "access.grant.changed", grant_id, seq,
                       body["command_id"],
                       {"grant_id": grant_id, "state": "revoked",
                        "revision": revision})
        return {"grant_id": grant_id, "state": "revoked", "revision": revision}

    def _next_grant_seq(self, tenant_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM outbox"
            " WHERE tenant_id = ? AND aggregate_id LIKE 'grt_%'", (tenant_id,)).fetchone()
        return int(row[0]) + 1

    # ---- 判定 ---------------------------------------------------------------

    def check_permissions(self, tenant_id: str, principal_id: str,
                          checks: list[dict], purpose: str | None) -> list[dict]:
        if len(checks) > 32:
            raise ServiceError(422, "INVALID_SCHEMA", "at most 32 checks per request")
        unknown = {c["permission_key"] for c in checks} - PERMISSION_KEYS
        if unknown:
            raise ServiceError(422, "UNKNOWN_PERMISSION_KEY",
                               f"not in the registry: {sorted(unknown)}")
        results = []
        for c in checks:
            allowed, reason = self._decide(
                tenant_id, principal_id, c["permission_key"],
                c["scope_kind"], c["scope_id"], purpose)
            decision_id = _new_id("dec_")
            self.conn.execute(
                "INSERT INTO authorization_decision (tenant_id, decision_id,"
                " principal_id, permission, scope_kind, scope_id, purpose, allowed,"
                " reason, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, decision_id, principal_id, c["permission_key"],
                 c["scope_kind"], c["scope_id"], purpose, int(allowed), reason, _now()))
            results.append({"permission_key": c["permission_key"],
                            "allowed": allowed, "reason": reason,
                            "decision_id": decision_id})
        self.conn.commit()
        return results

    def _decide(self, tenant_id: str, principal_id: str, permission: str,
                scope_kind: str, scope_id: str, purpose: str | None) -> tuple[bool, str]:
        tenant = self.conn.execute(
            "SELECT * FROM tenant WHERE tenant_id = ?", (tenant_id,)).fetchone()
        if tenant is None:
            return False, "TENANT_NOT_FOUND"
        if tenant["lifecycle"] != "active":
            return False, "TENANT_SUSPENDED"
        principal = self.conn.execute(
            "SELECT * FROM principal WHERE tenant_id = ? AND principal_id = ?",
            (tenant_id, principal_id)).fetchone()
        if principal is None:
            return False, "PRINCIPAL_NOT_FOUND"
        if principal["status"] != "active":
            return False, f"PRINCIPAL_{principal['status'].upper()}"

        rows = self.conn.execute(
            "SELECT rg.*, r.permissions, r.max_scope FROM role_grant rg"
            " JOIN role r ON r.tenant_id = rg.tenant_id AND r.role_key = rg.role_key"
            " WHERE rg.tenant_id = ? AND rg.principal_id = ? AND rg.state = 'active'",
            (tenant_id, principal_id)).fetchall()

        # deny 优先于 allow：负向授予是刻意的（plans/S01 §1.1）。
        for row in rows:
            perms = json.loads(row["permissions"])
            if permission in perms and self._scope_covers(
                    row["scope_kind"], row["scope_id"], scope_kind, scope_id):
                if row["effect"] == "deny":
                    return False, "EXPLICIT_DENY"
        for row in rows:
            perms = json.loads(row["permissions"])
            if permission in perms and self._scope_covers(
                    row["scope_kind"], row["scope_id"], scope_kind, scope_id):
                if row["effect"] == "allow":
                    return True, "GRANTED"
        return False, "NO_MATCHING_GRANT"

    @staticmethod
    def _scope_covers(grant_kind: str, grant_id: str,
                      want_kind: str, want_id: str) -> bool:
        # 授予在 tenant 上覆盖该租户内的一切 scope；其余需要精确命中。
        if grant_kind == "tenant":
            return True
        return grant_kind == want_kind and grant_id == want_id

    def authorization_context(self, tenant_id: str, principal_id: str,
                              scope_kind: str, scope_id: str,
                              purpose: str | None) -> dict:
        if not scope_id.startswith(SCOPE_PREFIX[scope_kind]):
            raise ServiceError(422, "SCOPE_KIND_MISMATCH",
                               f"{scope_kind} needs a {SCOPE_PREFIX[scope_kind]} id")
        tenant = self._tenant(tenant_id)
        principal = self.conn.execute(
            "SELECT * FROM principal WHERE tenant_id = ? AND principal_id = ?",
            (tenant_id, principal_id)).fetchone()
        if principal is None:
            raise ServiceError(404, "NOT_FOUND", "principal does not exist")

        # 离职/停用的主体不返回任何权限。已签发的 TCT 靠 personal_revocation_epoch
        # 失效，但这里是当前事实，必须当场空掉——否则调用方会拿到一份
        # 「主体已离职却仍有权限」的上下文。
        if principal["status"] != "active" or tenant["lifecycle"] != "active":
            return {
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "principal_status": principal["status"],
                "tenant_lifecycle": tenant["lifecycle"],
                "scope_kind": scope_kind,
                "scope_id": scope_id,
                "permissions": [],
                "denied": [],
                "revocation_epoch": tenant["revocation_epoch"],
                "personal_revocation_epoch": principal["personal_revocation_epoch"],
                "ttl_seconds": 5,
            }

        effective: dict[str, str] = {}
        rows = self.conn.execute(
            "SELECT rg.*, r.permissions FROM role_grant rg"
            " JOIN role r ON r.tenant_id = rg.tenant_id AND r.role_key = rg.role_key"
            " WHERE rg.tenant_id = ? AND rg.principal_id = ? AND rg.state = 'active'",
            (tenant_id, principal_id)).fetchall()
        for row in rows:
            if not self._scope_covers(row["scope_kind"], row["scope_id"],
                                      scope_kind, scope_id):
                continue
            for perm in json.loads(row["permissions"]):
                # deny 一旦出现就压住 allow，且不会被后面的 allow 翻回来。
                if effective.get(perm) == "deny":
                    continue
                effective[perm] = row["effect"]

        permissions = sorted(p for p, e in effective.items() if e == "allow")
        return {
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "principal_status": principal["status"],
            "tenant_lifecycle": tenant["lifecycle"],
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "permissions": permissions,
            "denied": sorted(p for p, e in effective.items() if e == "deny"),
            "revocation_epoch": tenant["revocation_epoch"],
            "personal_revocation_epoch": principal["personal_revocation_epoch"],
            "ttl_seconds": 5,
        }

    # ---- TCT ---------------------------------------------------------------

    def issue_tenant_context(self, body: dict, now: int) -> dict:
        audience = body.get("audience")
        if audience not in AUDIENCES:
            raise ServiceError(422, "UNKNOWN_AUDIENCE",
                               f"{audience!r} is not a registered audience")
        tenant_id = body["tenant_id"]
        tenant = self._tenant(tenant_id)
        if tenant["lifecycle"] != "active":
            raise ServiceError(409, "TENANT_SUSPENDED",
                               f"tenant is {tenant['lifecycle']}")
        principal = self.conn.execute(
            "SELECT * FROM principal WHERE tenant_id = ? AND principal_id = ?",
            (tenant_id, body["principal_id"])).fetchone()
        if principal is None:
            raise ServiceError(404, "NOT_FOUND", "principal does not exist")
        if principal["status"] != "active":
            raise ServiceError(403, "PERMISSION_DENIED",
                               f"principal is {principal['status']}")

        ctx = self.authorization_context(
            tenant_id, body["principal_id"], body["scope_kind"], body["scope_id"],
            body.get("purpose"))
        token = mint_tenant_context(
            self.signer,
            tenant_id=tenant_id,
            principal_id=body["principal_id"],
            principal_type=principal["principal_type"],
            audience=audience,
            request_hash=body["request_hash"],
            scope_kind=body["scope_kind"],
            scope_id=body["scope_id"],
            purpose=body.get("purpose") or "",
            permissions=ctx["permissions"],
            revocation_epoch=tenant["revocation_epoch"],
            personal_revocation_epoch=principal["personal_revocation_epoch"],
            now=now,
            delegation_id=body.get("delegation_id"),
        )
        return {"token": token, "expires_in": 60, "ttl_seconds": 5}

    # ---- 查询 ---------------------------------------------------------------

    def get_principal(self, tenant_id: str, principal_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM principal WHERE tenant_id = ? AND principal_id = ?",
            (tenant_id, principal_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "principal does not exist")
        return {k: row[k] for k in row.keys()
                if k not in ("idp_subject",)}
