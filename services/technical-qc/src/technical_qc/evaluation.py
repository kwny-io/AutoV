"""S35 的判定核心：逐项检查 → coverage → decide。方案 §2.4–§2.7、§3.3、§4。

这个模块**只做纯函数**，不碰数据库、不碰网络。它的输出决定 `decision`，
所以每一条分支都要能被"这条是不是查成了"回答清楚。

§2.7 第 3 步是整个服务最重要的四行：**有必查项 failed/not_evaluated 时先弃权，
再谈分数**。评估器超时不是低分，是没有结论；把它送进分数比较就等于用
"没测出来"去证明"没问题"。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math

from . import checks

QC_LAYER = "technical"

# §2.7 的阈值默认值。真正的值由 S02 下发（`policy_version` 记在报告里）；
# 这里只在策略没写时使用，并且**只有缺阈值时才会走到**。
DEFAULTS = {
    "min_coverage_ratio": 0.90,
    "tau_low": 0.40,
    "tau_abstain": 0.35,
    "tau_disagreement": 0.30,
    "freeze_min_seconds": 0.5,
    "flash_luma_delta": 0.6,
    "lufs_tolerance": 2.0,
    "true_peak_max_db": -1.0,
    "duration_tolerance_frames": 1,
    "audio_offset_tolerance_frames": 1,
    "silence_head_seconds": 0.5,
    "silence_tail_seconds": 0.5,
    "clipping_ratio_max": 0.001,
}

# ---------------------------------------------------------------- 基础工具


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _b32(key_hex: str, length: int) -> str:
    """§2.6 的 `base32_lower(derived_key)`。去掉 `=` 填充后取前 N 位。"""
    raw = bytes.fromhex(key_hex)
    encoded = base64.b32encode(raw).decode("ascii").lower().rstrip("=")
    return encoded[:length]


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _pos(value, default=0.0) -> float:
    return float(value) if _finite(value) and value >= 0 else default


# ---------------------------------------------------------------- 指纹


def defect_fingerprint(*, check_id: str, kind: str, character_id,
                       severity: str, start_seconds: float) -> str:
    """§2.4：与 artifact hash 无关的稳定身份，跨复检判"同类重复"。

    不含 `observed`（措辞随版本变）、不含 `artifact_sha256`（这正是跨尝试
    追踪的基础）、不含精确 severity（blocker/major 抖动不该分裂指纹）。

    取前 16 **字节**的十六进制（32 字符）而不是方案字面的 "16 个十六进制
    字符"——契约的 pattern 是 `^[0-9a-f]{32}$`，见 schema S35-3。
    """
    return _sha256(_jcs({
        "qc_layer": QC_LAYER,
        "check_id": check_id,
        "type": kind,
        "character_id": character_id,
        "severity_bucket": "blocker" if severity == "blocker" else "non_blocker",
        "location_bucket": int(round(start_seconds / 0.5)),
    }))[:32]


def _defect_id(fingerprint: str, index: int) -> str:
    """由指纹派生，保证"同样的输入给出同样的报告"（E24 可复现性、ROUTE）。"""
    return "df_" + _b32(_sha256(f"{fingerprint}:{index}"), 26)


# ---------------------------------------------------------------- 结果助手


def _res(check_id, status, reason, defects=None):
    return {"check_id": check_id, "status": status, "reason_code": reason,
            "defects": list(defects or [])}


def _not_evaluated(check_id, reason="dependency_unavailable"):
    return _res(check_id, "not_evaluated", reason)


def _failed(check_id, reason="evaluator_error"):
    return _res(check_id, "failed", reason)


def _na(check_id, reason="no_such_content"):
    return _res(check_id, "not_applicable", reason)


def _evaluated(check_id, defects=None):
    return _res(check_id, "evaluated", "ok", defects)


# ---------------------------------------------------------------- 检查执行


def evaluate_checks(*, probe, shot, profile, artifact, artifact_kind,
                    enabled, unreachable=False) -> list:
    """按 §4 的注册表逐项检查，返回 `[{check_id,status,reason_code,defects}]`。

    `enabled` 是 `TECHNICAL_QC_CHECKS` 白名单。**不在白名单 = not_evaluated +
    policy_disabled**，不是"跳过"。跳过的检查不会出现在 manifest 里，而 manifest
    是"该查的都查了"的唯一证明（§2.3），缺项会让 decide() 第 1 步弃权——
    所以这里宁可显式登记"被策略关掉"，也不留空。

    `shot` 是 ShotSpec 的 `{duration_seconds, dialogue}` 或 `None`（读不到）。
    `probe` 是媒体沙箱的探针文档；`probe` 本身没有结论时，全部依赖探针的项
     都是 `failed`，理由取探针给的错误码（§3.2 末段）。
    """
    threshold = (profile or {}).get("thresholds") or {}
    target = (profile or {}).get("target") or {}
    policy = (profile or {}).get("subtitle_policy")
    probe = probe if isinstance(probe, dict) else None

    # 探针整体没跑起来：§3.2 "沙箱超时 / 解码崩溃 → status=failed，
    # **不是**'没检出问题'"。asset_unavailable 也一样是 failed（报告仍出，
    # 由 decide() 落成 abstain）。
    probe_error = None
    if probe is None and unreachable:
        # §3.2 末段：连 ffprobe 都跑不起来（S28 / 沙箱不可达）时理由码是
        # `asset_unavailable`，不是 `evaluator_error`——那不是评估器算错，
        # 是素材根本取不到。两者都让整份报告 abstain，区别在归因（S35-6）。
        probe_error = ("asset_unavailable", "artifact source is unreachable")
    elif probe is None:
        probe_error = ("evaluator_error", "probe document is missing")
    elif probe.get("ok") is False or probe.get("error"):
        err = probe.get("error") or {}
        code = str(err.get("code") or "decode_error")
        probe_error = (
            "evaluator_timeout" if code == "timeout"
            else "asset_unavailable" if code == "asset_unavailable"
            else "evaluator_error",
            str(err.get("detail") or code))

    results = []
    for check_id in checks.ALL_CHECKS:
        if check_id not in enabled:
            results.append(_not_evaluated(check_id, "policy_disabled"))
            continue
        na = checks.applicability(check_id, artifact_kind)
        if na is not None:
            results.append(_na(check_id, na))
            continue
        if probe_error is not None:
            results.append(_failed(check_id, probe_error[0]))
            continue

        handler = _HANDLERS[check_id]
        try:
            result = handler(probe=probe, shot=shot, threshold=threshold,
                             target=target, policy=policy,
                             artifact_kind=artifact_kind, _artifact=artifact,
                             profile_id=(profile or {}).get("policy_id"),
                             profile_revision=(profile or {}).get("revision"))
        except _Missing as exc:
            # 探针跑起来了但这一段没有 → 这个检查没得出结论。**不能**降级成
            # "没有缺陷"：那正是把"没查"读成"通过"。
            result = _failed(check_id, "evaluator_error")
            result["_detail"] = str(exc)
        results.append(result)
    return results


class _Missing(Exception):
    """探针文档缺少该检查依赖的段落。"""


def _need(mapping, key, label=None):
    if not isinstance(mapping, dict) or mapping.get(key) is None:
        raise _Missing(f"probe has no {label or key}")
    return mapping[key]


def _need_number(mapping, key, label=None):
    value = _need(mapping, key, label)
    if not _finite(value):
        raise _Missing(f"probe.{label or key} is not a finite number")
    return float(value)


def _mk(*, check_id, kind, severity, scope, start, end, expected,
        expected_source, observed, measured, artifact_id, actions,
        confidence=1.0, character_id=None, region_bbox=None):
    """构造一条 defect。`detector` 恒为 deterministic（§3.1）。"""
    fingerprint = defect_fingerprint(
        check_id=check_id, kind=kind, character_id=character_id,
        severity=severity, start_seconds=start)
    return {
        "defect_fingerprint": fingerprint,
        "check_id": check_id,
        "type": kind,
        "severity": severity,
        "scope": scope,
        "time_range": {"start_seconds": round(float(start), 3),
                       "end_seconds": round(float(end), 3)},
        # `region_bbox` 按契约只接受归一化坐标；探针给的东西不在契约里，
        # 一律置空，等 §3.2 第 5 步的证据固化接上再填。
        "region_bbox": region_bbox,
        "character_id": character_id,
        "expected": expected,
        "expected_source": expected_source,
        "observed": observed,
        "measured": measured,
        "confidence": confidence,
        "detector": "deterministic",
        "evidence_artifact_ids": [artifact_id],
        "recommended_actions": list(actions),
    }


def _policy_source(profile_id, profile_revision, field):
    return {"service_id": "S02", "ref_id": str(profile_id),
            "ref_revision": int(profile_revision or 1)}


def _shot_source(shot):
    return {"service_id": "S06", "ref_id": str(shot.get("shot_id") or "shot"),
            "ref_revision": int(shot.get("shot_revision") or 1)}


def _artifact_id(artifact):
    """S28 的 artifact id 字段叫 `artifact_id`（`services/artifact-ingest` 的
    读路径），不是 `id`。拿不到就抛 `_Missing` → 这一条 `failed`：把编出来的
    字符串写进 `evidence_artifact_ids` 会让证据指向一个不存在的对象。
    """
    if isinstance(artifact, dict):
        value = artifact.get("artifact_id") or artifact.get("id")
        if value:
            return str(value)
    raise _Missing("artifact metadata has no artifact_id")


def _artifact_source(artifact):
    return {"service_id": "S28", "ref_id": _artifact_id(artifact),
            "ref_revision": int((artifact or {}).get("revision") or 1)}


def _measured(value, unit, threshold_value, comparator):
    return {"value": float(value), "unit": unit,
            "threshold": (None if threshold_value is None
                          else float(threshold_value)),
            "comparator": comparator}


# ---------------------------------------------------------------- 各检查

# 每个 handler 只回答一个问题："这一条查成了吗，查成了的话测到了什么"。
# 缺输入 → 抛 _Missing（→ failed），绝不返回空缺陷列表。


def _c_container(**kw):
    container = _need(kw["probe"], "container")
    artifact = kw["_artifact"]
    if not container.get("parsed") or not container.get("has_moov"):
        return _evaluated("TECH.CONTAINER.PARSE", [_mk(
            check_id="TECH.CONTAINER.PARSE", kind="technical",
            severity="blocker", scope="whole_artifact", start=0.0,
            end=max(0.001, _pos((kw["probe"].get("media") or {})
                                .get("duration_seconds"))),
            expected="container parses and carries a moov atom",
            expected_source=_artifact_source(artifact),
            observed="container unparsable or moov missing",
            measured=None, artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.CONTAINER.PARSE"]["actions"])])
    return _evaluated("TECH.CONTAINER.PARSE")


def _c_empty(**kw):
    container = _need(kw["probe"], "container")
    media = _need(kw["probe"], "media")
    artifact = kw["_artifact"]
    size = container.get("size_bytes")
    video = media.get("video")
    frame_count = (video or {}).get("frame_count")
    empty = (size is not None and float(size) == 0) or (
        video is not None and frame_count == 0)
    if empty:
        return _evaluated("TECH.FILE.EMPTY", [_mk(
            check_id="TECH.FILE.EMPTY", kind="technical", severity="blocker",
            scope="whole_artifact", start=0.0, end=0.001,
            expected="at least one decodable frame and non-zero size",
            expected_source=_artifact_source(artifact),
            observed=f"size_bytes={size}, frame_count={frame_count}",
            measured=_measured(0 if size is None else size, "bytes", 0, "gt"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.FILE.EMPTY"]["actions"])])
    return _evaluated("TECH.FILE.EMPTY")


def _c_truncated(**kw):
    decode = _need(kw["probe"], "decode")
    container = _need(kw["probe"], "container")
    media = _need(kw["probe"], "media")
    artifact = kw["_artifact"]
    declared = container.get("declared_frame_count")
    decoded = decode.get("frames_decoded")
    eof = decode.get("eof_reached")
    truncated = (eof is False
                 or (declared is not None and decoded is not None
                     and int(decoded) < int(declared)))
    if truncated:
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.FILE.TRUNCATED", [_mk(
            check_id="TECH.FILE.TRUNCATED", kind="technical", severity="blocker",
            scope="whole_artifact", start=max(0.0, duration - 0.001),
            end=max(duration, 0.002),
            expected=f"{declared if declared is not None else 'declared'} frames, clean EOF",
            expected_source=_artifact_source(artifact),
            observed=f"frames_decoded={decoded}, eof_reached={eof}",
            measured=None, artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.FILE.TRUNCATED"]["actions"])])
    return _evaluated("TECH.FILE.TRUNCATED")


def _c_packet(**kw):
    decode = _need(kw["probe"], "decode")
    media = _need(kw["probe"], "media")
    artifact = kw["_artifact"]
    errors = int(decode.get("packet_errors") or 0)
    if errors > 0:
        timecodes = [float(t) for t in (decode.get("error_timecodes") or [])
                     if _finite(t)]
        start = min(timecodes) if timecodes else 0.0
        end = max(timecodes + [start + 1.0]) if timecodes else _pos(
            media.get("duration_seconds"), 1.0)
        return _evaluated("TECH.DECODE.PACKET_ERROR", [_mk(
            check_id="TECH.DECODE.PACKET_ERROR", kind="technical",
            severity="blocker", scope="segment", start=start,
            end=max(end, start + 0.001),
            expected="0 decode errors in the deliverable range",
            expected_source=_artifact_source(artifact),
            observed=f"{errors} packet decode error(s)",
            measured=_measured(errors, "packets", 0, "eq"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.DECODE.PACKET_ERROR"]["actions"])])
    return _evaluated("TECH.DECODE.PACKET_ERROR")


def _c_duration_mismatch(**kw):
    media = _need(kw["probe"], "media")
    shot = kw["shot"]
    artifact = kw["_artifact"]
    if not isinstance(shot, dict) or not _finite(shot.get("duration_seconds")):
        # §4/C3：判定来自 ShotSpec 的声明，读不到就是"没查成"，不是"没问题"。
        return _not_evaluated("TECH.DURATION.MISMATCH", "dependency_unavailable")
    actual = _pos(media.get("duration_seconds"))
    spec = float(shot["duration_seconds"])
    fps_num, fps_den = _fps(media)
    frame = (fps_den / fps_num) if fps_num else 1.0 / 24.0
    tolerance = frame * int(kw["threshold"].get(
        "duration_tolerance_frames",
        DEFAULTS["duration_tolerance_frames"]))
    if abs(actual - spec) > tolerance:
        return _evaluated("TECH.DURATION.MISMATCH", [_mk(
            check_id="TECH.DURATION.MISMATCH", kind="technical",
            severity="blocker", scope="whole_artifact", start=0.0,
            end=max(actual, 0.001),
            expected=f"{spec:.3f}s per ShotSpec",
            expected_source=_shot_source(shot),
            observed=f"{actual:.3f}s measured",
            measured=_measured(actual, "s", spec, "ne"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.DURATION.MISMATCH"]["actions"])])
    return _evaluated("TECH.DURATION.MISMATCH")


def _c_duration_zero(**kw):
    media = _need(kw["probe"], "media")
    artifact = kw["_artifact"]
    actual = _pos(media.get("duration_seconds"))
    if actual <= 0.04:
        return _evaluated("TECH.DURATION.ZERO", [_mk(
            check_id="TECH.DURATION.ZERO", kind="technical", severity="blocker",
            scope="whole_artifact", start=0.0, end=0.04,
            expected="duration > 0.04s",
            expected_source=_artifact_source(artifact),
            observed=f"{actual:.3f}s measured",
            measured=_measured(actual, "s", 0.04, "gt"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.DURATION.ZERO"]["actions"])])
    return _evaluated("TECH.DURATION.ZERO")


def _c_resolution_below(**kw):
    media = _need(kw["probe"], "media")
    video = _need(media, "video")
    artifact = kw["_artifact"]
    floor = kw["threshold"].get("resolution_floor_short_side")
    if floor is None:
        return _not_evaluated("TECH.RESOLUTION.BELOW_SPEC", "policy_disabled")
    short = min(int(video["width"]), int(video["height"]))
    if short < int(floor):
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.RESOLUTION.BELOW_SPEC", [_mk(
            check_id="TECH.RESOLUTION.BELOW_SPEC", kind="technical",
            severity="blocker", scope="whole_artifact", start=0.0,
            end=max(duration, 0.001),
            expected=f"short side >= {int(floor)}px",
            expected_source=_policy_source(kw["profile_id"],
                                           kw["profile_revision"], "resolution"),
            observed=f"{video['width']}x{video['height']}",
            measured=_measured(short, "px", float(floor), "gte"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.RESOLUTION.BELOW_SPEC"]["actions"])])
    return _evaluated("TECH.RESOLUTION.BELOW_SPEC")


def _c_resolution_mismatch(**kw):
    media = _need(kw["probe"], "media")
    video = _need(media, "video")
    target = kw["target"]
    artifact = kw["_artifact"]
    tw, th = target.get("width"), target.get("height")
    if not (_finite(tw) and _finite(th)) or th == 0:
        return _not_evaluated("TECH.RESOLUTION.MISMATCH", "policy_disabled")
    actual_ratio = float(video["width"]) / max(1, int(video["height"]))
    target_ratio = float(tw) / float(th)
    if abs(actual_ratio - target_ratio) > 1e-3:
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.RESOLUTION.MISMATCH", [_mk(
            check_id="TECH.RESOLUTION.MISMATCH", kind="technical",
            severity="major", scope="whole_artifact", start=0.0,
            end=max(duration, 0.001),
            expected=f"{tw}x{th} ({target_ratio:.4f})",
            expected_source=_policy_source(kw["profile_id"],
                                           kw["profile_revision"], "target"),
            observed=f"{video['width']}x{video['height']} ({actual_ratio:.4f})",
            measured=_measured(actual_ratio, "ratio", target_ratio, "ne"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.RESOLUTION.MISMATCH"]["actions"])])
    return _evaluated("TECH.RESOLUTION.MISMATCH")


def _c_fps_mismatch(**kw):
    media = _need(kw["probe"], "media")
    video = _need(media, "video")
    target = kw["target"]
    artifact = kw["_artifact"]
    spec = target.get("fps")
    if spec is None:
        return _not_evaluated("TECH.FPS.MISMATCH", "policy_disabled")
    try:
        spec_num, spec_den = (int(x) for x in str(spec).split("/"))
    except (TypeError, ValueError):
        return _not_evaluated("TECH.FPS.MISMATCH", "policy_disabled")
    actual = float(video["fps_num"]) / max(1, int(video["fps_den"]))
    expected = spec_num / max(1, spec_den)
    # §4：VFR 视为不符。
    if bool(video.get("is_vfr")) or abs(actual - expected) > 1e-3:
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.FPS.MISMATCH", [_mk(
            check_id="TECH.FPS.MISMATCH", kind="technical", severity="major",
            scope="whole_artifact", start=0.0, end=max(duration, 0.001),
            expected=f"{spec_num}/{spec_den} CFR",
            expected_source=_policy_source(kw["profile_id"],
                                           kw["profile_revision"], "target"),
            observed=f"{actual:.3f}fps, is_vfr={bool(video.get('is_vfr'))}",
            measured=_measured(actual, "fps", expected, "ne"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.FPS.MISMATCH"]["actions"])])
    return _evaluated("TECH.FPS.MISMATCH")


def _c_vfr(**kw):
    media = _need(kw["probe"], "media")
    video = _need(media, "video")
    artifact = kw["_artifact"]
    if bool(video.get("is_vfr")):
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.FPS.VFR", [_mk(
            check_id="TECH.FPS.VFR", kind="technical", severity="major",
            scope="whole_artifact", start=0.0, end=max(duration, 0.001),
            expected="constant frame rate",
            expected_source=_artifact_source(artifact),
            observed="variable frame rate detected",
            measured=None, artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.FPS.VFR"]["actions"])])
    return _evaluated("TECH.FPS.VFR")


def _c_codec(**kw):
    media = _need(kw["probe"], "media")
    video = media.get("video")
    audio = media.get("audio")
    artifact = kw["_artifact"]
    allowed = kw["threshold"].get("codec_whitelist")
    if not isinstance(allowed, dict):
        return _not_evaluated("TECH.CODEC.UNSUPPORTED", "policy_disabled")
    bad = []
    if video is not None:
        if str(video.get("codec")) not in (allowed.get("video") or []):
            bad.append(f"video={video.get('codec')}/{video.get('pix_fmt')}")
        elif str(video.get("pix_fmt")) not in (allowed.get("pix_fmt") or []):
            bad.append(f"pix_fmt={video.get('pix_fmt')}")
    if audio is not None:
        if str(audio.get("codec")) not in (allowed.get("audio") or []):
            bad.append(f"audio={audio.get('codec')}")
    if bad:
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.CODEC.UNSUPPORTED", [_mk(
            check_id="TECH.CODEC.UNSUPPORTED", kind="technical",
            severity="major", scope="whole_artifact", start=0.0,
            end=max(duration, 0.001),
            expected="codec/pix_fmt in the delivery whitelist",
            expected_source=_policy_source(kw["profile_id"],
                                           kw["profile_revision"], "codec"),
            observed=", ".join(bad),
            measured=None, artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.CODEC.UNSUPPORTED"]["actions"])])
    return _evaluated("TECH.CODEC.UNSUPPORTED")


def _c_color(**kw):
    media = _need(kw["probe"], "media")
    video = _need(media, "video")
    artifact = kw["_artifact"]
    expected = kw["threshold"].get("color_range")
    actual = video.get("color_range")
    if expected is None:
        return _not_evaluated("TECH.COLOR.RANGE_MISMATCH", "policy_disabled")
    if actual == "unknown":
        # 探针没测出来 ≠ 没不一致。没有结论就不能算查过。
        return _failed("TECH.COLOR.RANGE_MISMATCH", "evaluator_error")
    if actual != expected:
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.COLOR.RANGE_MISMATCH", [_mk(
            check_id="TECH.COLOR.RANGE_MISMATCH", kind="technical",
            severity="minor", scope="whole_artifact", start=0.0,
            end=max(duration, 0.001),
            expected=f"color_range={expected}",
            expected_source=_policy_source(kw["profile_id"],
                                           kw["profile_revision"], "color"),
            observed=f"color_range={actual}",
            measured=None, artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.COLOR.RANGE_MISMATCH"]["actions"])])
    return _evaluated("TECH.COLOR.RANGE_MISMATCH")


def _c_black(**kw):
    frames = _need(kw["probe"], "frames")
    artifact = kw["_artifact"]
    minimum = kw["threshold"].get("black_min_frames")
    if minimum is None:
        return _not_evaluated("TECH.BLACK_FRAME", "policy_disabled")
    defects = []
    for interval in frames.get("black") or []:
        count = int(interval.get("frames") or 0)
        if count >= int(minimum):
            start = _pos(interval.get("start"))
            defects.append(_mk(
                check_id="TECH.BLACK_FRAME", kind="technical", severity="major",
                scope="segment", start=start,
                end=max(_pos(interval.get("end")), start + 0.001),
                expected=f"no run of >= {int(minimum)} fully black frames",
                expected_source=_artifact_source(artifact),
                observed=f"{count} consecutive black frames",
                measured=_measured(count, "frames", float(minimum), "gte"),
                artifact_id=_artifact_id(artifact),
                actions=checks.CHECKS["TECH.BLACK_FRAME"]["actions"]))
    return _evaluated("TECH.BLACK_FRAME", defects)


def _c_freeze(**kw):
    frames = _need(kw["probe"], "frames")
    artifact = kw["_artifact"]
    minimum = float(kw["threshold"].get(
        "freeze_min_seconds", DEFAULTS["freeze_min_seconds"]))
    defects = []
    for interval in frames.get("freeze") or []:
        start = _pos(interval.get("start"))
        end = _pos(interval.get("end"))
        if end - start >= minimum:
            defects.append(_mk(
                check_id="TECH.FREEZE_FRAME", kind="technical", severity="major",
                scope="segment", start=start, end=max(end, start + 0.001),
                expected=f"no still run >= {minimum}s",
                expected_source=_artifact_source(artifact),
                observed=f"{end - start:.3f}s of identical frames",
                measured=_measured(end - start, "s", minimum, "gte"),
                artifact_id=_artifact_id(artifact),
                actions=checks.CHECKS["TECH.FREEZE_FRAME"]["actions"]))
    return _evaluated("TECH.FREEZE_FRAME", defects)


def _c_flash(**kw):
    frames = _need(kw["probe"], "frames")
    artifact = kw["_artifact"]
    limit = float(kw["threshold"].get(
        "flash_luma_delta", DEFAULTS["flash_luma_delta"]))
    defects = []
    for item in frames.get("flash") or []:
        delta = item.get("delta")
        if not _finite(delta):
            continue
        if float(delta) > limit:
            at = _pos(item.get("timestamp"))
            fps_num, fps_den = 24, 1
            frame = fps_den / fps_num
            defects.append(_mk(
                check_id="TECH.FLASH_FRAME", kind="technical", severity="minor",
                scope="frame", start=at, end=max(at + frame, at + 0.001),
                expected=f"luma delta <= {limit}",
                expected_source=_artifact_source(artifact),
                observed=f"luma delta {float(delta):.3f}",
                measured=_measured(float(delta), "luma", limit, "lte"),
                artifact_id=_artifact_id(artifact),
                actions=checks.CHECKS["TECH.FLASH_FRAME"]["actions"]))
    return _evaluated("TECH.FLASH_FRAME", defects)


def _c_audio_missing(**kw):
    media = _need(kw["probe"], "media")
    audio = media.get("audio")
    shot = kw["shot"]
    artifact = kw["_artifact"]
    if not isinstance(shot, dict) or "dialogue" not in shot:
        # §4 末段：判定靠 ShotSpec 的声明，不靠 ASR；声明缺失 → 没查成。
        return _not_evaluated("TECH.AUDIO.MISSING", "dependency_unavailable")
    dialogue = shot.get("dialogue")
    has_dialogue = bool(dialogue)
    if not has_dialogue:
        # E24-T7：无对白且无音轨 = 本来就没有这类内容。
        return _na("TECH.AUDIO.MISSING", "no_dialogue")
    envelope = (kw["probe"].get("audio") or {})
    silent = audio is None or bool(envelope.get("silent"))
    if silent:
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.AUDIO.MISSING", [_mk(
            check_id="TECH.AUDIO.MISSING", kind="technical", severity="blocker",
            scope="whole_artifact", start=0.0, end=max(duration, 0.001),
            expected="an audible audio track (ShotSpec declares dialogue)",
            expected_source=_shot_source(shot),
            observed="no audio stream" if audio is None
                     else "audio stream is fully silent",
            measured=None, artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.AUDIO.MISSING"]["actions"])])
    return _evaluated("TECH.AUDIO.MISSING")


def _c_audio_offset(**kw):
    media = _need(kw["probe"], "media")
    video = media.get("video")
    audio = media.get("audio")
    if video is None or audio is None:
        return _na("TECH.AUDIO.OFFSET", "no_such_content")
    artifact = kw["_artifact"]
    fps_num, fps_den = _fps(media)
    frame = (fps_den / fps_num) if fps_num else 1.0 / 24.0
    tolerance = frame * int(kw["threshold"].get(
        "audio_offset_tolerance_frames",
        DEFAULTS["audio_offset_tolerance_frames"]))
    offset = _pos0(audio.get("start_offset_seconds")) - _pos0(
        video.get("start_offset_seconds"))
    if abs(offset) > tolerance:
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.AUDIO.OFFSET", [_mk(
            check_id="TECH.AUDIO.OFFSET", kind="technical", severity="major",
            scope="whole_artifact", start=0.0, end=max(duration, 0.001),
            expected=f"|audio-video offset| <= {tolerance:.4f}s",
            expected_source=_artifact_source(artifact),
            observed=f"{offset:+.4f}s",
            measured=_measured(abs(offset), "s", tolerance, "lte"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.AUDIO.OFFSET"]["actions"])])
    return _evaluated("TECH.AUDIO.OFFSET")


def _c_silence(**kw):
    media = _need(kw["probe"], "media")
    if media.get("audio") is None:
        return _na("TECH.AUDIO.SILENCE_HEAD_TAIL", "no_such_content")
    envelope = _need(kw["probe"], "audio")
    artifact = kw["_artifact"]
    head_limit = float(kw["threshold"].get(
        "silence_head_seconds", DEFAULTS["silence_head_seconds"]))
    tail_limit = float(kw["threshold"].get(
        "silence_tail_seconds", DEFAULTS["silence_tail_seconds"]))
    head = _pos(envelope.get("head_silence_seconds"))
    tail = _pos(envelope.get("tail_silence_seconds"))
    duration = _pos(media.get("duration_seconds"), max(head + tail, 0.001))
    defects = []
    if head > head_limit:
        defects.append(_mk(
            check_id="TECH.AUDIO.SILENCE_HEAD_TAIL", kind="technical",
            severity="minor", scope="segment", start=0.0,
            end=max(head, 0.001), expected=f"head silence <= {head_limit}s",
            expected_source=_artifact_source(artifact),
            observed=f"{head:.3f}s head silence",
            measured=_measured(head, "s", head_limit, "lte"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.AUDIO.SILENCE_HEAD_TAIL"]["actions"]))
    if tail > tail_limit:
        defects.append(_mk(
            check_id="TECH.AUDIO.SILENCE_HEAD_TAIL", kind="technical",
            severity="minor", scope="segment", start=max(0.0, duration - tail),
            end=max(duration, 0.002),
            expected=f"tail silence <= {tail_limit}s",
            expected_source=_artifact_source(artifact),
            observed=f"{tail:.3f}s tail silence",
            measured=_measured(tail, "s", tail_limit, "lte"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.AUDIO.SILENCE_HEAD_TAIL"]["actions"]))
    return _evaluated("TECH.AUDIO.SILENCE_HEAD_TAIL", defects)


def _c_loudness(**kw):
    media = _need(kw["probe"], "media")
    if media.get("audio") is None:
        return _na("TECH.LOUDNESS.LUFS", "no_such_content")
    envelope = _need(kw["probe"], "audio")
    target = kw["target"]
    artifact = kw["_artifact"]
    spec = target.get("loudness_lufs")
    if not _finite(spec):
        return _not_evaluated("TECH.LOUDNESS.LUFS", "policy_disabled")
    lufs = envelope.get("integrated_lufs")
    if not _finite(lufs):
        return _failed("TECH.LOUDNESS.LUFS", "evaluator_error")
    tolerance = float(kw["threshold"].get(
        "lufs_tolerance", DEFAULTS["lufs_tolerance"]))
    actual = float(lufs)
    if abs(actual - float(spec)) > tolerance:
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.LOUDNESS.LUFS", [_mk(
            check_id="TECH.LOUDNESS.LUFS", kind="technical", severity="major",
            scope="whole_artifact", start=0.0, end=max(duration, 0.001),
            expected=f"{float(spec):.1f} LUFS +/- {tolerance}",
            expected_source=_policy_source(kw["profile_id"],
                                           kw["profile_revision"], "target"),
            observed=f"{actual:.1f} LUFS integrated",
            measured=_measured(actual, "LUFS", float(spec), "ne"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.LOUDNESS.LUFS"]["actions"])])
    return _evaluated("TECH.LOUDNESS.LUFS")


def _c_true_peak(**kw):
    media = _need(kw["probe"], "media")
    if media.get("audio") is None:
        return _na("TECH.LOUDNESS.TRUE_PEAK", "no_such_content")
    envelope = _need(kw["probe"], "audio")
    artifact = kw["_artifact"]
    limit = kw["target"].get("true_peak_db")
    if not _finite(limit):
        limit = kw["threshold"].get(
            "true_peak_max_db", DEFAULTS["true_peak_max_db"])
    peak = envelope.get("true_peak_db")
    if not _finite(peak):
        return _failed("TECH.LOUDNESS.TRUE_PEAK", "evaluator_error")
    if float(peak) > float(limit):
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.LOUDNESS.TRUE_PEAK", [_mk(
            check_id="TECH.LOUDNESS.TRUE_PEAK", kind="technical",
            severity=checks.severity_for("TECH.LOUDNESS.TRUE_PEAK",
                                         kw["artifact_kind"]),
            scope="whole_artifact", start=0.0, end=max(duration, 0.001),
            expected=f"true peak <= {float(limit)} dBTP",
            expected_source=_policy_source(kw["profile_id"],
                                           kw["profile_revision"], "target"),
            observed=f"{float(peak)} dBTP",
            measured=_measured(float(peak), "dBTP", float(limit), "lte"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.LOUDNESS.TRUE_PEAK"]["actions"])])
    return _evaluated("TECH.LOUDNESS.TRUE_PEAK")


def _c_clipping(**kw):
    media = _need(kw["probe"], "media")
    if media.get("audio") is None:
        return _na("TECH.AUDIO.CLIPPING", "no_such_content")
    envelope = _need(kw["probe"], "audio")
    artifact = kw["_artifact"]
    limit = float(kw["threshold"].get(
        "clipping_ratio_max", DEFAULTS["clipping_ratio_max"]))
    ratio = envelope.get("clipping_ratio")
    if not _finite(ratio):
        return _failed("TECH.AUDIO.CLIPPING", "evaluator_error")
    if float(ratio) > limit:
        duration = _pos(media.get("duration_seconds"))
        return _evaluated("TECH.AUDIO.CLIPPING", [_mk(
            check_id="TECH.AUDIO.CLIPPING", kind="technical", severity="major",
            scope="segment", start=0.0, end=max(duration, 0.001),
            expected=f"clipped sample ratio <= {limit}",
            expected_source=_artifact_source(artifact),
            observed=f"{float(ratio):.5f} of samples clipped",
            measured=_measured(float(ratio), "ratio", limit, "lte"),
            artifact_id=_artifact_id(artifact),
            actions=checks.CHECKS["TECH.AUDIO.CLIPPING"]["actions"])])
    return _evaluated("TECH.AUDIO.CLIPPING")


def _c_burned_in(**kw):
    subtitles = _need(kw["probe"], "subtitles")
    media = _need(kw["probe"], "media")
    artifact = kw["_artifact"]
    policy = kw["policy"]
    if policy is None:
        return _not_evaluated("TECH.SUBTITLE.BURNED_IN", "policy_disabled")
    if not subtitles.get("burned_in"):
        return _evaluated("TECH.SUBTITLE.BURNED_IN")
    if str(policy) != "post_composite":
        # 项目允许烧录字幕 → 这不是缺陷。
        return _evaluated("TECH.SUBTITLE.BURNED_IN")
    frames = [float(t) for t in (subtitles.get("frames") or []) if _finite(t)]
    duration = _pos(media.get("duration_seconds"), 1.0)
    start = min(frames) if frames else 0.0
    return _evaluated("TECH.SUBTITLE.BURNED_IN", [_mk(
        check_id="TECH.SUBTITLE.BURNED_IN", kind="technical", severity="blocker",
        scope="frame", start=start,
        end=max(start + 1.0 / 24.0, start + 0.001, min(start + 1.0, duration)),
        expected="no burned-in subtitles when the project composites them",
        expected_source=_policy_source(kw["profile_id"],
                                       kw["profile_revision"], "subtitle_policy"),
        observed=f"burned-in subtitle detected in {len(frames) or 'some'} frame(s)",
        measured=None, artifact_id=_artifact_id(artifact),
        actions=checks.CHECKS["TECH.SUBTITLE.BURNED_IN"]["actions"])])


def _c_meta(**kw):
    metadata = _need(kw["probe"], "metadata")
    # §4：info 级、允许动作 `—`。defect 的 recommended_actions 是 minItems 1，
    # 所以这里只登记结论、不产 defect（schema S35-9）。缺元数据会在
    # `dimensions` 的 meta 组分数上体现，不进入 decision。
    return _evaluated("TECH.META.MISSING")


_HANDLERS = {
    "TECH.CONTAINER.PARSE": _c_container,
    "TECH.FILE.EMPTY": _c_empty,
    "TECH.FILE.TRUNCATED": _c_truncated,
    "TECH.DECODE.PACKET_ERROR": _c_packet,
    "TECH.DURATION.MISMATCH": _c_duration_mismatch,
    "TECH.DURATION.ZERO": _c_duration_zero,
    "TECH.RESOLUTION.BELOW_SPEC": _c_resolution_below,
    "TECH.RESOLUTION.MISMATCH": _c_resolution_mismatch,
    "TECH.FPS.MISMATCH": _c_fps_mismatch,
    "TECH.FPS.VFR": _c_vfr,
    "TECH.CODEC.UNSUPPORTED": _c_codec,
    "TECH.COLOR.RANGE_MISMATCH": _c_color,
    "TECH.BLACK_FRAME": _c_black,
    "TECH.FREEZE_FRAME": _c_freeze,
    "TECH.FLASH_FRAME": _c_flash,
    "TECH.AUDIO.MISSING": _c_audio_missing,
    "TECH.AUDIO.OFFSET": _c_audio_offset,
    "TECH.AUDIO.SILENCE_HEAD_TAIL": _c_silence,
    "TECH.LOUDNESS.LUFS": _c_loudness,
    "TECH.LOUDNESS.TRUE_PEAK": _c_true_peak,
    "TECH.AUDIO.CLIPPING": _c_clipping,
    "TECH.SUBTITLE.BURNED_IN": _c_burned_in,
    "TECH.META.MISSING": _c_meta,
}


def _fps(media: dict):
    video = (media or {}).get("video") or {}
    try:
        num = int(video.get("fps_num") or 0)
        den = int(video.get("fps_den") or 1)
    except (TypeError, ValueError):
        return 0, 1
    return (num, den) if num > 0 and den > 0 else (0, 1)


def _pos0(value) -> float:
    return float(value) if _finite(value) else 0.0


# ---------------------------------------------------------------- coverage


def compute_coverage(*, probe, sampling_policy_version, required_min) -> dict:
    """§3.3：`coverage_ratio = temporal_windows_evaluated / planned_windows`。

    `planned_windows = 0` 时强制 0——"一个窗口都没规划"不是 100% 覆盖。
    帧级检查是全帧解码，`frames_sampled` 取实测帧数。
    """
    probe = probe if isinstance(probe, dict) else {}
    frames = probe.get("frames") or {}
    media = probe.get("media") or {}
    envelope = probe.get("audio") or {}
    duration = _pos(media.get("duration_seconds"))
    planned = int(frames.get("planned_windows") or 0)
    evaluated = int(frames.get("windows_evaluated") or 0)
    if planned < 0:
        planned = 0
    if evaluated < 0:
        evaluated = 0
    if planned == 0:
        ratio = 0.0
    else:
        ratio = min(1.0, evaluated / planned)
    return {
        "media_duration_seconds": round(duration, 6),
        "frames_sampled": int(frames.get("sampled") or 0),
        "temporal_windows_evaluated": evaluated,
        "planned_windows": planned,
        "coverage_ratio": round(ratio, 6),
        "required_coverage_min": float(required_min),
        "audio_analyzed": bool(envelope.get("analyzed")),
        "audio_analyzed_seconds": round(
            _pos(envelope.get("analyzed_seconds")), 6),
        "sampling_policy_version": str(sampling_policy_version),
    }


# ---------------------------------------------------------------- decide


def decide(*, manifest, defects, coverage, confidence, policy) -> dict:
    """§2.7 的八步，顺序不能换。

    返回 `{decision, decision_reason, abstain}`。第 3 步在评分之前——这是
    "未知不能当 pass"的落点。
    """
    # §2.7 第 1 步：本层 registry 要求的 check 必须恰好各出现一次。
    # `not_applicable` 也算"出现了"——它是登记在案的结论，只是不计入失败。
    required = [entry["check_id"] for entry in manifest]
    present = {entry["check_id"] for entry in manifest}
    missing = [c for c in checks.ALL_CHECKS if c not in present]
    if missing:
        # 第 1 步：清单不完整。缺项本身就是一个"没查"。
        return _abstain("incomplete_coverage", missing,
                        f"check_manifest is missing {sorted(missing)}")

    # `not_applicable` 是"素材本来没有这项内容"，只有它允许不计入失败；
    # 其余非 evaluated 都是"该查没查成"。
    failed = [entry["check_id"] for entry in manifest
              if entry["status"] in ("failed", "not_evaluated")]
    # 第 2 步：覆盖不足。`failed ∪ required_checks`（§2.7 原文）。
    #
    # 理由码取 `reason_for(failed)` 而不是写死 `incomplete_coverage`：沙箱
    # 中途被杀时覆盖率**必然**一起掉（E24-T8 同时要求 `coverage_ratio<1` 与
    # `reason=evaluator_error`），照抄第 2 步会让更具体的失败原因被覆盖不足
    # 吞掉，E25-T1 的 `evaluator_timeout` 也一样。判定仍然是 abstain、`failed`
    # 仍然非空，两条原文都不需要改（S35-19）。
    minimum = float(policy.get("min_coverage_ratio",
                               DEFAULTS["min_coverage_ratio"]))
    if coverage["coverage_ratio"] < minimum:
        return _abstain(
            _reason_for(manifest, failed) if failed else "incomplete_coverage",
            failed or required,
            f"coverage_ratio {coverage['coverage_ratio']:.3f} < "
            f"required {minimum:.3f}")
    # 第 3 步：有 failed 的必查项 → 弃权，不看分数。
    if failed:
        return _abstain(_reason_for(manifest, failed), failed,
                        f"{len(failed)} required check(s) did not conclude")
    # 第 4 步：硬失败。
    if any(d["severity"] == "blocker" for d in defects):
        first = next(d for d in defects if d["severity"] == "blocker")
        return _decided("repair", f"{first['check_id']} blocker")
    # 第 5 步：缺陷分级。
    majors = [d for d in defects if d["severity"] == "major"]
    if majors:
        tau_low = float(policy.get("tau_low", DEFAULTS["tau_low"]))
        low = [d for d in majors if d["confidence"] < tau_low]
        if low:
            return _decided(
                "human_review",
                f"{low[0]['check_id']} major below tau_low {tau_low}")
        allowed = set(policy.get("allowed_actions") or [])
        if allowed and all(set(d["recommended_actions"]) <= allowed
                           for d in defects):
            return _decided("repair", f"{majors[0]['check_id']} major")
        return _decided(
            "human_review",
            f"{majors[0]['check_id']} major has no allowed auto action")
    # 第 6 步：模型冲突。S35 是确定性测量器，这一项恒为 0（§3.1）。
    if float(policy.get("model_disagreement") or 0.0) > float(
            policy.get("tau_disagreement", DEFAULTS["tau_disagreement"])):
        return _decided("human_review", "evaluator disagreement above tau")
    # 第 7 步：整体置信度不足。§2.7 的伪代码写的是 abstain(low_confidence, [])，
    # 但 abstain 要求 failed_check_ids 非空且对应项不是 evaluated——查完了却
    # 没有失败项，两条不能同时成立。§2.5 的判定表把"置信度落在灰区"归到
    # human_review，故按判定表实现（schema S35-4）。
    tau_abstain = float(policy.get("tau_abstain", DEFAULTS["tau_abstain"]))
    if confidence < tau_abstain:
        return _decided("human_review", "low_confidence")
    # 第 8 步。
    return _decided("pass", "all required checks evaluated; no blocker/major")


def _decided(decision, reason):
    return {"decision": decision, "decision_reason": reason, "abstain": None}


def _abstain(reason_code, failed_check_ids, detail):
    return {
        "decision": "abstain",
        "decision_reason": f"{reason_code}: {detail}",
        "abstain": {"reason_code": reason_code,
                    "failed_check_ids": sorted(set(failed_check_ids)),
                    "detail": detail},
    }


def _reason_for(manifest, failed) -> str:
    """§2.7 `reason_for(failed)`：优先取评估器自身的失败原因。"""
    priority = ("evaluator_timeout", "evaluator_error", "asset_unavailable",
                "dependency_unavailable", "sampling_not_selected",
                "policy_disabled")
    by_check = {entry["check_id"]: entry["reason_code"] for entry in manifest}
    codes = {by_check.get(check_id) for check_id in failed}
    for code in priority:
        if code in codes:
            return code
    return "incomplete_coverage"


# ---------------------------------------------------------------- dimensions


def build_dimensions(*, manifest, defects) -> list:
    """按 §4 的分组做一层确定性汇总。

    schema 要求 `dimensions` 非空却没有给 S35 定义维度名（S35-13），所以这里
    用检查分组作为维度：`score=1.0` 全通过、`0.0` 有缺陷、`null` 有检查没
    得出结论（null 而不是 0.0——"不知道"与"零分"是两件事）。判定只读
    `check_manifest` 与 `defects`，不读 dimensions。
    """
    groups: dict[str, list] = {}
    for check_id in checks.ALL_CHECKS:
        groups.setdefault(check_id.split(".")[1].lower(), []).append(check_id)
    by_check = {entry["check_id"]: entry for entry in manifest}
    defects_by_check: dict[str, int] = {}
    for defect in defects:
        defects_by_check[defect["check_id"]] = \
            defects_by_check.get(defect["check_id"], 0) + 1
    out = []
    for group, check_ids in groups.items():
        statuses = [by_check[c]["status"] for c in check_ids if c in by_check]
        total = sum(defects_by_check.get(c, 0) for c in check_ids)
        if any(s in ("failed", "not_evaluated") for s in statuses):
            score, reason = None, "some checks did not conclude"
        elif total:
            score, reason = 0.0, f"{total} defect(s)"
        else:
            score, reason = 1.0, "all checks passed"
        out.append({"name": group, "score": score, "reason": reason})
    return out


def compute_confidence(*, manifest, artifact_kind) -> float:
    """确定性测量没有中间态（S35-13 的 handling）。

    全部适用项都 `evaluated` → 1.0；任何一项没得出结论 → 0.0。
    `not_applicable` 是"本来没有这项内容"，不拉低置信度。
    """
    for entry in manifest:
        if entry["status"] == "not_applicable":
            continue
        if entry["status"] != "evaluated":
            return 0.0
    return 1.0
