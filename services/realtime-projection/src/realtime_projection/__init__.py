"""S47 realtime-projection：实时状态与通知投影。

**这是缓存，不是权威。** 一条 run 在界面上"现在长什么样"由这里回答，但
"现在到底是什么"永远回源（S08/S21/S23/S15/S16/S40）。所有对外响应都带
`as_of` 与 `projection_watermark`，`__hints.authority` 机械地列出每个字段该去
哪问。

对外只有 `RealtimeProjectionService` 一个类，加三个纯模块：`redaction`（按
`PROJECTION_SECRET_SCAN` 脱敏）、`deps`（两个只读 GET 上游）、`schema`（DDL 与
缺口登记）。`deps` 里没有 POST/PUT/PATCH/DELETE 的包装函数——"绝不写回"是
结构性的，不是靠自觉（§1.4 / §8.3）。
"""

from .schema import (  # noqa: F401
    CLASS_A_EVENTS,
    CONTRACT_GAPS,
    ID_PREFIX,
    SCHEMA,
)
from .service import (  # noqa: F401
    EVENT_CURSOR_ADVANCED,
    MERGE_WINDOWS_MS,
    SOURCE,
    TOPIC_CURSOR,
    RealtimeProjectionService,
    ServiceError,
    Transaction,
)

__all__ = [
    "CLASS_A_EVENTS", "CONTRACT_GAPS", "EVENT_CURSOR_ADVANCED", "ID_PREFIX",
    "MERGE_WINDOWS_MS", "RealtimeProjectionService", "SCHEMA", "SOURCE",
    "ServiceError", "TOPIC_CURSOR", "Transaction",
]
