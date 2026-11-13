"""S04 narrative：剧本文字的唯一权威。

对外只有 `NarrativeService` 与两个纯函数（`revision_content_digest` /
`line_set_digest`，必须与 S05/S06/S08 共用同一实现）。
`deps` 是上游适配，`schema` 是 DDL 与缺口登记。

**没有 `accepted` 状态。** 剧本被接受是 S08 某个 manifest revision 的
accepted pointer（plan §3.2 / §3.4）。任何从这里找 `accepted` 的调用方都
找错了服务。
"""

from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA  # noqa: F401
from .service import (  # noqa: F401
    EVENT_LINE_REVISION_CREATED,
    EVENT_REVISION_CREATED,
    EVENT_SCRIPT_WITHDRAWN,
    LOCAL_EVENT_TYPES,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC,
    NarrativeService,
    ServiceError,
    Transaction,
    line_set_digest,
    revision_content_digest,
    structure_digest,
    text_digest,
)

__all__ = [
    "CONTRACT_GAPS", "EVENT_LINE_REVISION_CREATED", "EVENT_REVISION_CREATED",
    "EVENT_SCRIPT_WITHDRAWN", "ID_PREFIX", "LOCAL_EVENT_TYPES",
    "NarrativeService", "SCHEMA", "SCHEMA_VERSION", "SOURCE", "ServiceError",
    "TOPIC", "Transaction", "line_set_digest", "revision_content_digest",
    "structure_digest", "text_digest",
]
