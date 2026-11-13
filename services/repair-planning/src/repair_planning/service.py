"""S39 的写路径：把 QC 报告折成"该修什么、按什么顺序、还能不能修"。

三个结构性纪律，代码里逐条可见：

1. **只出计划，不执行、不花钱、不放行。** 没有 artifact 写入口、没有
   供应商调用、没有金额列。`estimated_cost` 是档位估计（§4.5），真正的钱
   在 S23 reserve。计划可以被 S02 拒绝、被 S21 忽略——本服务不担保它会被
   执行，也**不把"计划已生成"当成任何形式的授权**。
2. **不接受状态。** 复检 pass 只把计划标 `resolved` 并交出一句"候选可接受"，
   S08 的 CAS 才是接受指针；`review.decision.recorded` 只把计划标
   `human_resolved`——`recorded` 不是 `accepted`，所以这里没有任何一行会去
   推进接受状态。
3. **两个游标分列存、分列读。** `execution_fence` 属于 run/branch，
   `node_epoch` 属于单个 `logical_node`。本文件里它们只作为列值写入与读出，
   **没有任何一处比较、换算、或由一列给另一列赋值**。旧 epoch 的结果在本
   服务里不推进任何东西——因为本服务不持有任何接受指针。

路由全部按数据走（`registry.py`）：S39 不认识任何具体 check_id。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Inbox, Outbox, new_event

from . import registry as registry_mod
from .schema import (
    ACTION_COST,
    ACTION_COST_UNDEFINED,
    ACTIONS,
    BLOCKING_WEIGHT,
    CONSUMER_GENERATION,
    CONSUMER_REVIEW,
    CONTRACT_GAPS,
    DEFAULT_AGGREGATION_DEADLINE_SECONDS,
    DEFAULT_MAX_ATTEMPTS_PER_EPISODE,
    DEFAULT_MAX_ATTEMPTS_PER_RUN,
    EVENT_ESCALATED,
    EVENT_PLANNED,
    ID_PREFIX,
    LAYER_OF_SERVICE,
    PLAN_STATES,
    RECHECK_COST,
    REQUIRED_LAYERS,
    ROUTING_ALGORITHM_VERSION,
    SCHEMA,
    SCHEMA_VERSION,
    SEVERITY_WEIGHT,
    SOURCE,
    TOPIC,
    TOPIC_ESCALATED,
)

# 报告校验的输入契约。运行期不 import jsonschema（只有 stdlib + SDK），
# 测试里用 jsonschema 当 oracle 对拍（见 tests）。
QC_REPORT_SCHEMA = "contracts/quality/qc-report.v2.schema.json"

# 哪一层归哪个服务。与 registry.LAYER_OWNER 互为反函数，但用途不同：
# 这里是把"到的是谁"翻成"哪层齐了"。
SERVICE_LAYER = dict(LAYER_OF_SERVICE)

REPORT_SERVICES = ("S35", "S36", "S37", "S38", "S17")

# 派生键**能**从文档本身重算的两层。S37 需要 audio_track_hash + language，
# S38 需要 track_id + subtitle_revision + attempt_key——这些都不在
# qc-report.v2 里，所以那两层只能靠 `content_hash` 钉住代数（C25）。
DERIVABLE_LAYERS = frozenset({"technical", "semantic"})

REJECTABLE_STATUSES = ("not_evaluated", "failed")
REPORTED_STATUSES = ("evaluated", "not_applicable")


# ---------------------------------------------------------------- 基础工具

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _now() -> str:
    return _fmt(_now_dt())


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
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _pos(value, default=0.0) -> float:
    return float(value) if _finite(value) and value >= 0 else default


def _b32(key_hex: str, length: int) -> str:
    raw = bytes.fromhex(key_hex)
    encoded = base64.b32encode(raw).decode("ascii").lower().rstrip("=")
    return encoded[:length]


def _hex64(value) -> str | None:
    """接受裸 64 位小写十六进制或 `sha256:` 前缀。"""
    if not isinstance(value, str):
        return None
    text = value[7:] if value.startswith("sha256:") else value
    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
        return text
    return None


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """非嵌套 `BEGIN IMMEDIATE`。已在外层事务里时不再开（由调用方负责）。

    嵌套判定用 sqlite3 自己的 `in_transaction`，**不往 Connection 上挂属性**：
    `sqlite3.Connection` 没有 `__dict__`，赋值会抛 `AttributeError`，而那个
    异常会从 `__enter__` 冒到调用方，看起来像"请求有错"。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.started = False

    def __enter__(self):
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
            self.started = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.started:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        return False


# 幂等摘要里剥掉的易变字段。与 S37/S41 同一份清单：trace 与时间戳进来，
# 同一请求重投就会算出不同 hash，然后被当成"同键不同 body"拒绝。
VOLATILE_CONTEXT_KEYS = ("trace_id", "traceparent", "timestamp",
                         "requested_at", "issued_at", "server_time")


def stable_request_hash(body: dict) -> str:
    from idempotency_sdk import canonical_request_hash

    if not isinstance(body, dict):
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    candidate = dict(body)
    context = candidate.get("execution_context")
    if isinstance(context, dict):
        candidate["execution_context"] = {
            k: v for k, v in context.items() if k not in VOLATILE_CONTEXT_KEYS}
    return canonical_request_hash(candidate)


# ------------------------------------------------------- 计划指纹与策略类

def plan_fingerprint(*, artifact_sha256: str, defects: list[dict],
                     registry_version: int, policy_snapshot_id: str,
                     policy_version: str) -> str:
    """§7.1 的确定性指纹。**同指纹 = 同一张计划**，不同 = 新计划。

    `routing_algorithm_version` 进了指纹：算法改了，同一份缺陷得到不同计划
    是合理的**新**计划，不是幂等冲突。
    """
    rows = sorted(
        {
            _jcs({
                "check_id": str(d.get("check_id") or ""),
                "window_start": round(_pos(
                    (d.get("time_range") or {}).get("start_seconds")), 3),
                "window_end": round(_pos(
                    (d.get("time_range") or {}).get("end_seconds")), 3),
                "severity": str(d.get("severity") or ""),
            })
            for d in defects
        }
    )
    return _sha256_hex(_jcs({
        "artifact_sha256": artifact_sha256,
        "defects": rows,
        "registry_version": int(registry_version),
        "policy_snapshot_id": str(policy_snapshot_id),
        "policy_version": str(policy_version),
        "routing_algorithm_version": ROUTING_ALGORITHM_VERSION,
    }))


def fingerprint_set(defects) -> str:
    """同一组缺陷的稳定摘要。§5.9 的死循环判据只看"集合有没有变"。

    接受 defect 字典或**裸指纹字符串**两种输入：调用点一处给 target、一处
    给指纹，混用会让这里在第 300 行之后才炸（`str.get`）。两种都认，但
    绝不把字符串当字典用。
    """
    values = sorted({
        str(d.get("defect_fingerprint") or "") if isinstance(d, dict)
        else str(d or "")
        for d in defects})
    return _sha256_hex(_jcs(values))


def _bucket(value) -> str:
    """0.1 归一化的位置桶（§5.3 `mask_region_bucket` / `target_frame_bucket`）。"""
    if _finite(value):
        return f"{round(float(value) * 10) / 10:.1f}"
    return "na"


def _token(value) -> str:
    if value in (None, ""):
        return "na"
    return str(value)


# §5.3 的归一化参数表。**只有这里列出的参数进 strategy_class**——
# `variation_nonce` / `seed` / `attempt_id` / `provider` / `model_id` 一律不进，
# 否则"同类同策略最多 2 次"形同虚设。
STRATEGY_PARAMETERS = {
    "regenerate_video": ("prompt_template_id", "shot_spec_revision"),
    "keyframe_edit": ("mask_region_bucket", "target_frame_bucket"),
    "temporal_edit": ("edit_kind",),
    "redo_lipsync": ("lipsync_model_family",),
    "remix": ("fix_kind",),
    "transcode": ("target_profile_id",),
    "reingest": ("source_kind",),
    "realign_subtitles": ("retime_kind",),
}
BUCKETED_PARAMETERS = frozenset({"mask_region_bucket", "target_frame_bucket"})


def strategy_class(action: str, parameters: dict | None) -> str:
    """`f(action, normalized_parameters)`（§5.3）。

    换模型、换种子、换 workflow、提 epoch 都不改变它——这正是跨 Run 累计
    能成立的前提。不在归一化表里的动作只由动作名构成。
    """
    names = STRATEGY_PARAMETERS.get(str(action))
    if not names:
        return str(action)
    parameters = parameters if isinstance(parameters, dict) else {}
    values = []
    for name in names:
        raw = parameters.get(name)
        values.append(_bucket(raw) if name in BUCKETED_PARAMETERS
                      else _token(raw))
    return ":".join([str(action)] + values)


# ------------------------------------------------- JSON Schema 子集校验器
#
# 只有 stdlib。关键字集是量出来的：把 contracts 里四份相关 schema 走一遍，
# 出现的每一个关键字都在下面有分支；**未知关键字一律报错**（failing closed）
# ——"看不懂这条约束"不能当成"这条约束满足"。
_IGNORED_KEYWORDS = frozenset({
    "$schema", "$id", "title", "description", "default", "examples",
    "deprecated", "readOnly", "writeOnly", "x-conflicts", "x-needs-owner",
})
_KNOWN_KEYWORDS = frozenset({
    "$defs", "$ref", "additionalProperties", "allOf", "const", "enum",
    "exclusiveMinimum", "format", "if", "items", "maximum", "minItems",
    "minLength", "minimum", "oneOf", "pattern", "properties", "required",
    "then", "type", "uniqueItems",
}) | _IGNORED_KEYWORDS

_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")


def _json_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _type_ok(value, expected: str) -> bool:
    actual = _json_type(value)
    if expected == "number":
        return actual in ("number", "integer")
    if expected == "integer":
        return actual == "integer"
    return actual == expected


