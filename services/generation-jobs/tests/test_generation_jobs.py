"""S26 generation-jobs 测试。编号对应 `plans/S26-generation-jobs.md` §15（T26-01…T26-40）。

覆盖 docs/16 要求的 E12/E14–E19/E21/E23 在 S26 边界上的那一半：跨租户拒绝、
重复投递幂等、乱序/缺失事件、版本冲突、撤销后失效。

## 环境

被测服务跑在内存 SQLite（`isolation_level=None` + 显式 `BEGIN IMMEDIATE`），
依赖一律是注入的假端口 —— 按 AGENTS.md「依赖服务未实现 → 用 mock 按契约走」。

## 与方案的偏差（逐条登记，测试只在能证明的那一侧断言）

- **T26-40**：方案要求「真实 Postgres + mock S22/S23/S27」。本仓库的服务实现全部
  是 SQLite（同 S21/S22/S23 的规则），没有 Postgres 依赖。本文件用内存 SQLite，
  并断言 `sqlite3` 连接本身（见 `test_t26_40_...`）。
- **T26-16**：伪签名回调的 401 是 S27 的职责；S26 能证明的是**跨租户事件进不来**
  （`/internal/events` 的 `tenant_id` 与头部不一致 → 403），以及没进来时状态不变。
- **T26-23/24/37**：Redis / S22 重启 / S27 重试都不在 S26 进程里。这里断言 S26
  自己的那一半：无进程内状态（换一个 service 实例读同一库，结论不变）、
  UNKNOWN 不丢、失败 claim 不产生出站事件。
- **T26-25**：`INGESTING` 期间收到新的 `succeeded` 观测会被 `PROGRESS_RANK` 判为
  `OUT_OF_ORDER`（`PROVIDER_SUCCEEDED`=7 < `INGESTING`=8），只推进观测水印，
  **不合并新 URL**。所以「渠道换发新 URL」这条补救路径在本实现里走不通，
  只能靠 `start_ingest(rearchive=True)` 用已有 URL 重归档。测试断言后者。
- **§7.4 重提复查**：`_resubmit` 从 `RECONCILING` 回 `ADMITTED` 后走
  `_saga_recheck_and_issue`，所以 §4 步骤 4 的三条实时复查（S23 仍 LIVE /
  authorize / S22 confirm_dispatch / S24 能力）会**再跑一遍**；`_resubmit_gate`
  只管 §7.4 的五条。T26-11 断言的是「同键 + 新 grant」，不是「跳步」。
- **T26-37**：方案说「S27 claim 失败重试，不发 HTTP」。失败 claim 本身在 S27
  进程里，S26 看不到；S26 这一侧能证明的是「job 不在 `SUBMITTING` 时的回执
  一律 `applied: false`，且不产生出站事件」。
- **T26-39**：方案要求「状态 `RUNNING` + `discard_on_completion`」。实现里
  `not_cancellable` 只写 `disposition`、**不改状态**（`CANCEL_REQUESTED` 是
  `last_error_code` 而不是状态），所以测试先把 job 推到 `RUNNING` 再取消。
- **`get_job` docstring** 写「14 个字段」，实际 15 个（多 `frozen`）。断言按代码。
- **`record_submit_outcome` 的 `accepted` 无 ref** 按 §7.1 归入 UNKNOWN（S27 自己
  也是这么归类的），所以这里不测「accepted 但缺 ref 应视为 QUEUED」。
- **T26-20b/20c**：步骤 4 第 3 条复查的反面 ——「端口没接线」与「端口超时」都
  必须停在 `ADMITTED`、不签 grant。此前的 `_check_capability` 在端口缺失时
  `return True`，等于跳过 S24 复查，已改为 `_require` 抛 503（与超时同路）。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from generation_jobs.schema import SCHEMA
from generation_jobs.service import (
    GenerationJobsService,
    ServiceError,
    _parse_ts,
    _ts,
    admission_id_for,
    intent_id_for,
    requirement_hash_for,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT.parents[1] / "contracts"

T = "tenant_a"
U = "tenant_b"
FAR = "2099-01-01T00:00:00.000000Z"
ACTOR = {"principal_id": "api", "principal_type": "service"}


# ------------------------------------------------------------------ 基础夹具

class Clock:
    """可变时钟。S26 的所有时间判断都走注入的 `clock`。"""

    def __init__(self, start: str = "2026-11-12T00:00:00Z"):
        self.t = _parse_ts(start)

    def __call__(self) -> str:
        return _ts(self.t)

    def advance(self, seconds: float) -> "Clock":
        self.t = self.t + timedelta(seconds=seconds)
        return self


def _conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def _command(*, attempt_id="attempt_1", node_id="shot_3_2.video",
             idempotency_key="ep3:node4:video:attempt1", deadline=FAR,
             tenant=T, input_obj=None, **over):
    command = {
        "schema_version": "2.0",
        "context": {
            "schema_version": "2.0",
            "tenant_id": tenant,
            "project_id": "project_demo",
            "series_id": "series_demo",
            "branch_id": "branch_main",
            "episode_id": "ep3",
            "home_region": "region_demo",
            "home_cell": "cell_a1",
            "manifest_ref": {"id": "manifest_ep3", "revision": 12,
                             "content_hash": "b" * 64},
            "node_id": node_id,
            "node_epoch": 4,
            "execution_fence": 2,
            "memory_checkpoint": {"branch_id": "branch_main",
                                  "commit_sequence": 3, "content_hash": "c" * 64},
            "policy_ref": {"service_id": "S07", "id": "policy_1",
                           "revision": 1, "content_hash": "d" * 64},
            "actor": {"principal_id": "user_demo", "principal_type": "human",
                      "delegation_id": "del_1"},
            "command_id": "cmd_video_demo",
            "request_hash": "d" * 64,
            "trace_id": "trace_demo",
        },
        "attempt_id": attempt_id,
        "variation_nonce": "variation_0",
        "idempotency_key": idempotency_key,
        "modality": "video",
        "input": input_obj if input_obj is not None else {"duration_seconds": 4},
        "route_policy_ref": {"service_id": "S25", "id": "rp_1", "revision": 1,
                             "content_hash": "f" * 64},
        "template_ref": {"service_id": "S12", "id": "tpl_1", "revision": 2,
                         "content_hash": "a" * 64},
        "budget_scope_id": "budget_ep3",
        "deadline": deadline,
    }
    command.update(over)
    return command


def _route(*, attempt_id="attempt_1", node_id="shot_3_2.video",
           quote_valid_to=FAR, normalized=None, feasible=True,
           quote_upper_bound_micro=1000, **over):
    route = {
        "feasible": feasible,
        "selected": {
            "candidate_key": "p1/c1/m1@rev1#cn#pool1",
            "normalized_input": (normalized if normalized is not None
                                 else {"duration_seconds": 5}),
            "price_snapshot_id": "ps_1",
            "quote_upper_bound_micro": quote_upper_bound_micro,
            "expected_cost_micro": 800,
            "currency": "USD",
            "quote_valid_to": quote_valid_to,
            "snapshot_id": "snap_1",
            "combination_key": "combo_1",
        },
        "quote_valid_to": quote_valid_to,
        "content_hash": "e" * 64,
        "revision": 3,
        "capability_bundle_revision": 2,
        "policy_snapshot_id": "policy_snap_1",
        "attempt_id": attempt_id,
        "node_id": node_id,
        "node_epoch": 4,
        "execution_fence": 2,
    }
    route.update(over)
    return route


# ------------------------------------------------------------------ 假端口

PORT_NAMES = (
    "read_route_decision", "read_channel_profile", "read_reservation",
    "create_artifact_import", "check_capability_validity", "reconcile_probe",
    "bill_covers_window", "request_provider_cancel", "isolate_account_pool",
    "report_capability_drift", "reserve_budget", "authorize_budget",
    "release_budget", "settle_budget", "acquire_permit", "confirm_dispatch",
    "release_permit",
)

HOST_PORTS = frozenset({
    "reserve_budget", "authorize_budget", "release_budget", "settle_budget",
    "acquire_permit", "confirm_dispatch", "release_permit",
})


class Ports:
    """S26 的十七个端口。全部记录调用；行为由属性控制。

    七个 host 端口（S23/S22 的写方法）多收 `actor=`/`correlation_id=`，
    其余只吃位置参数 —— 与 `GenerationJobsService._host` 的分派一致。
    """

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.route = _route()
        self.profile = {"provider_idempotency": "unsupported"}
        self.reservation_live = {"state": "AUTHORIZED", "amount_micro": 1000,
                                 "amount_conservative_micro": 1000}
        self.reservation_by_scope = None
        self.reservation_error = None
        self.reserve_error = None
        self.acquire_error = None
        self.authorize_error = None
        self.confirm_error = None
        self.capability = {"items": [{"usable": True}]}
        self.capability_error = None
        self.recon = {"outcome": "not_found"}
        self.recon_error = None
        self.bill_covers = True
        self.cancel = {"result": "cancelled"}
        self.import_result = None
        self.imports = 0
        self.settlement = {"settlement_id": "set_1"}

    # ---- 日志 ----

    def _log(self, name, args, kwargs=None):
        self.calls.append((name, args, kwargs or {}))

    def count(self, name) -> int:
        return sum(1 for c in self.calls if c[0] == name)

    def bodies(self, name) -> list:
        return [c[1][-1] for c in self.calls if c[0] == name]

    def as_ports(self) -> dict:
        return {name: getattr(self, name) for name in PORT_NAMES}

    # ---- S25 ----

    def read_route_decision(self, tenant_id, body):
        self._log("read_route_decision", (tenant_id, body))
        return dict(self.route)

    def read_channel_profile(self, tenant_id, body):
        self._log("read_channel_profile", (tenant_id, body))
        return dict(self.profile)

    # ---- S23 / S22 写 ----

    def reserve_budget(self, tenant_id, body, *, actor=None, correlation_id=""):
        self._log("reserve_budget", (tenant_id, body),
                  {"actor": actor, "correlation_id": correlation_id})
        if self.reserve_error:
            raise self.reserve_error
        return {"reservation_id": "res_1", "amount_micro": body["amount_micro"],
                "amount_conservative_micro": body["amount_micro"],
                "currency": body["currency"], "state": "RESERVED"}

    def authorize_budget(self, tenant_id, reservation_id, body, *, actor=None,
                         correlation_id=""):
        self._log("authorize_budget", (tenant_id, reservation_id, body))
        if self.authorize_error:
            raise self.authorize_error
        return {"state": "AUTHORIZED"}

    def release_budget(self, tenant_id, reservation_id, body, *, actor=None,
                       correlation_id=""):
        self._log("release_budget", (tenant_id, reservation_id, body))
        return {"state": "RELEASED"}

    def settle_budget(self, tenant_id, body, *, actor=None, correlation_id=""):
        self._log("settle_budget", (tenant_id, body))
        return dict(self.settlement)

    def acquire_permit(self, tenant_id, body, *, actor=None, correlation_id=""):
        self._log("acquire_permit", (tenant_id, body),
                  {"actor": actor, "correlation_id": correlation_id})
        if self.acquire_error:
            raise self.acquire_error
        return {"permit_id": "perm_1", "pool_id": body["requested_pool_id"],
                "state": "RESERVED"}

    def confirm_dispatch(self, tenant_id, permit_id, body, *, actor=None,
                         correlation_id=""):
        self._log("confirm_dispatch", (tenant_id, permit_id, body))
        if self.confirm_error:
            raise self.confirm_error
        return {"state": "DISPATCH_AUTHORIZED"}

    def release_permit(self, tenant_id, permit_id, body, *, actor=None,
                       correlation_id=""):
        self._log("release_permit", (tenant_id, permit_id, body))
        return {"state": "RELEASED"}

    # ---- 只吃位置参数的端口 ----

    def read_reservation(self, tenant_id, arg):
        """三种调用形状：`{"idempotency_scope",…}` / 裸 id / `{"id":…}`。"""
        self._log("read_reservation", (tenant_id, arg))
        if self.reservation_error:
            raise self.reservation_error
        if isinstance(arg, dict) and "idempotency_scope" in arg:
            return self.reservation_by_scope
        return dict(self.reservation_live)

    def create_artifact_import(self, tenant_id, body):
        self._log("create_artifact_import", (tenant_id, body))
        self.imports += 1
        if self.import_result is not None:
            return dict(self.import_result)
        return {"import_id": f"imp_{self.imports}", "state": "RECEIVED"}

    def check_capability_validity(self, tenant_id, body):
        self._log("check_capability_validity", (tenant_id, body))
        if self.capability_error:
            raise self.capability_error
        return dict(self.capability)

    def reconcile_probe(self, tenant_id, body):
        self._log("reconcile_probe", (tenant_id, body))
        if self.recon_error:
            raise self.recon_error
        return dict(self.recon)

    def bill_covers_window(self, tenant_id, body):
        self._log("bill_covers_window", (tenant_id, body))
        return self.bill_covers

    def request_provider_cancel(self, tenant_id, body):
        self._log("request_provider_cancel", (tenant_id, body))
        return dict(self.cancel)

    def isolate_account_pool(self, tenant_id, body):
        self._log("isolate_account_pool", (tenant_id, body))
        return {"ok": True}

    def report_capability_drift(self, tenant_id, body):
        self._log("report_capability_drift", (tenant_id, body))
        return {"ok": True}


def _svc(*, conn=None, clock=None, ports=None, **kwargs):
    return GenerationJobsService(conn or _conn(), clock=clock or Clock(),
                                 **(ports or Ports()).as_ports(), **kwargs)


# ------------------------------------------------------------------ 流程助手

def _create(svc, *, command=None, route_id="route_1", tenant=T):
    return svc.create_job(
        tenant,
        {"route_decision_id": route_id,
         "generation_command": command or _command(tenant=tenant)},
        actor=ACTOR, correlation_id="create")


def _admitted(svc, ports, *, command=None):
    out = _create(svc, command=command)
    job = svc.advance(T, out["provider_job_id"], correlation_id="adv")
    assert job["state"] == "DISPATCH_AUTHORIZED", job
    return out["provider_job_id"]


def _intent(svc, job_id, tenant=T):
    return svc.conn.execute(
        "SELECT * FROM submission_intent WHERE tenant_id = ? AND provider_job_id = ?",
        (tenant, job_id)).fetchone()


def _grant(svc, dispatch_id, tenant=T):
    return svc.conn.execute(
        "SELECT * FROM dispatch_grant WHERE tenant_id = ? AND dispatch_id = ?",
        (tenant, dispatch_id)).fetchone()


def _claim(svc, job_id, *, executor="S27", fencing_token=None, tenant=T):
    dispatch_id = _intent(svc, job_id, tenant)["dispatch_id"]
    token = (fencing_token if fencing_token is not None
             else _grant(svc, dispatch_id)["fencing_token"])
    return svc.claim(tenant, dispatch_id,
                     {"executor_id": executor, "fencing_token": token},
                     correlation_id="claim")


def _submitted(svc, ports, *, command=None):
    job_id = _admitted(svc, ports, command=command)
    _claim(svc, job_id)
    return job_id


def _grant_count(svc, tenant=T) -> int:
    return svc.conn.execute(
        "SELECT COUNT(*) FROM dispatch_grant WHERE tenant_id = ?",
        (tenant,)).fetchone()[0]


def _submit_events(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE event_type = 'connector.submit.requested'"
    ).fetchone()[0]


def _state(svc, job_id, tenant=T) -> str:
    return svc.get_job(tenant, job_id)["state"]


def _row(svc, job_id, tenant=T):
    return svc._job(tenant, job_id)


def _output(*, url="https://cdn.example/out/abc123.mp4",
            expires="2026-11-12T01:00:00Z", sha="3" * 64):
    """`contracts/examples/connector-observation-received.json` 的 manifest 形状。"""
    return {"manifest": [{"role": "video", "url": url, "url_expires_at": expires,
                          "sha256": sha, "bytes": 12345678,
                          "content_type": "video/mp4"}]}


def _obs(*, job_id=None, status="queued", ref=None, key=None, dispatch=None,
         event_id="evt_1", tenant=T, fence=None, epoch=None, output=None,
         raw=None, observed_at=None, error_class=None, retry_after=None,
         correlation_id="obs"):
    data = {"schema_version": "2.0", "status": status, "source": "poll",
            "observed_at": observed_at or "2026-11-12T00:00:05.000000Z"}
    if raw is not None:
        data["provider_status_raw"] = raw
    if ref:
        data["provider_job_ref"] = ref
    if key:
        data["stable_client_key"] = key
    if dispatch:
        data["dispatch_id"] = dispatch
    if fence is not None:
        data["execution_fence"] = fence
    if epoch is not None:
        data["node_epoch"] = epoch
    if output is not None:
        data["output"] = output
    if error_class:
        data["error_class"] = error_class
    if retry_after is not None:
        data["retry_after_seconds"] = retry_after
    envelope = {"id": event_id, "tenant_id": tenant, "data": data,
                "correlation_id": correlation_id}
    if job_id:
        envelope["aggregate_id"] = job_id
    if dispatch:
        envelope["data"]["dispatch_id"] = dispatch
    return envelope


def _unknown(svc, job_id):
    """claim 之后丢响应：S26 进 UNKNOWN 的唯一合法来源。"""
    _claim(svc, job_id)
    out = svc.record_submit_outcome(
        T, _intent(svc, job_id)["dispatch_id"], "unknown",
        correlation_id="unk")
    assert out["state"] == "SUBMISSION_UNKNOWN", out
    return job_id


# ------------------------------------------------------------------ HTTP 夹具


def _http_client():
    """HTTP 层的连接必须是 `check_same_thread=False` 的命名共享内存库。

    `TestClient` 在另一个线程里跑同步端点，`:memory:` 的默认连接会抛
    `sqlite3.ProgrammingError`。命名 + `cache=shared` 让两个连接看到同一个库。
    """
    # 直接把这一条连接交给 app：`create_app` 自己 connect 时不会带 `uri=True`，
    # `file:...?mode=memory` 那样传会被当成字面文件名，测试与应用就各看一个库了。
    keep = sqlite3.connect(":memory:", check_same_thread=False,
                           isolation_level=None)
    keep.row_factory = sqlite3.Row
    keep.executescript(SCHEMA)
    ports = Ports()
    spec = importlib.util.spec_from_file_location(
        "s26_api_app", ROOT / "api" / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["s26_api_app"] = module
    spec.loader.exec_module(module)
    app = module.create_app(conn=keep, **ports.as_ports())
    assert app is not None, "create_app must return the FastAPI instance"
    return TestClient(app), ports, keep


def _hdr(*, tenant=T, key="idem_1", principal=None):
    h = {"X-Tenant-Id": tenant, "Idempotency-Key": key}
    if principal:
        h["X-Principal"] = principal
    return h


# ------------------------------------------------------------------ 建单与准入

def test_t26_01_same_key_same_body_replays():
    """T26-01：同幂等键 + 同 body → 同一个 job，S23 只被调一次。"""
    ports = Ports()
    svc = _svc(ports=ports)
    first = _create(svc)
    second = _create(svc)
    assert first["provider_job_id"] == second["provider_job_id"]
    assert second["replayed"] is True
    assert ports.count("reserve_budget") == 0   # 还没 advance
    svc.advance(T, first["provider_job_id"])
    assert ports.count("reserve_budget") == 1


def test_t26_02_http_idempotency_key_reused_with_other_body():
    """T26-02：同 Idempotency-Key、不同 body → 409 `IDEMPOTENCY_KEY_REUSED`。"""
    client, _ports, _keep = _http_client()
    ok = client.post("/generation-jobs", headers=_hdr(),
                     json={"route_decision_id": "route_1",
                           "generation_command": _command()})
    assert ok.status_code == 202
    assert sorted(ok.json()) == ["operation_id", "state"]
    bad = client.post("/generation-jobs", headers=_hdr(),
                      json={"route_decision_id": "route_1",
                            "generation_command": _command(
                                idempotency_key="another_key")})
    assert bad.status_code == 409
    assert bad.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_t26_03_quota_rejected_releases_budget():
    """T26-03：S22 明确拒绝 → 先释放预算、再 `REJECTED_QUOTA`、不派单。"""
    ports = Ports()
    ports.acquire_error = ServiceError(409, "NO_CAPACITY", "no permit left")
    svc = _svc(ports=ports)
    job = svc.advance(T, _create(svc)["provider_job_id"])
    assert job["state"] == "REJECTED_QUOTA"
    assert ports.count("release_budget") == 1
    assert ports.count("release_permit") == 0
    assert _submit_events(svc.conn) == 0


def test_t26_04_quota_unavailable_stays_reserving():
    """T26-04：S22 超时（retryable）→ 停在 `RESERVING`，`saga_attempts` +1，不派单。"""
    ports = Ports()
    ports.acquire_error = ServiceError(503, "QUOTA_UNAVAILABLE", "timeout",
                                       retryable=True)
    svc = _svc(ports=ports)
    job = svc.advance(T, _create(svc)["provider_job_id"])
    assert job["state"] == "RESERVING"
    assert _row(svc, job["provider_job_id"])["saga_attempts"] == 1
    assert _submit_events(svc.conn) == 0


def test_t26_05_reserve_replays_under_intent_scope():
    """T26-05：预留的幂等键落在 intent 域上，重入不会双预留。

    `_saga_dispatch` 有 stash 时直接进步骤 4，**不会**发 dict 形
    `read_reservation`（只有"ADMITTED 但没 stash"的恢复窗口才发）。所以这里
    断言的是真正的去重依据：`reserve_budget` 的 `idempotency_scope`。
    """
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _create(svc)["provider_job_id"]
    svc.advance(T, job_id)
    assert _state(svc, job_id) == "DISPATCH_AUTHORIZED"
    bodies = ports.bodies("reserve_budget")
    assert len(bodies) == 1
    assert bodies[0]["idempotency_scope"] == intent_id_for(T, job_id)
    assert bodies[0]["command_id"] == f"{intent_id_for(T, job_id)}:reserve"
    assert bodies[0]["provider_job_ref"] == job_id


# ------------------------------------------------------------------ UNKNOWN 与对账

def test_t26_06_unknown_keeps_reservation_and_permit():
    """T26-06：`unknown` → `SUBMISSION_UNKNOWN`；预算/permit/grant 全部保留。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _unknown(svc, _admitted(svc, ports))
    before = ports.count("read_reservation")
    adm = svc.get_admission(T, admission_id_for(intent_id_for(T, job_id)))
    assert adm["state"] == "SUBMISSION_UNKNOWN"
    assert adm["reservation"]["state"] == "AUTHORIZED"
    assert adm["quota_permit"]["state"] == "INFLIGHT"
    assert adm["dispatch"]["grant_state"] == "CLAIMED"
    assert adm["must_reconcile_before_resubmit"] is True
    # 冻结态不许去问 S23 现状（`_reservation_state_echo` 硬编码 AUTHORIZED）
    assert ports.count("read_reservation") == before
    assert _submit_events(svc.conn) == 1


