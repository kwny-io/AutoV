"""S36 单测。编号对应 `plans/S36-semantic-qc.md` §10：E24-S1…S9、E25-S1…S6、
B1…B6、F1…F4。

上游全部是 stub，**按真实的上游契约写**：

- S26 `POST /generation-jobs` 返回 `{provider_job_id, operation_id, attempt_id}`，
  `GET /generation-jobs/{id}` 返回 `{state, output:{result:…}, cost_micro_units}`。
  测试里也**不**为了让内核好看而多给字段。
- S28 的 revision 读只给容器/时长/宽高/帧率——没有 codec/pix_fmt（SQC-12）。
- S17 的可达性与"有没有给 ref"是两件事：给了 ref 但 S17 掉线，依赖它的
  check 必须 `dependency_unavailable`（E25-S5）。

整个文件里没有网络、没有供应商。`F1` 那条断言就是「depts 里没有直连」的
静态证明：模块里不存在第二个 HTTP 基址。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
    canonical_request_hash,
)
from jsonschema import Draft202012Validator

from semantic_qc import registry
from semantic_qc.schema import CONTRACT_GAPS, SCHEMA
from semantic_qc.service import (
    SemanticQcService,
    ServiceError,
    _public,
    base32_lower,
    defect_fingerprint,
    derived_key,
    _promote,
    _jaccard,
)


REPO = Path(__file__).resolve().parents[3]
H = "a" * 64
POLICY_REF = {"service_id": "S02", "id": "pol_1", "revision": 3,
              "content_hash": "b" * 64}
FUTURE = "2099-01-01T00:00:00Z"

qc_report_schema = json.loads(
    (REPO / "contracts/quality/qc-report.v2.schema.json").read_text("utf-8"))
qc_event_schema = json.loads(
    (REPO / "contracts/quality.semantic-completed.v2.schema.json").read_text("utf-8"))
action_families = json.loads(
    (REPO / "contracts/quality/action-families.v2.json").read_text("utf-8"))
registry_schema = json.loads(
    (REPO / "contracts/quality/check-registry.v2.schema.json").read_text("utf-8"))


# ------------------------------------------------------------------ 上游形状

def artifact_record(**over):
    """S28 `GET /artifacts/{id}/revisions/{rev}` 的真实形状。

    **没有** `video_codec` / `pix_fmt` / `color_range` / `is_vfr` / `frame_count`
    ——那是 SQC-12 记下的缺口。测试不替上游补齐。
    """
    base = {"artifact_id": "clip_1", "revision": 1, "content_hash": H,
            "duration_ms": 3000, "width_px": 1920, "height_px": 1080,
            "frame_rate": {"num": 24, "den": 1}, "artifact_kind": "video",
            "audio_channels": 2}
    base.update(over)
    return base


def vlm_pass(confidence=0.9):
    return {"verdict": "pass", "severity": "info", "confidence": confidence}


def vlm_defect(check_severity="blocker", severity="blocker", observed="换人了",
               start=2.0, end=2.6, character_id="char_1", confidence=0.92,
               **over):
    out = {"verdict": "defect", "severity": severity, "confidence": confidence,
           "observed": observed, "expected": "同一角色",
           "character_id": character_id,
           "time_range": {"start_seconds": start, "end_seconds": end}}
    out.update(over)
    return out


def vlm_not_applicable(reason="no_face"):
    return {"verdict": "not_applicable", "severity": "info",
            "confidence": 0.5, "reason_code": reason}


class StubSource:
    """可调用的上游 stub，带 `configured` 标志。

    `api/app.py` 的 readiness 用 `getattr(source, "configured", False)` 判断
    上游是否就绪——而绑定方法不允许挂属性。这里包一层可调用对象，让 stub
    也能回答"我配好了"。真依赖（`deps.py`）里的未配置闭包给的是 False。
    """

    def __init__(self, fn):
        self._fn = fn
        self.configured = True

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class Harness:
    """内存库 + 幂等外壳，形状与 `api/app.py` 的 `command()` 一致。"""

    def __init__(self, conn=None, *, script=None, artifact=None,
                 expected=None, submit_error=None, states=None,
                 detector=None, submit_returns=None):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.idem = SqliteIdempotencyStore(self.conn)

        self.calls = {"submit": 0, "job": 0, "artifact": 0, "expected": 0}
        self.submits = []
        # `script` 是每次 GET job 的返回序列；用完之后一直重复最后一项。
        # 按 (state, result) 排队比按调用序号硬编码更能表达「第三次超时」。
        self.script = list(script or [("PROVIDER_SUCCEEDED", vlm_pass())])
        self.artifact = artifact if artifact is not None else artifact_record()
        # None 表示「命令没给 ref」；Exception 表示「给了 ref 但 S17 掉了」。
        self.expected = expected
        self.submit_error = submit_error
        self.submit_returns = submit_returns or {
            "provider_job_id": "pj_1", "operation_id": "op_1",
            "attempt_id": "at_1"}

        self.service = SemanticQcService(
            self.conn,
            artifact_source=StubSource(self._artifact),
            expected_state_source=StubSource(self._expected),
            submit_source=StubSource(self._submit),
            job_source=StubSource(self._job),
            detector_source=detector,
        )

    # ---- stubs ----
    def _artifact(self, tenant_id, ref):
        self.calls["artifact"] += 1
        if isinstance(self.artifact, Exception):
            raise self.artifact
        return self.artifact

    def _expected(self, tenant_id, ref):
        self.calls["expected"] += 1
        if isinstance(self.expected, Exception):
            raise self.expected
        return self.expected if self.expected is not None else {}

    def _submit(self, tenant_id, command, route_decision_id):
        self.calls["submit"] += 1
        self.submits.append(command)
        if isinstance(self.submit_error, Exception):
            raise self.submit_error
        return dict(self.submit_returns)

    def _job(self, tenant_id, job_id):
        self.calls["job"] += 1
        if not self.script:
            return {"state": "PROVIDER_SUCCEEDED",
                    "output": {"result": vlm_pass()}}
        item = self.script[0]
        if len(self.script) > 1:
            self.script.pop(0)
        state, result = item
        body = {"state": state}
        if result is not None:
            body["output"] = {"result": result}
        return body

    # ---- idempotency shell ----
    def command(self, body, tenant_id="t1", command_id="c1"):
        payload = {**body, "command_id": command_id}
        request_hash = canonical_request_hash(payload)
        record = self.idem.begin(tenant_id, command_id, request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = self.service.create_check(tenant_id, command_id, payload,
                                               request_hash)
        except Exception:
            self.idem.abort(tenant_id, command_id)
            raise
        self.idem.finish(tenant_id, command_id,
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ---- body ----
    def body(self, *, include=None, exclude=None, **over):
        body = {
            "schema_version": "2.0",
            "execution_context": {
                "schema_version": "2.0", "tenant_id": "t1",
                "project_id": "prj_1", "series_id": "ser_1",
                "branch_id": "br_1", "episode_id": "ep_1",
                "home_region": "cn", "home_cell": "cell_1",
                "manifest_ref": {"id": "man_1", "revision": 2,
                                 "content_hash": H},
                "node_id": "node_1", "node_epoch": 4, "execution_fence": 2,
                # `execution-context.v2` 把 `memory_checkpoint` 列进 `required`
                # 且类型是 object（`{branch_id, commit_sequence, content_hash}`）
                # ——不是 nullable。这里填真值，因为这份 context 会被原样
                # 塞进 S26 的 generation-command，而 S26 逐键检查非空。
                "memory_checkpoint": {"branch_id": "br_1", "commit_sequence": 7,
                                      "content_hash": H},
                "policy_ref": dict(POLICY_REF),
                "actor": {"principal_id": "u1", "principal_type": "service",
                          "delegation_id": "d1"},
                "command_id": "c1", "request_hash": "c" * 64,
                "trace_id": "trace_1",
            },
            "artifact": {"service_id": "S28", "id": "clip_1", "revision": 1,
                         "content_hash": H},
            "shot_spec_ref": {"service_id": "S06", "id": "shot_3_2",
                              "revision": 4, "content_hash": H},
            "binding_set_ref": {"service_id": "S10", "id": "bs_1",
                                "revision": 3, "content_hash": H},
            "shot_lineage_id": "lin_1",
            "run_id": "run_1",
            "check_selection": {"include": include or ["VIS.IDENTITY.*"],
                                "exclude": exclude or []},
            "evaluator_profile": "dual_high_risk",
            "evaluator_route_decision_id": "rd_1",
            "repair_context": None,
            "deadline": FUTURE,
        }
        body.update(over)
        return body


def run(h, body=None, tenant="t1", command="c1"):
    return h.command(body or h.body(), tenant_id=tenant, command_id=command)


def report_of(h, result, tenant="t1"):
    return h.service.get_report(tenant, result["report_id"])["report"]


def errors_of(schema, instance):
    return [f"{'/'.join(str(x) for x in e.absolute_path) or '<root>'}: {e.message}"
            for e in Draft202012Validator(schema).iter_errors(instance)]


def errors_of_with_registry(schema, instance):
    """跨文件 `$ref` 的离线校验。

    `generation-command.v2` 引了 `execution-context.v2`，后者的 `$id` 是
    `https://drama.example.invalid/…`；默认校验器会把它当远程资源去取，
    单测就变成了一次网络请求。把 `contracts/*.schema.json` 按 `$id` 注册
    进 registry，`$ref` 才会在本地解析——这也是「测试里没有网络」的一部分。
    """
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    reg = Registry()
    for path in (REPO / "contracts").glob("*.schema.json"):
        doc = json.loads(path.read_text("utf-8"))
        if "$id" in doc:
            reg = reg.with_resource(
                doc["$id"],
                Resource.from_contents(doc, default_specification=DRAFT202012))
    validator = Draft202012Validator(schema, registry=reg)
    return [f"{'/'.join(str(x) for x in e.absolute_path) or '<root>'}: {e.message}"
            for e in validator.iter_errors(instance)]


def all_calls(h, report_id, tenant="t1"):
    return h.service.get_report(tenant, report_id,
                                include="evaluator_calls")["evaluator_calls"]


# ================================================================ 注册表

class TestRegistry:
    """§10.3 B6 + §5：注册表是数据，必须真的能被契约校验器加载。"""

    def test_registry_loads_under_the_reference_validator(self, tmp_path):
        # 直接跑仓库里的校验器（不是复刻它的规则）：schema + 动作族两条都过。
        path = tmp_path / "registry.json"
        path.write_text(json.dumps(registry.registry_document(),
                                   ensure_ascii=False), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            "qc_validate_registry",
            REPO / "contracts/quality/validate_registry.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.validate(registry.registry_document(),
                               action_families) == []
        assert errors_of(registry_schema, registry.registry_document()) == []

    def test_every_check_has_an_owner_and_no_s16_expected_source(self):
        # B6：不留空；B5 的反面：S36 不得消费 S16。
        for check in registry.CHECKS:
            assert check["owner_service"] == "S36"
            assert check["_expected_source"] in ("S06", "S10", "S17")
        assert "VIS.KNOWLEDGE.SCOPE_RENDER" in registry.NOT_OURS
        assert "VIS.KNOWLEDGE.SCOPE_RENDER" not in registry.CHECK_IDS

    def test_blast_id_axis_name_is_the_registry_one(self):
        # SQC-21：§2.3 写 VIS.AXIS.*，§5.5 写 VIS.CONTINUITY.*。取后者。
        assert "VIS.CONTINUITY.AXIS_CROSSING" in registry.CHECK_IDS
        assert "VIS.AXIS.SHOT_CROSSING" not in registry.CHECK_IDS

    def test_identity_family_is_high_risk_not_just_blockers(self):
        # §6：高风险 = blocker ∪ VIS.IDENTITY.*。少了这一条，体型/服装这类
        # major 项会静默退化成单评估器。
        assert "VIS.IDENTITY.BODY_PROPORTION" in registry.HIGH_RISK
        assert "VIS.IDENTITY.ACCESSORY_EXTRA" in registry.HIGH_RISK
        assert "VIS.TEMPORAL.FLICKER" not in registry.HIGH_RISK

    def test_unknown_glob_is_rejected_not_ignored(self):
        with pytest.raises(ValueError):
            registry.select(["VIS.IDENTITY.NOPE.*"], [])

    def test_window_plan_covers_duration_without_gaps(self):
        windows = registry.window_plan("dialogue", 3.0)
        assert [w["window_index"] for w in windows] == [0, 1, 2]
        assert windows[0]["start_seconds"] == 0.0
        assert windows[-1]["end_seconds"] == 3.0


# ================================================================ E24

class TestE24:
    def test_s1_face_swap_reports_time_range_and_bbox(self):
        # E24-S1：2.0–2.6s 换脸 → blocker，time_range 精确到该段。
        # 3s 素材 = 3 个 1s 窗口，FACE_SWAP 是 window 范围且属高风险，每窗口
        # 主/副各一次 → 前两窗各两个 pass，第 3 窗两个 defect，共 6 次 job。
        passes = [("PROVIDER_SUCCEEDED", vlm_pass())] * 4
        hit = vlm_defect(
            observed="人脸换成另一角色",
            region_bbox={"x": 0.3, "y": 0.2, "width": 0.2, "height": 0.2})
        h = Harness(script=passes + [("PROVIDER_SUCCEEDED", hit)] * 2,
                    expected={"expected_state": []})
        h.artifact = artifact_record(duration_ms=3000)
        got = run(h, h.body(include=["VIS.IDENTITY.FACE_SWAP_WITHIN_SHOT"]))
        report = report_of(h, got)

        assert got["decision"] == "repair"
        [defect] = report["defects"]
        assert defect["check_id"] == "VIS.IDENTITY.FACE_SWAP_WITHIN_SHOT"
        assert defect["severity"] == "blocker"
        assert defect["time_range"] == {"start_seconds": 2.0,
                                        "end_seconds": 2.6}
        assert defect["region_bbox"]["width"] == 0.2
        assert defect["detector"] == "model"
        assert errors_of(qc_report_schema, report) == []

    def test_s2_outfit_defect_cites_s10_binding(self):
        # E24-S2：expected_source 指向 S10 binding revision。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect(
            severity="major", observed="服装与 binding 不符"))],
            expected={"expected_state": []})
        got = run(h, h.body(include=["VIS.IDENTITY.OUTFIT_REVISION"]))
        [defect] = report_of(h, got)["defects"]
        assert defect["expected_source"]["service_id"] == "S10"
        assert got["decision"] == "repair"

    def test_s3_hand_closeup_is_a_blocker(self):
        # E24-S3：手部特写六指 → blocker → repair。
        # 1s 素材 = 1 个窗口；FINGER_DEFORM 是 blocker（高风险）→ 双评估，
        # 两边同指纹合并成一条。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect(
            observed="六指", character_id=None))],
            expected={"expected_state": []})
        h.artifact = artifact_record(duration_ms=1000)
        got = run(h, h.body(include=["VIS.HANDS.FINGER_DEFORM"]))
        [defect] = report_of(h, got)["defects"]
        assert defect["severity"] == "blocker"
        assert got["decision"] == "repair"

    def test_s4_hand_too_small_is_not_applicable_and_does_not_block(self):
        # E24-S4：手部占 0.5% → not_applicable，decision 不受影响。
        def detector(tenant, ref, windows):
            return {"available": True,
                    "windows": [{"window_index": w["window_index"],
                                 "hand_area_ratio": 0.005} for w in windows]}

        h = Harness(detector=detector, script=[("PROVIDER_SUCCEEDED", vlm_pass())],
                    expected={"expected_state": []})
        got = run(h, h.body(include=["VIS.HANDS.FINGER_DEFORM"]))
        report = report_of(h, got)

        assert report["check_manifest"][0]["status"] == "not_applicable"
        assert report["check_manifest"][0]["reason_code"] == "no_such_content"
        assert got["decision"] == "pass"
        # 门控生效时不该付钱给模型。
        assert h.calls["submit"] == 0

    def test_s5_declared_axis_crossing_is_not_a_defect(self):
        # E24-S5：ShotSpec 声明越轴 → 不适用，而不是 blocker。
        h = Harness(expected={"expected_state": []})
        body = h.body(include=["VIS.CONTINUITY.AXIS_CROSSING"],
                      shot_spec={"shot_type": "dialogue",
                                 "declared_axis_crossing": True})
        report = report_of(h, run(h, body))
        assert report["check_manifest"][0]["status"] == "not_applicable"
        assert report["decision"] == "pass"
        assert h.calls["submit"] == 0

    def test_s5_undeclared_axis_crossing_is_a_blocker(self):
        # E24-S5 的另一半：没有声明就是缺陷。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect(
            observed="对白场越轴", character_id=None))],
            expected={"expected_state": []})
        body = h.body(include=["VIS.CONTINUITY.AXIS_CROSSING"],
                      shot_spec={"shot_type": "dialogue"})
        assert run(h, body)["decision"] == "repair"

    def test_s6_sixty_percent_coverage_abstains(self):
        # E24-S6：只完成 60% 窗口 → coverage 0.60 → abstain（不是 pass）。
        # 3 个窗口里第 2 个开始就一直返回失败终态。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_pass()),
                            ("PROVIDER_FAILED", None),
                            ("PROVIDER_FAILED", None)],
                    expected={"expected_state": []})
        body = h.body(include=["VIS.CAMERA.MOTION"])  # window 范围，1.0s × 3
        got = run(h, body)
        report = report_of(h, got)

        assert report["coverage"]["coverage_ratio"] < registry.MIN_COVERAGE_RATIO
        assert got["decision"] == "abstain"
        assert report["abstain"]["reason_code"] == "evaluator_error"

    def test_s7_fingerprint_is_stable_across_reruns(self):
        # E24-S7：同一缺陷复检必须得到同一 fingerprint（与 artifact hash 无关）。
        one = defect_fingerprint("VIS.SPACE.PROP_OWNERSHIP", "prop", "c1",
                                 "major", 1.0)
        two = defect_fingerprint("VIS.SPACE.PROP_OWNERSHIP", "prop", "c1",
                                 "minor", 1.2)
        three = defect_fingerprint("VIS.SPACE.PROP_OWNERSHIP", "prop", "c1",
                                   "blocker", 1.0)
        assert one == two          # 同桶内、同非 blocker 档
        assert one != three        # blocker 与非 blocker 必须分裂

    def test_s8_recheck_after_repair_points_at_the_slot(self):
        # E24-S8：复检传 repair_context → 新 report，supersedes/repair_context
        # 留痕，且 attempt_slot_id 进入派生键（换 slot 就是另一份报告）。
        h = Harness(expected={"expected_state": []})
        repair = {"repair_plan_id": "rp_1", "attempt_slot_id": "slot_2",
                  "attempt_seq": 2,
                  "defect_fingerprints": ["0" * 32]}
        got = run(h, h.body(include=["VIS.IDENTITY.ACCESSORY_EXTRA"],
                            repair_context=repair))
        report = report_of(h, got)
        assert report["repair_context"]["attempt_slot_id"] == "slot_2"
        assert report["decision"] == "pass"

        plain = Harness(expected={"expected_state": []})
        first = report_of(plain, run(plain, plain.body(
            include=["VIS.IDENTITY.ACCESSORY_EXTRA"])))
        assert first["report_id"] != report["report_id"]

    def test_s9_budget_exhausted_is_failed_not_skipped(self):
        # E24-S9 / F2：S26 明确拒绝（预算） → failed → abstain，**不得降级跳过**。
        h = Harness(submit_error=ServiceError(402, "BUDGET_EXHAUSTED",
                                             "no budget"),
                    expected={"expected_state": []})
        got = run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        report = report_of(h, got)
        assert got["decision"] == "abstain"
        assert report["check_manifest"][0]["status"] == "failed"
        assert report["abstain"]["reason_code"] == "evaluator_error"


# ================================================================ E25

class TestE25:
    def test_s1_primary_timeout_abstains_with_a_record(self):
        # E25-S1：主评估器超时 → abstain(evaluator_timeout)，outcome 留痕。
        # 3 个窗口各自提交一次并超时；这里关键是**每一次**都是 timeout 而不是
        # 「S26 不可用」，以及理由码不是被覆盖不足吞掉的 incomplete_coverage。
        h = Harness(submit_error=ServiceError(504, "UPSTREAM_TIMEOUT", "slow"),
                    expected={"expected_state": []})
        got = run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        report = report_of(h, got)

        assert got["decision"] == "abstain"
        assert report["abstain"]["reason_code"] == "evaluator_timeout"
        calls = all_calls(h, got["report_id"])
        assert [c["outcome"] for c in calls] == ["timeout"] * 3

    def test_s2_secondary_timeout_does_not_use_the_single_opinion(self):
        # E25-S2：主成功、副超时 → 不能用单边结果，即使单边说 pass。
        # 第一次 submit 正常、第二次 504；主结果本身是 pass。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_pass())],
                    expected={"expected_state": []})
        real_submit = h._submit
        count = {"n": 0}

        def flaky(tenant_id, command, route_decision_id):
            count["n"] += 1
            if count["n"] == 2:
                h.calls["submit"] += 1
                raise ServiceError(504, "UPSTREAM_TIMEOUT", "secondary slow")
            return real_submit(tenant_id, command, route_decision_id)

        h.service.submit_source = flaky
        got = run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        report = report_of(h, got)

        assert got["decision"] == "abstain"
        assert report["check_manifest"][0]["status"] == "failed"
        assert "secondary" in (report["check_manifest"][0]["check_id"] or "") or \
            report["abstain"]["reason_code"] == "evaluator_timeout"

    def test_s3_disagreement_goes_to_human_review(self):
        # E25-S3：两侧结论集合完全不相交 → Jaccard 0 → 距离 1.0 > 0.30。
        #
        # 选 ACCESSORY_EXTRA 有两个理由：它是 shot 范围（1 次窗口 = 1 对调用，
        # 不用脚本序列对齐 3 个窗口），且登记为 minor（默认档 minor、上限
        # major）——既不能是 blocker（第 4 步先 repair）也不能是 major
        # （第 5 步先 repair），否则根本走不到第 6 步的分歧判定。
        # 两侧都报 `minor`：`vlm_defect` 默认 severity 是 blocker，而
        # ACCESSORY_EXTRA 的上限是 major——blocker 会被封顶成 major，然后
        # 第 5 步就 `repair` 了，根本走不到第 6 步。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect(
            severity="minor", observed="左边多了一枚徽章", start=0.0, end=0.4)),
            ("PROVIDER_SUCCEEDED", vlm_defect(
                severity="minor", observed="右边腰间多了挂饰", start=0.5,
                end=0.9, character_id="char_9"))],
            expected={"expected_state": []})
        h.artifact = artifact_record(duration_ms=1000)
        got = run(h, h.body(include=["VIS.IDENTITY.ACCESSORY_EXTRA"]))
        report = report_of(h, got)

        assert got["decision"] == "human_review"
        # 两端结论都保留（两边指纹不同 → 两条缺陷）。
        assert len(report["defects"]) == 2
        assert {d["severity"] for d in report["defects"]} == {"minor"}
        names = [d["name"] for d in report["dimensions"]]
        assert "model_disagreement.VIS.IDENTITY.ACCESSORY_EXTRA" in names

    def test_s4_schema_mismatch_is_schema_error_then_abstain(self):
        # E25-S4：模型多给一个字段 → schema_error → failed → abstain。
        # 1s 素材 → 1 个窗口；被接受的 `verdict` 是 pass 也不影响——多出来的
        # 字段让整份响应当不了结论，不是「忽略多余字段」。
        h = Harness(script=[("PROVIDER_SUCCEEDED",
                             {"verdict": "pass", "severity": "info",
                              "confidence": 0.9, "prose": "我觉得没问题"})],
                    expected={"expected_state": []})
        h.artifact = artifact_record(duration_ms=1000)
        got = run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        report = report_of(h, got)

        assert got["decision"] == "abstain"
        assert report["check_manifest"][0]["status"] == "failed"
        assert report["abstain"]["reason_code"] == "evaluator_error"
        calls = all_calls(h, got["report_id"])
        assert [c["outcome"] for c in calls] == ["schema_error"]

    def test_s5_s17_unreachable_is_dependency_unavailable(self):
        # E25-S5：给了 expected_state_ref 但 S17 掉线 → 不是通过，是弃权。
        h = Harness(expected=ConnectionError("S17 down"))
        body = h.body(include=["VIS.SPACE.PROP_STATE"],
                      expected_state_ref={"service_id": "S17", "id": "cont_1",
                                          "revision": 7, "content_hash": H})
        got = run(h, body)
        report = report_of(h, got)

        assert got["decision"] == "abstain"
        assert report["check_manifest"][0]["reason_code"] == \
            "dependency_unavailable"
        assert report["abstain"]["reason_code"] == "dependency_stale"
        assert h.calls["submit"] == 0

    def test_s5_missing_expected_ref_is_also_dependency_unavailable(self):
        h = Harness(expected={"expected_state": []})
        got = run(h, h.body(include=["VIS.STATE.EXPECTED_VISIBLE"]))
        assert report_of(h, got)["check_manifest"][0]["reason_code"] == \
            "dependency_unavailable"

    def test_s6_exception_without_declared_by_is_discarded(self):
        # E25-S6：S17 给例外但没有 declared_by → 例外失效，按普通检查判。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect(
            severity="major", observed="服装不符"))],
            expected={"expected_state": []})
        body = h.body(include=["VIS.IDENTITY.OUTFIT_REVISION"], legal_exceptions=[{
            "exception_id": "exc_1", "kind": "flashback",
            "applies_to": {"time_ranges": [{"start": 0, "end": 3}]},
            "suppresses_check_ids": ["VIS.IDENTITY.OUTFIT_REVISION"],
        }])
        got = run(h, body)
        report = report_of(h, got)
        assert got["decision"] == "repair"
        assert len(report["defects"]) == 1

    def test_s6_declared_exception_suppresses_but_keeps_evidence(self):
        # B3：带 declared_by=S17 的例外 → not_applicable，dimension 保留。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect(
            severity="major", observed="时代错的服装"))],
            expected={"expected_state": []})
        body = h.body(include=["VIS.IDENTITY.OUTFIT_REVISION"], legal_exceptions=[{
            "exception_id": "exc_flashback", "kind": "flashback",
            "applies_to": {"time_ranges": [{"start": 0, "end": 3}]},
            "suppresses_check_ids": ["VIS.IDENTITY.OUTFIT_REVISION"],
            "declared_by": {"service_id": "S17", "ref_id": "cont_1"},
        }])
        report = report_of(h, run(h, body))

        assert report["check_manifest"][0]["status"] == "not_applicable"
        assert report["decision"] == "pass"
        assert report["defects"] == []
        names = [d["name"] for d in report["dimensions"]]
        assert "suppressed_observation.VIS.IDENTITY.OUTFIT_REVISION" in names

    def test_s6_exception_from_another_service_is_discarded(self):
        # 只有 S17 能声明合法例外。S16 说"这是历史事实"不算。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect(
            severity="major", observed="服装不符"))],
            expected={"expected_state": []})
        body = h.body(include=["VIS.IDENTITY.OUTFIT_REVISION"], legal_exceptions=[{
            "exception_id": "exc_x", "kind": "flashback",
            "applies_to": {"time_ranges": [{"start": 0, "end": 3}]},
            "suppresses_check_ids": ["VIS.IDENTITY.OUTFIT_REVISION"],
            "declared_by": {"service_id": "S16"},
        }])
        assert run(h, body)["decision"] == "repair"


# ================================================================ 边界 / 计费

class TestBoundaries:
    def test_b1_prop_ownership_is_reported_by_s36(self):
        # B1：S36 出 VIS.SPACE.PROP_OWNERSHIP，expected_source 指向 S17。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect(
            severity="major", observed="钥匙在 B 手上", character_id="char_b"))],
            expected={"expected_state": [{"unit_id": "u1",
                                          "predicate": "owns",
                                          "expected_value": "A"}]})
        body = h.body(include=["VIS.SPACE.PROP_OWNERSHIP"],
                      expected_state_ref={"service_id": "S17", "id": "cont_1",
                                          "revision": 7, "content_hash": H})
        report = report_of(h, run(h, body))
        [defect] = report["defects"]
        assert defect["type"] == "prop"
        assert defect["expected_source"] == {
            "service_id": "S17", "ref_id": "cont_1", "ref_revision": 7}
        event = json.loads(h.conn.execute(
            "SELECT payload FROM outbox").fetchone()[0])
        assert event["data"]["includes_continuity_defects"] is False
        assert event["data"]["expected_state_consumed"] == {
            "service_id": "S17", "ref_revision": 7}

    def test_b2_identity_drift_is_not_a_continuity_defect(self):
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect(
            observed="身份漂移"))], expected={"expected_state": []})
        report = report_of(h, run(h, h.body(
            include=["VIS.IDENTITY.FACE_DRIFT"])))
        assert all(d["type"] != "continuity" for d in report["defects"])
        event = json.loads(h.conn.execute(
            "SELECT payload FROM outbox").fetchone()[0])
        assert event["data"]["includes_continuity_defects"] is False

    def test_b4_flashback_without_exception_is_judged_normally(self):
        # B4 与 E25-S6 同一条路径：没有例外声明就没有豁免。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect(
            severity="major", observed="闪回穿了现代衣服"))],
            expected={"expected_state": []})
        assert run(h, h.body(
            include=["VIS.IDENTITY.OUTFIT_REVISION"]))["decision"] == "repair"

    def test_b5_report_never_cites_s16(self):
        # B5：S36 不得直接消费 S16。把 expected_source 强行指到 S16 会 500。
        from semantic_qc.service import SemanticQcService as Svc

        original = Svc._expected_source_for

        def bad(check, expected):
            return {"service_id": "S16", "ref_id": "x", "ref_revision": 1}

        Svc._expected_source_for = staticmethod(bad)
        try:
            h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect(
                observed="x", character_id=None))],
                expected={"expected_state": []})
            with pytest.raises(ServiceError) as exc:
                run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
            assert exc.value.code == "REPORT_VALIDATION_FAILED"
            assert "S16" in str(exc.value)
        finally:
            Svc._expected_source_for = staticmethod(original)

    def test_f1_no_provider_credentials_or_second_base_url(self):
        # F1：deps 里只能出现 S26/S28/S17 三个基址，没有供应商地址。
        source = (Path(__file__).resolve().parents[1] / "src" /
                  "semantic_qc" / "deps.py").read_text("utf-8")
        urls = set(re.findall(r"https?://[a-zA-Z0-9./_-]+", source))
        assert urls == set()
        assert source.count("urlopen(") == 1
        for env in ("S26_BASE_URL", "S28_BASE_URL", "S17_BASE_URL"):
            assert env in source

    def test_f1_unconfigured_s26_is_503_before_any_work(self):
        from semantic_qc.deps import build_default_deps

        conn = sqlite3.connect(":memory:", isolation_level=None)
        deps = build_default_deps()          # 环境变量全空
        service = SemanticQcService(conn, **deps)
        h = Harness(conn=conn)
        body = h.body()

        with pytest.raises(ServiceError) as exc:
            service.create_check("t1", "c1", body, "h" * 64)
        assert exc.value.status == 503
        assert exc.value.code == "S26_UNAVAILABLE"
        assert conn.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 0

    def test_f2_budget_rejection_is_not_a_skip(self):
        h = Harness(submit_error=ServiceError(402, "BUDGET_EXHAUSTED", "no"),
                    expected={"expected_state": []})
        report = report_of(h, run(h, h.body(
            include=["VIS.HANDS.OCCLUSION_ARTIFACT"])))
        assert report["check_manifest"][0]["status"] == "failed"

    def test_f3_cross_tenant_read_is_404_without_existence_leak(self):
        h = Harness(expected={"expected_state": []})
        got = run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        with pytest.raises(ServiceError) as exc:
            h.service.get_report("t2", got["report_id"])
        assert exc.value.status == 404
        assert exc.value.code == "REPORT_NOT_FOUND"
        # 同一份"存在"的判定对不存在的 id 完全一样。
        with pytest.raises(ServiceError) as missing:
            h.service.get_report("t1", "qc_nonexistent0000000000000")
        assert missing.value.status == exc.value.status

    def test_f3_cross_tenant_artifact_body_is_rejected(self):
        h = Harness(expected={"expected_state": []})
        body = h.body()
        body["execution_context"]["tenant_id"] = "t2"
        with pytest.raises(ServiceError) as exc:
            run(h, body)
        assert exc.value.status == 403

    def test_f4_replay_returns_same_report_without_new_vlm_calls(self):
        h = Harness(expected={"expected_state": []})
        body = h.body(include=["VIS.IDENTITY.FACE_DRIFT"])
        first = run(h, body)
        submits = h.calls["submit"]
        second = run(h, body)

        assert second["replayed"] is True
        assert second["report_id"] == first["report_id"]
        assert h.calls["submit"] == submits     # 一次都没重跑
        assert h.conn.execute("SELECT COUNT(*) FROM report"
                              ).fetchone()[0] == 1

    def test_f4_same_key_different_body_conflicts(self):
        h = Harness(expected={"expected_state": []})
        run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]), command="c1")
        with pytest.raises(CommandConflict):
            run(h, h.body(include=["VIS.IDENTITY.ACCESSORY_EXTRA"]),
                command="c1")


# ================================================================ 内核行为

class TestKernel:
    def test_report_is_schema_valid_and_recomputable(self):
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect())],
                    expected={"expected_state": []})
        got = run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        report = report_of(h, got)

        assert errors_of(qc_report_schema, report) == []
        assert report["report_id"] == got["report_id"]
        # report_id 的形状：`qc_` + 26 个 base32 小写（§2.6）。
        assert re.fullmatch(r"qc_[a-z0-9]{26}", report["report_id"])
        # 可由派生键重算（command_id 不参与）。
        assert report["report_id"] == "qc_" + base32_lower(derived_key(
            "t1", report["subject"]["artifact_sha256"],
            report["evaluator"]["model_snapshot"], report["policy_version"],
            report["sampling_policy_version"], None))[:26]
        # 语义闸门要能在**正确**的报告上通过（否则它只是一条永真的断言）。
        again = h.service._validate_report(report, ["VIS.IDENTITY.FACE_DRIFT"], {
            "sha256": report["subject"]["artifact_sha256"]})
        assert again is None
        # 反证：篡改 report_id 一定被拦，证明上面那条不是空转。
        broken = {**report, "report_id": "qc_" + "z" * 26}
        with pytest.raises(ServiceError):
            h.service._validate_report(broken, ["VIS.IDENTITY.FACE_DRIFT"], {
                "sha256": report["subject"]["artifact_sha256"]})

    def test_event_is_schema_valid_and_in_the_same_transaction(self):
        # 1s 素材：3 个窗口会给出 3 条 blocker，`blocker_count == 1` 的断言
        # 就变成在数窗口而不是在数缺陷。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect())],
                    expected={"expected_state": []})
        h.artifact = artifact_record(duration_ms=1000)
        got = run(h, h.body(include=["VIS.HANDS.FINGER_DEFORM"]))
        row = h.conn.execute("SELECT * FROM outbox").fetchone()
        event = json.loads(row["payload"])

        assert errors_of(qc_event_schema, event["data"]) == []
        assert event["type"] == "quality.semantic.completed"
        assert event["aggregate_id"] == got["report_id"]
        assert event["sequence"] == 1
        assert event["data"]["blocker_count"] == 1
        assert event["data"]["qc_layer"] == "semantic"
        assert event["data_ref"]["content_hash"] == \
            report_of(h, got)["report_content_hash"]

    def test_generation_increments_when_the_hook_is_reused_with_new_seed(self):
        # 同一派生键再算一次（不同命令、同内容）会落成第 2 代并 supersedes。
        h = Harness(expected={"expected_state": []})
        body = h.body(include=["VIS.IDENTITY.ACCESSORY_EXTRA"])
        first = run(h, body, command="c1")
        second = run(h, body, command="c2")

        assert second["report_id"] == first["report_id"]
        assert second["report_generation"] == 2
        report = report_of(h, second)
        assert report["supersedes"] == {"report_id": first["report_id"],
                                        "report_generation": 1}

    def test_private_keys_never_reach_the_payload(self):
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_defect())],
                    expected={"expected_state": []})
        report = report_of(h, run(h, h.body(
            include=["VIS.IDENTITY.ACCESSORY_EXTRA"])))
        text = json.dumps(report, ensure_ascii=False)
        assert "_window_index" not in text
        assert "_suppressed" not in text
        assert "_promoted" not in text

    def test_secondary_evaluator_gets_its_own_s26_command(self):
        # SQC-24：主/副必须是两条 S26 命令，否则第二意见就是同一个答案。
        # 用 shot 范围的 BODY_PROPORTION（也是高风险），1 个窗口 → 恰好 2 条
        # 命令，键的差异只可能来自角色后缀，不会被窗口号掩盖。
        h = Harness(expected={"expected_state": []})
        h.artifact = artifact_record(duration_ms=1000)
        run(h, h.body(include=["VIS.IDENTITY.BODY_PROPORTION"]))
        assert len(h.submits) == 2
        keys = [c["idempotency_key"] for c in h.submits]
        assert keys[0] != keys[1]
        assert keys[0].endswith(":primary") and keys[1].endswith(":secondary")
        # 换掉角色后缀后两条键必须完全相同——差异只来自角色。
        assert keys[0][:-len(":primary")] == keys[1][:-len(":secondary")]

    def test_generation_command_matches_the_contract(self):
        h = Harness(expected={"expected_state": []})
        h.artifact = artifact_record(duration_ms=1000)
        run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        [command] = h.submits[:1]

        schema = json.loads(
            (REPO / "contracts/generation-command.v2.schema.json")
            .read_text("utf-8"))
        # `context` 是对 execution-context.v2 的 `$ref`，而它的 `$id` 是
        # `https://drama.example.invalid/...`——不带 registry 的校验器会去
        # 网络上找它（在 CI 里就是超时/SSL 错）。把两份 schema 按各自 `$id`
        # 注册进去，与 `tools/validate/validate_examples.py` 同一套做法。
        assert errors_of_with_registry(schema, command) == []
        assert command["modality"] == "understanding"
        assert set(command["input"]) == {"media_refs", "question",
                                         "context_pack_ref", "output_schema_id"}
        assert command["budget_scope_id"].startswith("budget_qc_")

    def test_coverage_counts_a_window_only_if_every_call_succeeded(self):
        # 主成功、副失败：那个窗口只被看了一半，不能报成全覆盖。
        h = Harness(script=[("PROVIDER_SUCCEEDED", vlm_pass()),
                            ("PROVIDER_FAILED", None)],
                    expected={"expected_state": []})
        got = run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        report = report_of(h, got)
        assert report["coverage"]["temporal_windows_evaluated"] == 0
        assert got["decision"] == "abstain"

    def test_shot_scope_only_selection_is_not_penalised_for_windows(self):
        # 只选 shot 范围时一次整镜调用就覆盖完了，分母是 1 而不是 3。
        h = Harness(expected={"expected_state": []})
        got = run(h, h.body(include=["VIS.IDENTITY.BODY_PROPORTION"]))
        report = report_of(h, got)
        assert report["coverage"]["planned_windows"] == 1
        assert report["coverage"]["coverage_ratio"] == 1.0

    def test_route_decision_is_required(self):
        # SQC-9：没有路由决策就不能投 S26，也不能自己编。
        h = Harness(expected={"expected_state": []})
        body = h.body(include=["VIS.IDENTITY.FACE_DRIFT"])
        body.pop("evaluator_route_decision_id")
        with pytest.raises(ServiceError) as exc:
            run(h, body)
        assert exc.value.status == 503
        assert exc.value.code == "ROUTE_DECISION_UNAVAILABLE"

    def test_frozen_submission_is_never_resubmitted(self):
        # SUBMISSION_UNKNOWN：不知道供应商是否收下这次付费调用 → 不重提、不判过。
        #
        # 1s 素材把范围收到 1 个窗口：这样 `submit == 1` 的含义是「同一个窗口
        # 只提交了一次」，而不是「3 个窗口各提交一次正好凑够 3」。用 3s 素材
        # 的话 3 次提交都是不同窗口，断言就测不到「重提」这件事。
        h = Harness(script=[("SUBMISSION_UNKNOWN", None)],
                    expected={"expected_state": []})
        h.artifact = artifact_record(duration_ms=1000)
        got = run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        assert got["decision"] == "abstain"
        assert h.calls["submit"] == 1            # 同一窗口只提交过一次，没有重提
        calls = all_calls(h, got["report_id"])
        assert [c["outcome"] for c in calls] == ["error"]

    def test_artifact_hash_mismatch_is_409(self):
        h = Harness(expected={"expected_state": []})
        h.artifact = artifact_record(content_hash="d" * 64)
        with pytest.raises(ServiceError) as exc:
            run(h, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        assert exc.value.status == 409
        assert exc.value.code == "ARTIFACT_REVISION_MISMATCH"

    def test_artifact_from_another_service_is_rejected(self):
        h = Harness(expected={"expected_state": []})
        body = h.body()
        body["artifact"] = {**body["artifact"], "service_id": "S16"}
        with pytest.raises(ServiceError) as exc:
            run(h, body)
        assert exc.value.code == "INVALID_REFERENCE"


class TestDecide:
    """纯函数层：§2.7 的八步顺序，以及它不允许的几件事。"""

    def entry(self, check_id, status="evaluated", reason="ok"):
        return {"check_id": check_id, "status": status, "reason_code": reason}

    def coverage(self, ratio, planned=10, evaluated=None):
        return {"coverage_ratio": ratio,
                "temporal_windows_evaluated":
                    ratio * planned if evaluated is None else evaluated,
                "planned_windows": planned}

    def result(self, check_id, status="evaluated", reason="ok",
               confidence=0.9, disagreement=None, defects=()):
        return {"check_id": check_id, "status": status, "reason_code": reason,
                "confidence": confidence, "disagreement": disagreement,
                "defects": list(defects),
                "check": registry.BY_ID[check_id]}

    def test_step3_abstains_before_scoring(self):
        # 一个超时 + 一堆漂亮的高分 → 仍然是弃权。这是「未知不是低分」。
        h = Harness()
        decision, reason, abstain, confidence = h.service.decide(
            manifest=[self.entry("VIS.IDENTITY.FACE_DRIFT", "failed",
                                 "evaluator_timeout")],
            defects=[],
            coverage=self.coverage(1.0),
            results=[self.result("VIS.IDENTITY.FACE_DRIFT", "failed",
                                 "evaluator_timeout")])
        assert decision == "abstain"
        assert abstain["reason_code"] == "evaluator_timeout"
        assert confidence == 0.0

    def test_unjustified_not_applicable_also_abstains(self):
        h = Harness()
        decision, _, abstain, _ = h.service.decide(
            manifest=[self.entry("VIS.SPACE.PROP_STATE", "not_applicable",
                                 "asset_unavailable")],
            defects=[], coverage=self.coverage(1.0),
            results=[self.result("VIS.SPACE.PROP_STATE", "not_applicable",
                                 "asset_unavailable")])
        assert decision == "abstain"
        assert abstain["failed_check_ids"] == ["VIS.SPACE.PROP_STATE"]

    def test_justified_not_applicable_does_not_abstain(self):
        h = Harness()
        decision, _, abstain, _ = h.service.decide(
            manifest=[self.entry("VIS.IDENTITY.FACE_DRIFT", "not_applicable",
                                 "no_face")],
            defects=[], coverage=self.coverage(1.0),
            results=[self.result("VIS.IDENTITY.FACE_DRIFT", "not_applicable",
                                 "no_face")])
        assert decision == "pass"
        assert abstain is None

    def test_low_confidence_abstain_always_names_a_check(self):
        # SQC-25：契约要求 failed_check_ids 非空，一个都不能少。
        h = Harness()
        decision, _, abstain, _ = h.service.decide(
            manifest=[self.entry("VIS.TEMPORAL.FLICKER")], defects=[],
            coverage=self.coverage(1.0),
            results=[self.result("VIS.TEMPORAL.FLICKER", confidence=0.1)])
        assert decision == "abstain"
        assert abstain["failed_check_ids"]

    def test_major_below_tau_low_goes_to_human_review(self):
        h = Harness()
        defect = {"severity": "major", "confidence": 0.2,
                  "recommended_actions": ["regenerate_video"]}
        decision, _, _, _ = h.service.decide(
            manifest=[self.entry("VIS.SPACE.PERSON_COUNT")], defects=[defect],
            coverage=self.coverage(1.0),
            results=[self.result("VIS.SPACE.PERSON_COUNT")])
        assert decision == "human_review"

    def test_blocker_does_not_care_about_confidence(self):
        h = Harness()
        defect = {"severity": "blocker", "confidence": 0.01,
                  "recommended_actions": ["regenerate_video"]}
        decision, _, _, _ = h.service.decide(
            manifest=[self.entry("VIS.IDENTITY.FACE_DRIFT")], defects=[defect],
            coverage=self.coverage(1.0),
            results=[self.result("VIS.IDENTITY.FACE_DRIFT")])
        assert decision == "repair"


class TestPieces:
    def test_promote_needs_consecutive_windows(self):
        # §4：连续 ≥2 窗口才升档；孤立命中不升，但缺陷照报。
        check = registry.BY_ID["VIS.IDENTITY.ACCESSORY_EXTRA"]
        one = [{"defect_fingerprint": "f" * 32, "severity": "minor",
                "_window_index": 0}]
        assert _promote(one, check)[0]["severity"] == "minor"

        two = [{"defect_fingerprint": "f" * 32, "severity": "minor",
                "_window_index": index} for index in (0, 1)]
        got = _promote(two, check)
        assert all(d["severity"] == "major" for d in got)

        gap = [{"defect_fingerprint": "f" * 32, "severity": "minor",
                "_window_index": index} for index in (0, 2)]
        assert all(d["severity"] == "minor" for d in _promote(gap, check))

    def test_jaccard_ignores_wording(self):
        a = [{"type": "identity", "severity": "blocker",
              "time_range": {"start_seconds": 2.0, "end_seconds": 2.6}}]
        b = [{"type": "identity", "severity": "blocker",
              "time_range": {"start_seconds": 2.01, "end_seconds": 2.7}}]
        assert _jaccard(a, b) == 1.0
        assert _jaccard(a, []) == 0.0
        assert _jaccard([], []) == 1.0

    def test_public_strips_private_keys_recursively(self):
        value = {"a": 1, "_b": 2, "c": [{"_d": 3, "e": 4}]}
        assert _public(value) == {"a": 1, "c": [{"e": 4}]}

    def test_derived_key_ignores_command_identity(self):
        kwargs = dict(tenant_id="t1", artifact_sha256=H, model_snapshot=None,
                      policy_version="p", sampling_version="s",
                      attempt_slot_id=None)
        assert derived_key(**kwargs) == derived_key(**kwargs)
        assert derived_key(**{**kwargs, "attempt_slot_id": "slot_2"}) != \
            derived_key(**kwargs)


# ================================================================ HTTP 层

def http_client(monkeypatch, tmp_path, **overrides):
    """`api/app.py` 的 TestClient，上游全部换成 Harness 的 stub。

    `app.py` 在 import 时自己建连接、自己按环境变量拼上游，所以：
    - `S36_DB` 必须指向独立文件库（import 那一刻就建表）；
    - `module.service` / `module._idem` 必须重绑——路由函数捕获的是模块全局；
    - 重绑时用 `module._conn`，否则断言查的是另一个库。
    """
    monkeypatch.setenv("S36_DB", str(tmp_path / "s36.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s36_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s36_api_app"] = module

    h = Harness(conn=module._conn, expected={"expected_state": []}, **overrides)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h


def post(client, body, *, command="c1", tenant="t1"):
    return client.post("/semantic-checks", json=body,
                       headers={"Idempotency-Key": command,
                                "X-Tenant-Id": tenant})


class TestHttp:
    def test_create_is_202(self, monkeypatch, tmp_path):
        client, h = http_client(monkeypatch, tmp_path)
        response = post(client, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        assert response.status_code == 202
        assert response.json()["report_id"].startswith("qc_")

    def test_same_key_different_body_is_409_with_the_plan_name(
            self, monkeypatch, tmp_path):
        # SQC-14：SDK 的码是 IDEMPOTENCY_KEY_REUSED，对外用方案的名字。
        client, h = http_client(monkeypatch, tmp_path)
        post(client, h.body(include=["VIS.IDENTITY.FACE_DRIFT"]))
        response = post(client, h.body(include=["VIS.IDENTITY.ACCESSORY_EXTRA"]))
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "COMMAND_PAYLOAD_MISMATCH"
        assert body["details"]["sdk_code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_in_flight_is_409_with_retry_after(self, monkeypatch, tmp_path):
        # SQC-13：方案想 202 + 同一 operation_id；SDK 只抛异常，拿不到在途结果。
        client, h = http_client(monkeypatch, tmp_path)
        body = h.body(include=["VIS.IDENTITY.FACE_DRIFT"])
        h.idem.begin("t1", "c1", canonical_request_hash(
            {**body, "command_id": "c1"}))
        response = post(client, body)
        assert response.status_code == 409
        assert response.json()["code"] == "COMMAND_IN_FLIGHT"
        assert response.headers["Retry-After"] == "1"

    def test_replay_is_flagged(self, monkeypatch, tmp_path):
        client, h = http_client(monkeypatch, tmp_path)
        body = h.body(include=["VIS.IDENTITY.FACE_DRIFT"])
        first = post(client, body).json()
        second = post(client, body).json()
        assert second["replayed"] is True
        assert second["report_id"] == first["report_id"]

    def test_get_report_and_include_calls(self, monkeypatch, tmp_path):
        client, h = http_client(monkeypatch, tmp_path)
        # 1s 素材 → 1 个窗口 × 主副两个角色 = 2 条调用记录。3s 素材会是 6 条，
        # 那样断言通过也不证明"一次评估只有两次付费调用"。
        h.artifact = artifact_record(duration_ms=1000)
        created = post(client, h.body(include=["VIS.IDENTITY.FACE_DRIFT"])).json()

        response = client.get(f"/semantic-reports/{created['report_id']}",
                              params={"include": "evaluator_calls"},
                              headers={"X-Tenant-Id": "t1"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["report"]["report_id"] == created["report_id"]
        assert len(payload["evaluator_calls"]) == 2   # 双评估
        # 两条调用必须来自两个不同模型（副评估器不是同一次调用换个名字）。
        assert len({c["model_snapshot"] for c in payload["evaluator_calls"]}) == 2

    def test_get_across_tenant_is_404(self, monkeypatch, tmp_path):
        client, h = http_client(monkeypatch, tmp_path)
        created = post(client, h.body(include=["VIS.IDENTITY.FACE_DRIFT"])).json()
        response = client.get(f"/semantic-reports/{created['report_id']}",
                              headers={"X-Tenant-Id": "t2"})
        assert response.status_code == 404

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        client, h = http_client(monkeypatch, tmp_path)
        body = h.body()
        body["tenant_id"] = "t2"
        assert post(client, body).status_code == 403

    def test_missing_idempotency_key_is_400(self, monkeypatch, tmp_path):
        client, h = http_client(monkeypatch, tmp_path)
        response = client.post("/semantic-checks", json=h.body(),
                               headers={"X-Tenant-Id": "t1"})
        assert response.status_code == 400

    def test_readiness_names_upstreams_and_gaps(self, monkeypatch, tmp_path):
        client, _ = http_client(monkeypatch, tmp_path)
        payload = client.get("/health/readiness").json()
        assert payload["ready"] is True
        assert payload["upstreams"] == {"s26": True, "s28": True, "s17": True}
        assert [g["id"] for g in payload["contract_gaps"]] == \
            [g["id"] for g in CONTRACT_GAPS]

    def test_unconfigured_upstream_is_503_over_http(self, monkeypatch, tmp_path):
        # 环境变量一个都不设 → app 的默认依赖全是未配置闭包。
        monkeypatch.delenv("S26_BASE_URL", raising=False)
        monkeypatch.delenv("S28_BASE_URL", raising=False)
        monkeypatch.delenv("S17_BASE_URL", raising=False)
        client, h = http_client(monkeypatch, tmp_path)
        # 这次不重绑 service，用 app 自己按环境变量拼出来的那个。
        module = sys.modules["s36_api_app"]
        module.service = SemanticQcService(module._conn,
                                          **__import__(
                                              "semantic_qc.deps",
                                              fromlist=["build_default_deps"])
                                          .build_default_deps())
        response = post(client, h.body())
        assert response.status_code == 503
        assert response.json()["code"] == "S26_UNAVAILABLE"


class TestContractGaps:
    def test_shape_and_unique_ids(self):
        assert CONTRACT_GAPS, "缺口清单不能为空——它是本轮唯一的报告出口"
        ids = [gap["id"] for gap in CONTRACT_GAPS]
        assert len(ids) == len(set(ids))
        for gap in CONTRACT_GAPS:
            assert set(gap) == {"id", "kind", "summary", "handling"}
            assert gap["kind"] in ("conflict", "needs_owner", "gap")
            assert gap["summary"].strip() and gap["handling"].strip()

    def test_every_plan_gap_c1_to_c5_is_registered(self):
        # 方案 §11 的 C1…C5 必须都能在 SQC-1…SQC-5 里找到（编号刻意对齐）。
        for number in range(1, 6):
            assert f"SQC-{number}" in {g["id"] for g in CONTRACT_GAPS}
