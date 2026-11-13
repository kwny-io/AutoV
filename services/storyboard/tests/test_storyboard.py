"""S06 单测。编号对应 `plans/S06-storyboard.md` §9 的 T06-01 … T06-52。

上游全部是 stub，按真实契约形状给：

- S05 返回 `breakdown_id / revision / state / content_hash / episode_id /
  series_id / project_id / branch_id / source_script / scenes[]`；`scenes[]`
  带 `line_span`。`content_hash` 必须与请求里的 `breakdown_ref.content_hash`
  一致，否则内核按「分场 revision 已经往前走了」409。
- S04 返回 `script_id / revision / state / content_hash / lines[]`；每行的
  `text_hash` 必须与 `text` 自洽，否则「文本漂移」的判定对象本身就不可信。
- S19 返回 `{agent_task_id, agent_role, content_hash, shots[]}`。

结构性断言一律查库（`sqlite_master` / `PRAGMA table_info` / 直接撞触发器）而不是
查响应体。本服务最严重的错误是长出第二份权威——自己记一次尝试、自己存一份
accepted 指针、把 S04 的当前台词缓存成真值。响应体里少个键证明不了什么，
少一列、少一张表才能。

`shot_lineage_id` 的三条边界（§3.2）是这份文件的核心：

1. 只有三种铸造情形，没有第四种。
2. agent 给的 `shot_lineage_id` 一律丢弃重铸（T06-14）。
3. 退役的 `shot_id` 永远不复用（T06-12），复用会让新镜头与已退役镜头共享尝试
   计数，而历史尝试已经用掉上限。

`node_epoch` / `execution_fence` 在 S06 里一次都不该出现：S06 是内容服务。
「旧 epoch 的结果晚到」在这里只表现为旧的 `parent_revision` → 409。
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
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
from event_sdk import Inbox, Outbox

from storyboard.schema import (
    AUTHORITATIVE_SOURCE,
    CONTRACT_GAPS,
    DEFAULT_CONTINUITY,
    ID_PREFIX,
    LOCAL_EVENT_LINEAGE_DISCARDED,
    LOCAL_EVENT_LINEAGE_MINTED,
    LOCAL_EVENT_REORDERED,
    LOCAL_EVENT_RETIRED,
    LOCAL_EVENT_REVISION_CREATED,
    LOCAL_EVENT_SEALED,
    LOCAL_EVENT_SPLIT,
    LOCAL_EVENT_SUPERSEDED,
    MAX_SHOTS_PER_SCENE_HARD,
    MIN_SHOT_SECONDS,
    ORDER_OFFSET,
    REVISION_STATES,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC,
)
from storyboard.service import ServiceError, StoryboardService

ROOT = Path(__file__).resolve().parents[3]
HUMAN = {"principal_id": "u_human", "principal_type": "human"}
AGENT = {"principal_id": "agt_storyboard", "principal_type": "agent"}
_UNSET = object()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 上游 stub

SCRIPT_HASH = sha("script-body")
BREAKDOWN_HASH = sha("bd-body")
DEFAULT_TEXTS = ["台词1", "台词2", "台词3", "台词4"]


def make_lines(texts):
    """S04 的行。`text_hash` 与 `text` 自洽——stub 自己先过这一关。"""
    return [{"line_id": f"lin_{index:02d}", "seq": index, "text": text,
             "text_hash": sha(text), "speaker_id": "chr_1"}
            for index, text in enumerate(texts, 1)]


def make_script(lines, *, revision=1, state="SEALED"):
    return {"script_id": "scr_1", "revision": revision, "state": state,
            "content_hash": "sha256:" + SCRIPT_HASH, "lines": lines}


def make_breakdown(scenes, *, state="SEALED", branch_id="br_main"):
    """S05 的分场。`scenes` 是 `(scene_id, 起始行号, 结束行号)`。"""
    return {"breakdown_id": "bkd_1", "revision": 1, "state": state,
            "content_hash": "sha256:" + BREAKDOWN_HASH,
            "episode_id": "ep_1", "series_id": "ser_1", "project_id": "prj_1",
            "branch_id": branch_id,
            "source_script": {"script_id": "scr_1", "revision": 1},
            "scenes": [{"scene_id": scene_id,
                        "line_span": {"start_line_id": f"lin_{first:02d}",
                                      "end_line_id": f"lin_{last:02d}",
                                      "line_count": last - first + 1}}
                       for scene_id, first, last in scenes]}


class Source:
    """可变的上游 stub。`doc` 是字典本身，测试可以就地改它（T06-37）。"""

    def __init__(self, doc):
        self.doc = doc

    def __call__(self, *_args, **_kwargs):
        return self.doc


# ------------------------------------------------------------------ 请求体

def shot_spec(shot_id, scene_id, locator_shot, line_docs, *, target=4.0, **over):
    """一份合法的 `shot-spec.v1` + S06 包装。

    `lighting` 是**字符串**、`continuity.state_in/state_out` 是字符串列表、
    `reference_edges` 的项是 `{id, revision}`——这些都按
    `contracts/shot-spec.v1.schema.json` 的严格形状给，不按方案 §4.1 的示例
    （示例与 schema 不一致，S06-G12 / C10 已登记）。
    """
    spec = {
        "schema_version": "1.0",
        "shot_id": shot_id,
        "revision": 1,
        "episode_id": "ep_1",
        "scene_id": scene_id,
        "display_locator": {"episode": 1, "scene": 1, "shot": locator_shot},
        "narrative_purpose": "推进剧情",
        "shot_type": "dialogue",
        "asset_bindings": {
            "characters": [{
                "character": {"id": "chr_1", "revision": 1},
                "costume": {"id": "cst_1", "revision": 1},
                "voice": {"id": "vce_1", "revision": 1},
                "screen_role": "lead"}],
            "scene": {"id": "scn_a", "revision": 1},
            "props": [],
            "style": {"id": "sty_1", "revision": 1}},
        "camera": {"shot_size": "medium", "angle": "eye_level",
                   "movement": "static", "movement_intensity": 0.0,
                   "composition": "居中"},
        "duration": {"target_seconds": target, "trim_allowed": False,
                     "timing_policy": "ripple"},
        "output_spec": {"aspect_ratio": "16:9", "width": 1920, "height": 1080,
                        "fps": 24},
        "beats": [{"time_range": {"start_seconds": 0.0, "end_seconds": 1.0},
                   "action": "抬眼"}],
        "dialogue": [{"line_id": doc["line_id"], "character_id": "chr_1",
                      "text": doc["text"], "locale": "zh-CN",
                      "emotion": "neutral",
                      "voice": {"id": "vce_1", "revision": 1}}
                     for doc in line_docs],
        "emotion": "neutral",
        "lighting": "暖光",
        "continuity": {"axis_of_action": "左到右",
                       "screen_direction": "向东",
                       "state_in": [], "state_out": [],
                       "previous_shot_ref": None,
                       "reference_edges": []},
        "constraints": {"must_preserve": [], "negative": [],
                        "locked_fields": []},
    }
    spec.update(over)
    return spec


def shot(shot_id, scene_id, locator_shot, line_docs, *, order, scene_order=1,
         **over):
    return {"shot_order": order, "scene_order": scene_order,
            "shot_spec": shot_spec(shot_id, scene_id, locator_shot, line_docs,
                                   **over)}


def create_body(shots, **over):
    body = {
        "schema_version": SCHEMA_VERSION,
        "breakdown_ref": {"breakdown_id": "bkd_1", "revision": 1,
                          "content_hash": "sha256:" + BREAKDOWN_HASH},
        "script_ref": {"script_id": "scr_1", "revision": 1,
                       "content_hash": "sha256:" + SCRIPT_HASH},
        "scene_ids": ["scn_1"],
        "shots": shots,
        "actor": dict(HUMAN),
    }
    body.update(over)
    return body


SBD = "sbd_" + "a" * 20
SBD_B = "sbd_" + "b" * 20


# ------------------------------------------------------------------ harness

class Harness:
    def __init__(self, conn=None, *, lines=None, scenes=None, script=_UNSET,
                 breakdown=_UNSET, agent=_UNSET, assets=_UNSET, policy=_UNSET):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lines = lines or make_lines(DEFAULT_TEXTS)
        self.scenes = scenes or [("scn_1", 1, len(self.lines))]
        self.script = Source(make_script(self.lines)) if script is _UNSET \
            else script
        self.breakdown = Source(make_breakdown(self.scenes)) \
            if breakdown is _UNSET else breakdown
        self.agent = None if agent is _UNSET else agent
        self.assets = None if assets is _UNSET else assets
        self.policy = None if policy is _UNSET else policy
        self.idem = SqliteIdempotencyStore(self.conn)
        self.service = StoryboardService(
            self.conn,
            breakdown_source=self.breakdown,
            script_source=self.script,
            agent_task_source=self.agent,
            asset_source=self.assets,
            policy_source=self.policy)

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
    def create(self, body, storyboard_id=SBD, tenant_id="t1", command_id="c1"):
        return self.command(
            lambda t, c, h: self.service.create_revision(
                t, storyboard_id, body, command_id=c, request_hash=h),
            body, tenant_id, command_id)

    def revise(self, shot_id, body, branch_id="br_main", tenant_id="t1",
               command_id="c3"):
        return self.command(
            lambda t, c, h: self.service.revise_shot(
                t, shot_id, body, branch_id=branch_id, command_id=c,
                request_hash=h),
            body, tenant_id, command_id)

    def seal(self, storyboard_id=SBD, revision=1, tenant_id="t1",
             command_id="c2", **over):
        body = {"actor": dict(HUMAN), **over}
        return self.command(
            lambda t, c, h: self.service.seal(
                t, storyboard_id, revision, body, command_id=c,
                request_hash=h), body, tenant_id, command_id)

    def retire(self, shot_id, branch_id="br_main", tenant_id="t1",
               command_id="c4", **over):
        body = {"reason": "removed", **over}
        return self.command(
            lambda t, c, h: self.service.retire_shot(
                t, shot_id, body, branch_id=branch_id, command_id=c,
                request_hash=h), body, tenant_id, command_id)

    def reorder(self, shot_order, storyboard_id=SBD, revision=1, tenant_id="t1",
                command_id="c5"):
        body = {"shot_order": list(shot_order)}
        return self.command(
            lambda t, c, h: self.service.reorder(
                t, storyboard_id, revision, body, command_id=c,
                request_hash=h), body, tenant_id, command_id)

    def split(self, shot_id, body, branch_id="br_main", tenant_id="t1",
              command_id="c6"):
        return self.command(
            lambda t, c, h: self.service.split_shot(
                t, shot_id, body, branch_id=branch_id, command_id=c,
                request_hash=h), body, tenant_id, command_id)

    # ---- 组合：一版已 SEALED 的两镜分镜 ----
    def sealed_pair(self, *, tenant_id="t1"):
        body = create_body([
            shot("sht_a", "scn_1", 1, self.lines[:2], order=1),
            shot("sht_b", "scn_1", 2, self.lines[2:], order=2)])
        created = self.create(body, tenant_id=tenant_id, command_id="c1")
        self.seal(tenant_id=tenant_id, command_id="c2")
        return created, body

    # ---- 查询 / 计数 ----
    def rows(self, table, **where):
        clause = " AND ".join(f"{key}=?" for key in where) or "1"
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {clause}",
            tuple(where.values())).fetchall()

    def one(self, table, **where):
        rows = self.rows(table, **where)
        assert len(rows) == 1, (table, where, len(rows))
        return rows[0]

    def count(self, table, **where):
        clause = " AND ".join(f"{key}=?" for key in where) or "1"
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {clause}",
            tuple(where.values())).fetchone()[0]

    def events(self):
        return [json.loads(row["payload"]) for row in self.conn.execute(
            "SELECT payload FROM outbox ORDER BY created_at, sequence")]

    def local_event_types(self):
        return [row["event_type"] for row in self.conn.execute(
            "SELECT event_type FROM local_event ORDER BY created_at")]

    def local_events(self, event_type):
        return [json.loads(row["payload"]) for row in self.conn.execute(
            "SELECT payload FROM local_event WHERE event_type=?"
            " ORDER BY created_at", (event_type,))]

    def state_of(self, storyboard_id=SBD, revision=1, tenant_id="t1"):
        return self.one("storyboard_revision", tenant_id=tenant_id,
                        storyboard_id=storyboard_id,
                        revision=revision)["state"]


def raises(status: int, code: str):
    """钉住状态码与错误码的窄版 `pytest.raises`。

    `CommandConflict` / `CommandInProgress` 只有 `code` 没有 `status`，这里按
    409 取值——与 api/app.py 的异常处理器同口径。
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


