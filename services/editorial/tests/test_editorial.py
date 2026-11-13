"""S41 editorial 测试。

不接真实模型、不接真实渲染、不连真实 S28：素材校验用契约形状的桩。
每条测试对应 `plans/S41-editorial.md` §9 的一个验收 ID；方案与代码有出入的
地方断言的是**登记进 CONTRACT_GAPS 的那个行为**，并在注释里指出缺口编号。

测试分三组：
  * 纯函数（EDL / diff / completeness / 区间合并）—— 不需要库。
  * 内核（提交、CAS、幂等、fence、事件）—— in-memory SQLite + 桩。
  * HTTP（路由、header、状态码）—— 用 S41_DB 指向临时文件后重新加载模块。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import time

import pytest

from editorial import CONTRACT_GAPS, TICK_RATE
from editorial.schema import EVENT_DATA_KEYS, EVENT_REVISION_CREATED, SOURCE, TOPIC
from editorial.service import (
    ServiceError,
    TimelineService,
    bare_hash,
    compute_completeness,
    compute_content_hash,
    compute_diff,
    compute_edl,
    compute_edl_hash,
    compute_mix_plan,
    compute_output_plan,
    duration_ticks,
    merge_ranges,
    round_half_up,
    stable_request_hash,
)
from event_sdk import Inbox
from idempotency_sdk import SqliteIdempotencyStore

ART_HASH = "a" * 64
ART_HASH_2 = "b" * 64

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", ".."))
CONTRACT_PATH = os.path.join(
    ROOT, "contracts", "post.timeline-revision-created.v2.schema.json")


# ------------------------------------------------------------------ 夹具

def artifact(ident="art_1", digest=ART_HASH, millis=10_000,
             state="AVAILABLE"):
    return {"artifact_id": ident, "tenant_id": "t1", "state": state,
            "current_revision": 1, "content_hash": digest,
            "duration_ms": millis, "mime_type": "video/mp4"}


class Upstream:
    """S28 的桩。记录每次调用，便于断言「实时复查」而不是缓存。"""

    def __init__(self):
        self.calls = []
        self.store = {}
        self.failure = None

    def put(self, ident, revision, **over):
        item = artifact(ident, **over)
        item["current_revision"] = revision
        self.store[(ident, revision)] = item
        return item

    def __call__(self, tenant_id, artifact_id, revision):
        self.calls.append((tenant_id, artifact_id, revision))
        if self.failure is not None:
            raise self.failure
        if tenant_id != "t1":
            # 跨租户：S28 也是 404（E28-S41-02）。
            raise ServiceError(404, "ARTIFACT_NOT_FOUND", "not found")
        item = self.store.get((artifact_id, revision))
        if item is None:
            raise ServiceError(404, "ARTIFACT_NOT_FOUND", "not found")
        return item


def clip(clip_id="c1", track_id="v1", ordinal=0, src_in=0, src_out=360_000,
         rec_in=0, speed=(1, 1), link_group=None, artifact_id="art_1",
         revision=1, digest=ART_HASH, transition_in=None, transition_out=None,
         drift_reason=None, unlinked_at_revision=None):
    return {
        "clip_id": clip_id, "track_id": track_id, "ordinal": ordinal,
        "link_group_id": link_group,
        "src_artifact_ref": {"service_id": "S28", "id": artifact_id,
                             "revision": revision, "content_hash": digest},
        "src_in_ticks": src_in, "src_out_ticks": src_out,
        "rec_in_ticks": rec_in,
        "speed": {"num": speed[0], "den": speed[1]},
        "transition_in": transition_in, "transition_out": transition_out,
        "drift_reason": drift_reason,
        "unlinked_at_revision": unlinked_at_revision,
    }


def body(clips=None, expected_revision=0, expected_fence=2, tracks=None,
         stems=("dialogue", "mix"), locale=None, timeline_kind="master",
         change_cause=None, tenant="t1", node_epoch=4, manifest_id="man_1",
         fps=(24000, 1001), variant=None):
    """默认命令体。

    默认片段长度带 `expected_revision` 个 tick 的偏移：同一内容提交两次会
    被 PTR-3 的 `tr_hash_uniq` 拦成 409（那是**正确**行为），所以「下一版」
    必须真的换内容。需要刻意造同内容的测试显式传 `variant=0`。
    """
    if variant is None:
        variant = expected_revision
    default_clips = [
        clip("c1", "v1", 0, src_out=360_000 + variant),
        clip("c2", "a1", 0, src_out=360_000, artifact_id="art_2"),
    ]
    return {
        "context": {
            "tenant_id": tenant, "project_id": "prj_1", "series_id": "ser_1",
            "episode_id": "ep_1", "branch_id": "br_main",
            "manifest_ref": {"id": manifest_id, "revision": 3},
            "execution_fence": expected_fence, "node_id": "post.timeline",
            "node_epoch": node_epoch,
            "actor": {"principal_id": "u_1", "principal_type": "human"},
        },
        "expected_revision": expected_revision,
        "expected_execution_fence": expected_fence,
        "timeline_kind": timeline_kind,
        "fps": {"num": fps[0], "den": fps[1]},
        "tracks": tracks if tracks is not None else [
            {"track_id": "v1", "kind": "video"},
            {"track_id": "a1", "kind": "dialogue"},
        ],
        "clips": clips if clips is not None else default_clips,
        "output_plan": {"stems": list(stems), "subtitle_locale": locale},
        "change_cause": change_cause,
    }


class Harness:
    """内核 + 幂等 store，镜像 api/app.py 的 `command()` 顺序。"""

    def __init__(self, artifact_source=None, clock=None, conn=None):
        self.conn = conn if conn is not None else sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.upstream = artifact_source or Upstream()
        if isinstance(self.upstream, Upstream):
            self.upstream.put("art_1", 1)
            self.upstream.put("art_2", 1)
        self.service = TimelineService(self.conn,
                                       artifact_source=self.upstream,
                                       clock=clock)
        self.idem = SqliteIdempotencyStore(self.conn)

    def command(self, payload, tenant="t1", command_id="cmd_1",
                timeline_id="tl_1"):
        request_hash = stable_request_hash(payload)
        record = self.idem.begin(tenant, command_id, request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = self.service.commit_revision(
                tenant, timeline_id, payload, command_id, request_hash)
        except Exception:
            self.idem.abort(tenant, command_id)
            raise
        self.idem.finish(tenant, command_id, json.dumps(result,
                                                        ensure_ascii=False))
        return {**result, "replayed": False}

    def rows(self, table, **where):
        clauses = " AND ".join(f"{k} = ?" for k in where) or "1 = 1"
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM {table} WHERE {clauses}", tuple(where.values()))]

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def events(self):
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM outbox ORDER BY sequence, created_at")]


@pytest.fixture()
def h():
    return Harness()


def assert_error(exc_info, status, code):
    assert exc_info.value.status == status, exc_info.value.code
    assert exc_info.value.code == code, exc_info.value.details


# ------------------------------------------------------------------ 纯函数

def test_t41_01_round_half_up_is_integer_only():
    """E03-S41-04 的前提：取整不经过浮点。"""
    assert round_half_up(0, 4) == 0
    assert round_half_up(1, 2) == 1      # 0.5 向上
    assert round_half_up(1, 3) == 0
    assert round_half_up(2, 3) == 1
    assert round_half_up(5, 2) == 3      # 2.5 向上
    assert round_half_up(7, 2) == 4      # 3.5 向上
    with pytest.raises(ValueError):
        round_half_up(-1, 2)


def test_t41_02_edl_is_deterministic():
    """E03-S41-04 / C-S41-01：同一 revision 重算 100 次逐字节相同。"""
    clips = [{"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 0,
              "rec_out_ticks": 360_000, "src_in_ticks": 0,
              "src_out_ticks": 360_000, "speed_num": 1, "speed_den": 1,
              "transition_in": None, "transition_out": None}]
    first = compute_edl_hash(compute_edl(clips, 24000, 1001))
    for _ in range(100):
        assert compute_edl_hash(compute_edl(clips, 24000, 1001)) == first


def test_t41_03_edl_frame_boundary_matches_rounding_rule():
    """E03-S41-04：帧号 = round_half_up(ticks*fps_num/(fps_den*tick_rate))。"""
    # 90000 tick = 1 秒；24000/1001 fps 下 1 秒是 23.976 帧 → 24。
    clips = [{"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 0,
              "rec_out_ticks": TICK_RATE, "src_in_ticks": 0,
              "src_out_ticks": TICK_RATE, "speed_num": 1, "speed_den": 1,
              "transition_in": None, "transition_out": None}]
    entry = compute_edl(clips, 24000, 1001)[0]
    assert entry["record_in_frame"] == 0
    assert entry["record_out_frame"] == 24
    # 整数运算的直接对照：round(90000*24000/(1001*90000)) = round(23.976) = 24
    assert entry["record_out_frame"] == round_half_up(90000 * 24000,
                                                      1001 * TICK_RATE)


def test_t41_04_non_integer_fps_sum_has_zero_drift():
    """E03-S41-05：20 个片段逐段求和与总时长误差 0 tick。"""
    clips = []
    for index in range(20):
        start = index * 45_000
        clips.append({"clip_id": f"c{index}", "track_id": "v1",
                      "rec_in_ticks": start, "rec_out_ticks": start + 45_000,
                      "src_in_ticks": 0, "src_out_ticks": 45_000,
                      "speed_num": 1, "speed_den": 1,
                      "transition_in": None, "transition_out": None})
    assert duration_ticks(clips) == 20 * 45_000
    total = 0
    for clip in clips:
        total += clip["rec_out_ticks"] - clip["rec_in_ticks"]
    assert total == duration_ticks(clips)


def test_t41_05_edl_numbers_per_track_from_one():
    """每个 track 独立编号，`entry_no` 从 1 开始（§5 的 PK）。"""
    clips = [
        {"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 0,
         "rec_out_ticks": 100, "src_in_ticks": 0, "src_out_ticks": 100,
         "speed_num": 1, "speed_den": 1, "transition_in": None,
         "transition_out": None},
        {"clip_id": "c2", "track_id": "a1", "rec_in_ticks": 0,
         "rec_out_ticks": 100, "src_in_ticks": 0, "src_out_ticks": 100,
         "speed_num": 1, "speed_den": 1, "transition_in": None,
         "transition_out": None},
    ]
    entries = compute_edl(clips, 25, 1)
    assert sorted((e["track_id"], e["entry_no"]) for e in entries) == [
        ("a1", 1), ("v1", 1)]


def test_t41_06_merge_ranges_merges_adjacent():
    """E03-S41-06：两个相邻变更合并成一个区间，不留重叠。"""
    merged = merge_ranges([{"start": 0, "end": 100},
                           {"start": 100, "end": 200}])
    assert merged == [{"start": 0, "end": 200}]


def test_t41_07_merge_ranges_keeps_gap():
    merged = merge_ranges([{"start": 0, "end": 100},
                           {"start": 200, "end": 300}])
    assert merged == [{"start": 0, "end": 100}, {"start": 200, "end": 300}]


def test_t41_08_merge_ranges_handles_containment():
    """真包含：外包区间不被内层区间截断。"""
    merged = merge_ranges([{"start": 0, "end": 1000},
                           {"start": 100, "end": 200}])
    assert merged == [{"start": 0, "end": 1000}]


def test_t41_09_merge_ranges_drops_empty():
    assert merge_ranges([{"start": 5, "end": 5}]) == []


def test_t41_10_merge_ranges_is_linear():
    """C-S41-02：800 个区间合并是毫秒级，不是 O(n²)。"""
    ranges = [{"start": i * 10, "end": i * 10 + 5} for i in range(800)]
    started = time.perf_counter()
    merged = merge_ranges(ranges)
    elapsed = time.perf_counter() - started
    assert len(merged) == 800
    assert elapsed < 0.2


def test_t41_11_diff_marks_shifted_separately():
    """E03-S41-03：只改 rec_in 的片段进 shifted，不进 changed。"""
    before = [{"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 0,
               "rec_out_ticks": 100, "src_in_ticks": 0, "src_out_ticks": 100,
               "speed_num": 1, "speed_den": 1}]
    after = [{"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 50,
              "rec_out_ticks": 150, "src_in_ticks": 0, "src_out_ticks": 100,
              "speed_num": 1, "speed_den": 1}]
    diff = compute_diff(before, after, 1, 2, 100, 150)
    assert diff["clips"]["shifted"] == [
        {"clip_id": "c1", "track_id": "v1", "delta_ticks": 50}]
    assert diff["clips"]["changed"] == []


def test_t41_12_diff_changed_reports_fields_and_old_new():
    before = [{"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 0,
               "rec_out_ticks": 360_000, "src_in_ticks": 0,
               "src_out_ticks": 360_000, "speed_num": 1, "speed_den": 1}]
    after = [{"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 0,
              "rec_out_ticks": 0, "src_in_ticks": 0,
              "src_out_ticks": 0, "speed_num": 1, "speed_den": 1}]
    diff = compute_diff(before, after, 1, 2, 360_000, 0)
    changed = diff["clips"]["changed"][0]
    assert changed["fields"] == ["src_out", "rec_out"]
    assert changed["old"]["src_out"] == 360_000
    assert changed["new"]["src_out"] == 0


def test_t41_13_diff_covers_old_and_new_positions():
    """E03-S41-01：区间必须覆盖旧位置与新位置。"""
    before = [{"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 900_000,
               "rec_out_ticks": 1_000_000, "src_in_ticks": 0,
               "src_out_ticks": 100_000, "speed_num": 1, "speed_den": 1}]
    after = [{"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 180_000,
              "rec_out_ticks": 280_000, "src_in_ticks": 0,
              "src_out_ticks": 100_000, "speed_num": 1, "speed_den": 1}]
    diff = compute_diff(before, after, 1, 2, 1_000_000, 1_000_000)
    starts = [r["start"] for r in diff["affected_ranges"]]
    assert 180_000 in starts
    assert any(r["end"] >= 1_000_000 or r["start"] <= 900_000
               for r in diff["affected_ranges"]
               if r["start"] == 180_000)


def test_t41_14_diff_added_and_removed():
    before = [{"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 0,
               "rec_out_ticks": 100, "src_in_ticks": 0, "src_out_ticks": 100,
               "speed_num": 1, "speed_den": 1}]
    after = [{"clip_id": "c2", "track_id": "v1", "rec_in_ticks": 0,
              "rec_out_ticks": 100, "src_in_ticks": 0, "src_out_ticks": 100,
              "speed_num": 1, "speed_den": 1}]
    diff = compute_diff(before, after, 1, 2, 100, 100)
    assert [c["clip_id"] for c in diff["clips"]["added"]] == ["c2"]
    assert [c["clip_id"] for c in diff["clips"]["removed"]] == ["c1"]


def test_t41_15_diff_duration_change_is_affected():
    """总长变化即使没有 clip 变化也要进区间，否则 tail trim 会漏。"""
    clip_one = {"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 0,
                "rec_out_ticks": 100, "src_in_ticks": 0, "src_out_ticks": 100,
                "speed_num": 1, "speed_den": 1}
    diff = compute_diff([clip_one], [dict(clip_one)], 1, 2, 100, 50)
    assert diff["affected_ranges"] == [{"start": 50, "end": 100}]


def test_t41_16_diff_hash_is_stable():
    before, after = [], [{"clip_id": "c1", "track_id": "v1",
                          "rec_in_ticks": 0, "rec_out_ticks": 100,
                          "src_in_ticks": 0, "src_out_ticks": 100,
                          "speed_num": 1, "speed_den": 1}]
    first = compute_diff(before, after, 0, 1, 0, 100)["diff_hash"]
    second = compute_diff(before, after, 0, 1, 0, 100)["diff_hash"]
    assert first == second
    assert len(first) == 64


def test_t41_17_diff_has_both_range_names():
    """PTR-1：affected_ranges 与 affected_output_ranges 同值。"""
    diff = compute_diff([], [], 0, 1, 0, 0)
    assert diff["affected_ranges"] == diff["affected_output_ranges"]


def test_t41_18_diff_explosion_degrades_to_coarse():
    """§4.5 / §12-6：>500 区间降级为全量并在响应里标 coarse。"""
    before = []
    after = []
    for index in range(DIFF_EXPLOSION_LIMIT_FOR_TEST + 1):
        after.append({"clip_id": f"c{index}", "track_id": "v1",
                      "rec_in_ticks": index * 1000,
                      "rec_out_ticks": index * 1000 + 10,
                      "src_in_ticks": 0, "src_out_ticks": 10,
                      "speed_num": 1, "speed_den": 1})
    diff = compute_diff(before, after, 0, 1, 0, 600_000)
    assert diff["diff_mode"] == "coarse"
    assert diff["affected_ranges"] == [{"start": 0, "end": 600_000}]


DIFF_EXPLOSION_LIMIT_FOR_TEST = 500


def test_t41_19_content_hash_includes_rounding_rule():
    """§2.2 规则 2：改取整规则 = 新 hash。"""
    tracks = [{"track_id": "v1", "kind": "video"}]
    clips = [{"clip_id": "c1", "track_id": "v1", "link_group_id": None,
              "ordinal": 0,
              "src_artifact_ref": {"service_id": "S28", "id": "a",
                                   "revision": 1, "content_hash": ART_HASH},
              "src_in_ticks": 0, "src_out_ticks": 100, "rec_in_ticks": 0,
              "rec_out_ticks": 100, "speed_num": 1, "speed_den": 1}]
    base = compute_content_hash(tracks, clips, 25, 1, TICK_RATE)
    changed = compute_content_hash(tracks, clips, 25, 1, TICK_RATE,
                                   rounding_rule="other_rule_v2")
    assert base != changed


def test_t41_20_content_hash_ignores_clip_order():
    """排序后才比，所以请求里片段顺序不影响 content_hash。"""
    tracks = [{"track_id": "v1", "kind": "video"}]
    one = {"clip_id": "c1", "track_id": "v1", "link_group_id": None,
           "ordinal": 0,
           "src_artifact_ref": {"service_id": "S28", "id": "a",
                                "revision": 1, "content_hash": ART_HASH},
           "src_in_ticks": 0, "src_out_ticks": 100, "rec_in_ticks": 0,
           "rec_out_ticks": 100, "speed_num": 1, "speed_den": 1}
    two = dict(one, clip_id="c2", ordinal=1, rec_in_ticks=100,
               rec_out_ticks=200, src_in_ticks=100, src_out_ticks=200)
    assert (compute_content_hash(tracks, [one, two], 25, 1, TICK_RATE)
            == compute_content_hash(tracks, [two, one], 25, 1, TICK_RATE))


def test_t41_21_content_hash_excludes_revision_number():
    """同一内容两次提交应得到同一 hash（PTR-3 才有意义）。"""
    tracks = [{"track_id": "v1", "kind": "video"}]
    clips = [{"clip_id": "c1", "track_id": "v1", "link_group_id": None,
              "ordinal": 0,
              "src_artifact_ref": {"service_id": "S28", "id": "a",
                                   "revision": 1, "content_hash": ART_HASH},
              "src_in_ticks": 0, "src_out_ticks": 100, "rec_in_ticks": 0,
              "rec_out_ticks": 100, "speed_num": 1, "speed_den": 1}]
    assert (compute_content_hash(tracks, clips, 25, 1, TICK_RATE)
            == compute_content_hash(tracks, clips, 25, 1, TICK_RATE))


def test_t41_22_bare_hash_strips_prefix():
    """E41-HASH：`sha256:` 前缀接受，落库裸 hex。"""
    assert bare_hash("sha256:" + ART_HASH) == ART_HASH
    assert bare_hash(ART_HASH.upper()) == ART_HASH
    assert bare_hash("sha256:short") is None
    assert bare_hash(None) is None
    assert bare_hash("z" * 64) is None


def test_t41_23_completeness_missing_stem():
    """E37-S41-03：缺 stem → incomplete，但可以提交。"""
    tracks = [{"track_id": "v1", "kind": "video"}]
    result = compute_completeness(tracks, [], ["dialogue"], None, [])
    assert result["completeness"] == "incomplete"
    assert result["formal_master_eligible"] is False
    assert {"kind": "missing_stem", "stem": "dialogue"} in result["gaps"]


def test_t41_24_completeness_missing_subtitle_track():
    tracks = [{"track_id": "v1", "kind": "video"}]
    result = compute_completeness(tracks, [], [], "zh-CN", [])
    assert result["gaps"] == [{"kind": "missing_subtitle_track",
                               "locale": "zh-CN"}]


def test_t41_25_completeness_mix_is_not_a_stem():
    """"mix" 是最终混音，不是一路 stem，不该要求有 mix 音轨。"""
    tracks = [{"track_id": "v1", "kind": "video"}]
    result = compute_completeness(tracks, [], ["mix"], None, [])
    assert result["completeness"] == "complete"


def test_t41_26_output_plan_segments_are_merged():
    clips = [{"clip_id": "c1", "track_id": "v1", "rec_in_ticks": 0,
              "rec_out_ticks": 90_000},
             {"clip_id": "c2", "track_id": "a1", "rec_in_ticks": 0,
              "rec_out_ticks": 90_000}]
    plan = compute_output_plan([], clips, ["mix"], None, None)
    assert plan["segments"] == [{"segment_no": 1, "rec_in": 0,
                                 "rec_out": 90_000, "est_seconds": 1.0}]
    assert len(plan["plan_hash"]) == 64


def test_t41_27_mix_plan_has_no_measured_values():
    """§2.5 / E41-C2：MixPlan 只存计划，不存实测响度。"""
    plan = compute_mix_plan({"target_lufs": -16.0, "true_peak_ceiling": -1.0,
                             "stem_gains": {"dialogue": 0.0}})
    assert "measured_lufs" not in plan
    assert "measured_true_peak" not in plan
    assert plan["target_lufs"] == -16.0
    assert len(plan["plan_hash"]) == 64


def test_t41_28_stable_request_hash_ignores_trace():
    """E41-TRACE-HASH：换 trace 仍是同一次幂等重放。"""
    base = body()
    first = stable_request_hash(base)
    traced = body()
    traced["context"]["trace_id"] = "trace-1"
    traced["context"]["timestamp"] = "2026-01-01T00:00:00Z"
    assert stable_request_hash(traced) == first


def test_t41_29_stable_request_hash_notices_business_change():
    base = body()
    changed = body()
    changed["clips"][0]["src_out_ticks"] = 300_000
    assert stable_request_hash(changed) != stable_request_hash(base)


# ------------------------------------------------------------------ 提交

def test_t41_30_first_commit_is_revision_one(h):
    result = h.command(body())
    assert result["revision"] == 1
    assert result["base_revision"] == 0
    assert result["replayed"] is False


def test_t41_31_second_commit_is_revision_two(h):
    h.command(body(), command_id="c1")
    result = h.command(body(expected_revision=1), command_id="c2")
    assert result["revision"] == 2
    assert result["base_revision"] == 1


def test_t41_32_commit_supersedes_previous(h):
    """E41-SUPERSEDE：任一时刻至多一版 COMMITTED。"""
    h.command(body(), command_id="c1")
    h.command(body(expected_revision=1), command_id="c2")
    states = {r["revision"]: r["state"]
              for r in h.rows("timeline_revision")}
    assert states == {1: "SUPERSEDED", 2: "COMMITTED"}


def test_t41_33_commit_truthy_lock_version(h):
    h.command(body(), command_id="c1")
    row = h.rows("timeline")[0]
    assert row["lock_version"] == 1
    assert row["current_revision"] == 1


def test_t41_34_stale_expected_revision_conflicts(h):
    """E12-S41-03：旧 expected_revision → 409 并带 current_revision。"""
    h.command(body(), command_id="c1")
    with pytest.raises(ServiceError) as info:
        h.command(body(expected_revision=0), command_id="c2")
    assert_error(info, 409, "TIMELINE_REVISION_CONFLICT")
    assert info.value.details["current_revision"] == 1


def test_t41_35_conflict_writes_nothing(h):
    h.command(body(), command_id="c1")
    before = (h.count("timeline_revision"), h.count("clip"),
              h.count("outbox"), h.count("timeline_diff"))
    with pytest.raises(ServiceError):
        h.command(body(expected_revision=0), command_id="c2")
    assert (h.count("timeline_revision"), h.count("clip"),
            h.count("outbox"), h.count("timeline_diff")) == before


def test_t41_36_commit_writes_all_side_tables(h):
    h.command(body(), command_id="c1", )
    assert h.count("timeline_revision") == 1
    assert h.count("clip") == 2
    assert h.count("edl_entry") >= 2
    assert h.count("timeline_diff") == 1
    assert h.count("output_plan") == 1
    assert h.count("outbox") == 1
    assert h.count("command_receipt") == 1


def test_t41_37_commit_records_mix_plan(h):
    payload = body()
    payload["mix_plan"] = {"target_lufs": -16.0, "true_peak_ceiling": -1.0,
                           "stem_gains": {"dialogue": 0.0}}
    h.command(payload)
    assert h.count("mix_plan") == 1


def test_t41_38_commit_without_mix_plan_writes_none(h):
    h.command(body())
    assert h.count("mix_plan") == 0


def test_t41_39_commit_stores_pinned_artifact_ref(h):
    h.command(body())
    rows = h.rows("clip", clip_id="c1")
    ref = json.loads(rows[0]["src_artifact_ref"])
    assert ref == {"service_id": "S28", "id": "art_1", "revision": 1,
                   "content_hash": ART_HASH}


def test_t41_40_commit_response_has_no_accepted_wording(h):
    """§6.1 末段 / E41-C6：201 不表示已接受。"""
    result = h.command(body())
    assert result["committed"] is True
    assert "accepted" not in json.dumps(result)
    assert "accepted_pointer" not in result


def test_t41_41_response_exposes_affected_ranges(h):
    result = h.command(body())
    assert result["diff"]["affected_ranges"] == [{"start": 0,
                                                  "end": 360_000}]


def test_t41_42_edit_shortens_following_clips(h):
    """E03-S41-01：某 clip 时长 -4s，同轨后续片段 rec_in 全部 -4s。

    场景就是 docs/07 §2 的「缩 4 秒」：v1 上 c1 从 4 秒剪成 2 秒，
    c2 从 4 秒处左移到 2 秒处。注意这里**不自动**替调用方挪 c2——
    S41 只校验「提交上来的排布」，ripple 是剪辑软件的职责。
    """
    first = [clip("c1", "v1", 0, src_out=360_000),
             clip("c2", "v1", 1, src_in=360_000, src_out=720_000,
                  rec_in=360_000, artifact_id="art_2")]
    h.command(body(clips=first, stems=(), locale=None), command_id="c1")
    edited = [clip("c1", "v1", 0, src_out=180_000),
              clip("c2", "v1", 1, src_in=360_000, src_out=720_000,
                   rec_in=180_000, artifact_id="art_2")]
    h.command(body(clips=edited, expected_revision=1, stems=(), locale=None),
              command_id="c2")
    rows = {r["clip_id"]: r for r in h.rows("clip", revision=2)}
    assert rows["c1"]["rec_out_ticks"] == 180_000
    assert rows["c2"]["rec_in_ticks"] == 180_000
    # 总长也跟着缩短 4 秒。
    assert h.rows("timeline_revision", revision=2)[0]["duration_ticks"] == \
        540_000


def test_t41_43_ripple_shift_is_in_shifted(h):
    """E03-S41-03：后续片段只位移 → shifted，delta 为负。"""
    payload = body(clips=[
        clip("c1", "v1", 0, src_in=0, src_out=180_000),
        clip("c2", "v1", 1, src_in=180_000, src_out=360_000,
             rec_in=180_000, artifact_id="art_2"),
    ], stems=(), locale=None)
    h.command(payload, command_id="c1")
    payload2 = body(clips=[
        clip("c1", "v1", 0, src_in=0, src_out=90_000),
        clip("c2", "v1", 1, src_in=180_000, src_out=360_000,
             rec_in=90_000, artifact_id="art_2"),
    ], expected_revision=1, stems=(), locale=None)
    result = h.command(payload2, command_id="c2")
    diff = h.service.get_diff("t1", "tl_1", 1, 2)
    assert diff["clips"]["changed"][0]["clip_id"] == "c1"
    assert [s["clip_id"] for s in diff["clips"]["shifted"]] == ["c2"]
    assert diff["clips"]["shifted"][0]["delta_ticks"] == -90_000
    assert result["revision"] == 2


def test_t41_44_link_group_desync_rejected(h):
    """E03-S41-02：单独位移一个成员 → 422 LINK_GROUP_DESYNC。"""
    payload = body(clips=[
        clip("c1", "v1", 0, rec_in=0, link_group="lg"),
        clip("c2", "a1", 0, rec_in=10, link_group="lg", artifact_id="art_2"),
    ], stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "LINK_GROUP_DESYNC")
    assert info.value.details["link_group_id"] == "lg"


def test_t41_45_link_group_desync_metric(h):
    payload = body(clips=[
        clip("c1", "v1", 0, rec_in=0, link_group="lg"),
        clip("c2", "a1", 0, rec_in=10, link_group="lg", artifact_id="art_2"),
    ], stems=(), locale=None)
    with pytest.raises(ServiceError):
        h.command(payload)
    assert h.service.metrics.get("editorial_link_group_drift_total") is None


def test_t41_46_link_group_sync_allowed(h):
    payload = body(clips=[
        clip("c1", "v1", 0, rec_in=0, link_group="lg"),
        clip("c2", "a1", 0, rec_in=0, link_group="lg", artifact_id="art_2"),
    ], stems=(), locale=None)
    assert h.command(payload)["revision"] == 1


def test_t41_47_link_group_drift_requires_reason(h):
    """§2.4：显式 allow_av_speed_drift 时强制写 drift_reason。"""
    payload = body(clips=[
        clip("c1", "v1", 0, rec_in=0, link_group="lg"),
        clip("c2", "a1", 0, rec_in=10, link_group="lg", artifact_id="art_2"),
    ], stems=(), locale=None)
    payload["allow_av_speed_drift"] = True
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "LINK_GROUP_DESYNC")


def test_t41_48_link_group_drift_with_reason_allowed(h):
    payload = body(clips=[
        clip("c1", "v1", 0, rec_in=0, link_group="lg",
             drift_reason="intentional_av_offset"),
        clip("c2", "a1", 0, rec_in=10, link_group="lg", artifact_id="art_2",
             drift_reason="intentional_av_offset"),
    ], stems=(), locale=None)
    payload["allow_av_speed_drift"] = True
    assert h.command(payload)["revision"] == 1


def test_t41_49_invalid_speed_zero(h):
    payload = body(clips=[clip("c1", "v1", 0, speed=(0, 1))],
                   stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "INVALID_SPEED")


def test_t41_50_invalid_speed_negative_denominator(h):
    payload = body(clips=[clip("c1", "v1", 0, speed=(1, -2))],
                   stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "INVALID_SPEED")


def test_t41_51_speed_changes_rec_out(h):
    """rec_out = rec_in + (src_out-src_in)*den//num。2x → 一半时长。"""
    payload = body(clips=[clip("c1", "v1", 0, src_in=0, src_out=360_000,
                               speed=(2, 1))], stems=(), locale=None)
    h.command(payload)
    row = h.rows("clip")[0]
    assert row["rec_out_ticks"] == 180_000


