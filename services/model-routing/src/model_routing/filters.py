"""§3 硬过滤链。**顺序固定，第一个失败的原因成为唯一原因码。**

这段代码的唯一职责是"能不能用"，不含任何价格或质量判断。§3.2 说得最狠：
一个便宜但超预算的候选和贵但够预算的，前者不出现在 `eligible[]` —— 预算上界
只是准入资格，不是排序依据。所以这里返回的是 `(code, detail)` 或 `None`，
**永远不返回分数**。

三个判断上的坑，写在这里免得后来人"修"回去：

- **第 12 条先查对白，再查档位。** 4.2 秒对白 vs 只有 4 秒档，答案是
  `DIALOGUE_EXCEEDS_DURATION` 而不是 `DURATION_UNSUPPORTED`（T25-07）。
  反过来先查档位的话，4 秒的档位"放得下 4 秒请求"，对白就被静默丢了。
- **第 18/17/14 条在数据缺失时是"判不了"，不是"不通过"。** 延迟与幂等能力
  在 S24 的候选体里**故意没有**（C24-5：`provider_idempotency` 填错就是
  重复扣费）。数据缺失时跳过该条并在评分里记 `*_known=false`，而不是把它
  当成通过——这两者的区别会被下游当成"已经查过了"。
- **第 3 条的 degraded 例外只在候选**带着完整载荷**时能判。** S24 现在把
  degraded 无条件放进 `excluded[]`，而 `excluded[]` 只有
  `candidate_key + code + detail`，没有 `snapshot_id`，所以 S25 拿不到它的
  规格去打分。例外逻辑照 §3.1 实现，但真实上游走不到——这是缺口，不是可选项。
"""

from __future__ import annotations

import math

# 顺序即方案 §3 的表序（将来加过滤必须插到表里的位置，不能随手追加）。
FILTER_ORDER = (
    "CROSS_TENANT_DENIED", "POLICY_DENIED", "PROVIDER_DISABLED",
    "PROVIDER_RETIRED", "PROVIDER_DEGRADED", "PROVIDER_VALIDATING",
    "ACCOUNT_UNAVAILABLE", "BYOK_MISMATCH", "RESIDENCY_DENIED",
    "EVIDENCE_EXPIRED", "COMBINATION_UNVERIFIED", "MODE_UNSUPPORTED",
    "CAPABILITY_MISSING", "REFERENCE_SHAPE_UNSUPPORTED",
    "ASPECT_RATIO_UNSUPPORTED", "RESOLUTION_UNSUPPORTED",
    "DURATION_UNSUPPORTED", "DIALOGUE_EXCEEDS_DURATION",
    "AUDIO_STRATEGY_UNSUPPORTED", "VOICE_LOCK_UNSUPPORTED",
    "REFERENCE_URL_TTL_INSUFFICIENT", "NO_VERIFIABLE_UPPER_BOUND",
    "PRICE_EXPIRED", "OVER_BUDGET", "DEADLINE_UNREACHABLE",
    "IDEMPOTENCY_POLICY_UNSATISFIED", "LORA_INCOMPATIBLE",
)

TTS_BRANCHES = ("silent_then_tts", "tts", "native_plus_tts")


def snap_duration(requested_seconds, tiers, trims_to_nearest: bool):
    """向上吸附到档位。返回 `(snapped_seconds | None, reason)`。

    与 S24 §3.3 同语义（`next(t for t in tiers if requested <= t)`）：4 秒请求
    在 `trims_to_nearest=true` 的候选上变成 5 秒，而且**下游必须按 5 秒派**
    （§12 第 4 条）。`tiers` 为空表示该候选不限时长。
    """
    if requested_seconds is None:
        return None, None
    usable = sorted(int(t) for t in (tiers or []) if int(t) > 0)
    if not usable:
        return float(requested_seconds), None
    snapped = next((t for t in usable if float(requested_seconds) <= t), None)
    if snapped is None:
        return None, "no_tier_large_enough"
    if snapped != requested_seconds and not trims_to_nearest:
        # 与 S24 一致：不吸附又不允许裁剪的候选根本派不出来。
        return None, "trims_to_nearest_is_false"
    return float(snapped), None


