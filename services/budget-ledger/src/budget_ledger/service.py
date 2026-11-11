"""S23 budget-ledger 的业务内核。

范围：plans/S23-budget-ledger.md §1（数据模型）、§2（命令与查询）、§3（状态机）、
§4（Saga 补偿路径）、§5（并发与锁）。

明确不在本内核里（方案自己划出去的）：

- §6.4 账单对账进程（外部文件/API 拉取，独立 Deployment）；
- §9 每日一致性作业（只读审计作业，独立进程）；
- `writer_lease` 单写主选主（§5.3 第 1 层锁）—— 本内核假设单写，
  多写主靠部署侧按租户分区保证；
- §4.3 的 24h 告警与 §9 的 runbook 动作。

**S23 不是财务总账。** 它是生产成本预算账本，用于准入控制与成本归因
（docs/12 §3："不能把生产预算ledger冒充法定总账。"）。

两条必须守住的性质：

1. 金额全是整数 micro，无 float、无 numeric。跨币种永不相加。
2. `available` 可以为负 —— 它是"该层已透支"的精确表达，全树恒等式仍然配平。
   所以**不给 available 建 CHECK**；闸门是 reserve / grant_escrow 的预检。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

MICRO = 1_000_000

RESERVATION_STATES = (
    "RESERVED",
    "AUTHORIZED",
    "ACCRUED",
    "SETTLED",
    "RELEASED",
    "EXPIRED",
)

RELEASE_REASONS = (
    "SETTLED",
    "SAGA_ABORTED",
    "QUOTA_FAILED",
    "FENCE_SUPERSEDED",
    "QUOTE_EXPIRED",
    "NEVER_DISPATCHED",
    "RECONCILED_ABSENT",
    "OPERATOR",
)

# §2.3：这两个 reason 必须带 evidence_ref，否则 422。
REASONS_NEEDING_EVIDENCE = ("QUOTA_FAILED", "SAGA_ABORTED", "RECONCILED_ABSENT")

BILLING_UNITS = (
    "request",
    "generation_second",
    "character",
    "token",
    "pixel",
    "gpu_second",
    "cpu_second",
    "megabyte",
)

ACCRUAL_BASES = (
    "PROVIDER_INVOICE",
    "PROVIDER_QUOTE_CAP",
    "ESTIMATE_CONSERVATIVE",
    "RECONCILED",
)


def _now() -> str:
    return _ts(datetime.now(timezone.utc))


def _ts(value: datetime) -> str:
    """固定宽度 27 字符的 UTC 时间戳。

    固定宽度是为了让字符串比较等价于时间比较：所有 `valid_until > ?`
    这类判断都直接比文本。`datetime.isoformat()` 会在微秒为 0 时省掉小数部分，
    那样长短不一，"2026-01-01T00:00:00Z" > "2026-01-01T00:00:00.000001Z" 就成了
    字典序意外为真的假命题。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _loads(text):
    return json.loads(text) if text else None


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _int(value, field: str) -> int:
    """金额字段必须是整数。`True` 也是 `int`，但金额里它没有意义，一律拒绝。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(400, "MALFORMED_REQUEST", f"{field} must be an integer")
    return value


def _ceil_micro(amount_micro: int, rate_micro: int) -> int:
    """§1.6：一律向上取整，整型运算，不经过浮点。

    方向必须"让 available 变小"——预留与结算都是要占额度的方向。
    退回/释放方向只用于展示，不进这个函数。
    """
    return (amount_micro * rate_micro + MICRO - 1) // MICRO


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self, correlation_id: str = "") -> dict:
        """`contracts/openapi.v2.yaml` 的 `Error` 是**扁平的**，且 `correlation_id`
        有 `minLength: 1` —— 嵌套包一层 `error` 会直接违反 `additionalProperties: false`。"""
        out = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "correlation_id": correlation_id or "s23",
        }
        if self.details:
            out["details"] = self.details
        return out


class Transaction:
    """显式事务。

    `sqlite3` 在 `isolation_level=None` 下是自动提交，`with conn:` **不会**开启
    事务 —— 它只是 try/except/commit，而每次 execute 已经各自提交了。所以这里
    发显式 `BEGIN IMMEDIATE`：它同时拿到写锁，等价于 §5.1 的行锁语义在单库上的
    近似，也是"预检 + 写入"之间不会插入别人的前提。
    """

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


class BudgetLedgerService:
    def __init__(self, conn, *, clock=_now, home_cell: str = "cell_a1",
                 source: str = "S23"):
        self.conn = conn
        # 必须是 `sqlite3.Row`，**不能**是自定义的 dict 工厂：幂等 SDK 与本内核共用
        # 同一条连接，而 SDK 用 `stored_hash, result = row` 按下标解包。换成 dict
        # 行工厂后解包出的是**列名**，SDK 会拿 "request_hash" 当摘要去比对，
        # 于是同键同 body 也被判成 IDEMPOTENCY_KEY_REUSED。
        self.conn.row_factory = sqlite3.Row
        self._clock = clock
        self.home_cell = home_cell
        self.source = source
        self.outbox = Outbox(conn)

    # ------------------------------------------------------------------ 工具

    def _now(self) -> str:
        return self._clock()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:24]}"

    def _insert_entry(
        self,
        *,
        tenant_id: str,
        budget_id: str,
        currency: str,
        entry_type: str,
        business_key: str,
        attribution: dict,
        reason: str | None = None,
        actor: dict | None = None,
        correlation_id: str,
        causation_id: str | None = None,
        reservation_id: str | None = None,
        attempt_id: str | None = None,
        provider_job_ref: str | None = None,
        price_snapshot_ref: dict | None = None,
        fx_snapshot_ref: dict | None = None,
        delta_limit_micro: int = 0,
        delta_reserved_micro: int = 0,
        delta_consumed_micro: int = 0,
        delta_escrowed_micro: int = 0,
    ) -> dict:
        """写一条分录并返回它。`le_single_axis` 由 DB CHECK 兜底。"""
        entry_id = self._new_id("le")
        row = self.conn.execute(
            "INSERT INTO ledger_counter (tenant_id, next_seq) VALUES (?, 1) "
            "ON CONFLICT(tenant_id) DO UPDATE SET next_seq = next_seq + 1 "
            "RETURNING next_seq",
            (tenant_id,),
        ).fetchone()
        seq = row["next_seq"]
        self.conn.execute(
            "INSERT INTO ledger_entry (entry_id, tenant_id, entry_seq, budget_id,"
            " currency, entry_type, delta_limit_micro, delta_reserved_micro,"
            " delta_consumed_micro, delta_escrowed_micro, reservation_id, attempt_id,"
            " provider_job_ref, price_snapshot_ref, fx_snapshot_ref, business_key,"
            " attribution, reason, actor, correlation_id, causation_id, posted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                tenant_id,
                seq,
                budget_id,
                currency,
                entry_type,
                delta_limit_micro,
                delta_reserved_micro,
                delta_consumed_micro,
                delta_escrowed_micro,
                reservation_id,
                attempt_id,
                provider_job_ref,
                _dumps(price_snapshot_ref) if price_snapshot_ref is not None else None,
                _dumps(fx_snapshot_ref) if fx_snapshot_ref is not None else None,
                business_key,
                _dumps(attribution),
                reason,
                _dumps(actor or {"service": "S23"}),
                correlation_id,
                causation_id,
                self._now(),
            ),
        )
        # 回读整行：事件要带 attribution / reservation_id / correlation_id，
        # 让调用方各自拼装会成为第二份真相。
        return dict(self.conn.execute(
            "SELECT * FROM ledger_entry WHERE entry_id = ?", (entry_id,)
        ).fetchone())

    def _budget_row(self, budget_id: str, tenant_id: str) -> dict:
        """读一行预算。跨租户一律 404 —— 不泄漏"存在但你没权限"。"""
        row = self.conn.execute(
            "SELECT * FROM budget WHERE budget_id = ?", (budget_id,)
        ).fetchone()
        if row is None or row["tenant_id"] != tenant_id:
            raise ServiceError(404, "NOT_FOUND", "budget not found",
                               details={"budget_id": budget_id})
        return dict(row)

    @staticmethod
    def _available(row: dict) -> int:
        """§1.3。可以为负 —— 负值就是"该层已透支"。"""
        return (
            row["limit_micro"]
            - row["consumed_micro"]
            - row["outstanding_reserved_micro"]
            - row["escrowed_out_micro"]
        )

    def _budget_after(self, row: dict) -> dict:
        return {
            "budget_id": row["budget_id"],
            "limit_micro": row["limit_micro"],
            "consumed_micro": row["consumed_micro"],
            "outstanding_reserved_micro": row["outstanding_reserved_micro"],
            "available_micro": self._available(row),
            "state": row["state"],
        }

    def _budget_chain(self, budget: dict) -> list[str]:
        """[根, ..., budget_id]，按 depth 升序。"""
        chain = [budget["budget_id"]]
        parent = budget["parent_budget_id"]
        seen = {budget["budget_id"]}
        while parent is not None:
            if parent in seen:  # 数据损坏时的兜底，不让它变成死循环
                raise ServiceError(500, "BUDGET_TREE_CYCLE",
                                   "budget tree contains a cycle",
                                   details={"budget_id": parent})
            row = self.conn.execute(
                "SELECT budget_id, parent_budget_id FROM budget WHERE budget_id = ?",
                (parent,),
            ).fetchone()
            if row is None:
                raise ServiceError(500, "BUDGET_TREE_BROKEN",
                                   "budget parent row is missing",
                                   details={"budget_id": parent})
            chain.append(row["budget_id"])
            seen.add(row["budget_id"])
            parent = row["parent_budget_id"]
        chain.reverse()
        return chain

    def _bump(self, budget_id: str, *, limit_micro: int = 0, reserved_micro: int = 0,
              consumed_micro: int = 0, escrowed_micro: int = 0) -> dict:
        """§5.4：一律相对增量 UPDATE，绝不用应用算出的绝对值。

        绝对赋值在 READ COMMITTED 下会丢更新；这里用 `= col + ?` 让数据库做加法。
        """
        self.conn.execute(
            "UPDATE budget SET limit_micro = limit_micro + ?,"
            " consumed_micro = consumed_micro + ?,"
            " outstanding_reserved_micro = outstanding_reserved_micro + ?,"
            " escrowed_out_micro = escrowed_out_micro + ?,"
            " revision = revision + 1, updated_at = ?"
            " WHERE budget_id = ?",
            (limit_micro, consumed_micro, reserved_micro, escrowed_micro,
             self._now(), budget_id),
        )
        return dict(self.conn.execute(
            "SELECT * FROM budget WHERE budget_id = ?", (budget_id,)
        ).fetchone())

    # ------------------------------------------------------------------ 汇率

    def register_fx_snapshot(self, tenant_id: str, body: dict) -> dict:
        base = body.get("base_currency")
        quote = body.get("quote_currency")
        if not base or not quote:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "base_currency and quote_currency are required")
        if base == quote:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "base_currency and quote_currency must differ")
        rate = _int(body.get("rate_micro"), "rate_micro")
        if rate <= 0:
            raise ServiceError(400, "MALFORMED_REQUEST", "rate_micro must be > 0")
        observed_at = body.get("observed_at") or self._now()
        valid_until = body.get("valid_until")
        if not valid_until:
            raise ServiceError(400, "MALFORMED_REQUEST", "valid_until is required")
        fx_id = body.get("fx_snapshot_id") or self._new_id("fx")
        self.conn.execute(
            "INSERT INTO fx_snapshot (fx_snapshot_id, tenant_id, base_currency,"
            " quote_currency, rate_micro, source, observed_at, valid_until)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fx_id, tenant_id, base, quote, rate, body.get("source") or "CONTRACT",
             observed_at, valid_until),
        )
        return {
            "fx_snapshot_id": fx_id,
            "base_currency": base,
            "quote_currency": quote,
            "rate_micro": rate,
            "valid_until": valid_until,
        }

    def _fx_rate(self, tenant_id: str, base: str, quote: str, ref_id: str | None,
                 at: str) -> dict:
        """取汇率快照。§1.6：预留与结算各用各自时刻的快照。"""
        sql = (
            "SELECT * FROM fx_snapshot WHERE tenant_id = ? AND base_currency = ?"
            " AND quote_currency = ? AND valid_until > ?"
        )
        args: list = [tenant_id, base, quote, at]
        if ref_id:
            sql += " AND fx_snapshot_id = ?"
            args.append(ref_id)
        sql += " ORDER BY observed_at DESC LIMIT 1"
        row = self.conn.execute(sql, args).fetchone()
        if row is None:
            raise ServiceError(
                409,
                "FX_SNAPSHOT_MISSING",
                "no valid fx snapshot for this currency pair",
                details={"base_currency": base, "quote_currency": quote,
                         "fx_snapshot_ref": ref_id},
            )
        return dict(row)

    # ----------------------------------------------------------------- 预算树

    def create_budget(self, tenant_id: str, body: dict, *, actor: dict,
                      correlation_id: str) -> dict:
        """建预算行。

        根预算的 limit 由人工/审批设定（`set_limit`），这是唯一不从父级来的额度。
        **非根预算不接受 limit**：它的 limit 只能来自父级的 `grant_escrow`
        （§1.4）。传了就 422，否则会出现"凭空变出额度"的第二条来源。
        """
        scope_type = body.get("scope_type")
        scope_ref = body.get("scope_ref")
        if scope_type not in ("tenant", "project", "series", "episode", "run",
                             "shot", "node"):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "unknown scope_type", details={"scope_type": scope_type})
        if not scope_ref:
            raise ServiceError(400, "MALFORMED_REQUEST", "scope_ref is required")
        currency = body.get("currency")
        if not currency or len(currency) != 3:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "currency must be an ISO-4217 code")
        parent_id = body.get("parent_budget_id")
        limit = body.get("limit_micro")

        budget_id = body.get("budget_id") or self._new_id("budget")

        with Transaction(self.conn):
            depth = 0
            if parent_id is None:
                if limit is None:
                    raise ServiceError(400, "MALFORMED_REQUEST",
                                       "a root budget needs limit_micro")
                limit = _int(limit, "limit_micro")
                if limit < 0:
                    raise ServiceError(400, "MALFORMED_REQUEST",
                                       "limit_micro must be >= 0")
            else:
                parent = self._budget_row(parent_id, tenant_id)
                depth = parent["depth"] + 1
                if depth > 6:
                    raise ServiceError(422, "SCOPE_NOT_ON_CHAIN",
                                       "budget tree is deeper than 6 levels",
                                       details={"parent_budget_id": parent_id})
                if parent["currency"] != currency:
                    raise ServiceError(
                        422, "CURRENCY_MISMATCH",
                        "child budget currency must equal its parent's",
                        details={"parent_currency": parent["currency"],
                                 "currency": currency})
                # 非根 limit 只能来自 grant_escrow：建行时必须是 0，
                # 之后由 grant_escrow 原子地 +x。
                if limit not in (None, 0):
                    raise ServiceError(
                        422, "LIMIT_ONLY_VIA_ESCROW",
                        "a non-root budget cannot be given a limit directly;"
                        " use grant_escrow",
                        details={"parent_budget_id": parent_id})
                limit = 0

            now = self._now()
            self.conn.execute(
                "INSERT INTO budget (budget_id, tenant_id, scope_type, scope_ref,"
                " parent_budget_id, depth, currency, limit_micro, period_start,"
                " period_end, state, price_snapshot_policy, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)",
                (budget_id, tenant_id, scope_type, scope_ref, parent_id, depth,
                 currency, limit, body.get("period_start"), body.get("period_end"),
                 body.get("price_snapshot_policy") or "STRICT", now, now),
            )
            if limit:
                self._insert_entry(
                    tenant_id=tenant_id, budget_id=budget_id, currency=currency,
                    entry_type="LIMIT_SET",
                    business_key=f"limit_set:{budget_id}",
                    attribution=body.get("attribution") or {},
                    reason="budget created", actor=actor,
                    correlation_id=correlation_id,
                    delta_limit_micro=limit,
                )
            row = dict(self.conn.execute(
                "SELECT * FROM budget WHERE budget_id = ?", (budget_id,)
            ).fetchone())

        return {
            "budget_id": budget_id,
            "tenant_id": tenant_id,
            "scope_type": scope_type,
            "scope_ref": scope_ref,
            "parent_budget_id": parent_id,
            "depth": depth,
            "currency": currency,
            "limit_micro": row["limit_micro"],
            "consumed_micro": row["consumed_micro"],
            "outstanding_reserved_micro": row["outstanding_reserved_micro"],
            "available_micro": self._available(row),
            "state": row["state"],
            "revision": row["revision"],
        }

    def set_limit(self, tenant_id: str, budget_id: str, body: dict, *,
                  actor: dict, correlation_id: str) -> dict:
        """调根预算额度。只有根可以 —— 非根的 limit 恒等于 escrow_balance（§1.4）。"""
        new_limit = _int(body.get("limit_micro"), "limit_micro")
        if new_limit < 0:
            raise ServiceError(400, "MALFORMED_REQUEST", "limit_micro must be >= 0")
        with Transaction(self.conn):
            budget = self._budget_row(budget_id, tenant_id)
            if budget["parent_budget_id"] is not None:
                raise ServiceError(
                    422, "LIMIT_ONLY_VIA_ESCROW",
                    "only a root budget can set its limit directly",
                    details={"parent_budget_id": budget["parent_budget_id"]})
            delta = new_limit - budget["limit_micro"]
            if delta == 0:
                return {**self._budget_after(budget), "replayed": True}
            self._insert_entry(
                tenant_id=tenant_id, budget_id=budget_id,
                currency=budget["currency"], entry_type="LIMIT_SET",
                business_key=f"limit_set:{budget_id}:{new_limit}",
                attribution=body.get("attribution") or {},
                reason=body.get("reason") or "limit set", actor=actor,
                correlation_id=correlation_id, delta_limit_micro=delta,
            )
            # 补充额度把 EXHAUSTED 拉回 ACTIVE（§3.3 状态机）
            row = self._bump(budget_id, limit_micro=delta)
            if row["state"] == "EXHAUSTED" and delta > 0:
                self.conn.execute(
                    "UPDATE budget SET state = 'ACTIVE' WHERE budget_id = ?",
                    (budget_id,))
                self.conn.execute(
                    "UPDATE budget SET revision = revision + 1 WHERE budget_id = ?",
                    (budget_id,))
                row = dict(self.conn.execute(
                    "SELECT * FROM budget WHERE budget_id = ?", (budget_id,)
                ).fetchone())
        return {**self._budget_after(row), "replayed": False}

    def grant_escrow(self, tenant_id: str, body: dict, *, actor: dict,
                     correlation_id: str) -> dict:
        """§1.4 / §5.1：唯一会同时动两行预算的常规路径。

        父 `escrowed_out += x`，子 `limit_micro += x`，同一事务。子不动 outstanding。
        校验父级 `consumed + outstanding + escrowed + x <= limit`。
        """
        parent_id = body.get("parent_budget_id")
        child_id = body.get("child_budget_id")
        amount = _int(body.get("amount_micro"), "amount_micro")
        command_id = body.get("command_id") or self._new_id("grant")
        if not parent_id or not child_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "parent_budget_id and child_budget_id are required")
        if amount <= 0:
            raise ServiceError(422, "AMOUNT_NOT_POSITIVE",
                               "amount_micro must be > 0")
        with Transaction(self.conn):
            parent = self._budget_row(parent_id, tenant_id)
            # §5.1 锁序：(depth ASC, budget_id ASC)。两行一次取，按顺序。
            pair = self.conn.execute(
                "SELECT * FROM budget WHERE budget_id IN (?, ?)"
                " ORDER BY depth ASC, budget_id ASC",
                (parent_id, child_id),
            ).fetchall()
            child = next((dict(r) for r in pair if r["budget_id"] == child_id), None)
            if child is None:
                raise ServiceError(404, "NOT_FOUND", "child budget not found",
                                   details={"budget_id": child_id})
            if child["tenant_id"] != tenant_id:
                raise ServiceError(404, "NOT_FOUND", "child budget not found",
                                   details={"budget_id": child_id})
            if child["parent_budget_id"] != parent_id:
                raise ServiceError(
                    422, "SCOPE_NOT_ON_CHAIN",
                    "child is not a direct child of this parent",
                    details={"parent_budget_id": parent_id,
                             "child_budget_id": child_id})
            if parent["currency"] != child["currency"]:
                raise ServiceError(422, "CURRENCY_MISMATCH",
                                   "cannot escrow across currencies")
            self._assert_can_spend_from(parent, amount)
            if child["state"] == "CLOSED":
                raise ServiceError(409, "BUDGET_PERIOD_CLOSED",
                                   "child budget is closed")

            row = self.conn.execute(
                "SELECT * FROM budget_escrow WHERE parent_budget_id = ?"
                " AND child_budget_id = ? AND currency = ?",
                (parent_id, child_id, child["currency"]),
            ).fetchone()
            now = self._now()
            if row is None:
                escrow_id = self._new_id("escrow")
                self.conn.execute(
                    "INSERT INTO budget_escrow (escrow_id, tenant_id,"
                    " parent_budget_id, child_budget_id, currency, granted_micro,"
                    " returned_micro, child_consumed_micro, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
                    (escrow_id, tenant_id, parent_id, child_id, child["currency"],
                     amount, now, now),
                )
            else:
                escrow_id = row["escrow_id"]
                self.conn.execute(
                    "UPDATE budget_escrow SET granted_micro = granted_micro + ?,"
                    " updated_at = ? WHERE escrow_id = ?",
                    (amount, now, escrow_id),
                )

            key = f"escrow:{parent_id}:{child_id}:{command_id}"
            self._insert_entry(
                tenant_id=tenant_id, budget_id=parent_id,
                currency=parent["currency"], entry_type="ESCROW_GRANT",
                business_key=f"{key}:out",
                attribution=body.get("attribution") or {},
                reason="escrow granted", actor=actor,
                correlation_id=correlation_id, delta_escrowed_micro=amount,
            )
            self._insert_entry(
                tenant_id=tenant_id, budget_id=child_id,
                currency=child["currency"], entry_type="ESCROW_GRANT",
                business_key=f"{key}:in",
                attribution=body.get("attribution") or {},
                reason="escrow received", actor=actor,
                correlation_id=correlation_id, delta_limit_micro=amount,
            )
            self._bump(parent_id, escrowed_micro=amount)
            self._bump(child_id, limit_micro=amount)
            child_after = dict(self.conn.execute(
                "SELECT * FROM budget WHERE budget_id = ?", (child_id,)
            ).fetchone())
            parent_after = dict(self.conn.execute(
                "SELECT * FROM budget WHERE budget_id = ?", (parent_id,)
            ).fetchone())

        return {
            "escrow_id": escrow_id,
            "parent_budget_id": parent_id,
            "child_budget_id": child_id,
            "amount_micro": amount,
            "parent_after": self._budget_after(parent_after),
            "child_after": self._budget_after(child_after),
        }

    def return_escrow(self, tenant_id: str, body: dict, *, actor: dict,
                      correlation_id: str) -> dict:
        """退回未花的委派额度。规则：只能退子级还能让出的部分。"""
        parent_id = body.get("parent_budget_id")
        child_id = body.get("child_budget_id")
        amount = _int(body.get("amount_micro"), "amount_micro")
        command_id = body.get("command_id") or self._new_id("ret")
        if amount <= 0:
            raise ServiceError(422, "AMOUNT_NOT_POSITIVE",
                               "amount_micro must be > 0")
        with Transaction(self.conn):
            self._budget_row(parent_id, tenant_id)
            child = self._budget_row(child_id, tenant_id)
            row = self.conn.execute(
                "SELECT * FROM budget_escrow WHERE parent_budget_id = ?"
                " AND child_budget_id = ?",
                (parent_id, child_id),
            ).fetchone()
            if row is None:
                raise ServiceError(404, "NOT_FOUND", "escrow pair not found")
            # 子级已经花掉的（consumed + outstanding）不能退。
            locked = child["consumed_micro"] + child["outstanding_reserved_micro"]
            returnable = child["limit_micro"] - locked
            if amount > returnable:
                raise ServiceError(
                    409, "INSUFFICIENT_BUDGET",
                    "child has already committed this escrow",
                    details={"returnable_micro": max(returnable, 0),
                             "requested_micro": amount})
            if amount > row["escrow_balance_micro"]:
                raise ServiceError(
                    409, "INSUFFICIENT_BUDGET",
                    "escrow balance is smaller than the requested return",
                    details={"escrow_balance_micro": row["escrow_balance_micro"]})
            now = self._now()
            self.conn.execute(
                "UPDATE budget_escrow SET returned_micro = returned_micro + ?,"
                " updated_at = ? WHERE escrow_id = ?",
                (amount, now, row["escrow_id"]),
            )
            key = f"escrow_ret:{parent_id}:{child_id}:{command_id}"
            self._insert_entry(
                tenant_id=tenant_id, budget_id=parent_id,
                currency=child["currency"], entry_type="ESCROW_RETURN",
                business_key=f"{key}:out",
                attribution=body.get("attribution") or {},
                reason="escrow returned", actor=actor,
                correlation_id=correlation_id, delta_escrowed_micro=-amount,
            )
            self._insert_entry(
                tenant_id=tenant_id, budget_id=child_id,
                currency=child["currency"], entry_type="ESCROW_RETURN",
                business_key=f"{key}:in",
                attribution=body.get("attribution") or {},
                reason="escrow returned", actor=actor,
                correlation_id=correlation_id, delta_limit_micro=-amount,
            )
            self._bump(parent_id, escrowed_micro=-amount)
            self._bump(child_id, limit_micro=-amount)
            parent_after = dict(self.conn.execute(
                "SELECT * FROM budget WHERE budget_id = ?", (parent_id,)
            ).fetchone())
            child_after = dict(self.conn.execute(
                "SELECT * FROM budget WHERE budget_id = ?", (child_id,)
            ).fetchone())
        return {
            "escrow_id": row["escrow_id"],
            "returned_micro": amount,
            "parent_after": self._budget_after(parent_after),
            "child_after": self._budget_after(child_after),
        }

    def _assert_can_spend_from(self, budget: dict, amount: int) -> None:
        """§1.4 每行独立的不变量：`consumed + outstanding + escrowed + 新增 <= limit`。

        这是**准入闸门**，不是事后校验。settle 走的是另一条路（允许越界）。
        """
        if budget["state"] == "FROZEN":
            raise ServiceError(
                409, "BUDGET_FROZEN", "budget is frozen",
                details={"blocking_budget_id": budget["budget_id"]})
        if budget["state"] == "CLOSED":
            raise ServiceError(
                409, "BUDGET_PERIOD_CLOSED", "budget period is closed",
                details={"blocking_budget_id": budget["budget_id"]})
        if budget["state"] == "EXHAUSTED":
            raise ServiceError(
                409, "BUDGET_EXHAUSTED", "budget is exhausted",
                details={"blocking_budget_id": budget["budget_id"]})
        available = self._available(budget)
        if amount > available:
            raise ServiceError(
                409,
                "INSUFFICIENT_BUDGET",
                "available budget is smaller than the requested amount",
                details={
                    "budget_id": budget["budget_id"],
                    "available_micro": available,
                    "requested_micro": amount,
                },
            )

    # ------------------------------------------------------------- 预留与授权

    def reserve(self, tenant_id: str, body: dict, *, actor: dict,
                correlation_id: str) -> dict:
        """§2.1：准入第 1 步。**永远按报价上界预留**，不按预期费用。"""
        amount = _int(body.get("amount_micro"), "amount_micro")
        conservative = _int(body.get("amount_conservative_micro"),
                            "amount_conservative_micro")
        if amount <= 0:
            raise ServiceError(422, "AMOUNT_NOT_POSITIVE",
                               "amount_micro must be > 0")
        if conservative < amount:
            raise ServiceError(422, "CONSERVATIVE_BELOW_AMOUNT",
                               "amount_conservative_micro must be >= amount_micro",
                               details={"amount_micro": amount,
                                        "amount_conservative_micro": conservative})

        budget_scope_id = body.get("budget_scope_id") or body.get("budget_id")
        if not budget_scope_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "budget_scope_id is required")
        currency = body.get("currency")
        budget_currency = body.get("budget_currency")
        if not currency or not budget_currency:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "currency and budget_currency are required")
        attempt_id = body.get("attempt_id")
        if not attempt_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "attempt_id is required")
        if body.get("execution_fence") is None:
            raise ServiceError(400, "MALFORMED_REQUEST", "execution_fence is required")
        for required in ("manifest_ref", "route_decision_ref", "price_snapshot_ref",
                         "quote_expires_at"):
            if body.get(required) is None:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"{required} is required")

        scope = body.get("idempotency_scope") or attempt_id

        with Transaction(self.conn):
            budget = self._budget_row(budget_scope_id, tenant_id)
            if budget["currency"] != budget_currency:
                raise ServiceError(
                    422, "CURRENCY_MISMATCH",
                    "budget_currency does not match the target budget row",
                    details={"budget_currency": budget_currency,
                             "target_budget_currency": budget["currency"]})

            # 这里**不**拒已过期的报价。验收里两个用例要求先建出一条会过期的
            # `RESERVED`，再让它被 janitor 收走（T23-16）或被 authorize 拒掉
            # （T23-18）—— 若在 reserve 就拒，两者都无从构造。过期闸门在两处：
            # `authorize` 的 QUOTE_EXPIRED，和 janitor 的 RESERVED → EXPIRED。
            # 预留额度的占用本身是有时限的（janitor 会还），所以先占着不构成风险。
            quote_expires_at = body["quote_expires_at"]

            # §1.5：同币种直接用上界；跨币种 ceil 换算。
            fx_ref = None
            if currency == budget_currency:
                reserved = conservative
            else:
                # 报价币种与预算币种不同就必须有汇率，服务端按当时快照换算。
                # `fx_snapshot_ref` 只是"用哪一份快照"的**可选**提示 —— 缺它不是
                # 参数错误，而是"此刻没有可用汇率"，所以走 `_fx_rate` 的
                # FX_SNAPSHOT_MISSING（409），不是 422。
                fx_ref = body.get("fx_snapshot_ref")
                fx_id = fx_ref.get("id") if isinstance(fx_ref, dict) else fx_ref
                fx = self._fx_rate(tenant_id, currency, budget_currency, fx_id,
                                   self._now())
                reserved = _ceil_micro(conservative, fx["rate_micro"])
                fx_ref = {
                    "service_id": "S23",
                    "id": fx["fx_snapshot_id"],
                    "rate_micro": fx["rate_micro"],
                    "content_hash": hashlib.sha256(
                        f"{fx['base_currency']}:{fx['quote_currency']}:"
                        f"{fx['rate_micro']}:{fx['valid_until']}".encode()
                    ).hexdigest()[:16],
                }
            if reserved <= 0:
                raise ServiceError(422, "AMOUNT_NOT_POSITIVE",
                                   "reserved amount rounds to zero")

            # T23-03：同 (tenant, scope) 的活预留直接回放，不新建。
            existing = self.conn.execute(
                "SELECT * FROM reservation WHERE tenant_id = ?"
                " AND idempotency_scope = ?"
                " AND state NOT IN ('RELEASED','EXPIRED')",
                (tenant_id, scope),
            ).fetchone()
            if existing is not None:
                return self._reservation_response(dict(existing),
                                                  budget=budget, replayed=True)

            self._assert_can_spend_from(budget, reserved)

            chain = self._budget_chain(budget)
            reservation_id = self._new_id("res")
            now = self._now()
            self.conn.execute(
                "INSERT INTO reservation (reservation_id, tenant_id, budget_id,"
                " budget_chain, currency, amount_micro, amount_conservative_micro,"
                " budget_currency, reserved_budget_currency_micro,"
                " price_snapshot_ref, fx_snapshot_ref, quote_expires_at, attempt_id,"
                " run_id, node_id, node_epoch, execution_fence, manifest_ref,"
                " route_decision_ref, state, revision, idempotency_scope,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " 'RESERVED', 1, ?, ?, ?)",
                (
                    reservation_id, tenant_id, budget["budget_id"], _dumps(chain),
                    currency, amount, conservative, budget_currency, reserved,
                    _dumps(body["price_snapshot_ref"]),
                    _dumps(fx_ref) if fx_ref is not None else None,
                    quote_expires_at, attempt_id, body.get("run_id"),
                    body.get("node_id"), body.get("node_epoch"),
                    body["execution_fence"], _dumps(body["manifest_ref"]),
                    _dumps(body["route_decision_ref"]), scope, now, now,
                ),
            )
            self._insert_entry(
                tenant_id=tenant_id, budget_id=budget["budget_id"],
                currency=budget_currency, entry_type="RESERVE",
                business_key=f"reserve:{reservation_id}",
                attribution=body.get("attribution") or {},
                reason="budget reserved", actor=actor,
                correlation_id=correlation_id, causation_id=attempt_id,
                reservation_id=reservation_id, attempt_id=attempt_id,
                price_snapshot_ref=body["price_snapshot_ref"],
                fx_snapshot_ref=fx_ref,
                delta_reserved_micro=reserved,
            )
            budget = self._bump(budget["budget_id"], reserved_micro=reserved)
            row = dict(self.conn.execute(
                "SELECT * FROM reservation WHERE reservation_id = ?",
                (reservation_id,)).fetchone())
            entries = self.conn.execute(
                "SELECT * FROM ledger_entry WHERE business_key = ?",
                (f"reserve:{reservation_id}",)).fetchall()
            self._emit(tenant_id, budget, entries)

        return self._reservation_response(row, budget=budget, replayed=False)

    def _reservation_response(self, row: dict, *, budget: dict,
                              replayed: bool) -> dict:
        return {
            "reservation_id": row["reservation_id"],
            "state": row["state"],
            "currency": row["currency"],
            "amount_micro": row["amount_micro"],
            "amount_conservative_micro": row["amount_conservative_micro"],
            "budget_currency": row["budget_currency"],
            "reserved_budget_currency_micro": row[
                "reserved_budget_currency_micro"],
            "budget_id": row["budget_id"],
            "budget_chain": _loads(row["budget_chain"]),
            "budget_after": self._budget_after(budget),
            "quote_expires_at": row["quote_expires_at"],
            "replayed": replayed,
        }

    def _reservation(self, reservation_id: str, tenant_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM reservation WHERE reservation_id = ?",
            (reservation_id,)).fetchone()
        if row is None or row["tenant_id"] != tenant_id:
            raise ServiceError(404, "NOT_FOUND", "reservation not found",
                               details={"reservation_id": reservation_id})
        return dict(row)

    def authorize(self, tenant_id: str, reservation_id: str, body: dict, *,
                  actor: dict, correlation_id: str) -> dict:
        """§2.2：RESERVED → AUTHORIZED。只是状态推进，**不动任何计数**。"""
        with Transaction(self.conn):
            row = self._reservation(reservation_id, tenant_id)
            self._assert_revision(row, body)
            if row["state"] == "AUTHORIZED":
                return {"reservation_id": reservation_id, "state": "AUTHORIZED",
                        "authorized_at": row["authorized_at"], "replayed": True}
            if row["state"] != "RESERVED":
                raise ServiceError(
                    409, "RESERVATION_NOT_SETTLEABLE",
                    f"cannot authorize a reservation in state {row['state']}",
                    details={"state": row["state"]})
            if body.get("quote_still_valid") is False:
                raise ServiceError(409, "QUOTE_EXPIRED",
                                   "the quote is no longer valid")
            if row["quote_expires_at"] <= self._now():
                raise ServiceError(409, "QUOTE_EXPIRED", "quote has already expired",
                                   details={"quote_expires_at":
                                            row["quote_expires_at"]})
            now = self._now()
            self.conn.execute(
                "UPDATE reservation SET state = 'AUTHORIZED', authorized_at = ?,"
                " revision = revision + 1, updated_at = ? WHERE reservation_id = ?",
                (now, now, reservation_id))
            budget = dict(self.conn.execute(
                "SELECT * FROM budget WHERE budget_id = ?",
                (row["budget_id"],)).fetchone())
        return {"reservation_id": reservation_id, "state": "AUTHORIZED",
                "authorized_at": now, "replayed": False,
                "budget_after": self._budget_after(budget)}

    @staticmethod
    def _assert_revision(row: dict, body: dict) -> None:
        expected = body.get("expected_revision")
        if expected is None:
            return
        if int(expected) != row["revision"]:
            raise ServiceError(
                409, "VERSION_CONFLICT",
                "reservation revision does not match expected_revision",
                retryable=True,
                details={"expected_revision": expected,
                         "actual_revision": row["revision"]})

    def release(self, tenant_id: str, reservation_id: str, body: dict, *,
                actor: dict, correlation_id: str) -> dict:
        """§2.3：补偿。ACCRUED 不能 release —— 已暂记实际费用，必须先 settle。"""
        reason = body.get("reason")
        if reason not in RELEASE_REASONS:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "unknown release reason", details={"reason": reason})
        if reason == "SETTLED":
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "SETTLED is not a release reason")
        if reason in REASONS_NEEDING_EVIDENCE and body.get("evidence_ref") is None:
            raise ServiceError(
                422, "EVIDENCE_REQUIRED",
                f"reason {reason} requires evidence_ref",
                details={"reason": reason})

        with Transaction(self.conn):
            row = self._reservation(reservation_id, tenant_id)
            self._assert_revision(row, body)
            if row["state"] in ("RELEASED", "EXPIRED"):
                return {"reservation_id": reservation_id, "state": row["state"],
                        "released_micro": 0, "replayed": True}
            if row["state"] == "SETTLED":
                raise ServiceError(409, "RESERVATION_NOT_RELEASABLE",
                                   "reservation is already settled",
                                   details={"state": row["state"]})
            if row["state"] == "ACCRUED":
                # §2.3：只有确认供应商**从未收到**时才允许。
                if reason != "RECONCILED_ABSENT":
                    raise ServiceError(
                        409, "RESERVATION_NOT_RELEASABLE",
                        "an ACCRUED reservation must be settled; only"
                        " RECONCILED_ABSENT may release it",
                        details={"state": row["state"], "reason": reason})

            reserved = row["reserved_budget_currency_micro"]
            now = self._now()
            self.conn.execute(
                "UPDATE reservation SET state = 'RELEASED', released_at = ?,"
                " release_reason = ?, revision = revision + 1, updated_at = ?"
                " WHERE reservation_id = ?", (now, reason, now, reservation_id))
            self._insert_entry(
                tenant_id=tenant_id, budget_id=row["budget_id"],
                currency=row["budget_currency"], entry_type="RELEASE",
                business_key=f"release:{reservation_id}:{reason}",
                attribution=body.get("attribution") or {},
                reason=reason, actor=actor, correlation_id=correlation_id,
                reservation_id=reservation_id, attempt_id=row["attempt_id"],
                delta_reserved_micro=-reserved,
            )
            budget = self._bump(row["budget_id"], reserved_micro=-reserved)
            entry = self.conn.execute(
                "SELECT * FROM ledger_entry WHERE business_key = ?",
                (f"release:{reservation_id}:{reason}",)).fetchall()
            self._emit(tenant_id, budget, entry)

        return {"reservation_id": reservation_id, "state": "RELEASED",
                "released_micro": reserved, "replayed": False,
                "budget_after": self._budget_after(budget)}

    # ------------------------------------------------------------ 暂记与结算

    def accrue(self, tenant_id: str, reservation_id: str, body: dict, *,
               actor: dict, correlation_id: str) -> dict:
        """§2.4：记录实际用量。**不改任何 budget 计数**（§3.1 三个理由）。"""
        accrual = body.get("accrual") or {}
        unit = accrual.get("billing_unit")
        if unit not in BILLING_UNITS:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "unknown billing_unit", details={"billing_unit": unit})
        stage = accrual.get("stage") or "PROVISIONAL"
        if stage not in ("PROVISIONAL", "FINAL"):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "stage must be PROVISIONAL or FINAL")
        basis = accrual.get("basis")
        if basis not in ACCRUAL_BASES:
            raise ServiceError(400, "MALFORMED_REQUEST", "unknown basis",
                               details={"basis": basis})
        billed_micro = _int(accrual.get("billed_micro", 0), "billed_micro")
        if billed_micro < 0:
            raise ServiceError(400, "MALFORMED_REQUEST", "billed_micro must be >= 0")
        quantity = _int(accrual.get("billed_quantity", 0), "billed_quantity")
        if quantity < 0:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "billed_quantity must be >= 0")
        invoice_ref = accrual.get("invoice_ref")
        if stage == "PROVISIONAL" and invoice_ref:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "a PROVISIONAL accrual cannot carry invoice_ref")

        with Transaction(self.conn):
            row = self._reservation(reservation_id, tenant_id)
            if row["state"] in ("RELEASED", "EXPIRED", "SETTLED"):
                raise ServiceError(
                    409, "RESERVATION_NOT_SETTLEABLE",
                    f"cannot accrue a reservation in state {row['state']}",
                    details={"state": row["state"]})
            if row["state"] == "RESERVED":
                raise ServiceError(
                    409, "RESERVATION_NOT_AUTHORIZED",
                    "authorize the reservation before accruing usage",
                    details={"state": row["state"]})

            currency = accrual.get("currency") or row["currency"]
            budget_currency = row["budget_currency"]
            if currency == budget_currency:
                budget_micro = billed_micro
            else:
                fx_id = (accrual.get("fx_snapshot_ref") or {}).get("id") \
                    if isinstance(accrual.get("fx_snapshot_ref"), dict) \
                    else accrual.get("fx_snapshot_ref")
                fx = self._fx_rate(tenant_id, currency, budget_currency, fx_id,
                                   self._now())
                budget_micro = _ceil_micro(billed_micro, fx["rate_micro"])

            # §1.8：保守暂记 = max(报价上界, 已观测)。PROVISIONAL 不得哑过上界太多。
            cap = row["reserved_budget_currency_micro"]
            if stage == "PROVISIONAL" and budget_micro > cap:
                raise ServiceError(
                    422, "ACCRUAL_EXCEEDS_CONSERVATIVE_CAP",
                    "a provisional accrual cannot exceed the conservative cap;"
                    " settle instead",
                    details={"budget_micro": budget_micro, "cap_micro": cap})

            accrual_id = body.get("accrual_id") or self._new_id("ua")
            now = self._now()
            existing = self.conn.execute(
                "SELECT * FROM usage_accrual WHERE tenant_id = ?"
                " AND reservation_id = ? AND stage = ?"
                " AND coalesce(invoice_ref, '') = coalesce(?, '')",
                (tenant_id, reservation_id, stage, invoice_ref)).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO usage_accrual (accrual_id, tenant_id,"
                    " reservation_id, attempt_id, provider_job_ref, billing_unit,"
                    " billed_quantity, quantity_scale, currency, billed_micro,"
                    " budget_currency, billed_budget_currency_micro, stage, basis,"
                    " evidence_ref, invoice_ref, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        accrual_id, tenant_id, reservation_id, row["attempt_id"],
                        accrual.get("provider_job_ref"), unit, quantity,
                        _int(accrual.get("quantity_scale", 1), "quantity_scale"),
                        currency, billed_micro, budget_currency, budget_micro, stage,
                        basis,
                        _dumps(accrual["evidence_ref"])
                        if accrual.get("evidence_ref") is not None else None,
                        invoice_ref, now,
                    ))
            else:
                accrual_id = existing["accrual_id"]

            if row["state"] != "ACCRUED":
                self.conn.execute(
                    "UPDATE reservation SET state = 'ACCRUED', accrued_at = ?,"
                    " revision = revision + 1, updated_at = ?"
                    " WHERE reservation_id = ?", (now, now, reservation_id))
            budget = dict(self.conn.execute(
                "SELECT * FROM budget WHERE budget_id = ?",
                (row["budget_id"],)).fetchone())

        return {
            "accrual_id": accrual_id,
            "reservation_id": reservation_id,
            "reservation_state": "ACCRUED",
            "pending_reconciliation": self._pending_reconciliation(
                tenant_id, reservation_id),
            "budget_after": self._budget_after(budget),
        }

    def _pending_reconciliation(self, tenant_id: str, reservation_id: str) -> bool:
        """§3.4：存在 ACCRUED 且 basis 非 PROVIDER_INVOICE 的暂记即为 true。

        只读派生值，不落库为可写字段（避免第二权威）。
        """
        row = self.conn.execute(
            "SELECT 1 FROM usage_accrual WHERE tenant_id = ? AND reservation_id = ?"
            " AND basis <> 'PROVIDER_INVOICE' LIMIT 1",
            (tenant_id, reservation_id)).fetchone()
        return row is not None

    def settle(self, tenant_id: str, reservation_id: str, body: dict, *,
               actor: dict, correlation_id: str) -> dict:
        """§2.5：终态结算。可以重复调用（同 settlement_id 幂等）。

        **超支也要结算**（§4.5）：钱已经花了，拒绝结算只是丢失记录。
        拒绝的是下一笔 reserve。
        """
        terminal_state = body.get("terminal_state") or "PROVIDER_SUCCEEDED"
        if terminal_state not in ("PROVIDER_SUCCEEDED", "FAILED", "CANCELED"):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "unknown terminal_state")

        with Transaction(self.conn):
            row = self._reservation(reservation_id, tenant_id)
            if row["state"] == "SETTLED":
                # T23-06：同 reservation 但金额不同必须是冲突，不能被
                # "已结算" 的幂等分支吞掉 —— 否则重放了一个不同的金额却返回 200，
                # 调用方会以为自己那笔被记下了。
                submitted = body.get("final_accruals") or []
                if submitted:
                    claimed = sum(
                        _int(i.get("billed_micro", 0), "billed_micro")
                        for i in submitted)
                    existing = self.conn.execute(
                        "SELECT * FROM settlement WHERE reservation_id = ?",
                        (reservation_id,)).fetchone()
                    if existing["settled_micro"] != claimed:
                        raise ServiceError(
                            409, "RESERVATION_ALREADY_SETTLED",
                            "this reservation was settled with a different amount",
                            details={"settled_micro": existing["settled_micro"],
                                     "requested_micro": claimed})
                return self._settled_response(row, replayed=True)
            if row["state"] in ("RELEASED", "EXPIRED"):
                raise ServiceError(
                    409, "RESERVATION_NOT_SETTLEABLE",
                    f"cannot settle a reservation in state {row['state']}",
                    details={"state": row["state"]})
            if row["state"] == "RESERVED":
                raise ServiceError(
                    409, "RESERVATION_NOT_AUTHORIZED",
                    "authorize the reservation before settling",
                    details={"state": row["state"]})

            final = body.get("final_accruals") or []
            budget_currency = row["budget_currency"]
            f_original = 0
            f_budget = 0
            accrual_ids: list[str] = []
            now = self._now()
            for item in final:
                unit = item.get("billing_unit")
                if unit not in BILLING_UNITS:
                    raise ServiceError(400, "MALFORMED_REQUEST",
                                       "unknown billing_unit in final_accruals",
                                       details={"billing_unit": unit})
                basis = item.get("basis")
                if basis not in ACCRUAL_BASES:
                    raise ServiceError(400, "MALFORMED_REQUEST",
                                       "unknown basis in final_accruals",
                                       details={"basis": basis})
                billed = _int(item.get("billed_micro", 0), "billed_micro")
                currency = item.get("currency") or row["currency"]
                if currency == budget_currency:
                    converted = billed
                else:
                    fx_id = item.get("fx_snapshot_ref")
                    if isinstance(fx_id, dict):
                        fx_id = fx_id.get("id")
                    fx = self._fx_rate(tenant_id, currency, budget_currency, fx_id,
                                       now)
                    converted = _ceil_micro(billed, fx["rate_micro"])
                f_original += billed
                f_budget += converted
                invoice_ref = item.get("invoice_ref")
                accrual_id = self._new_id("ua")
                found = self.conn.execute(
                    "SELECT accrual_id FROM usage_accrual WHERE tenant_id = ?"
                    " AND reservation_id = ? AND stage = 'FINAL'"
                    " AND coalesce(invoice_ref, '') = coalesce(?, '')",
                    (tenant_id, reservation_id, invoice_ref)).fetchone()
                if found is not None:
                    accrual_ids.append(found["accrual_id"])
                    continue
                self.conn.execute(
                    "INSERT INTO usage_accrual (accrual_id, tenant_id,"
                    " reservation_id, attempt_id, provider_job_ref, billing_unit,"
                    " billed_quantity, quantity_scale, currency, billed_micro,"
                    " budget_currency, billed_budget_currency_micro, stage, basis,"
                    " evidence_ref, invoice_ref, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'FINAL', ?, ?, ?, ?)",
                    (
                        accrual_id, tenant_id, reservation_id, row["attempt_id"],
                        item.get("provider_job_ref"), unit,
                        _int(item.get("billed_quantity", 0), "billed_quantity"),
                        _int(item.get("quantity_scale", 1), "quantity_scale"),
                        currency, billed, budget_currency, converted, basis,
                        _dumps(item["evidence_ref"])
                        if item.get("evidence_ref") is not None else None,
                        invoice_ref, now,
                    ))
                accrual_ids.append(accrual_id)

            reserved = row["reserved_budget_currency_micro"]
            overage = max(0, f_budget - reserved)
            unused = max(0, reserved - f_budget)
            settlement_id = body.get("settlement_id") or self._new_id("st")

            existing = self.conn.execute(
                "SELECT * FROM settlement WHERE reservation_id = ?",
                (reservation_id,)).fetchone()
            if existing is not None:
                if (existing["settled_budget_currency_micro"] != f_budget
                        or existing["settled_micro"] != f_original):
                    raise ServiceError(
                        409, "RESERVATION_ALREADY_SETTLED",
                        "this reservation was settled with a different amount",
                        details={"settled_micro": existing["settled_micro"],
                                 "requested_micro": f_original})
                return self._settled_response({**row, "state": "SETTLED"},
                                              replayed=True)

            self.conn.execute(
                "INSERT INTO settlement (settlement_id, tenant_id, reservation_id,"
                " accrual_ids, currency, settled_micro, budget_currency,"
                " settled_budget_currency_micro, overage_micro, unused_released_micro,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (settlement_id, tenant_id, reservation_id, _dumps(accrual_ids),
                 row["currency"], f_original, budget_currency, f_budget,
                 overage, unused, now))
            self.conn.execute(
                "UPDATE reservation SET state = 'SETTLED', settled_at = ?,"
                " revision = revision + 1, updated_at = ? WHERE reservation_id = ?",
                (now, now, reservation_id))

            # §3.1：两条分录，同一事务。F = 0 时只写释放那条 ——
            # delta_consumed = 0 会违反 le_single_axis。
            self._insert_entry(
                tenant_id=tenant_id, budget_id=row["budget_id"],
                currency=budget_currency, entry_type="SETTLE",
                business_key=f"settle:{settlement_id}:res",
                attribution=body.get("attribution") or {},
                reason=terminal_state, actor=actor,
                correlation_id=correlation_id,
                reservation_id=reservation_id, attempt_id=row["attempt_id"],
                delta_reserved_micro=-reserved,
            )
            entries = [self.conn.execute(
                "SELECT * FROM ledger_entry WHERE business_key = ?",
                (f"settle:{settlement_id}:res",)).fetchone()]
            if f_budget > 0:
                self._insert_entry(
                    tenant_id=tenant_id, budget_id=row["budget_id"],
                    currency=budget_currency, entry_type="SETTLE",
                    business_key=f"settle:{settlement_id}:con",
                    attribution=body.get("attribution") or {},
                    reason=terminal_state, actor=actor,
                    correlation_id=correlation_id,
                    reservation_id=reservation_id, attempt_id=row["attempt_id"],
                    delta_consumed_micro=f_budget,
                )
                entries.append(self.conn.execute(
                    "SELECT * FROM ledger_entry WHERE business_key = ?",
                    (f"settle:{settlement_id}:con",)).fetchone())

            budget = self._bump(row["budget_id"], reserved_micro=-reserved,
                               consumed_micro=f_budget)
            # §4.5 第 2 步：越界即 EXHAUSTED。**不向上传播。**
            if budget["state"] == "ACTIVE" and self._available(budget) < 0:
                self.conn.execute(
                    "UPDATE budget SET state = 'EXHAUSTED', revision = revision + 1"
                    " WHERE budget_id = ?", (row["budget_id"],))
                budget = dict(self.conn.execute(
                    "SELECT * FROM budget WHERE budget_id = ?",
                    (row["budget_id"],)).fetchone())
            if overage > 0:
                # §4.5 第 2 步：写 adjustment 解释"为什么超"，
                # **不写 ledger_entry** —— SETTLE 已全额记进 consumed。
                self.conn.execute(
                    "INSERT INTO adjustment (adjustment_id, tenant_id, budget_id,"
                    " currency, amount_micro, reason_code, detail, source_ref,"
                    " approval_ref, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 'QUOTE_UNDERESTIMATE', ?, ?, ?, ?)",
                    (self._new_id("adj"), tenant_id, row["budget_id"],
                     budget_currency, overage,
                     "settled amount exceeded the conservative reservation",
                     _dumps({"settlement_id": settlement_id,
                             "reservation_id": reservation_id}),
                     _dumps({"kind": "system", "rule": "S23 §4.5"}),
                     now))
            self._emit(tenant_id, budget, entries,
                       settled_micro=f_original, overage_micro=overage)
        return {
            "settlement_id": settlement_id,
            "reservation_id": reservation_id,
            "reservation_state": "SETTLED",
            "settled_micro": f_original,
            "reserved_micro": reserved,
            "settled_budget_currency_micro": f_budget,
            "unused_released_micro": unused,
            "overage_micro": overage,
            "budget_after": self._budget_after(budget),
            "pending_reconciliation": self._pending_reconciliation(
                tenant_id, reservation_id),
            "replayed": False,
        }

    def _settled_response(self, row: dict, *, replayed: bool) -> dict:
        settlement = self.conn.execute(
            "SELECT * FROM settlement WHERE reservation_id = ?",
            (row["reservation_id"],)).fetchone()
        budget = dict(self.conn.execute(
            "SELECT * FROM budget WHERE budget_id = ?",
            (row["budget_id"],)).fetchone())
        return {
            "settlement_id": settlement["settlement_id"],
            "reservation_id": row["reservation_id"],
            "reservation_state": "SETTLED",
            "settled_micro": settlement["settled_micro"],
            "reserved_micro": row["reserved_budget_currency_micro"],
            "settled_budget_currency_micro": settlement[
                "settled_budget_currency_micro"],
            "unused_released_micro": settlement["unused_released_micro"],
            "overage_micro": settlement["overage_micro"],
            "budget_after": self._budget_after(budget),
            "pending_reconciliation": self._pending_reconciliation(
                row["tenant_id"], row["reservation_id"]),
            "replayed": replayed,
        }

    # --------------------------------------------------------------- 调整类

    def record_refund(self, tenant_id: str, body: dict, *, actor: dict,
                      correlation_id: str) -> dict:
        """§1.11：退款必须来自外部确认。没有 `external_credit_ref` 就是 422。"""
        external = body.get("external_credit_ref")
        if not external:
            raise ServiceError(
                422, "EXTERNAL_CREDIT_REQUIRED",
                "a refund must reference an external provider credit")
        amount = _int(body.get("amount_micro"), "amount_micro")
        if amount <= 0:
            raise ServiceError(422, "AMOUNT_NOT_POSITIVE",
                               "amount_micro must be > 0")
        refund_id = body.get("refund_id") or self._new_id("ref")
        with Transaction(self.conn):
            budget = self._budget_row(body.get("budget_id"), tenant_id)
            now = self._now()
            self.conn.execute(
                "INSERT INTO refund (refund_id, tenant_id, budget_id, reservation_id,"
                " currency, amount_micro, reason_code, external_credit_ref, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (refund_id, tenant_id, budget["budget_id"],
                 body.get("reservation_id"), budget["currency"], amount,
                 body.get("reason_code") or "PROVIDER_CREDIT", external, now))
            self._insert_entry(
                tenant_id=tenant_id, budget_id=budget["budget_id"],
                currency=budget["currency"], entry_type="REFUND",
                business_key=f"refund:{refund_id}",
                attribution=body.get("attribution") or {},
                reason=body.get("reason_code") or "PROVIDER_CREDIT",
                actor=actor, correlation_id=correlation_id,
                reservation_id=body.get("reservation_id"),
                delta_consumed_micro=-amount,
            )
            budget = self._bump(budget["budget_id"], consumed_micro=-amount)
        return {"refund_id": refund_id, "amount_micro": amount,
                "budget_after": self._budget_after(budget)}

    def set_frozen(self, tenant_id: str, budget_id: str, body: dict, *,
                   frozen: bool, actor: dict, correlation_id: str) -> dict:
        """§3.3：FROZEN 只拒绝本层新的 reserve 与 grant_escrow。

        **祖先冻结不传播到子级** —— 额度在 grant 时已下推，子级 limit 是它自己的。
        全停由 S02 的 kill switch 令 S26 停发 Saga 实现，不靠 S23 级联。
        """
        with Transaction(self.conn):
            budget = self._budget_row(budget_id, tenant_id)
            if frozen and budget["state"] == "CLOSED":
                raise ServiceError(409, "BUDGET_PERIOD_CLOSED",
                                   "a closed budget cannot be frozen")
            now = self._now()
            if frozen:
                self.conn.execute(
                    "INSERT INTO budget_freeze (freeze_id, tenant_id, budget_id,"
                    " frozen_at, reason) VALUES (?, ?, ?, ?, ?)",
                    (self._new_id("frz"), tenant_id, budget_id, now,
                     body.get("reason") or "operator"))
                new_state = "FROZEN"
            else:
                self.conn.execute(
                    "UPDATE budget_freeze SET unfrozen_at = ?"
                    " WHERE budget_id = ? AND unfrozen_at IS NULL",
                    (now, budget_id))
                # 解冻回到"本来的样子"：越界仍是 EXHAUSTED，否则 ACTIVE。
                new_state = "EXHAUSTED" if self._available(budget) < 0 else "ACTIVE"
            self.conn.execute(
                "UPDATE budget SET state = ?, revision = revision + 1,"
                " updated_at = ? WHERE budget_id = ?",
                (new_state, now, budget_id))
            row = dict(self.conn.execute(
                "SELECT * FROM budget WHERE budget_id = ?", (budget_id,)
            ).fetchone())
        return self._budget_after(row)

    # ---------------------------------------------------------------- 查询

    def get_budget(self, tenant_id: str, budget_id: str) -> dict:
        """§2.6：严格符合 openapi —— **没有 available_micro**。

        openapi 定死了 `additionalProperties: false`。可用额只能走 `/availability`。
        这是有意的：docs/04 §5 要求需要强一致余额时回源权威服务，而 UI 常常把
        available 缓存成静态值。强制它单独取，能减少误用。
        """
        budget = self._budget_row(budget_id, tenant_id)
        return {
            "budget_id": budget["budget_id"],
            "currency": budget["currency"],
            "limit_micro": budget["limit_micro"],
            "consumed_micro": budget["consumed_micro"],
            "outstanding_reserved_micro": budget["outstanding_reserved_micro"],
            "pending_reconciliation": self._budget_pending_reconciliation(
                tenant_id, budget_id),
        }

    def _budget_pending_reconciliation(self, tenant_id: str,
                                       budget_id: str) -> bool:
        """§3.4：范围是**该 budget 行自己的** reservation，不含子级。"""
        row = self.conn.execute(
            "SELECT 1 FROM reservation r JOIN usage_accrual u"
            "  ON u.reservation_id = r.reservation_id"
            " WHERE r.tenant_id = ? AND r.budget_id = ? AND r.state = 'ACCRUED'"
            "   AND u.basis <> 'PROVIDER_INVOICE' LIMIT 1",
            (tenant_id, budget_id)).fetchone()
        return row is not None

    def availability(self, tenant_id: str, budget_id: str) -> dict:
        budget = self._budget_row(budget_id, tenant_id)
        return {
            "budget_id": budget_id,
            "currency": budget["currency"],
            "limit_micro": budget["limit_micro"],
            "consumed_micro": budget["consumed_micro"],
            "outstanding_reserved_micro": budget["outstanding_reserved_micro"],
            "escrowed_out_micro": budget["escrowed_out_micro"],
            "available_micro": self._available(budget),
            "state": budget["state"],
            "revision": budget["revision"],
        }

    def tree(self, tenant_id: str, budget_id: str) -> dict:
        """§2.6：子树各层可用额。用递归 CTE，只读。"""
        self._budget_row(budget_id, tenant_id)
        rows = self.conn.execute(
            "WITH RECURSIVE sub(budget_id, depth) AS ("
            "  SELECT budget_id, depth FROM budget WHERE budget_id = ?"
            "  UNION ALL"
            "  SELECT b.budget_id, b.depth FROM budget b"
            "  JOIN sub s ON b.parent_budget_id = s.budget_id"
            ") SELECT b.* FROM budget b JOIN sub ON b.budget_id = sub.budget_id"
            " WHERE b.tenant_id = ? ORDER BY b.depth ASC, b.budget_id ASC",
            (budget_id, tenant_id)).fetchall()
        nodes = [self._budget_after(dict(r)) for r in rows]
        return {
            "budget_id": budget_id,
            "nodes": nodes,
            # §5.7 全树恒等式：Σavailable + Σoutstanding + Σconsumed = 根 limit。
            # escrowed_out **不**进等式 —— 它是分配动作，不是消耗；算进去就双算。
            "invariant": {
                "available_plus_outstanding_plus_consumed": sum(
                    n["available_micro"] + n["outstanding_reserved_micro"]
                    + n["consumed_micro"] for n in nodes),
                "root_limit_micro": nodes[0]["limit_micro"] if nodes else 0,
            },
        }

    def list_budgets(self, tenant_id: str, *, scope_type: str | None = None,
                     scope_ref: str | None = None) -> list[dict]:
        sql = "SELECT * FROM budget WHERE tenant_id = ?"
        args: list = [tenant_id]
        if scope_type:
            sql += " AND scope_type = ?"
            args.append(scope_type)
        if scope_ref:
            sql += " AND scope_ref = ?"
            args.append(scope_ref)
        sql += " ORDER BY depth ASC, budget_id ASC"
        return [self._budget_after(dict(r))
                for r in self.conn.execute(sql, args).fetchall()]

    def get_reservation(self, tenant_id: str, reservation_id: str) -> dict:
        row = self._reservation(reservation_id, tenant_id)
        return {
            "reservation_id": row["reservation_id"],
            "state": row["state"],
            "revision": row["revision"],
            "budget_id": row["budget_id"],
            "budget_chain": _loads(row["budget_chain"]),
            "attempt_id": row["attempt_id"],
            "run_id": row["run_id"],
            "node_id": row["node_id"],
            "node_epoch": row["node_epoch"],
            "execution_fence": row["execution_fence"],
            "currency": row["currency"],
            "amount_micro": row["amount_micro"],
            "amount_conservative_micro": row["amount_conservative_micro"],
            "budget_currency": row["budget_currency"],
            "reserved_budget_currency_micro": row[
                "reserved_budget_currency_micro"],
            "quote_expires_at": row["quote_expires_at"],
            "authorized_at": row["authorized_at"],
            "accrued_at": row["accrued_at"],
            "settled_at": row["settled_at"],
            "released_at": row["released_at"],
            "release_reason": row["release_reason"],
        }

    def list_reservations(self, tenant_id: str, *, attempt_id: str | None = None,
                          state: str | None = None,
                          run_id: str | None = None,
                          since: str | None = None,
                          until: str | None = None) -> list[dict]:
        sql = "SELECT * FROM reservation WHERE tenant_id = ?"
        args: list = [tenant_id]
        if attempt_id:
            sql += " AND attempt_id = ?"
            args.append(attempt_id)
        if state:
            sql += " AND state = ?"
            args.append(state)
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        if since:
            sql += " AND created_at >= ?"
            args.append(since)
        if until:
            sql += " AND created_at <= ?"
            args.append(until)
        sql += " ORDER BY created_at DESC LIMIT 500"
        return [self.get_reservation(tenant_id, r["reservation_id"])
                for r in self.conn.execute(sql, args).fetchall()]

    def ledger_entries(self, tenant_id: str, *, budget_id: str | None = None,
                       from_entry_seq: int | None = None,
                       limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM ledger_entry WHERE tenant_id = ?"
        args: list = [tenant_id]
        if budget_id:
            sql += " AND budget_id = ?"
            args.append(budget_id)
        if from_entry_seq is not None:
            sql += " AND entry_seq >= ?"
            args.append(int(from_entry_seq))
        sql += " ORDER BY entry_seq ASC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        out = []
        for r in self.conn.execute(sql, args).fetchall():
            row = dict(r)
            row["attribution"] = _loads(row["attribution"])
            row["actor"] = _loads(row["actor"])
            row["price_snapshot_ref"] = _loads(row["price_snapshot_ref"])
            row["fx_snapshot_ref"] = _loads(row["fx_snapshot_ref"])
            out.append(row)
        return out

    def attempt_cost(self, tenant_id: str, attempt_id: str) -> dict:
        """§2.6：单个 attempt 的全部费用（含失败返工）。

        每个 attempt 有自己的 reservation，所以"返工"天然分开记账，
        这里求和只是便于核对。
        """
        rows = self.conn.execute(
            "SELECT r.reservation_id, r.state, r.currency,"
            "       r.reserved_budget_currency_micro,"
            "       s.settled_micro, s.settled_budget_currency_micro, s.overage_micro"
            "  FROM reservation r LEFT JOIN settlement s"
            "    ON s.reservation_id = r.reservation_id"
            " WHERE r.tenant_id = ? AND r.attempt_id = ?", (tenant_id, attempt_id)
        ).fetchall()
        settled_budget = sum((r["settled_budget_currency_micro"] or 0) for r in rows)
        reserved_budget = sum(
            r["reserved_budget_currency_micro"] for r in rows
            if r["state"] in ("RESERVED", "AUTHORIZED", "ACCRUED"))
        return {
            "attempt_id": attempt_id,
            "reservation_count": len(rows),
            "settled_budget_currency_micro": settled_budget,
            "outstanding_budget_currency_micro": reserved_budget,
            "reservations": [
                {
                    "reservation_id": r["reservation_id"],
                    "state": r["state"],
                    "currency": r["currency"],
                    "settled_micro": r["settled_micro"],
                    "settled_budget_currency_micro": r[
                        "settled_budget_currency_micro"],
                    "overage_micro": r["overage_micro"],
                }
                for r in rows
            ],
        }

    def episode_cost(self, tenant_id: str, episode_id: str) -> dict:
        """§2.6：集级成本归因（给 S44 用）。按 attribution 里的 episode_id 分组。

        归因只读 `ledger_entry.attribution` —— 那是记账时写死的快照，
        之后改预算树结构不会改写历史归因。
        """
        rows = self.conn.execute(
            "SELECT currency, sum(delta_consumed_micro) AS consumed,"
            "       sum(delta_reserved_micro) AS reserved"
            "  FROM ledger_entry"
            " WHERE tenant_id = ?"
            "   AND json_extract(attribution, '$.episode_id') = ?"
            " GROUP BY currency ORDER BY currency",
            (tenant_id, episode_id)).fetchall()
        # 跨币种永不相加：按币种分开列，不给"总计"。
        return {
            "episode_id": episode_id,
            "by_currency": [
                {
                    "currency": r["currency"],
                    "consumed_micro": r["consumed"] or 0,
                    "reserved_delta_micro": r["reserved"] or 0,
                }
                for r in rows
            ],
        }

    def reconciliation_queue(self, tenant_id: str, *,
                             min_age_seconds: int = 0) -> list[dict]:
        """§2.6 / §3.4：待对账列表 —— ACCRUED 且 basis 非 PROVIDER_INVOICE。"""
        rows = self.conn.execute(
            "SELECT r.*, u.basis, u.billed_budget_currency_micro, u.accrual_id"
            "  FROM reservation r JOIN usage_accrual u"
            "    ON u.reservation_id = r.reservation_id"
            " WHERE r.tenant_id = ? AND r.state = 'ACCRUED'"
            "   AND u.basis <> 'PROVIDER_INVOICE'"
            " ORDER BY r.updated_at ASC LIMIT 500",
            (tenant_id,)).fetchall()
        return [
            {
                "reservation_id": r["reservation_id"],
                "attempt_id": r["attempt_id"],
                "budget_id": r["budget_id"],
                "accrual_id": r["accrual_id"],
                "basis": r["basis"],
                "reserved_budget_currency_micro": r[
                    "reserved_budget_currency_micro"],
                "accrued_budget_currency_micro": r["billed_budget_currency_micro"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    # ---------------------------------------------------------------- 运维

    def expire_stale_quotes(self, tenant_id: str) -> dict:
        """§2.4 janitor：只把过期的 `RESERVED` 置为 `EXPIRED`。

        **`AUTHORIZED` 绝不在这里动**（§3.1）：S22 的 permit 可能已 INFLIGHT，
        116 讲"未知提交不盲目重提"，对应到账本就是"不盲目放手"。
        `ACCRUED` 同理留在原处，进 P1 告警。
        """
        now = self._now()
        rows = self.conn.execute(
            "SELECT * FROM reservation WHERE tenant_id = ? AND state = 'RESERVED'"
            " AND quote_expires_at <= ?", (tenant_id, now)).fetchall()
        expired = []
        for r in rows:
            rid = r["reservation_id"]
            reserved = r["reserved_budget_currency_micro"]
            with Transaction(self.conn):
                cur = self.conn.execute(
                    "UPDATE reservation SET state = 'EXPIRED', released_at = ?,"
                    " release_reason = 'QUOTE_EXPIRED', revision = revision + 1,"
                    " updated_at = ? WHERE reservation_id = ? AND state = 'RESERVED'",
                    (now, now, rid))
                if cur.rowcount != 1:
                    continue
                self._insert_entry(
                    tenant_id=tenant_id, budget_id=r["budget_id"],
                    currency=r["budget_currency"], entry_type="RELEASE",
                    business_key=f"release:{rid}:QUOTE_EXPIRED",
                    attribution={"janitor": "S23.expire_stale_quotes"},
                    reason="QUOTE_EXPIRED", actor={"service": "S23.janitor"},
                    correlation_id=f"janitor:{rid}",
                    reservation_id=rid, attempt_id=r["attempt_id"],
                    delta_reserved_micro=-reserved,
                )
                budget = self._bump(r["budget_id"], reserved_micro=-reserved)
                entry = self.conn.execute(
                    "SELECT * FROM ledger_entry WHERE business_key = ?",
                    (f"release:{rid}:QUOTE_EXPIRED",)).fetchall()
                self._emit(tenant_id, budget, entry)
            expired.append(rid)
        return {"tenant_id": tenant_id, "expired_count": len(expired),
                "expired": expired}

    def rebuild_counters(self, tenant_id: str, budget_id: str) -> dict:
        """§9 第 3 项：用分录重放重建四个计数，与行上的值对比。

        **只读**：这里返回重建值，不写回。写回是人工运维动作，需要审批 ——
        自动"修复"账本会让真实漂移消失。
        """
        self._budget_row(budget_id, tenant_id)
        row = self.conn.execute(
            "SELECT coalesce(sum(delta_limit_micro), 0)     AS lim,"
            "       coalesce(sum(delta_consumed_micro), 0)  AS con,"
            "       coalesce(sum(delta_reserved_micro), 0)  AS res,"
            "       coalesce(sum(delta_escrowed_micro), 0)  AS esc"
            "  FROM ledger_entry WHERE tenant_id = ? AND budget_id = ?",
            (tenant_id, budget_id)).fetchone()
        return {
            "limit_micro": row["lim"],
            "consumed_micro": row["con"],
            "outstanding_reserved_micro": row["res"],
            "escrowed_out_micro": row["esc"],
        }

    # ---------------------------------------------------------------- 事件

    def _emit(self, tenant_id: str, budget: dict, entries, *,
              settled_micro: int | None = None,
              overage_micro: int = 0) -> None:
        """发 `finance.ledger.entry-posted`。

        §6.1：S23 **只发这一种 event_type**；其他语义（耗尽/对账）折叠进 data。

        粒度是**业务动作**，不是分录：settle 写两条分录（:res 与 :con，
        因为 le_single_axis 禁止一条分录动两个维度），但发一个事件，
        `data` 里两个 delta 都带 —— 这正是 §6.1 例子里 SETTLE 同时给出
        `delta_reserved_micro: -800000` 和 `delta_consumed_micro: 780000` 的形状。
        `aggregate_sequence` 取该动作**最后一条**分录的 entry_seq。

        因此 seq 会有跳号。这是设计允许的：ledger_counter 本身也会因回滚留空洞，
        消费方只按 seq 排序，不拿它做丢包检测（§6.3 的对账靠 Inbox + 全树恒等式）。

        **必须在调用方的事务内调用**：Outbox 与业务事实同事务，否则 COMMIT 之后
        崩溃会留下"账上有钱、账本没事件"。
        """
        rows = [dict(e) for e in entries if e is not None]
        last = rows[-1] if rows else {}
        reserved_id = None
        for r in rows:
            reserved_id = reserved_id or r.get("reservation_id")
        data = {
            "entry_ids": [r.get("entry_id") for r in rows],
            "entry_type": last.get("entry_type"),
            "budget_id": budget["budget_id"],
            "currency": budget["currency"],
            "delta_reserved_micro": sum(
                r.get("delta_reserved_micro") or 0 for r in rows),
            "delta_consumed_micro": sum(
                r.get("delta_consumed_micro") or 0 for r in rows),
            "delta_limit_micro": sum(
                r.get("delta_limit_micro") or 0 for r in rows),
            "delta_escrowed_micro": sum(
                r.get("delta_escrowed_micro") or 0 for r in rows),
            "reservation_id": reserved_id,
            "attempt_id": last.get("attempt_id"),
            "provider_job_ref": last.get("provider_job_ref"),
            "settled_micro": settled_micro,
            "overage_micro": overage_micro,
            "pending_reconciliation": (
                self._pending_reconciliation(tenant_id, reserved_id)
                if reserved_id else False),
            "attribution": _loads(last.get("attribution")) or {},
            "budget_after": self._budget_after(budget),
        }
        correlation_id = last.get("correlation_id") or last.get("entry_id") or ""
        envelope = new_event(
            source=self.source,
            type="finance.ledger.entry-posted",
            subject=f"budget/{budget['budget_id']}",
            tenant_id=tenant_id,
            home_cell=self.home_cell,
            aggregate_id=budget["budget_id"],
            sequence=last.get("entry_seq") or 0,
            correlation_id=correlation_id,
            # 契约要求 causation_id 有 minLength: 1（C23-15 登记的平台缺口）。
            # 根事件没有上游，用它自己的 entry_id 作因果起点，避免发出空串。
            causation_id=(last.get("causation_id")
                          or last.get("entry_id") or correlation_id),
            data=data,
        )
        self.outbox.append(envelope)

    # ------------------------------------------------------- 消费 S01 撤销

    def consume_access_revoked(self, tenant_id: str, event: dict,
                               *, correlation_id: str) -> dict:
        """§6.2：租户撤权 → 该租户所有 RESERVED 预留 release(OPERATOR)。

        **AUTHORIZED 不动**：S22 的 permit 可能已 INFLIGHT，钱不能单方面放手
        （§3.1「AUTHORIZED 不能 expire」）。
        """
        rows = self.conn.execute(
            "SELECT reservation_id FROM reservation WHERE tenant_id = ?"
            " AND state = 'RESERVED'", (tenant_id,)).fetchall()
        released = []
        for r in rows:
            result = self.release(
                tenant_id, r["reservation_id"],
                {"reason": "OPERATOR",
                 "evidence_ref": {"service_id": "S01",
                                  "id": event.get("id") or "access-revoked"}},
                actor={"service": "S01"}, correlation_id=correlation_id)
            released.append(result["reservation_id"])
        return {"tenant_id": tenant_id, "released_count": len(released),
                "released": released}
