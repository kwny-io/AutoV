"""S04 内核。方案：`plans/S04-narrative.md`。

四条不能违反的语义，都在这里落地：

1. **没有 `accepted` 状态。** 剧本"被接受"不是 script 或 script_revision 的
   状态，而是 S08 某个 manifest revision 的 accepted pointer（§3.2、§3.4）。
   同一个 script revision 可能在分支 A 已接受、分支 B 未接受 —— 状态机放不下
   这个事实。本模块从不写、不读任何 accepted 列，因为它不存在。
2. **seal 之后内容不可变。** 唯一的例外是状态字段（SEALED → SUPERSEDED /
   WITHDRAWN）与 `sealed_at`。改一句台词（§4.5）只写新的 `line_revision`，
   **不碰任何已 seal 的 `script_revision`**。
3. **`content_hash` 只由内容决定。** 不含 `author` / `created_at` / `state`
   （§2.2 行 218）。任何"顺手加个字段进 hash"的改动都会让已提交 manifest 的
   引用失效，S16 的 R1 会全错。
4. **事件只表示"可被引用的版本出现了"。** DRAFT 不发事件（§5.3）。行级事件与
   撤回事件没有登记，写 `local_event` 而不是 outbox（C04-2 / NAR-7）。

撤回守卫与接受判断**分在两个模块**：本文件只有 `apply_manifest_committed()`
维护投影，`projection.py` 只做守卫读取。§4.4 明确要求这一点，否则重构时很容易
把投影读成"接受状态"。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Inbox, Outbox, new_event
from idempotency_sdk import SqliteIdempotencyStore

from .schema import (
    ARC_DECISIONS,
    ARC_STATES,
    ARC_TRANSITIONS,
    BRIEF_BODY_KEYS,
    CONTRACT_GAPS,
    EXTRACTION_STATES,
    ID_PREFIX,
    MAX_BATCH_IDS,
    MAX_BODY_BYTES,
    MAX_CHUNK_BYTES,
    MAX_LIST_LIMIT,
    REFERENCE_MAX_LAG_SECONDS,
    REVISION_STATES,
    SCHEMA,
    SCRIPT_KINDS,
)

SOURCE = "S04"
TOPIC = "drama.narrative.v2"
SCHEMA_VERSION = "2.0"

EVENT_REVISION_CREATED = "narrative.script.revision.created"
# 未登记（C04-2 / §5.3）：写 local_event，不进 outbox。
EVENT_LINE_REVISION_CREATED = "narrative.line.revision.created"
EVENT_SCRIPT_WITHDRAWN = "narrative.script.withdrawn"
# 我消费的唯一外部事件（S08）。只在撤回守卫的投影里用，不是"接受判定"。
MANIFEST_COMMITTED = "snapshot.manifest.committed"

LOCAL_EVENT_TYPES = (EVENT_LINE_REVISION_CREATED, EVENT_SCRIPT_WITHDRAWN)

# 正文存储的私有前缀（§2.6：`bytes_ref` 必须带租户前缀，不可跨租户猜 key）。
BODY_KEY = "s04/{tenant_id}/{script_id}/r{revision}/c{chunk_no}"

# §6.2 锁序，写在这里只是让换 Postgres 时不需要重排代码：
# script → line → script_revision → line_revision → body_chunk → outbox


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


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。不嵌套：一个命令一个事务。

    已经在一个事务里时退化成空壳（`nested=True`）：`consume_manifest_event`
    要保证 inbox 行与投影同进同出，而 `apply_manifest_committed` 单独调用时
    又必须自己开事务。真正的修复是给事件入口一条独立路径，但那会让"投影
    写入"有两个事务边界；让内层跟随外层更不容易写错。
    """

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
            # §5.4 `503 NARRATIVE_UNAVAILABLE`：主库不可写（只读副本、磁盘满、
            # 锁超时）。`retryable` 为真 —— 写不进去不等于请求有错。
            raise ServiceError(
                503, "NARRATIVE_UNAVAILABLE",
                f"narrative database is not writable: {exc}",
                retryable=True) from exc
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.nested:
            # 内层不提交也不回滚：提交/回滚由外层决定。
            return False
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------------ 内容寻址
# §11.1 第 2 步要求 `platform/content-address-sdk` 与 S05/S06/S08 共用。仓库里
# 还没有这个 SDK，本文件先落地算法；一旦共用 SDK 出现，必须整段替换，不能让
# 两个服务各留一份（同一份剧本在四个服务算出四个 hash 是 R1 全错的形态）。

def line_set_digest(rows) -> str:
    """`line_set_hash`。§2.3 行 284。

    每行编成 `"{line_id}:{line_revision}:{text_hash}"`，**按整串字典序排序**
    再 `\\n` 连接后 sha256（S04-G3 说方案只写了 `sorted([...])`，没说行内
    分隔符与排序键，这里定死，必须与 S05/S06/S08 一致）。

    排序后 join 的意义：行顺序变化只体现在 `seq`，不改变"这版包含哪些内容"。
    """
    encoded = sorted(f"{line_id}:{revision}:{text_hash}"
                     for line_id, revision, text_hash in rows)
    return _sha256_hex("\n".join(encoded))


def revision_content_digest(*, script_id: str, revision: int, title,
                            language: str, structure: dict,
                            parent_revision, line_set_hash: str) -> str:
    """`content_hash`。§2.2 行 218。

    `sha256(JCS({script_id, revision, title, language, structure,
    parent_revision}) + "\\n" + line_set_hash)`。

    **不含 `author` / `created_at` / `state`**：前两个是元数据，改了不该让内容
    hash 变；`state` 不是内容。这条必须与 S08 `source_revisions[].content_hash`
    逐字节一致，否则 S16 的 R1 全部失败。
    """
    header = {"script_id": script_id, "revision": revision, "title": title,
              "language": language, "structure": structure,
              "parent_revision": parent_revision}
    return _sha256_hex(_jcs(header) + "\n" + line_set_hash)


def structure_digest(structure: dict) -> str:
    """`structure_hash`（事件字段）。NAR-2：方案只出现一次、无算法、无 DDL 列。

    本实现取 `sha256(JCS(structure))`。**下游无法用它复算**，因为事件不含
    `structure` —— 这是登记的缺口，不是"已经解决"。
    """
    return _sha256_hex(_jcs(structure))


def text_digest(text: str) -> str:
    """`text_hash = sha256(utf8(text))`（§2.3 行 246）。逐字节是权威。"""
    return _sha256_hex(text)


# ------------------------------------------------------------------ 校验

def _require(condition, message: str, code: str = "INVALID_BODY",
             status: int = 400, details=None) -> None:
    if not condition:
        raise ServiceError(status, code, message, details=details or {})


