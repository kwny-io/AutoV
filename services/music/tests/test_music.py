"""S34 单测。编号对应 `plans/S34-music.md` §10 的 T34-*。

上游全部是 stub，**按真实的上游契约写**，不是按内核希望的样子写：

- S25 的 HTTP 响应**没有顶层 `selected`**（只有 `eligible[]`），`deps` 负责
  归一到 `selected`；这里按 S25 的真实响应形状喂 stub，就不再考 deps 的归一。
- S26 的 `create_job` 逐字段比对 `route[field]` 与 `command[context][field]`
  （含 `attempt_id`），所以**每个候选必须各有一条自己的路由**。测试里 stub
  按 `attempt_id` 返回不同 route，重复用同一条会被下面的断言抓住。
- `artifact.ingested` 没有 payload schema，`natural_duration_ms` 得尽力取。

最重要的三组：`TestCutPlan`（纯函数，不花钱）、`TestNoStretch`（本服务的
核心承诺）、`TestPaidPath`（S26 未配置必须 503，绝不能假装成功）。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import SqliteIdempotencyStore, canonical_request_hash

from music.schema import SCHEMA, TERMINAL_STATES
from music.service import (
    MusicService,
    ServiceError,
    content_fingerprint,
    cut_plan,
)

NOW_FUTURE = "2099-01-01T00:00:00.000Z"
POLICY_REF = {"service_id": "S02", "id": "pol_1", "revision": 3,
              "content_hash": "a" * 64}

# 47 秒的曲子、120bpm 4/4、乐句线到 38s：39 秒的需求落在两条小节线正中间，
# 最近乐句在 38s（早 1s ≤ 一小节）→ `phrase`。T34-30 的形状，也是"复用能
# 成立"的最低前提——默认探针的网格只到 6s，任何 39s 方案都会 unplaceable。
_PLACEABLE_39S = {
    "bpm": 120.0, "meter_num": 4, "meter_den": 4,
    "downbeats_ms": list(range(0, 48_000, 2_000)),
    "phrases_ms": [0, 8_000, 16_000, 24_000, 32_000, 38_000],
    "confidence": 0.95, "tail_kind": "sustaining",
    "vocal_presence": False, "loop_ranges": [],
    "detected_key": "C major",
}


# ============================================================ 上游 stub

class Harness:
    """内存库 + 幂等外壳 + 一组可编程的上游 callback。

    形状与 `api/app.py` 的 `command()` 一致：`begin` → 执行 → `finish`，
    重放时打 `replayed: True`。
    """

    def __init__(self, conn=None, **overrides):
        # `conn` 可注入：HTTP 层测试要复用 `api/app.py` 在 import 时建的那个
        # 连接，否则断言查的是另一个库。
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)
        self.calls = {"route": 0, "submit": 0, "rights": 0, "budget": 0,
                      "artifact": 0, "probe": 0, "cancel": 0, "scene": 0}

        # 合并而不是整体替换：`lb_generated_has_basis` / `lb_sync_has_territory`
        # 是 DDL 里的 CHECK，替换掉 `training_basis_ref` 会得到 IntegrityError
        # 而不是被测的那个分支。
        self.rights_result = {
            "composition_status": "licensed_generated",
            "master_status": "licensed_generated",
            "performance_status": "licensed",
            "sync_status": "licensed",
            "sync_territories": ["CN"],
            "training_basis_ref": {"service_id": "S02", "id": "basis_1",
                                   "revision": 1,
                                   "content_hash": "b" * 64},
            **(overrides.get("rights") or {}),
        }
        self.quote = overrides.get("quote", 40_000)
        self.route_overrides = overrides.get("route_overrides", {})
        self.budget_available = overrides.get("budget_available", 1_000_000)
        self.submit_error = overrides.get("submit_error")
        self.artifact_present = overrides.get("artifact_present", True)
        self.probe = overrides.get("probe", {
            "bpm": 120.0, "meter_num": 4, "meter_den": 4,
            "downbeats_ms": [0, 2000, 4000, 6000],
            "phrases_ms": [0, 8000],
            "confidence": 0.95, "tail_kind": "sustain",
            "vocal_presence": False,
            "loop_ranges": [{"in_ms": 0, "out_ms": 8000,
                             "crossfade_ms": 200, "score": 0.9}],
            "detected_key": "C major",
        })
        self.scene = overrides.get("scene", {"beat_refs": ["b1"]})

        self.service = MusicService(
            self.conn,
            rights_resolver=self._rights,
            route_resolver=self._route,
            submitter=self._submit,
            artifact_check=self._artifact,
            analysis_probe=self._probe,
            budget_check=self._budget,
            canceler=self._cancel,
            edl_provider=self._edl,
            scene_check=self._scene,
        )

    # ---- callbacks ----
    def _rights(self, ref, tenant_id):
        self.calls["rights"] += 1
        if isinstance(self.rights_result, Exception):
            raise self.rights_result
        return self.rights_result

    def _route(self, tenant_id, job, generation_command):
        self.calls["route"] += 1
        attempt_id = generation_command["attempt_id"]
        # 每个 attempt 一条独立路由：S26 会拿 attempt_id 比对，复用同一条
        # 会让第 2 个候选起全部 409 ROUTE_DECISION_STALE。
        key = self.route_overrides.get(attempt_id)
        if key is not None:
            if isinstance(key, Exception):
                raise key
            return key
        return {
            "route_id": f"rd_{attempt_id}", "revision": 1,
            "feasible": True, "content_hash": "c" * 64,
            "quote_valid_to": NOW_FUTURE,
            "attempt_id": attempt_id,
            "selected": {
                "price_snapshot_id": f"price_{attempt_id}",
                "normalized_input": {"music_intent": "warm pad"},
                "quote_upper_bound_micro": self.quote,
                "candidate_key": "prov:ch:cn",
            },
            "capabilities": {"multi_candidate": True,
                             "instrumental_only": True},
            "degradation_options": [],
        }

    def _submit(self, job, generation_command, route, candidate_index):
        self.calls["submit"] += 1
        if self.submit_error is not None:
            raise self.submit_error
        return {
            "provider_job_id": f"pj_{job['music_job_id']}_{candidate_index}",
            "operation_id": f"op_{job['music_job_id']}_{candidate_index}",
            "attempt_id": generation_command["attempt_id"],
            "admission_id": f"adm_{candidate_index}",
            "state": "QUEUED",
        }

    def _artifact(self, artifact_ref):
        self.calls["artifact"] += 1
        return self.artifact_present

    def _probe(self, artifact_ref, natural_duration_ms):
        self.calls["probe"] += 1
        if isinstance(self.probe, Exception):
            raise self.probe
        return self.probe

    def _budget(self, tenant_id, budget_scope_id, total_quote):
        self.calls["budget"] += 1
        return {"sufficient": self.budget_available >= total_quote,
                "available_micro": self.budget_available}

    def _cancel(self, provider_job_id, reason):
        self.calls["cancel"] += 1

    def _edl(self, tenant_id, scene_lineage_id, edl_revision):
        return {"edl_revision": 12}

    def _scene(self, tenant_id, scene_lineage_id, cue_index):
        self.calls["scene"] += 1
        if isinstance(self.scene, Exception):
            raise self.scene
        return self.scene

    # ---- HTTP-like shell ----
    def command(self, body, tenant_id="t1", command_id="c1"):
        payload = {**body, "command_id": command_id}
        record = self.idem.begin(tenant_id, command_id,
                                 canonical_request_hash(payload))
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = self.service.create_job(payload, tenant_id, command_id)
        except Exception:
            self.idem.abort(tenant_id, command_id)
            raise
        self.idem.finish(tenant_id, command_id,
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ---- request body ----
    def body(self, **cue_over):
        cue = {
            "cue_id": "cue_1", "cue_revision": 1,
            "scene_lineage_id": "scene_A", "cue_index": 0,
            "beat_ref": None,
            "mood": "tense", "mood_intensity": 0.7,
            "instrumentation": "hybrid",
            "tempo_hint": {"low_bpm": 90, "high_bpm": 110},
            "key_hint": "D minor",
            "music_intent": "slow-building dread under a reveal",
            "instrumental_only": True,
            "dialogue_aware": True,
        }
        cue.update(cue_over)
        return {
            "context": {
                "schema_version": "2.0",
                "tenant_id": "t1", "project_id": "prj_1", "series_id": "ser_1",
                "branch_id": "br_1", "episode_id": "ep_1",
                "home_region": "cn-east", "home_cell": "cell_a",
                "manifest_ref": {"service_id": "S01", "id": "m1",
                                 "revision": 1, "content_hash": "d" * 64},
                "node_id": "node_1", "node_epoch": 4, "execution_fence": 2,
                "memory_checkpoint": {"id": "ck1", "revision": 1},
                "policy_ref": {"service_id": "S02", "id": "pol_1",
                               "revision": 3, "content_hash": "a" * 64},
                "actor": {"kind": "agent", "id": "agent_1"},
                "command_id": "c1",
                "request_hash": "e" * 64,
                "trace_id": "trace_1",
            },
            "cue": cue,
            "duration_seconds": 42.0,
            "candidate_count": 1,
            "rights_policy_ref": dict(POLICY_REF),
            "route_policy_ref": {"service_id": "S02", "id": "router_1",
                                 "revision": 1, "content_hash": "f" * 64},
            "budget_scope_id": "budget_ep1",
            "deadline": NOW_FUTURE,
        }

    # ---- event helpers ----
    def ingest(self, job_id, candidate_index=0, *, duration_ms=42_000,
               hash_=None):
        candidate = self.conn.execute(
            "SELECT * FROM music_job_candidate WHERE music_job_id=? AND "
            "candidate_index=?", (job_id, candidate_index)).fetchone()
        assert candidate is not None, "candidate must be submitted first"
        # `mt_artifact_uniq` 是 (tenant, artifact.id, content_hash)。N 个候选
        # 若共用一个 id，第 2 份 take 会撞唯一索引——那是测试的错，不是内核
        # 的错，所以这里按候选号生成不同的 id。
        content_hash = hash_ or f"{candidate_index + 1:x}" * 64
        return self.service.on_artifact_ingested({
            "tenant_id": "t1",
            "data": {
                "source": {"provider_job_id": candidate["provider_job_id"]},
                "artifact_ref": {"service_id": "S28",
                                 "id": f"art_{candidate_index}",
                                 "revision": 1, "content_hash": content_hash},
                "natural_duration_ms": duration_ms,
                "sample_rate_hz": 48000, "channel_count": 2,
                "container_format": "audio/wav",
            },
        })


# ================================================================ CutPlan

class TestCutPlan:
    """§4.3 判定表。**纯函数**：不碰库、不花钱，所以先做（§11.1）。"""

    def test_exact_length_is_hard(self):
        got = cut_plan(natural_duration_ms=42_000, requested_duration_ms=42_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=[0, 2000, 4000], phrases_ms=[0],
                       beat_confidence=0.95, tail_kind="hard_stop",
                       loop_ranges=[])
        assert got["end_strategy"] == "hard"
        assert got["segments"][0]["output_end_ms"] == 42_000

    def test_within_80ms_is_hard_not_bar(self):
        # 差在容差内时不做音乐决策，直接切。它不是 `bar`——把它算成 bar
        # 会让"切点落在小节线上"变成凭空断言。
        got = cut_plan(natural_duration_ms=42_060, requested_duration_ms=42_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=[0, 2000, 40_000],
                       phrases_ms=[0], beat_confidence=0.95,
                       tail_kind="hard_stop", loop_ranges=[])
        assert got["end_strategy"] == "hard"

    def test_bar_when_downbeat_near(self):
        got = cut_plan(natural_duration_ms=48_000, requested_duration_ms=42_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=[0, 8000, 16_000, 24_000, 32_000, 40_000,
                                     42_000],
                       phrases_ms=[], beat_confidence=0.95,
                       tail_kind="hard_stop", loop_ranges=[])
        assert got["end_strategy"] == "bar"
        assert got["segments"][0]["output_end_ms"] <= 42_000

    def test_low_confidence_blocks_bar(self):
        # §2.4：小节线不可信时不按小节裁。低置信的假小节线会切在半拍上。
        got = cut_plan(natural_duration_ms=48_000, requested_duration_ms=42_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=[0, 8000, 16_000, 24_000, 32_000, 40_000,
                                     42_000],
                       phrases_ms=[], beat_confidence=0.5,
                       tail_kind="hard_stop", loop_ranges=[])
        assert got["end_strategy"] == "unplaceable"

    def test_phrase_before_loop(self):
        got = cut_plan(natural_duration_ms=60_000, requested_duration_ms=42_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=[0, 8_000],
                       phrases_ms=[0, 40_000, 48_000], beat_confidence=0.95,
                       tail_kind="hard_stop",
                       loop_ranges=[{"in_ms": 0, "out_ms": 8_000,
                                     "crossfade_ms": 100, "score": 0.99}])
        assert got["end_strategy"] == "phrase"

    def test_loop_when_material_too_short(self):
        got = cut_plan(natural_duration_ms=8_000, requested_duration_ms=32_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=[0, 2_000, 4_000],
                       phrases_ms=[], beat_confidence=0.95,
                       tail_kind="hard_stop",
                       loop_ranges=[{"in_ms": 0, "out_ms": 8_000,
                                     "crossfade_ms": 100, "score": 0.95}])
        assert got["end_strategy"] == "loop"
        assert got["segments"]

    def test_bar_extend_only_for_sustaining_tail(self):
        got = cut_plan(natural_duration_ms=34_000, requested_duration_ms=35_500,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=[0, 32_000], phrases_ms=[],
                       beat_confidence=0.95, tail_kind="hard_stop",
                       loop_ranges=[])
        assert got["end_strategy"] == "unplaceable"

    def test_bar_extend_when_sustaining(self):
        got = cut_plan(natural_duration_ms=34_000, requested_duration_ms=35_500,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=[0, 32_000], phrases_ms=[],
                       beat_confidence=0.95, tail_kind="sustaining",
                       loop_ranges=[])
        assert got["end_strategy"] == "bar_extend"

    def test_unplaceable_when_nothing_fits(self):
        got = cut_plan(natural_duration_ms=8_000, requested_duration_ms=42_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=[0], phrases_ms=[], beat_confidence=0.95,
                       tail_kind="hard_stop", loop_ranges=[])
        assert got["end_strategy"] == "unplaceable"
        assert got["segments"] == []

    def test_rejects_non_positive_durations(self):
        with pytest.raises(ServiceError) as exc:
            cut_plan(natural_duration_ms=0, requested_duration_ms=1000,
                     bpm=120.0, meter_num=4, meter_den=4,
                     downbeats_ms=[0], phrases_ms=[], beat_confidence=0.9,
                     tail_kind="sustaining", loop_ranges=[])
        assert exc.value.code == "DURATION_OUT_OF_RANGE"


class TestCutPlanMatrix:
    """§10 的 T34-30…T34-35，用方案里写的那些秒数，一格一条。

    120bpm 4/4 一小节 = 2000ms，所以 39 秒落在两条小节线正中间——这正是
    "落点对不上小节线、但能对上乐句"的现实形状。
    """

    DOWNBEATS_2S = list(range(0, 48_000, 2_000))

    def test_39s_from_47s_aligns_phrase(self):
        # T34-30：最近的乐句线在 38s，早 1s（≤ 一小节），因此是 phrase。
        got = cut_plan(natural_duration_ms=47_000, requested_duration_ms=39_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=self.DOWNBEATS_2S,
                       phrases_ms=[0, 8_000, 16_000, 24_000, 32_000, 38_000],
                       beat_confidence=0.95, tail_kind="hard_stop",
                       loop_ranges=[])
        assert got["end_strategy"] == "phrase"
        assert got["segments"][0]["output_end_ms"] == 38_000
        # 收尾提前 1s，尾巴交给 S42 按 fade_out_ms 处理。
        assert got["tail_gap_ms"] == 1_000

    def test_39s_from_47s_aligns_bar(self):
        # T34-31：同一组秒数，但存在一条 39s 的小节线。
        got = cut_plan(natural_duration_ms=47_000, requested_duration_ms=39_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=[*self.DOWNBEATS_2S, 39_000],
                       phrases_ms=[0, 8_000, 16_000],
                       beat_confidence=0.95, tail_kind="hard_stop",
                       loop_ranges=[])
        assert got["end_strategy"] == "bar"
        assert got["segments"][0]["output_end_ms"] == 39_000

    def test_60s_from_47s_loops(self):
        # T34-32：需求比素材长，有循环区间 → 多段拼到 60s。
        got = cut_plan(natural_duration_ms=47_000, requested_duration_ms=60_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=self.DOWNBEATS_2S, phrases_ms=[0],
                       beat_confidence=0.95, tail_kind="hard_stop",
                       loop_ranges=[{"in_ms": 0, "out_ms": 8_000,
                                     "crossfade_ms": 200, "score": 0.95}])
        assert got["end_strategy"] == "loop"
        assert len(got["segments"]) > 1
        assert got["segments"][-1]["output_end_ms"] == 60_000

    def test_48s_from_47s_bar_extend_when_sustaining(self):
        # T34-33：只差 1s（< 一小节），尾音可延续 → 延长而不是重生成。
        got = cut_plan(natural_duration_ms=47_000, requested_duration_ms=48_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=self.DOWNBEATS_2S, phrases_ms=[0],
                       beat_confidence=0.95, tail_kind="sustaining",
                       loop_ranges=[])
        assert got["end_strategy"] == "bar_extend"
        assert got["extend_ms"] == 1_000

    def test_48s_from_47s_hard_stop_is_unplaceable(self):
        # T34-34：同一格，但干脆的终止拍上延长会听起来像卡住。
        got = cut_plan(natural_duration_ms=47_000, requested_duration_ms=48_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=self.DOWNBEATS_2S, phrases_ms=[0],
                       beat_confidence=0.95, tail_kind="hard_stop",
                       loop_ranges=[])
        assert got["end_strategy"] == "unplaceable"
        assert got["reason"] == "tail_not_extendable"

    def test_90s_from_47s_without_loop_is_unplaceable(self):
        # T34-35：差 43s，又没有循环区间。**不在这里偷偷重生成。**
        got = cut_plan(natural_duration_ms=47_000, requested_duration_ms=90_000,
                       bpm=120.0, meter_num=4, meter_den=4,
                       downbeats_ms=self.DOWNBEATS_2S, phrases_ms=[0],
                       beat_confidence=0.95, tail_kind="sustaining",
                       loop_ranges=[])
        assert got["end_strategy"] == "unplaceable"
        assert got["reason"] == "duration_mismatch"
        assert got["segments"] == []


class TestNoStretch:
    """本服务的核心承诺：**只动切点，不拉伸**。"""

    def test_no_segment_exceeds_requested(self):
        for requested in (5_000, 20_000, 42_000, 120_000):
            got = cut_plan(natural_duration_ms=48_000,
                           requested_duration_ms=requested,
                           bpm=120.0, meter_num=4, meter_den=4,
                           downbeats_ms=list(range(0, 48_000, 2_000)),
                           phrases_ms=[], beat_confidence=0.95,
                           tail_kind="sustaining",
                           loop_ranges=[{"in_ms": 0, "out_ms": 8_000,
                                         "crossfade_ms": 100, "score": 0.9}])
            total = 0
            for seg in got["segments"]:
                out_len = seg["output_end_ms"] - seg["output_start_ms"]
                src_len = seg["source_out_ms"] - seg["source_in_ms"]
                # 输出段与源段等长 = 没有拉伸。
                assert out_len == src_len
                total += out_len
            assert total <= requested

    def test_duration_change_does_not_change_content_fingerprint(self):
        # §4.2：改时长重算切点，内容层一个字不变。这就是省钱的那一刀。
        args = dict(music_intent="dread", mood="tense", mood_intensity=0.7,
                    instrumentation="hybrid", tempo_low_bpm=90,
                    tempo_high_bpm=110, key_hint="D minor",
                    instrumental_only=True)
        assert content_fingerprint(**args) == content_fingerprint(**args)

    def test_fingerprint_changes_with_content(self):
        base = dict(music_intent="dread", mood="tense", mood_intensity=0.7,
                    instrumentation="hybrid", tempo_low_bpm=90,
                    tempo_high_bpm=110, key_hint="D minor",
                    instrumental_only=True)
        assert content_fingerprint(**base) != content_fingerprint(
            **{**base, "mood": "ominous"})
        assert content_fingerprint(**base) != content_fingerprint(
            **{**base, "mood_intensity": 0.9})
        assert content_fingerprint(**base) != content_fingerprint(
            **{**base, "instrumental_only": False})


# ============================================================ 校验 / 契约

class TestValidation:
    def test_blank_intent_rejected(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command(h.body(music_intent="   "))
        assert exc.value.code == "EMPTY_MUSIC_INTENT"

    def test_mood_must_be_closed_set(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command(h.body(mood="epic"))
        assert exc.value.code == "INVALID_MOOD"

    def test_mood_intensity_range(self):
        # T34-05：强度越界有自己的码，和"情绪不在闭集"（T34-04）分开。
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command(h.body(mood_intensity=1.5))
        assert exc.value.code == "INVALID_MOOD_INTENSITY"

    def test_tempo_hint_low_above_high(self):
        # T34-09。
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command(h.body(tempo_hint={"low_bpm": 140, "high_bpm": 90}))
        assert exc.value.code == "TEMPO_HINT_INVALID"

    def test_instrumentation_closed_set(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command(h.body(instrumentation="kazoo"))
        assert exc.value.code == "INVALID_INSTRUMENTATION"

    def test_duration_must_be_positive(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command({**h.body(), "duration_seconds": 0})
        assert exc.value.code == "DURATION_OUT_OF_RANGE"

    def test_candidate_count_not_silently_downgraded(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command({**h.body(), "candidate_count": 4})
        assert exc.value.code == "CANDIDATE_COUNT_OUT_OF_RANGE"

    def test_rights_ref_must_be_s02(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command({**h.body(), "rights_policy_ref":
                       {**POLICY_REF, "service_id": "S99"}})
        assert exc.value.code == "INVALID_RIGHTS_POLICY_REF"

    def test_past_deadline_rejected(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command({**h.body(), "deadline": "2000-01-01T00:00:00.000Z"})
        assert exc.value.code == "DEADLINE_PASSED"

    def test_context_must_carry_all_18_keys(self):
        # S26 逐键查 18 键。少一个就是上游 400，而服务端没权限补签名值。
        h = Harness()
        body = h.body()
        del body["context"]["trace_id"]
        with pytest.raises(ServiceError) as exc:
            h.command(body)
        assert exc.value.code == "INVALID_CONTEXT"

    def test_cross_tenant_context_rejected(self):
        h = Harness()
        body = h.body()
        body["context"]["tenant_id"] = "t2"
        with pytest.raises(ServiceError) as exc:
            h.command(body)
        assert exc.value.code == "TENANT_MISMATCH"
        assert exc.value.status == 403

    def test_scene_not_found_is_404_not_403(self):
        h = Harness(scene=None)
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.status == 404
        assert exc.value.code == "SCENE_NOT_FOUND"


# ============================================================ 付费路径

class TestPaidPath:
    def test_happy_path_single_candidate_submits_once(self):
        h = Harness()
        result = h.command(h.body())
        # `_submit` 成功后的落点是 WAITING_PROVIDER（SUBMITTED 是中间态）。
        # 自动选定不在这里发生——它要等 ANALYZING 之后（§4.6）。
        assert result["state"] == "WAITING_PROVIDER"
        assert h.calls["submit"] == 1
        assert result["candidate_count"] == 1

    def test_generation_command_has_all_11_keys(self):
        # S26 逐键查 `COMMAND_REQUIRED`。少一个就是 400。
        h = Harness()
        captured = {}

        def capture(job, generation_command, route, index):
            captured.update(generation_command)
            return {"provider_job_id": "pj_x", "operation_id": "op_x",
                    "attempt_id": generation_command["attempt_id"]}

        h.service.submitter = capture
        h.command(h.body())
        for key in ("schema_version", "context", "attempt_id",
                    "variation_nonce", "idempotency_key", "modality",
                    "input", "template_ref", "route_policy_ref",
                    "budget_scope_id", "deadline"):
            assert key in captured, f"missing {key}"
        assert captured["modality"] == "music"
        # music 的 input 只有 4 个键——多塞会被 S26 422。
        assert set(captured["input"]) == {
            "music_intent", "duration_seconds", "instrumental_only",
            "rights_policy_ref"}

    def test_mood_never_reaches_provider_input(self):
        # C1：mood / 强度 / 编制契约里没有位置。
        h = Harness()
        captured = {}

        def capture(job, generation_command, route, index):
            captured.update(generation_command)
            return {"provider_job_id": "pj_x", "attempt_id": "at_x"}

        h.service.submitter = capture
        h.command(h.body())
        assert "mood" not in json.dumps(captured["input"])
        assert "instrumentation" not in json.dumps(captured["input"])

    def test_prepay_budget_covers_all_candidates(self):
        # F6：预算按 N 份之和查，不是一份。
        h = Harness(quote=40_000)
        seen = {}
        original = h._budget

        def spy(tenant_id, budget_scope_id, total_quote):
            seen["total"] = total_quote
            return original(tenant_id, budget_scope_id, total_quote)

        h.service.budget_check = spy
        h.command({**h.body(), "candidate_count": 3})
        assert seen["total"] == 120_000

    def test_insufficient_budget_rejects_before_spending(self):
        h = Harness(quote=40_000, budget_available=50_000)
        result = h.command({**h.body(), "candidate_count": 3})
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "BUDGET_INSUFFICIENT_FOR_CANDIDATES"
        assert h.calls["submit"] == 0

    def test_route_infeasible_does_not_submit(self):
        h = Harness()
        h.route_overrides = {}  # fall back to default
        h.service.route_resolver = lambda t, j, c: {
            "feasible": False,
            "degradation_options": [{"kind": "shorten_duration"}]}
        result = h.command(h.body())
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "ROUTE_INFEASIBLE"
        assert h.calls["submit"] == 0

    def test_route_lacking_multi_candidate_is_local_reject(self):
        # F8：不降级为 1。悄悄少给两个候选，导演会以为只对比了一份。
        h = Harness()
        h.service.route_resolver = lambda t, j, c: {
            "route_id": f"rd_{c['attempt_id']}", "feasible": True,
            "quote_valid_to": NOW_FUTURE, "attempt_id": c["attempt_id"],
            "selected": {"quote_upper_bound_micro": 40_000},
            "capabilities": {"multi_candidate": False,
                             "instrumental_only": True},
        }
        result = h.command({**h.body(), "candidate_count": 3})
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "CAPABILITY_MISSING"
        assert h.calls["submit"] == 0
        assert h.conn.execute(
            "SELECT COUNT(*) FROM music_intent_ledger").fetchone()[0] == 0

    def test_route_lacking_instrumental_only_is_local_reject(self):
        # F9：无人声是剧情需求，不是偏好。
        h = Harness()
        h.service.route_resolver = lambda t, j, c: {
            "route_id": f"rd_{c['attempt_id']}", "feasible": True,
            "quote_valid_to": NOW_FUTURE, "attempt_id": c["attempt_id"],
            "selected": {"quote_upper_bound_micro": 40_000},
            "capabilities": {"multi_candidate": True,
                             "instrumental_only": False},
        }
        result = h.command(h.body(instrumental_only=True))
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "CAPABILITY_MISSING"
        assert h.calls["submit"] == 0

    def test_residency_forbidden_is_rejected_before_paying(self):
        # F29：驻留必须在第一次付费提交之前判死。
        h = Harness()
        h.service.route_resolver = lambda t, j, c: {
            "route_id": f"rd_{c['attempt_id']}", "feasible": True,
            "quote_valid_to": NOW_FUTURE, "attempt_id": c["attempt_id"],
            "residency_allowed": False,
            "selected": {"quote_upper_bound_micro": 40_000},
        }
        result = h.command(h.body())
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "RESIDENCY_FORBIDDEN"
        assert h.calls["submit"] == 0

    def test_route_without_quote_503s(self):
        h = Harness()
        h.service.route_resolver = lambda t, j, c: {
            "route_id": "rd_1", "feasible": True,
            "selected": {"normalized_input": {}}}
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"
        assert h.calls["submit"] == 0

    def test_route_without_id_503s(self):
        h = Harness()
        h.service.route_resolver = lambda t, j, c: {
            "feasible": True, "quote_upper_bound_micro": 1000,
            "selected": {"quote_upper_bound_micro": 1000}}
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"

    def test_multi_candidate_submits_n_times(self):
        h = Harness()
        result = h.command({**h.body(), "candidate_count": 3})
        assert h.calls["submit"] == 3
        assert h.calls["route"] == 3
        assert result["candidate_count"] == 3

    def test_each_candidate_gets_its_own_route(self):
        # S26 会按 attempt_id 比对；复用一条 route 第 2 个起就 stale。
        h = Harness()
        attempts = []

        def capture_route(tenant_id, job, generation_command):
            attempts.append(generation_command["attempt_id"])
            return {"route_id": f"rd_{generation_command['attempt_id']}",
                    "feasible": True, "quote_valid_to": NOW_FUTURE,
                    "attempt_id": generation_command["attempt_id"],
                    "selected": {"quote_upper_bound_micro": 40_000}}

        h.service.route_resolver = capture_route
        h.command({**h.body(), "candidate_count": 2})
        assert len(set(attempts)) == 2

    def test_candidate_keys_unique_per_index(self):
        h = Harness()
        captured = []

        def capture(job, generation_command, route, index):
            captured.append((generation_command["attempt_id"],
                             generation_command["idempotency_key"]))
            return {"provider_job_id": f"pj_{index}",
                    "attempt_id": generation_command["attempt_id"]}

        h.service.submitter = capture
        h.command({**h.body(), "candidate_count": 3})
        assert len({a for a, _ in captured}) == 3
        assert len({k for _, k in captured}) == 3

    def test_partial_submit_failure_kills_whole_batch(self):
        # F10/F11：不交付半个候选集。
        h = Harness()
        calls = {"n": 0}
        original = h._submit

        def flaky(job, generation_command, route, index):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise ServiceError(500, "PROVIDER_ERROR", "boom")
            return original(job, generation_command, route, index)

        h.service.submitter = flaky
        result = h.command({**h.body(), "candidate_count": 3})
        assert result["state"] == "FAILED"
        assert result["reason_code"] == "PARTIAL_CANDIDATES"
        takes = h.conn.execute(
            "SELECT COUNT(*) AS n FROM music_take").fetchone()["n"]
        assert takes == 0

    def test_unconfigured_s26_never_fakes_success(self):
        # 最关键的一条：没配 S26 时绝不能返回假的 provider_job_id。
        from music.deps import build_submitter
        h = Harness()
        h.service.submitter = build_submitter(None)
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.status == 503
        assert exc.value.code == "GENERATION_JOBS_UNAVAILABLE"
        # 没花出去的钱也不该进台账。
        rows = h.conn.execute(
            "SELECT COUNT(*) AS n FROM music_intent_ledger").fetchone()["n"]
        assert rows == 0

    def test_unconfigured_s25_never_invents_route(self):
        from music.deps import build_route_resolver
        h = Harness()
        h.service.route_resolver = build_route_resolver(None)
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.status == 503
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"

    def test_unconfigured_s23_never_assumes_budget(self):
        from music.deps import build_budget_check
        h = Harness()
        h.service.budget_check = build_budget_check(None)
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.status == 503
        assert exc.value.code == "BUDGET_CHECK_UNAVAILABLE"

    def test_license_incomplete_blocks_without_spending(self):
        # 四元组任一 unknown → POLICY_BLOCKED + 缺口，不花钱。
        h = Harness(rights={
            "composition_status": "unknown",
            "master_status": "licensed_generated",
            "performance_status": "licensed",
            "sync_status": "licensed",
        })
        result = h.command(h.body())
        assert result["state"] == "POLICY_BLOCKED"
        assert result["reason_code"] == "LICENSE_INCOMPLETE"
        assert h.calls["submit"] == 0
        gap = h.conn.execute(
            "SELECT kind FROM music_gap WHERE state='open'").fetchone()
        assert gap["kind"] == "license_incomplete"


# ============================================================ 幂等

class TestIdempotency:
    def test_replay_returns_same_result(self):
        h = Harness()
        body = h.body()
        first = h.command(body)
        second = h.command(body)
        assert second["replayed"] is True
        assert second["music_job_id"] == first["music_job_id"]
        # 重放不重新提交。
        assert h.calls["submit"] == 1

    def test_same_key_different_body_conflicts(self):
        from idempotency_sdk import CommandConflict
        h = Harness()
        h.command(h.body())
        with pytest.raises(CommandConflict):
            h.command(h.body(music_intent="different intent"))


# ============================================================ 归档 / 分析

class TestIngest:
    def test_happy_path_through_to_candidates_ready(self):
        h = Harness()
        created = h.command(h.body())
        result = h.ingest(created["music_job_id"])
        assert result["state"] == "CANDIDATES_READY"
        take = h.conn.execute("SELECT * FROM music_take").fetchone()
        assert take is not None
        assert take["rights_state"] == "usable"
        grid = h.conn.execute("SELECT * FROM beat_grid").fetchone()
        assert grid is not None
        loops = h.conn.execute("SELECT * FROM loop_point_set").fetchone()
        assert loops is not None

    def test_single_candidate_auto_selected(self):
        h = Harness()
        created = h.command(h.body())
        h.ingest(created["music_job_id"])
        sel = h.conn.execute("SELECT * FROM cue_selection").fetchone()
        assert sel["state"] == "selected"
        assert sel["selection_kind"] == "auto_single"
        assert sel["selected_by"] == "system:auto_single"

    def test_multi_candidate_stays_pending(self):
        # §4.6：多候选不替导演挑。
        h = Harness()
        created = h.command({**h.body(), "candidate_count": 2})
        h.ingest(created["music_job_id"], 0)
        h.ingest(created["music_job_id"], 1)
        sel = h.conn.execute("SELECT * FROM cue_selection").fetchone()
        assert sel["state"] == "pending"
        assert sel["music_take_id"] is None

    def test_missing_duration_is_failure_not_zero(self):
        # C4：0ms 会让每个 CutPlan 都 unplaceable，看起来像正常缺口。
        h = Harness()
        created = h.command(h.body())
        result = h.ingest(created["music_job_id"], duration_ms=None)
        assert result["reason_code"] == "DURATION_UNKNOWN"
        assert result["state"] == "FAILED"
        count = h.conn.execute(
            "SELECT COUNT(*) AS n FROM music_take").fetchone()["n"]
        assert count == 0

    def test_hash_mismatch_fails(self):
        h = Harness(artifact_present=False)
        created = h.command(h.body())
        result = h.ingest(created["music_job_id"])
        assert result["reason_code"] == "HASH_MISMATCH"

    def test_artifact_check_none_does_not_veto(self):
        # 查不到 ≠ 不一致。不能让 S28 抖动否决一份已付费的 take。
        h = Harness()
        h.service.artifact_check = lambda ref: None
        created = h.command(h.body())
        result = h.ingest(created["music_job_id"])
        assert result["state"] == "CANDIDATES_READY"

    def test_unanalyzable_records_gap(self):
        h = Harness(probe={"bpm": None, "downbeats_ms": []})
        created = h.command(h.body())
        result = h.ingest(created["music_job_id"])
        assert result["state"] == "GAP_RECORDED"
        assert result["reason_code"] == "BEAT_UNANALYZABLE"
        gap = h.conn.execute(
            "SELECT kind FROM music_gap WHERE state='open'").fetchone()
        assert gap["kind"] == "beat_unanalyzable"

    def test_vocal_presence_when_instrumental_required(self):
        h = Harness(probe={
            "bpm": 120.0, "meter_num": 4, "meter_den": 4,
            "downbeats_ms": [0, 2000], "phrases_ms": [], "confidence": 0.95,
            "tail_kind": "sustain", "vocal_presence": True, "loop_ranges": []})
        created = h.command(h.body(instrumental_only=True))
        result = h.ingest(created["music_job_id"])
        assert result["state"] == "GAP_RECORDED"

    def test_partial_archive_fails_batch(self):
        h = Harness()
        created = h.command({**h.body(), "candidate_count": 2})
        result = h.ingest(created["music_job_id"], 0)
        # 只收了 1 份但要求 2 份 → 不能当作成功。
        assert result["state"] in ("WAITING_PROVIDER", "INGESTING",
                                   "CANDIDATES_READY")
        # 第 1 份还在等，第 0 份收了 → 仍是等待。
        assert result.get("state") == "INGESTING"


# ============================================================ 许可四元组

class TestLicense:
    def test_unknown_sync_blocks(self):
        h = Harness(rights={
            "composition_status": "licensed_generated",
            "master_status": "licensed_generated",
            "performance_status": "licensed",
            "sync_status": "unknown",
        })
        result = h.command(h.body())
        assert result["state"] == "POLICY_BLOCKED"

    def test_expired_sync_blocks(self):
        h = Harness(rights={
            "composition_status": "licensed_generated",
            "master_status": "licensed_generated",
            "performance_status": "licensed",
            "sync_status": "expired",
        })
        result = h.command(h.body())
        assert result["state"] == "POLICY_BLOCKED"

    def test_public_domain_is_usable(self):
        h = Harness(rights={
            "composition_status": "public_domain",
            "master_status": "public_domain",
            "performance_status": "not_applicable",
            "sync_status": "licensed",
        })
        result = h.command(h.body())
        assert result["state"] == "WAITING_PROVIDER"


# ============================================================ 只读接口

class TestReadOnly:
    def test_get_cue_returns_candidates(self):
        h = Harness()
        created = h.command(h.body())
        h.ingest(created["music_job_id"])
        got = h.service.get_cue("t1", "cue_1")
        assert got["cue_id"] == "cue_1"
        assert got["candidates"]
        assert got["cut_plan"] is not None

    def test_cross_tenant_get_is_404(self):
        h = Harness()
        created = h.command(h.body())
        h.ingest(created["music_job_id"])
        with pytest.raises(ServiceError) as exc:
            h.service.get_cue("t2", "cue_1")
        assert exc.value.status == 404

    def test_take_license_exposes_no_intent(self):
        # S43 只需要申报信息，不需要看剧情（权限分离）。
        h = Harness()
        created = h.command(h.body())
        h.ingest(created["music_job_id"])
        take = h.conn.execute("SELECT * FROM music_take").fetchone()
        got = h.service.get_take_license("t1", take["music_take_id"])
        blob = json.dumps(got, ensure_ascii=False)
        assert "dread" not in blob
        assert got["license"]["composition"]["status"] == "licensed_generated"
        assert got["rights_state"] == "usable"

    def test_duration_only_edit_reuses_take_and_writes_no_ledger_row(self):
        # T34-36 / §4.2：只改时长 → 指纹不变 → 复用 take，台账仍是 1 行
        # （第一次生成那一行），第二次一个字节都不再花。
        h = Harness(probe=_PLACEABLE_39S)
        first = h.command(h.body(), command_id="c1")
        h.ingest(first["music_job_id"], duration_ms=47_000)
        ledger_after_first = h.conn.execute(
            "SELECT COUNT(*) FROM music_intent_ledger").fetchone()[0]
        assert ledger_after_first == 1
        submits_after_first = h.calls["submit"]

        # 同一个 cue，只把时长从 42 秒改成 39 秒。
        second = h.command({**h.body(), "duration_seconds": 39.0},
                           command_id="c2")
        assert h.calls["submit"] == submits_after_first
        assert h.conn.execute(
            "SELECT COUNT(*) FROM music_intent_ledger").fetchone()[0] \
            == ledger_after_first
        # 指纹一致：两版 job 指向同一份内容。
        assert second["content_fingerprint"] == first["content_fingerprint"]
        assert second["requested_duration_ms"] == 39_000
        # 复用响应**没有 job**（不建 CREATED 行，业务幂等索引不占位）。
        assert second["reused"] is True
        assert second["music_job_id"] is None
        assert h.conn.execute(
            "SELECT COUNT(*) FROM music_job").fetchone()[0] == 1
        # 位置层这次直接按**新时长**算好了（T34-30）：38s 收，早 1s。
        assert second["cut_plan"]["end_strategy"] == "phrase"
        assert second["cut_plan"]["requested_duration_ms"] == 39_000
        assert second["cut_plan"]["segments"][0]["output_end_ms"] == 38_000

        # 重算端点用**存储的那次请求**（42 秒，落在小节线上 → bar），且免费。
        plan = h.service.recompute_cut_plan("t1", "cue_1", 7)
        assert plan["cut_plans"][0]["end_strategy"] == "bar"
        assert plan["requested_duration_ms"] == 42_000
        # 缓存是派生副本：清掉它，答案不变。
        h.conn.execute("DELETE FROM cut_plan_cache")
        again = h.service.recompute_cut_plan("t1", "cue_1", 7)
        assert again["cut_plans"][0]["end_strategy"] == "bar"
        assert h.calls["submit"] == submits_after_first

    def test_reuse_skipped_when_artifact_gone(self):
        # S28 明确说"没了"（False）才否决；"没问成"（None）不否决——
        # 见 test_artifact_check_none_does_not_veto。
        h = Harness(probe=_PLACEABLE_39S)
        first = h.command(h.body(), command_id="c1")
        h.ingest(first["music_job_id"], duration_ms=47_000)
        h.artifact_present = False
        out = h.command({**h.body(), "duration_seconds": 39.0},
                        command_id="c2")
        assert out["reused"] is False
        assert out["music_job_id"] is not None
        assert h.calls["submit"] == 2

    def test_reuse_skipped_when_rights_void(self):
        h = Harness(probe=_PLACEABLE_39S)
        first = h.command(h.body(), command_id="c1")
        h.ingest(first["music_job_id"], duration_ms=47_000)
        take = h.conn.execute("SELECT music_take_id FROM music_take").fetchone()
        h.conn.execute("UPDATE music_take SET rights_state='void' "
                       "WHERE music_take_id=?", (take["music_take_id"],))
        out = h.command({**h.body(), "duration_seconds": 39.0},
                        command_id="c2")
        assert out["reused"] is False
        assert h.calls["submit"] == 2

    def test_same_request_is_reused_even_without_a_duration_change(self):
        # 指纹与时长都没变：这也是复用（§4.2 第 1 层），仍然不花钱。
        h = Harness(probe=_PLACEABLE_39S)
        first = h.command(h.body(), command_id="c1")
        h.ingest(first["music_job_id"], duration_ms=47_000)
        before = h.calls["submit"]
        out = h.command(h.body(), command_id="c2")
        assert out["reused"] is True
        assert out["music_job_id"] is None
        assert h.calls["submit"] == before

    def test_unplaceable_take_is_not_reused(self):
        # T34-35：90 秒要不到 47 秒的曲子（没有循环区间）。复用会交一份空
        # 位置给 S42，所以退回生成——这条路**该花钱**。
        h = Harness(probe={
            "bpm": 120.0, "meter_num": 4, "meter_den": 4,
            "downbeats_ms": list(range(0, 48_000, 2_000)),
            "phrases_ms": [0, 8_000, 38_000],
            "confidence": 0.95, "tail_kind": "hard_stop",
            "vocal_presence": False, "loop_ranges": [],
            "detected_key": "C major",
        })
        first = h.command(h.body(), command_id="c1")
        h.ingest(first["music_job_id"], duration_ms=47_000)
        out = h.command({**h.body(), "duration_seconds": 90.0},
                        command_id="c2")
        assert out["reused"] is False
        assert h.calls["submit"] == 2

    def test_reuse_does_not_serve_multi_candidate(self):
        # §7.1 / §11 U2 的"复用已有 take + 补 2 个"需要给缺额开子 job，
        # 方案标为未定（MUS-14）。手里最多一份 take，静默交 1 个是降级。
        # 探针能放下 39s，所以这里唯一的拦路条件就是 candidate_count。
        h = Harness(probe=_PLACEABLE_39S)
        first = h.command(h.body(), command_id="c1")   # 1 个候选
        h.ingest(first["music_job_id"], duration_ms=47_000)
        before = h.calls["submit"]
        out = h.command({**h.body(), "duration_seconds": 39.0,
                         "candidate_count": 3}, command_id="c2")
        assert out["reused"] is False
        assert out["candidate_count"] == 3
        assert h.calls["submit"] == before + 3

    def test_cut_plan_recompute_is_free(self):
        h = Harness()
        created = h.command(h.body())
        h.ingest(created["music_job_id"])
        submits = h.calls["submit"]
        result = h.service.recompute_cut_plan("t1", "cue_1", 7)
        assert h.calls["submit"] == submits
        assert result["cut_plans"]

    def test_list_cues_filters_by_scene(self):
        h = Harness()
        created = h.command(h.body())
        h.ingest(created["music_job_id"])
        got = h.service.list_cues("t1", scene_lineage_id="scene_A")
        assert len(got["items"]) >= 1
        empty = h.service.list_cues("t1", scene_lineage_id="scene_nope")
        assert empty["items"] == []

    def test_checkpoint_carries_selected_take(self):
        h = Harness()
        created = h.command(h.body())
        h.ingest(created["music_job_id"])
        got = h.service.get_scene_checkpoint("t1", "scene_A")
        assert got["checkpoints"]
        assert got["checkpoints"][0]["music_take_id"] is not None


# ============================================================ 选定 / 重试

class TestSelection:
    def test_human_select_supersedes_auto(self):
        h = Harness()
        created = h.command({**h.body(), "candidate_count": 2})
        h.ingest(created["music_job_id"], 0)
        h.ingest(created["music_job_id"], 1)
        takes = h.conn.execute(
            "SELECT music_take_id FROM music_take ORDER BY candidate_index"
        ).fetchall()
        sel = h.service.create_selection("t1", {
            "command_id": "sel_1", "music_take_id": takes[1]["music_take_id"],
            "selected_by": "director_1", "selection_kind": "human"}, "sel_1")
        assert sel["state"] == "selected"

    def test_reject_all_raises_gap(self):
        h = Harness()
        created = h.command({**h.body(), "candidate_count": 2})
        h.ingest(created["music_job_id"], 0)
        h.ingest(created["music_job_id"], 1)
        selection = h.conn.execute(
            "SELECT * FROM cue_selection").fetchone()
        result = h.service.reject_all(
            "t1", selection["selection_id"],
            {"command_id": "rej_1", "reason": "neither fits"}, "rej_1")
        assert result["state"] == "rejected_all"
        gap = h.conn.execute(
            "SELECT kind FROM music_gap WHERE state='open'").fetchone()
        assert gap["kind"] == "all_candidates_rejected"

    def test_retry_only_from_failed(self):
        h = Harness()
        created = h.command(h.body())
        # 还在 SUBMITTED/WAITING_PROVIDER，不是 FAILED → 409。
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job("t1", created["music_job_id"], {}, "r1")
        assert exc.value.status == 409

    def test_cancel_unconfirmed_goes_unknown(self):
        # 取消没确认 → WAITING_UNKNOWN，不标 CANCELED。
        h = Harness()
        created = h.command(h.body())
        h.service.canceler = lambda pj, reason: (_ for _ in ()).throw(
            ServiceError(503, "GENERATION_JOBS_UNAVAILABLE", "nope"))
        result = h.service.cancel_job("t1", created["music_job_id"], {}, "x1")
        assert result["state"] == "WAITING_UNKNOWN"

    def test_second_selection_supersedes_the_first(self):
        # T34-26：`ms_current_idx` 只留一个；历史不是删除而是 superseded。
        h = Harness()
        created = h.command({**h.body(), "candidate_count": 2})
        h.ingest(created["music_job_id"], 0)
        h.ingest(created["music_job_id"], 1)
        takes = [r["music_take_id"] for r in h.conn.execute(
            "SELECT music_take_id FROM music_take ORDER BY candidate_index")]
        h.service.create_selection("t1", {
            "music_take_id": takes[0], "selected_by": "director_1"}, "s1")
        h.service.create_selection("t1", {
            "music_take_id": takes[1], "selected_by": "director_1"}, "s2")
        # 开跑时那条 pending 也在（`_open_selection` 先建），被第一次选定顶掉。
        rows = [dict(r) for r in h.conn.execute(
            "SELECT state, music_take_id FROM cue_selection "
            "ORDER BY created_at, rowid").fetchall()]
        assert [r["state"] for r in rows] == [
            "superseded", "superseded", "selected"]
        assert rows[0]["music_take_id"] is None      # 原先的 pending
        assert rows[1]["music_take_id"] == takes[0]  # 第一次选定
        assert rows[2]["music_take_id"] == takes[1]  # 当前
        # 当前只有一条 —— 这正是 `ms_current_idx` partial unique 守的东西。
        current = h.conn.execute(
            "SELECT COUNT(*) FROM cue_selection WHERE state='selected'"
        ).fetchone()[0]
        assert current == 1
        # 检查点只有一条，指向新的那一份。
        cp = h.conn.execute(
            "SELECT music_take_id FROM music_checkpoint "
            "WHERE scene_lineage_id='scene_A' AND cue_index=0").fetchone()
        assert cp["music_take_id"] == takes[1]

    def test_pending_selection_times_out_into_gap(self):
        # T34-22 / T34-23：24h 没人选 → 缺口，**不自动选第一个**。
        h = Harness()
        created = h.command({**h.body(), "candidate_count": 2})
        h.ingest(created["music_job_id"], 0)
        h.ingest(created["music_job_id"], 1)
        sweep = h.service.sweep_selection_timeouts()
        # 刚建的选择不算超时。
        assert sweep["gaps_opened"] == []
        later = h.service.sweep_selection_timeouts("2099-01-01T00:00:00.000Z")
        assert len(later["gaps_opened"]) == 1
        gap = h.conn.execute("SELECT * FROM music_gap").fetchone()
        assert gap["kind"] == "awaiting_selection"
        # 选择本身还是 pending —— 超时不推进它。
        assert h.conn.execute(
            "SELECT state FROM cue_selection").fetchone()["state"] == "pending"


# ============================================================ 状态机

class TestStateMachine:
    def test_unknown_provider_state_never_resubmits(self):
        h = Harness()
        created = h.command(h.body())
        candidate = h.conn.execute(
            "SELECT * FROM music_job_candidate").fetchone()
        submits_before = h.calls["submit"]
        result = h.service.on_generation_state_changed({
            "tenant_id": "t1",
            "data": {"provider_job_id": candidate["provider_job_id"],
                     "state": "SUBMISSION_UNKNOWN"},
        })
        job = h.conn.execute("SELECT * FROM music_job").fetchone()
        assert job["state"] == "WAITING_UNKNOWN"
        assert h.calls["submit"] == submits_before

    def test_reconcile_unrecoverable_records_gap_never_failed(self):
        # F26：对账确认没成 → GAP_RECORDED + submission_unrecoverable 缺口。
        # **不是 FAILED**：FAILED 允许 :retry 重提付费任务，而这里不知道供应商
        # 侧有没有残留，重提就是再花一次钱。
        h = Harness()
        created = h.command(h.body())
        candidate = h.conn.execute(
            "SELECT * FROM music_job_candidate").fetchone()
        h.service.on_generation_state_changed({
            "tenant_id": "t1",
            "data": {"provider_job_id": candidate["provider_job_id"],
                     "state": "SUBMISSION_UNKNOWN"},
        })
        submits_before = h.calls["submit"]
        result = h.service.reconcile_unknown(
            "t1", created["music_job_id"], {"succeeded": False})
        assert result["state"] == "GAP_RECORDED"
        assert result["reconciled"] is True
        assert h.calls["submit"] == submits_before
        gap = h.conn.execute(
            "SELECT * FROM music_gap WHERE state='open'").fetchone()
        assert gap["kind"] == "submission_unrecoverable"
        # 不可重试——GAP_RECORDED 不在 RETRYABLE_STATES 里。
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job("t1", created["music_job_id"], {}, "r9")
        assert exc.value.code == "JOB_NOT_RETRYABLE"

    def test_reconcile_still_unknown_keeps_state(self):
        # 供应商也答不上来 → 保持 WAITING_UNKNOWN，不动 state、不重提。
        h = Harness()
        created = h.command(h.body())
        candidate = h.conn.execute(
            "SELECT * FROM music_job_candidate").fetchone()
        h.service.on_generation_state_changed({
            "tenant_id": "t1",
            "data": {"provider_job_id": candidate["provider_job_id"],
                     "state": "SUBMISSION_UNKNOWN"},
        })
        result = h.service.reconcile_unknown("t1", created["music_job_id"], None)
        assert result["reconciled"] is False
        assert result["state"] == "WAITING_UNKNOWN"

    def test_cross_tenant_event_ignored(self):
        h = Harness()
        created = h.command(h.body())
        candidate = h.conn.execute(
            "SELECT * FROM music_job_candidate").fetchone()
        result = h.service.on_generation_state_changed({
            "tenant_id": "t2",
            "data": {"provider_job_id": candidate["provider_job_id"],
                     "state": "RUNNING"},
        })
        assert result.get("ignored") is True

    def test_terminal_states_are_terminal(self):
        assert "CANDIDATES_READY" in TERMINAL_STATES
        assert "FAILED" in TERMINAL_STATES
        assert "WAITING_UNKNOWN" not in TERMINAL_STATES


# ============================================================ 数据库约束

class TestDatabaseConstraints:
    def test_take_rejects_non_s28_artifact(self):
        h = Harness()
        created = h.command(h.body())
        h.ingest(created["music_job_id"])
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE music_take SET artifact_ref=?",
                (json.dumps({"service_id": "S99", "id": "x", "revision": 1,
                             "content_hash": "1" * 64}),))

    def test_take_rejects_wanted_vocal_contradiction(self):
        h = Harness()
        created = h.command(h.body(instrumental_only=True))
        h.ingest(created["music_job_id"])
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "UPDATE music_take SET vocal_presence=1 "
                "WHERE instrumental_required=1")

    def test_beat_grid_rejects_empty_downbeats(self):
        h = Harness()
        created = h.command(h.body())
        h.ingest(created["music_job_id"])
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute("UPDATE beat_grid SET downbeat_ms='[]'")

    def test_license_binding_rejects_non_s02_ref(self):
        # `with` 里先建 Harness：`Harness.__init__` 自己会 executeSCHEMA，
        # 把它放进去会让"建库失败"也满足 `pytest.raises`。
        h = Harness()
        with pytest.raises(sqlite3.IntegrityError) as exc:
            h.conn.execute(
                "INSERT INTO license_binding (license_binding_id, tenant_id, "
                "policy_ref, composition_status, master_status, "
                "performance_status, sync_status, sync_territories, "
                "sync_media, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("lb_x", "t1", json.dumps({"service_id": "S99", "id": "p",
                                           "revision": 1,
                                           "content_hash": "a" * 64}),
                 "unknown", "unknown", "unknown", "unknown", "[]", "[]",
                 NOW_FUTURE))
        # 断言是**这条**约束失败，而不是刚好撞上别的 CHECK。
        assert "lb_ref_known" in str(exc.value)

    def test_generated_license_without_training_basis_is_rejected(self):
        # T34-15：声称"生成授权"却拿不出训练合规声明。这条 CHECK 与
        # `_bind_rights` 的合并默认值正好互为反例（后者总是带上 basis）。
        h = Harness()
        with pytest.raises(sqlite3.IntegrityError) as exc:
            h.conn.execute(
                "INSERT INTO license_binding (license_binding_id, tenant_id, "
                "policy_ref, composition_status, master_status, "
                "performance_status, sync_status, sync_territories, "
                "sync_media, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("lb_y", "t1", json.dumps(dict(POLICY_REF)),
                 "licensed_generated", "licensed_generated", "licensed",
                 "licensed", '["CN"]', '[]', NOW_FUTURE))
        assert "lb_generated_has_basis" in str(exc.value)


# ============================================================ HTTP 层

def _http_client(monkeypatch, tmp_path, **stub_overrides):
    """`api/app.py` 的 TestClient，上游全部换成 Harness 的 stub。

    `app.py` 在 import 那一刻就建连接、拼八个上游，所以：
    - `S34_DB` 必须指向独立文件库；
    - `module.service` / `module._idem` 必须重绑——路由函数在定义时就捕获了
      模块全局，换 Harness 不管用；
    - Harness 必须复用 `module._conn`，否则断言查的是另一个库。
    """
    monkeypatch.setenv("S34_DB", str(tmp_path / "s34.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    # 不能 `import app`：别的服务也有 `api/app.py`，可能已经进了 sys.modules。
    spec = importlib.util.spec_from_file_location("s34_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s34_api_app"] = module

    h = Harness(conn=module._conn, **stub_overrides)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h


def _post(client, body, *, command_id="c1", tenant="t1",
          path="/music-jobs"):
    return client.post(path, json=body,
                       headers={"Idempotency-Key": command_id,
                                "X-Tenant-Id": tenant})


class TestHttpLayer:
    """13 条路由此前一行都没被执行过。"""

    def test_create_job(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = _post(client, h.body())
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "WAITING_PROVIDER"
        assert r.json()["replayed"] is False

    def test_replay_through_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        first = _post(client, h.body())
        again = _post(client, h.body())
        assert again.status_code == 200
        assert again.json()["replayed"] is True
        assert again.json()["music_job_id"] == first.json()["music_job_id"]
        assert h.calls["submit"] == 1

    def test_same_key_different_body_is_409(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = _post(client, h.body(music_intent="a different dread"))
        assert r.status_code == 409, r.text

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = h.body()
        body["context"]["tenant_id"] = "t_other"
        r = _post(client, body)
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_MISMATCH"

    def test_tenant_id_in_payload_root_also_checked(self, monkeypatch,
                                                    tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = {**h.body(), "tenant_id": "t_other"}
        r = _post(client, body)
        assert r.status_code == 403

    def test_missing_idempotency_key_is_not_5xx(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = client.post("/music-jobs", json=h.body(),
                        headers={"X-Tenant-Id": "t1"})
        # FastAPI 对缺 header 报 422；只要不是 5xx 就算拦住了。
        assert 400 <= r.status_code < 500

    def test_cross_tenant_get_is_404(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = client.get("/music-cues/cue_1", headers={"X-Tenant-Id": "t2"})
        assert r.status_code == 404
        assert r.json()["code"] == "MUSIC_CUE_NOT_FOUND"

    def test_get_cue_after_ingest(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        h.ingest(created["music_job_id"])
        r = client.get("/music-cues/cue_1", headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["cue_id"] == "cue_1"
        assert body["candidates"]
        assert body["cut_plan"] is not None
        assert body["license"]["sync"]["status"] == "licensed"
        # 位置层每次现算，EDL 版本号来自注入的 edl_provider。
        assert body["edl_revision"] == 12

    def test_get_cue_unknown_is_404(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/music-cues/nope", headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 404
        assert r.json()["code"] == "MUSIC_CUE_NOT_FOUND"

    def test_list_cues_and_checkpoint(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        h.ingest(created["music_job_id"])
        r = client.get("/music-cues", params={"scene_lineage_id": "scene_A"},
                       headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert r.json()["total"] == 1
        r = client.get("/scenes/scene_A/music-checkpoint",
                       headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert r.json()["cue_count"] == 1
        assert r.json()["checkpoints"][0]["music_take_id"]

    def test_take_and_license(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        h.ingest(created["music_job_id"])
        take_id = h.conn.execute(
            "SELECT music_take_id FROM music_take").fetchone()["music_take_id"]
        r = client.get(f"/music-takes/{take_id}",
                       headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert r.json()["beat_grid"]["bpm"] == 120.0
        r = client.get(f"/music-takes/{take_id}/license",
                       headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert "dread" not in r.text

    def test_selection_routes(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        h.ingest(created["music_job_id"])
        take_id = h.conn.execute(
            "SELECT music_take_id FROM music_take").fetchone()["music_take_id"]
        # 每条路由用自己的幂等键：store 在同一个库里，复用 `c1` 会得到
        # 409 IDEMPOTENCY_KEY_REUSED，而不是被测的那条分支。
        r = _post(client, {"music_take_id": take_id,
                           "selected_by": "director_1",
                           "selection_kind": "human"},
                  command_id="s1", path="/music-selections")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "selected"

    def test_selection_requires_decider(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        h.ingest(created["music_job_id"])
        take_id = h.conn.execute(
            "SELECT music_take_id FROM music_take").fetchone()["music_take_id"]
        r = _post(client, {"music_take_id": take_id}, command_id="s2",
                  path="/music-selections")
        assert r.status_code == 422
        assert r.json()["code"] == "SELECTED_BY_REQUIRED"

    def test_recompute_cut_plan_is_free(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        h.ingest(created["music_job_id"])
        submits = h.calls["submit"]
        r = client.post("/music-cues/cue_1/cut-plan:recompute",
                        params={"edl_revision": 9},
                        headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200, r.text
        assert r.json()["edl_revision"] == 9
        assert r.json()["cut_plans"]
        assert h.calls["submit"] == submits

    def test_cancel_and_retry_routes(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        job_id = _post(client, h.body()).json()["music_job_id"]
        r = _post(client, {}, command_id="x1", path=f"/music-jobs/{job_id}:cancel")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "CANCELED"
        # 已经终态，重试是 409（不是 500）。
        r = _post(client, {}, command_id="r1", path=f"/music-jobs/{job_id}:retry")
        assert r.status_code == 409
        assert r.json()["code"] == "JOB_NOT_RETRYABLE"

    def test_waive_gap_route(self, monkeypatch, tmp_path):
        client, h = _http_client(
            monkeypatch, tmp_path,
            rights={"sync_status": "unknown"})
        r = _post(client, h.body())
        assert r.json()["state"] == "POLICY_BLOCKED"
        gap_id = h.conn.execute("SELECT gap_id FROM music_gap").fetchone()[
            "gap_id"]
        # §6.3：S34 不豁免自己的缺口，必须带 S40 的裁决引用。
        r = _post(client, {"reason": "legal signed off"},
                  command_id="w0", path=f"/music-gaps/{gap_id}:waive")
        assert r.status_code == 422
        assert r.json()["code"] == "DECISION_REF_REQUIRED"
        r = _post(client, {"reason": "legal signed off",
                           "decision_ref": {"service_id": "S40",
                                            "id": "dec_1", "revision": 1}},
                  command_id="w1", path=f"/music-gaps/{gap_id}:waive")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "waived"

    def test_unconfigured_upstream_is_503_not_fake_success(self, monkeypatch,
                                                          tmp_path):
        # `app.py` 用 `os.environ.get`，环境里没这些变量时上游全是
        # `_unconfigured`。这条走的是**真实**的 deps 构造，不是 stub。
        for name in ("S02", "S06", "S23", "S25", "S26", "S28", "S41", "S42"):
            monkeypatch.delenv(f"{name}_BASE_URL", raising=False)
        monkeypatch.setenv("S34_DB", str(tmp_path / "s34_bare.db"))
        path = Path(__file__).resolve().parents[1] / "api" / "app.py"
        spec = importlib.util.spec_from_file_location("s34_api_bare", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = TestClient(module.app)
        r = client.post("/music-jobs", json={
            "context": {"tenant_id": "t1", "project_id": "p", "series_id": "s",
                        "episode_id": "e", "branch_id": "b", "node_id": "n",
                        "command_id": "c1", "request_hash": "e" * 64,
                        "schema_version": "2.0", "home_region": "cn-east",
                        "home_cell": "cell_a",
                        "manifest_ref": {"service_id": "S01", "id": "m",
                                         "revision": 1,
                                         "content_hash": "d" * 64},
                        "node_epoch": 4, "execution_fence": 2,
                        "memory_checkpoint": {"id": "ck", "revision": 1},
                        "policy_ref": {"service_id": "S02", "id": "p",
                                       "revision": 1,
                                       "content_hash": "a" * 64},
                        "actor": {"kind": "agent", "id": "a"},
                        "trace_id": "tr"},
            "cue": {"cue_id": "cue_1", "cue_revision": 1,
                    "scene_lineage_id": "scene_A", "cue_index": 0,
                    "mood": "tense", "mood_intensity": 0.7,
                    "instrumentation": "hybrid",
                    "music_intent": "dread", "instrumental_only": True},
            "duration_seconds": 42.0, "candidate_count": 1,
            "rights_policy_ref": dict(POLICY_REF),
            "budget_scope_id": "b1", "deadline": NOW_FUTURE,
        }, headers={"Idempotency-Key": "c1", "X-Tenant-Id": "t1"})
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "RIGHTS_RESOLVER_UNAVAILABLE"
        # 没有 job 被提交出去——不能有"先接受、后失败"的影子记录。
        assert module._conn.execute(
            "SELECT COUNT(*) AS n FROM music_job_candidate").fetchone()["n"] == 0

    def test_readiness_reports_contract_gaps(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/health/readiness")
        assert r.status_code == 200
        assert r.json()["ready"] is True
        gaps = r.json()["contract_gaps"]
        # 逐条列 id 而不是数个数：缺一条、重一条、编号跳号都会在这里炸。
        # `deps.build_scene_check` 的 docstring 指向 MUS-16，所以它必须真的
        # 存在——那条交叉引用曾经是悬空的。
        assert [g["id"] for g in gaps] == [f"MUS-{n}" for n in range(1, 17)]
        assert {g["kind"] for g in gaps} <= {"conflict", "needs_owner", "gap"}
        for gap in gaps:
            assert set(gap) == {"id", "kind", "summary", "handling"}
            assert gap["summary"].strip() and gap["handling"].strip()
