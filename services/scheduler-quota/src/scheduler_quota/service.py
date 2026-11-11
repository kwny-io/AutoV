"""S22 scheduler-quota 的业务内核。

范围：plans/S22-scheduler-quota.md §1（数据模型）、§2（命令与查询）、§3（状态机）、
§4（Saga 补偿）、§5（并发/锁/Redis/租约/WFQ）、§6（事件）。

明确不在本内核里（方案自己划出去的）：

- §2.6 的 `:resume` 所需的 S02 授权校验 —— 这里只校验调用方是内部 principal，
  授权判定在 S02；本内核记录 `actor` 以便审计。
- §9 的每分钟/每日对账作业（独立进程）—— 内核只提供 `reconcile()` 这一次调用。
- 真正的 Redis 客户端（§5.2）—— 见 C22-11。
- §5.3 的 PG 行锁。SQLite 用 `BEGIN IMMEDIATE` 拿到全库写锁，是"单写"的更强
  近似；锁序 `shard → pool → share → entry` 在代码里仍然按这个顺序执行，
  换到 PG 时不需要重排。

三条必须守住的性质：

1. **S22 不是钱。** `reservation_ref` 是不透明引用，本文件从不校验余额、
   不读 S23、不消费 `finance.ledger.entry-posted`。这里也**没有**"换池重试"
   的分支（T22-51）：路由由 S25 定，S22 找不到池就报错，不自行降级。
2. **两级计数同时守恒。** 池级 `inflight_used` 与账号级 `account_used_inflight`
   在同一个事务里 +1/-1。`resource_class` 是 gpu/cpu 的池不参与这两级。
3. **只有 `RESERVED` 能被 TTL 收回。** `DISPATCH_AUTHORIZED` 与 `INFLIGHT`
   一律不自动释放 —— 租约到期、时间过去很久、请求方崩溃都不算终态证据。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Inbox, Outbox, new_event

# §6.1：aggregate_sequence 是**状态编码**，不是每 permit 的递增计数器。
# PSP-2 登记了这个二义性；本服务选定状态编码（见 plans §11 C22-13）。
STATE_SEQUENCE = {
    "RESERVED": 1,
    "DISPATCH_AUTHORIZED": 2,
    "INFLIGHT": 3,
    "RELEASED": 4,
    "EXPIRED": 5,
}

LIVE_STATES = ("RESERVED", "DISPATCH_AUTHORIZED", "INFLIGHT")
HOLDING_STATES = ("RESERVED", "DISPATCH_AUTHORIZED", "INFLIGHT")

# 释放理由 → 允许从哪些状态发起（§3.1 的表）。
#
# RESERVED 也收 SUPERSEDED_BY_FENCE / SUPERSEDED_BY_BUDGET：T22-26 说的是
# 「未 dispatch 的 permit → release(SUPERSEDED_BY_FENCE)」，而 RESERVED 按定义
# 就是还没 dispatch。§3.1 的表只列了 DISPATCH_AUTHORIZED 那一行，等于把
# fence 递增时最该被回收的那批 permit 逼到用 OPERATOR 兜底，丢掉原因。
# 见 C22-18。
RELEASE_REASONS_BY_SOURCE = {
    "RESERVED": ("OPERATOR", "NEVER_DISPATCHED", "SUPERSEDED_BY_FENCE",
                 "SUPERSEDED_BY_BUDGET"),
    "DISPATCH_AUTHORIZED": ("SUPERSEDED_BY_BUDGET", "SUPERSEDED_BY_FENCE", "OPERATOR"),
    "INFLIGHT": ("SETTLED", "RECONCILED_ABSENT", "OPERATOR"),
}

# 这两类资源池不参与 inflight / account 两级计数（§1.1 末段）。
RESOURCE_SECONDS_CLASSES = ("gpu_second", "cpu_media_second")


def _now() -> str:
    return _ts(datetime.now(timezone.utc))


def _ts(value: datetime) -> str:
    """固定宽度 27 字符 UTC 时间戳；理由同 S23（字符串序 == 时间序）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _plus(ts: str, seconds: float) -> str:
    return _ts(_parse(ts) + timedelta(seconds=seconds))


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _loads(text):
    return json.loads(text) if text else None


