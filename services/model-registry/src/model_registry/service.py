"""S24 业务逻辑：渠道、账户池、能力快照、能力判定、价格与报价上界、生命周期守卫。

本服务是「模型能力与价格」的唯一权威（docs/01 权威表）。它**不**写余额（S23）、
不写 permit 与在途槽（S22）、不写任务状态（S26）、不写供应商回执（S27）、
不做路由决策（S25）。只回答三个问题：哪些候选合法、这个形状是否被验证支持、
这个候选的可验证报价上界是多少。

本轮实现方案 §9 的第 1–3、5 步（快照与只读查询、判定引擎与规范化、价格与上界、
批量有效性）。**没有**实现：
  - §9 第 4 步的只读探测 worker：需要真实供应商只读凭证与网络，本地 mock 只能
    得到「绿但什么都没证明」的测试；
  - §9 第 6 步的付费探测接 S26：S26 尚未实现，直连模型又违反 AGENTS.md；
  - §4.3 的调度与 §4.2 的过期扫描定时器：逻辑写成可调用方法且有测试，但没有
    worker 进程与调度器。
方案末节登记了这些偏离与全部未决（C24-1…C24-12）。

时间一律存成固定宽度 ISO-8601（`_ts`）。SQLite 没有时间类型，`valid_until > now()`
是**字符串比较**，宽度不固定就会比错。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING

from event_sdk import Outbox, new_event

from .combination import jcs, key_for, normalize_refs
from .schema import SCHEMA

SOURCE = "s24.model-registry"
EVENT_TYPE = "model.capability.changed"

LIFECYCLES = ("disabled", "validating", "active", "degraded", "retired")
EVIDENCE_STATES = ("documented", "probe_verified", "field_failed")
SUPPORTS = ("unsupported", "documented", "probe_verified", "field_failed")
PROBE_RESULTS = ("supported", "unsupported", "inconclusive")
EXECUTION_KINDS = ("sync", "poll", "callback", "hybrid")
HEALTH_STATES = ("healthy", "suspect", "unauthorized", "exhausted", "disabled")
METHODS = ("doc_read", "account_probe", "paid_probe", "field_observation")

# §4.3 行 671：「探测失败连续 3 次 → 池 health='suspect'」。方案给的是数字，
# 不是「若干次」——所以它可以落到代码里，不需要 owner 再填。
PROBE_FAIL_THRESHOLD = 3

# §4.1 的生命周期图。`retired` 是终态：没有出边。
LIFECYCLE_EDGES = {
    "disabled": {"validating"},
    "validating": {"active", "retired", "disabled"},
    "active": {"degraded", "retired", "disabled"},
    "degraded": {"active", "retired", "disabled"},
    "retired": set(),
}

# §3.1 第 5 条：历史端点永远 retired
HISTORICAL_CHANNELS = {"sora": {"historical"}}

# 币种最小单位的小数位。JPY 0 位；未列出的按 2 位向上取整（C24-7）。
CURRENCY_MINOR_DIGITS = {"USD": 2, "EUR": 2, "CNY": 2, "GBP": 2, "JPY": 0}


def _now() -> str:
    return _ts(datetime.now(timezone.utc))


def _ts(value) -> str:
    """固定宽度 ISO-8601 UTC：`YYYY-MM-DDTHH:MM:SS.ffffffZ`（总是 27 字符）。

    输入可以是 datetime、带 Z 的串、带偏移的串或 epoch 秒。规范化后再落库，
    字符串比较才等价于时间比较，`valid_until > ?` 才不会因为少了微秒而错判。
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


