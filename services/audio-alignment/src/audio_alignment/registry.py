"""S37 的检查项注册表（方案 §3）。

方案 §3 的四张表就是注册表的权威来源：20 行，每行一个 `check_id`。这里把
它们抄成**可加载的注册数据**，而不是散文——`contracts/quality/
validate_registry.py` 会逐条校验 `layer=="alignment"`、`owner_service=="S37"`、
`AUD.` 前缀、`defect_type` 在 14 值闭集内、`default_action` /
`allowed_actions` / `escalation_rule.escalate_to` 都在该族动作链内、
`severity_default ≤ severity_max`。测试里直接跑那个校验器。

四条实现上的取舍：

1. **`AUD.TEXT.SCRIPT_CONFLICT` 与 `AUD.LIPSYNC.FACE_NOT_FOUND` 也登记，
   但永不产 defect**（方案 §3.1 末行、§3.3 末行）。注册表表达的是「这条检查
   归 S37、动作链是哪一族」，不是「一定会产缺陷」。前者只写一条 info
   dimension（S37-7），后者走 `not_applicable(no_face)`。把其中任何一条从
   注册表里删掉，S39 就会以为"这个检查不存在"，然后无法解释一份
   `not_applicable` 的 manifest 项。
2. **口型族只有 `redo_lipsync` 一条链。** 动作族契约里 lipsync 族的
   `forbidden` 明确写着「口型缺陷不得推断出 `re_synthesize_speech`」——
   音频本身错了那是 `voice` 缺陷，必须按 `voice` 报。所以五条 `AUD.LIPSYNC.*`
   的 `allowed_actions` 一律只有 `redo_lipsync`，S39 于是只能走重做口型或
   人审。
3. **`AUD.VOICE.AUTHORIZATION` 登记 `never_auto_repair`。** 撤销授权后重合成
   会再拿同一个音色去合成，等于把权利问题变成内容问题；`regenerate_video`
   同理。方案 §3.4 末段要求 `recommended_actions` 只能是 `human_review`。
4. **严重度上限留档位，实际档位由内核按实测值取。** `OFFSET` 的
   "blocker(>120ms)/major(>60ms)"、`SUBSTITUTION` 的"blocker(关键台词)"、
   `EMBEDDING_DRIFT` 的"连续 3 窗口升级"都是**同一条检查的两个档位**，
   而注册表每条只有一个 `severity_default`。登记默认档 + `severity_max`
   上限，内核按测量决定实际值，绝不越过上限（S37-8/9/10）。
"""

# 注册表版本：每次增删改都 +1。
REGISTRY_VERSION = 1

# 采样策略版本。S37 不抽帧采样（口型必须逐帧），但仍要随报告记录一个版本，
# 因为它决定 coverage 的分母怎么算。
SAMPLING_VERSION = "s37-alignment.v1"

# 口径版本：对齐算法与证据流的组合。变了就是换了评估器。
ALIGN_MODEL_VERSION = "s37-forced-align.v1"
# 键名必须与 `qc-report.v2` 的 `evaluator.model_snapshot` 一致：
# `{provider, model_id, snapshot}`（`additionalProperties:false`）。分析文档
# 没回传 snapshot 时这份会被原样写进报告，用 `model`/`version` 命名会让
# 一份本该合法的报告校验失败。
ASR_MODEL_SNAPSHOT = {"provider": "mock", "model_id": "asr-multilingual",
                      "snapshot": "2026-01"}

# ---------------------------------------------------------------- §3 的阈值
# 数值来自方案 §3.2/§3.3/§4；真实值由 S02 下发并记进 policy_version，
# 这里是本服务在没有策略快照时的起点。

# §3.2
MAX_ALIGN_CI_MS = 120
DIALOGUE_DURATION_TOLERANCE_MS = 400
# §3.3
LIPSYNC_BLOCKER_MS = 120
LIPSYNC_MAJOR_MS = 60
LIPSYNC_REQUIRED_COVERAGE_MIN = 0.95
# §4 第 2、3 条
MIN_ALIGNMENT_CONFIDENCE = 0.5
MAX_UNJUDGED_TOKEN_RATIO = 0.10
# §3.4：连续命中的窗口数。
EMBEDDING_DRIFT_WINDOWS = 3
# 除口型外其他检查的最低覆盖率（S35 §2.7 的同一默认值）。
DEFAULT_REQUIRED_COVERAGE_MIN = 0.90

