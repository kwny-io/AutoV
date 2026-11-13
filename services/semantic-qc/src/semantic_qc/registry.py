"""S36 的检查项注册表与采样策略（方案 §4、§5）。

这里的每一项都必须是**可加载的注册数据**，不是散文。`contracts/quality/
validate_registry.py` 会逐条校验：`layer`/`owner_service` 与 `VIS.` 前缀一致、
`defect_type` 落在 14 值闭集、`default_action` 与 `allowed_actions` 都在该族的
动作链内、`severity_default ≤ severity_max`。测试里直接跑那个校验器，
所以"注册表加载不了"会在 CI 里报，而不是等到 S39 路由时才发现。

三条实现上的取舍：

1. **identity 族不登记 `keyframe_edit`。** 家族链里有它，但它的前置条件是
   `window_scope ∈ {frame, window}` 且 `window_max_frames ≤ 6`（24fps × 6 帧
   = 0.25s）。语义质检按 0.5–2.0s 的窗口出缺陷，登记 `keyframe_edit` 就等于
   承诺一件做不到的事；`temporal_edit` 在链内且没有这个前置条件。
2. **严重度只上提不下压。** `severity_default` 是登记值，模型的结论按
   `max(default, model)` 取并封顶在 `severity_max`（§5 首句）。被注入的 prompt
   无法把 blocker 说成 minor。
3. **连续窗口只影响 minor 的升级，绝不影响 blocker 的成立。** §4 的
   "连续 ≥2 窗口才升级"是为了压 VLM 单帧抖动；如果把它套在 blocker 上，
   "单窗口孤立命中所以不算"就把未知读成了通过——§3.3 与 docs/16 E25 都禁止。

`VIS.SAFE_AREA.TEXT_OVERLAP` 是方案里唯一一个 type 与动作族对不上的检查：
它被判为 `subtitle`，而 subtitle 族的两个动作都改不了烧录进画面的图形。
登记成 `never_auto_repair`（SQC-20），路由必然落人审。
"""

# 采样策略版本：换策略等于换了一个评估器，必须随报告记录（§4 首句）。
SAMPLING_VERSION = "s36-sampling.v1"

# 注册表版本：每次增删改都 +1。
REGISTRY_VERSION = 1

# §4 的窗口规划：镜头类型 → (窗口秒数, 每窗抽帧数)。
# `transition` 不是按秒切窗口，而是以切点为中心，所以单独一条。
SAMPLING_WINDOWS = {
    "dialogue": (1.0, 3),
    "action": (0.5, 6),        # 5 帧 + 运动峰值帧
    "camera_motion": (1.5, 4),
    "vfx": (1.0, 4),
    "establishing": (2.0, 2),
    "transition": (1.0, 4),    # 切点前后各 0.5s；切点帧 + ±3 帧
}
DEFAULT_SHOT_TYPE = "dialogue"

# §5.2：手部像素面积不到 2% 时手部检查判 not_applicable。
# 比例是面积比（bbox 面积 / 画面面积），不是边长比。
HAND_MIN_AREA_RATIO = 0.02

# §6：高风险 = 全部 blocker 项 + VIS.IDENTITY.*。
HIGH_RISK_GROUPS = ("VIS.IDENTITY.",)

# §6 / S35 §2.7 的阈值默认值。真实值由 S02 下发并记进 policy_version；
# 这里是本服务在没有策略快照时的起点。
MIN_COVERAGE_RATIO = 0.90
TAU_LOW = 0.40
TAU_ABSTAIN = 0.35
TAU_DISAGREEMENT = 0.30

# §4：连续窗口平滑阈值。SQC-15：schema 里没有 `promotion_windows` 字段。
PROMOTION_WINDOWS = 2
PROMOTION_RAISES_TO = "major"

# 严重度序，用于"只能上提不能下压"。
SEVERITY_ORDER = {"info": 0, "minor": 1, "major": 2, "blocker": 3}