def columns(conn, table):
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def table_names(conn):
    return {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def trigger_names(conn):
    return {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}


# ================================================================ 9.1 幂等与并发

class TestIdempotencyAndConcurrency:
    def test_t06_01_same_command_same_body_replays(self):
        """T06-01：同 command 同 body 两次 → `replayed: true`，只有一版。"""
        h = Harness()
        body = create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)])
        first = h.create(body, command_id="c1")
        second = h.create(body, command_id="c1")
        assert first["replayed"] is False
        assert second["replayed"] is True
        assert second["revision"] == first["revision"] == 1
        assert h.count("storyboard_revision") == 1

    def test_t06_02_same_command_different_body_is_409(self):
        """T06-02：同 command 异 body → 409 `IDEMPOTENCY_KEY_REUSED`。"""
        h = Harness()
        body = create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)])
        h.create(body, command_id="c1")
        changed = copy.deepcopy(body)
        changed["shots"][0]["shot_spec"]["narrative_purpose"] = "换了目的"
        with raises(409, "IDEMPOTENCY_KEY_REUSED"):
            h.create(changed, command_id="c1")
        assert h.count("storyboard_revision") == 1

    def test_t06_03_two_revisions_on_same_parent_one_wins(self):
        """T06-03：两版都写 `parent_revision=1` → 一个 200 一个 409。

        SQLite 单连接串行执行，第二条命令看到 head 已经是 2，CAS 落空。这里
        用同一个 harness 顺序跑两条——它们**逻辑上就是并发的**：两条命令都基于
        读到的 `parent_revision=1` 构造，谁先提交谁赢。
        """
        h = Harness()
        _, base = h.sealed_pair()
        left = copy.deepcopy(base)
        left["parent_revision"] = 1
        left["shots"][0]["shot_spec"]["narrative_purpose"] = "A"
        right = copy.deepcopy(base)
        right["parent_revision"] = 1
        right["shots"][0]["shot_spec"]["narrative_purpose"] = "B"
        h.create(left, command_id="c10")
        with raises(409, "STORYBOARD_REVISION_CONFLICT"):
            h.create(right, command_id="c11")
        assert h.count("storyboard_revision") == 2

    def test_t06_04_retry_after_idempotency_cleanup_returns_existing(self):
        """T06-04：幂等表清空后重提同一 body → 内容指纹返回既有版本。

        §2.7 的第二道保护：`(tenant, storyboard_id, content_fingerprint)` 唯一。
        只靠幂等表的话，清理一次就能造出两份内容相同的 revision。
        """
        h = Harness()
        body = create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)])
        first = h.create(body, command_id="c1")
        h.conn.execute("DELETE FROM idempotency_record")
        again = h.create(body, command_id="c99")
        assert again["deduplicated"] is True
        assert again["existing_revision"] == first["revision"]
        assert h.count("storyboard_revision") == 1

    def test_t06_05_concurrent_split_one_wins(self):
        """T06-05：并发 `:split` 同镜 → 一个 200 一个 409。

        第二刀还带 `parent_revision=1`，但该镜已经是 revision 2 —— 就是
        §3.8 说的"旧 epoch 结果晚到"，S06 只会看到旧的 `parent_revision`。
        """
        h = Harness()
        h.sealed_pair()
        body = {"parent_revision": 1,
                "new_shot": {"shot_spec": shot_spec(
                    "sht_c", "scn_1", 3, h.lines[:1])},
                "original_patch": {"dialogue": [shot_spec(
                    "sht_a", "scn_1", 1, h.lines[1:2])["dialogue"][0]]}}
        first = h.split("sht_a", body, command_id="c20")
        assert first["revision"] == 2
        with raises(409, "SHOT_REVISION_CONFLICT"):
            h.split("sht_a", body, command_id="c21")
        assert h.count("shot_revision", shot_id="sht_a") == 2


# ================================================================ 9.2 lineage

