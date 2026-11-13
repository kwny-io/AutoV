"""`RequirementSet` 的派生。**纯函数**：只读 `generation_command`，不看库、不看时间。

方案 §2 给出这张"需求来自哪里"的表。派生本身要小心的不是字段搬运，是两条
能让人花错钱的判断：

1. **对白时长不能估（§2.2）。** 命令声明了本镜有对白，而 `dialogue_duration_ms`
   读不到时，整条路由是 `INFEASIBLE / DIALOGUE_DURATION_UNKNOWN`——不是"按镜头
   时长凑一个"。这是"不删台词"的执行点：S25 没有任何权限把 4.2 秒对白塞进
   4 秒镜头，也没有权限假装自己知道对白有多长。
2. **`declares_dialogue` 要宁可多报。** 判错的代价不对称：把有对白的镜头判成
   没有，会生成一条静音镜头让人后期返工；把没对白的判成有，只会多要一个字段。
   所以策略里任何"原生出声"的档位都算有对白，`modality=speech` 也算。

三个字段名不是契约给的，是方案散文里点出来的（`dialogue_duration_ms`、
`tail_pause_ms`、`criticality`），记录在 `schema.CONTRACT_GAPS` 里。它们从
`input` 读，因为 `input` 在契约里就是 `{"type":"object"}`、没有任何声明——
**这不是本服务的自由度，是契约的缺口**：填错一个字段名不会有人拦。
"""

from __future__ import annotations

import math

# §2.2 与 §3 行 122：这些档位由模型自己出声，时长必须容得下对白。
NATIVE_DIALOGUE_STRATEGIES = ("native", "native_locked_voice")

# §3 行 122 与 §6.2：需要锁定音色。
VOICE_LOCK_STRATEGIES = ("native_locked_voice",)

# 参考 URL TTL 的"安全余量"（§2.3 的 `safety`）。方案只写了这个词，没给数。
# 取 60s：小于一次供应商轮询间隔的一半，不至于让每个候选都因为它被拒。
REFERENCE_TTL_SAFETY_SECONDS = 60