def test_t26_07_same_key_after_unknown_replays():
    """T26-07：UNKNOWN 之后同键重建 → 原 job，不重走路由、不重新付费。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _unknown(svc, _admitted(svc, ports))
    routes_before = ports.count("read_route_decision")
    again = _create(svc)
    assert again["provider_job_id"] == job_id
    assert again["state"] == "SUBMISSION_UNKNOWN"
    assert again["replayed"] is True
    assert ports.count("read_route_decision") == routes_before


def test_t26_08_unknown_cannot_be_claimed_again():
    """T26-08：冻结态的 grant 不能再被 claim（没有第二笔付费）。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _unknown(svc, _admitted(svc, ports))
    dispatch_id = _intent(svc, job_id)["dispatch_id"]
    token = _grant(svc, dispatch_id)["fencing_token"]
    with pytest.raises(ServiceError) as exc:
        svc.claim(T, dispatch_id,
                  {"executor_id": "S27", "fencing_token": token})
    assert exc.value.status == 409
    assert exc.value.code in ("GATE_ALREADY_CLAIMED", "ILLEGAL_TRANSITION")
    assert _submit_events(svc.conn) == 1


def test_t26_09_recon_found_backfills_ref():
    """T26-09：对账 `found` → `QUEUED`，`provider_job_ref` 补齐，预算仍持有。"""
    ports = Ports()
    ports.profile = {"provider_idempotency": "supported"}
    ports.recon = {"outcome": "found", "provider_job_ref": "pref_found"}
    svc = _svc(ports=ports)
    job_id = _unknown(svc, _admitted(svc, ports))
    out = svc.reconcile_due(T, job_id)
    assert out["outcome"] == "found"
    assert _state(svc, job_id) == "QUEUED"
    assert _row(svc, job_id)["provider_job_ref"] == "pref_found"
    assert _submit_events(svc.conn) == 1


