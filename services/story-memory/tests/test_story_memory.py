"""S16 测试。对照 `plans/S16-story-memory.md` §9 的用例表。

每个用例都断言**能失败的等式**，不只是「没抛异常」：

- E08：checkpoint 链可离线重算；旧 revision 变 `superseded`；已交付集绑定不迁移。
- E09：三个轴分别过滤，`withheld_claim_ids` 必须带上被滤掉的东西。
- E10：分支隔离用逐行快照比对；回滚只追加不删除。
- E11：`minimum_checkpoint` 是硬门，落后返回 409 而不是旧数据。

跨服务的两项（S08 accepted pointer、S01 kill switch）用注入的 callback 模拟，
**默认不注入**——§3.6 要求没接线就不放行，第 `test_commit_without_authority` 钉住它。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from story_memory.service import (
    ServiceError,
    StoryMemoryService,
    Transaction,
    checkpoint_hash,
    genesis_checkpoint,
)

TA = "t_aaa"
TB = "t_bbb"
SERIES = "series_demo"
H1 = "b" * 64
H2 = "c" * 64


def _conn(*, share_thread: bool = False) -> sqlite3.Connection:
    # TestClient 在另一个线程里跑请求；共享内存库必须允许跨线程使用。
    conn = sqlite3.connect(":memory:", isolation_level=None,
                           check_same_thread=not share_thread)
    conn.row_factory = sqlite3.Row
    return conn


def _authority(tenant_id, series_id, manifest_ref):
    """替身：凡是 revision >= 1 且 hash 非全零的 manifest 都算 accepted。"""
    if manifest_ref.get("content_hash") == "0" * 64:
        return {"accepted": False}
    return {"accepted": True, "revision": manifest_ref.get("revision")}


@pytest.fixture()
def svc():
    service = StoryMemoryService(_conn(), manifest_authority=_authority)
    service.create_series(TA, SERIES)
    service.register_clock(TA, SERIES, {"clock_id": "story_main", "clock_kind": "main",
                                        "tick_min": 0, "tick_max": 10000})
    service.register_clock(TA, SERIES, {"clock_id": "story_flash", "clock_kind": "flashback",
                                        "tick_min": 0, "tick_max": 10000,
                                        "parent_clock_id": "story_main"})
    for eid, order, start, end, touched in (
            ("ep001", 1, 0, 500, ["char_linlan", "prop_key"]),
            ("ep002", 2, 500, 1000, ["char_linlan", "prop_key", "char_chenmo"]),
            ("ep003", 3, 1000, 1500, ["char_linlan", "prop_key"]),
            ("ep007", 7, 100, 200, ["char_chenmo"])):
        service.register_episode_window(TA, SERIES, {
            "episode_id": eid, "episode_order": order, "clock_id": "story_main",
            "tick_start": start, "tick_end": end, "touched_entities": touched})
    for eid, etype, name in (("char_linlan", "character", "Linlan"),
                             ("char_chenmo", "character", "Chenmo"),
                             ("prop_key", "prop", "Key"),
                             ("prop_knife", "prop", "Knife"),
                             ("thread_letter", "plot_thread", "Letter"),
                             ("event_a", "story_event", "Event A"),
                             ("event_b", "story_event", "Event B")):
        service.register_entity(TA, SERIES, {"entity_id": eid, "entity_type": etype,
                                            "display_name": name})
    return service


def _manifest(episode_id: str, revision: int = 1) -> dict:
    return {"id": f"manifest_{episode_id}", "revision": revision,
            "content_hash": H1,
            "source_revisions": [{"id": episode_id, "revision": 1,
                                  "content_hash": H2}]}


def _cand(*, candidate_id="cand_1", subject="prop_key", predicate="held_by",
          value="char_chenmo", kind="entity_state", clock="story_main",
          start=500, end=None, episode="ep002", confidence=0.9,
          claim_id=None, supersedes=None, scope=None, extra=None) -> dict:
    cand = {
        "candidate_id": candidate_id,
        "kind": kind,
        "fact": {"subject_id": subject, "predicate": predicate, "value": value},
        "story_validity": {"clock_id": clock, "valid_from_tick": start,
                           "valid_to_tick": end},
        "source_refs": [{"service_id": "S08", "id": episode, "revision": 1,
                         "content_hash": H2, "span": "line 42"}],
        "confidence": confidence,
    }
    if claim_id:
        cand["proposed_claim_id"] = claim_id
    if supersedes:
        cand["supersedes_claim_refs"] = supersedes
    if scope:
        cand["epistemic_scope"] = scope
    if extra:
        cand.update(extra)
    return cand


def _stage(svc, cands, batch="batch_1", episode="ep002", revision=1,
           branch="canon") -> dict:
    return svc.submit_candidates(TA, SERIES, {
        "branch_id": branch, "batch_id": batch,
        "source_manifest_ref": _manifest(episode, revision),
        "candidates": cands})


def _commit(svc, *, expected=0, batch="batch_1", episode="ep002", kind="extract",
            command_id="cmd_1", revision=1, branch="canon") -> dict:
    return svc.commit(TA, SERIES, {
        "branch_id": branch, "expected_checkpoint": expected,
        "commit_kind": kind, "batch_id": batch, "episode_id": episode,
        "source_manifest_ref": _manifest(episode, revision),
        "command_id": command_id})


# ---- E08 · 改前集钥匙归属 -----------------------------------------------

def test_e08_1_supersede_and_checkpoint_recomputable(svc):
    _stage(svc, [_cand()])
    first = _commit(svc)
    assert first["commit_sequence"] == 1
    old_ref = first["changed_claim_refs"][0]

    _stage(svc, [_cand(candidate_id="cand_2", value="char_linlan",
                       claim_id=old_ref["id"], supersedes=[old_ref])], batch="batch_2",
           revision=2)
    second = _commit(svc, expected=1, batch="batch_2", command_id="cmd_2",
                     episode="ep002", revision=2)

    assert second["commit_sequence"] == 2
    assert [r["id"] for r in second["superseded_claim_refs"]] == [old_ref["id"]]

    rows = svc.conn.execute(
        "SELECT revision, status FROM claim_revision WHERE claim_id=?"
        " ORDER BY revision", (old_ref["id"],)).fetchall()
    assert [(r["revision"], r["status"]) for r in rows] == [
        (1, "superseded"), (2, "accepted")]

    # 链必须能离线重算：拿 chain 的输入重算，和落库的 hash 逐字节相等。
    head = svc._branch(TA, SERIES, "canon")
    expected = checkpoint_hash(SERIES, "canon", 2, first["checkpoint_hash"],
                               second["changed_claim_refs"])
    assert head["head_checkpoint_hash"] == expected


def test_e08_2_delivered_episode_binding_is_not_migrated(svc):
    _stage(svc, [_cand()])
    first = _commit(svc, episode="ep002")
    binding = svc.conn.execute(
        "SELECT commit_sequence, episode_id FROM commit_manifest_binding"
        " WHERE episode_id='ep002'").fetchone()
    assert binding["commit_sequence"] == first["commit_sequence"]

    # 新开一个谓词槽：本用例只验证「已交付集的绑定不迁移」，不是 C1。
    _stage(svc, [_cand(candidate_id="cand_2", predicate="stored_at",
                       value="char_linlan", start=1000, episode="ep003")],
           batch="batch_2", episode="ep003", revision=2)
    _commit(svc, expected=1, batch="batch_2", command_id="cmd_2", episode="ep003",
            revision=2)

    still = svc.conn.execute(
        "SELECT commit_sequence FROM commit_manifest_binding WHERE episode_id='ep002'"
    ).fetchone()
    assert still["commit_sequence"] == first["commit_sequence"]


def test_e08_3_closing_interval_without_holder_is_allowed(svc):
    _stage(svc, [_cand()])
    first = _commit(svc)
    claim_id = first["changed_claim_refs"][0]["id"]

    # 「钥匙被藏起来」：held_by 闭合 + 新开 stored_at，两条都要过 C3。
    _stage(svc, [
        _cand(candidate_id="cand_close", value=None, claim_id=claim_id,
              start=500, end=1000, episode="ep003",
              supersedes=[{"service_id": "S16", "id": claim_id,
                           "revision": first["changed_claim_refs"][0]["revision"],
                           "content_hash":
                           first["changed_claim_refs"][0]["content_hash"]}]),
        _cand(candidate_id="cand_store", predicate="stored_at", value="char_linlan",
              start=1000, end=None, episode="ep003"),
    ], batch="batch_2", episode="ep003", revision=2)
    out = _commit(svc, expected=1, batch="batch_2", command_id="cmd_2",
                  episode="ep003", revision=2)
    assert len(out["changed_claim_refs"]) == 2


def test_e08_4_conflicting_value_without_supersedes_is_rejected(svc):
    _stage(svc, [_cand()])
    _commit(svc)

    result = _stage(svc, [_cand(candidate_id="cand_2", value="char_linlan")],
                    batch="batch_2")
    assert result["candidates"][0]["state"] == "rejected"
    assert result["candidates"][0]["errors"][0]["code"] == "CLAIM_CONFLICT"

    head = svc._branch(TA, SERIES, "canon")
    assert head["head_commit_sequence"] == 1  # 正史不变


def test_e08_5_invalidation_bound_excludes_earlier_episodes(svc):
    _stage(svc, [_cand(start=500, end=1000)])
    _commit(svc, episode="ep002")
    out = svc.invalidation_bound(TA, SERIES, {"branch_id": "canon",
                                             "commit_sequence": 1})
    # ep001 (0..500) 与 [500,1000) 不重叠、且 order < E0=2，不进边界。
    assert out["origin_episode_order"] == 2
    assert out["invalidation_bound"] == 2


def test_e08_6_event_carries_seeds_not_a_closure(svc):
    _stage(svc, [_cand()])
    _commit(svc, episode="ep002")
    payload = svc.outbox.unpublished()[0]["data"]
    assert set(payload) == {
        "commit_sequence", "checkpoint_hash", "commit_kind", "previous_sequence",
        "changed_claim_refs", "superseded_claim_refs", "changed_subject_ids",
        "affected_story_window", "source_manifest_ref", "episode_id",
        "episode_order"}
    # 只给种子：没有「哪些集要重做」。
    assert not any(k in payload for k in
                   ("affected_episodes", "impact_plan", "episode_ids"))


# ---- E09 · 闪回 / 观众先知 / 角色不知 ------------------------------------

def test_e09_1_flashback_episode_keeps_its_order_and_window(svc):
    row = svc.conn.execute(
        "SELECT episode_order, clock_id, tick_start FROM episode_story_window"
        " WHERE episode_id='ep007'").fetchone()
    assert (row["episode_order"], row["clock_id"], row["tick_start"]) == (7, "story_main", 100)


def test_e09_2_reveal_after_query_episode_is_withheld(svc):
    conf = {"perspective": "audience", "knower_ids": [], "belief_type": "fact",
            "audience_reveal_episode_order": 7}
    _stage(svc, [_cand(candidate_id="cand_7", subject="char_chenmo",
                       predicate="age", value=44, kind="character_profile",
                       start=100, episode="ep007", scope=conf)],
           episode="ep007")
    _commit(svc, episode="ep007")

    early = svc.query(TA, SERIES, {
        "branch_id": "canon", "minimum_checkpoint": 1,
        "perspective": {"mode": "audience", "audience_as_of_episode_order": 3}})
    assert early["claims"] == []
    assert len(early["withheld_claim_ids"]) == 1

    late = svc.query(TA, SERIES, {
        "branch_id": "canon", "minimum_checkpoint": 1,
        "perspective": {"mode": "audience", "audience_as_of_episode_order": 7}})
    assert len(late["claims"]) == 1


def test_e09_3_unaware_character_gets_nothing_and_it_is_reported(svc):
    secret = {"perspective": "character", "knower_ids": ["char_linlan"],
              "belief_type": "unaware", "audience_reveal_episode_order": 7}
    _stage(svc, [_cand(candidate_id="cand_secret", subject="char_chenmo",
                       predicate="age", value=44, kind="character_profile",
                       start=100, episode="ep007", scope=secret)],
           episode="ep007")
    _commit(svc, episode="ep007")

    out = svc.query(TA, SERIES, {
        "branch_id": "canon", "minimum_checkpoint": 1,
        "perspective": {"mode": "character", "character_ids": ["char_linlan"],
                        "audience_as_of_episode_order": 7}})
    assert out["claims"] == []
    assert len(out["withheld_claim_ids"]) == 1


def test_e09_4_audience_sees_what_character_does_not(svc):
    scope = {"perspective": "character", "knower_ids": ["char_chenmo"],
             "belief_type": "knows", "audience_reveal_episode_order": 2}
    _stage(svc, [_cand(candidate_id="cand_k", subject="char_chenmo",
                       predicate="age", value=44, kind="character_profile",
                       start=100, episode="ep007", scope=scope)],
           episode="ep007")
    _commit(svc, episode="ep007")

    audience = svc.query(TA, SERIES, {
        "branch_id": "canon", "minimum_checkpoint": 1,
        "perspective": {"mode": "audience", "audience_as_of_episode_order": 3}})
    character = svc.query(TA, SERIES, {
        "branch_id": "canon", "minimum_checkpoint": 1,
        "perspective": {"mode": "character", "character_ids": ["char_linlan"],
                        "audience_as_of_episode_order": 3}})
    assert len(audience["claims"]) == 1
    assert character["claims"] == []


def test_e09_5_world_rule_is_visible_to_characters(svc):
    _stage(svc, [_cand(candidate_id="cand_rule", subject="prop_key",
                       predicate="rule_statement", value="keys open doors",
                       kind="world_rule", start=0, episode="ep001",
                       scope={"perspective": "omniscient", "knower_ids": [],
                              "belief_type": "fact",
                              "audience_reveal_episode_order": 1})],
           episode="ep001")
    _commit(svc, episode="ep001")

    out = svc.query(TA, SERIES, {
        "branch_id": "canon", "minimum_checkpoint": 1,
        "perspective": {"mode": "character", "character_ids": ["char_linlan"],
                        "audience_as_of_episode_order": 3}})
    assert [c["kind"] for c in out["claims"]] == ["world_rule"]


def test_e09_6_unaware_knowledge_does_not_grant_visibility(svc):
    # 先有一条事实本体（钥匙在陈默那儿），再让林岚显式 unaware 这条命题。
    _stage(svc, [_cand()])
    first = _commit(svc)
    fact_claim = first["changed_claim_refs"][0]["id"]

    unknown = {"perspective": "character", "knower_ids": ["char_linlan"],
               "belief_type": "unaware", "audience_reveal_episode_order": 1}
    _stage(svc, [_cand(candidate_id="cand_u", kind="knowledge_state",
                       subject="char_linlan", predicate="belief_about",
                       value="char_chenmo", start=500,
                       extra={"proposition_claim_id": fact_claim},
                       episode="ep003", scope=unknown)],
           batch="batch_2", episode="ep003", revision=2)
    _commit(svc, expected=1, batch="batch_2", episode="ep003", revision=2,
            command_id="cmd_2")

    out = svc.query(TA, SERIES, {
        "branch_id": "canon", "minimum_checkpoint": 2,
        "entity_ids": ["prop_key"],
        "perspective": {"mode": "character", "character_ids": ["char_linlan"]}})
    # unaware 是「确认不知道」：它不授权可见，而且比"没记录"更强，
    # 调用方可以据此生成禁则而不是留空。
    assert [c["claim_id"] for c in out["claims"]] == []
    assert fact_claim in out["withheld_claim_ids"]


def test_e09_8_a_claim_cannot_declare_its_own_visibility(svc):
    # §6.4 的闭世界公式只有两个放行口：这条 claim 是 world_rule，或有覆盖查询时点
    # 的 knowledge_state。事实自带 epistemic_scope 说「林岚知道」不算数 —— 否则
    # 一条事实就能自己指定谁看得见它，S16 里就有第二个可见性权威。
    seen = {"perspective": "character", "knower_ids": ["char_linlan"],
            "belief_type": "knows", "audience_reveal_episode_order": 1}
    _stage(svc, [_cand(scope=seen)])
    fact_claim = _commit(svc)["changed_claim_refs"][0]["id"]

    out = svc.query(TA, SERIES, {
        "branch_id": "canon", "minimum_checkpoint": 1,
        "entity_ids": ["prop_key"],
        "perspective": {"mode": "character", "character_ids": ["char_linlan"]}})
    assert [c["claim_id"] for c in out["claims"]] == []
    assert fact_claim in out["withheld_claim_ids"]


def test_e09_7_time_jump_closes_the_old_interval_explicitly(svc):
    _stage(svc, [_cand(candidate_id="cand_a", start=0, end=1500, episode="ep001")],
           episode="ep001")
    first = _commit(svc, episode="ep001")
    claim_id = first["changed_claim_refs"][0]["id"]

    _stage(svc, [_cand(candidate_id="cand_b", value=None, claim_id=claim_id,
                       start=1500, end=None, episode="ep003",
                       supersedes=first["changed_claim_refs"])],
           batch="batch_2", episode="ep003", revision=2)
    _commit(svc, expected=1, batch="batch_2", command_id="cmd_2", episode="ep003",
            revision=2)

    row = svc.conn.execute(
        "SELECT valid_from_tick, valid_to_tick FROM claim_revision WHERE claim_id=?"
        " ORDER BY revision DESC LIMIT 1", (claim_id,)).fetchone()
    # 跳跃不是「取最新」：旧区间显式闭合到 1500，新 revision 从 1500 起。
    assert (row["valid_from_tick"], row["valid_to_tick"]) == (1500, None)
    old = svc.conn.execute(
        "SELECT valid_from_tick, valid_to_tick FROM claim_revision WHERE claim_id=?"
        " AND revision=1", (claim_id,)).fetchone()
    assert (old["valid_from_tick"], old["valid_to_tick"]) == (0, 1500)


# ---- E10 · 分支与候选抽取 ------------------------------------------------

def test_e10_1_fork_does_not_touch_canon(svc):
    _stage(svc, [_cand()])
    _commit(svc)

    before = [tuple(r) for r in svc.conn.execute(
        "SELECT claim_id, revision, commit_sequence FROM canon_effective_claim"
        " WHERE branch_id='canon' ORDER BY claim_id")]
    svc.fork_branch(TA, SERIES, {"branch_id": "exp1", "from_branch_id": "canon",
                                 "command_id": "fork_1"})
    after = [tuple(r) for r in svc.conn.execute(
        "SELECT claim_id, revision, commit_sequence FROM canon_effective_claim"
        " WHERE branch_id='canon' ORDER BY claim_id")]
    assert before == after


def test_e10_2_candidate_never_appears_in_canon(svc):
    _stage(svc, [_cand(candidate_id="cand_pending")])
    # 还没有 commit：候选只在隔离区。
    out = svc.query(TA, SERIES, {"branch_id": "canon", "minimum_checkpoint": 0})
    assert out["claims"] == []
    assert svc.conn.execute("SELECT count(*) FROM claim_revision").fetchone()[0] == 0


def test_e10_3_bad_candidates_are_rejected_with_specific_codes(svc):
    result = _stage(svc, [
        _cand(candidate_id="cand_src",
              extra={"source_refs": [{"service_id": "S08", "id": "ep999",
                                      "revision": 1, "content_hash": H2,
                                      "span": "l1"}]}),
        _cand(candidate_id="cand_ent", subject="char_hallucinated"),
        _cand(candidate_id="cand_pred", predicate="invented_predicate"),
    ])
    codes = {c["candidate_id"]: [e["code"] for e in c["errors"]]
             for c in result["candidates"]}
    assert "SOURCE_NOT_IN_MANIFEST" in codes["cand_src"]
    assert "UNRESOLVED_ENTITY" in codes["cand_ent"]
    assert "UNKNOWN_PREDICATE" in codes["cand_pred"]


def test_e10_4_benign_add_merges_automatically(svc):
    _stage(svc, [_cand()])
    _commit(svc)
    svc.fork_branch(TA, SERIES, {"branch_id": "exp1", "from_branch_id": "canon",
                                 "command_id": "fork_1"})
    _stage(svc, [_cand(candidate_id="new_slot", predicate="stored_at",
                       value="char_linlan", start=1000, episode="ep003")],
           batch="batch_exp", episode="ep003", branch="exp1")
    _commit(svc, branch="exp1", expected=1, batch="batch_exp", episode="ep003",
            revision=2, command_id="cmd_exp")

    preview = svc.preview_merge(TA, SERIES, {"source_branch_id": "exp1",
                                             "target_branch_id": "canon"})
    assert preview["auto_merge"] is True
    out = svc.merge(TA, SERIES, {"source_branch_id": "exp1",
                                 "target_branch_id": "canon",
                                 "expected_checkpoint": 1, "command_id": "merge_1"})
    assert out["commit_kind"] == "merge"


def test_e10_5_holder_conflict_needs_a_human(svc):
    _stage(svc, [_cand()])
    _commit(svc)
    svc.fork_branch(TA, SERIES, {"branch_id": "exp1", "from_branch_id": "canon",
                                 "command_id": "fork_1"})
    _stage(svc, [_cand(candidate_id="other_claim", value="char_linlan", start=500)],
           batch="batch_exp", branch="exp1")
    # 新分支上 (prop_key, held_by) 用一条独立谱系承载，从而形成 HOLDER_CONFLICT。
    _commit(svc, branch="exp1", expected=1, batch="batch_exp", episode="ep002",
            command_id="cmd_exp")

    preview = svc.preview_merge(TA, SERIES, {"source_branch_id": "exp1",
                                             "target_branch_id": "canon"})
    kinds = {c["kind"] for c in preview["conflicts"]}
    assert kinds & {"HOLDER_CONFLICT", "VALUE_CONFLICT"}
    with pytest.raises(ServiceError) as exc:
        svc.merge(TA, SERIES, {"source_branch_id": "exp1",
                               "target_branch_id": "canon",
                               "expected_checkpoint": 1, "command_id": "merge_1"})
    assert exc.value.code == "MERGE_CONFLICT"
    assert exc.value.status == 409


def _fork(svc, name="exp1"):
    svc.fork_branch(TA, SERIES, {"branch_id": name, "from_branch_id": "canon",
                                 "command_id": f"fork_{name}"})
    return name


def test_e10_9_exclusive_group_conflict_needs_a_human(svc):
    # 两集各给同一组互斥事件一个成员：合并时它们互斥，S16 不判谁对。
    _stage(svc, [_cand(candidate_id="d1", subject="event_a",
                       predicate="exclusive_group", value="death_mode",
                       kind="story_event", start=500)])
    _commit(svc)
    _fork(svc)
    _stage(svc, [_cand(candidate_id="d2", subject="event_b",
                       predicate="exclusive_group", value="death_mode",
                       kind="story_event", start=500)],
           batch="batch_exp", branch="exp1", revision=2)
    _commit(svc, branch="exp1", expected=1, batch="batch_exp",
            command_id="cmd_exp", revision=2)

    preview = svc.preview_merge(TA, SERIES, {"source_branch_id": "exp1",
                                             "target_branch_id": "canon"})
    event = [c for c in preview["conflicts"] if c["kind"] == "EVENT_CONFLICT"]
    assert len(event) == 1
    assert event[0]["exclusive_group"] == "death_mode"
    assert event[0]["default"] == "require_human"
    assert preview["auto_merge"] is False


def test_e10_10_causal_break_needs_a_human(svc):
    # 正史里事件 B 由 A 引发。实验分支把 A 改掉（带 supersedes），
    # 于是 B 的 `causes` 指着一个被替代的事件 —— 合并时这是断裂。
    _stage(svc, [_cand(candidate_id="ev_a", subject="event_a",
                       predicate="participants", value=["char_chenmo"],
                       kind="story_event", start=500)])
    first = _commit(svc)
    a_ref = first["changed_claim_refs"][0]
    _stage(svc, [_cand(candidate_id="ev_b", subject="event_b",
                       predicate="causes", value=a_ref["id"],
                       kind="story_event", start=500)],
           batch="batch_2", revision=2)
    _commit(svc, expected=1, batch="batch_2", command_id="cmd_2", revision=2)

    _fork(svc)
    # 分支上把 A 的 participants 换掉，显式带 supersedes。
    _stage(svc, [_cand(candidate_id="ev_a2", subject="event_a",
                       predicate="participants", value=["char_linlan"],
                       kind="story_event", start=500,
                       claim_id=a_ref["id"], supersedes=[a_ref])],
           batch="batch_exp", branch="exp1", revision=3)
    _commit(svc, branch="exp1", expected=1, batch="batch_exp",
            command_id="cmd_exp", revision=3)

    preview = svc.preview_merge(TA, SERIES, {"source_branch_id": "exp1",
                                             "target_branch_id": "canon"})
    broken = [c for c in preview["conflicts"] if c["kind"] == "CAUSAL_BREAK"]
    assert [c["claim_id"] for c in broken] == [a_ref["id"]]
    assert broken[0]["caused_by"]  # 谁引用了这个被替代的事件
    assert broken[0]["default"] == "require_human"


def test_e10_11_identity_drift_is_a_warning_not_a_block(svc):
    _stage(svc, [_cand(candidate_id="ev_a", subject="event_a",
                       predicate="participants", value=["char_chenmo"],
                       kind="story_event", start=500)])
    first = _commit(svc)
    a_ref = first["changed_claim_refs"][0]
    _fork(svc)
    _stage(svc, [_cand(candidate_id="ev_a2", subject="event_a",
                       predicate="participants", value=["char_linlan"],
                       kind="story_event", start=500,
                       claim_id=a_ref["id"])],
           batch="batch_exp", branch="exp1", revision=2)
    _commit(svc, branch="exp1", expected=1, batch="batch_exp",
            command_id="cmd_exp", revision=2)

    preview = svc.preview_merge(TA, SERIES, {"source_branch_id": "exp1",
                                             "target_branch_id": "canon"})
    drift = [c for c in preview["conflicts"] if c["kind"] == "IDENTITY_DRIFT"]
    assert len(drift) == 1
    # warn 不拦合并：decisions_required 只对 require_human 为真。
    assert drift[0]["default"] == "warn"
    assert preview["decisions_required"] is False


def test_e10_12_revert_does_not_borrow_another_branchs_revision(svc):
    # revision 号是 claim 级全局递增。exp1 上编号更大的那条是该分支当前的答案；
    # canon 回滚时必须回到 canon 自己 target 之前的那一版，不能被别分支的编号带走。
    _stage(svc, [_cand()])
    first = _commit(svc)
    claim_id = first["changed_claim_refs"][0]["id"]

    svc.fork_branch(TA, SERIES, {"branch_id": "exp1", "from_branch_id": "canon",
                                 "command_id": "fork_1"})

    # canon 自己走两次：seq2 改成林岚，seq3 再改成无持有者（回滚 seq3 应回到 seq2）。
    _stage(svc, [_cand(candidate_id="c_canon", claim_id=claim_id,
                       value="char_linlan", start=500,
                       supersedes=[first["changed_claim_refs"][0]])],
           batch="batch_2", revision=2)
    _commit(svc, expected=1, batch="batch_2", command_id="cmd_2", revision=2)

    # exp1 在 canon seq2 之后才提交，于是它拿到**更大**的 revision 号，而它自己的
    # commit_sequence 仍是 2 —— 旧实现（只按 revision DESC、不限定分支）会误取它。
    _stage(svc, [_cand(candidate_id="c_exp", claim_id=claim_id,
                       value="char_chenmo", start=500,
                       supersedes=[{"service_id": "S16", "id": claim_id,
                                    "revision": 3}])],
           batch="batch_exp", branch="exp1", revision=2)
    _commit(svc, branch="exp1", expected=1, batch="batch_exp", command_id="cmd_exp",
            revision=2)
    _stage(svc, [_cand(candidate_id="c_canon2", claim_id=claim_id, value=None,
                       start=500, end=600,
                       supersedes=[{"service_id": "S16", "id": claim_id,
                                    "revision": 3}])],
           batch="batch_3", revision=3)
    _commit(svc, expected=2, batch="batch_3", command_id="cmd_3", revision=3)

    svc.commit(TA, SERIES, {"branch_id": "canon", "expected_checkpoint": 3,
                            "commit_kind": "revert", "revert_of_sequence": 3,
                            "command_id": "cmd_rev"})
    eff = svc.conn.execute(
        "SELECT r.fact FROM canon_effective_claim e JOIN claim_revision r"
        "   ON r.tenant_id=e.tenant_id AND r.series_id=e.series_id"
        "  AND r.claim_id=e.claim_id AND r.revision=e.revision"
        " WHERE e.branch_id='canon' AND e.claim_id=?", (claim_id,)).fetchone()
    # 回到 canon 的 seq2（林岚），不是 exp1 上编号更大、发生在 canon 第 2 步之前的
    # 那条，也不是被回滚掉的"无持有者"。
    assert json.loads(eff["fact"])["value"] == "char_linlan"


def test_e10_6_double_resolve_of_a_thread_is_rejected(svc):
    _stage(svc, [_cand(candidate_id="t1", subject="thread_letter",
                       predicate="thread_state", value="resolved",
                       kind="plot_thread", start=500, episode="ep002")])
    _commit(svc)
    result = _stage(svc, [_cand(candidate_id="t2", subject="thread_letter",
                                predicate="thread_state", value="resolved",
                                kind="plot_thread", start=500)],
                    batch="batch_2", revision=2)
    assert result["candidates"][0]["errors"][0]["code"] == "THREAD_CONFLICT"


def test_e10_7_revert_appends_and_keeps_history(svc):
    _stage(svc, [_cand()])
    first = _commit(svc)
    claim_id = first["changed_claim_refs"][0]["id"]
    _stage(svc, [_cand(candidate_id="c2", value="char_linlan", claim_id=claim_id,
                       supersedes=first["changed_claim_refs"])],
           batch="batch_2", revision=2)
    # 不同 revision 的 manifest：同一已接受 manifest 只能产生一次 extract。
    _commit(svc, expected=1, batch="batch_2", command_id="cmd_2", revision=2)

    out = svc.commit(TA, SERIES, {"branch_id": "canon", "expected_checkpoint": 2,
                                  "commit_kind": "revert", "revert_of_sequence": 2,
                                  "command_id": "cmd_revert"})
    assert out["commit_sequence"] == 3
    assert out["commit_kind"] == "revert"

    rows = svc.conn.execute(
        "SELECT revision, status, fact FROM claim_revision WHERE claim_id=?"
        " ORDER BY revision", (claim_id,)).fetchall()
    assert len(rows) == 3  # 一条都没删
    assert json.loads(rows[-1]["fact"])["value"] == "char_chenmo"


def test_e10_8_revert_chain_is_recomputable(svc):
    _stage(svc, [_cand()])
    _commit(svc)
    out = svc.commit(TA, SERIES, {"branch_id": "canon", "expected_checkpoint": 1,
                                  "commit_kind": "revert", "revert_of_sequence": 1,
                                  "command_id": "cmd_revert"})
    head = svc._branch(TA, SERIES, "canon")
    row = svc.conn.execute(
        "SELECT revert_of_sequence, previous_checkpoint_hash FROM memory_commit"
        " WHERE commit_sequence=?", (out["commit_sequence"],)).fetchone()
    assert row["revert_of_sequence"] == 1
    assert checkpoint_hash(SERIES, "canon", out["commit_sequence"],
                           row["previous_checkpoint_hash"],
                           out["changed_claim_refs"]) == head["head_checkpoint_hash"]


# ---- E11 · 索引落后 -----------------------------------------------------

def test_e11_1_watermark_behind_never_falls_back_to_old_data(svc):
    _stage(svc, [_cand()])
    _commit(svc)
    with pytest.raises(ServiceError) as exc:
        svc.query(TA, SERIES, {"branch_id": "canon", "minimum_checkpoint": 30})
    assert exc.value.code == "MEMORY_WATERMARK_BEHIND"
    assert exc.value.details == {"current_checkpoint": 1}
    assert exc.value.status == 409


def test_e11_2_existence_check_answers_not_found_with_checkpoint(svc):
    _stage(svc, [_cand()])
    _commit(svc)
    out = svc.query(TA, SERIES, {
        "branch_id": "canon", "minimum_checkpoint": 1,
        "required_predicates": [{"subject_id": "prop_knife",
                                 "predicate": "held_by"}]})
    assert out["existence_checks"] == [{"predicate": "held_by",
                                        "subject_id": "prop_knife",
                                        "found": False, "checkpoint": 1}]
    # 没查到 prop_knife ≠ prop_knife 没有持有者：existence_checks 带 checkpoint
    # 才是结论。这里不断言 claims 为空 —— 查询没按 entity_ids 收窄，
    # 已存在的 prop_key 本来就该返回。
    assert all(c["fact"]["subject_id"] != "prop_knife" for c in out["claims"])


# ---- 通用：幂等、CAS、跨租户、复查 ---------------------------------------

def test_duplicate_manifest_commit_conflicts(svc):
    _stage(svc, [_cand()])
    _commit(svc)
    svc.conn.execute("DELETE FROM canon_effective_claim")
    with pytest.raises(Exception):
        # 同一 manifest 的第二次 extract 提交撞唯一索引（§1.3）。
        _stage(svc, [_cand(candidate_id="dup")], batch="batch_dup")
        _commit(svc, expected=1, batch="batch_dup", command_id="cmd_dup")


def test_concurrent_cas_loses_cleanly(svc):
    _stage(svc, [_cand()])
    _commit(svc)
    with pytest.raises(ServiceError) as exc:
        _commit(svc, expected=0, command_id="cmd_stale")
    assert exc.value.code == "CHECKPOINT_CONFLICT"
    assert exc.value.details["current_checkpoint"] == 1


def test_cross_tenant_is_404(svc):
    with pytest.raises(ServiceError) as exc:
        svc.query(TB, SERIES, {"branch_id": "canon", "minimum_checkpoint": 0})
    assert exc.value.status == 404
    assert exc.value.code == "NOT_FOUND"


def test_commit_without_authority_is_blocked(svc):
    """§3.6：没接线的实时复查一律不放行，且留下审计。"""
    bare = StoryMemoryService(_conn())
    bare.create_series(TA, SERIES)
    bare.register_clock(TA, SERIES, {"clock_id": "story_main", "clock_kind": "main",
                                     "tick_min": 0, "tick_max": 1000})
    bare.register_entity(TA, SERIES, {"entity_id": "prop_key", "entity_type": "prop",
                                      "display_name": "Key"})
    bare.register_episode_window(TA, SERIES, {"episode_id": "ep002",
                                              "episode_order": 2,
                                              "clock_id": "story_main",
                                              "tick_start": 0, "tick_end": 500})
    bare.submit_candidates(TA, SERIES, {
        "branch_id": "canon", "batch_id": "b",
        "source_manifest_ref": _manifest("ep002"), "candidates": [_cand()]})
    with pytest.raises(ServiceError) as exc:
        bare.commit(TA, SERIES, {"branch_id": "canon", "expected_checkpoint": 0,
                                 "commit_kind": "extract", "batch_id": "b",
                                 "episode_id": "ep002",
                                 "source_manifest_ref": _manifest("ep002"),
                                 "command_id": "cmd"})
    assert exc.value.code == "BLOCKED"
    assert exc.value.details["reason"] == "MANIFEST_AUTHORITY_UNCONFIGURED"
    blocked = bare.conn.execute("SELECT reason FROM commit_blocked").fetchall()
    assert [r["reason"] for r in blocked] == ["MANIFEST_AUTHORITY_UNCONFIGURED"]


def test_frozen_branch_blocks_commit(svc):
    _stage(svc, [_cand()])
    with Transaction(svc.conn):
        svc.conn.execute("UPDATE canon_branch SET status='frozen'")
    with pytest.raises(ServiceError) as exc:
        _commit(svc)
    assert exc.value.details["reason"] == "BRANCH_FROZEN"


def test_kill_switch_blocks_commit(svc):
    _stage(svc, [_cand()])
    svc.kill_switch = lambda tenant_id, series_id: series_id == SERIES
    with pytest.raises(ServiceError) as exc:
        _commit(svc)
    assert exc.value.details["reason"] == "KILL_SWITCH"


# ---- HTTP 层 ------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch, tmp_path):
    """按路径加载 `api/app.py`。

    不能平名 `import app`：别的服务也有 `api/app.py` 且可能先入 `sys.modules`，
    平名导入会拿到另一份路由表。
    """
    monkeypatch.setenv("S16_DB", str(tmp_path / "s16.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s16_api_app", path)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    sys.modules["s16_api_app"] = api

    conn = _conn(share_thread=True)
    service = StoryMemoryService(conn, manifest_authority=_authority)
    monkeypatch.setattr(api, "_conn", conn)
    monkeypatch.setattr(api, "service", service)
    from idempotency_sdk import SqliteIdempotencyStore
    monkeypatch.setattr(api, "_idem", SqliteIdempotencyStore(conn))
    service.create_series(TA, SERIES)
    return TestClient(api.app), service


def test_http_query_reports_contract_gaps(client):
    api_client, _service = client
    resp = api_client.post(f"/series/{SERIES}/memory/query",
                           json={"branch_id": "canon", "minimum_checkpoint": 0},
                           headers={"X-Tenant-Id": TA})
    assert resp.status_code == 200
    body = resp.json()
    # openapi 只允许 {checkpoint, claims}；plan §6 多要的四个键都在。
    assert {"checkpoint", "claims", "checkpoint_hash", "withheld_claim_ids",
            "existence_checks", "unresolved_conflicts"} <= set(body)
    assert body["x-contract-gaps"]  # 冲突不藏着


def test_catalog_entry_paths_are_not_routed(client):
    """catalog / service.yaml / service.md 的入口在本服务是 404。

    这是 `CONTRACT_GAPS` 第 4 条。断言两个方向都成立：catalog 的路径不通，
    openapi 的路径通——否则这条探针只是「路径不存在」，证明不了是路径分歧。
    """
    api_client, _service = client
    for path in ("/memory-commits", "/memory-queries"):
        assert api_client.post(
            path, json={},
            headers={"X-Tenant-Id": TA, "Idempotency-Key": "k"},
            follow_redirects=False).status_code == 404
    ok = api_client.post(f"/series/{SERIES}/memory/query",
                         json={"branch_id": "canon", "minimum_checkpoint": 0},
                         headers={"X-Tenant-Id": TA})
    assert ok.status_code == 200


def test_readiness_publishes_the_gaps(client):
    """readiness 与 query 响应吐的是同一份缺口，且每条都非空、不重复。"""
    api_client, _service = client
    body = api_client.get("/health/readiness").json()
    assert body["status"] == "ready"
    gaps = body["contract_gaps"]
    assert gaps and all(isinstance(gap, str) and gap.strip() for gap in gaps)
    assert len(gaps) == len(set(gaps))
    assert any("/memory-commits" in gap and "/series/" in gap for gap in gaps)


def test_http_replays_idempotent_command(client):
    api_client, service = client
    service.register_clock(TA, SERIES, {"clock_id": "story_main",
                                        "clock_kind": "main",
                                        "tick_min": 0, "tick_max": 1000})
    for eid, etype in (("prop_key", "prop"), ("char_chenmo", "character")):
        service.register_entity(TA, SERIES, {"entity_id": eid,
                                             "entity_type": etype,
                                             "display_name": eid})
    service.register_episode_window(TA, SERIES, {"episode_id": "ep002",
                                                 "episode_order": 2,
                                                 "clock_id": "story_main",
                                                 "tick_start": 500, "tick_end": 1000})
    service.submit_candidates(TA, SERIES, {
        "branch_id": "canon", "batch_id": "b",
        "source_manifest_ref": _manifest("ep002"), "candidates": [_cand()]})

    body = {"branch_id": "canon", "expected_checkpoint": 0,
            "commit_kind": "extract", "batch_id": "b", "episode_id": "ep002",
            "source_manifest_ref": _manifest("ep002")}
    first = api_client.post(f"/series/{SERIES}/memory/commits", json=body,
                            headers={"X-Tenant-Id": TA,
                                     "Idempotency-Key": "cmd_1"})
    second = api_client.post(f"/series/{SERIES}/memory/commits", json=body,
                             headers={"X-Tenant-Id": TA,
                                      "Idempotency-Key": "cmd_1"})
    assert first.status_code == 202, first.json()
    assert second.json()["commit_sequence"] == first.json()["commit_sequence"]
    assert second.json()["replayed"] is True
    assert service._branch(TA, SERIES, "canon")["head_commit_sequence"] == 1


def test_http_rejects_tenants_that_disagree_with_the_signed_context(client):
    api_client, _service = client
    resp = api_client.post(f"/series/{SERIES}/memory/query",
                           json={"tenant_id": TB, "branch_id": "canon",
                                 "minimum_checkpoint": 0},
                           headers={"X-Tenant-Id": TA})
    assert resp.status_code == 403
    assert resp.json()["code"] == "TENANT_MISMATCH"
