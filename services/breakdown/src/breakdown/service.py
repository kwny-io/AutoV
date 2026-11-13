"""S05 内核。方案：`plans/S05-breakdown.md`。

五条不能违反的语义，都在这里落地：

1. **`scene_id` 是唯一可以跨版本引用的场景身份，"第几场"只是显示值。**
   `scene_order` / `display_locator` 只活在 `scene_revision` 行里，重排只改它们
   （§1.1 / §3.4）。任何把它们写进 `scene` 的改动都会让一次插入变成全局重命名。
2. **摘录只许复制，不许改写。** `opening_excerpt` 写入时与 S04 逐字节比对，
   不一致就是 422 `SCRIPT_LINE_DRIFT`（§1.2）。这里没有"顺手润色"的路径。
3. **需求不是绑定。** `requirements` 只写 `kind/label/spec_hint`，S05 不铸造
   `prop_id`、不查权利、不判定"这场能不能拍"（§1.3 / §0.2）。
4. **校验失败不自动修复。** 漏行、重复行、顺序矛盾、发言人不在场，全部返回
   422 + 违规列表；没有"把漏掉的行塞进上一场"的分支（§1.4 / §12 U8）。
5. **只有 SEALED 发事件，且只发一条。** DRAFT 不发（§6.3）；重排不发（§6.4）；
   `aggregate_sequence = revision`，跳号是正常的。

S05 不写 S04 的剧本、不写 S06 的镜头、不写 S10 的绑定、不写 S16 的正史。
上游一律是注入的 callback（`deps.build_*`），本模块不 import 任何别的服务的
业务模块（AGENTS.md）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Outbox, new_event
from idempotency_sdk import SqliteIdempotencyStore

from .schema import (
    AUTHORITATIVE_SOURCE,
    CONTINUITY_MODES,
    CONTRACT_GAPS,
    CONTEXT_COVERAGES,
    DEFAULT_CONTINUITY,
    HEADING_PLACES,
    ID_PREFIX,
    LOCAL_EVENT_REORDERED,
    LOCAL_EVENT_RETIRED,
    LOCAL_EVENT_SEALED,
    LOCAL_EVENT_SUPERSEDED,
    MAX_SCENES_DEFAULT,
    MAX_SCENES_HARD,
    ORDER_OFFSET,
    RELATION_KINDS,
    REQUIREMENT_KINDS,
    REVISION_STATES,
    SCENE_RETIRE_REASONS,
    SCHEMA,
    SCHEMA_VERSION,
    SOURCE,
)

# 唯一登记进 outbox 的类型（`contracts/event-topics.v2.json`）。
EVENT_REVISION_CREATED = "breakdown.revision.created"

# §5.8：短 TTL，默认 15 分钟。
RESOLUTION_TTL_SECONDS = 900

# 信任边界：proposal 里出现这些键即丢弃或拒绝（§3.2）。
UNTRUSTED_PROPOSAL_KEYS = ("breakdown_id", "revision", "scene_id", "tenant_id")

SCENE_KEYS = {"scene_order", "display_locator", "heading", "line_span",
              "opening_excerpt", "summary", "characters", "continuity_hints",
              "advisory"}
REQUIREMENT_KEYS = {"requirement_id", "kind", "label", "spec_hint",
                    "first_scene_id", "notes"}
RELATION_KEYS = {"from_scene_id", "to_scene_id", "relation", "note"}


# ------------------------------------------------------------------ 基础

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


def _row(row):
    return None if row is None else dict(row)


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
    """BRK-4：库里存裸 hex，查询响应按 §4.1 渲染 `sha256:` 前缀。"""
    if digest is None:
        return None
    return digest if digest.startswith("sha256:") else "sha256:" + digest


def _bare_digest(value, field: str) -> str:
    """接受裸 hex 或 `sha256:` 前缀，返回裸 hex；其它形态 400。

    §2.4 的示例带前缀、DDL 与事件 schema 要裸 hex（BRK-4）。把规范化放在
    入口一处，后续所有比较才是同一种尺子。
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

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.nested = False

    def __enter__(self):
        if self.conn.in_transaction:
            self.nested = True
            return self
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # 主库不可写（只读副本、磁盘满、锁超时）。写不进去不等于请求有错。
            raise ServiceError(
                503, "BREAKDOWN_UNAVAILABLE",
                f"breakdown database is not writable: {exc}",
                retryable=True) from exc
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.nested:
            return False
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------------ 内容寻址
# §11.1 第 2 步：这个算法必须与 S04/S06/S08 用的是同一个。仓库里还没有共享的
# content-address SDK，所以本文件与 S04 逐字一致地实现一遍；一旦共用 SDK 出现，
# 必须整段替换（同一份剧本在四个服务算出四个 hash 是 R1 全错的形态）。

def line_set_digest(rows) -> str:
    """`line_set_hash`。每行编成 `"{line_id}:{revision}:{text_hash}"`，
    按整串字典序排序后 `\\n` 连接再 sha256。与 S04 `line_set_digest` 同算法。

    排序后 join：行顺序变化只体现在 `seq`，不改变"这版包含哪些内容"。
    """
    encoded = sorted(f"{line_id}:{revision}:{text_hash}"
                     for line_id, revision, text_hash in rows)
    return _sha256_hex("\n".join(encoded))


def scene_content_projection(scene: dict) -> dict:
    """`content_hash` 里的场投影。

    S05-G12：显式排除 `scene_order` 与 `display_locator`。它们在 §1.1 就是
    显示值；把它们算进内容 hash 会让 T05-19（重排后 content_hash 不变）与
    §4.1 的算式直接矛盾。
    """
    return {
        "scene_id": scene["scene_id"],
        "heading": scene["heading"],
        "line_span": scene["line_span"],
        "opening_excerpt": scene["opening_excerpt"],
        "summary": scene["summary"],
        "characters": scene["characters"],
        "continuity_hints": scene["continuity_hints"],
        "advisory": scene["advisory"],
    }


def breakdown_content_hash(*, breakdown_id: str, revision: int,
                           source_script: dict, scenes: list[dict],
                           requirements: list[dict],
                           line_set_hash: str) -> str:
    """§4.1：`sha256(JCS({breakdown_id, revision, source_script, scenes,
    requirements}) + "\\n" + line_set_hash_of_span)`。

    不含 `created_at` / `produced_by` / `context`（§4.1 末段）。
    """
    header = {"breakdown_id": breakdown_id, "revision": revision,
              "source_script": source_script,
              "scenes": [scene_content_projection(s) for s in scenes],
              "requirements": requirements}
    return _sha256_hex(_jcs(header) + "\n" + line_set_hash)


def content_fingerprint(*, source_script: dict, scenes: list[dict],
                        requirements: list[dict], line_set_hash: str) -> str:
    """去重指纹（S05-G17）。**不是** `content_hash`。

    §4.1 的 hash 含 `breakdown_id` 与 `revision`（BRK-9），所以它只能挡"同一
    revision 的重复内容"。T05-03 / T05-06 要的是"同内容、不同 command 或不同
    revision 不新建"，那需要一个与容器、版本、身份都无关的指纹：去掉
    `scene_id`（身份）与显示值（顺序/定位），只留内容。
    """
    header = {
        "source_script": {k: source_script[k] for k in
                          ("script_id", "revision", "content_hash",
                           "line_set_hash")},
        "scenes": [{k: v for k, v in scene_content_projection(s).items()
                    if k != "scene_id"} for s in scenes],
        "requirements": requirements,
    }
    return _sha256_hex(_jcs(header) + "\n" + line_set_hash)


# ------------------------------------------------------------------ 小校验

def _require(condition, message: str, code: str = "INVALID_SCHEMA",
             status: int = 400, details=None) -> None:
    if not condition:
        raise ServiceError(status, code, message, details=details or {})


# 两个错误族，别混：§2.6 的 400 `INVALID_SCHEMA` 是**命令信封**写错（缺
# `script_ref`、`retire_map` 不是对象、`parent_revision` 不是整数）；
# §3.2 ① 的 422 `STRUCTURE_INVALID` 是 **proposal 结构**不合格 —— 那是模型
# 产物，语义是「退回 S19 重写」，不是「调用方把请求写错了」。
PROPOSAL_STRUCTURE = {"code": "STRUCTURE_INVALID", "status": 422}


def _obj(value, field: str, **error) -> dict:
    _require(isinstance(value, dict), f"{field} must be an object", **error)
    return value


def _nonempty_str(value, field: str, **error) -> str:
    _require(isinstance(value, str) and value, f"{field} is required", **error)
    return value


def _int_field(value, field: str, *, minimum: int = 0, **error) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool),
             f"{field} must be an integer", **error)
    _require(value >= minimum, f"{field} must be >= {minimum}", **error)
    return value


def _principal(value) -> dict:
    """`created_by` 的形状。actor 缺失时如实记为 unspecified（S05-G22），
    不编一个看起来像真人的 ID。"""
    if value is None:
        return {"principal_id": "unspecified", "principal_type": "unknown"}
    body = _obj(value, "actor")
    principal_id = _nonempty_str(body.get("principal_id"),
                                 "actor.principal_id")
    principal_type = body.get("principal_type")
    _require(principal_type in ("human", "agent"),
             "actor.principal_type must be human or agent")
    out = {"principal_id": principal_id, "principal_type": principal_type}
    if body.get("delegation_id") is not None:
        out["delegation_id"] = body["delegation_id"]
    return out


def _produced_by(value, *, proposal_ref) -> dict:
    """§4.1 的 `produced_by`。有 proposal_ref 默认是 agent，没有则是 human。"""
    if value is None:
        if proposal_ref:
            return {"kind": "agent",
                    "agent_task_id": proposal_ref.get("agent_task_id"),
                    "correction_rounds": 0}
        return {"kind": "human", "correction_rounds": 0}
    body = _obj(value, "produced_by")
    unknown = set(body) - {"kind", "agent_task_id", "definition_revision",
                           "correction_rounds", "principal_id"}
    _require(not unknown, f"unknown produced_by keys: {sorted(unknown)}")
    kind = body.get("kind")
    _require(kind in ("agent", "human"),
             "produced_by.kind must be agent or human")
    out = {"kind": kind,
           "correction_rounds": body.get("correction_rounds") or 0}
    for key in ("agent_task_id", "definition_revision", "principal_id"):
        if body.get(key) is not None:
            out[key] = body[key]
    return out


# ------------------------------------------------------------------ 场次输入

def _line_index(lines: list[dict]) -> tuple[dict, dict]:
    """把 S04 的行列表变成两把尺子：`seq -> line_id` 与 `line_id -> line`。

    C7：行顺序只隐含在 seq 里。缺 seq / seq 重复时**不猜顺序**，直接 503。
    """
    by_seq: dict[int, str] = {}
    by_id: dict[str, dict] = {}
    for item in lines:
        line_id = item.get("line_id")
        seq = item.get("seq")
        if not isinstance(line_id, str) or not line_id:
            raise ServiceError(
                503, "SCRIPT_SOURCE_UNAVAILABLE",
                "S04 returned a line without a line_id", retryable=True,
                details={"gap": "C7"})
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise ServiceError(
                503, "SCRIPT_SOURCE_UNAVAILABLE",
                f"line {line_id} has no integer seq; S05 will not guess the "
                f"line order (C7)", retryable=True, details={"gap": "C7"})
        if seq in by_seq:
            raise ServiceError(
                503, "SCRIPT_SOURCE_UNAVAILABLE",
                f"S04 returned duplicate seq {seq}; S05 will not guess the "
                f"line order (C7)", retryable=True, details={"gap": "C7"})
        if line_id in by_id:
            raise ServiceError(
                503, "SCRIPT_SOURCE_UNAVAILABLE",
                f"S04 returned line {line_id} twice", retryable=True)
        by_seq[seq] = line_id
        by_id[line_id] = item
    if not by_seq:
        raise ServiceError(
            422, "SCRIPT_NOT_SEALED",
            "the sealed script revision has no lines; an empty line set can "
            "not be broken down")
    return by_seq, by_id