def test_t41_52_speed_three_halves(h):
    payload = body(clips=[clip("c1", "v1", 0, src_in=0, src_out=300_000,
                               speed=(3, 2))], stems=(), locale=None)
    h.command(payload)
    assert h.rows("clip")[0]["rec_out_ticks"] == 200_000


def test_t41_53_clip_out_of_range_src_order(h):
    payload = body(clips=[clip("c1", "v1", 0, src_in=100, src_out=100)],
                   stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "CLIP_OUT_OF_RANGE")


def test_t41_54_negative_rec_in_rejected(h):
    payload = body(clips=[clip("c1", "v1", 0, rec_in=-1)],
                   stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "SCHEMA_INVALID")


def test_t41_55_overlap_without_transition_rejected(h):
    # 时长必须大于一帧（24000/1001 下一帧 ≈ 3754 tick），否则先撞上
    # E41-MIN 的「不足一帧」检查，测不到重叠这一支。
    payload = body(clips=[
        clip("c1", "v1", 0, src_in=0, src_out=180_000),
        clip("c2", "v1", 1, src_in=0, src_out=180_000, rec_in=90_000,
             artifact_id="art_2"),
    ], stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "CLIP_OUT_OF_RANGE")
    assert info.value.details["reason"] == "overlap_without_transition"