def _int_field(body: dict, key: str, *, required: bool, minimum: int = 0,
               code: str = "INVALID_BODY"):
    value = body.get(key)
    if value is None:
        if required:
            raise ServiceError(400, code, f"{key} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(400, code, f"{key} must be an integer")
    if value < minimum:
        raise ServiceError(400, code, f"{key} must be >= {minimum}")
    return value


def _principal(body) -> dict:
    """`author` / `reviewed_by` 的形状（§2.2 行 200）。

    `principal_id` 与 `principal_type` 必须有；`definition_revision` 可选。
    未知键拒绝 —— 审计字段里多一个拼错的键，事后就是"查不到是谁"。
    """
    _require(isinstance(body, dict), "author must be an object")
    unknown = set(body) - {"principal_id", "principal_type",
                           "definition_revision"}
    _require(not unknown, f"unknown author keys: {sorted(unknown)}")
    principal_id = body.get("principal_id")
    principal_type = body.get("principal_type")
    _require(isinstance(principal_id, str) and principal_id,
             "author.principal_id is required")
    _require(principal_type in ("human", "agent"),
             "author.principal_type must be human or agent")
    out = {"principal_id": principal_id, "principal_type": principal_type}
    if body.get("definition_revision") is not None:
        out["definition_revision"] = body["definition_revision"]
    return out


def _produced_by(body):
    """`produced_by` 的形状。

    NAR-1：方案三处键集不同，事件 schema 接受**五个键的并集且全部可选**。
    这里接受并集，不要求具体键；但非 null 时 `task_id` 必须存在，否则
    §4.6 第 16 条的回查无从做起（见 `_verify_agent_task`）。
    """
    if body is None:
        return None
    _require(isinstance(body, dict), "produced_by must be an object or null")
    allowed = {"task_id", "agent_task_ref", "definition_revision",
               "context_pack_ref", "model_ref"}
    unknown = set(body) - allowed
    _require(not unknown, f"unknown produced_by keys: {sorted(unknown)}")
    return {k: body[k] for k in sorted(body) if body[k] is not None}


def _normalize_structure(structure) -> dict:
    """`structure` 的固定形状（§2.2 行 197）。

    `scene_markers` 是**文本标记**，不是 `scene_id`（§2.2 行 216）。`line_count`
    与 `word_count` 允许缺省，因为 seal 时以 `script_revision_line` 的实际行数
    覆盖 `line_count`（§3.1：DRAFT→SEALED 不再写正文）。
    """
    _require(isinstance(structure, dict), "structure must be an object")
    unknown = set(structure) - {"scene_markers", "characters", "line_count",
                               "word_count"}
    _require(not unknown, f"unknown structure keys: {sorted(unknown)}")
    markers = structure.get("scene_markers", [])
    _require(isinstance(markers, list), "structure.scene_markers must be a list")
    for marker in markers:
        _require(isinstance(marker, str) and marker,
                 "structure.scene_markers entries must be non-empty strings")
    characters = structure.get("characters", [])
    _require(isinstance(characters, list),
             "structure.characters must be a list")
    for name in characters:
        _require(isinstance(name, str),
                 "structure.characters entries must be strings")
    out = {"scene_markers": list(markers), "characters": list(characters)}
    for key in ("line_count", "word_count"):
        if structure.get(key) is not None:
            value = structure[key]
            _require(isinstance(value, int) and not isinstance(value, bool)
                     and value >= 0, f"structure.{key} must be a non-negative "
                                     f"integer")
            out[key] = value
    return out


LINE_KEYS = ("line_id", "text", "speaker_id", "speaker_label",
             "parenthetical", "locale", "scene_marker", "order_in_scene")


def _normalize_lines(lines) -> list[dict]:
    """`lines[]` 的元素形状（§4.1 行 494）。

    `provided` 记录调用方**显式给了哪些键**：已有行只对比显式给出的字段。
    否则一次"只改 order_in_scene"的提交会因为没带 speaker_label 被误判成
    speaker 变更 —— 缺省不等于置空。
    """
    _require(isinstance(lines, list), "lines must be a list")
    out: list[dict] = []
    for index, entry in enumerate(lines):
        _require(isinstance(entry, dict), f"lines[{index}] must be an object")
        unknown = set(entry) - set(LINE_KEYS)
        _require(not unknown,
                 f"lines[{index}] has unknown keys: {sorted(unknown)}")
        text = entry.get("text")
        _require(isinstance(text, str) and text,
                 f"lines[{index}].text is required")
        line_id = entry.get("line_id")
        _require(line_id is None or (isinstance(line_id, str) and line_id),
                 f"lines[{index}].line_id must be a string or null")
        order = entry.get("order_in_scene")
        if order is not None:
            _require(isinstance(order, int) and not isinstance(order, bool),
                     f"lines[{index}].order_in_scene must be an integer")
        # §4.6 第 10 条：新建行二选一，不能都空。已有行的缺省字段由库里
        # 那一版补，所以这里只校验"新建"的情况。
        if line_id is None:
            _require(bool(entry.get("speaker_id"))
                     or bool(entry.get("speaker_label")),
                     f"lines[{index}] needs speaker_id or speaker_label",
                     code="SPEAKER_REQUIRED", status=422)
        out.append({
            "line_id": line_id,
            "text": text,
            "speaker_id": entry.get("speaker_id"),
            "speaker_label": entry.get("speaker_label"),
            "parenthetical": entry.get("parenthetical"),
            "locale": entry.get("locale"),
            "scene_marker": entry.get("scene_marker"),
            "order_in_scene": order,
            "provided": set(entry) & set(LINE_KEYS),
        })
    return out


def _brief_body(body) -> dict:
    """`brief_revision.body`：键集固定，`additionalProperties: false`
    （§2.1 行 160）。少键允许、未知键拒绝，因为新增键要发版。"""
    _require(isinstance(body, dict), "brief body must be an object")
    unknown = set(body) - set(BRIEF_BODY_KEYS)
    _require(not unknown, f"unknown brief keys: {sorted(unknown)}")
    out = {}
    for key in BRIEF_BODY_KEYS:
        if key in body:
            out[key] = body[key]
    for key in ("constraints", "references"):
        if key in out:
            _require(isinstance(out[key], list), f"brief.{key} must be a list")
    for key in ("logline", "genre", "tone", "audience"):
        if key in out:
            _require(isinstance(out[key], str), f"brief.{key} must be a string")
    return out


class NarrativeService:
    def __init__(self, conn: sqlite3.Connection, *, task_source=None,
                 body_store=None, catalog_source=None,
                 reference_reader=None, enable_writes: bool = True):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        # executescript 会隐式提交，所以表必须在任何事务之外建。
        self.conn.executescript(SCHEMA)
        # `idempotency_record` 由平台 SDK 建表（schema.py 不重复 DDL）。直接
        # 调内核（测试、内部调用）没有 HTTP 层的 `begin()`，表可能还不存在，
        # 所以这里建一次 —— `SqliteIdempotencyStore` 的 DDL 是 IF NOT EXISTS。
        self.idem = SqliteIdempotencyStore(self.conn)
        self.outbox = Outbox(self.conn)
        self._task_source = task_source
        self._body_store = body_store
        self._catalog = catalog_source
        self._reference_reader = reference_reader
        self._writes = enable_writes

    # ---- 命令外壳 --------------------------------------------------------

    def _command(self, tenant_id: str, command_id: str, produce,
                 request_hash: str = ""):
        """一个命令 = 一个事务，幂等结果在同一事务内写回。

        `result` 与业务写入一起提交（E12 的"事实与幂等结果不分家"）。
        用 upsert 而不是 update：HTTP 层已经 `begin()` 插过 IN_PROGRESS 行，
        直接调内核（测试、内部调用）则没有 —— 两条路都要留下 DONE 记录。
        冲突时只覆盖 `result`，`request_hash` 保留首次写入的那个。
        """
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "INSERT INTO idempotency_record (tenant_id, command_id,"
                " request_hash, result, created_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT (tenant_id, command_id) DO UPDATE SET"
                " result=excluded.result",
                (tenant_id, command_id, request_hash, _jcs(result), _now()))
            return result

    def _upstream(self, call, code: str, *, empty_ok=False):
        """把"上游不可达"统一成 503，**不给 degraded default**（§9 第 14 条）。

        `ConnectionError` 是适配层对"连不上"的约定；`ServiceError` 的 5xx
        也归到这里，让超时与"上游说它不行"同形。
        """
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
        if result is None and not empty_ok:
            raise ServiceError(503, code, "upstream returned no result",
                               retryable=True)
        return result

    # ---- 锁序（§6.2）-----------------------------------------------------

    def _lock_script(self, tenant_id: str, script_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM script WHERE tenant_id=? AND script_id=?",
            (tenant_id, script_id)).fetchone()
        if row is None:
            # 跨租户同码（E28）：别的租户的 script 在这里看起来就是不存在。
            raise ServiceError(404, "SCRIPT_NOT_FOUND",
                               f"script {script_id} not found")
        return row

    def _lock_line(self, tenant_id: str, line_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM line WHERE tenant_id=? AND line_id=?",
            (tenant_id, line_id)).fetchone()
        if row is None:
            raise ServiceError(404, "LINE_NOT_FOUND",
                               f"line {line_id} not found")
        return row

    @staticmethod
    def _cas(current: int, expected, what: str) -> None:
        if expected is None:
            return
        if int(current) != int(expected):
            raise ServiceError(
                409, "REVISION_CONFLICT",
                f"{what} is at revision {current}, expected {expected}",
                details={"current_revision": int(current),
                         "expected_revision": int(expected)})

    # ---- 事件 -------------------------------------------------------------

    def _next_sequence(self, tenant_id: str, aggregate_id: str) -> int:
        """§2.6 要求 (tenant, aggregate, sequence) 唯一，SDK 的 outbox 没有
        这条约束（S04-G5），所以序号在写事务内从 outbox 自己算。"""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS s FROM outbox"
            " WHERE tenant_id=? AND aggregate_id=?",
            (tenant_id, aggregate_id)).fetchone()
        return int(row["s"]) + 1

    def _emit_local(self, tenant_id: str, event_type: str,
                    aggregate_id: str, payload: dict) -> None:
        """未登记的事件走本地表，不走 outbox（C04-2 / NAR-7）。

        把未注册的 event_type 塞进 outbox，等于让中继去投递一个没有 schema
        的事件：消费方按契约校验会失败，而且没人知道该找谁。
        """
        self.conn.execute(
            "INSERT INTO local_event (tenant_id, local_event_id, event_type,"
            " aggregate_id, payload, created_at) VALUES (?,?,?,?,?,?)",
            (tenant_id, _new_id("local_event"), event_type, aggregate_id,
             _jcs(payload), _now()))

    def _emit_revision_created(self, tenant_id: str, row: sqlite3.Row,
                               *, command_id: str, home_cell: str,
                               line_count: int,
                               structure: dict) -> None:
        """`narrative.script.revision.created` —— **seal 时**一条（§5.3）。

        `data` 是 13 个键、`additionalProperties:false`，不含 `state`
        （NAR-7）、不含正文（C04-7）。`episode_id` 对 `series_outline` 是
        null —— 那会让事件不合 schema，这是 NAR-5 登记的冲突，不伪造。
        """
        sequence = self._next_sequence(tenant_id, row["script_id"])
        payload = {
            "schema_version": SCHEMA_VERSION,
            "script_id": row["script_id"],
            "revision": row["revision"],
            "episode_id": row["episode_id"],
            "series_id": row["series_id"],
            "content_hash": row["content_hash"],
            "line_set_hash": row["line_set_hash"],
            "line_count": line_count,
            "scene_markers": structure.get("scene_markers", []),
            "structure_hash": structure_digest(structure),
            "author": _loads(row["author"], {}),
            "produced_by": _loads(row["produced_by"], None),
            "parent_revision": row["parent_revision"],
        }
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_REVISION_CREATED,
            subject=f"script/{row['script_id']}/revision/{row['revision']}",
            tenant_id=tenant_id, home_cell=home_cell,
            aggregate_id=row["script_id"], sequence=sequence,
            correlation_id=command_id, causation_id=command_id,
            data=payload,
            data_ref={"service_id": SOURCE, "id": row["script_id"],
                      "revision": row["revision"],
                      "content_hash": row["content_hash"]}))

    # ---- 上游校验 ---------------------------------------------------------

    def _verify_agent_task(self, tenant_id: str, produced_by) -> None:
        """§4.6 第 16 条：`produced_by.task_id` 必须回查 S19。

        不信任客户端自报（§9 第 14 条）。未配置即 503 —— "查不了"不能等于
        "通过"。返回的任务不属于本租户时 S19 按跨租户同码给 404，这里原样
        转成 `UNKNOWN_AGENT_TASK` 的细节，不泄露任务存在性。
        """
        if produced_by is None:
            return
        task_id = produced_by.get("task_id")
        if not task_id:
            raise ServiceError(
                422, "UNKNOWN_AGENT_TASK",
                "produced_by without task_id cannot be verified against S19",
                details={"gap": "NAR-1", "produced_by": produced_by})
        if self._task_source is None:
            raise ServiceError(
                503, "AGENT_TASK_SOURCE_UNAVAILABLE",
                "no S19 task source is configured; S04 will not accept an "
                "unverifiable produced_by", retryable=True)
        try:
            task = self._upstream(
                lambda: self._task_source(tenant_id, task_id),
                "AGENT_TASK_SOURCE_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status == 404:
                raise ServiceError(
                    422, "UNKNOWN_AGENT_TASK",
                    f"task {task_id} is not visible in this tenant",
                    details={"task_id": task_id}) from exc
            raise
        _require(isinstance(task, dict), "S19 returned no task body")
        _require(task.get("task_id") in (None, task_id),
                 "S19 returned a different task_id", code="UNKNOWN_AGENT_TASK",
                 status=422)

    def _verify_episode(self, tenant_id: str, series_id: str,
                        episode_id) -> None:
        """S04-G10：`episode_id` 属于 `series_id` 没有契约保证。

        给了 episode 就回查 S03；未配置即 503（不能"没校验就当通过"）。
        `series_id` 本身只能当不透明引用存 —— 仓库里没有 series 读接口。
        """
        if not episode_id:
            return
        if self._catalog is None:
            raise ServiceError(
                503, "CATALOG_UNAVAILABLE",
                "no S03 catalog source is configured; S04 will not link an "
                "episode it cannot verify", retryable=True)
        try:
            episode = self._upstream(
                lambda: self._catalog(tenant_id, episode_id),
                "CATALOG_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status == 404:
                raise ServiceError(
                    404, "EPISODE_NOT_FOUND",
                    f"episode {episode_id} not found",
                    details={"episode_id": episode_id}) from exc
            raise
        _require(isinstance(episode, dict), "S03 returned no episode body")
        actual = episode.get("series_id")
        if actual is not None and actual != series_id:
            raise ServiceError(
                422, "EPISODE_SERIES_MISMATCH",
                f"episode {episode_id} belongs to series {actual}, not "
                f"{series_id}",
                details={"episode_id": episode_id, "series_id": series_id,
                         "actual_series_id": actual})

    # ---- 正文存储 ---------------------------------------------------------

    def _store_body(self, tenant_id: str, script_id: str, revision: int,
                    body: str) -> list[dict]:
        """把正文切块写对象存储，返回待写的 `body_chunk` 行（§2.6）。

        内容寻址：同一正文落在同一个 key 上，重写同内容不产生新字节。
        失败抛 `ServiceError(500)`，事务回滚 → 主库无行（§4.6 第 11 条）。
        分块已写、主库提交失败的孤儿由 `collect_orphan_chunks()` 回收。
        """
        if self._body_store is None:
            raise ServiceError(
                503, "BODY_STORE_UNAVAILABLE",
                "no body store is configured; S04 will not accept a revision "
                "whose text cannot be read back", retryable=True)
        encoded = body.encode("utf-8")
        if len(encoded) > MAX_BODY_BYTES:
            raise ServiceError(
                413, "BODY_TOO_LARGE",
                f"body is {len(encoded)} bytes, limit is {MAX_BODY_BYTES}",
                details={"byte_size": len(encoded),
                         "limit": MAX_BODY_BYTES, "gap": "S04-G2"})
        chunks: list[dict] = []
        for chunk_no, start in enumerate(range(0, max(len(encoded), 1),
                                              MAX_CHUNK_BYTES)):
            payload = encoded[start:start + MAX_CHUNK_BYTES]
            key = BODY_KEY.format(tenant_id=tenant_id, script_id=script_id,
                                  revision=revision, chunk_no=chunk_no)
            try:
                self._body_store.put(key, payload)
            except (ConnectionError, OSError) as exc:
                raise ServiceError(
                    500, "BODY_STORE_WRITE_FAILED",
                    f"body store rejected chunk {chunk_no}: {exc}",
                    retryable=True) from exc
            chunks.append({"chunk_no": chunk_no, "bytes_ref": key,
                           "sha256": hashlib.sha256(payload).hexdigest(),
                           "byte_size": len(payload)})
        return chunks

    def _read_body(self, tenant_id: str, script_id: str, revision: int,
                   chunks: list[sqlite3.Row]) -> dict:
        """读正文分块。**读失败必须显式 `body_unavailable`，绝不返回空串**
        （§4.6 第 18 条）。

        空字符串会让 S05 切出 0 场、一路"成功"地生成空集；一个显式的不可用
        标记至少能让下游停下来。
        """
        if self._body_store is None:
            return {"body": None, "body_unavailable": True,
                    "chunks": [dict(chunk) for chunk in chunks]}
        pieces: list[bytes] = []
        for chunk in chunks:
            try:
                piece = self._body_store.get(chunk["bytes_ref"])
            except (ConnectionError, OSError, KeyError):
                return {"body": None, "body_unavailable": True,
                        "chunks": [dict(c) for c in chunks]}
            if piece is None:
                return {"body": None, "body_unavailable": True,
                        "chunks": [dict(c) for c in chunks]}
            if not isinstance(piece, (bytes, bytearray)):
                piece = str(piece).encode("utf-8")
            if hashlib.sha256(piece).hexdigest() != chunk["sha256"]:
                # 字节与索引不符 = 读到的不是那一块。同样不可用。
                return {"body": None, "body_unavailable": True,
                        "chunks": [dict(c) for c in chunks]}
            pieces.append(bytes(piece))
        try:
            text = b"".join(pieces).decode("utf-8")
        except UnicodeDecodeError:
            return {"body": None, "body_unavailable": True,
                    "chunks": [dict(c) for c in chunks]}
        return {"body": text, "body_unavailable": False,
                "chunks": [dict(c) for c in chunks]}

    # ==================================================================
    # 命令（§5.1）
    # ==================================================================

    # ---- POST /scripts ----------------------------------------------------

    def create_script(self, tenant_id: str, body: dict, *,
                      command_id: str, request_hash: str) -> dict:
        def produce():
            series_id = body.get("series_id")
            _require(isinstance(series_id, str) and series_id,
                     "series_id is required")
            kind = body.get("script_kind")
            _require(kind in SCRIPT_KINDS,
                     f"script_kind must be one of {list(SCRIPT_KINDS)}")
            episode_id = body.get("episode_id")
            if kind == "episode_script":
                _require(bool(episode_id),
                         "episode_script requires episode_id")
            elif kind == "series_outline":
                _require(not episode_id,
                         "series_outline must not carry episode_id")
            language = body.get("language") or "zh-CN"
            _require(isinstance(language, str) and language,
                     "language must be a non-empty string")
            author = _principal(body.get("author"))
            self._verify_episode(tenant_id, series_id, episode_id)

            script_id = _new_id("script")
            self.conn.execute(
                "INSERT INTO script (tenant_id, script_id, series_id,"
                " episode_id, script_kind, language, head_revision,"
                " created_at) VALUES (?,?,?,?,?,?,0,?)",
                (tenant_id, script_id, series_id, episode_id, kind, language,
                 _now()))
            return self._script_dict(tenant_id, script_id)

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /scripts/{id}/revisions -------------------------------------

    def append_revision(self, tenant_id: str, script_id: str, body: dict, *,
                        command_id: str, request_hash: str,
                        home_cell: str = "local") -> dict:
        """§4.1（人）/ §4.2（Agent）共用同一条命令。

        DRAFT **不发事件**（§5.3）。Agent 的产出永远是 DRAFT；这里显式拒绝
        `state != DRAFT`，而不是静默改写成 DRAFT —— 客户端以为它 seal 了，
        与客户端知道它没 seal 是两件不同的事。
        """
        def produce():
            script = self._lock_script(tenant_id, script_id)
            self._cas(script["head_revision"],
                      body.get("expected_head_revision"),
                      f"script {script_id}")

            state = body.get("state", "DRAFT")
            _require(state == "DRAFT",
                     "POST /revisions only creates DRAFT; seal is a separate "
                     "command", code="ILLEGAL_STATE_TRANSITION", status=409)

            author = _principal(body.get("author"))
            produced_by = _produced_by(body.get("produced_by"))
            # Agent 的产出必须可核验；人写允许 null（NAR-6）。
            if author["principal_type"] == "agent":
                _require(produced_by is not None,
                         "agent-authored revisions require produced_by",
                         code="UNKNOWN_AGENT_TASK", status=422)
                self._verify_agent_task(tenant_id, produced_by)
            elif produced_by is not None:
                self._verify_agent_task(tenant_id, produced_by)

            parent_revision = _int_field(body, "parent_revision",
                                         required=False, minimum=1)
            head = int(script["head_revision"])
            if parent_revision is not None and parent_revision > head:
                raise ServiceError(
                    422, "INVALID_BODY",
                    f"parent_revision {parent_revision} is ahead of head "
                    f"{head}")

            title = body.get("title")
            if title is not None:
                _require(isinstance(title, str), "title must be a string")
            text = body.get("body")
            if text is None and body.get("body_chunks") is not None:
                _require(isinstance(body["body_chunks"], list),
                         "body_chunks must be a list of strings")
                text = "".join(str(chunk) for chunk in body["body_chunks"])
            _require(isinstance(text, str), "body (or body_chunks) is required")
            structure = _normalize_structure(body.get("structure") or {})
            lines = _normalize_lines(body.get("lines") or [])

            revision = head + 1
            # 行处理在写 script_revision 之前：拒绝条件不能留下半行数据
            # （事务回滚也会，但早拒绝省一次对象存储往返）。
            prepared = self._prepare_lines(tenant_id, script_id, lines)
            # `line_count` 以实际快照为准，覆盖调用方给的值 —— 它进
            # content_hash，若允许自报就会出现"两个 hash 指向同一份行集合"。
            structure["line_count"] = len(prepared)
            line_set_hash = line_set_digest(
                (entry["line_id"], entry["line_revision"], entry["text_hash"])
                for entry in prepared)
            content_hash = revision_content_digest(
                script_id=script_id, revision=revision, title=title,
                language=script["language"], structure=structure,
                parent_revision=parent_revision,
                line_set_hash=line_set_hash)

            # UNIQUE (tenant, script, content_hash)：同一内容不重复建版本。
            duplicate = self.conn.execute(
                "SELECT revision FROM script_revision WHERE tenant_id=? AND"
                " script_id=? AND content_hash=?",
                (tenant_id, script_id, content_hash)).fetchone()
            if duplicate is not None:
                return {"script_id": script_id,
                        "revision": duplicate["revision"],
                        "state": "DRAFT", "deduplicated": True,
                        "content_hash": content_hash}

            chunks = self._store_body(tenant_id, script_id, revision, text)
            body_ref = (f"{script_id}/r{revision}")
            self.conn.execute(
                "INSERT INTO script_revision (tenant_id, script_id, revision,"
                " state, title, body_ref, body_bytes, structure,"
                " line_set_hash, content_hash, author, produced_by,"
                " parent_revision, created_at)"
                " VALUES (?,?,?,'DRAFT',?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, script_id, revision, title, body_ref,
                 sum(chunk["byte_size"] for chunk in chunks),
                 _jcs(structure), line_set_hash, content_hash,
                 _jcs(author), None if produced_by is None else _jcs(produced_by),
                 parent_revision, _now()))
            for chunk in chunks:
                self.conn.execute(
                    "INSERT INTO body_chunk (tenant_id, script_id, revision,"
                    " chunk_no, bytes_ref, sha256, byte_size)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (tenant_id, script_id, revision, chunk["chunk_no"],
                     chunk["bytes_ref"], chunk["sha256"],
                     chunk["byte_size"]))
            for entry in prepared:
                if not entry["new"]:
                    continue
                source = entry["entry"]
                line_id = entry["line_id"]
                self.conn.execute(
                    "INSERT INTO line (tenant_id, line_id, script_id,"
                    " episode_id, life_state, head_revision, created_at)"
                    " VALUES (?,?,?,?,'active',1,?)",
                    (tenant_id, line_id, script_id, script["episode_id"],
                     _now()))
                self.conn.execute(
                    "INSERT INTO line_revision (tenant_id, line_id, revision,"
                    " text, text_hash, speaker_id, speaker_label,"
                    " parenthetical, locale, scene_marker, order_in_scene,"
                    " author, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tenant_id, line_id, 1, source["text"],
                     entry["text_hash"], source["speaker_id"],
                     source["speaker_label"], source["parenthetical"],
                     source["locale"] or "zh-CN", source["scene_marker"],
                     source["order_in_scene"], _jcs(author), _now()))
            for seq, entry in enumerate(prepared, start=1):
                self.conn.execute(
                    "INSERT INTO script_revision_line (tenant_id, script_id,"
                    " revision, line_id, line_revision, text_hash, seq)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (tenant_id, script_id, revision, entry["line_id"],
                     entry["line_revision"], entry["text_hash"], seq))
            self.conn.execute(
                "UPDATE script SET head_revision=? WHERE tenant_id=? AND"
                " script_id=?", (revision, tenant_id, script_id))
            # DRAFT：不写 outbox（§5.3）。
            return {"script_id": script_id, "revision": revision,
                    "state": "DRAFT", "content_hash": content_hash,
                    "line_set_hash": line_set_hash,
                    "line_count": len(prepared),
                    "head_revision": revision}

        return self._command(tenant_id, command_id, produce, request_hash)

    def _prepare_lines(self, tenant_id: str, script_id: str,
                       lines: list[dict]) -> list[dict]:
        """§4.1 第 6 步的**只读**一半：定行身份、查冲突，不写库。

        分成 resolve / persist 两段的原因：`content_hash` 里含 `revision`，
        要先算出来才能查内容去重；如果这一半已经写了 `line`，去重命中时那些
        行就成了没人引用的孤儿（而事务会提交它们）。

        `line_id=null` → 铸造新行（`new=True`，行在 persist 段插入）；已有
        `line_id` 且未变 → 复用；变了 → 422 `LINE_MUTATION_IN_DRAFT`。同一次
        提交里重复 `line_id` → 422 `DUPLICATE_LINE_IN_REVISION`。

        S04-G9：方案只对**文本**变更说了拒绝；speaker / parenthetical /
        locale / scene_marker / order_in_scene 的变更同样会让 line_revision
        编号混乱，所以一起拒（details.fields 列出差异）。
        """
        seen: set[str] = set()
        prepared: list[dict] = []
        for index, entry in enumerate(lines):
            text_hash = text_digest(entry["text"])
            line_id = entry["line_id"]
            if line_id is None:
                line_id = _new_id("line")
                prepared.append({"line_id": line_id, "line_revision": 1,
                                 "text_hash": text_hash, "new": True,
                                 "entry": entry})
                seen.add(line_id)
                continue

            if line_id in seen:
                raise ServiceError(
                    422, "DUPLICATE_LINE_IN_REVISION",
                    f"line {line_id} appears more than once in lines[]",
                    details={"line_id": line_id, "index": index})
            row = self.conn.execute(
                "SELECT * FROM line WHERE tenant_id=? AND line_id=?",
                (tenant_id, line_id)).fetchone()
            if row is None or row["script_id"] != script_id:
                # 别的 script / 别的租户的 line：同为"不存在"（E28）。
                raise ServiceError(
                    422, "LINE_NOT_FOUND",
                    f"line {line_id} does not belong to script {script_id}",
                    details={"line_id": line_id, "index": index})
            if row["life_state"] == "retired":
                raise ServiceError(
                    409, "ILLEGAL_STATE_TRANSITION",
                    f"line {line_id} is retired; a retired line cannot appear "
                    f"in a new revision",
                    details={"line_id": line_id})
            latest = self.conn.execute(
                "SELECT * FROM line_revision WHERE tenant_id=? AND line_id=?"
                " ORDER BY revision DESC LIMIT 1",
                (tenant_id, line_id)).fetchone()
            changed = []
            if latest is not None:
                if latest["text_hash"] != text_hash:
                    changed.append("text")
                # 只对比调用方**显式给出**的字段：缺省不等于置空。
                for key in ("speaker_id", "speaker_label", "parenthetical",
                            "locale", "scene_marker", "order_in_scene"):
                    if key in entry["provided"] and entry[key] != latest[key]:
                        changed.append(key)
            if changed:
                raise ServiceError(
                    422, "LINE_MUTATION_IN_DRAFT",
                    f"line {line_id} changed in-place ({', '.join(changed)}); "
                    f"use POST /lines/{line_id}/revisions",
                    details={"line_id": line_id, "fields": changed,
                             "required_endpoint":
                                 f"POST /lines/{line_id}/revisions",
                             "gap": "S04-G9"})
            prepared.append({"line_id": line_id,
                             "line_revision": int(latest["revision"]),
                             "text_hash": text_hash, "new": False,
                             "entry": entry})
            seen.add(line_id)
        return prepared

    # ---- POST /scripts/{id}/revisions/{rev}:seal --------------------------

    def seal(self, tenant_id: str, script_id: str, revision: int, body: dict,
             *, command_id: str, request_hash: str,
             home_cell: str = "local") -> dict:
        """§4.3 的七步。**这是唯一发 `revision.created` 的路径。**

        Agent 永远 seal 不了：seal 的 `author` 必须是 `human`（§9 第 15 条）。
        这里不做"猜测调用者身份"—— `author` 是请求的一部分，缺了就是 400；
        传 `agent` 就是 403。
        """
        def produce():
            script = self._lock_script(tenant_id, script_id)
            self._cas(script["head_revision"],
                      body.get("expected_head_revision"),
                      f"script {script_id}")
            row = self.conn.execute(
                "SELECT * FROM script_revision WHERE tenant_id=? AND"
                " script_id=? AND revision=?",
                (tenant_id, script_id, revision)).fetchone()
            if row is None:
                raise ServiceError(404, "SCRIPT_NOT_FOUND",
                                   f"revision {revision} of {script_id} not "
                                   f"found")
            if int(row["revision"]) != int(script["head_revision"]):
                raise ServiceError(
                    409, "NOT_HEAD_REVISION",
                    f"revision {revision} is not head "
                    f"({script['head_revision']})",
                    details={"revision": int(row["revision"]),
                             "head_revision": int(script["head_revision"])})
            if row["state"] != "DRAFT":
                raise ServiceError(
                    409, "ILLEGAL_STATE_TRANSITION",
                    f"revision {revision} is {row['state']}, only DRAFT can be "
                    f"sealed",
                    details={"state": row["state"]})
            author = _principal(body.get("author"))
            if author["principal_type"] != "human":
                raise ServiceError(
                    403, "PERMISSION_DENIED",
                    "seal is a human action; an agent-authored revision "
                    "cannot be sealed by the agent that produced it",
                    details={"principal_type": author["principal_type"],
                             "gap": "NAR-6"})

            # 第 3 步：重新核算 line_set_hash。理论上行集合没变，但 seal 是
            # "这版从此可被引用"的那一刻，不能假设提交后没有人动过。
            rows = self.conn.execute(
                "SELECT line_id, line_revision, text_hash"
                " FROM script_revision_line WHERE tenant_id=? AND script_id=?"
                " AND revision=? ORDER BY seq", (tenant_id, script_id,
                                                 revision)).fetchall()
            computed = line_set_digest(
                (r["line_id"], r["line_revision"], r["text_hash"])
                for r in rows)
            if computed != row["line_set_hash"]:
                raise ServiceError(
                    500, "LINE_SET_HASH_MISMATCH",
                    "stored line_set_hash disagrees with script_revision_line; "
                    "refusing to seal a revision whose contents moved",
                    details={"stored": row["line_set_hash"],
                             "computed": computed})
            # §3.1 的 seal 守卫：`structure.line_count > 0`。空行集合可以
            # seal 的话，S05 会切出 0 场并"成功"产出一个空集 —— 与正文读
            # 失败返回空串是同一类事故。
            if not rows:
                raise ServiceError(
                    409, "ILLEGAL_STATE_TRANSITION",
                    f"revision {revision} has no lines; a sealed revision must "
                    f"materialise a non-empty line set",
                    details={"line_count": 0})
            structure = _loads(row["structure"], {})
            structure["line_count"] = len(rows)

            sealed_at = _now()
            self.conn.execute(
                "UPDATE script_revision SET state='SEALED', sealed_at=?,"
                " structure=? WHERE tenant_id=? AND script_id=? AND revision=?",
                (sealed_at, _jcs(structure), tenant_id, script_id, revision))
            # 第 5 步：更早的 SEALED → SUPERSEDED。不发事件（NAR-7）。
            superseded = self.conn.execute(
                "SELECT revision FROM script_revision WHERE tenant_id=? AND"
                " script_id=? AND state='SEALED' AND revision<>?"
                " ORDER BY revision", (tenant_id, script_id, revision)
            ).fetchall()
            self.conn.execute(
                "UPDATE script_revision SET state='SUPERSEDED' WHERE"
                " tenant_id=? AND script_id=? AND state='SEALED' AND"
                " revision<>?", (tenant_id, script_id, revision))
            self.conn.execute(
                "UPDATE script SET sealed_revision=? WHERE tenant_id=? AND"
                " script_id=?", (revision, tenant_id, script_id))
            # 第 6 步：写 outbox。只有 seal 才发。
            fresh = self.conn.execute(
                "SELECT * FROM script_revision WHERE tenant_id=? AND"
                " script_id=? AND revision=?",
                (tenant_id, script_id, revision)).fetchone()
            merged = self.conn.execute(
                "SELECT sr.series_id, sr.episode_id, r.* FROM script sr"
                " JOIN script_revision r ON r.tenant_id=sr.tenant_id AND"
                " r.script_id=sr.script_id WHERE r.tenant_id=? AND"
                " r.script_id=? AND r.revision=?",
                (tenant_id, script_id, revision)).fetchone()
            self._emit_revision_created(
                tenant_id, merged, command_id=command_id,
                home_cell=home_cell, line_count=len(rows),
                structure=structure)
            return {"script_id": script_id, "revision": revision,
                    "state": "SEALED", "sealed_at": sealed_at,
                    "content_hash": fresh["content_hash"],
                    "superseded_revisions": [int(r["revision"])
                                             for r in superseded]}

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /scripts/{id}/revisions/{rev}:withdraw ----------------------

    def withdraw(self, tenant_id: str, script_id: str, revision: int,
                 body: dict, *, command_id: str, request_hash: str,
                 home_cell: str = "local") -> dict:
        """§3.1 的撤回守卫。

        顺序是**先查投影再锁**（§6.2）：S08 的 commit 不读 S04，所以没有跨
        服务锁；窗口内可能撤回一份正被 commit 的剧本，由 S08 回源校验兜底
        （C04-8）。这里只保证"我已知的引用"都被尊重。

        同 script 只允许撤回**当前 head 的那个 SEALED**：撤回一个已被新
        seal 顶掉的旧版本不会让任何 manifest 失效（它已经是 SUPERSEDED），
        但会让"哪一版有效"更难读。
        """
        def produce():
            script = self._lock_script(tenant_id, script_id)
            row = self.conn.execute(
                "SELECT * FROM script_revision WHERE tenant_id=? AND"
                " script_id=? AND revision=?",
                (tenant_id, script_id, revision)).fetchone()
            if row is None:
                raise ServiceError(404, "SCRIPT_NOT_FOUND",
                                   f"revision {revision} of {script_id} not "
                                   f"found")
            if row["state"] == "WITHDRAWN":
                # DDL 没有 `withdrawn_at` 列（§2.2 只有 `sealed_at`），撤回时间
                # 只存在 local_event 的 payload 里。这里不回填一个别的字段冒充
                # 它 —— 两次响应的键集必须一致，否则调用方没法判断缺的是哪个。
                return {"script_id": script_id, "revision": revision,
                        "state": "WITHDRAWN", "already_withdrawn": True}
            # §3.1 允许 DRAFT→WITHDRAWN（作者放弃草稿，无守卫）；只有
            # SEALED→WITHDRAWN 要过引用守卫。SUPERSEDED 是终态。
            if row["state"] not in ("DRAFT", "SEALED"):
                raise ServiceError(
                    409, "ILLEGAL_STATE_TRANSITION",
                    f"revision {revision} is {row['state']}, only DRAFT or "
                    f"SEALED can be withdrawn", details={"state": row["state"]})

            if row["state"] == "SEALED":
                self._guard_references(tenant_id, script_id, revision)

            self.conn.execute(
                "UPDATE script_revision SET state='WITHDRAWN' WHERE tenant_id=?"
                " AND script_id=? AND revision=?",
                (tenant_id, script_id, revision))
            if script["sealed_revision"] == revision:
                previous = self.conn.execute(
                    "SELECT MAX(revision) AS r FROM script_revision WHERE"
                    " tenant_id=? AND script_id=? AND state='SEALED'",
                    (tenant_id, script_id)).fetchone()
                self.conn.execute(
                    "UPDATE script SET sealed_revision=? WHERE tenant_id=? AND"
                    " script_id=?",
                    (previous["r"], tenant_id, script_id))
            self._emit_local(
                tenant_id, EVENT_SCRIPT_WITHDRAWN, script_id,
                {"script_id": script_id, "revision": revision,
                 "state": "WITHDRAWN",
                 "content_hash": row["content_hash"],
                 "withdrawn_by": _principal(body.get("author")
                                            or {"principal_id": "unknown",
                                                "principal_type": "human"})})
            return {"script_id": script_id, "revision": revision,
                    "state": "WITHDRAWN", "already_withdrawn": False}

        return self._command(tenant_id, command_id, produce, request_hash)

    def _guard_references(self, tenant_id: str, script_id: str,
                          revision: int) -> None:
        """撤回守卫：有 manifest 引用 → 409；投影不新鲜 → 保守拒绝。"""
        health = self.reference_health(tenant_id)
        if not health["fresh"]:
            raise ServiceError(
                409, "REFERENCE_CHECK_STALE",
                "the manifest reference projection is stale; retry once it "
                "catches up", retryable=True,
                details={"source": health["source"],
                         "last_sequence": health["sequence"],
                         "lag_seconds": health["lag_seconds"]})
        rows = self.conn.execute(
            "SELECT manifest_id, manifest_revision FROM"
            " script_reference_projection WHERE tenant_id=? AND script_id=?"
            " AND script_revision=?", (tenant_id, script_id, revision)
        ).fetchall()
        if rows:
            raise ServiceError(
                409, "REVISION_REFERENCED_BY_MANIFEST",
                f"revision {revision} of {script_id} is referenced by "
                f"{len(rows)} manifest revision(s)",
                details={"references": [dict(r) for r in rows]})

    # ---- POST /lines/{id}/revisions --------------------------------------

    def append_line_revision(self, tenant_id: str, line_id: str, body: dict,
                             *, command_id: str, request_hash: str,
                             home_cell: str = "local") -> dict:
        """§4.5。**不改任何 ScriptRevision。**

        `text_hash` 与最新相同 → 幂等返回既有 revision（T04-18），不新建。
        """
        def produce():
            line = self._lock_line(tenant_id, line_id)
            self._cas(line["head_revision"],
                      body.get("expected_head_revision"), f"line {line_id}")
            text = body.get("text")
            _require(isinstance(text, str) and text, "text is required")
            author = _principal(body.get("author") or
                                {"principal_id": "unknown",
                                 "principal_type": "human"})
            text_hash = text_digest(text)
            latest = self.conn.execute(
                "SELECT * FROM line_revision WHERE tenant_id=? AND line_id=?"
                " ORDER BY revision DESC LIMIT 1",
                (tenant_id, line_id)).fetchone()
            # UNIQUE (tenant, line, text_hash) 让 A→B→A 不可能新建第三版，
            # 所以这里按"这一行是否已有这段文本"查，而不是只比最新一版。
            # T04-18 要的正是"命中原 line_revision，不建新版本"。
            existing = self.conn.execute(
                "SELECT * FROM line_revision WHERE tenant_id=? AND line_id=?"
                " AND text_hash=?", (tenant_id, line_id, text_hash)).fetchone()
            if existing is not None:
                return {"line_id": line_id,
                        "revision": int(existing["revision"]),
                        "text_hash": text_hash,
                        "script_id": line["script_id"],
                        "head_revision": int(line["head_revision"]),
                        "reused": True}
            if line["life_state"] != "active":
                raise ServiceError(
                    409, "ILLEGAL_STATE_TRANSITION",
                    f"line {line_id} is retired",
                    details={"life_state": line["life_state"]})
            revision = int(latest["revision"]) + 1 if latest else 1
            prev_hash = latest["text_hash"] if latest else None
            speaker_id = body.get("speaker_id",
                                  latest["speaker_id"] if latest else None)
            speaker_label = body.get("speaker_label",
                                     latest["speaker_label"] if latest else None)
            _require(bool(speaker_id) or bool(speaker_label),
                     "speaker_id or speaker_label is required",
                     code="SPEAKER_REQUIRED", status=422)
            self.conn.execute(
                "INSERT INTO line_revision (tenant_id, line_id, revision, text,"
                " text_hash, speaker_id, speaker_label, parenthetical, locale,"
                " scene_marker, order_in_scene, author, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, line_id, revision, text, text_hash, speaker_id,
                 speaker_label,
                 body.get("parenthetical",
                          latest["parenthetical"] if latest else None),
                 body.get("locale", latest["locale"] if latest else "zh-CN"),
                 body.get("scene_marker",
                          latest["scene_marker"] if latest else None),
                 body.get("order_in_scene",
                          latest["order_in_scene"] if latest else None),
                 _jcs(author), _now()))
            self.conn.execute(
                "UPDATE line SET head_revision=? WHERE tenant_id=? AND"
                " line_id=?", (revision, tenant_id, line_id))
            # 未登记事件 → local_event（C04-2）。
            self._emit_local(
                tenant_id, EVENT_LINE_REVISION_CREATED, line_id,
                {"line_id": line_id, "revision": revision,
                 "script_id": line["script_id"], "text_hash": text_hash,
                 "prev_text_hash": prev_hash, "speaker_id": speaker_id,
                 "locale": body.get("locale",
                                    latest["locale"] if latest else "zh-CN"),
                 "life_state": line["life_state"]})
            return {"line_id": line_id, "revision": revision,
                    "text_hash": text_hash, "prev_text_hash": prev_hash,
                    "script_id": line["script_id"], "reused": False}

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /lines/{id}:retire -----------------------------------------

    def retire_line(self, tenant_id: str, line_id: str, body: dict, *,
                    command_id: str, request_hash: str) -> dict:
        """§3.2：active → retired。**不物理删除**（§9 第 6 条）。"""
        def produce():
            line = self._lock_line(tenant_id, line_id)
            self._cas(line["head_revision"],
                      body.get("expected_head_revision"), f"line {line_id}")
            if line["life_state"] == "retired":
                return {"line_id": line_id, "life_state": "retired",
                        "retired_at": line["retired_at"],
                        "already_retired": True}
            retired_at = _now()
            self.conn.execute(
                "UPDATE line SET life_state='retired', retired_at=?"
                " WHERE tenant_id=? AND line_id=?",
                (retired_at, tenant_id, line_id))
            return {"line_id": line_id, "life_state": "retired",
                    "retired_at": retired_at, "already_retired": False}

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /briefs -----------------------------------------------------

    def create_brief(self, tenant_id: str, body: dict, *,
                     command_id: str, request_hash: str) -> dict:
        """S04-G7：方案没有建容器的命令，但 `brief_revision` 有外键指向它。

        一剧一份（`UNIQUE (tenant, series)`）。第二条同 series 的 brief 返回
        409 并带上现有 id —— 让调用方知道该往哪写，而不是造一份平行的。
        """
        def produce():
            series_id = body.get("series_id")
            _require(isinstance(series_id, str) and series_id,
                     "series_id is required")
            existing = self.conn.execute(
                "SELECT brief_id FROM brief WHERE tenant_id=? AND series_id=?",
                (tenant_id, series_id)).fetchone()
            if existing is not None:
                raise ServiceError(
                    409, "ILLEGAL_STATE_TRANSITION",
                    f"series {series_id} already has brief "
                    f"{existing['brief_id']}",
                    details={"brief_id": existing["brief_id"],
                             "series_id": series_id})
            brief_id = _new_id("brief")
            self.conn.execute(
                "INSERT INTO brief (tenant_id, brief_id, series_id,"
                " head_revision, created_at) VALUES (?,?,?,0,?)",
                (tenant_id, brief_id, series_id, _now()))
            return {"brief_id": brief_id, "series_id": series_id,
                    "head_revision": 0}

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /briefs/{id}/revisions -------------------------------------

    def append_brief_revision(self, tenant_id: str, brief_id: str, body: dict,
                              *, command_id: str, request_hash: str) -> dict:
        def produce():
            brief = self.conn.execute(
                "SELECT * FROM brief WHERE tenant_id=? AND brief_id=?",
                (tenant_id, brief_id)).fetchone()
            if brief is None:
                raise ServiceError(404, "BRIEF_NOT_FOUND",
                                   f"brief {brief_id} not found")
            self._cas(brief["head_revision"],
                      body.get("expected_head_revision"), f"brief {brief_id}")
            author = _principal(body.get("author") or
                                {"principal_id": "unknown",
                                 "principal_type": "human"})
            payload = _brief_body(body.get("body") or {})
            digest = _sha256_hex(_jcs(payload))
            duplicate = self.conn.execute(
                "SELECT revision FROM brief_revision WHERE tenant_id=? AND"
                " brief_id=? AND content_hash=?",
                (tenant_id, brief_id, digest)).fetchone()
            if duplicate is not None:
                return {"brief_id": brief_id,
                        "revision": int(duplicate["revision"]),
                        "content_hash": digest, "deduplicated": True}
            revision = int(brief["head_revision"]) + 1
            self.conn.execute(
                "INSERT INTO brief_revision (tenant_id, brief_id, revision,"
                " body, content_hash, author, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (tenant_id, brief_id, revision, _jcs(payload), digest,
                 _jcs(author), _now()))
            self.conn.execute(
                "UPDATE brief SET head_revision=? WHERE tenant_id=? AND"
                " brief_id=?", (revision, tenant_id, brief_id))
            return {"brief_id": brief_id, "revision": revision,
                    "content_hash": digest, "deduplicated": False}

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /story-arcs ------------------------------------------------

    def submit_story_arc(self, tenant_id: str, body: dict, *,
                         command_id: str, request_hash: str) -> dict:
        """§2.4。**候选，不是正史。** `ADOPTED` 只影响 UI 与 S18 pack。"""
        def produce():
            series_id = body.get("series_id")
            _require(isinstance(series_id, str) and series_id,
                     "series_id is required")
            produced_by = _produced_by(body.get("produced_by"))
            # DDL 是 NOT NULL（§2.4），所以这里必须是 object。
            _require(produced_by is not None,
                     "story-arc proposals require produced_by")
            self._verify_agent_task(tenant_id, produced_by)
            arc_body = body.get("body") or {}
            _require(isinstance(arc_body, dict), "body must be an object")
            unknown = set(arc_body) - {"acts", "turning_points",
                                       "foreshadow_plan"}
            _require(not unknown,
                     f"unknown story-arc body keys: {sorted(unknown)}")
            confidence = body.get("confidence")
            if confidence is not None:
                _require(isinstance(confidence, (int, float))
                         and not isinstance(confidence, bool)
                         and 0 <= confidence <= 1,
                         "confidence must be within [0, 1]")
            unresolved = body.get("unresolved") or []
            _require(isinstance(unresolved, list)
                     and all(isinstance(item, str) for item in unresolved),
                     "unresolved must be a list of strings")
            proposal_id = _new_id("proposal")
            self.conn.execute(
                "INSERT INTO story_arc_proposal (tenant_id, proposal_id,"
                " series_id, revision, state, body, produced_by,"
                " source_revision, confidence, unresolved, created_at)"
                " VALUES (?,?,?,1,'PROPOSED',?,?,?,?,?,?)",
                (tenant_id, proposal_id, series_id, _jcs(arc_body),
                 _jcs(produced_by),
                 None if body.get("source_revision") is None
                 else _jcs(body["source_revision"]),
                 confidence, _jcs(unresolved), _now()))
            return {"proposal_id": proposal_id, "revision": 1,
                    "state": "PROPOSED", "series_id": series_id}

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /story-arcs/{id}/revisions/{rev}:decide ---------------------

    def decide_story_arc(self, tenant_id: str, proposal_id: str,
                         revision: int, body: dict, *, command_id: str,
                         request_hash: str) -> dict:
        """§3.3。S04-G8：决策追加新 revision，旧行不动。

        PK 含 revision，原地改会让 revision 失去意义。同 state 重复决策幂等。
        """
        def produce():
            row = self.conn.execute(
                "SELECT * FROM story_arc_proposal WHERE tenant_id=? AND"
                " proposal_id=? AND revision=?",
                (tenant_id, proposal_id, revision)).fetchone()
            if row is None:
                raise ServiceError(404, "STORY_ARC_NOT_FOUND",
                                   f"story arc {proposal_id}@{revision} not "
                                   f"found")
            self._cas(row["revision"], body.get("expected_revision"),
                      f"story arc {proposal_id}")
            decision = body.get("decision")
            _require(decision in ARC_DECISIONS,
                     f"decision must be one of {sorted(ARC_DECISIONS)}")
            target = ARC_DECISIONS[decision]
            if row["state"] == target:
                # 同状态重复决策是重试，不是转换：先判等再判可转换，否则
                # "adopt 已 ADOPTED 的版本"会落进"ADOPTED 不能变成 ADOPTED"
                # 的 409，而它本该是幂等返回。
                return {"proposal_id": proposal_id,
                        "revision": int(row["revision"]), "state": target,
                        "reused": True}
            if target not in ARC_TRANSITIONS[row["state"]]:
                raise ServiceError(
                    409, "ILLEGAL_STATE_TRANSITION",
                    f"{row['state']} cannot become {target}",
                    details={"state": row["state"], "target": target})
            latest = self.conn.execute(
                "SELECT MAX(revision) AS r FROM story_arc_proposal WHERE"
                " tenant_id=? AND proposal_id=?",
                (tenant_id, proposal_id)).fetchone()
            if int(latest["r"]) != int(revision):
                raise ServiceError(
                    409, "REVISION_CONFLICT",
                    f"story arc {proposal_id} has a newer revision "
                    f"{latest['r']}", details={"current_revision":
                                               int(latest["r"])})
            reviewer = _principal(body.get("author") or
                                  {"principal_id": "unknown",
                                   "principal_type": "human"})
            next_revision = int(row["revision"]) + 1
            self.conn.execute(
                "INSERT INTO story_arc_proposal (tenant_id, proposal_id,"
                " series_id, revision, state, body, produced_by,"
                " source_revision, confidence, unresolved, reviewed_by,"
                " created_at, decided_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, proposal_id, row["series_id"], next_revision,
                 target, row["body"], row["produced_by"],
                 row["source_revision"], row["confidence"],
                 row["unresolved"], _jcs(reviewer), _now(), _now()))
            return {"proposal_id": proposal_id, "revision": next_revision,
                    "state": target, "reused": False}

        return self._command(tenant_id, command_id, produce, request_hash)

    # ---- POST /scripts/{id}/revisions/{rev}:extract-claims ---------------

    def extract_claims(self, tenant_id: str, script_id: str, revision: int,
                       body: dict, *, command_id: str, request_hash: str
                       ) -> dict:
        """§2.5 / §5.1：**只写 `claim_extraction_run(PENDING)`，不存候选。**

        预算是 S19/S26 的事，S04 不碰 S23/S22。只允许从 SEALED 抽取
        （T04-10）—— DRAFT 随时可废，抽出来的候选指向一个不存在的版本。
        """
        def produce():
            self._lock_script(tenant_id, script_id)
            row = self.conn.execute(
                "SELECT * FROM script_revision WHERE tenant_id=? AND"
                " script_id=? AND revision=?",
                (tenant_id, script_id, revision)).fetchone()
            if row is None:
                raise ServiceError(404, "SCRIPT_NOT_FOUND",
                                   f"revision {revision} of {script_id} not "
                                   f"found")
            if row["state"] != "SEALED":
                raise ServiceError(
                    409, "ILLEGAL_STATE_TRANSITION",
                    f"only SEALED revisions can be extracted; revision "
                    f"{revision} is {row['state']}",
                    details={"state": row["state"]})
            task_request = body.get("task_request")
            _require(isinstance(task_request, dict),
                     "task_request is required (S04-G6: S04 forwards it to S19 "
                     "verbatim and does not construct context/limits)")
            if self._task_source is None:
                raise ServiceError(
                    503, "AGENT_TASK_SOURCE_UNAVAILABLE",
                    "no S19 task source is configured; S04 will not record an "
                    "extraction it cannot start", retryable=True)
            submit = getattr(self._task_source, "submit", None)
            if submit is None:
                raise ServiceError(
                    503, "AGENT_TASK_SOURCE_UNAVAILABLE",
                    "the configured S19 source cannot submit tasks",
                    retryable=True)
            task = self._upstream(
                lambda: submit(tenant_id, command_id, task_request),
                "AGENT_TASK_SOURCE_UNAVAILABLE")
            _require(isinstance(task, dict), "S19 returned no task body")
            task_id = task.get("task_id")
            _require(bool(task_id),
                     "S19 accepted the extraction but returned no task_id",
                     code="AGENT_TASK_SOURCE_UNAVAILABLE", status=503)
            extraction_id = _new_id("extraction")
            self.conn.execute(
                "INSERT INTO claim_extraction_run (tenant_id, extraction_id,"
                " script_id, script_revision, agent_task_ref, result_state,"
                " created_at) VALUES (?,?,?,?,?,'PENDING',?)",
                (tenant_id, extraction_id, script_id, revision, task_id,
                 _now()))
            return {"extraction_id": extraction_id, "script_id": script_id,
                    "script_revision": revision, "agent_task_ref": task_id,
                    "result_state": "PENDING"}

        return self._command(tenant_id, command_id, produce, request_hash)

    def complete_extraction(self, tenant_id: str, extraction_id: str, body: dict
                            ) -> dict:
        """S19 回调：写结果状态与 `handoff_ref`（S16 的候选批次引用）。

        不是幂等命令（没有 command_id）：同一个 `result_state` 重复写入是
        无害覆盖，状态只能前进不能倒退。
        """
        row = self.conn.execute(
            "SELECT * FROM claim_extraction_run WHERE tenant_id=? AND"
            " extraction_id=?", (tenant_id, extraction_id)).fetchone()
        if row is None:
            raise ServiceError(404, "EXTRACTION_NOT_FOUND",
                               f"extraction {extraction_id} not found")
        state = body.get("result_state")
        _require(state in EXTRACTION_STATES,
                 f"result_state must be one of {list(EXTRACTION_STATES)}")
        if row["result_state"] == state:
            # 同一结果重投：幂等返回，不覆盖已落库的 handoff_ref。
            return self._extraction_dict(tenant_id, extraction_id)
        # 终态不可再变：`SUCCEEDED → FAILED` 会让"结果去哪了"指向一个
        # 可能已经不存在的 handoff_ref。只有 PENDING 能前进。
        if row["result_state"] != "PENDING":
            raise ServiceError(
                409, "ILLEGAL_STATE_TRANSITION",
                f"extraction {extraction_id} is already "
                f"{row['result_state']}; a terminal result cannot change",
                details={"result_state": row["result_state"],
                         "incoming": state})
        handoff = body.get("handoff_ref")
        candidate_count = body.get("candidate_count")
        if candidate_count is not None:
            _require(isinstance(candidate_count, int)
                     and not isinstance(candidate_count, bool)
                     and candidate_count >= 0,
                     "candidate_count must be a non-negative integer")
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE claim_extraction_run SET result_state=?,"
                " candidate_count=?, handoff_ref=?, completed_at=?"
                " WHERE tenant_id=? AND extraction_id=?",
                (state, candidate_count,
                 None if handoff is None else _jcs(handoff), _now(),
                 tenant_id, extraction_id))
        return self._extraction_dict(tenant_id, extraction_id)

    # ==================================================================
    # 投影（只增不删、可重建、非权威）
    # ==================================================================

    def apply_manifest_committed(self, tenant_id: str, envelope: dict) -> dict:
        """消费 `snapshot.manifest.committed`，维护 `script_reference_projection`。

        **这不是"我的剧本被接受了"**（§9 第 10 条）。它唯一的消费者是撤回
        守卫。所以本方法不返回、不写任何 accepted 标记。

        `source_revisions` 在 S08 的 schema 里是 `{"type":"array","items":{}}`
        （无类型），所以元素形状只能按 §6.4 的约定读：`{service_id, id,
        revision, content_hash}`，`service_id` 不是 `"S04"` 的条目跳过。
        `applied_seq` 单调（T04-32）：乱序到达不覆盖更大的水位。
        """
        payload = envelope.get("data") or {}
        sequence = envelope.get("sequence")
        _require(isinstance(sequence, int), "envelope has no integer sequence")
        manifest_ref = payload.get("manifest_ref") or {}
        manifest_id = manifest_ref.get("id")
        manifest_revision = manifest_ref.get("revision")
        execution_fence = payload.get("execution_fence")
        if not manifest_id or not isinstance(manifest_revision, int):
            raise ServiceError(422, "INVALID_BODY",
                               "manifest_ref needs id and revision")
        if not isinstance(execution_fence, int):
            raise ServiceError(422, "INVALID_BODY",
                               "manifest envelope has no execution_fence")

        with Transaction(self.conn):
            watermark = self.conn.execute(
                "SELECT sequence FROM projection_watermark WHERE tenant_id=?"
                " AND source='S08'", (tenant_id,)).fetchone()
            if watermark is not None and int(watermark["sequence"]) >= sequence:
                # 乱序/重放：不前进（T04-32）。
                return {"applied": False, "reason": "out_of_order",
                        "sequence": sequence}
            applied = 0
            for entry in payload.get("source_revisions") or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("service_id") != SOURCE:
                    continue
                script_id = entry.get("id")
                revision = entry.get("revision")
                if not script_id or not isinstance(revision, int):
                    continue
                self.conn.execute(
                    "INSERT OR IGNORE INTO script_reference_projection"
                    " (tenant_id, script_id, script_revision, manifest_id,"
                    " manifest_revision, execution_fence, applied_seq)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (tenant_id, script_id, revision, manifest_id,
                     manifest_revision, execution_fence, sequence))
                applied += 1
            self.conn.execute(
                "INSERT INTO projection_watermark (tenant_id, source,"
                " sequence, updated_at) VALUES (?,'S08',?,?)"
                " ON CONFLICT (tenant_id, source) DO UPDATE SET"
                " sequence=excluded.sequence, updated_at=excluded.updated_at",
                (tenant_id, sequence, _now()))
            return {"applied": True, "references": applied,
                    "sequence": sequence}

    def reference_health(self, tenant_id: str) -> dict:
        """投影是否可信（S04-G12）。**没有水位行 = 从未消费 = 落后。**

        "没收到任何事件"不能读成"没有任何引用"：那样每次部署后第一次撤回
        都会放行一个被 manifest 引用的 revision。
        """
        row = self.conn.execute(
            "SELECT sequence, updated_at FROM projection_watermark WHERE"
            " tenant_id=? AND source='S08'", (tenant_id,)).fetchone()
        if row is None:
            return {"fresh": False, "source": "S08", "sequence": None,
                    "lag_seconds": None}
        try:
            stamp = datetime.strptime(row["updated_at"],
                                      "%Y-%m-%dT%H:%M:%S.%fZ")
            stamp = stamp.replace(tzinfo=timezone.utc)
            lag = int((_now_dt() - stamp).total_seconds())
        except ValueError:
            lag = None
        fresh = lag is not None and lag <= REFERENCE_MAX_LAG_SECONDS
        return {"fresh": fresh, "source": "S08",
                "sequence": int(row["sequence"]), "lag_seconds": lag}

    # ==================================================================
    # 查询（§5.2）
    # ==================================================================

    def _script_dict(self, tenant_id: str, script_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM script WHERE tenant_id=? AND script_id=?",
            (tenant_id, script_id)).fetchone()
        if row is None:
            raise ServiceError(404, "SCRIPT_NOT_FOUND",
                               f"script {script_id} not found")
        return {"script_id": row["script_id"], "series_id": row["series_id"],
                "episode_id": row["episode_id"],
                "script_kind": row["script_kind"],
                "language": row["language"],
                "head_revision": int(row["head_revision"]),
                "sealed_revision": row["sealed_revision"],
                "retired_at": row["retired_at"],
                "created_at": row["created_at"],
                "x-contract-gaps": CONTRACT_GAPS}

    def get_script(self, tenant_id: str, script_id: str) -> dict:
        return self._script_dict(tenant_id, script_id)

    def get_revision(self, tenant_id: str, script_id: str,
                     revision: int) -> dict:
        """单版元数据，**不含正文**（§5.2 行 669）。

        返回 `title / language / structure / parent_revision` 是给 NAR-3 的
        补救：事件里的 `content_hash` 无法用事件自带字段复算，至少让下游能
        回源复算。
        """
        row = self.conn.execute(
            "SELECT r.*, s.series_id, s.episode_id, s.script_kind, s.language"
            " FROM script_revision r JOIN script s ON s.tenant_id=r.tenant_id"
            " AND s.script_id=r.script_id WHERE r.tenant_id=? AND"
            " r.script_id=? AND r.revision=?",
            (tenant_id, script_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "SCRIPT_NOT_FOUND",
                               f"revision {revision} of {script_id} not found")
        out = {
            "script_id": row["script_id"], "revision": int(row["revision"]),
            "state": row["state"], "title": row["title"],
            "series_id": row["series_id"], "episode_id": row["episode_id"],
            "script_kind": row["script_kind"], "language": row["language"],
            "body_ref": row["body_ref"], "body_bytes": int(row["body_bytes"]),
            "structure": _loads(row["structure"], {}),
            "line_set_hash": row["line_set_hash"],
            "content_hash": row["content_hash"],
            "author": _loads(row["author"], {}),
            "produced_by": _loads(row["produced_by"], None),
            "parent_revision": row["parent_revision"],
            "sealed_at": row["sealed_at"], "created_at": row["created_at"],
            "x-contract-gaps": CONTRACT_GAPS,
        }
        return out

    def get_body(self, tenant_id: str, script_id: str, revision: int,
                 *, chunk: int | None = None) -> dict:
        """正文分块（权限化）。`chunk=None` 返回整版。

        **读不出来就 `body_unavailable:true`，绝不返回空串**（§4.6 第 18 条）。
        """
        row = self.conn.execute(
            "SELECT * FROM script_revision WHERE tenant_id=? AND script_id=?"
            " AND revision=?", (tenant_id, script_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "SCRIPT_NOT_FOUND",
                               f"revision {revision} of {script_id} not found")
        chunks = self.conn.execute(
            "SELECT * FROM body_chunk WHERE tenant_id=? AND script_id=? AND"
            " revision=? ORDER BY chunk_no",
            (tenant_id, script_id, revision)).fetchall()
        if chunk is not None:
            selected = [c for c in chunks if int(c["chunk_no"]) == int(chunk)]
            if not selected:
                raise ServiceError(404, "CHUNK_NOT_FOUND",
                                   f"revision {revision} has no chunk {chunk}")
            chunks = selected
        result = self._read_body(tenant_id, script_id, revision, chunks)
        result.update({"script_id": script_id, "revision": revision})
        return result

    def list_revision_lines(self, tenant_id: str, script_id: str,
                            revision: int, *, after_line_id: str | None = None,
                            limit: int = 50) -> dict:
        row = self.conn.execute(
            "SELECT 1 FROM script_revision WHERE tenant_id=? AND script_id=?"
            " AND revision=?", (tenant_id, script_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "SCRIPT_NOT_FOUND",
                               f"revision {revision} of {script_id} not found")
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        params: list = [tenant_id, script_id, revision]
        clause = ""
        if after_line_id:
            # 游标是 `line_id`，但**顺序必须是 `seq`**：`seq` 才是一版剧本里的
            # 出现顺序（§2.3）。按 `line_id` 排会把对白按随机 id 次序交给
            # S05/S06，切出来的场次顺序是错的。所以先把游标翻译成 seq。
            anchor = self.conn.execute(
                "SELECT seq FROM script_revision_line WHERE tenant_id=? AND"
                " script_id=? AND revision=? AND line_id=?",
                (tenant_id, script_id, revision, after_line_id)).fetchone()
            if anchor is None:
                # 游标不在这版里：静默从头上重来会让调用方的分页死循环。
                raise ServiceError(
                    404, "LINE_NOT_FOUND",
                    f"cursor line {after_line_id} is not in revision "
                    f"{revision} of {script_id}")
            clause = " AND srl.seq > ?"
            params.append(int(anchor["seq"]))
        params.append(limit + 1)
        rows = self.conn.execute(
            "SELECT srl.line_id, srl.line_revision, srl.text_hash, srl.seq,"
            " lr.text, lr.speaker_id, lr.speaker_label, lr.parenthetical,"
            " lr.locale, lr.scene_marker, lr.order_in_scene, l.life_state"
            " FROM script_revision_line srl JOIN line_revision lr ON"
            " lr.tenant_id=srl.tenant_id AND lr.line_id=srl.line_id AND"
            " lr.revision=srl.line_revision JOIN line l ON"
            " l.tenant_id=srl.tenant_id AND l.line_id=srl.line_id"
            " WHERE srl.tenant_id=? AND srl.script_id=? AND srl.revision=?"
            + clause + " ORDER BY srl.seq LIMIT ?", params).fetchall()
        page = rows[:limit]
        return {"script_id": script_id, "revision": revision,
                "lines": [dict(item) for item in page],
                "next_after_line_id":
                    page[-1]["line_id"] if len(rows) > limit else None}

    def get_line(self, tenant_id: str, line_id: str) -> dict:
        line = self._lock_line(tenant_id, line_id)
        latest = self.conn.execute(
            "SELECT * FROM line_revision WHERE tenant_id=? AND line_id=?"
            " ORDER BY revision DESC LIMIT 1",
            (tenant_id, line_id)).fetchone()
        return {"line_id": line_id, "script_id": line["script_id"],
                "episode_id": line["episode_id"],
                "life_state": line["life_state"],
                "head_revision": int(line["head_revision"]),
                "retired_at": line["retired_at"],
                "latest": _row(latest), "x-contract-gaps": CONTRACT_GAPS}

    def get_line_revision(self, tenant_id: str, line_id: str,
                          revision: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM line_revision WHERE tenant_id=? AND line_id=? AND"
            " revision=?", (tenant_id, line_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "LINE_NOT_FOUND",
                               f"revision {revision} of {line_id} not found")
        return dict(row)

    def batch_lines(self, tenant_id: str, ids: list[str]) -> dict:
        """`/lines:batch`，上限 500（§5.2 行 680）。

        超上限拒绝而不是截断：截断会让 S06 以为"剩下的行不存在"，然后写出
        一个缺对白的 ShotSpec。
        """
        _require(isinstance(ids, list) and ids, "ids is required")
        if len(ids) > MAX_BATCH_IDS:
            raise ServiceError(
                422, "TOO_MANY_IDS",
                f"{len(ids)} ids exceed the {MAX_BATCH_IDS} limit",
                details={"limit": MAX_BATCH_IDS, "count": len(ids)})
        unique = list(dict.fromkeys(ids))
        placeholders = ",".join("?" for _ in unique)
        rows = self.conn.execute(
            "SELECT l.line_id, l.script_id, l.episode_id, l.life_state,"
            " l.head_revision, l.retired_at, lr.revision, lr.text,"
            " lr.text_hash, lr.speaker_id, lr.speaker_label,"
            " lr.parenthetical, lr.locale, lr.scene_marker, lr.order_in_scene"
            " FROM line l LEFT JOIN line_revision lr ON lr.tenant_id=l.tenant_id"
            " AND lr.line_id=l.line_id AND lr.revision=l.head_revision"
            " WHERE l.tenant_id=? AND l.line_id IN (" + placeholders + ")",
            [tenant_id, *unique]).fetchall()
        found = {row["line_id"]: dict(row) for row in rows}
        # 缺失的 id 显式列出：让调用方能区分"不存在"与"忘了带"。
        return {"lines": [found[key] for key in unique if key in found],
                "missing": [key for key in unique if key not in found]}

    def list_scripts(self, tenant_id: str, *, series_id: str | None = None,
                     episode_id: str | None = None,
                     state: str | None = None, limit: int = 50) -> dict:
        if state is not None:
            _require(state in REVISION_STATES,
                     f"state must be one of {list(REVISION_STATES)}")
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        params: list = [tenant_id]
        clauses = ["s.tenant_id=?"]
        if series_id:
            clauses.append("s.series_id=?")
            params.append(series_id)
        if episode_id:
            clauses.append("s.episode_id=?")
            params.append(episode_id)
        if state:
            clauses.append(
                "EXISTS (SELECT 1 FROM script_revision r WHERE"
                " r.tenant_id=s.tenant_id AND r.script_id=s.script_id AND"
                " r.state=?)")
            params.append(state)
        params.append(limit)
        rows = self.conn.execute(
            "SELECT s.* FROM script s WHERE " + " AND ".join(clauses)
            + " ORDER BY s.created_at DESC, s.script_id LIMIT ?",
            params).fetchall()
        return {"scripts": [self._script_dict(tenant_id, row["script_id"])
                            for row in rows]}

    def get_story_arc(self, tenant_id: str, proposal_id: str,
                      revision: int | None = None) -> dict:
        if revision is None:
            row = self.conn.execute(
                "SELECT * FROM story_arc_proposal WHERE tenant_id=? AND"
                " proposal_id=? ORDER BY revision DESC LIMIT 1",
                (tenant_id, proposal_id)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM story_arc_proposal WHERE tenant_id=? AND"
                " proposal_id=? AND revision=?",
                (tenant_id, proposal_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "STORY_ARC_NOT_FOUND",
                               f"story arc {proposal_id} not found")
        return {**dict(row),
                "body": _loads(row["body"], {}),
                "produced_by": _loads(row["produced_by"], {}),
                "source_revision": _loads(row["source_revision"], None),
                "unresolved": _loads(row["unresolved"], []),
                "reviewed_by": _loads(row["reviewed_by"], None),
                "x-contract-gaps": CONTRACT_GAPS}

    def get_brief(self, tenant_id: str, brief_id: str,
                  revision: int | None = None) -> dict:
        brief = self.conn.execute(
            "SELECT * FROM brief WHERE tenant_id=? AND brief_id=?",
            (tenant_id, brief_id)).fetchone()
        if brief is None:
            raise ServiceError(404, "BRIEF_NOT_FOUND",
                               f"brief {brief_id} not found")
        if revision is None:
            row = self.conn.execute(
                "SELECT * FROM brief_revision WHERE tenant_id=? AND"
                " brief_id=? ORDER BY revision DESC LIMIT 1",
                (tenant_id, brief_id)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM brief_revision WHERE tenant_id=? AND"
                " brief_id=? AND revision=?",
                (tenant_id, brief_id, revision)).fetchone()
        return {"brief_id": brief_id, "series_id": brief["series_id"],
                "head_revision": int(brief["head_revision"]),
                # `body` 落库是 JSON 文本；出口按 `get_story_arc` 的同形还原成
                # 对象，不让 HTTP 客户端拿到"JSON 里的字符串"。
                "revision": (None if row is None
                             else {**dict(row), "body": _loads(row["body"], {})}),
                "x-contract-gaps": CONTRACT_GAPS}

    def _extraction_dict(self, tenant_id: str, extraction_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM claim_extraction_run WHERE tenant_id=? AND"
            " extraction_id=?", (tenant_id, extraction_id)).fetchone()
        if row is None:
            raise ServiceError(404, "EXTRACTION_NOT_FOUND",
                               f"extraction {extraction_id} not found")
        return {**dict(row),
                "handoff_ref": _loads(row["handoff_ref"], None),
                # §9 第 9 条：S04 不缓存候选。
                "candidates": None}

    def get_extraction(self, tenant_id: str, extraction_id: str) -> dict:
        return self._extraction_dict(tenant_id, extraction_id)

    # ==================================================================
    # 维护
    # ==================================================================

    def collect_orphan_chunks(self, tenant_id: str) -> int:
        """§4.6 第 12 条：分块已写、主库提交失败 → 孤儿。

        判据是"对象存储里有 key、`body_chunk` 里没有行"。对象存储适配器若
        不提供 `list_prefix`，返回 0（不能假装扫过了）—— 由 S04-G1 登记。
        """
        if self._body_store is None:
            return 0
        lister = getattr(self._body_store, "list_prefix", None)
        if lister is None:
            return 0
        # 前缀必须正好是 `s04/{tenant}/`：用 BODY_KEY 拼一个占位 key 再截断
        # 会多出一层斜杠（`script_id=""` → `s04/t1//r0/c0`），什么都匹配不到，
        # GC 会永远返回 0 而看起来"扫过了"。
        prefix = f"s04/{tenant_id}/"
        removed = 0
        for key in list(lister(prefix)):
            row = self.conn.execute(
                "SELECT 1 FROM body_chunk WHERE tenant_id=? AND bytes_ref=?",
                (tenant_id, key)).fetchone()
            if row is None:
                try:
                    self._body_store.delete(key)
                    removed += 1
                except (ConnectionError, OSError, KeyError):
                    continue
        return removed

    def sweep_stale_commands(self, *, older_than_seconds: int = 300) -> int:
        """删掉 `IN_PROGRESS` 且陈旧的幂等行（S04-G4：SDK 没有 ABANDONED）。"""
        cutoff = datetime.fromtimestamp(
            _now_dt().timestamp() - older_than_seconds, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        with Transaction(self.conn):
            cursor = self.conn.execute(
                "DELETE FROM idempotency_record WHERE result IS NULL AND"
                " created_at < ?", (cutoff,))
            return cursor.rowcount

    def consume_manifest_event(self, consumer: str, envelope: dict) -> dict:
        """幂等入口：同一个 `event_id` 只应用一次（T04-33/34）。

        inbox 行与投影写入在**同一个事务**里：分开写会出现"已标记消费、投影没
        进去"的窗口，之后重投被当成 duplicates 永远补不回来。信封形状先校验
        再消费 —— 缺 `id` / `tenant_id` 的事件不能进 inbox，否则一个空 id 会
        占掉一格，而它对应的投影永远不会被应用。
        """
        if not isinstance(envelope, dict):
            raise ServiceError(422, "SCHEMA_INVALID", "event must be an object")
        event_type = envelope.get("type")
        if event_type is not None and event_type != MANIFEST_COMMITTED:
            # 不认识的类型不是错误，只是不归我消费（与 S09 同形，不触发重试）。
            return {"consumed": False, "reason": "unrouted"}
        tenant_id = envelope.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ServiceError(422, "SCHEMA_INVALID", "event lacks tenant_id")
        event_id = envelope.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ServiceError(422, "SCHEMA_INVALID", "event lacks id")

        inbox = Inbox(self.conn)
        with Transaction(self.conn):
            if not inbox.consume(consumer, event_id):
                return {"consumed": False, "reason": "duplicate"}
            result = self.apply_manifest_committed(tenant_id, envelope)
        return {"consumed": True, **result}
