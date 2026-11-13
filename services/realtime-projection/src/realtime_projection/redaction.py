"""`PROJECTION_SECRET_SCAN`：写 change_log 与开 SSE 之前的就地脱敏。

规则表是 `contracts/redaction-policy.v2.json`，本模块**只做执行，不做定义**。
四条与被抄来的"惯例"相反、但契约明写的事实：

1. **命中从不拒绝、不丢弃、不进 DLQ。** 兜底是 `redact_and_store`，对所有
   producer 一致。在 S47 这里若丢事件，就是方案 §3.3 明令禁止的"silently
   introduced gap"，而且 SSE 客户端无从知道少了一条（见该文件 `fallback`）。
2. **替换发生在叶子内部，不改变外层 JSON 形状。** 只换命中的子串，同一字符串
   里其余的字符保留；对象的键保留；数组长度保留（`structural_rule`）。
3. **class_a 与 class_b 是仅有的两种形态。** `api_key` / `bearer_token` /
   `provider_credential` → `REDACTED:hmac_<class>:<first16hex>`（可关联）；
   `signature_secret` / `private_key_material` / `connection_string` →
   `REDACTED:<class>`（不可关联）。没有第三种。
4. **脱敏在哈希之前。** 这里的 payload 只进 change_log 与 SSE，不参与任何
   内容哈希；但顺序仍按契约写死，避免将来有人把 payload 拿去哈希时拿到明文。

`k_redact_scope` 按契约应在 KMS、按租户与类分别持钥。仓库里没有 KMS，所以
**未配置时用进程内派生密钥**（`hmac_scope_key`），并把这一条登记为缺口 C47-22
（见 `schema.CONTRACT_GAPS`）。它仍然满足"不可逆 + 同值同 token"的可观察性质；
不满足的只有"跨实例/跨轮换稳定"——那需要真 KMS，不在这里假装。

**误报豁免**：`api_key` 的通用 32–64 位分支必然命中内容哈希与无横线 UUID。
契约要求对"被外层 schema 标成 content_hash / id / revision 的字段"先豁免。
本模块用 `_HASHY_KEYS` 做这个豁免，且**只在整值恰好就是那个 hash 形态时**生效
（值里嵌着别的文本时照常扫描），这是契约 false_positive_note 的字面要求。
"""

from __future__ import annotations

import hashlib
import hmac
import re

REDACTED_PREFIX = "REDACTED:"

# 契约 replacement_action 的分组。顺序即优先级：一个串只归一个类。
CLASS_A = ("api_key", "bearer_token", "provider_credential")
CLASS_B = ("signature_secret", "private_key_material", "connection_string")

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
    "request_hash", "combination_key", "id", "revision", "watermark",
    "traceparent", "correlation_id", "causation_id", "tenant_id",
    "aggregate_id", "subscription_id", "run_id", "artifact_id", "node_id",
})


def hmac_scope_key(tenant_id: str, class_id: str,
                   secret: bytes | None = None) -> bytes:
    """派生出 `k_redact_scope`。

    契约要求在 KMS、按租户与类分别持钥。没有 KMS 时用进程密钥派生子钥：
    `HMAC(secret, tenant || ':' || class)`。**进程密钥必须稳定**，否则同一
    秘密在两个实例上得到两个 token，`class_a` 的可关联性就没了——所以调用方
    （`Service`）把它放在实例上，而不是每次现算。
    """
    root = secret if secret is not None else b"s47-local-redaction-key"
    return hmac.new(root, f"{tenant_id}:{class_id}".encode("utf-8"),
                    hashlib.sha256).digest()


def _neutralise(class_id: str, matched: str, tenant_id: str,
                secret: bytes | None) -> str:
    if class_id in CLASS_B:
        return REDACTED_PREFIX + class_id
    digest = hmac.new(hmac_scope_key(tenant_id, class_id, secret),
                      matched.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{REDACTED_PREFIX}hmac_{class_id}:{digest[:16]}"


def redact_text(text: str, tenant_id: str, *,
                secret: bytes | None = None,
                hash_exempt: bool = False) -> tuple[str, int]:
    """替换一个字符串里的全部命中。返回 `(新串, 命中数)`。

    同一串里多个类都命中时按 `CLASS_A + CLASS_B` 的顺序依次替换；已替换的
    `REDACTED:` 片段不会再被后面的模式命中（它的形态不含长随机串）。
    """
    if not isinstance(text, str) or not text:
        return text, 0
    if hash_exempt and _looks_like_pure_hash(text):
        return text, 0
    hits = 0
    out = text
    for class_id in CLASS_A + CLASS_B:
        def _sub(match, _cid=class_id):
            nonlocal hits
            hits += 1
            return _neutralise(_cid, match.group(0), tenant_id, secret)
        out = PATTERNS[class_id].sub(_sub, out)
    return out, hits


_HASH_VALUE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _looks_like_pure_hash(text: str) -> bool:
    """整值就是一段长 token（没有任何分隔/标签）——在 hash/id 键上豁免通用分支。

    只对"整个值就是一个 token"成立；`"Bearer eyJ..."` 或
    `"password=hunter2"` 这类带上下文的串**不豁免**。
    """
    return bool(_HASH_VALUE.match(text))


def redact_value(value, tenant_id: str, *, secret: bytes | None = None):
    """递归脱敏任意 JSON 值，保持形状（键保留、数组长度保留）。

    契约 `structural_rule`：只有叶子被换，外层结构不变；数组不删元素。
    没有键上下文，所以**不豁免** hash 形态的整值——宁可多脱敏一个
    content_hash，也不让明文漏出去。要带键豁免请用 `redact_payload`。
    """
    if isinstance(value, dict):
        return {key: redact_value(item, tenant_id, secret=secret)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item, tenant_id, secret=secret) for item in value]
    if isinstance(value, str):
        cleaned, _ = redact_text(value, tenant_id, secret=secret)
        return cleaned
    return value


def redact_payload(payload, tenant_id: str, *, secret: bytes | None = None):
    """按上游键名豁免地脱敏一个完整 payload。返回 `(值, 命中数)`。"""
    hits = 0

    def _walk(value, key: str | None):
        nonlocal hits
        if isinstance(value, dict):
            return {k: _walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v, key) for v in value]
        if isinstance(value, str):
            cleaned, count = redact_text(
                value, tenant_id, secret=secret,
                hash_exempt=key in _HASHY_KEYS)
            hits += count
            return cleaned
        return value

    return _walk(payload, None), hits
