"""S32 · 口型生产（lipsync）。

公开面只有三样：DDL、内核、注入用的上游回调。上游一律注入，本包不 import
任何别的服务——`AGENTS.md` 的「绝对不能做」第一条。

本服务不持有媒体、不持有 accepted pointer、不直连供应商。它持有的每一条
事实都是"做过一次什么、为什么现在还能用/不能用了"。
"""

from .schema import CONTRACT_GAPS, JOB_STATES, SCHEMA, TERMINAL_STATES
from .service import (
    LipsyncService,
    ServiceError,
    frame_range,
    input_fingerprint,
    visibility_grade,
)

__all__ = [
    "CONTRACT_GAPS",
    "JOB_STATES",
    "SCHEMA",
    "TERMINAL_STATES",
    "LipsyncService",
    "ServiceError",
    "frame_range",
    "input_fingerprint",
    "visibility_grade",
]