def _deadline(value: str | None, field: str = "deadline") -> str | None:
    """请求里的时间点统一成固定宽度 UTC。

    不归一化就与 `enqueued_at` 不是同一个尺子，`deadline < now` 的字符串比较
    会在 `...00Z` / `...+08:00` / `...00.5Z` 上判错。方案示例里的
    `"2026-10-30T10:00:00Z"` 正是这个形状。
    """
    if not value:
        return None
    if not isinstance(value, str):
        raise ServiceError(400, "MALFORMED_REQUEST", f"{field} must be a string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"{field} is not an ISO-8601 timestamp")
    return _ts(parsed)


def _int(value, field: str, *, minimum: int | None = None) -> int:
    """计数一律整数。`True` 是 `int` 但配额里没有意义，拒绝。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(400, "MALFORMED_REQUEST", f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"{field} must be >= {minimum}")
    return value


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
        # `contracts/openapi.v2.yaml` 的 Error 是**扁平**的，correlation_id 必填。
        out = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "correlation_id": correlation_id or "s22",
        }
        if self.details:
            out["details"] = self.details
        return out


class Transaction:
    """显式事务。理由同 S23：`isolation_level=None` 下 `with conn:` 不是事务。

    `BEGIN IMMEDIATE` 立刻拿写锁，因此"预检 + 自增"之间不会插入别人 ——
    这就是 T22-06「100 并发只授予 5 个」在单库上的成立条件。
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


class SchedulerQuotaService:
    def __init__(self, conn, *, clock=_now, home_cell: str = "cell_a1",
                 source: str = "S22", aging_rate: float = 0.5):
        self.conn = conn
        # 必须是 `sqlite3.Row`：幂等 SDK 用 `stored_hash, result = row` 按**下标**
        # 解包，dict 行工厂会让它拿到列名当摘要（S23 踩过的坑，C23-16）。
        self.conn.row_factory = sqlite3.Row
        self._clock = clock
        self.home_cell = home_cell
        self.source = source
        self.aging_rate = aging_rate
        self.outbox = Outbox(conn)
        self.inbox = Inbox(conn)
        # §5.5：Redis 丢失后的保守窗口。没有 Redis 的实现细节见 C22-11。
        self._degraded_until: str | None = None

    # ------------------------------------------------------------------ 工具

    def _now(self) -> str:
        return self._clock()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:24]}"

    # ------------------------------------------------------------ 池与分片

    @staticmethod
    def shard_key_for(provider_id: str, account_ref: str, region: str) -> str:
        """§5.1：同一 (provider, account, region) 的全部池落在同一分片。"""
        return f"{provider_id}/{account_ref}/{region}"

    def create_pool(self, tenant_id: str, body: dict, *, actor: dict,
                    correlation_id: str) -> dict:
        required = ("provider_id", "channel_id", "account_ref", "region", "modality")
        missing = [k for k in required if not body.get(k)]
        if missing:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"missing required fields: {', '.join(missing)}",
                               details={"missing": missing})
        scope = body.get("tenant_scope") or "shared"
        if scope != "shared" and scope != tenant_id:
            # 不允许替别的租户建 BYOK 池；那是跨租户借用凭证的入口（T22-42）。
            raise ServiceError(403, "TENANT_SCOPE_DENIED",
                               "tenant_scope must be 'shared' or the caller's tenant",
                               details={"tenant_scope": scope})
        resource_class = body.get("resource_class") or "api_request"
        inflight_limit = _int(body.get("inflight_limit"), "inflight_limit", minimum=1)
        account_cap = _int(body.get("account_cap_inflight"), "account_cap_inflight",
                           minimum=1)

        provider = body["provider_id"]
        account = body["account_ref"]
        region = body["region"]
        shard_key = self.shard_key_for(provider, account, region)
        pool_id = body.get("pool_id") or self._new_id("pool")
        now = self._now()

        with Transaction(self.conn):
            shard = self.conn.execute(
                "SELECT * FROM pool_shard WHERE shard_key = ?", (shard_key,)
            ).fetchone()
            if shard is None:
                shard_id = self.conn.execute(
                    "SELECT COALESCE(MAX(shard_id), 0) + 1 AS n FROM pool_shard"
                ).fetchone()["n"]
                self.conn.execute(
                    "INSERT INTO pool_shard (shard_key, shard_id, account_cap_inflight,"
                    " account_used_inflight) VALUES (?, ?, ?, 0)",
                    (shard_key, shard_id, account_cap),
                )
            elif shard["account_cap_inflight"] != account_cap:
                raise ServiceError(
                    422, "SHARD_CAP_CONFLICT",
                    "account_cap_inflight is fixed per shard; both pools share the"
                    " same provider account and must agree",
                    details={"shard_key": shard_key,
                             "account_cap_inflight": shard["account_cap_inflight"],
                             "requested": account_cap},
                )

            # T22-15：同一分片内 Σ pool.inflight_limit 不得超过账号上限。
            # 只在参与 inflight 计数的池之间求和（§1.1 末段）。
            accounted = self.conn.execute(
                "SELECT COALESCE(SUM(inflight_limit), 0) AS total FROM quota_pool"
                " WHERE shard_key = ? AND resource_class NOT IN (?, ?)",
                (shard_key, *RESOURCE_SECONDS_CLASSES),
            ).fetchone()["total"]
            add = 0 if resource_class in RESOURCE_SECONDS_CLASSES else inflight_limit
            if accounted + add > account_cap:
                raise ServiceError(
                    422, "SHARD_CAP_EXCEEDED",
                    "sum of pool inflight limits would exceed the account cap",
                    details={"shard_key": shard_key,
                             "account_cap_inflight": account_cap,
                             "already_allocated": accounted,
                             "requested": add},
                )

            self.conn.execute(
                "INSERT INTO quota_pool (pool_id, tenant_scope, provider_id, channel_id,"
                " account_ref, region, modality, resource_class, shard_key, rpm_limit,"
                " rpm_remaining_window, window_reset_at, tpm_limit, tpm_remaining_window,"
                " inflight_limit, inflight_used, resource_seconds_limit,"
                " resource_seconds_used_milli, state, revision, source_revision, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0,"
                " 'ACTIVE', 1, 0, ?)",
                (
                    pool_id, scope, provider, body["channel_id"], account, region,
                    body["modality"], resource_class, shard_key,
                    body.get("rpm_limit"),
                    int(body.get("rpm_remaining_window") or 0),
                    _deadline(body.get("window_reset_at"), "window_reset_at"),
                    body.get("tpm_limit"),
                    int(body.get("tpm_remaining_window") or 0),
                    inflight_limit,
                    body.get("resource_seconds_limit"),
                    now,
                ),
            )
        return self.get_pool(tenant_id, pool_id)

    def set_pool_limits(self, tenant_id: str, pool_id: str, body: dict, *,
                        actor: dict, correlation_id: str) -> dict:
        """改池定义。**允许**把 limit 降到当前 used 以下（T22-14）。

        上限不是列约束，是发放入口的事务内预检（C22-7）：运维下调配额时
        已有的 permit 必须继续跑到终态，不能让一条 UPDATE 自己失败。
        """
        pool = self._pool_row(pool_id, tenant_id)
        new_limit = body.get("inflight_limit")
        with Transaction(self.conn):
            pool = self._pool_row(pool_id, tenant_id)
            target_limit = (pool["inflight_limit"] if new_limit is None
                            else _int(new_limit, "inflight_limit", minimum=1))
            # 上调查也要重算 Σ ≤ cap，往下调永远安全。
            if target_limit > pool["inflight_limit"] and \
                    pool["resource_class"] not in RESOURCE_SECONDS_CLASSES:
                accounted = self.conn.execute(
                    "SELECT COALESCE(SUM(inflight_limit), 0) AS total FROM quota_pool"
                    " WHERE shard_key = ? AND pool_id <> ? AND resource_class NOT IN (?, ?)",
                    (pool["shard_key"] if "shard_key" in pool.keys() else
                     self.shard_key_for(pool["provider_id"], pool["account_ref"],
                                        pool["region"]),
                     pool_id, *RESOURCE_SECONDS_CLASSES),
                ).fetchone()["total"]
                cap = self.conn.execute(
                    "SELECT account_cap_inflight FROM pool_shard WHERE shard_key = ?",
                    (self.shard_key_for(pool["provider_id"], pool["account_ref"],
                                        pool["region"]),),
                ).fetchone()["account_cap_inflight"]
                if accounted + target_limit > cap:
                    raise ServiceError(
                        422, "SHARD_CAP_EXCEEDED",
                        "sum of pool inflight limits would exceed the account cap",
                        details={"account_cap_inflight": cap,
                                 "already_allocated": accounted,
                                 "requested": target_limit},
                    )
            self.conn.execute(
                "UPDATE quota_pool SET inflight_limit = ?, rpm_limit = ?,"
                " rpm_remaining_window = CASE WHEN ? IS NULL THEN rpm_remaining_window"
                "   ELSE ? END,"
                " tpm_limit = ?, resource_seconds_limit = ?,"
                " revision = revision + 1, updated_at = ? WHERE pool_id = ?",
                (
                    target_limit,
                    body.get("rpm_limit", pool["rpm_limit"]),
                    body.get("rpm_remaining_window"),
                    body.get("rpm_remaining_window"),
                    body.get("tpm_limit", pool["tpm_limit"]),
                    body.get("resource_seconds_limit", pool["resource_seconds_limit"]),
                    self._now(),
                    pool_id,
                ),
            )
        return self.get_pool(tenant_id, pool_id)

    def suspend_pool(self, tenant_id: str, pool_id: str, body: dict, *,
                     actor: dict, correlation_id: str) -> dict:
        """§3.3：SUSPENDED 拒新 acquire，**已有 permit 不受影响**。"""
        reason = body.get("suspend_reason") or body.get("reason") or "MANUAL"
        with Transaction(self.conn):
            self._pool_row(pool_id, tenant_id)
            self.conn.execute(
                "UPDATE quota_pool SET state = 'SUSPENDED', suspend_reason = ?,"
                " revision = revision + 1, updated_at = ? WHERE pool_id = ?",
                (reason, self._now(), pool_id),
            )
        return self.get_pool(tenant_id, pool_id)

    def resume_pool(self, tenant_id: str, pool_id: str, body: dict, *,
                    actor: dict, correlation_id: str) -> dict:
        """恢复。§3.3 要求 S02 授权 + 健康探测 —— 授权在调用方（S02），
        这里只要求 `authorization_ref` 存在，并把它写进审计。"""
        authorization_ref = body.get("authorization_ref")
        if not authorization_ref:
            raise ServiceError(403, "TENANT_SCOPE_DENIED",
                               "resume requires an S02 authorization_ref",
                               details={"pool_id": pool_id})
        with Transaction(self.conn):
            self._pool_row(pool_id, tenant_id)
            self.conn.execute(
                "UPDATE quota_pool SET state = 'ACTIVE', suspend_reason = NULL,"
                " revision = revision + 1, updated_at = ? WHERE pool_id = ?",
                (self._now(), pool_id),
            )
        return self.get_pool(tenant_id, pool_id)

    def drain_pool(self, tenant_id: str, pool_id: str, body: dict | None = None,
                   *, actor: dict, correlation_id: str) -> dict:
        """下线的第一步：拒绝新 acquire，已有 permit 走完。"""
        with Transaction(self.conn):
            self._pool_row(pool_id, tenant_id)
            self.conn.execute(
                "UPDATE quota_pool SET state = 'DRAINING', revision = revision + 1,"
                " updated_at = ? WHERE pool_id = ? AND state <> 'DISABLED'",
                (self._now(), pool_id),
            )
        return self.get_pool(tenant_id, pool_id)

    def _pool_row(self, pool_id: str, tenant_id: str) -> dict:
        """读池。跨租户的 BYOK 池一律 404（T22-39/T22-42）。"""
        row = self.conn.execute(
            "SELECT * FROM quota_pool WHERE pool_id = ?", (pool_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "quota pool not found",
                               details={"pool_id": pool_id})
        pool = dict(row)
        if pool["tenant_scope"] != "shared" and pool["tenant_scope"] != tenant_id:
            raise ServiceError(404, "NOT_FOUND", "quota pool not found",
                               details={"pool_id": pool_id})
        return pool

    @staticmethod
    def _pool_shard_key(pool: dict) -> str:
        return pool.get("shard_key") or SchedulerQuotaService.shard_key_for(
            pool["provider_id"], pool["account_ref"], pool["region"])

    def _shard_row(self, shard_key: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM pool_shard WHERE shard_key = ?", (shard_key,)
        ).fetchone()
        if row is None:
            raise ServiceError(500, "SHARD_MISSING",
                               "pool_shard row is missing for this pool",
                               details={"shard_key": shard_key})
        return dict(row)

    # ------------------------------------------------------------ 缓存降级

    def mark_cache_degraded(self, seconds: float = 2.0) -> dict:
        """§5.5 步骤 1：进入保守窗口，所有 acquire 立即 503。

        宁可多等一会，也不超发 —— 快速拒绝的误判方向必须偏保守。
        """
        self._degraded_until = _plus(self._now(), seconds)
        return {"degraded_until": self._degraded_until}

    def is_degraded(self) -> bool:
        return (self._degraded_until is not None
                and self._now() < self._degraded_until)

    def _assert_cache_ok(self) -> None:
        if self.is_degraded():
            raise ServiceError(
                503, "CACHE_DEGRADED",
                "quota cache is being rebuilt; admission is paused conservatively",
                retryable=True,
                details={"degraded_until": self._degraded_until},
            )

    def rebuild_cache(self) -> dict:
        """§5.5 步骤 2–4：重建 = 用 permit 聚合核对 PG 计数，然后退出降级。

        Redis 里没有"PG 不知道的计数"，所以重建立即完成；这一方法真正做的事
        是步骤 3 的对账，以及清掉降级标记。
        """
        report = self.reconcile()
        self._degraded_until = None
        return {"rebuilt": True, "degraded": False, "reconcile": report}

    # ---------------------------------------------------------------- 路由

    def _route_of(self, body: dict) -> dict:
        """把请求里的路由元组取出来。

        `route_decision_ref` 本身是**不透明引用**（C22-5），但 S22 不能去查 S25，
        所以路由元组必须随请求内联。扁平字段优先，其次读 ref 内部 —— 两种写法
        都接受，避免把"引用形状"变成第二处硬约定（C22-8）。

        `account_ref` 不是必填：S25 的 RouteDecision 只有在它已经选定账号池时
        才给（docs/09 的模型主键含 `account_pool`，L1 降级就是换这一格）。给了
        就**钉死**，S22 不许换；没给才由 S22 在同一路由的健康池之间分摊。
        """
        ref = body.get("route_decision_ref")
        inner = ref if isinstance(ref, dict) else {}
        route = {
            "provider_id": body.get("provider_id") or inner.get("provider_id"),
            "channel_id": body.get("channel_id") or inner.get("channel_id"),
            "region": body.get("region") or inner.get("region"),
            "modality": body.get("modality") or inner.get("modality"),
        }
        account = body.get("account_ref") or inner.get("account_ref")
        if account:
            route["account_ref"] = account
        return route

    def _resolve_pool(self, tenant_id: str, body: dict) -> dict:
        """按路由找池。**只在同一路由内挑候选，绝不跨路由降级**（§0.1）。"""
        requested = body.get("requested_pool_id")
        if requested:
            pool = self._pool_row(requested, tenant_id)  # BYOK 跨租户 → 404
            if pool["state"] == "DISABLED":
                raise ServiceError(422, "POOL_NOT_FOUND_FOR_ROUTE",
                                   "requested pool is disabled",
                                   details={"pool_id": requested, "state": "DISABLED"})
            modality = body.get("modality")
            if modality and pool["modality"] != modality:
                raise ServiceError(
                    422, "POOL_MODALITY_MISMATCH",
                    "requested pool serves a different modality",
                    details={"pool_id": requested, "pool_modality": pool["modality"],
                             "requested_modality": modality})
            return pool

        route = self._route_of(body)
        required = ["provider_id", "channel_id", "region", "modality"]
        missing = [k for k in required if not route.get(k)]
        if missing:
            raise ServiceError(
                422, "POOL_NOT_FOUND_FOR_ROUTE",
                "route tuple is incomplete; S22 will not guess a pool",
                details={"missing": missing, "required": required})

        # 路由钉了账号就只在那个账号内部找，绝不落到同 provider 的别的账号上：
        # 换账号是 S25 的 L1 降级，必须由 S25 重新决策并给出新引用（§0.1）。
        account = route.get("account_ref")
        sql = ("SELECT * FROM quota_pool WHERE provider_id = ? AND channel_id = ?"
               " AND region = ? AND modality = ?"
               " AND (tenant_scope = 'shared' OR tenant_scope = ?)"
               " AND state IN ('ACTIVE','SUSPENDED','DRAINING')")
        args = [route["provider_id"], route["channel_id"], route["region"],
                route["modality"], tenant_id]
        if account:
            sql += " AND account_ref = ?"
            args.append(account)
        pools = [dict(r) for r in self.conn.execute(sql, args).fetchall()]
        if not pools:
            # T22-45：找不到就报错，不搜索其他池、不建 permit、不占计数。
            raise ServiceError(
                422, "POOL_NOT_FOUND_FOR_ROUTE",
                "no quota pool matches the route decision",
                details={"route": route})

        active = [p for p in pools if p["state"] == "ACTIVE"]
        if active:
            # 同池优先（BYOK 专用池），再按负载率升序，最后 pool_id 稳定排序。
            active.sort(key=lambda p: (
                0 if p["tenant_scope"] != "shared" else 1,
                (p["inflight_used"] / p["inflight_limit"]
                 if p["inflight_limit"] else 0),
                p["pool_id"],
            ))
            return active[0]
        # 没有可用的 ACTIVE 池：按最具体的理由报错，**不换账号**（T22-46）。
        suspended = [p for p in pools if p["state"] == "SUSPENDED"]
        if suspended:
            raise ServiceError(
                503, "POOL_SUSPENDED",
                "the only pools on this route are suspended",
                retryable=True,
                details={"pool_id": suspended[0]["pool_id"],
                         "suspend_reason": suspended[0]["suspend_reason"],
                         "account_ref": suspended[0]["account_ref"]})
        return pools[0]  # DRAINING：交给 _check_capacity 报 429 drained

    # -------------------------------------------------------------- 容量

    def _check_capacity(self, pool: dict, tenant_id: str, body: dict) -> dict:
        """事务内预检。返回本次要扣的额度说明；不通过则抛错且**不改任何计数**。"""
        if pool["state"] == "SUSPENDED":
            raise ServiceError(503, "POOL_SUSPENDED", "pool is suspended",
                               retryable=True,
                               details={"pool_id": pool["pool_id"],
                                        "suspend_reason": pool["suspend_reason"]})
        if pool["state"] == "DRAINING":
            raise ServiceError(
                429, "NO_CAPACITY", "pool is draining and accepts no new permits",
                retryable=True,
                details={"pool_id": pool["pool_id"], "bottleneck": "drained",
                         "estimated_wait_ms": None, "queue_position": None,
                         "account_saturated": False})
        if pool["state"] == "DISABLED":
            raise ServiceError(422, "POOL_NOT_FOUND_FOR_ROUTE",
                               "pool is disabled",
                               details={"pool_id": pool["pool_id"]})
        if pool["resource_class"] in RESOURCE_SECONDS_CLASSES:
            # GPU / CPU 池只走资源秒，不占 inflight，也不共享账号在途上限。
            need = int(body.get("resource_seconds_milli") or
                       int(float(body.get("expected_run_seconds") or 0) * 1000))
            limit = pool["resource_seconds_limit"]
            if limit is not None and \
                    pool["resource_seconds_used_milli"] + need > limit:
                raise ServiceError(
                    429, "NO_CAPACITY", "pool resource seconds exhausted",
                    retryable=True,
                    details={"pool_id": pool["pool_id"],
                             "bottleneck": "resource_seconds",
                             "used_milli": pool["resource_seconds_used_milli"],
                             "limit": limit,
                             "estimated_wait_ms": None, "queue_position": None,
                             "account_saturated": False})
            return {"inflight": 0, "account": 0, "rpm": 0, "resource_milli": need}

        shard = self._shard_row(self._pool_shard_key(pool))
        # 顺序：账号级 → 池级 → rpm → tpm。前两个都是并发维度，先判共享的更严的。
        if shard["account_used_inflight"] + 1 > shard["account_cap_inflight"]:
            raise ServiceError(
                429, "NO_CAPACITY",
                "provider account is at its inflight cap",
                retryable=True,
                details={"pool_id": pool["pool_id"], "bottleneck": "account",
                         "account_used_inflight": shard["account_used_inflight"],
                         "account_cap_inflight": shard["account_cap_inflight"],
                         "estimated_wait_ms": self._estimate_wait_ms(pool),
                         "queue_position": None, "account_saturated": True})
        if pool["inflight_used"] + 1 > pool["inflight_limit"]:
            raise ServiceError(
                429, "NO_CAPACITY", "pool at inflight limit", retryable=True,
                details={"pool_id": pool["pool_id"], "bottleneck": "inflight",
                         "inflight_used": pool["inflight_used"],
                         "inflight_limit": pool["inflight_limit"],
                         "estimated_wait_ms": self._estimate_wait_ms(pool),
                         "queue_position": None, "account_saturated": False})
        if pool["rpm_limit"] is not None and pool["rpm_remaining_window"] <= 0:
            raise ServiceError(
                429, "NO_CAPACITY", "pool rpm window is exhausted", retryable=True,
                details={"pool_id": pool["pool_id"], "bottleneck": "rpm",
                         "rpm_limit": pool["rpm_limit"],
                         "window_reset_at": pool["window_reset_at"],
                         "estimated_wait_ms": self._estimate_wait_ms(pool),
                         "queue_position": None, "account_saturated": False})
        if pool["tpm_limit"] is not None and pool["tpm_remaining_window"] <= 0:
            raise ServiceError(
                429, "NO_CAPACITY", "pool tpm window is exhausted", retryable=True,
                details={"pool_id": pool["pool_id"], "bottleneck": "tpm",
                         "tpm_limit": pool["tpm_limit"],
                         "window_reset_at": pool["window_reset_at"],
                         "estimated_wait_ms": self._estimate_wait_ms(pool),
                         "queue_position": None, "account_saturated": False})
        return {"inflight": 1, "account": 1, "rpm": 1, "resource_milli": 0}

    def _estimate_wait_ms(self, pool: dict) -> int | None:
        """§2.1：粗算，**不是 SLA 承诺**。只在有 inflight 上限时有意义。"""
        if not pool["inflight_limit"]:
            return None
        avg = self._avg_service_seconds(pool["pool_id"]) or 0
        ahead = self.conn.execute(
            "SELECT COUNT(*) AS n FROM queue_entry WHERE pool_id = ?"
            " AND state = 'WAITING'", (pool["pool_id"],)).fetchone()["n"]
        return int(avg * 1000 * (ahead + 1) / pool["inflight_limit"])

    def _avg_service_seconds(self, pool_id: str) -> float | None:
        """从已完成 permit 的 dispatched→released 时长估均值。"""
        row = self.conn.execute(
            "SELECT AVG((julianday(released_at) - julianday(dispatched_at)) * 86400.0)"
            " AS avg_seconds FROM permit WHERE pool_id = ? AND state = 'RELEASED'"
            " AND dispatched_at IS NOT NULL AND released_at IS NOT NULL",
            (pool_id,),
        ).fetchone()
        return row["avg_seconds"]

    # ---------------------------------------------------------------- 领取

    def acquire(self, tenant_id: str, body: dict, *, actor: dict,
                correlation_id: str) -> dict:
        """§2.1 准入第 2 步。

        找不到容量时抛 429 并把 `queue_entry_id` / `queue_position` 填上 ——
        那正是调用方 `wait=true` 重试时要用到的座位号。
        """
        try:
            return self._acquire(tenant_id, body, actor=actor,
                                 correlation_id=correlation_id)
        except ServiceError as exc:
            if exc.code == "NO_CAPACITY" and body.get("wait"):
                self._attach_queue_position(tenant_id, body, exc)
            raise

    def _attach_queue_position(self, tenant_id: str, body: dict,
                              exc: ServiceError) -> None:
        pool_id = exc.details.get("pool_id")
        if not pool_id:
            return
        try:
            entry = self.enqueue(tenant_id, body, correlation_id="acquire")
        except ServiceError:
            return
        exc.details["queue_entry_id"] = entry["queue_entry_id"]
        exc.details["queue_position"] = entry["queue_position"]

    def _acquire(self, tenant_id: str, body: dict, *, actor: dict,
                 correlation_id: str) -> dict:
        reservation_ref = body.get("reservation_ref")
        amount = body.get("reservation_amount_micro")
        if not reservation_ref or amount is None:
            raise ServiceError(422, "INVALID_RESERVATION_REF",
                               "reservation_ref is required and amount must be present",
                               details={"reservation_ref": reservation_ref})
        amount = _int(amount, "reservation_amount_micro")
        if amount < 0:
            raise ServiceError(422, "INVALID_RESERVATION_REF",
                               "reservation_amount_micro must be >= 0",
                               details={"reservation_amount_micro": amount})
        attempt_id = body.get("attempt_id")
        if not attempt_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "attempt_id is required")
        if body.get("execution_fence") is None:
            raise ServiceError(400, "MALFORMED_REQUEST", "execution_fence is required")
        fence = _int(body.get("execution_fence"), "execution_fence", minimum=1)
        if body.get("route_decision_ref") is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "route_decision_ref is required")
        deductible_key = body.get("deductible_key") or attempt_id

        if body.get("wait"):
            return self._acquire_waiting(tenant_id, body, correlation_id=correlation_id)
        return self._acquire_once(tenant_id, body, deductible_key=deductible_key,
                                  fence=fence, correlation_id=correlation_id)

    def _acquire_once(self, tenant_id: str, body: dict, *, deductible_key: str,
                      fence: int, correlation_id: str,
                      queue_entry_id: str | None = None) -> dict:
        attempt_id = body["attempt_id"]
        self._assert_cache_ok()

        # 先在事务外把池选出来只为给出便宜的 422/404/503，真正的判定在事务内重读。
        candidate = self._resolve_pool(tenant_id, body)

        with Transaction(self.conn):
            self._assert_cache_ok()
            # §5.4 第 3 步：同 (tenant, deductible_key) 的活 permit 直接回放。
            existing = self.conn.execute(
                "SELECT * FROM permit WHERE tenant_id = ? AND deductible_key = ?"
                " AND state <> 'EXPIRED'", (tenant_id, deductible_key),
            ).fetchone()
            if existing is not None:
                permit = dict(existing)
                if permit["attempt_id"] != attempt_id:
                    # T22-04：同键不同 attempt 是调用方 bug，不能静默复用。
                    raise ServiceError(
                        422, "DEDUCTIBLE_KEY_CONFLICT",
                        "deductible_key is already bound to another attempt",
                        details={"deductible_key": deductible_key,
                                 "attempt_id": permit["attempt_id"],
                                 "requested_attempt_id": attempt_id})
                if permit["pool_id"] != candidate["pool_id"]:
                    raise ServiceError(
                        422, "DEDUCTIBLE_KEY_CONFLICT",
                        "deductible_key is already bound to another pool",
                        details={"deductible_key": deductible_key,
                                 "pool_id": permit["pool_id"],
                                 "requested_pool_id": candidate["pool_id"]})
                replay = self._acquire_response(permit, replayed=True)
                if queue_entry_id:
                    replay["queue_entry_id"] = queue_entry_id
                return replay

            # T22-26 的前置：更新的 fence 已经出现过，这次领取就是陈旧的。
            newer = self.conn.execute(
                "SELECT MAX(execution_fence) AS f FROM permit WHERE tenant_id = ?"
                " AND attempt_id = ? AND state IN ('RESERVED','DISPATCH_AUTHORIZED',"
                " 'INFLIGHT')", (tenant_id, attempt_id),
            ).fetchone()["f"]
            if newer is not None and newer > fence:
                raise ServiceError(
                    422, "FENCE_SUPERSEDED",
                    "a newer execution_fence already owns this attempt",
                    details={"attempt_id": attempt_id, "execution_fence": fence,
                             "current_fence": newer})

            pool = self._pool_row(candidate["pool_id"], tenant_id)
            need = self._check_capacity(pool, tenant_id, body)

            if body.get("modality") and pool["modality"] != body["modality"]:
                raise ServiceError(
                    422, "POOL_MODALITY_MISMATCH",
                    "requested modality does not match the pool",
                    details={"pool_id": pool["pool_id"],
                             "pool_modality": pool["modality"],
                             "requested_modality": body["modality"]})

            now = self._now()
            lease_ttl = float(body.get("lease_ttl_seconds") or 120)
            permit_id = self._new_id("permit")
            shard_key = self._pool_shard_key(pool)
            # §2.1：`grant_epoch` 是**队列授予序号**，permit 内单调递增，
            # 所有 `expected_grant_epoch` CAS 都以它为准。这里只负责起一个非零值。
            grant_epoch = self.conn.execute(
                "SELECT COALESCE(MAX(grant_epoch), 0) + 1 AS n FROM permit"
            ).fetchone()["n"]
            self.conn.execute(
                "INSERT INTO permit (permit_id, tenant_id, pool_id, shard_key,"
                " reservation_ref, reservation_amount_micro, budget_scope_id,"
                " attempt_id, run_id, execution_fence, node_id, node_epoch,"
                " route_decision_ref, state, release_reason, grant_epoch,"
                " lease_expires_at, dispatch_id, provider_job_ref,"
                " resource_milli_reserved, deductible_key, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', NULL, ?,"
                " ?, NULL, NULL, ?, ?, ?, ?)",
                (
                    permit_id, tenant_id, pool["pool_id"], shard_key,
                    body["reservation_ref"], body["reservation_amount_micro"],
                    body.get("budget_scope_id") or body.get("budget_id") or "",
                    attempt_id, body.get("run_id"), fence,
                    body.get("node_id"), body.get("node_epoch"),
                    _dumps(body["route_decision_ref"]),
                    grant_epoch,
                    _plus(now, lease_ttl),
                    need["resource_milli"],
                    deductible_key, now, now,
                ),
            )
            self._apply_counters(pool, shard_key, inflight=need["inflight"],
                                 account=need["account"], resource_milli=need["resource_milli"],
                                 rpm_delta=-need["rpm"] if need["rpm"] else 0,
                                 reason="ACQUIRE", permit_id=permit_id,
                                 tenant_id=tenant_id, tenant_scope=tenant_id)
            if queue_entry_id:
                self.conn.execute(
                    "UPDATE queue_entry SET state = 'GRANTED', granted_permit_id = ?"
                    " WHERE queue_entry_id = ?", (permit_id, queue_entry_id))
            row = dict(self.conn.execute(
                "SELECT * FROM permit WHERE permit_id = ?", (permit_id,)).fetchone())
            # §5.7「授予后」对**所有**授予生效，不只是排队授予。漏了立即授予，
            # 大租户的虚拟时间就永远不前进，它会一直排在队首，小租户被饿死
            # （T22-35 / T22-36 要的就是这一格）。
            self._bump_share(pool["pool_id"], tenant_id,
                             expected_run_seconds=int(
                                 body.get("expected_run_seconds") or 0))
            self._emit_permit(row, previous_state=None, correlation_id=correlation_id,
                              causation_id=attempt_id)

        result = self._acquire_response(row, replayed=False)
        if queue_entry_id:
            result["queue_entry_id"] = queue_entry_id
        return result

    def _acquire_waiting(self, tenant_id: str, body: dict, *,
                         correlation_id: str) -> dict:
        """有界等待：入队 → 反复尝试授予队首 → 超时 429 QUEUE_TIMEOUT。

        不阻塞端口上的其他请求（每次尝试是独立事务），只占用本次调用的时间。
        """
        timeout = float(body.get("wait_timeout_seconds") or 30)
        entry = self.enqueue(tenant_id, body, correlation_id=correlation_id)
        deadline = time.monotonic() + timeout
        last: ServiceError | None = None
        while True:
            try:
                return self._grant_next(body["requested_pool_id"] if
                                        body.get("requested_pool_id") else
                                        entry["pool_id"],
                                        tenant_id=tenant_id, body=body,
                                        entry=entry, correlation_id=correlation_id)
            except ServiceError as exc:
                last = exc
                if exc.code not in ("NO_CAPACITY", "QUEUE_TIMEOUT"):
                    raise
            if time.monotonic() >= deadline:
                raise ServiceError(
                    429, "QUEUE_TIMEOUT",
                    "wait window expired without a grant",
                    retryable=True,
                    details={"queue_entry_id": entry["queue_entry_id"],
                             "queue_position": self._queue_position(
                                 entry["pool_id"], entry["queue_entry_id"]),
                             "bottleneck": (last.details.get("bottleneck")
                                            if last else None),
                             "estimated_wait_ms": (last.details.get("estimated_wait_ms")
                                                   if last else None)})
            time.sleep(0.02)

    def _grant_next(self, pool_id: str, *, tenant_id: str, body: dict, entry: dict,
                    correlation_id: str) -> dict:
        """尝试把**队首**授予。队首不是我则返回 NO_CAPACITY 带上我的位置。"""
        self._assert_cache_ok()
        head = self.pick_next(pool_id)
        if head is None:
            # 队列空（我可能已被别人授予）
            mine = self.conn.execute(
                "SELECT * FROM queue_entry WHERE queue_entry_id = ?",
                (entry["queue_entry_id"],)).fetchone()
            if mine is not None and mine["state"] == "GRANTED":
                permit = dict(self.conn.execute(
                    "SELECT * FROM permit WHERE permit_id = ?",
                    (mine["granted_permit_id"],)).fetchone())
                return self._acquire_response(permit, replayed=False)
            raise ServiceError(429, "NO_CAPACITY", "no queue entry is waiting",
                               retryable=True,
                               details={"pool_id": pool_id,
                                        "bottleneck": "inflight"})
        if head["queue_entry_id"] != entry["queue_entry_id"]:
            raise ServiceError(
                429, "NO_CAPACITY", "another tenant is ahead in the fair queue",
                retryable=True,
                details={"pool_id": pool_id, "bottleneck": "inflight",
                         "queue_position": self._queue_position(
                             pool_id, entry["queue_entry_id"]),
                         "estimated_wait_ms": None})
        # 队首是我：走与立即授予完全相同的路径。
        result = self._acquire_once(
            tenant_id, {**body, "wait": False},
            deductible_key=body.get("deductible_key") or body["attempt_id"],
            fence=_int(body.get("execution_fence"), "execution_fence", minimum=1),
            correlation_id=correlation_id,
            queue_entry_id=entry["queue_entry_id"])
        # 份额推进在 _acquire_once 里做（立即授予与排队授予同一条路径）。
        result["queue_wait_ms"] = int(
            (_parse(self._now()) - _parse(entry["enqueued_at"])).total_seconds() * 1000)
        return result

    # ------------------------------------------------------------ 队列

    def enqueue(self, tenant_id: str, body: dict, *, correlation_id: str) -> dict:
        pool = self._resolve_pool(tenant_id, body)
        now = self._now()
        entry_id = self._new_id("qe")
        with Transaction(self.conn):
            # guaranteed_share 为 0 的粗粒度"先到先得"用 enqueued_at 兜底。
            self.conn.execute(
                "INSERT INTO tenant_share (tenant_id, pool_id, weight,"
                " virtual_time_micro, inflight_granted, guaranteed_share, updated_at)"
                " VALUES (?, ?, 1, 0, 0, 0, ?) ON CONFLICT DO NOTHING",
                (tenant_id, pool["pool_id"], now))
            self.conn.execute(
                "INSERT INTO queue_entry (queue_entry_id, tenant_id, pool_id,"
                " request_ref, priority, enqueued_at, effective_at,"
                " expected_run_seconds, state, deadline)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'WAITING', ?)",
                (entry_id, tenant_id, pool["pool_id"], body["attempt_id"],
                 _int(body.get("priority", 10), "priority", minimum=0),
                 now, now, int(body.get("expected_run_seconds") or 0),
                 _deadline(body.get("deadline"))))
        return {
            "queue_entry_id": entry_id,
            "pool_id": pool["pool_id"],
            "enqueued_at": now,
            "queue_position": self._queue_position(pool["pool_id"], entry_id),
        }

    def _queue_position(self, pool_id: str, entry_id: str) -> int | None:
        # 排序键全都要：只取 id 会让 _order_queue 在 tenant_id 上 KeyError。
        rows = self.conn.execute(
            "SELECT * FROM queue_entry WHERE pool_id = ?"
            " AND state = 'WAITING'", (pool_id,)).fetchall()
        ordered = self._order_queue([dict(r) for r in rows])
        for i, r in enumerate(ordered, start=1):
            if r["queue_entry_id"] == entry_id:
                return i
        return None

    def _order_queue(self, entries: list[dict]) -> list[dict]:
        """§5.7 的排序，用 Python 实现以便同一口径给队列快照与授予。

        1) 保底份额未用完的租户优先
        2) 虚拟时间最小者优先（WFQ）
        3) aging：等得越久 effective_at 越早
        4) 优先级
        5) 入队时间
        """
        if not entries:
            return []
        shares = {}
        tenants = {e["tenant_id"] for e in entries}
        for tid in tenants:
            row = self.conn.execute(
                "SELECT * FROM tenant_share WHERE tenant_id = ? AND pool_id = ?",
                (tid, entries[0]["pool_id"])).fetchone()
            shares[tid] = dict(row) if row else {
                "weight": 1, "virtual_time_micro": 0, "inflight_granted": 0,
                "guaranteed_share": 0}

        def key(e: dict):
            s = shares.get(e["tenant_id"], {})
            under_share = int(s.get("inflight_granted", 0)
                              < s.get("guaranteed_share", 0))
            # §1.3 / §5.7：`effective_at = enqueued_at - aging_bonus`，是**存下来的列**。
            # 读时再算 bonus 会把"等了多久"和"看的时候多久"混为一谈，
            # 同一份队列快照在两次读取之间可能改变顺序。
            return (
                -under_share,
                s.get("virtual_time_micro", 0),
                e["effective_at"],
                e["priority"],
                e["enqueued_at"],
            )

        return sorted(entries, key=key)

    def pick_next(self, pool_id: str) -> dict | None:
        """§5.7：挑下一个可授予的 WAITING 项，并把过期的转 ABANDONED。"""
        self._abandon_stale(pool_id)
        rows = self.conn.execute(
            "SELECT * FROM queue_entry WHERE pool_id = ? AND state = 'WAITING'",
            (pool_id,)).fetchall()
        entries = [dict(r) for r in rows]
        if not entries:
            return None
        return self._order_queue(entries)[0]

    def _abandon_stale(self, pool_id: str, *, queue_ttl_seconds: float = 600) -> int:
        """T22-38：超 deadline 或 queue_ttl 的 WAITING 转 ABANDONED（不占计数）。"""
        now = self._now()
        rows = self.conn.execute(
            "SELECT queue_entry_id, enqueued_at, deadline FROM queue_entry"
            " WHERE pool_id = ? AND state = 'WAITING'", (pool_id,)).fetchall()
        stale = []
        for r in rows:
            if r["deadline"] and r["deadline"] < now:
                stale.append(r["queue_entry_id"])
            elif (_parse(now) - _parse(r["enqueued_at"])).total_seconds() > queue_ttl_seconds:
                stale.append(r["queue_entry_id"])
        for eid in stale:
            self.conn.execute(
                "UPDATE queue_entry SET state = 'ABANDONED' WHERE queue_entry_id = ?",
                (eid,))
        return len(stale)

    def cancel_tenant_queue(self, tenant_id: str, *, pool_id: str | None = None) -> list[str]:
        """§6.2：撤权时只取消该租户的 WAITING 项；已授予的 permit 不动。"""
        sql = ("SELECT queue_entry_id FROM queue_entry WHERE tenant_id = ?"
               " AND state = 'WAITING'")
        args: list = [tenant_id]
        if pool_id:
            sql += " AND pool_id = ?"
            args.append(pool_id)
        rows = self.conn.execute(sql, args).fetchall()
        ids = [r["queue_entry_id"] for r in rows]
        for eid in ids:
            self.conn.execute(
                "UPDATE queue_entry SET state = 'CANCELLED' WHERE queue_entry_id = ?",
                (eid,))
        return ids

    def _bump_share(self, pool_id: str, tenant_id: str, *,
                    expected_run_seconds: int) -> None:
        row = self.conn.execute(
            "SELECT * FROM tenant_share WHERE tenant_id = ? AND pool_id = ?",
            (tenant_id, pool_id)).fetchone()
        weight = row["weight"] if row else 1
        # 每次授予至少推进一个单位。`expected_run_seconds` 缺省时若按 0 计，
        # 虚拟时间永远不动，队首永远是同一个租户 —— WFQ 退化成绝对优先（T22-36）。
        seconds = max(int(expected_run_seconds or 0), 1)
        bump_micro = int(seconds * 1000000 / max(weight, 1))
        self.conn.execute(
            "INSERT INTO tenant_share (tenant_id, pool_id, weight, virtual_time_micro,"
            " inflight_granted, guaranteed_share, updated_at)"
            " VALUES (?, ?, 1, ?, 1, 0, ?)"
            " ON CONFLICT(tenant_id, pool_id) DO UPDATE SET"
            " virtual_time_micro = virtual_time_micro + ?,"
            " inflight_granted = inflight_granted + 1, updated_at = excluded.updated_at",
            (tenant_id, pool_id, bump_micro, self._now(), bump_micro))

    # ---------------------------------------------------------------- 计数

    def _apply_counters(self, pool: dict, shard_key: str, *, inflight: int,
                        account: int, resource_milli: int, rpm_delta: int,
                        reason: str, permit_id: str | None,
                        tenant_id: str, tenant_scope: str | None = None) -> None:
        """两处计数一起动，并写流水。**绝不用应用算出的绝对值**。"""
        now = self._now()
        if inflight:
            self.conn.execute(
                "UPDATE quota_pool SET inflight_used = inflight_used + ?"
                " WHERE pool_id = ?", (inflight, pool["pool_id"]))
        if resource_milli:
            self.conn.execute(
                "UPDATE quota_pool SET resource_seconds_used_milli ="
                " resource_seconds_used_milli + ? WHERE pool_id = ?",
                (resource_milli, pool["pool_id"]))
        if rpm_delta:
            self.conn.execute(
                "UPDATE quota_pool SET rpm_remaining_window ="
                " MAX(0, rpm_remaining_window + ?) WHERE pool_id = ? AND rpm_limit IS NOT NULL",
                (rpm_delta, pool["pool_id"]))
        self.conn.execute(
            "UPDATE quota_pool SET source_revision = source_revision + 1,"
            " updated_at = ? WHERE pool_id = ?", (now, pool["pool_id"]))
        # 注意：DISPATCH 这一档 delta 全 0（槽在 acquire 时就已经占住），
        # 只推进 source_revision，因为它确实是这个池的一次计数事件。
        if account:
            self.conn.execute(
                "UPDATE pool_shard SET account_used_inflight ="
                " account_used_inflight + ? WHERE shard_key = ?",
                (account, shard_key))
        self.conn.execute(
            "INSERT INTO pool_usage_ledger (pool_id, shard_key, tenant_id,"
            " delta_inflight, delta_account, delta_rpm, delta_resource_milli,"
            " reason, permit_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pool["pool_id"], shard_key, tenant_scope if tenant_scope is not None
             else tenant_id, inflight, account, rpm_delta,
             resource_milli, reason, permit_id, now))

    def reconcile(self) -> dict:
        """§5.5 步骤 3 / §9：permit 聚合 vs 池计数，不一致就以聚合为准修正。

        permit 是本服务的唯一权威；`quota_pool.inflight_used` 只是它的投影。
        """
        findings = []
        pools = [dict(r) for r in self.conn.execute(
            "SELECT * FROM quota_pool").fetchall()]
        with Transaction(self.conn):
            for pool in pools:
                agg = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM permit WHERE pool_id = ?"
                    " AND state IN ('RESERVED','DISPATCH_AUTHORIZED','INFLIGHT')",
                    (pool["pool_id"],)).fetchone()["n"]
                expected = agg if pool["resource_class"] not in \
                    RESOURCE_SECONDS_CLASSES else pool["inflight_used"]
                if expected != pool["inflight_used"]:
                    findings.append({
                        "kind": "inflight_mismatch", "pool_id": pool["pool_id"],
                        "stored": pool["inflight_used"], "aggregate": expected,
                    })
                    self.conn.execute(
                        "UPDATE quota_pool SET inflight_used = ?, updated_at = ?"
                        " WHERE pool_id = ?",
                        (expected, self._now(), pool["pool_id"]))
                    self.conn.execute(
                        "INSERT INTO pool_usage_ledger (pool_id, shard_key, tenant_id,"
                        " delta_inflight, delta_account, delta_rpm,"
                        " delta_resource_milli, reason, permit_id, at)"
                        " VALUES (?, ?, '', ?, 0, 0, 0, 'RECONCILE', NULL, ?)",
                        (pool["pool_id"], self._pool_shard_key(pool),
                         expected - pool["inflight_used"], self._now()))
            shards = [dict(r) for r in self.conn.execute(
                "SELECT * FROM pool_shard").fetchall()]
            for shard in shards:
                agg = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM permit p JOIN quota_pool q"
                    " ON q.pool_id = p.pool_id WHERE p.shard_key = ?"
                    " AND p.state IN ('RESERVED','DISPATCH_AUTHORIZED','INFLIGHT')"
                    " AND q.resource_class NOT IN (?, ?)",
                    (shard["shard_key"], *RESOURCE_SECONDS_CLASSES),
                ).fetchone()["n"]
                if agg != shard["account_used_inflight"]:
                    findings.append({
                        "kind": "account_mismatch",
                        "shard_key": shard["shard_key"],
                        "stored": shard["account_used_inflight"], "aggregate": agg,
                    })
                    self.conn.execute(
                        "UPDATE pool_shard SET account_used_inflight = ?"
                        " WHERE shard_key = ?", (agg, shard["shard_key"]))
        return {"checked_pools": len(pools), "findings": findings,
                "consistent": not findings, "checked_at": self._now()}

    # ---------------------------------------------------- confirm-dispatch

    def confirm_dispatch(self, tenant_id: str, permit_id: str, body: dict, *,
                         actor: dict, correlation_id: str) -> dict:
        """§2.2 / §3.1。两次调用是合法的：RESERVED → DISPATCH_AUTHORIZED → INFLIGHT。"""
        expected = body.get("expected_grant_epoch")
        if expected is not None:
            expected = _int(expected, "expected_grant_epoch", minimum=1)
        dispatch_id = body.get("dispatch_id")

        with Transaction(self.conn):
            permit = self._permit_row(permit_id, tenant_id)
            if permit["state"] in ("RELEASED", "EXPIRED"):
                # T22-20：终态一律不可 dispatch，S26 必须走补偿。
                raise ServiceError(
                    409, "PERMIT_NOT_DISPATCHABLE",
                    "permit is already terminal",
                    details={"permit_id": permit_id, "state": permit["state"],
                             "release_reason": permit["release_reason"]})
            if expected is not None and expected != permit["grant_epoch"]:
                # T22-21：另一个执行器已推进过，本执行器持有的是过期 grant。
                raise ServiceError(
                    409, "GRANT_EPOCH_CONFLICT",
                    "another executor already advanced this permit",
                    retryable=True,
                    details={"permit_id": permit_id,
                             "expected_grant_epoch": expected,
                             "current_grant_epoch": permit["grant_epoch"]})

            now = self._now()
            if permit["state"] == "INFLIGHT":
                # 已推进过：同一个 dispatch_id 视为幂等确认，不同的就是换了一次提交。
                if dispatch_id and permit["dispatch_id"] and \
                        dispatch_id != permit["dispatch_id"]:
                    raise ServiceError(
                        409, "PERMIT_TERMINAL",
                        "permit is already INFLIGHT under a different dispatch_id",
                        details={"permit_id": permit_id,
                                 "dispatch_id": permit["dispatch_id"],
                                 "requested_dispatch_id": dispatch_id})
                return {"permit_id": permit_id, "state": "INFLIGHT",
                        "dispatch_id": permit["dispatch_id"], "inflight_ack": True,
                        "grant_epoch": permit["grant_epoch"], "replayed": True}

            if permit["state"] == "RESERVED":
                self.conn.execute(
                    "UPDATE permit SET state = 'DISPATCH_AUTHORIZED', updated_at = ?"
                    " WHERE permit_id = ?", (now, permit_id))
                nxt = dict(self.conn.execute(
                    "SELECT * FROM permit WHERE permit_id = ?", (permit_id,)).fetchone())
                self._emit_permit(nxt, previous_state="RESERVED",
                                  correlation_id=correlation_id,
                                  causation_id=permit["attempt_id"])
                return {"permit_id": permit_id, "state": "DISPATCH_AUTHORIZED",
                        "dispatch_id": None, "inflight_ack": False,
                        "grant_epoch": nxt["grant_epoch"], "replayed": False}

            # DISPATCH_AUTHORIZED → INFLIGHT。**不动 grant_epoch**：§2.1 定义它是
            # 队列授予序号（acquire 时一次定下，形如 7），§2.2 的两次 confirm-dispatch
            # 都带同一个 epoch —— 前一次是幂等确认，第二次才推进状态。若在这里 +1，
            # S26 用同一份 permit 快照重试的第二次调用就会撞 GRANT_EPOCH_CONFLICT。
            self.conn.execute(
                "UPDATE permit SET state = 'INFLIGHT', dispatch_id = ?,"
                " dispatched_at = ?, updated_at = ? WHERE permit_id = ?",
                (dispatch_id, now, now, permit_id))
            pool = self._pool_row(permit["pool_id"], tenant_id)
            self._apply_counters(pool, permit["shard_key"], inflight=0, account=0,
                                 resource_milli=0, rpm_delta=0, reason="DISPATCH",
                                 permit_id=permit_id, tenant_id=tenant_id)
            nxt = dict(self.conn.execute(
                "SELECT * FROM permit WHERE permit_id = ?", (permit_id,)).fetchone())
            self._emit_permit(nxt, previous_state="DISPATCH_AUTHORIZED",
                              correlation_id=correlation_id,
                              causation_id=permit["attempt_id"])
        return {"permit_id": permit_id, "state": "INFLIGHT",
                "dispatch_id": nxt["dispatch_id"], "inflight_ack": True,
                "grant_epoch": nxt["grant_epoch"], "replayed": False}

    # --------------------------------------------------------------- 释放

    def release(self, tenant_id: str, permit_id: str, body: dict, *, actor: dict,
                correlation_id: str) -> dict:
        """§2.3。**必须带终态证据** —— 没有证据不能释放 INFLIGHT（E16/E21）。"""
        reason = body.get("reason")
        if not reason:
            raise ServiceError(400, "MALFORMED_REQUEST", "reason is required")
        evidence = body.get("terminal_evidence_ref") or body.get("evidence_ref")
        if not evidence:
            # T22-27：422，permit 不变。错误表没有这个码，见 C22-9。
            raise ServiceError(
                422, "TERMINAL_EVIDENCE_REQUIRED",
                "terminal_evidence_ref is required to release a permit",
                details={"permit_id": permit_id, "reason": reason})
        expected = body.get("expected_grant_epoch")
        if expected is not None:
            expected = _int(expected, "expected_grant_epoch", minimum=1)

        with Transaction(self.conn):
            permit = self._permit_row(permit_id, tenant_id)
            if permit["state"] == "RELEASED":
                # T22-28 / T22-29：重复 release（同或不同 command_id）返回首次结果，
                # 绝不重复扣减。
                return self._release_response(permit, inflight_freed=False,
                                              account_freed=False, replayed=True)
            if permit["state"] == "EXPIRED":
                raise ServiceError(
                    409, "PERMIT_TERMINAL", "permit already expired",
                    details={"permit_id": permit_id, "state": "EXPIRED"})
            if expected is not None and expected != permit["grant_epoch"]:
                raise ServiceError(
                    409, "GRANT_EPOCH_CONFLICT",
                    "permit was advanced by another executor",
                    retryable=True,
                    details={"permit_id": permit_id,
                             "expected_grant_epoch": expected,
                             "current_grant_epoch": permit["grant_epoch"]})
            allowed = RELEASE_REASONS_BY_SOURCE.get(permit["state"], ())
            if reason not in allowed:
                raise ServiceError(
                    422, "RELEASE_REASON_NOT_ALLOWED",
                    "reason is not valid for this permit state",
                    details={"permit_id": permit_id, "state": permit["state"],
                             "reason": reason, "allowed": list(allowed)})

            pool = self._pool_row(permit["pool_id"], tenant_id)
            now = self._now()
            self.conn.execute(
                "UPDATE permit SET state = 'RELEASED', release_reason = ?,"
                " released_at = ?, provider_job_ref = COALESCE(?, provider_job_ref),"
                " updated_at = ? WHERE permit_id = ?",
                (reason, now, body.get("provider_job_ref"), now, permit_id))
            # 释放只还并发槽；rpm/tpm 是时间窗配额，释放不退还（它们会自然衰减）。
            participating = pool["resource_class"] not in RESOURCE_SECONDS_CLASSES
            # 退还领取时记下的量，不信调用方这次报的数。
            self.conn.execute(
                "UPDATE permit SET resource_milli_reserved = 0 WHERE permit_id = ?",
                (permit_id,))
            self._apply_counters(
                pool, permit["shard_key"],
                inflight=-1 if participating else 0,
                account=-1 if participating else 0,
                resource_milli=-permit["resource_milli_reserved"],
                rpm_delta=0, reason="RELEASE", permit_id=permit_id,
                tenant_id=tenant_id)
            shard = self._shard_row(permit["shard_key"])
            freed = {"inflight_freed": participating, "account_freed": participating}
            self._bump_share_terminal(permit["pool_id"], tenant_id)
            nxt = dict(self.conn.execute(
                "SELECT * FROM permit WHERE permit_id = ?", (permit_id,)).fetchone())
            self._emit_permit(nxt, previous_state=permit["state"],
                              correlation_id=correlation_id,
                              causation_id=permit["attempt_id"])
        return self._release_response(nxt, replayed=False, **freed)

    def _bump_share_terminal(self, pool_id: str, tenant_id: str) -> None:
        self.conn.execute(
            "UPDATE tenant_share SET inflight_granted ="
            " MAX(0, inflight_granted - 1), updated_at = ?"
            " WHERE tenant_id = ? AND pool_id = ?",
            (self._now(), tenant_id, pool_id))

    # --------------------------------------------------------------- 过期

    def expire(self, permit_id: str, *, actor: dict, correlation_id: str,
               tenant_id: str | None = None) -> dict:
        """§2.4：**只能由 janitor 调用**。只处理 RESERVED。"""
        with Transaction(self.conn):
            row = self.conn.execute(
                "SELECT * FROM permit WHERE permit_id = ?", (permit_id,)).fetchone()
            if row is None:
                raise ServiceError(404, "NOT_FOUND", "permit not found",
                                   details={"permit_id": permit_id})
            permit = dict(row)
            if tenant_id is not None and permit["tenant_id"] != tenant_id:
                raise ServiceError(404, "NOT_FOUND", "permit not found",
                                   details={"permit_id": permit_id})
            if permit["state"] != "RESERVED":
                # T22-31 / T22-32：DISPATCH_AUTHORIZED 与 INFLIGHT 永不自动回收。
                raise ServiceError(
                    409, "PERMIT_NOT_EXPIRABLE",
                    "only RESERVED permits expire on lease",
                    details={"permit_id": permit_id, "state": permit["state"]})
            return self._expire_locked(permit, correlation_id=correlation_id)

    def _expire_locked(self, permit: dict, *, correlation_id: str) -> dict:
        pool = self._pool_row(permit["pool_id"], permit["tenant_id"])
        now = self._now()
        self.conn.execute(
            "UPDATE permit SET state = 'EXPIRED', release_reason = 'NEVER_DISPATCHED',"
            " released_at = ?, updated_at = ? WHERE permit_id = ?",
            (now, now, permit["permit_id"]))
        participating = pool["resource_class"] not in RESOURCE_SECONDS_CLASSES
        self._apply_counters(
            pool, permit["shard_key"], inflight=-1 if participating else 0,
            account=-1 if participating else 0,
            resource_milli=-permit["resource_milli_reserved"], rpm_delta=0,
            reason="EXPIRE", permit_id=permit["permit_id"],
            tenant_id=permit["tenant_id"])
        self.conn.execute(
            "UPDATE permit SET resource_milli_reserved = 0 WHERE permit_id = ?",
            (permit["permit_id"],))
        self._bump_share_terminal(permit["pool_id"], permit["tenant_id"])
        nxt = dict(self.conn.execute(
            "SELECT * FROM permit WHERE permit_id = ?",
            (permit["permit_id"],)).fetchone())
        self._emit_permit(nxt, previous_state="RESERVED",
                          correlation_id=correlation_id,
                          causation_id=permit["attempt_id"])
        return {"permit_id": permit["permit_id"], "state": "EXPIRED",
                "release_reason": "NEVER_DISPATCHED"}

    def sweep_expired(self, *, limit: int = 200,
                      correlation_id: str = "janitor") -> dict:
        """§5.6 janitor。只捞 `RESERVED` 且租约已过期的。

        `SKIP LOCKED` 在 SQLite 上没有对应物；`BEGIN IMMEDIATE` 让多个副本串行，
        第二个进来时 permit 已是 EXPIRED，被 `state != 'RESERVED'` 跳过。
        """
        now = self._now()
        rows = self.conn.execute(
            "SELECT permit_id FROM permit WHERE state = 'RESERVED'"
            " AND lease_expires_at IS NOT NULL AND lease_expires_at < ?"
            " ORDER BY lease_expires_at LIMIT ?", (now, limit)).fetchall()
        expired = []
        for r in rows:
            with Transaction(self.conn):
                row = self.conn.execute(
                    "SELECT * FROM permit WHERE permit_id = ?",
                    (r["permit_id"],)).fetchone()
                if row is None or row["state"] != "RESERVED":
                    continue  # 另一个副本已经处理
                self._expire_locked(dict(row), correlation_id=correlation_id)
                expired.append(r["permit_id"])
        stale_authorized = self.conn.execute(
            "SELECT COUNT(*) AS n FROM permit WHERE state = 'DISPATCH_AUTHORIZED'"
            " AND updated_at < ?", (_plus(now, -1800),)).fetchone()["n"]
        return {"expired": expired, "count": len(expired),
                "authorized_stale_count": stale_authorized,
                "authorized_stale_alerts": stale_authorized > 0}

    # ------------------------------------------------- 执行 fence 取代

    def supersede_fence(self, tenant_id: str, body: dict, *, actor: dict,
                        correlation_id: str) -> dict:
        """T22-26：更新的 fence 出现 → 未 dispatch 的旧 permit 释放；INFLIGHT 不动。"""
        run_id = body.get("run_id")
        fence = _int(body.get("execution_fence"), "execution_fence", minimum=1)
        if not run_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "run_id is required")
        rows = self.conn.execute(
            "SELECT permit_id FROM permit WHERE tenant_id = ? AND run_id = ?"
            " AND execution_fence < ?"
            " AND state IN ('RESERVED','DISPATCH_AUTHORIZED')",
            (tenant_id, run_id, fence)).fetchall()
        released = []
        for r in rows:
            result = self.release(
                tenant_id, r["permit_id"],
                {"reason": "SUPERSEDED_BY_FENCE",
                 "terminal_evidence_ref": body.get("terminal_evidence_ref")
                 or {"service_id": "S22", "id": f"fence:{fence}",
                     "run_id": run_id}},
                actor=actor, correlation_id=correlation_id)
            released.append(result["permit_id"])
        return {"run_id": run_id, "execution_fence": fence,
                "released_count": len(released), "released": released,
                "inflight_untouched": True}

    # ---------------------------------------------------------------- 事件

    def _pool_snapshot(self, pool_id: str) -> dict:
        pool = dict(self.conn.execute(
            "SELECT * FROM quota_pool WHERE pool_id = ?", (pool_id,)).fetchone())
        shard = self._shard_row(self._pool_shard_key(pool))
        return {
            "pool_id": pool_id,
            "inflight_used": pool["inflight_used"],
            "inflight_limit": pool["inflight_limit"],
            "account_used_inflight": shard["account_used_inflight"],
            "account_cap_inflight": shard["account_cap_inflight"],
        }

    def _emit_permit(self, permit: dict, *, previous_state: str | None,
                     correlation_id: str, causation_id: str | None) -> None:
        """§6.1。`data` 必须恰好是契约的 13 个字段（additionalProperties: false）。

        **必须在调用方的事务内调用**：Outbox 与 permit 变更同事务。
        """
        snap = self._pool_snapshot(permit["pool_id"])
        data = {
            "schema_version": "2.0",
            "permit_id": permit["permit_id"],
            "pool_id": permit["pool_id"],
            "attempt_id": permit["attempt_id"],
            "previous_state": previous_state,
            "state": permit["state"],
            "release_reason": permit["release_reason"],
            "dispatch_id": permit["dispatch_id"],
            "execution_fence": permit["execution_fence"],
            "inflight_used": snap["inflight_used"],
            "inflight_limit": snap["inflight_limit"],
            "account_used_inflight": snap["account_used_inflight"],
            "account_cap_inflight": snap["account_cap_inflight"],
        }
        envelope = new_event(
            source=self.source,
            type="scheduler.permit.changed",
            subject=f"permit/{permit['permit_id']}",
            tenant_id=permit["tenant_id"],
            home_cell=self.home_cell,
            aggregate_id=permit["permit_id"],
            sequence=STATE_SEQUENCE[permit["state"]],
            correlation_id=correlation_id,
            causation_id=causation_id or correlation_id,
            data=data,
        )
        self.outbox.append(envelope)

    # ---------------------------------------------------------------- 响应

    def _acquire_response(self, permit: dict, *, replayed: bool) -> dict:
        return {
            "permit_id": permit["permit_id"],
            "pool_id": permit["pool_id"],
            "state": permit["state"],
            "lease_expires_at": permit["lease_expires_at"],
            "grant_epoch": permit["grant_epoch"],
            "queue_wait_ms": 0,
            "replayed": replayed,
        }

    def _release_response(self, permit: dict, *, inflight_freed: bool,
                          account_freed: bool, replayed: bool) -> dict:
        return {
            "permit_id": permit["permit_id"],
            "state": permit["state"],
            "release_reason": permit["release_reason"],
            "inflight_freed": inflight_freed,
            "account_freed": account_freed,
            "replayed": replayed,
        }

    def _permit_row(self, permit_id: str, tenant_id: str) -> dict:
        """跨租户一律 404（T22-39）。"""
        row = self.conn.execute(
            "SELECT * FROM permit WHERE permit_id = ?", (permit_id,)).fetchone()
        if row is None or row["tenant_id"] != tenant_id:
            raise ServiceError(404, "NOT_FOUND", "permit not found",
                               details={"permit_id": permit_id})
        return dict(row)

    # ---------------------------------------------------------------- 查询

    def get_pool(self, tenant_id: str, pool_id: str) -> dict:
        pool = self._pool_row(pool_id, tenant_id)
        shard = self._shard_row(self._pool_shard_key(pool))
        reserved_not_dispatched = self.conn.execute(
            "SELECT COUNT(*) AS n FROM permit WHERE pool_id = ?"
            " AND state IN ('RESERVED','DISPATCH_AUTHORIZED')",
            (pool_id,)).fetchone()["n"]
        oldest = self.conn.execute(
            "SELECT MIN(enqueued_at) AS t FROM queue_entry WHERE pool_id = ?"
            " AND state = 'WAITING'", (pool_id,)).fetchone()["t"]
        oldest_wait_ms = None
        if oldest:
            oldest_wait_ms = int(
                (_parse(self._now()) - _parse(oldest)).total_seconds() * 1000)
        participating = pool["resource_class"] not in RESOURCE_SECONDS_CLASSES
        return {
            "pool_id": pool["pool_id"],
            "state": pool["state"],
            "revision": pool["revision"],
            "source_revision": pool["source_revision"],
            "tenant_scope": pool["tenant_scope"],
            "provider_id": pool["provider_id"],
            "channel_id": pool["channel_id"],
            "account_ref": pool["account_ref"],
            "region": pool["region"],
            "modality": pool["modality"],
            "resource_class": pool["resource_class"],
            "read_at": self._now(),
            "rpm": {"limit": pool["rpm_limit"],
                    "remaining": pool["rpm_remaining_window"],
                    "window_reset_at": pool["window_reset_at"]},
            "tpm": {"limit": pool["tpm_limit"],
                    "remaining": pool["tpm_remaining_window"]},
            "inflight": {"limit": pool["inflight_limit"],
                         "used": pool["inflight_used"],
                         "reserved_not_dispatched": reserved_not_dispatched},
            "account": {"cap_inflight": shard["account_cap_inflight"],
                        "used_inflight": shard["account_used_inflight"],
                        "counted": participating},
            "resource_seconds": {
                "limit": pool["resource_seconds_limit"],
                "used_milli": pool["resource_seconds_used_milli"]},
            "oldest_wait_ms": oldest_wait_ms,
            "avg_service_seconds": self._avg_service_seconds(pool_id),
        }

    def list_pools(self, tenant_id: str, *, provider: str | None = None,
                   region: str | None = None, modality: str | None = None,
                   state: str | None = None) -> list[dict]:
        sql = ("SELECT pool_id FROM quota_pool WHERE"
               " (tenant_scope = 'shared' OR tenant_scope = ?)")
        args: list = [tenant_id]
        for column, value in (("provider_id", provider), ("region", region),
                              ("modality", modality), ("state", state)):
            if value:
                sql += f" AND {column} = ?"
                args.append(value)
        sql += " ORDER BY pool_id"
        return [self.get_pool(tenant_id, r["pool_id"])
                for r in self.conn.execute(sql, args).fetchall()]

    def get_permit(self, tenant_id: str, permit_id: str) -> dict:
        permit = self._permit_row(permit_id, tenant_id)
        return {
            "permit_id": permit["permit_id"],
            "tenant_id": permit["tenant_id"],
            "pool_id": permit["pool_id"],
            "attempt_id": permit["attempt_id"],
            "run_id": permit["run_id"],
            "state": permit["state"],
            "release_reason": permit["release_reason"],
            "grant_epoch": permit["grant_epoch"],
            "execution_fence": permit["execution_fence"],
            "node_id": permit["node_id"],
            "node_epoch": permit["node_epoch"],
            "dispatch_id": permit["dispatch_id"],
            "provider_job_ref": permit["provider_job_ref"],
            "lease_expires_at": permit["lease_expires_at"],
            "dispatched_at": permit["dispatched_at"],
            "released_at": permit["released_at"],
            "reconcile_required_at": permit["reconcile_required_at"],
            "reservation_ref": permit["reservation_ref"],
            "reservation_amount_micro": permit["reservation_amount_micro"],
            "created_at": permit["created_at"],
            "updated_at": permit["updated_at"],
        }

    def list_permits(self, tenant_id: str, *, attempt_id: str | None = None,
                     state: str | None = None, run_id: str | None = None) -> list[dict]:
        sql = "SELECT permit_id FROM permit WHERE tenant_id = ?"
        args: list = [tenant_id]
        for column, value in (("attempt_id", attempt_id), ("state", state),
                              ("run_id", run_id)):
            if value:
                sql += f" AND {column} = ?"
                args.append(value)
        sql += " ORDER BY created_at, permit_id"
        return [self.get_permit(tenant_id, r["permit_id"])
                for r in self.conn.execute(sql, args).fetchall()]

    def get_queue(self, tenant_id: str, pool_id: str, *, state: str = "WAITING",
                  limit: int = 100) -> dict:
        self._pool_row(pool_id, tenant_id)
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM queue_entry WHERE pool_id = ? AND state = ?"
            " ORDER BY enqueued_at LIMIT ?", (pool_id, state, limit)).fetchall()]
        ordered = self._order_queue(rows) if state == "WAITING" else rows
        items = []
        for i, r in enumerate(ordered, start=1):
            # 跨租户的队列快照只给位置与状态，不给别的租户的请求标识。
            mine = r["tenant_id"] == tenant_id
            items.append({
                "queue_entry_id": r["queue_entry_id"] if mine else None,
                "position": i if state == "WAITING" else None,
                "tenant_id": r["tenant_id"] if mine else "other",
                "priority": r["priority"],
                "enqueued_at": r["enqueued_at"],
                "expected_run_seconds": r["expected_run_seconds"],
                "state": r["state"],
                "granted_permit_id": r["granted_permit_id"] if mine else None,
                "deadline": r["deadline"],
            })
        return {"pool_id": pool_id, "state": state, "items": items,
                "count": len(items)}

    def get_share(self, tenant_id: str, *, pool_id: str) -> dict:
        self._pool_row(pool_id, tenant_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_share WHERE tenant_id = ? AND pool_id = ?",
            (tenant_id, pool_id)).fetchone()
        if row is None:
            return {"tenant_id": tenant_id, "pool_id": pool_id, "weight": 1,
                    "virtual_time_micro": 0, "inflight_granted": 0,
                    "guaranteed_share": 0}
        return {"tenant_id": tenant_id, "pool_id": pool_id,
                "weight": row["weight"],
                "virtual_time_micro": row["virtual_time_micro"],
                "inflight_granted": row["inflight_granted"],
                "guaranteed_share": row["guaranteed_share"]}

    def get_capacity(self, tenant_id: str, *, provider: str | None = None,
                     region: str | None = None) -> dict:
        """给 S47 投影用的汇总。**必须带水位**（docs/04 §5）。"""
        pools = self.list_pools(tenant_id, provider=provider, region=region)
        items = [{
            "pool_id": p["pool_id"],
            "state": p["state"],
            "provider_id": p["provider_id"],
            "region": p["region"],
            "modality": p["modality"],
            "inflight_used": p["inflight"]["used"],
            "inflight_limit": p["inflight"]["limit"],
            "account_used_inflight": p["account"]["used_inflight"],
            "account_cap_inflight": p["account"]["cap_inflight"],
            "oldest_wait_ms": p["oldest_wait_ms"],
        } for p in pools]
        watermark = max((p["read_at"] for p in pools), default=None)
        return {"items": items, "count": len(items),
                "generated_at": self._now(), "watermark": watermark or self._now()}

    # ---------------------------------------------------------------- 消费

    def consume_model_capability_changed(self, event: dict, *,
                                         correlation_id: str) -> dict:
        """§6.2 / §6.3：池定义版本单调，旧 revision 直接丢弃（T22-50）。"""
        if not self.inbox.consume("s22.model", event.get("id") or ""):
            return {"applied": False, "reason": "duplicate"}
        data = event.get("data") or {}
        pool_id = data.get("pool_id")
        revision = data.get("revision")
        if not pool_id or revision is None:
            return {"applied": False, "reason": "missing pool_id or revision"}
        row = self.conn.execute(
            "SELECT * FROM quota_pool WHERE pool_id = ?", (pool_id,)).fetchone()
        if row is None or int(revision) <= row["revision"]:
            return {"applied": False, "reason": "stale_or_unknown_pool",
                    "pool_revision": row["revision"] if row else None}
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE quota_pool SET inflight_limit = COALESCE(?, inflight_limit),"
                " rpm_limit = COALESCE(?, rpm_limit), tpm_limit = COALESCE(?, tpm_limit),"
                " state = COALESCE(?, state), revision = ?, updated_at = ?"
                " WHERE pool_id = ?",
                (data.get("inflight_limit"), data.get("rpm_limit"),
                 data.get("tpm_limit"), data.get("state"), int(revision),
                 self._now(), pool_id))
        return {"applied": True, "pool_id": pool_id, "revision": int(revision)}

    def consume_access_revoked(self, event: dict, *, correlation_id: str) -> dict:
        """§6.2：只取消 WAITING 项；已授予的 permit 不动。"""
        if not self.inbox.consume("s22.access", event.get("id") or ""):
            return {"applied": False, "reason": "duplicate"}
        data = event.get("data") or {}
        tenant_id = data.get("tenant_id") or event.get("tenant_id")
        cancelled: list[str] = []
        if tenant_id:
            with Transaction(self.conn):
                cancelled = self.cancel_tenant_queue(tenant_id)
        return {"applied": True, "tenant_id": tenant_id,
                "cancelled_count": len(cancelled), "cancelled": cancelled}

    def consume_job_state_changed(self, event: dict, *,
                                  correlation_id: str) -> dict:
        """§6.2：**绝不推进 permit**。只在 SUBMISSION_UNKNOWN 时打一个审计标记。"""
        if not self.inbox.consume("s22.job", event.get("id") or ""):
            return {"applied": False, "reason": "duplicate"}
        data = event.get("data") or {}
        attempt_id = data.get("attempt_id")
        state = data.get("state")
        if not attempt_id or state != "SUBMISSION_UNKNOWN":
            return {"applied": True, "marked": []}
        rows = self.conn.execute(
            "SELECT permit_id FROM permit WHERE attempt_id = ?"
            " AND state = 'INFLIGHT'", (attempt_id,)).fetchall()
        marked = [r["permit_id"] for r in rows]
        with Transaction(self.conn):
            for permit_id in marked:
                self.conn.execute(
                    "UPDATE permit SET reconcile_required_at = ?, updated_at = ?"
                    " WHERE permit_id = ? AND state = 'INFLIGHT'",
                    (self._now(), self._now(), permit_id))
        return {"applied": True, "marked": marked, "state_changed": False}

    def consume_observation_received(self, event: dict, *,
                                     correlation_id: str) -> dict:
        """§6.2：只回填 `provider_job_ref`，不改 state。"""
        if not self.inbox.consume("s22.observation", event.get("id") or ""):
            return {"applied": False, "reason": "duplicate"}
        data = event.get("data") or {}
        attempt_id = data.get("attempt_id")
        job_ref = data.get("provider_job_ref") or data.get("provider_job_id")
        if not attempt_id or not job_ref:
            return {"applied": False, "reason": "missing attempt_id or job ref"}
        rows = self.conn.execute(
            "SELECT permit_id FROM permit WHERE attempt_id = ?"
            " AND provider_job_ref IS NULL", (attempt_id,)).fetchall()
        filled = [r["permit_id"] for r in rows]
        with Transaction(self.conn):
            for permit_id in filled:
                self.conn.execute(
                    "UPDATE permit SET provider_job_ref = ?, updated_at = ?"
                    " WHERE permit_id = ? AND provider_job_ref IS NULL",
                    (job_ref, self._now(), permit_id))
        return {"applied": True, "filled": filled}
