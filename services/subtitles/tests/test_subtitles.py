"""S38 subtitles 测试。方案 §10 的 E24 / E25 / V / E26，加上通用验收。

三条主线：

1. **"没查成"永远不能变成 pass。** 对齐 abstain、词典不可达、覆盖率不足，
   每一种都必须落 `abstain`，`failed_check_ids` 非空，且 manifest 里对应项
   不是 `evaluated`。
2. **三份文本三组版本，互不冒充。** `cue` 上原句 / 词典 / 译文各占一组列；
   只改其中一份时，另外两份的 `(id, revision, hash)` 逐字段不变。
3. **本服务不是第二权威。** 字节只在 S28（库里只有 `artifact_id + hash`），
   没有接受指针，`execution_fence` 与 `node_epoch` 各存各的，报告与事件都要
   过契约。

上游全部是闭包 stub：**不联网、不调真实模型、不调 TTS**。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pytest
from idempotency_sdk import (
    CommandConflict,
    SqliteIdempotencyStore,
)

from subtitles import schema
from subtitles.schema import CONTRACT_GAPS
from subtitles.service import (
    ServiceError,
    SubtitleService,
    stable_request_hash,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
SERVICES = ROOT / "services"

TENANT = "t1"
OTHER_TENANT = "t2"
HASH = "a" * 64
LEXICON_HASH = "d" * 64
LEXICON_HASH_2 = "e" * 64

CHECK_ID_PATTERN = re.compile(r"^SUB\.[A-Z0-9_]+\.[A-Z0-9_]+$")
DEFECT_ID_PATTERN = re.compile(r"^df_[a-z0-9]{26}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{32}$")
REPORT_ID_PATTERN = re.compile(r"^qc_[a-z0-9]{26}$")
TRACK_ID_PATTERN = re.compile(r"^trk_[a-z0-9]{26}$")

# 本服务自己拥有的表。多一张、少一张都要在这里显式改——这是"没有第二份
# 权威"最便宜的可证伪断言。
OWNED_TABLES = {
    "clip_ref", "cue", "cue_text", "inbox", "outbox", "pending_render",
    "qc_check", "qc_defect", "qc_report", "rendition", "track", "translation",
}

# 报告里绝不出现、库里也绝不出现的字幕正文字节特征（V6）。
SUBTITLE_BYTES_MARKERS = ("-->", "[Script Info]", "WEBVTT", "Dialogue:")


# ---------------------------------------------------------------- 夹具

def unit(index, char_offset, char_length, start, end):
    """S37 对齐单元。`ci_low/ci_high` 是 cue 边界的依据（§4.1）。"""
    return {"index": index, "char_offset": char_offset,
            "char_length": char_length, "start": start, "end": end,
            "ci_low": start, "ci_high": end}


BASE_LINES = [{"line_id": "ln1", "revision": 4,
               "text": "你终于来了今天天气很好"}]
BASE_UNITS = [unit(0, 0, 3, 1.00, 1.30), unit(1, 3, 3, 1.30, 1.70),
              unit(2, 6, 4, 1.70, 2.30)]

TWO_LINES = [{"line_id": "ln1", "revision": 4, "text": "你终于来了"},
             {"line_id": "ln2", "revision": 4, "text": "今天天气很好"}]


def localization(translation_id, status, text, line_id="ln1"):
    """命令里的译文（S38 拥有的那份文本）。"""
    return {"translations": [{
        "translation_id": translation_id, "revision": 1, "status": status,
        "lines": [{"line_id": line_id, "text": text}],
    }]}


class Harness:
    """内存库 + 六个 stub 上游。全部是闭包，**不碰网络**。"""

    def __init__(self, lines=None, units=None, *, clips=None,
                 dur_ticks=90000 * 30, decision="pass", archive=None,
                 lexicon=True, fonts=True, formats=None, enabled=True,
                 checks=None):
        self.lines = BASE_LINES if lines is None else lines
        self.units = BASE_UNITS if units is None else units
        self.clips = clips if clips is not None else [
            {"clip_id": "c1", "in_ticks": 0, "out_ticks": dur_ticks,
             "speed_ratio": 1.0}]
        self.dur_ticks = dur_ticks
        self.decision = decision
        self.archive_override = archive
        self.lexicon_doc = ({"id": "lex1", "revision": 2,
                             "content_hash": LEXICON_HASH, "lexicon": {}}
                            if lexicon else None)
        self.fonts = ["Noto Sans CJK SC"] if fonts else None
        self.formats = formats
        self.calls = {"align": 0, "timeline": 0, "dialogue": 0, "lexicon": 0,
                      "archive": 0, "url": 0}
        self.archive_requests = []
        self.conn = sqlite3.connect(":memory:", check_same_thread=False,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.service = SubtitleService(
            self.conn,
            alignment_source=self._align,
            timeline_source=self._timeline,
            dialogue_source=self._dialogue,
            lexicon_source=self._lexicon,
            archive_source=self._archive,
            artifact_url_source=self._url,
            enabled=enabled,
            enabled_checks=tuple(schema.CHECK_IDS) if checks is None else checks,
        )
        # 必须在任何 check() 之前构造：内核的 `_command` 会 UPDATE 幂等占位。
        self._idem = SqliteIdempotencyStore(self.conn)

    # ---- stubs
    def _align(self, tenant_id, ref, generation="latest"):
        self.calls["align"] += 1
        return {"report": {"decision": self.decision,
                           "payload": {"decision": self.decision}},
                "units": {"units": self.units}}

    def _timeline(self, tenant_id, ref):
        self.calls["timeline"] += 1
        return {"timeline_id": "tl_ep3", "revision": 9, "tick_rate": 90000,
                "duration_ticks": self.dur_ticks, "completeness": "complete",
                "clips": self.clips}

    def _dialogue(self, tenant_id, ref):
        self.calls["dialogue"] += 1
        return {"script_id": "shot_3_2", "revision": 4, "lines": self.lines}

    def _lexicon(self, tenant_id, ref):
        self.calls["lexicon"] += 1
        doc = self.lexicon_doc
        # 传进来是 callable 时按"上游行为"调用它（用它模拟不可达）。
        return doc(tenant_id, ref) if callable(doc) else doc

    def _archive(self, tenant_id, request):
        self.calls["archive"] += 1
        self.archive_requests.append(dict(request))
        if self.archive_override is not None:
            return self.archive_override(tenant_id, request)
        n = len(self.archive_requests)
        return {"artifact_id": "art_%020d" % n,
                "content_hash": request["content_hash"],
                "byte_size": len(request["content_b64"])}

    def _url(self, tenant_id, artifact_id):
        self.calls["url"] += 1
        return {"url": "https://s28.invalid/x",
                "expires_at": "2026-11-14T00:00:00Z"}

    def body(self, **overrides):
        """一份"坐标齐全"的命令体。租户在 `execution_context` 里。"""
        body = {
            "command_id": "cmd1",
            "execution_context": {
                "tenant_id": TENANT, "project_id": "p1", "series_id": "s1",
                "episode_id": "e1", "branch_id": "b1", "home_cell": "cell-a",
                # AGENTS.md：fence 是 run/branch 的，epoch 是单个 logical_node
                # 的。故意给两个不同的数，混用会立刻露馅。
                "execution_fence": 3, "node_epoch": 7, "node_id": "n1",
                "schema_version": "2.0", "home_region": "cn-east",
                "command_id": "cmd1", "request_hash": HASH,
                "actor": {"principal_id": "svc_orch",
                          "principal_type": "service",
                          "delegation_id": "dlg1"},
                "trace_id": "tr-1",
                "policy_ref": {"id": "pol1", "revision": 2,
                               "content_hash": "b" * 64},
                "manifest_ref": {"id": "man1", "revision": 4,
                                 "content_hash": "c" * 64},
            },
            "run_id": "run1", "shot_lineage_id": "lin1",
            "timeline_ref": {"service_id": "S41", "id": "tl_ep3",
                             "revision": 9},
            "alignment_ref": {"service_id": "S37", "id": "qc_abc",
                              "revision": 1, "content_hash": HASH},
            "dialogue_ref": {"service_id": "S06", "id": "shot_3_2",
                             "revision": 4},
            "target_locale": "zh-CN", "source_locale": "zh-CN",
            "profile": ({"fonts_available": self.fonts} if self.fonts
                        else {}),
            "lexicon_ref": {"id": "lex1", "revision": 2},
            "checks": {"include": ["SUB.*"]},
        }
        if self.formats:
            body["profile"]["format"] = self.formats
        body.update(overrides)
        return body

    # ---- 命令（与 api/app.py 的 command() 同形）
    def command(self, body=None, *, command_id="cmd1", tenant_id=TENANT):
        body = self.body() if body is None else body
        command_id = body.get("command_id", command_id)
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

    def run(self, **overrides):
        """方案里的 `h.run(**)`：构造 body 再走一遍命令。"""
        return self.command(self.body(**overrides))

    # ---- 断言助手
    def reports(self):
        return [json.loads(row[0]) for row in self.conn.execute(
            "SELECT payload_json FROM qc_report ORDER BY report_generation")]

    def report(self, index=-1):
        return self.reports()[index]

    def manifest(self, index=-1):
        return {entry["check_id"]: entry
                for entry in self.report(index)["check_manifest"]}

    def defects(self, index=-1):
        return self.report(index)["defects"]

    def defect(self, check_id, index=-1):
        found = [d for d in self.defects(index) if d["check_id"] == check_id]
        assert len(found) == 1, (check_id, found)
        return found[0]

    def check_ids(self, index=-1):
        return Counter(d["check_id"] for d in self.defects(index))

    def cues(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM cue ORDER BY cue_index")]

    def events(self):
        """outbox 由平台 SDK 建表，**没有 `id` 列**，列名是 `sequence`
        （envelope JSON 里也叫 `sequence`；统一改成契约要求的
        `aggregate_sequence` 见 `validate_event`）。"""
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

    def columns(self, table):
        return [row[1] for row in self.conn.execute(
            f"PRAGMA table_info({table})")]


# ---------------------------------------------------------------- 契约校验

def _load(relative):
    with open(CONTRACTS / relative, encoding="utf-8") as handle:
        return json.load(handle)


def _errors(document, relative):
    jsonschema = pytest.importorskip("jsonschema")
    schema_document = _load(relative)
    return [(list(error.absolute_path), error.message)
            for error in jsonschema.Draft202012Validator(
                schema_document).iter_errors(document)]


def validate_report(payload):
    assert _errors(payload, "quality/qc-report.v2.schema.json") == []


def validate_event(envelope):
    assert _errors(envelope["data"], "post.subtitle-ready.v2.schema.json") == []
    # envelope：平台 SDK 的列名是 `sequence`，契约要求 `aggregate_sequence`；
    # 另外两个键（`event_type` / `outbox_sequence`）是 harness 为可读性加的，
    # 不是 envelope 的一部分。先做这两步再校验，而不是改契约。
    document = {k: v for k, v in envelope.items()
                if k not in ("event_type", "outbox_sequence")}
    document["aggregate_sequence"] = document.pop("sequence")
    assert _errors(document, "event-envelope.v2.schema.json") == []

    assert document["type"] == "post.subtitle.ready"
    assert document["source"] == "S38"
    assert document["specversion"] == "1.0"
    assert document["subject"] == document["data"]["track_id"]
    assert document["aggregate_id"] == document["data"]["track_id"]
    assert document["aggregate_sequence"] == \
        document["data"]["subtitle_revision"]
    assert document["data_ref"]["service_id"] == "S38"
    assert document["data_ref"]["id"] == document["data"]["track_id"]
    assert document["data_ref"]["revision"] == \
        document["data"]["subtitle_revision"]


# ================================================================ 基线

def test_clean_track_passes_and_both_contracts_accept_it():
    """干净样本：24 项全查、0 缺陷、pass，报告与事件都过契约。"""
    harness = Harness()
    result = harness.command()
    payload = harness.report()

    assert result["replayed"] is False
    assert result["state"] == "archived"
    assert result["qc_decision"] == "pass"
    assert TRACK_ID_PATTERN.match(result["track_id"])
    assert REPORT_ID_PATTERN.match(result["qc_report_id"])
    assert result["cue_count"] == 1
    assert result["pending_formats"] == []
    assert result["formats"] == ["srt", "vtt", "ass"]
    assert payload["decision"] == "pass", payload["decision_reason"]
    assert payload["qc_layer"] == "subtitle"
    assert payload["confidence"] == 1.0
    assert payload["abstain"] is None
    assert payload["defects"] == []
    assert payload["report_content_hash"] == result["content_hash"]
    assert payload["coverage"]["coverage_ratio"] == 1.0
    assert payload["coverage"]["planned_windows"] == 1
    assert payload["coverage"]["temporal_windows_evaluated"] == 1
    assert payload["subject"]["artifact_kind"] == "subtitle_track"
    assert payload["context"]["policy_ref"]["service_id"] == "S02"
    assert payload["shot_ref"]["node_epoch"] == 7
    assert payload["context"]["execution_fence"] == 3
    validate_report(payload)

    events = harness.events()
    assert len(events) == 1
    validate_event(events[0])
    assert events[0]["data"]["qc_decision"] == "pass"
    assert events[0]["data"]["defect_count"] == 0
    assert events[0]["data"]["stale_translation_count"] == 0
    assert events[0]["data"]["formats"] == ["ass", "srt", "vtt"]

    cue = harness.cues()[0]
    assert (cue["start_seconds"], cue["end_seconds"]) == (0.92, 2.42)
    assert cue["display_text"] == "你终于来了今天天气很好"
    assert cue["node_epoch"] == 7
    assert result["artifacts"][0]["artifact_id"] == "art_00000000000000000001"


def test_manifest_covers_every_registered_check():
    harness = Harness()
    harness.command()
    manifest = harness.manifest()
    assert len(manifest) == len(schema.CHECK_IDS) == 24
    assert set(manifest) == set(schema.CHECK_IDS)
    # 没有字体/帧率坐标的两项必须落 not_applicable，而不是"通过"。
    assert manifest["SUB.TIMING.FRAME_BOUNDARY"] == {
        "check_id": "SUB.TIMING.FRAME_BOUNDARY", "status": "not_applicable",
        "reason_code": "policy_disabled"}
    assert manifest["SUB.LAYOUT.FONT_MISSING"]["status"] == "evaluated"
    assert schema.READY_STATES == ("rendered", "archived", "qc_passed",
                                   "repair_pending")


def test_every_check_id_conforms_to_the_pattern():
    for check_id in schema.CHECK_IDS:
        assert CHECK_ID_PATTERN.match(check_id), check_id


# ================================================================ E24 字幕缺陷

def test_E24_U1_overlapping_cues_are_a_blocker_covering_both():
    """E24-U1：两条 cue 重叠 30ms → blocker，time_range 覆盖两条。"""
    units = [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90),
             unit(2, 0, 4, 1.87, 2.20), unit(3, 4, 2, 2.20, 2.60)]
    harness = Harness(TWO_LINES, units)
    result = harness.run()

    first, second = harness.cues()
    assert (first["start_seconds"], first["end_seconds"]) == (0.92, 1.9)
    assert (second["start_seconds"], second["end_seconds"]) == (1.87, 2.72)

    defect = harness.defect("SUB.TIMING.OVERLAP")
    assert defect["severity"] == "blocker"
    assert defect["type"] == "subtitle"
    assert defect["scope"] == "window"
    assert defect["measured"] == {"value": pytest.approx(29.999999999999805),
                                  "unit": "ms", "threshold": 0.0,
                                  "comparator": "lte"}
    # 两条 cue 都被包进证据区间。
    assert defect["time_range"]["start_seconds"] == pytest.approx(
        first["start_seconds"])
    assert defect["time_range"]["end_seconds"] == pytest.approx(
        second["end_seconds"])
    assert first["cue_id"] in defect["observed"]
    assert second["cue_id"] in defect["observed"]
    # 原句是基准：expected_source 指 S06。
    assert defect["expected_source"]["service_id"] == "S06"
    assert defect["expected_source"]["ref_id"] == "shot_3_2"
    assert defect["expected_source"]["ref_revision"] == 4
    assert defect["recommended_actions"] == ["realign_subtitles",
                                             "re_render_subtitles"]
    assert DEFECT_ID_PATTERN.match(defect["defect_id"])
    assert FINGERPRINT_PATTERN.match(defect["defect_fingerprint"])
    assert defect["evidence_artifact_ids"] == ["art_00000000000000000001"]
    assert result["qc_decision"] == "repair"
    assert result["state"] == "repair_pending"
    assert harness.report()["confidence"] == 1.0
    assert harness.report()["dimensions"][0] == {
        "name": "timing", "score": 0.0, "reason": "1 defect(s)"}
    validate_report(harness.report())


def test_E24_U2_overlap_survives_ass_centisecond_rounding():
    """E24-U2：整秒级的区间在 ASS 厘秒取整后仍被复检检出。"""
    units = [unit(0, 0, 3, 1.000, 1.100), unit(1, 3, 3, 1.100, 1.104),
             unit(2, 6, 4, 1.104, 1.60), unit(3, 10, 2, 1.60, 1.90)]
    harness = Harness([{"line_id": "ln1", "revision": 4,
                        "text": "你终于来了今天天气很好"}], units)
    result = harness.run()

    assert result["qc_decision"] == "repair"
    assert result["state"] == "repair_pending"
    defects = harness.defects()
    # 4.0ms 的区间缩小不到一个厘秒，取整后两端相同 → 复检必须看见。
    ass = [d for d in defects if d["measured"]["unit"] == "seconds"]
    assert len(ass) == 1, defects
    assert ass[0]["check_id"] == "SUB.TIMING.OVERLAP"
    assert ass[0]["measured"] == {"value": 0.0, "unit": "seconds",
                                  "threshold": 0.0, "comparator": "gt"}
    assert ass[0]["observed"].startswith("after ASS rounding")
    # 且非取整口径的 OVERLAP 也还在：两条路径互不替代。
    # `unit == "ms"` 不是 OVERLAP 专属（TOO_SHORT 也用毫秒），必须两个条件
    # 一起筛，否则会把短时长缺陷数进重叠里。
    ms = [d for d in defects
          if d["check_id"] == "SUB.TIMING.OVERLAP"
          and d["measured"]["unit"] == "ms"]
    assert len(ms) == 3
    assert harness.check_ids()["SUB.TIMING.OVERLAP"] == 4


def test_E24_U3_fifteen_chars_in_600ms_exceeds_cps():
    """E24-U3：15 字挤在 ~600ms 里 → CPS_EXCEEDED major（并连带短时长）。"""
    text = "一二三四五六七八九十甲乙丙丁戊"
    units = [unit(i, i, 1, 1.0 + i * 0.04, 1.04 + i * 0.04)
             for i in range(15)]
    harness = Harness([{"line_id": "ln1", "revision": 4, "text": text}],
                      units)
    result = harness.run()

    assert result["qc_decision"] == "human_review"
    assert len(harness.cues()) == 8
    counts = harness.check_ids()
    assert counts["SUB.TIMING.CPS_EXCEEDED"] == 6
    assert counts["SUB.TIMING.TOO_SHORT"] == 7
    cps = [d for d in harness.defects()
           if d["check_id"] == "SUB.TIMING.CPS_EXCEEDED"]
    assert all(d["severity"] == "major" for d in cps)
    assert cps[0]["measured"]["unit"] == "chars_per_second"
    assert cps[0]["measured"]["threshold"] == 9.0
    assert cps[0]["measured"]["value"] > 9.0
    # 语速是"绝不自动修"的：自动缩字正是方案 §4.3 禁止的静默缩字。
    assert "SUB.TIMING.CPS_EXCEEDED" in schema.NEVER_AUTO
    assert harness.manifest()["SUB.TIMING.CPS_EXCEEDED"]["status"] == \
        "evaluated"


def test_E24_U4_a_line_that_cannot_fit_is_line_overflow_and_human_review():
    """E24-U4：先按行宽切分，切不了仍溢出 → LINE_OVERFLOW + human_review。

    方案原文写"40 字"；这里用 60 个 ASCII 字符，因为切分的最小片段是 6 字符，
    40 字会被切成两行、不再溢出。要证明"切完仍溢出"必须跨过两行上限。
    """
    text = "a" * 60
    units = [unit(0, 0, 20, 1.0, 2.0), unit(1, 20, 20, 2.0, 3.0),
             unit(2, 40, 20, 3.0, 4.0)]
    harness = Harness([{"line_id": "ln1", "revision": 4, "text": text}],
                      units)
    result = harness.run(target_locale="en-US", source_locale="en-US")

    assert result["qc_decision"] == "human_review"
    assert result["state"] == "repair_pending"
    cue = harness.cues()[0]
    assert cue["line_count"] == 1          # 一个"词"，没有可切点
    assert cue["max_chars_per_line"] == 60
    overflow = harness.defect("SUB.LAYOUT.LINE_OVERFLOW")
    assert overflow["severity"] == "major"
    assert overflow["measured"] == {"value": 60.0, "unit": "characters",
                                    "threshold": 42.0, "comparator": "lte"}
    cps = harness.defect("SUB.TIMING.CPS_EXCEEDED")
    assert cps["measured"]["threshold"] == 17.0
    assert harness.check_ids() == Counter({
        "SUB.LAYOUT.LINE_OVERFLOW": 1, "SUB.TIMING.CPS_EXCEEDED": 1})


def test_E24_U5_source_text_moved_ahead_is_a_blocker():
    """E24-U5：原句已到 V4，这条字幕还按 V3 排 → SOURCE_STALE blocker。"""
    class StaleDialogue(Harness):
        def _dialogue(self, tenant_id, ref):
            self.calls["dialogue"] += 1
            return {"script_id": "shot_3_2", "revision": 4,
                    "lines": [{"line_id": "ln1", "revision": 3,
                               "text": "你终于来了"}]}

    harness = StaleDialogue(
        [{"line_id": "ln1", "revision": 3, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.30), unit(1, 3, 3, 1.30, 1.70)])
    result = harness.run()

    assert result["qc_decision"] == "human_review"
    defect = harness.defect("SUB.TEXT.SOURCE_STALE")
    assert defect["severity"] == "blocker"
    assert defect["scope"] == "track"
    assert defect["measured"] == {"value": 3.0, "unit": "revision",
                                  "threshold": 4.0, "comparator": "gte"}
    assert defect["expected_source"]["ref_revision"] == 4
    assert "SUB.TEXT.SOURCE_STALE" in schema.NEVER_AUTO
    assert harness.report()["dimensions"][2] == {
        "name": "text", "score": 0.0, "reason": "1 defect(s)"}


def test_E24_U6_missing_translation_is_a_blocker_per_cue():
    """E24-U6：target≠source 且没有译文 → 每个 cue 一条 TRANSLATION_MISSING。"""
    units = [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90),
             unit(2, 0, 4, 2.10, 2.55), unit(3, 4, 2, 2.55, 2.95)]
    harness = Harness(TWO_LINES, units)
    result = harness.run(target_locale="en-US", source_locale="zh-CN")

    assert result["qc_decision"] == "human_review"
    assert result["stale_translation_count"] == 0
    missing = [d for d in harness.defects()
               if d["check_id"] == "SUB.TEXT.TRANSLATION_MISSING"]
    assert len(missing) == 2
    windows = sorted((d["time_range"]["start_seconds"],
                      d["time_range"]["end_seconds"]) for d in missing)
    assert windows == [(0.92, 2.02), (2.02, 3.07)]
    for defect in missing:
        assert defect["severity"] == "blocker"
        assert defect["measured"] == {"value": 0.0, "unit": "translations",
                                      "threshold": 1.0, "comparator": "gte"}
    assert harness.report()["dimensions"][2]["reason"] == "2 defect(s)"


def test_E24_U7_cue_past_the_shrunk_timeline_is_out_of_range():
    """E24-U7：EDL 只剩 2s，cue 结尾落在 3.253s → OUT_OF_RANGE blocker。"""
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 2.5, 2.9), unit(1, 3, 3, 2.9, 3.6)],
        dur_ticks=90000 * 2)
    result = harness.run()

    assert result["qc_decision"] == "repair"
    cue = harness.cues()[0]
    assert (cue["start_seconds"], cue["end_seconds"]) == (2.42, 3.253)
    defect = harness.defect("SUB.TIMING.OUT_OF_RANGE")
    assert defect["severity"] == "blocker"
    assert defect["measured"] == {
        "value": pytest.approx(1.2530000000000001), "unit": "seconds",
        "threshold": 0.0, "comparator": "lte"}
    assert defect["time_range"]["start_seconds"] == pytest.approx(2.42)
    assert defect["time_range"]["end_seconds"] == pytest.approx(3.253)
    drift = harness.defect("SUB.TIMING.DRIFT_VS_ALIGNMENT")
    assert drift["measured"]["value"] == pytest.approx(347.0)
    assert drift["expected_source"]["service_id"] == "S37"


def _two_clip_harness():
    units = [unit(0, 0, 3, 1.00, 1.60), unit(1, 3, 3, 1.60, 2.10),
             unit(2, 0, 4, 11.00, 11.60), unit(3, 4, 2, 11.60, 12.10)]
    clips = [{"clip_id": "c1", "in_ticks": 0, "out_ticks": 900000,
              "speed_ratio": 1.0},
             {"clip_id": "c2", "in_ticks": 0, "out_ticks": 900000,
              "speed_ratio": 1.0}]
    return Harness(TWO_LINES, units, clips=clips, dur_ticks=90000 * 20)


def test_E24_U8_only_cues_of_the_changed_clip_move():
    """E24-U8：EDL 只改 c2 的速度 → c1 的行逐字段不变，c2 的时间码重算。"""
    harness = _two_clip_harness()
    result = harness.run()
    before = {cue["cue_id"]: dict(cue) for cue in harness.cues()}
    assert [cue["clip_id"] for cue in harness.cues()] == ["c1", "c2"]

    harness.clips = [{"clip_id": "c1", "in_ticks": 0, "out_ticks": 900000,
                      "speed_ratio": 1.0},
                     {"clip_id": "c2", "in_ticks": 0, "out_ticks": 900000,
                      "speed_ratio": 2.0}]
    timeline_event = {
        "id": "ev-tl-1", "type": schema.EVENT_TIMELINE,
        "aggregate_id": "tl_ep3", "sequence": 10, "tenant_id": TENANT,
        "data": {"timeline_id": "tl_ep3", "revision": 10}}
    outcome = harness.service.consume_timeline_revision_event(
        TENANT, timeline_event)
    assert outcome == {"duplicate": False, "affected_cues": 1,
                       "changed_clips": ["c2"]}

    after = {cue["cue_id"]: dict(cue) for cue in harness.cues()}
    changed = [cue_id for cue_id in before
               if before[cue_id] != after[cue_id]]
    assert len(changed) == 1
    moved = changed[0]
    assert before[moved]["clip_id"] == "c2"
    assert [field for field in before[moved]
            if before[moved][field] != after[moved][field]] == \
        ["start_seconds", "end_seconds"]
    assert (after[moved]["start_seconds"],
            after[moved]["end_seconds"]) == (15.92, 16.753)
    untouched = [cue_id for cue_id, cue in before.items()
                 if cue["clip_id"] == "c1"][0]
    assert before[untouched] == after[untouched]

    track = harness.service.get_track(TENANT, result["track_id"])
    assert track["subtitle_revision"] == 2
    assert track["state"] == "draft"
    assert track["supersedes_revision"] == 1
    assert track["timeline_ref"]["revision"] == 10
    # 重算不产生新报告：旧字节不再对应新轨道，但也不该假装又校验过。
    assert harness.count("qc_report") == 1


def test_E24_U8_duplicate_and_stale_timeline_events_change_nothing():
    harness = _two_clip_harness()
    result = harness.run()
    event = {"id": "ev-tl-1", "type": schema.EVENT_TIMELINE,
             "aggregate_id": "tl_ep3", "sequence": 10, "tenant_id": TENANT,
             "data": {"timeline_id": "tl_ep3", "revision": 10}}
    harness.clips = [{"clip_id": "c1", "in_ticks": 0, "out_ticks": 900000,
                      "speed_ratio": 1.0},
                     {"clip_id": "c2", "in_ticks": 0, "out_ticks": 900000,
                      "speed_ratio": 2.0}]
    assert harness.service.consume_timeline_revision_event(
        TENANT, event)["affected_cues"] == 1
    snapshot = harness.cues()

    duplicate = harness.service.consume_timeline_revision_event(TENANT, event)
    assert duplicate == {"duplicate": True, "affected_cues": 0}
    assert harness.cues() == snapshot
    assert harness.count("track") == 1

    older = dict(event, id="ev-tl-0", sequence=9,
                 data={"timeline_id": "tl_ep3", "revision": 9})
    stale = harness.service.consume_timeline_revision_event(TENANT, older)
    assert stale == {"duplicate": False, "affected_cues": 0,
                     "changed_clips": []}
    assert harness.cues() == snapshot
    assert harness.service.get_track(
        TENANT, result["track_id"])["timeline_ref"]["revision"] == 10


def test_E24_U8_timeline_unreachable_does_not_burn_the_event():
    """先回读 S41 再落 inbox：503 之后重投必须仍能生效。"""
    harness = _two_clip_harness()
    result = harness.run()
    before = harness.count("inbox")

    def down(_tenant, _ref):
        raise ConnectionError("S41 down")

    harness.service.timeline_source = down
    event = {"id": "ev-tl-9", "type": schema.EVENT_TIMELINE,
             "aggregate_id": "tl_ep3", "sequence": 11, "tenant_id": TENANT,
             "data": {"timeline_id": "tl_ep3", "revision": 11}}
    with pytest.raises(ServiceError) as caught:
        harness.service.consume_timeline_revision_event(TENANT, event)
    assert (caught.value.status, caught.value.code) == \
        (503, "TIMELINE_UNAVAILABLE")
    assert harness.count("inbox") == before
    assert harness.service.get_track(
        TENANT, result["track_id"])["timeline_ref"]["revision"] == 9


def test_E24_U9_repair_rerun_rechecks_and_passes():
    """E24-U9：修完时码复检 → pass，`repair_context` 指向 slot。"""
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了今天天气很好"}],
        [unit(0, 0, 3, 1.00, 1.05), unit(1, 3, 3, 1.05, 1.10),
         unit(2, 6, 4, 1.10, 1.16)])
    first = harness.run()
    assert first["qc_decision"] == "repair"
    assert first["subtitle_revision"] == 1
    assert first["report_generation"] == 1
    assert len(harness.defects()) > 0

    harness.units = [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90),
                     unit(2, 6, 4, 1.90, 2.50)]
    repair = {"repair_plan_id": "rp_1", "attempt_slot_id": "slot_7",
              "attempt_seq": 1}
    second = harness.run(command_id="cmd2", repair_context=repair)
    assert second["qc_decision"] == "pass"
    assert second["state"] == "archived"
    assert second["subtitle_revision"] == 2
    assert second["report_generation"] == 1
    payload = harness.report()
    assert payload["decision"] == "pass"
    assert payload["defects"] == []
    assert payload["repair_context"] == {
        "attempt_seq": 1, "attempt_slot_id": "slot_7",
        "defect_fingerprints": [], "repair_plan_id": "rp_1"}
    # 第二次是另一个时码，不是同一 revision 的下一代：不复述 supersedes。
    assert payload["supersedes"] is None
    assert harness.count("qc_report") == 2
    assert len({report["report_id"] for report in harness.reports()}) == 2
    validate_report(payload)


def test_E24_U10_burned_in_subtitles_are_S35s_check_not_ours():
    """E24-U10：烧录字幕由 S35 的 TECH.SUBTITLE.BURNED_IN 报，S38 不重复报。"""
    assert not [check_id for check_id in schema.CHECK_IDS
                if "BURN" in check_id]
    assert sorted(check_id for check_id in schema.CHECK_IDS
                  if check_id.startswith("SUB.FORMAT.")) == [
        "SUB.FORMAT.ASS_STYLE_MISSING", "SUB.FORMAT.BOM",
        "SUB.FORMAT.ENCODING", "SUB.FORMAT.SRT_SYNTAX"]
    harness = Harness()
    harness.command()
    assert "TECH.SUBTITLE.BURNED_IN" not in harness.manifest()
    assert all(entry["check_id"].startswith("SUB.")
               for entry in harness.report()["check_manifest"])


# ================================================================ E25 超时与冲突

def test_E25_U1_alignment_abstain_renders_a_draft_but_never_passes():
    """E25-U1：S37 abstain → 仍出草稿，但 `qc_decision=abstain`，不标 qc_passed。"""
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)],
        decision="abstain")
    result = harness.run()

    assert result["qc_decision"] == "abstain"
    assert result["state"] == "rendered"
    assert result["state"] not in ("accepted", "qc_passed")
    assert result["cue_count"] == 1
    payload = harness.report()
    assert payload["confidence"] == 0.0
    assert payload["abstain"] == {
        "reason_code": "dependency_stale",
        "failed_check_ids": sorted(schema.NEEDS_ALIGNMENT),
        "detail": "6 required check(s) did not conclude"}
    manifest = harness.manifest()
    for check_id in schema.NEEDS_ALIGNMENT:
        assert manifest[check_id]["status"] == "not_evaluated"
        assert manifest[check_id]["reason_code"] == "dependency_unavailable"
    assert payload["dimensions"][0] == {
        "name": "timing", "score": None,
        "reason": "some checks did not conclude"}
    event = harness.events()[0]
    assert event["data"]["qc_decision"] == "abstain"
    assert event["data"]["defect_count"] == 0
    validate_report(payload)


def test_E25_U2_partial_translation_never_reads_as_a_pass():
    """E25-U2：译文作业只回来一部分 → 缺的那条是 blocker，且不填空串。"""
    units = [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90),
             unit(2, 0, 4, 2.10, 2.55), unit(3, 4, 2, 2.55, 2.95)]
    harness = Harness(TWO_LINES, units)
    result = harness.run(
        target_locale="en-US", source_locale="zh-CN",
        localization=localization("trn_p", "reviewed", "you finally came"))

    assert result["qc_decision"] != "pass"
    assert result["state"] != "archived"
    assert result["qc_decision"] == "human_review"
    missing = harness.defect("SUB.TEXT.TRANSLATION_MISSING")
    assert missing["severity"] == "blocker"
    assert missing["time_range"] == {"start_seconds": 2.02,
                                     "end_seconds": 3.07}
    cues = harness.cues()
    # 第二条没有译文：保留原句可预览，绝不写空串冒充译文。
    assert cues[1]["display_text"] == "今天天气很好"
    assert cues[1]["translation_id"] is None
    assert cues[0]["display_text"] == "you finally came"


def test_E25_U3_lexicon_unavailable_abstains_on_that_check_only():
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)],
        lexicon=False)
    result = harness.run()

    assert result["qc_decision"] == "abstain"
    assert result["state"] == "rendered"
    payload = harness.report()
    assert payload["confidence"] == 0.0
    assert harness.manifest()["SUB.TEXT.LEXICON_UNUSED"] == {
        "check_id": "SUB.TEXT.LEXICON_UNUSED", "status": "not_evaluated",
        "reason_code": "dependency_unavailable"}
    assert payload["abstain"] == {
        "reason_code": "dependency_stale",
        "failed_check_ids": ["SUB.TEXT.LEXICON_UNUSED"],
        "detail": "1 required check(s) did not conclude"}
    assert payload["dimensions"][2] == {
        "name": "text", "score": None,
        "reason": "some checks did not conclude"}


def test_E25_U4_seventy_percent_coverage_abstains():
    """E25-U4：只覆盖 7/10 条 cue → coverage 0.70 → abstain。"""
    lines = [{"line_id": "ln%d" % i, "revision": 4,
              "text": "第%d句台词内容" % i} for i in range(1, 11)]
    units = []
    for i in range(7):
        start = 1.0 + i * 1.4
        units += [unit(2 * i, 0, 2, start, start + 0.5),
                  unit(2 * i + 1, 2, 4, start + 0.5, start + 1.0)]
    harness = Harness(lines, units, dur_ticks=90000 * 40)
    result = harness.run()

    assert result["qc_decision"] == "abstain"
    assert result["state"] == "rendered"
    assert result["cue_count"] == 7
    payload = harness.report()
    coverage = payload["coverage"]
    assert coverage["coverage_ratio"] == pytest.approx(0.70)
    assert coverage["planned_windows"] == 10
    assert coverage["temporal_windows_evaluated"] == 7
    assert coverage["required_coverage_min"] == pytest.approx(0.95)
    assert coverage["media_duration_seconds"] == pytest.approx(40.0)
    assert payload["abstain"]["reason_code"] == "incomplete_coverage"
    assert "0.700" in payload["abstain"]["detail"]
    assert "0.950" in payload["abstain"]["detail"]
    # 覆盖不足时**全部**检查都算"没查成"：只看过 70% 的轨道，结论不可采信。
    assert payload["abstain"]["failed_check_ids"] == sorted(schema.CHECK_IDS)
    assert payload["confidence"] == 1.0
    validate_report(payload)


def test_E25_U5_archive_outage_keeps_the_track_pending_then_retries():
    """E25-U5：S28 超时 → 停在 rendered_pending_archive；恢复后补发且不重渲染。"""
    down = lambda *args: (_ for _ in ()).throw(ConnectionError("S28 down"))
    units = [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90),
             unit(2, 0, 4, 1.87, 2.20), unit(3, 4, 2, 2.20, 2.60)]
    harness = Harness(TWO_LINES, units, archive=down)
    result = harness.run()

    assert result["state"] == "rendered_pending_archive"
    assert result["qc_decision"] == "repair"
    assert result["pending_formats"] == ["srt", "vtt", "ass"]
    assert result["artifacts"] == []
    assert harness.count("outbox") == 0      # 不发 post.subtitle.ready
    assert harness.count("rendition") == 0
    pending = {row["format"]: dict(row) for row in harness.conn.execute(
        "SELECT * FROM pending_render")}
    assert set(pending) == {"srt", "vtt", "ass"}
    for row in pending.values():
        assert row["byte_size"] > 0
        assert row["attempts"] == 0
        assert row["last_error"] == "unreachable: S28 down"
    assert "content_b64" not in harness.columns("pending_render")

    with pytest.raises(ServiceError) as caught:
        harness.service.get_rendition_url(TENANT, result["track_id"], 1, "srt")
    assert (caught.value.status, caught.value.code) == \
        (409, "RENDITION_NOT_ARCHIVED")
    assert caught.value.retryable is True
    assert caught.value.details["last_error"] == "unreachable: S28 down"

    harness.archive_override = None
    retried = harness.service.retry_pending_archive(
        TENANT, result["track_id"])
    # 归档完成不等于放行：这条轨本身是 repair（cue 重叠），所以补档后
    # 落回 `repair_pending`，不是 `archived`。归档是"字节到位"，修复是另一回事。
    assert retried["state"] == "repair_pending"
    assert retried["archived_formats"] == ["ass", "srt", "vtt"]
    assert retried["pending_formats"] == []
    assert retried["event_emitted"] is True
    assert harness.count("outbox") == 1
    assert harness.count("pending_render") == 0
    assert harness.count("rendition") == 3
    # 重试归档的字节与第一次渲染的摘要一致：没有重新渲染出第二份。
    sizes = {row["format"]: row["byte_size"] for row in harness.conn.execute(
        "SELECT format, byte_size FROM rendition")}
    assert sizes == {fmt: row["byte_size"] for fmt, row in pending.items()}

    again = harness.service.retry_pending_archive(TENANT, result["track_id"])
    assert again == {"track_id": result["track_id"], "subtitle_revision": 1,
                     "archived_formats": [], "pending_formats": [],
                     "event_emitted": False}
    validate_event(harness.events()[0])


def test_E25_U5_retry_refuses_to_archive_bytes_it_cannot_reproduce():
    down = lambda *args: (_ for _ in ()).throw(ConnectionError("S28 down"))
    units = [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90),
             unit(2, 0, 4, 1.87, 2.20), unit(3, 4, 2, 2.20, 2.60)]
    harness = Harness(TWO_LINES, units, archive=down)
    result = harness.run()
    harness.conn.execute(
        "UPDATE pending_render SET content_hash=? WHERE format='srt'",
        ("f" * 64,))
    harness.archive_override = None

    with pytest.raises(ServiceError) as caught:
        harness.service.retry_pending_archive(TENANT, result["track_id"])
    assert (caught.value.status, caught.value.code) == \
        (500, "RENDER_NOT_DETERMINISTIC")
    # 一份都没归档，也没有事件：不拿"另一次渲染"冒充原来那份。
    assert harness.count("rendition") == 0
    assert harness.count("outbox") == 0


def test_archive_contract_violations_are_502_not_silent_success():
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)],
        archive=lambda tenant, request: {"artifact_id": "not-an-artifact",
                                         "content_hash":
                                             request["content_hash"],
                                         "byte_size": 10})
    with pytest.raises(ServiceError) as caught:
        harness.run()
    assert (caught.value.status, caught.value.code) == \
        (502, "ARCHIVE_BAD_RESPONSE")
    assert harness.count("track") == 0

    harness2 = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)],
        archive=lambda tenant, request: {"artifact_id":
                                             "art_00000000000000000001",
                                         "content_hash": "b" * 64,
                                         "byte_size": 10})
    with pytest.raises(ServiceError) as caught:
        harness2.run()
    assert (caught.value.status, caught.value.code) == \
        (502, "ARTIFACT_HASH_MISMATCH")
    assert caught.value.details["stored_hash"] == "b" * 64


# ================================================================ 版本独立性

def _triple(harness):
    """一条 cue 上的三组 (id, revision, hash) + 派生文本。"""
    cue = harness.cues()[0]
    return {
        "source_text": (cue["source_text_revision"], cue["source_text_hash"]),
        "lexicon": (cue["lexicon_ref_id"], cue["lexicon_revision"],
                    cue["lexicon_hash"]),
        "translation": (cue["translation_id"], cue["translation_revision"],
                        cue["translation_hash"]),
        "display_text": cue["display_text"],
    }


EN_LOCALIZATION = localization("trn_a", "reviewed", "you finally came")


def test_V1_only_the_source_revision_moves_and_translations_go_stale():
    """V1：只改原句 revision → 原句那组变；另外两组不动；译文标 stale。"""
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)])
    harness.run(target_locale="en-US", source_locale="zh-CN",
                localization=EN_LOCALIZATION)
    before = _triple(harness)
    assert before["source_text"][0] == 4
    assert before["lexicon"][1] == 2
    assert before["translation"][0] == "trn_a"
    assert harness.conn.execute(
        "SELECT status FROM translation").fetchone()[0] == "reviewed"

    event = {"id": "ev-s-1", "type": schema.EVENT_SCRIPT,
             "aggregate_id": "shot_3_2", "sequence": 7, "tenant_id": TENANT,
             "data": {"script_id": "shot_3_2", "revision": 5}}
    outcome = harness.service.consume_script_revision_event(TENANT, event)
    assert outcome == {"duplicate": False, "tracks": 1,
                       "translations_marked_stale": 1}
    after = _triple(harness)
    assert after["source_text"][0] == 5
    # 词典与译文两组逐字段不变——包括 hash。
    assert after["lexicon"] == before["lexicon"]
    assert after["translation"] == before["translation"]
    # 文本保留（可预览），但状态是 stale：不能正式交付。
    assert after["display_text"] == before["display_text"]
    assert harness.conn.execute(
        "SELECT status FROM translation").fetchone()[0] == "stale"
    track = harness.service.get_track(
        TENANT, harness.cues()[0]["track_id"])
    assert track["dialogue_ref"]["revision"] == 5


def test_V1_duplicate_and_older_script_events_are_no_ops():
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)])
    harness.run()
    event = {"id": "ev-s-1", "type": schema.EVENT_SCRIPT,
             "aggregate_id": "shot_3_2", "sequence": 7, "tenant_id": TENANT,
             "data": {"script_id": "shot_3_2", "revision": 5}}
    harness.service.consume_script_revision_event(TENANT, event)
    snapshot = harness.cues()

    duplicate = harness.service.consume_script_revision_event(TENANT, event)
    assert duplicate == {"duplicate": True, "tracks": 0,
                         "translations_marked_stale": 0}
    older = dict(event, id="ev-s-0", sequence=6,
                 data={"script_id": "shot_3_2", "revision": 4})
    stale = harness.service.consume_script_revision_event(TENANT, older)
    assert stale == {"duplicate": False, "tracks": 0,
                     "translations_marked_stale": 0}
    assert harness.cues() == snapshot
    assert harness.count("inbox") == 2


def test_V2_only_the_lexicon_revision_moves():
    """V2：只改词典 revision → 只有 lexicon 那组变；译文组与状态不变。"""
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)])
    harness.run(target_locale="en-US", source_locale="zh-CN",
                localization=EN_LOCALIZATION)
    before = _triple(harness)
    harness.lexicon_doc = {"id": "lex1", "revision": 3,
                           "content_hash": LEXICON_HASH_2,
                           "lexicon": {"终": "zhong"}}
    result = harness.run(command_id="cmd2", target_locale="en-US",
                         source_locale="zh-CN",
                         localization=EN_LOCALIZATION)
    after = _triple(harness)
    assert after["lexicon"] == ("lex1", 3, LEXICON_HASH_2)
    assert after["source_text"] == before["source_text"]
    assert after["translation"] == before["translation"]
    assert harness.conn.execute(
        "SELECT status FROM translation").fetchone()[0] == "reviewed"
    assert result["qc_decision"] in ("pass", "human_review", "abstain",
                                     "repair")


def test_V3_only_the_translation_revision_moves_and_status_follows_review():
    """V3：只改译文 → 前三者不变；draft → reviewed 是显式的状态转换。"""
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)])
    draft = localization("trn_c", "draft", "you finally came")
    first = harness.run(target_locale="en-US", source_locale="zh-CN",
                        localization=draft)
    assert first["qc_decision"] == "human_review"
    before = _triple(harness)
    assert before["translation"][0] == "trn_c"
    assert set(harness.check_ids()) == {"SUB.TEXT.TRANSLATION_DRAFT",
                                        "SUB.TEXT.TRANSLATION_LENGTH_DRIFT"}

    moved = harness.service.mark_translation_reviewed(
        TENANT, "trn_c", 1, {"principal_id": "rv"})
    assert moved == {"translation_id": "trn_c", "revision": 1,
                     "status": "reviewed"}
    second = harness.run(command_id="cmd2", target_locale="en-US",
                         source_locale="zh-CN", localization=draft)
    after = _triple(harness)
    assert after["source_text"] == before["source_text"]
    assert after["lexicon"] == before["lexicon"]
    assert after["translation"] == before["translation"]
    # 人工验收后只剩长度漂移；DRAFT 消失，但 DB 状态以库为准（reviewed）。
    assert harness.check_ids() == Counter(
        {"SUB.TEXT.TRANSLATION_LENGTH_DRIFT": 1})
    assert harness.conn.execute(
        "SELECT status FROM translation").fetchone()[0] == "reviewed"
    assert second["qc_decision"] == "human_review"


def test_translation_transitions_are_explicit_and_orderly():
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)])
    harness.run(target_locale="en-US", source_locale="zh-CN",
                localization=EN_LOCALIZATION)

    accepted = harness.service.mark_translation_accepted(
        TENANT, "trn_a", 1, {"principal_id": "rv"})
    assert accepted == {"translation_id": "trn_a", "revision": 1,
                        "status": "accepted"}
    for target, status in (("accepted", "accepted"),
                           ("reviewed", "accepted")):
        with pytest.raises(ServiceError) as caught:
            if target == "accepted":
                harness.service.mark_translation_accepted(
                    TENANT, "trn_a", 1, {"principal_id": "rv"})
            else:
                harness.service.mark_translation_reviewed(
                    TENANT, "trn_a", 1, {"principal_id": "rv"})
        assert (caught.value.status, caught.value.code) == \
            (409, "INVALID_TRANSLATION_TRANSITION")
        assert status in str(caught.value)

    with pytest.raises(ServiceError) as caught:
        harness.service.mark_translation_reviewed(
            TENANT, "trn_missing", 1, {"principal_id": "rv"})
    assert (caught.value.status, caught.value.code) == \
        (404, "TRANSLATION_NOT_FOUND")


def test_V4_cue_keeps_three_version_groups_and_no_merged_text_column():
    harness = Harness()
    harness.command()
    columns = harness.columns("cue")
    assert len(columns) == 24
    assert "text" not in columns
    for prefix in ("source_text_", "lexicon_", "translation_"):
        assert {prefix + suffix for suffix in ("revision", "hash")} <= \
            set(columns), prefix
    assert {"lexicon_ref_id", "translation_id"} <= set(columns)
    assert "display_text" in columns
    # 三组是独立的列，不是打包 JSON。
    assert not [name for name in columns if name.endswith("_json")]


def test_V5_audit_can_locate_all_three_sources_for_one_cue():
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)])
    result = harness.run(target_locale="en-US", source_locale="zh-CN",
                         localization=EN_LOCALIZATION)
    cue = harness.service.get_track(TENANT, result["track_id"])["cues"][0]

    assert cue["source_text"]["service_id"] == "S06"
    assert cue["source_text"]["revision"] == 4
    assert re.fullmatch(r"[0-9a-f]{64}", cue["source_text"]["content_hash"])
    assert cue["lexicon"] == {"service_id": "S15", "id": "lex1",
                              "revision": 2,
                              "content_hash": LEXICON_HASH}
    assert cue["translation"]["service_id"] == "S38"
    assert cue["translation"]["id"] == "trn_a"
    assert cue["translation"]["revision"] == 1
    assert cue["translation"]["status"] == "reviewed"
    assert cue["display_text"] == "you finally came"
    assert cue["safe_area_ok"] is True
    assert cue["node_epoch"] == 7
    assert cue["alignment_report_id"] == "qc_abc"


def test_V6_no_subtitle_bytes_are_stored_anywhere():
    """V6：扫全库所有列，没有任何 .srt/.vtt/.ass 正文。"""
    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)])
    harness.run(target_locale="en-US", source_locale="zh-CN",
                localization=EN_LOCALIZATION)
    tables = [row["name"] for row in harness.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    assert harness.count("rendition") == 3
    for table in tables:
        for column in harness.columns(table):
            for marker in SUBTITLE_BYTES_MARKERS:
                hits = harness.conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                    f" WHERE CAST({column} AS TEXT) LIKE ?",
                    (f"%{marker}%",)).fetchone()[0]
                assert hits == 0, (table, column, marker, hits)
    # 归档请求里有字节（要交给 S28），但库里只留指针与摘要。
    assert all("content_b64" in request
               for request in harness.archive_requests)
    assert "content_b64" not in harness.columns("rendition")
    assert "content_b64" not in harness.columns("pending_render")
    assert set(harness.columns("rendition")) == {
        "track_id", "subtitle_revision", "format", "artifact_id",
        "content_hash", "byte_size", "rendered_at"}


# ================================================================ E26 跨 Run

def test_E26_U1_rerun_bumps_the_track_revision_not_the_lineage():
    harness = Harness()
    first = harness.run()
    second = harness.run(command_id="cmd2")

    assert first["track_id"] == second["track_id"]
    assert first["subtitle_revision"] == 1
    assert second["subtitle_revision"] == 2
    assert first["qc_report_id"] != second["qc_report_id"]
    track = harness.service.get_track(TENANT, first["track_id"])
    assert track["supersedes_revision"] == 1
    assert harness.report(-1)["context"]["shot_lineage_id"] == "lin1"
    assert harness.count("qc_report") == 2
    assert harness.count("track") == 1
    # 两代报告都在：审计要能回看上一代。
    assert harness.report(0)["report_id"] == first["qc_report_id"]


def test_E26_U2_the_same_defect_keeps_its_fingerprint_across_runs():
    def fingerprints():
        units = [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90),
                 unit(2, 0, 4, 1.87, 2.20), unit(3, 4, 2, 2.20, 2.60)]
        harness = Harness(TWO_LINES, units)
        harness.run()
        return [(d["check_id"], d["defect_fingerprint"], d["defect_id"])
                for d in harness.defects()]

    first, second = fingerprints(), fingerprints()
    assert first == second
    assert first[0][0] == "SUB.TIMING.OVERLAP"
    assert FINGERPRINT_PATTERN.match(first[0][1])
    assert DEFECT_ID_PATTERN.match(first[0][2])
    # 指纹只描述"同一个缺陷"，不含观测值以外的随机成分。
    assert first[0][1] != ""


# ================================================================ 幂等与租户

def test_replay_returns_the_first_result_without_rerunning():
    harness = Harness()
    first = harness.command()
    archive_calls = harness.calls["archive"]
    second = harness.command()

    assert second["replayed"] is True
    assert first["replayed"] is False
    assert second["track_id"] == first["track_id"]
    assert second["subtitle_revision"] == first["subtitle_revision"]
    assert second["qc_report_id"] == first["qc_report_id"]
    # 重放不重新渲染、不重新归档、不发第二个事件。
    assert harness.calls["archive"] == archive_calls
    assert harness.count("qc_report") == 1
    assert harness.count("outbox") == 1


def test_same_key_with_a_different_body_is_409_not_an_overwrite():
    harness = Harness()
    harness.command()
    changed = harness.body()
    changed["target_locale"] = "en-US"
    with pytest.raises(CommandConflict) as caught:
        harness._idem.begin(TENANT, "cmd1", stable_request_hash(changed))
    assert caught.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert caught.value.stored_hash != caught.value.incoming_hash
    assert harness.count("qc_report") == 1


def test_request_hash_ignores_trace_but_not_payload():
    harness = Harness()
    base = harness.body()
    traced = harness.body()
    traced["execution_context"]["trace_id"] = "tr-2"
    traced["execution_context"]["timestamp"] = "2026-01-01T00:00:00Z"
    different = harness.body()
    different["target_locale"] = "en-US"

    assert stable_request_hash(base) == stable_request_hash(traced)
    assert stable_request_hash(base) != stable_request_hash(different)


def test_same_command_id_in_two_tenants_is_two_independent_tracks():
    harness = Harness()
    harness.command()
    other = harness.body()
    other["execution_context"]["tenant_id"] = OTHER_TENANT
    second = harness.command(other, tenant_id=OTHER_TENANT)

    assert second["replayed"] is False
    assert harness.count("track") == 2
    track_ids = [row[0] for row in harness.conn.execute(
        "SELECT track_id FROM track ORDER BY tenant_id")]
    assert len(set(track_ids)) == 2
    with pytest.raises(ServiceError) as caught:
        harness.service.get_track(OTHER_TENANT,
                                  harness.cues()[0]["track_id"])
    assert (caught.value.status, caught.value.code) == \
        (404, "TRACK_NOT_FOUND")


def test_cross_tenant_read_is_indistinguishable_from_missing():
    harness = Harness()
    result = harness.command()
    with pytest.raises(ServiceError) as other:
        harness.service.get_track(OTHER_TENANT, result["track_id"])
    with pytest.raises(ServiceError) as missing:
        harness.service.get_track(TENANT, "trk_" + "0" * 26)
    assert (other.value.status, other.value.code) == \
        (missing.value.status, missing.value.code) == (404, "TRACK_NOT_FOUND")
    # 两个错误体的形状完全一致：跨租户不能靠 message 差异泄漏"存在与否"。
    assert (other.value.retryable, other.value.details) == \
        (missing.value.retryable, missing.value.details)
    assert "t2" not in str(other.value) and "t1" not in str(other.value)
    with pytest.raises(ServiceError):
        harness.service.get_rendition_url(
            OTHER_TENANT, result["track_id"], 1, "srt")


def test_retry_after_restart_still_replays_the_recorded_command():
    harness = Harness()
    first = harness.command()
    # 新进程：新 service、新幂等 store，共享同一份库。
    restarted = SubtitleService(
        harness.conn,
        alignment_source=harness._align,
        timeline_source=harness._timeline,
        dialogue_source=harness._dialogue,
        lexicon_source=harness._lexicon,
        archive_source=harness._archive,
        artifact_url_source=harness._url,
        enabled=True)
    reopened = SqliteIdempotencyStore(harness.conn)
    record = reopened.begin(TENANT, "cmd1", stable_request_hash(harness.body()))
    assert record.result is not None
    replay = json.loads(record.result)
    assert replay["track_id"] == first["track_id"]
    assert replay["subtitle_revision"] == first["subtitle_revision"]

    # 重启后读面照常：cue、rendition、签名 URL 都能取回。
    track = restarted.get_track(TENANT, first["track_id"])
    assert track["cue_count"] == 1
    assert track["state"] == "archived"
    url = restarted.get_rendition_url(TENANT, first["track_id"], 1, "srt")
    assert url["artifact_id"] == "art_00000000000000000001"
    assert url["byte_size"] > 0
    assert url["format"] == "srt"


# ================================================================ 错误面

def _error(call):
    with pytest.raises(ServiceError) as caught:
        call()
    return caught.value.status, caught.value.code


def test_service_disabled_refuses_to_produce_a_track():
    harness = Harness(enabled=False)
    assert _error(harness.command) == (403, "SERVICE_DISABLED")
    assert harness.count("track") == 0
    assert harness.count("qc_report") == 0


def test_check_selection_and_profile_errors():
    assert _error(lambda: Harness().run(checks={"include": ["XYZ.*"]})) == \
        (422, "INVALID_CHECKS")
    assert _error(lambda: Harness().run(checks={"include": []})) == \
        (422, "INVALID_CHECKS")

    empty = Harness()
    empty.service.enabled_checks = ()
    assert _error(empty.command) == (422, "NO_CHECKS_ENABLED")

    assert _error(lambda: Harness(formats=["txt"]).run()) == \
        (422, "INVALID_PROFILE")
    assert _error(lambda: Harness().run(profile={"frame_rate": "30"})) == \
        (422, "INVALID_PROFILE")


def test_command_coordinate_errors():
    harness = Harness()
    body = harness.body()
    del body["execution_context"]["home_cell"]
    assert _error(lambda: harness.service.check(
        TENANT, "cmd1", body, HASH)) == (422, "INVALID_SUBTITLE_JOB")

    assert _error(lambda: Harness().run(
        timeline_ref={"service_id": "S99", "id": "tl_ep3",
                      "revision": 9})) == (422, "INVALID_SUBTITLE_JOB")
    assert _error(lambda: Harness(lines=[]).run()) == (422, "NO_DIALOGUE")
    assert _error(lambda: Harness(units=[]).run()) == \
        (422, "NO_PLACEABLE_CUES")


def test_upstreams_that_are_down_say_so():
    harness = Harness()

    def down(*_args, **_kwargs):
        raise ConnectionError("down")

    harness.service.alignment_source = down
    assert _error(harness.command) == (503, "ALIGNMENT_UNAVAILABLE")

    harness = Harness()
    harness.service.timeline_source = down
    assert _error(harness.command) == (503, "TIMELINE_UNAVAILABLE")

    harness = Harness()
    harness.service.dialogue_source = down
    assert _error(harness.command) == (503, "DIALOGUE_UNAVAILABLE")


def test_translation_command_surface_errors():
    def run(status):
        return Harness(lines=[{"line_id": "ln1", "revision": 4,
                               "text": "你终于来了"}],
                       units=[unit(0, 0, 3, 1.00, 1.40),
                              unit(1, 3, 3, 1.40, 1.90)]).run(
            target_locale="en-US", source_locale="zh-CN",
            localization=localization("trn_x", status, "x"))

    assert _error(lambda: run("accepted")) == \
        (422, "INVALID_TRANSLATION_STATUS")

    harness = Harness(
        [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
        [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)])
    harness.run(target_locale="en-US", source_locale="zh-CN",
                localization=EN_LOCALIZATION)
    assert _error(lambda: harness.run(
        command_id="cmd2", target_locale="en-US", source_locale="zh-CN",
        localization=localization("trn_a", "reviewed", "different"))) == \
        (409, "TRANSLATION_REVISION_CONFLICT")
    assert _error(lambda: harness.run(
        command_id="cmd3", target_locale="en-US", source_locale="zh-CN",
        localization={"translations": [{
            "translation_id": "trn_empty", "revision": 1,
            "status": "reviewed", "lines": []}]})) == \
        (422, "INVALID_SUBTITLE_JOB")


def test_rendition_lookup_errors():
    harness = Harness()
    result = harness.command()
    track_id = result["track_id"]
    assert _error(lambda: harness.service.get_rendition_url(
        TENANT, track_id, "x", "srt")) == (422, "INVALID_REVISION")
    assert _error(lambda: harness.service.get_rendition_url(
        TENANT, track_id, 1, "txt")) == (422, "INVALID_FORMAT")
    assert _error(lambda: harness.service.get_rendition_url(
        TENANT, track_id, 2, "srt")) == (404, "RENDITION_NOT_FOUND")
    assert _error(lambda: harness.service.get_rendition_url(
        TENANT, "trk_nope", 1, "srt")) == (404, "TRACK_NOT_FOUND")

    harness.service.artifact_url_source = None
    assert _error(lambda: harness.service.get_rendition_url(
        TENANT, track_id, 1, "srt")) == (503, "ARTIFACT_SOURCE_UNAVAILABLE")


def test_redelivering_a_broken_event_is_400_not_a_silent_skip():
    harness = Harness()
    assert _error(lambda: harness.service.consume_script_revision_event(
        TENANT, {"id": "ev", "type": schema.EVENT_SCRIPT, "data": {}})) == \
        (400, "INVALID_EVENT")
    assert harness.count("inbox") == 0


# ================================================================ 权威边界

def test_this_service_never_marks_a_track_qc_passed_or_accepted():
    """S38-C15：状态机画了那两个值，但没有任何代码路径写它们。"""
    states = set()
    for decision in ("pass", "repair", "human_review", "abstain"):
        harness = Harness(
            [{"line_id": "ln1", "revision": 4, "text": "你终于来了"}],
            [unit(0, 0, 3, 1.00, 1.40), unit(1, 3, 3, 1.40, 1.90)],
            decision=decision)
        states.add(harness.run()["state"])
        states.add(harness.service.get_track(
            TENANT, harness.cues()[0]["track_id"])["state"])

    assert states <= {"archived", "rendered", "repair_pending",
                      "rendered_pending_archive", "draft"}
    assert not states & {"accepted", "qc_passed"}
    # 库里也没有任何"接受指针"式的列。
    tables = [row["name"] for row in Harness().conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        assert not [name for name in Harness().columns(table)
                    if "accept" in name or "pointer" in name], table


def test_node_epoch_and_execution_fence_are_separate_columns():
    """AGENTS.md：fence 属于 run/branch，epoch 属于单个 logical_node。"""
    harness = Harness()
    result = harness.command()
    track = harness.conn.execute(
        "SELECT execution_fence, node_epoch FROM track").fetchone()
    assert (track["execution_fence"], track["node_epoch"]) == (3, 7)
    cue = harness.cues()[0]
    assert cue["node_epoch"] == 7
    assert "execution_fence" not in harness.columns("cue")

    payload = harness.report()
    assert payload["context"]["execution_fence"] == 3
    assert payload["shot_ref"]["node_epoch"] == 7
    assert harness.service.get_track(
        TENANT, result["track_id"])["execution_fence"] == 3


def test_s38_holds_no_artifact_bytes_and_no_second_copy():
    harness = Harness()
    harness.command()
    tables = {row["name"] for row in harness.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables - {"idempotency_record"} == OWNED_TABLES
    assert not [name for name in tables
                if any(word in name for word in ("blob", "bytes", "media"))]
    assert harness.events()[0]["data_ref"]["service_id"] == "S38"
    assert {item["format"]: item["artifact_id"]
            for item in harness.events()[0]["data"]["artifacts"]} == {
        "srt": "art_00000000000000000001",
        "vtt": "art_00000000000000000002",
        "ass": "art_00000000000000000003"}


def test_source_imports_no_other_service():
    """AGENTS.md：不 import 别的服务的业务 ORM，只走注入的 callback。"""
    package = SERVICES / "subtitles" / "src" / "subtitles"
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for other in ("audio_alignment", "technical_qc", "semantic_qc",
                      "image_production", "artifact_ingest", "narrative",
                      "speech_production", "editorial"):
            assert f"import {other}" not in text, (path.name, other)


def test_no_hardcoded_provider_urls():
    """依赖地址只从环境变量经 `build_*` 注入（deps.py docstring）。"""
    package = SERVICES / "subtitles" / "src" / "subtitles"
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text, path.name
        assert "https://" not in text, path.name


# ================================================================ 注册表与缺口

class TestRegistry:
    def test_registry_loads_under_the_reference_validator(self):
        spec = importlib.util.spec_from_file_location(
            "qc_validate_registry",
            CONTRACTS / "quality" / "validate_registry.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        families = _load("quality/action-families.v2.json")
        document = schema.registry_document()
        assert module.validate(document, families) == []
        assert _errors(document, "quality/check-registry.v2.schema.json") == []

    def test_registry_entries_match_the_owner_and_layer(self):
        assert len(schema.CHECKS) == 24
        assert len(set(schema.CHECK_IDS)) == 24
        for check in schema.CHECKS:
            assert check["layer"] == "subtitle"
            assert check["owner_service"] == "S38"
            assert check["defect_type"] == "subtitle"
            assert check["check_id"].startswith("SUB.")
            assert check["_expected"] in ("S06", "S37", "S15")
            assert schema.SEVERITY_ORDER[check["severity_default"]] <= \
                schema.SEVERITY_ORDER[check["severity_max"]]
            assert set(check["allowed_actions"]) <= {
                "realign_subtitles", "re_render_subtitles"}
            assert check["default_action"] in check["allowed_actions"]

    def test_never_auto_checks_carry_a_reason(self):
        for check_id in schema.NEVER_AUTO:
            entry = schema.BY_ID[check_id]
            assert entry["never_auto_repair"] is True, check_id
            assert entry["no_auto_reason"], check_id
        assert schema.OBSERVATION_ONLY == {"SUB.TEXT.SCRIPT_CONFLICT"}
        assert schema.OBSERVATION_ONLY <= set(schema.CHECK_IDS)
        assert schema.BY_ID["SUB.TEXT.SCRIPT_CONFLICT"][
            "never_auto_repair"] is True

    def test_select_and_severity_helpers(self):
        assert len(schema.select(None, None)) == 24
        assert len(schema.select(["SUB.TIMING.*"], None)) == 8
        assert schema.select(["SUB.TIMING.*"], None) == [
            check_id for check_id in schema.CHECK_IDS
            if check_id.startswith("SUB.TIMING.")]
        with pytest.raises(ValueError, match="matches no check"):
            schema.select(["XYZ.*"], None)
        with pytest.raises(ValueError, match="non-empty"):
            schema.select([], None)
        with pytest.raises(ValueError, match="selects no check"):
            schema.select(["SUB.*"], ["SUB.*"])
        assert schema.severity_after("major", "minor", "blocker") == "major"
        assert schema.severity_after("major", "blocker", "blocker") == \
            "blocker"
        assert schema.severity_after("info", "major", "minor") == "minor"
        assert schema.report_scope("window") == "window"
        assert schema.report_scope("track") == "whole_artifact"
        with pytest.raises(ValueError):
            schema.report_scope("nonsense")
        assert schema.expected_source("SUB.TIMING.OVERLAP") == "S06"
        assert schema.expected_source("SUB.TEXT.LEXICON_UNUSED") == "S15"

    def test_registry_document_drops_internal_keys(self):
        document = schema.registry_document()
        assert set(document) == {"schema_version", "registry_version",
                                 "checks"}
        assert document["registry_version"] == schema.REGISTRY_VERSION
        for check in document["checks"]:
            assert not [key for key in check if key.startswith("_")]


def test_contract_gaps_are_declared_with_a_kind_and_a_handling():
    gaps = {gap["id"]: gap for gap in CONTRACT_GAPS}
    assert len(gaps) == 19
    ids = [gap["id"] for gap in CONTRACT_GAPS]
    # 顺序是写入顺序（C17 在最后追加），不是字典序：
    assert ids == [f"S38-C{i}" for i in range(1, 17)] + \
        ["S38-C18", "S38-C19", "S38-C17"]
    for gap in CONTRACT_GAPS:
        assert set(gap) == {"id", "kind", "summary", "handling"}
        assert gap["kind"] in {"conflict", "gap"}
        assert gap["summary"] and gap["handling"]
    # 只有三处是"与既有契约冲突"，其余是实现留白。
    assert {gap["id"] for gap in CONTRACT_GAPS
            if gap["kind"] == "conflict"} == {"S38-C1", "S38-C4", "S38-C10"}


# ================================================================ HTTP 层

def _client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    db = tmp_path / "s38.sqlite"
    monkeypatch.setenv("S38_DB", str(db))
    monkeypatch.setenv("S38_ENABLED", "true")
    monkeypatch.setenv("S38_CHECKS", ",".join(schema.CHECK_IDS))
    for name in ("S37_BASE_URL", "S41_BASE_URL", "S06_BASE_URL",
                 "S15_BASE_URL", "S28_BASE_URL", "S26_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    spec = importlib.util.spec_from_file_location(
        "s38_app_under_test",
        SERVICES / "subtitles" / "api" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    harness = Harness()
    module.service = harness.service
    module._idem = harness._idem
    module._conn = harness.conn
    return TestClient(module.app), harness


def test_http_command_read_track_rendition_and_readiness(tmp_path, monkeypatch):
    client, _harness = _client(tmp_path, monkeypatch)
    headers = {"Idempotency-Key": "cmd1", "X-Tenant-Id": TENANT}
    response = client.post("/subtitle-jobs", json=Harness().body(),
                           headers=headers)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "archived"
    assert body["qc_decision"] == "pass"
    assert body["replayed"] is False

    fetched = client.get(f"/subtitle-tracks/{body['track_id']}",
                         headers={"X-Tenant-Id": TENANT})
    assert fetched.status_code == 200
    assert fetched.json()["cue_count"] == 1
    assert fetched.json()["cues"][0]["source_text"]["service_id"] == "S06"

    other = client.get(f"/subtitle-tracks/{body['track_id']}",
                       headers={"X-Tenant-Id": OTHER_TENANT})
    assert other.status_code == 404
    assert other.json()["code"] == "TRACK_NOT_FOUND"

    default = client.get(
        f"/subtitle-tracks/{body['track_id']}/revisions/1",
        headers={"X-Tenant-Id": TENANT})
    assert default.status_code == 200
    assert default.json()["format"] == "srt"
    assert default.json()["url"].startswith("https://")
    vtt = client.get(f"/subtitle-tracks/{body['track_id']}/revisions/1",
                     params={"format": "vtt"},
                     headers={"X-Tenant-Id": TENANT})
    assert vtt.status_code == 200
    assert vtt.json()["format"] == "vtt"
    # 只给签名 URL，正文永远不经过本服务。
    assert "content" not in default.json()
    assert "bytes" not in default.json()

    ready = client.get("/health/readiness")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert ready.json()["enabled"] is True
    assert len(ready.json()["enabled_checks"]) == 24
    assert ready.json()["check_registry_version"] == 1
    assert len(ready.json()["contract_gaps"]) == 19


def test_http_replay_conflict_and_in_flight(tmp_path, monkeypatch):
    client, harness = _client(tmp_path, monkeypatch)
    headers = {"Idempotency-Key": "cmd1", "X-Tenant-Id": TENANT}
    first = client.post("/subtitle-jobs", json=harness.body(), headers=headers)
    second = client.post("/subtitle-jobs", json=harness.body(),
                         headers=headers)
    assert second.status_code == 202
    assert second.json()["replayed"] is True
    assert second.json()["track_id"] == first.json()["track_id"]

    changed = harness.body()
    changed["target_locale"] = "en-US"
    conflict = client.post("/subtitle-jobs", json=changed, headers=headers)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert conflict.json()["details"]["stored_request_hash"] != \
        conflict.json()["details"]["incoming_request_hash"]

    body = harness.body(command_id="cmd9")
    harness._idem.begin(TENANT, "cmd9", stable_request_hash(body))
    response = client.post("/subtitle-jobs", json=body,
                           headers={"Idempotency-Key": "cmd9",
                                    "X-Tenant-Id": TENANT})
    assert response.status_code == 409
    assert response.headers["Retry-After"] == "1"
    assert response.json()["code"] == "COMMAND_IN_FLIGHT"
    assert response.json()["retryable"] is True


def test_http_tenant_mismatch_and_key_mismatch(tmp_path, monkeypatch):
    client, harness = _client(tmp_path, monkeypatch)
    body = harness.body()
    body["execution_context"]["tenant_id"] = OTHER_TENANT
    response = client.post("/subtitle-jobs", json=body,
                           headers={"Idempotency-Key": "cmd1",
                                    "X-Tenant-Id": TENANT})
    assert response.status_code == 403
    assert response.json()["code"] == "TENANT_MISMATCH"

    # Idempotency-Key 必须等于 body.command_id（AGENTS.md）。
    mismatch = client.post("/subtitle-jobs", json=harness.body(),
                           headers={"Idempotency-Key": "cmdX",
                                    "X-Tenant-Id": TENANT})
    assert mismatch.status_code == 400
    assert mismatch.json()["code"] == "INVALID_BODY"


def test_http_event_consume_is_idempotent_and_typed(tmp_path, monkeypatch):
    client, harness = _client(tmp_path, monkeypatch)
    client.post("/subtitle-jobs", json=harness.body(),
                headers={"Idempotency-Key": "cmd1", "X-Tenant-Id": TENANT})
    event = {"id": "ev1", "type": schema.EVENT_SCRIPT,
             "aggregate_id": "shot_3_2", "sequence": 7, "tenant_id": TENANT,
             "data": {"script_id": "shot_3_2", "revision": 5}}
    first = client.post("/events:consume", json={"event": event},
                        headers={"X-Tenant-Id": TENANT})
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert first.json()["tracks"] == 1

    again = client.post("/events:consume", json=event,
                        headers={"X-Tenant-Id": TENANT})
    assert again.status_code == 200
    assert again.json()["duplicate"] is True

    unknown = client.post("/events:consume",
                          json={"event": dict(event, type="x.y")},
                          headers={"X-Tenant-Id": TENANT})
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "UNSUPPORTED_EVENT"

    foreign = client.post("/events:consume", json=event,
                          headers={"X-Tenant-Id": OTHER_TENANT})
    assert foreign.status_code == 403
    assert foreign.json()["code"] == "TENANT_MISMATCH"


def test_http_archive_retry_endpoint(tmp_path, monkeypatch):
    client, harness = _client(tmp_path, monkeypatch)
    body = harness.body()
    headers = {"Idempotency-Key": "cmd1", "X-Tenant-Id": TENANT}
    result = client.post("/subtitle-jobs", json=body, headers=headers).json()
    retried = client.post(f"/subtitle-tracks/{result['track_id']}"
                          "/archive-retries",
                          headers={"X-Tenant-Id": TENANT})
    assert retried.status_code == 200
    assert retried.json() == {
        "track_id": result["track_id"], "subtitle_revision": 1,
        "archived_formats": [], "pending_formats": [],
        "event_emitted": False}

    missing = client.post("/subtitle-tracks/trk_nope/archive-retries",
                          headers={"X-Tenant-Id": TENANT})
    assert missing.status_code == 404
    assert missing.json()["code"] == "TRACK_NOT_FOUND"


def test_http_disabled_service_is_403_and_reads_stay_open(tmp_path,
                                                          monkeypatch):
    client, harness = _client(tmp_path, monkeypatch)
    harness.service.enabled = False
    response = client.post("/subtitle-jobs", json=harness.body(),
                           headers={"Idempotency-Key": "cmd1",
                                    "X-Tenant-Id": TENANT})
    assert response.status_code == 403
    assert response.json()["code"] == "SERVICE_DISABLED"
    assert response.json()["details"] == {}
    # 只读面照常开放：审计可见性不该被生产开关收回（S38-C13）。
    ready = client.get("/health/readiness")
    assert ready.status_code == 200
    assert ready.json()["enabled"] is False
    assert client.get("/subtitle-tracks/trk_nope",
                      headers={"X-Tenant-Id": TENANT}).status_code == 404
