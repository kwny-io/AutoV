"""S35 内核。方案：`plans/S35-technical-qc.md`。

一句话边界：**S35 测量并报告，不修复。**

它没有"执行修复"的表、没有 artifact 写入口、没有模型调用。`defects[].recommended_actions`
是给 S39 的**建议**（§2.2 末表），不是本服务的待办。一旦 S35 开始按自己的
建议去动素材，它就变成了 artifact 内容的第二权威（AGENTS.md）。

七段结构（§3.2 的流水线，去掉沙箱内部）：

1. 校验命令体：租户、`run_id` / `shot_lineage_id` 等坐标（缺一个就 422，
   不补默认值）。
2. 读 S02 profile（阈值 / 目标 / 白名单）。读不到 → 503，**不套内置阈值**：
   内置阈值可能比租户合同宽松，那等于把"未达交付标准"测成"合格"。
3. 取 S28 artifact 元数据 + 探针文档。
   - S28 明确说这个 artifact 不存在（404/409）→ 422，命令不产报告（§6.1）。
   - S28 超时 / 5xx → 仍产报告，但 decided 为 `abstain`（§10.4），因为
     "取不到素材"是有记录的结论，不是命令错误。
4. 读 S06 ShotSpec（查询级依赖，C3 / S35-7）。读不到只影响依赖它的两项
   （`TECH.DURATION.MISMATCH`、`TECH.AUDIO.MISSING` → not_evaluated），
   不猜"大概没对白"。
5. 逐项检查 → coverage → 置信度。
6. `decide()`：**第 3 步在比较分数之前**——有必查项没得出结论就弃权。
7. 落库 + Outbox 同事务（§8）。报告与事件要么一起存在，要么都不存在。

派生键（§2.6）决定 `report_id` 与 generation，命令幂等因此是双层的：
命令层 `tenant_id + command_id + request_hash`，报告层派生键唯一索引。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from . import checks, evaluation
from .schema import (
    ABSTAIN_REASON_CODES,
    CONTRACT_GAPS,
    DECISIONS,
    ID_PREFIX,
    SCHEMA,
)

SOURCE = "S35"
SCHEMA_VERSION = "2.0"
QC_LAYER = "technical"
EVENT_COMPLETED = "quality.technical.completed"
TOPIC_COMPLETED = "drama.technical-qc.v2"
QCSDK_DECIDE_VERSION = "qc-sdk-decide/2.0.0"

REQUIRED_CONTEXT = ("project_id", "series_id", "episode_id", "branch_id",
                    "home_cell", "execution_fence", "node_epoch",
                    "policy_ref", "trace_id")
REQUIRED_TOP_LEVEL = ("run_id", "shot_lineage_id")


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


# ------------------------------------------------------------ §2.6 派生键

def derived_key(*, tenant_id, artifact_sha256, evaluator, policy_version,
                sampling_policy_version, attempt_slot_id) -> str:
    """§2.6。`command_id` 不参与派生——同一份内容用不同命令重算应得同一报告。

    `model_snapshot` 原样进 canonical JSON（`null` 保留）而不是被丢掉：
    评估器换了快照就该换一份报告，哪怕 version 没升。
    """
    return _sha256_hex(_jcs({
        "tenant_id": tenant_id,
        "artifact_sha256": artifact_sha256,
        "qc_layer": QC_LAYER,
        "evaluator.name": evaluator["name"],
        "evaluator.version": evaluator["version"],
        "evaluator.model_snapshot": evaluator.get("model_snapshot"),
        "policy_version": policy_version,
        "sampling_policy_version": sampling_policy_version,
        "repair_context.attempt_slot_id": attempt_slot_id,
    }))


def report_id_for(key_hex: str) -> str:
    return "qc_" + evaluation._b32(key_hex, 26)


def report_content_hash(payload: dict) -> str:
    """§2.3：除 `report_content_hash` 外全文档的 canonical sha256。

    `created_at` 在文档内、不进哈希——所以同一份测量在同一天重算会得到不同
    哈希（时间不同），这是对的：`content_hash` 是"这一份报告没有被改过"的
    证据，不是"两次测量一致"的证明。后者由 `report_generation` + 唯一索引
    表达。
    """
    body = {k: v for k, v in payload.items() if k != "report_content_hash"}
    return _sha256_hex(_jcs(body))


# ------------------------------------------------------------------ 服务

class TechnicalQcService:
    def __init__(self, conn: sqlite3.Connection, *, artifact_source,
                 probe_source, shot_source, policy_source,
                 enabled: bool = False, enabled_checks=()):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 executescript 隐式提交
        # 当前事务。只能在事务外建一次，之后复用同一实例，否则命令事务会在
        # 第一条事件写入时被悄悄提交，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        self.artifact_source = artifact_source
        self.probe_source = probe_source
        self.shot_source = shot_source
        self.policy_source = policy_source
        # §11：两个开关都默认关闭。`enabled_checks` 为空 = 不查任何项，此时
        # 执行命令会 422 NO_CHECKS_ENABLED——空 manifest 的 pass 是
        # "没查就说通过"的典型形态（S35-12）。
        self.enabled = bool(enabled)
        unknown = [c for c in enabled_checks if c not in checks.CHECKS]
        if unknown:
            # §2.8 第 1 条：registry 是数据，会漂移；越界登记在加载期失败，
            # 不是运行期静默跳过（ROUTE-T1）。
            raise ValueError(f"unknown check_id(s) in TECHNICAL_QC_CHECKS: "
                             f"{sorted(unknown)}")
        self.enabled_checks = tuple(enabled_checks)

    # ------------------------------------------------------------ 事务助手
    def _command(self, tenant_id: str, command_id: str, produce):
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    # ------------------------------------------------------------ 读上游
    def _upstream(self, call, code: str):
        """把"上游不可达"统一成 503，**绝不用缓存结论兜底**。

        `ConnectionError` 是上游不可达的既有约定；5xx 的 `ServiceError` 也
        归到 503，让"上游暂时读不到"与"上游说了不行"分开——后者是该命令
        自身的错误（4xx），前者是调用方可以重试的。
        """
        try:
            return call()
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(503, code, str(exc), retryable=True,
                                   details=exc.details) from exc
            raise

    # ------------------------------------------------------------ 命令
    def check(self, tenant_id: str, command_id: str, body: dict,
              request_hash: str) -> dict:
        """`POST /technical-checks`。同步评估，返回 `{report_id, ...}`。

        §6.1 把响应写成 202 + `operation_id`（异步产物）。本内核同步执行完成，
        所以直接返回已落库的报告标识，同时保留 `operation_id` 字段以符合
        HTTP 契约；`status` 用 `accepted`。见 S35-21。
        """
        if not self.enabled:
            # §11：默认关闭。关闭时明确拒绝，而不是产出一份"全项 policy_disabled
            # 的 abstain"——那会让调用方以为质检跑过了。
            raise ServiceError(503, "SERVICE_DISABLED",
                               "TECHNICAL_QC_ENABLED is not set; S35 will not "
                               "produce a report for this command (plan §11)")
        if not self.enabled_checks:
            raise ServiceError(
                422, "NO_CHECKS_ENABLED",
                "TECHNICAL_QC_CHECKS is empty; refusing to emit a report with "
                "an empty check_manifest, because that would read as a pass "
                "without measuring anything (plan §11)")

        context = body.get("execution_context")
        if not isinstance(context, dict):
            raise ServiceError(422, "INVALID_TECHNICAL_CHECK",
                               "execution_context is required")
        if context.get("tenant_id") != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "execution_context.tenant_id does not match "
                               "the credential")
        missing = [k for k in REQUIRED_CONTEXT if context.get(k) is None]
        # `policy_ref` 有 id 还不够：报告要钉住 `{id, revision, content_hash}`，
        # 少一个就无法复核"这份判定用的是哪一版策略"。
        policy_ref = context.get("policy_ref")
        if isinstance(policy_ref, dict):
            missing += [f"policy_ref.{k}" for k in
                        ("id", "revision", "content_hash")
                        if policy_ref.get(k) is None]
        else:
            missing.append("policy_ref")
        # S35-5：`run_id` / `shot_lineage_id` 不在 execution-context.v2 里，
        # 但在 QCReport v2 的 context 里是必填，所以从命令体顶层读。
        missing += [k for k in REQUIRED_TOP_LEVEL if body.get(k) is None]
        artifact_ref = body.get("artifact")
        if not isinstance(artifact_ref, dict) or not artifact_ref.get("id"):
            missing.append("artifact")
        # `shot_ref` 也必填。`shot_spec_ref` 是首选来源，`shot_id` 是直接给
        # 坐标的捷径；两个都没有就没法把报告挂到分镜上（S35-5）。
        shot_ref = body.get("shot_spec_ref")
        if not (isinstance(shot_ref, dict) and shot_ref.get("id")) \
                and not body.get("shot_id"):
            missing.append("shot_spec_ref.id or shot_id")
        if missing:
            raise ServiceError(
                422, "INVALID_TECHNICAL_CHECK",
                f"missing {sorted(missing)}; these are the coordinates the "
                f"report is recorded against")

        artifact_kind = body.get("artifact_kind") or "video"
        if artifact_kind not in checks_applies():
            raise ServiceError(
                422, "UNSUPPORTED_ARTIFACT_KIND",
                f"artifact_kind={artifact_kind!r} has no technical checks; "
                f"S35 accepts {sorted(checks_applies())} (S35-16)")

        profile = self._profile(tenant_id, context, body)
        artifact, probe, unreachable = self._artifact(
            tenant_id, artifact_ref, artifact_kind)
        shot = self._shot(tenant_id, context, body)

        return self._evaluate_and_persist(
            tenant_id=tenant_id, command_id=command_id, body=body,
            context=context, artifact_ref=artifact_ref, artifact=artifact,
            artifact_kind=artifact_kind, probe=probe,
            probe_unreachable=unreachable, shot=shot, profile=profile,
            policy_ref=context["policy_ref"], request_hash=request_hash)

    def _profile(self, tenant_id: str, context: dict, body: dict):
        """§4 末段：阈值全部来自 `policy_ref` 指向的 `technical_qc_profile`。

        `profile_ref` 与 `policy_ref`：命令体给 `profile_ref`（模式 + 目标），
        报告里的 `policy_ref` 必须指向 S02 的修订（schema 要求
        `service_id=="S02"`）。两者不是同一层，`policy_ref` 一律从 context 取。
        """
        policy_ref = context.get("policy_ref")
        if not isinstance(policy_ref, dict) or not policy_ref.get("id"):
            raise ServiceError(422, "INVALID_TECHNICAL_CHECK",
                               "execution_context.policy_ref is required")
        return self._upstream(
            lambda: self.policy_source(tenant_id, policy_ref,
                                       body.get("profile_ref")),
            "POLICY_UNAVAILABLE")

    def _artifact(self, tenant_id: str, ref: dict, artifact_kind: str):
        """取 manifest 元数据 + 探针文档。

        返回 `(metadata, probe, unreachable)`。`unreachable=True` 表示"上游
        暂时读不到"（超时 / 5xx），此时仍要产报告；而"上游明确说没有"在
        `_upstream` 里以 404/409 `ServiceError` 抛出 → 调用方 422（§6.1）。
        """
        try:
            metadata = self._upstream(
                lambda: self.artifact_source(tenant_id, ref),
                "ARTIFACT_SOURCE_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status == 503:
                metadata, probe = None, None
                return metadata, probe, True
            raise
        if not isinstance(metadata, dict):
            raise ServiceError(
                503, "ARTIFACT_SOURCE_UNAVAILABLE",
                "S28 returned no artifact metadata; S35 will not probe an "
                "unknown artifact", retryable=True)
        if str(metadata.get("content_hash") or "") != str(
                ref.get("content_hash") or metadata.get("content_hash")):
            # hash 不符：被测量的那份不是被引用的那份。继续会产出一份
            # "内容对不上"却看起来正常的报告（E24-T9 的前置）。
            raise ServiceError(
                409, "ARTIFACT_HASH_MISMATCH",
                "S28 metadata content_hash does not match the requested "
                "artifact_ref; S35 will not report on a different artifact",
                details={"requested": ref.get("content_hash"),
                         "actual": metadata.get("content_hash")})
        try:
            probe = self._upstream(
                lambda: self.probe_source(tenant_id, ref, artifact_kind),
                "PROBE_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status == 503:
                probe = None
                return metadata, probe, True
            raise
        return metadata, probe, False

    def _shot(self, tenant_id: str, context: dict, body: dict):
        """S06 ShotSpec。读不到返回 `None`——由依赖它的两项落 not_evaluated。"""
        ref = body.get("shot_spec_ref")
        if not isinstance(ref, dict) or not ref.get("id"):
            return None
        try:
            return self.shot_source(tenant_id, ref, context)
        except (ConnectionError, ServiceError):
            # 查询级依赖，读不到不该拖垮整份报告；它会让
            # TECH.DURATION.MISMATCH / TECH.AUDIO.MISSING 变成 not_evaluated，
            # 进而整份报告 abstain（S35-7）。
            return None

    # ------------------------------------------------------------ 评估 + 落库
    def _evaluate_and_persist(self, *, tenant_id, command_id, body, context,
                              artifact_ref, artifact, artifact_kind, probe,
                              probe_unreachable, shot, profile, policy_ref,
                              request_hash) -> dict:
        sampling_version = str(
            body.get("sampling_policy_version")
            or (profile or {}).get("sampling_policy_version")
            or "samp_unspecified")
        evaluator = {
            "name": str((profile or {}).get("evaluator_name")
                        or "s35-deterministic-probe"),
            "version": str((profile or {}).get("evaluator_version")
                           or "2.0.0"),
            "image_digest": str((profile or {}).get("image_digest")
                                or "sha256:unresolved"),
            "model_snapshot": None,   # §3.1：S35 不调模型，恒为 null
        }
        policy_version = str((profile or {}).get("policy_version")
                             or (policy_ref or {}).get("revision") or "unknown")
        attempt_slot_id = (body.get("repair_context") or {}).get(
            "attempt_slot_id") if isinstance(body.get("repair_context"),
                                             dict) else None

        manifest = evaluation.evaluate_checks(
            probe=probe, shot=shot, profile=profile, artifact=artifact,
            artifact_kind=artifact_kind, enabled=self.enabled_checks,
            unreachable=probe_unreachable)
        defects = [d for entry in manifest for d in entry["defects"]]
        defects = _assign_defect_ids(defects)
        coverage = evaluation.compute_coverage(
            probe=probe, sampling_policy_version=sampling_version,
            required_min=float((profile or {}).get("min_coverage_ratio")
                               or evaluation.DEFAULTS["min_coverage_ratio"]))
        confidence = evaluation.compute_confidence(
            manifest=manifest, artifact_kind=artifact_kind)
        decided = evaluation.decide(
            manifest=manifest, defects=defects, coverage=coverage,
            confidence=confidence, policy=_policy_view(profile))

        key = derived_key(
            tenant_id=tenant_id,
            artifact_sha256=str(artifact_ref.get("content_hash")
                                or artifact["content_hash"]),
            evaluator=evaluator, policy_version=policy_version,
            sampling_policy_version=sampling_version,
            attempt_slot_id=attempt_slot_id)
        report_id = report_id_for(key)
        generation = self._next_generation(tenant_id, key)

        payload = self._build_payload(
            report_id=report_id, generation=generation, tenant_id=tenant_id,
            context=context, body=body, artifact_ref=artifact_ref,
            artifact=artifact, artifact_kind=artifact_kind, probe=probe,
            evaluator=evaluator, policy_version=policy_version,
            sampling_version=sampling_version, manifest=manifest,
            defects=defects, coverage=coverage,
            confidence=confidence, decided=decided,
            attempt_slot_id=attempt_slot_id)
        digest = report_content_hash(payload)
        payload["report_content_hash"] = digest

        result = {
            "report_id": report_id,
            "report_generation": generation,
            "operation_id": _new_id("operation"),
            "status": "accepted",
            "decision": decided["decision"],
            "content_hash": digest,
        }

        def _produce():
            self._persist(
                report_id=report_id, generation=generation, key=key,
                tenant_id=tenant_id, context=context, body=body,
                artifact=artifact, artifact_kind=artifact_kind,
                evaluator=evaluator, policy_version=policy_version,
                sampling_version=sampling_version, manifest=manifest,
                defects=defects, coverage=coverage,
                decided=decided, attempt_slot_id=attempt_slot_id,
                payload=payload, digest=digest, command_id=command_id,
                request_hash=request_hash)

        self._command(tenant_id, command_id, _produce)
        return result

    def _next_generation(self, tenant_id: str, key: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM report WHERE tenant_id=? AND derived_key=?",
            (tenant_id, key)).fetchone()
        return int(row["n"] if row is not None else 0) + 1

    def _build_payload(self, *, report_id, generation, tenant_id, context,
                       body, artifact_ref, artifact, artifact_kind, probe,
                       evaluator, policy_version, sampling_version, manifest,
                       defects, coverage, confidence, decided,
                       attempt_slot_id) -> dict:
        """组装 QCReport v2。字段集由 `contracts/quality/qc-report.v2.schema.json`
        钉死（`additionalProperties: false`），少一个键就过不了校验。"""
        # S28 暂时读不到时 `artifact` 是 `None`：§10.4 仍要求产出弃权报告，
        # 所以这里不能对元数据假设非空。
        artifact = artifact if isinstance(artifact, dict) else {}
        media = _media_block(
            (probe or {}).get("media") if isinstance(probe, dict) else None,
            probe_media(body, artifact_kind), artifact, artifact_kind)
        # `severity_ceiling` 是可选的，只在探针真给了上限时才写。给 failed
        # 的项补一个 blocker 上限是编造——"没测出来"不等于"可能是 blocker"。
        manifest_out = [{"check_id": entry["check_id"],
                         "status": entry["status"],
                         "reason_code": entry["reason_code"]}
                        for entry in manifest]
        supersedes = None
        if generation > 1:
            supersedes = {"report_id": report_id,
                          "report_generation": generation - 1}
        repair_context = None
        if isinstance(body.get("repair_context"), dict):
            src = body["repair_context"]
            repair_context = {
                "repair_plan_id": str(src.get("repair_plan_id") or ""),
                "attempt_slot_id": str(src.get("attempt_slot_id") or ""),
                "attempt_seq": int(src.get("attempt_seq") or 1),
                "defect_fingerprints": list(
                    src.get("defect_fingerprints") or []),
            }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "report_id": report_id,
            "report_generation": generation,
            "qc_layer": QC_LAYER,
            "context": {
                "tenant_id": tenant_id,
                "project_id": str(context["project_id"]),
                "series_id": str(context["series_id"]),
                "branch_id": str(context["branch_id"]),
                "episode_id": str(context["episode_id"]),
                "home_cell": str(context["home_cell"]),
                "manifest_ref": {
                    "id": str(artifact.get("manifest_id")
                              or context.get("manifest_ref", {}).get("id")
                              or artifact_ref["id"]),
                    "revision": int(artifact.get("manifest_revision")
                                    or context.get("manifest_ref", {})
                                    .get("revision") or 1),
                    "content_hash": str(artifact.get("manifest_hash")
                                        or context.get("manifest_ref", {})
                                        .get("content_hash")
                                        or artifact_ref["content_hash"]),
                },
                "run_id": str(body["run_id"]),
                "execution_fence": int(context["execution_fence"]),
                "shot_lineage_id": str(body["shot_lineage_id"]),
                "policy_ref": {
                    "service_id": "S02",
                    "id": str(context["policy_ref"]["id"]),
                    "revision": int(context["policy_ref"]["revision"]),
                    "content_hash": str(context["policy_ref"]["content_hash"]),
                },
                "trace_id": str(context["trace_id"]),
            },
            "subject": {
                "artifact_id": str(artifact_ref["id"]),
                "artifact_sha256": str(artifact_ref.get("content_hash")
                                       or artifact["content_hash"]),
                "artifact_kind": artifact_kind,
                "media": media,
            },
            "shot_ref": {
                "shot_id": str((body.get("shot_spec_ref") or {})
                               .get("id") or body["shot_id"]),
                "shot_revision": int((body.get("shot_spec_ref") or {})
                                     .get("revision") or 1),
                "logical_node_id": str(context.get("node_id") or "node"),
                "node_epoch": int(context["node_epoch"]),
            },
            "evaluator": evaluator,
            "policy_version": policy_version,
            "sampling_policy_version": sampling_version,
            "check_manifest": manifest_out,
            "defects": defects,
            "dimensions": evaluation.build_dimensions(
                manifest=manifest, defects=defects),
            "coverage": coverage,
            "decision": decided["decision"],
            "decision_reason": decided["decision_reason"],
            "abstain": decided["abstain"],
            "confidence": confidence,
            "supersedes": supersedes,
            "repair_context": repair_context,
            # S35-14：内核不向 S28 归档探针 JSON / 拼板图（§3.2 第 5 步尚未
            # 实现）。引用被测量的 artifact 本身作为诚实下界，不伪造 id。
            "evidence_bundle_refs": [{
                "service_id": "S28",
                "id": str(artifact_ref["id"]),
                "revision": int(artifact_ref.get("revision") or 1),
                "content_hash": str(artifact_ref.get("content_hash")
                                    or artifact["content_hash"]),
            }],
            "created_at": _now(),
        }
        return payload

    def _persist(self, *, report_id, generation, key, tenant_id, context,
                 body, artifact, artifact_kind, evaluator, policy_version,
                 sampling_version, manifest, defects, coverage, decided,
                 attempt_slot_id, payload, digest, command_id,
                 request_hash) -> None:
        blocker = sum(1 for d in defects if d["severity"] == "blocker")
        major = sum(1 for d in defects if d["severity"] == "major")
        # 与 payload 的 `abstain.failed_check_ids` 取同一个来源，而不是另行
        # 从 manifest 数一遍：两条腿各数各的，正是"库里 0 条、文档里 23 条"
        # 那种自相矛盾的来源（`report_abstain_has_failed` 会先把它拦下）。
        failed = len((decided.get("abstain") or {}).get("failed_check_ids") or []) \
            if decided["decision"] == "abstain" else \
            sum(1 for e in manifest
                if e["status"] in ("failed", "not_evaluated"))
        shot_ref = payload["shot_ref"]
        # 列与值成对给出、由列表生成 `?,?,...`：手数占位符漏一个，SQLite 报的是
        # "36 values for 37 columns"，指不出漏了哪一列。
        row = [
            ("report_id", report_id),
            ("report_generation", generation),
            ("derived_key", key),
            ("tenant_id", tenant_id),
            ("project_id", payload["context"]["project_id"]),
            ("series_id", payload["context"]["series_id"]),
            ("episode_id", payload["context"]["episode_id"]),
            ("branch_id", payload["context"]["branch_id"]),
            ("shot_id", shot_ref["shot_id"]),
            ("shot_lineage_id", payload["context"]["shot_lineage_id"]),
            ("logical_node_id", shot_ref["logical_node_id"]),
            ("node_epoch", shot_ref["node_epoch"]),
            ("manifest_id", payload["context"]["manifest_ref"]["id"]),
            ("manifest_revision", payload["context"]["manifest_ref"]["revision"]),
            ("run_id", payload["context"]["run_id"]),
            ("execution_fence", payload["context"]["execution_fence"]),
            ("artifact_id", payload["subject"]["artifact_id"]),
            ("artifact_sha256", payload["subject"]["artifact_sha256"]),
            ("artifact_kind", artifact_kind),
            ("evaluator_name", evaluator["name"]),
            ("evaluator_version", evaluator["version"]),
            ("policy_version", policy_version),
            ("sampling_version", sampling_version),
            ("decision", decided["decision"]),
            ("decision_reason", decided["decision_reason"]),
            ("confidence", payload["confidence"]),
            ("coverage_ratio", coverage["coverage_ratio"]),
            ("required_coverage_min", coverage["required_coverage_min"]),
            ("defect_count", len(defects)),
            ("major_count", major),
            ("blocker_count", blocker),
            ("failed_check_count", failed),
            ("attempt_slot_id", attempt_slot_id),
            ("supersedes_report_id",
             payload["supersedes"]["report_id"] if payload["supersedes"] else None),
            ("supersedes_generation",
             payload["supersedes"]["report_generation"]
             if payload["supersedes"] else None),
            ("payload", _jcs(payload)),
            ("content_hash", digest),
            ("created_at", payload["created_at"]),
        ]
        columns = ", ".join(name for name, _ in row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(
            f"INSERT INTO report ({columns}) VALUES ({marks})",
            tuple(value for _, value in row))
        for ordinal, entry in enumerate(manifest):
            self.conn.execute(
                "INSERT INTO report_check (report_id, report_generation,"
                " ordinal, check_id, status, reason_code, defect_count)"
                " VALUES (?,?,?,?,?,?,?)",
                (report_id, generation, ordinal, entry["check_id"],
                 entry["status"], entry["reason_code"],
                 len(entry["defects"])))
        for defect in defects:
            self.conn.execute(
                "INSERT INTO report_defect (report_id, report_generation,"
                " defect_id, defect_fingerprint, check_id, defect_type,"
                " severity, confidence, detector, start_seconds, end_seconds,"
                " recommended_actions, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (report_id, generation, defect["defect_id"],
                 defect["defect_fingerprint"], defect["check_id"],
                 defect["type"], defect["severity"], defect["confidence"],
                 defect["detector"], defect["time_range"]["start_seconds"],
                 defect["time_range"]["end_seconds"],
                 _jcs(defect["recommended_actions"]), _jcs(defect)))
        # §8：事件与报告同事务。abstain **也要发**——弃权是有记录的结论，
        # 不是没有结论（§2.5 要求 4）。
        self._emit(report_id=report_id, generation=generation,
                   tenant_id=tenant_id, payload=payload, digest=digest,
                   command_id=command_id, defect_count=len(defects),
                   blocker_count=blocker,
                   coverage_ratio=coverage["coverage_ratio"],
                   attempt_slot_id=attempt_slot_id)

    def _emit(self, *, report_id, generation, tenant_id, payload, digest,
              command_id, defect_count, blocker_count, coverage_ratio,
              attempt_slot_id) -> None:
        context = payload["context"]
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_COMPLETED,
            # §7 与 QTC-1：`subject` 与 `data.shot_id` 由同一个变量赋值，
            # 代码里不可能分叉（契约层仍无约束）。
            subject=payload["shot_ref"]["shot_id"],
            tenant_id=tenant_id,
            home_cell=str(context.get("home_cell") or "unknown"),
            aggregate_id=report_id, sequence=generation,
            correlation_id=str(context.get("run_id") or command_id),
            causation_id=command_id,
            data={
                "schema_version": SCHEMA_VERSION,
                "report_id": report_id,
                "report_generation": generation,
                "qc_layer": QC_LAYER,
                "artifact_sha256": payload["subject"]["artifact_sha256"],
                "manifest_ref": dict(context["manifest_ref"]),
                "shot_id": payload["shot_ref"]["shot_id"],
                "shot_lineage_id": context["shot_lineage_id"],
                "node_epoch": int(payload["shot_ref"]["node_epoch"]),
                "run_id": context["run_id"],
                "execution_fence": int(context["execution_fence"]),
                "decision": payload["decision"],
                "decision_reason": payload["decision_reason"],
                "defect_count": defect_count,
                "blocker_count": blocker_count,
                "coverage_ratio": coverage_ratio,
                "attempt_slot_id": attempt_slot_id,
            },
            # 契约要 4 个键（§7 的 data_ref）；完整报告回查。
            data_ref={"service_id": SOURCE, "id": report_id,
                      "revision": generation, "content_hash": digest},
            schema_version=SCHEMA_VERSION))

    # ------------------------------------------------------------ 查询
    def get_report(self, tenant_id: str, report_id: str,
                   generation: str | int | None = None) -> dict:
        """§6.2。跨租户与不存在返回同一个 404，不泄漏存在性（E28）。"""
        if generation in (None, "", "latest"):
            row = _row(self.conn.execute(
                "SELECT * FROM report WHERE tenant_id=? AND report_id=?"
                " ORDER BY report_generation DESC LIMIT 1",
                (tenant_id, report_id)).fetchone())
        else:
            try:
                number = int(generation)
            except (TypeError, ValueError):
                raise ServiceError(422, "INVALID_GENERATION",
                                   f"generation={generation!r} is not an "
                                   f"integer or 'latest'")
            row = _row(self.conn.execute(
                "SELECT * FROM report WHERE tenant_id=? AND report_id=?"
                " AND report_generation=?", (tenant_id, report_id, number)
            ).fetchone())
        if row is None:
            raise ServiceError(404, "REPORT_NOT_FOUND",
                               f"no technical report {report_id}")
        return {
            "payload": _loads(row["payload"], {}),
            # §6.2 承诺这两个字段，而 §5 的 DDL 没有对应的投影列。本内核
            # 同步落库、无投影，故 source_revision 取报告代数、
            # projection_watermark 取落库时刻（见 S35-21）。
            "source_revision": row["report_generation"],
            "projection_watermark": row["created_at"],
            "report_generation": row["report_generation"],
            "decision": row["decision"],
            "content_hash": row["content_hash"],
        }

    def list_reports(self, tenant_id: str, *, artifact_sha256=None,
                     manifest_id=None, limit: int = 50) -> dict:
        """§6.3 内部查询。必须带一个范围——无范围列表在数据长大后就是事故。"""
        if not artifact_sha256 and not manifest_id:
            raise ServiceError(422, "QUERY_SCOPE_REQUIRED",
                               "one of artifact_sha256 or manifest_id is required")
        clause, params = [], [tenant_id]
        if artifact_sha256:
            clause.append("artifact_sha256=?")
            params.append(artifact_sha256)
        if manifest_id:
            clause.append("manifest_id=?")
            params.append(manifest_id)
        params.append(max(1, min(int(limit), 200)))
        rows = self.conn.execute(
            "SELECT report_id, report_generation, decision, decision_reason,"
            " artifact_sha256, manifest_id, manifest_revision, shot_id,"
            " node_epoch, execution_fence, coverage_ratio, defect_count,"
            " blocker_count, created_at, content_hash FROM report"
            " WHERE tenant_id=? AND " + " AND ".join(clause)
            + " ORDER BY created_at DESC LIMIT ?", params).fetchall()
        return {"items": [dict(r) for r in rows]}


# ------------------------------------------------------------ 纯函数：装配

def checks_applies():
    """S35 能处理的素材种类。§4 的 23 条检查没有一条适用于 image /
    subtitle_track，硬接会产出空清单的 pass（S35-16）。"""
    from .schema import SUPPORTED_ARTIFACT_KINDS
    return SUPPORTED_ARTIFACT_KINDS


def _assign_defect_ids(defects: list) -> list:
    """`defect_id` 由指纹 + 序号派生，保证同输入同输出（E24 可复现性）。

    指纹相同的两条缺陷（同一检查、同一 0.5s 桶、同严重度档）会拿到不同
    序号——否则同一份报告里的主键会撞。
    """
    seen: dict[str, int] = {}
    out = []
    for defect in defects:
        fingerprint = defect["defect_fingerprint"]
        index = seen.get(fingerprint, 0)
        seen[fingerprint] = index + 1
        item = dict(defect)
        item["defect_id"] = evaluation._defect_id(fingerprint, index)
        out.append(item)
    return out


def _policy_view(profile) -> dict:
    """把 S02 profile 折成 decide() 需要的形状。

    缺阈值给默认值；`allowed_actions` 缺省为空 = "策略没说，按 §2.7 第 5 步
    第 4 条走 human_review"，不是"什么都能自动修"。
    """
    profile = profile or {}
    policy = dict(evaluation.DEFAULTS)
    for key in ("min_coverage_ratio", "tau_low", "tau_abstain",
                "tau_disagreement", "model_disagreement"):
        if profile.get(key) is not None:
            policy[key] = profile[key]
    policy["allowed_actions"] = list(profile.get("allowed_actions") or [])
    return policy


def probe_media(body: dict, artifact_kind: str) -> dict | None:
    """命令体里可以带一份实测媒体描述（`body.media`）。

    沙箱真正的产物是探针文档；`body.media` 只是给"命令已经拿到了 ffprobe
    结果"的调用方用。两者都没有时 `subject.media` 只能给出空壳，那种情况的
    探针一定是 `failed`，报告是 abstain。见 S35-18。
    """
    media = body.get("media")
    return media if isinstance(media, dict) else None


def _media_block(probe_media_value, request_media_value, artifact: dict,
                 artifact_kind: str) -> dict:
    """`subject.media` 是**实测**结构（`coverate.media_duration_seconds` 的
    同一来源）。优先级：探针实测 → 命令体声明的实测值 → S28 元数据。

    最后一档是降级：S28 只存了宽高/时长，用它填 `codec`/`pix_fmt` 会写
    `unknown`。那种情况探针必然是 failed、报告 abstain，所以 `unknown` 不会
    被当成"测过"。
    """
    # `artifact` 可以是 `None`：S28 暂时读不到时 §10.4 仍要求产出报告。
    # 这时 `subject.media` 只能给空壳——那种情况的探针必然是 failed、
    # 报告是 abstain，所以空壳不会被当成"测过"（S35-18）。
    artifact = artifact if isinstance(artifact, dict) else {}
    source = probe_media_value or request_media_value or (
        artifact.get("media") if isinstance(artifact.get("media"), dict) else {})
    video = source.get("video")
    audio = source.get("audio")
    if not isinstance(video, dict):
        video = _video_from_artifact(artifact, artifact_kind)
    if not isinstance(audio, dict):
        audio = _audio_from_artifact(artifact, artifact_kind)
    duration = source.get("duration_seconds")
    if not isinstance(duration, (int, float)):
        duration = artifact.get("duration_seconds") or (
            float(artifact.get("duration_ms") or 0) / 1000.0)
    return {
        "duration_seconds": max(0.0, float(duration or 0)),
        "video": None if video is None else {
            "width": int(video.get("width") or 1),
            "height": int(video.get("height") or 1),
            "fps_num": int(video.get("fps_num") or 24),
            "fps_den": int(video.get("fps_den") or 1),
            "codec": str(video.get("codec") or "unknown"),
            "pix_fmt": str(video.get("pix_fmt") or "unknown"),
            "color_range": str(video.get("color_range") or "unknown"),
            "is_vfr": bool(video.get("is_vfr")),
            "frame_count": int(video.get("frame_count") or 0),
        },
        "audio": None if audio is None else {
            "codec": str(audio.get("codec") or "unknown"),
            "sample_rate_hz": int(audio.get("sample_rate_hz") or 48000),
            "channels": int(audio.get("channels") or 2),
            "stream_index": int(audio.get("stream_index") or 0),
            "start_offset_seconds": float(
                audio.get("start_offset_seconds") or 0.0),
        },
    }


def _video_from_artifact(artifact: dict, artifact_kind: str):
    if artifact_kind == "audio":
        return None
    width = artifact.get("width_px")
    height = artifact.get("height_px")
    if width is None or height is None:
        return None
    frame_rate = artifact.get("frame_rate") or {}
    return {"width": width, "height": height,
            "fps_num": frame_rate.get("num") or 24,
            "fps_den": frame_rate.get("den") or 1,
            "codec": artifact.get("video_codec") or "unknown",
            "pix_fmt": artifact.get("pix_fmt") or "unknown",
            "color_range": artifact.get("color_range") or "unknown",
            "is_vfr": bool(artifact.get("is_vfr")),
            "frame_count": artifact.get("frame_count") or 0}


def _audio_from_artifact(artifact: dict, artifact_kind: str):
    if artifact_kind == "video" and artifact.get("has_audio") is False:
        return None
    codec = artifact.get("audio_codec")
    if codec is None and artifact_kind != "audio":
        return None
    return {"codec": codec or "unknown",
            "sample_rate_hz": artifact.get("sample_rate_hz") or 48000,
            "channels": artifact.get("channels") or 2,
            "stream_index": artifact.get("audio_stream_index") or 0,
            "start_offset_seconds": artifact.get("audio_start_offset_seconds")
            or 0.0}
