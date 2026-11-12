"""S02 业务逻辑：策略版本登记、发布期校验、有效策略解析。

两条禁令写在最前面，因为它们是这个文件里每个决定背后的理由：

- **不改宽松**：join 只能收紧（`merge.py`）。发布期还要显式拒绝"被 join 吃掉"
  的覆盖（§4.5 (b)）——静默失效等于骗用户说覆盖生效了。
- **不做第二权威**：S02 不存授权、不存余额、不存 manifest。`resolve` 只读自己
  的 revision 表；其余上下文（`node_epoch` / `execution_fence` / `manifest_ref`）
  原样回显绑定，真正的并发保护在 S08 的 CAS。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from . import merge as M
from .schema import SCHEMA

# §2 行 74：guided + generation_mode=auto 在发布期 422，不做自动修正。
INCONSISTENT_MODE_SWITCHES = {("guided", "generation_mode", "auto")}

# §4.5 (e)：三条款是 const true，第四条款是 const false。
HARD_RULES_TRUE = ("rights_required", "enforce_data_residency",
                   "no_incomplete_formal_delivery")

GATE_PHASE = {
    "script": "pre_dispatch", "canonical_asset": "pre_dispatch",
    "storyboard": "pre_dispatch", "budget_increase": "pre_dispatch",
    "rough_cut": "post_generation", "final_master": "post_generation",
    "external_publish": "post_generation",
}


def _ts(value) -> str:
    """定宽 ISO-8601 UTC：`YYYY-MM-DDTHH:MM:SS.ffffffZ`。

    方案 §10.1 的时间列是 `timestamptz`，落到 SQLite 是字符串。不先规范化，
    `<=` / `>=` 就不是时间比较——`effective_window` 的区间求交会静默错。
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        dt = datetime.fromtimestamp(float(value), timezone.utc)
    elif isinstance(value, str) and value.strip():
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError(f"not a timestamp: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now() -> str:
    return _ts(datetime.now(timezone.utc))


def jcs(value) -> str:
    """内容寻址用的 canonical JSON。键排序 + 无空格，跨机器逐字节一致。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def content_hash(value) -> str:
    """裸 64 位小写十六进制。

    **不带 `sha256:` 前缀**：`contracts/policy.revision-published.v2.schema.json`
    对 `policy_ref.content_hash` 的 `pattern` 是 `^[0-9a-f]{64}$`，带前缀会被契约
    直接拒；全仓其余契约（`episode-manifest`、`execution-context`、`data_ref`）也
    都是裸 hex，方案里的 `sha256:...` 是散文展示形态，不是落库形态。
    """
    return hashlib.sha256(jcs(value).encode("utf-8")).hexdigest()


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """显式事务。`isolation_level=None` 下 `with conn` 不开启事务，必须手写。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


class GovernancePolicyService:
    def __init__(self, conn: sqlite3.Connection, *, clock=_now):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(conn)
        self.clock = clock

    # ---- 内部 ------------------------------------------------------------

    def _now(self) -> str:
        return _ts(self.clock())

    def _revision(self, tenant_id: str, policy_id: str,
                  revision: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM automation_policy_revision"
            " WHERE tenant_id = ? AND policy_id = ? AND revision = ?",
            (tenant_id, policy_id, revision)).fetchone()
        if row is None:
            # E12-03：跨租户与不存在不区分。
            raise ServiceError(404, "POLICY_NOT_FOUND",
                               f"policy {policy_id} revision {revision} does not exist")
        return row

    def _pointer(self, tenant_id: str, policy_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM policy_publish_pointer"
            " WHERE tenant_id = ? AND policy_id = ?",
            (tenant_id, policy_id)).fetchone()
        if row is None:
            raise ServiceError(404, "POLICY_NOT_FOUND",
                               f"policy {policy_id} has no published revision")
        return row

    def _bindings_for(self, tenant_id: str, scope_type: str,
                      scope_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM policy_scope_binding WHERE tenant_id = ?"
            " AND scope_type = ? AND scope_id = ?",
            (tenant_id, scope_type, scope_id)).fetchall()

    def _load_layer(self, tenant_id: str, binding: sqlite3.Row,
                    scope_type: str, scope_id: str,
                    via: str = "document") -> dict:
        """把一条绑定读成 layer；指针漂移一律拒绝，不猜（§4.4 行 210-212）。"""
        rev = self._revision(tenant_id, binding["policy_id"],
                             int(binding["revision"]))
        if rev["content_hash"] != binding["content_hash"]:
            raise ServiceError(
                409, "POLICY_SUPERSEDED",
                "scope binding points at a revision whose content moved",
                details={"policy_id": binding["policy_id"],
                         "revision": int(binding["revision"])})
        document = json.loads(rev["document"])
        return {**top_level_fields(document),
                "_source": {"scope_type": scope_type, "scope_id": scope_id,
                            "policy_id": binding["policy_id"],
                            "revision": int(binding["revision"]),
                            "content_hash": binding["content_hash"],
                            "effective_at": rev["effective_at"],
                            "via": via}}

    # ---- 写命令：登记一版策略 --------------------------------------------

    def create_revision(self, tenant_id: str, policy_id: str, body: dict) -> dict:
        """`POST /policies/{policy_id}/revisions`（§4.5 + §10.1）。

        `expected_revision` 是 CAS：0 表示"这份 policy_id 还没有任何已发布版本"。
        与指针不等 → 409 `REVISION_CONFLICT`，且**不产生 revision**（E12-04）。
        """
        if not isinstance(body, dict):
            raise ServiceError(400, "MALFORMED_REQUEST", "body must be a JSON object")
        document = body.get("policy")
        if not isinstance(document, dict):
            raise ServiceError(400, "MALFORMED_REQUEST", "policy is required")
        expected = body.get("expected_revision")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "expected_revision must be a non-negative integer")

        if document.get("policy_id") != policy_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "policy.policy_id disagrees with the path")
        if document.get("tenant_id") not in (None, tenant_id):
            # AGENTS.md：请求体里的 tenant_id 不可信，必须与签名上下文一致。
            raise ServiceError(403, "TENANT_MISMATCH",
                               "policy.tenant_id disagrees with the signed context")
        self._validate_document_shape(document)

        revision = document["revision"]
        with Transaction(self.conn):
            pointer = self.conn.execute(
                "SELECT current_revision FROM policy_publish_pointer"
                " WHERE tenant_id = ? AND policy_id = ?",
                (tenant_id, policy_id)).fetchone()
            current = int(pointer["current_revision"]) if pointer else 0
            if expected != current:
                raise ServiceError(
                    409, "REVISION_CONFLICT",
                    "expected_revision does not match the publish pointer",
                    details={"expected": expected, "current": current})
            if revision != current + 1:
                raise ServiceError(
                    409, "REVISION_CONFLICT",
                    "revision must be exactly one past the publish pointer",
                    details={"requested": revision, "expected": current + 1})

            self._validate_overrides(tenant_id, document)

            digest = content_hash(document)
            now = self._now()
            self.conn.execute(
                "INSERT INTO automation_policy_revision (tenant_id, policy_id,"
                " revision, content_hash, document, effective_at, created_by,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, policy_id, revision, digest, jcs(document),
                 _ts(document["effective_at"]), document["created_by"], now))
            self.conn.execute(
                "INSERT INTO policy_publish_pointer (tenant_id, policy_id,"
                " current_revision, content_hash, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (tenant_id, policy_id) DO UPDATE SET"
                " current_revision=excluded.current_revision,"
                " content_hash=excluded.content_hash,"
                " updated_at=excluded.updated_at",
                (tenant_id, policy_id, revision, digest, now))
            self._bind_scope(tenant_id, body.get("scope"), policy_id, revision,
                             digest, now)
            self.outbox.append(new_event(
                source="s02.governance-policy",
                type="policy.revision.published",
                subject=f"{policy_id}",
                tenant_id=tenant_id,
                home_cell="global",
                aggregate_id=f"{policy_id}@{revision}",
                sequence=revision,
                correlation_id=body.get("command_id") or "",
                # 载荷严格按 contracts/policy.revision-published.v2.schema.json：
                # 四个键，additionalProperties=false，没有 document。
                data={"schema_version": "2.0",
                      "policy_ref": {"service_id": "S02", "id": policy_id,
                                     "revision": revision,
                                     "content_hash": digest},
                      "effective_at": document["effective_at"],
                      "created_by": document["created_by"]},
            ))

        return {"policy_ref": {"service_id": "S02", "id": policy_id,
                               "revision": revision, "content_hash": digest},
                "effective_at": document["effective_at"],
                "created_by": document["created_by"]}

    def _bind_scope(self, tenant_id: str, scope, policy_id: str,
                    revision: int, digest: str, now: str) -> None:
        """把文档绑到它自己的 scope 上（§4.2 行 146 的 A 类 layer）。

        **scope 从命令信封来，不从策略文档来**：`automation-policy.v2` 的文档顶层
        没有 scope 字段，且 `additionalProperties: false`，往里塞一个私有键会让
        合法文档变成非法。信封里加 `scope` 是对 OpenAPI 的实现层扩展，与 §5 给
        `resolve` 加 `context` 同性质，登记在 plans/S02 §13 的 POL-6。
        缺省是 tenant——企业基座，这也是唯一能确定无疑的层。
        """
        scope_type = (scope or {}).get("type") or "tenant"
        scope_id = (scope or {}).get("id") or tenant_id
        if scope_type not in M.SCOPE_CHAIN:
            raise ServiceError(422, "INVALID_SCOPE",
                               f"unknown scope_type {scope_type!r}")
        self.conn.execute(
            "INSERT INTO policy_scope_binding (tenant_id, scope_type, scope_id,"
            " policy_id, revision, content_hash, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (tenant_id, scope_type, scope_id) DO UPDATE SET"
            " policy_id=excluded.policy_id, revision=excluded.revision,"
            " content_hash=excluded.content_hash, updated_at=excluded.updated_at",
            (tenant_id, scope_type, scope_id, policy_id, revision, digest, now))

    # ---- 发布期校验（§4.5）------------------------------------------------

    def _validate_document_shape(self, document: dict) -> None:
        """按 `contracts/automation-policy.v2.schema.json` 的必填键与枚举校验。

        **运行期不 import jsonschema**：其余服务同样只有 stdlib + SDK 依赖。
        `tests/test_governance_policy.py` 用 jsonschema 对同一批样例做对拍，
        保证这里的判断与契约一致，而生产路径不引入新依赖。
        """
        for key in ("schema_version", "policy_id", "revision", "tenant_id", "mode",
                    "generation_mode", "acceptance_mode", "overrides",
                    "repair_limits", "exception_behavior", "required_gates",
                    "hard_rules", "effective_at", "created_by"):
            if key not in document:
                raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                                   f"{key} is required")
        if document["schema_version"] != "2.0":
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "schema_version must be 2.0")
        if not isinstance(document["revision"], int) or isinstance(
                document["revision"], bool):
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "revision must be an integer")
        if document["mode"] not in M.RANK["mode"]:
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               f"mode {document['mode']!r} is not a known tier")
        if document["generation_mode"] not in M.RANK["generation_mode"]:
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "generation_mode must be manual or auto")
        if document["acceptance_mode"] not in M.RANK["acceptance_mode"]:
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "acceptance_mode must be manual or auto_if_qc_pass")
        if document["exception_behavior"] not in M.RANK["exception_behavior"]:
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "exception_behavior must be wait_review or"
                               " isolate_branch")
        gates = document["required_gates"]
        if not isinstance(gates, list) or any(g not in M.GATES for g in gates):
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               f"required_gates must be a subset of {M.GATES}")
        limits = document["repair_limits"]
        if not isinstance(limits, dict) or set(limits) != {
                "max_business_attempts_per_shot",
                "max_identical_strategy_repeats"}:
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "repair_limits must carry both counters")
        if not isinstance(document["hard_rules"], dict):
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "hard_rules must be an object")
        try:
            _ts(document["effective_at"])
        except ValueError:
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "effective_at must be an ISO-8601 timestamp")
        # §2 行 74：这一条是发布期的 422，不是自动修正——修正会把"用户明确要
        # 自动"悄悄变成"不自动"，属于行为变更，必须让人看见。
        if (document["mode"], "generation_mode",
                document["generation_mode"]) in INCONSISTENT_MODE_SWITCHES:
            raise ServiceError(
                422, "POLICY_MODE_INCONSISTENT",
                "guided means the user starts each node; generation_mode=auto"
                " contradicts it and will not be silently corrected",
                details={"mode": document["mode"],
                         "generation_mode": document["generation_mode"]})

    def _validate_overrides(self, tenant_id: str, document: dict) -> None:
        """§4.5 (a)–(g)，逐条对照方案行 252-294。

        (d) `BUDGET_SCOPE_ESCAPE` 与 (g) `assert_scope_exists` 都依赖 S23 的
        budget tree 与 S01/S08 的 scope 目录。S02 不拥有那棵树，也不读别人的库，
        所以这两条只做形状校验，语义校验登记在 plans/S02 §13。
        """
        rules = document["hard_rules"]
        # (e) 硬规则不可变。schema 里的 const 在这里再落一次：文档以 JSON 提交，
        # schema 能被绕过，const 不能。
        for key in HARD_RULES_TRUE:
            if rules.get(key) is not True:
                raise ServiceError(422, "HARD_RULE_IMMUTABLE",
                                   f"hard_rules.{key} must be true",
                                   details={"field": key})
        if rules.get("allow_silent_identity_downgrade") is not False:
            raise ServiceError(422, "HARD_RULE_IMMUTABLE",
                               "hard_rules.allow_silent_identity_downgrade must be"
                               " false")
        if not isinstance(rules.get("budget_scope_id"), str) \
                or not rules["budget_scope_id"]:
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "hard_rules.budget_scope_id is required")

        overrides = document["overrides"]
        if not isinstance(overrides, list):
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "overrides must be an array")

        # `parent` = 祖先层 join 结果。本仓能证实的祖先只有两层：租户企业基座与
        # 本文档自己的顶层字段。更深的祖先需要 scope 树（见 §13）。
        parent = self._parent_effective(tenant_id, document)
        for ov in overrides:
            self._validate_override(ov, parent)

    def _validate_override(self, ov: dict, parent: dict) -> None:
        required = {"scope_type", "scope_id", "generation_mode",
                    "acceptance_mode", "reviewer_roles", "locked"}
        if not isinstance(ov, dict) or set(ov) != required:
            # schema 的 overrides[].additionalProperties=false 也是这六个键。
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "override must carry exactly "
                               + ", ".join(sorted(required)))
        if ov["scope_type"] not in M.SCOPE_CHAIN[1:]:
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               f"scope_type {ov['scope_type']!r} is not one of the"
                               " four override scopes")

        for field in ("generation_mode", "acceptance_mode"):
            value, upper = ov[field], parent.get(field)
            if not isinstance(value, str) or value not in M.RANK[field]:
                raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                                   f"{field} {value!r} is not a known value")
            if upper is None:
                continue
            merged = M.MERGE[field](upper, value)
            # (b) 覆盖不产生任何效果（意图是放宽，被 join 吃掉）→ 422，不静默。
            if merged == upper and value != upper:
                raise ServiceError(
                    422, "LOOSENING_NOT_ALLOWED",
                    f"{field}={value} would not take effect; the ancestor already"
                    f" enforces {upper}",
                    details={"field": field, "scope": ov["scope_id"],
                             "requested": value, "enforced": upper,
                             "source": (parent.get("_provenance") or {})
                             .get(field, {}).get("source")})

        roles = ov["reviewer_roles"]
        if not isinstance(roles, list) or any(
                not isinstance(r, str) or not r for r in roles):
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "reviewer_roles must be an array of role ids")
        allow = set(parent.get("reviewer_roles") or [])
        # (c) 角色范围逃逸：写进来的角色必须是祖先允许的子集。
        if allow and not set(roles) <= allow:
            raise ServiceError(
                422, "REVIEWER_ROLE_ESCAPE",
                "override names reviewer roles the ancestor does not allow",
                details={"extra": sorted(set(roles) - allow),
                         "allowed": sorted(allow)})

        if not isinstance(ov["locked"], bool):
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "locked must be a boolean")
        if not isinstance(ov["scope_id"], str) or not ov["scope_id"]:
            raise ServiceError(422, "POLICY_SCHEMA_VIOLATION",
                               "scope_id must be a non-empty string")

    def _parent_effective(self, tenant_id: str, document: dict) -> dict:
        """可证实的祖先 join 结果：租户企业基座 + 本文档顶层字段。

        `_provenance` 里带每层的来源，供 422 的 details 指出"是谁压住的"。
        """
        layers: list[dict] = []
        for binding in self._bindings_for(tenant_id, "tenant", tenant_id):
            layers.append(self._load_layer(tenant_id, binding, "tenant", tenant_id))
        layers.append({**top_level_fields(document),
                       "_source": {"scope_type": "candidate",
                                   "scope_id": document["policy_id"],
                                   "policy_id": document["policy_id"],
                                   "revision": document["revision"],
                                   "content_hash": None,
                                   "effective_at": _ts(document["effective_at"]),
                                   "via": "document"}})
        effective, provenance = M.merge_layers(layers)
        effective["_provenance"] = provenance.get("fields", {})
        return effective

    # ---- 读：有效策略解析 -------------------------------------------------

    def resolve_effective(self, tenant_id: str, body: dict) -> dict:
        """`POST /policy-decisions:resolve-effective`（§5）。

        `node_epoch` / `execution_fence` 参与**回显**，绝不参与合并（§5 行 373）。
        """
        context = body.get("context") if isinstance(body, dict) else None
        if not isinstance(context, dict):
            raise ServiceError(400, "MALFORMED_REQUEST", "context is required")
        if context.get("tenant_id") not in (None, tenant_id):
            raise ServiceError(403, "TENANT_MISMATCH",
                               "context.tenant_id disagrees with the signed context")
        path = {k: context.get(k) for k in
                ("project_id", "episode_id", "stage_id", "node_id")}
        if not path["project_id"]:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "context.project_id is required")

        pinned = context.get("policy_ref")
        if pinned is not None:
            # §5 行 374：拿旧策略去推进必须被挡，且先于解析就挡。
            self._assert_pinned_current(tenant_id, pinned)

        layers, root_ref = self._collect_layers(tenant_id, path)
        if root_ref is None:
            raise ServiceError(404, "POLICY_NOT_FOUND",
                               "no enterprise policy is bound for this tenant")

        effective, provenance = M.merge_layers(layers)
        if effective.get("mode") is None:
            # §4.4 注 1：企业层必须有 mode，否则 422 ENTERPRISE_POLICY_MISSING。
            raise ServiceError(422, "ENTERPRISE_POLICY_MISSING",
                               "no policy provides a mode for this tenant")

        window = effective_window(layers)
        now = self._now()
        if window["until"] is not None and now >= window["until"]:
            # §5 行 376：过期不回落，直接拒。
            raise ServiceError(409, "POLICY_EXPIRED",
                               "the effective window does not cover now",
                               details={"window": window})

        return {
            "schema_version": "2.0",
            "policy_ref": {"service_id": "S02", **root_ref},
            "evaluated_for": {
                "manifest_ref": context.get("manifest_ref"),
                "node_epoch": context.get("node_epoch"),
                "execution_fence": context.get("execution_fence"),
            },
            "effective": {
                "mode": effective.get("mode"),
                "generation_mode": effective.get("generation_mode"),
                "acceptance_mode": effective.get("acceptance_mode"),
                "required_gates": effective.get("required_gates") or [],
                "repair_limits": {
                    "max_business_attempts_per_shot":
                        effective.get("max_business_attempts_per_shot"),
                    "max_identical_strategy_repeats":
                        effective.get("max_identical_strategy_repeats")},
                "exception_behavior": effective.get("exception_behavior"),
                "allowed_actions": effective.get("allowed_actions"),
                "reviewer_roles": effective.get("reviewer_roles"),
                "hard_rules": _deepest(layers, "hard_rules"),
                "locked": bool(effective.get("locked", False)),
                "effective_window": window,
            },
            "provenance": provenance,
            "normalized": provenance.get("normalized", []),
            "gate_plan": [{"gate": g,
                           "phase": GATE_PHASE.get(g, "pre_dispatch"),
                           "stop_at": "WAITING_GATE"}
                          for g in effective.get("required_gates") or []],
            "source_revision": root_ref["revision"],
            "computed_at": now,
        }

    def _collect_layers(self, tenant_id: str,
                        path: dict) -> tuple[list[dict], dict | None]:
        """§4.2 行 143-151 的 `LayersFor`，A/B/C 三类。

        返回的**顺序无关紧要**：合并用 join 算子，交换律由 `test_merge.py`
        逐算子验证。层缺失只是少一个 layer，不会让结果漂移。
        """
        chain = [("tenant", tenant_id)]
        if path.get("project_id"):
            chain.append(("project", path["project_id"]))
        if path.get("episode_id"):
            chain.append(("episode", path["episode_id"]))
        if path.get("stage_id"):
            chain.append(("stage", path["stage_id"]))
        if path.get("node_id"):
            chain.append(("node", path["node_id"]))

        layers: list[dict] = []
        root_ref: dict | None = None
        seen: set[str] = set()

        for index, (scope_type, scope_id) in enumerate(chain):
            # A 类：绑定在本层的文档顶层字段。
            for binding in self._bindings_for(tenant_id, scope_type, scope_id):
                layer = self._load_layer(tenant_id, binding, scope_type, scope_id)
                layers.append(layer)
                if scope_type == "tenant":
                    root_ref = {"id": binding["policy_id"],
                                "revision": int(binding["revision"]),
                                "content_hash": binding["content_hash"]}

            # B/C 类：任何祖先文档里命中本 scope 的 override。
            for ancestor_type, ancestor_id in chain[:index]:
                for binding in self._bindings_for(tenant_id, ancestor_type,
                                                  ancestor_id):
                    rev = self._revision(tenant_id, binding["policy_id"],
                                         int(binding["revision"]))
                    document = json.loads(rev["document"])
                    for ov in document.get("overrides") or []:
                        if ov["scope_type"] != scope_type:
                            continue
                        if not scope_matches(ov["scope_id"], scope_id, scope_type):
                            continue
                        marker = jcs([binding["policy_id"],
                                      int(binding["revision"]), ov])
                        if marker in seen:
                            continue
                        seen.add(marker)
                        layers.append({
                            **{f: ov.get(f) for f in
                               ("generation_mode", "acceptance_mode",
                                "reviewer_roles", "locked")},
                            "_source": {"scope_type": scope_type,
                                        "scope_id": scope_id,
                                        "policy_id": binding["policy_id"],
                                        "revision": int(binding["revision"]),
                                        "content_hash": binding["content_hash"],
                                        "effective_at": rev["effective_at"],
                                        "via": "override"}})

        return layers, root_ref

    def _assert_pinned_current(self, tenant_id: str, pinned: dict) -> None:
        """§5 行 374：`policy_ref` 必须是当前有效 revision，否则 409。"""
        policy_id = pinned.get("id")
        revision = pinned.get("revision")
        if not policy_id or revision is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "policy_ref needs id and revision")
        pointer = self._pointer(tenant_id, policy_id)
        if int(pointer["current_revision"]) != int(revision):
            raise ServiceError(
                409, "POLICY_SUPERSEDED",
                "policy_ref does not point at the current published revision",
                details={"pinned": int(revision),
                         "current": int(pointer["current_revision"])})