# `type` → 动作族。与 `contracts/quality/action-families.v2.json` 的
# `families[*].chain` 一致；这里只抄本服务用得到的族，校验时仍读契约文件。
_FAMILY = {
    "identity": ["keyframe_edit", "temporal_edit", "regenerate_video"],
    "anatomy": ["temporal_edit", "regenerate_video"],
    "hands": ["temporal_edit", "regenerate_video"],
    "costume": ["regenerate_video", "change_spec"],
    "prop": ["regenerate_video", "change_spec"],
    "continuity": ["regenerate_video", "change_spec"],
    "camera_adherence": ["regenerate_video", "change_spec"],
    "action_adherence": ["regenerate_video", "change_spec"],
    "temporal": ["temporal_edit", "regenerate_video"],
    "subtitle": ["realign_subtitles", "re_render_subtitles"],
}

# `expected_source.service_id`：S36 的"期望"只能来自这三个上游。
# B5 的反面证明：这里永远不会出现 S16——S36 不得直接消费正史（§2.4 契约 1）。
EXPECTED_SOURCE_S10 = "S10"   # 角色/服装/道具 binding revision
EXPECTED_SOURCE_S06 = "S06"   # ShotSpec
EXPECTED_SOURCE_S17 = "S17"   # expected_state_set


def _check(check_id, defect_type, severity, *, scope, max_frames=None,
           expected=EXPECTED_SOURCE_S06, default_action=None, allowed=None,
           recheck="semantic", promotable=False, never_auto=False,
           no_auto_reason=None, notes=None):
    """构造一条注册项。

    `expected` 只决定缺陷的 `expected_source`：S36 自己知道该拿谁当基准，
    不向模型询问来源。模型给的任何 `expected_source` 都会被丢弃。
    """
    # §6：高风险 = 全部 blocker 项 + VIS.IDENTITY.*。
    # 前缀要在**完整 check_id** 上比：先 rsplit 掉最后一段再 startswith
    # `"VIS.IDENTITY."`，那个点永远匹配不上（"VIS.IDENTITY" 没有尾点），
    # 于是 BODY_PROPORTION / OUTFIT_REVISION / ACCESSORY_* 这些非 blocker 的
    # 身份项全部悄悄退化成单评估器——双评估是这些项的验收要求（E25-S3）。
    is_high = severity == "blocker" or check_id.startswith(HIGH_RISK_GROUPS)
    if allowed is None:
        allowed = list(_FAMILY[defect_type])
        # identity 族的 keyframe_edit 不被任何一条登记项采用（见模块头）。
        # `validate_registry.py` 的规则是"文档里**出现** keyframe_edit 就要求
        # 窗口 ≤ 6 帧"，所以它既不能在 allowed 里，也不能当 default_action，
        # 否则 FACE_DRIFT（48 帧窗口）与 BODY_PROPORTION（shot）都会加载失败。
        allowed = [a for a in allowed if a != "keyframe_edit"] or allowed
    if default_action is None:
        # 取**可用集合**的第一项，不是族链的第一项——两者的差正好是 keyframe_edit。
        default_action = allowed[0]
    entry = {
        "check_id": check_id,
        "layer": "semantic",
        "owner_service": "S36",
        "defect_type": defect_type,
        "default_action": default_action,
        "allowed_actions": allowed,
        "window_scope": scope,
        "severity_default": severity,
        # 上限：登记值之上再一档；blocker 已经到顶。
        "severity_max": {"info": "minor", "minor": "major",
                         "major": "blocker", "blocker": "blocker"}[severity],
        "recheck_scope": recheck,
        "_expected_source": expected,
        "_high_risk": is_high,
        "_promotable": promotable,
    }
    if max_frames is not None:
        entry["window_max_frames"] = max_frames
    if promotable:
        # §4：单窗口孤立命中不升级；连续 ≥2 窗口命中才升一档。
        entry["escalation_rule"] = {
            "metric": "consecutive_windows",
            "comparator": "gte",
            "threshold": PROMOTION_WINDOWS,
            "escalate_to": _FAMILY[defect_type][-1],
        }
    if never_auto:
        entry["never_auto_repair"] = True
        entry["no_auto_reason"] = no_auto_reason
    if notes:
        entry["notes"] = notes
    return entry


