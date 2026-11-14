"""S44 内核。方案：`plans/S44-evaluation-analytics.md`。

一句话边界：**S44 是读模型 + 评测运行器，不是账本。**
它读 S23 的费用事实、S35–S40 的质量/人工事实、S26 的任务事实、S08 的接受
事实与 S43 的交付事实，按固定 allocation policy 做经营投影，跑评测、盲评
与实验，给出带样本量、范围、版本的统计。

## 不写的东西（§1 末表）

| 事实 | 权威 | S44 的做法 |
|---|---|---|
| 原始费用条目 / 余额 / 预留 | S23 | 只读；本地只存分摊结果 |
| 任务状态 / 尝试 | S26 | 只读 |
| QC 分数 / 缺陷 | S35–S38 | 只读；`decision` 原样搬运，`abstain` 不是 pass |
| 人工决定 | S40 | 只读 |
| 接受指针 / manifest | S08 | 只读 |
| 交付记录 | S43 | 只读 |
| 合同收入 | 未定（§11 C1） | 只作显式外部输入 |
| 审计链 | S45 | 不涉及 |
| 模型路由 | S25 | 只提供快照；不做禁用 |

## 四条结构性纪律的代码形态

1. **不发明数字。** 上游不可达 → `503 DEPENDENCY_UNAVAILABLE`，不是 0，
   也不是上一次的缓存。`n = 0` 时 `value` 是 `None`（DDL CHECK 与事件
   `allOf` 同时约束）。这是本服务最重要的性质（§0、§2.4）。
2. **分摊是整数 micro 的守恒。** `sum(allocations) == original_amount`，
   最后一项吸收余数；不守恒就 `ALLOCATION_INVARIANT_VIOLATION`，拒绝生成
   快照（§4.2、§4.6）。
3. **快照不可变。** 没有 UPDATE 路径；重算 = 新快照。天然键
   `tenant + cohort + metric_key + version + watermarks_hash + policy`。
4. **`execution_fence` 与 `node_epoch` 不参与任何计算。** 它们只在签名过的
   execution context 里被校验形状（`deps.validate_execution_context`），
   本服务不存、不比较、不换算；本方案也不需要它们做坐标。

## 账本边界

删掉本库的全部数据，S23 仍然完整、仍然能出账单；反过来删掉 S23，本库的
金额全部失去依据。任何会「扣钱或放行」的数都不属于本服务。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from event_sdk import Inbox, Outbox, new_event
from idempotency_sdk import canonical_request_hash

from .schema import (
    ALLOCATION_BASES,
    ALLOCATION_DIMENSIONS,
    BLIND_REVIEW_STATES,
    CI_METHODS,
    CONSUMERS,
    COST_TYPES,
    DATASET_PURPOSES,
    DIFFICULTIES,
    EVENT_EVALUATION,
    EVENT_JOB_STATE,
    EVENT_LEDGER,
    EVENT_PACKAGE_PUBLISHED,
    EVENT_PROJECTION,
    EVENT_REVIEW_DECISION,
    EVENT_SNAPSHOT,
    EVENT_SNAPSHOT_COMMITTED,
    EVENT_SUBTITLE_READY,
    EVENT_TECHNICAL_QC,
    EVENT_ALIGNMENT_QC,
    EVENT_SEMANTIC_QC,
    EVALUATION_STATES,
    EXPERIMENT_STATES,
    ID_PREFIX,
    METRIC_DEFINITIONS,
    METRIC_KEYS,
    METRIC_VERSION,
    PROJECTION_WINDOWS,
    SCHEMA,
    SCHEMA_VERSION,
    SEQUENCE_ONE,
    SOURCE,
    TERMINAL_EVALUATION_STATES,
    TOPIC,
    VOTE_DIMENSIONS,
)

VOLATILE_CONTEXT_KEYS = ("trace_id", "traceparent", "timestamp",
                         "requested_at", "issued_at", "server_time")

LOW_CONFIDENCE_N = 30          # 成本/耗时类（§2.4）
BLIND_LOW_CONFIDENCE_N = 5     # 盲评维度（§4.3）
LOW_ALPHA = 0.667              # Krippendorff alpha 的经验下限（§4.6）
MAX_WAIT_SECONDS = 60          # watermark 等待上限（§4.6）


# ------------------------------------------------------------------ 工具

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _fmt(_now_dt())


def _fmt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def stable_request_hash(body: dict) -> str:
    """命令幂等摘要：业务字段全进，trace 与时间戳不进。

    与 S43 / S37 / S41 同一份易变键清单。重投时 trace_id 变了不该被当成
    「同键不同 body」（AGENTS.md 要求 409 只给真正的不同 body）。
    """
    if not isinstance(body, dict):
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    candidate = dict(body)
    for key in ("context", "execution_context"):
        context = candidate.get(key)
        if isinstance(context, dict):
            candidate[key] = {k: v for k, v in context.items()
                              if k not in VOLATILE_CONTEXT_KEYS}
    return canonical_request_hash(candidate)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交；已在事务里时不再开第二个。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.owned = False

    def __enter__(self):
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
            self.owned = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.owned:
            self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# 未配置的上游是必然 503 的桩，不是 `None`：少写一个 `if source is None`
# 就会得到「没查 = 通过」。
def _missing_source(code: str, why: str):
    def _stub(*_args, **_kwargs):
        raise ServiceError(503, code, why)
    _stub._unavailable_code = code
    return _stub


# ------------------------------------------------------------- 统计工具

def percentile(sorted_values: list[float], q: float) -> float | None:
    """线性插值分位数（numpy 默认口径）。调用方保证列表已排序且非空。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[int(position)])
    weight = position - lower
    return float(sorted_values[lower] * (1 - weight)
                 + sorted_values[upper] * weight)


def distribution(values: list[float]) -> dict:
    """p50/p90/p95/p99/max。空样本给空对象，**不**给 0（§2.4）。"""
    if not values:
        return {}
    ordered = sorted(float(value) for value in values)
    return {
        "p50": percentile(ordered, 0.50),
        "p90": percentile(ordered, 0.90),
        "p95": percentile(ordered, 0.95),
        "p99": percentile(ordered, 0.99),
        "max": ordered[-1],
    }


def wilson_interval(successes: int, total: int, z: float = 1.96) -> dict | None:
    """比率型指标的 Wilson 区间（§2.5：小样本不用正态近似）。"""
    if total <= 0:
        return None
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = (z * math.sqrt(phat * (1 - phat) / total
                            + z * z / (4 * total * total)) / denominator)
    return {"low": max(0.0, centre - margin), "high": min(1.0, centre + margin)}


EXACT_RESAMPLE_LIMIT = 50_000   # n**n 以内可穷举，见 bootstrap_interval


