"""S03 内核。方案：`plans/S03-project-catalog.md`。

四条不能违反的语义，都在这里落地：

1. **`episode_id` 永不因重排改变**（§1.1）。重排只写 `episode_order`。
2. **`completed` 是投影、可回退**（§3.2）。唯一写它的是
   `apply_acceptance_projection()`，且本服务不自己判定"做完了"——事实在 S08。
3. **规格在写入时物化**（§1.4）。改项目默认画幅不会回头改已在制作的集；
   要改已有集必须走 S07 的 ChangePlan。
4. **三条版本概念互不代用**（§6.2）。`head_revision` 只做元数据 CAS，
   绝不拿来当幂等键，也不用来判断"内容变没变"。

事务与幂等：方案 §4.1 的十步在**一个**事务内。平台幂等 SDK 的 `begin/finish`
是裸语句（自己不开事务），所以这里在命令事务内把结果写回同一个
`idempotency_record` 行 —— 提交与"记下已提交"原子成立，不存在"事务提交了、
幂等记录还没写"的窗口。HTTP 层的 `_idem.finish` 因此只是同值覆盖。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Outbox, new_event

from .schema import (
    CONTRACT_GAPS,
    ID_PREFIX,
    ORDER_KINDS,
    SCHEMA,
    SERIES_KINDS,
    SPEC_SEGMENTS,
    WRITE_PERMISSIONS,
)

SOURCE = "S03"
EVENT_TYPE = "project.episode.created"
TOPIC = "drama.project-catalog.v2"
SCHEMA_VERSION = "2.0"

# 重排时整体抬高的偏移量（§4.3）。只需大于最大集数；短剧用 1e6 足够。
ORDINAL_OFFSET = 1_000_000

# 本地事件（不登记在 event-topics.v2.json，见缺口 G9）。
LOCAL_REORDERED = "project.episode.reordered"
LOCAL_LIFECYCLE = "project.lifecycle.changed"
LOCAL_SPEC_CHANGED = "catalog.spec.changed"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


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


# ------------------------------------------------------------------ 规格

def _bad(message: str, code: str = "INVALID_BODY") -> ServiceError:
    return ServiceError(400, code, message)


def normalize_spec(body, *, full: bool) -> dict:
    """校验并规整 `spec_revision.body`（§2.5）。

    `full=True`（项目层）必须四段齐全；`full=False`（剧/集覆盖）允许只带
    要覆盖的段。**只要出现某一段，该段内部必须完整** —— 半个 `output`
    （只有 width 没有 height）合并进基座后会产出一个谁也解释不了的画幅。

    未知键一律 422 `SPEC_FIELD_UNKNOWN`，不是静默忽略：拼错的
    `aspect_ratios` 被吞掉，结果就是"改了配置但没生效"，而没有任何信号。
    """
    if not isinstance(body, dict):
        raise _bad("spec body must be an object")
    unknown = set(body) - set(SPEC_SEGMENTS)
    if unknown:
        raise ServiceError(422, "SPEC_FIELD_UNKNOWN",
                           f"unknown spec keys: {sorted(unknown)}",
                           details={"unknown": sorted(unknown)})

    out: dict = {}
    for segment in ("output", "language", "delivery"):
        if segment not in body:
            if full:
                raise _bad(f"spec.{segment} is required at project scope")
            continue
        value = body[segment]
        if not isinstance(value, dict):
            raise _bad(f"spec.{segment} must be an object")
        allowed = SPEC_SEGMENTS[segment]
        unknown = set(value) - set(allowed)
        if unknown:
            raise ServiceError(
                422, "SPEC_FIELD_UNKNOWN",
                f"unknown keys in spec.{segment}: {sorted(unknown)}",
                details={"unknown": sorted(unknown), "segment": segment})
        missing = [k for k in allowed if k not in value]
        if missing:
            raise _bad(f"spec.{segment} is incomplete; missing {missing}; "
                       "a partial segment cannot be merged over its base")
        out[segment] = {k: value[k] for k in allowed}

    if "default_branch" in body:
        if not isinstance(body["default_branch"], str) or not body["default_branch"]:
            raise _bad("spec.default_branch must be a non-empty string")
        out["default_branch"] = body["default_branch"]
    elif full:
        raise _bad("spec.default_branch is required at project scope")

    _check_output(out.get("output"))
    _check_language(out.get("language"))
    return out


def _check_output(output: dict | None) -> None:
    if output is None:
        return
    for key in ("width", "height", "fps", "max_episode_seconds",
                "min_episode_seconds"):
        value = output[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise _bad(f"spec.output.{key} must be a positive integer")
    if not isinstance(output["aspect_ratio"], str) or not output["aspect_ratio"]:
        raise _bad("spec.output.aspect_ratio must be a non-empty string")
    if output["min_episode_seconds"] > output["max_episode_seconds"]:
        raise _bad("spec.output.min_episode_seconds must not exceed "
                   "max_episode_seconds")


def _check_language(language: dict | None) -> None:
    if language is None:
        return
    if not isinstance(language["primary"], str) or not language["primary"]:
        raise _bad("spec.language.primary must be a non-empty string")
    targets = language["dub_targets"]
    if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
        raise _bad("spec.language.dub_targets must be a list of strings")


def merge_specs(*layers: dict) -> dict:
    """按段覆盖：后一层里出现过的段整体替换。层顺序是 project→series→episode。"""
    merged: dict = {}
    for layer in layers:
        if not layer:
            continue
        for key, value in layer.items():
            merged[key] = value
    return merged


# ------------------------------------------------------------------ 服务

class ProjectCatalogService:
    def __init__(self, conn: sqlite3.Connection, *, authorizer=None,
                 run_probe=None, clock_validator=None, clock=_now):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(conn)
        self._authorize = authorizer
        self._run_probe = run_probe
        self._clock_validator = clock_validator
        self._clock = clock

    # ---- 命令外壳 --------------------------------------------------------

    def _command(self, tenant_id: str, command_id: str, produce):
        """一个命令 = 一个事务，幂等结果在同一事务内写回（§4.1 步骤 9）。

        `produce()` 抛异常时整个事务回滚，包括幂等行——HTTP 层随后会删掉
        自己抢先插入的 IN_PROGRESS 行，所以同 command_id 可以重发。
        """
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    def _guard(self, tenant_id: str, principal_id: str, scope_kind: str,
               scope_id: str, action: str) -> None:
        """闸：S01 授权。**在 BEGIN 之前**问，所以超时不会留下半行数据。

        `authorizer` 未注入时必然失败（503），不是放行 —— 与 S40 同构。
        """
        if self._authorize is None:
            raise ServiceError(
                503, "AUTH_CONTEXT_UNAVAILABLE",
                "no authorizer configured; refusing to write without an "
                "authorization context")
        permission = WRITE_PERMISSIONS.get(action)
        if permission is None:
            raise ServiceError(500, "INTERNAL", f"no permission for {action}")
        verdict = self._authorize(tenant_id, principal_id, scope_kind,
                                  scope_id, permission)
        if not verdict.get("allowed"):
            raise ServiceError(
                403, "PERMISSION_DENIED",
                f"principal {principal_id} may not perform {action} on "
                f"{scope_kind}/{scope_id}",
                details={"permission": permission,
                         "denied": verdict.get("denied") or []})

    # ---- 锁序（§6.3）------------------------------------------------------

    # series → episode → episode_order → spec_revision → outbox。只许从外到内。
    # SQLite 的 BEGIN IMMEDIATE 已经把写者串行化，所以真正的互斥来自事务本身；
    # 这里的辅助方法固定访问顺序，让换成 Postgres 时逐行 FOR UPDATE 的加锁序列
    # 不需要重排代码。
    def _lock_series(self, tenant_id: str, series_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM series WHERE tenant_id=? AND series_id=?",
            (tenant_id, series_id)).fetchone()
        if row is None:
            # 跨租户同码（E28）：别的租户的 series 在这里看起来就是不存在。
            raise ServiceError(404, "REF_NOT_FOUND",
                               f"series {series_id} not found")
        return row

    def _lock_project(self, tenant_id: str, project_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM project WHERE tenant_id=? AND project_id=?",
            (tenant_id, project_id)).fetchone()
        if row is None:
            raise ServiceError(404, "REF_NOT_FOUND",
                               f"project {project_id} not found")
        return row

    # ---- 规格写入与物化 ---------------------------------------------------

    def _latest_spec(self, tenant_id: str, scope_kind: str,
                     scope_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM spec_revision WHERE tenant_id=? AND scope_kind=? "
            "AND scope_id=? ORDER BY revision DESC LIMIT 1",
            (tenant_id, scope_kind, scope_id)).fetchone()

    def _write_spec(self, tenant_id: str, scope_kind: str, scope_id: str,
                    body: dict, author: dict) -> sqlite3.Row:
        """写一条 spec_revision。内容未变则复用现有 revision（T03-35）。

        复用而不是 +1：`content_hash` 相同意味着"规格没变"，再发一版只会让
        `derived_from` 里出现两个指向同一内容的 revision，事后无法解释。
        """
        encoded = _jcs(body)
        digest = _sha256_hex(encoded)
        existing = self.conn.execute(
            "SELECT * FROM spec_revision WHERE tenant_id=? AND scope_kind=? "
            "AND scope_id=? AND content_hash=?",
            (tenant_id, scope_kind, scope_id, digest)).fetchone()
        if existing is not None:
            return existing
        head = self._latest_spec(tenant_id, scope_kind, scope_id)
        revision = 1 if head is None else head["revision"] + 1
        spec_id = _new_id("spec")
        self.conn.execute(
            "INSERT INTO spec_revision (tenant_id, spec_id, scope_kind,"
            " scope_id, revision, body, content_hash, author, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, spec_id, scope_kind, scope_id, revision, encoded,
             digest, _jcs(author), self._clock()))
        return self.conn.execute(
            "SELECT * FROM spec_revision WHERE tenant_id=? AND spec_id=? "
            "AND revision=?", (tenant_id, spec_id, revision)).fetchone()

    def _materialize(self, tenant_id: str, *, project_spec: sqlite3.Row,
                     series_spec: sqlite3.Row | None, episode_id: str,
                     episode_body: dict | None = None,
                     episode_spec: sqlite3.Row | None = None) -> sqlite3.Row:
        """把三层规格合并成这一集的生效规格，落一行（§1.4、§2.5）。"""
        layers = [json.loads(project_spec["body"])]
        if series_spec is not None:
            layers.append(json.loads(series_spec["body"]))
        if episode_body:
            layers.append(episode_body)
        merged = merge_specs(*layers)
        encoded = _jcs(merged)
        digest = _sha256_hex(encoded)
        derived = {
            "project_spec": {"revision": project_spec["revision"],
                             "content_hash": project_spec["content_hash"]},
            "series_spec": None if series_spec is None else
            {"revision": series_spec["revision"],
             "content_hash": series_spec["content_hash"]},
            "episode_spec": None if episode_spec is None else
            {"revision": episode_spec["revision"],
             "content_hash": episode_spec["content_hash"]},
        }
        self.conn.execute(
            "INSERT INTO episode_spec_effective (tenant_id, episode_id,"
            " derived_from, body, content_hash, materialized_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT (tenant_id, episode_id) DO UPDATE SET"
            "  derived_from=excluded.derived_from, body=excluded.body,"
            "  content_hash=excluded.content_hash,"
            "  materialized_at=excluded.materialized_at",
            (tenant_id, episode_id, _jcs(derived), encoded, digest,
             self._clock()))
        return self.conn.execute(
            "SELECT * FROM episode_spec_effective WHERE tenant_id=? AND"
            " episode_id=?", (tenant_id, episode_id)).fetchone()

    # ---- 事件 -------------------------------------------------------------

    def _next_sequence(self, tenant_id: str, aggregate_id: str) -> int:
        """§2.8 要求 (tenant, aggregate, sequence) 唯一，SDK 的 outbox 没这条
        约束（缺口 G8），所以序号在写事务内从 outbox 自己算。"""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS s FROM outbox"
            " WHERE tenant_id=? AND aggregate_id=?",
            (tenant_id, aggregate_id)).fetchone()
        return int(row["s"]) + 1

    def _emit_created(self, tenant_id: str, *, episode_id: str, series_id: str,
                      project_id: str, release_ordinal: int | None,
                      shooting_ordinal: int | None, spec_hash: str,
                      command_id: str, home_cell: str) -> None:
        """`project.episode.created` —— **一集一条**（§5.3）。

        一条事件带 N 集会让 `aggregate_id` 无处可填：填 series 就把所有集
        串行到同一分区。`additionalProperties:false` 意味着这里多一个字段
        就是契约违约，所以 `branch_id` 不放（缺口 PEC-4）。
        """
        sequence = self._next_sequence(tenant_id, episode_id)
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_TYPE,
            subject=f"episode/{episode_id}",
            tenant_id=tenant_id, home_cell=home_cell,
            aggregate_id=episode_id, sequence=sequence,
            correlation_id=command_id, causation_id=command_id,
            data={
                "schema_version": SCHEMA_VERSION,
                "episode_id": episode_id,
                "series_id": series_id,
                "project_id": project_id,
                "release_ordinal": release_ordinal,
                "shooting_ordinal": shooting_ordinal,
                # PEC-1：这里填的是**物化后**的生效规格哈希，不是某个
                # spec_revision.body 的哈希。理由见 schema.CONTRACT_GAPS。
                "spec_hash": spec_hash,
            },
            data_ref={"service_id": SOURCE, "id": episode_id,
                      "revision": 1, "content_hash": spec_hash}))

    def _emit_local(self, tenant_id: str, event_type: str,
                    aggregate_id: str, payload: dict) -> None:
        """未登记的事件走本地表，不走 outbox（§gaps G9）。

        把未注册的 event_type 塞进 outbox，等于让中继去投递一个没有 schema
        的事件——消费方按契约校验会失败，而且没人知道该找谁。
        """
        self.conn.execute(
            "INSERT INTO local_event (tenant_id, local_event_id, event_type,"
            " aggregate_id, payload, created_at) VALUES (?,?,?,?,?,?)",
            (tenant_id, _new_id("local_event"), event_type, aggregate_id,
             _jcs(payload), self._clock()))

    # ---- 警告 -------------------------------------------------------------

    def _warnings(self, tenant_id: str, story_window_ref) -> list[dict]:
        """§4.6 第 11 条：叙事时钟查不到只告警，不拦。

        `clock_validator` 没配时**不产生告警**：`None` 是"没查"，不是
        "查了不存在"。把没查写成"未知时钟"会让每个集都背一条假告警。
        """
        if not isinstance(story_window_ref, dict) or self._clock_validator is None:
            return []
        clock_id = story_window_ref.get("clock_id")
        if not clock_id:
            return []
        known = self._clock_validator(tenant_id, clock_id)
        if known is None:
            return []
        if known:
            return []
        return [{"code": "STORY_CLOCK_UNKNOWN", "clock_id": clock_id,
                 "message": "S16 does not know this clock; the reference is "
                            "stored but not yet resolvable"}]

    # ---- 命令 1：建项目 + 剧 + 首批集 ------------------------------------

    def create_project(self, tenant_id: str, body: dict, *,
                       principal: dict) -> dict:
        command_id = body["command_id"]
        principal_id = principal.get("principal_id") or ""
        org_unit_id = body.get("org_unit_id")
        if not isinstance(org_unit_id, str) or not org_unit_id:
            raise _bad("org_unit_id is required")
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _bad("name is required")
        spec_body = normalize_spec(body.get("spec"), full=True)
        if not PRINCIPAL_IS_WELL_FORMED(principal_id):
            raise _bad("principal.principal_id is required")

        project_id = body.get("project_id") or _new_id("project")
        self._guard(tenant_id, principal_id, "org_unit", org_unit_id,
                    "create_project")

        def produce() -> dict:
            now = self._clock()
            slug = _slugify(name, project_id)
            self.conn.execute(
                "INSERT INTO project (tenant_id, project_id, slug,"
                " display_name, lifecycle, org_unit_id, created_by,"
                " created_at, updated_at, head_revision)"
                " VALUES (?,?,?,?,'draft',?,?,?,?,1)",
                (tenant_id, project_id, slug, name, org_unit_id,
                 _jcs(principal), now, now))
            project_spec = self._write_spec(tenant_id, "project", project_id,
                                            spec_body, principal)
            home_cell = body.get("home_cell") or "cell_a1"

            series_specs: dict[str, sqlite3.Row] = {}
            series_ids: list[str] = []
            for entry in body.get("series") or []:
                series_id = self._insert_series(tenant_id, project_id, entry,
                                                principal)
                series_ids.append(series_id)
                # 剧层 revision=1 存空覆盖 `{}`：建项目时 body 里没有剧级
                # 规格，凭空复制一份项目规格进来会在项目规格变更后变成一份
                # 谁也解释不了的"过期快照"，看起来像剧在覆盖项目。
                series_specs[series_id] = self._write_spec(
                    tenant_id, "series", series_id, {}, principal)

            goals = self._insert_goals(tenant_id, project_id, series_ids,
                                       body.get("goals") or [], now)

            episodes = body.get("episodes") or []
            if not isinstance(episodes, list):
                raise _bad("episodes must be a list")
            created: list[dict] = []
            warnings: list[dict] = []
            # 首批集只挂到第一条剧上：body 形状只有一个扁平 episodes 列表，
            # 而一个项目可能有多条剧。多剧时由调用方用追加命令显式指定。
            default_series = series_ids[0] if series_ids else None
            for ordinal, entry in enumerate(episodes, start=1):
                if not isinstance(entry, dict):
                    raise _bad("each episode must be an object")
                series_id = entry.get("series_id") or default_series
                if series_id is None:
                    raise _bad("episode without series_id needs at least one "
                               "series in the create body")
                if series_id not in series_specs:
                    raise ServiceError(
                        404, "REF_NOT_FOUND",
                        f"series {series_id} is not part of this create")
                episode_id = self._insert_episode(
                    tenant_id, project_id, series_id, entry,
                    release_ordinal=entry.get("release_ordinal", ordinal),
                    shooting_ordinal=entry.get("shooting_ordinal"),
                    principal=principal, home_cell=home_cell,
                    command_id=command_id, project_spec=project_spec,
                    series_spec=series_specs[series_id])
                warnings.extend(self._warnings(tenant_id,
                                               entry.get("story_window_ref")))
                created.append({"episode_id": episode_id,
                                "series_id": series_id})

            self._emit_local(tenant_id, LOCAL_LIFECYCLE, project_id, {
                "scope": "project", "id": project_id, "from": None,
                "to": "draft", "reason": "created"})
            return {
                "project_id": project_id,
                "slug": slug,
                "lifecycle": "draft",
                "head_revision": 1,
                "series_ids": series_ids,
                "episodes": created,
                "goal_ids": goals,
                "spec_revision": {"revision": project_spec["revision"],
                                  "content_hash": project_spec["content_hash"]},
                "warnings": warnings,
                "x-contract-gaps": CONTRACT_GAPS,
            }

        return self._command(tenant_id, command_id, produce)

    def _insert_series(self, tenant_id: str, project_id: str, entry: dict,
                       principal: dict) -> str:
        if not isinstance(entry, dict):
            raise _bad("each series must be an object")
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            raise _bad("series.title is required")
        kind = entry.get("kind") or entry.get("series_kind") or "drama"
        if kind not in SERIES_KINDS:
            raise _bad(f"series.kind must be one of {SERIES_KINDS}")
        target = entry.get("target_episodes")
        if target is not None and (not isinstance(target, int) or target <= 0):
            raise _bad("series.target_episodes must be a positive integer")
        series_id = entry.get("series_id") or _new_id("series")
        now = self._clock()
        self.conn.execute(
            "INSERT INTO series (tenant_id, series_id, project_id, title,"
            " series_kind, target_episodes, lifecycle, created_at, updated_at,"
            " head_revision, branch_default)"
            " VALUES (?,?,?,?,?,?,'draft',?,?,1,?)",
            (tenant_id, series_id, project_id, title, kind, target, now, now,
             entry.get("branch_default") or "canon"))
        return series_id

    def _insert_goals(self, tenant_id: str, project_id: str,
                      series_ids: list[str], goals: list,
                      now: str) -> list[str]:
        out: list[str] = []
        for goal in goals:
            if not isinstance(goal, dict):
                raise _bad("each goal must be an object")
            scope_kind = goal.get("scope_kind") or "project"
            if scope_kind == "project":
                scope_id = project_id
            elif scope_kind == "series":
                scope_id = goal.get("scope_id")
                if scope_id not in series_ids:
                    raise ServiceError(404, "REF_NOT_FOUND",
                                       f"goal scope_id {scope_id} is not part "
                                       "of this create")
            else:
                raise _bad("goal.scope_kind must be project or series")
            goal_id = _new_id("goal")
            self.conn.execute(
                "INSERT INTO business_goal (tenant_id, goal_id, scope_kind,"
                " scope_id, goal_kind, target, due_at, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,'active',?)",
                (tenant_id, goal_id, scope_kind, scope_id,
                 goal.get("goal_kind"), _jcs(goal.get("target")),
                 goal.get("due_at"), now))
            out.append(goal_id)
        return out

    def _insert_episode(self, tenant_id: str, project_id: str, series_id: str,
                        entry: dict, *, release_ordinal, shooting_ordinal,
                        principal: dict, home_cell: str, command_id: str,
                        project_spec: sqlite3.Row,
                        series_spec: sqlite3.Row) -> str:
        """写集 + 两条排期 + 物化规格 + 一条事件。**不带 episode_number。**"""
        episode_id = entry.get("episode_id") or _new_id("episode")
        now = self._clock()
        story_window_ref = entry.get("story_window_ref")
        if story_window_ref is not None and not isinstance(story_window_ref, dict):
            raise _bad("story_window_ref must be an object")
        self.conn.execute(
            "INSERT INTO episode (tenant_id, episode_id, series_id,"
            " project_id, title, lifecycle, story_window_ref, release_window,"
            " created_at, updated_at, head_revision)"
            " VALUES (?,?,?,?,?,'draft',?,?,?,?,1)",
            (tenant_id, episode_id, series_id, project_id, entry.get("title"),
             None if story_window_ref is None else _jcs(story_window_ref),
             None if entry.get("release_window") is None
             else _jcs(entry["release_window"]),
             now, now))

        release_ord = None if release_ordinal is None else int(release_ordinal)
        shooting_ord = None if shooting_ordinal is None else int(shooting_ordinal)
        for ordinal, kind in ((release_ord, "release"),
                              (shooting_ord, "shooting")):
            if ordinal is None:
                # 纯草稿集：不排期也合法（§4.2）。下游只按 ID 引用。
                continue
            self._insert_order(tenant_id, series_id, kind, "", ordinal,
                               episode_id, principal)

        effective = self._materialize(
            tenant_id, project_spec=project_spec, series_spec=series_spec,
            episode_id=episode_id)
        self._emit_created(
            tenant_id, episode_id=episode_id, series_id=series_id,
            project_id=project_id, release_ordinal=release_ord,
            shooting_ordinal=shooting_ord,
            spec_hash=effective["content_hash"], command_id=command_id,
            home_cell=home_cell)
        return episode_id

    def _insert_order(self, tenant_id: str, series_id: str, kind: str,
                      window_key: str, ordinal: int, episode_id: str,
                      principal: dict) -> None:
        if ordinal < 1:
            raise ServiceError(422, "ORDER_SET_MISMATCH",
                               f"{kind} ordinal must be >= 1")
        if kind == "rebroadcast" and not window_key:
            raise _bad("rebroadcast orders require a window_key")
        try:
            self.conn.execute(
                "INSERT INTO episode_order (tenant_id, order_id, series_id,"
                " order_kind, window_key, ordinal, episode_id, assigned_by,"
                " assigned_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, _new_id("order"), series_id, kind, window_key,
                 ordinal, episode_id, _jcs(principal), self._clock()))
        except sqlite3.IntegrityError as exc:
            text = str(exc)
            if "episode_order.tenant_id, episode_order.series_id" in text \
                    and "ordinal" in text:
                raise ServiceError(
                    422, "ORDER_SET_MISMATCH",
                    f"{kind} ordinal {ordinal} is already taken in this series",
                    details={"order_kind": kind, "window_key": window_key,
                             "ordinal": ordinal}) from exc
            if "episode_id" in text:
                raise ServiceError(
                    422, "DUPLICATE_EPISODE_IN_ORDER",
                    f"episode {episode_id} already has a {kind} order in this "
                    "series", details={"order_kind": kind}) from exc
            raise

    # ---- 命令 2：追加集 ---------------------------------------------------

    def append_episodes(self, tenant_id: str, project_id: str, body: dict, *,
                        principal: dict) -> dict:
        command_id = body["command_id"]
        principal_id = principal.get("principal_id") or ""
        if not PRINCIPAL_IS_WELL_FORMED(principal_id):
            raise _bad("principal.principal_id is required")
        entries = body.get("episodes")
        if not isinstance(entries, list) or not entries:
            raise _bad("episodes must be a non-empty list")
        self._guard(tenant_id, principal_id, "project", project_id,
                    "append_episodes")

        def produce() -> dict:
            project = self._lock_project(tenant_id, project_id)
            if project["lifecycle"] == "archived":
                # 归档不级联停 Run（C03-4），但归档项目下的写入要拒。
                raise ServiceError(
                    409, "PROJECT_ARCHIVED",
                    "project is archived; unarchive is out of scope for this "
                    "version", details={"project_id": project_id})
            created: list[dict] = []
            warnings: list[dict] = []
            home_cell = body.get("home_cell") or "cell_a1"
            for entry in entries:
                if not isinstance(entry, dict):
                    raise _bad("each episode must be an object")
                series_id = entry.get("series_id") or body.get("series_id")
                if not series_id:
                    raise _bad("series_id is required (body or per episode)")
                series = self._lock_series(tenant_id, series_id)
                if series["project_id"] != project_id:
                    # 跨项目引用同一租户：不是 404，是"这集不在这里"。
                    raise ServiceError(
                        404, "REF_NOT_FOUND",
                        f"series {series_id} does not belong to project "
                        f"{project_id}")
                expected = body.get("expected_series_revision")
                if expected is not None and int(expected) != series["head_revision"]:
                    raise ServiceError(
                        409, "REVISION_CONFLICT",
                        "series has moved on since this request was prepared",
                        details={"current_revision": series["head_revision"],
                                 "resource": "series",
                                 "id": series_id})
                project_spec = self._latest_spec(tenant_id, "project",
                                                 project_id)
                if project_spec is None:
                    raise ServiceError(500, "INTERNAL",
                                       "project has no spec revision")
                series_spec = self._latest_spec(tenant_id, "series", series_id)
                episode_id = entry.get("episode_id") or _new_id("episode")
                now = self._clock()
                story_window_ref = entry.get("story_window_ref")
                if story_window_ref is not None and not isinstance(
                        story_window_ref, dict):
                    raise _bad("story_window_ref must be an object")
                self.conn.execute(
                    "INSERT INTO episode (tenant_id, episode_id, series_id,"
                    " project_id, title, lifecycle, story_window_ref,"
                    " release_window, created_at, updated_at, head_revision)"
                    " VALUES (?,?,?,?,?,'draft',?,?,?,?,1)",
                    (tenant_id, episode_id, series_id, project_id,
                     entry.get("title"),
                     None if story_window_ref is None
                     else _jcs(story_window_ref),
                     None if entry.get("release_window") is None
                     else _jcs(entry["release_window"]), now, now))
                release_ord = entry.get("release_ordinal")
                shooting_ord = entry.get("shooting_ordinal")
                for ordinal, kind in ((release_ord, "release"),
                                      (shooting_ord, "shooting")):
                    if ordinal is None:
                        continue
                    self._insert_order(tenant_id, series_id, kind, "",
                                       int(ordinal), episode_id, principal)
                effective = self._materialize(
                    tenant_id, project_spec=project_spec,
                    series_spec=series_spec, episode_id=episode_id)
                self._emit_created(
                    tenant_id, episode_id=episode_id, series_id=series_id,
                    project_id=project_id,
                    release_ordinal=None if release_ord is None
                    else int(release_ord),
                    shooting_ordinal=None if shooting_ord is None
                    else int(shooting_ord),
                    spec_hash=effective["content_hash"],
                    command_id=command_id, home_cell=home_cell)
                warnings.extend(self._warnings(tenant_id, story_window_ref))
                created.append({"episode_id": episode_id,
                                "series_id": series_id})
            return {
                "project_id": project_id,
                "episodes": created,
                # 未排期的集在 episode_order 里没有行，UI 应显示在"未排期"区。
                "unsequenced": [c["episode_id"] for c in created
                                if self._has_no_order(tenant_id,
                                                      c["episode_id"])],
                "warnings": warnings,
                "x-contract-gaps": CONTRACT_GAPS,
            }

        return self._command(tenant_id, command_id, produce)

    def _has_no_order(self, tenant_id: str, episode_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM episode_order WHERE tenant_id=? AND episode_id=?"
            " LIMIT 1", (tenant_id, episode_id)).fetchone()
        return row is None

    # ---- 命令 3：重排（最有风险的路径）-----------------------------------

    def reorder_episodes(self, tenant_id: str, series_id: str, body: dict, *,
                         principal: dict) -> dict:
        command_id = body["command_id"]
        principal_id = principal.get("principal_id") or ""
        if not PRINCIPAL_IS_WELL_FORMED(principal_id):
            raise _bad("principal.principal_id is required")
        kind = body.get("order_kind")
        if kind not in ORDER_KINDS:
            raise _bad(f"order_kind must be one of {ORDER_KINDS}")
        window_key = body.get("window_key")
        window_key = "" if window_key is None else str(window_key)
        if kind == "rebroadcast" and not window_key:
            raise _bad("rebroadcast reorder requires a window_key")
        if kind == "release" and window_key:
            raise _bad("release orders have an empty window_key")
        order = body.get("order")
        if not isinstance(order, list) or not order:
            raise _bad("order must be a non-empty list of episode ids")
        if len(set(order)) != len(order):
            # 重复不是重排的中间态，是数据错误（§4.6 第 6 条）。
            dupes = sorted({e for e in order if order.count(e) > 1})
            raise ServiceError(
                422, "DUPLICATE_EPISODE_IN_ORDER",
                "an episode appears more than once in the new order",
                details={"duplicates": dupes})
        self._guard(tenant_id, principal_id, "series", series_id,
                    "reorder_episodes")

        def produce() -> dict:
            series = self._lock_series(tenant_id, series_id)
            expected = body.get("expected_head_revision")
            if expected is not None and int(expected) != series["head_revision"]:
                raise ServiceError(
                    409, "REVISION_CONFLICT",
                    "series has moved on since this list was loaded; re-read "
                    "GET /series/{id}/orders and resubmit",
                    details={"current_revision": series["head_revision"],
                             "resource": "series", "id": series_id})

            rows = self.conn.execute(
                "SELECT order_id, episode_id, ordinal FROM episode_order"
                " WHERE tenant_id=? AND series_id=? AND order_kind=?"
                " AND window_key=? ORDER BY ordinal",
                (tenant_id, series_id, kind, window_key)).fetchall()
            current = [r["episode_id"] for r in rows]
            if set(current) != set(order):
                # 422 而不是 409：调用方要改请求（重新取列表），不是重试。
                raise ServiceError(
                    422, "ORDER_SET_MISMATCH",
                    "the new order must cover exactly the episodes that "
                    "currently have this order kind; adding or removing "
                    "episodes is a separate command",
                    details={"missing": sorted(set(current) - set(order)),
                             "extra": sorted(set(order) - set(current)),
                             "order_kind": kind, "window_key": window_key})
            by_episode = {r["episode_id"]: r["order_id"] for r in rows}
            now = self._clock()

            # 步骤 3：整体抬高，避开唯一约束的中间冲突。不是多余——不依赖
            # 约束检查时机，SQLite 也没有 DEFERRABLE（§4.3）。
            self.conn.execute(
                "UPDATE episode_order SET ordinal = ordinal + ?"
                " WHERE tenant_id=? AND series_id=? AND order_kind=?"
                " AND window_key=?", (ORDINAL_OFFSET, tenant_id, series_id,
                                      kind, window_key))
            # 步骤 4：按新顺序写回 1..N。**episode_id 不动。**
            for position, episode_id in enumerate(order, start=1):
                self.conn.execute(
                    "UPDATE episode_order SET ordinal=?, assigned_by=?,"
                    " assigned_at=? WHERE tenant_id=? AND order_id=?",
                    (position, _jcs(principal), now, tenant_id,
                     by_episode[episode_id]))
            # 步骤 5：CAS 推进 head_revision。
            self.conn.execute(
                "UPDATE series SET head_revision = head_revision + 1,"
                " updated_at=? WHERE tenant_id=? AND series_id=?",
                (now, tenant_id, series_id))
            head = series["head_revision"] + 1

            self._emit_local(tenant_id, LOCAL_REORDERED, series_id, {
                "series_id": series_id, "order_kind": kind,
                "window_key": window_key, "order": list(order),
                "head_revision": head})
            return {
                "series_id": series_id,
                "order_kind": kind,
                "window_key": window_key,
                "order": [{"episode_id": e, "ordinal": i}
                          for i, e in enumerate(order, start=1)],
                "head_revision": head,
                # 重排不发 project.episode.created（C03-3）：那不是新集。
                "x-contract-gaps": CONTRACT_GAPS,
            }

        return self._command(tenant_id, command_id, produce)

    # ---- 命令 4：发布剧规格 revision --------------------------------------

    def publish_spec_revision(self, tenant_id: str, series_id: str, body: dict,
                              *, principal: dict) -> dict:
        command_id = body["command_id"]
        principal_id = principal.get("principal_id") or ""
        if not PRINCIPAL_IS_WELL_FORMED(principal_id):
            raise _bad("principal.principal_id is required")
        if body.get("apply_to_existing"):
            # 改画幅意味着重做全部关键帧、全部视频、重算字幕安全区。
            # 那不是目录服务能顺手决定的事（§4.5）。
            raise ServiceError(
                422, "APPLY_TO_EXISTING_UNSUPPORTED",
                "apply_to_existing must be false in this version: changing the "
                "spec of episodes already in production must go through S07's "
                "ChangePlan so S09 can compute the impact closure")
        override = normalize_spec(body.get("body"), full=False)
        self._guard(tenant_id, principal_id, "series", series_id,
                    "publish_spec")

        def produce() -> dict:
            series = self._lock_series(tenant_id, series_id)
            expected = body.get("expected_head_revision")
            if expected is not None and int(expected) != series["head_revision"]:
                raise ServiceError(
                    409, "REVISION_CONFLICT",
                    "series has moved on since this request was prepared",
                    details={"current_revision": series["head_revision"],
                             "resource": "series", "id": series_id})
            revision = self._write_spec(tenant_id, "series", series_id,
                                        override, principal)
            now = self._clock()
            self.conn.execute(
                "UPDATE series SET head_revision = head_revision + 1,"
                " updated_at=? WHERE tenant_id=? AND series_id=?",
                (now, tenant_id, series_id))
            self._emit_local(tenant_id, LOCAL_SPEC_CHANGED, series_id, {
                "scope": "series", "id": series_id,
                "revision": revision["revision"],
                "content_hash": revision["content_hash"],
                "applied_to_existing": False})
            return {
                "series_id": series_id,
                "spec_revision": {"revision": revision["revision"],
                                  "content_hash": revision["content_hash"]},
                "head_revision": series["head_revision"] + 1,
                # 已有集的生效规格**没有**被改：要改得走 S07（§4.5）。
                "applied_to_existing": 0,
                "note": "existing episodes keep their materialized spec; use "
                        "S07 ChangePlan to change them",
                "x-contract-gaps": CONTRACT_GAPS,
            }

        return self._command(tenant_id, command_id, produce)

    # ---- 命令 5：归档 -----------------------------------------------------

    def archive_project(self, tenant_id: str, project_id: str, body: dict, *,
                        principal: dict) -> dict:
        command_id = body["command_id"]
        principal_id = principal.get("principal_id") or ""
        if not PRINCIPAL_IS_WELL_FORMED(principal_id):
            raise _bad("principal.principal_id is required")
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise _bad("archive requires a reason")
        self._guard(tenant_id, principal_id, "project", project_id,
                    "archive_project")

        def produce() -> dict:
            project = self._lock_project(tenant_id, project_id)
            expected = body.get("expected_head_revision")
            if expected is not None \
                    and int(expected) != project["head_revision"]:
                raise ServiceError(
                    409, "REVISION_CONFLICT",
                    "project has moved on since this request was prepared",
                    details={"current_revision": project["head_revision"],
                             "resource": "project", "id": project_id})
            if project["lifecycle"] == "archived":
                raise ServiceError(409, "ALREADY_ARCHIVED",
                                   "project is already archived")
            now = self._clock()
            self.conn.execute(
                "UPDATE project SET lifecycle='archived', archived_at=?,"
                " archive_reason=?, head_revision=head_revision+1,"
                " updated_at=? WHERE tenant_id=? AND project_id=?",
                (now, reason, now, tenant_id, project_id))
            # 只标记项目，不级联改 episode/series 的 lifecycle：那会让
            # "这一集做完了没有"这个答案随归档动作改变，而它属于 S08。
            self._emit_local(tenant_id, LOCAL_LIFECYCLE, project_id, {
                "scope": "project", "id": project_id,
                "from": project["lifecycle"], "to": "archived",
                "reason": reason})

            # §4.6 第 13 条：有在跑的 Run 不阻断归档，只告警。
            # run_probe 没配时返回 None（没查），此时**不报**告警——
            # count=0 会被读成"已核实没有在跑的东西"（缺口 G11）。
            warnings: list[dict] = []
            active = None
            if self._run_probe is not None:
                active = self._run_probe(tenant_id, project_id)
            if active:
                warnings.append({"code": "ACTIVE_RUNS_EXIST", "count": active})
            return {
                "project_id": project_id,
                "lifecycle": "archived",
                "archived_at": now,
                "head_revision": project["head_revision"] + 1,
                "warnings": warnings,
                "active_runs_counted": active is not None,
                # 归档不停止在途 Run（C03-4）：准入拒绝是 S21/S26/S02 的义务，
                # 本服务只让这个事实可查。
                "x-contract-gaps": CONTRACT_GAPS,
            }

        return self._command(tenant_id, command_id, produce)

    # ---- 命令 6：成员 -----------------------------------------------------

    def add_member(self, tenant_id: str, project_id: str, body: dict, *,
                   principal: dict) -> dict:
        command_id = body["command_id"]
        principal_id = principal.get("principal_id") or ""
        if not PRINCIPAL_IS_WELL_FORMED(principal_id):
            raise _bad("principal.principal_id is required")
        target = body.get("principal_id")
        if not isinstance(target, str) or not target:
            raise _bad("principal_id is required")
        role_key = body.get("role_key")
        if not isinstance(role_key, str) or not role_key:
            raise _bad("role_key is required")
        scope_kind = body.get("scope_kind") or "project"
        scope_id = body.get("scope_id")
        if scope_kind not in ("project", "series", "episode"):
            raise _bad("scope_kind must be project, series or episode")
        if scope_kind == "project":
            scope_id = None
        elif not scope_id:
            raise _bad(f"scope_id is required for scope_kind={scope_kind}")
        self._guard(tenant_id, principal_id, "project", project_id,
                    "grant_member")

        def produce() -> dict:
            self._lock_project(tenant_id, project_id)
            existing = self.conn.execute(
                "SELECT * FROM member WHERE tenant_id=? AND project_id=?"
                " AND principal_id=? AND role_key=? AND scope_kind=?"
                " AND COALESCE(scope_id,'')=?",
                (tenant_id, project_id, target, role_key, scope_kind,
                 scope_id or "")).fetchone()
            if existing is not None and existing["revoked_at"] is None:
                return {
                    "member_id": existing["member_id"],
                    "principal_id": target, "role_key": role_key,
                    "scope_kind": scope_kind, "scope_id": scope_id,
                    "already_assigned": True,
                    # 这是"指派"，不是"授权"（反模式 8）。S01 才是权限权威。
                    "note": "membership is an assignment record, not a "
                            "permission grant; S01 decides access",
                    "x-contract-gaps": CONTRACT_GAPS,
                }
            member_id = _new_id("member")
            self.conn.execute(
                "INSERT INTO member (tenant_id, member_id, project_id,"
                " principal_id, role_key, scope_kind, scope_id, granted_by,"
                " granted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, member_id, project_id, target, role_key,
                 scope_kind, scope_id, _jcs(principal), self._clock()))
            return {
                "member_id": member_id,
                "principal_id": target, "role_key": role_key,
                "scope_kind": scope_kind, "scope_id": scope_id,
                "already_assigned": False,
                "note": "membership is an assignment record, not a permission "
                        "grant; S01 decides access",
                "x-contract-gaps": CONTRACT_GAPS,
            }

        return self._command(tenant_id, command_id, produce)

    # ---- 生命周期投影（C03-8）-------------------------------------------

    def apply_acceptance_projection(self, tenant_id: str, episode_id: str, *,
                                    has_accepted_master: bool,
                                    evidence: dict | None = None) -> dict:
        """把 S08 的"有 approved master"投影成本集的 `completed`。

        **`completed` 不是终态**（§3.2）：S08 新建 revision 并把 master 退回
        未接受时，这里必须能把它改回 `in_production`。"终态不倒退"在本服务
        只适用于 `archived`。
        """
        row = self.conn.execute(
            "SELECT * FROM episode WHERE tenant_id=? AND episode_id=?",
            (tenant_id, episode_id)).fetchone()
        if row is None:
            raise ServiceError(404, "REF_NOT_FOUND",
                               f"episode {episode_id} not found")
        if row["lifecycle"] == "archived":
            # 归档是终态：投影不许把它拉回来。
            return {"episode_id": episode_id, "lifecycle": "archived",
                    "changed": False}
        target = "completed" if has_accepted_master else "in_production"
        if row["lifecycle"] == target:
            return {"episode_id": episode_id, "lifecycle": target,
                    "changed": False}
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE episode SET lifecycle=?, updated_at=?,"
                " head_revision=head_revision+1"
                " WHERE tenant_id=? AND episode_id=?",
                (target, self._clock(), tenant_id, episode_id))
            self._emit_local(tenant_id, LOCAL_LIFECYCLE, episode_id, {
                "scope": "episode", "id": episode_id,
                "from": row["lifecycle"], "to": target,
                "reason": "s08 acceptance projection",
                "evidence": evidence or {}})
        return {"episode_id": episode_id, "lifecycle": target, "changed": True}

    # ---- 查询 -------------------------------------------------------------

    def get_project(self, tenant_id: str, project_id: str) -> dict:
        project = self._lock_project(tenant_id, project_id)
        series = self.conn.execute(
            "SELECT * FROM series WHERE tenant_id=? AND project_id=?"
            " ORDER BY created_at, series_id",
            (tenant_id, project_id)).fetchall()
        goals = self.conn.execute(
            "SELECT * FROM business_goal WHERE tenant_id=? AND scope_kind="
            "'project' AND scope_id=? ORDER BY created_at",
            (tenant_id, project_id)).fetchall()
        return {
            "project_id": project["project_id"],
            "slug": project["slug"],
            "display_name": project["display_name"],
            "lifecycle": project["lifecycle"],
            "org_unit_id": project["org_unit_id"],
            "head_revision": project["head_revision"],
            "archived_at": project["archived_at"],
            "archive_reason": project["archive_reason"],
            "series": [{"series_id": s["series_id"], "title": s["title"],
                        "kind": s["series_kind"],
                        "lifecycle": s["lifecycle"],
                        "target_episodes": s["target_episodes"],
                        "branch_default": s["branch_default"],
                        "head_revision": s["head_revision"]} for s in series],
            "goals": [{"goal_id": g["goal_id"], "goal_kind": g["goal_kind"],
                       "target": json.loads(g["target"]),
                       "status": g["status"], "due_at": g["due_at"]}
                      for g in goals],
            "source_revision": project["head_revision"],
            "read_at": self._clock(),
            "x-contract-gaps": CONTRACT_GAPS,
        }

    def list_projects(self, tenant_id: str, *, org_unit_id: str | None = None,
                      lifecycle: str | None = None, limit: int = 50) -> dict:
        sql = ("SELECT * FROM project WHERE tenant_id=?")
        params: list = [tenant_id]
        if org_unit_id:
            sql += " AND org_unit_id=?"
            params.append(org_unit_id)
        if lifecycle:
            sql += " AND lifecycle=?"
            params.append(lifecycle)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        rows = self.conn.execute(sql, params).fetchall()
        return {"projects": [{"project_id": r["project_id"],
                              "slug": r["slug"],
                              "display_name": r["display_name"],
                              "lifecycle": r["lifecycle"],
                              "org_unit_id": r["org_unit_id"],
                              "head_revision": r["head_revision"]}
                             for r in rows],
                "read_at": self._clock(),
                "x-contract-gaps": CONTRACT_GAPS}

    def list_episodes(self, tenant_id: str, project_id: str) -> dict:
        self._lock_project(tenant_id, project_id)
        rows = self.conn.execute(
            "SELECT * FROM episode WHERE tenant_id=? AND project_id=?"
            " ORDER BY created_at, episode_id",
            (tenant_id, project_id)).fetchall()
        orders = self.conn.execute(
            "SELECT episode_id, order_kind, window_key, ordinal"
            " FROM episode_order WHERE tenant_id=? AND episode_id IN"
            " (SELECT episode_id FROM episode WHERE tenant_id=?"
            "  AND project_id=?) ORDER BY order_kind, window_key, ordinal",
            (tenant_id, tenant_id, project_id)).fetchall()
        by_episode: dict[str, dict] = {}
        for o in orders:
            by_episode.setdefault(o["episode_id"], {}).setdefault(
                o["order_kind"], []).append(
                {"ordinal": o["ordinal"], "window_key": o["window_key"]})
        items = []
        for r in rows:
            slots = by_episode.get(r["episode_id"], {})
            items.append({
                "episode_id": r["episode_id"],
                "series_id": r["series_id"],
                "title": r["title"],
                "lifecycle": r["lifecycle"],
                "head_revision": r["head_revision"],
                "orders": slots,
                # 未排期不是错误：草稿集就是这样（§4.2）。
                "sequenced": bool(slots),
            })
        return {"project_id": project_id, "episodes": items,
                "unscheduled": [i["episode_id"] for i in items
                                if not i["sequenced"]],
                "read_at": self._clock(),
                "x-contract-gaps": CONTRACT_GAPS}

    def get_episode_context(self, tenant_id: str, episode_id: str) -> dict:
        """`GET /episodes/{id}` —— 全链路最热（§4.4）。

        **不返回 scene_ids**：调用方会把它当权威。要场次请走 S05 或
        `GET /episodes/{id}/scenes`（那条带 `authoritative_source`）。
        """
        row = self.conn.execute(
            "SELECT e.*, s.title AS series_title, s.project_id AS series_project,"
            " p.lifecycle AS project_lifecycle"
            " FROM episode e"
            " JOIN series s ON s.tenant_id=e.tenant_id"
            "  AND s.series_id=e.series_id"
            " JOIN project p ON p.tenant_id=e.tenant_id"
            "  AND p.project_id=e.project_id"
            " WHERE e.tenant_id=? AND e.episode_id=?",
            (tenant_id, episode_id)).fetchone()
        if row is None:
            raise ServiceError(404, "REF_NOT_FOUND",
                               f"episode {episode_id} not found")
        orders = self.conn.execute(
            "SELECT order_kind, window_key, ordinal FROM episode_order"
            " WHERE tenant_id=? AND episode_id=?"
            " ORDER BY order_kind, window_key, ordinal",
            (tenant_id, episode_id)).fetchall()
        slots: dict[str, dict] = {}
        multi: list[str] = []
        for o in orders:
            kind = o["order_kind"]
            if kind in slots:
                multi.append(kind)
                continue
            slots[kind] = {"ordinal": o["ordinal"],
                           "window_key": o["window_key"]}
        effective = self.conn.execute(
            "SELECT * FROM episode_spec_effective WHERE tenant_id=? AND"
            " episode_id=?", (tenant_id, episode_id)).fetchone()
        spec = None
        if effective is not None:
            body = json.loads(effective["body"])
            spec = {"content_hash": effective["content_hash"],
                    "output": body.get("output")}
        return {
            "episode_id": row["episode_id"],
            "series_id": row["series_id"],
            "project_id": row["project_id"],
            "tenant_id": tenant_id,
            "lifecycle": row["lifecycle"],
            "project_lifecycle": row["project_lifecycle"],
            # 同一 order_kind 有多行时只给最小的那条，并明确告知还有别的
            # 窗口 —— 静默挑一条会让调用方拿错窗口的序号。
            "orders": {"release": slots.get("release"),
                       "shooting": slots.get("shooting")},
            "orders_with_multiple_windows": sorted(set(multi)),
            "story_window_ref": None if row["story_window_ref"] is None
            else json.loads(row["story_window_ref"]),
            "spec": spec,
            "source_revision": row["head_revision"],
            "read_at": self._clock(),
            "warnings": self._warnings(
                tenant_id,
                None if row["story_window_ref"] is None
                else json.loads(row["story_window_ref"])),
            "x-contract-gaps": CONTRACT_GAPS,
        }

    def get_series_orders(self, tenant_id: str, series_id: str, *,
                          order_kind: str, window_key: str = "") -> dict:
        if order_kind not in ORDER_KINDS:
            raise _bad(f"order_kind must be one of {ORDER_KINDS}")
        series = self._lock_series(tenant_id, series_id)
        rows = self.conn.execute(
            "SELECT * FROM episode_order WHERE tenant_id=? AND series_id=?"
            " AND order_kind=? AND window_key=? ORDER BY ordinal",
            (tenant_id, series_id, order_kind, window_key)).fetchall()
        return {
            "series_id": series_id,
            "order_kind": order_kind,
            "window_key": window_key,
            "order": [{"episode_id": r["episode_id"],
                       "ordinal": r["ordinal"]} for r in rows],
            "head_revision": series["head_revision"],
            "read_at": self._clock(),
            # 列表可能是空的：这一 kind 还没排过，不等于系列没有集。
            "sequenced_count": len(rows),
            "x-contract-gaps": CONTRACT_GAPS,
        }

    def get_spec_revision(self, tenant_id: str, series_id: str,
                          revision: int) -> dict:
        self._lock_series(tenant_id, series_id)
        row = self.conn.execute(
            "SELECT * FROM spec_revision WHERE tenant_id=? AND scope_kind="
            "'series' AND scope_id=? AND revision=?",
            (tenant_id, series_id, int(revision))).fetchone()
        if row is None:
            raise ServiceError(404, "REF_NOT_FOUND",
                               f"spec revision {revision} not found")
        return {"scope_kind": "series", "scope_id": series_id,
                "revision": row["revision"], "body": json.loads(row["body"]),
                "content_hash": row["content_hash"],
                "created_at": row["created_at"],
                "read_at": self._clock(),
                "x-contract-gaps": CONTRACT_GAPS}

    def get_episode_scenes(self, tenant_id: str, episode_id: str) -> dict:
        """**代理** S05 的场次投影（§5.2），不是权威。

        `authoritative_source` 必须在响应里：否则未来读到这里的人会往
        `episode_scene_projection` 写数据（反模式 12）。投影为空时
        `stale=true`，**不能**读成"这一集没有场次"。
        """
        row = self.conn.execute(
            "SELECT 1 FROM episode WHERE tenant_id=? AND episode_id=?",
            (tenant_id, episode_id)).fetchone()
        if row is None:
            raise ServiceError(404, "REF_NOT_FOUND",
                               f"episode {episode_id} not found")
        scenes = self.conn.execute(
            "SELECT scene_id, scene_ordinal, breakdown_ref, applied_seq"
            " FROM episode_scene_projection WHERE tenant_id=? AND episode_id=?"
            " ORDER BY scene_ordinal, scene_id",
            (tenant_id, episode_id)).fetchall()
        watermark = self.conn.execute(
            "SELECT sequence, updated_at FROM projection_watermark"
            " WHERE tenant_id=? AND source='S05.scenes'",
            (tenant_id,)).fetchone()
        max_applied = max((s["applied_seq"] for s in scenes), default=0)
        behind = watermark is None or watermark["sequence"] > max_applied
        return {
            "episode_id": episode_id,
            "authoritative_source": "S05",
            "scenes": [{"scene_id": s["scene_id"],
                        "scene_ordinal": s["scene_ordinal"],
                        "breakdown_ref": json.loads(s["breakdown_ref"])}
                       for s in scenes],
            "stale": behind,
            "projection_watermark": None if watermark is None else
            {"source": "S05.scenes", "sequence": watermark["sequence"],
             "updated_at": watermark["updated_at"]},
            "read_at": self._clock(),
            "note": "this is a proxy of S05; an empty list with stale=true "
                    "means 'not projected yet', not 'this episode has no "
                    "scenes'",
            "x-contract-gaps": CONTRACT_GAPS,
        }

    # ---- 后台：幂等行清扫（§6.1）----------------------------------------

    def sweep_stale_commands(self, *, older_than_seconds: int = 300) -> int:
        """把卡住的 IN_PROGRESS 幂等行删掉，让重发被当作新请求。

        SDK 没有 `ABANDONED` 状态（缺口 G7），`result IS NULL` 就是进行中。
        删掉与「置 ABANDONED 后允许重发」的**事后状态**相同：同一
        command_id 可以重新执行。留着一个永不完成的进行中行才会让它永远
        409。
        """
        cutoff = self._clock()
        moment = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) \
            - timedelta(seconds=older_than_seconds)
        boundary = moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        with Transaction(self.conn):
            cur = self.conn.execute(
                "DELETE FROM idempotency_record WHERE result IS NULL"
                " AND created_at < ?", (boundary,))
            return cur.rowcount


def PRINCIPAL_IS_WELL_FORMED(principal_id: str) -> bool:
    return isinstance(principal_id, str) and bool(principal_id.strip())


def _slugify(name: str, project_id: str) -> str:
    """slug 只是给人看的路径片段，冲突时退回 id 后缀。"""
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in name.lower())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    if not cleaned:
        cleaned = project_id
    return cleaned[:60]