def test_t26_10_recon_found_then_success_ingests():
    """T26-10：`found` 之后成功观测 → 进归档（`INGESTING`），不重生成。"""
    ports = Ports()
    ports.profile = {"provider_idempotency": "supported"}
    ports.recon = {"outcome": "found", "provider_job_ref": "pref_found"}
    svc = _svc(ports=ports)
    job_id = _unknown(svc, _admitted(svc, ports))
    svc.reconcile_due(T, job_id)
    result = svc.consume_observation(
        T, _obs(job_id=job_id, status="succeeded", output=_output()))
    assert result["applied"] is True
    assert _state(svc, job_id) == "INGESTING"
    assert ports.count("create_artifact_import") == 1
    assert _submit_events(svc.conn) == 1


def test_t26_11_supported_resubmits_with_same_key():
    """T26-11：A 档 `not_found` 过 2×p99 → 同键重发，`submit_attempts` 到 2。"""
    ports = Ports()
    ports.profile = {"provider_idempotency": "supported"}
    clock = Clock()
    svc = _svc(ports=ports, clock=clock)
    job_id = _unknown(svc, _admitted(svc, ports))
    key_before = _intent(svc, job_id)["stable_client_key"]
    submits = _submit_events(svc.conn)
    clock.advance(121)
    out = svc.reconcile_due(T, job_id)
    assert out["outcome"] == "resubmitted"
    assert _state(svc, job_id) == "DISPATCH_AUTHORIZED"
    assert _intent(svc, job_id)["stable_client_key"] == key_before
    assert _submit_events(svc.conn) == submits + 1
    assert _row(svc, job_id)["submit_attempts"] == 1   # 下次 claim 才再 +1


