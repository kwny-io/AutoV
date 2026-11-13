"""S08 测试。全部用本地 SQLite 与 mock 证据，不接真实依赖、不付费。

对照 plans/S08-revision-snapshot.md §7；用例号沿用方案里的 T08-xx。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import (
    CommandConflict,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from revision_snapshot.schema import SCHEMA
from revision_snapshot.service import RevisionSnapshotService, ServiceError

TENANT = "tnt_a"
OTHER = "tnt_b"
MANIFEST = "mf_ep3"
SERIES = "ser_1"
BRANCH = "main"


def _fp(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _ref(service: str, ident: str, revision: int = 1, seed: str = "x") -> dict:
    return {"service_id": service, "id": ident, "revision": revision,
            "content_hash": _fp(f"{ident}:{revision}:{seed}")}


def _node(node_id: str, epoch: int = 1, fp: str = "a", locked: bool = False,
          accepted: dict | None = None) -> dict:
    return {"node_id": node_id, "node_epoch": epoch, "input_fingerprint": _fp(fp),
            "locked": locked, "accepted_artifact_ref": accepted}


def _manifest(nodes: list[dict], revision: int = 1, fence: int = 1,
              content_seed: str = "v1", episode: str = "ep3",
              branch: str = BRANCH) -> dict:
    body = {
        "schema_version": "2.0",
        "manifest_id": MANIFEST,
        "revision": revision,
        "tenant_id": TENANT,
        "project_id": "prj_1",
        "series_id": SERIES,
        "episode_id": episode,
        "branch_id": branch,
        "execution_fence": fence,
        "home_cell": "cell_a1",
        "source_revisions": [_ref("S04", "scr_1", 1, content_seed)],
        "binding_set_ref": _ref("S10", "bs_1"),
        "memory_checkpoint": {"commit_sequence": 10},
        "policy_ref": _ref("S02", "pol_1"),
        "node_states": nodes,
        "parent_manifest_ref": None,
        "content_hash": _fp(f"{content_seed}:{MANIFEST}"),
    }
    return body


@pytest.fixture()
def svc():
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    service = RevisionSnapshotService(conn)
    service.register_branch(TENANT, {"series_id": SERIES, "branch_id": BRANCH})
    return service


def _commit(svc, nodes, expected_revision=0, fence=1, seed="v1", command_id="cmd_c1",
            episode="ep3"):
    body = {"command_id": command_id, "expected_revision": expected_revision,
            "candidate_manifest": _manifest(nodes, revision=expected_revision + 1,
                                            fence=fence, content_seed=seed,
                                            episode=episode)}
    return svc.commit(TENANT, MANIFEST, body)


def _evidence(svc, manifest_revision, fence, epochs, artifact_hashes):
    svc.ingest_review(TENANT, {
        "decision_id": "dec_1", "decision_revision": 1, "manifest_id": MANIFEST,
        "manifest_revision": manifest_revision, "execution_fence": fence,
        "verdict": "approved", "node_epochs": epochs,
        "artifact_hashes": artifact_hashes,
    })


def _accept(svc, nodes, expected_revision, fence=1, node_ids=None,
            decision=("dec_1", 1), qc_seed=None, command_id="cmd_a1",
            override=None):
    hashes = {n["node_id"]: (n.get("accepted_artifact_ref") or {}).get("content_hash")
              for n in nodes}
    target = node_ids if node_ids is not None else sorted(hashes)
    for index, node_id in enumerate(target):
        svc.ingest_quality(TENANT, {
            "report_id": f"qc_{node_id}", "report_revision": 1, "manifest_id": MANIFEST,
            "manifest_revision": expected_revision, "execution_fence": fence,
            "node_id": node_id, "node_epoch": 1,
            "artifact_hash": qc_seed or hashes[node_id],
        })
    body = {"command_id": command_id, "expected_revision": expected_revision,
            "expected_fence": fence,
            "node_ids": target,
            "decision_ref": {"service_id": "S40", "id": decision[0],
                             "revision": decision[1], "content_hash": _fp("dec")},
            "policy_ref": _ref("S02", "pol_1", 7),
            "qc_report_refs": [{"service_id": "S35", "id": f"qc_{n}", "revision": 1,
                                "content_hash": _fp(f"qc{n}")} for n in target],
            "actor": {"principal_id": "prn_1", "principal_type": "human"},
            "override_reason": override}
    return svc.accept(TENANT, MANIFEST, body)


# ---- 7.1 幂等与版本 -------------------------------------------------------

def test_commit_creates_first_revision(svc):
    out = _commit(svc, [_node("n1"), _node("n2", fp="b")])
    assert out["previous_revision"] == 0
    assert out["manifest"]["revision"] == 1
    assert out["rebased"] is False
    head = svc.get_manifest(TENANT, MANIFEST)
    assert head["current_revision"] == 1
    assert head["accepted_revision"] is None


def test_commit_advances_only_revision(svc):
    _commit(svc, [_node("n1")])
    out = _commit(svc, [_node("n1", fp="b")], expected_revision=1, seed="v2")
    assert out["previous_revision"] == 1
    assert out["manifest"]["revision"] == 2
    assert svc.get_manifest(TENANT, MANIFEST)["current_revision"] == 2


def test_idempotent_replay_does_not_write_twice(svc):
    """T08-01：同 command_id 同 body 重放，只推进一次、Outbox 只 1 条。"""
    conn = svc.conn
    idem = SqliteIdempotencyStore(conn)
    body = {"command_id": "cmd_x", "expected_revision": 0}
    h = canonical_request_hash(body)
    assert idem.begin(TENANT, "cmd_x", h).result is None
    _commit(svc, [_node("n1")], command_id="cmd_x")
    idem.finish(TENANT, "cmd_x", json.dumps({"ok": 1}))

    replay = idem.begin(TENANT, "cmd_x", h)
    assert json.loads(replay.result) == {"ok": 1}
    assert svc.get_manifest(TENANT, MANIFEST)["current_revision"] == 1
    n = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    assert n == 1


def test_same_key_different_body_is_409(svc):
    """T08-02：同键不同 body 是拒绝，不是覆盖。"""
    conn = svc.conn
    idem = SqliteIdempotencyStore(conn)
    idem.begin(TENANT, "cmd_x", canonical_request_hash({"a": 1}))
    with pytest.raises(CommandConflict) as exc:
        idem.begin(TENANT, "cmd_x", canonical_request_hash({"a": 2}))
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert exc.value.retryable is False


def test_duplicate_content_hash_rejected(svc):
    """T08-03：不同版本同 content_hash 是 bug，409。"""
    _commit(svc, [_node("n1")])
    with pytest.raises(ServiceError) as exc:
        _commit(svc, [_node("n1")], expected_revision=1, seed="v1")
    assert exc.value.code == "DUPLICATE_MANIFEST_CONTENT"
    assert exc.value.status == 409


def test_expected_revision_ahead_conflicts(svc):
    """T08-07：客户端臆造 revision。"""
    _commit(svc, [_node("n1")])
    with pytest.raises(ServiceError) as exc:
        _commit(svc, [_node("n1", fp="b")], expected_revision=9, seed="v9")
    assert exc.value.code == "VERSION_CONFLICT"
    assert exc.value.details["head_revision"] == 1


def test_late_commit_rebases_when_nodes_disjoint(svc):
    """T08-04：落后但改动节点不相交 → rebase。"""
    _commit(svc, [_node("n1", fp="a"), _node("n2", fp="b")], expected_revision=0)
    _commit(svc, [_node("n1", fp="a2"), _node("n2", fp="b")],
            expected_revision=1, seed="v2")
    # 晚到的第三方只知道 rev1：它提交的 n1 仍停在 "a"，只有 n2 前进。
    out = _commit(svc, [_node("n1", fp="a"), _node("n2", fp="b2")],
                  expected_revision=1, seed="v3")
    assert out["rebased"] is True
    assert out["rebased_from_revision"] == 1
    assert out["manifest"]["revision"] == 3
    # head rev2 对 n1 的改动必须还在，不能被候选的旧值抹掉
    merged = {n["node_id"]: n["input_fingerprint"]
              for n in out["manifest"]["node_states"]}
    assert merged["n1"] == _fp("a2")
    assert merged["n2"] == _fp("b2")


def test_late_commit_conflicts_when_nodes_overlap(svc):
    """T08-05：改动节点相交 → 409。"""
    _commit(svc, [_node("n1", fp="a"), _node("n2", fp="b")])
    _commit(svc, [_node("n1", fp="a2"), _node("n2", fp="b")],
            expected_revision=1, seed="v2")
    with pytest.raises(ServiceError) as exc:
        _commit(svc, [_node("n1", fp="a3"), _node("n2", fp="b")],
                expected_revision=1, seed="v3")
    assert exc.value.code == "VERSION_CONFLICT"


# ---- 7.3 epoch 与接受 -----------------------------------------------------

def test_only_changed_node_advances_epoch(svc):
    """T08-14：改一个节点只推进它的 epoch，已接受的 A 完全不动。"""
    art_a, art_b = _ref("S29", "img_a", 1), _ref("S29", "img_b", 1)
    nodes = [{"node_id": "A", "node_epoch": 1, "input_fingerprint": _fp("a"),
              "locked": False, "accepted_artifact_ref": art_a},
             {"node_id": "B", "node_epoch": 1, "input_fingerprint": _fp("b"),
              "locked": False, "accepted_artifact_ref": art_b}]
    _commit(svc, nodes)
    _evidence(svc, 1, 1, {"A": 1, "B": 1},
              {"A": art_a["content_hash"], "B": art_b["content_hash"]})
    _accept(svc, nodes, expected_revision=1)
    assert svc.get_manifest(TENANT, MANIFEST)["accepted_revision"] == 1

    # A 的 epoch 停在 1；B 本轮才变，客户端仍报读到的 1，由服务端推进到 2。
    out = _commit(svc, [{"node_id": "A", "node_epoch": 1, "input_fingerprint": _fp("a"),
                         "locked": False, "accepted_artifact_ref": art_a},
                        {"node_id": "B", "node_epoch": 1, "input_fingerprint": _fp("b2"),
                         "locked": False, "accepted_artifact_ref": art_b}],
                  # 接受只写 accepted_revision，不占 current_revision；head 仍是 1。
                  expected_revision=1, seed="v2")
    changed = {c["node_id"]: (c["from"], c["to"]) for c in out["changed_node_epochs"]}
    assert changed == {"B": (1, 2)}
    ptr = svc.accepted_pointers(TENANT, MANIFEST)
    a = next(n for n in ptr["nodes"] if n["node_id"] == "A")
    assert a["node_epoch"] == 1
    assert a["accepted_artifact_ref"] == art_a
    assert a["accepted_at_revision"] == 1


def test_node_epoch_skip_is_422(svc):
    _commit(svc, [_node("n1")])
    with pytest.raises(ServiceError) as exc:
        _commit(svc, [_node("n1", epoch=5, fp="b")], expected_revision=1, seed="v2")
    assert exc.value.code == "NODE_EPOCH_SKIP"
    assert exc.value.status == 422


def test_stale_node_epoch_is_409(svc):
    _commit(svc, [_node("n1")])
    _commit(svc, [_node("n1", fp="b")], expected_revision=1, seed="v2")
    with pytest.raises(ServiceError) as exc:
        _commit(svc, [_node("n1", epoch=1, fp="c")], expected_revision=2, seed="v3")
    assert exc.value.code == "STALE_NODE_EPOCH"
    assert exc.value.status == 409


def test_stale_fence_decision_is_rejected_and_recorded(svc):
    """T08-15：旧 fence 的 QC 报告不能接受，且 REJECTED_STALE 必须留档。"""
    art = _ref("S29", "img_1")
    nodes = [{"node_id": "n1", "node_epoch": 1, "input_fingerprint": _fp("a"),
              "locked": False, "accepted_artifact_ref": art}]
    _commit(svc, nodes)
    _evidence(svc, 1, 1, {"n1": 1}, {"n1": art["content_hash"]})
    svc.fence(TENANT, SERIES, BRANCH,
              {"command_id": "cmd_f1", "expected_fence": 1, "reason": "RUN_CANCELED"})

    with pytest.raises(ServiceError) as exc:
        _accept(svc, nodes, expected_revision=1, fence=1)
    assert exc.value.code in ("STALE_EXECUTION_FENCE", "ILLEGAL_STATE_TRANSITION")
    assert exc.value.status == 409


def test_rejected_accept_leaves_audit_row(svc):
    """E07：晚到且无法 rebase 的审核必须留下 REJECTED_STALE 审计行。"""
    art = _ref("S29", "img_1")
    base = [{"node_id": "n1", "node_epoch": 1, "input_fingerprint": _fp("a"),
             "locked": False, "accepted_artifact_ref": art}]
    _commit(svc, base)
    _evidence(svc, 1, 1, {"n1": 1}, {"n1": art["content_hash"]})
    # 节点输入变了，旧审核无法 rebase
    _commit(svc, [{"node_id": "n1", "node_epoch": 1, "input_fingerprint": _fp("a2"),
                   "locked": False, "accepted_artifact_ref": art}],
            expected_revision=1, seed="v2")

    with pytest.raises(ServiceError) as exc:
        _accept(svc, base, expected_revision=1)
    assert exc.value.code == "STALE_DECISION"

    rows = svc.acceptance_commits(TENANT, MANIFEST, "REJECTED_STALE")
    assert len(rows["acceptance_commits"]) == 1
    detail = rows["acceptance_commits"][0]["reject_detail"]
    assert detail["reason"] in ("INPUT_CHANGED", "NODE_EPOCH_CHANGED")


def test_late_decision_rebases_when_node_unchanged(svc):
    """T08-17：审核指向 rev 1、head 是 2，但该节点没变 → 指针写在 head 上。"""
    art = _ref("S29", "img_1")
    nodes = [{"node_id": "n1", "node_epoch": 1, "input_fingerprint": _fp("a"),
              "locked": False, "accepted_artifact_ref": art},
             {"node_id": "n2", "node_epoch": 1, "input_fingerprint": _fp("b"),
              "locked": False, "accepted_artifact_ref": None}]
    _commit(svc, nodes)
    _evidence(svc, 1, 1, {"n1": 1, "n2": 1}, {"n1": art["content_hash"], "n2": None})
    # 只改 n2
    _commit(svc, [nodes[0], {"node_id": "n2", "node_epoch": 1,
                             "input_fingerprint": _fp("b2"), "locked": False,
                             "accepted_artifact_ref": None}],
            expected_revision=1, seed="v2")

    out = _accept(svc, nodes, expected_revision=1, node_ids=["n1"])
    assert out["rebased"] is True
    assert out["nodes"][0]["accepted_at_revision"] == 2


def test_superseded_revision_can_still_be_rebased(svc):
    """T08-19：旧 revision 被新提交取代后，节点没变的晚到审核仍可 rebase。

    §4 把 SUPERSEDED → ACCEPTED 列为非法转移，但同一节又说晚到的接受走
    §3.3 的 rebase-or-reject 而不是这个转移 —— 两条要一起读：接受写的是
    head 上的指针，旧 revision 自己并不变成 ACCEPTED。
    """
    art = _ref("S29", "img_1")
    n1 = {"node_id": "n1", "node_epoch": 1, "input_fingerprint": _fp("a"),
          "locked": False, "accepted_artifact_ref": art}
    _commit(svc, [n1])
    _evidence(svc, 1, 1, {"n1": 1}, {"n1": art["content_hash"]})
    _commit(svc, [n1, _node("n2")], expected_revision=1, seed="v2")
    assert svc.get_revision(TENANT, MANIFEST, 1)["state"] == "SUPERSEDED"

    out = _accept(svc, [n1], expected_revision=1, node_ids=["n1"])
    assert out["rebased"] is True and out["rebased_from_revision"] == 1
    assert out["accepted_revision"] is None  # n2 还没接受
    ptr = svc.accepted_pointers(TENANT, MANIFEST)
    got = next(n for n in ptr["nodes"] if n["node_id"] == "n1")
    assert got["accepted_at_revision"] == 2
    # 旧 revision 仍是历史，不会因为这次接受被改写成 ACCEPTED
    assert svc.get_revision(TENANT, MANIFEST, 1)["state"] == "SUPERSEDED"


def test_accepted_revision_is_not_superseded_by_next_commit(svc):
    """接受态不是「被取代」：新提交只把旧的 COMMITTED 版本标成 SUPERSEDED。"""
    nodes = [{"node_id": "n1", "node_epoch": 1, "input_fingerprint": _fp("a"),
              "locked": False, "accepted_artifact_ref": _ref("S29", "img_1")}]
    _commit(svc, nodes)
    _evidence(svc, 1, 1, {"n1": 1}, {"n1": nodes[0]["accepted_artifact_ref"]["content_hash"]})
    _accept(svc, nodes, expected_revision=1)
    assert svc.get_revision(TENANT, MANIFEST, 1)["state"] == "ACCEPTED"

    _commit(svc, [dict(nodes[0], input_fingerprint=_fp("a2"))],
            expected_revision=1, seed="v2")
    assert svc.get_revision(TENANT, MANIFEST, 1)["state"] == "ACCEPTED"
    assert svc.get_revision(TENANT, MANIFEST, 2)["state"] == "COMMITTED"


def test_accept_all_nodes_sets_accepted_revision(svc):
    """T08-23：最后一个节点接受时才更新 accepted_revision。"""
    art1, art2 = _ref("S29", "a"), _ref("S29", "b")
    nodes = [{"node_id": "n1", "node_epoch": 1, "input_fingerprint": _fp("a"),
              "locked": False, "accepted_artifact_ref": art1},
             {"node_id": "n2", "node_epoch": 1, "input_fingerprint": _fp("b"),
              "locked": False, "accepted_artifact_ref": art2}]
    _commit(svc, nodes)
    _evidence(svc, 1, 1, {"n1": 1, "n2": 1},
              {"n1": art1["content_hash"], "n2": art2["content_hash"]})

    first = _accept(svc, nodes, expected_revision=1, node_ids=["n1"], command_id="a1")
    assert first["accepted_revision"] is None
    assert svc.get_manifest(TENANT, MANIFEST)["accepted_revision"] is None

    second = _accept(svc, nodes, expected_revision=1, node_ids=["n2"], command_id="a2")
    assert second["accepted_revision"] == 1
    assert svc.get_manifest(TENANT, MANIFEST)["accepted_revision"] == 1


def test_qc_hash_mismatch_is_422(svc):
    """T08-20。"""
    art = _ref("S29", "img_1")
    nodes = [{"node_id": "n1", "node_epoch": 1, "input_fingerprint": _fp("a"),
              "locked": False, "accepted_artifact_ref": art}]
    _commit(svc, nodes)
    _evidence(svc, 1, 1, {"n1": 1}, {"n1": art["content_hash"]})
    with pytest.raises(ServiceError) as exc:
        _accept(svc, nodes, expected_revision=1, qc_seed=_fp("wrong"))
    assert exc.value.code == "QC_EVIDENCE_MISMATCH"
    assert exc.value.status == 422


def test_locked_node_needs_override(svc):
    """T08-21。"""
    art = _ref("S29", "img_1")
    nodes = [{"node_id": "n1", "node_epoch": 1, "input_fingerprint": _fp("a"),
              "locked": True, "accepted_artifact_ref": art}]
    _commit(svc, nodes)
    _evidence(svc, 1, 1, {"n1": 1}, {"n1": art["content_hash"]})
    with pytest.raises(ServiceError) as exc:
        _accept(svc, nodes, expected_revision=1)
    assert exc.value.code == "NODE_LOCKED"
    out = _accept(svc, nodes, expected_revision=1, override="manual fix",
                  command_id="cmd_a2")
    assert out["accepted_revision"] == 1


def test_fence_voids_and_blocks_accept(svc):
    """T08-18：fence 后旧版本 VOIDED，再接受是非法转移。"""
    art = _ref("S29", "img_1")
    nodes = [{"node_id": "n1", "node_epoch": 1, "input_fingerprint": _fp("a"),
              "locked": False, "accepted_artifact_ref": art}]
    _commit(svc, nodes)
    _evidence(svc, 1, 1, {"n1": 1}, {"n1": art["content_hash"]})
    out = svc.fence(TENANT, SERIES, BRANCH,
                    {"command_id": "cmd_f1", "expected_fence": 1, "reason": "RUN_CANCELED"})
    assert out["execution_fence"] == 2
    assert out["voided_revisions"] == [1]
    with pytest.raises(ServiceError) as exc:
        _accept(svc, nodes, expected_revision=1, fence=2, command_id="cmd_a9")
    assert exc.value.code == "ILLEGAL_STATE_TRANSITION"


def test_fence_conflict_is_409_retryable(svc):
    """`:fence` 的 expected 不匹配。"""
    with pytest.raises(ServiceError) as exc:
        svc.fence(TENANT, SERIES, BRANCH,
                  {"command_id": "cmd_f1", "expected_fence": 7, "reason": "MANUAL"})
    assert exc.value.code == "FENCE_CONFLICT"
    assert exc.value.retryable is True


# ---- 7.3b node-diff -------------------------------------------------------

@pytest.fixture()
def diff_setup(svc):
    art = _ref("S29", "img_1")
    base = [{"node_id": "A", "node_epoch": 1, "input_fingerprint": _fp("a"),
             "locked": False, "accepted_artifact_ref": art},
            {"node_id": "B", "node_epoch": 1, "input_fingerprint": _fp("b"),
             "locked": False, "accepted_artifact_ref": None}]
    _commit(svc, base)
    _commit(svc, [base[0],
                  {"node_id": "B", "node_epoch": 1, "input_fingerprint": _fp("b2"),
                   "locked": False, "accepted_artifact_ref": None},
                  {"node_id": "C", "node_epoch": 1, "input_fingerprint": _fp("c"),
                   "locked": False, "accepted_artifact_ref": None}],
            expected_revision=1, seed="v2")
    return svc


def test_node_diff_reports_unchanged_and_input_changed(diff_setup):
    """T08-23a：A 未变，B 输入变，C 新增；summary 与 nodes 自洽。"""
    out = diff_setup.node_diff(TENANT, MANIFEST, 1)
    by = {n["node_id"]: n["change"] for n in out["nodes"]}
    assert by["A"] == "UNCHANGED"
    assert by["B"] == "INPUT_CHANGED"
    assert by["C"] == "ADDED"
    assert out["to_revision"] == 2
    assert sum(out["summary"].values()) == len(out["nodes"])


def test_node_diff_removed_on_reverse(diff_setup):
    """T08-23c：反向查询把 C 视为 REMOVED。"""
    out = diff_setup.node_diff(TENANT, MANIFEST, 2, 1)
    by = {n["node_id"]: n["change"] for n in out["nodes"]}
    assert by["C"] == "REMOVED"


def test_node_diff_input_and_accepted_changed(diff_setup):
    """T08-23b：输入与 accepted ref 同时变化时合成一条。"""
    art2 = _ref("S29", "img_2")
    base = [{"node_id": "A", "node_epoch": 1, "input_fingerprint": _fp("a"),
             "locked": False, "accepted_artifact_ref": art2},
            # B 在 fixture 第二版里已推进到 epoch 2，客户端必须报读到的 2。
            {"node_id": "B", "node_epoch": 2, "input_fingerprint": _fp("b2"),
             "locked": False, "accepted_artifact_ref": None},
            {"node_id": "C", "node_epoch": 1, "input_fingerprint": _fp("c"),
             "locked": False, "accepted_artifact_ref": None}]
    # epoch 由服务端按 input_fingerprint 推进；客户端仍报读到的 1。
    _commit(diff_setup, [{"node_id": "A", "node_epoch": 1,
                          "input_fingerprint": _fp("a2"), "locked": False,
                          "accepted_artifact_ref": art2}] + base[1:],
            expected_revision=2, seed="v3")
    out = diff_setup.node_diff(TENANT, MANIFEST, 2, 3)
    a = next(n for n in out["nodes"] if n["node_id"] == "A")
    assert a["change"] == "INPUT_AND_ACCEPTED_CHANGED"


def test_node_diff_voided_revision_still_readable(svc):
    """T08-23j：作废不等于不可读 —— 历史不可变，diff 照常返回。"""
    _commit(svc, [_node("n1")])
    _commit(svc, [_node("n1", fp="b")], expected_revision=1, seed="v2")
    svc.fence(TENANT, SERIES, BRANCH,
              {"command_id": "cmd_f2", "expected_fence": 1, "reason": "MANUAL"})
    out = svc.node_diff(TENANT, MANIFEST, 1, 2)
    assert out["from_revision"] == 1 and out["to_revision"] == 2
    assert out["nodes"][0]["change"] == "INPUT_CHANGED"


def test_node_diff_too_large_is_413_without_partial_result(svc):
    """T08-23h：超 2000 节点报 413 并提示用 node_id 缩小，不给半截结果。"""
    _commit(svc, [_node(f"n{i}") for i in range(2001)])
    with pytest.raises(ServiceError) as exc:
        svc.node_diff(TENANT, MANIFEST, 1)
    assert exc.value.status == 413
    assert exc.value.code == "NODE_DIFF_TOO_LARGE"
    # 收窄到单节点可以正常返回，说明 413 不是能力缺失
    one = svc.node_diff(TENANT, MANIFEST, 1, node_id="n0")
    assert one["nodes"][0]["node_id"] == "n0"


def test_node_diff_locked_flip_is_unchanged(diff_setup):
    """T08-23d：只翻转 locked 不影响 change。"""
    diff_setup.fence(TENANT, SERIES, BRANCH,
                     {"command_id": "f0", "expected_fence": 1, "reason": "MANUAL"})
    _commit(diff_setup, [{"node_id": "A", "node_epoch": 1,
                          "input_fingerprint": _fp("a"), "locked": True,
                          "accepted_artifact_ref": _ref("S29", "img_1")}],
            expected_revision=2, fence=2, seed="v4")
    out = diff_setup.node_diff(TENANT, MANIFEST, 2, 3, node_id="A")
    a = out["nodes"][0]
    assert a["change"] == "UNCHANGED"
    assert a["from_locked"] is False and a["to_locked"] is True


def test_node_diff_etag_is_stable(diff_setup):
    """T08-23i：同一 (from,to) 的结果与 ETag 不变。"""
    one = diff_setup.node_diff(TENANT, MANIFEST, 1, 2)
    two = diff_setup.node_diff(TENANT, MANIFEST, 1, 2)
    assert one["etag"] == two["etag"]
    assert one["nodes"] == two["nodes"]


def test_node_diff_unknown_revision_404(diff_setup):
    """T08-23g。"""
    with pytest.raises(ServiceError) as exc:
        diff_setup.node_diff(TENANT, MANIFEST, 1, 99)
    assert exc.value.status == 404
    assert exc.value.code == "REVISION_NOT_FOUND"


# ---- 7.4 安全与租户 -------------------------------------------------------

def test_cross_tenant_read_is_404(svc):
    """T08-24 / E28。"""
    _commit(svc, [_node("n1")])
    with pytest.raises(ServiceError) as exc:
        svc.get_manifest(OTHER, MANIFEST)
    assert exc.value.status == 404
    assert MANIFEST not in str(exc.value)


def test_cross_tenant_commit_creates_nothing(svc):
    """T08-25：别人的 manifest_id 不会跨租户写入。"""
    _commit(svc, [_node("n1")])
    with pytest.raises(ServiceError):
        svc.commit(OTHER, MANIFEST,
                   {"command_id": "c9", "expected_revision": 1,
                    "candidate_manifest": _manifest([_node("n1", fp="z")])})
    assert svc.get_manifest(TENANT, MANIFEST)["current_revision"] == 1


def test_candidate_tenant_mismatch_is_403(svc):
    """T08-26：body 里的 tenant 与认证租户不一致。"""
    cand = _manifest([_node("n1")])
    cand["tenant_id"] = OTHER
    with pytest.raises(ServiceError) as exc:
        svc.commit(TENANT, MANIFEST,
                   {"command_id": "c9", "expected_revision": 0,
                    "candidate_manifest": cand})
    assert exc.value.status == 403
    assert exc.value.code == "TENANT_SCOPE_DENIED"


# ---- 7.5 事件 -------------------------------------------------------------

def test_commit_emits_one_event_per_revision(svc):
    """每次提交一条事件，aggregate_sequence = revision。"""
    _commit(svc, [_node("n1")], command_id="c1")
    _commit(svc, [_node("n1", fp="b")], expected_revision=1, seed="v2",
            command_id="c2")
    rows = svc.conn.execute(
        "SELECT sequence, event_type FROM outbox WHERE aggregate_id = ?"
        " ORDER BY sequence", (MANIFEST,)).fetchall()
    assert [(r["sequence"], r["event_type"]) for r in rows] == [
        (1, "snapshot.manifest.committed"),
        (2, "snapshot.manifest.committed"),
    ]


def test_first_event_has_null_parent_and_accepted(svc):
    """SMC-4：首次提交的事件里 parent/accepted 必然为空，schema 目前无法表达。"""
    _commit(svc, [_node("n1")])
    row = svc.conn.execute(
        "SELECT payload FROM outbox WHERE aggregate_id = ?", (MANIFEST,)).fetchone()
    data = json.loads(row["payload"])["data"]
    assert data["parent_manifest_ref"] is None
    assert data["accepted_revision"] is None
    assert data["trigger"] == "COMMIT"


def test_duplicate_evidence_is_ignored(svc):
    """T08-28：同一证据投递 3 次只落 1 行。"""
    for _ in range(3):
        svc.ingest_quality(TENANT, {
            "report_id": "qc_1", "report_revision": 1, "manifest_id": MANIFEST,
            "manifest_revision": 1, "execution_fence": 1, "node_id": "n1",
            "node_epoch": 1, "artifact_hash": _fp("art")})
    n = svc.conn.execute("SELECT COUNT(*) FROM qc_evidence").fetchone()[0]
    assert n == 1


def test_evidence_never_moves_pointer(svc):
    """E07 结构性保证：证据事件不能推进 accepted pointer。"""
    _commit(svc, [{"node_id": "n1", "node_epoch": 1,
                   "input_fingerprint": _fp("a"), "locked": False,
                   "accepted_artifact_ref": _ref("S29", "img_1")}])
    svc.ingest_review(TENANT, {
        "decision_id": "dec_1", "decision_revision": 1, "manifest_id": MANIFEST,
        "manifest_revision": 1, "execution_fence": 1, "verdict": "approved",
        "node_epochs": {"n1": 1}, "artifact_hashes": {}})
    assert svc.get_manifest(TENANT, MANIFEST)["accepted_revision"] is None
    ptr = svc.accepted_pointers(TENANT, MANIFEST)
    assert all(n["accepted_at_revision"] is None for n in ptr["nodes"])


# ---- HTTP 层 ------------------------------------------------------------

def _http_client(monkeypatch, tmp_path):
    """每个用例一个独立库：app.py 在 import 时建表，所以要先指好路径再加载。

    不能 `import app`：S01 也有 `api/app.py` 且先入 sys.modules，平名导入会拿到
    另一个服务的路由表。这里按本服务自己的文件路径加载一个独立模块。
    """
    import importlib.util
    from pathlib import Path

    monkeypatch.setenv("S08_DB", str(tmp_path / "s08.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s08_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return TestClient(module.app)


def test_http_commit_replay_and_409_on_different_body(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    body = {"expected_revision": 0,
            "candidate_manifest": _manifest([_node("n1")], revision=1)}
    h = {"Idempotency-Key": "cmd_http_1", "X-Tenant-Id": TENANT}
    r1 = client.post(f"/manifests/{MANIFEST}:commit", json=body, headers=h)
    assert r1.status_code == 200, r1.text
    assert r1.json()["replayed"] is False

    r2 = client.post(f"/manifests/{MANIFEST}:commit", json=body, headers=h)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["manifest"]["revision"] == r1.json()["manifest"]["revision"]

    other = {"expected_revision": 0,
             "candidate_manifest": _manifest([_node("n1", fp="z")], revision=1,
                                             content_seed="other")}
    r3 = client.post(f"/manifests/{MANIFEST}:commit", json=other, headers=h)
    assert r3.status_code == 409
    assert r3.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert r3.json()["retryable"] is False


def test_http_rejects_key_that_disagrees_with_body_command_id(monkeypatch, tmp_path):
    """C08-4：头与 body 不一致时无从判断重放哪一笔，必须 400。"""
    client = _http_client(monkeypatch, tmp_path)
    body = {"command_id": "cmd_in_body", "expected_revision": 0,
            "candidate_manifest": _manifest([_node("n1")], revision=1)}
    r = client.post(f"/manifests/{MANIFEST}:commit", json=body,
                    headers={"Idempotency-Key": "cmd_in_header",
                             "X-Tenant-Id": TENANT})
    assert r.status_code == 400
    assert r.json()["code"] == "MALFORMED_REQUEST"


def test_http_error_body_carries_the_envelope(monkeypatch, tmp_path):
    """422 也要带 code/retryable/details，客户端才能按契约分支。"""
    client = _http_client(monkeypatch, tmp_path)
    client.post(f"/manifests/{MANIFEST}:commit",
                json={"expected_revision": 0,
                      "candidate_manifest": _manifest([_node("n1")], revision=1)},
                headers={"Idempotency-Key": "cmd_http_2", "X-Tenant-Id": TENANT})
    r = client.post(f"/manifests/{MANIFEST}:commit",
                    json={"expected_revision": 1,
                          "candidate_manifest": _manifest(
                              [{"node_id": "n1", "node_epoch": 5,
                                "input_fingerprint": _fp("a2"), "locked": False,
                                "accepted_artifact_ref": None}], revision=2,
                              content_seed="v2")},
                    headers={"Idempotency-Key": "cmd_http_3", "X-Tenant-Id": TENANT})
    assert r.status_code == 422
    err = r.json()
    assert err["code"] == "NODE_EPOCH_SKIP"
    assert set(err) >= {"code", "message", "retryable", "correlation_id", "details"}
    assert err["retryable"] is False


def test_http_queries_read_back_what_commit_wrote(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    client.post(f"/manifests/{MANIFEST}:commit",
                json={"expected_revision": 0,
                      "candidate_manifest": _manifest([_node("n1")], revision=1)},
                headers={"Idempotency-Key": "cmd_http_4", "X-Tenant-Id": TENANT})
    h = {"X-Tenant-Id": TENANT}
    head = client.get(f"/manifests/{MANIFEST}", headers=h)
    assert head.status_code == 200
    assert head.json()["current_revision"] == 1
    rev = client.get(f"/manifests/{MANIFEST}/revisions/1", headers=h)
    assert rev.status_code == 200
    diff = client.get(f"/manifests/{MANIFEST}/node-diff",
                      params={"from_revision": 1}, headers=h)
    assert diff.status_code == 200
    assert diff.json()["summary"]["unchanged"] == 1
