"""S26 generation-jobs 的业务内核。

范围：`plans/S26-generation-jobs.md` §2（三层模型）、§3（状态机）、§4（准入
Saga）、§7（未知提交）、§8（归档）、§9（巡检）、§10（与 S27 解耦）、§12
（对外契约）、§13（失败对照）。

**这个文件是 S26 唯一的状态机。** `provider_job.state` 的 16 个值与 §3.1 的
转移白名单都在这里；数据库不加 CHECK（C26-2，理由见 `schema.py`）。

## 为什么依赖是注入的 callable

S25（模型路由）与 S27（供应商连接器）都是 `design_only`，本仓库也没有任何服务
发过出站 HTTP。按 AGENTS.md「依赖服务未实现 → 用 mock 按契约走，不要共用数据库
凑合」，S26 对依赖一律走端口：

| 端口 | 契约 | 状态 |
|---|---|---|
| `read_route_decision` | S25 `model.route-selected.v2` | design_only |
| `read_channel_profile` | 渠道能力（`provider_idempotency` / `p99_submit_latency`） | design_only，见 x-needs-owner |
| `reserve_budget` / `authorize_budget` / `release_budget` / `settle_budget` / `read_reservation` | S23 | 已实现 |
| `acquire_permit` / `confirm_dispatch` / `release_permit` / `read_permit` | S22 | 已实现 |
| `check_capability_validity` / `report_capability_drift` / `isolate_account_pool` | S24 | 已实现 |
| `archive_artifact` / `report_ingest_gap` | S28 | 已实现 |

**没有 S26 → S27 的同步端口**（§10.9）。派单是 Outbox 事件
`connector.submit.requested`；S27 → S26 的 `claim` 是 S26 当服务端的同步入口。

测试注入 fake；没注入就抛 `DEPENDENCY_UNAVAILABLE`（503，`retryable=true`）——
不静默降级。

## 三条不能破的性质

1. **`SUBMISSION_UNKNOWN` 没有通往 `DISPATCH_AUTHORIZED` 的边**（§7.2 六条铁律）。
   `TRANSITIONS` 里它的出边只有 `RECONCILING`、`QUEUED`、`RUNNING`、`ARCHIVED`。
   这是 AGENTS.md 红线「在 `SUBMISSION_UNKNOWN` 状态下盲目重提付费任务」的代码位置。
2. **只有"字节没有离开本机"的失败才可重试**（§7.1）。S27 只上报
   `{outcome: not_sent | unknown | rejected | accepted}`，**状态由 S26 定**；
   `unknown` 一律进 UNKNOWN，没有"大概没成功"这条分支。
3. **`execution_fence` 与 `node_epoch` 不混用**（AGENTS.md / §9.2）。fence 是
   run/branch 级，epoch 是单 `logical_node` 级；旧 epoch 的结果永远不能推进
   接受指针。观测与取消都同时校验两者，且**分别**判断。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from event_sdk import Inbox, Outbox, new_event

# ---------------------------------------------------------------- §3.1 状态

# §3.1 的 16 个状态，一字不多一字不少。`generation.job-state-changed.v2` 的
# `x-needs-owner` 第一条要求的就是这个闭集，而契约层没有机器可读版本 ——
# 这里就是它。
STATES = (
    "CREATED", "RESERVING", "ADMITTED", "DISPATCH_AUTHORIZED", "SUBMITTING",
    "QUEUED", "RUNNING", "PROVIDER_SUCCEEDED", "PROVIDER_FAILED", "CANCELED",
    "INGESTING", "ARCHIVED", "SUBMISSION_UNKNOWN", "RECONCILING",
    "REJECTED_BUDGET", "REJECTED_QUOTA",
)

# §3.2：真正不可逆的终态只有 ARCHIVED。`PROVIDER_SUCCEEDED` 还要 `INGESTING`；
# `PROVIDER_FAILED`/`CANCELED` 在结算完成前也不是终态。但对外（S21 的
# `query_jobs`）需要一个"这个 job 不会再有结果"的口径 —— 那就是下面这个集合：
# 它们都还会 → ARCHIVED，但都不会再产生供应商侧的新状态。
TERMINAL_STATES = frozenset({
    "PROVIDER_FAILED", "CANCELED", "REJECTED_BUDGET", "REJECTED_QUOTA", "ARCHIVED",
})

# 冻结态：只允许对账或放弃。铁律的具体落点，任何别处不得把它们当成可推进状态。
FROZEN_STATES = frozenset({"SUBMISSION_UNKNOWN", "RECONCILING"})

TRANSITIONS: dict[str, frozenset[str]] = {
    # — → CREATED 是建 job 本身，不经过 _transition（没有前态可写进事件）。
    "CREATED": frozenset({"RESERVING"}),
    # 重试耗尽留在 RESERVING（§6 的"超时/不明 → RESERVING（保持）"）。
    # §4 写的 `RESERVING_FAILED` 不在 16 态里，落成 last_error_code（C26-6）。
    "RESERVING": frozenset({"ADMITTED", "REJECTED_BUDGET", "REJECTED_QUOTA"}),
    "ADMITTED": frozenset({
        "DISPATCH_AUTHORIZED", "RESERVING", "REJECTED_BUDGET", "REJECTED_QUOTA",
        # §4 步骤 4 的 S24 失效、§13 的模型退休：`ROUTE_INVALIDATED` 不在 16 态，
        # 落成 PROVIDER_FAILED + last_error_code（C26-7）。
        "PROVIDER_FAILED", "CANCELED",
    }),
    "DISPATCH_AUTHORIZED": frozenset({
        "SUBMITTING", "ADMITTED",       # grant 过期未领 → 撤销重签（§10.6）
        "SUBMISSION_UNKNOWN",           # 领取后本进程崩在写状态之前
        "REJECTED_BUDGET", "REJECTED_QUOTA", "PROVIDER_FAILED", "CANCELED",
    }),
    "SUBMITTING": frozenset({
        "QUEUED", "RUNNING", "PROVIDER_FAILED", "SUBMISSION_UNKNOWN",
        # §3.1 行 221：S27 明确 not_sent 才回得来，且守卫见 `record_submit_outcome`。
        "DISPATCH_AUTHORIZED", "CANCELED",
    }),
    # §3.1 的行只写了 `QUEUED → RUNNING`，但 §3 开头的主干图写的是
    # `QUEUED / RUNNING → PROVIDER_SUCCEEDED`，且 `PROGRESS_RANK` 把
    # PROVIDER_SUCCEEDED(7) 排在 QUEUED(5) 之后 —— 两处都要求这条边存在。
    # 少了它，跳过 running 直接报完的供应商会让 job 永远停在 QUEUED，
    # `_after_terminal` 不触发 → 不归档、不结算（§8.1 / §12）。
    "QUEUED": frozenset({
        "RUNNING", "PROVIDER_SUCCEEDED", "PROVIDER_FAILED", "CANCELED",
    }),
    "RUNNING": frozenset({"PROVIDER_SUCCEEDED", "PROVIDER_FAILED", "CANCELED"}),
    "PROVIDER_SUCCEEDED": frozenset({"INGESTING"}),
    # §8.2：归档重试是同态转移（只重归档，不重生成）。
    "INGESTING": frozenset({"ARCHIVED", "INGESTING"}),
    # **铁律**：没有 DISPATCH_AUTHORIZED，没有 ADMITTED，没有 RESERVING。
    # 对账证明已收单 → QUEUED/RUNNING；证明未收单且可安全重提 → 由 RECONCILING
    # 转 ADMITTED（不是从这里）；放弃 → ARCHIVED。
    "SUBMISSION_UNKNOWN": frozenset({"RECONCILING", "QUEUED", "RUNNING", "ARCHIVED"}),
    "RECONCILING": frozenset({
        "QUEUED", "RUNNING", "ADMITTED", "SUBMISSION_UNKNOWN", "ARCHIVED",
    }),
    "PROVIDER_FAILED": frozenset({"ARCHIVED"}),
    "CANCELED": frozenset({"ARCHIVED"}),
    "REJECTED_BUDGET": frozenset({"ARCHIVED"}),
    "REJECTED_QUOTA": frozenset({"ARCHIVED"}),
    "ARCHIVED": frozenset(),
}

# §7.1 的 S27 上报值。**S26 定状态，S27 只报发生了什么。**
SUBMIT_OUTCOMES = ("not_sent", "unknown", "rejected", "accepted")

# S27 观测里的供应商状态 → S26 状态。`status` 在契约里**故意不收窄为 enum**
# （docs 给 6 个值又要求 `inconclusive`），所以这里按名字映射，未知值不动状态
# 并留下 `last_error_code`，而不是猜。
# 关键：`not_found` **绝不映射成 failed**（§7.3 的对账信号）。
OBSERVATION_STATUS_MAP = {
    "accepted": "QUEUED", "queued": "QUEUED", "pending": "QUEUED",
    "submitted": "QUEUED",
    "running": "RUNNING", "in_progress": "RUNNING", "processing": "RUNNING",
    "succeeded": "PROVIDER_SUCCEEDED", "success": "PROVIDER_SUCCEEDED",
    "completed": "PROVIDER_SUCCEEDED", "done": "PROVIDER_SUCCEEDED",
    "failed": "PROVIDER_FAILED", "error": "PROVIDER_FAILED",
    "canceled": "CANCELED", "cancelled": "CANCELED",
}

# 观测里出现但不推进 job 状态的值：`not_found`（对账信号）、`inconclusive`
# （本来就不知道）。落到 `reconciliation_attempt` 或忽略，绝不进状态机。
OBSERVATION_NON_ADVANCING = frozenset({"not_found", "inconclusive", "unknown"})

# §7.3 对账档位 → 该档允许的方法，按优先顺序。
RECON_METHODS_BY_TIER = {
    "supported": ("list_by_client_key", "list_recent", "query_by_metadata"),
    "client_lookup_only": ("list_recent", "query_by_metadata"),
    "unsupported": ("manual_provider_console", "ledger_snapshot", "bill_reconcile"),
}

# §7.3 的对账梯子（秒）。第 8 轮之后每 24h，到 T+7d 转人工。
RECON_SCHEDULE_SECONDS = (0, 60, 300, 1800, 7200, 21600, 86400)
RECON_HORIZON_SECONDS = 7 * 86400

# §7.4 第 4 条：同一 attempt 最多几次真实 submit（S02 策略的默认值）。
DEFAULT_MAX_SUBMIT_ATTEMPTS = 2

# §16 未决：`p99_submit_latency` 没有实测值，先用保守的 60s。
DEFAULT_P99_SUBMIT_SECONDS = 60.0

# §10.6：grant 的有效期。S27 必须在这个窗口内 claim；过期未 claim 就撤销重签，
# 因为 claim 之前没有任何字节离开本机，重签**不产生费用**。
_GRANT_TTL = 300

# §8.1 modality → S28 的 content_class。
CONTENT_CLASS_BY_MODALITY = {
    "video": "generated_video", "image": "generated_image",
    "speech": "generated_audio", "music": "generated_audio",
    "sound_effect": "generated_audio", "text": "generated_text",
    "lipsync": "generated_video", "understanding": "generated_metadata",
    "training": "training_data",
}

# execution-context.v2 的 18 个必填字段。租户上下文由平台签名绑定（AGENTS.md），
# 所以这里只校验形状，不重算签名。
CONTEXT_REQUIRED = (
    "schema_version", "tenant_id", "project_id", "series_id", "branch_id",
    "episode_id", "home_region", "home_cell", "manifest_ref", "node_id",
    "node_epoch", "execution_fence", "memory_checkpoint", "policy_ref", "actor",
    "command_id", "request_hash", "trace_id",
)

COMMAND_REQUIRED = (
    "schema_version", "context", "attempt_id", "variation_nonce",
    "idempotency_key", "modality", "input", "route_policy_ref", "template_ref",
    "budget_scope_id", "deadline",
)


def _now() -> str:
    return _ts(datetime.now(timezone.utc))


def _ts(value: datetime) -> str:
    """固定宽度 27 字符 UTC 时间戳；理由同 S22/S23（字符串序 == 时间序）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(value: str) -> datetime:
    """接受 `Z` 与 `+08:00` 两种写法。

    §12.1 第 7 条要判 `deadline` 是否已过 —— 直接字符串比较会在带偏移的
    输入上错位（`2026-11-12T20:00:00+08:00` 的字符串序比同刻的 `...Z` 大）。
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _shift(ts: str, seconds: float) -> str:
    """在固定宽度时间戳上加减秒数。`_parse_ts` 返回 `datetime`，
    `datetime + 秒数` 会 TypeError —— 排期、截止、宽限全部走这里。"""
    return _ts(_parse_ts(ts) + timedelta(seconds=seconds))


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _loads(text):
    return json.loads(text) if text else None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _merge_output(current_text, incoming: dict) -> str:
    """把一条观测的 `output` 合并进已有的 `output_snapshot`。

    **嵌在 `output` 键下**，不铺到快照顶层：快照顶层还存着 `reservation` 这类
    自己的引用（`_stash_reservation`），观测的 `output` 与它们不是同一层；
    `start_ingest` / `scan_ingest_ttl_warning` 也按 `snapshot["output"]` 读。

    **浅合并、新值覆盖**：供应商重发 URL 时整块 `output` 是新的，旧 URL 的
    过期时间不该继续生效。观测没带的键原样保留 —— 那说明没变，不是"撤回"。
    """
    snapshot = _loads(current_text) or {}
    merged = dict(snapshot.get("output") or {})
    merged.update(incoming)
    snapshot["output"] = merged
    return _dumps(snapshot)


def extract_artifact_source(output: dict) -> tuple[str | None, str | None, str | None]:
    """从 `output` 里取 (url, expires_at, sha256)。

    两种形状都要认（契约 `connector.observation.received` 的 `output` 只约束
    "对象或 null"，不锁键集 —— 见该 schema 的 x-needs-owner）：

    - 顶层 `provider_url` / `provider_url_expires_at` / `expected_sha256`
      （S26 方案 §8.1 的归档请求体形状）；
    - `manifest[i].url` / `url_expires_at` / `sha256`
      （S27 方案 §3.5 / `contracts/examples/connector-observation-received.json`）。

    顶层优先；顶层没有就按 manifest 顺序取第一条带 `url` 的。取不到返回 `None`
    三元组，由调用方决定是拒还是跳过 —— 这里不抛，因为告警扫描只要读到就够。
    """
    if not isinstance(output, dict):
        return None, None, None
    url = output.get("provider_url") or output.get("url")
    expires = output.get("provider_url_expires_at") or output.get("url_expires_at")
    expected = output.get("expected_sha256") or output.get("sha256")
    if not url:
        for item in output.get("manifest") or []:
            if isinstance(item, dict) and item.get("url"):
                url = item["url"]
                expires = expires or item.get("url_expires_at")
                expected = expected or item.get("sha256")
                break
    return url, expires, expected


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
        out = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "correlation_id": correlation_id or "s26",
        }
        if self.details:
            out["details"] = self.details
        return out


class Transaction:
    """显式事务。`isolation_level=None` 下 `with conn:` 不是事务（C21-16）。

    可重入：SQLite 不允许嵌套 `BEGIN`，而 Saga 与 `_transition` 天然是
    "外层一个事务、内层若干写入"。内层只挂在外层上，异常抛到最外层才 ROLLBACK。
    """

    def __init__(self, conn, depth: list[int]):
        # 深度放列表里：`sqlite3.Connection` 不接受自定义属性。
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
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


# ---------------------------------------------------------------- 稳定键

def stable_client_key_for(*, tenant_id: str, attempt_id: str, job_no: int,
                          requirement_hash: str, submission_nonce: str) -> str:
    """§2.4：跨重试**逐字节相同**的供应商侧幂等键。

    `"dr-" + base32_hex(sha256(tenant|attempt_id|job_no|requirement_hash|nonce))[0:40]`。

    五个输入一个都不能变。`submission_nonce` 在第一次准备 submit 时生成并
    持久化；**重发时必须复用**（§2.4 末段），重新生成 nonce 就是新请求。
    """
    if job_no < 1:
        raise ValueError("job_no must be >= 1")
    material = "|".join((tenant_id, attempt_id, str(int(job_no)),
                         requirement_hash, submission_nonce))
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return "dr-" + encoded[:40]


def requirement_hash_for(normalized_input: dict) -> str:
    """§2.3 的 `requirement_hash`：**规范化输入的 hash**。

    取 S25 吸附后的 `selected.normalized_input`，不是调用方的原始请求 ——
    T26-30（请求 4 秒、吸附到 5 秒）要求下游看到的 hash 基于 5 秒。
    """
    return _sha256(_dumps(normalized_input))


def parse_candidate_key(candidate_key: str) -> dict:
    """`candidate_key = provider/channel/model@revision#region#pool`（S25 §3.2）。

    这一个字符串是 S25 与 S26 之间唯一的候选标识 —— 它没有结构化版本
    （`model.route-selected.v2` 的 `selected` 只有 `candidate_key`）。解析失败
    说明路由决策的形状与契约不符，按 422 拒掉，不猜。
    """
    try:
        head, region, pool = candidate_key.split("#")
        provider, channel, model = head.split("/")
        model_id, snapshot = model.rsplit("@", 1)
    except ValueError:
        raise ServiceError(
            422, "ROUTE_DECISION_INVALID",
            "candidate_key is not provider/channel/model@revision#region#pool",
            details={"candidate_key": candidate_key})
    for name, value in (("provider", provider), ("channel", channel),
                        ("model_id", model_id), ("model_snapshot", snapshot),
                        ("region", region), ("account_pool_id", pool)):
        if not value:
            raise ServiceError(422, "ROUTE_DECISION_INVALID",
                               f"candidate_key has an empty {name}",
                               details={"candidate_key": candidate_key})
    return {"provider": provider, "channel": channel, "model_id": model_id,
            "model_snapshot": snapshot, "region": region,
            "account_pool_id": pool}


def recon_delay_seconds(round_no: int) -> int | None:
    """§7.3 的对账梯子。返回 None 表示超过 T+7d，该转人工。

    前 7 轮用 `RECON_SCHEDULE_SECONDS` 的偏移；第 8 轮起每 24h，**累计到
    T+7d 为止**。判据是自 T+0 起的累计时长，不是轮次本身 —— 拿轮次乘 24h
    会把"第 8 轮"当成"第 8 天"，早早就转人工。
    """
    if round_no < 1:
        raise ValueError("round_no must be >= 1")
    if round_no <= len(RECON_SCHEDULE_SECONDS):
        return RECON_SCHEDULE_SECONDS[round_no - 1]
    elapsed = (RECON_SCHEDULE_SECONDS[-1]
               + (round_no - len(RECON_SCHEDULE_SECONDS)) * 86400)
    return 86400 if elapsed <= RECON_HORIZON_SECONDS else None


# ---------------------------------------------------------------- 推进次序

# 只给"主干"排次序：观测乱序时（T26-15）用它判断"这条观测会不会让状态倒退"。
# 旁路状态（UNKNOWN / RECONCILING / 各种终态）刻意不在表里 —— 它们的推进不是
# 单调的直线，用次序判断会误伤（例如 RECONCILING 之后合法地回到 ADMITTED）。
PROGRESS_RANK = {
    "CREATED": 0, "RESERVING": 1, "ADMITTED": 2, "DISPATCH_AUTHORIZED": 3,
    "SUBMITTING": 4, "QUEUED": 5, "RUNNING": 6, "PROVIDER_SUCCEEDED": 7,
    "INGESTING": 8, "ARCHIVED": 9,
}

# C26-12：`admission-record.v2` 的 `state` 只有 8 个值，S26 自己有 16 个。
# 这是**唯一的**映射点，逐字段可核对。`REJECTED_BUDGET`/`REJECTED_QUOTA` 落
# `REJECTED`；其余终态落 `SETTLED`（它们都还要结算才算完）。
ADMISSION_STATE = {
    "CREATED": "RESERVING", "RESERVING": "RESERVING",
    "ADMITTED": "ADMITTED",
    "DISPATCH_AUTHORIZED": "DISPATCH_AUTHORIZED",
    "SUBMITTING": "SUBMITTING",
    "SUBMISSION_UNKNOWN": "SUBMISSION_UNKNOWN",
    "RECONCILING": "SUBMISSION_UNKNOWN",
    "QUEUED": "INFLIGHT", "RUNNING": "INFLIGHT",
    "PROVIDER_SUCCEEDED": "INFLIGHT", "INGESTING": "INFLIGHT",
    "ARCHIVED": "SETTLED",
    "PROVIDER_FAILED": "SETTLED", "CANCELED": "SETTLED",
    "REJECTED_BUDGET": "REJECTED", "REJECTED_QUOTA": "REJECTED",
}

# §13 里写在"状态"列、但不在 16 态里的四个名字（PGJ-1）。它们落成 `last_error_code`
# 或 `disposition`，**永远不写进 `provider_job.state`**（C26-2）。
NON_STATE_REASONS = frozenset({
    "ACCOUNT_UNAUTHORIZED", "CAPABILITY_DRIFT", "ROUTE_INVALIDATED",
    "CANCEL_REQUESTED",
})

# 可被对账证明为"从未收单"因而允许回到可提交状态的原因。空集合之外一律冻结。
_SAFE_RESUBMIT_CODES = frozenset({"RECONCILED_ABSENT"})


def intent_id_for(tenant_id: str, provider_job_id: str) -> str:
    """由 `(tenant, job)` 决定，**不是随机数**。

    理由：Saga 步骤 1/2 要先用 `{intent_id}:reserve` 做幂等键，而 intent 行要到
    步骤 3 才落库。若 intent_id 随机，崩溃重启后拿不到同一个键，就会对 S23 重新
    占用一笔。派生出来就没有这个窗口（§4 步骤 3 的"意图落库之前崩溃"分支）。
    """
    return "int_" + _sha256(f"{tenant_id}|{provider_job_id}")[:24]


def admission_id_for(intent_id: str) -> str:
    """`GET /admissions/{id}` 的 id。与 intent_id 双向可逆，不另存一列。"""
    return "adm_" + intent_id


def intent_id_of(admission_id: str) -> str | None:
    if not admission_id.startswith("adm_int_"):
        return None
    return admission_id[4:]


# ---------------------------------------------------------------- 服务

class GenerationJobsService:
    """S26 的唯一写者。

    所有外部依赖都是构造时注入的 callable（理由见模块 docstring）。签名一律
    `(tenant_id, body, *, actor, correlation_id) -> dict`，与已实现的服务同形 ——
    测试里既可以直接塞真服务的方法，也可以塞记录调用的 fake。
    """

    def __init__(self, conn, *, source: str = "S26", home_cell: str = "cell_a1",
                 clock=_now, **ports):
        self.conn = conn
        # 必须是 `sqlite3.Row`（幂等 SDK 按**下标**解包）。
        self.conn.row_factory = sqlite3.Row
        self.source = source
        self.home_cell = home_cell
        self._clock = clock
        self._tx_depth = [0]
        self.outbox = Outbox(conn)
        self.inbox = Inbox(conn)
        self._ports = ports
        self._p99_submit_seconds = float(
            ports.get("p99_submit_seconds") or DEFAULT_P99_SUBMIT_SECONDS)
        self._default_max_attempts = int(
            ports.get("max_attempts_per_node") or 3)
        self._default_max_submits = int(
            ports.get("max_submit_attempts") or DEFAULT_MAX_SUBMIT_ATTEMPTS)

    # ------------------------------------------------------------------ 工具

    def _now(self) -> str:
        return self._clock()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:24]}"

    def _tx(self):
        return Transaction(self.conn, self._tx_depth)

    def _require(self, name: str):
        port = self._ports.get(name)
        if port is None:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                f"port {name} is not configured", retryable=True,
                details={"port": name})
        return port

    # S23/S22 的写方法都是 `(tenant_id, ...body..., *, actor, correlation_id)`。
    # 直接 `port(*args)` 会少两个关键字参数，所以过这一层，形状只有一份。
    _HOST_PORTS = frozenset({
        "reserve_budget", "authorize_budget", "release_budget", "settle_budget",
        "acquire_permit", "confirm_dispatch", "release_permit",
    })

    def _service_actor(self) -> dict:
        """S26 以服务身份调用 S23/S22。不冒用人类 actor（AGENTS.md 的红线：
        审计要能追到"谁"，而这里确实是本服务在动钱）。"""
        return {"principal_id": self.source, "principal_type": "service",
                "delegation_id": None}

    def _host(self, name: str, *args, correlation_id: str = ""):
        port = self._require(name)
        if name in self._HOST_PORTS:
            return port(*args, actor=self._service_actor(),
                        correlation_id=correlation_id)
        return port(*args)

    def _job(self, tenant_id: str, provider_job_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM provider_job WHERE tenant_id = ? AND provider_job_id = ?",
            (tenant_id, provider_job_id)).fetchone()
        if row is None:
            raise ServiceError(404, "JOB_NOT_FOUND", "generation job not found",
                               details={"provider_job_id": provider_job_id})
        return row

    def _attempt(self, tenant_id: str, attempt_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM generation_attempt WHERE tenant_id = ? AND attempt_id = ?",
            (tenant_id, attempt_id)).fetchone()
        if row is None:
            raise ServiceError(404, "ATTEMPT_NOT_FOUND", "attempt not found",
                               details={"attempt_id": attempt_id})
        return row

    def _intent(self, tenant_id: str, intent_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM submission_intent WHERE tenant_id = ? AND intent_id = ?",
            (tenant_id, intent_id)).fetchone()

    # ------------------------------------------------------------ 事件与转移

    def _emit_state(self, job: sqlite3.Row, *, previous_state: str,
                    reason_code: str | None, correlation_id: str,
                    causation_id: str | None,
                    must_reconcile: bool | None = None):
        """§12.5：**恰好** 14 个必填 data 字段，`additionalProperties: false`。

        `provider`/`channel`/`node_id`/`node_epoch`/`execution_fence` 全部读 attempt
        与 job 的真实列，不从调用方传 —— 事件是投影，不是又一份输入。
        """
        attempt = self._attempt(job["tenant_id"], job["attempt_id"])
        if must_reconcile is None:
            must_reconcile = job["state"] == "SUBMISSION_UNKNOWN"
        data = {
            "schema_version": "2.0",
            "attempt_id": job["attempt_id"],
            "state": job["state"],
            "previous_state": previous_state,
            "business_attempt": int(job["business_attempt"]),
            "network_retry": int(job["network_retry"]),
            "submit_attempts": int(job["submit_attempts"]),
            "provider": job["provider"],
            "channel": job["channel"],
            "node_id": attempt["node_id"],
            "node_epoch": int(attempt["node_epoch"]),
            "execution_fence": int(attempt["execution_fence"]),
            "must_reconcile": bool(must_reconcile),
            "reason_code": reason_code,
        }
        envelope = new_event(
            source=self.source,
            type="generation.job.state-changed",
            subject=f"provider-job/{job['provider_job_id']}",
            tenant_id=job["tenant_id"],
            home_cell=self.home_cell,
            aggregate_id=job["provider_job_id"],
            sequence=int(job["aggregate_sequence"]),
            correlation_id=correlation_id,
            causation_id=causation_id,
            data=data,
        )
        self.outbox.append(envelope)

    _SETTABLE = frozenset({
        "provider_job_ref", "next_action_at", "disposition", "last_error_code",
        "last_error_at", "last_observed_at", "poll_watermark", "output_snapshot",
        "artifact_refs", "network_retry", "submit_attempts", "saga_attempts",
        "closed_at",
    })

    def _transition(self, job: sqlite3.Row, to_state: str, *, reason_code=None,
                    actor="S26", correlation_id="", causation_id=None,
                    extra_sets: dict | None = None,
                    must_reconcile: bool | None = None) -> sqlite3.Row:
        """状态机唯一入口。白名单外的任何边都在这里被拒。

        `extra_sets` 是同一 UPDATE 里的其它列（`provider_job_ref`、`next_action_at`
        …）—— 它们必须和状态一起改，否则会短暂出现"状态已变、引用还没写"的窗口。
        """
        from_state = job["state"]
        allowed = TRANSITIONS.get(from_state, frozenset())
        if from_state == to_state:
            # §8.2 的同态重试（`INGESTING → INGESTING`）是白名单里唯一合法的自环；
            # 其余自环是空操作 —— 不发事件、不推进 sequence（T26-14）。
            if to_state not in allowed:
                return job
        elif to_state not in allowed:
            raise ServiceError(
                409, "ILLEGAL_TRANSITION",
                f"{from_state} cannot go to {to_state}",
                details={"from": from_state, "to": to_state})

        now = self._now()
        sets = ["state = ?", "updated_at = ?",
                "aggregate_sequence = aggregate_sequence + 1"]
        args: list[Any] = [to_state, now]
        for column, value in (extra_sets or {}).items():
            if column not in self._SETTABLE:
                raise ServiceError(500, "INTERNAL",
                                   f"unsupported transition column {column}")
            sets.append(f"{column} = ?")
            args.append(value)
        if to_state == "ARCHIVED" and "closed_at" not in (extra_sets or {}):
            sets.append("closed_at = ?")
            args.append(now)
        args.extend([job["tenant_id"], job["provider_job_id"], from_state])
        cur = self.conn.execute(
            "UPDATE provider_job SET " + ", ".join(sets)
            + " WHERE tenant_id = ? AND provider_job_id = ? AND state = ?", args)
        if cur.rowcount != 1:
            # 并发：别人已经推过了。不覆盖，让调用方按自己的语义处理。
            raise ServiceError(409, "VERSION_CONFLICT",
                               "job state changed concurrently", retryable=True,
                               details={"expected": from_state})
        seq = int(job["aggregate_sequence"]) + 1
        self.conn.execute(
            "INSERT INTO job_transition (tenant_id, provider_job_id, seq,"
            " from_state, to_state, reason_code, actor, occurred_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job["tenant_id"], job["provider_job_id"], seq, from_state, to_state,
             reason_code, actor, now))
        updated = self._job(job["tenant_id"], job["provider_job_id"])
        self._emit_state(updated, previous_state=from_state,
                         reason_code=reason_code, correlation_id=correlation_id,
                         causation_id=causation_id,
                         must_reconcile=must_reconcile)
        return updated

    # --------------------------------------------------------------- 建单

    @staticmethod
    def _validate_command(command: dict) -> None:
        """§12.1 第 2 条：形状校验。schema 校验通过不等于分布式正确性 ——
        这里只证明"字段齐、类型对"，真正的守卫在下面每一条业务校验里。"""
        if not isinstance(command, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "generation_command must be an object")
        missing = [k for k in COMMAND_REQUIRED if command.get(k) is None]
        if missing:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"generation_command is missing: {', '.join(missing)}",
                               details={"missing": missing})
        if command["schema_version"] != "2.0":
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "generation_command.schema_version must be 2.0")
        context = command.get("context")
        if not isinstance(context, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "generation_command.context must be an object")
        missing = [k for k in CONTEXT_REQUIRED if context.get(k) is None]
        if missing:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"context is missing: {', '.join(missing)}",
                               details={"missing": missing})
        if not isinstance(command.get("input"), dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "generation_command.input must be an object")

    def _ledger(self, tenant_id: str, node_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM attempt_ledger WHERE tenant_id = ? AND node_id = ?",
            (tenant_id, node_id)).fetchone()
        if row is not None:
            return dict(row)
        return {"tenant_id": tenant_id, "node_id": node_id, "attempts_used": 0,
                "micro_spent": 0, "max_micro": None, "max_attempts": None}

    def _bump_ledger(self, tenant_id: str, node_id: str, *,
                     attempts: int = 0, micro: int = 0) -> dict:
        """§11 / T26-32：按 `(tenant, node)` 累计，**跨 Run 永不重置**。"""
        now = self._now()
        current = self._ledger(tenant_id, node_id)
        if current.get("attempts_used") == 0 and current.get("micro_spent") == 0:
            self.conn.execute(
                "INSERT OR IGNORE INTO attempt_ledger (tenant_id, node_id,"
                " attempts_used, micro_spent, updated_at) VALUES (?, ?, 0, 0, ?)",
                (tenant_id, node_id, now))
        self.conn.execute(
            "UPDATE attempt_ledger SET attempts_used = attempts_used + ?,"
            " micro_spent = micro_spent + ?, updated_at = ?"
            " WHERE tenant_id = ? AND node_id = ?",
            (attempts, micro, now, tenant_id, node_id))
        return self._ledger(tenant_id, node_id)

    def create_job(self, tenant_id: str, body: dict, *, actor: dict,
                   correlation_id: str) -> dict:
        """`POST /generation-jobs`。§12.1 的八条校验按顺序执行，失败即返回。

        返回 `202` 语义的 `{operation_id, provider_job_id, attempt_id, state, replayed}`；
        `operation_id` 就是 `provider_job_id` 的包装（§12.1 末段）。
        """
        command = body.get("generation_command")
        self._validate_command(command)
        context = command["context"]
        if context.get("tenant_id") != tenant_id:
            # AGENTS.md：租户上下文必须由平台签名绑定，body 里的 tenant_id
            # 不是可信输入；header 与签名上下文不一致一律拒绝。
            raise ServiceError(403, "FORBIDDEN_SCOPE",
                               "context.tenant_id does not match the caller",
                               details={"context_tenant": context.get("tenant_id")})
        idempotency_key = command["idempotency_key"]
        existing = self.conn.execute(
            "SELECT * FROM generation_attempt WHERE tenant_id = ? AND idempotency_key = ?",
            (tenant_id, idempotency_key)).fetchone()
        if existing is not None:
            # T26-01 / T26-07：同 key 重投返回原 attempt 与它的 job，不新建、不重提。
            job = self.conn.execute(
                "SELECT * FROM provider_job WHERE tenant_id = ? AND attempt_id = ?"
                " ORDER BY job_no LIMIT 1", (tenant_id, existing["attempt_id"])).fetchone()
            return {
                "operation_id": job["provider_job_id"] if job else None,
                "provider_job_id": job["provider_job_id"] if job else None,
                "attempt_id": existing["attempt_id"],
                "state": job["state"] if job else existing["state"],
                "admission_id": admission_id_for(
                    intent_id_for(tenant_id, job["provider_job_id"])) if job else None,
                "replayed": True,
            }

        route_decision_id = body.get("route_decision_id")
        if not route_decision_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "route_decision_id is required")
        route = self._require("read_route_decision")(
            tenant_id, {"route_decision_id": route_decision_id})
        if not isinstance(route, dict) or not route.get("feasible"):
            raise ServiceError(409, "ROUTE_INFEASIBLE",
                               "the route decision is not feasible",
                               details={"route_decision_id": route_decision_id})
        selected = route.get("selected") or {}

        # 第 3 条：调用方回显的 content_hash 必须与 S25 的记录一致。
        # 契约里 `POST /generation-jobs` 的 body 没有这个字段（C26-14），所以
        # 只在调用方给了的时候校验；不给就跳过，并在文档里写明这是契约缺口。
        echo = body.get("route_decision_ref")
        if isinstance(echo, dict) and echo.get("content_hash"):
            if echo["content_hash"] != route.get("content_hash"):
                raise ServiceError(
                    409, "ROUTE_DECISION_HASH_MISMATCH",
                    "route_decision content_hash does not match S25's record",
                    details={"expected": route.get("content_hash"),
                             "received": echo["content_hash"]})
        # 路由决策必须属于本次 attempt / 节点 / fence（三处都查，缺一不可）。
        for field in ("attempt_id", "node_id", "node_epoch", "execution_fence"):
            if route.get(field) is not None and \
                    route[field] != command["context"].get(field) and \
                    route[field] != command.get(field):
                raise ServiceError(
                    409, "ROUTE_DECISION_STALE",
                    f"route decision {field} does not match the command",
                    details={"field": field, "route": route.get(field),
                             "command": command.get(field)})

        now_dt = _parse_ts(self._now())
        quote_valid_to = route.get("quote_valid_to") or selected.get("quote_valid_to")
        if not quote_valid_to:
            raise ServiceError(422, "ROUTE_DECISION_INVALID",
                               "route decision carries no quote_valid_to")
        # 第 4 条：报价过期 → 409，不预留。
        if _parse_ts(quote_valid_to) <= now_dt:
            raise ServiceError(409, "ROUTE_QUOTE_EXPIRED",
                               "the route quote has expired",
                               details={"quote_valid_to": quote_valid_to})
        # 第 7 条：deadline。
        if _parse_ts(command["deadline"]) <= now_dt:
            raise ServiceError(422, "DEADLINE_PASSED",
                               "the generation deadline has already passed",
                               details={"deadline": command["deadline"]})

        node_id = context["node_id"]
        ledger = self._ledger(tenant_id, node_id)
        max_attempts = (ledger["max_attempts"] if ledger.get("max_attempts")
                        else self._default_max_attempts)
        # 第 5 条：业务 attempt 上限（跨 Run 累计）。
        attempt_no = int(ledger.get("attempts_used") or 0) + 1
        if attempt_no > max_attempts:
            raise ServiceError(
                409, "ATTEMPT_LIMIT_EXCEEDED",
                "this node has used all of its business attempts",
                details={"attempts_used": ledger.get("attempts_used"),
                         "max_business_attempts": max_attempts})
        # 第 6 条：节点费用上限。`micro_spent` 只累计**已结算**金额，预留不算。
        cap = ledger.get("max_micro")
        if cap is not None and int(ledger.get("micro_spent") or 0) >= int(cap):
            raise ServiceError(
                409, "NODE_COST_LIMIT_EXCEEDED",
                "this node has spent its whole cost allowance",
                details={"micro_spent": ledger.get("micro_spent"),
                         "max_micro": cap})

        # 第 8 条：`requirement_hash` 必须基于 S25 吸附后的 `normalized_input`。
        # T26-30（请求 4 秒、吸附到 5 秒）要求下游看到的是 5 秒。
        normalized = selected.get("normalized_input")
        if not isinstance(normalized, dict):
            raise ServiceError(422, "ROUTE_DECISION_INVALID",
                               "route decision has no normalized_input")
        requirement_hash = requirement_hash_for(normalized)
        if body.get("requirement_hash") and body["requirement_hash"] != requirement_hash:
            raise ServiceError(
                422, "REQUIREMENT_HASH_MISMATCH",
                "requirement_hash must be computed over the snapped input",
                details={"expected": requirement_hash,
                         "received": body["requirement_hash"]})

        candidate = parse_candidate_key(selected.get("candidate_key") or "")
        price_snapshot_id = (selected.get("price_snapshot_id")
                             or route.get("price_snapshot_id"))
        if not price_snapshot_id:
            # S25 的 `selected` 里没有这个字段（C26-8）；没有它 S23 拒收，
            # 而且"预留多少"就没有报价依据。不猜，直接拒。
            raise ServiceError(
                422, "PRICE_SNAPSHOT_UNAVAILABLE",
                "the route decision carries no price snapshot reference",
                details={"route_decision_id": route_decision_id})
        profile = {}
        read_profile = self._ports.get("read_channel_profile")
        if read_profile is not None:
            profile = read_profile(tenant_id, {
                "provider": candidate["provider"],
                "channel": candidate["channel"],
                "region": candidate["region"],
            }) or {}
        provider_idempotency = (profile.get("provider_idempotency")
                                or body.get("provider_idempotency"))
        if provider_idempotency not in ("supported", "client_lookup_only",
                                        "unsupported"):
            # 七条渠道的 `provider_idempotency` 都是 x-needs-owner（S24 侧
            # 未定），没有就拒 —— 猜一个会让"能不能自动重提"变成默认允许。
            raise ServiceError(
                503, "CHANNEL_PROFILE_UNAVAILABLE",
                "provider_idempotency is unknown for this channel", retryable=True,
                details={"provider": candidate["provider"],
                         "channel": candidate["channel"]})

        attempt_id = command["attempt_id"]
        provider_job_id = self._new_id("pj")
        submission_nonce = uuid.uuid4().hex
        requirement_set = {
            "quote_upper_bound_micro": selected.get("quote_upper_bound_micro"),
            "expected_cost_micro": selected.get("expected_cost_micro"),
            "currency": selected.get("currency"),
            "quote_valid_to": quote_valid_to,
            "snapshot_id": selected.get("snapshot_id"),
            "combination_key": selected.get("combination_key"),
            "candidate_key": selected.get("candidate_key"),
            "price_snapshot_id": price_snapshot_id,
            "normalized_input": normalized,
            "capability_bundle_revision": route.get("capability_bundle_revision"),
            "policy_snapshot_id": route.get("policy_snapshot_id"),
            "route_revision": route.get("revision"),
            "route_content_hash": route.get("content_hash"),
        }

        with self._tx():
            self.conn.execute(
                "INSERT INTO generation_attempt (attempt_id, tenant_id, project_id,"
                " series_id, episode_id, branch_id, node_id, node_epoch,"
                " execution_fence, modality, attempt_no, variation_nonce,"
                " idempotency_key, route_decision_id, requirement_set,"
                " budget_scope_id, deadline, max_business_attempts, state, context,"
                " request_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " 'OPEN', ?, ?, ?)",
                (attempt_id, tenant_id, context["project_id"], context["series_id"],
                 context["episode_id"], context["branch_id"], node_id,
                 int(context["node_epoch"]), int(context["execution_fence"]),
                 command["modality"], attempt_no, command["variation_nonce"],
                 idempotency_key, route_decision_id, _dumps(requirement_set),
                 command["budget_scope_id"], command["deadline"], max_attempts,
                 _dumps(context),
                 _sha256(_dumps({"command": command, "route": route})),
                 self._now()))
            self.conn.execute(
                "INSERT INTO provider_job (provider_job_id, attempt_id, tenant_id,"
                " job_no, state, provider, channel, model_id, model_snapshot,"
                " region, account_pool_id, requirement_hash, submission_nonce,"
                " provider_idempotency, business_attempt, created_at, updated_at,"
                " aggregate_sequence) VALUES (?, ?, ?, 1, 'CREATED', ?, ?, ?, ?, ?,"
                " ?, ?, ?, ?, ?, ?, ?, 1)",
                (provider_job_id, attempt_id, tenant_id, candidate["provider"],
                 candidate["channel"], candidate["model_id"],
                 candidate["model_snapshot"], candidate["region"],
                 candidate["account_pool_id"], requirement_hash, submission_nonce,
                 provider_idempotency, attempt_no, self._now(), self._now()))
            self._bump_ledger(tenant_id, node_id, attempts=1)

        job = self._job(tenant_id, provider_job_id)
        return {
            "operation_id": provider_job_id,
            "provider_job_id": provider_job_id,
            "attempt_id": attempt_id,
            "state": job["state"],
            "admission_id": admission_id_for(intent_id_for(tenant_id, provider_job_id)),
            "replayed": False,
        }

    # --------------------------------------------------------------- 准入 Saga

    def _intent_for_job(self, job) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM submission_intent WHERE tenant_id = ?"
            " AND provider_job_id = ?",
            (job["tenant_id"], job["provider_job_id"])).fetchone()

    def _grant_of(self, job) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM dispatch_grant WHERE tenant_id = ? AND provider_job_id = ?"
            " ORDER BY issued_at DESC LIMIT 1",
            (job["tenant_id"], job["provider_job_id"])).fetchone()

    def _saga_body(self, job, attempt) -> dict:
        """步骤 0：从**已持久化**的 attempt 重建 → S23/S22 的请求体。

        崩溃恢复要能重放同一请求，所以这里的每一个字段都必须来自库，不能来自
        调用栈上的局部变量。`command` 本体（含 prompt）不落库，故 `manifest_ref`
        与 `route_decision_ref` 从 `context` / `requirement_set` 重建。
        """
        requirement_set = _loads(attempt["requirement_set"]) or {}
        context = _loads(attempt["context"]) or {}
        route_ref = {
            "service_id": "S25",
            "id": attempt["route_decision_id"],
            "revision": int(requirement_set.get("route_revision") or 1),
            "content_hash": requirement_set.get("route_content_hash") or "",
        }
        price_ref = {
            "service_id": "S24",
            "id": requirement_set.get("price_snapshot_id"),
            "revision": 1,
            "content_hash": requirement_set.get("combination_key") or "",
        }
        return {
            "route_decision_ref": route_ref,
            "price_snapshot_ref": price_ref,
            "manifest_ref": context.get("manifest_ref") or {},
            "amount_micro": int(requirement_set.get("quote_upper_bound_micro") or 0),
            "currency": requirement_set.get("currency") or "",
            "quote_expires_at": requirement_set.get("quote_valid_to"),
            "requirement_set": requirement_set,
            "context": context,
        }

    def advance(self, tenant_id: str, provider_job_id: str, *,
                correlation_id: str = "") -> dict:
        """把 job 往前推一步。**每一步都可安全重入** —— 幂等键都是确定的。

        `CREATED → RESERVING` 起，一路推到 `DISPATCH_AUTHORIZED`（签发 grant）或
        一个明确的拒绝态。外部服务超时/不明 → 原地不动（留在 `RESERVING` /
        `ADMITTED`），下次调用用**同一幂等键**重放（§4 步骤 1 末段、§6 矩阵）。
        """
        job = self._job(tenant_id, provider_job_id)
        seen = {job["state"]}
        for _ in range(4):      # 状态机深度有限：每轮最多前进一格
            state = job["state"]
            if state == "CREATED":
                job = self._transition(job, "RESERVING",
                                       reason_code="ADMISSION_STARTED",
                                       correlation_id=correlation_id)
            elif state == "RESERVING":
                job = self._saga_reserve(job, correlation_id=correlation_id)
            elif state == "ADMITTED":
                job = self._saga_dispatch(job, correlation_id=correlation_id)
            else:
                break
            # 原地不动或绕回来就停：超时/不明是"停在原地等调用方按同键重试"
            # （§4），不是在一次 advance 里连打 S23/S22 四遍。绕回（如
            # ADMITTED → RESERVING → ADMITTED）同样没有新进展。
            if job["state"] in seen:
                break
            seen.add(job["state"])
        return self.get_job(tenant_id, provider_job_id)

    # ---- 步骤 1：S23 预算预留

    def _saga_reserve(self, job, *, correlation_id: str) -> sqlite3.Row:
        attempt = self._attempt(job["tenant_id"], job["attempt_id"])
        body = self._saga_body(job, attempt)
        intent_id = intent_id_for(job["tenant_id"], job["provider_job_id"])
        if body["amount_micro"] <= 0:
            # 没有报价上界就没有"最多花多少"的依据。不预留、不动钱。
            raise ServiceError(422, "ROUTE_DECISION_INVALID",
                               "quote_upper_bound_micro must be > 0",
                               details={"quote_upper_bound_micro":
                                        body["amount_micro"]})
        request = {
            # §4 步骤 1：幂等键就是 `{intent_id}:reserve`，所以 `intent_id`
            # 必须**先于** intent 行存在 —— 这正是它由 `(tenant, job)` 派生
            # 而不是随机的理由（见 `intent_id_for`）。
            "command_id": f"{intent_id}:reserve",
            "tenant_id": job["tenant_id"],
            "budget_scope_id": attempt["budget_scope_id"],
            "amount_micro": body["amount_micro"],
            # S23 要求 conservative >= amount；报价上界就是我们的保守估计
            # （§2 步骤 0：用上界预留，不用期望成本），所以两者相等。
            "amount_conservative_micro": body["amount_micro"],
            "currency": body["currency"],
            "budget_currency": body["currency"],
            "purpose": "generation",
            "attempt_id": job["attempt_id"],
            "execution_fence": int(attempt["execution_fence"]),
            "run_id": body["context"].get("run_id"),
            "node_id": attempt["node_id"],
            "node_epoch": int(attempt["node_epoch"]),
            "manifest_ref": body["manifest_ref"],
            "route_decision_ref": body["route_decision_ref"],
            "price_snapshot_ref": body["price_snapshot_ref"],
            "quote_expires_at": body["quote_expires_at"],
            # 同 (tenant, scope) 的活预留直接回放（T23-03），所以重放安全。
            "idempotency_scope": intent_id,
            "provider_job_ref": job["provider_job_id"],
        }
        try:
            answer = self._host("reserve_budget",
                job["tenant_id"], request, correlation_id=correlation_id)
        except ServiceError as exc:
            if exc.code in ("INSUFFICIENT_BUDGET", "BUDGET_EXHAUSTED", "BUDGET_FROZEN",
                            "BUDGET_PERIOD_CLOSED", "PERMISSION_DENIED",
                            "FORBIDDEN_SCOPE", "CURRENCY_MISMATCH"):
                # §6 矩阵第 1 行：明确拒绝 → REJECTED_BUDGET，不花钱。
                return self._transition(
                    job, "REJECTED_BUDGET", reason_code=exc.code,
                    extra_sets={"last_error_code": exc.code,
                                "last_error_at": self._now()},
                    correlation_id=correlation_id)
            if exc.retryable or exc.status >= 500:
                # 超时 / 5xx / 不明：**停在 RESERVING**，下次同键重放。
                # 不前进、不释放、不当成"没建过"（§4 步骤 1：超时不能当成功）。
                self._bump_saga_attempts(job)
                return job
            raise
        self._stash_reservation(tenant_id=job["tenant_id"],
                                provider_job_id=job["provider_job_id"],
                                answer=answer, job=job)
        return self._transition(job, "ADMITTED", reason_code="BUDGET_RESERVED",
                                correlation_id=correlation_id)

    def _bump_saga_attempts(self, job) -> None:
        self.conn.execute(
            "UPDATE provider_job SET saga_attempts = saga_attempts + 1,"
            " last_error_code = COALESCE(last_error_code, 'SAGA_RETRY'),"
            " updated_at = ? WHERE tenant_id = ? AND provider_job_id = ?",
            (self._now(), job["tenant_id"], job["provider_job_id"]))

    def _stash_reservation(self, *, tenant_id: str, provider_job_id: str,
                           answer: dict, job) -> None:
        """把 S23 的引用记到 job 上，供步骤 2/4 与崩溃恢复使用。

        只存**引用**（reservation_id + 金额 + 币种），不存 S23 的余额计数 ——
        权威只有一个（docs/01 的权威表）。
        """
        payload = _loads(job["output_snapshot"] or "") or {}
        payload["reservation"] = {
            "id": answer["reservation_id"],
            "amount_micro": int(answer.get("amount_conservative_micro")
                                or answer["amount_micro"]),
            "currency": answer["currency"],
            "budget_currency": answer.get("budget_currency") or answer["currency"],
            "state": answer.get("state") or "RESERVED",
        }
        self.conn.execute(
            "UPDATE provider_job SET output_snapshot = ?, updated_at = ?"
            " WHERE tenant_id = ? AND provider_job_id = ?",
            (_dumps(payload), self._now(), tenant_id, provider_job_id))

    def _stashed(self, job) -> dict:
        return _loads(job["output_snapshot"] or "") or {}

    # ---- 步骤 2 + 3：S22 permit，然后一个事务里落 intent

    def _saga_dispatch(self, job, *, correlation_id: str) -> sqlite3.Row:
        attempt = self._attempt(job["tenant_id"], job["attempt_id"])
        intent_id = intent_id_for(job["tenant_id"], job["provider_job_id"])
        existing = self._intent_for_job(job)
        if existing is not None:
            # 步骤 3 已落库：不再调 reserve/permit（§4：「意图落库之后崩溃 →
            # 恢复从 intent 继续」）。直接进步骤 4。
            return self._saga_recheck_and_issue(job, attempt, existing,
                                                correlation_id=correlation_id)

        reservation = self._stashed(job).get("reservation")
        if not reservation:
            # 恢复窗口：ADMITTED 但没有预留引用。用同键回查 S23。
            answer = self._host("read_reservation",
                job["tenant_id"], {"idempotency_scope": intent_id,
                                   "attempt_id": job["attempt_id"]})
            if not answer:
                # 查不到说明步骤 1 的写没成功；退回 RESERVING 重放同键。
                return self._transition(job, "RESERVING",
                                        reason_code="RESERVATION_MISSING",
                                        correlation_id=correlation_id)
            self._stash_reservation(tenant_id=job["tenant_id"],
                                    provider_job_id=job["provider_job_id"],
                                    answer=answer, job=job)
            job = self._job(job["tenant_id"], job["provider_job_id"])
            reservation = self._stashed(job)["reservation"]

        permit = self._saga_acquire(job, attempt, reservation, intent_id,
                                    correlation_id=correlation_id)
        if isinstance(permit, sqlite3.Row):
            return permit       # 已是拒绝态
        return self._write_intent(job, attempt, reservation, permit, intent_id,
                                  correlation_id=correlation_id)

    def _saga_acquire(self, job, attempt, reservation: dict, intent_id: str, *,
                      correlation_id: str):
        request = {
            "command_id": f"{intent_id}:permit",
            "tenant_id": job["tenant_id"],
            "reservation_ref": reservation["id"],
            "reservation_amount_micro": int(reservation["amount_micro"]),
            "attempt_id": job["attempt_id"],
            "execution_fence": int(attempt["execution_fence"]),
            "run_id": (_loads(attempt["context"]) or {}).get("run_id"),
            "node_id": attempt["node_id"],
            "node_epoch": int(attempt["node_epoch"]),
            "modality": attempt["modality"],
            "provider_id": job["provider"],
            "channel_id": job["channel"],
            "region": job["region"],
            "requested_pool_id": job["account_pool_id"],
            "route_decision_ref": self._saga_body(job, attempt)["route_decision_ref"],
            # S22 按 `deductible_key` 去重；用 intent_id 而不是 attempt_id，
            # 因为这个 job 自己的 permit 不该被同 attempt 的另一个 job 复用。
            "deductible_key": intent_id,
        }
        try:
            return self._host("acquire_permit",
                job["tenant_id"], request, correlation_id=correlation_id)
        except ServiceError as exc:
            if exc.code in ("NO_CAPACITY", "POOL_NOT_FOUND_FOR_ROUTE",
                            "POOL_MODALITY_MISMATCH", "TENANT_LIMIT_EXCEEDED",
                            "FENCE_SUPERSEDED", "DEDUCTIBLE_KEY_CONFLICT"):
                # E14：预算已建、配额被拒 → **必须先释放预算**，再落 REJECTED_QUOTA。
                self._release_reservation(job, reservation["id"], "QUOTA_FAILED",
                                          evidence=exc.code,
                                          correlation_id=correlation_id)
                return self._transition(
                    job, "REJECTED_QUOTA", reason_code=exc.code,
                    extra_sets={"last_error_code": exc.code,
                                "last_error_at": self._now()},
                    correlation_id=correlation_id)
            if exc.retryable or exc.status >= 500:
                # §4 步骤 2 最后一行：停在 RESERVING，**保留预算**（permit 可能已建）。
                self._bump_saga_attempts(job)
                return self._transition(job, "RESERVING",
                                        reason_code="QUOTA_UNCERTAIN",
                                        correlation_id=correlation_id)
            raise

    def _release_reservation(self, job, reservation_id: str, reason: str, *,
                             evidence: str, correlation_id: str) -> None:
        """补偿释放。`REASONS_NEEDING_EVIDENCE` 里的原因必须带 `evidence_ref`。"""
        body = {
            "reason": reason,
            "evidence_ref": evidence,
            "attribution": {"attempt_id": job["attempt_id"],
                            "provider_job_id": job["provider_job_id"]},
        }
        try:
            self._host("release_budget", job["tenant_id"], reservation_id, body,
                                            correlation_id=correlation_id)
        except ServiceError as exc:
            if exc.status >= 500 or exc.retryable:
                raise       # 不能假装释放成功；让调用方看到 503 去重试
            raise

    # ---- 步骤 3 + 4：intent 落库后实时复查，再签 grant

    def _write_intent(self, job, attempt, reservation: dict, permit: dict,
                      intent_id: str, *, correlation_id: str) -> sqlite3.Row:
        requirement_set = _loads(attempt["requirement_set"]) or {}
        context = _loads(attempt["context"]) or {}
        submission_nonce = job["submission_nonce"]
        stable_key = stable_client_key_for(
            tenant_id=job["tenant_id"], attempt_id=job["attempt_id"],
            job_no=int(job["job_no"]), requirement_hash=job["requirement_hash"],
            submission_nonce=submission_nonce)
        # 幂等：同 job 只落一条 intent（`si_job_uniq`）。重入时复用原行。
        row = self._intent_for_job(job)
        if row is None:
            self.conn.execute(
                "INSERT INTO submission_intent (intent_id, provider_job_id,"
                " tenant_id, request_hash, route_decision_id,"
                " budget_reservation_id, budget_amount_micro, budget_currency,"
                " budget_authorized_at, quota_permit_id, quota_pool_id, quota_state,"
                " dispatch_id, fencing_token, stable_client_key,"
                " capability_bundle_rev, price_snapshot_id, node_epoch,"
                " execution_fence, state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " 'RESERVED', ?)",
                (intent_id, job["provider_job_id"], job["tenant_id"],
                 job["requirement_hash"], attempt["route_decision_id"],
                 reservation["id"], int(reservation["amount_micro"]),
                 reservation["currency"], self._now(),
                 permit["permit_id"], permit["pool_id"], permit.get("state", "RESERVED"),
                 # dispatch_id 在签发 grant 时才定；schema 要求 NOT NULL，
                 # 用 intent_id 派生一个占位并在签发时改写（步骤 4 同事务）。
                 f"dsp_pending_{intent_id}", 1, stable_key,
                 int(requirement_set.get("capability_bundle_revision") or 1),
                 str(requirement_set.get("price_snapshot_id") or ""),
                 int(attempt["node_epoch"]), int(attempt["execution_fence"]),
                 self._now()))
            row = self._intent_for_job(job)
        return self._saga_recheck_and_issue(job, attempt, row,
                                            correlation_id=correlation_id)

    def _saga_recheck_and_issue(self, job, attempt, intent,
                                *, correlation_id: str) -> sqlite3.Row:
        """§4 步骤 4 的三条实时复查。任一失败 → 补偿并进拒绝态；超时 → 停在
        `ADMITTED` 且**不签 grant**。"""
        reservation = self._stashed(job).get("reservation") or {
            "id": intent["budget_reservation_id"],
            "amount_micro": int(intent["budget_amount_micro"]),
            "currency": intent["budget_currency"],
        }
        live = self._host("read_reservation", job["tenant_id"], reservation["id"])
        if not live:
            # 1) S23：权限被撤 / 余额不足 → 释放 permit → REJECTED_BUDGET。
            self._release_permit(job, intent, "SUPERSEDED_BY_BUDGET",
                                 correlation_id=correlation_id,
                                 fake_ok=True)
            return self._transition(
                job, "REJECTED_BUDGET", reason_code="RESERVATION_NOT_LIVE",
                extra_sets={"last_error_code": "RESERVATION_NOT_LIVE",
                            "last_error_at": self._now()},
                correlation_id=correlation_id)
        if live.get("state") not in ("RESERVED", "AUTHORIZED"):
            self._release_permit(job, intent, "SUPERSEDED_BY_BUDGET",
                                 correlation_id=correlation_id, fake_ok=True)
            return self._transition(
                job, "REJECTED_BUDGET", reason_code="RESERVATION_NOT_AUTHORIZED",
                extra_sets={"last_error_code": "RESERVATION_NOT_AUTHORIZED",
                            "last_error_at": self._now()},
                correlation_id=correlation_id)
        if int(live.get("amount_conservative_micro")
               or live.get("amount_micro") or 0) != int(reservation["amount_micro"]):
            # 金额被改过：授权的是旧价，不能按旧价派单。
            self._release_permit(job, intent, "SUPERSEDED_BY_BUDGET",
                                 correlation_id=correlation_id, fake_ok=True)
            return self._transition(
                job, "REJECTED_BUDGET", reason_code="RESERVATION_AMOUNT_CHANGED",
                extra_sets={"last_error_code": "RESERVATION_AMOUNT_CHANGED",
                            "last_error_at": self._now()},
                correlation_id=correlation_id)

        # 1b) S23：RESERVED → AUTHORIZED。S23 说这是"持久 ack"，之后才可派单。
        try:
            self._host("authorize_budget",
                job["tenant_id"], reservation["id"],
                {"quote_still_valid": True}, correlation_id=correlation_id)
        except ServiceError as exc:
            if exc.code == "QUOTE_EXPIRED":
                self._release_permit(job, intent, "SUPERSEDED_BY_BUDGET",
                                     correlation_id=correlation_id, fake_ok=True)
                return self._transition(
                    job, "REJECTED_BUDGET", reason_code=exc.code,
                    extra_sets={"last_error_code": exc.code,
                                "last_error_at": self._now()},
                    correlation_id=correlation_id)
            if exc.retryable or exc.status >= 500:
                self._bump_saga_attempts(job)
                return job         # 停在 ADMITTED，不签 grant
            raise

        # 2) S22：RESERVED → DISPATCH_AUTHORIZED（第一次 confirm，不带 dispatch_id）。
        try:
            self._host("confirm_dispatch",
                job["tenant_id"], intent["quota_permit_id"],
                {"expected_grant_epoch": 1}, correlation_id=correlation_id)
        except ServiceError as exc:
            if exc.code in ("PERMIT_NOT_DISPATCHABLE", "PERMIT_TERMINAL",
                            "GRANT_EPOCH_CONFLICT"):
                self._release_reservation(job, reservation["id"], "QUOTA_FAILED",
                                          evidence=exc.code,
                                          correlation_id=correlation_id)
                return self._transition(
                    job, "REJECTED_QUOTA", reason_code=exc.code,
                    extra_sets={"last_error_code": exc.code,
                                "last_error_at": self._now()},
                    correlation_id=correlation_id)
            if exc.retryable or exc.status >= 500:
                self._bump_saga_attempts(job)
                return job         # 停在 ADMITTED，不签 grant
            raise

        # 3) S24：能力与价格仍然有效。`usable` 是本服务唯一认的判据。
        verdict = self._check_capability(job, attempt)
        if verdict is not True:
            code = verdict or "CAPABILITY_DRIFT"
            self._release_permit(job, intent, "SUPERSEDED_BY_FENCE",
                                 correlation_id=correlation_id, fake_ok=True)
            self._release_reservation(job, reservation["id"], "FENCE_SUPERSEDED",
                                      evidence=code, correlation_id=correlation_id)
            # C26-7 / §13.1：`ROUTE_INVALIDATED` 不是 16 态之一，落成
            # `PROVIDER_FAILED` + `last_error_code`，S21 会据此重新路由。
            return self._transition(
                job, "PROVIDER_FAILED", reason_code="ROUTE_INVALIDATED",
                extra_sets={"last_error_code": code or "ROUTE_INVALIDATED",
                            "last_error_at": self._now()},
                correlation_id=correlation_id)
        return self._issue_grant(job, attempt, intent, reservation,
                                 correlation_id=correlation_id)

    def _check_capability(self, job, attempt):
        """返回 `True` 表示可用；否则返回 S24 的失效码（字符串）。"""
        check = self._ports.get("check_capability_validity")
        if check is None:
            return True         # 未注入 = 不做这项检查（测试/降级），不猜失效
        requirement_set = _loads(attempt["requirement_set"]) or {}
        try:
            answer = check(job["tenant_id"], {"items": [{
                "snapshot_id": requirement_set.get("snapshot_id"),
                "price_snapshot_id": requirement_set.get("price_snapshot_id"),
                "combination_key": requirement_set.get("combination_key"),
                "pool_id": job["account_pool_id"],
            }]})
        except ServiceError as exc:
            if exc.retryable or exc.status >= 500:
                raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                                   "capability validity check is unavailable",
                                   retryable=True,
                                   details={"port": "check_capability_validity"})
            return exc.code
        items = (answer or {}).get("items") or []
        first = items[0] if items else {}
        if first.get("usable") is True:
            return True
        return first.get("code") or "CAPABILITY_INVALID"

    def _issue_grant(self, job, attempt, intent, reservation: dict, *,
                     correlation_id: str) -> sqlite3.Row:
        """§4 步骤 4 末段 + §10.3：一个事务里写 grant、intent 与派单命令。"""
        requirement_set = _loads(attempt["requirement_set"]) or {}
        context = _loads(attempt["context"]) or {}
        dispatch_id = self._new_id("dsp")
        # §10.6：`fencing_token` 是本 job 的 **grant 签发序号**，每次签发 +1。
        # 不是 S22 的 `grant_epoch`（那个在 acquire 时定死，confirm 不推进）。
        fencing_token = int(self.conn.execute(
            "SELECT COALESCE(MAX(fencing_token), 0) + 1 AS n FROM dispatch_grant"
            " WHERE tenant_id = ? AND provider_job_id = ?",
            (job["tenant_id"], job["provider_job_id"])).fetchone()["n"])
        now = self._now()
        # grant 有效期：S27 必须在这个窗口内 claim。取 `deadline` 与一个固定
        # 上限（5 分钟）中的较早者 —— 领不到就撤销重签，不花钱（§10.6）。
        deadline = attempt["deadline"]
        expires_at = min(deadline, _shift(now, _GRANT_TTL))
        submit_payload_ref = (
            f"s26://submission-intents/{intent['intent_id']}/payload@sha256:"
            f"{job['requirement_hash']}")

        with self._tx():
            # `dg_live_uniq` 只允许一个 job 有一张活的 grant（ISSUED/CLAIMED）。
            # 走到这里时上一张可能还是 CLAIMED（重提、退避到期重签），必须先撤。
            # 撤销 CLAIMED 的前提是"这次尝试已被证明没成功"；三个调用点各自
            # 已经证明过（`_resubmit` 有对账证据、`_reissue_due_grant` /
            # `scan_expired_grants` 的 grant 早已过期）。
            self.conn.execute(
                "UPDATE dispatch_grant SET state = 'REVOKED'"
                " WHERE tenant_id = ? AND provider_job_id = ?"
                " AND state IN ('ISSUED', 'CLAIMED')",
                (job["tenant_id"], job["provider_job_id"]))
            self.conn.execute(
                "INSERT INTO dispatch_grant (dispatch_id, provider_job_id, tenant_id,"
                " request_hash, fencing_token, stable_client_key, state,"
                " provider_idempotency, channel_route, submit_payload_ref, deadline,"
                " issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'ISSUED', ?, ?, ?,"
                " ?, ?, ?)",
                (dispatch_id, job["provider_job_id"], job["tenant_id"],
                 job["requirement_hash"], fencing_token, intent["stable_client_key"],
                 job["provider_idempotency"],
                 f"{job['provider']}/{job['channel']}", submit_payload_ref,
                 deadline, now, expires_at))
            self.conn.execute(
                "UPDATE submission_intent SET dispatch_id = ?, fencing_token = ?,"
                " quota_state = 'DISPATCH_AUTHORIZED', submit_payload_ref = ?,"
                " created_at = created_at WHERE intent_id = ? AND tenant_id = ?",
                (dispatch_id, fencing_token, submit_payload_ref,
                 intent["intent_id"], job["tenant_id"]))
            # `_transition` 自己会在同一事务里发 state-changed 事件。
            updated = self._transition(
                job, "DISPATCH_AUTHORIZED", reason_code="GRANT_ISSUED",
                correlation_id=correlation_id)
            # §10.3：派单命令是 Outbox 事件，**不是**同步调 S27。
            # 付款保护第一原则：没有成功 claim，就不发 HTTP。
            self.outbox.append(new_event(
                source=self.source, type="connector.submit.requested",
                subject=f"dispatch/{dispatch_id}",
                tenant_id=job["tenant_id"], home_cell=context.get("home_cell")
                or self.home_cell,
                aggregate_id=dispatch_id, sequence=1,
                correlation_id=correlation_id,
                causation_id=job["provider_job_id"],
                data={
                    "schema_version": "2.0",
                    "dispatch_id": dispatch_id,
                    "provider_job_id": job["provider_job_id"],
                    "tenant_id": job["tenant_id"],
                    "channel_route": f"{job['provider']}/{job['channel']}",
                    "stable_client_key": intent["stable_client_key"],
                    "fencing_token": fencing_token,
                    "request_hash": job["requirement_hash"],
                    "submit_payload_ref": submit_payload_ref,
                    "submit_payload_inline": requirement_set.get("normalized_input"),
                    "provider_idempotency": job["provider_idempotency"],
                    "deadline": deadline,
                }))
        return updated

    def _release_permit(self, job, intent, reason: str, *,
                        correlation_id: str, fake_ok: bool = False,
                        evidence: str | None = None) -> None:
        """补偿释放 permit。`fake_ok` 用于步骤 4 的"已经不可能派单"分支：
        permit 可能还是 `RESERVED`，`SUPERSEDED_BY_BUDGET` 在三个状态里都合法。

        `evidence` 覆盖默认的 `job:<id>:<reason>` 引用 —— 结算那条路要指向
        `settlement_id`，否则事后无法从 permit 侧追回"对应哪一笔结算"。
        """
        body = {"reason": reason,
                "terminal_evidence_ref": evidence
                or f"job:{job['provider_job_id']}:{reason}"}
        try:
            self._host("release_permit",
                job["tenant_id"], intent["quota_permit_id"], body,
                correlation_id=correlation_id)
        except ServiceError:
            if not fake_ok:
                raise
            # 释放失败不该掩盖真正的拒绝原因（余额/能力失效）。记下来，继续。

    # ------------------------------------------------------------ 派单许可

    def claim(self, tenant_id: str, dispatch_id: str, body: dict, *,
              correlation_id: str = "") -> dict:
        """§12.4 / §10.9：S27 领走一次派单许可。

        **付款保护第一原则：没有成功 claim，就不发 HTTP。** 所以这个方法是 S26
        唯一能让字节离开本机的入口，它必须是原子的、且有两道门：

        1. grant 必须是 `ISSUED` 且未过期、fence 对得上（否则 409/410/403）；
        2. `UPDATE provider_job ... WHERE state='DISPATCH_AUTHORIZED'` 必须命中
           恰好一行（§12.4 的第二道门）—— 即使 grant 表被并发搞乱，job 状态机
           也不允许从别的状态跳进 `SUBMITTING`。

        `submit_attempts` 在这里 +1：**在字节发出之前**。计数只增不减（PGJ-2）。
        """
        if not isinstance(body, dict):
            raise ServiceError(400, "MALFORMED_REQUEST", "claim body must be an object")
        executor_id = body.get("executor_id")
        if not executor_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "executor_id is required")
        fencing_token = body.get("fencing_token")
        if fencing_token is None:
            raise ServiceError(400, "MALFORMED_REQUEST", "fencing_token is required")

        with self._tx():
            grant = self.conn.execute(
                "SELECT * FROM dispatch_grant WHERE tenant_id = ? AND dispatch_id = ?",
                (tenant_id, dispatch_id)).fetchone()
            if grant is None:
                raise ServiceError(404, "GRANT_NOT_FOUND", "dispatch grant not found",
                                   details={"dispatch_id": dispatch_id})
            job = self._job(tenant_id, grant["provider_job_id"])

            if grant["state"] == "CLAIMED":
                raise ServiceError(
                    409, "GATE_ALREADY_CLAIMED",
                    "this grant has already been claimed",
                    details={"claimed_by": grant["claimed_by"],
                             "claimed_at": grant["claimed_at"]})
            if grant["state"] == "REVOKED":
                raise ServiceError(403, "GATE_REVOKED", "this grant has been revoked",
                                   details={"dispatch_id": dispatch_id})
            if int(fencing_token) != int(grant["fencing_token"]):
                raise ServiceError(
                    409, "GATE_FENCE_MISMATCH", "fencing_token does not match the grant",
                    details={"expected": int(grant["fencing_token"]),
                             "received": int(fencing_token)})
            request_hash = body.get("request_hash")
            if request_hash is not None and request_hash != grant["request_hash"]:
                raise ServiceError(
                    409, "GATE_REQUEST_HASH_MISMATCH",
                    "request_hash does not match the grant",
                    details={"expected": grant["request_hash"]})
            if _parse_ts(grant["expires_at"]) <= _parse_ts(self._now()):
                raise ServiceError(410, "GATE_EXPIRED", "this grant has expired",
                                   details={"expires_at": grant["expires_at"]})

            now = self._now()
            # 门 1：grant 本身从 ISSUED 翻到 CLAIMED（原子，靠 WHERE）。
            cur = self.conn.execute(
                "UPDATE dispatch_grant SET state = 'CLAIMED', claimed_at = ?,"
                " claimed_by = ? WHERE tenant_id = ? AND dispatch_id = ?"
                " AND state = 'ISSUED'",
                (now, executor_id, tenant_id, dispatch_id))
            if cur.rowcount != 1:
                raise ServiceError(409, "GATE_ALREADY_CLAIMED",
                                   "this grant was claimed concurrently")

            # 门 2：`UPDATE ... WHERE state='DISPATCH_AUTHORIZED'`。
            # job 不在这个状态 → 整个 claim 回滚，grant 也退回 ISSUED（事务边界里）。
            nxt = _shift(now, 2.0 * self._p99_submit_seconds)
            updated = self._transition(
                job, "SUBMITTING", reason_code="GRANT_CLAIMED",
                actor=executor_id, correlation_id=correlation_id,
                extra_sets={"submit_attempts": int(job["submit_attempts"]) + 1,
                            "next_action_at": nxt})

            intent = self._intent_for_job(updated)
            if intent is not None:
                self.conn.execute(
                    "UPDATE submission_intent SET state = 'CLAIMED', submitted_at = ?,"
                    " quota_state = 'INFLIGHT' WHERE tenant_id = ? AND intent_id = ?",
                    (now, tenant_id, intent["intent_id"]))
            self.conn.execute(
                "UPDATE generation_attempt SET state = state, created_at = created_at"
                " WHERE tenant_id = ? AND attempt_id = ?",
                (tenant_id, updated["attempt_id"]))

        return {
            "dispatch_id": dispatch_id,
            "provider_job_id": grant["provider_job_id"],
            "attempt_id": updated["attempt_id"],
            "stable_client_key": grant["stable_client_key"],
            "submit_payload_ref": grant["submit_payload_ref"],
            "provider_idempotency": grant["provider_idempotency"],
            "channel_route": grant["channel_route"],
            "fencing_token": int(grant["fencing_token"]),
            "request_hash": grant["request_hash"],
            "deadline": grant["deadline"],
            "submit_attempts": int(updated["submit_attempts"]),
        }

    # ---------------------------------------------------------------- 查询

    def get_job(self, tenant_id: str, provider_job_id: str) -> dict:
        """§12.2 的查询体。14 个字段，`must_reconcile_before_resubmit` 是
        `state == 'SUBMISSION_UNKNOWN'` 的同义词 —— 不是另存的一份状态。"""
        job = self._job(tenant_id, provider_job_id)
        attempt = self._attempt(tenant_id, job["attempt_id"])
        requirement_set = _loads(attempt["requirement_set"]) or {}
        history = [
            {"seq": int(r["seq"]), "from": r["from_state"], "to": r["to_state"],
             "at": r["occurred_at"], "reason_code": r["reason_code"]}
            for r in self.conn.execute(
                "SELECT * FROM job_transition WHERE tenant_id = ? AND provider_job_id = ?"
                " ORDER BY seq", (tenant_id, provider_job_id)).fetchall()]
        refs = _loads(job["artifact_refs"] or "") or None
        intent = self._intent_for_job(job)
        return {
            "provider_job_id": job["provider_job_id"],
            # §12.2 的 `source_revision` 是**路由决策的版本**，不是 attempt 版本；
            # S21 用它判断"这份结果对应哪一版选型"。
            "source_revision": int(requirement_set.get("route_revision") or 1),
            "state": job["state"],
            "business_attempt": int(job["business_attempt"]),
            "network_retry": int(job["network_retry"]),
            "submit_attempts": int(job["submit_attempts"]),
            "provider_job_ref": job["provider_job_ref"],
            "submitted_at": intent["submitted_at"] if intent is not None else None,
            "last_observation_at": job["last_observed_at"],
            "poll_watermark": job["poll_watermark"],
            "output": {"artifact_refs": refs},
            "must_reconcile_before_resubmit": job["state"] == "SUBMISSION_UNKNOWN",
            "frozen": job["state"] in FROZEN_STATES,
            "node": {"node_id": attempt["node_id"],
                     "node_epoch": int(attempt["node_epoch"]),
                     "execution_fence": int(attempt["execution_fence"])},
            "history": history,
        }

    def get_admission(self, tenant_id: str, admission_id: str) -> dict:
        """§12.3：产出 `admission-record.v2.schema.json` 的记录。

        `admission_id` 由 `(tenant, provider_job_id)` 派生（`admission_id_for`），
        所以反查不需要另存索引，也不可能出现"admission 与 job 不一致"。

        **外键引用只回吐引用，不回吐金额/状态**：本函数里 S23 的 `amount_micro`、
        `state` 与 S22 的 `pool_id`、`state` 是**我们记下的派单时快照**。权威仍在
        S23/S22；这里之所以必须存一份，是因为 `SUBMISSION_UNKNOWN` 期间禁止去问
        它们（§7.2 第 2/3 条：不释放就说明状态没变）。见 docs/01 权威表。
        """
        intent = self._intent(tenant_id, admission_id)
        if intent is None:
            derived = intent_id_of(admission_id)
            if derived is not None:
                intent = self._intent(tenant_id, derived)
        if intent is None:
            raise ServiceError(404, "ADMISSION_NOT_FOUND",
                               "admission not found",
                               details={"admission_id": admission_id})
        job = self._job(tenant_id, intent["provider_job_id"])
        attempt = self._attempt(tenant_id, job["attempt_id"])
        context = _loads(attempt["context"]) or {}
        requirement_set = _loads(attempt["requirement_set"]) or {}
        grant = self._grant_of(job)
        # grant_state：没签过 = NONE；撤销过 = REVOKED；否则看当前那张。
        if grant is None:
            grant_state = "NONE"
        elif grant["state"] == "REVOKED":
            grant_state = "REVOKED"
        else:
            grant_state = grant["state"]
        return {
            "schema_version": "2.0",
            "admission_id": admission_id_for(intent["intent_id"]),
            "attempt_id": context.get("attempt_id") or attempt["attempt_id"],
            "context": context,
            "route_decision_ref": {
                "service_id": "S25",
                "id": attempt["route_decision_id"],
                "revision": int(requirement_set.get("route_revision") or 1),
                "content_hash": requirement_set.get("route_content_hash") or "",
            },
            # 与 context.request_hash 同一个值（示例里两个字段一致）。
            "request_hash": context.get("request_hash") or attempt["request_hash"],
            "state": ADMISSION_STATE.get(job["state"], "REJECTED"),
            "reservation": {
                "id": intent["budget_reservation_id"],
                "authority": "S23",
                "amount_micro": int(intent["budget_amount_micro"]),
                "currency": intent["budget_currency"],
                "state": self._reservation_state_echo(job, intent),
            },
            "quota_permit": {
                "id": intent["quota_permit_id"],
                "authority": "S22",
                "pool_id": intent["quota_pool_id"],
                "state": intent["quota_state"],
            },
            "dispatch": {
                "dispatch_id": intent["dispatch_id"],
                "fencing_token": int(intent["fencing_token"]),
                "grant_state": grant_state,
                "provider_idempotency": job["provider_idempotency"],
                "provider_job_id": job["provider_job_ref"],
            },
            "must_reconcile_before_resubmit": job["state"] == "SUBMISSION_UNKNOWN",
        }

    def _reservation_state_echo(self, job, intent) -> str:
        """回吐 S23 的 reservation state。

        `UNKNOWN` 期间**必须**是 `AUTHORIZED`（§7.2 第 2 条：不释放预算）。
        之后的结算可能让它变 `ACCRUED`；终态是 `SETTLED`/`RELEASED`。
        在能问 S23 的时候问一次，问不到就用最后一次观测到的值 —— 绝不猜。
        """
        state = job["state"]
        if state == "SUBMISSION_UNKNOWN":
            return "AUTHORIZED"
        if state in ("REJECTED_QUOTA",):
            return "RELEASED"
        read = self._ports.get("read_reservation")
        if read is not None:
            try:
                live = read(job["tenant_id"], {"id": intent["budget_reservation_id"]})
            except ServiceError:
                live = None
            if live and live.get("state"):
                return live["state"]
        if state in ("ARCHIVED",):
            return "SETTLED"
        if state in ("PROVIDER_FAILED", "CANCELED", "REJECTED_BUDGET"):
            return "RELEASED"
        return "AUTHORIZED" if state not in ("CREATED", "RESERVING") else "RESERVED"

    # ---------------------------------------------------- 提交结果与观测

    def record_submit_outcome(self, tenant_id: str, dispatch_id: str, outcome: str,
                              *, provider_job_ref: str | None = None,
                              error_class: str | None = None,
                              retry_after_seconds: float | None = None,
                              correlation_id: str = "") -> dict:
        """S27 的四个结论 → S26 的状态决定（§7.1 最后一句）。

        S27 只上报 `not_sent | unknown | rejected | accepted`；**判断状态的权力
        在 S26**。四者里只有 `not_sent` 可以立刻回到可派单态 —— 它代表"S27 能
        证明字节没有离开本机"（§7.1：只有这类失败可重试）。
        """
        if outcome not in SUBMIT_OUTCOMES:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"outcome must be one of {sorted(SUBMIT_OUTCOMES)}",
                               details={"outcome": outcome})
        with self._tx():
            grant = self.conn.execute(
                "SELECT * FROM dispatch_grant WHERE tenant_id = ? AND dispatch_id = ?",
                (tenant_id, dispatch_id)).fetchone()
            if grant is None:
                raise ServiceError(404, "GRANT_NOT_FOUND", "dispatch grant not found",
                                   details={"dispatch_id": dispatch_id})
            job = self._job(tenant_id, grant["provider_job_id"])
            if job["state"] != "SUBMITTING":
                # 重复投递 / 迟到的回执：不倒退，直接回当前状态（幂等）。
                return {"provider_job_id": job["provider_job_id"],
                        "state": job["state"], "applied": False}

            if outcome == "not_sent":
                return self._on_not_sent(job, grant, error_class,
                                         retry_after_seconds=retry_after_seconds,
                                         correlation_id=correlation_id)
            if outcome == "rejected":
                self._release_on_terminal_reject(
                    job, error_class, correlation_id=correlation_id)
                return self._to_row(self._transition(
                    job, "PROVIDER_FAILED",
                    reason_code=error_class or "SUBMIT_REJECTED",
                    extra_sets={"last_error_code": error_class or "SUBMIT_REJECTED",
                                "last_error_at": self._now()},
                    correlation_id=correlation_id))
            if outcome == "accepted" and provider_job_ref:
                return self._to_row(self._transition(
                    job, "QUEUED", reason_code="SUBMIT_ACCEPTED",
                    extra_sets={"provider_job_ref": provider_job_ref,
                                "last_observed_at": self._now(),
                                "next_action_at": _shift(
                                    self._now(), self._poll_interval_seconds())},
                    correlation_id=correlation_id))
            # `unknown`，以及"收到 2xx 但解析不出 ref"（S27 归类为 unknown）。
            return self._on_unknown(job, correlation_id=correlation_id)

    # §13 第 4 行（401/403）与第 7 行（模型退休）都要求**两侧都释放、且不换渠道**。
    # 唯一不做释放的是 4xx 参数类拒绝（`PROVIDER_REJECTED`）：那是"供应商明确
    # 未受理但可能已按失败计费"，钱要由 `settle_job` 按实际发生额结，而不是就地
    # 释放 —— 提前放掉预留，结算时就没有可扣的上界了。
    _RELEASE_ON_REJECT = frozenset({
        "ACCOUNT_UNAUTHORIZED", "FORBIDDEN", "CAPABILITY_DRIFT",
        "ROUTE_INVALIDATED", "MODEL_RETIRED", "QUOTA_EXHAUSTED",
    })

    def _release_on_terminal_reject(self, job, error_class: str | None, *,
                                    correlation_id: str) -> None:
        code = (error_class or "").upper()
        if code not in self._RELEASE_ON_REJECT:
            return
        intent = self._intent_for_job(job)
        if intent is None:
            return
        # 换渠道是 §13 明文禁止的"绕过"；这里只隔离账户池并释放，不改 job 的
        # `provider`/`channel`/`account_pool_id` —— 重新路由是 S25/S21 的事。
        # 释放理由用 `OPERATOR`：S23 的 `RELEASE_REASONS` 没有"供应商拒绝"这一档，
        # 而且这一路本来就要人工隔离账户池（C26-25）。
        self._release_permit(job, intent, "OPERATOR",
                             correlation_id=correlation_id, fake_ok=True)
        self._release_reservation(job, intent["budget_reservation_id"],
                                  "OPERATOR", evidence=code,
                                  correlation_id=correlation_id)
        # intent 用 `ABANDONED` 而不是 `SETTLED`：没有结算发生（没调 settle_budget），
        # 写 `SETTLED` 会让 `settle_job` 的重放分支直接返回"已结算"。
        self.conn.execute(
            "UPDATE submission_intent SET state = 'ABANDONED', resolved_at = ?"
            " WHERE tenant_id = ? AND intent_id = ?",
            (self._now(), job["tenant_id"], intent["intent_id"]))
        # §13 第 4 行（401/403）的「隔离账户池」与第 6 行的「写 capability
        # drift 回 S24」。两条都是**尽力而为**：S24 拒收不能反过来把这一次
        # 拒绝变成 503 —— job 已经进 PROVIDER_FAILED，两侧也已释放，重来一遍
        # 会再释放一次。失败只记在 `last_error_code` 的尾巴上留给告警。
        self._notify_s24(job, code, correlation_id=correlation_id)

    def _notify_s24(self, job, code: str, *, correlation_id: str) -> None:
        """把「这个池/这个组合有问题」告诉 S24。**不改本服务任何权威状态。**"""
        errors: list[str] = []
        try:
            if code in ("ACCOUNT_UNAUTHORIZED", "FORBIDDEN"):
                isolate = self._ports.get("isolate_account_pool")
                if isolate is not None:
                    isolate(job["tenant_id"], {
                        "pool_id": job["account_pool_id"],
                        "health": "unauthorized",
                        "reason": code,
                        "evidence_ref": f"job:{job['provider_job_id']}:{code}",
                    })
            elif code in ("CAPABILITY_DRIFT", "MODEL_RETIRED"):
                # §13.1 的 `POST /capability-drift-reports`。`MODEL_RETIRED` 不是
                # capability drift，但两者对 S24 的含义都是"这份快照过期了"，
                # 都会让 S24 降低 confidence；分开的理由是多一个 `observed_error`。
                drift = self._ports.get("report_capability_drift")
                if drift is not None:
                    attempt = self._attempt(job["tenant_id"], job["attempt_id"])
                    requirement_set = _loads(attempt["requirement_set"]) or {}
                    drift(job["tenant_id"], {
                        "command_id": f"{job['provider_job_id']}:drift",
                        "snapshot_id": requirement_set.get("snapshot_id"),
                        "mode": (requirement_set.get("normalized_input") or {})
                        .get("input_mode") or attempt["modality"],
                        "combination_key": requirement_set.get("combination_key"),
                        "observed_error": {
                            "provider_code": code,
                            "occurred_at": self._now(),
                        },
                        "provider_job_id": job["provider_job_id"],
                        "attempt_id": job["attempt_id"],
                    })
        except Exception as exc:     # noqa: BLE001 —— 见上：不许反向污染
            errors.append(getattr(exc, "code", None) or type(exc).__name__)
        if errors:
            self.conn.execute(
                "UPDATE provider_job SET last_error_code = COALESCE(last_error_code, '')"
                " || ';S24_NOTIFY_FAILED:' || ?, updated_at = ?"
                " WHERE tenant_id = ? AND provider_job_id = ?",
                (",".join(errors), self._now(), job["tenant_id"],
                 job["provider_job_id"]))

    def _to_row(self, job) -> dict:
        return {"provider_job_id": job["provider_job_id"], "state": job["state"],
                "applied": True}

    def _on_not_sent(self, job, grant, error_class: str | None, *,
                     retry_after_seconds: float | None = None,
                     correlation_id: str) -> dict:
        """字节从未离开 → 可以安全换一张新 grant 重发（同 stable_client_key）。

        `submit_attempts` **不回退**（PGJ-2：只增不减）——它记的是"我们让 S27
        尝试过几次"，不是"供应商看到过几次"。但这意味着重签必须有上限，否则
        `not_sent` 可以无限循环。上限就是 §7.4 第 4 条那个数。

        `retry_after_seconds` 非空时（§13 第一行：429 且供应商明确未受理）走
        **退避**分支：回 `DISPATCH_AUTHORIZED` 但**这一轮不签新 grant**，只把
        `next_action_at` 推到 `now + Retry-After`，到点由 `scan_due_jobs` 重走
        步骤 4。签了 grant 就等于立刻发出一条 `connector.submit.requested`，
        那和"尊重 Retry-After"正好相反。预算与 permit 全程保留。
        """
        spent = int(self.conn.execute(
            "SELECT COALESCE(SUM(submit_attempts), 0) AS n FROM provider_job"
            " WHERE tenant_id = ? AND attempt_id = ?",
            (job["tenant_id"], job["attempt_id"])).fetchone()["n"])
        if spent >= self._default_max_submits:
            return self._to_row(self._transition(
                job, "PROVIDER_FAILED", reason_code="SUBMIT_CAP_EXCEEDED",
                extra_sets={"last_error_code": "SUBMIT_CAP_EXCEEDED",
                            "last_error_at": self._now()},
                correlation_id=correlation_id))
        # 旧 grant 变 REVOKED：它对应的这次尝试已被 S27 明确否认。
        # （CLAIMED 之后原则上不撤销，这里是唯一的例外，前提是 S27 证明了未发出。
        #  见 C26-19。）
        self.conn.execute(
            "UPDATE dispatch_grant SET state = 'REVOKED' WHERE tenant_id = ?"
            " AND dispatch_id = ? AND state = 'CLAIMED'",
            (job["tenant_id"], grant["dispatch_id"]))
        intent = self._intent_for_job(job)
        if intent is not None:
            self.conn.execute(
                "UPDATE submission_intent SET state = 'RESERVED',"
                " quota_state = 'DISPATCH_AUTHORIZED'"
                " WHERE tenant_id = ? AND intent_id = ?",
                (job["tenant_id"], intent["intent_id"]))
        code = error_class or ("SUPPLIER_RATE_LIMITED"
                               if retry_after_seconds is not None else "NOT_SENT")
        extra = {"last_error_code": code, "last_error_at": self._now()}
        if retry_after_seconds is not None:
            extra["next_action_at"] = _shift(
                self._now(), max(0.0, float(retry_after_seconds)))
        back = self._transition(
            job, "DISPATCH_AUTHORIZED", reason_code=code,
            extra_sets=extra, correlation_id=correlation_id)
        if retry_after_seconds is not None:
            return self._to_row(back)
        attempt = self._attempt(job["tenant_id"], job["attempt_id"])
        reissued = self._issue_grant(back, attempt, self._intent_for_job(back),
                                     self._stashed(back).get("reservation") or {},
                                     correlation_id=correlation_id)
        return self._to_row(reissued)

    def _on_unknown(self, job, *, correlation_id: str) -> dict:
        """进入 `SUBMISSION_UNKNOWN`。六条铁律在这里体现为"什么都不做"。"""
        return self._to_row(self._transition(
            job, "SUBMISSION_UNKNOWN", reason_code="SUBMIT_UNCERTAIN",
            extra_sets={"last_error_code": "SUBMIT_UNCERTAIN",
                        "last_error_at": self._now(),
                        # 第 1 轮对账 T+0，`reconcile_due` 立刻捡起来。
                        "next_action_at": self._now()},
            correlation_id=correlation_id))

    def expire_submit_timer(self, tenant_id: str, provider_job_id: str, *,
                            correlation_id: str = "") -> dict:
        """§7.1 的 submit-confirm timer：`2×p99` 到期未见回执 → UNKNOWN。

        「提交确认」与「超时」分离的落点：计时器到期不代表失败，只代表**不知道**。
        """
        job = self._job(tenant_id, provider_job_id)
        if job["state"] != "SUBMITTING":
            return {"provider_job_id": provider_job_id, "state": job["state"],
                    "applied": False}
        if job["next_action_at"] and _parse_ts(job["next_action_at"]) > _parse_ts(
                self._now()):
            return {"provider_job_id": provider_job_id, "state": job["state"],
                    "applied": False}
        with self._tx():
            job = self._job(tenant_id, provider_job_id)
            updated = self._on_unknown(job, correlation_id=correlation_id)
        return updated

    def _poll_interval_seconds(self) -> float:
        return float(self._ports.get("poll_interval_seconds") or 15.0)

    def consume_observation(self, tenant_id: str, envelope: dict, *,
                            correlation_id: str = "") -> dict:
        """消费 S27 的 `connector.observation.received`。

        四道防线，缺一不可：

        1. **Inbox 去重**（`event_id`）——at-least-once 投递；
        2. **fence / epoch 双校验**——旧 fence 的事件不许推进当前 job（§9.2）；
        3. **`PROGRESS_RANK` 反倒退**——乱序到达的旧观测不许把 RUNNING 拉回 QUEUED
           （T26-15）；
        4. **`not_found` 永不进状态机**——它是"供应商列表里没有"，不是终态。
        """
        event_id = envelope.get("id")
        if not event_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "observation event has no id")
        data = envelope.get("data") or {}
        with self._tx():
            if not self.inbox.consume("S26", event_id):
                return {"applied": False, "reason": "DUPLICATE"}
            job = self._resolve_observation_job(tenant_id, envelope, data)
            if job is None:
                # C26-18 / S27 §6.4：回调早到、只带 provider_job_ref。
                # 不丢弃、不减分；由 `orphan_observations` 保留等待关联。
                return {"applied": False, "reason": "UNMAPPED",
                        "stable_client_key": data.get("stable_client_key")}
            if job["tenant_id"] != tenant_id:
                raise ServiceError(403, "FORBIDDEN_SCOPE",
                                   "observation belongs to another tenant")
            applied = self._apply_observation(
                job, data, correlation_id=correlation_id or
                str(envelope.get("correlation_id") or ""))
        return applied

    def _resolve_observation_job(self, tenant_id: str, envelope: dict,
                                 data: dict):
        """按 `aggregate_id` → `provider_job_id` → `stable_client_key` 逐级关联。

        第 3 级是 T26-13 的落点：`provider_job_ref` 未知时仍要能挂上
        （`stable_client_key` 在 intent 上有唯一约束）。
        """
        job_id = envelope.get("aggregate_id") or data.get("provider_job_id")
        if job_id:
            row = self.conn.execute(
                "SELECT * FROM provider_job WHERE tenant_id = ? AND provider_job_id = ?",
                (tenant_id, job_id)).fetchone()
            if row is not None:
                return row
        ref = data.get("provider_job_ref")
        if ref:
            row = self.conn.execute(
                "SELECT * FROM provider_job WHERE tenant_id = ? AND provider_job_ref = ?",
                (tenant_id, ref)).fetchone()
            if row is not None:
                return row
        key = data.get("stable_client_key")
        if key:
            intent = self.conn.execute(
                "SELECT * FROM submission_intent WHERE tenant_id = ?"
                " AND stable_client_key = ?", (tenant_id, key)).fetchone()
            if intent is not None:
                return self._job(tenant_id, intent["provider_job_id"])
        dispatch_id = data.get("dispatch_id")
        if dispatch_id:
            grant = self.conn.execute(
                "SELECT * FROM dispatch_grant WHERE tenant_id = ? AND dispatch_id = ?",
                (tenant_id, dispatch_id)).fetchone()
            if grant is not None:
                return self._job(tenant_id, grant["provider_job_id"])
        return None

    def _apply_observation(self, job, data: dict, *, correlation_id: str) -> dict:
        raw_status = str(data.get("status") or "").strip().lower()
        provider_ref = data.get("provider_job_ref")
        observed_at = data.get("observed_at") or self._now()

        if raw_status in SUBMIT_OUTCOMES and data.get("provider_status_raw") in (
                None, "", "SUBMIT_OUTCOME"):
            # S27 的提交结论（`not_sent` 等）走与回执相同的判定。
            return self.record_submit_outcome(
                job["tenant_id"], data.get("dispatch_id") or "",
                raw_status, provider_job_ref=provider_ref,
                error_class=data.get("error_class"),
                retry_after_seconds=data.get("retry_after_seconds"),
                correlation_id=correlation_id)

        if raw_status in OBSERVATION_NON_ADVANCING:
            # `not_found` / `inconclusive`：只是对账信号，**永远**不改 job 状态。
            return {"provider_job_id": job["provider_job_id"],
                    "state": job["state"], "applied": False,
                    "reason": raw_status}

        target = OBSERVATION_STATUS_MAP.get(raw_status)
        if target is None:
            # 未知取值不猜：已知的照常推进，未知的留给对账。
            return {"provider_job_id": job["provider_job_id"],
                    "state": job["state"], "applied": False,
                    "reason": "UNKNOWN_STATUS"}

        # 旧 fence / 旧 epoch 的观测一律不推进（§9.2 第三行）。
        attempt = self._attempt(job["tenant_id"], job["attempt_id"])
        if data.get("execution_fence") is not None and int(
                data["execution_fence"]) < int(attempt["execution_fence"]):
            return {"provider_job_id": job["provider_job_id"],
                    "state": job["state"], "applied": False,
                    "reason": "STALE_FENCE"}
        if data.get("node_epoch") is not None and int(
                data["node_epoch"]) < int(attempt["node_epoch"]):
            return {"provider_job_id": job["provider_job_id"],
                    "state": job["state"], "applied": False,
                    "reason": "STALE_EPOCH"}

        # 反倒退：已到达的进度比观测的更靠前 → 只更新观测水印，不改状态（T26-15）。
        if (job["state"] in PROGRESS_RANK and target in PROGRESS_RANK
                and PROGRESS_RANK[target] <= PROGRESS_RANK[job["state"]]
                and target != job["state"]):
            self._touch_observation(job, data, observed_at)
            return {"provider_job_id": job["provider_job_id"],
                    "state": job["state"], "applied": False,
                    "reason": "OUT_OF_ORDER"}

        # §3.1 第 17 行：UNKNOWN 且已有 ref 的行不该存在（schema 也禁止）。
        # 对账找到时是 `RECONCILING`，这里给一条合法路径。
        if job["state"] in ("SUBMISSION_UNKNOWN", "RECONCILING") and not provider_ref:
            return {"provider_job_id": job["provider_job_id"],
                    "state": job["state"], "applied": False,
                    "reason": "NO_PROVIDER_REF"}

        extra = {"last_observed_at": observed_at,
                 "poll_watermark": observed_at}
        if provider_ref:
            extra["provider_job_ref"] = provider_ref
        if target in ("QUEUED", "RUNNING"):
            extra["next_action_at"] = _shift(
                self._now(), self._poll_interval_seconds())
        if data.get("output"):
            extra["output_snapshot"] = _merge_output(
                job["output_snapshot"], data["output"])

        if job["state"] == target:
            # 同态重试：状态不动，但**观测到的事实**要落库。§8.3 的重归档
            # 依赖这一点 —— 供应商换发了新 URL 时，job 仍是 `INGESTING`，
            # 丢掉这条 output 就等于永远只能拿旧 URL 去重试（T26-25）。
            self._touch_observation(job, data, observed_at)
            if "output_snapshot" in extra:
                self.conn.execute(
                    "UPDATE provider_job SET output_snapshot = ?, updated_at = ?"
                    " WHERE tenant_id = ? AND provider_job_id = ?",
                    (extra["output_snapshot"], self._now(),
                     job["tenant_id"], job["provider_job_id"]))
            return {"provider_job_id": job["provider_job_id"], "state": job["state"],
                    "applied": False, "reason": "SAME_STATE"}
        updated = self._transition(job, target, reason_code="PROVIDER_OBSERVATION",
                                   correlation_id=correlation_id,
                                   extra_sets=extra)
        result = {"provider_job_id": updated["provider_job_id"],
                  "state": updated["state"],
                  "previous_state": job["state"], "applied": True}
        if target in ("PROVIDER_SUCCEEDED", "PROVIDER_FAILED", "CANCELED"):
            result["next"] = self._after_terminal(
                updated, data, correlation_id=correlation_id)
        return result

    def _touch_observation(self, job, data: dict, observed_at: str) -> None:
        """只推进观测水印。乱序观测不许倒退状态，但它的时间戳仍是有效事实。"""
        sets = ["last_observed_at = ?", "updated_at = ?",
                "poll_watermark = ?"]
        args: list[Any] = [observed_at, self._now(), observed_at]
        ref = data.get("provider_job_ref")
        if ref and not job["provider_job_ref"]:
            sets.append("provider_job_ref = ?")
            args.append(ref)
        args.extend([job["tenant_id"], job["provider_job_id"]])
        self.conn.execute(
            "UPDATE provider_job SET " + ", ".join(sets)
            + " WHERE tenant_id = ? AND provider_job_id = ?", args)

    def _after_terminal(self, job, data: dict, *, correlation_id: str) -> dict:
        """`PROVIDER_SUCCEEDED` 立即进 `INGESTING`（§8：归档前置优先）。

        失败与取消留在这里 —— 结算发生在 `PROVIDER_SUCCEEDED` / `PROVIDER_FAILED` /
        `CANCELED` **之后**、`ARCHIVED` **之前**（§4 步骤 7）。
        """
        if job["state"] == "PROVIDER_SUCCEEDED":
            ingest = self.start_ingest(job["tenant_id"], job["provider_job_id"],
                                       correlation_id=correlation_id)
            return {"ingest": ingest}
        return {"needs_settlement": True}

    # -------------------------------------------------------------- 归档

    def _next_artifact_seq(self, intent) -> int:
        """§8.2 重归档要换新 `command_id`，job 不变。

        `artifact_seq` 是这份 intent 上的重归档序号 —— 同一个 S28 命令只在
        同一个 seq 上重试（同 `command_id` 幂等），跨 seq 就是新命令。
        """
        return int(intent["artifact_seq"] or 0) + 1

    def start_ingest(self, tenant_id: str, provider_job_id: str, *,
                     correlation_id: str = "", rearchive: bool = False) -> dict:
        """§8.1：把已成功的结果交给 S28 归档。**只有这一条路能产出成片。**

        `PROVIDER_SUCCEEDED → INGESTING` 必须立即发起（§8：「归档前置优先」），
        所以这里在观测消费的同一事务里就被调用，不排队。
        """
        job = self._job(tenant_id, provider_job_id)
        if job["state"] not in ("PROVIDER_SUCCEEDED", "INGESTING"):
            raise ServiceError(
                409, "NOT_INGESTABLE",
                f"job is {job['state']}, not PROVIDER_SUCCEEDED/INGESTING",
                details={"state": job["state"]})
        intent = self._intent_for_job(job)
        if intent is None:
            raise ServiceError(500, "INTENT_MISSING",
                               "job has no submission_intent; cannot archive")
        snapshot = _loads(job["output_snapshot"] or "") or {}
        output = snapshot.get("output") or {}
        provider_url, expires_at, expected = self._archive_source(output)

        if not rearchive:
            job = self._transition(job, "INGESTING", reason_code="ARCHIVE_STARTED",
                                   correlation_id=correlation_id)
        seq = self._next_artifact_seq(intent)
        attempt = self._attempt(tenant_id, job["attempt_id"])
        requirement_set = _loads(attempt["requirement_set"]) or {}
        body = {
            "command_id": f"{intent['intent_id']}:ingest:{seq}",
            "tenant_id": tenant_id,
            "content_class": CONTENT_CLASS_BY_MODALITY.get(attempt["modality"],
                                                           "generated_video"),
            # C26-9：S28 只从 `source` 里读 URL 与到期时间，顶层字段它不看。
            "source": {
                "kind": "provider_url",
                "provider_url": provider_url,
                "provider_url_expires_at": expires_at,
                "provider_job_id": job["provider_job_id"],
                "attempt_id": job["attempt_id"],
            },
            # C26-10：S28 强制要求 `scope_ref`（它靠它判去重可见性）。
            "scope_ref": {
                "tenant_id": tenant_id,
                "project_id": attempt["project_id"],
                "series_id": attempt["series_id"],
                "episode_id": attempt["episode_id"],
                "branch_id": attempt["branch_id"],
                "node_id": attempt["node_id"],
                "node_epoch": int(attempt["node_epoch"]),
            },
            "retention": {"policy_ref": (requirement_set.get("policy_snapshot_id")
                                         or "default")},
        }
        if expected:
            body["expected_sha256"] = expected
        answer = self._require("create_artifact_import")(tenant_id, body)
        self.conn.execute(
            "UPDATE submission_intent SET artifact_seq = ? WHERE tenant_id = ?"
            " AND intent_id = ?", (seq, tenant_id, intent["intent_id"]))
        return {"import_id": answer.get("import_id"), "state": answer.get("state"),
                "artifact_seq": seq, "command_id": body["command_id"]}

    def _archive_source(self, output: dict) -> tuple[str, str, str | None]:
        """从最后一次成功观测里取归档所需的三个值。

        取不到 URL 就是"没有可归档的东西"—— 不能编一个，也不能跳过（§8.2：
        宁可报缺口）。URL 过期与否不在这里判，交给 S28 的 `URL_EXPIRED`
        （它自己看 `provider_url_expires_at`），因为 §8.3 的补救要先问供应商。
        """
        url, expires, expected = extract_artifact_source(output)
        if not url:
            raise ServiceError(409, "NO_ARTIFACT_URL",
                               "last observation carried no provider URL",
                               details={"has_output": bool(output)})
        if not expires:
            # S28 要求 provider_url 必须带到期时间，否则它没法判 TTL。
            raise ServiceError(409, "NO_ARTIFACT_URL",
                               "last observation carried no provider_url_expires_at",
                               details={"has_output": bool(output)})
        return url, expires, expected

    def on_artifact_ingested(self, tenant_id: str, envelope: dict, *,
                             correlation_id: str = "") -> dict:
        """消费 `artifact.ingested`：`INGESTING → ARCHIVED`。

        C26-18：这个事件的数据里**没有**顶层 `provider_job_id`，只有
        `source.provider_job_id`。两级都试，别只认一个。
        """
        event_id = envelope.get("id")
        if not event_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "artifact.ingested has no id")
        data = envelope.get("data") or {}
        source = data.get("source") or {}
        with self._tx():
            if not self.inbox.consume("S26", event_id):
                return {"applied": False, "reason": "DUPLICATE"}
            job_id = source.get("provider_job_id") or envelope.get("aggregate_id")
            job = None
            if job_id:
                job = self.conn.execute(
                    "SELECT * FROM provider_job WHERE tenant_id = ?"
                    " AND provider_job_id = ?", (tenant_id, job_id)).fetchone()
            if job is None:
                return {"applied": False, "reason": "UNMAPPED"}
            if job["state"] != "INGESTING":
                return {"applied": False, "reason": "NOT_INGESTING",
                        "state": job["state"]}
            ref = data.get("artifact_ref") or {}
            refs = _loads(job["artifact_refs"] or "") or []
            if ref and ref.get("id") and all(r.get("id") != ref["id"] for r in refs):
                refs.append(ref)
            updated = self._transition(
                job, "ARCHIVED", reason_code="ARTIFACT_INGESTED",
                correlation_id=correlation_id,
                extra_sets={"artifact_refs": _dumps(refs)})
        return {"provider_job_id": updated["provider_job_id"],
                "state": updated["state"], "artifact_refs": refs,
                "applied": True}

    def report_ingest_gap(self, tenant_id: str, provider_job_id: str, *,
                          reason: str, correlation_id: str = "") -> dict:
        """§8.2 的最后一格：归档重试耗尽 → `INGEST_FAILED`，缺口可见。

        E23 要的是「最终缺口可见」，不是「假装成功」。所以这里把 job 收进
        `ARCHIVED`（终态）并留下 `last_error_code`，而不去重生成。
        """
        job = self._job(tenant_id, provider_job_id)
        if job["state"] != "INGESTING":
            raise ServiceError(409, "NOT_INGESTABLE",
                               f"job is {job['state']}, not INGESTING")
        return self._to_row(self._transition(
            job, "ARCHIVED", reason_code="INGEST_FAILED",
            extra_sets={"last_error_code": f"INGEST_FAILED:{reason}",
                        "last_error_at": self._now()},
            correlation_id=correlation_id))

    # -------------------------------------------------------------- 结算

    def settle_job(self, tenant_id: str, provider_job_id: str, *,
                   actual_micro: int | None = None,
                   billing_unit: str = "request",
                   basis: str = "PROVIDER_INVOICE",
                   correlation_id: str = "") -> dict:
        """§4 步骤 7：结算在 `PROVIDER_SUCCEEDED`/`PROVIDER_FAILED`/`CANCELED`
        **之后**、`ARCHIVED` **之前**。取消 ≠ 退款。

        `actual_micro = 0` 也要记一笔（`none` 计费档），不然"这次到底花没花钱"
        永远答不上来。
        """
        job = self._job(tenant_id, provider_job_id)
        terminal = ("PROVIDER_SUCCEEDED", "PROVIDER_FAILED", "CANCELED",
                    "INGESTING")
        if job["state"] not in terminal:
            raise ServiceError(
                409, "NOT_SETTLEABLE",
                f"job is {job['state']}; settlement follows a terminal outcome",
                details={"state": job["state"]})
        intent = self._intent_for_job(job)
        if intent is None:
            raise ServiceError(500, "INTENT_MISSING", "job has no intent")
        if intent["state"] in ("SETTLED",):
            return {"provider_job_id": provider_job_id, "state": "SETTLED",
                    "replayed": True}
        attempt = self._attempt(tenant_id, job["attempt_id"])
        requirement_set = _loads(attempt["requirement_set"]) or {}
        amount = int(intent["budget_amount_micro"] if actual_micro is None
                     else actual_micro)
        if actual_micro is not None and amount > int(intent["budget_amount_micro"]):
            # 报价上界被突破：不拦截（钱可能真花了），但必须让账本看见。
            # §12 的 `actual > quote_upper_bound` 上报就是这条。
            self._flag_quote_breach(job, intent, amount)
        settlement = self._host("settle_budget",
            tenant_id,
            {
                "command_id": f"{intent['intent_id']}:settle",
                "reservation_ref": intent["budget_reservation_id"],
                "terminal_state": ("PROVIDER_SUCCEEDED"
                                   if job["state"] in ("PROVIDER_SUCCEEDED",
                                                       "INGESTING")
                                   else "FAILED" if job["state"] == "PROVIDER_FAILED"
                                   else "CANCELED"),
                "final_accruals": [{
                    "billing_unit": billing_unit,
                    "basis": basis,
                    "billed_micro": amount,
                }],
                "currency": intent["budget_currency"],
                "attempt_id": job["attempt_id"],
                "provider_job_ref": job["provider_job_ref"],
            },
            correlation_id=correlation_id)
        self.conn.execute(
            "UPDATE submission_intent SET state = 'SETTLED', resolved_at = ?"
            " WHERE tenant_id = ? AND intent_id = ?",
            (self._now(), tenant_id, intent["intent_id"]))
        self._bump_ledger(tenant_id, attempt["node_id"], micro=amount)
        self._release_permit(job, intent, "SETTLED", correlation_id=correlation_id,
                             evidence=f"settlement:{settlement.get('settlement_id')}"
                                      f"/{intent['intent_id']}", fake_ok=True)
        return {"provider_job_id": provider_job_id, "state": "SETTLED",
                "settlement": settlement, "charged_micro": amount,
                "quote_upper_bound_micro": int(intent["budget_amount_micro"]),
                "quote_breached": amount > int(intent["budget_amount_micro"])}

    def _flag_quote_breach(self, job, intent, actual_micro: int) -> None:
        self.conn.execute(
            "UPDATE provider_job SET last_error_code = 'QUOTE_UPPER_BOUND_EXCEEDED',"
            " last_error_at = ? WHERE tenant_id = ? AND provider_job_id = ?",
            (self._now(), job["tenant_id"], job["provider_job_id"]))

    # -------------------------------------------------------------- 对账

    def reconcile_due(self, tenant_id: str, provider_job_id: str, *,
                      correlation_id: str = "") -> dict:
        """§7.3：走**一轮**对账。每轮一条 `reconciliation_attempt`。

        档位由 `provider_idempotency` 决定（`RECON_METHODS_BY_TIER`）。只有
        `found` 能推进状态；`not_found` 能不能自动重发，A/B/C 三档答案不同 ——
        这就是本方法存在的全部理由。
        """
        job = self._job(tenant_id, provider_job_id)
        if job["state"] not in ("SUBMISSION_UNKNOWN", "RECONCILING"):
            raise ServiceError(409, "NOT_RECONCILABLE",
                               f"job is {job['state']}, not UNKNOWN/RECONCILING",
                               details={"state": job["state"]})
        intent = self._intent_for_job(job)
        if intent is None:
            raise ServiceError(500, "INTENT_MISSING",
                               "unknown job has no submission_intent")
        round_no = int(self.conn.execute(
            "SELECT COALESCE(MAX(round_no), 0) + 1 AS n FROM reconciliation_attempt"
            " WHERE tenant_id = ? AND provider_job_id = ?",
            (tenant_id, provider_job_id)).fetchone()["n"])
        # T+7d 之后转人工（§7.3 梯子最后一格）。判据**只**来自
        # `recon_delay_seconds` —— 梯子有几格、什么时候到底，只在那里定义一次。
        if recon_delay_seconds(round_no) is None:
            return {"provider_job_id": provider_job_id, "state": job["state"],
                    "round_no": round_no, "outcome": "manual_queue"}

        if job["state"] == "SUBMISSION_UNKNOWN":
            job = self._transition(job, "RECONCILING",
                                   reason_code="RECONCILE_STARTED",
                                   correlation_id=correlation_id)

        tier = job["provider_idempotency"]
        methods = RECON_METHODS_BY_TIER.get(tier, ())
        probe = self._ports.get("reconcile_probe")
        results = []
        outcome = "inconclusive"
        for method in methods:
            if probe is None:
                results.append({"method": method, "outcome": "unsupported"})
                continue
            try:
                answer = probe(tenant_id, {
                    "method": method,
                    "provider_job_id": provider_job_id,
                    "stable_client_key": intent["stable_client_key"],
                    "channel_route": f"{job['provider']}/{job['channel']}",
                    "provider_job_ref": job["provider_job_ref"],
                    "since": intent["submitted_at"] or intent["created_at"],
                })
            except ServiceError as exc:
                if exc.retryable or exc.status >= 500:
                    answer = {"outcome": "error"}
                else:
                    raise
            result = str((answer or {}).get("outcome") or "inconclusive")
            results.append({"method": method, "outcome": result,
                            "evidence_ref": (answer or {}).get("evidence_ref"),
                            "provider_job_ref": (answer or {}).get("provider_job_ref")})
            if result == "found":
                outcome = "found"
                break
            if result in ("not_found", "inconclusive"):
                outcome = result if outcome != "not_found" else outcome
            if result == "unsupported":
                continue
        self._record_recon(job, round_no, results)
        if outcome == "found":
            return self._recon_found(job, intent, results,
                                     correlation_id=correlation_id)
        return self._recon_not_found(job, intent, round_no, outcome,
                                     correlation_id=correlation_id)

    def _record_recon(self, job, round_no: int, results: list[dict]) -> None:
        now = self._now()
        for item in results:
            self.conn.execute(
                "INSERT OR IGNORE INTO reconciliation_attempt (recon_id,"
                " provider_job_id, tenant_id, round_no, method, outcome,"
                " evidence_ref, attempted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self._new_id("rcn"), job["provider_job_id"], job["tenant_id"],
                 round_no, item["method"], item["outcome"],
                 item.get("evidence_ref"), now))

    def _recon_found(self, job, intent, results, *, correlation_id: str) -> dict:
        """对账找到 → 已收单。补写 `provider_job_ref`，进 `QUEUED`。"""
        ref = next((r["provider_job_ref"] for r in results if r.get("provider_job_ref")),
                   None)
        extra = {"next_action_at": _shift(
            self._now(), self._poll_interval_seconds())}
        if ref:
            # 必须和状态同一条 UPDATE：分两步写会先撞
            # `pj_unknown_has_no_ref`（RECONCILING 期间不许有 ref）。
            extra["provider_job_ref"] = ref
        updated = self._transition(
            job, "QUEUED", reason_code="RECONCILED_FOUND",
            extra_sets=extra, correlation_id=correlation_id)
        return {"provider_job_id": updated["provider_job_id"],
                "state": updated["state"], "outcome": "found",
                "provider_job_ref": ref}

    def _recon_not_found(self, job, intent, round_no: int, outcome: str, *,
                         correlation_id: str) -> dict:
        """`not_found` / `inconclusive`：**默认什么都不做**。

        A 档（`supported`）是唯一可以在 `2×p99` 之后自动同键重发的档位；
        B 档要 `resubmission_approval`；C 档要账单覆盖。这个差异是 §7.3 的核心，
        不能用一个"重试"糊过去。
        """
        tier = job["provider_idempotency"]
        elapsed = (_parse_ts(self._now())
                   - _parse_ts(intent["submitted_at"] or intent["created_at"])
                   ).total_seconds()
        if (outcome == "not_found" and tier == "supported"
                and elapsed >= 2.0 * self._p99_submit_seconds):
            verdict = self._resubmit_gate(job, intent, "RECONCILED_ABSENT",
                                          evidence=f"recon:round{round_no}")
            if verdict is None:
                return {"provider_job_id": job["provider_job_id"],
                        "state": job["state"], "outcome": outcome,
                        "resubmit_blocked": "SUBMIT_CAP_EXCEEDED"}
            return self._resubmit(job, intent, verdict,
                                  reason_code="RECONCILED_ABSENT",
                                  correlation_id=correlation_id)

        if outcome == "not_found" and tier == "client_lookup_only" \
                and elapsed >= max(3600.0, 3.0 * self._pid_list_lag_seconds()):
            approval = self.conn.execute(
                "SELECT * FROM resubmission_approval WHERE tenant_id = ?"
                " AND provider_job_id = ? ORDER BY created_at DESC LIMIT 1",
                (job["tenant_id"], job["provider_job_id"])).fetchone()
            if approval is None:
                # 有资格重提，但**没有批准**。停在这里等人工/策略。
                next_at = _shift(self._now(), 3600.0)
                self.conn.execute(
                    "UPDATE provider_job SET next_action_at = ? WHERE tenant_id = ?"
                    " AND provider_job_id = ?",
                    (next_at, job["tenant_id"], job["provider_job_id"]))
                return {"provider_job_id": job["provider_job_id"],
                        "state": job["state"], "outcome": outcome,
                        "resubmit_blocked": "APPROVAL_REQUIRED"}

        if outcome == "not_found" and tier == "unsupported":
            covered = self._ports.get("bill_covers_window")
            if covered is not None and covered(job["tenant_id"], {
                    "channel_route": f"{job['provider']}/{job['channel']}",
                    "since": intent["submitted_at"] or intent["created_at"]}):
                verdict = self._resubmit_gate(job, intent, "RECONCILED_ABSENT",
                                              evidence=f"bill:round{round_no}")
                if verdict is not None:
                    return self._resubmit(job, intent, verdict,
                                          reason_code="RECONCILED_ABSENT",
                                          correlation_id=correlation_id)

        # 什么都不满足：回 UNKNOWN，排下一轮。§7.2 六条铁律一条都不破。
        # 梯子走完（`recon_delay_seconds` 返回 None）就不再排期：留在
        # `SUBMISSION_UNKNOWN` 冻结，由人工队列处理（§7.5）。
        delay = recon_delay_seconds(round_no + 1)
        next_at = (None if delay is None
                   else _shift(self._now(), delay))
        back = self._transition(job, "SUBMISSION_UNKNOWN",
                                reason_code=f"RECON_INCONCLUSIVE_{outcome.upper()}",
                                extra_sets={"next_action_at": next_at},
                                correlation_id=correlation_id)
        return {"provider_job_id": back["provider_job_id"],
                "state": back["state"], "outcome": outcome,
                "round_no": round_no,
                "next_action_at": back["next_action_at"]}

    def _pid_list_lag_seconds(self) -> float:
        return float(self._ports.get("p99_task_list_lag_seconds") or 900.0)

    def _resubmit_gate(self, job, intent, reason_code: str, *, evidence: str):
        """§7.4 的五条。全过才写 `RESUBMIT_APPROVED`，否则返回 None。

        第 4 条（`submit_attempts` 上限）是**费用上限的实际执行点**：即使每次都
        能证明"上次没成功"，也不能无限重试（§7.4 末段）。
        """
        spent = int(self.conn.execute(
            "SELECT COALESCE(SUM(submit_attempts), 0) AS n FROM provider_job"
            " WHERE tenant_id = ? AND attempt_id = ?",
            (job["tenant_id"], job["attempt_id"])).fetchone()["n"])
        if spent >= self._default_max_submits:
            return None
        self.conn.execute(
            "UPDATE submission_intent SET state = 'RESUBMIT_APPROVED'"
            " WHERE tenant_id = ? AND intent_id = ?",
            (job["tenant_id"], intent["intent_id"]))
        return {"evidence": evidence, "reason_code": reason_code}

    def _resubmit(self, job, intent, verdict: dict, *, reason_code: str,
                  correlation_id: str) -> dict:
        """`RECONCILING → ADMITTED`，然后重走步骤 4（§7.4：预算与 permit 会再查）。"""
        back = self._transition(
            job, "ADMITTED", reason_code=reason_code,
            extra_sets={"provider_job_ref": None,
                        "last_error_code": None, "last_error_at": None},
            correlation_id=correlation_id)
        # §7.4 第 3 条：预算与 permit **重新走步骤 4 会再查一遍**。所以不直接
        # `_issue_grant`，而是复用步骤 4 本身 —— 那里含 S23 存活/金额、authorize、
        # S22 confirm、S24 能力四项实时复查，任一失败就补偿进拒绝态（§4）。
        attempt = self._attempt(job["tenant_id"], job["attempt_id"])
        reissued = self._saga_recheck_and_issue(
            back, attempt, self._intent_for_job(back),
            correlation_id=correlation_id)
        return {"provider_job_id": reissued["provider_job_id"],
                "state": reissued["state"], "outcome": "resubmitted",
                "evidence": verdict["evidence"]}

    # ---- 人工批准（B 档重提的唯一入口）

    def approve_resubmission(self, tenant_id: str, provider_job_id: str, *,
                             approved_by: str, reason: str,
                             evidence_ref: str, correlation_id: str = "") -> dict:
        """§7.3B：写一条 `resubmission_approval`。默认策略下这是唯一的人工出口。

        这里只**记录**批准；重提由下一轮 `reconcile_due` 执行 —— 因为 §7.4 的
        五条仍要逐条过，批准不能替代它们。
        """
        job = self._job(tenant_id, provider_job_id)
        if job["provider_idempotency"] != "client_lookup_only":
            raise ServiceError(
                409, "APPROVAL_NOT_APPLICABLE",
                "resubmission approval only applies to client_lookup_only channels",
                details={"provider_idempotency": job["provider_idempotency"]})
        if not (approved_by and reason and evidence_ref):
            raise ServiceError(422, "APPROVAL_INCOMPLETE",
                               "approved_by, reason and evidence_ref are required")
        if job["state"] not in ("SUBMISSION_UNKNOWN", "RECONCILING"):
            raise ServiceError(409, "NOT_RECONCILABLE",
                               f"job is {job['state']}")
        approval_id = self._new_id("rsa")
        self.conn.execute(
            "INSERT INTO resubmission_approval (approval_id, provider_job_id,"
            " tenant_id, reason, approved_by, evidence_ref, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (approval_id, provider_job_id, tenant_id, reason, approved_by,
             evidence_ref, self._now()))
        return {"approval_id": approval_id, "provider_job_id": provider_job_id}

    # -------------------------------------------------------------- 巡检

    def scan_due_jobs(self, tenant_id: str, *, limit: int = 100) -> dict:
        """§9.1 `due_jobs`（10s）：`next_action_at <= now()` 的 job。

        同一入口处理三种到期原因：轮询、对账、submit 计时器 —— 因为它们的
        驱动量都是 `next_action_at`，分开扫描反而会互相追尾。
        """
        now = self._now()
        rows = self.conn.execute(
            "SELECT * FROM provider_job WHERE tenant_id = ? AND next_action_at IS NOT"
            " NULL AND next_action_at <= ? AND state NOT IN"
            " ('ARCHIVED','PROVIDER_FAILED','CANCELED','REJECTED_BUDGET',"
            "  'REJECTED_QUOTA','ADMITTED','RESERVING','CREATED')"
            " ORDER BY next_action_at LIMIT ?", (tenant_id, now, limit)).fetchall()
        acted = []
        for row in rows:
            if row["state"] == "SUBMITTING":
                acted.append(self.expire_submit_timer(tenant_id,
                                                      row["provider_job_id"]))
            elif row["state"] in ("SUBMISSION_UNKNOWN", "RECONCILING"):
                acted.append(self.reconcile_due(tenant_id, row["provider_job_id"]))
            elif row["state"] == "DISPATCH_AUTHORIZED":
                # §13 第一行（429 + Retry-After）：退避期结束时 `next_action_at`
                # 到期，这里才签新 grant。没有活 grant 才补签 —— 有活 grant 说明
                # 只是还没被领取，不该再签一张（`dg_live_uniq` 也不允许）。
                if self._grant_of(row) is None:
                    acted.append(self._reissue_due_grant(tenant_id,
                                                         row["provider_job_id"]))
            elif row["state"] in ("QUEUED", "RUNNING"):
                acted.append(self.request_poll(tenant_id, row["provider_job_id"]))
        return {"scanned": len(rows), "acted": acted}

    def _reissue_due_grant(self, tenant_id: str, provider_job_id: str) -> dict:
        """退避到期的 `DISPATCH_AUTHORIZED` 重签一张 grant（不重走 Saga）。"""
        with self._tx():
            job = self._job(tenant_id, provider_job_id)
            if job["state"] != "DISPATCH_AUTHORIZED" or self._grant_of(job) is not None:
                return {"provider_job_id": provider_job_id, "state": job["state"],
                        "applied": False}
            attempt = self._attempt(tenant_id, job["attempt_id"])
            updated = self._issue_grant(
                job, attempt, self._intent_for_job(job),
                self._stashed(job).get("reservation") or {},
                correlation_id="")
        return {"provider_job_id": updated["provider_job_id"],
                "state": updated["state"], "applied": True,
                "next_action_at": updated["next_action_at"]}

    def request_poll(self, tenant_id: str, provider_job_id: str, *,
                     correlation_id: str = "") -> dict:
        """发一条 `connector.status.requested`，并推后 `next_action_at`。

        **只读**：轮询不产生费用，所以它计入 `network_retry` 而不是
        `submit_attempts`（PGJ-2：两个计数器不是一回事）。
        """
        job = self._job(tenant_id, provider_job_id)
        if job["state"] not in ("QUEUED", "RUNNING"):
            return {"provider_job_id": provider_job_id, "state": job["state"],
                    "applied": False}
        interval = self._poll_interval_seconds() * (
            4.0 if job["state"] == "RUNNING" else 1.0)
        with self._tx():
            self.conn.execute(
                "UPDATE provider_job SET network_retry = network_retry + 1,"
                " next_action_at = ?, updated_at = ? WHERE tenant_id = ?"
                " AND provider_job_id = ?",
                (_shift(self._now(), interval), self._now(),
                 tenant_id, provider_job_id))
            self.outbox.append(new_event(
                source=self.source, type="connector.status.requested",
                subject=f"provider-job/{provider_job_id}",
                tenant_id=tenant_id, home_cell=self.home_cell,
                aggregate_id=provider_job_id,
                sequence=int(job["aggregate_sequence"]),
                correlation_id=correlation_id,
                data={
                    "schema_version": "2.0",
                    "provider_job_id": provider_job_id,
                    "provider_job_ref": job["provider_job_ref"],
                    "channel_route": f"{job['provider']}/{job['channel']}",
                    "dispatch_id": (self._grant_of(job) or {})["dispatch_id"]
                    if self._grant_of(job) is not None else None,
                    "stable_client_key": (self._intent_for_job(job) or
                                          {})["stable_client_key"]
                    if self._intent_for_job(job) is not None else None,
                }))
        return {"provider_job_id": provider_job_id, "state": job["state"],
                "applied": True}

    def scan_stuck_submitting(self, tenant_id: str) -> dict:
        """§9.1 `stuck_submitting`（30s）：`SUBMITTING` 超 `2×p99` 无回执 → UNKNOWN。"""
        cutoff = _shift(self._now(), -2.0 * self._p99_submit_seconds)
        rows = self.conn.execute(
            "SELECT * FROM provider_job WHERE tenant_id = ? AND state = 'SUBMITTING'"
            " AND updated_at <= ?", (tenant_id, cutoff)).fetchall()
        out = [self.expire_submit_timer(tenant_id, r["provider_job_id"])
               for r in rows]
        return {"stuck": len(rows), "acted": out}

    def scan_expired_grants(self, tenant_id: str) -> dict:
        """§10.6：过期未 claim 的 grant → `REVOKED`，job 回 `ADMITTED` 重签。

        **前提是 `ISSUED`**：`CLAIMED` 之后绝不撤销（请求可能已经在路上），
        这正是 §10.6 与 §7.2 第 4 条的分界。
        """
        now = self._now()
        rows = self.conn.execute(
            "SELECT * FROM dispatch_grant WHERE tenant_id = ? AND state = 'ISSUED'"
            " AND expires_at <= ?", (tenant_id, now)).fetchall()
        out = []
        for grant in rows:
            with self._tx():
                job = self._job(tenant_id, grant["provider_job_id"])
                if job["state"] != "DISPATCH_AUTHORIZED":
                    # 状态已经不是"等领取"了：只把 grant 收掉，不动 job。
                    self.conn.execute(
                        "UPDATE dispatch_grant SET state = 'REVOKED'"
                        " WHERE tenant_id = ? AND dispatch_id = ? AND state = 'ISSUED'",
                        (tenant_id, grant["dispatch_id"]))
                    out.append({"dispatch_id": grant["dispatch_id"],
                                "action": "revoked_only"})
                    continue
                self.conn.execute(
                    "UPDATE dispatch_grant SET state = 'REVOKED'"
                    " WHERE tenant_id = ? AND dispatch_id = ? AND state = 'ISSUED'",
                    (tenant_id, grant["dispatch_id"]))
                back = self._transition(job, "ADMITTED",
                                        reason_code="GRANT_EXPIRED_UNCLAIMED")
                attempt = self._attempt(tenant_id, job["attempt_id"])
                self._issue_grant(
                    back, attempt, self._intent_for_job(back),
                    self._stashed(back).get("reservation") or {},
                    correlation_id="")
                # `_issue_grant` 在同一个事务里把新 dispatch_id 写回 intent，
                # 从那里读回来 —— job 行上没有这个字段，也不该有（§2.3 分层）。
                fresh = self._intent_for_job(back)
            out.append({"dispatch_id": grant["dispatch_id"],
                        "action": "reissued",
                        "new_dispatch_id": fresh["dispatch_id"] if fresh else None})
        return {"expired": len(rows), "acted": out}

    def scan_orphan_intents(self, tenant_id: str, *, minutes: int = 30) -> dict:
        """§9.1 `orphan_intents`：`RESERVED` 超 30min 未推进 → 只是告警。

        **不自动改**。卡住的 Saga 可能是"钱已经花了、结果还在飞"，自动释放
        就是 §7.2 第 2 条的违例。
        """
        cutoff = _shift(self._now(), -60.0 * minutes)
        rows = self.conn.execute(
            "SELECT * FROM submission_intent WHERE tenant_id = ? AND state = 'RESERVED'"
            " AND created_at <= ?", (tenant_id, cutoff)).fetchall()
        return {"orphans": [{"intent_id": r["intent_id"],
                             "provider_job_id": r["provider_job_id"],
                             "created_at": r["created_at"]} for r in rows],
                "count": len(rows),
                "action": "alert_only"}

    def scan_ingest_ttl_warning(self, tenant_id: str, *,
                                window_seconds: float = 900.0) -> dict:
        """§9.1 `ingest_ttl_warning`：URL 15min 内到期且未归档 → 告警。"""
        horizon = _parse_ts(_shift(self._now(), window_seconds))
        rows = self.conn.execute(
            "SELECT * FROM provider_job WHERE tenant_id = ? AND state = 'INGESTING'",
            (tenant_id,)).fetchall()
        warn = []
        for row in rows:
            snapshot = _loads(row["output_snapshot"] or "") or {}
            _url, expires, _hash = extract_artifact_source(snapshot.get("output") or {})
            if not expires:
                continue
            if _parse_ts(expires) <= horizon:
                warn.append({"provider_job_id": row["provider_job_id"],
                             "provider_url_expires_at": expires})
        return {"at_risk": warn, "count": len(warn), "action": "alert_and_prioritize"}

    def scan_reservation_leak(self, tenant_id: str) -> dict:
        """§9.1 `reservation_leak_check`（1h）：与 S23 对账。

        **只告警，永不自动修**（方案原文："告警，不自动改"）。理由：错的可能是
        我们对，也可能是 S23 对；自动"修"一边就是让某个服务变成第二权威。
        """
        read = self._ports.get("read_reservation")
        if read is None:
            return {"checked": 0, "mismatches": [], "action": "unavailable"}
        rows = self.conn.execute(
            "SELECT * FROM submission_intent WHERE tenant_id = ? AND state IN"
            " ('CLAIMED','SUBMITTED','RESUBMIT_APPROVED')", (tenant_id,)).fetchall()
        mismatches = []
        for intent in rows:
            try:
                live = self._host("read_reservation", tenant_id,
                                  intent["budget_reservation_id"])
            except ServiceError as exc:
                mismatches.append({"intent_id": intent["intent_id"],
                                   "error": exc.code})
                continue
            if live and live.get("state") in ("RELEASED", "EXPIRED"):
                mismatches.append({"intent_id": intent["intent_id"],
                                   "s26_holds": True,
                                   "s23_state": live["state"]})
        return {"checked": len(rows), "mismatches": mismatches,
                "action": "alert_only"}

    def scan_unknown_submissions(self, tenant_id: str, *,
                                 limit: int = 50) -> dict:
        """§9.1 `unknown_submissions`（5min）：按 §7.3 排下一轮。

        与 `scan_due_jobs` 分开是因为周期不同：这条 5min 一次，且要算
        「超 7 天转人工」—— 那是 `reconcile_due` 里的 `manual_queue` 分支。
        """
        horizon = _shift(self._now(), -RECON_HORIZON_SECONDS)
        rows = self.conn.execute(
            "SELECT * FROM provider_job WHERE tenant_id = ? AND state IN"
            " ('SUBMISSION_UNKNOWN','RECONCILING') AND updated_at > ?"
            " ORDER BY next_action_at LIMIT ?",
            (tenant_id, horizon, limit)).fetchall()
        acted = []
        manual = []
        for row in rows:
            if row["next_action_at"] and _parse_ts(row["next_action_at"]) > \
                    _parse_ts(self._now()):
                continue
            answer = self.reconcile_due(tenant_id, row["provider_job_id"])
            if answer.get("outcome") == "manual_queue":
                manual.append(row["provider_job_id"])
            else:
                acted.append(answer)
        return {"scanned": len(rows), "acted": acted, "manual_queue": manual}

    def scan_ingest_backlog(self, tenant_id: str, *,
                            threshold_seconds: float = 600.0) -> dict:
        """§9.1 `ingest_backlog`（1min）：`INGESTING` 停留超阈值 → 告警。

        **不动状态**：归档慢是 S28 的问题，S26 若在这里改状态就会和
        `artifact.ingested` 打架（§8.2 的只重归档）。
        """
        cutoff = _shift(self._now(), -threshold_seconds)
        rows = self.conn.execute(
            "SELECT * FROM provider_job WHERE tenant_id = ? AND state = 'INGESTING'"
            " AND updated_at <= ?", (tenant_id, cutoff)).fetchall()
        return {"backlog": [{"provider_job_id": r["provider_job_id"],
                             "updated_at": r["updated_at"]} for r in rows],
                "count": len(rows), "action": "alert_only"}

    # ------------------------------------------------------ 栅栏与取消

    def on_execution_fence_bumped(self, tenant_id: str, *, run_id: str,
                                  branch_id: str, execution_fence: int,
                                  correlation_id: str = "") -> dict:
        """§9.2：run/branch 级栅栏提升（run 取消或切主）。

        只有这一种场景会**主动**撤销 `ISSUED` grant 并把未领取的
        `DISPATCH_AUTHORIZED` 退回 `ADMITTED` —— 因为 claim 之前字节没出去。
        已领取的进 `CANCEL_REQUESTED`（`last_error_code`，不是状态，C26-2）。
        """
        rows = self.conn.execute(
            "SELECT pj.* FROM provider_job pj"
            " JOIN generation_attempt ga ON ga.tenant_id = pj.tenant_id"
            "  AND ga.attempt_id = pj.attempt_id"
            " WHERE pj.tenant_id = ? AND ga.branch_id = ?"
            "  AND ga.execution_fence < ? AND pj.state NOT IN"
            " ('ARCHIVED','PROVIDER_FAILED','CANCELED','REJECTED_BUDGET',"
            "  'REJECTED_QUOTA')", (tenant_id, branch_id, int(execution_fence)),
        ).fetchall()
        out = []
        for job in rows:
            if job["state"] == "DISPATCH_AUTHORIZED":
                with self._tx():
                    self.conn.execute(
                        "UPDATE dispatch_grant SET state = 'REVOKED'"
                        " WHERE tenant_id = ? AND provider_job_id = ?"
                        " AND state = 'ISSUED'",
                        (tenant_id, job["provider_job_id"]))
                    self._transition(job, "ADMITTED",
                                     reason_code="FENCE_SUPERSEDED",
                                     extra_sets={"last_error_code":
                                                 "FENCE_SUPERSEDED",
                                                 "last_error_at": self._now()},
                                     correlation_id=correlation_id)
                out.append({"provider_job_id": job["provider_job_id"],
                            "action": "returned_to_admitted"})
            elif job["state"] == "SUBMITTING":
                # 字节可能已经在路上 → **不能重提**，只能进对账。
                self._transition(job, "SUBMISSION_UNKNOWN",
                                 reason_code="FENCE_SUPERSEDED_DURING_SUBMIT",
                                 extra_sets={"last_error_code":
                                             "FENCE_SUPERSEDED",
                                             "next_action_at": self._now()},
                                 correlation_id=correlation_id)
                out.append({"provider_job_id": job["provider_job_id"],
                            "action": "forced_reconcile"})
            elif job["state"] in ("QUEUED", "RUNNING"):
                self.conn.execute(
                    "UPDATE provider_job SET last_error_code = 'CANCEL_REQUESTED',"
                    " last_error_at = ?, updated_at = ? WHERE tenant_id = ?"
                    " AND provider_job_id = ?",
                    (self._now(), self._now(), tenant_id,
                     job["provider_job_id"]))
                out.append({"provider_job_id": job["provider_job_id"],
                            "action": "cancel_requested"})
        return {"superseded": len(rows), "acted": out}

    def on_node_epoch_bumped(self, tenant_id: str, *, node_id: str,
                             node_epoch: int,
                             correlation_id: str = "") -> dict:
        """§9.2：node_epoch 是**单节点**级。只碰这个节点，其他节点不动。

        旧 epoch 的 job 一刀切到 `PROVIDER_FAILED`（不再推进接受指针），但
        `SUBMITTING`/`UNKNOWN` 仍要先对账 —— 钱可能已经花了。
        """
        rows = self.conn.execute(
            "SELECT pj.* FROM provider_job pj"
            " JOIN generation_attempt ga ON ga.tenant_id = pj.tenant_id"
            "  AND ga.attempt_id = pj.attempt_id"
            " WHERE pj.tenant_id = ? AND ga.node_id = ? AND ga.node_epoch < ?"
            "  AND pj.state NOT IN ('ARCHIVED','PROVIDER_FAILED','CANCELED',"
            "   'REJECTED_BUDGET','REJECTED_QUOTA')",
            (tenant_id, node_id, int(node_epoch))).fetchall()
        out = []
        for job in rows:
            if job["state"] in ("SUBMITTING", "SUBMISSION_UNKNOWN", "RECONCILING"):
                out.append({"provider_job_id": job["provider_job_id"],
                            "action": "left_for_reconcile"})
                continue
            if job["state"] == "DISPATCH_AUTHORIZED":
                self.conn.execute(
                    "UPDATE dispatch_grant SET state = 'REVOKED'"
                    " WHERE tenant_id = ? AND provider_job_id = ?"
                    " AND state = 'ISSUED'",
                    (tenant_id, job["provider_job_id"]))
            if job["state"] in ("DISPATCH_AUTHORIZED", "ADMITTED", "QUEUED",
                                "RUNNING"):
                self._transition(job, "PROVIDER_FAILED",
                                 reason_code="NODE_EPOCH_SUPERSEDED",
                                 extra_sets={"last_error_code":
                                             "NODE_EPOCH_SUPERSEDED",
                                             "last_error_at": self._now()},
                                 correlation_id=correlation_id)
                out.append({"provider_job_id": job["provider_job_id"],
                            "action": "superseded"})
        return {"superseded": len(rows), "acted": out}

    def cancel_job(self, tenant_id: str, provider_job_id: str, *,
                   discard_on_completion: bool = False,
                   correlation_id: str = "") -> dict:
        """§9.2 / §13 的取消失败分支。**取消不等于退款。**

        返回值三态（S21 的契约）：`cancelled` / `not_cancellable` / `unknown`。
        - 未派单（`ADMITTED`/`DISPATCH_AUTHORIZED`）：撤销 grant、释放预算与
          permit、进 `CANCELED`。这三步都是本地/幂等调用，安全。
        - 已派单但没观测到终态：只能问供应商。问不到 → `unknown`，**保持原状态**，
          因为盲目置 `CANCELED` 会让一笔真实消费失去归属。
        """
        job = self._job(tenant_id, provider_job_id)
        state = job["state"]
        if state in ("ARCHIVED", "CANCELED"):
            return {"provider_job_id": provider_job_id, "state": state,
                    "result": "cancelled", "already_terminal": True}
        if state in ("PROVIDER_FAILED", "REJECTED_BUDGET", "REJECTED_QUOTA"):
            return {"provider_job_id": provider_job_id, "state": state,
                    "result": "cancelled", "already_terminal": True}
        if state in ("CREATED", "RESERVING", "ADMITTED", "DISPATCH_AUTHORIZED"):
            with self._tx():
                self.conn.execute(
                    "UPDATE dispatch_grant SET state = 'REVOKED'"
                    " WHERE tenant_id = ? AND provider_job_id = ?"
                    " AND state = 'ISSUED'", (tenant_id, provider_job_id))
                intent = self._intent_for_job(job)
                if intent is not None:
                    # 理由要按 permit 的真实状态选，不能一律给 `NEVER_DISPATCHED`：
                    # S22 只允许从 `RESERVED` 用它，而 grant 已签发时 permit 是
                    # `DISPATCH_AUTHORIZED`，那一格只收
                    # `SUPERSEDED_BY_BUDGET / SUPERSEDED_BY_FENCE / OPERATOR`。
                    # 取消发生在 claim 之前，所以 `SUPERSEDED_BY_FENCE` 最贴切
                    # 地表达了"这次派单作废了"，且三个状态都收（C26-23）。
                    permit_reason = ("NEVER_DISPATCHED"
                                     if intent["quota_state"] == "RESERVED"
                                     else "SUPERSEDED_BY_FENCE")
                    self._release_permit(job, intent, permit_reason,
                                         correlation_id=correlation_id,
                                         fake_ok=True)
                    self._release_reservation(
                        job, intent["budget_reservation_id"], "NEVER_DISPATCHED",
                        evidence=f"cancel:{provider_job_id}",
                        correlation_id=correlation_id)
                    self.conn.execute(
                        "UPDATE submission_intent SET state = 'ABANDONED'"
                        " WHERE tenant_id = ? AND intent_id = ?",
                        (tenant_id, intent["intent_id"]))
                self._transition(job, "CANCELED", reason_code="CANCELED_BEFORE_DISPATCH",
                                 correlation_id=correlation_id)
            return {"provider_job_id": provider_job_id, "state": "CANCELED",
                    "result": "cancelled", "already_terminal": False}

        # 已派单。能不能取消取决于供应商能力（S27 的回执）。
        cancel_port = self._ports.get("request_provider_cancel")
        if cancel_port is None:
            return {"provider_job_id": provider_job_id, "state": state,
                    "result": "unknown",
                    "reason": "cancel_capability_unavailable"}
        try:
            answer = cancel_port(tenant_id, {
                "provider_job_id": provider_job_id,
                "provider_job_ref": job["provider_job_ref"],
                "channel_route": f"{job['provider']}/{job['channel']}",
            })
        except ServiceError as exc:
            if exc.retryable or exc.status >= 500:
                return {"provider_job_id": provider_job_id, "state": state,
                        "result": "unknown", "reason": exc.code}
            raise
        verdict = str((answer or {}).get("result") or "unknown")
        if verdict == "cancelled":
            self._transition(job, "CANCELED", reason_code="CANCELED_BY_REQUEST",
                             correlation_id=correlation_id)
            return {"provider_job_id": provider_job_id, "state": "CANCELED",
                    "result": "cancelled"}
        if verdict == "not_cancellable":
            # §9.2：不可取消就让它跑完，成片归档但不进候选；费用照结。
            self.conn.execute(
                "UPDATE provider_job SET disposition = 'DISCARD_ON_COMPLETION',"
                " last_error_code = 'CANCEL_REQUESTED', last_error_at = ?,"
                " updated_at = ? WHERE tenant_id = ? AND provider_job_id = ?",
                (self._now(), self._now(), tenant_id, provider_job_id))
            return {"provider_job_id": provider_job_id, "state": state,
                    "result": "not_cancellable",
                    "disposition": "DISCARD_ON_COMPLETION"}
        return {"provider_job_id": provider_job_id, "state": state,
                "result": "unknown"}

    def recover_after_failover(self, tenant_id: str) -> dict:
        """§9.3 灾备切主：**所有 `SUBMITTING` 一律进 `RECONCILING`，不重提。**

        `docs/14 §5` 原文："仅有新 region 锁不证明旧请求未发生"。所以恢复时
        唯一正确的动作是把这批 job 全部转进对账，而不是"重来一遍"。
        """
        rows = self.conn.execute(
            "SELECT * FROM provider_job WHERE tenant_id = ? AND state = 'SUBMITTING'",
            (tenant_id,)).fetchall()
        out = []
        for job in rows:
            updated = self._transition(
                job, "SUBMISSION_UNKNOWN", reason_code="FAILOVER_RECOVERY",
                extra_sets={"last_error_code": "FAILOVER_RECOVERY",
                            "next_action_at": self._now()})
            self._transition(updated, "RECONCILING",
                             reason_code="FAILOVER_RECOVERY")
            out.append(job["provider_job_id"])
        return {"recovered": len(out), "provider_job_ids": out}
