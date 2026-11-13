"""S06 storyboard：一集分镜的唯一权威（镜头、在场顺序、对白引用）。

对外只有 `StoryboardService`、`ServiceError`、`Transaction`，以及三个纯函数
（`shot_content_hash` / `shot_fingerprint` / `check_feasibility`）。
`deps` 是上游适配，`schema` 是 DDL 与缺口登记。

**`shot_lineage_id` 在这里铸造，别处没有。** 它跨分支、跨 Run 存在
（`(tenant, series, branch, shot_lineage_id, strategy_class)` 是
`AttemptCounterKey`），所以它既不能由 agent 给、也不能被 patch 改、也不能
在回滚时重建（plan §3.2 / §7.6）。数据库触发器是它唯一不依赖纪律的防线。

**没有 `execution_fence`，没有 `node_epoch`。** 本服务不知道 Run 的版本机制
（plan §3.8）。旧 epoch 的结果晚到，对 S06 而言只是 `parent_revision` 过期，
走 409 `SHOT_REVISION_CONFLICT`。任何在这里写 `node_epoch` 的改动都已经越线。

**不是第二个权威。** 没有接受指针的副本、没有尝试计数、没有成本估算、
不缓存 S04 的当前台词（`text_copy` 只为防漂移与审计存在）、不编译提示词。
"""

from .schema import (  # noqa: F401
    AUTHORITATIVE_SOURCE,
    CONTRACT_GAPS,
    ID_PREFIX,
    LOCAL_EVENT_TYPES,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC,
)
from .service import (  # noqa: F401
    ServiceError,
    StoryboardService,
    Transaction,
    check_feasibility,
    shot_content_hash,
    shot_fingerprint,
)

__all__ = [
    "AUTHORITATIVE_SOURCE", "CONTRACT_GAPS", "ID_PREFIX", "LOCAL_EVENT_TYPES",
    "SCHEMA_SQL", "SCHEMA_VERSION", "SOURCE", "ServiceError",
    "StoryboardService", "TOPIC", "Transaction", "check_feasibility",
    "shot_content_hash", "shot_fingerprint",
]
