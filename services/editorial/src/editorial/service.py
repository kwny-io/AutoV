"""S41 剪辑与时间线：内核。

三条不变量，后面的每段代码都要能对上：

1. **本服务不推进任何接受决策。** 提交成功只表示「新 revision 已落库」。
   `timeline` 上有 `current_revision`（本服务自己的写序列）与
   `execution_fence`（S08 的投影），没有任何表示「已被接受」的列，
   也没有写它的语句。接受由 S08 用 CAS 决定（方案 §7.3）。

2. **`execution_fence` 与 `node_epoch` 不可互换。** fence 的作用域是
   run/branch，放在 `timeline` 行上；epoch 的作用域是单个 `logical_node`，
   只出现在 `built_against` 里原样透传。本服务**从不**写 epoch，也**从不**
   用 epoch 推进或作废任何本地状态（方案 §7.3、AGENTS.md）。
   旧 epoch 的结果不会推进任何东西——它只是被记成「我是基于那一版剪的」。

3. **EDL / completeness / diff 是纯函数。** 它们只吃 revision payload、
   fps、tick_rate 与取整规则；不读时钟、不读上游。任何不确定性进这三步，
   `content_hash` 就不再可复算（方案 §4.1 末段）。

并发模型：SQLite 的 `BEGIN IMMEDIATE` 是全库写锁，严格强于方案要求的
「锁 timeline 一行」。CAS 的语义（`expected_revision` / `expected_fence`）
仍由本层显式校验，不依赖锁的强弱。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Inbox, Outbox, new_event

from .schema import (
    CONSUMER,
    DIFF_EXPLOSION_LIMIT,
    EVENT_REVISION_CREATED,
    ID_PREFIX,
    ROUNDING_RULE,
    SCHEMA,
    SOURCE,
    STEM_TRACK_KINDS,
    TICK_RATE,
    TIMELINE_KINDS,
    TIMELINE_STATES,
    TOPIC,
    TRACK_KINDS,
)

# §7.1：request_hash 不含 trace 与时间戳，否则同一逻辑命令换个 trace 就是
# 另一个幂等键。只剥 context 里的这几个键，不递归到 clips。
VOLATILE_CONTEXT_KEYS = ("trace_id", "traceparent", "timestamp",
                         "requested_at", "issued_at", "server_time")

# 同键不同 body。方案 §3.3 用这个名字；SDK 的 CommandConflict.code 是
# IDEMPOTENCY_KEY_REUSED，HTTP 层做映射（CONTRACT_GAPS E41-IDEM-CODE）。
IDEMPOTENCY_MISMATCH = "IDEMPOTENCY_MISMATCH"

# 判定「内容变没变」的字段。rec_in 不在里面：只改 rec_in 是位移（shifted），
# §4.3 要求单独一类，好让 S42 复用已渲染的 segment。
CONTENT_FIELDS = ("track_id", "src_in_ticks", "src_out_ticks",
                  "speed_num", "speed_den")

# 差分里的短名，照 §4.3 的示例（`src_in` 不是 `src_in_ticks`）。
# speed 两列在方案示例里就叫 speed_num / speed_den，保持原名。
DIFF_FIELD_NAMES = {"src_in_ticks": "src_in", "src_out_ticks": "src_out",
                    "rec_in_ticks": "rec_in", "rec_out_ticks": "rec_out",
                    "speed_num": "speed_num", "speed_den": "speed_den"}


# ------------------------------------------------------------------ 基础工具

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


def _loads(text, default=None):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _dumps(value) -> str:
    return _jcs(value)


def bare_hash(value) -> str | None:
    """剥掉 `sha256:` 前缀（CONTRACT_GAPS E41-HASH）。

    方案示例写 `sha256:...`，DDL 的 CHECK 与事件 schema 都是裸 hex。
    这里统一成裸 hex；不是 64 位 hex 就返回 None，让调用方按「缺 hash」处理。
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        return None
    return text


