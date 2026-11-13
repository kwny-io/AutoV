"""S39 返工与修复决策。

对外的契约面：`RepairPlanningService`（计划、复检、计数、查询）、`schema`
（DDL + `CONTRACT_GAPS`）、`registry`（check 登记表与动作族的**加载器**）、
`deps`（上游适配工厂）。

三条不改的纪律：

1. **只出计划，不执行修复。** 本服务没有一行提交付费任务：动作、代价区间、
   能力要求都只是参考；真正派单在 S26，预算在 S23。
2. **不持有接受指针。** 复检 pass 只把计划标 `resolved` 并交出一句
   「候选可接受」；推进接受指针是 S08 的 CAS。
3. **`execution_fence` 与 `node_epoch` 分列。** 前者是 run/branch 的，
   后者是单个 `logical_node` 的；本服务分别写入、分别读取，不比较、不换算、
   不互相赋值。
"""

from . import registry  # noqa: F401
from .deps import (  # noqa: F401
    REPORT_PATHS,
    build_capability_source,
    build_continuity_source,
    build_policy_source,
    build_report_source,
    build_run_source,
)
from .schema import (  # noqa: F401
    ACTION_COST,
    ACTION_COST_UNDEFINED,
    ACTIONS,
    ALL_LAYERS,
    BLOCKING_WEIGHT,
    CONTRACT_GAPS,
    CONSUMER_GENERATION,
    CONSUMER_REVIEW,
    DEFAULT_AGGREGATION_DEADLINE_SECONDS,
    DEFAULT_MAX_ATTEMPTS_PER_EPISODE,
    DEFAULT_MAX_ATTEMPTS_PER_RUN,
    DEFECT_TYPES,
    DISPOSITIONS,
    ESCALATION_REASONS,
    EVENT_ESCALATED,
    EVENT_PLANNED,
    ID_PREFIX,
    KEYFRAME_MAX_FRAMES,
    LAYER_OF_SERVICE,
    OPTIONAL_LAYERS,
    PLAN_STATES,
    RECHECK_COST,
    REQUIRED_LAYERS,
    REQUIRED_TEMPORAL_CAPABILITY,
    ROUTING_ALGORITHM_VERSION,
    SCHEMA,
    SCHEMA_VERSION,
    SDK_TABLES,
    SEVERITIES,
    SEVERITY_WEIGHT,
    SOURCE,
    TOPIC,
    TOPIC_ESCALATED,
)
from .service import (  # noqa: F401
    QC_REPORT_SCHEMA,
    REPORT_SERVICES,
    SOURCE_PRIORITY,
    RepairPlanningService,
    ServiceError,
    Transaction,
    expected_checks,
    fingerprint_set,
    plan_fingerprint,
    priority_score,
    report_content_hash,
    report_id_for,
    report_problems,
    route,
    stable_request_hash,
    strategy_class,
    validate_schema,
    window_frames,
)

__all__ = [
    "ACTION_COST", "ACTION_COST_UNDEFINED", "ACTIONS", "ALL_LAYERS",
    "BLOCKING_WEIGHT", "CONTRACT_GAPS", "CONSUMER_GENERATION",
    "CONSUMER_REVIEW", "DEFAULT_AGGREGATION_DEADLINE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS_PER_EPISODE", "DEFAULT_MAX_ATTEMPTS_PER_RUN",
    "DEFECT_TYPES", "DISPOSITIONS", "ESCALATION_REASONS", "EVENT_ESCALATED",
    "EVENT_PLANNED", "ID_PREFIX", "KEYFRAME_MAX_FRAMES", "LAYER_OF_SERVICE",
    "OPTIONAL_LAYERS", "PLAN_STATES", "QC_REPORT_SCHEMA", "RECHECK_COST",
    "REPORT_PATHS", "REPORT_SERVICES", "REQUIRED_LAYERS",
    "REQUIRED_TEMPORAL_CAPABILITY", "ROUTING_ALGORITHM_VERSION",
    "RepairPlanningService", "SCHEMA", "SCHEMA_VERSION", "SDK_TABLES",
    "SEVERITIES", "SEVERITY_WEIGHT", "SOURCE", "SOURCE_PRIORITY",
    "ServiceError", "TOPIC", "TOPIC_ESCALATED", "Transaction",
    "build_capability_source", "build_continuity_source",
    "build_policy_source", "build_report_source", "build_run_source",
    "expected_checks", "fingerprint_set", "plan_fingerprint",
    "priority_score", "registry", "report_content_hash", "report_id_for",
    "report_problems", "route", "stable_request_hash", "strategy_class",
    "validate_schema", "window_frames",
]
