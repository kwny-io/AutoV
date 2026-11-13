"""S10 asset-catalog 的公开面。

调用方只应 import 这里，不要直接摸 `service` / `deps` 内部。
"""

from .schema import (
    ASSET_TYPES,
    CONTRACT_GAPS,
    DEFAULT_SCOPES,
    ID_PREFIX,
    SCOPES,
    SCHEMA,
)
from .service import (
    EVENT_BINDING_SET_CREATED,
    AssetCatalogService,
    ServiceError,
    Transaction,
    parse_role_key,
    rights_set_hash,
    role_key_for,
)

# `deps` 里有 `from .service import ServiceError`，所以它必须在 service 之后
# import，否则循环导入会在 `deps` 那一侧拿到半初始化的模块。
from .deps import (  # noqa: E402
    build_asset_status_source,
    build_manifest_source,
    build_revision_source,
)

__all__ = [
    "ASSET_TYPES",
    "CONTRACT_GAPS",
    "DEFAULT_SCOPES",
    "EVENT_BINDING_SET_CREATED",
    "ID_PREFIX",
    "SCOPES",
    "SCHEMA",
    "AssetCatalogService",
    "ServiceError",
    "Transaction",
    "build_asset_status_source",
    "build_manifest_source",
    "build_revision_source",
    "parse_role_key",
    "rights_set_hash",
    "role_key_for",
]