class RequirementError(Exception):
    """派生阶段就能确定的"整条路由不可行"。带上原因码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _as_int(value, default=None):
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_references(refs) -> list[dict]:
    """按角色折叠成 count，字典序。与 S24 §2.4 的规范化同语义。

    这里**不**求 `combination_key`：那份规范化在 S24 里，且方案 §2.4 要求
    跨服务逐字节一致（C24-1 未决）。S25 只把计数形状交出去，key 让 S24 算，
    免得两份实现漂移。这个函数存在的唯一理由是：没有它，`references` 的
    计数会随上游传入顺序抖动，而顺序不该影响可行性。
    """
    counts: dict[str, int] = {}
    for ref in refs or []:
        role = str((ref or {}).get("role") or "").strip()
        count = _as_int((ref or {}).get("count"), 0)
        if not role or count is None or count <= 0:
            continue
        counts[role] = counts.get(role, 0) + count
    return [{"role": role, "count": counts[role]} for role in sorted(counts)]


def _reference_url_ttl(refs) -> int | None:
    """取所有参考里**最小**的签名 URL 剩余 TTL。

    取最小不是保守估计，是唯一正确的读法：一条参考先过期，整次派单就拉不到
    素材。`None` 表示上游根本没给 TTL——那这一条硬过滤无从判断（见 §2.3 缺口）。
    """
    ttls = []
    for ref in refs or []:
        value = _as_int((ref or {}).get("url_ttl_seconds"))
        if value is not None:
            ttls.append(value)
    return min(ttls) if ttls else None


def derive(command: dict, tenant_id: str) -> dict:
    """§2.1 的 `RequirementSet`。`command` 是 `generation-command.v2` 实例。"""
    if not isinstance(command, dict):
        raise RequirementError("INVALID_GENERATION_COMMAND",
                               "generation_command must be an object")
    context = command.get("context")
    if not isinstance(context, dict):
        raise RequirementError("INVALID_GENERATION_COMMAND",
                               "generation_command.context is required")
    text_input = command.get("input")
    if not isinstance(text_input, dict):
        raise RequirementError("INVALID_GENERATION_COMMAND",
                               "generation_command.input must be an object")

    modality = command.get("modality")
    if not isinstance(modality, str) or not modality:
        raise RequirementError("INVALID_GENERATION_COMMAND",
                               "modality is required")

    strategy = text_input.get("audio_strategy") or "silent"
    # 声音需求（§2.1 的 `audio` 段）。`silent_then_tts` 不是"没有对白"，
    # 是"对白在后期由 TTS 加"——所以它仍需要音色与时长约束。
    needs_native_dialogue = strategy in NATIVE_DIALOGUE_STRATEGIES
    needs_voice_lock = strategy in VOICE_LOCK_STRATEGIES
    declares_dialogue = bool(
        needs_native_dialogue
        or modality == "speech"
        or text_input.get("dialogue_present") is True
        or (isinstance(text_input.get("text"), str) and text_input["text"].strip()))

    dialogue_ms = _as_int(text_input.get("dialogue_duration_ms"))
    if dialogue_ms is None and isinstance(command.get("template_ref"), dict):
        # §2.2：也从 `template_ref` 读。方案把两个来源并列写出来，
        # 说明它自己也不确定哪个是权威——两处都没有才算"未知"。
        dialogue_ms = _as_int(command["template_ref"].get("dialogue_duration_ms"))
    # §2.2 / E04：**不估算**。这里只标记"判不了"，由 service 落一条
    # `INFEASIBLE / DIALOGUE_DURATION_UNKNOWN` 决策。不抛 422：那样不会写
    # 任何行、也不发事件，S21 会一直等一个不会到来的成功（T25-26 的要求）。
    dialogue_unknown = bool(declares_dialogue
                            and (dialogue_ms is None or dialogue_ms <= 0))

    tail_pause_ms = _as_int(text_input.get("tail_pause_ms"), 0) or 0
    # 可用生成时长下界：对白取整秒 + 尾留白。两个都向上取，宁可要长一档。
    min_duration_seconds = None
    if dialogue_ms:
        min_duration_seconds = (math.ceil(dialogue_ms / 1000)
                                + math.ceil(tail_pause_ms / 1000))

    references = _normalize_references(text_input.get("references"))
    resolution = text_input.get("resolution")
    if isinstance(resolution, dict):
        resolution = {"width": _as_int(resolution.get("width")),
                      "height": _as_int(resolution.get("height"))}
    else:
        resolution = None

    policy_ref = context.get("policy_ref")
    if not isinstance(policy_ref, dict) or not policy_ref.get("id"):
        raise RequirementError("INVALID_GENERATION_COMMAND",
                               "context.policy_ref is required")

    return {
        "modality": modality,
        "mode": text_input.get("mode"),
        "required_capabilities": list(text_input.get("required_capabilities") or []),
        "references": references,
        "audio": {"strategy": strategy,
                  "needs_native_dialogue": needs_native_dialogue,
                  "needs_voice_lock": needs_voice_lock},
        "duration_seconds": text_input.get("duration_seconds"),
        "min_duration_seconds": min_duration_seconds,
        "dialogue_duration_ms": dialogue_ms,
        "dialogue_duration_unknown": dialogue_unknown,
        "tail_pause_ms": tail_pause_ms,
        "aspect_ratio": text_input.get("aspect_ratio"),
        "resolution": resolution,
        "deadline": command.get("deadline"),
        "tenant_id": tenant_id,
        "home_region": context.get("home_region"),
        "policy_ref": {"service_id": policy_ref.get("service_id", "S02"),
                       "id": policy_ref.get("id"),
                       "revision": policy_ref.get("revision"),
                       "content_hash": policy_ref.get("content_hash")},
        "budget_scope_id": command.get("budget_scope_id"),
        "lora": bool(text_input.get("lora")),
        # 只有显式写 non_critical 才允许 degraded 通过（§3.1）。缺省即关键。
        "criticality": text_input.get("criticality"),
        "reference_url_ttl_seconds": _reference_url_ttl(text_input.get("references")),
        "allow_trim": bool(text_input.get("allow_trim", False)),
    }


def required_reference_ttl(*, p90_queue_wait_s, p90_generation_s) -> int:
    """§2.3：`p90_queue_wait + p90_generation + 安全余量`。

    延迟未知时**不返回一个猜测值**——调用方据此跳过这一条硬过滤并把
    `latency_known=false` 写进评分，而不是拿 0 去比。
    """
    return int(math.ceil(p90_queue_wait_s) + math.ceil(p90_generation_s)
               + REFERENCE_TTL_SAFETY_SECONDS)