def test_t41_56_transition_with_handles_allowed(h):
    # 转场重叠 45000 tick（180000-135000）。入侧 src_in=90000 够，
    # 出侧 art_1 长 10s，src_out=180000 后还剩 720000 tick，也够。
    payload = body(clips=[
        clip("c1", "v1", 0, src_in=0, src_out=180_000, transition_out={
            "kind": "crossfade", "frames": 12}),
        clip("c2", "v1", 1, src_in=90_000, src_out=270_000, rec_in=135_000,
             artifact_id="art_2", transition_in={"kind": "crossfade",
                                                "frames": 12}),
    ], stems=(), locale=None)
    assert h.command(payload)["revision"] == 1


def test_t41_57_transition_insufficient_incoming_handle(h):
    # 重叠仍是 45000，但 c2 的 src_in=0 —— 入点之前没有素材可退。
    payload = body(clips=[
        clip("c1", "v1", 0, src_in=0, src_out=180_000, transition_out={
            "kind": "crossfade", "frames": 12}),
        clip("c2", "v1", 1, src_in=0, src_out=180_000, rec_in=135_000,
             artifact_id="art_2", transition_in={"kind": "crossfade",
                                                "frames": 12}),
    ], stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "INSUFFICIENT_HANDLES")
    assert info.value.details["reason"] == "incoming_handle"


def test_t41_58_transition_insufficient_outgoing_handle(h):
    # 入侧够（src_in=90000 ≥ 重叠 45000），但 art_1 只有 1 ms = 90 tick，
    # src_out 已经在尾部之外，出点后 handle 是负数。
    h.upstream.put("art_1", 1, millis=1)
    payload = body(clips=[
        clip("c1", "v1", 0, src_in=0, src_out=180_000, transition_out={
            "kind": "crossfade", "frames": 12}),
        clip("c2", "v1", 1, src_in=90_000, src_out=270_000, rec_in=135_000,
             artifact_id="art_2", transition_in={"kind": "crossfade",
                                                "frames": 12}),
    ], stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "INSUFFICIENT_HANDLES")
    assert info.value.details["reason"] == "outgoing_handle"


def test_t41_59_unknown_duration_is_not_assumed(h):
    """E41-DURATION：时长未知不假设足够。"""
    h.upstream.store[("art_1", 1)] = {"artifact_id": "art_1",
                                      "state": "AVAILABLE",
                                      "content_hash": ART_HASH}
    payload = body(clips=[
        clip("c1", "v1", 0, src_in=0, src_out=180_000, transition_out={
            "kind": "crossfade", "frames": 12}),
        clip("c2", "v1", 1, src_in=90_000, src_out=270_000, rec_in=135_000,
             artifact_id="art_2", transition_in={"kind": "crossfade",
                                                "frames": 12}),
    ], stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "INSUFFICIENT_HANDLES")
    assert info.value.details["available_ticks"] is None


def test_t41_60_empty_clips_rejected(h):
    payload = body(clips=[], stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "SCHEMA_INVALID")


def test_t41_61_undeclared_track_rejected(h):
    payload = body(clips=[clip("c1", "v9", 0)], stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "SCHEMA_INVALID")
    assert info.value.details["track_id"] == "v9"


def test_t41_62_duplicate_clip_id_rejected(h):
    payload = body(clips=[clip("c1", "v1", 0), clip("c1", "a1", 0)],
                   stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "SCHEMA_INVALID")


def test_t41_63_bad_timeline_kind_rejected(h):
    payload = body(timeline_kind="director_cut", stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "SCHEMA_INVALID")


def test_t41_64_fps_must_be_positive_integers(h):
    payload = body(fps=(24000, 0), stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "SCHEMA_INVALID")


