"""S35 测试。方案 §10 的用例 + 通用验收（§10.4）。

最重要的一组断言在这条线上：**"没查成"永远不能变成 pass**。
E24-T8 / E25-T1 / E25-T3 / E25-T4 / 依赖超时都归到它——每一种失败都必须
落 `abstain`，且 `abstain.failed_check_ids` 非空、manifest 里对应项不是
`evaluated`。

`jsonschema` 校验不是可有可无的：报告与事件都要过契约，否则"写进去了"只是
"我的库里能存"。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from idempotency_sdk import CommandConflict, SqliteIdempotencyStore

from technical_qc import evaluation
from technical_qc.schema import CONTRACT_GAPS
from technical_qc.service import (
    ServiceError,
    TechnicalQcService,
    report_content_hash,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"

TENANT = "tnt_a"
SHA = "a" * 64
SHA2 = "b" * 64


# ---------------------------------------------------------------- 测试脚手架

class Harness:
    """内存库 + 四个 stub 上游。**不碰网络**：全部是闭包。"""

    def __init__(self, *, probe=None, shot=None, profile=None,
                 artifact_error=None, probe_error=None, stored_hash=None):
        import sqlite3

        self.conn = sqlite3.connect(":memory:", check_same_thread=False,
                                    isolation_level=None)
        self.artifact_error = artifact_error
        self.probe_error = probe_error
        # S28 里那份字节的 hash。默认就是命令体引用的 SHA；hash 不符的
        # 用例把它改掉，而不是去改请求。
        self.stored_hash = stored_hash or SHA
        self.probe = probe if probe is not None else good_probe()
        self.shot = shot
        self.profile = profile if profile is not None else good_profile()
        self.calls = {"artifact": 0, "probe": 0, "shot": 0, "policy": 0}
        self.service = TechnicalQcService(
            self.conn,
            artifact_source=self._artifact,
            probe_source=self._probe,
            shot_source=self._shot,
            policy_source=self._policy,
            enabled=True,
            enabled_checks=tuple(checks_ids()),
        )
        self._idem = SqliteIdempotencyStore(self.conn)

    # ---- stubs
    def _artifact(self, tenant_id, ref):
        self.calls["artifact"] += 1
        if self.artifact_error is not None:
            raise self.artifact_error
        # 存的是**库里的**那一份字节的 hash，不是请求里说的那个：S28 的
        # 语义是"我这里有这份内容"，所以请求说 SHA2 而库里是 SHA 时，
        # 复核方要能看见不一致（`ARTIFACT_HASH_MISMATCH`）。回显请求值
        # 会让那条检查永远看不到差异。
        return {"artifact_id": ref["id"], "revision": ref.get("revision", 1),
                "content_hash": self.stored_hash,
                "size_bytes": 1048576, "mime_type": "video/mp4",
                "duration_ms": 4000, "width_px": 1920, "height_px": 1080,
                "frame_rate": {"num": 24, "den": 1},
                "manifest_id": "man_1", "manifest_revision": 12,
                "manifest_hash": "c" * 64, "has_audio": True,
                "audio_codec": "aac", "video_codec": "h264",
                "pix_fmt": "yuv420p", "color_range": "tv",
                "sample_rate_hz": 48000, "channels": 2}

    def _probe(self, tenant_id, ref, artifact_kind):
        self.calls["probe"] += 1
        if self.probe_error is not None:
            raise self.probe_error
        return self.probe

    def _shot(self, tenant_id, ref, context):
        self.calls["shot"] += 1
        return self.shot

    def _policy(self, tenant_id, ref, profile_ref):
        self.calls["policy"] += 1
        return self.profile

    # ---- 命令
    def command(self, body=None, *, command_id="cmd_1", tenant_id=TENANT):
        body = body if body is not None else check_body()
        from idempotency_sdk import canonical_request_hash
        request_hash = canonical_request_hash(body)
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

    def events(self):
        """outbox 由平台 SDK 建表，**没有 `id` 列**（`event_id` 是主键）。
        它自己的 `unpublished()` 就按 `created_at, sequence` 排序，照它来。
        返回的是解包后的 envelope，并补上 SDK 存在列里、envelope 里没有的
        两个投影字段，免得测试去猜列名。
        """
        rows = self.conn.execute(
            "SELECT * FROM outbox ORDER BY created_at, sequence").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            envelope = json.loads(item.pop("payload"))
            out.append({**envelope,
                        "event_type": item["event_type"],
                        "aggregate_sequence": item["sequence"]})
        return out

    def count(self, table="report"):
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def checks_ids():
    from technical_qc import checks
    return checks.ALL_CHECKS


def good_probe():
    """一份"全部查成、全部通过"的探针文档。

    每条 handler 依赖的段落都在；缺一段就会被判 failed——这正是要测的。
    """
    return {
        "ok": True,
        "error": None,
        "container": {"size_bytes": 1048576, "parsed": True, "has_moov": True,
                      "declared_frame_count": 96,
                      "declared_duration_seconds": 4.0},
        "media": {
            "duration_seconds": 4.0,
            "video": {"width": 1920, "height": 1080, "fps_num": 24,
                      "fps_den": 1, "codec": "h264", "pix_fmt": "yuv420p",
                      "color_range": "tv", "is_vfr": False, "frame_count": 96,
                      "start_offset_seconds": 0.0},
            "audio": {"codec": "aac", "sample_rate_hz": 48000, "channels": 2,
                      "stream_index": 1, "start_offset_seconds": 0.0},
        },
        "decode": {"packet_errors": 0, "error_timecodes": [],
                   "frames_decoded": 96, "eof_reached": True},
        "frames": {"sampled": 96, "windows_evaluated": 4, "planned_windows": 4,
                   "black": [], "freeze": [], "flash": []},
        "audio": {"analyzed": True, "analyzed_seconds": 4.0,
                  "integrated_lufs": -16.0, "true_peak_db": -1.5,
                  "clipping_ratio": 0.0, "head_silence_seconds": 0.1,
                  "tail_silence_seconds": 0.1, "silent": False},
        "subtitles": {"burned_in": False, "frames": [], "bbox": None},
        "metadata": {"color_primaries": "bt709", "rotation": 0},
    }


def good_profile():
    return {
        "policy_id": "pol_qc",
        "revision": 7,
        "policy_version": "7",
        "evaluator_name": "s35-deterministic-probe",
        "evaluator_version": "2.0.0",
        "image_digest": "sha256:abc",
        "sampling_policy_version": "samp_1",
        "min_coverage_ratio": 0.90,
        "tau_low": 0.40,
        "tau_abstain": 0.35,
        "tau_disagreement": 0.30,
        "allowed_actions": ["reingest", "transcode", "remix", "temporal_edit",
                            "regenerate_video"],
        "subtitle_policy": "post_composite",
        "target": {"width": 1920, "height": 1080, "fps": "24/1",
                   "loudness_lufs": -16.0, "true_peak_db": -1.0},
        "thresholds": {
            "resolution_floor_short_side": 720,
            "color_range": "tv",
            "black_min_frames": 8,
            "freeze_min_seconds": 0.5,
            "flash_luma_delta": 0.6,
            "duration_tolerance_frames": 1,
            "audio_offset_tolerance_frames": 1,
            "codec_whitelist": {"video": ["h264", "hevc"],
                                "pix_fmt": ["yuv420p", "yuv420p10le"],
                                "audio": ["aac", "pcm_s16le"]},
        },
    }


def check_body(**overrides):
    body = {
        "command_id": "cmd_1",
        "execution_context": {
            "schema_version": "2.0",
            "tenant_id": TENANT,
            "project_id": "prj_1",
            "series_id": "ser_1",
            "branch_id": "brn_main",
            "episode_id": "ep_1",
            "home_region": "cn",
            "home_cell": "cell_a1",
            "manifest_ref": {"id": "man_1", "revision": 12,
                             "content_hash": "c" * 64},
            "node_id": "node_3_2",
            "node_epoch": 4,
            "execution_fence": 2,
            "policy_ref": {"service_id": "S02", "id": "pol_qc", "revision": 7,
                           "content_hash": "d" * 64},
            "trace_id": "trace_1",
        },
        "run_id": "run_1",
        "shot_lineage_id": "lin_1",
        "artifact": {"service_id": "S28", "id": "clip_x", "revision": 1,
                     "content_hash": SHA},
        "artifact_kind": "video",
        "shot_spec_ref": {"service_id": "S06", "id": "shot_3_2",
                          "revision": 4, "content_hash": "e" * 64},
        "profile_ref": {"mode": "shot"},
        "repair_context": None,
    }
    body.update(overrides)
    return body


def shot_spec(**overrides):
    shot = {"shot_id": "shot_3_2", "revision": 4, "duration_seconds": 4.0,
            "dialogue": [{"character_id": "chr_a", "text": "你好"}]}
    shot.update(overrides)
    return shot


# ---------------------------------------------------------------- 契约校验

def _load_schema(relative):
    with open(CONTRACTS / relative, encoding="utf-8") as handle:
        return json.load(handle)


# `tools/check_all.py` 的 `KNOWN_BAD_CHECK_IDS` 点名了三条两段式 TECH id，
# 等 S35 的 owner 决定改名。测试跟着同一份白名单走：**逐条点名**而不是
# 静默跳过，名单外出现任何不匹配都算失败。
KNOWN_BAD_CHECK_IDS = {"TECH.BLACK_FRAME", "TECH.FREEZE_FRAME",
                       "TECH.FLASH_FRAME"}


def validate(instance, relative):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(instance, _load_schema(relative))


def validate_report(payload):
    """qc-report.v2 整份校验，按 S35-17 的已知分歧逐条豁免。

    豁免是**可证伪**的：把三条 id 换成三段式后，下面的断言会因为
    `nonconforming` 变空而失败，提醒这里该恢复成整份校验。
    """
    import re

    jsonschema = pytest.importorskip("jsonschema")
    pattern = re.compile(_load_schema("quality/qc-report.v2.schema.json")
                         ["properties"]["check_manifest"]["items"]
                         ["properties"]["check_id"]["pattern"])
    nonconforming = {entry["check_id"] for entry in payload["check_manifest"]
                     if not pattern.match(entry["check_id"])}
    assert nonconforming == KNOWN_BAD_CHECK_IDS, nonconforming
    reduced = json.loads(json.dumps(payload))
    reduced["check_manifest"] = [
        entry for entry in reduced["check_manifest"]
        if entry["check_id"] not in nonconforming]
    schema = _load_schema("quality/qc-report.v2.schema.json")
    schema["properties"]["check_manifest"]["items"]["properties"]["check_id"] = {
        "type": "string", "minLength": 1}
    jsonschema.validate(reduced, schema)
    # 被摘掉的那三条只差 pattern：status / reason_code / 键集仍要合法。
    # 所以对它们用同一份 items schema，只把 check_id 的 pattern 放成"任意
    # 非空字符串"——别的任何偏差都会被抓到。
    item_schema = dict(
        _load_schema("quality/qc-report.v2.schema.json")
        ["properties"]["check_manifest"]["items"],
        properties={"check_id": {"type": "string", "minLength": 1},
                    **{k: v for k, v in _load_schema(
                        "quality/qc-report.v2.schema.json")
                        ["properties"]["check_manifest"]["items"]
                        ["properties"].items() if k != "check_id"}})
    for entry in payload["check_manifest"]:
        if entry["check_id"] in nonconforming:
            jsonschema.validate(entry, item_schema)


# ---------------------------------------------------------------- 主路径

def test_happy_path_passes_and_validates_against_contract():
    harness = Harness(shot=shot_spec())
    result = harness.command()
    assert result["status"] == "accepted"
    assert result["report_id"].startswith("qc_")
    assert result["replayed"] is False

    payload = harness.payload()
    assert payload["decision"] == "pass", payload["decision_reason"]
    assert payload["abstain"] is None
    assert payload["confidence"] == 1.0
    assert payload["coverage"]["coverage_ratio"] == 1.0
    assert payload["defects"] == []
    assert len(payload["check_manifest"]) == 23
    # "该查的都查了"：23 条里没有被策略关掉、没有没查成的。
    assert {e["status"] for e in payload["check_manifest"]} == {"evaluated"}
    assert payload["report_content_hash"] == report_content_hash(payload)

    validate_report(payload)

    events = harness.events()
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "quality.technical.completed"
    assert event["aggregate_id"] == result["report_id"]
    assert event["aggregate_sequence"] == 1
    # 事件体就是 quality.technical-completed.v2 的 payload 本身，不是
    # 再包一层的字符串（`Outbox.append` 把整个 envelope 序列化进 payload 列）。
    data = event["data"]
    assert data["decision"] == "pass"
    assert data["defect_count"] == 0 and data["blocker_count"] == 0
    assert data["shot_id"] == payload["shot_ref"]["shot_id"]
    assert data["attempt_slot_id"] is None
    ref = event["data_ref"]
    assert set(ref) == {"service_id", "id", "revision", "content_hash"}
    assert ref["service_id"] == "S35"

    envelope = {
        "schema_version": data["schema_version"],
        "report_id": data["report_id"],
        "report_generation": data["report_generation"],
        "qc_layer": data["qc_layer"],
        "artifact_sha256": data["artifact_sha256"],
        "manifest_ref": data["manifest_ref"],
        "shot_id": data["shot_id"],
        "shot_lineage_id": data["shot_lineage_id"],
        "node_epoch": data["node_epoch"],
        "run_id": data["run_id"],
        "execution_fence": data["execution_fence"],
        "decision": data["decision"],
        "decision_reason": data["decision_reason"],
        "coverage_ratio": data["coverage_ratio"],
        "attempt_slot_id": data["attempt_slot_id"],
        "defect_count": data["defect_count"],
        "blocker_count": data["blocker_count"],
    }
    validate(envelope, "quality.technical-completed.v2.schema.json")


def test_report_row_matches_payload():
    harness = Harness(shot=shot_spec())
    harness.command()
    row = dict(harness.conn.execute("SELECT * FROM report").fetchone())
    payload = harness.payload()
    assert row["decision"] == payload["decision"]
    assert row["coverage_ratio"] == payload["coverage"]["coverage_ratio"]
    assert row["content_hash"] == payload["report_content_hash"]
    assert row["node_epoch"] == payload["shot_ref"]["node_epoch"]
    assert row["execution_fence"] == payload["context"]["execution_fence"]
    # 两个坐标分开存，绝不合并（AGENTS.md）。
    assert "execution_fence" in row and "node_epoch" in row


# ---------------------------------------------------------------- 缺陷

def test_blocker_defect_is_repair_with_measured_and_fingerprint():
    probe = good_probe()
    probe["media"]["duration_seconds"] = 3.958
    probe["container"]["declared_duration_seconds"] = 4.0
    harness = Harness(probe=probe, shot=shot_spec())
    harness.command()

    payload = harness.payload()
    assert payload["decision"] == "repair"
    defect = next(d for d in payload["defects"]
                  if d["check_id"] == "TECH.DURATION.MISMATCH")
    assert defect["severity"] == "blocker"
    assert defect["detector"] == "deterministic"
    assert defect["measured"]["value"] == pytest.approx(3.958)
    # E24-T3：measured = {3.958, s, 4.000, ne}
    assert defect["measured"]["unit"] == "s"
    assert defect["measured"]["threshold"] == pytest.approx(4.0)
    assert defect["measured"]["comparator"] == "ne"
    # 指纹 32 个十六进制（契约 pattern），且不含 artifact hash 的成分。
    assert len(defect["defect_fingerprint"]) == 32
    assert all(c in "0123456789abcdef" for c in defect["defect_fingerprint"])
    assert defect["defect_id"].startswith("df_")
    assert payload["decision_reason"].startswith("TECH.DURATION.MISMATCH")
    validate_report(payload)

    data = harness.events()[0]["data"]
    assert data["defect_count"] == 1 and data["blocker_count"] == 1


def test_major_defect_with_allowed_action_is_repair():
    probe = good_probe()
    probe["media"]["video"]["fps_num"] = 25
    harness = Harness(probe=probe, shot=shot_spec())
    harness.command()
    payload = harness.payload()
    assert payload["decision"] == "repair"
    check = harness.manifest()["TECH.FPS.MISMATCH"]
    assert check["status"] == "evaluated"


def test_major_without_policy_allowed_actions_goes_to_human_review():
    profile = good_profile()
    profile["allowed_actions"] = []
    probe = good_probe()
    probe["media"]["video"]["fps_num"] = 25
    harness = Harness(probe=probe, profile=profile, shot=shot_spec())
    harness.command()
    payload = harness.payload()
    assert payload["decision"] == "human_review"
    assert payload["abstain"] is None  # human_review 不是弃权


# ---------------------------------------------------------------- 没查成 ≠ pass

@pytest.mark.parametrize("probe_mutation,expected_reason", [
    (lambda p: p.pop("decode"), "evaluator_error"),
    (lambda p: p.pop("audio"), "evaluator_error"),
    (lambda p: p.pop("container"), "evaluator_error"),
    (lambda p: p.pop("frames"), "evaluator_error"),
])
def test_missing_probe_section_is_never_a_pass(probe_mutation, expected_reason):
    """探针缺一段 → 依赖它的检查 failed → 整份报告 abstain。

    这条是"没查成 ≠ 通过"的最短路径：把 `{}` 读成"没有缺陷"就会 pass。
    """
    probe = good_probe()
    probe_mutation(probe)
    harness = Harness(probe=probe, shot=shot_spec())
    harness.command()
    payload = harness.payload()
    assert payload["decision"] == "abstain"
    assert payload["abstain"] is not None
    assert payload["abstain"]["failed_check_ids"]
    assert payload["abstain"]["reason_code"] == expected_reason
    statuses = {e["check_id"]: e["status"] for e in payload["check_manifest"]}
    for check_id in payload["abstain"]["failed_check_ids"]:
        assert statuses[check_id] in ("failed", "not_evaluated")


def test_sandbox_timeout_is_failed_not_clean():
    """E25-T1 / §3.2：超时是 failed，不是"没检出问题"。"""
    harness = Harness(shot=shot_spec(),
                      probe={"ok": False,
                             "error": {"code": "timeout", "detail": "killed"}})
    harness.command()
    payload = harness.payload()
    assert payload["decision"] == "abstain"
    assert payload["abstain"]["reason_code"] == "evaluator_timeout"
    assert payload["abstain"]["failed_check_ids"]
    assert all(e["status"] == "failed"
               for e in payload["check_manifest"]
               if e["check_id"] in payload["abstain"]["failed_check_ids"])
    assert payload["confidence"] == 0.0


def test_partial_coverage_below_minimum_abstains():
    """E25-T3：覆盖 70% < 0.90 → abstain，且报告仍然发布。"""
    probe = good_probe()
    probe["frames"]["planned_windows"] = 10
    probe["frames"]["windows_evaluated"] = 7
    harness = Harness(probe=probe, shot=shot_spec())
    harness.command()
    payload = harness.payload()
    assert payload["coverage"]["coverage_ratio"] == pytest.approx(0.7)
    assert payload["decision"] == "abstain"
    assert payload["abstain"]["reason_code"] == "incomplete_coverage"
    # §2.5 要求 4：弃权也要发布。报告与事件都在。
    assert harness.count("report") == 1
    assert len(harness.events()) == 1


def test_zero_planned_windows_forces_zero_coverage():
    """§3.3：planned=0 → coverage_ratio 0，不是 1.0。"""
    probe = good_probe()
    probe["frames"]["planned_windows"] = 0
    probe["frames"]["windows_evaluated"] = 0
    harness = Harness(probe=probe, shot=shot_spec())
    harness.command()
    payload = harness.payload()
    assert payload["coverage"]["coverage_ratio"] == 0.0
    assert payload["decision"] == "abstain"


def test_missing_shot_spec_leaves_dependent_checks_not_evaluated():
    """S35-7：S06 读不到 → 两项 not_evaluated → abstain，不猜"没对白"。"""
    harness = Harness(shot=None)
    harness.command()
    manifest = harness.manifest()
    assert manifest["TECH.DURATION.MISMATCH"]["status"] == "not_evaluated"
    assert manifest["TECH.AUDIO.MISSING"]["status"] == "not_evaluated"
    assert harness.payload()["decision"] == "abstain"
    assert set(harness.payload()["abstain"]["failed_check_ids"]) >= {
        "TECH.DURATION.MISMATCH", "TECH.AUDIO.MISSING"}


def test_no_dialogue_without_audio_is_not_applicable_and_can_pass():
    """E24-T7：无对白且无音轨 = 本来就没有，不弃权。"""
    probe = good_probe()
    probe["media"]["audio"] = None
    harness = Harness(probe=probe,
                      shot=shot_spec(dialogue=[]))
    harness.command()
    manifest = harness.manifest()
    assert manifest["TECH.AUDIO.MISSING"] == {
        "check_id": "TECH.AUDIO.MISSING", "status": "not_applicable",
        "reason_code": "no_dialogue"}
    payload = harness.payload()
    assert payload["decision"] == "pass"
    assert payload["abstain"] is None
    validate_report(payload)


def test_dialogue_without_audio_is_blocker():
    """E24-T6：有对白但无音轨是 blocker，不是 not_applicable。"""
    probe = good_probe()
    probe["media"]["audio"] = None
    harness = Harness(probe=probe, shot=shot_spec())
    harness.command()
    payload = harness.payload()
    defect = next(d for d in payload["defects"]
                  if d["check_id"] == "TECH.AUDIO.MISSING")
    assert defect["severity"] == "blocker"
    assert payload["decision"] == "repair"


def test_asset_unreachable_still_generates_an_abstaining_report():
    """§10.4：S28 不可达 → abstain，不是 500。"""
    harness = Harness(shot=shot_spec(),
                      artifact_error=ConnectionError("no route"))
    result = harness.command()
    assert result["decision"] == "abstain"
    payload = harness.payload()
    # §3.2 末段 / §10.4：连 ffprobe 都跑不起来 → `asset_unavailable`。
    assert payload["abstain"]["reason_code"] == "asset_unavailable"
    assert payload["abstain"]["failed_check_ids"]
    assert harness.count("report") == 1


# ---------------------------------------------------------------- 判定词表

def test_decide_refuses_pass_with_failed_required_check():
    """§2.7 第 3 步在评分之前：failed 存在时即使没有缺陷也弃权。"""
    manifest = [{"check_id": c, "status": "evaluated", "reason_code": "ok",
                 "defects": []} for c in checks_ids()]
    manifest[5]["status"] = "failed"
    manifest[5]["reason_code"] = "evaluator_timeout"
    decided = evaluation.decide(
        manifest=manifest, defects=[], coverage={"coverage_ratio": 1.0},
        confidence=1.0,
        policy=dict(evaluation.DEFAULTS, allowed_actions=[]))
    assert decided["decision"] == "abstain"
    assert decided["abstain"]["failed_check_ids"] == [manifest[5]["check_id"]]


def test_decide_refuses_pass_when_manifest_is_incomplete():
    manifest = [{"check_id": c, "status": "evaluated", "reason_code": "ok",
                 "defects": []} for c in checks_ids()][:-1]
    decided = evaluation.decide(
        manifest=manifest, defects=[], coverage={"coverage_ratio": 1.0},
        confidence=1.0, policy=dict(evaluation.DEFAULTS))
    assert decided["decision"] == "abstain"
    assert decided["abstain"]["reason_code"] == "incomplete_coverage"


def _insert_report(harness, **override):
    """直接往库里塞一行，**只**改调用方点名的那几列。

    其余列全部合法，所以断言失败时能确定是那条约束拒的——不是某列
    漏填撞上 `NOT NULL`，也不是唯一索引。表结构变了这里会立刻报错，
    而不是让测试静默"通过"。
    """
    row = dict(
        report_id="qc_x", report_generation=1, derived_key="f" * 64,
        tenant_id="tnt_x", project_id="p", series_id="s", episode_id="e",
        branch_id="b", shot_id="sh", shot_lineage_id="ln",
        logical_node_id="nd", node_epoch=1, manifest_id="m",
        manifest_revision=1, run_id="r", execution_fence=1,
        artifact_id="a", artifact_sha256=SHA, artifact_kind="video",
        evaluator_name="ev", evaluator_version="1", policy_version="1",
        sampling_version="1", decision="pass", decision_reason="why",
        confidence=1.0, coverage_ratio=1.0, required_coverage_min=0.9,
        defect_count=0, major_count=0, blocker_count=0,
        failed_check_count=0, payload="{}", content_hash="0" * 64,
        created_at="2026-01-01T00:00:00.000Z")
    row.update(override)
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    harness.conn.execute(f"INSERT INTO report ({columns}) VALUES ({marks})",
                         tuple(row.values()))


def test_decision_vocabulary_is_a_database_check_constraint():
    """词表不是约定：库层直接拒绝第五个值。"""
    import sqlite3
    harness = Harness(shot=shot_spec())
    harness.command()
    with pytest.raises(sqlite3.IntegrityError,
                       match="CHECK constraint failed: decision"):
        _insert_report(harness, decision="ERRORED")


def test_abstain_needs_a_verifiable_reason_in_the_database():
    """弃权必须带一条库层可复核的理由，两种形态各测一次。

    形态一：有没查成的 check。形态二：实际覆盖低于本条报告自己的合规线。
    两者都不成立时（覆盖达标却没有任何失败项）拒绝——"查完了还说弃权"
    没有出处。
    """
    import sqlite3
    harness = Harness(shot=shot_spec())
    harness.command()
    # 有 failed：允许。
    _insert_report(harness, report_id="qc_ok1", derived_key="1" * 64,
                   decision="abstain", failed_check_count=3)
    # 覆盖 0.70 < 0.90：允许，理由在数据层能复核。
    _insert_report(harness, report_id="qc_ok2", derived_key="2" * 64,
                   decision="abstain", coverage_ratio=0.70)
    # 覆盖正好等于合规线：不足是严格的 `<`，所以拒绝。
    with pytest.raises(sqlite3.IntegrityError,
                       match="report_abstain_has_failed"):
        _insert_report(harness, report_id="qc_bad", derived_key="3" * 64,
                       decision="abstain", coverage_ratio=0.9)


def test_pass_and_repair_must_agree_with_their_own_findings():
    """`pass` 不能带 blocker；`repair` 不能没有任何要修的东西。"""
    import sqlite3
    harness = Harness(shot=shot_spec())
    harness.command()
    with pytest.raises(sqlite3.IntegrityError, match="report_pass_has_no_finding"):
        _insert_report(harness, report_id="qc_p", derived_key="4" * 64,
                       blocker_count=1)
    with pytest.raises(sqlite3.IntegrityError, match="report_repair_has_finding"):
        _insert_report(harness, report_id="qc_r", derived_key="5" * 64,
                       decision="repair")


# ---------------------------------------------------------------- 幂等 / 隔离

def test_replay_returns_same_report_without_remeasuring():
    harness = Harness(shot=shot_spec())
    first = harness.command(command_id="cmd_1")
    probes_after_first = harness.calls["probe"]
    second = harness.command(command_id="cmd_1")
    assert second["replayed"] is True
    assert second["report_id"] == first["report_id"]
    assert harness.calls["probe"] == probes_after_first
    assert harness.count("report") == 1
    assert len(harness.events()) == 1


def test_same_command_different_body_conflicts():
    harness = Harness(shot=shot_spec())
    harness.command(command_id="cmd_1")
    with pytest.raises(CommandConflict):
        harness.command(check_body(run_id="run_2"), command_id="cmd_1")


def test_new_command_same_input_supersedes_generation():
    """§2.6：同一派生键第 N 次评估沿用 report_id，generation+1。"""
    harness = Harness(shot=shot_spec())
    first = harness.command(command_id="cmd_1")
    second = harness.command(command_id="cmd_2")
    assert second["report_id"] == first["report_id"]
    assert second["report_generation"] == 2
    payload = harness.payload()
    assert payload["supersedes"] == {"report_id": first["report_id"],
                                     "report_generation": 1}
    assert harness.count("report") == 2
    # 审计回看：两代都能按 generation 取到。
    oldest = harness.report(generation=1)
    assert oldest["payload"]["report_generation"] == 1
    # 事件按 aggregate_sequence 递增。
    assert [e["aggregate_sequence"] for e in harness.events()] == [1, 2]


def test_attempt_slot_changes_derived_key():
    """E26-T1：复检报告的派生键含 attempt_slot_id，与首次不同。"""
    harness = Harness(shot=shot_spec())
    first = harness.command(command_id="cmd_1")
    body = check_body(
        repair_context={"repair_plan_id": "rp_1",
                        "attempt_slot_id": "slot_2", "attempt_seq": 1,
                        "defect_fingerprints": []},
        artifact={"service_id": "S28", "id": "clip_x", "revision": 2,
                  "content_hash": SHA2})
    harness.service.conn.execute("DELETE FROM idempotency_record")
    # 新的修订确实换了字节：S28 那边现在存的是 SHA2。
    harness.stored_hash = SHA2
    second = harness.command(body, command_id="cmd_2")
    assert second["report_id"] != first["report_id"]
    payload = harness.payload()
    assert payload["repair_context"]["attempt_slot_id"] == "slot_2"
    assert payload["shot_ref"]["shot_id"] == "shot_3_2"


def test_cross_tenant_read_is_404_not_403():
    harness = Harness(shot=shot_spec())
    report_id = harness.command()["report_id"]
    with pytest.raises(ServiceError) as caught:
        harness.service.get_report("tnt_b", report_id)
    assert caught.value.status == 404
    # 同一 404 用于"不存在"，不泄漏存在性。
    with pytest.raises(ServiceError) as missing:
        harness.service.get_report("tnt_b", "qc_doesnotexist")
    assert missing.value.status == 404
    assert caught.value.code == missing.value.code


def test_cross_tenant_command_is_403():
    harness = Harness(shot=shot_spec())
    with pytest.raises(ServiceError) as caught:
        harness.command(tenant_id="tnt_b")
    assert caught.value.status == 403
    assert caught.value.code == "TENANT_MISMATCH"
    assert harness.count("report") == 0


# ---------------------------------------------------------------- 命令校验

def test_missing_run_id_is_422():
    harness = Harness(shot=shot_spec())
    body = check_body()
    body.pop("run_id")
    with pytest.raises(ServiceError) as caught:
        harness.command(body)
    assert caught.value.status == 422
    assert harness.count("report") == 0


def test_disabled_service_refuses_to_emit_a_report():
    import sqlite3
    conn = sqlite3.connect(":memory:", isolation_level=None)
    service = TechnicalQcService(
        conn, artifact_source=lambda *a: {}, probe_source=lambda *a: {},
        shot_source=lambda *a: None, policy_source=lambda *a: {},
        enabled=False, enabled_checks=())
    with pytest.raises(ServiceError) as caught:
        service.check(TENANT, "cmd_1", check_body(), "h")
    assert caught.value.code == "SERVICE_DISABLED"


def test_empty_check_whitelist_refuses_to_emit_an_empty_manifest():
    """S35-12：白名单为空时不能产出一份空清单的 pass。"""
    import sqlite3
    conn = sqlite3.connect(":memory:", isolation_level=None)
    service = TechnicalQcService(
        conn, artifact_source=lambda *a: {}, probe_source=lambda *a: {},
        shot_source=lambda *a: None, policy_source=lambda *a: {},
        enabled=True, enabled_checks=())
    with pytest.raises(ServiceError) as caught:
        service.check(TENANT, "cmd_1", check_body(), "h")
    assert caught.value.status == 422
    assert caught.value.code == "NO_CHECKS_ENABLED"


def test_unknown_check_id_fails_at_load_time():
    """ROUTE-T1：越界登记在加载期失败，不是运行期静默跳过。"""
    import sqlite3
    conn = sqlite3.connect(":memory:", isolation_level=None)
    with pytest.raises(ValueError):
        TechnicalQcService(
            conn, artifact_source=lambda *a: {}, probe_source=lambda *a: {},
            shot_source=lambda *a: None, policy_source=lambda *a: {},
            enabled=True, enabled_checks=("TECH.NOT.A_CHECK",))


def test_artifact_metadata_hash_mismatch_is_409():
    """E24-T9 的前置：被测量的不是被引用的那份，不产报告。"""
    # 请求引用 SHA2，S28 里那份字节是 SHA → 被测量的不是被引用的那份。
    harness = Harness(shot=shot_spec(), stored_hash=SHA)
    body = check_body(artifact={"service_id": "S28", "id": "clip_x",
                                "revision": 1, "content_hash": SHA2})
    with pytest.raises(ServiceError) as caught:
        harness.command(body)
    assert caught.value.status == 409
    assert caught.value.code == "ARTIFACT_HASH_MISMATCH"
    assert harness.count("report") == 0


def test_unsupported_artifact_kind_is_422():
    harness = Harness(shot=shot_spec())
    with pytest.raises(ServiceError) as caught:
        harness.command(check_body(artifact_kind="image"))
    assert caught.value.code == "UNSUPPORTED_ARTIFACT_KIND"


def test_list_reports_requires_a_scope():
    harness = Harness(shot=shot_spec())
    harness.command()
    with pytest.raises(ServiceError) as caught:
        harness.service.list_reports(TENANT)
    assert caught.value.code == "QUERY_SCOPE_REQUIRED"
    items = harness.service.list_reports(TENANT, artifact_sha256=SHA)["items"]
    assert len(items) == 1
    assert items[0]["decision"] == "pass"


# ---------------------------------------------------------------- 指纹

def test_fingerprint_buckets_severity_and_time_but_not_artifact_hash():
    """§2.4 的四条：`severity` 只按 blocker / non_blocker 分档，时间按 0.5s
    桶，`observed` 与 `artifact_sha256` 都不进指纹。

    blocker 与 major 分属两档，所以**必须**不同（"blocker 与 non_blocker
    分裂"）；major 与 minor 同属 non_blocker，所以**必须**相同。
    """
    base = dict(check_id="TECH.DURATION.MISMATCH", kind="technical",
                character_id=None, start_seconds=1.0)
    blocker = evaluation.defect_fingerprint(severity="blocker", **base)
    major = evaluation.defect_fingerprint(severity="major", **base)
    minor = evaluation.defect_fingerprint(severity="minor", **base)
    assert blocker != major      # 跨档：分裂
    assert major == minor        # 同档：不分裂（精确值不参与）
    assert blocker == evaluation.defect_fingerprint(
        severity="blocker", **dict(base, start_seconds=1.2))   # 同 0.5s 桶
    assert blocker != evaluation.defect_fingerprint(
        severity="blocker", **dict(base, start_seconds=1.6))   # 跨桶
    assert len(blocker) == 32


def test_fingerprint_is_stable_across_reports_with_new_hash():
    """E24-T11：新 hash 下同类缺陷指纹相同，S39 才能判"同类重复"。"""
    probe = good_probe()
    probe["media"]["duration_seconds"] = 3.958
    harness = Harness(probe=probe, shot=shot_spec())
    harness.command()
    first = harness.payload()["defects"][0]["defect_fingerprint"]
    harness.service.conn.execute("DELETE FROM idempotency_record")
    body = check_body(artifact={"service_id": "S28", "id": "clip_x",
                                "revision": 2, "content_hash": SHA2})
    harness.stored_hash = SHA2
    harness.command(body, command_id="cmd_2")
    second = [d for d in harness.payload()["defects"]
              if d["check_id"] == "TECH.DURATION.MISMATCH"][0]
    assert second["defect_fingerprint"] == first


# ---------------------------------------------------------------- 边界

def test_service_does_not_repair_or_write_artifacts():
    """只测量不修复：内核不暴露任何写 artifact / 触发重生成的入口。"""
    names = {n for n in dir(TechnicalQcService) if not n.startswith("__")}
    for forbidden in ("repair", "reingest", "transcode", "regenerate",
                      "apply", "execute_repair"):
        assert not any(forbidden in name.lower() for name in names), forbidden
    tables = {row[0] for row in Harness(shot=shot_spec()).conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any("repair" in t for t in tables)


def test_readiness_exposes_contract_gaps():
    harness = Harness(shot=shot_spec())
    gaps = harness.service.conn.execute("SELECT 1").fetchone()
    assert gaps is not None
    ids = {g["id"] for g in CONTRACT_GAPS}
    assert {"S35-1", "S35-5", "S35-7", "S35-15"} <= ids
    for gap in CONTRACT_GAPS:
        assert set(gap) == {"id", "kind", "summary", "handling"}
        assert gap["kind"] in ("conflict", "needs_owner", "gap")


# ---------------------------------------------------------------- HTTP 层

def _client(tmp_path, monkeypatch, harness_env=None):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    db = tmp_path / "s35.sqlite"
    monkeypatch.setenv("S35_DB", str(db))
    monkeypatch.setenv("TECHNICAL_QC_ENABLED", "true")
    from technical_qc import checks
    monkeypatch.setenv("TECHNICAL_QC_CHECKS", ",".join(checks.ALL_CHECKS))
    for name in ("S28_BASE_URL", "MEDIA_PROBE_BASE_URL", "S06_BASE_URL",
                 "S02_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    spec = importlib.util.spec_from_file_location(
        "s35_app_under_test", ROOT / "services" / "technical-qc" / "api" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    harness = Harness(shot=shot_spec())
    module.service = harness.service
    module._idem = harness._idem
    module._conn = harness.conn
    return TestClient(module.app), harness


def test_http_command_and_query(tmp_path, monkeypatch):
    client, harness = _client(tmp_path, monkeypatch)
    # `app._body` 先比 header 与 body 的 command_id（400），再比租户（403）。
    # 两处用同一个 id，否则测到的是前者。
    response = client.post("/technical-checks", json=check_body(),
                           headers={"Idempotency-Key": "cmd_1",
                                    "X-Tenant-Id": TENANT})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["report_id"].startswith("qc_")

    fetched = client.get(f"/technical-reports/{body['report_id']}",
                         headers={"X-Tenant-Id": TENANT})
    assert fetched.status_code == 200
    assert fetched.json()["payload"]["decision"] == "pass"

    other = client.get(f"/technical-reports/{body['report_id']}",
                       headers={"X-Tenant-Id": "tnt_b"})
    assert other.status_code == 404


def test_http_tenant_mismatch_in_body_is_403(tmp_path, monkeypatch):
    client, _harness = _client(tmp_path, monkeypatch)
    body = check_body(tenant_id="tnt_b")
    response = client.post("/technical-checks", json=body,
                           headers={"Idempotency-Key": "cmd_1",
                                    "X-Tenant-Id": TENANT})
    assert response.status_code == 403
    assert response.json()["code"] == "TENANT_MISMATCH"


def test_http_readiness_lists_gaps(tmp_path, monkeypatch):
    client, _harness = _client(tmp_path, monkeypatch)
    response = client.get("/health/readiness")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert {g["id"] for g in response.json()["contract_gaps"]} >= {"S35-1"}
