"""S40 human-review：记录人说了什么，不让它变成已生效。"""

from .schema import CONTRACT_GAPS, SCHEMA
from .service import HumanReviewService, ServiceError

__all__ = ["CONTRACT_GAPS", "SCHEMA", "HumanReviewService", "ServiceError"]