def test_t26_12_unsupported_without_bill_stays_unknown():
    """T26-12：C 档且账单没覆盖 → 留在 UNKNOWN，不重发。"""
    ports = Ports()
    ports.profile = {"provider_idempotency": "unsupported"}
    ports.bill_covers = False
    clock = Clock()
    svc = _svc(ports=ports, clock=clock)
    job_id = _unknown(svc, _admitted(svc, ports))
    submits = _submit_events(svc.conn)
    clock.advance(100000)
    out = svc.reconcile_due(T, job_id)
    assert out["outcome"] == "not_found"
    assert _state(svc, job_id) == "SUBMISSION_UNKNOWN"
    assert _submit_events(svc.conn) == submits


def test_t26_13_observation_matched_by_client_key():
    """T26-13：回调只带 `stable_client_key`（无 job id、无 ref）也能挂上。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    key = _intent(svc, job_id)["stable_client_key"]
    result = svc.consume_observation(
        T, _obs(key=key, status="running", ref="pref_late"))
    assert result["applied"] is True
    assert _state(svc, job_id) == "RUNNING"


def test_t26_14_duplicate_delivery_is_idempotent():
    """T26-14：同 `event_id` 重投 5 次 → 只应用一次，`aggregate_sequence` 只 +1。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    seq_before = _row(svc, job_id)["aggregate_sequence"]
    results = [svc.consume_observation(
        T, _obs(job_id=job_id, status="running", event_id="evt_dup"))
        for _ in range(5)]
    applied = [r for r in results if r.get("applied")]
    dup = [r for r in results if r.get("reason") == "DUPLICATE"]
    assert len(applied) == 1 and len(dup) == 4
    assert _row(svc, job_id)["aggregate_sequence"] == seq_before + 1