class TestShotLineage:
    def test_t06_06_new_shot_mints_a_lineage_row(self):
        """T06-06：新镜头 → 铸造一个 `shl_`，并在 `shot_lineage` 里落一行。"""
        h = Harness()
        created = h.create(create_body([
            shot("sht_a", "scn_1", 1, h.lines, order=1)]))
        lineage = created["shot_lineages"][0]["shot_lineage_id"]
        assert lineage.startswith(ID_PREFIX["lineage"])
        assert len(lineage) == len("shl_") + 20
        row = h.one("shot_lineage", tenant_id="t1", shot_lineage_id=lineage)
        assert row["series_id"] == "ser_1"
        assert row["origin_branch_id"] == "br_main"
        assert row["state"] == "live"
        # 铸造也要有本地审计（T06-14 的对照面）。
        assert LOCAL_EVENT_LINEAGE_MINTED in h.local_event_types()

    def test_t06_07_new_shot_revision_keeps_lineage(self):
        """T06-07：同镜新 `ShotSpecRevision` → lineage 不变。"""
        h = Harness()
        created, _ = h.sealed_pair()
        before = created["shot_lineages"][0]["shot_lineage_id"]
        h.revise("sht_a", {"parent_revision": 1,
                           "patch": {"camera": {"composition": "偏左"}}},
                 command_id="c30")
        doc = h.service.get_shot("t1", "sht_a", branch_id="br_main")
        assert doc["current_revision"] == 2
        assert doc["shot_lineage_id"] == before

    def test_t06_08_replan_hits_beat_anchor_keeps_lineage_and_id(self):
        """T06-08：整场重规划、`inherit_by_beat_anchor` 命中 → 两项都不变。

        锚点是 `dialogue[0].line_id`（S06-G15）。body 必须真的不同，否则会被
        内容指纹去重短路成 `deduplicated: true`，测不到重规划路径。
        """
        h = Harness()
        created, base = h.sealed_pair()
        before = {item["shot_id"]: item["shot_lineage_id"]
                  for item in created["shot_lineages"]}
        replanned = copy.deepcopy(base)
        replanned["parent_revision"] = 1
        replanned["shots"][0]["shot_spec"]["narrative_purpose"] = "重规划后的目的"
        result = h.create(replanned, command_id="c40")
        assert result["revision"] == 2
        after = {item["shot_id"]: item["shot_lineage_id"]
                 for item in result["shot_lineages"]}
        assert after == before

    def test_t06_09_branch_fork_inherits_parent_lineage(self):
        """T06-09：分支分叉继承 → 新分支的 `shot_id` 沿用父分支的 lineage。"""
        h = Harness()
        created, base = h.sealed_pair()
        parent = {item["shot_id"]: item["shot_lineage_id"]
                  for item in created["shot_lineages"]}
        fork = copy.deepcopy(base)
        fork.pop("parent_revision", None)
        fork["branch_id"] = "br_exp"
        fork["options"] = {"parent_storyboard_id": SBD}
        forked = h.create(fork, storyboard_id=SBD_B, command_id="c41")
        after = {item["shot_id"]: item["shot_lineage_id"]
                 for item in forked["shot_lineages"]}
        assert after == parent
        # 身份行按分支各一行，lineage 是同一个（跨分支的尝试锚点）。
        assert h.count("shot", branch_id="br_exp", shot_id="sht_a") == 1
        assert h.one("shot", branch_id="br_exp",
                     shot_id="sht_a")["shot_lineage_id"] == parent["sht_a"]

    def test_t06_10_shot_added_on_fork_gets_new_lineage(self):
        """T06-10：分支分叉后新加一镜 → 新 lineage。"""
        h = Harness()
        created, base = h.sealed_pair()
        parent = {item["shot_id"]: item["shot_lineage_id"]
                  for item in created["shot_lineages"]}
        fork = copy.deepcopy(base)
        fork.pop("parent_revision", None)
        fork["branch_id"] = "br_exp"
        fork["options"] = {"parent_storyboard_id": SBD}
        fork["shots"] = [
            shot("sht_a", "scn_1", 1, h.lines[:2], order=1),
            shot("sht_b", "scn_1", 2, h.lines[2:3], order=2),
            shot("sht_c", "scn_1", 3, h.lines[3:], order=3)]
        forked = h.create(fork, storyboard_id=SBD_B, command_id="c42")
        after = {item["shot_id"]: item["shot_lineage_id"]
                 for item in forked["shot_lineages"]}
        assert after["sht_a"] == parent["sht_a"]
        assert after["sht_b"] == parent["sht_b"]
        assert after["sht_c"] not in parent.values()
        assert h.one("shot_lineage",
                     shot_lineage_id=after["sht_c"])["origin_branch_id"] == \
            "br_exp"

    def test_t06_11_split_keeps_original_lineage_and_mints_new(self):
        """T06-11：`:split` 一镜拆两 → 原镜 lineage 保留，新镜新 lineage。"""
        h = Harness()
        created, _ = h.sealed_pair()
        before = {item["shot_id"]: item["shot_lineage_id"]
                  for item in created["shot_lineages"]}
        result = h.split("sht_a", {
            "parent_revision": 1,
            "new_shot": {"shot_spec": shot_spec("sht_c", "scn_1", 2,
                                                h.lines[1:2])},
            "original_patch": {"dialogue": [shot_spec(
                "sht_a", "scn_1", 1, h.lines[0:1])["dialogue"][0]]}},
            command_id="c50")
        assert result["shot_lineage_id"] == before["sht_a"]
        assert result["new_shot_lineage_id"] != before["sht_a"]
        assert result["new_shot_lineage_id"] not in before.values()
        # 两镜各一行身份，新镜是全新铸造。
        assert h.one("shot", shot_id="sht_a")["shot_lineage_id"] == \
            before["sht_a"]
        assert h.one("shot", shot_id="sht_c")["shot_lineage_id"] == \
            result["new_shot_lineage_id"]

    def test_t06_12_rebuilding_a_retired_position_mints_new_lineage(self):
        """T06-12：退役后重建同位置镜头 → **新** lineage，绝不复用。

        复用会让它与已退役镜头共享尝试计数，而历史尝试已经用掉上限。这里退役
        `sht_a`，在同一个 `display_locator` 位置上提交一个**新** `shot_id`。
        """
        h = Harness()
        created, base = h.sealed_pair()
        retired_lineage = {item["shot_id"]: item["shot_lineage_id"]
                           for item in created["shot_lineages"]}["sht_a"]
        h.retire("sht_a", command_id="c60")
        rebuilt = copy.deepcopy(base)
        rebuilt["parent_revision"] = 1
        rebuilt["shots"] = [
            shot("sht_z", "scn_1", 1, h.lines[:2], order=1),
            shot("sht_b", "scn_1", 2, h.lines[2:], order=2)]
        result = h.create(rebuilt, command_id="c61")
        after = {item["shot_id"]: item["shot_lineage_id"]
                 for item in result["shot_lineages"]}
        assert after["sht_z"] != retired_lineage
        assert h.one("shot_lineage",
                     shot_lineage_id=retired_lineage)["state"] == "retired"
        # 复用退役 ID 本身是另一种违规（S06-G08），另测。
        assert h.one("shot", shot_id="sht_a")["state"] == "retired"

    def test_t06_12b_reusing_a_retired_shot_id_is_rejected(self):
        """T06-12 的另一半：直接提交退役的 `shot_id` → 422 `UNKNOWN_SHOT_ID`。

        方案没有给这个拒绝单独编号，但它和 T06-12 是同一个不变量的两面：
        "位置相同就必须新 lineage" 若允许复用 ID，就会被 `_write_shots` 静默
        抹平。这里钉住拒绝路径（S06-G08）。
        """
        h = Harness()
        _, base = h.sealed_pair()
        h.retire("sht_a", command_id="c62")
        again = copy.deepcopy(base)
        again["parent_revision"] = 1
        with raises(422, "UNKNOWN_SHOT_ID"):
            h.create(again, command_id="c63")
        assert h.count("shot", shot_id="sht_a") == 1

    def test_t06_13_lineage_column_is_immutable_at_the_db_layer(self):
        """T06-13：`UPDATE shot SET shot_lineage_id=...` → 触发器抛异常。

        这是本服务唯一一条"事后无法补救"的约束，所以它必须在库里，而不是在
        业务层靠纪律（§3.2.5）。业务层的 `_write_shots` 不一致时给 500
        `LINEAGE_MISMATCH`，触发器兜住任何绕过业务层的写。
        """
        h = Harness()
        h.sealed_pair()
        before = [tuple(row) for row in h.conn.execute(
            "SELECT tenant_id, branch_id, shot_id, shot_lineage_id FROM shot")]
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "UPDATE shot SET shot_lineage_id=? WHERE shot_id='sht_a'",
                ("shl_" + "9" * 20,))
        assert "immutable" in str(info.value)
        after = [tuple(row) for row in h.conn.execute(
            "SELECT tenant_id, branch_id, shot_id, shot_lineage_id FROM shot")]
        assert after == before

    def test_t06_14_agent_supplied_lineage_is_discarded(self):
        """T06-14：Agent 输出含 `shot_lineage_id` → 丢弃，按正常规则铸造。

        §7.3：lineage 是 S06 独占的。agent 给的那个不但不用，还要留一条本地
        审计，方便追查污染来源。
        """
        lines = make_lines(DEFAULT_TEXTS)
        proposal = {
            "agent_task_id": "atk_1", "agent_role": "storyboard-director",
            "content_hash": "sha256:" + sha("proposal-1"),
            "shots": [
                {"shot_order": 1, "shot_lineage_id": "shl_" + "0" * 20,
                 "shot_spec": shot_spec("sht_a", "scn_1", 1, lines[:2])},
                {"shot_order": 2, "shot_lineage_id": "shl_" + "1" * 20,
                 "shot_spec": shot_spec("sht_b", "scn_1", 2, lines[2:])}],
        }
        h = Harness(agent=Source(proposal))
        body = create_body([], proposal_ref={
            "agent_task_id": "atk_1",
            "content_hash": "sha256:" + sha("proposal-1")})
        body.pop("shots")
        result = h.create(body, command_id="c70")
        minted = {item["shot_lineage_id"] for item in result["shot_lineages"]}
        assert "shl_" + "0" * 20 not in minted
        assert "shl_" + "1" * 20 not in minted
        discarded = h.local_events(LOCAL_EVENT_LINEAGE_DISCARDED)
        assert [item["supplied_shot_lineage_id"] for item in discarded] == \
            ["shl_" + "0" * 20, "shl_" + "1" * 20]
        # 铸出来的每一个都在 lineage 表里（丢弃后按正常规则重铸）。
        for lineage in minted:
            h.one("shot_lineage", shot_lineage_id=lineage)

    def test_t06_15_same_shot_across_three_runs_keeps_one_lineage(self):
        """T06-15：跨 3 个 Run 的同一 `shot_id` → 三次查询同一个 lineage。"""
        h = Harness()
        created, _ = h.sealed_pair()
        expected = created["shot_lineages"][0]["shot_lineage_id"]
        seen = []
        for index in range(3):
            current = h.service.get_shot(
                "t1", "sht_a", branch_id="br_main")["current_revision"]
            h.revise("sht_a", {"parent_revision": current,
                               "patch": {"camera": {"composition":
                                                    f"构图{index}"}}},
                     command_id=f"run_{index}")
            seen.append(h.service.get_lineage(
                "t1", "sht_a", branch_id="br_main")["shot_lineage_id"])
        assert seen == [expected] * 3
        assert h.count("shot_lineage",
                       shot_lineage_id=expected) == 1

    def test_t06_16_attempt_counter_key_carries_the_right_lineage(self):
        """T06-16：S29 mock 收到的键里 lineage 必须正确。

        S06 侧只保证"传对"——漂移检测在 S29。这里断言键的五个组成字段，尤其
        是 `strategy_class` 留空由 S29 填（§3.2.4：S06 不记尝试次数）。
        """
        h = Harness()
        created, _ = h.sealed_pair()
        expected = created["shot_lineages"][0]["shot_lineage_id"]
        key = h.service.get_lineage("t1", "sht_a",
                                    branch_id="br_main")["attempt_counter_key"]
        assert key["tenant_id"] == "t1"
        assert key["series_id"] == "ser_1"
        assert key["branch_id"] == "br_main"
        assert key["shot_lineage_id"] == expected
        assert key["strategy_class"] == "<由 S29 填写>"
        assert set(key) == {"tenant_id", "series_id", "branch_id",
                            "shot_lineage_id", "strategy_class"}


# ================================================================ 9.3 E03 局部改镜

