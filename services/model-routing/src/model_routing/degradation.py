"""§6 降级选项。S25 **生成候选并标注**，不执行降级（§6 行 334）。

两条规则能让人花错钱或毁掉创作意图，都在数据层兜住：

- `silent_degradation=true` 的选项**只能作为提议**（§5.3）。要"既自动执行
  又静默降级"的选项在数据库层就不允许存在（`degradation_option` 的 CHECK）。
  这里生成时守住同一条：凡是 `auto_executable=true` 的，`silent_degradation`
  一律强制为 false。
- **`full_dialogue_required` 时"缩时长"必然 `auto_executable=false`**（§6.2
  行 369：缩时长 = 删台词）。方案 §11 T25-07 的断言就是这一条。

`degradation_level` 的语义（§6.3）：0 只 L1、1 加 L2、2 加 L3a 并列出 L3b/L4、
3 只出选项不做自动选。`unattended` 也不自动走 L3a，除非
`policy.allow_spec_adjust=true`——由调用方把 level 映射好，这里只按 level 过滤。
"""

from __future__ import annotations

LEVELS = (
    "pool", "model_swap", "spec_adjust", "split_shot", "layered", "human_review",
)

# §6.3：每个 level 允许**自动**出来的档位。
_AUTO_BY_LEVEL = {
    0: (),                       # 只 L1：同一快照换池，不算降级选项
    1: ("model_swap",),
    2: ("model_swap", "spec_adjust"),
    3: (),
}


def _option(kind: str, option_id: str, *, description: str,
            auto_executable: bool, silent_degradation: bool,
            requires=None, blocked_reason=None, cost_delta_micro=None,
            quality_risk=None, payload=None) -> dict:
    """构造一个选项，并在出口处强制那条不变式。"""
    if auto_executable:
        silent_degradation = False
    if silent_degradation and not blocked_reason:
        # 静默降级必须带"为什么不能自动执行"。没有理由的静默降级 = 无声降级。
        blocked_reason = "requires human confirmation by policy"
    return {
        "option_id": option_id, "kind": kind, "description": description,
        "auto_executable": bool(auto_executable),
        "silent_degradation": bool(silent_degradation),
        "requires": list(requires or []),
        "blocked_reason": blocked_reason,
        "cost_delta_micro": cost_delta_micro,
        "quality_risk": quality_risk,
        "payload": payload or {},
    }