def _line_set_hash_of_lines(lines: list[dict]) -> str:
    return line_set_digest(
        (line["line_id"], line.get("line_revision", 1), line["text_hash"])
        for line in lines)


# ------------------------------------------------------------------ 输入规范化

def _span_of(raw: dict, by_id: dict) -> tuple[int, int]:
    """把 `line_span` 翻译成 seq 区间。**不做任何修补**（§1.4 / U8）。"""
    span = _obj(raw.get("line_span"), "line_span", **PROPOSAL_STRUCTURE)
    start_id = span.get("start_line_id")
    end_id = span.get("end_line_id")
    if start_id not in by_id or end_id not in by_id:
        raise ServiceError(
            422, "LINE_ORDER_VIOLATION",
            "line_span references a line that is not in the source script "
            "revision",
            details={"start_line_id": start_id, "end_line_id": end_id})
    start_seq = by_id[start_id]["seq"]
    end_seq = by_id[end_id]["seq"]
    if start_seq > end_seq:
        # T05-09：start 晚于 end 是顺序矛盾，不是"空区间"。
        raise ServiceError(
            422, "LINE_ORDER_VIOLATION",
            f"scene starts at seq {start_seq} but ends at seq {end_seq}",
            details={"start_line_id": start_id, "end_line_id": end_id,
                     "start_seq": start_seq, "end_seq": end_seq})
    declared = span.get("line_count")
    if declared is not None:
        _require(isinstance(declared, int) and not isinstance(declared, bool),
                 "line_span.line_count must be an integer")
        if declared != end_seq - start_seq + 1:
            raise ServiceError(
                422, "INVALID_LINE_SPAN",
                f"line_count {declared} disagrees with the span "
                f"{end_seq - start_seq + 1}",
                details={"rule": "line_count", "declared": declared,
                         "actual": end_seq - start_seq + 1})
    return start_seq, end_seq


def _normalize_scenes(raw_scenes, by_id: dict) -> list[dict]:
    """结构校验 + 去掉信任输入。**agent 的 `scene_id` 一律丢弃**（§3.2）。

    `scene_id` 出现在输入里不是错误（T05-24）：模型常见行为，不构成安全
    事件。只有 `tenant_id` 出现才拒绝（T05-23）。
    """
    _require(isinstance(raw_scenes, list) and raw_scenes,
             "proposal.scenes must be a non-empty array",
             code="STRUCTURE_INVALID", status=422)
    out: list[dict] = []
    for index, raw in enumerate(raw_scenes):
        if not isinstance(raw, dict):
            raise ServiceError(422, "STRUCTURE_INVALID",
                               f"scenes[{index}] must be an object")
        # 三个键共用同一个错误码，`details.field` 必须回实际撞上的那个 ——
        # 一律写 "tenant_id" 会让 `project_id` 的告警指向错误的字段。
        scope_key = next((key for key in ("tenant_id", "project_id", "series_id")
                          if key in raw), None)
        if scope_key is not None:
            raise ServiceError(
                422, "AGENT_SUPPLIED_TENANT",
                "proposal carries a tenant/project/series scope; the prompt is "
                "contaminated and the proposal is refused",
                details={"field": scope_key, "index": index,
                         "gap": "S05-G21"})
        unknown = set(raw) - SCENE_KEYS - {"scene_id", "breakdown_id",
                                           "revision", "episode_id", "branch_id"}
        _require(not unknown,
                 f"scenes[{index}] has unknown keys: {sorted(unknown)}",
                 code="STRUCTURE_INVALID", status=422)
        heading = _obj(raw.get("heading"), f"scenes[{index}].heading",
                      **PROPOSAL_STRUCTURE)
        place = heading.get("place")
        _require(place in HEADING_PLACES,
                 f"scenes[{index}].heading.place must be one of "
                 f"{list(HEADING_PLACES)}", code="STRUCTURE_INVALID",
                 status=422)
        location = _nonempty_str(
            heading.get("location_label"),
            f"scenes[{index}].heading.location_label", **PROPOSAL_STRUCTURE)
        summary = raw.get("summary")
        _require(isinstance(summary, str) and summary.strip(),
                 f"scenes[{index}].summary is required",
                 code="STRUCTURE_INVALID", status=422)
        start_seq, end_seq = _span_of(raw, by_id)
        display = _obj(raw.get("display_locator"),
                       f"scenes[{index}].display_locator",
                       **PROPOSAL_STRUCTURE)
        _int_field(display.get("episode"), "display_locator.episode",
                   minimum=1, **PROPOSAL_STRUCTURE)
        _int_field(display.get("scene"), "display_locator.scene",
                   minimum=1, **PROPOSAL_STRUCTURE)
        excerpt = raw.get("opening_excerpt")
        if excerpt is not None:
            excerpt = _obj(excerpt, f"scenes[{index}].opening_excerpt",
                           **PROPOSAL_STRUCTURE)
            _nonempty_str(excerpt.get("line_id"),
                          f"scenes[{index}].opening_excerpt.line_id",
                          **PROPOSAL_STRUCTURE)
            _require(isinstance(excerpt.get("text"), str),
                     "opening_excerpt.text must be a string",
                     code="STRUCTURE_INVALID", status=422)
        characters = raw.get("characters") or []
        _require(isinstance(characters, list),
                 "scenes[].characters must be an array",
                 code="STRUCTURE_INVALID", status=422)
        hints = raw.get("continuity_hints") or []
        _require(isinstance(hints, list),
                 "scenes[].continuity_hints must be an array",
                 code="STRUCTURE_INVALID", status=422)
        advisory = raw.get("advisory") or {}
        _require(isinstance(advisory, dict),
                 "scenes[].advisory must be an object",
                 code="STRUCTURE_INVALID", status=422)
        # 输入标识只用于同一 proposal 内互相引用（关系端点），**不是身份**。
        input_key = raw.get("scene_id")
        if not isinstance(input_key, str) or not input_key:
            input_key = f"#{index + 1}"
        out.append({
            "input_key": input_key,
            "position": index + 1,
            "start_line_id": raw["line_span"].get("start_line_id"),
            "end_line_id": raw["line_span"].get("end_line_id"),
            "start_seq": start_seq,
            "end_seq": end_seq,
            # `content_hash` 的场投影按 §4.1 读 `line_span`，这里给出同形。
            "line_span": {"start_line_id": raw["line_span"].get("start_line_id"),
                          "end_line_id": raw["line_span"].get("end_line_id"),
                          "line_count": end_seq - start_seq + 1},
            "heading": {"place": place,
                        "location_label": location,
                        "time_of_day": heading.get("time_of_day")},
            "summary": summary,
            "opening_excerpt": None if excerpt is None else {
                "line_id": excerpt["line_id"],
                "text": excerpt["text"],
                "text_hash": _bare_digest(excerpt["text_hash"],
                                          "opening_excerpt.text_hash")
                if excerpt.get("text_hash") else None,
            },
            "characters": [dict(item) if isinstance(item, dict) else item
                           for item in characters],
            "continuity_hints": list(hints),
            "advisory": dict(advisory),
            "display_episode": display.get("episode"),
            "display_scene": display.get("scene"),
        })
    return out


def _normalize_requirements(raw_items) -> list[dict]:
    items = raw_items or []
    _require(isinstance(items, list),
             "proposal.requirements must be an array",
             code="STRUCTURE_INVALID", status=422)
    out: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise ServiceError(422, "STRUCTURE_INVALID",
                               f"requirements[{index}] must be an object")
        unknown = set(raw) - REQUIREMENT_KEYS
        _require(not unknown,
                 f"requirements[{index}] has unknown keys: {sorted(unknown)}",
                 code="STRUCTURE_INVALID", status=422)
        requirement_id = _nonempty_str(
            raw.get("requirement_id"),
            f"requirements[{index}].requirement_id", **PROPOSAL_STRUCTURE)
        if requirement_id in seen:
            raise ServiceError(
                422, "REQUIREMENT_REF_DANGLING",
                f"requirement_id {requirement_id!r} appears twice in one "
                f"revision", details={"requirement_id": requirement_id})
        seen.add(requirement_id)
        kind = raw.get("kind")
        _require(kind in REQUIREMENT_KINDS,
                 f"requirements[{index}].kind must be one of "
                 f"{list(REQUIREMENT_KINDS)}", code="STRUCTURE_INVALID",
                 status=422)
        label = _nonempty_str(raw.get("label"),
                              f"requirements[{index}].label",
                              **PROPOSAL_STRUCTURE)
        spec_hint = raw.get("spec_hint") or {}
        _require(isinstance(spec_hint, dict), "spec_hint must be an object",
                 code="STRUCTURE_INVALID", status=422)
        out.append({"requirement_id": requirement_id, "kind": kind,
                    "label": label, "spec_hint": dict(spec_hint),
                    "first_scene_id": raw.get("first_scene_id"),
                    "notes": raw.get("notes")})
    return out


def _normalize_retire_map(raw) -> dict:
    """S05-G10：退休理由没有来源，只能由命令体显式给。

    缺省 reason 是 `removed`，**不推断**"这两场合并了" —— 推断出来的
    `merged_into_scene_id` 会让 S09 关闭一条它从来没有打开过的边。
    """
    if raw is None:
        return {}
    _require(isinstance(raw, dict), "retire_map must be an object",
             code="INVALID_SCHEMA", status=400)
    out: dict[str, dict] = {}
    for scene_id, detail in raw.items():
        _require(isinstance(scene_id, str) and scene_id,
                 "retire_map keys must be scene IDs",
                 code="INVALID_SCHEMA", status=400)
        if detail is None:
            detail = {}
        _require(isinstance(detail, dict), "retire_map values must be objects",
                 code="INVALID_SCHEMA", status=400)
        unknown = set(detail) - {"reason", "merged_into_scene_id"}
        _require(not unknown,
                 f"retire_map[{scene_id}] has unknown keys: {sorted(unknown)}",
                 code="INVALID_SCHEMA", status=400)
        reason = detail.get("reason") or "removed"
        _require(reason in SCENE_RETIRE_REASONS,
                 f"retire_map[{scene_id}].reason must be one of "
                 f"{list(SCENE_RETIRE_REASONS)}",
                 code="INVALID_SCHEMA", status=400)
        merged = detail.get("merged_into_scene_id")
        if (reason == "merged_into") != (merged is not None):
            raise ServiceError(
                422, "INVALID_SCHEMA",
                f"retire_map[{scene_id}] needs merged_into_scene_id exactly "
                f"when the reason is merged_into",
                details={"reason": reason, "merged_into_scene_id": merged})
        out[scene_id] = {"reason": reason, "merged_into_scene_id": merged}
    return out


def _normalize_scene_id_map(raw) -> dict:
    """S05-G7：`inherit_explicit` 的键空间——新场的 `start_line_id`。"""
    if raw is None:
        return {}
    _require(isinstance(raw, dict), "scene_id_map must be an object",
             code="INVALID_SCHEMA", status=400)
    out: dict[str, str] = {}
    for anchor, scene_id in raw.items():
        _require(isinstance(anchor, str) and anchor,
                 "scene_id_map keys must be start_line_id values",
                 code="INVALID_SCHEMA", status=400)
        _require(isinstance(scene_id, str) and scene_id,
                 "scene_id_map values must be scene IDs",
                 code="INVALID_SCHEMA", status=400)
        out[anchor] = scene_id
    return out


