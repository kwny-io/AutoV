"""S37 测试。方案 §9 的 E24 / E25 / E26 与 I1–I3，加上通用验收。

两条主线：

1. **"没查成"永远不能变成 pass。** coverage 不足、评估器超时/报错、上游
   依赖不可达，每一种都必须落 `abstain`，`abstain.failed_check_ids` 非空，
   且 manifest 里对应项不是 `evaluated`。
2. **报告与事件要过契约。** `qc-report.v2` 与
   `quality.alignment-completed.v2` 都由 `jsonschema` 整份校验；注册表由
   仓库里的 `contracts/quality/validate_registry.py` 真跑一遍。

上游全部是闭包 stub：**不联网、不调真实模型、不调 TTS**。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
)

from audio_alignment import registry
from audio_alignment.schema import CONTRACT_GAPS
from audio_alignment.service import (
    AudioAlignmentService,
    ServiceError,
    stable_request_hash,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
SERVICES = ROOT / "services"

TENANT = "t1"
OTHER_TENANT = "t2"
HASH = "a" * 64
HASH2 = "d" * 64
HASH3 = "e" * 64

CHECK_ID_PATTERN = re.compile(r"^(TECH|VIS|AUD|SUB)\.[A-Z0-9_]+\.[A-Z0-9_]+$")
DEFECT_ID_PATTERN = re.compile(r"^df_[a-z0-9]{26}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{32}$")

# 本服务自己拥有的表。多一张、少一张都要在这里显式改——这是"没有第二份
# 权威"最便宜的可证伪断言。
OWNED_TABLES = {
    "alignment_unit", "asr_cache", "evaluator_call", "inbox", "outbox",
    "report", "report_check", "speaker_segment",
    "voice_authorization_projection",
}


# ---------------------------------------------------------------- 夹具

def command_body(**overrides):
    """一份"坐标齐全"的命令体。租户在 `execution_context` 里（平台签名面）。"""
    context = {
        "tenant_id": TENANT, "project_id": "p1", "series_id": "s1",
        "episode_id": "e1", "branch_id": "b1", "home_cell": "cell-a",
        # AGENTS.md：fence 是 run/branch 的，epoch 是单个 logical_node 的。
        # 这里故意给两个不同的数，混用会立刻露馅。
        "execution_fence": 3, "node_epoch": 7, "node_id": "n1",
        "trace_id": "tr-1",
        "policy_ref": {"id": "pol1", "revision": 2, "content_hash": "b" * 64},
        "manifest_ref": {"id": "man1", "revision": 4,
                         "content_hash": "c" * 64},
    }
    body = {
        "command_id": "cmd1",
        "execution_context": context,
        "run_id": "run1",
        "shot_lineage_id": "lin1",
        "artifact": {"service_id": "S28", "id": "clip1", "revision": 1,
                     "content_hash": HASH},
        "audio_source": {"mode": "embedded", "stream_index": 1,
                         "audio_track_hash": "track1"},
        "shot_spec_ref": {"service_id": "S06", "id": "shot1", "revision": 2},
        "dialogue": {
            "language": "zh-CN",
            "utterances": [{
                "utterance_id": "u1", "speaker_character_id": "char_a",
                "text": "你好呀", "text_revision": 2,
                "expected_duration_ms": 300,
            }],
        },
        "voice_binding_ref": {"service_id": "S15", "id": "voice_a",
                              "revision": 2},
        "checks": {"include": ["AUD.*"]},
    }
    body.update(overrides)
    return body


def asr_tokens():
    # CI 上界故意取 +0.118 而不是 +0.120：浮点减法下 0.12000000000000011 会
    # 越过 `> 120ms` 的边界，让一份干净样本自己被判 LOW_CONFIDENCE。
    return [
        {"text": "你", "start_seconds": 1.00, "end_seconds": 1.10,
         "posterior": 0.95, "alignment_confidence": 0.9,
         "ci_low_seconds": 1.00, "ci_high_seconds": 1.118,
         "script_index": 0, "speaker_id": "char_a", "phonemes": ["n", "i"]},
        {"text": "好", "start_seconds": 1.10, "end_seconds": 1.20,
         "posterior": 0.93, "alignment_confidence": 0.88,
         "ci_low_seconds": 1.10, "ci_high_seconds": 1.218,
         "script_index": 1, "speaker_id": "char_a", "phonemes": ["x", "a", "o"]},
        {"text": "呀", "start_seconds": 1.20, "end_seconds": 1.30,
         "posterior": 0.92, "alignment_confidence": 0.9,
         "ci_low_seconds": 1.20, "ci_high_seconds": 1.318,
         "script_index": 2, "speaker_id": "char_a", "phonemes": ["y", "a"]},
    ]


def analysis_document():
    """S26 understanding job 的分析文档（S37-5：本服务自定的协议）。"""
    return {
        "analysis_version": "s37-analysis.v1",
        "audio": {"analyzed": True, "analyzed_seconds": 4.0,
                  "duration_seconds": 4.0, "track_hash": "track1",
                  "stream_index": 1, "silent": False, "damaged": False,
                  "codec": "aac", "sample_rate_hz": 48000, "channels": 2},
        "video": {"present": True, "is_vfr": False, "fps_num": 24,
                  "fps_den": 1, "frame_count": 96, "frames_evaluated": 96,
                  "planned_frames": 96, "face_present": True,
                  "width": 1920, "height": 1080, "codec": "h264",
                  "pix_fmt": "yuv420p", "color_range": "tv"},
        "asr": {
            "outcome": "ok", "latency_ms": 900,
            "model_snapshot": {"provider": "mock", "model_id": "asr-x",
                               "snapshot": "2026-01"},
            "tokens_in": 120, "tokens_out": 40,
            "utterances": [{
                "utterance_id": "u1", "confidence": 0.95,
                "script_tokens": [
                    {"index": 0, "text": "你", "char_offset": 0,
                     "char_length": 1, "pause_after": False,
                     "phonemes": ["n", "i"]},
                    {"index": 1, "text": "好", "char_offset": 1,
                     "char_length": 1, "pause_after": False,
                     "phonemes": ["x", "a", "o"]},
                    {"index": 2, "text": "呀", "char_offset": 2,
                     "char_length": 1, "pause_after": False,
                     "phonemes": ["y", "a"]},
                ],
                "tokens": asr_tokens(),
            }],
        },
        "prosody": {"stress_match": 0.8, "confidence": 0.9,
                    "expected_emotion": "calm"},
        "speaker_segments": [{"speaker_id": "char_a", "start_seconds": 1.0,
                              "end_seconds": 1.3, "confidence": 0.9}],
        "lipsync": {"outcome": "ok", "confidence": 0.9, "offset_ms": 40.0,
                    "openness_correlation": 0.6,
                    "silent_mouth_motion": [], "speaks_closed": []},
        "voice": {"confidence": 0.9, "embedding_distance": 0.08,
                  "window_distances": [{"window_index": 0,
                                        "start_seconds": 1.0,
                                        "end_seconds": 1.3,
                                        "distance": 0.08}],
                  "cross_shot_distance": 0.05, "emotion_match": 0.8,
                  "expected_emotion": "calm", "detected_emotion": "calm"},
        "alignment": {"model_version": "v1"},
    }


class Harness:
    """内存库 + 五个 stub 上游。全部是闭包，**不碰网络**。"""

    def __init__(self, *, analysis=None, stored_hash=None, artifact_error=None,
                 analysis_error=None, analysis_payload=None, shot=None,
                 lexicon=None, voice=None, enabled=True, checks=None):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.analysis_doc = analysis if analysis is not None else \
            analysis_document()
        self.analysis_payload = analysis_payload
        self.stored_hash = stored_hash
        self.artifact_error = artifact_error
        self.analysis_error = analysis_error
        self.shot = shot if shot is not None else {
            "id": "shot1", "revision": 2, "dialogue_duration_ms": 300}
        self.lexicon = lexicon if lexicon is not None else {
            "id": "voice_a", "revision": 2, "lexicon": {"你": "ni"}}
        self.voice = voice if voice is not None else {
            "state": "active", "effective_state": "active", "state_seq": 3}
        self.calls = {"artifact": 0, "analysis": 0, "shot": 0, "lexicon": 0,
                      "voice": 0}
        self.analysis_requests = []
        self.service = AudioAlignmentService(
            self.conn,
            artifact_source=self._artifact,
            analysis_source=self._analysis,
            shot_source=self._shot,
            lexicon_source=self._lexicon,
            voice_source=self._voice,
            enabled=enabled,
            enabled_checks=tuple(registry.CHECK_IDS)
            if checks is None else checks,
        )
        # 必须在任何 check() 之前构造：内核的 `_command` 会 UPDATE 幂等占位。
        self._idem = SqliteIdempotencyStore(self.conn)

    # ---- stubs
    def _artifact(self, tenant_id, ref):
        self.calls["artifact"] += 1
        if self.artifact_error is not None:
            raise self.artifact_error
        # 返回的是**库里那一份**的 hash。回显请求值会让 HASH_MISMATCH 永远
        # 看不见差异；要测不一致时把 `stored_hash` 换掉，而不是改请求。
        return {"id": ref["id"], "revision": ref.get("revision", 1),
                "content_hash": self.stored_hash or ref["content_hash"],
                "audio_track_hash": "track1"}

    def _analysis(self, tenant_id, request):
        self.calls["analysis"] += 1
        self.analysis_requests.append(request)
        if self.analysis_error is not None:
            raise self.analysis_error
        if self.analysis_payload is not None:
            return self.analysis_payload
        return self.analysis_doc

    def _shot(self, tenant_id, ref, context):
        self.calls["shot"] += 1
        return self.shot

    def _lexicon(self, tenant_id, ref):
        self.calls["lexicon"] += 1
        return self.lexicon

    def _voice(self, tenant_id, ref):
        self.calls["voice"] += 1
        # 传进来的是 callable 时按"上游行为"调用它（用它模拟不可达）。
        if callable(self.voice):
            return self.voice(tenant_id, ref)
        return self.voice

    # ---- 命令（与 api/app.py 的 command() 同形）
    def command(self, body=None, *, command_id="cmd1", tenant_id=TENANT):
        body = body if body is not None else command_body()
        request_hash = stable_request_hash(body)
        record = self._idem.begin(tenant_id, command_id, request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = self.service.check(tenant_id, command_id, body,
                                        request_hash)
        except Exception:
            self._idem.abort(tenant_id, command_id)
            raise
        self._idem.finish(tenant_id, command_id,
                          json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ---- 断言助手
    def report(self, report_id=None, generation="latest", tenant_id=TENANT):
        rid = report_id or self.conn.execute(
            "SELECT report_id FROM report").fetchone()[0]
        return self.service.get_report(tenant_id, rid, generation)

    def payload(self, **kw):
        return self.report(**kw)["payload"]

    def manifest(self, **kw):
        return {entry["check_id"]: entry
                for entry in self.payload(**kw)["check_manifest"]}

    def defects(self, **kw):
        return self.payload(**kw)["defects"]

    def defect(self, check_id, **kw):
        found = [d for d in self.defects(**kw) if d["check_id"] == check_id]
        assert len(found) == 1, (check_id, found)
        return found[0]

    def events(self):
        """outbox 由平台 SDK 建表，**没有 `id` 列**，列名是 `sequence`
        （envelope JSON 里也叫 `sequence`；没有 `aggregate_sequence`）。"""
        rows = self.conn.execute(
            "SELECT * FROM outbox ORDER BY created_at, sequence").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            envelope = json.loads(item.pop("payload"))
            out.append({**envelope, "event_type": item["event_type"],
                        "outbox_sequence": item["sequence"]})
        return out

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- 契约校验

def _load(relative):
    with open(CONTRACTS / relative, encoding="utf-8") as handle:
        return json.load(handle)


def validate_report(payload):
    jsonschema = pytest.importorskip("jsonschema")
    schema = _load("quality/qc-report.v2.schema.json")
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(payload))
    assert errors == [], [(list(e.absolute_path), e.message) for e in errors]


def validate_event(envelope):
    jsonschema = pytest.importorskip("jsonschema")
    schema = _load("quality.alignment-completed.v2.schema.json")
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(
        envelope["data"]))
    assert errors == [], [(list(e.absolute_path), e.message) for e in errors]
    # envelope 本身：事件名、来源、聚合标识必须自洽。
    assert envelope["type"] == "quality.alignment.completed"
    assert envelope["source"] == "S37"
    assert envelope["subject"] == envelope["data"]["shot_id"]
    assert envelope["aggregate_id"] == envelope["data"]["report_id"]
    assert envelope["sequence"] == envelope["data"]["report_generation"]
    assert envelope["data_ref"] == {
        "service_id": "S37", "id": envelope["data"]["report_id"],
        "revision": envelope["data"]["report_generation"],
        "content_hash": envelope["data_ref"]["content_hash"],
    }


# ================================================================ 基线

def test_clean_shot_passes_and_both_contracts_accept_it():
    """干净样本：20 项全查、0 缺陷、pass，报告与事件都过契约。"""
    harness = Harness()
    result = harness.command()
    payload = harness.payload()

    assert result["status"] == "accepted"
    assert result["replayed"] is False
    assert result["report_id"].startswith("qc_")
    assert payload["decision"] == "pass", payload["decision_reason"]
    assert payload["check_manifest"], "an empty manifest reads as a pass"
    assert len(payload["check_manifest"]) == len(registry.CHECK_IDS)
    assert all(entry["status"] == "evaluated"
               for entry in payload["check_manifest"])
    assert payload["defects"] == []
    assert payload["abstain"] is None
    assert payload["report_content_hash"] == result["content_hash"]
    assert payload["coverage"]["coverage_ratio"] == 1.0
    assert payload["coverage"]["planned_windows"] == 96
    validate_report(payload)

    events = harness.events()
    assert len(events) == 1
    validate_event(events[0])
    assert events[0]["data"]["decision"] == "pass"
    assert events[0]["data"]["defect_count"] == 0
    assert events[0]["data"]["max_lipsync_offset_ms"] == 40


def test_every_check_id_conforms_to_the_report_pattern():
    for check_id in registry.CHECK_IDS:
        assert CHECK_ID_PATTERN.match(check_id), check_id


# ================================================================ E24 口型与字幕

def test_E24_A1_lipsync_offset_150ms_is_a_blocker_covering_the_line():
    """E24-A1：150ms → blocker，measured 是实测值，区间覆盖整句对白。"""
    harness = Harness()
    document = analysis_document()
    document["lipsync"]["offset_ms"] = 150.0
    harness.analysis_doc = document
    harness.command()

    defect = harness.defect("AUD.LIPSYNC.OFFSET")
    assert defect["severity"] == "blocker"
    assert defect["type"] == "lipsync"
    assert defect["scope"] == "segment"
    assert defect["measured"] == {"value": 150.0, "unit": "ms",
                                  "threshold": 120.0, "comparator": "gt"}
    # 全对白段：tokens 1.00–1.30。
    assert defect["time_range"]["start_seconds"] == pytest.approx(1.0)
    assert defect["time_range"]["end_seconds"] == pytest.approx(1.3)
    assert defect["recommended_actions"] == ["redo_lipsync"]
    assert DEFECT_ID_PATTERN.match(defect["defect_id"])
    assert FINGERPRINT_PATTERN.match(defect["defect_fingerprint"])
    assert harness.payload()["decision"] == "repair"


def test_E24_A1_known_container_offset_is_subtracted_not_reported_twice():
    """S35 已报过的容器偏移先扣掉：150 − 8 = 142，不是再报一次 150。"""
    harness = Harness()
    document = analysis_document()
    document["lipsync"]["offset_ms"] = 150.0
    harness.analysis_doc = document
    harness.command(command_body(known_audio_offset_seconds=0.008))

    defect = harness.defect("AUD.LIPSYNC.OFFSET")
    assert defect["measured"]["value"] == pytest.approx(142.0)
    assert defect["time_range"]["start_seconds"] == pytest.approx(0.992)
    assert defect["time_range"]["end_seconds"] == pytest.approx(1.292)


def test_E24_A2_lipsync_offset_80ms_is_major_not_blocker():
    harness = Harness()
    document = analysis_document()
    document["lipsync"]["offset_ms"] = 80.0
    harness.analysis_doc = document
    harness.command()

    defect = harness.defect("AUD.LIPSYNC.OFFSET")
    assert defect["severity"] == "major"
    assert defect["measured"]["threshold"] == 60.0
    # major 没有自动路线 → 人审，而不是静默修复。
    assert harness.payload()["decision"] == "human_review"


def test_E24_A3_speaks_with_closed_mouth_is_a_blocker():
    harness = Harness()
    document = analysis_document()
    document["lipsync"]["speaks_closed"] = [
        {"start_seconds": 1.0, "end_seconds": 1.3}]
    harness.analysis_doc = document
    harness.command()

    defect = harness.defect("AUD.LIPSYNC.SPEAKS_CLOSED")
    assert defect["severity"] == "blocker"
    assert defect["time_range"]["start_seconds"] == pytest.approx(1.0)
    assert harness.payload()["decision"] == "repair"


def test_E24_A4_mouth_moves_while_silent_is_major():
    harness = Harness()
    document = analysis_document()
    document["lipsync"]["silent_mouth_motion"] = [
        {"start_seconds": 0.2, "end_seconds": 0.6}]
    harness.analysis_doc = document
    harness.command()

    defect = harness.defect("AUD.LIPSYNC.MOUTH_MOVES_SILENT")
    assert defect["severity"] == "major"
    assert defect["time_range"]["start_seconds"] == pytest.approx(0.2)
    assert harness.payload()["decision"] == "human_review"


def test_E24_A5_missing_word_points_at_the_word_with_asr_evidence():
    harness = Harness()
    document = analysis_document()
    document["asr"]["utterances"][0]["tokens"].pop(1)
    harness.analysis_doc = document
    harness.command()

    defect = harness.defect("AUD.TEXT.MISSING_WORD")
    assert defect["severity"] == "major"
    # 区间精确到那个词（第 2 个字：1.10–1.20），不是整句。
    assert defect["time_range"]["start_seconds"] == pytest.approx(1.1)
    assert defect["time_range"]["end_seconds"] == pytest.approx(1.2)
    assert defect["evidence_artifact_ids"] == ["clip1"]
    assert "utterance u1" in defect["observed"]
    assert harness.payload()["decision"] == "human_review"


def test_E24_A6_mispronounced_char_links_the_S15_lexicon_revision():
    harness = Harness()
    document = analysis_document()
    document["asr"]["utterances"][0]["tokens"][1]["phonemes"] = ["h", "a", "o"]
    harness.analysis_doc = document
    harness.command()

    defect = harness.defect("AUD.TEXT.MISPRONOUNCE")
    assert defect["severity"] == "major"
    # 基准是 S15 的词典，不是模型自己说的。
    assert defect["expected_source"]["service_id"] == "S15"
    assert defect["expected_source"]["ref_id"] == "voice_a"
    assert defect["expected_source"]["ref_revision"] == 2
    assert harness.count("report") >= 1


def test_E24_A7_dialogue_longer_than_the_shot_is_major_and_human_reviewed():
    harness = Harness()
    harness.command(command_body(dialogue={
        "language": "zh-CN",
        "utterances": [{"utterance_id": "u1",
                        "speaker_character_id": "char_a", "text": "你好呀",
                        "text_revision": 2, "expected_duration_ms": 1100}],
    }))

    defect = harness.defect("AUD.ALIGN.DURATION_VS_SPEC")
    assert defect["severity"] == "major"
    assert defect["measured"]["value"] == pytest.approx(0.8)
    assert defect["measured"]["unit"] == "s"
    assert defect["expected_source"]["service_id"] == "S06"
    assert harness.payload()["decision"] == "human_review"
    # 修复是创作决定：动作链里没有"自动改时长"。
    assert "temporal_edit" in defect["recommended_actions"]


def test_E24_A8_sixty_percent_coverage_abstains():
    harness = Harness()
    document = analysis_document()
    document["video"].update({"frame_count": 100, "planned_frames": 100,
                              "frames_evaluated": 60})
    harness.analysis_doc = document
    harness.command()

    payload = harness.payload()
    assert payload["decision"] == "abstain"
    assert payload["coverage"]["coverage_ratio"] == pytest.approx(0.60)
    assert payload["coverage"]["required_coverage_min"] == pytest.approx(0.95)
    assert payload["abstain"]["reason_code"] == "incomplete_coverage"
    assert "0.600" in payload["abstain"]["detail"]
    # 覆盖不足时**全部**检查都算"没查成"：逐帧只看了 60%，口型结论不可采信，
    # 而同一条报告里其它结论也就不该单独放行。
    assert payload["abstain"]["failed_check_ids"] == sorted(registry.CHECK_IDS)
    assert payload["decision_reason"].startswith("incomplete_coverage:")
    validate_report(payload)


def test_E24_A9_vfr_is_refused_not_computed():
    harness = Harness()
    document = analysis_document()
    document["video"]["is_vfr"] = True
    harness.analysis_doc = document
    harness.command()

    payload = harness.payload()
    assert payload["decision"] == "abstain"
    assert payload["abstain"]["reason_code"] == "evaluator_error"
    lipsync = [entry for entry in payload["check_manifest"]
               if entry["check_id"] in registry.NEEDS_VIDEO_FACE]
    assert len(lipsync) == 4
    assert all(entry["status"] == "failed"
               and entry["reason_code"] == "evaluator_error"
               for entry in lipsync)
    vfr = [d for d in payload["dimensions"] if d["name"] == "vfr_unsupported"]
    assert len(vfr) == 1 and vfr[0]["score"] is None


def test_E24_A10_no_dialogue_is_an_explicit_noop_pass():
    harness = Harness()
    harness.command(command_body(dialogue={"language": "zh-CN",
                                           "utterances": []}))

    payload = harness.payload()
    assert payload["decision"] == "pass"
    assert payload["abstain"] is None
    assert payload["defects"] == []
    assert payload["coverage"]["audio_analyzed"] is False
    assert payload["coverage"]["coverage_ratio"] == 0.0
    # 全部 not_applicable(no_dialogue)——不是 evaluated，也不是 not_evaluated。
    for entry in payload["check_manifest"]:
        assert entry["status"] == "not_applicable", entry
        assert entry["reason_code"] == "no_dialogue", entry


def test_E24_A11_repair_recheck_passes_with_a_fresh_report_and_units():
    harness = Harness()
    bad = analysis_document()
    bad["lipsync"]["offset_ms"] = 150.0
    harness.analysis_doc = bad
    first = harness.command(command_id="cmd1")
    fingerprint = harness.defect("AUD.LIPSYNC.OFFSET")["defect_fingerprint"]
    assert first["decision"] == "repair"

    harness.analysis_doc = analysis_document()
    repaired = harness.command(command_body(
        command_id="cmd2",
        artifact={"service_id": "S28", "id": "clip1", "revision": 2,
                  "content_hash": HASH3},
        repair_context={"repair_plan_id": "rp_1",
                        "attempt_slot_id": "slot_2", "attempt_seq": 1,
                        "defect_fingerprints": [fingerprint]},
    ), command_id="cmd2")

    payload = harness.payload(report_id=repaired["report_id"])
    assert payload["decision"] == "pass"
    assert payload["repair_context"]["attempt_slot_id"] == "slot_2"
    assert payload["repair_context"]["defect_fingerprints"] == [fingerprint]
    assert repaired["report_id"] != first["report_id"]
    # 时间码重新生成，不是复用上一份。
    units = harness.service.get_units(TENANT, repaired["report_id"])
    assert len(units["units"]) == 3
    assert units["time_base"] == "presentation_pts"


# ================================================================ E25 评估器

def test_E25_A1_asr_timeout_abstains_and_records_the_call():
    harness = Harness()
    document = analysis_document()
    document["asr"]["outcome"] = "timeout"
    harness.analysis_doc = document
    harness.command()

    payload = harness.payload()
    assert payload["decision"] == "abstain"
    assert payload["abstain"]["reason_code"] == "evaluator_timeout"
    failed = {entry["check_id"] for entry in payload["check_manifest"]
              if entry["status"] == "failed"}
    assert failed == {c for c in registry.CHECK_IDS if c.startswith("AUD.TEXT.")
                      or c.startswith("AUD.ALIGN.")}
    outcomes = {row["outcome"] for row in harness.conn.execute(
        "SELECT outcome FROM evaluator_call")}
    assert "timeout" in outcomes


def test_E25_A2_all_tokens_below_minimum_confidence_fails_the_check():
    harness = Harness()
    document = analysis_document()
    for token in document["asr"]["utterances"][0]["tokens"]:
        token["alignment_confidence"] = 0.2
    harness.analysis_doc = document
    harness.command()

    payload = harness.payload()
    assert payload["decision"] == "abstain"
    assert payload["abstain"]["reason_code"] == "evaluator_error"
    assert "AUD.TEXT.MISPRONOUNCE" in payload["abstain"]["failed_check_ids"]
    # 低置信度的字不参与判定：不能据此产 defect。
    assert all(d["check_id"] != "AUD.TEXT.MISPRONOUNCE"
               for d in payload["defects"])


def test_E25_A3_script_conflict_is_an_observation_not_a_rewrite():
    harness = Harness()
    document = analysis_document()
    document["asr"]["utterances"][0]["tokens"][0]["text"] = "泥"
    harness.analysis_doc = document
    harness.command()

    payload = harness.payload()
    # 只记观测维度；不产 SCRIPT_CONFLICT 缺陷，也不给"改剧本"的建议。
    conflict = [d for d in payload["dimensions"]
                if d["name"] == "text_conflict"]
    assert len(conflict) == 1
    assert conflict[0]["score"] is None
    assert all(d["check_id"] != "AUD.TEXT.SCRIPT_CONFLICT"
               for d in payload["defects"])
    for defect in payload["defects"]:
        assert "rewrite_script" not in defect["recommended_actions"]
        assert "edit_script" not in defect["recommended_actions"]
    assert "AUD.TEXT.SCRIPT_CONFLICT" in registry.OBSERVATION_ONLY


def test_E25_A4_S15_unreachable_abstains_on_voice_checks():
    def down(*_args):
        raise ConnectionError("S15 down")

    harness = Harness(voice=down)
    harness.command()

    payload = harness.payload()
    assert payload["decision"] == "abstain"
    assert payload["abstain"]["reason_code"] == "dependency_stale"
    voice_entries = [entry for entry in payload["check_manifest"]
                     if entry["check_id"] in registry.NEEDS_VOICE_AUTHORITY
                     and entry["check_id"].startswith("AUD.VOICE.")]
    assert len(voice_entries) == 4
    assert all(entry["status"] == "not_evaluated"
               and entry["reason_code"] == "dependency_unavailable"
               for entry in voice_entries)


# ================================================================ E26 跨 Run

def test_E26_A1_reruns_keep_the_lineage_and_the_defect_fingerprint():
    harness = Harness()
    bad = analysis_document()
    bad["lipsync"]["offset_ms"] = 150.0
    harness.analysis_doc = bad
    first = harness.command(command_id="cmd1")
    first_defect = harness.defect("AUD.LIPSYNC.OFFSET")
    first_payload = harness.payload(report_id=first["report_id"])

    second = harness.command(command_body(
        command_id="cmd2",
        artifact={"service_id": "S28", "id": "clip1", "revision": 2,
                  "content_hash": HASH2},
    ), command_id="cmd2")
    second_defect = harness.defect(report_id=second["report_id"],
                                   check_id="AUD.LIPSYNC.OFFSET")
    second_payload = harness.payload(report_id=second["report_id"])

    assert (first_payload["context"]["shot_lineage_id"]
            == second_payload["context"]["shot_lineage_id"] == "lin1")
    assert first["report_id"] != second["report_id"]
    # 跨 Run 的身份是 fingerprint，不是按报告序号分配的 defect_id。
    assert first_defect["defect_fingerprint"] == \
        second_defect["defect_fingerprint"]


def test_E26_A2_unchanged_audio_track_reuses_the_asr_cache():
    harness = Harness()
    first = harness.command(command_id="cmd1")
    assert harness.analysis_requests[0]["cached_asr"] is None
    assert harness.count("asr_cache") == 1

    harness.command(command_body(command_id="cmd2"), command_id="cmd2")
    # 第二次带上了上一份 ASR：同一音轨不重复计费，也不再调 ASR。
    assert harness.analysis_requests[1]["cached_asr"] is not None
    assert harness.count("asr_cache") == 1
    assert first["report_id"] == harness.payload()["report_id"]


# ================================================================ 独立性

def test_I1_S36_registers_no_lipsync_check_and_S37_owns_the_aud_family():
    from semantic_qc import registry as semantic_registry

    s36 = semantic_registry.registry_document()["checks"]
    assert s36, "the S36 registry must actually load"
    assert all(not check["check_id"].startswith("AUD.")
               for check in s36)
    assert all(check["defect_type"] != "lipsync" for check in s36)
    aud_lipsync = [c for c in registry.CHECK_IDS if c.startswith("AUD.LIPSYNC.")]
    assert len(aud_lipsync) == 5
    assert all(registry.BY_ID[c]["owner_service"] == "S37"
               for c in registry.CHECK_IDS)


def test_I2_timecodes_are_served_only_by_the_units_endpoint():
    harness = Harness()
    result = harness.command()
    units = harness.service.get_units(TENANT, result["report_id"])

    assert set(units) == {"report_id", "report_generation", "time_base",
                          "units", "speaker_segments"}
    assert units["time_base"] == "presentation_pts"
    assert [u["index"] for u in units["units"]] == [0, 1, 2]
    assert units["units"][0]["start"] == pytest.approx(1.0)
    assert units["units"][0]["ci_high"] == pytest.approx(1.118)
    assert units["units"][1]["char_offset"] == 1
    # 按时间窗过滤只裁窗口，不重新编号。
    window = harness.service.get_units(TENANT, result["report_id"],
                                       start=1.15, end=2.0)
    assert [u["index"] for u in window["units"]] == [1, 2]
    # S38 能拿到时间码，但拿不到别人的媒体，也不需要读 S37 的库：
    # 端点返回的就是全部对外事实。
    assert not any("path" in unit or "url" in unit
                   for unit in units["units"])


def test_I3_revoked_voice_license_is_human_review_only():
    harness = Harness(voice={"state": "revoked", "effective_state": "revoked",
                             "state_seq": 4})
    harness.command()

    payload = harness.payload()
    defect = harness.defect("AUD.VOICE.AUTHORIZATION")
    assert defect["severity"] == "blocker"
    assert defect["recommended_actions"] == ["human_review"]
    assert payload["decision"] == "human_review"
    # 授权是 S15 的事实，S37 只记录观测。
    assert defect["expected_source"]["service_id"] == "S15"
    assert registry.BY_ID["AUD.VOICE.AUTHORIZATION"]["never_auto_repair"]


# ================================================================ 幂等

def test_replay_returns_the_stored_result_without_remeasuring():
    harness = Harness()
    first = harness.command()
    calls = dict(harness.calls)
    second = harness.command()

    assert second["replayed"] is True
    assert second["report_id"] == first["report_id"]
    assert second["content_hash"] == first["content_hash"]
    assert harness.calls == calls
    assert harness.count("report") == 1
    assert len(harness.events()) == 1


def test_same_key_different_body_is_a_conflict_not_an_overwrite():
    harness = Harness()
    harness.command()
    changed = command_body()
    changed["dialogue"]["utterances"][0]["text"] = "你好啊"

    with pytest.raises(CommandConflict) as caught:
        harness.command(changed)
    assert caught.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert caught.value.stored_hash != caught.value.incoming_hash
    # 旧的那一份还在，没被覆盖。
    assert harness.payload()["decision"] == "pass"
    assert harness.count("report") == 1


def test_volatile_trace_fields_do_not_change_the_request_hash():
    """同一个业务请求换了 trace 必须仍是重放，不是"同键不同 body"。"""
    harness = Harness()
    first = harness.command()
    body = command_body()
    body["execution_context"]["trace_id"] = "tr-2"
    second = harness.command(body)
    assert second["replayed"] is True
    assert second["report_id"] == first["report_id"]


def test_in_flight_command_is_reported_as_such():
    harness = Harness()
    body = command_body()
    harness._idem.begin(TENANT, "cmd1", stable_request_hash(body))
    with pytest.raises(CommandInProgress):
        harness._idem.begin(TENANT, "cmd1", stable_request_hash(body))


def test_command_ids_are_scoped_per_tenant():
    harness = Harness()
    body = command_body()
    harness.command(body, command_id="cmd1", tenant_id=TENANT)
    # 另一个租户用同一个 command_id 不受影响（键是 tenant+command）。
    other = dict(body)
    other["execution_context"] = dict(body["execution_context"],
                                      tenant_id=OTHER_TENANT)
    result = harness.command(other, command_id="cmd1", tenant_id=OTHER_TENANT)
    assert result["replayed"] is False


# ================================================================ 拒绝路径

def test_disabled_service_refuses_to_emit_a_report():
    harness = Harness(enabled=False)
    with pytest.raises(ServiceError) as caught:
        harness.command()
    assert caught.value.status == 503
    assert caught.value.code == "SERVICE_DISABLED"
    assert harness.count("report") == 0


def test_empty_check_selection_refuses_to_emit_an_empty_manifest():
    harness = Harness(checks=())
    with pytest.raises(ServiceError) as caught:
        harness.command()
    assert caught.value.status == 422
    assert caught.value.code == "NO_CHECKS_ENABLED"
    assert harness.count("report") == 0


def test_unknown_check_pattern_is_rejected_not_silently_skipped():
    harness = Harness()
    with pytest.raises(ServiceError) as caught:
        harness.command(command_body(checks={"include": ["XYZ.*"]}))
    assert caught.value.status == 422
    assert caught.value.code == "INVALID_CHECKS"
    assert "matches no check" in str(caught.value)


def test_missing_report_coordinates_are_named():
    harness = Harness()
    body = command_body()
    del body["execution_context"]["home_cell"]
    with pytest.raises(ServiceError) as caught:
        harness.command(body)
    assert caught.value.status == 422
    assert caught.value.code == "INVALID_ALIGNMENT_JOB"
    assert "home_cell" in str(caught.value)


def test_body_tenant_must_match_the_signed_tenant():
    harness = Harness()
    body = command_body()
    body["execution_context"]["tenant_id"] = OTHER_TENANT
    with pytest.raises(ServiceError) as caught:
        harness.command(body, tenant_id=TENANT)
    assert caught.value.status == 403
    assert caught.value.code == "TENANT_MISMATCH"
    assert harness.count("report") == 0


def test_cross_tenant_reads_are_404_not_403_with_content():
    harness = Harness()
    result = harness.command()
    with pytest.raises(ServiceError) as report_error:
        harness.service.get_report(OTHER_TENANT, result["report_id"])
    assert report_error.value.status == 404
    assert report_error.value.code == "REPORT_NOT_FOUND"
    with pytest.raises(ServiceError) as units_error:
        harness.service.get_units(OTHER_TENANT, result["report_id"])
    assert units_error.value.status == 404


def test_unreadable_artifact_is_503_and_a_hash_mismatch_is_409():
    with pytest.raises(ServiceError) as unavailable:
        Harness(artifact_error=ConnectionError("S28 down")).command()
    assert unavailable.value.status == 503
    assert unavailable.value.code == "ARTIFACT_SOURCE_UNAVAILABLE"

    with pytest.raises(ServiceError) as mismatch:
        Harness(stored_hash=HASH2).command()
    assert mismatch.value.status == 409
    assert mismatch.value.code == "ARTIFACT_HASH_MISMATCH"


def test_invalid_analysis_document_is_502_not_a_report():
    with pytest.raises(ServiceError) as wrong_version:
        Harness(analysis_payload={"analysis_version": "v0"}).command()
    assert wrong_version.value.status == 502
    assert wrong_version.value.code == "UPSTREAM_INVALID"

    with pytest.raises(ServiceError) as unreachable:
        Harness(analysis_error=ConnectionError("S26 down")).command()
    assert unreachable.value.status == 503
    assert unreachable.value.code == "ANALYSIS_SOURCE_UNAVAILABLE"


def test_silent_audio_abstains_instead_of_passing():
    harness = Harness()
    document = analysis_document()
    document["audio"]["silent"] = True
    harness.analysis_doc = document
    harness.command()

    payload = harness.payload()
    assert payload["decision"] == "abstain"
    assert payload["abstain"]["reason_code"] == "asset_unavailable"
    assert all(entry["status"] == "not_evaluated"
               for entry in payload["check_manifest"])


def test_missing_face_is_not_applicable_not_a_failure():
    harness = Harness()
    document = analysis_document()
    document["video"]["face_present"] = False
    harness.analysis_doc = document
    harness.command()

    payload = harness.payload()
    assert payload["decision"] == "pass"
    for check_id in registry.NEEDS_VIDEO_FACE | {"AUD.LIPSYNC.FACE_NOT_FOUND"}:
        entry = {e["check_id"]: e for e in payload["check_manifest"]}[check_id]
        assert entry["status"] == "not_applicable"
        assert entry["reason_code"] == "no_face"


def test_external_audio_skips_lipsync_as_no_such_content():
    harness = Harness()
    harness.command(command_body(
        audio_source={"mode": "external", "audio_track_hash": "track1"}))
    payload = harness.payload()
    assert payload["decision"] == "pass"
    for check_id in registry.NEEDS_VIDEO_FACE:
        entry = {e["check_id"]: e for e in payload["check_manifest"]}[check_id]
        assert entry["status"] == "not_applicable"
        assert entry["reason_code"] == "no_such_content"


# ================================================================ 版本与世系

def test_second_run_on_the_same_bytes_supersedes_generation_one():
    harness = Harness()
    first = harness.command(command_id="cmd1")
    second = harness.command(command_body(command_id="cmd2"),
                             command_id="cmd2")

    assert second["report_id"] == first["report_id"]
    latest = harness.payload(generation="latest")
    assert latest["report_generation"] == 2
    assert latest["supersedes"] == {"report_id": first["report_id"],
                                    "report_generation": 1}
    assert harness.payload(generation=1)["report_generation"] == 1
    assert [event["sequence"] for event in harness.events()] == [1, 2]


def test_node_epoch_and_execution_fence_are_separate_columns():
    """AGENTS.md：两者作用域不同，不能合并、不能互相推进。"""
    harness = Harness()
    harness.command()

    row = harness.conn.execute(
        "SELECT node_epoch, execution_fence FROM report").fetchone()
    assert (row["node_epoch"], row["execution_fence"]) == (7, 3)
    payload = harness.payload()
    assert payload["shot_ref"]["node_epoch"] == 7
    assert payload["context"]["execution_fence"] == 3
    event = harness.events()[0]
    assert event["data"]["node_epoch"] == 7
    assert event["data"]["execution_fence"] == 3


def test_service_has_no_acceptance_pointer_to_advance():
    """S37 只报告：它不掌握任何接受指针，旧 epoch 的结果无从"推进"。"""
    public = [name for name in dir(AudioAlignmentService)
              if not name.startswith("__")]
    assert not [name for name in public
                if "accept" in name or "pointer" in name]
    harness = Harness()
    harness.command()
    assert not [name for name in
                (row[1] for row in harness.conn.execute(
                    "PRAGMA table_info(report)"))
                if "accept" in name]


def test_service_does_not_repair_or_write_artifacts():
    """S37 的写路径只有报告：没有修媒体、也没有改稿的入口。"""
    public = [name for name in dir(AudioAlignmentService)
              if not name.startswith("_")]
    forbidden = ("repair", "rewrite", "regenerate", "upload", "write_artifact",
                 "update_script", "apply_fix")
    assert not [name for name in public
                if any(word in name for word in forbidden)]
    # 落库的一侧同样只有读上游、写自己：报告是唯一产物。
    harness = Harness()
    harness.command()
    tables = {row["name"] for row in harness.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not [name for name in tables
                if any(word in name for word in ("artifact", "media",
                                                 "script", "asset"))]
    assert harness.events()[0]["data_ref"]["service_id"] == "S37"


# ================================================================ 数据库约束

_REPORT_ROW = dict(
    report_id="qc_x", report_generation=1, derived_key="f" * 64,
    tenant_id="tnt_x", project_id="p", series_id="s", episode_id="e",
    branch_id="b", shot_id="sh", shot_lineage_id="ln",
    logical_node_id="nd", node_epoch=1, manifest_id="m",
    manifest_revision=1, run_id="r", execution_fence=1,
    artifact_id="a", artifact_sha256="a" * 64, audio_track_hash="tr",
    artifact_kind="video", language="zh-CN", asr_model_snapshot="{}",
    align_model_version="1", lexicon_revision=None, voice_binding_ref=None,
    evaluator_version="1", policy_version="1", sampling_version="1",
    decision="pass", decision_reason="why", confidence=1.0,
    coverage_ratio=1.0, required_coverage_min=0.9, defect_count=0,
    major_count=0, blocker_count=0, failed_check_count=0,
    attempt_slot_id=None, supersedes_report_id=None,
    supersedes_generation=None, payload="{}", content_hash="0" * 64,
    created_at="2026-01-01T00:00:00.000Z",
)


def _insert_report(harness, **override):
    """只改调用方点名的那几列；其余全部合法，失败即是被那条约束拒的。"""
    row = dict(_REPORT_ROW)
    row.update(override)
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    harness.conn.execute(f"INSERT INTO report ({columns}) VALUES ({marks})",
                         tuple(row.values()))


@pytest.fixture()
def empty():
    harness = Harness()
    _insert_report(harness)
    harness.conn.execute("DELETE FROM report")
    return harness


def test_report_row_is_valid_on_its_own(empty):
    _insert_report(empty)
    assert empty.count("report") == 1


@pytest.mark.parametrize("override,message", [
    ({"decision": "ERRORED"}, "decision IN"),
    ({"decision": "abstain", "coverage_ratio": 0.9},
     "report_abstain_has_failed"),
    ({"blocker_count": 1}, "report_pass_has_no_finding"),
    ({"decision": "repair"}, "report_repair_has_finding"),
    ({"report_generation": 2}, "report_supersedes_paired"),
    ({"coverage_ratio": 0.0, "decision": "repair", "blocker_count": 1},
     "report_analysis_provenance"),
    ({"artifact_kind": "image"}, "artifact_kind IN"),
    ({"node_epoch": 0}, "node_epoch >= 1"),
    ({"execution_fence": 0}, "execution_fence >= 1"),
])
def test_report_constraints_are_enforced_by_the_database(empty, override,
                                                         message):
    with pytest.raises(sqlite3.IntegrityError, match=re.escape(message)):
        _insert_report(empty, **override)


def test_check_status_and_reason_code_are_paired(empty):
    _insert_report(empty)
    base = dict(report_id="qc_x", report_generation=1, ordinal=0,
                check_id="AUD.TEXT.EXTRA_WORD", status="evaluated",
                reason_code="ok", defect_count=0)
    empty.conn.execute(
        "INSERT INTO report_check (report_id, report_generation, ordinal,"
        " check_id, status, reason_code, defect_count)"
        " VALUES (?,?,?,?,?,?,?)", tuple(base.values()))
    for override, message in [
        ({"check_id": "AUD.TEXT.MISSING_WORD", "status": "evaluated",
          "reason_code": "no_face"}, "rc_status_reason_paired"),
        ({"check_id": "AUD.TEXT.PROSODY_FLAT", "status": "failed",
          "reason_code": "ok"}, "rc_status_reason_paired"),
        ({"check_id": "AUD.TEXT.SUBSTITUTION", "status": "failed",
          "defect_count": 1}, "rc_status_reason_paired"),
    ]:
        row = dict(base)
        row.update(override)
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with pytest.raises(sqlite3.IntegrityError, match=message):
            empty.conn.execute(
                f"INSERT INTO report_check ({columns}) VALUES ({marks})",
                tuple(row.values()))


def test_alignment_unit_confidence_interval_must_be_ordered(empty):
    _insert_report(empty)
    base = dict(tenant_id="tnt_x", report_id="qc_x", report_generation=1,
                unit_index=0, utterance_index=0, utterance_id="u1",
                speaker_id="c", char_offset=0, char_length=1,
                start_seconds=1.0, end_seconds=1.2, ci_low_seconds=1.0,
                ci_high_seconds=1.2, confidence=0.9, phoneme_seq=None)
    columns = ", ".join(base)
    marks = ", ".join("?" for _ in base)
    empty.conn.execute(
        f"INSERT INTO alignment_unit ({columns}) VALUES ({marks})",
        tuple(base.values()))
    for override in ({"unit_index": 1, "ci_low_seconds": 1.1},
                     {"unit_index": 2, "ci_high_seconds": 1.1}):
        row = dict(base)
        row.update(override)
        row_columns = ", ".join(row)
        row_marks = ", ".join("?" for _ in row)
        with pytest.raises(sqlite3.IntegrityError, match="au_ci_ordered"):
            empty.conn.execute(
                f"INSERT INTO alignment_unit ({row_columns})"
                f" VALUES ({row_marks})", tuple(row.values()))


# ================================================================ 事件投影

def test_voice_authorization_projection_starts_empty_and_is_never_prefilled():
    harness = Harness()
    assert harness.count("voice_authorization_projection") == 0

    first = harness.service.consume_voice_authorization_event(TENANT, {
        "event_id": "ev1",
        "data": {"voice_id": "v1", "state": "revoked",
                 "effective_state": "revoked", "state_seq": 5,
                 "state_changed_at": "2026-01-01T00:00:00Z"},
    })
    assert first == {"accepted": True, "duplicate": False, "voice_id": "v1",
                     "state": "revoked", "effective_state": "revoked"}
    row = harness.conn.execute(
        "SELECT state, effective_state, state_seq"
        " FROM voice_authorization_projection").fetchone()
    assert tuple(row) == ("revoked", "revoked", 5)

    # Inbox 自己就是幂等：同一 event_id 再投一次不重复写。
    again = harness.service.consume_voice_authorization_event(TENANT, {
        "event_id": "ev1", "data": {"voice_id": "v1", "state": "revoked"}})
    assert again["duplicate"] is True

    # 更旧的事件不能把投影倒退。
    stale = harness.service.consume_voice_authorization_event(TENANT, {
        "event_id": "ev2",
        "data": {"voice_id": "v1", "state": "active",
                 "effective_state": "active", "state_seq": 2},
    })
    assert stale["accepted"] is True
    row = harness.conn.execute(
        "SELECT effective_state, state_seq"
        " FROM voice_authorization_projection").fetchone()
    assert tuple(row) == ("revoked", 5)


# ================================================================ 注册表与缺口

class TestRegistry:
    def test_registry_loads_under_the_reference_validator(self):
        spec = importlib.util.spec_from_file_location(
            "qc_validate_registry",
            CONTRACTS / "quality" / "validate_registry.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        families = _load("quality/action-families.v2.json")
        document = registry.registry_document()
        assert module.validate(document, families) == []

        jsonschema = pytest.importorskip("jsonschema")
        schema = _load("quality/check-registry.v2.schema.json")
        errors = list(jsonschema.Draft202012Validator(schema)
                      .iter_errors(document))
        assert errors == [], [(list(e.absolute_path), e.message)
                             for e in errors]

    def test_registry_entries_match_the_owner_and_layer(self):
        assert len(registry.CHECKS) == 20
        assert len(set(registry.CHECK_IDS)) == 20
        for check in registry.CHECKS:
            assert check["layer"] == "alignment"
            assert check["owner_service"] == "S37"
            assert check["check_id"].startswith("AUD.")
            assert check["_expected_source"] in ("S06", "S15")
            assert registry.SEVERITY_ORDER[check["severity_default"]] <= \
                registry.SEVERITY_ORDER[check["severity_max"]]
            assert set(check["allowed_actions"]) <= {
                "re_synthesize_speech", "regenerate_video",
                "temporal_edit", "redo_lipsync"}

    def test_observation_and_gating_checks_are_registered_but_never_defects(self):
        assert registry.OBSERVATION_ONLY == {"AUD.TEXT.SCRIPT_CONFLICT"}
        assert registry.GATING_ONLY == {"AUD.LIPSYNC.FACE_NOT_FOUND"}
        for check_id in registry.OBSERVATION_ONLY | registry.GATING_ONLY:
            assert check_id in registry.CHECK_IDS
            assert registry.BY_ID[check_id]["never_auto_repair"] is True
            assert registry.BY_ID[check_id]["no_auto_reason"]

    def test_lipsync_family_can_only_redo_lipsync(self):
        for check_id in [c for c in registry.CHECK_IDS
                         if c.startswith("AUD.LIPSYNC.")]:
            assert registry.BY_ID[check_id]["allowed_actions"] == \
                ["redo_lipsync"]

    def test_select_and_severity_helpers(self):
        assert len(registry.select(None, None)) == 20
        assert registry.select(["AUD.TEXT.*"], None) == [
            c for c in registry.CHECK_IDS if c.startswith("AUD.TEXT.")]
        with pytest.raises(ValueError, match="matches no check"):
            registry.select(["XYZ.*"], None)
        with pytest.raises(ValueError, match="non-empty"):
            registry.select([], None)
        with pytest.raises(ValueError, match="selects no check"):
            registry.select(["AUD.*"], ["AUD.*"])
        assert registry.severity_after("major", "minor", "blocker") == "major"
        assert registry.severity_after("major", "blocker", "blocker") == \
            "blocker"
        assert registry.severity_after("info", "major", "minor") == "minor"
        assert registry.report_scope("shot") == "segment"
        assert registry.report_scope("track") == "whole_artifact"
        with pytest.raises(ValueError):
            registry.report_scope("nonsense")

    def test_registry_document_drops_internal_keys(self):
        document = registry.registry_document()
        assert set(document) == {"schema_version", "registry_version",
                                 "checks"}
        for check in document["checks"]:
            assert not [k for k in check if k.startswith("_")]


def test_contract_gaps_are_declared_with_a_kind_and_a_handling():
    gaps = {gap["id"]: gap for gap in CONTRACT_GAPS}
    assert len(gaps) == 20
    assert set(gaps) == {f"S37-{i}" for i in range(1, 21)}
    for gap in CONTRACT_GAPS:
        assert set(gap) == {"id", "kind", "summary", "handling"}
        assert gap["kind"] in {"conflict", "gap", "needs_owner"}
        assert gap["summary"] and gap["handling"]


def test_service_owns_only_its_own_tables():
    harness = Harness()
    harness.command()
    tables = {row["name"] for row in harness.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables - {"idempotency_record"} == OWNED_TABLES


def test_source_imports_no_other_service():
    """AGENTS.md：不 import 别的服务的业务 ORM，只走注入的 callback。"""
    package = SERVICES / "audio-alignment" / "src" / "audio_alignment"
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for other in ("technical_qc", "semantic_qc", "prop_assets",
                      "scene_assets", "editorial", "artifact_ingest",
                      "voice_assets"):
            assert f"import {other}" not in text, (path.name, other)


# ================================================================ HTTP 层

def _client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    db = tmp_path / "s37.sqlite"
    monkeypatch.setenv("S37_DB", str(db))
    monkeypatch.setenv("AUDIO_ALIGNMENT_ENABLED", "true")
    monkeypatch.setenv("AUDIO_ALIGNMENT_CHECKS",
                       ",".join(registry.CHECK_IDS))
    for name in ("S28_BASE_URL", "S26_BASE_URL", "S06_BASE_URL",
                 "S15_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    spec = importlib.util.spec_from_file_location(
        "s37_app_under_test",
        SERVICES / "audio-alignment" / "api" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    harness = Harness()
    module.service = harness.service
    module._idem = harness._idem
    module._conn = harness.conn
    return TestClient(module.app), harness


def test_http_command_query_units_and_readiness(tmp_path, monkeypatch):
    client, _harness = _client(tmp_path, monkeypatch)
    headers = {"Idempotency-Key": "cmd1", "X-Tenant-Id": TENANT}
    response = client.post("/alignment-jobs", json=command_body(),
                           headers=headers)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["decision"] == "pass"
    assert body["replayed"] is False

    fetched = client.get(f"/alignment-reports/{body['report_id']}",
                         headers={"X-Tenant-Id": TENANT})
    assert fetched.status_code == 200
    assert fetched.json()["payload"]["decision"] == "pass"
    assert fetched.json()["source_revision"] == 1

    units = client.get(f"/alignment-reports/{body['report_id']}/units",
                       headers={"X-Tenant-Id": TENANT})
    assert units.status_code == 200
    assert units.json()["time_base"] == "presentation_pts"
    assert len(units.json()["units"]) == 3

    other = client.get(f"/alignment-reports/{body['report_id']}",
                       headers={"X-Tenant-Id": OTHER_TENANT})
    assert other.status_code == 404

    ready = client.get("/health/readiness")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert ready.json()["enabled"] is True
    assert len(ready.json()["enabled_checks"]) == 20
    assert {gap["id"] for gap in ready.json()["contract_gaps"]} >= {"S37-1"}


def test_http_replay_and_conflict(tmp_path, monkeypatch):
    client, _harness = _client(tmp_path, monkeypatch)
    headers = {"Idempotency-Key": "cmd1", "X-Tenant-Id": TENANT}
    first = client.post("/alignment-jobs", json=command_body(),
                        headers=headers)
    second = client.post("/alignment-jobs", json=command_body(),
                         headers=headers)
    assert second.status_code == 202
    assert second.json()["replayed"] is True
    assert second.json()["report_id"] == first.json()["report_id"]

    changed = command_body()
    changed["dialogue"]["utterances"][0]["text"] = "你好啊"
    conflict = client.post("/alignment-jobs", json=changed, headers=headers)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert conflict.json()["details"]["stored_request_hash"] != \
        conflict.json()["details"]["incoming_request_hash"]


def test_http_tenant_mismatch_is_403(tmp_path, monkeypatch):
    client, _harness = _client(tmp_path, monkeypatch)
    body = command_body()
    body["execution_context"]["tenant_id"] = OTHER_TENANT
    response = client.post("/alignment-jobs", json=body,
                           headers={"Idempotency-Key": "cmd1",
                                    "X-Tenant-Id": TENANT})
    assert response.status_code == 403
    assert response.json()["code"] == "TENANT_MISMATCH"


def test_http_in_flight_command_asks_for_a_retry(tmp_path, monkeypatch):
    client, harness = _client(tmp_path, monkeypatch)
    body = command_body()
    harness._idem.begin(TENANT, "cmd1", stable_request_hash(body))
    response = client.post("/alignment-jobs", json=body,
                           headers={"Idempotency-Key": "cmd1",
                                    "X-Tenant-Id": TENANT})
    assert response.status_code == 409
    assert response.headers["Retry-After"] == "1"
    assert response.json()["code"] == "COMMAND_IN_FLIGHT"
    assert response.json()["retryable"] is True


def test_http_disabled_service_is_503(tmp_path, monkeypatch):
    client, harness = _client(tmp_path, monkeypatch)
    harness.service.enabled = False
    response = client.post("/alignment-jobs", json=command_body(),
                           headers={"Idempotency-Key": "cmd1",
                                    "X-Tenant-Id": TENANT})
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_DISABLED"
    assert response.json()["details"] == {}
