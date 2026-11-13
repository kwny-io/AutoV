"""S46 开发者集成：内核。

四条不变量，后面的每段代码都要能对上：

1. **S46 不是任何业务事实的权威。** 没有 `run_status`、没有 `budget_balance`、
   没有 `approval_state`。`integration_command_receipt.state` 是**回执**状态
   （受理了没有、转发到哪一步），`upstream_state` 只是「上一次快照」，必须带
   `upstream_state_as_of` 一起读（方案 §1.2、§4.2）。`GET /operations/{id}`
   每次都回源上游，不缓存超过 5s。

2. **`execution_fence` 与 `node_epoch` 不在这里。** 两者都是上游的概念：
   fence 的作用域是 run/branch，epoch 是单个 `logical_node`。S46 转发时**原样
   透传**调用方在 `context_ref` 里给的值，从不生成、从不推进、从不拿旧值
   作废任何东西——S46 没有接受指针可推进（方案 §10.3、AGENTS.md）。
   本模块的任何代码都不该出现 "fence" 或 "epoch" 的写路径。

3. **`FORWARD_UNKNOWN` 绝不自动重提。** 上游可能是 S21（会走到 S26 付费），
   盲目重提就是重复付费（AGENTS.md 第 19 行）。`502 UPSTREAM_UNAVAILABLE`
   （确定未执行）才进 `FORWARD_RETRYING`；`503 UPSTREAM_UNKNOWN`（可能已经
   发生）进 `FORWARD_UNKNOWN`，只等对账。

4. **`tenant_id` 只来自认证产物。** 外部 HTTP body 里没有、也不接受
   `tenant_id`（出现即 `MALFORMED_REQUEST`）；MCP 参数里若出现，**覆盖**为
   认证值并记 `integration.tenant_param_ignored`，不报错（§1.3 第 3 条）。

并发模型：SQLite 的 `BEGIN IMMEDIATE` 是全库写锁，严格强于方案要求的
「锁 receipt 一行」。命令幂等靠 `icr_cmd_uniq` 唯一约束兜底——**不依赖先查
后插**（§6.2）。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from event_sdk import Inbox, Outbox, new_event

from .redaction import CLASSES, redact_payload
from .schema import (
    CONSUMER_RECONCILE,
    CONSUMER_WEBHOOK,
    DELIVERY_LEASE_SECONDS,
    DELIVERY_STATES,
    ENDPOINT_FAMILIES,
    EVENT_COMMAND_ACCEPTED,
    EVENT_DATA_KEYS,
    EVENT_FORWARD_UNKNOWN,
    EVENT_TENANT_PARAM_IGNORED,
    FORWARD_BACKOFF_SECONDS,
    ID_PREFIX,
    INTERNAL_KINDS,
    KEY_RETIRE_DAYS,
    MAX_FORWARD_ATTEMPTS,
    MAX_KEYS_ACTIVE,
    MAX_KEYS_LIVE,
    MAX_WEBHOOK_ATTEMPTS,
    MAX_WEBHOOK_SUBSCRIPTIONS,
    PLANS,
    PRINCIPAL_TYPES,
    QUOTA_DEFAULTS,
    RECEIPT_STATES,
    RECONCILE_WINDOW_SECONDS,
    SCHEMA,
    SCHEMA_VERSION,
    SCOPES,
    SOURCE,
    TOPIC,
    UPSTREAM_READ_TTL_SECONDS,
    WEBHOOK_BACKOFF_SECONDS,
    WEBHOOK_STATES,
)

# §4.6 的错误码。外部客户会写 `if (code === 'X')`，所以它们是常量。
MALFORMED_REQUEST = "MALFORMED_REQUEST"
REQUEST_HASH_MISMATCH = "REQUEST_HASH_MISMATCH"
UNAUTHENTICATED = "UNAUTHENTICATED"
CREDENTIAL_EXPIRED = "CREDENTIAL_EXPIRED"
TENANT_MISMATCH = "TENANT_MISMATCH"
INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
COMMAND_NOT_SUPPORTED = "COMMAND_NOT_SUPPORTED"
NOT_FOUND = "NOT_FOUND"
IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
COMMAND_IN_PROGRESS = "COMMAND_IN_PROGRESS"
KEY_LIMIT_REACHED = "KEY_LIMIT_REACHED"
EXTERNAL_ID_CONFLICT = "EXTERNAL_ID_CONFLICT"
COMMAND_NOT_FEASIBLE = "COMMAND_NOT_FEASIBLE"
TENANT_RATE_LIMITED = "TENANT_RATE_LIMITED"
SCOPE_CONCURRENCY_LIMITED = "SCOPE_CONCURRENCY_LIMITED"
ENDPOINT_FAMILY_LIMITED = "ENDPOINT_FAMILY_LIMITED"
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
UPSTREAM_UNKNOWN = "UPSTREAM_UNKNOWN"
UPSTREAM_AUTH_FAILED = "UPSTREAM_AUTH_FAILED"
POLICY_DENIED = "POLICY_DENIED"
SSRF_BLOCKED = "SSRF_BLOCKED"
WEBHOOK_LIMIT_REACHED = "WEBHOOK_LIMIT_REACHED"
INVALID_URL = "INVALID_URL"

# 外部 HTTP 命令体允许的键。**没有 `tenant_id`、没有 `actor`**（§2.2）。
COMMAND_BODY_KEYS = ("command_id", "request_hash", "command", "target",
                     "params", "context_ref", "callback", "client_trace_id",
                     "external_id")

# `request_hash` 排除的字段（§6.1）。`client_trace_id` 是观测字段，
# 客户改它不该导致 409。
HASH_EXCLUDED_KEYS = ("command_id", "request_hash", "client_trace_id")

# HTTP 层给幂等 SDK 算键时排除的字段，必须与 `request_hash()` 用同一组。
# 两处不一致会让「客户端重试 -> 409」和「回执重放 -> 200」给出不同答案。
COMMAND_IN_BODY_KEYS_ABSENT_FROM_HASH = HASH_EXCLUDED_KEYS

# MCP 参数里即使出现也一律覆盖的租户字段（§1.3 第 3 条）。
TENANT_PARAM_KEYS = ("tenant_id", "tenant", "tenantId", "tid")


# ------------------------------------------------------------------ 基础工具

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _loads(text, default=None):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _dumps(value) -> str:
    return _jcs(value)


def _parse_ts(text: str | None) -> datetime | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _fmt_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


class ServiceError(Exception):
    """带状态码的领域错误。`status` 决定 HTTP 层，`code` 决定调用方分支。"""

    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` 起手，异常即回滚。

    `BEGIN IMMEDIATE` 立刻拿写锁，把「先读后写」的竞态挡在锁外。不要改成
    默认的 deferred：那会在第一次写时才拿锁，两个并发受理都能通过幂等预检。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------------ 上游端口

class UpstreamUnavailable(ConnectionError):
    """上游不可达 / 429 / 5xx。**确定未执行**（或至少没有证据说执行了）→ 可重试。"""


class UpstreamUnknown(ConnectionError):
    """上游结果无法判定（超时在提交之后、网关 504、503 无 body）。

    **可能已经发生**。禁止自动重提，只能进 `FORWARD_UNKNOWN` 等对账。
    """


@dataclass
class UpstreamCall:
    service_id: str
    action: str
    tenant_id: str
    target: dict
    params: dict
    execution_context: dict
    idempotency_key: str
    client_trace_id: str | None


class UpstreamPort:
    """转发上游的端口。生产实现是 HTTP 适配器，测试用桩。

    三个必须由适配器保证的语义：

    1. **一律走上游自己的对外端点**，不直连数据库（§10.3）。
    2. **`Idempotency-Key` 用 `receipt.command_id`**，让上游按自己的幂等记录
       去重。这是「转发成功、写回执前崩溃」这一固有窗口的唯一缓解（§7.5）。
    3. **三种失败必须分开抛**：`UpstreamUnavailable`（未执行、可重试）、
       `UpstreamUnknown`（未知、不可重试）、`ServiceError(4xx)`（确定未执行）。
       混成一个 5xx 是这类系统最常见的重复付费来源（§7.2 末段）。
    """

    def call(self, request: UpstreamCall) -> dict:
        raise NotImplementedError

    def probe(self, *, tenant_id: str, service_id: str,
              upstream_ref: dict | None, command_id: str) -> str:
        """对账回问「这条业务命令存在吗」：`executed` / `not_executed` / `unknown`。

        未配置时返回 `unknown`——**绝不猜成 not_executed 然后重提**。
        """
        return "unknown"

    def state(self, *, tenant_id: str, service_id: str,
              upstream_ref: dict | None) -> dict | None:
        """读上游当前状态快照：`{state, state_as_of, projection_watermark}`。"""
        return None


class UnconfiguredUpstream(UpstreamPort):
    """没有配置上游时：转发一律 503 UPSTREAM_UNKNOWN，且**不说「没发生」**。"""

    def call(self, request: UpstreamCall) -> dict:
        raise UpstreamUnknown(
            f"no adapter configured for {request.service_id}; "
            f"the outcome of {request.action} is unknown")


# ------------------------------------------------------------------ 限流

class RateLimitBackendError(Exception):
    """Redis 不可用。**只对限流 fail-open**（§3.6），授权仍 fail-closed。"""


class LocalTokenBuckets:
    """进程内固定窗口计数器（§6.5）。

    生产是 Redis（`INCR` + `EXPIRE`）；这里保留同样的可观察语义：
    窗口边界突发是允许的，这不是 bug 而是取舍（§6.5 第 3 条）。
    """

    def __init__(self):
        self._windows: dict[str, tuple[int, int]] = {}
        self._inflight: dict[str, int] = {}
        self._lock = threading.Lock()

    def try_consume(self, key: str, limit: int,
                    window_seconds: int) -> tuple[bool, int, int]:
        now = int(time.time())
        start = now - (now % window_seconds)
        with self._lock:
            stamp, count = self._windows.get(key, (start, 0))
            if stamp != start:
                stamp, count = start, 0
            count += 1
            self._windows[key] = (stamp, count)
        remaining = max(0, limit - count)
        reset_after = start + window_seconds - now
        return count <= limit, remaining, max(1, reset_after)

    def try_enter(self, key: str, limit: int) -> bool:
        with self._lock:
            current = self._inflight.get(key, 0)
            if current >= limit:
                return False
            self._inflight[key] = current + 1
            return True

    def leave(self, key: str) -> None:
        with self._lock:
            current = self._inflight.get(key, 0)
            if current <= 1:
                self._inflight.pop(key, None)
            else:
                self._inflight[key] = current - 1


# ------------------------------------------------------------------ 命令目录

@dataclass(frozen=True)
class Command:
    """一条外部可见命令。目录是**代码常量**，不是数据库配置（§2.3 末段）。

    `requires_admission` 不是装饰：它是「`read` scope 永远不能触发付费」的
    执行点。判定时看这个标记，不看命令名——新增一条付费命令忘了改 scope
    也拦得住（§2.3 第 130 行）。
    """

    name: str
    upstream_service: str
    action: str
    required_scope: str
    target_kinds: tuple[str, ...]
    requires_admission: bool = False
    billed: bool = False
    # S40 的人工放行**由上游实时复查**，S46 不判定、不缓存（AGENTS.md 第 21 行）。
    upstream_human_gate: bool = False
    description: str = ""


COMMANDS: dict[str, Command] = {c.name: c for c in (
    Command("episode.generate", "S21", "runs.create", "execute",
            ("episode", "project"), requires_admission=True, billed=True,
            description="创建一次生成 run；经 S21 → S26 准入。"),
    Command("run.get", "S21", "runs.get", "read", ("run",),
            description="读 run 状态。只读，永不计费。"),
    Command("run.command", "S21", "runs.command", "execute", ("run",),
            description="pause/cancel/resume；不新增付费，但会触发结算。"),
    Command("change.plan", "S07", "change_plans.create", "plan",
            ("project", "episode"), description="创建变更计划。"),
    Command("change.apply", "S07", "change_plans.apply", "execute",
            ("project",), requires_admission=True, billed=True,
            upstream_human_gate=True,
            description="应用变更计划；人工放行由 S40 实时复查。"),
    Command("review.decide", "S40", "decisions.create", "review",
            ("decision",), description="提交评审决定。"),
    Command("delivery.prepare", "S43", "delivery.prepare", "execute",
            ("episode",), description="准备交付包。"),
    Command("delivery.publish", "S43", "delivery.publish", "execute",
            ("episode",), upstream_human_gate=True,
            description="发布交付包；仍要 S40 实时复查（§13）。"),
    Command("memory.query", "S16", "memory.query", "read", ("project",),
            description="查询叙事记忆。"),
    Command("context.preview", "S18", "context.preview", "read",
            ("episode",), description="预览上下文装配。"),
    Command("policy.get", "S02", "policy.get", "read", ("project",),
            description="读策略。"),
    Command("cost.query", "S23", "cost.query", "read", ("project",),
            description="查询费用；S23/S44 的只读面。"),
)}


# ------------------------------------------------------------------ 数据类

@dataclass(frozen=True)
class AuthContext:
    """`verify(credential)` 的产物。**唯一合法的 `tenant_id` 来源**（§1.3）。

    `tenant_id` 只能来自 `api_key` 行的绑定（或 S01 的 JWT `tid`），两者都
    不可由调用方构造。
    """

    tenant_id: str
    api_client_id: str
    kid: str
    principal_id: str
    principal_type: str
    scopes: frozenset[str]
    plan: str
    home_region: str
    home_cell: str
    kid_prefix: str
    delegation_id: str | None = None
    source_revision: int | None = None

    def has(self, scope: str) -> bool:
        # `admin` 隐含其它 scope 吗？不。方案没有这条规则，不自行发明。
        return scope in self.scopes


@dataclass
class ForwardOutcome:
    """一次转发的分类结果。"""

    state: str
    status: int = 202
    code: str = ""
    message: str = ""
    retryable: bool = False
    upstream_ref: dict | None = None
    upstream_state: str | None = None
    upstream_state_as_of: str | None = None
    upstream_watermark: int | None = None
    rejection: dict | None = None


# ------------------------------------------------------------------ 服务

class DeveloperIntegrationService:
    def __init__(self, conn: sqlite3.Connection, *, upstream: UpstreamPort | None = None,
                 authorization_source=None, rate_limiter=None,
                 webhook_sender=None, resolver=None,
                 redaction_secret: bytes = b"s46-local-redaction-key",
                 secret_key: bytes = b"s46-local-secret-key",
                 context_key: bytes = b"s46-local-context-key",
                 clock=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 隐式提交当前事务。只能在事务外建一次，之后复用同一实例。
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)
        self.upstream = upstream or UnconfiguredUpstream()
        # S01 的授权上下文：可选。未配置时只用本服务自己的 scope 凭证；
        # 配置了就 **fail closed**（不可达 → 503，绝不放行）。
        self.authorization_source = authorization_source
        self.rate_limiter = rate_limiter or LocalTokenBuckets()
        self.webhook_sender = webhook_sender
        # SSRF 校验用的解析器。测试必须注入，绝不真的查 DNS。
        self.resolver = resolver
        self.redaction_secret = redaction_secret
        self.secret_key = secret_key
        self.context_key = context_key
        self.clock = clock or _now_dt
        self.metrics: dict[str, int] = {}
        # 只存在于一个进程的生命周期里：见 `forward()` 的 docstring。
        # 它**故意**不落库——params 正文不驻留（E31）。
        self._pending: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------ 通用
    def _bump(self, name: str, amount: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def _now(self) -> str:
        moment = self.clock()
        if isinstance(moment, datetime):
            return _fmt_ts(moment)
        return _now()

    def _now_dt(self) -> datetime:
        moment = self.clock()
        return moment if isinstance(moment, datetime) else _now_dt()

    def _upstream(self, call, code: str = "DEPENDENCY_UNAVAILABLE"):
        """上游不可达 → 503。**绝不拿缓存的结论兜底**（§7.1）。"""
        try:
            return call()
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(503, code, str(exc), retryable=True,
                                   details=exc.details) from exc
            raise

    # ------------------------------------------------------------ 认证
    def verify_credential(self, raw_key: str) -> AuthContext:
        """API key 的哈希查表。**不读请求体**（§2.1）。

        `RETIRING` 的 key 在 `retire_at` 之前仍然可用——这正是零停机轮换的
        意义（§3.4 第 4 条）。到期即自动 `REVOKED`。
        """
        if not raw_key:
            raise ServiceError(401, UNAUTHENTICATED, "no credential supplied")
        row = _row(self.conn.execute(
            "SELECT * FROM api_key WHERE secret_hash=?",
            (_sha256_hex(raw_key),)).fetchone())
        if row is None:
            raise ServiceError(401, UNAUTHENTICATED, "credential is not valid")
        if row["state"] == "REVOKED":
            raise ServiceError(401, UNAUTHENTICATED, "credential is not valid")
        if row["state"] == "RETIRING":
            retire_at = _parse_ts(row["retire_at"])
            if retire_at is not None and retire_at <= self._now_dt():
                self._revoke_key(row["tenant_id"], row["kid"],
                                 reason="retire_at_elapsed")
                raise ServiceError(401, CREDENTIAL_EXPIRED,
                                   "credential has expired; issue a new key")
        client = _row(self.conn.execute(
            "SELECT * FROM api_client WHERE tenant_id=? AND api_client_id=?",
            (row["tenant_id"], row["api_client_id"])).fetchone())
        if client is None or client["state"] != "ACTIVE":
            raise ServiceError(401, UNAUTHENTICATED, "credential is not valid")

        self.conn.execute(
            "UPDATE api_key SET last_used_at=? WHERE tenant_id=? AND kid=?",
            (self._now(), row["tenant_id"], row["kid"]))

        scopes = frozenset(
            r["scope"] for r in self.conn.execute(
                "SELECT scope FROM client_scope_grant WHERE api_client_id=?"
                " AND revoked_at IS NULL"
                " AND (expires_at IS NULL OR expires_at > ?)",
                (row["api_client_id"], self._now())).fetchall())
        auth = AuthContext(
            tenant_id=row["tenant_id"],
            api_client_id=row["api_client_id"],
            kid=row["kid"],
            principal_id=client["principal_id"] or row["api_client_id"],
            principal_type=client["principal_type"],
            scopes=scopes,
            plan=client["plan"],
            home_region=client["home_region"],
            home_cell=client["home_cell"],
            kid_prefix=row["kid_prefix"],
            delegation_id=None,
        )
        return self._recheck_authorization(auth)

    def _recheck_authorization(self, auth: AuthContext) -> AuthContext:
        """回源 S01 的授权上下文（§10.1）。

        未配置时返回原值——本服务自己的 `client_scope_grant` 是它持有的授权
        凭证（§1.1）。配置了就**必须**成功：授权不可用不能放行（fail closed，
        与限流相反）。
        """
        if self.authorization_source is None:
            return auth
        context = self._upstream(
            lambda: self.authorization_source(
                tenant_id=auth.tenant_id,
                principal_id=auth.principal_id,
                principal_type=auth.principal_type),
            code="AUTHORIZATION_UNAVAILABLE")
        if not isinstance(context, dict):
            raise ServiceError(503, "AUTHORIZATION_UNAVAILABLE",
                               "authorization source returned no context",
                               retryable=True)
        if context.get("kill_switch") or (
                context.get("tenant_state") not in (None, "ACTIVE")):
            raise ServiceError(403, POLICY_DENIED,
                               "tenant is not permitted to make calls")
        # 取交集，不是并集：S01 撤掉的 scope 必须立刻失效（T46-06）。
        live = frozenset(context.get("scopes") or ())
        return AuthContext(
            tenant_id=auth.tenant_id, api_client_id=auth.api_client_id,
            kid=auth.kid, principal_id=auth.principal_id,
            principal_type=auth.principal_type,
            scopes=auth.scopes & live, plan=auth.plan,
            home_region=auth.home_region, home_cell=auth.home_cell,
            kid_prefix=auth.kid_prefix, delegation_id=auth.delegation_id,
            source_revision=context.get("source_revision"))

    # ------------------------------------------------------------ 限流
    def _quota(self, tenant_id: str, plan: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM tenant_quota_profile WHERE tenant_id=?",
            (tenant_id,)).fetchone())
        if row is not None:
            return row
        defaults = QUOTA_DEFAULTS.get(plan or "standard",
                                      QUOTA_DEFAULTS["standard"])
        return {"plan": plan or "standard", **defaults}

    def _rate_limit(self, auth: AuthContext, family: str,
                    scope: str | None) -> dict:
        """三维令牌桶（§3.6）。Redis 不可用 → **放行** + 告警（§7.1）。

        限流 fail-open、授权 fail-closed，方向相反是故意的：前者是可用性
        保护，后者是安全边界。
        """
        quota = self._quota(auth.tenant_id, auth.plan)
        try:
            allowed, remaining, reset = self.rate_limiter.try_consume(
                f"tenant:{auth.tenant_id}", int(quota["qps_total"]), 1)
            if not allowed:
                self._bump("s46_rate_limited_total")
                raise ServiceError(
                    429, TENANT_RATE_LIMITED,
                    "tenant request rate exceeded", retryable=True,
                    details={"retry_after": reset, "limit": quota["qps_total"],
                             "remaining": remaining, "reset": reset,
                             "dimension": "tenant"})
            family_limit = int(quota[f"qps_{family}"])
            allowed, remaining, reset = self.rate_limiter.try_consume(
                f"family:{auth.tenant_id}:{family}", family_limit, 1)
            if not allowed:
                self._bump("s46_rate_limited_total")
                raise ServiceError(
                    429, ENDPOINT_FAMILY_LIMITED,
                    f"endpoint family {family} rate exceeded", retryable=True,
                    details={"retry_after": reset, "limit": family_limit,
                             "remaining": remaining, "reset": reset,
                             "dimension": "family", "family": family})
        except RateLimitBackendError:
            # 只在这里吞：放行的后果是上游多扛一点，真正的付费闸门在 S22/S23。
            self._bump("s46_rate_limit_degraded_total")
            self._bump("rate_limit_degraded")
            remaining, reset = -1, 1
        return {"remaining": remaining, "reset": reset}

    def _enter_concurrency(self, auth: AuthContext, scope: str) -> str | None:
        """`execute` 的长任务并发（§3.6 第二维）。返回需要释放的 key。"""
        if scope not in ("execute", "plan", "read"):
            return None
        quota = self._quota(auth.tenant_id, auth.plan)
        limit = int(quota[f"concurrency_{scope}"])
        key = f"scope:{auth.tenant_id}:{scope}"
        try:
            if not self.rate_limiter.try_enter(key, limit):
                self._bump("s46_rate_limited_total")
                raise ServiceError(
                    429, SCOPE_CONCURRENCY_LIMITED,
                    f"{scope} concurrency limit reached", retryable=True,
                    details={"limit": limit, "dimension": "scope",
                             "scope": scope})
        except RateLimitBackendError:
            self._bump("s46_rate_limit_degraded_total")
            return None
        return key

    def _leave_concurrency(self, key: str | None) -> None:
        if key is None:
            return
        try:
            self.rate_limiter.leave(key)
        except RateLimitBackendError:
            self._bump("s46_rate_limit_degraded_total")

    # ------------------------------------------------------------ 合规模块
    def _check_body(self, body) -> dict:
        """外部命令体的结构校验。**这里拒 `tenant_id`**（§2.2 硬规则）。"""
        if not isinstance(body, dict):
            raise ServiceError(400, MALFORMED_REQUEST, "body must be a JSON object")
        unknown = sorted(set(body) - set(COMMAND_BODY_KEYS))
        if unknown:
            raise ServiceError(
                400, MALFORMED_REQUEST,
                f"unknown field(s): {', '.join(unknown)}",
                details={"unknown_fields": unknown,
                         "allowed_fields": list(COMMAND_BODY_KEYS)})
        command = body.get("command")
        if not isinstance(command, str) or not command:
            raise ServiceError(400, MALFORMED_REQUEST, "command is required")
        target = body.get("target")
        if not isinstance(target, dict) or not target:
            raise ServiceError(400, MALFORMED_REQUEST,
                               "target must be a non-empty object")
        params = body.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ServiceError(400, MALFORMED_REQUEST, "params must be an object")
        return {**body, "target": target, "params": params}

    @staticmethod
    def request_hash(body: dict) -> str:
        """§6.1：`sha256(JCS(body 去掉 command_id、request_hash、client_trace_id))`。

        对**收到的 body** 计算，不对脱敏后的副本计算：`params_sha256` 才是脱敏
        后的摘要，两者用途不同。若两者混用，命中密钥的合法重试会变成
        `REQUEST_HASH_MISMATCH`——方案 §2.2 明写这个顺序要求针对的是入库与
        转发，不是幂等键。
        """
        candidate = {k: v for k, v in body.items() if k not in HASH_EXCLUDED_KEYS}
        return _sha256_hex(_jcs(candidate))

    def _scrub_tenant_params(self, tenant_id: str, params: dict,
                             target: dict) -> tuple[dict, dict, list[dict]]:
        """MCP 路径：把 LLM 给的 `tenant_id` 覆盖为认证值（§1.3 第 3 条）。

        不报错。报错会诱使调用方去「修参数」；覆盖则让误用无害。原始值作为
        **证据**记进审计，不作为权威。
        """
        evidence: list[dict] = []
        cleaned = dict(params)
        for key in TENANT_PARAM_KEYS:
            if key in cleaned:
                evidence.append({"field": f"params.{key}",
                                 "supplied": cleaned[key]})
                cleaned[key] = tenant_id
        for key in TENANT_PARAM_KEYS:
            if key in target:
                evidence.append({"field": f"target.{key}",
                                 "supplied": target[key]})
                target = {**target, key: tenant_id}
        if evidence:
            self._bump("s46_tenant_param_ignored_total")
        return cleaned, target, evidence

    def _redact(self, tenant_id: str, params: dict) -> tuple[dict, dict[str, int]]:
        """`INGRESS_SECRET_SCAN`。命中从不 4xx（§2.2）。"""
        cleaned, counts = redact_payload(params, tenant_id,
                                         secret=self.redaction_secret)
        for class_id, count in counts.items():
            self._bump(f"s46_redactions_total{{{class_id}}}", count)
        return cleaned, counts

    # ------------------------------------------------------------ 执行上下文
    def build_execution_context(self, auth: AuthContext, *,
                                command_id: str, request_hash: str,
                                target: dict, context_ref: dict,
                                client_trace_id: str | None) -> dict:
        """平台签名的 `execution_context`（§1.3 第 4 条、§10.3）。

        含 `command_id` / `request_hash`（与外部命令相同）与来自 `AuthContext`
        的 `actor`。三处「不可信」在这里被结构性地切掉：

        - `tenant_id` 来自 `auth`，不来自 body；
        - `actor` 来自 `auth`，不来自 body；
        - `home_region` / `home_cell` 来自 `api_client` 行，不来自 header
          （T46-50：外部请求只能命中自己 `home_region` 的服务）。

        `execution_fence` / `node_epoch` 只**原样透传** `context_ref` 里调用方
        给的值；S46 不生成、不推进（AGENTS.md：fence 属 run/branch，epoch 属
        单个 logical_node）。
        """
        ref = context_ref or {}
        context = {
            "schema_version": SCHEMA_VERSION,
            "tenant_id": auth.tenant_id,
            "project_id": target.get("project_id") or ref.get("project_id", ""),
            "series_id": ref.get("series_id", ""),
            "branch_id": ref.get("branch_id", ""),
            "episode_id": target.get("episode_id") or ref.get("episode_id", ""),
            "home_region": auth.home_region,
            "home_cell": auth.home_cell,
            "manifest_ref": ref.get("manifest_ref") or {
                "id": "", "revision": 1, "content_hash": "0" * 64},
            "node_id": ref.get("node_id", ""),
            "node_epoch": int(ref.get("node_epoch") or 1),
            "execution_fence": int(ref.get("execution_fence") or 1),
            "memory_checkpoint": ref.get("memory_checkpoint") or {
                "branch_id": ref.get("branch_id", "main"),
                "commit_sequence": 0, "content_hash": "0" * 64},
            "policy_ref": ref.get("policy_ref") or {
                "service_id": "S02", "id": "default", "revision": 1,
                "content_hash": "0" * 64},
            "actor": {
                "principal_id": auth.principal_id,
                "principal_type": auth.principal_type,
                "delegation_id": auth.delegation_id or auth.api_client_id,
            },
            "command_id": command_id,
            "request_hash": request_hash,
            "trace_id": client_trace_id or command_id,
        }
        signature = hmac.new(self.context_key, _jcs(context).encode("utf-8"),
                             hashlib.sha256).hexdigest()
        return {"context": context, "signature": signature,
                "key_id": "s46-context-v1"}

    # ------------------------------------------------------------ 受理
    def accept_command(self, auth: AuthContext, body: dict, *,
                       command_id: str | None = None,
                       expected_tenant: str | None = None,
                       mcp: bool = False) -> dict:
        """§3.1 的第 2–8 步。返回落库后的回执（未转发）。

        调用方的 `x-expected-tenant` 只做一致性校验，**不参与任何取数**。
        """
        if expected_tenant is not None and expected_tenant != auth.tenant_id:
            raise ServiceError(403, TENANT_MISMATCH,
                               "expected tenant does not match credential")
        body = self._check_body(body)
        supplied_key = command_id or body.get("command_id")
        if not supplied_key:
            raise ServiceError(400, MALFORMED_REQUEST,
                               "command_id is required")
        if body.get("command_id") is not None and body["command_id"] != supplied_key:
            raise ServiceError(400, MALFORMED_REQUEST,
                               "Idempotency-Key does not match body command_id")

        # 3. 三维限流
        self._rate_limit(auth, "command", None)

        # 4. 白名单。未登记 → 404（不是 403：对外部调用方「这个动词不存在」
        #    比「你没权限」更少泄漏信息，也是事实）。
        command = COMMANDS.get(body["command"])
        if command is None:
            raise ServiceError(
                404, COMMAND_NOT_SUPPORTED,
                f"command {body['command']!r} is not supported",
                details={"supported_commands": sorted(COMMANDS)})

        # 5. request_hash：客户端填了就校验，不填就服务端算。
        computed = self.request_hash(body)
        supplied_hash = body.get("request_hash")
        if supplied_hash is not None and supplied_hash != computed:
            raise ServiceError(
                400, REQUEST_HASH_MISMATCH,
                "request_hash does not match the canonical request",
                details={"computed": computed})

        # 6. scope + requires_admission。**read 永远不能触发付费**——看的是
        #    注册表里的 requires_admission，不是命令名（§2.3 第 121 行）。
        #
        #    先判准入、再判 scope：顺序反了的话 `episode.generate`
        #    （`required_scope` 也是 `execute`）会先被通用 scope 检查拦下，
        #    调用方看到的是「缺 execute」，而真实原因是「这条会花钱」。
        #    两者合并成同一个错误码，但 details 要说出钱的那一条。
        if command.requires_admission and not auth.has("execute"):
            self._bump("s46_scope_denied_total")
            raise ServiceError(
                403, INSUFFICIENT_SCOPE,
                f"{command.name} would create a paid admission and requires "
                f"scope execute",
                details={"required_scope": "execute",
                         "granted_scopes": sorted(auth.scopes),
                         "would_require_admission": True})
        if not auth.has(command.required_scope):
            self._bump("s46_scope_denied_total")
            raise ServiceError(
                403, INSUFFICIENT_SCOPE,
                f"{command.name} requires scope {command.required_scope}",
                details={"required_scope": command.required_scope,
                         "granted_scopes": sorted(auth.scopes),
                         "would_require_admission": command.requires_admission})

        # 7. 幂等预检（快速路径）。并发窗口由 icr_cmd_uniq 兜底。
        existing = _row(self.conn.execute(
            "SELECT * FROM integration_command_receipt"
            " WHERE tenant_id=? AND command_id=?",
            (auth.tenant_id, supplied_key)).fetchone())
        if existing is not None:
            return self._replay_or_conflict(existing, computed)

        target = body["target"]
        params = body["params"]
        evidence: list[dict] = []
        if mcp:
            params, target, evidence = self._scrub_tenant_params(
                auth.tenant_id, params, target)

        # 8. 脱敏 → 摘要 → 单事务写回执 + outbox。
        redacted_params, redactions = self._redact(auth.tenant_id, params)
        params_sha = _sha256_hex(_jcs(redacted_params))
        params_bytes = len(_jcs(redacted_params).encode("utf-8"))

        operation_id = _new_id("operation")
        receipt_id = _new_id("receipt")
        accepted_at = self._now()
        upstream_ref = None

        try:
            with Transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO integration_command_receipt ("
                    " receipt_id, tenant_id, operation_id, command_id,"
                    " request_hash, api_client_id, kid, principal_type,"
                    " delegation_id, command, target, params_sha256,"
                    " params_bytes, params_redacted, state, attempt_count,"
                    " reconcile_count, upstream_service, client_trace_id,"
                    " external_id, accepted_at, next_attempt_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, auth.tenant_id, operation_id, supplied_key,
                     computed, auth.api_client_id, auth.kid,
                     auth.principal_type, auth.delegation_id, command.name,
                     _dumps(target), params_sha, params_bytes,
                     1 if redactions else 0, "ACCEPTED", 0, 0,
                     command.upstream_service, body.get("client_trace_id"),
                     body.get("external_id"), accepted_at, accepted_at))
                self._record_external_id(
                    auth.tenant_id, auth.api_client_id, command, target,
                    body.get("external_id"), operation_id, accepted_at)
                self._append_event(
                    tenant_id=auth.tenant_id, home_cell=auth.home_cell,
                    operation_id=operation_id, receipt_id=receipt_id,
                    command=command.name, target=target,
                    api_client_id=auth.api_client_id,
                    principal_type=auth.principal_type,
                    client_trace_id=body.get("client_trace_id"),
                    sequence=1, correlation_id=supplied_key,
                    event_type=EVENT_COMMAND_ACCEPTED, state="ACCEPTED")
                for item in evidence:
                    self._append_audit_event(
                        tenant_id=auth.tenant_id, home_cell=auth.home_cell,
                        operation_id=operation_id, sequence=2,
                        correlation_id=supplied_key,
                        event_type=EVENT_TENANT_PARAM_IGNORED,
                        data={"field": item["field"],
                              "supplied": item["supplied"],
                              "applied": auth.tenant_id,
                              "command": command.name})
        except sqlite3.IntegrityError:
            # 并发窗口：另一个请求赢了。回读并按同键规则处理。
            existing = _row(self.conn.execute(
                "SELECT * FROM integration_command_receipt"
                " WHERE tenant_id=? AND command_id=?",
                (auth.tenant_id, supplied_key)).fetchone())
            if existing is None:
                raise
            return self._replay_or_conflict(existing, computed)

        self._bump("s46_commands_total")
        # 步骤 9 紧跟步骤 8：把内存里的正文交给转发阶段，回执里只有摘要。
        self._pending[(auth.tenant_id, operation_id)] = params
        view = self.get_receipt(auth.tenant_id, operation_id)
        view["_context_ref"] = body.get("context_ref") or {}
        return view

    def submit_command(self, auth: AuthContext, body: dict, *,
                       command_id: str | None = None,
                       expected_tenant: str | None = None,
                       mcp: bool = False) -> tuple[dict, ForwardOutcome | None]:
        """受理 + 立即转发（§3.1 的完整路径）。返回 `(回执视图, 转发结果)`。

        幂等回放**不**再转发：重放意味着同一个 `command_id` 已经走过一次，
        再打一次上游就是在赌上游的幂等实现——而 S21 的 `POST /runs` 后面是
        真金白银。
        """
        receipt = self.accept_command(
            auth, body, command_id=command_id,
            expected_tenant=expected_tenant, mcp=mcp)
        if receipt.get("replayed"):
            return receipt, None
        context_ref = receipt.pop("_context_ref", {}) or {}
        outcome = self.forward(auth.tenant_id, receipt["operation_id"],
                               context_ref=context_ref)
        return self.get_receipt(auth.tenant_id, receipt["operation_id"]), outcome

    def _replay_or_conflict(self, existing: dict, request_hash: str) -> dict:
        """同键同 hash → 回放；同键异 hash → 409（**不是覆盖**，AGENTS.md）。"""
        if existing["request_hash"] != request_hash:
            raise ServiceError(
                409, IDEMPOTENCY_KEY_REUSED,
                "command_id was already used with a different request body",
                details={"stored_request_hash": existing["request_hash"],
                         "incoming_request_hash": request_hash})
        self._bump("s46_idempotent_replays_total")
        view = self._receipt_view(existing)
        view["replayed"] = True
        return view

    def _record_external_id(self, tenant_id: str, api_client_id: str,
                            command: Command, target: dict,
                            external_id, operation_id: str,
                            created_at: str) -> None:
        """§3.7：映射只用于查找，不用于授权。

        `external_system` 由 `api_client.name` 派生，**不由请求体自报**。
        同一 `external_id` 在同一租户下映射到不同内部 id → 409，不覆盖。
        """
        if not external_id:
            return
        if not isinstance(external_id, str):
            raise ServiceError(400, MALFORMED_REQUEST,
                               "external_id must be a string")
        client = _row(self.conn.execute(
            "SELECT name FROM api_client WHERE tenant_id=? AND api_client_id=?",
            (tenant_id, api_client_id)).fetchone())
        if client is None:
            raise ServiceError(401, UNAUTHENTICATED, "credential is not valid")
        system = client["name"]
        kind = next((k for k in command.target_kinds
                     if f"{k}_id" in target), None)
        if kind is None and command.target_kinds:
            kind = command.target_kinds[0]
        if kind is None or kind not in INTERNAL_KINDS:
            raise ServiceError(
                400, MALFORMED_REQUEST,
                "external_id needs a target with an internal resource id")
        internal_id = target.get(f"{kind}_id") or operation_id
        existing = _row(self.conn.execute(
            "SELECT * FROM external_id_mapping"
            " WHERE tenant_id=? AND external_system=? AND external_id=?",
            (tenant_id, system, external_id)).fetchone())
        if existing is not None:
            if existing["internal_id"] != internal_id:
                raise ServiceError(
                    409, EXTERNAL_ID_CONFLICT,
                    "external_id is already mapped to a different resource",
                    details={"external_system": system,
                             "existing_internal_id": existing["internal_id"],
                             "incoming_internal_id": internal_id})
            return
        self.conn.execute(
            "INSERT INTO external_id_mapping (tenant_id, external_system,"
            " external_id, internal_kind, internal_id, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (tenant_id, system, external_id, kind, internal_id, created_at))

    # ------------------------------------------------------------ 事件
    def _append_event(self, *, tenant_id: str, home_cell: str,
                      operation_id: str, receipt_id: str, command: str,
                      target: dict, api_client_id: str, principal_type: str,
                      client_trace_id, sequence: int, correlation_id: str,
                      event_type: str = EVENT_COMMAND_ACCEPTED,
                      state: str = "ACCEPTED") -> str:
        data = {
            "schema_version": SCHEMA_VERSION,
            "operation_id": operation_id,
            "receipt_id": receipt_id,
            "command": command,
            "state": state,
            "target": target,
            "api_client_id": api_client_id,
            "principal_type": principal_type,
            "client_trace_id": client_trace_id,
        }
        # 键集合与 contracts/integration.command-accepted.v2.schema.json 的
        # `required` 逐字相同（additionalProperties: false）。params 全文、
        # 签名、任何凭据都不在事件里（§4.5）。
        assert tuple(sorted(data)) == tuple(sorted(EVENT_DATA_KEYS))
        envelope = new_event(
            source=SOURCE, type=event_type,
            subject=f"integration-command/{operation_id}",
            tenant_id=tenant_id, home_cell=home_cell,
            aggregate_id=operation_id, sequence=sequence,
            correlation_id=correlation_id, data=data,
            data_ref=f"integration-command/{operation_id}")
        return self.outbox.append(envelope)

    def _append_audit_event(self, *, tenant_id: str, home_cell: str,
                            operation_id: str, sequence: int,
                            correlation_id: str, event_type: str,
                            data: dict) -> str:
        """审计意图走同一张 outbox（§10.5），不同 `event_type`。"""
        envelope = new_event(
            source=SOURCE, type=event_type,
            subject=f"integration-command/{operation_id}",
            tenant_id=tenant_id, home_cell=home_cell,
            aggregate_id=operation_id, sequence=sequence,
            correlation_id=correlation_id, data=data,
            data_ref=f"integration-command/{operation_id}")
        return self.outbox.append(envelope)

    # ------------------------------------------------------------ 转发
    def forward(self, tenant_id: str, operation_id: str, *,
                params: dict | None = None,
                context_ref: dict | None = None) -> ForwardOutcome:
        """一次转发尝试。**不在受理事务里**（§3.1 步骤 9）。

        `params` 只活在内存里：回执按 §5.2 / E31 只存摘要，绝不落正文。
        因此：

        - 受理路径把**内存中的** params 直接传进来（步骤 9 紧跟步骤 8）；
        - 重试路径从 `_pending` 取（同一进程内）；
        - 进程重启后两者都没有 → 进 `FORWARD_UNKNOWN`，**不拿空 body 重发**。
          发一个没有参数的 `episode.generate` 比不发更糟：上游可能受理一个
          空 run 并计费。这是 E13 在这里的正确表现，不是「恢复失败」。
        """
        receipt = self._load_receipt(tenant_id, operation_id)
        if receipt["state"] not in ("ACCEPTED", "FORWARD_RETRYING"):
            return self._outcome_from_receipt(receipt)

        command = COMMANDS.get(receipt["command"])
        if command is None:
            # 目录是代码常量；能走到这里说明回执比代码旧（回滚场景，T46-49）。
            # 不猜上游、不丢回执：标 REJECTED 并说明原因。
            return self._reject(receipt, 501, COMMAND_NOT_SUPPORTED,
                                "this command is no longer supported by this "
                                "build")

        if params is None:
            # 非破坏性读取：`FORWARD_UNKNOWN` 后对账还要用它重提。正文只在
            # 终态（FORWARDED / REJECTED）才从内存里丢掉。
            params = self._pending.get((tenant_id, operation_id))
        if params is None:
            return self._unknown(
                receipt,
                "client parameters are not retained across restarts; refusing "
                "to resend a command with an empty body")

        auth = self._auth_for_receipt(receipt)
        target = _loads(receipt["target"], {}) or {}
        context = self.build_execution_context(
            auth, command_id=receipt["command_id"],
            request_hash=receipt["request_hash"], target=target,
            context_ref=context_ref or {},
            client_trace_id=receipt["client_trace_id"])

        concurrency = self._enter_concurrency(auth, command.required_scope)
        try:
            attempt = int(receipt["attempt_count"]) + 1
            self._mark_attempt(tenant_id, operation_id, attempt)
            try:
                result = self.upstream.call(UpstreamCall(
                    service_id=command.upstream_service,
                    action=command.action,
                    tenant_id=tenant_id,
                    target=target,
                    params=params,
                    execution_context=context,
                    # 上游按自己的幂等记录去重；这是「转发成功、写回执前崩溃」
                    # 这一固有窗口的唯一缓解（§7.5）。
                    idempotency_key=receipt["command_id"],
                    client_trace_id=receipt["client_trace_id"]))
            except UpstreamUnknown as exc:
                # **保留** `_pending`：对账判定「确定未执行」时要拿它重提，
                # 而回执里没有正文可用（§5.2）。它只在进程内存里。
                return self._unknown(receipt, str(exc))
            except UpstreamUnavailable as exc:
                return self._retrying(receipt, attempt, str(exc), params)
            except ServiceError as exc:
                self._pending.pop((tenant_id, operation_id), None)
                return self._map_upstream_error(receipt, exc)
            except ConnectionError as exc:
                # 适配器没分类的裸连接错误：通常发生在建立连接阶段（确定没
                # 送到），按可重试处理。
                return self._retrying(receipt, attempt, str(exc), params)
        finally:
            self._leave_concurrency(concurrency)

        self._pending.pop((tenant_id, operation_id), None)
        return self._forwarded(receipt, result or {})

    def _auth_for_receipt(self, receipt: dict) -> AuthContext:
        """转发时重建 `AuthContext`。**只从数据库读**，不信任任何外部输入。"""
        client = _row(self.conn.execute(
            "SELECT * FROM api_client WHERE tenant_id=? AND api_client_id=?",
            (receipt["tenant_id"], receipt["api_client_id"])).fetchone())
        scopes = frozenset(
            r["scope"] for r in self.conn.execute(
                "SELECT scope FROM client_scope_grant WHERE api_client_id=?"
                " AND revoked_at IS NULL"
                " AND (expires_at IS NULL OR expires_at > ?)",
                (receipt["api_client_id"], self._now())).fetchall())
        return AuthContext(
            tenant_id=receipt["tenant_id"],
            api_client_id=receipt["api_client_id"],
            kid=receipt["kid"],
            principal_id=(client or {}).get("principal_id")
            or receipt["api_client_id"],
            principal_type=receipt["principal_type"],
            scopes=scopes,
            plan=(client or {}).get("plan", "standard"),
            home_region=(client or {}).get("home_region", "cn-east-1"),
            home_cell=(client or {}).get("home_cell", "cell_a1"),
            kid_prefix="",
            delegation_id=receipt["delegation_id"])

    def _mark_attempt(self, tenant_id: str, operation_id: str,
                      attempt: int) -> None:
        self.conn.execute(
            "UPDATE integration_command_receipt SET attempt_count=?,"
            " last_attempt_at=? WHERE tenant_id=? AND operation_id=?",
            (attempt, self._now(), tenant_id, operation_id))

    def _forwarded(self, receipt: dict, result: dict) -> ForwardOutcome:
        ref = result.get("ref") or {
            "id": result.get("id") or result.get("run_id")
            or result.get("operation_ref") or "",
            "revision": result.get("revision"),
        }
        now = self._now()
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE integration_command_receipt SET state='FORWARDED',"
                " upstream_ref=?, upstream_state=?, upstream_state_as_of=?,"
                " upstream_watermark=?, forwarded_at=?, next_attempt_at=NULL"
                " WHERE tenant_id=? AND operation_id=?",
                (_dumps(ref), result.get("state"),
                 result.get("state_as_of") or now,
                 result.get("projection_watermark"), now,
                 receipt["tenant_id"], receipt["operation_id"]))
        self._bump("s46_forward_total{forwarded}")
        return ForwardOutcome(
            state="FORWARDED", status=202,
            upstream_ref=ref, upstream_state=result.get("state"),
            upstream_state_as_of=result.get("state_as_of") or now,
            upstream_watermark=result.get("projection_watermark"))

    def _retrying(self, receipt: dict, attempt: int, message: str,
                  params: dict | None = None) -> ForwardOutcome:
        """上游 429/5xx/连接失败：**确定未执行**，有界退避重试（§7.2）。"""
        if attempt >= MAX_FORWARD_ATTEMPTS:
            return self._unknown(
                receipt, f"forwarding still unavailable after {attempt} "
                         f"attempt(s): {message}")
        if params is not None:
            # 同一进程内的重试要保住正文，否则重试会发出空 body。
            self._pending[(receipt["tenant_id"], receipt["operation_id"])] = params
        delay = FORWARD_BACKOFF_SECONDS[min(attempt - 1,
                                           len(FORWARD_BACKOFF_SECONDS) - 1)]
        next_at = _fmt_ts(self._now_dt() + timedelta(seconds=delay))
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE integration_command_receipt SET state='FORWARD_RETRYING',"
                " next_attempt_at=?, last_attempt_at=?"
                " WHERE tenant_id=? AND operation_id=?",
                (next_at, self._now(), receipt["tenant_id"],
                 receipt["operation_id"]))
        self._bump("s46_forward_total{retrying}")
        return ForwardOutcome(
            state="FORWARD_RETRYING", status=502, code=UPSTREAM_UNAVAILABLE,
            message=message, retryable=True)

    def _unknown(self, receipt: dict, message: str) -> ForwardOutcome:
        """结果未知。**绝不自动重提**——那是重复付费。"""
        now = self._now()
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE integration_command_receipt SET state='FORWARD_UNKNOWN',"
                " next_attempt_at=NULL, last_attempt_at=?"
                " WHERE tenant_id=? AND operation_id=?",
                (now, receipt["tenant_id"], receipt["operation_id"]))
            self._append_audit_event(
                tenant_id=receipt["tenant_id"],
                home_cell=self._home_cell(receipt["tenant_id"]),
                operation_id=receipt["operation_id"], sequence=3,
                correlation_id=receipt["command_id"],
                event_type=EVENT_FORWARD_UNKNOWN,
                data={"command": receipt["command"],
                      "upstream_service": receipt["upstream_service"],
                      "next_action": "reconcile"})
        self._bump("s46_forward_total{unknown}")
        self._bump("s46_forward_unknown_total")
        return ForwardOutcome(
            state="FORWARD_UNKNOWN", status=503, code=UPSTREAM_UNKNOWN,
            message=(message or "upstream outcome is unknown")
            + "; do not retry with a new command_id",
            retryable=True)

    def _reject(self, receipt: dict, status: int, code: str,
                message: str) -> ForwardOutcome:
        rejection = {"code": code, "message": message,
                     "upstream_service": receipt["upstream_service"]}
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE integration_command_receipt SET state='REJECTED',"
                " rejection=?, next_attempt_at=NULL, last_attempt_at=?"
                " WHERE tenant_id=? AND operation_id=?",
                (_dumps(rejection), self._now(), receipt["tenant_id"],
                 receipt["operation_id"]))
        self._bump("s46_forward_total{rejected}")
        return ForwardOutcome(state="REJECTED", status=status, code=code,
                              message=message, rejection=rejection)

    def _map_upstream_error(self, receipt: dict, exc: ServiceError) -> ForwardOutcome:
        """§7.2 的映射表。**4xx 不重试**。"""
        if exc.status >= 500:
            return self._retrying(receipt, str(exc))
        if exc.status in (401, 403):
            # S46 自己的上游凭据出问题：不重试，告警（F46-04）。
            return self._reject(receipt, 502, UPSTREAM_AUTH_FAILED,
                                "upstream rejected this service's credential")
        if exc.status == 409:
            return self._reject(receipt, 422, COMMAND_NOT_FEASIBLE,
                                exc.code or "upstream state conflict")
        if exc.status == 404:
            return self._reject(receipt, 404, NOT_FOUND,
                                exc.code or "target not found")
        if exc.status in (400, 422):
            return self._reject(receipt, exc.status, exc.code or MALFORMED_REQUEST,
                                str(exc))
        return self._reject(receipt, 422, COMMAND_NOT_FEASIBLE, str(exc))

    def _outcome_from_receipt(self, receipt: dict) -> ForwardOutcome:
        state = receipt["state"]
        if state == "FORWARDED":
            return ForwardOutcome(
                state="FORWARDED", status=202,
                upstream_ref=_loads(receipt["upstream_ref"]),
                upstream_state=receipt["upstream_state"],
                upstream_state_as_of=receipt["upstream_state_as_of"],
                upstream_watermark=receipt["upstream_watermark"])
        if state == "REJECTED":
            rejection = _loads(receipt["rejection"], {}) or {}
            return ForwardOutcome(state="REJECTED", status=422,
                                  code=rejection.get("code") or
                                  COMMAND_NOT_FEASIBLE,
                                  message=rejection.get("message", ""),
                                  rejection=rejection)
        if state == "FORWARD_UNKNOWN":
            return ForwardOutcome(
                state="FORWARD_UNKNOWN", status=503, code=UPSTREAM_UNKNOWN,
                message="upstream outcome is unknown; poll this operation",
                retryable=True)
        if state == "FORWARD_RETRYING":
            return ForwardOutcome(
                state="FORWARD_RETRYING", status=502,
                code=UPSTREAM_UNAVAILABLE, retryable=True)
        return ForwardOutcome(state=state, status=202)

    # ------------------------------------------------------------ 对账
    def forward_pending(self, limit: int = 50) -> list[dict]:
        """轮询 job：`ACCEPTED` / `FORWARD_RETRYING` 且已到点的回执（§6.3）。"""
        now = self._now()
        rows = self.conn.execute(
            "SELECT operation_id, tenant_id FROM integration_command_receipt"
            " WHERE state IN ('ACCEPTED','FORWARD_RETRYING')"
            "   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
            " ORDER BY accepted_at LIMIT ?", (now, limit)).fetchall()
        results = []
        for row in rows:
            outcome = self.forward(row["tenant_id"], row["operation_id"])
            results.append({"operation_id": row["operation_id"],
                            "state": outcome.state})
        return results

    def reconcile_one(self, operation_id: str) -> str:
        """单条对账，返回 `forwarded` / `retry` / `unknown` / `stale`。"""
        rows = self.conn.execute(
            "SELECT operation_id FROM integration_command_receipt"
            " WHERE operation_id=? AND state='FORWARD_UNKNOWN'",
            (operation_id,)).fetchall()
        if not rows:
            return "not_unknown"
        results = self.reconcile(limit=None, operation_id=operation_id)
        return results[0]["resolution"] if results else "unknown"

    def reconcile(self, limit: int | None = 100,
                  operation_id: str | None = None) -> list[dict]:
        """§3.3：`FORWARD_UNKNOWN` 的唯一出路。

        - 存在 → `FORWARDED`
        - 确定未执行且**还没重提过** → 允许一次重提（同一 receipt）
        - 上游也说不清 → 保持 UNKNOWN
        - 超过 7 天 → `STALE_UNKNOWN`，只读展示，不再自动处理
        """
        now_dt = self._now_dt()
        if operation_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM integration_command_receipt"
                " WHERE state='FORWARD_UNKNOWN' AND operation_id=?",
                (operation_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM integration_command_receipt"
                " WHERE state='FORWARD_UNKNOWN' ORDER BY accepted_at LIMIT ?",
                (limit if limit is not None else 100,)).fetchall()
        results = []
        for row in rows:
            receipt = dict(row)
            accepted = _parse_ts(receipt["accepted_at"])
            if accepted is not None and \
                    (now_dt - accepted).total_seconds() > RECONCILE_WINDOW_SECONDS:
                with Transaction(self.conn):
                    self.conn.execute(
                        "UPDATE integration_command_receipt"
                        " SET state='STALE_UNKNOWN'"
                        " WHERE tenant_id=? AND operation_id=?",
                        (receipt["tenant_id"], receipt["operation_id"]))
                self._bump("s46_reconcile_stale_total")
                results.append({"operation_id": receipt["operation_id"],
                                "resolution": "stale"})
                continue

            verdict = self.upstream.probe(
                tenant_id=receipt["tenant_id"],
                service_id=receipt["upstream_service"] or "",
                upstream_ref=_loads(receipt["upstream_ref"]),
                command_id=receipt["command_id"])
            if verdict == "executed":
                with Transaction(self.conn):
                    self.conn.execute(
                        "UPDATE integration_command_receipt"
                        " SET state='FORWARDED', forwarded_at=COALESCE("
                        "forwarded_at, ?), next_attempt_at=NULL"
                        " WHERE tenant_id=? AND operation_id=?",
                        (self._now(), receipt["tenant_id"],
                         receipt["operation_id"]))
                self._bump("s46_reconcile_resolved_total{forwarded}")
                results.append({"operation_id": receipt["operation_id"],
                                "resolution": "forwarded"})
            elif verdict == "not_executed" and int(
                    receipt["reconcile_count"]) == 0:
                with Transaction(self.conn):
                    self.conn.execute(
                        "UPDATE integration_command_receipt"
                        " SET reconcile_count=1, state='ACCEPTED',"
                        " attempt_count=0, next_attempt_at=?"
                        " WHERE tenant_id=? AND operation_id=?",
                        (self._now(), receipt["tenant_id"],
                         receipt["operation_id"]))
                self._bump("s46_reconcile_resolved_total{retry}")
                results.append({"operation_id": receipt["operation_id"],
                                "resolution": "retry"})
            else:
                results.append({"operation_id": receipt["operation_id"],
                                "resolution": "unknown"})
        return results

    # ------------------------------------------------------------ 查询
    def _load_receipt(self, tenant_id: str, operation_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM integration_command_receipt"
            " WHERE tenant_id=? AND operation_id=?",
            (tenant_id, operation_id)).fetchone())
        if row is None:
            # 跨租户与不存在同形（E28）：确认「存在但你没权限」本身就是泄漏。
            raise ServiceError(404, NOT_FOUND, f"no operation {operation_id}")
        return row

    def get_receipt(self, tenant_id: str, operation_id: str) -> dict:
        return self._receipt_view(self._load_receipt(tenant_id, operation_id))

    def _receipt_view(self, receipt: dict) -> dict:
        """§4.2 的响应形状。`upstream.state_as_of` **必填**（docs/04 §5）：
        缺了它就是让客户端把旧状态当成实时状态。"""
        upstream = None
        if receipt["upstream_service"]:
            upstream = {
                "service_id": receipt["upstream_service"],
                "ref": _loads(receipt["upstream_ref"]),
                "state": receipt["upstream_state"],
                "state_as_of": receipt["upstream_state_as_of"],
                "projection_watermark": receipt["upstream_watermark"],
            }
        return {
            "operation_id": receipt["operation_id"],
            "receipt_id": receipt["receipt_id"],
            "command": receipt["command"],
            "state": receipt["state"],
            "accepted_at": receipt["accepted_at"],
            "forwarded_at": receipt["forwarded_at"],
            "attempt_count": receipt["attempt_count"],
            "upstream": upstream,
            "tenant_id_derived": receipt["tenant_id"],
            "client_trace_id": receipt["client_trace_id"],
            "rejection": _loads(receipt["rejection"]),
            "poll_url": f"/operations/{receipt['operation_id']}",
        }

    def get_operation(self, tenant_id: str, operation_id: str,
                      refresh: bool = True) -> dict:
        """轮询回执 + 上游状态快照 + 水位（§4.2）。

        每次实时查上游，缓存不超过 5s（§10.3）。刷新失败**不**把状态改成
        未知：回执状态是本地事实，上游快照是附加信息，用 `state_as_of`
        标明它的年龄。
        """
        receipt = self._load_receipt(tenant_id, operation_id)
        if not refresh or receipt["state"] != "FORWARDED" or \
                not receipt["upstream_service"]:
            return self._receipt_view(receipt)
        as_of = _parse_ts(receipt["upstream_state_as_of"])
        fresh = as_of is not None and \
            (self._now_dt() - as_of).total_seconds() < UPSTREAM_READ_TTL_SECONDS
        if fresh and receipt["upstream_state"] is not None:
            return self._receipt_view(receipt)
        try:
            snapshot = self.upstream.state(
                tenant_id=tenant_id,
                service_id=receipt["upstream_service"],
                upstream_ref=_loads(receipt["upstream_ref"]))
        except (ConnectionError, ServiceError):
            return self._receipt_view(receipt)
        if not isinstance(snapshot, dict):
            return self._receipt_view(receipt)
        self.conn.execute(
            "UPDATE integration_command_receipt SET upstream_state=?,"
            " upstream_state_as_of=?, upstream_watermark=?"
            " WHERE tenant_id=? AND operation_id=?",
            (snapshot.get("state"),
             snapshot.get("state_as_of") or self._now(),
             snapshot.get("projection_watermark"),
             tenant_id, operation_id))
        return self._receipt_view(self._load_receipt(tenant_id, operation_id))

    def list_operations(self, tenant_id: str, *, state: str | None = None,
                        command: str | None = None, limit: int = 50) -> dict:
        clauses = ["tenant_id=?"]
        args: list = [tenant_id]
        if state:
            clauses.append("state=?")
            args.append(state)
        if command:
            clauses.append("command=?")
            args.append(command)
        args.append(max(1, min(limit, 200)))
        rows = self.conn.execute(
            "SELECT * FROM integration_command_receipt WHERE "
            + " AND ".join(clauses) + " ORDER BY accepted_at DESC LIMIT ?",
            args).fetchall()
        return {"operations": [self._receipt_view(dict(r)) for r in rows]}

    def cancel_operation(self, tenant_id: str, operation_id: str) -> dict:
        """`POST /operations/{id}:cancel`。只对 `FORWARD_RETRYING` 有效。

        `FORWARDED` 之后取消要走上游——S46 不替上游取消（它是第二权威）。
        """
        receipt = self._load_receipt(tenant_id, operation_id)
        if receipt["state"] == "FORWARD_RETRYING":
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE integration_command_receipt SET state='CANCELLED',"
                    " next_attempt_at=NULL WHERE tenant_id=? AND operation_id=?",
                    (tenant_id, operation_id))
            return self.get_receipt(tenant_id, operation_id)
        if receipt["state"] == "FORWARDED":
            raise ServiceError(
                409, COMMAND_NOT_FEASIBLE,
                "operation already forwarded; cancel it at the upstream",
                details={"upstream": receipt["upstream_service"]})
        return self.get_receipt(tenant_id, operation_id)

    def _home_cell(self, tenant_id: str) -> str:
        row = _row(self.conn.execute(
            "SELECT home_cell FROM api_client WHERE tenant_id=? LIMIT 1",
            (tenant_id,)).fetchone())
        return (row or {}).get("home_cell") or "cell_a1"

    # ------------------------------------------------------------ 客户端与 key
    def create_client(self, tenant_id: str, *, name: str, plan: str = "standard",
                      home_region: str = "cn-east-1", home_cell: str = "cell_a1",
                      principal_id: str | None = None,
                      principal_type: str = "api_client",
                      contact_email: str | None = None,
                      scopes: tuple[str, ...] = ("read",),
                      created_by: str = "system") -> dict:
        if plan not in PLANS:
            raise ServiceError(400, MALFORMED_REQUEST, f"unknown plan {plan!r}")
        if principal_type not in PRINCIPAL_TYPES:
            raise ServiceError(400, MALFORMED_REQUEST,
                               f"unknown principal_type {principal_type!r}")
        bad = sorted(set(scopes) - set(SCOPES))
        if bad:
            raise ServiceError(400, MALFORMED_REQUEST,
                               f"unknown scope(s): {', '.join(bad)}")
        if not name:
            raise ServiceError(400, MALFORMED_REQUEST, "name is required")
        client_id = _new_id("client")
        now = self._now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO api_client (api_client_id, tenant_id, name, plan,"
                " state, contact_email, principal_id, principal_type,"
                " home_region, home_cell, created_by, created_at)"
                " VALUES (?,?,?,?,'ACTIVE',?,?,?,?,?,?,?)",
                (client_id, tenant_id, name, plan, contact_email,
                 principal_id or client_id, principal_type,
                 home_region, home_cell, created_by, now))
            for scope in scopes:
                self.conn.execute(
                    "INSERT INTO client_scope_grant (api_client_id, scope,"
                    " granted_by, granted_at) VALUES (?,?,?,?)",
                    (client_id, scope, created_by, now))
        return self.get_client(tenant_id, client_id)

    def get_client(self, tenant_id: str, api_client_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM api_client WHERE tenant_id=? AND api_client_id=?",
            (tenant_id, api_client_id)).fetchone())
        if row is None:
            raise ServiceError(404, NOT_FOUND, f"no api client {api_client_id}")
        grants = [r["scope"] for r in self.conn.execute(
            "SELECT scope FROM client_scope_grant WHERE api_client_id=?"
            " AND revoked_at IS NULL ORDER BY scope",
            (api_client_id,)).fetchall()]
        return {"api_client_id": row["api_client_id"],
                "tenant_id": row["tenant_id"], "name": row["name"],
                "plan": row["plan"], "state": row["state"],
                "principal_id": row["principal_id"],
                "principal_type": row["principal_type"],
                "home_region": row["home_region"],
                "home_cell": row["home_cell"],
                "scopes": grants,
                "created_at": row["created_at"]}

    def create_api_key(self, tenant_id: str, api_client_id: str, *,
                       created_by: str = "system",
                       retire_days: int = KEY_RETIRE_DAYS) -> dict:
        """§3.4：明文只在创建响应里出现一次；存 `sha256` + `kid` 前缀。

        超过 `MAX_KEYS_ACTIVE`（2 个 ACTIVE + 1 个 RETIRING）→ 409。
        """
        client = self.get_client(tenant_id, api_client_id)
        live = self.conn.execute(
            "SELECT COUNT(*) AS n FROM api_key WHERE tenant_id=?"
            " AND api_client_id=? AND state IN ('ACTIVE','RETIRING')",
            (tenant_id, api_client_id)).fetchone()["n"]
        active = self.conn.execute(
            "SELECT COUNT(*) AS n FROM api_key WHERE tenant_id=?"
            " AND api_client_id=? AND state='ACTIVE'",
            (tenant_id, api_client_id)).fetchone()["n"]
        if live >= MAX_KEYS_LIVE or active >= MAX_KEYS_ACTIVE:
            raise ServiceError(
                409, KEY_LIMIT_REACHED,
                f"at most {MAX_KEYS_ACTIVE} ACTIVE and {MAX_KEYS_LIVE} live "
                f"keys per client", details={"active": active, "live": live})

        kid = _new_id("key")
        # 明文形态：`ak_live_<kid>.<random>`。前缀进日志与 UI，secret 部分
        # 只在这一个响应里出现。
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        plaintext = f"ak_live_{kid}.{secret}"
        kid_prefix = plaintext[:8]
        now = self._now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO api_key (kid, api_client_id, tenant_id,"
                " secret_hash, kid_prefix, state, scopes_snapshot, created_at,"
                " created_by) VALUES (?,?,?,?,?,'ACTIVE',?,?,?)",
                (kid, api_client_id, tenant_id, _sha256_hex(plaintext),
                 kid_prefix, _dumps(client["scopes"]), now, created_by))
        self._bump("s46_api_keys_created_total")
        return {"kid": kid, "api_client_id": api_client_id,
                "kid_prefix": kid_prefix, "state": "ACTIVE",
                "api_key": plaintext,
                "warning": "this is the only time the key is shown"}

    def list_api_keys(self, tenant_id: str, api_client_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT kid, kid_prefix, state, created_at, retire_at,"
            " revoked_at, last_used_at FROM api_key"
            " WHERE tenant_id=? AND api_client_id=? ORDER BY created_at",
            (tenant_id, api_client_id)).fetchall()
        return {"keys": [dict(r) for r in rows]}

    def retire_api_key(self, tenant_id: str, kid: str, *,
                       retire_days: int = KEY_RETIRE_DAYS) -> dict:
        row = self._load_key(tenant_id, kid)
        if row["state"] == "REVOKED":
            return {"kid": kid, "state": "REVOKED",
                    "retire_at": row["retire_at"]}
        retire_at = _fmt_ts(self._now_dt() + timedelta(days=retire_days))
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE api_key SET state='RETIRING', retire_at=?"
                " WHERE tenant_id=? AND kid=?", (retire_at, tenant_id, kid))
        self._bump("s46_api_keys_retired_total")
        return {"kid": kid, "state": "RETIRING", "retire_at": retire_at}

    def revoke_api_key(self, tenant_id: str, kid: str) -> dict:
        row = self._load_key(tenant_id, kid)
        if row["state"] != "REVOKED":
            self._revoke_key(tenant_id, kid, reason="explicit")
        return {"kid": kid, "state": "REVOKED",
                "revoked_at": self._now()}

    def _load_key(self, tenant_id: str, kid: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM api_key WHERE tenant_id=? AND kid=?",
            (tenant_id, kid)).fetchone())
        if row is None:
            raise ServiceError(404, NOT_FOUND, f"no api key {kid}")
        return row

    def _revoke_key(self, tenant_id: str, kid: str, *, reason: str) -> None:
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE api_key SET state='REVOKED', revoked_at=?"
                " WHERE tenant_id=? AND kid=?",
                (self._now(), tenant_id, kid))
        self._bump(f"s46_api_keys_revoked_total{{{reason}}}")

    def sweep_expired_keys(self) -> int:
        """janitor：`RETIRING` 超 `retire_at` → `REVOKED`（F46-02）。"""
        now = self._now()
        with Transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE api_key SET state='REVOKED', revoked_at=?"
                " WHERE state='RETIRING' AND retire_at IS NOT NULL"
                " AND retire_at <= ?", (now, now))
            count = cursor.rowcount or 0
        if count:
            self._bump("s46_api_keys_revoked_total{retire_at_elapsed}", count)
        return count

    def grant_scope(self, tenant_id: str, api_client_id: str, scope: str, *,
                    granted_by: str = "system",
                    expires_at: str | None = None) -> dict:
        if scope not in SCOPES:
            raise ServiceError(400, MALFORMED_REQUEST, f"unknown scope {scope!r}")
        self.get_client(tenant_id, api_client_id)
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO client_scope_grant (api_client_id, scope,"
                " granted_by, granted_at, expires_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(api_client_id, scope) DO UPDATE SET"
                " revoked_at=NULL, granted_by=excluded.granted_by,"
                " granted_at=excluded.granted_at, expires_at=excluded.expires_at",
                (api_client_id, scope, granted_by, self._now(), expires_at))
        return self.get_client(tenant_id, api_client_id)

    def revoke_scope(self, tenant_id: str, api_client_id: str,
                     scope: str) -> dict:
        """撤权**立即生效**（T46-06）：判定读 `client_scope_grant`，每次现查。"""
        self.get_client(tenant_id, api_client_id)
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE client_scope_grant SET revoked_at=?"
                " WHERE api_client_id=? AND scope=?", (self._now(),
                                                       api_client_id, scope))
        self._bump("s46_scope_revoked_total")
        return self.get_client(tenant_id, api_client_id)

    # ------------------------------------------------------------ 外部 ID
    def resolve_external_id(self, tenant_id: str, external_system: str,
                            external_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM external_id_mapping WHERE tenant_id=?"
            " AND external_system=? AND external_id=?",
            (tenant_id, external_system, external_id)).fetchone())
        if row is None:
            raise ServiceError(404, NOT_FOUND, "no mapping for that external id")
        return row

    # ------------------------------------------------------------ Webhook
    def create_subscription(self, tenant_id: str, *, url: str,
                            event_types, secret: str,
                            api_client_id: str | None = None,
                            description: str | None = None) -> dict:
        """§2.4：HTTPS + 非私网（E29），订阅数上限（默认 20）。"""
        self._validate_webhook_url(url)
        types = self._normalize_event_types(event_types)
        live = self.conn.execute(
            "SELECT COUNT(*) AS n FROM webhook_subscription WHERE tenant_id=?"
            " AND state <> 'DELETED'", (tenant_id,)).fetchone()["n"]
        if live >= MAX_WEBHOOK_SUBSCRIPTIONS:
            raise ServiceError(
                409, WEBHOOK_LIMIT_REACHED,
                f"at most {MAX_WEBHOOK_SUBSCRIPTIONS} subscriptions per tenant",
                details={"limit": MAX_WEBHOOK_SUBSCRIPTIONS})
        subscription_id = _new_id("subscription")
        now = self._now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO webhook_subscription (subscription_id, tenant_id,"
                " api_client_id, url, event_types, secret_ref, state,"
                " description, created_at)"
                " VALUES (?,?,?,?,?,?,'ACTIVE',?,?)",
                (subscription_id, tenant_id, api_client_id, url,
                 _dumps(types), _seal(secret, self.secret_key),
                 description, now))
        return self.get_subscription(tenant_id, subscription_id)

    def _normalize_event_types(self, event_types) -> list[str]:
        if isinstance(event_types, str):
            event_types = [event_types]
        if not isinstance(event_types, (list, tuple)) or not event_types:
            raise ServiceError(400, MALFORMED_REQUEST,
                               "event_types must be a non-empty array")
        types = []
        for item in event_types:
            if not isinstance(item, str) or not item:
                raise ServiceError(400, MALFORMED_REQUEST,
                                   "event_types entries must be strings")
            # 注册过的事件类型：本服务自己的 + 上游的（`event-topics.v2.json`）。
            if item not in self._known_event_types():
                raise ServiceError(
                    400, MALFORMED_REQUEST, f"unknown event type {item!r}",
                    details={"known": sorted(self._known_event_types())[:50]})
            types.append(item)
        return sorted(set(types))

    @staticmethod
    def _known_event_types() -> frozenset[str]:
        return frozenset({
            EVENT_COMMAND_ACCEPTED,
            "orchestration.run.state-changed",
            "delivery.package.sealed",
            "audit.record.appended",
            "human-review.decision.recorded",
        })

    def get_subscription(self, tenant_id: str, subscription_id: str) -> dict:
        row = self._load_subscription(tenant_id, subscription_id)
        return {"subscription_id": row["subscription_id"],
                "tenant_id": row["tenant_id"],
                "api_client_id": row["api_client_id"],
                "url": row["url"],
                "event_types": _loads(row["event_types"], []),
                "state": row["state"],
                "description": row["description"],
                "created_at": row["created_at"],
                "rotated_at": row["rotated_at"],
                # **不回显 secret**（§4.3）。
                "secret_set": bool(row["secret_ref"])}

    def _load_subscription(self, tenant_id: str,
                           subscription_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM webhook_subscription WHERE tenant_id=?"
            " AND subscription_id=?", (tenant_id, subscription_id)).fetchone())
        if row is None:
            raise ServiceError(404, NOT_FOUND,
                               f"no subscription {subscription_id}")
        return row

    def list_subscriptions(self, tenant_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT subscription_id FROM webhook_subscription WHERE tenant_id=?"
            " AND state <> 'DELETED' ORDER BY created_at", (tenant_id,)).fetchall()
        return {"subscriptions": [self.get_subscription(tenant_id, r[0])
                                  for r in rows]}

    def update_subscription(self, tenant_id: str, subscription_id: str, *,
                            url: str | None = None, event_types=None,
                            active: bool | None = None,
                            description: str | None = None) -> dict:
        """PATCH **不能改 secret**（改 secret = 新建，§4.3）。"""
        row = self._load_subscription(tenant_id, subscription_id)
        if url is not None:
            self._validate_webhook_url(url)
        types = self._normalize_event_types(event_types) if event_types is not None \
            else _loads(row["event_types"], [])
        state = row["state"]
        if active is True and state in ("SUSPENDED", "DELETED"):
            state = "ACTIVE"
        elif active is False and state == "ACTIVE":
            state = "SUSPENDED"
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE webhook_subscription SET url=?, event_types=?,"
                " state=?, description=? WHERE tenant_id=?"
                " AND subscription_id=?",
                (url if url is not None else row["url"], _dumps(types), state,
                 description if description is not None else row["description"],
                 tenant_id, subscription_id))
        return self.get_subscription(tenant_id, subscription_id)

    def rotate_subscription_secret(self, tenant_id: str, subscription_id: str,
                                   secret: str) -> dict:
        """双活期：旧 secret 在 `previous_secret_expires_at` 之前仍能验签。"""
        row = self._load_subscription(tenant_id, subscription_id)
        now = self._now_dt()
        expires = _fmt_ts(now + timedelta(days=KEY_RETIRE_DAYS))
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE webhook_subscription SET secret_ref=?, rotated_at=?,"
                " previous_secret_ref=?, previous_secret_expires_at=?"
                " WHERE tenant_id=? AND subscription_id=?",
                (_seal(secret, self.secret_key), _fmt_ts(now),
                 row["secret_ref"], expires, tenant_id, subscription_id))
        return {**self.get_subscription(tenant_id, subscription_id),
                "previous_secret_expires_at": expires}

    def delete_subscription(self, tenant_id: str, subscription_id: str) -> dict:
        """DELETE：停用并**取消未投递**（T46-38）。不改业务（§7.4）。"""
        self._load_subscription(tenant_id, subscription_id)
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE webhook_subscription SET state='DELETED'"
                " WHERE tenant_id=? AND subscription_id=?",
                (tenant_id, subscription_id))
            cursor = self.conn.execute(
                "UPDATE webhook_delivery SET state='CANCELLED'"
                " WHERE tenant_id=? AND subscription_id=?"
                " AND state IN ('PENDING','DELIVERING')",
                (tenant_id, subscription_id))
            cancelled = cursor.rowcount or 0
        return {"subscription_id": subscription_id, "state": "DELETED",
                "cancelled_deliveries": cancelled}

    # ------------------------------------------------------------ SSRF
    def _validate_webhook_url(self, url: str) -> None:
        """E29：必须 HTTPS 且不能是私网。创建时校验，**每次投递前再校验**。"""
        parsed = urlsplit(url or "")
        if parsed.scheme != "https":
            raise ServiceError(400, INVALID_URL,
                               "webhook url must use https")
        host = parsed.hostname
        if not host:
            raise ServiceError(400, INVALID_URL,
                               "webhook url must have a host")
        if parsed.port not in (None, 443):
            raise ServiceError(400, INVALID_URL,
                               "webhook url must use port 443")
        addresses = self._resolve(host)
        if not addresses:
            raise ServiceError(400, INVALID_URL,
                               "webhook host does not resolve")
        for address in addresses:
            if _is_private_address(address):
                raise ServiceError(400, SSRF_BLOCKED,
                                   "webhook host resolves to a private address",
                                   details={"reason": "private_address"})

    def _resolve(self, host: str) -> list[str]:
        literal = _literal_ip(host)
        if literal is not None:
            return [literal]
        if self.resolver is None:
            # 没有解析器就不猜「安全」。但也不做真实 DNS：返回一个明确的
            # 占位，让「未配置」变成 400 而不是静默放行。
            raise ServiceError(503, INVALID_URL,
                               "webhook host resolution is not configured",
                               retryable=True)
        try:
            return list(self.resolver(host))
        except (OSError, ValueError) as exc:
            raise ServiceError(400, INVALID_URL,
                               f"webhook host does not resolve: {exc}") from exc

    # ------------------------------------------------------------ 投递
    def enqueue_event(self, event: dict) -> int:
        """把一条外部/内部事件扇出给匹配的订阅。

        **入库幂等，投递至少一次**：`wd_uniq` 挡的是「事件被消费两次导致入队
        两次」，不挡「投递失败重试」（§5.3）。
        """
        tenant_id = event.get("tenant_id")
        event_id = event.get("id") or event.get("event_id")
        event_type = event.get("type") or event.get("event_type")
        if not tenant_id or not event_id or not event_type:
            raise ServiceError(400, MALFORMED_REQUEST,
                               "event needs tenant_id, id and type")
        aggregate = event.get("aggregate_id") or event.get("aggregateId") or event_id
        partition_key = f"{tenant_id}:{aggregate}"
        payload = _jcs(event)
        now = self._now()
        # 事件级去重：同一消费者见过这条事件就不再入队（T46-34 / T46-36）。
        rows = self.conn.execute(
            "SELECT subscription_id FROM webhook_subscription"
            " WHERE tenant_id=? AND state='ACTIVE'", (tenant_id,)).fetchall()
        created = 0
        for row in rows:
            subscription = self._load_subscription(tenant_id,
                                                   row["subscription_id"])
            if event_type not in _loads(subscription["event_types"], []):
                continue
            delivery_id = _new_id("delivery")
            with Transaction(self.conn):
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO webhook_delivery (delivery_id,"
                    " subscription_id, tenant_id, event_id, event_type,"
                    " partition_key, payload, state, attempt_count,"
                    " next_attempt_at, created_at)"
                    " VALUES (?,?,?,?,?,?,?,'PENDING',0,?,?)",
                    (delivery_id, subscription["subscription_id"], tenant_id,
                     event_id, event_type, partition_key, payload, now, now))
                created += 1 if cursor.rowcount else 0
        if created:
            self._bump("s46_webhook_enqueued_total", created)
        return created

    def dispatch_deliveries(self, limit: int = 100) -> list[dict]:
        """同订阅、同 `aggregate_id` 的事件**串行投递**（§3.5、§6.4）。

        SQLite 没有 `DISTINCT ON` / `SKIP LOCKED`；等价做法是：每个
        `partition_key` 只取最早一条，且该 partition 没有在途（`DELIVERING`）
        的行。这保证「同一 run 的状态更新不会乱序到达客户」。
        """
        if self.webhook_sender is None:
            return []
        now = self._now()
        rows = self.conn.execute(
            "SELECT * FROM webhook_delivery WHERE state='PENDING'"
            " AND next_attempt_at <= ? ORDER BY partition_key, created_at",
            (now,)).fetchall()
        claimed: set[str] = set()
        busy = {r["partition_key"] for r in self.conn.execute(
            "SELECT partition_key FROM webhook_delivery"
            " WHERE state='DELIVERING'").fetchall()}
        results = []
        for row in rows:
            delivery = dict(row)
            partition = delivery["partition_key"]
            if partition in claimed or partition in busy:
                continue
            claimed.add(partition)
            results.append(self._deliver_one(delivery))
            if len(results) >= limit:
                break
        return results

    def _deliver_one(self, delivery: dict) -> dict:
        subscription = _row(self.conn.execute(
            "SELECT * FROM webhook_subscription WHERE tenant_id=?"
            " AND subscription_id=?",
            (delivery["tenant_id"], delivery["subscription_id"])).fetchone())
        if subscription is None or subscription["state"] != "ACTIVE":
            self._finish_delivery(delivery, state="CANCELLED")
            return {"delivery_id": delivery["delivery_id"],
                    "state": "CANCELLED"}
        # 每次投递前复查 URL：DNS 可以改，创建时校验不够（§2.4 / E29）。
        try:
            self._validate_webhook_url(subscription["url"])
        except ServiceError as exc:
            self._finish_delivery(delivery, state="FAILED",
                                  error=exc.code,
                                  response_code=None)
            self._bump(f"s46_webhook_failed_total{{{exc.code.lower()}}}")
            return {"delivery_id": delivery["delivery_id"], "state": "FAILED",
                    "reason": exc.code}

        attempt = int(delivery["attempt_count"]) + 1
        lease = _fmt_ts(self._now_dt() + timedelta(seconds=DELIVERY_LEASE_SECONDS))
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE webhook_delivery SET state='DELIVERING', attempt_count=?,"
                " lease_expires_at=? WHERE delivery_id=?",
                (attempt, lease, delivery["delivery_id"]))
        timestamp = str(int(self._now_dt().timestamp()))
        secret = _open(subscription["secret_ref"], self.secret_key)
        signature = sign_webhook(secret, timestamp, delivery["payload"])
        headers = {
            "Content-Type": "application/json",
            "X-Drama-Signature": signature,
            "X-Drama-Delivery-Id": (f"{delivery['subscription_id']}:"
                                    f"{delivery['event_id']}:{attempt}"),
            "X-Drama-Event-Type": delivery["event_type"],
        }
        try:
            response_code = self.webhook_sender(url=subscription["url"],
                                                body=delivery["payload"],
                                                headers=headers)
        except ConnectionError as exc:
            return self._retry_or_fail(delivery, attempt, None, str(exc))
        if 200 <= int(response_code) < 300:
            self._finish_delivery(delivery, state="DELIVERED",
                                  response_code=int(response_code))
            self._bump("s46_webhook_delivery_total{delivered}")
            return {"delivery_id": delivery["delivery_id"],
                    "state": "DELIVERED", "response_code": int(response_code)}
        # 4xx 也重试：可能是对方临时 bug（§7.4）。8 次后 FAILED。
        return self._retry_or_fail(delivery, attempt, int(response_code),
                                   f"http_{response_code}")

    def _retry_or_fail(self, delivery: dict, attempt: int,
                       response_code: int | None,
                       error: str) -> dict:
        if attempt >= MAX_WEBHOOK_ATTEMPTS:
            self._finish_delivery(delivery, state="FAILED",
                                  response_code=response_code, error=error)
            self._bump("s46_webhook_failed_total{exhausted}")
            return {"delivery_id": delivery["delivery_id"], "state": "FAILED"}
        delay = WEBHOOK_BACKOFF_SECONDS[min(attempt - 1,
                                            len(WEBHOOK_BACKOFF_SECONDS) - 1)]
        next_at = _fmt_ts(self._now_dt() + timedelta(seconds=delay))
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE webhook_delivery SET state='PENDING',"
                " next_attempt_at=?, last_response_code=?, last_error=?"
                " WHERE delivery_id=?",
                (next_at, response_code, error, delivery["delivery_id"]))
        self._bump("s46_webhook_delivery_total{retry}")
        return {"delivery_id": delivery["delivery_id"], "state": "PENDING",
                "next_attempt_at": next_at}

    def _finish_delivery(self, delivery: dict, *, state: str,
                         response_code: int | None = None,
                         error: str | None = None) -> None:
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE webhook_delivery SET state=?, last_response_code=?,"
                " last_error=?, delivered_at=? WHERE delivery_id=?",
                (state, response_code, error,
                 self._now() if state == "DELIVERED" else None,
                 delivery["delivery_id"]))

    def requeue_stale_deliveries(self) -> int:
        """janitor（T46-47）：租约过期的 `DELIVERING` → `PENDING`。

        只看 `lease_expires_at`，不看 `created_at`：后者会把「刚认领、正在
        发」的行当成垃圾，导致同一事件并发投递两次。`attempt_count` **不增**
        ——这条可能已经送达，收方按 `X-Drama-Delivery-Id` 去重。
        """
        now = self._now()
        with Transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE webhook_delivery SET state='PENDING',"
                " lease_expires_at=NULL"
                " WHERE state='DELIVERING'"
                "   AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
                (now,))
            stale = cursor.rowcount or 0
        if stale:
            self._bump("s46_webhook_requeued_total", stale)
        return stale

    def list_deliveries(self, tenant_id: str, *,
                        subscription_id: str | None = None,
                        state: str | None = None, limit: int = 50) -> dict:
        """只含 `event_id` + `response_code`，**不含** payload 全文（§4.3）。"""
        clauses = ["tenant_id=?"]
        args: list = [tenant_id]
        if subscription_id:
            clauses.append("subscription_id=?")
            args.append(subscription_id)
        if state:
            clauses.append("state=?")
            args.append(state)
        args.append(max(1, min(limit, 200)))
        rows = self.conn.execute(
            "SELECT delivery_id, subscription_id, event_id, event_type, state,"
            " attempt_count, last_response_code, last_error, delivered_at,"
            " created_at FROM webhook_delivery WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT ?", args).fetchall()
        return {"deliveries": [dict(r) for r in rows]}


# ------------------------------------------------------------------ 组装

def build_dependencies(conn: sqlite3.Connection, **kwargs) -> DeveloperIntegrationService:
    """从环境变量装配上游端口等可选依赖。

    每个缺省值都是**保守的**：没有上游 → 转发一律结果未知（不是「没发生」）；
    没有解析器 → webhook 创建被拒（不是静默放行）；没有授权源 → 只信本地
    授权凭证（配置了才 fail closed）。
    """
    kwargs.setdefault(
        "redaction_secret",
        os.environ.get("S46_REDACTION_KEY", "s46-local-redaction-key").encode())
    kwargs.setdefault(
        "secret_key",
        os.environ.get("S46_SECRET_KEY", "s46-local-secret-key").encode())
    kwargs.setdefault(
        "context_key",
        os.environ.get("S46_CONTEXT_KEY", "s46-local-context-key").encode())
    kwargs.setdefault("rate_limiter", LocalTokenBuckets())
    return DeveloperIntegrationService(conn, **kwargs)


# ------------------------------------------------------------------ Webhook 签名

def sign_webhook(secret: str, timestamp: str, raw_body: str) -> str:
    """§3.5：`X-Drama-Signature: t=<ts>,v1=<hex(hmac_sha256(secret, ts.body))>`。

    **签名基于原始字节，不是重新序列化的 JSON**——否则对方按自己的 JSON 库
    重算会不一致。这里 `raw_body` 就是将要发送的字符串本身。
    """
    signed = f"{timestamp}.{raw_body}"
    digest = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_webhook(secret: str, header: str, raw_body: str,
                   tolerance_seconds: int = 300,
                   now_epoch: int | None = None) -> bool:
    """独立复算（T46-30）。timestamp 超 5 分钟 → 拒绝（防重放）。"""
    parts = dict(piece.split("=", 1) for piece in header.split(",")
                 if "=" in piece)
    timestamp, provided = parts.get("t", ""), parts.get("v1", "")
    if not timestamp or not provided:
        return False
    try:
        stamp = int(timestamp)
    except ValueError:
        return False
    current = int(time.time()) if now_epoch is None else now_epoch
    if abs(current - stamp) > tolerance_seconds:
        return False
    expected = sign_webhook(secret, timestamp, raw_body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, provided)


def _seal(secret: str, key: bytes) -> str:
    """把 webhook secret 封成 `local-v1:<nonce>:<ct>:<tag>`。

    仓里没有 KMS（C46-KMS），所以用 HMAC-SHA256 计数器流 + 认证标签。
    满足「明文不落库」与「可解密后验签」；不满足跨实例托管与轮换。
    """
    if not isinstance(secret, str) or not secret:
        raise ServiceError(400, MALFORMED_REQUEST, "secret must be a string")
    nonce = uuid.uuid4().hex
    raw = secret.encode("utf-8")
    stream = _keystream(key, nonce, len(raw))
    cipher = bytes(a ^ b for a, b in zip(raw, stream))
    tag = hmac.new(key, bytes.fromhex(nonce) + cipher,
                   hashlib.sha256).hexdigest()[:32]
    return f"local-v1:{nonce}:{cipher.hex()}:{tag}"


def _open(sealed: str, key: bytes) -> str:
    if not sealed or not sealed.startswith("local-v1:"):
        raise ServiceError(500, "SECRET_UNREADABLE",
                           "subscription secret is not in a readable form")
    _, nonce, cipher_hex, tag = sealed.split(":", 3)
    cipher = bytes.fromhex(cipher_hex)
    expected = hmac.new(key, bytes.fromhex(nonce) + cipher,
                        hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, tag):
        raise ServiceError(500, "SECRET_UNREADABLE",
                           "subscription secret failed authentication")
    stream = _keystream(key, nonce, len(cipher))
    return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8")


def _keystream(key: bytes, nonce: str, length: int) -> bytes:
    nonce_bytes = bytes.fromhex(nonce)
    blocks = []
    counter = 0
    while sum(len(b) for b in blocks) < length:
        blocks.append(hmac.new(key, nonce_bytes + counter.to_bytes(8, "big"),
                               hashlib.sha256).digest())
        counter += 1
    return b"".join(blocks)[:length]


def _literal_ip(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def _is_private_address(address: str) -> bool:
    """私网 / 回环 / 链路本地 / 保留段。E29 的判定核心。"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_unspecified or ip.is_multicast)