def _normalize_relations(raw_items, scene_keys: set[str]) -> list[dict]:
    """关系端点用 proposal 内的输入标识（或 1-based 序号）指代，落库前翻译。"""
    items = raw_items or []
    _require(isinstance(items, list), "proposal.relations must be an array",
             code="STRUCTURE_INVALID", status=422)
    out: list[dict] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise ServiceError(422, "STRUCTURE_INVALID",
                               f"relations[{index}] must be an object")
        unknown = set(raw) - RELATION_KEYS
        _require(not unknown,
                 f"relations[{index}] has unknown keys: {sorted(unknown)}",
                 code="STRUCTURE_INVALID", status=422)
        relation = raw.get("relation")
        _require(relation in RELATION_KINDS,
                 f"relations[{index}].relation must be one of "
                 f"{list(RELATION_KINDS)}", code="STRUCTURE_INVALID",
                 status=422)
        ends = []
        for key in ("from_scene_id", "to_scene_id"):
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                value = f"#{value}"
            if not isinstance(value, str) or value not in scene_keys:
                raise ServiceError(
                    422, "REQUIREMENT_REF_DANGLING",
                    f"relations[{index}].{key} does not resolve to a scene in "
                    f"this revision", details={"field": key, "index": index})
            ends.append(value)
        if ends[0] == ends[1]:
            raise ServiceError(
                422, "STRUCTURE_INVALID",
                f"relations[{index}] relates a scene to itself",
                details={"index": index})
        out.append({"from": ends[0], "to": ends[1], "relation": relation,
                    "note": raw.get("note")})
    return out


# ------------------------------------------------------------------ 五条校验

def _validate_coverage(scenes: list[dict], by_seq: dict) -> None:
    """① 每行恰好属于一个场（§1.4）。**不修补**（U8）。

    分母是 S04 实际返回的 seq 集合，不是 `1..N`：方案从没保证 seq 连续
    （C7 只要求唯一单调）。用 `1..N` 当分母会把"seq 从 0 开始"误报成漏行。
    """
    owner: dict[int, int] = {}
    duplicates: list[int] = []
    for scene in scenes:
        for seq in range(scene["start_seq"], scene["end_seq"] + 1):
            if seq in owner:
                duplicates.append(seq)
            else:
                owner[seq] = scene["position"]
    if duplicates:
        raise ServiceError(
            422, "LINE_COVERAGE_OVERLAP",
            f"{len(set(duplicates))} line(s) are assigned to more than one "
            f"scene",
            details={"duplicate_seqs": sorted(set(duplicates))[:100],
                     "duplicate_line_ids": [by_seq[seq] for seq in
                                            sorted(set(duplicates))[:100]
                                            if seq in by_seq],
                     "duplicate_count": len(set(duplicates))})
    missing = sorted(set(by_seq) - set(owner))
    if missing:
        raise ServiceError(
            422, "LINE_COVERAGE_GAP",
            f"{len(missing)} line(s) are not assigned to any scene",
            details={"missing_seqs": missing[:100],
                     "missing_line_ids": [by_seq[seq] for seq in missing[:100]],
                     "missing_count": len(missing)})
    # 场区间跨过了不存在的 seq（比如 S04 缺了一段）：那不是"覆盖"，是幻觉。
    unknown = sorted(set(owner) - set(by_seq))
    if unknown:
        raise ServiceError(
            422, "LINE_ORDER_VIOLATION",
            f"{len(unknown)} seq value(s) in scene spans do not exist in the "
            f"source script revision",
            details={"unknown_seqs": unknown[:100]})


def _validate_order(scenes: list[dict]) -> None:
    """② 场的行区间按提交顺序严格递增、不重叠（§1.4 / §3.6 第 2 条）。"""
    previous_end = 0
    for scene in scenes:
        if scene["start_seq"] <= previous_end:
            raise ServiceError(
                422, "LINE_ORDER_VIOLATION",
                f"scene {scene['position']} starts at seq "
                f"{scene['start_seq']}, which is not after the previous "
                f"scene's end ({previous_end})",
                details={"scene_position": scene["position"],
                         "start_seq": scene["start_seq"],
                         "previous_end": previous_end})
        previous_end = scene["end_seq"]


def _validate_display(scenes: list[dict]) -> None:
    """④ `display_scene` 是该场在本次提交里的 1-based 位置（S05-G5）。"""
    episodes = {scene["display_episode"] for scene in scenes}
    if len(episodes) > 1:
        raise ServiceError(
            422, "DISPLAY_LOCATOR_INVALID",
            "all scenes in one revision must share the same display episode",
            details={"display_episodes": sorted(episodes)})
    for scene in scenes:
        if scene["display_scene"] != scene["position"]:
            raise ServiceError(
                422, "DISPLAY_LOCATOR_INVALID",
                f"scene {scene['position']} is displayed as "
                f"{scene['display_scene']}; the display number must match the "
                f"submitted position",
                details={"scene_position": scene["position"],
                         "display_scene": scene["display_scene"]})


def _validate_requirements(scenes: list[dict], requirements: list[dict],
                           scene_keys: set[str]) -> None:
    """⑤ 每个 `requirement_ref` 都能解析，且角色引用 kind 正确（S05-G14）。"""
    by_id = {item["requirement_id"]: item for item in requirements}
    for scene in scenes:
        for index, member in enumerate(scene["characters"]):
            if not isinstance(member, dict):
                raise ServiceError(422, "STRUCTURE_INVALID",
                                   "characters[] entries must be objects")
            unknown = set(member) - {"requirement_ref", "speaking", "enters",
                                     "exits"}
            _require(not unknown,
                     f"characters[{index}] has unknown keys: "
                     f"{sorted(unknown)}", code="STRUCTURE_INVALID",
                     status=422)
            ref = member.get("requirement_ref")
            if ref not in by_id:
                raise ServiceError(
                    422, "REQUIREMENT_REF_DANGLING",
                    f"scene {scene['position']} references requirement {ref!r} "
                    f"which is not in this revision",
                    details={"requirement_ref": ref,
                             "scene_position": scene["position"]})
            if by_id[ref]["kind"] != "character":
                # S05-G14：characters[] 只放角色。道具/场景进不了参与列表。
                raise ServiceError(
                    422, "REQUIREMENT_KIND_MISMATCH",
                    f"requirement {ref!r} is a {by_id[ref]['kind']}, not a "
                    f"character",
                    details={"requirement_ref": ref,
                             "kind": by_id[ref]["kind"]})
    for item in requirements:
        first = item.get("first_scene_id")
        if first is not None and first not in scene_keys:
            raise ServiceError(
                422, "REQUIREMENT_REF_DANGLING",
                f"requirement {item['requirement_id']!r} says it first appears "
                f"in scene {first!r}, which is not in this revision",
                details={"requirement_id": item["requirement_id"],
                         "first_scene_id": first})


def _validate_speakers(scenes: list[dict], requirements: list[dict],
                       by_seq: dict, by_id: dict) -> None:
    """③ 对白发言人必须在本场角色需求内（§1.4；S05-G4 的匹配规则）。"""
    by_requirement = {item["requirement_id"]: item for item in requirements}
    for scene in scenes:
        allowed_labels: set[str] = set()
        allowed_ids: set[str] = set()
        for member in scene["characters"]:
            item = by_requirement[member["requirement_ref"]]
            allowed_labels.add(item["label"])
            character_id = (item["spec_hint"] or {}).get("character_id")
            if isinstance(character_id, str):
                allowed_ids.add(character_id)
        for seq in range(scene["start_seq"], scene["end_seq"] + 1):
            line = by_id[by_seq[seq]]
            speaker_id = line.get("speaker_id")
            speaker_label = line.get("speaker_label")
            if speaker_id is None and speaker_label is None:
                continue
            if speaker_id and speaker_id in allowed_ids:
                continue
            if speaker_label and speaker_label in allowed_labels:
                continue
            raise ServiceError(
                422, "SPEAKER_NOT_IN_SCENE",
                f"line {line['line_id']} is spoken by "
                f"{speaker_label or speaker_id!r}, who is not among scene "
                f"{scene['position']}'s characters",
                details={"line_id": line["line_id"],
                         "speaker_id": speaker_id,
                         "speaker_label": speaker_label,
                         "scene_position": scene["position"]})


def _validate_excerpts(scenes: list[dict], by_id: dict) -> None:
    """摘录只许复制（§1.2）。

    S05-G20：S04 的行列表不带正文时无法逐字节比对。此时**校验只认
    `text_hash`**，不假装验过——没有正文就没有"逐字节相等"这个事实。
    """
    for scene in scenes:
        excerpt = scene["opening_excerpt"]
        if excerpt is None:
            continue
        line = by_id.get(excerpt["line_id"])
        if line is None:
            raise ServiceError(
                422, "SCRIPT_LINE_DRIFT",
                f"opening_excerpt references {excerpt['line_id']!r}, which is "
                f"not in the source script revision",
                details={"line_id": excerpt["line_id"]})
        text = line.get("text")
        if text is not None:
            digest = _sha256_hex(text)
            if excerpt["text"] != text:
                raise ServiceError(
                    422, "SCRIPT_LINE_DRIFT",
                    f"opening_excerpt for {excerpt['line_id']} does not match "
                    f"the source text; excerpts may only be copied (§1.2)",
                    details={"line_id": excerpt["line_id"]})
            if excerpt["text_hash"] and excerpt["text_hash"] != digest:
                raise ServiceError(
                    422, "SCRIPT_LINE_DRIFT",
                    f"opening_excerpt.text_hash for {excerpt['line_id']} does "
                    f"not match the source text",
                    details={"line_id": excerpt["line_id"]})
            continue
        line_hash = line.get("text_hash")
        if excerpt["text_hash"] and line_hash and \
                excerpt["text_hash"] != line_hash:
            raise ServiceError(
                422, "SCRIPT_LINE_DRIFT",
                f"opening_excerpt.text_hash for {excerpt['line_id']} disagrees "
                f"with the source line's text_hash",
                details={"line_id": excerpt["line_id"]})
# ======================================================================
# 服务
# ======================================================================

