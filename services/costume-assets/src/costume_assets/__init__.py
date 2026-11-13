"""S12 · 服装资产。方案：[`plans/S12-costume-assets.md`](../../../plans/S12-costume-assets.md)。

对外只暴露内核与词汇表。上游依赖一律由 `deps.py` 注入，本包不 import 任何
别的服务（AGENTS.md：不跨库、不互相 import 业务 ORM）。
"""

from .schema import (  # noqa: F401
    CONTRACT_GAPS,
    COSTUME_KINDS,
    DECISION_KINDS,
    ID_PREFIX,
    REF_ROLES,
    SCHEMA,
    SHEENS,
    SOURCE_KINDS,
    STATUSES,
)
from .service import (  # noqa: F401
    CANDIDATE_TTL,
    EVENT_COSTUME_REVISION_ACCEPTED,
    MAX_PENDING_CANDIDATES,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC_COSTUME,
    CostumeAssetsService,
    ServiceError,
    Transaction,
    content_hash,
    variant_check,
    variant_default,
)

__all__ = [
    "CANDIDATE_TTL",
    "CONTRACT_GAPS",
    "COSTUME_KINDS",
    "CostumeAssetsService",
    "DECISION_KINDS",
    "EVENT_COSTUME_REVISION_ACCEPTED",
    "ID_PREFIX",
    "MAX_PENDING_CANDIDATES",
    "REF_ROLES",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SHEENS",
    "SOURCE",
    "SOURCE_KINDS",
    "STATUSES",
    "ServiceError",
    "TOPIC_COSTUME",
    "Transaction",
    "content_hash",
    "variant_check",
    "variant_default",
]
