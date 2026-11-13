"""S41 editorial：片段排列 + EDL + 差分。

对外的契约面只有 `TimelineService`（编排）、四个纯函数模块的导出
（`compute_edl` / `compute_diff` / `compute_completeness` /
`compute_output_plan`），以及 `deps`（上游适配）与 `schema`（DDL 与缺口）。

本服务**不**推进任何接受决策，**不**写 `execution_fence` 或 `node_epoch`
的权威值，也**不是**第二份 EDL 真相——EDL 是 revision 的确定性函数。
"""

from .schema import (  # noqa: F401
    CONTRACT_GAPS,
    DIFF_EXPLOSION_LIMIT,
    EVENT_REVISION_CREATED,
    ID_PREFIX,
    ROUNDING_RULE,
    SCHEMA,
    SOURCE,
    TICK_RATE,
    TOPIC,
)
from .service import (  # noqa: F401
    Transaction,
    TimelineService,
    ServiceError,
    bare_hash,
    compute_completeness,
    compute_content_hash,
    compute_diff,
    compute_edl,
    compute_edl_hash,
    compute_mix_plan,
    compute_output_plan,
    duration_ticks,
    merge_ranges,
    normalize_request,
    round_half_up,
    stable_request_hash,
)

__all__ = [
    "CONTRACT_GAPS", "DIFF_EXPLOSION_LIMIT", "EVENT_REVISION_CREATED",
    "ID_PREFIX", "ROUNDING_RULE", "SCHEMA", "SOURCE", "TICK_RATE", "TOPIC",
    "TimelineService", "Transaction", "ServiceError", "bare_hash",
    "compute_completeness", "compute_content_hash", "compute_diff",
    "compute_edl", "compute_edl_hash", "compute_mix_plan",
    "compute_output_plan", "duration_ticks", "merge_ranges",
    "normalize_request", "round_half_up", "stable_request_hash",
]
