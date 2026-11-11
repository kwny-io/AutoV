"""S09 依赖溯源与影响分析。见 plans/S09-lineage-impact.md。"""

from .service import LineageImpactService, ServiceError

__all__ = ["LineageImpactService", "ServiceError"]