def test_t26_15_out_of_order_observation_does_not_regress():
    """T26-15：RUNNING 之后收到 QUEUED → 拒绝倒退，但水印前进。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    svc.consume_observation(T, _obs(job_id=job_id, status="running",
                                    event_id="evt_run",
                                    observed_at="2026-11-12T00:00:10.000000Z"))
    late = "2026-11-12T00:00:20.000000Z"
    result = svc.consume_observation(
        T, _obs(job_id=job_id, status="queued", event_id="evt_late",
                observed_at=late))
    assert result["applied"] is False
    assert result["reason"] == "OUT_OF_ORDER"
    assert _state(svc, job_id) == "RUNNING"
    assert _row(svc, job_id)["poll_watermark"] == late


def test_t26_16_cross_tenant_event_is_refused():
    """T26-16：事件 `tenant_id` 与头部不一致 → 403，状态不变。"""
    client, ports, keep = _http_client()
    created = client.post("/generation-jobs", headers=_hdr(),
                          json={"route_decision_id": "route_1",
                                "generation_command": _command()})
    job_id = created.json()["operation_id"]
    resp = client.post("/internal/events/connector.observation.received",
                       headers=_hdr(key="evt_evil", principal="S27"),
                       json=_obs(job_id=job_id, tenant=U, status="succeeded"))
    assert resp.status_code == 403
    assert resp.json()["code"] == "TENANT_SCOPE_DENIED"
    row = keep.execute("SELECT state FROM provider_job WHERE provider_job_id = ?",
                       (job_id,)).fetchone()
    assert row["state"] == "CREATED"


# ------------------------------------------------------------------ 回执与拒绝

def test_t26_17_not_sent_honours_retry_after():
    """T26-17：`not_sent` + `retry_after_seconds` → 回 `DISPATCH_AUTHORIZED` 排期。"""
    ports = Ports()
    clock = Clock()
    svc = _svc(ports=ports, clock=clock)
    job_id = _submitted(svc, ports)
    dispatch = _intent(svc, job_id)["dispatch_id"]
    submits = _submit_events(svc.conn)
    out = svc.record_submit_outcome(
        T, dispatch, "not_sent", retry_after_seconds=30.0)
    assert out["state"] == "DISPATCH_AUTHORIZED"
    assert _row(svc, job_id)["next_action_at"] == "2026-11-12T00:00:30.000000Z"
    assert _submit_events(svc.conn) == submits


def test_t26_18_unknown_outcome_freezes():
    """T26-18：`unknown` → `SUBMISSION_UNKNOWN`，什么都不释放。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _unknown(svc, _admitted(svc, ports))
    assert _state(svc, job_id) == "SUBMISSION_UNKNOWN"
    assert ports.count("release_budget") == 0
    assert ports.count("release_permit") == 0