# 严重度序，用于"只能上提不能下压"。
SEVERITY_ORDER = {"info": 0, "minor": 1, "major": 2, "blocker": 3}

# `type` → 动作族。与 `contracts/quality/action-families.v2.json` 的
# `families[*].chain` 一致；这里只抄本服务用得到的三个族，校验时仍读契约文件。
_FAMILY = {
    "voice": ["re_synthesize_speech", "regenerate_video"],
    "temporal": ["temporal_edit", "regenerate_video"],
    "lipsync": ["redo_lipsync"],
}

# 注册表的 `window_scope` → 报告 defect 的 `scope`（qc-report.v2 的四值闭集）。
SCOPE_MAP = {
    "frame": "frame",
    "window": "window",
    "shot": "segment",
    "track": "whole_artifact",
    "all": "whole_artifact",
}


def _check(check_id, defect_type, severity, *, scope, default_action=None,
           allowed=None, recheck="alignment", promotable_windows=None,
           never_auto=False, no_auto_reason=None, expected="S06",
           notes=None):
    """构造一条注册项。

    `expected` 只决定缺陷的 `expected_source.service_id`：S37 自己知道该拿谁
    当基准（脚本、ShotSpec、S15 绑定、S15 词典），不向模型询问来源。
    """
    if allowed is None:
        allowed = list(_FAMILY[defect_type])
    if default_action is None:
        default_action = allowed[0]
    entry = {
        "check_id": check_id,
        "layer": "alignment",
        "owner_service": "S37",
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
    }
    if promotable_windows is not None:
        entry["escalation_rule"] = {
            "metric": "consecutive_windows",
            "comparator": "gte",
            "threshold": promotable_windows,
            "escalate_to": _FAMILY[defect_type][-1],
        }
    if never_auto:
        entry["never_auto_repair"] = True
        entry["no_auto_reason"] = no_auto_reason
    if notes:
        entry["notes"] = notes
    return entry


