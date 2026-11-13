"""S17 测试。对照 `plans/S17-continuity.md` §9 的用例表。

两处替身，都刻意选了"会失败"的形态：

- `memory_query` 是一个**真的 S16 实例**（内存库），不是 dict 桩。这样水位门、
  闭世界可见性、knowledge_state 全部由 S16 自己的规则回答 —— 用例若把 S17 写成
  "自己复刻可见性公式"，E09_4 / E09_5 立刻会分叉。
- `shot_source` 是 dict 查表，缺什么就返回 None，于是 MISSING_EVIDENCE 路径可测。

每个用例断言能失败的具体值。`test_e11_1` 的三条 assert 一起才能证明"abstain 而不是
空报告"：verdict、`pass` 不出现、以及 `abstain_count` 等于请求的镜头数。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from story_memory.service import StoryMemoryService

from continuity.service import ContinuityService, ServiceError

TA = "t_aaa"
TB = "t_bbb"
SERIES = "series_demo"
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64


def _conn(*, share_thread: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None,
                           check_same_thread=not share_thread)
    conn.row_factory = sqlite3.Row
    return conn


def _authority(tenant_id, series_id, manifest_ref):
    return {"accepted": True, "revision": manifest_ref.get("revision")}


# ---- S16 侧的真实现代：正史是它算的，S17 只是问 -------------------------

def _memory(svc: StoryMemoryService):
    def query(tenant_id: str, series_id: str, body: dict) -> dict:
        return svc.query(tenant_id, series_id, body)
    return query


class World:
    """一个最小但完整的故事：林岚手上有一把钥匙，陈默不知道它的存在。"""

    def __init__(self):
        # TestClient 在另一个线程里发请求，注入的 S16 也要能跨线程用。
        self.mem = StoryMemoryService(_conn(share_thread=True),
                                      manifest_authority=_authority)
        self.mem.create_series(TA, SERIES)
        self.mem.register_clock(TA, SERIES, {"clock_id": "story_main",
                                             "clock_kind": "main",
                                             "tick_min": 0, "tick_max": 10000})
        for eid, order, start, end in (("ep001", 1, 0, 500),
                                       ("ep002", 2, 500, 1000),
                                       ("ep003", 3, 1000, 1500)):
            self.mem.register_episode_window(TA, SERIES, {
                "episode_id": eid, "episode_order": order,
                "clock_id": "story_main", "tick_start": start,
                "tick_end": end})
        for eid, etype in (("char_linlan", "character"),
                           ("char_chenmo", "character"),
                           ("prop_key", "prop"),
                           ("prop_knife", "prop"),
                           ("thread_letter", "plot_thread"),
                           ("event_a", "story_event"),
                           ("event_b", "story_event")):
            self.mem.register_entity(TA, SERIES, {
                "entity_id": eid, "entity_type": etype, "display_name": eid})
        self.head = 0
        self.shots: dict[str, dict] = {}

    # -- 正史写入 -------------------------------------------------------

    def manifest(self, episode="ep002", revision=1) -> dict:
        return {"id": f"manifest_{episode}", "revision": revision,
                "content_hash": H1,
                "source_revisions": [{"id": episode, "revision": 1,
                                      "content_hash": H2}]}

    def claim(self, *, claim_id, kind, subject, predicate, value,
              tick=500, episode="ep002", extra=None,
              scope=None, ref_revision=1) -> dict:
        cand = {
            "candidate_id": f"cand_{claim_id}_{self.head}",
            "proposed_claim_id": claim_id,
            "kind": kind,
            "fact": {"subject_id": subject, "predicate": predicate,
                     "value": value},
            "story_validity": {"clock_id": "story_main",
                               "valid_from_tick": tick, "valid_to_tick": None},
            "source_refs": [{"service_id": "S08", "id": episode,
                             "revision": ref_revision,
                             "content_hash": H2, "span": "line 42"}],
            "confidence": 0.9,
        }
        if extra:
            cand.update(extra)
        if scope:
            cand["epistemic_scope"] = scope
        return cand

    def commit(self, candidates, *, revision=1, branch="canon") -> dict:
        """manifest 的集与候选的来源集必须一致（R1）。从候选自己取，别手传。"""
        episode = candidates[0]["source_refs"][0]["id"]
        batch = f"batch_{self.head + 1}"
        manifest = self.manifest(episode, revision)
        self.mem.submit_candidates(TA, SERIES, {
            "branch_id": branch, "batch_id": batch,
            "source_manifest_ref": manifest, "candidates": candidates})
        result = self.mem.commit(TA, SERIES, {
            "branch_id": branch, "expected_checkpoint": self.head,
            "commit_kind": "extract", "batch_id": batch,
            "episode_id": episode, "source_manifest_ref": manifest,
            "command_id": f"cmd_{self.head + 1}"})
        self.head = result["commit_sequence"]
        return result

    def checkpoint(self, branch="canon") -> dict:
        row = self.mem._branch(TA, SERIES, branch)
        return {"branch_id": branch,
                "commit_sequence": row["head_commit_sequence"],
                "content_hash": row["head_checkpoint_hash"]}

    # -- 镜头 ------------------------------------------------------------

    def shot(self, shot_id, *, scene_id="sc_1", duration_ms=1000,
             observations=(), **extra) -> None:
        self.shots[shot_id] = {"shot_id": shot_id, "scene_id": scene_id,
                               "duration_ms": duration_ms,
                               "observations": list(observations), **extra}

    def shot_source(self, tenant_id, series_id, branch_id, shot_id):
        return self.shots.get(shot_id)


def _obs(entity_id, predicate, value, *, confidence=0.95,
         category=None, pool="text_logic", **extra) -> dict:
    out = {"entity_id": entity_id, "predicate": predicate, "value": value,
           "confidence": confidence, "pool": pool,
           "evidence_refs": [{"kind": "keyframe", "ref": f"kf_{entity_id}"}]}
    if category:
        out["category"] = category
    out.update(extra)
    return out


@pytest.fixture()
def world():
    w = World()
    w.commit([w.claim(claim_id="claim_key_hold", kind="entity_state",
                      subject="prop_key", predicate="held_by",
                      value="char_linlan")])
    w.commit([w.claim(claim_id="claim_linlan_injury", kind="entity_state",
                      subject="char_linlan", predicate="injury",
                      value="bandaged_left_hand", tick=1000, episode="ep003")])
    w.shot("shot_3_1", observations=[
        _obs("prop_key", "held_by", "char_linlan")])
    return w


def _service(world, *, memory=None, shots=True) -> ContinuityService:
    return ContinuityService(
        _conn(),
        memory_query=memory or _memory(world.mem),
        shot_source=world.shot_source if shots else None)


def _request(world, *, shots=("shot_3_1",), profile="full", episode="ep003",
             episode_order=3, checkpoint=None, command_id="cmd_cont_1",
             **extra) -> dict:
    body = {
        "command_id": command_id,
        "series_id": SERIES,
        "episode_id": episode,
        "episode_order": episode_order,
        "manifest_ref": {"id": f"manifest_{episode}", "revision": 4,
                         "content_hash": H1},
        "memory_checkpoint": checkpoint or world.checkpoint(),
        "check_profile": profile,
        "scope": {"shot_ids": list(shots)},
    }
    body.update(extra)
    return body


# ---- E11 · 水位落后与 checkpoint 不一致 ---------------------------------

def test_e11_1_watermark_behind_abstains_and_never_passes(world):
    svc = _service(world)
    ahead = {**world.checkpoint(), "commit_sequence": 30,
             "content_hash": H3}
    result = svc.run_check(TA, _request(world, checkpoint=ahead))

    assert result["verdict"] == "abstained"
    assert result["reason"] == "MEMORY_WATERMARK_BEHIND"
    assert result["defect_count"] == 0
    # abstain_count 记的是"本来要查的量"，不是 0 —— 否则 abstain 和干净报告分不开。
    assert result["abstain_count"] == 1
    assert result["coverage_ratio"] == 0.0


def test_e11_2_checkpoint_hash_mismatch_abstains(world):
    svc = _service(world)
    wrong = {**world.checkpoint(), "content_hash": H3}
    result = svc.run_check(TA, _request(world, checkpoint=wrong))

    assert result["verdict"] == "abstained"
    assert result["reason"] == "CHECKPOINT_MISMATCH"


def test_e11_3_report_pins_the_checkpoint_it_used(world):
    svc = _service(world)
    result = svc.run_check(TA, _request(world))
    stored = svc.get_report(TA, result["report_id"])

    assert stored["report"]["memory_checkpoint"]["commit_sequence"] == world.head
    assert stored["report"]["covered_scope"]["shot_ids"] == ["shot_3_1"]


# ---- E09 · 闪回 / 观众先知 / 角色不知 ------------------------------------

def test_e09_1_declared_flashback_suppresses_the_regression(world):
    world.shot("shot_3_2", diegetic_frame="flashback", frame_declared=True,
               observations=[_obs("char_linlan", "injury", "healthy")])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["disposition"] for d in stored["defects"]] == ["suppress"]
    assert stored["defects"][0]["suppression_reason"] == "DREAM_OR_FLASHBACK_FRAME"
    # 压制的缺陷仍在报告里，带理由 —— 这是审计证据，不是"没检出"。
    assert result["defect_count"] == 0
    assert result["verdict"] == "pass"


def test_e09_2_undeclared_flashback_is_a_defect(world):
    world.shot("shot_3_2", diegetic_frame="flashback", frame_declared=False,
               observations=[_obs("char_linlan", "injury", "healthy")])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["category"] for d in stored["defects"]] == ["FLASHBACK_MISFRAME"]
    assert stored["defects"][0]["severity"] == "medium"
    assert result["verdict"] == "defects"


def test_e09_3_subjective_misrepresentation_is_suppressed(world):
    world.shot("shot_3_2", pov_of="char_linlan", dialogue_intent="misleading",
               observations=[_obs("prop_key", "held_by", "char_chenmo")])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert stored["defects"][0]["disposition"] == "suppress"
    assert (stored["defects"][0]["suppression_reason"]
            == "SUBJECTIVE_MISREPRESENTATION")


def test_e09_4_character_says_what_they_cannot_know(world):
    """陈默不知道钥匙在林岚手上，却在全知镜头里说出来了。"""
    world.shot("shot_3_2", observations=[
        _obs("char_chenmo", "held_by", "knows", category="KNOWLEDGE_VIOLATION",
             belief="knows", proposition_claim_id="claim_key_hold",
             evidence_refs=[{"kind": "script_span", "span": "line 88"}])])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["category"] for d in stored["defects"]] == ["KNOWLEDGE_VIOLATION"]
    assert stored["defects"][0]["severity"] == "blocking"
    assert stored["defects"][0]["disposition"] == "raise"


def test_blocking_script_defect_without_span_drops_to_needs_human(world):
    """§5.3 末句：同一缺陷，证据里没有剧本出处 → 不阻断流水线。

    证据只有关键帧、指不到剧本行，S39 无从下手，所以交人而不是判 raise。
    """
    world.shot("shot_3_2", observations=[
        _obs("char_chenmo", "held_by", "knows", category="KNOWLEDGE_VIOLATION",
             belief="knows", proposition_claim_id="claim_key_hold")])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["category"] for d in stored["defects"]] == ["KNOWLEDGE_VIOLATION"]
    assert stored["defects"][0]["disposition"] == "needs_human"
    assert stored["defects"][0]["subcategory"] == "missing_source_span"


def test_e09_5_audience_knowing_is_not_character_knowing(world):
    """观众的揭示进度与角色是否知情是两条轴。**用错轴两个方向都会误报。**

    这是一组两格交叉，只有两格都过才说明 S17 没把观众轴当角色轴：

    - 观众已揭示（`order=1`），陈默 `unaware`：镜头里他不知道 → 不报缺陷。
      若拿观众轴当角色轴，会推出"角色知道"，这条本该干净的反而报缺陷。
    - 林岚 `knows`，但该命题对观众 gate 到第 5 集（当前是 ep3）：镜头里她知道
      → 不报缺陷。若把 `audience_as_of_episode_order` 传进角色查询，S16 会把
      命题收进 `withheld_claim_ids`，角色变成"不知道"，反转成 KNOWLEDGE_VIOLATION。
    """
    early = {"perspective": "omniscient", "knower_ids": [],
             "belief_type": "fact", "audience_reveal_episode_order": 1}
    world.commit([world.claim(claim_id="claim_knife_hold", kind="entity_state",
                              subject="prop_knife", predicate="held_by",
                              value="char_linlan", scope=early)],
                 revision=2)

    late = {"perspective": "omniscient", "knower_ids": [],
            "belief_type": "fact", "audience_reveal_episode_order": 5}
    world.commit([world.claim(claim_id="claim_key_hold", kind="entity_state",
                              subject="prop_key", predicate="held_by",
                              value="char_linlan", tick=1000, episode="ep003",
                              scope=late)],
                 revision=3)
    world.commit([world.claim(
        claim_id="claim_linlan_knows", kind="knowledge_state",
        subject="char_linlan", predicate="belief_about", value="char_linlan",
        tick=500, episode="ep002",
        extra={"proposition_claim_id": "claim_key_hold"},
        scope={"perspective": "character", "knower_ids": ["char_linlan"],
               "belief_type": "knows", "audience_reveal_episode_order": 3})],
        revision=4)

    world.shot("shot_3_2", observations=[
        _obs("char_chenmo", "held_by", "unaware",
             category="KNOWLEDGE_VIOLATION", belief="unaware",
             proposition_claim_id="claim_knife_hold"),
        _obs("char_linlan", "held_by", "knows",
             category="KNOWLEDGE_VIOLATION", belief="knows",
             proposition_claim_id="claim_key_hold")])

    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))

    assert result["defect_count"] == 0
    assert result["verdict"] in ("pass", "pass_with_warnings")


def test_e09_6_world_rule_allows_the_superpower(world):
    """观测援引了那条规则，并断言落在它的例外内 → 合法特效。"""
    world.commit([world.claim(claim_id="claim_wr_shift", kind="world_rule",
                              subject="char_linlan", predicate="rule_statement",
                              value="may teleport once per night")],
                 revision=2)
    world.shot("shot_3_2", observations=[
        _obs("char_linlan", "rule_statement", "teleported",
             category="WORLD_RULE_BREACH",
             evidence_refs=[{"kind": "world_rule_ref", "id": "claim_wr_shift",
                             "within_exception": True}])])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["disposition"] for d in stored["defects"]] == ["suppress"]
    assert stored["defects"][0]["suppression_reason"] == "WORLD_RULE_ALLOWS"


def test_any_world_rule_does_not_blanket_excuse_the_entity(world):
    """一条"会飞"的规则不能压掉同一角色上的伤势回退。

    回归用例。修之前 `_world_rule_allows` 只问"这个主体有没有 world_rule"，
    于是 E09-1/2/3/6 之外任何一条无关规则都会把该实体的所有缺陷压成 pass。
    """
    world.commit([world.claim(claim_id="claim_wr_fly", kind="world_rule",
                              subject="char_linlan", predicate="rule_statement",
                              value="may fly", tick=100, episode="ep001")],
                 revision=2)
    world.shot("shot_3_1", observations=[
        _obs("char_linlan", "injury", "healthy")])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_1",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["category"] for d in stored["defects"]] == ["INJURY_REGRESSION"]
    assert stored["defects"][0]["disposition"] == "raise"
    assert result["verdict"] == "defects"


def test_citing_a_rule_without_claiming_the_exception_is_not_legal(world):
    """引用规则 ≠ 落在例外内。只给 id 不给例外断言，仍然报缺陷。"""
    world.commit([world.claim(claim_id="claim_wr_fly", kind="world_rule",
                              subject="char_linlan", predicate="rule_statement",
                              value="may fly", tick=100, episode="ep001")],
                 revision=2)
    world.shot("shot_3_1", observations=[
        _obs("char_linlan", "injury", "healthy",
             evidence_refs=[{"kind": "world_rule_ref",
                             "id": "claim_wr_fly", "within_exception": False}])])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_1",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["category"] for d in stored["defects"]] == ["INJURY_REGRESSION"]
    assert stored["defects"][0]["disposition"] == "raise"


def test_world_rule_breach_is_not_excused_by_the_same_rule(world):
    """§2.3 `WORLD_RULE_BREACH + conf≥0.80 → blocking` 必须真的能触发。

    回归用例。修之前这条检查会把**自己**压掉：期望来源就是那条规则，
    而 `_world_rule_allows` 只问"有没有规则" —— 于是 blocking 行是死码。
    """
    world.commit([world.claim(claim_id="claim_wr_once", kind="world_rule",
                              subject="char_linlan", predicate="rule_statement",
                              value="may teleport once per night", tick=100,
                              episode="ep001")], revision=2)
    world.shot("shot_3_2", observations=[
        _obs("char_linlan", "rule_statement", "teleported_twice",
             category="WORLD_RULE_BREACH",
             evidence_refs=[{"kind": "world_rule_ref",
                             "id": "claim_wr_once",
                             "within_exception": False},
                            # §5.3：剧本类 blocking 必须带 source span，否则按
                            # 规则降级成 needs_human。这里要验的是 `§2.3` 那一行
                            # 本身，所以把 span 给足。
                            {"kind": "script_span",
                             "span": "ep002.md:210-214"}])])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["category"] for d in stored["defects"]] == ["WORLD_RULE_BREACH"]
    assert stored["defects"][0]["disposition"] == "raise"
    assert stored["defects"][0]["severity"] == "blocking"
    assert result["verdict"] == "defects"


def _linlan_knows_key(world, *, revision):
    """林岚知道钥匙在林岚手上 —— 一条真正落进 `knowledge_state` 投影的 claim。"""
    world.commit([world.claim(
        claim_id="claim_linlan_knows", kind="knowledge_state",
        subject="char_linlan", predicate="belief_about", value="char_linlan",
        tick=500, episode="ep002",
        extra={"proposition_claim_id": "claim_key_hold"},
        scope={"perspective": "character", "knower_ids": ["char_linlan"],
               "belief_type": "knows", "audience_reveal_episode_order": 3})],
        revision=revision)


def test_e09_7_unmarked_deception_is_reported_as_annotation_gap(world):
    """林岚知道钥匙在哪，却说"我不知道"。没标 deception → 报标注缺失。"""
    _linlan_knows_key(world, revision=2)
    world.shot("shot_3_2", observations=[
        _obs("char_linlan", "held_by", "unaware",
             category="KNOWLEDGE_VIOLATION", belief="unaware",
             proposition_claim_id="claim_key_hold")])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["category"] for d in stored["defects"]] == [
        "UNMARKED_KNOWLEDGE_CONTRADICTION"]
    assert stored["defects"][0]["severity"] == "medium"
    assert stored["defects"][0]["disposition"] == "raise"


def test_annotated_deception_is_suppressed_with_reason(world):
    """同一条撒谎台词，标了 `dialogue_intent=deception` → 合法，留痕 suppressed。"""
    _linlan_knows_key(world, revision=2)
    world.shot("shot_3_2", dialogue_intent="deception", observations=[
        _obs("char_linlan", "held_by", "unaware",
             category="KNOWLEDGE_VIOLATION", belief="unaware",
             proposition_claim_id="claim_key_hold")])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["disposition"] for d in stored["defects"]] == ["suppress"]
    assert stored["defects"][0]["suppression_reason"] == "CHARACTER_DECEPTION"
    assert result["defect_count"] == 0


# ---- 检查能力 ------------------------------------------------------------

def test_injury_regression_is_raised_as_high(world):
    world.shot("shot_3_2", observations=[
        _obs("char_linlan", "injury", "healthy")])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["category"] for d in stored["defects"]] == ["INJURY_REGRESSION"]
    assert stored["defects"][0]["severity"] == "high"
    assert stored["defects"][0]["disposition"] == "raise"


def test_low_confidence_abstains_instead_of_downgrading(world):
    world.shot("shot_3_2", observations=[
        _obs("char_linlan", "injury", "healthy", confidence=0.4)])
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert stored["defects"][0]["disposition"] == "abstain"
    assert stored["defects"][0]["severity"] is None
    assert result["abstain_count"] == 1
    assert result["defect_count"] == 0


def test_missing_shot_records_evidence_gap_not_silence(world):
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_9",)))
    stored = svc.get_report(TA, result["report_id"])

    assert [d["category"] for d in stored["defects"]] == ["MISSING_EVIDENCE"]
    assert stored["defects"][0]["disposition"] == "needs_human"
    assert result["coverage_ratio"] == 0.0


def test_low_coverage_cannot_verdict_pass(world):
    """6 个镜头只覆盖了 2 个量的时长 → 0.33，verdict 不能是 pass。"""
    for i in range(6):
        world.shot(f"shot_x{i}", duration_ms=1000)
    world.shot("shot_x0", duration_ms=1000, observations=[
        _obs("prop_key", "held_by", "char_linlan")])
    world.shot("shot_x1", duration_ms=1000, observations=[
        _obs("char_linlan", "injury", "bandaged_left_hand")])
    svc = _service(world)
    result = svc.run_check(TA, _request(
        world, shots=tuple(f"shot_x{i}" for i in range(6))))

    assert result["coverage_ratio"] < 0.85
    assert result["verdict"] == "pass_with_warnings"
    stored = svc.get_report(TA, result["report_id"])
    assert "COVERAGE_GAP" in {d["category"] for d in stored["defects"]}


def test_axis_crossing_is_needs_human_medium(world):
    world.shot("shot_3_1", camera={"axis_degrees": 10})
    world.shot("shot_3_2", camera={"axis_degrees": 200})
    svc = _service(world)
    result = svc.run_check(TA, _request(world, shots=("shot_3_1", "shot_3_2")))
    stored = svc.get_report(TA, result["report_id"])

    axis = [d for d in stored["defects"]
            if d["category"] == "SCREEN_DIRECTION_BREACH"]
    assert len(axis) == 1
    assert axis[0]["disposition"] == "needs_human"
    assert axis[0]["severity"] == "medium"


def test_double_payoff_of_one_thread_is_blocking(world):
    """正史里这封信用 `event_a` 回收；本集剧本又用 `event_b` 回收一次。

    判据必须是"剧本 vs 正史"。S16 的 `claim` 表对 `(kind, subject_id, predicate)`
    有唯一约束，一个伏笔只有一条 `resolved_by_event` 谱系 —— 想在正史里放两条
    互相矛盾的回收，结构上就写不进去。
    """
    world.commit([world.claim(claim_id="event_a", kind="story_event",
                              subject="event_a", predicate="participants",
                              value=["char_linlan"], tick=900, episode="ep002")],
                 revision=2)
    world.commit([world.claim(claim_id="event_b", kind="story_event",
                              subject="event_b", predicate="participants",
                              value=["char_linlan"], tick=1200, episode="ep003")],
                 revision=3)
    world.commit([world.claim(claim_id="claim_thread_resolution", kind="plot_thread",
                              subject="thread_letter", predicate="resolved_by_event",
                              value="event_a", tick=1000, episode="ep003")],
                 revision=4)
    world.shot("shot_3_1", observations=[
        _obs("thread_letter", "resolved_by_event", "event_b",
             evidence_refs=[{"kind": "script_span", "span": "line 120"}])])
    svc = _service(world)
    result = svc.run_check(TA, _request(world))
    stored = svc.get_report(TA, result["report_id"])

    double = [d for d in stored["defects"]
              if d["category"] == "THREAD_DOUBLE_PAYOFF"]
    assert len(double) == 1
    assert double[0]["severity"] == "blocking"
    assert double[0]["disposition"] == "raise"
    assert double[0]["expected"]["resolved_by_event"] == "event_a"
    assert double[0]["observed"]["resolved_by_event"] == "event_b"
    # §5.3：阻塞级剧本缺陷必须带 span，否则会降成 needs_human。
    assert any("span" in ref for ref in double[0]["evidence_refs"])


def test_second_payoff_without_script_span_drops_to_needs_human(world):
    """同一缺陷，证据里没有剧本出处 → 不阻断流水线（§5.3 末句）。"""
    world.commit([world.claim(claim_id="event_a", kind="story_event",
                              subject="event_a", predicate="participants",
                              value=["char_linlan"], tick=900, episode="ep002")],
                 revision=2)
    world.commit([world.claim(claim_id="claim_thread_resolution", kind="plot_thread",
                              subject="thread_letter", predicate="resolved_by_event",
                              value="event_a", tick=1000, episode="ep003")],
                 revision=3)
    world.shot("shot_3_1", observations=[
        _obs("thread_letter", "resolved_by_event", "event_b",
             evidence_refs=[{"kind": "keyframe", "ref": "kf_letter"}])])
    svc = _service(world)
    result = svc.run_check(TA, _request(world))
    stored = svc.get_report(TA, result["report_id"])

    double = [d for d in stored["defects"]
              if d["category"] == "THREAD_DOUBLE_PAYOFF"]
    assert len(double) == 1
    assert double[0]["disposition"] == "needs_human"
    assert double[0]["subcategory"] == "missing_source_span"


# ---- 边界与防越权 --------------------------------------------------------

def test_cross_tenant_report_read_is_404(world):
    svc = _service(world)
    result = svc.run_check(TA, _request(world))
    with pytest.raises(ServiceError) as excinfo:
        svc.get_report(TB, result["report_id"])
    assert excinfo.value.status == 404


def test_exemption_without_justification_is_rejected(world):
    svc = _service(world)
    with pytest.raises(sqlite3.IntegrityError):
        svc.conn.execute(
            "INSERT INTO exemption (tenant_id, series_id, exemption_id, category,"
            " match_scope, justification, basis, approved_by_principal,"
            " approved_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (TA, SERIES, "exm_bad", "INJURY_REGRESSION", '{"shot_ids":["shot_3_2"]}',
             "", "director_decision", "principal_1", "2026-01-01T00:00:00Z"))


def test_expired_exemption_does_not_apply(world):
    world.shot("shot_3_2", observations=[
        _obs("char_linlan", "injury", "healthy")])
    svc = _service(world)
    svc.conn.execute(
        "INSERT INTO exemption (tenant_id, series_id, exemption_id, category,"
        " match_scope, justification, basis, approved_by_principal, approved_at,"
        " expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TA, SERIES, "exm_old", "INJURY_REGRESSION",
         '{"shot_ids":["shot_3_2"]}', "old decision", "director_decision",
         "principal_1", "2020-01-01T00:00:00Z", "2020-02-01T00:00:00Z"))

    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert stored["defects"][0]["disposition"] == "raise"
    assert stored["defects"][0]["suppression_reason"] is None
    assert stored["exemption_applications"] == []


def test_live_exemption_suppresses_and_is_recorded(world):
    world.shot("shot_3_2", observations=[
        _obs("char_linlan", "injury", "healthy")])
    svc = _service(world)
    svc.conn.execute(
        "INSERT INTO exemption (tenant_id, series_id, exemption_id, category,"
        " match_scope, justification, basis, approved_by_principal, approved_at,"
        " expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TA, SERIES, "exm_017", "INJURY_REGRESSION",
         '{"shot_ids":["shot_3_2"]}', "director wants the scar gone here",
         "director_decision", "principal_1", "2026-01-01T00:00:00Z",
         "2099-01-01T00:00:00Z"))

    result = svc.run_check(TA, _request(world, shots=("shot_3_2",)))
    stored = svc.get_report(TA, result["report_id"])

    assert stored["defects"][0]["suppression_reason"] == "EXEMPTION_exm_017"
    assert [a["exemption_id"] for a in stored["exemption_applications"]] == [
        "exm_017"]


def test_without_shot_source_every_shot_is_missing_evidence(world):
    """没注入依赖就不检查 —— 不是静默通过。"""
    svc = _service(world, shots=False)
    result = svc.run_check(TA, _request(world))

    assert result["coverage_ratio"] == 0.0
    assert result["verdict"] != "pass"


def test_expected_state_always_carries_a_canon_claim(world):
    """§1.4 的硬约束：没有来源的期望写不进库。"""
    svc = _service(world)
    result = svc.run_check(TA, _request(world))
    rows = svc.conn.execute(
        "SELECT source_claim_ref, checkpoint FROM expected_state"
        " WHERE tenant_id=? AND report_id=?", (TA, result["report_id"])).fetchall()

    assert rows
    for row in rows:
        ref = json.loads(row["source_claim_ref"])
        assert ref["service_id"] == "S16" and ref["id"]
        assert row["checkpoint"] == world.head


def test_report_freezes_the_expected_state_it_derived(world):
    """正史推进后旧报告不跟着变 —— 期望是快照，不是视图。"""
    svc = _service(world)
    old = svc.run_check(TA, _request(world, command_id="cmd_old"))
    old_rows = svc.conn.execute(
        "SELECT expected_value FROM expected_state WHERE report_id=?"
        " ORDER BY unit_id, entity_id, predicate", (old["report_id"],)).fetchall()
    assert [r["expected_value"] for r in old_rows] == ["char_linlan"]

    # 换手必须走 supersedes：`held_by` 基数=1，同 claim 新值不带 supersedes 会被
    # S16 判成 CLAIM_CONFLICT（C3），根本进不去正史。
    moved = world.claim(claim_id="claim_key_hold", kind="entity_state",
                        subject="prop_key", predicate="held_by",
                        value="char_chenmo", tick=1000, episode="ep003")
    moved["supersedes_claim_refs"] = [{"service_id": "S16",
                                       "id": "claim_key_hold", "revision": 1}]
    world.commit([moved], revision=2)

    new = svc.run_check(TA, _request(world, command_id="cmd_new"))
    new_rows = svc.conn.execute(
        "SELECT expected_value FROM expected_state WHERE report_id=?"
        " ORDER BY unit_id, entity_id, predicate", (new["report_id"],)).fetchall()

    # 两条一起才证明是快照：新报告确实拿到了新值，旧报告没被带着变。
    assert [r["expected_value"] for r in new_rows] == ["char_chenmo"]
    same_rows = svc.conn.execute(
        "SELECT expected_value FROM expected_state WHERE report_id=?"
        " ORDER BY unit_id, entity_id, predicate", (old["report_id"],)).fetchall()
    assert [r["expected_value"] for r in same_rows] == ["char_linlan"]


# ---- HTTP ---------------------------------------------------------------

def _api(path: Path, world):
    spec = importlib.util.spec_from_file_location("s17_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    conn = _conn(share_thread=True)
    module._conn = conn
    module.service = ContinuityService(conn, memory_query=_memory(world.mem),
                                       shot_source=world.shot_source)
    from idempotency_sdk import SqliteIdempotencyStore
    module._idem = SqliteIdempotencyStore(conn)
    return module, TestClient(module.app)


API_PATH = Path(__file__).resolve().parents[1] / "api" / "app.py"


def test_http_check_is_idempotent(world):
    _module, client = _api(API_PATH, world)
    body = _request(world)
    headers = {"Idempotency-Key": "cmd_cont_1", "X-Tenant-Id": TA}

    first = client.post("/continuity-checks", json=body, headers=headers)
    second = client.post("/continuity-checks", json=body, headers=headers)

    assert first.status_code == 202 and second.status_code == 202
    assert second.json()["replayed"] is True
    assert first.json()["report_id"] == second.json()["report_id"]


def test_http_same_command_id_with_different_body_conflicts(world):
    _module, client = _api(API_PATH, world)
    headers = {"Idempotency-Key": "cmd_cont_1", "X-Tenant-Id": TA}
    client.post("/continuity-checks", json=_request(world), headers=headers)
    other = _request(world, profile="text_logic")
    response = client.post("/continuity-checks", json=other, headers=headers)

    assert response.status_code == 409


def test_http_body_tenant_must_match_signed_context(world):
    _module, client = _api(API_PATH, world)
    body = {**_request(world), "tenant_id": TB}
    response = client.post("/continuity-checks", json=body,
                           headers={"Idempotency-Key": "cmd_cont_1",
                                    "X-Tenant-Id": TA})

    assert response.status_code == 403


def test_http_abstained_check_still_returns_202_and_the_reason(world):
    _module, client = _api(API_PATH, world)
    ahead = {**world.checkpoint(), "commit_sequence": 99, "content_hash": H3}
    response = client.post("/continuity-checks",
                           json=_request(world, checkpoint=ahead),
                           headers={"Idempotency-Key": "cmd_cont_1",
                                    "X-Tenant-Id": TA})

    assert response.status_code == 202
    assert response.json()["verdict"] == "abstained"
    assert response.json()["reason"] == "MEMORY_WATERMARK_BEHIND"