def test_t26_19_account_unauthorized_releases_and_isolates():
    """T26-19：`ACCOUNT_UNAUTHORIZED` → 释放两侧、隔离账户池、不动渠道。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    before = _row(svc, job_id)
    dispatch = _intent(svc, job_id)["dispatch_id"]
    svc.record_submit_outcome(T, dispatch, "rejected",
                              error_class="ACCOUNT_UNAUTHORIZED")
    after = _row(svc, job_id)
    assert after["state"] == "PROVIDER_FAILED"
    assert ports.count("release_budget") == 1
    assert ports.count("release_permit") == 1
    assert ports.count("isolate_account_pool") == 1
    assert (after["provider"], after["channel"], after["account_pool_id"]) == (
        before["provider"], before["channel"], before["account_pool_id"])


def test_t26_20_capability_invalid_blocks_dispatch():
    """T26-20：S24 判能力失效 → `PROVIDER_FAILED` + `ROUTE_INVALIDATED`，不派单。"""
    ports = Ports()
    ports.capability = {"items": [{"usable": False, "code": "MODEL_RETIRED"}]}
    svc = _svc(ports=ports)
    job = svc.advance(T, _create(svc)["provider_job_id"])
    assert job["state"] == "PROVIDER_FAILED"
    assert _row(svc, job["provider_job_id"])["last_error_code"] == "MODEL_RETIRED"
    assert _submit_events(svc.conn) == 0
    assert ports.count("release_budget") == 1
    assert ports.count("release_permit") == 1


def test_t26_20b_unwired_capability_port_blocks_dispatch():
    """端口表承诺「没注入就抛 503，不静默降级」，S24 复查也不能例外。

    派单前必须实时复查能力（AGENTS.md / §4 步骤 4 第 3 条）。只缺这一个端口时，
    job 必须停在 `ADMITTED`、不签 grant、不发派单事件 —— 与端口调用超时同路。
    """
    ports = Ports()
    wired = ports.as_ports()
    wired.pop("check_capability_validity")
    svc = GenerationJobsService(_conn(), clock=Clock(), **wired)
    job_id = _create(svc)["provider_job_id"]
    with pytest.raises(ServiceError) as exc:
        svc.advance(T, job_id)
    assert exc.value.code == "DEPENDENCY_UNAVAILABLE"
    assert exc.value.retryable is True
    assert _state(svc, job_id) == "ADMITTED"
    assert _submit_events(svc.conn) == 0
    assert _grant_count(svc) == 0


def test_t26_20c_capability_port_timeout_stays_admitted():
    """S24 超时/5xx → 停在 `ADMITTED` 重试，不签 grant（§4 步骤 4「超时」一行）。"""
    ports = Ports()
    ports.capability_error = ServiceError(503, "CAPABILITY_UNAVAILABLE", "timeout",
                                          retryable=True)
    svc = _svc(ports=ports)
    with pytest.raises(ServiceError) as exc:
        svc.advance(T, _create(svc)["provider_job_id"])
    assert exc.value.code == "DEPENDENCY_UNAVAILABLE"
    assert exc.value.retryable is True
    assert _submit_events(svc.conn) == 0


def test_t26_21_plain_rejection_keeps_account_pool():
    """T26-21：普通业务拒绝 → 不隔离账户池、不报能力漂移。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    svc.record_submit_outcome(
        T, _intent(svc, job_id)["dispatch_id"], "rejected",
        error_class="PROVIDER_REJECTED")
    assert _state(svc, job_id) == "PROVIDER_FAILED"
    assert ports.count("isolate_account_pool") == 0
    assert ports.count("report_capability_drift") == 0


def test_t26_22_capability_drift_is_reported():
    """T26-22：`CAPABILITY_DRIFT` → 上报 S24。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    svc.record_submit_outcome(
        T, _intent(svc, job_id)["dispatch_id"], "rejected",
        error_class="CAPABILITY_DRIFT")
    assert ports.count("report_capability_drift") == 1


# ------------------------------------------------------------------ 无进程内状态

def test_t26_23_unknown_survives_new_service_instance():
    """T26-23：换一个 service 实例读同一个库，UNKNOWN 仍在。"""
    ports = Ports()
    conn = _conn()
    svc = _svc(conn=conn, ports=ports)
    job_id = _unknown(svc, _admitted(svc, ports))
    other = _svc(conn=conn, ports=ports)
    assert other.get_job(T, job_id)["state"] == "SUBMISSION_UNKNOWN"
    assert other.get_job(T, job_id)["frozen"] is True


def test_t26_24_leak_scan_does_not_false_alarm():
    """T26-24：permit 侧仍 AUTHORIZED 时，泄漏扫描不误报、也不自动改。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _unknown(svc, _admitted(svc, ports))
    out = svc.scan_reservation_leak(T)
    assert out["action"] == "alert_only"
    assert out["mismatches"] == []
    assert _state(svc, job_id) == "SUBMISSION_UNKNOWN"
    assert ports.count("release_budget") == 0


def test_t26_25_rearchive_uses_existing_url():
    """T26-25：INGESTING 期间渠道换发新 URL 会被乱序拦下，只能 `rearchive=True`。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    svc.record_submit_outcome(
        T, _intent(svc, job_id)["dispatch_id"], "accepted",
        provider_job_ref="pref_1")
    svc.consume_observation(T, _obs(job_id=job_id, status="succeeded",
                                    output=_output()))
    assert _state(svc, job_id) == "INGESTING"
    seq = _row(svc, job_id)["aggregate_sequence"]
    new_url = "https://cdn.example/out/second.mp4"
    late = svc.consume_observation(
        T, _obs(job_id=job_id, status="succeeded", event_id="evt_late",
                output=_output(url=new_url)))
    assert late["applied"] is False
    assert late["reason"] == "OUT_OF_ORDER"
    assert _row(svc, job_id)["aggregate_sequence"] == seq
    out = svc.start_ingest(T, job_id, rearchive=True)
    assert out["artifact_seq"] == 2
    assert _intent(svc, job_id)["artifact_seq"] == 2
    assert _submit_events(svc.conn) == 1
    assert ports.count("create_artifact_import") == 2


def test_t26_26_ingest_gap_is_visible():
    """T26-26：归档重试耗尽 → `ARCHIVED` + `INGEST_FAILED:*`，缺口可见。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    svc.record_submit_outcome(
        T, _intent(svc, job_id)["dispatch_id"], "accepted",
        provider_job_ref="pref_1")
    svc.consume_observation(T, _obs(job_id=job_id, status="succeeded",
                                    output=_output()))
    assert _state(svc, job_id) == "INGESTING"
    out = svc.report_ingest_gap(T, job_id, reason="HASH_MISMATCH")
    assert out["state"] == "ARCHIVED"
    assert _row(svc, job_id)["last_error_code"] == "INGEST_FAILED:HASH_MISMATCH"
    assert _state(svc, job_id) == "ARCHIVED"


def test_t26_27_artifact_ingested_archives():
    """T26-27：`artifact.ingested` → `ARCHIVED`，引用落库；重投不重复推进。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    svc.record_submit_outcome(
        T, _intent(svc, job_id)["dispatch_id"], "accepted",
        provider_job_ref="pref_1")
    svc.consume_observation(T, _obs(job_id=job_id, status="succeeded",
                                    output=_output()))
    assert _row(svc, job_id)["submit_attempts"] == 1
    submits = _submit_events(svc.conn)
    envelope = {"id": "evt_art", "tenant_id": T, "correlation_id": "ai",
                "data": {"schema_version": "2.0", "artifact_id": "art_1",
                         "artifact_ref": {"id": "art_1", "sha256": "3" * 64},
                         "source": {"provider_job_id": job_id}}}
    first = svc.on_artifact_ingested(T, envelope)
    assert first["applied"] is True
    assert _state(svc, job_id) == "ARCHIVED"
    assert json.loads(_row(svc, job_id)["artifact_refs"])[0]["id"] == "art_1"
    seq = _row(svc, job_id)["aggregate_sequence"]
    again = svc.on_artifact_ingested(T, envelope)
    assert again["reason"] == "DUPLICATE"
    assert _row(svc, job_id)["aggregate_sequence"] == seq
    # 归档不是重新生成
    assert _submit_events(svc.conn) == submits


def test_t26_28_ingest_ttl_warning_window():
    """T26-28：URL 30 分钟后到期 → 3600s 窗口报出，默认 900s 窗口不报。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    svc.record_submit_outcome(
        T, _intent(svc, job_id)["dispatch_id"], "accepted",
        provider_job_ref="pref_1")
    svc.consume_observation(
        T, _obs(job_id=job_id, status="succeeded",
                output=_output(expires="2026-11-12T00:30:00Z")))
    assert _state(svc, job_id) == "INGESTING"
    assert svc.scan_ingest_ttl_warning(T)["at_risk"] == []
    wide = svc.scan_ingest_ttl_warning(T, window_seconds=3600.0)
    assert [w["provider_job_id"] for w in wide["at_risk"]] == [job_id]


