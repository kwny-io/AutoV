"""S37 内核。方案：`plans/S37-audio-alignment.md`。

一句话边界：**S37 测量并报告，不修复、不改稿、不进接受指针。**

它回答三个彼此独立的问题，且**分别出证据**（service.yaml acceptance：
「错读/漏词与字幕偏移有独立证据」）：

| 问题 | 产出 | 消费方 |
|---|---|---|
| 说的词对不对 | `AUD.TEXT.*` → `voice` 缺陷 | S39 |
| 每个字在什么时间 | `alignment_unit` + 置信区间 | S38 / S32 |
| 嘴和声音对不对得上 | `AUD.LIPSYNC.*` → `lipsync` 缺陷 | S39 |

三条流**绝不合成一个音频得分**：合成之后 S39 就选不出动作——重配、重对齐、
重做口型是三件不同的事（§1 首表）。所以 `defects` 里出现的是三种不同的
`type`，没有任何一个字段把三者加权成一个数。

## 时间基准

全链路只有一个：容器 presentation timeline（PTS），单位秒。`start_seconds`
相对该轨道第一个呈现时刻。`frame_index = round(presentation_seconds *
fps_num / fps_den)`。VFR 素材帧-时间映射不唯一 → 拒绝口型检查
（字段 `vfr_unsupported`），不硬算（§2.1）。

`known_audio_offset_seconds` 是 S35 已经报过的容器偏移。S37 先扣除它再评估，
不把同一个偏移再报一次。

## 上游分析文档（S26）

方案 §2 把 ASR / forced alignment / 说话人分离 / 口型 / 音色评估都放在 S26 的
understanding job 里，而 contracts 里没有这个产物形状（S37-5）。S37 因此把
**分析结果当成注入的输入文档**（`analysis_version: "s37-analysis.v1"`），
由 `analysis_source` 供给；未配置时命令 503，**绝不**产出"没有分析的空报告"。

文档形状（`_analysis` 校验后使用；缺哪一段，依赖它的检查就 `not_evaluated`）：

```python
{
  "analysis_version": "s37-analysis.v1",
  "audio": {"analyzed": bool, "analyzed_seconds": float,
            "duration_seconds": float, "track_hash": str,
            "stream_index": int, "silent": bool, "damaged": bool,
            "codec": str, "sample_rate_hz": int, "channels": int},
  "video": {"present": bool, "is_vfr": bool, "fps_num": int, "fps_den": int,
            "frame_count": int, "frames_evaluated": int, "planned_frames": int,
            "face_present": bool, "width": int, "height": int,
            "codec": str, "pix_fmt": str, "color_range": str},
  "asr": {"outcome": "ok"|"error"|"timeout", "latency_ms": int,
          "model_snapshot": {provider, model_id, snapshot},
          "model_version": str, "tokens_in": int, "tokens_out": int,
          "utterances": [{"utterance_id": str,
            "confidence": float,                       # 可选
            "script_tokens": [{"index": int, "text": str,
                               "char_offset": int, "char_length": int,
                               "pause_after": bool, "phonemes": [str]}],
            "tokens": [{"text": str, "start_seconds": float,
                        "end_seconds": float, "posterior": float,
                        "alignment_confidence": float,
                        "ci_low_seconds": float, "ci_high_seconds": float,
                        "script_index": int|None, "speaker_id": str,
                        "phonemes": [str]}]}]},
  "speaker_segments": [{"speaker_id": str, "start_seconds": float,
                        "end_seconds": float, "confidence": float}],
  "lipsync": {"outcome": "ok"|"error"|"timeout", "confidence": float,
              "offset_ms": float, "openness_correlation": float,
              "silent_mouth_motion": [{"start_seconds", "end_seconds"}],
              "speaks_closed": [{"start_seconds", "end_seconds"}]},
  "voice": {"confidence": float, "embedding_distance": float,
            "window_distances": [{"window_index", "start_seconds",
                                  "end_seconds", "distance"}],
            "cross_shot_distance": float|None,
            "emotion_match": float|None, "expected_emotion": str,
            "detected_emotion": str},
  "alignment": {"model_version": str},
}
```

这是**私有分析文档**，不是契约。把它升成契约、或并进 S26 的读面，由 owner
决定（AGENTS.md：契约冲突停下报告，不自行选边）。

## 顺序（决定永远排在分数之前）

1. 校验命令体（缺坐标就 422，不补默认值）。
2. 定位分析文档：`asr_cache` 命中就把缓存里的 ASR 交给 `analysis_source`，
   让真实上游跳过付费 ASR（§8；E26-A2）。
3. 逐项检查 → gates（无对白 / 坏音频 / ASR 失败 / 无视频 / VFR / 无脸 /
   S15 不可达）。
4. `decide()`：**有必查项没得出结论就先弃权**，再谈严重度。
5. 落库 + Outbox 同事务；报告与事件要么一起存在，要么都不存在。

## 两条结构性纪律的代码形态

- `execution_fence`（run/branch）与 `node_epoch`（单个 `logical_node`）是
  `report` 上的两列，分别写、分别读，**没有任何一处比较或换算**。本服务也
  不用它们推进接受指针——接受不在 S37（AGENTS.md）。
- 租户只来自平台签名的 `X-Tenant-Id`；请求体里的 `tenant_id` 只用于比对，
  不一致即 403。LLM/上游分析文档里的任何租户字段都不被读取。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Inbox, Outbox, new_event

from . import registry
from .schema import (
    ABSTAIN_REASON_CODES,
    CONTRACT_GAPS,
    DECISIONS,
    ID_PREFIX,
    SCHEMA,
)

SOURCE = "S37"
SCHEMA_VERSION = "2.0"
QC_LAYER = "alignment"
EVENT_COMPLETED = "quality.alignment.completed"
TOPIC_COMPLETED = "drama.audio-alignment.v2"

ANALYSIS_VERSION = "s37-analysis.v1"
EVALUATOR_NAME = "s37-audio-alignment"
EVALUATOR_VERSION = "2.0.0"
# 镜像摘要由部署填入；未填时用固定串而不是编一个 sha256。
EVALUATOR_IMAGE_DIGEST = "sha256:unresolved"

REQUIRED_CONTEXT = ("project_id", "series_id", "episode_id", "branch_id",
                    "home_cell", "execution_fence", "node_epoch", "node_id",
                    "policy_ref", "trace_id")
REQUIRED_TOP_LEVEL = ("run_id", "shot_lineage_id")

# §4：整体置信度低于这个值 → human_review（与 S35 同一阈值口径）。
TAU_LOW = 0.40

# §3.2 GAP 的默认间隙阈值。方案没给数值；0.3s 是「明显不是正常字间停顿」的
# 起点，真实值应由 S02 下发（S37-24）。
MAX_GAP_SECONDS = 0.3

# §3.3 OPENNESS 的最低相关系数。方案写「低于阈值」未给数值（S37-24）。
MIN_OPENNESS_CORRELATION = 0.2

# §3.4 音色距离阈值 τ。
VOICE_DISTANCE_TAU = 0.35

# §3.4 情绪匹配下限。
MIN_EMOTION_MATCH = 0.5


# ---------------------------------------------------------------- 基础工具

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _pos(value, default=0.0) -> float:
    return float(value) if _finite(value) and value >= 0 else default


def _b32(key_hex: str, length: int) -> str:
    raw = bytes.fromhex(key_hex)
    encoded = base64.b32encode(raw).decode("ascii").lower().rstrip("=")
    return encoded[:length]


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。不嵌套：一个命令一个事务。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------- §2.6 / §8 派生键

def derived_key(*, tenant_id, artifact_sha256, audio_track_hash, language,
                evaluator, policy_version, sampling_policy_version,
                attempt_slot_id) -> str:
    """§8：派生键**含 `audio_track_hash`**。

    「视频重新编码但音轨没变」时同一音轨仍应能复用 ASR 结果；但报告本身
    仍然挂在 `artifact_sha256` 上——因为报告还包含口型结论，而口型依赖画面。
    两个 hash 各司其职：`artifact_sha256` 决定**这份报告**是不是同一份，
    `audio_track_hash` 决定**ASR** 能不能复用。
    """
    return _sha256_hex(_jcs({
        "tenant_id": tenant_id,
        "artifact_sha256": artifact_sha256,
        "audio_track_hash": audio_track_hash,
        "qc_layer": QC_LAYER,
        "language": language,
        "evaluator.name": evaluator["name"],
        "evaluator.version": evaluator["version"],
        "evaluator.model_snapshot": evaluator.get("model_snapshot"),
        "policy_version": policy_version,
        "sampling_policy_version": sampling_policy_version,
        "repair_context.attempt_slot_id": attempt_slot_id,
    }))


def report_id_for(key_hex: str) -> str:
    return "qc_" + _b32(key_hex, 26)


def report_content_hash(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "report_content_hash"}
    return _sha256_hex(_jcs(body))


def asr_cache_key(*, tenant_id, audio_track_hash, asr_model_snapshot,
                  lexicon_revision, voice_binding_hash) -> str:
    """§8 的 S26 幂等键，按 QAC-3 补上词典与音色绑定（S37-4）。

    原键 `qc:s37:{audio_track_hash}:{utterance_id}:{asr_model_snapshot}` 里
    没有 `lexicon_revision`：换一版发音词典后同一段音频仍命中旧缓存，多音字
    会被判成上一版的读音。键里补两个摘要，缓存才会跟着词典/绑定换版。
    """
    return _sha256_hex(_jcs({
        "tenant_id": tenant_id,
        "audio_track_hash": audio_track_hash,
        "asr_model_snapshot": asr_model_snapshot,
        "lexicon_revision": lexicon_revision,
        "voice_binding_hash": voice_binding_hash,
    }))


def defect_fingerprint(*, check_id: str, kind: str, character_id,
                       severity: str, start_seconds: float) -> str:
    """与 artifact hash 无关的稳定身份，跨复检判"同类重复"（E26-A1）。

    不含 `observed`（措辞随版本变）、不含 `artifact_sha256`（跨 Run 追踪的
    基础）、不含精确 severity（blocker/major 抖动不该分裂指纹）。契约的
    pattern 是 `^[0-9a-f]{32}$`。
    """
    return _sha256_hex(_jcs({
        "qc_layer": QC_LAYER,
        "check_id": check_id,
        "type": kind,
        "character_id": character_id,
        "severity_bucket": "blocker" if severity == "blocker" else "non_blocker",
        "location_bucket": int(round(start_seconds / 0.5)),
    }))[:32]


def defect_id_for(fingerprint: str, index: int) -> str:
    return "df_" + _b32(_sha256_hex(f"{fingerprint}:{index}"), 26)


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


def _measured(value, unit, threshold_value, comparator):
    return {"value": float(value), "unit": unit,
            "threshold": (None if threshold_value is None
                          else float(threshold_value)),
            "comparator": comparator}


def _mk(*, check_id, kind, severity, scope, start, end, expected,
        expected_source, observed, measured, artifact_id, actions,
        confidence=1.0, character_id=None, region_bbox=None):
    """构造一条 defect。`detector` 恒为 deterministic。

    `confidence` 必须由调用方按 §4 第 1 条取**支撑证据里最弱的一环**，
    不是平均值——「多数证据高置信」不许抬高结论。
    """
    start = max(0.0, float(start))
    end = max(start + 1e-3, float(end))
    fingerprint = defect_fingerprint(
        check_id=check_id, kind=kind, character_id=character_id,
        severity=severity, start_seconds=start)
    return {
        "defect_fingerprint": fingerprint,
        "check_id": check_id,
        "type": kind,
        "severity": severity,
        "scope": scope,
        "time_range": {"start_seconds": round(start, 3),
                       "end_seconds": round(end, 3)},
        "region_bbox": region_bbox,
        "character_id": character_id,
        "expected": expected,
        "expected_source": expected_source,
        "observed": observed,
        "measured": measured,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "detector": "deterministic",
        "evidence_artifact_ids": [artifact_id],
        "recommended_actions": list(actions),
    }


def _source(service_id, ref_id, revision):
    return {"service_id": service_id, "ref_id": str(ref_id),
            "ref_revision": max(1, int(revision or 1))}


# ---------------------------------------------------------------- coverage

def _coverage(*, analysis, utterances, required_min, sampling_version) -> dict:
    """`coverage_ratio = temporal_windows_evaluated / planned_windows`。

    口型检查在时，分母**按帧**算（§3.3 末段）；否则按对白段算。`planned=0`
    时强制 0——"一个窗口都没规划"不是 100% 覆盖。
    """
    audio = analysis.get("audio") or {}
    video = analysis.get("video") or {}
    lipsync_planned = bool(video.get("present")) and any(
        c.startswith("AUD.LIPSYNC.") for c in registry.CHECK_IDS)
    if lipsync_planned:
        planned = max(0, int(video.get("planned_frames") or 0))
        evaluated = max(0, int(video.get("frames_evaluated") or 0))
        frames_sampled = evaluated
    else:
        planned = len(utterances)
        evaluated = sum(1 for u in utterances
                        if (u.get("tokens") or []))
        frames_sampled = 0
    ratio = 0.0 if planned == 0 else min(1.0, evaluated / planned)
    duration = (_pos(audio.get("duration_seconds"))
                or _pos(audio.get("analyzed_seconds")))
    return {
        "media_duration_seconds": round(duration, 6),
        "frames_sampled": int(frames_sampled),
        "temporal_windows_evaluated": int(evaluated),
        "planned_windows": int(planned),
        "coverage_ratio": round(ratio, 6),
        "required_coverage_min": float(required_min),
        "audio_analyzed": bool(audio.get("analyzed")),
        "audio_analyzed_seconds": round(_pos(audio.get("analyzed_seconds")), 6),
        "sampling_policy_version": str(sampling_version),
    }


# ---------------------------------------------------------------- decide

def decide(*, manifest, defects, coverage, confidence) -> dict:
    """判定。**顺序不能换**：第 3 步在严重度之前。

    S37 没有 S02 策略源（service.yaml 的 depends_on 只有 S28/S26/S15），
    所以 major 一律 human_review——本服务没有"自动修复已被授权"的信息，
    不替 S39 决定（S37-22）。
    """
    required = [entry["check_id"] for entry in manifest]
    present = {entry["check_id"] for entry in manifest}
    missing = [c for c in required if c not in present]
    if missing:
        return _abstain("incomplete_coverage", missing,
                        f"check_manifest is missing {sorted(missing)}")

    failed = [entry["check_id"] for entry in manifest
              if entry["status"] in ("failed", "not_evaluated")]
    minimum = float(coverage["required_coverage_min"])
    if coverage["coverage_ratio"] < minimum:
        return _abstain(
            _reason_for(manifest, failed) if failed else "incomplete_coverage",
            failed or required,
            f"coverage_ratio {coverage['coverage_ratio']:.3f} < "
            f"required {minimum:.3f}")
    if failed:
        return _abstain(_reason_for(manifest, failed), failed,
                        f"{len(failed)} required check(s) did not conclude")

    blockers = [d for d in defects if d["severity"] == "blocker"]
    if blockers:
        # `never_auto_repair` 的 blocker 只能人审：自动动作会再犯同一个错误
        # （音色授权撤销后重合成仍用同一音色）。
        auto = [d for d in blockers
                if not registry.BY_ID[d["check_id"]].get("never_auto_repair")]
        if auto:
            return _decided("repair",
                            f"{auto[0]['check_id']} blocker")
        return _decided("human_review",
                        f"{blockers[0]['check_id']} blocker needs a human")

    majors = [d for d in defects if d["severity"] == "major"]
    if majors:
        low = [d for d in majors if d["confidence"] < TAU_LOW]
        if low:
            return _decided("human_review",
                            f"{low[0]['check_id']} major below tau_low")
        return _decided("human_review",
                        f"{majors[0]['check_id']} major has no authorized "
                        f"automatic route in S37")
    if confidence < TAU_LOW:
        return _decided("human_review", "low_confidence")
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
    """把 manifest 的 reason_code 折成 `abstain.reason_code` 的 7 值闭集。

    `abstain` 的理由集与 manifest 的理由集不同名：manifest 说
    `dependency_unavailable`，abstain 只能写 `dependency_stale`（"依赖的
    那份状态过期/读不到"）。直接透传会让报告过不了 `qc-report.v2` 的校验，
    而这里恰好是最容易写错的地方——一个依赖读不到，abstain 的理由却成了
    别的码。
    """
    priority = ("evaluator_timeout", "evaluator_error", "asset_unavailable",
                "dependency_unavailable", "sampling_not_selected",
                "policy_disabled")
    to_abstain = {
        "evaluator_timeout": "evaluator_timeout",
        "evaluator_error": "evaluator_error",
        "asset_unavailable": "asset_unavailable",
        "dependency_unavailable": "dependency_stale",
    }
    by_check = {entry["check_id"]: entry["reason_code"] for entry in manifest}
    codes = {by_check.get(check_id) for check_id in failed}
    for code in priority:
        if code in codes:
            return to_abstain.get(code, "incomplete_coverage")
    return "incomplete_coverage"


def build_dimensions(*, manifest, defects, observations) -> list:
    """`dimensions` 非空。分组分数只是可读汇总，判定不读它。

    观测型结论（`AUD.TEXT.SCRIPT_CONFLICT`）单独成一条 dimension：它不进
    `defects`，但"ASR 听到的和脚本写的不一样"这件事必须留在报告里（E25-A3）。
    """
    groups: dict[str, list] = {}
    for check_id in registry.CHECK_IDS:
        groups.setdefault(check_id.split(".")[1].lower(), []).append(check_id)
    by_check = {entry["check_id"]: entry for entry in manifest}
    defects_by_check: dict[str, int] = {}
    for defect in defects:
        defects_by_check[defect["check_id"]] = \
            defects_by_check.get(defect["check_id"], 0) + 1
    out = []
    for group, check_ids in groups.items():
        present = [c for c in check_ids if c in by_check]
        if not present:
            continue
        statuses = [by_check[c]["status"] for c in present]
        total = sum(defects_by_check.get(c, 0) for c in present)
        if any(s in ("failed", "not_evaluated") for s in statuses):
            score, reason = None, "some checks did not conclude"
        elif total:
            score, reason = 0.0, f"{total} defect(s)"
        elif all(s == "not_applicable" for s in statuses):
            score, reason = None, "no applicable content"
        else:
            score, reason = 1.0, "all checks passed"
        out.append({"name": group, "score": score, "reason": reason})
    out.extend(observations)
    if not out:
        # schema 要求 minItems 1；清单非空时不会走到这里，但空报告不许过。
        out.append({"name": "alignment", "score": None,
                    "reason": "no check produced a conclusion"})
    return out


def compute_confidence(*, manifest) -> float:
    """全部适用项 `evaluated` → 1.0；任何一项没得出结论 → 0.0。

    `not_applicable` 是"本来没有这项内容"，不拉低置信度。
    """
    for entry in manifest:
        if entry["status"] == "not_applicable":
            continue
        if entry["status"] != "evaluated":
            return 0.0
    return 1.0


# ---------------------------------------------------------------- 逐项检查

def _derive_script_tokens(text: str) -> list[dict]:
    """没有 `script_tokens` 时按字符切。

    中文按字切是对的（一个字一个发音单元）；拉丁文本按空格切更合理，所以
    连续的非空白 ASCII 字母算一个 token。两种都能给出 `char_offset` /
    `char_length`，因为 S38 需要的是"第几个字"而不是"第几个词"。
    """
    out = []
    index = 0
    position = 0
    while position < len(text):
        char = text[position]
        if char.isspace():
            position += 1
            continue
        if ord(char) < 128 and char.isalnum():
            start = position
            while (position < len(text) and ord(text[position]) < 128
                   and text[position].isalnum()):
                position += 1
            out.append({"index": index, "text": text[start:position],
                        "char_offset": start,
                        "char_length": position - start,
                        "pause_after": False, "phonemes": None})
        else:
            out.append({"index": index, "text": char, "char_offset": position,
                        "char_length": 1, "pause_after": False,
                        "phonemes": None})
            position += 1
        index += 1
    return out


def _utterance_view(dialogue_utterance: dict, analysis: dict) -> dict:
    """把命令体的对白与分析文档的同一条 utterance 拼成一个视图。

    `script_tokens` 只从命令体/分析文档拿，**不向 S06 回读**（S37-17）：
    本服务不拥有对白文本，比对结论永远表述成「ASR 听到的 vs 这次命令
    声明的文本」。
    """
    found = None
    utterance_id = dialogue_utterance.get("utterance_id")
    for candidate in ((analysis.get("asr") or {}).get("utterances") or []):
        if candidate.get("utterance_id") == utterance_id:
            found = candidate
            break
    found = found if isinstance(found, dict) else {}
    script_tokens = found.get("script_tokens")
    if not isinstance(script_tokens, list) or not script_tokens:
        script_tokens = _derive_script_tokens(
            str(dialogue_utterance.get("text") or ""))
    return {
        "utterance_id": utterance_id,
        "speaker_id": dialogue_utterance.get("speaker_character_id"),
        "text": str(dialogue_utterance.get("text") or ""),
        "text_revision": dialogue_utterance.get("text_revision"),
        "critical": bool(dialogue_utterance.get("critical")),
        "expected_duration_ms": dialogue_utterance.get("expected_duration_ms"),
        "script_tokens": script_tokens,
        "tokens": [t for t in (found.get("tokens") or [])
                   if isinstance(t, dict)],
        "confidence": found.get("confidence"),
        "analysis_present": bool(found),
    }


def _shift(seconds, offset: float) -> float:
    return max(0.0, float(seconds) - float(offset))


def _tokens_shifted(utterance: dict, offset: float) -> list[dict]:
    out = []
    for token in utterance["tokens"]:
        item = dict(token)
        item["start_seconds"] = _shift(_pos(token.get("start_seconds")), offset)
        item["end_seconds"] = _shift(_pos(token.get("end_seconds")), offset)
        if item["end_seconds"] <= item["start_seconds"]:
            item["end_seconds"] = item["start_seconds"] + 1e-3
        item["posterior"] = float(token.get("posterior")
                                  if _finite(token.get("posterior")) else 0.0)
        item["alignment_confidence"] = float(
            token.get("alignment_confidence")
            if _finite(token.get("alignment_confidence")) else 0.0)
        out.append(item)
    return out


class _Ctx:
    """一次检查需要的全部输入。**没有网络**：全部来自已取到的文档。"""

    def __init__(self, *, tenant_id, analysis, utterances, artifact,
                 shot, offset, authorization, policy_version, lexicon_ref,
                 voice_ref):
        self.tenant_id = tenant_id
        self.analysis = analysis
        self.utterances = utterances
        self.artifact = artifact
        self.shot = shot
        self.offset = offset
        self.authorization = authorization
        self.policy_version = policy_version
        # 缺陷的 `expected_source` 要指向**词典修订**或**音色绑定修订**，
        # 而不是本服务自己。这里存的是命令体钉住的那两个 ref。
        lexicon_ref = lexicon_ref if isinstance(lexicon_ref, dict) else {}
        voice_ref = voice_ref if isinstance(voice_ref, dict) else {}
        self.lexicon_id = str(lexicon_ref.get("id") or "lexicon")
        self.lexicon_revision = int(lexicon_ref.get("revision") or 1)
        self.voice_id = str(voice_ref.get("id") or "voice")
        self.voice_revision = int(voice_ref.get("revision") or 1)

    def tokens(self):
        return [(u, t) for u in self.utterances
                for t in _tokens_shifted(u, self.offset)]

    def artifact_ref(self):
        return _source("S28", self.artifact["id"], self.artifact["revision"])

    def shot_ref(self):
        return _source("S06", self.shot["id"], self.shot["revision"])

    def actions(self, check_id):
        return registry.BY_ID[check_id]["allowed_actions"]

    def scope(self, check_id):
        return registry.report_scope(registry.BY_ID[check_id]["window_scope"])


def _speech_span(ctx: "_Ctx") -> tuple[float, float]:
    """对白段的整体时间范围。`time_range` 覆盖全对白段（E24-A1 要求）。"""
    pairs = ctx.tokens()
    if not pairs:
        return 0.0, 0.001
    start = min(t["start_seconds"] for _, t in pairs)
    end = max(t["end_seconds"] for _, t in pairs)
    return start, max(end, start + 1e-3)


def _check_missing_word(ctx: _Ctx) -> dict:
    defects = []
    for utterance in ctx.utterances:
        covered = {t.get("script_index") for t in
                   _tokens_shifted(utterance, ctx.offset)
                   if isinstance(t.get("script_index"), int)}
        for script_token in utterance["script_tokens"]:
            if script_token["index"] in covered:
                continue
            # 定位：用脚本 token 在 utterance 内的时间比例做估计？不行——
            # 没有音频证据就没有时间。用相邻已对齐 token 的边界，取一个
            # 最小区间，并在 observed 里说清这是"脚本里存在、ASR 未覆盖"。
            neighbours = [t for t in _tokens_shifted(utterance, ctx.offset)
                          if isinstance(t.get("script_index"), int)
                          and t["script_index"] < script_token["index"]]
            anchor = (neighbours[-1]["end_seconds"] if neighbours else
                      _speech_span(ctx)[0])
            defects.append(_mk(
                check_id="AUD.TEXT.MISSING_WORD", kind="voice",
                severity="major", scope=ctx.scope("AUD.TEXT.MISSING_WORD"),
                start=anchor, end=anchor + 0.1,
                expected=f"script token {script_token['text']!r} at "
                         f"char_offset {script_token['char_offset']} is spoken",
                expected_source=ctx.shot_ref(),
                observed=f"ASR produced no token for script index "
                         f"{script_token['index']} ({script_token['text']!r}) "
                         f"in utterance {utterance['utterance_id']}",
                measured=None, artifact_id=ctx.artifact["id"],
                actions=ctx.actions("AUD.TEXT.MISSING_WORD"),
                # §4 第 1 条：证据链最弱环节。缺词只要 ASR 覆盖到了该段，
                # 支撑证据就是 ASR 的 token posterior。
                confidence=min([t["posterior"] for t in
                                _tokens_shifted(utterance, ctx.offset)]
                               or [0.0]) if utterance["tokens"] else 0.0,
                character_id=utterance["speaker_id"]))
    return _evaluated("AUD.TEXT.MISSING_WORD", defects)


def _check_extra_word(ctx: _Ctx) -> dict:
    defects = []
    for utterance, token in ctx.tokens():
        length = len(utterance["script_tokens"])
        index = token.get("script_index")
        if isinstance(index, int) and 0 <= index < length:
            continue
        defects.append(_mk(
            check_id="AUD.TEXT.EXTRA_WORD", kind="voice",
            severity="major", scope=ctx.scope("AUD.TEXT.EXTRA_WORD"),
            start=token["start_seconds"], end=token["end_seconds"],
            expected=f"only the {length} script token(s) of utterance "
                     f"{utterance['utterance_id']} are spoken",
            expected_source=ctx.shot_ref(),
            observed=f"ASR token {token.get('text')!r} has no script position",
            measured=None, artifact_id=ctx.artifact["id"],
            actions=ctx.actions("AUD.TEXT.EXTRA_WORD"),
            confidence=token["posterior"],
            character_id=utterance["speaker_id"]))
    return _evaluated("AUD.TEXT.EXTRA_WORD", defects)


def _check_substitution(ctx: _Ctx) -> dict:
    defects = []
    for utterance, token in ctx.tokens():
        index = token.get("script_index")
        if not isinstance(index, int):
            continue
        if not 0 <= index < len(utterance["script_tokens"]):
            continue
        script = utterance["script_tokens"][index]
        if str(token.get("text") or "") == str(script["text"]):
            continue
        severity = "blocker" if utterance["critical"] else "major"
        severity = registry.severity_after(
            registry.BY_ID["AUD.TEXT.SUBSTITUTION"]["severity_default"],
            severity,
            registry.BY_ID["AUD.TEXT.SUBSTITUTION"]["severity_max"])
        defects.append(_mk(
            check_id="AUD.TEXT.SUBSTITUTION", kind="voice",
            severity=severity, scope=ctx.scope("AUD.TEXT.SUBSTITUTION"),
            start=token["start_seconds"], end=token["end_seconds"],
            expected=f"script says {script['text']!r}",
            expected_source=ctx.shot_ref(),
            observed=f"ASR transcribes {token.get('text')!r} at the same "
                     f"position (utterance {utterance['utterance_id']}, "
                     f"text_revision {utterance['text_revision']})",
            measured=None, artifact_id=ctx.artifact["id"],
            actions=ctx.actions("AUD.TEXT.SUBSTITUTION"),
            confidence=min(token["posterior"],
                           token["alignment_confidence"]),
            character_id=utterance["speaker_id"]))
    return _evaluated("AUD.TEXT.SUBSTITUTION", defects)


def _check_mispronounce(ctx: _Ctx) -> dict:
    """§4 第 2、3 条：置信度 < 0.5 的字不参与判定；跳过 > 10% 则整条 failed。"""
    defects = []
    judged = 0
    skipped = 0
    total = 0
    for utterance, token in ctx.tokens():
        index = token.get("script_index")
        if not isinstance(index, int) or not 0 <= index < len(
                utterance["script_tokens"]):
            continue
        total += 1
        script = utterance["script_tokens"][index]
        expected_phonemes = script.get("phonemes")
        observed_phonemes = token.get("phonemes")
        if token["alignment_confidence"] < registry.MIN_ALIGNMENT_CONFIDENCE:
            skipped += 1
            continue
        if not expected_phonemes or not observed_phonemes:
            skipped += 1
            continue
        judged += 1
        if list(expected_phonemes) == list(observed_phonemes):
            continue
        defects.append(_mk(
            check_id="AUD.TEXT.MISPRONOUNCE", kind="voice",
            severity="major", scope=ctx.scope("AUD.TEXT.MISPRONOUNCE"),
            start=token["start_seconds"], end=token["end_seconds"],
            expected=f"/{' '.join(expected_phonemes)}/ for "
                     f"{script['text']!r}",
            # 期望来源是 S15 的发音词典修订，不是 S06：多音字的读音由词典定。
            expected_source=_source("S15", ctx.lexicon_id, ctx.lexicon_revision),
            observed=f"/{' '.join(observed_phonemes)}/",
            measured=_measured(1.0, "phoneme_mismatch", None, "na"),
            artifact_id=ctx.artifact["id"],
            actions=ctx.actions("AUD.TEXT.MISPRONOUNCE"),
            confidence=min(token["posterior"], token["alignment_confidence"]),
            character_id=utterance["speaker_id"]))
    if total == 0:
        # 没有可判的字：有对白但一个字都没对齐上 → evaluator 没干活；
        # 没有对白 → 本来就没有这项内容（走 no-op 前的那道门）。
        return _failed("AUD.TEXT.MISPRONOUNCE", "evaluator_error") \
            if ctx.utterances else \
            _na("AUD.TEXT.MISPRONOUNCE", "no_such_content")
    if skipped / total > registry.MAX_UNJUDGED_TOKEN_RATIO:
        # §4 第 3 条：每个字都不确定 → 一个字也没判 → 不许 pass。
        return _failed("AUD.TEXT.MISPRONOUNCE", "evaluator_error")
    return _evaluated("AUD.TEXT.MISPRONOUNCE", defects)


def _check_prosody(ctx: _Ctx) -> dict:
    prosody = ctx.analysis.get("prosody")
    if not isinstance(prosody, dict):
        return _not_evaluated("AUD.TEXT.PROSODY_FLAT", "dependency_unavailable")
    match = prosody.get("stress_match")
    if not _finite(match):
        return _not_evaluated("AUD.TEXT.PROSODY_FLAT", "dependency_unavailable")
    if float(match) >= MIN_EMOTION_MATCH:
        return _evaluated("AUD.TEXT.PROSODY_FLAT")
    start, end = _speech_span(ctx)
    defect = _mk(
        check_id="AUD.TEXT.PROSODY_FLAT", kind="voice", severity="minor",
        scope=ctx.scope("AUD.TEXT.PROSODY_FLAT"), start=start, end=end,
        expected=f"stress/emotion profile matching ShotSpec "
                 f"{prosody.get('expected_emotion') or 'label'}",
        expected_source=ctx.shot_ref(),
        observed=f"stress match {float(match):.3f}",
        measured=_measured(float(match), "ratio", MIN_EMOTION_MATCH, "lt"),
        artifact_id=ctx.artifact["id"],
        actions=ctx.actions("AUD.TEXT.PROSODY_FLAT"),
        confidence=_pos(prosody.get("confidence"), 1.0))
    return _evaluated("AUD.TEXT.PROSODY_FLAT", [defect])


def _check_script_conflict(ctx: _Ctx) -> tuple[dict, dict | None]:
    """只在**字面相同、音素不同**之外的情况记一条观测。

    ASR 说 B、脚本说 A 且两者不是同一个字的两个读音时，S37 不下结论谁对。
    观测进 `dimensions`，不进 `defects`（§3.1 末行、E25-A3）。
    """
    conflicts = 0
    for utterance, token in ctx.tokens():
        index = token.get("script_index")
        if not isinstance(index, int) or not 0 <= index < len(
                utterance["script_tokens"]):
            continue
        if str(token.get("text") or "") != str(
                utterance["script_tokens"][index]["text"]):
            conflicts += 1
    if not conflicts:
        return _evaluated("AUD.TEXT.SCRIPT_CONFLICT"), None
    observation = {"name": "text_conflict", "score": None,
                   "reason": f"{conflicts} spoken token(s) differ from the "
                             f"declared script; S37 reports the observation "
                             f"only and does not decide which is right"}
    return _evaluated("AUD.TEXT.SCRIPT_CONFLICT"), observation


def _check_low_confidence(ctx: _Ctx) -> dict:
    defects = []
    for utterance, token in ctx.tokens():
        low = token.get("ci_low_seconds")
        high = token.get("ci_high_seconds")
        if not (_finite(low) and _finite(high)):
            continue
        width_ms = (float(high) - float(low)) * 1000.0
        if width_ms <= registry.MAX_ALIGN_CI_MS:
            continue
        defects.append(_mk(
            check_id="AUD.ALIGN.LOW_CONFIDENCE", kind="temporal",
            severity="major", scope=ctx.scope("AUD.ALIGN.LOW_CONFIDENCE"),
            start=token["start_seconds"], end=token["end_seconds"],
            expected=f"alignment CI width <= {registry.MAX_ALIGN_CI_MS}ms",
            expected_source=ctx.shot_ref(),
            observed=f"CI width {width_ms:.1f}ms for token "
                     f"{token.get('text')!r}",
            measured=_measured(width_ms, "ms", registry.MAX_ALIGN_CI_MS, "gt"),
            artifact_id=ctx.artifact["id"],
            actions=ctx.actions("AUD.ALIGN.LOW_CONFIDENCE"),
            confidence=token["alignment_confidence"],
            character_id=utterance["speaker_id"]))
    return _evaluated("AUD.ALIGN.LOW_CONFIDENCE", defects)


def _check_gap(ctx: _Ctx) -> dict:
    defects = []
    for utterance in ctx.utterances:
        tokens = sorted(_tokens_shifted(utterance, ctx.offset),
                        key=lambda t: t["start_seconds"])
        by_index = {t.get("script_index"): t for t in tokens}
        for previous, following in zip(tokens, tokens[1:]):
            gap = following["start_seconds"] - previous["end_seconds"]
            if gap <= MAX_GAP_SECONDS:
                continue
            index = previous.get("script_index")
            script = (utterance["script_tokens"][index]
                      if isinstance(index, int)
                      and 0 <= index < len(utterance["script_tokens"])
                      else None)
            if script and script.get("pause_after"):
                continue
            defects.append(_mk(
                check_id="AUD.ALIGN.GAP", kind="temporal", severity="minor",
                scope=ctx.scope("AUD.ALIGN.GAP"),
                start=previous["end_seconds"], end=following["start_seconds"],
                expected="consecutive spoke tokens with no scripted pause",
                expected_source=ctx.shot_ref(),
                observed=f"{gap * 1000.0:.0f}ms of non-speech with no pause "
                         f"marker in the script",
                measured=_measured(gap, "s", MAX_GAP_SECONDS, "gt"),
                artifact_id=ctx.artifact["id"],
                actions=ctx.actions("AUD.ALIGN.GAP"),
                confidence=min(previous["alignment_confidence"],
                               following["alignment_confidence"]),
                character_id=utterance["speaker_id"]))
    return _evaluated("AUD.ALIGN.GAP", defects)


def _check_overlap(ctx: _Ctx) -> dict:
    defects = []
    by_speaker: dict[str, list] = {}
    for utterance, token in ctx.tokens():
        speaker = token.get("speaker_id") or utterance["speaker_id"] or "unknown"
        by_speaker.setdefault(str(speaker), []).append(token)
    for speaker, tokens in by_speaker.items():
        tokens.sort(key=lambda t: t["start_seconds"])
        for previous, following in zip(tokens, tokens[1:]):
            overlap = previous["end_seconds"] - following["start_seconds"]
            if overlap <= 1e-6:
                continue
            defects.append(_mk(
                check_id="AUD.ALIGN.OVERLAP", kind="temporal",
                severity="major", scope=ctx.scope("AUD.ALIGN.OVERLAP"),
                start=following["start_seconds"],
                end=previous["end_seconds"],
                expected="tokens of one speaker do not overlap in time",
                expected_source=ctx.shot_ref(),
                observed=f"{overlap * 1000.0:.0f}ms overlap between "
                         f"{previous.get('text')!r} and "
                         f"{following.get('text')!r}",
                measured=_measured(overlap, "s", 0.0, "gt"),
                artifact_id=ctx.artifact["id"],
                actions=ctx.actions("AUD.ALIGN.OVERLAP"),
                confidence=min(previous["alignment_confidence"],
                               following["alignment_confidence"]),
                character_id=speaker))
    return _evaluated("AUD.ALIGN.OVERLAP", defects)


def _check_speaker_switch(ctx: _Ctx) -> dict:
    defects = []
    for utterance, token in ctx.tokens():
        expected = utterance["speaker_id"]
        observed = token.get("speaker_id")
        if not expected or not observed or expected == observed:
            continue
        defects.append(_mk(
            check_id="AUD.ALIGN.SPEAKER_SWITCH", kind="voice",
            severity="major", scope=ctx.scope("AUD.ALIGN.SPEAKER_SWITCH"),
            start=token["start_seconds"], end=token["end_seconds"],
            expected=f"utterance {utterance['utterance_id']} is spoken by "
                     f"{expected}",
            expected_source=ctx.shot_ref(),
            observed=f"speaker diarization assigns {observed}",
            measured=None, artifact_id=ctx.artifact["id"],
            actions=ctx.actions("AUD.ALIGN.SPEAKER_SWITCH"),
            confidence=min(token["alignment_confidence"],
                           _pos(utterance.get("confidence"), 1.0)),
            character_id=str(observed)))
    return _evaluated("AUD.ALIGN.SPEAKER_SWITCH", defects)


def _check_duration(ctx: _Ctx) -> dict:
    defects = []
    tolerance_s = registry.DIALOGUE_DURATION_TOLERANCE_MS / 1000.0
    for utterance in ctx.utterances:
        tokens = _tokens_shifted(utterance, ctx.offset)
        if not tokens:
            continue
        actual = (max(t["end_seconds"] for t in tokens)
                  - min(t["start_seconds"] for t in tokens))
        expected_ms = utterance.get("expected_duration_ms")
        if not _finite(expected_ms):
            expected_ms = (ctx.shot.get("dialogue_duration_ms"))
        if not _finite(expected_ms):
            continue
        expected_s = float(expected_ms) / 1000.0
        delta = actual - expected_s
        if abs(delta) <= tolerance_s:
            continue
        start = min(t["start_seconds"] for t in tokens)
        end = max(t["end_seconds"] for t in tokens)
        defects.append(_mk(
            check_id="AUD.ALIGN.DURATION_VS_SPEC", kind="temporal",
            severity="major", scope=ctx.scope("AUD.ALIGN.DURATION_VS_SPEC"),
            start=start, end=end,
            expected=f"{expected_s * 1000.0:.0f}ms per the shot spec",
            expected_source=ctx.shot_ref(),
            observed=f"{actual * 1000.0:.0f}ms of speech "
                     f"({delta * 1000.0:+.0f}ms)",
            measured=_measured(abs(delta), "s", tolerance_s, "gt"),
            artifact_id=ctx.artifact["id"],
            actions=ctx.actions("AUD.ALIGN.DURATION_VS_SPEC"),
            confidence=min(t["alignment_confidence"] for t in tokens),
            character_id=utterance["speaker_id"]))
    return _evaluated("AUD.ALIGN.DURATION_VS_SPEC", defects)


def _check_lipsync_offset(ctx: _Ctx) -> dict:
    lipsync = ctx.analysis["lipsync"]
    # §2.1：`known_audio_offset_seconds` 是 S35 已经报过的容器偏移。先扣掉
    # 再判，否则同一个偏移会被两个服务各报一次，S39 会以为有两处问题。
    measured_ms = float(lipsync.get("offset_ms") or 0.0)
    offset_ms = abs(measured_ms - ctx.offset * 1000.0)
    if offset_ms <= registry.LIPSYNC_MAJOR_MS:
        return _evaluated("AUD.LIPSYNC.OFFSET")
    severity = ("blocker" if offset_ms > registry.LIPSYNC_BLOCKER_MS
                else "major")
    threshold = (registry.LIPSYNC_BLOCKER_MS if severity == "blocker"
                 else registry.LIPSYNC_MAJOR_MS)
    start, end = _speech_span(ctx)
    defect = _mk(
        check_id="AUD.LIPSYNC.OFFSET", kind="lipsync", severity=severity,
        scope=ctx.scope("AUD.LIPSYNC.OFFSET"), start=start, end=end,
        expected="lip closure events align with the phoneme plosives",
        expected_source=ctx.artifact_ref(),
        observed=f"audio leads/lags the lips by {offset_ms:.0f}ms",
        measured=_measured(offset_ms, "ms", threshold, "gt"),
        artifact_id=ctx.artifact["id"],
        actions=ctx.actions("AUD.LIPSYNC.OFFSET"),
        confidence=_pos(lipsync.get("confidence"), 1.0))
    return _evaluated("AUD.LIPSYNC.OFFSET", [defect])


def _check_lipsync_openness(ctx: _Ctx) -> dict:
    lipsync = ctx.analysis["lipsync"]
    correlation = lipsync.get("openness_correlation")
    if not _finite(correlation):
        return _not_evaluated("AUD.LIPSYNC.OPENNESS_MISMATCH",
                              "dependency_unavailable")
    if float(correlation) >= MIN_OPENNESS_CORRELATION:
        return _evaluated("AUD.LIPSYNC.OPENNESS_MISMATCH")
    start, end = _speech_span(ctx)
    defect = _mk(
        check_id="AUD.LIPSYNC.OPENNESS_MISMATCH", kind="lipsync",
        severity="major", scope=ctx.scope("AUD.LIPSYNC.OPENNESS_MISMATCH"),
        start=start, end=end,
        expected="mouth openness correlates with vowel aperture",
        expected_source=ctx.artifact_ref(),
        observed=f"correlation {float(correlation):.3f}",
        measured=_measured(float(correlation), "correlation",
                           MIN_OPENNESS_CORRELATION, "lt"),
        artifact_id=ctx.artifact["id"],
        actions=ctx.actions("AUD.LIPSYNC.OPENNESS_MISMATCH"),
        confidence=_pos(lipsync.get("confidence"), 1.0))
    return _evaluated("AUD.LIPSYNC.OPENNESS_MISMATCH", [defect])


def _lipsync_intervals(ctx: _Ctx, *, check_id, kind, severity):
    defects = []
    lipsync = ctx.analysis["lipsync"]
    for interval in (lipsync.get(kind) or []):
        if not isinstance(interval, dict):
            continue
        start = _shift(_pos(interval.get("start_seconds")), ctx.offset)
        end = max(start + 1e-3,
                  _shift(_pos(interval.get("end_seconds")), ctx.offset))
        defects.append(_mk(
            check_id=check_id, kind="lipsync", severity=severity,
            scope=ctx.scope(check_id), start=start, end=end,
            expected=("the mouth is still while audio is silent"
                      if kind == "silent_mouth_motion"
                      else "the mouth moves while audio is voiced"),
            expected_source=ctx.artifact_ref(),
            observed=f"{end - start:.3f}s of {'mouth motion in silence' if kind == 'silent_mouth_motion' else 'voiced audio with a closed mouth'}",
            measured=_measured(end - start, "s", None, "na"),
            artifact_id=ctx.artifact["id"],
            actions=ctx.actions(check_id),
            confidence=_pos(lipsync.get("confidence"), 1.0)))
    return _evaluated(check_id, defects)


def _check_mouth_moves_silent(ctx: _Ctx) -> dict:
    return _lipsync_intervals(ctx, check_id="AUD.LIPSYNC.MOUTH_MOVES_SILENT",
                              kind="silent_mouth_motion", severity="major")


def _check_speaks_closed(ctx: _Ctx) -> dict:
    return _lipsync_intervals(ctx, check_id="AUD.LIPSYNC.SPEAKS_CLOSED",
                              kind="speaks_closed", severity="blocker")


def _check_face_not_found(ctx: _Ctx) -> dict:
    # 门控型：有人脸 = evaluated 且无缺陷；没有人脸 = not_applicable(no_face)。
    if (ctx.analysis.get("video") or {}).get("face_present"):
        return _evaluated("AUD.LIPSYNC.FACE_NOT_FOUND")
    return _na("AUD.LIPSYNC.FACE_NOT_FOUND", "no_face")


def _check_embedding_drift(ctx: _Ctx) -> dict:
    voice = ctx.analysis.get("voice") or {}
    windows = [w for w in (voice.get("window_distances") or [])
               if isinstance(w, dict) and _finite(w.get("distance"))]
    if not windows:
        return _not_evaluated("AUD.VOICE.EMBEDDING_DRIFT",
                              "dependency_unavailable")
    defects = []
    streak: list[dict] = []

    def flush(group):
        if not group:
            return
        # 连续窗口 ≥3 才升 blocker；孤立命中仍是 major——不能因为"只有一窗"
        # 就当没看见（S37-8）。
        severity = ("blocker"
                    if len(group) >= registry.EMBEDDING_DRIFT_WINDOWS
                    else "major")
        first, last = group[0], group[-1]
        defects.append(_mk(
            check_id="AUD.VOICE.EMBEDDING_DRIFT", kind="voice",
            severity=severity, scope=ctx.scope("AUD.VOICE.EMBEDDING_DRIFT"),
            start=_shift(_pos(first.get("start_seconds")), ctx.offset),
            end=_shift(_pos(last.get("end_seconds")), ctx.offset),
            expected=f"voice embedding within {VOICE_DISTANCE_TAU} of the "
                     f"S15 binding",
            expected_source=_source("S15", ctx.voice_id, ctx.voice_revision),
            observed=f"distance {max(float(w['distance']) for w in group):.3f} "
                     f"over {len(group)} consecutive window(s)",
            measured=_measured(
                max(float(w["distance"]) for w in group), "distance",
                VOICE_DISTANCE_TAU, "gt"),
            artifact_id=ctx.artifact["id"],
            actions=ctx.actions("AUD.VOICE.EMBEDDING_DRIFT"),
            confidence=_pos(voice.get("confidence"), 1.0)))

    for window in sorted(windows, key=lambda w: _pos(w.get("start_seconds"))):
        if float(window["distance"]) > VOICE_DISTANCE_TAU:
            streak.append(window)
        else:
            flush(streak)
            streak = []
    flush(streak)
    return _evaluated("AUD.VOICE.EMBEDDING_DRIFT", defects)


def _check_character_mixup(ctx: _Ctx) -> dict:
    voice = ctx.analysis.get("voice") or {}
    distance = voice.get("cross_shot_distance")
    if not _finite(distance):
        # 单镜头内无法比较时不是"通过"，也不是"缺陷"（S37-18）。
        return _na("AUD.VOICE.CHARACTER_MIXUP", "no_such_content")
    if float(distance) <= VOICE_DISTANCE_TAU:
        return _evaluated("AUD.VOICE.CHARACTER_MIXUP")
    start, end = _speech_span(ctx)
    defect = _mk(
        check_id="AUD.VOICE.CHARACTER_MIXUP", kind="voice", severity="blocker",
        scope=ctx.scope("AUD.VOICE.CHARACTER_MIXUP"), start=start, end=end,
        expected="the same character keeps one voice across shots",
        expected_source=_source("S15", ctx.voice_id, ctx.voice_revision),
        observed=f"cross-shot embedding distance {float(distance):.3f}",
        measured=_measured(float(distance), "distance", VOICE_DISTANCE_TAU,
                           "gt"),
        artifact_id=ctx.artifact["id"],
        actions=ctx.actions("AUD.VOICE.CHARACTER_MIXUP"),
        confidence=_pos(voice.get("confidence"), 1.0))
    return _evaluated("AUD.VOICE.CHARACTER_MIXUP", [defect])


def _check_emotion(ctx: _Ctx) -> dict:
    voice = ctx.analysis.get("voice") or {}
    match = voice.get("emotion_match")
    if not _finite(match):
        return _not_evaluated("AUD.VOICE.EMOTION_MISMATCH",
                              "dependency_unavailable")
    if float(match) >= MIN_EMOTION_MATCH:
        return _evaluated("AUD.VOICE.EMOTION_MISMATCH")
    start, end = _speech_span(ctx)
    defect = _mk(
        check_id="AUD.VOICE.EMOTION_MISMATCH", kind="voice", severity="minor",
        scope=ctx.scope("AUD.VOICE.EMOTION_MISMATCH"), start=start, end=end,
        expected=f"emotion {voice.get('expected_emotion') or 'per ShotSpec'}",
        expected_source=ctx.shot_ref(),
        observed=f"detected {voice.get('detected_emotion') or 'unknown'} "
                 f"(match {float(match):.3f})",
        measured=_measured(float(match), "ratio", MIN_EMOTION_MATCH, "lt"),
        artifact_id=ctx.artifact["id"],
        actions=ctx.actions("AUD.VOICE.EMOTION_MISMATCH"),
        confidence=_pos(voice.get("confidence"), 1.0))
    return _evaluated("AUD.VOICE.EMOTION_MISMATCH", [defect])


# S15 授权投影里表示"可以继续用"的状态。**没有认过的状态不算允许**：
# 缺状态、未知状态一律 `not_evaluated(dependency_unavailable)`，否则一个
# 拼错的状态串会被读成"授权没问题"（AGENTS.md：不得预填 active）。
AUTHORIZED_STATES = ("active", "authorized", "valid")
REVOKED_STATES = ("expired", "revoked")


def _check_authorization(ctx: _Ctx) -> dict:
    authorization = ctx.authorization or {}
    state = authorization.get("effective_state") or authorization.get("state")
    if not state:
        return _not_evaluated("AUD.VOICE.AUTHORIZATION",
                              "dependency_unavailable")
    state = str(state)
    if state in AUTHORIZED_STATES:
        return _evaluated("AUD.VOICE.AUTHORIZATION")
    if state not in REVOKED_STATES:
        return _not_evaluated("AUD.VOICE.AUTHORIZATION",
                              "dependency_unavailable")
    start, end = _speech_span(ctx)
    # §3.4 末段：不是内容缺陷，是权利缺陷。任何自动重生成都会再用同一个
    # 已撤销音色——所以 `recommended_actions` 只能是 human_review（I3）。
    defect = _mk(
        check_id="AUD.VOICE.AUTHORIZATION", kind="voice", severity="blocker",
        scope=ctx.scope("AUD.VOICE.AUTHORIZATION"), start=start, end=end,
        expected="the voice binding is still authorized for synthesis",
        expected_source=_source("S15", ctx.voice_id, ctx.voice_revision),
        observed=f"S15 reports effective_state={state}",
        measured=None, artifact_id=ctx.artifact["id"],
        actions=["human_review"],
        confidence=_pos(authorization.get("confidence"), 1.0))
    return _evaluated("AUD.VOICE.AUTHORIZATION", [defect])


HANDLERS = {
    "AUD.TEXT.MISSING_WORD": _check_missing_word,
    "AUD.TEXT.EXTRA_WORD": _check_extra_word,
    "AUD.TEXT.SUBSTITUTION": _check_substitution,
    "AUD.TEXT.MISPRONOUNCE": _check_mispronounce,
    "AUD.TEXT.PROSODY_FLAT": _check_prosody,
    "AUD.ALIGN.LOW_CONFIDENCE": _check_low_confidence,
    "AUD.ALIGN.GAP": _check_gap,
    "AUD.ALIGN.OVERLAP": _check_overlap,
    "AUD.ALIGN.SPEAKER_SWITCH": _check_speaker_switch,
    "AUD.ALIGN.DURATION_VS_SPEC": _check_duration,
    "AUD.LIPSYNC.OFFSET": _check_lipsync_offset,
    "AUD.LIPSYNC.OPENNESS_MISMATCH": _check_lipsync_openness,
    "AUD.LIPSYNC.MOUTH_MOVES_SILENT": _check_mouth_moves_silent,
    "AUD.LIPSYNC.SPEAKS_CLOSED": _check_speaks_closed,
    "AUD.LIPSYNC.FACE_NOT_FOUND": _check_face_not_found,
    "AUD.VOICE.EMBEDDING_DRIFT": _check_embedding_drift,
    "AUD.VOICE.CHARACTER_MIXUP": _check_character_mixup,
    "AUD.VOICE.EMOTION_MISMATCH": _check_emotion,
    "AUD.VOICE.AUTHORIZATION": _check_authorization,
}

# 观测型检查单独处理：它返回 (result, observation)，不产 defect。
OBSERVATION_HANDLERS = {"AUD.TEXT.SCRIPT_CONFLICT": _check_script_conflict}

# 依赖 ASR token 的检查。ASR 超时/出错时它们 `failed`，而不是"没有缺陷"。
NEEDS_ASR = frozenset({
    "AUD.TEXT.MISSING_WORD", "AUD.TEXT.EXTRA_WORD",
    "AUD.TEXT.SUBSTITUTION", "AUD.TEXT.MISPRONOUNCE",
    "AUD.TEXT.PROSODY_FLAT", "AUD.TEXT.SCRIPT_CONFLICT",
    "AUD.ALIGN.LOW_CONFIDENCE", "AUD.ALIGN.GAP", "AUD.ALIGN.OVERLAP",
    "AUD.ALIGN.SPEAKER_SWITCH", "AUD.ALIGN.DURATION_VS_SPEC",
})

# 依赖口型分析段的检查。
NEEDS_LIPSYNC = frozenset({
    "AUD.LIPSYNC.OFFSET", "AUD.LIPSYNC.OPENNESS_MISMATCH",
    "AUD.LIPSYNC.MOUTH_MOVES_SILENT", "AUD.LIPSYNC.SPEAKS_CLOSED",
})

# 依赖音色分析段的检查（不是 S15 授权，是分析器回传的音色度量）。
NEEDS_VOICE_ANALYSIS = frozenset({
    "AUD.VOICE.EMBEDDING_DRIFT", "AUD.VOICE.CHARACTER_MIXUP",
    "AUD.VOICE.EMOTION_MISMATCH",
})

# 幂等摘要里被剥掉的易变字段。与 S41 同一份清单：trace 与时间戳进来，
# 同一个请求重投就会算出不同 hash，然后被当成"同键不同 body"拒绝。
VOLATILE_CONTEXT_KEYS = ("trace_id", "traceparent", "timestamp",
                         "requested_at", "issued_at", "server_time")


def stable_request_hash(body: dict) -> str:
    """命令幂等摘要：业务字段全进，trace 与时间戳不进（§8）。"""
    from idempotency_sdk import canonical_request_hash

    if not isinstance(body, dict):
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    candidate = dict(body)
    context = candidate.get("execution_context")
    if isinstance(context, dict):
        candidate["execution_context"] = {
            k: v for k, v in context.items() if k not in VOLATILE_CONTEXT_KEYS}
    return canonical_request_hash(candidate)


def _hex64(value) -> str | None:
    """接受裸 64 位十六进制或 `sha256:` 前缀的摘要。"""
    if not isinstance(value, str):
        return None
    text = value[7:] if value.startswith("sha256:") else value
    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
        return text
    return None


class AudioAlignmentService:
    """S37 的写路径。**只有报告，没有 artifact 写入口。**

    构造顺序不能改：`executescript(SCHEMA)` 与 `Outbox.__init__` 都含
    `executescript`，而它会隐式提交。两者都必须在任何 `BEGIN IMMEDIATE`
    之前建一次，否则命令事务会被悄悄提交，ROLLBACK 失效。
    """

    def __init__(self, conn: sqlite3.Connection, *, artifact_source,
                 analysis_source, shot_source=None, lexicon_source=None,
                 voice_source=None, enabled: bool = False,
                 enabled_checks=()):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)
        self.artifact_source = artifact_source
        self.analysis_source = analysis_source
        self.shot_source = shot_source
        self.lexicon_source = lexicon_source
        self.voice_source = voice_source
        # 生产开关默认关闭（AGENTS.md）。关闭时命令 503，不产空报告。
        self.enabled = bool(enabled)
        unknown = [c for c in enabled_checks if c not in registry.CHECK_IDS]
        if unknown:
            raise ValueError("unknown check_id(s) in AUDIO_ALIGNMENT_CHECKS: "
                             f"{sorted(unknown)}")
        self.enabled_checks = tuple(enabled_checks)
        self.metrics: dict[str, int] = {}

    # ------------------------------------------------------------ 工具
    def _bump(self, name: str, amount: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def _command(self, tenant_id: str, command_id: str, produce):
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    def _upstream(self, call, code: str):
        """上游不可达统一成 503，**绝不用缓存结论兜底**。"""
        try:
            return call()
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(503, code, str(exc), retryable=True,
                                   details=exc.details) from exc
            raise

    # ------------------------------------------------------------ 命令
    def check(self, tenant_id: str, command_id: str, body: dict,
              request_hash: str) -> dict:
        """`POST /alignment-jobs`。同步执行，返回已落库的报告标识。"""
        if not self.enabled:
            raise ServiceError(
                503, "SERVICE_DISABLED",
                "AUDIO_ALIGNMENT_ENABLED is not set; S37 will not produce a "
                "report for this command (S37-19)")
        if not self.enabled_checks:
            raise ServiceError(
                422, "NO_CHECKS_ENABLED",
                "AUDIO_ALIGNMENT_CHECKS is empty; refusing to emit a report "
                "with an empty check_manifest, because that reads as a pass "
                "without measuring anything (S37-19)")

        context = body.get("execution_context")
        if not isinstance(context, dict):
            raise ServiceError(422, "INVALID_ALIGNMENT_JOB",
                               "execution_context is required")
        if context.get("tenant_id") != tenant_id:
            # 租户只来自平台签名；请求体只用于比对（AGENTS.md）。
            raise ServiceError(403, "TENANT_MISMATCH",
                               "execution_context.tenant_id does not match the "
                               "credential")
        missing = [k for k in REQUIRED_CONTEXT if context.get(k) is None]
        policy_ref = context.get("policy_ref")
        if isinstance(policy_ref, dict):
            missing += [f"policy_ref.{k}" for k in
                        ("id", "revision", "content_hash")
                        if policy_ref.get(k) is None]
        else:
            missing.append("policy_ref")
        manifest_ref = context.get("manifest_ref")
        if isinstance(manifest_ref, dict):
            missing += [f"manifest_ref.{k}" for k in
                        ("id", "revision", "content_hash")
                        if manifest_ref.get(k) is None]
        else:
            missing.append("manifest_ref")
        missing += [k for k in REQUIRED_TOP_LEVEL if body.get(k) is None]

        artifact_ref = body.get("artifact")
        if not isinstance(artifact_ref, dict) or not artifact_ref.get("id"):
            missing.append("artifact")
        shot_ref = body.get("shot_spec_ref")
        if not (isinstance(shot_ref, dict) and shot_ref.get("id")) \
                and not body.get("shot_id"):
            missing.append("shot_spec_ref.id or shot_id")
        dialogue = body.get("dialogue")
        if not isinstance(dialogue, dict) or not dialogue.get("language"):
            missing.append("dialogue.language")
        if missing:
            raise ServiceError(
                422, "INVALID_ALIGNMENT_JOB",
                f"missing {sorted(missing)}; these are the coordinates the "
                f"report is recorded against")

        artifact_kind = body.get("artifact_kind") or "video"
        from .schema import SUPPORTED_ARTIFACT_KINDS
        if artifact_kind not in SUPPORTED_ARTIFACT_KINDS:
            raise ServiceError(
                422, "UNSUPPORTED_ARTIFACT_KIND",
                f"artifact_kind={artifact_kind!r} has no alignment checks; "
                f"S37 accepts {sorted(SUPPORTED_ARTIFACT_KINDS)}")

        audio_source = body.get("audio_source")
        if not isinstance(audio_source, dict):
            raise ServiceError(422, "INVALID_ALIGNMENT_JOB",
                               "audio_source is required")
        from .schema import AUDIO_SOURCE_MODES
        mode = str(audio_source.get("mode") or "embedded")
        if mode not in AUDIO_SOURCE_MODES:
            raise ServiceError(
                422, "INVALID_ALIGNMENT_JOB",
                f"audio_source.mode={mode!r} must be one of "
                f"{sorted(AUDIO_SOURCE_MODES)}")

        try:
            chosen = registry.select((body.get("checks") or {}).get("include"),
                                     (body.get("checks") or {}).get("exclude"))
        except ValueError as exc:
            raise ServiceError(422, "INVALID_CHECKS", str(exc)) from exc
        chosen = [c for c in chosen if c in self.enabled_checks]
        if not chosen:
            raise ServiceError(
                422, "NO_CHECKS_ENABLED",
                "checks.include selects no check that AUDIO_ALIGNMENT_CHECKS "
                "enables; refusing to emit an empty manifest (S37-19)")

        utterances_raw = [u for u in (dialogue.get("utterances") or [])
                          if isinstance(u, dict)]
        artifact = self._artifact(tenant_id, artifact_ref)
        shot = self._shot(tenant_id, body, context)

        if not utterances_raw:
            if isinstance((shot or {}).get("dialogue"), list) \
                    and shot["dialogue"]:
                # ShotSpec 里有对白，命令却没给文本：这时候产 pass no-op
                # 就是"没查就说没有"。拒绝，让调用方补文本。
                raise ServiceError(
                    422, "MISSING_DIALOGUE",
                    "shot_spec declares dialogue but dialogue.utterances is "
                    "empty; S37 will not report a no-dialogue no-op")
            return self._no_dialogue(
                tenant_id=tenant_id, command_id=command_id, body=body,
                context=context, artifact_ref=artifact_ref, artifact=artifact,
                shot=shot, artifact_kind=artifact_kind, chosen=chosen,
                audio_source=audio_source, request_hash=request_hash)

        return self._evaluate_and_persist(
            tenant_id=tenant_id, command_id=command_id, body=body,
            context=context, artifact_ref=artifact_ref, artifact=artifact,
            shot=shot, artifact_kind=artifact_kind, chosen=chosen,
            audio_source=audio_source, utterances_raw=utterances_raw,
            request_hash=request_hash)

    # ------------------------------------------------------------ 读上游
    def _artifact(self, tenant_id: str, ref: dict) -> dict:
        """S28 元数据。读不到就是 503——不拿"未知"当素材。"""
        if self.artifact_source is None:
            raise ServiceError(
                503, "ARTIFACT_SOURCE_UNAVAILABLE",
                "artifact source (S28) is not configured", retryable=True,
                details={"dependency": "S28", "reason": "not_configured"})
        metadata = self._upstream(
            lambda: self.artifact_source(tenant_id, ref),
            "ARTIFACT_SOURCE_UNAVAILABLE")
        if not isinstance(metadata, dict):
            raise ServiceError(
                503, "ARTIFACT_SOURCE_UNAVAILABLE",
                "S28 returned no artifact metadata; S37 will not report on an "
                "unknown artifact", retryable=True)
        inner = metadata.get("artifact")
        if isinstance(inner, dict):
            metadata = inner
        expected = _hex64(ref.get("content_hash"))
        actual = _hex64(metadata.get("content_hash"))
        if expected and actual and expected != actual:
            # 被测量的那份不是被引用的那份（E24 前置）。
            raise ServiceError(
                409, "ARTIFACT_HASH_MISMATCH",
                "S28 metadata content_hash does not match the requested "
                "artifact_ref; S37 will not report on a different artifact",
                details={"requested": expected, "actual": actual})
        return metadata

    def _shot(self, tenant_id: str, body: dict, context: dict):
        """S06 ShotSpec。读不到返回 None——依赖它的项落 not_evaluated。"""
        ref = body.get("shot_spec_ref")
        if not (isinstance(ref, dict) and ref.get("id")):
            if body.get("shot_id"):
                return {"id": str(body["shot_id"]),
                        "revision": int(body.get("shot_revision") or 1)}
            return None
        if self.shot_source is None:
            return None
        try:
            shot = self.shot_source(tenant_id, ref, context)
        except (ConnectionError, ServiceError):
            return None
        if not isinstance(shot, dict):
            return None
        inner = shot.get("sheet") if isinstance(shot.get("sheet"), dict) else None
        if inner is not None:
            shot = inner
        return {"id": str(shot.get("id") or ref["id"]),
                "revision": int(shot.get("revision") or ref.get("revision") or 1),
                **{k: v for k, v in shot.items()
                   if k not in ("id", "revision")}}

    def _analysis(self, tenant_id: str, *, body, artifact_ref, artifact,
                  audio_source, utterances_raw, cached_asr):
        """取私有分析文档。未配置 → 503；形状不对 → 502。

        这就是 S37-5 的处理方式：不假装 S26 有产物契约，也不产空报告。
        """
        if self.analysis_source is None:
            raise ServiceError(
                503, "ANALYSIS_SOURCE_UNAVAILABLE",
                "analysis source (S26 understanding job) is not configured; "
                "S37 will not emit a report without an analysis document "
                "(S37-5)", retryable=True,
                details={"dependency": "S26", "reason": "not_configured"})
        request = {
            "artifact": {"id": str(artifact_ref["id"]),
                         "revision": int(artifact_ref.get("revision") or 1),
                         "content_hash": _hex64(artifact_ref.get("content_hash"))},
            "audio_source": dict(audio_source),
            "language": str((body.get("dialogue") or {})["language"]),
            "dialogue": {"utterances": [dict(u) for u in utterances_raw]},
            "cached_asr": cached_asr,
        }
        document = self._upstream(
            lambda: self.analysis_source(tenant_id, request),
            "ANALYSIS_SOURCE_UNAVAILABLE")
        if not isinstance(document, dict):
            raise ServiceError(502, "UPSTREAM_INVALID",
                               "S26 returned no analysis document")
        version = document.get("analysis_version")
        if version != ANALYSIS_VERSION:
            raise ServiceError(
                502, "UPSTREAM_INVALID",
                f"analysis document version {version!r} is not "
                f"{ANALYSIS_VERSION!r}")
        return document

    def _lexicon(self, tenant_id: str, ref):
        """S15 发音词典。不可达返回 None → 依赖项 not_evaluated。"""
        if not isinstance(ref, dict) or not ref.get("id"):
            return None
        if self.lexicon_source is None:
            return None
        try:
            return self.lexicon_source(tenant_id, ref)
        except (ConnectionError, ServiceError):
            return None

    def _authorization(self, tenant_id: str, ref):
        """S15 音色绑定/授权。不可达返回 None（E25-A4 → abstain）。"""
        if not isinstance(ref, dict) or not ref.get("id"):
            return None
        if self.voice_source is None:
            return None
        try:
            return self.voice_source(tenant_id, ref)
        except (ConnectionError, ServiceError):
            return None

    # ------------------------------------------------------- ASR 缓存（§8）
    def _cached_asr(self, tenant_id: str, cache_key: str):
        row = self.conn.execute(
            "SELECT payload FROM asr_cache WHERE tenant_id=? AND cache_key=?",
            (tenant_id, cache_key)).fetchone()
        if row is None:
            return None
        payload = _loads(row["payload"], None)
        return payload if isinstance(payload, dict) else None

    def _store_asr(self, tenant_id: str, *, cache_key, audio_track_hash,
                   asr_snapshot, lexicon_revision, voice_binding_ref, asr):
        self.conn.execute(
            "INSERT INTO asr_cache (tenant_id, cache_key, audio_track_hash,"
            " asr_model_snapshot, lexicon_revision, voice_binding_ref,"
            " payload, created_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(tenant_id, cache_key) DO NOTHING",
            (tenant_id, cache_key, audio_track_hash, _jcs(asr_snapshot),
             lexicon_revision, voice_binding_ref, _jcs(asr), _now()))

    # ------------------------------------------------------------ 评估
    def _evaluate_and_persist(self, *, tenant_id, command_id, body, context,
                              artifact_ref, artifact, shot, artifact_kind,
                              chosen, audio_source, utterances_raw,
                              request_hash) -> dict:
        dialogue = body["dialogue"]
        lexicon_ref = None
        for utterance in utterances_raw:
            if isinstance(utterance.get("lexicon_ref"), dict):
                lexicon_ref = utterance["lexicon_ref"]
                break
        if lexicon_ref is None and isinstance(body.get("voice_binding_ref"),
                                              dict):
            lexicon_ref = body["voice_binding_ref"]
        voice_ref = body.get("voice_binding_ref")
        if not isinstance(voice_ref, dict):
            voice_ref = lexicon_ref
        lexicon = self._lexicon(tenant_id, lexicon_ref)
        authorization = self._authorization(tenant_id, voice_ref)
        lexicon_revision = (int(lexicon.get("revision") or 1)
                            if isinstance(lexicon, dict) else
                            (int(lexicon_ref.get("revision") or 1)
                             if isinstance(lexicon_ref, dict) else None))
        voice_binding_ref = _jcs(voice_ref) if isinstance(voice_ref, dict) \
            else None

        language = str(dialogue["language"])
        # 先解析音轨 hash：它是 ASR 缓存的键的一半，也是报告的必填列。
        audio_track_hash = (
            str((audio_source.get("audio_track_hash") or
                 artifact.get("audio_track_hash") or "")) or None)
        asr_snapshot = dict(registry.ASR_MODEL_SNAPSHOT)
        cache_key = None
        cached_asr = None
        if audio_track_hash:
            cache_key = asr_cache_key(
                tenant_id=tenant_id, audio_track_hash=audio_track_hash,
                asr_model_snapshot=asr_snapshot,
                lexicon_revision=lexicon_revision,
                voice_binding_hash=voice_binding_ref)
            cached_asr = self._cached_asr(tenant_id, cache_key)

        analysis = self._analysis(
            tenant_id, body=body, artifact_ref=artifact_ref,
            artifact=artifact, audio_source=audio_source,
            utterances_raw=utterances_raw, cached_asr=cached_asr)

        audio_track_hash = str(
            (analysis.get("audio") or {}).get("track_hash")
            or audio_track_hash or "") or None
        if not audio_track_hash:
            # C2：QCReport 的 media.audio 没有 audio_track_hash 字段，而报告的
            # 幂等与 ASR 复用都靠它。拿不到就不编一个（AGENTS.md）。
            raise ServiceError(
                422, "MISSING_AUDIO_TRACK_HASH",
                "no audio_track_hash from the analysis document, the command, "
                "or S28 metadata; S37 cannot record a report whose ASR reuse "
                "key is unknown (plan C2)")

        asr = analysis.get("asr") if isinstance(analysis.get("asr"), dict) else {}
        asr_snapshot = (asr.get("model_snapshot")
                        if isinstance(asr.get("model_snapshot"), dict)
                        else asr_snapshot)
        if cache_key is None:
            cache_key = asr_cache_key(
                tenant_id=tenant_id, audio_track_hash=audio_track_hash,
                asr_model_snapshot=asr_snapshot,
                lexicon_revision=lexicon_revision,
                voice_binding_hash=voice_binding_ref)
        units_source = self._utterances(utterances_raw, analysis)

        ctx = _Ctx(
            tenant_id=tenant_id, analysis=analysis, utterances=units_source,
            artifact={"id": str(artifact_ref["id"]),
                      "revision": int(artifact_ref.get("revision") or 1),
                      "sha256": _hex64(artifact_ref.get("content_hash"))
                      or _hex64(artifact.get("content_hash")) or ""},
            shot=shot or {"id": str(body.get("shot_id") or "shot"),
                          "revision": 1},
            offset=_pos(body.get("known_audio_offset_seconds")),
            authorization=authorization if isinstance(authorization, dict)
            else None,
            policy_version=str((context.get("policy_ref") or {}).get("revision")
                               or "unknown"),
            lexicon_ref=lexicon_ref, voice_ref=voice_ref)

        gates = self._gates(analysis=analysis, audio_source=audio_source,
                            authorization=authorization, chosen=chosen,
                            lexicon=lexicon, lexicon_ref=lexicon_ref,
                            voice_ref=voice_ref)
        manifest, observations = self._run_checks(
            ctx=ctx, chosen=chosen, analysis=analysis, gates=gates)
        defects = [d for entry in manifest for d in entry["defects"]]

        required_min = (registry.LIPSYNC_REQUIRED_COVERAGE_MIN
                        if (any(c.startswith("AUD.LIPSYNC.") for c in chosen)
                            and (analysis.get("video") or {}).get("present"))
                        else registry.DEFAULT_REQUIRED_COVERAGE_MIN)
        coverage = _coverage(analysis=analysis, utterances=units_source,
                             required_min=required_min,
                             sampling_version=registry.SAMPLING_VERSION)
        confidence = compute_confidence(manifest=manifest)
        decided = decide(manifest=manifest, defects=defects, coverage=coverage,
                         confidence=confidence)

        evaluator = {
            "name": EVALUATOR_NAME,
            "version": EVALUATOR_VERSION,
            "image_digest": EVALUATOR_IMAGE_DIGEST,
            "model_snapshot": asr_snapshot if isinstance(asr_snapshot, dict)
            else None,
        }
        policy_version = str((context.get("policy_ref") or {}).get("revision")
                             or "unknown")
        attempt_slot_id = (body.get("repair_context") or {}).get(
            "attempt_slot_id") if isinstance(body.get("repair_context"),
                                             dict) else None

        key = derived_key(
            tenant_id=tenant_id,
            artifact_sha256=_hex64(artifact_ref.get("content_hash"))
            or _hex64(artifact.get("content_hash")) or "",
            audio_track_hash=audio_track_hash, language=language,
            evaluator=evaluator, policy_version=policy_version,
            sampling_policy_version=registry.SAMPLING_VERSION,
            attempt_slot_id=attempt_slot_id)
        report_id = report_id_for(key)
        generation = self._next_generation(tenant_id, key)

        payload = self._build_payload(
            report_id=report_id, generation=generation, tenant_id=tenant_id,
            context=context, body=body, artifact_ref=artifact_ref,
            artifact=artifact, artifact_kind=artifact_kind, analysis=analysis,
            evaluator=evaluator, policy_version=policy_version,
            manifest=manifest, defects=defects, coverage=coverage,
            confidence=confidence, decided=decided,
            attempt_slot_id=attempt_slot_id, observations=observations,
            audio_track_hash=audio_track_hash, language=language,
            asr_snapshot=asr_snapshot)
        digest = report_content_hash(payload)
        payload["report_content_hash"] = digest

        result = {
            "report_id": report_id,
            "report_generation": generation,
            "operation_id": _new_id("operation"),
            "status": "accepted",
            "decision": decided["decision"],
            "content_hash": digest,
        }

        def _produce():
            self._persist(
                report_id=report_id, generation=generation, key=key,
                tenant_id=tenant_id, context=context, body=body,
                artifact_ref=artifact_ref, artifact=artifact,
                artifact_kind=artifact_kind, analysis=analysis,
                evaluator=evaluator, policy_version=policy_version,
                manifest=manifest, defects=defects, coverage=coverage,
                decided=decided, attempt_slot_id=attempt_slot_id,
                payload=payload, digest=digest, command_id=command_id,
                request_hash=request_hash, units=units_source,
                audio_track_hash=audio_track_hash, language=language,
                asr_snapshot=asr_snapshot, lexicon_revision=lexicon_revision,
                voice_binding_ref=voice_binding_ref,
                cache_key=cache_key, asr_document=cached_asr)

        self._command(tenant_id, command_id, _produce)
        return result

    def _utterances(self, utterances_raw, analysis) -> list[dict]:
        return [_utterance_view(u, analysis) for u in utterances_raw]

    def _gates(self, *, analysis, audio_source, authorization, chosen,
               lexicon=None, lexicon_ref=None, voice_ref=None) -> dict:
        audio = analysis.get("audio") if isinstance(analysis.get("audio"),
                                                    dict) else {}
        video = analysis.get("video") if isinstance(analysis.get("video"),
                                                    dict) else {}
        asr = analysis.get("asr") if isinstance(analysis.get("asr"), dict) else {}
        lipsync = analysis.get("lipsync") if isinstance(
            analysis.get("lipsync"), dict) else {}
        voice = analysis.get("voice") if isinstance(analysis.get("voice"),
                                                    dict) else {}
        # §8：无声/损坏 → abstain(asset_unavailable)，绝不产出"全静音 =
        # 全漏词"的假 defects。
        audio_unusable = bool(audio.get("silent") or audio.get("damaged")) \
            or audio.get("analyzed") is False
        asr_outcome = str(asr.get("outcome") or "ok")
        lipsync_outcome = str(lipsync.get("outcome") or "ok")
        video_present = bool(video.get("present")) \
            and str(audio_source.get("mode") or "embedded") == "embedded"
        # 只有"命令体点名了词典、但 S15 读不到"才算缺词典。没点名词典是
        # 合法配置（§5：lexicon_revision = null 表示未使用发音词典），
        # 这时 MISPRONOUNCE 不是"依赖没到"，而是"没有可比的多音字依据"。
        lexicon_absent = bool(isinstance(lexicon_ref, dict)
                              and lexicon_ref.get("id")
                              and not isinstance(lexicon, dict))
        voice_bound = isinstance(voice_ref, dict) and bool(voice_ref.get("id"))
        return {
            "audio_unusable": audio_unusable,
            "asr_failed": asr_outcome in ("error", "timeout"),
            "asr_reason": ("evaluator_timeout" if asr_outcome == "timeout"
                           else "evaluator_error"),
            "lipsync_failed": lipsync_outcome in ("error", "timeout"),
            "lipsync_reason": ("evaluator_timeout"
                               if lipsync_outcome == "timeout"
                               else "evaluator_error"),
            "lipsync_absent": (not video_present) or not lipsync,
            "vfr": bool(video.get("is_vfr")) and video_present,
            "no_face": bool(video_present) and not video.get("face_present"),
            # 音色度量缺失（分析器没给）与授权不可达是两件事：前者不是
            # "依赖不可用"，后者是。两者都让 AUD.VOICE.* 没结论。
            "voice_absent": not voice,
            "authorization_absent": authorization is None,
            "voice_bound": voice_bound,
            "lexicon_absent": lexicon_absent,
            "lexicon_unused": not (isinstance(lexicon_ref, dict)
                                   and lexicon_ref.get("id")),
        }

    def _run_checks(self, *, ctx, chosen, analysis, gates):
        manifest, observations = [], []
        needs_video = registry.NEEDS_VIDEO_FACE
        vfr_note = False
        for check_id in chosen:
            if gates["audio_unusable"] and check_id.startswith("AUD."):
                # §8：坏音频一律 `not_evaluated(asset_unavailable)`。绝不能
                # 让"全静音"变成"全漏词"的 defects（那是最贵的一种假阳性：
                # S39 会去重配一段根本没问题的台词）。
                manifest.append(_not_evaluated(check_id, "asset_unavailable"))
                continue
            if check_id in needs_video or check_id == "AUD.LIPSYNC.FACE_NOT_FOUND":
                if gates["lipsync_absent"]:
                    # external 音频 / 无视频段：没有同基准的画面，做不了口型。
                    # 这不是通过（S37-11）。
                    manifest.append(_na(check_id, "no_such_content"))
                    continue
                if gates["vfr"]:
                    # 帧-时间映射不唯一 → 不硬算（§2.1 / E24-A9）。
                    manifest.append(_failed(check_id, "evaluator_error"))
                    if not vfr_note:
                        vfr_note = True
                        observations.append({
                            "name": "vfr_unsupported", "score": None,
                            "reason": "variable frame rate: the frame/time "
                                      "mapping is not unique, so lipsync "
                                      "checks were not computed"})
                    continue
                if gates["no_face"]:
                    # 没有人脸 → not_applicable(no_face)，不是"口型没问题"。
                    manifest.append(_na(check_id, "no_face"))
                    continue
            if check_id in NEEDS_ASR and gates["asr_failed"]:
                manifest.append(_failed(check_id, gates["asr_reason"]))
                continue
            if check_id in NEEDS_LIPSYNC and gates["lipsync_failed"]:
                manifest.append(_failed(check_id, gates["lipsync_reason"]))
                continue
            if check_id == "AUD.VOICE.AUTHORIZATION":
                if gates["authorization_absent"]:
                    # E25-A4：S15 不可达 → dependency_unavailable → abstain。
                    # **不能**把"读不到授权"读成"授权有效"（AGENTS.md）。
                    manifest.append(_not_evaluated(check_id,
                                                   "dependency_unavailable"))
                    continue
            elif check_id in NEEDS_VOICE_ANALYSIS:
                if gates["authorization_absent"] or gates["voice_absent"]:
                    # 音色距离要跟 S15 的绑定比；绑定读不到，度量再准也没基准。
                    manifest.append(_not_evaluated(check_id,
                                                   "dependency_unavailable"))
                    continue
            if check_id in registry.NEEDS_LEXICON and (gates["lexicon_absent"]
                                                       or gates["lexicon_unused"]):
                # 没有发音词典就没有"应该怎么念"的基准，不能自己 G2P 一个
                # 期望再去判人念错了。
                manifest.append(_not_evaluated(check_id,
                                               "dependency_unavailable"))
                continue
            handler = OBSERVATION_HANDLERS.get(check_id) or HANDLERS.get(check_id)
            if handler is None:
                raise ServiceError(
                    500, "CHECK_NOT_IMPLEMENTED",
                    f"check {check_id} is registered but has no handler")
            product = handler(ctx)
            if check_id in OBSERVATION_HANDLERS:
                entry, observation = product
                manifest.append(entry)
                if observation is not None:
                    observations.append(observation)
            else:
                manifest.append(product)
        # 顺序稳定（S39 按 ordinal 读）。
        order = {cid: i for i, cid in enumerate(chosen)}
        manifest.sort(key=lambda e: order[e["check_id"]])
        return manifest, observations

    # ------------------------------------------------------- 无对白 no-op
    def _no_dialogue(self, *, tenant_id, command_id, body, context,
                     artifact_ref, artifact, shot, artifact_kind, chosen,
                     audio_source, request_hash) -> dict:
        """§8.1：显式 no-op。**不创建空音频假成功。**

        报告仍然生成：`check_manifest` 全部 `not_applicable(no_dialogue)`，
        `decision=pass`，`coverage.audio_analyzed=false`。这里不取分析文档：
        没有对白就没有要分析的东西，编一份"分析过、没问题"的分析文档才是
        docs/07 §1.5 禁的那件事。
        """
        audio_track_hash = str(audio_source.get("audio_track_hash")
                               or artifact.get("audio_track_hash") or "")
        if not audio_track_hash:
            raise ServiceError(
                422, "MISSING_AUDIO_TRACK_HASH",
                "a no-dialogue report still records which audio track it "
                "covers; none was provided and none is in S28 metadata")
        language = str(body["dialogue"]["language"])
        manifest = [_na(check_id, "no_dialogue") for check_id in chosen]
        coverage = {
            "media_duration_seconds": 0.0,
            "frames_sampled": 0,
            "temporal_windows_evaluated": 0,
            "planned_windows": 0,
            "coverage_ratio": 0.0,
            "required_coverage_min": registry.DEFAULT_REQUIRED_COVERAGE_MIN,
            "audio_analyzed": False,
            "audio_analyzed_seconds": 0.0,
            "sampling_policy_version": registry.SAMPLING_VERSION,
        }
        decided = _decided("pass", "no_dialogue: explicit no-op per plan §8.1")
        evaluator = {
            "name": EVALUATOR_NAME, "version": EVALUATOR_VERSION,
            "image_digest": EVALUATOR_IMAGE_DIGEST, "model_snapshot": None,
        }
        policy_version = str((context.get("policy_ref") or {}).get("revision")
                             or "unknown")
        attempt_slot_id = (body.get("repair_context") or {}).get(
            "attempt_slot_id") if isinstance(body.get("repair_context"),
                                             dict) else None
        analysis = {"audio": {"analyzed": False, "duration_seconds": 0.0,
                              "track_hash": audio_track_hash},
                    "video": {}, "asr": {}, "voice": {}, "lipsync": {}}
        key = derived_key(
            tenant_id=tenant_id,
            artifact_sha256=_hex64(artifact_ref.get("content_hash"))
            or _hex64(artifact.get("content_hash")) or "",
            audio_track_hash=audio_track_hash, language=language,
            evaluator=evaluator, policy_version=policy_version,
            sampling_policy_version=registry.SAMPLING_VERSION,
            attempt_slot_id=attempt_slot_id)
        report_id = report_id_for(key)
        generation = self._next_generation(tenant_id, key)
        payload = self._build_payload(
            report_id=report_id, generation=generation, tenant_id=tenant_id,
            context=context, body=body, artifact_ref=artifact_ref,
            artifact=artifact, artifact_kind=artifact_kind, analysis=analysis,
            evaluator=evaluator, policy_version=policy_version,
            manifest=manifest, defects=[], coverage=coverage, confidence=1.0,
            decided=decided, attempt_slot_id=attempt_slot_id,
            observations=[], audio_track_hash=audio_track_hash,
            language=language, asr_snapshot=None)
        digest = report_content_hash(payload)
        payload["report_content_hash"] = digest
        result = {"report_id": report_id, "report_generation": generation,
                  "operation_id": _new_id("operation"), "status": "accepted",
                  "decision": decided["decision"], "content_hash": digest}

        def _produce():
            self._persist(
                report_id=report_id, generation=generation, key=key,
                tenant_id=tenant_id, context=context, body=body,
                artifact_ref=artifact_ref, artifact=artifact,
                artifact_kind=artifact_kind, analysis=analysis,
                evaluator=evaluator, policy_version=policy_version,
                manifest=manifest, defects=[], coverage=coverage,
                decided=decided, attempt_slot_id=attempt_slot_id,
                payload=payload, digest=digest, command_id=command_id,
                request_hash=request_hash, units=[],
                audio_track_hash=audio_track_hash, language=language,
                asr_snapshot=None, lexicon_revision=None,
                voice_binding_ref=None, cache_key=None, asr_document=None)

        self._command(tenant_id, command_id, _produce)
        return result

    def _next_generation(self, tenant_id: str, key: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM report WHERE tenant_id=? AND derived_key=?",
            (tenant_id, key)).fetchone()
        return int(row["n"] if row is not None else 0) + 1

    # ------------------------------------------------------------ 装配
    def _media_block(self, *, analysis, artifact, artifact_kind) -> dict:
        audio = analysis.get("audio") if isinstance(analysis.get("audio"),
                                                    dict) else {}
        video = analysis.get("video") if isinstance(analysis.get("video"),
                                                    dict) else {}
        video_block = None
        if video.get("present"):
            video_block = {
                "width": int(video.get("width") or 1),
                "height": int(video.get("height") or 1),
                "fps_num": int(video.get("fps_num") or 25),
                "fps_den": int(video.get("fps_den") or 1),
                "codec": str(video.get("codec") or "unknown"),
                "pix_fmt": str(video.get("pix_fmt") or "unknown"),
                "color_range": str(video.get("color_range") or "unknown"),
                "is_vfr": bool(video.get("is_vfr")),
                "frame_count": int(video.get("frame_count") or 0),
            }
        audio_block = None
        if audio.get("analyzed") or audio.get("duration_seconds"):
            audio_block = {
                "codec": str(audio.get("codec") or "unknown"),
                "sample_rate_hz": int(audio.get("sample_rate_hz") or 48000),
                "channels": int(audio.get("channels") or 2),
                "stream_index": int(audio.get("stream_index") or 0),
                "start_offset_seconds": float(
                    audio.get("start_offset_seconds") or 0.0),
            }
        duration = _pos(audio.get("duration_seconds")) or _pos(
            video.get("duration_seconds"))
        return {"duration_seconds": round(duration, 6),
                "video": video_block, "audio": audio_block}

    def _build_payload(self, *, report_id, generation, tenant_id, context,
                       body, artifact_ref, artifact, artifact_kind, analysis,
                       evaluator, policy_version, manifest, defects, coverage,
                       confidence, decided, attempt_slot_id, observations,
                       audio_track_hash, language, asr_snapshot) -> dict:
        """QCReport v2。23 个顶层键由 schema 钉死（`additionalProperties:false`）。"""
        artifact = artifact if isinstance(artifact, dict) else {}
        artifact_sha256 = (_hex64(artifact_ref.get("content_hash"))
                           or _hex64(artifact.get("content_hash")) or "")
        context = context if isinstance(context, dict) else {}
        manifest_ref = context.get("manifest_ref") or {}
        policy_ref = context.get("policy_ref") or {}
        shot_ref = body.get("shot_spec_ref") if isinstance(
            body.get("shot_spec_ref"), dict) else {}
        shot_id = str(shot_ref.get("id") or body.get("shot_id") or "shot")
        shot_revision = int(shot_ref.get("revision") or
                            body.get("shot_revision") or 1)

        supersedes = None
        if generation > 1:
            supersedes = {"report_id": report_id,
                          "report_generation": generation - 1}
        repair_context = None
        if isinstance(body.get("repair_context"), dict):
            src = body["repair_context"]
            repair_context = {
                "repair_plan_id": str(src.get("repair_plan_id") or ""),
                "attempt_slot_id": str(src.get("attempt_slot_id") or ""),
                "attempt_seq": int(src.get("attempt_seq") or 1),
                "defect_fingerprints": list(
                    src.get("defect_fingerprints") or []),
            }
        manifest_out = [{"check_id": entry["check_id"],
                         "status": entry["status"],
                         "reason_code": entry["reason_code"]}
                        for entry in manifest]
        # defect_id 在这里分配：指纹是内容寻址的（跨 Run 稳定，E26-A1），
        # id 是"这份报告里的第几条"，必须随报告而定。两者都不能在 `_mk`
        # 里编——那时还不知道这一代里已经出现过几条。
        group_defects = []
        for index, defect in enumerate(defects):
            item = dict(defect)
            item["defect_id"] = defect_id_for(
                defect["defect_fingerprint"], index)
            group_defects.append(item)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "report_id": report_id,
            "report_generation": generation,
            "qc_layer": QC_LAYER,
            "context": {
                "tenant_id": tenant_id,
                "project_id": str(context.get("project_id")),
                "series_id": str(context.get("series_id")),
                "branch_id": str(context.get("branch_id")),
                "episode_id": str(context.get("episode_id")),
                "home_cell": str(context.get("home_cell") or "unknown"),
                "manifest_ref": {
                    "id": str(manifest_ref.get("id") or artifact_ref["id"]),
                    "revision": int(manifest_ref.get("revision") or 1),
                    "content_hash": str(manifest_ref.get("content_hash")
                                        or artifact_sha256),
                },
                "run_id": str(body.get("run_id")),
                "execution_fence": int(context.get("execution_fence") or 1),
                "shot_lineage_id": str(body.get("shot_lineage_id")),
                "policy_ref": {
                    "service_id": "S02",
                    "id": str(policy_ref.get("id") or "policy"),
                    "revision": int(policy_ref.get("revision") or 1),
                    "content_hash": str(policy_ref.get("content_hash")
                                        or "0" * 64),
                },
                "trace_id": str(context.get("trace_id") or ""),
            },
            "subject": {
                "artifact_id": str(artifact_ref["id"]),
                "artifact_sha256": artifact_sha256,
                "artifact_kind": artifact_kind,
                "media": self._media_block(analysis=analysis,
                                           artifact=artifact,
                                           artifact_kind=artifact_kind),
            },
            "shot_ref": {
                "shot_id": shot_id,
                "shot_revision": shot_revision,
                "logical_node_id": str(context.get("node_id") or "node"),
                "node_epoch": int(context.get("node_epoch") or 1),
            },
            "evaluator": evaluator,
            "policy_version": policy_version,
            "sampling_policy_version": registry.SAMPLING_VERSION,
            "check_manifest": manifest_out,
            "defects": group_defects,
            "dimensions": build_dimensions(manifest=manifest,
                                           defects=group_defects,
                                           observations=observations),
            "coverage": coverage,
            "decision": decided["decision"],
            "decision_reason": decided["decision_reason"],
            "abstain": decided["abstain"],
            "confidence": confidence,
            "supersedes": supersedes,
            "repair_context": repair_context,
            "evidence_bundle_refs": [{
                "service_id": "S28",
                "id": str(artifact_ref["id"]),
                "revision": int(artifact_ref.get("revision") or 1),
                "content_hash": artifact_sha256,
            }],
            "created_at": _now(),
        }
        return payload

    # ------------------------------------------------------------ 落库
    def _persist(self, *, report_id, generation, key, tenant_id, context,
                 body, artifact_ref, artifact, artifact_kind, analysis,
                 evaluator, policy_version, manifest, defects, coverage,
                 decided, attempt_slot_id, payload, digest, command_id,
                 request_hash, units, audio_track_hash, language, asr_snapshot,
                 lexicon_revision, voice_binding_ref, cache_key,
                 asr_document) -> None:
        blocker = sum(1 for d in defects if d["severity"] == "blocker")
        major = sum(1 for d in defects if d["severity"] == "major")
        failed = (len((decided.get("abstain") or {}).get("failed_check_ids")
                      or []) if decided["decision"] == "abstain"
                  else sum(1 for e in manifest
                           if e["status"] in ("failed", "not_evaluated")))
        artifact_sha256 = payload["subject"]["artifact_sha256"]
        row = [
            ("report_id", report_id),
            ("report_generation", generation),
            ("derived_key", key),
            ("tenant_id", tenant_id),
            ("project_id", payload["context"]["project_id"]),
            ("series_id", payload["context"]["series_id"]),
            ("episode_id", payload["context"]["episode_id"]),
            ("branch_id", payload["context"]["branch_id"]),
            ("shot_id", payload["shot_ref"]["shot_id"]),
            ("shot_lineage_id", payload["context"]["shot_lineage_id"]),
            ("logical_node_id", payload["shot_ref"]["logical_node_id"]),
            # 两列分开写：execution_fence 是 run/branch 的，node_epoch 是
            # 单个 logical_node 的。这里不做任何比较或换算。
            ("node_epoch", int(payload["shot_ref"]["node_epoch"])),
            ("manifest_id", payload["context"]["manifest_ref"]["id"]),
            ("manifest_revision",
             int(payload["context"]["manifest_ref"]["revision"])),
            ("run_id", payload["context"]["run_id"]),
            ("execution_fence", int(payload["context"]["execution_fence"])),
            ("artifact_id", payload["subject"]["artifact_id"]),
            ("artifact_sha256", artifact_sha256),
            ("audio_track_hash", audio_track_hash),
            ("artifact_kind", artifact_kind),
            ("language", language),
            ("asr_model_snapshot", _jcs(asr_snapshot or {})),
            ("align_model_version", registry.ALIGN_MODEL_VERSION),
            ("lexicon_revision", lexicon_revision),
            ("voice_binding_ref", voice_binding_ref),
            ("evaluator_version", evaluator["version"]),
            ("policy_version", policy_version),
            ("sampling_version", registry.SAMPLING_VERSION),
            ("decision", decided["decision"]),
            ("decision_reason", decided["decision_reason"]),
            ("confidence", payload["confidence"]),
            ("coverage_ratio", coverage["coverage_ratio"]),
            ("required_coverage_min", coverage["required_coverage_min"]),
            ("defect_count", len(defects)),
            ("major_count", major),
            ("blocker_count", blocker),
            ("failed_check_count", failed),
            ("attempt_slot_id", attempt_slot_id),
            ("supersedes_report_id",
             payload["supersedes"]["report_id"]
             if payload["supersedes"] else None),
            ("supersedes_generation",
             payload["supersedes"]["report_generation"]
             if payload["supersedes"] else None),
            ("payload", _jcs(payload)),
            ("content_hash", digest),
            ("created_at", payload["created_at"]),
        ]
        columns = ", ".join(name for name, _ in row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(
            f"INSERT INTO report ({columns}) VALUES ({marks})",
            tuple(value for _, value in row))

        for ordinal, entry in enumerate(manifest):
            self.conn.execute(
                "INSERT INTO report_check (report_id, report_generation,"
                " ordinal, check_id, status, reason_code, defect_count)"
                " VALUES (?,?,?,?,?,?,?)",
                (report_id, generation, ordinal, entry["check_id"],
                 entry["status"], entry["reason_code"],
                 len(entry["defects"])))

        # 字级时间码：S38 的唯一来源。只收有时间码的 token（script_index 有值），
        # 没有对上的 token 不编一个起始时刻。
        unit_index = 0
        for utterance_index, utterance in enumerate(units):
            offset = _pos(body.get("known_audio_offset_seconds"))
            for token in _tokens_shifted(utterance, offset):
                index = token.get("script_index")
                if not isinstance(index, int) \
                        or not 0 <= index < len(utterance["script_tokens"]):
                    continue
                script = utterance["script_tokens"][index]
                low = token.get("ci_low_seconds")
                high = token.get("ci_high_seconds")
                low = (float(low) - offset) if _finite(low) else None
                high = (float(high) - offset) if _finite(high) else None
                if low is not None:
                    low = max(0.0, min(low, token["start_seconds"]))
                if high is not None:
                    high = max(high, token["end_seconds"])
                self.conn.execute(
                    "INSERT INTO alignment_unit (tenant_id, report_id,"
                    " report_generation, unit_index, utterance_index,"
                    " utterance_id, speaker_id, char_offset, char_length,"
                    " start_seconds, end_seconds, ci_low_seconds,"
                    " ci_high_seconds, confidence, phoneme_seq)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tenant_id, report_id, generation, unit_index,
                     utterance_index, str(utterance["utterance_id"]),
                     utterance["speaker_id"], int(script["char_offset"]),
                     int(script["char_length"]), token["start_seconds"],
                     token["end_seconds"], low, high,
                     max(0.0, min(1.0, token["alignment_confidence"])),
                     _jcs(token.get("phonemes")) if token.get("phonemes")
                     else None))
                unit_index += 1

        for segment_index, segment in enumerate(
                analysis.get("speaker_segments") or []):
            if not isinstance(segment, dict):
                continue
            start = _pos(segment.get("start_seconds"))
            end = _pos(segment.get("end_seconds"))
            if end <= start:
                continue
            self.conn.execute(
                "INSERT INTO speaker_segment (tenant_id, report_id,"
                " report_generation, segment_index, speaker_id, start_seconds,"
                " end_seconds, confidence) VALUES (?,?,?,?,?,?,?,?)",
                (tenant_id, report_id, generation, segment_index,
                 str(segment.get("speaker_id") or "unknown"), start, end,
                 max(0.0, min(1.0, _pos(segment.get("confidence"), 1.0)))))

        # 评估器调用：只记录**真的调用过**的项。`not_applicable` 与
        # `dependency_unavailable` 没有调用，就没有行——编一行 outcome=ok
        # 等于伪造"评估器跑过"。
        asr = analysis.get("asr") if isinstance(analysis.get("asr"), dict) else {}
        lipsync = analysis.get("lipsync") if isinstance(
            analysis.get("lipsync"), dict) else {}
        for entry in manifest:
            check_id = entry["check_id"]
            if check_id in NEEDS_ASR:
                source = asr
            elif check_id in NEEDS_LIPSYNC:
                source = lipsync
            else:
                continue
            if entry["status"] == "not_applicable":
                continue
            outcome = {"evaluated": "ok", "failed": (
                "timeout" if entry["reason_code"] == "evaluator_timeout"
                else "error")}.get(entry["status"])
            if outcome is None:
                continue
            self.conn.execute(
                "INSERT OR IGNORE INTO evaluator_call (call_id, tenant_id,"
                " report_id, report_generation, check_id, window_index,"
                " model_snapshot, prompt_hash, tokens_in, tokens_out,"
                " latency_ms, outcome, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_new_id("call"), tenant_id, report_id, generation, check_id,
                 None, _jcs(asr_snapshot or {}),
                 _sha256_hex(_jcs({"check_id": check_id,
                                   "analysis_version": ANALYSIS_VERSION,
                                   "artifact_sha256": artifact_sha256,
                                   "audio_track_hash": audio_track_hash,
                                   "language": language})),
                 int(source.get("tokens_in") or 0),
                 int(source.get("tokens_out") or 0),
                 max(0, int(source.get("latency_ms") or 0)), outcome, _now()))

        # ASR 缓存：只在真的拿到 ASR token 时写。命中缓存时上游原样返回同一份
        # ASR，写回是幂等的（ON CONFLICT DO NOTHING）。
        if cache_key and asr.get("utterances") and asr_document is None:
            self._store_asr(
                tenant_id, cache_key=cache_key,
                audio_track_hash=audio_track_hash,
                asr_snapshot=asr_snapshot or {},
                lexicon_revision=lexicon_revision,
                voice_binding_ref=voice_binding_ref, asr=asr)

        # §8：事件与报告同事务。abstain 也发——弃权是有记录的结论。
        self._emit(report_id=report_id, generation=generation,
                   tenant_id=tenant_id, payload=payload, digest=digest,
                   command_id=command_id, defect_count=len(defects),
                   blocker_count=blocker, coverage_ratio=coverage["coverage_ratio"],
                   attempt_slot_id=attempt_slot_id, language=language,
                   audio_track_hash=audio_track_hash,
                   max_lipsync_offset_ms=self._max_lipsync_offset(analysis),
                   voice_embedding_distance=self._voice_distance(analysis),
                   alignment_unit_count=unit_index)

    def _max_lipsync_offset(self, analysis):
        lipsync = analysis.get("lipsync")
        if not isinstance(lipsync, dict) or lipsync.get("outcome") not in (
                None, "ok"):
            return None
        value = lipsync.get("offset_ms")
        return int(round(abs(float(value)))) if _finite(value) else None

    def _voice_distance(self, analysis):
        voice = analysis.get("voice")
        if not isinstance(voice, dict):
            return None
        value = voice.get("embedding_distance")
        return round(float(value), 6) if _finite(value) else None

    def _emit(self, *, report_id, generation, tenant_id, payload, digest,
              command_id, defect_count, blocker_count, coverage_ratio,
              attempt_slot_id, language, audio_track_hash,
              max_lipsync_offset_ms, voice_embedding_distance,
              alignment_unit_count) -> None:
        context = payload["context"]
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_COMPLETED,
            # `subject` 与 `data.shot_id` 同源赋值，代码里不可能分叉。
            subject=payload["shot_ref"]["shot_id"],
            tenant_id=tenant_id,
            home_cell=str(context.get("home_cell") or "unknown"),
            aggregate_id=report_id, sequence=generation,
            correlation_id=str(context.get("run_id") or command_id),
            causation_id=command_id,
            data={
                "schema_version": SCHEMA_VERSION,
                "report_id": report_id,
                "report_generation": generation,
                "qc_layer": QC_LAYER,
                "artifact_sha256": payload["subject"]["artifact_sha256"],
                "audio_track_hash": audio_track_hash,
                "shot_id": payload["shot_ref"]["shot_id"],
                "shot_lineage_id": context["shot_lineage_id"],
                "node_epoch": int(payload["shot_ref"]["node_epoch"]),
                "run_id": context["run_id"],
                "execution_fence": int(context["execution_fence"]),
                "manifest_id": context["manifest_ref"]["id"],
                "manifest_revision": int(context["manifest_ref"]["revision"]),
                "decision": payload["decision"],
                "coverage_ratio": coverage_ratio,
                "attempt_slot_id": attempt_slot_id,
                "language": language,
                # 无对白 no-op / 无视频时这两个量没有可测值 → null，不是 0
                # （S37-14：0ms 偏移与"没测"是两件事）。
                "max_lipsync_offset_ms": max_lipsync_offset_ms,
                "voice_embedding_distance": voice_embedding_distance,
                "alignment_unit_count": alignment_unit_count,
                "defect_count": defect_count,
                "blocker_count": blocker_count,
            },
            data_ref={"service_id": SOURCE, "id": report_id,
                      "revision": generation, "content_hash": digest},
            schema_version=SCHEMA_VERSION))

    # ------------------------------------------------------------ 查询
    def get_report(self, tenant_id: str, report_id: str,
                   generation: str | int | None = None) -> dict:
        """跨租户与不存在返回同一个 404，不泄漏存在性。"""
        if generation in (None, "", "latest"):
            row = _row(self.conn.execute(
                "SELECT * FROM report WHERE tenant_id=? AND report_id=?"
                " ORDER BY report_generation DESC LIMIT 1",
                (tenant_id, report_id)).fetchone())
        else:
            try:
                number = int(generation)
            except (TypeError, ValueError):
                raise ServiceError(
                    422, "INVALID_GENERATION",
                    f"generation={generation!r} is not an integer or 'latest'")
            row = _row(self.conn.execute(
                "SELECT * FROM report WHERE tenant_id=? AND report_id=?"
                " AND report_generation=?", (tenant_id, report_id, number)
            ).fetchone())
        if row is None:
            raise ServiceError(404, "REPORT_NOT_FOUND",
                               f"no alignment report {report_id}")
        return {
            "payload": _loads(row["payload"], {}),
            "report_generation": row["report_generation"],
            "decision": row["decision"],
            "content_hash": row["content_hash"],
            # §6.2 承诺的投影水位：本内核同步落库、无投影，取落库时刻。
            "source_revision": row["report_generation"],
            "projection_watermark": row["created_at"],
        }

    def get_units(self, tenant_id: str, report_id: str,
                  generation: str | int | None = None,
                  start=None, end=None) -> dict:
        """§6 的字级时间码。**S38 的唯一时间码来源（I2）**。

        默认只返回最大代数：被 supersede 的那一代的时间码不能冒充当前结论。
        """
        report = self.get_report(tenant_id, report_id, generation)
        number = int(report["report_generation"])
        clause = ["tenant_id=?", "report_id=?", "report_generation=?"]
        params: list = [tenant_id, report_id, number]
        if _finite(start):
            clause.append("end_seconds >= ?")
            params.append(float(start))
        if _finite(end):
            clause.append("start_seconds <= ?")
            params.append(float(end))
        rows = self.conn.execute(
            "SELECT unit_index, char_offset, char_length, start_seconds,"
            " end_seconds, ci_low_seconds, ci_high_seconds, confidence"
            " FROM alignment_unit WHERE " + " AND ".join(clause)
            + " ORDER BY unit_index", params).fetchall()
        segments = self.conn.execute(
            "SELECT speaker_id, start_seconds, end_seconds, confidence"
            " FROM speaker_segment WHERE tenant_id=? AND report_id=?"
            " AND report_generation=? ORDER BY segment_index",
            (tenant_id, report_id, number)).fetchall()
        return {
            "report_id": report_id,
            "report_generation": number,
            "time_base": "presentation_pts",
            "units": [{
                "index": row["unit_index"],
                "char_offset": row["char_offset"],
                "char_length": row["char_length"],
                "start": round(row["start_seconds"], 3),
                "end": round(row["end_seconds"], 3),
                "ci_low": (None if row["ci_low_seconds"] is None
                           else round(row["ci_low_seconds"], 3)),
                "ci_high": (None if row["ci_high_seconds"] is None
                            else round(row["ci_high_seconds"], 3)),
                "confidence": row["confidence"],
            } for row in rows],
            "speaker_segments": [{
                "speaker_id": row["speaker_id"],
                "start": round(row["start_seconds"], 3),
                "end": round(row["end_seconds"], 3),
                "confidence": row["confidence"],
            } for row in segments],
        }

    # ------------------------------------------------------------ 事件投影
    def consume_voice_authorization_event(self, tenant_id: str,
                                          envelope: dict) -> dict:
        """S15 音色授权事件 → 本地投影。**不是授权权威。**

        inbox 自己去重（`consumer + event_id`）；投影只用于发现"读到的状态
        比事件旧"。判定前仍实时回查 S15。
        """
        from .schema import CONSUMER_VOICE_AUTH

        event_id = str(envelope.get("event_id") or envelope.get("id") or "")
        if not event_id:
            raise ServiceError(400, "INVALID_BODY", "event_id is required")
        data = envelope.get("data") if isinstance(envelope.get("data"),
                                                   dict) else envelope
        voice_id = str(data.get("voice_id") or "")
        if not voice_id:
            raise ServiceError(400, "INVALID_BODY", "data.voice_id is required")
        with Transaction(self.conn):
            fresh = self.inbox.consume(CONSUMER_VOICE_AUTH, event_id)
            if not fresh:
                return {"accepted": True, "duplicate": True,
                        "voice_id": voice_id}
            state = str(data.get("state") or "unknown")
            effective = str(data.get("effective_state") or state)
            self.conn.execute(
                "INSERT INTO voice_authorization_projection (tenant_id,"
                " voice_id, state, effective_state, state_seq,"
                " state_changed_at, event_id, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(tenant_id, voice_id) DO UPDATE SET"
                " state=excluded.state, effective_state=excluded.effective_state,"
                " state_seq=excluded.state_seq,"
                " state_changed_at=excluded.state_changed_at,"
                " event_id=excluded.event_id, updated_at=excluded.updated_at"
                " WHERE excluded.state_seq >= voice_authorization_projection.state_seq",
                (tenant_id, voice_id, state, effective,
                 int(data.get("state_seq") or 0), data.get("state_changed_at"),
                 event_id, _now()))
        return {"accepted": True, "duplicate": False, "voice_id": voice_id,
                "state": state, "effective_state": effective}
