"""S13 scene-assets：场景与空间资产（拓扑 / anchor / 光照三层）。

对外只有 `SceneService`，加上两个纯函数模块：

- `geometry` —— 拓扑与足迹的机器校验，不碰库、不碰网络。
- `schema` —— DDL 与 `CONTRACT_GAPS`。

`deps` 是上游适配（S28 / S01），`service` 是内核。
"""

from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA  # noqa: F401
from .service import (  # noqa: F401
    CANDIDATE_TTL_DAYS,
    DECISION_KINDS,
    EVENT_SCENE_REVISION_ACCEPTED,
    LOCATION_CLASSES,
    SCHEMA_VERSION,
    SCENE_KINDS,
    SOURCE,
    SOURCE_KINDS,
    TOPIC_SCENE,
    SceneService,
    ServiceError,
    Transaction,
    revision_content_hash,
    topology_hash,
)

__all__ = [
    "CANDIDATE_TTL_DAYS", "CONTRACT_GAPS", "DECISION_KINDS",
    "EVENT_SCENE_REVISION_ACCEPTED", "ID_PREFIX", "LOCATION_CLASSES",
    "SCHEMA", "SCHEMA_VERSION", "SCENE_KINDS", "SOURCE", "SOURCE_KINDS",
    "SceneService", "ServiceError", "TOPIC_SCENE", "Transaction",
    "revision_content_hash", "topology_hash",
]