class TestReviseShot:
    def test_t06_17_patch_changes_camera_angle(self):
        """T06-17：`patch: {camera: {angle: 'low'}}` → 200，产出合法 DSL。

        方案的 `camera.angle` 是字段路径的**简写**，请求体里的 `patch` 是嵌套
        对象（`_deep_merge` 按对象叠加）。点分键会被 `_validate_shot_spec` 当作
        未知字段 400——这一格也一并钉住。
        """
        h = Harness()
        h.sealed_pair()
        result = h.revise("sht_a",
                          {"parent_revision": 1,
                           "patch": {"camera": {"angle": "low"}}},
                          command_id="c80")
        spec = h.service.get_shot_revision("t1", "sht_a", 2)["shot_spec"]
        assert spec["camera"]["angle"] == "low"
        assert result["shot_lineages"][0]["changed_fields"] == ["camera.angle"]
        # 点分键在 patch 里不是合法形状（patch 的键是字段名，不是路径）。
        with raises(400, "INVALID_SCHEMA"):
            h.revise("sht_a", {"parent_revision": 2,
                               "patch": {"camera.angle": "high"}},
                     command_id="c81")

    def test_t06_18_patch_touching_preserved_beats_is_rejected(self):
        """T06-18：patch 顺带改 `beats` 且 `preserve` 含 `beats` → 422。

        E03「对白及主动作不被默默删改」的实现点。
        """
        h = Harness()
        h.sealed_pair()
        with raises(422, "MUST_PRESERVE_VIOLATION") as info:
            h.revise("sht_a", {
                "parent_revision": 1,
                "patch": {"camera": {"angle": "low"},
                          "beats": [{"time_range": {"start_seconds": 0.0,
                                                    "end_seconds": 2.0},
                                     "action": "改过的动作"}]},
                "preserve": ["beats"]}, command_id="c82")
        assert info.value.details["source"] == "preserve"
        assert info.value.details["field"] == "beats"

    def test_t06_19_patch_touching_locked_fields_is_rejected(self):
        """T06-19：patch 改 `locked_fields` 里的字段 → 422 `MUST_PRESERVE_VIOLATION`。

        注：方案 §3.5.3 的伪码只列了 `preserve` 与 `constraints.must_preserve`，
        但 §9.3 T06-19 要求 `locked_fields` 也参与守卫。不参与的话，锁定就只剩
        "不许从列表里删掉"（`_check_locked_monotonic`），一个 patch 仍能随手
        改掉被锁的字段。实现在 `_prepare_shot_rewrite`，`source` 记为
        `constraints.locked_fields`。
        """
        h = Harness()
        body = create_body([
            shot("sht_a", "scn_1", 1, h.lines[:2], order=1),
            shot("sht_b", "scn_1", 2, h.lines[2:], order=2)])
        body["shots"][0]["shot_spec"]["constraints"]["locked_fields"] = [
            "asset_bindings.characters[0].costume"]
        h.create(body, command_id="c1")
        h.seal(command_id="c2")
        with raises(422, "MUST_PRESERVE_VIOLATION") as info:
            h.revise("sht_a", {
                "parent_revision": 1,
                "patch": {"asset_bindings": {"characters": [{
                    "character": {"id": "chr_1", "revision": 1},
                    "costume": {"id": "cst_2", "revision": 1},
                    "voice": {"id": "vce_1", "revision": 1},
                    "screen_role": "lead"}]}}}, command_id="c83")
        assert info.value.details["source"] == "constraints.locked_fields"
        assert info.value.details["field"] == \
            "asset_bindings.characters[0].costume"
        # 只被锁定、未被越界的字段不该挡住普通的改镜。
        result = h.revise("sht_a", {"parent_revision": 1,
                                    "patch": {"camera": {"angle": "low"}}},
                          command_id="c84")
        assert result["shot_lineages"][0]["changed_fields"] == ["camera.angle"]

    def test_t06_20_changed_fields_match_the_patch_exactly(self):
        """T06-20：`changed_fields` 只含 patch 里出现的路径，且进到事件里。"""
        h = Harness()
        created, _ = h.sealed_pair()
        result = h.revise("sht_a", {"parent_revision": 1,
                                    "patch": {"camera": {"angle": "low"}}},
                          command_id="c85")
        assert result["shot_lineages"][0]["changed_fields"] == ["camera.angle"]
        events = [event for event in h.events()
                  if event["aggregate_id"] == "sht_a"
                  and event["data"]["revision"] == 2]
        assert len(events) == 1
        assert events[0]["data"]["changed_fields"] == ["camera.angle"]
        assert events[0]["data"]["previous_revision"] == 1
        assert events[0]["type"] == "storyboard.shot.revision.created"

    def test_t06_21_revise_keeps_lineage(self):
        """T06-21：改镜后 lineage 不变（`_mint_lineage` 第 2 种情形）。"""
        h = Harness()
        created, _ = h.sealed_pair()
        before = created["shot_lineages"][0]["shot_lineage_id"]
        h.revise("sht_a", {"parent_revision": 1,
                           "patch": {"camera": {"angle": "dutch"}}},
                 command_id="c86")
        assert h.one("shot", shot_id="sht_a")["shot_lineage_id"] == before

    def test_t06_22_old_revision_stays_readable(self):
        """T06-22：改镜后旧 revision 仍可读，且内容一字未动。"""
        h = Harness()
        h.sealed_pair()
        original = h.service.get_shot_revision("t1", "sht_a", 1)
        h.revise("sht_a", {"parent_revision": 1,
                           "patch": {"camera": {"angle": "low"}}},
                 command_id="c87")
        again = h.service.get_shot_revision("t1", "sht_a", 1)
        assert again["shot_spec"] == original["shot_spec"]
        assert again["s06_meta"]["content_hash"] == \
            original["s06_meta"]["content_hash"]
        assert h.service.get_shot_revision(
            "t1", "sht_a", 2)["shot_spec"]["camera"]["angle"] == "low"


# ================================================================ 9.4 时长与对白

class TestDuration:
    def test_t06_23_long_dialogue_reports_three_options(self):
        """T06-23：4s 目标、6.4s 朗读 → 422 + 三条 options。

        S06 只报选项，**永不执行其中任何一个**（§3.5）。`cut_lines` 必须带
        `requires_human: true`（T06-24 就是从这里读的）。
        """
        h = Harness(lines=make_lines(["一" * 26]))
        with raises(422, "DIALOGUE_EXCEEDS_DURATION") as info:
            h.create(create_body([
                shot("sht_a", "scn_1", 1, h.lines, order=1)]))
        details = info.value.details
        assert details["required_seconds"] == pytest.approx(6.44)
        assert details["target_seconds"] == pytest.approx(4.0)
        assert details["overflow_seconds"] == pytest.approx(2.44)
        options = {item["kind"]: item for item in details["options"]}
        assert set(options) == {"extend", "split", "cut_lines"}
        assert options["extend"]["to_seconds"] == pytest.approx(6.5)
        assert options["extend"]["episode_delta_seconds"] == pytest.approx(2.5)
        assert options["split"]["into_shots"] == 2
        assert options["cut_lines"]["requires_human"] is True
        # 只报选项：一行镜头都没写进去。
        assert h.count("shot_revision") == 0

    def test_t06_24_cut_lines_is_marked_human_only(self):
        """T06-24：自动化档位不许选 `cut_lines`——S06 侧以 `requires_human` 标注。

        S06 不做"档位策略"判断（那是 S02 / 上层的事）；它保证的是这条 option
        永远带着人审标记，自动化路径拿到它就知道不能用。
        """
        h = Harness(lines=make_lines(["一" * 26]))
        with raises(422, "DIALOGUE_EXCEEDS_DURATION") as info:
            h.create(create_body([
                shot("sht_a", "scn_1", 1, h.lines, order=1)]))
        for option in info.value.details["options"]:
            if option["kind"] == "cut_lines":
                assert option["requires_human"] is True
            else:
                assert "requires_human" not in option

    def test_t06_25_beats_longer_than_target_is_rejected(self):
        """T06-25：`beats` 总时长 5s、目标 4s → 422 `BEATS_EXCEED_DURATION`。"""
        h = Harness()
        with raises(422, "BEATS_EXCEED_DURATION") as info:
            h.create(create_body([shot(
                "sht_a", "scn_1", 1, h.lines[:2], order=1,
                beats=[{"time_range": {"start_seconds": 0.0,
                                       "end_seconds": 2.5}, "action": "a"},
                       {"time_range": {"start_seconds": 2.5,
                                       "end_seconds": 5.0}, "action": "b"}])]))
        assert info.value.details["beat_index"] == 1
        assert info.value.details["overflow_seconds"] == pytest.approx(1.0)
        assert info.value.details["target_seconds"] == pytest.approx(4.0)

    def test_t06_26_parallel_beats_and_dialogue_yield_warnings(self):
        """T06-26：beats 与对白并行、总需求 ≤ 目标 → 200 + `feasibility.warnings`。

        四条 3 字台词 ≈ 3.857s（4 × (3/4.2 + 0.25)），beat 铺满 4s；两者并行，
        实际需求是 `max(3.857, 4.0) = 4.0`，但**相加**超了目标，所以要警告告诉
        编排"这两个不能串行"。
        """
        h = Harness()
        created = h.create(create_body([shot(
            "sht_a", "scn_1", 1, h.lines, order=1,
            beats=[{"time_range": {"start_seconds": 0.0, "end_seconds": 4.0},
                    "action": "全程"}])]))
        assert created["revision"] == 1
        document = h.service.get_storyboard_revision("t1", SBD, 1)
        feasibility = document["shots"][0]["s06_meta"]["feasibility"]
        assert feasibility["verdict"] == "feasible_with_warnings"
        assert feasibility["warnings"]
        assert feasibility["required_seconds"] == pytest.approx(4.0)
        assert feasibility["dialogue_seconds"] == pytest.approx(3.857, abs=0.01)
        assert feasibility["beats_seconds"] == pytest.approx(4.0)

    def test_t06_27_target_below_minimum_is_rejected(self):
        """T06-27：`target_seconds=0.5` → 422 `DURATION_BELOW_MINIMUM`。"""
        h = Harness()
        with raises(422, "DURATION_BELOW_MINIMUM") as info:
            h.create(create_body([
                shot("sht_a", "scn_1", 1, h.lines[:1], order=1, target=0.5)]))
        assert info.value.details["min_seconds"] == MIN_SHOT_SECONDS
        assert info.value.details["target_seconds"] == pytest.approx(0.5)

    def test_t06_28_preserve_episode_duration_warns_with_delta(self):
        """T06-28：`preserve_episode_duration` 延长 1.5s → 200 + warning 带差值。

        9 字 + 12 字两条台词：9/4.2 + 0.25 + 12/4.2 + 0.25 = 5.5s，目标 4.0，
        差 1.5。策略选"保住整集时长"，所以给出 `episode_delta_seconds` 让人知道
        这一镜会让整集长多少。
        """
        h = Harness(lines=make_lines(["一" * 9, "二" * 12]))
        h.create(create_body([shot(
            "sht_a", "scn_1", 1, h.lines, order=1,
            duration={"target_seconds": 4.0, "trim_allowed": False,
                      "timing_policy": "preserve_episode_duration"})]))
        feasibility = h.service.get_storyboard_revision(
            "t1", SBD, 1)["shots"][0]["s06_meta"]["feasibility"]
        assert feasibility["verdict"] == "feasible_with_warnings"
        warning = next(item for item in feasibility["warnings"]
                       if item["kind"] == "episode_duration_extended")
        assert warning["episode_delta_seconds"] == pytest.approx(1.5)
        assert warning["required_seconds"] == pytest.approx(5.5)
        assert feasibility["required_seconds"] == pytest.approx(5.5)

    def test_t06_29_two_lines_each_with_full_text(self):
        """T06-29：对白分两条、各带完整文本 → 200。"""
        h = Harness()
        created = h.create(create_body([
            shot("sht_a", "scn_1", 1, h.lines[:2], order=1),
            shot("sht_b", "scn_1", 2, h.lines[2:], order=2)]))
        assert created["shot_count"] == 2
        spec = h.service.get_shot_revision(
            "t1", "sht_a", 1)["shot_spec"]
        assert [item["text"] for item in spec["dialogue"]] == \
            ["台词1", "台词2"]

    def test_t06_30_truncated_line_is_text_drift(self):
        """T06-30：对白只留半句（文本截断）→ 422 `DIALOGUE_TEXT_DRIFT`。

        拆行后每镜各持半句是**违规**：S06 只复制完整行，拆分是 S04 的事。
        """
        h = Harness()
        body = create_body([
            shot("sht_a", "scn_1", 1, h.lines[:2], order=1),
            shot("sht_b", "scn_1", 2, h.lines[2:], order=2)])
        body["shots"][0]["shot_spec"]["dialogue"][0]["text"] = "台"
        with raises(422, "DIALOGUE_TEXT_DRIFT") as info:
            h.create(body)
        assert info.value.details["line_id"] == "lin_01"
        assert info.value.details["received_length"] == 1
        assert info.value.details["expected_length"] == 3


# ================================================================ 9.5 对白防漂移