def bootstrap_interval(values: list[float], *, seed_material: str,
                       iterations: int = 400) -> dict | None:
    """确定性 bootstrap percentile CI（§2.5：n<30 必须用 bootstrap）。

    随机源由 `seed_material` 派生：同 cohort 同 watermark 重算必须逐字段
    相同（E38-S44-03 / §4.6 最后一行）。用系统随机数会让每一次重算都报
    `nondeterministic_snapshot`。

    n 很小的时候 n**n 个重采样均值可以整个枚举出来（n=3 只有 27 个），
    这时走精确路径：2.5/97.5 分位是确定的数学量，与种子无关。抽样路径
    在 n=3 时根本分辨不出尾部——P(三次都抽到最大值) = 1/27 ≈ 3.7%，而
    2.5% 分位只留 10 个样本位，于是 97.5 分位会随种子在 `max` 与次大值
    之间摇摆，同一个 cohort 换个日期（日期进种子）就换一个区间。这不是
    「重算不确定」，是抽样次数不够描绘尾部；能穷举就别抽。
    """
    if not values:
        return None
    if len(values) == 1:
        value = float(values[0])
        return {"low": value, "high": value}
    samples = [float(value) for value in values]
    size = len(samples)
    if size ** size <= EXACT_RESAMPLE_LIMIT:
        means = sorted(sum(combo) / size for combo in
                       itertools.product(samples, repeat=size))
        return {"low": percentile(means, 0.025), "high": percentile(means, 0.975)}
    state = int(_sha256_hex(seed_material)[:16], 16) or 1
    means = []
    for _ in range(iterations):
        total = 0.0
        for _ in range(size):
            # xorshift64*：确定、够均匀、不依赖 random 的全局状态
            state ^= (state >> 12) & 0xFFFFFFFFFFFFFFFF
            state ^= (state << 25) & 0xFFFFFFFFFFFFFFFF
            state ^= (state >> 27) & 0xFFFFFFFFFFFFFFFF
            index = ((state * 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF) % size
            total += samples[index]
        means.append(total / size)
    means.sort()
    return {"low": percentile(means, 0.025), "high": percentile(means, 0.975)}


def krippendorff_alpha(ratings: list[list[float]]) -> float | None:
    """区间尺度的 Krippendorff's alpha（§4.3 第 2 条）。

    `ratings` 是「每个单元一行、每行是该单元收到的分值」。单元内至少 2 个
    评分才参与。返回 None 表示无法计算（单元数或评分对数不足），调用方
    标 `insufficient_data` 而不给均值。
    """
    units = [row for row in ratings if len(row) >= 2]
    if len(units) < 2:
        return None
    values = [value for row in ratings for value in row]
    if len(values) < 3:
        return None
    grand = sum(values) / len(values)
    observed_sum = 0.0
    observed_pairs = 0
    for row in units:
        count = len(row)
        for i in range(count):
            for j in range(count):
                if i == j:
                    continue
                observed_sum += (row[i] - row[j]) ** 2
        observed_pairs += count * (count - 1)
    if observed_pairs == 0:
        return None
    observed = observed_sum / observed_pairs
    expected = sum((value - grand) ** 2 for value in values) / len(values)
    if expected == 0:
        # 全体同分：完全一致，但区间口径下 Do 也是 0。给 1.0 会让「一个人
        # 给所有条目打 3 分」看起来像完美标注，所以给 None。
        return None
    return 1 - observed / expected


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _round(value, digits: int = 6):
    if value is None:
        return None
    return round(float(value), digits)


# ------------------------------------------------------------- 整数分摊

def split_micro(total: int, weights: list[float]) -> list[int]:
    """按权重把整数 micro 拆开，**最后一项吸收舍入余数**（§4.2）。

    返回的长度与 `weights` 相同，且 `sum(result) == total` 恒成立。用浮点
    分摊会在上万笔后产生分币漂移，客户对账时无法解释。
    """
    if not isinstance(total, int):
        raise ServiceError(500, "ALLOCATION_INVARIANT_VIOLATION",
                           "amount_micro must be an integer")
    if not weights:
        raise ServiceError(500, "ALLOCATION_INVARIANT_VIOLATION",
                           "cannot allocate to an empty target set")
    positive = [max(0.0, float(weight)) for weight in weights]
    weight_sum = sum(positive)
    if weight_sum <= 0:
        positive = [1.0] * len(weights)
        weight_sum = float(len(weights))
    parts: list[int] = []
    remaining = total
    for index, weight in enumerate(positive[:-1]):
        share = int(math.floor(total * weight / weight_sum))
        parts.append(share)
        remaining -= share
    parts.append(remaining)
    if sum(parts) != total:  # 防御：整数运算不该走到这里
        raise ServiceError(500, "ALLOCATION_INVARIANT_VIOLATION",
                           "integer split did not conserve the amount")
    return parts


def _attribution(attribution: dict, dimension: str):
    """`dimension` 的闭集来自 §5 的 DDL（`episode`），而 §4.2 的规则示例写
    `episode_id`/`shot_id`。两种拼法都要认，否则同一条规则在 DDL 与示例
    之间会「匹配上了却没有目标」。"""
    if not isinstance(attribution, dict):
        return None
    for key in (dimension, dimension + "_id"):
        if attribution.get(key) is not None:
            return attribution.get(key)
    if dimension.endswith("_id"):
        return attribution.get(dimension[:-3])
    return None


def _match_rule(rule_match: dict, entry: dict) -> bool:
    if not isinstance(rule_match, dict):
        return False
    attribution = entry.get("attribution") or {}
    for key, expected in rule_match.items():
        actual = entry.get(key)
        if actual is None:
            actual = attribution.get(key)
        if expected == "*":
            if actual is None:
                return False
            continue
        if actual != expected:
            return False
    return True


class AllocationEngine:
    """固定、版本化的分摊规则（§4.2）。没有「猜一个维度」的兜底路径。

    `rules` 按顺序匹配；第一条命中的规则生效。没有规则命中 → 记
    `unallocated_micro`，**不静默丢弃**（§4.6）。
    """

    def __init__(self, policy_version: str, rules: list[dict]):
        self.policy_version = policy_version
        self.rules = [rule for rule in rules if isinstance(rule, dict)]

    def allocate(self, entry: dict, *, targets: dict | None = None) -> dict:
        """`targets` 给 `by_episode_share` / `by_usable_minutes` 的权重表。"""
        amount = entry.get("amount_micro")
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise ServiceError(
                422, "INVALID_COST_ENTRY",
                "cost entry is missing an integer amount_micro",
                details={"cost_entry_id": entry.get("cost_entry_id")})
        attribution = entry.get("attribution") or {}
        for rule in self.rules:
            if not _match_rule(rule.get("match") or {}, entry):
                continue
            basis = rule.get("basis")
            dimension = rule.get("dimension")
            if basis not in ALLOCATION_BASES:
                # C-S44-03：旧版本不认识新 basis → 拒绝，不静默归零。
                raise ServiceError(
                    409, "ALLOCATION_BASIS_UNSUPPORTED",
                    f"allocation basis {basis!r} is not in the supported set",
                    details={"basis": basis,
                             "supported": list(ALLOCATION_BASES)})
            if dimension not in ALLOCATION_DIMENSIONS:
                raise ServiceError(
                    422, "ALLOCATION_DIMENSION_UNSUPPORTED",
                    f"dimension {dimension!r} is not in the closed set",
                    details={"dimension": dimension})
            rows = self._rows(entry, attribution, basis, dimension, amount,
                              targets or {})
            return {"policy_version": self.policy_version, "basis": basis,
                    "dimension": dimension, "allocations": rows,
                    "unallocated_micro": 0}
        return {"policy_version": self.policy_version, "basis": "unmatched",
                "dimension": None, "allocations": [],
                "unallocated_micro": amount}

    def _rows(self, entry: dict, attribution: dict, basis: str,
              dimension: str, amount: int, targets: dict) -> list[dict]:
        if basis in ("direct", "estimated_by_policy", "excluded"):
            value = _attribution(attribution, dimension)
            if value is None and basis == "excluded" and dimension == "tenant":
                value = entry.get("tenant_id")
            if value is None:
                raise ServiceError(
                    500, "ALLOCATION_INVARIANT_VIOLATION",
                    f"basis {basis!r} needs attribution.{dimension}",
                    details={"cost_entry_id": entry.get("cost_entry_id")})
            return [{"dimension": dimension, "dimension_value": str(value),
                     "amount_micro": amount, "basis": basis}]

        if basis == "to_parent_episode":
            episode = attribution.get("episode_id")
            if episode is None:
                raise ServiceError(
                    500, "ALLOCATION_INVARIANT_VIOLATION",
                    "to_parent_episode needs attribution.episode_id",
                    details={"cost_entry_id": entry.get("cost_entry_id")})
            return [{"dimension": "episode", "dimension_value": str(episode),
                     "amount_micro": amount, "basis": basis}]

        # by_episode_share / by_usable_minutes：权重来自条目或 cohort 目标表。
        weights = attribution.get("shares") or targets.get(basis) or {}
        if not isinstance(weights, dict) or not weights:
            raise ServiceError(
                500, "ALLOCATION_INVARIANT_VIOLATION",
                f"basis {basis!r} has no weight table; refusing to guess",
                details={"cost_entry_id": entry.get("cost_entry_id")})
        keys = sorted(weights)
        parts = split_micro(amount, [float(weights[key]) for key in keys])
        return [{"dimension": dimension, "dimension_value": str(key),
                 "amount_micro": part, "basis": basis}
                for key, part in zip(keys, parts)]

    @staticmethod
    def verify(entry_amounts: dict[str, int], allocations: list[dict]) -> None:
        """`sum(allocations of entry) == original_amount`（§2.1 规则 1）。"""
        totals: dict[str, int] = {}
        for row in allocations:
            key = row["cost_entry_id"]
            totals[key] = totals.get(key, 0) + row["amount_micro"]
        for entry_id, amount in entry_amounts.items():
            if totals.get(entry_id, 0) != amount:
                raise ServiceError(
                    500, "ALLOCATION_INVARIANT_VIOLATION",
                    f"allocation of {entry_id!r} does not conserve "
                    f"{amount} micro",
                    details={"cost_entry_id": entry_id,
                             "original_micro": amount,
                             "allocated_micro": totals.get(entry_id, 0)})


# ------------------------------------------------------------ 内核主体

class EvaluationAnalyticsService:
    """S44 的写路径。

    构造顺序不能改：`executescript(SCHEMA)` 与 `Outbox.__init__` 都含
    `executescript`，而它会隐式提交。两者必须在任何 `BEGIN IMMEDIATE` 之前
    建一次，否则命令事务会被悄悄提交，ROLLBACK 失效。
    """

    def __init__(self, conn: sqlite3.Connection, *,
                 cost_source=None, qc_source=None, review_source=None,
                 delivery_source=None, job_source=None, accepted_source=None,
                 evaluator_source=None,
                 evaluation_execution_enabled: bool = False):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)

        self.cost_source = cost_source or _missing_source(
            "COST_SOURCE_UNAVAILABLE",
            "S23 is the only cost authority; S44 will not report a cost KPI "
            "without its raw entries (plan §0)")
        self.qc_source = qc_source or _missing_source(
            "QC_SOURCE_UNAVAILABLE",
            "S44 will not compute a quality KPI without QC reports "
            "(plan §4.1 step 3)")
        self.review_source = review_source or _missing_source(
            "REVIEW_SOURCE_UNAVAILABLE",
            "S44 will not compute intervention/memory KPIs without S40 "
            "(plan §4.1 step 3)")
        self.delivery_source = delivery_source or _missing_source(
            "DELIVERY_SOURCE_UNAVAILABLE",
            "S44 will not compute delivery lead time without S43 "
            "(plan §4.1 step 3)")
        self.job_source = job_source or _missing_source(
            "JOB_SOURCE_UNAVAILABLE",
            "S44 will not compute a shot/node KPI without S26 "
            "(plan §4.1 step 3)")
        self.accepted_source = accepted_source or _missing_source(
            "ACCEPTED_SOURCE_UNAVAILABLE",
            "S44 will not claim an episode is usable without S08 "
            "(plan §2.3 K1)")
        self.evaluator_source = evaluator_source or _missing_source(
            "EVALUATOR_UNAVAILABLE",
            "evaluation execution must go through S21->S26 (plan §1)")

        # 评测执行的平台级开关，默认关闭。未打开时命令 503 —— 本服务不
        # 自行发起付费生成（AGENTS.md 的停止条件）。
        self.evaluation_execution_enabled = bool(evaluation_execution_enabled)
        self._seed_metric_definitions()
        self.metrics: dict[str, int] = {}

    # ---------------------------------------------------------- 基础设施
    def _bump(self, name: str, amount: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def unavailable_dependencies(self) -> list[str]:
        """还没配的上游（按注入的桩判定）。

        `/health/readiness` 用它：一个源没配，对应的一族指标就算不出来。
        把「起得来但一律 503」报成 ready，会让看板一直显示空表而没人知道
        为什么。
        """
        names = ("cost_source", "qc_source", "review_source",
                 "delivery_source", "job_source", "accepted_source",
                 "evaluator_source")
        return sorted({getattr(getattr(self, name), "_unavailable_code", None)
                       for name in names} - {None})

    def _dep(self, gate: str, upstream: str, call):
        """上游不可达 → 503 `DEPENDENCY_UNAVAILABLE`，**绝不退回缓存或 0**。

        §4.6 第一行：源服务 503 → 快照 PARTIAL + stale_sources，「不给出
        看起来完整的数字」。连接断无法判断是哪一部分缺，所以整条命令拒绝；
        4xx 原样抛给调用方。
        """
        try:
            return call()
        except ConnectionError as exc:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                f"{gate}: {upstream} is unreachable; S44 will not invent a "
                f"number for it ({exc})",
                retryable=True,
                details={"gate": gate, "upstream": upstream,
                         "upstream_code": "UNREACHABLE"}) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(
                    503, "DEPENDENCY_UNAVAILABLE",
                    f"{gate}: {upstream} answered {exc.status} ({exc.code}); "
                    "S44 will not substitute a cached or zero value",
                    retryable=True,
                    details={"gate": gate, "upstream": upstream,
                             "upstream_code": exc.code}) from exc
            raise

    def _next_sequence(self, tenant_id: str, aggregate_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS s FROM outbox "
            "WHERE tenant_id=? AND aggregate_id=?",
            (tenant_id, aggregate_id)).fetchone()
        return int(row["s"]) + 1

    # ------------------------------------------------------ 指标定义
    def _seed_metric_definitions(self) -> None:
        """§10 第 1 步：八个 KPI 的口径写死，与 docs/15 §3 对齐。

        `INSERT OR IGNORE`：定义是版本化事实，重建时不能覆盖已存在的版本
        （那会把「旧快照用哪个定义算的」变成不可回答）。
        """
        stamp = _now()
        for definition in METRIC_DEFINITIONS:
            self.conn.execute(
                "INSERT OR IGNORE INTO metric_definition (tenant_id,"
                " metric_key, version, unit, numerator_spec, denominator_spec,"
                " sample_unit, exclusion_rules, ci_method, min_n, created_at)"
                " VALUES ('', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (definition["metric_key"], definition["version"],
                 definition["unit"], _jcs(definition["numerator_spec"]),
                 _jcs(definition["denominator_spec"]),
                 definition["sample_unit"],
                 _jcs(definition["exclusion_rules"]),
                 definition["ci_method"], definition["min_n"], stamp))

    def metric_definition(self, tenant_id: str, metric_key: str,
                          version: int | None = None) -> dict:
        """取定义：租户自定义版本优先于内置版本。"""
        params: list = [metric_key]
        sql = "SELECT * FROM metric_definition WHERE metric_key=?"
        if version is not None:
            sql += " AND version=?"
            params.append(version)
        sql += " ORDER BY (tenant_id = ?) DESC, version DESC"
        params.append(tenant_id)
        row = _row(self.conn.execute(sql, params).fetchone())
        if row is None:
            raise ServiceError(404, "METRIC_DEFINITION_NOT_FOUND",
                               f"no definition for metric {metric_key!r}")
        row["numerator_spec"] = _loads(row.get("numerator_spec"), {})
        row["denominator_spec"] = _loads(row.get("denominator_spec"), {})
        row["exclusion_rules"] = _loads(row.get("exclusion_rules"), [])
        return row

    def register_metric_definition(self, tenant_id: str, body: dict) -> dict:
        """注册（新版本）指标定义。同版本不可改写：口径变化 = 新版本。"""
        metric_key = body.get("metric_key")
        if metric_key not in METRIC_KEYS:
            raise ServiceError(
                422, "METRIC_KEY_UNSUPPORTED",
                f"{metric_key!r} is not one of the eight KPIs",
                details={"supported": list(METRIC_KEYS)})
        version = _int(body.get("version"))
        if version is None or version < 1:
            raise ServiceError(422, "INVALID_BODY",
                               "version must be an integer >= 1")
        ci_method = body.get("ci_method") or "none"
        if ci_method not in CI_METHODS:
            raise ServiceError(422, "CI_METHOD_UNSUPPORTED",
                               f"{ci_method!r} is not a supported CI method")
        existing = _row(self.conn.execute(
            "SELECT * FROM metric_definition WHERE tenant_id=? AND"
            " metric_key=? AND version=?",
            (tenant_id, metric_key, version)).fetchone())
        if existing is not None:
            raise ServiceError(
                409, "METRIC_DEFINITION_IMMUTABLE",
                "an existing (metric_key, version) is a frozen definition; "
                "bump the version instead of rewriting it")
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO metric_definition (tenant_id, metric_key, version,"
                " unit, numerator_spec, denominator_spec, sample_unit,"
                " exclusion_rules, ci_method, min_n, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, metric_key, version, body.get("unit") or "ratio",
                 _jcs(body.get("numerator_spec") or {}),
                 _jcs(body.get("denominator_spec") or {}),
                 body.get("sample_unit") or "episode",
                 _jcs(body.get("exclusion_rules") or []), ci_method,
                 int(body.get("min_n") or 1), _now()))
        return self.metric_definition(tenant_id, metric_key, version)

    # ---------------------------------------------------------- cohort
    def _project_scope_known(self, tenant_id: str, project_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM project_scope WHERE tenant_id=? AND project_id=?",
            (tenant_id, project_id)).fetchone() is not None

    def freeze_cohort(self, tenant_id: str, command_id: str, body: dict,
                      request_hash: str) -> dict:
        """冻结 cohort：成员集合 + 每源 watermark（§2.2、§4.1 第 2 步）。

        `include_failed_runs` 默认 true（§2.2 第三条）。项目归属必须已被
        本租户的 S08 事件证明，否则 403 —— 跨租户的 project_id 不能被
        「看起来合理」放过（E28-S44-02）。
        """
        definition = body.get("definition")
        if not isinstance(definition, dict):
            raise ServiceError(422, "INVALID_COHORT",
                               "definition must be an object")
        definition = self._normalize_cohort_definition(definition)
        project_ids = definition.get("project_ids") or []
        for project_id in project_ids:
            if not self._project_scope_known(tenant_id, str(project_id)):
                raise ServiceError(
                    403, "PROJECT_NOT_IN_TENANT",
                    f"project {project_id!r} is not known to belong to this "
                    "tenant; S44 will not freeze a cross-tenant cohort",
                    details={"project_id": str(project_id)})
        if body.get("cohort_id"):
            existing = _row(self.conn.execute(
                "SELECT * FROM cohort WHERE tenant_id=? AND cohort_id=?",
                (tenant_id, body["cohort_id"])).fetchone())
            if existing is not None:
                if _jcs(_loads(existing["definition"], {})) != _jcs(definition):
                    raise ServiceError(
                        409, "COHORT_DEFINITION_IMMUTABLE",
                        "this cohort_id is already frozen with another "
                        "definition")
                return self.cohort_view(self._cohort_view_row(existing))

        members = self._freeze_members(tenant_id, definition)
        watermarks = self._freeze_watermarks(tenant_id)
        cohort_id = str(body.get("cohort_id") or _new_id("cohort"))
        stamp = _now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO cohort (tenant_id, cohort_id, definition,"
                " frozen_at, member_count, source_watermarks, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, cohort_id, _jcs(definition), stamp,
                 len(members), _jcs(watermarks), stamp))
            for member in members:
                self.conn.execute(
                    "INSERT INTO cohort_member (tenant_id, cohort_id,"
                    " episode_id, project_id) VALUES (?, ?, ?, ?)",
                    (tenant_id, cohort_id, member["episode_id"],
                     member.get("project_id")))
        return self.cohort_view(self._cohort(tenant_id, cohort_id))

    def _normalize_cohort_definition(self, definition: dict) -> dict:
        normalized = dict(definition)
        normalized["include_failed_runs"] = bool(
            definition.get("include_failed_runs", True))
        normalized["include_abandoned"] = bool(
            definition.get("include_abandoned", False))
        window = definition.get("time_window")
        if not isinstance(window, dict) or not window.get("from") \
                or not window.get("to"):
            raise ServiceError(
                422, "INVALID_COHORT",
                "time_window needs explicit 'from' and 'to'; S44 will not "
                "default to 'now' (that makes recomputation nondeterministic)")
        start, end = _parse_ts(window["from"]), _parse_ts(window["to"])
        if start is None or end is None:
            raise ServiceError(422, "INVALID_COHORT",
                               "time_window bounds must be RFC 3339 timestamps")
        if start >= end:
            raise ServiceError(422, "INVALID_COHORT",
                               "time_window 'from' must precede 'to'")
        return normalized

    def _freeze_members(self, tenant_id: str, definition: dict) -> list[dict]:
        """从 S26 读面取成员。取不到就 503 —— 不按 definition 现场重算。"""
        window = definition.get("time_window") or {}
        query = {"project_ids": ",".join(definition.get("project_ids") or []),
                 "series_ids": ",".join(definition.get("series_ids") or []),
                 "episode_ids": ",".join(definition.get("episode_ids") or []),
                 "from": window.get("from"),
                 "to": window.get("to"),
                 "include_failed_runs": definition.get("include_failed_runs")}
        result = self._dep("cohort.freezes", "S26",
                           lambda: self.job_source(tenant_id, query))
        episodes = result.get("episodes") if isinstance(result, dict) else None
        if not isinstance(episodes, list):
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "cohort.freezes: S26 returned no episode list; an empty "
                "result is not the same as an empty cohort",
                details={"gate": "cohort.freezes", "upstream": "S26"})
        members = []
        for episode in episodes:
            if not isinstance(episode, dict) or not episode.get("episode_id"):
                continue
            if not definition.get("include_abandoned") \
                    and episode.get("abandoned") is True:
                continue
            members.append({"episode_id": str(episode["episode_id"]),
                            "project_id": episode.get("project_id")})
        members.sort(key=lambda member: member["episode_id"])
        return members

    def _freeze_watermarks(self, tenant_id: str) -> dict:
        return {source: self._observed_watermark(tenant_id, source)
                for source in ("s23", "s26", "s35", "s40", "s08", "s43")}

    def _observed_watermark(self, tenant_id: str, source: str) -> int:
        row = self.conn.execute(
            "SELECT watermark FROM source_watermark WHERE tenant_id=? AND"
            " source=?", (tenant_id, source)).fetchone()
        return int(row["watermark"]) if row is not None else 0

    def _cohort(self, tenant_id: str, cohort_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM cohort WHERE tenant_id=? AND cohort_id=?",
            (tenant_id, cohort_id)).fetchone())
        if row is None:
            # 跨租户与不存在同一个 404，不泄漏存在性（E28-S44-01）。
            raise ServiceError(404, "COHORT_NOT_FOUND",
                               f"no cohort {cohort_id!r}")
        return self._cohort_view_row(row)

    @staticmethod
    def _cohort_view_row(row: dict) -> dict:
        row["definition"] = _loads(row["definition"], {})
        row["source_watermarks"] = _loads(row["source_watermarks"], {})
        return row

    def cohort_members(self, tenant_id: str, cohort_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM cohort_member WHERE tenant_id=? AND cohort_id=?"
            " ORDER BY episode_id", (tenant_id, cohort_id)).fetchall()
        return [dict(row) for row in rows]

    def cohort_view(self, cohort: dict) -> dict:
        return {
            "cohort_id": cohort["cohort_id"],
            "definition": cohort["definition"],
            "frozen_at": cohort["frozen_at"],
            "member_count": cohort["member_count"],
            "source_watermarks": cohort["source_watermarks"],
        }

    def get_cohort(self, tenant_id: str, cohort_id: str) -> dict:
        return self.cohort_view(self._cohort(tenant_id, cohort_id))

    # ---------------------------------------------------------- 分摊
    def put_allocation_policy(self, tenant_id: str, body: dict) -> dict:
        """版本化分摊规则。同版本不可改写（§2.1 规则 2）。"""
        version = body.get("policy_version")
        if not isinstance(version, str) or not version:
            raise ServiceError(422, "INVALID_BODY",
                               "policy_version is required")
        rules = body.get("rules")
        if not isinstance(rules, list) or not rules:
            raise ServiceError(422, "INVALID_BODY",
                               "rules must be a non-empty list")
        for rule in rules:
            if not isinstance(rule, dict):
                raise ServiceError(422, "INVALID_BODY",
                                   "each rule is an object")
            if rule.get("basis") not in ALLOCATION_BASES:
                raise ServiceError(
                    422, "ALLOCATION_BASIS_UNSUPPORTED",
                    f"{rule.get('basis')!r} is not a supported basis",
                    details={"supported": list(ALLOCATION_BASES)})
            if rule.get("dimension") not in ALLOCATION_DIMENSIONS:
                raise ServiceError(
                    422, "ALLOCATION_DIMENSION_UNSUPPORTED",
                    f"{rule.get('dimension')!r} is not a supported dimension",
                    details={"supported": list(ALLOCATION_DIMENSIONS)})
        existing = _row(self.conn.execute(
            "SELECT * FROM allocation_policy WHERE tenant_id=? AND"
            " policy_version=?", (tenant_id, version)).fetchone())
        if existing is not None:
            if _loads(existing["rules"], []) != rules:
                raise ServiceError(
                    409, "ALLOCATION_POLICY_IMMUTABLE",
                    "changing a policy's rules requires a new "
                    "policy_version; old snapshots must stay explainable "
                    "(plan §2.1)")
            return self._policy_view(existing)
        effective_from = body.get("effective_from") or _now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO allocation_policy (tenant_id, policy_version,"
                " rules, effective_from, created_at) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, version, _jcs(rules), effective_from, _now()))
        return self._policy_view(self._allocation_policy(tenant_id, version))

    @staticmethod
    def _policy_view(row: dict) -> dict:
        return {"policy_version": row["policy_version"],
                "rules": _loads(row["rules"], []),
                "effective_from": row["effective_from"]}

    def _allocation_policy(self, tenant_id: str, version: str | None) -> dict:
        if version is None:
            row = _row(self.conn.execute(
                "SELECT * FROM allocation_policy WHERE tenant_id=?"
                " ORDER BY effective_from DESC, policy_version DESC LIMIT 1",
                (tenant_id,)).fetchone())
        else:
            row = _row(self.conn.execute(
                "SELECT * FROM allocation_policy WHERE tenant_id=? AND"
                " policy_version=?", (tenant_id, version)).fetchone())
        if row is None:
            raise ServiceError(
                422, "ALLOCATION_POLICY_REQUIRED",
                "no allocation policy is configured for this tenant; S44 "
                "will not invent a split (plan §4.2)")
        return row

    def _persist_allocations(self, tenant_id: str, entries: list[dict],
                             engine: AllocationEngine,
                             targets: dict | None = None) -> dict:
        """把分摊结果写进 `cost_allocation`，并校验守恒（§4.2、§7）。

        幂等靠主键 `(cost_entry_id, policy_version, dimension,
        dimension_value)`：重跑不产生重复金额。**绝不先删除旧分摊再写新
        的** —— 那会让「为什么这个月成本变了」无法回答（§7 末条）。
        """
        amounts = {str(entry["cost_entry_id"]): int(entry["amount_micro"])
                   for entry in entries
                   if entry.get("amount_micro") is not None}
        allocations: list[dict] = []
        unallocated = 0
        basis_counts: dict[str, int] = {}
        for entry in entries:
            # 归属到 tenant 的规则（evaluation → excluded）需要一个租户值。
            # **一律**用本次调用已签名的租户，上游条目里自带的 tenant_id
            # 不可信 —— 分摊行会落进 cost_allocation 的租户主键。
            scoped = {**entry, "tenant_id": tenant_id}
            outcome = engine.allocate(scoped, targets=targets)
            unallocated += outcome["unallocated_micro"]
            entry_id = str(entry["cost_entry_id"])
            for row in outcome["allocations"]:
                allocations.append({**row, "cost_entry_id": entry_id})
                basis_counts[row["basis"]] = \
                    basis_counts.get(row["basis"], 0) + 1
        AllocationEngine.verify(amounts, allocations)
        with Transaction(self.conn):
            for row in allocations:
                self.conn.execute(
                    "INSERT OR IGNORE INTO cost_allocation (tenant_id,"
                    " cost_entry_id, policy_version, dimension,"
                    " dimension_value, amount_micro, basis)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (tenant_id, row["cost_entry_id"], engine.policy_version,
                     row["dimension"], row["dimension_value"],
                     row["amount_micro"], row["basis"]))
        if unallocated:
            # §4.6：规则不覆盖 → 非零 + 告警，**不静默丢弃**。
            self._bump("analytics_allocation_unallocated_micro", unallocated)
        return {"policy_version": engine.policy_version,
                "entry_count": len(amounts),
                "allocation_count": len(allocations),
                "unallocated_micro": unallocated,
                "basis_counts": basis_counts}

    # ---- 指标计算 · 快照 · 导出 ----

    # ---- 成员与源读取 -------------------------------------------------------
    def _member_ids(self, tenant_id: str, cohort: dict) -> list[str]:
        return [member["episode_id"]
                for member in self.cohort_members(tenant_id, cohort["cohort_id"])]

    def _member_rows(self, tenant_id: str, cohort: dict) -> dict:
        return {member["episode_id"]: member
                for member in self.cohort_members(tenant_id, cohort["cohort_id"])}

    def _read(self, tenant_id: str, source: str, request: dict,
             gate: str | None = None) -> dict:
        """读一个上游。失败就是 503 `DEPENDENCY_UNAVAILABLE`（`_dep`）。"""
        callback = getattr(self, SOURCE_ATTR[source])
        return self._dep(gate or f"{source}.query", UPSTREAM_NAME[source],
                         lambda: callback(tenant_id, request))

    def _require_rows(self, source: str, members: list[str],
                     rows: list[dict], gate: str) -> dict[str, dict]:
        """冻结后的成员在源里必须有事实，否则**不许缩小分母**。

        少一个成员就少一个分母单位，率会偏高、成本会偏低 —— 这正是「只报
        成功样本均值」。缺事实不是「这一集没有」，所以 503，不是跳过。
        """
        by_id: dict[str, dict] = {}
        for row in rows:
            if isinstance(row, dict) and row.get("episode_id") is not None:
                by_id[str(row["episode_id"])] = row
        missing = [member for member in members if member not in by_id]
        if missing:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                f"{gate}: {UPSTREAM_NAME[source]} has no facts for "
                f"{len(missing)} frozen cohort member(s); S44 will not shrink the "
                "denominator by dropping them",
                retryable=True,
                details={"gate": gate, "upstream": UPSTREAM_NAME[source],
                         "upstream_code": "INCOMPLETE_READ",
                         "missing_count": len(missing),
                         "missing_members": missing[:20]})
        return by_id

    def _watermarks(self, result: dict, source_key: str) -> dict:
        watermark = result.get("watermark")
        if isinstance(watermark, int) and not isinstance(watermark, bool):
            return {source_key: watermark}
        return {}

    def _mixed_versions(self, definition: dict) -> bool:
        """§2.2：`model_revisions` 为空 = 不限 = 跨版本混算，必须显式标出。"""
        return not (definition.get("definition") or {}).get("model_revisions")

    # ---- 各 KPI 的取数 ------------------------------------------------------
    def collect_usable_rate(self, tenant_id, cohort, definition, body):
        """K1：分子 = S08 接受的 master 数；分母 = 进入 formal 渲染的集数。

        失败集留在分母里（E38-S44-04）。「渲染成功」不是「可用」：可用只由
        S08 的接受指针定义（§2.3 K1 陷阱）。
        """
        members = self._member_ids(tenant_id, cohort)
        jobs = self._read(tenant_id, "job", {"episode_ids": members}, "s26.query")
        accepted = self._read(tenant_id, "accepted", {"episode_ids": members},
                              "s08.query")
        job_rows = self._require_rows("job", members, jobs.get("episodes") or [],
                                      "s26.query")
        accepted_rows = self._require_rows(
            "accepted", members, accepted.get("episodes") or [], "s08.query")

        excluded = _excluded_skeleton()
        values: list[float] = []
        numerator = 0
        for member in members:
            job = job_rows[member]
            if job.get("abandoned") is True:
                excluded["abandoned"] += 1
                continue
            if not job.get("entered_formal_render"):
                # 没进入 formal 渲染的集不在分母里 —— 但计数写进 excluded，
                # 而不是让读者以为 cohort 就这么多集。
                excluded["insufficient_data"] += 1
                continue
            if job.get("failed") is True:
                excluded["failed_runs"] += 1
            hit = 1 if accepted_rows[member].get("accepted") is True else 0
            numerator += hit
            values.append(float(hit))
        watermarks = {**self._watermarks(jobs, "s26"),
                      **self._watermarks(accepted, "s08")}
        return {"numerator": numerator, "denominator": len(values),
                "n": len(values), "values": values, "excluded": excluded,
                "watermarks": watermarks, "scale": 1.0}

    def collect_first_pass_shot_rate(self, tenant_id, cohort, definition, body):
        """K2：分母 = 首次进入 QC 的镜头；分子 = 首轮 pass 且无返工。

        `abstain` 在分母里但**不是** pass（§2.3 K1/K2 注、E25-S44-01/02）。
        """
        members = self._member_ids(tenant_id, cohort)
        jobs = self._read(tenant_id, "job", {"episode_ids": members}, "s26.query")
        job_rows = self._require_rows("job", members, jobs.get("episodes") or [],
                                      "s26.query")
        excluded = _excluded_skeleton()
        excluded["abstain_treated_as"] = "not_pass"
        excluded["abstain_count"] = 0
        values: list[float] = []
        for member in members:
            job = job_rows[member]
            if job.get("abandoned") is True:
                excluded["abandoned"] += 1
                continue
            for shot in job.get("shots") or []:
                if not isinstance(shot, dict) or not shot.get("entered_first_qc"):
                    continue
                decision = shot.get("first_qc_decision")
                if decision == "abstain":
                    excluded["abstain_count"] += 1
                hit = (decision == "pass"
                       and int(shot.get("rework_decisions") or 0) == 0)
                values.append(1.0 if hit else 0.0)
        return {"numerator": int(sum(values)), "denominator": len(values),
                "n": len(values), "values": values, "excluded": excluded,
                "watermarks": self._watermarks(jobs, "s26"), "scale": 1.0}

    def collect_cost_per_usable_minute(self, tenant_id, cohort, definition,
                                       body):
        """K3：分子 = 生产类分摊金额（micro）；分母 = **可用**成片分钟。

        三条口径同时生效：
        1. `cost_type=evaluation` 不计入（E38-S44-09）；
        2. `estimated_by_policy` 与精确分摊分开计数，**不混成一个数**
           （§2.1 规则 3、E38-S44-08）；
        3. 分钟按每集整分钟向下取整；不足一分钟的集不产生每分钟成本，
           计入 `insufficient_data`，而不是把 0.4 分钟当 1 分钟。
        """
        members = self._member_ids(tenant_id, cohort)
        member_rows = self._member_rows(tenant_id, cohort)
        jobs = self._read(tenant_id, "job", {"episode_ids": members}, "s26.query")
        accepted = self._read(tenant_id, "accepted", {"episode_ids": members},
                              "s08.query")
        cost = self._read(tenant_id, "cost", {"episode_ids": members,
                                              "cohort": {"cohort_id":
                                                         cohort["cohort_id"]}},
                          "s23.cost_breakdown")
        job_rows = self._require_rows("job", members, jobs.get("episodes") or [],
                                      "s26.query")
        accepted_rows = self._require_rows(
            "accepted", members, accepted.get("episodes") or [], "s08.query")

        policy = self._allocation_policy(tenant_id, body.get("allocation_policy_version"))
        engine = AllocationEngine(policy["policy_version"], _loads(policy["rules"], []))
        targets = {
            "by_episode_share": {m: max(1, len(job_rows[m].get("shots") or []))
                                 for m in members},
            "by_usable_minutes": {
                m: max(1, int(accepted_rows[m].get("duration_seconds") or 0) // 60)
                for m in members},
        }
        entries = [entry for entry in (cost.get("entries") or [])
                   if isinstance(entry, dict)]
        persisted = self._persist_allocations(tenant_id, entries, engine,
                                              targets=targets)

        rows = self.conn.execute(
            "SELECT * FROM cost_allocation WHERE tenant_id=? AND policy_version=?",
            (tenant_id, policy["policy_version"])).fetchall()
        precise: dict[str, int] = {}
        estimated_micro = 0
        for row in rows:
            row = dict(row)
            if row["basis"] == "excluded":
                continue
            entry = next((item for item in entries
                          if str(item.get("cost_entry_id")) == row["cost_entry_id"]),
                         None)
            if entry is not None and entry.get("cost_type") == "evaluation":
                continue  # 评测成本不进 K3
            if row["basis"] == "estimated_by_policy":
                estimated_micro += int(row["amount_micro"])
                continue
            if row["dimension"] == "episode":
                precise[row["dimension_value"]] = \
                    precise.get(row["dimension_value"], 0) + int(row["amount_micro"])

        excluded = _excluded_skeleton()
        excluded["estimated_allocation_micro"] = estimated_micro
        excluded["estimated_allocation_counted_separately"] = True
        excluded["unallocated_micro"] = persisted["unallocated_micro"]
        excluded["cost_type_evaluation"] = "excluded_from_production"
        values: list[float] = []
        minutes_total = 0
        precise_total = 0
        for member in members:
            job = job_rows[member]
            if job.get("abandoned") is True:
                excluded["abandoned"] += 1
                continue
            if accepted_rows[member].get("accepted") is not True:
                if job.get("failed") is True:
                    excluded["failed_runs"] += 1
                continue  # 只有可用成片的分钟进分母（§2.3 K3 陷阱）
            seconds = int(accepted_rows[member].get("duration_seconds") or 0)
            minutes = seconds // 60
            if minutes <= 0:
                excluded["insufficient_data"] += 1
                continue
            amount = precise.get(member, 0)
            minutes_total += minutes
            precise_total += amount
            values.append((amount / 1_000_000.0) / minutes)
        return {"numerator": precise_total, "denominator": minutes_total,
                "n": minutes_total, "values": values, "excluded": excluded,
                "watermarks": {**self._watermarks(jobs, "s26"),
                               **self._watermarks(accepted, "s08"),
                               **self._watermarks(cost, "s23")},
                "scale": 1.0 / 1_000_000.0,
                "allocation": persisted,
                "allocation_policy_version": policy["policy_version"]}

    def collect_delivery_lead_time(self, tenant_id, cohort, definition, body):
        """K4：`published_at` − 首次 run 创建时间，单位秒（§2.3 K4）。

        用发布时刻，不用最后一次修改；没有发布记录的集不产生样本（不是 0）。
        """
        members = self._member_ids(tenant_id, cohort)
        jobs = self._read(tenant_id, "job", {"episode_ids": members}, "s26.query")
        delivery = self._read(tenant_id, "delivery", {"episode_ids": members},
                              "s43.query")
        job_rows = self._require_rows("job", members, jobs.get("episodes") or [],
                                      "s26.query")
        packages: dict[str, dict] = {}
        for row in delivery.get("packages") or []:
            if isinstance(row, dict) and row.get("episode_id") is not None \
                    and row.get("published_at"):
                packages[str(row["episode_id"])] = row
        excluded = _excluded_skeleton()
        values: list[float] = []
        for member in members:
            job = job_rows[member]
            if job.get("abandoned") is True:
                excluded["abandoned"] += 1
                continue
            package = packages.get(member)
            first_run = _parse_ts(job.get("first_run_created_at"))
            if package is None or first_run is None:
                excluded["insufficient_data"] += 1
                continue
            published = _parse_ts(package.get("published_at"))
            if published is None:
                excluded["insufficient_data"] += 1
                continue
            seconds = (published - first_run).total_seconds()
            if seconds < 0:
                # 时间倒挂是数据缺陷，不是 0 秒交付。
                raise ServiceError(
                    500, "SOURCE_INCONSISTENT",
                    f"episode {member!r} was published before its first run "
                    "started; S44 will not report a negative lead time as zero",
                    details={"episode_id": member})
            values.append(float(seconds))
        return {"numerator": sum(values), "denominator": len(values),
                "n": len(values), "values": values, "excluded": excluded,
                "watermarks": {**self._watermarks(jobs, "s26"),
                               **self._watermarks(delivery, "s43")},
                "scale": 1.0}

    def collect_rework_rate(self, tenant_id, cohort, definition, body):
        """K5：返回工过的**镜头数** / 全部镜头数（按镜头，不按次）。"""
        members = self._member_ids(tenant_id, cohort)
        jobs = self._read(tenant_id, "job", {"episode_ids": members}, "s26.query")
        job_rows = self._require_rows("job", members, jobs.get("episodes") or [],
                                      "s26.query")
        excluded = _excluded_skeleton()
        values: list[float] = []
        for member in members:
            job = job_rows[member]
            if job.get("abandoned") is True:
                excluded["abandoned"] += 1
                continue
            for shot in job.get("shots") or []:
                if not isinstance(shot, dict):
                    continue
                reworked = int(shot.get("rework_decisions") or 0) >= 1
                values.append(1.0 if reworked else 0.0)
        return {"numerator": int(sum(values)), "denominator": len(values),
                "n": len(values), "values": values, "excluded": excluded,
                "watermarks": self._watermarks(jobs, "s26"), "scale": 1.0}

    def collect_human_intervention_rate(self, tenant_id, cohort, definition,
                                        body):
        """K6：有人工决定的节点数 / 全部节点数（自动通过不算干预）。"""
        members = self._member_ids(tenant_id, cohort)
        jobs = self._read(tenant_id, "job", {"episode_ids": members}, "s26.query")
        job_rows = self._require_rows("job", members, jobs.get("episodes") or [],
                                      "s26.query")
        excluded = _excluded_skeleton()
        values: list[float] = []
        for member in members:
            job = job_rows[member]
            if job.get("abandoned") is True:
                excluded["abandoned"] += 1
                continue
            for node in job.get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                values.append(1.0 if node.get("human_decision") is True else 0.0)
        return {"numerator": int(sum(values)), "denominator": len(values),
                "n": len(values), "values": values, "excluded": excluded,
                "watermarks": self._watermarks(jobs, "s26"), "scale": 1.0}

    def collect_memory_correctness(self, tenant_id, cohort, definition, body):
        """K7：被引用事实里未被「S17 标记冲突且 S40 确认」的条数占比。

        S44 **不判定证伪**（§11 C7）：`confirmed` 只能由 S40 给。缺这一源就
        503，不按「没确认就是对的」推断。
        """
        members = self._member_ids(tenant_id, cohort)
        review = self._read(tenant_id, "review",
                            {"episode_ids": members, "kind": "memory_conflict"},
                            "s40.query")
        referenced = [str(fact) for fact in (review.get("referenced_fact_ids") or [])]
        if not referenced:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "s40.query: no referenced memory facts were returned; an empty "
                "list is not a denominator of zero",
                details={"gate": "s40.query", "upstream": "S40",
                         "upstream_code": "INCOMPLETE_READ"})
        refuted = set()
        for decision in review.get("decisions") or []:
            if not isinstance(decision, dict):
                continue
            if decision.get("kind") != "memory_conflict":
                continue
            if decision.get("confirmed") is not True:
                continue  # 未确认不算证伪
            if decision.get("fact_id") is not None:
                refuted.add(str(decision["fact_id"]))
        excluded = _excluded_skeleton()
        excluded["refuted_facts"] = len(refuted & set(referenced))
        excluded["rule"] = "only_s17_flagged_and_s40_confirmed"
        values = [0.0 if fact in refuted else 1.0 for fact in referenced]
        return {"numerator": int(sum(values)), "denominator": len(referenced),
                "n": len(referenced), "values": values, "excluded": excluded,
                "watermarks": self._watermarks(review, "s40"), "scale": 1.0}

    def collect_customer_roi(self, tenant_id, cohort, definition, body):
        """K8：合同收入 − 分摊成本，分母是收入。

        收入没有服务权威（§11 C1）：**必须显式给**，缺就是 422，绝不填 0。
        """
        revenue = _int(body.get("revenue_micro"))
        if revenue is None or revenue <= 0:
            raise ServiceError(
                422, "REVENUE_REQUIRED",
                "customer_roi needs an explicit positive revenue_micro and a "
                "contract_ref; S44 has no revenue authority and will not assume 0",
                details={"field": "revenue_micro"})
        if not body.get("contract_ref"):
            raise ServiceError(422, "REVENUE_REQUIRED",
                               "customer_roi needs contract_ref to attribute the "
                               "revenue", details={"field": "contract_ref"})
        policy = self._allocation_policy(tenant_id,
                                         body.get("allocation_policy_version"))
        members = self._member_ids(tenant_id, cohort)
        if not members:
            return {"numerator": None, "denominator": None, "n": 0, "values": [],
                    "excluded": _excluded_skeleton(), "watermarks": {},
                    "scale": 1.0, "revenue_micro": revenue,
                    "contract_ref": body.get("contract_ref")}
        rows = self.conn.execute(
            "SELECT amount_micro, dimension_value FROM cost_allocation "
            "WHERE tenant_id=? AND policy_version=? AND dimension='episode' "
            "AND basis NOT IN ('estimated_by_policy','excluded')",
            (tenant_id, policy["policy_version"])).fetchall()
        member_set = set(members)
        cost = sum(int(row["amount_micro"]) for row in rows
                   if row["dimension_value"] in member_set)
        if not rows:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "customer_roi: no cost allocations exist for this policy version; "
                "run the cost_per_usable_minute snapshot first (S44 does not "
                "assume a cost of 0)",
                details={"gate": "s23.allocation", "upstream": "S23",
                         "upstream_code": "NO_ALLOCATION"})
        excluded = _excluded_skeleton()
        excluded["contract_revenue_is_external_input"] = True
        excluded["allocated_cost_micro"] = cost
        return {"numerator": revenue - cost, "denominator": revenue,
                "n": 1, "values": [float(revenue - cost) / revenue],
                "excluded": excluded, "watermarks": {},
                "scale": 1.0, "revenue_micro": revenue,
                "contract_ref": body.get("contract_ref")}

    # ---- 快照 ---------------------------------------------------------------
    def compute_snapshot(self, tenant_id: str, command_id: str, body: dict,
                         request_hash: str) -> dict:
        """§4.1 的 1–7 步。重算 = 新快照；同天然键返回已有快照。"""
        metric_key = body.get("metric_key")
        if metric_key not in COLLECTORS:
            raise ServiceError(
                422, "METRIC_KEY_UNSUPPORTED",
                f"{metric_key!r} is not one of the eight KPIs",
                details={"supported": list(METRIC_KEYS)})
        cohort_id = body.get("cohort_id")
        if not cohort_id:
            raise ServiceError(422, "INVALID_BODY", "cohort_id is required")
        definition = self.metric_definition(tenant_id, metric_key,
                                            _int(body.get("metric_version")))
        cohort = self._cohort(tenant_id, str(cohort_id))

        wait_seconds = min(max(int(body.get("wait_seconds") or 0), 0),
                           MAX_WAIT_SECONDS)
        started = time.monotonic()
        collected = self._collect(tenant_id, cohort, definition, body)
        stale = self._stale_sources(cohort, collected["watermarks"])
        if stale and wait_seconds > 0:
            # §4.6：可等待，最长 60s；等完再读一遍，水位由上游自己决定。
            remaining = wait_seconds - (time.monotonic() - started)
            if remaining > 0:
                sleeper = getattr(self, "sleeper", None) or time.sleep
                sleeper(min(remaining, MAX_WAIT_SECONDS))
            collected = self._collect(tenant_id, cohort, definition, body)
            stale = self._stale_sources(cohort, collected["watermarks"])

        fields = self._snapshot_fields(collected, definition, stale,
                                   self._mixed_versions(cohort))
        watermarks = collected["watermarks"]
        watermarks_hash = _sha256_hex(_jcs(watermarks))
        existing = _row(self.conn.execute(
            "SELECT * FROM metric_snapshot WHERE tenant_id=? AND cohort_id=? AND"
            " metric_key=? AND metric_version=? AND watermarks_hash=? AND"
            " allocation_policy_version IS ?",
            (tenant_id, cohort["cohort_id"], metric_key,
             definition["version"], watermarks_hash,
             fields["allocation_policy_version"])).fetchone())
        if existing is not None:
            self._assert_same_snapshot(existing, fields)
            return {**_metric_view(existing), "replayed": True}

        self._reject_older_watermarks(tenant_id, cohort["cohort_id"], metric_key,
                                      definition["version"], watermarks)

        snapshot_id = _new_id("snapshot")
        computed_at = _now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO metric_snapshot (tenant_id, snapshot_id, metric_key,"
                " metric_version, cohort_id, value, numerator_value,"
                " denominator_value, unit, n, distribution, ci95, excluded,"
                " mixed_versions, stale_sources, source_watermarks,"
                " watermarks_hash, allocation_policy_version, low_confidence,"
                " computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?, ?, ?, ?)",
                (tenant_id, snapshot_id, metric_key, definition["version"],
                 cohort["cohort_id"], fields["value"], fields["numerator_value"],
                 fields["denominator_value"], definition["unit"], fields["n"],
                 _jcs(fields["distribution"]), _jcs(fields["ci95"]),
                 _jcs(fields["excluded"]), 1 if fields["mixed_versions"] else 0,
                 _jcs(stale), _jcs(watermarks), watermarks_hash,
                 fields["allocation_policy_version"],
                 1 if fields["low_confidence"] else 0, computed_at))
            self._emit(tenant_id, EVENT_SNAPSHOT, snapshot_id, snapshot_id,
                       self._snapshot_event(tenant_id, snapshot_id, metric_key,
                                            definition, cohort, fields, stale,
                                            watermarks, computed_at, body),
                       body)
        if stale:
            self._bump("analytics_snapshot_partial_total", len(stale))
        row = _row(self.conn.execute(
            "SELECT * FROM metric_snapshot WHERE tenant_id=? AND snapshot_id=?",
            (tenant_id, snapshot_id)).fetchone())
        return {**_metric_view(row), "replayed": False}

    def _collect(self, tenant_id: str, cohort: dict, definition: dict,
                body: dict) -> dict:
        """`definition` 是 MetricDefinition；collector 要的是 cohort 的筛选定义。"""
        collector = getattr(self, COLLECTORS[definition["metric_key"]])
        return collector(tenant_id, cohort, cohort["definition"], body)

    def _stale_sources(self, cohort: dict, watermarks: dict) -> dict:
        """落后于 cohort 冻结水位的源。值是本服务**观测到的**水位，不是 lag。"""
        frozen = cohort.get("source_watermarks") or {}
        stale = {}
        for source, observed in sorted(watermarks.items()):
            baseline = frozen.get(source)
            if isinstance(baseline, int) and observed < baseline:
                stale[source] = observed
        return stale

    def _snapshot_fields(self, collected: dict, definition: dict,
                        stale: dict, mixed_versions: bool) -> dict:
        n = int(collected["n"])
        numerator = collected["numerator"]
        denominator = collected["denominator"]
        scale = float(collected.get("scale") or 1.0)
        values = collected["values"]
        method = definition["ci_method"]

        if n <= 0 or not denominator or numerator is None:
            value = None
            ci = None
            dist = {}
        else:
            value = _round(float(numerator) * scale / float(denominator))
            dist = {key: _round(item) for key, item in
                    distribution(values).items()}
            if method == "wilson":
                ci = wilson_interval(int(numerator), int(denominator))
                ci = None if ci is None else {
                    "low": _round(ci["low"]), "high": _round(ci["high"])}
            elif method == "bootstrap_percentile":
                seed = f"{definition['metric_key']}|{definition['version']}|" \
                       f"{_jcs(collected['watermarks'])}"
                ci = bootstrap_interval(values, seed_material=seed)
                ci = None if ci is None else {
                    "low": _round(ci["low"]), "high": _round(ci["high"])}
            else:
                ci = None

        low_confidence = bool(collected.get("force_low_confidence"))
        if method == "bootstrap_percentile" and 0 < n < LOW_CONFIDENCE_N:
            low_confidence = True
        excluded = dict(collected["excluded"])
        if not excluded:
            excluded = _excluded_skeleton()
        return {
            "value": value,
            "n": n,
            "numerator_value": _round(numerator) if numerator is not None else None,
            "denominator_value": _round(denominator) if denominator is not None
            else None,
            "distribution": dist,
            "ci95": ci,
            "excluded": excluded,
            "mixed_versions": mixed_versions,
            "stale_sources": stale,
            "low_confidence": low_confidence,
            "allocation_policy_version": collected.get("allocation_policy_version"),
        }

    def _assert_same_snapshot(self, existing: dict, fields: dict) -> None:
        """同天然键必须给出同样的数（§4.6 最后一行）。

        不同说明有非确定性（例如用了当前时间做窗口）—— 这是 bug，不是
        「统计本来就有波动」。宁可报错，也不要让两份互相矛盾的报告都叫
        「同一个快照」。
        """
        keys = ("value", "n", "numerator_value", "denominator_value",
                "allocation_policy_version", "low_confidence")
        differences = {}
        for key in keys:
            stored = existing[key]
            if key == "low_confidence":
                stored, incoming = bool(stored), bool(fields[key])
            else:
                stored, incoming = stored, fields[key]
            if stored != incoming:
                differences[key] = {"stored": stored, "recomputed": incoming}
        if differences:
            self._bump("analytics_snapshot_nondeterministic_total")
            raise ServiceError(
                500, "NONDETERMINISTIC_SNAPSHOT",
                "recomputing the same cohort at the same watermarks produced "
                "different numbers; this is a determinism bug, not statistical "
                "noise",
                details={"differences": differences})

    def _reject_older_watermarks(self, tenant_id, cohort_id, metric_key,
                                metric_version, watermarks: dict) -> None:
        """旧水位不得覆盖新水位（E36-S44-02）。"""
        rows = self.conn.execute(
            "SELECT source_watermarks FROM metric_snapshot WHERE tenant_id=? AND"
            " cohort_id=? AND metric_key=? AND metric_version=?",
            (tenant_id, cohort_id, metric_key, metric_version)).fetchall()
        for row in rows:
            stored = _loads(row["source_watermarks"], {})
            shared = [key for key in watermarks if key in stored]
            if not shared:
                continue
            if all(watermarks[key] <= stored[key] for key in shared) and \
                    any(watermarks[key] < stored[key] for key in shared):
                raise ServiceError(
                    409, "STALE_WATERMARK",
                    "this computation is based on older watermarks than an "
                    "existing snapshot; it will not overwrite a newer projection",
                    details={"incoming": watermarks, "stored": stored})

    def _snapshot_event(self, tenant_id, snapshot_id, metric_key, definition,
                       cohort, fields, stale, watermarks, computed_at,
                       body) -> dict:
        """17 个键，与 `contracts/analytics.metric-snapshot-created.v2.json`
        的 `required` 完全一致（`additionalProperties: false`）。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "metric_key": metric_key,
            "metric_version": definition["version"],
            "cohort_id": cohort["cohort_id"],
            "value": fields["value"],
            "unit": definition["unit"],
            "n": fields["n"],
            "distribution": fields["distribution"],
            "ci95": fields["ci95"],
            "excluded": fields["excluded"],
            "mixed_versions": bool(fields["mixed_versions"]),
            "stale_sources": stale,
            "source_watermarks": watermarks,
            "allocation_policy_version": fields["allocation_policy_version"],
            "low_confidence": bool(fields["low_confidence"]),
            "computed_at": computed_at,
        }

    def get_snapshot(self, tenant_id: str, snapshot_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM metric_snapshot WHERE tenant_id=? AND snapshot_id=?",
            (tenant_id, snapshot_id)).fetchone())
        if row is None:
            raise ServiceError(404, "SNAPSHOT_NOT_FOUND",
                               f"no snapshot {snapshot_id!r}")
        return _metric_view(row, superseded_definition=self._is_superseded(
            tenant_id, row))

    def _is_superseded(self, tenant_id: str, row: dict) -> bool:
        newer = self.conn.execute(
            "SELECT 1 FROM metric_snapshot WHERE tenant_id=? AND cohort_id=? AND"
            " metric_key=? AND metric_version > ? LIMIT 1",
            (tenant_id, row["cohort_id"], row["metric_key"],
             row["metric_version"])).fetchone()
        return newer is not None

    def list_snapshots(self, tenant_id: str, cohort_id: str | None = None,
                       metric_key: str | None = None,
                       limit: int = 50) -> dict:
        clauses = ["tenant_id=?"]
        params: list = [tenant_id]
        if cohort_id:
            clauses.append("cohort_id=?")
            params.append(cohort_id)
        if metric_key:
            clauses.append("metric_key=?")
            params.append(metric_key)
        params.append(max(1, min(int(limit), 500)))
        rows = self.conn.execute(
            "SELECT * FROM metric_snapshot WHERE " + " AND ".join(clauses) +
            " ORDER BY computed_at DESC, snapshot_id LIMIT ?", params).fetchall()
        return {"snapshots": [_metric_view(dict(row),
                                          superseded_definition=self._is_superseded(
                                              tenant_id, dict(row)))
                              for row in rows]}

    def get_metrics(self, tenant_id: str, *, series_id: str | None = None,
                    cohort_id: str | None = None, project_id: str | None = None,
                    limit: int = 50) -> dict:
        """§6.1 的扩展响应：每个指标一条最新快照 + `data_as_of` + `staleness`。

        没有快照的指标**不出现在列表里**（不是 value=0 的一条）。
        """
        if cohort_id:
            cohort_ids = [cohort_id]
        else:
            cohort_ids = []
            for row in self.conn.execute(
                    "SELECT cohort_id, definition FROM cohort WHERE tenant_id=?",
                    (tenant_id,)).fetchall():
                definition = _loads(row["definition"], {})
                if series_id and series_id in (definition.get("series_ids") or []):
                    cohort_ids.append(row["cohort_id"])
                elif project_id and project_id in (definition.get("project_ids")
                                                   or []):
                    cohort_ids.append(row["cohort_id"])
        if not cohort_ids and not cohort_id and (series_id or project_id):
            raise ServiceError(404, "COHORT_NOT_FOUND",
                               "no cohort matches this series/project for the "
                               "tenant")
        metrics = []
        seen = set()
        for cid in cohort_ids:
            for row in self.conn.execute(
                    "SELECT * FROM metric_snapshot WHERE tenant_id=? AND"
                    " cohort_id=? ORDER BY computed_at DESC, metric_version DESC,"
                    " snapshot_id DESC",
                    (tenant_id, cid)).fetchall():
                data = dict(row)
                if data["metric_key"] in seen:
                    continue
                seen.add(data["metric_key"])
                item = _metric_view(data, superseded_definition=False)
                for key in ("numerator_value", "denominator_value",
                            "superseded_definition"):
                    item.pop(key, None)
                metrics.append(item)
        metrics.sort(key=lambda item: item["metric_key"])
        watermarks = {source: self._observed_watermark(tenant_id, source)
                      for source in COHORT_SOURCES}
        data_as_of = max((item["computed_at"] for item in metrics), default=None)
        return {
            "series_id": series_id,
            "project_id": project_id,
            "cohort_id": cohort_id,
            "projection_watermark": min(watermarks.values()) if watermarks
            else None,
            "data_as_of": data_as_of,
            "staleness": self._staleness(tenant_id),
            "metrics": metrics[:max(1, min(int(limit), 500))],
            "mixed_versions": any(item["mixed_versions"] for item in metrics),
            "allocation_policy_version": next(
                (item["allocation_policy_version"] for item in metrics
                 if item["allocation_policy_version"]), None),
            "source_watermarks": watermarks,
        }

    def _staleness(self, tenant_id: str) -> dict:
        """§4.1、§6.1：每个源「距今多久前观测到」。**不能把旧进度说成现在。**"""
        now = _now_dt()
        out = {}
        for row in self.conn.execute(
                "SELECT source, observed_at FROM source_watermark WHERE tenant_id=?",
                (tenant_id,)).fetchall():
            observed = _parse_ts(row["observed_at"])
            if observed is None:
                continue
            seconds = max(0, int((now - observed).total_seconds()))
            out[row["source"]] = f"{seconds}s"
        return out

    def export_snapshot(self, tenant_id: str, snapshot_id: str,
                        fmt: str = "csv") -> dict:
        """导出的是**带口径的表格**，不是一列数字：首行口径头 + 每行 n/CI/排除项。

        缺数据是空单元格 + `note`，**不是 0**（§6.5 末条）。
        """
        if (fmt or "").lower() != "csv":
            raise ServiceError(
                422, "EXPORT_FORMAT_UNSUPPORTED",
                f"export format {fmt!r} is not implemented; S44 will not hand back "
                "a CSV disguised as a spreadsheet",
                details={"supported": ["csv"]})
        snapshot = self.get_snapshot(tenant_id, snapshot_id)
        cohort = self.get_cohort(tenant_id, snapshot["cohort_id"])
        definition = self.metric_definition(tenant_id, snapshot["metric_key"],
                                            snapshot["metric_version"])
        window = (cohort["definition"] or {}).get("time_window") or {}
        header = [
            f"metric_key={snapshot['metric_key']}",
            f"metric_version={snapshot['metric_version']}",
            f"unit={snapshot['unit']}",
            f"cohort_id={snapshot['cohort_id']}",
            f"cohort_frozen_at={cohort['frozen_at']}",
            f"time_window_from={window.get('from')}",
            f"time_window_to={window.get('to')}",
            f"include_failed_runs={cohort['definition'].get('include_failed_runs')}",
            f"source_watermarks={_jcs(snapshot['source_watermarks'])}",
            f"allocation_policy_version={snapshot['allocation_policy_version']}",
            f"ci_method={definition['ci_method']}",
            f"numerator_spec={_jcs(definition['numerator_spec'])}",
            f"denominator_spec={_jcs(definition['denominator_spec'])}",
            f"excluded={_jcs(snapshot['excluded'])}",
            f"computed_at={snapshot['computed_at']}",
        ]
        ci = snapshot["ci95"] or {}
        note = "" if snapshot["value"] is not None else "no data: value is null, not 0"
        cells = [
            snapshot["snapshot_id"], snapshot["metric_key"],
            str(snapshot["metric_version"]), snapshot["cohort_id"],
            "" if snapshot["value"] is None else repr(snapshot["value"]),
            snapshot["unit"], str(snapshot["n"]),
            "" if snapshot["numerator_value"] is None
            else repr(snapshot["numerator_value"]),
            "" if snapshot["denominator_value"] is None
            else repr(snapshot["denominator_value"]),
            "" if ci.get("low") is None else repr(ci["low"]),
            "" if ci.get("high") is None else repr(ci["high"]),
            definition["ci_method"],
            snapshot["allocation_policy_version"] or "",
            "true" if snapshot["low_confidence"] else "false",
            "true" if snapshot["mixed_versions"] else "false",
            _jcs(snapshot["source_watermarks"]), _jcs(snapshot["excluded"]), note,
        ]
        body = "\n".join(header) + "\n" + ",".join(SNAPSHOT_CSV_COLUMNS) + "\n" \
            + _csv_row(cells) + "\n"
        return {"snapshot_id": snapshot_id, "format": "csv", "csv": body,
                "columns": list(SNAPSHOT_CSV_COLUMNS)}

    # ---- 评测 · 数据集 · 盲评 · 实验 · 投影 · 事件 ----

    # ---- 评测运行器（§3.1、§4.6）-------------------------------------------
    def create_evaluation_run(self, tenant_id: str, command_id: str,
                              body: dict, request_hash: str) -> dict:
        """启动评测。**重复启动不会产生第二次付费**（E12-S44-02）。

        三重防线：
        1. `(tenant, command_id)` 唯一（DDL）+ API 层幂等表；
        2. 执行前先按 `(dataset_id, dataset_version, evaluator_ref)` 找未终结
           的 run —— 同一次评测被不同 command_id 重触发也不会再跑一遍；
        3. 平台级开关 `evaluation_execution_enabled` 默认 false → 503。
        """
        dataset_id = body.get("dataset_id")
        if not dataset_id:
            raise ServiceError(422, "INVALID_BODY", "dataset_id is required")
        dataset_version = _int(body.get("dataset_version"))
        if dataset_version is None or dataset_version < 1:
            raise ServiceError(422, "INVALID_BODY",
                               "dataset_version must be an integer >= 1")
        evaluator_ref = body.get("evaluator_ref")
        if not isinstance(evaluator_ref, dict) or not evaluator_ref:
            raise ServiceError(422, "INVALID_BODY",
                               "evaluator_ref must be a non-empty object")
        self._require_dataset(tenant_id, str(dataset_id), dataset_version)

        existing = _row(self.conn.execute(
            "SELECT * FROM evaluation_run WHERE tenant_id=? AND command_id=?",
            (tenant_id, command_id)).fetchone())
        if existing is not None:
            return self._evaluation_view(existing, replayed=True)

        active = self.conn.execute(
            "SELECT * FROM evaluation_run WHERE tenant_id=? AND dataset_id=? AND"
            " dataset_version=? AND evaluator_ref=? AND state NOT IN"
            " ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')",
            (tenant_id, str(dataset_id), dataset_version,
             _jcs(evaluator_ref))).fetchone()
        if active is not None:
            # 同一数据集+评测器已在跑：返回那一个，而不是再产生一次执行。
            return {**self._evaluation_view(dict(active), replayed=True),
                    "deduplicated": True}

        if not self.evaluation_execution_enabled:
            raise ServiceError(
                503, "EVALUATION_EXECUTION_DISABLED",
                "evaluation execution is disabled; samples that need generation "
                "must go through S21->S26 with S23 budget reservation and S22 "
                "licensing (plan §1, AGENTS.md stop condition)")

        item_total = self._dataset_item_count(tenant_id, str(dataset_id),
                                              dataset_version)
        evaluation_run_id = _new_id("evaluation")
        started_at = _now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO evaluation_run (tenant_id, evaluation_run_id,"
                " dataset_id, dataset_version, evaluator_ref, state, item_total,"
                " item_succeeded, item_failed, result_ref, budget_ref, command_id,"
                " request_hash, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, 0, 0, NULL, NULL, ?, ?,"
                " ?, NULL)",
                (tenant_id, evaluation_run_id, str(dataset_id), dataset_version,
                 _jcs(evaluator_ref), item_total, command_id, request_hash,
                 started_at))

        request = {"dataset_id": str(dataset_id),
                   "dataset_version": dataset_version,
                   "evaluator_ref": evaluator_ref,
                   "items": self._dataset_items(tenant_id, str(dataset_id),
                                                dataset_version),
                   "evaluation_run_id": evaluation_run_id}
        try:
            outcome = self._dep(
                "evaluation.execute", "S21",
                lambda: self.evaluator_source(tenant_id, request))
        except ServiceError:
            # 执行失败就是 FAILED，**不产快照**（§4.6）。半截结果不叫 PARTIAL：
            # PARTIAL 的定义是「确实跑完了，部分条目失败」。
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE evaluation_run SET state='FAILED', finished_at=?"
                    " WHERE tenant_id=? AND evaluation_run_id=?",
                    (_now(), tenant_id, evaluation_run_id))
                row = _row(self.conn.execute(
                    "SELECT * FROM evaluation_run WHERE tenant_id=? AND"
                    " evaluation_run_id=?",
                    (tenant_id, evaluation_run_id)).fetchone())
                self._emit(tenant_id, EVENT_EVALUATION, evaluation_run_id,
                           evaluation_run_id,
                           self._evaluation_event(tenant_id, row), body)
            raise

        succeeded = _int(outcome.get("item_succeeded"))
        failed = _int(outcome.get("item_failed"))
        if succeeded is None or failed is None:
            raise ServiceError(
                500, "SOURCE_INCONSISTENT",
                "evaluator returned no item_succeeded/item_failed counts; S44 "
                "will not infer them (a missing count is not zero)")
        if succeeded + failed > item_total:
            raise ServiceError(
                500, "SOURCE_INCONSISTENT",
                "evaluator reported more items than the dataset has",
                details={"item_total": item_total, "succeeded": succeeded,
                         "failed": failed})
        if failed == 0 and succeeded == item_total:
            state = "SUCCEEDED"
        elif succeeded == 0:
            state = "FAILED"        # 全部失败 → FAILED，不产出快照（§4.6）
        else:
            state = "PARTIAL"
        finished_at = _now()
        result_ref = outcome.get("result_ref") if isinstance(
            outcome.get("result_ref"), dict) else None
        budget_ref = outcome.get("budget_ref") if isinstance(
            outcome.get("budget_ref"), dict) else None
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE evaluation_run SET state=?, item_succeeded=?,"
                " item_failed=?, result_ref=?, budget_ref=?, finished_at=?"
                " WHERE tenant_id=? AND evaluation_run_id=?",
                (state, succeeded, failed, _jcs(result_ref), _jcs(budget_ref),
                 finished_at, tenant_id, evaluation_run_id))
            row = _row(self.conn.execute(
                "SELECT * FROM evaluation_run WHERE tenant_id=? AND"
                " evaluation_run_id=?",
                (tenant_id, evaluation_run_id)).fetchone())
            self._emit(tenant_id, EVENT_EVALUATION, evaluation_run_id,
                       evaluation_run_id,
                       self._evaluation_event(tenant_id, row), body)
        if state == "PARTIAL":
            self._bump("analytics_eval_run_partial_total")
        return self._evaluation_view(row, replayed=False)

    def _evaluation_event(self, tenant_id: str, row: dict) -> dict:
        """13 个键，与 `analytics.evaluation-completed.v2` 的 `required` 一致。

        事件只在**终态**发出；`state` 的枚举里没有 RUNNING（契约如此）。
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "evaluation_run_id": row["evaluation_run_id"],
            "dataset_id": row["dataset_id"],
            "dataset_version": row["dataset_version"],
            "evaluator_ref": _loads(row["evaluator_ref"], {}),
            "state": row["state"],
            "item_total": row["item_total"],
            "item_succeeded": row["item_succeeded"],
            "item_failed": row["item_failed"],
            "result_ref": _loads(row["result_ref"], None),
            "budget_ref": _loads(row["budget_ref"], None),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    def _evaluation_view(self, row: dict, *, replayed: bool) -> dict:
        return {
            "evaluation_run_id": row["evaluation_run_id"],
            "dataset_id": row["dataset_id"],
            "dataset_version": row["dataset_version"],
            "evaluator_ref": _loads(row["evaluator_ref"], {}),
            "state": row["state"],
            "item_total": row["item_total"],
            "item_succeeded": row["item_succeeded"],
            "item_failed": row["item_failed"],
            "result_ref": _loads(row["result_ref"], None),
            "budget_ref": _loads(row["budget_ref"], None),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            # PARTIAL 的结果可用于分析，但调用方必须知道圈了多少条（§3.1）。
            "partial": row["state"] == "PARTIAL",
            "replayed": replayed,
        }

    def get_evaluation_run(self, tenant_id: str, run_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM evaluation_run WHERE tenant_id=? AND"
            " evaluation_run_id=?", (tenant_id, run_id)).fetchone())
        if row is None:
            raise ServiceError(404, "EVALUATION_RUN_NOT_FOUND",
                               f"no evaluation run {run_id!r}")
        return self._evaluation_view(row, replayed=False)

    def _require_dataset(self, tenant_id: str, dataset_id: str,
                        version: int) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM benchmark_dataset WHERE tenant_id=? AND dataset_id=?"
            " AND version=?", (tenant_id, dataset_id, version)).fetchone())
        if row is None:
            raise ServiceError(404, "DATASET_NOT_FOUND",
                               f"no dataset {dataset_id!r} v{version}")
        return row

    def _dataset_item_count(self, tenant_id, dataset_id, version) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM benchmark_item WHERE tenant_id=? AND"
            " dataset_id=? AND dataset_version=?", (tenant_id, dataset_id, version)
        ).fetchone()
        return int(row["c"])

    def _dataset_items(self, tenant_id, dataset_id, version) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM benchmark_item WHERE tenant_id=? AND dataset_id=? AND"
            " dataset_version=? ORDER BY item_id",
            (tenant_id, dataset_id, version)).fetchall()
        return [{"item_id": row["item_id"],
                 "artifact_refs": _loads(row["artifact_refs"], []),
                 "ground_truth": _loads(row["ground_truth"], {}),
                 "difficulty": row["difficulty"]}
                for row in rows]

    # ---- 数据集 -------------------------------------------------------------
    def put_dataset(self, tenant_id: str, body: dict) -> dict:
        """建数据集/版本。同版本不可改写（评测必须可复现）。"""
        dataset_id = body.get("dataset_id")
        if not dataset_id:
            raise ServiceError(422, "INVALID_BODY", "dataset_id is required")
        version = _int(body.get("version"))
        if version is None or version < 1:
            raise ServiceError(422, "INVALID_BODY", "version must be >= 1")
        purpose = body.get("purpose")
        if purpose not in DATASET_PURPOSES:
            raise ServiceError(422, "DATASET_PURPOSE_UNSUPPORTED",
                               f"{purpose!r} is not a supported purpose",
                               details={"supported": list(DATASET_PURPOSES)})
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise ServiceError(422, "INVALID_BODY", "items must be a non-empty list")
        existing = _row(self.conn.execute(
            "SELECT * FROM benchmark_dataset WHERE tenant_id=? AND dataset_id=?"
            " AND version=?", (tenant_id, str(dataset_id), version)).fetchone())
        if existing is not None:
            raise ServiceError(409, "DATASET_IMMUTABLE",
                               "a dataset version is frozen once written; bump the "
                               "version instead of rewriting its items")
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO benchmark_dataset (tenant_id, dataset_id, name,"
                " version, purpose, item_count, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, str(dataset_id), body.get("name") or str(dataset_id),
                 version, purpose, len(items), _now()))
            for item in items:
                if not isinstance(item, dict) or not item.get("item_id"):
                    raise ServiceError(422, "INVALID_BODY",
                                       "each item needs an item_id")
                if not item.get("artifact_refs"):
                    # 评测条目必须指真实产物（§1 唯一写者：引用真实产物 + 标注）。
                    raise ServiceError(
                        422, "INVALID_BODY",
                        "each benchmark item needs artifact_refs to a real "
                        "artifact; S44 will not evaluate an empty reference",
                        details={"item_id": item.get("item_id")})
                difficulty = item.get("difficulty")
                if difficulty is not None and difficulty not in DIFFICULTIES:
                    raise ServiceError(422, "INVALID_BODY",
                                       f"difficulty {difficulty!r} is unsupported")
                self.conn.execute(
                    "INSERT INTO benchmark_item (tenant_id, dataset_id,"
                    " dataset_version, item_id, artifact_refs, ground_truth,"
                    " difficulty, source_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (tenant_id, str(dataset_id), version, str(item["item_id"]),
                     _jcs(item.get("artifact_refs") or []),
                     _jcs(item.get("ground_truth") or {}), difficulty,
                     _jcs(item.get("source_ref")) if item.get("source_ref")
                     else None))
        return self.get_dataset(tenant_id, str(dataset_id), version)

    def get_dataset(self, tenant_id: str, dataset_id: str,
                    version: int | None = None) -> dict:
        if version is None:
            row = _row(self.conn.execute(
                "SELECT * FROM benchmark_dataset WHERE tenant_id=? AND dataset_id=?"
                " ORDER BY version DESC LIMIT 1",
                (tenant_id, dataset_id)).fetchone())
        else:
            row = _row(self.conn.execute(
                "SELECT * FROM benchmark_dataset WHERE tenant_id=? AND dataset_id=?"
                " AND version=?", (tenant_id, dataset_id, version)).fetchone())
        if row is None:
            raise ServiceError(404, "DATASET_NOT_FOUND",
                               f"no dataset {dataset_id!r}")
        return {"dataset_id": row["dataset_id"], "version": row["version"],
                "name": row["name"], "purpose": row["purpose"],
                "item_count": row["item_count"],
                "items": self._dataset_items(tenant_id, row["dataset_id"],
                                             row["version"])}

    # ---- 盲评（§4.3）--------------------------------------------------------
    def create_blind_review(self, tenant_id: str, command_id: str, body: dict,
                            request_hash: str) -> dict:
        """开盲评：抽样可复现、必须列投票人、响应**永不**带模型/prompt/重试信息。"""
        dataset_id = body.get("dataset_id")
        if not dataset_id:
            raise ServiceError(422, "INVALID_BODY", "dataset_id is required")
        version = _int(body.get("dataset_version"))
        if version is None or version < 1:
            raise ServiceError(422, "INVALID_BODY",
                               "dataset_version must be an integer >= 1")
        self._require_dataset(tenant_id, str(dataset_id), version)
        dimensions = body.get("vote_dimensions") or list(VOTE_DIMENSIONS)
        if not isinstance(dimensions, list) or not dimensions:
            raise ServiceError(422, "INVALID_BODY",
                               "vote_dimensions must be a non-empty list")
        for dimension in dimensions:
            if dimension not in VOTE_DIMENSIONS:
                raise ServiceError(
                    422, "VOTE_DIMENSION_UNSUPPORTED",
                    f"{dimension!r} is not a supported vote dimension",
                    details={"supported": list(VOTE_DIMENSIONS)})
        reviewers = body.get("reviewers")
        if not isinstance(reviewers, list) or len(reviewers) < 2:
            # 一个人打分的「一致性」没有意义；少于两位不叫盲评。
            raise ServiceError(422, "INVALID_BODY",
                               "reviewers must list at least two reviewer ids")
        all_items = self._dataset_items(tenant_id, str(dataset_id), version)
        item_count = _int(body.get("item_count")) or len(all_items)
        if item_count < 1 or item_count > len(all_items):
            raise ServiceError(
                422, "INVALID_BODY",
                f"item_count must be between 1 and {len(all_items)}",
                details={"available": len(all_items)})
        blind_review_id = _new_id("blind_review")
        sampled = _sample_items(blind_review_id, all_items, item_count)
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO blind_review (tenant_id, blind_review_id, dataset_id,"
                " dataset_version, dimensions, item_count, reviewer_count, state,"
                " agreement_alpha, item_ids, reviewers, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL, ?, ?, ?)",
                (tenant_id, blind_review_id, str(dataset_id), version,
                 _jcs(dimensions), len(sampled), len(reviewers),
                 _jcs([item["item_id"] for item in sampled]), _jcs(reviewers),
                 _now()))
            for item in sampled:
                self.conn.execute(
                    "INSERT INTO blind_review_item (tenant_id, blind_review_id,"
                    " item_id, artifact_refs) VALUES (?, ?, ?, ?)",
                    (tenant_id, blind_review_id, item["item_id"],
                     _jcs(item["artifact_refs"])))
        return self.get_blind_review(tenant_id, blind_review_id)

    def get_blind_review(self, tenant_id: str, review_id: str) -> dict:
        """盲评视图。**响应里没有 model_id / prompt_revision / attempt_no /
        is_retry**（§4.3 第 1 条）—— 条目只给 artifact 引用与条目 id。"""
        row = _row(self.conn.execute(
            "SELECT * FROM blind_review WHERE tenant_id=? AND blind_review_id=?",
            (tenant_id, review_id)).fetchone())
        if row is None:
            raise ServiceError(404, "BLIND_REVIEW_NOT_FOUND",
                               f"no blind review {review_id!r}")
        items = self.conn.execute(
            "SELECT item_id, artifact_refs FROM blind_review_item WHERE tenant_id=?"
            " AND blind_review_id=? ORDER BY item_id",
            (tenant_id, review_id)).fetchall()
        return {
            "blind_review_id": row["blind_review_id"],
            "dataset_id": row["dataset_id"],
            "dataset_version": row["dataset_version"],
            "dimensions": _loads(row["dimensions"], []),
            "item_count": row["item_count"],
            "reviewer_count": row["reviewer_count"],
            "reviewers": _loads(row["reviewers"], []),
            "state": row["state"],
            "agreement_alpha": row["agreement_alpha"],
            "items": [{"item_id": item["item_id"],
                       "artifact_refs": _loads(item["artifact_refs"], [])}
                      for item in items],
            "blind": True,
        }

    def vote(self, tenant_id: str, command_id: str, review_id: str, body: dict,
             request_hash: str) -> dict:
        """批量投票。同 `(reviewer,item,dimension)` 覆盖并更新 `voted_at`（§7）。"""
        review = _row(self.conn.execute(
            "SELECT * FROM blind_review WHERE tenant_id=? AND blind_review_id=?",
            (tenant_id, review_id)).fetchone())
        if review is None:
            raise ServiceError(404, "BLIND_REVIEW_NOT_FOUND",
                               f"no blind review {review_id!r}")
        if review["state"] != "OPEN":
            raise ServiceError(409, "BLIND_REVIEW_CLOSED",
                               f"blind review is {review['state']}; votes are "
                               "closed")
        dimensions = set(_loads(review["dimensions"], []))
        reviewers = set(_loads(review["reviewers"], []))
        item_ids = set(_loads(review["item_ids"], []))
        votes = body.get("votes")
        if not isinstance(votes, list) or not votes:
            raise ServiceError(422, "INVALID_BODY", "votes must be a non-empty list")
        normalized = []
        for vote in votes:
            if not isinstance(vote, dict):
                raise ServiceError(422, "INVALID_BODY", "each vote is an object")
            reviewer = str(vote.get("reviewer_id") or "")
            item_id = str(vote.get("item_id") or "")
            dimension = str(vote.get("dimension") or "")
            score = vote.get("score")
            if not reviewer or not item_id or not dimension:
                raise ServiceError(422, "INVALID_BODY",
                                   "each vote needs reviewer_id/item_id/dimension")
            if reviewer not in reviewers:
                raise ServiceError(
                    403, "REVIEWER_NOT_IN_SESSION",
                    f"{reviewer!r} is not a reviewer of this blind review",
                    details={"reviewer_id": reviewer})
            if item_id not in item_ids:
                raise ServiceError(
                    422, "ITEM_NOT_IN_SESSION",
                    f"{item_id!r} was not sampled for this blind review",
                    details={"item_id": item_id})
            if dimension not in dimensions:
                raise ServiceError(422, "VOTE_DIMENSION_UNSUPPORTED",
                                   f"{dimension!r} is not a dimension of this review")
            if not isinstance(score, int) or isinstance(score, bool) \
                    or not 1 <= score <= 5:
                raise ServiceError(422, "INVALID_BODY",
                                   "score must be an integer 1..5")
            normalized.append({"reviewer_id": reviewer, "item_id": item_id,
                               "dimension": dimension, "score": score,
                               "comment_ref": vote.get("comment_ref")})
        stamp = _now()
        with Transaction(self.conn):
            for vote in normalized:
                self.conn.execute(
                    "INSERT INTO blind_review_vote (tenant_id, blind_review_id,"
                    " reviewer_id, item_id, dimension, score, comment_ref,"
                    " voted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (tenant_id, blind_review_id, reviewer_id,"
                    " item_id, dimension) DO UPDATE SET score=excluded.score,"
                    " comment_ref=excluded.comment_ref, voted_at=excluded.voted_at",
                    (tenant_id, review_id, vote["reviewer_id"], vote["item_id"],
                     vote["dimension"], vote["score"],
                     _jcs(vote["comment_ref"]) if vote.get("comment_ref") else None,
                     stamp))
        return self.get_blind_review(tenant_id, review_id)

    def close_blind_review(self, tenant_id: str, command_id: str,
                           review_id: str, body: dict,
                           request_hash: str) -> dict:
        """关评并算 alpha。`α < 0.667` 标 low_agreement，**不给校准结论**（§4.6）。"""
        review = _row(self.conn.execute(
            "SELECT * FROM blind_review WHERE tenant_id=? AND blind_review_id=?",
            (tenant_id, review_id)).fetchone())
        if review is None:
            raise ServiceError(404, "BLIND_REVIEW_NOT_FOUND",
                               f"no blind review {review_id!r}")
        if review["state"] != "OPEN":
            return {**self.get_blind_review(tenant_id, review_id),
                    "replayed": True}
        dimensions = _loads(review["dimensions"], [])
        item_ids = _loads(review["item_ids"], [])
        summary = {}
        low_agreement = False
        all_ratings: list[list[float]] = []
        for dimension in dimensions:
            ratings = []
            for item_id in item_ids:
                rows = self.conn.execute(
                    "SELECT score FROM blind_review_vote WHERE tenant_id=? AND"
                    " blind_review_id=? AND item_id=? AND dimension=?",
                    (tenant_id, review_id, item_id, dimension)).fetchall()
                scores = [float(row["score"]) for row in rows]
                if scores:
                    ratings.append(scores)
            all_ratings.extend(ratings)
            alpha = krippendorff_alpha(ratings)
            rated_items = len(ratings)
            if rated_items < BLIND_LOW_CONFIDENCE_N or alpha is None:
                # §4.3 第 3 条：样本太少或无法算一致性 → 不给均值。
                summary[dimension] = {"status": "insufficient_data",
                                      "rated_items": rated_items,
                                      "agreement_alpha": alpha,
                                      "mean": None}
                continue
            values = [score for scores in ratings for score in scores]
            if alpha < LOW_ALPHA:
                low_agreement = True
                self._bump("analytics_blind_review_low_alpha_total")
            summary[dimension] = {
                "status": "low_agreement" if alpha < LOW_ALPHA else "ok",
                "rated_items": rated_items,
                "agreement_alpha": _round(alpha),
                "mean": _round(_mean(values)),
                "distribution": {key: _round(item) for key, item in
                                 distribution(values).items()},
            }
        overall = krippendorff_alpha(all_ratings)
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE blind_review SET state='CLOSED', agreement_alpha=?"
                " WHERE tenant_id=? AND blind_review_id=?",
                (_round(overall), tenant_id, review_id))
        return {
            "blind_review_id": review_id,
            "state": "CLOSED",
            "agreement_alpha": _round(overall),
            "low_agreement": low_agreement,
            "dimensions": summary,
            # S44 可以说「自动 QC 与人工判断的一致率是 x」，但**不能**据此写回
            # QC（§4.3 第 4 条）。
            "writes_back_to_qc": False,
            "replayed": False,
        }

    # ---- 实验（§3.3、§4.4）--------------------------------------------------
    def create_experiment(self, tenant_id: str, command_id: str, body: dict,
                          request_hash: str) -> dict:
        """建实验：分配确定性、计划指标必须显式列出（未计划的标 exploratory）。"""
        name = body.get("name")
        if not name:
            raise ServiceError(422, "INVALID_BODY", "name is required")
        arms = body.get("arms")
        if not isinstance(arms, list) or len(arms) < 2:
            raise ServiceError(422, "INVALID_BODY",
                               "an experiment needs at least two arms")
        arm_names = []
        for arm in arms:
            if not isinstance(arm, dict) or not arm.get("name"):
                raise ServiceError(422, "INVALID_BODY", "each arm needs a name")
            if not isinstance(arm.get("config_ref"), dict):
                raise ServiceError(422, "INVALID_BODY",
                                   "each arm needs a config_ref object")
            arm_names.append(str(arm["name"]))
        if len(set(arm_names)) != len(arm_names):
            raise ServiceError(422, "INVALID_BODY", "arm names must be unique")
        planned = body.get("planned_metrics")
        if not isinstance(planned, list) or not planned:
            # §4.4：提前停止要看多重比较；没有计划指标就没有校正的分母。
            raise ServiceError(422, "INVALID_BODY",
                               "planned_metrics must be a non-empty list")
        for key in planned:
            if key not in METRIC_KEYS:
                raise ServiceError(422, "METRIC_KEY_UNSUPPORTED",
                                   f"{key!r} is not one of the eight KPIs")
        assignment_rule = body.get("assignment_rule") or {
            "kind": "hash_modulo", "field": "subject_id", "salt": name}
        if assignment_rule.get("kind") != "hash_modulo":
            raise ServiceError(422, "ASSIGNMENT_RULE_UNSUPPORTED",
                               "only deterministic hash_modulo assignment is "
                               "supported (plan §4.4)")
        experiment_id = _new_id("experiment")
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO experiment (tenant_id, experiment_id, name, arms,"
                " planned_metrics, assignment_rule, state, started_at, stopped_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'DRAFT', NULL, NULL)",
                (tenant_id, experiment_id, str(name), _jcs(arms),
                 _jcs(list(planned)), _jcs(assignment_rule)))
        return self.get_experiment(tenant_id, experiment_id)

    def start_experiment(self, tenant_id: str, command_id: str,
                         experiment_id: str, body: dict,
                         request_hash: str) -> dict:
        row = self._experiment_row(tenant_id, experiment_id)
        if row["state"] == "RUNNING":
            return self.get_experiment(tenant_id, experiment_id)
        if row["state"] != "DRAFT":
            raise ServiceError(409, "EXPERIMENT_NOT_DRAFT",
                               f"experiment is {row['state']}")
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE experiment SET state='RUNNING', started_at=?"
                " WHERE tenant_id=? AND experiment_id=?",
                (_now(), tenant_id, experiment_id))
        return self.get_experiment(tenant_id, experiment_id)

    def stop_experiment(self, tenant_id: str, command_id: str,
                        experiment_id: str, body: dict,
                        request_hash: str) -> dict:
        """止损。STOPPED 保留全部数据（§3.3）。"""
        row = self._experiment_row(tenant_id, experiment_id)
        if row["state"] in ("STOPPED", "COMPLETED"):
            return self.get_experiment(tenant_id, experiment_id)
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE experiment SET state=?, stopped_at=?"
                " WHERE tenant_id=? AND experiment_id=?",
                (body.get("state") if body.get("state") in ("STOPPED", "COMPLETED")
                 else "STOPPED", _now(), tenant_id, experiment_id))
        return self.get_experiment(tenant_id, experiment_id)

    def _experiment_row(self, tenant_id: str, experiment_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM experiment WHERE tenant_id=? AND experiment_id=?",
            (tenant_id, experiment_id)).fetchone())
        if row is None:
            raise ServiceError(404, "EXPERIMENT_NOT_FOUND",
                               f"no experiment {experiment_id!r}")
        return row

    def get_experiment(self, tenant_id: str, experiment_id: str) -> dict:
        row = self._experiment_row(tenant_id, experiment_id)
        return {"experiment_id": row["experiment_id"], "name": row["name"],
                "arms": _loads(row["arms"], []),
                "planned_metrics": _loads(row["planned_metrics"], []),
                "assignment_rule": _loads(row["assignment_rule"], {}),
                "state": row["state"], "started_at": row["started_at"],
                "stopped_at": row["stopped_at"]}

    def assign(self, tenant_id: str, experiment_id: str,
               subject_id: str) -> dict:
        """同 episode 永远同臂（§4.4）；已分配的不重算。"""
        row = self._experiment_row(tenant_id, experiment_id)
        if row["state"] != "RUNNING":
            raise ServiceError(409, "EXPERIMENT_NOT_RUNNING",
                               f"experiment is {row['state']}; assignments only "
                               "exist while it is RUNNING")
        arms = _loads(row["arms"], [])
        rule = _loads(row["assignment_rule"], {})
        existing = _row(self.conn.execute(
            "SELECT * FROM experiment_assignment WHERE tenant_id=? AND"
            " experiment_id=? AND subject_id=?",
            (tenant_id, experiment_id, str(subject_id))).fetchone())
        if existing is not None:
            return {"experiment_id": experiment_id, "subject_id": subject_id,
                    "arm": existing["arm"], "replayed": True}
        bucket = int(_sha256_hex(
            f"{rule.get('salt', '')}|{subject_id}")[:12], 16) % len(arms)
        arm = str(arms[bucket]["name"])
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO experiment_assignment (tenant_id, experiment_id,"
                " subject_id, arm, assigned_at) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, experiment_id, str(subject_id), arm, _now()))
        return {"experiment_id": experiment_id, "subject_id": subject_id,
                "arm": arm, "replayed": False}

    def experiment_results(self, tenant_id: str, experiment_id: str) -> dict:
        """计划指标 = 可解释；未计划的快照结果标 exploratory（§4.4 第 3 条）。"""
        row = self._experiment_row(tenant_id, experiment_id)
        planned = set(_loads(row["planned_metrics"], []))
        assignments = self.conn.execute(
            "SELECT arm, COUNT(*) AS c FROM experiment_assignment WHERE tenant_id=?"
            " AND experiment_id=? GROUP BY arm",
            (tenant_id, experiment_id)).fetchall()
        counts = {item["arm"]: int(item["c"]) for item in assignments}
        return {
            "experiment_id": experiment_id,
            "state": row["state"],
            "planned_metrics": sorted(planned),
            "arm_counts": counts,
            "exploratory": {},   # 快照是 cohort 级对象；按 experiment 出快照时填
            "note": "snapshots are cohort-scoped; use exclude_experiment_arms or a "
                    "per-experiment cohort so experiment data does not enter "
                    "production KPIs",
        }

    # ---- 成本投影（§4.5）----------------------------------------------------
    def compute_projection(self, tenant_id: str, command_id: str, body: dict,
                           request_hash: str) -> dict:
        """`actual_to_date + remaining_days × p50_daily_rate ± CI`。

        三条：区间必给、窗口必标（7d/30d 可能差 3 倍）、**不用于放行**。
        """
        window = body.get("window")
        if window not in PROJECTION_WINDOWS:
            raise ServiceError(422, "PROJECTION_WINDOW_UNSUPPORTED",
                               f"{window!r} is not a supported window",
                               details={"supported": list(PROJECTION_WINDOWS)})
        scope = body.get("scope")
        if not isinstance(scope, dict) or not scope:
            raise ServiceError(422, "INVALID_BODY",
                               "scope must name a project or the tenant")
        cohort_id = body.get("cohort_id")
        if not cohort_id:
            raise ServiceError(422, "INVALID_BODY", "cohort_id is required")
        cohort = self._cohort(tenant_id, str(cohort_id))
        members = self._member_ids(tenant_id, cohort)
        cost = self._read(tenant_id, "cost", {"episode_ids": members,
                                              "cohort": {"cohort_id":
                                                         cohort["cohort_id"]}},
                          "s23.cost_breakdown")
        entries = [entry for entry in (cost.get("entries") or [])
                   if isinstance(entry, dict)]
        amount_total = sum(int(entry["amount_micro"]) for entry in entries
                           if isinstance(entry.get("amount_micro"), int)
                           and not isinstance(entry.get("amount_micro"), bool))
        days = 7 if window == "7d" else 30
        window_start = _now_dt().timestamp() - days * 86400
        daily: dict[str, int] = {}
        for entry in entries:
            amount = entry.get("amount_micro")
            if not isinstance(amount, int) or isinstance(amount, bool):
                continue
            stamp = _parse_ts(entry.get("occurred_at") or entry.get("recorded_at"))
            if stamp is None:
                continue
            if stamp.timestamp() < window_start:
                continue
            key = stamp.strftime("%Y-%m-%d")
            daily[key] = daily.get(key, 0) + amount
        if not daily:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "s23.cost_breakdown: no dated entries in the projection window; "
                "S44 will not extrapolate from an empty rate",
                retryable=True,
                details={"gate": "s23.cost_breakdown", "upstream": "S23",
                         "upstream_code": "NO_DATED_ENTRIES"})
        rates = [float(value) for value in daily.values()]
        p50 = percentile(sorted(rates), 0.5) or 0.0
        interval = bootstrap_interval(rates, seed_material=f"projection|{window}|"
                                      f"{_jcs(sorted(daily.items()))}")
        remaining_days = max(0, 30 - len(daily))
        projected = amount_total + int(round(remaining_days * p50))
        ci_low = ci_high = None
        if interval is not None:
            ci_low = max(0, amount_total + int(round(remaining_days * interval["low"])))
            ci_high = amount_total + int(round(remaining_days * interval["high"]))
        projection_id = _new_id("projection")
        computed_at = _now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO cost_projection (tenant_id, projection_id, scope,"
                " window, actual_to_date_micro, projected_total_micro,"
                " ci_low_micro, ci_high_micro, based_on_rate, computed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, projection_id, _jcs(scope), window, amount_total,
                 projected, ci_low if ci_low is not None else 0,
                 ci_high if ci_high is not None else projected, p50, computed_at))
            self._emit(tenant_id, EVENT_PROJECTION, projection_id, projection_id, {
                "schema_version": SCHEMA_VERSION,
                "projection_id": projection_id,
                "scope": scope,
                "window": window,
                "actual_to_date_micro": amount_total,
                "projected_total_micro": projected,
                "ci_low_micro": ci_low,
                "ci_high_micro": ci_high,
                "based_on_rate": p50,
                "computed_at": computed_at,
            }, body)
        return self.get_projection(tenant_id, projection_id)

    def get_projection(self, tenant_id: str, projection_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM cost_projection WHERE tenant_id=? AND projection_id=?",
            (tenant_id, projection_id)).fetchone())
        if row is None:
            raise ServiceError(404, "PROJECTION_NOT_FOUND",
                               f"no projection {projection_id!r}")
        return {"projection_id": row["projection_id"],
                "scope": _loads(row["scope"], {}),
                "window": row["window"],
                "actual_to_date_micro": row["actual_to_date_micro"],
                "projected_total_micro": row["projected_total_micro"],
                "ci_low_micro": row["ci_low_micro"],
                "ci_high_micro": row["ci_high_micro"],
                "based_on_rate": row["based_on_rate"],
                "computed_at": row["computed_at"],
                # §4.5 末条：投影只给看板与人看，**从不**用于预算放行。
                "used_for_admission": False}

    # ---- 客户 ROI（K8）------------------------------------------------------
    def compute_roi(self, tenant_id: str, command_id: str, body: dict,
                    request_hash: str) -> dict:
        cohort_id = body.get("cohort_id")
        if not cohort_id:
            raise ServiceError(422, "INVALID_BODY", "cohort_id is required")
        revenue = _int(body.get("revenue_micro"))
        if revenue is None or revenue <= 0:
            raise ServiceError(422, "REVENUE_REQUIRED",
                               "revenue_micro must be an explicit positive integer")
        if not body.get("contract_ref"):
            raise ServiceError(422, "REVENUE_REQUIRED",
                               "contract_ref is required to attribute the revenue")
        cohort = self._cohort(tenant_id, str(cohort_id))
        snapshot = self.compute_snapshot(
            tenant_id, command_id,
            {"metric_key": "customer_roi", "cohort_id": cohort["cohort_id"],
             "revenue_micro": revenue, "contract_ref": body.get("contract_ref"),
             "allocation_policy_version": body.get("allocation_policy_version")},
            request_hash)
        roi_id = _new_id("roi")
        cost = int(snapshot.get("excluded", {}).get("allocated_cost_micro") or 0)
        margin = _round((revenue - cost) / revenue) if revenue else None
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO customer_roi (tenant_id, roi_id, contract_ref,"
                " cohort_id, revenue_micro, allocated_cost_micro, gross_margin,"
                " as_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, roi_id, _jcs(body.get("contract_ref")),
                 cohort["cohort_id"], revenue, cost, margin, _now()))
        return {"roi_id": roi_id, "cohort_id": cohort["cohort_id"],
                "revenue_micro": revenue, "allocated_cost_micro": cost,
                "gross_margin": margin, "snapshot": snapshot,
                "revenue_source": "external_input"}

    # ---- 事件 ---------------------------------------------------------------
    def _emit(self, tenant_id: str, event_type: str, subject: str,
             aggregate_id: str, data: dict, body: dict) -> str:
        context = (body or {}).get("execution_context") or \
                  (body or {}).get("context") or {}
        correlation = str(context.get("trace_id") or context.get("command_id")
                          or (body or {}).get("command_id") or "")
        envelope = new_event(
            source=SOURCE, type=event_type, subject=subject, tenant_id=tenant_id,
            home_cell=str(context.get("home_cell") or "cell-0"),
            aggregate_id=aggregate_id,
            sequence=self._next_sequence(tenant_id, aggregate_id)
            if event_type != EVENT_SNAPSHOT else SEQUENCE_ONE,
            correlation_id=correlation or aggregate_id,
            causation_id=(body or {}).get("command_id"),
            data=data, schema_version=SCHEMA_VERSION)
        return self.outbox.append(envelope)

    def consume_event(self, tenant_id: str, envelope: dict) -> dict:
        """消费上游事件：只推进本地水位/归属投影，**不改任何生产事实**。"""
        if not isinstance(envelope, dict):
            raise ServiceError(400, "INVALID_BODY",
                               "event envelope must be an object")
        event_id = str(envelope.get("id") or envelope.get("event_id") or "")
        if not event_id:
            raise ServiceError(400, "INVALID_BODY", "event id is required")
        event_type = str(envelope.get("type") or envelope.get("event_type") or "")
        consumer = CONSUMERS.get(event_type)
        if consumer is None:
            raise ServiceError(422, "UNKNOWN_EVENT",
                               f"S44 does not consume {event_type!r}")
        envelope_tenant = envelope.get("tenant_id")
        if envelope_tenant is not None and str(envelope_tenant) != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "event tenant disagrees with the signed context")
        if not self.inbox.consume(consumer, event_id):
            return {"consumed": False, "duplicate": True, "event_id": event_id}
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        sequence = envelope.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            sequence = _int(sequence)
        source_key = EVENT_SOURCE[event_type][1]
        with Transaction(self.conn):
            if isinstance(sequence, int):
                current = self._observed_watermark(tenant_id, source_key)
                if sequence > current:
                    self.conn.execute(
                        "INSERT INTO source_watermark (tenant_id, source,"
                        " watermark, event_id, observed_at) VALUES (?, ?, ?, ?, ?)"
                        " ON CONFLICT (tenant_id, source) DO UPDATE SET"
                        " watermark=excluded.watermark, event_id=excluded.event_id,"
                        " observed_at=excluded.observed_at",
                        (tenant_id, source_key, sequence, event_id, _now()))
            if event_type == EVENT_SNAPSHOT_COMMITTED:
                self.learn_project_scope(tenant_id, event_id, data)
                self.learn_accepted(tenant_id, event_id, data)
        return {"consumed": True, "duplicate": False, "event_id": event_id,
                "source": source_key, "watermark": sequence}

    def learn_project_scope(self, tenant_id: str, event_id: str,
                            data: dict) -> None:
        """`snapshot.manifest.committed` 证明 project 属于本租户（S44-10）。"""
        project_id = data.get("project_id")
        if project_id is None:
            return
        self.conn.execute(
            "INSERT INTO project_scope (tenant_id, project_id, event_id,"
            " updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (tenant_id, project_id)"
            " DO UPDATE SET event_id=excluded.event_id,"
            " updated_at=excluded.updated_at",
            (tenant_id, str(project_id), event_id, _now()))

    def learn_accepted(self, tenant_id: str, event_id: str, data: dict) -> None:
        """K1 的「被接受」事实。**没有事件 = 没看见事实**，不是 0。"""
        manifest_id = data.get("manifest_id")
        node_id = data.get("node_id")
        revision = _int(data.get("accepted_revision"))
        if not manifest_id or not node_id or revision is None:
            return
        self.conn.execute(
            "INSERT INTO accepted_projection (tenant_id, manifest_id, node_id,"
            " episode_id, accepted_revision, event_id, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (tenant_id, manifest_id,"
            " node_id) DO UPDATE SET episode_id=excluded.episode_id,"
            " accepted_revision=excluded.accepted_revision,"
            " event_id=excluded.event_id, updated_at=excluded.updated_at",
            (tenant_id, str(manifest_id), str(node_id),
             data.get("episode_id"), revision, event_id, _now()))


