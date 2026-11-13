"""S06 内核。

一句话定位：**S06 是内容服务，不是执行服务**。

由此推出三条贯穿全文件的边界：

1. **不认识 `execution_fence`，也不认识 `node_epoch`。** 本文件里没有这两个词
   的任何读取或写入。一次"旧 epoch 的结果晚到"在 S06 只表现为一条带着旧
   `parent_revision` 的请求，用乐观并发回 409 就够了（§3.8）。
2. **不做第二权威。** 对白以 S04 为权威（这里只存 `text_copy` 供校验与审计）、
   分场以 S05 为权威（只读、不缓存）、尝试次数与费用归 S26/S29/S23（这里一行
   都不记）、提示词归 S20（这里一个词都不写）。
3. **可行性只报选项，不替人做决定。** `DIALOGUE_EXCEEDS_DURATION` 附带
   `extend` / `split` / `cut_lines` 三条，S06 从不执行任何一条，也从不悄悄截短
   台词或节拍（§3.5.2）。

`shot_lineage_id` 的铸造只在 `StoryboardService._mint_lineage` 一处（§3.2.2），
三种情形之外没有第四种。它是本服务唯一一条"事后无法补救"的约束，所以数据库层
还有触发器兜底（`schema.SCHEMA_SQL` 里的 `shot_lineage_immutable`）。

一次"整场分镜"的写入形状（本文件反复用到）：

    storyboard_revision(rev=N)              ← 一版快照
      └─ shot_revision(shot=A, revision=k)  ← 每个镜头一行，内容不可变
           ├─ shot_beat
           └─ shot_dialogue_ref

`shot_revision.revision` 是"这一镜的内容第几次落库"（跨整场版本单调递增），
不是"这一镜改过几次"——复制未改动的镜头也会得到新行、新 revision。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event
from idempotency_sdk import SqliteIdempotencyStore

from .schema import (
    AUTHORITATIVE_SOURCE,
    CAMERA_ANGLES,
    CAMERA_MOVEMENTS,
    CHARS_PER_SECOND,
    CONTRACT_GAPS,
    CONTINUITY_MODES,
    DEFAULT_CHARS_PER_SECOND,
    DEFAULT_CONTINUITY,
    DEFAULT_EMOTION_FACTOR,
    EMOTION_FACTOR,
    ID_PREFIX,
    INTER_LINE_PAUSE_SECONDS,
    LOCAL_EVENT_LINEAGE_DISCARDED,
    LOCAL_EVENT_LINEAGE_MINTED,
    LOCAL_EVENT_REORDERED,
    LOCAL_EVENT_RETIRED,
    LOCAL_EVENT_REVISION_CREATED,
    LOCAL_EVENT_SEALED,
    LOCAL_EVENT_SPLIT,
    LOCAL_EVENT_SUPERSEDED,
    MANUAL_CONTINUITY,
    MAX_SHOTS_PER_SCENE_DEFAULT,
    MAX_SHOTS_PER_SCENE_HARD,
    MIN_SHOT_SECONDS,
    ORDER_OFFSET,
    SCHEMA_VERSION,
    SHOT_SIZES,
    SHOT_STATES,
    SHOT_TYPES,
    SOURCE,
    TIMING_POLICIES,
    SCHEMA_SQL,
    TOPIC,
)


# ---------------------------------------------------------------- 基础工具

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _prefixed(digest: str | None) -> str | None:
    """库里存裸 hex，响应与方案 §4.1 一样渲染 `sha256:` 前缀。"""
    if digest is None:
        return None
    return digest if digest.startswith("sha256:") else "sha256:" + digest


def _bare_digest(value, field: str) -> str:
    """接受裸 hex 或 `sha256:` 前缀，返回裸 hex；其它形态 400。

    方案示例带前缀，DDL 与事件 schema 要裸 hex。把规范化放在入口一处，
    后续所有比较才是同一种尺子。
    """
    if not isinstance(value, str):
        raise ServiceError(400, "INVALID_SCHEMA", f"{field} must be a string")
    text = value[len("sha256:"):] if value.startswith("sha256:") else value
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ServiceError(
            400, "INVALID_SCHEMA",
            f"{field} must be 64 hex characters (optionally sha256:-prefixed)",
            details={"field": field})
    return text


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

    def __init__(self, conn: sqlite3.Connection, service_code: str = "STORYBOARD"):
        self.conn = conn
        self.nested = False
        self.service_code = service_code

    def __enter__(self):
        if self.conn.in_transaction:
            self.nested = True
            return self
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # 主库不可写（只读副本、磁盘满、锁超时）。写不进去不等于请求有错。
            raise ServiceError(
                503, self.service_code + "_UNAVAILABLE",
                f"storyboard database is not writable: {exc}",
                retryable=True) from exc
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.nested:
            return False
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ---------------------------------------------------------------- 校验小件

def _require(condition, message: str, *, code: str = "INVALID_SCHEMA",
             status: int = 400, details: dict | None = None):
    if not condition:
        raise ServiceError(status, code, message, details=details)


def _obj(value, field: str) -> dict:
    _require(isinstance(value, dict) and value,
             f"{field} must be a non-empty object",
             details={"field": field})
    return value


def _list(value, field: str, *, allow_empty: bool = True) -> list:
    _require(isinstance(value, list) and (allow_empty or value),
             f"{field} must be a {'non-empty ' if not allow_empty else ''}array",
             details={"field": field})
    return value


def _text(value, field: str) -> str:
    _require(isinstance(value, str) and value.strip(),
             f"{field} must be a non-empty string",
             details={"field": field})
    return value


def _int(value, field: str, minimum: int | None = None) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool),
             f"{field} must be an integer", details={"field": field})
    if minimum is not None:
        _require(value >= minimum, f"{field} must be >= {minimum}",
                 details={"field": field, "minimum": minimum})
    return value


def _num(value, field: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool),
             f"{field} must be a number", details={"field": field})
    _require(math.isfinite(float(value)),
             f"{field} must be finite", details={"field": field})
    return float(value)


def _enum(value, field: str, allowed) -> str:
    _require(value in allowed,
             f"{field} must be one of {list(allowed)}",
             details={"field": field, "allowed": list(allowed),
                      "received": value})
    return value


# ---------------------------------------------------------------- 内容寻址

def _strip_punctuation(text: str) -> str:
    """朗读时长只看"要念出来的字"。

    标点、空白、换行不算音节。用 Unicode 类别而不是写死一张标点表：中文的
    顿号、引号、省略号，英文的连字符，都在 Punctuation 类里。
    """
    return "".join(ch for ch in text
                   if not unicodedata.category(ch).startswith("P")
                   and not ch.isspace())


def _dialogue_seconds(entries, locale: str) -> float:
    """§3.5.1。`entries` 是 `[{text, emotion}]`，逐条累加。

    速率与情绪系数是服务配置（`schema.CHARS_PER_SECOND` / `EMOTION_FACTOR`），
    没有实测依据（§12 U2），所以它们集中在 schema 里，等配音侧校准。
    """
    rate = CHARS_PER_SECOND.get(locale, DEFAULT_CHARS_PER_SECOND)
    total = 0.0
    for entry in entries:
        body = _strip_punctuation(entry["text"])
        factor = EMOTION_FACTOR.get(entry.get("emotion"),
                                    DEFAULT_EMOTION_FACTOR)
        total += len(body) / rate * factor + INTER_LINE_PAUSE_SECONDS
    return total


def _beat_seconds(beats) -> float:
    """节拍的总跨度：`max(end) - min(start)`，不是各段相加。

    相加会把"两段并行"重复计成两倍。§3.5.2 的 `required` 用的是"最长 beat
    与对白不重叠的部分"，这里先给出总跨度，重叠判定交给 `_feasibility`。
    """
    if not beats:
        return 0.0
    start = min(b["time_range"]["start_seconds"] for b in beats)
    end = max(b["time_range"]["end_seconds"] for b in beats)
    return max(0.0, end - start)


def _shot_content_projection(spec: dict) -> dict:
    """`content_hash` 里的 ShotSpec 投影。

    S06-G07：显式排除 `display_locator`。它在 §1.1 就是显示值，把它算进内容
    hash 会让 T06-47（重排后 `content_hash` 不变）与 §4.1 的算式直接矛盾——
    重排要改 `display_locator.shot`。其余字段原样参与。
    """
    return {key: value for key, value in spec.items()
            if key != "display_locator"}


def _script_line_hash_digest(refs) -> str:
    """§4.1：`sha256(concat(sorted(script_line_refs.map(text_hash))))`。

    排序后拼接，所以"同一组行、换顺序"不改变这一项。
    """
    return _sha256_hex("".join(sorted(r["text_hash"] for r in refs)))


def shot_content_hash(spec: dict, script_line_refs) -> str:
    """§4.1。`sha256(JCS(ShotSpec 去掉 s06_meta) + "\\n" + 对白 hash 拼接)`。

    把对白 hash 拼进去，是为了让"文本被改过但 ShotSpec 看起来没变"改变
    `content_hash`（§1.2 的防漂移在内容层落地）。
    """
    return _sha256_hex(_jcs(_shot_content_projection(spec)) + "\n"
                       + _script_line_hash_digest(script_line_refs))


def shot_fingerprint(spec: dict, script_line_refs) -> str:
    """跨版本去重指纹（S06-G07）。**不是** `content_hash`。

    去掉 `shot_id` / `revision` / `display_locator` 之后求 hash：同一份内容
    在任何位置上都是同一份内容。`storyboard_revision.content_fingerprint` 用它
    回答 T06-04"同内容不新建一版"。
    """
    body = {key: value for key, value in _shot_content_projection(spec).items()
            if key not in ("shot_id", "revision")}
    return _sha256_hex(_jcs(body) + "\n" + _script_line_hash_digest(
        script_line_refs))


# ---------------------------------------------------------------- 字段路径

_FIELD_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[0-9]+\])?$")
_WRITE_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[0-9]+\])*$|^[0-9]+$")


def _field_path_matches(field: str, guard: str) -> bool:
    """`camera.angle` 命中 `camera`；`beats[2].action` 命中 `beats`（§3.5.3）。

    按段前缀匹配，不做字符串 `startswith`：`camera_notes` 不能命中 `camera`。
    两侧都允许段内带 `[n]`，因为 `must_preserve` 可能写
    `asset_bindings.characters[0].costume`（T06-19）。
    """
    left = field.split(".")
    right = guard.split(".")
    if len(right) > len(left):
        return False
    return all(a == b or a.split("[")[0] == b.split("[")[0]
               for a, b in zip(left, right))


def _flatten_paths(value, prefix: str = "") -> list[str]:
    """把一份字段级 patch 摊成叶子路径，供 `changed_fields` 使用。

    `changed_fields` 是 E03 的直接产物：S09 拿它在字段依赖图上找受影响子图。
    语义是"patch 里出现的路径"，**不是**"值真的变了的路径"——两者在
    "把 4.0 改成 4.0"时不同，这里取前者，因为调用方表达的意图才是下游要重算的
    范围。
    """
    if isinstance(value, dict) and value:
        out: list[str] = []
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else key
            out.extend(_flatten_paths(item, child))
        return out
    if isinstance(value, list) and value:
        out = []
        for index, item in enumerate(value):
            out.extend(_flatten_paths(item, f"{prefix}[{index}]"))
        return out
    return [prefix] if prefix else []


def _walk_path(root, path: str):
    """按点分路径读值，返回 `(found, value)`。`beats[2].action` 也支持。"""
    current = root
    for segment in path.split("."):
        name, _, rest = segment.partition("[")
        if not isinstance(current, dict) or name not in current:
            return False, None
        current = current[name]
        while rest:
            index_text, _, rest = rest.partition("]")
            rest = rest.lstrip("[")
            try:
                index = int(index_text)
            except ValueError:
                return False, None
            if not isinstance(current, list) or not 0 <= index < len(current):
                return False, None
            current = current[index]
    return True, current


def _deep_merge(base: dict, patch: dict) -> dict:
    """字段级 patch 叠加到既有 ShotSpec 上。

    `patch` 是**字段级**的，不是整份替换（§2.5）：没出现在 patch 里的字段原样
    保留。数组按"整段替换"处理——按索引逐元素合并会让"删掉一个 beat"无法表达。
    """
    out = json.loads(json.dumps(base))
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = json.loads(json.dumps(value))
    return out


# ---------------------------------------------------------------- 事件

def _local_event(conn, tenant_id: str, event_type: str, aggregate_id: str,
                 payload: dict, command_id: str = "") -> None:
    """未登记进 `event-topics.v2.json` 的事件写本地表，不进 outbox（§8.3）。

    只写本地不是偷懒：把一个没有 topic、没有 payload schema 的 `event_type`
    塞进 outbox，会让中继去投递一个下游无法校验的事件（§11 C3）。
    """
    body = dict(payload)
    if command_id:
        body.setdefault("command_id", command_id)
    conn.execute(
        "INSERT INTO local_event (tenant_id, local_event_id, event_type,"
        " aggregate_id, payload, created_at) VALUES (?,?,?,?,?,?)",
        (tenant_id, _new_id("local_event"), event_type, aggregate_id,
         _jcs(body), _now()))


def _outbox_event(*, tenant_id: str, event_type: str, subject: str,
                  aggregate_id: str, sequence: int, command_id: str,
                  data: dict, home_cell: str, data_ref=None):
    return new_event(
        source=SOURCE, type=event_type, subject=subject,
        tenant_id=tenant_id, home_cell=home_cell,
        aggregate_id=aggregate_id, sequence=int(sequence),
        correlation_id=command_id, causation_id=command_id,
        data=data, data_ref=data_ref)

# ---------------------------------------------------------------- ShotSpec 校验

SHOT_SPEC_KEYS = {
    "schema_version", "shot_id", "revision", "episode_id", "scene_id",
    "display_locator", "narrative_purpose", "shot_type", "asset_bindings",
    "camera", "duration", "output_spec", "beats", "dialogue", "emotion",
    "lighting", "continuity", "constraints",
}

_ASPECT_RATIO = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")


def _validate_shot_spec(spec, field: str = "shot_spec") -> dict:
    """严格按 `contracts/domain-v1/shot-spec.v1.schema.json` 校验。

    **契约是权威，方案里的示例不是。** §4.1 的示例写了
    `shot_type: "medium_close_up"`、`lighting` 是对象、`output_spec` 用
    `resolution` 字符串、`continuity.state_in` 是对象——这四处都与 v1 schema
    不符，本函数按 schema 判（S06-G12 / C10）。示例过期不等于契约错。

    校验顺序与 schema 的 required 一致：先集合，再逐键类型与枚举。错误一律
    400 `INVALID_SCHEMA`，因为这是"body 非法"，不是"内容不可行"。
    """
    spec = _obj(spec, field)
    unknown = set(spec) - SHOT_SPEC_KEYS
    _require(not unknown, f"{field} has unknown keys: {sorted(unknown)}",
             details={"field": field, "unknown": sorted(unknown)})
    missing = SHOT_SPEC_KEYS - set(spec)
    _require(not missing, f"{field} is missing keys: {sorted(missing)}",
             details={"field": field, "missing": sorted(missing)})

    _require(spec["schema_version"] == "1.0",
             f"{field}.schema_version must be '1.0'",
             details={"field": f"{field}.schema_version"})
    _text(spec["shot_id"], f"{field}.shot_id")
    _int(spec["revision"], f"{field}.revision", minimum=1)
    _text(spec["episode_id"], f"{field}.episode_id")
    _text(spec["scene_id"], f"{field}.scene_id")

    locator = _obj(spec["display_locator"], f"{field}.display_locator")
    _require(set(locator) == {"episode", "scene", "shot"},
             f"{field}.display_locator must be exactly "
             f"{{episode, scene, shot}}",
             details={"field": f"{field}.display_locator"})
    for key in ("episode", "scene", "shot"):
        _int(locator[key], f"{field}.display_locator.{key}", minimum=1)

    _text(spec["narrative_purpose"], f"{field}.narrative_purpose")
    _enum(spec["shot_type"], f"{field}.shot_type", SHOT_TYPES)

    bindings = _obj(spec["asset_bindings"], f"{field}.asset_bindings")
    _require(set(bindings) == {"characters", "scene", "props", "style"},
             f"{field}.asset_bindings must be exactly "
             f"{{characters, scene, props, style}}",
             details={"field": f"{field}.asset_bindings"})
    characters = _list(bindings["characters"],
                       f"{field}.asset_bindings.characters", allow_empty=False)
    for index, member in enumerate(characters):
        where = f"{field}.asset_bindings.characters[{index}]"
        member = _obj(member, where)
        _require(set(member) == {"character", "costume", "voice", "screen_role"},
                 f"{where} must be exactly "
                 f"{{character, costume, voice, screen_role}}",
                 details={"field": where})
        for sub in ("character", "costume", "voice"):
            ref = _obj(member[sub], f"{where}.{sub}")
            _require(set(ref) == {"id", "revision"},
                     f"{where}.{sub} must be exactly {{id, revision}}",
                     details={"field": f"{where}.{sub}"})
            _text(ref["id"], f"{where}.{sub}.id")
            _int(ref["revision"], f"{where}.{sub}.revision", minimum=1)
        _text(member["screen_role"], f"{where}.screen_role")
    for sub in ("scene", "style"):
        ref = _obj(bindings[sub], f"{field}.asset_bindings.{sub}")
        _require(set(ref) == {"id", "revision"},
                 f"{field}.asset_bindings.{sub} must be exactly {{id, revision}}",
                 details={"field": f"{field}.asset_bindings.{sub}"})
        _text(ref["id"], f"{field}.asset_bindings.{sub}.id")
        _int(ref["revision"], f"{field}.asset_bindings.{sub}.revision",
             minimum=1)
    props = _list(bindings["props"], f"{field}.asset_bindings.props")
    for index, item in enumerate(props):
        where = f"{field}.asset_bindings.props[{index}]"
        item = _obj(item, where)
        _require(set(item) == {"id", "revision"},
                 f"{where} must be exactly {{id, revision}}",
                 details={"field": where})
        _text(item["id"], f"{where}.id")
        _int(item["revision"], f"{where}.revision", minimum=1)

    camera = _obj(spec["camera"], f"{field}.camera")
    _require(set(camera) == {"shot_size", "angle", "movement",
                             "movement_intensity", "composition"},
             f"{field}.camera must be exactly {{shot_size, angle, movement, "
             f"movement_intensity, composition}}",
             details={"field": f"{field}.camera",
                      "received": sorted(camera)})
    _enum(camera["shot_size"], f"{field}.camera.shot_size", SHOT_SIZES)
    _enum(camera["angle"], f"{field}.camera.angle", CAMERA_ANGLES)
    _enum(camera["movement"], f"{field}.camera.movement", CAMERA_MOVEMENTS)
    intensity = _num(camera["movement_intensity"],
                     f"{field}.camera.movement_intensity")
    _require(0.0 <= intensity <= 1.0,
             f"{field}.camera.movement_intensity must be within [0, 1]",
             details={"field": f"{field}.camera.movement_intensity"})
    _text(camera["composition"], f"{field}.camera.composition")

    duration = _obj(spec["duration"], f"{field}.duration")
    _require(set(duration) == {"target_seconds", "trim_allowed",
                               "timing_policy"},
             f"{field}.duration must be exactly "
             f"{{target_seconds, trim_allowed, timing_policy}}",
             details={"field": f"{field}.duration"})
    _require(_num(duration["target_seconds"],
                  f"{field}.duration.target_seconds") > 0,
             f"{field}.duration.target_seconds must be > 0",
             details={"field": f"{field}.duration.target_seconds"})
    _require(isinstance(duration["trim_allowed"], bool),
             f"{field}.duration.trim_allowed must be a boolean",
             details={"field": f"{field}.duration.trim_allowed"})
    _enum(duration["timing_policy"], f"{field}.duration.timing_policy",
          TIMING_POLICIES)

    output = _obj(spec["output_spec"], f"{field}.output_spec")
    _require(set(output) == {"aspect_ratio", "width", "height", "fps"},
             f"{field}.output_spec must be exactly "
             f"{{aspect_ratio, width, height, fps}}",
             details={"field": f"{field}.output_spec",
                      "received": sorted(output)})
    _require(isinstance(output["aspect_ratio"], str)
             and _ASPECT_RATIO.match(output["aspect_ratio"]) is not None,
             f"{field}.output_spec.aspect_ratio must match N:M",
             details={"field": f"{field}.output_spec.aspect_ratio"})
    _int(output["width"], f"{field}.output_spec.width", minimum=1)
    _int(output["height"], f"{field}.output_spec.height", minimum=1)
    _require(_num(output["fps"], f"{field}.output_spec.fps") > 0,
             f"{field}.output_spec.fps must be > 0",
             details={"field": f"{field}.output_spec.fps"})

    beats = _list(spec["beats"], f"{field}.beats", allow_empty=False)
    for index, beat in enumerate(beats):
        where = f"{field}.beats[{index}]"
        beat = _obj(beat, where)
        _require(set(beat) == {"time_range", "action"},
                 f"{where} must be exactly {{time_range, action}}",
                 details={"field": where})
        span = _obj(beat["time_range"], f"{where}.time_range")
        _require(set(span) == {"start_seconds", "end_seconds"},
                 f"{where}.time_range must be exactly "
                 f"{{start_seconds, end_seconds}}",
                 details={"field": f"{where}.time_range"})
        start = _num(span["start_seconds"], f"{where}.time_range.start_seconds")
        end = _num(span["end_seconds"], f"{where}.time_range.end_seconds")
        _require(start >= 0,
                 f"{where}.time_range.start_seconds must be >= 0",
                 details={"field": f"{where}.time_range.start_seconds"})
        _require(end > 0,
                 f"{where}.time_range.end_seconds must be > 0",
                 details={"field": f"{where}.time_range.end_seconds"})
        _require(end > start,
                 f"{where}.time_range.end_seconds must exceed start_seconds",
                 details={"field": where, "start_seconds": start,
                          "end_seconds": end})
        _text(beat["action"], f"{where}.action")

    dialogue = _list(spec["dialogue"], f"{field}.dialogue")
    for index, line in enumerate(dialogue):
        where = f"{field}.dialogue[{index}]"
        line = _obj(line, where)
        _require(set(line) == {"line_id", "character_id", "text", "locale",
                               "emotion", "voice"},
                 f"{where} must be exactly "
                 f"{{line_id, character_id, text, locale, emotion, voice}}",
                 details={"field": where, "received": sorted(line)})
        _text(line["line_id"], f"{where}.line_id")
        _text(line["character_id"], f"{where}.character_id")
        _text(line["text"], f"{where}.text")
        _text(line["locale"], f"{where}.locale")
        _text(line["emotion"], f"{where}.emotion")
        voice = _obj(line["voice"], f"{where}.voice")
        _require(set(voice) == {"id", "revision"},
                 f"{where}.voice must be exactly {{id, revision}}",
                 details={"field": f"{where}.voice"})
        _text(voice["id"], f"{where}.voice.id")
        _int(voice["revision"], f"{where}.voice.revision", minimum=1)

    ids = [line["line_id"] for line in dialogue]
    _require(len(ids) == len(set(ids)),
             f"{field}.dialogue repeats a line_id; one line belongs to one "
             f"shot (use a second shot for a second delivery)",
             details={"field": f"{field}.dialogue",
                      "duplicates": sorted({i for i in ids
                                            if ids.count(i) > 1})})

    _text(spec["emotion"], f"{field}.emotion")
    # `lighting` 在 v1 里是字符串，不是对象。§4.1 的示例是过期写法（C10）。
    _text(spec["lighting"], f"{field}.lighting")

    continuity = _obj(spec["continuity"], f"{field}.continuity")
    _require(set(continuity) == {"axis_of_action", "screen_direction",
                                 "state_in", "state_out", "previous_shot_ref",
                                 "reference_edges"},
             f"{field}.continuity must be exactly "
             f"{{axis_of_action, screen_direction, state_in, state_out, "
             f"previous_shot_ref, reference_edges}}",
             details={"field": f"{field}.continuity",
                      "received": sorted(continuity)})
    _text(continuity["axis_of_action"], f"{field}.continuity.axis_of_action")
    _text(continuity["screen_direction"],
          f"{field}.continuity.screen_direction")
    for key in ("state_in", "state_out"):
        items = _list(continuity[key], f"{field}.continuity.{key}")
        for index, item in enumerate(items):
            _text(item, f"{field}.continuity.{key}[{index}]")
    previous = continuity["previous_shot_ref"]
    if previous is not None:
        previous = _obj(previous, f"{field}.continuity.previous_shot_ref")
        _require(set(previous) == {"id", "revision"},
                 f"{field}.continuity.previous_shot_ref must be exactly "
                 f"{{id, revision}} or null",
                 details={"field": f"{field}.continuity.previous_shot_ref"})
        _text(previous["id"], f"{field}.continuity.previous_shot_ref.id")
        _int(previous["revision"],
             f"{field}.continuity.previous_shot_ref.revision", minimum=1)
    edges = _list(continuity["reference_edges"],
                  f"{field}.continuity.reference_edges")
    for index, edge in enumerate(edges):
        where = f"{field}.continuity.reference_edges[{index}]"
        edge = _obj(edge, where)
        _require(set(edge) == {"id", "revision"},
                 f"{where} must be exactly {{id, revision}}",
                 details={"field": where})
        _text(edge["id"], f"{where}.id")
        _int(edge["revision"], f"{where}.revision", minimum=1)

    constraints = _obj(spec["constraints"], f"{field}.constraints")
    _require(set(constraints) == {"must_preserve", "negative",
                                  "locked_fields"},
             f"{field}.constraints must be exactly "
             f"{{must_preserve, negative, locked_fields}}",
             details={"field": f"{field}.constraints"})
    for key in ("must_preserve", "negative", "locked_fields"):
        items = _list(constraints[key], f"{field}.constraints.{key}")
        for index, item in enumerate(items):
            _text(item, f"{field}.constraints.{key}[{index}]")
    return spec

# ---------------------------------------------------------------- 可行性

def _round3(value: float) -> float:
    return round(float(value) + 0.0, 3)


def _split_point(entries, target: float) -> tuple[list[str], list[str]]:
    """建议的拆分点：`(前半的行, 后半的行)`。

    逐条累加，第一条让累计时长越过目标的台词归入后半——"这一句放不下"是
    最直观的拆分点。全都不越过（目标够放）时后半为空，调用方不应走到这里。
    """
    cumulative = 0.0
    for index, entry in enumerate(entries):
        cumulative += entry["seconds"]
        if cumulative > target:
            return ([e["line_id"] for e in entries[:index]],
                    [e["line_id"] for e in entries[index:]])
    return ([e["line_id"] for e in entries], [])


def check_feasibility(spec: dict) -> dict:
    """§3.5.2。返回 `feasibility`，不可行时抛 422 并附三条 options。

    判定顺序有意如此：

    1. 先看 `target_seconds >= min_shot_seconds`。0.5s 的镜头连一次呼吸都放
       不下，谈"放不下台词"没有意义。
    2. 再看 beats 是否落在 `[0, target]` 内。节拍时间戳是**声明**，越界就是
       声明本身自相矛盾（§3.7 第 7 条），任何 policy 都不能豁免。
    3. 最后看对白。对白超出时按 `timing_policy` 决定是拒绝还是带警告接受：
       `ripple` 必须拒绝（会让相邻镜头跟着挪），`preserve_episode_duration`
       带 `episode_delta_seconds` 警告接受（T06-28）。**两种路径都不改台词**。
    """
    target = float(spec["duration"]["target_seconds"])
    policy = spec["duration"]["timing_policy"]
    if target < MIN_SHOT_SECONDS:
        raise ServiceError(
            422, "DURATION_BELOW_MINIMUM",
            f"target_seconds {target} is below the minimum shot length "
            f"{MIN_SHOT_SECONDS}s",
            details={"target_seconds": target,
                     "min_seconds": MIN_SHOT_SECONDS, "gap": "C12"})

    beats = spec["beats"]
    span = _beat_seconds(beats)
    for index, beat in enumerate(beats):
        end = float(beat["time_range"]["end_seconds"])
        start = float(beat["time_range"]["start_seconds"])
        if end > target + 1e-9 or start > target + 1e-9:
            raise ServiceError(
                422, "BEATS_EXCEED_DURATION",
                f"beats[{index}] ends at {end}s, beyond target {target}s",
                details={"beat_index": index, "start_seconds": start,
                         "end_seconds": end, "target_seconds": target,
                         "overflow_seconds": _round3(max(end, start) - target)})
    # 时间轴上的重叠是声明错误，不是"并行"。并行指的是 beat 与台词同时发生，
    # 不是两个 beat 抢同一段时间。
    ordered = sorted(beats, key=lambda b: b["time_range"]["start_seconds"])
    for previous, current in zip(ordered, ordered[1:]):
        if (float(current["time_range"]["start_seconds"])
                < float(previous["time_range"]["end_seconds"]) - 1e-9):
            raise ServiceError(
                422, "BEATS_EXCEED_DURATION",
                "two beats overlap on the timeline; overlapping beats make "
                "the required duration ambiguous",
                details={"rule": "beat_overlap",
                         "previous": previous["time_range"],
                         "current": current["time_range"],
                         "target_seconds": target})

    entries = [{"line_id": line["line_id"], "text": line["text"],
                "emotion": line.get("emotion"), "locale": line["locale"]}
               for line in spec["dialogue"]]
    dialogue_total = 0.0
    per_line: list[dict] = []
    for entry in entries:
        seconds = _dialogue_seconds([entry], entry["locale"])
        per_line.append({"line_id": entry["line_id"], "seconds": seconds})
        dialogue_total += seconds

    # 对白与节拍可以并行：两者都占满 4s 时，需求仍然是 4s，不是 8s。
    # 这正是 T06-26 的场景——可行，但必须给出"要并行"的警告。
    required = max(dialogue_total, span)
    warnings: list[dict] = []
    if (dialogue_total + span > target + 1e-9
            and required <= target + 1e-9):
        warnings.append({
            "kind": "parallel_required",
            "message": "对白与节拍必须并行才能放进目标时长",
            "dialogue_seconds": _round3(dialogue_total),
            "beats_seconds": _round3(span),
            "target_seconds": target})

    if dialogue_total > target + 1e-9:
        overflow = dialogue_total - target
        if policy == "preserve_episode_duration":
            # T06-28：显式延长，不静默裁剪。给出对整集时长的影响。
            warnings.append({
                "kind": "episode_duration_extended",
                "message": "镜头超出目标时长，整集时长随之增加",
                "episode_delta_seconds": _round3(overflow),
                "target_seconds": target,
                "required_seconds": _round3(dialogue_total)})
        else:
            first_half, second_half = _split_point(per_line, target)
            cut = second_half or ([per_line[-1]["line_id"]] if per_line
                                  else [])
            raise ServiceError(
                422, "DIALOGUE_EXCEEDS_DURATION",
                f"dialogue reads {_round3(dialogue_total)}s but the shot is "
                f"{target}s; S06 will not trim the lines and will not pick an "
                f"option for you",
                details={
                    "required_seconds": _round3(dialogue_total),
                    "target_seconds": target,
                    "overflow_seconds": _round3(overflow),
                    "options": [
                        {"kind": "extend",
                         "to_seconds": _round3(math.ceil(
                             dialogue_total * 10) / 10),
                         "episode_delta_seconds": _round3(
                             math.ceil(dialogue_total * 10) / 10 - target)},
                        {"kind": "split",
                         "at_line_ids": [second_half[0]] if second_half else [],
                         "keep_first": first_half,
                         "keep_second": second_half,
                         "into_shots": 2},
                        {"kind": "cut_lines", "line_ids": cut,
                         "requires_human": True},
                    ]})

    verdict = "feasible_with_warnings" if warnings else "feasible"
    return {
        "dialogue_seconds": _round3(dialogue_total),
        "beats_seconds": _round3(span),
        "required_seconds": _round3(required),
        "target_seconds": target,
        "verdict": verdict,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 对白与行覆盖

def _line_index(script: dict) -> dict[str, dict]:
    """`line_id -> line`。S04 的行列表是唯一权威（§1.2）。"""
    return {line["line_id"]: line for line in script["lines"]}


def check_dialogue_bytes(*, spec: dict, lines_by_id: dict[str, dict],
                         script: dict) -> list[dict]:
    """逐句核对 `dialogue[].text` 与 S04 的 `text` 逐字节相等（§1.2）。

    返回 `script_line_refs`（S06 侧把那四元组存进 `shot_dialogue_ref` 与
    `s06_meta`）。三件事在这里同时落定：

    - **字节相等**：不 strip、不归一化、不比较"语义相同"。繁简不同、多一个
      句号都算漂移（T06-32 / T06-33）。
    - **行必须存在**：S04 里没有这行 → 422 `DIALOGUE_TEXT_DRIFT` 而不是
      404——对 S06 而言这是"引用了一行不存在的台词"，属于内容错误。
    - **拆台词是违规**：每句必须整句出现（T06-30）。"只留半句"只能表现为
      文本不相等，这里必然被抓住。

    `emotion` 不参与比较：它是 S06 自己的字段，同一行在不同镜头可以有不同情绪
    （T06-36）。
    """
    refs: list[dict] = []
    for index, line in enumerate(spec["dialogue"]):
        source = lines_by_id.get(line["line_id"])
        if source is None:
            raise ServiceError(
                422, "DIALOGUE_TEXT_DRIFT",
                f"dialogue[{index}] references line {line['line_id']} which "
                f"does not exist in script {script['script_id']} revision "
                f"{script['revision']}",
                details={"line_id": line["line_id"], "index": index,
                         "rule": "line_missing"})
        if line["text"] != source["text"]:
            raise ServiceError(
                422, "DIALOGUE_TEXT_DRIFT",
                "dialogue text is not byte-identical to the script line; S06 "
                "copies lines verbatim and never shortens or rewrites them",
                details={"line_id": line["line_id"], "index": index,
                         "expected_hash": _sha256_hex(source["text"]),
                         "received_hash": _sha256_hex(line["text"]),
                         "expected_length": len(source["text"]),
                         "received_length": len(line["text"])})
        refs.append({
            "script_id": script["script_id"],
            "revision": int(script["revision"]),
            "line_id": line["line_id"],
            "text_hash": _sha256_hex(source["text"]),
            "text": source["text"],
            "emotion": line["emotion"],
            "locale": line["locale"],
        })
    return refs


def _span_line_ids(scene: dict, lines: list[dict]) -> list[str]:
    """`line_span` 覆盖的行 id（按 `seq` 推进）。

    S05 只给首尾 `line_id` 与 `line_count`；把 `line_count` 与"首尾之间
    实际有几行"对一遍，对不上说明两端不是同一版剧本 → 上游不自洽，503。
    """
    by_id = {line["line_id"]: line for line in lines}
    start = scene["line_span"]["start_line_id"]
    end = scene["line_span"]["end_line_id"]
    if start not in by_id or end not in by_id:
        raise ServiceError(
            503, "SCRIPT_SOURCE_UNAVAILABLE",
            "breakdown line_span references lines this script revision does "
            "not contain; the two upstreams disagree",
            retryable=True,
            details={"scene_id": scene["scene_id"], "start_line_id": start,
                     "end_line_id": end})
    ordered = sorted(lines, key=lambda line: line["seq"])
    first = by_id[start]["seq"]
    last = by_id[end]["seq"]
    span = [line["line_id"] for line in ordered
            if first <= line["seq"] <= last]
    declared = scene["line_span"].get("line_count")
    if declared is not None and declared != len(span):
        raise ServiceError(
            503, "SCRIPT_SOURCE_UNAVAILABLE",
            f"scene {scene['scene_id']} declares {declared} lines but the "
            f"script revision has {len(span)} between its endpoints",
            retryable=True,
            details={"scene_id": scene["scene_id"], "declared": declared,
                     "actual": len(span)})
    return span


def check_line_coverage(*, scenes: list[dict], shots: list[dict],
                        lines: list[dict], strict: bool = True) -> None:
    """每个场内"有对白的行"恰好被一个镜头覆盖（§3.1 校验①）。

    S05 保证"每行恰好属于一个场"，S06 保证"每行恰好属于一个镜头"。两层都做
    是因为中间可能有人手工删了一镜。

    **`strict=False` 用于局部改镜**：一次 E03 只改一镜，该场其余镜头不在这次
    请求里；此时只在"请求内出现的行"上查重，不能拿整场分母判漏（那会把所有
    未改动的镜头全判成缺失）。整场生成与整场替换用 `strict=True`。

    S06-G01：只有带 `speaker_id` 的行要求被覆盖。无 speaker 的是纯动作行，
    S05 也没承诺它们会被拍成台词。
    """
    by_scene: dict[str, list[dict]] = {}
    for shot in shots:
        by_scene.setdefault(shot["spec"]["scene_id"], []).append(shot)

    for scene in scenes:
        scene_id = scene["scene_id"]
        span = _span_line_ids(scene, lines)
        by_id = {line["line_id"]: line for line in lines}
        required = [line_id for line_id in span
                    if by_id[line_id].get("speaker_id")]
        covered: dict[str, int] = {}
        for shot in by_scene.get(scene_id, []):
            for line in shot["spec"]["dialogue"]:
                covered[line["line_id"]] = covered.get(line["line_id"], 0) + 1

        outside = sorted(set(covered) - set(span))
        if outside:
            raise ServiceError(
                422, "SCENE_NOT_IN_BREAKDOWN",
                f"shot(s) put lines outside scene {scene_id} into dialogue",
                details={"scene_id": scene_id, "line_ids": outside})

        overlap = sorted(line_id for line_id, count in covered.items()
                         if count > 1)
        if overlap:
            raise ServiceError(
                422, "LINE_COVERAGE_OVERLAP",
                "a line may belong to exactly one shot; overlapping coverage "
                "would let two shots claim the same dialogue",
                details={"scene_id": scene_id, "line_ids": overlap,
                         "counts": {line_id: covered[line_id]
                                    for line_id in overlap}})

        if not strict:
            continue
        missing = [line_id for line_id in required if line_id not in covered]
        if missing:
            raise ServiceError(
                422, "LINE_COVERAGE_GAP",
                f"scene {scene_id} has lines no shot covers",
                details={"scene_id": scene_id, "line_ids": missing,
                         "required_count": len(required),
                         "covered_count": len(required) - len(missing)})


def check_shot_order(*, shots: list[dict], scenes: list[dict]) -> None:
    """`shot_order` 在场内连续唯一，且与 `line_span` 的推进一致（校验③）。

    "连续"是 1..N 不许跳号。跨场的镜头顺序是两层 `(scene_order, shot_order)`，
    所以这里按场分别检查；整场生成时镜头列表的顺序就是写入顺序。
    """
    by_scene: dict[str, list[int]] = {}
    for shot in shots:
        by_scene.setdefault(shot["spec"]["scene_id"], []).append(
            int(shot["order"]))
    for scene in scenes:
        orders = sorted(by_scene.get(scene["scene_id"], []))
        expected = list(range(1, len(orders) + 1))
        if orders != expected:
            raise ServiceError(
                400, "INVALID_SCHEMA",
                f"scene {scene['scene_id']} shot_order must be 1..N without "
                f"gaps or duplicates",
                details={"scene_id": scene["scene_id"], "received": orders,
                         "expected": expected})

# ---------------------------------------------------------------- 字段路径校验

_PATH_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[0-9]+\])*$")


def _check_field_paths(patch: dict) -> None:
    """`patch` 的键必须是点分字段路径，不能是 JSON Pointer 或任意字符串。

    §4.3 的 `changed_fields` 用 `camera.angle` 这种点分写法；如果 patch 接受
    `camera/angle`，同一份"改了什么"就会有两种写法流到 S09。
    """
    for path in _flatten_paths(patch):
        for segment in path.split("."):
            if not _PATH_SEGMENT.match(segment):
                raise ServiceError(
                    400, "INVALID_SCHEMA",
                    f"patch field path {path!r} is not dotted "
                    f"identifier syntax (camera.angle, beats[2].action)",
                    details={"field": "patch", "path": path,
                             "segment": segment, "gap": "S06-G18"})


def _check_locked_monotonic(base: dict, merged: dict) -> None:
    """`constraints.locked_fields` 单调不减（§10.5 / C8）。

    `locked_fields` 是"这一镜锁定不许动的字段"，它一旦被悄悄移除，下一次
    patch 就能绕过所有保护。S06 没有 `:unlock` 命令，所以唯一的处理是拒绝。
    """
    before = list((base.get("constraints") or {}).get("locked_fields") or [])
    after = list((merged.get("constraints") or {}).get("locked_fields") or [])
    removed = [field for field in before if field not in after]
    if removed:
        raise ServiceError(
            422, "LOCKED_FIELD_REMOVED",
            "a new revision may not drop locked_fields; S06 has no :unlock",
            details={"removed": removed, "before": before, "after": after,
                     "gap": "C8"})


# ---------------------------------------------------------------- 产出者与资产

def _produced_by(actor, proposal, *, proposal_given: bool) -> dict:
    """`produced_by`：谁产出了这一版。

    只记"人"或"某个 agent task"，**不记模型名、不记提示词**——提示词归 S20
    （§0.2）。`actor` 来自签名的执行上下文，不是 body 里随便写的字符串。
    """
    actor = actor if isinstance(actor, dict) else {}
    out = {"actor": {"principal_id": actor.get("principal_id"),
                     "kind": actor.get("kind", "human")},
           "source": "proposal" if proposal_given else "manual"}
    if proposal_given and isinstance(proposal, dict):
        out["agent_task_id"] = proposal.get("agent_task_id")
        out["agent_role"] = proposal.get("agent_role")
    return out


def _check_asset_bindings(tenant_id: str, prepared: list[dict],
                          assets) -> None:
    """§3.1 校验⑥：每个 `asset_binding` 的 revision 必须存在。

    S10 未配置时**不校验**（`ASSET_REVISION_UNKNOWN` 只在能问的时候才问得
    出来）。这不是放行：绑定仍会原样写进 ShotSpec，下游 S25/S26 派单前还会
    按自己的引用解析再核一次。
    """
    if assets is None:
        return
    seen: set[tuple[str, str, int]] = set()
    for shot in prepared:
        bindings = shot["spec"]["asset_bindings"]
        refs: list[tuple[str, str, int]] = []
        for member in bindings["characters"]:
            for kind in ("character", "costume", "voice"):
                refs.append((kind, member[kind]["id"], member[kind]["revision"]))
        for kind in ("scene", "style"):
            refs.append((kind, bindings[kind]["id"], bindings[kind]["revision"]))
        for item in bindings["props"]:
            refs.append(("prop", item["id"], item["revision"]))
        for kind, asset_id, revision in refs:
            key = (kind, asset_id, revision)
            if key in seen:
                continue
            seen.add(key)
            try:
                # 租户由内核显式传，不从 ShotSpec 猜——asset_bindings 里没有
                # tenant，猜出来的只能是错的（AGENTS.md 租户上下文不由内容给）。
                found = assets(tenant_id, kind, asset_id, revision)
            except ConnectionError as exc:
                raise ServiceError(
                    503, "ASSET_SOURCE_UNAVAILABLE",
                    f"asset catalog unreachable: {exc}",
                    retryable=True) from exc
            if not found:
                raise ServiceError(
                    422, "ASSET_REVISION_UNKNOWN",
                    f"{kind} {asset_id} revision {revision} does not exist",
                    details={"kind": kind, "asset_id": asset_id,
                             "revision": revision,
                             "shot_id": shot["spec"]["shot_id"]})


# ---------------------------------------------------------------- 内核

class StoryboardService:
    """六条命令 + 六条查询。所有上游都是注入的 callback（AGENTS.md）。

    上游一个都不能少的是 S05 与 S04：没有分场无从分镜，没有剧本文本无从防漂移。
    S10 / S18 缺失可以降级（内联绑定、`partial` 上下文），S19 缺失走人工分镜。
    """

    def __init__(self, conn: sqlite3.Connection, *, breakdown_source=None,
                 script_source=None, context_pack_source=None,
                 agent_task_source=None, binding_set_source=None,
                 asset_source=None, policy_source=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        # 在 executescript 之后构造，否则 SDK 的 DDL 会把外面的事务提交掉。
        self.idem = SqliteIdempotencyStore(self.conn)
        self.outbox = Outbox(self.conn)
        self._breakdown = breakdown_source
        self._script = script_source
        self._context = context_pack_source
        self._agent = agent_task_source
        self._bindings = binding_set_source
        self._assets = asset_source
        self._policy = policy_source

    # ---- 命令外壳 --------------------------------------------------------

    def _command(self, tenant_id: str, command_id: str, produce,
                 request_hash: str = ""):
        """一个命令 = 一个事务；幂等结果与业务写入一起提交（E12）。"""
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "INSERT INTO idempotency_record (tenant_id, command_id,"
                " request_hash, result, created_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT (tenant_id, command_id) DO UPDATE SET"
                " result=excluded.result",
                (tenant_id, command_id, request_hash, _jcs(result), _now()))
            return result

    def _upstream(self, call, code: str):
        """上游不可达统一成 503，**不给 degraded default**（§7.5）。"""
        try:
            result = call()
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(503, code, str(exc), retryable=True,
                                   details=exc.details) from exc
            raise
        if result is None:
            raise ServiceError(503, code, "upstream returned no result",
                               retryable=True)
        return result

    # ---- 上游 ------------------------------------------------------------

    def _load_breakdown(self, tenant_id: str, ref: dict) -> dict:
        """S05 的分场 revision。**只读、不缓存**（§7.5：不许用本地缓存凑合）。"""
        breakdown_id = _text(ref.get("breakdown_id"),
                             "breakdown_ref.breakdown_id")
        revision = _int(ref.get("revision"), "breakdown_ref.revision", 1)
        expected = _bare_digest(ref.get("content_hash"),
                                "breakdown_ref.content_hash")
        if self._breakdown is None:
            raise ServiceError(
                503, "BREAKDOWN_SOURCE_UNAVAILABLE",
                "no S05 breakdown source is configured; S06 will not storyboard "
                "a scene list it cannot read", retryable=True)
        try:
            doc = self._upstream(
                lambda: self._breakdown(tenant_id, breakdown_id, revision),
                "BREAKDOWN_SOURCE_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status == 404:
                # 跨租户同码（E28）：别的租户的分场在这里就是不存在。
                raise ServiceError(
                    404, "NOT_FOUND",
                    f"breakdown {breakdown_id} revision {revision} not found",
                    details={"breakdown_id": breakdown_id,
                             "revision": revision}) from exc
            raise
        if doc.get("state") != "SEALED":
            raise ServiceError(
                422, "BREAKDOWN_NOT_SEALED",
                f"breakdown {breakdown_id} revision {revision} is "
                f"{doc.get('state')}, not SEALED",
                details={"breakdown_id": breakdown_id, "revision": revision,
                         "state": doc.get("state")})
        stored = str(doc.get("content_hash", "")).removeprefix("sha256:")
        if stored != expected:
            # 换最新 sealed 版本重试可能成功 → 409 而不是 422（§2.6）。
            raise ServiceError(
                409, "BREAKDOWN_REVISION_MOVED",
                f"breakdown_ref pins {expected[:12]}… but revision {revision} "
                f"is {stored[:12]}…; pin the latest sealed revision",
                retryable=True,
                details={"breakdown_id": breakdown_id, "revision": revision,
                         "expected_hash": "sha256:" + expected,
                         "actual_hash": "sha256:" + stored})
        return doc

    def _load_script(self, tenant_id: str, ref: dict) -> dict:
        """S04 的整版行列表。缺失即 503——跳过校验等于没有防漂移（§7.5）。"""
        script_id = _text(ref.get("script_id"), "script_ref.script_id")
        revision = _int(ref.get("revision"), "script_ref.revision", 1)
        expected = _bare_digest(ref.get("content_hash"),
                                "script_ref.content_hash")
        if self._script is None:
            raise ServiceError(
                503, "SCRIPT_SOURCE_UNAVAILABLE",
                "no S04 script source is configured; S06 will not accept "
                "dialogue it cannot verify", retryable=True)
        try:
            script = self._upstream(
                lambda: self._script(tenant_id, script_id, revision),
                "SCRIPT_SOURCE_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status == 404:
                raise ServiceError(
                    404, "NOT_FOUND",
                    f"script {script_id} revision {revision} not found",
                    details={"script_id": script_id, "revision": revision}
                ) from exc
            raise
        stored = str(script.get("content_hash", "")).removeprefix("sha256:")
        if stored != expected:
            raise ServiceError(
                409, "BREAKDOWN_REVISION_MOVED",
                f"script_ref pins {expected[:12]}… but revision {revision} is "
                f"{stored[:12]}…",
                retryable=True,
                details={"script_id": script_id, "revision": revision,
                         "expected_hash": "sha256:" + expected,
                         "actual_hash": "sha256:" + stored})
        return script

    def _load_binding_set(self, tenant_id: str, ref: dict) -> dict | None:
        """S10 的 BindingSet。**只用它核对存在性与 hash，不当资产权威**。

        S06-G02：方案没给 BindingSet 到 `ShotSpec.asset_bindings` 的映射，
        所以这里不编一份——绑定仍由 proposal / patch 内联给出。
        """
        if not ref:
            return None
        binding_set_id = _text(ref.get("binding_set_id"),
                               "asset_binding_source.binding_set_id")
        revision = _int(ref.get("revision"),
                        "asset_binding_source.revision", 1)
        expected = _bare_digest(ref.get("content_hash"),
                                "asset_binding_source.content_hash")
        if self._bindings is None:
            return None
        try:
            doc = self._upstream(
                lambda: self._bindings(tenant_id, binding_set_id, revision),
                "BINDING_SET_SOURCE_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status == 404:
                raise ServiceError(
                    422, "ASSET_REVISION_UNKNOWN",
                    f"binding set {binding_set_id} revision {revision} does "
                    f"not exist",
                    details={"binding_set_id": binding_set_id,
                             "revision": revision}) from exc
            raise
        stored = str(doc.get("content_hash", "")).removeprefix("sha256:")
        if stored != expected:
            raise ServiceError(
                422, "ASSET_REVISION_UNKNOWN",
                f"binding set {binding_set_id} revision {revision} hashes to "
                f"{stored[:12]}…, not the pinned {expected[:12]}…",
                details={"binding_set_id": binding_set_id,
                         "revision": revision,
                         "expected_hash": "sha256:" + expected,
                         "actual_hash": "sha256:" + stored})
        return doc

    def _load_context(self, tenant_id: str, ref) -> dict:
        """S18 的 ContextPack。缺失 → `partial`，不是拒绝（§7.5）。"""
        if not ref:
            return {"pack_id": None, "content_hash": None,
                    "coverage": "none"}
        pack_id = _text(ref.get("pack_id"), "context_pack_ref.pack_id")
        expected = _bare_digest(ref.get("content_hash"),
                                "context_pack_ref.content_hash")
        if self._context is None:
            return {"pack_id": pack_id, "content_hash": expected,
                    "coverage": "partial"}
        try:
            doc = self._upstream(
                lambda: self._context(tenant_id, pack_id),
                "CONTEXT_PACK_SOURCE_UNAVAILABLE")
        except ServiceError:
            return {"pack_id": pack_id, "content_hash": expected,
                    "coverage": "partial"}
        stored = str(doc.get("content_hash", "")).removeprefix("sha256:")
        coverage = "full" if stored == expected and doc.get("stale") is not True \
            else "partial"
        return {"pack_id": pack_id, "content_hash": expected,
                "coverage": coverage}

    def _load_proposal(self, tenant_id: str, ref) -> dict | None:
        """S19 `storyboard-director` 的输出。人工分镜时为空。"""
        if not ref:
            return None
        task_id = _text(ref.get("agent_task_id"), "proposal_ref.agent_task_id")
        expected = _bare_digest(ref.get("content_hash"),
                                "proposal_ref.content_hash")
        if self._agent is None:
            raise ServiceError(
                422, "PROPOSAL_UNAVAILABLE",
                "proposal_ref was given but no S19 agent source is configured",
                details={"agent_task_id": task_id})
        proposal = self._upstream(
            lambda: self._agent(tenant_id, task_id),
            "AGENT_TASK_SOURCE_UNAVAILABLE")
        stored = str(proposal.get("content_hash", "")).removeprefix("sha256:")
        if stored != expected:
            raise ServiceError(
                422, "PROPOSAL_UNAVAILABLE",
                "the agent task result is not the proposal the caller pinned",
                details={"agent_task_id": task_id,
                         "expected_hash": "sha256:" + expected,
                         "actual_hash": "sha256:" + stored})
        return proposal

    # ---- 场景与镜头身份 --------------------------------------------------

    def _breakdown_scenes(self, doc: dict, wanted: list[str]) -> list[dict]:
        by_id = {scene["scene_id"]: scene for scene in doc.get("scenes", [])}
        unknown = [scene_id for scene_id in wanted if scene_id not in by_id]
        if unknown:
            raise ServiceError(
                422, "SCENE_NOT_IN_BREAKDOWN",
                "scene_id(s) are not part of the pinned breakdown revision",
                details={"breakdown_id": doc["breakdown_id"],
                         "revision": doc["revision"], "scene_ids": unknown,
                         "known_scene_ids": sorted(by_id)})
        return [by_id[scene_id] for scene_id in wanted]

    def _storyboard(self, tenant_id: str, storyboard_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM storyboard WHERE tenant_id=? AND storyboard_id=?",
            (tenant_id, storyboard_id)).fetchone()
        return None if row is None else dict(row)

    def _require_storyboard(self, tenant_id: str, storyboard_id: str) -> dict:
        row = self._storyboard(tenant_id, storyboard_id)
        if row is None:
            # 跨租户同码（E28）：别人的 storyboard 在这里就是不存在。
            raise ServiceError(404, "NOT_FOUND",
                               f"storyboard {storyboard_id} not found")
        return row

    def _find_shot(self, tenant_id: str, shot_id: str,
                   branch_id: str | None = None) -> dict:
        """`shot_id` 的作用域是 `(tenant, branch)`（§3.3.1）。

        URL 里只有 `shot_id`，所以跨分支同名时要求调用方给 `branch_id`。
        **不猜分支**：猜错会让一次改动落到另一条实验分支上，且没有任何报错。
        """
        if branch_id:
            row = self.conn.execute(
                "SELECT * FROM shot WHERE tenant_id=? AND branch_id=? AND"
                " shot_id=?", (tenant_id, branch_id, shot_id)).fetchone()
            if row is None:
                raise ServiceError(404, "NOT_FOUND",
                                   f"shot {shot_id} not found in branch "
                                   f"{branch_id}")
            return dict(row)
        rows = self.conn.execute(
            "SELECT * FROM shot WHERE tenant_id=? AND shot_id=?",
            (tenant_id, shot_id)).fetchall()
        if not rows:
            # 跨租户同码（E28）。
            raise ServiceError(404, "NOT_FOUND", f"shot {shot_id} not found")
        if len(rows) > 1:
            raise ServiceError(
                400, "INVALID_SCHEMA",
                f"shot {shot_id} exists in several branches; branch_id is "
                f"required to disambiguate",
                details={"shot_id": shot_id,
                         "branch_ids": sorted(row["branch_id"] for row in rows)})
        return dict(rows[0])

    def _latest_shot_revision(self, tenant_id: str, branch_id: str,
                              shot_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM shot_revision WHERE tenant_id=? AND branch_id=? AND"
            " shot_id=? ORDER BY revision DESC LIMIT 1",
            (tenant_id, branch_id, shot_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"shot {shot_id} has no revision")
        return dict(row)

    # ---- shot_lineage_id（本文核心） ------------------------------------

    def _mint_lineage(self, tenant_id: str, series_id: str, *,
                      branch_id: str, parent_branch_id: str | None,
                      proposal_shot_id: str | None,
                      parent_shots: dict[str, dict],
                      existing_shots: dict[str, dict],
                      origin_shot_id: str | None = None) -> dict:
        """§3.2.2。**三种情形，没有第四种。**

        1. 跨分支分叉且候选 `shot_id` 在父分支里 → 继承父分支的 lineage。
        2. 同分支重规划且候选 `shot_id` 已存在 → 沿用它的 lineage。
        3. 其余 → 铸造新 lineage。

        候选 `shot_id` 在本分支不存在也不是从父分支继承时，是"凭空指定 ID"，
        422 `UNKNOWN_SHOT_ID`（S06-G08）。**已退役的 `shot_id` 也不接受**：
        复用会让新镜头与已退役镜头共享尝试计数，而历史尝试已经用掉上限
        （T06-12 的展开说明）。
        """
        if (branch_id != parent_branch_id and proposal_shot_id
                and proposal_shot_id in parent_shots):
            inherited = parent_shots[proposal_shot_id]
            return {"shot_lineage_id": inherited["shot_lineage_id"],
                    "minted": False, "case": "branch_inherit",
                    "origin_shot_id": inherited["shot_id"]}

        if proposal_shot_id:
            if proposal_shot_id in existing_shots:
                existing = existing_shots[proposal_shot_id]
                if existing["state"] != "active":
                    raise ServiceError(
                        422, "UNKNOWN_SHOT_ID",
                        f"shot {proposal_shot_id} is retired; a shot that was "
                        f"retired is never reused — mint a new one",
                        details={"shot_id": proposal_shot_id,
                                 "state": existing["state"],
                                 "gap": "S06-G08"})
                return {"shot_lineage_id": existing["shot_lineage_id"],
                        "minted": False, "case": "same_branch",
                        "origin_shot_id": origin_shot_id}
            raise ServiceError(
                422, "UNKNOWN_SHOT_ID",
                f"proposal names shot {proposal_shot_id}, which does not exist "
                f"in branch {branch_id}",
                details={"shot_id": proposal_shot_id,
                         "branch_id": branch_id, "gap": "S06-G08"})

        shot_lineage_id = _new_id("lineage")
        self.conn.execute(
            "INSERT INTO shot_lineage (tenant_id, shot_lineage_id, series_id,"
            " origin_branch_id, origin_shot_id, state, retired_at, created_at)"
            " VALUES (?,?,?,?,?,'live',NULL,?)",
            (tenant_id, shot_lineage_id, series_id, branch_id,
             origin_shot_id or "", _now()))
        return {"shot_lineage_id": shot_lineage_id, "minted": True,
                "case": "new", "origin_shot_id": origin_shot_id}

    def _retire_lineage_if_idle(self, tenant_id: str,
                                shot_lineage_id: str) -> bool:
        """该 lineage 在所有分支都没有活动镜头时才退役（§3.6.1）。"""
        alive = self.conn.execute(
            "SELECT 1 FROM shot WHERE tenant_id=? AND shot_lineage_id=? AND"
            " state='active' LIMIT 1",
            (tenant_id, shot_lineage_id)).fetchone()
        if alive is not None:
            return False
        cursor = self.conn.execute(
            "UPDATE shot_lineage SET state='retired', retired_at=?"
            " WHERE tenant_id=? AND shot_lineage_id=? AND state='live'",
            (_now(), tenant_id, shot_lineage_id))
        return cursor.rowcount == 1

    # ---- shot_id 继承策略 ------------------------------------------------

    def _resolve_shot_ids(self, *, mode: str, raw_shots: list[dict],
                          existing_shots: dict[str, dict],
                          shot_id_map: dict | None,
                          manual: bool) -> list[str | None]:
        """§3.3.2 的四种策略，返回每个候选的 `shot_id`（None = 新铸）。

        `inherit_by_proposal` 在自动路径被禁（§3.3.3）：直接采信 LLM 给的
        `shot_id` 会让它把 A 镜的 ID 分给一个全新的画面，S09 于是只重算依赖
        A 的子图，而整场其实都变了——漏掉的影响面不会报错。
        """
        if mode == "fresh":
            return [None] * len(raw_shots)
        if mode == "inherit_explicit":
            if not isinstance(shot_id_map, dict):
                raise ServiceError(
                    400, "INVALID_SCHEMA",
                    "inherit_explicit needs options.shot_id_map",
                    details={"field": "options.shot_id_map"})
            out: list[str | None] = []
            for index, _shot in enumerate(raw_shots):
                chosen = shot_id_map.get(str(index))
                if chosen is None:
                    out.append(None)
                    continue
                _text(chosen, f"options.shot_id_map[{index}]")
                if chosen not in existing_shots:
                    raise ServiceError(
                        422, "UNKNOWN_SHOT_ID",
                        f"options.shot_id_map[{index}] names {chosen}, which "
                        f"does not exist in this branch",
                        details={"shot_id": chosen, "index": index,
                                 "gap": "S06-G08"})
                out.append(chosen)
            return out
        if mode == "inherit_by_proposal":
            if not manual:
                raise ServiceError(
                    403, "PERMISSION_DENIED",
                    "inherit_by_proposal is forbidden on the automated path: "
                    "trusting an agent's shot_id lets a replan silently move "
                    "content between shots",
                    details={"mode": mode, "gap": "C6"})
            return [shot.get("shot_id") for shot in raw_shots]
        # inherit_by_beat_anchor（默认）。锚点是"首个对白行落在旧某镜的行集合
        # 内"。方案写的是"首个 beat 覆盖的 line_id"，但 shot-spec.v1 的 beat
        # 没有行引用（S06-G15），所以退到 dialogue[0].line_id——它同样是"这一镜
        # 从哪一行开始"，且边界移动时稳定。
        anchor_owner: dict[str, str] = {}
        for shot_id, row in existing_shots.items():
            for line in _loads(row["dialogue_texts"], []):
                anchor_owner.setdefault(line["line_id"], shot_id)
        out = []
        used: set[str] = set()
        for shot in raw_shots:
            spec = shot["spec"]
            anchor = spec["dialogue"][0]["line_id"] if spec["dialogue"] else None
            owner = anchor_owner.get(anchor) if anchor else None
            if owner and owner not in used:
                used.add(owner)
                out.append(owner)
            else:
                out.append(None)
        return out

    # ---- 写入一版分镜 ----------------------------------------------------

    def _write_shot_revision(self, *, tenant_id: str, branch_id: str,
                             storyboard_id: str, storyboard_revision: int,
                             shot: dict, revision: int, scene_order: int,
                             shot_order: int, display_shot: int,
                             produced_by: dict,
                             state: str, sealed_at: str | None) -> dict:
        """落一行 `shot_revision` 与它的节拍、对白引用。

        `revision` 由调用方算好（跨整场版本单调递增，见模块头）。返回一个
        供事件与响应使用的摘要。
        """
        spec = shot["spec"]
        feasibility = shot["feasibility"]
        refs = shot["refs"]
        content_hash = shot["content_hash"]
        self.conn.execute(
            "INSERT INTO shot_revision (tenant_id, branch_id, shot_id, revision,"
            " state, storyboard_id, storyboard_revision, scene_id, shot_order,"
            " display_shot, shot_spec, content_hash, target_seconds,"
            " min_seconds_ok, feasibility, dialogue_texts, parent_revision,"
            " produced_by, sealed_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, branch_id, shot["shot_id"], revision, state,
             storyboard_id, storyboard_revision, spec["scene_id"], shot_order,
             display_shot, _jcs(spec), content_hash,
             float(spec["duration"]["target_seconds"]), 1,
             _jcs(feasibility),
             _jcs([{"line_id": ref["line_id"],
                    "text_hash": ref["text_hash"]} for ref in refs]),
             shot.get("parent_revision"), _jcs(produced_by), sealed_at, _now()))
        for index, beat in enumerate(spec["beats"]):
            self.conn.execute(
                "INSERT INTO shot_beat (tenant_id, branch_id, shot_id, revision,"
                " beat_index, start_seconds, end_seconds, action)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (tenant_id, branch_id, shot["shot_id"], revision, index,
                 float(beat["time_range"]["start_seconds"]),
                 float(beat["time_range"]["end_seconds"]), beat["action"]))
        for ref in refs:
            self.conn.execute(
                "INSERT INTO shot_dialogue_ref (tenant_id, branch_id, shot_id,"
                " revision, line_id, script_id, script_revision, text_hash,"
                " text_copy, emotion) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, branch_id, shot["shot_id"], revision,
                 ref["line_id"], ref["script_id"], ref["revision"],
                 ref["text_hash"], ref["text"],
                 ref.get("emotion")))
        return {"shot_id": shot["shot_id"], "revision": revision,
                "content_hash": content_hash,
                "shot_lineage_id": shot["shot_lineage_id"],
                "changed_fields": shot.get("changed_fields", []),
                "previous_revision": shot.get("parent_revision")}

    def _emit_shot_event(self, *, tenant_id: str, branch_id: str,
                         shot_id: str, shot_lineage_id: str, revision: int,
                         changed_fields: list[str], previous_revision,
                         content_hash: str, storyboard_id: str,
                         storyboard_revision: int, episode_id: str,
                         command_id: str, home_cell: str) -> None:
        """`storyboard.shot.revision.created` —— **唯一登记进 outbox 的类型**。

        `data` 是 8 个键、`additionalProperties: false`，与
        `contracts/storyboard.shot-revision-created.v2.schema.json` 逐键对齐。
        `aggregate_id = shot_id`（SRC-2：跨分支同名会撞，登记在 S06-G16，
        本服务不改契约）。

        只有 `SEALED` 的镜头版本发事件；DRAFT 不发（§6.3 / T06-45）。
        """
        data = {
            "schema_version": SCHEMA_VERSION,
            "shot_id": shot_id,
            "shot_lineage_id": shot_lineage_id,
            "revision": int(revision),
            "changed_fields": list(changed_fields),
            "previous_revision": previous_revision,
            "content_hash": content_hash,
            "source_storyboard_ref": {"storyboard_id": storyboard_id,
                                      "revision": int(storyboard_revision)},
        }
        self.outbox.append(_outbox_event(
            tenant_id=tenant_id,
            event_type="storyboard.shot.revision.created",
            subject=episode_id,
            aggregate_id=shot_id, sequence=int(revision),
            command_id=command_id, data=data, home_cell=home_cell,
            data_ref={"service_id": SOURCE, "id": shot_id,
                      "revision": int(revision),
                      "content_hash": content_hash,
                      "branch_id": branch_id}))

    # ---- 整场分镜 --------------------------------------------------------

    def create_revision(self, tenant_id: str, storyboard_id: str | None,
                        body: dict, *, command_id: str, request_hash: str,
                        home_cell: str = "cell_a1") -> dict:
        """`POST /storyboards/{id}/revisions` 的命令外壳（一个命令一个事务）。"""
        return self._command(
            tenant_id, command_id,
            lambda: self._create_revision_body(
                tenant_id, storyboard_id, body, command_id=command_id,
                home_cell=home_cell),
            request_hash)

    def _create_revision_body(self, tenant_id: str, storyboard_id: str | None,
                              body: dict, *, command_id: str,
                              home_cell: str = "cell_a1") -> dict:
        """`POST /storyboards/{id}/revisions`（§2.4）。

        一次请求产出一版 `DRAFT` 分镜（快照）。`:seal` 才是发整场事件与变成
        `SEALED` 的地方；这个命令不发 outbox，只写本地审计（§6.3）。
        """
        _require(body.get("schema_version") == SCHEMA_VERSION,
                 f"schema_version must be '{SCHEMA_VERSION}'")
        options = body.get("options") or {}
        _require(isinstance(options, dict), "options must be an object")
        continuity = options.get("shot_id_continuity", DEFAULT_CONTINUITY)
        _enum(continuity, "options.shot_id_continuity", CONTINUITY_MODES)
        max_shots = options.get("max_shots_per_scene",
                                MAX_SHOTS_PER_SCENE_DEFAULT)
        _int(max_shots, "options.max_shots_per_scene", 1)
        _require(max_shots <= MAX_SHOTS_PER_SCENE_HARD,
                 f"options.max_shots_per_scene is capped at "
                 f"{MAX_SHOTS_PER_SCENE_HARD} (S06-G06)",
                 details={"max_shots_per_scene": max_shots,
                          "hard_limit": MAX_SHOTS_PER_SCENE_HARD,
                          "gap": "S06-G06"})

        breakdown_doc = self._load_breakdown(tenant_id,
                                             body.get("breakdown_ref") or {})
        script = self._load_script(tenant_id, body.get("script_ref") or {})
        scene_ids = _list(body.get("scene_ids"), "scene_ids",
                          allow_empty=False)
        for index, scene_id in enumerate(scene_ids):
            _text(scene_id, f"scene_ids[{index}]")
        if len(set(scene_ids)) != len(scene_ids):
            raise ServiceError(400, "INVALID_SCHEMA",
                               "scene_ids repeats a scene")
        scenes = self._breakdown_scenes(breakdown_doc, scene_ids)

        # 分场里声明的剧本必须与请求 pin 的是同一版。S05 的 `source_script`
        # 是权威；不一致说明调用方拼错了两个上游引用。
        declared = breakdown_doc.get("source_script") or {}
        if declared.get("script_id") and declared["script_id"] != script["script_id"]:
            raise ServiceError(
                422, "SCENE_NOT_IN_BREAKDOWN",
                "breakdown_ref and script_ref point at different scripts",
                details={"breakdown_script_id": declared.get("script_id"),
                         "script_ref_script_id": script["script_id"]})

        binding_doc = self._load_binding_set(
            tenant_id, body.get("asset_binding_source") or {})
        context = self._load_context(tenant_id, body.get("context_pack_ref"))
        proposal = self._load_proposal(tenant_id, body.get("proposal_ref"))

        # 候选镜头：proposal 优先，其次是请求体自带的 shots（人工分镜路径）。
        raw_shots = self._collect_proposals(tenant_id, body, proposal, scenes,
                                            max_shots=max_shots)
        lines_by_id = _line_index(script)

        parent_id = options.get("parent_storyboard_id")
        parent_row = None
        if parent_id:
            parent_row = self._require_storyboard(tenant_id, parent_id)
        branch_id = self._target_branch(
            tenant_id, storyboard_id, body.get("branch_id"),
            parent_row, breakdown_doc)
        _text(branch_id, "branch_id")
        series_id = (parent_row["series_id"] if parent_row
                     else breakdown_doc.get("series_id") or "series")
        project_id = (parent_row["project_id"] if parent_row
                      else breakdown_doc.get("project_id") or "project")

        container, created = self._ensure_container(
            tenant_id=tenant_id, storyboard_id=storyboard_id,
            breakdown_doc=breakdown_doc, branch_id=branch_id,
            series_id=series_id, project_id=project_id,
            created_by=body.get("actor") or {})
        storyboard_id = container["storyboard_id"]

        parent_shots = self._shots_of_storyboard(tenant_id, parent_row) \
            if parent_row else {}
        existing_shots = self._active_shots(tenant_id, branch_id)
        chosen_ids = self._resolve_shot_ids(
            mode=continuity, raw_shots=raw_shots,
            existing_shots=existing_shots,
            shot_id_map=options.get("shot_id_map"),
            manual=proposal is None)

        # 逐镜校验 + 铸造 identity + 算 hash。
        prepared: list[dict] = []
        seen_ids: set[str] = set()
        lineages: list[dict] = []
        for index, shot in enumerate(raw_shots):
            spec = shot["spec"]
            if spec["episode_id"] != breakdown_doc["episode_id"]:
                raise ServiceError(
                    422, "SCENE_NOT_IN_BREAKDOWN",
                    f"shots[{index}].episode_id {spec['episode_id']} disagrees "
                    f"with breakdown {breakdown_doc['episode_id']}",
                    details={"field": "episode_id", "index": index})
            if spec["scene_id"] not in scene_ids:
                raise ServiceError(
                    422, "SCENE_NOT_IN_BREAKDOWN",
                    f"shots[{index}].scene_id {spec['scene_id']} is not in "
                    f"scene_ids",
                    details={"field": "scene_id", "index": index,
                             "scene_id": spec["scene_id"]})

            # 身份由策略决定，不由候选自称（§3.3.1）。三种落点：
            #   a) 策略给出了 shot_id → 用它（`inherit_explicit` /
            #      `inherit_by_beat_anchor` / 人工的 `inherit_by_proposal`）；
            #   b) 策略没给出、候选 ID 在本分支或父分支存在 → 这是"自称继承"
            #      但锚点没命中，拒绝；`fresh` 例外，按"全部新铸"改铸新 ID；
            #   c) 策略没给出、候选 ID 哪里都不存在 → 它是新标签，收下。
            candidate = spec["shot_id"]
            resolved = chosen_ids[index]
            # 候选 ID 的四种处境必须分开，混在一起会各错一半：
            #   a) 本分支的活动身份 → 同分支重规划，只认锚点（S06-G19）；
            #   b) 本分支的退役身份 → 拒绝（S06-G08 / T06-12），复用会让新
            #      镜头与退役镜头共享尝试计数；
            #   c) 父分支的身份且这次是分叉 → 正当的分支继承（T06-09）；
            #   d) 哪里都没有 → 新标签，收下。
            in_branch = candidate in existing_shots
            retired = self._retired_shot(tenant_id, branch_id, candidate)
            if resolved is not None:
                shot_id = resolved
            elif retired is not None:
                raise ServiceError(
                    422, "UNKNOWN_SHOT_ID",
                    f"shots[{index}] reuses shot_id {candidate}, which was "
                    f"retired on {branch_id}; a retired identity is never "
                    f"resurrected — submit a new shot_id (S06-G08)",
                    details={"shot_id": candidate, "index": index,
                             "state": retired["state"], "gap": "S06-G08"})
            elif in_branch and continuity == "fresh":
                # `fresh` 的意思是“全部新铸”。候选自称的旧 ID 在这里被换成
                # 新 ID，所以它**不**参与后续的继承判定（否则 _mint_lineage
                # 会拿这个崭新的 ID 去 existing_shots 里找，找不到就 422）。
                shot_id = _new_id("shot")
            elif in_branch:
                raise ServiceError(
                    422, "UNKNOWN_SHOT_ID",
                    f"shots[{index}] reuses existing shot_id {candidate} but "
                    f"shot_id_continuity={continuity!r} found no anchor for it; "
                    f"S06 will not reassign an existing identity (S06-G19)",
                    details={"shot_id": candidate, "index": index,
                             "mode": continuity, "gap": "S06-G19"})
            else:
                shot_id = candidate
            if shot_id != candidate:
                spec["shot_id"] = shot_id
            if shot_id in seen_ids:
                raise ServiceError(400, "INVALID_SCHEMA",
                                   f"shots[{index}] repeats shot_id {shot_id}",
                                   details={"shot_id": shot_id})
            seen_ids.add(shot_id)

            refs = check_dialogue_bytes(spec=spec, lines_by_id=lines_by_id,
                                        script=script)
            feasibility = check_feasibility(spec)
            # 只有“最终这个 shot_id 已经属于某个身份”时才让它参与继承判定。
            # `fresh` 换过 ID 之后它不再属于任何身份，于是走情形 3 新铸。
            owns_identity = shot_id in existing_shots or shot_id in parent_shots
            minted = self._mint_lineage(
                tenant_id, series_id, branch_id=branch_id,
                parent_branch_id=(parent_row["branch_id"] if parent_row
                                  else branch_id),
                proposal_shot_id=(shot_id if owns_identity else None),
                parent_shots=parent_shots, existing_shots=existing_shots,
                origin_shot_id=(shot_id if owns_identity else None))
            lineages.append({"shot_id": shot_id, **minted})
            previous = self._last_shot_revision(tenant_id, branch_id, shot_id)
            content_hash = shot_content_hash(spec, refs)
            prepared.append({
                "shot_id": shot_id,
                "spec": spec,
                "refs": refs,
                "feasibility": feasibility,
                "content_hash": content_hash,
                "shot_lineage_id": minted["shot_lineage_id"],
                "order": shot["order"],
                "scene_order": shot["scene_order"],
                # 落库计数：这一镜的内容第几次入库。复制的镜头也拿新号，
                # 所以它不是"改过几次"（见模块头）。
                "revision": (previous or 0) + 1,
                "parent_revision": previous,
                "fingerprint": shot_fingerprint(spec, refs),
                "origin_shot_id": minted.get("origin_shot_id"),
                "changed_fields": [],
            })

        _check_asset_bindings(tenant_id, prepared, self._assets)
        check_line_coverage(scenes=scenes, shots=[
            {"spec": shot["spec"]} for shot in prepared],
            lines=script["lines"], strict=True)
        check_shot_order(shots=prepared, scenes=scenes)
        self._check_shot_limit(prepared, max_shots)

        fingerprint = self._revision_fingerprint(prepared)
        existing = self.conn.execute(
            "SELECT revision FROM storyboard_revision WHERE tenant_id=? AND"
            " storyboard_id=? AND content_fingerprint=?",
            (tenant_id, storyboard_id, fingerprint)).fetchone()
        if existing is not None:
            # §7.4 / T06-04：同内容返回既有 revision，不新建。
            return {"deduplicated": True,
                    "existing_revision": int(existing["revision"]),
                    "storyboard_id": storyboard_id}

        # 乐观并发：`head_revision` 就是这一版的 parent。
        parent_revision = int(body.get("parent_revision") or 0)
        current_head = int(container["head_revision"])
        if parent_revision != current_head:
            raise ServiceError(
                409, "STORYBOARD_REVISION_CONFLICT",
                f"storyboard {storyboard_id} is at head_revision "
                f"{current_head}, not {parent_revision}",
                retryable=True,
                details={"storyboard_id": storyboard_id,
                         "parent_revision": parent_revision,
                         "head_revision": current_head, "gap": "S06-G09"})
        revision = current_head + 1
        cursor = self.conn.execute(
            "UPDATE storyboard SET head_revision=?, updated_at=? WHERE"
            " tenant_id=? AND storyboard_id=? AND head_revision=?",
            (revision, _now(), tenant_id, storyboard_id, current_head))
        if cursor.rowcount != 1:
            raise ServiceError(
                409, "STORYBOARD_REVISION_CONFLICT",
                "storyboard head moved while this command was running",
                retryable=True,
                details={"storyboard_id": storyboard_id,
                         "parent_revision": parent_revision})

        produced_by = _produced_by(body.get("actor"), proposal,
                                   proposal_given=proposal is not None)
        content_hash = self._revision_content_hash(
            revision=revision, breakdown_doc=breakdown_doc, script=script,
            scenes=scenes, prepared=prepared)
        self.conn.execute(
            "INSERT INTO storyboard_revision (tenant_id, storyboard_id,"
            " revision, state, episode_id, branch_id, breakdown_id,"
            " breakdown_revision, breakdown_hash, script_id, script_revision,"
            " script_hash, binding_set_id, binding_set_revision,"
            " binding_set_hash, context_pack_id, context_pack_hash,"
            " context_coverage, policy_ref, proposal_ref, options, produced_by,"
            " parent_revision, scene_ids, shot_count, changed_shot_ids,"
            " retired_shot_ids, lineage_mints, content_hash,"
            " content_fingerprint, sealed_at, created_at)"
            " VALUES (?,?,?,'DRAFT',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,?,NULL,?)",
            (tenant_id, storyboard_id, revision, breakdown_doc["episode_id"],
             branch_id, breakdown_doc["breakdown_id"],
             int(breakdown_doc["revision"]),
             _bare_digest(breakdown_doc["content_hash"], "breakdown"),
             script["script_id"], int(script["revision"]),
             _bare_digest(script["content_hash"], "script"),
             binding_doc["binding_set_id"] if binding_doc else None,
             int(binding_doc["revision"]) if binding_doc else None,
             _bare_digest(binding_doc["content_hash"], "binding")
             if binding_doc else None,
             context["pack_id"], context["content_hash"],
             context["coverage"],
             _jcs(body.get("policy_ref") or {}),
             _jcs(body.get("proposal_ref") or {}),
             _jcs(options), _jcs(produced_by), parent_revision or None,
             _dumps(scene_ids), len(prepared), _dumps([]), _dumps([]),
             _jcs(lineages), content_hash, fingerprint, _now()))

        wrote = self._write_shots(
            tenant_id=tenant_id, branch_id=branch_id,
            storyboard_id=storyboard_id, storyboard_revision=revision,
            prepared=prepared, produced_by=produced_by,
            state="DRAFT", sealed=False, command_id=command_id,
            home_cell=home_cell, episode_id=breakdown_doc["episode_id"])

        for minted in lineages:
            if minted["minted"]:
                _local_event(
                    self.conn, tenant_id, LOCAL_EVENT_LINEAGE_MINTED,
                    minted["shot_lineage_id"],
                    {"shot_lineage_id": minted["shot_lineage_id"],
                     "shot_id": minted["shot_id"], "branch_id": branch_id,
                     "series_id": series_id, "case": minted["case"]},
                    command_id=command_id)
        _local_event(
            self.conn, tenant_id, LOCAL_EVENT_REVISION_CREATED, storyboard_id,
            {"storyboard_id": storyboard_id, "revision": revision,
             "state": "DRAFT", "shot_count": len(prepared),
             "shot_lineages": wrote,
             "content_hash": "sha256:" + content_hash},
            command_id=command_id)

        return {"storyboard_id": storyboard_id, "revision": revision,
                "state": "DRAFT", "created": created,
                "deduplicated": False, "shot_count": len(prepared),
                "shot_lineages": wrote,
                "content_hash": _prefixed(content_hash)}

    # ---- 整场分镜的内部件 ------------------------------------------------

    def _target_branch(self, tenant_id: str, storyboard_id: str | None,
                       requested, parent_row: dict | None,
                       breakdown_doc: dict) -> str:
        """决定这一版落在哪个分支。

        `options.parent_storyboard_id` 是**分叉**：调用方必须同时用
        `branch_id` 指明新分支。父分镜自己的分支不能当默认值——那样分叉
        永远落回原地，等于普通重规划，而分叉要的正是"从父分支的某一版起
        另开一条分支继续改"（T06-09 / T06-10）。

        没有父分镜时，`branch_id` 优先，其次沿用既有容器的分支，最后才看
        分场文档声明的分支。沿用容器分支是为了让"不带 branch_id 的重规划"
        落回原分支，而不是被 `_ensure_container` 以分支不符为由拒绝。
        """
        if parent_row is not None:
            _require(
                requested not in (None, ""),
                "options.parent_storyboard_id needs body.branch_id naming the "
                "branch to fork onto",
                details={"parent_storyboard_id": parent_row["storyboard_id"],
                         "gap": "S06-G10"})
            _text(requested, "branch_id")
            if requested == parent_row["branch_id"]:
                raise ServiceError(
                    400, "INVALID_SCHEMA",
                    "options.parent_storyboard_id must name a storyboard on a "
                    "different branch; forking onto the same branch is a plain "
                    "replan, not a fork",
                    details={"parent_storyboard_id":
                             parent_row["storyboard_id"],
                             "branch_id": requested, "gap": "S06-G10"})
            return requested
        if requested not in (None, ""):
            _text(requested, "branch_id")
            return requested
        if storyboard_id:
            row = self._storyboard(tenant_id, storyboard_id)
            if row is not None:
                return row["branch_id"]
        return breakdown_doc.get("branch_id") or "br_main"

    def _collect_proposals(self, tenant_id: str, body: dict, proposal,
                           scenes: list[dict], *, max_shots: int) -> list[dict]:
        """把 proposal 或请求体里的 `shots` 归一成内部形状。

        两条路径的差别只在来源：S19 的输出必须过信任边界（丢弃 agent 给的
        `shot_lineage_id`、拒绝 `tenant_id` / `project_id` / `series_id`），
        人工分镜的 `shots` 本来就是调用方结构化给出的，但也一样过 schema 校验。
        """
        if proposal is not None:
            raw = proposal.get("shots")
            source = "proposal"
        else:
            raw = body.get("shots")
            source = "inline"
        _require(isinstance(raw, list) and raw,
                 f"{source} must carry a non-empty shots array",
                 details={"source": source})
        shots: list[dict] = []
        for index, item in enumerate(raw):
            item = _obj(item, f"shots[{index}]")
            scope_key = next((key for key in ("tenant_id", "project_id",
                                              "series_id")
                              if key in item), None)
            if scope_key is not None:
                # 422 + 告警（T06-38）。这不是"多给了一个字段"，而是提示里
                # 混进了租户上下文——那意味着这份输出可能属于另一个租户。
                raise ServiceError(
                    422, "AGENT_SUPPLIED_TENANT",
                    f"shots[{index}] carries {scope_key}; the proposal is "
                    f"contaminated and is refused",
                    details={"field": scope_key, "index": index,
                             "gap": "S06-G17"})
            if "shot_lineage_id" in item:
                # §7.3：lineage 是 S06 独占的，agent 给的任何 lineage 都不可信。
                # 丢弃并重铸/继承 —— 记一条本地审计，便于追查污染来源。
                _local_event(
                    self.conn, tenant_id, LOCAL_EVENT_LINEAGE_DISCARDED,
                    str(item.get("shot_id") or f"shots[{index}]"),
                    {"shot_id": item.get("shot_id"),
                     "supplied_shot_lineage_id": item["shot_lineage_id"],
                     "index": index, "source": source},
                    command_id="")
            spec = _validate_shot_spec(item.get("shot_spec"), f"shots[{index}]")
            shots.append({"spec": spec,
                          "order": _int(item.get("shot_order", index + 1),
                                        f"shots[{index}].shot_order", 1),
                          "scene_order": _int(
                              item.get("scene_order", 0),
                              f"shots[{index}].scene_order", 0)})
        return shots

    def _ensure_container(self, *, tenant_id: str, storyboard_id: str | None,
                          breakdown_doc: dict, branch_id: str,
                          series_id: str, project_id: str,
                          created_by: dict) -> tuple[dict, bool]:
        """找到或建立 storyboard 容器。

        `(episode_id, branch_id)` 可以有多个 storyboard（§3.6.1）；不带 ID 时
        取该集该分支下最新的 active 那个，没有才新建。
        """
        if storyboard_id:
            row = self._storyboard(tenant_id, storyboard_id)
            if row is not None:
                if row["branch_id"] != branch_id:
                    raise ServiceError(
                        400, "INVALID_SCHEMA",
                        f"storyboard {storyboard_id} belongs to branch "
                        f"{row['branch_id']}, not {branch_id}",
                        details={"storyboard_id": storyboard_id,
                                 "branch_id": branch_id})
                return row, False
            # 契约只声明 `POST /storyboards/{id}/revisions`，没有"先建容器"
            # 的端点，所以**第一版由调用方给 ID**。这里不替它换 ID：换掉会让
            # 响应与调用方 pin 的 ID 不同，而分镜 ID 是要写进 manifest 的。
            # 同 (episode, branch) 允许多个 storyboard（§3.6.1），所以也不
            # 需要"该集已有容器"的排他判断。
            _require(storyboard_id.startswith(ID_PREFIX["storyboard"])
                     and len(storyboard_id) == len(ID_PREFIX["storyboard"]) + 20,
                     f"storyboard_id must look like "
                     f"{ID_PREFIX['storyboard']}<20 hex>",
                     details={"storyboard_id": storyboard_id})
            now = _now()
            self.conn.execute(
                "INSERT INTO storyboard (tenant_id, storyboard_id, project_id,"
                " series_id, episode_id, branch_id, breakdown_id, lifecycle,"
                " head_revision, sealed_revision, created_by, created_at,"
                " updated_at)"
                " VALUES (?,?,?,?,?,?,?,'active',0,NULL,?,?,?)",
                (tenant_id, storyboard_id, project_id, series_id,
                 breakdown_doc["episode_id"], branch_id,
                 breakdown_doc["breakdown_id"], _jcs(created_by), now, now))
            return self._storyboard(tenant_id, storyboard_id), True
        row = self.conn.execute(
            "SELECT * FROM storyboard WHERE tenant_id=? AND episode_id=? AND"
            " branch_id=? AND lifecycle='active' ORDER BY updated_at DESC,"
            " storyboard_id LIMIT 1",
            (tenant_id, breakdown_doc["episode_id"], branch_id)).fetchone()
        if row is not None:
            return dict(row), False
        storyboard_id = _new_id("storyboard")
        now = _now()
        self.conn.execute(
            "INSERT INTO storyboard (tenant_id, storyboard_id, project_id,"
            " series_id, episode_id, branch_id, breakdown_id, lifecycle,"
            " head_revision, sealed_revision, created_by, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,'active',0,NULL,?,?,?)",
            (tenant_id, storyboard_id, project_id, series_id,
             breakdown_doc["episode_id"], branch_id,
             breakdown_doc["breakdown_id"], _jcs(created_by), now, now))
        return self._storyboard(tenant_id, storyboard_id), True

    def _shots_of_storyboard(self, tenant_id: str, row: dict | None) -> dict:
        """父分镜的镜头（按 `shot_id`）。分支继承的比对基准。

        `shot_revision` 上没有 `shot_lineage_id`（lineage 是身份，只存在
        `shot` 上），而分支继承要读它，所以这里连 `shot` 一起取。
        """
        if row is None:
            return {}
        revision = row["sealed_revision"] or row["head_revision"]
        if not revision:
            return {}
        out: dict[str, dict] = {}
        for item in self.conn.execute(
                "SELECT r.*, s.shot_lineage_id FROM shot_revision AS r"
                " JOIN shot AS s ON s.tenant_id=r.tenant_id AND"
                " s.branch_id=r.branch_id AND s.shot_id=r.shot_id"
                " WHERE r.tenant_id=? AND r.storyboard_id=? AND"
                " r.storyboard_revision=?",
                (tenant_id, row["storyboard_id"], revision)).fetchall():
            out[item["shot_id"]] = dict(item)
        return out

    def _active_shots(self, tenant_id: str, branch_id: str) -> dict:
        """本分支的活动镜头，连同它最新一版的 `dialogue_texts`。

        `dialogue_texts` 只在 `shot_revision` 上，而 `inherit_by_beat_anchor`
        要在它上面找"这一镜从哪一行开始"的锚点（`shot` 表刻意不存内容）。
        所以这里取身份行 + 最新版本的内容列，拼成一份只读视图。
        """
        rows = self.conn.execute(
            "SELECT s.*, r.dialogue_texts FROM shot AS s"
            " JOIN shot_revision AS r ON r.tenant_id=s.tenant_id AND"
            " r.branch_id=s.branch_id AND r.shot_id=s.shot_id AND"
            " r.revision=(SELECT MAX(x.revision) FROM shot_revision AS x"
            "   WHERE x.tenant_id=s.tenant_id AND x.branch_id=s.branch_id"
            "   AND x.shot_id=s.shot_id)"
            " WHERE s.tenant_id=? AND s.branch_id=? AND s.state='active'",
            (tenant_id, branch_id)).fetchall()
        return {row["shot_id"]: dict(row) for row in rows}

    def _retired_shot(self, tenant_id: str, branch_id: str,
                      shot_id: str) -> dict | None:
        """本分支上这个 `shot_id` 的退役身份行（没有则 None）。

        `shot_id` 是永久身份：退役只是把 `state` 改成 `retired`，行还在。
        因此“本分支没见过这个 ID”与“这个 ID 已退役”必须分开判断——后者要
        拒绝，而不是当成可以收下的新标签（S06-G08 / T06-12）。
        """
        row = self.conn.execute(
            "SELECT shot_id, state, shot_lineage_id FROM shot WHERE tenant_id=?"
            " AND branch_id=? AND shot_id=? AND state='retired'",
            (tenant_id, branch_id, shot_id)).fetchone()
        return dict(row) if row is not None else None

    def _check_shot_limit(self, prepared: list[dict], max_shots: int) -> None:
        counts: dict[str, int] = {}
        for shot in prepared:
            scene_id = shot["spec"]["scene_id"]
            counts[scene_id] = counts.get(scene_id, 0) + 1
        over = {scene_id: count for scene_id, count in counts.items()
                if count > max_shots}
        if over:
            raise ServiceError(
                422, "SHOT_LIMIT_EXCEEDED",
                "a scene has more shots than options.max_shots_per_scene",
                details={"limit": max_shots, "scenes": over})

    def _revision_fingerprint(self, prepared: list[dict]) -> str:
        """整版去重指纹：镜头内容指纹按 `shot_id` 排序后拼接。

        **不含顺序**——同一组镜头换顺序是重排，不是新内容（§6.4）。也不含
        `storyboard_id` / `revision`，这样"同一集重导一次得到同样的内容"会被
        认出来（T06-04）。代价是不同 `shot_id` 但内容相同的两版不会被去重，
        这是有意的：`shot_id` 是身份，不是内容。
        """
        encoded = sorted(f"{shot['shot_id']}:{shot['fingerprint']}"
                         for shot in prepared)
        return _sha256_hex("\n".join(encoded))

    def _revision_content_hash(self, *, revision: int, breakdown_doc: dict,
                               script: dict, scenes: list[dict],
                               prepared: list[dict]) -> str:
        """整版 `content_hash`。

        §5.2 只要求它是 `char(64)`，没给算式。这里取
        `sha256(JCS({revision, breakdown_ref, script_ref, scene_ids, shots}) +
        "\\n" + 各镜 content_hash 拼接)`——把每镜的 hash 拼进去，让"只改一镜"
        也必然改变整版 hash；`revision` 参与是因为它就在 revision 行上，两版
        同内容的去重已由 fingerprint 负责。
        """
        body = {
            "revision": revision,
            "breakdown_ref": {"breakdown_id": breakdown_doc["breakdown_id"],
                              "revision": int(breakdown_doc["revision"]),
                              "content_hash": "sha256:" + _bare_digest(
                                  breakdown_doc["content_hash"], "breakdown")},
            "script_ref": {"script_id": script["script_id"],
                           "revision": int(script["revision"]),
                           "content_hash": "sha256:" + _bare_digest(
                               script["content_hash"], "script")},
            "scene_ids": [scene["scene_id"] for scene in scenes],
            "shots": [{"shot_id": shot["shot_id"],
                       "content_hash": shot["content_hash"]}
                      for shot in prepared],
        }
        return _sha256_hex(_jcs(body) + "\n" + "\n".join(
            shot["content_hash"] for shot in prepared))

    def _write_shots(self, *, tenant_id: str, branch_id: str,
                     storyboard_id: str, storyboard_revision: int,
                     prepared: list[dict], produced_by: dict, state: str,
                     sealed: bool, command_id: str, home_cell: str,
                     episode_id: str) -> list[dict]:
        """按场分组写入镜头，并处理 `shot` 身份行的建立/退役。

        §3.4 的顺序是两层的，落库时分成两列：

        - `shot_order`：按场块展平后的**整版序号** 1..N。场块的先后由
          `scene_order` 决定（缺省按镜头列表出现的先后）。
        - `display_shot`：**场内**序号（C7），与 `display_locator.shot` 同值。

        两列都由这里算，不接受调用方给——顺序是一版分镜的排列，不是镜头的
        身份（§1.1）。
        """
        scene_order_map: dict[str, int] = {}
        for shot in prepared:
            spec = shot["spec"]
            scene_order_map.setdefault(spec["scene_id"], len(scene_order_map) + 1)
        grouped: dict[str, list[dict]] = {}
        for shot in prepared:
            grouped.setdefault(shot["spec"]["scene_id"], []).append(shot)
        ordered_scenes = sorted(
            grouped,
            key=lambda scene_id: (scene_order_map[scene_id],
                                  min(item["scene_order"] for item in
                                      grouped[scene_id])))
        flat: list[dict] = []
        for scene_id in ordered_scenes:
            flat.extend(sorted(grouped[scene_id],
                               key=lambda item: (item["scene_order"],
                                                 item["order"])))
        counters: dict[str, int] = {}
        wrote: list[dict] = []
        for index, shot in enumerate(flat):
            spec = shot["spec"]
            scene_id = spec["scene_id"]
            counters[scene_id] = counters.get(scene_id, 0) + 1
            display_shot = counters[scene_id]
            order = index + 1
            locator = dict(spec.get("display_locator") or {})
            locator["shot"] = display_shot
            spec["display_locator"] = locator
            shot_id = shot["shot_id"]
            existing = self.conn.execute(
                "SELECT * FROM shot WHERE tenant_id=? AND branch_id=? AND"
                " shot_id=?", (tenant_id, branch_id, shot_id)).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO shot (tenant_id, branch_id, shot_id,"
                    " shot_lineage_id, storyboard_id, episode_id, scene_id,"
                    " state, origin_shot_id, first_revision, retired_at,"
                    " retire_reason, created_at)"
                    " VALUES (?,?,?,?,?,?,?,'active',?,?,NULL,NULL,?)",
                    (tenant_id, branch_id, shot_id, shot["shot_lineage_id"],
                     storyboard_id, episode_id, scene_id,
                     shot.get("origin_shot_id"),
                     shot["revision"], _now()))
            else:
                # 身份行已存在：lineage 一律不写（列都不出现在 SET 里）。
                # 走到这里只可能是同分支重规划，`_mint_lineage` 应当已经给出
                # 与身份行一致的 lineage。不一致说明铸造逻辑错了——**不能**
                # 用身份行覆盖，否则错误会被静默抹平；而 lineage 列本身也有
                # 触发器禁止改（T06-13），这里只是把错误变成可读的 500。
                if existing["shot_lineage_id"] != shot["shot_lineage_id"]:
                    raise ServiceError(
                        500, "LINEAGE_MISMATCH",
                        f"shot {shot_id} would change lineage from "
                        f"{existing['shot_lineage_id']} to "
                        f"{shot['shot_lineage_id']}; lineage is immutable",
                        details={"shot_id": shot_id,
                                 "stored": existing["shot_lineage_id"],
                                 "minted": shot["shot_lineage_id"]})
                self.conn.execute(
                    "UPDATE shot SET storyboard_id=?, scene_id=? WHERE"
                    " tenant_id=? AND branch_id=? AND shot_id=?",
                    (storyboard_id, scene_id, tenant_id, branch_id, shot_id))
            summary = self._write_shot_revision(
                tenant_id=tenant_id, branch_id=branch_id,
                storyboard_id=storyboard_id,
                storyboard_revision=storyboard_revision, shot=shot,
                revision=shot["revision"], scene_order=scene_order_map[scene_id],
                shot_order=order, display_shot=display_shot,
                produced_by=produced_by,
                state="SEALED" if sealed else "DRAFT",
                sealed_at=_now() if sealed else None)
            wrote.append(summary)
            if sealed:
                self._emit_shot_event(
                    tenant_id=tenant_id, branch_id=branch_id,
                    shot_id=shot_id,
                    shot_lineage_id=shot["shot_lineage_id"],
                    revision=shot["revision"],
                    changed_fields=shot.get("changed_fields", []),
                    previous_revision=shot.get("parent_revision"),
                    content_hash=shot["content_hash"],
                    storyboard_id=storyboard_id,
                    storyboard_revision=storyboard_revision,
                    episode_id=episode_id, command_id=command_id,
                    home_cell=home_cell)
        return wrote

    def _next_shot_revision(self, tenant_id: str, branch_id: str,
                            shot_id: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(revision) AS revision FROM shot_revision WHERE"
            " tenant_id=? AND branch_id=? AND shot_id=?",
            (tenant_id, branch_id, shot_id)).fetchone()
        return int(row["revision"] or 0) + 1

    def _last_shot_revision(self, tenant_id: str, branch_id: str,
                            shot_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(revision) AS revision FROM shot_revision WHERE"
            " tenant_id=? AND branch_id=? AND shot_id=?",
            (tenant_id, branch_id, shot_id)).fetchone()
        if row is None or row["revision"] is None:
            return None
        return int(row["revision"])

    # ---- E03：单镜重写 ---------------------------------------------------

    def revise_shot(self, tenant_id: str, shot_id: str, body: dict, *,
                    branch_id: str | None, command_id: str,
                    request_hash: str, home_cell: str = "cell_a1") -> dict:
        """`POST /shots/{id}/revisions` 的命令外壳。"""
        return self._command(
            tenant_id, command_id,
            lambda: self._revise_shot_body(
                tenant_id, shot_id, body, branch_id=branch_id,
                command_id=command_id, home_cell=home_cell),
            request_hash)

    def _revise_shot_body(self, tenant_id: str, shot_id: str, body: dict, *,
                          branch_id: str | None, command_id: str,
                          home_cell: str = "cell_a1") -> dict:
        """`POST /shots/{id}/revisions`。字段级 `patch`，不是整份替换（§2.5）。

        产出形状（S06-G03）：写一版新的**整场快照** storyboard revision，被改
        镜头在其中拿到新 `shot_revision`；它立即 `SEALED` 并发出唯一登记过的
        那条单镜事件；其余镜头原样复制（内容、lineage、hash 都不变）。storyboard
        revision 自身保持 `DRAFT` 直到 `:seal`——整场还没定稿。

        三条必须成立的语义：

        - `preserve` 与 `constraints.must_preserve` 的命中检查（§3.5.3）。
        - 旧 `shot_revision` 一行不动，仍可读（T06-22）。
        - lineage 沿用（T06-21）：`_mint_lineage` 的第 2 种情形兜住它。
        """
        shot = self._find_shot(tenant_id, shot_id, branch_id)
        branch = shot["branch_id"]
        current = self._latest_shot_revision(tenant_id, branch, shot_id)
        parent_revision = _int(body.get("parent_revision"),
                               "parent_revision", 1)
        if parent_revision != int(current["revision"]):
            # 旧 epoch 的结果晚到就是这条路径（§3.8）：本服务不需要理解 epoch，
            # 只认 parent_revision 是否还是当前值。
            raise ServiceError(
                409, "SHOT_REVISION_CONFLICT",
                f"shot {shot_id} is at revision {current['revision']}, not "
                f"{parent_revision}; reload and retry the patch",
                retryable=True,
                details={"shot_id": shot_id,
                         "parent_revision": parent_revision,
                         "current_revision": int(current["revision"])})

        patch = _obj(body.get("patch"), "patch")
        _check_field_paths(patch)
        preserve = body.get("preserve")
        if preserve is None:
            preserve = ["dialogue", "constraints.must_preserve"]
        for index, item in enumerate(_list(preserve, "preserve")):
            _text(item, f"preserve[{index}]")

        parent_scene = self._revision_row(tenant_id, shot["storyboard_id"],
                                          int(current["storyboard_revision"]))
        prepared, copied = self._prepare_shot_rewrite(
            tenant_id=tenant_id, shot=shot, branch=branch, current=current,
            parent_scene=parent_scene, patch=patch, preserve=preserve,
            touched=_flatten_paths(patch), extra_specs=[])
        return self._write_rewrite_revision(
            tenant_id=tenant_id, shot=shot, branch=branch, current=current,
            parent_scene=parent_scene, prepared=prepared, copied=copied,
            command_id=command_id, home_cell=home_cell, response_extra={},
            proposal_ref={})

    def _prepare_shot_rewrite(self, *, tenant_id: str, shot: dict, branch: str,
                              current: dict, parent_scene: dict, patch: dict,
                              preserve: list, touched: list,
                              extra_specs: list[dict]) -> tuple[list, list]:
        """校验一次改镜（含 `:split` 的第二镜），返回 `(prepared, copied)`。

        顺序有意如此：先做 preserve 判定（最便宜、也最该先报），再叠加 patch，
        最后才回上游核台词——不值得为一个必然被拒的 patch 去读 S04。
        """
        base_spec = _loads(current["shot_spec"], {})
        # preserve 与 must_preserve 分开判：前者是本次命令的临时保护，后者是
        # 镜头自己声明的长期约束。任一违规都足够拒绝。
        #
        # `locked_fields` 也进同一张表：方案 §3.5.3 的伪码只列了 preserve 与
        # must_preserve，但 §9.3 T06-19 明确要求"patch 改了 locked_fields 里的
        # 字段 → 422 MUST_PRESERVE_VIOLATION"。不把 locked_fields 当作 patch
        # 守卫，`locked_fields` 就只是"不许从列表里删掉"的记号（那由
        # `_check_locked_monotonic` 管），一个 patch 仍能随手改掉被锁的字段，
        # "锁定"也就名不副实。这里按 T06-19 收口，并把冲突记在方案缺口里。
        constraints = base_spec.get("constraints") or {}
        guards = [("preserve", list(preserve)),
                  ("constraints.must_preserve",
                   list(constraints.get("must_preserve") or [])),
                  ("constraints.locked_fields",
                   list(constraints.get("locked_fields") or []))]
        for source, items in guards:
            for guard in items:
                hit = [field for field in touched
                       if _field_path_matches(field, guard)]
                if hit:
                    raise ServiceError(
                        422, "MUST_PRESERVE_VIOLATION",
                        f"patch touches {guard!r}, which is protected by "
                        f"{source}",
                        details={"field": guard, "source": source,
                                 "changed_fields": sorted(set(hit))})

        merged = _deep_merge(base_spec, patch)
        # 身份字段不由 patch 决定：它们描述这一镜"在哪"，改位置是拆/并镜，
        # 不是改镜。顺序同理——顺序归 `shot-order` 命令。
        merged["shot_id"] = current["shot_id"]
        merged["scene_id"] = current["scene_id"]
        merged["episode_id"] = shot["episode_id"]
        merged["revision"] = int(current["revision"]) + 1
        merged["display_locator"] = dict(base_spec.get("display_locator") or {})
        _check_locked_monotonic(base_spec, merged)
        spec = _validate_shot_spec(merged, "shot_spec")

        breakdown_doc = self._load_breakdown(tenant_id, {
            "breakdown_id": parent_scene["breakdown_id"],
            "revision": int(parent_scene["breakdown_revision"]),
            "content_hash": parent_scene["breakdown_hash"]})
        script = self._load_script(tenant_id, {
            "script_id": parent_scene["script_id"],
            "revision": int(parent_scene["script_revision"]),
            "content_hash": parent_scene["script_hash"]})
        lines_by_id = _line_index(script)
        refs = check_dialogue_bytes(spec=spec, lines_by_id=lines_by_id,
                                    script=script)
        feasibility = check_feasibility(spec)
        prepared = [self._prepared_shot(
            spec, refs, feasibility, lineage=shot["shot_lineage_id"],
            revision=int(current["revision"]) + 1,
            parent_revision=int(current["revision"]),
            changed_fields=sorted(set(touched)))]
        # `:split` 的第二镜：它与第一镜共同覆盖该场，且不能重复覆盖同一行
        # （整句 + 不重叠两条同时成立）。
        for extra in extra_specs:
            extra_refs = check_dialogue_bytes(spec=extra,
                                              lines_by_id=lines_by_id,
                                              script=script)
            prepared.append(self._prepared_shot(
                extra, extra_refs, check_feasibility(extra),
                lineage=None, revision=1, parent_revision=None,
                changed_fields=["shot_id"]))

        _check_asset_bindings(tenant_id, prepared, self._assets)

        scenes = self._breakdown_scenes(
            breakdown_doc, _loads(parent_scene["scene_ids"], []))
        scene = next(item for item in scenes
                     if item["scene_id"] == current["scene_id"])
        # 局部改镜只查"这一场、这次请求里的镜头"是否重复覆盖；其余镜头不在
        # 请求里，用整场分母判漏会把它们全判成缺失。
        check_line_coverage(scenes=[scene],
                            shots=[{"spec": item["spec"]} for item in prepared],
                            lines=script["lines"], strict=False)
        copied = self._snapshot_shots(
            tenant_id=tenant_id, branch=branch,
            storyboard_id=shot["storyboard_id"],
            source_revision=int(current["storyboard_revision"]),
            skip_shot_ids={item["shot_id"] for item in prepared})
        return prepared, copied

    def _prepared_shot(self, spec: dict, refs: list, feasibility: dict, *,
                       lineage, revision: int, parent_revision, changed_fields,
                       sealed: bool = True) -> dict:
        return {"shot_id": spec["shot_id"], "spec": spec, "refs": refs,
                "feasibility": feasibility,
                "content_hash": shot_content_hash(spec, refs),
                "shot_lineage_id": lineage, "revision": revision,
                "parent_revision": parent_revision,
                "changed_fields": changed_fields,
                "scene_id": spec["scene_id"], "sealed": sealed,
                "fingerprint": shot_fingerprint(spec, refs)}

    def _snapshot_shots(self, *, tenant_id: str, branch: str,
                        storyboard_id: str, source_revision: int,
                        skip_shot_ids: set[str]) -> list[dict]:
        """把上一版的其余镜头准备成"原样复制"的写入项。

        复制会给每镜新建一行 `shot_revision`（内容 hash 与 lineage 都不变）。
        这是"一版分镜是一份完整快照"的代价：不复制就得让下游自己回拼多版，
        而 §4.1 的响应契约是一版完整分镜。
        """
        out: list[dict] = []
        rows = self.conn.execute(
            "SELECT * FROM shot_revision WHERE tenant_id=? AND branch_id=? AND"
            " storyboard_id=? AND storyboard_revision=? ORDER BY shot_order",
            (tenant_id, branch, storyboard_id, source_revision)).fetchall()
        for row in rows:
            row = dict(row)
            if row["shot_id"] in skip_shot_ids:
                continue
            shot_row = self.conn.execute(
                "SELECT * FROM shot WHERE tenant_id=? AND branch_id=? AND"
                " shot_id=?", (tenant_id, branch, row["shot_id"])).fetchone()
            if shot_row is None or shot_row["state"] != "active":
                # 已退役的镜头不出现在新版本（§3.6.1）。
                continue
            out.append({
                "shot_id": row["shot_id"],
                "spec": _loads(row["shot_spec"], {}),
                "refs": self._refs_of(tenant_id, branch, row["shot_id"],
                                      int(row["revision"])),
                "feasibility": _loads(row["feasibility"], {}),
                "content_hash": row["content_hash"],
                "shot_lineage_id": shot_row["shot_lineage_id"],
                "revision": self._next_shot_revision(tenant_id, branch,
                                                    row["shot_id"]),
                "parent_revision": int(row["revision"]),
                "changed_fields": [], "scene_id": row["scene_id"],
                "sealed": False, "fingerprint": row["content_hash"],
                "shot_order": int(row["shot_order"]),
            })
        return out

    def _write_rewrite_revision(self, *, tenant_id: str, shot: dict,
                                branch: str, current: dict, parent_scene: dict,
                                prepared: list, copied: list,
                                command_id: str, home_cell: str,
                                response_extra: dict,
                                proposal_ref: dict) -> dict:
        """把一次改镜落成一版新的整场快照，并发出被改镜头的单镜事件。

        E03 与 `:split` 都走这里：它们的写入形状完全一样——新 storyboard
        revision + 被改镜头的新 revision（立即 SEALED）+ 其余镜头原样复制。
        """
        storyboard_row = self._require_storyboard(tenant_id,
                                                  shot["storyboard_id"])
        produced_by = _produced_by(None, None, proposal_given=False)
        next_revision = int(storyboard_row["head_revision"]) + 1
        cursor = self.conn.execute(
            "UPDATE storyboard SET head_revision=?, updated_at=? WHERE"
            " tenant_id=? AND storyboard_id=? AND head_revision=?",
            (next_revision, _now(), tenant_id, shot["storyboard_id"],
             int(storyboard_row["head_revision"])))
        if cursor.rowcount != 1:
            raise ServiceError(
                409, "STORYBOARD_REVISION_CONFLICT",
                "storyboard head moved while this command was running",
                retryable=True, details={"shot_id": current["shot_id"]})

        # 被改的镜头回到它原来的位置，其余镜头按原顺序铺开。
        anchor = int(current["shot_order"])
        before = [item for item in copied if item["shot_order"] < anchor]
        after = [item for item in copied if item["shot_order"] >= anchor]
        sequence = before + prepared + after

        changed = sorted(set(_loads(parent_scene["changed_shot_ids"], []))
                         | {item["shot_id"] for item in prepared})
        hashes = sorted(({"shot_id": item["shot_id"],
                          "content_hash": item["content_hash"]}
                         for item in sequence),
                        key=lambda entry: entry["shot_id"])
        revision_hash = _sha256_hex(_jcs({
            "storyboard_id": shot["storyboard_id"],
            "revision": next_revision,
            "parent_revision": int(parent_scene["revision"]),
            "changed_shot_ids": changed,
            "shots": hashes}))
        fingerprint = _sha256_hex(_jcs({
            "parent": int(parent_scene["revision"]),
            "changed": changed,
            "shots": hashes}))
        self.conn.execute(
            "INSERT INTO storyboard_revision (tenant_id, storyboard_id,"
            " revision, state, episode_id, branch_id, breakdown_id,"
            " breakdown_revision, breakdown_hash, script_id, script_revision,"
            " script_hash, binding_set_id, binding_set_revision,"
            " binding_set_hash, context_pack_id, context_pack_hash,"
            " context_coverage, policy_ref, proposal_ref, options, produced_by,"
            " parent_revision, scene_ids, shot_count, changed_shot_ids,"
            " retired_shot_ids, lineage_mints, content_hash,"
            " content_fingerprint, sealed_at, created_at)"
            " VALUES (?,?,?,'DRAFT',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,?,NULL,?)",
            (tenant_id, shot["storyboard_id"], next_revision,
             parent_scene["episode_id"], branch, parent_scene["breakdown_id"],
             int(parent_scene["breakdown_revision"]),
             parent_scene["breakdown_hash"], parent_scene["script_id"],
             int(parent_scene["script_revision"]), parent_scene["script_hash"],
             parent_scene["binding_set_id"],
             parent_scene["binding_set_revision"],
             parent_scene["binding_set_hash"], parent_scene["context_pack_id"],
             parent_scene["context_pack_hash"], parent_scene["context_coverage"],
             parent_scene["policy_ref"], _jcs(proposal_ref),
             parent_scene["options"], _jcs(produced_by),
             int(parent_scene["revision"]), parent_scene["scene_ids"],
             len(sequence), _dumps(changed),
             parent_scene["retired_shot_ids"],
             _dumps([{"shot_id": item["shot_id"],
                      "minted": item["shot_lineage_id"] is not None
                      and item.get("minted", False)} for item in prepared]),
             revision_hash, fingerprint, _now()))

        summaries = []
        display_counters: dict[str, int] = {}
        for index, item in enumerate(sequence):
            scene_id = item["scene_id"]
            display_counters[scene_id] = display_counters.get(scene_id, 0) + 1
            summaries.append(self._write_shot_revision(
                tenant_id=tenant_id, branch_id=branch,
                storyboard_id=shot["storyboard_id"],
                storyboard_revision=next_revision, shot=item,
                revision=item["revision"], scene_order=0,
                shot_order=index + 1,
                display_shot=display_counters[scene_id],
                produced_by=produced_by,
                state="SEALED" if item.get("sealed") else "DRAFT",
                sealed_at=_now() if item.get("sealed") else None))
        by_shot = {summary["shot_id"]: summary for summary in summaries}
        for item in prepared:
            self._emit_shot_event(
                tenant_id=tenant_id, branch_id=branch,
                shot_id=item["shot_id"],
                shot_lineage_id=item["shot_lineage_id"],
                revision=item["revision"],
                changed_fields=item["changed_fields"],
                previous_revision=item["parent_revision"],
                content_hash=item["content_hash"],
                storyboard_id=shot["storyboard_id"],
                storyboard_revision=next_revision,
                episode_id=parent_scene["episode_id"],
                command_id=command_id, home_cell=home_cell)
            by_shot[item["shot_id"]]["emitted"] = True
        _local_event(
            self.conn, tenant_id, LOCAL_EVENT_REVISION_CREATED,
            shot["storyboard_id"],
            {"storyboard_id": shot["storyboard_id"],
             "revision": next_revision, "state": "DRAFT",
             "single_shot": len(prepared) == 1,
             "changed_shot_ids": [item["shot_id"] for item in prepared],
             "shot_lineages": summaries,
             "content_hash": "sha256:" + revision_hash},
            command_id=command_id)
        result = {"storyboard_id": shot["storyboard_id"],
                  "storyboard_revision": next_revision,
                  "storyboard_state": "DRAFT",
                  "content_hash": _prefixed(revision_hash),
                  "shot_lineages": summaries}
        result.update(response_extra)
        return result

    def _refs_of(self, tenant_id: str, branch: str, shot_id: str,
                 revision: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM shot_dialogue_ref WHERE tenant_id=? AND branch_id=?"
            " AND shot_id=? AND revision=? ORDER BY line_id",
            (tenant_id, branch, shot_id, revision)).fetchall()
        return [{"script_id": row["script_id"],
                 "revision": int(row["script_revision"]),
                 "line_id": row["line_id"],
                 "text_hash": row["text_hash"],
                 "text": row["text_copy"],
                 "emotion": row["emotion"]} for row in rows]

    def _revision_row(self, tenant_id: str, storyboard_id: str,
                      revision: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM storyboard_revision WHERE tenant_id=? AND"
            " storyboard_id=? AND revision=?",
            (tenant_id, storyboard_id, int(revision))).fetchone()
        if row is None:  # pragma: no cover - FK 保证不会发生
            raise ServiceError(500, "INTERNAL",
                               f"storyboard revision {revision} is missing")
        return dict(row)

    # ---- 退役 / 拆分 -----------------------------------------------------

    def retire_shot(self, tenant_id: str, shot_id: str, body: dict, *,
                    branch_id: str | None, command_id: str,
                    request_hash: str) -> dict:
        """`POST /shots/{id}:retire` 的命令外壳。"""
        return self._command(
            tenant_id, command_id,
            lambda: self._retire_shot_body(
                tenant_id, shot_id, body, branch_id=branch_id,
                command_id=command_id),
            request_hash)

    def _retire_shot_body(self, tenant_id: str, shot_id: str, body: dict, *,
                          branch_id: str | None, command_id: str) -> dict:
        """`POST /shots/{id}:retire`。

        `WHERE state='active'` 的 0 行判定（§6.2）：并发两次退役一镜只有一次
        成功，第二次 409——静默 no-op 会让调用方以为版本已经更新。
        退役不改已有 revision：旧版本里这一镜还在（历史是历史），只是它不再
        出现在**新**版本里（§3.6.1）。
        """
        shot = self._find_shot(tenant_id, shot_id, branch_id)
        reason = body.get("reason") or "removed"
        _enum(reason, "reason", ("removed", "merged", "replaced"))
        cursor = self.conn.execute(
            "UPDATE shot SET state='retired', retired_at=?, retire_reason=?"
            " WHERE tenant_id=? AND branch_id=? AND shot_id=? AND"
            " state='active'",
            (_now(), reason, tenant_id, shot["branch_id"], shot_id))
        if cursor.rowcount != 1:
            raise ServiceError(
                409, "SHOT_REVISION_CONFLICT",
                f"shot {shot_id} is already retired",
                details={"shot_id": shot_id, "state": shot["state"]})
        lineage_retired = self._retire_lineage_if_idle(
            tenant_id, shot["shot_lineage_id"])
        _local_event(
            self.conn, tenant_id, LOCAL_EVENT_RETIRED, shot_id,
            {"shot_id": shot_id, "shot_lineage_id": shot["shot_lineage_id"],
             "branch_id": shot["branch_id"], "reason": reason,
             "lineage_retired": lineage_retired}, command_id=command_id)
        return {"shot_id": shot_id, "state": "retired", "reason": reason,
                "shot_lineage_id": shot["shot_lineage_id"],
                "lineage_retired": lineage_retired}

    def split_shot(self, tenant_id: str, shot_id: str, body: dict, *,
                   branch_id: str | None, command_id: str,
                   request_hash: str, home_cell: str = "cell_a1") -> dict:
        """`POST /shots/{id}:split` 的命令外壳。"""
        return self._command(
            tenant_id, command_id,
            lambda: self._split_shot_body(
                tenant_id, shot_id, body, branch_id=branch_id,
                command_id=command_id, home_cell=home_cell),
            request_hash)

    def _split_shot_body(self, tenant_id: str, shot_id: str, body: dict, *,
                         branch_id: str | None, command_id: str,
                         home_cell: str = "cell_a1") -> dict:
        """`POST /shots/{id}:split`。一镜拆两镜（显式操作）。

        `shot_lineage_id` 的规则（§3.2.3）：**原镜保留 lineage，新镜新铸**。
        共享 lineage 会让两镜的尝试计数互相污染，"这一镜已经试了 8 次"就分不清
        是拆前还是拆后。

        新镜的 `shot_spec` 由请求体给出（调用方按 S06 在 422 里建议的
        `split.at_line_ids` 填好）；两镜各自必须持有**完整**台词——把一句拆成
        两半是违规（T06-30），`check_dialogue_bytes` 会拒绝。
        """
        shot = self._find_shot(tenant_id, shot_id, branch_id)
        branch = shot["branch_id"]
        current = self._latest_shot_revision(tenant_id, branch, shot_id)
        parent_revision = _int(body.get("parent_revision"),
                               "parent_revision", 1)
        if parent_revision != int(current["revision"]):
            # T06-05：并发两次 :split 只有一个成功。第二次在这里就撞上，
            # 因为第一次已经把这一镜的 revision 推高了。
            raise ServiceError(
                409, "SHOT_REVISION_CONFLICT",
                f"shot {shot_id} is at revision {current['revision']}, not "
                f"{parent_revision}; a concurrent split or revise already won",
                retryable=True,
                details={"shot_id": shot_id,
                         "parent_revision": parent_revision,
                         "current_revision": int(current["revision"])})

        raw_new = _obj(body.get("new_shot"), "new_shot")
        new_spec = _validate_shot_spec(
            raw_new["shot_spec"] if "shot_spec" in raw_new else raw_new,
            "new_shot")
        if new_spec["shot_id"] == shot_id:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "new_shot.shot_id must differ from the original")
        if new_spec["scene_id"] != current["scene_id"]:
            raise ServiceError(
                422, "SCENE_NOT_IN_BREAKDOWN",
                "a split stays inside the original shot's scene",
                details={"scene_id": new_spec["scene_id"],
                         "expected": current["scene_id"]})
        clash = self.conn.execute(
            "SELECT 1 FROM shot WHERE tenant_id=? AND branch_id=? AND"
            " shot_id=?", (tenant_id, branch, new_spec["shot_id"])).fetchone()
        if clash is not None:
            raise ServiceError(
                422, "UNKNOWN_SHOT_ID",
                f"new_shot.shot_id {new_spec['shot_id']} already exists; a "
                f"split never reuses an id (S06-G08)",
                details={"shot_id": new_spec["shot_id"], "gap": "S06-G08"})

        parent_scene = self._revision_row(tenant_id, shot["storyboard_id"],
                                          int(current["storyboard_revision"]))
        original_patch = body.get("original_patch") or {}
        _require(isinstance(original_patch, dict),
                 "original_patch must be an object",
                 details={"field": "original_patch"})
        _check_field_paths(original_patch)
        preserve = body.get("preserve")
        if preserve is None:
            preserve = ["constraints.must_preserve"]
        prepared, copied = self._prepare_shot_rewrite(
            tenant_id=tenant_id, shot=shot, branch=branch, current=current,
            parent_scene=parent_scene, patch=original_patch,
            preserve=preserve,
            touched=sorted(set(_flatten_paths(original_patch))),
            extra_specs=[new_spec])

        # 新镜铸新 lineage；原镜沿用（`_mint_lineage` 情形 2 已给出）。
        storyboard_row = self._require_storyboard(tenant_id,
                                                  shot["storyboard_id"])
        minted = self._mint_lineage(
            tenant_id, storyboard_row["series_id"], branch_id=branch,
            parent_branch_id=branch, proposal_shot_id=None,
            parent_shots={}, existing_shots={})
        prepared[1]["shot_lineage_id"] = minted["shot_lineage_id"]
        prepared[1]["minted"] = True
        # 新镜从第 1 版起算，`shot_spec.revision` 与列上的 revision 必须同值
        # （否则同一件事有两个版本号）。
        prepared[1]["spec"]["revision"] = 1
        # 新镜身份行先建：`shot_revision` 有指向 `shot` 的外键。
        self.conn.execute(
            "INSERT INTO shot (tenant_id, branch_id, shot_id, shot_lineage_id,"
            " storyboard_id, episode_id, scene_id, state, origin_shot_id,"
            " first_revision, retired_at, retire_reason, created_at)"
            " VALUES (?,?,?,?,?,?,?,'active',?,?,NULL,NULL,?)",
            (tenant_id, branch, new_spec["shot_id"],
             minted["shot_lineage_id"], shot["storyboard_id"],
             shot["episode_id"], current["scene_id"], shot_id,
             prepared[1]["revision"], _now()))

        result = self._write_rewrite_revision(
            tenant_id=tenant_id, shot=shot, branch=branch, current=current,
            parent_scene=parent_scene, prepared=prepared, copied=copied,
            command_id=command_id, home_cell=home_cell,
            proposal_ref={},
            response_extra={
                "shot_id": shot_id,
                "shot_lineage_id": prepared[0]["shot_lineage_id"],
                "revision": prepared[0]["revision"],
                "new_shot_id": new_spec["shot_id"],
                "new_shot_lineage_id": minted["shot_lineage_id"],
                "new_revision": prepared[1]["revision"],
            })
        _local_event(
            self.conn, tenant_id, LOCAL_EVENT_SPLIT, shot_id,
            {"shot_id": shot_id,
             "original_shot_lineage_id": prepared[0]["shot_lineage_id"],
             "new_shot_id": new_spec["shot_id"],
             "new_shot_lineage_id": minted["shot_lineage_id"],
             "branch_id": branch,
             "storyboard_revision": result["storyboard_revision"]},
            command_id=command_id)
        return result

    # ---- 冻结 / 重排 -----------------------------------------------------

    def seal(self, tenant_id: str, storyboard_id: str, revision: int,
             body: dict, *, command_id: str, request_hash: str,
             home_cell: str = "cell_a1") -> dict:
        """`POST /storyboards/{id}/revisions/{rev}:seal` 的命令外壳。"""
        return self._command(
            tenant_id, command_id,
            lambda: self._seal_body(
                tenant_id, storyboard_id, revision, body,
                command_id=command_id, home_cell=home_cell),
            request_hash)

    def _seal_body(self, tenant_id: str, storyboard_id: str, revision: int,
                   body: dict, *, command_id: str,
                   home_cell: str = "cell_a1") -> dict:
        """`POST /storyboards/{id}/revisions/{rev}:seal`（§6.2）。

        CAS：`WHERE state='DRAFT'`，0 行即 409。冻结时把本版里仍是 `DRAFT` 的
        镜头版本一起冻结并各发一条单镜事件；已经 `SEALED` 的（E03 改出来的那
        一镜）**不重复发**——同一 `(aggregate_id, sequence)` 发两次会让下游的
        去重键吃掉第二次，而"改过"这件事只该被知道一次。

        整场事件不登记进 outbox（§11 C3），写本地审计，字段与 §4.2 的 `data`
        对齐（T06-43 要从这里构造 `AttemptCounterKey`）。
        """
        row = self._revision_row(tenant_id, storyboard_id, int(revision))
        now = _now()
        cursor = self.conn.execute(
            "UPDATE storyboard_revision SET state='SEALED', sealed_at=?"
            " WHERE tenant_id=? AND storyboard_id=? AND revision=? AND"
            " state='DRAFT'", (now, tenant_id, storyboard_id, int(revision)))
        if cursor.rowcount != 1:
            raise ServiceError(
                409, "REVISION_NOT_DRAFT",
                f"revision {revision} is {row['state']}, not DRAFT",
                details={"storyboard_id": storyboard_id, "revision": revision,
                         "state": row["state"]})

        shot_rows = [dict(item) for item in self.conn.execute(
            "SELECT * FROM shot_revision WHERE tenant_id=? AND branch_id=? AND"
            " storyboard_id=? AND storyboard_revision=? ORDER BY shot_order",
            (tenant_id, row["branch_id"], storyboard_id,
             int(revision))).fetchall()]
        changed_ids = _loads(row["changed_shot_ids"], [])
        summaries = []
        for shot_row in shot_rows:
            lineage = self.conn.execute(
                "SELECT shot_lineage_id FROM shot WHERE tenant_id=? AND"
                " branch_id=? AND shot_id=?",
                (tenant_id, row["branch_id"],
                 shot_row["shot_id"])).fetchone()
            shot_lineage_id = lineage["shot_lineage_id"] if lineage else ""
            if shot_row["state"] == "DRAFT":
                self.conn.execute(
                    "UPDATE shot_revision SET state='SEALED', sealed_at=?"
                    " WHERE tenant_id=? AND branch_id=? AND shot_id=? AND"
                    " revision=? AND state='DRAFT'",
                    (now, tenant_id, row["branch_id"], shot_row["shot_id"],
                     int(shot_row["revision"])))
                self._emit_shot_event(
                    tenant_id=tenant_id, branch_id=row["branch_id"],
                    shot_id=shot_row["shot_id"],
                    shot_lineage_id=shot_lineage_id,
                    revision=int(shot_row["revision"]),
                    # 冻结一条从未被 E03 改过的镜头，`changed_fields` 是空的：
                    # 它相对上一版没有字段变化，只是从 DRAFT 变成 SEALED。
                    changed_fields=[],
                    previous_revision=shot_row["parent_revision"],
                    content_hash=shot_row["content_hash"],
                    storyboard_id=storyboard_id,
                    storyboard_revision=int(revision),
                    episode_id=row["episode_id"], command_id=command_id,
                    home_cell=home_cell)
            summaries.append({
                "shot_id": shot_row["shot_id"],
                "shot_lineage_id": shot_lineage_id,
                "revision": int(shot_row["revision"]),
                "content_hash": _prefixed(shot_row["content_hash"])})

        superseded = [dict(item) for item in self.conn.execute(
            "SELECT revision FROM storyboard_revision WHERE tenant_id=? AND"
            " storyboard_id=? AND state='SEALED' AND revision<>?",
            (tenant_id, storyboard_id, int(revision))).fetchall()]
        for item in superseded:
            self.conn.execute(
                "UPDATE storyboard_revision SET state='SUPERSEDED' WHERE"
                " tenant_id=? AND storyboard_id=? AND revision=? AND"
                " state='SEALED'",
                (tenant_id, storyboard_id, int(item["revision"])))
            _local_event(
                self.conn, tenant_id, LOCAL_EVENT_SUPERSEDED, storyboard_id,
                {"storyboard_id": storyboard_id,
                 "revision": int(item["revision"]),
                 "superseded_by": int(revision)}, command_id=command_id)
        self.conn.execute(
            "UPDATE storyboard SET sealed_revision=?, updated_at=? WHERE"
            " tenant_id=? AND storyboard_id=?",
            (int(revision), now, tenant_id, storyboard_id))

        _local_event(
            self.conn, tenant_id, LOCAL_EVENT_SEALED, storyboard_id,
            {"storyboard_id": storyboard_id, "revision": int(revision),
             "state": "SEALED", "episode_id": row["episode_id"],
             "branch_id": row["branch_id"],
             "breakdown_ref": {"breakdown_id": row["breakdown_id"],
                               "revision": int(row["breakdown_revision"]),
                               "content_hash": _prefixed(
                                   row["breakdown_hash"])},
             "shot_count": int(row["shot_count"]),
             "changed_shot_ids": changed_ids,
             "retired_shot_ids": _loads(row["retired_shot_ids"], []),
             "shot_lineages": summaries,
             "content_hash": _prefixed(row["content_hash"]),
             "occurred_at": now}, command_id=command_id)
        return {"storyboard_id": storyboard_id, "revision": int(revision),
                "state": "SEALED", "sealed_at": now,
                "shot_lineages": summaries,
                "superseded_revisions": [int(item["revision"])
                                         for item in superseded]}

    def reorder(self, tenant_id: str, storyboard_id: str, revision: int,
                body: dict, *, command_id: str, request_hash: str) -> dict:
        """`POST /storyboards/{id}/revisions/{rev}/shot-order` 的命令外壳。"""
        return self._command(
            tenant_id, command_id,
            lambda: self._reorder_body(
                tenant_id, storyboard_id, revision, body,
                command_id=command_id),
            request_hash)

    def _reorder_body(self, tenant_id: str, storyboard_id: str, revision: int,
                      body: dict, *, command_id: str) -> dict:
        """`POST /storyboards/{id}/revisions/{rev}/shot-order`（§3.4）。

        与 S05 同构：**只改顺序不改内容**、必须提交完整列表、不接受 delta、
        跨场不能重排（只允许整场块移动），因此 `content_hash` 不变（T06-47）。

        SQLite 没有 `DEFERRABLE INITIALLY DEFERRED`，所以"先整体加偏移再重写
        1..N"完成一次没有中间冲突的重排（S06-G05）。
        """
        row = self._revision_row(tenant_id, storyboard_id, int(revision))
        if row["state"] in ("SUPERSEDED", "WITHDRAWN"):
            raise ServiceError(
                409, "REVISION_NOT_ACTIVE",
                f"revision {revision} is {row['state']}; its order is history",
                details={"storyboard_id": storyboard_id, "revision": revision,
                         "state": row["state"]})
        wanted = body.get("shot_order")
        _require(isinstance(wanted, list),
                 "shot_order must be a complete array of shot_id",
                 details={"field": "shot_order"})
        for index, item in enumerate(wanted):
            _text(item, f"shot_order[{index}]")
        if len(set(wanted)) != len(wanted):
            raise ServiceError(400, "INVALID_SCHEMA",
                               "shot_order repeats a shot_id",
                               details={"field": "shot_order"})

        rows = [dict(item) for item in self.conn.execute(
            "SELECT shot_id, scene_id, shot_order, shot_spec FROM shot_revision"
            " WHERE tenant_id=? AND branch_id=? AND storyboard_id=? AND"
            " storyboard_revision=? ORDER BY shot_order",
            (tenant_id, row["branch_id"], storyboard_id,
             int(revision))).fetchall()]
        current = [item["shot_id"] for item in rows]
        if set(wanted) != set(current) or len(wanted) != len(current):
            raise ServiceError(
                400, "INVALID_SCHEMA",
                "shot_order must be the complete list for this storyboard "
                "revision; S06 does not accept deltas",
                details={"field": "shot_order",
                         "missing": sorted(set(current) - set(wanted)),
                         "unknown": sorted(set(wanted) - set(current)),
                         "expected_count": len(current),
                         "received_count": len(wanted)})
        scene_of = {item["shot_id"]: item["scene_id"] for item in rows}
        # 两层顺序：场块之间按"该场第一次出现的位置"，场内按提交顺序。一个场
        # 的镜头被拆成两段就意味着跨场混排——拒绝，因为场块是 EDL 的段落。
        seen_scenes: list[str] = []
        last = None
        for shot_id in wanted:
            scene_id = scene_of[shot_id]
            if scene_id != last:
                if scene_id in seen_scenes:
                    raise ServiceError(
                        400, "INVALID_SCHEMA",
                        "shot_order interleaves scenes; the order is two-tier "
                        "(scene block, then shot inside the block)",
                        details={"field": "shot_order", "scene_id": scene_id})
                seen_scenes.append(scene_id)
                last = scene_id
        if seen_scenes != list(dict.fromkeys(
                item["scene_id"] for item in rows)):
            raise ServiceError(
                400, "INVALID_SCHEMA",
                "shot_order moves a scene block relative to another scene; "
                "cross-scene ordering is S05's, not S06's",
                details={"field": "shot_order",
                         "received_scene_order": seen_scenes,
                         "expected_scene_order": list(dict.fromkeys(
                             item["scene_id"] for item in rows))})

        self.conn.execute(
            "UPDATE shot_revision SET shot_order = shot_order + ?,"
            " display_shot = display_shot + ? WHERE tenant_id=? AND branch_id=?"
            " AND storyboard_id=? AND storyboard_revision=?",
            (ORDER_OFFSET, ORDER_OFFSET, tenant_id, row["branch_id"],
             storyboard_id, int(revision)))
        counters: dict[str, int] = {}
        specs = {item["shot_id"]: _loads(item["shot_spec"], {})
                 for item in rows}
        for index, shot_id in enumerate(wanted):
            scene_id = scene_of[shot_id]
            counters[scene_id] = counters.get(scene_id, 0) + 1
            display_shot = counters[scene_id]
            # `display_shot` 列与 `shot_spec.display_locator.shot` 必须同值
            # （§5.5 / C7）：读响应、EDL、S41 都从 `display_locator` 取序号。
            # 只改列不改文档的话，同一行里两个"场内序号"会打架，而且
            # `content_hash` 不包含 `display_locator`，所以改它不会让版本内容
            # 变化——这正是 §1.1 "位置是显示值"的意思。
            #
            # 在 Python 侧重序列化而不是 `json_set`：`json_set` 不保证键序，
            # 而 `shot_spec` 落的字节是 `_jcs` 输出，抖动会让触发器里的整串
            # 比较失效（也因此触发器才会先摘掉 `display_locator`）。
            spec = dict(specs[shot_id])
            spec["display_locator"] = {**dict(
                spec.get("display_locator") or {}), "shot": display_shot}
            self.conn.execute(
                "UPDATE shot_revision SET shot_order=?, display_shot=?,"
                " shot_spec=? WHERE tenant_id=? AND branch_id=? AND"
                " storyboard_id=? AND storyboard_revision=? AND shot_id=?",
                (index + 1, display_shot, _jcs(spec), tenant_id,
                 row["branch_id"], storyboard_id, int(revision), shot_id))
        _local_event(
            self.conn, tenant_id, LOCAL_EVENT_REORDERED, storyboard_id,
            {"storyboard_id": storyboard_id, "revision": int(revision),
             "shot_order": wanted}, command_id=command_id)
        document = self.get_storyboard_revision(tenant_id, storyboard_id,
                                                int(revision))
        document["reordered"] = True
        return document

    # ---- 查询 ------------------------------------------------------------

    def get_storyboard(self, tenant_id: str, storyboard_id: str) -> dict:
        """`GET /storyboards/{id}`：容器元数据 + head。不返回镜头正文。"""
        row = self._require_storyboard(tenant_id, storyboard_id)
        revisions = self.conn.execute(
            "SELECT revision, state, shot_count, created_at, sealed_at FROM"
            " storyboard_revision WHERE tenant_id=? AND storyboard_id=?"
            " ORDER BY revision", (tenant_id, storyboard_id)).fetchall()
        return {
            "storyboard_id": row["storyboard_id"],
            "project_id": row["project_id"],
            "series_id": row["series_id"],
            "episode_id": row["episode_id"],
            "branch_id": row["branch_id"],
            "breakdown_id": row["breakdown_id"],
            "lifecycle": row["lifecycle"],
            "head_revision": int(row["head_revision"]),
            "sealed_revision": row["sealed_revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "revisions": [{"revision": int(item["revision"]),
                           "state": item["state"],
                           "shot_count": int(item["shot_count"]),
                           "created_at": item["created_at"],
                           "sealed_at": item["sealed_at"]}
                          for item in revisions],
            "authoritative_source": AUTHORITATIVE_SOURCE,
            "projection_watermark": self._watermark(tenant_id),
            "contract_gaps": CONTRACT_GAPS,
        }

    def get_storyboard_revision(self, tenant_id: str, storyboard_id: str,
                                revision: int) -> dict:
        """`GET /storyboards/{id}/revisions/{revision}`：一版完整分镜。

        `s06_meta` 一律带 `authoritative_source`：任何读到这里的人都不该把
        这份快照当成可以写回去的权威（§4.3）。
        """
        row = self.conn.execute(
            "SELECT * FROM storyboard_revision WHERE tenant_id=? AND"
            " storyboard_id=? AND revision=?",
            (tenant_id, storyboard_id, int(revision))).fetchone()
        if row is None:
            # 跨租户同码（E28）：别人的版本在这里就是不存在。
            raise ServiceError(
                404, "NOT_FOUND",
                f"storyboard {storyboard_id} revision {revision} not found")
        row = dict(row)
        shot_rows = self.conn.execute(
            "SELECT * FROM shot_revision WHERE tenant_id=? AND branch_id=? AND"
            " storyboard_id=? AND storyboard_revision=? ORDER BY shot_order",
            (tenant_id, row["branch_id"], storyboard_id,
             int(revision))).fetchall()
        shots = []
        for shot_row in shot_rows:
            shots.append(self._shot_document(tenant_id, dict(shot_row)))
        return {
            "storyboard_id": storyboard_id,
            "revision": int(revision),
            "state": row["state"],
            "episode_id": row["episode_id"],
            "branch_id": row["branch_id"],
            "parent_revision": row["parent_revision"],
            "breakdown_ref": {"breakdown_id": row["breakdown_id"],
                              "revision": int(row["breakdown_revision"]),
                              "content_hash": _prefixed(row["breakdown_hash"])},
            "script_ref": {"script_id": row["script_id"],
                           "revision": int(row["script_revision"]),
                           "content_hash": _prefixed(row["script_hash"])},
            "binding_set_ref": (
                {"binding_set_id": row["binding_set_id"],
                 "revision": row["binding_set_revision"],
                 "content_hash": _prefixed(row["binding_set_hash"])}
                if row["binding_set_id"] else None),
            "context_coverage": row["context_coverage"],
            "produced_by": _loads(row["produced_by"], {}),
            "options": _loads(row["options"], {}),
            "shot_count": int(row["shot_count"]),
            "changed_shot_ids": _loads(row["changed_shot_ids"], []),
            "retired_shot_ids": _loads(row["retired_shot_ids"], []),
            "lineage_mints": _loads(row["lineage_mints"], []),
            "shots": shots,
            "content_hash": _prefixed(row["content_hash"]),
            "created_at": row["created_at"],
            "sealed_at": row["sealed_at"],
            "authoritative_source": AUTHORITATIVE_SOURCE,
            "projection_watermark": self._watermark(tenant_id),
            "contract_gaps": CONTRACT_GAPS,
        }

    def get_shot(self, tenant_id: str, shot_id: str,
                 branch_id: str | None = None) -> dict:
        """`GET /shots/{id}`：跨 revision 的身份。退役后仍返回（§3.6.1）。"""
        shot = self._find_shot(tenant_id, shot_id, branch_id)
        revisions = self.conn.execute(
            "SELECT revision, state, storyboard_revision, sealed_at,"
            " content_hash FROM shot_revision WHERE tenant_id=? AND branch_id=?"
            " AND shot_id=? ORDER BY revision",
            (tenant_id, shot["branch_id"], shot_id)).fetchall()
        latest = revisions[-1] if revisions else None
        return {
            "shot_id": shot_id,
            "shot_lineage_id": shot["shot_lineage_id"],
            "branch_id": shot["branch_id"],
            "storyboard_id": shot["storyboard_id"],
            "episode_id": shot["episode_id"],
            "scene_id": shot["scene_id"],
            "state": shot["state"],
            "first_revision": int(shot["first_revision"]),
            "current_revision": int(latest["revision"]) if latest else None,
            "retired_at": shot["retired_at"],
            "retire_reason": shot["retire_reason"],
            "revisions": [{"revision": int(item["revision"]),
                           "state": item["state"],
                           "storyboard_revision": int(
                               item["storyboard_revision"]),
                           "content_hash": _prefixed(item["content_hash"]),
                           "sealed_at": item["sealed_at"]}
                          for item in revisions],
            "authoritative_source": AUTHORITATIVE_SOURCE,
            "projection_watermark": self._watermark(tenant_id),
            "contract_gaps": CONTRACT_GAPS,
        }

    def get_shot_revision(self, tenant_id: str, shot_id: str, revision: int,
                          branch_id: str | None = None) -> dict:
        """`GET /shots/{id}/revisions/{revision}`：`{shot_spec, s06_meta}`。

        T06-22 的依据：旧 revision 的行一经插入就不可变，改镜写的是新行，
        所以这里任何一版都永远读得到。
        """
        shot = self._find_shot(tenant_id, shot_id, branch_id)
        row = self.conn.execute(
            "SELECT * FROM shot_revision WHERE tenant_id=? AND branch_id=? AND"
            " shot_id=? AND revision=?",
            (tenant_id, shot["branch_id"], shot_id, int(revision))).fetchone()
        if row is None:
            raise ServiceError(
                404, "NOT_FOUND",
                f"shot {shot_id} revision {revision} not found")
        return self._shot_document(tenant_id, dict(row))

    def list_scene_shots(self, tenant_id: str, scene_id: str,
                         branch_id: str | None = None) -> dict:
        """`GET /scenes/{id}/shots`：该场"当前有效"的镜头。

        有效 = `shot.state='active'` 且取它最新的 `shot_revision`。已退役镜头
        不出现（§3.6.1），历史版本走 `GET /shots/{id}/revisions/{n}`。
        """
        params: list = [tenant_id, scene_id]
        clause = ""
        if branch_id:
            clause = " AND branch_id=?"
            params.append(branch_id)
        rows = self.conn.execute(
            "SELECT * FROM shot WHERE tenant_id=? AND scene_id=? AND"
            " state='active'" + clause, tuple(params)).fetchall()
        if not rows:
            # 场存在但没有镜头，与"这场不存在"在 S06 里无法区分：场景的权威
            # 是 S05，S06 只存镜头的 `scene_id`（S06-G13）。
            return {"scene_id": scene_id, "branch_id": branch_id,
                    "shots": [], "count": 0,
                    "authoritative_source": AUTHORITATIVE_SOURCE,
                    "projection_watermark": self._watermark(tenant_id),
                    "contract_gaps": CONTRACT_GAPS,
                    "notice": "S06 只知道自己已有的镜头；空列表不代表该场"
                              "在 S05 里没有镜头"}
        out = []
        for row in rows:
            row = dict(row)
            latest = self._latest_shot_revision(tenant_id, row["branch_id"],
                                                row["shot_id"])
            if latest["state"] != "SEALED":
                # 只有已冻结的镜头版本对外"当前有效"。
                continue
            out.append(self._shot_document(tenant_id, latest))
        out.sort(key=lambda doc: (doc["s06_meta"]["storyboard_ref"]["revision"],
                                  doc["shot_spec"]["display_locator"]["shot"]))
        return {"scene_id": scene_id,
                "branch_id": branch_id or (out[0]["s06_meta"]["branch_id"]
                                           if out else None),
                "shots": out, "count": len(out),
                "authoritative_source": AUTHORITATIVE_SOURCE,
                "projection_watermark": self._watermark(tenant_id),
                "contract_gaps": CONTRACT_GAPS}

    def get_lineage(self, tenant_id: str, shot_id: str,
                    branch_id: str | None = None) -> dict:
        """`GET /shots/{id}/lineage`：S26/S29 构造 `AttemptCounterKey` 的入口。

        这里**只给键的组成**，不记任何尝试次数——尝试归 S29/S23（§3.2.4）。
        """
        shot = self._find_shot(tenant_id, shot_id, branch_id)
        lineage = self.conn.execute(
            "SELECT * FROM shot_lineage WHERE tenant_id=? AND"
            " shot_lineage_id=?",
            (tenant_id, shot["shot_lineage_id"])).fetchone()
        if lineage is None:  # pragma: no cover - FK 保证不会发生
            raise ServiceError(500, "INTERNAL", "lineage row is missing")
        lineage = dict(lineage)
        siblings = self.conn.execute(
            "SELECT branch_id, shot_id, state FROM shot WHERE tenant_id=? AND"
            " shot_lineage_id=? ORDER BY branch_id, shot_id",
            (tenant_id, shot["shot_lineage_id"])).fetchall()
        key = {
            "tenant_id": tenant_id,
            "series_id": lineage["series_id"],
            "branch_id": shot["branch_id"],
            "shot_lineage_id": shot["shot_lineage_id"],
            "strategy_class": "<由 S29 填写>",
        }
        return {
            "shot_id": shot_id,
            "shot_lineage_id": shot["shot_lineage_id"],
            "branch_id": shot["branch_id"],
            "series_id": lineage["series_id"],
            "lineage_state": lineage["state"],
            "origin_branch_id": lineage["origin_branch_id"],
            "origin_shot_id": lineage["origin_shot_id"],
            "created_at": lineage["created_at"],
            "retired_at": lineage["retired_at"],
            "siblings": [{"branch_id": item["branch_id"],
                          "shot_id": item["shot_id"],
                          "state": item["state"]} for item in siblings],
            "attempt_counter_key": key,
            "authoritative_source": AUTHORITATIVE_SOURCE,
            "projection_watermark": self._watermark(tenant_id),
            "contract_gaps": CONTRACT_GAPS,
        }

    # ---- 读侧的公共件 ----------------------------------------------------

    def _shot_document(self, tenant_id: str, row: dict) -> dict:
        """把一行 `shot_revision` 渲染成 §4.1 的 `{shot_spec, s06_meta}`。"""
        spec = _loads(row["shot_spec"], {})
        refs = self._refs_of(tenant_id, row["branch_id"], row["shot_id"],
                             int(row["revision"]))
        scene_row = self.conn.execute(
            "SELECT * FROM storyboard_revision WHERE tenant_id=? AND"
            " storyboard_id=? AND revision=?",
            (tenant_id, row["storyboard_id"],
             int(row["storyboard_revision"]))).fetchone()
        lineage = self.conn.execute(
            "SELECT shot_lineage_id FROM shot WHERE tenant_id=? AND"
            " branch_id=? AND shot_id=?",
            (tenant_id, row["branch_id"], row["shot_id"])).fetchone()
        stale, warning = self._staleness(tenant_id, refs)
        meta = {
            "shot_lineage_id": (lineage["shot_lineage_id"] if lineage else None),
            "branch_id": row["branch_id"],
            "state": row["state"],
            "storyboard_ref": {"storyboard_id": row["storyboard_id"],
                               "revision": int(row["storyboard_revision"])},
            "breakdown_ref": ({"breakdown_id": scene_row["breakdown_id"],
                               "revision": int(scene_row["breakdown_revision"]),
                               "content_hash": _prefixed(
                                   scene_row["breakdown_hash"])}
                              if scene_row else None),
            "script_line_refs": [
                {"script_id": ref["script_id"], "revision": ref["revision"],
                 "line_id": ref["line_id"],
                 "text_hash": _prefixed(ref["text_hash"])}
                for ref in refs],
            "feasibility": _loads(row["feasibility"], {}),
            "content_hash": _prefixed(row["content_hash"]),
            "sealed_at": row["sealed_at"],
            "stale": stale,
            "authoritative_source": AUTHORITATIVE_SOURCE,
            "projection_watermark": self._watermark(tenant_id),
            "contract_gaps": CONTRACT_GAPS,
        }
        if warning:
            meta["warnings"] = [warning]
        return {"shot_spec": spec, "s06_meta": meta}

    def _staleness(self, tenant_id: str, refs: list[dict]):
        """T06-37 / S06-G11：`text_copy` 与当前 S04 是否已经不一致。

        只在**能读到** S04 时给布尔值。读不到时给 `None` 并附警告——"不知道"
        与"没变"是两件事，把前者写成 `false` 会让上游以为可以照用。
        """
        if not refs or self._script is None:
            return None, None
        groups: dict[tuple[str, int], list[dict]] = {}
        for ref in refs:
            groups.setdefault((ref["script_id"], ref["revision"]), []).append(ref)
        drifted = False
        for (script_id, revision), items in groups.items():
            try:
                script = self._script(tenant_id, script_id, revision)
            except Exception:  # noqa: BLE001 - 任何读失败都只降级为"不知道"
                return None, {"kind": "stale_unknown",
                              "message": "读不到 S04，无法判断台词是否漂移",
                              "script_id": script_id, "revision": revision}
            by_id = {line["line_id"]: line for line in script.get("lines", [])}
            for ref in items:
                line = by_id.get(ref["line_id"])
                if line is None or _sha256_hex(line["text"]) != ref["text_hash"]:
                    drifted = True
        return drifted, None

    def _watermark(self, tenant_id: str) -> dict:
        row = self.conn.execute(
            "SELECT MAX(created_at) AS at FROM local_event WHERE tenant_id=?",
            (tenant_id,)).fetchone()
        return {"source": AUTHORITATIVE_SOURCE,
                "as_of": (row["at"] if row else None)}
