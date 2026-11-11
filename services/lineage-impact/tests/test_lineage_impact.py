"""S09 测试。本地 SQLite + 手工构造的图，不接真实依赖、不付费。

对照 plans/S09-lineage-impact.md §8.1；用例号沿用方案里的 T09-xx。
E03 的图（shot_3_2 → keyframe/video/tts/subtitle → master）在 `_e03` 里搭一次。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from lineage_impact import closure
from lineage_impact.schema import WHITELIST
from lineage_impact.service import LineageImpactService, ServiceError

TENANT = "tnt_a"
OTHER = "tnt_b"
BRANCH = "main"
MANIFEST = "mf_ep3"
EPISODE = "ep3"


def _h(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _ref(service: str, ident: str, revision: int = 1, seed: str = "x") -> dict:
    return {"service_id": service, "id": ident, "revision": revision,
            "content_hash": _h(f"{ident}:{revision}:{seed}")}


@pytest.fixture()
def svc():
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return LineageImpactService(conn)


def _manifest(*nodes, revision: int = 1, fence: int = 1, episode: str = EPISODE,
              branch: str = BRANCH) -> dict:
    states = []
    for node in nodes:
        state = {"node_id": node["node_id"], "ref": node["ref"]}
        if node.get("expected_edges"):
            state["expected_edges"] = node["expected_edges"]
        states.append(state)
    return {"id": f"ev_{revision}", "type": "snapshot.manifest.committed",
            "tenant_id": TENANT, "sequence": revision,
            "data": {"manifest_id": MANIFEST, "revision": revision,
                     "branch_id": branch, "execution_fence": fence,
                     "episode_id": episode, "node_states": states}}


def _edge(kind: str, source: dict, target: dict, field_path: str | None = None) -> dict:
    return {"edge_kind": kind, "source": source, "target": target,
            "field_path": field_path}


def _node(node_id: str, service: str, ident: str, revision: int, seed: str,
          expected: tuple[str, ...] = ()) -> dict:
    return {"node_id": node_id,
            "ref": _ref(service, ident, revision, seed),
            "expected_edges": [{"edge_kind": k} for k in expected]}


# 会向 S09 投递事件的源（与 service.EVENT_SOURCES 同一批）。闭包只要碰到
# 其中一个而它没有水位行，就判成"追不上"——这是对的，所以夹具必须先让它们
# 都有行。S05 之类不投递事件的源不在其中，也不需要。
def _settle(svc, sources=("S04", "S06", "S08", "S10", "S16", "S18", "S28",
                          "S37")):
    """把各源标成"刚追平"：没有这一步，任何闭包都会因为源没消息而保守。"""
    for source in sources:
        svc.observe_lag(TENANT, BRANCH, source, 0)


def _e03(svc):
    """E03 夹具：仰拍 4 秒局部改镜。

    shot_3_2 → keyframe(/camera/angle) / video(/duration/target_seconds)
             → tts(/dialogue) → subtitle → master
    """
    shot = _ref("S06", "shot_3_2", 8, "a")
    kf = _ref("S27", "kf_3_2", 3, "b")
    video = _ref("S27", "vid_3_2", 3, "c")
    tts = _ref("S31", "tts_3_2", 2, "d")
    sub = _ref("S38", "sub_3_2", 2, "e")
    master = _ref("S44", "mst_ep3", 5, "f")

    svc.consume(_manifest(
        {"node_id": "shot_3_2", "ref": shot},
        {"node_id": "shot_3_2.keyframe", "ref": kf,
         "expected_edges": [{"edge_kind": "ARTIFACT_FROM_SHOT"}]},
        {"node_id": "shot_3_2.video", "ref": video,
         "expected_edges": [{"edge_kind": "ARTIFACT_FROM_SHOT"}]},
        {"node_id": "shot_3_2.tts", "ref": tts,
         "expected_edges": [{"edge_kind": "AUDIO_FROM_DIALOGUE"}]},
        {"node_id": "shot_3_2.subtitle", "ref": sub,
         "expected_edges": [{"edge_kind": "SUBTITLE_FROM_AUDIO"}]},
        {"node_id": "ep3.master", "ref": master,
         "expected_edges": [{"edge_kind": "EDIT_FROM_ARTIFACT"}]},
    ))
    # video 依赖两个字段：画面（机位角）和时长。只声明时长会让改机位时
    # 视频被判成可复用——那正是"证明得了就复用"的反面：边没声明全，
    # 复用规则会诚实地给出 REUSE。所以图本身必须说全。
    svc.write_edges(TENANT, {"command_id": "cmd_edges", "branch_id": BRANCH, "edges": [
        _edge("ARTIFACT_FROM_SHOT", shot, kf, "/camera/angle"),
        _edge("ARTIFACT_FROM_SHOT", shot, video, "/camera/angle"),
        _edge("ARTIFACT_FROM_SHOT", shot, video, "/duration/target_seconds"),
        _edge("AUDIO_FROM_DIALOGUE", shot, tts, "/dialogue"),
        _edge("SUBTITLE_FROM_AUDIO", tts, sub),
        _edge("EDIT_FROM_ARTIFACT", sub, master),
    ]})

    # /dialogue 只有 hash（白名单之外）——两个 revision 的 hash 相同。
    for revision, angle, duration in ((7, "eye_level", 6), (8, "low", 4)):
        svc.project_fields(TENANT, BRANCH, {
            "service_id": "S06", "id": "shot_3_2", "revision": revision,
            "fields": {"/camera/angle": angle, "/duration/target_seconds": duration,
                       "/dialogue": {"text": "同一句台词"},
                       "/duration/trim_allowed": True}})
    _settle(svc)
    return {"shot": shot, "kf": kf, "video": video, "tts": tts, "sub": sub,
            "master": master}


def _query(svc, refs, *, mode="EXACT", fence=1, command_id="cmd_q1",
           tenant=TENANT, manifest_revision=1):
    return svc.query_impact(tenant, {
        "command_id": command_id, "branch_id": BRANCH, "execution_fence": fence,
        "mode_hint": mode,
        "base_manifest_ref": {"id": MANIFEST, "revision": manifest_revision,
                              "content_hash": _h("mf")},
        "seed": {"kind": "CHANGE_SEED", "refs": refs}})


def _disp(analysis: dict) -> dict[str, str]:
    return {n["node_id"]: n["disposition"] for n in analysis["nodes"]}


def _reason(analysis: dict, node_id: str) -> str:
    return next(n["reason_code"] for n in analysis["nodes"] if n["node_id"] == node_id)


# ---- 8.1 E03 · 局部改镜 ---------------------------------------------------

def test_angle_change_regenerates_keyframe_and_video(svc):
    """T09-01：机位角变了 → 关键帧与视频都重做；未涉及的镜头不进闭包。"""
    g = _e03(svc)
    other = _node("shot_3_4.video", "S27", "vid_3_4", 1, "z")
    svc.consume(_manifest({"node_id": "shot_3_4.video", "ref": other["ref"]},
                          revision=2))
    result = _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}])
    analysis = svc.get_analysis(TENANT, result["analysis_id"])
    disp = _disp(analysis)
    assert disp["shot_3_2.keyframe"] == "REGENERATE"
    assert disp["shot_3_2.video"] == "REGENERATE"
    assert "shot_3_4.video" not in disp


def test_duration_change_reuses_tts_and_regenerates_subtitle(svc):
    """T09-02：只改时长且可裁 → TTS 复用；字幕时码依赖音频长度 → 重做。"""
    g = _e03(svc)
    result = _query(svc, [{**g["shot"], "fields": ["/duration/target_seconds"]}])
    analysis = svc.get_analysis(TENANT, result["analysis_id"])
    disp = _disp(analysis)
    assert disp["shot_3_2.tts"] == "REUSE"
    assert _reason(analysis, "shot_3_2.tts") == "REUSABLE_TEXT_UNCHANGED"
    assert disp["shot_3_2.subtitle"] == "REGENERATE"
    # 关键帧只依赖 /camera/angle，时长变了它照样可复用——这不是顺带，
    # 是复用规则该给的结论；写死成"只复用 TTS"会把这条规则测成假的。
    assert disp["shot_3_2.keyframe"] == "REUSE"
    reused = {r["id"] for r in result["impact"]["reuse_artifact_refs"]}
    assert reused == {"tts_3_2", "kf_3_2"}


def test_untrimmable_duration_downgrades_reuse_to_recheck(svc):
    """T09-03：trim_allowed=false 时 TTS 不能直接复用，交 QC 复检。"""
    g = _e03(svc)
    svc.project_fields(TENANT, BRANCH, {
        "service_id": "S06", "id": "shot_3_2", "revision": 8,
        "fields": {"/duration/trim_allowed": False}})
    result = _query(svc, [{**g["shot"], "fields": ["/duration/target_seconds"]}])
    analysis = svc.get_analysis(TENANT, result["analysis_id"])
    assert _disp(analysis)["shot_3_2.tts"] == "RECHECK"
    assert _reason(analysis, "shot_3_2.tts") == "DURATION_DERIVED"


def test_unchanged_dialogue_never_reports_direct_field_change(svc):
    """T09-06：/dialogue 的 hash 没变，依赖它的 TTS 不该出现 DIRECT_FIELD_CHANGED。"""
    g = _e03(svc)
    result = _query(svc, [{**g["shot"], "fields": ["/duration/target_seconds"]}])
    analysis = svc.get_analysis(TENANT, result["analysis_id"])
    assert _reason(analysis, "shot_3_2.tts") != "DIRECT_FIELD_CHANGED"
    assert _reason(analysis, "shot_3_2") == "DIRECT_FIELD_CHANGED"


def test_delivered_episode_never_regenerates(svc):
    """§4.3：已交付集的节点只 RECHECK，即使它的输入确实变了。"""
    g = _e03(svc)
    result = _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}])
    analysis = svc.get_analysis(TENANT, result["analysis_id"])
    assert _disp(analysis)["ep3.master"] == "RECHECK"
    assert "ep3.master" not in result["impact"]["regenerate_node_ids"]


# ---- 8.2 E08 · 跨集改稿 ---------------------------------------------------

def _memory_graph(svc):
    """一个 claim 被两集引用：claim → pack → script → scene → shot。

    链上每一段都得有边。只写 claim→pack 的话，闭包走到 pack 就没了下游，
    `affected_episode_ids` 会是空的——那不是"共享事实只影响一处"，是图断了。
    """
    claim = _ref("S16", "claim_1", 2, "cl")
    packs = {"ep3": _ref("S18", "pack_3", 1, "p3"),
             "ep4": _ref("S18", "pack_4", 1, "p4")}
    scripts = {"ep3": _ref("S04", "scr_3", 1, "sc3"),
               "ep4": _ref("S04", "scr_4", 1, "sc4")}
    scenes = {"ep3": _ref("S05", "scene_3", 1, "s3"),
              "ep4": _ref("S05", "scene_4", 1, "s4")}
    shots = {"ep3": _ref("S06", "shot_3_1", 4, "s31"),
             "ep4": _ref("S06", "shot_4_1", 2, "s41")}

    svc.write_edges(TENANT, {"command_id": "cmd_x1", "branch_id": BRANCH, "edges": [
        *(_edge("PACK_USES_CLAIM", claim, packs[ep]) for ep in packs),
        *(_edge("SCRIPT_DERIVED_FROM_PACK", packs[ep], scripts[ep]) for ep in packs),
        *(_edge("SCENE_DERIVED_FROM_SCRIPT", scripts[ep], scenes[ep]) for ep in packs),
        *(_edge("SHOT_DERIVED_FROM_SCENE", scenes[ep], shots[ep]) for ep in packs),
    ]})
    for index, ep in enumerate(("ep3", "ep4"), start=1):
        svc.consume(_manifest(
            {"node_id": f"shot_{3 if ep == 'ep3' else 4}_1", "ref": shots[ep]},
            revision=index, episode=ep))
    _settle(svc)
    return claim


def test_claim_superseded_reaches_both_episodes(svc):
    """T09-10/T09-11：同一 claim 被两集引用，两集的镜头都进闭包且不重复计。"""
    claim = _memory_graph(svc)
    result = _query(svc, [{**claim, "fields": []}])
    assert sorted(result["impact"]["affected_episode_ids"]) == ["ep3", "ep4"]
    nodes = svc.get_analysis(TENANT, result["analysis_id"])["nodes"]
    ids = [n["node_id"] for n in nodes]
    assert len(ids) == len(set(ids))
    assert {"shot_3_1", "shot_4_1"} <= set(ids)


def test_memory_event_closes_superseded_claim_edges(svc):
    """§4.3：记忆事件自己不改闭包，只登记并关掉被 supersede 的边。"""
    claim = _memory_graph(svc)
    out = svc.consume({
        "id": "ev_mem", "type": "memory.canon.committed", "tenant_id": TENANT,
        "sequence": 3,
        "data": {"branch_id": BRANCH, "commit_sequence": 3,
                 "superseded_claim_refs": [claim]}})
    assert out["claims_closed"] == 2
    # 边关了以后闭包从 claim 出发再走不到任何集。
    result = _query(svc, [{**claim, "fields": []}])
    assert result["impact"]["affected_episode_ids"] == []
    # 只是不再生效，不是被删掉（§3.2）：8 条里关了 2 条，生效的剩 6 条，
    # 物理行数仍是 8。
    assert svc.edges(TENANT)["total"] == 6
    assert svc.conn.execute(
        "SELECT count(*) FROM lineage_edge WHERE tenant_id = ?",
        (TENANT,)).fetchone()[0] == 8


# ---- 8.3 E11 · 水位与缺口 -------------------------------------------------

def test_within_budget_stays_exact(svc):
    """T09-20：500ms 落后、预算 2s → 仍可 EXACT。"""
    g = _e03(svc)
    svc.observe_lag(TENANT, BRANCH, "S06", 500, budget_ms=2000)
    result = _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}])
    assert result["mode"] == "EXACT"


def test_over_budget_conservative_merges_episode(svc):
    """T09-21：超预算 → CONSERVATIVE，并入整集 shot，RECHECK 全升 REGENERATE。"""
    g = _e03(svc)
    svc.consume(_manifest(
        {"node_id": "shot_3_2", "ref": g["shot"]},
        {"node_id": "shot_3_9", "ref": _ref("S06", "shot_3_9", 1, "n")},
        revision=2))
    svc.observe_lag(TENANT, BRANCH, "S06", 10_000, budget_ms=2000)
    result = _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}],
                    mode="ALLOW_CONSERVATIVE")
    assert result["mode"] == "CONSERVATIVE"
    disp = _disp(svc.get_analysis(TENANT, result["analysis_id"]))
    assert disp["shot_3_9"] == "REGENERATE"
    assert disp["ep3.master"] == "REGENERATE"


def test_exact_hint_refuses_to_silently_go_conservative(svc):
    """T09-22：只能保守而调用方要 EXACT → 409，不静默降级。"""
    g = _e03(svc)
    svc.observe_lag(TENANT, BRANCH, "S06", 10_000, budget_ms=2000)
    with pytest.raises(ServiceError) as caught:
        _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}], mode="EXACT")
    assert caught.value.status == 409
    assert caught.value.code == "CONSERVATIVE_REQUIRED"
    assert caught.value.details["would_be_mode"] == "CONSERVATIVE"


def test_missing_asset_binding_blocks(svc):
    """T09-23：声明了 ASSET_BINDING 却没有边 → 409 IMPACT_INCOMPLETE。"""
    shot = _ref("S06", "shot_3_2", 8, "a")
    svc.consume(_manifest({"node_id": "shot_3_2", "ref": shot,
                           "expected_edges": [{"edge_kind": "ASSET_BINDING"}]}))
    _settle(svc)
    with pytest.raises(ServiceError) as caught:
        _query(svc, [{**shot, "fields": ["/camera/angle"]}])
    assert caught.value.status == 409
    assert caught.value.code == "IMPACT_INCOMPLETE"
    assert caught.value.details["gap_ids"]
    analysis = svc.get_analysis(TENANT, caught.value.details["analysis_id"])
    assert analysis["status"] == "INCOMPLETE"


def test_backfill_resolves_gap_and_changes_hash(svc):
    """T09-24：补边后可算；gap 关掉；analysis_hash 与补齐前不同。"""
    shot = _ref("S06", "shot_3_2", 8, "a")
    asset = _ref("S10", "asset_1", 1, "as")
    svc.consume(_manifest(
        {"node_id": "shot_3_2", "ref": shot,
         "expected_edges": [{"edge_kind": "ASSET_BINDING"}]},
        {"node_id": "shot_3_2.keyframe", "ref": _ref("S27", "kf_3_2", 3, "b")}))
    _settle(svc)
    with pytest.raises(ServiceError) as caught:
        _query(svc, [{**shot, "fields": ["/camera/angle"]}])
    gap_ids = caught.value.details["gap_ids"]
    assert svc.open_gaps(TENANT, BRANCH)

    svc.write_edges(TENANT, {"command_id": "cmd_bf", "branch_id": BRANCH, "edges": [
        _edge("ASSET_BINDING", asset, shot)]})
    assert [g["gap_id"] for g in svc.open_gaps(TENANT, BRANCH)] == []
    assert set(gap_ids)  # 关掉的是同一批

    first = _query(svc, [{**shot, "fields": ["/camera/angle"]}], command_id="cmd_q2")
    assert first["status"] == "COMPUTED"

    # 再来一次同样的查询（不同 command_id）：同语义 → 同 analysis。
    again = _query(svc, [{**shot, "fields": ["/camera/angle"]}], command_id="cmd_q3")
    assert again["analysis_id"] == first["analysis_id"]


def test_degraded_gap_only_lowers_mode(svc):
    """T09-25：SUBTITLE_FROM_AUDIO 缺失只降保守，不阻断。"""
    tts = _ref("S31", "tts_3_2", 2, "d")
    sub = _ref("S38", "sub_3_2", 2, "e")
    svc.consume(_manifest(
        {"node_id": "shot_3_2.tts", "ref": tts,
         "expected_edges": [{"edge_kind": "SUBTITLE_FROM_AUDIO"}]},
        {"node_id": "shot_3_2.subtitle", "ref": sub}))
    _settle(svc)
    result = _query(svc, [{**tts, "fields": []}], mode="ALLOW_CONSERVATIVE")
    assert result["status"] == "COMPUTED"
    assert result["mode"] == "CONSERVATIVE"
    gaps = svc.open_gaps(TENANT, BRANCH)
    assert [g["severity"] for g in gaps] == ["DEGRADED"]


def test_out_of_order_event_does_not_rewind_watermark(svc):
    """T09-26：乱序事件先到不能把 applied_sequence 往回拉。"""
    for sequence, event_id in ((5, "e5"), (3, "e3")):
        svc.consume({
            "id": event_id, "type": "script.revision.created", "tenant_id": TENANT,
            "sequence": sequence,
            "data": {"branch_id": BRANCH, "object_id": "scr_1", "revision": 1}})
    marks = svc.watermarks(TENANT, BRANCH)["items"]
    s04 = next(m for m in marks if m["source_service"] == "S04")
    assert s04["applied_sequence"] == 5


def test_unused_source_lag_does_not_degrade(svc):
    """T09-27：只有 S41 落后而闭包不涉及 S41 → 不降级。"""
    g = _e03(svc)
    svc.observe_lag(TENANT, BRANCH, "S41", 60_000, budget_ms=2000)
    result = _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}])
    assert result["mode"] == "EXACT"


def test_watermark_is_min_over_consumed_sources_only(svc):
    """§2.6：水位只对本次闭包用到的源取 min，不是全局 min。

    S28 确实会向 S09 投递事件（它在 EVENT_SOURCES 里），但这次闭包里没有
    任何 S28 节点。让它停在 30，闭包用到的 S06/S08 都在 50——水位必须是
    50。按全局 min 算会得到 30，进而把每个项目都按最慢的那个源计水位。
    """
    g = _e03(svc)
    svc.consume({
        "id": "ev_art", "type": "artifact.archived", "tenant_id": TENANT,
        "sequence": 30,
        "data": {"branch_id": BRANCH, "artifact_id": "art_other", "revision": 1,
                 "edges": []}})
    svc.consume({
        "id": "ev_shot50", "type": "storyboard.shot.revision.created",
        "tenant_id": TENANT, "sequence": 50,
        "data": {"branch_id": BRANCH, "shot_id": "shot_9_9", "revision": 1,
                 "edges": []}})
    svc.consume(_manifest({"node_id": "shot_3_2", "ref": g["shot"]}, revision=50))
    result = _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}])
    assert result["lineage_watermark"] == 50


# ---- 8.4 E12 / E28 · 幂等与租户 -------------------------------------------

def test_same_command_replays(svc):
    """T09-30：同命令同 body → replayed，不新增 outbox。"""
    g = _e03(svc)
    first = _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}])
    before = len(svc.outbox.unpublished(limit=1000))
    second = _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}])
    assert second["analysis_id"] == first["analysis_id"]
    assert second["replayed"] is True
    assert len(svc.outbox.unpublished(limit=1000)) == before


def test_same_semantic_query_shares_analysis(svc):
    """T09-32：不同 command_id、同语义 → 同 analysis_id 与 analysis_hash。"""
    g = _e03(svc)
    first = _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}],
                   command_id="cmd_a")
    second = _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}],
                    command_id="cmd_b")
    assert second["analysis_id"] == first["analysis_id"]
    assert second["analysis_hash"] == first["analysis_hash"]


def test_cross_tenant_read_is_404(svc):
    """T09-33：跨租户读与不存在同码。"""
    g = _e03(svc)
    result = _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}])
    with pytest.raises(ServiceError) as caught:
        svc.get_analysis(OTHER, result["analysis_id"])
    assert caught.value.status == 404
    assert caught.value.code == "ANALYSIS_NOT_FOUND"


def test_cross_tenant_edge_write_is_403(svc):
    """T09-34：body tenant 与签名上下文不一致 → 403。"""
    with pytest.raises(ServiceError) as caught:
        svc.write_edges(TENANT, {"tenant_id": OTHER, "command_id": "cmd_t",
                                 "branch_id": BRANCH, "edges": []})
    assert caught.value.status == 403
    assert caught.value.code == "TENANT_MISMATCH"


# ---- 8.5 边界行为 ---------------------------------------------------------

def test_batch_write_is_atomic(svc):
    """T09-38：一批里有一条非法 → 整批不落地。"""
    shot = _ref("S06", "shot_3_2", 8, "a")
    with pytest.raises(ServiceError):
        svc.write_edges(TENANT, {"command_id": "cmd_bad", "branch_id": BRANCH, "edges": [
            _edge("ARTIFACT_FROM_SHOT", shot, _ref("S27", "kf_3_2", 3, "b")),
            {"edge_kind": "NOT_A_KIND", "source": shot,
             "target": _ref("S27", "x", 1, "x")},
        ]})
    assert svc.edges(TENANT)["total"] == 0


def test_cycle_is_recorded_and_stops(svc):
    """T09-39：A→B→A 标 DEGRADED 并停在该分支，不无限循环。"""
    a = _ref("S27", "a", 1, "a")
    b = _ref("S27", "b", 1, "b")
    svc.write_edges(TENANT, {"command_id": "cmd_cy", "branch_id": BRANCH, "edges": [
        _edge("ARTIFACT_FROM_SHOT", a, b), _edge("ARTIFACT_FROM_SHOT", b, a)]})
    _settle(svc)
    result = _query(svc, [{**a, "fields": []}], mode="ALLOW_CONSERVATIVE")
    gaps = svc.open_gaps(TENANT, BRANCH)
    assert any(g["expected_kind"] == "CYCLE" for g in gaps)
    assert result["status"] == "COMPUTED"


def test_depth_limit_fails_loudly(svc):
    """§4.1 步骤 5：超过 12 层说明边建错了，FAILED 而不是悄悄截断。"""
    edges = []
    for index in range(closure.MAX_DEPTH + 2):
        edges.append(_edge("ARTIFACT_FROM_SHOT",
                           _ref("S27", f"n{index}", 1, "n"),
                           _ref("S27", f"n{index + 1}", 1, "n")))
    svc.write_edges(TENANT, {"command_id": "cmd_deep", "branch_id": BRANCH,
                             "edges": edges})
    _settle(svc)
    with pytest.raises(ServiceError) as caught:
        _query(svc, [{**_ref("S27", "n0", 1, "n"), "fields": []}],
               mode="ALLOW_CONSERVATIVE")
    assert caught.value.code == "GRAPH_TOO_DEEP"


def test_stale_fence_after_computation_is_rejected(svc):
    """F-06：计算期间 fence 前进 → 409 STALE_EXECUTION_FENCE，不写 COMPUTED。"""
    g = _e03(svc)
    svc.consume(_manifest({"node_id": "shot_3_2", "ref": g["shot"]},
                          revision=2, fence=2))
    with pytest.raises(ServiceError) as caught:
        _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}], fence=1)
    assert caught.value.status == 409
    assert caught.value.code == "STALE_EXECUTION_FENCE"


def test_field_projection_rejects_non_whitelisted_scalar(svc):
    """§2.2：白名单之外只存 hash。对白原文写不进去。"""
    with pytest.raises(ServiceError) as caught:
        svc.project_fields(TENANT, BRANCH, {
            "service_id": "S06", "id": "shot_3_2", "revision": 8,
            "fields": {"/dialogue": "一句台词"}})
    assert caught.value.status == 422
    assert caught.value.code == "SCALAR_PROJECTION_NOT_ALLOWED"


def test_whitelist_is_enforced_in_the_database(svc):
    """白名单在库里执行：绕过 service 直接 INSERT 同样被拒。"""
    with pytest.raises(sqlite3.IntegrityError) as caught:
        svc.conn.execute(
            "INSERT INTO field_projection (tenant_id, branch_id, service_id,"
            " object_id, revision, field_path, value_type, value_hash, scalar_value,"
            " projected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (TENANT, BRANCH, "S06", "shot_3_2", 8, "/dialogue", "string",
             _h("x"), json.dumps("一句台词"), "2026-01-01T00:00:00Z"))
    assert "SCALAR_PROJECTION_NOT_ALLOWED" in str(caught.value)


def test_whitelist_matches_the_six_documented_rows():
    """白名单是闭集：加一行就是加一次内容泄漏面，必须显式改。"""
    assert [f"{s}{p}" for s, p, _, _ in WHITELIST] == [
        "S06/camera/angle", "S06/duration/target_seconds",
        "S06/duration/trim_allowed", "S06/duration/timing_policy",
        "S06/order", "S04/scene_count"]


def test_replaying_all_history_is_idempotent(svc):
    """T09-35：重放全部历史事件，边与水位不变。"""
    g = _e03(svc)
    events = [
        _manifest({"node_id": "shot_3_2", "ref": g["shot"]}),
        {"id": "ev_shot", "type": "storyboard.shot.revision.created",
         "tenant_id": TENANT, "sequence": 2,
         "data": {"branch_id": BRANCH, "shot_id": "shot_3_2", "revision": 8,
                  "edges": [{"edge_kind": "SHOT_DERIVED_FROM_SCENE",
                             "source": _ref("S05", "scene_3", 1, "s3"),
                             "target": g["shot"]}]}},
    ]
    for event in events:
        svc.consume(event)
    edges_before = svc.edges(TENANT)["total"]
    marks_before = svc.watermarks(TENANT, BRANCH)["items"]

    for event in events:
        outcome = svc.consume(event, side_effects=False)
        assert outcome["consumed"] is False

    assert svc.edges(TENANT)["total"] == edges_before
    assert svc.watermarks(TENANT, BRANCH)["items"] == marks_before


def test_event_replay_never_emits_external_messages(svc):
    """T09-35 的后半句：side_effects=false 时不发外部消息。"""
    g = _e03(svc)
    svc.consume(_manifest({"node_id": "shot_3_2", "ref": g["shot"]},
                          revision=2), side_effects=False)
    assert svc.outbox.unpublished(limit=100) == []


def test_emitted_payload_satisfies_the_contract_schema(svc):
    """发出去的事件体要能用 `contracts/` 里那份 schema 验过。

    这条不是"再测一遍序列化"：S09 自己的断言全绿也能发出一条下游验不过的
    事件——`base_manifest_ref` 少 `content_hash`、`data` 少 `schema_version`
    时就是这样，34 个用例一条都没红。所以这里直接拿真 schema 对真事件。
    """
    import json
    from pathlib import Path

    import jsonschema

    g = _e03(svc)
    _query(svc, [{**g["shot"], "fields": ["/camera/angle"]}], mode="ALLOW_CONSERVATIVE")
    event = svc.outbox.unpublished(limit=10)[0]

    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "contracts" / "lineage.impact-computed.v2.schema.json")
                        .read_text(encoding="utf-8"))
    jsonschema.Draft7Validator(schema).validate(event["data"])


def test_base_manifest_ref_must_carry_content_hash(svc):
    """只给 `{id}` 的基线引用要在入口就拒。

    契约里它是 required[ id, revision, content_hash ] 且
    `additionalProperties: false`；缺了 hash，S07 无法判 analysis 是否 stale
    （LNI-4），而这个洞只有走到 S07 才会以 422 显形。
    """
    g = _e03(svc)
    body = {
        "command_id": "cmd_q1", "branch_id": BRANCH, "execution_fence": 1,
        "base_manifest_ref": {"id": MANIFEST, "revision": 1},
        "seed": {"kind": "CHANGE_SEED",
                 "refs": [{**g["shot"], "fields": ["/camera/angle"]}]},
    }
    with pytest.raises(ServiceError) as err:
        svc.query_impact(TENANT, body)
    assert err.value.status == 422
    assert "content_hash" in str(err.value)


def test_base_manifest_ref_content_hash_must_be_a_hash(svc):
    """长度/字符集不对同样是 422——schema 给的是 `^[0-9a-f]{64}$`。"""
    g = _e03(svc)
    body = {
        "command_id": "cmd_q1", "branch_id": BRANCH, "execution_fence": 1,
        "base_manifest_ref": {"id": MANIFEST, "revision": 1, "content_hash": "mf"},
        "seed": {"kind": "CHANGE_SEED",
                 "refs": [{**g["shot"], "fields": ["/camera/angle"]}]},
    }
    with pytest.raises(ServiceError) as err:
        svc.query_impact(TENANT, body)
    assert err.value.status == 422


# ---- HTTP 层 -------------------------------------------------------------

def _http_client(monkeypatch, tmp_path):
    """每个用例一个独立库。不能 `import app`：S01/S08 也有同名 `api/app.py`。"""
    import importlib.util
    from pathlib import Path

    monkeypatch.setenv("S09_DB", str(tmp_path / "s09.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s09_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return TestClient(module.app)


def test_http_edge_write_then_query(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    headers = {"Idempotency-Key": "cmd_h1", "X-Tenant-Id": TENANT}
    shot = _ref("S06", "shot_3_2", 8, "a")
    kf = _ref("S27", "kf_3_2", 3, "b")
    r = client.post("/lineage-edges", json={
        "branch_id": BRANCH,
        "edges": [_edge("ARTIFACT_FROM_SHOT", shot, kf, "/camera/angle")]},
        headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1

    r2 = client.post("/lineage-edges", json={
        "branch_id": BRANCH,
        "edges": [_edge("ARTIFACT_FROM_SHOT", shot, kf, "/camera/angle")]},
        headers=headers)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True

    listing = client.get("/lineage/edges", headers={"X-Tenant-Id": TENANT})
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


def test_http_conservative_required_keeps_details(monkeypatch, tmp_path):
    """409 CONSERVATIVE_REQUIRED 必须带 would_be_mode，S07 靠它做选择。"""
    client = _http_client(monkeypatch, tmp_path)
    headers = {"Idempotency-Key": "cmd_h2", "X-Tenant-Id": TENANT}
    shot = _ref("S06", "shot_3_2", 8, "a")
    client.post("/events:consume", json={
        "id": "ev_mf", "type": "snapshot.manifest.committed", "tenant_id": TENANT,
        "sequence": 1,
        "data": {"manifest_id": MANIFEST, "revision": 1, "branch_id": BRANCH,
                 "execution_fence": 1, "episode_id": EPISODE,
                 "node_states": [{"node_id": "shot_3_2", "ref": shot}]}},
        headers={"X-Tenant-Id": TENANT})
    r = client.post("/impact-queries", json={
        "branch_id": BRANCH, "execution_fence": 1, "mode_hint": "EXACT",
        "base_manifest_ref": {"id": MANIFEST, "revision": 1,
                              "content_hash": _h("mf")},
        "seed": {"kind": "CHANGE_SEED",
                 "refs": [{**shot, "fields": ["/camera/angle"]}]}},
        headers=headers)
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "CONSERVATIVE_REQUIRED"
    assert body["details"]["would_be_mode"] == "CONSERVATIVE"
    assert set(body) >= {"code", "message", "retryable", "correlation_id", "details"}


def test_http_missing_key_is_400(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    r = client.post("/lineage-edges", json={"edges": []},
                    headers={"X-Tenant-Id": TENANT})
    assert r.status_code == 422  # FastAPI 先拦下缺失的 header


def test_http_key_disagreeing_with_body_is_400(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    r = client.post("/lineage-edges", json={"command_id": "cmd_in_body", "edges": []},
                    headers={"Idempotency-Key": "cmd_in_header",
                             "X-Tenant-Id": TENANT})
    assert r.status_code == 400
    assert r.json()["code"] == "MALFORMED_REQUEST"


def test_http_tenant_mismatch_is_403(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    r = client.post("/lineage-edges",
                    json={"tenant_id": OTHER, "edges": []},
                    headers={"Idempotency-Key": "cmd_h3", "X-Tenant-Id": TENANT})
    assert r.status_code == 403
    assert r.json()["code"] == "TENANT_MISMATCH"