CHECKS = [
    # --- §3.1 语音内容 -----------------------------------------------------
    _check("AUD.TEXT.MISSING_WORD", "voice", "major", scope="shot",
           notes="脚本有、ASR 无；区间精确到该词，ASR token 作证据（E24-A5）"),
    _check("AUD.TEXT.EXTRA_WORD", "voice", "major", scope="shot",
           notes="ASR 有、脚本无"),
    _check("AUD.TEXT.MISPRONOUNCE", "voice", "major", scope="shot",
           expected="S15",
           notes="字面一致但音素序列与 S15 词典不符（E24-A6）；"
                 "对齐置信度 < 0.5 的字不参与判定（§4 第 2 条）"),
    _check("AUD.TEXT.SUBSTITUTION", "voice", "major", scope="shot",
           notes="念成另一个词且改变语义；关键台词升 blocker（S37-10）"),
    _check("AUD.TEXT.PROSODY_FLAT", "voice", "minor", scope="shot",
           notes="情感/重音与 ShotSpec 情绪标签严重不符"),
    # 方案 §3.1 末行：不产 defect，只记 info dimension。登记它是为了让
    # S39 能解释 manifest 里的那一项，且明确它永远不进自动修复。
    _check("AUD.TEXT.SCRIPT_CONFLICT", "voice", "info", scope="shot",
           allowed=["re_synthesize_speech"], never_auto=True,
           no_auto_reason="ASR 与脚本冲突时 S37 只上报观测，不判定谁对；"
                          "重配与改稿都是创作决定，必须由人或 S39 决定"
                          "（service.yaml recovery：文本冲突不自动改剧本）",
           notes="观测型：不产生 defect，只写一条 info dimension（S37-7）"),

    # --- §3.2 时间与对齐 ---------------------------------------------------
    _check("AUD.ALIGN.LOW_CONFIDENCE", "temporal", "major", scope="shot",
           notes=f"置信区间宽于 {MAX_ALIGN_CI_MS}ms 的字"),
    _check("AUD.ALIGN.GAP", "temporal", "minor", scope="shot",
           notes="字间非语音间隙而脚本无停顿标记"),
    _check("AUD.ALIGN.OVERLAP", "temporal", "major", scope="shot",
           notes="同一说话人 token 时间重叠——对齐失败信号"),
    _check("AUD.ALIGN.SPEAKER_SWITCH", "voice", "major", scope="shot",
           notes="说话人变更点与脚本说话人标记不符"),
    _check("AUD.ALIGN.DURATION_VS_SPEC", "temporal", "major", scope="shot",
           notes=f"实际时长与 ShotSpec dialogue_duration 偏差 > "
                 f"{DIALOGUE_DURATION_TOLERANCE_MS}ms（E24-A7）；"
                 f"修复属创作决策，S39 转 human_review"),

    # --- §3.3 口型（需要视频 + face track） --------------------------------
    # 口型族只有 redo_lipsync；抽帧比对只能发现粗粒度问题，OFFSET 必须逐帧，
    # 所以 coverage 按帧算、下限 0.95（§3.3 末段）。
    _check("AUD.LIPSYNC.OFFSET", "lipsync", "major", scope="shot",
           allowed=["redo_lipsync"],
           notes=f"唇部闭合事件与音素爆破点时间差；>{LIPSYNC_MAJOR_MS}ms major、"
                 f">{LIPSYNC_BLOCKER_MS}ms blocker（S37-9）；逐帧覆盖，下限 "
                 f"{LIPSYNC_REQUIRED_COVERAGE_MIN}"),
    _check("AUD.LIPSYNC.OPENNESS_MISMATCH", "lipsync", "major", scope="shot",
           allowed=["redo_lipsync"],
           notes="张口幅度与元音开口度相关性低于阈值"),
    _check("AUD.LIPSYNC.MOUTH_MOVES_SILENT", "lipsync", "major", scope="shot",
           allowed=["redo_lipsync"], notes="静音段嘴部持续运动（E24-A4）"),
    _check("AUD.LIPSYNC.SPEAKS_CLOSED", "lipsync", "blocker", scope="shot",
           allowed=["redo_lipsync"],
           notes="有声段嘴部完全静止——明显没在对口型（E24-A3）"),
    # 方案 §3.3 末行：没有人脸不是缺陷，是 not_applicable(no_face)。
    _check("AUD.LIPSYNC.FACE_NOT_FOUND", "lipsync", "info", scope="shot",
           allowed=["redo_lipsync"], never_auto=True,
           no_auto_reason="没有人脸轨道就没有可修的口型；自动重做口型会先要求"
                          "生成一张脸，那是分镜决策，不是质检能做的",
           notes="门控型：判 not_applicable(no_face)，永不产 defect（S37-7）"),

    # --- §3.4 音色 ---------------------------------------------------------
    _check("AUD.VOICE.EMBEDDING_DRIFT", "voice", "major", scope="shot",
           promotable_windows=EMBEDDING_DRIFT_WINDOWS,
           notes=f"与 S15 binding 的 embedding 距离 > τ；单窗仍是 major，"
                 f"连续 {EMBEDDING_DRIFT_WINDOWS} 窗口升 blocker（S37-8）"),
    _check("AUD.VOICE.CHARACTER_MIXUP", "voice", "blocker", scope="shot",
           notes="同一角色跨镜音色显著不同；距离由分析器回传（S37-18）"),
    _check("AUD.VOICE.EMOTION_MISMATCH", "voice", "minor", scope="shot",
           notes="情绪与 ShotSpec 不符"),
    # 方案 §3.4 末段：命中时 recommended_actions 只能是 human_review。
    # 重合成会用同一个已撤销音色去合成，等于把权利问题变成内容问题。
    _check("AUD.VOICE.AUTHORIZATION", "voice", "blocker", scope="shot",
           allowed=["regenerate_video"], never_auto=True,
           no_auto_reason="音色授权已撤销；重合成与重生成都会再次使用同一个"
                          "已撤销音色。必须先由人更换音色或恢复授权（§3.4）",
           notes="权利缺陷，不是内容缺陷；recommended_actions 只能是 "
                 "human_review（I3）"),
]

# 方案 §3.1 末行是"不许留空"的显式记录：知情范围不由 S37 判。
NOT_OURS = {
    "AUD.TEXT.SCRIPT_CONFLICT": "观测型检查，永不产 defect；谁对谁错不由 S37 判"
}

CHECK_IDS = [c["check_id"] for c in CHECKS]

