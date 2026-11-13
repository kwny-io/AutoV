"""S35 的检查项登记与确定性判定。方案：`plans/S35-technical-qc.md` §3.1–§3.3、§4。

三条不变量，每一条都在代码里有对应结构：

1. **没查成 ≠ 通过。** 每条检查只可能给出四种结论：`evaluated`（查了，可能带
   缺陷）、`not_applicable`（素材本来就没有这项内容，理由可验证）、
   `not_evaluated` / `failed`（该查没查成）。后两者一律让 `decide()` 走到
   `abstain`，而且是**在比较分数之前**（§2.7 第 3 步）。探针文档缺一段、
   ShotSpec 读不到、策略没给阈值，走的都是 `not_evaluated`——把 `{}` 读成
   空缺陷列表就是在把"没查"说成"没问题"。

2. **只测量，不修复。** 这里的 `actions` 是 `recommended_actions`，§2.2 的
   表写明它是"S39 的输入，不是指令"。本模块没有任何写 artifact、触发重生成
   或调用模型的路径——S35 是确定性测量器（§3.1），`detector` 恒为
   `deterministic`。

3. **判定的输入是实测值 + 阈值 + 比较符。** 报告只记这三个（§4 末段），不
   嵌策略正文；策略版本在 `policy_version` 里。

探针文档（`probe`）的形状是与媒体沙箱之间的**私有约定**（`deps.build_probe_source`
的 docstring 有完整说明），不是契约。缺任何一段，依赖它的检查就是
`not_evaluated`，不是"没有缺陷"。
"""

from __future__ import annotations

LAYER = "technical"

# §4 的 23 条检查。字典顺序即 manifest 顺序——稳定顺序让两次评估的
# `check_manifest` 可以逐项对拍（E24 的可复现性要求）。
#
# `applies`：素材种类。不在集合里 → `not_applicable` + `no_such_content`，
#           这是"本来就没有这类内容"，不是"没查"。
# `needs`：`dialogue` 表示判定需要 ShotSpec 的 `dialogue` 声明（§4 末段：
#          不来自 ASR）；`shot` 表示需要 ShotSpec 的时长。
# `severity`：字符串，或 `(默认档, "master" 时升到的档)`。
CHECKS = {
    "TECH.CONTAINER.PARSE": dict(
        type="technical", severity="blocker", scope="whole_artifact",
        actions=("reingest", "transcode"), applies=("video", "audio", "master")),
    "TECH.FILE.EMPTY": dict(
        type="technical", severity="blocker", scope="whole_artifact",
        actions=("reingest",), applies=("video", "audio", "master")),
    "TECH.FILE.TRUNCATED": dict(
        type="technical", severity="blocker", scope="whole_artifact",
        actions=("reingest", "transcode"), applies=("video", "audio", "master")),
    "TECH.DECODE.PACKET_ERROR": dict(
        type="technical", severity="blocker", scope="segment",
        actions=("transcode",), applies=("video", "audio", "master")),
    "TECH.DURATION.MISMATCH": dict(
        type="technical", severity="blocker", scope="whole_artifact",
        actions=("regenerate_video", "temporal_edit"),
        applies=("video", "audio", "master"), needs=("shot",)),
    "TECH.DURATION.ZERO": dict(
        type="technical", severity="blocker", scope="whole_artifact",
        actions=("reingest",), applies=("video", "audio", "master")),
    "TECH.RESOLUTION.BELOW_SPEC": dict(
        type="technical", severity="blocker", scope="whole_artifact",
        actions=("regenerate_video",), applies=("video", "master")),
    "TECH.RESOLUTION.MISMATCH": dict(
        type="technical", severity="major", scope="whole_artifact",
        actions=("regenerate_video", "transcode"), applies=("video", "master")),
    "TECH.FPS.MISMATCH": dict(
        type="technical", severity="major", scope="whole_artifact",
        actions=("transcode",), applies=("video", "master")),
    "TECH.FPS.VFR": dict(
        type="technical", severity="major", scope="whole_artifact",
        actions=("transcode",), applies=("video", "master")),
    "TECH.CODEC.UNSUPPORTED": dict(
        type="technical", severity="major", scope="whole_artifact",
        actions=("transcode",), applies=("video", "audio", "master")),
    "TECH.COLOR.RANGE_MISMATCH": dict(
        type="technical", severity="minor", scope="whole_artifact",
        actions=("transcode",), applies=("video", "master")),
    "TECH.BLACK_FRAME": dict(
        type="technical", severity="major", scope="segment",
        actions=("temporal_edit", "regenerate_video"), applies=("video", "master")),
    "TECH.FREEZE_FRAME": dict(
        type="technical", severity="major", scope="segment",
        actions=("temporal_edit", "regenerate_video"), applies=("video", "master")),
    "TECH.FLASH_FRAME": dict(
        type="technical", severity="minor", scope="frame",
        actions=("temporal_edit",), applies=("video", "master")),
    "TECH.AUDIO.MISSING": dict(
        type="technical", severity="blocker", scope="whole_artifact",
        actions=("regenerate_video", "remix"),
        applies=("video", "audio", "master"), needs=("dialogue",)),
    "TECH.AUDIO.OFFSET": dict(
        # §4 写的是 `remux→transcode`；`remux` 不在 12 值枚举里，`transcode`
        # 在，且是同一族（technical）里最接近的落地动作。见 schema S35-9。
        type="technical", severity="major", scope="whole_artifact",
        actions=("transcode",), applies=("video", "master")),
    "TECH.AUDIO.SILENCE_HEAD_TAIL": dict(
        type="technical", severity="minor", scope="segment",
        actions=("temporal_edit",), applies=("video", "audio", "master")),
    "TECH.LOUDNESS.LUFS": dict(
        type="technical", severity="major", scope="whole_artifact",
        actions=("remix",), applies=("video", "audio", "master")),
    "TECH.LOUDNESS.TRUE_PEAK": dict(
        # §4：`blocker（master）`。master 是交付母版，峰值越界不可放行；
        # 中间产物同一条只算 major（见 schema S35-13）。
        type="technical", severity=("major", "blocker"), scope="whole_artifact",
        actions=("remix",), applies=("video", "audio", "master")),
    "TECH.AUDIO.CLIPPING": dict(
        type="technical", severity="major", scope="segment",
        actions=("remix",), applies=("video", "audio", "master")),
    "TECH.SUBTITLE.BURNED_IN": dict(
        type="technical", severity="blocker", scope="frame",
        actions=("regenerate_video",), applies=("video", "master")),
    "TECH.META.MISSING": dict(
        # §4 的允许动作是 `—`，而 defect.recommended_actions 是 minItems 1，
        # 所以这条只登记测量值、不产 defect（S35-9）。info 不参与判定。
        type="technical", severity="info", scope="whole_artifact",
        actions=(), applies=("video", "audio", "master")),
}

ALL_CHECKS = tuple(CHECKS)


def severity_for(check_id: str, artifact_kind: str) -> str:
    """§4 的严重度可以是条件式的（master 时升档）。"""
    severity = CHECKS[check_id]["severity"]
    if isinstance(severity, tuple):
        default, master = severity
        return master if artifact_kind == "master" else default
    return severity


def applicability(check_id: str, artifact_kind: str) -> str | None:
    """返回 `None` 表示适用；否则返回 `not_applicable` 的 reason_code。

    素材种类不在登记的 `applies` 里 = 根本没有这类内容（音频文件没有画幅），
    这是 §2.5 的 `not_applicable`，不是"没查成"。
    """
    if artifact_kind in CHECKS[check_id]["applies"]:
        return None
    return "no_such_content"