def round_half_up(numerator: int, denominator: int) -> int:
    """非负数四舍五入。只用整数，绝不经过 float。

    `fps_num/fps_den` 是 24000/1001 这类有理数，写成 float 再乘会漂
    （§2.2 最后一段）。负数不走这条路：调用方先校验非负。
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator < 0:
        raise ValueError("round_half_up is defined for non-negative values")
    return (numerator * 2 + denominator) // (2 * denominator)


class ServiceError(Exception):
    """带状态码的领域错误。`status` 决定 HTTP 层，`code` 决定调用方分支。"""

    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` 起手，异常即回滚。

    `BEGIN IMMEDIATE` 立刻拿写锁，把「先读后写」的竞态挡在锁外。不要改成
    默认的 deferred：那会在第一次写时才拿锁，两个并发提交都能读到同一个
    `current_revision`，CAS 变成摆设。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------------ 纯函数


def stable_request_hash(body: dict) -> str:
    """§7.1 的幂等摘要：业务字段全进，trace 与时间戳不进。

    `canonical_request_hash` 只剥 `command_id` / `request_hash`。因为 trace_id
    在 `context` 里面，这里先浅拷贝 context 去掉易变键，再交给 SDK。
    """
    if not isinstance(body, dict):
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    candidate = dict(body)
    context = candidate.get("context")
    if isinstance(context, dict):
        candidate["context"] = {k: v for k, v in context.items()
                                if k not in VOLATILE_CONTEXT_KEYS}
    from idempotency_sdk import canonical_request_hash
    return canonical_request_hash(candidate)


def compute_content_hash(tracks: list[dict], clips: list[dict],
                         fps_num: int, fps_den: int, tick_rate: int,
                         rounding_rule: str = ROUNDING_RULE) -> str:
    """revision 的内容指纹。

    **不含** revision 号、时间、提交人、change_cause —— 那两个 revision
    内容相同就该得到同一个 hash（PTR-3 因此才有意义）。含 `rounding_rule`：
    改取整规则必须得到新 hash，否则下游会以为旧 EDL 仍有效（§2.2 规则 2）。
    """
    payload = {
        "fps": {"num": fps_num, "den": fps_den},
        "tick_rate": tick_rate,
        "rounding_rule": rounding_rule,
        "tracks": sorted(({"track_id": t["track_id"], "kind": t["kind"]}
                          for t in tracks), key=lambda t: t["track_id"]),
        "clips": sorted(({
            "clip_id": c["clip_id"],
            "track_id": c["track_id"],
            "link_group_id": c.get("link_group_id"),
            "ordinal": c["ordinal"],
            "src_artifact_ref": c["src_artifact_ref"],
            "src_in_ticks": c["src_in_ticks"],
            "src_out_ticks": c["src_out_ticks"],
            "rec_in_ticks": c["rec_in_ticks"],
            "rec_out_ticks": c["rec_out_ticks"],
            "speed_num": c["speed_num"],
            "speed_den": c["speed_den"],
            "transition_in": c.get("transition_in"),
            "transition_out": c.get("transition_out"),
            "unlinked_at_revision": c.get("unlinked_at_revision"),
            "drift_reason": c.get("drift_reason"),
        } for c in clips), key=lambda c: (c["track_id"], c["ordinal"],
                                          c["clip_id"])),
    }
    return _sha256_hex(_jcs(payload))


def compute_edl(clips: list[dict], fps_num: int, fps_den: int,
                tick_rate: int = TICK_RATE) -> list[dict]:
    """revision → EDL。确定性：同一输入永远逐字节相同。

    帧号 = round_half_up(rec_ticks * fps_num / (fps_den * tick_rate))，
    只在**这里**做一次取整；中间的 rec_out 都是整数 tick，不取整到帧
    （§2.2 规则 1）。每个 track 独立编号 `entry_no` 从 1 开始。
    """
    by_track: dict[str, list[dict]] = {}
    for clip in clips:
        by_track.setdefault(clip["track_id"], []).append(clip)
    entries: list[dict] = []
    for track_id in sorted(by_track):
        ordered = sorted(by_track[track_id],
                         key=lambda c: (c["rec_in_ticks"], c["clip_id"]))
        for index, clip in enumerate(ordered, start=1):
            entries.append({
                "track_id": track_id,
                "entry_no": index,
                "clip_id": clip["clip_id"],
                "record_in_frame": round_half_up(
                    clip["rec_in_ticks"] * fps_num, fps_den * tick_rate),
                "record_out_frame": round_half_up(
                    clip["rec_out_ticks"] * fps_num, fps_den * tick_rate),
                "src_in_ticks": clip["src_in_ticks"],
                "src_out_ticks": clip["src_out_ticks"],
                "speed_num": clip["speed_num"],
                "speed_den": clip["speed_den"],
                "transition_in": clip.get("transition_in"),
                "transition_out": clip.get("transition_out"),
            })
    return entries


def compute_edl_hash(entries: list[dict]) -> str:
    return _sha256_hex(_jcs(entries))


def duration_ticks(clips: list[dict]) -> int:
    """时间线总长 = 所有片段 `rec_out` 的最大值；没有片段是 0。"""
    return max((c["rec_out_ticks"] for c in clips), default=0)


def merge_ranges(ranges: list[dict]) -> list[dict]:
    """合并闭开区间并按 start 排序。线性扫描，不是 O(n²)（§4.5 末段）。

    零长区间丢弃：`[a,a)` 里没有任何时刻需要重算。
    """
    cleaned = sorted(
        ({"start": int(r["start"]), "end": int(r["end"])}
         for r in ranges if int(r["end"]) > int(r["start"])),
        key=lambda r: (r["start"], r["end"]))
    merged: list[dict] = []
    for item in cleaned:
        if merged and item["start"] <= merged[-1]["end"]:
            if item["end"] > merged[-1]["end"]:
                merged[-1]["end"] = item["end"]
        else:
            merged.append(dict(item))
    return merged


def compute_diff(old_clips: list[dict], new_clips: list[dict],
                 from_revision: int, to_revision: int,
                 old_duration: int, new_duration: int,
                 change_cause: dict | None = None) -> dict:
    """两次 revision 的差分（§4.3）。

    四条规则都在这一个函数里，缺一条下游就会拿到错区间：

    1. `affected_output_ranges` 是**合并后的并集**，不是逐 clip 列表。
    2. 区间必须覆盖**旧位置与新位置**——片段移走后旧位置同样要重算，
       否则下游在旧位置会留下残影。
    3. `shifted` 与 `changed` 分开：只改 `rec_in` 的片段内容没变，
       S42 可以搬已渲染的 segment 而不重渲染。
    4. 保守超集：多算是性能问题，少算是交付缺陷。

    区间数超过 `DIFF_EXPLOSION_LIMIT` 时降级为「整条时间线受影响」，
    并在 payload 里标 `diff_mode="coarse"`（§12 第 6 条：不标的话下游会
    以为这是精确区间）。
    """
    old_by_id = {c["clip_id"]: c for c in old_clips}
    new_by_id = {c["clip_id"]: c for c in new_clips}

    added = []
    removed = []
    changed = []
    shifted = []
    touched_tracks: set[str] = set()
    ranges: list[dict] = []

    for clip_id in sorted(set(new_by_id) - set(old_by_id)):
        clip = new_by_id[clip_id]
        added.append({"clip_id": clip_id, "track_id": clip["track_id"],
                      "rec_in": clip["rec_in_ticks"],
                      "rec_out": clip["rec_out_ticks"]})
        touched_tracks.add(clip["track_id"])
        ranges.append({"start": clip["rec_in_ticks"],
                       "end": clip["rec_out_ticks"]})

    for clip_id in sorted(set(old_by_id) - set(new_by_id)):
        clip = old_by_id[clip_id]
        removed.append({"clip_id": clip_id, "track_id": clip["track_id"]})
        touched_tracks.add(clip["track_id"])
        ranges.append({"start": clip["rec_in_ticks"],
                       "end": clip["rec_out_ticks"]})

    for clip_id in sorted(set(old_by_id) & set(new_by_id)):
        before = old_by_id[clip_id]
        after = new_by_id[clip_id]
        fields = [f for f in CONTENT_FIELDS if before[f] != after[f]]
        if fields:
            # `rec_out` 是派生值（E41-REC-OUT），不在 CONTENT_FIELDS 里，但
            # §4.3 的示例把 `["src_out","rec_out"]` 一起报给下游——S42 要按它
            # 决定重渲染哪一段。**只在内容已经变了时**补：`rec_out` 会随
            # `rec_in` 一起动，先补会把「纯位移」误判成 changed，
            # 让 S42 白重渲染（§4.3 规则 3）。
            if (before["rec_out_ticks"] != after["rec_out_ticks"]
                    and "rec_out_ticks" not in fields):
                fields.append("rec_out_ticks")
            old_view, new_view = {}, {}
            for field in ("src_in_ticks", "src_out_ticks", "rec_in_ticks",
                          "rec_out_ticks", "speed_num", "speed_den"):
                old_view[DIFF_FIELD_NAMES[field]] = before[field]
                new_view[DIFF_FIELD_NAMES[field]] = after[field]
            changed.append({
                "clip_id": clip_id, "track_id": after["track_id"],
                "fields": [DIFF_FIELD_NAMES.get(f, f) for f in fields],
                "old": old_view, "new": new_view,
            })
            touched_tracks.add(after["track_id"])
            if before["track_id"] != after["track_id"]:
                touched_tracks.add(before["track_id"])
            ranges.append({"start": before["rec_in_ticks"],
                           "end": before["rec_out_ticks"]})
            ranges.append({"start": after["rec_in_ticks"],
                           "end": after["rec_out_ticks"]})
            continue
        delta = after["rec_in_ticks"] - before["rec_in_ticks"]
        if delta != 0:
            shifted.append({"clip_id": clip_id,
                            "track_id": after["track_id"],
                            "delta_ticks": delta})
            touched_tracks.add(after["track_id"])
            ranges.append({"start": before["rec_in_ticks"],
                           "end": before["rec_out_ticks"]})
            ranges.append({"start": after["rec_in_ticks"],
                           "end": after["rec_out_ticks"]})

    # 总长变化本身就是一次输出变化：变短时尾部要重算（旧内容被截掉），
    # 变长时新出现的尾部也要重算。不加这条，最后一个片段的 trim 会漏。
    duration_span = None
    if old_duration != new_duration:
        duration_span = {"start": min(old_duration, new_duration),
                         "end": max(old_duration, new_duration)}

    merged = merge_ranges(ranges)
    diff_mode = "exact"
    if len(merged) > DIFF_EXPLOSION_LIMIT:
        # 爆炸判定只看**片段级**区间。总长区间若先并进来，跨度会覆盖整条
        # 时间线，合并结果永远只剩 1 个区间 —— 也就是说 `diff_mode` 会在
        # 最该报 coarse 的时候报 exact，下游会把一个全量重算当精确区间用。
        merged = [{"start": 0, "end": max(old_duration, new_duration)}]
        diff_mode = "coarse"
        touched_tracks = touched_tracks or {
            c["track_id"] for c in list(old_clips) + list(new_clips)}
    elif duration_span is not None:
        merged = merge_ranges(merged + [duration_span])
        if len(merged) > DIFF_EXPLOSION_LIMIT:
            merged = [{"start": 0, "end": max(old_duration, new_duration)}]
            diff_mode = "coarse"
            touched_tracks = touched_tracks or {
                c["track_id"] for c in list(old_clips) + list(new_clips)}

    body = {
        "from_revision": from_revision,
        "to_revision": to_revision,
        "clips": {"added": added, "removed": removed,
                  "changed": changed, "shifted": shifted},
        # PTR-1：同一个集合有三个名字。两个键都给同值。
        "affected_output_ranges": merged,
        "affected_ranges": merged,
        "affected_tracks": sorted(touched_tracks),
        "change_causes": [change_cause] if change_cause else [],
        "diff_mode": diff_mode,
    }
    body["diff_hash"] = _sha256_hex(_jcs(
        {k: v for k, v in body.items() if k != "diff_hash"}))
    return body


def compute_completeness(tracks: list[dict], clips: list[dict],
                         stems: list[str],
                         subtitle_locale: str | None,
                         gaps: list[dict]) -> dict:
    """§4.4：completeness 是**计算字段**，不是可自由设置的标记。

    素材类缺口（missing_artifact / hash_mismatch / insufficient_handles）
    已在提交校验里 422，进不来这里；所以这里只剩「能提交但不能做正式
    master」的两类：缺 stem、缺字幕轨（CONTRACT_GAPS E41-GAPS-422）。

    `incomplete` 的 revision 可以提交、可以预览，但
    `formal_master_eligible=false`。
    """
    computed = list(gaps)
    kinds_on_timeline = {t["kind"] for t in tracks}
    for stem in stems:
        # "mix" 是最终混音，不是一路 stem；只有 dialogue/sfx/bgm 需要在
        # 时间线里有对应音轨。
        if stem not in STEM_TRACK_KINDS:
            continue
        if stem not in kinds_on_timeline:
            computed.append({"kind": "missing_stem", "stem": stem})
    if subtitle_locale and "subtitle" not in kinds_on_timeline:
        computed.append({"kind": "missing_subtitle_track",
                         "locale": subtitle_locale})
    complete = not computed
    return {
        "completeness": "complete" if complete else "incomplete",
        "gaps": computed,
        "formal_master_eligible": complete,
    }


def compute_output_plan(tracks: list[dict], clips: list[dict],
                        stems: list[str], subtitle_locale: str | None,
                        master_spec_ref: dict | None) -> dict:
    """§5 的 output_plan。segment 划分方案没定义（E41-SEGMENTS）。"""
    segments = [{"segment_no": index, "rec_in": r["start"],
                 "rec_out": r["end"],
                 "est_seconds": round((r["end"] - r["start"]) / TICK_RATE, 3)}
                for index, r in enumerate(
                    merge_ranges([{"start": c["rec_in_ticks"],
                                   "end": c["rec_out_ticks"]}
                                  for c in clips]), start=1)]
    plan = {
        "segments": segments,
        "stems": list(stems),
        "subtitle_track_ref": ({"locale": subtitle_locale}
                               if subtitle_locale else None),
        "master_spec_ref": master_spec_ref,
    }
    plan["plan_hash"] = _sha256_hex(_jcs(plan))
    return plan


def compute_mix_plan(mix_plan: dict | None) -> dict | None:
    """§2.5：MixPlan 只存计划，不存任何实测值（响度、峰值都不测）。"""
    if not mix_plan:
        return None
    plan = {
        "target_lufs": mix_plan.get("target_lufs"),
        "true_peak_ceiling": mix_plan.get("true_peak_ceiling"),
        "ducking_policy_ref": mix_plan.get("ducking_policy_ref"),
        "stem_gains": mix_plan.get("stem_gains") or {},
        "notes": mix_plan.get("notes"),
    }
    plan["plan_hash"] = _sha256_hex(_jcs(plan))
    return plan


# ------------------------------------------------------------------ 校验

def _require_int(value, field: str, *, minimum: int | None = None,
                 maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be an integer",
                           details={"field": field})
    if minimum is not None and value < minimum:
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be >= {minimum}",
                           details={"field": field, "minimum": minimum})
    if maximum is not None and value > maximum:
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be <= {maximum}",
                           details={"field": field, "maximum": maximum})
    return value


def _require_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be a non-empty string",
                           details={"field": field})
    return value


def _require_object(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be an object",
                           details={"field": field})
    return value


def _require_list(value, field: str) -> list:
    if not isinstance(value, list):
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be an array",
                           details={"field": field})
    return value


def _transition(value, field: str) -> dict | None:
    if value is None:
        return None
    item = _require_object(value, field)
    _require_text(item.get("kind"), f"{field}.kind")
    frames = item.get("frames", 0)
    if frames is not None:
        _require_int(frames, f"{field}.frames", minimum=0)
    return {"kind": item["kind"], "frames": frames or 0}


def _fps_ticks(frames: int, fps_num: int, fps_den: int) -> int:
    """帧数 → tick，整数运算，向下取整。

    用于把手（handle）需求换算到 tick；这里向下取整是**保守**方向：少算
    一点需求，等于允许刚好够的素材通过。
    """
    return (frames * fps_den * TICK_RATE) // fps_num


def normalize_request(body: dict) -> dict:
    """把命令体规范化成落库形状。只做结构校验，不读上游。

    规范化要点（都登记在 CONTRACT_GAPS）：

    - `rec_out_ticks` 不在命令体里，按 E41-REC-OUT 派生。
    - `src_artifact_ref.content_hash` 剥掉 `sha256:` 前缀（E41-HASH）。
    - clip 的 `track_kind` 从 `tracks` 连接得到（E41-TRACK-KIND）。
    - 每个 clip 的 `src_artifact_ref` 必须是「钉住的一版」：带 revision 与
      content_hash。少一个就不是可复算的引用。
    """
    context = _require_object(body.get("context"), "context")
    for key in ("project_id", "series_id", "episode_id", "branch_id"):
        _require_text(context.get(key), f"context.{key}")
    manifest_ref = _require_object(context.get("manifest_ref"),
                                   "context.manifest_ref")
    _require_text(manifest_ref.get("id"), "context.manifest_ref.id")
    fence = _require_int(context.get("execution_fence"),
                         "context.execution_fence", minimum=1)
    node_epoch = _require_int(context.get("node_epoch"),
                              "context.node_epoch", minimum=1)
    _require_text(context.get("node_id"), "context.node_id")
    actor = context.get("actor")
    if isinstance(actor, str):
        actor = {"principal_id": actor}
    actor = _require_object(actor, "context.actor")
    _require_text(actor.get("principal_id"), "context.actor.principal_id")

    expected_revision = _require_int(body.get("expected_revision"),
                                     "expected_revision", minimum=0)
    expected_fence = _require_int(body.get("expected_execution_fence"),
                                  "expected_execution_fence", minimum=1)
    if expected_fence != fence:
        # context 里的 fence 是「我基于哪一版剪的」，expected 是「我认为
        # 本地投影是哪一版」。两者不一致说明调用方自己都没对齐。
        raise ServiceError(
            422, "SCHEMA_INVALID",
            "expected_execution_fence must equal context.execution_fence",
            details={"field": "expected_execution_fence"})

    kind = body.get("timeline_kind", "master")
    if kind not in TIMELINE_KINDS:
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"timeline_kind must be one of {TIMELINE_KINDS}",
                           details={"field": "timeline_kind"})
    fps = _require_object(body.get("fps"), "fps")
    fps_num = _require_int(fps.get("num"), "fps.num", minimum=1)
    fps_den = _require_int(fps.get("den"), "fps.den", minimum=1)

    tracks = []
    seen_tracks: dict[str, str] = {}
    for raw in _require_list(body.get("tracks"), "tracks"):
        item = _require_object(raw, "tracks[]")
        track_id = _require_text(item.get("track_id"), "tracks[].track_id")
        track_kind = item.get("kind")
        if track_kind not in TRACK_KINDS:
            raise ServiceError(422, "SCHEMA_INVALID",
                               f"tracks[].kind must be one of {TRACK_KINDS}",
                               details={"field": "tracks[].kind",
                                        "track_id": track_id})
        if track_id in seen_tracks:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "duplicate track_id",
                               details={"field": "tracks[].track_id",
                                        "track_id": track_id})
        seen_tracks[track_id] = track_kind
        tracks.append({"track_id": track_id, "kind": track_kind})
    if not tracks:
        raise ServiceError(422, "SCHEMA_INVALID", "tracks must not be empty",
                           details={"field": "tracks"})

    raw_clips = _require_list(body.get("clips"), "clips")
    if not raw_clips:
        # E41-MIN：空片段列表不是「空时间线」，是没提交内容。
        raise ServiceError(422, "SCHEMA_INVALID", "clips must not be empty",
                           details={"field": "clips"})

    clips = []
    seen_clips: set[str] = set()
    seen_positions: set[tuple[str, int]] = set()
    for raw in raw_clips:
        item = _require_object(raw, "clips[]")
        clip_id = _require_text(item.get("clip_id"), "clips[].clip_id")
        if clip_id in seen_clips:
            raise ServiceError(422, "SCHEMA_INVALID", "duplicate clip_id",
                               details={"field": "clips[].clip_id",
                                        "clip_id": clip_id})
        seen_clips.add(clip_id)
        track_id = _require_text(item.get("track_id"), "clips[].track_id")
        if track_id not in seen_tracks:
            # E41-TRACK-KIND：track_kind 只能来自 tracks 的声明。
            raise ServiceError(422, "SCHEMA_INVALID",
                               "clip references an undeclared track_id",
                               details={"field": "clips[].track_id",
                                        "clip_id": clip_id,
                                        "track_id": track_id})
        ordinal = _require_int(item.get("ordinal", 0), "clips[].ordinal",
                               minimum=0)
        if (track_id, ordinal) in seen_positions:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "duplicate (track_id, ordinal)",
                               details={"clip_id": clip_id,
                                        "track_id": track_id,
                                        "ordinal": ordinal})
        seen_positions.add((track_id, ordinal))

        src_ref = _require_object(item.get("src_artifact_ref"),
                                  "clips[].src_artifact_ref")
        content_hash = bare_hash(src_ref.get("content_hash"))
        if content_hash is None:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "src_artifact_ref.content_hash must be a "
                               "64-character lowercase hex digest",
                               details={"field": "clips[].src_artifact_ref",
                                        "clip_id": clip_id})
        pinned = {
            "service_id": _require_text(src_ref.get("service_id") or "S28",
                                        "clips[].src_artifact_ref.service_id"),
            "id": _require_text(src_ref.get("id"),
                                "clips[].src_artifact_ref.id"),
            "revision": _require_int(src_ref.get("revision"),
                                     "clips[].src_artifact_ref.revision",
                                     minimum=1),
            "content_hash": content_hash,
        }

        src_in = _require_int(item.get("src_in_ticks"), "clips[].src_in_ticks",
                              minimum=0)
        src_out = _require_int(item.get("src_out_ticks"),
                               "clips[].src_out_ticks", minimum=0)
        if src_out <= src_in:
            raise ServiceError(422, "CLIP_OUT_OF_RANGE",
                               "src_out_ticks must be greater than src_in_ticks",
                               details={"clip_id": clip_id,
                                        "src_in_ticks": src_in,
                                        "src_out_ticks": src_out})
        rec_in = _require_int(item.get("rec_in_ticks"), "clips[].rec_in_ticks",
                              minimum=0)
        speed = item.get("speed") or {"num": 1, "den": 1}
        speed = _require_object(speed, "clips[].speed")
        speed_num = speed.get("num", 1)
        speed_den = speed.get("den", 1)
        if (isinstance(speed_num, bool) or not isinstance(speed_num, int)
                or isinstance(speed_den, bool)
                or not isinstance(speed_den, int)
                or speed_num <= 0 or speed_den <= 0):
            # §3.3 INVALID_SPEED：0、负、非整数都是「这个请求本身不成立」。
            raise ServiceError(422, "INVALID_SPEED",
                               "speed must be a rational number with positive "
                               "integer numerator and denominator",
                               details={"clip_id": clip_id,
                                        "speed": {"num": speed_num,
                                                  "den": speed_den}})
        # E41-REC-OUT：整数 tick 只在最后截断一次。
        rec_out = rec_in + ((src_out - src_in) * speed_den) // speed_num
        if rec_out <= rec_in:
            raise ServiceError(422, "CLIP_OUT_OF_RANGE",
                               "clip is shorter than one tick at this speed",
                               details={"clip_id": clip_id,
                                        "rec_in_ticks": rec_in,
                                        "speed": {"num": speed_num,
                                                  "den": speed_den}})
        # E41-MIN：按 fps 取整后不足一帧的片段会让 edl_entry 的
        # `record_out_frame > record_in_frame` 在写库时炸成 500。这里先拒。
        frame_in = round_half_up(rec_in * fps_num, fps_den * TICK_RATE)
        frame_out = round_half_up(rec_out * fps_num, fps_den * TICK_RATE)
        if frame_out <= frame_in:
            raise ServiceError(
                422, "CLIP_OUT_OF_RANGE",
                "clip is shorter than one frame at this fps",
                details={"clip_id": clip_id,
                         "rec_in_ticks": rec_in,
                         "rec_out_ticks": rec_out,
                         "record_in_frame": frame_in,
                         "record_out_frame": frame_out,
                         "reason": "shorter_than_one_frame"})

        clips.append({
            "clip_id": clip_id,
            "track_id": track_id,
            "track_kind": seen_tracks[track_id],
            "link_group_id": item.get("link_group_id") or None,
            "ordinal": ordinal,
            "src_artifact_ref": pinned,
            "src_in_ticks": src_in,
            "src_out_ticks": src_out,
            "rec_in_ticks": rec_in,
            "rec_out_ticks": rec_out,
            "speed_num": speed_num,
            "speed_den": speed_den,
            "transition_in": _transition(item.get("transition_in"),
                                         "clips[].transition_in"),
            "transition_out": _transition(item.get("transition_out"),
                                          "clips[].transition_out"),
            "unlinked_at_revision": item.get("unlinked_at_revision"),
            "drift_reason": item.get("drift_reason") or None,
        })

    _check_link_groups(clips, bool(body.get("allow_av_speed_drift")))
    _check_track_ranges(clips)

    output_plan = body.get("output_plan") or {}
    output_plan = _require_object(output_plan, "output_plan")
    stems = _require_list(output_plan.get("stems") or [], "output_plan.stems")
    for stem in stems:
        _require_text(stem, "output_plan.stems[]")
    subtitle_locale = output_plan.get("subtitle_locale")
    if subtitle_locale is not None:
        _require_text(subtitle_locale, "output_plan.subtitle_locale")

    mix_raw = body.get("mix_plan")
    if mix_raw is not None:
        mix_raw = _require_object(mix_raw, "mix_plan")
        for key in ("target_lufs", "true_peak_ceiling"):
            value = mix_raw.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"mix_plan.{key} must be a number",
                                   details={"field": f"mix_plan.{key}"})
            if not math.isfinite(float(value)):
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"mix_plan.{key} must be finite",
                                   details={"field": f"mix_plan.{key}"})

    master_spec_ref = context.get("master_spec_ref")
    if master_spec_ref is not None:
        master_spec_ref = _require_object(master_spec_ref,
                                          "context.master_spec_ref")

    return {
        "context": context,
        "manifest_ref": manifest_ref,
        "fence": fence,
        "node_epoch": node_epoch,
        "node_id": context["node_id"],
        "actor": actor,
        "project_id": context["project_id"],
        "series_id": context["series_id"],
        "episode_id": context["episode_id"],
        "branch_id": context["branch_id"],
        "expected_revision": expected_revision,
        "expected_fence": expected_fence,
        "timeline_kind": kind,
        "fps_num": fps_num,
        "fps_den": fps_den,
        "tracks": tracks,
        "clips": clips,
        "stems": stems,
        "subtitle_locale": subtitle_locale,
        "mix_plan": compute_mix_plan(mix_raw),
        "master_spec_ref": master_spec_ref,
        "change_cause": body.get("change_cause"),
        "allow_av_speed_drift": bool(body.get("allow_av_speed_drift")),
    }


def _check_link_groups(clips: list[dict], allow_drift: bool) -> None:
    """§2.4：同组成员 `rec_in` 与 `speed` 必须一致。

    不一致时只有两条合法出路：显式 `allow_av_speed_drift` 且每个成员写了
    `drift_reason`。不写理由就放行，等于把「视频剪了 4 秒、音频没动」这种
    口型事故留给 S36 发现。
    """
    groups: dict[str, list[dict]] = {}
    for clip in clips:
        if clip["link_group_id"]:
            groups.setdefault(clip["link_group_id"], []).append(clip)
    for group_id, members in groups.items():
        offsets = {m["rec_in_ticks"] for m in members}
        speeds = {(m["speed_num"], m["speed_den"]) for m in members}
        if len(offsets) <= 1 and len(speeds) <= 1:
            continue
        if allow_drift and all(m["drift_reason"] for m in members):
            continue
        raise ServiceError(
            422, "LINK_GROUP_DESYNC",
            "link group members disagree on rec_in or speed",
            details={
                "link_group_id": group_id,
                "clip_ids": sorted(m["clip_id"] for m in members),
                "rec_in_ticks": sorted(offsets),
                "speeds": sorted(speeds),
                "reason": ("drift_reason_required" if allow_drift
                           else "link_group_desync"),
            })


def _check_track_ranges(clips: list[dict]) -> None:
    """同轨重叠只允许在转场覆盖的范围内（§5 的注）。

    `clip_track_pos_uniq` 只挡「起点相同」，不挡重叠——转场本来就要重叠。
    区间合法性在这里判：有重叠就必须有一侧写了 transition，且重叠量不能
    超过该侧的素材 handle。
    """
    by_track: dict[str, list[dict]] = {}
    for clip in clips:
        by_track.setdefault(clip["track_id"], []).append(clip)
    for track_id, members in by_track.items():
        ordered = sorted(members, key=lambda c: (c["rec_in_ticks"], c["clip_id"]))
        for index, clip in enumerate(ordered):
            if index == 0:
                continue
            previous = ordered[index - 1]
            if clip["rec_in_ticks"] >= previous["rec_out_ticks"]:
                continue
            overlap = previous["rec_out_ticks"] - clip["rec_in_ticks"]
            has_transition = bool(clip["transition_in"]
                                  or previous["transition_out"])
            if not has_transition:
                raise ServiceError(
                    422, "CLIP_OUT_OF_RANGE",
                    "clips overlap on the same track without a transition",
                    details={"clip_id": clip["clip_id"],
                             "track_id": track_id,
                             "overlap_ticks": overlap,
                             "reason": "overlap_without_transition"})
            # 入点之前要有 src_in 那么多的素材在手（handle）。出点那一侧
            # 需要 S28 的素材时长，放在 check_out_handles 里（读上游之后）。
            incoming_needed = (overlap * clip["speed_den"]) // clip["speed_num"]
            if clip["transition_in"] and clip["src_in_ticks"] < incoming_needed:
                raise ServiceError(
                    422, "INSUFFICIENT_HANDLES",
                    "transition needs more source handle before the in-point",
                    details={"clip_id": clip["clip_id"],
                             "required_ticks": incoming_needed,
                             "available_ticks": clip["src_in_ticks"],
                             "reason": "incoming_handle"})


def check_out_handles(clips: list[dict]) -> None:
    """出点之后的 handle 只能读 S28 的时长才知道，所以在复查之后单独跑。

    结构校验阶段 `duration_ticks` 尚未填充，那时跳过这一支；否则每个带
    `transition_out` 的片段都会被误判成「素材时长未知」。
    """
    by_track: dict[str, list[dict]] = {}
    for clip in clips:
        by_track.setdefault(clip["track_id"], []).append(clip)
    for members in by_track.values():
        ordered = sorted(members, key=lambda c: (c["rec_in_ticks"],
                                                 c["clip_id"]))
        for index, clip in enumerate(ordered):
            if index == 0:
                continue
            previous = ordered[index - 1]
            if clip["rec_in_ticks"] >= previous["rec_out_ticks"]:
                continue
            if not previous["transition_out"]:
                continue
            overlap = previous["rec_out_ticks"] - clip["rec_in_ticks"]
            needed = (overlap * previous["speed_den"]) // previous["speed_num"]
            available = previous.get("duration_ticks")
            if available is None:
                raise ServiceError(
                    422, "INSUFFICIENT_HANDLES",
                    "artifact duration is unknown, cannot prove the outgoing "
                    "handle exists",
                    details={"clip_id": previous["clip_id"],
                             "required_ticks": needed,
                             "available_ticks": None,
                             "reason": "artifact_duration_unknown"})
            tail = available - previous["src_out_ticks"]
            if tail < needed:
                raise ServiceError(
                    422, "INSUFFICIENT_HANDLES",
                    "transition needs more source handle after the out-point",
                    details={"clip_id": previous["clip_id"],
                             "required_ticks": needed,
                             "available_ticks": tail,
                             "reason": "outgoing_handle"})


# ------------------------------------------------------------------ 服务

class TimelineService:
    """一个 `timeline` 行的写序列 + 三个纯函数的编排。

    构造顺序不能改：`executescript(SCHEMA)` 会隐式提交，`Outbox.__init__`
    里也有一个 `executescript`。两者都必须在任何 `BEGIN IMMEDIATE` 之前
    完成，否则事务里的 COMMIT/ROLLBACK 会撞上「cannot commit - no
    transaction is active」。
    """

    def __init__(self, conn: sqlite3.Connection, artifact_source=None,
                 clock=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)
        self.artifact_source = artifact_source
        self.clock = clock or _now
        self.metrics: dict[str, int] = {}

    # ------------------------------------------------------------ 工具
    def _bump(self, name: str, amount: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def _row(self, row) -> dict | None:
        return None if row is None else dict(row)

    def _timeline_row(self, tenant_id: str, timeline_id: str) -> dict | None:
        return self._row(self.conn.execute(
            "SELECT * FROM timeline WHERE tenant_id = ? AND timeline_id = ?",
            (tenant_id, timeline_id)).fetchone())

    def _require_timeline(self, tenant_id: str, timeline_id: str) -> dict:
        """跨租户与不存在返回**同一个** 404（E28-S41-01）。

        403 会泄漏「这个 id 存在但不属于你」。这里连 details 都不带
        timeline_id 之外的东西。
        """
        row = self._timeline_row(tenant_id, timeline_id)
        if row is None:
            raise ServiceError(404, "TIMELINE_NOT_FOUND",
                               "timeline not found",
                               details={"timeline_id": timeline_id})
        return row

    # ------------------------------------------------------ 上游（S28）
    def _fetch_artifact(self, tenant_id: str, ref: dict) -> dict:
        """实时读 S28。**不缓存**（§4.1 第 6 步）。"""
        if self.artifact_source is None:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "artifact source (S28) is not configured",
                retryable=True,
                details={"dependency": "S28", "reason": "not_configured"})
        try:
            return self.artifact_source(tenant_id, ref["id"], ref["revision"])
        except ServiceError as exc:
            if exc.status == 404:
                # 跨租户时 S28 也是 404；这里不区分，避免探测存在性（E28-S41-02）。
                raise ServiceError(
                    422, "SOURCE_ARTIFACT_MISSING",
                    "source artifact does not exist",
                    details={"clip_id": ref.get("clip_id"),
                             "artifact_ref": _public_ref(ref)}) from exc
            if exc.status >= 500 or exc.status == 409:
                # 5xx 与 409（上游一时判断不了）都是「现在判断不了」，不是
                # 「这个请求不成立」。§3.3：503 不落库、可重试。
                raise ServiceError(
                    503, "DEPENDENCY_UNAVAILABLE",
                    "artifact source is unavailable",
                    retryable=True,
                    details={"dependency": "S28"}) from exc
            raise
        except (ConnectionError, TimeoutError) as exc:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "artifact source is unreachable",
                retryable=True,
                details={"dependency": "S28"}) from exc

    def validate_artifacts(self, tenant_id: str, clips: list[dict]) -> None:
        """每个 clip 钉住的 artifact 必须存在且 hash 相符。

        同一个 artifact 在一个命令里只查一次（按 id+revision+hash 去重），
        但错误要带**所有** 引用它的 clip_id —— 只报第一个会让调用方多跑几轮。
        """
        by_ref: dict[tuple[str, int], list[dict]] = {}
        for clip in clips:
            ref = clip["src_artifact_ref"]
            by_ref.setdefault((ref["id"], ref["revision"]), []).append(clip)
        for (artifact_id, revision), members in sorted(by_ref.items()):
            # clip_id 要跟着 ref 走：`_fetch_artifact` 的 404 分支用它填
            # details，缺了它调用方只知道「有个素材没了」，不知道是哪一段。
            ref = dict(members[0]["src_artifact_ref"],
                       clip_id=members[0]["clip_id"])
            artifact = self._fetch_artifact(tenant_id, ref)
            if artifact is None:
                raise ServiceError(
                    422, "SOURCE_ARTIFACT_MISSING",
                    "source artifact does not exist",
                    details={"clip_id": members[0]["clip_id"],
                             "artifact_ref": _public_ref(ref)})
            state = artifact.get("state")
            if state not in (None, "AVAILABLE"):
                raise ServiceError(
                    422, "SOURCE_ARTIFACT_MISSING",
                    "source artifact is not available",
                    details={"clip_id": members[0]["clip_id"],
                             "artifact_ref": _public_ref(ref),
                             "state": state})
            actual = bare_hash(artifact.get("content_hash"))
            expected = members[0]["src_artifact_ref"]["content_hash"]
            if actual is not None and actual != expected:
                self._bump("editorial_hash_mismatch_total")
                raise ServiceError(
                    422, "SOURCE_ARTIFACT_MISMATCH",
                    "artifact content hash does not match the pinned reference",
                    details={"clip_id": members[0]["clip_id"],
                             "expected": expected,
                             "actual": actual,
                             "artifact_ref": _public_ref(ref)})
            available = _artifact_duration_ticks(artifact)
            for clip in members:
                clip["duration_ticks"] = available

    # ------------------------------------------------------------ 提交
    def commit_revision(self, tenant_id: str, timeline_id: str, body: dict,
                        command_id: str, request_hash: str) -> dict:
        """§4.1 的 12 步。

        **与方案字面顺序的一处差异**：结构校验与 S28 复查放在事务外，
        CAS 与 fence 校验放在事务内。方案第 3 步「锁行」在第 4 步校验之前，
        目的是让「读 current_revision → 比较 → 写」不被插队；这里把比较
        本身挪进事务，效果相同，而 HTTP 调用不再占着写锁。可观测行为
        （成功/409/422/503）与方案一致。
        """
        prepared = normalize_request(body)
        if prepared["allow_av_speed_drift"]:
            self._bump("editorial_link_group_drift_total")
        self.validate_artifacts(tenant_id, prepared["clips"])
        # 出点侧的 handle 需要 S28 的素材时长，只能在复查之后判（E41-DURATION）。
        check_out_handles(prepared["clips"])

        with Transaction(self.conn):
            timeline = self._timeline_row(tenant_id, timeline_id)
            if timeline is None:
                if prepared["expected_revision"] != 0:
                    # 没有这一行却声称有前序 revision：是调用方拿错了 id。
                    raise ServiceError(
                        409, "TIMELINE_REVISION_CONFLICT",
                        "timeline does not exist yet; expected_revision must be 0",
                        details={"current_revision": 0})
                timeline = self._create_timeline(tenant_id, timeline_id,
                                                 prepared)
                base_revision = 0
                revision = 1
            else:
                self._check_timeline_usable(timeline, prepared)
                if prepared["expected_revision"] != timeline["current_revision"]:
                    raise ServiceError(
                        409, "TIMELINE_REVISION_CONFLICT",
                        "expected_revision does not match current_revision",
                        details={"current_revision":
                                 timeline["current_revision"],
                                 "expected_revision":
                                 prepared["expected_revision"]})
                if prepared["expected_fence"] != timeline["execution_fence"]:
                    # §3.2：fence 归 S08，本服务只做比对。旧 fence 的写者
                    # 在灾备切主后必须停手。
                    self._bump("editorial_stale_fence_reject_total")
                    raise ServiceError(
                        409, "STALE_FENCE",
                        "execution_fence has moved; re-read from S08",
                        details={"current_execution_fence":
                                 timeline["execution_fence"],
                                 "expected_execution_fence":
                                 prepared["expected_fence"]})
                base_revision = timeline["current_revision"]
                revision = base_revision + 1

            content_hash = compute_content_hash(
                prepared["tracks"], prepared["clips"], prepared["fps_num"],
                prepared["fps_den"], TICK_RATE)
            collision = self.conn.execute(
                "SELECT revision FROM timeline_revision WHERE tenant_id = ? "
                "AND timeline_id = ? AND content_hash = ?",
                (tenant_id, timeline_id, content_hash)).fetchone()
            if collision is not None:
                # PTR-3：唯一索引会拦，但拦成 IntegrityError 就是 500。
                raise ServiceError(
                    409, "CONTENT_HASH_COLLISION",
                    "an identical revision already exists on this timeline",
                    details={"existing_revision": collision["revision"],
                             "content_hash": content_hash})

            edl = compute_edl(prepared["clips"], prepared["fps_num"],
                              prepared["fps_den"], TICK_RATE)
            if not edl:
                raise ServiceError(422, "SCHEMA_INVALID",
                                   "edl would be empty")
            edl_hash = compute_edl_hash(edl)
            duration = duration_ticks(prepared["clips"])
            previous_clips = self._clips(tenant_id, timeline_id, base_revision)
            previous_duration = self._revision_duration(
                tenant_id, timeline_id, base_revision)
            completeness = compute_completeness(
                prepared["tracks"], prepared["clips"], prepared["stems"],
                prepared["subtitle_locale"], [])
            diff = compute_diff(previous_clips, prepared["clips"],
                                base_revision, revision, previous_duration,
                                duration, prepared["change_cause"])
            if diff["diff_mode"] == "coarse":
                self._bump("editorial_diff_explosion_total")
            self._bump("editorial_diff_size_total", len(diff["affected_ranges"]))

            built_against = {
                "manifest_ref": prepared["manifest_ref"],
                "execution_fence": prepared["fence"],
                "node_epoch": prepared["node_epoch"],
            }
            payload = {
                "timeline_id": timeline_id,
                "revision": revision,
                "timeline_kind": prepared["timeline_kind"],
                "fps": {"num": prepared["fps_num"],
                        "den": prepared["fps_den"]},
                "tick_rate": TICK_RATE,
                "rounding_rule": ROUNDING_RULE,
                "tracks": prepared["tracks"],
                "clips": [
                    {k: v for k, v in clip.items()
                     if k not in ("duration_ticks", "track_kind")}
                    | {"track_kind": clip["track_kind"]}
                    for clip in prepared["clips"]
                ],
                "change_cause": prepared["change_cause"],
            }
            output_plan = compute_output_plan(
                prepared["tracks"], prepared["clips"], prepared["stems"],
                prepared["subtitle_locale"], prepared["master_spec_ref"])

            committed_at = self.clock()
            self.conn.execute(
                "UPDATE timeline_revision SET state = 'SUPERSEDED' "
                "WHERE tenant_id = ? AND timeline_id = ? AND state = 'COMMITTED'",
                (tenant_id, timeline_id))
            self.conn.execute(
                "INSERT INTO timeline_revision (tenant_id, timeline_id, revision,"
                " content_hash, base_revision, state, built_against, payload,"
                " edl_hash, duration_ticks, completeness, gaps, rounding_rule,"
                " change_cause, committed_by, committed_at) "
                "VALUES (?,?,?,?,?, 'COMMITTED', ?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, timeline_id, revision, content_hash, base_revision,
                 _dumps(built_against), _dumps(payload), edl_hash, duration,
                 completeness["completeness"], _dumps(completeness["gaps"]),
                 ROUNDING_RULE, _dumps(prepared["change_cause"]),
                 _dumps(prepared["actor"]), committed_at))
            self._insert_clips(tenant_id, timeline_id, revision,
                               prepared["clips"])
            self._insert_edl(tenant_id, timeline_id, revision, edl)
            self._insert_diff(tenant_id, timeline_id, base_revision, revision,
                              diff, committed_at)
            self._insert_output_plan(tenant_id, timeline_id, revision,
                                     output_plan)
            self._insert_mix_plan(tenant_id, timeline_id, revision,
                                  prepared["mix_plan"])
            self.conn.execute(
                "UPDATE timeline SET current_revision = ?, lock_version = ?,"
                " fps_num = ?, fps_den = ?, master_spec_ref = ?,"
                " updated_at = ? WHERE tenant_id = ? AND timeline_id = ?",
                (revision, timeline["lock_version"] + 1,
                 prepared["fps_num"], prepared["fps_den"],
                 _dumps(prepared["master_spec_ref"])
                 if prepared["master_spec_ref"] is not None else None,
                 committed_at, tenant_id, timeline_id))

            response = {
                "timeline_id": timeline_id,
                "revision": revision,
                "content_hash": content_hash,
                "duration_ticks": duration,
                "completeness": completeness["completeness"],
                "gaps": completeness["gaps"],
                "formal_master_eligible":
                    completeness["formal_master_eligible"],
                "diff": {
                    "from_revision": base_revision,
                    "to_revision": revision,
                    "affected_ranges": diff["affected_ranges"],
                    "affected_tracks": diff["affected_tracks"],
                    "diff_hash": diff["diff_hash"],
                    "diff_mode": diff["diff_mode"],
                },
                "edl_ref": {"timeline_id": timeline_id, "revision": revision,
                            "edl_hash": edl_hash},
                "base_revision": base_revision,
                "lock_version": timeline["lock_version"] + 1,
                # 201 不表示「已接受」（§6.1 末段、§11 C6）。这里的键名刻意
                # 叫 committed 而不是 accepted。
                "committed": True,
            }

            event = new_event(
                source=SOURCE,
                type=EVENT_REVISION_CREATED,
                subject=f"timeline/{timeline_id}",
                tenant_id=tenant_id,
                home_cell=self._home_cell(prepared),
                aggregate_id=timeline_id,
                sequence=revision,
                correlation_id=command_id,
                causation_id=command_id,
                data=self._event_data(timeline_id, prepared, revision,
                                      base_revision, content_hash, duration,
                                      completeness, diff),
                data_ref={"service_id": SOURCE, "id": timeline_id,
                          "revision": revision, "content_hash": content_hash},
                schema_version="2.0")
            event_id = self.outbox.append(event)
            response["outbox_event_id"] = event_id
            response["event_type"] = EVENT_REVISION_CREATED

            self.conn.execute(
                "INSERT INTO command_receipt (tenant_id, command_id,"
                " request_hash, timeline_id, revision, response_body,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                (tenant_id, command_id, request_hash, timeline_id, revision,
                 _jcs(response), committed_at))
            self._bump("editorial_revision_commit_total")
            return response

    def _home_cell(self, prepared: dict) -> str:
        value = prepared["context"].get("home_cell")
        return value if isinstance(value, str) and value else "cell_a1"

    def _event_data(self, timeline_id: str, prepared: dict, revision: int,
                    base_revision: int, content_hash: str, duration: int,
                    completeness: dict, diff: dict) -> dict:
        """事件的 `data`，键集合与契约逐字相同（`additionalProperties:false`）。

        刻意**不**放：`gaps`（PTR-5）、`tick_rate`/`fps`（PTR-2）、
        `change_causes`、片段 payload、媒体 URL、`shot_revisions`
        （x-needs-owner，E41-STALE）。
        """
        return {
            "schema_version": "2.0",
            "timeline_id": timeline_id,
            "revision": revision,
            "content_hash": content_hash,
            "duration_ticks": duration,
            "completeness": completeness["completeness"],
            "formal_master_eligible":
                completeness["formal_master_eligible"],
            "built_against": {
                "manifest_ref": prepared["manifest_ref"],
                "execution_fence": prepared["fence"],
                "node_epoch": prepared["node_epoch"],
            },
            "diff_ref": {
                "timeline_id": timeline_id,
                "from_revision": base_revision,
                "to_revision": revision,
                "diff_hash": diff["diff_hash"],
            },
            "affected_ranges": diff["affected_ranges"],
            "affected_tracks": diff["affected_tracks"],
        }

    def _create_timeline(self, tenant_id: str, timeline_id: str,
                         prepared: dict) -> dict:
        now = self.clock()
        try:
            self.conn.execute(
                "INSERT INTO timeline (tenant_id, timeline_id, project_id,"
                " series_id, episode_id, branch_id, timeline_kind,"
                " current_revision, lock_version, execution_fence,"
                " master_spec_ref, fps_num, fps_den, tick_rate, state,"
                " created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,1,0,?,?,?,?,?, 'ACTIVE', ?,?)",
                (tenant_id, timeline_id, prepared["project_id"],
                 prepared["series_id"], prepared["episode_id"],
                 prepared["branch_id"], prepared["timeline_kind"],
                 prepared["fence"],
                 _dumps(prepared["master_spec_ref"])
                 if prepared["master_spec_ref"] is not None else None,
                 prepared["fps_num"], prepared["fps_den"], TICK_RATE, now,
                 now))
        except sqlite3.IntegrityError as exc:
            self._bump("editorial_identity_conflict_total")
            raise ServiceError(
                409, "TIMELINE_IDENTITY_CONFLICT",
                "another timeline already owns this (series, episode, branch,"
                " kind)",
                details={"timeline_id": timeline_id,
                         "timeline_kind": prepared["timeline_kind"]}) from exc
        return self._require_timeline(tenant_id, timeline_id)

    def _check_timeline_usable(self, timeline: dict, prepared: dict) -> None:
        if timeline["state"] != "ACTIVE":
            raise ServiceError(
                409, "TIMELINE_NOT_ACTIVE",
                "timeline is not accepting new revisions",
                details={"timeline_id": timeline["timeline_id"],
                         "state": timeline["state"]})
        identity = (timeline["project_id"], timeline["series_id"],
                    timeline["episode_id"], timeline["branch_id"],
                    timeline["timeline_kind"])
        wanted = (prepared["project_id"], prepared["series_id"],
                  prepared["episode_id"], prepared["branch_id"],
                  prepared["timeline_kind"])
        if identity != wanted:
            raise ServiceError(
                409, "TIMELINE_IDENTITY_CONFLICT",
                "request identity does not match the stored timeline",
                details={"timeline_id": timeline["timeline_id"]})
        if (timeline["fps_num"], timeline["fps_den"]) != (prepared["fps_num"],
                                                          prepared["fps_den"]):
            # E41-FPS-CHANGE：换帧率会改变整条 EDL 的帧号，不是一次普通剪辑。
            raise ServiceError(
                409, "TIMELINE_FPS_CONFLICT",
                "fps cannot change on an existing timeline",
                details={"current": {"num": timeline["fps_num"],
                                     "den": timeline["fps_den"]},
                         "requested": {"num": prepared["fps_num"],
                                       "den": prepared["fps_den"]}})
        if timeline["tick_rate"] != TICK_RATE:
            raise ServiceError(
                409, "TIMELINE_TICK_RATE_CONFLICT",
                "timeline was built at a different tick_rate",
                details={"current_tick_rate": timeline["tick_rate"]})

    # -------------------------------------------------------- 落库细节
    def _insert_clips(self, tenant_id: str, timeline_id: str, revision: int,
                      clips: list[dict]) -> None:
        for clip in clips:
            self.conn.execute(
                "INSERT INTO clip (tenant_id, timeline_id, revision, clip_id,"
                " track_id, track_kind, link_group_id, ordinal,"
                " src_artifact_ref, src_in_ticks, src_out_ticks,"
                " rec_in_ticks, rec_out_ticks, speed_num, speed_den,"
                " transition_in, transition_out, unlinked_at_revision,"
                " drift_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, timeline_id, revision, clip["clip_id"],
                 clip["track_id"], clip["track_kind"], clip["link_group_id"],
                 clip["ordinal"], _dumps(clip["src_artifact_ref"]),
                 clip["src_in_ticks"], clip["src_out_ticks"],
                 clip["rec_in_ticks"], clip["rec_out_ticks"],
                 clip["speed_num"], clip["speed_den"],
                 _dumps(clip["transition_in"]),
                 _dumps(clip["transition_out"]),
                 clip["unlinked_at_revision"], clip["drift_reason"]))

    def _insert_edl(self, tenant_id: str, timeline_id: str, revision: int,
                    edl: list[dict]) -> None:
        for entry in edl:
            self.conn.execute(
                "INSERT INTO edl_entry (tenant_id, timeline_id, revision,"
                " entry_no, clip_id, track_id, record_in_frame,"
                " record_out_frame, src_in_ticks, src_out_ticks, speed_num,"
                " speed_den, transition_in, transition_out) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, timeline_id, revision, entry["entry_no"],
                 entry["clip_id"], entry["track_id"],
                 entry["record_in_frame"], entry["record_out_frame"],
                 entry["src_in_ticks"], entry["src_out_ticks"],
                 entry["speed_num"], entry["speed_den"],
                 _dumps(entry["transition_in"]),
                 _dumps(entry["transition_out"])))

    def _insert_diff(self, tenant_id: str, timeline_id: str,
                     from_revision: int, to_revision: int, diff: dict,
                     created_at: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO timeline_diff (tenant_id, timeline_id,"
            " from_revision, to_revision, diff_hash, affected_ranges,"
            " affected_tracks, payload, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, timeline_id, from_revision, to_revision,
             diff["diff_hash"], _dumps(diff["affected_ranges"]),
             _dumps(diff["affected_tracks"]), _dumps(diff), created_at))

    def _insert_output_plan(self, tenant_id: str, timeline_id: str,
                            revision: int, plan: dict) -> None:
        self.conn.execute(
            "INSERT INTO output_plan (tenant_id, timeline_id, revision,"
            " plan_hash, segments, stems, subtitle_track_ref, master_spec_ref)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (tenant_id, timeline_id, revision, plan["plan_hash"],
             _dumps(plan["segments"]), _dumps(plan["stems"]),
             _dumps(plan["subtitle_track_ref"]),
             _dumps(plan["master_spec_ref"])
             if plan["master_spec_ref"] is not None else None))

    def _insert_mix_plan(self, tenant_id: str, timeline_id: str, revision: int,
                         plan: dict | None) -> None:
        if plan is None:
            return
        self.conn.execute(
            "INSERT INTO mix_plan (tenant_id, timeline_id, revision,"
            " plan_hash, target_lufs, true_peak_ceiling, ducking_policy_ref,"
            " stem_gains, notes) VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, timeline_id, revision, plan["plan_hash"],
             plan["target_lufs"], plan["true_peak_ceiling"],
             _dumps(plan["ducking_policy_ref"]),
             _dumps(plan["stem_gains"]), plan["notes"]))

    # ------------------------------------------------------------ 读
    def _clips(self, tenant_id: str, timeline_id: str,
               revision: int) -> list[dict]:
        if revision < 1:
            return []
        rows = self.conn.execute(
            "SELECT * FROM clip WHERE tenant_id = ? AND timeline_id = ?"
            " AND revision = ? ORDER BY track_id, ordinal, clip_id",
            (tenant_id, timeline_id, revision)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["src_artifact_ref"] = _loads(item["src_artifact_ref"], {})
            item["transition_in"] = _loads(item["transition_in"])
            item["transition_out"] = _loads(item["transition_out"])
            out.append(item)
        return out

    def _revision_duration(self, tenant_id: str, timeline_id: str,
                           revision: int) -> int:
        if revision < 1:
            return 0
        row = self.conn.execute(
            "SELECT duration_ticks FROM timeline_revision WHERE tenant_id = ?"
            " AND timeline_id = ? AND revision = ?",
            (tenant_id, timeline_id, revision)).fetchone()
        return int(row["duration_ticks"]) if row is not None else 0

    def _revision_row(self, tenant_id: str, timeline_id: str,
                      revision: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM timeline_revision WHERE tenant_id = ?"
            " AND timeline_id = ? AND revision = ?",
            (tenant_id, timeline_id, revision)).fetchone()
        if row is None:
            # 跨租户同样落这里：404，不泄漏。
            raise ServiceError(404, "REVISION_NOT_FOUND",
                               "timeline revision not found",
                               details={"timeline_id": timeline_id,
                                        "revision": revision})
        return dict(row)

    def _edl_rows(self, tenant_id: str, timeline_id: str,
                  revision: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM edl_entry WHERE tenant_id = ? AND timeline_id = ?"
            " AND revision = ? ORDER BY track_id, entry_no",
            (tenant_id, timeline_id, revision)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["transition_in"] = _loads(item["transition_in"])
            item["transition_out"] = _loads(item["transition_out"])
            out.append(item)
        return out

    def get_revision(self, tenant_id: str, timeline_id: str,
                     revision: int) -> dict:
        timeline = self._require_timeline(tenant_id, timeline_id)
        row = self._revision_row(tenant_id, timeline_id, revision)
        built_against = _loads(row["built_against"], {})
        manifest_ref = built_against.get("manifest_ref") or {}
        return {
            "timeline_id": timeline_id,
            "revision": row["revision"],
            "state": row["state"],
            "content_hash": row["content_hash"],
            "base_revision": row["base_revision"],
            "duration_ticks": row["duration_ticks"],
            "completeness": row["completeness"],
            "gaps": _loads(row["gaps"], []),
            "formal_master_eligible": (
                row["completeness"] == "complete" and row["state"] == "COMMITTED"),
            "built_against": built_against,
            "rounding_rule": row["rounding_rule"],
            "edl_hash": row["edl_hash"],
            "edl": self._edl_rows(tenant_id, timeline_id, revision),
            "change_cause": _loads(row["change_cause"]),
            "committed_by": _loads(row["committed_by"], {}),
            "committed_at": row["committed_at"],
            "payload": _loads(row["payload"], {}),
            "clips": self._clips(tenant_id, timeline_id, revision),
            # §6.2：读投影时带 source_revision 与 projection_watermark。
            "source_revision": manifest_ref.get("revision"),
            "projection_watermark": timeline["execution_fence"],
            "current_revision": timeline["current_revision"],
            "lock_version": timeline["lock_version"],
            "timeline_state": timeline["state"],
        }

    def get_diff(self, tenant_id: str, timeline_id: str, from_revision: int,
                 to_revision: int) -> dict:
        self._require_timeline(tenant_id, timeline_id)
        if to_revision <= from_revision:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "to must be greater than from",
                               details={"from": from_revision,
                                        "to": to_revision})
        row = self.conn.execute(
            "SELECT payload FROM timeline_diff WHERE tenant_id = ?"
            " AND timeline_id = ? AND from_revision = ? AND to_revision = ?",
            (tenant_id, timeline_id, from_revision, to_revision)).fetchone()
        if row is not None:
            return _loads(row["payload"], {})
        # 非相邻的两版：纯函数现算。读路径不写库（§2.1：EDL/diff 不是事实）。
        old = self._revision_row(tenant_id, timeline_id, from_revision) \
            if from_revision >= 1 else None
        new = self._revision_row(tenant_id, timeline_id, to_revision)
        if new is None:
            raise ServiceError(404, "REVISION_NOT_FOUND",
                               "timeline revision not found",
                               details={"timeline_id": timeline_id,
                                        "revision": to_revision})
        old_clips = self._clips(tenant_id, timeline_id, from_revision)
        new_clips = self._clips(tenant_id, timeline_id, to_revision)
        old_duration = int(old["duration_ticks"]) if old else 0
        return compute_diff(old_clips, new_clips, from_revision, to_revision,
                            old_duration, int(new["duration_ticks"]),
                            _loads(new["change_cause"]))

    def get_edl(self, tenant_id: str, timeline_id: str, revision: int,
                format: str = "json") -> dict:
        timeline = self._require_timeline(tenant_id, timeline_id)
        row = self._revision_row(tenant_id, timeline_id, revision)
        if format == "json":
            return {
                "timeline_id": timeline_id,
                "revision": revision,
                "edl_hash": row["edl_hash"],
                "fps": {"num": timeline["fps_num"], "den": timeline["fps_den"]},
                "tick_rate": timeline["tick_rate"],
                "rounding_rule": row["rounding_rule"],
                "entries": self._edl_rows(tenant_id, timeline_id, revision),
            }
        if format == "otio":
            return self._otio(timeline, revision, row["edl_hash"])
        raise ServiceError(422, "SCHEMA_INVALID",
                           "format must be 'json' or 'otio'",
                           details={"field": "format"})

    def _otio(self, timeline: dict, revision: int, edl_hash: str) -> dict:
        """OTIO 是**视图**，不是新事实（§12 第 4 条）。

        秒数只能是浮点，因此这里确实出现浮点——但它不参与 `content_hash`，
        也不落库。OTIO 的 hash 不稳定，所以导出结果不能当版本标识。
        """
        rate = timeline["fps_num"] / timeline["fps_den"]
        entries = self._edl_rows(timeline["tenant_id"], timeline["timeline_id"],
                                 revision)
        by_track: dict[str, list[dict]] = {}
        for entry in entries:
            by_track.setdefault(entry["track_id"], []).append(entry)
        children = []
        for track_id in sorted(by_track):
            items = []
            for entry in by_track[track_id]:
                start = entry["record_in_frame"] / rate
                stop = entry["record_out_frame"] / rate
                items.append({
                    "OTIO_SCHEMA": "Clip.1",
                    "name": entry["clip_id"],
                    "source_range": {
                        "OTIO_SCHEMA": "TimeRange.1",
                        "start_time": {"OTIO_SCHEMA": "RationalTime.1",
                                       "value": entry["record_in_frame"],
                                       "rate": rate},
                        "duration": {"OTIO_SCHEMA": "RationalTime.1",
                                     "value": entry["record_out_frame"]
                                     - entry["record_in_frame"],
                                     "rate": rate},
                    },
                    "metadata": {"s41": {"record_in_seconds": start,
                                         "record_out_seconds": stop,
                                         "src_in_ticks": entry["src_in_ticks"],
                                         "src_out_ticks": entry["src_out_ticks"],
                                         "speed_num": entry["speed_num"],
                                         "speed_den": entry["speed_den"]}},
                })
            children.append({"OTIO_SCHEMA": "Track.1", "name": track_id,
                             "kind": "Video" if track_id.startswith("v")
                             else "Audio",
                             "children": items})
        return {
            "OTIO_SCHEMA": "Timeline.1",
            "name": timeline["timeline_id"],
            "metadata": {"s41": {"revision": revision,
                                 "edl_hash": edl_hash,
                                 "note": "view only; not content-addressed"}},
            "tracks": {"OTIO_SCHEMA": "Stack.1", "children": children},
        }

    def list_timelines(self, tenant_id: str, episode_id: str | None = None,
                       branch_id: str | None = None,
                       timeline_kind: str | None = None,
                       limit: int = 50) -> dict:
        if limit < 1 or limit > 500:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "limit must be between 1 and 500",
                               details={"field": "limit"})
        # tenant 过滤写在字面量里而不是拼进 clauses：这样任何按 SQL 字面量
        # 做的审计（例如「每条 FROM 都有 tenant_id」）都能看见它，不会被
        # 拼接顺序藏起来。
        filters = ""
        params: list = [tenant_id]
        if episode_id:
            filters += " AND episode_id = ?"
            params.append(episode_id)
        if branch_id:
            filters += " AND branch_id = ?"
            params.append(branch_id)
        if timeline_kind:
            filters += " AND timeline_kind = ?"
            params.append(timeline_kind)
        rows = self.conn.execute(
            "SELECT * FROM timeline WHERE tenant_id = ?" + filters
            + " ORDER BY updated_at DESC, timeline_id LIMIT ?",
            (*params, limit)).fetchall()
        return {"timelines": [dict(row) for row in rows]}

    def render_eligibility(self, tenant_id: str, timeline_id: str,
                           revision: int) -> dict:
        """S42 渲染前的回查（§4.4、PTR-4）。

        `gaps` 是提交时的快照，**不能**当放行依据；这里只回答「这一版在
        本服务里是不是还能用于正式 master」，素材与权利的实时复查归 S42。
        """
        timeline = self._require_timeline(tenant_id, timeline_id)
        row = self._revision_row(tenant_id, timeline_id, revision)
        if row["state"] == "VOIDED":
            raise ServiceError(422, "REVISION_VOIDED",
                               "voided revisions can never be used for a "
                               "formal master",
                               details={"timeline_id": timeline_id,
                                        "revision": revision})
        if row["state"] == "SUPERSEDED":
            raise ServiceError(422, "REVISION_SUPERSEDED",
                               "superseded revisions cannot be used for a "
                               "formal master",
                               details={"timeline_id": timeline_id,
                                        "revision": revision,
                                        "current_revision":
                                        timeline["current_revision"]})
        if row["completeness"] != "complete":
            raise ServiceError(422, "INCOMPLETE_TIMELINE",
                               "revision has completeness gaps",
                               details={"gaps": _loads(row["gaps"], [])})
        return {"eligible": True, "timeline_id": timeline_id,
                "revision": revision, "state": row["state"],
                "formal_master_eligible": True}

    def retire_revision(self, tenant_id: str, timeline_id: str, revision: int,
                        target: str) -> dict:
        """COMMITTED → SUPERSEDED / VOIDED。终态不回退（§3.1）。"""
        if target not in ("SUPERSEDED", "VOIDED"):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "target must be SUPERSEDED or VOIDED",
                               details={"field": "target"})
        with Transaction(self.conn):
            self._require_timeline(tenant_id, timeline_id)
            row = self._revision_row(tenant_id, timeline_id, revision)
            if row["state"] == target:
                return {"timeline_id": timeline_id, "revision": revision,
                        "state": target, "changed": False}
            if row["state"] != "COMMITTED":
                raise ServiceError(
                    409, "REVISION_STATE_TERMINAL",
                    "terminal revision state cannot be changed",
                    details={"timeline_id": timeline_id,
                             "revision": revision,
                             "state": row["state"],
                             "target": target})
            self.conn.execute(
                "UPDATE timeline_revision SET state = ? WHERE tenant_id = ?"
                " AND timeline_id = ? AND revision = ? AND state = 'COMMITTED'",
                (target, tenant_id, timeline_id, revision))
            return {"timeline_id": timeline_id, "revision": revision,
                    "state": target, "changed": True}

    # ------------------------------------------------------------ 事件消费
    def consume_manifest_event(self, tenant_id: str, envelope: dict) -> dict:
        """`snapshot.manifest.committed`：只推进 fence 投影。

        三件**不做**的事：

        - 不写 `node_epoch`（那是 S08/节点自己的事实）。事件里的
          `changed_node_epochs` 只用来看「这次有没有镜头变化」，不落库。
        - 不自动生成/提交新 revision（§4.2：剪辑是创作决定）。
        - 不回退 fence（C-S41-03 的乱序/重复断言）。
        """
        event_id = envelope.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ServiceError(400, "INVALID_BODY",
                               "event.id is required")
        event_type = envelope.get("type")
        if event_type != "snapshot.manifest.committed":
            return {"ignored": True, "reason": "unhandled_event_type",
                    "type": event_type}
        data = envelope.get("data") or {}
        if not isinstance(data, dict):
            raise ServiceError(400, "INVALID_BODY",
                               "event.data must be an object")
        manifest_ref = data.get("manifest_ref") or {}
        manifest_id = manifest_ref.get("id") if isinstance(manifest_ref,
                                                          dict) else None
        if not isinstance(manifest_id, str) or not manifest_id:
            raise ServiceError(400, "INVALID_BODY",
                               "event.data.manifest_ref.id is required")
        fence = data.get("execution_fence")
        if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise ServiceError(400, "INVALID_BODY",
                               "event.data.execution_fence must be an "
                               "integer >= 1")
        changed_epochs = data.get("changed_node_epochs") or []

        with Transaction(self.conn):
            if not self.inbox.consume(CONSUMER, event_id):
                # E13-S41-03：重复投递不重复计数，也不重复推进。
                return {"duplicate": True, "event_id": event_id}
            rows = self.conn.execute(
                "SELECT timeline_id, execution_fence FROM timeline"
                " WHERE tenant_id = ? AND timeline_id IN ("
                "   SELECT timeline_id FROM timeline_revision"
                "   WHERE tenant_id = ?"
                "   AND json_extract(built_against, '$.manifest_ref.id') = ?)",
                (tenant_id, tenant_id, manifest_id)).fetchall()
            advanced = []
            for row in rows:
                if fence <= row["execution_fence"]:
                    continue
                self.conn.execute(
                    "UPDATE timeline SET execution_fence = ?, updated_at = ?"
                    " WHERE tenant_id = ? AND timeline_id = ?"
                    " AND execution_fence < ?",
                    (fence, self.clock(), tenant_id, row["timeline_id"], fence))
                advanced.append(row["timeline_id"])
        return {
            "duplicate": False,
            "event_id": event_id,
            "manifest_revision": manifest_ref.get("revision"),
            "advanced_timelines": advanced,
            # 只回显，不落库：epoch 属于节点，本服务不是它的权威。
            "changed_node_epochs_seen": len(changed_epochs),
            "node_epoch_written": False,
        }


def _artifact_duration_ticks(artifact: dict) -> int | None:
    """S28 的 `duration_ms` → tick。没有时长就是 `None`，不假设 0。"""
    value = artifact.get("duration_ticks")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    millis = artifact.get("duration_ms")
    if isinstance(millis, (int, float)) and not isinstance(millis, bool):
        return int(round(float(millis) * TICK_RATE / 1000))
    return None


def _public_ref(ref: dict) -> dict:
    """错误 details 里只放定位信息，不放 hash 之外的内容。"""
    return {"service_id": ref.get("service_id"), "id": ref.get("id"),
            "revision": ref.get("revision")}