CHECKS = [
    # --- §5.1 身份与形象 ---------------------------------------------------
    _check("VIS.IDENTITY.FACE_DRIFT", "identity", "blocker",
           scope="window", max_frames=48, expected=EXPECTED_SOURCE_S10,
           notes="窗口内人脸嵌入与角色参考集距离超阈值；本地 embedding 给信号，VLM 确认"),
    _check("VIS.IDENTITY.FACE_SWAP_WITHIN_SHOT", "identity", "blocker",
           scope="window", max_frames=48, expected=EXPECTED_SOURCE_S10,
           notes="同一 bbox 轨道的人脸嵌入在窗口间跳变（E24-S1）"),
    _check("VIS.IDENTITY.BODY_PROPORTION", "identity", "major",
           scope="shot", expected=EXPECTED_SOURCE_S10,
           notes="体型/身高比例与 CharacterProfile 偏差超阈值"),
    _check("VIS.IDENTITY.OUTFIT_REVISION", "costume", "major",
           scope="shot", expected=EXPECTED_SOURCE_S10,
           notes="服装与 ShotSpec binding 的 costume revision 视觉不符（E24-S2）"),
    _check("VIS.IDENTITY.ACCESSORY_MISSING", "costume", "major",
           scope="shot", expected=EXPECTED_SOURCE_S10),
    _check("VIS.IDENTITY.ACCESSORY_EXTRA", "costume", "minor",
           scope="shot", expected=EXPECTED_SOURCE_S10, promotable=True),

    # --- §5.2 身体与手部 ---------------------------------------------------
    _check("VIS.ANATOMY.LIMB_COUNT", "anatomy", "blocker",
           scope="window", max_frames=48, expected=EXPECTED_SOURCE_S10),
    _check("VIS.ANATOMY.JOINT_ARTIFACT", "anatomy", "major",
           scope="window", max_frames=48, expected=EXPECTED_SOURCE_S10),
    _check("VIS.HANDS.FINGER_DEFORM", "hands", "blocker",
           scope="window", max_frames=48, expected=EXPECTED_SOURCE_S10,
           notes="手部特写窗口手指结构崩坏（E24-S3）；手部面积 < 2% 判 not_applicable"),
    _check("VIS.HANDS.OCCLUSION_ARTIFACT", "hands", "major",
           scope="window", max_frames=48, expected=EXPECTED_SOURCE_S10),
    _check("VIS.HANDS.INTERACTION", "hands", "minor",
           scope="window", max_frames=48, expected=EXPECTED_SOURCE_S10,
           promotable=True),

    # --- §5.3 人数与空间 ---------------------------------------------------
    _check("VIS.SPACE.PERSON_COUNT", "continuity", "major", scope="shot",
           recheck="semantic+continuity"),
    _check("VIS.SPACE.RELATIVE_POSITION", "continuity", "major", scope="shot",
           recheck="semantic+continuity"),
    _check("VIS.SPACE.PROP_STATE", "prop", "major", scope="shot",
           expected=EXPECTED_SOURCE_S17, recheck="semantic+continuity"),
    _check("VIS.SPACE.PROP_OWNERSHIP", "prop", "major", scope="shot",
           expected=EXPECTED_SOURCE_S17, recheck="semantic+continuity"),
    _check("VIS.SPACE.BACKGROUND_JUMP", "continuity", "major", scope="shot",
           recheck="semantic+continuity"),

    # --- §5.4 机位与动作 ---------------------------------------------------
    _check("VIS.CAMERA.SHOT_TYPE", "camera_adherence", "major", scope="shot"),
    _check("VIS.CAMERA.ANGLE", "camera_adherence", "major", scope="shot"),
    _check("VIS.CAMERA.MOTION", "camera_adherence", "major",
           scope="window", max_frames=48),
    _check("VIS.ACTION.BEAT_MISSING", "action_adherence", "major", scope="shot"),
    _check("VIS.ACTION.BEAT_EXTRA", "action_adherence", "minor", scope="window",
           max_frames=48, promotable=True),
    _check("VIS.ACTION.TIMING", "action_adherence", "minor", scope="shot",
           promotable=True),

    # --- §5.5 时序与跨镜 ---------------------------------------------------
    _check("VIS.TEMPORAL.FLICKER", "temporal", "minor", scope="window",
           max_frames=48, promotable=True),
    _check("VIS.TEMPORAL.MORPH_ARTIFACT", "temporal", "major", scope="window",
           max_frames=48),
    _check("VIS.TEMPORAL.BOUNDARY_POP", "temporal", "major", scope="frame",
           max_frames=6),
    _check("VIS.CONTINUITY.SHOT_BOUNDARY", "continuity", "major", scope="frame",
           max_frames=6, recheck="semantic+continuity"),
    # §2.3 那张表把它写成 `VIS.AXIS.SHOT_CROSSING`，§5.5 写成
    # `VIS.CONTINUITY.AXIS_CROSSING`。取注册表节（§5.5）的名字，冲突记在 SQC-21。
    _check("VIS.CONTINUITY.AXIS_CROSSING", "continuity", "blocker", scope="shot",
           recheck="semantic+continuity",
           notes="对白场越轴且 ShotSpec 未声明越轴意图（E24-S5）"),
    _check("VIS.CONTINUITY.LIGHT_DIRECTION", "continuity", "minor", scope="shot",
           recheck="semantic+continuity", promotable=True),
    _check("VIS.SAFE_AREA.TEXT_OVERLAP", "subtitle", "major", scope="frame",
           max_frames=6, recheck="subtitle",
           default_action="re_render_subtitles",
           allowed=["re_render_subtitles"],
           never_auto=True,
           no_auto_reason="烧录进画面的图形无法靠重排字幕轨道修正；"
                          "只能改分镜或重生成，必须由人决定（SQC-20）"),

    # --- §5.6 S17 期望校验 -------------------------------------------------
    _check("VIS.STATE.EXPECTED_VISIBLE", "continuity", "major", scope="shot",
           expected=EXPECTED_SOURCE_S17, recheck="semantic+continuity"),
    _check("VIS.STATE.UNEXPECTED_VISIBLE", "continuity", "major", scope="shot",
           expected=EXPECTED_SOURCE_S17, recheck="semantic+continuity"),
]

