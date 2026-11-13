"""S45 · 审计与证据保全。方案：`plans/S45-audit.md`。

公开面只有内核、错误类型、事务、schema 常量与缺口登记。脱敏扫描器的实现
（`redact_leaf`）也导出，因为它是 §2.5 的可测试单元——但它不是"可以替换成
不脱敏版本"的钩子，契约要求它必须在哈希之前跑。
"""

from .deps import build_authorization_source, build_redaction_scanner
from .schema import (CONTRACT_GAPS, ID_PREFIX, PERMISSIONS, READ_PERMISSION,
                     REGISTERED_PERMISSIONS, SCHEMA, WRITE_PERMISSIONS)
from .service import (CLASS_A, CONSUMER_INGRESS, DEFAULT_POLICY,
                      EVENT_EXPORT_READY, GENESIS_HASH, MAX_DETAILS_BYTES,
                      OUTCOMES, RAW_CONTENT_KEYS, SCANNER_UNAVAILABLE_MARKER,
                      SEGMENT_IDLE_SECONDS, SEGMENT_MAX_AGE_SECONDS,
                      SEGMENT_MAX_RECORDS, SOURCE, TOPIC_AUDIT, AuditService,
                      ServiceError, Transaction, redact_leaf)

__all__ = [
    "AuditService",
    "ServiceError",
    "Transaction",
    "CONTRACT_GAPS",
    "SCHEMA",
    "ID_PREFIX",
    "PERMISSIONS",
    "READ_PERMISSION",
    "WRITE_PERMISSIONS",
    "REGISTERED_PERMISSIONS",
    "SOURCE",
    "TOPIC_AUDIT",
    "EVENT_EXPORT_READY",
    "CONSUMER_INGRESS",
    "GENESIS_HASH",
    "OUTCOMES",
    "RAW_CONTENT_KEYS",
    "DEFAULT_POLICY",
    "MAX_DETAILS_BYTES",
    "SEGMENT_MAX_RECORDS",
    "SEGMENT_MAX_AGE_SECONDS",
    "SEGMENT_IDLE_SECONDS",
    "CLASS_A",
    "SCANNER_UNAVAILABLE_MARKER",
    "redact_leaf",
    "build_authorization_source",
    "build_redaction_scanner",
]
