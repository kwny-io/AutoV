"""S36 内核。方案：`plans/S36-semantic-qc.md`。

一句话边界：**S36 只回答「画面和绑给它的东西对得上吗」，而且只在真的看过之后。**

四条不能让步的性质，代码里各有落点：

1. **模型调用只有一条路：S26。** 本服务没有供应商凭证，也没有去供应商的
   HTTP 客户端（`deps.py` 只连 S26/S28/S17）。S26 没配 → 整个命令 503，
   不是「跳过 VLM 继续判」——那就等于没有证据也出结论。
2. **没跑成的检查不是通过。** 超时（`evaluator_timeout`）、结构错
   （`schema_error`）、上游拒绝（`evaluator_error`）、期望状态拿不到
   （`dependency_unavailable`）全部记 `failed`，并在 `decide()` 的第 3 步
   直接弃权——**在评分之前**。这是 docs/16 E25 与 docs/11 §3 的硬要求。
3. **不做修复。** `recommended_actions` 只是建议；S36 不触发生成、不改
   artifact、不写别人的库。修复是 S39。
4. **合法例外由 S17 声明，本服务只认带来源的。** 没有 `declared_by` 的例外
   直接丢弃并按普通检查判（E25-S6）；被例外压掉的观测仍留在 `dimensions`
   里，审计能看见「我们看到了不一致，但有人声明它合法」。

`decide()` 按 S35 §2.7 实现（三个质检服务共用同一算法；本仓还没有
`platform/qc-sdk`，所以只能对齐语义、不能共享实现，记在 SQC-25 的相邻缺口里）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from . import registry
from .schema import ID_PREFIX, SCHEMA

SOURCE = "S36"
SCHEMA_VERSION = "2.0"
QC_LAYER = "semantic"
EVENT_TYPE = "quality.semantic.completed"
TOPIC = "drama.semantic-qc.v2"

EVALUATOR_NAME = "s36.semantic-qc"
# 评估器版本参与 report_id 派生：换了问题口径或判定规则就必须换版本，
# 否则同一个 report_id 会代表两套不同的判定。
EVALUATOR_VERSION = "s36-eval.v1"
# 本服务自带的镜像摘要（镜像不在本仓构建，这里只是契约要求的必填位）。
EVALUATOR_IMAGE_DIGEST = "sha256:s36-semantic-qc-unbuilt"

POLICY_VERSION = "s36-policy.v1"

# 提示模板版本。模板本身在 S20，S36 的 `depends_on` 里没有它，所以这里记的是
# 「本服务发出去的问题形状」的版本；换问题就要 +1，因为它会改变 call 的可复现性。
PROMPT_TEMPLATE_REV = 1

# 方案 §9 之外的两个必需坐标，见 SQC-10。
EXTRA_CONTEXT_KEYS = ("shot_lineage_id", "run_id")

# S26 job 状态分类。**冻结状态绝不重提**（AGENTS.md 硬红线）。
S26_SUCCESS_STATES = frozenset({"PROVIDER_SUCCEEDED", "INGESTING", "ARCHIVED",
                                "SUCCEEDED", "COMPLETED"})
S26_FAILURE_STATES = frozenset({"PROVIDER_FAILED", "CANCELED", "REJECTED",
                                "REJECTED_BUDGET", "REJECTED_QUOTA"})
S26_FROZEN_STATES = frozenset({"SUBMISSION_UNKNOWN", "RECONCILING"})

# 轮询上限。真实等待由 deadline 兜底；这里防的是「状态一直不终结」把请求挂死。
POLL_MAX = 20
POLL_INTERVAL_SECONDS = 0.02

# SQC-3：没有 `vlm-response.v2.schema.json`，内核自定临时形状。
# 严格解析：多一个键、少一个必填键都算 schema_error——不做 best-effort 兼容，
# 否则「模型说了什么」会随着解析器的宽容度悄悄漂移。
VLM_REQUIRED = ("verdict", "severity", "confidence")
VLM_OPTIONAL = ("reason_code", "time_range", "region_bbox", "character_id",
                "expected", "observed", "measured", "evidence_artifact_ids",
                "recommended_actions")
VLM_VERDICTS = frozenset({"pass", "defect", "not_applicable"})
VLM_NOT_APPLICABLE_REASONS = frozenset({"no_such_content", "no_face"})
SEVERITIES = frozenset(registry.SEVERITY_ORDER)

# 报告 `defect.scope` 的闭集与注册表 `window_scope` 不是同一套枚举，
# 映射放在一处，避免每个缺陷各译一次。
SCOPE_MAP = {"frame": "frame", "window": "window", "shot": "segment",
             "track": "segment", "all": "whole_artifact"}

# 4 键 ref 的服务归属。S36 不读别人能写的资产：画面只从 S28 来。
REF_SERVICE_ARTIFACT = "S28"
REF_SERVICE_SHOT = "S06"
REF_SERVICE_BINDING = "S10"
REF_SERVICE_EXPECTED = "S17"

CONTEXT_REQUIRED = ("tenant_id", "project_id", "series_id", "branch_id",
                    "episode_id", "home_cell", "manifest_ref", "node_id",
                    "node_epoch", "execution_fence", "policy_ref", "trace_id")

TOP_REQUIRED = ("execution_context", "artifact", "shot_spec_ref",
                "binding_set_ref", "shot_lineage_id", "run_id",
                "check_selection", "evaluator_profile", "deadline")

SUPPORTED_PROFILES = frozenset({"dual_high_risk"})

# `check_manifest.reason_code` 与 `abstain.reason_code` 是两个不同的闭集
# （qc-report.v2 里分别声明）。这里只做翻译，不改写任何一边的值。
ABSTAIN_FOR_REASON = {
    "evaluator_timeout": "evaluator_timeout",
    "evaluator_error": "evaluator_error",
    "dependency_unavailable": "dependency_stale",
    "asset_unavailable": "asset_unavailable",
}


# ---------------------------------------------------------------- 基础助手

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def base32_lower(digest_hex: str) -> str:
    """sha256 的十六进制 → 小写 base32（无填充）。

    `report_id` 要求 `^qc_[a-z0-9]{26}$`，所以必须是 base32 而不是 base64：
    base64 会带 `+` `/` 两个非法字符。
    """
    raw = bytes.fromhex(digest_hex)
    return base64.b32encode(raw).decode("ascii").lower().rstrip("=")


def _rounded_bucket(seconds: float) -> int:
    """0.5s 时间桶，四舍五入。

    不用 Python 的 `round`：它是银行家舍入（`round(0.5)==0`，`round(1.5)==2`），
    会让桶边界两边的缺陷落到不对称的桶里，复检时同类缺陷无法归并。
    """
    return int(math.floor(float(seconds) / 0.5 + 0.5))


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。不嵌套：一个命令一个事务。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------ 纯函数：身份

def derived_key(tenant_id: str, artifact_sha256: str, model_snapshot,
                policy_version: str, sampling_version: str,
                attempt_slot_id: str | None) -> str:
    """S35 §2.6 的派生键。**`command_id` 不参与**——同一份内容换个命令重算，
    必须得到同一份报告，否则幂等只能靠命令表，报告本身不可复用。
    """
    return _sha256_hex(_jcs({
        "tenant_id": tenant_id,
        "artifact_sha256": artifact_sha256,
        "qc_layer": QC_LAYER,
        "evaluator.name": EVALUATOR_NAME,
        "evaluator.version": EVALUATOR_VERSION,
        "evaluator.model_snapshot": model_snapshot,
        "policy_version": policy_version,
        "sampling_policy_version": sampling_version,
        "repair_context.attempt_slot_id": attempt_slot_id,
    }))


def defect_fingerprint(check_id: str, defect_type: str, character_id,
                       severity: str, start_seconds: float) -> str:
    """S35 §2.4。与 artifact hash 无关，复检才能认出「同一个缺陷又回来了」。

    严重度只分两档：major 与 minor 之间抖动不该分裂身份，blocker 与
    non_blocker 必须分裂（前者要重生成，后者可能只需修）。
    """
    return _sha256_hex(_jcs({
        "qc_layer": QC_LAYER,
        "check_id": check_id,
        "type": defect_type,
        "character_id": character_id,
        "severity_bucket": "blocker" if severity == "blocker" else "non_blocker",
        "location_bucket": _rounded_bucket(start_seconds),
    }))[:32]


def defect_id_for(fingerprint: str, time_range: dict) -> str:
    # 同一指纹在不同窗口可能同时命中（比如连续两窗的漂移），id 带上时间段，
    # 否则两条缺陷会拿到同一个 id。
    digest = _sha256_hex(_jcs({"fingerprint": fingerprint,
                               "start": time_range["start_seconds"],
                               "end": time_range["end_seconds"]}))
    return "df_" + base32_lower(digest)[:26]


# ------------------------------------------------------------------ 服务

class SemanticQcService:
    """一次语义质检 = 一次命令 = 一份报告。评估在事务外，落库在一个事务里。"""

    def __init__(self, conn: sqlite3.Connection, *, artifact_source,
                 expected_state_source, submit_source, job_source,
                 detector_source=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 隐式提交当前事务。只能在事务外建一次，之后复用同一实例，否则命令
        # 事务会在第一条事件写入时被悄悄提交，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        self.artifact_source = artifact_source
        self.expected_state_source = expected_state_source
        self.submit_source = submit_source
        self.job_source = job_source
        # 本地检测器是可选的**提示**来源。它的缺席不会让检查变成通过：
        # 没有提示时模型仍然要答，模型不答就是 failed。见 `_hints`。
        self.detector_source = detector_source

    # ------------------------------------------------------------ 上游守卫

    def _require_upstream(self):
        """未配置的上游在花任何力气之前就 503。

        依赖闭包带 `configured` 标记（`deps.py` 里两种实现都设了）。这样
        「忘了配 S26」不会走到第 4 步才失败，也不会留下半份报告。
        """
        for code, fn in (("S26_UNAVAILABLE", self.submit_source),
                         ("S26_UNAVAILABLE", self.job_source),
                         ("S28_UNAVAILABLE", self.artifact_source)):
            if getattr(fn, "configured", True) is False:
                raise ServiceError(503, code,
                                   "upstream is not configured; S36 will not "
                                   "produce a verdict without it")

    def _upstream(self, call, code: str):
        """把「不可达」统一成 503；4xx 原样上抛（跨租户要保住 404）。

        408 / 504 必须**原样**上抛。它们也是 >=500，但语义完全不同：超时是
        「这一次没结论」，`_invoke` 靠它记 `outcome=timeout` 并让该 check
        failed；改写成 503 会让超时分支永远走不到，于是一次超时被读成
        「服务不可用」——更糟的是 E25-S1 的 `evaluator_timeout` 再也发不出来。
        """
        try:
            return call()
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc
        except ServiceError as exc:
            if exc.status in (408, 504):
                raise
            if exc.status >= 500:
                raise ServiceError(503, code, str(exc), retryable=True,
                                   details=exc.details) from exc
            raise

    # ------------------------------------------------------------ 命令入口

    def create_check(self, tenant_id: str, command_id: str, body: dict,
                     request_hash: str) -> dict:
        self._require_upstream()
        ctx = self._validate(tenant_id, body)

        artifact = self._resolve_artifact(tenant_id, body["artifact"])
        expected = self._resolve_expected_state(tenant_id, body)
        exceptions = self._legal_exceptions(body)

        shot_type, shot_type_defaulted = self._shot_type(body)
        windows = registry.window_plan(shot_type, artifact["duration_seconds"])
        selected = self._selected(body.get("check_selection"))

        hints = self._hints(tenant_id, body["artifact"], windows)

        results, calls = [], []
        for check_id in selected:
            outcome = self._evaluate_check(
                tenant_id=tenant_id, body=body, ctx=ctx,
                artifact=artifact, expected=expected, exceptions=exceptions,
                hints=hints, check=registry.BY_ID[check_id],
                windows=windows, shot_type=shot_type)
            results.append(outcome)
            calls.extend(outcome["calls"])

        dimensions = self._dimensions(results, shot_type_defaulted,
                                     artifact, hints)
        manifest = [self._manifest_entry(r) for r in results]
        defects = [_public(d) for r in results
                   for d in (r.get("defects") or [])]
        # 每个 check 的诊断文字也是一条 dimension。契约的 dimension 是
        # `{name, score, reason}`，score=null 表示「不可打分，只是留痕」——
        # 直接把字符串塞进数组会让整份报告被 schema 拒收。
        for result in results:
            if result.get("notes"):
                dimensions.append({
                    "name": f"note.{result['check_id']}", "score": None,
                    "reason": str(result["notes"])})

        coverage = self._coverage(
            artifact, self._planned_windows(selected, windows), results)
        decision, reason, abstain, confidence = self.decide(
            manifest=manifest, defects=defects, coverage=coverage,
            results=results)

        model_snapshot = self._model_snapshot(body)
        gen_key = derived_key(tenant_id, artifact["sha256"], model_snapshot,
                              POLICY_VERSION, registry.SAMPLING_VERSION,
                              self._attempt_slot(body))
        report_id = "qc_" + base32_lower(gen_key)[:26]
        generation, supersedes = self._next_generation(tenant_id, report_id)

        payload = {
            "schema_version": SCHEMA_VERSION,
            "report_id": report_id,
            "report_generation": generation,
            "qc_layer": QC_LAYER,
            "context": self._report_context(ctx, body),
            "subject": {
                "artifact_id": body["artifact"]["id"],
                "artifact_sha256": artifact["sha256"],
                "artifact_kind": artifact["kind"],
                "media": artifact["media"],
            },
            "shot_ref": {
                "shot_id": body["shot_spec_ref"]["id"],
                "shot_revision": int(body["shot_spec_ref"]["revision"]),
                "logical_node_id": ctx["node_id"],
                "node_epoch": int(ctx["node_epoch"]),
            },
            "evaluator": {
                "name": EVALUATOR_NAME,
                "version": EVALUATOR_VERSION,
                "image_digest": EVALUATOR_IMAGE_DIGEST,
                "model_snapshot": model_snapshot,
            },
            "policy_version": POLICY_VERSION,
            "sampling_policy_version": registry.SAMPLING_VERSION,
            "check_manifest": manifest,
            "defects": defects,
            "dimensions": dimensions,
            "coverage": coverage,
            "decision": decision,
            "decision_reason": reason,
            "abstain": abstain,
            "confidence": round(confidence, 4),
            "supersedes": supersedes,
            "repair_context": self._repair_context(body),
            "evidence_bundle_refs": [self._artifact_ref(body["artifact"])],
            "created_at": _now(),
        }
        payload["report_content_hash"] = _sha256_hex(_jcs(
            {k: v for k, v in payload.items() if k != "report_content_hash"}))
        self._validate_report(payload, selected, artifact)
        # 事件里有两个量不属于报告本体（`expected_state_consumed` 的形状与
        # 报告里的完整 ref 不同，`model_disagreement` 是聚合值），单独传，
        # 不塞进 payload——塞进去会进入 content_hash，报告就不再可复算了。
        self._persist(
            tenant_id, command_id, payload, calls, ctx,
            expected_ref=expected.get("ref"),
            disagreement=max([0.0] + [r["disagreement"] for r in results
                                      if r.get("disagreement") is not None]))
        operation_id = ID_PREFIX["operation"] + _sha256_hex(
            f"{report_id}:{generation}")[:20]
        return {
            "operation_id": operation_id,
            "report_id": report_id,
            "report_generation": generation,
            "decision": decision,
            "decision_reason": reason,
            "coverage_ratio": coverage["coverage_ratio"],
            "evaluator_calls": len(calls),
        }

    # ------------------------------------------------------------ 校验

    def _validate(self, tenant_id: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
        for key in TOP_REQUIRED:
            if key not in body:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"missing required field: {key}")
        ctx = body["execution_context"]
        if not isinstance(ctx, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "execution_context must be an object")
        missing = [k for k in CONTEXT_REQUIRED if not ctx.get(k)]
        missing += [k for k in EXTRA_CONTEXT_KEYS if not body.get(k)]
        if missing:
            # SQC-10：`shot_lineage_id` / `run_id` 在 execution-context.v2 里
            # 没有位置（additionalProperties:false），只能从命令体顶层来。
            # 缺了就报，不拿分镜 id 顶替——血缘是 S39 的归并键。
            raise ServiceError(
                422, "EXECUTION_CONTEXT_INCOMPLETE",
                "missing coordinates: " + ", ".join(sorted(missing)))
        if ctx["tenant_id"] != tenant_id:
            # 租户来自签名头；body 里的 tenant_id 只用于比对（AGENTS.md）。
            raise ServiceError(403, "FORBIDDEN_SCOPE",
                               "execution_context tenant_id disagrees with "
                               "the signed tenant")
        for key, expect in ((("artifact"), REF_SERVICE_ARTIFACT),
                            (("shot_spec_ref"), REF_SERVICE_SHOT),
                            (("binding_set_ref"), REF_SERVICE_BINDING)):
            self._check_ref(body[key], expect, key)
        expected_ref = body.get("expected_state_ref")
        if expected_ref is not None:
            self._check_ref(expected_ref, REF_SERVICE_EXPECTED,
                            "expected_state_ref")
        if not body.get("evaluator_route_decision_id"):
            # SQC-9：S26 只接受带路由决策的提交，而 S36 的 depends_on 里没有
            # S25。缺这条决策时既不能自己编一个（第二个权威），也不能降级成
            # 直连模型（AGENTS.md 红线）；只能 503 让调用方补。
            raise ServiceError(
                503, "ROUTE_DECISION_UNAVAILABLE",
                "evaluator_route_decision_id is required; S36 does not invent "
                "a route decision and never calls a provider directly",
                retryable=True)
        if body.get("evaluator_profile") not in SUPPORTED_PROFILES:
            raise ServiceError(
                400, "UNSUPPORTED_EVALUATOR_PROFILE",
                "only 'dual_high_risk' is implemented; other profiles would "
                "silently change how many evaluators run")
        self._selected(body.get("check_selection"))
        return ctx

    @staticmethod
    def _check_ref(ref, service_id: str, field: str) -> None:
        if not isinstance(ref, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"{field} must be an object")
        if ref.get("service_id") != service_id:
            # 画面只能从 S28 读、期望只能从 S17 读。别的来源一律拒绝，
            # 这不是格式问题，是「谁的数据可信」的问题。
            raise ServiceError(400, "INVALID_REFERENCE",
                               f"{field}.service_id must be {service_id}")
        if not ref.get("id") or not isinstance(ref.get("revision"), int):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"{field} needs id and integer revision")
        if not isinstance(ref.get("content_hash"), str) or \
                len(ref["content_hash"]) != 64:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"{field}.content_hash must be 64 hex chars")

    def _selected(self, selection) -> list[str]:
        if not isinstance(selection, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "check_selection must be an object")
        include = selection.get("include")
        exclude = selection.get("exclude") or []
        if not isinstance(exclude, list):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "check_selection.exclude must be a list")
        if include is not None and not isinstance(include, list):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "check_selection.include must be a list")
        try:
            return registry.select(include, exclude)
        except ValueError as exc:
            # 拼错一个前缀就静默少查一组——这是「沉默变成绿灯」最容易发生的地方。
            raise ServiceError(400, "INVALID_CHECK_SELECTION", str(exc)) from exc

    def _shot_type(self, body: dict) -> tuple[str, bool]:
        shot_spec = body.get("shot_spec")
        if isinstance(shot_spec, dict) and shot_spec.get("shot_type"):
            value = shot_spec["shot_type"]
            if value in registry.SAMPLING_WINDOWS:
                return value, False
            raise ServiceError(400, "INVALID_SHOT_TYPE",
                               f"unknown shot_type: {value}")
        # SQC-11：`depends_on` 没有 S06，命令只给引用不内联内容。缺类型时
        # 退回 dialogue 并在 dimensions 里留痕；不假装知道。
        return registry.DEFAULT_SHOT_TYPE, True

    @staticmethod
    def _attempt_slot(body: dict):
        repair = body.get("repair_context")
        if isinstance(repair, dict):
            return repair.get("attempt_slot_id")
        return None

    @staticmethod
    def _repair_context(body: dict):
        repair = body.get("repair_context")
        if repair is None:
            return None
        if not isinstance(repair, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "repair_context must be an object or null")
        for key in ("repair_plan_id", "attempt_slot_id", "attempt_seq",
                    "defect_fingerprints"):
            if key not in repair:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"repair_context missing {key}")
        return {
            "repair_plan_id": repair["repair_plan_id"],
            "attempt_slot_id": repair["attempt_slot_id"],
            "attempt_seq": int(repair["attempt_seq"]),
            "defect_fingerprints": list(repair["defect_fingerprints"]),
        }

    def _model_snapshot(self, body: dict):
        """主评估器的模型快照。它参与 report_id 派生，所以必须稳定。

        契约允许 `model_snapshot` 为 null；本服务在没有 S25 决策时不编造
        模型名，如实发 null。
        """
        snapshot = body.get("evaluator_model_snapshot")
        if snapshot is None:
            return None
        if not isinstance(snapshot, dict) or set(snapshot) != {
                "provider", "model_id", "snapshot"}:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "evaluator_model_snapshot must carry "
                               "provider/model_id/snapshot")
        return snapshot

    # ------------------------------------------------------------ 读上游

    def _resolve_artifact(self, tenant_id: str, ref: dict) -> dict:
        detail = self._upstream(
            lambda: self.artifact_source(tenant_id, ref), "S28_UNAVAILABLE")
        if not isinstance(detail, dict):
            raise ServiceError(503, "S28_UNAVAILABLE",
                               "artifact read returned no record", retryable=True)
        actual = detail.get("content_hash")
        if actual and actual != ref["content_hash"]:
            # 引用与实读不一致：后面的缺陷会挂在一个不是我们检查过的字节序列上。
            raise ServiceError(
                409, "ARTIFACT_REVISION_MISMATCH",
                "artifact content_hash differs from the referenced revision",
                details={"referenced": ref["content_hash"], "actual": actual})
        duration_ms = detail.get("duration_ms")
        if not isinstance(duration_ms, (int, float)):
            raise ServiceError(503, "S28_UNAVAILABLE",
                               "artifact record has no duration_ms",
                               retryable=True)
        return self._artifact_view(ref, detail)

    @staticmethod
    def _artifact_view(ref: dict, detail: dict) -> dict:
        duration = max(0.0, float(detail.get("duration_ms") or 0) / 1000.0)
        frame_rate = detail.get("frame_rate") or {}
        fps_num = int(frame_rate.get("num") or 0) or 24000
        fps_den = int(frame_rate.get("den") or 0) or 1001
        width = int(detail.get("width_px") or 0)
        height = int(detail.get("height_px") or 0)
        channels = detail.get("audio_channels")
        audio = None
        if channels:
            audio = {
                "codec": str(detail.get("audio_codec") or ""),
                "sample_rate_hz": int(detail.get("sample_rate_hz") or 48000),
                "channels": int(channels),
                "stream_index": int(detail.get("audio_stream_index") or 0),
                "start_offset_seconds": float(
                    detail.get("audio_start_offset_seconds") or 0.0),
            }
        media = {
            "duration_seconds": duration,
            # SQC-12：S28 不提供 codec/pix_fmt/color_range/is_vfr/frame_count。
            # 空值与推断值都如实写，并在 dimensions 标 `media_facts_inferred`。
            "video": {
                "width": max(1, width),
                "height": max(1, height),
                "fps_num": max(1, fps_num),
                "fps_den": max(1, fps_den),
                "codec": str(detail.get("video_codec") or ""),
                "pix_fmt": str(detail.get("pix_fmt") or ""),
                "color_range": str(detail.get("color_range") or "unknown"),
                "is_vfr": bool(detail.get("is_vfr") or False),
                "frame_count": int(round(duration * fps_num / fps_den)),
            },
            "audio": audio,
        }
        return {
            "id": ref["id"],
            "sha256": ref["content_hash"],
            "kind": str(detail.get("artifact_kind") or "video"),
            "duration_seconds": duration,
            "media": media,
            "detail": detail,
        }

    def _resolve_expected_state(self, tenant_id: str, body: dict):
        ref = body.get("expected_state_ref")
        if not ref:
            # 没拿到期望状态不是错误：依赖它的 check 记 dependency_unavailable，
            # 若被选中则整份 abstain（§9 末段：不允许 silent skip）。
            return {"ref": None, "value": None, "rows": [], "available": False}
        try:
            value = self._upstream(
                lambda: self.expected_state_source(tenant_id, ref),
                "S17_UNAVAILABLE")
        except ServiceError:
            # 给了引用但读不到（S17 掉线 / 404 / 5xx）：仍然把 ref 留着，好让
            # 报告和事件说明"我们本来要拿哪一份期望"。`available=False` 才是
            # 依赖它的 check 该看的信号。
            return {"ref": ref, "value": None, "rows": [], "available": False}
        rows = []
        if isinstance(value, dict):
            rows = value.get("expected_state") or []
        return {"ref": ref, "value": value, "rows": rows,
                "available": isinstance(value, dict)}

    def _hints(self, tenant_id: str, artifact_ref: dict,
               windows: list[dict]) -> dict:
        """本地检测器提示。失败或缺席都只是「没有提示」，不是「没有问题」。"""
        if self.detector_source is None:
            return {"available": False, "windows": {}}
        try:
            raw = self.detector_source(tenant_id, artifact_ref, windows)
        except (ServiceError, ConnectionError):
            return {"available": False, "windows": {}}
        by_window = {}
        for item in (raw or {}).get("windows") or []:
            index = item.get("window_index")
            if isinstance(index, int):
                by_window[index] = item
        return {"available": bool((raw or {}).get("available")),
                "windows": by_window}

    # ------------------------------------------------------------ 合法例外

    @staticmethod
    def _legal_exceptions(body: dict) -> list[dict]:
        """§7 的形状从命令体来（SQC-22：S17 的现有响应里没有它）。

        只保留 `declared_by.service_id == "S17"` 的条目。没有来源的例外一律
        丢弃并按普通检查判（E25-S6）——不信任无来源的例外。
        """
        kept = []
        for entry in body.get("legal_exceptions") or []:
            if not isinstance(entry, dict):
                continue
            declared_by = entry.get("declared_by")
            if not isinstance(declared_by, dict):
                continue
            if declared_by.get("service_id") != REF_SERVICE_EXPECTED:
                continue
            if not entry.get("exception_id"):
                continue
            kept.append(entry)
        return kept

    @staticmethod
    def _exception_for(check_id: str, window: dict,
                       exceptions: list[dict]) -> dict | None:
        for entry in exceptions:
            if check_id not in (entry.get("suppresses_check_ids") or []):
                continue
            applies = entry.get("applies_to") or {}
            ranges = applies.get("time_ranges") or []
            hit = not ranges
            for span in ranges:
                start = float(span.get("start", 0))
                end = float(span.get("end", 0))
                if start < window["end_seconds"] and end > window["start_seconds"]:
                    hit = True
                    break
            if hit:
                return entry
        return None

    # ------------------------------------------------------------ 评估

    def _evaluate_check(self, *, tenant_id, body, ctx, artifact, expected,
                        exceptions, hints, check, windows, shot_type) -> dict:
        check_id = check["check_id"]
        scoped = (windows if check["window_scope"] in ("window", "frame")
                  else [self._whole_shot(windows)])
        # 门控分支（不调模型）也要交代「这些窗口我看过了」。否则一次
        # 「ShotSpec 声明越轴」会在 coverage 里记成 0/3 未覆盖，第 2 步先弃权，
        # E24-S4/S5「decision 不受影响」永远达不成——把「本来就不必看」读成
        # 「没看」，与把「没看」读成「通过」是同一类错误的镜像。
        resolved = [w["window_index"] for w in scoped]

        if check_id == "VIS.CONTINUITY.AXIS_CROSSING":
            shot_spec = body.get("shot_spec") or {}
            if shot_spec.get("declared_axis_crossing") is True:
                # ShotSpec 声明了越轴意图：这不是缺陷，也不是「没查」。
                return self._result(check, "not_applicable", "policy_disabled",
                                    notes=("ShotSpec 声明本镜越轴，越轴检查不适用"),
                                    calls=[], resolved_windows=resolved)

        # 手部面积不足：按 §5.2 判 not_applicable，不打给模型（省一次付费调用）。
        if check_id in registry.NEEDS_HAND and hints["available"]:
            ratios = [w.get("hand_area_ratio") for w in hints["windows"].values()
                      if isinstance(w.get("hand_area_ratio"), (int, float))]
            if ratios and max(ratios) < registry.HAND_MIN_AREA_RATIO:
                return self._result(
                    check, "not_applicable", "no_such_content",
                    notes=(f"手部最大面积 {max(ratios):.3%} < 阈值 "
                           f"{registry.HAND_MIN_AREA_RATIO:.0%}，远景手不判"),
                    calls=[], resolved_windows=resolved)

        # 期望状态不可用：依赖它的检查记 failed，不是跳过。
        #
        # 判据是「读没读到」而不是「有没有给引用」：给了 ref 但 S17 不可达时，
        # `_resolve_expected_state` 保留 ref、把 available 置 False。若这里只看
        # ref，S17 掉线会被当成「有期望」，检查照常打给模型，最后按模型的话
        # 出结论——一个掉线的依赖就这么变成了绿灯（E25-S5）。
        #
        # 这里**不**登记 `resolved_windows`：没读到期望状态是「没看」，不是
        # 「看过了没有该内容」。它必须留在分母外，让 coverage 掉下来。
        if check_id in registry.NEEDS_EXPECTED_STATE and not expected["available"]:
            return self._result(
                check, "failed", "dependency_unavailable",
                notes=("命令未提供 expected_state_ref" if expected["ref"] is None
                       else "S17 expected_state 读取失败"),
                calls=[])

        defects, calls, failures = [], [], []
        disagreements, confidences, not_applicable = [], [], []
        for window in scoped:
            outcome = self._evaluate_window(
                tenant_id=tenant_id, body=body, ctx=ctx, artifact=artifact,
                expected=expected, hints=hints, check=check, window=window,
                shot_type=shot_type)
            calls.extend(outcome["calls"])
            if outcome.get("disagreement") is not None:
                disagreements.append(outcome["disagreement"])
            if outcome["status"] == "failed":
                failures.append(outcome)
                continue
            if outcome["status"] == "not_applicable":
                not_applicable.append(outcome)
                continue
            confidences.append(outcome["confidence"])
            defects.extend(outcome["defects"])

        if failures:
            first = failures[0]
            return {"check_id": check_id, "status": "failed",
                    "reason_code": first["reason_code"],
                    "defects": [], "confidence": 0.0, "calls": calls,
                    "disagreement": (max(disagreements) if disagreements
                                     else None),
                    "notes": first.get("notes"), "check": check}
        if not defects and not_applicable and len(not_applicable) == len(scoped):
            return {"check_id": check_id, "status": "not_applicable",
                    "reason_code": not_applicable[0]["reason_code"],
                    "defects": [], "confidence": 0.0, "calls": calls,
                    "disagreement": (max(disagreements) if disagreements
                                     else None),
                    "notes": not_applicable[0].get("notes"), "check": check}

        # §4 的连续窗口平滑：只有 promotable（登记为 minor）的检查才升档，
        # 且必须**连续 ≥2 个窗口**命中同一指纹。单窗口孤立命中维持原档——
        # 把它当「不算」才是禁止的；这里是「不升级」，缺陷照报（SQC-15）。
        visible_before = [d for d in defects if not d.get("_suppressed")]
        if check.get("_promotable") and visible_before:
            defects = _promote(defects, check)
        defects = self._suppress(defects, check, exceptions)
        # 「有没有被压制」要看**每一条**，不是只看第一条：同一 check 可能只有
        # 一个窗口落在例外时间段里，另一条观测仍然成立。只看 defects[0] 会让
        # 一条未压制的缺陷随着数组顺序消失。
        suppressed_any = any(d.get("_suppressed") for d in defects)
        visible = [d for d in defects if not d.get("_suppressed")]
        notes = None
        if suppressed_any:
            notes = "；".join(
                f"{d['check_id']} 观测被 {d['_suppressed']} 压制"
                for d in defects if d.get("_suppressed"))
        suppressed_observations = [
            {**_public(d), "exception_id": d["_suppressed"]}
            for d in defects if d.get("_suppressed")]
        if suppressed_any and not visible:
            # §2.4 行 73 给了两个选项：`no_such_content` 或新增
            # `legal_exception`。当前 qc-report.v2 的 reason_code 是闭集，
            # 发枚举外的值会让每一层校验器拒收整份报告——比一个略粗的理由码
            # 更糟。所以发 §2.4 明写允许的 `no_such_content`，并在 dimension
            # 里保留例外 id 与原始观测（SQC-2）。
            return {"check_id": check_id, "status": "not_applicable",
                    "reason_code": "no_such_content", "defects": [],
                    "confidence": 0.0, "calls": calls,
                    "disagreement": (max(disagreements) if disagreements
                                     else None),
                    "notes": notes, "check": check,
                    "suppressed_observations": suppressed_observations}
        return {"check_id": check_id, "status": "evaluated",
                "reason_code": "ok", "defects": visible,
                "confidence": (sum(confidences) / len(confidences)
                               if confidences else 0.0),
                "calls": calls,
                "disagreement": (max(disagreements) if disagreements else None),
                "notes": notes, "check": check,
                # 部分压制：被例外盖掉的观测也要留痕（B3 的「保留 dimension」）。
                "suppressed_observations": suppressed_observations}

    @staticmethod
    def _result(check, status: str, reason_code: str, *, notes=None,
                calls=None, resolved_windows=None) -> dict:
        """不调用模型的分支（不适用 / 上游缺失）也要产出完整形状。

        每个分支都必须给出 `calls`：空的 `calls` 和不存在的 `calls` 在下游
        没有区别，但在取证时区别很大——E25 要回答的是「这次调用发生了什么」。

        `resolved_windows` 是「这些窗口有定论了，只是不需要模型」。它和
        `calls` 分开：call 是一次付费调用的取证，resolved 是覆盖率的分母
        归属。把不调模型的不适用记进 calls 会伪造一次调用；不记进覆盖率又会
        让「不必看」变成「没看」。
        """
        return {"check_id": check["check_id"], "status": status,
                "reason_code": reason_code, "defects": [], "confidence": 0.0,
                "calls": calls or [], "disagreement": None, "notes": notes,
                "check": check,
                "resolved_windows": list(resolved_windows or [])}

    @staticmethod
    def _whole_shot(windows: list[dict]) -> dict:
        if not windows:
            return {"window_index": 0, "start_seconds": 0.0,
                    "end_seconds": 0.0}
        return {"window_index": 0, "start_seconds": 0.0,
                "end_seconds": windows[-1]["end_seconds"]}

    def _evaluate_window(self, *, tenant_id, body, ctx, artifact, expected,
                         hints, check, window, shot_type) -> dict:
        primary = self._invoke(tenant_id, body, ctx, artifact, expected, hints,
                              check, window, shot_type, role="primary")
        if primary["status"] == "failed":
            return primary

        if check["check_id"] not in registry.HIGH_RISK:
            # 非高风险：单评估器。模型说 not_applicable 也只是「这段没有该内容」，
            # 不是「我们没看」。
            return primary

        secondary = self._invoke(tenant_id, body, ctx, artifact, expected,
                                 hints, check, window, shot_type,
                                 role="secondary")
        if secondary["status"] == "failed":
            # E25-S2：主成功、副超时，**不得用单边结果冒充**。
            return {"status": "failed",
                    "reason_code": secondary["reason_code"],
                    "calls": primary["calls"] + secondary["calls"],
                    "notes": "secondary evaluator did not answer; a single "
                             "opinion cannot stand in for dual review"}
        if primary["status"] == "not_applicable" or \
                secondary["status"] == "not_applicable":
            return primary if primary["status"] == "not_applicable" else secondary

        disagreement = 1.0 - _jaccard(primary["defects"], secondary["defects"])
        merged = _merge(primary["defects"], secondary["defects"])
        return {"status": "evaluated", "reason_code": "ok",
                "defects": merged,
                "confidence": 0.5 * (primary["confidence"]
                                     + secondary["confidence"]),
                "calls": primary["calls"] + secondary["calls"],
                "disagreement": round(disagreement, 4)}

    # ------------------------------------------------------------ 单次调用

    def _invoke(self, tenant_id, body, ctx, artifact, expected, hints,
                check, window, shot_type, *, role: str) -> dict:
        started = time.monotonic()
        key = self._idempotency_key(artifact, check, window, role)
        command = self._generation_command(
            body, ctx, artifact, expected, check, window, role, key)
        call = {"check_id": check["check_id"], "window_index": window["window_index"],
                "model_snapshot": self._snapshot_label(role),
                "prompt_template_rev": PROMPT_TEMPLATE_REV,
                "input_frame_count": registry.frames_per_window(shot_type),
                "provider_job_id": "", "attempt_ref": "", "outcome": "error",
                "raw_response_ref": None, "cost_micro_units": None,
                "latency_ms": None}

        try:
            submitted = self._upstream(
                lambda: self.submit_source(
                    tenant_id, command, body["evaluator_route_decision_id"]),
                "S26_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status in (408, 504):
                call["outcome"] = "timeout"
                return {"status": "failed", "reason_code": "evaluator_timeout",
                        "calls": [call],
                        "notes": f"S26 submit timed out: {exc}"}
            if exc.status == 503:
                raise
            # 预算耗尽（F2）等：S26 明确拒绝，既不是通过也不是超时。
            call["outcome"] = "error"
            return {"status": "failed", "reason_code": "evaluator_error",
                    "calls": [call],
                    "notes": f"S26 rejected the job: {exc.code} {exc}"}

        job_id = (submitted or {}).get("provider_job_id") or \
            (submitted or {}).get("operation_id") or ""
        call["provider_job_id"] = job_id
        call["attempt_ref"] = str((submitted or {}).get("attempt_id") or
                                  (submitted or {}).get("operation_id") or "")

        job = None
        for _ in range(POLL_MAX):
            try:
                job = self._upstream(
                    lambda: self.job_source(tenant_id, job_id),
                    "S26_UNAVAILABLE")
            except ServiceError as exc:
                if exc.status in (408, 504):
                    call["outcome"] = "timeout"
                    return {"status": "failed",
                            "reason_code": "evaluator_timeout",
                            "calls": [call],
                            "notes": f"S26 poll timed out: {exc}"}
                call["outcome"] = "error"
                return {"status": "failed", "reason_code": "evaluator_error",
                        "calls": [call], "notes": f"S26 poll failed: {exc}"}
            state = (job or {}).get("state")
            if state in S26_SUCCESS_STATES:
                break
            if state in S26_FROZEN_STATES:
                # SUBMISSION_UNKNOWN / RECONCILING：**不重提**。我们不知道
                # 供应商是否收下了这次付费调用，所以这一项没有结论。
                call["outcome"] = "error"
                return {"status": "failed", "reason_code": "evaluator_error",
                        "calls": [call],
                        "notes": f"S26 job is frozen in {state}; S36 will not "
                                 f"resubmit a possibly-paid job"}
            if state in S26_FAILURE_STATES:
                call["outcome"] = "error"
                return {"status": "failed", "reason_code": "evaluator_error",
                        "calls": [call],
                        "notes": f"S26 job ended in {state}"}
            time.sleep(POLL_INTERVAL_SECONDS)
        else:
            call["outcome"] = "timeout"
            return {"status": "failed", "reason_code": "evaluator_timeout",
                    "calls": [call],
                    "notes": "S26 job did not reach a terminal state"}

        call["latency_ms"] = int((time.monotonic() - started) * 1000)
        call["cost_micro_units"] = (job or {}).get("cost_micro_units")
        output = (job or {}).get("output") or {}
        result = output.get("result")
        if result is None:
            # SQC-17：仓库里没有「读 artifact 内容」的接口；真实接线前只能
            # 接受 job 输出里内联的 result。拿不到就是没读到，不是通过。
            call["outcome"] = "error"
            return {"status": "failed", "reason_code": "evaluator_error",
                    "calls": [call],
                    "notes": "S26 job output carried no readable result"}
        parsed, error = self._parse_response(result)
        if parsed is None:
            call["outcome"] = "schema_error"
            return {"status": "failed", "reason_code": "evaluator_error",
                    "calls": [call], "notes": error}

        call["outcome"] = "ok"
        if parsed["verdict"] == "not_applicable":
            reason = parsed.get("reason_code") or "no_such_content"
            if reason not in VLM_NOT_APPLICABLE_REASONS:
                reason = "no_such_content"
            return {"status": "not_applicable", "reason_code": reason,
                    "calls": [call],
                    "notes": "模型判本窗口没有该内容"}
        if parsed["verdict"] == "pass":
            return {"status": "evaluated", "reason_code": "ok", "defects": [],
                    "confidence": float(parsed["confidence"]),
                    "calls": [call], "disagreement": None}
        return {"status": "evaluated", "reason_code": "ok",
                "defects": [self._defect(check, window, parsed, artifact,
                                         expected)],
                "confidence": float(parsed["confidence"]),
                "calls": [call], "disagreement": None}

    def _parse_response(self, result) -> tuple[dict | None, str]:
        if not isinstance(result, dict):
            return None, "model result is not an object"
        keys = set(result)
        if keys - set(VLM_REQUIRED) - set(VLM_OPTIONAL):
            return None, ("model result has unknown keys: "
                          + ", ".join(sorted(keys - set(VLM_REQUIRED)
                                             - set(VLM_OPTIONAL))))
        missing = [k for k in VLM_REQUIRED if k not in result]
        if missing:
            return None, "model result missing: " + ", ".join(missing)
        if result["verdict"] not in VLM_VERDICTS:
            return None, f"unknown verdict: {result['verdict']}"
        if result["severity"] not in SEVERITIES:
            return None, f"unknown severity: {result['severity']}"
        try:
            confidence = float(result["confidence"])
        except (TypeError, ValueError):
            return None, "confidence is not a number"
        if not 0.0 <= confidence <= 1.0:
            return None, "confidence out of range"
        if result["verdict"] == "defect":
            if not str(result.get("observed") or "").strip():
                return None, "defect without an observation"
            span = result.get("time_range")
            if not isinstance(span, dict) or \
                    not isinstance(span.get("start_seconds"), (int, float)) or \
                    not isinstance(span.get("end_seconds"), (int, float)):
                return None, "defect without a time_range"
            if span["end_seconds"] <= span["start_seconds"]:
                return None, "time_range.end must be greater than start"
        return result, ""

    def _defect(self, check, window, parsed, artifact, expected) -> dict:
        span = parsed.get("time_range") or {
            "start_seconds": window["start_seconds"],
            "end_seconds": window["end_seconds"]}
        start = max(0.0, min(float(span["start_seconds"]),
                             artifact["duration_seconds"]))
        end = max(start + 0.001, min(float(span["end_seconds"]),
                                     artifact["duration_seconds"]))
        severity = registry.severity_after(
            check["severity_default"], parsed["severity"], check["severity_max"])
        fingerprint = defect_fingerprint(
            check["check_id"], check["defect_type"],
            parsed.get("character_id"), severity, start)
        actions = [a for a in (parsed.get("recommended_actions") or [])
                   if a in check["allowed_actions"]]
        if not actions:
            actions = [check["default_action"]]
        evidence = [str(x) for x in (parsed.get("evidence_artifact_ids") or [])]
        if not evidence:
            # `evidence_artifact_ids` 不能为空。这里挂回**真实的源素材 id** +
            # 窗口号；不编造一个不存在的裁剪帧引用。合成证据需要 S28 落盘，
            # 那一步还没接（SQC-26）。
            evidence = [f"{artifact['id']}.w{window['window_index']}"]
        source = self._expected_source_for(check, expected)
        if source["service_id"] not in (REF_SERVICE_SHOT, REF_SERVICE_BINDING,
                                        REF_SERVICE_EXPECTED):
            # B5：S36 不得直接消费 S16。这条断言在构造处就挡住。
            raise ServiceError(500, "REPORT_VALIDATION_FAILED",
                               "S36 may not cite S16 as expected_source")
        return {
            "defect_id": defect_id_for(fingerprint, {
                "start_seconds": start, "end_seconds": end}),
            "defect_fingerprint": fingerprint,
            "check_id": check["check_id"],
            "type": check["defect_type"],
            "severity": severity,
            "scope": SCOPE_MAP[check["window_scope"]],
            "time_range": {"start_seconds": round(start, 3),
                           "end_seconds": round(end, 3)},
            "region_bbox": parsed.get("region_bbox"),
            "character_id": parsed.get("character_id"),
            "expected": str(parsed.get("expected") or
                            f"符合 {source['service_id']} 的期望"),
            "expected_source": source,
            "observed": str(parsed.get("observed")),
            "measured": parsed.get("measured"),
            "confidence": float(parsed["confidence"]),
            "detector": "model",
            "evidence_artifact_ids": evidence,
            "recommended_actions": actions,
            # 内核私有：连续窗口平滑要看窗口序号（`_public` 会去掉它）。
            "_window_index": int(window["window_index"]),
        }

    @staticmethod
    def _expected_source_for(check, expected) -> dict:
        service_id = check["_expected_source"]
        if service_id == REF_SERVICE_EXPECTED:
            ref = expected.get("ref") or {}
            return {"service_id": service_id, "ref_id": ref.get("id", ""),
                    "ref_revision": int(ref.get("revision") or 1)}
        # S06 / S10 的 ref 由调用方给出，`_defect` 调用前已校验过来源服务。
        return {"service_id": service_id, "ref_id": "", "ref_revision": 1}

    # ------------------------------------------------------------ 证据

    def _suppress(self, defects, check, exceptions) -> list[dict]:
        if not defects or not exceptions:
            return defects
        for index, defect in enumerate(defects):
            span = defect["time_range"]
            entry = self._exception_for(check["check_id"], {
                "start_seconds": span["start_seconds"],
                "end_seconds": span["end_seconds"]}, exceptions)
            if entry is not None:
                defects[index] = {**defect,
                                  "_suppressed": str(entry["exception_id"])}
        return defects

    def _dimensions(self, results, shot_type_defaulted, artifact, hints) -> list:
        dims = []
        if shot_type_defaulted:
            dims.append({
                "name": "sampling_shot_type_defaulted", "score": None,
                "reason": "命令未内联 shot_spec.shot_type（S36 的 depends_on "
                          "没有 S06，见 SQC-11），采样按 dialogue 策略执行"})
        if not artifact["detail"].get("video_codec"):
            dims.append({
                "name": "media_facts_inferred", "score": None,
                "reason": "S28 不提供 codec/pix_fmt/color_range/is_vfr/"
                          "frame_count（SQC-12）；这些值由容器信息推导，"
                          "不得用于技术层判定"})
        if not hints["available"]:
            dims.append({
                "name": "local_detector_unavailable", "score": None,
                "reason": "没有本地检测器提示；手部/人脸门控退化为由模型判断，"
                          "模型不答的检查计入 abstain"})
        for result in results:
            if result.get("disagreement") is not None:
                dims.append({
                    "name": f"model_disagreement.{result['check_id']}",
                    "score": float(result["disagreement"]),
                    "reason": "主/副评估器的缺陷集合 Jaccard 距离（SQC-19）"})
            for observation in result.get("suppressed_observations") or []:
                dims.append({
                    "name": f"suppressed_observation.{result['check_id']}",
                    "score": float(observation["confidence"]),
                    "reason": "合法例外压制了这条观测，但它仍然为真，保留可审计"
                              f"（{observation.get('observed')}）"})
        return dims

    @staticmethod
    def _planned_windows(selected: list[str], windows: list[dict]) -> list[int]:
        """采样计划的分母，按窗口序号给出。

        只选了 shot/track 范围的 check 时，一次整镜调用就覆盖完了，分母是 1
        （用整镜的窗口号 0 代表）；若仍按分镜窗口数当分母，任何只查服装/体型
        的请求都会得到 1/3 的覆盖率并弃权——那是把「没按窗口切」误读成
        「只看了三分之一」。混选时按窗口算：只要有一个窗口级 check，就必须
        逐窗口有定论，整镜 check 的结论不替它担保。
        """
        needs_windows = any(
            registry.BY_ID[c]["window_scope"] in ("window", "frame")
            for c in selected)
        if not needs_windows:
            return [0]
        return [int(w["window_index"]) for w in windows]

    def _coverage(self, artifact, plan: list[int], results) -> dict:
        """覆盖率回答的是「我们看了多少」，按**窗口**算，逐 check 取交集。

        一个窗口算完成，必须**每个要看它的 check** 都交代过：
        - `ok` 的调用（模型答了）；
        - 门控给出的 `resolved_windows`（本窗口没有该内容 / ShotSpec 声明
          不适用，不必调用模型也算看过）。

        只数「有任意一次 ok」会把「主成功、副超时」的窗口记成已完成——那个
        窗口只被看了一半。只数调用则会把「本来就不必看」记成「没看」，让
        E24-S4/S5 那种合法不适用拖垮覆盖率。两个方向都不是「看了」。
        """
        open_windows = set(plan)
        frames = 0
        for result in results:
            calls = result.get("calls") or []
            frames += sum(int(c["input_frame_count"]) for c in calls)
            scope = result["check"]["window_scope"]
            if scope not in ("window", "frame"):
                # 整镜/整轨 check 一次调用覆盖全片。它没结论就等于全片没结论；
                # 它有了结论也不替窗口级 check 担保（两条线各算各的）。
                if result["status"] == "failed":
                    open_windows = set()
                continue
            ok = {c["window_index"] for c in calls if c["outcome"] == "ok"}
            bad = {c["window_index"] for c in calls if c["outcome"] != "ok"}
            if result["status"] == "failed" and not calls:
                # 根本没有调用（依赖不可用等）：这些窗口一次都没看过。
                bad = set(plan)
            # 门控分支（不调模型）的「看过」只在不适用/已评估时成立；失败
            # 分支不带 resolved_windows，所以这里不会把失败洗成覆盖。
            resolved = (set(result.get("resolved_windows") or []) | ok) - bad
            open_windows &= resolved

        planned = len(plan)
        evaluated_windows = len(open_windows)
        ratio = (evaluated_windows / planned) if planned else 0.0
        return {
            "media_duration_seconds": artifact["duration_seconds"],
            # 每次调用都重采该窗口；帧数是逐次累加，不是「窗数 × 某一种帧数」。
            "frames_sampled": frames,
            "temporal_windows_evaluated": evaluated_windows,
            "planned_windows": planned,
            "coverage_ratio": round(min(1.0, max(0.0, ratio)), 4),
            "required_coverage_min": registry.MIN_COVERAGE_RATIO,
            "audio_analyzed": False,
            "audio_analyzed_seconds": 0.0,
            "sampling_policy_version": registry.SAMPLING_VERSION,
        }

    # ------------------------------------------------------------ 决策

    def decide(self, *, manifest, defects, coverage, results) -> tuple:
        """S35 §2.7。顺序不能换：**第 3 步在评分之前**。

        这是「未知不能当 pass」在代码层面的落点——评估器超时不是低分，是
        没有结论，不能进入分数比较。
        """
        # 1. 清单完整性：选中的每个 check 恰好出现一次。
        missing = [entry["check_id"] for entry in manifest
                   if entry["status"] == "not_evaluated"]
        if missing:
            return ("abstain", "check manifest is incomplete",
                    self._abstain("incomplete_coverage", missing,
                                  "some selected checks were never scheduled"),
                    0.0)

        # 2. 采样覆盖不足。
        #
        # 理由码取「造成缺口的那条具体原因」而不是写死 `incomplete_coverage`：
        # 一次超时**必然**同时让覆盖率掉下去（E24-S6 要求 coverage<1，
        # E25-S1 要求 reason=evaluator_timeout）。照抄成 coverage 的理由会把
        # 更具体的失败吞掉，调用方就无法区分「模型超时」与「只采了六成窗口」。
        # 判定仍是 abstain、failed 仍非空，同 S35 的 S35-19。
        if coverage["coverage_ratio"] < registry.MIN_COVERAGE_RATIO:
            failed = [e["check_id"] for e in manifest
                      if e["status"] in ("failed", "not_evaluated")] or \
                [e["check_id"] for e in manifest] or ["VIS.IDENTITY.FACE_DRIFT"]
            code = self._reason_for(manifest, failed)
            return ("abstain",
                    f"coverage {coverage['coverage_ratio']:.2f} is below "
                    f"{registry.MIN_COVERAGE_RATIO:.2f}",
                    self._abstain(
                        ABSTAIN_FOR_REASON.get(code, "incomplete_coverage"),
                        failed,
                        "not enough temporal windows were evaluated; a partial "
                        "look cannot become a pass"),
                    0.0)

        # 3. 有 failed / 未评估 / 无正当理由的 not_applicable → 弃权。
        failed = [r for r in results
                  if r["status"] == "failed"
                  or (r["status"] == "not_applicable"
                      and r["reason_code"] not in
                      registry.JUSTIFIED_NOT_APPLICABLE)]
        if failed:
            reasons = {r["reason_code"] for r in failed}
            if "evaluator_timeout" in reasons:
                code = "evaluator_timeout"
            elif "dependency_unavailable" in reasons:
                code = "dependency_unavailable"
            else:
                code = "evaluator_error"
            abstain_code = {
                "evaluator_timeout": "evaluator_timeout",
                "dependency_unavailable": "dependency_stale",
            }.get(code, "evaluator_error")
            return ("abstain", "one or more required checks produced no verdict",
                    self._abstain(abstain_code, [r["check_id"] for r in failed],
                                  "a check that could not run is not a pass"),
                    0.0)

        disagreement = max((r["disagreement"] or 0.0) for r in results) \
            if results else 0.0
        # 置信度只对「真的下过结论」的 check 取均值。到这里已经没有 failed，
        # 剩下的不是 evaluated 就是**有正当理由的** not_applicable。
        # 若一个 evaluated 都没有（例如整镜无脸、手部面积不足），把 0.0 当
        # 「不自信」是错的——那是「没有可自信的对象」，与 S35
        # `compute_confidence` 的规则一致：not_applicable 不拉低置信度。
        evaluated = [r for r in results if r["status"] == "evaluated"]
        confidence = (sum(r["confidence"] for r in evaluated) / len(evaluated)
                      if evaluated else 1.0)

        # 4. 硬失败。
        blockers = [d for d in defects if d["severity"] == "blocker"]
        if blockers:
            return ("repair",
                    f"{len(blockers)} blocker defect(s) require regeneration",
                    None, confidence)

        # 5. 分级。
        majors = [d for d in defects if d["severity"] == "major"]
        if majors:
            if any(d["confidence"] < registry.TAU_LOW for d in defects):
                return ("human_review",
                        "a major defect has confidence below tau_low; it is "
                        "not safe to repair automatically", None, confidence)
            allowed = self._allowed_actions()
            if all(set(d["recommended_actions"]) <= allowed for d in defects):
                return ("repair", f"{len(majors)} major defect(s)", None,
                        confidence)
            return ("human_review",
                    "a major defect has no automatically executable action",
                    None, confidence)

        # 6. 模型冲突。
        if disagreement > registry.TAU_DISAGREEMENT:
            return ("human_review",
                    f"evaluators disagree ({disagreement:.2f} Jaccard distance); "
                    f"both conclusions are kept, no averaging", None, confidence)

        # 7. 整体置信度不足。契约要求 `failed_check_ids` 非空（SQC-25），
        #    所以这里带上置信度最低的那些 check。
        if confidence < registry.TAU_ABSTAIN:
            weak = [r["check_id"] for r in results
                    if r["status"] == "evaluated"]
            return ("abstain", "overall confidence is too low",
                    self._abstain("low_confidence", weak or ["VIS.IDENTITY.FACE_DRIFT"],
                                  "the evaluators answered, but not confidently "
                                  "enough to accept"), confidence)

        # 8. 只剩 minor / info。
        return ("pass", "all required checks were evaluated and no blocker or "
                        "major defect was found", None, confidence)

    @staticmethod
    def _allowed_actions() -> set:
        allowed = set()
        for entry in registry.CHECKS:
            allowed.update(entry["allowed_actions"])
        return allowed

    @staticmethod
    def _reason_for(manifest, failed) -> str:
        """§2.7 `reason_for(failed)`：优先取评估器自身的原因，而不是「覆盖不足」。

        只用于第 2 步的理由码。顺序与 S35 的同名函数一致：超时 > 错误 >
        依赖不可用 > 资产不可用 > 未采样 > 策略禁用 > 覆盖不足。
        返回值是 **manifest 的理由码**，翻译成 abstain 的理由码由
        `ABSTAIN_FOR_REASON` 做（两套枚举不同，见 qc-report.v2 的两个闭集）。
        """
        by_check = {entry["check_id"]: entry["reason_code"]
                    for entry in manifest}
        codes = {by_check.get(check_id) for check_id in failed}
        for code in ("evaluator_timeout", "evaluator_error",
                     "dependency_unavailable", "asset_unavailable",
                     "sampling_not_selected", "policy_disabled"):
            if code in codes:
                return code
        return "incomplete_coverage"

    @staticmethod
    def _abstain(reason_code: str, check_ids: list[str], detail: str) -> dict:
        # 契约：failed_check_ids minItems 1。空列表会被 S39 的 validator 拒收。
        ids = list(dict.fromkeys([c for c in check_ids if c]))
        return {"reason_code": reason_code,
                "failed_check_ids": ids or ["VIS.IDENTITY.FACE_DRIFT"],
                "detail": detail}

    # ------------------------------------------------------------ 落库

    def _validate_report(self, payload, selected, artifact) -> None:
        """落库前的语义闸门。schema 校验由测试对契约文件跑；这里查的是
        schema 表达不了的等式：
        """
        expected_id = "qc_" + base32_lower(derived_key(
            payload["context"]["tenant_id"], artifact["sha256"],
            payload["evaluator"]["model_snapshot"], payload["policy_version"],
            payload["sampling_policy_version"],
            payload["repair_context"]["attempt_slot_id"]
            if payload["repair_context"] else None))[:26]
        if payload["report_id"] != expected_id:
            raise ServiceError(500, "REPORT_VALIDATION_FAILED",
                               "report_id does not match its derived key")
        ids = [entry["check_id"] for entry in payload["check_manifest"]]
        if sorted(ids) != sorted(selected):
            raise ServiceError(500, "REPORT_VALIDATION_FAILED",
                               "check_manifest does not match check_selection")
        if len(ids) != len(set(ids)):
            raise ServiceError(500, "REPORT_VALIDATION_FAILED",
                               "a check appears twice in check_manifest")
        coverage = payload["coverage"]
        if coverage["planned_windows"]:
            ratio = coverage["temporal_windows_evaluated"] / \
                coverage["planned_windows"]
            # 两侧用同一把尺子（4 位小数）。拿未舍入的 2/3 去比 0.6667 会差
            # 3e-5，把一份正确的报告判成「分母对不上」。
            if round(min(1.0, ratio), 4) != coverage["coverage_ratio"]:
                raise ServiceError(500, "REPORT_VALIDATION_FAILED",
                                   "coverage_ratio disagrees with its numerator")
        for defect in payload["defects"]:
            if defect["time_range"]["end_seconds"] <= \
                    defect["time_range"]["start_seconds"]:
                raise ServiceError(500, "REPORT_VALIDATION_FAILED",
                                   "defect time_range is empty")
            if defect["check_id"] not in selected:
                raise ServiceError(500, "REPORT_VALIDATION_FAILED",
                                   "defect cites an unselected check")
        if payload["decision"] == "abstain" and not payload["abstain"]:
            raise ServiceError(500, "REPORT_VALIDATION_FAILED",
                               "abstain decision without an abstain record")
        if payload["decision"] == "pass" and payload["abstain"]:
            raise ServiceError(500, "REPORT_VALIDATION_FAILED",
                               "pass decision carries an abstain record")

    def _next_generation(self, tenant_id: str, report_id: str) -> tuple:
        row = self.conn.execute(
            "SELECT MAX(report_generation) AS gen FROM report"
            " WHERE tenant_id=? AND report_id=?",
            (tenant_id, report_id)).fetchone()
        gen = int(_row(row)["gen"] or 0)
        generation = gen + 1
        supersedes = ({"report_id": report_id, "report_generation": gen}
                      if gen else None)
        return generation, supersedes

    def _persist(self, tenant_id: str, command_id: str, payload: dict,
                 calls: list[dict], ctx: dict, *, expected_ref,
                 disagreement: float) -> None:
        now = _now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO report (report_id, report_generation, tenant_id,"
                " series_id, episode_id, branch_id, shot_id, shot_lineage_id,"
                " logical_node_id, node_epoch, manifest_id, manifest_revision,"
                " manifest_content_hash, run_id, execution_fence,"
                " artifact_sha256, shot_spec_revision, expected_state_ref,"
                " evaluator_version, policy_version, sampling_version,"
                " decision, coverage_ratio, attempt_slot_id, payload,"
                " content_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (payload["report_id"], payload["report_generation"], tenant_id,
                 payload["context"]["series_id"],
                 payload["context"]["episode_id"],
                 payload["context"]["branch_id"],
                 payload["shot_ref"]["shot_id"],
                 payload["context"]["shot_lineage_id"],
                 payload["shot_ref"]["logical_node_id"],
                 int(payload["shot_ref"]["node_epoch"]),
                 payload["context"]["manifest_ref"]["id"],
                 int(payload["context"]["manifest_ref"]["revision"]),
                 payload["context"]["manifest_ref"]["content_hash"],
                 payload["context"]["run_id"],
                 int(payload["context"]["execution_fence"]),
                 payload["subject"]["artifact_sha256"],
                 int(payload["shot_ref"]["shot_revision"]),
                 _dumps(expected_ref),
                 payload["evaluator"]["version"],
                 payload["policy_version"],
                 payload["sampling_policy_version"],
                 payload["decision"],
                 payload["coverage"]["coverage_ratio"],
                 payload["repair_context"]["attempt_slot_id"]
                 if payload["repair_context"] else None,
                 _dumps(payload), payload["report_content_hash"], now))
            for call in calls:
                self.conn.execute(
                    "INSERT INTO evaluator_call (call_id, report_id,"
                    " report_generation, tenant_id, check_id, window_index,"
                    " provider_job_id, attempt_ref, model_snapshot,"
                    " prompt_template_rev, input_frame_count, latency_ms,"
                    " outcome, raw_response_ref, cost_micro_units, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_new_id("call"), payload["report_id"],
                     payload["report_generation"], tenant_id, call["check_id"],
                     int(call["window_index"]), call["provider_job_id"],
                     call["attempt_ref"], call["model_snapshot"],
                     int(call["prompt_template_rev"]),
                     int(call["input_frame_count"]), call["latency_ms"],
                     call["outcome"], _dumps(call["raw_response_ref"]),
                     call["cost_micro_units"], now))
            self._emit(payload, calls, ctx, command_id, expected_ref,
                       disagreement)

    def _emit(self, payload: dict, calls: list[dict], ctx: dict,
              command_id: str, expected_ref, disagreement: float) -> None:
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_TYPE,
            subject=f"qc_report/{payload['report_id']}",
            tenant_id=payload["context"]["tenant_id"],
            home_cell=payload["context"]["home_cell"],
            aggregate_id=payload["report_id"],
            sequence=int(payload["report_generation"]),
            correlation_id=str(ctx.get("correlation_id") or command_id),
            causation_id=command_id,
            data={
                "schema_version": SCHEMA_VERSION,
                "report_id": payload["report_id"],
                "report_generation": payload["report_generation"],
                "qc_layer": QC_LAYER,
                "artifact_sha256": payload["subject"]["artifact_sha256"],
                "manifest_ref": payload["context"]["manifest_ref"],
                "shot_id": payload["shot_ref"]["shot_id"],
                "shot_lineage_id": payload["context"]["shot_lineage_id"],
                "node_epoch": int(payload["shot_ref"]["node_epoch"]),
                "run_id": payload["context"]["run_id"],
                "execution_fence": int(ctx["execution_fence"]),
                "decision": payload["decision"],
                "decision_reason": payload["decision_reason"],
                "coverage_ratio": payload["coverage"]["coverage_ratio"],
                "attempt_slot_id": (payload["repair_context"] or {}).get(
                    "attempt_slot_id"),
                "includes_continuity_defects": any(
                    d["type"] == "continuity" for d in payload["defects"]),
                "expected_state_consumed": (None if not expected_ref else
                                            {"service_id": REF_SERVICE_EXPECTED,
                                             "ref_revision": int(expected_ref["revision"])}),
                "defect_count": len(payload["defects"]),
                "blocker_count": sum(1 for d in payload["defects"]
                                     if d["severity"] == "blocker"),
                "model_disagreement": round(float(disagreement), 4),
                "evaluator_calls": len(calls),
                "cost_micro_units": sum(int(c["cost_micro_units"] or 0)
                                        for c in calls),
            },
            data_ref={"service_id": SOURCE, "id": payload["report_id"],
                      "revision": payload["report_generation"],
                      "content_hash": payload["report_content_hash"]},
            schema_version=SCHEMA_VERSION))

    # ------------------------------------------------------------ 查询

    def get_report(self, tenant_id: str, report_id: str,
                   include: str | None = None) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM report WHERE tenant_id=? AND report_id=?"
            " ORDER BY report_generation DESC LIMIT 1",
            (tenant_id, report_id)).fetchone())
        if row is None:
            # 跨租户 → 404，不确认存在。
            raise ServiceError(404, "REPORT_NOT_FOUND",
                               f"no semantic report {report_id}")
        out = {"report": _loads(row["payload"], {})}
        if include and "evaluator_calls" in include:
            calls = [dict(r) for r in self.conn.execute(
                "SELECT * FROM evaluator_call WHERE tenant_id=? AND report_id=?"
                " AND report_generation=? ORDER BY check_id, window_index,"
                " model_snapshot",
                (tenant_id, report_id, int(row["report_generation"]))).fetchall()]
            out["evaluator_calls"] = calls
        return out

    # ------------------------------------------------------------ 命令拼装

    @staticmethod
    def _artifact_ref(ref: dict) -> dict:
        return {"service_id": REF_SERVICE_ARTIFACT, "id": ref["id"],
                "revision": int(ref["revision"]),
                "content_hash": ref["content_hash"]}

    def _idempotency_key(self, artifact, check, window, role) -> str:
        # §3.2 的公式 + 评估器角色。少了角色，双评估的第二条会命中 S26 的
        # 同一幂等键、拿回同一个 job——所谓「第二意见」其实是同一个答案（SQC-24）。
        return (f"qc:s36:{artifact['sha256']}:{check['check_id']}:"
                f"{window['window_index']}:{EVALUATOR_VERSION}:{role}")

    @staticmethod
    def _snapshot_label(role: str) -> str:
        # 模型快照未知时不编造模型名，用角色名占位并如实标出。
        return f"s36:{role}:unresolved"

    def _generation_command(self, body, ctx, artifact, expected, check, window,
                            role, key) -> dict:
        question = (
            f"检查 {check['check_id']}（{check['defect_type']}），时间窗 "
            f"{window['start_seconds']:.3f}-{window['end_seconds']:.3f}s。"
            f"判断本窗口是否存在该缺陷，只输出 quality/vlm-response.v2 JSON。")
        context_pack = expected.get("ref") or body["shot_spec_ref"]
        return {
            "schema_version": SCHEMA_VERSION,
            "context": ctx,
            "attempt_id": f"{check['check_id']}:{window['window_index']}:{role}",
            "variation_nonce": f"{role}-{uuid.uuid4().hex[:12]}",
            "idempotency_key": key,
            "modality": "understanding",
            # SQC-8：契约的 understanding 分支只有这 4 个字段，§3.2 的概念
            # 字段（model_role/images/prompt_template_ref/response_schema_ref）
            # 不能原样上送。
            "input": {
                "media_refs": [self._artifact_ref(body["artifact"])],
                "question": question,
                "context_pack_ref": {
                    "service_id": context_pack["service_id"],
                    "id": context_pack["id"],
                    "revision": int(context_pack["revision"]),
                    "content_hash": context_pack["content_hash"],
                },
                "output_schema_id": "quality/vlm-response.v2",
            },
            "route_policy_ref": ctx["policy_ref"],
            "template_ref": None,
            # docs/12：质检有自己的预算 scope，不与生产生成混用。
            "budget_scope_id": f"budget_qc_{ctx['episode_id']}",
            "deadline": body["deadline"],
        }

    @staticmethod
    def _report_context(ctx: dict, body: dict) -> dict:
        out = {
            "tenant_id": ctx["tenant_id"],
            "project_id": ctx["project_id"],
            "series_id": ctx["series_id"],
            "branch_id": ctx["branch_id"],
            "episode_id": ctx["episode_id"],
            "home_cell": ctx["home_cell"],
            "manifest_ref": ctx["manifest_ref"],
            "run_id": body["run_id"],
            "execution_fence": int(ctx["execution_fence"]),
            "shot_lineage_id": body["shot_lineage_id"],
            "policy_ref": ctx["policy_ref"],
            "trace_id": ctx["trace_id"],
        }
        if ctx.get("memory_checkpoint") is not None:
            out["memory_checkpoint"] = ctx["memory_checkpoint"]
        return out

    @staticmethod
    def _manifest_entry(result: dict) -> dict:
        entry = {"check_id": result["check_id"], "status": result["status"],
                 "reason_code": result["reason_code"],
                 "severity_ceiling": result["check"]["severity_max"]}
        return entry


# ------------------------------------------------------------ 纯函数：集合

def _jaccard(left: list[dict], right: list[dict]) -> float:
    """(type, severity_bucket, time_bucket) 的三元组 Jaccard。

    只比三元组：模型对同一缺陷的措辞和分数都会抖，把 `observed` 放进来
    会让「同一个结论」永远算不一致。
    """
    def keys(items):
        return {(d["type"],
                 "blocker" if d["severity"] == "blocker" else "non_blocker",
                 _rounded_bucket(d["time_range"]["start_seconds"]))
                for d in items}
    a, b = keys(left), keys(right)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _merge(left: list[dict], right: list[dict]) -> list[dict]:
    """一致时合并两边的缺陷：同指纹取更重的一档、置信度取均值。"""
    merged: dict[str, dict] = {}
    for defect in list(left) + list(right):
        key = defect["defect_fingerprint"]
        if key not in merged:
            merged[key] = dict(defect)
            continue
        current = merged[key]
        current["severity"] = registry.severity_after(
            current["severity"], defect["severity"], current["severity"])
        current["confidence"] = round(
            0.5 * (current["confidence"] + defect["confidence"]), 4)
    return list(merged.values())


def _promote(defects: list[dict], check: dict) -> list[dict]:
    """§4：同一指纹在相邻窗口连续命中 ≥ PROMOTION_WINDOWS 次才升一档。

    只对登记为 `_promotable`（都是 minor）的检查生效；**升的是严重度**，
    封顶在 `severity_max`。blocker 永远不会经过这里——registry 模块头第 3 条
    解释了为什么：「孤立命中所以不算」就是把未知读成通过。

    `escalation_rule.escalate_to` 是**动作**枚举（如 `change_spec`），不是
    严重度。把它当 proposed severity 传给 `severity_after` 会在第一次触发
    升级时抛 `ValueError: unknown severity: change_spec`——即「连续命中」这条
    平滑规则一旦真的生效就让整份质检 500。这个字段留给 S39 读（它回答「跳到
    族链哪一步」），严重度的上提只按 PROMOTION_RAISES_TO。

    「连续」按窗口序号判断，不看时间桶：一个 0.5s 桶里可能同时落着两个窗口
    的起点，用桶数会把一次长命中算成两次。
    """
    rule = check.get("escalation_rule") or {}
    threshold = int(rule.get("threshold") or registry.PROMOTION_WINDOWS)
    target = registry.PROMOTION_RAISES_TO

    runs: dict[str, list[int]] = {}
    for defect in defects:
        if defect.get("_suppressed"):
            continue
        runs.setdefault(defect["defect_fingerprint"], []).append(
            int(defect.get("_window_index", 0)))

    promoted = []
    for defect in defects:
        indices = sorted(runs.get(defect["defect_fingerprint"], []))
        # 最长连续段：indices 已排序且每个窗口最多贡献一条（同 check 同一指纹），
        # 所以相邻差 1 即连续。
        best = longest = 1 if indices else 0
        for a, b in zip(indices, indices[1:]):
            longest = longest + 1 if b == a + 1 else 1
            best = max(best, longest)
        if best >= threshold and not defect.get("_suppressed"):
            promoted.append({**defect, "severity": registry.severity_after(
                defect["severity"], target, check["severity_max"]),
                "_promoted": True})
        else:
            promoted.append(defect)
    return promoted


def _public(value):
    """去掉内核私有键（`_` 前缀）。

    `qc-report.v2` 是 `additionalProperties:false`：一个 `_window_index`
    漏进 payload 就会让整份报告被下游拒收。所有私有键只在内核里活。
    """
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items()
                if not k.startswith("_")}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value
