"""S30 · 视频生产 —— 内核的公开面。

只导出"调用方真会用到"的东西。`_` 开头的留在包内：它们是实现细节，导出会让
下游有机会绕过 `VideoProductionService` 的事务边界直接碰 SQL。

导出四个纯函数是有意的：`reference_combination_hash` /
`input_fingerprint` / `canonical_transition` / `solve_trim` 都不碰库、不看时间、
不读环境。S39/S41 可以在自己的进程里算一遍与 S30 对拍——让它们各自实现一份
"裁切到目标时长"的算法，比让同一个 C1–C4 出现两个版本要危险得多。
"""

from __future__ import annotations

from .schema import (
    AUDIO_STRATEGIES,
    CANDIDATE_STATES,
    CONTRACT_GAPS,
    DURATION_POLICIES,
    DURATION_TOLERANCE_MS,
    ID_PREFIX,
    JOB_STATES,
    LEDGER_ACTIONS,
    RETRYABLE_STATES,
    SCHEMA,
    TERMINAL_STATES,
    TRANSITIONS,
    TRIM_MODES,
)

from .service import (
    EVENT_VIDEO_READY,
    EXECUTION_CONTEXT_REQUIRED,
    LOCAL_CLIP_ATTENTION,
    LOCAL_DURATION_MISMATCH,
    LOCAL_JOB_STATE,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC_VIDEO,
    ServiceError,
    Transaction,
    VideoProductionService,
    canonical_transition,
    input_fingerprint,
    reference_combination_hash,
    solve_trim,
)

__all__ = [
    # 服务
    "VideoProductionService",
    "ServiceError",
    "Transaction",
    # 纯函数
    "reference_combination_hash",
    "input_fingerprint",
    "canonical_transition",
    "solve_trim",
    # schema / 词汇
    "SCHEMA",
    "CONTRACT_GAPS",
    "JOB_STATES",
    "TERMINAL_STATES",
    "RETRYABLE_STATES",
    "TRANSITIONS",
    "DURATION_POLICIES",
    "AUDIO_STRATEGIES",
    "TRIM_MODES",
    "LEDGER_ACTIONS",
    "CANDIDATE_STATES",
    "DURATION_TOLERANCE_MS",
    "ID_PREFIX",
    # 事件
    "SOURCE",
    "SCHEMA_VERSION",
    "EVENT_VIDEO_READY",
    "TOPIC_VIDEO",
    "LOCAL_JOB_STATE",
    "LOCAL_CLIP_ATTENTION",
    "LOCAL_DURATION_MISMATCH",
    # 校验用
    "EXECUTION_CONTEXT_REQUIRED",
]