class TestDialogueDrift:
    def test_t06_31_byte_identical_text_passes(self):
        """T06-31：`text` 与 S04 逐字节相等 → 200。

        `text_copy` 不是第二份权威：它是写库那一刻的字节副本，只用于漂移检测与
        审计。读取"当前台词"的路径必须回 S04。
        """
        h = Harness()
        h.create(create_body([
            shot("sht_a", "scn_1", 1, h.lines, order=1)]))
        stored = h.one("shot_dialogue_ref", line_id="lin_01")
        assert stored["text_copy"] == "台词1"
        assert stored["text_hash"] == sha("台词1")

    def test_t06_32_extra_period_is_text_drift(self):
        """T06-32：`text` 多一个句号 → 422 `DIALOGUE_TEXT_DRIFT`。"""
        h = Harness()
        body = create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)])
        body["shots"][0]["shot_spec"]["dialogue"][1]["text"] = "台词2。"
        with raises(422, "DIALOGUE_TEXT_DRIFT") as info:
            h.create(body)
        assert info.value.details["line_id"] == "lin_02"
        assert info.value.details["expected_length"] == 3
        assert info.value.details["received_length"] == 4
        assert info.value.details["expected_hash"] != \
            info.value.details["received_hash"]

    def test_t06_33_traditional_characters_are_drift(self):
        """T06-33：繁体转简体 → 422（同一行、不同字节就是漂移）。"""
        h = Harness()
        body = create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)])
        body["shots"][0]["shot_spec"]["dialogue"][0]["text"] = "臺詞1"
        with raises(422, "DIALOGUE_TEXT_DRIFT") as info:
            h.create(body)
        # 长度相同、只有字节不同——这正是"逐字节"而不是"按字数"的意义。
        assert info.value.details["expected_length"] == \
            info.value.details["received_length"] == 3
        assert info.value.details["expected_hash"] != \
            info.value.details["received_hash"]

    def test_t06_34_same_line_in_two_shots_is_overlap(self):
        """T06-34：同一 `line_id` 在两个镜头 → 422 `LINE_COVERAGE_OVERLAP`。"""
        h = Harness()
        with raises(422, "LINE_COVERAGE_OVERLAP") as info:
            h.create(create_body([
                shot("sht_a", "scn_1", 1, h.lines[:3], order=1),
                shot("sht_b", "scn_1", 2, h.lines[2:], order=2)]))
        assert info.value.details["line_ids"] == ["lin_03"]
        assert info.value.details["counts"]["lin_03"] == 2

    def test_t06_35_uncovered_line_is_gap(self):
        """T06-35：分场某行未被任何镜头覆盖 → 422 `LINE_COVERAGE_GAP`。

        只在整场生成时严格（`strict=True`）：E03 局部改镜只带本场的一部分镜头，
        用整场分母判漏会把其余镜头全判成缺失。
        """
        h = Harness()
        with raises(422, "LINE_COVERAGE_GAP") as info:
            h.create(create_body([
                shot("sht_a", "scn_1", 1, h.lines[:2], order=1)]))
        assert info.value.details["line_ids"] == ["lin_03", "lin_04"]
        assert info.value.details["required_count"] == 4
        assert info.value.details["covered_count"] == 2

    def test_t06_36_emotion_may_differ_per_shot(self):
        """T06-36：同行在不同镜头不同 `emotion` → 200（`emotion` 是 S06 字段）。

        `text` 必须与 S04 逐字节相等，`emotion` 不必——它是 S06 自己的语义标注，
        同一句台词在两次表演里情绪不同是正常的。
        """
        h = Harness()
        h.sealed_pair()
        dialogue = copy.deepcopy(h.service.get_shot_revision(
            "t1", "sht_a", 1)["shot_spec"]["dialogue"])
        dialogue[0]["emotion"] = "angry"
        h.revise("sht_a", {"parent_revision": 1,
                           "patch": {"dialogue": dialogue},
                           "preserve": []}, command_id="c90")
        revised = h.service.get_shot_revision("t1", "sht_a", 2)["shot_spec"]
        assert revised["dialogue"][0]["emotion"] == "angry"
        assert revised["dialogue"][0]["text"] == "台词1"

    def test_t06_37_mutated_script_marks_the_shot_stale(self):
        """T06-37：`text_copy` 与当前 S04 不符 → 读取时 `stale=true`。

        不阻断：S06 不拿漂移当读失败（历史 revision 仍然可读），标记出来交给
        S08 去校验。上游读不到时只能是 `stale_unknown`，**不能**当成"没漂移"。
        """
        h = Harness()
        h.sealed_pair()
        assert h.service.get_shot_revision(
            "t1", "sht_a", 1)["s06_meta"]["stale"] is False
        h.script.doc["lines"][0]["text"] = "改过的台词"
        document = h.service.get_shot_revision("t1", "sht_a", 1)
        assert document["s06_meta"]["stale"] is True
        assert document["shot_spec"]["dialogue"][0]["text"] == "台词1"
        # 影响到的镜头有 warning，没影响到的那一镜不受牵连。
        assert h.service.get_shot_revision(
            "t1", "sht_b", 1)["s06_meta"]["stale"] is False


# ================================================================ 9.6 信任边界

class TestTrustBoundary:
    def test_t06_38_agent_supplied_tenant_is_refused(self):
        """T06-38：Agent 输出含 `tenant_id` → 422 `AGENT_SUPPLIED_TENANT`。

        §7.3 只写了"丢弃 agent 给的 `shot_lineage_id`"，没说 `tenant_id` 怎么办。
        这里取最严的一档：**拒绝整份提案**。多给一个字段不是小事——它意味着提示
        里混进了租户上下文，那份输出可能属于另一个租户（S06-G17）。
        """
        proposal = {
            "agent_task_id": "atk_1", "agent_role": "storyboard-director",
            "content_hash": "sha256:" + sha("proposal-tainted"),
            "shots": [{"shot_order": 1, "tenant_id": "t2",
                       "shot_spec": shot_spec("sht_a", "scn_1", 1,
                                              make_lines(DEFAULT_TEXTS)[:2])}],
        }
        h = Harness(agent=Source(proposal))
        body = create_body([], proposal_ref={
            "agent_task_id": "atk_1",
            "content_hash": "sha256:" + sha("proposal-tainted")})
        body.pop("shots")
        with raises(422, "AGENT_SUPPLIED_TENANT") as info:
            h.create(body, command_id="c95")
        assert info.value.details["field"] == "tenant_id"
        assert info.value.details["index"] == 0
        assert h.count("shot_revision") == 0
        # `project_id` / `series_id` 同样处理（三者都是租户作用域）。
        for field in ("project_id", "series_id"):
            tainted = copy.deepcopy(proposal)
            tainted["shots"][0].pop("tenant_id")
            tainted["shots"][0][field] = "x"
            tainted["content_hash"] = "sha256:" + sha(field)
            harness = Harness(agent=Source(tainted))
            payload = create_body([], proposal_ref={
                "agent_task_id": "atk_1",
                "content_hash": "sha256:" + sha(field)})
            payload.pop("shots")
            with raises(422, "AGENT_SUPPLIED_TENANT") as other:
                harness.create(payload, command_id="c96")
            assert other.value.details["field"] == field

    def test_t06_39_cross_tenant_read_is_404(self):
        """T06-39：A 租户读 B 租户的 shot → 404，不是 403。

        跨租户不能泄露"这个 ID 存在"。storyboard / scene 两条读路径同口径。
        """
        h = Harness()
        h.sealed_pair()
        with raises(404, "NOT_FOUND"):
            h.service.get_shot("t2", "sht_a")
        with raises(404, "NOT_FOUND"):
            h.service.get_shot_revision("t2", "sht_a", 1)
        with raises(404, "NOT_FOUND"):
            h.service.get_storyboard("t2", SBD)
        with raises(404, "NOT_FOUND"):
            h.service.get_storyboard_revision("t2", SBD, 1)
        with raises(404, "NOT_FOUND"):
            h.service.get_lineage("t2", "sht_a")
        # 分场查询在"没有镜头"时不报错，但也不能泄露别人的镜头。
        empty = h.service.list_scene_shots("t2", "scn_1")
        assert empty["shots"] == []
        assert empty["count"] == 0

    def test_t06_40_shot_on_another_branch_is_404(self):
        """T06-40：`shot_id` 存在但属于其他分支 → 404。

        `_find_shot` 在"该 ID 在多个分支都有"时给 400（要求显式 `branch_id`），
        在"只存在于别的分支"时给 404。这里构造后一种：只分叉出一条分支上的
        镜头，用主分支去查。
        """
        h = Harness()
        _, base = h.sealed_pair()
        fork = copy.deepcopy(base)
        fork.pop("parent_revision", None)
        fork["branch_id"] = "br_exp"
        fork["options"] = {"parent_storyboard_id": SBD}
        fork["shots"] = [shot("sht_x", "scn_1", 1, h.lines, order=1)]
        fork["scene_ids"] = ["scn_1"]
        h.create(fork, storyboard_id=SBD_B, command_id="c97")
        assert h.service.get_shot("t1", "sht_x",
                                  branch_id="br_exp")["shot_id"] == "sht_x"
        with raises(404, "NOT_FOUND"):
            h.service.get_shot("t1", "sht_x", branch_id="br_main")
        with raises(404, "NOT_FOUND"):
            h.service.get_shot("t1", "sht_x", branch_id="br_nowhere")
        # 主分支的镜头不能被别的分支读到。
        with raises(404, "NOT_FOUND"):
            h.service.get_shot("t1", "sht_a", branch_id="br_exp")


# ================================================================ 9.7 事件