def top_level_fields(document: dict) -> dict:
    """从一份 `automation-policy.v2` 文档取参与合并的顶层字段。

    `repair_limits` 在文档里是嵌套对象（`{"max_business_attempts_per_shot": …,
    "max_identical_strategy_repeats": …}`），而 join 表按两个独立计数器建（§3
    #5/#6 是两行）。这里拆平；不拆的话两个计数器永远读不到，`min` 也就永远是
    `None`——而 `None` 是单位元，于是"更严的那层"静默消失。
    """
    fields = {f: document.get(f) for f in M.MERGE}
    limits = document.get("repair_limits") or {}
    fields["max_business_attempts_per_shot"] = limits.get(
        "max_business_attempts_per_shot")
    fields["max_identical_strategy_repeats"] = limits.get(
        "max_identical_strategy_repeats")
    rules = document.get("hard_rules") or {}
    fields["budget_scope_id"] = rules.get("budget_scope_id")
    # `hard_rules` 整体也带上：它不参与 join（四个 const 一律不可变，§4.5 (e)），
    # 但 §5 的响应要把它交给调用方——`rights_required`、`budget_scope_id` 之类
    # 正是 S26 派单前要读的。`merge_layers` 只遍历 `MERGE` 的键，多带一个不会被
    # 当成可 join 字段；取值在 `resolve_effective` 里按"最specific 层胜"选。
    fields["hard_rules"] = document.get("hard_rules")
    return {k: v for k, v in fields.items() if v is not None}


