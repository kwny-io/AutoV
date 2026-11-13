"""S37 音频理解与对齐质检。

对外的契约面：`AudioAlignmentService`（命令、查询、事件投影）、`schema`
（DDL + `CONTRACT_GAPS`）、`registry`（20 条 AUD.* 检查的可加载数据）、
`deps`（上游适配）。

本服务**测量并报告**：不修复、不改稿、不进接受指针。它也不是任何东西的
第二权威——ASR/时间码/口型/音色结论只写在本库里，资产、脚本、ShotSpec、
音色授权分别属于 S28 / S06-S07 / S15。
"""

from . import registry  # noqa: F401
from .schema import (  # noqa: F401
    ABSTAIN_REASON_CODES,
    AUDIO_SOURCE_MODES,
    CHECK_STATUSES,
    CONTRACT_GAPS,
    CONSUMER_VOICE_AUTH,
    DECISIONS,
    DEFECT_TYPES,
    EVENT_TYPE,
    ID_PREFIX,
    QC_LAYER,
    REASON_CODES,
    RECOMMENDED_ACTIONS,
    SCHEMA,
    SCHEMA_VERSION,
    SEVERITIES,
    SOURCE,
    STATUS_REASON_PAIRS,
    SUPPORTED_ARTIFACT_KINDS,
    TOPIC,
)
from .service import (  # noqa: F401
    ANALYSIS_VERSION,
    AudioAlignmentService,
    ServiceError,
    Transaction,
    asr_cache_key,
    build_dimensions,
    compute_confidence,
    decide,
    defect_fingerprint,
    defect_id_for,
    derived_key,
    report_content_hash,
    report_id_for,
    stable_request_hash,
)

__all__ = [
    "ABSTAIN_REASON_CODES", "ANALYSIS_VERSION", "AUDIO_SOURCE_MODES",
    "AudioAlignmentService", "CHECK_STATUSES", "CONTRACT_GAPS",
    "CONSUMER_VOICE_AUTH", "DECISIONS", "DEFECT_TYPES", "EVENT_TYPE",
    "ID_PREFIX", "QC_LAYER", "REASON_CODES", "RECOMMENDED_ACTIONS", "SCHEMA",
    "SCHEMA_VERSION", "SEVERITIES", "SOURCE", "STATUS_REASON_PAIRS",
    "SUPPORTED_ARTIFACT_KINDS", "ServiceError", "TOPIC", "Transaction",
    "asr_cache_key", "build_dimensions", "compute_confidence", "decide",
    "defect_fingerprint", "defect_id_for", "derived_key", "registry",
    "report_content_hash", "report_id_for", "stable_request_hash",
]
