"""S29 · 图像与关键帧生产 —— 内核的公开面。

只导出"调用方真会用到"的东西。`_` 开头的留在包内：它们是实现细节，导出会让
下游有机会绕过 `ImageProductionService` 的事务边界直接碰 SQL。

导出三个纯函数是有意的：`content_hash` / `input_fingerprint` /
`canonical_transition` 都不碰库、不看时间、不读环境。S30/S39 可以在自己的
进程里算一遍与 S29 对拍——让它们各自实现一份"参考组合怎么寻址"，比让
哈希算法出现两个版本要危险得多。
"""

from __future__ import annotations

from .schema import (
    CONTRACT_GAPS,
    ID_PREFIX,
    JOB_STATES,
    MAX_EDIT_WINDOW_FRAMES,
    NODE_KINDS,
    REFERENCE_ROLES,
    RETRYABLE_STATES,
    ROLES_NOT_APPLICABLE_FOR_IMAGE,
    SCHEMA,
    TERMINAL_STATES,
    TRANSITIONS,
)

from .service import (
    EVENT_IMAGE_READY,
    EXECUTION_CONTEXT_REQUIRED,
    ImageProductionService,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC_IMAGE,
    ServiceError,
    Transaction,
    canonical_transition,
    content_hash,
    input_fingerprint,
)

__all__ = [
    # 服务
    "ImageProductionService",
    "ServiceError",
    "Transaction",
    # 纯函数
    "content_hash",
    "input_fingerprint",
    "canonical_transition",
    # schema / 词汇
    "SCHEMA",
    "CONTRACT_GAPS",
    "JOB_STATES",
    "TERMINAL_STATES",
    "RETRYABLE_STATES",
    "TRANSITIONS",
    "NODE_KINDS",
    "REFERENCE_ROLES",
    "ROLES_NOT_APPLICABLE_FOR_IMAGE",
    "MAX_EDIT_WINDOW_FRAMES",
    "ID_PREFIX",
    # 事件
    "SOURCE",
    "SCHEMA_VERSION",
    "EVENT_IMAGE_READY",
    "TOPIC_IMAGE",
    # 校验用
    "EXECUTION_CONTEXT_REQUIRED",
]