# ------------------------------------------------------- 指标计算（§4.1）


SOURCE_ATTR = {
    "cost": "cost_source", "qc": "qc_source", "review": "review_source",
    "delivery": "delivery_source", "job": "job_source",
    "accepted": "accepted_source", "evaluator": "evaluator_source",
}


UPSTREAM_NAME = {
    "cost": "S23", "qc": "S35-S37", "review": "S40", "delivery": "S43",
    "job": "S26", "accepted": "S08", "evaluator": "S21",
}


# 事件 → (源代号, 水位表里的键)
EVENT_SOURCE = {
    EVENT_LEDGER: ("s23", "s23"),
    EVENT_JOB_STATE: ("s26", "s26"),
    EVENT_TECHNICAL_QC: ("s35", "s35"),
    EVENT_SEMANTIC_QC: ("s36", "s36"),
    EVENT_ALIGNMENT_QC: ("s37", "s37"),
    EVENT_SUBTITLE_READY: ("s38", "s38"),
    EVENT_REVIEW_DECISION: ("s40", "s40"),
    EVENT_SNAPSHOT_COMMITTED: ("s08", "s08"),
    EVENT_PACKAGE_PUBLISHED: ("s43", "s43"),
}


COHORT_SOURCES = ("s23", "s26", "s35", "s40", "s08", "s43")


