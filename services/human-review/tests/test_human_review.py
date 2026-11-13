"""S40 测试。对照 `plans/S40-human-review.md` §11 的用例表。

每个用例断言能失败的具体值。最关键的一条是 `APPLIED` 的入口唯一性 ——
`test_applied_is_only_reachable_through_s08` 用 AST 与运行时两条证据证明它：
没有 S08 成功结果时，任何路径都写不出 `APPLIED`。

没有用真的 S01/S02/S08 实例（那会要求跨服务的完整环境），但仍守住 S18 测试的
原则：桩的行为按**真实接口**写（S01 返回 permissions 不带 roles；S08 两步
evidence+accept），不是按内核希望的样子写。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import SqliteIdempotencyStore

from human_review.schema import CONTRACT_GAPS
from human_review.service import (
    HumanReviewService,
    ServiceError,
    compare_binding,
    pack_hash,
)

TA = "t_aaa"
TB = "t_bbb"
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64


def _conn(*, share_thread: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None,
                           check_same_thread=not share_thread)
    conn.row_factory = sqlite3.Row
    return conn


def _binding(**over):
    base = {
        "manifest_ref": {"id": "mf_1", "revision": 4, "content_hash": H1},
        "node_id": "node_shot_2",
        "node_epoch": 4,
        "execution_fence": 7,
        "lock_version": 2,
        "artifact": {"content_hash": H2},
        "policy_ref": {"content_hash": H3},
        "home_cell": "cell_a",
    }
    base.update(over)
    return base


def _task_body(**over):
    body = {
        "kind": "gate_acceptance",
        "binding": _binding(),
        "evidence": {"qc_reports": [{"id": "qc_1", "revision": 2,
                                     "content_hash": H1}]},
        "subject": {"scope_kind": "project", "scope_id": "prj_demo",
                    "summary": "第3场第2镜待接受"},
        "reason": {"code": "QC_PASSED"},
        "options": [{"option_id": "accept", "label": "接受"},
                    {"option_id": "request_repair", "label": "要求修复"}],
        "assignment": {"quorum": {"required": 1},
                       "assignee_roles": ["director"],
                       "assignee_principal_id": "p_1",
                       "sla": {"deadline": "2099-01-01T00:00:00.000Z"}},
        "created_by": {"principal_id": "p_1"},
    }
    body.update(over)
    return body


def _ctx(task: dict, **over) -> dict:
    """从任务冻结的 binding 造一份"现在"的上下文。默认全部一致。"""
    b = task["binding"]
    ctx = {
        "manifest_ref": {"id": b["manifest_ref"]["id"],
                         "revision": b["manifest_ref"]["revision"],
                         "content_hash": b["manifest_ref"]["content_hash"]},
        "node_id": b["node_id"],
        "node_epoch": b["node_epoch"],
        "execution_fence": b["execution_fence"],
        "lock_version": b["lock_version"],
        "artifact": {"content_hash": b["artifact"]["content_hash"]},
        "policy_ref": {"content_hash": b["policy_ref"]["content_hash"]},
        "pack_hash": b["pack_hash"],
        "tenant_id": TA,
    }
    ctx.update(over)
    return ctx


def _decision_body(task: dict, command_id: str = "cmd_1", **over) -> dict:
    body = {
        "context": _ctx(task),
        "decision": "accept",
        "option_id": "accept",
        "reason": "第3场第2镜已修复，可以接受",
        "actor": {"principal_id": "p_1", "roles": ["director"]},
        "expected_task_revision": task["revision"],
        "command_id": command_id,
    }
    body.update(over)
    return body


# ---- 桩：按真实接口写 ------------------------------------------------

def _authz_ok(tenant_id, principal_id, task):
    """S01 的真实形状：permissions / denied，**没有 roles**。"""
    return {"tenant_id": tenant_id, "principal_id": principal_id,
            "principal_status": "active", "tenant_lifecycle": "active",
            "scope_kind": "project", "scope_id": "prj_demo",
            "permissions": ["review.decide"], "denied": []}


def _authz_revoked(tenant_id, principal_id, task):
    return {"principal_status": "suspended", "tenant_lifecycle": "active",
            "permissions": [], "denied": []}


def _authz_denied(tenant_id, principal_id, task):
    return {"principal_status": "active", "tenant_lifecycle": "active",
            "permissions": [], "denied": ["review.decide"]}


def _policy_allow(tenant_id, task, body, authz):
    return {"verdict": "allow", "policy_ref": {"content_hash": H3}}


def _policy_deny(tenant_id, task, body, authz):
    return {"verdict": "deny", "reason": "GATE_ALREADY_SATISFIED"}


def _policy_abstain(tenant_id, task, body, authz):
    return {"verdict": "abstain", "reason": "POLICY_UNAVAILABLE"}


def _policy_require_human(tenant_id, task, body, authz):
    return {"verdict": "require_human", "reason": "SEPARATION_OF_DUTIES"}


def _cas_committed(tenant_id, task, command_id):
    return {"result": "committed", "acceptance_commit_id": "ac_1",
            "accepted_revision": 4}


def _cas_conflict(tenant_id, task, command_id):
    return {"result": "conflict", "code": "VERSION_CONFLICT"}


def _cas_stale_epoch(tenant_id, task, command_id):
    return {"result": "stale_epoch", "code": "STALE_DECISION"}


def _cas_locked(tenant_id, task, command_id):
    return {"result": "locked", "code": "NODE_LOCKED"}


def _cas_down(tenant_id, task, command_id):
    raise ConnectionError("S08 is down")


def _service(*, authz=_authz_ok, policy=_policy_allow, cas=_cas_committed,
             node_diff=None) -> HumanReviewService:
    return HumanReviewService(
        _conn(), authz_source=authz, policy_validator=policy,
        acceptance_cas=cas, node_diff=node_diff)


# ---- E05 任务生命周期 --------------------------------------------------

def test_create_freezes_binding_and_computes_own_pack_hash():
    svc = _service()
    task = svc.create_task(TA, _task_body())
    assert task["state"] == "OPEN"
    assert task["revision"] == 1
    expected = pack_hash(_binding(), _task_body()["evidence"])
    assert task["binding"]["pack_hash"] == expected
    assert task["pack_hash"] == expected
    assert len(task["pack_hash"]) == 64


def test_caller_supplied_pack_hash_is_ignored():
    """调用方能给哈希就能给一个永远匹配的 —— 核心是服务算。"""
    svc = _service()
    task = svc.create_task(TA, _task_body(binding=_binding(pack_hash="0" * 64)))
    assert task["pack_hash"] != "0" * 64
    assert task["pack_hash"] == pack_hash(_binding(), _task_body()["evidence"])


def test_binding_is_immutable_at_the_database_layer():
    svc = _service()
    task = svc.create_task(TA, _task_body())
    with pytest.raises(sqlite3.DatabaseError) as exc:
        svc.conn.execute(
            "UPDATE review_task SET binding=? WHERE tenant_id=?"
            " AND review_task_id=?", ("{}", TA, task["review_task_id"]))
    assert "immutable" in str(exc.value)


def test_cross_tenant_task_is_404_not_403():
    svc = _service()
    task = svc.create_task(TA, _task_body())
    with pytest.raises(ServiceError) as exc:
        svc.get_task(TB, task["review_task_id"])
    assert exc.value.status == 404
    assert exc.value.code == "TASK_NOT_FOUND"


def test_quorum_required_must_be_positive():
    svc = _service()
    with pytest.raises(ServiceError) as exc:
        svc.create_task(TA, _task_body(
            assignment={"quorum": {"required": 0}}))
    assert exc.value.status == 400


# ---- E07 决定与状态机 --------------------------------------------------

def test_accept_reaches_applied_only_after_s08_commits():
    svc = _service()
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(TA, task["review_task_id"],
                              _decision_body(task), command_id="cmd_1")
    assert out["status"] == "APPLIED"
    assert out["downstream"]["cas_result"]["result"] == "committed"
    assert svc.get_task(TA, task["review_task_id"])["state"] == "DECIDED"


def test_reject_is_recorded_but_never_advances_the_pointer():
    """G11：S08 不因 reject 推指针；任务仍 DECIDED，但下游写者是 null。"""
    svc = _service(policy=_policy_allow,
                   cas=lambda t, task, c: {"result": "recorded_only"})
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(
        TA, task["review_task_id"],
        _decision_body(task, decision="reject", option_id="reject",
                       reason="手部仍有异常"), command_id="cmd_rej")
    assert out["status"] == "RECORDED"
    assert out["downstream"]["cas_result"]["result"] == "recorded_only"
    final = svc.get_task(TA, task["review_task_id"])
    assert final["state"] == "DECIDED"
    row = svc.conn.execute(
        "SELECT payload FROM outbox WHERE tenant_id=?", (TA,)).fetchone()
    data = json.loads(row["payload"])["data"]
    assert data["accepted_pointer_written_by"] is None
    assert data["acceptance_commit_id"] is None


def test_applied_is_only_reachable_through_s08():
    """两个方向的证明：源码只有一处写 APPLIED，运行时无 S08 就到不了。"""
    source = Path(__file__).resolve().parents[1] / "src" / "human_review" / \
        "service.py"
    text = source.read_text(encoding="utf-8")
    tree = ast.parse(text)

    writers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        body = ast.get_source_segment(text, node) or ""
        # 只数**写入**，不数读取：`status IN ('RECORDED','APPLIED')` 是查询，
        # `SET status='APPLIED'` 才是改写。
        if "status='APPLIED'" in body:
            writers.append(node.name)
    assert writers == ["apply_downstream"], writers

    # 运行时不变量：S08 不给 committed，就写不出 APPLIED。
    for cas, expected in ((_cas_conflict, "STALE"),
                          (_cas_stale_epoch, "STALE"),
                          (_cas_locked, "STALE")):
        svc = _service(cas=cas)
        task = svc.create_task(TA, _task_body())
        out = svc.submit_decision(TA, task["review_task_id"],
                                  _decision_body(task), command_id="cmd_x")
        assert out["status"] == expected
        applied = svc.conn.execute(
            "SELECT COUNT(*) AS n FROM review_decision WHERE tenant_id=?"
            " AND status='APPLIED'", (TA,)).fetchone()["n"]
        assert applied == 0


def test_s08_unreachable_keeps_recorded_and_enqueues_retry():
    """§5.1。不可达不是"大概能过"。"""
    svc = _service(cas=_cas_down)
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(TA, task["review_task_id"],
                              _decision_body(task), command_id="cmd_1")
    assert out["status"] == "RECORDED"
    assert out["pending_acceptance"] is True

    pending = svc.pending_acceptance(TA)["pending"]
    assert len(pending) == 1
    assert pending[0]["review_task_id"] == task["review_task_id"]
    assert pending[0]["attempt_count"] == 1
    assert "down" in pending[0]["last_error"]

    # 决定仍然是 RECORDED，本地判定没有偷偷升级成 APPLIED。
    again = svc.get_task(TA, task["review_task_id"])
    assert again["state"] != "DECIDED"
    assert [d["status"] for d in again["decisions"]] == ["RECORDED"]


def test_retry_with_same_command_id_applies_when_s08_returns():
    """重试走同一 command_id：S08 幂等，且 quorum 不重复计。"""
    calls = {"n": 0}
    state = {"up": False}

    def flaky(tenant_id, task, command_id):
        calls["n"] += 1
        if not state["up"]:
            raise ConnectionError("still down")
        return {"result": "committed", "acceptance_commit_id": "ac_9"}

    svc = _service(cas=flaky)
    task = svc.create_task(TA, _task_body())
    svc.submit_decision(TA, task["review_task_id"], _decision_body(task),
                        command_id="cmd_1")
    assert calls["n"] == 1

    state["up"] = True
    svc.retry_pending(TA, task["review_task_id"])
    final = svc.get_task(TA, task["review_task_id"])
    assert final["state"] == "DECIDED"
    assert [d["status"] for d in final["decisions"]] == ["APPLIED"]
    # quorum 仍是 1：重试没有再记一票。
    assert final["quorum"]["votes_valid"] == 1
    assert svc.pending_acceptance(TA)["pending"] == []


def test_policy_abstain_is_not_persisted():
    """§4：abstain 不落库。未知不能变成一张有效票（E25）。"""
    svc = _service(policy=_policy_abstain)
    task = svc.create_task(TA, _task_body())
    with pytest.raises(ServiceError) as exc:
        svc.submit_decision(TA, task["review_task_id"],
                            _decision_body(task), command_id="cmd_1")
    assert exc.value.status == 503
    assert exc.value.code == "POLICY_ABSTAINED"
    rows = svc.conn.execute(
        "SELECT COUNT(*) AS n FROM review_decision WHERE tenant_id=?",
        (TA,)).fetchone()["n"]
    assert rows == 0
    assert svc.get_task(TA, task["review_task_id"])["quorum"]["votes_valid"] == 0


def test_policy_deny_is_persisted_as_invalid_and_not_counted():
    svc = _service(policy=_policy_deny)
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(TA, task["review_task_id"],
                              _decision_body(task), command_id="cmd_1")
    assert out["status"] == "REJECTED_INVALID"
    q = svc.get_task(TA, task["review_task_id"])["quorum"]
    assert q["votes_valid"] == 0
    assert q["votes_invalid"] == 1


def test_unconfigured_policy_validator_is_503_not_allow():
    """G7：S02 没有这个接口。配了 url 也是 abstain，不配就是 503。"""
    svc = HumanReviewService(_conn(), authz_source=_authz_ok,
                             policy_validator=None, acceptance_cas=_cas_committed)
    task = svc.create_task(TA, _task_body())
    with pytest.raises(ServiceError) as exc:
        svc.submit_decision(TA, task["review_task_id"],
                            _decision_body(task), command_id="cmd_1")
    assert exc.value.status == 503
    assert exc.value.code == "POLICY_VALIDATOR_UNCONFIGURED"


def test_require_human_records_the_vote_but_stays_partially_decided():
    svc = _service(policy=_policy_require_human)
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(TA, task["review_task_id"],
                              _decision_body(task), command_id="cmd_1")
    assert out["status"] == "RECORDED"
    final = svc.get_task(TA, task["review_task_id"])
    assert final["state"] == "PARTIALLY_DECIDED"
    assert final["quorum"]["votes_valid"] == 1
    assert [d["status"] for d in final["decisions"]] == ["RECORDED"]


def test_reason_shorter_than_four_chars_is_422():
    svc = _service()
    task = svc.create_task(TA, _task_body())
    with pytest.raises(ServiceError) as exc:
        svc.submit_decision(TA, task["review_task_id"],
                            _decision_body(task, reason="好"),
                            command_id="cmd_1")
    assert exc.value.status == 422
    assert exc.value.code == "REASON_TOO_SHORT"


def test_exception_accept_needs_twenty_chars():
    svc = _service()
    task = svc.create_task(TA, _task_body())
    short = "手部异常，例外接受"
    assert len(short) < 20
    with pytest.raises(ServiceError) as exc:
        svc.submit_decision(
            TA, task["review_task_id"],
            _decision_body(task, option_id="exception_accept", reason=short),
            command_id="cmd_1")
    assert exc.value.code == "REASON_TOO_SHORT"

    ok = svc.submit_decision(
        TA, task["review_task_id"],
        _decision_body(task, option_id="exception_accept",
                       reason="第3场第2镜手部异常不影响叙事，本轮例外接受并登记为缺陷"),
        command_id="cmd_2")
    assert ok["option_id"] == "exception_accept"


# ---- E25 授权与职责分离 ------------------------------------------------

def test_revoked_actor_is_rejected_invalid_and_persisted():
    """§8.2 复审失败不能用旧快照继续（E27 的落点）。"""
    svc = _service(authz=_authz_revoked)
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(TA, task["review_task_id"],
                              _decision_body(task), command_id="cmd_1")
    assert out["status"] == "REJECTED_INVALID"
    assert out["validation"]["code"] == "NOT_AUTHORIZED_FOR_TASK"
    assert out["validation"]["authz"]["problem"] == "PRINCIPAL_OR_TENANT_INACTIVE"


def test_explicit_deny_beats_present_permission():
    svc = _service(authz=_authz_denied)
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(TA, task["review_task_id"],
                              _decision_body(task), command_id="cmd_1")
    assert out["status"] == "REJECTED_INVALID"
    assert out["validation"]["authz"]["problem"] == "EXPLICITLY_DENIED"


def test_missing_review_decide_permission_is_rejected():
    svc = _service(authz=lambda t, p, task: {
        "principal_status": "active", "tenant_lifecycle": "active",
        "permissions": ["context.preview"], "denied": []})
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(TA, task["review_task_id"],
                              _decision_body(task), command_id="cmd_1")
    assert out["status"] == "REJECTED_INVALID"
    assert out["validation"]["authz"]["problem"] == "MISSING_PERMISSION"


def test_unconfigured_authz_is_503_not_reject():
    svc = HumanReviewService(_conn(), authz_source=None,
                             policy_validator=_policy_allow,
                             acceptance_cas=_cas_committed)
    task = svc.create_task(TA, _task_body())
    with pytest.raises(ServiceError) as exc:
        svc.submit_decision(TA, task["review_task_id"],
                            _decision_body(task), command_id="cmd_1")
    assert exc.value.status == 503
    assert exc.value.code == "AUTHZ_UNCONFIGURED"


def test_separation_of_duties_blocks_until_every_role_votes():
    """S01 不返回 roles（G8），角色只能来自 actor.roles。缺了就判不满足。"""
    svc = _service()
    task = svc.create_task(TA, _task_body(assignment={
        "quorum": {"required": 2, "separation_of_duties": True,
                   "must_include_roles": ["director", "legal"]},
        "assignee_roles": ["director", "legal"],
        "sla": {"deadline": "2099-01-01T00:00:00.000Z"}}))
    tid = task["review_task_id"]

    def vote(principal, roles, command_id):
        # 每票都带"当前"revision：闸 B 比的是页面加载时的版本，投票之间任务
        # revision 会前进（IN_REVIEW 升版），拿 t0 的 body 复用会撞 409。
        current = svc.get_task(TA, tid)
        return svc.submit_decision(
            TA, tid, _decision_body(task, command_id=command_id,
                                    expected_task_revision=current["revision"],
                                    actor={"principal_id": principal,
                                           "roles": roles}),
            command_id=command_id)

    first = vote("p_1", ["director"], "cmd_a")
    assert first["status"] == "RECORDED"
    assert svc.get_task(TA, tid)["state"] == "IN_REVIEW"

    # 两票到手但只有 director —— 职责分离未满足，不释放。
    second = vote("p_2", ["director"], "cmd_b")
    assert second["status"] == "RECORDED"
    assert svc.get_task(TA, tid)["quorum"]["votes_valid"] == 2
    assert svc.get_task(TA, tid)["state"] != "DECIDED"

    third = vote("p_3", ["legal"], "cmd_c")
    assert third["status"] == "APPLIED"
    assert svc.get_task(TA, tid)["state"] == "DECIDED"


# ---- E27 幂等与一人一票 ------------------------------------------------

def test_same_command_id_same_body_replays_without_double_counting():
    svc = _service(cas=_cas_down)
    task = svc.create_task(TA, _task_body())
    body = _decision_body(task)
    first = svc.submit_decision(TA, task["review_task_id"], body,
                                command_id="cmd_1")
    second = svc.submit_decision(TA, task["review_task_id"], body,
                                 command_id="cmd_1")
    assert second["decision_id"] == first["decision_id"]
    assert svc.get_task(TA, task["review_task_id"])["quorum"]["votes_valid"] == 1


def test_same_command_id_different_body_is_409():
    svc = _service(cas=_cas_down)
    task = svc.create_task(TA, _task_body())
    svc.submit_decision(TA, task["review_task_id"], _decision_body(task),
                        command_id="cmd_1")
    with pytest.raises(ServiceError) as exc:
        svc.submit_decision(
            TA, task["review_task_id"],
            _decision_body(task, reason="换了一个完全不同的理由"),
            command_id="cmd_1")
    assert exc.value.status == 409
    assert exc.value.code == "IDEMPOTENCY_MISMATCH"


def test_same_actor_same_option_is_idempotent():
    svc = _service(cas=_cas_down)
    task = svc.create_task(TA, _task_body())
    first = svc.submit_decision(TA, task["review_task_id"],
                                _decision_body(task, command_id="cmd_1"),
                                command_id="cmd_1")
    second = svc.submit_decision(TA, task["review_task_id"],
                                 _decision_body(task, command_id="cmd_2"),
                                 command_id="cmd_2")
    assert second["decision_id"] == first["decision_id"]
    assert svc.get_task(TA, task["review_task_id"])["quorum"]["votes_valid"] == 1


def test_same_actor_different_option_is_409():
    svc = _service(cas=_cas_down)
    task = svc.create_task(TA, _task_body())
    svc.submit_decision(TA, task["review_task_id"],
                        _decision_body(task, command_id="cmd_1"),
                        command_id="cmd_1")
    with pytest.raises(ServiceError) as exc:
        svc.submit_decision(
            TA, task["review_task_id"],
            _decision_body(task, command_id="cmd_2", decision="reject",
                           option_id="reject", reason="改主意了"),
            command_id="cmd_2")
    assert exc.value.status == 409
    assert exc.value.code == "DUPLICATE_VOTE_DIFFERENT_OPTION"
    assert exc.value.details["prior_option"] == "accept"


# ---- C-01..C-10 契约与形状 ---------------------------------------------

def test_stale_vote_is_not_counted_toward_quorum():
    svc = _service()
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(
        TA, task["review_task_id"],
        _decision_body(task, context=_ctx(task, execution_fence=8)),
        command_id="cmd_1")
    assert out["status"] == "STALE"
    assert out["stale_reason"] == "EXECUTION_FENCE_ADVANCED"
    q = svc.get_task(TA, task["review_task_id"])["quorum"]
    assert q["votes_valid"] == 0
    assert q["votes_invalid"] == 1


def test_node_epoch_and_execution_fence_are_checked_independently():
    """AGENTS.md：两者不能混用。只动一个，只能报出那一个。"""
    svc = _service()
    task = svc.create_task(TA, _task_body())

    fence_only = svc.submit_decision(
        TA, task["review_task_id"],
        _decision_body(task, command_id="cmd_f",
                       context=_ctx(task, execution_fence=9)),
        command_id="cmd_f")
    assert fence_only["stale_reason"] == "EXECUTION_FENCE_ADVANCED"

    epoch_only = svc.submit_decision(
        TA, task["review_task_id"],
        _decision_body(task, command_id="cmd_e",
                       context=_ctx(task, node_epoch=9)),
        command_id="cmd_e")
    assert epoch_only["stale_reason"] == "NODE_EPOCH_ADVANCED"


def test_lock_version_and_artifact_and_policy_and_pack_all_make_it_stale():
    svc = _service()
    task = svc.create_task(TA, _task_body())
    cases = [
        ({"lock_version": 99}, "NODE_LOCK_CHANGED"),
        ({"artifact": {"content_hash": "9" * 64}}, "ARTIFACT_REPLACED"),
        ({"policy_ref": {"content_hash": "9" * 64}}, "POLICY_SUPERSEDED"),
        ({"pack_hash": "9" * 64}, "EVIDENCE_CHANGED"),
        ({"manifest_ref": {"id": "mf_other", "revision": 4}}, "MANIFEST_CHANGED"),
    ]
    for i, (over, expected) in enumerate(cases):
        out = svc.submit_decision(
            TA, task["review_task_id"],
            _decision_body(task, command_id=f"cmd_{i}",
                           actor={"principal_id": f"p_{i}",
                                  "roles": ["director"]},
                           context=_ctx(task, **over)),
            command_id=f"cmd_{i}")
        assert out["status"] == "STALE", (over, out["status"])
        assert out["stale_reason"] == expected, (over, out["stale_reason"])


def test_manifest_revision_bump_without_node_change_is_not_stale():
    """§6.4 第 1 条：改别的镜头不该让本节点的决定失效。"""
    seen = {}

    def node_diff(manifest_id, from_rev, to_rev, node_id):
        seen.update(manifest_id=manifest_id, from_rev=from_rev,
                    to_rev=to_rev, node_id=node_id)
        return False

    svc = _service(node_diff=node_diff)
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(
        TA, task["review_task_id"],
        _decision_body(task, context=_ctx(
            task, manifest_ref={"id": "mf_1", "revision": 5})),
        command_id="cmd_1")
    assert out["status"] == "APPLIED", out
    assert seen == {"manifest_id": "mf_1", "from_rev": 4, "to_rev": 5,
                    "node_id": "node_shot_2"}


def test_manifest_revision_bump_without_node_diff_degrades_to_stale():
    """没有 node diff 能力时更严，不是更松。"""
    svc = _service(node_diff=None)
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(
        TA, task["review_task_id"],
        _decision_body(task, context=_ctx(
            task, manifest_ref={"id": "mf_1", "revision": 5})),
        command_id="cmd_1")
    assert out["status"] == "STALE"
    assert out["stale_reason"] == "MANIFEST_AFFECTS_NODE"
    assert "degraded" in out["validation"]["mismatches"][0]


def test_manifest_revision_bump_that_touches_the_node_is_stale():
    svc = _service(node_diff=lambda m, a, b, n: True)
    task = svc.create_task(TA, _task_body())
    out = svc.submit_decision(
        TA, task["review_task_id"],
        _decision_body(task, context=_ctx(
            task, manifest_ref={"id": "mf_1", "revision": 5})),
        command_id="cmd_1")
    assert out["stale_reason"] == "MANIFEST_AFFECTS_NODE"
    assert out["validation"]["mismatches"][0]["nodes"] == ["node_shot_2"]


def test_expected_task_revision_mismatch_is_409_before_any_write():
    svc = _service()
    task = svc.create_task(TA, _task_body())
    with pytest.raises(ServiceError) as exc:
        svc.submit_decision(
            TA, task["review_task_id"],
            _decision_body(task, expected_task_revision=2),
            command_id="cmd_1")
    assert exc.value.status == 409
    assert exc.value.code == "TASK_SUPERSEDED"
    assert exc.value.details["current_revision"] == 1
    assert svc.conn.execute(
        "SELECT COUNT(*) AS n FROM review_decision WHERE tenant_id=?",
        (TA,)).fetchone()["n"] == 0


def test_late_vote_on_a_superseded_task_is_history_only():
    svc = _service()
    task = svc.create_task(TA, _task_body())
    svc.supersede(TA, task["review_task_id"], "MANIFEST_ADVANCED")
    after = svc.get_task(TA, task["review_task_id"])
    out = svc.submit_decision(
        TA, task["review_task_id"],
        _decision_body(after), command_id="cmd_late")
    assert out["status"] == "STALE"
    assert out["stale_reason"] == "TASK_SUPERSEDED"
    assert svc.get_task(TA, task["review_task_id"])["state"] == "SUPERSEDED"


def test_expiry_neither_accepts_nor_leaves_live_votes():
    """§7：超时不自动通过。"""
    svc = _service(cas=_cas_down)
    task = svc.create_task(TA, _task_body(assignment={
        "quorum": {"required": 2},
        "sla": {"deadline": "2000-01-01T00:00:00.000Z"}}))
    svc.submit_decision(TA, task["review_task_id"],
                        _decision_body(task, command_id="cmd_1"),
                        command_id="cmd_1")

    out = svc.expire_due(TA, now="2099-06-01T00:00:00.000Z")
    assert out["expired"] == [task["review_task_id"]]

    final = svc.get_task(TA, task["review_task_id"])
    assert final["state"] == "EXPIRED"
    assert [d["status"] for d in final["decisions"]] == ["STALE"]
    assert final["decisions"][0]["stale_reason"] == "DEADLINE_PASSED"
    assert svc.conn.execute(
        "SELECT COUNT(*) AS n FROM review_decision WHERE tenant_id=?"
        " AND status='APPLIED'", (TA,)).fetchone()["n"] == 0


def test_contract_decisions_reject_extended_values():
    """C40-1：`replace` 必须走 option_id，不能顶替 decision。"""
    svc = _service()
    task = svc.create_task(TA, _task_body())
    with pytest.raises(ServiceError) as exc:
        svc.submit_decision(
            TA, task["review_task_id"],
            _decision_body(task, decision="replace"),
            command_id="cmd_1")
    assert exc.value.status == 400
    assert exc.value.code == "CONTRACT_DECISION_REQUIRED"

    ok = svc.submit_decision(
        TA, task["review_task_id"],
        _decision_body(task, decision="reject", option_id="replace",
                       reason="换用候选 B，手部动作另拍"),
        command_id="cmd_2")
    assert ok["decision"] == "reject"
    assert ok["option_id"] == "replace"


def test_decision_payload_carries_no_reason_text_in_the_event():
    """§10.2：理由正文可能含敏感内容，只进决定本体。"""
    svc = _service()
    task = svc.create_task(TA, _task_body())
    secret = "这段评审意见里有不该外泄的名字：张三"
    svc.submit_decision(TA, task["review_task_id"],
                        _decision_body(task, reason=secret),
                        command_id="cmd_1")
    rows = svc.conn.execute("SELECT payload FROM outbox WHERE tenant_id=?",
                            (TA,)).fetchall()
    assert rows
    for row in rows:
        text = row["payload"]
        assert "张三" not in text
        assert secret not in text
    # 决定本体里仍在，审计可查。
    decision = svc.get_decision(TA, svc.get_task(
        TA, task["review_task_id"])["decisions"][0]["decision_id"])
    assert decision["reason"] == secret


# ---- HTTP 层 ------------------------------------------------------------

def _client(monkeypatch, svc: HumanReviewService) -> TestClient:
    """TestClient 在 worker 线程里跑 handler，所以连接必须 share_thread。"""
    spec = importlib.util.spec_from_file_location(
        "s40_app", Path(__file__).resolve().parents[1] / "api" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.service = HumanReviewService(
        _conn(share_thread=True), authz_source=svc._authz,
        policy_validator=svc._policy, acceptance_cas=svc._cas,
        node_diff=svc._node_diff)
    module._idem = SqliteIdempotencyStore(module.service.conn)
    # 模块级 import 的 _idem 绑在 app.py 自己的连接上，这里必须换掉，
    # 否则幂等和内核看的是两个库。
    return TestClient(module.app)


def test_http_create_then_decide_round_trips(monkeypatch):
    svc = _service()
    client = _client(monkeypatch, svc)

    created = client.post("/review-tasks", json=_task_body(),
                          headers={"Idempotency-Key": "cmd_create",
                                   "X-Tenant-Id": TA})
    assert created.status_code == 201, created.text
    task = created.json()

    decided = client.post(
        f"/review-tasks/{task['review_task_id']}/decisions",
        json=_decision_body(task),
        headers={"Idempotency-Key": "cmd_1", "X-Tenant-Id": TA})
    assert decided.status_code == 202, decided.text
    body = decided.json()
    assert body["status"] == "APPLIED"
    assert body["replayed"] is False

    fetched = client.get(f"/review-tasks/{task['review_task_id']}",
                         headers={"X-Tenant-Id": TA})
    assert fetched.status_code == 200
    assert fetched.json()["state"] == "DECIDED"


def test_http_body_tenant_mismatch_is_403(monkeypatch):
    svc = _service()
    client = _client(monkeypatch, svc)
    body = _task_body(tenant_id=TB)
    response = client.post("/review-tasks", json=body,
                           headers={"Idempotency-Key": "cmd_x",
                                    "X-Tenant-Id": TA})
    assert response.status_code == 403
    assert response.json()["code"] == "TENANT_MISMATCH"


def test_http_contract_envelope_reports_unchecked_binding_dimensions(monkeypatch):
    """C40-2：契约信封只能带四维。剩下三维是「没查」，不是「通过」。"""
    svc = _service()
    client = _client(monkeypatch, svc)
    task = client.post("/review-tasks", json=_task_body(),
                       headers={"Idempotency-Key": "cmd_create",
                                "X-Tenant-Id": TA}).json()

    envelope_ctx = {
        "schema_version": "2",
        "tenant_id": TA,
        "manifest_ref": {"id": "mf_1", "revision": 4, "content_hash": H1},
        "node_id": "node_shot_2",
        "node_epoch": 4,
        "execution_fence": 7,
        "policy_ref": {"content_hash": H3},
    }
    response = client.post(
        f"/review-tasks/{task['review_task_id']}/decisions",
        json={"context": envelope_ctx, "decision": "accept",
              "reason": "第3场第2镜可以接受", "command_id": "cmd_env",
              "actor": {"principal_id": "p_1", "roles": ["director"]}},
        headers={"Idempotency-Key": "cmd_env", "X-Tenant-Id": TA})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["context_shape"] == "contract_envelope"
    assert body["binding_dimensions_unchecked"] == [
        "lock_version", "artifact.content_hash", "pack_hash"]
    assert body["binding_dimensions_checked"] == [
        "manifest_ref.id", "node_epoch", "execution_fence",
        "policy_ref.content_hash"]
    assert any("C40-2" in gap for gap in body["x-contract-gaps"])


def test_http_envelope_still_catches_a_fence_bump(monkeypatch):
    """信封少三维，但 fence 那一维还是真的在查。"""
    svc = _service()
    client = _client(monkeypatch, svc)
    task = client.post("/review-tasks", json=_task_body(),
                       headers={"Idempotency-Key": "cmd_create",
                                "X-Tenant-Id": TA}).json()
    envelope_ctx = {
        "tenant_id": TA,
        "manifest_ref": {"id": "mf_1", "revision": 4, "content_hash": H1},
        "node_id": "node_shot_2",
        "node_epoch": 4,
        "execution_fence": 8,          # 变了
        "policy_ref": {"content_hash": H3},
    }
    response = client.post(
        f"/review-tasks/{task['review_task_id']}/decisions",
        json={"context": envelope_ctx, "decision": "accept",
              "reason": "第3场第2镜可以接受", "command_id": "cmd_env",
              "actor": {"principal_id": "p_1", "roles": ["director"]}},
        headers={"Idempotency-Key": "cmd_env", "X-Tenant-Id": TA})
    assert response.status_code == 202, response.text
    assert response.json()["stale_reason"] == "EXECUTION_FENCE_ADVANCED"


def test_http_envelope_tenant_mismatch_inside_context_is_403(monkeypatch):
    """信封自带 tenant_id，同样只能用于比对。"""
    svc = _service()
    client = _client(monkeypatch, svc)
    task = client.post("/review-tasks", json=_task_body(),
                       headers={"Idempotency-Key": "cmd_create",
                                "X-Tenant-Id": TA}).json()
    response = client.post(
        f"/review-tasks/{task['review_task_id']}/decisions",
        json={"context": {"tenant_id": TB, "manifest_ref": {"id": "mf_1"}},
              "decision": "accept", "reason": "第3场第2镜可以接受",
              "command_id": "cmd_env"},
        headers={"Idempotency-Key": "cmd_env", "X-Tenant-Id": TA})
    assert response.status_code == 403


def test_http_replay_is_flagged(monkeypatch):
    svc = _service()
    client = _client(monkeypatch, svc)
    task = client.post("/review-tasks", json=_task_body(),
                       headers={"Idempotency-Key": "cmd_create",
                                "X-Tenant-Id": TA}).json()
    headers = {"Idempotency-Key": "cmd_1", "X-Tenant-Id": TA}
    url = f"/review-tasks/{task['review_task_id']}/decisions"
    first = client.post(url, json=_decision_body(task), headers=headers).json()
    second = client.post(url, json=_decision_body(task), headers=headers).json()
    assert second["replayed"] is True
    assert second["decision_id"] == first["decision_id"]


def test_http_readiness(monkeypatch):
    client = _client(monkeypatch, _service())
    assert client.get("/health/readiness").json() == {"status": "ready"}


def test_every_contract_gap_is_non_empty_and_cites_feedback():
    """缺口清单一律随响应带出，不能有空条目。"""
    assert CONTRACT_GAPS
    for gap in CONTRACT_GAPS:
        assert isinstance(gap, str) and len(gap) > 60
        assert gap.startswith("S40 ")


# ---- 纯函数的边界 ------------------------------------------------------

def test_compare_binding_ignores_absent_context_fields():
    """没给 ≠ 没变。空 ctx 不能报任何 mismatch。"""
    assert compare_binding(_binding(), {}) == []


def test_pack_hash_is_deterministic_and_evidence_sensitive():
    a = pack_hash(_binding(), _task_body()["evidence"])
    b = pack_hash(_binding(), _task_body()["evidence"])
    assert a == b
    changed = {"qc_reports": [{"id": "qc_1", "revision": 3,
                               "content_hash": H1}]}
    assert pack_hash(_binding(), changed) != a


# ---- 方向复查挖出来的四条 -----------------------------------------------

def test_option_claiming_pointer_advance_is_refused_at_create():
    """E25-07。S40 兑现不了的承诺不落库，也不静默抹掉。"""
    svc = _service()
    body = _task_body(options=[{"option_id": "accept", "label": "接受",
                                "effect": {"advances_pointer": True}}])
    with pytest.raises(ServiceError) as exc:
        svc.create_task(TA, body)
    assert exc.value.status == 400
    assert exc.value.code == "OPTION_CLAIMS_POINTER_ADVANCE"
    assert svc.conn.execute(
        "SELECT COUNT(*) AS n FROM review_task WHERE tenant_id=?",
        (TA,)).fetchone()["n"] == 0

    # 显式 false 是允许的，且不会变成 true。
    ok = svc.create_task(TA, _task_body(options=[
        {"option_id": "accept", "label": "接受",
         "effect": {"advances_pointer": False}}]))
    assert ok["options"][0]["effect"]["advances_pointer"] is False


def test_every_task_termination_publishes_an_event():
    """状态终结后下游必须能看见。四种终结各发一次。"""
    def events(svc, task_id):
        rows = svc.conn.execute(
            "SELECT payload FROM outbox WHERE tenant_id=?", (TA,)).fetchall()
        return [json.loads(r["payload"])["data"] for r in rows
                if json.loads(r["payload"])["data"].get("review_task_id")
                == task_id]

    # 1. committed
    svc = _service()
    t = svc.create_task(TA, _task_body())
    svc.submit_decision(TA, t["review_task_id"], _decision_body(t),
                        command_id="c1")
    last = events(svc, t["review_task_id"])[-1]
    assert last["task_state"] == "DECIDED"

    # 2. S08 说窗口里变了 → SUPERSEDED
    svc = _service(cas=_cas_stale_epoch)
    t = svc.create_task(TA, _task_body())
    svc.submit_decision(TA, t["review_task_id"], _decision_body(t),
                        command_id="c1")
    last = events(svc, t["review_task_id"])[-1]
    assert last["task_state"] == "SUPERSEDED"
    assert last["accepted_pointer_written_by"] is None

    # 3. 显式 supersede
    svc = _service()
    t = svc.create_task(TA, _task_body())
    svc.supersede(TA, t["review_task_id"], "MANIFEST_ADVANCED")
    assert events(svc, t["review_task_id"])[-1]["task_state"] == "SUPERSEDED"

    # 4. 过期
    svc = _service()
    t = svc.create_task(TA, _task_body(assignment={
        "quorum": {"required": 1},
        "sla": {"deadline": "2000-01-01T00:00:00.000Z"}}))
    svc.expire_due(TA, now="2099-06-01T00:00:00.000Z")
    last = events(svc, t["review_task_id"])[-1]
    assert last["task_state"] == "EXPIRED"
    assert last["accepted_pointer_written_by"] is None


def test_terminal_state_is_not_republished_on_repeat_supersede():
    """重复/乱序的 supersede 幂等（C-04）：终态不回退，也不重复发事件。"""
    svc = _service()
    task = svc.create_task(TA, _task_body())
    svc.supersede(TA, task["review_task_id"], "MANIFEST_ADVANCED")
    first = svc.get_task(TA, task["review_task_id"])
    count = svc.conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE tenant_id=?",
        (TA,)).fetchone()["n"]

    svc.supersede(TA, task["review_task_id"], "MANIFEST_ADVANCED")
    again = svc.get_task(TA, task["review_task_id"])
    assert again["state"] == "SUPERSEDED"
    assert again["revision"] == first["revision"]
    assert svc.conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE tenant_id=?",
        (TA,)).fetchone()["n"] == count


def test_http_flat_context_is_not_mistaken_for_the_contract_envelope(monkeypatch):
    """扁平 ctx 也带 `context` 键。别因为它长得像信封就谎报只查了四维。"""
    svc = _service()
    client = _client(monkeypatch, svc)
    task = client.post("/review-tasks", json=_task_body(),
                       headers={"Idempotency-Key": "cmd_create",
                                "X-Tenant-Id": TA}).json()
    response = client.post(
        f"/review-tasks/{task['review_task_id']}/decisions",
        json=_decision_body(task),
        headers={"Idempotency-Key": "cmd_1", "X-Tenant-Id": TA})
    assert response.status_code == 202, response.text
    body = response.json()
    assert "context_shape" not in body
    assert "binding_dimensions_unchecked" not in body
    assert body["status"] == "APPLIED"


def test_policy_abstain_does_not_call_s08_at_all():
    """E25-06：abstain 不落库、不计票，也不去碰接受权威。"""
    calls = {"n": 0}

    def cas(tenant_id, task, command_id):
        calls["n"] += 1
        return {"result": "committed"}

    svc = _service(policy=_policy_abstain, cas=cas)
    task = svc.create_task(TA, _task_body())
    with pytest.raises(ServiceError):
        svc.submit_decision(TA, task["review_task_id"],
                            _decision_body(task), command_id="cmd_1")
    assert calls["n"] == 0


def test_stale_path_never_calls_s08():
    """E07-10：本地闸就拦下了，别让 S08 白跑一次 CAS。"""
    calls = {"n": 0}

    def cas(tenant_id, task, command_id):
        calls["n"] += 1
        return {"result": "committed"}

    svc = _service(cas=cas)
    task = svc.create_task(TA, _task_body())
    for i, over in enumerate(({"node_epoch": 5}, {"lock_version": 9},
                              {"execution_fence": 9})):
        svc.submit_decision(
            TA, task["review_task_id"],
            _decision_body(task, command_id=f"cmd_{i}",
                           actor={"principal_id": f"p_{i}"},
                           context=_ctx(task, **over)),
            command_id=f"cmd_{i}")
    assert calls["n"] == 0


def test_rotation_of_authorization_between_votes_only_affects_that_vote():
    """E27-04：一人被撤权只影响他那票，quorum 重新计算。"""
    revoked = {"p_2"}
    svc = _service(
        authz=lambda t, p, task: (
            _authz_revoked(t, p, task) if p in revoked
            else _authz_ok(t, p, task)),
        cas=_cas_down)
    task = svc.create_task(TA, _task_body(assignment={
        "quorum": {"required": 3},
        "sla": {"deadline": "2099-01-01T00:00:00.000Z"}}))
    tid = task["review_task_id"]

    svc.submit_decision(TA, tid, _decision_body(
        task, command_id="c1", actor={"principal_id": "p_1"},
        expected_task_revision=1), command_id="c1")
    out = svc.submit_decision(TA, tid, _decision_body(
        task, command_id="c2", actor={"principal_id": "p_2"},
        expected_task_revision=svc.get_task(TA, tid)["revision"]),
        command_id="c2")
    assert out["status"] == "REJECTED_INVALID"

    q = svc.get_task(TA, tid)["quorum"]
    assert q["votes_valid"] == 1
    assert q["votes_invalid"] == 1
    assert svc.get_task(TA, tid)["state"] == "IN_REVIEW"
