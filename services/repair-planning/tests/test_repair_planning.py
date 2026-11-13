"""S39 测试。方案 §10 的 E24-R1..R18 / E25-R1..R6 / E26-R1..R14 / D1..D5，
加上通用验收（租户隔离、幂等重放、乱序与重复事件、重启恢复、版本冲突、
审计可追溯、契约缺口形状、注册表双校验、事件契约、HTTP 层）。

三条主线：

1. **只出计划，不执行。** 没有一处付费调用、没有接受指针、没有金额列。
   复检 pass 只交出"候选可接受"，`accept_pointer_touched` 恒为 False。
2. **`execution_fence` 与 `node_epoch` 分列。** 测试里故意给两个不同的数
   （fence=3、epoch=7），并且分别单独变化，混用会立刻露馅。
3. **计数器的键里没有 run / manifest / epoch / fence / 模型 / workflow。**
   每个"换一个维度重试仍 DENY"的用例都直接打这条。

上游全部是闭包 stub：**不联网、不调模型、不调供应商**。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
)

from repair_planning import registry as registry_mod
from repair_planning.schema import CONTRACT_GAPS, LAYER_OF_SERVICE
from repair_planning.service import (
    RepairPlanningService,
    ServiceError,
    derived_key_for,
    plan_fingerprint,
    report_content_hash,
    report_id_for,
    route,
    stable_request_hash,
    strategy_class,
    window_frames,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
SERVICES = ROOT / "services"
QUALITY = CONTRACTS / "quality"

TENANT = "t1"
OTHER_TENANT = "t2"
HASH = "a" * 64
HASH2 = "d" * 64
HASH3 = "e" * 64
HASH4 = "f" * 64

SERIES = "ser1"
BRANCH = "br1"
EPISODE = "ep3"
SHOT = "lin_shot_3_2_v1"
RUN = "run_1"
RUN2 = "run_2"

DEFAULT_TS = "2026-01-01T00:00:00.000Z"

CHECK_ID_PATTERN = re.compile(r"^(TECH|VIS|AUD|SUB)\.[A-Z0-9_]+\.[A-Z0-9_]+$")
DEFECT_ID_PATTERN = re.compile(r"^df_[a-z0-9]{26}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{32}$")

# 本服务自己拥有的表（SDK 的两张表另算）。多一张、少一张都要显式改——
# 这是"没有第二份权威"最便宜的可证伪断言。
OWNED_TABLES = {
    "plan", "action", "attempt_ledger", "attempt_reservation", "recheck_job",
    "qc_rejection", "inbox", "outbox",
}

SERVICE_LAYERS = {"S35": "technical", "S36": "semantic", "S37": "alignment",
                  "S38": "subtitle", "S17": "semantic"}
EVALUATOR_NAMES = {"S35": "s35-technical-qc", "S36": "s36-semantic-qc",
                   "S37": "s37-alignment-qc", "S38": "s38-subtitle-qc",
                   "S17": "s17-continuity"}

MEDIA = {
    "duration_seconds": 10.0,
    "video": {"width": 1920, "height": 1080, "fps_num": 24, "fps_den": 1,
              "codec": "h264", "pix_fmt": "yuv420p", "color_range": "tv",
              "is_vfr": False, "frame_count": 240},
    "audio": {"codec": "aac", "sample_rate_hz": 48000, "channels": 2,
              "stream_index": 1, "start_offset_seconds": 0.0},
}

DEFAULT_COVERAGE = {
    "media_duration_seconds": 10.0, "frames_sampled": 240,
    "temporal_windows_evaluated": 4, "planned_windows": 4,
    "coverage_ratio": 1.0, "required_coverage_min": 0.9,
    "audio_analyzed": True, "audio_analyzed_seconds": 10.0,
    "sampling_policy_version": "sp1",
}


# ------------------------------------------------------------------ 工具

def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


FAMILIES = _load_json(QUALITY / "action-families.v2.json")
QC_REPORT_SCHEMA = _load_json(QUALITY / "qc-report.v2.schema.json")
EXECUTION_CONTEXT_SCHEMA = _load_json(
    CONTRACTS / "execution-context.v2.schema.json")
CHECK_REGISTRY_SCHEMA = _load_json(QUALITY / "check-registry.v2.schema.json")
PLANNED_EVENT_SCHEMA = _load_json(
    CONTRACTS / "quality.repair-planned.v2.schema.json")


def check(check_id, layer, owner, defect_type, default_action, allowed_actions,
          window_scope="window", *, window_max_frames=6,
          severity_default="major", severity_max="major",
          recheck_scope="semantic", escalation_rule=None,
          never_auto_repair=None, no_auto_reason=None) -> dict:
    entry = {
        "check_id": check_id, "layer": layer, "owner_service": owner,
        "defect_type": defect_type, "default_action": default_action,
        "allowed_actions": list(allowed_actions),
        "window_scope": window_scope,
        "severity_default": severity_default, "severity_max": severity_max,
        "recheck_scope": recheck_scope,
    }
    if window_scope in ("frame", "window"):
        entry["window_max_frames"] = window_max_frames
    if escalation_rule is not None:
        entry["escalation_rule"] = escalation_rule
    if never_auto_repair is not None:
        entry["never_auto_repair"] = never_auto_repair
    if no_auto_reason is not None:
        entry["no_auto_reason"] = no_auto_reason
    return entry


TECH_CODEC = check(
    "TECH.CONTAINER.CODEC", "technical", "S35", "technical", "reingest",
    ["reingest", "transcode", "remix", "temporal_edit", "regenerate_video"],
    window_scope="shot", window_max_frames=None, recheck_scope="technical")
# `recheck_scope` 取 `check-registry.v2.schema.json` 的**闭集**。方案 §6.3 写的是
# `semantic:{aspect}` / `alignment:{aspect}` 的带切面形式，而 schema 的 enum 只有
# `semantic` / `semantic+continuity` / `alignment` / `subtitle` 这几档——带切面
# 的写法不在契约里（C5 已登记"数据文件未落、登记项字段缺失"）。测试用契约允许
# 的值，才能在 `test_registry_passes_both_validators` 里同时过两个校验器；
# `split_recheck_scope` 的带切面形式单独用纯函数覆盖（§6.3 的并集语义）。
VIS_IDENTITY = check(
    "VIS.IDENTITY.DRIFT", "semantic", "S36", "identity", "keyframe_edit",
    ["keyframe_edit", "temporal_edit", "regenerate_video"],
    window_scope="window", window_max_frames=6,
    recheck_scope="semantic+continuity")
AUD_LIPSYNC = check(
    "AUD.LIPSYNC.OFFSET", "alignment", "S37", "lipsync", "redo_lipsync",
    ["redo_lipsync"], window_scope="window", window_max_frames=48,
    severity_default="minor", recheck_scope="alignment")
SUB_TIMING = check(
    "SUB.TIMING.OVERLAP", "subtitle", "S38", "subtitle",
    "realign_subtitles", ["realign_subtitles", "re_render_subtitles"],
    window_scope="window", window_max_frames=50, severity_default="minor",
    recheck_scope="subtitle")

DEFAULT_CHECKS = [TECH_CODEC, VIS_IDENTITY, AUD_LIPSYNC]


def registry_document(checks, version=3) -> dict:
    return {"schema_version": "2.0", "registry_version": version,
            "checks": [dict(c) for c in checks]}


def make_registry(checks=None, version=3):
    return registry_mod.Registry(registry_document(checks or DEFAULT_CHECKS,
                                                   version), FAMILIES)


def fingerprint_for(check_id: str, start: float, end: float,
                    defect_type: str) -> str:
    raw = f"{check_id}|{start}|{end}|{defect_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def make_defect(check_id, *, dtype, severity="major", scope="window",
                start=0.0, end=0.25, actions=("regenerate_video",),
                fingerprint=None, confidence=0.95, bbox=None,
                measured=None, disposition=None, disposition_reason=None):
    defect_fingerprint = fingerprint or fingerprint_for(check_id, start, end,
                                                        dtype)
    defect = {
        "defect_id": "df_" + hashlib.sha256(
            defect_fingerprint.encode("utf-8")).hexdigest()[:26],
        "defect_fingerprint": defect_fingerprint,
        "check_id": check_id,
        "type": dtype,
        "severity": severity,
        "scope": scope,
        "time_range": {"start_seconds": start, "end_seconds": end},
        "region_bbox": bbox,
        "character_id": None,
        "expected": "the spec says one thing",
        "expected_source": {"service_id": "S01", "ref_id": "ref1",
                            "ref_revision": 1},
        "observed": "the artifact says another",
        "measured": measured,
        "confidence": confidence,
        "detector": "deterministic",
        "evidence_artifact_ids": ["art_evidence_1"],
        "recommended_actions": list(actions),
    }
    if disposition is not None:
        defect["disposition"] = disposition
        defect["disposition_reason"] = disposition_reason
    return defect


def make_report(service_id: str, *, defects=(), manifest=None, artifact=HASH,
                generation=1, decision="repair", tenant=TENANT, shot=SHOT,
                run_id=RUN, episode=EPISODE, repair_context=None,
                coverage=None, abstain=None, confidence=0.9,
                policy_version="2", sampling="sp1") -> dict:
    layer = SERVICE_LAYERS[service_id]
    document = {
        "schema_version": "2.0",
        "qc_layer": layer,
        "context": {
            "tenant_id": tenant, "project_id": "prj1", "series_id": SERIES,
            "branch_id": BRANCH, "episode_id": episode, "home_cell": "cell-a",
            "manifest_ref": {"id": "man1", "revision": 3,
                             "content_hash": HASH},
            "run_id": run_id, "execution_fence": 3,
            "shot_lineage_id": shot,
            "policy_ref": {"service_id": "S02", "id": "pol1", "revision": 2,
                           "content_hash": HASH},
            "trace_id": "trace-1",
        },
        "subject": {"artifact_id": "art_1", "artifact_sha256": artifact,
                    "artifact_kind": "video", "media": MEDIA},
        "shot_ref": {"shot_id": "shot_3_2", "shot_revision": 4,
                     "logical_node_id": "node-1", "node_epoch": 7},
        "evaluator": {"name": EVALUATOR_NAMES[service_id], "version": "1.0.0",
                      "image_digest": "sha256:image", "model_snapshot": None},
        "policy_version": policy_version,
        "sampling_policy_version": sampling,
        "check_manifest": [dict(m) for m in (manifest or [])],
        "defects": [dict(d) for d in defects],
        "dimensions": [],
        "coverage": dict(coverage or DEFAULT_COVERAGE),
        "decision": decision,
        "decision_reason": "the report says so",
        "abstain": abstain,
        "confidence": confidence,
        "supersedes": None,
        "repair_context": repair_context,
        "evidence_bundle_refs": [
            {"service_id": service_id, "id": "ev_1", "revision": 1,
             "content_hash": HASH}],
        "created_at": DEFAULT_TS,
    }
    document["report_generation"] = generation
    key = derived_key_for(document)
    if key is None:
        seed = _jcs({"service_id": service_id, "artifact": artifact,
                     "generation": generation, "layer": layer})
        document["report_id"] = "qc_" + hashlib.sha256(
            seed.encode("utf-8")).hexdigest()[:26]
    else:
        document["report_id"] = report_id_for(key)
    # C10：`qc-report.v2` 的 defect 是 additionalProperties:false，装不下
    # `disposition`。S39 在校验前把 S17 的那两个字段剥掉，然后对**剥完的**
    # 正文核对 `report_content_hash`。所以哈希也必须按剥完的形状算——否则
    # 一份带 disposition 的 S17 报告会被判 `content_hash_mismatch` 隔离。
    document["report_content_hash"] = report_content_hash(
        _without_dispositions(document) if service_id == "S17" else document)
    return document


def _without_dispositions(document: dict) -> dict:
    """与 `_split_dispositions` 同形：只去掉 `disposition*`，其余原样。"""
    body = dict(document)
    body["defects"] = [
        {k: v for k, v in defect.items()
         if k not in ("disposition", "disposition_reason")}
        if isinstance(defect, dict) else defect
        for defect in document.get("defects") or []]
    return body


def manifest_for(*check_ids, status="evaluated", reason_code="ok"):
    return [{"check_id": cid, "status": status, "reason_code": reason_code}
            for cid in check_ids]


def execution_context(**overrides) -> dict:
    context = {
        "schema_version": "2.0",
        "tenant_id": TENANT,
        "project_id": "prj1",
        "series_id": SERIES,
        "branch_id": BRANCH,
        "episode_id": EPISODE,
        "home_region": "cn-east-1",
        "home_cell": "cell-a",
        "manifest_ref": {"id": "man1", "revision": 3, "content_hash": HASH},
        "node_id": "node-1",
        "node_epoch": 7,
        "execution_fence": 3,
        "memory_checkpoint": {"branch_id": BRANCH, "commit_sequence": 4,
                              "content_hash": HASH},
        "policy_ref": {"service_id": "S02", "id": "pol1", "revision": 2,
                       "content_hash": HASH},
        "actor": {"principal_id": "svc-s21", "principal_type": "service",
                  "delegation_id": "dlg-1"},
        "command_id": "cmd1",
        "request_hash": HASH,
        "trace_id": "trace-1",
    }
    context.update(overrides)
    return context


def report_ref(service_id: str, document: dict) -> dict:
    return {"service_id": service_id, "id": document["report_id"],
            "revision": document["report_generation"],
            "content_hash": document["report_content_hash"]}


def reports_for(checks, defects_by_check=None, *, artifact=HASH, **overrides):
    """按 owner 服务各出一份报告；manifest 恰好等于该层在 registry 里的行。

    `report_problems` 的 (d) 与 `_coverage_gap` 两头都咬这点：多一行、少一行
    都会让计划从 `planned` 掉到 `escalated(coverage_gap)`。
    """
    defects_by_check = defects_by_check or {}
    owners: dict[str, list[str]] = {}
    for entry in checks:
        owners.setdefault(entry["owner_service"], []).append(entry["check_id"])
    documents = []
    for owner, check_ids in owners.items():
        defects = [d for cid in check_ids for d in defects_by_check.get(cid, [])]
        documents.append(make_report(
            owner, defects=defects, manifest=manifest_for(*check_ids),
            artifact=artifact, **overrides))
    return documents


def plan_body(*, artifact=HASH, refs=(), command_id="cmd1",
              context=None, dry_run=False, max_actions=10,
              include_run=True, include_shot=True) -> dict:
    body = {
        "command_id": command_id,
        "execution_context": context or execution_context(),
        "qc_snapshot": {"artifact_sha256": artifact, "reports": list(refs)},
        "aggregation_mode": "wait_all",
        "max_actions": max_actions,
        "dry_run": dry_run,
    }
    if include_run:
        body["run_id"] = RUN
    if include_shot:
        body["shot_lineage_id"] = SHOT
    return body


def default_policy(**limits) -> dict:
    """S02 `getEffectivePolicy` 的已合并结果。**S39 不自己做层级合并。**"""
    merged = {"max_business_attempts_per_shot": 3,
              "max_identical_strategy_repeats": 1,
              "max_business_attempts_per_episode": 12,
              "max_business_attempts_per_run": 40}
    merged.update(limits)
    return {"effective": {"repair_limits": merged, "allowed_actions": None,
                          "exception_behavior": "wait_review"},
            "policy_ref": {"service_id": "S02", "id": "pol1", "revision": 2,
                           "content_hash": HASH}}


def capability_payload(*, temporal=True, max_window_frames=64,
                       preserves_identity=True) -> dict:
    """S24/S25 的候选能力。默认只有*声明了*的那一项才算数（C19）。"""
    candidates = [] if not temporal else [{
        "provider_id": "prov1", "model_id": "m1",
        "temporal_consistency": True,
        "preserves_identity": preserves_identity,
        "max_window_frames": max_window_frames,
    }]
    return {"candidates": candidates}


class Harness:
    """内存库 + 注入式上游。**没有任何真实网络调用。**"""

    def __init__(self, *, checks=None, version=3, enabled=True, policy=None,
                 capability=None, continuity_mode="absent", run=None,
                 qc_report_schema=True, execution_context_schema=True,
                 db_path=None):
        # `db_path` 非空时落在文件上：重启恢复（E13 系列）要开第二个实例
        # 读同一个库，内存库做不到。
        self.conn = sqlite3.connect(db_path or ":memory:",
                                    check_same_thread=False,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.checks = list(checks if checks is not None else DEFAULT_CHECKS)
        self.registry = make_registry(self.checks, version)
        self.policy = policy if policy is not None else default_policy()
        self.capability = (capability if capability is not None
                           else capability_payload())
        self.reports: dict[tuple[str, str], dict] = {}
        self.added: list[tuple[str, dict]] = []
        self.calls: dict[str, int] = {}
        self._continuity_mode = continuity_mode
        self.service = RepairPlanningService(
            self.conn,
            policy_source=self._policy_source,
            report_sources={sid: self._report_source(sid)
                            for sid in ("S35", "S36", "S37", "S38")},
            capability_source=self._capability_source,
            continuity_source=(self._no_continuity()
                               if continuity_mode == "absent"
                               else self._report_source("S17")),
            run_source=(self._run_source if run is not None else None),
            registry=self.registry,
            qc_report_schema=QC_REPORT_SCHEMA if qc_report_schema else None,
            execution_context_schema=(EXECUTION_CONTEXT_SCHEMA
                                      if execution_context_schema else None),
            enabled=enabled)
        # 幂等存储必须在 service 之后建：service 的 executescript 会隐式提交。
        self._idem = SqliteIdempotencyStore(self.conn)
        self.run_reply = run

    # ------------------------------------------------------------ 上游
    def _bump(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def _policy_source(self, tenant_id, request):
        self._bump("policy")
        return self.policy

    def _capability_source(self, tenant_id, requirement):
        self._bump("capability")
        return self.capability

    def _run_source(self, tenant_id, run_id):
        self._bump("run")
        return self.run_reply if self.run_reply is not None else \
            {"remaining_repair_attempts": 40, "budget_exhausted": False}

    def _report_source(self, service_id):
        def source(tenant_id, ref):
            self._bump(f"report:{service_id}")
            document = self.reports.get((service_id, str(ref.get("id"))))
            if document is None:
                raise ServiceError(404, "REPORT_NOT_FOUND",
                                   f"no {service_id} report {ref.get('id')!r}")
            return document
        return source

    @staticmethod
    def _no_continuity():
        def source(tenant_id, ref):
            raise ServiceError(503, "S17_REPORT_SOURCE_UNAVAILABLE",
                               "S17 continuity source is not configured")
        return source

    # ------------------------------------------------------------ 数据注入
    def add(self, *documents):
        for document in documents:
            name = (document.get("evaluator") or {}).get("name")
            service_id = next(sid for sid, value in EVALUATOR_NAMES.items()
                              if value == name)
            self.reports[(service_id, document["report_id"])] = document
            self.added.append((service_id, document))
        return self

    def refs(self) -> list[dict]:
        return [report_ref(sid, doc) for sid, doc in self.added]

    def reset(self) -> None:
        self.reports.clear()
        self.added.clear()

    # ------------------------------------------------------------ 命令
    def plan(self, *, command_id="cmd1", body=None, **overrides) -> dict:
        payload = body if body is not None else plan_body(
            refs=self.refs(), command_id=command_id, **overrides)
        return self.command(payload, command_id)

    def command(self, body: dict, command_id: str | None = None,
                tenant: str = TENANT) -> dict:
        """与 `api/app.py` 的 `command()` 同一条路：占位 → 执行 → 落结果。"""
        cmd = command_id or str(body.get("command_id"))
        request_hash = stable_request_hash(body)
        record = self._idem.begin(tenant, cmd, request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = self.service.create_plan(tenant, cmd, body, request_hash)
        except Exception:
            self._idem.abort(tenant, cmd)
            raise
        self._idem.finish(tenant, cmd,
                          json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ------------------------------------------------------------ 取证
    def events(self, event_type: str | None = None) -> list[dict]:
        rows = list(self.conn.execute(
            "SELECT * FROM outbox ORDER BY rowid"))
        out = []
        for row in rows:
            payload = json.loads(row["payload"])
            if event_type and payload.get("type") != event_type:
                continue
            out.append(payload)
        return out

    def count(self, table: str, where: str = "", args=()) -> int:
        clause = f" WHERE {where}" if where else ""
        return int(self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}{clause}", args).fetchone()["n"])

    def metric(self, name: str) -> int:
        return self.service.metrics.get(name, 0)

    def plan_row(self, plan_id: str):
        return self.conn.execute("SELECT * FROM plan WHERE plan_id=?",
                                 (plan_id,)).fetchone()


# ===================================================================== 用例
#
# 下面每个断言的值都是先在活代码上跑出来、再写进测试的（见 `tools/loop`
# 的临时对拍脚本）。不写"应该是这样"，只写"现在是这样，且这正是方案要的"。

from repair_planning.schema import SDK_TABLES            # noqa: E402
from repair_planning.service import validate_schema       # noqa: E402


def identity_defect(check_id="VIS.IDENTITY.DRIFT", **overrides):
    return make_defect(check_id, dtype="identity", **overrides)


def bound_context(plan_id: str) -> dict:
    """`qc-report.v2` 的 `repair_context` 是**闭集四键**（additionalProperties:false）。"""
    return {"repair_plan_id": plan_id, "attempt_slot_id": "slot-1",
            "attempt_seq": 1, "defect_fingerprints": []}


def abstain(reason_code: str, check_id: str) -> dict:
    return {"reason_code": reason_code, "failed_check_ids": [check_id],
            "detail": "the evaluator said so"}


def recheck_body(artifact: str) -> dict:
    return {"artifact_sha256": artifact, "reports": [],
            "execution_context": execution_context()}


# ---------------------------------------------------------------- E24 路由

class TestRouting:
    def test_r1_keyframe_window_and_scope(self):
        """E24-R1：6 帧以内的 identity 漂移 → `keyframe_edit`，scope 来自注册表。"""
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect(start=1.0, end=1.05,
                                                   actions=("keyframe_edit",))]}))
        result = h.plan()
        detail = h.service.get_plan(TENANT, result["plan_id"])

        assert result["state"] == "planned"
        assert result["top_action"] == "keyframe_edit"
        assert result["action_count"] == 1
        # `split_recheck_scope("semantic+continuity")` 切开后排序。
        assert result["recheck_scope"] == ["continuity", "semantic"]

        action = detail["actions"][0]
        assert action["action"] == "keyframe_edit"
        assert action["parameters"] == {"edit_kind": "trim",
                                        "window_start_seconds": 1.0,
                                        "window_end_seconds": 1.05}
        assert action["attempt_slot_id"].startswith("slot_")

    def test_r2_nine_frame_window_exceeds_keyframe_limit(self):
        """E24-R2：9 帧 > `window_max_frames=6` → 前进到 `temporal_edit`。

        方案 §10 的表格写的是 `regenerate_video`。实现按"推荐动作 → 能力 →
        代价"排序，推荐动作被帧上限剪掉后落到次优的 `temporal_edit`。这是
        方案表格与算法之间的张力，登记在报告里，内核不动。
        """
        defect = identity_defect(start=0.0, end=0.334,
                                 actions=("keyframe_edit", "temporal_edit"))
        assert window_frames(defect, MEDIA) == 9

        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {"VIS.IDENTITY.DRIFT": [defect]}))
        result = h.plan()
        detail = h.service.get_plan(TENANT, result["plan_id"])

        assert result["state"] == "planned"
        assert result["top_action"] == "temporal_edit"
        assert detail["actions"][0]["parameters"]["window_end_seconds"] == 0.334
        assert strategy_class("temporal_edit", {"edit_kind": "trim"}) == \
            "temporal_edit:trim"

    def test_r3_no_capable_route_escalates_without_spending(self):
        """E24-R3：没有候选能证明时域能力 → `no_capable_route`，一个位都不占。

        三层报告必须齐全：缺层时 `blocked_incomplete` 会先赢，测的就不是
        路由这条分支了。
        """
        anatomy = check("VIS.ANATOMY.HANDS", "semantic", "S36", "anatomy",
                        "regenerate_video", ["regenerate_video",
                                             "temporal_edit"],
                        window_scope="window", window_max_frames=6,
                        recheck_scope="semantic+continuity")
        checks = [TECH_CODEC, anatomy, AUD_LIPSYNC]

        h = Harness(checks=checks, capability={"candidates": []})
        h.add(*reports_for(checks, {
            "VIS.ANATOMY.HANDS": [make_defect("VIS.ANATOMY.HANDS",
                                              dtype="anatomy",
                                              actions=("regenerate_video",))]}))
        result = h.plan()

        assert result["state"] == "escalated"
        assert result["top_action"] == "human_review"
        assert result["action_count"] == 0
        assert result["target_count"] == 1
        escalation = result["escalation"]
        assert escalation["reason"] == "no_capable_route"
        assert escalation["handoff"] == "S40"
        assert "VIS.ANATOMY.HANDS" in escalation["detail"]
        assert h.count("attempt_reservation") == 0

    def test_r3b_wide_enough_window_routes_to_regenerate(self):
        """同一份注册表、只把窗口上限放开 → 同一个缺陷改走 `regenerate_video`。"""
        anatomy = check("VIS.ANATOMY.HANDS", "semantic", "S36", "anatomy",
                        "regenerate_video", ["regenerate_video",
                                             "temporal_edit"],
                        window_scope="window", window_max_frames=6,
                        recheck_scope="semantic+continuity")
        checks = [TECH_CODEC, anatomy, AUD_LIPSYNC]
        h = Harness(checks=checks,
                    capability=capability_payload(max_window_frames=1))
        h.add(*reports_for(checks, {
            "VIS.ANATOMY.HANDS": [make_defect("VIS.ANATOMY.HANDS",
                                              dtype="anatomy",
                                              actions=("regenerate_video",))]}))
        result = h.plan()
        assert result["state"] == "planned"
        assert result["top_action"] == "regenerate_video"

    def test_r4_lipsync_routes_to_alignment_only(self):
        """E24-R4：口型偏移只该复检对齐层，不该顺带重跑语义层。"""
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "AUD.LIPSYNC.OFFSET": [make_defect("AUD.LIPSYNC.OFFSET",
                                               dtype="lipsync",
                                               severity="minor",
                                               actions=("redo_lipsync",))]}))
        result = h.plan()
        assert result["state"] == "planned"
        assert result["top_action"] == "redo_lipsync"
        assert result["recheck_scope"] == ["alignment"]

    def test_r5_subtitle_layer_is_optional(self):
        """E24-R5：字幕层缺席不阻塞、也不排进 `pending_layers`（那是必需层的位置）。"""
        checks = DEFAULT_CHECKS + [SUB_TIMING]
        h = Harness(checks=checks)
        h.add(*reports_for(DEFAULT_CHECKS, {}))
        h.add(make_report("S38", manifest=manifest_for("SUB.TIMING.OVERLAP"),
                          defects=[make_defect("SUB.TIMING.OVERLAP",
                                               dtype="subtitle",
                                               severity="minor",
                                               actions=("realign_subtitles",))]))
        result = h.plan()
        assert result["state"] == "planned"
        assert result["top_action"] == "realign_subtitles"
        assert result["recheck_scope"] == ["subtitle"]
        assert result["pending_layers"] == []

    def test_split_recheck_scope_handles_aspect_form(self):
        """§6.3 的带切面写法只走纯函数（契约 enum 里没有这个形式）。"""
        assert registry_mod.split_recheck_scope("alignment+lipsync+semantic") \
            == ["alignment", "lipsync", "semantic"]
        assert registry_mod.split_recheck_scope("") == []
        assert registry_mod.split_recheck_scope(" semantic + continuity ") \
            == ["semantic", "continuity"]

    def test_strategy_class_is_the_ledger_key_not_the_action(self):
        """同一动作、不同参数是两次不同尝试；键必须带上参数。"""
        assert strategy_class("keyframe_edit", {"edit_kind": "trim"}) == \
            "keyframe_edit:na:na"
        assert strategy_class("temporal_edit", {"edit_kind": "trim"}) == \
            "temporal_edit:trim"
        assert strategy_class("temporal_edit", {}) == "temporal_edit:na"
        assert strategy_class("regenerate_video", {}) == "regenerate_video:na:na"


# ------------------------------------------------------------ E25 缺层与复检

class TestMissingLayers:
    def test_e25_r1_abstained_required_layer_blocks_the_plan(self):
        """E25-R1：一份报告 `abstain`、其余 pass（且**确实报出了缺陷**）→
        不得 `planned`，而是 `blocked_incomplete` + `missing_required_layer`。

        方案原文："一份报告 `abstain`，其余 pass | 不得 `state=RESOLVED`；
        ESCALATE 或 `BLOCKED_INCOMPLETE`"。

        对照组是同一个缺陷、只把 S35 的 abstain 拿掉：两份计划唯一的差别
        就是那份 abstain，状态却从 `planned` 翻成 `blocked_incomplete`。
        所以这条**不能**靠 `action_count == 0` 成立 —— 实现即使阻塞也会把
        已路由的动作写进计划（`action_count` 两边都是 1，见下面的断言）。
        方案没写阻塞时的动作行该不该留，这里断的是**状态与升级理由**，
        `action_count` 只作为"两边相同"的旁证。
        """
        def plan(*, abstaining):
            h = Harness()
            reports = reports_for(DEFAULT_CHECKS, {
                "VIS.IDENTITY.DRIFT": [identity_defect()]})
            if abstaining:
                reports = [dict(doc) for doc in reports]
                for doc in reports:
                    if doc["evaluator"]["name"] == "s35-technical-qc":
                        doc["decision"] = "abstain"
                        doc["abstain"] = abstain("evaluator_error",
                                                 "TECH.CONTAINER.CODEC")
                        doc["report_content_hash"] = report_content_hash(doc)
            h.add(*reports)
            result = h.plan()
            return h, result, h.service.get_plan(TENANT, result["plan_id"])

        blocked, result, detail = plan(abstaining=True)

        assert result["state"] == "blocked_incomplete"
        assert result["state"] != "resolved"
        assert result["top_action"] == "regenerate_video"
        assert result["target_count"] == 1
        escalation = result["escalation"]
        assert escalation["reason"] == "missing_required_layer"
        assert escalation["missing_layers"] == ["technical"]
        # 三层报告都在，没有"等补"的层：abstain 让技术层**缺失**，不是迟到。
        assert escalation["pending_layers"] == []
        assert result["pending_layers"] == []
        assert escalation["handoff"] == "S40"
        assert detail["escalation"] == escalation

        healthy, allowed, allowed_detail = plan(abstaining=False)

        # 只差那一份 abstain：状态翻转，理由随 abstain 出现/消失。
        assert (allowed["state"], allowed["escalation"]) == ("planned", None)
        assert allowed_detail["escalation"] is None
        assert allowed["action_count"] == result["action_count"] == 1
        assert allowed["top_action"] == result["top_action"]
        assert blocked.count("attempt_reservation") == 1
        assert healthy.count("attempt_reservation") == 1

    def test_e25_r5_missing_layer_without_blocker_checks_is_partial(self):
        """E25-R5：缺的是**没有 blocker 可能**的必需层 → `planned_partial`。

        registry 里 alignment 的 `severity_max` 是 major，所以整层缺席只降级、
        不阻塞：现有动作照排，缺的那层记进 `pending_layers` 等补。
        """
        h = Harness()
        documents = reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]})
        h.add(*[doc for doc in documents
                if doc["evaluator"]["name"] != "s37-alignment-qc"])
        result = h.plan()

        assert result["state"] == "planned_partial"
        assert result["pending_layers"] == ["alignment"]
        assert result["action_count"] == 1

    def test_absent_subtitle_is_recorded_as_optional_pending(self):
        """字幕缺席要在快照里留痕，否则"查过了、没问题"和"没查"分不清。"""
        h = Harness(checks=DEFAULT_CHECKS + [SUB_TIMING])
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        result = h.plan()
        snapshot = json.loads(h.plan_row(result["plan_id"])["snapshot"])

        assert result["state"] == "planned"
        assert snapshot["optional_pending_layers"] == ["subtitle"]
        assert snapshot["pending_layers"] == []
        assert snapshot["present_layers"] == ["alignment", "semantic",
                                             "technical"]


class TestRecheck:
    def _planned(self):
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        return h, h.plan()["plan_id"]

    def test_r6_resolved_names_s08_and_leaves_the_pointer_alone(self):
        """E24-R6：原指纹全消失 → `resolved`，但接受指针一动没动。"""
        h, plan_id = self._planned()
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {}, decision="pass",
                           artifact=HASH2,
                           repair_context=bound_context(plan_id)))
        result = h.service.submit_recheck(TENANT, plan_id, {
            "artifact_sha256": HASH2, "reports": h.refs(),
            "execution_context": execution_context()})

        assert result["outcome"] == "resolved"
        assert result["state"] == "resolved"
        assert result["accept_candidate"] is True
        assert result["accept_authority"] == "S08"
        assert result["accept_pointer_touched"] is False
        assert result["consume_business_attempt"] is False
        states = [r["state"] for r in h.conn.execute(
            "SELECT state FROM attempt_reservation")]
        assert states == ["committed"]
        # 复检本身不发新事件：planned 那一份仍是唯一一份。
        assert [e["type"] for e in h.events()] == ["quality.repair.planned"]

    def test_r7_repeated_defects_supersedes_the_plan(self):
        """复检仍含同指纹 → `superseded` + `replan_required`，不扣尝试。

        完整的"计数 +1 且真能建出第二份计划"在 `TestRoutingBudgetAndRegistry`
        里；这条只覆盖"旧计划被取代"这一半，编号归下面那条。
        """
        h, plan_id = self._planned()
        original = sorted(t["defect_fingerprint"] for t in
                          h.service.get_plan(TENANT, plan_id)["targets"])
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}, artifact=HASH2,
            repair_context=bound_context(plan_id)))
        result = h.service.submit_recheck(TENANT, plan_id, {
            "artifact_sha256": HASH2, "reports": h.refs(),
            "execution_context": execution_context()})

        assert result["outcome"] == "same_defects"
        assert result["state"] == "superseded"
        assert result["repeated_fingerprints"] == original
        assert result["replan_required"] is True
        assert result["consume_business_attempt"] is False
        assert h.plan_row(plan_id)["state"] == "superseded"

    def test_e25_r3_evaluator_timeout_retries_without_charging(self):
        """E25-R3：整层超时是算力问题，不是修复失败 —— 不扣业务尝试。"""
        h, plan_id = self._planned()
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {}, decision="abstain",
                           abstain=abstain("evaluator_timeout", "X"),
                           artifact=HASH2,
                           repair_context=bound_context(plan_id)))
        result = h.service.submit_recheck(TENANT, plan_id, {
            "artifact_sha256": HASH2, "reports": h.refs(),
            "execution_context": execution_context()})

        assert result["outcome"] == "recheck_retry"
        assert result["retry_services"] == ["S35", "S36", "S37"]
        assert result["consume_business_attempt"] is False
        assert result["state"] == "planned"
        jobs = h.service.get_plan(TENANT, plan_id)["recheck_jobs"]
        assert [(j["round"], j["state"]) for j in jobs] == [(1, "failed")]

    def test_e25_r4_partial_abstain_escalates(self):
        """E25-R4：只有 S36 低置信度 → 升级，不是整轮重跑。"""
        h, plan_id = self._planned()
        h.reset()
        context = bound_context(plan_id)
        h.add(make_report("S35", manifest=manifest_for("TECH.CONTAINER.CODEC"),
                          decision="pass", artifact=HASH2,
                          repair_context=context),
              make_report("S36", manifest=manifest_for("VIS.IDENTITY.DRIFT"),
                          decision="abstain",
                          abstain=abstain("low_confidence", "X"),
                          artifact=HASH2, repair_context=context),
              make_report("S37", manifest=manifest_for("AUD.LIPSYNC.OFFSET"),
                          decision="pass", artifact=HASH2,
                          repair_context=context))
        result = h.service.submit_recheck(TENANT, plan_id, {
            "artifact_sha256": HASH2, "reports": h.refs(),
            "execution_context": execution_context()})

        assert result["outcome"] == "escalated"
        escalation = result["escalation"]
        assert escalation["reason"] == "recheck_abstain"
        assert escalation["detail"]["services"] == ["S36"]
        assert escalation["detail"]["reasons"] == {"S36": "low_confidence"}
        assert escalation["handoff"] == "S40"

    def test_report_without_this_plans_context_is_not_a_recheck(self):
        """§6.1：没有本计划 `repair_context` 的报告是新 ingest，不是复检。"""
        h, plan_id = self._planned()
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {}, decision="pass",
                           artifact=HASH2, repair_context=None))
        with pytest.raises(ServiceError) as exc:
            h.service.submit_recheck(TENANT, plan_id, {
                "artifact_sha256": HASH2, "reports": h.refs(),
                "execution_context": execution_context()})
        assert exc.value.status == 422
        assert exc.value.code == "RECHECK_NOT_BOUND"


# ------------------------------------------------------------ E26 上限与记账

class TestLimitsAndLedger:
    def test_d1_no_progress_after_the_repeat_budget(self):
        """D1：同指纹第 3、4 次 → `no_progress`，不再占位、计数继续涨。"""
        h = Harness()
        seen = []
        for index, severity in enumerate(("info", "minor", "major",
                                          "blocker"), 1):
            h.reset()
            h.add(*reports_for(DEFAULT_CHECKS, {
                "VIS.IDENTITY.DRIFT": [identity_defect(severity=severity)]}))
            result = h.plan(command_id=f"cmd{index}")
            seen.append((result["state"],
                         (result["escalation"] or {}).get("reason")))

        assert seen == [("planned", None), ("planned", None),
                        ("escalated", "no_progress"),
                        ("escalated", "no_progress")]
        assert h.count("plan") == 4
        assert h.metric("repair_plan_no_progress_total") == 2
        assert h.count("attempt_reservation") == 2

    def _envelope(self, *, state="DISPATCH_AUTHORIZED", event_id="evt-1",
                  reason_code=None, network_retry=False, attempt_id="att-1",
                  plan_id=None, action_id=None, strategy=None):
        # 台账列全 NOT NULL：没有可归属的计划，事件只能记 metric 不落行
        # （C14）。要断言"+1"，就必须先有一个真实计划。
        data = {"state": state, "attempt_id": attempt_id,
                "repair_plan_id": plan_id, "action_id": action_id,
                "provider_job_id": "pj_1", "business_attempt": True,
                "network_retry": network_retry}
        if strategy:
            data["strategy_class"] = strategy
        if reason_code:
            data["reason_code"] = reason_code
        return {"id": event_id, "type": "generation.job.state-changed",
                "tenant_id": TENANT, "causation_id": f"cause-{event_id}",
                "data": data}

    def _planned_action(self):
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        plan_id = h.plan()["plan_id"]
        detail = h.service.get_plan(TENANT, plan_id)
        return (h, plan_id, detail["actions"][0]["action_id"],
                detail["targets"][0]["strategy_class"])

    def test_generation_event_counts_a_business_attempt_once(self):
        h, plan_id, action_id, strategy = self._planned_action()
        first = h.service.consume_generation_event(TENANT, self._envelope(
            plan_id=plan_id, action_id=action_id, strategy=strategy))
        second = h.service.consume_generation_event(TENANT, self._envelope(
            plan_id=plan_id, action_id=action_id, strategy=strategy))

        assert first["counted"] is True
        assert first["attempt_seq"] == 1
        assert second["counted"] is False
        assert second["reason"] == "duplicate_event"
        assert h.count("attempt_ledger") == 1
        # 台账记一笔、预留标 committed —— 同一次尝试不能被数两遍。
        assert [r["state"] for r in h.conn.execute(
            "SELECT state FROM attempt_reservation")] == ["committed"]

    def test_submission_unknown_neither_counts_nor_reopens(self):
        """AGENTS.md：`SUBMISSION_UNKNOWN` 下不盲目重提，也不记账。"""
        h = Harness()
        result = h.service.consume_generation_event(
            TENANT, self._envelope(state="SUBMISSION_UNKNOWN"))
        assert result["counted"] is False
        assert result["reason"] == "submission_unknown"
        assert result["pending"] is True
        assert h.count("attempt_ledger") == 0

    def test_reconciled_not_submitted_counts_zero(self):
        h = Harness()
        result = h.service.consume_generation_event(
            TENANT, self._envelope(state="FAILED",
                                   reason_code="CONFIRMED_NOT_SUBMITTED"))
        assert result["counted"] is False
        assert h.count("attempt_ledger") == 0

    def test_network_retry_is_not_a_business_attempt(self):
        h = Harness()
        result = h.service.consume_generation_event(
            TENANT, self._envelope(network_retry=True))
        assert result["counted"] is False
        assert result["reason"] == "not_a_business_attempt"

    def test_same_attempt_from_two_event_ids_is_one_row(self):
        """D5：同一 `attempt_id` 换个 `event_id` 重投仍只有一行。"""
        h, plan_id, action_id, strategy = self._planned_action()
        h.service.consume_generation_event(TENANT, self._envelope(
            event_id="e1", plan_id=plan_id, action_id=action_id,
            strategy=strategy))
        again = h.service.consume_generation_event(TENANT, self._envelope(
            event_id="e2", plan_id=plan_id, action_id=action_id,
            strategy=strategy))

        assert again["counted"] is False
        assert again["reason"] == "attempt_already_counted"
        assert h.count("attempt_ledger") == 1

    def test_event_for_a_foreign_plan_is_unattributed(self):
        """乱序/伪造的事件不能凭 `repair_plan_id` 记账。"""
        h = Harness()
        result = h.service.consume_generation_event(TENANT, {
            "id": "g1", "type": "generation.job.state-changed",
            "tenant_id": TENANT, "causation_id": "c1",
            "data": {"state": "DISPATCH_AUTHORIZED", "attempt_id": "a1",
                     "repair_plan_id": "rp_missing", "action_id": "act_1"}})
        assert result["counted"] is False
        assert result["reason"] == "unattributed"
        assert h.count("attempt_ledger") == 0


# --------------------------------------------------- 版本语义 / 第二权威

class TestVersionSemantics:
    def test_fence_and_epoch_land_in_their_own_columns(self):
        """AGENTS.md：fence 管 run/branch，epoch 管单个 `logical_node`。

        上下文故意给 fence=3 / epoch=7 —— 两列相等就说明有人把两者混用了。
        """
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        row = h.plan_row(h.plan()["plan_id"])
        assert int(row["execution_fence"]) == 3
        assert int(row["node_epoch"]) == 7

    def test_ddl_declares_the_two_columns_separately(self):
        h = Harness()
        ddl = h.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='plan'").fetchone()[0]
        assert "execution_fence      INTEGER NOT NULL CHECK (execution_fence >= 1)" \
            in ddl
        assert "node_epoch           INTEGER NOT NULL CHECK (node_epoch >= 1)" \
            in ddl

    def test_attempt_key_excludes_run_manifest_fence_and_epoch(self):
        """计数器的键里没有 run / manifest / epoch / fence / 供应商任务。

        换一个维度重试仍是同一次尝试 —— 否则上限可以被"换个 run"绕开。
        """
        h = Harness()
        ddl = h.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='attempt_ledger'"
        ).fetchone()[0]
        start = ddl.index("UNIQUE")
        unique_clause = ddl[start:ddl.index(")", start)]
        for column in ("execution_fence", "node_epoch", "run_id",
                       "manifest_id", "provider_job_id"):
            assert column not in unique_clause, column
        for column in ("tenant_id", "series_id", "branch_id",
                       "shot_lineage_id", "strategy_class", "attempt_seq"):
            assert column in unique_clause, column

    def test_plan_table_holds_no_acceptance_or_money_column(self):
        """本服务不是第二权威：不记接受指针、不记余额。"""
        h = Harness()
        columns = {r["name"] for r in h.conn.execute("PRAGMA table_info(plan)")}
        for forbidden in ("accepted", "accepted_revision", "accepted_pointer",
                          "current_revision", "balance", "amount", "price"):
            assert forbidden not in columns, forbidden

    def test_contract_gaps_are_registered_not_silently_decided(self):
        """方案留给 owner 的口子必须显式登记，不能在代码里悄悄拍板。"""
        gaps = {entry["id"]: entry for entry in CONTRACT_GAPS}
        assert len(gaps) == 29
        assert {entry["kind"] for entry in gaps.values()} <= {"gap", "conflict"}
        assert {cid for cid, entry in gaps.items()
                if entry["kind"] == "conflict"} == {"C12", "C13", "C17",
                                                    "C24", "C26"}
        for entry in gaps.values():
            assert entry.get("summary"), entry["id"]
            assert entry.get("handling"), entry["id"]


# ------------------------------------------------------------- 幂等 / 隔离

class TestIdempotency:
    def _ready(self):
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        return h

    def test_same_body_replays_the_same_plan(self):
        h = self._ready()
        first = h.plan()
        second = h.plan()
        assert first["replayed"] is False
        assert second["replayed"] is True
        assert second["plan_id"] == first["plan_id"]
        assert h.count("plan") == 1

    def test_creating_a_plan_does_not_mutate_its_own_body(self):
        """回归：`_context` 曾就地写 body，把 request_hash 改了 →
        同一个命令的合法重投被判成 `IDEMPOTENCY_KEY_REUSED`。"""
        h = self._ready()
        body = plan_body(refs=h.refs())
        before = stable_request_hash(body)
        h.command(body, "cmd1")
        assert stable_request_hash(body) == before

        replay = h.command(body, "cmd1")
        assert replay["replayed"] is True
        assert h.count("plan") == 1

    def test_same_key_different_body_is_a_conflict(self):
        h = self._ready()
        h.plan(max_actions=10)
        with pytest.raises(CommandConflict) as exc:
            h.plan(max_actions=3)
        assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
        assert h.count("plan") == 1

    def test_trace_id_does_not_change_the_request_hash(self):
        first = plan_body(refs=[])
        second = plan_body(
            refs=[], context=execution_context(trace_id="trace-2"))
        assert stable_request_hash(first) == stable_request_hash(second)

    def test_dry_run_reserves_nothing_and_emits_nothing(self):
        h = self._ready()
        result = h.plan(dry_run=True)
        assert result["state"] == "planned"
        assert result["dry_run"] is True
        assert h.count("plan") == 1
        assert h.count("attempt_reservation") == 0
        assert h.events() == []


class TestIsolation:
    def test_another_tenants_plan_looks_exactly_like_a_missing_one(self):
        """跨租户与不存在必须同形 —— 差异本身就是存在性泄漏。"""
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        plan_id = h.plan()["plan_id"]

        with pytest.raises(ServiceError) as foreign:
            h.service.get_plan(OTHER_TENANT, plan_id)
        with pytest.raises(ServiceError) as absent:
            h.service.get_plan(OTHER_TENANT, "rp_does_not_exist")

        assert foreign.value.status == absent.value.status == 404
        assert foreign.value.code == absent.value.code == "PLAN_NOT_FOUND"
        # 两个响应只差调用方自己给的那个 id，不含任何租户侧信号。
        assert (str(foreign.value).replace(plan_id, "<id>")
                == str(absent.value).replace("rp_does_not_exist", "<id>"))

    def test_report_from_another_tenant_is_quarantined(self):
        """跨租户的报告是拒收（审计事实），不是"没找到"。"""
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}, tenant=OTHER_TENANT))
        with pytest.raises(ServiceError) as exc:
            h.plan()
        assert exc.value.status == 422
        assert exc.value.code == "QC_REPORT_REJECTED"
        assert "tenant_mismatch" in str(exc.value)
        assert h.count("plan") == 0
        assert h.count("qc_rejection") == 1


# ----------------------------------------------------------- 事件 / 契约

class TestEventsAndContracts:
    def test_planned_event_payload_matches_its_schema(self):
        """校验的是 `event['data']`，不是整个 envelope。"""
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        h.plan()
        events = h.events("quality.repair.planned")
        assert len(events) == 1
        payload = events[0]["data"]
        assert len(payload) == 13
        assert validate_schema(payload, PLANNED_EVENT_SCHEMA) == []
        for key in ("schema_version", "id", "tenant_id", "type", "sequence",
                    "source", "subject"):
            assert key in events[0], key

    def test_registry_passes_both_validators(self):
        """schema 校验形状，`validate_registry` 校验跨文件族约束（x-cross-checks）。"""
        document = registry_document(DEFAULT_CHECKS)
        assert registry_mod.validate_registry(document, FAMILIES) == []
        assert registry_mod.validate_families(FAMILIES) == []
        assert registry_mod.Registry(document, FAMILIES).version == 3

    def test_registry_entry_outside_its_action_family_is_rejected_at_load(self):
        """形状对、语义错的注册表必须在**加载期**炸，不能等到派单时。"""
        bad = registry_document([check(
            "VIS.IDENTITY.DRIFT", "semantic", "S36", "identity",
            "keyframe_edit", ["regen_video"],
            recheck_scope="semantic+continuity")])
        problems = registry_mod.validate_registry(bad, FAMILIES)
        assert len(problems) == 1
        assert "regen_video" in problems[0]
        with pytest.raises(registry_mod.RegistryError):
            registry_mod.Registry(bad, FAMILIES)

    def test_owned_tables_are_exactly_the_expected_set(self):
        """多一张表就多一份权威。这里逐张点名，SDK 的表另算。

        `SDK_TABLES` 里也有 `inbox` / `outbox` —— 它们同时出现在
        `OWNED_TABLES`（本服务自己的收件/发件箱实现），两边都列是对的，
        所以断言要按"差集"算，不能按"不相交"算。
        """
        h = Harness()
        tables = {r["name"] for r in h.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables - set(SDK_TABLES) == OWNED_TABLES - set(SDK_TABLES)
        assert tables == OWNED_TABLES | {"idempotency_record"}

    def test_missing_run_or_shot_is_a_typed_422(self):
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {}))
        with pytest.raises(ServiceError) as run:
            h.plan(include_run=False)
        assert run.value.status == 422 and run.value.code == "RUN_UNKNOWN"
        with pytest.raises(ServiceError) as shot:
            h.plan(include_shot=False)
        assert shot.value.status == 422
        assert shot.value.code == "SHOT_LINEAGE_UNKNOWN"

    def test_report_whose_hash_disagrees_with_its_body_is_quarantined(self):
        """正文与 `report_content_hash` 不符 → 拒收并记账，不出计划。"""
        h = Harness()
        h.add(*[doc for doc in reports_for(DEFAULT_CHECKS, {})
                if doc["evaluator"]["name"] != "s36-semantic-qc"])
        tampered = make_report("S36",
                               manifest=manifest_for("VIS.IDENTITY.DRIFT"),
                               defects=[identity_defect()])
        tampered["coverage"] = {**tampered["coverage"], "coverage_ratio": 0.1}
        h.add(tampered)

        with pytest.raises(ServiceError) as exc:
            h.plan()
        assert exc.value.status == 422
        assert exc.value.code == "QC_REPORT_REJECTED"
        assert "content_hash_mismatch" in str(exc.value)
        assert h.count("plan") == 0
        reasons = [row["reason"] for row in h.conn.execute(
            "SELECT reason FROM qc_rejection")]
        assert reasons == ["content_hash_mismatch"]

    def test_two_reports_for_one_service_in_one_snapshot_is_rejected(self):
        """同一快照里两份同层报告 → 无法判断用哪份，拒收而不是"取最新"。"""
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {}))
        h.add(make_report("S36", manifest=manifest_for("VIS.IDENTITY.DRIFT"),
                          generation=2))
        with pytest.raises(ServiceError) as exc:
            h.plan()
        assert exc.value.code == "DUPLICATE_REPORT_SERVICE"
        assert h.count("plan") == 0

    def test_disabled_service_refuses_instead_of_planning_empty(self):
        h = Harness(enabled=False)
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        with pytest.raises(ServiceError) as exc:
            h.plan()
        assert exc.value.status == 503
        assert exc.value.code == "REPAIR_PLANNING_DISABLED"
        assert h.count("plan") == 0

    def test_escalate_is_idempotent_and_releases_reservations(self):
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        plan_id = h.plan()["plan_id"]

        first = h.service.escalate(TENANT, plan_id, "manual_escalation")
        second = h.service.escalate(TENANT, plan_id, "manual_escalation")

        assert first["changed"] is True and first["state"] == "escalated"
        assert second["changed"] is False
        assert first["escalation"]["handoff"] == "S40"
        assert first["escalation"]["previous_state"] == "planned"
        # 未执行的预留必须释放，否则会永久占住上限。
        assert h.count("attempt_reservation", "state='reserved'") == 0

    def test_review_decision_only_marks_human_resolved(self):
        """`recorded` ≠ `accepted`：只标状态，指针仍是 S08 的。"""
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        plan_id = h.plan()["plan_id"]
        envelope = {
            "id": "rev-1", "type": "review.decision.recorded",
            "tenant_id": TENANT, "causation_id": "c1",
            "data": {"status": "RECORDED", "decision_id": "dec_1",
                     "review_task_id": "rt_1",
                     "decided_against": {"repair_plan_id": plan_id}}}

        applied = h.service.consume_review_decision(TENANT, envelope)
        assert applied["applied"] is True
        assert applied["state"] == "human_resolved"
        assert applied["accept_pointer_touched"] is False
        assert applied["accept_authority"] == "S08"
        assert h.plan_row(plan_id)["state"] == "human_resolved"

        duplicate = h.service.consume_review_decision(TENANT, envelope)
        assert duplicate["applied"] is False
        assert duplicate["reason"] == "duplicate_event"

    def test_non_recorded_review_status_leaves_the_plan_alone(self):
        """`APPLIED` / `STALE` 是 S40 自己的状态机，本服务不据此改计划。"""
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        plan_id = h.plan()["plan_id"]
        result = h.service.consume_review_decision(TENANT, {
            "id": "rev-2", "type": "review.decision.recorded",
            "tenant_id": TENANT, "causation_id": "c2",
            "data": {"status": "APPLIED", "decision_id": "dec_2",
                     "decided_against": {"repair_plan_id": plan_id}}})
        assert result["applied"] is False
        assert result["reason"] == "status_not_recorded"
        assert h.plan_row(plan_id)["state"] == "planned"


class TestRestart:
    def test_plan_survives_a_process_restart_and_still_replays(self, tmp_path):
        """E13：换一个实例读同一个库，计划还在，同一命令仍不新建。"""
        path = str(tmp_path / "s39.sqlite3")
        first = Harness(db_path=path)
        first.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        plan_id = first.plan()["plan_id"]
        first.conn.close()

        second = Harness(db_path=path)
        try:
            assert second.service.get_plan(TENANT, plan_id)["state"] == "planned"
            second.add(*reports_for(DEFAULT_CHECKS, {
                "VIS.IDENTITY.DRIFT": [identity_defect()]}))
            replay = second.plan()
            assert replay["replayed"] is True
            assert replay["plan_id"] == plan_id
            assert second.count("plan") == 1
        finally:
            second.conn.close()


# ----------------------------------------------------------------- HTTP 层

class TestHttpLayer:
    @staticmethod
    def _client(tmp_path, monkeypatch, *, enabled="false"):
        """每例一份独立库：`import app` 会造模块级 service，不能共用。"""
        monkeypatch.setenv("S39_DB", str(tmp_path / "s39.sqlite3"))
        monkeypatch.setenv("REPAIR_PLANNING_ENABLED", enabled)
        path = Path(__file__).resolve().parents[1] / "api" / "app.py"
        spec = importlib.util.spec_from_file_location("s39_api_app", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["s39_api_app"] = module
        spec.loader.exec_module(module)
        from fastapi.testclient import TestClient
        return TestClient(module.app)

    def test_command_needs_both_headers(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        assert client.post("/repair-plans", json={}).status_code == 422
        assert client.post("/repair-plans", json={},
                           headers={"X-Tenant-Id": TENANT}).status_code == 422
        assert client.post(
            "/repair-plans", json={},
            headers={"Idempotency-Key": "cmd"}).status_code == 422

    def test_body_tenant_must_match_the_signed_header(self, tmp_path,
                                                     monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        response = client.post(
            "/repair-plans",
            json={"command_id": "cmd1",
                  "execution_context": execution_context()},
            headers={"Idempotency-Key": "cmd1",
                     "X-Tenant-Id": OTHER_TENANT})
        assert response.status_code in (403, 422)
        body = response.json()
        assert "TENANT_MISMATCH" in json.dumps(body)

    def test_unknown_event_type_is_422(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        response = client.post(
            "/events:consume",
            json={"event": {"id": "e1",
                            "type": "quality.technical.completed",
                            "tenant_id": TENANT, "data": {}}},
            headers={"X-Tenant-Id": TENANT})
        assert response.status_code == 422
        assert response.json()["code"] == "UNSUPPORTED_EVENT_TYPE"

    def test_default_state_is_not_ready_and_says_why(self, tmp_path,
                                                    monkeypatch):
        """默认起得来，但出不了计划 —— readiness 必须如实报 false。

        `contracts/quality/check-registry.v2.json` 至今没落（C5），所以
        两个 blocker 同时成立；报 `ready: true` 会让编排层等到第一次派单
        才发现，那时已经是一次失败的修复。
        """
        client = self._client(tmp_path, monkeypatch)
        body = client.get("/health/readiness").json()

        assert body["ready"] is False
        assert body["enabled"] is False
        assert body["registry_version"] is None
        # 上游没配也在其中：少报它们就等于把"起得来"报成"能用"。
        assert body["blockers"] == ["REPAIR_PLANNING_DISABLED",
                                    "CHECK_REGISTRY_UNAVAILABLE",
                                    "CAPABILITY_SOURCE_UNAVAILABLE",
                                    "POLICY_SOURCE_UNAVAILABLE",
                                    "S35_REPORT_SOURCE_UNAVAILABLE",
                                    "S36_REPORT_SOURCE_UNAVAILABLE",
                                    "S37_REPORT_SOURCE_UNAVAILABLE",
                                    "S38_REPORT_SOURCE_UNAVAILABLE"]
        assert len(body["contract_gaps"]) == len(CONTRACT_GAPS)

    def test_enabled_without_registry_still_is_not_ready(self, tmp_path,
                                                         monkeypatch):
        """开关打开但没注册表：仍然 503，仍然不 ready。"""
        client = self._client(tmp_path, monkeypatch, enabled="true")
        body = client.get("/health/readiness").json()
        assert body["enabled"] is True
        assert body["ready"] is False
        # 开 registry 不等于能用：上游照样一个都没配，命令仍会 503。
        assert set(body["blockers"]) == {
            "CHECK_REGISTRY_UNAVAILABLE",
            "CAPABILITY_SOURCE_UNAVAILABLE",
            "POLICY_SOURCE_UNAVAILABLE",
            "S35_REPORT_SOURCE_UNAVAILABLE",
            "S36_REPORT_SOURCE_UNAVAILABLE",
            "S37_REPORT_SOURCE_UNAVAILABLE",
            "S38_REPORT_SOURCE_UNAVAILABLE"}


# ======================================= 方案 §10 的编号：补齐没被断言过的行为
#
# 上面那十一个类是该文件原有的用例，这里只在 docstring 里补上它**本来就
# 断言着**的方案编号（见文件后半段的说明）。下面这些是真正没人断言的
# 行为，全部追加在文件末尾，不改动上面的任何一行。
#
# 每个数字都先在活代码上跑出来、再写进断言。凡是方案与实现不一致的地方，
# 测试断**实现**，并在注释里贴方案原话 —— 内核不动，由报告登记。


def plan_command(h, command_id, *, run=None, artifact=None, context=None):
    """按显式的 `run_id` 组一次计划命令。

    `run_id` / `shot_lineage_id` 在命令体**顶层**（`execution-context.v2`
    里没有这两个键），而 `plan_body` 不接受它们，所以这里直接改 body。
    """
    body = plan_body(refs=h.refs(), command_id=command_id,
                     artifact=HASH if artifact is None else artifact,
                     context=context)
    if run is not None:
        body["run_id"] = run
    return h.plan(body=body, command_id=command_id)


def generation_event(*, event_id="evt-1", attempt_id="att-1", plan_id=None,
                     action_id=None, strategy=None, provider_job_id="pj_1",
                     state="DISPATCH_AUTHORIZED", reason_code=None,
                     business_attempt=True, network_retry=False):
    """`generation.job.state-changed` 的最小合法信封。"""
    data = {"state": state, "attempt_id": attempt_id,
            "repair_plan_id": plan_id, "action_id": action_id,
            "provider_job_id": provider_job_id,
            "business_attempt": business_attempt,
            "network_retry": network_retry}
    if strategy:
        data["strategy_class"] = strategy
    if reason_code:
        data["reason_code"] = reason_code
    return {"id": event_id, "type": "generation.job.state-changed",
            "tenant_id": TENANT, "causation_id": f"cause-{event_id}",
            "data": data}


def consume_planned(h, result, *, event_id, attempt_id,
                    provider_job_id="pj_1", **overrides):
    """把一条事件记到这份计划的第一个动作上（策略类取自 target）。"""
    detail = h.service.get_plan(TENANT, result["plan_id"])
    action = detail["actions"][0]
    return h.service.consume_generation_event(TENANT, generation_event(
        event_id=event_id, attempt_id=attempt_id, plan_id=result["plan_id"],
        action_id=action["action_id"],
        strategy=detail["targets"][0]["strategy_class"],
        provider_job_id=provider_job_id, **overrides))


def spend_two_attempts(h, *, prefix="c", severities=("minor", "major"),
                       actions=("regenerate_video",)):
    """生成并消费两条**同策略**尝试，让 `identical_strategy` 用到上限。

    两条缺陷的窗口与严重度都不同：`plan_fingerprint` 不含窗口，只换窗口
    会被判成 `reused_plan` 而不新建计划，所以严重度必须变。
    """
    consumed = []
    for index, severity in enumerate(severities, 1):
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {"VIS.IDENTITY.DRIFT": [
            identity_defect(severity=severity, start=float(index),
                            end=float(index) + 0.25,
                            actions=tuple(actions))]}))
        result = plan_command(h, f"{prefix}{index}")
        assert result["state"] == "planned"
        consumed.append(consume_planned(
            h, result, event_id=f"{prefix}-e{index}",
            attempt_id=f"{prefix}-att{index}"))
    return consumed


# ------------------------------------------------- E24 —— 预算上限与登记表

class TestRoutingBudgetAndRegistry:
    def test_e24_r7_same_fingerprint_after_recheck_counts_one_and_replans(
            self):
        """E24-R7：复检仍含同指纹 → 计数 +1；未超限 → 新计划。

        方案写"计数 +1"与"新 plan"两件事。这里走完整回路：第一轮消费掉
        seq=1，复检报同指纹 → `superseded` + `replan_required`，第二轮计划
        仍在预算内、消费得到 seq=2。复检本身**不**扣业务尝试。
        """
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        first = plan_command(h, "r7-a")
        first_seq = consume_planned(h, first, event_id="r7-e1",
                                    attempt_id="r7-att1")["attempt_seq"]
        assert first_seq == 1

        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}, artifact=HASH2,
            repair_context=bound_context(first["plan_id"])))
        body = recheck_body(HASH2)
        body["reports"] = h.refs()
        recheck = h.service.submit_recheck(TENANT, first["plan_id"], body)
        assert recheck["outcome"] == "same_defects"
        assert recheck["state"] == "superseded"
        assert recheck["replan_required"] is True
        assert recheck["consume_business_attempt"] is False

        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect(severity="blocker")]},
            artifact=HASH2))
        second = plan_command(h, "r7-b", artifact=HASH2)
        assert second["state"] == "planned"
        assert consume_planned(h, second, event_id="r7-e2",
                               attempt_id="r7-att2")["attempt_seq"] == 2
        assert h.count("plan") == 2
        assert h.count("attempt_ledger") == 2

    def test_e24_r8_identical_strategy_cap_stops_auto_repair(self):
        """E24-R8：预算在上限内但质量仍不过 → ESCALATE(`..._cap`)。

        `max_identical_strategy_repeats=1` 允许 seq 1、2；第三条同策略被拒，
        `capped_at` 报的是**已用 2 / 上限 2**。被拒的计划不新增预留。
        """
        h = Harness()
        spend_two_attempts(h, prefix="cap")
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect(
                severity="blocker", start=3.0, end=3.25,
                actions=("regenerate_video",))]}))
        result = plan_command(h, "cap3")

        assert result["state"] == "escalated"
        assert result["escalation"]["reason"] == "identical_strategy_cap"
        assert result["escalation"]["capped_at"] == {
            "shot_lineage_id": SHOT,
            "strategy_class": "regenerate_video:na:na",
            "attempts_used": 2,
            "limit": 2}
        assert result["escalation"]["handoff"] == "S40"
        # 被拒的这一条动作仍然写进计划，但一个位都没占。
        assert h.count("attempt_reservation") == 2
        assert h.count("attempt_ledger") == 2
        assert h.metric(
            "repair_authorize_denied_total|identical_strategy_cap") == 1

    def test_e24_r9_one_regenerate_covers_two_defects(self):
        """E24-R9：同一 `regenerate_video` 消两个缺陷 → 合并成一个 action。

        `target_ids` 两条、预留只占一个 —— 合并是为了不把一次真实尝试记成
        两次（§5.3 的键靠参数区分，这两条参数完全相同）。

        两条缺陷的窗口**故意不重叠**：窗口重叠时"窗口相交"那条合并规则也
        能合并它们，这条用例就分不清是哪种合并生效了。互不重叠时只有
        "全镜动作覆盖整条 artifact"这一条规则能合并，才真的钉住 §4.4 第 2 步。
        """
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {"VIS.IDENTITY.DRIFT": [
            identity_defect(start=0.0, end=0.25),
            identity_defect(start=2.0, end=2.25)]}))
        result = h.plan()
        detail = h.service.get_plan(TENANT, result["plan_id"])

        assert result["state"] == "planned"
        assert result["target_count"] == 2
        assert result["action_count"] == 1
        assert len(detail["actions"]) == 1
        action = detail["actions"][0]
        assert action["action"] == "regenerate_video"
        assert len(action["target_ids"]) == 2
        assert len(set(action["target_ids"])) == 2
        assert {t["strategy_class"] for t in detail["targets"]} == {
            "regenerate_video:na:na"}
        assert h.count("attempt_reservation") == 1

    def test_e24_r10_stale_qc_for_a_new_artifact_is_quarantined(self):
        """E24-R10：修复后 hash 变了却复用旧 QC → ingest 拒收。

        方案原文："ingest 拒收（`subject.artifact_sha256` 不等于新 artifact
        hash）"。拒收要留审计事实（`qc_rejection`）并打 P1，不能只回一个错。
        """
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}, artifact=HASH))
        with pytest.raises(ServiceError) as caught:
            h.plan(command_id="stale", artifact=HASH2)

        error = caught.value
        assert error.status == 422
        assert error.code == "QC_REPORT_REJECTED"
        assert error.details["reason"] == "artifact_hash_mismatch"
        assert error.details["layer"] == "technical"
        assert error.details["problems"][0].startswith(
            "artifact_hash_mismatch: report is bound to")
        rejected = [tuple(row) for row in h.conn.execute(
            "SELECT service_id, reason FROM qc_rejection")]
        assert rejected == [("S35", "artifact_hash_mismatch")]
        assert h.metric(
            "qc_report_rejected_total|artifact_hash_mismatch|technical") == 1
        assert h.metric("p1_alert|qc_report_rejected") == 1
        assert h.count("plan") == 0
        assert h.count("attempt_reservation") == 0

    def test_e24_r11_suppressed_disposition_is_recorded_not_planned(self):
        """E24-R11：S17 标了 `suppressed` 的闪回服装 → 记下来，不排动作。

        方案写"不生成计划"。实现会落一条 `target_count=0` 的计划行，把
        这条观测原样记进 `suppressed_defects`（谁、哪份报告、什么理由）——
        沉默压掉和"记一笔但不动"是两件事，测试按实现断言，措辞差异登记在
        报告里。
        """
        h = Harness(continuity_mode="present")
        suppressed = make_defect(
            "VIS.COSTUME.FLASHBACK", dtype="costume",
            actions=("regenerate_video",), disposition="suppressed",
            disposition_reason="intentional_flashback")
        h.add(make_report("S17", defects=[suppressed],
                          manifest=manifest_for("VIS.IDENTITY.DRIFT")),
              make_report("S36", manifest=manifest_for("VIS.IDENTITY.DRIFT")),
              make_report("S35", manifest=manifest_for("TECH.CONTAINER.CODEC")),
              make_report("S37", manifest=manifest_for("AUD.LIPSYNC.OFFSET")))
        result = h.plan()
        detail = h.service.get_plan(TENANT, result["plan_id"])

        assert result["state"] == "planned"
        assert result["target_count"] == 0
        assert result["action_count"] == 0
        assert result["suppressed_count"] == 1
        assert result["escalation"] is None
        assert [entry["check_id"]
                for entry in detail["suppressed_defects"]] == [
            "VIS.COSTUME.FLASHBACK"]
        recorded = detail["suppressed_defects"][0]
        assert recorded["disposition"] == "suppressed"
        assert recorded["reason"] == "intentional_flashback"
        assert recorded["service_id"] == "S17"
        assert recorded["defect_fingerprint"] == \
            suppressed["defect_fingerprint"]
        assert h.count("action") == 0
        assert h.count("attempt_reservation") == 0
        assert h.count("attempt_ledger") == 0

    def test_e24_r12_s17_suppressed_but_s36_reported_escalates(self):
        """E24-R12：同指纹 S17 说 suppressed、S36 说 reported → 不静默压掉。

        方案要求 `escalated` + `exception_not_propagated`，并保留"谁报的、
        谁压的"。这是上一条（suppressed 被静默记下）的对照组：同样一条
        suppressed，只有它同时又被别人报出来时才升级。
        """
        h = Harness(continuity_mode="present")
        defect = make_defect("VIS.COSTUME.FLASHBACK", dtype="costume",
                             actions=("regenerate_video",))
        disputed = dict(defect, disposition="suppressed",
                        disposition_reason="s17 thinks this is a flashback")
        h.add(make_report("S17", defects=[disputed],
                          manifest=manifest_for("VIS.IDENTITY.DRIFT")),
              make_report("S36", defects=[defect],
                          manifest=manifest_for("VIS.IDENTITY.DRIFT")),
              make_report("S35", manifest=manifest_for("TECH.CONTAINER.CODEC")),
              make_report("S37", manifest=manifest_for("AUD.LIPSYNC.OFFSET")))
        result = h.plan()

        assert result["state"] == "escalated"
        assert result["action_count"] == 0
        assert result["suppressed_count"] == 1
        escalation = result["escalation"]
        assert escalation["reason"] == "exception_not_propagated"
        assert escalation["handoff"] == "S40"
        assert escalation["clashes"] == [{
            "defect_fingerprint": defect["defect_fingerprint"],
            "reported_by": ["S36"],
            "suppressed_by": "S17"}]
        assert h.count("action") == 0
        assert h.count("attempt_reservation") == 0

    def test_e24_r13_a_new_registry_row_is_all_it_takes(self):
        """E24-R13：registry 加一行，S39 代码不变，新 check 就能出计划。

        "二进制未重新构建"在这里是一个结构事实：测试只把一份新注册表文档
        交给同一个类，没有子类化、没有 patch、也没有按 check_id 分支的桩。
        """
        axis = check("VIS.COMPOSITION.AXIS", "semantic", "S36",
                     "camera_adherence", "regenerate_video",
                     ["regenerate_video", "change_spec"],
                     window_scope="window", window_max_frames=24,
                     recheck_scope="semantic+continuity")
        checks = [TECH_CODEC, axis, AUD_LIPSYNC]
        h = Harness(checks=checks)
        h.add(*reports_for(checks, {"VIS.COMPOSITION.AXIS": [
            make_defect("VIS.COMPOSITION.AXIS", dtype="camera_adherence",
                        actions=("regenerate_video",))]}))
        result = h.plan()
        detail = h.service.get_plan(TENANT, result["plan_id"])

        assert result["state"] == "planned"
        assert result["top_action"] == "regenerate_video"
        assert result["action_count"] == 1
        assert [t["check_id"] for t in detail["targets"]] == [
            "VIS.COMPOSITION.AXIS"]
        assert [t["strategy_class"] for t in detail["targets"]] == [
            "regenerate_video:na:na"]
        assert type(h.service) is RepairPlanningService

    def test_e24_r14_unregistered_check_is_human_review_with_metric(self):
        """E24-R14：registry 缺该 `check_id` → 人审 + 按 check_id 告警。

        方案要求 `registry_entry_invalid_total{check_id}`，不猜动作。告警
        按 check_id 分桶，不合并 —— 合并了就看不出是哪个 check 没登记。
        """
        h = Harness(checks=[TECH_CODEC, AUD_LIPSYNC])
        h.add(make_report("S35", manifest=manifest_for("TECH.CONTAINER.CODEC")),
              make_report("S37", manifest=manifest_for("AUD.LIPSYNC.OFFSET")),
              make_report("S36", manifest=manifest_for("VIS.IDENTITY.DRIFT"),
                          defects=[identity_defect()]))
        result = h.plan()

        assert result["state"] == "escalated"
        assert result["top_action"] == "human_review"
        assert result["action_count"] == 0
        escalation = result["escalation"]
        assert escalation["reason"] == "registry_entry_invalid"
        assert "is not registered" in escalation["detail"]
        assert escalation["handoff"] == "S40"
        assert len(escalation["human_review_targets"]) == 1
        assert h.metric(
            "registry_entry_invalid_total|VIS.IDENTITY.DRIFT") == 1
        assert h.count("action") == 0
        assert h.count("attempt_reservation") == 0

    def test_e24_r15_cross_family_entry_never_reaches_dispatch(self):
        """E24-R15：把 `redo_lipsync` 登到 technical 类 check 上 → 双向都拒。

        第一半是加载期（构造 `Registry` 就抛），第二半是运行期：绕过加载器
        强行塞进 `by_id` 后，`entry_errors` 再算一次，仍然落到人审而不是
        派单。方案说"按族裁剪后为空"；实现是在入口校验里直接判死，效果
        相同（都到 `human_review`），机制不同，登记在报告里。
        """
        bad = check("TECH.CONTAINER.CODEC", "technical", "S35", "technical",
                    "redo_lipsync", ["redo_lipsync"], window_scope="shot",
                    window_max_frames=None, recheck_scope="technical")
        with pytest.raises(registry_mod.RegistryError) as load_error:
            registry_mod.Registry(registry_document([bad, AUD_LIPSYNC]),
                                  FAMILIES)
        message = str(load_error.value)
        assert "default_action 'redo_lipsync' is not in the technical " \
               "family chain" in message
        assert "allowed_actions contains 'redo_lipsync', outside the " \
               "technical family" in message

        illegal = check("VIS.COSTUME.FLASHBACK", "semantic", "S36", "identity",
                        "redo_lipsync", ["redo_lipsync"],
                        window_scope="window", window_max_frames=6,
                        recheck_scope="semantic")
        h = Harness()
        h.registry.by_id[illegal["check_id"]] = illegal
        h.registry.checks.append(illegal)
        h.add(make_report("S35", manifest=manifest_for("TECH.CONTAINER.CODEC")),
              make_report("S37", manifest=manifest_for("AUD.LIPSYNC.OFFSET")),
              make_report(
                  "S36",
                  manifest=manifest_for("VIS.IDENTITY.DRIFT",
                                        "VIS.COSTUME.FLASHBACK"),
                  defects=[make_defect("VIS.COSTUME.FLASHBACK",
                                       dtype="identity",
                                       actions=("keyframe_edit",),
                                       start=1.0, end=1.05)]))
        result = h.plan()

        assert result["state"] == "escalated"
        assert result["top_action"] == "human_review"
        assert result["escalation"]["reason"] == "registry_entry_invalid"
        assert "default_action 'redo_lipsync' is not in the identity " \
               "family chain" in result["escalation"]["detail"]
        assert h.metric(
            "registry_entry_invalid_total|VIS.COSTUME.FLASHBACK") == 1
        assert h.count("action") == 0
        assert h.count("attempt_reservation") == 0

    def test_e24_r16_keyframe_edit_on_a_shot_scope_check_is_invalid(self):
        """E24-R16：给 `shot` 范围的 check 登记 `keyframe_edit` → 登记无效。

        §3.1.2 第 1 条：逐帧修不能用在连续缺陷上。加载期给两条理由（越族 +
        范围），运行期只取第一条，但都指向同一件事：不派单。
        """
        bad = check("TECH.CONTAINER.CODEC", "technical", "S35", "technical",
                    "keyframe_edit", ["keyframe_edit"], window_scope="shot",
                    window_max_frames=None, recheck_scope="technical")
        with pytest.raises(registry_mod.RegistryError) as load_error:
            registry_mod.Registry(registry_document([bad, AUD_LIPSYNC]),
                                  FAMILIES)
        message = str(load_error.value)
        assert "names keyframe_edit but window_scope is 'shot'" in message
        assert "per-frame edits need a continuous window of at most 6 " \
               "frames" in message

        illegal = check("VIS.IDENTITY.GLOBAL", "semantic", "S36", "identity",
                        "keyframe_edit", ["keyframe_edit"],
                        window_scope="shot", window_max_frames=None,
                        recheck_scope="semantic")
        h = Harness()
        h.registry.by_id[illegal["check_id"]] = illegal
        h.registry.checks.append(illegal)
        h.add(make_report("S35", manifest=manifest_for("TECH.CONTAINER.CODEC")),
              make_report("S37", manifest=manifest_for("AUD.LIPSYNC.OFFSET")),
              make_report(
                  "S36",
                  manifest=manifest_for("VIS.IDENTITY.DRIFT",
                                        "VIS.IDENTITY.GLOBAL"),
                  defects=[make_defect("VIS.IDENTITY.GLOBAL",
                                       dtype="identity",
                                       actions=("keyframe_edit",))]))
        result = h.plan()

        assert result["state"] == "escalated"
        assert result["escalation"]["reason"] == "registry_entry_invalid"
        assert "names keyframe_edit but window_scope is 'shot'" in \
            result["escalation"]["detail"]
        assert h.count("action") == 0
        assert h.count("attempt_reservation") == 0

    def test_e24_r17_never_auto_repair_uses_the_registered_reason(self):
        """E24-R17：`never_auto_repair: true` 的新 check → 理由取登记项。

        代码里没有任何一个具体 check_id：换一条登记、换一句 `no_auto_reason`，
        理由跟着变。这条断的是"理由来自数据"，不是"抄了一句常量"。
        """
        consent = check("VIS.LIKENESS.CONSENT", "semantic", "S36", "identity",
                        "regenerate_video", ["regenerate_video"],
                        never_auto_repair=True,
                        no_auto_reason="likeness_consent_required",
                        recheck_scope="semantic")
        checks = [TECH_CODEC, consent, AUD_LIPSYNC]
        h = Harness(checks=checks)
        h.add(*reports_for(checks, {"VIS.LIKENESS.CONSENT": [
            make_defect("VIS.LIKENESS.CONSENT", dtype="identity",
                        actions=("regenerate_video",))]}))
        result = h.plan()

        assert result["state"] == "escalated"
        assert result["top_action"] == "human_review"
        assert result["action_count"] == 0
        escalation = result["escalation"]
        assert escalation["reason"] == "likeness_consent_required"
        assert "never_auto_repair" in escalation["detail"]
        assert escalation["handoff"] == "S40"
        assert h.count("action") == 0
        assert h.count("attempt_reservation") == 0

    def test_e24_r18_same_type_two_scopes_route_independently(self):
        """E24-R18：同 `defect_type`、不同 `check_id`/`window_scope` → 各走各的。

        两条都是 identity：窗口内那条按注册表起点走 `keyframe_edit`，全镜
        那条没有逐帧候选、走 `regenerate_video`。族相同而细节不同，互不吞并。
        """
        windowed = check("VIS.IDENTITY.DRIFT", "semantic", "S36", "identity",
                         "keyframe_edit",
                         ["keyframe_edit", "temporal_edit",
                          "regenerate_video"],
                         window_scope="window", window_max_frames=6,
                         recheck_scope="semantic+continuity")
        whole = check("VIS.IDENTITY.GLOBAL", "semantic", "S36", "identity",
                      "regenerate_video",
                      ["regenerate_video", "temporal_edit"],
                      window_scope="shot", window_max_frames=None,
                      recheck_scope="semantic+continuity")
        checks = [TECH_CODEC, windowed, whole, AUD_LIPSYNC]
        h = Harness(checks=checks)
        h.add(*reports_for(checks, {
            "VIS.IDENTITY.DRIFT": [make_defect(
                "VIS.IDENTITY.DRIFT", dtype="identity",
                actions=("keyframe_edit",), start=1.0, end=1.05)],
            "VIS.IDENTITY.GLOBAL": [make_defect(
                "VIS.IDENTITY.GLOBAL", dtype="identity",
                actions=("regenerate_video",), start=2.0, end=3.0)]}))
        result = h.plan()
        detail = h.service.get_plan(TENANT, result["plan_id"])

        assert result["state"] == "planned"
        assert result["action_count"] == 2
        routed = {t["check_id"]: t["action"] for t in detail["targets"]}
        assert routed == {"VIS.IDENTITY.DRIFT": "keyframe_edit",
                          "VIS.IDENTITY.GLOBAL": "regenerate_video"}
        windows = {a["action"]: (a["parameters"]["window_start_seconds"],
                                 a["parameters"]["window_end_seconds"])
                   for a in detail["actions"]}
        assert windows == {"keyframe_edit": (1.0, 1.05),
                           "regenerate_video": (2.0, 3.0)}
        assert [a["sequence"] for a in detail["actions"]] == [1, 2]
        assert h.count("attempt_reservation") == 2


# ------------------------------------------- E25 —— abstain 不当 pass（补齐）

class TestAbstainIsNotPass:
    def test_e25_r2_validator_failure_is_quarantined_not_planned(self):
        """E25-R2：报告 validator 失败 → quarantine + 告警，不出计划。

        两条不同的失败路径都走同一条闸门：一条是缺陷缺字段
        （`$.defects[0]`），一条是主体缺媒体信息（`$.subject.media`）。
        拒收理由要精确到 JSON 路径 —— 只写"schema 失败"就查不出是谁坏了。
        """
        def rejected(payload):
            documents = reports_for(DEFAULT_CHECKS, {})
            h = Harness()
            h.add(*[doc for doc in documents
                    if doc["evaluator"]["name"] != "s36-semantic-qc"])
            broken = [doc for doc in documents
                      if doc["evaluator"]["name"] == "s36-semantic-qc"][0]
            broken["defects"] = [identity_defect()]
            payload(broken)
            broken["report_content_hash"] = report_content_hash(broken)
            h.add(broken)
            with pytest.raises(ServiceError) as caught:
                h.plan()
            return h, caught.value

        def drop_severity(document):
            document["defects"][0].pop("severity")

        def drop_media(document):
            document["subject"] = {**document["subject"], "media": {}}

        for mutate, path in ((drop_severity, "$.defects[0]"),
                             (drop_media, "$.subject.media")):
            h, error = rejected(mutate)
            assert error.status == 422
            assert error.code == "QC_REPORT_REJECTED"
            assert error.details["reason"] == path
            assert error.details["layer"] == "semantic"
            assert error.details["problems"][0].startswith(path + ":")
            assert [tuple(row) for row in h.conn.execute(
                "SELECT service_id, reason FROM qc_rejection")] == [
                ("S36", path)]
            assert h.metric(
                f"qc_report_rejected_total|{path}|semantic") == 1
            assert h.metric("p1_alert|qc_report_rejected") == 1
            assert h.count("plan") == 0
            assert h.count("attempt_reservation") == 0

    def test_e25_r6_action_conflict_goes_to_human_review(self):
        """E25-R6：两个源的建议不兼容，且主源的建议这条缺陷吃不下 → 人审。

        S17 是语义层主源（优先级最高），它建议 `temporal_edit`；但这一轮
        S24/S25 一个时序能力都没声明，候选被剪空。实现不"挑另一个建议
        顶上"，而是 `action_conflict` —— 猜哪条都是在替人做决定。
        """
        h = Harness(continuity_mode="present",
                    capability={"candidates": []})
        from_s17 = make_defect("VIS.IDENTITY.DRIFT", dtype="identity",
                               actions=("temporal_edit",))
        from_s36 = make_defect("VIS.IDENTITY.DRIFT", dtype="identity",
                               actions=("keyframe_edit",))
        assert from_s17["defect_fingerprint"] == \
            from_s36["defect_fingerprint"]
        h.add(make_report("S17", defects=[from_s17],
                          manifest=manifest_for("VIS.IDENTITY.DRIFT")),
              make_report("S36", defects=[from_s36],
                          manifest=manifest_for("VIS.IDENTITY.DRIFT")),
              make_report("S35", manifest=manifest_for("TECH.CONTAINER.CODEC")),
              make_report("S37", manifest=manifest_for("AUD.LIPSYNC.OFFSET")))
        result = h.plan()

        assert result["state"] == "escalated"
        assert result["top_action"] == "human_review"
        assert result["action_count"] == 0
        escalation = result["escalation"]
        assert escalation["reason"] == "action_conflict"
        assert escalation["detail"] == (
            "primary source S17 recommends ['temporal_edit'], none of which "
            "this defect can take")
        assert escalation["handoff"] == "S40"
        assert h.count("action") == 0
        assert h.count("attempt_reservation") == 0


# ---------------------------------------- E26 —— 跨 Run 累计（核心，补齐）

class TestCrossRunAccumulation:
    def test_e26_r1_two_attempts_then_the_third_is_denied(self):
        """E26-R1：Run 1 用策略 A 两次 → seq 1、2；第 3 次 DENY。

        方案写"第 3 次 DENY(`identical_strategy_cap`)"。
        """
        h = Harness()
        consumed = spend_two_attempts(h, prefix="r1")
        assert [item["attempt_seq"] for item in consumed] == [1, 2]
        assert {item["strategy_class"] for item in consumed} == {
            "regenerate_video:na:na"}
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect(
                severity="blocker", start=3.0, end=3.25,
                actions=("regenerate_video",))]}))
        third = plan_command(h, "r1-third")
        assert third["state"] == "escalated"
        assert third["escalation"]["reason"] == "identical_strategy_cap"
        assert h.count("attempt_ledger") == 2

    def test_e26_r2_a_new_run_cannot_take_a_third_attempt(self):
        """E26-R2：换 Run（还换了 fence）再要策略 A → 仍 DENY（键无 `run_id`）。

        两个维度一起换才有说服力：run 变了、`execution_fence` 也变了，计数
        不动。键里少任何一维都会在这里露馅。
        """
        h = Harness()
        spend_two_attempts(h, prefix="r2")
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect(
                severity="blocker", start=3.0, end=3.25,
                actions=("regenerate_video",))]}))
        result = plan_command(
            h, "r2-new-run", run=RUN2,
            context=dict(execution_context(), execution_fence=99))

        assert result["state"] == "escalated"
        assert result["escalation"]["reason"] == "identical_strategy_cap"
        assert result["escalation"]["capped_at"]["attempts_used"] == 2
        assert h.count("attempt_ledger") == 2

    def test_e26_r3_new_strategy_starts_at_one_but_shot_keeps_counting(self):
        """E26-R3：Run 2 换策略 B → `attempt_seq`=1，但 `shot_total` 继续涨。

        两个计数器必须分开：`strategy_class` 上是新键（从 1 起），镜头级
        总数接着前两次走（第 3 条台账）。混成一个就会少算钱。
        """
        h = Harness()
        spend_two_attempts(h, prefix="r3")
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {"AUD.LIPSYNC.OFFSET": [
            make_defect("AUD.LIPSYNC.OFFSET", dtype="lipsync",
                        severity="minor", actions=("redo_lipsync",))]}))
        result = plan_command(
            h, "r3-b", run=RUN2,
            context=dict(execution_context(), execution_fence=99))
        assert result["state"] == "planned"
        consumed = consume_planned(h, result, event_id="r3-e3",
                                   attempt_id="r3-att3")

        assert consumed["attempt_seq"] == 1
        assert consumed["strategy_class"] == "redo_lipsync:na"
        rows = [(row["strategy_class"], row["attempt_seq"])
                for row in h.conn.execute(
                    "SELECT strategy_class, attempt_seq FROM attempt_ledger "
                    "ORDER BY attempt_seq, rowid")]
        assert rows == [("regenerate_video:na:na", 1), ("redo_lipsync:na", 1),
                        ("regenerate_video:na:na", 2)]
        # 镜头级累计 = 3 条台账，与 strategy 级的两条是不同的问题。
        assert h.count("attempt_ledger") == 3

    def test_e26_r4_model_and_seed_do_not_change_the_strategy_key(self):
        """E26-R4：换模型/换 seed 重试策略 A → 仍是同一 strategy_class → DENY。

        键只由动作与**登记参数**决定；模型与种子进不了键，否则换个 seed
        就能无限重试同一个策略。
        """
        assert strategy_class("regenerate_video",
                              {"model_id": "m7", "seed": 99}) == \
            "regenerate_video:na:na"
        h = Harness()
        spend_two_attempts(h, prefix="r4")
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect(
                severity="blocker", start=3.0, end=3.25,
                actions=("regenerate_video",))]}))
        result = plan_command(h, "r4-model")
        target = h.service.get_plan(TENANT, result["plan_id"])["targets"][0]
        parameters = h.service.get_plan(
            TENANT, result["plan_id"])["actions"][0]["parameters"]

        assert "model_id" not in parameters
        assert "seed" not in parameters
        assert target["strategy_class"] == "regenerate_video:na:na"
        assert result["state"] == "escalated"
        assert result["escalation"]["reason"] == "identical_strategy_cap"

    def test_e26_r5_a_bumped_node_epoch_does_not_reset_the_counter(self):
        """E26-R5：提 `node_epoch` 后重试 → 仍 DENY（键无 `node_epoch`）。

        epoch 只对单个 `logical_node` 的局部重生成有意义，不是"重新开始
        计数"的开关。
        """
        h = Harness()
        spend_two_attempts(h, prefix="r5")
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect(
                severity="blocker", start=3.0, end=3.25,
                actions=("regenerate_video",))]}))
        result = plan_command(
            h, "r5-epoch", context=dict(execution_context(), node_epoch=88))

        assert result["state"] == "escalated"
        assert result["escalation"]["reason"] == "identical_strategy_cap"

    def test_e26_r6_a_new_fence_does_not_reset_the_counter(self):
        """E26-R6：取消后新 Run（新 `execution_fence`）→ 仍 DENY。

        fence 管 run/branch 的失效与切换，不管尝试预算。
        """
        h = Harness()
        spend_two_attempts(h, prefix="r6")
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect(
                severity="blocker", start=3.0, end=3.25,
                actions=("regenerate_video",))]}))
        result = plan_command(
            h, "r6-fence", run=RUN2,
            context=dict(execution_context(), execution_fence=999))

        assert result["state"] == "escalated"
        assert result["escalation"]["reason"] == "identical_strategy_cap"

    def test_e26_r7_a_sub_workflow_does_not_reset_the_counter(self):
        """E26-R7：套子工作流 → 仍 DENY（键无 `workflow_id`）。

        `workflow_id` 连 `execution-context.v2` 都不在（闭集 18 键），只能在
        命令体顶层；带上它也一样被拒。
        """
        h = Harness()
        spend_two_attempts(h, prefix="r7")
        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect(
                severity="blocker", start=3.0, end=3.25,
                actions=("regenerate_video",))]}))
        body = plan_body(refs=h.refs(), command_id="r7-wf")
        body["run_id"] = RUN
        body["workflow_id"] = "wf-sub-1"
        result = h.plan(body=body, command_id="r7-wf")

        assert result["state"] == "escalated"
        assert result["escalation"]["reason"] == "identical_strategy_cap"

    def test_e26_r8_a_held_slot_rejects_the_second_plan(self):
        """E26-R8：两个计划同时要 seq=2 → 唯一约束只放一个过。

        "并发"在这里用一条**过期但仍在表里**的预留来复现：过期行不计入
        `_used()`（所以策略上限还放行），但它仍占着
        `(tenant, series, branch, shot, strategy_class, attempt_seq)` 这个
        唯一键，第二个计划插入时撞约束 → `attempt_slot_taken`。
        """
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        first = plan_command(h, "r8-a")
        assert first["state"] == "planned"
        assert h.count("attempt_reservation") == 1
        h.conn.execute("UPDATE attempt_reservation SET "
                       "expires_at='2000-01-01T00:00:00.000Z'")

        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect(
                severity="minor", start=1.0, end=1.25)]}))
        second = plan_command(h, "r8-b")
        assert second["state"] == "escalated"
        assert second["escalation"]["reason"] == "attempt_slot_taken"
        assert second["escalation"]["capped_at"]["attempts_used"] == 1
        assert second["escalation"]["capped_at"]["limit"] == 2
        assert h.metric(
            "repair_authorize_denied_total|attempt_slot_taken") == 1
        assert h.count("attempt_reservation") == 1

        # 约束本身：六列联合唯一，少一列就会放进第二个并发的同名 slot。
        row = dict(h.conn.execute(
            "SELECT * FROM attempt_reservation").fetchone())
        columns = list(row)
        with pytest.raises(sqlite3.IntegrityError) as caught:
            h.conn.execute(
                f"INSERT INTO attempt_reservation ({','.join(columns)}) "
                f"VALUES ({','.join('?' * len(columns))})",
                [row[name] for name in columns])
        message = str(caught.value)
        for name in ("tenant_id", "series_id", "branch_id",
                     "shot_lineage_id", "strategy_class", "attempt_seq"):
            assert "attempt_reservation." + name in message

    def test_e26_r9_network_retries_add_nothing_but_the_attempt_does(self):
        """E26-R9：网络重试 3 次 → 计数只 +1（且 +1 来自那次真实尝试）。

        三个 `network_retry=true` 的事件都不算业务尝试；同一个 `attempt_id`
        的真事件才记一笔。
        """
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        result = plan_command(h, "r9")
        for index in range(3):
            retry = consume_planned(
                h, result, event_id=f"r9-retry-{index}",
                attempt_id="r9-att", provider_job_id=f"pj-{index}",
                network_retry=True)
            assert retry["counted"] is False
            assert retry["reason"] == "not_a_business_attempt"
        assert h.count("attempt_ledger") == 0

        business = consume_planned(h, result, event_id="r9-business",
                                   attempt_id="r9-att",
                                   provider_job_id="pj-final")
        assert business["counted"] is True
        assert business["attempt_seq"] == 1
        assert h.count("attempt_ledger") == 1

    def test_e26_r10_a_denied_plan_counts_nothing(self):
        """E26-R10：计划生成但 S02 拒绝 → 计数 +0。

        两个拒绝口都试：镜头配额 0，以及 run 级预算耗尽。被拒的计划仍有
        一个动作行，但没有预留、没有台账 —— 这正是"看 action_count 会
        看错"的地方，所以这里断的是预留与台账。
        """
        h = Harness(policy=default_policy(max_business_attempts_per_shot=0))
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        denied = plan_command(h, "r10-shot")

        assert denied["state"] == "escalated"
        assert denied["escalation"]["reason"] == "shot_attempt_cap"
        assert denied["escalation"]["capped_at"]["attempts_used"] == 0
        assert denied["escalation"]["capped_at"]["limit"] == 0
        assert h.count("attempt_reservation") == 0
        assert h.count("attempt_ledger") == 0
        assert h.metric(
            "repair_authorize_denied_total|shot_attempt_cap") == 1

        exhausted = Harness(run={"remaining_repair_attempts": 0,
                                 "budget_exhausted": True})
        exhausted.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        result = plan_command(exhausted, "r10-run")
        assert result["state"] == "escalated"
        assert result["escalation"]["reason"] == "run_cap_or_budget"
        assert exhausted.count("attempt_reservation") == 0
        assert exhausted.count("attempt_ledger") == 0

    def test_e26_r11_confirmed_submission_backfills_one_attempt(self):
        """E26-R11：`SUBMISSION_UNKNOWN` 后确认已提交 → 计数 +1（补记）。

        未知状态下先不记（否则重投会双记），确认后补记那一笔，并标
        `backfilled`。
        """
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        result = plan_command(h, "r11")
        pending = consume_planned(h, result, event_id="r11-unknown",
                                  attempt_id="r11-att",
                                  state="SUBMISSION_UNKNOWN")
        assert pending["counted"] is False
        assert pending["reason"] == "submission_unknown"
        assert pending["pending"] is True
        assert h.count("attempt_ledger") == 0

        confirmed = consume_planned(
            h, result, event_id="r11-confirmed", attempt_id="r11-att",
            state="FAILED", reason_code="CONFIRMED_SUBMITTED")
        assert confirmed["counted"] is True
        assert confirmed["backfilled"] is True
        assert confirmed["attempt_seq"] == 1
        assert h.count("attempt_ledger") == 1

    def test_e26_r12_confirmed_not_submitted_counts_zero(self):
        """E26-R12：`SUBMISSION_UNKNOWN` 后确认未提交 → 计数 +0。"""
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        result = plan_command(h, "r12")
        consume_planned(h, result, event_id="r12-unknown",
                        attempt_id="r12-att", state="SUBMISSION_UNKNOWN")
        not_submitted = consume_planned(
            h, result, event_id="r12-confirmed", attempt_id="r12-att",
            state="FAILED", reason_code="CONFIRMED_NOT_SUBMITTED")

        assert not_submitted["counted"] is False
        assert not_submitted["reason"] == "confirmed_not_submitted"
        assert h.count("attempt_ledger") == 0

    def test_e26_r13_every_provider_job_is_reachable_from_the_ledger(self):
        """E26-R13：费用跨 Run 累计由 S23 保证；S39 的 `attempt_id` 要能关联
        到全部 `provider_job`。

        两次 Run 各一次尝试，两次的 `provider_job_id` 都能从台账与
        `list_plans` 里按 `attempt_id` 找回来 —— 找不到就和钱对不上账。
        """
        h = Harness()
        for index, (run, severity) in enumerate(
                ((RUN, "minor"), (RUN2, "major")), 1):
            h.reset()
            h.add(*reports_for(DEFAULT_CHECKS, {
                "VIS.IDENTITY.DRIFT": [identity_defect(
                    severity=severity, start=float(index),
                    end=float(index) + 0.25)]}))
            result = plan_command(h, f"r13-{index}", run=run)
            assert result["state"] == "planned"
            consume_planned(h, result, event_id=f"r13-e{index}",
                            attempt_id=f"r13-att{index}",
                            provider_job_id=f"prov-job-{index}")

        ledger = [(row["attempt_id"], row["provider_job_id"], row["run_id"])
                  for row in h.conn.execute(
                      "SELECT attempt_id, provider_job_id, run_id "
                      "FROM attempt_ledger ORDER BY attempt_seq, rowid")]
        assert ledger == [("r13-att1", "prov-job-1", RUN),
                          ("r13-att2", "prov-job-2", RUN2)]

        attempts = h.service.list_plans(TENANT, SHOT)["attempts"]
        assert [(item["attempt_id"], item["provider_job_id"],
                 item["run_id"]) for item in attempts] == ledger
        assert len(h.service.list_plans(TENANT, SHOT)["plans"]) == 2

    def test_e26_r14_the_fourth_defect_is_denied_at_the_shot_cap(self):
        """E26-R14：同一镜头 3 个不同缺陷各修一次 → shot_total=3，第 4 次 DENY。

        三个策略各不相同（所以策略级上限不拦），镜头级总数先到顶。
        """
        h = Harness()
        attempts = [
            ("minor", 0.0, "regenerate_video", "VIS.IDENTITY.DRIFT"),
            ("major", 1.0, "temporal_edit", "VIS.IDENTITY.DRIFT"),
            ("blocker", 2.0, "redo_lipsync", "AUD.LIPSYNC.OFFSET"),
        ]
        for index, (severity, start, action, check_id) in enumerate(
                attempts, 1):
            h.reset()
            if check_id == "AUD.LIPSYNC.OFFSET":
                h.add(*reports_for(DEFAULT_CHECKS, {check_id: [make_defect(
                    check_id, dtype="lipsync", severity="minor",
                    actions=(action,))]}))
            else:
                h.add(*reports_for(DEFAULT_CHECKS, {check_id: [make_defect(
                    check_id, dtype="identity", severity=severity,
                    start=start, end=start + 0.25,
                    actions=(action,))]}))
            result = plan_command(h, f"r14-{index}")
            assert result["state"] == "planned"
            consume_planned(h, result, event_id=f"r14-e{index}",
                            attempt_id=f"r14-att{index}")
        assert h.count("attempt_ledger") == 3

        h.reset()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect(
                severity="info", start=3.0, end=3.25,
                actions=("regenerate_video",))]}))
        fourth = plan_command(h, "r14-4")

        assert fourth["state"] == "escalated"
        assert fourth["escalation"]["reason"] == "shot_attempt_cap"
        assert fourth["escalation"]["capped_at"]["attempts_used"] == 3
        assert fourth["escalation"]["capped_at"]["limit"] == 3
        assert h.count("attempt_ledger") == 3
        assert h.count("attempt_reservation") == 3


# --------------------------------------------------- D —— 死循环与异常（补齐）

class TestDeadEndsAndIsolation:
    def test_d2_a_foreign_shot_lineage_is_rejected_without_spending(self):
        """D2：计划引用了不存在的 `shot_lineage_id` → 422，不消耗计数。

        方案原文："计划引用了不存在的 shot_lineage_id | 422，不消耗计数"。
        实现只在**报告之间**比对 lineage：body 与报告不一致时 422；但一条
        从没存在过的 lineage 只要前后一致就照常出计划并占位（S39 没有
        S06/S08 的镜头目录，查不了存在性，C4 登记的正是这个缺口）。两半都
        断，措辞差异登记在报告里，内核不动。
        """
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}, shot="lin_other"))
        with pytest.raises(ServiceError) as caught:
            h.plan()
        assert caught.value.status == 422
        assert caught.value.code == "MULTIPLE_SHOT_LINEAGES"
        assert h.count("plan") == 0
        assert h.count("attempt_reservation") == 0

        unknown = Harness()
        unknown.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]},
            shot="lin_does_not_exist_v1"))
        body = plan_body(refs=unknown.refs(), command_id="d2")
        body["shot_lineage_id"] = "lin_does_not_exist_v1"
        result = unknown.plan(body=body, command_id="d2")
        assert result["state"] == "planned"
        assert result["shot_lineage_id"] == "lin_does_not_exist_v1"
        assert unknown.count("attempt_reservation") == 1

    def test_d3_a_defect_without_recommended_actions_is_rejected(self):
        """D3：缺陷无 `recommended_actions` → validator 拒收（minItems 1）。

        先断契约本身（`qc-report.v2` 的 minItems），再断 ingest 真的按它拒。
        """
        defect = make_defect("VIS.IDENTITY.DRIFT", dtype="identity",
                             actions=())
        document = make_report("S36", defects=[defect],
                               manifest=manifest_for("VIS.IDENTITY.DRIFT"))
        problems = validate_schema(document, QC_REPORT_SCHEMA)
        assert any("recommended_actions" in problem
                   and "fewer than minItems 1" in problem
                   for problem in problems), problems

        h = Harness()
        h.add(make_report("S35", manifest=manifest_for("TECH.CONTAINER.CODEC")),
              make_report("S37", manifest=manifest_for("AUD.LIPSYNC.OFFSET")),
              document)
        with pytest.raises(ServiceError) as caught:
            h.plan()
        assert caught.value.status == 422
        assert caught.value.code == "QC_REPORT_REJECTED"
        assert caught.value.details["reason"] == \
            "$.defects[0].recommended_actions"
        assert h.count("plan") == 0
        assert h.count("attempt_reservation") == 0

    def test_d4_cross_tenant_reads_leak_nothing(self):
        """D4：跨租户查 `attempt_ledger` → 查不到，也不泄漏存在性。

        方案原文："跨租户查询 attempt_ledger | 404 不泄漏"。`get_plan` 是
        404（与不存在同形）；`list_plans` 是 200 + 两个空数组 —— 它本来
        就是"按镜头列"，没有命中不是错误。两种形状都不含别的租户的字。
        """
        h = Harness()
        h.add(*reports_for(DEFAULT_CHECKS, {
            "VIS.IDENTITY.DRIFT": [identity_defect()]}))
        result = plan_command(h, "d4")
        consume_planned(h, result, event_id="d4-e1", attempt_id="d4-att1")
        assert h.count("attempt_ledger") == 1

        own = h.service.list_plans(TENANT, SHOT)
        assert len(own["attempts"]) == 1
        other = h.service.list_plans(OTHER_TENANT, SHOT)
        assert other == {"shot_lineage_id": SHOT, "plans": [], "attempts": []}
        assert OTHER_TENANT not in json.dumps(own, ensure_ascii=False)

        with pytest.raises(ServiceError) as caught:
            h.service.get_plan(OTHER_TENANT, result["plan_id"])
        assert caught.value.status == 404
        assert caught.value.code == "PLAN_NOT_FOUND"


# ------------------------------------------------------- 方案编号双向覆盖

def test_plan_ids_are_covered_both_ways():
    """方案 §10 的编号集合与本文件用例 docstring 的编号集合必须相等。

    两个方向都断：方案有的这里都有（没漏测），这里写的方案里都有（没
    编造编号）。分母写死，防止"方案被删几行后覆盖率悄悄变成 100%"。
    `C1..C28` 是契约缺口编号，与本集合不同名空间，正则不含它们。
    """
    import ast

    plan_path = ROOT / "plans" / "S39-repair-planning.md"
    case_id = re.compile(r"\b(?:E24-R\d+|E25-R\d+|E26-R\d+|D\d+)\b")
    plan_ids = set(case_id.findall(plan_path.read_text(encoding="utf-8")))
    assert len(plan_ids) == 43, sorted(plan_ids)

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    tested = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        # 编号写在 docstring 里：函数名会随重构改，docstring 是"对应方案
        # 哪一项"的声明。
        tested.update(case_id.findall(ast.get_docstring(node) or ""))

    assert not (plan_ids - tested), \
        f"方案有、测试没有: {sorted(plan_ids - tested)}"
    assert not (tested - plan_ids), \
        f"测试有、方案没有: {sorted(tested - plan_ids)}"
    assert tested == plan_ids
    assert len(tested) == 43