def _excluded_skeleton() -> dict:
    """`excluded` 永远非空（§2.4、E38-S44-05）。

    即使一项都没排除，四个零也在：字段的**存在**才是「没有悄悄过滤」的
    结构性保障，缺字段才是问题。
    """
    return {"failed_runs": 0, "abandoned": 0, "insufficient_data": 0,
            "failed_items": 0}


def _metric_view(row: dict, *, superseded_definition: bool = False) -> dict:
    return {
        "snapshot_id": row["snapshot_id"],
        "metric_key": row["metric_key"],
        "metric_version": row["metric_version"],
        "cohort_id": row["cohort_id"],
        "value": row["value"],
        "unit": row["unit"],
        "n": row["n"],
        "distribution": _loads(row["distribution"], {}),
        "ci95": _loads(row["ci95"], None),
        "excluded": _loads(row["excluded"], _excluded_skeleton()),
        "mixed_versions": bool(row["mixed_versions"]),
        "stale_sources": _loads(row["stale_sources"], {}),
        "source_watermarks": _loads(row["source_watermarks"], {}),
        "allocation_policy_version": row["allocation_policy_version"] or None,
        "low_confidence": bool(row["low_confidence"]),
        "computed_at": row["computed_at"],
        "numerator_value": row["numerator_value"],
        "denominator_value": row["denominator_value"],
        "superseded_definition": superseded_definition,
    }