class BreakdownService:
    def __init__(self, conn: sqlite3.Connection, *, script_source=None,
                 context_pack_source=None, agent_task_source=None,
                 authorizer=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        # executescript 会隐式提交：表必须在任何事务之外建。
        self.conn.executescript(SCHEMA)
        # outbox / idempotency_record 由平台 SDK 建表（schema.py 不重复 DDL）。
        # 在 executescript 之后构造，否则 SDK 的 DDL 会把外面的事务提交掉。
        self.idem = SqliteIdempotencyStore(self.conn)
        self.outbox = Outbox(self.conn)
        self._script_source = script_source
        self._context_source = context_pack_source
        self._agent_source = agent_task_source
        self._authorizer = authorizer

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

    # ---- 上游：S04 剧本 --------------------------------------------------

    def _load_script(self, tenant_id: str, script_id: str, revision: int, *,
                     require_sealed: bool, expected_content_hash=None,
                     expected_line_set_hash=None) -> dict:
        """读 S04 的 SEALED revision 与完整行列表。

        §6.1 第 1 步：外部读不进事务。S05 不试图消除"读完 S04、写入之前剧本
        又变了"的窗口（无法跨库加锁），只把读到的 hash 存进 revision，S08 提交
        manifest 时再校验一次。
        """
        if self._script_source is None:
            raise ServiceError(
                503, "SCRIPT_SOURCE_UNAVAILABLE",
                "no S04 script source is configured; S05 will not break down a "
                "script it cannot read", retryable=True)
        try:
            script = self._upstream(
                lambda: self._script_source(tenant_id, script_id, revision),
                "SCRIPT_SOURCE_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status == 404:
                # 跨租户同码（E28）：别的租户的剧本在这里看起来就是不存在。
                raise ServiceError(
                    404, "SCRIPT_NOT_FOUND",
                    f"script {script_id} revision {revision} not found",
                    details={"script_id": script_id, "revision": revision}
                ) from exc
            raise
        _require(isinstance(script, dict), "S04 returned no script body",
                 code="SCRIPT_SOURCE_UNAVAILABLE", status=503)
        state = script.get("state")
        if require_sealed and state != "SEALED":
            # §7.1：不是 409。不管重试多少次，DRAFT 剧本都不该被落库。
            raise ServiceError(
                422, "SCRIPT_NOT_SEALED",
                f"script {script_id} revision {revision} is {state or 'unknown'}"
                f"; only a SEALED revision can be persisted",
                details={"script_id": script_id, "revision": revision,
                         "state": state})
        lines = script.get("lines")
        if not isinstance(lines, list):
            raise ServiceError(
                503, "SCRIPT_SOURCE_UNAVAILABLE",
                "S04 returned no line list; S05 will not break down a script "
                "whose lines it cannot enumerate", retryable=True)
        content_hash = script.get("content_hash")
        line_set_hash = script.get("line_set_hash")
        if expected_content_hash and content_hash and \
                expected_content_hash != content_hash:
            # §2.4：这是乐观并发，不是失败 —— 拿最新 ref 重试可能成功。
            raise ServiceError(
                409, "SCRIPT_REVISION_MOVED",
                f"script {script_id} revision {revision} is at content_hash "
                f"{content_hash}, the request pinned {expected_content_hash}",
                retryable=True,
                details={"latest": {"script_id": script_id,
                                    "revision": revision,
                                    "content_hash": _prefixed(content_hash),
                                    "line_set_hash": _prefixed(line_set_hash)}})
        if expected_line_set_hash and line_set_hash and \
                expected_line_set_hash != line_set_hash:
            raise ServiceError(
                409, "SCRIPT_REVISION_MOVED",
                f"script {script_id} revision {revision} line_set_hash moved",
                retryable=True,
                details={"latest": {"script_id": script_id,
                                    "revision": revision,
                                    "content_hash": _prefixed(content_hash),
                                    "line_set_hash": _prefixed(line_set_hash)}})
        if line_set_hash:
            computed = _line_set_hash_of_lines(lines)
            if computed != line_set_hash:
                # §3.6 第 7 条：上游自相矛盾时不能照抄它的声明。
                raise ServiceError(
                    503, "SCRIPT_SOURCE_UNAVAILABLE",
                    f"S04 reported line_set_hash {line_set_hash} but the "
                    f"returned lines hash to {computed}; refusing to record a "
                    f"source it cannot reproduce", retryable=True,
                    details={"reported": line_set_hash, "computed": computed,
                             "gap": "C7"})
        return {"script_id": script_id, "revision": revision, "state": state,
                "content_hash": content_hash or _sha256_hex(
                    _line_set_hash_of_lines(lines)),
                "line_set_hash": line_set_hash or _line_set_hash_of_lines(lines),
                "lines": lines}

    # ---- 上游：S18 ContextPack -------------------------------------------

    def _load_context(self, tenant_id: str, ref) -> dict:
        """§3.1 / §7.1：ContextPack 缺失或不可用 → `partial`，**不阻断**。"""
        if not ref:
            return {"pack_id": None, "content_hash": None, "coverage": "partial"}
        ref = _obj(ref, "context_pack_ref")
        pack_id = _nonempty_str(ref.get("pack_id"), "context_pack_ref.pack_id")
        if self._context_source is None:
            return {"pack_id": pack_id,
                    "content_hash": ref.get("content_hash"),
                    "coverage": "partial"}
        try:
            pack = self._upstream(
                lambda: self._context_source(tenant_id, pack_id),
                "CONTEXT_PACK_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status == 404:
                raise ServiceError(
                    422, "CONTEXT_PACK_EXPIRED",
                    f"context pack {pack_id} no longer exists",
                    details={"pack_id": pack_id}) from exc
            if exc.status == 503:
                # §7.5：S18 不可用允许继续。降级必须是显式的。
                return {"pack_id": pack_id,
                        "content_hash": ref.get("content_hash"),
                        "coverage": "partial"}
            raise
        # `deps.build_context_pack_source` 交回的是 S18 的原始响应 `{pack: {}}`
        # （与 S19 adapter 同形）；直接读顶层会永远看不到 `content_hash`/`stale`。
        # 两种形状都接受，平铺形态留给直接注入 pack 的调用方。
        if isinstance(pack, dict) and isinstance(pack.get("pack"), dict):
            pack = pack["pack"]
        if not isinstance(pack, dict):
            return {"pack_id": pack_id,
                    "content_hash": ref.get("content_hash"),
                    "coverage": "partial"}
        if pack.get("stale") is True:
            # §7.1：失效的 pack 不能当作有效上下文。
            raise ServiceError(
                422, "CONTEXT_PACK_EXPIRED",
                f"context pack {pack_id} is stale; fetch a fresh one",
                details={"pack_id": pack_id})
        pinned = ref.get("content_hash")
        actual = pack.get("content_hash")
        if pinned and actual and _bare_digest(pinned, "context_pack_ref."
                                              "content_hash") != actual:
            raise ServiceError(
                422, "CONTEXT_PACK_EXPIRED",
                f"context pack {pack_id} moved; S05 will not record a pack "
                f"whose content differs from the pinned hash",
                details={"pack_id": pack_id})
        return {"pack_id": pack_id, "content_hash": actual or pinned,
                "coverage": "full"}

    # ---- 上游：S19 proposal ---------------------------------------------

    def _resolve_proposal(self, tenant_id: str, body: dict) -> dict:
        """S05-G1：场次内容既可从请求体内联，也可回源 S19。"""
        inline = body.get("proposal")
        if isinstance(inline, dict):
            return inline
        ref = body.get("proposal_ref")
        if not ref:
            raise ServiceError(
                422, "STRUCTURE_INVALID",
                "neither an inline proposal nor a proposal_ref was supplied; "
                "there is nothing to break down",
                details={"gap": "S05-G1"})
        ref = _obj(ref, "proposal_ref")
        task_id = _nonempty_str(ref.get("agent_task_id"),
                                "proposal_ref.agent_task_id")
        if self._agent_source is None:
            raise ServiceError(
                503, "AGENT_TASK_SOURCE_UNAVAILABLE",
                "S19_BASE_URL is not configured; S05 cannot fetch the proposal "
                "it was asked to persist", retryable=True)
        try:
            result = self._upstream(
                lambda: self._agent_source(tenant_id, task_id),
                "AGENT_TASK_SOURCE_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status == 404:
                raise ServiceError(
                    422, "STRUCTURE_INVALID",
                    f"agent task {task_id} has no result to break down",
                    details={"agent_task_id": task_id,
                             "reason": "task_result_missing"}) from exc
            raise
        proposal = result.get("proposal") if isinstance(result, dict) else None
        if not isinstance(proposal, dict):
            raise ServiceError(
                422, "STRUCTURE_INVALID",
                f"agent task {task_id} returned no proposal object",
                details={"agent_task_id": task_id})
        return proposal

    # ---- 事件 ------------------------------------------------------------

    def _emit_local(self, tenant_id: str, event_type: str,
                    aggregate_id: str, payload: dict) -> None:
        """未登记进 event-topics 的事件写本地表，不进 outbox（§3.4 / §8.3）。"""
        self.conn.execute(
            "INSERT INTO local_event (tenant_id, local_event_id, event_type,"
            " aggregate_id, payload, created_at) VALUES (?,?,?,?,?,?)",
            (tenant_id, _new_id("local_event"), event_type, aggregate_id,
             _jcs(payload), _now()))

    def _emit_revision_created(self, tenant_id: str, revision_row,
                               scene_ids: list[str], retired_ids: list[str],
                               *, command_id: str, home_cell: str) -> None:
        """`breakdown.revision.created` —— 只在 seal 时一条（§4.2 / §6.3）。

        `data` 是 12 个键、`additionalProperties: false`，与
        `contracts/breakdown.revision-created.v2.schema.json` 逐键对齐。
        `aggregate_sequence = revision`（BRK-6：跳号是正常的）。

        data_ref 在方案 §4.2 里是对象，而 event-sdk 标的是 `str | None`
        （S05-G19）—— 与 S04 一样传对象，SDK 不做运行时校验。
        """
        content_hash = revision_row["content_hash"]
        data = {
            "schema_version": SCHEMA_VERSION,
            "breakdown_id": revision_row["breakdown_id"],
            "revision": int(revision_row["revision"]),
            "state": "SEALED",
            "episode_id": revision_row["episode_id"],
            "branch_id": revision_row["branch_id"],
            "source_script": {
                "script_id": revision_row["script_id"],
                "revision": int(revision_row["script_revision"]),
                "content_hash": revision_row["script_content_hash"],
            },
            "scene_ids": list(scene_ids),
            "retired_scene_ids": list(retired_ids),
            "requirement_count": int(revision_row["requirement_count"]),
            "content_hash": content_hash,
            "occurred_at": revision_row["sealed_at"],
        }
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_REVISION_CREATED,
            subject=revision_row["episode_id"],
            tenant_id=tenant_id, home_cell=home_cell,
            aggregate_id=revision_row["breakdown_id"],
            sequence=int(revision_row["revision"]),
            correlation_id=command_id, causation_id=command_id,
            data=data,
            data_ref={"service_id": SOURCE,
                      "id": revision_row["breakdown_id"],
                      "revision": int(revision_row["revision"]),
                      "content_hash": content_hash}))

    # ---- 场景身份 --------------------------------------------------------

    def _retire_scene(self, tenant_id: str, scene_id: str, reason: str,
                      merged_into: str | None) -> bool:
        """`retired_at` 一旦落值不可清除（§3.6 第 5 条）。

        `WHERE retired_at IS NULL`：第二次退休是 no-op，返回 False，不报错
        （§6.2「并发 :retire 同一场 → 第二次仍是 200」）。
        """
        if reason not in SCENE_RETIRE_REASONS:
            raise ServiceError(
                400, "INVALID_SCHEMA",
                f"retire reason {reason!r} must be one of "
                f"{list(SCENE_RETIRE_REASONS)}")
        if (reason == "merged_into") != (merged_into is not None):
            raise ServiceError(
                422, "INVALID_SCHEMA",
                "merged_into_scene_id is required exactly when the reason is "
                "merged_into",
                details={"reason": reason,
                         "merged_into_scene_id": merged_into})
        if merged_into == scene_id:
            raise ServiceError(422, "INVALID_SCHEMA",
                               "a scene cannot be merged into itself")
        cursor = self.conn.execute(
            "UPDATE scene SET retired_at=?, retire_reason=?,"
            " merged_into_scene_id=? WHERE tenant_id=? AND scene_id=?"
            " AND retired_at IS NULL",
            (_now(), reason, merged_into, tenant_id, scene_id))
        return cursor.rowcount == 1

    @staticmethod
    def _mint_scene() -> str:
        """只分配 ID，不写库。

        `content_fingerprint` 命中时整条命令要返回既有 revision；若铸造时
        就插了 `scene` 行，去重路径会留下一批没人引用的场（而事务会提交它们）。
        """
        return _new_id("scene")

    def _assign_scene_ids(self, tenant_id: str, breakdown, scenes: list[dict],
                          *, parent_revision, continuity: str,
                          explicit_map: dict, retire_map: dict,
                          by_id: dict) -> tuple[list[str], list[dict], list[str]]:
        """§3.3 的三种连续策略。

        返回 `(本版场 ID 列表, 父版场行列表, 本版新铸造的 ID 列表)`。
        第三个返回值存在的理由：调用方要在**去重判断之后**才插 `scene` 行。

        两条不变量在这里保证：
        - 一个旧 `scene_id` 最多被一个新场继承（S05-G8：第二个场 mint）。
        - 被 `retire_map` 点名的旧场**不参与继承**，否则"拆分"永远表达不出来
          （S05-G24 / T05-17）。
        """
        old_rows: list[dict] = []
        if parent_revision is not None:
            old_rows = [dict(row) for row in self.conn.execute(
                "SELECT * FROM scene_revision WHERE tenant_id=? AND"
                " breakdown_id=? AND revision=? ORDER BY scene_order",
                (tenant_id, breakdown["breakdown_id"], parent_revision)
            ).fetchall()]
        forced = set(retire_map)
        # 已退休的场永远不参与自动继承（S05-G6 / T05-22）。退休可能发生在这
        # 一版的旧行之外：`:retire` 命令、或更早一版的 `_retire_set`。只查
        # 父 revision 的行看不到这些，必须回 `scene` 身份表。
        retired = {row["scene_id"] for row in self.conn.execute(
            "SELECT scene_id FROM scene WHERE tenant_id=? AND episode_id=?"
            " AND branch_id=? AND retired_at IS NOT NULL",
            (tenant_id, breakdown["episode_id"], breakdown["branch_id"])
        ).fetchall()}

        def _contains(old: dict, anchor: str) -> bool:
            if old["start_line_id"] == anchor or old["end_line_id"] == anchor:
                return True
            # 跨 revision 时旧端点可能已不在当前行索引里；能比 seq 才比，
            # 比不了就不猜（S05-G26 / C7）。
            if anchor not in by_id:
                return False
            if old["start_line_id"] not in by_id or \
                    old["end_line_id"] not in by_id:
                return False
            seq = by_id[anchor]["seq"]
            return by_id[old["start_line_id"]]["seq"] <= seq <= \
                by_id[old["end_line_id"]]["seq"]

        assigned: list[str] = []
        used: set[str] = set()
        for scene in scenes:
            anchor = scene["start_line_id"]
            chosen = None
            if continuity == "inherit_explicit":
                candidate = explicit_map.get(anchor)
                if candidate:
                    row = self.conn.execute(
                        "SELECT * FROM scene WHERE tenant_id=? AND scene_id=?",
                        (tenant_id, candidate)).fetchone()
                    if row is None or row["episode_id"] != breakdown["episode_id"] \
                            or row["branch_id"] != breakdown["branch_id"]:
                        raise ServiceError(
                            422, "SCENE_ID_CONFLICT",
                            f"scene_id {candidate!r} does not belong to this "
                            f"episode and branch",
                            details={"scene_id": candidate})
                    if row["retired_at"] is not None:
                        # T05-22 / S05-G6：退休的场不许回到新 revision。
                        raise ServiceError(
                            422, "SCENE_RETIRED",
                            f"scene {candidate!r} is retired and cannot be "
                            f"reused in a new revision",
                            details={"scene_id": candidate})
                    if candidate in used:
                        raise ServiceError(
                            422, "SCENE_ID_CONFLICT",
                            f"scene_id {candidate!r} is mapped onto two scenes",
                            details={"scene_id": candidate})
                    chosen = candidate
            elif continuity == "inherit_by_line_anchor":
                for old in old_rows:
                    if old["scene_id"] in forced or old["scene_id"] in used \
                            or old["scene_id"] in retired:
                        continue
                    if _contains(old, anchor):
                        chosen = old["scene_id"]
                        break
            if chosen is not None and chosen not in used:
                used.add(chosen)
                assigned.append(chosen)
            else:
                assigned.append(self._mint_scene())
        minted = [scene_id for scene_id in assigned
                  if scene_id not in {row["scene_id"] for row in old_rows}]
        return assigned, old_rows, minted
    # ---- 输入解析 --------------------------------------------------------

    def _parse_script_ref(self, body: dict) -> dict:
        ref = _obj(body.get("script_ref"), "script_ref")
        script_id = _nonempty_str(ref.get("script_id"), "script_ref.script_id")
        revision = _int_field(ref.get("revision"), "script_ref.revision",
                              minimum=1)
        content_hash = ref.get("content_hash")
        line_set_hash = ref.get("line_set_hash")
        # 前缀与裸 hex 都接受，落库统一裸 hex（BRK-4）。
        return {
            "script_id": script_id,
            "revision": revision,
            "content_hash": None if content_hash is None else _bare_digest(
                content_hash, "script_ref.content_hash"),
            "line_set_hash": None if line_set_hash is None else _bare_digest(
                line_set_hash, "script_ref.line_set_hash"),
        }

    def _parse_options(self, body: dict) -> dict:
        options = body.get("options") or {}
        _require(isinstance(options, dict), "options must be an object",
                 code="INVALID_SCHEMA", status=400)
        unknown = set(options) - {"allow_reuse_scene_ids", "max_scenes",
                                  "preview", "seal"}
        _require(not unknown, f"unknown options keys: {sorted(unknown)}",
                 code="INVALID_SCHEMA", status=400)
        max_scenes = options.get("max_scenes", MAX_SCENES_DEFAULT)
        _int_field(max_scenes, "options.max_scenes", minimum=1)
        if max_scenes > MAX_SCENES_HARD:
            # S05-G13 / C11：上限没有依据，这里不放宽。
            raise ServiceError(
                400, "INVALID_SCHEMA",
                f"options.max_scenes may not exceed {MAX_SCENES_HARD}",
                details={"max_scenes": max_scenes, "hard_limit": MAX_SCENES_HARD,
                         "gap": "S05-G13"})
        preview = options.get("preview", False)
        _require(isinstance(preview, bool), "options.preview must be a boolean",
                 code="INVALID_SCHEMA", status=400)
        seal = options.get("seal", False)
        _require(isinstance(seal, bool), "options.seal must be a boolean",
                 code="INVALID_SCHEMA", status=400)
        return {"max_scenes": max_scenes, "preview": preview, "seal": seal}

    def _build_revision(self, tenant_id: str, body: dict, breakdown,
                        revision: int, *, parent_revision,
                        continuity: str, home_cell: str,
                        command_id: str) -> dict:
        """§6.1 的 1–6 步。**只写库，不返回**；调用方决定响应形状。

        外部读（S04/S18/S19）全部在写之前完成。`Transaction` 已经把整个
        命令包住，这里再加一层也不会嵌套（`Transaction` 检测 in_transaction）。
        """
        script_ref = self._parse_script_ref(body)
        if breakdown is not None and script_ref["script_id"] != \
                breakdown["script_id"]:
            raise ServiceError(
                409, "BREAKDOWN_REVISION_CONFLICT",
                f"breakdown {breakdown['breakdown_id']} is bound to script "
                f"{breakdown['script_id']}, not {script_ref['script_id']}",
                details={"reason": "script_mismatch",
                         "bound_script_id": breakdown["script_id"]})

        # 1. 校验 S04 当前 revision 与 hash（读，不写）。
        script = self._load_script(
            tenant_id, script_ref["script_id"], script_ref["revision"],
            require_sealed=True,
            expected_content_hash=script_ref["content_hash"],
            expected_line_set_hash=script_ref["line_set_hash"])
        by_seq, by_id = _line_index(script["lines"])

        # 2. 上下文（S18 缺失不阻断，但必须如实记成 partial）。
        context = self._load_context(tenant_id, body.get("context_pack_ref"))

        # 3. proposal（内联或回源 S19；两者都没有就无从分场）。
        proposal = self._resolve_proposal(tenant_id, body)
        scenes = _normalize_scenes(proposal.get("scenes"), by_id)
        options = self._parse_options(body)
        if len(scenes) > options["max_scenes"]:
            raise ServiceError(
                422, "SCENE_LIMIT_EXCEEDED",
                f"{len(scenes)} scenes exceed max_scenes "
                f"{options['max_scenes']}",
                details={"scene_count": len(scenes),
                         "max_scenes": options["max_scenes"]})
        requirements = _normalize_requirements(proposal.get("requirements"))
        scene_keys = {scene["input_key"] for scene in scenes}
        relations = _normalize_relations(proposal.get("relations"), scene_keys)

        # 4. 五条校验。任何一条失败都是拒绝，没有自动修复（§1.4 / U8）。
        _validate_coverage(scenes, by_seq)
        _validate_order(scenes)
        _validate_display(scenes)
        _validate_requirements(scenes, requirements, scene_keys)
        _validate_speakers(scenes, requirements, by_seq, by_id)
        _validate_excerpts(scenes, by_id)

        # 5. 身份：铸造 / 继承。**只决定，不插入** —— 去重命中时不能留下孤儿场。
        retire_map = _normalize_retire_map(body.get("retire_map"))
        explicit_map = _normalize_scene_id_map(body.get("scene_id_map"))
        assigned, old_rows, new_ids = self._assign_scene_ids(
            tenant_id, breakdown, scenes, parent_revision=parent_revision,
            continuity=continuity, explicit_map=explicit_map,
            retire_map=retire_map, by_id=by_id)
        for scene, scene_id in zip(scenes, assigned):
            scene["scene_id"] = scene_id
            scene["scene_order"] = scene["position"]

        # requirement.first_scene_id：输入里给的是 proposal 内的键，翻译成
        # 真正的 scene_id；没给就取第一个引用它的场（S05-G15 不做跨版身份）。
        key_to_scene = {scene["input_key"]: scene["scene_id"]
                        for scene in scenes}
        first_ref: dict[str, str] = {}
        for scene in scenes:
            for member in scene["characters"]:
                ref = member.get("requirement_ref")
                if ref and ref not in first_ref:
                    first_ref[ref] = scene["scene_id"]
        for item in requirements:
            declared = item.get("first_scene_id")
            if declared is None:
                item["first_scene_id"] = first_ref.get(
                    item["requirement_id"], scenes[0]["scene_id"])
            else:
                item["first_scene_id"] = key_to_scene[declared]

        # 6. 内容 hash 与跨版去重指纹。
        content_hash = breakdown_content_hash(
            breakdown_id=breakdown["breakdown_id"] if breakdown else
            body["_breakdown_id"], revision=revision,
            source_script={"script_id": script["script_id"],
                           "revision": script["revision"],
                           "content_hash": script["content_hash"]},
            scenes=scenes, requirements=requirements,
            line_set_hash=script["line_set_hash"])
        fingerprint = content_fingerprint(
            source_script={"script_id": script["script_id"],
                           "revision": script["revision"],
                           "content_hash": script["content_hash"],
                           "line_set_hash": script["line_set_hash"]},
            scenes=scenes, requirements=requirements,
            line_set_hash=script["line_set_hash"])

        existing = self.conn.execute(
            "SELECT revision FROM breakdown_revision WHERE tenant_id=? AND"
            " breakdown_id=? AND content_fingerprint=?",
            (tenant_id, body["_breakdown_id"], fingerprint)).fetchone()
        if existing is None:
            existing = self.conn.execute(
                "SELECT revision FROM breakdown_revision WHERE tenant_id=? AND"
                " breakdown_id=? AND content_hash=?",
                (tenant_id, body["_breakdown_id"], content_hash)).fetchone()
        if existing is not None:
            # §7.4：同一份内容返回既有 revision，不新建（T05-03 / T05-06）。
            return {"deduplicated": True,
                    "existing_revision": int(existing["revision"])}

        # 7. 写。
        retired_ids = sorted(self._retire_set(old_rows, assigned, retire_map))
        produced_by = _produced_by(body.get("produced_by"),
                                   proposal_ref=body.get("proposal_ref"))
        self.conn.execute(
            "INSERT INTO breakdown_revision (tenant_id, breakdown_id, revision,"
            " state, episode_id, branch_id, script_id, script_revision,"
            " script_content_hash, script_line_set_hash, context_pack_id,"
            " context_pack_hash, context_coverage, produced_by,"
            " parent_revision, scene_id_continuity, scene_count,"
            " requirement_count, retired_scene_ids, content_hash,"
            " content_fingerprint, sealed_at, created_at)"
            " VALUES (?,?,?,'DRAFT',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (tenant_id, breakdown["breakdown_id"], revision,
             breakdown["episode_id"], breakdown["branch_id"],
             script["script_id"], script["revision"],
             script["content_hash"], script["line_set_hash"],
             context["pack_id"],
             None if context["content_hash"] is None
             else _bare_digest(context["content_hash"],
                               "context_pack_ref.content_hash"),
             context["coverage"], _jcs(produced_by), parent_revision,
             continuity, len(scenes), len(requirements),
             _dumps(retired_ids), content_hash, fingerprint, _now()))

        for scene_id in new_ids:
            self.conn.execute(
                "INSERT INTO scene (tenant_id, scene_id, project_id, series_id,"
                " episode_id, branch_id, first_breakdown_id, first_revision,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, scene_id, breakdown["project_id"],
                 breakdown["series_id"], breakdown["episode_id"],
                 breakdown["branch_id"], breakdown["breakdown_id"], revision,
                 _now()))

        for scene in scenes:
            excerpt = scene["opening_excerpt"]
            text_hash = None
            if excerpt is not None:
                text_hash = excerpt["text_hash"] or _sha256_hex(excerpt["text"])
            self.conn.execute(
                "INSERT INTO scene_revision (tenant_id, breakdown_id, revision,"
                " scene_id, scene_order, display_episode, display_scene,"
                " heading_place, heading_location, heading_tod,"
                " start_line_id, end_line_id, line_count, opening_line_id,"
                " opening_text, opening_text_hash, summary, continuity_hints,"
                " advisory) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, breakdown["breakdown_id"], revision,
                 scene["scene_id"], scene["scene_order"],
                 scene["display_episode"], scene["display_scene"],
                 scene["heading"]["place"], scene["heading"]["location_label"],
                 scene["heading"]["time_of_day"], scene["start_line_id"],
                 scene["end_line_id"],
                 scene["end_seq"] - scene["start_seq"] + 1,
                 None if excerpt is None else excerpt["line_id"],
                 None if excerpt is None else excerpt["text"],
                 text_hash, scene["summary"], _dumps(scene["continuity_hints"]),
                 _jcs(scene["advisory"])))

        for item in requirements:
            self.conn.execute(
                "INSERT INTO requirement (tenant_id, breakdown_id, revision,"
                " requirement_id, kind, label, spec_hint, first_scene_id, notes)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, breakdown["breakdown_id"], revision,
                 item["requirement_id"], item["kind"], item["label"],
                 _jcs(item["spec_hint"]), item["first_scene_id"],
                 item["notes"]))

        for scene in scenes:
            for member in scene["characters"]:
                self.conn.execute(
                    "INSERT INTO scene_participant (tenant_id, breakdown_id,"
                    " revision, scene_id, requirement_id, speaking, enters,"
                    " exits) VALUES (?,?,?,?,?,?,?,?)",
                    (tenant_id, breakdown["breakdown_id"], revision,
                     scene["scene_id"], member["requirement_ref"],
                     1 if member.get("speaking") else 0,
                     1 if member.get("enters") else 0,
                     1 if member.get("exits") else 0))

        for relation in relations:
            self.conn.execute(
                "INSERT INTO scene_relation (tenant_id, breakdown_id, revision,"
                " from_scene_id, to_scene_id, relation, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (tenant_id, breakdown["breakdown_id"], revision,
                 key_to_scene[relation["from"]], key_to_scene[relation["to"]],
                 relation["relation"], relation["note"]))

        # 8. 旧场退休（只改身份表，不改历史 revision 行）。
        for scene_id in retired_ids:
            detail = retire_map.get(scene_id) or {}
            self._retire_scene(tenant_id, scene_id,
                               detail.get("reason", "removed"),
                               detail.get("merged_into_scene_id"))

        return {"deduplicated": False, "content_hash": content_hash,
                "scene_ids": list(assigned), "retired_scene_ids": retired_ids,
                "retired_rows": old_rows, "requirements": requirements,
                "scenes": scenes}

    def _retire_set(self, old_rows: list[dict], assigned: list[str],
                    retire_map: dict) -> set[str]:
        inherited = set(assigned)
        conflict = inherited & set(retire_map)
        if conflict:
            # "这版继承它"与"这版退休它"同时成立会让退休语义落空：
            # `_retire_scene` 写的是 scene 行，继承走的是同一个 ID。
            raise ServiceError(
                422, "SCENE_ID_CONFLICT",
                "retire_map names a scene that this revision also inherits",
                details={"scene_ids": sorted(conflict)})
        retired = {row["scene_id"] for row in old_rows
                   if row["scene_id"] not in inherited}
        retired |= set(retire_map)
        return retired

    # ---- POST /breakdowns ------------------------------------------------

    def create_breakdown(self, tenant_id: str, body: dict, *,
                         command_id: str, request_hash: str,
                         home_cell: str = "local") -> dict:
        def produce():
            for key in ("project_id", "series_id", "episode_id", "branch_id"):
                _nonempty_str(body.get(key), key)
            script_ref = self._parse_script_ref(body)
            options = self._parse_options(body)
            breakdown_id = _new_id("breakdown")
            actor = _principal(body.get("actor"))
            now = _now()
            self.conn.execute(
                "INSERT INTO breakdown (tenant_id, breakdown_id, project_id,"
                " series_id, episode_id, branch_id, script_id, lifecycle,"
                " head_revision, sealed_revision, created_by, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,'active',0,NULL,?,?,?)",
                (tenant_id, breakdown_id, body["project_id"],
                 body["series_id"], body["episode_id"], body["branch_id"],
                 script_ref["script_id"], _jcs(actor), now, now))
            breakdown = self.conn.execute(
                "SELECT * FROM breakdown WHERE tenant_id=? AND breakdown_id=?",
                (tenant_id, breakdown_id)).fetchone()

            scope = dict(body)
            scope["_breakdown_id"] = breakdown_id
            outcome = self._build_revision(
                tenant_id, scope, breakdown, 1, parent_revision=None,
                continuity="fresh", home_cell=home_cell,
                command_id=command_id)
            if outcome["deduplicated"]:
                # 新建容器不可能命中已有 revision（fingerprint 按 container
                # 过滤），真出现就是内部错误，不能返回一个不存在的版本。
                raise ServiceError(
                    500, "INTERNAL_ERROR",
                    "a brand-new breakdown matched an existing revision")
            self.conn.execute(
                "UPDATE breakdown SET head_revision=1, updated_at=?"
                " WHERE tenant_id=? AND breakdown_id=?",
                (_now(), tenant_id, breakdown_id))
            if options["seal"]:
                self._seal_revision(tenant_id, breakdown_id, 1, body,
                                    command_id=command_id,
                                    home_cell=home_cell)
            document = self._revision_document(tenant_id, breakdown_id, 1)
            document["deduplicated"] = False
            return document

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /breakdowns/{id}/revisions ---------------------------------

    def append_revision(self, tenant_id: str, breakdown_id: str, body: dict, *,
                        command_id: str, request_hash: str,
                        home_cell: str = "local") -> dict:
        """§2.5。CAS 在 `parent_revision` 上（§6.2 第一行）。"""
        def produce():
            breakdown = self.conn.execute(
                "SELECT * FROM breakdown WHERE tenant_id=? AND breakdown_id=?",
                (tenant_id, breakdown_id)).fetchone()
            if breakdown is None:
                raise ServiceError(
                    404, "NOT_FOUND",
                    f"breakdown {breakdown_id} not found")
            head = int(breakdown["head_revision"])
            parent = _int_field(body.get("parent_revision"), "parent_revision",
                                minimum=1) if body.get("parent_revision") \
                is not None else head
            if parent != head:
                # §6.2：`UPDATE ... WHERE head_revision = $parent` 的 0 行。
                raise ServiceError(
                    409, "BREAKDOWN_REVISION_CONFLICT",
                    f"breakdown {breakdown_id} is at revision {head}, the "
                    f"request pinned parent_revision {parent}",
                    retryable=True,
                    details={"head_revision": head,
                             "parent_revision": parent})
            continuity = body.get("scene_id_continuity") or DEFAULT_CONTINUITY
            _require(continuity in CONTINUITY_MODES,
                     f"scene_id_continuity must be one of "
                     f"{list(CONTINUITY_MODES)}",
                     code="INVALID_SCHEMA", status=400)
            options = self._parse_options(body)
            revision = head + 1
            scope = dict(body)
            scope["_breakdown_id"] = breakdown_id
            outcome = self._build_revision(
                tenant_id, scope, breakdown, revision,
                parent_revision=parent, continuity=continuity,
                home_cell=home_cell, command_id=command_id)
            if outcome["deduplicated"]:
                document = self._revision_document(
                    tenant_id, breakdown_id, outcome["existing_revision"])
                document["deduplicated"] = True
                return document
            self.conn.execute(
                "UPDATE breakdown SET head_revision=?, updated_at=?"
                " WHERE tenant_id=? AND breakdown_id=? AND head_revision=?",
                (revision, _now(), tenant_id, breakdown_id, head))
            if options["seal"]:
                self._seal_revision(tenant_id, breakdown_id, revision, body,
                                    command_id=command_id,
                                    home_cell=home_cell)
            document = self._revision_document(tenant_id, breakdown_id, revision)
            document["deduplicated"] = False
            return document

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /breakdowns/{id}/revisions/{rev}:seal ----------------------

    def _seal_revision(self, tenant_id: str, breakdown_id: str, revision: int,
                       body: dict, *, command_id: str, home_cell: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM breakdown_revision WHERE tenant_id=? AND"
            " breakdown_id=? AND revision=?",
            (tenant_id, breakdown_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"revision {revision} not found")
        if row["state"] != "DRAFT":
            # S05-G9 / §6.2：0 行更新 → 409，终态不倒退。
            raise ServiceError(
                409, "REVISION_NOT_DRAFT",
                f"revision {revision} is {row['state']}, not DRAFT",
                details={"state": row["state"], "revision": revision})
        now = _now()
        cursor = self.conn.execute(
            "UPDATE breakdown_revision SET state='SEALED', sealed_at=?"
            " WHERE tenant_id=? AND breakdown_id=? AND revision=?"
            " AND state='DRAFT'",
            (now, tenant_id, breakdown_id, revision))
        if cursor.rowcount != 1:
            raise ServiceError(
                409, "REVISION_NOT_DRAFT",
                f"revision {revision} was sealed concurrently",
                details={"revision": revision})

        previous = self.conn.execute(
            "SELECT revision FROM breakdown_revision WHERE tenant_id=? AND"
            " breakdown_id=? AND state='SEALED' AND revision<>?",
            (tenant_id, breakdown_id, revision)).fetchall()
        for old in previous:
            self.conn.execute(
                "UPDATE breakdown_revision SET state='SUPERSEDED'"
                " WHERE tenant_id=? AND breakdown_id=? AND revision=?",
                (tenant_id, breakdown_id, old["revision"]))
            self._emit_local(
                tenant_id, LOCAL_EVENT_SUPERSEDED, breakdown_id,
                {"breakdown_id": breakdown_id, "revision": old["revision"],
                 "superseded_by": revision, "command_id": command_id})

        self.conn.execute(
            "UPDATE breakdown SET sealed_revision=?, updated_at=?"
            " WHERE tenant_id=? AND breakdown_id=?",
            (revision, now, tenant_id, breakdown_id))

        sealed = self.conn.execute(
            "SELECT * FROM breakdown_revision WHERE tenant_id=? AND"
            " breakdown_id=? AND revision=?",
            (tenant_id, breakdown_id, revision)).fetchone()
        scenes = self.conn.execute(
            "SELECT scene_id FROM scene_revision WHERE tenant_id=? AND"
            " breakdown_id=? AND revision=? ORDER BY scene_order",
            (tenant_id, breakdown_id, revision)).fetchall()
        scene_ids = [item["scene_id"] for item in scenes]
        retired_ids = _loads(sealed["retired_scene_ids"], [])
        # §6.3：SEALED 才发，且只有一条。
        self._emit_revision_created(
            tenant_id, sealed, scene_ids, retired_ids,
            command_id=command_id, home_cell=home_cell)
        self._emit_local(
            tenant_id, LOCAL_EVENT_SEALED, breakdown_id,
            {"breakdown_id": breakdown_id, "revision": revision,
             "content_hash": sealed["content_hash"],
             "scene_ids": scene_ids, "retired_scene_ids": retired_ids,
             "command_id": command_id})
        return {"breakdown_id": breakdown_id, "revision": revision,
                "state": "SEALED", "sealed_at": now}

    def seal(self, tenant_id: str, breakdown_id: str, revision: int,
             body: dict, *, command_id: str, request_hash: str,
             home_cell: str = "local") -> dict:
        def produce():
            breakdown = self.conn.execute(
                "SELECT * FROM breakdown WHERE tenant_id=? AND breakdown_id=?",
                (tenant_id, breakdown_id)).fetchone()
            if breakdown is None:
                raise ServiceError(404, "NOT_FOUND",
                                   f"breakdown {breakdown_id} not found")
            self._seal_revision(tenant_id, breakdown_id, revision, body,
                                command_id=command_id, home_cell=home_cell)
            return self._revision_document(tenant_id, breakdown_id, revision)

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /scenes/{scene_id}:retire ----------------------------------

    def retire_scene(self, tenant_id: str, scene_id: str, body: dict, *,
                     command_id: str, request_hash: str) -> dict:
        def produce():
            row = self.conn.execute(
                "SELECT * FROM scene WHERE tenant_id=? AND scene_id=?",
                (tenant_id, scene_id)).fetchone()
            if row is None:
                raise ServiceError(404, "NOT_FOUND",
                                   f"scene {scene_id} not found")
            reason = body.get("reason") or "removed"
            merged = body.get("merged_into_scene_id")
            if merged is not None:
                target = self.conn.execute(
                    "SELECT episode_id, branch_id FROM scene WHERE tenant_id=?"
                    " AND scene_id=?", (tenant_id, merged)).fetchone()
                if target is None or target["episode_id"] != row["episode_id"] \
                        or target["branch_id"] != row["branch_id"]:
                    raise ServiceError(
                        422, "SCENE_ID_CONFLICT",
                        f"merged_into_scene_id {merged!r} is not a scene in "
                        f"the same episode and branch",
                        details={"merged_into_scene_id": merged})
            changed = self._retire_scene(tenant_id, scene_id, reason, merged)
            if changed:
                self._emit_local(
                    tenant_id, LOCAL_EVENT_RETIRED, scene_id,
                    {"scene_id": scene_id, "reason": reason,
                     "merged_into_scene_id": merged,
                     "command_id": command_id})
            current = self.conn.execute(
                "SELECT * FROM scene WHERE tenant_id=? AND scene_id=?",
                (tenant_id, scene_id)).fetchone()
            # §6.2：并发 retire 同一场，第二次仍是 200，不报错。
            return {"scene_id": scene_id,
                    "retired": current["retired_at"] is not None,
                    "retired_at": current["retired_at"],
                    "retire_reason": current["retire_reason"],
                    "merged_into_scene_id": current["merged_into_scene_id"],
                    "changed": changed,
                    "replayed": not changed}

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST .../scene-order --------------------------------------------

    def reorder(self, tenant_id: str, breakdown_id: str, revision: int,
                body: dict, *, command_id: str, request_hash: str) -> dict:
        """§3.4。只改 `scene_order` / `display_scene`，不改内容、不动 hash。

        S05-G11：允许对 SEALED 重排；SUPERSEDED / WITHDRAWN 拒绝。
        """
        def produce():
            row = self.conn.execute(
                "SELECT * FROM breakdown_revision WHERE tenant_id=? AND"
                " breakdown_id=? AND revision=?",
                (tenant_id, breakdown_id, revision)).fetchone()
            if row is None:
                raise ServiceError(404, "NOT_FOUND",
                                   f"revision {revision} not found")
            if row["state"] in ("SUPERSEDED", "WITHDRAWN"):
                raise ServiceError(
                    409, "REVISION_NOT_ACTIVE",
                    f"revision {revision} is {row['state']}; its order is "
                    f"frozen", details={"state": row["state"]})
            current = [item["scene_id"] for item in self.conn.execute(
                "SELECT scene_id FROM scene_revision WHERE tenant_id=? AND"
                " breakdown_id=? AND revision=?",
                (tenant_id, breakdown_id, revision)).fetchall()]
            wanted = body.get("scene_ids")
            if not isinstance(wanted, list) or not all(
                    isinstance(item, str) for item in wanted):
                raise ServiceError(
                    400, "INVALID_SCENE_ORDER",
                    "scene_ids must be the complete ordered list of scene IDs; "
                    "deltas are refused (S03 anti-pattern 7)",
                    details={"gap": "S05-G2"})
            # 必须完整列表：缺一个、多一个、重一个都拒（T05-20）。
            if len(wanted) != len(current) or set(wanted) != set(current):
                raise ServiceError(
                    400, "INVALID_SCENE_ORDER",
                    "scene_ids must contain every scene in the revision "
                    "exactly once",
                    details={"expected_count": len(current),
                             "received_count": len(set(wanted)),
                             "missing": sorted(set(current) - set(wanted)),
                             "unknown": sorted(set(wanted) - set(current))})
            display_episode = self.conn.execute(
                "SELECT display_episode FROM scene_revision WHERE tenant_id=? AND"
                " breakdown_id=? AND revision=? LIMIT 1",
                (tenant_id, breakdown_id, revision)).fetchone()["display_episode"]
            # SQLite 没有 DEFERRABLE；先整体偏移再写 1..N（S05-G16）。
            self.conn.execute(
                "UPDATE scene_revision SET scene_order=scene_order+?,"
                " display_scene=display_scene+?"
                " WHERE tenant_id=? AND breakdown_id=? AND revision=?",
                (ORDER_OFFSET, ORDER_OFFSET, tenant_id, breakdown_id, revision))
            for position, scene_id in enumerate(wanted, start=1):
                self.conn.execute(
                    "UPDATE scene_revision SET scene_order=?, display_scene=?"
                    " WHERE tenant_id=? AND breakdown_id=? AND revision=?"
                    " AND scene_id=?",
                    (position, position, tenant_id, breakdown_id, revision,
                     scene_id))
            self._emit_local(
                tenant_id, LOCAL_EVENT_REORDERED, breakdown_id,
                {"breakdown_id": breakdown_id, "revision": revision,
                 "scene_ids": list(wanted), "command_id": command_id})
            document = self._revision_document(tenant_id, breakdown_id, revision)
            document["reordered"] = True
            return document

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /scenes/{scene_id}/requirement-resolutions ------------------

    def cache_requirement_resolutions(self, tenant_id: str, scene_id: str,
                                      body: dict, *, command_id: str,
                                      request_hash: str) -> dict:
        """§5.8。**写缓存不改任何权威**：不写 requirement、不进 outbox。"""
        def produce():
            scene = self.conn.execute(
                "SELECT * FROM scene WHERE tenant_id=? AND scene_id=?",
                (tenant_id, scene_id)).fetchone()
            if scene is None:
                raise ServiceError(404, "NOT_FOUND",
                                   f"scene {scene_id} not found")
            breakdown_id = _nonempty_str(body.get("breakdown_id"),
                                         "breakdown_id")
            revision = _int_field(body.get("revision"), "revision", minimum=1)
            items = body.get("resolutions")
            _require(isinstance(items, list) and items,
                     "resolutions must be a non-empty array",
                     code="INVALID_SCHEMA", status=400)
            present = {item["requirement_id"] for item in self.conn.execute(
                "SELECT requirement_id FROM requirement WHERE tenant_id=? AND"
                " breakdown_id=? AND revision=?",
                (tenant_id, breakdown_id, revision)).fetchall()}
            participant = {item["requirement_id"] for item in self.conn.execute(
                "SELECT requirement_id FROM scene_participant WHERE tenant_id=? AND"
                " breakdown_id=? AND revision=? AND scene_id=?",
                (tenant_id, breakdown_id, revision, scene_id)).fetchall()}
            now = _now_dt()
            valid_until = (now + timedelta(
                seconds=RESOLUTION_TTL_SECONDS)).strftime(
                "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            written = 0
            for item in items:
                _require(isinstance(item, dict), "resolutions[] must be objects",
                         code="INVALID_SCHEMA", status=400)
                requirement_id = _nonempty_str(
                    item.get("requirement_id"), "resolutions[].requirement_id")
                if requirement_id not in present or \
                        requirement_id not in participant:
                    raise ServiceError(
                        422, "REQUIREMENT_REF_DANGLING",
                        f"requirement {requirement_id!r} is not required by "
                        f"scene {scene_id} in revision {revision}",
                        details={"requirement_id": requirement_id,
                                 "scene_id": scene_id, "revision": revision})
                self.conn.execute(
                    "INSERT INTO requirement_resolution_cache (tenant_id,"
                    " breakdown_id, revision, requirement_id, resolved_json,"
                    " resolved_at, valid_until) VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT (tenant_id, breakdown_id, revision,"
                    " requirement_id) DO UPDATE SET"
                    " resolved_json=excluded.resolved_json,"
                    " resolved_at=excluded.resolved_at,"
                    " valid_until=excluded.valid_until",
                    (tenant_id, breakdown_id, revision, requirement_id,
                     _jcs(item.get("resolved") or {}),
                     now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                     valid_until))
                written += 1
            return {"scene_id": scene_id, "breakdown_id": breakdown_id,
                    "revision": revision, "cached": written,
                    "authoritative": False, "valid_until": valid_until,
                    "note": "cache only; asset availability must be re-checked "
                            "against S10"}

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- 查询 ------------------------------------------------------------

    def _watermark(self, tenant_id: str, breakdown_id: str) -> str:
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM ("
            " SELECT updated_at AS ts FROM breakdown WHERE tenant_id=? AND"
            "  breakdown_id=?"
            " UNION ALL SELECT created_at FROM breakdown_revision"
            "  WHERE tenant_id=? AND breakdown_id=?"
            " UNION ALL SELECT COALESCE(sealed_at, created_at) FROM"
            "  breakdown_revision WHERE tenant_id=? AND breakdown_id=?)",
            (tenant_id, breakdown_id, tenant_id, breakdown_id, tenant_id,
             breakdown_id)).fetchone()
        return row["ts"] or _now()

    def _revision_document(self, tenant_id: str, breakdown_id: str,
                           revision: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM breakdown_revision WHERE tenant_id=? AND"
            " breakdown_id=? AND revision=?",
            (tenant_id, breakdown_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"revision {revision} not found")
        scene_rows = self.conn.execute(
            "SELECT * FROM scene_revision WHERE tenant_id=? AND breakdown_id=?"
            " AND revision=? ORDER BY scene_order",
            (tenant_id, breakdown_id, revision)).fetchall()
        participants: dict[str, list[dict]] = {}
        for item in self.conn.execute(
                "SELECT * FROM scene_participant WHERE tenant_id=? AND"
                " breakdown_id=? AND revision=? ORDER BY scene_id,"
                " requirement_id",
                (tenant_id, breakdown_id, revision)).fetchall():
            participants.setdefault(item["scene_id"], []).append(
                {"requirement_ref": item["requirement_id"],
                 "speaking": bool(item["speaking"]),
                 "enters": bool(item["enters"]),
                 "exits": bool(item["exits"])})
        requirements = [
            {"requirement_id": item["requirement_id"], "kind": item["kind"],
             "label": item["label"], "spec_hint": _loads(item["spec_hint"], {}),
             "first_scene_id": item["first_scene_id"],
             "notes": item["notes"]}
            for item in self.conn.execute(
                "SELECT * FROM requirement WHERE tenant_id=? AND breakdown_id=?"
                " AND revision=? ORDER BY requirement_id",
                (tenant_id, breakdown_id, revision)).fetchall()]
        relations = [
            {"from_scene_id": item["from_scene_id"],
             "to_scene_id": item["to_scene_id"],
             "relation": item["relation"], "note": item["note"]}
            for item in self.conn.execute(
                "SELECT * FROM scene_relation WHERE tenant_id=? AND"
                " breakdown_id=? AND revision=? ORDER BY from_scene_id,"
                " to_scene_id, relation",
                (tenant_id, breakdown_id, revision)).fetchall()]
        scenes = []
        for item in scene_rows:
            excerpt = None
            if item["opening_line_id"] is not None:
                excerpt = {"line_id": item["opening_line_id"],
                           "text": item["opening_text"],
                           "text_hash": _prefixed(item["opening_text_hash"])}
            scenes.append({
                "scene_id": item["scene_id"],
                "scene_order": item["scene_order"],
                "display_locator": {"episode": item["display_episode"],
                                    "scene": item["display_scene"]},
                "heading": {"place": item["heading_place"],
                            "location_label": item["heading_location"],
                            "time_of_day": item["heading_tod"]},
                "line_span": {"start_line_id": item["start_line_id"],
                              "end_line_id": item["end_line_id"],
                              "line_count": item["line_count"]},
                "opening_excerpt": excerpt,
                "summary": item["summary"],
                "characters": participants.get(item["scene_id"], []),
                "continuity_hints": _loads(item["continuity_hints"], []),
                "advisory": _loads(item["advisory"], {}),
            })
        return {
            "schema_version": SCHEMA_VERSION,
            "breakdown_id": breakdown_id,
            "revision": row["revision"],
            "state": row["state"],
            "episode_id": row["episode_id"],
            "branch_id": row["branch_id"],
            "source_script": {"script_id": row["script_id"],
                              "revision": row["script_revision"],
                              "content_hash": _prefixed(
                                  row["script_content_hash"]),
                              "line_set_hash": _prefixed(
                                  row["script_line_set_hash"])},
            "context": {"context_pack_ref": (
                            None if row["context_pack_id"] is None else
                            {"pack_id": row["context_pack_id"],
                             "content_hash": _prefixed(
                                 row["context_pack_hash"])}),
                        "coverage": row["context_coverage"]},
            "produced_by": _loads(row["produced_by"], {}),
            "parent_revision": row["parent_revision"],
            "scene_id_continuity": row["scene_id_continuity"],
            "scene_count": row["scene_count"],
            "requirement_count": row["requirement_count"],
            "retired_scene_ids": _loads(row["retired_scene_ids"], []),
            "scenes": scenes,
            "requirements": requirements,
            "relations": relations,
            "content_hash": _prefixed(row["content_hash"]),
            "sealed_at": row["sealed_at"],
            "created_at": row["created_at"],
            "authoritative_source": AUTHORITATIVE_SOURCE,
            "projection_watermark": self._watermark(tenant_id, breakdown_id),
            "contract_gaps": CONTRACT_GAPS,
        }

    def get_breakdown(self, tenant_id: str, breakdown_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM breakdown WHERE tenant_id=? AND breakdown_id=?",
            (tenant_id, breakdown_id)).fetchone()
        if row is None:
            # 跨租户同码（E28）：别人的 breakdown 在这里就是不存在。
            raise ServiceError(404, "NOT_FOUND",
                               f"breakdown {breakdown_id} not found")
        revisions = [
            {"revision": item["revision"], "state": item["state"],
             "content_hash": _prefixed(item["content_hash"]),
             "sealed_at": item["sealed_at"], "created_at": item["created_at"]}
            for item in self.conn.execute(
                "SELECT * FROM breakdown_revision WHERE tenant_id=? AND"
                " breakdown_id=? ORDER BY revision",
                (tenant_id, breakdown_id)).fetchall()]
        return {
            "schema_version": SCHEMA_VERSION,
            "breakdown_id": row["breakdown_id"],
            "project_id": row["project_id"],
            "series_id": row["series_id"],
            "episode_id": row["episode_id"],
            "branch_id": row["branch_id"],
            "script_id": row["script_id"],
            "lifecycle": row["lifecycle"],
            "head_revision": row["head_revision"],
            "sealed_revision": row["sealed_revision"],
            "created_by": _loads(row["created_by"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "revisions": revisions,
            "authoritative_source": AUTHORITATIVE_SOURCE,
            "projection_watermark": self._watermark(tenant_id, breakdown_id),
            "contract_gaps": CONTRACT_GAPS,
        }

    def get_revision(self, tenant_id: str, breakdown_id: str,
                     revision: int) -> dict:
        self.get_breakdown(tenant_id, breakdown_id)  # 404 与跨租户同码
        return self._revision_document(tenant_id, breakdown_id, revision)

    def list_episode_scenes(self, tenant_id: str, episode_id: str,
                            branch_id: str | None = None) -> dict:
        """`GET /episodes/{id}/scenes`。**当前有效** = 已 seal 的最新一版。

        §4.3：这个响应是投影，必须带 `authoritative_source`。
        """
        params = [tenant_id, episode_id]
        clause = "tenant_id=? AND episode_id=?"
        if branch_id:
            clause += " AND branch_id=?"
            params.append(branch_id)
        breakdowns = [dict(item) for item in self.conn.execute(
            f"SELECT * FROM breakdown WHERE {clause} AND lifecycle='active'"
            f" ORDER BY updated_at DESC, breakdown_id", params).fetchall()]
        out_scenes = []
        watermarks = []
        for breakdown in breakdowns:
            revision = breakdown["sealed_revision"] or breakdown["head_revision"]
            if not revision:
                continue
            rows = self.conn.execute(
                "SELECT * FROM scene_revision WHERE tenant_id=? AND"
                " breakdown_id=? AND revision=? ORDER BY scene_order",
                (tenant_id, breakdown["breakdown_id"], revision)).fetchall()
            for item in rows:
                scene = self.conn.execute(
                    "SELECT retired_at, retire_reason FROM scene WHERE"
                    " tenant_id=? AND scene_id=?",
                    (tenant_id, item["scene_id"])).fetchone()
                out_scenes.append({
                    "scene_id": item["scene_id"],
                    "breakdown_id": breakdown["breakdown_id"],
                    "revision": revision,
                    "scene_order": item["scene_order"],
                    "display_locator": {"episode": item["display_episode"],
                                        "scene": item["display_scene"]},
                    "heading": {"place": item["heading_place"],
                                "location_label": item["heading_location"],
                                "time_of_day": item["heading_tod"]},
                    "line_span": {"start_line_id": item["start_line_id"],
                                  "end_line_id": item["end_line_id"],
                                  "line_count": item["line_count"]},
                    "summary": item["summary"],
                    "retired": scene["retired_at"] is not None,
                })
            watermarks.append(self._watermark(tenant_id,
                                              breakdown["breakdown_id"]))
        return {"episode_id": episode_id, "branch_id": branch_id,
                "scenes": out_scenes,
                "authoritative_source": AUTHORITATIVE_SOURCE,
                "projection_watermark": max(watermarks) if watermarks else _now(),
                "contract_gaps": CONTRACT_GAPS}

    def get_scene(self, tenant_id: str, scene_id: str) -> dict:
        """跨 revision 的身份。历史 revision 仍能解析到它（§3.5.3）。"""
        row = self.conn.execute(
            "SELECT * FROM scene WHERE tenant_id=? AND scene_id=?",
            (tenant_id, scene_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", f"scene {scene_id} not found")
        appearances = []
        watermarks = [row["created_at"]]
        for item in self.conn.execute(
                "SELECT * FROM scene_revision WHERE tenant_id=? AND scene_id=?"
                " ORDER BY revision DESC",
                (tenant_id, scene_id)).fetchall():
            appearances.append({
                "breakdown_id": item["breakdown_id"],
                "revision": item["revision"],
                "scene_order": item["scene_order"],
                "line_span": {"start_line_id": item["start_line_id"],
                              "end_line_id": item["end_line_id"],
                              "line_count": item["line_count"]},
                "summary": item["summary"]})
            # `scene_revision` 没有自己的 created_at；改以所属 revision 的
            # sealed_at / created_at 作为水位，避免凭空造一列。
            revision_row = self.conn.execute(
                "SELECT COALESCE(sealed_at, created_at) AS ts FROM"
                " breakdown_revision WHERE tenant_id=? AND breakdown_id=?"
                " AND revision=?",
                (tenant_id, item["breakdown_id"],
                 item["revision"])).fetchone()
            if revision_row is not None and revision_row["ts"]:
                watermarks.append(revision_row["ts"])
        return {"schema_version": SCHEMA_VERSION, "scene_id": scene_id,
                "project_id": row["project_id"], "series_id": row["series_id"],
                "episode_id": row["episode_id"],
                "branch_id": row["branch_id"],
                "first_breakdown_id": row["first_breakdown_id"],
                "first_revision": row["first_revision"],
                "retired": row["retired_at"] is not None,
                "retired_at": row["retired_at"],
                "retire_reason": row["retire_reason"],
                "merged_into_scene_id": row["merged_into_scene_id"],
                "revisions": appearances,
                "created_at": row["created_at"],
                "authoritative_source": AUTHORITATIVE_SOURCE,
                "projection_watermark": max(watermarks),
                "contract_gaps": CONTRACT_GAPS}

    def get_scene_requires(self, tenant_id: str, scene_id: str,
                           revision: int | None = None) -> dict:
        """S10 的输入：**需求**清单，不是绑定（§1.3 / §10.4）。"""
        scene = self.conn.execute(
            "SELECT * FROM scene WHERE tenant_id=? AND scene_id=?",
            (tenant_id, scene_id)).fetchone()
        if scene is None:
            raise ServiceError(404, "NOT_FOUND", f"scene {scene_id} not found")
        if revision is None:
            row = self.conn.execute(
                "SELECT * FROM scene_revision WHERE tenant_id=? AND scene_id=?"
                " ORDER BY revision DESC LIMIT 1",
                (tenant_id, scene_id)).fetchone()
            if row is None:
                raise ServiceError(
                    404, "NOT_FOUND",
                    f"scene {scene_id} appears in no breakdown revision")
            revision = row["revision"]
            breakdown_id = row["breakdown_id"]
        else:
            row = self.conn.execute(
                "SELECT * FROM scene_revision WHERE tenant_id=? AND scene_id=?"
                " AND revision=?", (tenant_id, scene_id, revision)).fetchone()
            if row is None:
                raise ServiceError(
                    404, "NOT_FOUND",
                    f"scene {scene_id} does not appear in revision {revision}")
            breakdown_id = row["breakdown_id"]
        requirements = [
            {"requirement_id": item["requirement_id"], "kind": item["kind"],
             "label": item["label"], "spec_hint": _loads(item["spec_hint"], {}),
             "notes": item["notes"]}
            for item in self.conn.execute(
                "SELECT r.* FROM requirement r JOIN scene_participant p"
                "  ON p.tenant_id=r.tenant_id AND p.breakdown_id=r.breakdown_id"
                "  AND p.revision=r.revision AND p.requirement_id=r.requirement_id"
                " WHERE r.tenant_id=? AND r.breakdown_id=? AND r.revision=?"
                "  AND p.scene_id=? ORDER BY r.kind, r.requirement_id",
                (tenant_id, breakdown_id, revision, scene_id)).fetchall()]
        # 缓存单独放、单独标：任何判"资产可用"的调用方必须忽略它（§5.8）。
        cached = {}
        for item in self.conn.execute(
                "SELECT * FROM requirement_resolution_cache WHERE tenant_id=? AND"
                " breakdown_id=? AND revision=?",
                (tenant_id, breakdown_id, revision)).fetchall():
            cached[item["requirement_id"]] = {
                "resolved": _loads(item["resolved_json"], {}),
                "resolved_at": item["resolved_at"],
                "valid_until": item["valid_until"]}
        return {"scene_id": scene_id, "breakdown_id": breakdown_id,
                "revision": revision, "requirements": requirements,
                "requirement_count": len(requirements),
                "resolution_cache": cached,
                "resolution_cache_authoritative": False,
                "authoritative_source": AUTHORITATIVE_SOURCE,
                "projection_watermark": self._watermark(tenant_id, breakdown_id),
                "contract_gaps": CONTRACT_GAPS}