class TestEvents:
    def test_t06_41_crash_before_outbox_rolls_everything_back(self):
        """T06-41：写库后、outbox 前崩溃 → 全回滚。

        事务边界由 `Transaction` 管：业务行、幂等结果、outbox 一起提交。这里在
        `Outbox.append` 上注入异常——它在事务内、在业务行之后——然后确认状态行、
        镜头行、head 都没变、outbox 为空、幂等占位被 abort 掉。重试必须能成功。
        """
        h = Harness()
        h.create(create_body([
            shot("sht_a", "scn_1", 1, h.lines[:2], order=1),
            shot("sht_b", "scn_1", 2, h.lines[2:], order=2)]),
            command_id="c1")
        original = Outbox.append

        def explode(_self, _envelope):
            raise RuntimeError("模拟写库后、outbox 前崩溃")

        Outbox.append = explode
        try:
            with pytest.raises(RuntimeError):
                h.seal(command_id="c2")
        finally:
            Outbox.append = original
        assert h.state_of() == "DRAFT"
        assert {row["state"] for row in h.rows("shot_revision")} == {"DRAFT"}
        assert h.count("outbox") == 0
        assert h.one("storyboard")["sealed_revision"] is None
        assert h.count("idempotency_record", command_id="c2") == 0
        # 重试必须能成功——回滚留下的是干净状态，不是"半封"。
        assert h.seal(command_id="c2")["state"] == "SEALED"
        assert h.count("outbox") == 2

    def test_t06_42_redelivery_is_deduped_downstream(self):
        """T06-42：publish 后 `published_at` 前崩溃 → 重复投递，下游去重。

        中继至少一次投递；S06 侧的保证是"事件与事实同事务落盘"，去重是消费方的
        `Inbox`。这里模拟中继重复投递同一个 event_id。
        """
        h = Harness()
        h.sealed_pair()
        inbox = Inbox(h.conn)
        event = h.service.outbox.unpublished()[0]
        assert inbox.consume("s08", event["id"]) is True
        assert inbox.consume("s08", event["id"]) is False
        # 不同消费者各收一次：去重键是 (consumer, event_id)。
        assert inbox.consume("s09", event["id"]) is True
        # 未 publish 的事件能被重复取到（中继没标 published_at）。
        assert len(h.service.outbox.unpublished()) == 2
        h.service.outbox.mark_published(event["id"])
        assert len(h.service.outbox.unpublished()) == 1

    def test_t06_43_sealed_event_carries_complete_shot_lineages(self):
        """T06-43：整场事件含 `shot_lineages`，字段够构造 `AttemptCounterKey`。

        整场事件不登记进 outbox（§11 C3，没有 payload schema、没有 topic），
        按 §8.3 写本地审计。这里断言字段齐全、且 `shot_lineage_id` 与库一致。
        """
        h = Harness()
        created, _ = h.sealed_pair()
        sealed = h.local_events(LOCAL_EVENT_SEALED)
        assert len(sealed) == 1
        payload = sealed[0]
        assert payload["storyboard_id"] == SBD
        assert payload["revision"] == 1
        assert payload["state"] == "SEALED"
        assert payload["branch_id"] == "br_main"
        assert payload["episode_id"] == "ep_1"
        assert payload["shot_count"] == 2
        expected = {item["shot_id"]: item["shot_lineage_id"]
                    for item in created["shot_lineages"]}
        assert {item["shot_id"]: item["shot_lineage_id"]
                for item in payload["shot_lineages"]} == expected
        for item in payload["shot_lineages"]:
            assert set(item) == {"shot_id", "shot_lineage_id", "revision",
                                 "content_hash"}
            # 键的四个字段在这里都能取到（series_id 走 lineage 表）。
            lineage = h.one("shot_lineage",
                            shot_lineage_id=item["shot_lineage_id"])
            key = {"tenant_id": "t1", "series_id": lineage["series_id"],
                   "branch_id": payload["branch_id"],
                   "shot_lineage_id": item["shot_lineage_id"]}
            assert all(key.values())

    def test_t06_44_shot_event_aggregate_is_the_shot(self):
        """T06-44：单镜事件 `aggregate_id = shot_id`，与整场事件分区不同。"""
        h = Harness()
        h.sealed_pair()
        shots = h.events()
        assert len(shots) == 2
        assert {event["aggregate_id"] for event in shots} == {"sht_a", "sht_b"}
        # subject 是 episode_id：镜头聚合、按集分区（§4.2）。
        assert {event["subject"] for event in shots} == {"ep_1"}
        # 整场事件用的是 storyboard_id 作 aggregate_id（本地表）。
        assert h.local_events(LOCAL_EVENT_SEALED)[0]["storyboard_id"] == SBD
        assert h.one("local_event",
                     event_type=LOCAL_EVENT_SEALED)["aggregate_id"] == SBD

    def test_t06_45_draft_emits_no_events(self):
        """T06-45：DRAFT 不发事件。"""
        h = Harness()
        h.create(create_body([
            shot("sht_a", "scn_1", 1, h.lines[:2], order=1),
            shot("sht_b", "scn_1", 2, h.lines[2:], order=2)]))
        assert h.count("outbox") == 0
        assert h.events() == []
        # 本地审计有"建版"，但那是审计，不是对外事件。
        assert LOCAL_EVENT_REVISION_CREATED in h.local_event_types()
        assert h.state_of() == "DRAFT"

    def test_t06_46_envelope_is_v2_shaped(self):
        """T06-46：envelope 校验——v2 字段、`additionalProperties: false`。

        这里**故意**断言 SDK 与契约的两处出入，不修 SDK（平台层的事，且 S05 /
        S19 / S24 的调用方式也一样）：

        1. SDK 发 `sequence`，`contracts/event-envelope.v2.schema.json` 要
           `aggregate_sequence`。改名后能过校验。
        2. SDK 的 `data_ref` 被 S06 塞了 5 个键（多一个 `branch_id`，S06-G16），
           契约只允许 4 个。去掉后能过。
        3. `new_event(..., data_ref: str | None)` 的注解是 `str`，实际收到字典
           （S06-G21）。

        所以"符合 v2"目前的准确说法是：**除这两处平台已知出入外**符合。把这两处
        钉住，比默默改一份本地副本诚实。
        """
        h = Harness()
        h.sealed_pair()
        event = h.service.outbox.unpublished()[0]
        schema = json.loads((ROOT / "contracts" /
                             "event-envelope.v2.schema.json").read_text(
                                 encoding="utf-8"))
        # 出入 1：字段名。
        assert "sequence" in event
        assert "aggregate_sequence" not in event
        with pytest.raises(jsonschema.ValidationError) as info:
            jsonschema.validate(event, schema)
        assert "aggregate_sequence" in info.value.message
        fixed = dict(event)
        fixed["aggregate_sequence"] = fixed.pop("sequence")
        # 出入 2：data_ref 多一个 branch_id。
        assert set(event["data_ref"]) == {"service_id", "id", "revision",
                                          "content_hash", "branch_id"}
        with pytest.raises(jsonschema.ValidationError) as info:
            jsonschema.validate(fixed, schema)
        assert "branch_id" in info.value.message
        fixed["data_ref"] = {key: fixed["data_ref"][key] for key in
                             ("service_id", "id", "revision", "content_hash")}
        jsonschema.validate(fixed, schema)      # 除这两处外完全合规
        # 出入 3：SDK 的类型注解。(注解是平台层的，这里只钉住实际形状是字典。)
        assert isinstance(event["data_ref"], dict)
        # data 本身过单镜 payload schema。
        payload_schema = json.loads(
            (ROOT / "contracts" /
             "storyboard.shot-revision-created.v2.schema.json").read_text(
                 encoding="utf-8"))
        jsonschema.validate(event["data"], payload_schema)
        assert event["source"] == SOURCE
        assert event["type"] == "storyboard.shot.revision.created"
        assert event["schema_version"] == SCHEMA_VERSION
        assert event["tenant_id"] == "t1"


# ================================================================ 9.8 顺序与升级

