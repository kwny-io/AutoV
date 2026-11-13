"""S44 · 评测与分析（evaluation-analytics）—— 内核的公开面。

公开三样东西：DDL 与词汇（`schema`）、内核（`service`）、上游适配
（`deps`）。本包**不 import 任何别的服务**——`AGENTS.md` 的「绝对不能做」
第一条：跨服务只走注入回调。

边界一句话：**S44 是读数，不是账本。** 成本原始凭证归 S23、接受指针归
S08、QC 结论归 S35–S37、交付包归 S43、记忆证伪判定归 S17/S40。这里只存
由它们派生的指标、快照与分摊，删掉本服务的整张库，上游的账单、验收与交付
一条都不少。

两条硬纪律：

1. **不编数字。** 上游不可达时返回 `503 DEPENDENCY_UNAVAILABLE`，不是 0，
   也不是上次缓存的值。`n = 0` 时 `value` 必须是 `null`，由 CHECK 约束兜底。
2. **不碰版本坐标。** `execution_fence`（run/branch）与 `node_epoch`
   （单个 `logical_node`）都不是本服务的计算维度：本服务按 cohort +
   源水位做快照，不比较、不换算、不互相赋值。
"""

from . import deps  # noqa: F401
from .schema import (  # noqa: F401
    ALLOCATION_BASES,
    ALLOCATION_DIMENSIONS,
    BLIND_REVIEW_STATES,
    CI_METHODS,
    CONSUMERS,
    CONTRACT_GAPS,
    COST_TYPES,
    DATASET_PURPOSES,
    DIFFICULTIES,
    EVALUATION_STATES,
    EVENTS,
    EVENT_ALIGNMENT_QC,
    EVENT_EVALUATION,
    EVENT_JOB_STATE,
    EVENT_LEDGER,
    EVENT_PACKAGE_PUBLISHED,
    EVENT_PROJECTION,
    EVENT_REVIEW_DECISION,
    EVENT_SEMANTIC_QC,
    EVENT_SNAPSHOT,
    EVENT_SNAPSHOT_COMMITTED,
    EVENT_SUBTITLE_READY,
    EVENT_TECHNICAL_QC,
    EXPERIMENT_STATES,
    ID_PREFIX,
    METRIC_DEFINITIONS,
    METRIC_KEYS,
    METRIC_VERSION,
    PROJECTION_WINDOWS,
    SCHEMA,
    SCHEMA_VERSION,
    SERVICE_NAME,
    SEQUENCE_ONE,
    SOURCE,
    TERMINAL_EVALUATION_STATES,
    TOPIC,
    VOTE_DIMENSIONS,
)
from .service import (  # noqa: F401
    COLLECTORS,
    LOW_ALPHA,
    LOW_CONFIDENCE_N,
    SNAPSHOT_CSV_COLUMNS,
    AllocationEngine,
    EvaluationAnalyticsService,
    ServiceError,
    Transaction,
    bootstrap_interval,
    distribution,
    krippendorff_alpha,
    percentile,
    split_micro,
    stable_request_hash,
    wilson_interval,
)

__all__ = [
    # 服务
    "EvaluationAnalyticsService",
    "AllocationEngine",
    "ServiceError",
    "Transaction",
    # 纯函数（下游可以自己复算，不算第二份权威）
    "bootstrap_interval",
    "distribution",
    "krippendorff_alpha",
    "percentile",
    "split_micro",
    "stable_request_hash",
    "wilson_interval",
    # schema / 词汇
    "SCHEMA",
    "CONTRACT_GAPS",
    "SERVICE_NAME",
    "ID_PREFIX",
    "ALLOCATION_BASES",
    "ALLOCATION_DIMENSIONS",
    "COST_TYPES",
    "METRIC_KEYS",
    "METRIC_DEFINITIONS",
    "METRIC_VERSION",
    "CI_METHODS",
    "EVALUATION_STATES",
    "TERMINAL_EVALUATION_STATES",
    "EXPERIMENT_STATES",
    "BLIND_REVIEW_STATES",
    "DATASET_PURPOSES",
    "DIFFICULTIES",
    "VOTE_DIMENSIONS",
    "PROJECTION_WINDOWS",
    "SEQUENCE_ONE",
    "COLLECTORS",
    "SNAPSHOT_CSV_COLUMNS",
    "LOW_CONFIDENCE_N",
    "LOW_ALPHA",
    "CONSUMERS",
    # 事件
    "SOURCE",
    "SCHEMA_VERSION",
    "TOPIC",
    "EVENTS",
    "EVENT_SNAPSHOT",
    "EVENT_EVALUATION",
    "EVENT_PROJECTION",
    "EVENT_LEDGER",
    "EVENT_JOB_STATE",
    "EVENT_TECHNICAL_QC",
    "EVENT_SEMANTIC_QC",
    "EVENT_ALIGNMENT_QC",
    "EVENT_SUBTITLE_READY",
    "EVENT_REVIEW_DECISION",
    "EVENT_SNAPSHOT_COMMITTED",
    "EVENT_PACKAGE_PUBLISHED",
    # 适配
    "deps",
]
