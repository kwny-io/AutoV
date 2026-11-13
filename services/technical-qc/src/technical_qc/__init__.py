"""S35 媒体技术质检。方案：`plans/S35-technical-qc.md`。

边界一句话：**测量并报告，不修复。** 判定词表是数据库 CHECK 约束，
"没查成"进不了 `evaluated`，所以它不可能被读成 pass。
"""

from .schema import (
    ABSTAIN_REASON_CODES,
    CHECK_STATUSES,
    CONTRACT_GAPS,
    DECISIONS,
    DEFECT_TYPES,
    REASON_CODES,
    RECOMMENDED_ACTIONS,
    SCHEMA,
    SEVERITIES,
    SUPPORTED_ARTIFACT_KINDS,
)
from .service import (
    EVENT_COMPLETED,
    QC_LAYER,
    TOPIC_COMPLETED,
    ServiceError,
    TechnicalQcService,
    Transaction,
)

__all__ = [
    "ABSTAIN_REASON_CODES",
    "CHECK_STATUSES",
    "CONTRACT_GAPS",
    "DECISIONS",
    "DEFECT_TYPES",
    "EVENT_COMPLETED",
    "QC_LAYER",
    "REASON_CODES",
    "RECOMMENDED_ACTIONS",
    "SCHEMA",
    "SEVERITIES",
    "SUPPORTED_ARTIFACT_KINDS",
    "TOPIC_COMPLETED",
    "ServiceError",
    "TechnicalQcService",
    "Transaction",
]