def _loads(text, default=None):
    if text is None:
        return default
    return json.loads(text)


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """显式事务。

    `sqlite3.Connection.__enter__` 在 `isolation_level=None`（autocommit）下不开启
    事务——它只返回连接本身。用它包住「同事务写事实与 outbox」会得到一组各自
    立即提交的语句。这里显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


class ModelRegistryService:
    def __init__(self, conn: sqlite3.Connection, *, clock=_now):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(conn)
        self.clock = clock

    # -- 内部：时间与自增 ---------------------------------------------------

    def _now(self) -> str:
        return _ts(self.clock())

    def _next_bundle_rev(self) -> int:
        """§6.2：全局单调计数器，串行化在 registry_counter 单行上。"""
        row = self.conn.execute(
            "SELECT value FROM registry_counter WHERE name = 'bundle_rev'"
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO registry_counter (name, value) VALUES ('bundle_rev', 1)")
            return 1
        value = int(row["value"]) + 1
        self.conn.execute(
            "UPDATE registry_counter SET value = ? WHERE name = 'bundle_rev'",
            (value,))
        return value

    def bundle_revision(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM registry_counter WHERE name = 'bundle_rev'"
        ).fetchone()
        return int(row["value"]) if row else 0

    # -- 内部：读 -----------------------------------------------------------

    def _snapshot_row(self, snapshot_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM model_capability_snapshot WHERE snapshot_id = ?",
            (snapshot_id,)).fetchone()
        if row is None:
            raise ServiceError(404, "SNAPSHOT_NOT_FOUND",
                               f"snapshot {snapshot_id} does not exist")
        return row

    def _modes_of(self, snapshot_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM capability_mode WHERE snapshot_id = ? ORDER BY mode",
            (snapshot_id,)).fetchall()
        return [dict(r) for r in rows]

    def _output_spec(self, snapshot_id: str, mode: str, aspect_ratio: str):
        row = self.conn.execute(
            "SELECT * FROM mode_output_spec WHERE snapshot_id = ? AND mode = ?"
            " AND aspect_ratio = ?", (snapshot_id, mode, aspect_ratio)).fetchone()
        if row is None:
            return None
        return {
            "aspect_ratio": row["aspect_ratio"],
            "max_width": row["max_width"],
            "max_height": row["max_height"],
            "duration_tiers_s": _loads(row["duration_tiers_s"], []),
            "trims_to_nearest": bool(row["trims_to_nearest"]),
        }

    def _probe(self, snapshot_id: str, mode: str, combination_key: str):
        return self.conn.execute(
            "SELECT * FROM mode_combination_probe WHERE snapshot_id = ?"
            " AND mode = ? AND combination_key = ?",
            (snapshot_id, mode, combination_key)).fetchone()

    def _price_row(self, price_snapshot_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM price_snapshot WHERE price_snapshot_id = ?",
            (price_snapshot_id,)).fetchone()
        if row is None:
            raise ServiceError(404, "PRICE_SNAPSHOT_NOT_FOUND",
                               f"price snapshot {price_snapshot_id} does not exist")
        return row

    # -- 写：渠道与账户池 ---------------------------------------------------

    def register_channel(self, body: dict) -> dict:
        """§2.1 渠道。credential_ref 只存引用，不含密钥值。"""
        for key in ("provider", "channel", "connector_deployment",
                    "egress_identity", "credential_ref"):
            if not isinstance(body.get(key), str) or not body[key].strip():
                raise ServiceError(400, "INVALID_REQUEST", f"{key} is required")
        lifecycle = body.get("lifecycle", "disabled")
        if lifecycle not in LIFECYCLES:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"lifecycle {lifecycle!r} is not one of {LIFECYCLES}")
        retired_reason = body.get("retired_reason")
        if lifecycle == "retired" and not retired_reason:
            raise ServiceError(400, "INVALID_REQUEST",
                               "retired requires retired_reason")
        version = body.get("credential_version", 1)
        if not _int(version) or version < 1:
            raise ServiceError(400, "INVALID_REQUEST",
                               "credential_version must be an integer >= 1")
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO provider_channel (provider, channel,"
                " connector_deployment, egress_identity, credential_ref,"
                " credential_version, lifecycle, retired_reason, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (provider, channel) DO UPDATE SET"
                " connector_deployment=excluded.connector_deployment,"
                " egress_identity=excluded.egress_identity,"
                " credential_ref=excluded.credential_ref,"
                " credential_version=excluded.credential_version,"
                " lifecycle=excluded.lifecycle,"
                " retired_reason=excluded.retired_reason,"
                " updated_at=excluded.updated_at",
                (body["provider"], body["channel"], body["connector_deployment"],
                 body["egress_identity"], body["credential_ref"], version,
                 lifecycle, retired_reason, self._now()))
        return {"provider": body["provider"], "channel": body["channel"],
                "lifecycle": lifecycle}

    def register_pool(self, body: dict) -> dict:
        """§2.1 账户池。BYOK 硬规则在 DDL 与这里各拦一次。"""
        for key in ("pool_id", "tenant_id", "provider", "channel", "region",
                    "scope", "data_region"):
            if not isinstance(body.get(key), str) or not body[key].strip():
                raise ServiceError(400, "INVALID_REQUEST", f"{key} is required")
        scope = body["scope"]
        if scope not in ("shared", "tenant_byok"):
            raise ServiceError(400, "INVALID_REQUEST",
                               f"scope {scope!r} must be shared or tenant_byok")
        tenant_id = body["tenant_id"]
        if scope == "tenant_byok" and tenant_id == "*":
            # BYOK 池必须归属某个具体企业，否则它会变成所有人都能借的凭证
            raise ServiceError(400, "CROSS_TENANT_DENIED",
                               "tenant_byok pool needs a concrete tenant_id")
        if scope == "shared" and tenant_id != "*":
            raise ServiceError(400, "INVALID_REQUEST",
                               "shared pool uses tenant_id '*'")
        if scope == "tenant_byok" and not body.get("credential_ref"):
            raise ServiceError(400, "INVALID_REQUEST",
                               "tenant_byok pool requires credential_ref")
        for key in ("max_inflight", "rpm"):
            if not _int(body.get(key)) or body[key] < 0:
                raise ServiceError(400, "INVALID_REQUEST",
                                   f"{key} must be a non-negative integer")
        health = body.get("health", "disabled")
        if health not in HEALTH_STATES:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"health {health!r} is not one of {HEALTH_STATES}")
        enabled = 1 if body.get("enabled") else 0
        with Transaction(self.conn):
            if self.conn.execute(
                    "SELECT 1 FROM provider_channel WHERE provider = ?"
                    " AND channel = ?",
                    (body["provider"], body["channel"])).fetchone() is None:
                raise ServiceError(422, "CHANNEL_NOT_FOUND",
                                   "provider_channel must exist before a pool")
            self.conn.execute(
                "INSERT INTO provider_account_pool (pool_id, tenant_id, provider,"
                " channel, region, scope, credential_ref, credential_version,"
                " data_region, max_inflight, rpm, tpm, health, health_reason,"
                " health_checked_at, enabled, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (pool_id) DO UPDATE SET"
                " max_inflight=excluded.max_inflight, rpm=excluded.rpm,"
                " tpm=excluded.tpm, health=excluded.health,"
                " health_reason=excluded.health_reason,"
                " health_checked_at=excluded.health_checked_at,"
                " enabled=excluded.enabled,"
                " credential_version=excluded.credential_version,"
                " credential_ref=excluded.credential_ref",
                (body["pool_id"], tenant_id, body["provider"], body["channel"],
                 body["region"], scope, body.get("credential_ref"),
                 body.get("credential_version"), body["data_region"],
                 body["max_inflight"], body["rpm"], body.get("tpm"), health,
                 body.get("health_reason"), body.get("health_checked_at"),
                 enabled, self._now()))
        return {"pool_id": body["pool_id"], "scope": scope, "health": health,
                "enabled": bool(enabled)}

    def set_pool_health(self, pool_id: str, health: str,
                        reason: str | None = None) -> dict:
        """§4.3：探测结果的落点。401/403 → unauthorized，连续失败 → suspect。

        **不发事件**：§3.5 的 `data` 只有 snapshot 字段（`additionalProperties:
        false`），`aggregate_id` 里也不含池，发出去下游定位不到是哪个池；同一快照
        的 `capability_bundle_revision` 还会被重复当作 `aggregate_sequence`。
        这是契约缺口 C24-3——不靠加字段绕开已发布的 schema。
        """
        if health not in HEALTH_STATES:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"health {health!r} is not one of {HEALTH_STATES}")
        if self.conn.execute(
                "SELECT 1 FROM provider_account_pool WHERE pool_id = ?",
                (pool_id,)).fetchone() is None:
            raise ServiceError(404, "POOL_NOT_FOUND", f"pool {pool_id} does not exist")
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE provider_account_pool SET health = ?, health_reason = ?,"
                " health_checked_at = ? WHERE pool_id = ?",
                (health, reason, self._now(), pool_id))
        return {"pool_id": pool_id, "health": health}

    def record_probe_outcome(self, pool_id: str, *, status: int,
                             method: str = "account_probe",
                             reason: str | None = None) -> dict:
        """§4.3 的两条推导规则。**由探测结论决定 health，不由调用方给结论。**

        - 401/403 → 立即 `unauthorized`（`§4.3` 行 672），且**不靠连续计数**：
          凭证被拒不需要"三次确认"，第一次就该停用。
        - 其余失败连续满 `PROBE_FAIL_THRESHOLD` 次 → `suspect`（`§4.3` 行 671）。
        - 成功 → 回到 `healthy` 并把计数清零。

        与 `set_pool_health` 的分工：那个收的是**人工判定**（人要写什么就是什么，
        还带 reason）；这个收的是**探测原始结论**（HTTP 状态码），健康态由本方法
        按方案推导。让调用方自己算「几次算连续」等于把 §4.3 的阈值搬到每个调用方。

        **不发事件**（C24-3）：`model.capability.changed` 的 `data` 装不下池。
        """
        if method not in METHODS:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"method {method!r} is not one of {METHODS}")
        if not _int(status):
            raise ServiceError(400, "INVALID_REQUEST",
                               "probe status must be an integer HTTP status")
        if self.conn.execute(
                "SELECT 1 FROM provider_account_pool WHERE pool_id = ?",
                (pool_id,)).fetchone() is None:
            raise ServiceError(404, "POOL_NOT_FOUND", f"pool {pool_id} does not exist")

        failed = not (200 <= status < 300)
        with Transaction(self.conn):
            row = self.conn.execute(
                "SELECT consecutive_fail FROM account_pool_probe_state"
                " WHERE pool_id = ?", (pool_id,)).fetchone()
            previous = int(row["consecutive_fail"]) if row else 0
            # 401/403 与「其它失败」分开计数：一次凭证被拒不该被之前的 5xx
            # 计数放大，也不该因为后面来了个 500 就慢慢"降级"成 suspect。
            unauthorized = status in (401, 403)
            fails = 0 if not failed else (
                1 if unauthorized else previous + 1)

            if not failed:
                health, why = "healthy", None
            elif unauthorized:
                health, why = "unauthorized", reason or f"probe returned {status}"
            elif fails >= PROBE_FAIL_THRESHOLD:
                health, why = "suspect", reason or (
                    f"{fails} consecutive probe failures")
            else:
                health, why = None, None   # 还没到阈值，不动健康态

            now = self._now()
            self.conn.execute(
                "INSERT INTO account_pool_probe_state (pool_id, consecutive_fail,"
                " last_method, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (pool_id) DO UPDATE SET"
                " consecutive_fail=excluded.consecutive_fail,"
                " last_method=excluded.last_method,"
                " updated_at=excluded.updated_at",
                (pool_id, fails, method, now))
            if health is not None:
                self.conn.execute(
                    "UPDATE provider_account_pool SET health = ?, health_reason = ?,"
                    " health_checked_at = ? WHERE pool_id = ?",
                    (health, why, now, pool_id))
            current = self.conn.execute(
                "SELECT health FROM provider_account_pool WHERE pool_id = ?",
                (pool_id,)).fetchone()["health"]
        return {"pool_id": pool_id, "status": status, "health": current,
                "consecutive_fail": fails, "changed": health is not None}

    # -- 写：S01 授权投影缓存 -----------------------------------------------

    def apply_authorization_projection(self, tenant_id: str, *,
                                       home_region: str | None,
                                       allowed_residency_tags: list[str],
                                       source_revision: str | None,
                                       resolved_at: str) -> dict:
        """§5.2 的「S01 已缓存授权投影」。

        **这不是权威**：S24 不判定授权，只是把 S01 给出的投影缓存下来，并记住
        它的水印。写入者只应是 S01 的事件消费者（尚未实现），本服务没有任何
        逻辑会自己算出授权事实。
        """
        tags = sorted({t for t in (allowed_residency_tags or [])
                       if isinstance(t, str) and t})
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO authorization_projection_cache (tenant_id,"
                " home_region, allowed_residency_tags, source_revision, resolved_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (tenant_id) DO UPDATE SET"
                " home_region=excluded.home_region,"
                " allowed_residency_tags=excluded.allowed_residency_tags,"
                " source_revision=excluded.source_revision,"
                " resolved_at=excluded.resolved_at",
                (tenant_id, home_region, jcs(tags), source_revision,
                 _ts(resolved_at)))
        return {"tenant_id": tenant_id, "resolved_at": _ts(resolved_at)}

    def _authorization_projection(self, tenant_id: str) -> sqlite3.Row:
        """§5.2：读路径只用本地库 + 已缓存投影；投影缺失 → 503，不降级放行。"""
        row = self.conn.execute(
            "SELECT * FROM authorization_projection_cache WHERE tenant_id = ?",
            (tenant_id,)).fetchone()
        if row is None:
            raise ServiceError(
                503, "AUTH_DEPENDENCY_UNAVAILABLE",
                "no cached S01 authorization projection for this tenant",
                True)
        return row

    # -- 写：能力快照 -------------------------------------------------------

    def register_snapshot(self, body: dict) -> dict:
        """§3.1 `POST /capability-snapshots`。

        `snapshot_id` 内容寻址：同一 claim 集重复登记不产生新行，也不递增
        `capability_bundle_revision`（T24-01）。`lifecycle` **不**参与哈希——
        它是可变状态（§4.2 会 UPDATE 它），参与哈希会让每次降级都变成一个新快照。
        """
        snap = body.get("snapshot")
        if not isinstance(snap, dict):
            raise ServiceError(400, "INVALID_REQUEST", "snapshot is required")
        expected_revision = body.get("expected_revision")
        if expected_revision is not None and not _int(expected_revision):
            raise ServiceError(400, "INVALID_REQUEST",
                               "expected_revision must be an integer")

        self._validate_snapshot_shape(snap)
        snapshot_id = hashlib.sha256(jcs(self._claim_set(snap)).encode()).hexdigest()

        with Transaction(self.conn):
            existing = self.conn.execute(
                "SELECT * FROM model_capability_snapshot WHERE snapshot_id = ?",
                (snapshot_id,)).fetchone()
            if existing is not None:
                return self._snapshot_response(existing, replayed_content=True)

            current = self.bundle_revision()
            if expected_revision is not None and expected_revision != current:
                raise ServiceError(
                    409, "REVISION_CONFLICT",
                    f"expected_revision {expected_revision} != current {current}",
                    details={"current_revision": current})

            self._assert_not_occupied(snap)
            self._assert_evidence_guards(snap)
            self._assert_channel_guards(snap)

            revision = self._next_bundle_rev()
            accepted_modes, rejected = self._write_snapshot_rows(
                snap, snapshot_id, revision)
            self._close_previous(snap, snapshot_id)
            prices = body.get("prices") or []
            if not isinstance(prices, list):
                raise ServiceError(400, "INVALID_REQUEST", "prices must be a list")
            price_ids = [self._write_price(p, snap) for p in prices]

            self._record_transition(snapshot_id, None, snap["lifecycle"],
                                    reason="SNAPSHOT_REGISTERED", evidence_id=None,
                                    actor=body.get("actor") or "s24.api")
            row = self._snapshot_row(snapshot_id)
            self._emit(row, change_kind="snapshot_published",
                       changed_modes=accepted_modes)
        result = self._snapshot_response(row, replayed_content=False)
        result["rejected_claims"] = rejected
        result["price_snapshot_ids"] = price_ids
        return result

    def _validate_snapshot_shape(self, snap: dict) -> None:
        for key in ("provider", "channel", "model_id", "model_snapshot", "region",
                    "account_pool_id", "lifecycle", "data_region",
                    "effective_from"):
            if not isinstance(snap.get(key), str) or not snap[key].strip():
                raise ServiceError(400, "INVALID_REQUEST",
                                   f"snapshot.{key} is required")
        if snap["lifecycle"] not in LIFECYCLES:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"snapshot.lifecycle {snap['lifecycle']!r} is invalid")
        tags = snap.get("residency_tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ServiceError(400, "INVALID_REQUEST",
                               "snapshot.residency_tags must be a string array")
        if not isinstance(snap.get("modes"), list) or not snap["modes"]:
            raise ServiceError(400, "INVALID_REQUEST",
                               "snapshot.modes must be a non-empty array")
        for mode in snap["modes"]:
            self._validate_mode(mode)

    def _validate_mode(self, mode: dict) -> None:
        name = mode.get("mode")
        if not isinstance(name, str) or not name.strip():
            raise ServiceError(400, "INVALID_REQUEST", "mode.mode is required")
        if mode.get("support") not in SUPPORTS:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"mode {name}: support {mode.get('support')!r} is invalid")
        if mode.get("execution_kind") not in EXECUTION_KINDS:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"mode {name}: execution_kind is invalid")
        for rule in mode.get("reference_rules") or []:
            self._validate_rule(name, rule)
        for claim in mode.get("capability_claims") or []:
            if claim.get("support") not in SUPPORTS:
                raise ServiceError(
                    400, "INVALID_REQUEST",
                    f"mode {name}: claim {claim.get('capability_key')!r} has"
                    " invalid support")
        for spec in mode.get("output_specs") or []:
            tiers = spec.get("duration_tiers_s") or []
            if not isinstance(tiers, list) or not all(
                    _int(t) and t > 0 for t in tiers):
                raise ServiceError(
                    400, "INVALID_REQUEST",
                    f"mode {name}: duration_tiers_s must be positive integers")

    def _validate_rule(self, mode: str, rule: dict) -> None:
        role = rule.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ServiceError(400, "INVALID_REQUEST",
                               f"mode {mode}: reference rule needs a role")
        low = rule.get("min_count", 0)
        high = rule.get("max_count", 0)
        for value, label in ((low, "min_count"), (high, "max_count")):
            if not _int(value) or value < 0:
                raise ServiceError(400, "INVALID_REQUEST",
                                   f"mode {mode}/{role}: {label} must be >= 0")
        if low > high:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"mode {mode}/{role}: min_count {low} > max_count {high}")
        if rule.get("ordering", "unordered") not in ("unordered", "ordered"):
            raise ServiceError(400, "INVALID_REQUEST",
                               f"mode {mode}/{role}: ordering is invalid")
        excl = rule.get("exclusive_with") or []
        if not isinstance(excl, list) or not all(isinstance(e, str) for e in excl):
            raise ServiceError(400, "INVALID_REQUEST",
                               f"mode {mode}/{role}: exclusive_with must be strings")

    def _claim_set(self, snap: dict) -> dict:
        """内容寻址的 claim 集：确定「这是哪个快照」的全部不可变事实。

        刻意**不含** `lifecycle`、`lifecycle_reason`、证据与组合探测行——它们是
        随后追加/迁移的状态，不是身份。含进去会让同一条目每次降级都换 id。
        """
        modes = []
        for mode in snap["modes"]:
            modes.append({
                "mode": mode["mode"],
                "support": mode["support"],
                "execution_kind": mode["execution_kind"],
                "prompt_required": bool(mode.get("prompt_required", False)),
                "prompt_max_chars": mode.get("prompt_max_chars"),
                "native_audio": bool(mode.get("native_audio", False)),
                "prompt_audio_lock": bool(mode.get("prompt_audio_lock", False)),
                "evidence_id": mode.get("evidence_id"),
                "reference_rules": sorted(
                    ({"role": r.get("role"),
                      "min_count": r.get("min_count", 0),
                      "max_count": r.get("max_count", 0),
                      "ordering": r.get("ordering", "unordered"),
                      # 互斥是对称关系，排序后入哈希才与书写顺序无关
                      "exclusive_with": sorted(r.get("exclusive_with") or [])}
                     for r in (mode.get("reference_rules") or [])),
                    key=lambda r: r["role"]),
                "output_specs": sorted(
                    ({"aspect_ratio": s.get("aspect_ratio"),
                      "max_width": s.get("max_width"),
                      "max_height": s.get("max_height"),
                      "duration_tiers_s": sorted(s.get("duration_tiers_s") or []),
                      "trims_to_nearest": bool(s.get("trims_to_nearest", False))}
                     for s in (mode.get("output_specs") or [])),
                    key=lambda s: s["aspect_ratio"]),
                "capability_claims": sorted(
                    ({"capability_key": c.get("capability_key"),
                      "support": c.get("support"),
                      "params": c.get("params") or {},
                      "evidence_id": c.get("evidence_id")}
                     for c in (mode.get("capability_claims") or [])),
                    key=lambda c: c["capability_key"]),
                "input_limits": sorted(
                    ({"media_kind": l.get("media_kind"),
                      "max_bytes": l.get("max_bytes"),
                      "max_pixels": l.get("max_pixels"),
                      "max_duration_s": l.get("max_duration_s"),
                      "allowed_mime": sorted(l.get("allowed_mime") or [])}
                     for l in (mode.get("input_limits") or [])),
                    key=lambda l: l["media_kind"]),
            })
        modes.sort(key=lambda m: m["mode"])
        return {
            "provider": snap["provider"],
            "channel": snap["channel"],
            "model_id": snap["model_id"],
            "model_snapshot": snap["model_snapshot"],
            "region": snap["region"],
            "account_pool_id": snap["account_pool_id"],
            "data_region": snap["data_region"],
            "residency_tags": sorted(snap["residency_tags"]),
            "effective_from": _ts(snap["effective_from"]),
            "modes": modes,
        }

    def _assert_not_occupied(self, snap: dict) -> None:
        """同一自然键 + 同一 `effective_from` 已属于别的快照。

        DDL 的 `UNIQUE (..., effective_from)` 会拦，但拦下来是 `IntegrityError`，
        对调用方只说「数据库错误」。提前判成 409，让它知道该重读而不是重试。
        """
        clash = self.conn.execute(
            "SELECT snapshot_id FROM model_capability_snapshot"
            " WHERE provider = ? AND channel = ? AND model_id = ?"
            " AND model_snapshot = ? AND region = ? AND account_pool_id = ?"
            " AND effective_from = ?",
            (snap["provider"], snap["channel"], snap["model_id"],
             snap["model_snapshot"], snap["region"], snap["account_pool_id"],
             _ts(snap["effective_from"]))).fetchone()
        if clash is not None:
            raise ServiceError(
                409, "REVISION_CONFLICT",
                "another snapshot already occupies this effective_from",
                details={"occupying_snapshot_id": clash["snapshot_id"]})

    def _assert_channel_guards(self, snap: dict) -> None:
        """§3.1 第 5 条：历史端点不许被激活。

        方案原话是「任何提交若 lifecycle != 'retired' 一律 422」，所以不按
        `active` 判——`validating` 也照样拦。§4.1 也没有 `retired → *` 的边。
        """
        if HISTORICAL_CHANNELS.get(snap["provider"], set()) & {snap["channel"]}:
            if snap["lifecycle"] != "retired":
                raise ServiceError(
                    422, "HISTORICAL_ENDPOINT_CANNOT_BE_ACTIVATED",
                    f"{snap['provider']}/{snap['channel']} is a historical endpoint;"
                    " only lifecycle=retired may be registered")

    def _assert_evidence_guards(self, snap: dict) -> None:
        """§3.1 第 1 条：写 active 必须有未过期的探测证据。"""
        if snap["lifecycle"] != "active":
            return
        now = self._now()
        good = [e for e in (snap.get("evidence") or [])
                if e.get("method") in ("account_probe", "paid_probe")
                and e.get("result") == "supported"
                and _ts(e.get("valid_until")) > now]
        if not good:
            raise ServiceError(
                422, "ACTIVE_REQUIRES_PROBE_EVIDENCE",
                "lifecycle=active needs at least one unexpired"
                " account_probe/paid_probe evidence with result=supported")

    def _write_snapshot_rows(self, snap: dict, snapshot_id: str,
                             revision: int) -> tuple[list[str], list[dict]]:
        """写快照 + 模式族。返回 (被接受的模式, 被降级的 claim 列表)。

        §3.1 第 2 条：`support='probe_verified'` 的 claim 必须指向真实证据，
        否则**降级为 documented 并在响应里列出**——不静默接受，也不整单拒绝。
        """
        now = self._now()
        evidence_ids = {e.get("evidence_id") for e in (snap.get("evidence") or [])}
        for evidence in snap.get("evidence") or []:
            self._write_evidence(evidence, snapshot_id)

        valid_until = min(
            (_ts(e["valid_until"]) for e in (snap.get("evidence") or [])),
            default=None)
        self.conn.execute(
            "INSERT INTO model_capability_snapshot (snapshot_id, tenant_id,"
            " provider, channel, model_id, model_snapshot, region, account_pool_id,"
            " capability_bundle_rev, lifecycle, lifecycle_reason, data_region,"
            " residency_tags, evidence_state, evidence_valid_until, effective_from,"
            " effective_to, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (snapshot_id, snap.get("tenant_id", "*"), snap["provider"],
             snap["channel"], snap["model_id"], snap["model_snapshot"],
             snap["region"], snap["account_pool_id"], revision, snap["lifecycle"],
             snap.get("lifecycle_reason"), snap["data_region"],
             jcs(sorted(snap["residency_tags"])),
             snap.get("evidence_state", "documented"), valid_until,
             _ts(snap["effective_from"]), now))

        accepted: list[str] = []
        rejected: list[dict] = []
        for mode in snap["modes"]:
            name = mode["mode"]
            self.conn.execute(
                "INSERT INTO capability_mode (snapshot_id, mode, support,"
                " execution_kind, prompt_required, prompt_max_chars, native_audio,"
                " prompt_audio_lock, evidence_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, name, mode["support"], mode["execution_kind"],
                 1 if mode.get("prompt_required") else 0, mode.get("prompt_max_chars"),
                 1 if mode.get("native_audio") else 0,
                 1 if mode.get("prompt_audio_lock") else 0, mode.get("evidence_id")))
            accepted.append(name)
            self._write_rules(snapshot_id, name, mode)
            self._write_claims(snapshot_id, name, mode, evidence_ids, rejected)
            self._write_output_specs(snapshot_id, name, mode)
            self._write_input_limits(snapshot_id, name, mode)
        for probe in snap.get("combination_probes") or []:
            self._write_probe(snapshot_id, probe, now=now)
        return accepted, rejected

    def _write_evidence(self, evidence: dict, snapshot_id: str) -> None:
        if not isinstance(evidence.get("evidence_id"), str):
            raise ServiceError(400, "INVALID_REQUEST",
                               "evidence.evidence_id is required")
        if evidence.get("method") not in METHODS:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"evidence.method {evidence.get('method')!r} is invalid")
        if evidence.get("result") not in PROBE_RESULTS:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"evidence.result {evidence.get('result')!r} is invalid")
        if not isinstance(evidence.get("valid_until"), str):
            raise ServiceError(400, "INVALID_REQUEST",
                               "evidence.valid_until is required")
        now = self._now()
        # snapshot_id 落在这里，`transition()` 的「→ active 需要探测证据」才能
        # 只认这条快照自己的证据，而不是库里任意一条未过期探测
        self.conn.execute(
            "INSERT INTO capability_evidence (evidence_id, snapshot_id,"
            " combination_key, method, result, source_url, fetched_at, raw_ref,"
            " account_pool_id, reviewer, valid_until, created_at)"
            " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (evidence_id) DO UPDATE SET"
            " snapshot_id=excluded.snapshot_id,"
            " source_url=excluded.source_url, raw_ref=excluded.raw_ref,"
            " reviewer=excluded.reviewer, valid_until=excluded.valid_until",
            (evidence["evidence_id"], snapshot_id, evidence["method"],
             evidence["result"], evidence.get("source_url"),
             _ts(evidence.get("fetched_at") or now),
             evidence.get("raw_ref"), evidence.get("account_pool_id"),
             evidence.get("reviewer"), _ts(evidence["valid_until"]), now))

    def _write_rules(self, snapshot_id: str, mode: str, spec: dict) -> None:
        rules = {r["role"]: dict(r) for r in (spec.get("reference_rules") or [])}
        # §3.1 第 3 条：互斥必须对称。写 A 排 B 时补 B 排 A，否则判定会随
        # 请求顺序变化——查 A 看到互斥、查 B 看不到，同一个形状两个结论。
        for role, rule in list(rules.items()):
            for other in list(rule.get("exclusive_with") or []):
                if other not in rules:
                    rules[other] = {"role": other, "min_count": 0, "max_count": 0,
                                    "ordering": "unordered", "exclusive_with": []}
                peers = set(rules[other].get("exclusive_with") or [])
                peers.add(role)
                rules[other]["exclusive_with"] = sorted(peers)
        for role, rule in rules.items():
            self.conn.execute(
                "INSERT INTO mode_reference_rule (snapshot_id, mode, role, min_count,"
                " max_count, ordering, exclusive_with, evidence_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, mode, role, rule.get("min_count", 0),
                 rule.get("max_count", 0), rule.get("ordering", "unordered"),
                 jcs(sorted(set(rule.get("exclusive_with") or []))),
                 rule.get("evidence_id")))

    def _write_claims(self, snapshot_id: str, mode: str, spec: dict,
                      evidence_ids: set, rejected: list[dict]) -> None:
        for claim in spec.get("capability_claims") or []:
            support = claim["support"]
            evidence_id = claim.get("evidence_id")
            if support == "probe_verified" and evidence_id not in evidence_ids:
                rejected.append({
                    "mode": mode,
                    "capability_key": claim["capability_key"],
                    "reason": "EVIDENCE_MISSING_FOR_PROBE_VERIFIED",
                })
                support = "documented"
            self.conn.execute(
                "INSERT INTO capability_claim (snapshot_id, mode, capability_key,"
                " support, params, evidence_id) VALUES (?, ?, ?, ?, ?, ?)",
                (snapshot_id, mode, claim["capability_key"], support,
                 jcs(claim.get("params") or {}), evidence_id))

    def _write_output_specs(self, snapshot_id: str, mode: str, spec: dict) -> None:
        for out in spec.get("output_specs") or []:
            self.conn.execute(
                "INSERT INTO mode_output_spec (snapshot_id, mode, aspect_ratio,"
                " max_width, max_height, duration_tiers_s, trims_to_nearest)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, mode, out["aspect_ratio"], out.get("max_width"),
                 out.get("max_height"), jcs(sorted(out.get("duration_tiers_s") or [])),
                 1 if out.get("trims_to_nearest") else 0))

    def _write_input_limits(self, snapshot_id: str, mode: str, spec: dict) -> None:
        for limit in spec.get("input_limits") or []:
            self.conn.execute(
                "INSERT INTO mode_input_limit (snapshot_id, mode, media_kind,"
                " max_bytes, max_pixels, max_duration_s, allowed_mime)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, mode, limit["media_kind"], limit.get("max_bytes"),
                 limit.get("max_pixels"),
                 None if limit.get("max_duration_s") is None
                 else str(limit["max_duration_s"]),
                 jcs(sorted(limit.get("allowed_mime") or []))))

    def _shape_and_key(self, mode: str, shape: dict,
                       spec: dict | None) -> tuple[str, dict]:
        """把「请求形状」规范化成 combination_key。判定与探测**必须**走这一条。

        两条路各写一份规范化，写进去的 key 与查出来的 key 就会永远不相等，
        表现为「明明探测过却总是 UNVERIFIED」。这里的输入是可行性形状
        （`duration_seconds` / `resolution{width,height}`），`output_spec` 提供
        档位与最大像素，缺它就退化成按秒取整、按原分辨率取桶。
        """
        resolution = shape.get("resolution") or {}
        key, normalized = key_for(
            mode=mode,
            refs=shape.get("refs"),
            audio_strategy=shape.get("audio_strategy") or "silent",
            duration_seconds=shape.get("duration_seconds") or 0,
            aspect_ratio=shape.get("aspect_ratio") or "adaptive",
            width=resolution.get("width") or (spec or {}).get("max_width") or 1,
            height=resolution.get("height") or (spec or {}).get("max_height") or 1,
            lora=bool(shape.get("lora")),
            output_spec=spec,
        )
        return key, normalized

    def _write_probe(self, snapshot_id: str, probe: dict, *, now: str) -> str:
        """§2.4/§6.2：探测结论。乱序到达的旧结果不许覆盖新的（T24-15）。"""
        shape = probe.get("shape")
        if not isinstance(shape, dict):
            raise ServiceError(400, "INVALID_REQUEST",
                               "combination probe needs a shape")
        if probe.get("result") not in PROBE_RESULTS:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"probe result {probe.get('result')!r} is invalid")
        mode = probe.get("mode") or shape.get("mode")
        if not isinstance(mode, str) or not mode:
            raise ServiceError(400, "INVALID_REQUEST",
                               "combination probe needs a mode")
        evidence_id = probe.get("evidence_id")
        if not isinstance(evidence_id, str):
            raise ServiceError(400, "INVALID_REQUEST",
                               "combination probe needs evidence_id")
        if self.conn.execute(
                "SELECT 1 FROM capability_evidence WHERE evidence_id = ?",
                (evidence_id,)).fetchone() is None:
            # 证据可能在同一请求里、也可能先前登记；都没有就是悬空引用
            raise ServiceError(422, "EVIDENCE_MISSING",
                               f"evidence {evidence_id} does not exist")
        spec = self._output_spec(
            snapshot_id, mode, shape.get("aspect_ratio") or "adaptive")
        key, normalized = self._shape_and_key(mode, shape, spec)
        probed_at = _ts(probe.get("probed_at") or now)
        if not isinstance(probe.get("valid_until"), str):
            raise ServiceError(400, "INVALID_REQUEST",
                               "combination probe needs valid_until")
        valid_until = _ts(probe["valid_until"])
        if valid_until <= probed_at:
            raise ServiceError(400, "INVALID_REQUEST",
                               "probe valid_until must be after probed_at")
        self.conn.execute(
            "INSERT INTO mode_combination_probe (snapshot_id, mode, combination_key,"
            " shape, result, evidence_id, probed_at, valid_until)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (snapshot_id, mode, combination_key) DO UPDATE SET"
            " result=excluded.result, evidence_id=excluded.evidence_id,"
            " probed_at=excluded.probed_at, valid_until=excluded.valid_until,"
            " shape=excluded.shape"
            " WHERE mode_combination_probe.probed_at < excluded.probed_at",
            (snapshot_id, mode, key, jcs(normalized), probe["result"], evidence_id,
             probed_at, valid_until))
        return key

    def _close_previous(self, snap: dict, snapshot_id: str) -> None:
        """§3.1 第 4 条：新快照把旧快照 `effective_to` 置为新的 `effective_from`。

        旧快照**不删除**——历史 attempt 要能回溯到当时生效的那一版。
        """
        self.conn.execute(
            "UPDATE model_capability_snapshot SET effective_to = ?"
            " WHERE provider = ? AND channel = ? AND model_id = ?"
            " AND model_snapshot = ? AND region = ? AND account_pool_id = ?"
            " AND snapshot_id <> ? AND effective_to IS NULL"
            " AND effective_from <= ?",
            (_ts(snap["effective_from"]), snap["provider"], snap["channel"],
             snap["model_id"], snap["model_snapshot"], snap["region"],
             snap["account_pool_id"], snapshot_id, _ts(snap["effective_from"])))

    def _record_transition(self, snapshot_id: str, from_state, to_state, *,
                           reason: str, evidence_id, actor: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO lifecycle_transition (snapshot_id, from_state,"
            " to_state, reason, evidence_id, actor, occurred_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, from_state or "(new)", to_state, reason, evidence_id,
             actor, self._now()))

    def _snapshot_response(self, row: sqlite3.Row, *, replayed_content: bool) -> dict:
        return {
            "snapshot_id": row["snapshot_id"],
            "capability_bundle_revision": int(row["capability_bundle_rev"]),
            "lifecycle": row["lifecycle"],
            "accepted_modes": [m["mode"] for m in self._modes_of(row["snapshot_id"])],
            "replayed_content": replayed_content,
        }

    # -- 写：价格 -----------------------------------------------------------

    def _write_price(self, price: dict, snap: dict) -> str:
        if not isinstance(price, dict):
            raise ServiceError(400, "INVALID_REQUEST", "each price must be an object")
        units = price.get("billing_units")
        if not isinstance(units, dict) or not units:
            raise ServiceError(400, "INVALID_REQUEST", "price needs billing_units")
        for name, value in units.items():
            if not _int(value) or value < 0:
                raise ServiceError(
                    400, "INVALID_REQUEST",
                    f"billing_units.{name} must be a non-negative integer (micro-units)")
        if "request" not in units and "gpu_second" not in units:
            raise ServiceError(400, "INVALID_REQUEST",
                               "billing_units needs request or gpu_second (§2.5)")
        currency = price.get("currency")
        if not isinstance(currency, str) or len(currency) != 3:
            raise ServiceError(400, "INVALID_REQUEST",
                               "currency must be a 3-letter code")
        if price.get("tax_mode") not in ("inclusive", "exclusive"):
            raise ServiceError(400, "INVALID_REQUEST", "tax_mode is required")
        if price.get("failure_charge") not in ("none", "full", "partial", "per_unit"):
            raise ServiceError(400, "INVALID_REQUEST", "failure_charge is required")
        for key in ("valid_from", "valid_to", "source_ref", "source_hash"):
            if not isinstance(price.get(key), str) or not price[key]:
                raise ServiceError(400, "INVALID_REQUEST", f"price.{key} is required")
        valid_from = _ts(price["valid_from"])
        valid_to = _ts(price["valid_to"])
        if valid_from >= valid_to:
            raise ServiceError(400, "INVALID_REQUEST",
                               "valid_from must precede valid_to")
        for tier in price.get("tiers") or []:
            if tier.get("dimension") not in (
                    "duration_s", "resolution_mp", "duration_x_resolution"):
                raise ServiceError(400, "INVALID_REQUEST",
                                   f"price tier dimension {tier.get('dimension')!r}"
                                   " is invalid")
            if not _int(tier.get("unit_price_micro")) or tier["unit_price_micro"] < 0:
                raise ServiceError(400, "INVALID_REQUEST",
                                   "price tier unit_price_micro must be >= 0")

        tiers = sorted(
            ({"dimension": t["dimension"],
              "from_inclusive": str(t["from_inclusive"]),
              "to_exclusive": None if t.get("to_exclusive") is None
              else str(t["to_exclusive"]),
              "unit_price_micro": t["unit_price_micro"]}
             for t in (price.get("tiers") or [])),
            key=lambda t: (t["dimension"], t["from_inclusive"]))
        price_id = hashlib.sha256(jcs({
            "provider": snap["provider"], "channel": snap["channel"],
            "model_id": snap["model_id"], "model_snapshot": snap["model_snapshot"],
            "region": snap["region"], "currency": currency,
            "tax_mode": price["tax_mode"], "billing_units": units,
            "min_charge_micro": price.get("min_charge_micro", 0),
            "failure_charge": price["failure_charge"],
            "failure_charge_micro": price.get("failure_charge_micro", 0),
            "margin_bps": price.get("margin_bps", 1500),
            "valid_from": valid_from, "valid_to": valid_to,
            "source_ref": price["source_ref"], "source_hash": price["source_hash"],
            "tiers": tiers,
        }).encode()).hexdigest()

        if self.conn.execute(
                "SELECT 1 FROM price_snapshot WHERE price_snapshot_id = ?",
                (price_id,)).fetchone() is not None:
            return price_id
        self.conn.execute(
            "INSERT INTO price_snapshot (price_snapshot_id, tenant_id, provider,"
            " channel, model_id, model_snapshot, region, currency, tax_mode,"
            " billing_units, min_charge_micro, failure_charge, failure_charge_micro,"
            " quotable, margin_bps, valid_from, valid_to, source_ref, source_hash,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?)",
            (price_id, snap.get("tenant_id", "*"), snap["provider"], snap["channel"],
             snap["model_id"], snap["model_snapshot"], snap["region"], currency,
             price["tax_mode"], jcs(units), price.get("min_charge_micro", 0),
             price["failure_charge"], price.get("failure_charge_micro", 0),
             1 if price.get("quotable", True) else 0, price.get("margin_bps", 1500),
             valid_from, valid_to, price["source_ref"], price["source_hash"],
             self._now()))
        for tier in tiers:
            self.conn.execute(
                "INSERT INTO price_tier (price_snapshot_id, dimension, from_inclusive,"
                " to_exclusive, unit_price_micro) VALUES (?, ?, ?, ?, ?)",
                (price_id, tier["dimension"], tier["from_inclusive"],
                 tier["to_exclusive"], tier["unit_price_micro"]))
        return price_id

    # -- 读：报价上界 -------------------------------------------------------

    def price_snapshot(self, tenant_id: str, price_snapshot_id: str) -> dict:
        row = self._price_row(price_snapshot_id)
        self._assert_price_visible(row, tenant_id)
        tiers = self.conn.execute(
            "SELECT * FROM price_tier WHERE price_snapshot_id = ?"
            " ORDER BY dimension, from_inclusive", (price_snapshot_id,)).fetchall()
        bound, missing = self._upper_bound(row, tiers, aspect_ratio=None)
        return {
            "price_snapshot_id": row["price_snapshot_id"],
            "currency": row["currency"],
            "tax_mode": row["tax_mode"],
            "billing_units": _loads(row["billing_units"], {}),
            "min_charge_micro": int(row["min_charge_micro"]),
            "failure_charge": row["failure_charge"],
            "failure_charge_micro": int(row["failure_charge_micro"]),
            "margin_bps": int(row["margin_bps"]),
            "quotable": bool(row["quotable"]),
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "expired": row["valid_to"] <= self._now(),
            "quote_upper_bound_micro": bound,
            "unbounded_units": missing,
            "tiers": [dict(t) for t in tiers],
        }

    def _assert_price_visible(self, row: sqlite3.Row, tenant_id: str) -> None:
        if row["tenant_id"] not in ("*", tenant_id):
            raise ServiceError(403, "CROSS_TENANT_DENIED",
                               "price snapshot belongs to another tenant")

    def _upper_bound(self, price: sqlite3.Row, tiers, *,
                     aspect_ratio: str | None) -> tuple[int | None, list[str]]:
        """§2.5 的公式：

            raw = Σ_units(worst_case_units × unit_price) + min_charge + failure_reserve
            upper = ceil_to_currency_minor(raw × (1 + margin_bps/10000))

        `worst_case_units` 必须由**快照自身**给出（时长取档位最大值、像素取
        max_width×max_height），不能由调用方传入——否则调用方压低上界就能把贵
        任务塞进预算。拿不到上界的单位进 `unbounded_units`，此时**不给数**：
        返回 None 而不是猜一个。
        """
        if not price["quotable"]:
            return None, ["quotable=false"]
        units = _loads(price["billing_units"], {})
        spec = self._worst_case_spec(price, aspect_ratio)
        if spec is None:
            return None, ["(no output spec on the effective snapshot)"]
        durations = spec["duration_tiers_s"]
        max_seconds = max(durations) if durations else None
        pixels = (spec["max_width"] or 0) * (spec["max_height"] or 0) or None

        total = Decimal(0)
        missing: list[str] = []
        for name, unit_price in units.items():
            if unit_price == 0:
                continue
            count = self._worst_case_units(name, max_seconds, pixels)
            if count is None:
                missing.append(name)
                continue
            total += Decimal(count) * Decimal(
                self._tier_price(tiers, count, unit_price))
        if missing:
            return None, sorted(missing)
        total += Decimal(int(price["min_charge_micro"]))
        total += self._failure_reserve(price, total)
        margin = Decimal(1) + Decimal(int(price["margin_bps"])) / Decimal(10000)
        return _ceil_minor(total * margin, price["currency"]), []

    def _worst_case_spec(self, price: sqlite3.Row, aspect_ratio) -> dict | None:
        """价格不是按模式定价的（§2.5 只到 model/region 粒度），所以「最坏情况」
        必须横跨该模型当前生效快照的**全部**输出规格取上界：最大像素 × 最长档位。
        只看一条规格会低估——恰好选中没有时长档位的模式就会给出一个假的宽裕上界。
        """
        snap = self.conn.execute(
            "SELECT * FROM model_capability_snapshot s"
            " WHERE s.provider = ? AND s.channel = ? AND s.model_id = ?"
            " AND s.model_snapshot = ? AND s.region = ?"
            " AND s.effective_to IS NULL"
            " ORDER BY s.capability_bundle_rev DESC LIMIT 1",
            (price["provider"], price["channel"], price["model_id"],
             price["model_snapshot"], price["region"])).fetchone()
        if snap is None:
            return None
        params: list = [snap["snapshot_id"]]
        clause = ""
        if aspect_ratio:
            clause = " AND aspect_ratio = ?"
            params.append(aspect_ratio)
        specs = self.conn.execute(
            "SELECT * FROM mode_output_spec WHERE snapshot_id = ?" + clause, params
        ).fetchall()
        if not specs:
            return None
        tiers: set[int] = set()
        width = height = 0
        for spec in specs:
            tiers.update(int(t) for t in _loads(spec["duration_tiers_s"], []))
            width = max(width, spec["max_width"] or 0)
            height = max(height, spec["max_height"] or 0)
        return {
            "max_width": width or None,
            "max_height": height or None,
            "duration_tiers_s": sorted(tiers),
        }

    @staticmethod
    def _tier_price(tiers, count: int, base: int) -> int:
        """按档位覆盖单价。

        档位覆盖的是「整笔按该档」还是「超出部分按该档」，§2.5 没写（C24-8）。
        这里取「包含该计数量的那一档」作为整笔单价；没有恰好覆盖的档就取最高档
        ——报价上界只许偏高。原实现无条件取最后一档，多个档位时上界会算错。
        """
        duration_tiers = [t for t in tiers if t["dimension"] == "duration_s"]
        if not duration_tiers:
            return base
        chosen = None
        for tier in duration_tiers:
            low = Decimal(tier["from_inclusive"])
            high = (None if tier["to_exclusive"] is None
                    else Decimal(tier["to_exclusive"]))
            if low <= count and (high is None or count < high):
                chosen = tier
        if chosen is None:
            chosen = max(duration_tiers, key=lambda t: Decimal(t["from_inclusive"]))
        return int(chosen["unit_price_micro"])

    @staticmethod
    def _worst_case_units(name: str, max_seconds, pixels) -> int | None:
        if name == "request":
            return 1
        if name in ("generated_second", "gpu_second"):
            return max_seconds
        if name == "output_megapixel":
            if not pixels:
                return None
            return int(math.ceil(pixels / 1_000_000))
        # input_token / output_token / character 由提示词与文本长度决定，快照里
        # 没有任何字段能给出上界。不猜。
        return None

    @staticmethod
    def _failure_reserve(price: sqlite3.Row, total: Decimal) -> Decimal:
        mode = price["failure_charge"]
        if mode == "none":
            return Decimal(0)
        if mode == "full":
            return total
        # partial / per_unit 的具体金额由数据给出；数据是 0 说明没填，这时按 full
        # 保守兜底——低估失败成本会让准入放行一笔实际更贵的任务。
        declared = int(price["failure_charge_micro"])
        return Decimal(declared) if declared else total

    # -- 读：候选 -----------------------------------------------------------

    def candidates(self, tenant_id: str, params: dict) -> dict:
        """§3.2 `GET /model-candidates`。空 candidates + 非空 excluded 是合法响应。

        每一行要么进 `candidates`，要么进 `excluded` 并带上唯一原因码——S25 的
        「候选拒绝原因可解释」直接依赖后者。任何一个分支返回 `None` 都会让一个
        不该用的候选静默通过，所以下面每个 if 都返回码。
        """
        projection = self._authorization_projection(tenant_id)
        now = self._now()
        required = [c.strip() for c in
                    (params.get("required_capabilities") or "").split(",") if c.strip()]
        mode = params.get("mode")
        aspect_ratio = params.get("aspect_ratio")
        allow_byok = str(params.get("allow_byok", "true")).lower() != "false"
        home_region = params.get("home_region") or projection["home_region"]
        residency = [r.strip() for r in
                     (params.get("residency") or "").split(",") if r.strip()]

        rows = self.conn.execute(
            "SELECT s.*, p.scope AS pool_scope, p.health AS pool_health,"
            " p.enabled AS pool_enabled, p.max_inflight AS pool_max_inflight,"
            " p.rpm AS pool_rpm, p.tenant_id AS pool_tenant"
            " FROM model_capability_snapshot s"
            " JOIN provider_account_pool p ON p.pool_id = s.account_pool_id"
            " WHERE s.effective_to IS NULL"
            " ORDER BY s.capability_bundle_rev DESC").fetchall()

        candidates: list[dict] = []
        excluded: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            key = self._candidate_key(row)
            if key in seen:
                continue
            seen.add(key)
            code, detail = self._exclude_reason(
                row, tenant_id, now, required, mode, allow_byok, residency)
            if code is not None:
                excluded.append({"candidate_key": key, "code": code,
                                 "detail": detail})
                continue
            payload, code, detail = self._candidate_payload(
                row, tenant_id, now, aspect_ratio)
            if code is not None:
                excluded.append({"candidate_key": key, "code": code,
                                 "detail": detail})
                continue
            candidates.append(payload)
        return {
            "capability_bundle_revision": self.bundle_revision(),
            "projection_watermark": projection["resolved_at"],
            "candidates": candidates,
            "excluded": excluded,
        }

    @staticmethod
    def _candidate_key(row) -> str:
        return (f"{row['provider']}/{row['channel']}/{row['model_id']}"
                f"@{row['model_snapshot']}#{row['region']}#{row['account_pool_id']}")

    def _exclude_reason(self, row, tenant_id: str, now: str, required: list[str],
                        mode, allow_byok: bool,
                        residency: list[str]) -> tuple[str | None, str]:
        """顺序照 S25 §3 的硬过滤表：租户 → 生命周期 → 账户 → 驻留 → 证据 →
        模式 → 能力。第一个失败的成为唯一原因码。"""
        # BYOK 硬规则：别的企业的自有凭证不能出现在这台租户的候选集里
        if row["pool_scope"] == "tenant_byok":
            if row["pool_tenant"] != tenant_id:
                return "CROSS_TENANT_DENIED", (
                    f"byok pool belongs to {row['pool_tenant']}")
            if not allow_byok:
                return "BYOK_MISMATCH", "byok pools are excluded for this request"
        if row["lifecycle"] == "retired":
            return "PROVIDER_RETIRED", f"lifecycle {row['lifecycle']}"
        if row["lifecycle"] == "disabled":
            return "PROVIDER_DISABLED", f"lifecycle {row['lifecycle']}"
        if row["lifecycle"] == "validating":
            # S25 的码表里没有 validating，但这个状态的候选绝不能派单
            return "PROVIDER_VALIDATING", f"lifecycle {row['lifecycle']}"
        if row["lifecycle"] == "degraded":
            # 默认硬过滤；是否放行属于 S02 策略（§4.4），不在这里放宽
            return "PROVIDER_DEGRADED", f"lifecycle {row['lifecycle']}"
        if row["pool_health"] in ("unauthorized", "exhausted", "disabled"):
            return "ACCOUNT_UNAVAILABLE", f"pool health {row['pool_health']}"
        if not row["pool_enabled"]:
            return "ACCOUNT_UNAVAILABLE", "pool is disabled"
        tags = set(_loads(row["residency_tags"], []))
        if residency and not set(residency).issubset(tags):
            return "RESIDENCY_DENIED", f"residency tags {sorted(tags)}"
        if row["evidence_valid_until"] is None or row["evidence_valid_until"] <= now:
            return "EVIDENCE_EXPIRED", f"valid_until {row['evidence_valid_until']}"
        if mode is not None:
            supported = self.conn.execute(
                "SELECT support FROM capability_mode WHERE snapshot_id = ?"
                " AND mode = ?", (row["snapshot_id"], mode)).fetchone()
            if supported is None:
                return "MODE_UNSUPPORTED", f"no mode {mode!r}"
            if supported["support"] in ("unsupported", "field_failed"):
                return "MODE_UNSUPPORTED", (
                    f"mode {mode!r} is {supported['support']}")
        for capability in required:
            claim = self.conn.execute(
                "SELECT support FROM capability_claim WHERE snapshot_id = ?"
                " AND capability_key = ?",
                (row["snapshot_id"], capability)).fetchone()
            if claim is None or claim["support"] in ("unsupported", "field_failed"):
                return (f"CAPABILITY_MISSING:{capability}",
                        "not documented as supported")
        return None, ""

    def _candidate_payload(self, row, tenant_id: str, now: str,
                           aspect_ratio) -> tuple[dict | None, str | None, str]:
        """价格与上界在同一次读取里算，避免「候选在、价不在」。

        `url_ttl_seconds` / `job_ttl_seconds` / `cancel_supported` /
        `callback_scheme` / `provider_idempotency` / `execution_kind` 在 §3.2 的
        示例里出现，但 §2 没有任何一张表给出它们（C24-5）。这里**不填**：
        `provider_idempotency` 尤其不能从文档推断（§10：填错就是重复扣费）。
        """
        price = self._current_price(row, tenant_id)
        payload = {
            "candidate_key": self._candidate_key(row),
            "snapshot_id": row["snapshot_id"],
            "provider": row["provider"], "channel": row["channel"],
            "model_id": row["model_id"], "model_snapshot": row["model_snapshot"],
            "region": row["region"], "account_pool_id": row["account_pool_id"],
            "scope": row["pool_scope"], "data_region": row["data_region"],
            "residency_tags": _loads(row["residency_tags"], []),
            "lifecycle": row["lifecycle"],
            "evidence_valid_until": row["evidence_valid_until"],
            "modes": [m["mode"] for m in self._modes_of(row["snapshot_id"])],
            "max_inflight": int(row["pool_max_inflight"]),
            "rpm": int(row["pool_rpm"]),
        }
        if price is None:
            return None, "PRICE_EXPIRED", "no price snapshot for this candidate"
        payload.update(price_snapshot_id=price["price_snapshot_id"],
                       price_valid_to=price["valid_to"],
                       currency=price["currency"])
        if price["valid_to"] <= now:
            return None, "PRICE_EXPIRED", f"valid_to {price['valid_to']}"
        tiers = self.conn.execute(
            "SELECT * FROM price_tier WHERE price_snapshot_id = ?",
            (price["price_snapshot_id"],)).fetchall()
        bound, missing = self._upper_bound(price, tiers, aspect_ratio=aspect_ratio)
        payload["quote_upper_bound_micro"] = bound
        if bound is None:
            # docs/12：无可验证上界的任务不能以 0 元估计准入
            return None, "NO_VERIFIABLE_UPPER_BOUND", f"unbounded units {missing}"
        return payload, None, ""

    def _current_price(self, snap, tenant_id: str):
        return self.conn.execute(
            "SELECT * FROM price_snapshot WHERE provider = ? AND channel = ?"
            " AND model_id = ? AND model_snapshot = ? AND region = ?"
            " AND tenant_id IN ('*', ?)"
            " ORDER BY valid_from DESC LIMIT 1",
            (snap["provider"], snap["channel"], snap["model_id"],
             snap["model_snapshot"], snap["region"], tenant_id)).fetchone()

    # -- 读：能力判定 -------------------------------------------------------

    def check_capability(self, tenant_id: str, body: dict) -> dict:
        """§3.3 `POST /capability-checks`。

        `UNVERIFIED` **不等于可用**：未验证的组合正是最容易「提交成功但成片不能
        用」的地方。只有存在未过期的 `supported` 探测行才给 `SUPPORTED`。
        """
        snapshot_id = body.get("snapshot_id")
        if not isinstance(snapshot_id, str):
            raise ServiceError(400, "INVALID_REQUEST", "snapshot_id is required")
        row = self._snapshot_row(snapshot_id)
        self._assert_snapshot_visible(row, tenant_id)

        mode = body.get("mode")
        if not isinstance(mode, str):
            raise ServiceError(400, "INVALID_REQUEST", "mode is required")
        mode_row = self.conn.execute(
            "SELECT * FROM capability_mode WHERE snapshot_id = ? AND mode = ?",
            (snapshot_id, mode)).fetchone()
        if mode_row is None:
            raise ServiceError(404, "MODE_NOT_FOUND",
                               f"snapshot has no mode {mode!r}")

        now = self._now()
        checks: list[dict] = []
        reasons: list[dict] = []
        aspect_ratio = body.get("aspect_ratio") or "adaptive"
        spec = self._output_spec(snapshot_id, mode, aspect_ratio)
        if spec is None:
            checks.append({"rule": "output.aspect_ratio", "allowed": [],
                           "requested": aspect_ratio, "ok": False})
            reasons.append({"code": "OUTPUT_ASPECT_RATIO_UNSUPPORTED",
                            "aspect_ratio": aspect_ratio})
            return self._verdict("UNSUPPORTED", None, None, checks, reasons, None)

        refs = normalize_refs(body.get("refs"))
        refs_ok = self._check_reference_rules(snapshot_id, mode, refs,
                                              checks, reasons)
        duration_ok, snap_to = self._check_duration(spec, body, checks, reasons)

        # 用**吸附后**的时长算 key：§3.3 要求 4s 请求在 trims_to_nearest 下按
        # 5s 的档位判定，键也必须落在 5s 上，否则与 5s 的探测行对不上。
        key, normalized = self._shape_and_key(mode, {
            "refs": refs,
            "audio_strategy": body.get("audio_strategy") or "silent",
            "duration_seconds": (snap_to if snap_to is not None
                                 else body.get("duration_seconds") or 0),
            "aspect_ratio": aspect_ratio,
            "resolution": body.get("resolution"),
            "lora": body.get("lora"),
        }, spec)

        if not refs_ok or not duration_ok:
            return self._verdict("UNSUPPORTED", key, normalized, checks, reasons, None)

        if mode_row["support"] in ("unsupported", "field_failed"):
            checks.append({"rule": "mode.support",
                           "support": mode_row["support"], "ok": False})
            reasons.append({"code": "MODE_UNSUPPORTED", "mode": mode})
            return self._verdict("UNSUPPORTED", key, normalized, checks, reasons,
                                 None)

        probe = self._probe(snapshot_id, mode, key)
        if probe is None:
            checks.append({"rule": "combination.probe", "ok": False,
                           "note": "no probe for this shape"})
            reasons.append({"code": "COMBINATION_UNVERIFIED", "mode": mode})
            return self._verdict("UNVERIFIED", key, normalized, checks, reasons, None)
        if probe["valid_until"] <= now:
            checks.append({"rule": "combination.probe", "ok": False,
                           "probed_at": probe["probed_at"],
                           "valid_until": probe["valid_until"],
                           "note": "probe expired"})
            reasons.append({"code": "COMBINATION_UNVERIFIED", "mode": mode,
                            "detail": "probe expired"})
            return self._verdict("UNVERIFIED", key, normalized, checks, reasons,
                                 probe["evidence_id"])
        checks.append({"rule": "combination.probe",
                       "ok": probe["result"] == "supported",
                       "probed_at": probe["probed_at"],
                       "valid_until": probe["valid_until"]})
        if probe["result"] == "unsupported":
            reasons.append({"code": "COMBINATION_UNSUPPORTED", "mode": mode})
            return self._verdict("UNSUPPORTED", key, normalized, checks, reasons,
                                 probe["evidence_id"])
        if probe["result"] == "inconclusive":
            reasons.append({"code": "COMBINATION_UNVERIFIED", "mode": mode,
                            "detail": "probe was inconclusive"})
            return self._verdict("UNVERIFIED", key, normalized, checks, reasons,
                                 probe["evidence_id"])

        for capability in (body.get("required_capabilities") or []):
            claim = self.conn.execute(
                "SELECT * FROM capability_claim WHERE snapshot_id = ? AND mode = ?"
                " AND capability_key = ?",
                (snapshot_id, mode, capability)).fetchone()
            ok = claim is not None and claim["support"] in (
                "documented", "probe_verified")
            checks.append({"rule": "capability.required",
                           "capability_key": capability,
                           "support": None if claim is None else claim["support"],
                           "ok": ok})
            if not ok:
                reasons.append({"code": f"CAPABILITY_MISSING:{capability}",
                                "mode": mode})
        if reasons:
            return self._verdict("UNSUPPORTED", key, normalized, checks, reasons,
                                 probe["evidence_id"])
        return self._verdict("SUPPORTED", key, normalized, checks, reasons,
                             probe["evidence_id"])

    @staticmethod
    def _verdict(verdict: str, key, shape, checks, reasons, evidence_id) -> dict:
        """§3.3 的响应体，字段与方案逐字一致。不额外加字段：
        `priced_duration_seconds` 之类若真需要，先改契约再加。"""
        assert verdict in ("SUPPORTED", "UNSUPPORTED", "UNVERIFIED"), verdict
        return {"verdict": verdict, "combination_key": key,
                "normalized_shape": shape, "checks": checks,
                "reasons": reasons, "evidence_id": evidence_id}

    def _assert_snapshot_visible(self, row, tenant_id: str) -> None:
        """跨租户 → 404（不确认存在）。

        S24 的快照本身是共享目录（`tenant_id='*'`）；BYOK 越权发生在池这一层，
        由 `candidates` 与 `validity` 判 `CROSS_TENANT_DENIED`。
        """
        if row["tenant_id"] in ("*", tenant_id):
            return
        raise ServiceError(404, "SNAPSHOT_NOT_FOUND",
                           f"snapshot {row['snapshot_id']} does not exist")

    def _check_reference_rules(self, snapshot_id: str, mode: str, refs,
                               checks: list, reasons: list) -> bool:
        ok = True
        counts = {r["role"]: r["count"] for r in refs}
        rules = self.conn.execute(
            "SELECT * FROM mode_reference_rule WHERE snapshot_id = ? AND mode = ?",
            (snapshot_id, mode)).fetchall()
        declared = {r["role"] for r in rules}
        for role in sorted(counts):
            if role not in declared:
                checks.append({"rule": "reference.role", "role": role, "ok": False})
                reasons.append({"code": f"REFERENCE_ROLE_UNSUPPORTED:{role}"})
                ok = False
        for rule in rules:
            actual = counts.get(rule["role"], 0)
            within = rule["min_count"] <= actual <= rule["max_count"]
            if actual or not within:
                checks.append({"rule": "reference.max_count", "role": rule["role"],
                               "min_count": rule["min_count"],
                               "limit": rule["max_count"], "actual": actual,
                               "ok": within})
            if not within:
                reasons.append({"code": "REFERENCE_COUNT_OUT_OF_RANGE",
                                "role": rule["role"], "actual": actual,
                                "min_count": rule["min_count"],
                                "max_count": rule["max_count"]})
                ok = False
            for peer in _loads(rule["exclusive_with"], []):
                if actual and counts.get(peer):
                    checks.append({"rule": "reference.exclusive",
                                   "role": rule["role"],
                                   "exclusive_with": peer, "ok": False})
                    reasons.append({"code": "REFERENCE_EXCLUSIVE_CONFLICT",
                                    "roles": [rule["role"], peer]})
                    ok = False
        return ok

    def _check_duration(self, spec: dict, body: dict, checks: list,
                        reasons: list) -> tuple[bool, float | None]:
        requested = body.get("duration_seconds")
        if requested is None:
            return True, None
        if not (isinstance(requested, (int, float))
                and not isinstance(requested, bool) and requested > 0):
            raise ServiceError(400, "INVALID_REQUEST",
                               "duration_seconds must be a positive number")
        tiers = sorted(spec["duration_tiers_s"])
        if not tiers:
            return True, float(requested)
        snapped = next((t for t in tiers if requested <= t), None)
        if snapped is None:
            checks.append({"rule": "output.duration_tier", "allowed": tiers,
                           "requested": requested, "ok": False})
            reasons.append({"code": "OUTPUT_DURATION_UNSUPPORTED",
                            "allowed": tiers, "requested": requested})
            return False, None
        if snapped != requested and not spec["trims_to_nearest"]:
            checks.append({"rule": "output.duration_tier", "allowed": tiers,
                           "requested": requested, "ok": False,
                           "note": "trims_to_nearest is false"})
            reasons.append({"code": "OUTPUT_DURATION_UNSUPPORTED",
                            "allowed": tiers, "requested": requested})
            return False, None
        check = {"rule": "output.duration_tier", "allowed": tiers,
                 "requested": requested, "ok": True}
        if snapped != requested:
            check["note"] = f"snap_to={snapped}s"
        checks.append(check)
        return True, float(snapped)

    # -- 读：批量有效性（S26 用） -------------------------------------------

    def validity(self, tenant_id: str, body: dict) -> dict:
        """§5.3：只回答「现在还能不能用」，不预留任何东西。

        S26 必须在**签发 dispatch grant 前**调它。撤销、kill switch、凭证失效、
        价格过期都可能发生在路由之后。
        """
        items = body.get("items")
        if not isinstance(items, list):
            raise ServiceError(400, "INVALID_REQUEST", "items must be a list")
        if len(items) > 200:
            raise ServiceError(400, "INVALID_REQUEST", "at most 200 items per call")
        now = self._now()
        return {"items": [self._validity_one(tenant_id, item, now)
                          for item in items]}

    def _validity_one(self, tenant_id: str, item: dict, now: str) -> dict:
        snapshot_id = item.get("snapshot_id")
        row = self.conn.execute(
            "SELECT * FROM model_capability_snapshot WHERE snapshot_id = ?",
            (snapshot_id,)).fetchone()
        if row is None:
            return {"snapshot_id": snapshot_id, "usable": False,
                    "code": "SNAPSHOT_NOT_FOUND"}
        if row["tenant_id"] not in ("*", tenant_id):
            return {"snapshot_id": snapshot_id, "usable": False,
                    "code": "CROSS_TENANT_DENIED"}
        pool = self.conn.execute(
            "SELECT * FROM provider_account_pool WHERE pool_id = ?",
            (row["account_pool_id"],)).fetchone()
        result = {
            "snapshot_id": snapshot_id,
            "usable": True,
            "code": None,
            "lifecycle": row["lifecycle"],
            "pool_health": None if pool is None else pool["health"],
            "evidence_valid_until": row["evidence_valid_until"],
            "price_valid_to": None,
            "combination": None,
        }
        if row["lifecycle"] != "active":
            result.update(usable=False, code="NOT_ACTIVE")
        if row["evidence_valid_until"] is None or row["evidence_valid_until"] <= now:
            result.update(usable=False, code="EVIDENCE_EXPIRED")
        if (pool is None or not pool["enabled"]
                or pool["health"] in ("unauthorized", "exhausted", "disabled")):
            result.update(usable=False, code="ACCOUNT_UNAVAILABLE")
        if (pool is not None and pool["scope"] == "tenant_byok"
                and pool["tenant_id"] != tenant_id):
            result.update(usable=False, code="CROSS_TENANT_DENIED")
        price_id = item.get("price_snapshot_id")
        if price_id:
            price = self.conn.execute(
                "SELECT * FROM price_snapshot WHERE price_snapshot_id = ?",
                (price_id,)).fetchone()
            if price is None:
                result.update(usable=False, code="PRICE_EXPIRED")
            else:
                result["price_valid_to"] = price["valid_to"]
                if price["valid_to"] <= now:
                    result.update(usable=False, code="PRICE_EXPIRED")
                if not price["quotable"]:
                    result.update(usable=False, code="NO_VERIFIABLE_UPPER_BOUND")
        key = item.get("combination_key")
        if key:
            probe = self._probe(snapshot_id, item.get("mode") or "", key)
            if probe is None or probe["valid_until"] <= now:
                verdict = "UNVERIFIED"
            elif probe["result"] == "supported":
                verdict = "SUPPORTED"
            else:
                verdict = "UNSUPPORTED"
            result["combination"] = verdict
            if verdict != "SUPPORTED":
                result.update(usable=False, code="COMBINATION_NOT_SUPPORTED")
        return result

    # -- 生命周期 -----------------------------------------------------------

    def transition(self, tenant_id: str, body: dict) -> dict:
        """§4.1 迁移。守卫写在这里，不写在文档里。

        `retired` 没有出边；→`active` 必须有**这条快照自己的**未过期探测证据；
        →`retired` 必须有 `doc_read` 证据与归档引用。
        """
        snapshot_id = body.get("snapshot_id")
        row = self._snapshot_row(snapshot_id)
        self._assert_snapshot_visible(row, tenant_id)
        to_state = body.get("to_state")
        if to_state not in LIFECYCLES:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"invalid to_state {to_state!r}")
        from_state = row["lifecycle"]
        if to_state == from_state:
            raise ServiceError(409, "NO_STATE_CHANGE",
                               f"snapshot is already {from_state}")
        if to_state not in LIFECYCLE_EDGES[from_state]:
            raise ServiceError(422, "INVALID_TRANSITION",
                               f"{from_state} -> {to_state} is not a legal transition")
        evidence_id = body.get("evidence_id")
        if to_state == "active":
            # 必须限定在**这条快照**上：不加 snapshot_id 的话，库里任意一条
            # 未过期探测都能给任意快照解锁 active——那是把证据权威让出去
            good = self.conn.execute(
                "SELECT 1 FROM capability_evidence WHERE snapshot_id = ?"
                " AND method IN ('account_probe','paid_probe')"
                " AND result = 'supported' AND valid_until > ?",
                (snapshot_id, self._now())).fetchone()
            if good is None:
                raise ServiceError(
                    422, "ACTIVE_REQUIRES_PROBE_EVIDENCE",
                    "this snapshot has no unexpired account_probe/paid_probe"
                    " evidence with result=supported")
        if to_state == "retired":
            official = None
            if evidence_id:
                official = self.conn.execute(
                    "SELECT * FROM capability_evidence WHERE evidence_id = ?"
                    " AND snapshot_id = ? AND method = 'doc_read'",
                    (evidence_id, snapshot_id)).fetchone()
            if official is None or not official["raw_ref"]:
                raise ServiceError(
                    422, "RETIRE_REQUIRES_OFFICIAL_EVIDENCE",
                    "retiring needs a doc_read evidence of this snapshot"
                    " with an archived raw_ref")
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE model_capability_snapshot SET lifecycle = ?,"
                " lifecycle_reason = ? WHERE snapshot_id = ?",
                (to_state, body.get("reason"), snapshot_id))
            self._record_transition(snapshot_id, from_state, to_state,
                                    reason=body.get("reason") or "",
                                    evidence_id=evidence_id,
                                    actor=body.get("actor") or "s24.api")
            updated = self._snapshot_row(snapshot_id)
            self._emit(updated, change_kind="lifecycle_changed",
                       changed_modes=[m["mode"] for m in
                                      self._modes_of(snapshot_id)])
        return {"snapshot_id": snapshot_id, "from_state": from_state,
                "to_state": to_state, "lifecycle": to_state}

    def expire_evidence(self, now: str | None = None) -> list[dict]:
        """§4.2 过期扫描。**没有**调度器——这是一个可调用方法，由将来的 worker
        每 15 分钟调一次。`lifecycle_transition` 的主键挡住同一迁移重复记录，
        但事件仍可能重发；重复消费由下游 `inbox` 按 `event_id` 去重。"""
        stamp = _ts(now) if now else self._now()
        rows = self.conn.execute(
            "SELECT * FROM model_capability_snapshot WHERE lifecycle = 'active'"
            " AND (evidence_valid_until IS NULL OR evidence_valid_until <= ?)",
            (stamp,)).fetchall()
        degraded = []
        for row in rows:
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE model_capability_snapshot SET lifecycle = 'degraded',"
                    " lifecycle_reason = 'EVIDENCE_EXPIRED' WHERE snapshot_id = ?",
                    (row["snapshot_id"],))
                self._record_transition(row["snapshot_id"], "active", "degraded",
                                        reason="EVIDENCE_EXPIRED",
                                        evidence_id=None,
                                        actor="s24.expiry-scan")
                updated = self._snapshot_row(row["snapshot_id"])
                self._emit(updated, change_kind="lifecycle_changed",
                           changed_modes=[m["mode"] for m in
                                          self._modes_of(row["snapshot_id"])])
            degraded.append({"snapshot_id": row["snapshot_id"],
                             "code": "EVIDENCE_EXPIRED"})
        return degraded

    # -- 事件 ---------------------------------------------------------------

    @staticmethod
    def _aggregate_id(snap) -> str:
        return (f"{snap['provider']}/{snap['channel']}/{snap['model_id']}"
                f"/{snap['model_snapshot']}")

    def _emit(self, snap, *, change_kind: str, changed_modes: list[str]) -> str:
        """§3.5 的事件。`aggregate_sequence = capability_bundle_revision`。

        payload 必须能过已发布的事件 schema（`additionalProperties: false`），
        所以不存在于其中的字段一个都不加。信封差异（SDK 发 `sequence`、契约要
        `aggregate_sequence`）是平台级问题 C09-9，不在本服务内规避。
        """
        aggregate = self._aggregate_id(snap)
        data = {
            "schema_version": "2.0",
            "snapshot_id": snap["snapshot_id"],
            "capability_bundle_revision": int(snap["capability_bundle_rev"]),
            "change_kind": change_kind,
            "lifecycle": snap["lifecycle"],
            "effective_from": snap["effective_from"],
            "changed_modes": sorted(set(changed_modes)),
            "evidence_valid_until": snap["evidence_valid_until"],
        }
        envelope = new_event(
            source=SOURCE, type=EVENT_TYPE, subject=aggregate,
            tenant_id=snap["tenant_id"] or "*", home_cell="global",
            aggregate_id=aggregate,
            sequence=int(snap["capability_bundle_rev"]),
            correlation_id=snap["snapshot_id"], causation_id=snap["snapshot_id"],
            data=data,
        )
        return self.outbox.append(envelope)

    # -- 查询 ---------------------------------------------------------------

    def snapshot(self, tenant_id: str, snapshot_id: str) -> dict:
        row = self._snapshot_row(snapshot_id)
        self._assert_snapshot_visible(row, tenant_id)
        payload = self._snapshot_response(row, replayed_content=False)
        payload.update({
            "provider": row["provider"], "channel": row["channel"],
            "model_id": row["model_id"], "model_snapshot": row["model_snapshot"],
            "region": row["region"], "account_pool_id": row["account_pool_id"],
            "data_region": row["data_region"],
            "residency_tags": _loads(row["residency_tags"], []),
            "evidence_state": row["evidence_state"],
            "evidence_valid_until": row["evidence_valid_until"],
            "effective_from": row["effective_from"],
            "effective_to": row["effective_to"],
            "mode_details": [self._mode_detail(snapshot_id, m)
                             for m in self._modes_of(snapshot_id)],
        })
        return payload

    def _mode_detail(self, snapshot_id: str, mode: dict) -> dict:
        rules = [{"role": r["role"], "min_count": r["min_count"],
                  "max_count": r["max_count"], "ordering": r["ordering"],
                  "exclusive_with": _loads(r["exclusive_with"], [])}
                 for r in self.conn.execute(
                     "SELECT * FROM mode_reference_rule WHERE snapshot_id = ?"
                     " AND mode = ? ORDER BY role",
                     (snapshot_id, mode["mode"])).fetchall()]
        specs = [{"aspect_ratio": o["aspect_ratio"], "max_width": o["max_width"],
                  "max_height": o["max_height"],
                  "duration_tiers_s": _loads(o["duration_tiers_s"], []),
                  "trims_to_nearest": bool(o["trims_to_nearest"])}
                 for o in self.conn.execute(
                     "SELECT * FROM mode_output_spec WHERE snapshot_id = ?"
                     " AND mode = ? ORDER BY aspect_ratio",
                     (snapshot_id, mode["mode"])).fetchall()]
        claims = [{"capability_key": c["capability_key"], "support": c["support"],
                   "params": _loads(c["params"], {}),
                   "evidence_id": c["evidence_id"]}
                  for c in self.conn.execute(
                      "SELECT * FROM capability_claim WHERE snapshot_id = ?"
                      " AND mode = ? ORDER BY capability_key",
                      (snapshot_id, mode["mode"])).fetchall()]
        probes = [{"combination_key": p["combination_key"], "result": p["result"],
                   "probed_at": p["probed_at"], "valid_until": p["valid_until"],
                   "evidence_id": p["evidence_id"]}
                  for p in self.conn.execute(
                      "SELECT * FROM mode_combination_probe WHERE snapshot_id = ?"
                      " AND mode = ? ORDER BY probed_at DESC",
                      (snapshot_id, mode["mode"])).fetchall()]
        return {**mode, "reference_rules": rules, "output_specs": specs,
                "capability_claims": claims, "combination_probes": probes}

    def lifecycle_history(self, tenant_id: str, snapshot_id: str) -> dict:
        row = self._snapshot_row(snapshot_id)
        self._assert_snapshot_visible(row, tenant_id)
        rows = self.conn.execute(
            "SELECT * FROM lifecycle_transition WHERE snapshot_id = ?"
            " ORDER BY occurred_at", (snapshot_id,)).fetchall()
        return {"items": [dict(r) for r in rows]}


def _ceil_minor(value: Decimal, currency: str) -> int:
    """向上取整到币种最小单位。报价上界只许偏高，不许偏低。"""
    digits = CURRENCY_MINOR_DIGITS.get(currency.upper(), 2)
    step = Decimal(10) ** (6 - digits)
    return int((value / step).to_integral_value(rounding=ROUND_CEILING) * step)