def _deepest(layers: list[dict], field: str):
    """取**最具体那一层**的字段值；`layers` 已按 tenant→node 排好。

    `hard_rules` 不能 join：三条款是 `const true`、一条是 `const false`，变了就是
    `HARD_RULE_IMMUTABLE`，所以各层只有 `budget_scope_id` 会不同。它的「更严」
    方向由 §4.5 (d) 的有序校验保证（后代 ⊆ 祖先），不是格上的 min/max——
    所以这里按层序取最后一个，而不是套一个算不出来的 join。
    """
    for layer in reversed(layers):
        if layer.get(field) is not None:
            return layer[field]
    return None


def effective_window(layers: list[dict]) -> dict:
    """§4.4 行 235：`[max(start), min(expiry)]`。

    `expiry` 在 v2 schema 里不存在（§11 G2），所以 `until` 恒为 `None` 表示
    "无上界"。这不是把过期检查悄悄关掉——`POLICY_EXPIRED` 仍然在
    `resolve_effective` 里判，只是当前契约下没有能填的值。
    """
    starts = [layer["_source"]["effective_at"] for layer in layers
              if layer["_source"].get("effective_at")]
    return {"from": max(starts) if starts else None, "until": None}


def scope_matches(pattern: str, scope_id: str, scope_type: str) -> bool:
    """§4.2 行 149 的 C 类通配：`shot_3.*` 命中 `shot_3.video`。

    只支持尾部 `*`，且**只在 node 层**生效——其余层的 id 是实体 id，通配会让
    "某个项目"变成"所有项目"，那是覆盖范围扩张，不是收紧。
    """
    if pattern == scope_id:
        return True
    return scope_type == "node" and pattern.endswith("*") and \
        scope_id.startswith(pattern[:-1])
