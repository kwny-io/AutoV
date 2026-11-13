"""`combination_key` 的规范化。§2.4 的规则逐条落在这里。

**为什么单独一个模块**：§2.4 要求这个函数「跨服务逐字节一致，写进 contracts/」，
§9 第 2 条要求把它抽到 `platform/` 共享库，S25 与 S26 必须用同一实现。但
`platform/README.md` 写的是「SDK 不许包含任何服务的数据模型或业务规则」。两条
要求互相冲突，写进 `contracts/` 又会动到几份被 `contracts/README.md` 钉住的计数
（那个文件当前禁止编辑）。所以本轮**先留在 S24 内**，把冲突登记为方案 §10 的
C24-1，等 owner 决定归属；不在这里自行选边，也不悄悄复制一份到别处。

规范化输入是**可行性**形状，不是内容：`refs` / `audio_strategy` /
`duration_bucket` / `aspect_ratio` / `resolution_bucket` / `lora` 参与哈希；
提示词、种子、风格等创作参数一律不参与，否则同一个可行组合会因为换了措辞而
变成"未验证"。
"""

from __future__ import annotations

import hashlib
import json
import math
from fractions import Fraction

_SHAPE_KEYS = (
    "mode", "refs", "audio_strategy", "duration_bucket",
    "aspect_ratio", "resolution_bucket", "lora",
)

_AUDIO_STRATEGIES = (
    "silent", "native", "silent_then_tts", "native_plus_tts", "tts",
)


def jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _ceil_to_int(value: float) -> int:
    return int(math.ceil(value))


def duration_bucket(seconds, tiers) -> str:
    """向上取到最近档位；无档位时按 ceil(秒) 成桶（§2.4）。"""
    value = float(seconds)
    usable = sorted(int(t) for t in (tiers or []) if int(t) > 0)
    for tier in usable:
        if value <= tier:
            return f"{tier}s"
    # 超出全部档位，或没有档位：向上取整秒。这里刻意**不**截断到最大档，
    # 因为截断会把一个超长请求的 key 与最大档的 key 合并，等于声称两者可行性
    # 相同——而超长正是最可能不可行的情形。
    return f"{_ceil_to_int(value)}s"


def resolution_bucket(width, height, max_width, max_height) -> str:
    """`{宽}x{高}`，超出允许档时按同一比例下采样（§2.4）。

    用 Fraction 做等比缩放，不用浮点：同一个比例在不同机器上必须给出同一个
    整数，否则跨服务的 key 会对不上。
    """
    w, h = int(width), int(height)
    scale = Fraction(1, 1)
    if max_width:
        scale = min(scale, Fraction(int(max_width), w))
    if max_height:
        scale = min(scale, Fraction(int(max_height), h))
    if scale < 1:
        w = _round_half_up(w * scale)
        h = _round_half_up(h * scale)
    return f"{w}x{h}"


def _round_half_up(value: Fraction) -> int:
    return (value.numerator * 2 + value.denominator) // (value.denominator * 2)


def normalize_refs(refs) -> list[dict]:
    """按角色字典序，同角色折叠成 count（§2.4）。0 计数的角色被丢弃。"""
    counts: dict[str, int] = {}
    for ref in refs or []:
        role = str(ref.get("role") or "").strip()
        count = int(ref.get("count") or 0)
        if not role or count <= 0:
            continue
        counts[role] = counts.get(role, 0) + count
    return [{"role": role, "count": counts[role]} for role in sorted(counts)]


def normalize_shape(*, mode: str, refs, audio_strategy: str, duration_seconds,
                    aspect_ratio: str, width, height, lora: bool,
                    output_spec: dict | None = None) -> dict:
    spec = output_spec or {}
    return {
        "mode": mode,
        "refs": normalize_refs(refs),
        "audio_strategy": audio_strategy,
        "duration_bucket": duration_bucket(
            duration_seconds, spec.get("duration_tiers_s")),
        "aspect_ratio": aspect_ratio,
        "resolution_bucket": resolution_bucket(
            width, height, spec.get("max_width"), spec.get("max_height")),
        "lora": bool(lora),
    }


def combination_key(shape: dict) -> str:
    """对规范化后的形状取 sha256。键集必须恰好是 `_SHAPE_KEYS`。

    多一个键或少一个键都会得到不同的 key，两个服务就会各自认为对方"没验证过"。
    所以这里不是"取现有键"，而是**先证明键集正确**再哈希。
    """
    keys = set(shape)
    if keys != set(_SHAPE_KEYS):
        raise ValueError(
            f"normalized shape keys {sorted(keys)} != {sorted(_SHAPE_KEYS)}")
    return hashlib.sha256(jcs({k: shape[k] for k in _SHAPE_KEYS}).encode()).hexdigest()


def key_for(**kwargs) -> tuple[str, dict]:
    shape = normalize_shape(**kwargs)
    return combination_key(shape), shape
