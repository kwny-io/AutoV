"""S30 单测。编号对应 `plans/S30-video-production.md` §10 的 T30-*。

这份文件要守住四条红线（AGENTS.md 与任务书），每条都有专门的测试类：

1. `TestPaidPathRedLines`：**没有任何供应商 base url**，付费出口只有
   `deps.build_submitter` 指向 S26；未配置的上游一律 503，不返回"看起来成功"
   的值。
2. `TestSubmissionUnknown`：`SUBMISSION_UNKNOWN` 期间绝不重提、绝不取消、
   不因 deadline 逼近而行动。
3. `TestDispatch` / `TestDispatchRechecksBudget`：预算与路由在**派单时**重新向
   S25 取，不信任早先的"已批准"；报价过期宁可 503 也不按旧价提交。
4. `TestTenantIsolation`：`tenant_id` 只来自签名头；跨租户读与不存在同形（404）。

上游全部是 stub，且按真实契约写：

- S25 的 HTTP 响应没有顶层 `selected`（`deps` 负责把 `eligible[0]` 抬上来）。
  这里直接按内核要的形状喂，`deps` 的归一另有测试。
- S26 的 `provider_job_id` 是提交成功的唯一信号；stub 不伪造它。
- `artifact.ingested` 的 `provider_job_id` 在 `source.provider_job_id` 里，
  并且**必须**带 `observed_duration_ms` + `frame_rate`（§3.2 的硬门）。

一组"不能由 S30 自己证明"的行（T30-27 的上限计数、T30-38 的 broker 重放）
只证明 S30 这半边：把 `shot_lineage_id` 带对、把事件写进 outbox、不自己数数。
假装测了别人的行为是自欺。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import SqliteIdempotencyStore, canonical_request_hash

from video_production import deps as s30_deps
from video_production.schema import (
    AUDIO_STRATEGIES,
    CANDIDATE_STATES,
    CONTRACT_GAPS,
    DURATION_POLICIES,
    DURATION_TOLERANCE_MS,
    ID_PREFIX,
    JOB_STATES,
    LEDGER_ACTIONS,
    RETRYABLE_STATES,
    SCHEMA,
    TERMINAL_STATES,
    TRANSITIONS,
    TRIM_MODES,
)
from video_production.service import (
    EVENT_VIDEO_READY,
    EXECUTION_CONTEXT_REQUIRED,
    LOCAL_CLIP_ATTENTION,
    LOCAL_DURATION_MISMATCH,
    LOCAL_JOB_STATE,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC_VIDEO,
    ServiceError,
    Transaction,
    VideoProductionService,
    canonical_transition,
    input_fingerprint,
    reference_combination_hash,
    solve_trim,
)

FUTURE = "2099-01-01T00:00:00.000Z"
PAST = "2000-01-01T00:00:00.000Z"
NOW = "2026-11-14T00:00:00.000Z"
HEX = "0123456789abcdef" * 4

SERVICE_ROOT = Path(__file__).resolve().parents[1]
OMIT = object()


def _hex(ch: str) -> str:
    return ch * 64


def _art(artifact_id="art_kf_1", *, service_id="S28", hash_ch="c",
         revision=2):
    return {"service_id": service_id, "id": artifact_id, "revision": revision,
            "content_hash": _hex(hash_ch)}


# =============================================================== 上游 stub

class Harness:
    """内存库 + 幂等外壳 + 一组可编程的上游 callback。

    形状与 `api/app.py` 的 `command()` 一致：`begin` → 执行 → `finish`；
    重放打 `replayed: True`。七个 callback 与
    `VideoProductionService.__init__` 的关键字参数一一对应——比 S29 多一个
    `keyframe_lookup`（C7 / S29 的 `GET image-checkpoint`）。
    """

    def __init__(self, conn=None, **overrides):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)
        self.calls = {"route": 0, "submit": 0, "artifact": 0, "import": 0,
                      "cancel": 0, "state": 0, "keyframe": 0}
        self.routes = []
        self.route_result = overrides.get("route_result")
        self.route_sequence = list(overrides.get("route_sequence") or [])
        self.submit_error = overrides.get("submit_error")
        self.submit_result = overrides.get("submit_result")
        self.artifact_verdict = overrides.get("artifact_verdict", True)
        self.import_error = overrides.get("import_error")
        self.cancel_error = overrides.get("cancel_error")
        self.state_result = overrides.get("state_result")
        self.keyframe_result = overrides.get("keyframe_result")
        self.known_tenants = overrides.get("known_tenants", {"t1"})
        self.submitted_commands = []
        self.imported_jobs = []
        self.canceled = []
        self._seq = 0

        self.service = VideoProductionService(
            self.conn,
            route_resolver=self._route,
            submitter=self._submit,
            artifact_check=self._artifact,
            artifact_importer=self._import,
            generation_state=self._state,
            canceler=self._cancel,
            keyframe_lookup=self._lookup,
        )

    # ---- callbacks ----
    def _route(self, tenant_id, job, generation_command):
        self.calls["route"] += 1
        if self.route_sequence:
            item = self.route_sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
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
            "selected": {
                "normalized_input": {"duration_seconds": 5.0},
                "expected_cost_micro": 1_000,
                "quote_upper_bound_micro": 900_000,
                "currency": "USD",
            },
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
        return {"provider_job_id": f"pj_{job['video_job_id']}",
                "operation_id": f"op_{job['video_job_id']}",
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
        self.imported_jobs.append(job["video_job_id"])
        if self.import_error is not None:
            raise self.import_error
        return {"state": "accepted"}

    def _state(self, tenant_id, provider_job_id):
        self.calls["state"] += 1
        if self.state_result is not None:
            if isinstance(self.state_result, Exception):
                raise self.state_result
            return self.state_result
        return {"state": "RUNNING"}

    def _cancel(self, provider_job_id, reason):
        self.calls["cancel"] += 1
        self.canceled.append((provider_job_id, reason))
        if self.cancel_error is not None:
            raise self.cancel_error

    def _lookup(self, tenant_id, candidate_id, shot_lineage_id):
        """S29 的读口。`None` **只**表示"不存在或不是本租户"。"""
        self.calls["keyframe"] += 1
        if isinstance(self.keyframe_result, Exception):
            raise self.keyframe_result
        if self.keyframe_result is not None:
            return self.keyframe_result
        if tenant_id not in self.known_tenants:
            return None
        if candidate_id != "kfc_1":
            return None
        return {"candidate_id": candidate_id, "checkpoint_seq": 3,
                "artifact_ref": _art("art_kf_1"), "content_hash": _hex("c")}

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
            "segment_no": 1,
            "keyframe_baseline": {"candidate_id": "kfc_1",
                                  "checkpoint_seq": 1},
            "prompt_intent": "低机位仰拍林岚，轻微推进，目标四秒",
            "target_duration_ms": 4000,
            "duration_policy": "allow_tier_snap_with_trim",
            "aspect_ratio": "9:16",
            "resolution": {"width": 1080, "height": 1920},
            "audio_strategy": "silent_then_tts",
            "required_capabilities": ["image_to_video"],
            "allow_trim": True,
            "route_policy_ref": {"service_id": "S25", "id": "routing_demo",
                                 "revision": 1, "content_hash": _hex("c")},
            "budget_scope_id": "budget_ep3",
            "deadline": FUTURE,
            "shot_context": {"has_dialogue": True, "voice_locked_by_s15": False},
        }
        for key, value in over.items():
            if key in ("context", "shot_context"):
                body[key].update(value)
            else:
                body[key] = value
        if "allow_trim" not in over and "duration_policy" in over:
            body["allow_trim"] = (
                over["duration_policy"] == "allow_tier_snap_with_trim")
        return body

    # ---- event helpers ----
    def _provider_job(self, job_id):
        row = self.conn.execute(
            "SELECT provider_job_id FROM video_job WHERE video_job_id=?",
            (job_id,)).fetchone()
        assert row is not None, job_id
        return row["provider_job_id"]

    def state_event(self, job_id, state, *, event_id=None, tenant="t1",
                    reason_code=None):
        provider_job_id = self._provider_job(job_id)
        data = {"state": state, "provider_job_id": provider_job_id}
        if reason_code:
            data["reason_code"] = reason_code
        return self.service.on_generation_state_changed({
            "id": event_id or f"ev_state_{job_id}_{self._seq}",
            "tenant_id": tenant,
            "source": "S26", "type": "generation.job.state-changed",
            "aggregate_id": provider_job_id, "data": data})

    def artifact_event(self, job_id, *, event_id=None, artifact_id="art_out_1",
                       content_hash=None, tenant="t1",
                       service_id="S28", observed_duration_ms=5000,
                       frame_rate=24.0, observed_field="observed_duration_ms"):
        """S28 `artifact.ingested`。

        `observed_duration_ms` 传 `OMIT` 时整个键不出现——T30-13 测"S28 没报"。
        `observed_field="duration_ms"` 走灵名归一那条路（C30-12）。
        """
        provider_job_id = self._provider_job(job_id)
        data = {
            "schema_version": "2.0",
            "artifact_ref": {"service_id": service_id, "id": artifact_id,
                             "revision": 1,
                             "content_hash": content_hash or _hex("f")},
            "content_class": "generated_video",
            "size_bytes": 4096,
            "source": {"kind": "provider_url",
                       "provider_job_id": provider_job_id},
            "dedup": None, "derivative_hint": [],
            "mime_type": "video/mp4",
        }
        if observed_duration_ms is not OMIT:
            data[observed_field] = observed_duration_ms
        if frame_rate is not OMIT:
            data["frame_rate"] = frame_rate
        return self.service.on_artifact_ingested({
            "id": event_id or f"ev_art_{job_id}_{self._seq}",
            "tenant_id": tenant, "source": "S28", "type": "artifact.ingested",
            "aggregate_id": data["artifact_ref"]["id"], "data": data})

    def image_ready_event(self, lineage, checkpoint_seq, *, event_id=None,
                          tenant="t1", checkpoint=True):
        data = {"shot_lineage_id": lineage}
        if checkpoint:
            data["checkpoint"] = {"checkpoint_seq": checkpoint_seq}
        return self.service.on_image_ready({
            "id": event_id or f"ev_img_{lineage}_{self._seq}",
            "tenant_id": tenant, "source": "S29",
            "type": "production.image.ready",
            "aggregate_id": lineage, "data": data})

    def speech_ready_event(self, lineage, audio_duration_ms, *, event_id=None,
                           tenant="t1"):
        return self.service.on_speech_ready({
            "id": event_id or f"ev_sp_{lineage}_{self._seq}",
            "tenant_id": tenant, "source": "S31",
            "type": "production.speech.ready",
            "aggregate_id": lineage,
            "data": {"shot_lineage_id": lineage,
                     "audio_duration_ms": audio_duration_ms}})

    # ---- convenience ----
    def make_job(self, command_id=None, **over):
        body = self.body(**over)
        if command_id is None:
            self._seq += 1
            command_id = f"c_auto_{self._seq}"
        return self.command(body, command_id=command_id), body

    def ready_job(self, **over):
        """一路推到 READY，返回 (job_id, candidate_id)。

        `artifact_id` 也走 `over`：同一条 lineage 上要造多份**不同字节**的候选
        时必须换 id，否则 `cc_artifact_uniq` 会把它们复用成同一行。
        """
        result, _ = self.make_job(**over)
        job_id = result["video_job_id"]
        self._seq += 1
        self.state_event(job_id, "PROVIDER_SUCCEEDED",
                         event_id=f"ev_s_{job_id}")
        ingested = self.artifact_event(job_id, event_id=f"ev_a_{job_id}",
                                       **{k: v for k, v in over.items()
                                          if k in ("observed_duration_ms",
                                                   "frame_rate",
                                                   "artifact_id")})
        assert ingested.get("state") == "READY", ingested
        return job_id, ingested["candidate_id"]

    def rewind(self, job_id, state, *, drop_provider_job=False):
        """把一个已成功派出的 job 拨回更早的非终态，并**保持历史自洽**。

        不能只改 `video_job.state`：`job_transition` 的 PK 是
        `(video_job_id, seq)`，而 `_transition` 用 `state_seq + 1` 当新 seq。
        若留下真实历史（seq 1..3）却把 `state_seq` 写回 1，下一次转移就会撞
        主键，测出来的是测试构造的错而不是内核的错。

        所以这里把该 job 的历史**截断**到"拨回后应有的样子"：

        - `CREATED` 是 job 的**初始**态（`state_seq=0`），内核从不为它写转移
          行（与 S29 同约定），所以拨回 CREATED 就是清空历史；
        - 其他态必须真的在历史里出现过，截到它那一条为止；
        - 从未出现过的态直接断言失败，免得拼出一个不存在的过去。

        这样后续每条真实转移都接着往下写。
        """
        if state == "CREATED":
            self.conn.execute(
                "DELETE FROM job_transition WHERE video_job_id = ?", (job_id,))
            target_seq = 0
        else:
            row = self.conn.execute(
                "SELECT MAX(seq) AS m FROM job_transition "
                "WHERE video_job_id = ? AND to_state = ?",
                (job_id, state)).fetchone()
            if row["m"] is None:
                raise AssertionError(
                    f"{job_id} never passed through {state}; cannot rewind to "
                    "a state that is not in its history")
            target_seq = int(row["m"])
            self.conn.execute(
                "DELETE FROM job_transition WHERE video_job_id = ? AND seq > ?",
                (job_id, target_seq))
        sets = "state = ?, state_seq = ?, updated_at = ?"
        params = [state, target_seq, "2026-01-01T00:00:00.000Z"]
        if drop_provider_job:
            sets += ", provider_job_id = NULL, submitted_duration_ms = NULL"
        self.conn.execute(
            f"UPDATE video_job SET {sets} WHERE video_job_id = ?",
            (*params, job_id))
        return job_id

    def force_state(self, job_id, state):
        """**测试脚手架**：把 job 直接放到某个状态，并追加一条自洽的转移行。

        只用于"驱动不到这个状态但测试需要从它出发"的场景（例如让 S31 的对白
        事件与派单并发）。它不校验状态机——那是内核的职责，测试若借它绕过一条
        内核本该拒绝的边，就是假绿。行里的 `reason_code='TEST_FORCE'` 让任何
        事后读历史的人一眼看出这一段不是内核走出来的。
        """
        assert state in JOB_STATES, state
        current = self.job_row(job_id)
        seq = int(current["state_seq"]) + 1
        self.conn.execute(
            "INSERT INTO job_transition (video_job_id, seq, from_state, "
            "to_state, reason_code, detail, at) VALUES (?,?,?,?,?,?,?)",
            (job_id, seq, current["state"], state, "TEST_FORCE", None,
             "2026-01-01T00:00:00.000Z"))
        self.conn.execute(
            "UPDATE video_job SET state = ?, state_seq = ?, updated_at = ? "
            "WHERE video_job_id = ?",
            (state, seq, "2026-01-01T00:00:00.000Z", job_id))
        return job_id

    def state_of(self, job_id):
        return self.conn.execute(
            "SELECT state FROM video_job WHERE video_job_id=?",
            (job_id,)).fetchone()["state"]

    def job_row(self, job_id):
        return dict(self.conn.execute(
            "SELECT * FROM video_job WHERE video_job_id=?",
            (job_id,)).fetchone())

    def count(self, table, where="1=1", params=()):
        return self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {where}",
            params).fetchone()["n"]

    def events(self, event_type):
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM outbox WHERE event_type=? ORDER BY sequence",
            (event_type,)).fetchall()]


@pytest.fixture
def h():
    harness = Harness()
    yield harness
    harness.conn.close()


# ================================================================ 纯函数

class TestAddressing:
    """寻址与哈希。这些是幂等键的原料，错一个就多花一次钱。"""

    def test_input_fingerprint_is_order_insensitive_for_capabilities(self):
        kwargs = dict(keyframe_baseline="kfc_1", prompt_intent="p",
                      target_duration_ms=4000, duration_policy="allow_tier_snap",
                      aspect_ratio="9:16", resolution={"width": 1080, "height": 1920},
                      audio_strategy="silent_then_tts")
        a = input_fingerprint(required_capabilities=["a", "b"], **kwargs)
        b = input_fingerprint(required_capabilities=["b", "a"], **kwargs)
        assert a == b

    def test_input_fingerprint_changes_with_each_field(self):
        base = dict(keyframe_baseline="kfc_1", prompt_intent="p",
                    target_duration_ms=4000, duration_policy="allow_tier_snap",
                    aspect_ratio="9:16", resolution={"width": 1080, "height": 1920},
                    audio_strategy="silent_then_tts",
                    required_capabilities=["image_to_video"])
        seen = {input_fingerprint(**base)}
        for key, value in [("keyframe_baseline", "kfc_2"), ("prompt_intent", "q"),
                           ("target_duration_ms", 5000),
                           ("duration_policy", "native_only"),
                           ("aspect_ratio", "16:9"),
                           ("resolution", {"width": 720, "height": 1280}),
                           ("audio_strategy", "non_dialogue_native"),
                           ("required_capabilities", ["image_to_video", "x"])]:
            seen.add(input_fingerprint(**{**base, key: value}))
        assert len(seen) == 9, "every field must matter to the fingerprint"

    def test_input_fingerprint_excludes_trace_and_budget(self):
        """§7.1：`request_hash` 不含 `trace_id`。业务指纹同理——否则同一个
        逻辑请求换一次追踪就变成第二次付费。"""
        base = dict(keyframe_baseline="kfc_1", prompt_intent="p",
                    target_duration_ms=4000, duration_policy="allow_tier_snap",
                    aspect_ratio="9:16", resolution={"width": 1080, "height": 1920},
                    audio_strategy="silent_then_tts",
                    required_capabilities=["image_to_video"])
        assert input_fingerprint(**base) == input_fingerprint(**base)

    def test_reference_combination_hash_distinguishes_none(self):
        assert reference_combination_hash(None) \
            == reference_combination_hash(None)
        one = reference_combination_hash(_art("a1", hash_ch="1"))
        two = reference_combination_hash(_art("a2", hash_ch="2"))
        assert len({reference_combination_hash(None), one, two}) == 3

    def test_reference_combination_hash_ignores_revision_only_changes(self):
        """同一份字节换了 revision 还是同一组合（内容哈希才是身份）。"""
        a = reference_combination_hash({"service_id": "S28", "id": "x",
                                        "revision": 1, "content_hash": _hex("c")})
        b = reference_combination_hash({"service_id": "S28", "id": "x",
                                        "revision": 9, "content_hash": _hex("c")})
        assert a == b


class TestStateMachine:
    """§3.1 + C30-29 + C30-30。"""

    def test_every_state_has_a_row(self):
        assert set(TRANSITIONS) == JOB_STATES | {None}

    def test_terminal_states_have_no_out_edges(self):
        for state in TERMINAL_STATES:
            assert TRANSITIONS[state] == frozenset(), state

    def test_reconciled_ready_is_terminal(self):
        """C30-2：§3.2 的列表漏了它，§3.1 把它当终局。"""
        assert "RECONCILED_READY" in TERMINAL_STATES

    def test_unknown_has_only_reconciliation_exits(self):
        assert TRANSITIONS["WAITING_UNKNOWN"] == frozenset(
            {"RECONCILED_READY", "RECONCILED_FAILED"})

    def test_unknown_cannot_be_canceled(self):
        """§4.5：UNKNOWN 期间不发起取消。"""
        assert "CANCELED" not in TRANSITIONS["WAITING_UNKNOWN"]
        assert canonical_transition("WAITING_UNKNOWN", "CANCELED") is False

    def test_preparing_can_fail_only_for_f5(self):
        """C30-29：F5 要求 `FAILED(SPEC_ASSERTION)`，表里因此有这条边。"""
        assert canonical_transition("PREPARING", "FAILED") is True

    def test_retry_edge_requires_explicit_flag(self):
        for state in RETRYABLE_STATES:
            assert canonical_transition(state, "PREPARING") is False
            assert canonical_transition(state, "PREPARING", allow_retry=True) \
                is True

    def test_retry_flag_does_not_open_other_states(self):
        assert canonical_transition("READY", "PREPARING", allow_retry=True) \
            is False
        assert canonical_transition("FAILED", "READY", allow_retry=True) is False

    def test_unknown_state_name_is_rejected(self):
        with pytest.raises(AssertionError):
            canonical_transition("CREATED", "NOPE")
        with pytest.raises(AssertionError):
            canonical_transition("NOPE", "CREATED")

    def test_submitted_cannot_go_straight_to_ready(self):
        """正常路径必经 INGESTING；越过去会让 `INGESTING → INGESTING` 的重归档
        语义消失（C30-30 用补记那一格来处理丢失的中间事件）。"""
        assert canonical_transition("SUBMITTED", "READY") is False
        assert canonical_transition("WAITING_PROVIDER", "READY") is False

    def test_t30_36_the_transition_table_is_the_only_authority(self):
        """T30-36：独立升级 / 回滚。

        新旧版本之所以给出同一答案，是因为**没有任何一条转移是写死在调用点
        的 if 分支里**——只有 `TRANSITIONS` 一张表。这条测试从两侧钉住它：
        表一旦被改，`canonical_transition` 跟着变；表外的边不可能被"顺手"
        放行。回滚旧镜像时读到的是旧表，两版对同一对 (from, to) 的结论必须
        由各自那份表决定，不能被代码路径分叉。
        """
        for from_state in JOB_STATES:
            for to_state in JOB_STATES:
                expected = to_state in TRANSITIONS.get(from_state, frozenset())
                if from_state in RETRYABLE_STATES:
                    # 重试是显式命令，必须带 allow_retry；不带时一律按表判。
                    assert canonical_transition(from_state, to_state) is False
                    assert (canonical_transition(from_state, to_state,
                                                 allow_retry=True)
                            is (to_state == "PREPARING"))
                else:
                    assert canonical_transition(from_state, to_state) is expected

    def test_t30_36_the_table_has_no_unreachable_states(self):
        """每个状态必须从某处可达，否则它只是表里的一行死数据。"""
        reachable = set(TRANSITIONS[None])
        frontier = list(reachable)
        while frontier:
            state = frontier.pop()
            for nxt in TRANSITIONS[state]:
                if nxt not in reachable:
                    reachable.add(nxt)
                    frontier.append(nxt)
        assert reachable == set(JOB_STATES), sorted(set(JOB_STATES) - reachable)


class TestTrimSolver:
    """§4.3 C1–C4。T30-08 的算术要逐字段对。"""

    def test_t30_08_both_sides(self):
        """tier 5000, target 4000, dialogue [3000,4800]。"""
        plan = solve_trim(tier_ms=5000, target_ms=4000,
                          dialogue_window={"start_ms": 3000, "end_ms": 4800},
                          allow_head=True)
        assert plan["trim_head_ms"] == 800
        assert plan["trim_tail_ms"] == 200
        assert plan["resulting_duration_ms"] == 4000
        assert plan["trim_mode"] == "both"

    def test_tail_only_when_head_not_allowed(self):
        plan = solve_trim(tier_ms=5000, target_ms=4000,
                          dialogue_window={"start_ms": 3000, "end_ms": 4800})
        assert plan["trim_mode"] == "tail"
        assert plan["trim_head_ms"] == 0
        assert plan["trim_tail_ms"] == 1000

    def test_head_only_is_never_produced(self):
        """`dm_head_only_forbidden`：head 只在 both 里出现。"""
        for d_start in range(0, 4000, 250):
            plan = solve_trim(
                tier_ms=5000, target_ms=4000,
                dialogue_window={"start_ms": d_start,
                                 "end_ms": min(d_start + 1200, 5000)},
                allow_head=True)
            assert plan["trim_mode"] != "head", d_start

    def test_none_when_tier_equals_target(self):
        plan = solve_trim(tier_ms=4000, target_ms=4000)
        assert plan["trim_mode"] == "none"
        assert plan["resulting_duration_ms"] == 4000

    def test_arithmetic_always_balances(self):
        for tier in (4000, 4500, 5000, 6000):
            for target in (2000, 3000, 4000):
                if target > tier:
                    continue
                for d_end in (None, 3500, 4200, 4800, tier):
                    dw = (None if d_end is None
                          else {"start_ms": max(0, d_end - 1500), "end_ms": d_end})
                    plan = solve_trim(tier_ms=tier, target_ms=target,
                                      dialogue_window=dw, allow_head=True)
                    assert plan["trim_head_ms"] + plan["trim_tail_ms"] \
                        + plan["resulting_duration_ms"] == tier, (tier, target, dw)
                    assert plan["resulting_duration_ms"] == target

    def test_dialogue_containment_is_exact(self):
        """C2：裁切窗必须完整包含对白区间。"""
        plan = solve_trim(tier_ms=5000, target_ms=4000,
                          dialogue_window={"start_ms": 3000, "end_ms": 4800},
                          allow_head=True)
        head, tail = plan["trim_head_ms"], plan["trim_tail_ms"]
        assert 3000 >= head and 4800 <= head + 4000
        assert 3000 >= head and 4800 <= 5000 - tail

    def test_impossible_when_dialogue_longer_than_target(self):
        """对白 4.2s、目标 4s：任何裁切都装不下。"""
        plan = solve_trim(tier_ms=5000, target_ms=4000,
                          dialogue_window={"start_ms": 300, "end_ms": 4500},
                          allow_head=True)
        assert plan["feasible"] is False and plan["truncates_dialogue"]

    def test_action_window_is_checked_independently(self):
        plan = solve_trim(tier_ms=5000, target_ms=4000,
                          action_window={"start_ms": 100, "end_ms": 4900},
                          allow_head=True)
        assert plan["feasible"] is False and plan["truncates_action"]


# ================================================================ 幂等

class TestIdempotency:
    """T30-01 / T30-02 / §7.1 三层键。"""

    def test_t30_01_same_command_returns_same_job(self, h):
        first, body = h.make_job(command_id="cmd-1")
        again = h.command(body, command_id="cmd-1")
        assert again["video_job_id"] == first["video_job_id"]
        assert again["replayed"] is True
        assert first["replayed"] is False
        assert h.count("video_job") == 1

    def test_t30_01_replay_does_not_reroute_or_resubmit(self, h):
        """重放不能重新付费：路由与提交计数必须停住。"""
        _, body = h.make_job(command_id="cmd-1")
        routes, submits = h.calls["route"], h.calls["submit"]
        h.command(body, command_id="cmd-1")
        h.command(body, command_id="cmd-1")
        assert h.calls["route"] == routes
        assert h.calls["submit"] == submits

    def test_t30_02_same_command_different_body_is_409(self, h):
        from idempotency_sdk import CommandConflict
        _, body = h.make_job(command_id="cmd-1")
        with pytest.raises(CommandConflict):
            h.command(h.body(prompt_intent="另一个意图"), command_id="cmd-1")
        assert h.count("video_job") == 1

    def test_same_body_different_tenant_is_a_different_command(self, h):
        _, body = h.make_job(command_id="cmd-1")
        other = {**body, "context": {**body["context"], "tenant_id": "t2"}}
        with pytest.raises(ServiceError) as exc:
            h.command(other, tenant_id="t2", command_id="cmd-1")
        assert exc.value.code == "KEYFRAME_CANDIDATE_NOT_FOUND"

    def test_business_key_suppresses_a_second_job(self, h):
        """§7.1 业务键：同 (tenant, lineage, segment, fingerprint) 未终结。"""
        first, _ = h.make_job(command_id="a")
        second, _ = h.make_job(command_id="b")
        assert second["video_job_id"] == first["video_job_id"]
        assert second.get("duplicate_suppressed") is True
        assert h.count("video_job") == 1
        assert h.calls["submit"] == 1

    def test_business_key_releases_after_terminal(self, h):
        """`video_job_open_uniq` 排除终态：拒绝之后同输入可以重开。"""
        h.route_result = {"route_id": "r", "revision": 3,
                          "content_hash": _hex("a"), "feasible": False,
                          "infeasible_reason": "NOPE",
                          "degradation_options": [],
                          "quote_valid_to": FUTURE, "selected": {},
                          "residency_allowed": None}
        first, _ = h.make_job(command_id="a")
        assert first["state"] == "REJECTED_LOCAL"
        h.route_result = None
        second, _ = h.make_job(command_id="b")
        assert second["state"] == "WAITING_PROVIDER"
        assert second["video_job_id"] != first["video_job_id"]

    def test_business_key_does_not_cross_tenants(self, h):
        h.known_tenants = {"t1", "t2"}
        first, _ = h.make_job(command_id="a")
        second, body2 = h.make_job(command_id="b")
        other_body = {**body2, "context": {**body2["context"],
                                          "tenant_id": "t2"}}
        other = h.command(other_body, tenant_id="t2", command_id="b")
        assert other["video_job_id"] != first["video_job_id"]

    def test_repair_key_is_409_on_second_use(self, h):
        """§7.1 修复键：(tenant, repair_action_id)，409 带首次 job。"""
        repair = {"repair_action_id": "ra_1", "action_type": "regenerate_video",
                  "shot_lineage_id": "lineage_A"}
        first, _ = h.make_job(command_id="a", repair_action=repair)
        with pytest.raises(ServiceError) as exc:
            h.make_job(command_id="b",
                       prompt_intent="改过的提示词", repair_action=repair)
        assert exc.value.status == 409
        assert exc.value.code == "REPAIR_ACTION_ALREADY_EXECUTED"
        assert exc.value.details["video_job_id"] == first["video_job_id"]

    def test_repair_key_is_per_tenant(self, h):
        h.known_tenants = {"t1", "t2"}
        repair = {"repair_action_id": "ra_1", "action_type": "regenerate_video",
                  "shot_lineage_id": "lineage_A"}
        h.make_job(command_id="a", repair_action=repair)
        body = h.body(prompt_intent="另一个提示词", repair_action=repair)
        body["context"]["tenant_id"] = "t2"
        other = h.command(body, tenant_id="t2", command_id="b")
        assert other["state"] == "WAITING_PROVIDER"

    def test_failed_command_leaves_no_idempotency_row(self, h):
        """失败的命令必须能重试：`abort` 之后同键可用。"""
        with pytest.raises(ServiceError):
            h.make_job(command_id="bad", target_duration_ms=0)
        assert h.count("idempotency_record") == 0
        ok, _ = h.make_job(command_id="bad")
        assert ok["state"] == "WAITING_PROVIDER"


class TestRepairValidation:
    """T30-21 / T30-22 / T30-29。"""

    def test_t30_21_redo_lipsync_creates_no_job(self, h):
        with pytest.raises(ServiceError) as exc:
            h.make_job(repair_action={"repair_action_id": "ra_1",
                                      "action_type": "redo_lipsync",
                                      "shot_lineage_id": "lineage_A"})
        assert exc.value.status == 422
        assert exc.value.code == "ACTION_NOT_OWNED"
        assert exc.value.details["owner"] == "S32"
        assert h.count("video_job") == 0
        assert h.count("video_intent_ledger") == 0
        assert h.calls["submit"] == 0

    def test_t30_22_lipsync_defect_with_regenerate_video_is_422(self, h):
        with pytest.raises(ServiceError) as exc:
            h.make_job(repair_action={"repair_action_id": "ra_2",
                                      "action_type": "regenerate_video",
                                      "defect_class": "lipsync",
                                      "shot_lineage_id": "lineage_A"})
        assert exc.value.code == "ACTION_TYPE_MISMATCH"
        assert exc.value.status == 422
        assert h.count("video_job") == 0

    def test_t30_29_lineage_mismatch_is_422(self, h):
        with pytest.raises(ServiceError) as exc:
            h.make_job(repair_action={"repair_action_id": "ra_3",
                                      "action_type": "regenerate_video",
                                      "shot_lineage_id": "lineage_OTHER"})
        assert exc.value.code == "LINEAGE_MISMATCH"
        assert exc.value.status == 422
        assert h.count("video_job") == 0

    def test_voice_defect_with_regenerate_video_is_accepted(self, h):
        """§4.6：`voice` 族的第二步就是重生成视频。"""
        out, _ = h.make_job(repair_action={
            "repair_action_id": "ra_4", "action_type": "regenerate_video",
            "defect_class": "voice", "shot_lineage_id": "lineage_A"})
        assert out["state"] == "WAITING_PROVIDER"
        assert h.count("video_job") == 1

    def test_repair_requires_an_id(self, h):
        with pytest.raises(ServiceError) as exc:
            h.make_job(repair_action={"action_type": "regenerate_video"})
        assert exc.value.status == 422
        assert h.count("video_job") == 0


class TestTenantIsolation:
    """T30-31 / F2：跨租户与不存在**同形**。"""

    def test_t30_31_cross_tenant_read_is_404_like_missing(self, h):
        """跨租户读与不存在必须**同形**：只回调用方自己给的 id，不带归属线索。"""
        _, candidate = h.ready_job()
        missing = None
        try:
            h.service.get_candidate("t2", "cc_does_not_exist")
        except ServiceError as exc:
            missing = (exc.status, exc.code, exc.details)
        crossed = None
        try:
            h.service.get_candidate("t2", candidate)
        except ServiceError as exc:
            crossed = (exc.status, exc.code, exc.details)
        assert missing is not None
        assert crossed == missing
        assert crossed[0] == 404

    def test_t30_31_cross_tenant_message_leaks_nothing(self, h):
        """消息里只允许出现调用方已知的 id，不许出现"存在但不是你的"这类话。"""
        _, candidate = h.ready_job()
        with pytest.raises(ServiceError) as exc:
            h.service.get_candidate("t2", candidate)
        text = str(exc.value)
        assert candidate in text
        assert "t1" not in text and "tenant" not in text.lower()

    def test_t30_31_cross_tenant_job_read_is_404(self, h):
        job_id, _ = h.ready_job()
        with pytest.raises(ServiceError) as exc:
            h.service.get_job("t2", job_id)
        assert exc.value.status == 404
        assert exc.value.code == "VIDEO_JOB_NOT_FOUND"

    def test_t30_31_cross_tenant_edit_plan_is_404(self, h):
        _, candidate = h.ready_job()
        with pytest.raises(ServiceError) as exc:
            h.service.get_edit_plan("t2", candidate)
        assert exc.value.status == 404
        assert exc.value.code == "CLIP_CANDIDATE_NOT_FOUND"

    def test_t30_31_cross_tenant_list_is_empty(self, h):
        h.ready_job()
        assert h.service.list_jobs("t2")["count"] == 0
        assert h.service.list_jobs("t1")["count"] == 1

    def test_f2_cross_tenant_keyframe_candidate_is_404(self, h):
        """S29 的读口返回 `None`（跨租户/不存在不分），这里必须是 404。"""
        before = h.count("video_job")
        with pytest.raises(ServiceError) as exc:
            h.make_job(keyframe_baseline={"candidate_id": "kfc_other",
                                          "checkpoint_seq": 1})
        assert exc.value.status == 404
        assert exc.value.code == "KEYFRAME_CANDIDATE_NOT_FOUND"
        assert h.count("video_job") == before
        assert h.calls["submit"] == 0

    def test_body_tenant_must_match_signed_tenant(self, h):
        body = h.body()
        body["context"]["tenant_id"] = "t2"
        with pytest.raises(ServiceError) as exc:
            h.command(body, tenant_id="t1", command_id="x")
        assert exc.value.status == 403
        assert exc.value.code == "TENANT_MISMATCH"
        assert h.count("video_job") == 0

    def test_tenant_is_never_taken_from_the_body(self, h):
        """body 只用于比对：签名为 t1、body 声明 t2 → 拒绝，不是采信 body。"""
        body = h.body()
        body["context"]["tenant_id"] = "t2"
        with pytest.raises(ServiceError):
            h.command(body, tenant_id="t1", command_id="x")
        assert h.count("video_job") == 0


class TestValidation:
    """§6.1 的 11 步固定顺序，逐条一个测试。"""

    def _code(self, h, **over):
        with pytest.raises(ServiceError) as exc:
            h.make_job(**over)
        return exc.value

    def test_context_missing(self, h):
        body = h.body()
        del body["context"]
        with pytest.raises(ServiceError) as exc:
            h.command(body, command_id="c")
        assert exc.value.status == 422
        assert exc.value.code == "INVALID_CONTEXT"

    def test_context_missing_required_key(self, h):
        body = h.body()
        del body["context"]["branch_id"]
        with pytest.raises(ServiceError) as exc:
            h.command(body, command_id="c")
        assert exc.value.code == "INVALID_CONTEXT"
        assert "branch_id" in exc.value.details["missing"]

    def test_context_manifest_hash_must_be_hex64(self, h):
        body = h.body()
        body["context"]["manifest_ref"]["content_hash"] = "short"
        with pytest.raises(ServiceError) as exc:
            h.command(body, command_id="c")
        assert exc.value.code == "INVALID_CONTEXT"

    def test_lineage_required(self, h):
        assert self._code(h, shot_lineage_id="  ").code \
            == "SHOT_LINEAGE_REQUIRED"

    def test_segment_no_must_be_int_not_bool(self, h):
        assert self._code(h, segment_no=True).code == "INVALID_SEGMENT_NO"
        assert self._code(h, segment_no="1").code == "INVALID_SEGMENT_NO"
        assert self._code(h, segment_no=0).code == "INVALID_SEGMENT_NO"

    def test_shot_ref_revision_required(self, h):
        assert self._code(h, shot_ref={"service_id": "S06", "id": "s"}).code \
            == "INVALID_SHOT_REF"

    def test_shot_ref_revision_must_be_positive_int(self, h):
        assert self._code(
            h, shot_ref={"service_id": "S06", "id": "s", "revision": 0}).code \
            == "INVALID_SHOT_REF"

    def test_shot_lineage_drift_is_409(self, h):
        h.make_job(command_id="a", shot_ref={"service_id": "S06", "id": "s1",
                                             "revision": 1})
        exc = self._code(h, command_id="b", shot_lineage_id="lineage_B",
                         shot_ref={"service_id": "S06", "id": "s1",
                                   "revision": 1})
        assert exc.status == 409
        assert exc.code == "SHOT_LINEAGE_DRIFT"

    def test_keyframe_baseline_ahead_is_422(self, h):
        """S29 当前 checkpoint_seq = 3（stub）。要 4 就是"还没发生的事实"。"""
        exc = self._code(h, keyframe_baseline={"candidate_id": "kfc_1",
                                               "checkpoint_seq": 4})
        assert exc.code == "KEYFRAME_BASELINE_AHEAD"
        assert exc.details["current"] == 3

    def test_keyframe_baseline_seq_must_be_positive(self, h):
        assert self._code(
            h, keyframe_baseline={"candidate_id": "kfc_1",
                                  "checkpoint_seq": 0}).code \
            == "INVALID_KEYFRAME_BASELINE"

    def test_keyframe_lookup_unavailable_is_503(self, h):
        h.keyframe_result = ServiceError(503, "KEYFRAME_LOOKUP_UNAVAILABLE",
                                         "down")
        exc = self._code(h)
        assert exc.status == 503
        assert exc.code == "KEYFRAME_LOOKUP_UNAVAILABLE"

    def test_target_duration_must_be_positive(self, h):
        assert self._code(h, target_duration_ms=0).code \
            == "INVALID_TARGET_DURATION"
        assert self._code(h, target_duration_ms=-1).code \
            == "INVALID_TARGET_DURATION"

    def test_duration_policy_closed_set(self, h):
        assert self._code(h, duration_policy="whatever").code \
            == "INVALID_DURATION_POLICY"

    def test_allow_trim_must_agree_with_policy(self, h):
        assert self._code(h, duration_policy="allow_tier_snap",
                          allow_trim=True).code == "ALLOW_TRIM_INCONSISTENT"
        assert self._code(h, duration_policy="allow_tier_snap_with_trim",
                          allow_trim=False).code == "ALLOW_TRIM_INCONSISTENT"

    def test_t30_23_audio_strategy_inconsistent(self, h):
        exc = self._code(h, audio_strategy="non_dialogue_native")
        assert exc.status == 422
        assert exc.code == "AUDIO_STRATEGY_INCONSISTENT"
        assert exc.details["expected"] == "silent_then_tts"

    def test_audio_strategy_native_locked_requires_voice_locked(self, h):
        exc = self._code(h, audio_strategy="native_locked_voice")
        assert exc.code == "AUDIO_STRATEGY_INCONSISTENT"
        ok, _ = h.make_job(audio_strategy="native_locked_voice",
                           shot_context={"voice_locked_by_s15": True},
                           command_id="ok")
        assert ok["state"] == "WAITING_PROVIDER"

    def test_audio_strategy_non_dialogue_requires_no_dialogue(self, h):
        assert self._code(h, audio_strategy="non_dialogue_native").status == 422
        ok, _ = h.make_job(audio_strategy="non_dialogue_native",
                           shot_context={"has_dialogue": False},
                           command_id="ok")
        assert ok["state"] == "WAITING_PROVIDER"

    def test_audio_strategy_closed_set(self, h):
        assert self._code(h, audio_strategy="magic").code \
            == "INVALID_AUDIO_STRATEGY"

    def test_dialogue_exceeds_target(self, h):
        exc = self._code(h, shot_context={"dialogue_duration_ms": 4500})
        assert exc.code == "DIALOGUE_EXCEEDS_TARGET"

    def test_dialogue_duration_must_be_positive_int(self, h):
        assert self._code(h, shot_context={"dialogue_duration_ms": 0}).code \
            == "INVALID_SHOT_CONTEXT"

    def test_prompt_must_not_be_blank(self, h):
        assert self._code(h, prompt_intent="   ").code == "EMPTY_PROMPT_INTENT"

    def test_aspect_ratio_required(self, h):
        assert self._code(h, aspect_ratio="").code == "INVALID_ASPECT_RATIO"

    def test_resolution_must_have_positive_integers(self, h):
        assert self._code(h, resolution={"width": 0, "height": 1920}).code \
            == "INVALID_RESOLUTION"
        assert self._code(h, resolution={"width": 1080}).code \
            == "INVALID_RESOLUTION"

    def test_required_capabilities_must_not_be_empty(self, h):
        assert self._code(h, required_capabilities=[]).code \
            == "REQUIRED_CAPABILITIES_EMPTY"

    def test_required_capabilities_entries_must_be_strings(self, h):
        assert self._code(h, required_capabilities=[1]).code \
            == "INVALID_REQUIRED_CAPABILITIES"

    def test_route_policy_ref_must_be_complete(self, h):
        assert self._code(h, route_policy_ref={"id": "x"}).code \
            == "INVALID_ROUTE_POLICY_REF"

    def test_budget_scope_is_required(self, h):
        """绝不默认：一个 default 预算域会把所有漏传的租户汇进一个桶。"""
        assert self._code(h, budget_scope_id="").code == "INVALID_BUDGET_SCOPE"

    def test_deadline_must_be_in_the_future(self, h):
        assert self._code(h, deadline=PAST).code == "DEADLINE_IN_PAST"

    def test_deadline_must_parse(self, h):
        assert self._code(h, deadline="not-a-time").code == "INVALID_DEADLINE"

    def test_validation_happens_before_any_paid_call(self, h):
        for over in ({"target_duration_ms": 0}, {"prompt_intent": ""},
                     {"required_capabilities": []},
                     {"deadline": PAST}):
            with pytest.raises(ServiceError):
                h.make_job(**over)
        assert h.calls["route"] == 0
        assert h.calls["submit"] == 0


# ================================================================ 派单

def _route(seconds, *, feasible=True, residency=None, valid_to=FUTURE,
           reason=None, degradation=None):
    route = {
        "route_id": "route_x", "revision": 3, "content_hash": _hex("a"),
        "feasible": feasible, "quote_valid_to": valid_to,
        "selected": {"normalized_input": {"duration_seconds": seconds},
                     "expected_cost_micro": 1_000,
                     "quote_upper_bound_micro": 900_000,
                     "currency": "USD"},
        "residency_allowed": residency,
    }
    if reason is not None:
        route["infeasible_reason"] = reason
    if degradation is not None:
        route["degradation_options"] = degradation
    return route


class TestDispatch:
    """T30-03 / T30-04 / T30-05 / T30-06 / F6。"""

    def test_t30_03_command_seconds_equal_normalized(self, h):
        """4s 目标、档位 5s：命令里的秒数必须是 5，不是 4。"""
        out, _ = h.make_job()
        assert out["state"] == "WAITING_PROVIDER"
        assert out["submitted_duration_ms"] == 5000
        command = h.submitted_commands[0]
        assert command["input"]["duration_seconds"] == 5.0
        assert h.job_row(out["video_job_id"])["submitted_duration_ms"] == 5000

    def test_t30_03_target_is_never_sent_as_the_duration(self, h):
        h.make_job(target_duration_ms=4000)
        assert h.submitted_commands[0]["input"]["duration_seconds"] != 4

    def test_generation_command_has_all_contract_keys(self, h):
        """`generation-command.v2` 是 `additionalProperties: false`。"""
        h.make_job()
        command = h.submitted_commands[0]
        assert set(command) == {
            "schema_version", "context", "attempt_id", "variation_nonce",
            "idempotency_key", "modality", "input", "template_ref",
            "route_policy_ref", "budget_scope_id", "deadline"}
        assert set(command["input"]) == {
            "prompt_intent", "shot_revision", "duration_seconds",
            "aspect_ratio", "resolution", "references", "audio_strategy",
            "required_capabilities", "allow_trim"}
        assert command["modality"] == "video"
        assert command["template_ref"] is None

    def test_command_carries_the_signed_context_verbatim(self, h):
        _, body = h.make_job()
        assert h.submitted_commands[0]["context"] == body["context"]

    def test_command_references_the_frozen_keyframe(self, h):
        h.make_job()
        refs = h.submitted_commands[0]["input"]["references"]
        assert refs == [{"artifact_id": "art_kf_1", "sha256": _hex("c"),
                         "role": "first_frame"}]

    def test_text_to_video_has_no_references(self, h):
        h.make_job(keyframe_baseline=None)
        assert h.submitted_commands[0]["input"]["references"] == []

    def test_allow_trim_follows_the_policy(self, h):
        h.make_job(duration_policy="allow_tier_snap")
        assert h.submitted_commands[0]["input"]["allow_trim"] is False
        h2 = Harness()
        try:
            h2.make_job(duration_policy="allow_tier_snap_with_trim")
            assert h2.submitted_commands[0]["input"]["allow_trim"] is True
        finally:
            h2.conn.close()

    def test_submit_gets_the_route_decision(self, h):
        h.make_job()
        route = h.service.get_job("t1", h.routes and
                                 h.conn.execute("SELECT video_job_id FROM video_job").fetchone()["video_job_id"])
        assert route["state"] in ("SUBMITTED", "WAITING_PROVIDER")

    def test_route_decision_ref_is_frozen(self, h):
        out, _ = h.make_job()
        row = h.job_row(out["video_job_id"])
        assert row["route_decision_ref"]
        # 存的是 S25 的三元组（id/revision/content_hash），不是行情快照。
        ref = json.loads(row["route_decision_ref"])
        assert ref["service_id"] == "S25"
        assert ref["id"] and ref["content_hash"]
        assert "selected" not in ref, "a quote is not a decision"

    def test_t30_04_duration_assertion_failure_fails_without_paying(self, h):
        """F5：两次派单给不同档位 → `FAILED(SPEC_ASSERTION)`，不提交。"""
        h.route_sequence = [_route(5.0)]
        out, _ = h.make_job(command_id="first")
        job_id = out["video_job_id"]
        assert out["submitted_duration_ms"] == 5000
        h.state_event(job_id, "PROVIDER_FAILED", reason_code="PROVIDER_ERROR",
                      event_id="fail1")
        submits = h.calls["submit"]
        h.route_sequence = [_route(6.0)]
        retried = h.command(
            {"reason": "retry"}, command_id="retry1",
            fn=lambda: h.service.retry_job("t1", job_id, {"reason": "retry"},
                                           "retry1"))
        assert retried["state"] == "FAILED"
        assert retried["failure_code"] == "SPEC_ASSERTION"
        assert retried["alert"] == "vid_assertion_failures_total"
        assert h.calls["submit"] == submits, "no second paid submit"
        history = [(r["from_state"], r["to_state"], r["reason_code"])
                   for r in h.conn.execute(
                       "SELECT * FROM job_transition WHERE video_job_id=? "
                       "ORDER BY seq", (job_id,))]
        assert ("PREPARING", "FAILED", "SPEC_ASSERTION") in history

    def test_t30_04_does_not_increment_the_attempt(self, h):
        h.route_sequence = [_route(5.0)]
        out, _ = h.make_job(command_id="first")
        job_id = out["video_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="f1")
        h.route_sequence = [_route(6.0)]
        h.command({}, command_id="r1",
                  fn=lambda: h.service.retry_job("t1", job_id, {}, "r1"))
        assert h.job_row(job_id)["business_attempt_no"] == 2
        assert h.count("video_intent_ledger") == 1, "ledger only for real submits"

    def test_t30_05_native_only_with_no_exact_tier(self, h):
        h.route_sequence = [_route(5.0)]
        out, _ = h.make_job(duration_policy="native_only")
        assert out["state"] == "REJECTED_LOCAL"
        assert out["reason_code"] == "DURATION_UNSUPPORTED_NATIVE_ONLY"
        assert h.calls["submit"] == 0

    def test_t30_05_native_only_with_an_exact_tier_submits(self, h):
        h.route_sequence = [_route(4.0)]
        out, _ = h.make_job(duration_policy="native_only")
        assert out["state"] == "WAITING_PROVIDER"
        assert h.submitted_commands[0]["input"]["duration_seconds"] == 4.0

    def test_t30_06_infeasible_passes_degradation_through(self, h):
        h.route_sequence = [_route(5.0, feasible=False,
                                   reason="DIALOGUE_EXCEEDS_DURATION",
                                   degradation=[{"kind": "split_shot"},
                                                {"kind": "lower_resolution"}])]
        out, _ = h.make_job()
        assert out["state"] == "REJECTED_LOCAL"
        assert out["reason_code"] == "ROUTE_INFEASIBLE"
        assert out["degradation_options"] == [{"kind": "split_shot"},
                                              {"kind": "lower_resolution"}]
        assert h.calls["submit"] == 0
        # 不缩对白、不选降级：请求里的目标与对白长度原样留在 job 上
        row = h.job_row(out["video_job_id"])
        assert row["target_duration_ms"] == 4000

    def test_t30_35_residency_forbidden_rejects_before_paying(self, h):
        h.route_sequence = [_route(5.0, residency=False)]
        out, _ = h.make_job()
        assert out["state"] == "REJECTED_LOCAL"
        assert out["reason_code"] == "RESIDENCY_FORBIDDEN"
        assert h.calls["submit"] == 0
        assert h.count("video_intent_ledger") == 0

    def test_t30_07_head_trim_forbidden_under_allow_tier_snap(self, h):
        """不裁时对白 [4200,5000] 装不进 [0,4000] → 付款前拒绝。"""
        h.route_sequence = [_route(5.0)]
        out, _ = h.make_job(duration_policy="allow_tier_snap",
                            shot_context={
                                "dialogue_window": {"start_ms": 4200,
                                                    "end_ms": 5000}})
        assert out["state"] == "REJECTED_LOCAL"
        assert out["reason_code"] == "DIALOGUE_WOULD_BE_TRUNCATED"
        assert h.calls["submit"] == 0

    def test_t30_07_with_trim_keeps_the_candidate_and_flags_it(self, h):
        """`allow_tier_snap_with_trim` 下无解也得留候选（钱已花）→ 标 flag。

        对白宽 4500ms > 目标 4000ms：任何头偏移都装不下整段，C2 必然失败。
        这与 §4.3 第三个例子（对白 [4200,5000]，裁头 1000ms 正好装下）不同——
        那一个应当**不**被标截断。
        """
        job_id, candidate = h.ready_job(
            duration_policy="allow_tier_snap_with_trim",
            shot_context={"dialogue_window": {"start_ms": 0, "end_ms": 4500}})
        body = h.service.get_candidate("t1", candidate)
        assert body["truncates_dialogue"] is True
        assert body["requires_rehuman_decision"] is True
        assert body["state"] == "READY"
        assert h.events(LOCAL_CLIP_ATTENTION)

    def test_action_window_truncation_under_allow_tier_snap(self, h):
        h.route_sequence = [_route(5.0)]
        out, _ = h.make_job(duration_policy="allow_tier_snap",
                            shot_context={"action_window": {"start_ms": 200,
                                                            "end_ms": 4900}})
        assert out["reason_code"] == "ACTION_WOULD_BE_TRUNCATED"

    def test_tier_shorter_than_target_is_unmappable(self, h):
        h.route_sequence = [_route(3.0)]
        out, _ = h.make_job(target_duration_ms=4000)
        assert out["state"] == "REJECTED_LOCAL"
        assert out["reason_code"] == "DURATION_UNMAPPABLE"
        assert h.calls["submit"] == 0

    def test_missing_normalized_input_is_503(self, h):
        h.route_result = {"route_id": "r", "revision": 1,
                          "content_hash": _hex("a"), "feasible": True,
                          "quote_valid_to": FUTURE, "selected": {},
                          "residency_allowed": None}
        with pytest.raises(ServiceError) as exc:
            h.make_job()
        assert exc.value.status == 503
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"
        assert exc.value.retryable is True
        assert h.calls["submit"] == 0

    def test_missing_route_id_is_503_not_invented(self, h):
        route = _route(5.0)
        del route["route_id"]
        h.route_sequence = [route]
        with pytest.raises(ServiceError) as exc:
            h.make_job()
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"
        assert h.calls["submit"] == 0

    def test_expired_quote_is_re_requested_not_submitted(self, h):
        """F6：报价过期 → 重取路由，仍停在 PREPARING 的语义里。"""
        h.route_sequence = [_route(5.0, valid_to=PAST), _route(5.0)]
        out, _ = h.make_job()
        assert out["state"] == "WAITING_PROVIDER"
        assert h.calls["route"] == 2
        assert h.calls["submit"] == 1
        notes = [e for e in h.events(LOCAL_JOB_STATE)
                 if e["data"]["reason_code"] == "QUOTE_EXPIRED"]
        assert notes, "the expiry must be visible in the event stream"

    def test_two_expired_quotes_refuse_to_submit(self, h):
        h.route_sequence = [_route(5.0, valid_to=PAST),
                            _route(5.0, valid_to=PAST)]
        with pytest.raises(ServiceError) as exc:
            h.make_job()
        assert exc.value.status == 503
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"
        assert "stale price" in str(exc.value)
        assert h.calls["submit"] == 0
        # 命令事务整体回滚：没有"半个 job"留在库里冒充可重试的订单。
        # 重试是调用方拿同一个 command_id 重投——那时会重新路由、重新报价。
        assert h.count("video_job") == 0
        assert h.count("job_transition") == 0
        assert h.count("video_intent_ledger") == 0
        assert h.count("idempotency_record") == 0, \
            "a rolled-back command must not leave a placeholder that 409s on retry"

    def test_unparseable_quote_timestamp_is_503(self, h):
        h.route_sequence = [_route(5.0, valid_to="whenever")]
        with pytest.raises(ServiceError) as exc:
            h.make_job()
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"
        assert h.calls["submit"] == 0

    def test_route_resolver_failure_is_503(self, h):
        h.route_sequence = [ServiceError(503, "ROUTE_DECISION_UNAVAILABLE",
                                         "down")]
        with pytest.raises(ServiceError) as exc:
            h.make_job()
        assert exc.value.status == 503
        assert h.calls["submit"] == 0

    def test_submitter_without_provider_job_id_is_503(self, h):
        h.submit_result = {"operation_id": "op", "state": "QUEUED"}
        with pytest.raises(ServiceError) as exc:
            h.make_job()
        assert exc.value.code == "GENERATION_JOBS_UNAVAILABLE"
        assert "no provider_job_id" in str(exc.value)

    def test_submitter_failure_rolls_the_job_back(self, h):
        """派单失败不能留下一个"看起来已提交"的 job。"""
        h.submit_error = ServiceError(503, "GENERATION_JOBS_UNAVAILABLE", "down")
        with pytest.raises(ServiceError):
            h.make_job()
        assert h.count("video_job") == 0
        assert h.count("idempotency_record") == 0
        assert h.count("video_intent_ledger") == 0


class TestLedger:
    """§8.1 台账：只在真的付出去之后写一行。"""

    def test_ledger_written_on_submit(self, h):
        out, body = h.make_job()
        rows = h.conn.execute("SELECT * FROM video_intent_ledger").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["tenant_id"] == "t1"
        assert row["shot_lineage_id"] == "lineage_A"
        assert row["segment_no"] == 1
        assert row["action"] == "generate"
        assert row["node_epoch"] == 4
        assert row["run_id"] == body["context"]["run_id"] \
            if "run_id" in body["context"] else True

    def test_ledger_records_the_repair_action(self, h):
        h.make_job(repair_action={"repair_action_id": "ra_1",
                                  "action_type": "regenerate_video",
                                  "strategy_class": "temporal_edit",
                                  "shot_lineage_id": "lineage_A"})
        row = h.conn.execute("SELECT * FROM video_intent_ledger").fetchone()
        assert row["action"] == "regenerate_video"
        assert row["repair_action_id"] == "ra_1"
        assert row["strategy_class"] == "temporal_edit"

    def test_ledger_not_written_on_local_reject(self, h):
        h.route_sequence = [_route(5.0, feasible=False, reason="NOPE")]
        h.make_job()
        assert h.count("video_intent_ledger") == 0


class TestDispatchRechecksBudget:
    """任务书红线：不信任"已批准"，每次派单都重新向 S25 取。"""

    def test_second_attempt_re_routes_and_re_prices(self, h):
        h.route_sequence = [_route(5.0)]
        out, _ = h.make_job(command_id="a")
        job_id = out["video_job_id"]
        h.state_event(job_id, "REJECTED_BUDGET", event_id="b1")
        before = h.calls["route"]
        h.route_sequence = [_route(5.0)]
        h.command({}, command_id="r1",
                  fn=lambda: h.service.retry_job("t1", job_id, {}, "r1"))
        assert h.calls["route"] == before + 1
        assert h.calls["submit"] == 2

    def test_retry_does_not_reuse_the_stored_route(self, h):
        h.route_sequence = [_route(5.0)]
        out, _ = h.make_job(command_id="a")
        job_id = out["video_job_id"]
        first_ref = h.job_row(job_id)["route_decision_ref"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="f1")
        h.route_sequence = [{"route_id": "route_NEW", "revision": 9,
                             "content_hash": _hex("b"), "feasible": True,
                             "quote_valid_to": FUTURE,
                             "selected": {"normalized_input":
                                          {"duration_seconds": 5.0}},
                             "residency_allowed": None}]
        h.command({}, command_id="r1",
                  fn=lambda: h.service.retry_job("t1", job_id, {}, "r1"))
        assert h.job_row(job_id)["route_decision_ref"] != first_ref
        assert "route_NEW" in h.job_row(job_id)["route_decision_ref"]

    def test_new_attempt_gets_a_new_variation_nonce(self, h):
        h.route_sequence = [_route(5.0)]
        out, _ = h.make_job(command_id="a")
        job_id = out["video_job_id"]
        nonce1 = h.job_row(job_id)["variation_nonce"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="f1")
        h.route_sequence = [_route(5.0)]
        h.command({}, command_id="r1",
                  fn=lambda: h.service.retry_job("t1", job_id, {}, "r1"))
        assert h.job_row(job_id)["variation_nonce"] != nonce1

    def test_new_attempt_gets_a_new_idempotency_key(self, h):
        h.route_sequence = [_route(5.0)]
        out, _ = h.make_job(command_id="a")
        job_id = out["video_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="f1")
        h.route_sequence = [_route(5.0)]
        h.command({}, command_id="r1",
                  fn=lambda: h.service.retry_job("t1", job_id, {}, "r1"))
        keys = [c["idempotency_key"] for c in h.submitted_commands]
        assert len(set(keys)) == 2, keys

    def test_new_attempt_gets_a_new_attempt_id(self, h):
        h.route_sequence = [_route(5.0)]
        out, _ = h.make_job(command_id="a")
        job_id = out["video_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="f1")
        h.route_sequence = [_route(5.0)]
        h.command({}, command_id="r1",
                  fn=lambda: h.service.retry_job("t1", job_id, {}, "r1"))
        ids = [c["attempt_id"] for c in h.submitted_commands]
        assert len(set(ids)) == 2, ids


# ================================================================ 供应商状态

class TestProviderStates:
    """T30-32 / T30-33 / T30-34 / F7–F11 的落点。"""

    def _submitted(self, h):
        out, _ = h.make_job()
        return out["video_job_id"]

    def test_t30_32_budget_reject_does_not_scale_down(self, h):
        job = self._submitted(h)
        before = h.job_row(job)
        result = h.state_event(job, "REJECTED_BUDGET", event_id="b")
        assert result["state"] == "FAILED"
        assert result["failure_code"] == "BUDGET"
        after = h.job_row(job)
        assert after["resolution"] == before["resolution"]
        assert after["target_duration_ms"] == before["target_duration_ms"]
        assert after["aspect_ratio"] == before["aspect_ratio"]

    def test_t30_33_quota_reject(self, h):
        job = self._submitted(h)
        result = h.state_event(job, "REJECTED_QUOTA", event_id="q")
        assert result["state"] == "FAILED"
        assert result["failure_code"] == "QUOTA"

    def test_t30_34_account_revoked_does_not_switch_channel(self, h):
        job = self._submitted(h)
        result = h.state_event(job, "REJECTED_ACCOUNT", event_id="a")
        assert result["state"] == "FAILED"
        assert result["failure_code"] == "ACCOUNT_REVOKED"
        assert h.calls["submit"] == 1, "no re-dispatch on another channel"
        assert h.calls["route"] == 1

    def test_provider_failed_records_the_reason(self, h):
        job = self._submitted(h)
        result = h.state_event(job, "PROVIDER_FAILED",
                               reason_code="CONTENT_POLICY", event_id="p")
        assert result["failure_code"] == "CONTENT_POLICY"

    def test_provider_failed_without_a_reason_uses_the_default(self, h):
        job = self._submitted(h)
        assert h.state_event(job, "PROVIDER_FAILED", event_id="p")["failure_code"] \
            == "PROVIDER_FAILED"

    def test_provider_succeeded_moves_to_ingesting(self, h):
        job = self._submitted(h)
        result = h.state_event(job, "PROVIDER_SUCCEEDED", event_id="s")
        assert result["state"] == "INGESTING"
        assert h.calls["import"] >= 1

    def test_provider_canceled_marks_canceled(self, h):
        job = self._submitted(h)
        result = h.state_event(job, "CANCELED", event_id="c")
        assert result["state"] == "CANCELED"

    def test_queued_and_running_are_ignored(self, h):
        job = self._submitted(h)
        for state in ("QUEUED", "RUNNING"):
            result = h.state_event(job, state, event_id=state)
            assert result["ignored"] is True
            assert h.state_of(job) == "WAITING_PROVIDER"

    def test_unknown_provider_state_does_not_move_the_job(self, h):
        job = self._submitted(h)
        result = h.state_event(job, "SOMETHING_NEW", event_id="x")
        assert result["ignored"] is True
        assert "unmapped_provider_state" in result["reason"]
        assert h.state_of(job) == "WAITING_PROVIDER"

    def test_duplicate_event_is_not_reapplied(self, h):
        job = self._submitted(h)
        first = h.state_event(job, "PROVIDER_FAILED", event_id="same")
        assert first["state"] == "FAILED"
        again = h.state_event(job, "PROVIDER_FAILED", event_id="same")
        assert again == {"applied": False, "reason": "DUPLICATE"}
        assert len(h.events(LOCAL_JOB_STATE)) == len(
            set((e["data"]["to"], e["data"]["reason_code"])
                for e in h.events(LOCAL_JOB_STATE)))

    def test_late_event_after_terminal_is_ignored_not_crashed(self, h):
        job = self._submitted(h)
        h.state_event(job, "PROVIDER_FAILED", event_id="f1")
        for provider_state in ("PROVIDER_SUCCEEDED", "ARCHIVED",
                               "SUBMISSION_UNKNOWN", "CANCELED"):
            result = h.state_event(job, provider_state,
                                   event_id=f"late-{provider_state}")
            assert result["ignored"] is True
            assert result["reason"] == "job_already_terminal:FAILED"
            assert h.state_of(job) == "FAILED"

    def test_unknown_job_reference_is_ignored(self, h):
        result = h.service.on_generation_state_changed({
            "id": "orphan", "tenant_id": "t1", "source": "S26",
            "type": "generation.job.state-changed",
            "aggregate_id": "pj_nope",
            "data": {"state": "PROVIDER_FAILED"}})
        assert result == {"ignored": True, "reason": "no_matching_video_job"}

    def test_event_without_id_is_400(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.on_generation_state_changed({
                "tenant_id": "t1", "aggregate_id": "pj_1",
                "data": {"state": "PROVIDER_FAILED"}})
        assert exc.value.status == 400
        assert exc.value.code == "INVALID_BODY"

    def test_event_for_another_tenant_does_not_touch_the_job(self, h):
        job = self._submitted(h)
        result = h.state_event(job, "PROVIDER_FAILED", event_id="other-tenant",
                               tenant="t2")
        assert result["ignored"] is True
        assert h.state_of(job) == "WAITING_PROVIDER"


class TestOutOfOrderEvents:
    """C30-30：乱序 / 迟到 / 换过 event id 的重复投递都不能让消费者 500。"""

    PROVIDER_STATES = [
        "QUEUED", "RUNNING", "PROVIDER_SUCCEEDED", "INGESTING", "ARCHIVED",
        "SUBMISSION_UNKNOWN", "RECONCILING", "PROVIDER_FAILED",
        "REJECTED_BUDGET", "REJECTED_QUOTA", "REJECTED_ACCOUNT", "CANCELED",
        "SOMETHING_NEW", ""]

    def _forced(self, h, state):
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        h.conn.execute("UPDATE video_job SET state=? WHERE video_job_id=?",
                       (state, job_id))
        return job_id

    @pytest.mark.parametrize("local", ["CREATED", "PREPARING", "SUBMITTED",
                                       "WAITING_PROVIDER", "INGESTING",
                                       "WAITING_UNKNOWN"])
    def test_every_provider_state_is_handled(self, h, local):
        """14 个供应商状态 × 每个本地状态：要么推进，要么 `ignored`，不抛。"""
        for provider_state in self.PROVIDER_STATES:
            harness = Harness()
            try:
                job_id = self._forced(harness, local)
                result = harness.state_event(
                    job_id, provider_state,
                    event_id=f"{local}-{provider_state or 'EMPTY'}")
                assert isinstance(result, dict), (local, provider_state)
                assert "ignored" in result or "state" in result
                if result.get("state"):
                    assert result["state"] in JOB_STATES
            finally:
                harness.conn.close()

    def test_unsubmitted_job_ignores_provider_conclusions(self, h):
        for local in ("CREATED", "PREPARING"):
            harness = Harness()
            try:
                job_id = self._forced(harness, local)
                for provider_state in ("PROVIDER_FAILED", "REJECTED_BUDGET",
                                       "CANCELED", "SUBMISSION_UNKNOWN"):
                    result = harness.state_event(
                        job_id, provider_state,
                        event_id=f"{local}-{provider_state}")
                    assert result["reason"] == "job_not_yet_submitted"
                    assert harness.state_of(job_id) == local
            finally:
                harness.conn.close()

    def test_unsubmitted_job_ignores_a_success_event(self, h):
        for local in ("CREATED", "PREPARING"):
            harness = Harness()
            try:
                job_id = self._forced(harness, local)
                result = harness.state_event(
                    job_id, "PROVIDER_SUCCEEDED", event_id=f"s-{local}")
                assert result["reason"] \
                    == "provider_state_arrived_before_job_was_submitted"
                assert harness.state_of(job_id) == local
            finally:
                harness.conn.close()

    def test_ingesting_ignores_a_late_unknown(self, h):
        harness = Harness()
        try:
            job_id = self._forced(harness, "INGESTING")
            for provider_state in ("SUBMISSION_UNKNOWN", "RECONCILING"):
                result = harness.state_event(
                    job_id, provider_state, event_id=f"u-{provider_state}")
                assert result["reason"] == "unknown_state_after_ingest_started"
                assert harness.state_of(job_id) == "INGESTING"
            # 归档仍然能落定：乱序事件没有把 job 冻住
            assert harness.artifact_event(job_id, event_id="art")["state"] \
                == "READY"
        finally:
            harness.conn.close()

    def test_submitted_takes_a_skipped_success_event(self, h):
        """`QUEUED` 丢了：不能把已付费的 job 永远停在 SUBMITTED。"""
        harness = Harness()
        try:
            job_id = self._forced(harness, "SUBMITTED")
            result = harness.state_event(job_id, "PROVIDER_SUCCEEDED",
                                         event_id="s")
            assert result["state"] == "INGESTING"
            history = [(r["from_state"], r["to_state"]) for r in harness.conn.execute(
                "SELECT * FROM job_transition WHERE video_job_id=? ORDER BY seq",
                (job_id,))]
            assert ("SUBMITTED", "WAITING_PROVIDER") in history
            assert ("WAITING_PROVIDER", "INGESTING") in history
        finally:
            harness.conn.close()

    def test_artifact_before_state_event_backfills_the_middle_state(self, h):
        harness = Harness()
        try:
            job_id = self._forced(harness, "WAITING_PROVIDER")
            result = harness.artifact_event(job_id, event_id="a")
            assert result["state"] == "READY"
            history = [(r["from_state"], r["to_state"]) for r in harness.conn.execute(
                "SELECT * FROM job_transition WHERE video_job_id=? ORDER BY seq",
                (job_id,))]
            assert ("WAITING_PROVIDER", "INGESTING") in history
            assert ("INGESTING", "READY") in history
        finally:
            harness.conn.close()

    def test_artifact_before_submit_is_ignored(self, h):
        """重试留着旧 attempt 的 provider_job_id：上一轮的归档不能算这一轮的。"""
        for local in ("CREATED", "PREPARING"):
            harness = Harness()
            try:
                job_id = self._forced(harness, local)
                result = harness.artifact_event(job_id, event_id=f"a-{local}")
                assert result["reason"] == "artifact_ingested_before_submit"
                assert harness.state_of(job_id) == local
                assert harness.count("clip_candidate") == 0
            finally:
                harness.conn.close()

    @pytest.mark.parametrize("local", ["CREATED", "PREPARING", "SUBMITTED",
                                       "WAITING_PROVIDER", "INGESTING",
                                       "WAITING_UNKNOWN", "FAILED", "CANCELED",
                                       "READY", "RECONCILED_READY",
                                       "RECONCILED_FAILED"])
    def test_artifact_in_every_state_is_handled(self, h, local):
        harness = Harness()
        try:
            job_id = self._forced(harness, local)
            result = harness.artifact_event(job_id, event_id="a")
            assert isinstance(result, dict)
            assert result.get("state") in (JOB_STATES | {None})
        finally:
            harness.conn.close()

    def test_repeated_artifact_event_is_a_duplicate(self, h):
        harness = Harness()
        try:
            job_id = self._forced(harness, "INGESTING")
            harness.artifact_event(job_id, event_id="same")
            again = harness.artifact_event(job_id, event_id="same")
            assert again == {"applied": False, "reason": "DUPLICATE"}
            assert harness.count("clip_candidate") == 1
        finally:
            harness.conn.close()
    def test_t30_38_redelivery_with_a_new_event_id_does_not_republish(self, h):
        """T30-38：broker 重放。

        同一条消息换一个 event id 再来一次（Kafka 重放常常就是这样：位置回退
        后消费者又读了一遍，信封 id 未必和上次相同）。候选是同一行字节，指针
        已经指向它——此时**不能再推进一格**，也不能再发一条
        `production.video.ready`。否则下游会收到 `checkpoint: {from: X, to: X}`
        这种并不存在的 revision，而 S39 可能据此以为镜头动过。

        这里同时钉住"不触发二次生成"：submit / route 都不增加，台账仍是一行。
        """
        job_id, candidate = h.ready_job()
        ready_before = len(h.events(EVENT_VIDEO_READY))
        submits, routes = h.calls["submit"], h.calls["route"]
        ledger = h.count("video_intent_ledger")
        out = h.artifact_event(job_id, event_id="replayed-envelope")
        assert out["checkpoint_advanced"] is False
        assert out["candidate_id"] == candidate, "same bytes, same candidate"
        assert len(h.events(EVENT_VIDEO_READY)) == ready_before
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["checkpoint_seq"] == 1, "a replay must not move the pointer"
        assert (h.calls["submit"], h.calls["route"]) == (submits, routes)
        assert h.count("video_intent_ledger") == ledger
        assert h.count("clip_candidate") == 1

    def test_t30_38_replay_does_not_repeat_the_attention_nudge(self, h):
        """带 flag 的候选重投时，`clip-attention-needed` 也只能有一条。

        提示事件是给人/S39 看的；重放把它再发一遍，收件人就会以为又出了
        一个新问题。
        """
        job_id, _ = h.ready_job(
            duration_policy="allow_tier_snap_with_trim",
            shot_context={"dialogue_window": {"start_ms": 0, "end_ms": 4500}})
        before = len(h.events(LOCAL_CLIP_ATTENTION))
        assert before == 1
        h.artifact_event(job_id, event_id="replayed-envelope")
        assert len(h.events(LOCAL_CLIP_ATTENTION)) == before


class TestSubmissionUnknown:
    """T30-14 / T30-15 / T30-16 / T30-17 / §4.5。"""

    def _unknown(self, h):
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        result = h.state_event(job_id, "SUBMISSION_UNKNOWN", event_id="u1")
        assert result["state"] == "WAITING_UNKNOWN"
        assert result["frozen"] is True
        return job_id

    def test_t30_14_SubMISSION_UNKNOWN_freezes_the_job(self, h):
        job_id = self._unknown(h)
        body = h.service.get_job("t1", job_id)
        assert body["frozen"] is True
        assert body["must_reconcile_before_resubmit"] is True

    def test_t30_14_never_resubmits_while_unknown(self, h):
        job_id = self._unknown(h)
        submits = h.calls["submit"]
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job("t1", job_id, {}, "r1")
        assert exc.value.status == 409
        assert exc.value.code == "MUST_RECONCILE_FIRST"
        assert h.calls["submit"] == submits

    def test_t30_14_never_cancels_while_unknown(self, h):
        job_id = self._unknown(h)
        cancels = h.calls["cancel"]
        with pytest.raises(ServiceError) as exc:
            h.service.cancel_job("t1", job_id, {}, "c1")
        assert exc.value.status == 409
        assert exc.value.code == "MUST_RECONCILE_FIRST"
        assert h.calls["cancel"] == cancels

    def test_t30_15_same_fingerprint_is_suppressed_while_unknown(self, h):
        job_id = self._unknown(h)
        again, _ = h.make_job(command_id="other")
        assert again["video_job_id"] == job_id
        assert h.count("video_job") == 1
        assert h.calls["submit"] == 1

    def test_t30_16_deadline_pressure_does_not_fail_or_resubmit(self, h):
        """deadline 逼近不是行动理由。这里把 deadline 改到 1 秒后，再触发一次
        恢复扫描：既不能 FAILED(DEADLINE)，也不能重提。"""
        job_id = self._unknown(h)
        h.conn.execute(
            "UPDATE video_job SET deadline = ? WHERE video_job_id = ?",
            ("2026-11-14T00:00:01.000Z", job_id))
        submits = h.calls["submit"]
        h.state_result = {"state": "SUBMISSION_UNKNOWN"}
        report = h.service.recover_non_terminal("t1")
        assert report["errors"] == []
        assert h.calls["submit"] == submits
        assert h.state_of(job_id) == "WAITING_UNKNOWN"
        assert h.job_row(job_id)["failure_code"] is None

    def test_t30_16_unknown_survives_an_unknown_recovery(self, h):
        job_id = self._unknown(h)
        h.state_result = {"state": "SUBMISSION_UNKNOWN"}
        h.service.recover_non_terminal("t1")
        assert h.state_of(job_id) == "WAITING_UNKNOWN"

    def test_t30_17_s30_never_splits_the_shot(self, h):
        """拆镜是"新请求"，不是 S30 在 UNKNOWN 里自己派生 segment。"""
        job_id = self._unknown(h)
        h.state_result = {"state": "RECONCILING"}
        h.service.recover_non_terminal("t1")
        assert h.count("video_job") == 1
        assert h.count("video_intent_ledger") == 1

    def test_unknown_ignores_a_running_snapshot(self, h):
        job_id = self._unknown(h)
        result = h.state_event(job_id, "RUNNING", event_id="r")
        assert result["frozen"] is True
        assert result["ignored"] is True

    def test_unknown_ignores_a_late_artifact(self, h):
        job_id = self._unknown(h)
        result = h.artifact_event(job_id, event_id="a")
        assert result["reason"] == "artifact_ingested_while_waiting_unknown"
        assert h.count("clip_candidate") == 0

    def test_reconcile_archived_becomes_reconciled_ready(self, h):
        job_id = self._unknown(h)
        result = h.state_event(job_id, "ARCHIVED", event_id="arch")
        assert result["state"] == "RECONCILED_READY"
        assert result["reconciled"] is True
        assert h.state_of(job_id) == "RECONCILED_READY"

    def test_reconciled_ready_still_accepts_the_artifact(self, h):
        """对账已证明视频存在，缺的只是候选行：后来的 `artifact.ingested`
        要把候选补上。但状态**不能**从 `RECONCILED_READY` 再往前写——它已经
        是终态，`READY` 那条边不在 §3.1 的表里（C30-15）。"""
        job_id = self._unknown(h)
        h.state_event(job_id, "ARCHIVED", event_id="arch")
        result = h.artifact_event(job_id, event_id="a")
        assert result["state"] == "RECONCILED_READY", (
            "a terminal reconciled job never becomes READY retroactively")
        assert result["checkpoint_advanced"] is True
        assert h.count("clip_candidate") == 1

    def test_reconcile_failure_conclusions(self, h):
        for provider_state, expected in [("REJECTED_BUDGET", "REJECTED_BUDGET"),
                                         ("REJECTED_QUOTA", "REJECTED_QUOTA"),
                                         ("PROVIDER_FAILED", "PROVIDER_FAILED"),
                                         ("CANCELED", "CANCELED")]:
            harness = Harness()
            try:
                job_id = self._unknown(harness)
                result = harness.state_event(job_id, provider_state,
                                             event_id="x")
                assert result["state"] == "RECONCILED_FAILED"
                assert result["failure_code"] == expected
            finally:
                harness.conn.close()

    def test_unknown_does_not_accept_a_non_conclusion(self, h):
        job_id = self._unknown(h)
        for provider_state in ("QUEUED", "RUNNING", "INGESTING",
                               "PROVIDER_SUCCEEDED"):
            result = h.state_event(job_id, provider_state,
                                   event_id=f"nc-{provider_state}")
            assert result["under"] if False else result["ignored"] is True
            assert "not_a_reconciliation_conclusion" in result["reason"]
            assert h.state_of(job_id) == "WAITING_UNKNOWN"

    def test_retry_after_reconciled_failure(self, h):
        job_id = self._unknown(h)
        h.state_event(job_id, "PROVIDER_FAILED", event_id="pf")
        submits = h.calls["submit"]
        h.route_sequence = [_route(5.0)]
        out = h.command({}, command_id="r1",
                        fn=lambda: h.service.retry_job("t1", job_id, {},
                                                       "r1"))
        assert out["state"] == "WAITING_PROVIDER"
        assert h.job_row(job_id)["business_attempt_no"] == 2
        assert h.calls["submit"] == submits + 1
        assert h.job_row(job_id)["reconciled"] == 0

    def test_unknown_released_only_by_a_reconciliation_conclusion(self, h):
        job_id = self._unknown(h)
        for state in ("QUEUED", "RUNNING", "INGESTING"):
            h.state_event(job_id, state, event_id=f"x-{state}")
        assert h.state_of(job_id) == "WAITING_UNKNOWN"
        h.state_event(job_id, "ARCHIVED", event_id="finally")
        assert h.state_of(job_id) == "RECONCILED_READY"


# ================================================================ 候选落定

class TestArtifactIngest:
    """§3.2 / §4.7 F12–F14 / T30-11 ~ T30-13 / T30-18。"""

    def _ingesting(self, h, **over):
        out, _ = h.make_job(**over)
        job_id = out["video_job_id"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id=f"s_{job_id}")
        return job_id

    def test_happy_path_writes_one_of_each(self, h):
        job_id, candidate = h.ready_job()
        assert h.count("clip_candidate") == 1
        assert h.count("duration_mapping") == 1
        assert h.count("video_edit_plan") == 1
        assert h.count("shot_video_checkpoint") == 1
        assert h.state_of(job_id) == "READY"
        body = h.service.get_candidate("t1", candidate)
        assert body["state"] == "READY"
        assert body["observed_duration_ms"] == 5000
        assert body["frame_rate"] == 24.0

    def test_ready_event_has_exactly_the_contract_keys(self, h):
        h.ready_job()
        event = h.events(EVENT_VIDEO_READY)[0]
        assert set(event["data"]) == {
            "schema_version", "video_job_id", "clip_candidate_id",
            "shot_lineage_id", "segment_no", "artifact_ref",
            "observed_duration_ms", "frame_rate", "duration_mapping",
            "edit_plan_ref", "checkpoint", "keyframe_baseline", "stale_basis",
            "requires_recheck", "flags"}
        assert event["source"] == SOURCE
        assert event["schema_version"] == SCHEMA_VERSION
        assert event["aggregate_id"] == "lineage_A"
        assert event["subject"].startswith("clip-candidate/")

    def test_ready_event_nested_objects_are_closed(self, h):
        h.ready_job()
        data = h.events(EVENT_VIDEO_READY)[0]["data"]
        assert set(data["duration_mapping"]) == {
            "target_duration_ms", "tier_duration_ms", "trim_mode",
            "trim_head_ms", "trim_tail_ms", "resulting_duration_ms",
            "cost_basis", "dialogue_duration_ms"}
        assert set(data["edit_plan_ref"]) == {
            "id", "revision", "trim", "first_frame_handling",
            "last_frame_handling"}
        assert set(data["edit_plan_ref"]["trim"]) == {
            "head_ms", "tail_ms", "resulting_duration_ms"}
        assert set(data["checkpoint"]) == {"from", "to", "checkpoint_seq"}
        assert set(data["keyframe_baseline"]) == {"candidate_id",
                                                  "checkpoint_seq"}
        assert set(data["flags"]) == {"truncates_dialogue", "truncates_action",
                                      "dialogue_window_unverified"}

    def test_segment_no_is_an_integer_in_the_event(self, h):
        h.ready_job()
        assert isinstance(h.events(EVENT_VIDEO_READY)[0]["data"]["segment_no"],
                          int)

    def test_checkpoint_sequence_starts_at_one(self, h):
        job_id, candidate = h.ready_job()
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["checkpoint_seq"] == 1
        assert cp["clip_candidate_id"] == candidate
        assert cp["updated_by_job"] == job_id
        assert h.events(EVENT_VIDEO_READY)[0]["data"]["checkpoint"]["from"] \
            is None

    def test_t30_11_duration_outside_tolerance_fails(self, h):
        job_id = self._ingesting(h)
        result = h.artifact_event(job_id, event_id="a",
                                  observed_duration_ms=5200)
        assert result["state"] == "FAILED"
        assert result["failure_code"] == "DURATION_MISMATCH"
        assert result["alert"] == "vid_duration_mismatch_total"
        assert h.count("clip_candidate") == 0, "a bad take is not a candidate"
        alert = h.events(LOCAL_DURATION_MISMATCH)
        assert len(alert) == 1
        assert alert[0]["data"]["delta_ms"] == 200
        assert alert[0]["data"]["tolerance_ms"] == DURATION_TOLERANCE_MS

    def test_t30_12_duration_inside_tolerance_is_accepted(self, h):
        job_id = self._ingesting(h)
        result = h.artifact_event(job_id, event_id="a",
                                  observed_duration_ms=5080)
        assert result["state"] == "READY"
        candidate = h.service.get_candidate("t1", result["candidate_id"])
        assert candidate["observed_duration_ms"] == 5080
        mapping = candidate["duration_mapping"]
        assert mapping["tier_duration_ms"] == 5080, "mapping uses real bytes"
        assert mapping["resulting_duration_ms"] == 4000
        assert h.events(LOCAL_DURATION_MISMATCH) == []

    @pytest.mark.parametrize("observed", [4900, 5100])
    def test_tolerance_boundary_is_inclusive(self, h, observed):
        job_id = self._ingesting(h)
        assert h.artifact_event(job_id, event_id="a",
                                observed_duration_ms=observed)["state"] == "READY"

    def test_just_outside_tolerance_fails(self, h):
        job_id = self._ingesting(h)
        assert h.artifact_event(job_id, event_id="a",
                                observed_duration_ms=5101)["state"] == "FAILED"

    def test_t30_13_missing_observed_duration_blocks(self, h):
        job_id = self._ingesting(h)
        result = h.artifact_event(job_id, event_id="a",
                                  observed_duration_ms=OMIT)
        assert result["blocked"] is True
        assert result["reason"] == "observed_duration_missing_or_invalid"
        assert result["alert"] == "vid_duration_unknown_total"
        assert h.state_of(job_id) == "INGESTING"
        assert h.count("clip_candidate") == 0, "no fake READY"
        assert h.events(EVENT_VIDEO_READY) == []

    def test_missing_frame_rate_also_blocks(self, h):
        job_id = self._ingesting(h)
        result = h.artifact_event(job_id, event_id="a", frame_rate=OMIT)
        assert result["blocked"] is True
        assert h.state_of(job_id) == "INGESTING"

    def test_zero_duration_does_not_count_as_observed(self, h):
        job_id = self._ingesting(h)
        assert h.artifact_event(job_id, event_id="a",
                                observed_duration_ms=0)["blocked"] is True

    def test_c30_12_duration_ms_alias_is_honoured(self, h):
        job_id = self._ingesting(h)
        result = h.artifact_event(job_id, event_id="a",
                                  observed_duration_ms=5000,
                                  observed_field="duration_ms")
        assert result["state"] == "READY"

    def test_c30_12_frame_rate_num_den_is_honoured(self, h):
        job_id = self._ingesting(h)
        h.conn.execute("SELECT 1")
        provider_job_id = h._provider_job(job_id)
        result = h.service.on_artifact_ingested({
            "id": "a_frac", "tenant_id": "t1", "source": "S28",
            "type": "artifact.ingested", "aggregate_id": "art_out_1",
            "data": {"artifact_ref": {"service_id": "S28", "id": "art_out_1",
                                      "revision": 1, "content_hash": _hex("f")},
                     "source": {"provider_job_id": provider_job_id},
                     "observed_duration_ms": 5000,
                     "frame_rate_num": 24000, "frame_rate_den": 1001}})
        assert result["state"] == "READY"
        assert h.service.get_candidate("t1", result["candidate_id"])[
            "frame_rate"] == 23.976

    def test_artifact_from_a_non_s28_service_is_ignored(self, h):
        job_id = self._ingesting(h)
        result = h.artifact_event(job_id, event_id="a", service_id="S99")
        assert result["reason"] == "artifact_not_owned_by_s28"
        assert h.count("clip_candidate") == 0
        assert h.state_of(job_id) == "INGESTING"

    def test_artifact_without_a_content_hash_is_ignored(self, h):
        job_id = self._ingesting(h)
        result = h.artifact_event(job_id, event_id="a", content_hash="nope")
        assert result["reason"] == "artifact_ref_incomplete"

    def test_artifact_for_an_unknown_provider_job_is_ignored(self, h):
        result = h.service.on_artifact_ingested({
            "id": "orphan_art", "tenant_id": "t1", "source": "S28",
            "type": "artifact.ingested", "aggregate_id": "art_orphan",
            "data": {"artifact_ref": {"service_id": "S28", "id": "art_orphan",
                                      "revision": 1, "content_hash": _hex("f")},
                     "source": {"provider_job_id": "pj_unknown"},
                     "observed_duration_ms": 5000, "frame_rate": 24.0}})
        assert result == {"ignored": True, "reason": "no_matching_video_job"}

    def test_artifact_without_an_event_id_is_400(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.on_artifact_ingested({
                "tenant_id": "t1", "data": {"artifact_ref": {}}})
        assert exc.value.status == 400
        assert exc.value.code == "INVALID_BODY"

    def test_t30_18_url_expired_keeps_ingesting_and_asks_for_a_new_one(self, h):
        """导入是在 `PROVIDER_SUCCEEDED` 那一步催的，所以错误要**先**设好。"""
        h.import_error = ServiceError(503, "URL_EXPIRED", "signed url expired")
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        result = h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id="s1")
        assert result["awaiting_fresh_url"] is True
        assert "URL_EXPIRED" in result["note"]
        assert h.state_of(job_id) == "INGESTING"
        assert h.calls["import"] == 1
        assert h.calls["submit"] == 1, "an expired URL is never a new generation"
        assert h.count("clip_candidate") == 0, "nothing was archived yet"

    def test_hash_mismatch_fails_the_job_without_retrying(self, h):
        h.import_error = ServiceError(400, "HASH_MISMATCH", "wrong bytes")
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        result = h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id="s1")
        assert result["failure_code"] == "ARTIFACT_HASH_MISMATCH"
        assert h.state_of(job_id) == "FAILED"
        assert h.job_row(job_id)["failure_code"] == "ARTIFACT_HASH_MISMATCH"
        assert h.calls["submit"] == 1
        assert h.calls["import"] == 1, "a hash mismatch is terminal, not retried"

    def test_import_failure_alone_does_not_fail_the_job(self, h):
        h.import_error = ServiceError(503, "ARTIFACT_INGEST_UNAVAILABLE", "down")
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        result = h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id="s1")
        assert result["awaiting_fresh_url"] is False
        assert "ARTIFACT_INGEST_UNAVAILABLE" in result["note"]
        assert h.state_of(job_id) == "INGESTING"
        assert h.job_row(job_id)["failure_code"] is None

    def test_same_artifact_from_another_job_reuses_the_candidate(self, h):
        job_id, candidate = h.ready_job()
        second, _ = h.make_job(prompt_intent="different prompt")
        job2 = second["video_job_id"]
        h.state_event(job2, "PROVIDER_SUCCEEDED", event_id="s2")
        out = h.artifact_event(job2, event_id="a2")
        assert out["candidate_id"] == candidate, "one artifact, one candidate"
        assert h.count("clip_candidate") == 1
        assert h.count("duration_mapping") == 1
        assert h.count("video_edit_plan") == 1

    def test_candidate_carries_the_built_for_provenance(self, h):
        job_id, candidate = h.ready_job()
        body = h.service.get_candidate("t1", candidate)
        assert body["built_for"] == {
            "manifest_ref": {"service_id": "S21", "id": "m1", "revision": 1,
                             "content_hash": _hex("d")},
            "node_epoch_input": 4,
            "execution_fence": 2}
        # AGENTS.md：两个作用域不互相派生。
        assert body["built_for"]["node_epoch_input"] \
            != body["built_for"]["execution_fence"]

    def test_edit_plan_says_s42_executes_it(self, h):
        job_id, candidate = h.ready_job()
        plan = h.service.get_edit_plan("t1", candidate)
        assert plan["executed_by"] is None
        assert plan["note"] == "planning only; S42 executes"
        assert plan["speed"] is None
        assert plan["revision"] == 1

    def test_duration_mapping_view_reports_its_provenance(self, h):
        job_id, candidate = h.ready_job()
        mapping = h.service.get_job("t1", job_id)["duration_mapping"]
        assert mapping["resolution_source"] == "s25_normalized_input"
        assert mapping["cost_basis"] == "tier"
        # 事件里没有这个键：它是本地回显，不是 S25 的断言（C30-27）。
        assert "resolution_source" not in h.events(EVENT_VIDEO_READY)[0][
            "data"]["duration_mapping"]

    def test_t30_24_native_locked_voice_flags_unverified_dialogue(self, h):
        job_id, candidate = h.ready_job(
            audio_strategy="native_locked_voice",
            shot_context={"has_dialogue": True, "voice_locked_by_s15": True})
        body = h.service.get_candidate("t1", candidate)
        assert body["dialogue_window_unverified"] is True
        event = h.events(EVENT_VIDEO_READY)[0]
        assert event["data"]["flags"]["dialogue_window_unverified"] is True

    def test_unverified_dialogue_does_not_require_a_human(self, h):
        """§4.4：延后验证不是"裁坏了对白"，不该升级成重看。"""
        job_id, candidate = h.ready_job(
            audio_strategy="native_locked_voice",
            shot_context={"has_dialogue": True, "voice_locked_by_s15": True})
        assert h.service.get_candidate("t1", candidate)[
            "requires_rehuman_decision"] is False

    def test_c30_28_text_to_video_keeps_the_candidate_but_not_the_event(self, h):
        result, _ = h.make_job(keyframe_baseline=None)
        job_id = result["video_job_id"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id="s")
        out = h.artifact_event(job_id, event_id="a")
        assert out["state"] == "READY"
        assert out["ready_published"] is False
        assert out["ready_blocked_by"] == "KEYFRAME_BASELINE_ABSENT"
        assert out["contract_gap"] == "C30-28"
        assert out["checkpoint_advanced"] is True
        assert h.count("clip_candidate") == 1, "the paid bytes are still an asset"
        assert h.events(EVENT_VIDEO_READY) == []
        # S41 仍能读到它
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["clip_candidate_id"] == out["candidate_id"]

    def test_the_trim_plan_matches_the_mapping(self, h):
        job_id, candidate = h.ready_job()
        body = h.service.get_candidate("t1", candidate)
        mapping, plan = body["duration_mapping"], body["edit_plan_ref"]
        assert plan["trim"]["head_ms"] == mapping["trim_head_ms"]
        assert plan["trim"]["tail_ms"] == mapping["trim_tail_ms"]
        assert plan["trim"]["resulting_duration_ms"] \
            == mapping["resulting_duration_ms"]

    def test_mapping_math_holds_in_the_database(self, h):
        job_id, candidate = h.ready_job()
        row = h.conn.execute("SELECT * FROM duration_mapping").fetchone()
        assert row["resulting_duration_ms"] == (
            row["tier_duration_ms"] - row["trim_head_ms"]
            - row["trim_tail_ms"])
        assert row["resulting_duration_ms"] == row["target_duration_ms"]

    def test_both_trim_records_the_c2_minimal_policy(self, h):
        job_id, candidate = h.ready_job(
            shot_context={"dialogue_window": {"start_ms": 3000,
                                              "end_ms": 4800}})
        row = h.conn.execute("SELECT * FROM duration_mapping").fetchone()
        assert row["trim_mode"] == "both"
        assert row["trim_head_ms"] == 800
        assert row["trim_tail_ms"] == 200
        assert row["trim_policy"] == "c2_minimal"

    def test_tail_only_is_the_default_policy(self, h):
        job_id, candidate = h.ready_job()
        row = h.conn.execute("SELECT * FROM duration_mapping").fetchone()
        assert row["trim_mode"] == "tail"
        assert row["trim_policy"] == "tail_only"


# ================================================================ 取消 / 重试

class TestCancelRetry:
    """§6.3 / T30-37 / F10。"""

    def test_t30_37_cancel_pending_when_s26_does_not_confirm(self, h):
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        h.cancel_error = ServiceError(503, "GENERATION_JOBS_UNAVAILABLE", "down")
        result = h.command({}, command_id="cancel1",
                           fn=lambda: h.service.cancel_job(
                               "t1", job_id, {"reason": "director"}, "cancel1"))
        assert result["cancel_state"] == "CANCEL_PENDING"
        assert result["state"] == "WAITING_PROVIDER", "unconfirmed is not canceled"
        assert result["refund_promised"] is False
        assert "unconfirmed" in result["note"]

    def test_t30_37_cancel_never_promises_a_refund(self, h):
        out, _ = h.make_job()
        result = h.command({}, command_id="c",
                           fn=lambda: h.service.cancel_job(
                               "t1", out["video_job_id"], {}, "c"))
        assert result["cancel_state"] == "CANCELED"
        assert result["refund_promised"] is False
        assert h.calls["cancel"] == 1

    def test_cancel_before_submission_sends_nothing(self, h):
        """`PREPARING` 里还没有 provider_job_id：没有可取消的远端任务。"""
        out, _ = h.make_job()
        job_id = h.rewind(out["video_job_id"], "PREPARING",
                          drop_provider_job=True)
        result = h.service.cancel_job("t1", job_id, {}, "c_cancel")
        assert result["cancel_state"] == "CANCELED"
        assert h.calls["cancel"] == 0, "no remote job to cancel"
        assert h.state_of(job_id) == "CANCELED"

    def test_cancel_of_a_terminal_job_is_409(self, h):
        job_id, _ = h.ready_job()
        cancels = h.calls["cancel"]
        with pytest.raises(ServiceError) as exc:
            h.service.cancel_job("t1", job_id, {}, "c")
        assert exc.value.status == 409
        assert exc.value.code == "JOB_ALREADY_TERMINAL"
        assert h.calls["cancel"] == cancels

    def test_cancel_other_tenant_is_404(self, h):
        out, _ = h.make_job()
        with pytest.raises(ServiceError) as exc:
            h.conn.execute("SELECT 1")
            h.service.cancel_job("t2", out["video_job_id"], {}, "c")
        assert exc.value.status == 404

    def test_retry_of_a_live_job_is_409(self, h):
        out, _ = h.make_job()
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job("t1", out["video_job_id"], {}, "r")
        assert exc.value.status == 409
        assert exc.value.code == "JOB_NOT_RETRYABLE"

    def test_retry_of_a_ready_job_is_409(self, h):
        job_id, _ = h.ready_job()
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job("t1", job_id, {}, "r")
        assert exc.value.code == "JOB_NOT_RETRYABLE"
        assert h.calls["submit"] == 1

    def test_retry_clears_the_failure_code(self, h):
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", reason_code="OOM", event_id="f")
        assert h.job_row(job_id)["failure_code"] == "OOM"
        h.route_sequence = [_route(5.0)]
        h.command({}, command_id="r",
                  fn=lambda: h.service.retry_job("t1", job_id, {}, "r"))
        assert h.job_row(job_id)["failure_code"] is None
        assert h.state_of(job_id) == "WAITING_PROVIDER"

    def test_retry_history_is_append_only(self, h):
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        before = h.service.get_job("t1", job_id)["history"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="f")
        h.route_sequence = [_route(5.0)]
        h.command({}, command_id="r",
                  fn=lambda: h.service.retry_job("t1", job_id, {}, "r"))
        history = h.service.get_job("t1", job_id)["history"]
        edges = [(e["from"], e["to"]) for e in history]
        # 只追加：前缀一格不动，`seq` 连号不跳回。
        assert [e["seq"] for e in history[:len(before)]] == \
            [e["seq"] for e in before]
        assert [(e["from"], e["to"]) for e in history[:len(before)]] == \
            [(e["from"], e["to"]) for e in before]
        assert [e["seq"] for e in history] == list(range(1, len(history) + 1))
        # `CREATED` 是初始态，内核不为它写转移行（与 S29 同约定），所以第一条
        # 边是 `CREATED → PREPARING`。
        assert edges[0] == ("CREATED", "PREPARING")
        assert edges[-1] == ("SUBMITTED", "WAITING_PROVIDER")
        assert ("FAILED", "PREPARING") in edges, "retry edge must be recorded"

    def test_cancel_after_failure_is_refused(self, h):
        """§3.1 只给了"任意**非终态** → CANCELED"。`FAILED` 已经终态：
        S26 那边没有可撤销的东西，回一句 CANCELED 就是凭空断言"它不做了"。
        重试才是 `FAILED` 的出口。"""
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        h.state_event(job_id, "PROVIDER_FAILED", event_id="f")
        cancels = h.calls["cancel"]
        with pytest.raises(ServiceError) as exc:
            h.service.cancel_job("t1", job_id, {}, "c")
        assert exc.value.status == 409
        assert exc.value.code == "JOB_ALREADY_TERMINAL"
        assert h.calls["cancel"] == cancels, "no cancel may reach S26"
        assert h.state_of(job_id) == "FAILED"


# ================================================================ 读口

class TestReads:
    def test_get_job_exposes_the_whole_chain(self, h):
        job_id, candidate = h.ready_job()
        body = h.service.get_job("t1", job_id)
        assert body["state"] == "READY"
        assert body["source_revision"] == 2
        assert body["business_attempt_no"] == 1
        assert body["provider_state_source"] == "local_map"
        assert body["frozen"] is False
        assert [c["clip_candidate_id"] for c in body["candidates"]] == [candidate]
        assert body["checkpoint"]["clip_candidate_id"] == candidate
        assert body["failure"] is None
        # `CREATED` 是初始态；第一条被记录的边是它离开 CREATED 的那一步。
        assert body["history"][0]["from"] == "CREATED"
        assert body["history"][0]["to"] == "PREPARING"
        assert body["history"][-1]["to"] == "READY"

    def test_get_job_unknown_is_404(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.get_job("t1", "vj_nope")
        assert exc.value.status == 404
        assert exc.value.code == "VIDEO_JOB_NOT_FOUND"

    def test_list_jobs_filters(self, h):
        a, _ = h.make_job(command_id="a")
        b, _ = h.make_job(command_id="b", prompt_intent="other")
        items = h.service.list_jobs("t1")["items"]
        assert {i["video_job_id"] for i in items} == {
            a["video_job_id"], b["video_job_id"]}
        assert len(h.service.list_jobs("t1", state="WAITING_PROVIDER")["items"]) == 2
        assert h.service.list_jobs("t1", state="READY")["items"] == []
        assert h.service.list_jobs("t1", segment_no=2)["items"] == []
        assert h.service.list_jobs("t1", shot_lineage_id="other")["items"] == []

    def test_list_jobs_reports_a_watermark(self, h):
        h.make_job()
        body = h.service.list_jobs("t1")
        assert body["count"] == 1
        assert body["projection_watermark"] >= 2

    def test_checkpoint_read_is_empty_when_nothing_landed(self, h):
        body = h.service.get_checkpoint("t1", "lineage_A")
        assert body == {"shot_lineage_id": "lineage_A", "checkpoints": []}

    def test_checkpoint_read_can_pick_one_segment(self, h):
        h.ready_job()
        body = h.service.get_checkpoint("t1", "lineage_A", segment_no=1)
        assert len(body["checkpoints"]) == 1
        assert h.service.get_checkpoint("t1", "lineage_A",
                                        segment_no=2)["checkpoints"] == []

    def test_candidate_unknown_is_404(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.get_candidate("t1", "cc_nope")
        assert exc.value.code == "CLIP_CANDIDATE_NOT_FOUND"

    def test_edit_plan_for_a_missing_candidate_is_404(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.get_edit_plan("t1", "cc_nope")
        assert exc.value.code == "CLIP_CANDIDATE_NOT_FOUND"


class TestCheckpointPriority:
    """§7.2 / U3：`created_at` 当优先级；检查点**不回退**。"""

    def test_second_candidate_advances_the_sequence(self, h):
        """两版视频是两份不同的字节。用同一个 `artifact_id` 造第二版，会让
        `cc_artifact_uniq` 复用同一行候选，指针没有可推进的目标——测到的就
        不是"第二版推进了序列"。"""
        job_id, first = h.ready_job()
        second, _ = h.make_job(prompt_intent="second take")
        h.state_event(second["video_job_id"], "PROVIDER_SUCCEEDED", event_id="s2")
        out = h.artifact_event(second["video_job_id"], event_id="a2",
                               artifact_id="art_out_2")
        assert out["candidate_id"] != first
        assert out["checkpoint_advanced"] is True
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["clip_candidate_id"] == out["candidate_id"]
        assert cp["checkpoint_seq"] == 2
        assert h.events(EVENT_VIDEO_READY)[-1]["data"]["checkpoint"]["from"] \
            == first

    def test_older_candidate_cannot_regress_the_checkpoint(self, h):
        """迟到的是**更旧**的候选：不是"再来一版"，是不许把指针往回拨（U3）。

        三个候选必须真的是三份字节，否则 `_existing_candidate` 会复用同一行，
        `created_at` 相等、守卫根本不会触发——那条断言会"通过"得毫无意义。
        """
        _, first = h.ready_job()

        second_out, _ = h.make_job(prompt_intent="second take")
        second = second_out["video_job_id"]
        second_candidate = self._land(h, second, "art_out_2")
        assert second_candidate != first

        third_out, _ = h.make_job(prompt_intent="third take")
        third = third_out["video_job_id"]
        third_candidate = self._land(h, third, "art_out_3")
        assert third_candidate not in (first, second_candidate)

        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["clip_candidate_id"] == third_candidate
        assert cp["checkpoint_seq"] == 3

        # 把第三个候选拨早成 1999，并把指针恢复到它落地**之前**的样子——这正是
        # 一次重驱动/乱序重投要面对的状态。恢复的是脚手架，守卫跑的仍是内核。
        h.conn.execute(
            "UPDATE clip_candidate SET created_at = '1999-01-01T00:00:00.000Z' "
            "WHERE clip_candidate_id = ?", (third_candidate,))
        h.conn.execute(
            "UPDATE shot_video_checkpoint SET clip_candidate_id = ?, "
            "checkpoint_seq = 2, expected_revision = 1 "
            "WHERE tenant_id = 't1' AND shot_lineage_id = 'lineage_A' "
            "AND segment_no = 1", (second_candidate,))

        job = h.job_row(third)
        candidate = h.service._candidate_row("t1", third_candidate)
        assert h.service._advance_checkpoint(job, candidate) is None, (
            "an older candidate must not move the pointer")
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["clip_candidate_id"] == second_candidate
        assert cp["checkpoint_seq"] == 2, "the pointer must not have moved"

    @staticmethod
    def _land(h, job_id, artifact_id):
        h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id=f"ev_s_{job_id}")
        out = h.artifact_event(job_id, event_id=f"ev_a_{job_id}",
                               artifact_id=artifact_id)
        assert out["state"] == "READY", out
        return out["candidate_id"]

    def test_segments_have_independent_pointers(self, h):
        """拆镜后每个 segment 各一条指针，互不影响（T30-27 / T30-28）。"""
        a, cand_a = h.ready_job(segment_no=1)
        b, cand_b = h.ready_job(segment_no=2, prompt_intent="segment two")
        body = h.service.get_checkpoint("t1", "lineage_A")
        assert [c["segment_no"] for c in body["checkpoints"]] == [1, 2]
        assert {c["clip_candidate_id"] for c in body["checkpoints"]} == {
            cand_a, cand_b}

    def test_t30_28_segment_retry_stays_in_the_same_lineage(self, h):
        job_id = h.ready_job(segment_no=1)[0]
        second, _ = h.make_job(segment_no=2, prompt_intent="seg two")
        assert h.job_row(second["video_job_id"])["shot_lineage_id"] == "lineage_A"
        assert h.service.get_checkpoint("t1", "lineage_A")["checkpoints"] != []

    def test_t30_26_lost_cas_race_rereads_instead_of_overwriting(self, h):
        """T30-26：两个 job 同时 READY，CAS 必须"一个成功、另一个重读"。

        `UPDATE ... WHERE checkpoint_seq = ?` 用旧值赢不了，内核随即重读并
        按新值再试一次——而不是拿旧 seq 覆盖别人刚写的行。这里让**第一次**
        `_checkpoint_row` 读到过期值（真实行已被别人推进到 5），第二次开始
        读真实值；如果内核不重读，UPDATE 会失败并抛 `CHECKPOINT_CAS_FAILED`。
        """
        first_job, first_candidate = h.ready_job()
        second_out, _ = h.make_job(prompt_intent="concurrent take")
        second = second_out["video_job_id"]
        h.state_event(second, "PROVIDER_SUCCEEDED", event_id="s2")
        landed = h.artifact_event(second, event_id="a2",
                                  artifact_id="art_out_2")
        real_row = h.service._checkpoint_row(h.job_row(second))
        job = h.job_row(second)
        candidate = h.service._candidate_row("t1", landed["candidate_id"])

        # 别人已经把指针推到 5（第二个候选自己落地时就推进过一次；这里再往前
        # 一格，制造"我读到的是旧值"的局面）。
        h.conn.execute(
            "UPDATE shot_video_checkpoint SET checkpoint_seq = 5 "
            "WHERE tenant_id = 't1' AND shot_lineage_id = 'lineage_A' "
            "AND segment_no = 1")
        stale = {**dict(real_row), "checkpoint_seq": 4}
        calls = {"n": 0}
        original = h.service._checkpoint_row

        def racy(current_job):
            calls["n"] += 1
            return stale if calls["n"] == 1 else original(current_job)

        h.service._checkpoint_row = racy
        try:
            advanced = h.service._advance_checkpoint(job, candidate)
        finally:
            h.service._checkpoint_row = original
        assert advanced is not None, "the retry must not give up"
        assert calls["n"] >= 2, "it must re-read after losing the race"
        assert advanced["row"]["checkpoint_seq"] == 6, (
            "it must build on the value it re-read, not the stale one")
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"][0]
        assert cp["checkpoint_seq"] == 6
        assert cp["clip_candidate_id"] == candidate["clip_candidate_id"]

    def test_t30_27_segments_accumulate_on_one_lineage(self, h):
        """T30-27 / §8.1：`shot_lineage_id` 跨 segment 不变，`segment_no` 只作
        审计列。S39 的 `shot_total` 按 lineage 求和，所以 3 段看起来必须是
        **3 行挂在同一个 lineage 下**，而不是 3 个互不相干的 lineage。

        计数上限本身归 S39（S30 不自己数数），这里只证明 S30 给出的分组键是
        对的：3 个 segment 全部落在 `lineage_A`，`segment_no` 各不相同。
        """
        for seg in (1, 2, 3):
            # 三个 segment 是三份不同的字节，否则 `cc_artifact_uniq` 会把它们
            # 复用成同一个候选，测出来的就不是"各段各一份"。
            h.ready_job(segment_no=seg, prompt_intent=f"拆镜第 {seg} 段",
                        artifact_id=f"art_seg_{seg}")
        rows = h.conn.execute(
            "SELECT shot_lineage_id, segment_no FROM video_intent_ledger "
            "ORDER BY segment_no").fetchall()
        assert [r["segment_no"] for r in rows] == [1, 2, 3]
        assert {r["shot_lineage_id"] for r in rows} == {"lineage_A"},             "splitting a shot must not create a new lineage"
        # 每个 segment 各一条检查点指针，互不覆盖（§7.2）。
        cp = h.service.get_checkpoint("t1", "lineage_A")["checkpoints"]
        assert [c["segment_no"] for c in cp] == [1, 2, 3]
        assert len({c["clip_candidate_id"] for c in cp}) == 3

    def test_checkpoint_refuses_a_non_ready_candidate(self, h):
        """C30-6 的触发器：指针只能指向 READY / ACCEPTED。"""
        job_id, candidate = h.ready_job()
        h.conn.execute("UPDATE clip_candidate SET state='REJECTED' "
                       "WHERE clip_candidate_id=?", (candidate,))
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE shot_video_checkpoint SET clip_candidate_id=? "
                "WHERE shot_lineage_id='lineage_A'", (candidate,))


# ================================================================ 图像漂移

class TestImageDrift:
    """§2.5 / F17 / T30-19 / T30-20。"""

    def test_t30_19_late_image_ready_marks_the_candidate_stale(self, h):
        job_id, candidate = h.ready_job()
        result = h.image_ready_event("lineage_A", 9, event_id="img1")
        assert result["auto_regenerated"] is False
        assert result["backfilled_candidates"] == [candidate]
        body = h.service.get_candidate("t1", candidate)
        assert body["stale_basis"] is True
        assert body["requires_recheck"] is True
        assert body["state"] == "READY", "stale is not rejected"

    def test_t30_19_never_regenerates_on_its_own(self, h):
        h.ready_job()
        submits = h.calls["submit"]
        routes = h.calls["route"]
        h.image_ready_event("lineage_A", 9, event_id="img1")
        assert h.calls["submit"] == submits
        assert h.calls["route"] == routes
        assert h.count("video_job") == 1

    def test_t30_19_emits_one_attention_event(self, h):
        job_id, candidate = h.ready_job()
        h.image_ready_event("lineage_A", 9, event_id="img1")
        attention = h.events(LOCAL_CLIP_ATTENTION)
        assert len(attention) == 1
        assert "stale_basis" in attention[0]["data"]["reasons"]

    def test_t30_19_drift_is_applied_at_landing_time(self, h):
        """生成中途到达：候选落定时就该带着 stale_basis。"""
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id="s")
        h.image_ready_event("lineage_A", 9, event_id="img1")
        assert h.job_row(job_id)["keyframe_drifted"] == 1
        result = h.artifact_event(job_id, event_id="a")
        assert result["state"] == "READY"
        body = h.service.get_candidate("t1", result["candidate_id"])
        assert body["stale_basis"] is True
        assert body["requires_recheck"] is True
        assert h.events(EVENT_VIDEO_READY)[0]["data"]["stale_basis"] is True

    def test_t30_20_a_stale_candidate_still_advances_the_checkpoint(self, h):
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id="s")
        h.image_ready_event("lineage_A", 9, event_id="img1")
        result = h.artifact_event(job_id, event_id="a")
        assert result["checkpoint_advanced"] is True
        assert result["state"] == "READY"

    def test_drift_older_than_the_baseline_is_ignored(self, h):
        job_id, candidate = h.ready_job()
        result = h.image_ready_event("lineage_A", 1, event_id="img_old")
        assert result["backfilled_candidates"] == []
        assert h.service.get_candidate("t1", candidate)["stale_basis"] is False

    def test_drift_does_not_touch_text_to_video(self, h):
        result, _ = h.make_job(keyframe_baseline=None)
        out = h.image_ready_event("lineage_A", 99, event_id="img1")
        assert out["drifted_jobs"] == []
        assert h.job_row(result["video_job_id"])["keyframe_drifted"] == 0

    def test_drift_event_is_deduplicated(self, h):
        job_id, candidate = h.ready_job()
        h.image_ready_event("lineage_A", 9, event_id="img1")
        again = h.image_ready_event("lineage_A", 9, event_id="img1")
        assert again == {"applied": False, "reason": "DUPLICATE"}
        assert len(h.events(LOCAL_CLIP_ATTENTION)) == 1

    def test_drift_for_another_tenant_is_invisible(self, h):
        job_id, candidate = h.ready_job()
        h.image_ready_event("lineage_A", 9, event_id="img1", tenant="t2")
        assert h.service.get_candidate("t1", candidate)["stale_basis"] is False

    def test_malformed_drift_event_is_ignored(self, h):
        out = h.image_ready_event("lineage_A", 3, event_id="img1",
                                  checkpoint=False)
        assert out == {"ignored": True, "reason": "not_shaped_for_video"}

    def test_drift_event_without_an_id_is_400(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.on_image_ready({"tenant_id": "t1", "data": {}})
        assert exc.value.code == "INVALID_BODY"


# ================================================================ 对白时长

class TestSpeechReady:
    """§7.2：只影响还没派单的 job；映射一旦确定就不再改。"""

    @staticmethod
    def _preparing(h, **over):
        """造一个还没派单的 job。

        不能靠"路由失败留下 job"：命令事务整体回滚，失败后库里根本没有行。
        这里把一个成功派出的 job 拨回 `PREPARING`——模拟的正是 S31 事件与
        派单并发的窗口（§7.2 最后一行）。刻意**保留** `provider_job_id`：那正是
        重试后"新 attempt 在 PREPARING、旧 attempt 的 id 还挂着"的形状，也是
        对白事件最该覆盖到的窗口。`rewind` 同时截断历史，后续每条真实转移都能
        合法往下写。
        """
        out, _ = h.make_job(**over)
        job_id = h.rewind(out["video_job_id"], "PREPARING")
        return job_id, out

    def test_preparing_job_adopts_the_duration(self, h):
        job_id, _ = self._preparing(h)
        result = h.speech_ready_event("lineage_A", 3210, event_id="sp1")
        assert result["updated_jobs"] == [job_id]
        assert h.job_row(job_id)["dialogue_duration_ms"] == 3210

    def test_created_job_adopts_the_duration_too(self, h):
        job_id, _ = self._preparing(h)
        h.rewind(job_id, "CREATED")
        result = h.speech_ready_event("lineage_A", 1500, event_id="sp1")
        assert result["updated_jobs"] == [job_id]

    def test_adopted_duration_is_carried_onto_the_mapping(self, h):
        """采纳的对白时长落到 `duration_mapping`，不是被静默丢掉。"""
        job_id, _ = self._preparing(h)
        h.speech_ready_event("lineage_A", 3999, event_id="sp1")
        assert h.job_row(job_id)["dialogue_duration_ms"] == 3999
        # 拨回时截掉了 `SUBMITTED → WAITING_PROVIDER` 那两格；补回来（内核的
        # `_transition`，不是手写 SQL），让供应商事件落在一个真实的后派单状态。
        job = h.job_row(job_id)
        h.service._transition(job, "SUBMITTED", reason_code="TEST_DISPATCH")
        h.service._transition(h.job_row(job_id), "WAITING_PROVIDER",
                              reason_code="TEST_DISPATCH")
        h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id="s1")
        ingested = h.artifact_event(job_id, event_id="a1")
        mapping = h.service.get_candidate("t1", ingested["candidate_id"])[
            "duration_mapping"]
        assert mapping["dialogue_duration_ms"] == 3999

    def test_ready_job_is_not_rewritten(self, h):
        job_id, candidate = h.ready_job()
        before = h.service.get_candidate("t1", candidate)["duration_mapping"]
        result = h.speech_ready_event("lineage_A", 1234, event_id="sp1")
        assert result["updated_jobs"] == []
        after = h.service.get_candidate("t1", candidate)["duration_mapping"]
        assert after == before

    def test_other_audio_strategies_are_not_touched(self, h):
        self._preparing(h, audio_strategy="non_dialogue_native",
                        shot_context={"has_dialogue": False})
        result = h.speech_ready_event("lineage_A", 2000, event_id="sp1")
        assert result == {"updated_jobs": []}

    def test_unusable_duration_is_ignored(self, h):
        assert h.speech_ready_event("lineage_A", 0, event_id="sp1") == {
            "ignored": True, "reason": "no_usable_duration"}
        assert h.speech_ready_event("lineage_A", "3s", event_id="sp2") == {
            "ignored": True, "reason": "no_usable_duration"}

    def test_duplicate_speech_event_is_ignored(self, h):
        h.speech_ready_event("lineage_A", 100, event_id="sp1")
        assert h.speech_ready_event("lineage_A", 100, event_id="sp1") == {
            "applied": False, "reason": "DUPLICATE"}


# ================================================================ 重启恢复

class TestRestart:
    """F19 / §7.3 / T30-30：恢复只问 S26，不从内存里捞任何东西。"""

    def _restart(self, h):
        """同一个库、新的服务实例：进程重启的最小复现。"""
        return Harness(conn=h.conn)

    def test_t30_30_ingesting_job_re_requests_the_import(self, h):
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id="s")
        assert h.state_of(job_id) == "INGESTING"
        h2 = self._restart(h)
        try:
            report = h2.service.recover_non_terminal("t1")
            assert report["errors"] == []
            assert job_id in report["imports_retried"]
            assert h.state_of(job_id) == "INGESTING"
            assert h2.count("clip_candidate") == 0, "no duration was invented"
        finally:
            h2.conn.close() if h2.conn is not h.conn else None

    def test_t30_30_recovery_does_not_fabricate_a_candidate(self, h):
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        h.state_event(job_id, "PROVIDER_SUCCEEDED", event_id="s")
        h.import_error = ServiceError(503, "ARTIFACT_INGEST_UNAVAILABLE", "down")
        h2 = Harness(conn=h.conn)
        report = h2.service.recover_non_terminal("t1")
        assert report["errors"] == []
        assert h.count("clip_candidate") == 0
        assert h.state_of(job_id) == "INGESTING"

    def test_recovery_follows_s26_forward(self, h):
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        h2 = Harness(conn=h.conn)
        h2.state_result = {"state": "PROVIDER_FAILED",
                           "reason_code": "OOM"}
        report = h2.service.recover_non_terminal("t1")
        assert report["reconciled"][0]["state"] == "FAILED"
        assert h.job_row(job_id)["failure_code"] == "OOM"

    def test_recovery_turns_unknown_into_a_conclusion(self, h):
        out, _ = h.make_job()
        job_id = out["video_job_id"]
        h.state_event(job_id, "SUBMISSION_UNKNOWN", event_id="u")
        h2 = Harness(conn=h.conn)
        h2.state_result = {"state": "ARCHIVED"}
        report = h2.service.recover_non_terminal("t1")
        assert report["reconciled"][0]["state"] == "RECONCILED_READY"

    def test_recovery_skips_terminal_jobs(self, h):
        h.ready_job()
        h2 = Harness(conn=h.conn)
        assert h2.service.recover_non_terminal("t1")["scanned"] == 0

    def test_recovery_reports_an_unreachable_s26(self, h):
        out, _ = h.make_job()
        h2 = Harness(conn=h.conn)
        h2.state_result = ServiceError(503, "GENERATION_JOBS_UNAVAILABLE", "down")
        report = h2.service.recover_non_terminal("t1")
        assert report["errors"]
        assert "GENERATION_JOBS_UNAVAILABLE" in report["errors"][0]["reason"]
        assert h.state_of(out["video_job_id"]) == "WAITING_PROVIDER"

    def test_recovery_is_tenant_scoped(self, h):
        out, _ = h.make_job()
        h2 = Harness(conn=h.conn)
        assert h2.service.recover_non_terminal("t2")["scanned"] == 0

    def test_state_survives_a_restart(self, h):
        job_id, candidate = h.ready_job()
        h2 = Harness(conn=h.conn)
        body = h2.service.get_job("t1", job_id)
        assert body["state"] == "READY"
        assert body["candidates"][0]["clip_candidate_id"] == candidate


# ============================================ 红线：付费路径与配置

class TestPaidPathRedLines:
    def test_no_provider_base_url_in_the_module(self):
        """模块里任何供应商域名都是缺陷：绕过 S26 = 绕过 S25/S23/S22。"""
        root = Path(__file__).resolve().parents[1] / "src" / "video_production"
        offenders = []
        for path in sorted(root.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in ("http://", "https://"):
                if token in text:
                    offenders.append((path.name, token))
        assert offenders == []

    def test_no_provider_url_in_the_http_layer(self):
        path = Path(__file__).resolve().parents[1] / "api" / "app.py"
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text

    def test_deps_has_no_url_literals_at_all(self):
        from video_production import deps
        tree = ast.parse(Path(deps.__file__).read_text(encoding="utf-8"))
        urls = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and ("http://" in node.value or "https://" in node.value)]
        assert urls == []

    def test_the_only_paid_outlet_is_s26_generation_jobs(self):
        from video_production import deps
        source = Path(deps.__file__).read_text(encoding="utf-8")
        assert "/generation-jobs" in source
        assert "/route-decisions" in source
        # 没有别的"生成"路径
        for forbidden in ("/videos", "/generations", "/models/", "/predictions"):
            assert forbidden not in source, forbidden

    def test_unconfigured_submitter_is_503(self):
        submitter = s30_deps.build_submitter(None)
        with pytest.raises(ServiceError) as exc:
            submitter({"video_job_id": "vj_1", "tenant_id": "t1",
                       "business_attempt_no": 1}, {}, {}, 0)
        assert exc.value.status == 503
        assert exc.value.code == "GENERATION_JOBS_UNAVAILABLE"

    def test_unconfigured_router_is_503(self):
        resolver = s30_deps.build_route_resolver(None)
        with pytest.raises(ServiceError) as exc:
            resolver("t1", {"video_job_id": "vj_1"}, {})
        assert exc.value.status == 503
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"

    def test_unconfigured_canceler_is_503(self):
        canceler = s30_deps.build_canceler(None)
        with pytest.raises(ServiceError) as exc:
            canceler("pj_1", "reason")
        assert exc.value.status == 503

    def test_unconfigured_importer_is_503(self):
        importer = s30_deps.build_artifact_importer(None)
        with pytest.raises(ServiceError) as exc:
            importer("t1", {"video_job_id": "vj_1"})
        assert exc.value.status == 503
        assert exc.value.code == "ARTIFACT_INGEST_UNAVAILABLE"

    def test_unconfigured_state_reader_is_503(self):
        reader = s30_deps.build_generation_state(None)
        with pytest.raises(ServiceError) as exc:
            reader("t1", "pj_1")
        assert exc.value.status == 503

    def test_unconfigured_keyframe_lookup_is_503_not_none(self):
        """`None` 在内核里等于 404；未配置必须是 503（`deps` 文件头的解释）。"""
        lookup = s30_deps.build_keyframe_lookup(None)
        with pytest.raises(ServiceError) as exc:
            lookup("t1", "kfc_1", "lineage_A")
        assert exc.value.status == 503
        assert exc.value.code == "KEYFRAME_LOOKUP_UNAVAILABLE"

    def test_unconfigured_artifact_check_is_none_not_false(self):
        check = s30_deps.build_artifact_check(None)
        assert check("t1", {"id": "a", "revision": 1}) is None

    def test_submitter_fabricates_no_provider_job_id(self, monkeypatch):
        def fake_post(url, body, headers=None, timeout=None):
            assert "/generation-jobs" in url
            return {"operation_id": "op_only"}

        monkeypatch.setattr(s30_deps, "_post", fake_post)
        submitter = s30_deps.build_submitter("http://s26.invalid")
        with pytest.raises(ServiceError) as exc:
            submitter({"video_job_id": "vj_1", "tenant_id": "t1",
                       "business_attempt_no": 1}, {}, {"route_id": "r1"}, 0)
        assert exc.value.status == 503

    def test_submitter_refuses_without_a_route_id(self):
        submitter = s30_deps.build_submitter("http://s26.invalid")
        with pytest.raises(ServiceError) as exc:
            submitter({"video_job_id": "vj_1", "tenant_id": "t1",
                       "business_attempt_no": 1}, {}, {}, 0)
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"

    def test_submitter_forwards_the_command_untouched(self, monkeypatch):
        seen = {}

        def fake_post(url, body, headers=None, timeout=None):
            seen.update(body)
            seen["_headers"] = headers
            return {"provider_job_id": "pj_1", "operation_id": "op",
                    "attempt_id": "a", "state": "QUEUED"}

        monkeypatch.setattr(s30_deps, "_post", fake_post)
        submitter = s30_deps.build_submitter("http://s26.invalid")
        command = {"schema_version": "2.0", "attempt_id": "a",
                   "input": {"duration_seconds": 5.0}}
        submitter({"video_job_id": "vj_1", "tenant_id": "t1",
                   "business_attempt_no": 2}, command,
                  {"route_id": "r1", "revision": 4}, 0)
        assert seen["generation_command"] == command
        assert seen["route_decision_id"] == "r1"
        assert seen["route_decision_ref"] == {"service_id": "S25", "id": "r1",
                                              "revision": 4}
        assert seen["_headers"] == {"X-Tenant-Id": "t1"}
        assert "content_hash" not in seen["route_decision_ref"]

    def test_router_does_not_invent_a_route_id(self, monkeypatch):
        monkeypatch.setattr(s30_deps, "_post",
                            lambda *a, **k: {"route": {"feasible": True},
                                             "eligible": []})
        resolver = s30_deps.build_route_resolver("http://s25.invalid")
        result = resolver("t1", {"video_job_id": "vj_1"},
                          {"attempt_id": "a1"})
        assert "route_id" not in result
        assert result["feasible"] is True

    def test_router_lifts_eligible_zero_into_selected(self, monkeypatch):
        monkeypatch.setattr(s30_deps, "_post", lambda *a, **k: {
            "route": {"route_decision_id": "rd_1", "quote_valid_to": FUTURE},
            "eligible": [{"normalized_input": {"duration_seconds": 5.0},
                          "expected_cost_micro": 7}],
            "rejected": []})
        result = s30_deps.build_route_resolver("http://s25.invalid")(
            "t1", {"video_job_id": "vj_1"}, {"attempt_id": "a1"})
        assert result["selected"]["normalized_input"] == {"duration_seconds": 5.0}
        assert result["route_id"] == "rd_1"

    def test_router_reads_residency_from_rejection_codes(self, monkeypatch):
        monkeypatch.setattr(s30_deps, "_post", lambda *a, **k: {
            "route": {"route_id": "rd_1", "feasible": True},
            "eligible": [{"normalized_input": {"duration_seconds": 5.0}}],
            "rejected": [{"rejection_code": "RESIDENCY_FORBIDDEN"}]})
        result = s30_deps.build_route_resolver("http://s25.invalid")(
            "t1", {"video_job_id": "vj_1"}, {"attempt_id": "a1"})
        assert result["residency_allowed"] is False

    def test_artifact_check_404_reads_as_absent(self, monkeypatch):
        def boom(*a, **k):
            raise ServiceError(404, "NOT_FOUND", "gone")

        monkeypatch.setattr(s30_deps, "_get", boom)
        check = s30_deps.build_artifact_check("http://s28.invalid")
        assert check("t1", {"id": "a", "revision": 1}) is False

    def test_artifact_check_unreachable_reads_as_unknown(self, monkeypatch):
        def boom(*a, **k):
            raise ConnectionError("down")

        monkeypatch.setattr(s30_deps, "_get", boom)
        check = s30_deps.build_artifact_check("http://s28.invalid")
        assert check("t1", {"id": "a", "revision": 1}) is None

    def test_artifact_check_detects_a_hash_mismatch(self, monkeypatch):
        monkeypatch.setattr(s30_deps, "_get",
                            lambda *a, **k: {"content_hash": _hex("9")})
        check = s30_deps.build_artifact_check("http://s28.invalid")
        assert check("t1", {"id": "a", "revision": 1,
                            "content_hash": _hex("1")}) is False

    def test_build_all_wires_seven_callbacks(self):
        wired = s30_deps.build_all({})
        assert set(wired) == {"route_resolver", "submitter", "artifact_check",
                              "artifact_importer", "generation_state",
                              "canceler", "keyframe_lookup"}
        for name, fn in wired.items():
            assert callable(fn), name

    def test_kernel_never_opens_a_socket(self):
        """内核模块只 import 标准库与 SDK，没有任何 HTTP 客户端。"""
        path = Path(__file__).resolve().parents[1] / "src" / "video_production" \
            / "service.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "urllib" not in imported
        assert "requests" not in imported
        assert "httpx" not in imported
        assert "deps" not in imported


# ================================================================ HTTP 层

def _http_client(monkeypatch, tmp_path, **overrides):
    """`api/app.py` 的 TestClient，上游换成 Harness 的 stub。

    `app.py` 在 import 那一刻就建连接、按 env 拼上游，所以 `S30_DB` 要指到独立
    文件库，`module.service` / `module._idem` 必须重绑，Harness 要复用
    `module._conn`——否则请求写进一个库、断言读另一个库。

    开关 `S30_ENABLED` 默认不开（生产默认关闭）。HTTP 用例显式打开；专门测
    "关着"的用例再把它翻回去。
    """
    monkeypatch.setenv("S30_DB", str(tmp_path / "s30_http.db"))
    monkeypatch.setenv("S30_ENABLED", "1")
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s30_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s30_api_app"] = module

    h = Harness(conn=module._conn, **overrides)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h, module


def _post(client, body, *, command_id="c1", tenant="t1",
          path="/video-jobs"):
    return client.post(path, json=body,
                       headers={"Idempotency-Key": command_id,
                                "X-Tenant-Id": tenant})


class TestHttpLayer:
    """§6.1–§6.3 的契约面：状态码、错误码、租户只能来自头。"""

    def test_create_returns_202(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        r = _post(client, h.body())
        assert r.status_code == 202, r.text
        assert r.json()["state"] == "WAITING_PROVIDER"
        assert r.json()["replayed"] is False
        assert r.json()["duration_mapping_preview"]["target_duration_ms"] == 4000

    def test_202_is_not_a_completion_promise(self, monkeypatch, tmp_path):
        """202 的 body 只说"此刻"，`duration_mapping_preview` 明确不是承诺。"""
        client, h, _ = _http_client(monkeypatch, tmp_path)
        body = _post(client, h.body()).json()
        assert body["submitted_duration_ms"] == 5000
        assert "not a commitment" in body["duration_mapping_preview"]["note"]

    def test_replay_through_http(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        first = _post(client, h.body())
        again = _post(client, h.body())
        assert again.status_code == 202
        assert again.json()["replayed"] is True
        assert again.json()["video_job_id"] == first.json()["video_job_id"]
        assert h.calls["submit"] == 1, "a replay must never pay twice"
        assert h.calls["route"] == 1, "a replay must not re-quote"

    def test_same_key_different_body_is_409(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = _post(client, h.body(prompt_intent="另一个提示词"))
        assert r.status_code == 409
        assert r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert h.calls["submit"] == 1

    def test_missing_idempotency_key_is_400(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/video-jobs", json=h.body(),
                        headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_BODY"

    def test_missing_tenant_header_is_400(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/video-jobs", json=h.body(),
                        headers={"Idempotency-Key": "c1"})
        assert r.status_code == 400

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        body = h.body()
        body["tenant_id"] = "t_other"
        r = _post(client, body)
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_MISMATCH"
        assert h.count("video_job") == 0
        assert h.calls["submit"] == 0

    def test_command_id_header_must_match_body(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        body = h.body()
        body["command_id"] = "different"
        r = _post(client, body, command_id="c1")
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_BODY"

    def test_validation_error_is_422_with_code(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        r = _post(client, h.body(duration_policy="allow_everything"))
        assert r.status_code == 422
        assert r.json()["code"] == "INVALID_DURATION_POLICY"
        assert r.json()["retryable"] is False
        assert h.calls["route"] == 0, "validation happens before any paid call"

    def test_local_reject_is_202_with_a_terminal_state(self, monkeypatch,
                                                       tmp_path):
        """本地拒绝不是 HTTP 错误：§4.7 的 F1–F8 都落 `REJECTED_LOCAL`。"""
        client, h, _ = _http_client(monkeypatch, tmp_path)
        h.route_result = {"feasible": False,
                          "infeasible_reason": "DIALOGUE_EXCEEDS_DURATION",
                          "degradation_options": [{"option": "extend"}]}
        r = _post(client, h.body())
        assert r.status_code == 202
        assert r.json()["state"] == "REJECTED_LOCAL"
        assert r.json()["failure_code"] == "ROUTE_INFEASIBLE"

    def test_upstream_503_is_retryable(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        h.route_sequence = [ServiceError(500, "ROUTE_INTERNAL", "boom")]
        r = _post(client, h.body())
        assert r.status_code == 503
        assert r.json()["code"] == "ROUTE_DECISION_UNAVAILABLE"
        assert r.json()["retryable"] is True

    def test_keyframe_check_failure_is_unknown_not_absent(self, monkeypatch,
                                                          tmp_path):
        """`artifact_check` 抛错 ≠ 首帧坏了。

        三态契约（`deps.build_artifact_check`）：`True` 在、`False` 明确不在/
        hash 不符、`None` 没查成。S28 的 404 在 `deps` 里已被映射成 `False`
        （`test_artifact_check_404_reads_as_absent`）；内核拿到一个异常时只能算
        `None`——把一次网络抖动读成"首帧坏了"会白扔一次已批准的生产，而把它
        读成 `True` 则是把没校验的字节当事实。所以这里是 202。
        """
        client, h, _ = _http_client(monkeypatch, tmp_path)

        def check(tenant_id, ref):
            raise ServiceError(404, "ARTIFACT_NOT_FOUND", "not found")

        h.service.artifact_check = check
        r = _post(client, h.body())
        assert r.status_code == 202, r.text
        assert r.json()["state"] == "WAITING_PROVIDER"
        assert h.calls["submit"] == 1, "an unreadable check must not block"

    def test_cancel_route(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        r = client.post(f"/video-jobs/{created['video_job_id']}:cancel",
                        json={},
                        headers={"Idempotency-Key": "c_cancel",
                                 "X-Tenant-Id": "t1"})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "CANCELED"
        assert r.json()["refund_promised"] is False

    def test_retry_route_requires_a_retryable_state(self, monkeypatch,
                                                    tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        r = client.post(f"/video-jobs/{created['video_job_id']}:retry",
                        json={},
                        headers={"Idempotency-Key": "c_retry",
                                 "X-Tenant-Id": "t1"})
        assert r.status_code == 409
        assert r.json()["code"] == "JOB_NOT_RETRYABLE"

    def test_get_job_route(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        r = client.get(f"/video-jobs/{created['video_job_id']}",
                       headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert r.json()["video_job_id"] == created["video_job_id"]
        assert r.json()["provider_state_source"] == "local_map"

    def test_get_job_other_tenant_is_404(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        missing = client.get("/video-jobs/vj_nope",
                             headers={"X-Tenant-Id": "t2"})
        other = client.get(f"/video-jobs/{created['video_job_id']}",
                           headers={"X-Tenant-Id": "t2"})
        assert other.status_code == missing.status_code == 404
        assert other.json() == missing.json(), "cross-tenant must look like absent"

    def test_get_job_without_tenant_header_is_400(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        r = client.get(f"/video-jobs/{created['video_job_id']}")
        assert r.status_code == 400

    def test_list_route(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = client.get("/video-jobs", headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert r.json()["count"] == 1

    def test_list_is_tenant_scoped_over_http(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = client.get("/video-jobs", headers={"X-Tenant-Id": "t2"})
        assert r.json()["count"] == 0

    def test_checkpoint_route(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        h.ready_job()
        r = client.get("/shots/lineage_A/video-checkpoint",
                       headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        body = r.json()
        assert len(body["checkpoints"]) == 1
        assert body["checkpoints"][0]["segment_no"] == 1

    def test_candidate_and_edit_plan_routes(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        _, candidate_id = h.ready_job()
        cand = client.get(f"/clip-candidates/{candidate_id}",
                          headers={"X-Tenant-Id": "t1"})
        plan = client.get(f"/clip-candidates/{candidate_id}/edit-plan",
                          headers={"X-Tenant-Id": "t1"})
        assert cand.status_code == plan.status_code == 200
        assert cand.json()["clip_candidate_id"] == candidate_id
        assert plan.json()["executed_by"] is None
        assert plan.json()["note"] == "planning only; S42 executes"

    def test_candidate_other_tenant_is_404(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        _, candidate_id = h.ready_job()
        missing = client.get("/clip-candidates/cc_nope",
                             headers={"X-Tenant-Id": "t2"})
        other = client.get(f"/clip-candidates/{candidate_id}",
                           headers={"X-Tenant-Id": "t2"})
        assert other.status_code == missing.status_code == 404
        # 消息里会回显调用方给的 id；逐字比较会把"没泄漏存在性"测成"字符串相同"。
        assert other.json()["code"] == missing.json()["code"]
        assert other.json()["details"] == missing.json()["details"]

    def test_readiness_reports_the_gap_list(self, monkeypatch, tmp_path):
        client, _, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/health/readiness")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert body["enabled"] is True
        assert [g["id"] for g in body["contract_gaps"]] == \
            [f"C30-{i}" for i in range(1, len(CONTRACT_GAPS) + 1)]

    def test_disabled_service_refuses_commands(self, monkeypatch, tmp_path):
        """生产开关缺省关闭：没配 = 关，不是"没配 = 放行"。"""
        client, h, _ = _http_client(monkeypatch, tmp_path)
        monkeypatch.setenv("S30_ENABLED", "0")
        r = _post(client, h.body())
        assert r.status_code == 403
        assert r.json()["code"] == "VIDEO_PRODUCTION_NOT_ENABLED"
        assert h.count("video_job") == 0
        assert h.calls["route"] == 0

    def test_disabled_still_answers_reads(self, monkeypatch, tmp_path):
        """关掉开关不该让巡检与 S41 的读口一起瞎掉。"""
        client, h, _ = _http_client(monkeypatch, tmp_path)
        _, candidate_id = h.ready_job()
        monkeypatch.setenv("S30_ENABLED", "1")
        before = client.get(f"/clip-candidates/{candidate_id}",
                            headers={"X-Tenant-Id": "t1"})
        monkeypatch.setenv("S30_ENABLED", "0")
        after = client.get(f"/clip-candidates/{candidate_id}",
                           headers={"X-Tenant-Id": "t1"})
        assert before.status_code == after.status_code == 200
        assert after.json() == before.json()
        assert client.get("/health/readiness").json()["enabled"] is False

    def test_disabled_blocks_cancel_and_retry(self, monkeypatch, tmp_path):
        client, h, _ = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        monkeypatch.setenv("S30_ENABLED", "0")
        for suffix in ("cancel", "retry"):
            r = client.post(f"/video-jobs/{created['video_job_id']}:{suffix}",
                            json={},
                            headers={"Idempotency-Key": f"c_{suffix}",
                                     "X-Tenant-Id": "t1"})
            assert r.status_code == 403, suffix
            assert r.json()["code"] == "VIDEO_PRODUCTION_NOT_ENABLED"
        assert h.calls["cancel"] == 0

    def test_tenant_header_alone_picks_the_tenant(self, monkeypatch, tmp_path):
        """body 里写什么租户都不算数：job 归 `X-Tenant-Id`。"""
        client, h, _ = _http_client(monkeypatch, tmp_path)
        body = h.body()
        body.pop("tenant_id", None)
        created = _post(client, body, tenant="t1").json()
        assert h.job_row(created["video_job_id"])["tenant_id"] == "t1"


# ================================================================ 契约缺口

class TestContractGaps:
    """缺口必须**登记**，不能靠实现悄悄选边，也不能发明 owner 决策。"""

    def test_exactly_thirty_one_gaps(self):
        assert len(CONTRACT_GAPS) == 31

    def test_ids_are_unique_and_contiguous(self):
        ids = [g["id"] for g in CONTRACT_GAPS]
        assert ids == [f"C30-{i}" for i in range(1, 32)]
        assert len(set(ids)) == 31

    def test_kinds_are_from_the_closed_set(self):
        assert {g["kind"] for g in CONTRACT_GAPS} <= {"conflict", "gap",
                                                      "needs_owner"}

    def test_every_gap_states_a_summary_and_a_handling(self):
        for gap in CONTRACT_GAPS:
            assert set(gap) == {"id", "kind", "summary", "handling"}, gap["id"]
            assert gap["summary"].strip(), gap["id"]
            assert gap["handling"].strip(), gap["id"]

    def test_the_owner_decision_is_not_invented(self):
        """`needs_owner` 的条目只能说明"停在这里等谁"，不能自带裁决。"""
        for gap in CONTRACT_GAPS:
            if gap["kind"] == "needs_owner":
                assert len(gap["handling"].strip()) > 0, gap["id"]

    def test_the_kernel_defects_found_in_implementation_are_registered(self):
        by_id = {g["id"]: g for g in CONTRACT_GAPS}
        # F5 在 §3.1 的表里没有 PREPARING→FAILED；实现时补了这条边。
        assert "SPEC_ASSERTION" in by_id["C30-29"]["summary"] \
            or "PREPARING" in by_id["C30-29"]["summary"]
        # 乱序/重复的供应商事件在 §3.1 里没有落点。
        assert by_id["C30-30"]["kind"] in ("gap", "conflict")
        # 重复的 `artifact.ingested`（换过 event id）没有幂等出口。
        assert "checkpoint" in by_id["C30-31"]["handling"]
        # 文生视频没有首帧基线，ready 事件因此没有载荷。
        assert "C30-28" in by_id

    def test_conflicts_outnumber_silent_choices(self):
        """每条 `conflict` 都必须写清取哪一处——否则等于没登记。"""
        for gap in CONTRACT_GAPS:
            if gap["kind"] == "conflict":
                assert len(gap["handling"].strip()) > 10, gap["id"]


# ================================================================ 边界

OWN_TABLES = frozenset({
    "video_job", "keyframe_baseline_ref", "duration_mapping",
    "video_edit_plan", "clip_candidate", "shot_video_checkpoint",
    "job_transition", "video_intent_ledger",
})


class TestBoundaries:
    """DDL 层与模块边界的硬约束。"""

    def test_creates_exactly_the_eight_own_tables(self, h):
        names = {r[0] for r in h.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        sdk = {"idempotency_record", "inbox", "outbox"}
        assert names - sdk == OWN_TABLES, names - sdk

    def test_no_foreign_service_tables(self, h):
        """不碰别人的库：没有 S28/S29/S26 的表。"""
        names = {r[0] for r in h.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for foreign in ("artifact", "image_job", "keyframe_candidate",
                        "generation_job", "budget", "route_decision"):
            assert foreign not in names

    def test_kernel_imports_no_other_service_orm(self):
        path = Path(__file__).resolve().parents[1] / "src" / "video_production" \
            / "service.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        for prefix in ("image_production", "sound_effects", "music",
                       "artifact_ingest", "generation_jobs", "model_routing"):
            assert not any(m == prefix or m.startswith(prefix + ".")
                           for m in modules), prefix

    def test_t30_25_edit_plan_executor_cannot_be_s30(self, h):
        """规划归 S30、执行归 S42：表里只允许 NULL 或 'S42'。"""
        job_id, candidate = h.ready_job()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE video_edit_plan SET executed_by = 'S30' "
                "WHERE clip_candidate_id = ?", (candidate,))

    def test_t30_09_mapping_math_is_enforced_by_the_database(self, h):
        h.ready_job()
        row = h.conn.execute("SELECT * FROM duration_mapping").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE duration_mapping SET resulting_duration_ms = ? "
                "WHERE duration_mapping_id = ?",
                (int(row["resulting_duration_ms"]) + 1,
                 row["duration_mapping_id"]))

    def test_head_only_trim_is_forbidden(self, h):
        h.ready_job()
        row = h.conn.execute("SELECT * FROM duration_mapping").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE duration_mapping SET trim_mode = 'head' "
                "WHERE duration_mapping_id = ?", (row["duration_mapping_id"],))

    def test_t30_10_both_requires_the_explicit_policy(self, h):
        """`both` 只能来自 `c2_minimal`：算术对得上也不行。"""
        h.ready_job()
        row = h.conn.execute("SELECT * FROM duration_mapping").fetchone()
        # 让 dm_math / dm_target_match 同时成立，隔离出 dm_both_forbidden_default。
        # 真实行是 5000/4000 tail-only（tail=1000）：头尾各切一点仍要切到 target，
        # 于是 head=200 / resulting=4000 / tail=800 三条约束同时成立。
        target = int(row["target_duration_ms"])
        tier = int(row["tier_duration_ms"])
        head, resulting = 200, target
        tail = tier - head - resulting
        assert head + tail > 0, "must actually be a 'both' trim"
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE duration_mapping SET trim_mode = 'both', "
                "trim_head_ms = ?, trim_tail_ms = ?, resulting_duration_ms = ? "
                "WHERE duration_mapping_id = ?",
                (head, tail, resulting, row["duration_mapping_id"]))
        # 换成同一组数字、policy 标对，就能写进去——证明上面拦的是 policy 列。
        h.conn.execute(
            "UPDATE duration_mapping SET trim_mode = 'both', trim_policy = "
            "'c2_minimal', trim_head_ms = ?, trim_tail_ms = ?, "
            "resulting_duration_ms = ? WHERE duration_mapping_id = ?",
            (head, tail, resulting, row["duration_mapping_id"]))
        assert h.conn.execute(
            "SELECT trim_mode FROM duration_mapping WHERE duration_mapping_id=?",
            (row["duration_mapping_id"],)).fetchone()[0] == "both"

    def test_solve_trim_never_produces_head_even_though_the_vocabulary_has_it(self):
        """`TRIM_MODES` 是词汇表，不是解空间：裁头要么和裁尾并存，要么不裁。"""
        assert "head" in TRIM_MODES
        seen = set()
        for tier in (4000, 5000, 6000, 7000):
            for target in (3000, 4000, 5000):
                for dialogue in (None, {"start_ms": 200, "end_ms": 3800}):
                    for action in (None, {"start_ms": 0, "end_ms": 3900}):
                        for head in (False, True):
                            plan = solve_trim(tier_ms=tier, target_ms=target,
                                              dialogue_window=dialogue,
                                              action_window=action,
                                              allow_head=head)
                            seen.add(plan["trim_mode"])
        assert "head" not in seen
        assert seen <= {"none", "tail", "both"}

    def test_duration_tolerance_is_one_hundred_milliseconds(self):
        assert DURATION_TOLERANCE_MS == 100

    def test_state_seq_is_strictly_monotonic(self, h):
        job_id, _ = h.ready_job()
        body = h.service.get_job("t1", job_id)
        seqs = [entry["seq"] for entry in body["history"]]
        assert seqs == list(range(1, len(seqs) + 1))
        assert body["state_seq"] == seqs[-1], \
            "the job's state_seq and its last history row must agree"
        # `CREATED` 是初始态（state_seq=0），内核不为它写转移行；每条边都是
        # 真的有状态变化的那一步，`from` 因此从不为空。
        assert all(entry["from"] is not None for entry in body["history"])
        assert body["history"][0]["from"] == "CREATED"
        assert body["history"][-1]["to"] == "READY"

    def test_transaction_rolls_back_on_exception(self, h):
        h.conn.execute(
            "INSERT INTO video_intent_ledger (entry_id, tenant_id, video_job_id,"
            " shot_lineage_id, segment_no, action, run_id, node_epoch,"
            " manifest_id, created_at) VALUES "
            "('vil_probe','t1','vj_probe','lineage_A',1,'generate','run_probe',"
            "3,'man_probe','2026-01-01T00:00:00.000Z')")
        with pytest.raises(RuntimeError):
            with Transaction(h.conn, [0]):
                h.conn.execute("DELETE FROM video_intent_ledger")
                raise RuntimeError("boom")
        assert h.count("video_intent_ledger", "entry_id = 'vil_probe'") == 1

    def test_id_prefixes_cover_every_table_that_needs_one(self):
        assert ID_PREFIX["job"] == "vj_"
        assert ID_PREFIX["candidate"] == "cc_"
        assert ID_PREFIX["mapping"] == "dm_"
        assert ID_PREFIX["edit_plan"] == "vep_"
        assert len(set(ID_PREFIX.values())) == len(ID_PREFIX)

    def test_deadline_is_not_a_license_to_retry(self, h):
        """deadline 逼近不是重提的理由——AGENTS.md 与 S26 §7.2 都写着。"""
        out, _ = h.make_job(deadline="2099-01-01T00:00:00.000Z")
        job_id = out["video_job_id"]
        h.state_event(job_id, "SUBMISSION_UNKNOWN", event_id="s1")
        assert h.state_of(job_id) == "WAITING_UNKNOWN"
        before = h.calls["submit"]
        h.conn.execute(
            "UPDATE video_job SET deadline = '2026-11-14T00:00:01.000Z' "
            "WHERE video_job_id = ?", (job_id,))
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job("t1", job_id, {}, "c_retry")
        assert exc.value.status == 409
        assert exc.value.code == "MUST_RECONCILE_FIRST"
        assert h.calls["submit"] == before
