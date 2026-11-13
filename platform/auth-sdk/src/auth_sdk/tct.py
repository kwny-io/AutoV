"""TenantContextToken：签发与校验。

TCT 把 `tenant_id` 与一个具体的 `request_hash`、一个 `audience`、一个
`revocation_epoch` 绑在一起。业务服务只校验，不签发；签发者只有 S01。

`exp - iat <= 60s`（plans/S01 §1.3 第 4 条）是撤销传播上限的一部分，
不是可调的运维参数。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

TCT_MAX_TTL_SECONDS = 60


class TenantContextError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    principal_id: str
    principal_type: str
    audience: str
    request_hash: str
    scope_kind: str
    scope_id: str
    purpose: str
    permissions: tuple[str, ...]
    revocation_epoch: int
    personal_revocation_epoch: int
    iat: int
    exp: int
    key_id: str
    delegation_id: str | None = None


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _token_payload(ctx: TenantContext) -> dict[str, Any]:
    return {
        "tenant_id": ctx.tenant_id,
        "principal_id": ctx.principal_id,
        "principal_type": ctx.principal_type,
        "audience": ctx.audience,
        "request_hash": ctx.request_hash,
        "scope_kind": ctx.scope_kind,
        "scope_id": ctx.scope_id,
        "purpose": ctx.purpose,
        "permissions": list(ctx.permissions),
        "revocation_epoch": ctx.revocation_epoch,
        "personal_revocation_epoch": ctx.personal_revocation_epoch,
        "iat": ctx.iat,
        "exp": ctx.exp,
        "key_id": ctx.key_id,
        "delegation_id": ctx.delegation_id,
    }


class TokenSigner:
    """HS256 形态的最小实现，用于单机验证；生产走 KMS 私钥，库里只存公钥元数据。

    `secrets` 是 `key_id -> 对称密钥`；生产环境替换为签名/验签分离的实现，
    但 token 的字段与校验顺序不变。
    """

    def __init__(self, secrets: dict[str, bytes]):
        if not secrets:
            raise ValueError("TokenSigner needs at least one key")
        self.secrets = secrets
        self.current_key_id = next(iter(secrets))

    def _sign(self, signing_input: str, key_id: str) -> str:
        mac = hmac.new(self.secrets[key_id], signing_input.encode("ascii"), hashlib.sha256)
        return _b64e(mac.digest())

    def sign(self, ctx: TenantContext) -> str:
        header = _b64e(json.dumps({"alg": "HS256", "kid": ctx.key_id},
                                  sort_keys=True, separators=(",", ":")).encode())
        body = _b64e(json.dumps(_token_payload(ctx), sort_keys=True,
                                separators=(",", ":"), ensure_ascii=False).encode())
        signing_input = f"{header}.{body}"
        return f"{signing_input}.{self._sign(signing_input, ctx.key_id)}"

    def verify(self, token: str, now: int) -> TenantContext:
        try:
            header_b64, body_b64, sig_b64 = token.split(".")
        except ValueError:
            raise TenantContextError("TOKEN_MALFORMED", "token is not three dot-parts")
        header = json.loads(_b64d(header_b64))
        key_id = header.get("kid")
        if key_id not in self.secrets:
            raise TenantContextError("TOKEN_UNKNOWN_KEY", f"unknown kid {key_id!r}")
        expected = self._sign(f"{header_b64}.{body_b64}", key_id)
        # 定长比较，失败不泄漏前缀信息。
        if not hmac.compare_digest(expected, sig_b64):
            raise TenantContextError("TOKEN_SIGNATURE_INVALID", "signature does not verify")

        raw = json.loads(_b64d(body_b64))
        iat, exp = int(raw["iat"]), int(raw["exp"])
        if exp - iat > TCT_MAX_TTL_SECONDS:
            raise TenantContextError(
                "TOKEN_TTL_TOO_LONG",
                f"exp-iat is {exp - iat}s, the maximum is {TCT_MAX_TTL_SECONDS}s",
            )
        if now >= exp:
            raise TenantContextError("TOKEN_EXPIRED", "token has expired")
        if now < iat:
            raise TenantContextError("TOKEN_NOT_YET_VALID", "token was issued in the future")
        return TenantContext(
            tenant_id=raw["tenant_id"],
            principal_id=raw["principal_id"],
            principal_type=raw["principal_type"],
            audience=raw["audience"],
            request_hash=raw["request_hash"],
            scope_kind=raw["scope_kind"],
            scope_id=raw["scope_id"],
            purpose=raw["purpose"],
            permissions=tuple(raw["permissions"]),
            revocation_epoch=int(raw["revocation_epoch"]),
            personal_revocation_epoch=int(raw["personal_revocation_epoch"]),
            iat=iat,
            exp=exp,
            key_id=key_id,
            delegation_id=raw.get("delegation_id"),
        )


def mint_tenant_context(
    signer: TokenSigner,
    *,
    tenant_id: str,
    principal_id: str,
    principal_type: str,
    audience: str,
    request_hash: str,
    scope_kind: str,
    scope_id: str,
    purpose: str,
    permissions: list[str],
    revocation_epoch: int,
    personal_revocation_epoch: int,
    now: int,
    ttl_seconds: int = TCT_MAX_TTL_SECONDS,
    delegation_id: str | None = None,
) -> str:
    if ttl_seconds > TCT_MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds {ttl_seconds} exceeds the {TCT_MAX_TTL_SECONDS}s ceiling"
        )
    ctx = TenantContext(
        tenant_id=tenant_id,
        principal_id=principal_id,
        principal_type=principal_type,
        audience=audience,
        request_hash=request_hash,
        scope_kind=scope_kind,
        scope_id=scope_id,
        purpose=purpose,
        permissions=tuple(sorted(set(permissions))),
        revocation_epoch=revocation_epoch,
        personal_revocation_epoch=personal_revocation_epoch,
        iat=now,
        exp=now + ttl_seconds,
        key_id=signer.current_key_id,
        delegation_id=delegation_id,
    )
    return signer.sign(ctx)


def verify_tenant_context(
    signer: TokenSigner,
    token: str,
    *,
    now: int,
    expected_audience: str,
    request_hash: str | None = None,
    header_tenant_id: str | None = None,
    current_revocation_epoch: int | None = None,
    current_personal_revocation_epoch: int | None = None,
) -> TenantContext:
    """校验顺序对应 plans/S01 §3.1 的判定链；每一步失败都是不同的 code。"""
    ctx = signer.verify(token, now)

    if ctx.audience != expected_audience:
        raise TenantContextError(
            "AUDIENCE_MISMATCH",
            f"token is for {ctx.audience!r}, this service is {expected_audience!r}",
        )
    # header 只用于交叉核对：不一致就拒绝，绝不以 header 为准。
    if header_tenant_id is not None and header_tenant_id != ctx.tenant_id:
        raise TenantContextError(
            "TENANT_CONTEXT_MISMATCH",
            f"header says {header_tenant_id!r}, token says {ctx.tenant_id!r}",
        )
    # TCT 绑定到被授权的那次请求，因此不能被拿去重放另一个 body。
    if request_hash is not None and request_hash != ctx.request_hash:
        raise TenantContextError(
            "REQUEST_HASH_MISMATCH",
            "token was issued for a different request_hash",
        )
    if current_revocation_epoch is not None and ctx.revocation_epoch < current_revocation_epoch:
        raise TenantContextError(
            "TENANT_REVOKED",
            f"token epoch {ctx.revocation_epoch} is behind the current "
            f"{current_revocation_epoch}",
        )
    if (
        current_personal_revocation_epoch is not None
        and ctx.personal_revocation_epoch < current_personal_revocation_epoch
    ):
        raise TenantContextError(
            "PRINCIPAL_REVOKED",
            f"token epoch {ctx.personal_revocation_epoch} is behind the current "
            f"{current_personal_revocation_epoch}",
        )
    return ctx
