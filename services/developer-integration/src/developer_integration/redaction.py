"""`INGRESS_SECRET_SCAN`：外部命令 `params` 进入哈希与转发之前的就地脱敏。

规则表是 `contracts/redaction-policy.v2.json`，本模块**只做执行，不做定义**。
与本仓 S45/S47 的执行点共用同一份契约，因此三处对「什么算 secret」与
「换成什么形态」不会各自演化。

四条与被抄来的"惯例"相反、但契约明写的事实：

1. **命中从不拒绝、不丢弃。** 兜底是 `redact_and_store`；契约
   `fallback.why_not_reject` 的理由就是这里：若 4xx，客户端可以用一串密钥
   形状的字符串把自己的调用记录抹掉。
2. **替换发生在叶子内部，不改变外层 JSON 形状。** 只换命中的子串；对象的
   键保留；数组长度保留（`structural_rule`）。
3. **class_a 与 class_b 是仅有的两种形态。** `api_key` / `bearer_token` /
   `provider_credential` → `REDACTED:hmac_<class>:<first16hex>`（可关联）；
   `signature_secret` / `private_key_material` / `connection_string` →
   `REDACTED:<class>`（不可关联）。
4. **脱敏在哈希之前。** 方案 §2.2：`params_sha256` 是幂等比较的依据，先哈希
   后替换会让合法重试变成 `REQUEST_HASH_MISMATCH`。

`k_redact_scope` 按契约应在 KMS、按租户与类分别持钥。仓库里没有 KMS，所以
**未配置时用进程内派生密钥**，并把这一条登记为缺口（C46-KMS）。它仍满足
「不可逆 + 同值同 token」的可观察性质。

**误报豁免**：`api_key` 的通用 32–64 位分支必然命中内容哈希与无横线 UUID。
契约要求对「被外层 schema 标成 content_hash / id / revision 的字段」先豁免。
本模块用 `_HASHY_KEYS`，且**只在整值恰好就是那个 hash 形态时**生效。
"""

from __future__ import annotations

import hashlib
import hmac
import re

REDACTED_PREFIX = "REDACTED:"

# 契约 replacement_action 的分组。顺序即优先级：一个串只归一个类。
CLASS_A = ("api_key", "bearer_token", "provider_credential")
CLASS_B = ("signature_secret", "private_key_material", "connection_string")
CLASSES = CLASS_A + CLASS_B

# 参考模式（契约 `must_match`）。实现不得比它更宽松；这里逐字照抄。
PATTERNS: dict[str, re.Pattern[str]] = {
    "api_key": re.compile(
        r"(?i)(\bak_(live|test)_[A-Za-z0-9]{16,}|\bsk-[A-Za-z0-9_-]{16,}"
        r"|(?<![A-Za-z0-9])[A-Za-z0-9]{32,64}(?![A-Za-z0-9]))"),
    "bearer_token": re.compile(
        r"(?i)(\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"
        r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"),
    "signature_secret": re.compile(
        r"(?i)(\bwhsec_[A-Za-z0-9+/=_-]{8,}"
        r"|X-Drama-Signature\s*[:=]\s*t=\d+\s*,\s*v1=[0-9a-f]{16,}"
        r"|\bv1=[0-9a-f]{32,})"),
    "provider_credential": re.compile(
        r"(?i)(\b[A-Z0-9]{16,}\b(?=.*(?:region|endpoint|supplier|provider))"
        r"|(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"
        r"|\bclient_secret\b\s*[:=]\s*\S{8,})"),
    "private_key_material": re.compile(
        r"(?is)(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?"
        r"-----END [A-Z ]*PRIVATE KEY-----"
        r"|\b(?:password|passwd|pwd)\b\s*[:=]\s*\S{4,})"),
    "connection_string": re.compile(
        r"(?i)([a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"
        r"|(?<![A-Za-z])[?&](X-Amz-Signature|X-Goog-Signature|sig|token)"
        r"=[A-Za-z0-9%._~+/=-]{16,})"),
}

# 被外层 schema 标成 hash/id 的键：通用 32–64 位分支对它们豁免（契约要求）。
_HASHY_KEYS = frozenset({
    "content_hash", "manifest_hash", "hash", "sha256", "event_id",
    "request_hash", "params_sha256", "combination_key", "id", "revision",
    "watermark", "traceparent", "correlation_id", "causation_id", "tenant_id",
    "aggregate_id", "subscription_id", "run_id", "artifact_id", "node_id",
    "operation_id", "receipt_id", "command_id", "api_client_id", "delivery_id",
})

_HASH_VALUE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def hmac_scope_key(tenant_id: str, class_id: str, secret: bytes) -> bytes:
    """派生 `k_redact_scope`：`HMAC(secret, tenant || ':' || class)`。

    **进程密钥必须稳定**，否则同一秘密在两个实例上得到两个 token，class_a
    的可关联性就没了。
    """
    return hmac.new(secret, f"{tenant_id}:{class_id}".encode("utf-8"),
                    hashlib.sha256).digest()


def _neutralise(class_id: str, matched: str, tenant_id: str,
                secret: bytes) -> str:
    if class_id in CLASS_B:
        return REDACTED_PREFIX + class_id
    digest = hmac.new(hmac_scope_key(tenant_id, class_id, secret),
                      matched.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{REDACTED_PREFIX}hmac_{class_id}:{digest[:16]}"


def _looks_like_pure_hash(text: str) -> bool:
    return bool(_HASH_VALUE.match(text))


def redact_text(text: str, tenant_id: str, *, secret: bytes,
                hash_exempt: bool = False) -> tuple[str, dict[str, int]]:
    """替换一个字符串里的全部命中。返回 `(新串, {class: 命中数})`。

    同一串里多个类都命中时按 `CLASS_A + CLASS_B` 的顺序依次替换；已替换的
    `REDACTED:` 片段不会再被后面的模式命中（它的形态不含长随机串）。
    """
    counts: dict[str, int] = {}
    if not isinstance(text, str) or not text:
        return text, counts
    if hash_exempt and _looks_like_pure_hash(text):
        return text, counts
    out = text
    for class_id in CLASSES:
        def _sub(match, _cid=class_id):
            counts[_cid] = counts.get(_cid, 0) + 1
            return _neutralise(_cid, match.group(0), tenant_id, secret)
        out = PATTERNS[class_id].sub(_sub, out)
    return out, counts


def redact_payload(payload, tenant_id: str, *, secret: bytes):
    """按上游键名豁免地脱敏一个完整 payload。返回 `(值, {class: 命中数})`。

    没有键上下文时（数组元素）**不豁免** hash 形态的整值——宁可多脱敏一个
    content_hash，也不让明文漏出去。
    """
    totals: dict[str, int] = {}

    def _merge(counts: dict[str, int]) -> None:
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value

    def _walk(value, key: str | None):
        if isinstance(value, dict):
            return {k: _walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v, key) for v in value]
        if isinstance(value, str):
            cleaned, counts = redact_text(value, tenant_id, secret=secret,
                                          hash_exempt=key in _HASHY_KEYS)
            _merge(counts)
            return cleaned
        return value

    return _walk(payload, None), totals