# 名字 → 方法名。放在类体之后，避免类定义时前向引用。
COLLECTORS = {
    "usable_rate": "collect_usable_rate",
    "first_pass_shot_rate": "collect_first_pass_shot_rate",
    "cost_per_usable_minute": "collect_cost_per_usable_minute",
    "delivery_lead_time": "collect_delivery_lead_time",
    "rework_rate": "collect_rework_rate",
    "human_intervention_rate": "collect_human_intervention_rate",
    "memory_correctness": "collect_memory_correctness",
    "customer_roi": "collect_customer_roi",
}


# ---- 导出口径（§6.5、C-S44-04）-----------------------------------------
SNAPSHOT_CSV_COLUMNS = (
    "snapshot_id", "metric_key", "metric_version", "cohort_id", "value",
    "unit", "n", "numerator_value", "denominator_value", "ci95_low",
    "ci95_high", "method", "allocation_policy_version", "low_confidence",
    "mixed_versions", "source_watermarks", "excluded", "note",
)


def _csv_row(cells: list[str]) -> str:
    escaped = []
    for cell in cells:
        text = "" if cell is None else str(cell)
        if any(char in text for char in ',"\n'):
            text = '"' + text.replace('"', '""') + '"'
        escaped.append(text)
    return ",".join(escaped)


def _sample_items(blind_review_id: str, items: list[dict],
                  count: int) -> list[dict]:
    """确定性抽样：按 `sha256(blind_review_id + item_id)` 排序取前 N。

    用随机数会让「重跑同一次开评」得到不同样本，导出就无法复算（§2.2
    的精神同样适用于盲评）。
    """
    ordered = sorted(items, key=lambda item: _sha256_hex(
        f"{blind_review_id}|{item['item_id']}"))
    return ordered[:count]
