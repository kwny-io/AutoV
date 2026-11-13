"""S27 采集核。语义见 `plans/S27-provider-connectors.md` §5 / §6 / §8 / §9。

S27 是**参数映射器 + 协议处理器 + 观测上报者**。它不选模型、不动预算、
不接受成片、不持调度状态（§1.2 / §7）。所有让字节离开本机的动作只有一处：
`submit`，且它前面有两道门。

## 顺序是语义的一部分

```
熔断检查 → claim → 落 SENDING → COMMIT → 发 HTTP → 更新回执 → 写观测 → COMMIT
```

- **熔断必须在 claim 之前**（§8.1 末段）。否则"熔断打开"时 grant 已被领走，
  S26 要等过期才回收，白占一个派单窗口。
- **先落 SENDING 再发 HTTP**（§5.3）。回执先于字节存在，进程在任意点崩溃都能
  从表里看出"这次提交处于什么阶段"。
- **观测进 Outbox 与回执更新同一个事务**（§9.3 末段）。不能"先发事件再落库"。

## 四态判定（§5.3）

| 结论 | 条件 | 依据 |
|---|---|---|
| `not_sent` | 能证明字节没离开本机：DNS/TCP/TLS/连接池/请求构造 | `phase in PRE_WRITE_PHASES` |
| `accepted` | HTTP 2xx 且解析出 `provider_job_ref` | 响应事实 |
| `rejected` | 4xx 且错误码在"未受理"集合内 | 响应事实 |
| `unknown` | 其余一切：写完后超时/重置/5xx/2xx 无 ref | 最坏假设 |

**崩溃恢复后的 `SENDING` 一律 `unknown`，哪怕 `bytes_written_at IS NULL`**
（§5.3 行 461–463）。`not_sent` 只在运行中的进程能说出口 —— 它有明确的错误
类型；崩溃的进程没有。这条区别是 `recover_stale_sending` 与 `classify_submit`
分开写的原因。

## 与方案的偏离（接 `schema.py` 的 C27-1…3）

- **C27-4：429 判 `not_sent`。** §5.3 把 `not_sent` 定义成"字节没离开本机"，
  而 429 显然收到了响应 —— 两张表在这里冲突。取 §13 的失败分支表：429 表示
  供应商**未受理**，付费风险为零（§13 行 845），且 S26 的 `_on_not_sent` 在
  带 `retry_after` 时走 `SUPPLIER_RATE_LIMITED`，正是"退避后重签"那条路。
  按 §5.3 判 `unknown` 会把一次明确退避送进 `SUBMISSION_UNKNOWN` 对账梯子，
  既贵又掩盖了原因。`bytes_written_at` 在这一支里非空，与"`IS NULL` ⇒
  `not_sent`"不矛盾（那是充分条件，不是必要条件）。
- **C27-5：`aggregate_sequence` 固定填 1。** S27 看不到 job 的完整序列，方案
  §6.5 行 611 明说该由 S26 分配，而 envelope 要求 ≥1（PCR-7）。填 1 是**占位**，
  不来自任何真实知识。S26 的观测消费不读 envelope 的 `sequence`（它用
  `PROGRESS_RANK` 判倒退），所以这个占位不会造成错误推进。
- **C27-6：四个提交结论经事件的 `status` 传达。**
  `connector.observation.received` 的 12 个键里**没有** `error_class` 与
  `retry_after_seconds`（`additionalProperties: false`），而 S26 的
  `_apply_observation` 正是按 `status ∈ SUBMIT_OUTCOMES` + `provider_status_raw
  == "SUBMIT_OUTCOME"` 分派到 `record_submit_outcome` 的。所以 S27 把结论写在
  `status` 上，并带 `provider_status_raw="SUBMIT_OUTCOME"` 作为**标记**，防止
  供应商恰好也返回 `accepted` 这个词时被误判。`error_class` 与
  `retry_after_ms` 只留在本地回执与 `channel_health` 里 —— **契约没有给它们
  位置，这是要 owner 补的缺口，不是本服务能自行填的**（见报告）。
- **C27-7 / C27-9：** 见 `mappers.py`（`status` 取值限于 7 个；`provider_usage`
  提到与 `output` 平级）。
- **C27-8：回调在 ACK 之后才映射。** 入口只做"验签 → 落 Inbox → COMMIT → 200"，
  映射由 `map_pending_callbacks` 在响应之后（或 janitor）执行，UNMAPPED 保留
  直到拿到回执，超 24h 转 `DEAD_LETTER`（§6.2 / §6.4）。
- **C27-10：`cancel` / `manifest` 两个命令事件目前无人发出。** 方案 §9.1 行
  677–679 声明四个命令事件，S27 全部实现并有测试。但仓库里 S26 只发
  `connector.submit.requested` 与 `connector.status.requested`（
  `generation_jobs/service.py:1429`、`:2659`），另两个在 S26 与任何其他服务里
  都不存在。这**不是** S27 可以自行补的：发不发出 S26 的调度语义决定（取消
  从哪里触发、刷新 URL 的时机）。S27 这边保证"收到就正确执行"，
  事件一旦上线即可用；缺口登记在这里，不代 S26 填。
- **C27-12：两处方案承诺没有契约依据，本实现不代填。**
  1. §2.3 的 `max_inflight_per_pod` 本地限流没有实现。全局并发闸由 S26 持有
     S22 permit 保证（同一个 permit 覆盖整个派单窗口），S27 不重复计数是明文
     要求；剩下的那一道"单 Pod 别自己打满"需要一个按渠道与副本数确定的阈值，
     方案没给数，也不该由实现拍。它属于部署参数。
  2. §8.1 的「auth 熔断后上报 S24 `pool_health='unauthorized'`」本服务不直接做。
     §8.2 给的第一个选项是 `connector.health.degraded` 事件，但 `contracts/`
     里**没有**这个事件——新增一个 topic 会让 `README_CLAIMS` 的 55 被钉死的
     计数检查和 `contracts/README.md` 一起变红，而那份 README 在本循环的禁改
     清单里。**§8.2 的第二个选项（S24 probe 周期发现）已经可用**：S24 侧本轮
     补了 `record_probe_outcome(pool_id, status=401|403)`，它按 §4.3 直接把池
     判成 `unauthorized`，并已由 `POST /account-pools/{pool_id}/probe-outcomes`
     暴露。也就是说「401/403 让池停用」这件事在本服务之外有确定落点了。

     仍然**不该由 S27 调用它**：S24 方案 §3.4/§4.3 明文要求 `account_probe` 由
     S24 自有的 `probe-worker` 用独立只读凭证执行、**不经过 S27 业务出站身份**；
     而 S27 在提交路径上根本不知道 `account_pool_id`（`claim` 的返回体、
     `connector_request_receipt`、`submit.requested` 的 payload 里都没有这个
     字段）。要让 S27 报池，得先给这三处加字段——那是契约变更，不是实现选择。
     这一条留给 owner 定：要么 S24 的 probe-worker 上线，要么给 S27 的提交链路
     补池标识。本服务不代填。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from provider_connectors.mappers import (
    OBSERVATION_STATUSES,
    SUBMIT_OUTCOME_MARKER,
    SUBMIT_OUTCOMES,
    SubmitContext,
    SubmitPayloadMapper,
)
from provider_connectors.schema import SCHEMA

# §5.2 门 1：**编译期常量**，不是 env，也不是配置项。
# 这个模块不 import os —— T27-25 用 `ast` 扫描断言这一点。
REQUIRE_DISPATCH_GRANT = True

# §5.5 / §16 未决：各渠道实测前统一 60s。
SUBMIT_HTTP_TIMEOUT_SECONDS = 60.0

# §8.1：连续 5 次 transport / provider_5xx → open 30s。
BREAKER_THRESHOLD = 5
BREAKER_OPEN_MS = 30_000

# §8.1：连续失败只数这两类。`timeout` 是"写完之后超时"，与 transport 同族。
BREAKER_FAILURE_CLASSES = frozenset({"transport", "provider_5xx", "timeout"})

# §6.4 第 4 步：UNMAPPED 保留 24h。
UNMAPPED_TTL_SECONDS = 86400

# §9.1 的四个命令事件。
SUBMIT_REQUESTED = "connector.submit.requested"
STATUS_REQUESTED = "connector.status.requested"
CANCEL_REQUESTED = "connector.cancel.requested"
MANIFEST_REQUESTED = "connector.manifest.requested"
COMMAND_EVENTS = (SUBMIT_REQUESTED, STATUS_REQUESTED, CANCEL_REQUESTED,
                  MANIFEST_REQUESTED)

# §9.2 S27 只发这一种。
OBSERVATION_RECEIVED = "connector.observation.received"

# §5.3 的 phase 分类。`build` 也算 pre-write：请求还没构造完，字节一定没出去。
PRE_WRITE_PHASES = frozenset({"dns", "connect", "tls", "pool", "build"})
POST_WRITE_PHASES = frozenset({"timeout", "reset", "partial"})

# §13：这些 `error_class` 是"字节没出去"的，可安全重试。
NOT_SENT_ERROR_CLASSES = frozenset({"transport", "channel_circuit_open",
                                    "grant_unavailable", "rate_limit"})


# ---------------------------------------------------------------- 时间与序列化

def _ts(value: datetime) -> str:
    """固定宽度 27 字符 UTC；字符串序 == 时间序（同 S21/S22/S23/S26）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _shift(ts: str, seconds: float) -> str:
    return _ts(_parse_ts(ts) + timedelta(seconds=seconds))


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 错误

