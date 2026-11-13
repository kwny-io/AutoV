"""S42 单测。编号对应 `plans/S42-composition.md` §9 的用例与 `docs/16` 的验收条目。

三组是本文件存在的理由：

- `TestVersionColumns` —— `execution_fence`（run/branch）与 `node_epoch`
  （单个 logical_node）是**两列两作用域**。合并、互赋、取 max、共用错误码
  都会让"哪个作用域旧了"无法判断，而旧 epoch 的结果永远不能推进任何东西。
- `TestIdempotency` / `TestHttpLayer` —— 幂等键 = `tenant_id + command_id +
  request_hash`；同键同 body 重放，同键不同 body 是 409（**不是覆盖**）。
- `TestIsolation` —— 跨租户与不存在返回**同一个响应体**；差异本身就是泄漏。

上游全部是进程内闭包 stub，按 `deps.py` 七条路径的**归一化形状**写。
没有任何 socket、没有模型、没有付费。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import SqliteIdempotencyStore

from composition import schema
from composition.service import (
    CompositionService,
    ServiceError,
    build_mix_graph,
    compute_graph_hash,
    loudness_gate,
    render_env_hash,
    segment_key,
)

TENANT = "t_alpha"
OTHER = "t_beta"

VID_HASH = "a" * 64
AUD_HASH = "b" * 64
TIMELINE_HASH = "c" * 64
MIX_HASH = "d" * 64
SPEC_HASH = "e" * 64
SUB_HASH = "f" * 64
SUB_HASH_2 = "1" * 64
STEM_HASH = "2" * 64
STEM_HASH_2 = "3" * 64

FUTURE = "2099-01-01T00:00:00.000Z"
PAST = "2000-01-01T00:00:00.000Z"

DURATION_TICKS = 180000          # 90000 tick/s → 2 秒
FRAME_COUNT = 60                 # 2 秒 × 30fps

DEFAULT_SPEC = {
    "container": "mp4",
    "video_codec": "h264",
    "video_params": {"preset": "medium", "crf": 18},
    "audio_codec": "aac",
    "audio_params": {"bitrate_kbps": 192},
    "color_primaries": "bt709",
    "transfer_function": "bt709",
    "matrix_coefficients": "bt709",
    "loudness": {"target_lufs": -16.0, "tolerance_lu": 1.0,
                 "true_peak_ceiling_dbtp": -1.0},
}

SPEC_REF = {"service_id": "S01", "id": "spec_1", "revision": 1,
            "content_hash": SPEC_HASH}


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _context(tenant=TENANT, epoch=3, fence=9, command_id="cmd_1"):
    """平台签名后的 execution-context.v2。18 个必填键一个不少。

    **没有 `run_id`**：`execution-context.v2` 的 18 个键里没有它。
    """
    return {
        "schema_version": "2.0", "tenant_id": tenant,
        "project_id": "proj_1", "series_id": "ser_1",
        "branch_id": "br_main", "episode_id": "ep_3",
        "home_region": "cn-east", "home_cell": "cell_a",
        "manifest_ref": {"id": "man_7", "revision": 12,
                         "content_hash": "9" * 64},
        "node_id": "node_compose_ep3", "node_epoch": epoch,
        "execution_fence": fence,
        "memory_checkpoint": None,
        "policy_ref": {"service_id": "S02", "id": "pol_1", "revision": 2,
                       "content_hash": "8" * 64},
        "actor": {"principal_id": "u_1", "principal_type": "human",
                  "delegation_id": None},
        "command_id": command_id, "request_hash": "7" * 64,
        "trace_id": "trace_1",
    }


def _ref(identifier, revision=1, content_hash=VID_HASH, service_id="S28"):
    return {"service_id": service_id, "id": identifier,
            "revision": revision, "content_hash": content_hash}


# ------------------------------------------------------------------ 夹具

class Harness:
    """进程内上游 + 一个真 SQLite 上的 `CompositionService`。

    七个 callback 覆盖 `deps.py` 的归一化返回形状；`artifacts` / `subtitles`
    按 `(tenant, id, revision)` 存，这样跨租户天然是 404 —— 与 S28 的真实
    行为一致，也让 E28-S42-02「S28 拒绝 → S42 转 422」可以真跑。
    """

    def __init__(self, conn=None, path=None):
        if conn is None:
            if path is None:
                conn = sqlite3.connect(":memory:", check_same_thread=False,
                                       isolation_level=None)
            else:
                conn = sqlite3.connect(path, check_same_thread=False,
                                       isolation_level=None)
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

        self.artifacts: dict[tuple, dict] = {}
        self.subtitles: dict[tuple, str] = {}
        self.specs: dict[tuple, dict] = {}
        self.timeline_payload = self._default_timeline()
        self.affected_ranges: list[dict] = []
        self.permit_state = "GRANTED"
        self.permit_requests: list[dict] = []
        self.usage_requests: list[dict] = []
        self.usage_down = False
        self.calls: Counter = Counter()

        self.service = CompositionService(self.conn, **self._deps())
        self.idem = SqliteIdempotencyStore(self.conn)

    # -- 上游 stub ---------------------------------------------------─

    def _deps(self):
        return {
            "timeline_source": self._timeline,
            "timeline_diff": self._diff,
            "artifact_facts": self._facts,
            "subtitle_source": self._subtitle,
            "master_spec_source": self._spec,
            "permit_source": self._permit,
            "usage_reporter": self._report,
        }

    def _timeline(self, tenant_id, ref):
        self.calls["timeline"] += 1
        return copy.deepcopy(self.timeline_payload)

    def _diff(self, tenant_id, timeline_id, from_revision, to_revision):
        self.calls["diff"] += 1
        return {"affected_ranges": copy.deepcopy(self.affected_ranges),
                "diff_mode": "exact"}

    def _facts(self, tenant_id, ref):
        self.calls["facts"] += 1
        entry = self.artifacts.get((tenant_id, ref.get("id"),
                                    ref.get("revision")))
        if entry is None:
            # S28 对跨租户/不存在都是 404；S42 侧把它转成「缺失」。
            raise ServiceError(404, "NOT_FOUND", "artifact not found")
        return {"artifact_id": ref.get("id"), "revision": ref.get("revision"),
                **entry}

    def _subtitle(self, tenant_id, ref):
        self.calls["subtitle"] += 1
        identifier = ref.get("id") or ref.get("artifact_id")
        digest = self.subtitles.get((tenant_id, identifier,
                                     ref.get("revision")))
        if digest is None:
            raise ServiceError(404, "NOT_FOUND", "subtitle not found")
        return {"id": identifier, "revision": ref.get("revision"),
                "content_hash": digest}

    def _spec(self, tenant_id, ref):
        self.calls["spec"] += 1
        key = (tenant_id, ref.get("id"), ref.get("revision"))
        return {"spec": copy.deepcopy(self.specs.get(key, DEFAULT_SPEC))}

    def _permit(self, tenant_id, request):
        self.calls["permit"] += 1
        self.permit_requests.append(request)
        return {"permit_id": "pm_1", "pool_id": "pool_a",
                "state": self.permit_state, "lease_expires_at": FUTURE}

    def _report(self, tenant_id, request):
        self.calls["usage"] += 1
        if self.usage_down:
            raise ConnectionError("S23 down")
        self.usage_requests.append(request)
        return {"recorded": True}

    # -- 夹具工具 ---------------------------------------------------─

    def _default_timeline(self):
        return {
            "timeline_id": "tl_1", "revision": 10,
            "content_hash": TIMELINE_HASH,
            "duration_ticks": DURATION_TICKS, "completeness": 1.0,
            "formal_master_eligible": True, "tick_rate": 90000,
            "fps": {"num": 30, "den": 1},
            "clips": [{
                "src_artifact_ref": _ref("vid_a", 1, VID_HASH),
                "src_in_ticks": 0, "src_out_ticks": DURATION_TICKS,
                "rec_in_ticks": 0}],
            "output_plan": {"duration_ticks": DURATION_TICKS, "segments": [
                {"segment_no": 1, "rec_in": 0, "rec_out": 90000},
                {"segment_no": 2, "rec_in": 90000, "rec_out": DURATION_TICKS}]},
            "mix_plan": {"plan_hash": MIX_HASH, "target_lufs": -16.0,
                         "true_peak_ceiling": -1.0, "stem_gains": {}},
        }

    def set_timeline(self, **over):
        self.timeline_payload = {**self._default_timeline(), **over}

    def put_artifact(self, identifier, revision=1, content_hash=VID_HASH,
                     tenant=TENANT, tombstoned=False):
        self.artifacts[(tenant, identifier, revision)] = {
            "content_hash": content_hash, "tombstoned": tombstoned,
            "state": "TOMBSTONED" if tombstoned else "READY"}
        return _ref(identifier, revision, content_hash)

    def put_subtitle(self, identifier="sub_1", revision=1,
                     content_hash=SUB_HASH, tenant=TENANT):
        self.subtitles[(tenant, identifier, revision)] = content_hash
        return _ref(identifier, revision, content_hash, service_id="S38")

    def body(self, command_id="cmd_1", tenant=TENANT, **over):
        payload = {
            "command_id": command_id,
            "request_hash": "7" * 64,
            "context": _context(tenant=tenant, command_id=command_id),
            "purpose": "formal",
            "timeline_ref": {"timeline_id": "tl_1", "revision": 10,
                             "content_hash": TIMELINE_HASH},
            "master_spec_ref": dict(SPEC_REF),
        }
        payload.update(over)
        return payload

    def create(self, command_id="cmd_1", tenant=TENANT, **over):
        self.put_artifact("vid_a", 1, VID_HASH, tenant=tenant)
        return self.service.create_render_job(
            tenant, command_id, self.body(command_id, tenant=tenant, **over),
            "h_" + command_id)

    def rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        found = self.conn.execute(sql, args).fetchone()
        return None if found is None else dict(found)

    def outbox(self, event_type=None):
        rows = self.rows("SELECT payload FROM outbox ORDER BY created_at,"
                         " aggregate_id, sequence")
        payloads = [json.loads(r["payload"]) for r in rows]
        if event_type is not None:
            payloads = [p for p in payloads if p["type"] == event_type]
        return payloads

    def upload_all(self, job_id, fence=9, epoch=3, gpu_ms=100):
        """把 job 的所有段按序回报为已上传，返回各自的 ref。"""
        job = self.service.get_render_job(TENANT, job_id)
        refs = []
        for segment in job["segments"]:
            if segment["state"] in ("DONE", "REUSED"):
                refs.append(segment["output_artifact_ref"])
                continue
            digest = _sha(f"{job_id}:{segment['segment_no']}")
            ref = self.put_artifact(f"seg_{segment['segment_no']}", 1, digest)
            self.service.record_segment_uploaded(
                TENANT, job_id, segment_no=segment["segment_no"],
                output_ref=ref, consumed_gpu_ms=gpu_ms,
                execution_fence=fence, node_epoch=epoch)
            refs.append(ref)
        return refs

    def measurement(self, integrated=-16.0, peak=-1.2):
        return {"integrated_lufs": integrated, "true_peak_dbtp": peak,
                "lra_lu": 7.0, "measurement_tool": "ffmpeg-ebur128",
                "measurement_version": "6.0"}

    def finish(self, job_id, audio_hash=AUD_HASH, **over):
        audio = self.put_artifact("aud_master", 1, audio_hash)
        video = self.put_artifact("vid_master", 1, VID_HASH)
        kwargs = {"video_ref": video, "audio_ref": audio,
                  "measurement": self.measurement(),
                  "frame_count": FRAME_COUNT}
        kwargs.update(over)
        return self.service.finish_render_job(TENANT, job_id, **kwargs)

    def render_to_candidate(self, command_id="cmd_1"):
        job_id = self.create(command_id)["render_job_id"]
        self.upload_all(job_id)
        return self.finish(job_id), job_id


def _new_service(path):
    """模拟一次进程重启：同一文件库上重新构造服务。"""
    conn = sqlite3.connect(path, check_same_thread=False,
                           isolation_level=None)
    return Harness(conn=conn)


@pytest.fixture
def h():
    return Harness()


@pytest.fixture
def files(tmp_path):
    return Harness(path=str(tmp_path / "s42.db"))


# ------------------------------------------------------------------ 契约校验

_CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _load_contract(name: str) -> dict:
    return json.loads((_CONTRACTS / name).read_text(encoding="utf-8"))


def _validate_event(name: str, envelope: dict) -> None:
    """按发布契约校验事件的 `data`。schema 自己是 `additionalProperties:false`，
    多一个字段就会红 —— 这就是"事件形状没有自定义余地"的检查。"""
    jsonschema.validate(instance=envelope["data"], schema=_load_contract(name))


# ------------------------------------------------------------------ 创建

class TestCreateRenderJob:
    def test_happy_path_writes_job_segments_and_graph(self, h):
        out = h.create()
        assert out["state"] == "QUEUED"
        assert out["segments"] == 2
        assert out["render_job_id"].startswith("rj_")
        assert out["operation_id"] == out["render_job_id"]

        job = h.one("SELECT * FROM render_job WHERE render_job_id=?",
                    (out["render_job_id"],))
        assert job["tenant_id"] == TENANT
        assert job["timeline_revision"] == 10
        assert job["timeline_content_hash"] == TIMELINE_HASH
        assert job["mix_plan_hash"] == MIX_HASH
        assert job["purpose"] == "formal"
        # 四个版本是四列，各算各的。
        assert job["render_profile_hash"] != job["render_env_hash"]
        assert len(job["render_profile_hash"]) == 64
        assert len(job["render_env_hash"]) == 64
        assert len(job["graph_hash"]) == 64

        segments = h.rows("SELECT * FROM render_segment WHERE render_job_id=?"
                          " ORDER BY segment_no", (out["render_job_id"],))
        assert [s["state"] for s in segments] == ["PENDING", "PENDING"]
        assert [s["rec_in_ticks"] for s in segments] == [0, 90000]
        assert [s["rec_out_ticks"] for s in segments] == [90000, 180000]
        assert all(len(s["segment_key"]) == 64 for s in segments)
        assert segments[0]["segment_key"] != segments[1]["segment_key"]

        graph = h.one("SELECT * FROM filter_graph WHERE graph_hash=?",
                      (job["graph_hash"],))
        assert graph["graph_version"] == schema.GRAPH_VERSION
        body = json.loads(graph["graph"])
        assert body["graph_version"] == "ffgraph-v1"
        assert compute_graph_hash(body) == job["graph_hash"]
        # 两遍 loudnorm，不是一遍。
        modes = [n.get("mode") for n in body["nodes"]
                 if n["kind"] == "loudnorm"]
        assert modes == ["measure", "apply"]

    def test_permit_is_acquired_before_the_job_row(self, h):
        """§4.1 第 6 步在前。反过来会造出"排队但永远拿不到许可"的僵尸。"""
        h.permit_state = "DENIED"
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 422 and exc.value.code == "BUDGET_DENIED"
        assert h.rows("SELECT * FROM render_job") == []
        assert h.rows("SELECT * FROM render_segment") == []
        assert h.outbox() == []
        assert h.calls["permit"] == 1

    def test_permit_carries_amount_from_timeline_duration(self, h):
        h.create()
        request = h.permit_requests[0]
        assert request["resource_class"] == "gpu_second"
        # 2 秒 × 600ms/秒 = 1200ms
        assert request["requested_ms"] == 1200
        assert request["estimated_cpu_ms"] == 240

    def test_preview_uses_cpu_class_and_skips_l1_gate(self, h):
        h.set_timeline(formal_master_eligible=False, completeness=0.5)
        out = h.create(purpose="preview")
        assert out["state"] == "QUEUED"
        assert h.permit_requests[0]["resource_class"] == "cpu_media_second"

    def test_formal_rejects_ineligible_timeline(self, h):
        h.set_timeline(formal_master_eligible=False, completeness=0.4)
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 422
        assert exc.value.code == "INCOMPLETE_TIMELINE"
        assert exc.value.details == {"completeness": 0.4}

    def test_unknown_purpose_is_rejected(self, h):
        with pytest.raises(ServiceError) as exc:
            h.create(purpose="final")
        assert exc.value.code == "SCHEMA_INVALID"

    def test_master_spec_ref_is_required_no_default_encoder(self, h):
        """E37-S42-02：缺规格就 422，不默认 H.264、不默认响度目标。"""
        with pytest.raises(ServiceError) as exc:
            h.create(master_spec_ref=None)
        assert exc.value.status == 422
        assert exc.value.code == "MASTER_SPEC_REQUIRED"
        assert h.rows("SELECT * FROM render_job") == []

    def test_unsupported_codec_is_rejected_from_closed_set(self, h):
        h.specs[(TENANT, "spec_1", 1)] = {**DEFAULT_SPEC,
                                          "video_codec": "av1"}
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.code == "UNSUPPORTED_SPEC"

    def test_mix_target_is_required(self, h):
        h.set_timeline(mix_plan={"plan_hash": MIX_HASH,
                                 "true_peak_ceiling": -1.0})
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.code == "MIX_TARGET_REQUIRED"

    def test_empty_segments_is_upstream_502(self, h):
        h.set_timeline(output_plan={"duration_ticks": DURATION_TICKS,
                                    "segments": []})
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 502
        assert exc.value.code == "TIMELINE_PLAN_UNAVAILABLE"

    def test_missing_input_artifact_is_422_with_refs(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.create_render_job(TENANT, "cmd_miss",
                                        h.body("cmd_miss"), "hx")
        assert exc.value.status == 422
        assert exc.value.code == "INPUT_ARTIFACT_MISSING"
        assert exc.value.details["missing"][0]["reason"] == "NOT_FOUND"
        assert exc.value.details["missing"][0]["ref"]["id"] == "vid_a"

    def test_tombstoned_input_is_missing(self, h):
        h.put_artifact("vid_a", 1, VID_HASH, tombstoned=True)
        with pytest.raises(ServiceError) as exc:
            h.service.create_render_job(TENANT, "cmd_tomb",
                                        h.body("cmd_tomb"), "ht")
        assert exc.value.code == "INPUT_ARTIFACT_MISSING"
        assert exc.value.details["missing"][0]["reason"] == "TOMBSTONED"

    def test_input_hash_drift_is_missing(self, h):
        h.put_artifact("vid_a", 1, "4" * 64)
        with pytest.raises(ServiceError) as exc:
            h.service.create_render_job(TENANT, "cmd_drift",
                                        h.body("cmd_drift"), "hd")
        assert exc.value.details["missing"][0]["reason"] == "HASH_MISMATCH"

    def test_subtitle_revision_drift_is_rejected(self, h):
        h.put_subtitle(content_hash=SUB_HASH)
        stale_ref = _ref("sub_1", 1, SUB_HASH_2, "S38")
        with pytest.raises(ServiceError) as exc:
            h.create(subtitle_ref=stale_ref)
        assert exc.value.status == 422
        assert exc.value.code == "INPUT_ARTIFACT_MISSING"
        assert exc.value.details["actual"] == SUB_HASH

    def test_timeline_unavailable_is_503_not_a_guess(self, h):
        def boom(tenant_id, ref):
            raise ConnectionError("S41 down")
        h.service.timeline_source = boom
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 503
        assert exc.value.code == "TIMELINE_UNAVAILABLE"
        assert exc.value.retryable is True
        assert h.rows("SELECT * FROM render_job") == []

    def test_create_emits_no_event(self, h):
        """创建只落事实。事件在收尾与失败时才发。"""
        h.create()
        assert h.outbox() == []


# ------------------------------------------------------------------ 两个版本列

class TestVersionColumns:
    """`execution_fence` 与 `node_epoch` 是**两列两作用域**。

    这组测试的每一条都在问同一个问题：如果把两者合并、互赋、比较或取
    `max()`，哪一条会红？—— `test_two_columns_are_stored_separately` 与
    `test_each_column_has_its_own_check` 会红；错误码分开的两条也会红。
    """

    def test_two_columns_are_stored_separately(self, h):
        job_id = h.create()["render_job_id"]
        job = h.one("SELECT * FROM render_job WHERE render_job_id=?", (job_id,))
        assert job["execution_fence"] == 9
        assert job["node_epoch"] == 3
        view = h.service.get_render_job(TENANT, job_id)
        # 两个独立的 int，不是一个字符串、也不是取过 max 的值。
        assert view["execution_fence"] == 9
        assert view["node_epoch"] == 3

    def test_each_column_has_its_own_check(self, h):
        """两列各自 `CHECK (>= 1)`：把任一列改成 0 都会被库拒绝。"""
        job_id = h.create()["render_job_id"]
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute("UPDATE render_job SET execution_fence=0"
                           " WHERE render_job_id=?", (job_id,))
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute("UPDATE render_job SET node_epoch=0"
                           " WHERE render_job_id=?", (job_id,))

    def test_fence_and_epoch_are_independent(self, h):
        """一个旧、一个不旧 → 只报那一个。合并成一个检查会同时报或都不报。"""
        job_id = h.create()["render_job_id"]
        ref = h.put_artifact("seg_1", 1, _sha("s1"))

        with pytest.raises(ServiceError) as exc:
            h.service.record_segment_uploaded(
                TENANT, job_id, segment_no=1, output_ref=ref,
                execution_fence=1, node_epoch=4)
        assert exc.value.code == "STALE_FENCE"
        assert set(exc.value.details) == {"job_execution_fence",
                                          "reported_execution_fence"}

        with pytest.raises(ServiceError) as exc:
            h.service.record_segment_uploaded(
                TENANT, job_id, segment_no=1, output_ref=ref,
                execution_fence=9, node_epoch=2)
        assert exc.value.code == "STALE_EPOCH"
        assert set(exc.value.details) == {"job_node_epoch",
                                          "reported_node_epoch"}

    def test_stale_epoch_changes_nothing(self, h):
        """旧 epoch 的结果永远不推进任何东西 —— 连段状态都不改。"""
        job_id = h.create()["render_job_id"]
        ref = h.put_artifact("seg_1", 1, _sha("s1"))
        with pytest.raises(ServiceError):
            h.service.record_segment_uploaded(
                TENANT, job_id, segment_no=1, output_ref=ref,
                execution_fence=9, node_epoch=2)
        segment = h.one("SELECT * FROM render_segment WHERE render_job_id=?"
                        " AND segment_no=1", (job_id,))
        assert segment["state"] == "PENDING"
        assert segment["output_artifact_ref"] is None
        assert h.one("SELECT consumed_gpu_ms FROM render_job"
                     " WHERE render_job_id=?",
                     (job_id,))["consumed_gpu_ms"] == 0
        assert h.rows("SELECT * FROM render_usage") == []
        assert h.outbox() == []

    def test_newer_epoch_and_fence_are_accepted(self, h):
        job_id = h.create()["render_job_id"]
        ref = h.put_artifact("seg_1", 1, _sha("s1"))
        out = h.service.record_segment_uploaded(
            TENANT, job_id, segment_no=1, output_ref=ref,
            execution_fence=10, node_epoch=4)
        assert out["state"] == "DONE"

    def test_candidate_carries_fence_and_not_epoch(self, h):
        """候选只带 fence（契约 `post.master.rendered.execution_fence`）。

        这不是漏字段：候选是整片产物，`node_epoch` 的作用域是单个节点。
        """
        candidate, _job = h.render_to_candidate()
        assert candidate["execution_fence"] == 9
        assert "node_epoch" not in candidate
        columns = {r["name"] for r in h.rows(
            "PRAGMA table_info(master_candidate)")}
        assert "execution_fence" in columns
        assert "node_epoch" not in columns

    def test_outbox_sequence_is_per_aggregate_not_global(self, h):
        """三个 aggregate、三条事件，序号全是 1 —— 全局计数器会给出 1/2/3。"""
        candidate, _job1 = h.render_to_candidate("cmd_a")

        # 两个 job 各自失败 → 两个 render_job aggregate。
        h.set_timeline(output_plan={"duration_ticks": DURATION_TICKS,
                                    "segments": [{"segment_no": 1, "rec_in": 0,
                                                  "rec_out": 90000}]})
        failed = []
        for command in ("cmd_b", "cmd_c"):
            job_id = h.create(command)["render_job_id"]
            h.upload_all(job_id)
            h.put_artifact("aud_master", 1, AUD_HASH)
            h.put_artifact("vid_master", 1, VID_HASH)
            with pytest.raises(ServiceError) as exc:
                h.finish(job_id)    # 段没覆盖整片 → DURATION_MISMATCH
            assert exc.value.code == "DURATION_MISMATCH"
            failed.append(job_id)

        rows = h.rows("SELECT aggregate_id, sequence FROM outbox")
        by_aggregate = {}
        for row in rows:
            by_aggregate.setdefault(row["aggregate_id"], []).append(
                row["sequence"])
        assert by_aggregate == {candidate["candidate_id"]: [1],
                                failed[0]: [1],
                                failed[1]: [1]}
        # 同一 aggregate 上不重复。
        assert all(len(v) == len(set(v)) for v in by_aggregate.values())

    def test_one_candidate_gets_one_identity(self, h):
        """同一 manifest → 同一个候选。`candidate_hash_uniq` 的唯一键是
        `(tenant, episode, branch, manifest_content_hash)`，所以第二条事件的
        sequence 是 2 而不是造出第二个候选。"""
        first, _ = h.render_to_candidate("cmd_a")
        second, _ = h.render_to_candidate("cmd_b")
        assert first["candidate_id"] == second["candidate_id"]
        assert h.rows("SELECT * FROM master_candidate") != []
        assert len(h.rows("SELECT * FROM master_candidate")) == 1
        assert [e["sequence"] for e in h.outbox(schema.EVENT_MASTER_RENDERED)] \
            == [1, 2]

    def test_sequence_increments_within_one_aggregate(self, h):
        context = _context()
        service = h.service
        first = service._emit(tenant_id=TENANT, aggregate_id="rj_x",
                              event_type=schema.EVENT_RENDER_FAILED,
                              subject="episode/ep_3/render-job/rj_x",
                              context=context, data={"n": 1})
        second = service._emit(tenant_id=TENANT, aggregate_id="rj_x",
                               event_type=schema.EVENT_RENDER_FAILED,
                               subject="episode/ep_3/render-job/rj_x",
                               context=context, data={"n": 2})
        assert first["sequence"] == 1
        assert second["sequence"] == 2


# ------------------------------------------------------------------ 段回报

class TestSegments:
    def test_upload_requires_s28_confirmation(self, h):
        job_id = h.create()["render_job_id"]
        ref = _ref("seg_1", 1, _sha("s1"))
        with pytest.raises(ServiceError) as exc:
            h.service.record_segment_uploaded(
                TENANT, job_id, segment_no=1, output_ref=ref,
                execution_fence=9, node_epoch=3)
        assert exc.value.status == 502
        assert exc.value.code == "ARCHIVE_MISMATCH"
        assert h.one("SELECT state FROM render_segment WHERE segment_no=1"
                     " AND render_job_id=?",
                     (job_id,))["state"] == "PENDING"

    def test_upload_hash_drift_is_archive_mismatch(self, h):
        job_id = h.create()["render_job_id"]
        h.put_artifact("seg_1", 1, _sha("real"))
        wrong = _ref("seg_1", 1, _sha("claimed"))
        with pytest.raises(ServiceError) as exc:
            h.service.record_segment_uploaded(
                TENANT, job_id, segment_no=1, output_ref=wrong,
                execution_fence=9, node_epoch=3)
        assert exc.value.code == "ARCHIVE_MISMATCH"

    def test_double_upload_is_segment_terminal_not_overwrite(self, h):
        job_id = h.create()["render_job_id"]
        first = h.put_artifact("seg_1", 1, _sha("s1"))
        h.service.record_segment_uploaded(
            TENANT, job_id, segment_no=1, output_ref=first,
            execution_fence=9, node_epoch=3)
        second = h.put_artifact("seg_1", 2, _sha("s1-again"))
        with pytest.raises(ServiceError) as exc:
            h.service.record_segment_uploaded(
                TENANT, job_id, segment_no=1, output_ref=second,
                execution_fence=9, node_epoch=3)
        assert exc.value.status == 409
        assert exc.value.code == "SEGMENT_TERMINAL"
        row = h.one("SELECT * FROM render_segment WHERE render_job_id=?"
                    " AND segment_no=1", (job_id,))
        assert row["output_artifact_ref"] == _jcs(first)
        assert row["attempts"] == 1

    def test_upload_accumulates_gpu_and_records_usage(self, h):
        job_id = h.create()["render_job_id"]
        for number in (1, 2):
            ref = h.put_artifact(f"seg_{number}", 1, _sha(f"s{number}"))
            h.service.record_segment_uploaded(
                TENANT, job_id, segment_no=number, output_ref=ref,
                consumed_gpu_ms=250, execution_fence=9, node_epoch=3)
        job = h.one("SELECT * FROM render_job WHERE render_job_id=?", (job_id,))
        assert job["consumed_gpu_ms"] == 500
        usage = h.rows("SELECT * FROM render_usage ORDER BY occurred_at")
        assert [u["quantity_ms"] for u in usage] == [250, 250]
        assert {u["resource_class"] for u in usage} == {"gpu_second"}
        assert all(u["reported_to_s23"] == 0 for u in usage)

    def test_third_attempt_fails_the_job_with_event(self, h):
        job_id = h.create()["render_job_id"]
        out = h.service.mark_segment_retry(
            TENANT, job_id, segment_no=1, attempts=3, consumed_gpu_ms=10)
        assert out["job_state"] == "FAILED"
        job = h.one("SELECT * FROM render_job WHERE render_job_id=?", (job_id,))
        assert job["state"] == "FAILED"
        assert job["failure_code"] == "SEGMENT_RENDER_FAILED"
        assert json.loads(job["failure_detail"])["attempts"] == 3
        events = h.outbox(schema.EVENT_RENDER_FAILED)
        assert len(events) == 1
        _validate_event("post.render-failed.v2.schema.json", events[0])
        assert events[0]["data"]["state"] == "FAILED"
        assert events[0]["data"]["failure_code"] == "SEGMENT_RENDER_FAILED"

    def test_first_two_attempts_keep_the_job_alive(self, h):
        job_id = h.create()["render_job_id"]
        h.service.mark_segment_retry(
            TENANT, job_id, segment_no=1, attempts=1, consumed_gpu_ms=10)
        out = h.service.mark_segment_retry(
            TENANT, job_id, segment_no=1, attempts=2, consumed_gpu_ms=10)
        assert out["job_state"] == "QUEUED"
        job = h.one("SELECT * FROM render_job WHERE render_job_id=?", (job_id,))
        assert job["state"] == "QUEUED"
        assert job["retry_count"] == 2
        assert h.outbox(schema.EVENT_RENDER_FAILED) == []

    def test_terminal_job_discards_late_artifacts(self, h):
        job_id = h.create()["render_job_id"]
        h.service.cancel_render_job(TENANT, job_id)
        h.service._fail_job(TENANT, job_id, "USER_CANCELLED",
                            state="CANCELLED")
        ref = h.put_artifact("seg_1", 1, _sha("s1"))
        with pytest.raises(ServiceError) as exc:
            h.service.record_segment_uploaded(
                TENANT, job_id, segment_no=1, output_ref=ref,
                execution_fence=9, node_epoch=3)
        assert exc.value.code == "JOB_TERMINAL"

    def test_identical_inputs_reuse_segments(self, h):
        h.render_to_candidate("cmd_1")
        second = h.create("cmd_2")
        segments = h.rows("SELECT * FROM render_segment WHERE render_job_id=?"
                          " ORDER BY segment_no", (second["render_job_id"],))
        assert [s["state"] for s in segments] == ["REUSED", "REUSED"]
        assert all(s["output_artifact_ref"] for s in segments)

    def test_affected_range_suppresses_reuse_only_where_it_overlaps(self, h):
        h.render_to_candidate("cmd_1")
        h.set_timeline(revision=11)
        h.affected_ranges = [{"start": 95000, "end": 140000}]
        second = h.create("cmd_2")
        segments = h.rows("SELECT * FROM render_segment WHERE render_job_id=?"
                          " ORDER BY segment_no", (second["render_job_id"],))
        assert [s["state"] for s in segments] == ["REUSED", "PENDING"]
        assert h.calls["diff"] == 1

    def test_unrecognised_affected_span_means_no_reuse(self, h):
        """区间形状不认识时保守处理：不复用，而不是"看起来不重叠"。"""
        h.render_to_candidate("cmd_1")
        h.set_timeline(revision=12)
        h.affected_ranges = [{"from_tick": 0, "to_tick": 10}]
        second = h.create("cmd_2")
        segments = h.rows("SELECT * FROM render_segment WHERE render_job_id=?"
                          " ORDER BY segment_no", (second["render_job_id"],))
        assert [s["state"] for s in segments] == ["PENDING", "PENDING"]

    def test_segment_key_ignores_tenant_but_cache_query_does_not(self, files):
        """`segment_key` 是内容键，不含租户；所以查询**必须**带租户谓词。

        这条测试就是那个谓词的守卫：同一个库、同一份时间线、两个租户，
        B 不能命中 A 的段产物。
        """
        alpha = files
        alpha.render_to_candidate("cmd_a")

        beta_conn = sqlite3.connect(alpha.conn.execute(
            "PRAGMA database_list").fetchone()["file"],
            check_same_thread=False, isolation_level=None)
        beta = Harness(conn=beta_conn)
        beta.put_artifact("vid_a", 1, VID_HASH, tenant=OTHER)
        job_id = beta.service.create_render_job(
            OTHER, "cmd_b", beta.body("cmd_b", tenant=OTHER),
            "hb")["render_job_id"]
        segments = beta.rows("SELECT * FROM render_segment WHERE"
                             " render_job_id=? ORDER BY segment_no", (job_id,))
        assert [s["state"] for s in segments] == ["PENDING", "PENDING"]
        assert all(s["output_artifact_ref"] is None for s in segments)

        # 段键确实相同（唯一差别是租户，而键里没有租户）。
        alpha_keys = {s["segment_key"] for s in alpha.rows(
            "SELECT segment_key FROM render_segment WHERE tenant_id=?",
            (TENANT,))}
        beta_keys = {s["segment_key"] for s in segments}
        assert alpha_keys == beta_keys


# ------------------------------------------------------------------ 收尾与响度门

class TestFinishRenderJob:
    def test_happy_path_writes_a_deliverable_candidate(self, h):
        candidate, job_id = h.render_to_candidate()
        assert candidate["loudness_gate"] == "PASSED"
        assert candidate["deliverable"] is True
        assert candidate["stale_inputs"] == []
        assert candidate["state"] == "CANDIDATE"
        assert candidate["manifest_content_hash"] == _sha(
            _jcs(candidate["manifest"]))
        assert candidate["manifest"]["timeline"] == {"timeline_id": "tl_1",
                                                     "revision": 10}
        assert candidate["manifest"]["duration_ticks"] == DURATION_TICKS
        assert candidate["manifest"]["frame_count"] == FRAME_COUNT

        job = h.one("SELECT * FROM render_job WHERE render_job_id=?", (job_id,))
        assert job["state"] == "SUCCEEDED"
        assert job["output_candidate_id"] == candidate["candidate_id"]

        master = h.outbox(schema.EVENT_MASTER_RENDERED)
        assert len(master) == 1
        _validate_event("post.master-rendered.v2.schema.json", master[0])
        assert master[0]["aggregate_id"] == candidate["candidate_id"]
        assert master[0]["data"]["deliverable"] is True
        assert master[0]["data"]["loudness"] == {
            "integrated_lufs": -16.0, "true_peak_dbtp": -1.2,
            "gate": "PASSED"}
        assert master[0]["data"]["stale_inputs"] == []

    def test_pending_segment_blocks_finish(self, h):
        job_id = h.create()["render_job_id"]
        h.put_artifact("aud_master", 1, AUD_HASH)
        h.put_artifact("vid_master", 1, VID_HASH)
        with pytest.raises(ServiceError) as exc:
            h.finish(job_id)
        assert exc.value.status == 409
        assert exc.value.code == "SEGMENTS_INCOMPLETE"
        assert exc.value.details["pending_segments"] == [1, 2]
        assert h.outbox() == []

    def test_segment_coverage_must_equal_timeline_duration(self, h):
        h.set_timeline(output_plan={"duration_ticks": DURATION_TICKS,
                                    "segments": [{"segment_no": 1,
                                                  "rec_in": 0,
                                                  "rec_out": 90000}]})
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        h.put_artifact("aud_master", 1, AUD_HASH)
        h.put_artifact("vid_master", 1, VID_HASH)
        with pytest.raises(ServiceError) as exc:
            h.finish(job_id)
        assert exc.value.status == 422
        assert exc.value.code == "DURATION_MISMATCH"
        assert exc.value.details == {"duration_ticks": DURATION_TICKS,
                                     "segment_coverage_ticks": 90000}
        job = h.one("SELECT * FROM render_job WHERE render_job_id=?", (job_id,))
        assert job["state"] == "FAILED"
        assert job["failure_code"] == "DURATION_MISMATCH"
        assert h.rows("SELECT * FROM master_candidate") == []
        events = h.outbox(schema.EVENT_RENDER_FAILED)
        assert len(events) == 1
        _validate_event("post.render-failed.v2.schema.json", events[0])

    def test_frame_count_mismatch_fails(self, h):
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        h.put_artifact("aud_master", 1, AUD_HASH)
        h.put_artifact("vid_master", 1, VID_HASH)
        with pytest.raises(ServiceError) as exc:
            h.finish(job_id, frame_count=61)
        assert exc.value.code == "DURATION_MISMATCH"
        assert exc.value.details == {"expected_frames": 60, "actual_frames": 61}
        assert h.one("SELECT state FROM render_job WHERE render_job_id=?",
                     (job_id,))["state"] == "FAILED"

    def test_frame_count_is_not_checked_without_fps(self, h):
        h.set_timeline(fps=None)
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        h.put_artifact("aud_master", 1, AUD_HASH)
        h.put_artifact("vid_master", 1, VID_HASH)
        candidate = h.finish(job_id, frame_count=999)
        assert candidate["loudness_gate"] == "PASSED"

    def test_output_not_confirmed_in_s28_is_422(self, h):
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        with pytest.raises(ServiceError) as exc:
            h.service.finish_render_job(
                TENANT, job_id,
                video_ref=_ref("vid_master", 1, VID_HASH),
                audio_ref=_ref("aud_master", 1, AUD_HASH),
                measurement=h.measurement(), frame_count=FRAME_COUNT)
        assert exc.value.status == 422
        assert exc.value.code == "INPUT_ARTIFACT_MISSING"
        assert h.one("SELECT state FROM render_job WHERE render_job_id=?",
                     (job_id,))["state"] == "QUEUED"
        assert h.rows("SELECT * FROM master_candidate") == []

    def test_loudness_out_of_tolerance_fails_the_gate(self, h):
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        candidate = h.finish(job_id, measurement=h.measurement(
            integrated=-12.0, peak=-1.2))
        assert candidate["loudness_gate"] == "FAILED"
        assert candidate["deliverable"] is False
        event = h.outbox(schema.EVENT_MASTER_RENDERED)[0]
        _validate_event("post.master-rendered.v2.schema.json", event)
        assert event["data"]["loudness"]["gate"] == "FAILED"
        assert event["data"]["deliverable"] is False

    def test_true_peak_over_ceiling_fails_but_lufs_in_range(self, h):
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        candidate = h.finish(job_id, measurement=h.measurement(
            integrated=-16.0, peak=-0.2))
        assert candidate["loudness_gate"] == "FAILED"
        assert candidate["deliverable"] is False

    def test_no_measurement_is_not_run_never_passed(self, h):
        """§4.5：完全没测 → `NOT_RUN`，绝不是 `PASSED`。"""
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        candidate = h.finish(job_id, measurement=None)
        assert candidate["loudness_gate"] == "NOT_RUN"
        assert candidate["deliverable"] is False
        # 没有测量行（NOT_RUN 没有 conforms 值可存）。
        assert h.rows("SELECT * FROM loudness_measurement") == []
        event = h.outbox(schema.EVENT_MASTER_RENDERED)[0]
        _validate_event("post.master-rendered.v2.schema.json", event)
        assert event["data"]["loudness"]["gate"] == "NOT_RUN"
        assert event["data"]["loudness"]["integrated_lufs"] is None

    def test_measurement_failure_fails_the_gate_and_marks_stale(self, h):
        """测不出 = 门失败。它进 `stale_inputs`，所以也是不可交付。"""
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        candidate = h.finish(job_id, measurement=None, measurement_failed=True)
        assert candidate["loudness_gate"] == "FAILED"
        assert candidate["deliverable"] is False
        assert candidate["stale_inputs"] == [{"role": "loudness",
                                              "reason": "MEASUREMENT_FAILED"}]

    def test_non_finite_measurement_is_a_failure_not_a_pass(self, h):
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        candidate = h.finish(job_id, measurement=h.measurement(
            integrated=float("nan"), peak=-1.2))
        assert candidate["loudness_gate"] == "FAILED"
        assert candidate["deliverable"] is False

    def test_spec_without_tolerance_is_refused_not_invented(self, h):
        h.specs[(TENANT, "spec_1", 1)] = {
            **DEFAULT_SPEC, "loudness": {"target_lufs": -16.0}}
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        h.put_artifact("aud_master", 1, AUD_HASH)
        h.put_artifact("vid_master", 1, VID_HASH)
        with pytest.raises(ServiceError) as exc:
            h.finish(job_id)
        assert exc.value.code == "MASTER_SPEC_REQUIRED"

    def test_changed_subtitle_makes_candidate_stale_and_undeliverable(self, h):
        h.put_subtitle(content_hash=SUB_HASH)
        sub_ref = _ref("sub_1", 1, SUB_HASH, "S38")
        job_id = h.create(subtitle_ref=sub_ref)["render_job_id"]
        h.upload_all(job_id)
        # 渲染之后字幕修了一版。
        h.subtitles[(TENANT, "sub_1", 1)] = SUB_HASH_2
        candidate = h.finish(job_id)
        assert candidate["loudness_gate"] == "PASSED"
        assert candidate["deliverable"] is False
        assert candidate["stale_inputs"] == [{
            "role": "subtitle", "artifact_ref": sub_ref,
            "reason": "SUBTITLE_CHANGED",
            "current_content_hash": SUB_HASH_2}]
        event = h.outbox(schema.EVENT_MASTER_RENDERED)[0]
        _validate_event("post.master-rendered.v2.schema.json", event)
        assert event["data"]["deliverable"] is False

    def test_missing_subtitle_after_render_is_stale(self, h):
        h.put_subtitle(content_hash=SUB_HASH)
        sub_ref = _ref("sub_1", 1, SUB_HASH, "S38")
        job_id = h.create(subtitle_ref=sub_ref)["render_job_id"]
        h.upload_all(job_id)
        del h.subtitles[(TENANT, "sub_1", 1)]
        candidate = h.finish(job_id)
        assert candidate["stale_inputs"][0]["reason"] == "SUBTITLE_MISSING"
        assert candidate["deliverable"] is False

    def test_expensive_manifest_recomputes_the_stored_hash(self, h):
        """C-S42-03：审计要能从 manifest 独立复算 `manifest_content_hash`。"""
        candidate, _ = h.render_to_candidate()
        assert candidate["manifest_content_hash"] == _sha(
            _jcs(candidate["manifest"]))
        evidence = h.service.candidate_evidence(TENANT,
                                                candidate["candidate_id"])
        assert evidence["manifest_content_hash"] == _sha(
            _jcs(evidence["manifest"]))

    def test_terminal_job_cannot_be_finished_twice(self, h):
        candidate, job_id = h.render_to_candidate()
        h.put_artifact("aud_master", 1, AUD_HASH)
        h.put_artifact("vid_master", 1, VID_HASH)
        with pytest.raises(ServiceError) as exc:
            h.finish(job_id)
        assert exc.value.code == "JOB_TERMINAL"


class TestLoudnessGateUnit:
    """门本身是可单测的纯函数，四条规则一条一条来。"""

    def test_conforms_within_tolerance(self):
        spec = DEFAULT_SPEC
        assert loudness_gate({"integrated_lufs": -16.4, "true_peak_dbtp": -1.5},
                             spec) == ("PASSED", True)

    def test_outside_tolerance(self):
        spec = DEFAULT_SPEC
        assert loudness_gate({"integrated_lufs": -17.1, "true_peak_dbtp": -1.5},
                             spec) == ("FAILED", False)

    def test_measurement_failed_beats_a_good_number(self):
        spec = DEFAULT_SPEC
        assert loudness_gate({"integrated_lufs": -16.0, "true_peak_dbtp": -1.2},
                             spec, measurement_failed=True) == ("FAILED", False)

    def test_no_measurement_is_not_run(self):
        assert loudness_gate(None, DEFAULT_SPEC) == ("NOT_RUN", None)

    def test_null_values_are_a_failure(self):
        assert loudness_gate({"integrated_lufs": None,
                              "true_peak_dbtp": -1.2},
                             DEFAULT_SPEC) == ("FAILED", False)

    def test_ceiling_is_not_the_target(self):
        """`true_peak_ceiling` 是上限，`target_lufs` 是响度目标，两个量。"""
        spec = {**DEFAULT_SPEC,
                "loudness": {"target_lufs": -16.0, "tolerance_lu": 1.0,
                             "true_peak_ceiling_dbtp": -3.0}}
        assert loudness_gate({"integrated_lufs": -16.0,
                              "true_peak_dbtp": -2.5}, spec) == ("FAILED", False)


class TestCandidates:
    def test_candidate_view_and_artifacts(self, h):
        candidate, _job = h.render_to_candidate()
        view = h.service.get_candidate(TENANT, candidate["candidate_id"])
        assert {a["role"] for a in view["artifacts"]} == {"master_video",
                                                         "master_audio"}
        ordinals = {a["role"]: a["ordinal"] for a in view["artifacts"]}
        assert ordinals == {"master_video": 0, "master_audio": 1}
        assert view["state_is_projection"] is False
        assert view["artifact_roles"] == ["master_audio", "master_video"]

    def test_small_artifacts_go_into_their_own_roles(self, h):
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        thumb = h.put_artifact("thumb", 1, "5" * 64)
        stem = h.put_artifact("stem_d", 1, "6" * 64)
        candidate = h.finish(job_id, small={"thumbnail": thumb,
                                            "stem_dialogue": stem})
        artifacts = h.rows("SELECT role, ordinal FROM master_candidate_artifact"
                           " WHERE candidate_id=? ORDER BY role",
                           (candidate["candidate_id"],))
        assert {a["role"]: a["ordinal"] for a in artifacts} == {
            "master_video": 0, "master_audio": 1, "stem_dialogue": 0,
            "thumbnail": 0}
        assert {s["role"] for s in candidate["manifest"]["stems"]} == {
            "stem_dialogue"}

    def test_list_filters_by_deliverable(self, h):
        good, _ = h.render_to_candidate("cmd_a")
        h.specs[(TENANT, "spec_1", 1)] = DEFAULT_SPEC
        job_id = h.create("cmd_b")["render_job_id"]
        h.upload_all(job_id)
        bad = h.finish(job_id, measurement=h.measurement(integrated=-10.0))
        every = h.service.list_candidates(TENANT)
        assert {c["candidate_id"] for c in every["items"]} == {
            good["candidate_id"], bad["candidate_id"]}
        only_good = h.service.list_candidates(TENANT, deliverable=True)
        assert [c["candidate_id"] for c in only_good["items"]] == [
            good["candidate_id"]]

    def test_evidence_answers_how_to_reproduce(self, h):
        candidate, _job = h.render_to_candidate()
        evidence = h.service.candidate_evidence(TENANT,
                                                candidate["candidate_id"])
        assert evidence["graph_version"] == schema.GRAPH_VERSION
        assert evidence["graph_hash"] == candidate["graph_hash"]
        assert compute_graph_hash(evidence["graph"]) == evidence["graph_hash"]
        assert evidence["render_profile_hash"]
        assert evidence["render_env_hash"] == candidate["render_env_hash"]
        assert evidence["mix_plan_hash"] == MIX_HASH
        assert evidence["timeline_content_hash"] == TIMELINE_HASH
        assert evidence["execution_fence"] == 9
        assert evidence["loudness"]["conforms_to_spec"] is True
        assert evidence["segments"][0]["segment_key"]
        # 证据是"当时是什么样"，不是现查 S28 —— 删掉输入也不影响它。
        h.artifacts.clear()
        again = h.service.candidate_evidence(TENANT, candidate["candidate_id"])
        assert again["manifest_content_hash"] == evidence["manifest_content_hash"]


# ------------------------------------------------------------------ remix

class TestMixJobs:
    def _request(self, h, candidate, command_id="cmd_mix", **over):
        stems = over.pop("stems", None)
        if stems is None:
            stems = [
                {"role": "dialogue",
                 "artifact_ref": h.put_artifact("stem_d", 1, STEM_HASH)},
                {"role": "bgm",
                 "artifact_ref": h.put_artifact("stem_b", 1, STEM_HASH_2)},
            ]
        body = {
            "command_id": command_id,
            "request_hash": "8" * 64,
            "context": _context(command_id=command_id),
            "source_video_ref": candidate["video_artifact_ref"],
            "stems": stems,
            "mix_plan_hash": "b" * 64,
            "target_lufs": -16.0,
            "true_peak_ceiling": -1.0,
            "reason": "remix_defect",
            "repair_plan_ref": _ref("rp_1", 1, "c" * 64, "S39"),
        }
        body.update(over)
        return h.service.create_mix_job(TENANT, command_id, body,
                                        "h_" + command_id)

    def test_happy_path_reuses_the_video_stream_verbatim(self, h):
        basis, _job = h.render_to_candidate()
        created = self._request(h, basis)
        assert created["state"] == "CREATED"
        assert created["basis_candidate_id"] == basis["candidate_id"]
        assert created["reason"] == "remix_defect"

        mix_job_id = created["mix_job_id"]
        audio = h.put_artifact("aud_mixed", 1, AUD_HASH)
        measured = h.service.finish_mix_job(TENANT, mix_job_id,
                                            output_audio_ref=audio,
                                            measurement=h.measurement(),
                                            consumed_cpu_ms=800)
        assert measured["source_video_preserved"] is True
        # E24-S42-01：逐字段相同，不是"碰巧一样"。
        assert measured["video_artifact_ref"] == basis["video_artifact_ref"]
        assert measured["audio_artifact_ref"] == audio
        assert measured["manifest"]["video_artifact_ref"] == \
            basis["video_artifact_ref"]
        assert measured["manifest"]["remix"]["video_stream_reused"] is True
        assert measured["manifest"]["remix"]["basis_candidate_id"] == \
            basis["candidate_id"]
        assert measured["render_env_hash"] == basis["render_env_hash"]
        assert measured["duration_ticks"] == basis["duration_ticks"]
        assert measured["frame_count"] == basis["frame_count"]
        assert measured["loudness_gate"] == "PASSED"
        assert measured["deliverable"] is True

        job = h.one("SELECT * FROM mix_job WHERE mix_job_id=?", (mix_job_id,))
        assert job["state"] == "SUCCEEDED"
        assert job["candidate_id"] == measured["candidate_id"]
        assert job["consumed_cpu_ms"] == 800

        events = h.outbox(schema.EVENT_MIX_COMPLETED)
        assert len(events) == 1
        _validate_event("post.mix-completed.v2.schema.json", events[0])
        assert events[0]["aggregate_id"] == mix_job_id
        assert events[0]["data"]["source_video_ref"] == \
            basis["video_artifact_ref"]
        assert events[0]["data"]["reason"] == "remix_defect"
        assert events[0]["data"]["repair_plan_ref"]["service_id"] == "S39"
        assert events[0]["data"]["state"] == "SUCCEEDED"

    def test_remix_candidate_is_a_new_candidate_not_an_overwrite(self, h):
        basis, _job = h.render_to_candidate()
        created = self._request(h, basis)
        audio = h.put_artifact("aud_mixed", 1, AUD_HASH)
        remixed = h.service.finish_mix_job(TENANT, created["mix_job_id"],
                                           output_audio_ref=audio,
                                           measurement=h.measurement())
        assert remixed["candidate_id"] != basis["candidate_id"]
        originals = h.one("SELECT * FROM master_candidate WHERE candidate_id=?",
                          (basis["candidate_id"],))
        assert originals["state"] == "CANDIDATE"
        assert originals["manifest_content_hash"] == \
            basis["manifest_content_hash"]
        assert remixed["mix_job_id"] == created["mix_job_id"]
        assert remixed["render_job_id"] is None

    def test_missing_stem_is_stems_unavailable_not_a_silent_separation(self, h):
        basis, _job = h.render_to_candidate()
        permits_before = len(h.permit_requests)
        with pytest.raises(ServiceError) as exc:
            self._request(h, basis, stems=[
                {"role": "dialogue",
                 "artifact_ref": _ref("stem_d", 1, STEM_HASH)},
                {"role": "bgm",
                 "artifact_ref": _ref("stem_b", 1, STEM_HASH_2)}])
        assert exc.value.status == 422
        assert exc.value.code == "STEMS_UNAVAILABLE"
        assert [m["ref"]["id"] for m in exc.value.details["missing"]] == [
            "stem_d", "stem_b"]
        assert h.rows("SELECT * FROM mix_job") == []
        # 在向 S23 要许可之前就停了：为一件做不成的事预留预算没有意义。
        assert len(h.permit_requests) == permits_before

    def test_tombstoned_stem_is_unavailable(self, h):
        basis, _job = h.render_to_candidate()
        with pytest.raises(ServiceError) as exc:
            self._request(h, basis, stems=[
                {"role": "dialogue",
                 "artifact_ref": h.put_artifact("stem_d", 1, STEM_HASH,
                                                tombstoned=True)}])
        assert exc.value.code == "STEMS_UNAVAILABLE"

    def test_video_without_a_basis_candidate_has_no_invented_timeline(self, h):
        basis, _job = h.render_to_candidate()
        stranger = h.put_artifact("vid_other", 1, "e" * 64)
        with pytest.raises(ServiceError) as exc:
            self._request(h, basis, source_video_ref=stranger)
        assert exc.value.status == 422
        assert exc.value.code == "REMASTER_BASIS_MISSING"
        assert exc.value.details["source_video_ref"]["id"] == "vid_other"
        assert h.rows("SELECT * FROM mix_job") == []

    def test_duplicate_stem_role_is_rejected(self, h):
        basis, _job = h.render_to_candidate()
        one = h.put_artifact("stem_d", 1, STEM_HASH)
        with pytest.raises(ServiceError) as exc:
            self._request(h, basis, stems=[
                {"role": "dialogue", "artifact_ref": one},
                {"role": "dialogue", "artifact_ref": one}])
        assert exc.value.code == "SCHEMA_INVALID"
        assert "duplicate" in str(exc.value)

    def test_unknown_stem_role_is_rejected_from_closed_set(self, h):
        basis, _job = h.render_to_candidate()
        with pytest.raises(ServiceError) as exc:
            self._request(h, basis, stems=[
                {"role": "music", "artifact_ref": h.put_artifact(
                    "stem_d", 1, STEM_HASH)}])
        assert exc.value.details["allowed"] == ["dialogue", "sfx", "bgm",
                                                "voice"]

    def test_remix_defect_without_a_repair_plan_is_refused(self, h):
        basis, _job = h.render_to_candidate()
        with pytest.raises(ServiceError) as exc:
            self._request(h, basis, repair_plan_ref=None)
        assert exc.value.status == 422
        assert exc.value.code == "SCHEMA_INVALID"
        assert "repair_plan_ref" in str(exc.value)

    def test_repair_plan_from_the_wrong_service_is_refused(self, h):
        basis, _job = h.render_to_candidate()
        with pytest.raises(ServiceError) as exc:
            self._request(h, basis,
                          repair_plan_ref=_ref("rp_1", 1, "c" * 64, "S08"))
        assert exc.value.code == "SCHEMA_INVALID"

    def test_other_remix_reasons_do_not_need_a_repair_plan(self, h):
        basis, _job = h.render_to_candidate()
        created = self._request(h, basis, reason="spec_change",
                                repair_plan_ref=None)
        assert created["reason"] == "spec_change"

    def test_mix_target_is_required(self, h):
        basis, _job = h.render_to_candidate()
        with pytest.raises(ServiceError) as exc:
            self._request(h, basis, true_peak_ceiling=None)
        assert exc.value.code == "MIX_TARGET_REQUIRED"

    def test_no_audio_codec_means_no_encode_node(self, h):
        basis, _job = h.render_to_candidate()
        created = self._request(h, basis)
        graph = h.one("SELECT graph FROM filter_graph WHERE tenant_id=?"
                      " AND graph_hash=?", (TENANT, h.one(
                          "SELECT graph_hash FROM mix_job WHERE mix_job_id=?",
                          (created["mix_job_id"],))["graph_hash"]))
        kinds = [n["kind"] for n in json.loads(graph["graph"])["nodes"]]
        assert "encode" not in kinds
        assert kinds.count("loudnorm") == 2

    def test_explicit_codec_adds_one_encode_node(self, h):
        basis, _job = h.render_to_candidate()
        created = self._request(h, basis, audio_codec="pcm_s24le")
        stored = h.one("SELECT graph_hash FROM mix_job WHERE mix_job_id=?",
                       (created["mix_job_id"],))
        graph = json.loads(h.one(
            "SELECT graph FROM filter_graph WHERE tenant_id=? AND graph_hash=?",
            (TENANT, stored["graph_hash"]))["graph"])
        encode = [n for n in graph["nodes"] if n["kind"] == "encode"]
        assert [n["codec"] for n in encode] == ["pcm_s24le"]

    def test_mix_message_goes_through_the_permit_without_gpu(self, h):
        basis, _job = h.render_to_candidate()
        before = len(h.permit_requests)
        self._request(h, basis)
        request = h.permit_requests[before]
        assert request["resource_class"] == "cpu_media_second"
        assert request["estimated_gpu_ms"] == 0
        assert request["requested_ms"] == 80   # 2 s × 40 ms/s

    def test_denied_permit_creates_nothing(self, h):
        basis, _job = h.render_to_candidate()
        h.permit_state = "DENIED"
        with pytest.raises(ServiceError) as exc:
            self._request(h, basis)
        assert exc.value.code == "BUDGET_DENIED"
        assert h.rows("SELECT * FROM mix_job") == []

    def test_cancel_moves_to_cancelled_and_is_not_repeatable(self, h):
        basis, _job = h.render_to_candidate()
        created = self._request(h, basis)
        cancelled = h.service.cancel_mix_job(TENANT, created["mix_job_id"])
        assert cancelled["state"] == "CANCELLED"
        with pytest.raises(ServiceError) as exc:
            h.service.cancel_mix_job(TENANT, created["mix_job_id"])
        assert exc.value.code == "JOB_TERMINAL"

    def test_cancelled_mix_job_cannot_finish(self, h):
        basis, _job = h.render_to_candidate()
        created = self._request(h, basis)
        h.service.cancel_mix_job(TENANT, created["mix_job_id"])
        audio = h.put_artifact("aud_mixed", 1, AUD_HASH)
        with pytest.raises(ServiceError) as exc:
            h.service.finish_mix_job(TENANT, created["mix_job_id"],
                                     output_audio_ref=audio,
                                     measurement=h.measurement())
        assert exc.value.code == "JOB_TERMINAL"

    def test_mix_usage_is_reported_once_at_finish(self, h):
        basis, _job = h.render_to_candidate()
        created = self._request(h, basis)
        assert h.rows("SELECT * FROM render_usage WHERE mix_job_id IS NULL"
                      " AND render_job_id IS NULL") == []
        audio = h.put_artifact("aud_mixed", 1, AUD_HASH)
        h.service.finish_mix_job(TENANT, created["mix_job_id"],
                                 output_audio_ref=audio,
                                 measurement=h.measurement(),
                                 consumed_cpu_ms=80)
        usage = h.rows("SELECT * FROM render_usage WHERE mix_job_id=?",
                       (created["mix_job_id"],))
        assert [(u["resource_class"], u["quantity_ms"]) for u in usage] == [
            ("cpu_media_second", 80)]
        assert usage[0]["reported_to_s23"] == 0

    def test_unconfirmed_mixed_audio_is_422(self, h):
        basis, _job = h.render_to_candidate()
        created = self._request(h, basis)
        with pytest.raises(ServiceError) as exc:
            h.service.finish_mix_job(
                TENANT, created["mix_job_id"],
                output_audio_ref=_ref("aud_mixed", 1, AUD_HASH),
                measurement=h.measurement())
        assert exc.value.code == "INPUT_ARTIFACT_MISSING"
        assert h.one("SELECT state FROM mix_job WHERE mix_job_id=?",
                     (created["mix_job_id"],))["state"] == "CREATED"

    def test_mix_without_a_measurement_is_not_deliverable(self, h):
        basis, _job = h.render_to_candidate()
        created = self._request(h, basis)
        audio = h.put_artifact("aud_mixed", 1, AUD_HASH)
        result = h.service.finish_mix_job(TENANT, created["mix_job_id"],
                                          output_audio_ref=audio,
                                          measurement=None)
        assert result["loudness_gate"] == "NOT_RUN"
        assert result["deliverable"] is False

    def test_mix_is_tenant_scoped(self, h):
        basis, _job = h.render_to_candidate()
        created = self._request(h, basis)
        audio = h.put_artifact("aud_mixed", 1, AUD_HASH, tenant=OTHER)
        for call in (
            lambda: h.service.cancel_mix_job(OTHER, created["mix_job_id"]),
            lambda: h.service.finish_mix_job(
                OTHER, created["mix_job_id"], output_audio_ref=audio,
                measurement=h.measurement()),
        ):
            with pytest.raises(ServiceError) as exc:
                call()
            assert exc.value.status == 404
            assert exc.value.code == "NOT_FOUND"
        assert h.one("SELECT state FROM mix_job WHERE mix_job_id=?",
                     (created["mix_job_id"],))["state"] == "CREATED"


# ------------------------------------------------------------------ 取消 / 恢复 / 重启

class TestCancelAndRecovery:
    def test_cancel_goes_to_cancelling_not_stopped(self, h):
        """E22：取消只是「请求停止」，不是「已经停止」，也不退款。"""
        job_id = h.create()["render_job_id"]
        result = h.service.cancel_render_job(TENANT, job_id)
        assert result["state"] == "CANCELLING"
        assert result["consumed_gpu_ms"] == 0

    def test_cancel_is_terminal_only_after_the_job_is(self, h):
        job_id = h.create()["render_job_id"]
        h.service.cancel_render_job(TENANT, job_id)
        # CANCELLING 上 worker 不能再认领。
        with pytest.raises(ServiceError) as exc:
            h.service.claim_render_job(TENANT, job_id, "w_1")
        assert exc.value.code == "JOB_TERMINAL"

    def test_cancel_of_a_succeeded_job_is_refused(self, h):
        _candidate, job_id = h.render_to_candidate()
        with pytest.raises(ServiceError) as exc:
            h.service.cancel_render_job(TENANT, job_id)
        assert exc.value.code == "JOB_TERMINAL"
        assert exc.value.details == {"state": "SUCCEEDED"}

    def test_claim_sets_a_lease_and_moves_to_running(self, h):
        job_id = h.create()["render_job_id"]
        claimed = h.service.claim_render_job(TENANT, job_id, "w_1")
        assert claimed["state"] == "RUNNING"
        assert claimed["worker_id"] == "w_1"
        assert claimed["lease_expires_at"] is not None
        with pytest.raises(ServiceError) as exc:
            h.service.claim_render_job(TENANT, job_id, "w_2")
        assert exc.value.code == "CLAIM_RACE"
        assert exc.value.retryable is True

    def test_expired_lease_is_orphaned_without_guessing_artifacts(self, h):
        job_id = h.create()["render_job_id"]
        h.service.claim_render_job(TENANT, job_id, "w_1")
        h.conn.execute("UPDATE render_job SET lease_expires_at=? WHERE"
                       " tenant_id=? AND render_job_id=?", (PAST, TENANT, job_id))
        result = h.service.recover_expired_leases(TENANT)
        assert result["orphaned"] == [job_id]
        job = h.one("SELECT * FROM render_job WHERE render_job_id=?", (job_id,))
        assert job["state"] == "ORPHANED"
        assert job["worker_id"] is None
        assert job["lease_expires_at"] is None
        assert h.rows("SELECT * FROM master_candidate") == []

    def test_live_lease_is_not_orphaned(self, h):
        job_id = h.create()["render_job_id"]
        h.service.claim_render_job(TENANT, job_id, "w_1")
        assert h.service.recover_expired_leases(TENANT)["orphaned"] == []

    def test_reconcile_with_no_confirmed_segment_requeues(self, h):
        job_id = self._orphan(h)
        result = h.service.reconcile_render_job(TENANT, job_id)
        assert result["state"] == "QUEUED"
        assert result["confirmed_segments"] == []

    def test_reconcile_with_all_segments_confirmed_resumes(self, h):
        job_id = self._orphan(h)
        h.upload_all(job_id)
        result = h.service.reconcile_render_job(TENANT, job_id)
        assert result["state"] == "RUNNING"
        assert result["confirmed_segments"] == [1, 2]
        assert result["remaining_segments"] == []

    def test_reconcile_notices_an_artifact_lost_after_upload(self, h):
        """C14：已记录的段 ref 逐条回 S28 复核，不信本地那一行。"""
        job_id = self._orphan(h)
        h.upload_all(job_id)
        del h.artifacts[(TENANT, "seg_1", 1)]
        result = h.service.reconcile_render_job(TENANT, job_id)
        assert result["lost_segments"] == [1]
        assert result["confirmed_segments"] == [2]
        # 有确认的段 → RUNNING 续跑；但丢的那段仍是 DONE，需要调用方重排。
        assert result["state"] == "RUNNING"

    def test_reconcile_applies_to_resumable_states_only(self, h):
        # QUEUED 也在 RESUMABLE_JOB_STATES 里：对账是幂等的，重排一次无妨。
        job_id = h.create()["render_job_id"]
        assert h.service.reconcile_render_job(TENANT, job_id)["state"] == \
            "QUEUED"
        # SUCCEEDED 不是可恢复态。
        _candidate, done_job = h.render_to_candidate("cmd_b")
        with pytest.raises(ServiceError) as exc:
            h.service.reconcile_render_job(TENANT, done_job)
        assert exc.value.code == "JOB_NOT_RESUMABLE"
        assert exc.value.details == {"state": "SUCCEEDED"}

    def test_failed_job_can_be_retried_back_to_queued(self, h):
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        for attempt in (1, 2, 3):
            h.service.mark_segment_retry(TENANT, job_id, segment_no=2,
                                         attempts=attempt, consumed_gpu_ms=0)
        job = h.one("SELECT state FROM render_job WHERE render_job_id=?",
                    (job_id,))
        assert job["state"] == "FAILED"
        retried = h.service.retry_render_job(TENANT, job_id)
        assert retried["state"] == "QUEUED"
        assert retried["failure_code"] is None

    def test_a_queued_job_cannot_be_retried(self, h):
        job_id = h.create()["render_job_id"]
        with pytest.raises(ServiceError) as exc:
            h.service.retry_render_job(TENANT, job_id)
        assert exc.value.code == "JOB_NOT_RETRYABLE"
        assert exc.value.details == {"state": "QUEUED"}

    def test_state_survives_a_restart(self, files):
        """重启恢复：job/段/outbox 都在文件库里，新进程照常收尾。"""
        job_id = files.create()["render_job_id"]
        files.upload_all(job_id)
        path = files.conn.execute("PRAGMA database_list").fetchone()[2]
        restarted = _new_service(path)
        # 重启后上游 stub 是空的；媒体事实本来就不在库里。
        restarted.artifacts = dict(files.artifacts)
        restarted.subtitles = dict(files.subtitles)
        job = restarted.service.get_render_job(TENANT, job_id)
        assert job["state"] == "QUEUED"
        assert [s["state"] for s in job["segments"]] == ["DONE", "DONE"]
        candidate = restarted.finish(job_id)
        assert candidate["loudness_gate"] == "PASSED"
        assert len(restarted.outbox(schema.EVENT_MASTER_RENDERED)) == 1

    def test_orphan_recovery_survives_a_restart(self, files):
        job_id = files.create()["render_job_id"]
        files.service.claim_render_job(TENANT, job_id, "w_1")
        files.conn.execute("UPDATE render_job SET lease_expires_at=? WHERE"
                           " render_job_id=?", (PAST, job_id))
        path = files.conn.execute("PRAGMA database_list").fetchone()[2]
        restarted = _new_service(path)
        assert restarted.service.recover_expired_leases(TENANT)["orphaned"] \
            == [job_id]
        assert restarted.service.reconcile_render_job(TENANT, job_id)[
            "state"] == "QUEUED"

    def _orphan(self, h):
        job_id = h.create()["render_job_id"]
        h.service.claim_render_job(TENANT, job_id, "w_1")
        h.conn.execute("UPDATE render_job SET lease_expires_at=?,"
                       " state='ORPHANED' WHERE tenant_id=? AND"
                       " render_job_id=?", (PAST, TENANT, job_id))
        return job_id


# ------------------------------------------------------------------ 事件消费

class TestEventConsumption:
    def _timeline_event(self, sequence, event_id="ev_1", revision=10,
                        ranges=None):
        return {
            "id": event_id, "type": schema.EVENT_TIMELINE,
            "tenant_id": TENANT, "aggregate_id": "tl_1",
            "sequence": sequence,
            "data": {"timeline_id": "tl_1", "revision": revision,
                     "content_hash": TIMELINE_HASH,
                     "affected_ranges": ranges or []},
        }

    def test_a_duplicate_event_is_not_projected_twice(self, h):
        event = self._timeline_event(1)
        first = h.service.consume_timeline_revision(TENANT, event)
        second = h.service.consume_timeline_revision(TENANT, event)
        assert first["outcome"] == "applied" and first["accepted"] is True
        assert second["outcome"] == "duplicate" and second["accepted"] is False

    def test_a_late_older_event_is_stale_not_an_error(self, h):
        h.service.consume_timeline_revision(TENANT, self._timeline_event(
            2, "ev_2", revision=11, ranges=[{"start": 0, "end": 90000}]))
        late = h.service.consume_timeline_revision(TENANT, self._timeline_event(
            1, "ev_1", revision=10))
        assert late["outcome"] == "stale"
        # 水位没被拉回去：投影还是后到的那一版。
        row = h.one("SELECT * FROM timeline_signal WHERE timeline_id='tl_1'")
        assert row["revision"] == 11
        assert json.loads(row["affected_ranges"]) == [{"start": 0,
                                                       "end": 90000}]

    def test_watermarks_are_per_aggregate_not_global(self, h):
        h.service.consume_timeline_revision(TENANT, {
            **self._timeline_event(5, "ev_a"), "aggregate_id": "tl_a"})
        other = h.service.consume_timeline_revision(TENANT, {
            **self._timeline_event(1, "ev_b"), "aggregate_id": "tl_b",
            "data": {"timeline_id": "tl_b", "revision": 1,
                     "content_hash": TIMELINE_HASH}})
        assert other["outcome"] == "applied"

    def test_snapshot_projection_accepts_the_matching_candidate(self, h):
        candidate, _job = h.render_to_candidate()
        result = h.service.consume_snapshot_committed(TENANT, {
            "id": "ev_s1", "type": schema.EVENT_SNAPSHOT, "tenant_id": TENANT,
            "aggregate_id": "snap_1", "sequence": 1,
            "data": {"accepted_revision": 4, "execution_fence": 9,
                     "accepted_pointers_delta": [
                         candidate["video_artifact_ref"]]}})
        assert result["projected"] == [candidate["candidate_id"]]
        view = h.service.get_candidate(TENANT, candidate["candidate_id"])
        assert view["state"] == "ACCEPTED_BY_SNAPSHOT"
        assert view["state_is_projection"] is True

    def test_snapshot_supersedes_the_previous_candidate_in_the_branch(self, h):
        old, _job = h.render_to_candidate("cmd_a")
        h.specs[(TENANT, "spec_1", 1)] = {**DEFAULT_SPEC, "video_codec": "hevc"}
        job_id = h.create("cmd_b")["render_job_id"]
        h.upload_all(job_id)
        new = h.finish(job_id, audio_hash="9" * 64)
        assert new["candidate_id"] != old["candidate_id"]

        h.service.consume_snapshot_committed(TENANT, {
            "id": "ev_s1", "type": schema.EVENT_SNAPSHOT, "tenant_id": TENANT,
            "aggregate_id": "snap_1", "sequence": 1,
            "data": {"accepted_revision": 5, "execution_fence": 9,
                     "accepted_pointers_delta": [
                         new["video_artifact_ref"]]}})
        assert h.service.get_candidate(TENANT, new["candidate_id"])[
            "state"] == "ACCEPTED_BY_SNAPSHOT"
        assert h.service.get_candidate(TENANT, old["candidate_id"])[
            "state"] == "SUPERSEDED"

    def test_an_older_snapshot_fence_cannot_move_a_newer_candidate(self, h):
        candidate, _job = h.render_to_candidate()
        result = h.service.consume_snapshot_committed(TENANT, {
            "id": "ev_s1", "type": schema.EVENT_SNAPSHOT, "tenant_id": TENANT,
            "aggregate_id": "snap_1", "sequence": 1,
            "data": {"accepted_revision": 3, "execution_fence": 1,
                     "accepted_pointers_delta": [
                         candidate["video_artifact_ref"]]}})
        assert result["projected"] == []
        assert result["unmatched"] == [
            {**candidate["video_artifact_ref"],
             "reason": "STALE_SNAPSHOT_FENCE"}]
        assert h.service.get_candidate(TENANT, candidate["candidate_id"])[
            "state"] == "CANDIDATE"

    def test_a_non_s28_delta_entry_is_not_guessed(self, h):
        candidate, _job = h.render_to_candidate()
        loose = {**candidate["video_artifact_ref"], "extra": 1}
        result = h.service.consume_snapshot_committed(TENANT, {
            "id": "ev_s1", "type": schema.EVENT_SNAPSHOT, "tenant_id": TENANT,
            "aggregate_id": "snap_1", "sequence": 1,
            "data": {"accepted_revision": 3, "execution_fence": 9,
                     "accepted_pointers_delta": [loose]}})
        assert result["projected"] == []
        assert result["unmatched"] == [loose]

    def test_repair_plan_is_mirrored_not_executed(self, h):
        result = h.service.consume_repair_planned(TENANT, {
            "id": "ev_r1", "type": schema.EVENT_REPAIR_PLANNED,
            "tenant_id": TENANT, "aggregate_id": "rp_1", "sequence": 1,
            "data": {"plan_id": "rp_1", "state": "PLANNED",
                     "top_action": "regenerate_video", "episode_id": "ep_1"}})
        assert result["outcome"] == "applied"
        assert result["top_action"] == "regenerate_video"
        row = h.one("SELECT * FROM repair_signal WHERE plan_id='rp_1'")
        assert row["top_action"] == "regenerate_video"
        # 只镜像：没有 job、没有事件。
        assert h.rows("SELECT * FROM render_job") == []
        assert h.outbox() == []

    def test_artifact_ingested_only_touches_the_inbox(self, h):
        result = h.service.consume_artifact_ingested(TENANT, {
            "id": "ev_a1", "type": schema.EVENT_ARTIFACT_INGESTED,
            "tenant_id": TENANT, "aggregate_id": "art_1", "sequence": 1,
            "data": {"artifact_id": "art_1"}})
        assert result["outcome"] == "applied"
        assert h.rows("SELECT * FROM render_segment WHERE state='DONE'") == []

    def test_event_ranges_are_not_trusted_as_the_suppression_source(self, h):
        """§4.2 L1：事件不是授权、也不是输入 —— 复用范围回读 S41 的 diff。

        事件里说「整片都变了」，但 S41 的实时 diff 什么也没说，所以段仍
        可复用。把事件当数据源会让一条伪造/过期的事件改掉渲染计划。
        """
        h.service.consume_timeline_revision(TENANT, self._timeline_event(
            1, "ev_1", ranges=[{"start": 0, "end": 180000}]))
        first = h.create("cmd_a")["render_job_id"]
        h.upload_all(first)
        second = h.create("cmd_b")["render_job_id"]
        job = h.service.get_render_job(TENANT, second)
        assert [s["state"] for s in job["segments"]] == ["REUSED", "REUSED"]
        # 记录的仍是事件原话（审计用途），只是没被当判据用。
        row = h.one("SELECT * FROM timeline_signal WHERE timeline_id='tl_1'")
        assert json.loads(row["affected_ranges"]) == [{"start": 0,
                                                       "end": 180000}]

    def test_event_requires_an_id(self, h):
        with pytest.raises(ServiceError) as exc:
            h.service.consume_timeline_revision(TENANT, {
                "type": schema.EVENT_TIMELINE, "tenant_id": TENANT,
                "aggregate_id": "tl_1", "sequence": 1,
                "data": {"timeline_id": "tl_1", "revision": 10}})
        assert exc.value.code == "SCHEMA_INVALID"


# ------------------------------------------------------------------ 用量上报

class TestUsageReporting:
    def test_usage_is_flushed_and_marked_reported(self, h):
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        assert len(h.rows("SELECT * FROM render_usage")) == 2
        result = h.service.report_usage(TENANT)
        assert result["reported"] and result["pending"] == 0
        assert result["error"] is None
        assert {u["resource_class"] for u in h.usage_requests} == {"gpu_second"}
        assert h.rows("SELECT * FROM render_usage WHERE reported_to_s23=0") \
            == []
        # 第二次 flush 没有新事实可报。
        assert h.service.report_usage(TENANT)["reported"] == []

    def test_s23_down_keeps_the_facts_unreported(self, h):
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        h.usage_down = True
        result = h.service.report_usage(TENANT)
        assert result["reported"] == []
        assert result["pending"] == 2
        assert result["error"] == {"code": "USAGE_REPORT_UNAVAILABLE",
                                   "status": 503}
        assert len(h.rows("SELECT * FROM render_usage"
                          " WHERE reported_to_s23=0")) == 2
        # 恢复之后还能补报。
        h.usage_down = False
        assert len(h.service.report_usage(TENANT)["reported"]) == 2

    def test_a_failure_mid_flush_stops_but_keeps_what_went_through(self, h):
        job_id = h.create()["render_job_id"]
        h.upload_all(job_id)
        original = h.service.usage_reporter
        seen = {"n": 0}

        def flaky(tenant_id, request):
            seen["n"] += 1
            if seen["n"] == 2:
                raise ConnectionError("S23 dropped")
            return original(tenant_id, request)

        h.service.usage_reporter = flaky
        result = h.service.report_usage(TENANT)
        assert len(result["reported"]) == 1
        assert result["pending"] == 1
        assert result["error"] is not None
        assert len(h.rows("SELECT * FROM render_usage"
                          " WHERE reported_to_s23=0")) == 1


# ------------------------------------------------------------------ HTTP 层

def _http_client(monkeypatch, tmp_path):
    """按文件路径加载 `api/app.py`。**不 `import app`，也不改 `sys.path`**：
    `pyproject.toml` 已声明 `services/composition/api`，模块名 `composition`
    来自 `services/composition/src`。"""
    db = tmp_path / "s42.db"
    monkeypatch.setenv("S42_DB", str(db))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s42_api_app", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["s42_api_app"] = module
    spec.loader.exec_module(module)
    harness = Harness(conn=module._conn)
    module.service = harness.service
    module._idem = harness.idem
    return TestClient(module.app), harness, module


class TestHttpLayer:
    def _create(self, client, h, command_id="cmd_1", body=None, key=None):
        return client.post("/render-jobs", json=body or h.body(command_id),
                           headers={"X-Tenant-Id": TENANT,
                                    "Idempotency-Key": key or command_id})

    def test_create_returns_202_and_a_job(self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        h.put_artifact("vid_a", 1, VID_HASH)
        response = self._create(client, h)
        assert response.status_code == 202
        assert response.json()["state"] == "QUEUED"
        assert response.json()["replayed"] is False

    def test_same_key_same_body_replays_without_a_second_job(
            self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        h.put_artifact("vid_a", 1, VID_HASH)
        first = self._create(client, h)
        second = self._create(client, h)
        assert first.status_code == second.status_code == 202
        assert second.json()["replayed"] is True
        assert second.json()["render_job_id"] == first.json()["render_job_id"]
        assert len(h.rows("SELECT * FROM render_job")) == 1
        # 重放不走第二次许可申请。
        assert h.calls["permit"] == 1

    def test_same_key_different_body_is_409_not_an_overwrite(
            self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        h.put_artifact("vid_a", 1, VID_HASH)
        self._create(client, h)
        changed = h.body("cmd_1", purpose="preview")
        response = self._create(client, h, body=changed)
        assert response.status_code == 409
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
        details = response.json()["details"]
        assert details["stored_request_hash"] != \
            details["incoming_request_hash"]
        # 原 job 没被覆盖。
        assert h.one("SELECT purpose FROM render_job")["purpose"] == "formal"

    def test_idempotency_key_must_equal_body_command_id(
            self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        h.put_artifact("vid_a", 1, VID_HASH)
        response = self._create(client, h, body=h.body("cmd_body"),
                                key="cmd_header")
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_BODY"
        assert h.rows("SELECT * FROM render_job") == []

    def test_body_tenant_must_match_the_signed_tenant(
            self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        h.put_artifact("vid_a", 1, VID_HASH)
        body = h.body("cmd_1")
        body["tenant_id"] = OTHER
        response = self._create(client, h, body=body)
        assert response.status_code == 403
        assert response.json()["code"] == "TENANT_MISMATCH"

    def test_context_tenant_must_match_too(self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        h.put_artifact("vid_a", 1, VID_HASH)
        body = h.body("cmd_1")
        body["context"]["tenant_id"] = OTHER
        response = self._create(client, h, body=body)
        assert response.status_code == 403
        assert response.json()["code"] == "TENANT_MISMATCH"

    def test_cross_tenant_get_is_byte_identical_to_missing(
            self, monkeypatch, tmp_path):
        """架构红线：跨租户 404 与不存在 404 的 body 必须完全一样。"""
        client, h, _module = _http_client(monkeypatch, tmp_path)
        h.put_artifact("vid_a", 1, VID_HASH)
        job_id = self._create(client, h).json()["render_job_id"]

        foreign = client.get(f"/render-jobs/{job_id}",
                             headers={"X-Tenant-Id": OTHER})
        missing = client.get("/render-jobs/rj_does_not_exist",
                             headers={"X-Tenant-Id": OTHER})
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
        assert foreign.json() == {"code": "NOT_FOUND",
                                  "message": "render job not found",
                                  "retryable": False,
                                  "correlation_id": "", "details": {}}

    def test_cross_tenant_candidate_404_is_indistinguishable(
            self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        candidate, _job = h.render_to_candidate()
        foreign = client.get(f"/master-candidates/{candidate['candidate_id']}",
                             headers={"X-Tenant-Id": OTHER})
        missing = client.get("/master-candidates/mc_nope",
                             headers={"X-Tenant-Id": OTHER})
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()

    def test_cross_tenant_cancel_is_404_and_changes_nothing(
            self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        h.put_artifact("vid_a", 1, VID_HASH)
        job_id = self._create(client, h).json()["render_job_id"]
        response = client.post(
            f"/render-jobs/{job_id}:cancel", json={},
            headers={"X-Tenant-Id": OTHER, "Idempotency-Key": "cmd_x"})
        assert response.status_code == 404
        assert h.one("SELECT state FROM render_job WHERE render_job_id=?",
                     (job_id,))["state"] == "QUEUED"

    def test_missing_signed_tenant_is_422(self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        response = client.post("/render-jobs", json=h.body("cmd_1"),
                               headers={"Idempotency-Key": "cmd_1"})
        assert response.status_code == 422       # FastAPI: missing header

    def test_upstream_503_is_reported_not_swallowed(self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        h.put_artifact("vid_a", 1, VID_HASH)

        def down(tenant_id, ref):
            raise ServiceError(503, "TIMELINE_UNAVAILABLE", "S41 is down",
                               retryable=True)
        h.service.timeline_source = down
        response = self._create(client, h)
        assert response.status_code == 503
        assert response.json()["code"] == "TIMELINE_UNAVAILABLE"
        assert response.json()["retryable"] is True

    def test_usage_flush_http_reports_pending(self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        h.put_artifact("vid_a", 1, VID_HASH)
        job_id = self._create(client, h).json()["render_job_id"]
        h.upload_all(job_id)
        h.usage_down = True
        response = client.post("/usage-facts:flush",
                               headers={"X-Tenant-Id": TENANT})
        assert response.status_code == 503
        assert response.json()["code"] == "USAGE_REPORT_UNAVAILABLE"
        assert response.json()["details"] == {"pending": 2}

    def test_consume_unsupported_event_is_422(self, monkeypatch, tmp_path):
        client, _h, _module = _http_client(monkeypatch, tmp_path)
        response = client.post("/events:consume", json={
            "id": "ev_1", "type": "something.else", "tenant_id": TENANT},
            headers={"X-Tenant-Id": TENANT})
        assert response.status_code == 422
        assert response.json()["code"] == "UNSUPPORTED_EVENT"

    def test_event_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        client, _h, _module = _http_client(monkeypatch, tmp_path)
        response = client.post("/events:consume", json={
            "id": "ev_1", "type": schema.EVENT_TIMELINE, "tenant_id": OTHER,
            "aggregate_id": "tl_1", "sequence": 1,
            "data": {"timeline_id": "tl_1", "revision": 10}},
            headers={"X-Tenant-Id": TENANT})
        assert response.status_code == 403
        assert response.json()["code"] == "TENANT_MISMATCH"

    def test_event_mode_is_consumed_with_the_signed_tenant(
            self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        response = client.post("/events:consume", json={
            "event": {"id": "ev_1", "type": schema.EVENT_TIMELINE,
                      "aggregate_id": "tl_1", "sequence": 1,
                      "data": {"timeline_id": "tl_1", "revision": 10,
                               "content_hash": TIMELINE_HASH}}},
            headers={"X-Tenant-Id": TENANT})
        assert response.status_code == 200
        assert response.json()["outcome"] == "applied"
        assert h.one("SELECT * FROM timeline_signal")["tenant_id"] == TENANT

    def test_readiness_names_topic_and_graph_version(self, monkeypatch,
                                                     tmp_path):
        client, _h, _module = _http_client(monkeypatch, tmp_path)
        body = client.get("/health/readiness").json()
        assert body["ready"] is True
        assert body["topic"] == schema.TOPIC
        assert body["graph_version"] == schema.GRAPH_VERSION
        assert body["render_env_hash"]
        assert isinstance(body["contract_gaps"], list)

    def test_mix_create_over_http_is_202_and_replays(
            self, monkeypatch, tmp_path):
        client, h, _module = _http_client(monkeypatch, tmp_path)
        basis, _job = h.render_to_candidate()
        stem = h.put_artifact("stem_d", 1, STEM_HASH)
        body = {
            "command_id": "cmd_mix", "request_hash": "8" * 64,
            "context": _context(command_id="cmd_mix"),
            "source_video_ref": basis["video_artifact_ref"],
            "stems": [{"role": "dialogue", "artifact_ref": stem}],
            "mix_plan_hash": "b" * 64, "target_lufs": -16.0,
            "true_peak_ceiling": -1.0, "reason": "manual",
        }
        headers = {"X-Tenant-Id": TENANT, "Idempotency-Key": "cmd_mix"}
        first = client.post("/mix-jobs", json=body, headers=headers)
        second = client.post("/mix-jobs", json=body, headers=headers)
        assert first.status_code == second.status_code == 202
        assert second.json()["replayed"] is True
        assert second.json()["mix_job_id"] == first.json()["mix_job_id"]
