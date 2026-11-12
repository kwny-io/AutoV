"""S17 continuity：只读的连续性校验。

它对正史的**唯一**接口是注入进来的 `memory_query`（`plans/S17-continuity.md` §4.2）。
S17 不 import S16、不连 S16 的库、不缓存它的事实 —— 每次检查都重新问一遍。
"""

from .service import ContinuityService, ServiceError

__all__ = ["ContinuityService", "ServiceError"]
