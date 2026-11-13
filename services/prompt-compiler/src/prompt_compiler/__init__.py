"""S20 prompt-compiler 的公开面。

调用方只应 import 这里，不要直接摸 `service` / `render` 内部。
"""

from .schema import (
    CONTRACT_GAPS,
    ID_PREFIX,
    MAX_PROBE_SNAPSHOTS,
    MAX_REGRESSION_CASES,
    MODALITIES,
    REGRESSION_VERDICTS,
    RENDER_TYPES,
    SCHEMA,
    SHOT_REQUIRED,
    TEMPLATE_STATES,
    WARNING_CODES,
)
from .service import (
    EVENT_PROMPT_COMPILED,
    SOURCE,
    TOPIC_PROMPT,
    VISUAL_REFERENCE_ROLES,
    PromptCompilerService,
    ServiceError,
    Transaction,
    adapter_content_hash,
    check_capabilities,
    manifest_content_hash,
    order_references,
    resolve_duration,
    select_adapter,
    template_content_hash,
)

# `deps` 里有 `from .service import ServiceError`，所以它必须在 service 之后
# import，否则循环导入会在 `deps` 那一侧拿到半初始化的模块。
from .deps import (  # noqa: E402
    build_alignment_source,
    build_binding_source,
    build_capability_source,
    build_regression_source,
    build_shot_source,
)

__all__ = [
    "CONTRACT_GAPS",
    "EVENT_PROMPT_COMPILED",
    "ID_PREFIX",
    "MAX_PROBE_SNAPSHOTS",
    "MAX_REGRESSION_CASES",
    "MODALITIES",
    "REGRESSION_VERDICTS",
    "RENDER_TYPES",
    "SCHEMA",
    "SHOT_REQUIRED",
    "SOURCE",
    "TEMPLATE_STATES",
    "TOPIC_PROMPT",
    "VISUAL_REFERENCE_ROLES",
    "WARNING_CODES",
    "PromptCompilerService",
    "ServiceError",
    "Transaction",
    "adapter_content_hash",
    "build_alignment_source",
    "build_binding_source",
    "build_capability_source",
    "build_regression_source",
    "build_shot_source",
    "check_capabilities",
    "manifest_content_hash",
    "order_references",
    "resolve_duration",
    "select_adapter",
    "template_content_hash",
]
