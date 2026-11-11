"""租户上下文（TCT）与授权判定原语。

**全平台获得 `tenant_id` 的唯一合法路径**：读 S01 签发的、绑定本次请求
`request_hash`、未过期、且 `revocation_epoch` 未推进的 TenantContextToken。
任何「自己解 IdP token / 从 header 取 tenant_id / 缓存到过期之后」都是越权。

见 plans/S01-tenant-access.md §1.3 与 docs/04 §1。
"""

from .tct import (
    TCT_MAX_TTL_SECONDS,
    TenantContext,
    TenantContextError,
    TokenSigner,
    mint_tenant_context,
    verify_tenant_context,
)

__all__ = [
    "TCT_MAX_TTL_SECONDS",
    "TenantContext",
    "TenantContextError",
    "TokenSigner",
    "mint_tenant_context",
    "verify_tenant_context",
]
