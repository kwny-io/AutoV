"""S31 · 配音生产（speech-production）。

公开面只有三样东西：DDL、内核、以及注入用的上游回调。上游一律注入，
本包不 import 任何别的服务——`AGENTS.md` 的「绝对不能做」第一条。
"""

from .schema import CONTRACT_GAPS, JOB_STATES, SCHEMA, TERMINAL_STATES
from .service import (
    ServiceError,
    SpeechService,
    input_fingerprint,
)

__all__ = [
    "CONTRACT_GAPS",
    "JOB_STATES",
    "SCHEMA",
    "TERMINAL_STATES",
    "ServiceError",
    "SpeechService",
    "input_fingerprint",
]
