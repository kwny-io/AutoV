"""S04 单测。编号对应 `plans/S04-narrative.md` §8 的 T04-01 … T04-37。

上游全部是 stub，**按真实的上游契约写**：

- S03 `GET /episodes/{id}` 返回带 `episode_id` 与 `series_id` 的对象
  （`project-catalog` 的 `get_episode`）；测试只塞这两项。
- S19 `GET /agent-tasks/{id}` 返回带 `task_id` 的任务体；`POST /agent-tasks`
  返回 `task_id`。本服务只读 `task_id`，不看任务状态。
- 正文对象存储不是 HTTP 服务，是 S04 自己的适配器（S04-G1 介质未定），
  用 `deps.InMemoryBodyStore` 覆盖真实调用路径。

结构性断言一律查库（`PRAGMA table_info` / `sqlite_master`）而不是查响应体：
本服务最严重的错误是长出 `accepted`、`scene_id`、候选缓存这类第二权威，
响应体里少个键证明不了什么，少一列才能。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from event_sdk import Inbox
from fastapi.testclient import TestClient
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from narrative.deps import (
    InMemoryBodyStore,
    build_body_store,
    build_catalog_source,
    build_task_source,
)
from narrative.schema import (
    CONTRACT_GAPS,
    ID_PREFIX,
    MAX_BATCH_IDS,
    MAX_BODY_BYTES,
    MAX_CHUNK_BYTES,
    REVISION_STATES,
    SCRIPT_KINDS,
)
from narrative.service import (
    BODY_KEY,
    EVENT_LINE_REVISION_CREATED,
    EVENT_REVISION_CREATED,
    EVENT_SCRIPT_WITHDRAWN,
    LOCAL_EVENT_TYPES,
    MANIFEST_COMMITTED,
    SOURCE,
    TOPIC,
    NarrativeService,
    ServiceError,
    line_set_digest,
    revision_content_digest,
    structure_digest,
    text_digest,
)

HUMAN = {"principal_id": "u_human", "principal_type": "human"}
AGENT = {"principal_id": "u_agent", "principal_type": "agent"}


# ------------------------------------------------------------------ 上游 stub

class EpisodeCatalog:
    """S03 `GET /episodes/{episode_id}`。只读 `episode_id` / `series_id`。"""

    def __init__(self, episodes=None):
        self.episodes = ({"ep_1": "ser_1", "ep_2": "ser_1", "ep_3": "ser_1"}
                         if episodes is None else episodes)
        self.calls = 0

    def __call__(self, tenant_id, episode_id):
        self.calls += 1
        value = self.episodes.get(episode_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            # S03 对不存在与跨租户同码：404 REF_NOT_FOUND。
            raise ServiceError(404, "REF_NOT_FOUND", "no such episode")
        return {"episode_id": episode_id, "series_id": value}


class TaskSource:
    """S19：`__call__` 是回查，`submit` 是建任务。"""

    def __init__(self, tasks=None):
        self.tasks = {"task_1": {}, "task_2": {}} if tasks is None else tasks
        self.queried: list[str] = []
        self.submitted: list[tuple] = []

    def __call__(self, tenant_id, task_id):
        self.queried.append(task_id)
        value = self.tasks.get(task_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ServiceError(404, "TASK_NOT_FOUND", "no such task")
        return {"task_id": task_id, **value}

    def get(self, tenant_id, task_id):
        return self(tenant_id, task_id)

    def submit(self, tenant_id, command_id, task_request):
        self.submitted.append((command_id, task_request))
        return {"task_id": "task_new"}


# ------------------------------------------------------------------ 数据形状

def author(principal_id="u_human", principal_type="human"):
    return {"principal_id": principal_id, "principal_type": principal_type}


def line(text="你终于来了。", **over):
    base = {"line_id": None, "text": text, "speaker_label": "陈默"}
    base.update(over)
    return base


def revision_body(**over):
    base = {
        "title": "第 3 集",
        "body": "第一场\n你终于来了。\n第二场\n我们走吧。",
        "structure": {"scene_markers": ["1", "2"],
                      "characters": ["林岚", "陈默"]},
        "lines": [line()],
        "author": author(),
    }
    base.update(over)
    return base


def episode_body(**over):
    base = {"series_id": "ser_1", "script_kind": "episode_script",
            "episode_id": "ep_1", "author": author()}
    base.update(over)
    return base


def s04_ref(script_id, revision, content_hash=None):
    return {"service_id": "S04", "id": script_id, "revision": revision,
            "content_hash": content_hash or "c" * 64}


def manifest_event(*, event_id="evt_1", sequence=1, tenant="t1",
                   manifest_id="man_1", manifest_revision=1, refs=(),
                   execution_fence=1, event_type=None):
    return {
        "id": event_id,
        "type": MANIFEST_COMMITTED if event_type is None else event_type,
        "tenant_id": tenant,
        "sequence": sequence,
        "data": {
            "manifest_ref": {"id": manifest_id,
                             "revision": manifest_revision},
            "execution_fence": execution_fence,
            "source_revisions": list(refs),
        },
    }


# ------------------------------------------------------------------ harness

class Harness:
    def __init__(self, conn=None, **over):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # `outbox` 由 NarrativeService 里的 Outbox 建表；`inbox` 由
        # consume_manifest_event 首次调用时建。幂等表在 SDK 构造时建。
        self.idem = SqliteIdempotencyStore(self.conn)
        # 构造 Inbox 只为建表：生产路径上它也总是先建表再消费。
        Inbox(self.conn)
        self.body_store = over.get("body_store", InMemoryBodyStore())
        self.catalog = over.get("catalog", EpisodeCatalog())
        self.tasks = over.get("tasks", TaskSource())
        self.service = NarrativeService(
            self.conn,
            task_source=self.tasks,
            body_store=self.body_store,
            catalog_source=self.catalog,
            enable_writes=over.get("enable_writes", True),
        )

    # ---- 幂等外壳：与 api/app.py 的 command() 同语义 ----
    def command(self, fn, body, tenant_id="t1", command_id="c1"):
        payload = {**body, "command_id": command_id}
        digest = canonical_request_hash(payload)
        record = self.idem.begin(tenant_id, command_id, digest)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = fn(tenant_id, command_id, payload)
        except Exception:
            self.idem.abort(tenant_id, command_id)
            raise
        self.idem.finish(tenant_id, command_id,
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ---- 命令助手 ----
    def create(self, tenant_id="t1", command_id="c1", **over):
        body = episode_body(**over)
        return self.command(
            lambda t, c, b: self.service.create_script(
                t, b, command_id=c, request_hash=""),
            body, tenant_id, command_id)

    def append(self, script_id, tenant_id="t1", command_id="c2", **over):
        body = revision_body(**over)
        return self.command(
            lambda t, c, b: self.service.append_revision(
                t, script_id, b, command_id=c, request_hash=""),
            body, tenant_id, command_id)

    def seal(self, script_id, revision, tenant_id="t1", command_id="c3",
             **over):
        body = {"author": author()}
        body.update(over)
        return self.command(
            lambda t, c, b: self.service.seal(
                t, script_id, revision, b, command_id=c, request_hash=""),
            body, tenant_id, command_id)

    def withdraw(self, script_id, revision, tenant_id="t1", command_id="c4",
                 **over):
        body = {} if not over else dict(over)
        return self.command(
            lambda t, c, b: self.service.withdraw(
                t, script_id, revision, b, command_id=c, request_hash=""),
            body, tenant_id, command_id)

    def edit_line(self, line_id, text, tenant_id="t1", command_id="c5", **over):
        body = {"text": text}
        body.update(over)
        return self.command(
            lambda t, c, b: self.service.append_line_revision(
                t, line_id, b, command_id=c, request_hash=""),
            body, tenant_id, command_id)

    def retire(self, line_id, tenant_id="t1", command_id="c6"):
        return self.command(
            lambda t, c, b: self.service.retire_line(
                t, line_id, b, command_id=c, request_hash=""),
            {}, tenant_id, command_id)

    # ---- 组合：建 script + 一版 + seal ----
    def sealed(self, *, lines=None, body="第一场\n你终于来了。", tenant_id="t1",
               command_id_prefix="x", **over):
        script = self.create(tenant_id=tenant_id,
                             command_id=f"{command_id_prefix}1")
        rev = self.append(script["script_id"], tenant_id=tenant_id,
                          command_id=f"{command_id_prefix}2",
                          body=body,
                          lines=lines if lines is not None else [line()],
                          **over)
        self.seal(script["script_id"], rev["revision"], tenant_id=tenant_id,
                  command_id=f"{command_id_prefix}3")
        return script["script_id"], rev["revision"]

    # ---- 查询助手 ----
    def rows(self, table, **where):
        clause = " AND ".join(f"{k}=?" for k in where) or "1"
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {clause}", tuple(where.values())
        ).fetchall()

    def one(self, table, **where):
        rows = self.rows(table, **where)
        assert len(rows) == 1, (table, where, len(rows))
        return rows[0]

    def count(self, table):
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def events(self):
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM outbox ORDER BY created_at, sequence")]

    def local_events(self):
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM local_event ORDER BY created_at")]

    def state_of(self, script_id, revision, tenant_id="t1"):
        return self.one("script_revision", tenant_id=tenant_id,
                        script_id=script_id, revision=revision)["state"]

    def first_line_id(self, script_id, tenant_id="t1"):
        return self.one("line", tenant_id=tenant_id,
                        script_id=script_id)["line_id"]


def raises(status: int, code: str):
    """`pytest.raises` 的窄版：同时钉住状态码与错误码。"""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        with pytest.raises((ServiceError, CommandConflict)) as info:
            yield info
        value = info.value
        assert getattr(value, "status", 409) == status, (
            getattr(value, "status", 409), value.code)
        assert value.code == code, value.code
    return _cm()


def _columns(conn, table):
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _tables(conn):
    return {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


# ================================================================ 纯函数

class TestDigests:
    """内容寻址的算法。漂移一次，S08 已提交的 manifest 全部失效（R1）。"""

    def test_line_set_is_order_independent(self):
        # 行顺序只体现在 seq；"这版包含哪些内容"不能随数组次序变。
        rows = [("ln_a", 1, "h1"), ("ln_b", 2, "h2"), ("ln_c", 1, "h3")]
        assert line_set_digest(rows) == line_set_digest(list(reversed(rows)))

    def test_line_set_changes_with_any_component(self):
        base = line_set_digest([("ln_a", 1, "h1")])
        assert line_set_digest([("ln_b", 1, "h1")]) != base
        assert line_set_digest([("ln_a", 2, "h1")]) != base
        assert line_set_digest([("ln_a", 1, "h2")]) != base

    def test_line_set_is_sha256_hex(self):
        value = line_set_digest([("ln_a", 1, "h1")])
        assert len(value) == 64
        assert value == hashlib.sha256(b"ln_a:1:h1").hexdigest()

    def test_content_digest_matches_documented_formula(self):
        header = {"script_id": "scr_1", "revision": 2, "title": "第 3 集",
                  "language": "zh-CN", "parent_revision": 1,
                  "structure": {"scene_markers": ["1"], "characters": [],
                                "line_count": 1}}
        expected = hashlib.sha256(
            json.dumps(header, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")
            + b"\n" + b"a" * 64).hexdigest()
        assert revision_content_digest(
            script_id="scr_1", revision=2, title="第 3 集",
            language="zh-CN", structure=header["structure"],
            parent_revision=1, line_set_hash="a" * 64) == expected

    def test_content_digest_excludes_author_state_and_time(self):
        # 函数签名里根本没有 author / created_at / state —— 这是"不含"的
        # 最强形态。这里再用两次调用的稳定性把它钉住。
        kw = dict(script_id="scr_1", revision=1, title=None, language="zh-CN",
                  structure={"scene_markers": [], "characters": []},
                  parent_revision=None, line_set_hash="b" * 64)
        assert revision_content_digest(**kw) == revision_content_digest(**kw)
        assert "author" not in (revision_content_digest.__code__.co_varnames
                                + revision_content_digest.__code__.co_names)
        assert "state" not in (revision_content_digest.__code__.co_varnames
                               + revision_content_digest.__code__.co_names)

    def test_text_digest_is_sha256_of_utf8_bytes(self):
        assert text_digest("你终于来了。") == \
            hashlib.sha256("你终于来了。".encode("utf-8")).hexdigest()

    def test_structure_digest_is_key_order_independent(self):
        a = {"scene_markers": ["1"], "characters": ["林岚"]}
        b = {"characters": ["林岚"], "scene_markers": ["1"]}
        assert structure_digest(a) == structure_digest(b)

    def test_structure_digest_changes_with_content(self):
        a = {"scene_markers": ["1"], "characters": []}
        b = {"scene_markers": ["2"], "characters": []}
        assert structure_digest(a) != structure_digest(b)


# ================================================================ 反模式的结构形态

class TestStructuralRedLines:
    """§9 的 16 条。能用"库里没有这一列/这张表"证明的，就用库证明。"""

    def test_no_accepted_or_approved_column_anywhere(self):
        # 反模式 1 / 2：第二接受权威在结构上不可能存在。
        h = Harness()
        banned = {"accepted", "is_accepted", "accepted_at", "approved",
                  "approved_by", "approved_at"}
        for table in _tables(h.conn):
            leaked = banned & set(_columns(h.conn, table))
            assert not leaked, (table, leaked)

    def test_no_claim_scene_or_translation_table(self):
        # 反模式 7 / 9：S04 不持有场次、候选、译文、音素表。
        h = Harness()
        banned = {"scene", "scene_breakdown", "shot", "shot_spec", "claim",
                  "claim_candidate", "acceptance", "translation",
                  "phoneme_lexicon", "canon", "manifest"}
        assert not (_tables(h.conn) & banned), _tables(h.conn) & banned

    def test_script_revision_stores_no_body_text(self):
        # §2.6：正文在私有对象存储，库里只有引用。列名里没有正文。
        h = Harness()
        cols = set(_columns(h.conn, "script_revision"))
        assert "body_ref" in cols
        assert not ({"body", "text", "content", "script_text"} & cols)

    def test_body_chunk_has_no_payload_column(self):
        h = Harness()
        cols = set(_columns(h.conn, "body_chunk"))
        assert {"bytes_ref", "sha256", "byte_size"} <= cols
        assert "payload" not in cols and "data" not in cols

    def test_line_has_no_physical_delete_flag(self):
        # 反模式 6：删除 = retired，不是 DELETE，也不是 deleted_at。
        h = Harness()
        cols = set(_columns(h.conn, "line"))
        assert "life_state" in cols and "retired_at" in cols
        assert not ({"deleted_at", "is_deleted", "removed_at"} & cols)

    def test_script_has_no_scene_identity_column(self):
        # 反模式 7：S04 不铸造 scene_id。唯一与场次有关的是文本标记。
        h = Harness()
        cols = set(_columns(h.conn, "script"))
        assert not ({"scene_id", "scene_count", "first_scene_id"} & cols)

    def test_revision_states_have_no_accepted_value(self):
        # §3.4：没有 accepted / published / in_production。
        assert set(REVISION_STATES) == {"DRAFT", "SEALED", "SUPERSEDED",
                                       "WITHDRAWN"}

    def test_structure_rejects_story_time_keys(self):
        # 反模式 / T04-13：S04 不记录故事时间。未知键直接拒绝。
        h = Harness()
        script = h.create()
        with raises(400, "INVALID_BODY"):
            h.append(script["script_id"], command_id="c2",
                     structure={"scene_markers": ["1"], "story_time": "1998"})
        with raises(400, "INVALID_BODY"):
            h.append(script["script_id"], command_id="c3",
                     structure={"scene_markers": ["1"], "timeline": [1, 2]})

    def test_contract_gaps_are_registered_with_shape(self):
        # 缺口必须可读、可去重、可分类；needs_owner 一律不得被填成实现。
        kinds = {"conflict", "gap", "needs_owner"}
        ids = [gap["id"] for gap in CONTRACT_GAPS]
        assert len(ids) == len(set(ids))
        for gap in CONTRACT_GAPS:
            assert set(gap) == {"id", "kind", "summary", "handling"}
            assert gap["kind"] in kinds
            assert gap["summary"] and gap["handling"]

    def test_contract_gaps_travel_with_responses(self):
        h = Harness()
        script = h.create()
        assert script["x-contract-gaps"] == CONTRACT_GAPS
        assert h.service.get_script("t1", script["script_id"])[
            "x-contract-gaps"] == CONTRACT_GAPS

    def test_id_prefixes_are_distinct(self):
        values = list(ID_PREFIX.values())
        assert len(values) == len(set(values))
        assert all(value.endswith("_") and len(value) >= 3 for value in values)

    def test_only_registered_event_types_go_to_outbox(self):
        # 反模式 3 的形态：DRAFT / 行级 / 撤回都不进 outbox。
        assert EVENT_LINE_REVISION_CREATED in LOCAL_EVENT_TYPES
        assert EVENT_SCRIPT_WITHDRAWN in LOCAL_EVENT_TYPES
        assert EVENT_REVISION_CREATED not in LOCAL_EVENT_TYPES

    def test_line_retired_is_a_state_not_a_row_removal(self):
        h = Harness()
        script_id, _ = h.sealed(command_id_prefix="a")
        line_id = h.first_line_id(script_id)
        h.retire(line_id, command_id="a9")
        assert h.count("line") == 1
        assert h.one("line", line_id=line_id)["retired_at"] is not None


# ================================================================ 8.1 三集连续生产

class TestThreeEpisodes:
    def test_t04_01_three_scripts_are_independent(self):
        h = Harness()
        scripts = []
        for index, episode in enumerate(["ep_1", "ep_2", "ep_3"]):
            script = h.create(command_id=f"c{index}s", episode_id=episode)
            rev = h.append(script["script_id"], command_id=f"c{index}a",
                           body=f"第 {index + 1} 集正文",
                           lines=[line(f"第 {index + 1} 集的台词")])
            h.seal(script["script_id"], rev["revision"],
                   command_id=f"c{index}z")
            scripts.append((script["script_id"], rev))
        assert len({script_id for script_id, _ in scripts}) == 3
        hashes = {h.service.get_revision("t1", script_id, rev["revision"])[
            "content_hash"] for script_id, rev in scripts}
        assert len(hashes) == 3
        # 事件各一条。
        assert h.count("outbox") == 3
        assert len({event["data"]["script_id"] for event in h.events()}) == 3

    def test_t04_02_line_id_survives_rewrite(self):
        h = Harness()
        script_id, revision = h.sealed(command_id_prefix="b")
        line_id = h.first_line_id(script_id)
        first = h.one("line_revision", line_id=line_id)
        h.edit_line(line_id, "你终于来了吗？", command_id="b9")
        second = h.one("line_revision", line_id=line_id, revision=2)
        assert second["line_id"] == first["line_id"] == line_id
        assert second["revision"] == first["revision"] + 1
        # 改稿不新建行身份。
        assert h.count("line") == 1

    def test_t04_03_revision_query_matches_manifest_refs(self):
        # 三条 S04 引用必须能用 GET /revisions/{rev} 复算。
        h = Harness()
        refs = []
        for index, episode in enumerate(["ep_1", "ep_2", "ep_3"]):
            script = h.create(command_id=f"d{index}s", episode_id=episode)
            rev = h.append(script["script_id"], command_id=f"d{index}a",
                           body=f"第 {index + 1} 集", lines=[line(f"台词{index}")])
            h.seal(script["script_id"], rev["revision"],
                   command_id=f"d{index}z")
            got = h.service.get_revision("t1", script["script_id"],
                                         rev["revision"])
            refs.append(s04_ref(script["script_id"], got["revision"],
                                got["content_hash"]))
        applied = h.service.apply_manifest_committed(
            "t1", manifest_event(sequence=1, refs=refs))
        assert applied["applied"] is True
        assert applied["references"] == 3
        for ref in refs:
            row = h.service.get_revision("t1", ref["id"], ref["revision"])
            assert row["content_hash"] == ref["content_hash"]


# ================================================================ 8.2 改镜与对白时长

class TestRewrite:
    def test_t04_04_line_edit_leaves_sealed_hash_alone(self):
        h = Harness()
        script_id, revision = h.sealed(command_id_prefix="e")
        before = h.service.get_revision("t1", script_id, revision)
        h.edit_line(h.first_line_id(script_id), "改过的台词", command_id="e9")
        after = h.service.get_revision("t1", script_id, revision)
        # 反模式 4 的操作面：改字不碰任何已 seal 的 revision 内容字段。
        assert after["content_hash"] == before["content_hash"]
        assert after["line_set_hash"] == before["line_set_hash"]
        assert after["state"] == "SEALED"
        assert after["sealed_at"] == before["sealed_at"]

    def test_t04_05_s04_emits_no_redo_event(self):
        h = Harness()
        script_id, _ = h.sealed(command_id_prefix="f")
        outbox_before = h.count("outbox")
        h.edit_line(h.first_line_id(script_id), "改台词", command_id="f9")
        # 改台词不产生任何 outbox 行：重做是 S06/S09 的判断，不是 S04 的职责。
        assert h.count("outbox") == outbox_before
        local = h.local_events()
        assert len(local) == 1
        assert local[0]["line_id"] == h.first_line_id(script_id)

    def test_t04_06_s04_makes_no_duration_judgement(self):
        # 超长台词照收；S04 的响应里没有时长/可行性的任何判断字段。
        h = Harness()
        script = h.create(command_id="g1")
        long_text = "这是一句非常长的台词。" * 200
        rev = h.append(script["script_id"], command_id="g2",
                       lines=[line(long_text)])
        assert rev["revision"] == 1
        stored = h.service.list_revision_lines("t1", script["script_id"], 1)
        assert stored["lines"][0]["text"] == long_text
        for banned in ("duration_seconds", "feasible", "too_long",
                       "estimated_seconds"):
            assert banned not in rev
            assert banned not in stored["lines"][0]

    def test_t04_07_unknown_episode_is_404_and_no_nearest(self):
        h = Harness()
        with raises(404, "EPISODE_NOT_FOUND") as info:
            h.create(episode_id="ep_missing")
        # 不返回近似集：details 里只有被请求的那个 id。
        assert info.value.details == {"episode_id": "ep_missing"}

    def test_episode_script_requires_episode_id(self):
        h = Harness()
        with raises(400, "INVALID_BODY"):
            h.create(episode_id=None)

    def test_series_outline_must_not_carry_episode(self):
        h = Harness()
        with raises(400, "INVALID_BODY"):
            h.create(script_kind="series_outline", episode_id="ep_1")

    def test_episode_series_mismatch_is_422(self):
        h = Harness(catalog=EpisodeCatalog({"ep_1": "ser_other"}))
        with raises(422, "EPISODE_SERIES_MISMATCH") as info:
            h.create(episode_id="ep_1", series_id="ser_1")
        assert info.value.details["actual_series_id"] == "ser_other"

    def test_catalog_down_is_503_not_a_default(self):
        h = Harness(catalog=EpisodeCatalog({"ep_1": ConnectionError("refused")}))
        with raises(503, "CATALOG_UNAVAILABLE"):
            h.create(episode_id="ep_1")

    def test_unconfigured_catalog_is_503(self):
        h = Harness(catalog=None)
        with raises(503, "CATALOG_UNAVAILABLE"):
            h.create(episode_id="ep_1")


# ================================================================ 8.3 改稿与正史

class TestRevisionsAndCanon:
    def test_t04_08_old_revision_still_readable(self):
        h = Harness()
        script = h.create(command_id="h1")
        first = h.append(script["script_id"], command_id="h2",
                         body="第一版", lines=[line("第一版台词")])
        h.seal(script["script_id"], first["revision"], command_id="h3")
        second = h.append(script["script_id"], command_id="h4",
                          body="第二版", lines=[line("第二版台词")])
        assert h.service.get_revision("t1", script["script_id"],
                                      first["revision"])["state"] == "SEALED"
        assert h.service.get_body("t1", script["script_id"],
                                  first["revision"])["body"] == "第一版"
        assert second["revision"] == first["revision"] + 1

    def test_t04_09_extraction_stores_only_handoff_ref(self):
        h = Harness()
        script_id, revision = h.sealed(command_id_prefix="i")
        run = h.command(
            lambda t, c, b: h.service.extract_claims(
                t, script_id, revision, b, command_id=c, request_hash=""),
            {"task_request": {"role": "claim_extractor", "limits": {}}},
            command_id="i9")
        assert run["result_state"] == "PENDING"
        done = h.service.complete_extraction(
            "t1", run["extraction_id"],
            {"result_state": "SUCCEEDED",
             "handoff_ref": {"service_id": "S16", "id": "batch_1"},
             "candidate_count": 7})
        assert done["handoff_ref"] == {"service_id": "S16", "id": "batch_1"}
        # 反模式 9：S04 不缓存候选。
        assert done["candidates"] is None
        assert "candidate" not in _tables(h.conn)
        assert h.tasks.submitted[0][0] == "i9"

    def test_t04_10_draft_extraction_rejected(self):
        h = Harness()
        script = h.create(command_id="j1")
        rev = h.append(script["script_id"], command_id="j2")
        with raises(409, "ILLEGAL_STATE_TRANSITION") as info:
            h.command(
                lambda t, c, b: h.service.extract_claims(
                    t, script["script_id"], rev["revision"], b,
                    command_id=c, request_hash=""),
                {"task_request": {}}, command_id="j3")
        assert info.value.details["state"] == "DRAFT"
        assert h.count("claim_extraction_run") == 0
        assert h.tasks.submitted == []

    def test_t04_11_withdraw_unreferenced_sealed(self):
        h = Harness()
        script_id, revision = h.sealed(command_id_prefix="k")
        # 投影必须先新鲜：没有水位行 = 从未消费 = 落后（S04-G12）。
        h.service.apply_manifest_committed(
            "t1", manifest_event(sequence=1, refs=[]))
        result = h.withdraw(script_id, revision, command_id="k9")
        assert result["state"] == "WITHDRAWN"
        assert h.state_of(script_id, revision) == "WITHDRAWN"
        # 撤回是 local_event，不进 outbox（NAR-7）。
        assert h.count("outbox") == 1
        assert h.local_events()[0]["state"] == "WITHDRAWN"

    def test_t04_12_withdraw_referenced_revision_is_409(self):
        h = Harness()
        script_id, revision = h.sealed(command_id_prefix="l")
        h.service.apply_manifest_committed(
            "t1", manifest_event(sequence=1,
                                 refs=[s04_ref(script_id, revision)]))
        with raises(409, "REVISION_REFERENCED_BY_MANIFEST") as info:
            h.withdraw(script_id, revision, command_id="l9")
        assert info.value.details["references"][0]["manifest_id"] == "man_1"
        # 旧 revision 保持 SEALED。
        assert h.state_of(script_id, revision) == "SEALED"

    def test_withdraw_with_stale_projection_is_refused(self):
        # 反模式 13：不能因为"没收到事件"就当成"没有被引用"。
        h = Harness()
        script_id, revision = h.sealed(command_id_prefix="m")
        with raises(409, "REFERENCE_CHECK_STALE") as info:
            h.withdraw(script_id, revision, command_id="m9")
        assert info.value.retryable is True
        assert h.state_of(script_id, revision) == "SEALED"

    def test_withdraw_draft_has_no_guard(self):
        h = Harness()
        script = h.create(command_id="n1")
        rev = h.append(script["script_id"], command_id="n2")
        result = h.withdraw(script["script_id"], rev["revision"],
                            command_id="n3")
        assert result["state"] == "WITHDRAWN"
        assert result["already_withdrawn"] is False

    def test_withdraw_twice_is_idempotent(self):
        h = Harness()
        script = h.create(command_id="o1")
        rev = h.append(script["script_id"], command_id="o2")
        h.withdraw(script["script_id"], rev["revision"], command_id="o3")
        again = h.withdraw(script["script_id"], rev["revision"],
                           command_id="o4")
        assert again["already_withdrawn"] is True

    def test_superseded_cannot_be_withdrawn(self):
        h = Harness()
        script = h.create(command_id="p1")
        first = h.append(script["script_id"], command_id="p2",
                         body="第一版", lines=[line("一")])
        h.seal(script["script_id"], first["revision"], command_id="p3")
        second = h.append(script["script_id"], command_id="p4",
                          body="第二版", lines=[line("二")])
        h.seal(script["script_id"], second["revision"], command_id="p5")
        assert h.state_of(script["script_id"],
                          first["revision"]) == "SUPERSEDED"
        with raises(409, "ILLEGAL_STATE_TRANSITION"):
            h.withdraw(script["script_id"], first["revision"],
                       command_id="p6")


# ================================================================ 8.4 闪回与知情

class TestFlashback:
    def test_t04_13_no_story_time_only_text_markers(self):
        h = Harness()
        script_id, revision = h.sealed(
            command_id_prefix="q",
            structure={"scene_markers": ["闪回:1998", "现在"],
                       "characters": ["林岚"]})
        got = h.service.get_revision("t1", script_id, revision)
        assert got["structure"]["scene_markers"] == ["闪回:1998", "现在"]
        # 标记只是字符串；没有结构化的时间字段，也没有 scene 身份。
        assert not ({"story_time", "timeline", "scene_ids", "scenes"}
                    & set(got["structure"]))
        assert "scene" not in _tables(h.conn)

    def test_t04_14_same_dialogue_in_two_scripts_is_two_lines(self):
        h = Harness()
        text = "我们还会再见的。"
        first = h.create(command_id="r1", episode_id="ep_2")
        second = h.create(command_id="r2", episode_id="ep_3")
        h.append(first["script_id"], command_id="r3", lines=[line(text)])
        h.append(second["script_id"], command_id="r4", lines=[line(text)])
        ids = {row["line_id"] for row in h.rows("line")}
        assert len(ids) == 2
        assert h.count("line_revision") == 2
        hashes = {row["text_hash"] for row in h.rows("line_revision")}
        assert hashes == {text_digest(text)}


# ================================================================ 8.5 幂等

class TestIdempotency:
    def test_t04_15_same_command_same_body_replays(self):
        h = Harness()
        script = h.create(command_id="s1")
        rev = h.append(script["script_id"], command_id="s2")
        first = h.seal(script["script_id"], rev["revision"], command_id="s3")
        second = h.seal(script["script_id"], rev["revision"], command_id="s3")
        assert first["replayed"] is False
        assert second["replayed"] is True
        assert second["sealed_at"] == first["sealed_at"]
        assert h.count("outbox") == 1

    def test_t04_16_same_command_different_body_is_conflict(self):
        h = Harness()
        script = h.create(command_id="t1c")
        h.append(script["script_id"], command_id="t2c", body="第一版")
        with raises(409, "IDEMPOTENCY_KEY_REUSED"):
            h.append(script["script_id"], command_id="t2c", body="第二版")
        # 冲突不落第二版。
        assert h.count("script_revision") == 1

    def test_in_flight_command_is_retryable_409(self):
        h = Harness()
        script = h.create(command_id="u1")
        body = revision_body(body="处理中的正文")
        payload = {**body, "command_id": "u2"}
        digest = canonical_request_hash(payload)
        h.idem.begin("t1", "u2", digest)  # 占位但没 finish
        with pytest.raises(CommandInProgress):
            h.idem.begin("t1", "u2", digest)

    def test_t04_17_concurrent_seal_only_one_wins(self):
        h = Harness()
        script = h.create(command_id="v1")
        rev = h.append(script["script_id"], command_id="v2")
        h.seal(script["script_id"], rev["revision"], command_id="v3")
        with raises(409, "ILLEGAL_STATE_TRANSITION"):
            h.seal(script["script_id"], rev["revision"], command_id="v4")
        assert h.count("outbox") == 1

    def test_t04_18_same_text_reuses_original_line_revision(self):
        h = Harness()
        script_id, _ = h.sealed(command_id_prefix="w")
        line_id = h.first_line_id(script_id)
        original = h.one("line_revision", line_id=line_id)
        result = h.edit_line(line_id, original["text"], command_id="w9")
        assert result["reused"] is True
        assert result["revision"] == original["revision"]
        assert h.count("line_revision") == 1

    def test_t04_19_same_draft_twice_creates_one_revision(self):
        h = Harness()
        script = h.create(command_id="x1")
        first = h.append(script["script_id"], command_id="x2")
        second = h.append(script["script_id"], command_id="x2")
        assert second["replayed"] is True
        assert second["revision"] == first["revision"]
        assert h.count("script_revision") == 1

    def test_different_command_same_text_is_a_new_revision(self):
        # `content_hash` 里含 `revision`（§2.2），所以"同文本再交一次"是一个
        # 新版本、新 hash。去重不能按文本跨版本合并 —— 那样 revision 会跳号。
        h = Harness()
        script = h.create(command_id="y1")
        first = h.append(script["script_id"], command_id="y2")
        second = h.append(script["script_id"], command_id="y3")
        assert second["revision"] == first["revision"] + 1
        assert second["content_hash"] != first["content_hash"]
        assert h.count("script_revision") == 2

    def test_content_hash_header_has_exactly_six_keys(self):
        # 反模式 12：author / created_at / state 不在 header 里。键集就是
        # 证据 —— 多一个键，已提交 manifest 的 hash 全部作废。
        header = {"script_id": "scr_1", "revision": 1, "title": None,
                  "language": "zh-CN",
                  "structure": {"scene_markers": [], "characters": []},
                  "parent_revision": None}
        expected = hashlib.sha256(
            json.dumps(header, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")
            + b"\n" + b"b" * 64).hexdigest()
        actual = revision_content_digest(
            script_id="scr_1", revision=1, title=None, language="zh-CN",
            structure={"scene_markers": [], "characters": []},
            parent_revision=None, line_set_hash="b" * 64)
        assert actual == expected

    def test_cas_mismatch_is_revision_conflict(self):
        h = Harness()
        script = h.create(command_id="aa1")
        h.append(script["script_id"], command_id="aa2")
        with raises(409, "REVISION_CONFLICT") as info:
            h.append(script["script_id"], command_id="aa3",
                     expected_head_revision=0, body="另一版")
        assert info.value.details["current_revision"] == 1


# ================================================================ 8.6 崩溃恢复

class TestCrashRecovery:
    def test_t04_20_orphan_chunks_are_collected(self):
        h = Harness()
        script = h.create(command_id="ab1")
        h.append(script["script_id"], command_id="ab2")
        # 模拟"分块已写、主库提交前崩溃"：对象存储里有 key，库里没有行。
        orphan = BODY_KEY.format(tenant_id="t1", script_id="scr_gone",
                                 revision=1, chunk_no=0)
        h.body_store.put(orphan, b"half written")
        others = BODY_KEY.format(tenant_id="t2", script_id="scr_other",
                                 revision=1, chunk_no=0)
        h.body_store.put(others, b"other tenant")
        assert h.count("script_revision") == 1
        assert h.service.collect_orphan_chunks("t1") == 1
        assert h.body_store.get(orphan) is None
        # 已被引用的分块不能被 GC，别的租户的前缀也不能碰。
        referenced = h.one("body_chunk")["bytes_ref"]
        assert h.body_store.get(referenced) is not None
        assert h.body_store.get(others) is not None

    def test_t04_21_resend_after_commit_returns_same_revision(self):
        h = Harness()
        script = h.create(command_id="ac1")
        first = h.append(script["script_id"], command_id="ac2")
        # 主库已提交、响应丢了：同 command 重发。
        again = h.append(script["script_id"], command_id="ac2")
        assert again["replayed"] is True
        assert again["revision"] == first["revision"]
        assert h.count("script_revision") == 1

    def test_t04_22_outbox_ids_are_stable_for_dedupe(self):
        h = Harness()
        h.sealed(command_id_prefix="ad")
        events = h.events()
        assert len(events) == 1
        # 中继崩溃后重读同一行，event_id 不变；下游按 event_id 去重。
        inbox = Inbox(h.conn)
        assert inbox.consume("downstream", events[0]["id"]) is True
        assert inbox.consume("downstream", events[0]["id"]) is False

    def test_t04_23_no_half_sealed_revision(self, monkeypatch):
        h = Harness()
        script = h.create(command_id="ae1")
        rev = h.append(script["script_id"], command_id="ae2")

        def boom(*args, **kwargs):
            raise RuntimeError("outbox unavailable")

        monkeypatch.setattr(h.service, "_emit_revision_created", boom)
        with pytest.raises(RuntimeError):
            h.seal(script["script_id"], rev["revision"], command_id="ae3")
        row = h.one("script_revision", script_id=script["script_id"],
                    revision=rev["revision"])
        assert row["state"] == "DRAFT"
        assert row["sealed_at"] is None
        assert h.count("outbox") == 0


# ================================================================ 8.7 跨租户

class TestTenantIsolation:
    def test_t04_24_cross_tenant_read_is_same_404(self):
        h = Harness()
        script = h.create(command_id="af1")
        with raises(404, "SCRIPT_NOT_FOUND"):
            h.service.get_script("t2", script["script_id"])
        with raises(404, "SCRIPT_NOT_FOUND"):
            h.service.get_revision("t2", script["script_id"], 1)

    def test_cross_tenant_error_carries_no_foreign_identifiers(self):
        h = Harness()
        script = h.create(command_id="ag1")
        with raises(404, "SCRIPT_NOT_FOUND") as info:
            h.service.get_script("t2", script["script_id"])
        assert info.value.details == {}
        assert script["series_id"] not in str(info.value)

    def test_t04_25_cross_tenant_line_edit_is_404_without_write(self):
        h = Harness()
        script_id, _ = h.sealed(command_id_prefix="ah")
        line_id = h.first_line_id(script_id)
        before = h.count("line_revision")
        with raises(404, "LINE_NOT_FOUND"):
            h.edit_line(line_id, "越权改写", tenant_id="t2", command_id="ah9")
        assert h.count("line_revision") == before

    def test_t04_26_body_tenant_disagrees_header(self):
        # HTTP 层测；内核层面 body 的 tenant 根本不参与：tenant 由调用方传入。
        h = Harness()
        script = h.create(command_id="ai1")
        assert h.service.get_script("t1", script["script_id"])["script_id"] == \
            script["script_id"]
        with raises(404, "SCRIPT_NOT_FOUND"):
            h.service.get_script("t2", script["script_id"])

    def test_t04_27_body_chunk_ref_is_tenant_scoped(self):
        h = Harness()
        script = h.create(command_id="aj1")
        h.append(script["script_id"], command_id="aj2")
        ref = h.one("body_chunk")["bytes_ref"]
        assert ref == BODY_KEY.format(tenant_id="t1",
                                     script_id=script["script_id"],
                                     revision=1, chunk_no=0)
        assert ref.startswith("s04/t1/")
        with raises(404, "SCRIPT_NOT_FOUND"):
            h.service.get_body("t2", script["script_id"], 1)

    def test_cross_tenant_script_list_is_empty_not_error(self):
        h = Harness()
        h.create(command_id="ak1")
        assert h.service.list_scripts("t2")["scripts"] == []


# ================================================================ 8.8 Agent 与审计

class TestAgentAndAudit:
    def test_t04_28_agent_draft_lands_with_agent_author(self):
        h = Harness()
        script = h.create(command_id="al1")
        rev = h.append(script["script_id"], command_id="al2",
                       author=author("u_agent", "agent"),
                       produced_by={"task_id": "task_1",
                                    "definition_revision": 3})
        row = h.one("script_revision", script_id=script["script_id"],
                    revision=rev["revision"])
        assert json.loads(row["author"])["principal_type"] == "agent"
        assert json.loads(row["produced_by"])["task_id"] == "task_1"
        assert row["state"] == "DRAFT"
        assert h.count("outbox") == 0

    def test_agent_without_produced_by_is_422(self):
        h = Harness()
        script = h.create(command_id="am1")
        with raises(422, "UNKNOWN_AGENT_TASK"):
            h.append(script["script_id"], command_id="am2",
                     author=author("u_agent", "agent"))

    def test_t04_29_fake_task_id_is_422(self):
        h = Harness()
        script = h.create(command_id="an1")
        with raises(422, "UNKNOWN_AGENT_TASK") as info:
            h.append(script["script_id"], command_id="an2",
                     author=author("u_agent", "agent"),
                     produced_by={"task_id": "task_forged"})
        assert info.value.details["task_id"] == "task_forged"
        assert h.count("script_revision") == 0

    def test_produced_by_without_task_id_is_422(self):
        # NAR-1：键集并集可接受，但没有 task_id 就无法回查。
        h = Harness()
        script = h.create(command_id="ao1")
        with raises(422, "UNKNOWN_AGENT_TASK") as info:
            h.append(script["script_id"], command_id="ao2",
                     produced_by={"agent_task_ref": "opaque"})
        assert info.value.details["gap"] == "NAR-1"

    def test_unconfigured_task_source_is_503(self):
        h = Harness(tasks=None)
        script = h.create(command_id="ap1")
        with raises(503, "AGENT_TASK_SOURCE_UNAVAILABLE"):
            h.append(script["script_id"], command_id="ap2",
                     author=author("u_agent", "agent"),
                     produced_by={"task_id": "task_1"})

    def test_task_source_down_is_503(self):
        h = Harness(tasks=TaskSource({"task_1": ConnectionError("refused")}))
        script = h.create(command_id="aq1")
        with raises(503, "AGENT_TASK_SOURCE_UNAVAILABLE"):
            h.append(script["script_id"], command_id="aq2",
                     author=author("u_agent", "agent"),
                     produced_by={"task_id": "task_1"})

    def test_t04_30_agent_cannot_submit_sealed(self):
        h = Harness()
        script = h.create(command_id="ar1")
        with raises(409, "ILLEGAL_STATE_TRANSITION"):
            h.append(script["script_id"], command_id="ar2", state="SEALED",
                     author=author("u_agent", "agent"),
                     produced_by={"task_id": "task_1"})
        assert h.count("script_revision") == 0

    def test_agent_cannot_seal(self):
        # 反模式 15：seal 必须是人的动作。
        h = Harness()
        script = h.create(command_id="as1")
        rev = h.append(script["script_id"], command_id="as2",
                       author=author("u_agent", "agent"),
                       produced_by={"task_id": "task_1"})
        with raises(403, "PERMISSION_DENIED") as info:
            h.seal(script["script_id"], rev["revision"], command_id="as3",
                   author=author("u_agent", "agent"))
        assert info.value.details["gap"] == "NAR-6"
        assert h.state_of(script["script_id"], rev["revision"]) == "DRAFT"

    def test_t04_31_audit_fields_complete_and_no_text_in_events(self):
        h = Harness()
        script = h.create(command_id="at1")
        secret = "这句台词只应该出现在正文里。"
        rev = h.append(script["script_id"], command_id="at2",
                       body=secret, lines=[line(secret)],
                       author=author("u_agent", "agent"),
                       produced_by={"task_id": "task_1"})
        h.seal(script["script_id"], rev["revision"], command_id="at3")
        events = h.events()
        assert len(events) == 1
        payload = events[0]["data"]
        assert payload["author"] == author("u_agent", "agent")
        assert payload["produced_by"] == {"task_id": "task_1"}
        # 反模式 11：事件里没有正文，只有 hash。
        encoded = json.dumps(events[0], ensure_ascii=False)
        assert secret not in encoded
        assert payload["content_hash"].startswith("")
        assert set(payload) == {
            "schema_version", "script_id", "revision", "episode_id",
            "series_id", "content_hash", "line_set_hash", "line_count",
            "scene_markers", "structure_hash", "author", "produced_by",
            "parent_revision"}

    def test_seal_requires_author(self):
        h = Harness()
        script = h.create(command_id="au1")
        rev = h.append(script["script_id"], command_id="au2")
        with raises(400, "INVALID_BODY"):
            h.command(
                lambda t, c, b: h.service.seal(
                    t, script["script_id"], rev["revision"], b,
                    command_id=c, request_hash=""),
                {}, command_id="au3")


# ================================================================ 8.9 乱序与重放

class TestEventOrdering:
    def test_t04_32_watermark_never_regresses(self):
        h = Harness()
        first = h.service.apply_manifest_committed(
            "t1", manifest_event(event_id="e10", sequence=10, refs=[]))
        assert first["applied"] is True
        older = h.service.apply_manifest_committed(
            "t1", manifest_event(event_id="e5", sequence=5, refs=[]))
        assert older == {"applied": False, "reason": "out_of_order",
                         "sequence": 5}
        assert h.one("projection_watermark", tenant_id="t1",
                     source="S08")["sequence"] == 10

    def test_out_of_order_event_does_not_add_references(self):
        h = Harness()
        h.service.apply_manifest_committed(
            "t1", manifest_event(sequence=10, refs=[]))
        h.service.apply_manifest_committed(
            "t1", manifest_event(event_id="e5", sequence=5,
                                 refs=[s04_ref("scr_1", 1)]))
        assert h.count("script_reference_projection") == 0

    def test_projection_ignores_foreign_service_refs(self):
        h = Harness()
        result = h.service.apply_manifest_committed("t1", manifest_event(
            sequence=10, refs=[
                {"service_id": "S06", "id": "shot_1", "revision": 2},
                s04_ref("scr_1", 1)]))
        assert result["references"] == 1
        assert h.count("script_reference_projection") == 1

    def test_t04_33_replaying_s04_topic_produces_nothing(self):
        # S04 不消费自己的 topic：回放（side_effects=false）不产生新 outbox。
        h = Harness()
        h.sealed(command_id_prefix="av")
        before = h.count("outbox")
        result = h.service.consume_manifest_event(
            "s04.replay", manifest_event(
                event_id="own_1", event_type=EVENT_REVISION_CREATED))
        assert result == {"consumed": False, "reason": "unrouted"}
        assert h.count("outbox") == before
        assert h.count("inbox") == 0

    def test_t04_34_replay_does_not_block_drafts(self):
        h = Harness()
        h.service.consume_manifest_event(
            "s04.replay", manifest_event(event_id="own_2",
                                        event_type="narrative.unknown"))
        script = h.create(command_id="aw1")
        rev = h.append(script["script_id"], command_id="aw2")
        assert rev["revision"] == 1

    def test_duplicate_event_is_consumed_once(self):
        h = Harness()
        envelope = manifest_event(event_id="evt_dup", sequence=1,
                                  refs=[s04_ref("scr_1", 1)])
        first = h.service.consume_manifest_event("s04.events", envelope)
        second = h.service.consume_manifest_event("s04.events", envelope)
        assert first["consumed"] is True
        assert second == {"consumed": False, "reason": "duplicate"}
        assert h.count("script_reference_projection") == 1

    def test_event_without_id_is_schema_invalid(self):
        h = Harness()
        envelope = manifest_event(event_id="evt_x")
        envelope.pop("id")
        with raises(422, "SCHEMA_INVALID"):
            h.service.consume_manifest_event("s04.events", envelope)
        assert h.count("inbox") == 0

    def test_event_without_tenant_is_schema_invalid(self):
        h = Harness()
        envelope = manifest_event()
        envelope.pop("tenant_id")
        with raises(422, "SCHEMA_INVALID"):
            h.service.consume_manifest_event("s04.events", envelope)

    def test_manifest_envelope_needs_ref_and_fence(self):
        h = Harness()
        with raises(422, "INVALID_BODY"):
            h.service.apply_manifest_committed("t1", {
                "id": "e1", "sequence": 1,
                "data": {"source_revisions": []}})
        with raises(422, "INVALID_BODY"):
            h.service.apply_manifest_committed("t1", {
                "id": "e2", "sequence": 1,
                "data": {"manifest_ref": {"id": "man_1", "revision": 1},
                         "source_revisions": []}})

    def test_reference_health_is_stale_before_any_event(self):
        h = Harness()
        health = h.service.reference_health("t1")
        assert health["fresh"] is False
        assert health["sequence"] is None
        h.service.apply_manifest_committed("t1", manifest_event(sequence=1))
        health = h.service.reference_health("t1")
        assert health["fresh"] is True
        assert health["sequence"] == 1


# ================================================================ 8.10 对白一致性

class TestDialogueConsistency:
    def test_t04_35_line_text_is_byte_identical_across_endpoints(self):
        h = Harness()
        script_id, revision = h.sealed(command_id_prefix="ax")
        line_id = h.first_line_id(script_id)
        listed = h.service.list_revision_lines("t1", script_id, revision)
        item = listed["lines"][0]
        single = h.service.get_line_revision("t1", line_id,
                                             item["line_revision"])
        batch = h.service.batch_lines("t1", [line_id])["lines"][0]
        assert item["text"].encode("utf-8") == single["text"].encode("utf-8")
        assert batch["text"].encode("utf-8") == single["text"].encode("utf-8")
        assert single["text_hash"] == text_digest(single["text"])

    def test_t04_36_divergence_is_detectable_from_hash(self):
        # S06 侧的拒绝不在本服务；S04 的责任是给出可字节比对的原串与 hash。
        h = Harness()
        script_id, revision = h.sealed(command_id_prefix="ay")
        line_id = h.first_line_id(script_id)
        source = h.service.get_line_revision("t1", line_id, 1)
        divergent = source["text"] + "（改）"
        assert text_digest(divergent) != source["text_hash"]

    def test_t04_37_old_hash_survives_and_old_version_readable(self):
        h = Harness()
        script_id, revision = h.sealed(command_id_prefix="az")
        old = h.service.get_revision("t1", script_id, revision)
        h.edit_line(h.first_line_id(script_id), "新台词", command_id="az9")
        still = h.service.get_revision("t1", script_id, revision)
        # 已提交 manifest 引用的旧 hash 依旧成立。
        assert still["content_hash"] == old["content_hash"]
        assert h.service.get_body("t1", script_id, revision)["body"] is not None
        # 新 line revision 有新的 text_hash，S09 的边可以指向它。
        new = h.one("line_revision", line_id=h.first_line_id(script_id),
                    revision=2)
        assert new["text_hash"] != h.one(
            "line_revision", line_id=h.first_line_id(script_id),
            revision=1)["text_hash"]


# ================================================================ 3.x 状态机

class TestRevisionStateMachine:
    def test_all_four_states_are_reachable(self):
        h = Harness()
        script = h.create(command_id="ba1")
        # DRAFT → WITHDRAWN
        d = h.append(script["script_id"], command_id="ba2", body="草稿")
        h.withdraw(script["script_id"], d["revision"], command_id="ba3")
        assert h.state_of(script["script_id"], d["revision"]) == "WITHDRAWN"
        # DRAFT → SEALED
        s = h.append(script["script_id"], command_id="ba4", body="第一版",
                     lines=[line("一")])
        h.seal(script["script_id"], s["revision"], command_id="ba5")
        assert h.state_of(script["script_id"], s["revision"]) == "SEALED"
        # SEALED → SUPERSEDED
        t = h.append(script["script_id"], command_id="ba6", body="第二版",
                     lines=[line("二")])
        h.seal(script["script_id"], t["revision"], command_id="ba7")
        assert h.state_of(script["script_id"], s["revision"]) == "SUPERSEDED"
        # SEALED → WITHDRAWN（投影新鲜、无引用）
        h.service.apply_manifest_committed("t1", manifest_event(sequence=1))
        h.withdraw(script["script_id"], t["revision"], command_id="ba8")
        assert h.state_of(script["script_id"], t["revision"]) == "WITHDRAWN"
        assert {row["state"] for row in h.rows("script_revision")} == {
            "WITHDRAWN", "SUPERSEDED"}

    def test_sealed_at_is_only_on_sealed(self):
        h = Harness()
        script = h.create(command_id="bb1")
        rev = h.append(script["script_id"], command_id="bb2")
        assert h.one("script_revision")["sealed_at"] is None
        h.seal(script["script_id"], rev["revision"], command_id="bb3")
        assert h.one("script_revision")["sealed_at"] is not None

    def test_only_head_can_be_sealed(self):
        h = Harness()
        script = h.create(command_id="bc1")
        first = h.append(script["script_id"], command_id="bc2", body="一",
                         lines=[line("a")])
        h.append(script["script_id"], command_id="bc3", body="二",
                 lines=[line("b")])
        with raises(409, "NOT_HEAD_REVISION") as info:
            h.seal(script["script_id"], first["revision"], command_id="bc4")
        assert info.value.details["head_revision"] == 2

    def test_sealing_second_revision_supersedes_first(self):
        h = Harness()
        script = h.create(command_id="bd1")
        first = h.append(script["script_id"], command_id="bd2", lines=[line("a")])
        h.seal(script["script_id"], first["revision"], command_id="bd3")
        second = h.append(script["script_id"], command_id="bd4",
                          lines=[line("b")])
        result = h.seal(script["script_id"], second["revision"],
                        command_id="bd5")
        assert result["superseded_revisions"] == [first["revision"]]
        assert h.one("script")["sealed_revision"] == second["revision"]

    def test_empty_line_set_cannot_be_sealed(self):
        h = Harness()
        script = h.create(command_id="be1")
        rev = h.append(script["script_id"], command_id="be2", lines=[])
        with raises(409, "ILLEGAL_STATE_TRANSITION") as info:
            h.seal(script["script_id"], rev["revision"], command_id="be3")
        # 空行集合 seal 成功会让 S05 切出 0 场并"成功"产出空集。
        assert info.value.details["line_count"] == 0

    def test_line_set_hash_mismatch_refuses_to_seal(self):
        h = Harness()
        script = h.create(command_id="bf1")
        rev = h.append(script["script_id"], command_id="bf2")
        # 模拟"提交后有人动过快照"：直接改库里的 text_hash。
        h.conn.execute(
            "UPDATE script_revision_line SET text_hash=? WHERE script_id=?",
            ("f" * 64, script["script_id"]))
        with raises(500, "LINE_SET_HASH_MISMATCH"):
            h.seal(script["script_id"], rev["revision"], command_id="bf3")
        assert h.state_of(script["script_id"], rev["revision"]) == "DRAFT"

    def test_superseded_and_withdrawn_are_terminal(self):
        h = Harness()
        script = h.create(command_id="bg1")
        first = h.append(script["script_id"], command_id="bg2", body="一",
                         lines=[line("a")])
        h.seal(script["script_id"], first["revision"], command_id="bg3")
        second = h.append(script["script_id"], command_id="bg4", body="二",
                          lines=[line("b")])
        h.seal(script["script_id"], second["revision"], command_id="bg5")
        # SUPERSEDED 是终态：既不能再 seal（409），也不能再撤回。
        with raises(409, "ILLEGAL_STATE_TRANSITION"):
            h.withdraw(script["script_id"], first["revision"],
                       command_id="bg6")
        assert h.state_of(script["script_id"], first["revision"]) == \
            "SUPERSEDED"
        # 撤回当前 SEALED 后同样不能回头。
        h.service.apply_manifest_committed("t1", manifest_event(sequence=1))
        h.withdraw(script["script_id"], second["revision"], command_id="bg7")
        with raises(409, "ILLEGAL_STATE_TRANSITION"):
            h.seal(script["script_id"], second["revision"], command_id="bg8")
        assert h.state_of(script["script_id"], second["revision"]) == \
            "WITHDRAWN"

    def test_withdraw_clears_sealed_pointer(self):
        h = Harness()
        script_id, revision = h.sealed(command_id_prefix="bh")
        assert h.one("script")["sealed_revision"] == revision
        h.service.apply_manifest_committed("t1", manifest_event(sequence=1))
        h.withdraw(script_id, revision, command_id="bh9")
        assert h.one("script")["sealed_revision"] is None


class TestStoryArcStateMachine:
    def _arc(self, h, command_id="ca1", **over):
        body = {"series_id": "ser_1",
                "produced_by": {"task_id": "task_1"},
                "body": {"acts": [{"index": 1, "summary": "开端"}],
                         "turning_points": [],
                         "foreshadow_plan": []}}
        body.update(over)
        return h.command(
            lambda t, c, b: h.service.submit_story_arc(
                t, b, command_id=c, request_hash=""),
            body, command_id=command_id)

    def _decide(self, h, proposal_id, revision, decision, command_id):
        return h.command(
            lambda t, c, b: h.service.decide_story_arc(
                t, proposal_id, revision, b, command_id=c, request_hash=""),
            {"decision": decision}, command_id=command_id)

    def test_proposed_to_adopted(self):
        h = Harness()
        arc = self._arc(h)
        assert arc["state"] == "PROPOSED"
        decided = self._decide(h, arc["proposal_id"], 1, "adopt", "ca2")
        assert decided["state"] == "ADOPTED"
        assert decided["revision"] == 2
        # 追加新行，旧行不动（G8）。
        assert h.count("story_arc_proposal") == 2
        assert h.one("story_arc_proposal", revision=1)["state"] == "PROPOSED"

    def test_proposed_to_rejected_and_superseded(self):
        h = Harness()
        for index, decision in enumerate(("reject", "supersede")):
            arc = self._arc(h, command_id=f"cb{index}a")
            decided = self._decide(h, arc["proposal_id"], 1, decision,
                                  f"cb{index}b")
            assert decided["state"] == {
                "reject": "REJECTED", "supersede": "SUPERSEDED"}[decision]

    def test_same_state_decision_is_idempotent(self):
        h = Harness()
        arc = self._arc(h, command_id="cc1")
        first = self._decide(h, arc["proposal_id"], 1, "adopt", "cc2")
        second = self._decide(h, arc["proposal_id"], first["revision"],
                              "adopt", "cc3")
        assert second["reused"] is True
        assert second["state"] == "ADOPTED"
        assert h.count("story_arc_proposal") == 2

    def test_rejected_is_terminal(self):
        h = Harness()
        arc = self._arc(h, command_id="cd1")
        rejected = self._decide(h, arc["proposal_id"], 1, "reject", "cd2")
        with raises(409, "ILLEGAL_STATE_TRANSITION") as info:
            self._decide(h, arc["proposal_id"], rejected["revision"],
                         "adopt", "cd3")
        assert info.value.details == {"state": "REJECTED",
                                      "target": "ADOPTED"}

    def test_superseded_is_terminal(self):
        h = Harness()
        arc = self._arc(h, command_id="ce1")
        h._ = None
        done = self._decide(h, arc["proposal_id"], 1, "supersede", "ce2")
        with raises(409, "ILLEGAL_STATE_TRANSITION"):
            self._decide(h, arc["proposal_id"], done["revision"], "adopt",
                         "ce3")

    def test_adopted_can_be_superseded(self):
        h = Harness()
        arc = self._arc(h, command_id="cf1")
        adopted = self._decide(h, arc["proposal_id"], 1, "adopt", "cf2")
        final = self._decide(h, arc["proposal_id"], adopted["revision"],
                             "supersede", "cf3")
        assert final["state"] == "SUPERSEDED"

    def test_deciding_old_revision_is_conflict(self):
        h = Harness()
        arc = self._arc(h, command_id="cg1")
        self._decide(h, arc["proposal_id"], 1, "adopt", "cg2")
        with raises(409, "REVISION_CONFLICT"):
            self._decide(h, arc["proposal_id"], 1, "supersede", "cg3")

    def test_multiple_adopted_arcs_per_series_are_allowed(self):
        # ADOPTED 只影响 UI 与 S18 pack，不是一剧一个的唯一事实。
        h = Harness()
        first = self._arc(h, command_id="ch1")
        second = self._arc(h, command_id="ch2")
        self._decide(h, first["proposal_id"], 1, "adopt", "ch3")
        self._decide(h, second["proposal_id"], 1, "adopt", "ch4")
        adopted = h.rows("story_arc_proposal", state="ADOPTED")
        assert len(adopted) == 2

    def test_arc_requires_produced_by(self):
        h = Harness()
        with raises(400, "INVALID_BODY"):
            h.command(
                lambda t, c, b: h.service.submit_story_arc(
                    t, b, command_id=c, request_hash=""),
                {"series_id": "ser_1", "body": {"acts": []}},
                command_id="ci1")

    def test_arc_rejects_story_time_like_keys(self):
        h = Harness()
        with raises(400, "INVALID_BODY"):
            self._arc(h, command_id="cj1", body={"acts": [],
                                                "timeline": [1998, 2001]})

    def test_confidence_out_of_range_is_rejected(self):
        h = Harness()
        with raises(400, "INVALID_BODY"):
            self._arc(h, command_id="ck1", confidence=1.5)

    def test_unresolved_must_be_strings(self):
        h = Harness()
        with raises(400, "INVALID_BODY"):
            self._arc(h, command_id="cl1", unresolved=[{"q": "谁杀了谁"}])

    def test_arc_carries_no_accepted_flag(self):
        h = Harness()
        arc = self._arc(h, command_id="cm1")
        got = h.service.get_story_arc("t1", arc["proposal_id"])
        assert "accepted" not in got
        assert "canon" not in got
        assert got["x-contract-gaps"] == CONTRACT_GAPS


class TestBrief:
    def test_brief_container_and_revisions(self):
        h = Harness()
        brief = h.command(
            lambda t, c, b: h.service.create_brief(
                t, b, command_id=c, request_hash=""),
            {"series_id": "ser_1"}, command_id="da1")
        assert brief["head_revision"] == 0
        rev = h.command(
            lambda t, c, b: h.service.append_brief_revision(
                t, brief["brief_id"], b, command_id=c, request_hash=""),
            {"body": {"logline": "一个女人回到 1998。", "genre": "悬疑"}},
            command_id="da2")
        assert rev["revision"] == 1
        got = h.service.get_brief("t1", brief["brief_id"])
        assert got["head_revision"] == 1
        assert got["revision"]["body"]["logline"] == "一个女人回到 1998。"

    def test_second_brief_for_same_series_is_409_with_existing_id(self):
        h = Harness()
        first = h.command(
            lambda t, c, b: h.service.create_brief(
                t, b, command_id=c, request_hash=""),
            {"series_id": "ser_1"}, command_id="db1")
        with raises(409, "ILLEGAL_STATE_TRANSITION") as info:
            h.command(
                lambda t, c, b: h.service.create_brief(
                    t, b, command_id=c, request_hash=""),
                {"series_id": "ser_1"}, command_id="db2")
        assert info.value.details["brief_id"] == first["brief_id"]

    def test_brief_unknown_key_is_rejected(self):
        h = Harness()
        brief = h.command(
            lambda t, c, b: h.service.create_brief(
                t, b, command_id=c, request_hash=""),
            {"series_id": "ser_1"}, command_id="dc1")
        with raises(400, "INVALID_BODY"):
            h.command(
                lambda t, c, b: h.service.append_brief_revision(
                    t, brief["brief_id"], b, command_id=c, request_hash=""),
                {"body": {"logline": "x", "secret": "y"}}, command_id="dc2")

    def test_brief_revision_dedupes_by_content(self):
        h = Harness()
        brief = h.command(
            lambda t, c, b: h.service.create_brief(
                t, b, command_id=c, request_hash=""),
            {"series_id": "ser_1"}, command_id="dd1")
        for command_id in ("dd2", "dd3"):
            result = h.command(
                lambda t, c, b: h.service.append_brief_revision(
                    t, brief["brief_id"], b, command_id=c, request_hash=""),
                {"body": {"logline": "同一份"}}, command_id=command_id)
        assert result["deduplicated"] is True
        assert h.count("brief_revision") == 1

    def test_brief_cross_tenant_is_404(self):
        h = Harness()
        brief = h.command(
            lambda t, c, b: h.service.create_brief(
                t, b, command_id=c, request_hash=""),
            {"series_id": "ser_1"}, command_id="de1")
        with raises(404, "BRIEF_NOT_FOUND"):
            h.service.get_brief("t2", brief["brief_id"])


# ================================================================ 查询与分页

class TestQueries:
    def test_lines_are_ordered_by_seq_not_id(self):
        h = Harness()
        script = h.create(command_id="ea1")
        texts = ["第一句", "第二句", "第三句", "第四句"]
        rev = h.append(script["script_id"], command_id="ea2",
                       lines=[line(text) for text in texts])
        page = h.service.list_revision_lines("t1", script["script_id"],
                                             rev["revision"])
        assert [item["text"] for item in page["lines"]] == texts
        assert [item["seq"] for item in page["lines"]] == [1, 2, 3, 4]

    def test_cursor_pagination_walks_in_seq_order(self):
        h = Harness()
        script = h.create(command_id="eb1")
        rev = h.append(script["script_id"], command_id="eb2",
                       lines=[line(f"第{i}句") for i in range(1, 6)])
        seen = []
        cursor = None
        while True:
            page = h.service.list_revision_lines(
                "t1", script["script_id"], rev["revision"],
                after_line_id=cursor, limit=2)
            seen.extend(item["text"] for item in page["lines"])
            cursor = page["next_after_line_id"]
            if cursor is None:
                break
        assert seen == [f"第{i}句" for i in range(1, 6)]

    def test_foreign_cursor_is_404(self):
        h = Harness()
        script = h.create(command_id="ec1")
        rev = h.append(script["script_id"], command_id="ec2")
        with raises(404, "LINE_NOT_FOUND"):
            h.service.list_revision_lines("t1", script["script_id"],
                                          rev["revision"],
                                          after_line_id="ln_missing")

    def test_limit_is_clamped_not_trusted(self):
        h = Harness()
        script = h.create(command_id="ed1")
        rev = h.append(script["script_id"], command_id="ed2",
                       lines=[line(f"第{i}句") for i in range(1, 4)])
        assert len(h.service.list_revision_lines(
            "t1", script["script_id"], rev["revision"], limit=0)["lines"]) == 1
        assert len(h.service.list_revision_lines(
            "t1", script["script_id"], rev["revision"],
            limit=10_000)["lines"]) == 3

    def test_batch_lines_reports_missing_separately(self):
        h = Harness()
        script = h.create(command_id="ee1")
        h.append(script["script_id"], command_id="ee2",
                 lines=[line("甲"), line("乙")])
        ids = [row["line_id"] for row in h.rows("line")]
        result = h.service.batch_lines("t1", [*ids, "ln_absent"])
        assert len(result["lines"]) == 2
        assert result["missing"] == ["ln_absent"]
        # 返回顺序跟请求顺序，不跟 id 字典序。
        reordered = h.service.batch_lines("t1", list(reversed(ids)))
        assert [item["line_id"] for item in reordered["lines"]] == \
            list(reversed(ids))

    def test_batch_lines_rejects_over_limit(self):
        h = Harness()
        ids = [f"ln_{index}" for index in range(MAX_BATCH_IDS + 1)]
        with raises(422, "TOO_MANY_IDS"):
            h.service.batch_lines("t1", ids)

    def test_batch_lines_rejects_empty(self):
        h = Harness()
        with raises(400, "INVALID_BODY"):
            h.service.batch_lines("t1", [])

    def test_list_scripts_filters(self):
        h = Harness()
        h.create(command_id="ef1", episode_id="ep_1")
        h.create(command_id="ef2", episode_id="ep_2")
        assert len(h.service.list_scripts("t1")["scripts"]) == 2
        assert len(h.service.list_scripts("t1", episode_id="ep_1")[
            "scripts"]) == 1
        assert len(h.service.list_scripts("t1", series_id="ser_x")[
            "scripts"]) == 0

    def test_list_scripts_rejects_unknown_state(self):
        # 没有 APPROVED / ACCEPTED 这种"状态"，传了就是 400。
        h = Harness()
        for state in ("APPROVED", "ACCEPTED", "accepted"):
            with raises(400, "INVALID_BODY"):
                h.service.list_scripts("t1", state=state)

    def test_list_scripts_by_state_uses_revision_state(self):
        h = Harness()
        script_id, _ = h.sealed(command_id_prefix="eg")
        h.create(command_id="eg9", episode_id="ep_2")
        sealed = h.service.list_scripts("t1", state="SEALED")["scripts"]
        assert [item["script_id"] for item in sealed] == [script_id]

    def test_get_body_missing_chunk_is_404(self):
        h = Harness()
        script = h.create(command_id="eh1")
        rev = h.append(script["script_id"], command_id="eh2")
        with raises(404, "CHUNK_NOT_FOUND"):
            h.service.get_body("t1", script["script_id"], rev["revision"],
                               chunk=99)

    def test_body_roundtrip_is_byte_exact(self):
        h = Harness()
        script = h.create(command_id="ei1")
        text = "第一场\n「你终于来了。」\n\n—— 陈默"
        rev = h.append(script["script_id"], command_id="ei2", body=text)
        got = h.service.get_body("t1", script["script_id"], rev["revision"])
        assert got["body"] == text
        assert got["body_unavailable"] is False

    def test_body_read_failure_is_unavailable_not_empty(self):
        # 反模式 18：读失败返回空串会让 S05 切出 0 场并"成功"。
        h = Harness()
        script = h.create(command_id="ej1")
        rev = h.append(script["script_id"], command_id="ej2", body="正文")
        for key in h.body_store.list_prefix("s04/t1/"):
            h.body_store.delete(key)
        got = h.service.get_body("t1", script["script_id"], rev["revision"])
        assert got["body"] is None
        assert got["body_unavailable"] is True
        assert got["chunks"]

    def test_body_checksum_mismatch_is_unavailable(self):
        h = Harness()
        script = h.create(command_id="ek1")
        rev = h.append(script["script_id"], command_id="ek2", body="正文")
        key = h.one("body_chunk")["bytes_ref"]
        h.body_store.put(key, "不是原文".encode("utf-8"))
        got = h.service.get_body("t1", script["script_id"], rev["revision"])
        assert got["body_unavailable"] is True
        assert got["body"] is None

    def test_body_over_limit_is_413(self):
        h = Harness()
        script = h.create(command_id="el1")
        oversized = "字" * (MAX_BODY_BYTES // 3 + 10)
        with raises(413, "BODY_TOO_LARGE") as info:
            h.append(script["script_id"], command_id="el2", body=oversized)
        assert info.value.details["limit"] == MAX_BODY_BYTES
        assert h.count("script_revision") == 0

    def test_body_chunks_are_capped(self):
        h = Harness()
        script = h.create(command_id="em1")
        # 每块 1 MiB：2.5 MiB 的正文应分成 3 块。
        text = "a" * (MAX_CHUNK_BYTES * 2 + 1024)
        rev = h.append(script["script_id"], command_id="em2", body=text)
        chunks = h.rows("body_chunk", revision=rev["revision"])
        assert len(chunks) == 3
        assert all(chunk["byte_size"] <= MAX_CHUNK_BYTES for chunk in chunks)
        assert h.service.get_body("t1", script["script_id"],
                                  rev["revision"])["body"] == text


class TestLines:
    def test_new_line_without_speaker_is_422(self):
        h = Harness()
        script = h.create(command_id="fa1")
        with raises(422, "SPEAKER_REQUIRED"):
            h.append(script["script_id"], command_id="fa2",
                     lines=[{"text": "没有说话人"}])

    def test_duplicate_line_id_in_one_revision_is_422(self):
        h = Harness()
        script = h.create(command_id="fb1")
        h.append(script["script_id"], command_id="fb2")
        line_id = h.first_line_id(script["script_id"])
        text = h.one("line_revision", line_id=line_id)["text"]
        # 第二次出现时先撞上"重复"判定；文本保持一致，避免先被
        # LINE_MUTATION_IN_DRAFT 拦下（那是另一个错误码）。
        with raises(422, "DUPLICATE_LINE_IN_REVISION"):
            h.append(script["script_id"], command_id="fb3",
                     lines=[{"line_id": line_id, "text": text},
                            {"line_id": line_id, "text": text}])
        assert h.count("script_revision") == 1

    def test_in_place_text_edit_in_draft_is_422(self):
        h = Harness()
        script_id, _ = h.sealed(command_id_prefix="fc")
        line_id = h.first_line_id(script_id)
        with raises(422, "LINE_MUTATION_IN_DRAFT") as info:
            h.append(script_id, command_id="fc9",
                     lines=[{"line_id": line_id, "text": "偷偷改字"}])
        assert info.value.details["fields"] == ["text"]
        assert info.value.details["required_endpoint"].endswith(
            f"/lines/{line_id}/revisions")

    def test_non_text_field_edit_in_draft_is_also_422(self):
        # S04-G9：speaker/parenthetical 变更同样会让编号混乱。
        h = Harness()
        script_id, _ = h.sealed(command_id_prefix="fd")
        line_id = h.first_line_id(script_id)
        text = h.one("line_revision", line_id=line_id)["text"]
        with raises(422, "LINE_MUTATION_IN_DRAFT") as info:
            h.append(script_id, command_id="fd9",
                     lines=[{"line_id": line_id, "text": text,
                             "parenthetical": "（新增）"}])
        assert "parenthetical" in info.value.details["fields"]

    def test_omitted_field_is_not_read_as_cleared(self):
        # 只带 text 且 text 未变 → 不算变更（缺省不等于置空）。
        h = Harness()
        script_id, _ = h.sealed(command_id_prefix="fe")
        line_id = h.first_line_id(script_id)
        text = h.one("line_revision", line_id=line_id)["text"]
        rev2 = h.append(script_id, command_id="fe9",
                        lines=[{"line_id": line_id, "text": text}])
        assert rev2["revision"] == 2

    def test_line_from_another_script_is_422(self):
        h = Harness()
        first = h.create(command_id="ff1", episode_id="ep_1")
        second = h.create(command_id="ff2", episode_id="ep_2")
        h.append(first["script_id"], command_id="ff3")
        foreign_line = h.first_line_id(first["script_id"])
        with raises(422, "LINE_NOT_FOUND"):
            h.append(second["script_id"], command_id="ff4",
                     lines=[{"line_id": foreign_line, "text": "借用"}])

    def test_retired_line_cannot_return_in_new_revision(self):
        h = Harness()
        script_id, _ = h.sealed(command_id_prefix="fg")
        line_id = h.first_line_id(script_id)
        # fg1..fg3 已被 sealed() 的建/写/seal 用掉，不能复用。
        h.retire(line_id, command_id="fg4")
        text = h.one("line_revision", line_id=line_id)["text"]
        with raises(409, "ILLEGAL_STATE_TRANSITION"):
            h.append(script_id, command_id="fg5",
                     lines=[{"line_id": line_id, "text": text}])

    def test_retire_is_idempotent(self):
        h = Harness()
        script = h.create(command_id="fh1")
        h.append(script["script_id"], command_id="fh2")
        line_id = h.first_line_id(script["script_id"])
        first = h.retire(line_id, command_id="fh3")
        second = h.retire(line_id, command_id="fh4")
        assert first["already_retired"] is False
        assert second["already_retired"] is True
        assert second["retired_at"] == first["retired_at"]

    def test_retired_line_with_new_text_is_refused(self):
        h = Harness()
        script = h.create(command_id="fi1")
        h.append(script["script_id"], command_id="fi2")
        line_id = h.first_line_id(script["script_id"])
        h.retire(line_id, command_id="fi3")
        with raises(409, "ILLEGAL_STATE_TRANSITION"):
            h.edit_line(line_id, "退休后改字", command_id="fi4")
        assert h.count("line_revision") == 1

    def test_line_revision_does_not_touch_script_revision(self):
        h = Harness()
        script_id, revision = h.sealed(command_id_prefix="fj")
        before = dict(h.one("script_revision"))
        h.edit_line(h.first_line_id(script_id), "新台词", command_id="fj9")
        after = dict(h.one("script_revision"))
        for key in ("content_hash", "line_set_hash", "structure", "state",
                    "sealed_at", "body_ref", "body_bytes"):
            assert after[key] == before[key], key

    def test_line_revision_writes_local_event_not_outbox(self):
        h = Harness()
        script_id, _ = h.sealed(command_id_prefix="fk")
        outbox_before = h.count("outbox")
        h.edit_line(h.first_line_id(script_id), "新台词", command_id="fk9")
        assert h.count("outbox") == outbox_before
        event = h.local_events()[0]
        assert event["prev_text_hash"] is not None
        assert event["text_hash"] != event["prev_text_hash"]

    def test_get_line_revision_unknown_revision_is_404(self):
        h = Harness()
        script = h.create(command_id="fl1")
        h.append(script["script_id"], command_id="fl2")
        line_id = h.first_line_id(script["script_id"])
        with raises(404, "LINE_NOT_FOUND"):
            h.service.get_line_revision("t1", line_id, 99)


# ================================================================ 维护

class TestMaintenance:
    def test_sweep_stale_commands_only_removes_old_in_progress(self):
        h = Harness()
        digest = canonical_request_hash({"command_id": "ga2"})
        h.idem.begin("t1", "ga2", digest)
        h.idem.begin("t1", "ga3", canonical_request_hash({"command_id": "ga3"}))
        h.idem.finish("t1", "ga3", json.dumps({"ok": True}))
        h.conn.execute(
            "UPDATE idempotency_record SET created_at='2000-01-01T00:00:00.000Z'"
            " WHERE command_id='ga2'")
        removed = h.service.sweep_stale_commands(older_than_seconds=300)
        assert removed == 1
        remaining = {row["command_id"] for row in h.rows("idempotency_record")}
        assert remaining == {"ga3"}

    def test_sweep_leaves_fresh_in_progress_alone(self):
        h = Harness()
        h.idem.begin("t1", "gb1", canonical_request_hash({"command_id": "gb1"}))
        assert h.service.sweep_stale_commands() == 0

    def test_orphan_sweep_without_list_prefix_returns_zero(self):
        class NoLister:
            def put(self, key, payload):
                pass

            def get(self, key):
                return None

            def delete(self, key):
                pass

        h = Harness(body_store=NoLister())
        # 不能假装扫过了。
        assert h.service.collect_orphan_chunks("t1") == 0

    def test_narrative_unavailable_when_db_is_read_only(self):
        # §5.4：主库不可写 → 503 retryable，不是 500。
        # 直接调内核：harness 的 command() 会先写幂等占位，那一写在内核事务
        # 之前；HTTP 层同一位置的映射由 TestHttpLayer 单独覆盖。
        h = Harness()
        h.conn.execute("PRAGMA query_only=ON")
        try:
            with raises(503, "NARRATIVE_UNAVAILABLE") as info:
                h.service.create_script("t1", episode_body(),
                                        command_id="gc1", request_hash="")
            assert info.value.retryable is True
        finally:
            h.conn.execute("PRAGMA query_only=OFF")

    def test_missing_body_store_is_503(self):
        h = Harness(body_store=None)
        script = h.create(command_id="gd1")
        with raises(503, "BODY_STORE_UNAVAILABLE"):
            h.append(script["script_id"], command_id="gd2")
        assert h.count("script_revision") == 0

    def test_build_body_store_has_no_default(self):
        for value in (None, "", "s3", "gcs"):
            with raises(503, "BODY_STORE_UNAVAILABLE"):
                build_body_store(value)
        assert isinstance(build_body_store("memory"), InMemoryBodyStore)

    def test_build_upstream_sources_fail_closed(self):
        with raises(503, "CATALOG_UNAVAILABLE"):
            build_catalog_source(None)("t1", "ep_1")
        with raises(503, "AGENT_TASK_SOURCE_UNAVAILABLE"):
            build_task_source("")("t1", "task_1")


# ================================================================ HTTP 层

def _http_client(monkeypatch, tmp_path, **over):
    monkeypatch.setenv("S04_DB", str(tmp_path / "s04.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s04_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s04_api_app"] = module
    harness = Harness(conn=module._conn, **over)
    module.service = harness.service
    module._idem = harness.idem
    # HTTP 层只接 `command_id` 与 `request_hash`，和 harness 的 wrapper 同形。
    return TestClient(module.app), harness


def _headers(command_id="c1", tenant="t1", **extra):
    # 生产调用方带签名身份；`author` 由 `_principal` 从 header 派生，body 不必
    # 自报（S04-G13）。不带头时按方案 §4.1 第 2 步只能是 400。
    out = {"Idempotency-Key": command_id, "X-Tenant-Id": tenant,
           "X-Principal-Id": "u_human", "X-Principal-Type": "human"}
    out.update(extra)
    return out


class TestHttpLayer:
    def test_roundtrip_create_append_seal_read(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/scripts", headers=_headers("h1"),
                              json={"series_id": "ser_1",
                                    "script_kind": "episode_script",
                                    "episode_id": "ep_1"})
        assert created.status_code == 201, created.text
        script_id = created.json()["script_id"]
        appended = client.post(
            f"/scripts/{script_id}/revisions", headers=_headers("h2"),
            json={"body": "正文", "lines": [line("台词")]})
        assert appended.status_code == 201, appended.text
        revision = appended.json()["revision"]
        sealed = client.post(f"/scripts/{script_id}/revisions/{revision}:seal",
                             headers=_headers("h3"), json={})
        assert sealed.status_code == 200, sealed.text
        assert sealed.json()["state"] == "SEALED"
        got = client.get(f"/scripts/{script_id}/revisions/{revision}",
                         headers={"X-Tenant-Id": "t1"})
        assert got.status_code == 200
        assert got.json()["content_hash"] == sealed.json()["content_hash"]
        assert client.get("/health/readiness").json()["ready"] is True

    def test_missing_tenant_header_is_400(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        response = client.post("/scripts", headers={"Idempotency-Key": "i1"},
                               json={"series_id": "ser_1",
                                    "script_kind": "episode_script",
                                    "episode_id": "ep_1"})
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_BODY"

    def test_missing_idempotency_key_is_400(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        response = client.post("/scripts", headers={"X-Tenant-Id": "t1"},
                               json={"series_id": "ser_1",
                                    "script_kind": "episode_script",
                                    "episode_id": "ep_1"})
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_BODY"

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        response = client.post("/scripts", headers=_headers("i2"),
                               json={"series_id": "ser_1",
                                     "script_kind": "episode_script",
                                     "episode_id": "ep_1",
                                     "tenant_id": "t_other"})
        assert response.status_code == 403
        assert response.json()["code"] == "TENANT_MISMATCH"
        assert h.count("script") == 0

    def test_body_command_id_disagreeing_header_is_400(self, monkeypatch,
                                                      tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        response = client.post("/scripts", headers=_headers("i3"),
                               json={"series_id": "ser_1",
                                     "script_kind": "episode_script",
                                     "episode_id": "ep_1",
                                     "command_id": "other"})
        assert response.status_code == 400

    def test_same_key_different_body_is_409_over_http(self, monkeypatch,
                                                      tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/scripts", headers=_headers("j1"),
                              json={"series_id": "ser_1",
                                    "script_kind": "episode_script",
                                    "episode_id": "ep_1"})
        script_id = created.json()["script_id"]
        client.post(f"/scripts/{script_id}/revisions", headers=_headers("j2"),
                    json={"body": "第一版"})
        conflict = client.post(f"/scripts/{script_id}/revisions",
                               headers=_headers("j2"),
                               json={"body": "第二版"})
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert h.count("script_revision") == 1

    def test_replay_is_marked_replayed(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        first = client.post("/scripts", headers=_headers("k1"),
                            json={"series_id": "ser_1",
                                    "script_kind": "episode_script",
                                    "episode_id": "ep_1"})
        second = client.post("/scripts", headers=_headers("k1"),
                             json={"series_id": "ser_1",
                                    "script_kind": "episode_script",
                                    "episode_id": "ep_1"})
        assert first.json()["replayed"] is False
        assert second.json()["replayed"] is True
        assert second.json()["script_id"] == first.json()["script_id"]

    def test_cross_tenant_get_is_404(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        created = client.post("/scripts", headers=_headers("l1"),
                              json={"series_id": "ser_1",
                                    "script_kind": "episode_script",
                                    "episode_id": "ep_1"})
        script_id = created.json()["script_id"]
        response = client.get(f"/scripts/{script_id}",
                              headers={"X-Tenant-Id": "t2"})
        assert response.status_code == 404
        assert response.json()["code"] == "SCRIPT_NOT_FOUND"

    def test_get_without_tenant_is_400(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        response = client.get("/scripts")
        assert response.status_code == 400

    def test_principal_header_overrides_body_author(self, monkeypatch,
                                                    tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post(
            "/scripts", headers=_headers("m1", **{"X-Principal-Id": "u_hdr",
                                                  "X-Principal-Type": "human"}),
            json={"series_id": "ser_1", "script_kind": "episode_script",
                  "episode_id": "ep_1", "author": author("u_other")})
        assert created.status_code == 403
        assert created.json()["details"]["gap"] == "S04-G13"
        assert h.count("script") == 0

    def test_principal_header_is_used_when_body_omits_author(self, monkeypatch,
                                                             tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post(
            "/scripts", headers=_headers("m2", **{"X-Principal-Id": "u_hdr",
                                                  "X-Principal-Type": "agent"}),
            json={"series_id": "ser_1",
                                    "script_kind": "episode_script",
                                    "episode_id": "ep_1"})
        assert created.status_code == 201, created.text
        assert h.one("script")["script_id"] == created.json()["script_id"]

    def test_seal_with_agent_principal_is_403(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        created = client.post("/scripts", headers=_headers("n1"),
                              json={"series_id": "ser_1",
                                    "script_kind": "episode_script",
                                    "episode_id": "ep_1"})
        script_id = created.json()["script_id"]
        client.post(f"/scripts/{script_id}/revisions", headers=_headers("n2"),
                    json={"body": "正文", "lines": [line("台词")]})
        sealed = client.post(
            f"/scripts/{script_id}/revisions/1:seal",
            headers=_headers("n3", **{"X-Principal-Id": "u_agent",
                                      "X-Principal-Type": "agent"}), json={})
        assert sealed.status_code == 403
        assert sealed.json()["code"] == "PERMISSION_DENIED"

    def test_batch_lines_uses_comma_query(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/scripts", headers=_headers("o1"),
                              json={"series_id": "ser_1",
                                    "script_kind": "episode_script",
                                    "episode_id": "ep_1"})
        script_id = created.json()["script_id"]
        client.post(f"/scripts/{script_id}/revisions", headers=_headers("o2"),
                    json={"body": "正文", "lines": [line("甲"), line("乙")]})
        ids = [row["line_id"] for row in h.rows("line")]
        response = client.get(f"/lines:batch?ids={','.join(ids)}",
                              headers={"X-Tenant-Id": "t1"})
        assert response.status_code == 200
        assert len(response.json()["lines"]) == 2

    def test_events_consume_injects_header_tenant(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        envelope = manifest_event(event_id="p1", sequence=3,
                                  refs=[s04_ref("scr_1", 1)])
        envelope.pop("tenant_id")
        response = client.post("/events:consume", headers=_headers("q1"),
                               json=envelope)
        assert response.status_code == 200, response.text
        assert response.json()["consumed"] is True
        assert h.count("script_reference_projection") == 1

    def test_events_consume_rejects_foreign_tenant(self, monkeypatch,
                                                   tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        response = client.post("/events:consume", headers=_headers("q2"),
                               json=manifest_event(tenant="t_other"))
        assert response.status_code == 403
        assert response.json()["code"] == "TENANT_MISMATCH"
        assert h.count("inbox") == 0

    def test_error_body_has_retryable_and_details(self, monkeypatch,
                                                  tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        response = client.get("/scripts/scr_missing",
                              headers={"X-Tenant-Id": "t1"})
        assert response.status_code == 404
        body = response.json()
        assert set(body) == {"code", "message", "retryable", "correlation_id",
                             "details"}
        assert body["retryable"] is False

    def test_readiness_lists_contract_gaps(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        body = client.get("/health/readiness").json()
        assert body["contract_gaps"] == CONTRACT_GAPS

    def test_unwritable_db_surfaces_503_retryable(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        h.conn.execute("PRAGMA query_only=ON")
        try:
            response = client.post("/scripts", headers=_headers("r1"),
                                   json={"series_id": "ser_1",
                                    "script_kind": "episode_script",
                                    "episode_id": "ep_1"})
        finally:
            h.conn.execute("PRAGMA query_only=OFF")
        assert response.status_code == 503
        assert response.json()["code"] == "NARRATIVE_UNAVAILABLE"
        assert response.json()["retryable"] is True