# §5.6 末行是"不许留空"的显式记录：知情范围不由 S36 判，因此它**不在**上表里。
NOT_OURS = {
    "VIS.KNOWLEDGE.SCOPE_RENDER": "知情范围由 S17 全权负责，S36 不判（§5.6）",
}

CHECK_IDS = [c["check_id"] for c in CHECKS]

# check_id → 注册项。内核按 id 取用；顺序与 `CHECKS` 一致。
BY_ID = {c["check_id"]: c for c in CHECKS}

# 依赖 S17 expected_state 的检查。S17 不可达时这些记 dependency_unavailable，
# 若它们是必查项则整份报告 abstain（§9 末段：不允许 silent skip）。
NEEDS_EXPECTED_STATE = frozenset(
    c["check_id"] for c in CHECKS if c["_expected_source"] == EXPECTED_SOURCE_S17)

# 依赖本地手部检测的检查：手部面积不足时它们判 not_applicable。
NEEDS_HAND = frozenset({
    "VIS.HANDS.FINGER_DEFORM",
    "VIS.HANDS.OCCLUSION_ARTIFACT",
    "VIS.HANDS.INTERACTION",
})

# 依赖"画面上有人脸"的检查：无脸窗口判 not_applicable(no_face)，不是通过。
NEEDS_FACE = frozenset({
    "VIS.IDENTITY.FACE_DRIFT",
    "VIS.IDENTITY.FACE_SWAP_WITHIN_SHOT",
    "VIS.IDENTITY.BODY_PROPORTION",
    "VIS.IDENTITY.OUTFIT_REVISION",
    "VIS.IDENTITY.ACCESSORY_MISSING",
    "VIS.IDENTITY.ACCESSORY_EXTRA",
})

HIGH_RISK = frozenset(c["check_id"] for c in CHECKS if c["_high_risk"])

