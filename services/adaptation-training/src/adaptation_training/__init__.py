"""S48 · LoRA 与模型适配训练。

权威边界（读这个包之前先记住）：

- 训练**产物**不是放行依据。`CANDIDATE` / `PUBLISHED` 只说明"本服务认为
  这批权重训完了"；能不能用来生成由 S25/S26 在路由时实时判断。
- `base_model_compatibility` 是**训练时刻**的记录。当前兼容性问 S24。
- 授权、钱、permit、字节、评估规则分别属于 S11–S15 / S23 / S22 / S28 / S44。
  本包只有它们的引用与历史快照。

`SCHEMA` 见 `schema.py`；不可调和的契约缺口登记在 `CONTRACT_GAPS`。
"""

from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA
from .service import (
    ACTIVE_STATES,
    ADAPTATION_TYPES,
    BASE_METRICS,
    CANCELLABLE,
    DATASET_MODES,
    DEFAULT_MAX_ITEMS,
    DEFAULT_MIN_ITEMS,
    DEFAULT_MIN_RATIO,
    DEFAULT_THRESHOLDS,
    DEFAULT_TENANT_ACTIVE_CAP,
    EVENT_COMPLETED,
    EXCLUSION_REASONS,
    HEARTBEAT_TIMEOUT,
    MAX_CHECKPOINT_RETRIES,
    MAX_EVAL_ATTEMPTS,
    SOURCE,
    STATES,
    TERMINAL_STATES,
    TOPIC,
    TRANSITIONS,
    VOICE_METRICS,
    AdaptationTrainingService,
    ServiceError,
    Transaction,
    bare_hash,
    evaluate_metrics,
    holdout_items_hash,
    items_hash,
    stable_request_hash,
)

__all__ = [
    "ACTIVE_STATES",
    "ADAPTATION_TYPES",
    "BASE_METRICS",
    "CANCELLABLE",
    "CONTRACT_GAPS",
    "DATASET_MODES",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MIN_ITEMS",
    "DEFAULT_MIN_RATIO",
    "DEFAULT_THRESHOLDS",
    "DEFAULT_TENANT_ACTIVE_CAP",
    "EVENT_COMPLETED",
    "EXCLUSION_REASONS",
    "HEARTBEAT_TIMEOUT",
    "ID_PREFIX",
    "MAX_CHECKPOINT_RETRIES",
    "MAX_EVAL_ATTEMPTS",
    "SCHEMA",
    "SOURCE",
    "STATES",
    "TERMINAL_STATES",
    "TOPIC",
    "TRANSITIONS",
    "VOICE_METRICS",
    "AdaptationTrainingService",
    "ServiceError",
    "Transaction",
    "bare_hash",
    "evaluate_metrics",
    "holdout_items_hash",
    "items_hash",
    "stable_request_hash",
]