def test_t26_29_expired_quote_is_refused():
    """T26-29：路由报价已过期 → 409 `ROUTE_QUOTE_EXPIRED`，不建单。"""
    ports = Ports()
    ports.route = _route(quote_valid_to="2026-11-11T00:00:00Z")
    svc = _svc(ports=ports)
    with pytest.raises(ServiceError) as exc:
        _create(svc)
    assert exc.value.status == 409
    assert exc.value.code == "ROUTE_QUOTE_EXPIRED"
    assert svc.conn.execute(
        "SELECT COUNT(*) FROM provider_job").fetchone()[0] == 0


def test_t26_30_requirement_hash_uses_snapped_input():
    """T26-30：请求 4 秒、S25 吸附到 5 秒 → hash 与派单载荷都按 5 秒。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _admitted(svc, ports)
    expected = requirement_hash_for({"duration_seconds": 5})
    assert _row(svc, job_id)["requirement_hash"] == expected
    envelope = json.loads(svc.conn.execute(
        "SELECT payload FROM outbox WHERE event_type = 'connector.submit.requested'"
    ).fetchone()["payload"])
    data = envelope["data"]
    assert data["submit_payload_inline"] == {"duration_seconds": 5}
    assert data["request_hash"] == expected


def test_t26_31_attempt_limit_is_enforced():
    """T26-31：同一 node 的两次业务尝试超上限 → `ATTEMPT_LIMIT_EXCEEDED`。"""
    ports = Ports()
    svc = _svc(ports=ports, max_attempts_per_node=1)
    _admitted(svc, ports)
    # 路由决策必须跟着换 attempt，否则先撞 `ROUTE_DECISION_STALE`（那条检查
    # 在 attempt 上限之前）。
    ports.route = _route(attempt_id="attempt_2")
    with pytest.raises(ServiceError) as exc:
        _create(svc, command=_command(attempt_id="attempt_2",
                                      idempotency_key="key_2"))
    assert exc.value.status == 409
    assert exc.value.code == "ATTEMPT_LIMIT_EXCEEDED"


def test_t26_32_settlement_accumulates_and_keeps_job_state():
    """T26-32：结算累计 `micro_spent`，且不改 job 行状态。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    svc.record_submit_outcome(
        T, _intent(svc, job_id)["dispatch_id"], "rejected",
        error_class="PROVIDER_REJECTED")
    state_before = _state(svc, job_id)
    out = svc.settle_job(T, job_id, actual_micro=900)
    assert out["state"] == "SETTLED"
    assert out["charged_micro"] == 900
    assert out["quote_breached"] is False
    assert _state(svc, job_id) == state_before
    ledger = svc.conn.execute(
        "SELECT micro_spent FROM attempt_ledger WHERE tenant_id = ?"
        " AND node_id = 'shot_3_2.video'", (T,)).fetchone()
    assert ledger["micro_spent"] == 900


def test_t26_33_fence_bump_supersedes_run_branch():
    """T26-33：`execution_fence` 提升 → 未领 grant 回 ADMITTED，已提交进 UNKNOWN。"""
    ports = Ports()
    svc = _svc(ports=ports)
    unclaimed = _admitted(svc, ports)
    ports.route = _route(attempt_id="attempt_2")
    submitted = _admitted(svc, ports,
                          command=_command(attempt_id="attempt_2",
                                           idempotency_key="key_2"))
    _claim(svc, submitted)
    svc.on_execution_fence_bumped(T, run_id="run_1", branch_id="branch_main",
                                  execution_fence=3)
    assert _state(svc, unclaimed) == "ADMITTED"
    assert _state(svc, submitted) == "SUBMISSION_UNKNOWN"


def test_t26_34_epoch_bump_is_per_node():
    """T26-34：`node_epoch` 只管单个 `logical_node`，另一个节点不动。"""
    ports = Ports()
    svc = _svc(ports=ports)
    same = _admitted(svc, ports)
    ports.route = _route(attempt_id="attempt_2", node_id="shot_4.video")
    other_node = _admitted(
        svc, ports, command=_command(attempt_id="attempt_2",
                                     node_id="shot_4.video",
                                     idempotency_key="key_2"))
    svc.on_node_epoch_bumped(T, node_id="shot_3_2.video", node_epoch=5)
    assert _state(svc, same) == "PROVIDER_FAILED"
    assert _state(svc, other_node) == "DISPATCH_AUTHORIZED"


def test_t26_35_failover_does_not_resubmit():
    """T26-35：灾备切主 → 全部进 `RECONCILING`，不重提、不新增派单事件。"""
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    submits = _submit_events(svc.conn)
    out = svc.recover_after_failover(T)
    assert out["provider_job_ids"] == [job_id]
    assert _state(svc, job_id) == "RECONCILING"
    assert _submit_events(svc.conn) == submits


def test_t26_36_expired_grant_is_reissued():
    """T26-36：未领 grant 过期 → 撤销重签，不重新花钱。"""
    ports = Ports()
    clock = Clock()
    svc = _svc(ports=ports, clock=clock)
    job_id = _admitted(svc, ports)
    submits = _submit_events(svc.conn)
    clock.advance(301)
    out = svc.scan_expired_grants(T)
    assert out["expired"] == 1
    actions = [a["action"] for a in out["acted"]]
    assert actions == ["reissued"]
    assert _state(svc, job_id) == "DISPATCH_AUTHORIZED"
    rows = svc.conn.execute(
        "SELECT state, fencing_token FROM dispatch_grant WHERE provider_job_id = ?"
        " ORDER BY fencing_token", (job_id,)).fetchall()
    assert [r["state"] for r in rows] == ["REVOKED", "ISSUED"]
    assert [r["fencing_token"] for r in rows] == [1, 2]
    # 重签只是重新授权一次，不产生新的派单请求（那是 claim 之后的事）
    assert _submit_events(svc.conn) == submits + 1   # `_issue_grant` 自己发一条


