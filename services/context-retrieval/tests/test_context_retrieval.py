"""S18 测试。对照 `plans/S18-context-retrieval.md` §10 的用例表。

一个刻意的选择：`memory_query` 是一个**真的 S16 实例**（内存库），不是 dict 桩。
这样分支、故事有效期、角色知情、观众揭示全部由 S16 自己的规则回答 —— 用例若把
S18 写成"自己复刻可见性公式"，E09-1 / E09-4 / E11-1 会立刻分叉。

每个用例断言能失败的具体值，而不是"结果非空"。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from story_memory.service import StoryMemoryService

from context_retrieval.service import ContextRetrievalService, ServiceError

TA = "t_aaa"
TB = "t_bbb"
SERIES = "series_demo"
H1 = "1" * 64
H2 = "2" * 64


def _conn(*, share_thread: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None,
                           check_same_thread=not share_thread)
    conn.row_factory = sqlite3.Row
    return conn


def _authority(tenant_id, series_id, manifest_ref):
    return {"accepted": True, "revision": manifest_ref.get("revision")}


def _memory(svc: StoryMemoryService):
    def query(tenant_id: str, series_id: str, body: dict) -> dict:
        return svc.query(tenant_id, series_id, body)
    return query


class World:
    """林岚手上有一把钥匙；陈默不知道。第 7 集才揭示陈默的真实身份。"""

    def __init__(self):
        self.mem = StoryMemoryService(_conn(share_thread=True),
                                      manifest_authority=_authority)
        self.mem.create_series(TA, SERIES)
        self.mem.register_clock(TA, SERIES, {"clock_id": "story_main",
                                             "clock_kind": "main",
                                             "tick_min": 0, "tick_max": 10000})
        for eid, order, start, end in (("ep001", 1, 0, 500),
                                       ("ep002", 2, 500, 1000),
                                       ("ep003", 3, 1000, 1500),
                                       ("ep007", 7, 1500, 2000)):
            self.mem.register_episode_window(TA, SERIES, {
                "episode_id": eid, "episode_order": order,
                "clock_id": "story_main", "tick_start": start,
                "tick_end": end})
        for eid, etype in (("char_linlan", "character"),
                           ("char_chenmo", "character"),
                           ("prop_key", "prop"),
                           ("thread_letter", "plot_thread"),
                           ("event_a", "story_event")):
            self.mem.register_entity(TA, SERIES, {
                "entity_id": eid, "entity_type": etype, "display_name": eid})
        self.head = 0

    def manifest(self, episode="ep002", revision=1) -> dict:
        return {"id": f"manifest_{episode}", "revision": revision,
                "content_hash": H1,
                "source_revisions": [{"id": episode, "revision": 1,
                                      "content_hash": H2}]}

    def claim(self, *, claim_id, kind, subject, predicate, value,
              tick=500, episode="ep002", scope=None, extra=None) -> dict:
        cand = {
            "candidate_id": f"cand_{claim_id}_{self.head}",
            "proposed_claim_id": claim_id,
            "kind": kind,
            "fact": {"subject_id": subject, "predicate": predicate,
                     "value": value},
            "story_validity": {"clock_id": "story_main",
                               "valid_from_tick": tick, "valid_to_tick": None},
            "source_refs": [{"service_id": "S08", "id": episode,
                             "revision": 1, "content_hash": H2,
                             "span": "line 42"}],
            "confidence": 0.9,
        }
        if extra:
            cand.update(extra)
        if scope:
            cand["epistemic_scope"] = scope
        return cand

    def commit(self, candidates, *, revision=1, branch="canon") -> dict:
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


def _service(world: World, *, memory=None) -> ContextRetrievalService:
    return ContextRetrievalService(
        _conn(), memory_query=memory or _memory(world.mem))


def _request(world: World, **extra) -> dict:
    body = {
        "command_id": "cmd_ctx_1",
        "series_id": SERIES,
        "branch_id": "canon",
        "episode_id": "ep003",
        "episode_order": 3,
        "task_role": "writer",
        "perspective": {"mode": "omniscient"},
        "story_clock_id": "story_main",
        "story_tick": 1100,
        "required_checkpoint": world.checkpoint(),
        "token_budget": 4000,
        "focus": {"entity_ids": ["char_linlan", "prop_key"]},
    }
    body.update(extra)
    return body


@pytest.fixture()
def world() -> World:
    w = World()
    w.commit([w.claim(claim_id="claim_key_owner", kind="entity_state",
                      subject="prop_key", predicate="held_by",
                      value="char_linlan")])
    return w


# ---- §10.1 E08 · 改前集钥匙归属 -----------------------------------------

def test_e08_1_writer_pack_references_the_current_key_owner(world):
    svc = _service(world)
    result = svc.create_pack(TA, _request(world))

    assert result["checkpoint"]["commit_sequence"] == 1
    refs = {r["id"]: r for r in result["claim_refs"]}
    assert refs["claim_key_owner"]["revision"] == 1
    assert refs["claim_key_owner"]["service_id"] == "S16"


def test_e08_3_old_checkpoint_is_rejected_not_served_stale(world):
    """§5 / 反模式 7：水位不够就 409，不返回 stale pack。"""
    svc = _service(world)
    body = _request(world)
    body["required_checkpoint"] = {"commit_sequence": 99,
                                   "content_hash": H1}
    with pytest.raises(ServiceError) as exc:
        svc.create_pack(TA, body)
    assert exc.value.status == 409
    assert exc.value.code == "MEMORY_WATERMARK_BEHIND"


# ---- §10.2 E09 · 视角 ---------------------------------------------------

def test_e09_1_character_perspective_does_not_see_unknown_propositions(world):
    """闭世界：陈默没被记录为知道钥匙 → 不进 claim_refs，且生成 KNOWLEDGE 禁则。"""
    svc = _service(world)
    body = _request(world, task_role="prompt",
                    perspective={"mode": "character",
                                 "character_ids": ["char_chenmo"]})
    result = svc.create_pack(TA, body)

    assert "claim_key_owner" not in {r["id"] for r in result["claim_refs"]}
    assert any("不可见" in text for text in result["prohibited_assumptions"])


def test_e09_4_omniscient_still_respects_reveal_order(world):
    """全知 ≠ 全可见（S18 反模式 4）。

    两条同时刻、同种类、只差揭示序的事实：揭示序 3 的进包，揭示序 7 的不进。
    单边断言「第 7 集的不在」会在实体过滤或时间窗把它先筛掉时假绿，所以必须
    有那条应该进来的作为对照 —— 它证明这条探针本来就看得见这类 claim。
    """
    scope_early = {"perspective": "omniscient", "knower_ids": [],
                   "belief_type": "fact",
                   "audience_reveal_episode_order": 3}
    scope_late = dict(scope_early, audience_reveal_episode_order=7)
    world.commit([world.claim(claim_id="claim_linlan_age",
                              kind="character_profile",
                              subject="char_linlan", predicate="age",
                              value=30, tick=1100, scope=scope_early),
                  world.claim(claim_id="claim_chenmo_age",
                              kind="character_profile",
                              subject="char_chenmo", predicate="age",
                              value=44, tick=1100, scope=scope_late)],
                 revision=2)
    svc = _service(world)
    result = svc.create_pack(TA, _request(world, focus={
        "entity_ids": ["char_linlan", "char_chenmo", "prop_key"]}))

    ids = {r["id"] for r in result["claim_refs"]}
    assert "claim_linlan_age" in ids          # 对照：探针看得见这一类
    assert "claim_chenmo_age" not in ids      # 揭示序 7 > 第 3 集


def test_e09_6_world_rule_is_visible_from_a_character_perspective(world):
    world.commit([world.claim(claim_id="claim_rule_teleport",
                              kind="world_rule", subject="char_linlan",
                              predicate="rule_statement",
                              value="may teleport once per night")],
                 revision=2)
    svc = _service(world)
    body = _request(world, task_role="prompt",
                    perspective={"mode": "character",
                                 "character_ids": ["char_chenmo"]})
    result = svc.create_pack(TA, body)

    assert "claim_rule_teleport" in {r["id"] for r in result["claim_refs"]}


def test_e09_7_禁则文本不重复正史取值(world):
    """§4.8：禁则只说限制。状态禁则里不得出现 `held_by` 的 value。"""
    svc = _service(world)
    result = svc.create_pack(TA, _request(world))
    for text in result["prohibited_assumptions"]:
        assert "char_linlan" not in text or "不得" in text


def test_leak_scan_catches_a_withheld_claim_id_named_in_an_assumption(world):
    """探针自证：把 withheld 的 id 塞进禁则文本，扫描必须命中。

    没有这条，`_assert_no_leak` 的 `hits` 永远是 0 —— 一个从不工作的检查看起来
    和一个正常工作的一样绿。这里直接替换模板产物再跑一次同一个函数。
    """
    svc = _service(world)
    leaked = [{"assumption_id": "pa_999", "rule_kind": "KNOWLEDGE",
               "severity": "hard",
               "text": "注意 claim_key_owner 这条不可见",
               "basis": {"reason_code": "S16_WITHHELD"},
               "claim_refs": [], "entity_ids": [],
               "generated_by": "probe@v1"}]
    request = {"perspective_mode": "character"}
    with pytest.raises(ServiceError) as exc:
        svc._assert_no_leak(request, leaked, [], ["claim_key_owner"])
    assert exc.value.code == "LEAK_DETECTED"

    clean = [dict(leaked[0], text="部分命题对当前视角不可见",
                  assumption_id="pa_998")]
    svc._assert_no_leak(request, clean, [], ["claim_key_owner"])
    assert request["_leak_scan"]["result"] == "clean"
    assert request["_leak_scan"]["withheld_claims"] == 1


def test_believes_produces_a_knowledge_belief_assumption(world):
    """§4.2：`believes` 模板不是死码 —— 必须能被一条真实 claim 触发。

    S16 的 `belief_type` 存在 `epistemic_scope` 里，所以夹具写在那里。

    用 omniscient 而非 character：闭世界下「陈默相信 X」这条 claim 对陈默自己
    也要先有一条 knowledge_state 说「他知道自己有此信念」才可见，那是 S16 的
    规则，不是这里要验的东西。
    """
    world.commit([world.claim(claim_id="claim_other_holder",
                              kind="entity_state", subject="prop_key",
                              predicate="held_by", value="char_chenmo",
                              tick=1100),
                  world.claim(claim_id="claim_chenmo_belief",
                              kind="knowledge_state",
                              subject="char_chenmo",
                              predicate="belief_about",
                              value="char_linlan", tick=1100,
                              extra={"proposition_claim_id":
                                     "claim_key_owner"},
                              scope={"perspective": "character",
                                     "knower_ids": ["char_chenmo"],
                                     "belief_type": "believes",
                                     "audience_reveal_episode_order": 1})],
                 revision=2)
    svc = _service(world)
    result = svc.create_pack(TA, _request(world, focus={
        "entity_ids": ["char_linlan", "char_chenmo", "prop_key"]}))
    rows = svc.conn.execute(
        "SELECT rule_kind, generated_by FROM prohibited_assumption"
        " WHERE tenant_id=? AND context_pack_id=?",
        (TA, result["context_pack_id"])).fetchall()
    by_kind = {r["rule_kind"]: r["generated_by"] for r in rows}
    assert by_kind.get("KNOWLEDGE_BELIEF") == "knowledge-belief@v1"


# ---- §10.3 E10 · 分支与候选 ---------------------------------------------

def test_e10_3_candidates_rejected_for_writer(world):
    svc = _service(world)
    with pytest.raises(ServiceError) as exc:
        svc.create_pack(TA, _request(world, include_candidates=True))
    assert exc.value.status == 400
    assert exc.value.code == "CANDIDATES_NOT_ALLOWED"


def test_e10_4_candidate_pack_is_never_deliverable(world):
    """§2 第 3 条：候选包永久不可交付，靠库约束堵死。"""
    svc = _service(world)
    body = _request(world, task_role="preview",
                    command_id="cmd_preview",
                    include_candidates=True)
    result = svc.create_pack(TA, body)

    row = svc.conn.execute(
        "SELECT candidate, pack_for_delivery FROM context_pack"
        " WHERE tenant_id=? AND context_pack_id=?",
        (TA, result["context_pack_id"])).fetchone()
    assert row["candidate"] == 1
    assert row["pack_for_delivery"] == 0


def test_candidate_flag_cannot_be_flipped_while_deliverable(world):
    """库约束本身可测：candidate=1 且 pack_for_delivery=1 必须被拒。"""
    svc = _service(world)
    with pytest.raises(sqlite3.IntegrityError):
        svc.conn.execute(
            "INSERT INTO context_pack (tenant_id, context_pack_id, revision,"
            " command_id, series_id, branch_id, task_role, target_episode_id,"
            " target_episode_order, perspective, checkpoint_sequence,"
            " checkpoint_hash, source_watermark, required_checkpoint,"
            " claim_refs, asset_refs, token_budget, tokens_used,"
            " budget_breakdown, forbidden_leak_scan, content_hash, candidate,"
            " pack_for_delivery, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (TA, "ctx_bad", 1, "cmd_x", SERIES, "canon", "preview", "ep003",
             3, "omniscient", 1, H1, 1, 1, "[]", "[]", 100, 0, "[]",
             "{}", H1, 1, 1, "now"))


# ---- §10.4 E11 · 索引落后与水门 -----------------------------------------

def test_e11_1_hard_facts_always_come_from_s16(world):
    """硬事实实时查 S16 —— 本实现没有本地索引，所以不存在"读旧索引"的路径。"""
    svc = _service(world)
    result = svc.create_pack(TA, _request(world))
    assert result["source_watermark"] == 1
    assert result["claim_refs"], "硬事实不该为空"


def test_e11_4_memory_behind_rejects_without_any_pack(world):
    svc = _service(world)
    body = _request(world)
    body["required_checkpoint"] = {"commit_sequence": 50,
                                   "content_hash": H1}
    with pytest.raises(ServiceError) as exc:
        svc.create_pack(TA, body)
    assert exc.value.code == "MEMORY_WATERMARK_BEHIND"
    count = svc.conn.execute(
        "SELECT COUNT(*) AS n FROM context_pack").fetchone()["n"]
    assert count == 0


def test_e11_5_memory_unreachable_is_503_not_degraded(world):
    def down(tenant_id, series_id, body):
        raise ServiceError(503, "MEMORY_UNAVAILABLE", "down", retryable=True)

    svc = _service(world, memory=down)
    with pytest.raises(ServiceError) as exc:
        svc.create_pack(TA, _request(world))
    assert exc.value.status == 503
    assert exc.value.code == "MEMORY_UNAVAILABLE"
    count = svc.conn.execute(
        "SELECT COUNT(*) AS n FROM context_pack").fetchone()["n"]
    assert count == 0


def test_watermark_never_goes_backwards(world):
    """§6.5。更小的 commit 序列不覆盖已应用的水位。"""
    svc = _service(world)
    svc.create_pack(TA, _request(world))
    svc._upsert_watermark(TA, {"series_id": SERIES, "branch_id": "canon"},
                          0, H1)
    row = svc.conn.execute(
        "SELECT applied_commit_sequence AS s FROM watermark"
        " WHERE tenant_id=? AND series_id=? AND branch_id=?",
        (TA, SERIES, "canon")).fetchone()
    assert row["s"] == 1


# ---- §10.5 预算与确定性 -------------------------------------------------

def test_budget_too_small_fails_instead_of_truncating(world):
    """反模式 8：硬事实被预算挤掉 → 报错，不截断。"""
    svc = _service(world)
    with pytest.raises(ServiceError) as exc:
        svc.create_pack(TA, _request(world, token_budget=1))
    assert exc.value.status == 422
    assert exc.value.code == "BUDGET_TOO_SMALL_FOR_REQUIRED_FACTS"
    assert exc.value.details["minimum_tokens"] > 1


def test_same_input_yields_same_content_hash(world):
    """§1.1：确定性是可测试的性质，不是口头承诺。"""
    svc = _service(world)
    first = svc.create_pack(TA, _request(world))
    second = svc.create_pack(TA, _request(world, command_id="cmd_ctx_2"))
    assert first["content_hash"] == second["content_hash"]
    assert first["claim_refs"] == second["claim_refs"]


def test_request_without_story_tick_is_rejected(world):
    """§2 第 4 条：缺 story_tick 不能默认当前。"""
    svc = _service(world)
    body = _request(world)
    del body["story_tick"]
    with pytest.raises(ServiceError) as exc:
        svc.create_pack(TA, body)
    assert exc.value.code == "MALFORMED_REQUEST"


def test_character_perspective_without_characters_is_rejected(world):
    """闭世界下空角色集什么都看不见，静默空 pack 比报错危险。"""
    svc = _service(world)
    with pytest.raises(ServiceError) as exc:
        svc.create_pack(TA, _request(world,
                                     perspective={"mode": "character"}))
    assert exc.value.code == "MALFORMED_REQUEST"


# ---- §10.6 越权 ---------------------------------------------------------

def test_cross_tenant_pack_read_is_404(world):
    svc = _service(world)
    result = svc.create_pack(TA, _request(world))
    with pytest.raises(ServiceError) as exc:
        svc.get_pack(TB, result["context_pack_id"])
    assert exc.value.status == 404
    assert exc.value.code == "PACK_NOT_FOUND"


def test_every_assumption_carries_a_basis(world):
    """反模式：没有 basis 的禁则是"模型凭空觉得应该禁止"。"""
    svc = _service(world)
    result = svc.create_pack(TA, _request(world))
    rows = svc.conn.execute(
        "SELECT basis FROM prohibited_assumption WHERE tenant_id=?"
        " AND context_pack_id=?", (TA, result["context_pack_id"])).fetchall()
    assert rows
    for row in rows:
        assert json.loads(row["basis"])


# ---- HTTP 层 ------------------------------------------------------------

def _client(monkeypatch, world) -> TestClient:
    spec = importlib.util.spec_from_file_location(
        "s18_app", Path(__file__).resolve().parents[1] / "api" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.service = ContextRetrievalService(
        _conn(share_thread=True), memory_query=_memory(world.mem))
    return TestClient(module.app)


def test_http_create_then_get_round_trips(monkeypatch, world):
    client = _client(monkeypatch, world)
    body = _request(world)
    created = client.post("/context-packs", json=body,
                          headers={"Idempotency-Key": body["command_id"],
                                   "X-Tenant-Id": TA})
    assert created.status_code == 201
    pack_id = created.json()["context_pack_id"]

    fetched = client.get(f"/context-packs/{pack_id}",
                         headers={"X-Tenant-Id": TA})
    assert fetched.status_code == 200
    assert fetched.json()["pack"]["context_pack_id"] == pack_id
    assert fetched.json()["prohibited_assumption_details"]


def test_http_replay_returns_the_same_pack(monkeypatch, world):
    client = _client(monkeypatch, world)
    body = _request(world)
    headers = {"Idempotency-Key": body["command_id"], "X-Tenant-Id": TA}
    first = client.post("/context-packs", json=body, headers=headers).json()
    second = client.post("/context-packs", json=body, headers=headers).json()
    assert second["replayed"] is True
    assert second["context_pack_id"] == first["context_pack_id"]
    assert second["content_hash"] == first["content_hash"]


def test_http_body_tenant_mismatch_is_403(monkeypatch, world):
    client = _client(monkeypatch, world)
    body = _request(world, tenant_id=TB)
    response = client.post("/context-packs", json=body,
                           headers={"Idempotency-Key": body["command_id"],
                                    "X-Tenant-Id": TA})
    assert response.status_code == 403
    assert response.json()["code"] == "TENANT_MISMATCH"


def test_http_unknown_pack_is_404(monkeypatch, world):
    client = _client(monkeypatch, world)
    response = client.get("/context-packs/ctx_missing",
                          headers={"X-Tenant-Id": TA})
    assert response.status_code == 404


def test_contract_gaps_are_reported_not_hidden(monkeypatch, world):
    client = _client(monkeypatch, world)
    body = _request(world)
    payload = client.post("/context-packs", json=body,
                          headers={"Idempotency-Key": body["command_id"],
                                   "X-Tenant-Id": TA}).json()
    assert any("content_hash" in gap for gap in payload["x-contract-gaps"])


def test_openapi_execution_context_envelope_is_refused_not_guessed(
        monkeypatch, world):
    """openapi 的 body 形状装不下 §2，收到就要明说，而不是丢掉视角字段。

    `execution-context.v2` 的 18 个字段全部 required 且 additionalProperties:
    false，`episode_order` / `perspective` / `story_tick` / `focus` /
    `include_candidates` / `story_clock_id` 一个都不在里面。如果实现"尽力映射"，
    它只能给 `perspective` 编一个默认值 —— 那就是 S18 反模式 4「全知视角 = 全部
    可见」，一个泄漏。所以这条路径必须 400。
    """
    client = _client(monkeypatch, world)
    body = _request(world)
    envelope = {
        "command_id": body["command_id"],
        "task_role": body["task_role"],
        "token_budget": body["token_budget"],
        "context": {"schema_version": "2", "tenant_id": TA},
    }
    response = client.post("/context-packs", json=envelope,
                           headers={"Idempotency-Key": body["command_id"],
                                    "X-Tenant-Id": TA})
    assert response.status_code == 400
    assert response.json()["code"] == "CONTRACT_BODY_UNSUPPORTED"


def test_missing_upstream_hash_becomes_a_labelled_placeholder(world):
    """缺口不是一句说明：真的没 hash 时，ref 里是 64-hex 占位，且可辨认。

    没有这条，`CONTRACT_GAPS` 第一条就只是文字，占位逻辑坏了也没人知道
    （契约 schema 只校验 64-hex，不校验它是不是内容哈希）。
    """
    body = _request(world)
    pack = _service(world).create_pack(TA, body)
    refs = pack["claim_refs"]
    assert refs, "fixture must produce at least one claim ref"
    # S16 确实不返回该字段 —— 否则下面的占位断言测的不是占位。
    up = world.mem.query(TA, SERIES, {
        "branch_id": "canon",
        "minimum_checkpoint": world.head,
        "as_of_commit_sequence": world.head,
        "story_clock_id": "story_main",
        "story_from_tick": 0, "story_to_tick": 1400,
        "perspective": {"mode": "omniscient"}})
    assert up["claims"], "fixture must have visible claims"
    assert all("content_hash" not in c for c in up["claims"])
    for ref in refs:
        assert len(ref["content_hash"]) == 64
        assert ref["content_hash"] == _sha_placeholder(ref["id"], ref["revision"])


def _sha_placeholder(claim_id, revision):
    import hashlib
    return hashlib.sha256(
        f"unavailable:{claim_id}:{revision}".encode()).hexdigest()


def test_checkpoint_mismatch_is_409_and_writes_no_pack(world):
    """§2 第 1 条：S16 报的 checkpoint 必须就是要求的那个，多一个也不行。

    两条分支都要测：序列不同、以及序列相同时 hash 不同。后者是"同样的
    commit_sequence 但分支内容已经变了"——只比序列会漏掉它。
    """
    real = _memory(world.mem)
    tamper = {}

    def routed(tenant_id, series_id, body):
        out = real(tenant_id, series_id, body)
        if tamper.get("seq"):
            out["checkpoint"] = out["checkpoint"] + 1
        if tamper.get("hash"):
            out["checkpoint_hash"] = "0" * 64
        return out

    # 同一个 service 实例跑两种篡改，最后在**它自己的**连接上数行 —— 否则
    # 换一个新建的 _conn() 去数，永远是 0，测的是空气。
    svc = _service(world, memory=routed)

    tamper["seq"] = True
    with pytest.raises(ServiceError) as exc:
        svc.create_pack(TA, _request(world, command_id="cmd_seq"))
    assert exc.value.status == 409
    assert exc.value.code == "CHECKPOINT_MISMATCH"
    tamper["seq"] = False

    tamper["hash"] = True
    with pytest.raises(ServiceError) as exc:
        svc.create_pack(TA, _request(world, command_id="cmd_hash"))
    assert exc.value.code == "CHECKPOINT_MISMATCH"
    assert exc.value.details["declared_hash"] is not None
    tamper["hash"] = False

    count = svc.conn.execute(
        "SELECT COUNT(*) AS n FROM context_pack").fetchone()["n"]
    assert count == 0

    # 正控：拿掉篡改，同一个实例必须能写进来 —— 否则上面的 0 也是假的。
    svc.create_pack(TA, _request(world, command_id="cmd_clean"))
    assert svc.conn.execute(
        "SELECT COUNT(*) AS n FROM context_pack").fetchone()["n"] == 1