def build(*, feasible: bool, rejection_codes: list[str], guard: dict,
          policy_flags: dict, degradation_level: int,
          identity_locked: bool, min_delivery_spec: dict | None,
          dialogue_exceeds_duration: bool = False) -> list[dict]:
    """按 §5.3 / §6 口径生成 `degradation_options`。

    只生成**有依据**的选项：没有 `dialogue_exceeds_duration` 的拒绝理由时
    不给"拆镜"，没有身份锁定时不给"换模型会降身份"的提议。空列表是合法且
    正确的答案——凭空造一个降级选项会让 S21 去执行一个没必要的动作。
    """
    codes = set(rejection_codes)
    options: list[dict] = []

    # L1：同一快照的另一个健康池。`combination_key` 不变，总是安全，
    # 所以只要还有别的池就可以提。这里不查库（S25 不拥有池的运行时健康），
    # 只给出"试试换个池"这个动作。
    if "ACCOUNT_UNAVAILABLE" in codes:
        options.append(_option(
            "pool", "opt_pool_swap",
            description="Try another healthy account pool on the same model snapshot",
            auto_executable=True, silent_degradation=False,
            quality_risk="none",
            payload={"combination_key_unchanged": True}))

    # L2：换模型。两种触发——被能力/时长挡掉，或排名第一身份保持偏低。
    if codes & {"CAPABILITY_MISSING", "MODE_UNSUPPORTED",
                "DIALOGUE_EXCEEDS_DURATION", "AUDIO_STRATEGY_UNSUPPORTED"}:
        options.append(_option(
            "model_swap", "opt_model_swap",
            description="Use another model snapshot with the same hard capabilities",
            # 身份锁定 + 换模型：§6.2 表"禁止自动"。I 分量可能掉到节点阈值以下。
            auto_executable=not identity_locked,
            silent_degradation=identity_locked,
            blocked_reason=("identity is locked for this node; a model swap may "
                            "drop identity preservation below threshold"
                            if identity_locked else None),
            requires=["hard capabilities identical"],
            quality_risk="medium" if identity_locked else "low",
            payload={"identity_locked": bool(identity_locked)}))

    # L2 的另一面：原生不出声的模型要换音色（§6.2 行 367）。
    if "VOICE_LOCK_UNSUPPORTED" in codes:
        options.append(_option(
            "model_swap", "opt_model_swap_native_voice",
            description="Switch to a model that can speak natively, so the "
                        "locked voice does not have to be re-created",
            auto_executable=False, silent_degradation=True,
            blocked_reason="voice lock may not survive a voice change",
            quality_risk="medium",
            payload={"voice_lock_must_be_preserved": True}))

    # L3a：规格调整。缩时长在 `full_dialogue_required` 下禁止自动（会删台词）。
    if dialogue_exceeds_duration:
        full_dialogue = bool(guard.get("full_dialogue_required"))
        options.append(_option(
            "spec_adjust", "opt_shorten_duration",
            description="Shorten the shot so it fits a smaller duration tier",
            auto_executable=False, silent_degradation=True,
            blocked_reason=("this shot's dialogue must be kept in full; "
                            "shortening the shot would drop lines"
                            if full_dialogue else
                            "shortening changes the creative spec"),
            requires=["S07 change plan"],
            quality_risk="high" if full_dialogue else "medium",
            payload={"dialogue_ms": guard.get("dialogue_duration_ms")}))

    if codes & {"DIALOGUE_EXCEEDS_DURATION", "DURATION_UNSUPPORTED"}:
        # L3b：拆镜 / 分层。属于创作变更，必须走 S06+S07（§6.1 行 343）。
        options.append(_option(
            "split_shot", "opt_split_shot",
            description="Split the shot into two shorter segments and cut the "
                        "dialogue by sentence",
            auto_executable=False, silent_degradation=False,
            blocked_reason="splitting is a creative change, not an execution change",
            requires=["S06 shot split", "S07 change plan"],
            quality_risk="low",
            payload={}))
        options.append(_option(
            "layered", "opt_layered_composite",
            description="Composite two generations (base motion + lip-synced "
                        "dialogue) into one shot",
            auto_executable=False, silent_degradation=False,
            blocked_reason="layered compositing needs S06/S07 approval",
            requires=["S06", "S07"],
            quality_risk="medium",
            payload={}))

    # L3a：降分辨率。低于 `min_delivery_spec` 的一律禁止自动（§6.2 行 368）。
    if codes & {"RESOLUTION_UNSUPPORTED"}:
        options.append(_option(
            "spec_adjust", "opt_lower_resolution",
            description="Lower the output resolution to fit the model's limit",
            auto_executable=False,
            silent_degradation=bool(min_delivery_spec),
            blocked_reason=("lowering resolution would fall below the node's "
                            "min_delivery_spec" if min_delivery_spec else None),
            quality_risk="medium" if min_delivery_spec else "low",
            payload={"min_delivery_spec": min_delivery_spec}))

    # L3a 的准入前提：策略没允许 spec_adjust 时，连提议都不能自动执行。
    if not policy_flags.get("allow_spec_adjust"):
        for option in options:
            if option["kind"] == "spec_adjust" and option["auto_executable"]:
                option["auto_executable"] = False
                option["silent_degradation"] = True
                option["blocked_reason"] = "policy.allow_spec_adjust is false"

    # L4：人审。总是允许，总是可以提。
    options.append(_option(
        "human_review", "opt_human_review",
        description="Ask S40 to decide whether a lower spec is acceptable",
        auto_executable=False, silent_degradation=False,
        quality_risk="none",
        payload={"always_available": True}))

    # 只保留本 level 允许自动的档位；level 3 只出选项，什么都不自动。
    allowed_auto = _AUTO_BY_LEVEL.get(int(degradation_level), ())
    for option in options:
        if option["auto_executable"] and option["kind"] not in allowed_auto:
            option["auto_executable"] = False
            if not option["silent_degradation"]:
                option["silent_degradation"] = False
            option["blocked_reason"] = (
                f"degradation_level={degradation_level} does not allow "
                f"auto-executing {option['kind']}")

    # 选项 id 唯一（DDL 是 (route_id, option_id) 主键）。
    seen: dict[str, int] = {}
    for option in options:
        base = option["option_id"]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            option["option_id"] = f"{base}_{seen[base]}"
    return options