def test_t26_37_outcome_without_submitting_is_not_applied():
    """T26-37：job 不在 `SUBMITTING` 时回执不生效，也不发出站事件。

    S27 的 claim 失败重试发生在 S27 进程里；S26 这一侧能证明的是：已签 grant
    但没人领走时，任何回执都是 `applied: false`，且 `_submit_events` 不增
    （§10.7「没有成功 claim，就不发 HTTP」）。
    """
    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _admitted(svc, ports)
    dispatch = _intent(svc, job_id)["dispatch_id"]
    submits = _submit_events(svc.conn)
    out = svc.record_submit_outcome(T, dispatch, "accepted",
                                    provider_job_ref="pref_1")
    assert out["applied"] is False
    assert out["state"] == "DISPATCH_AUTHORIZED"
    assert _state(svc, job_id) == "DISPATCH_AUTHORIZED"
    assert _submit_events(svc.conn) == submits


def test_t26_38_admission_record_matches_contract():
    """T26-38：UNKNOWN 的 admission record 过 `admission-record.v2` 校验。"""
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    ports = Ports()
    svc = _svc(ports=ports)
    job_id = _unknown(svc, _admitted(svc, ports))
    adm = svc.get_admission(T, admission_id_for(intent_id_for(T, job_id)))

    # 按各 schema 自己的 `$id`（`https://drama.example.invalid/contracts/...`）
    # 注册 —— 与 `tools/validate/validate_examples.py` 同一套做法。
    reg = Registry()
    for path in CONTRACTS.glob("*.schema.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        reg = reg.with_resource(
            doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
    schema = json.loads(
        (CONTRACTS / "admission-record.v2.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, registry=reg)
    errors = sorted(validator.iter_errors(adm), key=lambda e: list(e.path))
    assert not errors, [e.message for e in errors]

    # allOf 里那条条件规则的四项要真成立（不靠 schema 兜底）
    assert adm["state"] == "SUBMISSION_UNKNOWN"
    assert adm["must_reconcile_before_resubmit"] is True
    assert adm["reservation"]["state"] in ("AUTHORIZED", "ACCRUED")
    assert adm["quota_permit"]["state"] == "INFLIGHT"
    assert adm["dispatch"]["grant_state"] == "CLAIMED"


def test_t26_39_not_cancellable_discards_on_completion():
    """T26-39：供应商说不可取消 → 状态不变，标 `DISCARD_ON_COMPLETION`，仍可结算。"""
    ports = Ports()
    ports.cancel = {"result": "not_cancellable"}
    svc = _svc(ports=ports)
    job_id = _submitted(svc, ports)
    svc.record_submit_outcome(
        T, _intent(svc, job_id)["dispatch_id"], "accepted",
        provider_job_ref="pref_1")
    svc.consume_observation(T, _obs(job_id=job_id, status="running",
                                    event_id="evt_run"))
    assert _state(svc, job_id) == "RUNNING"
    out = svc.cancel_job(T, job_id)
    assert out["result"] == "not_cancellable"
    assert out["disposition"] == "DISCARD_ON_COMPLETION"
    # 终态只有 ARCHIVED；`CANCEL_REQUESTED` 是 last_error_code，不是状态。
    assert _state(svc, job_id) == "RUNNING"
    assert _row(svc, job_id)["disposition"] == "DISCARD_ON_COMPLETION"
    assert _row(svc, job_id)["last_error_code"] == "CANCEL_REQUESTED"
    # 结果仍要收、仍要结算（取消 ≠ 退款）
    svc.consume_observation(T, _obs(job_id=job_id, status="succeeded",
                                    event_id="evt_done", output=_output()))
    assert _state(svc, job_id) == "INGESTING"
    settled = svc.settle_job(T, job_id, actual_micro=500)
    assert settled["state"] == "SETTLED"
    assert settled["charged_micro"] == 500


def test_t26_40_runs_on_sqlite():
    """T26-40：方案写 Postgres，本仓库统一 SQLite —— 断言偏差本身。"""
    svc = _svc()
    assert isinstance(svc.conn, sqlite3.Connection)
    tables = {r["name"] for r in svc.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"generation_attempt", "provider_job", "submission_intent",
            "dispatch_grant", "job_transition", "reconciliation_attempt",
            "resubmission_approval", "attempt_ledger"} <= tables


# ------------------------------------------------------------------ 入口守卫（HTTP）

def test_http_janitor_sweep_shape():
    """`/janitor:sweep` 返回恰 8 个分桶（`docs/16` 的运维面）。"""
    client, _ports, _keep = _http_client()
    resp = client.post("/janitor:sweep", headers=_hdr(principal="janitor"), json={})
    assert resp.status_code == 200
    assert set(resp.json()) == {
        "due_jobs", "stuck_submitting", "unknown_submissions", "expired_grants",
        "ingest_ttl_warning", "ingest_backlog", "orphan_intents",
        "reservation_leak_check"}


def test_http_internal_principal_allowlist():
    """内部端点只认服务主体；未知主体 403。"""
    client, _ports, _keep = _http_client()
    bad = client.post("/internal/fence-bumped", headers=_hdr(principal="tenant_admin"),
                      json={"run_id": "r", "branch_id": "b", "execution_fence": 1})
    assert bad.status_code == 403
    assert bad.json()["code"] == "TENANT_SCOPE_DENIED"


def test_http_unknown_topic_is_rejected():
    """未知事件 topic → 400，不是静默吞掉。"""
    client, _ports, _keep = _http_client()
    resp = client.post("/internal/events/nope.topic",
                       headers=_hdr(principal="S27"),
                       json={"id": "e1", "tenant_id": T, "data": {}})
    assert resp.status_code == 400
    assert resp.json()["code"] == "MALFORMED_REQUEST"


def test_http_body_tenant_must_match_header():
    """body 里的 `tenant_id` 与头部不一致 → 403（不许 LLM 自带租户）。"""
    client, _ports, _keep = _http_client()
    resp = client.post("/generation-jobs", headers=_hdr(),
                       json={"route_decision_id": "route_1",
                             "generation_command": _command(),
                             "tenant_id": U})
    assert resp.status_code == 403
    assert resp.json()["code"] == "TENANT_SCOPE_DENIED"
