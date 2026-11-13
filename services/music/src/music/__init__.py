"""S34 · BGM与音乐 —— 内核的公开面。

只导出"调用方真会用到"的东西。`_` 开头的全部留在包内：它们是实现细节，
导出会让下游有机会绕过 `MusicService` 的事务边界直接碰 SQL。

导出 `cut_plan` 与 `content_fingerprint` 是有意的：两者都是**纯函数**，
没有任何副作用、不碰库、不看时间。让 S41/S42/S35 能在自己的进程里算一遍、
和 S34 的结论对拍，比让它们各自实现一份"切点算法"要安全得多。
"""

from __future__ import annotations

from .schema import (
    CONTRACT_GAPS,
    GAP_KINDS,
    INSTRUMENTATIONS,
    JOB_STATES,
    MOODS,
    SCHEMA,
    SELECTION_TIMEOUT_SECONDS,
    TAIL_KINDS,
    TERMINAL_STATES,
)
from .service import (
    EVENT_MUSIC_READY,
    EXECUTION_CONTEXT_REQUIRED,
    MusicService,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC_MUSIC,
    ServiceError,
    Transaction,
    content_fingerprint,
    cut_plan,
)

__all__ = [
    # 服务
    "MusicService",
    "ServiceError",
    "Transaction",
    # 纯函数
    "content_fingerprint",
    "cut_plan",
    # schema / 词汇
    "SCHEMA",
    "CONTRACT_GAPS",
    "JOB_STATES",
    "TERMINAL_STATES",
    "GAP_KINDS",
    "MOODS",
    "INSTRUMENTATIONS",
    "TAIL_KINDS",
    "SELECTION_TIMEOUT_SECONDS",
    # 事件
    "SOURCE",
    "SCHEMA_VERSION",
    "EVENT_MUSIC_READY",
    "TOPIC_MUSIC",
    # 校验用
    "EXECUTION_CONTEXT_REQUIRED",
]
