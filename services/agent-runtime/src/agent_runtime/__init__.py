"""S19 agent-runtime：有界推理执行。

对外只有 `AgentRuntimeService` 与几个纯函数指纹。`deps` 是上游适配（全部注入
式，未配置即 503），`schema` 是 DDL 与缺口登记。

一句话边界见 `service.py` 的模块文档。
"""

from .service import (  # noqa: F401
    EVENT_TASK_COMPLETED,
    MAX_CORRECTION_ROUND,
    REQUIRED_CONTEXT,
    SOURCE,
    TOPIC_TASK,
    AgentRuntimeService,
    ServiceError,
    Transaction,
    definition_content_hash,
    result_content_hash,
)
from .schema import (  # noqa: F401
    ALL_STATES,
    CONTRACT_GAPS,
    FAILED_REASONS,
    ID_PREFIX,
    LIMIT_FIELDS,
    PERSPECTIVES,
    ROLE_MAX_SCOPE,
    ROLES,
    SCHEMA,
    STOP_REASONS,
    TERMINAL_STATES,
    TOOL_NAMES,
)

__all__ = [
    "ALL_STATES", "AgentRuntimeService", "CONTRACT_GAPS", "EVENT_TASK_COMPLETED",
    "FAILED_REASONS", "ID_PREFIX", "LIMIT_FIELDS", "MAX_CORRECTION_ROUND",
    "PERSPECTIVES", "REQUIRED_CONTEXT", "ROLES", "ROLE_MAX_SCOPE", "SCHEMA",
    "SOURCE", "STOP_REASONS", "ServiceError", "TERMINAL_STATES", "TOOL_NAMES",
    "TOPIC_TASK", "Transaction", "definition_content_hash",
    "result_content_hash",
]