# check_id → 注册项。内核按 id 取用；顺序与 `CHECKS` 一致。
BY_ID = {c["check_id"]: c for c in CHECKS}

# 依赖视频 + 有人脸的检查。external 音频 / 无人脸 / VFR 时它们判
# not_applicable（理由分别是 no_such_content / no_face），**不是通过**（S37-11）。
NEEDS_VIDEO_FACE = frozenset({
    "AUD.LIPSYNC.OFFSET",
    "AUD.LIPSYNC.OPENNESS_MISMATCH",
    "AUD.LIPSYNC.MOUTH_MOVES_SILENT",
    "AUD.LIPSYNC.SPEAKS_CLOSED",
})

# 依赖 S15 发音词典的检查。词典不可达时它们 not_evaluated(dependency_unavailable)。
NEEDS_LEXICON = frozenset({"AUD.TEXT.MISPRONOUNCE"})

# 依赖 S15 音色绑定/授权投影的检查（E25-A4：S15 不可达 → abstain）。
NEEDS_VOICE_AUTHORITY = frozenset(c["check_id"] for c in CHECKS
                                  if c["_expected_source"] == "S15") | {
    "AUD.VOICE.EMBEDDING_DRIFT",
    "AUD.VOICE.CHARACTER_MIXUP",
    "AUD.VOICE.EMOTION_MISMATCH",
    "AUD.VOICE.AUTHORIZATION",
}

# 观测型检查：只写 dimension，永不产 defect。
OBSERVATION_ONLY = frozenset({"AUD.TEXT.SCRIPT_CONFLICT"})

# 门控型检查：结论只有 not_applicable / evaluated，永不产 defect。
GATING_ONLY = frozenset({"AUD.LIPSYNC.FACE_NOT_FOUND"})

# `not_applicable` 的合法理由。只有这些能不计入 abstain。
JUSTIFIED_NOT_APPLICABLE = frozenset({
    "no_face", "no_such_content", "no_dialogue", "sampling_not_selected",
    "policy_disabled",
})


def registry_document() -> dict:
    """导出成 `check-registry.v2` 形状的文档，供契约校验器加载。

    注册项里的 `_` 前缀字段是内核用的，不进文档（schema 是
    `additionalProperties:false`，多一个键就加载不了）。
    """
    checks = [{k: v for k, v in entry.items() if not k.startswith("_")}
              for entry in CHECKS]
    return {
        "schema_version": "2.0",
        "registry_version": REGISTRY_VERSION,
        "checks": checks,
    }


def select(include, exclude) -> list[str]:
    """把 `checks.include` 解析成有序的 check_id 列表。

    通配只支持 fnmatch（§6 示例 `AUD.TEXT.*`）。未知模式直接报错：拼错一个
    前缀就静默少查一组，这是"沉默变成绿灯"最容易发生的地方。
    显式点名一个不在注册表里的 check 同样报错，不静默跳过。
    """
    import fnmatch

    if include is None:
        include = ["AUD.*"]
    if not isinstance(include, list) or not include:
        raise ValueError("checks.include must be a non-empty list")
    for pattern in list(include) + list(exclude or []):
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("checks.include entries must be non-empty strings")
        if not any(fnmatch.fnmatchcase(check_id, pattern)
                   for check_id in CHECK_IDS):
            raise ValueError(f"checks.include pattern matches no check: {pattern}")

    chosen = []
    for check_id in CHECK_IDS:
        if not any(fnmatch.fnmatchcase(check_id, p) for p in include):
            continue
        if any(fnmatch.fnmatchcase(check_id, p) for p in (exclude or [])):
            continue
        chosen.append(check_id)
    if not chosen:
        raise ValueError("checks.include selects no check")
    return chosen


def severity_after(default: str, proposed: str, ceiling: str) -> str:
    """只能上提不能下压，且封顶在登记上限（§3 首句）。"""
    order = SEVERITY_ORDER
    if proposed not in order:
        raise ValueError(f"unknown severity: {proposed}")
    value = default if order[default] >= order[proposed] else proposed
    if order[value] > order[ceiling]:
        value = ceiling
    return value


def report_scope(window_scope: str) -> str:
    try:
        return SCOPE_MAP[window_scope]
    except KeyError:
        raise ValueError(f"unknown window_scope: {window_scope!r}") from None