# `not_applicable` 的合法理由。只有这些能不计入 abstain（S35 §2.5）。
# `legal_exception` 目前不在 qc-report.v2 的枚举里（SQC-2），但 §7 要求它。
JUSTIFIED_NOT_APPLICABLE = frozenset({
    "no_face", "no_such_content", "no_dialogue", "sampling_not_selected",
    "policy_disabled", "legal_exception",
})


def registry_document() -> dict:
    """导出成 `check-registry.v2` 形状的文档，供契约校验器加载。

    注册项里的 `_` 前缀字段是内核用的，不进文档（schema 是
    `additionalProperties:false`，多一个键就加载不了）。
    """
    checks = []
    for entry in CHECKS:
        checks.append({k: v for k, v in entry.items() if not k.startswith("_")})
    return {
        "schema_version": "2.0",
        "registry_version": REGISTRY_VERSION,
        "checks": checks,
    }


def select(include, exclude) -> list[str]:
    """把 `check_selection` 解析成有序的 check_id 列表。

    通配只支持末尾的 `.*`（§9 示例 `VIS.IDENTITY.*`）。未知模式直接报错：
    拼错一个前缀就静默少查一组，这是"沉默变成绿灯"最容易发生的地方。
    """
    import fnmatch

    if include is None:
        include = ["VIS.*"]
    if not isinstance(include, list) or not include:
        raise ValueError("check_selection.include must be a non-empty list")
    for pattern in list(include) + list(exclude or []):
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("check_selection entries must be non-empty strings")
        if not _pattern_matches_anything(pattern):
            raise ValueError(f"check_selection pattern matches no check: {pattern}")

    chosen = []
    for check_id in CHECK_IDS:
        if not any(fnmatch.fnmatchcase(check_id, p) for p in include):
            continue
        if any(fnmatch.fnmatchcase(check_id, p) for p in (exclude or [])):
            continue
        chosen.append(check_id)
    if not chosen:
        raise ValueError("check_selection selects no check")
    return chosen


def _pattern_matches_anything(pattern: str) -> bool:
    import fnmatch

    return any(fnmatch.fnmatchcase(check_id, pattern) for check_id in CHECK_IDS)


def window_plan(shot_type: str, duration_seconds: float) -> list[dict]:
    """按 §4 把时长切成窗口。返回 `[{window_index, start_seconds, end_seconds}]`。

    `transition` 例外：以切点为中心取 1.0s，而不是从头平铺——平铺会在
    切点落在窗口中间时把"前后 3 帧"拆到两个窗口里，边界连续性就看不出来。
    """
    seconds, _frames = SAMPLING_WINDOWS.get(
        shot_type, SAMPLING_WINDOWS[DEFAULT_SHOT_TYPE])
    duration = max(0.0, float(duration_seconds))
    if duration <= 0:
        return []
    if shot_type == "transition":
        # 切点假定在镜头末尾（上一镜与下一镜的接缝）；没有切点信息时宁可
        # 把整段当一个窗口，也不假装知道切点在哪。
        start = max(0.0, duration - 0.5)
        return [{"window_index": 0, "start_seconds": round(start, 3),
                 "end_seconds": round(duration, 3)}]

    windows = []
    index = 0
    start = 0.0
    while start < duration - 1e-9:
        end = min(duration, start + seconds)
        windows.append({"window_index": index,
                        "start_seconds": round(start, 3),
                        "end_seconds": round(end, 3)})
        index += 1
        start = end
    return windows


def frames_per_window(shot_type: str) -> int:
    return SAMPLING_WINDOWS.get(
        shot_type, SAMPLING_WINDOWS[DEFAULT_SHOT_TYPE])[1]


def severity_after(default: str, proposed: str, ceiling: str) -> str:
    """模型只能上提不能下压，且封顶在登记上限（§5 首句）。"""
    order = SEVERITY_ORDER
    if proposed not in order:
        raise ValueError(f"unknown severity: {proposed}")
    value = default if order[default] >= order[proposed] else proposed
    if order[value] > order[ceiling]:
        value = ceiling
    return value