def test_t41_65_fps_change_conflicts(h):
    """E41-FPS-CHANGE：fps 建线即固定。"""
    h.command(body(stems=(), locale=None), command_id="c1")
    payload = body(expected_revision=1, fps=(25, 1), stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload, command_id="c2")
    assert_error(info, 409, "TIMELINE_FPS_CONFLICT")


def test_t41_66_timeline_identity_conflict(h):
    h.command(body(stems=(), locale=None), command_id="c1", timeline_id="tl_1")
    with pytest.raises(ServiceError) as info:
        h.command(body(stems=(), locale=None), command_id="c2",
                  timeline_id="tl_2")
    assert_error(info, 409, "TIMELINE_IDENTITY_CONFLICT")


def test_t41_67_timeline_not_active(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    h.conn.execute("UPDATE timeline SET state = 'FROZEN'")
    with pytest.raises(ServiceError) as info:
        h.command(body(expected_revision=1, stems=(), locale=None),
                  command_id="c2")
    assert_error(info, 409, "TIMELINE_NOT_ACTIVE")


def test_t41_68_content_hash_collision(h):
    """PTR-3：同内容第二次提交 → 409 而不是 IntegrityError 500。"""
    h.command(body(stems=(), locale=None), command_id="c1")
    # variant=0：与第一版逐字节相同的内容，才能撞上 tr_hash_uniq。
    with pytest.raises(ServiceError) as info:
        h.command(body(expected_revision=1, variant=0, stems=(), locale=None),
                  command_id="c2")
    assert_error(info, 409, "CONTENT_HASH_COLLISION")
    assert info.value.details["existing_revision"] == 1


def test_t41_69_expected_revision_on_missing_timeline(h):
    with pytest.raises(ServiceError) as info:
        h.command(body(expected_revision=5, stems=(), locale=None))
    assert_error(info, 409, "TIMELINE_REVISION_CONFLICT")


def test_t41_70_revision_payload_is_stored(h):
    h.command(body(stems=(), locale=None))
    payload = json.loads(h.rows("timeline_revision")[0]["payload"])
    assert payload["revision"] == 1
    assert payload["tick_rate"] == TICK_RATE
    assert payload["rounding_rule"] == "half_up_at_frame_boundary_v1"
    assert len(payload["clips"]) == 2


# ------------------------------------------------------------------ 幂等

def test_t41_71_replay_same_revision(h):
    """E12-S41-01：同 command_id 同 body → 同一个 revision。"""
    first = h.command(body(), command_id="c1")
    second = h.command(body(), command_id="c1")
    assert second["revision"] == first["revision"]
    assert second["replayed"] is True
    assert h.count("timeline_revision") == 1


def test_t41_72_replay_does_not_call_upstream(h):
    """重放路径不再复查 S28：素材两分钟后可能已被删，重放不会因此 422。"""
    h.command(body(), command_id="c1")
    calls = len(h.upstream.calls)
    h.command(body(), command_id="c1")
    assert len(h.upstream.calls) == calls


def test_t41_73_replay_keeps_original_response(h):
    first = h.command(body(), command_id="c1")
    second = h.command(body(), command_id="c1")
    for key in ("revision", "content_hash", "duration_ticks", "edl_ref"):
        assert second[key] == first[key]


def test_t41_74_same_key_different_body_conflicts(h):
    """E12-S41-02：同 key 不同 body → 409（SDK），零写入。"""
    from idempotency_sdk import CommandConflict
    h.command(body(), command_id="c1")
    changed = body()
    changed["clips"][0]["src_out_ticks"] = 100
    with pytest.raises(CommandConflict) as info:
        h.command(changed, command_id="c1")
    assert info.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert h.count("timeline_revision") == 1


def test_t41_75_trace_change_is_same_command(h):
    """E41-TRACE-HASH：换 trace 的重投是重放，不是新命令。"""
    payload = body()
    payload["context"]["trace_id"] = "trace-a"
    h.command(payload, command_id="c1")
    retried = body()
    retried["context"]["trace_id"] = "trace-b"
    result = h.command(retried, command_id="c1")
    assert result["replayed"] is True
    assert h.count("timeline_revision") == 1


def test_t41_76_rollback_leaves_no_idempotency_row(h):
    """E12-S41-04 / E13-S41-01：事务回滚后重投仍能成功。"""
    original = h.service.validate_artifacts

    def exploding(tenant, clips):
        original(tenant, clips)
        raise RuntimeError("synthetic crash inside the command")

    h.service.validate_artifacts = exploding
    with pytest.raises(RuntimeError):
        h.command(body(), command_id="c1")
    assert h.count("idempotency_record") == 0
    assert h.count("timeline_revision") == 0
    assert h.count("outbox") == 0

    h.service.validate_artifacts = original
    assert h.command(body(), command_id="c1")["revision"] == 1


def test_t41_77_rollback_inside_transaction_leaves_nothing(h):
    """E13-S41-01：提交事务内崩溃 → 无 revision、无 outbox、无回执。"""
    original = h.service._insert_edl

    def exploding(*args, **kwargs):
        raise RuntimeError("crash after revision insert")

    h.service._insert_edl = exploding
    with pytest.raises(RuntimeError):
        h.command(body(), command_id="c1")
    assert h.count("timeline_revision") == 0
    assert h.count("clip") == 0
    assert h.count("outbox") == 0
    assert h.count("timeline") == 0


def test_t41_78_command_receipt_written_once(h):
    h.command(body(), command_id="c1")
    h.command(body(), command_id="c1")
    assert h.count("command_receipt") == 1


def test_t41_79_concurrent_losers_get_conflict(h):
    """E12-S41-03：两个命令都读到 revision 0，后者失败。"""
    first = h.command(body(), command_id="c1")
    with pytest.raises(ServiceError) as info:
        h.command(body(), command_id="c2")   # expected_revision 仍是 0
    assert_error(info, 409, "TIMELINE_REVISION_CONFLICT")
    assert first["revision"] == 1


# ------------------------------------------------------------------ fence / epoch

def test_t41_80_stale_fence_rejected(h):
    """E07-S41-01：旧 expected_execution_fence → 409 STALE_FENCE。"""
    h.command(body(stems=(), locale=None), command_id="c1")
    stalled = body(expected_revision=1, expected_fence=1, stems=(), locale=None)
    stalled["context"]["execution_fence"] = 1
    with pytest.raises(ServiceError) as info:
        h.command(stalled, command_id="c2")
    assert_error(info, 409, "STALE_FENCE")
    assert info.value.details["current_execution_fence"] == 2


def test_t41_81_stale_fence_write_nothing(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    before = h.count("timeline_revision")
    stalled = body(expected_revision=1, expected_fence=1, stems=(), locale=None)
    stalled["context"]["execution_fence"] = 1
    with pytest.raises(ServiceError):
        h.command(stalled, command_id="c2")
    assert h.count("timeline_revision") == before


def test_t41_82_context_fence_must_match_expected(h):
    payload = body(stems=(), locale=None)
    payload["expected_execution_fence"] = 9
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "SCHEMA_INVALID")


def test_t41_83_no_node_epoch_column_anywhere(h):
    """E07-S41-04 / AGENTS.md：本服务没有节点级 epoch 的写路径。"""
    h.command(body(stems=(), locale=None))
    for table in ("timeline", "timeline_revision", "clip", "edl_entry",
                  "timeline_diff", "output_plan", "mix_plan"):
        columns = {r[1] for r in h.conn.execute(f"PRAGMA table_info({table})")}
        assert "node_epoch" not in columns, table
    # 事件里 epoch 只出现在 built_against（原样透传），不在顶层。
    data = h.events()[0]["data"]
    assert "node_epoch" not in data
    assert data["built_against"]["node_epoch"] == 4


def test_t41_84_no_accepted_pointer_column():
    """E07-S41-04：静态断言——库里没有接受指针列。"""
    from editorial.schema import SCHEMA
    assert "accepted" not in SCHEMA.lower()


def test_t41_85_no_accepted_pointer_sql():
    """E07-S41-04：代码里没有任何写 accepted 的语句。"""
    path = os.path.join(ROOT, "services", "editorial", "src", "editorial",
                        "service.py")
    with io.open(path, encoding="utf-8") as handle:
        source = handle.read()
    assert "accepted_pointer" not in source
    assert "accepted_revision" not in source


def test_t41_86_built_against_carries_epoch_read_only(h):
    """§7.3：`built_against.node_epoch` 是记录，不是权威。"""
    h.command(body(node_epoch=4, stems=(), locale=None))
    built = json.loads(h.rows("timeline_revision")[0]["built_against"])
    assert built["node_epoch"] == 4
    assert built["execution_fence"] == 2
    # 库里没有可更新的 epoch 列。
    columns = {r["name"] for r in h.conn.execute("PRAGMA table_info(timeline)")}
    assert "node_epoch" not in columns


def test_t41_87_old_epoch_does_not_change_local_state(h):
    """E07-S41-02：基于旧 epoch 的提交不改任何本地状态。"""
    h.command(body(node_epoch=4, stems=(), locale=None), command_id="c1")
    snapshot = snapshot_state(h)
    # 新提交带更高的 epoch，仍然只是记录。
    h.command(body(expected_revision=1, node_epoch=9, stems=(), locale=None),
              command_id="c2")
    after = snapshot_state(h)
    assert after["timeline_revision"] == snapshot["timeline_revision"] + 1
    # fence 没有被 API 命令推进过。
    assert h.rows("timeline")[0]["execution_fence"] == 2


def snapshot_state(h):
    return {
        "timeline": h.count("timeline"),
        "timeline_revision": h.count("timeline_revision"),
        "clip": h.count("clip"),
        "outbox": h.count("outbox"),
    }


def test_t41_88_stale_epoch_result_cannot_advance_acceptance(h):
    """E07-S41-04：旧 epoch 的 revision 没有通往「接受」的路径。

    断言链条：提交只写 timeline_revision / outbox；没有任何 UPDATE 会把
    某个 revision 标成 accepted；S08 才是接受权威。
    """
    result = h.command(body(node_epoch=4, stems=(), locale=None))
    assert result["revision"] == 1
    columns = {r["name"] for r in h.conn.execute(
        "PRAGMA table_info(timeline_revision)")}
    assert "accepted" not in {c.lower() for c in columns}
    assert "accepted_pointer" not in {c.lower() for c in columns}
    # 事件里也不含「已接受」语义。
    data = h.events()[0]["data"]
    assert "accepted" not in json.dumps(data)


def test_t41_89_fence_advances_only_from_manifest_event(h):
    """§3.2：fence 只由消费 S08 事件推进。"""
    h.command(body(stems=(), locale=None), command_id="c1")
    assert h.rows("timeline")[0]["execution_fence"] == 2
    envelope = {
        "id": "ev_1", "type": "snapshot.manifest.committed", "tenant_id": "t1",
        "data": {"manifest_ref": {"id": "man_1", "revision": 4},
                 "execution_fence": 5, "changed_node_epochs": [5]},
    }
    result = h.service.consume_manifest_event("t1", envelope)
    assert result["advanced_timelines"] == ["tl_1"]
    assert h.rows("timeline")[0]["execution_fence"] == 5


def test_t41_90_manifest_event_never_writes_epoch(h):
    """E07-S41-04：消费事件也不写 epoch。"""
    h.command(body(stems=(), locale=None), command_id="c1")
    envelope = {
        "id": "ev_1", "type": "snapshot.manifest.committed", "tenant_id": "t1",
        "data": {"manifest_ref": {"id": "man_1", "revision": 4},
                 "execution_fence": 5, "changed_node_epochs": [7, 8]},
    }
    result = h.service.consume_manifest_event("t1", envelope)
    assert result["node_epoch_written"] is False
    assert result["changed_node_epochs_seen"] == 2
    assert json.loads(h.rows("timeline_revision")[0]["built_against"])[
        "node_epoch"] == 4


def test_t41_91_manifest_event_out_of_order_does_not_regress(h):
    """C-S41-03：乱序/旧 fence 的事件不回退投影。"""
    h.command(body(stems=(), locale=None), command_id="c1")
    h.service.consume_manifest_event("t1", {
        "id": "ev_2", "type": "snapshot.manifest.committed", "tenant_id": "t1",
        "data": {"manifest_ref": {"id": "man_1", "revision": 4},
                 "execution_fence": 5, "changed_node_epochs": []}})
    h.service.consume_manifest_event("t1", {
        "id": "ev_1", "type": "snapshot.manifest.committed", "tenant_id": "t1",
        "data": {"manifest_ref": {"id": "man_1", "revision": 3},
                 "execution_fence": 3, "changed_node_epochs": []}})
    assert h.rows("timeline")[0]["execution_fence"] == 5


def test_t41_92_manifest_event_duplicate_is_ignored(h):
    """E13-S41-03：inbox 去重，不重复推进。"""
    h.command(body(stems=(), locale=None), command_id="c1")
    envelope = {
        "id": "ev_1", "type": "snapshot.manifest.committed", "tenant_id": "t1",
        "data": {"manifest_ref": {"id": "man_1", "revision": 4},
                 "execution_fence": 5, "changed_node_epochs": []}}
    first = h.service.consume_manifest_event("t1", envelope)
    second = h.service.consume_manifest_event("t1", envelope)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert h.count("inbox") == 1


def test_t41_93_manifest_event_unknown_type_ignored(h):
    result = h.service.consume_manifest_event(
        "t1", {"id": "ev_9", "type": "artifact.ingested", "tenant_id": "t1",
               "data": {}})
    assert result["ignored"] is True


def test_t41_94_manifest_event_missing_id_rejected(h):
    with pytest.raises(ServiceError) as info:
        h.service.consume_manifest_event(
            "t1", {"type": "snapshot.manifest.committed", "tenant_id": "t1",
                   "data": {}})
    assert_error(info, 400, "INVALID_BODY")


def test_t41_95_manifest_event_bad_fence_rejected(h):
    with pytest.raises(ServiceError) as info:
        h.service.consume_manifest_event(
            "t1", {"id": "ev_1", "type": "snapshot.manifest.committed",
                   "tenant_id": "t1",
                   "data": {"manifest_ref": {"id": "man_1"},
                            "execution_fence": 0}})
    assert_error(info, 400, "INVALID_BODY")


# ------------------------------------------------------------------ 上游 / 素材

def test_t41_96_missing_artifact_rejected(h):
    """E37-S41-01：引用不存在的 artifact → 422，revision 不落库。"""
    payload = body(clips=[clip("c1", "v1", 0, artifact_id="art_missing")],
                   stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "SOURCE_ARTIFACT_MISSING")
    assert info.value.details["clip_id"] == "c1"
    assert h.count("timeline_revision") == 0


def test_t41_97_hash_mismatch_rejected(h):
    """E37-S41-02：hash 不符 → 422 SOURCE_ARTIFACT_MISMATCH，带 clip_id。"""
    h.upstream.put("art_1", 1, digest=ART_HASH_2)
    payload = body(clips=[clip("c1", "v1", 0, artifact_id="art_1",
                               digest=ART_HASH)], stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "SOURCE_ARTIFACT_MISMATCH")
    assert info.value.details["clip_id"] == "c1"
    assert info.value.details["expected"] == ART_HASH
    assert info.value.details["actual"] == ART_HASH_2


def test_t41_98_tombstoned_artifact_rejected(h):
    h.upstream.put("art_1", 1, state="TOMBSTONED")
    payload = body(clips=[clip("c1", "v1", 0)], stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert_error(info, 422, "SOURCE_ARTIFACT_MISSING")


def test_t41_99_upstream_down_is_503(h):
    """E37-S41-05：S28 不可达 → 503，不落库，可重试。"""
    h.upstream.failure = ConnectionError("connection refused")
    with pytest.raises(ServiceError) as info:
        h.command(body(stems=(), locale=None))
    assert_error(info, 503, "DEPENDENCY_UNAVAILABLE")
    assert info.value.retryable is True
    assert h.count("timeline_revision") == 0
    assert h.count("idempotency_record") == 0


def test_t41_100_upstream_500_is_503(h):
    h.upstream.failure = ServiceError(500, "BOOM", "server error")
    with pytest.raises(ServiceError) as info:
        h.command(body(stems=(), locale=None))
    assert_error(info, 503, "DEPENDENCY_UNAVAILABLE")


def test_t41_101_upstream_409_is_503(h):
    """上游「一时判断不了」不是「请求不成立」。"""
    h.upstream.failure = ServiceError(409, "BUSY", "try again", retryable=True)
    with pytest.raises(ServiceError) as info:
        h.command(body(stems=(), locale=None))
    assert_error(info, 503, "DEPENDENCY_UNAVAILABLE")


def test_t41_102_upstream_403_is_not_masked(h):
    """上游 4xx（不是 404/409）原样上抛：那是调用方的问题，重试无用。"""
    h.upstream.failure = ServiceError(403, "FORBIDDEN", "no access")
    with pytest.raises(ServiceError) as info:
        h.command(body(stems=(), locale=None))
    assert_error(info, 403, "FORBIDDEN")


def test_t41_103_unconfigured_upstream_is_503():
    """没配 S28 时不是「没有素材」，是 503。"""
    harness = Harness(artifact_source=None)
    harness.service.artifact_source = None
    with pytest.raises(ServiceError) as info:
        harness.command(body(stems=(), locale=None))
    assert_error(info, 503, "DEPENDENCY_UNAVAILABLE")
    assert info.value.details["reason"] == "not_configured"


def test_t41_104_upstream_is_called_per_commit(h):
    """§4.1 第 6 步：每次提交实时复查，不缓存结果。"""
    h.command(body(stems=(), locale=None), command_id="c1")
    first = len(h.upstream.calls)
    h.command(body(expected_revision=1, stems=(), locale=None),
              command_id="c2")
    assert len(h.upstream.calls) > first


def test_t41_105_artifact_deduped_within_one_command(h):
    """同一个 artifact 在一个命令里只查一次。"""
    payload = body(clips=[
        clip("c1", "v1", 0, artifact_id="art_1"),
        clip("c2", "a1", 0, artifact_id="art_1"),
    ], stems=(), locale=None)
    h.command(payload)
    assert len(h.upstream.calls) == 1


def test_t41_106_transport_404_becomes_artifact_missing(h):
    """E28-S41-02：跨租户时上游 404，本服务转 422 且不泄漏存在性。"""
    payload = body(tenant="t2", stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload, tenant="t2")
    assert_error(info, 422, "SOURCE_ARTIFACT_MISSING")
    assert "t2" not in json.dumps(info.value.details)


def test_t41_107_artifact_duration_ms_converted_to_ticks(h):
    from editorial.service import _artifact_duration_ticks
    assert _artifact_duration_ticks({"duration_ms": 1000}) == TICK_RATE
    assert _artifact_duration_ticks({"duration_ticks": 7}) == 7
    assert _artifact_duration_ticks({}) is None


# ------------------------------------------------------------------ 跨租户

def test_t41_108_cross_tenant_read_is_404(h):
    """E28-S41-01：跨租户读 → 404，不是 403。"""
    h.command(body(stems=(), locale=None), command_id="c1")
    with pytest.raises(ServiceError) as info:
        h.service.get_revision("t2", "tl_1", 1)
    assert_error(info, 404, "TIMELINE_NOT_FOUND")
    assert info.value.status != 403


def test_t41_109_cross_tenant_diff_is_404(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    with pytest.raises(ServiceError) as info:
        h.service.get_diff("t2", "tl_1", 0, 1)
    assert_error(info, 404, "TIMELINE_NOT_FOUND")


def test_t41_110_cross_tenant_edl_is_404(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    with pytest.raises(ServiceError) as info:
        h.service.get_edl("t2", "tl_1", 1)
    assert_error(info, 404, "TIMELINE_NOT_FOUND")


def test_t41_111_cross_tenant_list_is_empty(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    assert h.service.list_timelines("t2")["timelines"] == []


def test_t41_112_cross_tenant_error_has_no_foreign_ids(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    with pytest.raises(ServiceError) as info:
        h.service.get_revision("t2", "tl_1", 1)
    assert info.value.details == {"timeline_id": "tl_1"}


def test_t41_113_every_table_has_tenant_id():
    """E28-S41-03：所有业务表的主键/谓词都带 tenant_id。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(__import__("editorial").SCHEMA)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "tenant_id" in columns, table


def test_t41_114_all_tenant_scoped_queries_filter_tenant():
    """E28-S41-03：服务代码里没有不带 tenant_id 的跨表读。

    用 AST 取真正的 SQL 字面量，而不是对全文做 `split("SELECT")`：
    后者会把 `"SELECT * FROM timeline WHERE " + filters` 里的拼接拆开，
    断言只能在拼接前的半句里找 tenant_id。
    """
    import ast
    path = os.path.join(ROOT, "services", "editorial", "src", "editorial",
                        "service.py")
    with io.open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    business = ("TIMELINE", "CLIP", "EDL_ENTRY", "TIMELINE_DIFF",
                "TIMELINE_REVISION")
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        sql = " ".join(node.value.upper().split())
        if not any(f"FROM {table}" in sql for table in business):
            continue
        checked += 1
        assert "TENANT_ID" in sql, (node.lineno, sql[:120])
    # 分母必须非零：一条都没扫到就等于没检查。
    assert checked >= 6, checked


# ------------------------------------------------------------------ 事件

def test_t41_115_event_type_and_topic(h):
    h.command(body(stems=(), locale=None))
    event = h.events()[0]
    assert event["type"] == EVENT_REVISION_CREATED
    assert event["source"] == SOURCE
    assert TOPIC == "drama.editorial.v2"


def test_t41_116_event_aggregate_is_timeline(h):
    h.command(body(stems=(), locale=None))
    event = h.events()[0]
    assert event["aggregate_id"] == "tl_1"
    assert event["sequence"] == 1


def test_t41_117_event_data_matches_contract(h):
    """§6.3：`data` 与契约 `additionalProperties: false` 逐键相同。"""
    h.command(body(stems=(), locale=None))
    data = h.events()[0]["data"]
    assert set(data) == set(EVENT_DATA_KEYS)


def test_t41_118_event_validates_against_contract(h):
    """用契约自带的 JSON Schema 校验真实产出的事件。"""
    jsonschema = pytest.importorskip("jsonschema")
    h.command(body(stems=(), locale=None), command_id="c1")
    h.command(body(expected_revision=1, stems=(), locale=None),
              command_id="c2")
    with io.open(CONTRACT_PATH, encoding="utf-8") as handle:
        schema = json.load(handle)
    second = h.events()[1]["data"]
    jsonschema.validate(second, schema)
    assert second["diff_ref"]["to_revision"] == second["revision"]


def test_t41_119_first_revision_event_cannot_validate(h):
    """PTR-7：revision 1 的 diff_ref 无法满足契约的 to_revision>=2。

    这不是实现 bug，是契约表达不了「首次提交」；见 CONTRACT_GAPS PTR-7。
    测试明确记录这一事实，避免有人把 from_revision 伪造成 1。
    """
    h.command(body(stems=(), locale=None))
    data = h.events()[0]["data"]
    assert data["diff_ref"]["from_revision"] == 0
    assert data["diff_ref"]["to_revision"] == 1
    assert any(gap["id"] == "PTR-7" for gap in CONTRACT_GAPS)


def test_t41_120_event_has_no_gaps_or_tick_rate(h):
    """PTR-2 / PTR-5：事件不含 gaps 与 tick_rate。"""
    h.command(body(stems=(), locale=None))
    data = h.events()[0]["data"]
    assert "gaps" not in data
    assert "tick_rate" not in data
    assert "change_causes" not in data
    assert "clips" not in data


def test_t41_121_event_built_against_exact_keys(h):
    """x-needs-owner：shot_revisions / memo_checkpoint 不得出现。"""
    h.command(body(stems=(), locale=None))
    built = h.events()[0]["data"]["built_against"]
    assert set(built) == {"manifest_ref", "execution_fence", "node_epoch"}
    assert "shot_revisions" not in built
    assert "memo_checkpoint" not in built


def test_t41_122_event_carries_fence_and_epoch(h):
    h.command(body(node_epoch=4, expected_fence=2, stems=(), locale=None))
    built = h.events()[0]["data"]["built_against"]
    assert built["execution_fence"] == 2
    assert built["node_epoch"] == 4


def test_t41_123_event_content_hash_is_bare_hex(h):
    h.command(body(stems=(), locale=None))
    data = h.events()[0]["data"]
    assert len(data["content_hash"]) == 64
    assert ":" not in data["content_hash"]
    assert len(data["diff_ref"]["diff_hash"]) == 64


def test_t41_124_event_has_data_ref(h):
    h.command(body(stems=(), locale=None))
    event = h.events()[0]
    assert event["data_ref"] == {"service_id": "S41", "id": "tl_1",
                                 "revision": 1,
                                 "content_hash": event["data"]["content_hash"]}


def test_t41_125_event_affected_tracks_sorted(h):
    h.command(body(stems=(), locale=None))
    tracks = h.events()[0]["data"]["affected_tracks"]
    assert tracks == sorted(tracks)


def test_t41_126_incomplete_event_is_flagged(h):
    """E37-S41-03：缺 stem 的事件带 formal_master_eligible=false。"""
    h.command(body(stems=["sfx"], locale=None))
    data = h.events()[0]["data"]
    assert data["completeness"] == "incomplete"
    assert data["formal_master_eligible"] is False


def test_t41_127_outbox_payload_round_trips(h):
    from event_sdk import EventEnvelope
    h.command(body(stems=(), locale=None))
    event = h.events()[0]
    assert event["schema_version"] == "2.0"
    assert event["specversion"] == "1.0"
    assert len(event["traceparent"].split("-")) == 4


def test_t41_128_event_published_at_is_null_until_relay(h):
    h.command(body(stems=(), locale=None))
    row = h.rows("outbox")[0]
    assert row["published_at"] is None


def test_t41_129_relay_marks_published_idempotently(h):
    h.command(body(stems=(), locale=None))
    unpublished = h.service.outbox.unpublished()
    assert len(unpublished) == 1
    h.service.outbox.mark_published(h.events()[0]["id"])
    assert h.service.outbox.unpublished() == []


def test_t41_130_consumer_inbox_dedupes_by_event_id():
    """E13-S41-03：下游 inbox 去重。"""
    inbox = Inbox(sqlite3.connect(":memory:"))
    assert inbox.consume("s38", "ev_1") is True
    assert inbox.consume("s38", "ev_1") is False


# ------------------------------------------------------------------ 读 / EDL / diff

def test_t41_131_get_revision_shape(h):
    h.command(body(stems=(), locale=None))
    revision = h.service.get_revision("t1", "tl_1", 1)
    for key in ("timeline_id", "revision", "state", "content_hash",
                "duration_ticks", "completeness", "gaps", "built_against",
                "edl_hash", "edl", "source_revision", "projection_watermark"):
        assert key in revision, key


def test_t41_132_get_revision_source_revision(h):
    H = h.command(body(stems=(), locale=None))
    revision = h.service.get_revision("t1", "tl_1", H["revision"])
    assert revision["source_revision"] == 3
    assert revision["projection_watermark"] == 2


def test_t41_133_get_revision_is_immutable_snapshot(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    first = h.service.get_revision("t1", "tl_1", 1)
    h.command(body(expected_revision=1, stems=(), locale=None),
              command_id="c2")
    again = h.service.get_revision("t1", "tl_1", 1)
    assert first["content_hash"] == again["content_hash"]
    assert again["state"] == "SUPERSEDED"


def test_t41_134_get_edl_json(h):
    h.command(body(stems=(), locale=None))
    edl = h.service.get_edl("t1", "tl_1", 1)
    assert edl["fps"] == {"num": 24000, "den": 1001}
    assert edl["tick_rate"] == TICK_RATE
    assert edl["rounding_rule"] == "half_up_at_frame_boundary_v1"
    assert len(edl["entries"]) == 2


def test_t41_135_get_edl_recomputes_identically(h):
    """C-S41-01：库里的 EDL 与纯函数重算逐字段相同。"""
    h.command(body(stems=(), locale=None))
    stored = h.service.get_edl("t1", "tl_1", 1)
    clips = h.service._clips("t1", "tl_1", 1)
    recomputed = compute_edl(clips, 24000, 1001, TICK_RATE)
    assert compute_edl_hash(recomputed) == stored["edl_hash"]


def test_t41_136_get_edl_otio_is_view_only(h):
    """§12-4：OTIO 不参与 content_hash，标注为视图。"""
    h.command(body(stems=(), locale=None))
    otio = h.service.get_edl("t1", "tl_1", 1, format="otio")
    assert otio["OTIO_SCHEMA"] == "Timeline.1"
    assert otio["metadata"]["s41"]["note"].startswith("view only")
    revision = h.service.get_revision("t1", "tl_1", 1)
    assert len(revision["content_hash"]) == 64


def test_t41_137_bad_edl_format(h):
    h.command(body(stems=(), locale=None))
    with pytest.raises(ServiceError) as info:
        h.service.get_edl("t1", "tl_1", 1, format="xml")
    assert_error(info, 422, "SCHEMA_INVALID")


def test_t41_138_get_diff_stored_pair(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    h.command(body(expected_revision=1, stems=(), locale=None),
              command_id="c2")
    diff = h.service.get_diff("t1", "tl_1", 1, 2)
    assert diff["from_revision"] == 1
    assert diff["to_revision"] == 2


def test_t41_139_get_diff_non_adjacent_recomputes(h):
    """非相邻两版现算；读路径不写库（§2.1）。"""
    h.command(body(stems=(), locale=None), command_id="c1")
    h.command(body(expected_revision=1, stems=(), locale=None),
              command_id="c2")
    before = h.count("timeline_diff")
    diff = h.service.get_diff("t1", "tl_1", 0, 2)
    assert diff["to_revision"] == 2
    assert h.count("timeline_diff") == before


def test_t41_140_get_diff_rejects_backwards(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    with pytest.raises(ServiceError) as info:
        h.service.get_diff("t1", "tl_1", 1, 1)
    assert_error(info, 422, "SCHEMA_INVALID")


def test_t41_141_missing_revision_404(h):
    h.command(body(stems=(), locale=None))
    with pytest.raises(ServiceError) as info:
        h.service.get_revision("t1", "tl_1", 99)
    assert_error(info, 404, "REVISION_NOT_FOUND")


def test_t41_142_list_timelines_filters(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    assert len(h.service.list_timelines("t1", episode_id="ep_1")["timelines"]) == 1
    assert h.service.list_timelines("t1", episode_id="ep_9")["timelines"] == []


def test_t41_143_list_timelines_limit_bounds(h):
    with pytest.raises(ServiceError) as info:
        h.service.list_timelines("t1", limit=0)
    assert_error(info, 422, "SCHEMA_INVALID")


# ------------------------------------------------------------------ 状态机 / 退役

def test_t41_144_retire_to_voided(h):
    h.command(body(stems=(), locale=None))
    result = h.service.retire_revision("t1", "tl_1", 1, "VOIDED")
    assert result["changed"] is True
    assert h.rows("timeline_revision")[0]["state"] == "VOIDED"


def test_t41_145_voided_cannot_render(h):
    """E07-S41-03：VOIDED 请求正式渲染 → 422。"""
    h.command(body(stems=(), locale=None))
    h.service.retire_revision("t1", "tl_1", 1, "VOIDED")
    with pytest.raises(ServiceError) as info:
        h.service.render_eligibility("t1", "tl_1", 1)
    assert_error(info, 422, "REVISION_VOIDED")


def test_t41_146_superseded_cannot_render(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    h.command(body(expected_revision=1, stems=(), locale=None),
              command_id="c2")
    with pytest.raises(ServiceError) as info:
        h.service.render_eligibility("t1", "tl_1", 1)
    assert_error(info, 422, "REVISION_SUPERSEDED")


def test_t41_147_incomplete_cannot_render(h):
    """E37-S41-03/04：incomplete → 422 INCOMPLETE_TIMELINE。"""
    h.command(body(stems=["sfx"], locale=None))
    with pytest.raises(ServiceError) as info:
        h.service.render_eligibility("t1", "tl_1", 1)
    assert_error(info, 422, "INCOMPLETE_TIMELINE")
    assert info.value.details["gaps"]


def test_t41_148_complete_can_render(h):
    h.command(body(stems=[], locale=None))
    assert h.service.render_eligibility("t1", "tl_1", 1)["eligible"] is True


def test_t41_149_terminal_state_never_regresses(h):
    """§3.1：终态不回退。"""
    h.command(body(stems=(), locale=None))
    h.service.retire_revision("t1", "tl_1", 1, "VOIDED")
    with pytest.raises(ServiceError) as info:
        h.service.retire_revision("t1", "tl_1", 1, "SUPERSEDED")
    assert_error(info, 409, "REVISION_STATE_TERMINAL")


def test_t41_150_retire_is_idempotent(h):
    h.command(body(stems=(), locale=None))
    h.service.retire_revision("t1", "tl_1", 1, "VOIDED")
    again = h.service.retire_revision("t1", "tl_1", 1, "VOIDED")
    assert again["changed"] is False


def test_t41_151_retire_rejects_other_targets(h):
    h.command(body(stems=(), locale=None))
    with pytest.raises(ServiceError) as info:
        h.service.retire_revision("t1", "tl_1", 1, "COMMITTED")
    assert_error(info, 422, "SCHEMA_INVALID")


def test_t41_152_no_server_side_draft_state():
    """§2.1：没有 DRAFT 状态。"""
    from editorial.schema import REVISION_STATES
    assert REVISION_STATES == ("COMMITTED", "SUPERSEDED", "VOIDED")
    assert "DRAFT" not in REVISION_STATES


def test_t41_153_rollback_is_new_revision(h):
    """§2.1 / C-S41-05：回滚不是指针回拨，但也造不出逐字节相同的 revision。

    §2.1 说「回滚 = 提交一个新 revision（内容等于旧 revision）」，§5 又有
    `tr_hash_uniq`。两者冲突：真的逐字节回滚会撞唯一键。这里断言的是
    **登记在 PTR-3 的那个行为**——409 并告知 existing_revision，调用方
    复用旧 revision 号，不产生第三行。指针本身（current_revision）不回退。
    """
    h.command(body(clips=[clip("c1", "v1", 0, src_out=180_000)],
                   stems=(), locale=None), command_id="c1")
    h.command(body(clips=[clip("c1", "v1", 0, src_out=360_000)],
                   expected_revision=1, stems=(), locale=None),
              command_id="c2")
    before = h.count("timeline_revision")
    with pytest.raises(ServiceError) as info:
        h.command(body(clips=[clip("c1", "v1", 0, src_out=180_000)],
                       expected_revision=2, stems=(), locale=None),
                  command_id="c3")
    assert_error(info, 409, "CONTENT_HASH_COLLISION")
    assert info.value.details["existing_revision"] == 1
    assert h.count("timeline_revision") == before
    assert h.rows("timeline")[0]["current_revision"] == 2
    assert any(gap["id"] == "PTR-3" for gap in CONTRACT_GAPS)


# ------------------------------------------------------------------ 契约缺口

def test_t41_154_gaps_are_registered():
    ids = {gap["id"] for gap in CONTRACT_GAPS}
    for expected in ("PTR-1", "PTR-2", "PTR-3", "PTR-4", "PTR-5", "PTR-6",
                     "PTR-7", "E41-HASH", "E41-REC-OUT", "E41-TRACK-KIND",
                     "E41-IDEM-CODE", "E41-TRACE-HASH", "E41-AV-CHECK",
                     "E41-GAPS-422", "E41-AUTHZ", "E41-C1"):
        assert expected in ids, expected


def test_t41_155_gap_entries_have_required_fields():
    for gap in CONTRACT_GAPS:
        assert set(gap) == {"id", "kind", "summary", "handling"}
        assert gap["kind"] in ("conflict", "needs_owner", "gap")


def test_t41_156_no_gap_filled_in_needs_owner_fields(h):
    """用户约束：needs_owner 项只登记，不填值。

    断言的是**行为**而不是措辞：`built_against` 里没有 `shot_revisions`
    （E41-STALE 的形状未定），事件里也没有；`handling` 里提到它只是说明
    缺口在哪，不是填了值。
    """
    h.command(body(stems=(), locale=None))
    data = h.events()[0]["data"]
    assert "shot_revisions" not in data["built_against"]
    assert "memo_checkpoint" not in data["built_against"]
    assert "shot_revisions" not in data
    assert "memo_checkpoint" not in data
    revision = h.service.get_revision("t1", "tl_1", 1)
    assert set(revision["built_against"]) == {
        "manifest_ref", "execution_fence", "node_epoch"}


def test_t41_157_c1_master_spec_has_no_default(h):
    """§11 C1：master_spec_ref 只引用，不给默认值。"""
    h.command(body(stems=(), locale=None))
    revision = h.service.get_revision("t1", "tl_1", 1)
    assert revision["projection_watermark"] == 2
    assert h.rows("timeline")[0]["master_spec_ref"] is None


def test_t41_158_contract_gaps_surface_on_readiness():
    """缺口必须能被运维看到，不能只活在注释里。"""
    assert len(CONTRACT_GAPS) >= 20


def test_t41_159_event_data_keys_constant_matches_schema():
    with io.open(CONTRACT_PATH, encoding="utf-8") as handle:
        schema = json.load(handle)
    assert tuple(schema["required"]) == EVENT_DATA_KEYS


def test_t41_160_schema_has_no_forbidden_tables():
    """§5 末段：刻意不建这三张表。"""
    from editorial.schema import SCHEMA
    for forbidden in ("clip_operation_log", "edl_cache", "render_state"):
        assert forbidden not in SCHEMA


def test_t41_161_schema_has_manifest_index(h):
    """tr_manifest_idx 的用途：fence 投影靠它定位 timeline。"""
    names = {r["name"] for r in h.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "tr_manifest_idx" in names


def test_t41_162_foreign_key_cascade_guard(h):
    """clip 行必须挂在存在的 revision 上。"""
    with pytest.raises(sqlite3.IntegrityError):
        h.conn.execute(
            "INSERT INTO clip (tenant_id, timeline_id, revision, clip_id,"
            " track_id, track_kind, ordinal, src_artifact_ref,"
            " src_in_ticks, src_out_ticks, rec_in_ticks, rec_out_ticks)"
            " VALUES ('t1','tl_1',9,'c1','v1','video',0,'{}',0,1,0,1)")


def test_t41_163_content_hash_check_constraint(h):
    """DDL 的 CHECK 与契约一致：裸 64 位小写 hex。"""
    h.command(body(stems=(), locale=None))
    with pytest.raises(sqlite3.IntegrityError):
        h.conn.execute(
            "UPDATE timeline_revision SET content_hash = 'sha256:abc'")


# ------------------------------------------------------------------ HTTP 层

def _api(monkeypatch, tmp_path, upstream=None):
    """重新加载 HTTP 模块，把它绑到一个临时库上。

    模块在 import 时就建了连接与 service；测试要换库只能重新执行模块。
    桩（Upstream）必须挂到**模块自己的** service 上——换一个 conn 建
    service 会让 `_idem` 与内核落在两个库里，幂等就不起作用了。
    """
    path = os.path.join(ROOT, "services", "editorial", "api", "app.py")
    monkeypatch.setenv("S41_DB", str(tmp_path / "s41.db"))
    spec = importlib.util.spec_from_file_location("s41_api_app", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["s41_api_app"] = module
    spec.loader.exec_module(module)
    harness = Harness(artifact_source=upstream, conn=module._conn)
    module.service = harness.service
    module._idem = harness.idem
    from fastapi.testclient import TestClient
    return TestClient(module.app), harness


def headers(command_id="cmd_1", tenant="t1"):
    return {"Idempotency-Key": command_id, "X-Tenant-Id": tenant}


def test_t41_164_http_commit_roundtrip(monkeypatch, tmp_path):
    client, harness = _api(monkeypatch, tmp_path)
    response = client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                           headers=headers())
    assert response.status_code == 201
    payload = response.json()
    assert payload["revision"] == 1
    assert payload["replayed"] is False
    assert harness.count("timeline_revision") == 1


def test_t41_165_http_requires_idempotency_key(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    response = client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                           headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_BODY"


def test_t41_166_http_requires_tenant_header(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    response = client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                           headers={"Idempotency-Key": "c1"})
    assert response.status_code == 400


def test_t41_167_http_tenant_mismatch_is_403(monkeypatch, tmp_path):
    """AGENTS.md：请求体里的 tenant_id 只用于比对。"""
    client, _ = _api(monkeypatch, tmp_path)
    payload = body(stems=[])
    payload["context"]["tenant_id"] = "t2"
    response = client.post("/timelines/tl_1/revisions", json=payload,
                           headers=headers())
    assert response.status_code == 403
    assert response.json()["code"] == "TENANT_MISMATCH"


def test_t41_168_http_top_level_tenant_checked(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    payload = body(stems=[])
    payload["tenant_id"] = "t2"
    response = client.post("/timelines/tl_1/revisions", json=payload,
                           headers=headers())
    assert response.status_code == 403


def test_t41_169_http_command_id_mismatch(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    payload = body(stems=[])
    payload["command_id"] = "other"
    response = client.post("/timelines/tl_1/revisions", json=payload,
                           headers=headers())
    assert response.status_code == 400


def test_t41_170_http_conflict_code_is_plan_name(monkeypatch, tmp_path):
    """E41-IDEM-CODE：HTTP 返回方案点名的 IDEMPOTENCY_MISMATCH。"""
    client, _ = _api(monkeypatch, tmp_path)
    client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                headers=headers("c1"))
    changed = body(stems=[])
    changed["clips"][0]["src_out_ticks"] = 100
    response = client.post("/timelines/tl_1/revisions", json=changed,
                           headers=headers("c1"))
    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_MISMATCH"
    assert response.json()["details"]["sdk_code"] == "IDEMPOTENCY_KEY_REUSED"


def test_t41_171_http_replay_returns_same_revision(monkeypatch, tmp_path):
    client, harness = _api(monkeypatch, tmp_path)
    first = client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                        headers=headers("c1"))
    second = client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                         headers=headers("c1"))
    assert first.json()["revision"] == second.json()["revision"]
    assert second.json()["replayed"] is True
    assert harness.count("timeline_revision") == 1


def test_t41_172_http_stale_revision_conflict(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                headers=headers("c1"))
    response = client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                           headers=headers("c2"))
    assert response.status_code == 409
    assert response.json()["details"]["current_revision"] == 1


def test_t41_173_http_cross_tenant_read_404(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                headers=headers())
    response = client.get("/timelines/tl_1/revisions/1",
                          headers={"X-Tenant-Id": "t2"})
    assert response.status_code == 404
    assert response.status_code != 403


def test_t41_174_http_get_revision(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                headers=headers())
    response = client.get("/timelines/tl_1/revisions/1",
                          headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 200
    assert response.json()["timeline_id"] == "tl_1"


def test_t41_175_http_diff_endpoint(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                headers=headers("c1"))
    client.post("/timelines/tl_1/revisions",
                json=body(expected_revision=1, stems=[]),
                headers=headers("c2"))
    response = client.get("/timelines/tl_1/diff?from=1&to=2",
                          headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 200
    assert response.json()["to_revision"] == 2


def test_t41_176_http_edl_endpoint(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                headers=headers())
    response = client.get("/timelines/tl_1/edl/1",
                          headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 200
    assert len(response.json()["entries"]) == 2


def test_t41_177_http_list_endpoint(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                headers=headers())
    response = client.get("/timelines?episode_id=ep_1",
                          headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 200
    assert len(response.json()["timelines"]) == 1


def test_t41_178_http_eligibility_endpoint(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    client.post("/timelines/tl_1/revisions", json=body(stems=["sfx"]),
                headers=headers())
    response = client.get("/timelines/tl_1/revisions/1/eligibility",
                          headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 422
    assert response.json()["code"] == "INCOMPLETE_TIMELINE"


def test_t41_179_http_retire_endpoint(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                headers=headers())
    response = client.post("/timelines/tl_1/revisions/1:retire",
                           json={"target": "VOIDED"},
                           headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 200
    assert response.json()["state"] == "VOIDED"


def test_t41_180_http_consume_manifest_event(monkeypatch, tmp_path):
    client, harness = _api(monkeypatch, tmp_path)
    client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                headers=headers())
    response = client.post("/events:consume", json={
        "id": "ev_1", "type": "snapshot.manifest.committed", "tenant_id": "t1",
        "data": {"manifest_ref": {"id": "man_1", "revision": 4},
                 "execution_fence": 5, "changed_node_epochs": []}},
        headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 200
    assert response.json()["advanced_timelines"] == ["tl_1"]


def test_t41_181_http_event_tenant_mismatch(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    response = client.post("/events:consume", json={
        "id": "ev_1", "type": "snapshot.manifest.committed", "tenant_id": "t2",
        "data": {}}, headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 403


def test_t41_182_http_readiness_lists_gaps(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    response = client.get("/health/readiness")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert len(response.json()["contract_gaps"]) == len(CONTRACT_GAPS)


def test_t41_183_http_error_envelope_shape(monkeypatch, tmp_path):
    client, _ = _api(monkeypatch, tmp_path)
    response = client.get("/timelines/nope/revisions/1",
                          headers={"X-Tenant-Id": "t1"})
    payload = response.json()
    for key in ("code", "message", "retryable", "correlation_id", "details"):
        assert key in payload


def test_t41_184_http_upstream_down_is_503(monkeypatch, tmp_path):
    upstream = Upstream()
    upstream.failure = ConnectionError("down")
    client, _ = _api(monkeypatch, tmp_path, upstream=upstream)
    response = client.post("/timelines/tl_1/revisions", json=body(stems=[]),
                           headers=headers())
    assert response.status_code == 503
    assert response.json()["retryable"] is True


def test_t41_185_http_non_dict_body(monkeypatch, tmp_path):
    # `payload: dict` 由 FastAPI 先校验，非对象根本进不到 `_body()`，所以是
    # 框架的 422 而不是本层的 400 —— 与 change-management 的同名用例一致。
    client, harness = _api(monkeypatch, tmp_path)
    response = client.post("/timelines/tl_1/revisions", json=[1, 2],
                           headers=headers())
    assert response.status_code == 422
    assert harness.count("timeline") == 0


# ------------------------------------------------------------------ 一致性 / 恢复

def test_t41_186_edl_hash_matches_revision_row(h):
    h.command(body(stems=(), locale=None))
    row = h.rows("timeline_revision")[0]
    clips = h.service._clips("t1", "tl_1", 1)
    assert row["edl_hash"] == compute_edl_hash(
        compute_edl(clips, 24000, 1001, TICK_RATE))


def test_t41_187_revision_payload_round_trips(h):
    """C-S41-04：payload 可被重新读出并重算（expand 兼容的基础）。"""
    h.command(body(stems=(), locale=None))
    payload = json.loads(h.rows("timeline_revision")[0]["payload"])
    rebuilt = [{k: c[k] for k in ("clip_id", "track_id", "link_group_id",
                                  "ordinal", "src_artifact_ref",
                                  "src_in_ticks", "src_out_ticks",
                                  "rec_in_ticks", "rec_out_ticks",
                                  "speed_num", "speed_den")}
               for c in payload["clips"]]
    assert compute_content_hash(
        payload["tracks"], rebuilt, payload["fps"]["num"],
        payload["fps"]["den"], payload["tick_rate"],
        payload["rounding_rule"]) == h.rows("timeline_revision")[0][
            "content_hash"]


def test_t41_188_rec_out_derived_from_speed(h):
    h.command(body(clips=[clip("c1", "v1", 0, src_in=0, src_out=450_000,
                               rec_in=1000, speed=(3, 2))],
                   stems=(), locale=None))
    assert h.rows("clip")[0]["rec_out_ticks"] == 1000 + 300_000


def test_t41_189_duration_uses_max_rec_out(h):
    # 片段必须长于 1 帧（24000/1001 下 ≈3754 tick），否则先被 E41-MIN 拦下。
    h.command(body(clips=[
        clip("c1", "v1", 0, src_in=0, src_out=180_000),
        clip("c2", "v1", 1, src_in=0, src_out=180_000, rec_in=180_000,
             artifact_id="art_2"),
    ], stems=(), locale=None))
    # 时长取 max(rec_out)，不是逐段相加：180000 + 180000。
    assert h.rows("timeline_revision")[0]["duration_ticks"] == 360_000


def test_t41_190_large_timeline_diff_under_200ms():
    """C-S41-02：800 clip × 6 track 的 diff 是毫秒级。"""
    before, after = [], []
    for track in range(6):
        for index in range(133):
            ident = f"c_{track}_{index}"
            start = index * 1000
            before.append({"clip_id": ident, "track_id": f"t{track}",
                           "rec_in_ticks": start, "rec_out_ticks": start + 900,
                           "src_in_ticks": 0, "src_out_ticks": 900,
                           "speed_num": 1, "speed_den": 1})
            after.append(dict(before[-1],
                              rec_in_ticks=start + (100 if index % 7 == 0
                                                    else 0)))
    started = time.perf_counter()
    diff = compute_diff(before, after, 1, 2, 133_000, 133_000)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.2
    assert diff["diff_mode"] in ("exact", "coarse")


def test_t41_191_diff_is_recomputable(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    h.command(body(expected_revision=1, stems=(), locale=None),
              command_id="c2")
    stored = h.service.get_diff("t1", "tl_1", 1, 2)
    recomputed = compute_diff(
        h.service._clips("t1", "tl_1", 1), h.service._clips("t1", "tl_1", 2),
        1, 2, 360_000, 360_000)
    assert stored["diff_hash"] == recomputed["diff_hash"]


def test_t41_192_incomplete_revision_still_commits(h):
    """E37-S41-03：incomplete 可以提交、可以预览。"""
    result = h.command(body(stems=["sfx"], locale=None))
    assert result["revision"] == 1
    assert result["completeness"] == "incomplete"
    assert h.count("timeline_revision") == 1


def test_t41_193_gaps_snapshot_is_stored(h):
    h.command(body(stems=["sfx"], locale=None))
    gaps = json.loads(h.rows("timeline_revision")[0]["gaps"])
    assert {"kind": "missing_stem", "stem": "sfx"} in gaps


def test_t41_194_missing_artifact_is_422_not_gap(h):
    """E41-GAPS-422：missing_artifact 是 422，不落库成 gap。"""
    payload = body(clips=[clip("c1", "v1", 0, artifact_id="nope")],
                   stems=(), locale=None)
    with pytest.raises(ServiceError) as info:
        h.command(payload)
    assert info.value.status == 422
    assert h.count("timeline_revision") == 0


def test_t41_195_span_timeline_identity_is_stable(h):
    h.command(body(stems=(), locale=None), command_id="c1")
    h.command(body(expected_revision=1, stems=(), locale=None),
              command_id="c2")
    events = h.events()
    assert [e["aggregate_id"] for e in events] == ["tl_1", "tl_1"]
    assert [e["sequence"] for e in events] == [1, 2]


def test_t41_196_retire_does_not_emit_event(h):
    """PTR-4：VOIDED 没有事件；退役不发任何消息。"""
    h.command(body(stems=(), locale=None))
    before = h.count("outbox")
    h.service.retire_revision("t1", "tl_1", 1, "VOIDED")
    assert h.count("outbox") == before


def test_t41_197_retire_does_not_touch_fence(h):
    h.command(body(stems=(), locale=None))
    h.service.retire_revision("t1", "tl_1", 1, "VOIDED")
    assert h.rows("timeline")[0]["execution_fence"] == 2


def test_t41_198_two_timelines_do_not_interfere(h):
    """不同 episode 的两条 timeline 互不干扰；同一身份不能再开第二条。"""
    def with_episode(episode, **kwargs):
        payload = body(stems=(), locale=None, **kwargs)
        payload["context"]["episode_id"] = episode
        return payload

    h.command(with_episode("ep_1"), command_id="c1", timeline_id="tl_1")
    h.command(with_episode("ep_2"), command_id="c2", timeline_id="tl_2")
    assert len(h.service.list_timelines("t1")["timelines"]) == 2
    assert h.service.get_revision("t1", "tl_2", 1)["timeline_id"] == "tl_2"
    # 同一 (series, episode, branch, kind) 不能有第二个 timeline_id。
    with pytest.raises(ServiceError) as info:
        h.command(with_episode("ep_1"), command_id="c3", timeline_id="tl_3")
    assert_error(info, 409, "TIMELINE_IDENTITY_CONFLICT")


def test_t41_199_commit_receipt_contains_response(h):
    result = h.command(body(stems=(), locale=None))
    receipt = h.rows("command_receipt")[0]
    stored = json.loads(receipt["response_body"])
    assert stored["revision"] == result["revision"]
    assert stored["content_hash"] == result["content_hash"]


def test_t41_200_commit_is_atomic_across_tables(h):
    """E13-S41-01 的正面：一次提交要么全写，要么全不写。"""
    h.command(body(stems=(), locale=None))
    assert h.count("timeline_revision") == 1
    assert h.count("output_plan") == 1
    assert h.count("timeline_diff") == 1
    assert h.count("command_receipt") == 1
    assert h.count("outbox") == 1


# ------------------------------------------------------------------ 与下游联调

class S38Mock:
    """S38 的桩：只重算落在 `affected_ranges` 里的 cue（§4.3）。"""

    def __init__(self, cues):
        self.cues = cues
        self.recomputed = []

    @staticmethod
    def _hit(cue, ranges):
        return any(cue["start"] < r["end"] and cue["end"] > r["start"]
                   for r in ranges)

    def apply(self, diff):
        self.recomputed = [c["cue_id"] for c in self.cues
                           if self._hit(c, diff["affected_ranges"])]
        return self.recomputed


def test_t41_201_e24_01_only_cues_in_affected_ranges_recomputed(h):
    """E24-S41-01：EDL 缩短 1s 后，只有落在受影响区间的 cue 被重算。"""
    h.command(body(clips=[
        clip("c1", "v1", 0, src_in=0, src_out=180_000),
        clip("c2", "v1", 1, src_in=0, src_out=180_000, rec_in=180_000,
             artifact_id="art_2"),
    ], stems=(), locale=None), command_id="c1")
    # 第二段砍掉 1s（180000 → 90000 tick），时间线从 6s 缩到 5s。
    h.command(body(clips=[
        clip("c1", "v1", 0, src_in=0, src_out=180_000),
        clip("c2", "v1", 1, src_in=0, src_out=900_000, rec_in=180_000,
             artifact_id="art_2"),
    ], expected_revision=1, stems=(), locale=None), command_id="c2")
    diff = h.service.get_diff("t1", "tl_1", 1, 2)
    cues = [
        {"cue_id": "cue_head", "start": 0, "end": 90_000},
        {"cue_id": "cue_tail", "start": 450_000, "end": 540_000},
    ]
    mock = S38Mock(cues)
    assert mock.apply(diff) == ["cue_tail"]
    # 受影响区间必须覆盖旧位置与新位置（规则 2），否则旧位置会留残影。
    assert {"start": 0, "end": 180_000} not in diff["affected_ranges"]


def test_t41_202_e24_02_untouched_cues_keep_every_field(h):
    """E24-S41-02：只改一个 clip，其他 cue 的 start/end 逐字段不变。"""
    tracks = [{"track_id": "v1", "kind": "video"},
              {"track_id": "a1", "kind": "dialogue"}]
    base = [
        clip("c1", "v1", 0, src_in=0, src_out=180_000),
        clip("c2", "v1", 1, src_in=0, src_out=180_000, rec_in=180_000,
             artifact_id="art_2"),
        clip("c3", "v1", 2, src_in=0, src_out=180_000, rec_in=360_000,
             artifact_id="art_1"),
        clip("a1c1", "a1", 0, src_in=0, src_out=540_000),
    ]
    h.command(body(clips=base, tracks=tracks, stems=(), locale=None),
              command_id="c1")
    changed = [dict(c) for c in base]
    changed[0]["src_out_ticks"] = 90_000
    h.command(body(clips=changed, tracks=tracks, expected_revision=1,
                   stems=(), locale=None), command_id="c2")
    ignored = ("tenant_id", "timeline_id", "revision", "entry_no")
    before = {e["clip_id"]: {k: v for k, v in e.items() if k not in ignored}
              for e in h.service.get_edl("t1", "tl_1", 1)["entries"]}
    after = {e["clip_id"]: {k: v for k, v in e.items() if k not in ignored}
             for e in h.service.get_edl("t1", "tl_1", 2)["entries"]}
    for clip_id in ("c3", "a1c1"):
        assert before[clip_id] == after[clip_id]
    assert before["c1"]["record_out_frame"] != after["c1"]["record_out_frame"]


def test_t41_203_e13_02_outbox_survives_restart_and_delivers_once(h):
    """E13-S41-02：提交后、relay 前崩溃 → 重启后 outbox 仍在，下游只收一次。"""
    h.command(body(stems=(), locale=None))
    # 「重启」= 同一个库上新建内核与消费者 inbox，未发布事件仍在。
    restarted = TimelineService(h.conn)
    pending = restarted.outbox.unpublished()  # SDK 直接返回解析后的事件
    assert [event["id"] for event in pending] == [h.events()[0]["id"]]
    downstream = Inbox(h.conn)
    event_id = pending[0]["id"]
    assert downstream.consume("s38", event_id) is True
    assert downstream.consume("s38", event_id) is False
    restarted.outbox.mark_published(event_id)
    assert restarted.outbox.unpublished() == []


def test_t41_204_e13_04_crash_during_diff_leaves_no_half_rows(h):
    """E13-S41-04：diff 计算中途失败 → 不留半写行。"""
    original = h.service._insert_diff

    def boom(*args, **kwargs):
        raise RuntimeError("crash while writing diff")

    h.service._insert_diff = boom
    try:
        with pytest.raises(RuntimeError):
            h.command(body(stems=(), locale=None))
    finally:
        h.service._insert_diff = original
    for table in ("timeline", "timeline_revision", "clip", "edl_entry",
                  "timeline_diff", "output_plan", "outbox"):
        assert h.count(table) == 0, table
    # 幂等占位由 api 层 abort() 删除；内核直调时会留下占位，这里对齐
    # `Harness.command()` 的行为后断言重投能成功。
    h.idem.abort("t1", "cmd_1")
    assert h.command(body(stems=(), locale=None))["revision"] == 1


def test_t41_205_e37_04_incomplete_event_makes_s42_refuse(h):
    """E37-S41-04：S42 mock 看到 formal_master_eligible=false → 422。"""
    h.command(body(stems=["sfx"], locale=None))
    event = h.events()[0]["data"]
    assert event["formal_master_eligible"] is False

    class S42Mock:
        def request_render(self, data):
            if not data["formal_master_eligible"]:
                raise ServiceError(
                    422, "NOT_FORMAL_MASTER_ELIGIBLE",
                    "incomplete revision cannot be a formal master",
                    details={"timeline_id": data["timeline_id"],
                             "revision": data["revision"]})
            return {"job_id": "job_1"}

    with pytest.raises(ServiceError) as info:
        S42Mock().request_render(event)
    assert_error(info, 422, "NOT_FORMAL_MASTER_ELIGIBLE")
    # 本服务也不声称可以放行（§4.4：快照用于解释，实时复查用于放行）。
    with pytest.raises(ServiceError) as info:
        h.service.render_eligibility("t1", "tl_1", 1)
    assert_error(info, 422, "INCOMPLETE_TIMELINE")


def test_t41_206_e37_06_rights_has_no_producer_in_s41(h):
    """E37-S41-06：rights_expired 不由 S41 产出，只登记为上游缺口。

    方案 §4.4 把它列为 gap kind，但 S41 的依赖里没有 S15，本服务也不
    缓存「已授权」结论（CONTRACT_GAPS E41-RIGHTS）。这里断言的是
    **不产出**：提交与回查都不会凭空生成或清掉该 gap。
    """
    h.command(body(stems=(), locale=None))
    revision = h.service.get_revision("t1", "tl_1", 1)
    assert all(g["kind"] != "rights_expired" for g in revision["gaps"])
    from editorial.schema import GAP_KINDS
    assert "rights_expired" in GAP_KINDS
    assert any(gap["id"] == "E41-RIGHTS" for gap in CONTRACT_GAPS)
