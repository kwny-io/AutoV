"""S25 model-routing：路由估价。

对外只有 `RoutingService` 与三个纯函数模块（`requirements` / `filters` /
`scoring`）。`deps` 是上游适配，`schema` 是 DDL 与缺口登记。
"""

from .service import (  # noqa: F401
    EVENT_ROUTE_SELECTED,
    MAX_CANDIDATES,
    QUOTE_TTL_SECONDS,
    REQUIRED_CONTEXT,
    SOURCE,
    TOPIC_ROUTE,
    RoutingService,
    ServiceError,
    Transaction,
    content_hash,
)
from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA  # noqa: F401

__all__ = [
    "CONTRACT_GAPS", "EVENT_ROUTE_SELECTED", "ID_PREFIX", "MAX_CANDIDATES",
    "QUOTE_TTL_SECONDS", "REQUIRED_CONTEXT", "RoutingService", "SCHEMA",
    "SOURCE", "ServiceError", "TOPIC_ROUTE", "Transaction", "content_hash",
]
