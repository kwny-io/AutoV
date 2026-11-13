"""S38 字幕与本地化。

对外的契约面：`SubtitleService`（生成 + 自校验 + 事件投影）、`schema`
（DDL、`SUB.*` 注册表、`CONTRACT_GAPS`）、`deps`（S37/S41/S06/S15/S28 适配）。

本服务**生成并自校验**一条字幕轨，不修复、不进接受指针。它也不是任何东西
的第二权威——原句归 S06、发音词典归 S15、EDL 与输出时间归 S41、渲染字节
归 S28；本库只有版本引用与 `artifact_id + content_hash`。
"""

from . import schema  # noqa: F401
from .schema import (  # noqa: F401
    ABSTAIN_REASON_CODES,
    CHECK_IDS,
    CHECK_STATUSES,
    CONTRACT_GAPS,
    CONSUMER_SCRIPT,
    CONSUMER_TIMELINE,
    CONSUMER_TRANSLATION,
    DECISIONS,
    DEFECT_TYPES,
    EVENT_READY,
    EVENT_SCRIPT,
    EVENT_TIMELINE,
    FORMATS,
    ID_PREFIX,
    QC_LAYER,
    READY_STATES,
    RECOMMENDED_ACTIONS,
    REGISTRY_VERSION,
    SCHEMA,
    SCHEMA_VERSION,
    SEVERITIES,
    SOURCE,
    TOPIC,
    TRACK_STATES,
    TRANSLATION_STATUSES,
    expected_source,
    registry_document,
    select,
    severity_after,
)
from .service import (  # noqa: F401
    DECISION_STATE,
    EVALUATOR_NAME,
    EVALUATOR_VERSION,
    MEDIA_TYPES,
    DEFAULT_PROFILE,
    TAU_LOW,
    SubtitleService,
    ServiceError,
    Transaction,
    build_dimensions,
    build_payload,
    compute_confidence,
    cue_id_for,
    decide,
    defect_fingerprint,
    defect_id_for,
    clip_map,
    evaluate,
    locale_family,
    output_to_source,
    pair_units,
    plan_cues,
    render,
    render_ass,
    render_srt,
    render_vtt,
    report_content_hash,
    report_id_for,
    source_to_output,
    stable_request_hash,
    track_id_for,
)

__all__ = [
    "ABSTAIN_REASON_CODES", "CHECK_IDS", "CHECK_STATUSES", "CONTRACT_GAPS",
    "CONSUMER_SCRIPT", "CONSUMER_TIMELINE", "CONSUMER_TRANSLATION",
    "DECISIONS", "DECISION_STATE", "DEFAULT_PROFILE", "DEFECT_TYPES",
    "EVALUATOR_NAME", "EVALUATOR_VERSION", "EVENT_READY", "EVENT_SCRIPT",
    "EVENT_TIMELINE", "FORMATS", "ID_PREFIX", "MEDIA_TYPES", "QC_LAYER",
    "READY_STATES", "RECOMMENDED_ACTIONS", "REGISTRY_VERSION", "SCHEMA",
    "SCHEMA_VERSION", "SEVERITIES", "SOURCE", "SubtitleService", "TAU_LOW",
    "TOPIC", "TRACK_STATES", "TRANSLATION_STATUSES", "Transaction",
    "ServiceError", "build_dimensions", "build_payload", "clip_map",
    "compute_confidence", "cue_id_for", "decide", "defect_fingerprint",
    "defect_id_for", "evaluate", "expected_source", "locale_family",
    "output_to_source", "pair_units", "plan_cues", "registry_document",
    "render", "render_ass", "render_srt", "render_vtt", "report_content_hash",
    "report_id_for", "schema", "select", "severity_after", "source_to_output",
    "stable_request_hash", "track_id_for",
]
