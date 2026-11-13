"""S05 单测。编号对应 `plans/S05-breakdown.md` §9 的 T05-01 … T05-39。

上游全部是 stub，按真实的上游契约写：

- S04 返回 `state` / `content_hash` / `line_set_hash` / `lines`；`line_set_hash`
  必须与行列表**自洽**，否则内核按「上游自相矛盾」503（C7 / S05-G25）。行顺序
  只由 `seq` 表达，stub 也照这个形状给。
- S18 返回 `{pack_id, content_hash, stale}`；缺失/不可用降级为 `partial`。
- S19 返回 `{proposal}`；只读 `result.proposal`，不读任务状态。

结构性断言一律查库（`PRAGMA table_info` / `sqlite_master` / 直接撞 CHECK）而不是
查响应体：本服务最严重的错误是长出 `accepted`、`scene_order`、`asset_id`、
`shot_projection` 这类第二权威，响应体里少个键证明不了什么，少一列才能。
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)
from event_sdk import Inbox

from breakdown.deps import (
    build_agent_task_source,
    build_context_pack_source,
    build_script_source,
)
from breakdown.schema import (
    AUTHORITATIVE_SOURCE,
    CONTRACT_GAPS,
    DEFAULT_CONTINUITY,
    ID_PREFIX,
    LOCAL_EVENT_REORDERED,
    LOCAL_EVENT_RETIRED,
    LOCAL_EVENT_SEALED,
    LOCAL_EVENT_SUPERSEDED,
    LOCAL_EVENT_TYPES,
    MAX_SCENES_HARD,
    ORDER_OFFSET,
    REVISION_STATES,
    REVISION_TRANSITIONS,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC,
)
from breakdown.service import (
    EVENT_REVISION_CREATED,
    RESOLUTION_TTL_SECONDS,
    BreakdownService,
    ServiceError,
    Transaction,
    breakdown_content_hash,
    content_fingerprint,
    line_set_digest,
    scene_content_projection,
)

HUMAN = {"principal_id": "u_human", "principal_type": "human"}
AGENT = {"principal_id": "u_agent", "principal_type": "agent"}
ROOT = Path(__file__).resolve().parents[3]

_UNSET = object()


# ------------------------------------------------------------------ 数据形状

def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lsh(lines) -> str:
    """与 S04 / S05 同一个算法（`line_set_digest` 的输入形状）。"""
    return line_set_digest(
        (item["line_id"], item.get("line_revision", 1), item["text_hash"])
        for item in lines)


def make_lines(count=10, *, speakers=None, prefix="lin"):
    """`seq` 从 1 开始、连续、唯一。`speakers={seq: label}` 可覆盖发言人。"""
    out = []
    for seq in range(1, count + 1):
        text = f"台词{seq}"
        label = "陈默" if speakers is None else speakers.get(seq, "陈默")
        out.append({"line_id": f"{prefix}_{seq:02d}", "seq": seq,
                    "line_revision": 1, "text": text,
                    "text_hash": sha(text), "speaker_label": label})
    return out


def requirement(requirement_id="rq_chen", label="陈默", kind="character", **over):
    item = {"requirement_id": requirement_id, "kind": kind, "label": label,
            "spec_hint": ({"character_id": "chr_chen"}
                          if kind == "character" else {})}
    item.update(over)
    return item


def scene(start, end, *, key=None, heading=None, **over):
    item = {
        "line_span": {"start_line_id": f"lin_{start:02d}",
                      "end_line_id": f"lin_{end:02d}"},
        "heading": heading or {"place": "INT", "location_label": "客厅"},
        **over,
    }
    if key is not None:
        item["scene_id"] = key
    return item


def proposal(scenes, requirements=None, relations=None):
    out = []
    for index, raw in enumerate(scenes, start=1):
        item = dict(raw)
        item.setdefault("scene_id", f"s{index}")
        item.setdefault("display_locator", {"episode": 1, "scene": index})
        item.setdefault("summary", f"第{index}场")
        item.setdefault("characters",
                        [{"requirement_ref": "rq_chen", "speaking": True}])
        out.append(item)
    return {"scenes": out,
            "requirements": [requirement()] if requirements is None
            else requirements,
            "relations": relations or []}


def two_scenes(**over):
    return proposal([scene(1, 5), scene(6, 10)], **over)


def base_body(scenes=None, requirements=None, relations=None, **over):
    payload = {
        "project_id": "prj_1",
        "series_id": "ser_1",
        "episode_id": "ep_1",
        "branch_id": "main",
        "script_ref": {"script_id": "scr_1", "revision": 1},
        "proposal": proposal(scenes or [scene(1, 5), scene(6, 10)],
                             requirements=requirements, relations=relations),
        "actor": dict(HUMAN),
    }
    # `seal=True` 是命令选项，不是顶层字段：内核只读 `body["options"]`，
    # 且未知的顶层键不会被拒 —— 放错位置会静默留下 DRAFT。
    seal = over.pop("seal", None)
    if seal is not None:
        payload["options"] = {"seal": seal}
    payload.update(over)
    return payload


# ------------------------------------------------------------------ 上游 stub

class ScriptSource:
    """S04 `GET /scripts/{id}/revisions/{rev}` + `/lines`。

    `revisions` 的键是 `(tenant_id, script_id, revision)`；值可以是 dict（按
    `over` 覆盖几个键）或一个异常实例。**跨租户**通过「键不存在」表达：
    S04 对不存在与别人家的剧本同码 404。
    """

    def __init__(self, lines=None, revisions=None):
        self.lines = make_lines() if lines is None else lines
        self.revisions = revisions or {}
        self.calls: list[tuple] = []

    @staticmethod
    def key(tenant_id, script_id, revision):
        return (tenant_id, script_id, revision)

    def put(self, tenant_id="t1", script_id="scr_1", revision=1, **over):
        entry = {"state": "SEALED", "lines": self.lines}
        entry.update(over)
        self.revisions[self.key(tenant_id, script_id, revision)] = entry

    def __call__(self, tenant_id, script_id, revision):
        self.calls.append((tenant_id, script_id, revision))
        entry = self.revisions.get(self.key(tenant_id, script_id, revision))
        if entry is None:
            raise ServiceError(404, "SCRIPT_NOT_FOUND", "no such revision")
        if isinstance(entry, Exception):
            raise entry
        lines = entry.get("lines", self.lines)
        return {
            "script_id": script_id,
            "revision": revision,
            "state": entry.get("state", "SEALED"),
            "content_hash": entry.get("content_hash") or sha("script-body"),
            "line_set_hash": entry.get("line_set_hash", lsh(lines)),
            "lines": lines,
        }


class ContextSource:
    """S18 `GET /context-packs/{id}`。返回 `{pack: {...}}`，与 deps 同形。"""

    def __init__(self, packs=None):
        self.packs = {"pack_1": {"pack_id": "pack_1",
                                 "content_hash": sha("pack-1")}} \
            if packs is None else packs
        self.calls: list[tuple] = []

    def __call__(self, tenant_id, pack_id):
        self.calls.append((tenant_id, pack_id))
        value = self.packs.get(pack_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ServiceError(404, "PACK_NOT_FOUND", "no such pack")
        return {"pack": dict(value)}


class TaskSource:
    """S19 `GET /agent-tasks/{id}/result`。"""

    def __init__(self, tasks=None):
        self.tasks = {"task_1": {"proposal": two_scenes()}} \
            if tasks is None else tasks
        self.calls: list[tuple] = []

    def __call__(self, tenant_id, task_id):
        self.calls.append((tenant_id, task_id))
        value = self.tasks.get(task_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ServiceError(404, "TASK_NOT_FOUND", "no such task")
        return dict(value)


# ------------------------------------------------------------------ harness


class Harness:
    def __init__(self, conn=None, *, script=None, context=_UNSET, tasks=_UNSET):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.script = script or ScriptSource()
        self.script.put()                       # 默认：t1 的 scr_1 rev 1
        self.context = ContextSource() if context is _UNSET else context
        self.tasks = TaskSource() if tasks is _UNSET else tasks
        self.idem = SqliteIdempotencyStore(self.conn)
        self.service = BreakdownService(
            self.conn,
            script_source=self.script,
            context_pack_source=self.context,
            agent_task_source=self.tasks,
        )

    # ---- 幂等外壳：与 api/app.py 的 command() 同语义 ----
    def command(self, fn, body, tenant_id="t1", command_id="c1"):
        digest = canonical_request_hash(body)
        record = self.idem.begin(tenant_id, command_id, digest)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = fn(tenant_id, command_id, digest)
        except Exception:
            self.idem.abort(tenant_id, command_id)
            raise
        self.idem.finish(tenant_id, command_id,
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ---- 命令助手 ----
    def create(self, tenant_id="t1", command_id="c1", **over):
        body = base_body(**over)
        return self.command(
            lambda t, c, h: self.service.create_breakdown(
                t, body, command_id=c, request_hash=h),
            body, tenant_id, command_id)

    def created(self, **over):
        """建一版并返回响应；`command_id` 默认 `c1`。"""
        return self.create(**over)

    def append(self, breakdown_id, tenant_id="t1", command_id="c2", **over):
        body = base_body(**over)
        for key in ("project_id", "series_id", "episode_id", "branch_id"):
            body.pop(key)
        return self.command(
            lambda t, c, h: self.service.append_revision(
                t, breakdown_id, body, command_id=c, request_hash=h),
            body, tenant_id, command_id)

    def seal(self, breakdown_id, revision, tenant_id="t1", command_id="c3",
             **over):
        body = {"actor": dict(HUMAN), **over}
        return self.command(
            lambda t, c, h: self.service.seal(
                t, breakdown_id, revision, body, command_id=c,
                request_hash=h),
            body, tenant_id, command_id)

    def retire(self, scene_id, tenant_id="t1", command_id="c4", **over):
        body = {"reason": "removed", **over}
        return self.command(
            lambda t, c, h: self.service.retire_scene(
                t, scene_id, body, command_id=c, request_hash=h),
            body, tenant_id, command_id)

    def reorder(self, breakdown_id, revision, scene_ids, tenant_id="t1",
                command_id="c5"):
        body = {"scene_ids": list(scene_ids)}
        return self.command(
            lambda t, c, h: self.service.reorder(
                t, breakdown_id, revision, body, command_id=c,
                request_hash=h),
            body, tenant_id, command_id)

    def cache_resolutions(self, scene_id, breakdown_id, revision, resolutions,
                          tenant_id="t1", command_id="c6"):
        body = {"breakdown_id": breakdown_id, "revision": revision,
                "resolutions": resolutions}
        return self.command(
            lambda t, c, h: self.service.cache_requirement_resolutions(
                t, scene_id, body, command_id=c, request_hash=h),
            body, tenant_id, command_id)

    # ---- 查询 / 计数 ----
    def rows(self, table, **where):
        clause = " AND ".join(f"{k}=?" for k in where) or "1"
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {clause}",
            tuple(where.values())).fetchall()

    def one(self, table, **where):
        rows = self.rows(table, **where)
        assert len(rows) == 1, (table, where, len(rows))
        return rows[0]

    def count(self, table, **where):
        clause = " AND ".join(f"{k}=?" for k in where) or "1"
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {clause}",
            tuple(where.values())).fetchone()[0]

    def events(self):
        return [json.loads(row["payload"]) for row in self.conn.execute(
            "SELECT payload FROM outbox ORDER BY created_at, sequence")]

    def local_event_types(self):
        return [row["event_type"] for row in self.conn.execute(
            "SELECT event_type FROM local_event ORDER BY created_at")]

    def state_of(self, breakdown_id, revision, tenant_id="t1"):
        return self.one("breakdown_revision", tenant_id=tenant_id,
                        breakdown_id=breakdown_id,
                        revision=revision)["state"]


def raises(status: int, code: str):
    """`pytest.raises` 的窄版：钉住状态码与错误码。

    `CommandConflict` 没有 `status` 属性（幂等 SDK 只带 `code`），这里按 409
    取值 —— 与 api/app.py 的异常处理器一致。
    """
    @contextlib.contextmanager
    def _cm():
        with pytest.raises((ServiceError, CommandConflict,
                            CommandInProgress)) as info:
            yield info
        value = info.value
        assert getattr(value, "status", 409) == status, (
            getattr(value, "status", 409), value.code)
        assert value.code == code, value.code
    return _cm()


def _capture(call, *args):
    """跑一次调用并交回抛出的异常；用来比较两条失败路径是否无差别。"""
    with pytest.raises((ServiceError, CommandConflict, CommandInProgress)) as info:
        call(*args)
    return info.value


def columns(conn, table):
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def table_names(conn):
    return {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


# ================================================================ 纯函数

class TestPureFunctions:
    def test_line_set_digest_is_order_independent(self):
        rows = [("lin_01", 1, sha("a")), ("lin_02", 1, sha("b"))]
        assert line_set_digest(rows) == line_set_digest(list(reversed(rows)))

    def test_line_set_digest_matches_the_documented_algorithm(self):
        rows = [("lin_02", 1, "b" * 64), ("lin_01", 2, "a" * 64)]
        encoded = "\n".join(sorted(["lin_02:1:" + "b" * 64,
                                    "lin_01:2:" + "a" * 64]))
        assert line_set_digest(rows) == hashlib.sha256(
            encoded.encode("utf-8")).hexdigest()

    def test_line_set_digest_changes_when_content_changes(self):
        base = [("lin_01", 1, sha("a"))]
        assert line_set_digest(base) != line_set_digest([("lin_01", 1, sha("b"))])
        assert line_set_digest(base) != line_set_digest([("lin_01", 2, sha("a"))])

    def test_scene_projection_excludes_order_and_display(self):
        scene = {"scene_id": "scn_1", "scene_order": 7,
                 "display_locator": {"episode": 9, "scene": 7},
                 "heading": {}, "line_span": {}, "opening_excerpt": None,
                 "summary": "s", "characters": [], "continuity_hints": [],
                 "advisory": {}}
        projection = scene_content_projection(scene)
        assert "scene_order" not in projection
        assert "display_locator" not in projection
        assert projection["scene_id"] == "scn_1"

    def test_content_hash_moves_with_revision_but_fingerprint_does_not(self):
        scene = {"scene_id": "scn_1", "heading": {"place": "INT"},
                 "line_span": {"start_line_id": "lin_01",
                               "end_line_id": "lin_05", "line_count": 5},
                 "opening_excerpt": None, "summary": "s", "characters": [],
                 "continuity_hints": [], "advisory": {}}
        source = {"script_id": "scr_1", "revision": 1,
                  "content_hash": sha("body"), "line_set_hash": sha("lines")}
        first = breakdown_content_hash(breakdown_id="bkd_1", revision=1,
                                       source_script=source, scenes=[scene],
                                       requirements=[], line_set_hash=sha("x"))
        second = breakdown_content_hash(breakdown_id="bkd_1", revision=2,
                                        source_script=source, scenes=[scene],
                                        requirements=[], line_set_hash=sha("x"))
        assert first != second
        # 指纹没有 revision 参数；同一行集合下稳定。
        assert content_fingerprint(source_script=source, scenes=[scene],
                                   requirements=[],
                                   line_set_hash=sha("x")) == \
            content_fingerprint(source_script=source, scenes=[scene],
                                requirements=[], line_set_hash=sha("x"))
        # 但行集合变了指纹必须变（它参与摘要）。
        assert content_fingerprint(source_script=source, scenes=[scene],
                                   requirements=[],
                                   line_set_hash=sha("x")) != \
            content_fingerprint(source_script=source, scenes=[scene],
                                requirements=[], line_set_hash=sha("y"))

    def test_fingerprint_ignores_scene_identity(self):
        scene = {"scene_id": "scn_1", "heading": {"place": "INT"},
                 "line_span": {"start_line_id": "lin_01",
                               "end_line_id": "lin_05", "line_count": 5},
                 "opening_excerpt": None, "summary": "s", "characters": [],
                 "continuity_hints": [], "advisory": {}}
        other = {**scene, "scene_id": "scn_2"}
        source = {"script_id": "scr_1", "revision": 1,
                  "content_hash": sha("body"), "line_set_hash": sha("lines")}
        assert content_fingerprint(source_script=source, scenes=[scene],
                                   requirements=[], line_set_hash=sha("x")) == \
            content_fingerprint(source_script=source, scenes=[other],
                                requirements=[], line_set_hash=sha("x"))

    def test_fingerprint_ignores_display_order(self):
        scene = {"scene_id": "scn_1", "scene_order": 1,
                 "display_locator": {"episode": 1, "scene": 1},
                 "heading": {"place": "INT"},
                 "line_span": {"start_line_id": "lin_01",
                               "end_line_id": "lin_05", "line_count": 5},
                 "opening_excerpt": None, "summary": "s", "characters": [],
                 "continuity_hints": [], "advisory": {}}
        source = {"script_id": "scr_1", "revision": 1,
                  "content_hash": sha("body"), "line_set_hash": sha("lines")}
        moved = {**scene, "scene_order": 2,
                 "display_locator": {"episode": 1, "scene": 2}}
        assert content_fingerprint(source_script=source, scenes=[scene],
                                   requirements=[], line_set_hash=sha("x")) == \
            content_fingerprint(source_script=source, scenes=[moved],
                                requirements=[], line_set_hash=sha("x"))

    def test_transaction_does_not_nest(self):
        h = Harness()
        with Transaction(h.conn):
            with Transaction(h.conn) as inner:
                assert inner.nested is True
            assert h.conn.in_transaction is True
        assert h.conn.in_transaction is False

    def test_transaction_rolls_back_on_error(self):
        h = Harness()
        with pytest.raises(RuntimeError):
            with Transaction(h.conn):
                h.conn.execute(
                    "INSERT INTO local_event (tenant_id, local_event_id,"
                    " event_type, aggregate_id, payload, created_at)"
                    " VALUES ('t1','lev_x','e','a','{}','now')")
                raise RuntimeError("boom")
        assert h.count("local_event") == 0

    def test_service_error_carries_status_code_retryable_details(self):
        error = ServiceError(409, "X", "m", retryable=True, details={"a": 1})
        assert (error.status, error.code, error.retryable) == (409, "X", True)
        assert error.details == {"a": 1}

    def test_constants_match_the_plan(self):
        assert ID_PREFIX["breakdown"] == "bkd_"
        assert ID_PREFIX["scene"] == "scn_"
        assert ID_PREFIX["requirement"] == "rq_"
        assert SOURCE == "S05"
        assert TOPIC == "drama.breakdown.v2"
        assert SCHEMA_VERSION == "2.0"
        assert AUTHORITATIVE_SOURCE == "s05.breakdown"
        assert DEFAULT_CONTINUITY == "inherit_by_line_anchor"
        assert MAX_SCENES_HARD == 400
        assert ORDER_OFFSET > MAX_SCENES_HARD
        assert RESOLUTION_TTL_SECONDS == 900
        assert EVENT_REVISION_CREATED == "breakdown.revision.created"


# ================================================================ 结构红线
# 这些断言查库。响应体少个键证明不了"没有第二权威"，少一列才能。

class TestSchemaStructure:
    def test_tables_are_only_s05_plus_platform_sdk(self):
        h = Harness()
        expected = {"breakdown", "breakdown_revision", "scene",
                    "scene_revision", "scene_participant", "requirement",
                    "scene_relation", "requirement_resolution_cache",
                    "local_event",
                    # 平台 SDK 建的表，不是本服务的 schema
                    "outbox", "idempotency_record"}
        assert table_names(h.conn) == expected

    @pytest.mark.parametrize("foreign", [
        "shot_projection", "asset_binding", "scene_canon",
        "continuity_state", "inbox", "story_clock", "edl", "manifest",
    ])
    def test_no_foreign_fact_table_exists(self, foreign):
        h = Harness()
        assert foreign not in table_names(h.conn)

    def test_scene_has_no_scene_order_column(self):
        # §1.1：身份与"第几场"分表。序号写进 scene 会让一次插入变成全局重命名。
        h = Harness()
        assert "scene_order" not in columns(h.conn, "scene")
        assert "scene_order" in columns(h.conn, "scene_revision")

    def test_scene_revision_has_no_body_column(self):
        h = Harness()
        scene_columns = columns(h.conn, "scene_revision")
        assert "body" not in scene_columns
        assert "text" not in scene_columns
        # 只有摘录的复制字节 + hash
        assert "opening_text" in scene_columns
        assert "opening_text_hash" in scene_columns

    @pytest.mark.parametrize("column", ["prop_id", "asset_id", "rights_ref",
                                       "asset_version_id", "bound_at"])
    def test_requirement_is_not_a_binding(self, column):
        h = Harness()
        assert column not in columns(h.conn, "requirement")

    @pytest.mark.parametrize("column", ["execution_fence", "node_epoch",
                                       "fence", "epoch"])
    def test_no_fence_or_epoch_columns(self, column):
        # AGENTS.md：execution_fence 归 run/branch，node_epoch 归单个节点；
        # S05 不写任何一种。
        h = Harness()
        for table in table_names(h.conn):
            assert column not in columns(h.conn, table), table

    def test_no_accepted_status_anywhere(self):
        # accepted pointer 归 S08；S05 的状态里不能出现它。
        h = Harness()
        for table in ("breakdown", "breakdown_revision", "scene"):
            ddl = h.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()["sql"]
            assert "accepted" not in ddl.lower()

    def test_breakdown_revision_states_are_exactly_the_plan_set(self):
        assert REVISION_STATES == ("DRAFT", "SEALED", "SUPERSEDED", "WITHDRAWN")
        assert "accepted" not in REVISION_STATES

    def test_terminal_states_have_no_outgoing_transitions(self):
        assert REVISION_TRANSITIONS["SUPERSEDED"] == set()
        assert REVISION_TRANSITIONS["WITHDRAWN"] == set()
        assert REVISION_TRANSITIONS["SEALED"] == {"SUPERSEDED"}
        assert REVISION_TRANSITIONS["DRAFT"] == {"SEALED", "WITHDRAWN"}

    def test_breakdown_has_no_unique_on_episode_and_branch(self):
        # §3.5.1：同一集同一分支允许多版（不同编剧、A/B 版）。
        h = Harness()
        ddl = h.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND"
            " name='breakdown'").fetchone()["sql"]
        assert "UNIQUE (tenant_id, episode_id" not in ddl

    def test_line_count_zero_is_rejected_by_the_database(self):
        h = Harness()
        created = h.created()
        breakdown_id = created["breakdown_id"]
        scene_id = created["scenes"][0]["scene_id"]
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO scene_revision (tenant_id, breakdown_id, revision,"
                " scene_id, scene_order, display_episode, display_scene,"
                " heading_place, heading_location, start_line_id, end_line_id,"
                " line_count, summary) VALUES"
                " ('t1', ?, 1, ?, 9, 1, 9, 'INT', 'x', 'lin_01', 'lin_05', 0,"
                " 's')", (breakdown_id, scene_id))

    def test_excerpt_columns_are_all_or_nothing(self):
        h = Harness()
        created = h.created()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO scene_revision (tenant_id, breakdown_id, revision,"
                " scene_id, scene_order, display_episode, display_scene,"
                " heading_place, heading_location, start_line_id, end_line_id,"
                " line_count, opening_line_id, opening_text, summary)"
                " VALUES ('t1', ?, 1, ?, 9, 1, 9, 'INT', 'x', 'lin_01',"
                " 'lin_05', 5, 'lin_01', NULL, 's')",
                (created["breakdown_id"], created["scenes"][0]["scene_id"]))

    def test_draft_iff_sealed_at_is_null(self):
        h = Harness()
        created = h.created()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE breakdown_revision SET sealed_at='now' WHERE"
                " tenant_id='t1' AND breakdown_id=? AND revision=1",
                (created["breakdown_id"],))
        h.seal(created["breakdown_id"], 1)
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE breakdown_revision SET sealed_at=NULL WHERE"
                " tenant_id='t1' AND breakdown_id=? AND revision=1",
                (created["breakdown_id"],))

    def test_retired_at_and_retire_reason_move_together(self):
        h = Harness()
        created = h.created()
        scene_id = created["scenes"][0]["scene_id"]
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE scene SET retired_at='now' WHERE scene_id=?",
                (scene_id,))
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE scene SET retire_reason='removed' WHERE scene_id=?",
                (scene_id,))

    def test_merged_reason_requires_a_target(self):
        h = Harness()
        created = h.created()
        scene_id = created["scenes"][0]["scene_id"]
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE scene SET retired_at='now', retire_reason='merged_into'"
                " WHERE scene_id=?", (scene_id,))

    def test_content_hashes_are_bare_hex_of_length_64(self):
        h = Harness()
        created = h.created(seal=True)
        row = h.one("breakdown_revision", tenant_id="t1",
                    breakdown_id=created["breakdown_id"], revision=1)
        assert len(row["content_hash"]) == 64
        assert len(row["content_fingerprint"]) == 64
        assert ":" not in row["content_hash"]
        assert created["content_hash"].startswith("sha256:")

    def test_duplicate_content_fingerprint_is_rejected_by_unique_index(self):
        h = Harness()
        created = h.created()
        row = h.one("breakdown_revision", tenant_id="t1",
                    breakdown_id=created["breakdown_id"], revision=1)
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO breakdown_revision (tenant_id, breakdown_id,"
                " revision, state, episode_id, branch_id, script_id,"
                " script_revision, script_content_hash, script_line_set_hash,"
                " context_coverage, produced_by, scene_id_continuity,"
                " scene_count, requirement_count, content_hash,"
                " content_fingerprint, created_at) VALUES"
                " ('t1', ?, 2, 'DRAFT', 'ep_1', 'main', 'scr_1', 1, ?, ?,"
                " 'partial', '{}', 'fresh', 2, 1, ?, ?, 'now')",
                (created["breakdown_id"], row["script_content_hash"],
                 row["script_line_set_hash"], sha("other-content"),
                 row["content_fingerprint"]))


# ================================================================ 9.1 幂等与并发 [E12]

class TestIdempotency:
    def test_t05_01_same_command_same_body_replays(self):
        h = Harness()
        first = h.created(command_id="c1")
        second = h.created(command_id="c1")
        assert first["replayed"] is False
        assert second["replayed"] is True
        assert second["breakdown_id"] == first["breakdown_id"]
        assert h.count("breakdown") == 1
        assert h.count("breakdown_revision") == 1
        assert h.count("outbox") == 0

    def test_t05_01_replay_is_byte_identical(self):
        h = Harness()
        first = h.created(command_id="c1", seal=True)
        replay = h.created(command_id="c1", seal=True)
        replay.pop("replayed")
        first.pop("replayed")
        assert replay == first
        assert h.count("outbox") == 1

    def test_t05_02_same_command_different_body_is_409(self):
        h = Harness()
        h.created(command_id="c1")
        with raises(409, "IDEMPOTENCY_KEY_REUSED"):
            h.created(command_id="c1", episode_id="ep_2")
        assert h.count("breakdown") == 1

    def test_t05_02_conflict_writes_no_rows(self):
        h = Harness()
        h.created(command_id="c1")
        before = (h.count("breakdown"), h.count("breakdown_revision"),
                  h.count("scene"), h.count("requirement"))
        with raises(409, "IDEMPOTENCY_KEY_REUSED"):
            h.created(command_id="c1", branch_id="alt")
        after = (h.count("breakdown"), h.count("breakdown_revision"),
                 h.count("scene"), h.count("requirement"))
        assert before == after

    def test_t05_02_same_key_different_tenant_is_not_a_conflict(self):
        # 幂等键的作用域是 tenant_id + command_id（AGENTS.md）。
        h = Harness()
        h.script.put(tenant_id="t2")
        h.created(tenant_id="t1", command_id="c1")
        second = h.created(tenant_id="t2", command_id="c1")
        assert second["replayed"] is False
        assert h.count("breakdown", tenant_id="t1") == 1
        assert h.count("breakdown", tenant_id="t2") == 1

    def test_t05_03_idempotency_record_expired_returns_existing_revision(self):
        h = Harness()
        created = h.created()
        first = h.append(created["breakdown_id"], command_id="a1",
                         scenes=[scene(1, 3), scene(4, 10)])
        assert first["revision"] == 2
        h.conn.execute("DELETE FROM idempotency_record WHERE command_id='a1'")
        # 30 天后同 body、新 command_id：内容指纹命中既有版本，不新建。
        again = h.append(created["breakdown_id"], command_id="a2",
                         scenes=[scene(1, 3), scene(4, 10)])
        assert again["deduplicated"] is True
        assert again["revision"] == 2
        assert h.count("breakdown_revision") == 2
        assert h.one("breakdown", breakdown_id=created["breakdown_id"])[
            "head_revision"] == 2

    def test_t05_04_concurrent_append_with_stale_parent_is_409(self):
        h = Harness()
        created = h.created()
        h.append(created["breakdown_id"], command_id="a1",
                 scenes=[scene(1, 3), scene(4, 10)])          # head -> 2
        with raises(409, "BREAKDOWN_REVISION_CONFLICT"):
            h.append(created["breakdown_id"], command_id="a2",
                     parent_revision=1,
                     scenes=[scene(1, 3), scene(4, 10)])
        assert h.count("breakdown_revision") == 2

    def test_t05_04_rejected_append_does_not_advance_head(self):
        h = Harness()
        created = h.created()
        with raises(409, "BREAKDOWN_REVISION_CONFLICT"):
            h.append(created["breakdown_id"], command_id="a1",
                     parent_revision=9)
        assert h.one("breakdown", breakdown_id=created["breakdown_id"])[
            "head_revision"] == 1

    def test_t05_05_second_seal_is_409(self):
        h = Harness()
        created = h.created()
        h.seal(created["breakdown_id"], 1, command_id="s1")
        with raises(409, "REVISION_NOT_DRAFT"):
            h.seal(created["breakdown_id"], 1, command_id="s2")
        assert h.count("outbox") == 1

    def test_t05_05_seal_of_unknown_revision_is_404(self):
        h = Harness()
        created = h.created()
        with raises(404, "NOT_FOUND"):
            h.seal(created["breakdown_id"], 7, command_id="s1")

    def test_t05_06_same_content_twice_is_deduplicated(self):
        h = Harness()
        created = h.created()
        again = h.append(created["breakdown_id"], command_id="a1")
        assert again["deduplicated"] is True
        assert again["revision"] == 1
        assert h.count("breakdown_revision") == 1

    def test_t05_06_dedup_does_not_leave_orphan_scene_rows(self):
        h = Harness()
        created = h.created()
        before = h.count("scene")
        h.append(created["breakdown_id"], command_id="a1")
        assert h.count("scene") == before

    def test_t05_06_dedup_still_writes_a_result(self):
        h = Harness()
        created = h.created()
        h.append(created["breakdown_id"], command_id="a1")
        replay = h.append(created["breakdown_id"], command_id="a1")
        assert replay["replayed"] is True
        assert replay["deduplicated"] is True

    def test_in_flight_command_is_409(self):
        h = Harness()
        body = base_body()
        digest = canonical_request_hash(body)
        h.idem.begin("t1", "c1", digest)     # 占位但不 finish
        with raises(409, "COMMAND_IN_PROGRESS"):
            h.command(lambda t, c, hr: h.service.create_breakdown(
                t, body, command_id=c, request_hash=hr), body, "t1", "c1")
        assert h.count("breakdown") == 0


# ================================================================ 9.2 分场语义

class TestValidation:
    def test_t05_07_missing_line_is_line_coverage_gap(self):
        h = Harness(script=ScriptSource(make_lines(100)))
        with raises(422, "LINE_COVERAGE_GAP") as info:
            h.created(scenes=[scene(1, 56), scene(58, 100)])
        assert "lin_57" in info.value.details["missing_line_ids"]
        assert info.value.details["missing_seqs"] == [57]
        assert info.value.details["missing_count"] == 1

    def test_t05_07_gap_writes_nothing(self):
        h = Harness(script=ScriptSource(make_lines(100)))
        with raises(422, "LINE_COVERAGE_GAP"):
            h.created(scenes=[scene(1, 56), scene(58, 100)])
        for table in ("breakdown", "breakdown_revision", "scene",
                      "scene_revision", "requirement", "outbox"):
            assert h.count(table) == 0, table

    def test_t05_07_denominator_is_the_actual_seq_set(self):
        # seq 不从 1 开始也必须能分场（C7 只要求唯一单调）。分母若是 `1..N`，
        # 这里会一口气报 1..100 全漏 —— 断言只漏 110 才能证明用的是实际集合。
        lines = [dict(item, seq=item["seq"] + 100)
                 for item in make_lines(10)]
        h = Harness(script=ScriptSource(lines))
        with raises(422, "LINE_COVERAGE_GAP") as info:
            h.created(scenes=[scene(1, 5), scene(6, 9)])
        assert info.value.details["missing_seqs"] == [110]
        assert info.value.details["missing_line_ids"] == ["lin_10"]
        assert info.value.details["missing_count"] == 1

    def test_t05_07_seq_offset_lines_are_covered_by_seq(self):
        lines = [dict(item, seq=item["seq"] + 100) for item in make_lines(10)]
        h = Harness(script=ScriptSource(lines))
        created = h.created(scenes=[scene(1, 5), scene(6, 10)])
        assert created["scene_count"] == 2

    def test_t05_08_overlapping_line_is_line_coverage_overlap(self):
        h = Harness()
        with raises(422, "LINE_COVERAGE_OVERLAP") as info:
            h.created(scenes=[scene(1, 6), scene(6, 10)])
        assert info.value.details["duplicate_seqs"] == [6]
        assert "lin_06" in info.value.details["duplicate_line_ids"]

    def test_t05_08_overlap_is_checked_before_order(self):
        # 两场都是 1-5：先撞重叠，不该先报顺序。
        h = Harness()
        with raises(422, "LINE_COVERAGE_OVERLAP"):
            h.created(scenes=[scene(1, 5), scene(1, 5)])

    def test_t05_09_start_after_end_is_line_order_violation(self):
        h = Harness()
        with raises(422, "LINE_ORDER_VIOLATION") as info:
            h.created(scenes=[scene(1, 5), scene(10, 6)])
        assert info.value.details["start_seq"] == 10
        assert info.value.details["end_seq"] == 6

    def test_t05_09_span_to_unknown_line_is_line_order_violation(self):
        h = Harness()
        with raises(422, "LINE_ORDER_VIOLATION"):
            h.created(scenes=[scene(1, 5), scene(6, 99)])

    def test_t05_10_submitted_out_of_order_is_line_order_violation(self):
        h = Harness(script=ScriptSource(make_lines(30)))
        with raises(422, "LINE_ORDER_VIOLATION") as info:
            h.created(scenes=[scene(1, 10), scene(21, 30), scene(11, 20)])
        assert info.value.details["scene_position"] == 3
        assert info.value.details["previous_end"] == 30

    def test_t05_11_speaker_not_in_scene(self):
        speakers = {7: "陈守义"}
        h = Harness(script=ScriptSource(make_lines(10, speakers=speakers)))
        with raises(422, "SPEAKER_NOT_IN_SCENE") as info:
            h.created(scenes=[scene(1, 5), scene(6, 10)])
        assert info.value.details["line_id"] == "lin_07"
        assert info.value.details["speaker_label"] == "陈守义"

    def test_t05_11_speaker_matches_by_character_id(self):
        lines = make_lines(10)
        lines[6]["speaker_label"] = None
        lines[6]["speaker_id"] = "chr_chen"
        h = Harness(script=ScriptSource(lines))
        created = h.created(scenes=[scene(1, 5), scene(6, 10)])
        assert created["scene_count"] == 2

    def test_t05_11_unknown_speaker_on_a_silent_line_is_fine(self):
        lines = make_lines(10)
        lines[6]["speaker_label"] = None
        lines[6]["speaker_id"] = None
        h = Harness(script=ScriptSource(lines))
        assert h.created(scenes=[scene(1, 5), scene(6, 10)])["scene_count"] == 2

    def test_t05_12_pure_action_scene_is_accepted(self):
        # 没有一句台词，场里仍可以有角色需求（§1.4 只约束有台词的发言人）。
        lines = make_lines(5)
        for item in lines:
            item["speaker_label"] = None
            item["speaker_id"] = None
        h = Harness(script=ScriptSource(lines))
        created = h.created(proposal=proposal(
            [{"line_span": {"start_line_id": "lin_01",
                            "end_line_id": "lin_05"},
              "heading": {"place": "INT", "location_label": "客厅"},
              "display_locator": {"episode": 1, "scene": 1},
              "summary": "纯动作",
              "characters": [{"requirement_ref": "rq_a"},
                             {"requirement_ref": "rq_b"}]}],
            requirements=[requirement("rq_a", label="林岚"),
                          requirement("rq_b", label="陈默")]))
        assert created["scene_count"] == 1
        assert h.count("scene_participant") == 2

    def test_t05_13_excerpt_altered_is_script_line_drift(self):
        h = Harness()
        changed = scene(1, 5)
        changed["opening_excerpt"] = {"line_id": "lin_01",
                                      "text": "台词1。"}
        with raises(422, "SCRIPT_LINE_DRIFT") as info:
            h.created(scenes=[changed, scene(6, 10)])
        assert info.value.details["line_id"] == "lin_01"

    def test_t05_13_exact_copy_is_accepted(self):
        h = Harness()
        copied = scene(1, 5)
        copied["opening_excerpt"] = {"line_id": "lin_01", "text": "台词1"}
        created = h.created(scenes=[copied, scene(6, 10)])
        assert created["scenes"][0]["opening_excerpt"]["text"] == "台词1"

    def test_t05_13_excerpt_hash_only_mode_compares_hash(self):
        # S05-G20：S04 不给正文时只能比 hash，不假装验过。
        lines = make_lines(10)
        for item in lines:
            item.pop("text")
        h = Harness(script=ScriptSource(lines))
        bad = scene(1, 5)
        bad["opening_excerpt"] = {"line_id": "lin_01", "text": "改了",
                                  "text_hash": sha("别的")}
        with raises(422, "SCRIPT_LINE_DRIFT"):
            h.created(scenes=[bad, scene(6, 10)])

    def test_t05_13_excerpt_for_a_missing_line_is_drift(self):
        h = Harness()
        bad = scene(1, 5)
        bad["opening_excerpt"] = {"line_id": "lin_99", "text": "台词99"}
        with raises(422, "SCRIPT_LINE_DRIFT"):
            h.created(scenes=[bad, scene(6, 10)])

    def test_t05_14_zero_line_count_is_422_and_a_db_check(self):
        h = Harness()
        broken = scene(1, 5)
        broken["line_span"]["line_count"] = 0
        with raises(422, "INVALID_LINE_SPAN") as info:
            h.created(scenes=[broken, scene(6, 10)])
        assert info.value.details["rule"] == "line_count"
        assert h.count("scene_revision") == 0

    def test_t05_14_inconsistent_line_count_is_refused(self):
        h = Harness()
        broken = scene(1, 5)
        broken["line_span"]["line_count"] = 4
        with raises(422, "INVALID_LINE_SPAN") as info:
            h.created(scenes=[broken, scene(6, 10)])
        assert (info.value.details["declared"],
                info.value.details["actual"]) == (4, 5)

    def test_display_locator_must_match_position(self):
        h = Harness()
        bad = scene(1, 5)
        bad["display_locator"] = {"episode": 1, "scene": 2}
        with raises(422, "DISPLAY_LOCATOR_INVALID"):
            h.created(scenes=[bad, scene(6, 10)])

    def test_display_episode_must_be_shared(self):
        h = Harness()
        bad = scene(6, 10)
        bad["display_locator"] = {"episode": 2, "scene": 2}
        with raises(422, "DISPLAY_LOCATOR_INVALID") as info:
            h.created(scenes=[scene(1, 5), bad])
        assert info.value.details["display_episodes"] == [1, 2]

    def test_unknown_seq_inside_a_span_is_not_coverage(self):
        # 场的区间跨过 S04 缺的行：那不是覆盖，是幻觉。
        lines = [item for item in make_lines(10) if item["seq"] != 3]
        h = Harness(script=ScriptSource(lines))
        with raises(422, "LINE_ORDER_VIOLATION") as info:
            h.created(scenes=[scene(1, 5), scene(6, 10)])
        assert 3 in info.value.details["unknown_seqs"]

    def test_requirement_ref_must_resolve(self):
        h = Harness()
        with raises(422, "REQUIREMENT_REF_DANGLING"):
            h.created(scenes=[scene(1, 5), scene(6, 10)],
                      requirements=[])

    def test_requirement_kind_must_be_character_for_participants(self):
        h = Harness()
        with raises(422, "REQUIREMENT_KIND_MISMATCH") as info:
            h.created(scenes=[scene(1, 5), scene(6, 10)],
                      requirements=[requirement(kind="prop", label="剑")])
        assert info.value.details["kind"] == "prop"

    def test_duplicate_requirement_id_is_refused(self):
        h = Harness()
        with raises(422, "REQUIREMENT_REF_DANGLING"):
            h.created(scenes=[scene(1, 5), scene(6, 10)],
                      requirements=[requirement(), requirement()])

    def test_relation_endpoint_must_resolve(self):
        h = Harness()
        with raises(422, "REQUIREMENT_REF_DANGLING"):
            h.created(scenes=[scene(1, 5), scene(6, 10)],
                      relations=[{"from_scene_id": "s1",
                                  "to_scene_id": "nope",
                                  "relation": "adjacent"}])

    def test_relation_to_itself_is_refused(self):
        h = Harness()
        with raises(422, "STRUCTURE_INVALID"):
            h.created(scenes=[scene(1, 5), scene(6, 10)],
                      relations=[{"from_scene_id": "s1",
                                  "to_scene_id": "s1",
                                  "relation": "adjacent"}])

    def test_valid_relation_is_persisted_with_minted_ids(self):
        h = Harness()
        created = h.created(
            scenes=[scene(1, 5), scene(6, 10)],
            relations=[{"from_scene_id": "s1", "to_scene_id": "s2",
                        "relation": "adjacent", "note": "直接接"}])
        relation = created["relations"][0]
        assert relation["from_scene_id"] == created["scenes"][0]["scene_id"]
        assert relation["to_scene_id"] == created["scenes"][1]["scene_id"]
        assert h.count("scene_relation") == 1

    def test_scene_limit_exceeded(self):
        h = Harness(script=ScriptSource(make_lines(10)))
        with raises(422, "SCENE_LIMIT_EXCEEDED") as info:
            h.created(scenes=[scene(1, 5), scene(6, 10)],
                      options={"max_scenes": 1})
        assert info.value.details["scene_count"] == 2

    def test_max_scenes_above_the_hard_cap_is_400(self):
        h = Harness()
        with raises(400, "INVALID_SCHEMA"):
            h.created(options={"max_scenes": MAX_SCENES_HARD + 1})

    def test_unknown_option_is_400(self):
        h = Harness()
        with raises(400, "INVALID_SCHEMA"):
            h.created(options={"auto_repair": True})

    def test_no_proposal_at_all_is_structure_invalid(self):
        h = Harness()
        body = base_body()
        body.pop("proposal")
        with raises(422, "STRUCTURE_INVALID") as info:
            h.command(lambda t, c, hr: h.service.create_breakdown(
                t, body, command_id=c, request_hash=hr), body, "t1", "c1")
        assert info.value.details["gap"] == "S05-G1"

    def test_scene_with_unknown_key_is_structure_invalid(self):
        h = Harness()
        bad = scene(1, 5)
        bad["shot_count"] = 4
        with raises(422, "STRUCTURE_INVALID"):
            h.created(scenes=[bad, scene(6, 10)])

    def test_scene_without_heading_is_structure_invalid(self):
        h = Harness()
        bad = scene(1, 5)
        bad.pop("heading")
        with raises(422, "STRUCTURE_INVALID"):
            h.created(scenes=[bad, scene(6, 10)])

    def test_heading_place_is_enumerated(self):
        h = Harness()
        bad = scene(1, 5, heading={"place": "SOMEWHERE",
                                   "location_label": "客厅"})
        with raises(422, "STRUCTURE_INVALID"):
            h.created(scenes=[bad, scene(6, 10)])


# ================================================================ 9.3 身份与序号

class TestIdentity:
    def test_t05_15_unchanged_first_line_inherits_scene_id(self):
        # 1-8 的首行仍是 lin_01 → 继承第一场；9-10 的首行 lin_09 仍落在旧
        # 第二场 6-10 的区间里 → 也继承。§3.3 只看 anchor，不看改动大小。
        h = Harness()
        created = h.created()
        first_id = created["scenes"][0]["scene_id"]
        second_id = created["scenes"][1]["scene_id"]
        revised = h.append(created["breakdown_id"], command_id="a1",
                           scenes=[scene(1, 8), scene(9, 10)])
        assert revised["revision"] == 2
        assert revised["scenes"][0]["scene_id"] == first_id
        assert revised["scenes"][1]["scene_id"] == second_id
        assert revised["retired_scene_ids"] == []

    def test_t05_15_inherited_id_has_one_scene_row(self):
        h = Harness()
        created = h.created()
        first_id = created["scenes"][0]["scene_id"]
        h.append(created["breakdown_id"], command_id="a1",
                 scenes=[scene(1, 8), scene(9, 10)])
        assert h.count("scene", scene_id=first_id) == 1
        assert h.count("scene_revision", scene_id=first_id) == 2

    def test_t05_15_all_scene_ids_are_minted_prefixes(self):
        h = Harness()
        created = h.created()
        for item in created["scenes"]:
            assert item["scene_id"].startswith("scn_")

    def test_t05_15_agent_supplied_scene_id_is_discarded(self):
        # T05-24：输入里的 scene_id 只当作 proposal 内引用键，不当身份。
        h = Harness()
        created = h.created(
            scenes=[scene(1, 5, key="scn_attacker"), scene(6, 10, key="s2")])
        assert created["scenes"][0]["scene_id"] != "scn_attacker"
        assert created["scenes"][1]["scene_id"] != "s2"
        assert h.count("scene", scene_id="scn_attacker") == 0

    def test_t05_16_merge_retires_the_absorbed_scene(self):
        h = Harness()
        created = h.created()
        survivor, absorbed = [item["scene_id"] for item in created["scenes"]]
        revised = h.append(
            created["breakdown_id"], command_id="a1",
            scenes=[scene(1, 10)],
            retire_map={absorbed: {"reason": "merged_into",
                                   "merged_into_scene_id": survivor}})
        assert revised["scenes"][0]["scene_id"] == survivor
        assert revised["retired_scene_ids"] == [absorbed]
        row = h.one("scene", scene_id=absorbed)
        assert row["retire_reason"] == "merged_into"
        assert row["merged_into_scene_id"] == survivor
        assert row["retired_at"] is not None

    def test_t05_16_retired_scene_is_not_deleted(self):
        h = Harness()
        created = h.created()
        absorbed = created["scenes"][1]["scene_id"]
        h.append(created["breakdown_id"], command_id="a1",
                 scenes=[scene(1, 10)],
                 retire_map={absorbed: {"reason": "removed"}})
        # §3.6 第 5 条：历史 revision 仍要能解析到它。
        assert h.count("scene", scene_id=absorbed) == 1
        assert h.count("scene_revision", scene_id=absorbed) == 1

    def test_t05_17_split_mints_two_new_ids(self):
        h = Harness()
        created = h.created()
        old_first, old_second = [item["scene_id"] for item in created["scenes"]]
        revised = h.append(
            created["breakdown_id"], command_id="a1",
            scenes=[scene(1, 3), scene(4, 10)],
            retire_map={old_first: {"reason": "split"}})
        new_ids = [item["scene_id"] for item in revised["scenes"]]
        assert old_first not in new_ids
        assert len(set(new_ids)) == 2
        assert old_first in revised["retired_scene_ids"]
        assert h.one("scene", scene_id=old_first)["retire_reason"] == "split"

    def test_t05_17_scene_not_inherited_by_anchor_is_retired(self):
        # 新第二场锚在 lin_04，落在被退休的旧第一场里；旧第二场（6-10）没有
        # 任何新场锚在它的首行上 → 不被继承，自动退休（§3.3 的锚点规则）。
        h = Harness()
        created = h.created()
        old_first, old_second = [item["scene_id"] for item in created["scenes"]]
        revised = h.append(
            created["breakdown_id"], command_id="a1",
            scenes=[scene(1, 3), scene(4, 10)],
            retire_map={old_first: {"reason": "split"}})
        new_ids = [item["scene_id"] for item in revised["scenes"]]
        assert old_second not in new_ids
        assert old_second in revised["retired_scene_ids"]
        assert h.one("scene", scene_id=old_second)["retired_at"] is not None

    def test_t05_18_fresh_continuity_mints_everything(self):
        h = Harness()
        created = h.created()
        old_ids = [item["scene_id"] for item in created["scenes"]]
        revised = h.append(created["breakdown_id"], command_id="a1",
                           scene_id_continuity="fresh")
        assert not set(old_ids) & {item["scene_id"]
                                   for item in revised["scenes"]}
        assert set(revised["retired_scene_ids"]) == set(old_ids)
        for scene_id in old_ids:
            assert h.one("scene", scene_id=scene_id)["retired_at"] is not None

    def test_t05_18_explicit_continuity_uses_the_map(self):
        h = Harness()
        created = h.created()
        first_id = created["scenes"][0]["scene_id"]
        revised = h.append(
            created["breakdown_id"], command_id="a1",
            scene_id_continuity="inherit_explicit",
            scene_id_map={"lin_06": first_id})
        assert revised["scenes"][1]["scene_id"] == first_id
        assert revised["scenes"][0]["scene_id"] != first_id

    def test_t05_18_explicit_map_to_foreign_scene_is_refused(self):
        h = Harness()
        created = h.created()
        with raises(422, "SCENE_ID_CONFLICT"):
            h.append(created["breakdown_id"], command_id="a1",
                     scene_id_continuity="inherit_explicit",
                     scene_id_map={"lin_06": "scn_not_mine"})

    def test_t05_18_explicit_map_onto_two_scenes_is_refused(self):
        h = Harness()
        created = h.created()
        first_id = created["scenes"][0]["scene_id"]
        with raises(422, "SCENE_ID_CONFLICT"):
            h.append(created["breakdown_id"], command_id="a1",
                     scene_id_continuity="inherit_explicit",
                     scene_id_map={"lin_01": first_id, "lin_06": first_id})

    def test_t05_18_unknown_continuity_is_400(self):
        h = Harness()
        created = h.created()
        with raises(400, "INVALID_SCHEMA"):
            h.append(created["breakdown_id"], command_id="a1",
                     scene_id_continuity="reuse_everything")

    def test_t05_19_reorder_changes_order_not_identity_or_hash(self):
        h = Harness()
        created = h.created()
        ids = [item["scene_id"] for item in created["scenes"]]
        result = h.reorder(created["breakdown_id"], 1, list(reversed(ids)))
        assert [item["scene_id"] for item in result["scenes"]] == \
            list(reversed(ids))
        assert [item["scene_order"] for item in result["scenes"]] == [1, 2]
        assert [item["display_locator"]["scene"]
                for item in result["scenes"]] == [1, 2]
        assert result["content_hash"] == created["content_hash"]

    def test_t05_19_reorder_does_not_touch_the_scene_table(self):
        h = Harness()
        created = h.created()
        before = [dict(row) for row in h.rows("scene")]
        h.reorder(created["breakdown_id"], 1,
                  list(reversed([item["scene_id"]
                                 for item in created["scenes"]])))
        assert [dict(row) for row in h.rows("scene")] == before

    def test_t05_19_reorder_is_visible_in_the_projection(self):
        h = Harness()
        created = h.created(seal=True)
        ids = [item["scene_id"] for item in created["scenes"]]
        h.reorder(created["breakdown_id"], 1, list(reversed(ids)))
        listed = h.service.list_episode_scenes("t1", "ep_1")
        assert [item["scene_id"] for item in listed["scenes"]] == \
            list(reversed(ids))

    def test_t05_19_reorder_of_sealed_revision_is_allowed(self):
        # S05-G11：§3.4 的存在意义就是不改内容也能调顺序。
        h = Harness()
        created = h.created(seal=True)
        ids = [item["scene_id"] for item in created["scenes"]]
        assert h.reorder(created["breakdown_id"], 1,
                         ids)["reordered"] is True

    def test_t05_20_partial_order_list_is_400(self):
        h = Harness()
        created = h.created()
        first = created["scenes"][0]["scene_id"]
        with raises(400, "INVALID_SCENE_ORDER") as info:
            h.reorder(created["breakdown_id"], 1, [first])
        assert info.value.details["expected_count"] == 2
        assert info.value.details["received_count"] == 1
        assert len(info.value.details["missing"]) == 1

    def test_t05_20_duplicate_id_in_order_list_is_400(self):
        h = Harness()
        created = h.created()
        first = created["scenes"][0]["scene_id"]
        with raises(400, "INVALID_SCENE_ORDER"):
            h.reorder(created["breakdown_id"], 1, [first, first])

    def test_t05_20_unknown_id_in_order_list_is_400(self):
        h = Harness()
        created = h.created()
        ids = [item["scene_id"] for item in created["scenes"]]
        with raises(400, "INVALID_SCENE_ORDER"):
            h.reorder(created["breakdown_id"], 1, [ids[0], "scn_ghost"])

    def test_t05_20_non_list_payload_is_400(self):
        h = Harness()
        created = h.created()
        body = {"scene_ids": "scn_1"}
        with raises(400, "INVALID_SCENE_ORDER"):
            h.command(lambda t, c, hr: h.service.reorder(
                t, created["breakdown_id"], 1, body, command_id=c,
                request_hash=hr), body, "t1", "ro")

    def test_t05_20_reorder_of_unknown_revision_is_404(self):
        h = Harness()
        created = h.created()
        with raises(404, "NOT_FOUND"):
            h.reorder(created["breakdown_id"], 9, ["scn_x"])

    def test_t05_20_reorder_of_superseded_revision_is_409(self):
        h = Harness()
        created = h.created(seal=True)
        ids = [item["scene_id"] for item in created["scenes"]]
        second = h.append(created["breakdown_id"], command_id="a1",
                          scenes=[scene(1, 3), scene(4, 10)])
        h.seal(created["breakdown_id"], second["revision"], command_id="s2")
        assert h.state_of(created["breakdown_id"], 1) == "SUPERSEDED"
        with raises(409, "REVISION_NOT_ACTIVE"):
            h.reorder(created["breakdown_id"], 1, ids)

    def test_t05_21_no_two_scenes_share_a_scene_id(self):
        # 重叠区间会先被 LINE_COVERAGE_OVERLAP 拦住，根本走不到铸造；
        # 所以这里断言两件事：重叠被拒，且被拒后没有半截的场行留下。
        h = Harness()
        created = h.created()
        ids = [item["scene_id"] for item in created["scenes"]]
        assert len(ids) == len(set(ids))
        before = h.count("scene")
        with raises(422, "LINE_COVERAGE_OVERLAP"):
            h.append(created["breakdown_id"], command_id="a1",
                     scenes=[scene(1, 3), scene(1, 10)])
        assert h.count("scene") == before
        assert h.count("scene") == len(set(
            row["scene_id"] for row in h.rows("scene")))

    def test_t05_21_two_scenes_matching_one_old_scene_mint_a_new_id(self):
        # S05-G8：§3.3 的算法保证"一个旧 ID 最多被一个新场继承"。
        h = Harness()
        created = h.created(scenes=[scene(1, 10)])
        old_id = created["scenes"][0]["scene_id"]
        revised = h.append(created["breakdown_id"], command_id="a1",
                           scenes=[scene(1, 5), scene(6, 10)])
        ids = [item["scene_id"] for item in revised["scenes"]]
        assert ids[0] == old_id
        assert ids[1] != old_id
        assert len(set(ids)) == 2

    def test_t05_22_explicit_reuse_of_a_retired_scene_is_refused(self):
        h = Harness()
        created = h.created()
        retired_id = created["scenes"][1]["scene_id"]
        h.append(created["breakdown_id"], command_id="a1",
                 scenes=[scene(1, 10)])
        with raises(422, "SCENE_RETIRED") as info:
            h.append(created["breakdown_id"], command_id="a2",
                     scene_id_continuity="inherit_explicit",
                     scene_id_map={"lin_06": retired_id})
        assert info.value.details["scene_id"] == retired_id

    def test_t05_22_automatic_inheritance_skips_a_retired_scene(self):
        h = Harness()
        created = h.created()
        retired_id = created["scenes"][1]["scene_id"]
        h.append(created["breakdown_id"], command_id="a1",
                 scenes=[scene(1, 10)])
        revised = h.append(created["breakdown_id"], command_id="a2",
                           scenes=[scene(1, 4), scene(5, 10)])
        ids = [item["scene_id"] for item in revised["scenes"]]
        assert retired_id not in ids
        # 被跳过之后必须真的铸造了一个新 ID，而不是"少了一个场"。
        assert len(ids) == 2 and len(set(ids)) == 2
        assert h.one("scene", scene_id=retired_id)["retired_at"] is not None

    def test_t05_22_retired_by_command_outside_any_revision_is_not_resurrected(
            self):
        # 退休不一定来自上一版的 `_retire_set`：`POST /scenes/{id}:retire`
        # 直接改身份表，父 revision 的行里看不出 `retired_at`。继承规则若只
        # 读父行就会把这场复活。
        h = Harness()
        created = h.created()
        retired_id = created["scenes"][1]["scene_id"]
        h.retire(retired_id, command_id="r1", reason="split")
        revised = h.append(created["breakdown_id"], command_id="a1",
                           scenes=[scene(1, 4), scene(5, 10)])
        ids = [item["scene_id"] for item in revised["scenes"]]
        assert retired_id not in ids
        # 它仍未继承，所以本版照样把它记进 `retired_scene_ids`（幂等：已经
        # 退休的行不会被重写，理由也不会被缺省的 'removed' 覆盖）。
        assert revised["retired_scene_ids"] == [retired_id]
        row = h.one("scene", scene_id=retired_id)
        assert row["retired_at"] is not None
        assert row["retire_reason"] == "split"   # 不被缺省的 'removed' 覆盖

    def test_retire_scene_is_idempotent(self):
        h = Harness()
        created = h.created()
        first = h.retire(created["scenes"][0]["scene_id"], command_id="r1")
        second = h.retire(created["scenes"][0]["scene_id"], command_id="r2")
        assert first["changed"] is True
        assert second["changed"] is False
        assert second["retired"] is True
        assert h.count("local_event") == 1

    def test_retire_unknown_scene_is_404(self):
        h = Harness()
        with raises(404, "NOT_FOUND"):
            h.retire("scn_nope", command_id="r1")

    def test_retire_merged_requires_a_same_episode_target(self):
        h = Harness()
        created = h.created()
        with raises(422, "SCENE_ID_CONFLICT"):
            h.retire(created["scenes"][0]["scene_id"], command_id="r1",
                     reason="merged_into", merged_into_scene_id="scn_ghost")

    def test_retire_reason_is_enumerated(self):
        h = Harness()
        created = h.created()
        with raises(400, "INVALID_SCHEMA"):
            h.retire(created["scenes"][0]["scene_id"], command_id="r1",
                     reason="because")

    def test_retire_map_pointing_at_an_inherited_scene_is_refused(self):
        # 自动继承会跳过 retire_map 点名的场（"拆分"要靠它），所以必须用
        # 显式映射才造得出"既继承又退休"这个矛盾。
        h = Harness()
        created = h.created()
        first_id = created["scenes"][0]["scene_id"]
        with raises(422, "SCENE_ID_CONFLICT"):
            h.append(created["breakdown_id"], command_id="a1",
                     scenes=[scene(1, 6), scene(7, 10)],
                     scene_id_continuity="inherit_explicit",
                     scene_id_map={"lin_01": first_id},
                     retire_map={first_id: {"reason": "removed"}})

    def test_first_scene_id_is_translated_to_a_minted_id(self):
        h = Harness()
        created = h.created(
            scenes=[scene(1, 5), scene(6, 10)],
            requirements=[requirement(first_scene_id="s2")])
        assert created["requirements"][0]["first_scene_id"] == \
            created["scenes"][1]["scene_id"]

    def test_first_scene_id_defaults_to_the_first_referencing_scene(self):
        h = Harness()
        created = h.created()
        assert created["requirements"][0]["first_scene_id"] == \
            created["scenes"][0]["scene_id"]


# ================================================================ 9.4 信任边界 [E28]

class TestTrustBoundary:
    def test_t05_23_agent_supplied_tenant_id_is_refused(self):
        h = Harness()
        bad = scene(1, 5)
        bad["tenant_id"] = "t2"
        with raises(422, "AGENT_SUPPLIED_TENANT") as info:
            h.created(scenes=[bad, scene(6, 10)])
        assert info.value.details["gap"] == "S05-G21"
        assert info.value.details["field"] == "tenant_id"
        assert info.value.details["index"] == 0
        assert h.count("breakdown") == 0

    def test_t05_23_agent_supplied_project_or_series_is_refused(self):
        h = Harness()
        for field in ("project_id", "series_id"):
            bad = scene(1, 5)
            bad[field] = "elsewhere"
            with raises(422, "AGENT_SUPPLIED_TENANT") as info:
                h.created(scenes=[bad, scene(6, 10)], command_id=f"k-{field}")
            # 告警要指向真正撞上的那个字段。
            assert info.value.details["field"] == field

    def test_t05_24_agent_supplied_breakdown_id_is_not_trusted(self):
        h = Harness()
        first = h.created(command_id="c1")
        scope = scene(1, 5)
        scope["breakdown_id"] = first["breakdown_id"]
        created = h.created(command_id="c2", scenes=[scope, scene(6, 10)])
        assert created["breakdown_id"] != first["breakdown_id"]
        assert h.count("breakdown") == 2

    def test_t05_24_agent_supplied_revision_is_not_trusted(self):
        h = Harness()
        scope = scene(1, 5)
        scope["revision"] = 9
        created = h.created(scenes=[scope, scene(6, 10)])
        assert created["revision"] == 1
        assert h.count("breakdown_revision") == 1

    def test_t05_25_cross_tenant_read_is_404_with_no_identifiers(self):
        # E28 的"无差别"：别人的 ID 与本租户不存在的 ID 走同一条分支，
        # `details` 都为空，消息模板也一致（只有入参那串 ID 不同）。
        h = Harness()
        created = h.created(tenant_id="t1")
        calls = (
            lambda tenant, ident: h.service.get_breakdown(tenant, ident),
            lambda tenant, ident: h.service.get_revision(tenant, ident, 1),
            lambda tenant, ident: h.service.get_scene(tenant, ident),
            lambda tenant, ident: h.service.get_scene_requires(tenant, ident),
        )
        for call in calls:
            foreign = _capture(call, "t2", created["breakdown_id"])
            ghost = _capture(call, "t2", "bkd_ghost")
            assert foreign.code == ghost.code == "NOT_FOUND"
            assert foreign.details == ghost.details == {}
            assert str(foreign).replace(created["breakdown_id"],
                                        "bkd_ghost") == str(ghost)

    def test_t05_25_cross_tenant_projection_is_empty(self):
        h = Harness()
        h.created(tenant_id="t1")
        assert h.service.list_episode_scenes("t2", "ep_1")["scenes"] == []

    def test_t05_25_cross_tenant_append_is_404(self):
        h = Harness()
        created = h.created(tenant_id="t1")
        with raises(404, "NOT_FOUND"):
            h.append(created["breakdown_id"], tenant_id="t2", command_id="a1")
        assert h.count("breakdown_revision") == 1

    def test_t05_25_cross_tenant_retire_is_404(self):
        h = Harness()
        created = h.created(tenant_id="t1")
        with raises(404, "NOT_FOUND"):
            h.retire(created["scenes"][0]["scene_id"], tenant_id="t2",
                     command_id="r1")
        assert h.one("scene", scene_id=created["scenes"][0]["scene_id"])[
            "retired_at"] is None

    def test_t05_26_foreign_script_is_404_not_passed_through(self):
        # S04 对跨租户与不存在同码；S05 不透传上游的判定。
        h = Harness()
        h.script.revisions.clear()              # 只有 t2 能读到 scr_1
        h.script.put(tenant_id="t2")
        with raises(404, "SCRIPT_NOT_FOUND") as info:
            h.created(tenant_id="t1")
        assert info.value.details["revision"] == 1

    def test_t05_26_script_body_is_never_echoed_on_404(self):
        h = Harness()
        h.script.revisions.clear()
        h.script.put(tenant_id="t2")
        with raises(404, "SCRIPT_NOT_FOUND") as info:
            h.created(tenant_id="t1")
        assert "lines" not in info.value.details

    def test_actor_missing_is_recorded_as_unspecified(self):
        # S05-G22：不编一个看起来像真人的 ID。
        h = Harness()
        body = base_body()
        body.pop("actor")
        created = h.command(lambda t, c, hr: h.service.create_breakdown(
            t, body, command_id=c, request_hash=hr), body, "t1", "c1")
        creator = h.one("breakdown", breakdown_id=created["breakdown_id"])[
            "created_by"]
        assert json.loads(creator) == {"principal_id": "unspecified",
                                       "principal_type": "unknown"}

    def test_actor_type_is_enumerated(self):
        h = Harness()
        body = base_body(actor={"principal_id": "x", "principal_type": "robot"})
        with raises(400, "INVALID_SCHEMA"):
            h.command(lambda t, c, hr: h.service.create_breakdown(
                t, body, command_id=c, request_hash=hr), body, "t1", "c1")


# ================================================================ 9.5 事件 [E13][E32]

class TestEvents:
    def test_t05_27_crash_before_outbox_rolls_back_everything(self):
        h = Harness()
        created = h.created()

        def explode(envelope):
            raise RuntimeError("crashed before the outbox write")
        h.service.outbox.append = explode
        with pytest.raises(RuntimeError):
            h.seal(created["breakdown_id"], 1, command_id="s1")
        assert h.count("outbox") == 0
        assert h.count("local_event") == 0
        assert h.state_of(created["breakdown_id"], 1) == "DRAFT"
        assert h.one("breakdown", breakdown_id=created["breakdown_id"])[
            "sealed_revision"] is None

    def test_t05_28_republish_before_marking_is_safe_for_consumers(self):
        h = Harness()
        created = h.created(seal=True)
        event = h.events()[0]
        first = h.service.outbox.unpublished()
        second = h.service.outbox.unpublished()   # 崩溃在 published_at 之前
        assert first == second
        assert len(first) == 1
        inbox = Inbox(h.conn)
        assert inbox.consume("S08", event["id"]) is True
        assert inbox.consume("S08", event["id"]) is False
        h.service.outbox.mark_published(event["id"])
        assert h.service.outbox.unpublished() == []

    def test_t05_29_draft_emits_nothing(self):
        h = Harness()
        created = h.created()
        assert created["state"] == "DRAFT"
        assert h.count("outbox") == 0
        assert h.count("local_event") == 0

    def test_t05_29_append_of_a_draft_emits_nothing(self):
        h = Harness()
        created = h.created()
        h.append(created["breakdown_id"], command_id="a1",
                 scenes=[scene(1, 3), scene(4, 10)])
        assert h.count("outbox") == 0

    def test_t05_30_sequence_is_the_revision_and_may_skip(self):
        h = Harness()
        created = h.created()
        break_id = created["breakdown_id"]
        h.append(break_id, command_id="a2", scenes=[scene(1, 3), scene(4, 10)])
        h.append(break_id, command_id="a3", scenes=[scene(1, 4), scene(5, 10)])
        h.append(break_id, command_id="a4", scenes=[scene(1, 6), scene(7, 10)])
        h.seal(break_id, 4, command_id="s4")
        events = h.events()
        assert len(events) == 1
        assert events[0]["sequence"] == 4
        assert events[0]["data"]["revision"] == 4
        assert h.state_of(break_id, 3) == "DRAFT"

    def test_t05_30_supersede_does_not_emit_a_second_event(self):
        h = Harness()
        created = h.created(seal=True)
        break_id = created["breakdown_id"]
        second = h.append(break_id, command_id="a2",
                          scenes=[scene(1, 3), scene(4, 10)])
        h.seal(break_id, second["revision"], command_id="s2")
        # 两版 SEALED 各一条（§6.3）；supersede 自己不发事件。
        assert [item["data"]["revision"] for item in h.events()] == [1, 2]
        assert h.state_of(break_id, 1) == "SUPERSEDED"
        assert LOCAL_EVENT_SUPERSEDED in h.local_event_types()

    def test_t05_31_event_payload_matches_the_schema(self):
        schema = json.loads(
            (ROOT / "contracts" /
             "breakdown.revision-created.v2.schema.json").read_text(
                 encoding="utf-8"))
        h = Harness()
        h.created(seal=True)
        data = h.events()[0]["data"]
        jsonschema.validate(data, schema)
        assert set(data) == {
            "schema_version", "breakdown_id", "revision", "state",
            "episode_id", "branch_id", "source_script", "scene_ids",
            "retired_scene_ids", "requirement_count", "content_hash",
            "occurred_at"}

    def test_t05_31_envelope_is_v2(self):
        h = Harness()
        h.created(seal=True)
        event = h.events()[0]
        assert event["specversion"] == "1.0"
        assert event["schema_version"] == "2.0"
        assert event["source"] == SOURCE
        assert event["type"] == EVENT_REVISION_CREATED
        assert event["subject"] == "ep_1"
        assert event["tenant_id"] == "t1"
        assert event["aggregate_id"].startswith("bkd_")
        assert event["correlation_id"] == "c1"
        assert event["data"]["state"] == "SEALED"

    def test_t05_31_event_hashes_are_bare_hex(self):
        h = Harness()
        h.created(seal=True)
        data = h.events()[0]["data"]
        assert len(data["content_hash"]) == 64
        assert ":" not in data["content_hash"]
        assert len(data["source_script"]["content_hash"]) == 64
        assert "line_set_hash" not in data["source_script"]   # BRK-7

    def test_t05_31_event_carries_the_minted_scene_ids_in_order(self):
        h = Harness()
        created = h.created(seal=True)
        assert h.events()[0]["data"]["scene_ids"] == [
            item["scene_id"] for item in created["scenes"]]

    def test_t05_31_event_reports_retired_ids(self):
        h = Harness()
        created = h.created(seal=True)
        old = created["scenes"][1]["scene_id"]
        second = h.append(created["breakdown_id"], command_id="a1",
                          scenes=[scene(1, 10)])
        h.seal(created["breakdown_id"], second["revision"], command_id="s2")
        assert h.events()[-1]["data"]["retired_scene_ids"] == [old]

    def test_t05_32_data_ref_can_be_audited_but_is_not_public(self):
        h = Harness()
        created = h.created(seal=True)
        ref = h.events()[0]["data_ref"]
        assert ref["service_id"] == SOURCE
        assert ref["id"] == created["breakdown_id"]
        assert ref["revision"] == 1
        # 无权限调用方读不到 data_ref 指向的分场。
        with raises(404, "NOT_FOUND"):
            h.service.get_revision("t2", ref["id"], ref["revision"])

    def test_local_events_are_not_outbox_events(self):
        h = Harness()
        created = h.created()
        h.reorder(created["breakdown_id"], 1,
                  list(reversed([item["scene_id"]
                                 for item in created["scenes"]])))
        assert LOCAL_EVENT_REORDERED in h.local_event_types()
        assert h.count("outbox") == 0

    def test_local_event_type_set_is_declared(self):
        assert set(LOCAL_EVENT_TYPES) == {
            LOCAL_EVENT_SEALED, LOCAL_EVENT_SUPERSEDED,
            LOCAL_EVENT_RETIRED, LOCAL_EVENT_REORDERED}
        assert EVENT_REVISION_CREATED not in LOCAL_EVENT_TYPES

    def test_seal_writes_one_local_event_with_the_command_id(self):
        h = Harness()
        created = h.created()
        h.seal(created["breakdown_id"], 1, command_id="s1")
        rows = [row for row in h.conn.execute(
            "SELECT event_type, payload FROM local_event")]
        assert [row["event_type"] for row in rows] == [LOCAL_EVENT_SEALED]
        assert json.loads(rows[0]["payload"])["command_id"] == "s1"


# ================================================================ 9.6 依赖降级

class TestDegradation:
    def test_t05_33_s18_unavailable_is_partial_not_failure(self):
        h = Harness()
        h.context = ContextSource({"pack_1": ConnectionError("down")})
        h.service._context_source = h.context
        created = h.created(context_pack_ref={"pack_id": "pack_1"})
        assert created["context"]["coverage"] == "partial"

    def test_t05_33_s18_absent_is_partial(self):
        h = Harness(context=None)
        h.service._context_source = None
        created = h.created(context_pack_ref={"pack_id": "pack_1"})
        assert created["context"]["coverage"] == "partial"
        assert created["context"]["context_pack_ref"]["pack_id"] == "pack_1"

    def test_t05_33_partial_coverage_is_recorded_on_the_row(self):
        h = Harness(context=None)
        h.service._context_source = None
        created = h.created(context_pack_ref={"pack_id": "pack_1"})
        row = h.one("breakdown_revision",
                    breakdown_id=created["breakdown_id"], revision=1)
        assert row["context_coverage"] == "partial"

    def test_t05_33_full_coverage_requires_a_pack(self):
        h = Harness()
        created = h.created(context_pack_ref={
            "pack_id": "pack_1", "content_hash": "sha256:" + sha("pack-1")})
        assert created["context"]["coverage"] == "full"
        row = h.one("breakdown_revision",
                    breakdown_id=created["breakdown_id"], revision=1)
        assert row["context_pack_id"] == "pack_1"

    def test_t05_33_moved_context_pack_is_422(self):
        h = Harness()
        with raises(422, "CONTEXT_PACK_EXPIRED"):
            h.created(context_pack_ref={
                "pack_id": "pack_1", "content_hash": "sha256:" + sha("other")})

    def test_t05_33_stale_context_pack_is_422(self):
        h = Harness(context=ContextSource({"pack_1": {
            "pack_id": "pack_1", "content_hash": sha("pack-1"),
            "stale": True}}))
        with raises(422, "CONTEXT_PACK_EXPIRED"):
            h.created(context_pack_ref={"pack_id": "pack_1"})

    def test_t05_34_s19_unavailable_with_inline_proposal_still_succeeds(self):
        h = Harness(tasks=None)
        h.service._agent_source = None
        created = h.created(produced_by={"kind": "human"})
        assert created["produced_by"]["kind"] == "human"
        assert h.count("breakdown_revision") == 1

    def test_t05_34_proposal_ref_defaults_to_agent(self):
        h = Harness()
        body = base_body()
        body.pop("proposal")
        body["proposal_ref"] = {"agent_task_id": "task_1"}
        created = h.command(lambda t, c, hr: h.service.create_breakdown(
            t, body, command_id=c, request_hash=hr), body, "t1", "c1")
        assert created["produced_by"]["kind"] == "agent"
        assert created["produced_by"]["agent_task_id"] == "task_1"

    def test_t05_34_missing_proposal_ref_source_is_503(self):
        h = Harness(tasks=None)
        h.service._agent_source = None
        body = base_body()
        body.pop("proposal")
        body["proposal_ref"] = {"agent_task_id": "task_1"}
        with raises(503, "AGENT_TASK_SOURCE_UNAVAILABLE"):
            h.command(lambda t, c, hr: h.service.create_breakdown(
                t, body, command_id=c, request_hash=hr), body, "t1", "c1")

    def test_t05_34_missing_task_result_is_422(self):
        h = Harness()
        body = base_body()
        body.pop("proposal")
        body["proposal_ref"] = {"agent_task_id": "task_missing"}
        with raises(422, "STRUCTURE_INVALID"):
            h.command(lambda t, c, hr: h.service.create_breakdown(
                t, body, command_id=c, request_hash=hr), body, "t1", "c1")

    def test_t05_34_task_without_a_proposal_is_422(self):
        h = Harness(tasks=TaskSource({"task_1": {"status": "succeeded"}}))
        body = base_body()
        body.pop("proposal")
        body["proposal_ref"] = {"agent_task_id": "task_1"}
        with raises(422, "STRUCTURE_INVALID"):
            h.command(lambda t, c, hr: h.service.create_breakdown(
                t, body, command_id=c, request_hash=hr), body, "t1", "c1")

    def test_t05_35_s04_down_is_503_not_a_local_copy(self):
        h = Harness(script=None)
        h.service._script_source = None
        with raises(503, "SCRIPT_SOURCE_UNAVAILABLE"):
            h.created()
        assert h.count("breakdown") == 0

    def test_t05_35_s04_connection_error_is_503_retryable(self):
        h = Harness(script=ScriptSource())
        h.script.revisions[("t1", "scr_1", 1)] = ConnectionError("down")
        with raises(503, "SCRIPT_SOURCE_UNAVAILABLE") as info:
            h.created()
        assert info.value.retryable is True

    def test_t05_35_unsealed_script_is_422(self):
        h = Harness(script=ScriptSource())
        h.script.put(state="DRAFT")
        with raises(422, "SCRIPT_NOT_SEALED") as info:
            h.created()
        assert info.value.details["state"] == "DRAFT"

    def test_t05_35_empty_line_set_is_422(self):
        h = Harness(script=ScriptSource([]))
        h.script.put(lines=[])
        with raises(422, "SCRIPT_NOT_SEALED"):
            h.created()

    def test_t05_35_script_without_seq_is_503(self):
        lines = make_lines(10)
        lines[3].pop("seq")
        h = Harness(script=ScriptSource(lines))
        with raises(503, "SCRIPT_SOURCE_UNAVAILABLE") as info:
            h.created()
        assert info.value.details["gap"] == "C7"

    def test_t05_35_duplicate_seq_is_503(self):
        lines = make_lines(10)
        lines[3]["seq"] = 1
        h = Harness(script=ScriptSource(lines))
        with raises(503, "SCRIPT_SOURCE_UNAVAILABLE"):
            h.created()

    def test_t05_35_upstream_line_set_hash_must_reproduce(self):
        # S05-G25：不照抄上游自报的 hash。
        h = Harness(script=ScriptSource())
        h.script.put(line_set_hash=sha("not-the-lines"))
        with raises(503, "SCRIPT_SOURCE_UNAVAILABLE") as info:
            h.created()
        assert info.value.details["gap"] == "C7"

    def test_t05_36_script_revision_moved_is_409_retryable(self):
        h = Harness()
        h.script.put(content_hash=sha("new-body"))
        with raises(409, "SCRIPT_REVISION_MOVED") as info:
            h.created(script_ref={"script_id": "scr_1", "revision": 1,
                                  "content_hash": "sha256:" + sha("old-body")})
        assert info.value.retryable is True
        assert info.value.details["latest"]["content_hash"] == \
            "sha256:" + sha("new-body")

    def test_t05_36_line_set_moved_is_also_409(self):
        h = Harness()
        h.script.put(line_set_hash=sha("different-lines"))
        with raises(409, "SCRIPT_REVISION_MOVED"):
            h.created(script_ref={
                "script_id": "scr_1", "revision": 1,
                "line_set_hash": "sha256:" + sha("original-lines")})

    def test_t05_36_matching_hashes_are_accepted(self):
        h = Harness()
        h.script.put(content_hash=sha("body-1"))
        created = h.created(script_ref={
            "script_id": "scr_1", "revision": 1,
            "content_hash": "sha256:" + sha("body-1"),
            "line_set_hash": "sha256:" + lsh(make_lines())})
        assert created["source_script"]["content_hash"] == \
            "sha256:" + sha("body-1")


# ================================================================ 9.7 独立升级

class TestUpgrade:
    def test_t05_37_two_instances_share_the_schema_without_pk_leaks(
            self, tmp_path):
        path = str(tmp_path / "s05.db")
        first = Harness(conn=sqlite3.connect(
            path, check_same_thread=False, isolation_level=None))
        second = Harness(conn=sqlite3.connect(
            path, check_same_thread=False, isolation_level=None))
        created = first.created()
        appended = second.append(created["breakdown_id"], command_id="a1",
                                 scenes=[scene(1, 3), scene(4, 10)])
        assert appended["revision"] == 2
        assert second.count("breakdown_revision") == 2
        # 旧实例按同一份内容重放：命中指纹，不新建也不撞主键。
        dedup = first.append(created["breakdown_id"], command_id="a2",
                             scenes=[scene(1, 3), scene(4, 10)])
        assert dedup["deduplicated"] is True
        assert dedup["revision"] == 2

    def test_t05_37_stale_parent_from_the_other_instance_is_409(
            self, tmp_path):
        path = str(tmp_path / "s05.db")
        first = Harness(conn=sqlite3.connect(
            path, check_same_thread=False, isolation_level=None))
        second = Harness(conn=sqlite3.connect(
            path, check_same_thread=False, isolation_level=None))
        created = first.created()
        second.append(created["breakdown_id"], command_id="a1",
                      scenes=[scene(1, 3), scene(4, 10)])
        with raises(409, "BREAKDOWN_REVISION_CONFLICT"):
            first.append(created["breakdown_id"], command_id="a2",
                         parent_revision=1, scenes=[scene(1, 4), scene(5, 10)])
        assert first.count("breakdown_revision") == 2

    def test_t05_38_failed_command_does_not_unseal(self):
        h = Harness()
        created = h.created(seal=True)
        break_id = created["breakdown_id"]
        with raises(409, "SCRIPT_REVISION_MOVED"):
            h.append(break_id, command_id="a1",
                     script_ref={"script_id": "scr_1", "revision": 1,
                                 "content_hash": "sha256:" + sha("wrong")})
        assert h.state_of(break_id, 1) == "SEALED"
        assert h.one("breakdown", breakdown_id=break_id)[
            "sealed_revision"] == 1

    def test_t05_38_sealed_revision_is_readable_after_a_later_failure(self):
        h = Harness()
        created = h.created(seal=True)
        with raises(422, "LINE_COVERAGE_GAP"):
            h.append(created["breakdown_id"], command_id="a1",
                     scenes=[scene(1, 4), scene(6, 10)])
        document = h.service.get_revision("t1", created["breakdown_id"], 1)
        assert document["state"] == "SEALED"
        assert document["scene_count"] == 2

    def test_t05_39_the_service_uses_exactly_one_connection(self):
        h = Harness()
        assert h.service.conn is h.conn
        assert h.service.outbox.conn is h.conn
        assert h.service.idem.conn is h.conn

    def test_t05_39_no_cross_service_table_is_touched(self):
        h = Harness()
        h.created(seal=True)
        owned = {"breakdown", "breakdown_revision", "scene", "scene_revision",
                 "scene_participant", "requirement", "scene_relation",
                 "requirement_resolution_cache", "local_event",
                 "outbox", "idempotency_record"}
        assert table_names(h.conn) <= owned

    def test_t05_39_deps_are_plain_closures_not_imported_modules(self):
        # deps 只返回可调用对象；没有把另一个服务的类/ORM 带进来。
        for builder in (build_script_source, build_context_pack_source,
                        build_agent_task_source):
            closure = builder("http://127.0.0.1:1")
            assert callable(closure)
            assert not hasattr(closure, "conn")
            assert "requests" not in type(closure).__module__

    def test_build_upstream_sources_fail_closed(self):
        # 只测"没配置"：`"   "` 是 truthy，会真的发 HTTP（连 127.0.0.1:1），
        # 那是另一个用例该管的事，不是 fail-closed。
        for value in (None, ""):
            with raises(503, "SCRIPT_SOURCE_UNAVAILABLE"):
                build_script_source(value)("t1", "scr_1", 1)
        for value in (None, ""):
            with raises(503, "CONTEXT_PACK_UNAVAILABLE"):
                build_context_pack_source(value)("t1", "pack_1")
            with raises(503, "AGENT_TASK_SOURCE_UNAVAILABLE"):
                build_agent_task_source(value)("t1", "task_1")


# ================================================================ 查询

class TestQueries:
    def test_get_breakdown_reports_every_revision(self):
        h = Harness()
        created = h.created()
        h.append(created["breakdown_id"], command_id="a1",
                 scenes=[scene(1, 3), scene(4, 10)])
        document = h.service.get_breakdown("t1", created["breakdown_id"])
        assert [item["revision"] for item in document["revisions"]] == [1, 2]
        assert document["head_revision"] == 2
        assert document["sealed_revision"] is None
        assert document["lifecycle"] == "active"

    def test_every_projection_names_its_authority(self):
        h = Harness()
        created = h.created(seal=True)
        scene_id = created["scenes"][0]["scene_id"]
        documents = [
            h.service.get_breakdown("t1", created["breakdown_id"]),
            h.service.get_revision("t1", created["breakdown_id"], 1),
            h.service.list_episode_scenes("t1", "ep_1"),
            h.service.get_scene("t1", scene_id),
            h.service.get_scene_requires("t1", scene_id),
        ]
        for document in documents:
            assert document["authoritative_source"] == AUTHORITATIVE_SOURCE
            assert document["projection_watermark"]
            assert document["contract_gaps"] == CONTRACT_GAPS

    def test_get_breakdown_unknown_is_404(self):
        h = Harness()
        with raises(404, "NOT_FOUND"):
            h.service.get_breakdown("t1", "bkd_ghost")

    def test_get_revision_unknown_is_404(self):
        h = Harness()
        created = h.created()
        with raises(404, "NOT_FOUND"):
            h.service.get_revision("t1", created["breakdown_id"], 12)

    def test_revision_document_renders_prefixed_hashes(self):
        h = Harness()
        created = h.created()
        document = h.service.get_revision("t1", created["breakdown_id"], 1)
        assert document["content_hash"].startswith("sha256:")
        assert document["source_script"]["content_hash"].startswith("sha256:")
        assert document["source_script"]["line_set_hash"].startswith("sha256:")

    def test_list_episode_scenes_prefers_the_sealed_revision(self):
        h = Harness()
        created = h.created(seal=True)
        second = h.append(created["breakdown_id"], command_id="a1",
                          scenes=[scene(1, 3), scene(4, 10)])
        listed = h.service.list_episode_scenes("t1", "ep_1")
        assert {item["revision"] for item in listed["scenes"]} == {1}
        assert len(listed["scenes"]) == 2
        assert second["revision"] == 2          # head 前进了，投影没动

    def test_list_episode_scenes_falls_back_to_head_before_any_seal(self):
        h = Harness()
        h.created()
        listed = h.service.list_episode_scenes("t1", "ep_1")
        assert [item["revision"] for item in listed["scenes"]] == [1, 1]

    def test_list_episode_scenes_filters_by_branch(self):
        h = Harness()
        h.created()
        assert h.service.list_episode_scenes(
            "t1", "ep_1", branch_id="alt")["scenes"] == []
        assert len(h.service.list_episode_scenes(
            "t1", "ep_1", branch_id="main")["scenes"]) == 2

    def test_list_episode_scenes_follows_the_newest_seal(self):
        # 投影 = 最新一版 sealed。被合并掉的那场只存在于旧 revision 里，
        # 因此不出现在投影中；存活那场 `retired` 仍是 False。
        h = Harness()
        created = h.created(seal=True)
        absorbed = created["scenes"][1]["scene_id"]
        second = h.append(created["breakdown_id"], command_id="a1",
                          scenes=[scene(1, 10)],
                          retire_map={absorbed: {"reason": "merged_into",
                                                 "merged_into_scene_id":
                                                 created["scenes"][0]["scene_id"]}})
        h.seal(created["breakdown_id"], second["revision"], command_id="s2")
        listed = h.service.list_episode_scenes("t1", "ep_1")
        assert [item["scene_id"] for item in listed["scenes"]] == \
            [created["scenes"][0]["scene_id"]]
        assert listed["scenes"][0]["revision"] == 2
        assert listed["scenes"][0]["retired"] is False
        assert h.one("scene", scene_id=absorbed)["retired_at"] is not None

    def test_get_scene_lists_appearances_newest_first(self):
        h = Harness()
        created = h.created()
        scene_id = created["scenes"][0]["scene_id"]
        h.append(created["breakdown_id"], command_id="a1",
                 scenes=[scene(1, 8), scene(9, 10)])
        document = h.service.get_scene("t1", scene_id)
        assert [item["revision"] for item in document["revisions"]] == [2, 1]
        assert document["retired"] is False

    def test_get_scene_survives_the_scene_being_retired(self):
        h = Harness()
        created = h.created()
        absorbed = created["scenes"][1]["scene_id"]
        h.append(created["breakdown_id"], command_id="a1",
                 scenes=[scene(1, 10)],
                 retire_map={absorbed: {"reason": "removed"}})
        document = h.service.get_scene("t1", absorbed)
        assert document["retired"] is True
        assert document["retire_reason"] == "removed"
        assert [item["revision"] for item in document["revisions"]] == [1]

    def test_get_scene_unknown_is_404(self):
        h = Harness()
        with raises(404, "NOT_FOUND"):
            h.service.get_scene("t1", "scn_ghost")

    def test_get_scene_requires_returns_requirements_not_bindings(self):
        h = Harness()
        created = h.created(seal=True)
        scene_id = created["scenes"][0]["scene_id"]
        document = h.service.get_scene_requires("t1", scene_id)
        assert document["requirement_count"] == 1
        item = document["requirements"][0]
        assert set(item) == {"requirement_id", "kind", "label", "spec_hint",
                             "notes"}
        assert "asset_id" not in item
        assert document["resolution_cache_authoritative"] is False

    def test_get_scene_requires_pins_a_revision(self):
        h = Harness()
        created = h.created()
        first_id = created["scenes"][0]["scene_id"]
        h.append(created["breakdown_id"], command_id="a1",
                 scenes=[scene(1, 8), scene(9, 10)])
        assert h.service.get_scene_requires(
            "t1", first_id, revision=1)["revision"] == 1
        with raises(404, "NOT_FOUND"):
            h.service.get_scene_requires("t1", first_id, revision=9)

    def test_cache_stores_resolutions_without_touching_the_requirement(self):
        h = Harness()
        created = h.created(seal=True)
        scene_id = created["scenes"][0]["scene_id"]
        before = [dict(row) for row in h.rows("requirement")]
        out = h.cache_resolutions(
            scene_id, created["breakdown_id"], 1,
            [{"requirement_id": "rq_chen", "resolved": {"asset_id": "a_1"}}])
        assert out["cached"] == 1
        assert out["authoritative"] is False
        assert out["replayed"] is False
        assert [dict(row) for row in h.rows("requirement")] == before
        assert h.count("outbox") == 1          # 只有 seal 的那条

    def test_cache_is_read_back_but_never_authoritative(self):
        h = Harness()
        created = h.created(seal=True)
        scene_id = created["scenes"][0]["scene_id"]
        h.cache_resolutions(
            scene_id, created["breakdown_id"], 1,
            [{"requirement_id": "rq_chen", "resolved": {"asset_id": "a_1"}}])
        document = h.service.get_scene_requires("t1", scene_id)
        assert document["resolution_cache"]["rq_chen"]["resolved"] == \
            {"asset_id": "a_1"}
        assert document["resolution_cache_authoritative"] is False

    def test_cache_ttl_is_fifteen_minutes(self):
        h = Harness()
        created = h.created(seal=True)
        scene_id = created["scenes"][0]["scene_id"]
        out = h.cache_resolutions(
            scene_id, created["breakdown_id"], 1,
            [{"requirement_id": "rq_chen", "resolved": {}}])
        row = h.one("requirement_resolution_cache",
                    breakdown_id=created["breakdown_id"], revision=1,
                    requirement_id="rq_chen")
        assert out["valid_until"] == row["valid_until"]
        assert RESOLUTION_TTL_SECONDS == 900

    def test_cache_rejects_a_requirement_the_scene_does_not_need(self):
        h = Harness()
        created = h.created(seal=True)
        with raises(422, "REQUIREMENT_REF_DANGLING"):
            h.cache_resolutions(
                created["scenes"][0]["scene_id"], created["breakdown_id"], 1,
                [{"requirement_id": "rq_ghost", "resolved": {}}])
        assert h.count("requirement_resolution_cache") == 0

    def test_cache_requires_a_non_empty_resolution_list(self):
        h = Harness()
        created = h.created(seal=True)
        with raises(400, "INVALID_SCHEMA"):
            h.cache_resolutions(created["scenes"][0]["scene_id"],
                                created["breakdown_id"], 1, [])

    def test_cache_on_unknown_scene_is_404(self):
        h = Harness()
        created = h.created(seal=True)
        with raises(404, "NOT_FOUND"):
            h.cache_resolutions("scn_ghost", created["breakdown_id"], 1,
                                [{"requirement_id": "rq_chen"}])


# ================================================================ HTTP 层 [E13]

def _http_client(monkeypatch, tmp_path, **over):
    monkeypatch.setenv("S05_DB", str(tmp_path / "s05.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s05_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s05_api_app"] = module
    harness = Harness(conn=module._conn, **over)
    module.service = harness.service
    module._idem = harness.idem
    return TestClient(module.app), harness


def _headers(command_id="c1", tenant="t1", **extra):
    out = {"Idempotency-Key": command_id, "X-Tenant-Id": tenant,
           "X-Principal-Id": "u_human", "X-Principal-Type": "human"}
    out.update(extra)
    return out


class TestHttpLayer:
    def test_roundtrip_create_append_seal_read(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/breakdowns", headers=_headers("h1"),
                              json=base_body())
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["replayed"] is False
        assert body["state"] == "DRAFT"
        breakdown_id = body["breakdown_id"]
        assert breakdown_id.startswith("bkd_")

        appended = client.post(f"/breakdowns/{breakdown_id}/revisions",
                               headers=_headers("h2"),
                               json={"script_ref": {"script_id": "scr_1",
                                                    "revision": 1},
                                     "proposal": proposal([scene(1, 3),
                                                           scene(4, 10)])})
        assert appended.status_code == 201, appended.text
        assert appended.json()["revision"] == 2

        sealed = client.post(
            f"/breakdowns/{breakdown_id}/revisions/2:seal",
            headers=_headers("h3"), json={})
        assert sealed.status_code == 200, sealed.text
        assert sealed.json()["state"] == "SEALED"

        got = client.get(f"/breakdowns/{breakdown_id}/revisions/2",
                         headers={"X-Tenant-Id": "t1"})
        assert got.status_code == 200, got.text
        assert got.json()["content_hash"] == sealed.json()["content_hash"]
        assert client.get("/health/readiness").json()["ready"] is True

    def test_projection_routes_are_reachable(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/breakdowns", headers=_headers("h1"),
                              json=base_body(seal=True)).json()
        scene_id = created["scenes"][0]["scene_id"]
        for path in (f"/breakdowns/{created['breakdown_id']}",
                     f"/breakdowns/{created['breakdown_id']}/revisions/1",
                     "/episodes/ep_1/scenes",
                     f"/scenes/{scene_id}",
                     f"/scenes/{scene_id}/requires"):
            response = client.get(path, headers={"X-Tenant-Id": "t1"})
            assert response.status_code == 200, (path, response.text)
            assert response.json()["authoritative_source"] == \
                AUTHORITATIVE_SOURCE

    def test_missing_tenant_header_is_400(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        headers = {"Idempotency-Key": "h1"}
        response = client.post("/breakdowns", headers=headers,
                               json=base_body())
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_BODY"
        assert h.count("breakdown") == 0

    def test_missing_idempotency_key_is_400(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        response = client.post("/breakdowns", headers={"X-Tenant-Id": "t1"},
                               json=base_body())
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_BODY"
        assert h.count("breakdown") == 0

    def test_get_without_tenant_header_is_400(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/breakdowns", headers=_headers("h1"),
                              json=base_body()).json()
        response = client.get(f"/breakdowns/{created['breakdown_id']}")
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_BODY"

    def test_body_tenant_mismatch_is_403_and_writes_nothing(
            self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = base_body(tenant_id="t2")
        response = client.post("/breakdowns", headers=_headers("h1"),
                               json=body)
        assert response.status_code == 403
        assert response.json()["code"] == "TENANT_MISMATCH"
        assert h.count("breakdown") == 0

    def test_body_command_id_must_match_the_header(self, monkeypatch,
                                                   tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = base_body(command_id="other")
        response = client.post("/breakdowns", headers=_headers("h1"),
                               json=body)
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_BODY"
        assert h.count("breakdown") == 0

    def test_same_key_different_body_over_http_is_409(self, monkeypatch,
                                                      tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        first = client.post("/breakdowns", headers=_headers("h1"),
                            json=base_body())
        assert first.status_code == 201
        second = client.post("/breakdowns", headers=_headers("h1"),
                             json=base_body(episode_id="ep_2"))
        assert second.status_code == 409
        assert second.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert h.count("breakdown") == 1
        assert h.count("breakdown_revision") == 1

    def test_replay_over_http_is_marked_replayed(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        first = client.post("/breakdowns", headers=_headers("h1"),
                            json=base_body()).json()
        replay = client.post("/breakdowns", headers=_headers("h1"),
                             json=base_body()).json()
        assert replay["replayed"] is True
        assert replay["breakdown_id"] == first["breakdown_id"]
        assert h.count("breakdown") == 1

    def test_principal_header_disagreeing_with_body_is_403(self, monkeypatch,
                                                           tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = base_body(actor={"principal_id": "someone_else",
                                "principal_type": "human"})
        response = client.post("/breakdowns", headers=_headers("h1"),
                               json=body)
        assert response.status_code == 403
        assert response.json()["code"] == "PERMISSION_DENIED"
        assert response.json()["details"]["gap"] == "S05-G22"
        assert h.count("breakdown") == 0

    def test_principal_header_wins_over_the_body_actor(self, monkeypatch,
                                                       tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = base_body()
        body.pop("actor")
        created = client.post("/breakdowns",
                              headers=_headers("h1", **{
                                  "X-Principal-Type": "agent"}),
                              json=body).json()
        creator = h.one("breakdown", breakdown_id=created["breakdown_id"])[
            "created_by"]
        assert json.loads(creator) == {"principal_id": "u_human",
                                       "principal_type": "agent"}

    def test_service_error_keeps_its_status_and_details(self, monkeypatch,
                                                        tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        response = client.post("/breakdowns", headers=_headers("h1"),
                               json=base_body(scenes=[scene(1, 4),
                                                      scene(6, 10)]))
        assert response.status_code == 422
        payload = response.json()
        assert payload["code"] == "LINE_COVERAGE_GAP"
        assert payload["details"]["missing_seqs"] == [5]
        assert payload["retryable"] is False
        assert payload["correlation_id"] == ""

    def test_cross_tenant_read_over_http_is_404(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/breakdowns", headers=_headers("h1"),
                              json=base_body()).json()
        response = client.get(f"/breakdowns/{created['breakdown_id']}",
                              headers={"X-Tenant-Id": "t2"})
        ghost = client.get("/breakdowns/bkd_does_not_exist",
                           headers={"X-Tenant-Id": "t2"})
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"
        # E28：跨租户与"不存在"必须无差别。唯一的差别是回显了调用方自己
        # 发来的那串 ID（它本来就是入参），其余逐字节相同。
        assert response.json()["details"] == {}
        assert ghost.json()["details"] == {}
        assert response.text.replace(created["breakdown_id"],
                                     "bkd_does_not_exist") == ghost.text

    def test_retire_and_reorder_routes_are_reachable(self, monkeypatch,
                                                     tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/breakdowns", headers=_headers("h1"),
                              json=base_body()).json()
        ids = [item["scene_id"] for item in created["scenes"]]
        reordered = client.post(
            f"/breakdowns/{created['breakdown_id']}/revisions/1/scene-order",
            headers=_headers("h2"), json={"scene_ids": list(reversed(ids))})
        assert reordered.status_code == 200, reordered.text
        assert [item["scene_id"] for item in reordered.json()["scenes"]] == \
            list(reversed(ids))
        retired = client.post(f"/scenes/{ids[0]}:retire",
                              headers=_headers("h3"),
                              json={"reason": "removed"})
        assert retired.status_code == 200, retired.text
        assert retired.json()["retired"] is True
        assert retired.json()["changed"] is True

    def test_unconfigured_upstream_is_503_over_http(self, monkeypatch,
                                                    tmp_path):
        # 不注入 stub：模块构造的是 `build_script_source(None)`，必须 503。
        monkeypatch.delenv("S04_BASE_URL", raising=False)
        monkeypatch.setenv("S05_DB", str(tmp_path / "s05.db"))
        path = Path(__file__).resolve().parents[1] / "api" / "app.py"
        spec = importlib.util.spec_from_file_location("s05_api_app_bare", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["s05_api_app_bare"] = module
        client = TestClient(module.app)
        response = client.post("/breakdowns", headers=_headers("h1"),
                               json=base_body())
        assert response.status_code == 503
        assert response.json()["code"] == "SCRIPT_SOURCE_UNAVAILABLE"
        assert response.json()["retryable"] is True
        assert module.service.conn.execute(
            "SELECT COUNT(*) FROM breakdown").fetchone()[0] == 0
