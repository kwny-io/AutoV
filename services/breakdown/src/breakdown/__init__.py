"""S05 breakdown：场次分场的唯一权威。

对外只有 `BreakdownService`、`ServiceError`、`Transaction`，以及三个纯函数
（`line_set_digest` / `breakdown_content_hash` / `content_fingerprint`）。
`deps` 是上游适配，`schema` 是 DDL 与缺口登记。

**`accepted` 不在这里。** 某一版分场被接受是 S08 manifest revision 的
accepted pointer（plan §3.5.1）。任何从这里找 `accepted` 的调用方都找错了服务。

**`scene_id` 是身份，`scene_order` 是显示。** 两者分表存放；本服务的任何
路径都不把序号写进 `scene`（plan §1.1）。
"""

from .schema import (  # noqa: F401
    AUTHORITATIVE_SOURCE,
    CONTRACT_GAPS,
    ID_PREFIX,
    LOCAL_EVENT_TYPES,
    SCHEMA,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC,
)
from .service import (  # noqa: F401
    EVENT_REVISION_CREATED,
    BreakdownService,
    ServiceError,
    Transaction,
    breakdown_content_hash,
    content_fingerprint,
    line_set_digest,
    scene_content_projection,
)

__all__ = [
    "AUTHORITATIVE_SOURCE", "BreakdownService", "CONTRACT_GAPS",
    "EVENT_REVISION_CREATED", "ID_PREFIX", "LOCAL_EVENT_TYPES", "SCHEMA",
    "SCHEMA_VERSION", "SOURCE", "ServiceError", "TOPIC", "Transaction",
    "breakdown_content_hash", "content_fingerprint", "line_set_digest",
    "scene_content_projection",
]
