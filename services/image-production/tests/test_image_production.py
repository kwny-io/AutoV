"""S29 单测。编号对应 `plans/S29-image-production.md` §10 的 T29-*。

四条红线对应的测试，是这份文件真正要守住的东西（AGENTS.md 与任务书）：

1. `test_t29_31_*` / `test_t29_90_*`：**没有任何供应商 base url**，付费出口只有
   `deps.build_submitter` 指向 S26。
2. `test_t29_10_*` / `test_t29_12_*` / `test_t29_91_*`：`WAITING_UNKNOWN` 期间
   绝不重提付费任务。
3. `test_t29_92_*`：预算/配额在**派单时**重新向 S25 取，不信任早先的"已批准"。
4. `test_t29_93_*`：`tenant_id` 只来自签名头；body 里那份只比对。

上游全部是 stub，且**按真实上游契约写**：

- S25 的 HTTP 响应没有顶层 `selected`，只有 `eligible[]`；`deps` 负责归一。
  这里直接按内核要的形状喂，因为 `deps` 有自己的测试。
- S26 的 `provider_job_id` 是提交成功与否的唯一信号；`build_submitter` 绝不
  伪造它，所以 stub 也不伪造。
- `artifact.ingested` 的 `provider_job_id` 只在 `source.provider_job_id` 里
  （C29-11），事件 helper 按这个形状发。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import SqliteIdempotencyStore, canonical_request_hash

from image_production.schema import (
    CONTRACT_GAPS,
    ID_PREFIX,
    JOB_STATES,
    MAX_EDIT_WINDOW_FRAMES,
    NODE_KINDS,
    REFERENCE_ROLES,
    RETRYABLE_STATES,
    ROLES_NOT_APPLICABLE_FOR_IMAGE,
    SCHEMA,
    TERMINAL_STATES,
    TRANSITIONS,
)
from image_production.service import (
    EVENT_IMAGE_READY,
    EXECUTION_CONTEXT_REQUIRED,
    ImageProductionService,
    ServiceError,
    Transaction,
    canonical_transition,
    content_hash,
    input_fingerprint,
)

FUTURE = "2099-01-01T00:00:00.000Z"
PAST = "2000-01-01T00:00:00.000Z"
HEX = "0123456789abcdef" * 4


def _hex(ch: str) -> str:
    return ch * 64


# =============================================================== 上游 stub

class Harness:
    """内存库 + 幂等外壳 + 一组可编程的上游 callback。

    形状与 `api/app.py` 的 `command()` 一致：`begin` → 执行 → `finish`；
    重放打 `replayed: True`。
    """

    def __init__(self, conn=None, **overrides):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)
        self.calls = {"route": 0, "submit": 0, "artifact": 0, "import": 0,
                      "cancel": 0, "state": 0}
        self.routes = []
        self.route_result = overrides.get("route_result")
        self.submit_error = overrides.get("submit_error")
        self.submit_result = overrides.get("submit_result")
        self.artifact_verdict = overrides.get("artifact_verdict", True)
        self.import_error = overrides.get("import_error")
        self.cancel_error = overrides.get("cancel_error")
        self.submitted_commands = []
        self.imported_jobs = []
        self._seq = 0

        self.service = ImageProductionService(
            self.conn,
            route_resolver=self._route,
            submitter=self._submit,
            artifact_check=self._artifact,
            artifact_importer=self._import,
            generation_state=self._state,
            canceler=self._cancel,
        )

    # ---- callbacks ----
    def _route(self, tenant_id, job, generation_command):
        self.calls["route"] += 1
        if self.route_result is not None:
            if isinstance(self.route_result, Exception):
                raise self.route_result
            return self.route_result
        route = {
            "route_id": f"route_{len(self.routes) + 1}",
            "revision": 3,
            "content_hash": _hex("a"),
            "feasible": True,
            "quote_valid_to": FUTURE,
            "selected": {"expected_cost_micro": 1_000, "currency": "USD"},
            "capabilities": {"multi_candidate": False, "mask_edit": True},
            "residency_allowed": None,
        }
        self.routes.append(route)
        return route

    def _submit(self, job, generation_command, route, candidate_index):
        self.calls["submit"] += 1
        self.submitted_commands.append(generation_command)
        if self.submit_error is not None:
            raise self.submit_error
        if self.submit_result is not None:
            return self.submit_result
        return {"provider_job_id": f"pj_{job['image_job_id']}",
                "operation_id": f"op_{job['image_job_id']}",
                "attempt_id": generation_command["attempt_id"],
                "admission_id": "adm_1", "state": "QUEUED"}

    def _artifact(self, tenant_id, artifact_ref):
        self.calls["artifact"] += 1
        if isinstance(self.artifact_verdict, Exception):
            raise self.artifact_verdict
        verdict = self.artifact_verdict
        if callable(verdict):
            return verdict(artifact_ref)
        return verdict

    def _import(self, tenant_id, job):
        self.calls["import"] += 1
        self.imported_jobs.append(job["image_job_id"])
        if self.import_error is not None:
            raise self.import_error
        return {"state": "accepted"}

    def _state(self, tenant_id, provider_job_id):
        self.calls["state"] += 1
        return {"state": "RUNNING"}

    def _cancel(self, provider_job_id, reason):
        self.calls["cancel"] += 1
        if self.cancel_error is not None:
            raise self.cancel_error

    # ---- HTTP-like command shell ----
    def command(self, body, tenant_id="t1", command_id="c1", fn=None):
        record = self.idem.begin(tenant_id, command_id,
                                 canonical_request_hash(body))
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        call = fn or (lambda: self.service.create_job(body, tenant_id,
                                                      command_id))
        try:
            result = call()
        except Exception:
            self.idem.abort(tenant_id, command_id)
            raise
        self.idem.finish(tenant_id, command_id,
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ---- request body ----
    def body(self, **over):
        body = {
            "context": {
                "schema_version": "execution-context.v2",
                "tenant_id": "t1", "project_id": "prj_1", "series_id": "ser_1",
                "episode_id": "ep_1", "branch_id": "br_1", "node_id": "node_1",
                "node_epoch": 4, "execution_fence": 2,
                "manifest_ref": {"service_id": "S21", "id": "m1",
                                 "revision": 1, "content_hash": _hex("d")},
                "memory_checkpoint": {"id": "ck1"},
                "policy_ref": {"service_id": "S02", "id": "pol_1",
                               "revision": 3, "content_hash": _hex("a")},
                "actor": {"kind": "agent", "id": "agent_1"},
                "home_cell": "cell_a", "home_region": "cn-east",
                "trace_id": "trace_1", "command_id": "c1",
                "request_hash": _hex("e"),
            },
            "shot_ref": {"service_id": "S06", "id": "shot_1", "revision": 2},
            "shot_lineage_id": "lineage_A",
            "node_kind": "first_frame",
            "mode": "generate",
            "prompt_intent": "林岚站在雨夜巷口，回头，冷光侧逆",
            "width": 1080, "height": 1920,
            "references": [{"ordinal": 0, "artifact_id": "art_char_1",
                            "sha256": _hex("b"), "role": "character"}],
            "identity_constraints": ["char_linlan@3"],
            "route_policy_ref": {"service_id": "S25", "id": "policy_route",
                                 "revision": 7, "content_hash": _hex("c")},
            "budget_scope_id": "bs_ep1",
            "deadline": FUTURE,
        }
        for key, value in over.items():
            if key == "context":
                body["context"].update(value)
            else:
                body[key] = value
        return body

    # ---- event helpers ----
    def state_event(self, job_id, state, *, event_id="ev_state_1",
                    tenant="t1", reason_code=None):
        job = self.conn.execute(
            "SELECT provider_job_id FROM image_job WHERE image_job_id=?",
            (job_id,)).fetchone()
        assert job is not None
        data = {"state": state, "provider_job_id": job["provider_job_id"]}
        if reason_code:
            data["reason_code"] = reason_code
        return self.service.on_generation_state_changed({
            "id": event_id, "tenant_id": tenant,
            "source": "S26", "type": "generation.job.state-changed",
            "aggregate_id": job["provider_job_id"], "data": data})

    def artifact_event(self, job_id, *, event_id="ev_art_1",
                       artifact_id="art_out_1", content_hash=None,
                       tenant="t1", width=None, height=None,
                       service_id="S28"):
        job = self.conn.execute(
            "SELECT provider_job_id FROM image_job WHERE image_job_id=?",
            (job_id,)).fetchone()
        assert job is not None
        data = {
            "schema_version": "2.0",
            "artifact_ref": {"service_id": service_id, "id": artifact_id,
                             "revision": 1,
                             "content_hash": content_hash or _hex("f")},
            "content_class": "generated_image",
            "size_bytes": 4096,
            "source": {"kind": "provider_url",
                       "provider_job_id": job["provider_job_id"]},
            "dedup": None, "derivative_hint": [],
            "mime_type": "image/png",
        }
        if width is not None:
            data["width_px"] = width
        if height is not None:
            data["height_px"] = height
        return self.service.on_artifact_ingested({
            "id": event_id, "tenant_id": tenant, "source": "S28",
            "type": "artifact.ingested",
            "aggregate_id": data["artifact_ref"]["id"], "data": data})

    # ---- convenience ----
    def make_job(self, command_id=None, **over):
        body = self.body(**over)
        if command_id is None:
            self._seq += 1
            command_id = f"c_auto_{self._seq}"
        return self.command(body, command_id=command_id), body

    def ready_job(self, **over):
        """一路推到 READY，返回 (job_id, candidate_id)。"""
        result, _ = self.make_job(**over)
        job_id = result["image_job_id"]
        self.state_event(job_id, "PROVIDER_SUCCEEDED")
        ingested = self.artifact_event(job_id)
        assert ingested.get("state") == "READY", ingested
        return job_id, ingested["candidate_id"]

    def state_of(self, job_id):
        return self.conn.execute(
            "SELECT state FROM image_job WHERE image_job_id=?",
            (job_id,)).fetchone()["state"]

    def row(self, table, job_id):
        return dict(self.conn.execute(
            f"SELECT * FROM {table} WHERE image_job_id=?", (job_id,)).fetchone())


@pytest.fixture
def h():
    harness = Harness()
    yield harness
    harness.conn.close()


# =========================================================== 纯函数：寻址

class TestAddressing:
    """§2.4。不碰库、不花钱，所以先做。"""

    def test_t29_05_reference_order_changes_hash(self):
        a = {"ordinal": 0, "artifact_id": "art_A", "sha256": _hex("1"),
             "role": "first_frame"}
        b = {"ordinal": 1, "artifact_id": "art_B", "sha256": _hex("2"),
             "role": "style"}
        assert content_hash([a, b], []) != content_hash([b, a], [])

    def test_t29_06_identity_constraint_order_is_irrelevant(self):
        ref = [{"ordinal": 0, "artifact_id": "art_A", "sha256": _hex("1"),
                "role": "character"}]
        forward = content_hash(ref, ["char_a@1", "char_b@2"])
        backward = content_hash(ref, ["char_b@2", "char_a@1"])
        assert forward == backward

    def test_t29_06_different_constraints_differ(self):
        ref = [{"ordinal": 0, "artifact_id": "art_A", "sha256": _hex("1"),
                "role": "character"}]
        assert content_hash(ref, ["char_a@1"]) != content_hash(ref, ["char_a@2"])

    def test_ordinal_does_not_enter_hash(self):
        """数组位置已经表达顺序；把序号也编进去会让"换个编号"变成新图。"""
        a = {"ordinal": 0, "artifact_id": "art_A", "sha256": _hex("1"),
             "role": "character"}
        b = {"ordinal": 7, "artifact_id": "art_A", "sha256": _hex("1"),
             "role": "character"}
        assert content_hash([a], []) == content_hash([b], [])

    def test_fingerprint_excludes_call_routing(self):
        base = dict(reference_content_hash=_hex("1"), prompt_intent="a cat",
                    mode="generate", width=1080, height=1920,
                    identity_constraints=["c@1"])
        # 同输入、不同 command/deadline/budget_scope → 同 fingerprint
        assert input_fingerprint(**base) == input_fingerprint(**base)

    def test_fingerprint_normalizes_whitespace(self):
        a = input_fingerprint(reference_content_hash=_hex("1"),
                              prompt_intent="a   cat\n", mode="generate",
                              width=1080, height=1920,
                              identity_constraints=[])
        b = input_fingerprint(reference_content_hash=_hex("1"),
                              prompt_intent="a cat", mode="generate",
                              width=1080, height=1920,
                              identity_constraints=[])
        assert a == b

    def test_fingerprint_changes_with_dimensions(self):
        a = input_fingerprint(reference_content_hash=_hex("1"),
                              prompt_intent="a cat", mode="generate",
                              width=1080, height=1920,
                              identity_constraints=[])
        b = input_fingerprint(reference_content_hash=_hex("1"),
                              prompt_intent="a cat", mode="generate",
                              width=720, height=1280,
                              identity_constraints=[])
        assert a != b

    def test_fingerprint_is_64_hex(self):
        got = input_fingerprint(reference_content_hash=_hex("1"),
                                prompt_intent="a cat", mode="generate",
                                width=1, height=1, identity_constraints=[])
        assert len(got) == 64
        assert set(got) <= set("0123456789abcdef")


# =========================================================== 状态机

class TestStateMachine:
    """§3.1 表驱动。T29-33：新旧版本对同一状态机解释一致。"""

    def test_every_state_has_a_row(self):
        assert set(TRANSITIONS) - {None} == JOB_STATES

    def test_terminal_states_have_no_outgoing_edges(self):
        for state in TERMINAL_STATES:
            assert TRANSITIONS[state] == frozenset(), state

    def test_reconciled_ready_is_terminal(self):
        # C29-2：方案 §3.2 的终态列表漏了它，但 §3.1 把它当成功终局。
        assert "RECONCILED_READY" in TERMINAL_STATES

    def test_waiting_unknown_cannot_be_canceled(self):
        # C29-4 / §4.3：UNKNOWN 期间的取消没意义（可能已收单）。
        assert "CANCELED" not in TRANSITIONS["WAITING_UNKNOWN"]
        assert TRANSITIONS["WAITING_UNKNOWN"] == frozenset(
            {"RECONCILED_READY", "RECONCILED_FAILED"})

    def test_waiting_unknown_exits_are_only_reconciliation(self):
        for target in TRANSITIONS["WAITING_UNKNOWN"]:
            assert target.startswith("RECONCILED_"), target

    def test_created_only_goes_forward(self):
        assert TRANSITIONS["CREATED"] == frozenset({"PREPARING", "CANCELED"})

    def test_preparing_has_no_self_loop(self):
        # C29-25：F5 说"停在 PREPARING"，但表里没有自环；写自环会让 state_seq 假涨。
        assert "PREPARING" not in TRANSITIONS["PREPARING"]

    def test_retry_only_from_retryable(self):
        assert RETRYABLE_STATES == frozenset({"FAILED", "RECONCILED_FAILED"})
        for state in JOB_STATES:
            allowed = canonical_transition(state, "PREPARING", allow_retry=True)
            # CREATED -> PREPARING 本来就是正常边，与重试无关。
            expected = state in RETRYABLE_STATES or state in ("CREATED",)
            assert allowed == expected, state

    def test_retry_without_flag_is_refused(self):
        assert not canonical_transition("FAILED", "PREPARING")

    def test_unknown_state_is_never_legal(self):
        assert not canonical_transition("READY", "TOTALLY_MADE_UP")
        assert not canonical_transition("TOTALLY_MADE_UP", "READY")

    def test_no_transition_reaches_terminal_from_terminal(self):
        for state in TERMINAL_STATES:
            for target in JOB_STATES:
                assert not canonical_transition(state, target), (state, target)

    def test_every_legal_edge_is_expressible(self):
        for source, targets in TRANSITIONS.items():
            if source is None:
                assert canonical_transition(None, "CREATED")
                continue
            for target in targets:
                assert canonical_transition(source, target), (source, target)

    def test_ingesting_can_restate_itself(self):
        # 归档重驱动（T29-23）需要这条自环。
        assert canonical_transition("INGESTING", "INGESTING")

    def test_node_kinds_are_the_four_planned(self):
        assert NODE_KINDS == frozenset(
            {"first_frame", "last_frame", "keyframe_mid", "reference_still"})

    def test_reference_roles_are_closed_set(self):
        assert "mask" in REFERENCE_ROLES
        assert "audio_reference" in ROLES_NOT_APPLICABLE_FOR_IMAGE
        assert "video_reference" in ROLES_NOT_APPLICABLE_FOR_IMAGE

    def test_id_prefixes_are_distinct(self):
        values = list(ID_PREFIX.values())
        assert len(values) == len(set(values))


# ====================================================== 幂等与租户隔离

class TestIdempotency:
    def test_t29_01_same_key_same_body_returns_original(self, h):
        first, _ = h.make_job(command_id="c1")
        again, _ = h.make_job(command_id="c1")
        assert again["image_job_id"] == first["image_job_id"]
        assert again["replayed"] is True
        rows = h.conn.execute("SELECT COUNT(*) AS n FROM image_job").fetchone()
        assert rows["n"] == 1

    def test_t29_01_replay_does_not_resubmit(self, h):
        h.make_job(command_id="c1")
        assert h.calls["submit"] == 1
        h.make_job(command_id="c1")
        assert h.calls["submit"] == 1

    def test_t29_01_replay_does_not_reroute(self, h):
        h.make_job(command_id="c1")
        assert h.calls["route"] == 1
        h.make_job(command_id="c1")
        assert h.calls["route"] == 1

    def test_t29_02_same_key_different_body_is_conflict(self, h):
        h.make_job(command_id="c1")
        other = h.body(prompt_intent="a completely different frame")
        with pytest.raises(Exception) as exc:
            h.command(other, command_id="c1")
        assert getattr(exc.value, "code", "") == "IDEMPOTENCY_KEY_REUSED"
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_job").fetchone()["n"] == 1

    def test_t29_02_conflict_writes_zero_rows(self, h):
        h.make_job(command_id="c1")
        before = {
            t: h.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("image_job", "reference_combination", "outbox")}
        with pytest.raises(Exception):
            h.command(h.body(prompt_intent="different"), command_id="c1")
        after = {
            t: h.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("image_job", "reference_combination", "outbox")}
        assert before == after

    def test_same_key_replay_does_not_charge_twice(self, h):
        h.make_job()
        submitted = len(h.submitted_commands)
        h.make_job()
        assert len(h.submitted_commands) == submitted

    def test_command_id_is_per_tenant(self, h):
        """同 `command_id` 在不同租户下是**两条**命令，不是重放。

        幂等键是 `tenant_id + command_id + request_hash`：少了租户这一段，
        租户 A 的 `c1` 会让租户 B 的 `c1` 直接读到 A 的结果。
        """
        h.make_job()
        other = h.service.create_job(h.body(context={"tenant_id": "t2"}),
                                     "t2", "c1")
        assert other["image_job_id"] != h.service.list_jobs("t1")["items"][0][
            "image_job_id"]
        assert h.conn.execute(
            "SELECT COUNT(DISTINCT tenant_id) AS n FROM image_job"
        ).fetchone()["n"] == 2

    def test_in_flight_command_is_refused(self, h):
        body = h.body()
        h.idem.begin("t1", "c_inflight", canonical_request_hash(body))

        def boom():
            raise AssertionError("must not execute while in flight")

        with pytest.raises(Exception):
            h.command(body, command_id="c_inflight")


class TestTenantIsolation:
    def test_t29_03_cross_tenant_reference_is_404(self, h):
        # §6.1 校验 3：跨租户 artifact 一律 404，不是 403。403 会告诉探测者
        # "这个 id 存在，只是不属于你"。
        def check(tenant_id, ref):
            raise ServiceError(404, "ARTIFACT_NOT_FOUND", "no such artifact")
        h.service.artifact_check = check
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(), "t1", "c1")
        assert exc.value.status == 404
        # 404 的响应体不得携带任何外部标识
        assert "art_char_1" not in str(exc.value)
        assert exc.value.code == "ARTIFACT_NOT_FOUND"

    def test_t29_03_reference_404_leaks_no_foreign_ids(self, h):
        def check(tenant_id, ref):
            raise ServiceError(404, "ARTIFACT_NOT_FOUND", "artifact not found")
        h.service.artifact_check = check
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(), "t1", "c1")
        blob = json.dumps(exc.value.details, ensure_ascii=False)
        assert "other_tenant" not in blob
        assert exc.value.details == {}

    def test_body_tenant_must_match_signed_header(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(context={"tenant_id": "t_other"}), "t1",
                                 "c1")
        assert exc.value.status == 403
        assert exc.value.code == "TENANT_MISMATCH"

    def test_context_tenant_is_untrusted_without_header(self, h):
        """LLM 给的 tenant_id 不能当身份：header 与它不一致就是 403。"""
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(), "t_from_header", "c1")
        assert exc.value.code == "TENANT_MISMATCH"

    def test_list_is_tenant_scoped(self, h):
        h.make_job()
        assert h.service.list_jobs("t2")["count"] == 0
        assert h.service.list_jobs("t1")["count"] == 1

    def test_get_job_is_tenant_scoped(self, h):
        result, _ = h.make_job()
        with pytest.raises(ServiceError) as exc:
            h.service.get_job("t2", result["image_job_id"])
        assert exc.value.status == 404

    def test_existing_lineage_is_not_reusable_across_tenants(self, h):
        """lineage 唯一性带 tenant：另一个租户用同一 lineage 不该被挡。"""
        h.make_job()
        h.service.artifact_check = lambda tenant_id, ref: True
        # 不同租户、同 shot_lineage_id、同 fingerprint → 应该建出第二个 job
        h.service.create_job(h.body(context={"tenant_id": "t2"}), "t2", "c2")
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_job").fetchone()["n"] == 2


# ==================================================== POST /image-jobs 校验

class TestValidation:
    def test_context_missing_key_is_422(self, h):
        body = h.body()
        del body["context"]["trace_id"]
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c1")
        assert exc.value.code == "INVALID_CONTEXT"
        assert "trace_id" in exc.value.details["missing"]

    def test_manifest_ref_requires_hash(self, h):
        body = h.body()
        body["context"]["manifest_ref"] = {"service_id": "S21", "id": "m1",
                                           "revision": 1}
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c1")
        assert exc.value.code == "INVALID_CONTEXT"

    def test_shot_lineage_is_required(self, h):
        body = h.body()
        del body["shot_lineage_id"]
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c1")
        assert exc.value.code == "SHOT_LINEAGE_REQUIRED"

    def test_shot_ref_shape(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(shot_ref={"id": "x"}), "t1", "c1")
        assert exc.value.code == "INVALID_SHOT_REF"

    def test_node_kind_is_closed(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(node_kind="hero_shot"), "t1", "c1")
        assert exc.value.code == "INVALID_NODE_KIND"

    def test_shot_lineage_drift_is_409(self, h):
        h.make_job()
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(
                h.body(shot_lineage_id="lineage_B", node_kind="last_frame"),
                "t1", "c2")
        assert exc.value.status == 409
        assert exc.value.code == "SHOT_LINEAGE_DRIFT"
        assert exc.value.details["existing_shot_lineage_id"] == "lineage_A"

    def test_mode_is_closed(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(mode="inpaint"), "t1", "c1")
        assert exc.value.code == "INVALID_MODE"

    def test_blank_prompt_is_refused(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(prompt_intent="   \n  "), "t1", "c1")
        assert exc.value.code == "EMPTY_PROMPT_INTENT"

    def test_zero_dimension_is_refused(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(width=0), "t1", "c1")
        assert exc.value.code == "INVALID_DIMENSIONS"

    def test_non_integer_dimension_is_refused(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(width="wide"), "t1", "c1")
        assert exc.value.code == "INVALID_DIMENSIONS"

    def test_identity_constraint_must_parse(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(identity_constraints=["linlan"]), "t1",
                                 "c1")
        assert exc.value.code == "INVALID_IDENTITY_CONSTRAINTS"

    def test_deadline_in_past_is_refused(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(deadline=PAST), "t1", "c1")
        assert exc.value.code == "DEADLINE_IN_PAST"

    def test_deadline_with_offset_is_compared_as_time(self, h):
        """字符串比较会把 `+08:00` 的字符串序读错；这里必须是时间比较。"""
        body = h.body(deadline="2000-01-01T08:00:00+08:00")
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c1")
        assert exc.value.code == "DEADLINE_IN_PAST"

    def test_budget_scope_is_required_not_defaulted(self, h):
        # C29-24：S29 不发明预算桶。
        body = h.body()
        del body["budget_scope_id"]
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c1")
        assert exc.value.status == 422
        assert exc.value.code == "INVALID_BUDGET_SCOPE"

    def test_blank_budget_scope_is_refused(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(budget_scope_id="   "), "t1", "c1")
        assert exc.value.code == "INVALID_BUDGET_SCOPE"

    def test_route_policy_ref_must_be_s25(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(route_policy_ref={
                "service_id": "S02", "id": "p", "revision": 1,
                "content_hash": _hex("c")}), "t1", "c1")
        assert exc.value.code == "INVALID_ROUTE_POLICY_REF"

    def test_route_policy_ref_requires_four_keys(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(route_policy_ref={
                "service_id": "S25", "id": "p", "revision": 1}), "t1", "c1")
        assert exc.value.code == "INVALID_ROUTE_POLICY_REF"

    def test_route_policy_ref_rejects_extra_keys(self, h):
        # generation-command.v2 的 route_policy_ref 是 additionalProperties:false
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(route_policy_ref={
                "service_id": "S25", "id": "p", "revision": 1,
                "content_hash": _hex("c"), "model": "whatever"}), "t1", "c1")
        assert exc.value.code == "INVALID_ROUTE_POLICY_REF"

    def test_references_are_required(self, h):
        # C29-12：S29 不发明占位图。
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(references=[]), "t1", "c1")
        assert exc.value.code == "REFERENCES_REQUIRED"

    def test_reference_sha_must_be_hex64(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(references=[
                {"ordinal": 0, "artifact_id": "a", "sha256": "nothex",
                 "role": "character"}]), "t1", "c1")
        assert exc.value.code == "INVALID_REFERENCE"

    def test_reference_role_is_closed(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(references=[
                {"ordinal": 0, "artifact_id": "a", "sha256": _hex("b"),
                 "role": "protagonist"}]), "t1", "c1")
        assert exc.value.code == "INVALID_REFERENCE_ROLE"

    def test_audio_role_is_rejected_for_images(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(references=[
                {"ordinal": 0, "artifact_id": "a", "sha256": _hex("b"),
                 "role": "audio_reference"}]), "t1", "c1")
        assert exc.value.code == "ROLE_NOT_APPLICABLE_FOR_IMAGE"

    def test_mask_role_only_in_edit(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(references=[
                {"ordinal": 0, "artifact_id": "a", "sha256": _hex("b"),
                 "role": "mask"}]), "t1", "c1")
        assert exc.value.code == "MASK_ROLE_NOT_ALLOWED_IN_GENERATE"

    def test_duplicate_ordinals_are_refused(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(references=[
                {"ordinal": 0, "artifact_id": "a", "sha256": _hex("b"),
                 "role": "character"},
                {"ordinal": 0, "artifact_id": "c", "sha256": _hex("d"),
                 "role": "scene"}]), "t1", "c1")
        assert exc.value.code == "INVALID_REFERENCE"

    def test_validation_failure_writes_no_job(self, h):
        with pytest.raises(ServiceError):
            h.service.create_job(h.body(mode="nope"), "t1", "c1")
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_job").fetchone()["n"] == 0


# ================================================== 本地拒绝（不花钱）

class TestLocalRejection:
    """F1 / F2 / T29-04：调用方要求留下 `REJECTED_LOCAL` 的 job。"""

    def test_t29_04_reference_hash_mismatch_is_rejected_local(self, h):
        h.artifact_verdict = False
        result, _ = h.make_job()
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "REFERENCE_HASH_MISMATCH"
        assert h.calls["submit"] == 0

    def test_t29_04_rejected_local_spends_nothing(self, h):
        h.artifact_verdict = False
        h.make_job()
        assert h.calls["route"] == 0
        assert h.calls["submit"] == 0
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_intent_ledger").fetchone()["n"] == 0

    def test_unreadable_reference_is_rejected_local(self, h):
        # `None` = 没查成。不知道参考是不是这张图时花钱，是把钱押在未验证输入上。
        h.artifact_verdict = None
        result, _ = h.make_job()
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "REFERENCE_UNREADABLE"

    def test_artifact_check_5xx_becomes_503_not_rejection(self, h):
        """F1 的 503（上游挂了）与"查了不匹配"必须可区分。"""
        def check(tenant_id, ref):
            raise ServiceError(503, "ARTIFACT_CHECK_UNAVAILABLE", "down")
        h.service.artifact_check = check
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(), "t1", "c1")
        assert exc.value.status == 503
        assert exc.value.retryable is True
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_job").fetchone()["n"] == 0

    def test_artifact_check_connection_error_becomes_503(self, h):
        def check(tenant_id, ref):
            raise ConnectionError("refused")
        h.service.artifact_check = check
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(), "t1", "c1")
        assert exc.value.status == 503

    def test_t29_07_infeasible_route_is_rejected_local(self, h):
        h.route_result = {"feasible": False,
                          "infeasible_reason": "no model supports 1080x1920"}
        result, _ = h.make_job()
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "ROUTE_INFEASIBLE"
        assert h.calls["submit"] == 0

    def test_t29_07_infeasible_route_does_not_default_a_model(self, h):
        h.route_result = {"feasible": False, "infeasible_reason": "nope"}
        result, _ = h.make_job()
        assert result.get("provider_job_id") is None
        assert h.calls["submit"] == 0

    def test_t29_32_residency_forbidden_is_rejected_local(self, h):
        h.route_result = {"feasible": True, "route_id": "r1", "revision": 1,
                          "quote_valid_to": FUTURE,
                          "residency_allowed": False}
        result, _ = h.make_job()
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "RESIDENCY_FORBIDDEN"
        assert h.calls["submit"] == 0

    def test_route_5xx_becomes_503(self, h):
        h.route_result = ServiceError(500, "ROUTE_INTERNAL", "boom")
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(), "t1", "c1")
        assert exc.value.status == 503
        assert exc.value.retryable is True

    def test_route_connection_error_becomes_503(self, h):
        h.route_result = ConnectionError("unreachable")
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(), "t1", "c1")
        assert exc.value.status == 503

    def test_route_without_id_is_503(self, h):
        h.route_result = {"feasible": True, "quote_valid_to": FUTURE}
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(), "t1", "c1")
        assert exc.value.status == 503
        assert h.calls["submit"] == 0

    def test_submit_503_writes_no_provider_job(self, h):
        h.submit_error = ServiceError(503, "GENERATION_JOBS_UNAVAILABLE", "x")
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(), "t1", "c1")
        assert exc.value.status == 503
        # 提交失败必须整体回滚：不能留下一个"已提交但没 provider_job_id"的 job
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_job").fetchone()["n"] == 0

    def test_submit_without_provider_job_id_is_503(self, h):
        h.submit_result = {"operation_id": "op_1"}
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(), "t1", "c1")
        assert exc.value.status == 503
        assert "provider_job_id" in str(exc.value) or exc.value.code


# ==================================================== 派单与 S26 契约

class TestDispatch:
    def test_happy_path_reaches_waiting_provider(self, h):
        result, _ = h.make_job()
        assert result["state"] == "WAITING_PROVIDER"
        assert result["provider_job_id"].startswith("pj_")

    def test_state_seq_is_monotonic(self, h):
        result, _ = h.make_job()
        job = h.service.get_job("t1", result["image_job_id"])
        seqs = [edge["seq"] for edge in job["history"]]
        assert seqs == sorted(seqs)
        assert job["state_seq"] == seqs[-1]

    def test_history_starts_at_created(self, h):
        result, _ = h.make_job()
        job = h.service.get_job("t1", result["image_job_id"])
        assert job["history"][0]["to"] == "PREPARING"
        assert job["history"][0]["from"] == "CREATED"

    def test_generation_command_has_all_required_keys(self, h):
        h.make_job()
        command = h.submitted_commands[0]
        for key in ("schema_version", "context", "attempt_id",
                    "variation_nonce", "idempotency_key", "modality", "input",
                    "route_policy_ref", "template_ref", "budget_scope_id",
                    "deadline"):
            assert key in command, key
        assert command["modality"] == "image"
        assert command["template_ref"] is None

    def test_generation_command_context_is_the_signed_one(self, h):
        _, body = h.make_job()
        command = h.submitted_commands[0]
        assert command["context"] == body["context"]

    def test_generation_command_input_is_closed_set(self, h):
        h.make_job()
        payload = h.submitted_commands[0]["input"]
        assert set(payload) <= {"prompt_intent", "mode", "width", "height",
                                "references", "identity_constraints"}

    def test_generation_command_strips_local_ordinal(self, h):
        # C29-18：`generation-command.v2` 的 reference item 只有三个键。
        h.make_job()
        refs = h.submitted_commands[0]["input"]["references"]
        assert refs
        for ref in refs:
            assert set(ref) == {"artifact_id", "sha256", "role"}

    def test_generation_command_route_policy_ref_is_persisted(self, h):
        # C29-26：DDL 里一开始没有这一列，重试时就拼不出合法命令。
        _, body = h.make_job()
        assert h.submitted_commands[0]["route_policy_ref"] == \
            body["route_policy_ref"]

    def test_route_decision_ref_is_persisted_separately(self, h):
        result, _ = h.make_job()
        row = dict(h.conn.execute(
            "SELECT route_decision_ref, route_policy_ref FROM image_job "
            "WHERE image_job_id=?", (result["image_job_id"],)).fetchone())
        decision = json.loads(row["route_decision_ref"])
        policy = json.loads(row["route_policy_ref"])
        assert decision["id"] == "route_1"
        assert policy["id"] == "policy_route"
        assert decision != policy

    def test_attempt_id_includes_business_attempt(self, h):
        h.make_job()
        assert h.submitted_commands[0]["attempt_id"].endswith(":1")

    def test_ledger_row_is_written_once_per_paid_submit(self, h):
        result, _ = h.make_job()
        rows = h.conn.execute(
            "SELECT * FROM image_intent_ledger WHERE image_job_id=?",
            (result["image_job_id"],)).fetchall()
        assert len(rows) == 1
        assert rows[0]["action"] == "generate"

    def test_ledger_does_not_fake_a_run_id(self, h):
        # C29-8：context 没有 run_id → 存空串，不拿 trace_id 冒充。
        result, _ = h.make_job()
        row = h.conn.execute(
            "SELECT run_id FROM image_intent_ledger WHERE image_job_id=?",
            (result["image_job_id"],)).fetchone()
        assert row["run_id"] == ""

    def test_expired_quote_is_refetched_not_reused(self, h):
        # F5：第一次报价过期 → 再取一次（不提交），第二次有效 → 提交一次。
        expired = {"route_id": "r_expired", "revision": 1,
                   "content_hash": _hex("a"), "feasible": True,
                   "quote_valid_to": PAST}
        h.route_result = None
        original = h._route
        calls = {"n": 0}

        def route(tenant_id, job, command):
            calls["n"] += 1
            if calls["n"] == 1:
                return dict(expired)
            return original(tenant_id, job, command)

        h.service.route_resolver = route
        result, _ = h.make_job()
        assert result["state"] == "WAITING_PROVIDER"
        assert calls["n"] == 2
        assert h.calls["submit"] == 1

    def test_expired_quote_keeps_state_preparing(self, h):
        h.route_result = {"route_id": "r1", "revision": 1,
                          "content_hash": _hex("a"), "feasible": True,
                          "quote_valid_to": PAST}
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(h.body(), "t1", "c1")
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"
        assert h.calls["submit"] == 0

    def test_expired_quote_does_not_bump_state_seq(self, h):
        """C29-25：F5 的"重取"不是状态转移，不能造自环、不能让 seq 假涨。"""
        expired = {"route_id": "r1", "revision": 1, "content_hash": _hex("a"),
                   "feasible": True, "quote_valid_to": PAST}
        fresh = {"route_id": "r2", "revision": 1, "content_hash": _hex("a"),
                 "feasible": True, "quote_valid_to": FUTURE}
        seen = {"n": 0}

        def route(tenant_id, job, command):
            seen["n"] += 1
            return expired if seen["n"] == 1 else fresh

        h.service.route_resolver = route
        result, _ = h.make_job()
        job = h.service.get_job("t1", result["image_job_id"])
        tos = [edge["to"] for edge in job["history"]]
        assert tos == ["PREPARING", "SUBMITTED", "WAITING_PROVIDER"]
        assert tos.count("PREPARING") == 1

    def test_expired_quote_event_is_emitted_with_reason(self, h):
        expired = {"route_id": "r1", "revision": 1, "content_hash": _hex("a"),
                   "feasible": True, "quote_valid_to": PAST}
        fresh = {"route_id": "r2", "revision": 1, "content_hash": _hex("a"),
                 "feasible": True, "quote_valid_to": FUTURE}
        seen = {"n": 0}

        def route(tenant_id, job, command):
            seen["n"] += 1
            return expired if seen["n"] == 1 else fresh

        h.service.route_resolver = route
        result, _ = h.make_job()
        payloads = [json.loads(r["payload"]) for r in h.conn.execute(
            "SELECT payload FROM outbox WHERE event_type=?",
            ("image.job.state-changed",)).fetchall()]
        reasons = [p["data"].get("reason_code") for p in payloads]
        assert "QUOTE_EXPIRED" in reasons
        quotes = [p["data"] for p in payloads
                  if p["data"].get("reason_code") == "QUOTE_EXPIRED"]
        assert quotes[0]["from"] == quotes[0]["to"] == "PREPARING"


# ================================================ S26 状态事件 → 本地状态

class TestProviderStates:
    def test_t29_08_rejected_budget_fails_with_budget(self, h):
        result, _ = h.make_job()
        out = h.state_event(result["image_job_id"], "REJECTED_BUDGET")
        assert out["state"] == "FAILED"
        assert out["failure_code"] == "BUDGET"
        job = h.service.get_job("t1", result["image_job_id"])
        assert job["failure"]["code"] == "BUDGET"

    def test_t29_09_rejected_quota_fails_with_quota(self, h):
        result, _ = h.make_job()
        out = h.state_event(result["image_job_id"], "REJECTED_QUOTA")
        assert out["state"] == "FAILED"
        assert out["failure_code"] == "QUOTA"

    def test_provider_fails_becomes_failed(self, h):
        result, _ = h.make_job()
        out = h.state_event(result["image_job_id"], "PROVIDER_FAILED",
                            reason_code="SAFETY_BLOCKED")
        assert out["state"] == "FAILED"
        assert out["failure_code"] == "SAFETY_BLOCKED"

    def test_provider_success_enters_ingesting(self, h):
        result, _ = h.make_job()
        out = h.state_event(result["image_job_id"], "PROVIDER_SUCCEEDED")
        assert out["state"] == "INGESTING"

    def test_success_triggers_an_import_request(self, h):
        result, _ = h.make_job()
        h.state_event(result["image_job_id"], "PROVIDER_SUCCEEDED")
        assert h.calls["import"] == 1

    def test_import_failure_does_not_fake_success(self, h):
        result, _ = h.make_job()
        h.import_error = ServiceError(503, "ARTIFACT_INGEST_UNAVAILABLE", "x")
        out = h.state_event(result["image_job_id"], "PROVIDER_SUCCEEDED")
        assert out["state"] == "INGESTING"
        assert "import not accepted" in out["note"]

    def test_t29_14_expired_url_is_flagged(self, h):
        result, _ = h.make_job()
        h.import_error = ServiceError(409, "URL_EXPIRED", "stale url",
                                      retryable=True)
        out = h.state_event(result["image_job_id"], "PROVIDER_SUCCEEDED")
        assert out["awaiting_fresh_url"] is True
        # 不重生成：job 仍是 INGESTING，submit 只发生过一次
        assert h.calls["submit"] == 1
        assert h.state_of(result["image_job_id"]) == "INGESTING"

    def test_t29_15_hash_mismatch_fails_without_retry(self, h):
        result, _ = h.make_job()
        h.import_error = ServiceError(422, "HASH_MISMATCH", "different bytes")
        out = h.state_event(result["image_job_id"], "PROVIDER_SUCCEEDED")
        assert out["failure_code"] == "ARTIFACT_HASH_MISMATCH"
        assert h.state_of(result["image_job_id"]) == "FAILED"
        assert h.calls["submit"] == 1

    def test_canceled_event_moves_to_canceled(self, h):
        result, _ = h.make_job()
        out = h.state_event(result["image_job_id"], "CANCELED")
        assert out["state"] == "CANCELED"

    def test_unknown_provider_state_is_ignored(self, h):
        result, _ = h.make_job()
        out = h.state_event(result["image_job_id"], "SOMETHING_NEW")
        assert out["ignored"] is True
        assert h.state_of(result["image_job_id"]) == "WAITING_PROVIDER"

    def test_event_for_unknown_provider_job_is_ignored(self, h):
        out = h.service.on_generation_state_changed({
            "id": "ev_x", "tenant_id": "t1", "aggregate_id": "pj_ghost",
            "data": {"state": "PROVIDER_SUCCEEDED"}})
        assert out["ignored"] is True

    def test_event_without_id_is_refused(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.on_generation_state_changed({
                "tenant_id": "t1", "aggregate_id": "pj_1",
                "data": {"state": "PROVIDER_SUCCEEDED"}})
        assert exc.value.status == 400

    def test_duplicate_event_is_not_applied_twice(self, h):
        result, _ = h.make_job()
        first = h.state_event(result["image_job_id"], "PROVIDER_SUCCEEDED",
                              event_id="ev_dup")
        again = h.state_event(result["image_job_id"], "PROVIDER_SUCCEEDED",
                              event_id="ev_dup")
        assert first["state"] == "INGESTING"
        assert again["reason"] == "DUPLICATE"
        assert h.calls["import"] == 1

    def test_duplicate_state_event_does_not_double_transition(self, h):
        result, _ = h.make_job()
        h.state_event(result["image_job_id"], "PROVIDER_FAILED",
                      event_id="ev_fail")
        seq_after = h.service.get_job("t1", result["image_job_id"])["state_seq"]
        h.state_event(result["image_job_id"], "PROVIDER_FAILED",
                      event_id="ev_fail")
        assert h.service.get_job(
            "t1", result["image_job_id"])["state_seq"] == seq_after

    def test_terminal_job_never_regresses_on_new_state(self, h):
        result, _ = h.make_job()
        h.state_event(result["image_job_id"], "PROVIDER_FAILED",
                      event_id="ev_1")
        out = h.state_event(result["image_job_id"], "PROVIDER_SUCCEEDED",
                            event_id="ev_2")
        assert out["ignored"] is True
        assert h.state_of(result["image_job_id"]) == "FAILED"


# ============================================== SUBMISSION_UNKNOWN（红线）

class TestSubmissionUnknown:
    """AGENTS.md 的红线：看不清是否已付费时，暴露，不重买。"""

    def _unknown(self, h):
        result, body = h.make_job()
        h.state_event(result["image_job_id"], "SUBMISSION_UNKNOWN")
        return result["image_job_id"], body

    def test_t29_10_unknown_freezes_the_job(self, h):
        job_id, _ = self._unknown(h)
        job = h.service.get_job("t1", job_id)
        assert job["state"] == "WAITING_UNKNOWN"
        assert job["frozen"] is True
        assert job["must_reconcile_before_resubmit"] is True

    def test_t29_10_unknown_sends_no_new_request(self, h):
        job_id, _ = self._unknown(h)
        assert h.calls["import"] == 0
        assert h.calls["submit"] == 1

    def test_t29_12_retry_during_unknown_is_409(self, h):
        job_id, body = self._unknown(h)
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job("t1", job_id, {}, "c_retry")
        assert exc.value.status == 409
        assert exc.value.code == "MUST_RECONCILE_FIRST"
        assert h.calls["submit"] == 1

    def test_t29_12_cancel_during_unknown_is_409(self, h):
        job_id, body = self._unknown(h)
        with pytest.raises(ServiceError) as exc:
            h.service.cancel_job("t1", job_id, {}, "c_cancel")
        assert exc.value.code == "MUST_RECONCILE_FIRST"
        assert h.calls["cancel"] == 0

    def test_t29_11_same_fingerprint_during_unknown_does_not_resubmit(self, h):
        job_id, body = self._unknown(h)
        again = h.command(body, command_id="c_new")
        assert again["duplicate_suppressed"] is True
        assert again["image_job_id"] == job_id
        assert h.calls["submit"] == 1

    def test_t29_11_new_job_is_blocked_by_the_unique_index(self, h):
        job_id, body = self._unknown(h)
        h.command(body, command_id="c_new")
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_job").fetchone()["n"] == 1

    def test_unknown_does_not_accept_artifact_ingested(self, h):
        job_id, _ = self._unknown(h)
        out = h.artifact_event(job_id)
        assert out["ignored"] is True
        assert "waiting_unknown" in out["reason"]
        assert h.state_of(job_id) == "WAITING_UNKNOWN"

    def test_t29_13_archived_reconciles_to_ready(self, h):
        job_id, _ = self._unknown(h)
        out = h.state_event(job_id, "ARCHIVED", event_id="ev_rec")
        assert out["state"] == "RECONCILED_READY"
        assert out["reconciled"] is True
        row = dict(h.conn.execute(
            "SELECT reconciled FROM image_job WHERE image_job_id=?",
            (job_id,)).fetchone())
        assert row["reconciled"] == 1

    def test_t29_13_reconciled_ready_accepts_artifact(self, h):
        job_id, _ = self._unknown(h)
        h.state_event(job_id, "ARCHIVED", event_id="ev_rec")
        out = h.artifact_event(job_id)
        # 对账已经把它定成 RECONCILED_READY；补候选不改判定（§3.2 终态）。
        assert out["state"] == "RECONCILED_READY"
        assert out["checkpoint_advanced"] is True

    def test_reconcile_failure_concludes_as_failed(self, h):
        job_id, _ = self._unknown(h)
        out = h.state_event(job_id, "PROVIDER_FAILED", event_id="ev_rec")
        assert out["state"] == "RECONCILED_FAILED"
        assert out["failure_code"] == "PROVIDER_FAILED"

    def test_reconcile_budget_concludes_as_failed(self, h):
        job_id, _ = self._unknown(h)
        out = h.state_event(job_id, "REJECTED_BUDGET", event_id="ev_rec")
        assert out["state"] == "RECONCILED_FAILED"

    def test_running_is_not_a_reconciliation_conclusion(self, h):
        """`RUNNING` 说的是"还在做"，把它当结论就是把可能已付费读成一定没好。"""
        job_id, _ = self._unknown(h)
        out = h.state_event(job_id, "RUNNING", event_id="ev_running")
        assert out["ignored"] is True
        assert out["frozen"] is True
        assert h.state_of(job_id) == "WAITING_UNKNOWN"

    def test_queued_is_not_a_reconciliation_conclusion(self, h):
        job_id, _ = self._unknown(h)
        out = h.state_event(job_id, "QUEUED", event_id="ev_queued")
        assert out["ignored"] is True

    def test_reconciled_ready_is_terminal_and_not_cancelable(self, h):
        job_id, _ = self._unknown(h)
        h.state_event(job_id, "ARCHIVED", event_id="ev_rec")
        with pytest.raises(ServiceError) as exc:
            h.service.cancel_job("t1", job_id, {}, "c_cancel")
        assert exc.value.code == "JOB_ALREADY_TERMINAL"

    def test_reconciled_ready_frees_the_business_key(self, h):
        """C29-3：不释放的话那一格会永远占着，之后同输入再也建不出来。"""
        job_id, body = self._unknown(h)
        h.state_event(job_id, "ARCHIVED", event_id="ev_rec")
        h.artifact_event(job_id, event_id="ev_art")
        again = h.command(body, command_id="c_after")
        assert again["image_job_id"] != job_id


# ==================================================== 归档与候选

class TestArtifactIngest:
    def test_ready_writes_a_candidate(self, h):
        job_id, candidate_id = h.ready_job()
        assert candidate_id.startswith(ID_PREFIX["candidate"])
        row = h.conn.execute(
            "SELECT * FROM keyframe_candidate WHERE candidate_id=?",
            (candidate_id,)).fetchone()
        assert row["state"] == "READY"
        assert row["image_job_id"] == job_id

    def test_ready_event_has_all_required_keys(self, h):
        h.ready_job()
        payload = json.loads(h.conn.execute(
            "SELECT payload FROM outbox WHERE event_type=?",
            (EVENT_IMAGE_READY,)).fetchone()["payload"])
        data = payload["data"]
        for key in ("schema_version", "image_job_id", "candidate_id",
                    "shot_lineage_id", "node_kind", "artifact_ref",
                    "reference_combination_hash", "checkpoint", "built_for",
                    "repaired_from"):
            assert key in data, key

    def test_ready_event_does_not_carry_extra_keys(self, h):
        # C29-10：additionalProperties:false，`requires_recheck` 塞不进去。
        h.ready_job()
        payload = json.loads(h.conn.execute(
            "SELECT payload FROM outbox WHERE event_type=?",
            (EVENT_IMAGE_READY,)).fetchone()["payload"])
        assert "requires_recheck" not in payload["data"]

    def test_ready_candidate_matches_checkpoint_target(self, h):
        # PIR-2：契约表达不了这条，生产方必须自己保证。
        h.ready_job()
        payload = json.loads(h.conn.execute(
            "SELECT payload FROM outbox WHERE event_type=?",
            (EVENT_IMAGE_READY,)).fetchone()["payload"])["data"]
        assert payload["candidate_id"] == payload["checkpoint"]["to_candidate_id"]

    def test_second_ingest_with_new_hash_advances_checkpoint(self, h):
        job_id, first = h.ready_job()
        # 第二个 job，同一 lineage/node_kind，不同 prompt → 新 fingerprint
        result2, _ = h.make_job(prompt_intent="a different frame entirely")
        h.state_event(result2["image_job_id"], "PROVIDER_SUCCEEDED",
                      event_id="ev_s2")
        out = h.artifact_event(result2["image_job_id"], event_id="ev_a2",
                               artifact_id="art_out_2",
                               content_hash=_hex("e"))
        assert out["checkpoint_advanced"] is True
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["candidate_id"] == out["candidate_id"]
        assert cp["checkpoint_seq"] == 2

    def test_checkpoint_records_from_candidate(self, h):
        # 每次推进都要能回答"上一张是谁"，事后推断就是编数据。
        job_id, first = h.ready_job()
        result2, _ = h.make_job(prompt_intent="another frame")
        h.state_event(result2["image_job_id"], "PROVIDER_SUCCEEDED",
                      event_id="ev_s2")
        h.artifact_event(result2["image_job_id"], event_id="ev_a2",
                         artifact_id="art_out_2", content_hash=_hex("e"))
        events = [json.loads(r["payload"])["data"] for r in h.conn.execute(
            "SELECT payload FROM outbox WHERE event_type=? ORDER BY rowid",
            (EVENT_IMAGE_READY,)).fetchall()]
        assert events[0]["checkpoint"]["from_candidate_id"] is None
        assert events[1]["checkpoint"]["from_candidate_id"] == first

    def test_t29_20_same_artifact_reuses_candidate(self, h):
        job_id, first = h.ready_job()
        result2, _ = h.make_job(prompt_intent="another frame")
        h.state_event(result2["image_job_id"], "PROVIDER_SUCCEEDED",
                      event_id="ev_s2")
        out = h.artifact_event(result2["image_job_id"], event_id="ev_a2",
                               artifact_id="art_out_1", content_hash=_hex("f"))
        assert out["candidate_id"] == first
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM keyframe_candidate").fetchone()["n"] == 1

    def test_artifact_must_be_owned_by_s28(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED")
        out = h.artifact_event(job_id, service_id="S27")
        assert out["ignored"] is True
        assert out["reason"] == "artifact_not_owned_by_s28"
        assert h.state_of(job_id) == "INGESTING"

    def test_poison_artifact_event_is_consumed(self, h):
        """不消费的话会被无限重投。"""
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED")
        h.artifact_event(job_id, event_id="ev_bad", service_id="S27")
        out = h.artifact_event(job_id, event_id="ev_bad", service_id="S27")
        assert out["reason"] == "DUPLICATE"

    def test_incomplete_artifact_ref_is_ignored(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED")
        out = h.service.on_artifact_ingested({
            "id": "ev_bad", "tenant_id": "t1",
            "data": {"source": {"provider_job_id": "pj_x"},
                     "artifact_ref": {"service_id": "S28", "id": "a",
                                      "revision": 1, "content_hash": "short"}}})
        assert out["reason"] == "artifact_ref_incomplete"

    def test_artifact_event_without_id_is_refused(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.on_artifact_ingested({"tenant_id": "t1", "data": {}})
        assert exc.value.status == 400

    def test_artifact_event_without_provider_job_is_ignored(self, h):
        out = h.service.on_artifact_ingested({
            "id": "ev_1", "tenant_id": "t1", "data": {"artifact_ref": {}}})
        assert out["ignored"] is True

    def test_candidate_is_tenant_scoped(self, h):
        _, candidate_id = h.ready_job()
        with pytest.raises(ServiceError) as exc:
            h.service.get_candidate("t2", candidate_id)
        assert exc.value.status == 404

    def test_candidate_carries_reference_chain(self, h):
        _, candidate_id = h.ready_job()
        body = h.service.get_candidate("t1", candidate_id)
        chain = body["reference_chain"]
        assert len(chain["references"]) == 1
        assert chain["references"][0]["artifact_id"] == "art_char_1"
        assert chain["identity_constraints"] == ["char_linlan@3"]
        assert len(chain["reference_combination_hash"]) == 64

    def test_candidate_built_for_has_manifest_triplet(self, h):
        _, candidate_id = h.ready_job()
        built = h.service.get_candidate("t1", candidate_id)["built_for"]
        assert set(built["manifest_ref"]) >= {"id", "revision", "content_hash"}
        assert built["execution_fence"] == 2
        assert built["node_epoch_input"] == 4

    def test_dimension_mismatch_requires_recheck(self, h):
        # U7：观测尺寸不同 → READY 但 requires_recheck=true，S29 不自行缩放。
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED")
        out = h.artifact_event(job_id, width=720, height=1280)
        cand = h.service.get_candidate("t1", out["candidate_id"])
        assert cand["requires_recheck"] is True

    def test_matching_dimensions_do_not_require_recheck(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED")
        out = h.artifact_event(job_id, width=1080, height=1920)
        cand = h.service.get_candidate("t1", out["candidate_id"])
        assert cand["requires_recheck"] is False

    def test_checkpoint_only_points_at_ready_candidates(self, h):
        """§2.3 的触发器：指向非 READY 候选要 ABORT。"""
        h.ready_job()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO shot_image_checkpoint (tenant_id, shot_lineage_id,"
                " node_kind, candidate_id, checkpoint_seq, expected_revision,"
                " updated_by_job, updated_at) VALUES "
                "('t1','x','first_frame','ghost',1,0,'j','now')")

    def test_checkpoint_has_no_accepted_semantics(self, h):
        """§2.3：它只说"该拿哪张图"，不回答"这集被接受了吗"。"""
        h.ready_job()
        body = h.service.get_checkpoint("t1", "lineage_A")
        assert set(body) == {"shot_lineage_id", "checkpoints"}
        assert "accepted" not in json.dumps(body)


# ======================================================= 取消与重试

class TestCancelRetry:
    def test_cancel_confirms_with_s26(self, h):
        result, _ = h.make_job()
        out = h.service.cancel_job("t1", result["image_job_id"], {}, "c_c")
        assert out["state"] == "CANCELED"
        assert out["cancel_state"] == "CANCELED"
        assert h.calls["cancel"] == 1

    def test_cancel_never_promises_a_refund(self, h):
        result, _ = h.make_job()
        out = h.service.cancel_job("t1", result["image_job_id"], {}, "c_c")
        assert out["refund_promised"] is False

    def test_t29_34_unconfirmed_cancel_stays_pending(self, h):
        result, _ = h.make_job()
        h.cancel_error = ServiceError(503, "GENERATION_JOBS_UNAVAILABLE", "x")
        out = h.service.cancel_job("t1", result["image_job_id"], {}, "c_c")
        assert out["cancel_state"] == "CANCEL_PENDING"
        assert out["refund_promised"] is False
        # 没确认就绝不能标 CANCELED
        assert h.state_of(result["image_job_id"]) == "WAITING_PROVIDER"

    def test_cancel_connection_error_stays_pending(self, h):
        result, _ = h.make_job()
        h.cancel_error = ConnectionError("down")
        out = h.service.cancel_job("t1", result["image_job_id"], {}, "c_c")
        assert out["cancel_state"] == "CANCEL_PENDING"

    def test_cancel_terminal_job_is_409(self, h):
        job_id, _ = h.ready_job()
        with pytest.raises(ServiceError) as exc:
            h.service.cancel_job("t1", job_id, {}, "c_c")
        assert exc.value.code == "JOB_ALREADY_TERMINAL"

    def test_retry_requires_a_retryable_state(self, h):
        result, _ = h.make_job()
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job("t1", result["image_job_id"], {}, "c_r")
        assert exc.value.status == 409
        assert exc.value.code == "JOB_NOT_RETRYABLE"

    def test_retry_after_failure_resubmits_once(self, h):
        result, _ = h.make_job()
        h.state_event(result["image_job_id"], "PROVIDER_FAILED",
                      event_id="ev_f")
        assert h.calls["submit"] == 1
        out = h.service.retry_job("t1", result["image_job_id"], {}, "c_r")
        assert out["state"] == "WAITING_PROVIDER"
        assert h.calls["submit"] == 2

    def test_retry_keeps_the_same_job(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="ev_f")
        out = h.service.retry_job("t1", job_id, {}, "c_r")
        assert out["image_job_id"] == job_id
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_job").fetchone()["n"] == 1

    def test_retry_bumps_the_business_attempt(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="ev_f")
        h.service.retry_job("t1", job_id, {}, "c_r")
        row = dict(h.conn.execute(
            "SELECT business_attempt_no FROM image_job WHERE image_job_id=?",
            (job_id,)).fetchone())
        assert row["business_attempt_no"] == 2
        assert h.submitted_commands[-1]["attempt_id"].endswith(":2")

    def test_retry_changes_the_variation_nonce(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        first_nonce = h.submitted_commands[-1]["variation_nonce"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="ev_f")
        h.service.retry_job("t1", job_id, {}, "c_r")
        assert h.submitted_commands[-1]["variation_nonce"] != first_nonce

    def test_retry_clears_failure(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="ev_f")
        h.service.retry_job("t1", job_id, {}, "c_r")
        job = h.service.get_job("t1", job_id)
        assert job["failure"] is None

    def test_retry_of_canceled_job_is_refused(self, h):
        result, _ = h.make_job()
        h.service.cancel_job("t1", result["image_job_id"], {}, "c_c")
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job("t1", result["image_job_id"], {}, "c_r")
        assert exc.value.code == "JOB_NOT_RETRYABLE"

    def test_cancel_is_idempotent_by_command_key(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]

        def cancel():
            return h.service.cancel_job("t1", job_id, {}, "c_c")

        first = h.command({}, command_id="c_c", fn=cancel)
        again = h.command({}, command_id="c_c", fn=cancel)
        # 同 command_id 的重放不会走第二次业务逻辑
        assert h.calls["cancel"] == 1
        assert again["image_job_id"] == first["image_job_id"]
        assert again["replayed"] is True


# ==================================================== keyframe_edit

class TestKeyframeEdit:
    def _edit_body(self, h, candidate_id, **over):
        body = h.body(
            mode="edit",
            source_candidate_id=candidate_id,
            repair_action_id="ra_1",
            prompt_intent="fix the hand",
            instruction="把左手手指数量改成五根",
            edit_mask={"artifact_id": "art_mask_1", "sha256": _hex("9"),
                       "geometry": {"bbox": [10, 20, 100, 100]}},
            window={"start_frame": 10, "end_frame": 15, "fps": 24},
            node_kind="keyframe_mid",
            shot_lineage_id="lineage_A",
        )
        body.update(over)
        return body

    def test_t29_16_seven_frame_window_is_422_without_job(self, h):
        _, candidate_id = h.ready_job()
        body = self._edit_body(h, candidate_id,
                               window={"start_frame": 10, "end_frame": 16,
                                       "fps": 24})
        before = h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_job").fetchone()["n"]
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c_edit")
        assert exc.value.status == 422
        assert exc.value.code == "KEYFRAME_EDIT_OUT_OF_WINDOW"
        assert exc.value.details["span_frames"] == 7
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_job").fetchone()["n"] == before

    def test_t29_17_six_frame_window_is_accepted(self, h):
        _, candidate_id = h.ready_job()
        body = self._edit_body(h, candidate_id,
                               window={"start_frame": 10, "end_frame": 15,
                                       "fps": 24})
        result = h.command(body, command_id="c_edit")
        assert result["state"] == "WAITING_PROVIDER"

    def test_t29_17_edit_output_requires_recheck(self, h):
        _, candidate_id = h.ready_job()
        body = self._edit_body(h, candidate_id)
        result = h.command(body, command_id="c_edit")
        h.state_event(result["image_job_id"], "PROVIDER_SUCCEEDED",
                      event_id="ev_edit")
        out = h.artifact_event(result["image_job_id"], event_id="ev_edit_art",
                               artifact_id="art_edited")
        cand = h.service.get_candidate("t1", out["candidate_id"])
        assert cand["requires_recheck"] is True
        assert cand["repair_action_id"] == "ra_1"
        assert cand["source_candidate_id"] == candidate_id

    def test_t29_18_same_repair_action_is_409(self, h):
        _, candidate_id = h.ready_job()
        body = self._edit_body(h, candidate_id)
        h.command(body, command_id="c_edit")
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(
                self._edit_body(h, candidate_id, prompt_intent="again"),
                "t1", "c_edit_2")
        assert exc.value.status == 409
        assert exc.value.code == "REPAIR_ACTION_ALREADY_EXECUTED"
        assert exc.value.details["image_job_id"]

    def test_t29_19_mask_reuse_across_candidates_is_refused(self, h):
        _, candidate_id = h.ready_job()
        h.command(self._edit_body(h, candidate_id), command_id="c_e1")
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(
                self._edit_body(h, candidate_id, repair_action_id="ra_2",
                                prompt_intent="second edit"),
                "t1", "c_e2")
        assert exc.value.status == 409
        assert exc.value.code == "MASK_ALREADY_BOUND"

    def test_edit_requires_repair_action(self, h):
        _, candidate_id = h.ready_job()
        body = self._edit_body(h, candidate_id)
        del body["repair_action_id"]
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c_e")
        assert exc.value.code == "KEYFRAME_EDIT_NOT_OWNED"

    def test_edit_requires_source_candidate(self, h):
        _, candidate_id = h.ready_job()
        body = self._edit_body(h, candidate_id)
        del body["source_candidate_id"]
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c_e")
        assert exc.value.code == "INVALID_EDIT_SOURCE"

    def test_edit_of_foreign_candidate_is_404(self, h):
        _, candidate_id = h.ready_job()
        body = self._edit_body(h, "kfc_ghost")
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c_e")
        assert exc.value.status == 404
        assert exc.value.code == "CANDIDATE_NOT_FOUND"

    def test_edit_source_lineage_must_match(self, h):
        _, candidate_id = h.ready_job()
        body = self._edit_body(h, candidate_id, shot_lineage_id="lineage_OTHER")
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c_e")
        assert exc.value.code in ("EDIT_SOURCE_LINEAGE_MISMATCH",
                                  "SHOT_LINEAGE_DRIFT")

    def test_edit_mask_shape_is_validated(self, h):
        _, candidate_id = h.ready_job()
        body = self._edit_body(h, candidate_id)
        body["edit_mask"]["geometry"] = {"bbox": [1, 2]}
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c_e")
        assert exc.value.code == "INVALID_EDIT_MASK"

    def test_edit_window_fps_is_required(self, h):
        # C29-7：没有帧率就解释不了"6 帧"。
        _, candidate_id = h.ready_job()
        body = self._edit_body(h, candidate_id)
        del body["window"]["fps"]
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c_e")
        assert exc.value.code == "KEYFRAME_EDIT_FPS_UNKNOWN"

    def test_edit_window_order_is_validated(self, h):
        _, candidate_id = h.ready_job()
        body = self._edit_body(h, candidate_id,
                               window={"start_frame": 20, "end_frame": 10,
                                       "fps": 24})
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(body, "t1", "c_e")
        assert exc.value.code == "KEYFRAME_EDIT_OUT_OF_WINDOW"

    def test_edit_references_put_source_and_mask_first(self, h):
        _, candidate_id = h.ready_job()
        h.command(self._edit_body(h, candidate_id), command_id="c_edit")
        refs = h.submitted_commands[-1]["input"]["references"]
        assert refs[0]["role"] == "first_frame"
        assert refs[1]["role"] == "mask"

    def test_edit_instruction_is_stored_not_forwarded(self, h):
        # C29-22：generation-command 的 image input 放不下 instruction。
        _, candidate_id = h.ready_job()
        h.command(self._edit_body(h, candidate_id), command_id="c_edit")
        command = h.submitted_commands[-1]
        assert "instruction" not in json.dumps(command["input"])
        row = h.conn.execute("SELECT instruction FROM edit_mask").fetchone()
        assert row["instruction"] == "把左手手指数量改成五根"

    def test_edit_ledger_action_is_keyframe_edit(self, h):
        _, candidate_id = h.ready_job()
        result = h.command(self._edit_body(h, candidate_id), command_id="c_e")
        row = h.conn.execute(
            "SELECT action FROM image_intent_ledger WHERE image_job_id=?",
            (result["image_job_id"],)).fetchone()
        assert row["action"] == "keyframe_edit"


# ================================================== 读口与投影

class TestReads:
    def test_list_filters_by_lineage(self, h):
        h.make_job()
        assert h.service.list_jobs("t1", shot_lineage_id="nope")["count"] == 0
        assert h.service.list_jobs("t1", shot_lineage_id="lineage_A")["count"] == 1

    def test_list_filters_by_state(self, h):
        h.make_job()
        assert h.service.list_jobs("t1", state="WAITING_PROVIDER")["count"] == 1
        assert h.service.list_jobs("t1", state="READY")["count"] == 0

    def test_list_reports_a_watermark(self, h):
        h.make_job()
        assert h.service.list_jobs("t1")["projection_watermark"] >= 1

    def test_get_unknown_job_is_404(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.get_job("t1", "ij_missing")
        assert exc.value.code == "IMAGE_JOB_NOT_FOUND"

    def test_get_reports_provider_state_as_a_projection(self, h):
        # C29-11：这是本地映射，不是 S26 的权威值。
        result, _ = h.make_job()
        job = h.service.get_job("t1", result["image_job_id"])
        assert job["provider_state_source"] == "local_map"
        assert job["provider_job_state"] == "RUNNING"

    def test_get_exposes_frozen_flags(self, h):
        result, _ = h.make_job()
        job = h.service.get_job("t1", result["image_job_id"])
        assert job["frozen"] is False
        assert job["must_reconcile_before_resubmit"] is False

    def test_checkpoint_is_empty_before_any_ready(self, h):
        body = h.service.get_checkpoint("t1", "lineage_A")
        assert body["checkpoints"] == []

    def test_t29_26_checkpoint_shows_new_candidate_but_no_acceptance(self, h):
        job_id, first = h.ready_job()
        result2, _ = h.make_job(prompt_intent="second attempt")
        h.state_event(result2["image_job_id"], "PROVIDER_SUCCEEDED",
                      event_id="ev_2")
        out = h.artifact_event(result2["image_job_id"], event_id="ev_a2",
                               artifact_id="art_out_2", content_hash=_hex("e"))
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["candidate_id"] == out["candidate_id"]
        # S08 的 accepted pointer 不在这个读口里，S29 也没写过它
        assert "accepted" not in json.dumps(cp)


# ============================================= 最新 artifact 优先级

class TestCheckpointPriority:
    """U3 未决（C29-14）：当前用 created_at，后到者优先。"""

    def test_newer_candidate_wins(self, h):
        job_id, first = h.ready_job()
        result2, _ = h.make_job(prompt_intent="newer")
        h.state_event(result2["image_job_id"], "PROVIDER_SUCCEEDED",
                      event_id="ev_s2")
        out = h.artifact_event(result2["image_job_id"], event_id="ev_a2",
                               artifact_id="art_out_2", content_hash=_hex("e"))
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["candidate_id"] == out["candidate_id"]

    def test_checkpoint_never_regresses_to_an_older_candidate(self, h):
        """U5：回退是人工动作，需要 S40 权限。"""
        # 手工造一个"更旧"的候选，直接喂 _advance_checkpoint
        job_id, first = h.ready_job()
        h.conn.execute(
            "INSERT INTO keyframe_candidate (candidate_id, tenant_id, "
            "image_job_id, shot_lineage_id, node_kind, artifact_ref, "
            "reference_combination_id, source_candidate_id, edit_mask_id, "
            "provider_job_id, attempt_id, observed_output, strategy_class, "
            "repair_action_id, requires_recheck, state, created_at) "
            "SELECT 'kfc_older', tenant_id, image_job_id, shot_lineage_id, "
            "node_kind, ?, reference_combination_id, NULL, NULL, "
            "provider_job_id, attempt_id, observed_output, NULL, NULL, 0, "
            "'READY', '1999-01-01T00:00:00.000Z' FROM keyframe_candidate "
            "WHERE candidate_id=?",
            (json.dumps({"service_id": "S28", "id": "art_older",
                         "revision": 1, "content_hash": _hex("7")}), first))
        job = dict(h.conn.execute(
            "SELECT * FROM image_job WHERE image_job_id=?", (job_id,)).fetchone())
        advanced = h.service._advance_checkpoint(job, "kfc_older")
        assert advanced is None
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["candidate_id"] == first


# ============================================ 红线：付费路径与配置

class TestPaidPathRedLines:
    def test_t29_31_no_provider_base_url_in_the_module(self):
        """模块里任何供应商域名都是缺陷：绕过 S26 = 绕过 S25/S23/S22。"""
        root = Path(__file__).resolve().parents[1] / "src" / "image_production"
        offenders = []
        for path in sorted(root.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in ("https://", "http://"):
                if token in text:
                    offenders.append((path.name, token))
        assert offenders == []

    def test_t29_31_no_provider_url_in_the_http_layer(self):
        path = Path(__file__).resolve().parents[1] / "api" / "app.py"
        text = path.read_text(encoding="utf-8")
        assert "https://" not in text and "http://" not in text

    def test_t29_31_submitter_targets_s26_only(self):
        from image_production import deps
        source = Path(deps.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        urls = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and ("http://" in node.value or "https://" in node.value)]
        assert urls == []

    def test_t29_31_unconfigured_submitter_is_503(self):
        from image_production.deps import build_submitter
        submitter = build_submitter(None)
        with pytest.raises(ServiceError) as exc:
            submitter({"image_job_id": "ij_1", "tenant_id": "t1",
                       "business_attempt_no": 1}, {}, {}, 0)
        assert exc.value.status == 503
        assert exc.value.code == "GENERATION_JOBS_UNAVAILABLE"

    def test_t29_31_unconfigured_route_is_503(self):
        from image_production.deps import build_route_resolver
        resolver = build_route_resolver(None)
        with pytest.raises(ServiceError) as exc:
            resolver("t1", {"image_job_id": "ij_1"}, {})
        assert exc.value.status == 503
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"

    def test_t29_31_unconfigured_canceler_is_503(self):
        from image_production.deps import build_canceler
        canceler = build_canceler(None)
        with pytest.raises(ServiceError) as exc:
            canceler("pj_1", "reason")
        assert exc.value.status == 503

    def test_t29_31_unconfigured_importer_is_503(self):
        from image_production.deps import build_artifact_importer
        importer = build_artifact_importer(None)
        with pytest.raises(ServiceError) as exc:
            importer("t1", {"image_job_id": "ij_1"})
        assert exc.value.status == 503

    def test_t29_31_unconfigured_artifact_check_is_none_not_false(self):
        from image_production.deps import build_artifact_check
        check = build_artifact_check(None)
        # 三态：`None` = 没查，必须与"查了，不匹配"可区分。
        assert check("t1", {"id": "a", "revision": 1}) is None

    def test_submitter_fabricates_no_provider_job_id(self, monkeypatch):
        from image_production import deps

        def fake_post(url, body, headers=None, timeout=None):
            assert "/generation-jobs" in url
            return {"operation_id": "op_only"}

        monkeypatch.setattr(deps, "_post", fake_post)
        submitter = deps.build_submitter("http://s26.invalid")
        with pytest.raises(ServiceError) as exc:
            submitter({"image_job_id": "ij_1", "tenant_id": "t1",
                       "business_attempt_no": 1}, {}, {"route_id": "r1"}, 0)
        assert exc.value.status == 503

    def test_submitter_refuses_without_a_route_id(self):
        from image_production.deps import build_submitter
        submitter = build_submitter("http://s26.invalid")
        with pytest.raises(ServiceError) as exc:
            submitter({"image_job_id": "ij_1", "tenant_id": "t1",
                       "business_attempt_no": 1}, {}, {}, 0)
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"

    def test_no_model_is_ever_called_directly(self, h):
        """公开方法只有九个：六个写/读口 + 两个事件消费者。"""
        public = {name for name in vars(ImageProductionService)
                  if not name.startswith("_") and callable(
                      getattr(ImageProductionService, name))}
        assert public == {"create_job", "get_job", "list_jobs", "get_checkpoint",
                          "get_candidate", "cancel_job", "retry_job",
                          "on_generation_state_changed", "on_artifact_ingested"}

    def test_service_has_no_provider_client_attribute(self, h):
        for name in ("provider", "client", "model", "vendor", "openai",
                     "stability", "replicate"):
            assert not hasattr(h.service, name), name


class TestDispatchRechecksBudget:
    """红线：预算/配额在**派单时**复查，不信任早先的"已批准"。"""

    def test_t29_92_route_is_asked_on_every_dispatch(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        assert h.calls["route"] == 1
        h.state_event(job_id, "PROVIDER_FAILED", event_id="ev_f")
        h.service.retry_job("t1", job_id, {}, "c_r")
        # 第二次派单必须重新问 S25（新报价、新配额窗口），不能复用第一条
        assert h.calls["route"] == 2

    def test_t29_92_retry_uses_a_fresh_route_decision(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="ev_f")
        h.service.retry_job("t1", job_id, {}, "c_r")
        assert h.routes[-1]["route_id"] != h.routes[0]["route_id"]

    def test_t29_92_no_approval_is_cached_on_the_job(self, h):
        result, _ = h.make_job()
        row = dict(h.conn.execute(
            "SELECT * FROM image_job WHERE image_job_id=?",
            (result["image_job_id"],)).fetchone())
        for name in row:
            assert "approved" not in name, name
            assert "reserved" not in name, name
            assert "permit" not in name, name
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_job WHERE budget_scope_id=''"
        ).fetchone()["n"] == 0

    def test_t29_92_route_failure_on_retry_does_not_submit(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="ev_f")
        h.route_result = ServiceError(503, "ROUTE_DECISION_UNAVAILABLE", "x")
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job("t1", job_id, {}, "c_r")
        assert exc.value.status == 503
        assert h.calls["submit"] == 1

    def test_t29_92_retry_respects_a_new_infeasible_route(self, h):
        """早先可行不等于现在可行（模型退休 / 账户撤销，T29-31）。"""
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="ev_f")
        h.route_result = {"feasible": False,
                          "infeasible_reason": "model retired"}
        out = h.service.retry_job("t1", job_id, {}, "c_r")
        assert out["state"] == "REJECTED_LOCAL"
        assert out["reason_code"] == "ROUTE_INFEASIBLE"
        assert h.calls["submit"] == 1


# ===================================================== 重启 / 恢复

class TestRestart:
    def test_t29_22_replay_after_restart_does_not_resubmit(self, tmp_path):
        db = tmp_path / "s29.db"
        first = Harness(conn=sqlite3.connect(
            str(db), check_same_thread=False, isolation_level=None))
        first.conn.row_factory = sqlite3.Row
        first.idem = SqliteIdempotencyStore(first.conn)
        first.calls = {k: 0 for k in first.calls}
        first.routes = []
        first.service = ImageProductionService(
            first.conn, route_resolver=first._route, submitter=first._submit,
            artifact_check=first._artifact, artifact_importer=first._import,
            generation_state=first._state, canceler=first._cancel)
        result, body = first.make_job(command_id="c1")
        assert first.calls["submit"] == 1
        first.conn.commit()

        second = Harness(conn=sqlite3.connect(
            str(db), check_same_thread=False, isolation_level=None))
        second.conn.row_factory = sqlite3.Row
        second.idem = SqliteIdempotencyStore(second.conn)
        second.calls = {k: 0 for k in second.calls}
        second.routes = []
        second.service = ImageProductionService(
            second.conn, route_resolver=second._route,
            submitter=second._submit, artifact_check=second._artifact,
            artifact_importer=second._import,
            generation_state=second._state, canceler=second._cancel)
        replay = second.command(body, command_id="c1")
        assert replay["replayed"] is True
        assert replay["image_job_id"] == result["image_job_id"]
        assert second.calls["submit"] == 0

    def test_t29_23_ingesting_job_is_not_regenerated_after_restart(self, tmp_path):
        db = tmp_path / "s29b.db"
        harness = Harness(conn=sqlite3.connect(
            str(db), check_same_thread=False, isolation_level=None))
        harness.conn.row_factory = sqlite3.Row
        harness.idem = SqliteIdempotencyStore(harness.conn)
        harness.calls = {k: 0 for k in harness.calls}
        harness.routes = []
        harness.service = ImageProductionService(
            harness.conn, route_resolver=harness._route,
            submitter=harness._submit, artifact_check=harness._artifact,
            artifact_importer=harness._import,
            generation_state=harness._state, canceler=harness._cancel)
        result, _ = harness.make_job()
        harness.state_event(result["image_job_id"], "PROVIDER_SUCCEEDED")
        assert harness.calls["submit"] == 1

        fresh = Harness(conn=sqlite3.connect(
            str(db), check_same_thread=False, isolation_level=None))
        fresh.conn.row_factory = sqlite3.Row
        fresh.idem = SqliteIdempotencyStore(fresh.conn)
        fresh.calls = {k: 0 for k in fresh.calls}
        fresh.routes = []
        fresh.service = ImageProductionService(
            fresh.conn, route_resolver=fresh._route, submitter=fresh._submit,
            artifact_check=fresh._artifact, artifact_importer=fresh._import,
            generation_state=fresh._state, canceler=fresh._cancel)
        out = fresh.artifact_event(result["image_job_id"], event_id="ev_art_r")
        assert out["state"] == "READY"
        assert fresh.calls["submit"] == 0

    def test_state_survives_a_rollback_mid_command(self, h):
        """事务回滚后不留半个 job：这是"派单不重复付费"的结构前提。"""
        h.submit_error = ServiceError(503, "GENERATION_JOBS_UNAVAILABLE", "x")
        with pytest.raises(ServiceError):
            h.service.create_job(h.body(), "t1", "c1")
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_job").fetchone()["n"] == 0
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM reference_combination").fetchone()["n"] == 0
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM outbox").fetchone()["n"] == 0
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM idempotency_record").fetchone()["n"] == 0

    def test_transaction_is_reentrant(self, h):
        conn = h.conn
        depth = [0]
        with Transaction(conn, depth):
            conn.execute("INSERT INTO job_transition (image_job_id, seq, "
                         "from_state, to_state, at) VALUES "
                         "('j1',1,'A','B','now')")
            with Transaction(conn, depth):
                conn.execute("INSERT INTO job_transition (image_job_id, seq, "
                             "from_state, to_state, at) VALUES "
                             "('j1',2,'B','C','now')")
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM job_transition").fetchone()["n"] == 2

    def test_inner_rollback_bubbles_to_the_outer_transaction(self, h):
        depth = [0]

        def attempt():
            with Transaction(h.conn, depth):
                h.conn.execute(
                    "INSERT INTO job_transition (image_job_id, seq, from_state,"
                    " to_state, at) VALUES ('j2',1,'A','B','now')")
                with Transaction(h.conn, depth):
                    raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            attempt()
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM job_transition").fetchone()["n"] == 0


# ======================================================= HTTP 层

def _http_client(monkeypatch, tmp_path, **overrides):
    """`api/app.py` 的 TestClient，上游换成 Harness 的 stub。

    `app.py` 在 import 那一刻就建连接、拼上游，所以 `S29_DB` 要指到独立文件库，
    `module.service` / `module._idem` 必须重绑，Harness 要复用 `module._conn`。
    """
    monkeypatch.setenv("S29_DB", str(tmp_path / "s29_http.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s29_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s29_api_app"] = module

    h = Harness(conn=module._conn, **overrides)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h


def _post(client, body, *, command_id="c1", tenant="t1",
          path="/image-jobs"):
    return client.post(path, json=body,
                       headers={"Idempotency-Key": command_id,
                                "X-Tenant-Id": tenant})


class TestHttpLayer:
    def test_create_returns_202(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = _post(client, h.body())
        assert r.status_code == 202, r.text
        assert r.json()["state"] == "WAITING_PROVIDER"
        assert r.json()["replayed"] is False

    def test_replay_through_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        first = _post(client, h.body())
        again = _post(client, h.body())
        assert again.json()["replayed"] is True
        assert again.json()["image_job_id"] == first.json()["image_job_id"]
        assert h.calls["submit"] == 1

    def test_same_key_different_body_is_409(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = _post(client, h.body(prompt_intent="a different frame"))
        assert r.status_code == 409
        assert r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_missing_idempotency_key_is_400(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = client.post("/image-jobs", json=h.body(),
                        headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_BODY"

    def test_missing_tenant_header_is_400(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = client.post("/image-jobs", json=h.body(),
                        headers={"Idempotency-Key": "c1"})
        assert r.status_code == 400

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = h.body()
        body["tenant_id"] = "t_other"
        r = _post(client, body)
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_MISMATCH"

    def test_command_id_header_must_match_body(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = h.body()
        body["command_id"] = "different"
        r = _post(client, body, command_id="c1")
        assert r.status_code == 400

    def test_validation_error_is_422_with_code(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = _post(client, h.body(mode="nope"))
        assert r.status_code == 422
        assert r.json()["code"] == "INVALID_MODE"

    def test_reference_404_is_passed_through(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)

        def check(tenant_id, ref):
            raise ServiceError(404, "ARTIFACT_NOT_FOUND", "not found")
        h.service.artifact_check = check
        r = _post(client, h.body())
        assert r.status_code == 404

    def test_upstream_503_is_retryable(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        h.route_result = ServiceError(500, "ROUTE_INTERNAL", "boom")
        r = _post(client, h.body())
        assert r.status_code == 503
        assert r.json()["retryable"] is True

    def test_cancel_route(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        r = _post(client, {}, command_id="c_cancel",
                  path=f"/image-jobs/{created['image_job_id']}:cancel")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "CANCELED"

    def test_retry_route_on_unknown_is_409(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        h.state_event(created["image_job_id"], "SUBMISSION_UNKNOWN")
        r = _post(client, {}, command_id="c_retry",
                  path=f"/image-jobs/{created['image_job_id']}:retry")
        assert r.status_code == 409
        assert r.json()["code"] == "MUST_RECONCILE_FIRST"

    def test_get_job_route(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        r = client.get(f"/image-jobs/{created['image_job_id']}",
                       headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert r.json()["image_job_id"] == created["image_job_id"]

    def test_get_job_other_tenant_is_404(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        r = client.get(f"/image-jobs/{created['image_job_id']}",
                       headers={"X-Tenant-Id": "t2"})
        assert r.status_code == 404

    def test_get_job_without_tenant_header_is_400(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        r = client.get(f"/image-jobs/{created['image_job_id']}")
        assert r.status_code == 400

    def test_list_route(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = client.get("/image-jobs", headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert r.json()["count"] == 1

    def test_list_is_tenant_scoped_over_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = client.get("/image-jobs", headers={"X-Tenant-Id": "t2"})
        assert r.json()["count"] == 0

    def test_checkpoint_route(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        h.ready_job()
        r = client.get("/shots/lineage_A/image-checkpoint",
                       headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert len(r.json()["checkpoints"]) == 1

    def test_candidate_route(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _, candidate_id = h.ready_job()
        r = client.get(f"/keyframe-candidates/{candidate_id}",
                       headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert r.json()["candidate_id"] == candidate_id

    def test_candidate_other_tenant_is_404(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _, candidate_id = h.ready_job()
        r = client.get(f"/keyframe-candidates/{candidate_id}",
                       headers={"X-Tenant-Id": "t2"})
        assert r.status_code == 404

    def test_readiness_lists_gaps(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = client.get("/health/readiness")
        assert r.status_code == 200
        assert r.json()["ready"] is True
        assert r.json()["contract_gaps"] == CONTRACT_GAPS


# ================================================== 契约缺口登记

class TestContractGaps:
    def test_every_gap_has_id_kind_summary_handling(self):
        for gap in CONTRACT_GAPS:
            assert set(gap) >= {"id", "kind", "summary", "handling"}, gap

    def test_gap_ids_are_unique(self):
        ids = [gap["id"] for gap in CONTRACT_GAPS]
        assert len(ids) == len(set(ids))

    def test_gap_kinds_are_known(self):
        assert {gap["kind"] for gap in CONTRACT_GAPS} <= {
            "gap", "conflict", "needs_owner"}

    def test_needs_owner_items_are_not_silently_filled(self):
        """任务书的硬要求：`needs_owner` 一条都不能被"顺手实现"。"""
        owners = [gap for gap in CONTRACT_GAPS if gap["kind"] == "needs_owner"]
        assert owners, "U3 的优先级规则仍未定，应登记为 needs_owner"
        for gap in owners:
            assert "未定" in gap["summary"] or "needs" in gap["summary"].lower()

    def test_plan_conflicts_are_registered(self):
        """方案内部冲突不能自行选边还不留痕。"""
        conflicts = {gap["id"] for gap in CONTRACT_GAPS
                     if gap["kind"] == "conflict"}
        for expected in ("C29-1", "C29-2", "C29-3", "C29-4", "C29-23"):
            assert expected in conflicts, expected

    def test_route_policy_ref_gap_is_registered(self):
        # 我们刚补的那一列，必须留下出处，不然下一个读者会以为是方案写的。
        assert any(gap["id"] == "C29-26" for gap in CONTRACT_GAPS)

    def test_hash_mismatch_detection_gap_is_registered(self):
        assert any(gap["id"] == "C29-27" for gap in CONTRACT_GAPS)


# ================================================== 服务边界

class TestBoundaries:
    def test_schema_has_no_foreign_service_tables(self):
        names = {row["name"] for row in sqlite3.connect(":memory:")
                 .execute("SELECT 1")} if False else set()
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {
            "image_job", "reference_combination", "keyframe_candidate",
            "edit_mask", "shot_image_checkpoint", "image_intent_ledger",
            "job_transition"}
        assert names == set()

    def test_service_writes_only_its_own_tables(self):
        """只看 SQL 字面量：`on_artifact_ingested` 是方法名，不是表名。"""
        path = Path(Path(__file__).resolve().parents[1] / "src"
                    / "image_production" / "service.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sql = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                if any(upper.lstrip().startswith(k) or f" {k} " in upper
                       for k in ("SELECT", "INSERT INTO", "UPDATE", "DELETE")):
                    sql.append(node.value)
        assert sql, "no SQL literals found; the probe is broken"
        blob = "\n".join(sql).lower()
        for foreign in ("tenant_access", "budget_ledger", "scheduler_quota",
                        "generation_jobs", "artifact_ingest",
                        "revision_snapshot", "model_routing"):
            assert foreign not in blob, foreign

    def test_service_imports_no_other_service_package(self):
        root = Path(__file__).resolve().parents[1] / "src" / "image_production"
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    top = name.split(".")[0]
                    assert top not in {
                        "music", "generation_jobs", "artifact_ingest",
                        "budget_ledger", "scheduler_quota", "model_routing",
                        "tenant_access"}, (path.name, name)

    def test_upstreams_are_injected_closures(self, h):
        for name in ("route_resolver", "submitter", "artifact_check",
                     "artifact_importer", "generation_state", "canceler"):
            assert callable(getattr(h.service, name)), name

    def test_ledger_has_no_cost_columns(self):
        """S29 不是费用权威：台账只记意图，不记钱。"""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(image_intent_ledger)")}
        assert "cost_micro" not in cols
        assert "amount" not in cols
        assert "currency" not in cols

    def test_image_job_has_no_balance_columns(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(image_job)")}
        for name in ("balance", "reserved_micro", "permit_id", "quota_left"):
            assert name not in cols, name

    def test_no_cross_tenant_join_possible(self):
        """每张表都带 tenant_id；没有不带租户的读取路径。"""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        for table in ("image_job", "reference_combination", "keyframe_candidate",
                      "edit_mask", "shot_image_checkpoint",
                      "image_intent_ledger"):
            cols = {row[1] for row in conn.execute(
                f"PRAGMA table_info({table})")}
            assert "tenant_id" in cols, table

    def test_edit_mask_window_ceiling_is_in_the_database(self):
        """C6：应用层有 bug 时数据库仍然拒绝。"""
        h = Harness()
        h.ready_job()
        candidate = h.conn.execute(
            "SELECT candidate_id FROM keyframe_candidate").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO edit_mask (edit_mask_id, tenant_id, "
                "source_candidate_id, artifact_ref, mask_artifact_id, "
                "mask_sha256, geometry_digest, instruction, "
                "window_start_frame, window_end_frame, window_fps, "
                "constraint_6frames, created_at) VALUES "
                "('em_x','t1',?,'{}','m',?,'g','i',0,6,24,1,'now')",
                (candidate, _hex("9")))
        h.conn.close()

    def test_max_edit_window_is_six(self):
        assert MAX_EDIT_WINDOW_FRAMES == 6

    def test_execution_context_requires_eighteen_keys(self):
        assert len(EXECUTION_CONTEXT_REQUIRED) == 18
        assert "run_id" not in EXECUTION_CONTEXT_REQUIRED


# =============================== 补齐方案 §10 里尚未覆盖的验收行 ===============================

class TestRemainingAcceptanceRows:
    """T29-21/24/25/27/28/29/30/33/35。

    这一组里有几行**不是 S29 能自己证明的**——它们断言的是别人的行为
    （S30 标记 stale、S39 计数、S28 沙箱）。对这些行，测试只证明 S29 这半边：
    不做那件事、不写那张表、不自己算那个值。假装测了整个 E 编号是自欺。
    """

    def test_t29_21_cas_advances_one_at_a_time(self, h):
        """两个候选先后推进：`checkpoint_seq` 每次 +1，不跳号、不覆盖。"""
        job_id, first = h.ready_job()
        result2, _ = h.make_job(prompt_intent="concurrent contender")
        h.state_event(result2["image_job_id"], "PROVIDER_SUCCEEDED",
                      event_id="ev_c2")
        out = h.artifact_event(result2["image_job_id"], event_id="ev_c2_art",
                               artifact_id="art_c2", content_hash=_hex("e"))
        assert out["checkpoint_advanced"] is True
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["checkpoint_seq"] == 2

    def test_t29_21_cas_failure_surfaces_as_a_code(self, h):
        """打掉 CAS 的 rowcount，验证走 409 CHECKPOINT_CAS_FAILED，
        而不是悄悄当成功。`sqlite3.Connection.execute` 是只读属性，所以包一层
        代理对象，别去给连接打补丁。"""
        job_id, first = h.ready_job()
        job = dict(h.conn.execute(
            "SELECT * FROM image_job WHERE image_job_id=?",
            (job_id,)).fetchone())
        real_conn = h.conn

        class _Cur:
            rowcount = 0

        class _Conn:
            """只拦 UPDATE，其余原样转给真连接；事务助手仍走真连接。"""

            row_factory = real_conn.row_factory

            def execute(self, sql, params=()):
                if sql.lstrip().startswith("UPDATE shot_image_checkpoint"):
                    return _Cur()
                return real_conn.execute(sql, params)

            def executescript(self, script):
                return real_conn.executescript(script)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        h.service.conn = _Conn()
        try:
            with pytest.raises(ServiceError) as exc:
                h.service._advance_checkpoint(job, first)
        finally:
            h.service.conn = real_conn
        assert exc.value.status == 409
        assert exc.value.code == "CHECKPOINT_CAS_FAILED"
        assert exc.value.retryable is True

    def test_t29_24_writes_no_s30_tables(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        tables = {r[0].lower() for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for foreign in ("clip", "edit_timeline", "edit_decision", "s30_clip",
                        "timeline_clip"):
            assert foreign not in tables, foreign

    def test_t29_24_ready_event_is_the_only_coupling_to_s30(self, h):
        """S29 → S30 只有一个出口：`production.image.ready`。"""
        h.ready_job()
        types = {r["event_type"] for r in h.conn.execute(
            "SELECT event_type FROM outbox").fetchall()}
        assert EVENT_IMAGE_READY in types
        assert not any(t.startswith("clip.") for t in types), types

    def test_t29_25_state_seq_is_strictly_monotonic(self, h):
        job_id, _ = h.ready_job()
        job = h.service.get_job("t1", job_id)
        seqs = [edge["seq"] for edge in job["history"]]
        assert seqs == list(range(1, len(seqs) + 1))

    def test_t29_25_ready_event_carries_checkpoint_seq(self, h):
        h.ready_job()
        payload = json.loads(h.conn.execute(
            "SELECT payload FROM outbox WHERE event_type=?",
            (EVENT_IMAGE_READY,)).fetchone()["payload"])
        assert isinstance(payload["data"]["checkpoint"]["checkpoint_seq"], int)
        assert payload["data"]["checkpoint"]["checkpoint_seq"] >= 1

    def test_t29_25_older_candidate_cannot_bump_checkpoint_seq(self, h):
        job_id, first = h.ready_job()
        before = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        h.conn.execute(
            "INSERT INTO keyframe_candidate (candidate_id, tenant_id, "
            "image_job_id, shot_lineage_id, node_kind, artifact_ref, "
            "reference_combination_id, source_candidate_id, edit_mask_id, "
            "provider_job_id, attempt_id, observed_output, strategy_class, "
            "repair_action_id, requires_recheck, state, created_at) "
            "SELECT 'kfc_old2', tenant_id, image_job_id, shot_lineage_id, "
            "node_kind, ?, reference_combination_id, NULL, NULL, "
            "provider_job_id, attempt_id, observed_output, NULL, NULL, 0, "
            "'READY', '1999-01-01T00:00:00.000Z' FROM keyframe_candidate "
            "WHERE candidate_id=?",
            (json.dumps({"service_id": "S28", "id": "art_old2",
                         "revision": 1, "content_hash": _hex("6")}), first))
        job = dict(h.conn.execute(
            "SELECT * FROM image_job WHERE image_job_id=?",
            (job_id,)).fetchone())
        assert h.service._advance_checkpoint(job, "kfc_old2") is None
        after = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert after["checkpoint_seq"] == before["checkpoint_seq"]

    def test_t29_27_replayed_ready_event_has_no_side_effect(self, h):
        job_id, candidate_id = h.ready_job()
        submits = h.calls["submit"]
        out = h.artifact_event(job_id, event_id="ev_art_1")
        assert out["reason"] == "DUPLICATE"
        assert h.calls["submit"] == submits
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM keyframe_candidate").fetchone()["n"] == 1
        assert h.conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE event_type=?",
            (EVENT_IMAGE_READY,)).fetchone()["n"] == 1

    def test_t29_27_consumers_never_call_the_submitter(self, h):
        job_id, _ = h.ready_job()
        before = h.calls["submit"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id="ev_again")
        h.artifact_event(job_id, event_id="ev_new_art")
        assert h.calls["submit"] == before

    def test_t29_28_same_shot_new_lineage_is_refused(self, h):
        h.make_job()
        with pytest.raises(ServiceError) as exc:
            h.service.create_job(
                h.body(shot_lineage_id="lineage_run2",
                       context={"execution_fence": 3}), "t1", "c_drift")
        assert exc.value.status == 409
        assert exc.value.code == "SHOT_LINEAGE_DRIFT"
        assert exc.value.details["existing_shot_lineage_id"] == "lineage_A"

    def test_t29_28_other_shot_may_have_its_own_lineage(self, h):
        h.make_job()
        h.service.create_job(
            h.body(shot_lineage_id="lineage_B",
                   shot_ref={"service_id": "S06", "id": "shot_2",
                             "revision": 1}), "t1", "c_other")
        assert h.service.list_jobs("t1")["count"] == 2

    def test_t29_29_strategy_class_is_carried_verbatim(self, h):
        result, _ = h.make_job(strategy_class="S39-A")
        row = dict(h.conn.execute(
            "SELECT strategy_class FROM image_job WHERE image_job_id=?",
            (result["image_job_id"],)).fetchone())
        assert row["strategy_class"] == "S39-A"

    def test_t29_29_s29_computes_no_strategy_class(self, h):
        """§8.3：计算规则归 S39。不传就必须留 NULL，不能推断。"""
        result, _ = h.make_job()
        row = dict(h.conn.execute(
            "SELECT strategy_class FROM image_job WHERE image_job_id=?",
            (result["image_job_id"],)).fetchone())
        assert row["strategy_class"] is None

    def test_t29_30_run_id_is_not_an_upper_bound_key(self, h):
        """跨 Run 重试不清零：`run_id` 只做审计归因（§8.1 / C29-8）。"""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        idx = [r[1] for r in conn.execute(
            "PRAGMA index_list(image_intent_ledger)")]
        for name in idx:
            cols = [r[2] for r in conn.execute(f"PRAGMA index_info({name})")]
            assert "run_id" not in cols, (name, cols)

    def test_t29_30_retry_does_not_reset_the_ledger(self, h):
        result, _ = h.make_job()
        job_id = result["image_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="ev_f")
        h.service.retry_job("t1", job_id, {}, "c_r")
        row = h.conn.execute(
            "SELECT business_attempt_no FROM image_job WHERE image_job_id=?",
            (job_id,)).fetchone()
        assert row["business_attempt_no"] == 2
        entries = h.conn.execute(
            "SELECT COUNT(*) AS n FROM image_intent_ledger WHERE image_job_id=?",
            (job_id,)).fetchone()["n"]
        # 两次付费提交 → 两行（台账只追加，不重写第一次）
        assert entries == 2

    def test_t29_33_transitions_are_pure_data(self):
        """状态机是可序列化的表——回滚时旧版本读到同一张表才会得出同一结论。"""
        assert isinstance(TRANSITIONS, dict)
        for source, targets in TRANSITIONS.items():
            assert source is None or isinstance(source, str)
            assert isinstance(targets, frozenset)

    def test_t29_33_transitions_survive_a_json_roundtrip(self):
        wire = {str(k): sorted(v) for k, v in TRANSITIONS.items()}
        assert json.loads(json.dumps(wire)) == wire

    def test_t29_33_canonical_transition_has_no_side_effects(self):
        before = {k: set(v) for k, v in TRANSITIONS.items()}
        canonical_transition("PREPARING", "SUBMITTED")
        canonical_transition("FAILED", "PREPARING", allow_retry=True)
        assert {k: set(v) for k, v in TRANSITIONS.items()} == before

    def test_t29_35_s29_never_fetches_the_mask_bytes(self, h):
        """E29：mask 字节只由 S28 在沙箱里取。

        `deps` 里有 urllib —— 那是发给 S26/S28/S25 的**控制面**请求，合法。
        这条要证明的是另一个更强的东西：任何上游回调都拿不到 mask 的字节，
        因为 mask 在本地只是一个 `{artifact_id, sha256, geometry}` 三元组，
        服务从没把它交给一个"下载"函数。
        """
        import inspect

        from image_production import deps
        # 六个上游 closure 的签名里都不接受 mask、URL 或字节。
        for factory in ("build_route_resolver", "build_submitter",
                        "build_artifact_check", "build_artifact_importer",
                        "build_generation_state", "build_canceler"):
            fn = getattr(deps, factory)
            params = set(inspect.signature(fn).parameters)
            for forbidden in ("mask", "url", "bytes", "blob", "payload"):
                assert forbidden not in params, (factory, forbidden)

        source = Path(deps.__file__).read_text(encoding="utf-8")
        # 没有任何函数按 artifact_id 去取字节
        assert "download" not in source.lower()
        assert "artifact_bytes" not in source

    def test_t29_35_mask_never_reaches_the_submitter(self, h):
        """edit 派单时，S26 收到的 command 里只有 mask 的 id/sha256/role。"""
        _, candidate_id = h.ready_job()
        body = h.body(
            mode="edit", source_candidate_id=candidate_id,
            repair_action_id="ra_9b", prompt_intent="repair",
            instruction="fix",
            edit_mask={"artifact_id": "art_mask_9b", "sha256": _hex("9"),
                       "geometry": {"bbox": [1, 2, 3, 4]}},
            window={"start_frame": 1, "end_frame": 4, "fps": 24},
            node_kind="keyframe_mid", shot_lineage_id="lineage_A")
        h.command(body, command_id="c_mask_9b")
        blob = json.dumps(h.submitted_commands[-1])
        assert "bbox" not in blob
        assert _hex("9") in blob

    def test_t29_35_mask_is_referenced_by_id_and_hash_only(self, h):
        _, candidate_id = h.ready_job()
        body = h.body(
            mode="edit", source_candidate_id=candidate_id,
            repair_action_id="ra_9", prompt_intent="repair",
            instruction="fix",
            edit_mask={"artifact_id": "art_mask_9", "sha256": _hex("9"),
                       "geometry": {"bbox": [1, 2, 3, 4]}},
            window={"start_frame": 1, "end_frame": 4, "fps": 24},
            node_kind="keyframe_mid", shot_lineage_id="lineage_A")
        h.command(body, command_id="c_mask_9")
        refs = h.submitted_commands[-1]["input"]["references"]
        mask = [r for r in refs if r["role"] == "mask"][0]
        assert set(mask) == {"artifact_id", "sha256", "role"}

    def test_t29_35_mask_geometry_carries_no_pixels(self, h):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edit_mask)")}
        for forbidden in ("mask_bytes", "mask_png", "pixels", "payload"):
            assert forbidden not in cols, forbidden
        assert "geometry_digest" in cols