def _resolve(root: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref {ref!r}")
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate_schema(value, schema, root=None, path: str = "$") -> list[str]:
    """返回错误列表。空列表 = 通过。"""
    if root is None:
        root = schema
    if not isinstance(schema, dict):
        return [f"{path}: schema node is not an object"]
    unknown = set(schema) - _KNOWN_KEYWORDS
    if unknown:
        return [f"{path}: unsupported schema keyword(s) {sorted(unknown)}"]

    errors: list[str] = []

    if "$ref" in schema:
        return validate_schema(value, _resolve(root, schema["$ref"]), root, path)

    if "type" in schema:
        expected = schema["type"]
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, kind) for kind in options):
            return [f"{path}: expected type {expected}, got {_json_type(value)}"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, "
                      f"got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength "
                          f"{schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match pattern {schema['pattern']}")
        if schema.get("format") == "date-time" and not _DATE_TIME.match(value):
            errors.append(f"{path}: not an RFC 3339 date-time")

    if _json_type(value) in ("number", "integer"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: not above exclusiveMinimum "
                          f"{schema['exclusiveMinimum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if schema.get("uniqueItems"):
            seen = []
            for item in value:
                key = _jcs(item)
                if key in seen:
                    errors.append(f"{path}: duplicate item under uniqueItems")
                    break
                seen.append(key)
        if "items" in schema:
            for index, item in enumerate(value):
                errors += validate_schema(item, schema["items"], root,
                                          f"{path}[{index}]")

    if isinstance(value, dict):
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{path}: missing required {name!r}")
        properties = schema.get("properties") or {}
        for name, item in value.items():
            if name in properties:
                errors += validate_schema(item, properties[name], root,
                                          f"{path}.{name}")
            else:
                extra = schema.get("additionalProperties")
                if extra is False:
                    errors.append(f"{path}: unexpected property {name!r}")
                elif isinstance(extra, dict):
                    errors += validate_schema(item, extra, root,
                                              f"{path}.{name}")

    for branch in schema.get("allOf") or []:
        errors += validate_schema(value, branch, root, path)

    if "oneOf" in schema:
        matches = [branch for branch in schema["oneOf"]
                   if not validate_schema(value, branch, root, path)]
        if len(matches) != 1:
            errors.append(f"{path}: matched {len(matches)} of oneOf "
                          f"alternatives (exactly one required)")

    if "if" in schema:
        if not validate_schema(value, schema["if"], root, path):
            if "then" in schema:
                errors += validate_schema(value, schema["then"], root, path)
    return errors


# ------------------------------------------------------- 报告派生键的重算
#
# S35/S36 的派生键输入全部在 qc-report.v2 文档里，可以重算；S37/S38 的
# 派生键另含 `audio_track_hash` / `language` / `track_id` 等文档里没有的
# 量，重算不出来（C25）。重算不出来时**不假装算过**——那两层的代数由
# `content_hash` + `report_generation` 钉住，由调用方按 revision 读。

def derived_key_for(report: dict) -> str | None:
    """按层重算派生键；该层不可重算时返回 None。"""
    layer = report.get("qc_layer")
    if layer not in DERIVABLE_LAYERS:
        return None
    context = report.get("context") or {}
    evaluator = report.get("evaluator") or {}
    repair_context = report.get("repair_context") or {}
    return _sha256_hex(_jcs({
        "tenant_id": context.get("tenant_id"),
        "artifact_sha256": (report.get("subject") or {}).get("artifact_sha256"),
        "qc_layer": layer,
        "evaluator.name": evaluator.get("name"),
        "evaluator.version": evaluator.get("version"),
        "evaluator.model_snapshot": evaluator.get("model_snapshot"),
        "policy_version": report.get("policy_version"),
        "sampling_policy_version": report.get("sampling_policy_version"),
        "repair_context.attempt_slot_id":
            repair_context.get("attempt_slot_id") if repair_context else None,
    }))


def report_id_for(key_hex: str) -> str:
    return "qc_" + _b32(key_hex, 26)


def report_content_hash(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "report_content_hash"}
    return _sha256_hex(_jcs(body))


# ---------------------------------------------------------- 缺陷 / 归因

SOURCE_PRIORITY = ("S17", "S36", "S38", "S37", "S35")
SEVERITY_ORDER = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
BAND_ORDER = {"low": 0, "medium": 1, "high": 2}


def _severity_max(left: str, right: str) -> str:
    return left if SEVERITY_ORDER.get(left, 0) >= SEVERITY_ORDER.get(
        right, 0) else right


def window_frames(defect: dict, media: dict | None) -> int | None:
    """缺陷窗口的帧数。**算不出来就返回 None**（保守：不允许逐帧修）。

    `qc-report.v2` 的 defect 没有「帧数」字段，只有 `time_range` 与 `scope`。
    有 fps 才敢换算；没有 fps 时只有 `scope=frame` 能证明"就一帧"。
    """
    span = defect.get("time_range") or {}
    start, end = span.get("start_seconds"), span.get("end_seconds")
    video = (media or {}).get("video") if isinstance(media, dict) else None
    fps = None
    if isinstance(video, dict):
        num = video.get("fps_num")
        den = video.get("fps_den")
        if _finite(num) and _finite(den) and den:
            fps = float(num) / float(den)
    if fps and _finite(start) and _finite(end) and end > start:
        return max(1, int(math.ceil((float(end) - float(start)) * fps - 1e-9)))
    if defect.get("scope") == "frame":
        return 1
    return None


def _media_of(reports: list[dict]) -> dict:
    for report in reports:
        media = (report.get("subject") or {}).get("media")
        if isinstance(media, dict):
            return media
    return {}


def expected_checks(registry, layer: str | None = None) -> list[str]:
    """registry 里 owner ∈ {S35,S36,S37,S38,S17} 的全部 check（§4.3.2）。"""
    if registry is None:
        return []
    out: list[str] = []
    for service in REPORT_SERVICES:
        check_ids = registry.checks_owned_by(service)
        if layer is None:
            out += check_ids
        else:
            out += [c for c in check_ids
                    if (registry.entry(c) or {}).get("layer") == layer]
    return out


# ------------------------------------------------------------ 报告校验器
#
# §4.2 的 a–j。**每一步失败都是拒收**，没有"跳过这一条继续"。
#
# 一处**方案内部冲突**必须写明（C26）：§4.2 步骤 i 要求"check_id 存在且登记
# 项通过族校验"否则拒收，但 §3.1.1 与 E24-R14/R15/R16 要求"登记无效 → 该
# 缺陷按 human_review 处理并告警"。两者不兼容：拒收就没有计划，也就没有
# human_review 可标。本实现按 E24-R14/R15/R16（验收用例）执行——登记无效是
# **路由层**的人审，不是 ingest 的拒收；拒收留给报告**自身**不自洽的情况
# （a–h、j）。冲突登记在 CONTRACT_GAPS，不冒充 owner 的决定。

def report_problems(report: dict, *, registry, layer: str, artifact_sha256: str,
                    tenant_id: str, ref: dict | None = None,
                    recompute_identity: bool = True) -> list[dict]:
    """返回 `[{reason, detail}]`。空列表 = 通过（§4.2 a–j）。"""
    problems: list[dict] = []
    context = report.get("context") or {}
    subject = report.get("subject") or {}
    manifest = report.get("check_manifest") or []
    coverage = report.get("coverage") or {}
    defects = report.get("defects") or []

    # (a) JSON Schema。调用方已经把 S17 的 disposition 剥掉（C10）。
    #     这里只做形状：schema 由调用方注入，未注入时调用方已 503。
    # (b) 报告身份。
    key = derived_key_for(report) if recompute_identity else None
    if key is not None:
        expected_id = report_id_for(key)
        if report.get("report_id") != expected_id:
            problems.append({
                "reason": "report_id_mismatch",
                "detail": f"report_id {report.get('report_id')!r} does not "
                          f"recompute from the derived key ({expected_id})"})
    digest = report.get("report_content_hash")
    if digest != report_content_hash(report):
        problems.append({
            "reason": "content_hash_mismatch",
            "detail": "report_content_hash does not match the document"})
    if isinstance(ref, dict) and ref.get("content_hash"):
        if _hex64(ref["content_hash"]) != digest:
            problems.append({
                "reason": "content_hash_mismatch",
                "detail": "the snapshot pins a different content_hash than "
                          "the report carries (a stale QC was supplied)"})
    if isinstance(ref, dict) and ref.get("revision") is not None:
        if int(ref["revision"]) != int(report.get("report_generation") or 0):
            problems.append({
                "reason": "generation_mismatch",
                "detail": f"snapshot pins revision {ref['revision']}, the "
                          f"report is generation "
                          f"{report.get('report_generation')}"})

    # (c) 报告绑定的 artifact 必须就是事件/命令声明的那个。
    if subject.get("artifact_sha256") != artifact_sha256:
        problems.append({
            "reason": "artifact_hash_mismatch",
            "detail": f"report is bound to "
                      f"{subject.get('artifact_sha256')!r}, the request "
                      f"declares {artifact_sha256!r}"})
    if context.get("tenant_id") not in (None, ""):
        # 租户由签名头绑定；报告里的租户只用来发现错投。
        if str(context.get("tenant_id")) != str(tenant_id):
            problems.append({
                "reason": "tenant_mismatch",
                "detail": "report context carries another tenant"})

    # (d) check_manifest 完备：本层要求的每个 check 恰好一次，且只列自己的。
    if not manifest:
        problems.append({"reason": "manifest_incomplete",
                         "detail": "check_manifest is empty"})
    seen: list[str] = []
    for entry in manifest:
        check_id = str(entry.get("check_id"))
        if check_id in seen:
            problems.append({"reason": "manifest_duplicate",
                             "detail": f"{check_id} appears more than once"})
        seen.append(check_id)
        registered = registry.entry(check_id) if registry is not None else None
        if registered is not None:
            if registered.get("layer") != layer:
                problems.append({
                    "reason": "manifest_not_owned",
                    "detail": f"{check_id} belongs to layer "
                              f"{registered.get('layer')!r}, this report is "
                              f"{layer!r}"})
        elif check_id.split(".")[0] not in {
                p for p, lay in registry_mod.LAYER_OF_PREFIX.items()
                if lay == layer}:
            problems.append({
                "reason": "manifest_not_owned",
                "detail": f"{check_id} is not in the {layer!r} namespace"})
    if registry is not None:
        for check_id in expected_checks(registry, layer):
            if check_id not in seen:
                problems.append({
                    "reason": "manifest_incomplete",
                    "detail": f"{check_id} is required for this layer but is "
                              f"absent from check_manifest"})

    # (e) 覆盖率自洽。`planned=0` 时契约强制 ratio 为 0。
    planned = coverage.get("planned_windows")
    evaluated = coverage.get("temporal_windows_evaluated")
    ratio = coverage.get("coverage_ratio")
    if not isinstance(planned, int) or isinstance(planned, bool):
        problems.append({"reason": "coverage_inconsistent",
                         "detail": "planned_windows is not an integer"})
    elif planned <= 0:
        if ratio not in (0, 0.0):
            problems.append({
                "reason": "coverage_inconsistent",
                "detail": "planned_windows=0 requires coverage_ratio=0"})
    elif not isinstance(evaluated, int) or isinstance(evaluated, bool):
        problems.append({"reason": "coverage_inconsistent",
                         "detail": "temporal_windows_evaluated is not an "
                                   "integer"})
    else:
        want = round(evaluated / planned, 6)
        if not _finite(ratio) or abs(float(ratio) - want) > 1e-6:
            problems.append({
                "reason": "coverage_inconsistent",
                "detail": f"coverage_ratio {ratio!r} != "
                          f"{evaluated}/{planned} = {want}"})

    # (f) 结论自洽。重算这样一套层无关的阶梯，不复用任何一家的 decide()
    #     （C22：三份实现无法由本服务证明等价，import 别家违反 AGENTS.md）。
    decision = report.get("decision")
    failed = [e.get("check_id") for e in manifest
              if e.get("status") in REJECTABLE_STATUSES]
    blockers = [d for d in defects if d.get("severity") == "blocker"]
    required_min = coverage.get("required_coverage_min")
    below = (_finite(ratio) and _finite(required_min)
             and float(ratio) < float(required_min))
    if failed or below:
        if decision != "abstain":
            problems.append({
                "reason": "decision_inconsistent",
                "detail": f"decision={decision!r} but "
                          f"{len(failed)} check(s) did not conclude"
                          f"{' and coverage is below the minimum' if below else ''}"})
    elif blockers and decision != "repair":
        problems.append({
            "reason": "decision_inconsistent",
            "detail": f"decision={decision!r} with {len(blockers)} blocker "
                      f"defect(s)"})
    if decision == "pass" and defects:
        problems.append({
            "reason": "decision_inconsistent",
            "detail": f"decision=pass with {len(defects)} defect(s)"})
    if decision == "abstain":
        abstain = report.get("abstain")
        if not isinstance(abstain, dict) or not abstain.get("reason_code"):
            problems.append({
                "reason": "decision_inconsistent",
                "detail": "decision=abstain without an abstain block"})
        elif not set(abstain.get("failed_check_ids") or []) and not below:
            problems.append({
                "reason": "decision_inconsistent",
                "detail": "abstain lists no failed check and coverage is "
                          "adequate"})
    elif isinstance(report.get("abstain"), dict):
        problems.append({
            "reason": "decision_inconsistent",
            "detail": f"decision={decision!r} carries an abstain block"})

    # (g) 时间窗与区域。
    for defect in defects:
        span = defect.get("time_range") or {}
        start, end = span.get("start_seconds"), span.get("end_seconds")
        if not (_finite(start) and _finite(end)) or float(end) <= float(start):
            problems.append({
                "reason": "defect_range_invalid",
                "detail": f"{defect.get('check_id')}: time_range "
                          f"{start!r}..{end!r}"})
        bbox = defect.get("region_bbox")
        if isinstance(bbox, dict):
            for name in ("x", "y", "width", "height"):
                value = bbox.get(name)
                if not _finite(value) or not (0.0 <= float(value) <= 1.0):
                    problems.append({
                        "reason": "defect_range_invalid",
                        "detail": f"{defect.get('check_id')}: region_bbox."
                                  f"{name}={value!r} outside [0,1]"})
            if _finite(bbox.get("x")) and _finite(bbox.get("width")):
                if float(bbox["x"]) + float(bbox["width"]) > 1.0 + 1e-9:
                    problems.append({
                        "reason": "defect_range_invalid",
                        "detail": f"{defect.get('check_id')}: region_bbox "
                                  f"x+width > 1"})

    # (h)(j) 类型与建议动作。登记无效交给路由（C26），这里只验报告自身。
    for defect in defects:
        check_id = str(defect.get("check_id"))
        entry = registry.entry(check_id) if registry is not None else None
        if entry is not None and entry.get("defect_type") != defect.get("type"):
            problems.append({
                "reason": "defect_type_mismatch",
                "detail": f"{check_id} is registered as "
                          f"{entry.get('defect_type')!r}, the defect says "
                          f"{defect.get('type')!r}"})
        family = set(registry.chain(defect.get("type"))) if registry else set()
        for action in defect.get("recommended_actions") or []:
            if family and action not in family and action != "human_review":
                problems.append({
                    "reason": "action_out_of_family",
                    "detail": f"{check_id}: recommended action {action!r} is "
                              f"outside the {defect.get('type')} family"})
    return problems


# ------------------------------------------------------------------ 路由

def _capability_probe(capability, tenant_id: str, frames: int | None):
    """问一次 S24/S25 的候选能力，返回 `(ok, candidates)`。

    C19：只发 `temporal_consistency`（词表里真实存在的那个），
    `max_window_frames` / `preserves_identity` 不作为查询参数发出去。
    结果回收后再按候选自己声明的字段判定；判定不出一律当作"不满足"。
    """
    requirement = {"modality": "video",
                   "required_capabilities": ["temporal_consistency"]}
    payload = capability(tenant_id, requirement)
    candidates = [c for c in (payload.get("candidates") or [])
                  if isinstance(c, dict)]
    return bool(candidates), candidates, frames


def _candidate_covers(candidate: dict, frames: int | None) -> bool:
    """候选是否声明了覆盖该窗口的时序能力（C19：声明的字段才认）。"""
    if candidate.get("preserves_identity") is not True:
        return False
    ceiling = candidate.get("max_window_frames")
    if frames is None or not _finite(ceiling):
        return False
    return int(ceiling) >= int(frames)


def _choose_by_cost(actions, chain) -> str | None:
    """交集/建议集合里挑"最省"的那个（§4.3 第 1 条）。"""
    if not actions:
        return None
    order = {name: i for i, name in enumerate(registry_mod.ACTION_PREFERENCE)}
    rank = {name: i for i, name in enumerate(chain)}
    return sorted(set(actions),
                  key=lambda a: (ACTION_COST.get(
                      a, ACTION_COST_UNDEFINED.get(a, 0)),
                      rank.get(a, len(chain)),
                      order.get(a, len(order))))[0]


def route(*, target: dict, registry, policy_allowed, capability_source,
          tenant_id: str, media: dict) -> dict:
    """把一个 target 折成一个动作决定（§3.1 / §3.2 / §4.3）。

    返回：
      `{"action", "reason", "escalation", "candidates", "parameters"}`。
    `action == "human_review"` 时 `escalation` 非空。
    """
    entry = registry.entry(target["check_id"])
    if entry is None:
        return _human("registry_entry_invalid",
                      f"{target['check_id']} is not registered; S39 will not "
                      f"guess an action for it")
    problems = registry.entry_errors(entry)
    if problems:
        return _human("registry_entry_invalid",
                      f"{target['check_id']}: {problems[0]}")

    # 硬规则 2：`never_auto_repair` 不看动作族，理由取登记的那一句。
    # 代码里**没有**任何一个具体 check_id。
    if entry.get("never_auto_repair"):
        return _human(str(entry.get("no_auto_reason") or "creative_decision"),
                      f"{target['check_id']} is registered as "
                      f"never_auto_repair")

    chain = registry.chain(target["defect_type"])
    if not chain:
        return _human("registry_entry_invalid",
                      f"no action family for type {target['defect_type']!r}")

    scope = target.get("scope")
    frames = target.get("window_frames")
    candidates = registry_mod.candidate_actions(
        chain=chain, entry=entry, policy_allowed=policy_allowed,
        window_scope=scope, window_frames=frames)

    # 硬规则 1 的第二道闸：缺陷实际窗口决定了逐帧能不能用（E24-R2）。
    if "keyframe_edit" in candidates and (
            scope not in registry_mod.SCOPE_WINDOW or frames is None
            or int(frames) > registry_mod.KEYFRAME_MAX_FRAMES):
        candidates = [a for a in candidates if a != "keyframe_edit"]

    # §3.2 能力闸门。时序类动作必须**证明**能力；证明不了就少一个候选。
    temporal_ok, declared, _ = (False, [], frames)
    if any(a in registry_mod.TEMPORAL_ACTIONS for a in candidates):
        temporal_ok, declared, _ = _capability_probe(
            capability_source, tenant_id, frames)
    allowed = []
    for action in candidates:
        if action not in registry_mod.TEMPORAL_ACTIONS:
            allowed.append(action)
        elif action == "regenerate_video":
            # 全镜重生成只需要"支持时序一致性"；没有窗口上界的要求。
            if temporal_ok:
                allowed.append(action)
        elif temporal_ok and any(_candidate_covers(c, frames)
                                 for c in declared):
            allowed.append(action)
    if not allowed:
        return _human("no_capable_route",
                      f"no candidate for {target['check_id']} can prove the "
                      f"required temporal capability")

    # 起点与升级（§3.1）。
    start = entry.get("default_action")
    promoted = registry_mod.escalate(entry, defect=target["defect"],
                                     media=media, extra=target.get("extra"))
    if promoted in allowed:
        start = promoted
    walk = registry_mod.outward(chain, start, allowed)
    routed = walk[0] if walk else _choose_by_cost(allowed, chain)

    # §4.3 动作选择：建议是输入，不是指令。
    chosen, conflict = _select_by_sources(target, allowed, chain)
    if conflict:
        return _human("action_conflict", conflict)
    return {"action": chosen or routed, "reason": None, "escalation": None,
            "candidates": allowed,
            "parameters": _parameters(chosen or routed, target, entry)}


def _human(reason: str, detail: str) -> dict:
    return {"action": "human_review", "reason": reason,
            "escalation": {"reason": reason, "detail": detail},
            "candidates": [], "parameters": {}}


def _select_by_sources(target: dict, allowed, chain) -> tuple[str | None,
                                                              str | None]:
    """§4.3 的 1→2→3。返回 `(action, conflict_reason)`。"""
    groups = [set(s.get("recommended_actions") or [])
              for s in target.get("sources") or [] if s]
    groups = [g for g in groups if g]
    if not groups:
        return None, None
    if len(groups) == 1:
        picked = _choose_by_cost(groups[0] & set(allowed), chain)
        return picked, None
    common = set.intersection(*groups)
    if common:
        return _choose_by_cost(common & set(allowed), chain), None
    primary = next((s for s in target.get("sources") or []
                    if s and s.get("primary")), None)
    if primary is None:
        return None, None
    suggestion = set(primary.get("recommended_actions") or [])
    picked = _choose_by_cost(suggestion & set(allowed), chain)
    if picked:
        return picked, None
    return None, (f"primary source {primary.get('service_id')} recommends "
                  f"{sorted(suggestion)}, none of which this defect can take")


def _parameters(action: str, target: dict, entry: dict) -> dict:
    """动作参数。**只放能从缺陷本身推出来的量**，不编供应商参数。"""
    span = target["defect"].get("time_range") or {}
    base = {"window_start_seconds": round(_pos(span.get("start_seconds")), 3),
            "window_end_seconds": round(_pos(span.get("end_seconds")), 3)}
    if action in ("temporal_edit", "keyframe_edit"):
        base["edit_kind"] = "trim"
    if action == "realign_subtitles":
        base["retime_kind"] = "shift"
    if action == "regenerate_video":
        base["shot_spec_revision"] = target.get("shot_spec_revision")
        base["prompt_template_id"] = target.get("prompt_template_id")
    if action == "redo_lipsync":
        base["lipsync_model_family"] = target.get("lipsync_model_family")
    for name in ("mask_region_bucket", "target_frame_bucket"):
        if entry.get(name) is not None:
            base[name] = entry[name]
    return base


def priority_score(*, defect: dict, action: str, scope: str | None,
                   recheck_scope: list[str]) -> int:
    """§4.4。只看能从输入算出来的量；算不出的按 0，不臆测。"""
    score = SEVERITY_WEIGHT.get(str(defect.get("severity")), 0)
    # "阻断正式交付"：blocker 就是它。产品对"仅阻断预览"没有机器可读标记，
    # 所以这里不加其余情形（宁可少加，也不虚构一个阻断性）。
    if defect.get("severity") == "blocker":
        score += BLOCKING_WEIGHT
    extents = {"frame": "single_frame", "whole_artifact": "whole_film"}
    extent = ("whole_film" if "technical" in recheck_scope
              and scope == "whole_artifact" else extents.get(str(scope),
                                                             "continuous_window"))
    score += RECHECK_COST.get(extent, 0)
    score -= ACTION_COST.get(action, ACTION_COST_UNDEFINED.get(action, 0))
    if action == "change_spec":
        # 改规格会改变上游，下游全失效（§4.4 dependency_penalty）。
        score -= 400
    return score


def _overlaps(left: dict, right: dict) -> bool:
    """两个缺陷的时间窗是否相交（合并判据）。算不出就当作不相交。"""
    a = left.get("time_range") or {}
    b = right.get("time_range") or {}
    a0, a1 = a.get("start_seconds"), a.get("end_seconds")
    b0, b1 = b.get("start_seconds"), b.get("end_seconds")
    if not all(_finite(v) for v in (a0, a1, b0, b1)):
        return False
    return float(a0) < float(b1) and float(b0) < float(a1)


def _parse_ts(text) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class RepairPlanningService:
    """S39 的写路径。**只出计划**：没有 artifact 写入口、没有付费调用、
    没有接受指针，也没有金额列。

    构造顺序不能改：`executescript(SCHEMA)` 与 `Outbox/Inbox.__init__` 都
    含 `executescript`（隐式提交），必须在任何 `BEGIN IMMEDIATE` 之前建一次，
    否则命令事务会被悄悄提交、ROLLBACK 失效。
    """

    def __init__(self, conn: sqlite3.Connection, *, policy_source,
                 report_sources: dict, capability_source,
                 continuity_source=None, run_source=None, registry=None,
                 qc_report_schema: dict | None = None,
                 execution_context_schema: dict | None = None,
                 enabled: bool = False, now=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)
        # 上游全部是注入的 callback（AGENTS.md：不跨库、不 import 别家 ORM）。
        self.policy_source = policy_source
        self.report_sources = dict(report_sources or {})
        self.capability_source = capability_source
        self.continuity_source = continuity_source
        self.run_source = run_source
        self.registry = registry
        self.qc_report_schema = qc_report_schema
        self.execution_context_schema = execution_context_schema
        # 生产开关默认关闭（AGENTS.md）。关着时命令 503，不产空计划：
        # 一张没有上限、没有 registry 的计划比没有计划更危险。
        self.enabled = bool(enabled)
        self._clock = now or _now
        self.metrics: dict[str, int] = {}

    def unavailable_dependencies(self) -> list[str]:
        """还没配的上游（按注入的桩判定）。

        `/health/readiness` 用它。一份报告读不到、策略问不到、预算查不到
        的时候 `create_plan` 一律 503；把这种状态报成 ready，编排层要等到
        第一次派单才发现。
        """
        sources = [self.policy_source, self.capability_source,
                   self.continuity_source, self.run_source,
                   *self.report_sources.values()]
        return sorted({getattr(source, "_unavailable_code", None)
                       for source in sources} - {None})

    # ------------------------------------------------------------ 工具
    def _bump(self, name: str, amount: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def _now(self) -> str:
        return self._clock()

    def _command(self, tenant_id: str, command_id: str, produce):
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    def _upstream(self, call, code: str):
        """上游不可达统一成 503，**绝不用缓存结论兜底**。"""
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

    def _require_ready(self) -> None:
        if not self.enabled:
            raise ServiceError(
                503, "REPAIR_PLANNING_DISABLED",
                "S39 is disabled (REPAIR_PLANNING_ENABLED is false); it will "
                "not emit a plan with invented caps or routes")
        if self.registry is None:
            raise ServiceError(
                503, "CHECK_REGISTRY_UNAVAILABLE",
                "check registry / action families are not configured; S39 "
                "will not guess a route (plan §11 C5)")
        if self.qc_report_schema is None:
            raise ServiceError(
                503, "QC_REPORT_SCHEMA_UNAVAILABLE",
                "qc-report.v2 schema is not configured; S39 cannot run the "
                "ingest gate and will not plan from unvalidated reports")

    # ------------------------------------------------------- 命令体校验
    def _context(self, tenant_id: str, body: dict) -> dict:
        context = body.get("execution_context")
        if not isinstance(context, dict):
            raise ServiceError(400, "INVALID_BODY",
                               "execution_context must be an object")
        problems = (validate_schema(context, self.execution_context_schema)
                    if self.execution_context_schema else [])
        if problems:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "execution_context is not execution-context v2",
                               details={"problems": problems[:20]})
        # 租户只认签名头；body 里的租户仅用于发现冲突，永不赋值。
        for candidate in (body.get("tenant_id"), context.get("tenant_id")):
            if candidate is not None and candidate != tenant_id:
                raise ServiceError(
                    403, "TENANT_MISMATCH",
                    "body tenant_id disagrees with the signed context")
        # `run_id` / `shot_lineage_id` 在 execution-context v2 里**不存在**
        # （该 schema `additionalProperties:false`，18 个键）。它们与 S37/S38
        # 一样放在命令体顶层（C28 / C4）。这里**在 schema 校验之后**并入
        # 内存里的 context：线缆上的 body 不变，只补本服务需要的两个坐标。
        # 不能塞进 body 再验——那样会 400 INVALID_CONTEXT。
        #
        # 拷一份再并：`request_hash` 是调用方**在调用前**按原样 body 算的，
        # 就地改 `body["execution_context"]` 会让同一份请求在下一次被哈希出
        # 另一个值（合法重投被判成"同键不同 body"）。只在内存里补坐标，
        # 调用方手里的 body 保持原样。
        context = dict(context)
        for name in ("run_id", "shot_lineage_id"):
            value = body.get(name)
            if value is not None and context.get(name) is None:
                context[name] = value
        return context

    def _snapshot(self, body: dict) -> tuple[str, list[dict]]:
        snapshot = body.get("qc_snapshot")
        if not isinstance(snapshot, dict):
            raise ServiceError(400, "INVALID_BODY",
                               "qc_snapshot must be an object")
        artifact = _hex64(snapshot.get("artifact_sha256"))
        if artifact is None:
            raise ServiceError(
                400, "INVALID_BODY",
                "qc_snapshot.artifact_sha256 must be 64 lowercase hex digits")
        reports = snapshot.get("reports")
        if not isinstance(reports, list) or not reports:
            raise ServiceError(400, "INVALID_BODY",
                               "qc_snapshot.reports must be a non-empty list")
        refs, seen = [], set()
        for item in reports:
            if not isinstance(item, dict) or not item.get("id"):
                raise ServiceError(400, "INVALID_BODY",
                                   "each report ref needs an id")
            service_id = str(item.get("service_id") or "").upper()
            if service_id not in REPORT_SERVICES:
                raise ServiceError(
                    400, "INVALID_BODY",
                    f"unknown report service {item.get('service_id')!r}")
            if service_id in seen:
                # 同一层两份不同的引用：谁是这一层的结论没有答案。
                raise ServiceError(
                    422, "DUPLICATE_REPORT_SERVICE",
                    f"qc_snapshot lists {service_id} twice")
            seen.add(service_id)
            refs.append({**item, "service_id": service_id})
        return artifact, refs

    def _aggregation_mode(self, body: dict) -> str:
        mode = body.get("aggregation_mode") or "wait_all"
        if mode != "wait_all":
            raise ServiceError(
                422, "UNSUPPORTED_AGGREGATION_MODE",
                f"aggregation_mode {mode!r} is not defined; only wait_all has "
                f"semantics in plan §4.1")
        return mode

    def _max_actions(self, body: dict) -> int:
        value = body.get("max_actions")
        if value is None:
            return 10
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ServiceError(400, "INVALID_BODY",
                               "max_actions must be a positive integer")
        return value

    # ------------------------------------------------------------ policy
    def _resolve_policy(self, tenant_id: str, context: dict) -> dict:
        """读 S02 已合并好的 `getEffectivePolicy`。**不做层级合并**（§5.4）。"""
        response = self._upstream(
            lambda: self.policy_source(tenant_id, {
                "project_id": context.get("project_id"),
                "series_id": context.get("series_id"),
                "branch_id": context.get("branch_id"),
                "episode_id": context.get("episode_id"),
                "node_id": context.get("node_id"),
                "policy_ref": context.get("policy_ref"),
            }),
            "POLICY_SOURCE_UNAVAILABLE")
        if not isinstance(response, dict):
            raise ServiceError(503, "POLICY_SOURCE_UNAVAILABLE",
                               "policy response is not a JSON object")
        effective = response.get("effective")
        if not isinstance(effective, dict):
            raise ServiceError(
                503, "POLICY_SOURCE_UNAVAILABLE",
                "policy response has no effective block; S39 will not invent "
                "repair_limits (plan §5.4)")
        limits = effective.get("repair_limits")
        limits = limits if isinstance(limits, dict) else {}

        partial = False
        episode_cap = limits.get("max_business_attempts_per_episode")
        if not isinstance(episode_cap, int) or isinstance(episode_cap, bool):
            episode_cap = DEFAULT_MAX_ATTEMPTS_PER_EPISODE
            partial = True
        run_cap = limits.get("max_business_attempts_per_run")
        if not isinstance(run_cap, int) or isinstance(run_cap, bool):
            run_cap = DEFAULT_MAX_ATTEMPTS_PER_RUN
            partial = True
        repeats = limits.get("max_identical_strategy_repeats")
        if not isinstance(repeats, int) or isinstance(repeats, bool):
            repeats = 1
        per_shot = limits.get("max_business_attempts_per_shot")
        if not isinstance(per_shot, int) or isinstance(per_shot, bool):
            per_shot = 3

        policy_ref = response.get("policy_ref")
        if not isinstance(policy_ref, dict):
            policy_ref = dict(context.get("policy_ref")
                              or {"service_id": "S02"})
        allowed = effective.get("allowed_actions")
        if not isinstance(allowed, list) or not all(
                isinstance(a, str) for a in allowed):
            allowed = None
        return {
            "effective": effective,
            "policy_ref": policy_ref,
            "policy_snapshot_id": str(policy_ref.get("id") or "unknown"),
            "policy_version": str(policy_ref.get("revision")
                                  if policy_ref.get("revision") is not None
                                  else response.get("source_revision") or "0"),
            "limits": {"shot": per_shot, "strategy": repeats,
                       "episode": episode_cap, "run": run_cap},
            "policy_partial": partial,
            "allowed_actions": allowed,
            "exception_behavior": str(
                effective.get("exception_behavior") or "wait_review"),
        }

    # ------------------------------------------------------- 读报告 + 闸门
    def _report_source(self, service_id: str):
        if service_id == "S17":
            if self.continuity_source is None:
                raise ServiceError(
                    503, "S17_REPORT_SOURCE_UNAVAILABLE",
                    "S17 continuity source is not configured; a report ref "
                    "that cannot be read is an error, not a silent skip")
            return self.continuity_source
        source = self.report_sources.get(service_id)
        if source is None:
            raise ServiceError(
                503, f"{service_id}_REPORT_SOURCE_UNAVAILABLE",
                f"no report source for {service_id}; S39 has no QC report to "
                f"plan from and will not fabricate one")
        return source

    def _read_reports(self, tenant_id: str, artifact_sha256: str,
                      refs: list[dict]) -> tuple[dict, dict]:
        """读 + 校验。返回 `(service_id -> report, service_id -> dispositions)`。

        按 **service_id** 而不是层来索引：S36 与 S17 都属于 `semantic` 层，
        按层索引会静默丢掉一份报告。

        任何一条报告不合格 → quarantine（落拒收事实 + metric + P1 计数），
        然后 422。**不 best-effort 解析**（§4.2）。
        """
        reports: dict[str, dict] = {}
        dispositions: dict[str, dict] = {}
        for ref in refs:
            service_id = ref["service_id"]
            layer = SERVICE_LAYER[service_id]
            source = self._report_source(service_id)
            document = self._upstream(
                lambda ref=ref, source=source: source(tenant_id, ref),
                f"{service_id}_REPORT_SOURCE_UNAVAILABLE")
            if not isinstance(document, dict):
                raise ServiceError(
                    503, f"{service_id}_REPORT_SOURCE_UNAVAILABLE",
                    "report response is not a JSON object")

            # C10：qc-report.v2 的 defect 是 additionalProperties:false，没有
            # disposition。只有 S17 文档带它，校验前剥掉；S35–S38 带了就拒收。
            body = document
            if service_id == "S17":
                body, dispositions[service_id] = _split_dispositions(document)
            elif any(isinstance(d, dict) and "disposition" in d
                     for d in (document.get("defects") or [])):
                self._quarantine(tenant_id, service_id, document, layer,
                                 "unexpected_disposition",
                                 "disposition is only accepted from S17 (C10)")
                raise ServiceError(
                    422, "QC_REPORT_REJECTED",
                    f"{service_id} carried a disposition field, which "
                    f"qc-report.v2 does not allow (C10)")

            problems = list(validate_schema(body, self.qc_report_schema))
            problems += [f"{p['reason']}: {p['detail']}" for p in
                         report_problems(body, registry=self.registry,
                                         layer=layer,
                                         artifact_sha256=artifact_sha256,
                                         tenant_id=tenant_id, ref=ref)]
            if problems:
                reason = problems[0].split(":")[0]
                self._quarantine(tenant_id, service_id, document, layer,
                                 reason, "; ".join(problems))
                raise ServiceError(
                    422, "QC_REPORT_REJECTED",
                    f"{service_id} report failed the ingest gate "
                    f"({reason}); it is quarantined and no plan is produced",
                    details={"reason": reason, "layer": layer,
                             "report_id": document.get("report_id"),
                             "problems": problems[:20]})
            reports[service_id] = body
        return reports, dispositions

    # ------------------------------------------------------- 聚合窗口 §4.1
    def _aggregate(self, reports: dict, dispositions: dict,
                   body: dict) -> dict:
        """四层里哪层到了、哪层缺、缺的层是不是 blocker 必需层。

        `wait_all` 在**同步命令**里没有"等"的余地：调用方给的是它手上已有的
        `qc_snapshot`。所以这里的语义是"按快照算齐不齐"，而不是"轮询十分钟"。
        真实等待由编排层（S21）在重投命令前完成——本服务不睡、不轮询。
        """
        present = {SERVICE_LAYER[sid] for sid in reports}
        pending = [layer for layer in REQUIRED_LAYERS if layer not in present]
        optional_pending = [layer for layer in ("subtitle",)
                            if layer not in present]

        # 必需层缺席时，缺的是不是 blocker 级检查的必需层？§4.1 说这种
        # 情况是 BLOCKED_INCOMPLETE + 人审。registry 里该层只要有 blocker
        # 可能（severity_max=blocker）就算必需；没有 registry 时无法证明，
        # 一律按必需处理（保守：宁可人审，不可少查一层就修）。
        blocked_by = []
        for layer in pending:
            owner = registry_mod.LAYER_OWNER.get(layer)
            entries = [self.registry.entry(c)
                       for c in (self.registry.checks_owned_by(owner)
                                 if owner else [])]
            if not entries or any(e and e.get("severity_max") == "blocker"
                                  for e in entries):
                blocked_by.append(layer)
        return {"present_layers": sorted(present), "pending_layers": pending,
                "optional_pending": optional_pending,
                "blocked_by": blocked_by}

    # ------------------------------------------------------- 归因 §4.3
    def _attribute(self, reports: dict, dispositions: dict,
                   media: dict) -> dict:
        """按 `defect_fingerprint` 去重成 target，并分出 suppressed 条目。"""
        targets: dict[str, dict] = {}
        suppressed: list[dict] = []
        reported_sources: dict[str, set[str]] = {}
        exception_clash: list[dict] = []

        for service_id in REPORT_SERVICES:
            report = reports.get(service_id)
            if report is None:
                continue
            for defect in report.get("defects") or []:
                fingerprint = str(defect.get("defect_fingerprint"))
                placement = (dispositions.get(service_id) or {}).get(fingerprint)
                disposition = (placement or {}).get("disposition") or "reported"
                if disposition != "reported":
                    suppressed.append({
                        "defect_fingerprint": fingerprint,
                        "check_id": defect.get("check_id"),
                        "disposition": disposition,
                        "reason": (placement or {}).get("reason"),
                        "report_id": report.get("report_id"),
                        "report_generation": report.get("report_generation"),
                        "service_id": service_id,
                    })
                    continue
                reported_sources.setdefault(fingerprint, set()).add(service_id)

                entry = targets.get(fingerprint)
                if entry is None:
                    # 指纹确定 target_id：同一缺陷在重算/重投时得到同一个 id，
                    # 否则事件里的 target_ids 会随重试漂移。
                    targets[fingerprint] = {
                        "target_id": "tgt_" + fingerprint[:20],
                        "defect_fingerprint": fingerprint,
                        "defect": defect,
                        "media": media,
                        "sources": [],
                        "severity": defect.get("severity"),
                        "window_frames": window_frames(defect, media),
                    }
                    entry = targets[fingerprint]
                else:
                    entry["severity"] = _severity_max(
                        str(entry["severity"]), str(defect.get("severity")))
                entry["sources"].append({
                    "service_id": service_id,
                    "report_id": report.get("report_id"),
                    "report_generation": report.get("report_generation"),
                    "check_id": defect.get("check_id"),
                    "recommended_actions":
                        list(defect.get("recommended_actions") or []),
                })

        # 同一指纹：S17 说 suppressed、别人说 reported（E24-R12）。不猜谁对。
        for item in suppressed:
            if item["defect_fingerprint"] in reported_sources:
                exception_clash.append({
                    "defect_fingerprint": item["defect_fingerprint"],
                    "suppressed_by": item["service_id"],
                    "reported_by": sorted(reported_sources[
                        item["defect_fingerprint"]]),
                })

        # primary_source：S17 > S36 > S38 > S37 > S35（S35 §1.1）。
        for entry in targets.values():
            order = {name: i for i, name in enumerate(SOURCE_PRIORITY)}
            entry["sources"].sort(key=lambda s: order.get(s["service_id"], 99))
            entry["primary_source"] = entry["sources"][0]["service_id"]
            entry["sources"][0]["primary"] = True
            entry["scope"] = entry["defect"].get("scope")
            entry["check_id"] = entry["defect"].get("check_id")
            entry["defect_type"] = entry["defect"].get("type")
        return {"targets": list(targets.values()), "suppressed": suppressed,
                "exception_clash": exception_clash}

    # ------------------------------------------------- 覆盖断言 §4.3.2
    def _coverage_gap(self, reports: dict) -> list[str]:
        """`expected - reported`，**只对本轮已到层的报告算**。

        §4.1 与 §4.3.2 在这里分工不同，不能互相覆盖：
        - **整层没到**（聚合窗口未齐）→ `pending_layers`，计划 `planned_partial`，
          缺的是 blocker 层则 `blocked_incomplete`（`_aggregate` / `_decide_state`）；
        - **层到了但报告里 `not_evaluated`/`failed`** → 这一层"该查没查成"，
          才是 §4.3.2 的 `coverage_gap`。

        不按"已到层"收窄的话，任何一层迟到都会先命中 coverage_gap，把
        `planned_partial` 变成 `escalated`——E25-R5 与 §4.1 都要求前者。
        """
        present = {SERVICE_LAYER[sid] for sid in reports}
        expected = [c for c in expected_checks(self.registry)
                    if (self.registry.entry(c) or {}).get("layer") in present]
        reported: set[str] = set()
        for report in reports.values():
            for entry in report.get("check_manifest") or []:
                if entry.get("status") in REPORTED_STATUSES:
                    reported.add(str(entry.get("check_id")))
        return sorted(set(expected) - reported)

    # ------------------------------------------------- 动作装配 §4.4
    def _assemble(self, attributed: dict, policy: dict, context: dict,
                  tenant_id: str, max_actions: int) -> list[dict]:
        """路由 + 打分 + 合并 + 拓扑序。返回 action 列表（未落库）。"""
        routed: list[dict] = []
        for target in attributed["targets"]:
            decision = route(
                target=target, registry=self.registry,
                policy_allowed=policy["allowed_actions"],
                capability_source=self.capability_source,
                tenant_id=tenant_id, media=target["media"])
            entry = self.registry.entry(target["check_id"]) or {}
            target["action"] = decision["action"]
            target["escalation"] = decision["escalation"]
            target["parameters"] = decision["parameters"]
            target["strategy_class"] = strategy_class(decision["action"],
                                                      decision["parameters"])
            target["recheck_scope"] = registry_mod.split_recheck_scope(
                entry.get("recheck_scope") or "")
            target["score"] = priority_score(
                defect=target["defect"], action=decision["action"],
                scope=target.get("scope"),
                recheck_scope=target["recheck_scope"])
            routed.append(target)

        # C26（E24-R14）：注册表缺项 / 登记不合法的点必须在**知道 check_id
        # 的地方**打点。`route()` 是模块级函数、拿不到 metrics，所以记在这里：
        # 每条这样的 target 恰好对应一次 `registry_entry_invalid` 人审，
        # 按 check_id 分桶，不合并、不吞掉。
        for target in routed:
            if (target["action"] == "human_review"
                    and (target.get("escalation") or {}).get("reason")
                    == "registry_entry_invalid"):
                self._bump(
                    f"registry_entry_invalid_total|{target['check_id']}")

        # 有人审的 target 不参与动作装配，但计划整体状态要升级。
        actionable = [t for t in routed if t["action"] != "human_review"]
        merges = self._merge(actionable)
        actions = []
        for group in merges:
            head = group[0]
            action = {
                "action_type": head["action"],
                "target_ids": [t["target_id"] for t in group],
                "parameters": dict(head["parameters"]),
                "capability_req": self._capability_requirement(head),
                "recheck_scope": sorted({s for t in group
                                         for s in t["recheck_scope"]}),
                "depends_on": [],   # 依赖由拓扑序补齐（见 _topo）
                "score": max(t["score"] for t in group),
                "targets": group,
            }
            actions.append(action)
        actions.sort(key=lambda a: (-a["score"], a["action_type"]))
        # id 先于拓扑序：`depends_on` 引用的是 action_id。
        for action in actions:
            action["action_id"] = _new_id("action")
        actions = self._topo(actions)
        for index, action in enumerate(actions, start=1):
            action["sequence"] = index
        return actions[:max_actions] if max_actions else actions

    def _capability_requirement(self, target: dict) -> dict:
        """§4.5 的 `capability_requirements`。只写真的会查的项（C19）。"""
        if target["action"] in registry_mod.TEMPORAL_ACTIONS:
            return {"temporal_consistency": True,
                    "max_window_frames": target.get("window_frames"),
                    "preserves_identity": True}
        return {}

    def _merge(self, targets: list[dict]) -> list[list[dict]]:
        """§4.4 第 2 步：同一动作能同时修多条时合并。

        合并条件严格：动作类型相同、策略类相同（同参数同策略），且窗口
        互相覆盖或同属一个全镜动作。**不同参数的同类动作不合并**——合并
        会把两次真实尝试记成一次（§5.3 的键正是靠参数区分的）。
        """
        groups: list[list[dict]] = []
        for target in sorted(targets, key=lambda t: (-t["score"],
                                                     t["target_id"])):
            placed = False
            for group in groups:
                head = group[0]
                if (head["action"] != target["action"]
                        or head["strategy_class"] != target["strategy_class"]):
                    continue
                if head["action"] in ("regenerate_video", "change_spec",
                                      "reingest", "transcode", "remix"):
                    # 全镜/全片动作：同一策略覆盖整条 artifact，可合并。
                    group.append(target)
                    placed = True
                    break
                if _overlaps(head["defect"], target["defect"]):
                    group.append(target)
                    placed = True
                    break
            if not placed:
                groups.append([target])
        return groups

    def _topo(self, actions: list[dict]) -> list[dict]:
        """§4.4 第 3 步：拓扑序。

        依赖只有一种、且可从动作语义证明：`regenerate_video` / `change_spec`
        会重写整条产物，任何针对旧产物的“更小”动作必须先于它，否则会被
        它覆盖。其余动作之间没有可证明的依赖——**不编造依赖**（编出来的
        依赖会让 S21 串行执行本可并行的动作，且无法从计划里看出理由）。
        """
        rewriting = {"regenerate_video", "change_spec"}
        prerequisites = [a for a in actions if a["action_type"] not in rewriting]
        rewrites = [a for a in actions if a["action_type"] in rewriting]
        for action in rewrites:
            action["depends_on"] = [p["action_id"] for p in prerequisites
                                    if p.get("action_id")]
        return prerequisites + rewrites

    # --------------------------------------------------- 上限 §5.6/§5.7
    def _used(self, tenant_id: str, context: dict, shot_lineage_id: str,
              strategy_class_: str | None = None,
              exclude_plan: str | None = None) -> dict:
        """已消耗：台账 + **未兑现**的预留。**键里没有 run/manifest/epoch/fence**。

        `reserved` 一定算；`committed` 只有**台账里还没有对应那一笔**时才算：
        生成事件把预留标 `committed` 的同时会写一行台账，两个都数就是同一次
        业务尝试算两遍（E26-R14 的 `shot_total` 会凭空翻倍）。反过来，复检
        pass 也会把预留标 `committed` 而台账没有行——那种情况必须继续占额度，
        所以不能简单只数 `reserved`。`released` 是升级时释放掉的额度，不算。

        `exclude_plan`：本计划自己刚落的预留不算"已消耗"。不排除就会在
        同一张计划的第 2 个 target 上把第 1 个 target 记两次（`_authorize`
        另有 `reserved_in_plan` 记同计划内的累加），一个动作被算成两次。
        """
        key = (tenant_id, context["series_id"], context["branch_id"],
               shot_lineage_id)
        not_mine = "" if exclude_plan is None else \
            "AND r.repair_plan_id<>?"
        not_mine_args = () if exclude_plan is None else (exclude_plan,)
        # 一格仍要占额度 = 还处于 reserved，或已 committed 但台账里没有它。
        # 匹配用台账的真实唯一键（不含 run/fence/epoch）。
        live = (
            "(r.state='reserved' OR (r.state='committed' AND NOT EXISTS ("
            "SELECT 1 FROM attempt_ledger l WHERE l.tenant_id=r.tenant_id "
            "AND l.series_id=r.series_id AND l.branch_id=r.branch_id "
            "AND l.shot_lineage_id=r.shot_lineage_id "
            "AND l.strategy_class=r.strategy_class "
            "AND l.attempt_seq=r.attempt_seq)))")
        shot_total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM attempt_ledger WHERE tenant_id=? AND "
            "series_id=? AND branch_id=? AND shot_lineage_id=?",
            key).fetchone()["n"]
        shot_total += self.conn.execute(
            "SELECT COUNT(*) AS n FROM attempt_reservation r WHERE "
            "r.tenant_id=? AND r.series_id=? AND r.branch_id=? AND "
            "r.shot_lineage_id=? AND " + live + " AND r.expires_at > ? " +
            not_mine, (*key, self._now(), *not_mine_args)).fetchone()["n"]
        strategy_total = 0
        if strategy_class_ is not None:
            strategy_total = self.conn.execute(
                "SELECT COUNT(*) AS n FROM attempt_ledger WHERE tenant_id=? "
                "AND series_id=? AND branch_id=? AND shot_lineage_id=? AND "
                "strategy_class=?",
                (*key, strategy_class_)).fetchone()["n"]
            strategy_total += self.conn.execute(
                "SELECT COUNT(*) AS n FROM attempt_reservation r WHERE "
                "r.tenant_id=? AND r.series_id=? AND r.branch_id=? AND "
                "r.shot_lineage_id=? AND r.strategy_class=? AND " + live +
                " AND r.expires_at > ? " + not_mine,
                (*key, strategy_class_, self._now(),
                 *not_mine_args)).fetchone()["n"]
        episode_total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM attempt_ledger WHERE tenant_id=? AND "
            "series_id=? AND branch_id=? AND episode_id=?",
            (tenant_id, context["series_id"], context["branch_id"],
             context["episode_id"])).fetchone()["n"]
        episode_total += self.conn.execute(
            "SELECT COUNT(*) AS n FROM attempt_reservation r JOIN plan p "
            "ON p.plan_id = r.repair_plan_id WHERE p.tenant_id=? AND "
            "p.series_id=? AND p.branch_id=? AND p.episode_id=? AND " + live +
            " AND r.expires_at > ? " + not_mine,
            (tenant_id, context["series_id"], context["branch_id"],
             context["episode_id"], self._now(),
             *not_mine_args)).fetchone()["n"]
        return {"shot_total": shot_total, "strategy_total": strategy_total,
                "episode_total": episode_total}

    def _run_precheck(self, tenant_id: str, run_id: str) -> dict:
        """Level 4 的**读取预检**。只读，不 reserve，不修改任何东西。"""
        if self.run_source is None:
            return {"state": "unavailable",
                    "detail": "S21 has no repair-quota read face (plan §11 C6); "
                              "the run-level cap is enforced by S21 at "
                              "execution time"}
        try:
            payload = self._upstream(lambda: self.run_source(tenant_id, run_id),
                                     "RUN_SOURCE_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status >= 500:
                return {"state": "unavailable", "detail": str(exc)}
            raise
        if not isinstance(payload, dict):
            return {"state": "unavailable",
                    "detail": "run status response is not an object"}
        remaining = payload.get("remaining_repair_attempts")
        exhausted = payload.get("budget_exhausted") is True
        if exhausted or (isinstance(remaining, int) and remaining < 1):
            return {"state": "exhausted", "remaining": remaining,
                    "budget_exhausted": exhausted}
        return {"state": "ok", "remaining": remaining,
                "budget_exhausted": False}

    def _authorize(self, *, tenant_id: str, context: dict,
                   actions: list[dict], policy: dict, plan_id: str,
                   dry_run: bool, run_state: dict) -> dict:
        """四级上限：全部满足才 ALLOW。**唯一约束是最后一道闸**（§5.6）。

        `run_state` 由调用方在**事务之外**取好传进来（Level 4 是一次网络读，
        不能拿着写锁等 S21）。
        """
        limits = policy["limits"]
        decisions: list[dict] = []
        taken: list[dict] = []
        reserved_in_plan: dict[tuple, int] = {}

        # **按 action 计数，不是按 target 计数。** 一次合并后的
        # `regenerate_video` 同时修两条缺陷，仍是一次业务尝试（§4.4 第 2 步：
        # "合并为一个 action，target_ids 两条，不消耗两次计数"，E24-R9）。
        # `attempt_ledger` 一行带 `defect_fingerprints` 数组，也是这个语义。
        # `_merge` 保证同一 action 里所有 target 的 strategy_class 相同。
        for index, action in enumerate(actions):
            shot = context["shot_lineage_id"]
            strategy = action["targets"][0]["strategy_class"]
            slot = (shot, strategy)
            used = self._used(tenant_id, context, shot, strategy,
                              exclude_plan=plan_id)
            # 同一张计划里已占的槽也算进去，否则一张 4 动作的计划能
            # 一次把同一镜头推到上限之上。
            extra = reserved_in_plan.get(slot, 0)
            strategy_total = used["strategy_total"] + extra
            shot_extra = sum(v for (s, _), v in reserved_in_plan.items()
                             if s == shot)
            shot_total = used["shot_total"] + shot_extra

            denial = None
            if shot_total + 1 > limits["shot"]:
                denial = ("shot_attempt_cap", shot_total, limits["shot"])
            elif strategy_total + 1 > limits["strategy"] + 1:
                denial = ("identical_strategy_cap", strategy_total,
                          limits["strategy"] + 1)
            elif used["episode_total"] + len(taken) + 1 > limits["episode"]:
                denial = ("episode_attempt_cap", used["episode_total"],
                          limits["episode"])
            elif run_state["state"] == "exhausted":
                denial = ("run_cap_or_budget", None, None)

            if denial is not None:
                reason, attempts_used, limit = denial
                self._bump(f"repair_authorize_denied_total|{reason}")
                return {
                    "allowed": False,
                    "escalation": {
                        "reason": reason,
                        "capped_at": {
                            "shot_lineage_id": shot,
                            "strategy_class": strategy,
                            "attempts_used": attempts_used,
                            "limit": limit,
                        },
                        # 被拒的这一条 action 里的 target 也还没派出去。
                        "remaining_targets": [
                            t["target_id"] for a in actions[index:]
                            for t in a["targets"]],
                        "handoff": ("S40"
                                    if policy["exception_behavior"]
                                    != "isolate_branch"
                                    else "isolate_branch"),
                    },
                    "decisions": decisions,
                    "run_precheck": run_state,
                }

            seq = strategy_total + 1
            slot_id = _new_id("slot")
            if not dry_run:
                try:
                    self.conn.execute(
                        "INSERT INTO attempt_reservation (reservation_id, "
                        "tenant_id, series_id, branch_id, shot_lineage_id, "
                        "strategy_class, attempt_seq, repair_plan_id, "
                        "state, expires_at, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,'reserved',?,?)",
                        (slot_id, tenant_id, context["series_id"],
                         context["branch_id"], shot, strategy, seq,
                         plan_id, _fmt(_now_dt() + timedelta(hours=1)),
                         self._now()))
                except sqlite3.IntegrityError:
                    # 并发计划同时要同一个 (key, seq)：唯一约束只放一个。
                    self._bump("repair_authorize_denied_total|"
                               "attempt_slot_taken")
                    return {
                        "allowed": False,
                        "escalation": {
                            "reason": "attempt_slot_taken",
                            "capped_at": {
                                "shot_lineage_id": shot,
                                "strategy_class": strategy,
                                "attempts_used": seq,
                                "limit": limits["strategy"] + 1,
                            },
                            "remaining_targets": [
                                t["target_id"] for a in actions[index:]
                                for t in a["targets"]],
                            "handoff": "S40",
                        },
                        "decisions": decisions,
                        "run_precheck": run_state,
                    }
            reserved_in_plan[slot] = extra + 1
            action["attempt_seq"] = seq
            action["attempt_slot_id"] = slot_id
            taken.append({"shot_lineage_id": shot,
                          "strategy_class": strategy, "attempt_seq": seq,
                          "attempt_slot_id": slot_id})
            for target in action["targets"]:
                target["attempt_seq"] = seq
                target["attempt_slot_id"] = slot_id
                decisions.append({"target_id": target["target_id"],
                                  "action": target["action"],
                                  "attempt_seq": seq,
                                  "strategy_class": target["strategy_class"]})
        return {"allowed": True, "escalation": None, "decisions": decisions,
                "run_precheck": run_state}

    # --------------------------------------------------- 死循环 §5.9
    def _no_progress(self, tenant_id: str, shot_lineage_id: str,
                     fingerprint_set_: str) -> bool:
        """24h 内同一指纹集合已有 ≥2 张计划 → 这一张就是"没进展"。

        §5.9 的判据只看**集合有没有变**，不看计数——计数怎么涨都不影响它。
        """
        window = _fmt(_now_dt() - timedelta(hours=24))
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM plan WHERE tenant_id=? AND dry_run=0 "
            "AND fingerprint_set=? AND snapshot IS NOT NULL AND "
            "created_at >= ? AND json_extract(snapshot, '$.shot_lineage_id')=?",
            (tenant_id, fingerprint_set_, window, shot_lineage_id)).fetchone()
        return int(row["n"]) >= 2

    # ------------------------------------------------------------ 命令
    def create_plan(self, tenant_id: str, command_id: str, body: dict,
                    request_hash: str) -> dict:
        """`POST /repair-plans`。

        网络读全部在事务**之外**：`BEGIN IMMEDIATE` 期间不能拿着锁等 S02 /
        S35–S38 / S24。只有最后落库那一段进事务。
        """
        self._require_ready()
        context = self._context(tenant_id, body)
        artifact_sha256, refs = self._snapshot(body)
        self._aggregation_mode(body)
        max_actions = self._max_actions(body)
        dry_run = body.get("dry_run") is True

        policy = self._resolve_policy(tenant_id, context)
        reports, dispositions = self._read_reports(tenant_id, artifact_sha256,
                                                   refs)
        # C28：`run_id` 只在 execution-context v2 之外（命令体顶层）。主动
        # 拒绝而不是留给 `context["run_id"]` 抛 KeyError——那个异常在
        # `_command` 里会变成 500，看起来像本服务的 bug 而不是请求的问题。
        run_id = str(context.get("run_id") or "")
        if not run_id:
            raise ServiceError(
                422, "RUN_UNKNOWN",
                "run_id is required at the top level of the command body; "
                "execution-context v2 has no run_id (C28)")
        run_state = self._run_precheck(tenant_id, run_id)
        return self._command(tenant_id, command_id, lambda: self._plan(
            tenant_id=tenant_id, command_id=command_id, context=context,
            artifact_sha256=artifact_sha256, reports=reports,
            dispositions=dispositions, policy=policy, request_hash=request_hash,
            max_actions=max_actions, dry_run=dry_run, run_state=run_state))

    # -------------------------------------------------------- 计划装配
    def _plan(self, *, tenant_id: str, command_id: str, context: dict,
              artifact_sha256: str, reports: dict, dispositions: dict,
              policy: dict, request_hash: str, max_actions: int,
              dry_run: bool, run_state: dict) -> dict:
        media = _media_of(list(reports.values()))
        attributed = self._attribute(reports, dispositions, media)

        defects_for_fingerprint = [{"check_id": t["check_id"],
                                    "window_start": (t["defect"].get(
                                        "time_range") or {}).get(
                                            "start_seconds"),
                                    "window_end": (t["defect"].get(
                                        "time_range") or {}).get("end_seconds"),
                                    "severity": t["severity"]}
                                   for t in attributed["targets"]]
        fingerprint = plan_fingerprint(
            artifact_sha256=artifact_sha256,
            defects=defects_for_fingerprint,
            registry_version=self.registry.version,
            policy_snapshot_id=policy["policy_snapshot_id"],
            policy_version=policy["policy_version"])
        fingerprint_set_ = fingerprint_set(
            [t["defect_fingerprint"] for t in attributed["targets"]])

        # §7.1：同指纹返回已有计划（幂等），不同即新计划。
        existing = self.conn.execute(
            "SELECT * FROM plan WHERE tenant_id=? AND plan_fingerprint=? AND "
            "dry_run=?", (tenant_id, fingerprint, int(dry_run))).fetchone()
        if existing is not None:
            self._bump("repair_plan_reused_total")
            return self._summary(existing, replayed_plan=True)

        self._check_shot_lineage(context, reports)

        aggregate = self._aggregate(reports, dispositions, {})
        abstained = sorted(
            sid for sid, report in reports.items()
            if report.get("decision") == "abstain")
        for layer in (SERVICE_LAYER[sid] for sid in abstained):
            if layer in REQUIRED_LAYERS and layer not in aggregate["blocked_by"]:
                aggregate["blocked_by"].append(layer)

        actions = self._assemble(attributed, policy, context, tenant_id,
                                 max_actions)
        coverage_gap = self._coverage_gap(reports)
        no_progress = (not dry_run) and self._no_progress(
            tenant_id, context["shot_lineage_id"], fingerprint_set_)
        if no_progress:
            self._bump("repair_plan_no_progress_total")

        plan_id = _new_id("plan")
        authorization = self._authorize(
            tenant_id=tenant_id, context=context, actions=actions,
            policy=policy, plan_id=plan_id, dry_run=dry_run,
            run_state=run_state)

        escalation, state, deferred = self._decide_state(
            aggregate=aggregate, attributed=attributed,
            coverage_gap=coverage_gap, no_progress=no_progress,
            authorization=authorization, policy=policy)
        if deferred:
            self._bump("repair_plan_targets_deferred_total",
                       len(deferred))
        if state == "planned" and aggregate["pending_layers"]:
            state = "planned_partial"

        snapshot = {
            "shot_lineage_id": context["shot_lineage_id"],
            "aggregation_mode": "wait_all",
            "present_layers": aggregate["present_layers"],
            "pending_layers": aggregate["pending_layers"],
            "optional_pending_layers": aggregate["optional_pending"],
            "blocked_by_layers": aggregate["blocked_by"],
            "abstained_services": abstained,
            "reports": [{"service_id": sid, "report_id": r.get("report_id"),
                         "report_generation": r.get("report_generation"),
                         "qc_layer": r.get("qc_layer"),
                         "decision": r.get("decision"),
                         "content_hash": r.get("report_content_hash")}
                        for sid, r in sorted(reports.items())],
            "targets": [self._target_summary(t) for t in attributed["targets"]],
            "suppressed_defects": attributed["suppressed"],
            "coverage_gap": coverage_gap,
            "deferred_targets": [t.get("target_id") for t in deferred],
            "run_precheck": authorization["run_precheck"],
            "authorization": authorization["decisions"],
            "limits": policy["limits"],
            "policy_partial": policy["policy_partial"],
            "policy_version": policy["policy_version"],
            "registry_version": self.registry.version,
            "routing_algorithm_version": ROUTING_ALGORITHM_VERSION,
        }
        actions_payload = [self._action_summary(a) for a in actions]
        band = self._cost_band(actions_payload)
        top_action = actions_payload[0]["action"] if actions_payload else \
            "human_review"

        now = self._now()
        self.conn.execute(
            "INSERT INTO plan (plan_id, tenant_id, series_id, branch_id, "
            "episode_id, run_id, manifest_id, manifest_revision, "
            "execution_fence, node_epoch, artifact_sha256, state, "
            "aggregation_deadline, policy_ref, policy_version, policy_partial, "
            "registry_version, routing_algorithm_version, plan_fingerprint, "
            "fingerprint_set, snapshot, escalation, dry_run, created_at, "
            "updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, tenant_id, context["series_id"], context["branch_id"],
             context["episode_id"], context["run_id"],
             (context.get("manifest_ref") or {}).get("id", "unknown"),
             int((context.get("manifest_ref") or {}).get("revision", 1)),
             int(context["execution_fence"]), int(context["node_epoch"]),
             artifact_sha256, state,
             _fmt(_now_dt() + timedelta(
                 seconds=DEFAULT_AGGREGATION_DEADLINE_SECONDS)),
             _jcs(policy["policy_ref"]), policy["policy_version"],
             int(policy["policy_partial"]), self.registry.version,
             ROUTING_ALGORITHM_VERSION, fingerprint, fingerprint_set_,
             _jcs(snapshot), None if escalation is None else _jcs(escalation),
             int(dry_run), now, now))
        for action in actions:
            self.conn.execute(
                "INSERT INTO action (plan_id, action_id, sequence, action_type, "
                "target_ids, parameters, capability_req, recheck_scope, "
                "attempt_slot_id, depends_on) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (plan_id, action["action_id"], action["sequence"],
                 action["action_type"], _jcs(action["target_ids"]),
                 _jcs(action["parameters"]),
                 _jcs(action["capability_req"]), _jcs(action["recheck_scope"]),
                 action.get("attempt_slot_id"),
                 _jcs([d for d in action["depends_on"] if d])))

        if not dry_run:
            self._emit_planned(
                plan_id=plan_id, tenant_id=tenant_id, command_id=command_id,
                context=context, artifact_sha256=artifact_sha256,
                state=state, target_count=len(attributed["targets"]),
                action_count=len(actions), top_action=top_action, band=band,
                recheck_scope=sorted({s for a in actions_payload
                                      for s in a["recheck_scope"]}),
                escalation=escalation)
            if state in ("escalated", "blocked_incomplete"):
                self._emit_escalated(plan_id=plan_id, tenant_id=tenant_id,
                                     command_id=command_id, context=context,
                                     artifact_sha256=artifact_sha256,
                                     state=state, escalation=escalation)
        self._bump(f"repair_plan_created_total|{state}")
        result = {
            "plan_id": plan_id, "state": state,
            "artifact_sha256": artifact_sha256,
            "shot_lineage_id": context["shot_lineage_id"],
            "target_count": len(attributed["targets"]),
            "action_count": len(actions),
            "top_action": top_action,
            "estimated_cost_band": band,
            "recheck_scope": sorted({s for a in actions_payload
                                     for s in a["recheck_scope"]}),
            "pending_layers": aggregate["pending_layers"],
            "escalation": escalation,
            "dry_run": dry_run,
            "policy_partial": policy["policy_partial"],
            "run_precheck": authorization["run_precheck"],
            "suppressed_count": len(attributed["suppressed"]),
            "coverage_gap": coverage_gap,
        }
        return json.loads(_jcs(result))

    def _check_shot_lineage(self, context: dict, reports: dict) -> None:
        """C16：一张计划只接受一个 `shot_lineage_id`。

        报告里的 lineage 与命令上下文不一致 → 422，不静默取第一个。
        """
        declared = str(context.get("shot_lineage_id") or "")
        if not declared:
            raise ServiceError(422, "SHOT_LINEAGE_UNKNOWN",
                               "execution_context.shot_lineage_id is required; "
                               "it is the cross-run counter anchor (C4)")
        seen = set()
        for report in reports.values():
            value = (report.get("context") or {}).get("shot_lineage_id")
            if value:
                seen.add(str(value))
        others = seen - {declared}
        if others:
            raise ServiceError(
                422, "MULTIPLE_SHOT_LINEAGES",
                "the reports cover more than one shot_lineage_id; one plan "
                "cannot represent them (C16)",
                details={"declared": declared, "others": sorted(others)})

    def _decide_state(self, *, aggregate, attributed, coverage_gap,
                      no_progress, authorization, policy):
        """状态与交接信息。**顺序即优先级**，理由写在这里而不是散在分支里。"""
        if aggregate["blocked_by"]:
            return ({"reason": "missing_required_layer",
                     "missing_layers": aggregate["blocked_by"],
                     "pending_layers": aggregate["pending_layers"],
                     "handoff": "S40"}, "blocked_incomplete", [])
        if attributed["exception_clash"]:
            return ({"reason": "exception_not_propagated",
                     "clashes": attributed["exception_clash"],
                     "handoff": "S40"}, "escalated", [])
        if coverage_gap:
            return ({"reason": "coverage_gap", "gap": coverage_gap,
                     "handoff": "S40"}, "escalated", [])
        if no_progress:
            return ({"reason": "no_progress",
                     "detail": "three plans within 24h with an unchanged "
                               "defect fingerprint set; counters are not the "
                               "problem, the repair is not working",
                     "handoff": "S40"}, "escalated", [])
        if not authorization["allowed"]:
            return (authorization["escalation"], "escalated", [])
        humans = [t for t in attributed["targets"]
                  if t.get("action") == "human_review"]
        if humans:
            first = humans[0]["escalation"] or {}
            return ({"reason": first.get("reason") or "human_review_requested",
                     "detail": first.get("detail"),
                     "human_review_targets": [t["target_id"] for t in humans],
                     "handoff": ("S40"
                                 if policy["exception_behavior"]
                                 != "isolate_branch" else "isolate_branch")},
                    "escalated", [])
        return None, "planned", []

    def _target_summary(self, target: dict) -> dict:
        return {
            "target_id": target.get("target_id"),
            "defect_fingerprint": target["defect_fingerprint"],
            "check_id": target.get("check_id"),
            "defect_type": target.get("defect_type"),
            "severity": target["severity"],
            "primary_source": target.get("primary_source"),
            "contributing_sources": [s["service_id"]
                                     for s in target.get("sources") or []],
            "action": target.get("action"),
            "strategy_class": target.get("strategy_class"),
            "recheck_scope": target.get("recheck_scope") or [],
            "priority_score": target.get("score"),
            "attempt_seq": target.get("attempt_seq"),
            "attempt_slot_id": target.get("attempt_slot_id"),
            "escalation": target.get("escalation"),
        }

    def _action_summary(self, action: dict) -> dict:
        return {
            "action_id": action["action_id"],
            "sequence": action.get("sequence"),
            "action": action["action_type"],
            "target_ids": action["target_ids"],
            "parameters": action["parameters"],
            "capability_requirements": action["capability_req"],
            "recheck_scope": action["recheck_scope"],
            "attempt_slot_id": action.get("attempt_slot_id"),
            "depends_on": [d for d in action["depends_on"] if d],
        }

    def _cost_band(self, actions: list[dict]) -> str:
        """档位只由动作代价表推出（§4.4），**不是金额**。"""
        if not actions:
            return "low"
        total = sum(ACTION_COST.get(a["action"],
                                    ACTION_COST_UNDEFINED.get(a["action"], 0))
                    for a in actions)
        if total >= 300:
            return "high"
        return "medium" if total >= 80 else "low"

    def _summary(self, row, *, replayed_plan: bool = False) -> dict:
        snapshot = _loads(row["snapshot"], {})
        actions = [
            {"action_id": r["action_id"], "sequence": r["sequence"],
             "action": r["action_type"],
             "target_ids": _loads(r["target_ids"], []),
             "parameters": _loads(r["parameters"], {}),
             "capability_requirements": _loads(r["capability_req"], {}),
             "recheck_scope": _loads(r["recheck_scope"], []),
             "attempt_slot_id": r["attempt_slot_id"],
             "depends_on": _loads(r["depends_on"], [])}
            for r in self.conn.execute(
                "SELECT * FROM action WHERE plan_id=? ORDER BY sequence",
                (row["plan_id"],))]
        band = self._cost_band(actions)
        return {
            "plan_id": row["plan_id"],
            "state": row["state"],
            "artifact_sha256": row["artifact_sha256"],
            "shot_lineage_id": snapshot.get("shot_lineage_id"),
            "target_count": len(snapshot.get("targets") or []),
            "action_count": len(actions),
            "top_action": actions[0]["action"] if actions else "human_review",
            "estimated_cost_band": band,
            "recheck_scope": sorted({s for a in actions
                                     for s in a["recheck_scope"]}),
            "actions": actions,
            "pending_layers": snapshot.get("pending_layers") or [],
            "escalation": _loads(row["escalation"], None),
            "dry_run": bool(row["dry_run"]),
            "policy_partial": bool(row["policy_partial"]),
            "run_precheck": snapshot.get("run_precheck"),
            "suppressed_count": len(snapshot.get("suppressed_defects") or []),
            "coverage_gap": snapshot.get("coverage_gap") or [],
            "reused_plan": replayed_plan,
        }

    # ------------------------------------------------------------ 事件
    def _emit_planned(self, *, plan_id, tenant_id, command_id, context,
                      artifact_sha256, state, target_count, action_count,
                      top_action, band, recheck_scope, escalation) -> None:
        """`quality.repair.planned`。13 个必需键，`additionalProperties:false`。"""
        data = {
            "schema_version": SCHEMA_VERSION,
            "plan_id": plan_id,
            "state": state,
            "artifact_sha256": artifact_sha256,
            "shot_lineage_id": context["shot_lineage_id"],
            "episode_id": context["episode_id"],
            "run_id": context["run_id"],
            "target_count": int(target_count),
            "action_count": int(action_count),
            "top_action": top_action,
            "estimated_cost_band": band,
            "recheck_scope": list(recheck_scope),
            # RPP-1/RPP-5：事件在 `planned` 里也带 escalation 键（契约要求）；
            # 非 planned 状态时它同时说明"这张计划不是可执行的"。
            "escalation": escalation,
        }
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_PLANNED,
            subject=str(context.get("node_id") or context["shot_lineage_id"]),
            tenant_id=tenant_id,
            home_cell=str(context.get("home_cell") or "unknown"),
            aggregate_id=plan_id,
            sequence=self._next_sequence(plan_id),
            correlation_id=str(context.get("run_id") or command_id),
            causation_id=command_id,
            data=data,
            data_ref={"service_id": SOURCE, "id": plan_id, "revision": 1,
                      "content_hash": _sha256_hex(_jcs(data))}))

    def _emit_escalated(self, *, plan_id, tenant_id, command_id, context,
                        artifact_sha256, state, escalation) -> None:
        """`quality.repair.escalated`（C1：`event-topics.v2.json` 还没有它）。"""
        data = {
            "schema_version": SCHEMA_VERSION,
            "plan_id": plan_id,
            "state": state,
            "artifact_sha256": artifact_sha256,
            "shot_lineage_id": context["shot_lineage_id"],
            "episode_id": context["episode_id"],
            "run_id": context["run_id"],
            "escalation": escalation,
        }
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_ESCALATED,
            subject=str(context.get("node_id") or context["shot_lineage_id"]),
            tenant_id=tenant_id,
            home_cell=str(context.get("home_cell") or "unknown"),
            aggregate_id=plan_id,
            sequence=self._next_sequence(plan_id),
            correlation_id=str(context.get("run_id") or command_id),
            causation_id=command_id,
            data=data))

    def _next_sequence(self, plan_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE aggregate_id=?",
            (plan_id,)).fetchone()
        return int(row["n"]) + 1

    # ------------------------------------------------------------ 查询
    def get_plan(self, tenant_id: str, plan_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM plan WHERE tenant_id=? AND plan_id=?",
            (tenant_id, plan_id)).fetchone()
        if row is None:
            # 跨租户与不存在**完全同一个 404**（E26 的意图：不泄漏存在性）。
            raise ServiceError(404, "PLAN_NOT_FOUND",
                               f"repair plan {plan_id} does not exist")
        return self.get_plan_detail(row)

    def get_plan_detail(self, row) -> dict:
        summary = self._summary(row)
        snapshot = _loads(row["snapshot"], {})
        summary.update({
            "targets": snapshot.get("targets") or [],
            "actions": summary.pop("actions", None) or [
                {"action_id": r["action_id"], "sequence": r["sequence"],
                 "action": r["action_type"],
                 "target_ids": _loads(r["target_ids"], []),
                 "parameters": _loads(r["parameters"], {}),
                 "capability_requirements": _loads(r["capability_req"], {}),
                 "recheck_scope": _loads(r["recheck_scope"], []),
                 "attempt_slot_id": r["attempt_slot_id"],
                 "depends_on": _loads(r["depends_on"], [])}
                for r in self.conn.execute(
                    "SELECT * FROM action WHERE plan_id=? ORDER BY sequence",
                    (row["plan_id"],))],
            "suppressed_defects": snapshot.get("suppressed_defects") or [],
            "snapshot_reports": snapshot.get("reports") or [],
            "limits": snapshot.get("limits") or {},
            "recheck_jobs": [
                {"recheck_id": r["recheck_id"], "round": r["round"],
                 "state": r["state"], "artifact_sha256": r["artifact_sha256"],
                 "scope": _loads(r["scope"], []),
                 "attempt_slot_id": r["attempt_slot_id"],
                 "report_refs": _loads(r["report_refs"], None)}
                for r in self.conn.execute(
                    "SELECT * FROM recheck_job WHERE plan_id=? AND tenant_id=? "
                    "ORDER BY round", (row["plan_id"], row["tenant_id"]))],
        })
        return summary

    def list_plans(self, tenant_id: str, shot_lineage_id: str) -> dict:
        """`GET /repair-plans?shot_lineage_id=`：跨 Run 的完整尝试历史。"""
        if not shot_lineage_id:
            raise ServiceError(400, "INVALID_BODY",
                               "shot_lineage_id is required")
        rows = [r for r in self.conn.execute(
            "SELECT * FROM plan WHERE tenant_id=? AND "
            "json_extract(snapshot, '$.shot_lineage_id')=? "
            "ORDER BY created_at DESC", (tenant_id, shot_lineage_id))]
        return {"shot_lineage_id": shot_lineage_id,
                "plans": [self._summary(r) for r in rows],
                "attempts": [
                    {"attempt_id": r["attempt_id"],
                     "strategy_class": r["strategy_class"],
                     "attempt_seq": r["attempt_seq"],
                     "episode_id": r["episode_id"],
                     "provider_job_id": r["provider_job_id"],
                     "outcome": r["outcome"],
                     "repair_plan_id": r["repair_plan_id"],
                     "action_id": r["action_id"],
                     "run_id": r["run_id"],
                     "created_at": r["created_at"]}
                    for r in self.conn.execute(
                        "SELECT * FROM attempt_ledger WHERE tenant_id=? AND "
                        "shot_lineage_id=? ORDER BY created_at, attempt_seq",
                        (tenant_id, shot_lineage_id))]}

    def escalate(self, tenant_id: str, plan_id: str,
                 reason: str = "manual_escalation") -> dict:
        """人工/系统强制升级。**幂等**：已 escalated 再调不重复发事件。"""
        row = self.conn.execute(
            "SELECT * FROM plan WHERE tenant_id=? AND plan_id=?",
            (tenant_id, plan_id)).fetchone()
        if row is None:
            raise ServiceError(404, "PLAN_NOT_FOUND",
                               f"repair plan {plan_id} does not exist")
        if row["state"] == "escalated":
            self._bump("repair_plan_escalate_noop_total")
            return {"plan_id": plan_id, "state": row["state"],
                    "escalation": _loads(row["escalation"], None),
                    "changed": False}
        if row["state"] in ("resolved", "human_resolved", "superseded"):
            # 已经结束的计划再升级会改写历史结论，拒绝而不是覆盖。
            raise ServiceError(
                409, "PLAN_TERMINAL",
                f"plan {plan_id} is {row['state']}; escalating it would "
                f"overwrite a terminal outcome")
        escalation = {"reason": reason, "handoff": "S40",
                      "previous_state": row["state"]}
        snapshot = _loads(row["snapshot"], {})
        now = self._now()
        self.conn.execute(
            "UPDATE plan SET state='escalated', escalation=?, updated_at=? "
            "WHERE tenant_id=? AND plan_id=?",
            (_jcs(escalation), now, tenant_id, plan_id))
        # 未执行的预留要释放，否则会永久占用上限（预留在事务内 INSERT，
        # 释放也必须同事务，避免并发读到"既 reserved 又 released"）。
        self.conn.execute(
            "UPDATE attempt_reservation SET state='released' WHERE "
            "repair_plan_id=? AND state='reserved'", (plan_id,))
        if not row["dry_run"]:
            self._emit_escalated(
                plan_id=plan_id, tenant_id=tenant_id,
                command_id=f"escalate:{plan_id}",
                context={"shot_lineage_id": snapshot.get("shot_lineage_id"),
                         "episode_id": row["episode_id"],
                         "run_id": row["run_id"]},
                artifact_sha256=row["artifact_sha256"], state="escalated",
                escalation=escalation)
        self._bump("repair_plan_escalated_total")
        return {"plan_id": plan_id, "state": "escalated",
                "escalation": escalation, "changed": True}

    # ------------------------------------------------------------ 复检 §6
    def submit_recheck(self, tenant_id: str, plan_id: str, body: dict) -> dict:
        """投递一次复检结论（C18：没有"这是复检"的事件，所以给个入口）。

        复检**不是**修复失败，也不能当 pass 放行：
        `abstain(evaluator_timeout)` 重跑且不消耗业务尝试；其余 abstain 升级；
        pass 且原指纹消失 → `resolved`，但**只交出一句"候选可接受"**——
        接受指针在 S08，本服务没有一行会去推进它。
        """
        row = self.conn.execute(
            "SELECT * FROM plan WHERE tenant_id=? AND plan_id=?",
            (tenant_id, plan_id)).fetchone()
        if row is None:
            raise ServiceError(404, "PLAN_NOT_FOUND",
                               f"repair plan {plan_id} does not exist")
        if row["state"] in ("resolved", "human_resolved", "superseded"):
            raise ServiceError(
                409, "PLAN_TERMINAL",
                f"plan {plan_id} is already {row['state']}")

        snapshot = _loads(row["snapshot"], {})
        limits = snapshot.get("limits") or {}
        max_rounds = int(limits.get("shot") or 3) + 1
        used_rounds = self.conn.execute(
            "SELECT COUNT(*) AS n FROM recheck_job WHERE plan_id=?",
            (plan_id,)).fetchone()["n"]
        round_no = int(used_rounds) + 1

        artifact = _hex64(body.get("artifact_sha256"))
        if artifact is None:
            raise ServiceError(
                400, "INVALID_BODY",
                "artifact_sha256 of the repaired artifact is required")
        reports = body.get("reports")
        if not isinstance(reports, list) or not reports:
            raise ServiceError(400, "INVALID_BODY",
                               "reports must be a non-empty list")
        context = self._context(tenant_id, body)

        decisions = {}
        for ref in reports:
            service_id = str(ref.get("service_id") or "").upper()
            source = self._report_source(service_id)
            document = self._upstream(
                lambda ref=ref, source=source: source(tenant_id, ref),
                f"{service_id}_REPORT_SOURCE_UNAVAILABLE")
            if not isinstance(document, dict):
                raise ServiceError(
                    503, f"{service_id}_REPORT_SOURCE_UNAVAILABLE",
                    "report response is not a JSON object")
            repair_context = document.get("repair_context")
            if not isinstance(repair_context, dict) or \
                    repair_context.get("repair_plan_id") != plan_id:
                raise ServiceError(
                    422, "RECHECK_NOT_BOUND",
                    f"{service_id} report does not carry this plan's "
                    f"repair_context; an unbound report is a fresh ingest, "
                    f"not a recheck (plan §6.1)",
                    details={"service_id": service_id,
                             "report_id": document.get("report_id")})
            problems = validate_schema(document, self.qc_report_schema)
            if problems:
                self._quarantine(tenant_id, service_id, document,
                                 SERVICE_LAYER.get(service_id, "unknown"),
                                 "recheck_schema", "; ".join(problems[:5]))
                raise ServiceError(
                    422, "QC_REPORT_REJECTED",
                    f"{service_id} recheck report failed the ingest gate",
                    details={"problems": problems[:20]})
            decisions[service_id] = {
                "decision": document.get("decision"),
                "abstain_reason": (document.get("abstain") or {}).get(
                    "reason_code"),
                "fingerprints": {str(d.get("defect_fingerprint"))
                                 for d in document.get("defects") or []},
                "report_id": document.get("report_id"),
            }

        self.conn.execute(
            "INSERT INTO recheck_job (recheck_id, tenant_id, plan_id, "
            "attempt_slot_id, artifact_sha256, scope, round, state, "
            "report_refs, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_new_id("recheck"), tenant_id, plan_id,
             str(body.get("attempt_slot_id") or "unknown"), artifact,
             _jcs(sorted({s for a in snapshot.get("targets") or []
                          for s in a.get("recheck_scope") or []})),
             round_no, "completed",
             _jcs({sid: d["report_id"] for sid, d in decisions.items()}),
             self._now()))

        if round_no > max_rounds:
            return self._escalate_from_recheck(
                tenant_id, row, snapshot, "recheck_rounds_exhausted",
                {"rounds_used": round_no - 1, "limit": max_rounds})

        timed_out = [sid for sid, d in decisions.items()
                     if d["decision"] == "abstain"
                     and d["abstain_reason"] == "evaluator_timeout"]
        if timed_out:
            # 重跑复检，**不消耗业务尝试**——这是算力/网络问题，不是修复失败。
            self.conn.execute(
                "UPDATE recheck_job SET state='failed' WHERE plan_id=? AND "
                "round=?", (plan_id, round_no))
            self._bump("recheck_retry_total")
            return {"plan_id": plan_id, "outcome": "recheck_retry",
                    "consume_business_attempt": False,
                    "retry_services": timed_out,
                    "state": row["state"]}

        abstained = [sid for sid, d in decisions.items()
                     if d["decision"] == "abstain"]
        if abstained:
            return self._escalate_from_recheck(
                tenant_id, row, snapshot, "recheck_abstain",
                {"services": abstained,
                 "reasons": {sid: decisions[sid]["abstain_reason"]
                             for sid in abstained}})

        human = [sid for sid, d in decisions.items()
                 if d["decision"] == "human_review"]
        if human:
            return self._escalate_from_recheck(
                tenant_id, row, snapshot, "human_review_requested",
                {"services": human})

        original = {t["defect_fingerprint"]
                    for t in snapshot.get("targets") or []}
        seen = set().union(*(d["fingerprints"] for d in decisions.values()))
        passed = all(d["decision"] == "pass" for d in decisions.values())

        if passed and not (original & seen):
            # 原指纹全部消失。**只标 resolved 并交出候选**；S08 才是权威。
            self.conn.execute(
                "UPDATE plan SET state='resolved', updated_at=? WHERE "
                "tenant_id=? AND plan_id=?",
                (self._now(), tenant_id, plan_id))
            self.conn.execute(
                "UPDATE attempt_reservation SET state='committed' WHERE "
                "repair_plan_id=? AND state='reserved'", (plan_id,))
            self._bump("recheck_resolved_total")
            return {"plan_id": plan_id, "outcome": "resolved",
                    "state": "resolved",
                    "accept_candidate": True,
                    "accept_authority": "S08",
                    "accept_pointer_touched": False,
                    "consume_business_attempt": False}

        if passed:
            # pass 但出现了原指纹之外的新缺陷：新指纹、新计数，不动原额度。
            self._bump("recheck_new_defects_total")
            return {"plan_id": plan_id, "outcome": "new_defects",
                    "new_fingerprints": sorted(seen - original),
                    "replan_required": True,
                    "consume_business_attempt": False,
                    "state": row["state"]}

        repeated = sorted(original & seen)
        self.conn.execute(
            "UPDATE plan SET state='superseded', updated_at=? WHERE "
            "tenant_id=? AND plan_id=?", (self._now(), tenant_id, plan_id))
        self._bump("recheck_repeat_total")
        return {"plan_id": plan_id, "outcome": "same_defects",
                "repeated_fingerprints": repeated,
                # 计数已由 `REPAIR_ATTEMPT_STARTED` 记过；复检不再加一次。
                "consume_business_attempt": False,
                "replan_required": True, "state": "superseded"}

    def _escalate_from_recheck(self, tenant_id, row, snapshot, reason,
                               detail) -> dict:
        plan_id = row["plan_id"]
        escalation = {"reason": reason, "detail": detail,
                      "handoff": "S40"}
        self.conn.execute(
            "UPDATE plan SET state='escalated', escalation=?, updated_at=? "
            "WHERE tenant_id=? AND plan_id=?",
            (_jcs(escalation), self._now(), tenant_id, plan_id))
        self.conn.execute(
            "UPDATE attempt_reservation SET state='released' WHERE "
            "repair_plan_id=? AND state='reserved'", (plan_id,))
        if not row["dry_run"]:
            self._emit_escalated(
                plan_id=plan_id, tenant_id=tenant_id,
                command_id=f"recheck:{plan_id}",
                context={"shot_lineage_id": snapshot.get("shot_lineage_id"),
                         "episode_id": row["episode_id"],
                         "run_id": row["run_id"]},
                artifact_sha256=row["artifact_sha256"], state="escalated",
                escalation=escalation)
        self._bump(f"recheck_escalated_total|{reason}")
        return {"plan_id": plan_id, "outcome": "escalated",
                "state": "escalated", "escalation": escalation,
                "consume_business_attempt": False}

    # ------------------------------------------------- 计数事件 §5.5
    def consume_generation_event(self, tenant_id: str, envelope: dict) -> dict:
        """`generation.job.state-changed` → 业务尝试 +1 的唯一入口。

        +1 的条件只有一条：**这次尝试真的被提交了**。
        - `DISPATCH_AUTHORIZED` 且是业务尝试、不是网络重试 → +1
        - `SUBMISSION_UNKNOWN` → 保持 pending，既不 +1 也不放行重提
        - 之后对账 `CONFIRMED_SUBMITTED` → 补记 +1；`CONFIRMED_NOT_SUBMITTED` → +0
        - 重投同一事件 / 同一 `attempt_id` 再次到达 → 不重复（唯一索引，D5）
        """
        event_id = str(envelope.get("id") or "")
        if not event_id:
            raise ServiceError(400, "INVALID_BODY", "event id is required")
        envelope_tenant = envelope.get("tenant_id")
        if envelope_tenant is not None and envelope_tenant != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "event tenant disagrees with the signed context")
        if not self.inbox.consume(CONSUMER_GENERATION, event_id):
            self._bump("generation_event_duplicate_total")
            return {"counted": False, "reason": "duplicate_event"}
        data = envelope.get("data") or {}
        if not isinstance(data, dict):
            raise ServiceError(400, "INVALID_BODY",
                               "event data must be an object")
        return self._count_from_generation(tenant_id, data,
                                           str(envelope.get("causation_id")
                                               or event_id))

    def _count_from_generation(self, tenant_id: str, data: dict,
                               causation: str) -> dict:
        state = str(data.get("state") or "")
        reason_code = str(data.get("reason_code") or "")
        attempt_id = str(data.get("attempt_id") or "")
        if not attempt_id:
            raise ServiceError(400, "INVALID_BODY",
                               "generation event has no attempt_id")

        existing = self.conn.execute(
            "SELECT ledger_id FROM attempt_ledger WHERE attempt_id=?",
            (attempt_id,)).fetchone()
        if existing is not None:
            self._bump("generation_event_already_counted_total")
            return {"counted": False, "reason": "attempt_already_counted",
                    "attempt_id": attempt_id}

        if data.get("network_retry") is True or data.get("business_attempt") is False:
            return {"counted": False, "reason": "not_a_business_attempt"}
        if state == "SUBMISSION_UNKNOWN":
            # AGENTS.md：不确定时不重提、不记账，等对账。
            self._bump("generation_event_submission_unknown_total")
            return {"counted": False, "reason": "submission_unknown",
                    "pending": True, "attempt_id": attempt_id}
        if reason_code == "CONFIRMED_NOT_SUBMITTED":
            return {"counted": False, "reason": "confirmed_not_submitted"}
        if state != "DISPATCH_AUTHORIZED" and \
                reason_code != "CONFIRMED_SUBMITTED":
            return {"counted": False, "reason": "not_authorized",
                    "state": state}

        plan_id = data.get("repair_plan_id")
        slot_id = data.get("attempt_slot_id")
        reservation = None
        if slot_id:
            reservation = self.conn.execute(
                "SELECT * FROM attempt_reservation WHERE reservation_id=? AND "
                "tenant_id=?", (str(slot_id), tenant_id)).fetchone()
        if plan_id is None and reservation is not None:
            plan_id = reservation["repair_plan_id"]
        if not plan_id:
            # C14：事件不带计划关联，台账列全 NOT NULL，无法在不伪造
            # series/branch/shot_lineage 的前提下归属。记 metric，不落假行。
            self._bump("repair_attempt_unattributed_total")
            return {"counted": False, "reason": "unattributed",
                    "detail": "generation event carries no repair_plan_id; "
                              "see CONTRACT_GAPS C14"}
        row = self.conn.execute(
            "SELECT * FROM plan WHERE tenant_id=? AND plan_id=?",
            (tenant_id, str(plan_id))).fetchone()
        if row is None:
            return {"counted": False, "reason": "unattributed",
                    "detail": "repair_plan_id does not belong to this tenant"}
        snapshot = _loads(row["snapshot"], {})
        shot = snapshot.get("shot_lineage_id")
        strategy = str(data.get("strategy_class") or "")
        if not strategy and reservation is not None:
            strategy = reservation["strategy_class"]
        if not strategy:
            action_id = data.get("action_id")
            hit = None
            if action_id:
                hit = self.conn.execute(
                    "SELECT action_type, parameters FROM action WHERE "
                    "plan_id=? AND action_id=?", (row["plan_id"], action_id)
                ).fetchone()
            if hit is None:
                targets = snapshot.get("targets") or []
                strategy = targets[0]["strategy_class"] if targets else ""
                action_id = None
            else:
                strategy = strategy_class(hit["action_type"],
                                          _loads(hit["parameters"], {}))
        if not strategy or not shot:
            self._bump("repair_attempt_unattributed_total")
            return {"counted": False, "reason": "unattributed",
                    "detail": "no strategy_class / shot_lineage_id to attribute"}
        if reservation is None:
            # C14：`generation.job.state-changed` v2 里**没有** `attempt_slot_id`
            # 字段（S26 也不知道 S39 的预留 id）。认领本计划已预留的那一格：
            # 否则台账记一笔、预留仍算一笔，同一次业务尝试被数两遍，
            # `attempt_seq` 还会跳过被占用的号。
            # 只在本计划、本镜头、本策略、仍 reserved 的格子里找——跨计划
            # 认领会把别人的槽位标 committed。
            reservation = self.conn.execute(
                "SELECT * FROM attempt_reservation WHERE tenant_id=? AND "
                "repair_plan_id=? AND shot_lineage_id=? AND strategy_class=? "
                "AND state='reserved' AND expires_at > ? "
                "ORDER BY attempt_seq LIMIT 1",
                (tenant_id, row["plan_id"], shot, strategy,
                 self._now())).fetchone()
            if reservation is not None:
                # 台账的 `action_id` 也从这一格所属的动作补齐。
                slot_action = self.conn.execute(
                    "SELECT action_id FROM action WHERE plan_id=? AND "
                    "attempt_slot_id=?", (row["plan_id"],
                                          reservation["reservation_id"])
                ).fetchone()
                if slot_action is not None and not data.get("action_id"):
                    data = {**data, "action_id": slot_action["action_id"]}
        if reservation is not None:
            seq = reservation["attempt_seq"]
        else:
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(attempt_seq),0) AS n FROM attempt_ledger "
                "WHERE tenant_id=? AND series_id=? AND branch_id=? AND "
                "shot_lineage_id=? AND strategy_class=?",
                (tenant_id, row["series_id"], row["branch_id"], shot,
                 strategy)).fetchone()["n"] + 1
        try:
            self.conn.execute(
                "INSERT INTO attempt_ledger (ledger_id, tenant_id, series_id, "
                "branch_id, episode_id, shot_lineage_id, strategy_class, "
                "attempt_seq, attempt_id, provider_job_id, run_id, manifest_id, "
                "node_epoch, execution_fence, repair_plan_id, action_id, "
                "defect_fingerprints, outcome, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_new_id("ledger"), tenant_id, row["series_id"],
                 row["branch_id"], row["episode_id"], shot, strategy, seq,
                 attempt_id, data.get("provider_job_id"),
                 row["run_id"], row["manifest_id"],
                 int(data.get("node_epoch") or row["node_epoch"]),
                 int(data.get("execution_fence") or row["execution_fence"]),
                 row["plan_id"], str(data.get("action_id") or "unknown"),
                 _jcs(sorted({t["defect_fingerprint"]
                              for t in snapshot.get("targets") or []})),
                 "pending", self._now()))
        except sqlite3.IntegrityError:
            # 同一 attempt_id 再从另一个 event_id 进来：唯一索引挡住。
            self._bump("generation_event_already_counted_total")
            return {"counted": False, "reason": "attempt_already_counted",
                    "attempt_id": attempt_id}
        if reservation is not None:
            self.conn.execute(
                "UPDATE attempt_reservation SET state='committed' WHERE "
                "reservation_id=?", (reservation["reservation_id"],))
        self._bump("repair_attempt_counted_total")
        return {"counted": True, "attempt_id": attempt_id,
                "attempt_seq": seq, "strategy_class": strategy,
                "shot_lineage_id": shot, "repair_plan_id": row["plan_id"],
                "backfilled": reason_code == "CONFIRMED_SUBMITTED"}

    def consume_review_decision(self, tenant_id: str,
                                envelope: dict) -> dict:
        """`review.decision.recorded`：**`recorded` ≠ `accepted`**。

        只把计划标 `human_resolved`。接受指针的推进是 S08 的 CAS——这里没有
        任何一行碰它，返回值里也写死 `accept_pointer_touched: false`。
        """
        event_id = str(envelope.get("id") or "")
        if not event_id:
            raise ServiceError(400, "INVALID_BODY", "event id is required")
        if envelope.get("tenant_id") not in (None, tenant_id):
            raise ServiceError(403, "TENANT_MISMATCH",
                               "event tenant disagrees with the signed context")
        if not self.inbox.consume(CONSUMER_REVIEW, event_id):
            return {"applied": False, "reason": "duplicate_event",
                    "accept_pointer_touched": False}
        data = envelope.get("data") or {}
        if not isinstance(data, dict):
            raise ServiceError(400, "INVALID_BODY",
                               "event data must be an object")
        decided_against = data.get("decided_against") or {}
        plan_id = (decided_against.get("repair_plan_id")
                   if isinstance(decided_against, dict) else None) or \
            data.get("repair_plan_id")
        if not plan_id:
            return {"applied": False, "reason": "no_plan_reference",
                    "accept_pointer_touched": False}
        row = self.conn.execute(
            "SELECT * FROM plan WHERE tenant_id=? AND plan_id=?",
            (tenant_id, str(plan_id))).fetchone()
        if row is None:
            return {"applied": False, "reason": "plan_not_found",
                    "accept_pointer_touched": False}
        if data.get("status") != "RECORDED":
            # 只有 RECORDED 是"人已看过并留了决定"；APPLIED/STALE/... 是
            # S40 自己的状态机，本服务不据此改计划。
            return {"applied": False, "reason": "status_not_recorded",
                    "status": data.get("status"),
                    "accept_pointer_touched": False}
        snapshot = _loads(row["snapshot"], {})
        snapshot.setdefault("review_decisions", []).append({
            "decision_id": data.get("decision_id"),
            "review_task_id": data.get("review_task_id"),
            "status": "RECORDED",
            "recorded_at": self._now()})
        now = self._now()
        self.conn.execute(
            "UPDATE plan SET state='human_resolved', snapshot=?, updated_at=? "
            "WHERE tenant_id=? AND plan_id=?",
            (_jcs(snapshot), now, tenant_id, row["plan_id"]))
        self._bump("plan_human_resolved_total")
        return {"applied": True, "plan_id": row["plan_id"],
                "state": "human_resolved",
                "decision_id": data.get("decision_id"),
                "accept_pointer_touched": False,
                "accept_authority": "S08"}

    def _quarantine(self, tenant_id: str, service_id: str, document: dict,
                    layer: str, reason: str, detail: str) -> None:
        """拒收是审计事实（C11）：落库 + 计数 + P1。**不存报告正文**。"""
        self.conn.execute(
            "INSERT INTO qc_rejection (rejection_id, tenant_id, service_id, "
            "report_id, layer, reason, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_new_id("rejection"), tenant_id, service_id,
             str(document.get("report_id") or "unknown"), layer, reason,
             detail[:2000], self._now()))
        self._bump(f"qc_report_rejected_total|{reason}|{layer}")
        self._bump("p1_alert|qc_report_rejected")


def _split_dispositions(document: dict) -> tuple[dict, dict]:
    """把 S17 文档里的 `disposition` 剥出来，正文保持 qc-report.v2 形状（C10）。

    返回 `(body, {defect_fingerprint: {disposition, reason, check_id}})`。
    """
    body = dict(document)
    defects, mapping = [], {}
    for defect in document.get("defects") or []:
        if not isinstance(defect, dict):
            defects.append(defect)
            continue
        clean = {k: v for k, v in defect.items()
                 if k not in ("disposition", "disposition_reason")}
        if "disposition" in defect:
            mapping[str(defect.get("defect_fingerprint"))] = {
                "disposition": defect.get("disposition"),
                "reason": defect.get("disposition_reason"),
                "check_id": defect.get("check_id"),
            }
        defects.append(clean)
    body["defects"] = defects
    return body, mapping
