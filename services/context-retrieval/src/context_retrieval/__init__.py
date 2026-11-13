"""S18 context-retrieval：只读的上下文检索与组装。"""

from .schema import CONTRACT_GAPS, SCHEMA
from .service import ContextRetrievalService, ServiceError

__all__ = ["CONTRACT_GAPS", "SCHEMA", "ContextRetrievalService", "ServiceError"]
