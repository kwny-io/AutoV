"""S36 semantic-qc：视觉语义与时序质检。

对外是 `SemanticQcService`（一个命令一份报告）、`registry`（可加载的检查项
注册表）与 `deps`（S26/S28/S17 适配）。`schema` 是 DDL 与契约缺口登记。
"""

from . import registry  # noqa: F401
from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA  # noqa: F401
from .service import (  # noqa: F401
    EVENT_TYPE,
    EVALUATOR_NAME,
    EVALUATOR_VERSION,
    QC_LAYER,
    SOURCE,
    TOPIC,
    SemanticQcService,
    ServiceError,
    Transaction,
    base32_lower,
    defect_fingerprint,
    derived_key,
)

__all__ = [
    "CONTRACT_GAPS", "EVALUATOR_NAME", "EVALUATOR_VERSION", "EVENT_TYPE",
    "ID_PREFIX", "QC_LAYER", "SCHEMA", "SOURCE", "TOPIC",
    "SemanticQcService", "ServiceError", "Transaction", "base32_lower",
    "defect_fingerprint", "derived_key", "registry",
]
