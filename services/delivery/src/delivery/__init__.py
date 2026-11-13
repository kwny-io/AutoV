"""S43 · 交付与外发（delivery）—— 内核的公开面。

公开三样东西：DDL 与词汇（`schema`）、内核（`service`）、上游适配
（`deps`）。本包**不 import 任何别的服务**——`AGENTS.md` 的「绝对不能做」
第一条：跨服务只走注入回调。

边界一句话：**S43 只做封包、签名、按实时授权外发、留回执**。
accepted master 归 S08、媒体字节归 S28、策略判定归 S02、审计链归 S45、
成本归 S23；这里只存引用与自己的状态机，因此它不会变成任何东西的第二
权威。`execution_fence` 与 `node_epoch` 分列存放，代码里没有任何比较或
换算，也不推进任何接受指针。
"""

from . import deps  # noqa: F401
from .schema import (  # noqa: F401
    ATTEMPT_STATES,
    CANONICAL_VERSION,
    CHANNEL_KINDS,
    CONSUMERS,
    CONTRACT_GAPS,
    EVENT_CREATED,
    EVENT_FAILED,
    EVENT_PUBLISHED,
    EVENT_SEALED,
    EVENTS,
    GATE_IDS,
    GATE_RESULTS,
    ID_PREFIX,
    ITEM_ROLES,
    PACKAGE_STATES,
    PACKAGE_TYPES,
    SCHEMA,
    SCHEMA_VERSION,
    SOURCE,
    TERMINAL_ATTEMPT_STATES,
    TOPIC,
    VERDICTS,
)
from .service import (  # noqa: F401
    DeliveryService,
    ServiceError,
    Transaction,
    build_manifest,
    canonical_item,
    deliverable_of,
    manifest_hash_for,
    package_hash_for,
    stable_request_hash,
)

__all__ = [
    # 服务
    "DeliveryService",
    "ServiceError",
    "Transaction",
    # 纯函数（下游可以在自己进程里对拍，不算权威）
    "build_manifest",
    "canonical_item",
    "deliverable_of",
    "manifest_hash_for",
    "package_hash_for",
    "stable_request_hash",
    # schema / 词汇
    "SCHEMA",
    "CONTRACT_GAPS",
    "ID_PREFIX",
    "ITEM_ROLES",
    "PACKAGE_TYPES",
    "PACKAGE_STATES",
    "ATTEMPT_STATES",
    "TERMINAL_ATTEMPT_STATES",
    "CHANNEL_KINDS",
    "GATE_IDS",
    "GATE_RESULTS",
    "VERDICTS",
    "CANONICAL_VERSION",
    "CONSUMERS",
    # 事件
    "SOURCE",
    "SCHEMA_VERSION",
    "TOPIC",
    "EVENTS",
    "EVENT_CREATED",
    "EVENT_SEALED",
    "EVENT_PUBLISHED",
    "EVENT_FAILED",
    # 适配
    "deps",
]