class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


class ClaimUnavailable(Exception):
    """S26 的 claim 明确拒绝（404/403/409/410）。**不是**网络失败。

    区别很重要：拒绝意味着"这个 grant 已经不能用了"，S27 报 `not_sent`；
    网络失败意味着"我不知道能不能用"，S27 什么都不报、留着下次再来（§5.1）。
    """

    def __init__(self, code: str, status: int = 409, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = details or {}


class UnknownChannel(Exception):
    """没有这个渠道的适配器 —— 例如 `sora/historical`（§2.1）。

    **不 ACK、不发事件、不猜**：让上游重投，直到 dead-letter 告警。
    """


# ---------------------------------------------------------------- 传输端口

@dataclass
class TransportResult:
    """一次出站调用的事实。`status=0` 表示没拿到 HTTP 状态。"""

    status: int
    body: Any = None
    retry_after_ms: int | None = None


class TransportError(Exception):
    """`phase` 决定结论，`error_class` 决定熔断与 S26 的分支。"""

    def __init__(self, phase: str, message: str = "",
                 error_class: str | None = None):
        super().__init__(message or phase)
        self.phase = phase
        self.error_class = error_class or (
            "transport" if phase in PRE_WRITE_PHASES else "timeout")


def classify_submit(*, phase: str, http_status: int | None = None,
                    provider_ref: str | None = None,
                    rejection: str | None = None,
                    error_class: str | None = None) -> tuple[str, str | None]:
    """§5.3 的四态判定。**纯函数**，四个分支各有一条测试。

    返回 `(outcome, error_class)`。
    """
    if phase in PRE_WRITE_PHASES:
        return "not_sent", error_class or "transport"
    if phase in POST_WRITE_PHASES:
        # 字节已写出：任何失败都是 unknown，哪怕看起来像"连接被拒"。
        return "unknown", error_class or (
            "timeout" if phase == "timeout" else "transport")
    if http_status is None:
        return "unknown", error_class or "bad_response"
    if 200 <= http_status < 300:
        # 2xx 但解析不出 ref 也算 unknown：供应商可能建了任务却没告诉我们 id。
        return ("accepted", None) if provider_ref else ("unknown", "bad_response")
    if http_status == 429:
        # C27-4：§13 明列"未受理，付费风险无"。S26 带 retry_after 时走退避。
        return "not_sent", error_class or "rate_limit"
    if 400 <= http_status < 500:
        if rejection is not None:
            return "rejected", rejection
        # 看不清错误码的 4xx：不能证明未受理 → 最坏假设。
        return "unknown", error_class or "bad_response"
    return "unknown", error_class or "provider_5xx"


# ---------------------------------------------------------------- 服务

class ProviderConnectorsService:
    """一个进程只管一个渠道（§2.1）。`channels` 只含本进程注册的适配器。"""

    source = "S27"

    def __init__(self, conn, *, channels: list | None = None,
                 transport: Callable[[str, dict], TransportResult] | None = None,
                 claim_grant: Callable[[str, str, dict], dict] | None = None,
                 secret_for: Callable[[str], bytes | None] | None = None,
                 now: Callable[[], str] | None = None,
                 home_cell: str = "cell_a1"):
        self.conn = conn
        self.channels: dict[str, Any] = {
            c.channel_route: c for c in (channels or [])}
        self.transport = transport
        self.claim_grant_port = claim_grant
        self.secret_for = secret_for
        self._now = now or (lambda: _ts(datetime.now(timezone.utc)))
        self.home_cell = home_cell
        self._tx_depth = [0]
        # `Outbox.__init__` 跑的是 `executescript`，而 pysqlite 的 `executescript`
        # 会**先提交当前事务**。在 `_emit` 里现构造就等于每次发事件都掐断调用方的
        # 事务。所以只在装配时建一次表，之后复用同一个实例。
        from event_sdk import Outbox
        self._outbox = Outbox(conn)

    # ---- 基础设施 ------------------------------------------------------

    class _Tx:
        def __init__(self, conn, depth):
            self.conn = conn
            self.depth = depth
            self.outermost = False

        def __enter__(self):
            self.outermost = self.depth[0] == 0
            if self.outermost:
                self.conn.execute("BEGIN IMMEDIATE")
            self.depth[0] += 1
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            self.depth[0] -= 1
            if not self.outermost:
                return False
            self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
            return False

    def _tx(self):
        return self._Tx(self.conn, self._tx_depth)

    def channel(self, channel_route: str) -> Any:
        impl = self.channels.get(channel_route)
        if impl is None:
            raise UnknownChannel(channel_route)
        return impl

    def _port(self, name: str, port):
        if port is None:
            raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                               f"{name} is not wired", retryable=True,
                               details={"port": name})
        return port

    # ---- 事件出站 ------------------------------------------------------

    def _emit(self, *, tenant_id: str, type_: str, aggregate_id: str,
              data: dict, correlation_id: str = "",
              causation_id: str | None = None,
              home_cell: str | None = None) -> str:
        """写 Outbox。**调用方必须在事务里调用它**（§9.3）。

        只接受能关联的观测：`aggregate_id` 必须非空。UNMAPPED 回调不进这里，
        它留在 `connector_callback_inbox` 等回执到达（PCR-7 / C27-8）。
        """
        from event_sdk import new_event

        seq = 1                      # C27-5：S27 不持有序列知识
        if not aggregate_id:
            raise ServiceError(500, "OBSERVATION_UNMAPPABLE",
                               "an observation must carry aggregate_id")
        envelope = new_event(
            source=self.source, type=type_,
            subject=f"connector-observation/{aggregate_id}",
            tenant_id=tenant_id, home_cell=home_cell or self.home_cell,
            aggregate_id=aggregate_id, sequence=seq,
            correlation_id=correlation_id, causation_id=causation_id,
            data={"schema_version": "2.0", **data})
        self._outbox.append(envelope)
        return envelope.id

    def _emit_observation(self, *, tenant_id: str, channel_route: str,
                          dispatch_id: str, stable_client_key: str,
                          provider_job_id: str, provider_job_ref: str | None,
                          source: str, provider_status_raw: str,
                          status: str, terminal: bool,
                          output: dict | None = None,
                          provider_usage: dict | None = None,
                          correlation_id: str = "",
                          causation_id: str | None = None) -> str:
        """§6.5 的 12 个键，一个不多一个不少（`additionalProperties: false`）。

        `status` 的两类合法值：7 个供应商状态，或 4 个提交结论（C27-6）。后者
        只在 `provider_status_raw == "SUBMIT_OUTCOME"` 时允许 —— 别的组合是
        代码写错了，归一到 `inconclusive` 而不是发出去。
        """
        is_outcome = provider_status_raw == SUBMIT_OUTCOME_MARKER
        if is_outcome:
            if status not in SUBMIT_OUTCOMES:
                raise ServiceError(500, "BAD_OUTCOME", status)
        elif status not in OBSERVATION_STATUSES:
            # C27-7：契约不收 enum，但 S27 只发这 7 个。防御性归一。
            status = "inconclusive"
        return self._emit(
            tenant_id=tenant_id, type_=OBSERVATION_RECEIVED,
            aggregate_id=provider_job_id,
            correlation_id=correlation_id, causation_id=causation_id,
            data={
                "channel_route": channel_route,
                "dispatch_id": dispatch_id,
                "stable_client_key": stable_client_key,
                "provider_job_ref": provider_job_ref,
                "source": source,
                "observed_at": self._now(),
                "provider_status_raw": provider_status_raw,
                "status": status,
                "terminal": bool(terminal),
                "output": output,
                "provider_usage": provider_usage,
            })

    def _emit_submit_outcome(self, *, tenant_id: str, channel_route: str,
                             dispatch_id: str, stable_client_key: str,
                             provider_job_id: str,
                             provider_job_ref: str | None, outcome: str,
                             correlation_id: str = "",
                             causation_id: str | None = None) -> str:
        """C27-6：结论写在 `status` 上，`provider_status_raw` 是标记而不是原文。"""
        if outcome not in SUBMIT_OUTCOMES:      # 拼错就发不出去，不静默
            raise ServiceError(500, "BAD_OUTCOME", outcome)
        return self._emit_observation(
            tenant_id=tenant_id, channel_route=channel_route,
            dispatch_id=dispatch_id, stable_client_key=stable_client_key,
            provider_job_id=provider_job_id,
            provider_job_ref=provider_job_ref, source="poll",
            provider_status_raw=SUBMIT_OUTCOME_MARKER,
            status=outcome, terminal=False, output=None, provider_usage=None,
            correlation_id=correlation_id, causation_id=causation_id)

    # ---- 命令去重（C27-1）----------------------------------------------

    def _command_begin(self, consumer_id: str, envelope: dict) -> bool:
        """返回 True 表示"这次该处理"。`processed_at` 非空就是处理完了。

        与平台 `Inbox` 的区别：那个一见到 `event_id` 就永远返回 False，
        而 §5.1 要求"claim 网络失败 → 下次再来"。
        """
        event_id = envelope.get("id") or ""
        data = envelope.get("data") or {}
        row = self.conn.execute(
            "SELECT processed_at FROM connector_command_inbox"
            " WHERE consumer_id = ? AND event_id = ?",
            (consumer_id, event_id)).fetchone()
        if row is not None and row["processed_at"]:
            return False
        with self._tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO connector_command_inbox"
                " (consumer_id, event_id, event_type, aggregate_id,"
                "  payload_hash, received_at, attempts)"
                " VALUES (?, ?, ?, ?, ?, ?, 0)",
                (consumer_id, event_id, envelope.get("type") or "",
                 envelope.get("aggregate_id"), _sha256(_dumps(data)),
                 self._now()))
            self.conn.execute(
                "UPDATE connector_command_inbox SET attempts = attempts + 1"
                " WHERE consumer_id = ? AND event_id = ?",
                (consumer_id, event_id))
        return True

    def _command_done(self, consumer_id: str, event_id: str) -> None:
        self.conn.execute(
            "UPDATE connector_command_inbox SET processed_at = ?"
            " WHERE consumer_id = ? AND event_id = ?",
            (self._now(), consumer_id, event_id))

    # ---- 熔断（§8.1）---------------------------------------------------

    def health(self, channel_route: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM channel_health WHERE channel_route = ?",
            (channel_route,)).fetchone()
        if row is None:
            return {"channel_route": channel_route, "breaker_state": "closed",
                    "consecutive_fail": 0, "opened_at": None,
                    "retry_after_ms": None, "last_error_class": None,
                    "updated_at": self._now()}
        return {k: row[k] for k in row.keys()}

    def _breaker_allows(self, channel_route: str) -> bool:
        """`open` 到点转 `half_open` 并放**一个**请求；auth 引起的 open 不放。

        判定与转移在同一个事务里做，两个副本同时探也不会重复放行 ——
        `half_open` 之后仍然"允许一次"是因为真实实现里由单副本持锁调用；
        这里的状态位只保证"不会在 open 期间放行"。
        """
        state = self.health(channel_route)
        if state["breaker_state"] == "closed":
            return True
        if state["breaker_state"] == "half_open":
            return True
        retry_after = state["retry_after_ms"]
        if retry_after is None:
            return False                      # 等人处理（auth）
        if _parse_ts(
                _shift(state["opened_at"], retry_after / 1000.0)
        ) <= _parse_ts(self._now()):
            with self._tx():
                self._put_health(channel_route, breaker_state="half_open",
                                 consecutive_fail=state["consecutive_fail"],
                                 opened_at=state["opened_at"],
                                 retry_after_ms=retry_after,
                                 last_error_class=state["last_error_class"])
            return True
        return False

    def _put_health(self, channel_route: str, *, breaker_state: str,
                    consecutive_fail: int, opened_at: str | None,
                    retry_after_ms: int | None,
                    last_error_class: str | None) -> None:
        self.conn.execute(
            "INSERT INTO channel_health (channel_route, breaker_state,"
            " consecutive_fail, opened_at, retry_after_ms, last_error_class,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(channel_route) DO UPDATE SET"
            "  breaker_state = excluded.breaker_state,"
            "  consecutive_fail = excluded.consecutive_fail,"
            "  opened_at = excluded.opened_at,"
            "  retry_after_ms = excluded.retry_after_ms,"
            "  last_error_class = excluded.last_error_class,"
            "  updated_at = excluded.updated_at",
            (channel_route, breaker_state, consecutive_fail, opened_at,
             retry_after_ms, last_error_class, self._now()))

    def _record_failure(self, channel_route: str, error_class: str,
                        retry_after_ms: int | None = None) -> None:
        state = self.health(channel_route)
        if error_class == "auth":
            # §8.1：401/403 立即 open，**不自动恢复**（retry_after NULL）。
            self._put_health(channel_route, breaker_state="open",
                             consecutive_fail=state["consecutive_fail"] + 1,
                             opened_at=self._now(), retry_after_ms=None,
                             last_error_class=error_class)
            return
        if error_class == "rate_limit":
            # 429 不熔断，只记住供应商要求的退避（§8.1 末条）。
            self._put_health(channel_route,
                             breaker_state=state["breaker_state"],
                             consecutive_fail=state["consecutive_fail"],
                             opened_at=state["opened_at"],
                             retry_after_ms=retry_after_ms,
                             last_error_class=error_class)
            return
        if error_class not in BREAKER_FAILURE_CLASSES:
            self._put_health(channel_route,
                             breaker_state=state["breaker_state"],
                             consecutive_fail=state["consecutive_fail"],
                             opened_at=state["opened_at"],
                             retry_after_ms=state["retry_after_ms"],
                             last_error_class=error_class)
            return
        fails = state["consecutive_fail"] + 1
        if state["breaker_state"] == "half_open" or fails >= BREAKER_THRESHOLD:
            self._put_health(channel_route, breaker_state="open",
                             consecutive_fail=fails, opened_at=self._now(),
                             retry_after_ms=BREAKER_OPEN_MS,
                             last_error_class=error_class)
        else:
            self._put_health(channel_route, breaker_state=state["breaker_state"],
                             consecutive_fail=fails,
                             opened_at=state["opened_at"],
                             retry_after_ms=state["retry_after_ms"],
                             last_error_class=error_class)

    def _record_success(self, channel_route: str) -> None:
        self._put_health(channel_route, breaker_state="closed",
                         consecutive_fail=0, opened_at=None,
                         retry_after_ms=None, last_error_class=None)

    # ---- 回执 ----------------------------------------------------------

    def _receipt_for_dispatch(self, dispatch_id: str):
        return self.conn.execute(
            "SELECT * FROM connector_request_receipt WHERE dispatch_id = ?",
            (dispatch_id,)).fetchone()

    def get_receipt(self, tenant_id: str, receipt_id: str) -> dict:
        """`GET /connector-receipts/{id}`（catalog 的 `query_api`）。"""
        row = self.conn.execute(
            "SELECT * FROM connector_request_receipt WHERE receipt_id = ?"
            " AND tenant_id = ?", (receipt_id, tenant_id)).fetchone()
        if row is None:
            raise ServiceError(404, "RECEIPT_NOT_FOUND", "receipt not found",
                               details={"receipt_id": receipt_id})
        keys = ("receipt_id", "dispatch_id", "provider_job_id", "tenant_id",
                "channel_route", "stable_client_key", "fencing_token",
                "request_hash", "state", "outcome", "http_status",
                "provider_job_ref", "error_class", "request_bytes_sha256",
                "sent_at", "responded_at", "bytes_written_at", "created_at")
        return {k: row[k] for k in keys}

    def list_receipts(self, tenant_id: str, provider_job_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT receipt_id FROM connector_request_receipt"
            " WHERE tenant_id = ? AND provider_job_id = ?"
            " ORDER BY created_at", (tenant_id, provider_job_id)).fetchall()
        return [self.get_receipt(tenant_id, r["receipt_id"]) for r in rows]

    # ---- 命令分派 ------------------------------------------------------

    def consume(self, envelope: dict, *, consumer_id: str = "s27") -> dict:
        """一个命令事件的入口。返回处理摘要，便于 HTTP 层与测试观察。"""
        type_ = envelope.get("type") or ""
        if type_ not in COMMAND_EVENTS:
            raise ServiceError(422, "UNSUPPORTED_EVENT",
                               f"{type_} is not a connector command",
                               details={"type": type_})
        data = envelope.get("data") or {}
        channel_route = data.get("channel_route") or ""
        self.channel(channel_route)          # UnknownChannel → 不 ACK

        if not self._command_begin(consumer_id, envelope):
            return {"applied": False, "reason": "DUPLICATE", "type": type_}
        try:
            result = {
                SUBMIT_REQUESTED: self._on_submit,
                STATUS_REQUESTED: self._on_status,
                CANCEL_REQUESTED: self._on_cancel,
                MANIFEST_REQUESTED: self._on_manifest,
            }[type_](envelope, data)
        except Exception:
            # claim 网络失败等：`processed_at` 留空，下次重投继续。
            raise
        with self._tx():
            self._command_done(consumer_id, envelope.get("id") or "")
        return {**result, "type": type_}

    # ---- submit（唯一付费）---------------------------------------------

    def _on_submit(self, envelope: dict, data: dict) -> dict:
        """§5.1 的完整协议。"""
        if not REQUIRE_DISPATCH_GRANT:       # pragma: no cover - 常量恒真
            raise ServiceError(500, "GRANT_GATE_DISABLED",
                               "REQUIRE_DISPATCH_GRANT must stay true")
        channel_route = data["channel_route"]
        impl: SubmitPayloadMapper = self.channel(channel_route)
        tenant_id = data.get("tenant_id") or envelope.get("tenant_id") or ""
        if not tenant_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "tenant_id is required")
        dispatch_id = data.get("dispatch_id") or ""
        provider_job_id = data.get("provider_job_id") or ""
        stable_client_key = data.get("stable_client_key") or ""
        fencing_token = data.get("fencing_token")
        for name, value in (("dispatch_id", dispatch_id),
                            ("provider_job_id", provider_job_id),
                            ("stable_client_key", stable_client_key)):
            if not value:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"{name} is required",
                                   details={"field": name})
        if fencing_token is None:
            raise ServiceError(400, "MALFORMED_REQUEST", "fencing_token is required")
        if data.get("provider_idempotency") and \
                data["provider_idempotency"] != impl.idempotency_class:
            # §5.4：S26 记的档位与适配器实测的档位不一致 → 两侧对"重发是否安全"
            # 有不同答案，此时发出去就是拿重复付费赌一个实现细节。
            raise ServiceError(
                409, "IDEMPOTENCY_CLASS_MISMATCH",
                "grant says one idempotency class, adapter says another",
                details={"grant": data["provider_idempotency"],
                         "adapter": impl.idempotency_class})

        existing = self._receipt_for_dispatch(dispatch_id)
        if existing is not None:
            # 第二道门的前半：dispatch_id 已经发过。**不重发**（§5.2）。
            return {"applied": False, "reason": "RECEIPT_EXISTS",
                    "receipt_id": existing["receipt_id"],
                    "outcome": existing["outcome"]}

        # ① 熔断在 claim 之前（§8.1 末段）。
        if not self._breaker_allows(channel_route):
            with self._tx():
                self._emit_submit_outcome(
                    tenant_id=tenant_id, channel_route=channel_route,
                    dispatch_id=dispatch_id,
                    stable_client_key=stable_client_key,
                    provider_job_id=provider_job_id, provider_job_ref=None,
                    outcome="not_sent",
                    correlation_id=data.get("correlation_id")
                    or envelope.get("correlation_id") or "",
                    causation_id=envelope.get("id"))
            return {"applied": True, "outcome": "not_sent",
                    "error_class": "channel_circuit_open", "claimed": False}

        # ② claim。
        claim = self._port("claim_grant", self.claim_grant_port)
        try:
            granted = claim(tenant_id, dispatch_id, {
                "executor_id": self._executor_id(channel_route),
                "fencing_token": int(fencing_token),
                "request_hash": data.get("request_hash"),
            })
        except ClaimUnavailable:
            # §13 行 843：grant 已领/过期/撤销 → 不 submit，报 not_sent。
            with self._tx():
                self._emit_submit_outcome(
                    tenant_id=tenant_id, channel_route=channel_route,
                    dispatch_id=dispatch_id,
                    stable_client_key=stable_client_key,
                    provider_job_id=provider_job_id, provider_job_ref=None,
                    outcome="not_sent",
                    correlation_id=data.get("correlation_id")
                    or envelope.get("correlation_id") or "",
                    causation_id=envelope.get("id"))
            return {"applied": True, "outcome": "not_sent",
                    "error_class": "grant_unavailable", "claimed": False}
        # 其它异常（网络失败）不捕获：`processed_at` 留空，下次重投再 claim。

        payload = data.get("submit_payload_inline") or {}
        ctx = SubmitContext(
            dispatch_id=dispatch_id,
            provider_job_id=granted.get("provider_job_id") or provider_job_id,
            tenant_id=tenant_id,
            stable_client_key=granted.get("stable_client_key")
            or stable_client_key,
            fencing_token=int(granted.get("fencing_token") or fencing_token),
            deadline=str(granted.get("deadline") or ""),
            callback_url=self._callback_url(channel_route),
            traceparent=envelope.get("traceparent") or "")
        request = {
            "op": "submit",
            "channel_route": channel_route,
            "native": impl.to_native(payload, ctx),
            "timeout_seconds": SUBMIT_HTTP_TIMEOUT_SECONDS,
        }
        request_bytes = _dumps(request["native"])

        receipt_id = "rcp_" + uuid.uuid4().hex
        # ③ 落 SENDING 并 COMMIT —— 必须在字节之前（§5.3）。
        with self._tx():
            try:
                self.conn.execute(
                    "INSERT INTO connector_request_receipt (receipt_id,"
                    " dispatch_id, provider_job_id, tenant_id, channel_route,"
                    " stable_client_key, fencing_token, request_hash, state,"
                    " request_bytes_sha256, sent_at, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SENDING', ?, ?, ?)",
                    (receipt_id, dispatch_id,
                     ctx.provider_job_id, tenant_id, channel_route,
                     ctx.stable_client_key, ctx.fencing_token,
                     str(granted.get("request_hash") or data.get("request_hash") or ""),
                     _sha256(request_bytes), self._now(), self._now()))
            except Exception as exc:          # UNIQUE(dispatch_id) / ux_receipt_client_key
                if "UNIQUE" in str(exc).upper():
                    raise ServiceError(
                        409, "RECEIPT_CONFLICT",
                        "a receipt already exists for this dispatch_id or"
                        " stable_client_key; refusing to re-send",
                        details={"dispatch_id": dispatch_id,
                                 "stable_client_key": ctx.stable_client_key})
                raise

        # ④ 发 HTTP。
        outcome, error_class, http_status, body, retry_after_ms = \
            self._send_submit(impl, channel_route, request)
        provider_ref = impl.provider_ref_from(body) if outcome == "accepted" else None
        state = "RESPONDED" if http_status else "ABANDONED"

        # ⑤ 回执 + 观测 + 本地索引，同一个事务（§9.3）。
        with self._tx():
            self.conn.execute(
                "UPDATE connector_request_receipt SET state = ?, outcome = ?,"
                " http_status = ?, provider_job_ref = ?, error_class = ?,"
                " bytes_written_at = ?, responded_at = ? WHERE receipt_id = ?",
                (state, outcome, http_status, provider_ref, error_class,
                 self._now() if http_status is not None or outcome == "unknown"
                 else None, self._now(), receipt_id))
            if impl.idempotency_class == "unsupported" and provider_ref:
                # §5.4：S26 对账时的唯一本地证据。
                self.conn.execute(
                    "INSERT OR IGNORE INTO connector_client_key_index"
                    " (stable_client_key, dispatch_id, provider_job_ref,"
                    "  channel_route, tenant_id, first_seen_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (ctx.stable_client_key, dispatch_id, provider_ref,
                     channel_route, tenant_id, self._now()))
            self._emit_submit_outcome(
                tenant_id=tenant_id, channel_route=channel_route,
                dispatch_id=dispatch_id,
                stable_client_key=ctx.stable_client_key,
                provider_job_id=ctx.provider_job_id,
                provider_job_ref=provider_ref, outcome=outcome,
                correlation_id=data.get("correlation_id")
                or envelope.get("correlation_id") or "",
                causation_id=envelope.get("id"))

        if error_class:
            self._record_failure(channel_route, error_class, retry_after_ms)
        else:
            self._record_success(channel_route)

        # 回调可能早于这个响应（E17）：拿到回执后回填 UNMAPPED。
        if provider_ref:
            self.map_pending_callbacks(channel_route)

        return {"applied": True, "outcome": outcome, "receipt_id": receipt_id,
                "provider_job_ref": provider_ref, "http_status": http_status,
                "error_class": error_class, "claimed": True}

    def _send_submit(self, impl, channel_route: str, request: dict):
        """返回 `(outcome, error_class, http_status, body, retry_after_ms)`。"""
        transport = self._port("transport", self.transport)
        try:
            result = transport("submit", request)
        except TransportError as exc:
            outcome, error_class = classify_submit(phase=exc.phase,
                                                   error_class=exc.error_class)
            return outcome, error_class, None, None, None
        http_status = int(result.status or 0)
        body = result.body
        rejection = impl.classify_rejection(http_status, body) \
            if http_status else None
        outcome, error_class = classify_submit(
            phase="response", http_status=http_status,
            provider_ref=impl.provider_ref_from(body), rejection=rejection)
        return (outcome, error_class, http_status or None, body,
                result.retry_after_ms)

    # ---- get_status ----------------------------------------------------

    def _on_status(self, envelope: dict, data: dict) -> dict:
        channel_route = data["channel_route"]
        impl = self.channel(channel_route)
        tenant_id = data.get("tenant_id") or envelope.get("tenant_id") or ""
        provider_job_id = data.get("provider_job_id") or ""
        provider_job_ref = data.get("provider_job_ref")
        if not provider_job_ref:
            # 还没拿到供应商 id：没东西可查，也别假装查过。
            return {"applied": False, "reason": "NO_PROVIDER_JOB_REF"}
        transport = self._port("transport", self.transport)
        try:
            result = transport("status", {
                "channel_route": channel_route,
                "native": {"provider_job_ref": provider_job_ref,
                           "client_key": data.get("stable_client_key")},
                "timeout_seconds": 30.0})
        except TransportError as exc:
            return self._emit_inconclusive(
                channel_route=channel_route, tenant_id=tenant_id,
                data=data, provider_job_id=provider_job_id,
                provider_job_ref=provider_job_ref,
                raw=f"TRANSPORT_{exc.phase.upper()}",
                envelope=envelope, error_class=exc.error_class)
        if not 200 <= int(result.status or 0) < 300:
            # 查不动的原因可能是 5xx / 429；状态结论是"不知道"。
            error_class = ("rate_limit" if result.status == 429
                           else "provider_5xx" if result.status and
                           result.status >= 500 else "bad_response")
            return self._emit_inconclusive(
                channel_route=channel_route, tenant_id=tenant_id,
                data=data, provider_job_id=provider_job_id,
                provider_job_ref=provider_job_ref,
                raw=f"HTTP_{result.status}",
                envelope=envelope, error_class=error_class)

        obs = impl.from_native_status(result.body)
        with self._tx():
            self._emit_observation(
                tenant_id=tenant_id, channel_route=channel_route,
                dispatch_id=data.get("dispatch_id") or "",
                stable_client_key=data.get("stable_client_key") or "",
                provider_job_id=provider_job_id,
                provider_job_ref=obs.get("provider_job_ref") or provider_job_ref,
                source="poll",
                provider_status_raw=obs["provider_status_raw"],
                status=obs["status"], terminal=obs["terminal"],
                output=obs.get("output"),
                provider_usage=obs.get("provider_usage"),
                correlation_id=envelope.get("correlation_id") or "",
                causation_id=envelope.get("id"))
        return {"applied": True, "status": obs["status"],
                "provider_status_raw": obs["provider_status_raw"]}

    def _emit_inconclusive(self, *, channel_route: str, tenant_id: str,
                           data: dict, provider_job_id: str,
                           provider_job_ref: str | None, raw: str,
                           envelope: dict, error_class: str | None = None) -> dict:
        """查不清就报 `inconclusive` / `terminal=False`，**不改 S26 的状态**。

        `error_class=None` 用于**配置层面**的查不动（渠道不支持取消、没有
        `provider_job_ref`、manifest 为空）：那是我们的路由问题或还没拿到 id，
        不是这个渠道此刻不可用，记进 `channel_health` 只会污染熔断判断。
        """
        with self._tx():
            self._emit_observation(
                tenant_id=tenant_id, channel_route=channel_route,
                dispatch_id=data.get("dispatch_id") or "",
                stable_client_key=data.get("stable_client_key") or "",
                provider_job_id=provider_job_id,
                provider_job_ref=provider_job_ref, source="poll",
                provider_status_raw=raw, status="inconclusive",
                terminal=False, output=None, provider_usage=None,
                correlation_id=envelope.get("correlation_id") or "",
                causation_id=envelope.get("id"))
        if error_class is not None:
            self._record_failure(channel_route, error_class)
        return {"applied": True, "status": "inconclusive",
                "error_class": error_class}

    # ---- cancel（§3.6）-------------------------------------------------

    def _on_cancel(self, envelope: dict, data: dict) -> dict:
        channel_route = data["channel_route"]
        impl = self.channel(channel_route)
        tenant_id = data.get("tenant_id") or envelope.get("tenant_id") or ""
        provider_job_ref = data.get("provider_job_ref")
        if impl.cancel_semantics == "unsupported" or not provider_job_ref:
            return self._emit_inconclusive(
                channel_route=channel_route, tenant_id=tenant_id, data=data,
                provider_job_id=data.get("provider_job_id") or "",
                provider_job_ref=provider_job_ref,
                raw="CANCEL_UNSUPPORTED" if impl.cancel_semantics == "unsupported"
                else "NO_PROVIDER_JOB_REF",
                envelope=envelope)
        transport = self._port("transport", self.transport)
        try:
            result = transport("cancel", {
                "channel_route": channel_route,
                "native": impl.native_cancel(provider_job_ref),
                "timeout_seconds": 30.0})
        except TransportError as exc:
            return self._emit_inconclusive(
                channel_route=channel_route, tenant_id=tenant_id, data=data,
                provider_job_id=data.get("provider_job_id") or "",
                provider_job_ref=provider_job_ref,
                raw=f"TRANSPORT_{exc.phase.upper()}",
                envelope=envelope, error_class=exc.error_class)
        body = result.body if isinstance(result.body, dict) else {}
        canceled = bool(body.get("canceled"))
        if canceled:
            status, raw, terminal = "canceled", "CANCELED", True
        elif body.get("reason") == "not_cancelable":
            # S26 保持 RUNNING 并加 `discard_on_completion`；我们只报事实。
            status, raw, terminal = "running", "NOT_CANCELABLE", False
        elif body.get("reason") == "cancel_requested":
            status, raw, terminal = "running", "CANCEL_REQUESTED", False
        else:
            status, raw, terminal = "inconclusive", "CANCEL_UNKNOWN", False
        with self._tx():
            self._emit_observation(
                tenant_id=tenant_id, channel_route=channel_route,
                dispatch_id=data.get("dispatch_id") or "",
                stable_client_key=data.get("stable_client_key") or "",
                provider_job_id=data.get("provider_job_id") or "",
                provider_job_ref=provider_job_ref, source="poll",
                provider_status_raw=raw, status=status, terminal=terminal,
                output=None, provider_usage=None,
                correlation_id=envelope.get("correlation_id") or "",
                causation_id=envelope.get("id"))
        return {"applied": True, "canceled": canceled, "status": status}

    # ---- fetch_manifest（§3.8）-----------------------------------------

    def _on_manifest(self, envelope: dict, data: dict) -> dict:
        channel_route = data["channel_route"]
        impl = self.channel(channel_route)
        tenant_id = data.get("tenant_id") or envelope.get("tenant_id") or ""
        provider_job_ref = data.get("provider_job_ref")
        refresh = bool(data.get("refresh_urls"))
        if not provider_job_ref:
            return self._emit_inconclusive(
                channel_route=channel_route, tenant_id=tenant_id, data=data,
                provider_job_id=data.get("provider_job_id") or "",
                provider_job_ref=None, raw="NO_PROVIDER_JOB_REF",
                envelope=envelope)
        if refresh and not impl.refresh_url_supported:
            # §3.8 / T27-23：不支持刷新的渠道明确回 `refresh_supported: false`，
            # S26 据此走 §8.3 的补救路径（不能靠"没拿到新 URL"去猜）。
            self._emit_inconclusive(
                channel_route=channel_route, tenant_id=tenant_id, data=data,
                provider_job_id=data.get("provider_job_id") or "",
                provider_job_ref=provider_job_ref, raw="REFRESH_UNSUPPORTED",
                envelope=envelope)
            return {"applied": False, "refresh_supported": False,
                    "reason": "REFRESH_UNSUPPORTED"}
        transport = self._port("transport", self.transport)
        try:
            result = transport("manifest", {
                "channel_route": channel_route,
                "native": impl.native_manifest(provider_job_ref, refresh),
                "timeout_seconds": 30.0})
        except TransportError as exc:
            return self._emit_inconclusive(
                channel_route=channel_route, tenant_id=tenant_id, data=data,
                provider_job_id=data.get("provider_job_id") or "",
                provider_job_ref=provider_job_ref,
                raw=f"TRANSPORT_{exc.phase.upper()}",
                envelope=envelope, error_class=exc.error_class)
        body = result.body if isinstance(result.body, dict) else {}
        manifest = body.get("manifest")
        if not manifest:
            return self._emit_inconclusive(
                channel_route=channel_route, tenant_id=tenant_id, data=data,
                provider_job_id=data.get("provider_job_id") or "",
                provider_job_ref=provider_job_ref, raw="MANIFEST_EMPTY",
                envelope=envelope)
        with self._tx():
            # 观测的 `status` 不因"取到新 URL"而改变：这条只说"输出还在"。
            self._emit_observation(
                tenant_id=tenant_id, channel_route=channel_route,
                dispatch_id=data.get("dispatch_id") or "",
                stable_client_key=data.get("stable_client_key") or "",
                provider_job_id=data.get("provider_job_id") or "",
                provider_job_ref=provider_job_ref, source="poll",
                provider_status_raw="MANIFEST_REFRESHED", status="inconclusive",
                terminal=False, output={"manifest": manifest},
                provider_usage=None,
                correlation_id=envelope.get("correlation_id") or "",
                causation_id=envelope.get("id"))
        return {"applied": True, "manifest": manifest,
                "refresh_supported": True}

    # ---- 回调（§6）-----------------------------------------------------

    def receive_callback(self, channel_route: str, raw_body: bytes,
                         headers: dict, *, now_epoch: float | None = None) -> dict:
        """验签 → 落 Inbox → COMMIT。**映射不在这里**（ACK-first，§6.2）。

        `signature_ok` 恒为 1：验签失败根本不入库（§6.3 末段）。
        """
        impl = self.channel(channel_route)
        secret_port = self._port("secret_for", self.secret_for)
        secret = secret_port(channel_route)
        if secret is None:
            raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                               "callback secret is not available",
                               retryable=True,
                               details={"channel_route": channel_route})
        now = now_epoch
        if now is None:
            now = _parse_ts(self._now()).timestamp()
        verdict = impl.verify_callback(raw_body, headers, secret, now=now)
        if not verdict.get("ok"):
            # 401 且**不入 Inbox**：伪造的回调不能污染数据。
            raise ServiceError(401, "CALLBACK_SIGNATURE_INVALID",
                               "callback signature verification failed",
                               details={"reason": verdict.get("reason")})

        event_key = self._callback_event_key(raw_body, headers)
        existing = self.conn.execute(
            "SELECT state FROM connector_callback_inbox WHERE event_key = ?",
            (event_key,)).fetchone()
        if existing is not None:
            # 供应商重投同一条：必须 ACK，否则它会一直重试（§6.2 第 4 步）。
            return {"accepted": True, "duplicate": True, "state": existing["state"]}

        parsed = impl.parse_callback(self._loads(raw_body))
        with self._tx():
            self.conn.execute(
                "INSERT INTO connector_callback_inbox (event_key, channel_route,"
                " tenant_id, raw_body, signature_ok, correlation, state,"
                " received_at) VALUES (?, ?, NULL, ?, 1, ?, 'RECEIVED', ?)",
                (event_key, channel_route, raw_body,
                 _dumps({"client_key": parsed.get("client_key"),
                         "provider_job_ref": parsed.get("provider_job_ref")}),
                 self._now()))
        return {"accepted": True, "duplicate": False, "state": "RECEIVED",
                "event_key": event_key}

    def _callback_event_key(self, raw_body: bytes, headers: dict) -> str:
        for k, v in headers.items():
            if k.lower() == "x-event-id" and v:
                return "evt_" + _sha256(str(v))[:32]
        return "sha_" + hashlib.sha256(raw_body).hexdigest()

    def _loads(self, raw: bytes):
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def map_pending_callbacks(self, channel_route: str | None = None,
                              *, limit: int = 200) -> dict:
        """把 Inbox 里 `RECEIVED` / `UNMAPPED` 的回调映射成观测（§6.4）。

        - 关联得上 → `MAPPED` + 一条 `source=callback` 的观测。
        - 关联不上且不到 24h → `UNMAPPED`，留着等回执。
        - 关联不上且超 24h → `DEAD_LETTER`，**不丢弃**（§6.4 第 4 步）。

        由提交成功后、响应之后的后台任务、以及 janitor 调用。
        """
        sql = ("SELECT * FROM connector_callback_inbox WHERE state IN"
               " ('RECEIVED', 'UNMAPPED')")
        args: list[Any] = []
        if channel_route is not None:
            sql += " AND channel_route = ?"
            args.append(channel_route)
        sql += " ORDER BY received_at LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()

        mapped = unmapped = dead = 0
        for row in rows:
            impl = self.channels.get(row["channel_route"])
            if impl is None:
                continue                    # 别的渠道的回调，不是本进程的事
            body = self._loads(row["raw_body"])
            parsed = impl.parse_callback(body)
            receipt = self._correlate(parsed)
            if receipt is None:
                age = _parse_ts(self._now()) - _parse_ts(row["received_at"])
                if age.total_seconds() > UNMAPPED_TTL_SECONDS:
                    with self._tx():
                        self.conn.execute(
                            "UPDATE connector_callback_inbox SET state ="
                            " 'DEAD_LETTER' WHERE event_key = ?",
                            (row["event_key"],))
                    dead += 1
                else:
                    with self._tx():
                        self.conn.execute(
                            "UPDATE connector_callback_inbox SET state ="
                            " 'UNMAPPED' WHERE event_key = ?",
                            (row["event_key"],))
                    unmapped += 1
                continue
            with self._tx():
                for obs in parsed.get("observations") or []:
                    self._emit_observation(
                        tenant_id=receipt["tenant_id"],
                        channel_route=row["channel_route"],
                        dispatch_id=receipt["dispatch_id"],
                        stable_client_key=receipt["stable_client_key"],
                        provider_job_id=receipt["provider_job_id"],
                        provider_job_ref=obs.get("provider_job_ref")
                        or receipt["provider_job_ref"],
                        source="callback",
                        provider_status_raw=obs["provider_status_raw"],
                        status=obs["status"], terminal=obs["terminal"],
                        output=obs.get("output"),
                        provider_usage=obs.get("provider_usage"),
                        causation_id=row["event_key"])
                self.conn.execute(
                    "UPDATE connector_callback_inbox SET state = 'MAPPED',"
                    " tenant_id = ?, mapped_at = ? WHERE event_key = ?",
                    (receipt["tenant_id"], self._now(), row["event_key"]))
            mapped += 1
        return {"mapped": mapped, "unmapped": unmapped, "dead_letter": dead}

    def _correlate(self, parsed: dict):
        """§6.4 的关联顺序：`stable_client_key` → `provider_job_ref` → 本地索引。"""
        key = parsed.get("client_key")
        if key:
            row = self.conn.execute(
                "SELECT * FROM connector_request_receipt"
                " WHERE stable_client_key = ?", (key,)).fetchone()
            if row is not None:
                return row
            indexed = self.conn.execute(
                "SELECT * FROM connector_client_key_index"
                " WHERE stable_client_key = ?", (key,)).fetchone()
            if indexed is not None:
                return self.conn.execute(
                    "SELECT * FROM connector_request_receipt"
                    " WHERE dispatch_id = ?",
                    (indexed["dispatch_id"],)).fetchone()
        ref = parsed.get("provider_job_ref")
        if ref:
            return self.conn.execute(
                "SELECT * FROM connector_request_receipt"
                " WHERE provider_job_ref = ?", (ref,)).fetchone()
        return None

    # ---- 崩溃恢复（§5.3）-----------------------------------------------

    def recover_stale_sending(self) -> dict:
        """把遗留的 `SENDING` 一律标 `unknown`，**不重发**。

        这是 §5.3 行 461–463 的代码位置：崩溃后 `bytes_written_at IS NULL`
        也不代表字节没出去（可能刚好要写），所以只有运行中的进程能说
        `not_sent`。恢复路径没有那个权利。
        """
        rows = self.conn.execute(
            "SELECT * FROM connector_request_receipt WHERE state = 'SENDING'"
        ).fetchall()
        recovered = []
        for row in rows:
            with self._tx():
                self.conn.execute(
                    "UPDATE connector_request_receipt SET state = 'ABANDONED',"
                    " outcome = 'unknown', error_class = 'crash_recovery',"
                    " responded_at = ? WHERE receipt_id = ?",
                    (self._now(), row["receipt_id"]))
                self._emit_submit_outcome(
                    tenant_id=row["tenant_id"],
                    channel_route=row["channel_route"],
                    dispatch_id=row["dispatch_id"],
                    stable_client_key=row["stable_client_key"],
                    provider_job_id=row["provider_job_id"],
                    provider_job_ref=row["provider_job_ref"],
                    outcome="unknown")
            recovered.append(row["receipt_id"])
        return {"recovered": len(recovered), "receipt_ids": recovered}

    # ---- 杂项 ----------------------------------------------------------

    def _executor_id(self, channel_route: str) -> str:
        return "s27-" + channel_route.replace("/", "-")

    def _callback_url(self, channel_route: str) -> str:
        return f"/callbacks/{channel_route}"

    def capabilities(self, channel_route: str) -> dict:
        return self.channel(channel_route).capabilities()

    def validate(self, channel_route: str, submit_payload: dict) -> dict:
        errors = self.channel(channel_route).validate(submit_payload)
        return {"ok": not errors, "errors": errors}

    def estimate(self, channel_route: str, submit_payload: dict) -> dict:
        return self.channel(channel_route).estimate(submit_payload)


def initialise(conn) -> None:
    """建表。`create_app` 与测试都走这里，避免两处 schema 漂移。"""
    conn.executescript(SCHEMA)
