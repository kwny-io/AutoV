"""S14 · 道具资产。方案：[`plans/S14-prop-assets.md`](../../../plans/S14-prop-assets.md)。

对外只暴露内核与词汇表。上游依赖一律由 `deps.py` 注入，本包不 import 任何
别的服务（AGENTS.md：不跨库、不互相 import 业务 ORM）。
"""

from .service import (  # noqa: F401
    CANDIDATE_TTL,
    DECISION_KINDS,
    EVENT_PROP_REVISION_ACCEPTED,
    LEGIBILITIES,
    MAX_DIMENSION_CM,
    MAX_PENDING_CANDIDATES,
    MAX_WEIGHT_G,
    PROP_KINDS,
    SCHEMA_VERSION,
    SENSITIVE_READ_SCOPE,
    SOURCE,
    SOURCE_KINDS,
    STATUSES,
    TOPIC_PROP,
    PropAssetsService,
    ServiceError,
    Transaction,
    content_hash,
    text_changed,
    text_fingerprint,
)
from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA  # noqa: F401

__all__ = [
    "CANDIDATE_TTL",
    "CONTRACT_GAPS",
    "DECISION_KINDS",
    "EVENT_PROP_REVISION_ACCEPTED",
    "ID_PREFIX",
    "LEGIBILITIES",
    "MAX_DIMENSION_CM",
    "MAX_PENDING_CANDIDATES",
    "MAX_WEIGHT_G",
    "PROP_KINDS",
    "PropAssetsService",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SENSITIVE_READ_SCOPE",
    "SOURCE",
    "SOURCE_KINDS",
    "STATUSES",
    "ServiceError",
    "TOPIC_PROP",
    "Transaction",
    "content_hash",
    "text_changed",
    "text_fingerprint",
]
