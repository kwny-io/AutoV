"""S26 统一生成任务协调。"""

from .schema import SCHEMA, schema_sql
from .service import (
    ADMISSION_STATE,
    PROGRESS_RANK,
    RECON_METHODS_BY_TIER,
    STATES,
    SUBMIT_OUTCOMES,
    TRANSITIONS,
    GenerationJobsService,
    ServiceError,
    admission_id_for,
    intent_id_for,
    intent_id_of,
    parse_candidate_key,
    recon_delay_seconds,
    requirement_hash_for,
    stable_client_key_for,
)

__all__ = [
    "SCHEMA",
    "schema_sql",
    "ADMISSION_STATE",
    "PROGRESS_RANK",
    "RECON_METHODS_BY_TIER",
    "STATES",
    "SUBMIT_OUTCOMES",
    "TRANSITIONS",
    "GenerationJobsService",
    "ServiceError",
    "admission_id_for",
    "intent_id_for",
    "intent_id_of",
    "parse_candidate_key",
    "recon_delay_seconds",
    "requirement_hash_for",
    "stable_client_key_for",
]