def _reference_counts(req: dict) -> dict[str, int]:
    return {r["role"]: r["count"] for r in req.get("references") or []}


def mode_detail_for(facts: dict, mode) -> dict | None:
    """该模式在这一快照里的规格。`mode_details` 是 S24 的列表，不是单条。"""
    for detail in facts.get("mode_details") or []:
        if detail.get("mode") == mode:
            return detail
    return None


def _output_spec_for(detail: dict | None, aspect_ratio) -> dict | None:
    if not detail:
        return None
    for spec in detail.get("output_specs") or []:
        if spec.get("aspect_ratio") == aspect_ratio:
            return spec
    return None


def evaluate(req: dict, candidate: dict, facts: dict):
    """按 §3 的表逐条判。返回 `(code, detail)`；通过返回 `(None, {})`。

    `facts` 的全部输入：
      - `policy`: 已解析的策略（flags/thresholds）
      - `validity`: S24 `/capability-validity` 的这一条（可能为 None）
      - `mode_details`: S24 该快照的 `mode_details`（可能为 None）
      - `budget`: S23 的 availability（可能为 None）
      - `telemetry`: 该候选的历史统计（可能为空）
      - `now_epoch` / `deadline_epoch`: 秒
    """
    policy = facts.get("policy") or {}
    flags = policy.get("flags") or {}
    validity = facts.get("validity") or {}
    budget = facts.get("budget") or {}
    telemetry = facts.get("telemetry") or {}
    detail = mode_detail_for(facts, req.get("mode"))

    # 1 租户授权。S24 已经在 `candidates()` 里拦过（同码），这里只在载荷
    # 明说池属于别的租户时再判一次——那说明上游漏了，不能放过。
    pool_tenant = candidate.get("pool_tenant_id")
    if pool_tenant and pool_tenant != req["tenant_id"]:
        return "CROSS_TENANT_DENIED", {"pool_tenant": pool_tenant}

    # 2 策略禁用：kill switch / 模态禁用 / 地区禁用。
    if flags.get("kill_switch"):
        return "POLICY_DENIED", {"reason": "kill_switch"}
    if req["modality"] in (flags.get("disabled_modalities") or []):
        return "POLICY_DENIED", {"reason": "modality_disabled",
                                 "modality": req["modality"]}
    if candidate.get("region") in (flags.get("disabled_regions") or []):
        return "POLICY_DENIED", {"reason": "region_disabled",
                                 "region": candidate.get("region")}

    # 3 渠道生命周期。degraded 可被策略放行（§3.1），retired 永不放行。
    lifecycle = candidate.get("lifecycle")
    if lifecycle == "retired":
        return "PROVIDER_RETIRED", {"lifecycle": lifecycle}
    if lifecycle == "disabled":
        return "PROVIDER_DISABLED", {"lifecycle": lifecycle}
    if lifecycle == "validating":
        return "PROVIDER_VALIDATING", {"lifecycle": lifecycle}
    if lifecycle == "degraded":
        allowed = (flags.get("allow_degraded_for_non_critical")
                   and req.get("criticality") == "non_critical")
        if not allowed:
            return "PROVIDER_DEGRADED", {
                "lifecycle": lifecycle,
                "criticality": req.get("criticality"),
                "allow_degraded_for_non_critical":
                    bool(flags.get("allow_degraded_for_non_critical"))}

    # 4 账户池。validity 的读数是主判据；候选体自己的 max_inflight/rpm 不作为
    # 可用性依据（那是容量，不是健康）。
    if validity:
        if validity.get("usable") is False:
            code = validity.get("code") or ""
            if "CROSS_TENANT" in code:
                return "CROSS_TENANT_DENIED", {"validity_code": code}
            if "BYOK" in code:
                return "BYOK_MISMATCH", {"validity_code": code}
            if code in ("NOT_ACTIVE",):
                return (("PROVIDER_RETIRED" if lifecycle == "retired"
                         else "PROVIDER_DISABLED"), {"validity_code": code})
            if code == "ACCOUNT_UNAVAILABLE":
                return "ACCOUNT_UNAVAILABLE", {"pool_health":
                                               validity.get("pool_health")}
            if code == "EVIDENCE_EXPIRED":
                return "EVIDENCE_EXPIRED", {
                    "valid_until": validity.get("evidence_valid_until")}
            if code == "PRICE_EXPIRED":
                return "PRICE_EXPIRED", {
                    "valid_to": validity.get("price_valid_to")}
            if code == "NO_VERIFIABLE_UPPER_BOUND":
                return "NO_VERIFIABLE_UPPER_BOUND", {"validity_code": code}
            if code == "COMBINATION_NOT_SUPPORTED":
                # S24 对**未验证**组合也是 `usable=false` + 这个 code（它没法
                # 区分"探测判否"和"没探测过"），所以这里不能直接拒——那会让
                # 第 6 条的策略放行（T25-13）永远走不到。真正的裁决留给第 6 条：
                # `UNSUPPORTED` 拒，`UNVERIFIED` 按策略 + `attempt_no` 判。
                if (validity.get("combination") or "UNSUPPORTED") != "UNVERIFIED":
                    return "COMBINATION_UNVERIFIED", {"validity_code": code}
    if candidate.get("scope") == "tenant_byok" and flags.get("allow_byok") is False:
        return "BYOK_MISMATCH", {"reason": "byok_not_allowed_for_request"}

    # 5 数据驻留。要求"候选覆盖请求的全部驻留标签"，不是相交。
    required = set(flags.get("required_residency") or [])
    if required and not required.issubset(set(candidate.get("residency_tags") or [])):
        return "RESIDENCY_DENIED", {
            "required": sorted(required),
            "candidate_tags": sorted(candidate.get("residency_tags") or [])}

    # 6 能力证据 / 组合验证。
    if validity.get("code") == "EVIDENCE_EXPIRED":
        return "EVIDENCE_EXPIRED", {
            "valid_until": validity.get("evidence_valid_until")}
    combination = validity.get("combination")
    if combination == "UNVERIFIED":
        if not (flags.get("allow_unverified_combination")
                and int(facts.get("attempt_no") or 1) == 1):
            # T25-13 的反面：策略不允许，或已经不是第一次尝试（那说明上次这个
            # 组合已经失败过，不能再拿"未验证"当放行理由）。
            return "COMBINATION_UNVERIFIED", {"combination": combination}
    elif combination == "UNSUPPORTED":
        return "COMBINATION_UNVERIFIED", {"combination": combination}

    # 7 模态/模式。
    modes = candidate.get("modes") or []
    if req.get("mode") is not None and req["mode"] not in modes:
        return "MODE_UNSUPPORTED", {"mode": req["mode"], "supported": modes}
    if detail is not None and detail.get("support") in ("unsupported", "field_failed"):
        return "MODE_UNSUPPORTED", {"mode": req["mode"],
                                    "support": detail.get("support")}

    # 8 必需能力键。`probe_verified` 与 `documented` 都算有，其余不算。
    if detail is not None:
        claims = {c["capability_key"]: c.get("support")
                  for c in detail.get("capability_claims") or []}
        for key in req.get("required_capabilities") or []:
            if claims.get(key) not in ("documented", "probe_verified"):
                return f"CAPABILITY_MISSING:{key}", {
                    "capability_key": key, "support": claims.get(key)}

    # 9 参考形状：角色未声明、数量越界、互斥冲突，都归这一个码。
    if detail is not None:
        rules = detail.get("reference_rules") or []
        declared = {r["role"] for r in rules}
        counts = _reference_counts(req)
        for role in sorted(counts):
            if role not in declared:
                return "REFERENCE_SHAPE_UNSUPPORTED", {
                    "rule": "reference.role", "role": role}
        for rule in rules:
            actual = counts.get(rule["role"], 0)
            if not (rule["min_count"] <= actual <= rule["max_count"]):
                return "REFERENCE_SHAPE_UNSUPPORTED", {
                    "rule": "reference.max_count", "role": rule["role"],
                    "actual": actual, "min_count": rule["min_count"],
                    "max_count": rule["max_count"]}
            for peer in rule.get("exclusive_with") or []:
                if actual and counts.get(peer):
                    return "REFERENCE_SHAPE_UNSUPPORTED", {
                        "rule": "reference.exclusive",
                        "roles": [rule["role"], peer]}

    # 10 画幅。`output_spec` 缺失说明该模式没有任何画幅规格，不能放行。
    spec = _output_spec_for(detail, req.get("aspect_ratio"))
    if req.get("aspect_ratio") and detail is not None and spec is None:
        return "ASPECT_RATIO_UNSUPPORTED", {
            "aspect_ratio": req["aspect_ratio"],
            "supported": [s.get("aspect_ratio")
                          for s in detail.get("output_specs") or []]}

    # 11 分辨率上限。
    resolution = req.get("resolution") or {}
    if spec is not None and resolution.get("width"):
        max_w, max_h = spec.get("max_width"), spec.get("max_height")
        if ((max_w and int(resolution["width"]) > int(max_w))
                or (max_h and int(resolution.get("height") or 0) > int(max_h))):
            return "RESOLUTION_UNSUPPORTED", {
                "requested": resolution, "max_width": max_w, "max_height": max_h}

    # 12 时长：**先对白，后档位**（见模块注释）。
    tiers = (spec or {}).get("duration_tiers_s") or []
    trims = bool((spec or {}).get("trims_to_nearest"))
    needed = req.get("min_duration_seconds")
    snapped = None
    if tiers:
        largest = max(int(t) for t in tiers)
        if needed and needed > largest:
            return "DIALOGUE_EXCEEDS_DURATION", {
                "rule": "output.duration_tier", "allowed": sorted(tiers),
                "dialogue_ms": req.get("dialogue_duration_ms"),
                "tail_pause_ms": req.get("tail_pause_ms"),
                "required_seconds": needed}
    if req.get("duration_seconds") is not None:
        snapped, why = snap_duration(req["duration_seconds"], tiers, trims)
        if snapped is None:
            return "DURATION_UNSUPPORTED", {
                "rule": "output.duration_tier", "allowed": sorted(tiers),
                "requested": req["duration_seconds"], "why": why}
        if needed and snapped < needed:
            return "DIALOGUE_EXCEEDS_DURATION", {
                "rule": "output.duration_tier", "allowed": sorted(tiers),
                "dialogue_ms": req.get("dialogue_duration_ms"),
                "required_seconds": needed, "snapped": snapped}

    # 13 声音。缺 `mode_details` 时判不了，跳过（记 `audio_known=false`）。
    audio = req.get("audio") or {}
    if detail is not None:
        if audio.get("needs_native_dialogue") and not detail.get("native_audio"):
            return "AUDIO_STRATEGY_UNSUPPORTED", {
                "strategy": audio.get("strategy"), "native_audio": False}
        if (audio.get("needs_voice_lock") and not detail.get("prompt_audio_lock")
                and (audio.get("strategy") not in TTS_BRANCHES)):
            return "VOICE_LOCK_UNSUPPORTED", {
                "strategy": audio.get("strategy"),
                "prompt_audio_lock": False}

    # 14 参考 URL TTL。三个输入缺一个就判不了——不能拿 0 去比。
    ttl = req.get("reference_url_ttl_seconds")
    queue_p90 = telemetry.get("p90_queue_wait_s")
    gen_p90 = telemetry.get("p90_latency_s")
    if ttl is not None and queue_p90 is not None and gen_p90 is not None:
        required_ttl = (math.ceil(queue_p90) + math.ceil(gen_p90)
                        + (facts.get("reference_ttl_safety_s") or 0))
        if ttl < required_ttl:
            return "REFERENCE_URL_TTL_INSUFFICIENT", {
                "url_ttl_seconds": ttl, "required_ttl_seconds": required_ttl,
                "p90_queue_wait_s": queue_p90, "p90_latency_s": gen_p90}

    # 15 报价。上界缺失或已过期都不能派。
    bound = candidate.get("quote_upper_bound_micro")
    if bound is None:
        return "NO_VERIFIABLE_UPPER_BOUND", {"reason": "no quote"}
    price_valid_to = candidate.get("price_valid_to")
    if price_valid_to and price_valid_to <= facts["now"]:
        return "PRICE_EXPIRED", {"valid_to": price_valid_to}

    # 16 预算。**硬过滤**：读不到余额就不出决策（§9），不跳过。
    available = budget.get("available_micro")
    if available is None:
        # availability 已经由 service 层保证存在，走到这里是契约违约。
        return "OVER_BUDGET", {"reason": "budget_unreadable"}
    if int(bound) > int(available):
        return "OVER_BUDGET", {
            "quote_upper_bound_micro": int(bound),
            "budget_remaining_micro": int(available),
            "budget_id": budget.get("budget_id"),
            # §13：带上读数时刻，事后才能判断是不是一次误杀。
            "read_at": budget.get("_read_at")}

    # 17 期限。延迟未知则跳过并记账（见模块注释）。
    deadline_epoch = facts.get("deadline_epoch")
    if deadline_epoch is not None and gen_p90 is not None:
        margin = (facts.get("ingress_margin_s") or 0)
        slack = deadline_epoch - facts["now_epoch"] - margin
        if math.ceil(gen_p90) > slack:
            return "DEADLINE_UNREACHABLE", {
                "p90_latency_s": gen_p90, "slack_s": slack,
                "ingress_margin_s": margin}

    # 18 供应商幂等能力与策略不符。能力未知时：策略要求就拒，不要求就放行并记账。
    required_idem = flags.get("required_provider_idempotency")
    supplied = candidate.get("provider_idempotency")
    if required_idem in ("required", "client_key_required"):
        if supplied is None:
            return "IDEMPOTENCY_POLICY_UNSATISFIED", {
                "reason": "provider idempotency capability is unknown (S24 "
                          "does not publish it; C24-5)"}
        if supplied == "none":
            return "IDEMPOTENCY_POLICY_UNSATISFIED", {
                "provider_idempotency": supplied}

    # 19 LoRA。未知一律拒：把 LoRA 派给不兼容的基座是既花钱又毁片。
    if req.get("lora"):
        compatible = (detail or {}).get("lora_compatible")
        if not compatible:
            return "LORA_INCOMPATIBLE", {
                "lora": True, "lora_compatible": bool(compatible)}

    return None, {}


def normalized_input(req: dict, detail: dict | None, facts: dict) -> dict:
    """交给 S26/S27 的实际派单规格（§12 第 4 条）。**含吸附后的时长。**

    S26 必须原样派这个，不是原始请求值——报价是按吸附后的档位算的，
    派短了报价就失去意义、S23 的预留也不成立。
    """
    spec = _output_spec_for(detail, req.get("aspect_ratio"))
    tiers = (spec or {}).get("duration_tiers_s") or []
    trims = bool((spec or {}).get("trims_to_nearest"))
    snapped, _why = snap_duration(req.get("duration_seconds"), tiers, trims)
    return {
        "duration_seconds": (int(snapped) if snapped is not None
                             else req.get("duration_seconds")),
        "aspect_ratio": req.get("aspect_ratio"),
        "resolution": req.get("resolution"),
        "references": req.get("references"),
        "mode": req.get("mode"),
        "audio_strategy": (req.get("audio") or {}).get("strategy"),
        # 显式给出，避免 S26 自己再猜一次：这次派单要不要重新签参考 URL。
        "requires_refreshed_reference_urls": bool(
            facts.get("reference_url_ttl_insufficient_but_allowed")),
    }
