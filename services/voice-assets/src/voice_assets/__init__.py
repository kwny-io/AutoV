"""S15 · 音色与声音授权。

对外只暴露内核与错误类型；`CONTRACT_GAPS` / `SCHEMA` 也在这里转出，供
`GET /health/readiness` 与测试使用。
"""

from __future__ import annotations

from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA
from .service import (
    AUTHORIZATION_PERMISSION,
    CONTENT_PERMISSION,
    DENY_REASONS,
    ERROR_CODES,
    EVENT_AUTHORIZATION_CHANGED,
    REASON_CODES,
    SOURCE,
    TOPIC_VOICE,
    ServiceError,
    Transaction,
    VoiceAssetService,
    effective_state,
    revision_content_hash,
    use_is_authorized,
)

__all__ = [
    "AUTHORIZATION_PERMISSION",
    "CONTRACT_GAPS",
    "CONTENT_PERMISSION",
    "DENY_REASONS",
    "ERROR_CODES",
    "EVENT_AUTHORIZATION_CHANGED",
    "ID_PREFIX",
    "REASON_CODES",
    "SCHEMA",
    "SOURCE",
    "TOPIC_VOICE",
    "ServiceError",
    "Transaction",
    "VoiceAssetService",
    "effective_state",
    "revision_content_hash",
    "use_is_authorized",
]
