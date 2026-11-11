"""幂等原语：`tenant_id + command_id + request_hash`。

只承载协议，不承载业务。见 platform/README.md 与 docs/04 §1。
"""

from .core import (
    CommandConflict,
    CommandInProgress,
    CommandRecord,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

__all__ = [
    "CommandConflict",
    "CommandInProgress",
    "CommandRecord",
    "SqliteIdempotencyStore",
    "canonical_request_hash",
]
