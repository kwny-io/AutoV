"""S07 对话修改与变更计划。

对外只有 `ChangeManagementService` 与两个纯函数（`plan_hash_of` /
`parse_instruction`）。`deps` 是上游适配，`schema` 是 DDL 与缺口登记。

`plan_hash_of` 与 `saga_command_id` 之所以在公开面上：它们是持久化语义的
一部分——客户端要能对同一份计划算出同一个 hash，崩溃恢复要靠同一个
commit command_id 重试（S07-19）。
"""

from .service import (  # noqa: F401
    CONSUMERS,
    DEFAULT_TTL_SECONDS,
    EVENT_APPLIED,
    LOCAL_PLANNED,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    SAGA_STEPS,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC_CHANGE,
    ChangeManagementService,
    ServiceError,
    Transaction,
    parse_instruction,
    plan_hash_of,
    saga_command_id,
)
from .schema import (  # noqa: F401
    CLARIFICATION_KINDS,
    CONTRACT_GAPS,
    ID_PREFIX,
    PLAN_STATES,
    PLAN_TRANSITIONS,
    SAGA_STATES,
    SCHEMA,
    STEP_STATES,
    TERMINAL_PLAN_STATES,
)

__all__ = [
    "CLARIFICATION_KINDS", "CONTRACT_GAPS", "CONSUMERS",
    "DEFAULT_TTL_SECONDS", "EVENT_APPLIED", "ID_PREFIX", "LOCAL_PLANNED",
    "MAX_TTL_SECONDS", "MIN_TTL_SECONDS", "PLAN_STATES", "PLAN_TRANSITIONS",
    "SAGA_STATES", "SAGA_STEPS", "SCHEMA", "SCHEMA_VERSION", "SOURCE",
    "STEP_STATES", "TERMINAL_PLAN_STATES", "TOPIC_CHANGE",
    "ChangeManagementService", "ServiceError", "Transaction",
    "parse_instruction", "plan_hash_of", "saga_command_id",
]