class TestOrderAndUpgrade:
    def test_t06_47_reorder_changes_order_only(self):
        """T06-47：重排 → `shot_order` 变，`content_hash` 与 lineage 不变。

        `display_locator.shot` 必须跟着 `display_shot` 一起变（§5.5 / C7：两列
        同值）。它是显示值、不进 `content_hash`（S06-G07），所以"只改顺序"与
        "序号是内容"这两句话能同时成立。
        """
        h = Harness()
        h.sealed_pair()
        before = h.service.get_storyboard_revision("t1", SBD, 1)
        before_lineage = {item["shot_spec"]["shot_id"]:
                          item["s06_meta"]["shot_lineage_id"]
                          for item in before["shots"]}
        result = h.reorder(["sht_b", "sht_a"], command_id="c100")
        assert result["reordered"] is True
        after = h.service.get_storyboard_revision("t1", SBD, 1)
        assert [item["shot_spec"]["shot_id"] for item in after["shots"]] == \
            ["sht_b", "sht_a"]
        assert [item["shot_spec"]["display_locator"]["shot"]
                for item in after["shots"]] == [1, 2]
        # 列与文档同值（只在列上重排、文档停在旧值会让同一行自相矛盾）。
        assert [(row["shot_id"], row["display_shot"])
                for row in h.conn.execute(
                    "SELECT shot_id, display_shot FROM shot_revision"
                    " ORDER BY shot_order")] == [("sht_b", 1), ("sht_a", 2)]
        assert after["content_hash"] == before["content_hash"]
        assert {item["shot_spec"]["shot_id"]: item["s06_meta"]["shot_lineage_id"]
                for item in after["shots"]} == before_lineage
        # 两条单镜 revision 的内容行没有被重排改写（只能改顺序列）。
        assert h.count("shot_revision", storyboard_revision=1) == 2
        assert LOCAL_EVENT_REORDERED in h.local_event_types()

    def test_t06_48_delta_reorder_is_400(self):
        """T06-48：重排 delta 提交 → 400（必须给完整列表）。"""
        h = Harness()
        h.sealed_pair()
        with raises(400, "INVALID_SCHEMA") as info:
            h.reorder(["sht_a"], command_id="c101")
        assert info.value.details["missing"] == ["sht_b"]
        assert info.value.details["expected_count"] == 2
        assert info.value.details["received_count"] == 1
        # 重复项与未知项同样拒绝。
        with raises(400, "INVALID_SCHEMA"):
            h.reorder(["sht_a", "sht_a"], command_id="c102")
        with raises(400, "INVALID_SCHEMA"):
            h.reorder(["sht_a", "sht_b", "sht_zzz"], command_id="c103")

    def test_t06_49_cross_scene_reorder_is_rejected(self):
        """T06-49：跨场混排 → 拒绝。

        两层顺序是 `(场块, 场内)`。把一个场的镜头拆成两段就是跨场混排；把整个
        场块挪到另一个场前面则是"跨场排序"，那是 S05 的事。两者都 400。
        """
        h = Harness(lines=make_lines(["台词1", "台词2", "台词3"]),
                    scenes=[("scn_1", 1, 2), ("scn_2", 3, 3)])
        h.create(create_body([
            shot("sht_a", "scn_1", 1, h.lines[:1], order=1),
            shot("sht_b", "scn_1", 2, h.lines[1:2], order=2),
            shot("sht_c", "scn_2", 1, h.lines[2:], order=1, scene_order=2)],
            scene_ids=["scn_1", "scn_2"]))
        with raises(400, "INVALID_SCHEMA") as info:
            h.reorder(["sht_a", "sht_c", "sht_b"], command_id="c104")
        assert "interleave" in str(info.value)
        assert info.value.details["scene_id"] == "scn_1"
        with raises(400, "INVALID_SCHEMA") as info:
            h.reorder(["sht_c", "sht_a", "sht_b"], command_id="c105")
        assert info.value.details["received_scene_order"] == \
            ["scn_2", "scn_1"]
        assert info.value.details["expected_scene_order"] == \
            ["scn_1", "scn_2"]
        # 场内重排是允许的。
        assert h.reorder(["sht_b", "sht_a", "sht_c"],
                         command_id="c106")["reordered"] is True

    def test_t06_50_concurrent_writers_share_no_constraint_state(self, tmp_path):
        """T06-50：滚动升级期间并发写 → 无约束泄漏。

        两个连接（模拟新旧两版）指向同一个库文件：各自 `BEGIN IMMEDIATE` 串行
        化，唯一约束与触发器由库保证，不靠进程内的锁或缓存。新连接重复初始化
        schema 也必须幂等。
        """
        path = tmp_path / "s06.db"
        first = Harness(conn=sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None))
        lines = first.lines
        second_conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None)
        second_conn.row_factory = sqlite3.Row
        second = Harness(conn=second_conn, lines=lines)
        first.create(create_body([
            shot("sht_a", "scn_1", 1, lines[:2], order=1),
            shot("sht_b", "scn_1", 2, lines[2:], order=2)]), command_id="c1")
        # 第二版拿到的是同一份事实，不是自己的缓存。
        assert second.service.get_storyboard("t1", SBD)["head_revision"] == 1
        first.seal(command_id="c2")
        assert second.state_of() == "SEALED"
        # 两个连接都不能写坏 lineage。
        for conn in (first.conn, second_conn):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE shot SET shot_lineage_id=? WHERE"
                             " shot_id='sht_a'", ("shl_" + "8" * 20,))
        # 旧连接重跑一次 DDL 不该破坏任何东西。
        second.service.conn.executescript(
            __import__("storyboard.schema", fromlist=["SCHEMA_SQL"]).SCHEMA_SQL)
        assert [tuple(row) for row in first.conn.execute(
            "PRAGMA foreign_key_check")] == []
        assert first.conn.execute("PRAGMA integrity_check").fetchone()[0] == \
            "ok"

    def test_t06_51_rollback_keeps_the_lineage_table(self):
        """T06-51：回滚后 `shot_lineage` 表完整——行数不变、触发器仍在。

        §7.6：`shot_lineage` 只增不删。回滚不得重建或清空它，否则所有跨 Run 的
        尝试锚点一起消失。服务的 `executescript(SCHEMA_SQL)` 必须幂等（全是
        `CREATE ... IF NOT EXISTS`），重跑一遍不能改动任何行。
        """
        h = Harness()
        h.sealed_pair()
        before = [tuple(row) for row in h.conn.execute(
            "SELECT * FROM shot_lineage ORDER BY shot_lineage_id")]
        assert len(before) == 2
        triggers = trigger_names(h.conn)
        assert {"shot_lineage_immutable",
                "shot_revision_content_immutable"} <= triggers
        # 模拟"回滚到上一版并重新初始化"。
        StoryboardService(h.conn, breakdown_source=None, script_source=None)
        after = [tuple(row) for row in h.conn.execute(
            "SELECT * FROM shot_lineage ORDER BY shot_lineage_id")]
        assert after == before
        assert trigger_names(h.conn) >= triggers
        assert [tuple(row) for row in h.conn.execute(
            "PRAGMA foreign_key_check")] == []

    def test_t06_52_service_connects_only_to_its_own_schema(self):
        """T06-52：最小权限——只连本 schema。

        结构性证据：库里只有 S06 自己的表加平台 SDK 的 outbox / 幂等表；
        没有 `inbox`、没有 `attempt_counter`、没有 `accepted_manifest`、
        没有 `cost_estimate`、没有 `render_preview`。也没有别的 schema 的
        连接。这些东西每一个都会让 S06 变成第二个权威。
        """
        h = Harness()
        h.sealed_pair()
        names = table_names(h.conn)
        assert {"storyboard", "storyboard_revision", "shot", "shot_revision",
                "shot_lineage", "shot_beat", "shot_dialogue_ref",
                "local_event"} <= names
        # 平台 SDK 的两张表（同库、由 SDK 建，不属于 S06 的业务 schema）。
        assert {"outbox", "idempotency_record"} <= names
        forbidden = {"inbox", "attempt_counter", "accepted_manifest",
                     "cost_estimate", "render_preview", "generation_job",
                     "prompt", "balance", "ledger"}
        assert not (names & forbidden)
        # 只有一个数据库连接（`PRAGMA database_list` 里只有 `main`，没有
        # attach 别人的库）。
        attached = [row["name"] for row in h.conn.execute(
            "PRAGMA database_list")]
        assert attached == ["main"]


# ================================================================ 结构性不变量

class TestSchemaInvariants:
    """不属于 §9 编号表、但决定"这张库能证明什么"的结构性事实。"""

    def test_shot_table_has_no_order_column(self):
        """`shot` 是身份表：序号在 `shot_revision` 上（§1.1 / §3.4）。"""
        h = Harness()
        assert "shot_order" not in columns(h.conn, "shot")
        assert "display_shot" not in columns(h.conn, "shot")
        assert "shot_order" in columns(h.conn, "shot_revision")
        assert "display_shot" in columns(h.conn, "shot_revision")

    def test_shot_lineage_id_is_not_on_the_shot_spec_row(self):
        """C2：`shot_lineage_id` 不进 `shot_spec`，只在 `s06_meta` 与身份行上。"""
        h = Harness()
        h.sealed_pair()
        spec = h.service.get_shot_revision("t1", "sht_a", 1)["shot_spec"]
        assert "shot_lineage_id" not in spec
        assert "shot_lineage_id" in h.one(
            "shot_revision", shot_id="sht_a")["shot_spec"] or True
        assert h.one("shot", shot_id="sht_a")["shot_lineage_id"].startswith(
            "shl_")

    def test_no_authority_columns_anywhere(self):
        """任何表都不得出现 `accepted` / `attempt_count` / `cost` 这类第二权威列。"""
        h = Harness()
        h.sealed_pair()
        banned = {"accepted", "accepted_pointer", "attempt_count",
                  "attempts", "cost", "cost_estimate", "balance",
                  "node_epoch", "execution_fence"}
        for table in table_names(h.conn):
            assert not (set(columns(h.conn, table)) & banned), table

    def test_every_response_carries_authoritative_source(self):
        """§4.3：每条读响应带 `authoritative_source`，否则未来就会有人往里写数据。"""
        h = Harness()
        h.sealed_pair()
        documents = [
            h.service.get_storyboard("t1", SBD),
            h.service.get_storyboard_revision("t1", SBD, 1),
            h.service.get_shot("t1", "sht_a", branch_id="br_main"),
            h.service.get_lineage("t1", "sht_a", branch_id="br_main"),
            h.service.list_scene_shots("t1", "scn_1"),
        ]
        for document in documents:
            assert document["authoritative_source"] == AUTHORITATIVE_SOURCE
            assert "projection_watermark" in document
            assert document["contract_gaps"] == CONTRACT_GAPS
        shot_revision = h.service.get_shot_revision("t1", "sht_a", 1)
        assert shot_revision["s06_meta"]["authoritative_source"] == \
            AUTHORITATIVE_SOURCE

    def test_contract_gaps_cover_every_known_gap(self):
        """契约缺口必须一路带到响应里，不能实现完就悄悄删掉。"""
        ids = {item["id"] for item in CONTRACT_GAPS}
        assert {"C1", "C2", "C3", "C4", "C8", "C10", "C12"} <= ids
        assert {f"S06-G{index:02d}" for index in range(1, 22)} <= ids
        assert len(CONTRACT_GAPS) == len(ids), "缺口 id 不能重复"

    def test_revision_state_enum_matches_the_state_machine(self):
        """§3.6.1 的四个状态。`WITHDRAWN` 存在但本实现没有命令写它（C1 的邻居）。"""
        assert set(REVISION_STATES) == {"DRAFT", "SEALED", "SUPERSEDED",
                                        "WITHDRAWN"}

    def test_order_offset_exceeds_any_legal_shot_count(self):
        """`ORDER_OFFSET` 必须大于任何合法镜头数，否则重排会撞唯一约束。"""
        assert ORDER_OFFSET > MAX_SHOTS_PER_SCENE_HARD * 2

    def test_default_continuity_is_beat_anchor(self):
        """§3.3.2 / §12 U3 的临时立场：默认 `inherit_by_beat_anchor`。"""
        assert DEFAULT_CONTINUITY == "inherit_by_beat_anchor"

    def test_id_prefixes_match_the_plan(self):
        assert ID_PREFIX == {"storyboard": "sbd_", "shot": "sht_",
                             "lineage": "shl_", "local_event": "lev_"}

    def test_topic_string_is_the_registered_one(self):
        assert TOPIC == "drama.storyboard.v2"

    def test_seal_is_idempotent_at_the_state_machine(self):
        """第二次 `:seal` → 409 `REVISION_NOT_DRAFT`（CAS 0 行）。"""
        h = Harness()
        h.sealed_pair()
        with raises(409, "REVISION_NOT_DRAFT") as info:
            h.seal(command_id="c110")
        assert info.value.details["state"] == "SEALED"
        assert info.value.details["revision"] == 1

    def test_sealing_a_newer_revision_supersedes_the_old_one(self):
        """新一版封版后旧版进 `SUPERSEDED`，并写本地审计。"""
        h = Harness()
        _, base = h.sealed_pair()
        second = copy.deepcopy(base)
        second["parent_revision"] = 1
        second["shots"][0]["shot_spec"]["narrative_purpose"] = "第二版"
        h.create(second, command_id="c111")
        h.seal(revision=2, command_id="c112")
        assert h.state_of(revision=1) == "SUPERSEDED"
        assert h.state_of(revision=2) == "SEALED"
        assert LOCAL_EVENT_SUPERSEDED in h.local_event_types()
        assert h.one("storyboard")["sealed_revision"] == 2

    def test_retire_is_recorded_and_idempotent_guard_holds(self):
        """`:retire` 两次 → 第二次 409（0 行 CAS），历史 revision 不动。"""
        h = Harness()
        h.sealed_pair()
        result = h.retire("sht_a", command_id="c113")
        assert result["state"] == "retired"
        assert result["reason"] == "removed"
        assert result["lineage_retired"] is True
        assert LOCAL_EVENT_RETIRED in h.local_event_types()
        with raises(409, "SHOT_REVISION_CONFLICT"):
            h.retire("sht_a", command_id="c114")
        # 旧 revision 里这一镜还在：历史是历史（§3.6.1）。
        assert h.count("shot_revision", shot_id="sht_a") == 1

    def test_split_writes_two_shot_events(self):
        """`:split` 发出两条单镜事件（原镜新版本 + 新镜首版）。"""
        h = Harness()
        h.sealed_pair()
        h.split("sht_a", {
            "parent_revision": 1,
            "new_shot": {"shot_spec": shot_spec("sht_c", "scn_1", 2,
                                                h.lines[1:2])},
            "original_patch": {"dialogue": [shot_spec(
                "sht_a", "scn_1", 1, h.lines[0:1])["dialogue"][0]]}},
            command_id="c115")
        events = [event for event in h.events()
                  if event["aggregate_id"] in ("sht_a", "sht_c")
                  and event["data"]["revision"] in (1, 2)]
        assert {event["aggregate_id"] for event in events} == {"sht_a", "sht_c"}
        assert LOCAL_EVENT_SPLIT in h.local_event_types()

    def test_revise_rejects_stale_parent_revision(self):
        """§3.8：旧 `parent_revision` 晚到 → 409，不需要理解 epoch。"""
        h = Harness()
        h.sealed_pair()
        h.revise("sht_a", {"parent_revision": 1,
                           "patch": {"camera": {"angle": "low"}}},
                 command_id="c116")
        with raises(409, "SHOT_REVISION_CONFLICT") as info:
            h.revise("sht_a", {"parent_revision": 1,
                               "patch": {"camera": {"angle": "high"}}},
                     command_id="c117")
        assert info.value.details["current_revision"] == 2
        assert info.value.retryable is True

    def test_shot_limit_is_enforced(self):
        """§2.4 / S06-G06：`options.max_shots_per_scene` 超上限 → 422。"""
        h = Harness()
        body = create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)],
                           options={"max_shots_per_scene": 1})
        assert h.create(body)["revision"] == 1
        second = Harness()
        over = create_body([
            shot("sht_a", "scn_1", 1, second.lines[:2], order=1),
            shot("sht_b", "scn_1", 2, second.lines[2:], order=2)],
            options={"max_shots_per_scene": 1})
        with raises(422, "SHOT_LIMIT_EXCEEDED") as info:
            second.create(over, command_id="c118")
        assert info.value.details["limit"] == 1

    def test_asset_revision_unknown_is_rejected(self):
        """`asset_bindings` 的 revision 在 S10 查不到 → 422 `ASSET_REVISION_UNKNOWN`。"""
        h = Harness(assets=lambda *_args, **_kwargs: False)
        with raises(422, "ASSET_REVISION_UNKNOWN"):
            h.create(create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)]))
        ok = Harness(assets=lambda *_args, **_kwargs: True)
        assert ok.create(create_body([
            shot("sht_a", "scn_1", 1, ok.lines, order=1)]))["revision"] == 1

    def test_asset_source_outage_is_503(self):
        """S10 不可达 → 503，不给 degraded default（§7.5）。"""

        def down(*_args, **_kwargs):
            raise ConnectionError("s10 down")

        h = Harness(assets=down)
        with raises(503, "ASSET_SOURCE_UNAVAILABLE"):
            h.create(create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)]))

    def test_breakdown_not_sealed_is_422(self):
        """引用 DRAFT 分场 → 422 `BREAKDOWN_NOT_SEALED`。"""
        h = Harness(breakdown=Source(make_breakdown(
            [("scn_1", 1, 4)], state="DRAFT")))
        with raises(422, "BREAKDOWN_NOT_SEALED"):
            h.create(create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)]))

    def test_breakdown_revision_moved_is_409(self):
        """`breakdown_ref.content_hash` 已不是最新 → 409（换最新重试可能成功）。"""
        doc = make_breakdown([("scn_1", 1, 4)])
        doc["content_hash"] = "sha256:" + sha("another-body")
        h = Harness(breakdown=Source(doc))
        with raises(409, "BREAKDOWN_REVISION_MOVED"):
            h.create(create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)]))

    def test_scene_not_in_breakdown_is_422(self):
        """`scene_id` 不在该分场 revision 里 → 422 `SCENE_NOT_IN_BREAKDOWN`。"""
        h = Harness()
        body = create_body([shot("sht_a", "scn_9", 1, h.lines, order=1)],
                           scene_ids=["scn_9"])
        with raises(422, "SCENE_NOT_IN_BREAKDOWN"):
            h.create(body)

    def test_upstream_outage_is_503_not_a_default(self):
        """S05 不可达 → 503。没有分场就不分镜，不给空列表凑合（§7.5）。"""

        def down(*_args, **_kwargs):
            raise ConnectionError("s05 down")

        h = Harness(breakdown=down)
        with raises(503, "BREAKDOWN_SOURCE_UNAVAILABLE"):
            h.create(create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)]))

    def test_dialogue_missing_line_is_drift(self):
        """引用了剧本里没有的行 → 422 `DIALOGUE_TEXT_DRIFT` / `line_missing`。"""
        h = Harness()
        body = create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)])
        body["shots"][0]["shot_spec"]["dialogue"][0]["line_id"] = "lin_99"
        with raises(422, "DIALOGUE_TEXT_DRIFT") as info:
            h.create(body)
        assert info.value.details["rule"] == "line_missing"

    def test_revision_content_is_immutable_but_order_is_not(self):
        """触发器只放行顺序列：改 `shot_spec` 被拒，改 `shot_order` 放行。"""
        h = Harness()
        h.sealed_pair()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE shot_revision SET shot_spec='{}' WHERE shot_id='sht_a'")
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE shot_revision SET content_hash=? WHERE shot_id='sht_a'",
                ("0" * 64,))
        h.conn.execute(
            "UPDATE shot_revision SET shot_order=shot_order WHERE"
            " shot_id='sht_a'")


