"""S11 · 角色身份资产的公开面。

只导出"别处应该用到的"：内核类、错误类型、两个纯函数与它们依赖的常量、
契约缺口清单。schema 的 DDL 也导出，因为测试要拿它断言"某列不存在"。
"""

from __future__ import annotations

from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA
from .service import (
    ALL_VIEWS,
    ALIAS_KINDS,
    CANDIDATE_STATES,
    CANDIDATE_TTL,
    DECISION_KINDS,
    EVENT_CANDIDATE_CREATED,
    EVENT_CHARACTER_REVISION_ACCEPTED,
    FULL_TURNAROUND_VIEWS,
    HARD_IDENTITY_FIELDS,
    IDENTITY_KINDS,
    MAX_PENDING_CANDIDATES,
    REQUIRED_VIEWS,
    RIGHTED_IDENTITY_KINDS,
    SCHEMA_VERSION,
    SOFT_IDENTITY_FIELDS,
    SOURCE,
    SOURCE_KINDS,
    STATUSES,
    TOPIC_CHARACTER,
    CharacterAssetsService,
    ServiceError,
    Transaction,
    classify_drift,
    content_hash,
    identity_feature_hash,
)

__all__ = [
    # 内核
    "CharacterAssetsService",
    "ServiceError",
    "Transaction",
    # 纯函数：可被测试与其他服务复算
    "content_hash",
    "identity_feature_hash",
    "classify_drift",
    # 契约
    "CONTRACT_GAPS",
    "ID_PREFIX",
    "SCHEMA",
    "SOURCE",
    "SCHEMA_VERSION",
    "EVENT_CHARACTER_REVISION_ACCEPTED",
    "EVENT_CANDIDATE_CREATED",
    "TOPIC_CHARACTER",
    # 状态机与枚举（测试与调用方共用同一份定义）
    "CANDIDATE_TTL",
    "MAX_PENDING_CANDIDATES",
    "IDENTITY_KINDS",
    "RIGHTED_IDENTITY_KINDS",
    "STATUSES",
    "SOURCE_KINDS",
    "DECISION_KINDS",
    "CANDIDATE_STATES",
    "ALIAS_KINDS",
    "REQUIRED_VIEWS",
    "FULL_TURNAROUND_VIEWS",
    "ALL_VIEWS",
    "HARD_IDENTITY_FIELDS",
    "SOFT_IDENTITY_FIELDS",
]
