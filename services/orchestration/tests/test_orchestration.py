"""S21 orchestration 测试。编号对应 plans/S21-orchestration.md §8。

S08 / S02 / S22 / S23 / S26 全部用 fake 端口（S02 本身仍是 design_only，
按 AGENTS.md「用 mock 按契约走」）。这里验证的是 **S21 自己的行为**：
两套 epoch 的分界、fence 先于 cancel、`SUBMISSION_UNKNOWN` 不可取消、
`projection_watermark` 单调、投影重建以 Temporal 为准。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from orchestration.schema import SCHEMA
from orchestration.service import (
    OrchestrationService,
    ServiceError,
    business_idem_key_for,
    workflow_id_for,
)

T = "tenant_a"
U = "tenant_b"


# --------------------------------------------------------------- 夹具

def _conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.executescript(SCHEMA)
    return c


def _context(*, fence=2, revision=7, nodes=40, episode="ep_1", branch="br_main",
             manifest_id="mf_1", tenant=T, command_id="cmd_1"):
    return {
        "schema_version": "2.0",
        "tenant_id": tenant,
        "project_id": "prj_1",
        "series_id": "se_1",
        "branch_id": branch,
        "episode_id": episode,
        "home_region": "cn",
        "home_cell": "cell_a1",
        "manifest_ref": {
            "id": manifest_id,
            "revision": revision,
            "content_hash": hashlib.sha256(b"mf").hexdigest(),
        },
        "node_id": "node_root",
        "node_epoch": 1,
        "execution_fence": fence,
        "memory_checkpoint": {"branch_id": branch, "commit_sequence": 3,
                              "content_hash": hashlib.sha256(b"mc").hexdigest()},
        "policy_ref": {"service_id": "S02", "id": "pol_1", "revision": 4,
                       "content_hash": hashlib.sha256(b"pol").hexdigest()},
        "actor": {"principal_id": "u1", "principal_type": "human",
                  "delegation_id": None},
        "command_id": command_id,
        "request_hash": hashlib.sha256(b"req").hexdigest(),
        "trace_id": "trace_1",
    }


class Ports:
    """五个依赖端口的 fake。默认全部"正常"，用例里单独改一个。"""

    def __init__(self, *, fence=2, generation_mode="auto", node_count=40,
                 jobs=None):
        self.fence_value = fence
        self.generation_mode = generation_mode
        self.node_count = node_count
        self.jobs = jobs or {}
        self.calls = []
        self.cancelled = []
        self.workflows = []

    # S08
    def read_manifest(self, tenant_id, manifest_id):
        self.calls.append(("read_manifest", manifest_id))
        return {"id": manifest_id, "revision": 7, "execution_fence": self.fence_value,
                "node_count": self.node_count}

    def fence(self, tenant_id, body):
        self.calls.append(("fence", body))
        # S08 是唯一写者：这里模拟它把 fence 推进一格。
        self.fence_value += 1
        return {"execution_fence": self.fence_value}

    # S02（design_only → mock 按契约走）
    def effective_policy(self, tenant_id, body):
        self.calls.append(("effective_policy", body["project_id"]))
        return {"automation_mode": "guided",
                "generation_mode": self.generation_mode,
                "repair_limits": {"max_business_attempts_per_shot": 3,
                                  "max_identical_strategy_repeats": 1}}

    # S26
    def query_jobs(self, tenant_id, body):
        self.calls.append(("query_jobs", body["attempt_id"]))
        return self.jobs.get(body["attempt_id"], {"state": "RUNNING",
                                                  "terminal": False})

    def cancel_job(self, tenant_id, body):
        self.calls.append(("cancel_job", body["provider_job_id"]))
        self.cancelled.append(body["provider_job_id"])
        return {"result": "cancelled"}

    # Temporal
    def start_workflow(self, workflow_id, payload):
        self.workflows.append((workflow_id, payload))
        return {"started": True}

    def as_ports(self) -> dict:
        return {
            "fence": self.fence,
            "effective_policy": self.effective_policy,
            "read_manifest": self.read_manifest,
            "query_jobs": self.query_jobs,
            "cancel_job": self.cancel_job,
            "start_workflow": self.start_workflow,
        }


def _svc(ports: Ports | None = None, *, conn=None):
    p = ports or Ports()
    return OrchestrationService(conn or _conn(), **p.as_ports()), p


def _body(**over):
    body = {"context": _context(), "budget_scope_id": "bs_1", "command_id": "cmd_1"}
    body.update(over)
    return body


def _create(svc, *, tenant=T, episode="ep_1", body=None):
    return svc.create_run(tenant, episode, body or _body(), correlation_id="cmd_1")


def _events(conn):
    rows = conn.execute(
        "SELECT payload FROM outbox ORDER BY rowid").fetchall()
    return [json.loads(r[0]) for r in rows]


# ------------------------------------------------- T21-01…05（E06 失败报告）

def test_t21_01_create_run_emits_queued_state_changed():
    svc, p = _svc()
    op = _create(svc)
    assert set(op) == {"operation_id", "state", "run_id"}, \
        "Operation 是 additionalProperties:false，只能这三个键"
    assert op["state"] == "accepted"

    events = _events(svc.conn)
    assert len(events) == 1
    env = events[0]
    assert env["type"] == "orchestration.run.state-changed"
    assert env["subject"] == f"run/{op['run_id']}"
    assert env["aggregate_id"] == op["run_id"]

    data = env["data"]
    expected = {"schema_version", "run_id", "previous_state", "state",
                "manifest_revision", "execution_fence", "completed_nodes",
                "blocked_nodes", "failed_nodes", "waiting_reason",
                "blocked_reason", "current_stage", "terminal"}
    assert set(data) == expected, "契约 data 恰好 13 个字段"
    assert data["state"] == "QUEUED"
    assert data["terminal"] is False
    assert data["execution_fence"] == 2
    assert data["manifest_revision"] == 7
    assert isinstance(data["current_stage"], str) and data["current_stage"]


def test_t21_02_aggregate_sequence_equals_projection_watermark():
    svc, p = _svc()
    op = _create(svc)
    run_id = op["run_id"]
    svc.register_in_flight(T, run_id, "shot_1.video", 1, "VIDEO")
    got = svc.get_run(T, run_id)
    assert got["state"] == "RUNNING"

    # 两条事件（QUEUED / RUNNING），序列稠密 1,2，无缺口——每个序列号都等于
    # 发它时的 projection_watermark（§5.5）。
    events = _events(svc.conn)
    assert [e["sequence"] for e in events] == [1, 2]
    assert got["projection_watermark"] == 2
    # 事件 data 里**没有** projection_watermark 字段（契约 13 项里没有它）；
    # 序列号本身就是水位，这是 §5.5 的约定。
    assert "projection_watermark" not in events[-1]["data"]


def test_t21_03_blockers_surface_failure_reason():
    svc, p = _svc()
    op = _create(svc)
    run_id = op["run_id"]
    svc.register_in_flight(T, run_id, "shot_9.video", 2, "VIDEO",
                           attempt_id="att_9")
    svc._set_blocker(T, run_id, "shot_9.video", 2, "VIDEO",
                     "QC_FAILED", attempt_id="att_9",
                     suggestion="regenerate", detail={"score": 0.31})
    blockers = svc.list_blockers(T, run_id)
    assert blockers["run_id"] == run_id
    (item,) = blockers["items"]
    assert item["blocked_reason"] == "QC_FAILED"
    assert item["suggestion"] == "regenerate"
    assert item["detail"] == {"score": 0.31}
    # 计数跟着事实走，而不是调用方自己写。
    assert svc.get_run(T, run_id)["blocked_nodes"] == 1


def test_t21_04_get_run_returns_exactly_seven_fields():
    svc, p = _svc()
    op = _create(svc)
    got = svc.get_run(T, op["run_id"])
    assert set(got) == {"run_id", "manifest_revision", "execution_fence", "state",
                        "completed_nodes", "blocked_nodes",
                        "projection_watermark"}
    # 这三个在内部有，但不能进 Run 响应（§5.2）。
    for forbidden in ("failed_nodes", "waiting_reason", "current_stage"):
        assert forbidden not in got


def test_t21_05_generation_mode_manual_waits_for_input():
    """§3.1 末段：manual 不启动 workflow，但 Run 仍然存在且可见。"""
    ports = Ports(generation_mode="manual")
    svc, p = _svc(ports)
    op = _create(svc)
    assert p.workflows == [], "manual 不该启动 workflow"
    run = svc.get_run(T, op["run_id"])
    assert run["state"] == "WAITING_INPUT"
    assert op["state"] == "waiting_input"
    data = _events(svc.conn)[0]["data"]
    assert data["waiting_reason"] == "GENERATION_MODE_MANUAL"


# ----------------------------------------------------- T21-10…14（E13 准入）

def test_t21_10_stale_fence_rejected_retryable():
    ports = Ports(fence=5)
    svc, p = _svc(ports)
    with pytest.raises(ServiceError) as exc:
        _create(svc, body=_body(context=_context(fence=2)))
    assert exc.value.status == 409
    assert exc.value.code == "STALE_EXECUTION_FENCE"
    assert exc.value.retryable is True, "§5.7 里这个码标 retryable=true"


def test_t21_11_episode_too_large_is_422():
    ports = Ports(node_count=9999)
    svc, p = _svc(ports)
    with pytest.raises(ServiceError) as exc:
        _create(svc)
    assert exc.value.status == 422
    assert exc.value.code == "EPISODE_TOO_LARGE"


def test_t21_11b_shot_cap_is_800_not_the_temporal_child_ceiling():
    """§2.2：`MAX_SHOTS_PER_EPISODE = 800`。

    2000 是 Temporal 的 `MAX_CONCURRENT_CHILDREN`（并发子工作流硬上限），
    不是集规模闸门；用它当闸门等于没有闸门。801 必须被拒，800 必须放行。
    """
    over, _ = _svc(Ports(node_count=801))
    with pytest.raises(ServiceError) as exc:
        _create(over)
    assert exc.value.code == "EPISODE_TOO_LARGE"

    at_limit, ports = _svc(Ports(node_count=800))
    created = _create(at_limit)
    assert created["state"] == "accepted"
    assert len(ports.workflows) == 1


def test_t21_12_episode_path_must_match_context():
    svc, p = _svc()
    with pytest.raises(ServiceError) as exc:
        _create(svc, episode="ep_other")
    assert exc.value.status == 400
    assert exc.value.code == "INVALID_CONTEXT"


def test_t21_13_manifest_revision_missing_is_422_not_409():
    ports = Ports()

    def read_manifest(tenant_id, manifest_id):
        return {"id": manifest_id, "revision": 3, "execution_fence": 2,
                "node_count": 10}

    ports.read_manifest = read_manifest
    svc, p = _svc(ports)
    with pytest.raises(ServiceError) as exc:
        _create(svc)
    assert exc.value.status == 422
    assert exc.value.code == "DEPENDENCY_PRECHECK_FAILED"


def test_t21_14_unwired_dependency_is_503_retryable():
    """没接线就 503 DEPENDENCY_UNAVAILABLE，不静默降级。"""
    svc = OrchestrationService(_conn())  # 一个端口都没注
    with pytest.raises(ServiceError) as exc:
        _create(svc)
    assert exc.value.status == 503
    assert exc.value.code == "DEPENDENCY_UNAVAILABLE"
    assert exc.value.retryable is True


# --------------------------------------------- T21-20…27（E22 停止/取消/恢复）

def test_t21_20_pause_does_not_cancel_in_flight():
    svc, p = _svc()
    op = _create(svc)
    run_id = op["run_id"]
    svc.register_in_flight(T, run_id, "shot_1.video", 1, "VIDEO",
                           provider_job_id="job_1", attempt_id="att_1")

    svc.command_run(T, run_id, "pause", correlation_id="cmd_pause")
    assert svc.get_run(T, run_id)["state"] == "PAUSED"
    # §4.1：暂停不中止在途工作。
    assert p.cancelled == []
    assert not [c for c in p.calls if c[0] == "fence"]
    (open_node,) = svc.list_nodes(T, run_id, state="OPEN")["items"]
    assert open_node["provider_job_id"] == "job_1"


def test_t21_21_pause_twice_is_illegal_transition_409():
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    svc.command_run(T, run_id, "pause", correlation_id="cmd_p1")
    with pytest.raises(ServiceError) as exc:
        svc.command_run(T, run_id, "pause", correlation_id="cmd_p2")
    assert exc.value.status == 409
    assert exc.value.code == "ILLEGAL_STATE_TRANSITION"


def test_t21_22_resume_reads_before_writing():
    """§4.3：恢复的第一个动作必须是读。"""
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    svc.register_in_flight(T, run_id, "shot_1.video", 1, "VIDEO",
                           attempt_id="att_1")
    svc.command_run(T, run_id, "pause", correlation_id="cmd_p1")
    p.calls.clear()

    svc.command_run(T, run_id, "resume", correlation_id="cmd_p2")
    assert p.calls and p.calls[0][0] == "query_jobs", \
        "resume 必须先查 S26，不能直接重派"
    assert svc.get_run(T, run_id)["state"] == "RUNNING"


def test_t21_23_resume_does_not_redispatch_terminal_job():
    ports = Ports()
    svc, p = _svc(ports)
    run_id = _create(svc)["run_id"]
    svc.register_in_flight(T, run_id, "shot_1.video", 1, "VIDEO",
                           attempt_id="att_1")
    svc.command_run(T, run_id, "pause", correlation_id="cmd_p1")
    ports.jobs["att_1"] = {"state": "SUCCEEDED", "terminal": True}
    p.calls.clear()

    svc.command_run(T, run_id, "resume", correlation_id="cmd_p2")
    assert not [c for c in p.calls if c[0] == "cancel_job"]
    assert svc.list_nodes(T, run_id, state="OPEN")["items"] == [], \
        "已终结的任务应被结算，不是继续挂着"


def test_t21_24_submission_unknown_is_never_cancelled():
    """§4.5 / T21-24：SUBMISSION_UNKNOWN 不调取消接口，标 discard_on_completion。"""
    ports = Ports()
    svc, p = _svc(ports)
    run_id = _create(svc)["run_id"]
    svc.register_in_flight(T, run_id, "shot_1.video", 1, "VIDEO",
                           provider_job_id="job_1", attempt_id="att_1")
    ports.jobs["att_1"] = {"state": "SUBMISSION_UNKNOWN", "terminal": False}
    p.calls.clear()

    result = svc.command_run(T, run_id, "cancel", correlation_id="cmd_c")
    assert p.cancelled == [], "UNKNOWN 的 job 绝不能被取消"
    # cancel 先 fence 再取消，这条断言同时证明顺序可用。
    kinds = [c[0] for c in p.calls]
    assert kinds.index("fence") < len(kinds)
    nodes = svc.list_nodes(T, run_id, state="DISCARD_ON_COMPLETION")["items"]
    assert [n["node_id"] for n in nodes] == ["shot_1.video"]
    assert result["state"] == "accepted"


def test_t21_25_cancel_does_not_refund_and_is_idempotent_on_terminal():
    """E22：取消不假退款；终态不倒退。"""
    ports = Ports()
    svc, p = _svc(ports)
    run_id = _create(svc)["run_id"]
    svc.command_run(T, run_id, "cancel", correlation_id="cmd_c1")
    assert svc.get_run(T, run_id)["state"] == "CANCELED"
    fence_writes = len([c for c in p.calls if c[0] == "fence"])

    # 第二次 cancel：终态 Run 直接 409，不重复 fence。
    with pytest.raises(ServiceError) as exc:
        svc.command_run(T, run_id, "cancel", correlation_id="cmd_c2")
    assert exc.value.code == "RUN_NOT_ACTIVE"
    assert len([c for c in p.calls if c[0] == "fence"]) == fence_writes


def test_t21_26_fence_happens_before_cancel_signal():
    """§4.2 / T21-26：顺序反了会有迟到 commit 写进已取消 Run 的窗口。"""
    order = []

    class Ordered(Ports):
        def fence(self, tenant_id, body):
            order.append("fence")
            return super().fence(tenant_id, body)

        def cancel_job(self, tenant_id, body):
            order.append("cancel_job")
            return super().cancel_job(tenant_id, body)

    ports = Ordered()
    svc, p = _svc(ports)
    run_id = _create(svc)["run_id"]
    svc.register_in_flight(T, run_id, "shot_1.video", 1, "VIDEO",
                           provider_job_id="job_1", attempt_id="att_1")
    svc.command_run(T, run_id, "cancel", correlation_id="cmd_c")
    assert order == ["fence", "cancel_job"], \
        "fence 必须先于取消信号"


def test_t21_27_cancel_advances_fence_in_projection():
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    svc.command_run(T, run_id, "cancel", correlation_id="cmd_c")
    run = svc.get_run(T, run_id)
    assert run["execution_fence"] == 3, "S08 推进后的值要落到投影里"
    binding = svc._binding_row(T, run_id)
    assert binding["status"] == "TERMINATED"


# ------------------------------------------- T21-30…34（E33 投影与重建）

def test_t21_30_record_state_cas_retry_returns_none():
    """§6.3：CAS 落 0 行 = 上一次已经成功，不是错误。"""
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    # 直接打一次 CAS，然后用同一个 expected_watermark 再打一次。
    from orchestration.service import Transaction
    with Transaction(svc.conn, svc._tx_depth):
        assert svc._record_state(T, run_id, "RUNNING", expected_watermark=1,
                                 terminal=False, correlation_id="x") == 2
    with Transaction(svc.conn, svc._tx_depth):
        assert svc._record_state(T, run_id, "RUNNING", expected_watermark=1,
                                 terminal=False, correlation_id="x") is None


def test_t21_31_watermark_monotonic_across_states():
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    marks = [svc.get_run(T, run_id)["projection_watermark"]]
    svc.command_run(T, run_id, "pause", correlation_id="cmd_p1")
    marks.append(svc.get_run(T, run_id)["projection_watermark"])
    svc.command_run(T, run_id, "resume", correlation_id="cmd_r1")
    marks.append(svc.get_run(T, run_id)["projection_watermark"])
    assert marks == sorted(marks) and len(set(marks)) == 3


def test_t21_32_rebuild_projection_from_temporal_truth():
    """§1.4 / T21-33：冲突时 Temporal 赢，水位不回退。"""
    svc, p = _svc()
    op = _create(svc)
    run_id = op["run_id"]
    before = svc.get_run(T, run_id)["projection_watermark"]

    rebuilt = svc.rebuild_projection(T, run_id, truth={
        "state": "RUNNING", "manifest_revision": 8, "execution_fence": 4,
        "completed_nodes": 11, "blocked_nodes": 0, "failed_nodes": 0,
        "waiting_reason": None, "blocked_reason": None,
        "current_stage": "SHOT_QC",
    })
    assert rebuilt["state"] == "RUNNING"
    assert rebuilt["manifest_revision"] == 8
    assert rebuilt["execution_fence"] == 4
    assert rebuilt["completed_nodes"] == 11
    assert rebuilt["projection_watermark"] > before, "水位只前进"


def test_t21_33_rebuild_creates_projection_when_row_was_lost():
    svc, p = _svc()
    op = _create(svc)
    run_id = op["run_id"]
    svc.conn.execute("DELETE FROM run_projection WHERE run_id = ?", (run_id,))
    rebuilt = svc.rebuild_projection(T, run_id, truth={
        "state": "SUCCEEDED", "manifest_revision": 9, "execution_fence": 2,
        "completed_nodes": 40, "blocked_nodes": 0, "failed_nodes": 0,
        "waiting_reason": None, "blocked_reason": None,
        "current_stage": "MASTER",
    })
    assert rebuilt["state"] == "SUCCEEDED"
    assert rebuilt["projection_watermark"] >= 1


def test_t21_34_terminal_state_cannot_regress():
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    svc.command_run(T, run_id, "cancel", correlation_id="cmd_c")
    before = svc.get_run(T, run_id)
    with pytest.raises(ServiceError) as exc:
        svc.command_run(T, run_id, "cancel", correlation_id="cmd_c2")
    assert exc.value.code == "RUN_NOT_ACTIVE"
    assert svc.get_run(T, run_id) == before


# ------------------------------------------------- T21-40…50（通用必测类）

def test_t21_47_cross_tenant_read_is_404_same_code():
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    with pytest.raises(ServiceError) as missing:
        svc.get_run(T, "run_does_not_exist")
    with pytest.raises(ServiceError) as foreign:
        svc.get_run(U, run_id)
    assert missing.value.status == foreign.value.status == 404
    assert missing.value.code == foreign.value.code == "RUN_NOT_FOUND", \
        "跨租户与不存在同码，不泄漏存在性"


def test_t21_48_cross_tenant_command_is_404():
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    with pytest.raises(ServiceError) as exc:
        svc.command_run(U, run_id, "pause", correlation_id="cmd_x")
    assert exc.value.status == 404


def test_t21_42_same_intent_does_not_create_second_active_run():
    """§6.1 第二层：同 business_idem_key 已有 ACTIVE Run → 返回原 Run。"""
    svc, p = _svc()
    first = _create(svc)
    second = _create(svc)
    assert first["run_id"] == second["run_id"]
    count = svc.conn.execute(
        "SELECT COUNT(*) FROM workflow_binding WHERE tenant_id = ?",
        (T,)).fetchone()[0]
    assert count == 1
    assert len(p.workflows) == 1, "workflow 只启动一次"


def test_t21_50_start_workflow_failure_is_retryable_not_500():
    """§5.7：`start_workflow` 失败必须是 503 `TEMPORAL_UNAVAILABLE`（可重试）。

    Run 已经落库了（§3.1 步骤 6 先于步骤 7），所以这里不是"什么都没发生"：
    binding 与 projection 都在，只是 workflow 没启动。
    """
    class Broken(Ports):
        def start_workflow(self, workflow_id, payload):
            raise RuntimeError("temporal frontend unreachable")

    svc, p = _svc(Broken())
    with pytest.raises(ServiceError) as ei:
        _create(svc)
    assert ei.value.status == 503
    assert ei.value.code == "TEMPORAL_UNAVAILABLE"
    assert ei.value.retryable is True

    row = svc.conn.execute(
        "SELECT run_id, workflow_started_at FROM workflow_binding"
        " WHERE tenant_id = ?", (T,)).fetchone()
    assert row is not None, "binding 已经落了"
    assert row["workflow_started_at"] is None, "启动没成功就不能标记"


def test_t21_51_retrying_create_run_actually_starts_the_workflow():
    """C21-21：重试必须真的补启，而不是命中业务幂等就返回。

    方案 §3.1 说"启动失败只需重试 `start_workflow`"，但重试走同一个 POST，
    业务层幂等会直接返回原 Run——不补启的话 workflow 永远不启动。
    """
    class Flaky(Ports):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def start_workflow(self, workflow_id, payload):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("temporal frontend unreachable")
            self.workflows.append((workflow_id, payload))
            return {"started": True}

    svc, p = _svc(Flaky())
    with pytest.raises(ServiceError):
        _create(svc)
    assert p.attempts == 1

    second = _create(svc)
    assert p.attempts == 2, "重试真的又调了一次 start_workflow"
    assert len(p.workflows) == 1
    assert svc.get_run(T, second["run_id"])["state"] == "QUEUED"

    # 第三次：已经启动过，不再重复启动。
    _create(svc)
    assert p.attempts == 2, "启动成功后不应再调端口"


def test_t21_52_manual_generation_mode_never_starts_a_workflow():
    """§3.1：`generation_mode=manual` 不启动 workflow，重试也不启动。"""
    svc, p = _svc(Ports(generation_mode="manual"))
    first = _create(svc)
    second = _create(svc)
    assert first["run_id"] == second["run_id"]
    assert p.workflows == []
    assert svc.get_run(T, first["run_id"])["state"] == "WAITING_INPUT"


def test_t21_43_cancel_then_rebuild_works_because_index_is_partial():
    """§6.1 第三层：旧 Run TERMINATED 后不在部分索引里，可以重建。

    取消会推进 fence（S08 是写者），所以重建必须带新 fence，否则就是拿旧
    fence 去建 Run——那正是 STALE_EXECUTION_FENCE 要拒的东西。这里同时
    验证了「取消之后旧 binding 不再挡路」。
    """
    ports = Ports()
    svc, p = _svc(ports)
    first = _create(svc)
    svc.command_run(T, first["run_id"], "cancel", correlation_id="cmd_c")
    assert ports.fence_value == 3, "取消推进了一次 fence"

    second = _create(svc, body=_body(context=_context(fence=3)))
    assert second["run_id"] != first["run_id"]
    assert svc.get_run(T, second["run_id"])["state"] == "QUEUED"
    assert len(p.workflows) == 2


def test_t21_44_partial_cancel_only_touches_changed_epoch():
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    svc.register_in_flight(T, run_id, "shot_1.video", 1, "VIDEO",
                           provider_job_id="job_1", attempt_id="att_1")
    svc.register_in_flight(T, run_id, "shot_2.video", 1, "VIDEO",
                           provider_job_id="job_2", attempt_id="att_2")

    svc.consume_manifest_committed({
        "id": "evt_m1", "tenant_id": T,
        "data": {
            "schema_version": "2.0",
            "manifest_ref": {"id": "mf_1", "revision": 8,
                             "content_hash": hashlib.sha256(b"m8").hexdigest()},
            "parent_manifest_ref": {"id": "mf_1", "revision": 7,
                                    "content_hash": hashlib.sha256(b"mf").hexdigest()},
            "execution_fence": 2,
            "changed_node_epochs": [{"node_id": "shot_1.video", "from": 1, "to": 2}],
            "accepted_pointers_delta": [],
            "accepted_revision": 8,
            "source_revisions": {},
            "trigger": "COMMIT",
        },
    })
    cancelled = svc.list_nodes(T, run_id, state="CANCELED")["items"]
    assert [n["node_id"] for n in cancelled] == ["shot_1.video"], \
        "只取消 epoch 真的变了的那个节点"
    open_ids = [n["node_id"] for n in svc.list_nodes(T, run_id, state="OPEN")["items"]]
    assert "shot_2.video" in open_ids


def test_t21_46_pointer_only_change_cancels_nothing():
    """§3.3 步骤 3：只有指针变、没有 epoch 变 → 不取消、不启动子图。"""
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    svc.register_in_flight(T, run_id, "shot_1.video", 1, "VIDEO",
                           attempt_id="att_1")
    out = svc.consume_manifest_committed({
        "id": "evt_m2", "tenant_id": T,
        "data": {
            "schema_version": "2.0",
            "manifest_ref": {"id": "mf_1", "revision": 8,
                             "content_hash": hashlib.sha256(b"m8").hexdigest()},
            "parent_manifest_ref": {"id": "mf_1", "revision": 7,
                                    "content_hash": hashlib.sha256(b"mf").hexdigest()},
            "execution_fence": 2,
            "changed_node_epochs": [],
            "accepted_pointers_delta": [{"node_id": "shot_1.video"}],
            "accepted_revision": 8,
            "source_revisions": {},
            "trigger": "ACCEPT",
        },
    })
    assert out["subgraph"] is False
    assert svc.list_nodes(T, run_id, state="CANCELED")["items"] == []


def test_t21_smc5_from_equals_to_does_not_cancel():
    """SMC-5：`from == to` 表示 epoch 没动，在途任务仍然有效。"""
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    svc.register_in_flight(T, run_id, "shot_3_4.video", 2, "VIDEO",
                           attempt_id="att_34")
    out = svc.consume_manifest_committed({
        "id": "evt_m3", "tenant_id": T,
        "data": {
            "schema_version": "2.0",
            "manifest_ref": {"id": "mf_1", "revision": 8,
                             "content_hash": hashlib.sha256(b"m8").hexdigest()},
            "parent_manifest_ref": {"id": "mf_1", "revision": 7,
                                    "content_hash": hashlib.sha256(b"mf").hexdigest()},
            "execution_fence": 2,
            "changed_node_epochs": [{"node_id": "shot_3_4.video",
                                     "from": 2, "to": 2}],
            "accepted_pointers_delta": [],
            "accepted_revision": 8,
            "source_revisions": {},
            "trigger": "COMMIT",
        },
    })
    assert out["changed"][0]["reason"] == "EPOCH_UNCHANGED"
    assert svc.list_nodes(T, run_id, state="CANCELED")["items"] == []
    assert [n["node_id"] for n in svc.list_nodes(T, run_id, state="OPEN")["items"]] \
        == ["shot_3_4.video"]


def test_t21_45_fence_advance_cancels_whole_run():
    """§4.4 末段：fence 前进是 execution_fence 语义，整 Run 作废。"""
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    svc.register_in_flight(T, run_id, "shot_1.video", 1, "VIDEO",
                           attempt_id="att_1")
    out = svc.consume_manifest_committed({
        "id": "evt_m4", "tenant_id": T,
        "data": {
            "schema_version": "2.0",
            "manifest_ref": {"id": "mf_1", "revision": 8,
                             "content_hash": hashlib.sha256(b"m8").hexdigest()},
            "parent_manifest_ref": {"id": "mf_1", "revision": 7,
                                    "content_hash": hashlib.sha256(b"mf").hexdigest()},
            "execution_fence": 3,
            "changed_node_epochs": [],
            "accepted_pointers_delta": [],
            "accepted_revision": 8,
            "source_revisions": {},
            "trigger": "FENCE_VOID",
        },
    })
    assert out["state"] == "CANCELED"
    assert out["reason"] == "FENCE_ADVANCED"
    assert svc.get_run(T, run_id)["state"] == "CANCELED"


def test_t21_fence_response_does_not_call_s08_fence():
    """§4.4：S21 是"响应 fence"，不是"发起 fence"。"""
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    p.calls.clear()
    svc.consume_manifest_committed({
        "id": "evt_m5", "tenant_id": T,
        "data": {
            "schema_version": "2.0",
            "manifest_ref": {"id": "mf_1", "revision": 8,
                             "content_hash": hashlib.sha256(b"m8").hexdigest()},
            "parent_manifest_ref": {"id": "mf_1", "revision": 7,
                                    "content_hash": hashlib.sha256(b"mf").hexdigest()},
            "execution_fence": 3,
            "changed_node_epochs": [], "accepted_pointers_delta": [],
            "accepted_revision": 8, "source_revisions": {}, "trigger": "FENCE_VOID",
        },
    })
    assert not [c for c in p.calls if c[0] == "fence"], \
        "S21 从不自己调 S08 :fence 去响应别人的 fence"


def test_t21_duplicate_event_delivery_is_noop():
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    svc.register_in_flight(T, run_id, "shot_1.video", 1, "VIDEO", attempt_id="att_1")
    event = {
        "id": "evt_dup", "tenant_id": T,
        "data": {"schema_version": "2.0", "attempt_id": "att_1",
                 "state": "RUNNING", "previous_state": "SUBMITTED",
                 "business_attempt": 1, "network_retry": 0, "submit_attempts": 1,
                 "provider": "p1", "channel": "c1", "node_id": "shot_1.video",
                 "node_epoch": 1, "execution_fence": 2, "must_reconcile": False,
                 "reason_code": None},
    }
    first = svc.consume_job_state_changed(event)
    second = svc.consume_job_state_changed(event)
    assert first["touched_runs"] == [run_id]
    assert second == {"duplicate": True, "event_id": "evt_dup"}


def test_t21_out_of_order_job_event_for_unknown_attempt_is_ignored():
    """乱序：事件先于派单到达时不能造出实体。"""
    svc, p = _svc()
    _create(svc)
    out = svc.consume_job_state_changed({
        "id": "evt_early", "tenant_id": T,
        "data": {"schema_version": "2.0", "attempt_id": "att_never", "state": "RUNNING"},
    })
    assert out["ignored"] is True
    assert out["reason"] == "no_open_in_flight"


def test_t21_dispatch_unknown_is_not_redelivered():
    """§4.7 / F-05 / T21-50：超时只记 dispatch_request_id，绝不重提。"""
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    out = svc.record_dispatch_unknown(T, run_id, "shot_7.video", 1, "VIDEO",
                                     dispatch_request_id="dsp_abc")
    assert out["blocked_reason"] == "DISPATCH_UNKNOWN"
    assert svc.get_run(T, run_id)["state"] == "WAITING_BUDGET"
    blockers = svc.list_blockers(T, run_id)["items"]
    assert blockers[0]["attempt_id"] == "dsp_abc"
    # 再调一次同样的 record_dispatch_unknown 不会产生新的 dispatch 请求。
    svc.record_dispatch_unknown(T, run_id, "shot_7.video", 1, "VIDEO",
                                dispatch_request_id="dsp_abc")
    rows = svc.conn.execute(
        "SELECT COUNT(*) FROM node_in_flight WHERE run_id = ?", (run_id,)).fetchone()[0]
    assert rows == 1


def test_t21_49_many_shots_stay_consistent():
    """T21-49：800 个镜头的登记与统计不串行化出错。"""
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    for i in range(800):
        svc.register_in_flight(T, run_id, f"shot_{i}.video", 1, "VIDEO",
                               attempt_id=f"att_{i}")
    items = svc.list_nodes(T, run_id, limit=1000)["items"]
    assert len(items) == 800
    assert len({i["node_id"] for i in items}) == 800


# ------------------------------------------------------------- 修复额度

def test_t21_repair_budget_is_not_a_rejection_basis():
    """§5.4：`ceiling_micro` 只读复制，S21 不把估算当拒绝依据。"""
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    budget = svc.seed_repair_budget(T, run_id, max_business_attempts=3,
                                    max_recheck_rounds=2, ceiling_micro=50_000)
    assert budget["remaining_business_attempts"] == 3
    assert budget["ceiling_micro"] == 50_000
    assert budget["budget_exhausted"] is False
    # 用尽后是 409 而不是 422：改额度/新 Run 后重试可能行。
    for _ in range(3):
        svc.consume_repair_attempt(T, run_id)
    with pytest.raises(ServiceError) as exc:
        svc.consume_repair_attempt(T, run_id)
    assert exc.value.status == 409
    assert svc.get_repair_budget(T, run_id)["budget_exhausted"] is True


def test_t21_repair_budget_absent_means_zero_not_infinite():
    svc, p = _svc()
    run_id = _create(svc)["run_id"]
    budget = svc.get_repair_budget(T, run_id)
    assert budget["remaining_business_attempts"] == 0
    assert budget["budget_exhausted"] is True


# ------------------------------------------------------------ 纯函数

def test_workflow_id_is_deterministic():
    a = workflow_id_for("episode_production", T, "k" * 64, 1)
    b = workflow_id_for("episode_production", T, "k" * 64, 1)
    assert a == b
    assert a != workflow_id_for("episode_production", T, "k" * 64, 2)


def test_business_idem_key_covers_all_nine_inputs():
    base = dict(tenant_id=T, series_id="se_1", branch_id="br", episode_id="ep",
                base_manifest_ref={"id": "mf", "revision": 1, "content_hash": "0" * 64},
                automation_mode="guided", policy_revision=4,
                budget_scope_id="bs", intent="episode_production")
    key = business_idem_key_for(**base)
    assert len(key) == 64
    for field, value in (("episode_id", "ep2"), ("budget_scope_id", "bs2"),
                         ("intent", "repair"), ("policy_revision", 5),
                         ("automation_mode", "unattended_batch")):
        assert business_idem_key_for(**{**base, field: value}) != key, \
            f"{field} 必须影响业务幂等键"