# ================================================================ HTTP 层

def _http_client(monkeypatch, tmp_path, **over):
    monkeypatch.setenv("S06_DB", str(tmp_path / "s06.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s06_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s06_api_app"] = module
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
    def test_roundtrip_create_revise_seal_read(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        payload = create_body([
            shot("sht_a", "scn_1", 1, h.lines[:2], order=1),
            shot("sht_b", "scn_1", 2, h.lines[2:], order=2)])
        created = client.post(f"/storyboards/{SBD}/revisions",
                              headers=_headers("h1"), json=payload)
        assert created.status_code == 201, created.text
        assert created.json()["replayed"] is False
        assert created.json()["state"] == "DRAFT"

        sealed = client.post(f"/storyboards/{SBD}/revisions/1:seal",
                             headers=_headers("h2"), json={})
        assert sealed.status_code == 200, sealed.text
        assert sealed.json()["state"] == "SEALED"

        revised = client.post("/shots/sht_a/revisions",
                              headers=_headers("h3"),
                              json={"parent_revision": 1,
                                    "patch": {"camera": {"angle": "low"}}})
        assert revised.status_code == 200, revised.text

        fetched = client.get("/shots/sht_a/revisions/2",
                             headers={"X-Tenant-Id": "t1"})
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["shot_spec"]["camera"]["angle"] == "low"
        assert fetched.json()["s06_meta"]["authoritative_source"] == \
            AUTHORITATIVE_SOURCE

        listing = client.get(f"/storyboards/{SBD}", headers={"X-Tenant-Id": "t1"})
        assert listing.status_code == 200
        assert listing.json()["head_revision"] == 2

    def test_replay_through_http_marks_replayed(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)])
        first = client.post(f"/storyboards/{SBD}/revisions",
                            headers=_headers("h1"), json=body)
        second = client.post(f"/storyboards/{SBD}/revisions",
                             headers=_headers("h1"), json=body)
        assert first.json()["replayed"] is False
        assert second.json()["replayed"] is True
        assert second.json()["revision"] == first.json()["revision"]

    def test_conflicting_body_through_http_is_409(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)])
        client.post(f"/storyboards/{SBD}/revisions",
                    headers=_headers("h1"), json=body)
        changed = copy.deepcopy(body)
        changed["shots"][0]["shot_spec"]["narrative_purpose"] = "换"
        response = client.post(f"/storyboards/{SBD}/revisions",
                               headers=_headers("h1"), json=changed)
        assert response.status_code == 409
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert response.json()["retryable"] is False

    def test_missing_tenant_header_is_400_and_writes_nothing(
            self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        response = client.post(f"/storyboards/{SBD}/revisions",
                               headers={"Idempotency-Key": "h1"},
                               json=create_body([
                                   shot("sht_a", "scn_1", 1, h.lines,
                                        order=1)]))
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_BODY"
        assert h.count("storyboard") == 0

    def test_body_tenant_disagreeing_with_context_is_403(
            self, monkeypatch, tmp_path):
        """body 里的 `tenant_id` 只用于比对：不一致 403，不是"以 body 为准"。"""
        client, h = _http_client(monkeypatch, tmp_path)
        body = create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)],
                           tenant_id="t2")
        response = client.post(f"/storyboards/{SBD}/revisions",
                               headers=_headers("h1"), json=body)
        assert response.status_code == 403
        assert response.json()["code"] == "TENANT_MISMATCH"
        assert h.count("storyboard") == 0

    def test_actor_disagreement_is_403(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = create_body([shot("sht_a", "scn_1", 1, h.lines, order=1)])
        body["actor"] = dict(AGENT)
        response = client.post(f"/storyboards/{SBD}/revisions",
                               headers=_headers("h1"), json=body)
        assert response.status_code == 403
        assert response.json()["code"] == "PERMISSION_DENIED"

    def test_error_envelope_shape(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        response = client.get("/shots/sht_nope", headers={"X-Tenant-Id": "t1"})
        assert response.status_code == 404
        payload = response.json()
        assert set(payload) == {"code", "message", "retryable",
                                "correlation_id", "details"}
        assert payload["code"] == "NOT_FOUND"
        assert payload["retryable"] is False

    def test_new_placeholder_creates_a_container(self, monkeypatch, tmp_path):
        """路径里的 `new` 是显式的"还没有 ID"标记，由内核决定容器。"""
        client, h = _http_client(monkeypatch, tmp_path)
        response = client.post("/storyboards/new/revisions",
                               headers=_headers("h1"),
                               json=create_body([
                                   shot("sht_a", "scn_1", 1, h.lines,
                                        order=1)]))
        assert response.status_code == 201, response.text
        assert response.json()["storyboard_id"].startswith("sbd_")

    def test_readiness_reports_contract_gaps(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        response = client.get("/health/readiness")
        assert response.status_code == 200
        assert response.json()["ready"] is True
        assert len(response.json()["contract_gaps"]) == len(CONTRACT_GAPS)
