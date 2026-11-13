"""S46 developer-integration：外部开发者的统一命令面、回执与 Webhook。

对外的契约面：`DeveloperIntegrationService`（受理 / 转发 / 对账 / 查询 /
凭据 / 订阅 / 投递）、`COMMANDS`（外部可见命令目录，代码常量）、
`CONTRACT_GAPS` 与 `SCHEMA`，以及 `redact_payload`（`INGRESS_SECRET_SCAN`）。

本服务**不是**任何业务事实的权威：没有 run 状态、没有余额、没有审批状态。
`integration_command_receipt` 是**回执**，`upstream_state` 是带时间戳的旧快照。
本服务**不**生成、不推进 `execution_fence` / `node_epoch`（它们分别属于
run/branch 与单个 `logical_node`），只原样透传调用方在 `context_ref` 里给的值。
"""

from .redaction import (  # noqa: F401
    CLASSES,
    CLASS_A,
    CLASS_B,
    PATTERNS,
    REDACTED_PREFIX,
    hmac_scope_key,
    redact_payload,
    redact_text,
)
from .schema import (  # noqa: F401
    CONTRACT_GAPS,
    EVENT_COMMAND_ACCEPTED,
    EVENT_FORWARD_UNKNOWN,
    EVENT_TENANT_PARAM_IGNORED,
    ID_PREFIX,
    MAX_KEYS_ACTIVE,
    MAX_KEYS_LIVE,
    RECEIPT_STATES,
    SCHEMA,
    SCHEMA_VERSION,
    SCOPES,
    SOURCE,
    TOPIC,
)
from .service import (  # noqa: F401
    COMMANDS,
    AuthContext,
    Command,
    DeveloperIntegrationService,
    ForwardOutcome,
    LocalTokenBuckets,
    RateLimitBackendError,
    ServiceError,
    Transaction,
    UnconfiguredUpstream,
    UpstreamCall,
    UpstreamPort,
    UpstreamUnavailable,
    UpstreamUnknown,
    build_dependencies,
    sign_webhook,
    verify_webhook,
)

__all__ = [
    "CLASSES", "CLASS_A", "CLASS_B", "PATTERNS", "REDACTED_PREFIX",
    "hmac_scope_key", "redact_payload", "redact_text",
    "CONTRACT_GAPS", "EVENT_COMMAND_ACCEPTED", "EVENT_FORWARD_UNKNOWN",
    "EVENT_TENANT_PARAM_IGNORED", "ID_PREFIX", "MAX_KEYS_ACTIVE",
    "MAX_KEYS_LIVE", "RECEIPT_STATES", "SCHEMA", "SCHEMA_VERSION", "SCOPES",
    "SOURCE", "TOPIC",
    "COMMANDS", "AuthContext", "Command", "DeveloperIntegrationService",
    "ForwardOutcome", "LocalTokenBuckets", "RateLimitBackendError",
    "ServiceError", "Transaction", "UnconfiguredUpstream", "UpstreamCall",
    "UpstreamPort", "UpstreamUnavailable", "UpstreamUnknown",
    "build_dependencies", "sign_webhook", "verify_webhook",
]
