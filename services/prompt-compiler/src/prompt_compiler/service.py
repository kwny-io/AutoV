"""S20 prompt-compiler 内核。

本服务只做一件事：把**已钉住的**镜头、模板、adapter、绑定集、能力快照、对齐
报告，确定性地翻译成一份可提交给供应商的 prompt，并留下可审计的 manifest。

四条贯穿全文的性质：

1. **确定性。** 同样的输入 + 同样的 pin ⇒ 同样的 `content_hash`。编译路径里
   没有时间、没有随机、没有集合遍历、没有 `repr` 之外的格式化。`content_hash`
   的投影是显式列出来的（`manifest_content_hash`），不是"把整个 dict 序列化"。
2. **对白零变换。** 对白只能经 `verbatim` 出去，逐字节追加；允许的唯一差异是
   adapter 决定的说话人前缀，且前缀不在那一段 `text` 里（§4.2 第 1 条）。
3. **不猜。** 上游缺 → 503；词表缺 → 报错；超范围 → 拒绝，不裁剪；适配器
   缺 → 报错，不从"相似模型"推断。所有"看起来能用"的降级都必须显式配在
   adapter 里并留下 warning。
4. **不是权威。** S20 不保管资产内容、不保管模型状态、不保管对白对齐。它只按
   pin 读一次、记下 hash、然后不再回看。

`node_epoch` 与 `execution_fence` 是**标签**，不是哈希的一部分（§4.1）：它们是
"这次编译属于哪个重生成分支"的路由信息，不改变产物字节。把它们塞进
`content_hash` 会让同一个 prompt 在每次局部重生成时"变了"。

七步顺序（§3.1）不能交换，理由写在每一步的上方。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from prompt_compiler.render import (
    RenderError,
    check_coverage,
    estimate_tokens,
    join_fragments,
    render_sections,
)
from prompt_compiler.schema import (
    CONTRACT_GAPS,
    ID_PREFIX,
    MAX_PROBE_SNAPSHOTS,
    MODALITIES,
    RENDER_TYPES,
    SHOT_REQUIRED,
    TEMPLATE_STATES,
    WARNING_CODES,
    SCHEMA,
)

SOURCE = "S20"
SCHEMA_VERSION = "2.0"
EVENT_PROMPT_COMPILED = "prompt.compiled"
TOPIC_PROMPT = "drama.prompt-compiler.v2"

# 视觉参考的角色白名单。`voice` 不在这张图里：它不是参考图，是音频资产，
# 走 S31。混进 references[] 会让下游把它当图去下载。
VISUAL_REFERENCE_ROLES = ("character", "costume", "scene", "prop")

UNSUPPORTED_SUPPORT = ("unsupported", "field_failed")

DEFAULT_SECTION_SEPARATOR = "\n"


# ---------------------------------------------------------------- 基础件

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    """固定宽度 UTC：毫秒 + `Z`。全文只有这一个时间格式。"""
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(payload) -> str:
    """RFC 8785 的可用子集：排序键、无空格、不转义非 ASCII。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha256_hex(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def _hash_of(payload) -> str:
    return _sha256_hex(_jcs(payload))


def _new_id(kind: str) -> str:
    prefix = ID_PREFIX.get(kind)
    if prefix is None:
        raise ValueError(f"unknown id kind {kind!r}")
    return prefix + uuid.uuid4().hex[:24]


def _loads(text, default=None):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _row(row):
    return dict(row) if row is not None else None


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` 包一层。**只能包一次**：Outbox 的 DDL 用
    `executescript`，那会隐式 COMMIT，嵌套时会破坏 ROLLBACK。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def _upstream(call, code: str, *, empty_ok: bool = False):
    """调一个上游闭包，把"拿不到"统一翻成 503。

    - `ConnectionError` → 503 retryable
    - 上游自身 5xx 的 `ServiceError` → 503 retryable，保留 details
    - `None`（404） → 503，除非 `empty_ok`

    为什么 404 也变 503：S20 的输入全是 pin 过的 revision。一个 pin 过的
    revision 查不到，说明上游状态与 pin 不一致 —— 这是可以重试重新解析的
    情况，不是"用户请求错了"。真正的用户错误在服务层用 409/422 表达。
    """
    try:
        result = call()
    except ConnectionError as exc:
        raise ServiceError(503, code, str(exc), retryable=True) from exc
    except ServiceError as exc:
        if exc.status >= 500:
            raise ServiceError(503, exc.code if exc.code != "UPSTREAM_ERROR"
                               else code,
                               str(exc), retryable=True,
                               details=exc.details) from exc
        raise
    if result is None and not empty_ok:
        raise ServiceError(503, code,
                           "the pinned upstream revision could not be read; "
                           "refusing to compile without it", retryable=True)
    return result


# ---------------------------------------------------------------- 哈希投影

def manifest_content_hash(manifest: dict) -> str:
    """manifest 的 `content_hash`（§4.1 + S20-45）。

    显式投影，不 `_hash_of(manifest)`：
    - 排除 `manifest_id` / `revision` / `compiled_at`：身份与时间不是内容。
    - 排除 `node_id` / `node_epoch` / `execution_fence`：标签（§4.1）。
    - 排除 `tokens_estimate`：tokenizer 升级会改数字，产物没变。
    - **包含** `dialogue_alignment_ref`（S20-45）：不同对齐是不同产物，漏了它
      会让两份不同对齐的 manifest 撞进 `manifest_input_once`。

    整份 `warnings` 与 `derived_from` 都进：它们是内容的一部分 —— 两个 manifest
    一个带 warning 一个不带，不是同一份产物（§3.6 末段）。
    """
    shot_ref = manifest.get("shot_ref") or {}
    template_ref = manifest.get("template_source_ref") or {}
    adapter_ref = manifest.get("adapter_ref") or {}
    capability_ref = manifest.get("capability_snapshot_ref") or {}
    binding_ref = manifest.get("binding_set_ref") or {}
    alignment_ref = manifest.get("dialogue_alignment_ref")

    projection = {
        "shot_content_hash": shot_ref.get("content_hash"),
        "template_source_hash": template_ref.get("content_hash"),
        "adapter_content_hash": adapter_ref.get("content_hash"),
        "model_snapshot": manifest.get("model_snapshot"),
        "capability_snapshot_hash": capability_ref.get("content_hash"),
        "binding_set_hash": binding_ref.get("content_hash"),
        "dialogue_alignment_ref": (
            None if alignment_ref is None else {
                "id": alignment_ref.get("id"),
                "revision": alignment_ref.get("revision"),
                "content_hash": alignment_ref.get("content_hash"),
            }),
        "prompt_text": manifest.get("prompt_text"),
        "negative_prompt": manifest.get("negative_prompt"),
        "references": [
            {"position": ref.get("position"), "role": ref.get("role"),
             "token": ref.get("token"),
             "artifact_ref": ref.get("artifact_ref")}
            for ref in manifest.get("references") or []
        ],
        "dialogue_segments": [
            {"line_id": seg.get("line_id"),
             "text_sha256": seg.get("text_sha256")}
            for seg in manifest.get("dialogue_segments") or []
        ],
        "duration": manifest.get("duration"),
        "parameters": manifest.get("parameters"),
        "derived_from": manifest.get("derived_from"),
        "warnings": manifest.get("warnings"),
    }
    return _hash_of(projection)


def template_content_hash(template: dict) -> str:
    """模板正文的 `content_hash`（§1.1 + S20-40）。

    排除 `state` / `published_at` / `author_subject`：它们描述的是**谁在什么
    时候发布了它**，不是它是什么。发布一份未改动的 DRAFT 不应该产生新的内容
    哈希 —— 否则回归集与 adapter 区间全部失效。

    **包含** `adapter_scopes`（S20-40）：它驱动探测与 adapter 选择，是内容。
    """
    projection = {
        "compatible_schema": template.get("compatible_schema"),
        "modality": template.get("modality"),
        "provider": template.get("provider"),
        "model_snapshots": sorted(template.get("model_snapshots") or []),
        "adapter_scopes": sorted(template.get("adapter_scopes") or []),
        "body": template.get("body"),
        "adapter_revision_min": template.get("adapter_revision_min"),
        "adapter_revision_max": template.get("adapter_revision_max"),
        # 整个 `regression_set_ref`，不是只有它的 `content_hash`：
        # 回归集是**发布门**，"在 rgs@1 上通过"与"在 rgs@2 上通过"是两次
        # 不同的事实（T20-67）。只投影 `content_hash` 时，换了版本号但内容
        # 相同的两次发布会撞进同一个 hash —— 下游无法分辨哪一版被哪套断言
        # 背书。`id` 同理：换一个回归集不是"同一版重跑"。
        "regression_set_ref": {
            "id": (template.get("regression_set_ref") or {}).get("id"),
            "revision": (template.get("regression_set_ref") or {}).get(
                "revision"),
            "content_hash": (template.get("regression_set_ref") or {}).get(
                "content_hash"),
        },
    }
    return _hash_of(projection)


def adapter_content_hash(adapter: dict) -> str:
    projection = {
        "adapter_id": adapter.get("adapter_id"),
        "provider": adapter.get("provider"),
        "model_snapshot": adapter.get("model_snapshot"),
        "revision": adapter.get("revision"),
        "field_map": adapter.get("field_map") or {},
        "phrase_tables": adapter.get("phrase_tables") or {},
        "reference_order_rule": adapter.get("reference_order_rule"),
        "reference_token_format": adapter.get("reference_token_format"),
        "token_budget": adapter.get("token_budget"),
        "unsupported_optionals": sorted(
            adapter.get("unsupported_optionals") or []),
        "speaker_prefix": adapter.get("speaker_prefix"),
        "section_separator": adapter.get("section_separator"),
        "parameter_ranges": adapter.get("parameter_ranges") or {},
        "content_type": adapter.get("content_type"),
    }
    return _hash_of(projection)


# ---------------------------------------------------------------- 选择/排序

SNAPSHOT_RE = __import__("re").compile(
    r"^(\*|[a-z0-9][a-z0-9.-]*@[0-9]{4}-[0-9]{2})$")


def _check_snapshot(value: str, *, allow_wildcard: bool = False) -> str:
    """`name@YYYY-MM`，或（仅 adapter）`'*'`。

    §5.2 的 CHECK 在 Postgres 用正则；SQLite 没有 REGEXP，所以这条放在
    Python 写入路径（C11）。运行动态校验不能只靠 DDL —— 那在两种引擎上不等价。
    """
    if not isinstance(value, str) or not SNAPSHOT_RE.match(value):
        raise ServiceError(400, "INVALID_SCHEMA",
                           f"model_snapshot {value!r} is not name@YYYY-MM")
    if value == "*" and not allow_wildcard:
        raise ServiceError(400, "INVALID_SCHEMA",
                           "model_snapshot '*' is only valid on an adapter")
    return value


def select_adapter(adapters: list, *, provider: str, model_snapshot: str):
    """精确 → 通配 → 失败（§3.3）。**没有"相似模型推断"分支。**

    `adapters` 已按 `revision` 降序；同一 (provider, snapshot, adapter_id) 的
    多版里取最高 revision（DDL 的唯一索引在真实 Postgres 里也只允许一条，
    但 SQLite 的部分索引表达式不等价，所以排序兜底）。
    """
    exact = [a for a in adapters
             if a.get("model_snapshot") == model_snapshot]
    if exact:
        return exact[0], False
    wildcard = [a for a in adapters if a.get("model_snapshot") == "*"]
    if wildcard:
        return wildcard[0], True
    raise ServiceError(
        422, "NO_ADAPTER_FOR_SNAPSHOT",
        f"no adapter for provider={provider!r} "
        f"snapshot={model_snapshot!r}, and no wildcard is declared; "
        f"S20 does not infer a mapping from similar models",
        details={"provider": provider, "model_snapshot": model_snapshot})


def _rule_parts(rule):
    """把 `reference_order_rule` 归一成 `(ordered[(role, position)], allow_leftover)`。

    §3.4 第 1 条：存**有序数组**，不用 JSON object，因为 object 的键序在
    反序列化后依赖源字节。两种形状都接受，但都保留顺序。
    """
    if isinstance(rule, dict):
        roles = rule.get("roles")
        if not isinstance(roles, list):
            raise ServiceError(400, "INVALID_SCHEMA",
                               "reference_order_rule.roles must be an array")
        parsed = []
        for item in roles:
            parsed.append(_rule_item(item))
        return parsed, bool(rule.get("allow_leftover", False))
    if isinstance(rule, list):
        return [_rule_item(item) for item in rule], False
    raise ServiceError(400, "INVALID_SCHEMA",
                       "reference_order_rule must be an array or an object "
                       "with a roles array")


def _rule_item(item):
    if not isinstance(item, dict):
        raise ServiceError(400, "INVALID_SCHEMA",
                           "reference_order_rule entries must be objects")
    role = item.get("role")
    position = item.get("position")
    if not isinstance(role, str) or not role:
        raise ServiceError(400, "INVALID_SCHEMA",
                           "reference_order_rule entry needs a role")
    if not isinstance(position, int) or isinstance(position, bool) \
            or position < 0:
        raise ServiceError(400, "INVALID_SCHEMA",
                           "reference_order_rule entry needs a position >= 0")
    return {"role": role, "position": position,
            "required": bool(item.get("required", True))}


def order_references(pinned: list, rule, *, adapter_id: str,
                     token_format: str) -> tuple[list, list]:
    """参考图排序 + token 映射（§3.4）。返回 `(ordered, warnings)`。

    排序键 `(position, order_hint, role_key)`：`role_key` 作最后一级保证确定性，
    **不用** `artifact_ref`（它随绑定集变，会破坏"同一 ShotSpec 同一顺序"）。

    角色缺口的处理（S20-44，F6 与 T20-26 的交界）：
    - adapter 点名了该角色 → `REFERENCE_ORDER_UNRESOLVABLE`（adapter 与绑定
      集不匹配，修的是这两者之一）；
    - adapter 没点名 → 该图不属于任何位置，`allow_leftover` 决定是丢弃还是
      追加 + warning。
    """
    ordered_rule, allow_leftover = _rule_parts(rule)
    if not ordered_rule:
        raise ServiceError(400, "INVALID_SCHEMA",
                           "reference_order_rule is empty")

    known_roles = {entry["role"] for entry in ordered_rule}
    by_role: dict = {}
    for ref in pinned:
        by_role.setdefault(ref["role"], []).append(ref)

    slots: list = []
    warnings: list = []
    for entry in ordered_rule:
        candidates = by_role.get(entry["role"], [])
        if not candidates and entry["required"]:
            raise ServiceError(
                422, "REFERENCE_ORDER_UNRESOLVABLE",
                f"adapter {adapter_id!r} requires a {entry['role']!r} "
                f"reference at position {entry['position']}, but the binding "
                f"set has none", details={"role": entry["role"],
                                          "position": entry["position"]})
        for ref in candidates:
            slots.append((entry["position"], ref.get("order_hint", 0),
                          ref["global_asset_id"], entry["role"], ref))

    leftovers = [ref for ref in pinned if ref["role"] not in known_roles]
    if leftovers:
        if not allow_leftover:
            raise ServiceError(
                422, "REFERENCE_ORDER_UNRESOLVABLE",
                "the binding set has references whose role the adapter does "
                "not map, and allow_leftover is false; S20 will not silently "
                "drop them", details={"roles": sorted(
                    {r["role"] for r in leftovers})})
        highest = max((s[0] for s in slots), default=-1)
        for offset, ref in enumerate(sorted(
                leftovers, key=lambda r: (r.get("order_hint", 0),
                                          r["global_asset_id"])), start=1):
            slots.append((highest + offset, ref.get("order_hint", 0),
                          ref["global_asset_id"], ref["role"], ref))
        warnings.append({"code": "UNMAPPED_REFERENCE_APPENDED",
                         "field": ",".join(sorted({r["role"]
                                                   for r in leftovers}))})

    slots.sort(key=lambda s: (s[0], s[1], s[2]))
    ordered: list = []
    for position, (_, _, _, role, ref) in enumerate(
            (s[0], s[1], s[2], s[3], s[4]) for s in slots):
        ordered.append({**ref, "position": position, "role": role,
                        "token": token_format.format(n=position + 1)})
    return ordered, warnings


# ---------------------------------------------------------------- 能力校验

def _mode_detail(capability: dict, mode: str):
    for detail in capability.get("mode_details") or []:
        if detail.get("mode") == mode:
            return detail
    return None


def _claim(detail: dict, key: str):
    """`capability_claims[]` 里某 key 的支持度。**缺省视为 unsupported。**

    缺省不是"没声明就放行"：S24 没有规定 capability_key 词表，"没出现"与
    "不支持"在消费侧无法区分，只能按不支持处理 —— 否则一个拼错的 key 会让
    必需字段静静通过。
    """
    for claim in detail.get("capability_claims") or []:
        if claim.get("capability_key") == key:
            return claim.get("support")
    return None


def _is_supported(detail: dict, key: str) -> bool:
    support = _claim(detail, key)
    return isinstance(support, str) and support not in UNSUPPORTED_SUPPORT


def _output_specs(capability: dict) -> list:
    """取 `output_specs[]`，S24 两种形状都认。

    S24 把 `output_specs` 放在 `mode_details[]` **里面**，不在快照顶层
    （S20-48）。曾经这里只读顶层，于是"宽高比/时长档位查不到"被当成"供应商
    不支持"——一个静默的 fail-open：明明有数据，却报告不可用。所以两种形状
    都找，找不到才返回空（那时才真的是未知）。
    """
    specs = capability.get("output_specs")
    if isinstance(specs, list) and specs:
        return specs
    out: list = []
    for detail in capability.get("mode_details") or []:
        out.extend(detail.get("output_specs") or [])
    return out


def check_capabilities(*, fragments: list, capability: dict, mode: str,
                       adapter: dict, options: dict,
                       parameters: dict) -> tuple:
    """步骤 ⑥（§3.6）。返回 `(kept_fragments, warnings)`。

    **不许裁剪。** 值超范围 → 422，绝不 clamp（T20-33）。可选字段不支持时，
    只有 adapter 在 `unsupported_optionals` 里**显式声明过**、且调用方允许
    省略，才省略；其余一律 422。这是 F8/F10 之外不容许任何降级的落点。
    """
    allow_omission = bool(options.get("allow_optional_omission", True))
    strict = bool(options.get("strict_capabilities", True))
    declared_unsupported = set(adapter.get("unsupported_optionals") or [])
    detail = _mode_detail(capability, mode)
    if detail is None:
        # 快照没声明这个模态：能力未知，不是"支持"。
        raise ServiceError(
            422, "REQUIRED_FIELD_UNSUPPORTED",
            f"capability snapshot declares no mode {mode!r}; S20 will not "
            f"assume support",
            details={"mode": mode, "reason": "MODE_NOT_DECLARED"})

    kept: list = []
    warnings: list = []
    for fragment in fragments:
        if not fragment.requires:
            kept.append(fragment)
            continue
        missing = [key for key in fragment.requires
                   if not _is_supported(detail, key)]
        if not missing:
            kept.append(fragment)
            continue
        if fragment.optional:
            declared = all(key in declared_unsupported for key in missing)
            if allow_omission and declared:
                warnings.append({"code": "OPTIONAL_OMITTED",
                                 "field": ",".join(missing)})
                continue
            if allow_omission and not declared:
                # 未声明的降级：模板作者没说过这段可省，S20 不替他决定。
                raise ServiceError(
                    422, "REQUIRED_FIELD_UNSUPPORTED",
                    f"section {fragment.section_id!r} needs {missing}, which "
                    f"this model snapshot does not support, and the adapter "
                    f"does not declare it as an omittable optional",
                    details={"section_id": fragment.section_id,
                             "missing": missing,
                             "reason": "NOT_DECLARED_OMITTABLE"})
            raise ServiceError(
                422, "REQUIRED_FIELD_UNSUPPORTED",
                f"section {fragment.section_id!r} needs {missing}, and "
                f"allow_optional_omission is false",
                details={"section_id": fragment.section_id,
                         "missing": missing})
        raise ServiceError(
            422, "REQUIRED_FIELD_UNSUPPORTED",
            f"section {fragment.section_id!r} is required and needs {missing}, "
            f"which this model snapshot does not support "
            f"(strict_capabilities={str(strict).lower()}; required fields "
            f"never degrade)",
            details={"section_id": fragment.section_id, "missing": missing})

    _check_ranges(adapter=adapter, capability=detail, parameters=parameters)
    return kept, warnings


def _check_ranges(*, adapter: dict, capability: dict, parameters: dict) -> None:
    """参数取值必须在 adapter 声明 + 快照声明的范围内。超了报错，不裁。"""
    declared = adapter.get("parameter_ranges") or {}
    for key, limits in declared.items():
        if key not in parameters or not isinstance(limits, dict):
            continue
        value = parameters[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        low = limits.get("min")
        high = limits.get("max")
        if (low is not None and value < low) or (high is not None
                                                 and value > high):
            raise ServiceError(
                422, "CAPABILITY_RANGE_VIOLATION",
                f"{key}={value!r} is outside [{low}, {high}]; S20 never "
                f"clamps because that silently changes intent",
                details={"parameter": key, "value": value,
                         "min": low, "max": high})

    # 快照侧的 output_specs 约束宽高比可用性与分辨率上限。超上限同样拒绝，
    # 不是缩放到上限。
    aspect = parameters.get("aspect_ratio")
    specs = _output_specs(capability)
    if aspect and specs:
        allowed = {spec.get("aspect_ratio") for spec in specs}
        if aspect not in allowed:
            raise ServiceError(
                422, "CAPABILITY_RANGE_VIOLATION",
                f"aspect_ratio {aspect!r} is not produced by this snapshot",
                details={"parameter": "aspect_ratio", "value": aspect,
                         "allowed": sorted(a for a in allowed if a)})
        for spec in specs:
            if spec.get("aspect_ratio") != aspect:
                continue
            resolution = parameters.get("resolution") or {}
            width = resolution.get("width")
            height = resolution.get("height")
            max_w, max_h = spec.get("max_width"), spec.get("max_height")
            if width and max_w and width > max_w:
                raise ServiceError(
                    422, "CAPABILITY_RANGE_VIOLATION",
                    f"width {width} exceeds the snapshot maximum {max_w}",
                    details={"parameter": "resolution.width", "value": width,
                             "max": max_w})
            if height and max_h and height > max_h:
                raise ServiceError(
                    422, "CAPABILITY_RANGE_VIOLATION",
                    f"height {height} exceeds the snapshot maximum {max_h}",
                    details={"parameter": "resolution.height", "value": height,
                             "max": max_h})


# ---------------------------------------------------------------- 时长

def resolve_duration(*, target_seconds: float, capability: dict,
                     aspect_ratio) -> dict:
    """目标时长 vs 供应商档位（§1.5、S20-39）。**不把 4.0 改成 5 往下走。**

    `provider_tier_seconds` 只从快照的 `duration_tiers_s` 里取最小的、
    不小于目标的档位。没有匹配档位（或快照没有该宽高比的 output_spec）→
    `null` + `handling='unacceptable'`：宁可承认不可用，也不发明一个档位。
    """
    tiers: list = []
    for spec in _output_specs(capability):
        if aspect_ratio and spec.get("aspect_ratio") != aspect_ratio:
            continue
        tiers = [t for t in spec.get("duration_tiers_s") or []
                 if isinstance(t, (int, float)) and not isinstance(t, bool)
                 and t > 0]
        break
    tier = None
    for candidate in sorted(tiers):
        if candidate + 1e-9 >= target_seconds:
            tier = candidate
            break
    if tier is None:
        return {"target_seconds": target_seconds,
                "provider_tier_seconds": None,
                "overrun_seconds": None,
                "handling": "unacceptable",
                "trim_window": None}

    overrun = round(tier - target_seconds, 6)
    if overrun <= 1e-9:
        return {"target_seconds": target_seconds,
                "provider_tier_seconds": tier,
                "overrun_seconds": 0.0,
                "handling": "trim",
                "trim_window": {"from": 0.0, "to": target_seconds}}
    # 档位比目标长：记录两端。剪哪一段是 S41 的权限，S20 只给窗口。
    return {"target_seconds": target_seconds,
            "provider_tier_seconds": tier,
            "overrun_seconds": overrun,
            "handling": "trim",
            "trim_window": {"from": 0.0, "to": target_seconds}}


# ---------------------------------------------------------------- 服务

class PromptCompilerService:
    """模板、adapter、编译。所有写入都在 `Transaction` 里，所有读取都校验租户。

    依赖注入是**闭包**而不是对象：上游只有 HTTP，闭包让"未配置即 503"成为
    构造期就决定的事，而不是运行到一半才发现 `None`。
    """

    def __init__(self, conn, *, shot_source=None, binding_source=None,
                 capability_source=None, alignment_source=None,
                 regression_source=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # Outbox.__init__ 会 executescript 建表 —— 它自带隐式 COMMIT，所以
        # 只能在事务外建一次，之后复用同一实例。放进 Transaction 会破坏
        # COMMIT/ROLLBACK（与 S25 的注释同因）。
        self.outbox = Outbox(self.conn)
        self.shot_source = shot_source
        self.binding_source = binding_source
        self.capability_source = capability_source
        self.alignment_source = alignment_source
        self.regression_source = regression_source

    # ------------------------------------------------------------ 模板

    def create_template(self, tenant_id: str, command_id: str, body: dict,
                        request_hash: str) -> dict:
        name = body.get("name")
        modality = body.get("modality")
        provider = body.get("provider")
        if not isinstance(name, str) or not name.strip():
            raise ServiceError(400, "INVALID_SCHEMA",
                               "name must be a non-empty string")
        if modality not in MODALITIES:
            raise ServiceError(400, "INVALID_SCHEMA",
                               f"modality {modality!r} is not one of "
                               f"{list(MODALITIES)}")
        if not isinstance(provider, str) or not provider.strip():
            raise ServiceError(400, "INVALID_SCHEMA",
                               "provider must be a non-empty string")
        template_id = body.get("template_id") or _new_id("template")
        if not isinstance(template_id, str) or not template_id:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "template_id must be a string")

        now = _now()
        with Transaction(self.conn):
            existing = self.conn.execute(
                "SELECT 1 FROM template WHERE tenant_id=? AND template_id=?",
                (tenant_id, template_id)).fetchone()
            if existing is not None:
                # S20-36：方案 §7 没给这个分支分配码。
                raise ServiceError(
                    409, "TEMPLATE_EXISTS",
                    f"template {template_id!r} already exists in this tenant",
                    details={"template_id": template_id})
            self.conn.execute(
                "INSERT INTO template (tenant_id, template_id, name, modality,"
                " provider, current_revision, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?,0,'ACTIVE',?,?)",
                (tenant_id, template_id, name.strip(), modality,
                 provider.strip(), now, now))
        return {"template_id": template_id, "name": name.strip(),
                "modality": modality, "provider": provider.strip(),
                "current_revision": 0, "state": "ACTIVE",
                "created_at": now, "updated_at": now}

    def _template_row(self, tenant_id: str, template_id: str):
        row = self.conn.execute(
            "SELECT * FROM template WHERE tenant_id=? AND template_id=?",
            (tenant_id, template_id)).fetchone()
        if row is None:
            # 跨租户一律 404，不泄漏存在性（E28 / T20-48）。
            raise ServiceError(404, "TEMPLATE_NOT_FOUND",
                               f"template {template_id!r} not found")
        return dict(row)

    def _revision_row(self, tenant_id: str, template_id: str, revision: int):
        row = self.conn.execute(
            "SELECT * FROM template_revision WHERE tenant_id=? AND "
            "template_id=? AND revision=?",
            (tenant_id, template_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "TEMPLATE_NOT_FOUND",
                               f"template {template_id!r} revision "
                               f"{revision} not found")
        return dict(row)

    @staticmethod
    def _revision_out(row: dict) -> dict:
        return {
            "template_id": row["template_id"],
            "revision": row["revision"],
            "state": row["state"],
            "compatible_schema": _loads(row["compatible_schema"], {}),
            "body": _loads(row["body"], {}),
            "adapter_revision_min": row["adapter_revision_min"],
            "adapter_revision_max": row["adapter_revision_max"],
            "model_snapshots": _loads(row["model_snapshots"], []),
            "adapter_scopes": _loads(row["adapter_scopes"], []),
            "regression_set_ref": _loads(row["regression_set_ref"], {}),
            "regression_verdict": row["regression_verdict"],
            "content_hash": row["content_hash"],
            "author_subject": row["author_subject"],
            "published_at": row["published_at"],
            "retired_at": row["retired_at"],
            "created_at": row["created_at"],
        }

    def get_template_revision(self, tenant_id: str, template_id: str,
                              revision: int) -> dict:
        """S19 期待这里有一个顶层 `template` 对象（`agent-runtime/deps.py`）。

        同时保留平铺字段：S19 的现有实现读 `payload["template"]`，而这不影响
        任何其他调用方。两者内容相同，不是两个权威。
        """
        row = self._revision_row(tenant_id, template_id, revision)
        out = self._revision_out(row)
        return {"template": out, **out}

    def list_template_revisions(self, tenant_id: str, template_id: str) -> dict:
        template = self._template_row(tenant_id, template_id)
        rows = self.conn.execute(
            "SELECT * FROM template_revision WHERE tenant_id=? AND "
            "template_id=? ORDER BY revision ASC",
            (tenant_id, template_id)).fetchall()
        return {"template_id": template_id,
                "current_revision": template["current_revision"],
                "revisions": [
                    {"revision": row["revision"], "state": row["state"],
                     "content_hash": row["content_hash"],
                     "regression_verdict": row["regression_verdict"],
                     "published_at": row["published_at"],
                     "retired_at": row["retired_at"]}
                    for row in rows]}

    def put_adapter(self, tenant_id: str, command_id: str, body: dict,
                    request_hash: str) -> dict:
        """登记/更新一版 adapter。**不自动递增 revision**：调用方必须显式给，
        因为 revision 是模板区间引用的那个东西，隐式自增会让区间含义漂移。"""
        adapter_id = body.get("adapter_id")
        revision = body.get("revision")
        provider = body.get("provider")
        snapshot = body.get("model_snapshot")
        token_format = body.get("reference_token_format")
        budget = body.get("token_budget")
        if not isinstance(adapter_id, str) or not adapter_id:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "adapter_id must be a non-empty string")
        if not isinstance(revision, int) or isinstance(revision, bool) \
                or revision < 1:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "revision must be an integer >= 1")
        if not isinstance(provider, str) or not provider:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "provider must be a non-empty string")
        _check_snapshot(snapshot, allow_wildcard=True)
        if not isinstance(token_format, str) or "{n}" not in token_format:
            raise ServiceError(
                400, "INVALID_SCHEMA",
                "reference_token_format must contain {n}, e.g. '<image_{n}>'")
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "token_budget must be a positive integer")
        rule = body.get("reference_order_rule")
        # 形状校验前置：把"adapter 写错了"挡在写入，而不是编译时才发现。
        _rule_parts(rule)

        adapter = {
            "adapter_id": adapter_id,
            "provider": provider,
            "model_snapshot": snapshot,
            "revision": revision,
            "field_map": body.get("field_map") or {},
            "phrase_tables": body.get("phrase_tables") or {},
            "reference_order_rule": rule,
            "reference_token_format": token_format,
            "token_budget": budget,
            "unsupported_optionals": body.get("unsupported_optionals") or [],
            "speaker_prefix": body.get("speaker_prefix"),
            "section_separator": body.get("section_separator"),
            "parameter_ranges": body.get("parameter_ranges") or {},
            "content_type": body.get("content_type"),
        }
        content_hash = adapter_content_hash(adapter)
        now = _now()
        try:
            with Transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO adapter_binding (tenant_id, adapter_id,"
                    " revision, provider, model_snapshot, field_map,"
                    " reference_order_rule, reference_token_format,"
                    " token_budget, unsupported_optionals, phrase_tables,"
                    " speaker_prefix, section_separator, parameter_ranges,"
                    " content_type, content_hash, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT (tenant_id, adapter_id, revision) DO NOTHING",
                    (tenant_id, adapter_id, revision, provider, snapshot,
                     _dumps(adapter["field_map"]), _dumps(rule), token_format,
                     budget, _dumps(adapter["unsupported_optionals"]),
                     _dumps(adapter["phrase_tables"]),
                     adapter["speaker_prefix"], adapter["section_separator"],
                     _dumps(adapter["parameter_ranges"]),
                     adapter["content_type"], content_hash, now))
        except sqlite3.IntegrityError as exc:
            # 撞上的是 `adapter_binding_exact` / `adapter_binding_wildcard`
            # 而不是主键：同一 (provider, snapshot, adapter_id) 已经有另一版。
            # §5.2 给的主键允许 revision 累积，但唯一索引不允许 —— 两者冲突，
            # 登记为 S20-50。这里不能让它变成 500：它是调用方的写入冲突，
            # 该拿到 409 和"现有那一版是谁"。
            stored = self.conn.execute(
                "SELECT revision FROM adapter_binding WHERE tenant_id=? AND "
                "adapter_id=? AND provider=? AND model_snapshot=?",
                (tenant_id, adapter_id, provider, snapshot)).fetchone()
            raise ServiceError(
                409, "TEMPLATE_REVISION_CONFLICT",
                f"adapter {adapter_id!r} already has revision "
                f"{stored['revision'] if stored else '?'} for "
                f"({provider!r}, {snapshot!r}); §5.2's unique index forbids a "
                f"second revision of the same adapter for one snapshot "
                f"(CONTRACT_GAPS S20-50)",
                details={"adapter_id": adapter_id,
                         "provider": provider, "model_snapshot": snapshot,
                         "existing_revision": (stored["revision"]
                                               if stored else None)}) from exc
        with Transaction(self.conn):
            stored = self.conn.execute(
                "SELECT content_hash FROM adapter_binding WHERE tenant_id=? AND"
                " adapter_id=? AND revision=?",
                (tenant_id, adapter_id, revision)).fetchone()
        if stored is not None and stored["content_hash"] != content_hash:
            # 同 (adapter_id, revision) 不同内容：不可变对象被改写，P0。
            raise ServiceError(
                409, "TEMPLATE_REVISION_CONFLICT",
                f"adapter {adapter_id!r} revision {revision} already exists "
                f"with a different content_hash; revisions are immutable",
                details={"adapter_id": adapter_id, "revision": revision,
                         "current_content_hash": stored["content_hash"]})
        return {"adapter_id": adapter_id, "revision": revision,
                "content_hash": content_hash,
                "reference_order_rule": rule}

    def get_adapter(self, tenant_id: str, adapter_id: str,
                    revision: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM adapter_binding WHERE tenant_id=? AND adapter_id=? "
            "AND revision=?", (tenant_id, adapter_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"adapter {adapter_id!r} revision {revision} "
                               f"not found")
        return self._adapter_out(dict(row))

    @staticmethod
    def _adapter_out(row: dict) -> dict:
        return {
            "adapter_id": row["adapter_id"],
            "revision": row["revision"],
            "provider": row["provider"],
            "model_snapshot": row["model_snapshot"],
            "field_map": _loads(row["field_map"], {}),
            "reference_order_rule": _loads(row["reference_order_rule"], []),
            "reference_token_format": row["reference_token_format"],
            "token_budget": row["token_budget"],
            "unsupported_optionals": _loads(row["unsupported_optionals"], []),
            "content_hash": row["content_hash"],
            # 方案 §5.2 没有这些列（S20 内部扩展，见 CONTRACT_GAPS S20-47）：
            # 词表与说话人前缀必须能被 adapter 表达，否则 phrase_table 无表可查。
            "phrase_tables": _loads(row["phrase_tables"], {}),
            "speaker_prefix": row["speaker_prefix"],
            "section_separator": row["section_separator"],
            "parameter_ranges": _loads(row["parameter_ranges"], {}),
            "content_type": row["content_type"],
        }

    # ------------------------------------------------------------ 发布

    def publish_revision(self, tenant_id: str, command_id: str,
                         template_id: str, body: dict,
                         request_hash: str) -> dict:
        """`POST /templates/{id}/revisions`。

        一次调用同时完成"写一版 DRAFT 并跑回归、通过则发布"。方案 §2.2 的表格
        把它描述成发布，但 §4.4 的回归是发布门，二者必须是同一个事务 ——
        否则会留下一个"写进去了但没人跑回归"的 DRAFT，而
        `template_revision_one_draft` 会把这个模板永远卡住。

        S20-37：回归失败时**不写 revision 行**。方案 F23 说"版本仍 DRAFT"，
        但幂等包装器在异常时 ROLLBACK，那一行不会存在；且留下的 DRAFT 会让
        重试撞上部分唯一索引。因此失败只返回 422 + 报告，库里无痕。
        """
        template = self._template_row(tenant_id, template_id)
        parent_revision = body.get("parent_revision")
        if not isinstance(parent_revision, int) or isinstance(
                parent_revision, bool) or parent_revision < 0:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "parent_revision must be an integer >= 0")

        revision = parent_revision + 1
        compatible = body.get("compatible_schema")
        if not isinstance(compatible, dict) or not compatible.get("id"):
            raise ServiceError(400, "INVALID_SCHEMA",
                               "compatible_schema {id, revision, content_hash} "
                               "is required")
        body_spec = body.get("body")
        if not isinstance(body_spec, dict):
            raise ServiceError(400, "INVALID_SCHEMA", "body must be an object")
        sections = body_spec.get("sections")
        if not isinstance(sections, list) or not sections:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "body.sections must be a non-empty array")
        _validate_sections(sections, body_spec)
        modality = body.get("modality") or template["modality"]
        if modality not in MODALITIES:
            raise ServiceError(400, "INVALID_SCHEMA",
                               f"modality {modality!r} is not valid")
        provider = body.get("provider") or template["provider"]
        model_snapshots = body.get("model_snapshots")
        if not isinstance(model_snapshots, list) or not model_snapshots:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "model_snapshots must be a non-empty array")
        for snapshot in model_snapshots:
            _check_snapshot(snapshot)
        min_rev = body.get("adapter_revision_min")
        max_rev = body.get("adapter_revision_max")
        if not isinstance(min_rev, int) or not isinstance(max_rev, int) \
                or min_rev < 1 or max_rev < min_rev:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "adapter_revision_min/max must satisfy "
                               "1 <= min <= max")
        adapter_scopes = body.get("adapter_scopes") or []
        if not isinstance(adapter_scopes, list) or \
                any(not isinstance(a, str) or not a for a in adapter_scopes):
            raise ServiceError(400, "INVALID_SCHEMA",
                               "adapter_scopes must be an array of strings")
        regression_ref = body.get("regression_set_ref")
        if not isinstance(regression_ref, dict) or not regression_ref.get("id") \
                or not isinstance(regression_ref.get("revision"), int):
            raise ServiceError(
                400, "INVALID_SCHEMA",
                "regression_set_ref {id, revision, content_hash} is required "
                "(F23: a template cannot be published without a regression set)")
        author = body.get("author_subject") or "unknown"

        template_doc = {
            "compatible_schema": compatible,
            "modality": modality,
            "provider": provider,
            "model_snapshots": model_snapshots,
            "adapter_scopes": adapter_scopes,
            "body": body_spec,
            "adapter_revision_min": min_rev,
            "adapter_revision_max": max_rev,
            "regression_set_ref": regression_ref,
        }
        content_hash = template_content_hash(template_doc)

        # 回归在事务外跑：它只读（渲染 + 静态断言），不碰库，也不该延长
        # 写锁的持有时间（§4.4 明确"不含付费调用"）。
        report = self._run_regression(
            tenant_id=tenant_id, template_id=template_id,
            revision=revision, template_doc=template_doc,
            regression_ref=regression_ref)
        if report["verdict"] != "pass":
            raise ServiceError(
                422, "REGRESSION_NOT_PASSED",
                f"regression set {regression_ref.get('id')!r} did not pass; "
                f"the revision was not written",
                details={"report": report})

        now = _now()
        with Transaction(self.conn):
            # 乐观并发（§6.3）：parent_revision 落后 → 0 行 → 409。
            cursor = self.conn.execute(
                "UPDATE template SET current_revision=?, updated_at=? "
                "WHERE tenant_id=? AND template_id=? AND current_revision=?",
                (revision, now, tenant_id, template_id, parent_revision))
            if cursor.rowcount == 0:
                current = self.conn.execute(
                    "SELECT current_revision FROM template WHERE tenant_id=? "
                    "AND template_id=?",
                    (tenant_id, template_id)).fetchone()
                raise ServiceError(
                    409, "TEMPLATE_REVISION_CONFLICT",
                    "parent_revision is stale; re-read the template and retry",
                    retryable=True,
                    details={"current_revision": current["current_revision"]
                             if current else None,
                             "parent_revision": parent_revision})
            try:
                self.conn.execute(
                    "INSERT INTO template_revision (tenant_id, template_id,"
                    " revision, state, compatible_schema, body,"
                    " adapter_revision_min, adapter_revision_max,"
                    " model_snapshots, adapter_scopes, regression_set_ref,"
                    " regression_verdict, content_hash, author_subject,"
                    " published_at, retired_at, created_at)"
                    " VALUES (?,?,?,'PUBLISHED',?,?,?,?,?,?,?,'pass',?,?,?,"
                    "NULL,?)",
                    (tenant_id, template_id, revision, _dumps(compatible),
                     _dumps(body_spec), min_rev, max_rev,
                     _dumps(sorted(model_snapshots)), _dumps(adapter_scopes),
                     _dumps(regression_ref), content_hash, author, now, now))
            except sqlite3.IntegrityError as exc:
                # 两个请求以不同 parent_revision 同时通过乐观检查：部分唯一
                # 索引 `template_revision_one_draft` 不拦 PUBLISHED，但主键会。
                raise ServiceError(
                    409, "TEMPLATE_REVISION_CONFLICT",
                    f"revision {revision} already exists: {exc}",
                    retryable=True) from exc
            result_id = _new_id("regression")
            self.conn.execute(
                "INSERT INTO compile_regression_result (tenant_id, result_id,"
                " template_id, template_revision, regression_set_ref, verdict,"
                " case_total, case_passed, case_failed, report, started_at,"
                " finished_at, created_at) VALUES (?,?,?,?,?,'pass',?,?,?,?,"
                "?,?,?)",
                (tenant_id, result_id, template_id, revision,
                 _dumps(regression_ref), report["summary"]["total"],
                 report["summary"]["passed"], report["summary"]["failed"],
                 _dumps(report), report["started_at"], report["finished_at"],
                 now))

        return {"template_id": template_id, "revision": revision,
                "state": "PUBLISHED", "content_hash": content_hash,
                "regression_result_id": result_id,
                "regression_report": report}

    def retire_revision(self, tenant_id: str, command_id: str,
                        template_id: str, revision: int, body: dict,
                        request_hash: str) -> dict:
        """`POST .../{rev}:retire`。**只影响新编译**（S20-38）。"""
        self._template_row(tenant_id, template_id)
        row = self._revision_row(tenant_id, template_id, revision)
        if row["state"] == "RETIRED":
            # 幂等语义：已退役再退役是同一结果，不报错。取消退役不支持（§6.5）。
            return {"template_id": template_id, "revision": revision,
                    "state": "RETIRED", "retired_at": row["retired_at"]}
        if row["state"] == "DRAFT":
            raise ServiceError(
                422, "TEMPLATE_NOT_PUBLISHED",
                "a DRAFT cannot be retired; only a PUBLISHED revision can")
        now = _now()
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE template_revision SET state='RETIRED', retired_at=? "
                "WHERE tenant_id=? AND template_id=? AND revision=? AND "
                "state='PUBLISHED'",
                (now, tenant_id, template_id, revision))
            self.conn.execute(
                "UPDATE template SET updated_at=? WHERE tenant_id=? AND "
                "template_id=?", (now, tenant_id, template_id))
        return {"template_id": template_id, "revision": revision,
                "state": "RETIRED", "retired_at": now,
                "note": "retirement blocks new compilations only; in-flight "
                        "compiles and existing manifests are unaffected"}

    def get_regression_report(self, tenant_id: str, template_id: str,
                              revision: int) -> dict:
        self._revision_row(tenant_id, template_id, revision)
        row = self.conn.execute(
            "SELECT * FROM compile_regression_result WHERE tenant_id=? AND "
            "template_id=? AND template_revision=? "
            "ORDER BY created_at DESC, result_id DESC LIMIT 1",
            (tenant_id, template_id, revision)).fetchone()
        if row is None:
            raise ServiceError(
                404, "NOT_FOUND",
                "no regression result exists for this revision")
        return _loads(row["report"], {})

    # ------------------------------------------------------------ 回归

    def _run_regression(self, *, tenant_id: str, template_id: str,
                        revision: int, template_doc: dict,
                        regression_ref: dict) -> dict:
        """§4.4 的断言集合。**全部是确定性渲染 + 静态断言，无付费调用。**

        回归集本体由外部（模板作者 / 回归集服务）提供；本仓库没有那个服务，
        所以 `regression_source` 未配置时**只有内置的结构断言**，并且报告里
        明确写出 `cases` 来自哪里 —— 不假装跑过一个不存在的回归集。

        内置断言（每个 `sections[]` 一条 case）：
        - `section_renderable`：段声明合法、`render` 四值之一；
        - `path_declared`：段引用的路径都在 `from` 声明里；
        - `phrase_table_present`：`phrase_table` 段在 adapter 词表里逐叶有映射。

        这三条正好覆盖 §3.5 的三件事：能渲染、有来源、不臆造短语。
        """
        started = _now()

        adapter_rows = self._adapters_for_template(tenant_id, template_doc)
        cases: list = []
        for index, section in enumerate(template_doc["body"]["sections"]):
            sid = str(section.get("id") or f"s{index}")
            assertions: list = []
            render_as = section.get("render") or "verbatim"
            assertions.append({
                "name": "section_renderable",
                "result": "pass" if render_as in RENDER_TYPES else "fail",
                "detail": {"render": render_as}})
            source = section.get("from")
            text = section.get("text")
            declared = {str(s.get("from")).strip()
                        for s in template_doc["body"]["sections"]
                        if s.get("from")}
            referenced = _template_paths(text) if text else []
            undeclared = [p for p in referenced
                          if not _path_declared(p, declared)]
            assertions.append({
                "name": "path_declared",
                "result": "fail" if undeclared else "pass",
                "detail": {"undeclared": undeclared}})
            if render_as == "phrase_table" and source:
                missing = []
                for adapter in adapter_rows:
                    tables = adapter.get("phrase_tables") or {}
                    # 叶子查的是 `<from>.<key>`（`_phrase_leaves` 逐层下钻），
                    # 所以存在性是前缀匹配而不是相等。用相等会把"只按叶子建
                    # 表"的正确模板判成缺表 —— 而那是唯一能表达对象映射的写法。
                    if not any(path == source
                               or path.startswith(source + ".")
                               or path.startswith(source + "[")
                               for path in tables):
                        missing.append({"adapter_id": adapter["adapter_id"],
                                        "path": source})
                assertions.append({
                    "name": "phrase_table_present",
                    "result": "fail" if missing else "pass",
                    "detail": {"missing": missing}})
            cases.append({"case_id": f"rc_{index:03d}", "label": sid,
                          "assertions": assertions})

        passed = sum(1 for case in cases
                     if all(a["result"] == "pass"
                            for a in case["assertions"]))
        failed = len(cases) - passed
        external = None
        if self.regression_source is not None:
            try:
                external = _upstream(
                    lambda: self.regression_source(regression_ref),
                    "CAPABILITY_SOURCE_UNAVAILABLE", empty_ok=True)
            except ServiceError:
                external = None
        finished = _now()
        return {
            "template_ref": {"id": template_id, "revision": revision,
                             "content_hash": template_content_hash(
                                 template_doc)},
            "regression_set_ref": regression_ref,
            "started_at": started,
            "finished_at": finished,
            "cases": cases,
            "verdict": "pass" if failed == 0 else "fail",
            "summary": {"total": len(cases), "passed": passed,
                        "failed": failed},
            "source": ("internal" if external is None
                       else {"external": external.get("id")
                             if isinstance(external, dict) else True}),
        }

    def _adapters_for_template(self, tenant_id: str,
                               template_doc: dict) -> list:
        scopes = template_doc.get("adapter_scopes") or []
        if not scopes:
            return []
        placeholders = ",".join("?" for _ in scopes)
        rows = self.conn.execute(
            f"SELECT * FROM adapter_binding WHERE tenant_id=? AND adapter_id "
            f"IN ({placeholders}) ORDER BY adapter_id, revision DESC",
            (tenant_id, *scopes)).fetchall()
        return [self._adapter_out(dict(row)) for row in rows]

    # ------------------------------------------------------------ 探测

    def probe(self, tenant_id: str, body: dict) -> dict:
        """`GET|POST /prompt-compilations:probe`（§2.6）。**纯读，无 manifest。**

        S20-41：入参只有 `model_snapshot` 字符串，而能力绑定在
        `capability_snapshot_ref` 上，S24 又没有"按快照字符串查 id"的路由。
        因此探测只回答 adapter × 区间 × 可选省略；`requirement_set_ref` 原样
        回显，不评估。真正的能力校验在步骤 ⑥。
        """
        template_ref = body.get("template_ref")
        if not isinstance(template_ref, dict) or not template_ref.get("id") \
                or not isinstance(template_ref.get("revision"), int):
            raise ServiceError(400, "INVALID_SCHEMA",
                               "template_ref {id, revision} is required")
        snapshots = body.get("model_snapshots")
        if not isinstance(snapshots, list) or not snapshots:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "model_snapshots must be a non-empty array")
        if len(snapshots) > MAX_PROBE_SNAPSHOTS:
            raise ServiceError(
                400, "INVALID_SCHEMA",
                f"at most {MAX_PROBE_SNAPSHOTS} snapshots per probe; "
                f"batch the rest", details={"limit": MAX_PROBE_SNAPSHOTS,
                                            "got": len(snapshots)})
        for snapshot in snapshots:
            _check_snapshot(snapshot)

        row = self._revision_row(tenant_id, template_ref["id"],
                                 template_ref["revision"])
        revision = self._revision_out(row)
        if revision["state"] == "DRAFT":
            raise ServiceError(422, "TEMPLATE_NOT_PUBLISHED",
                               "a DRAFT has no compatibility to probe")
        if revision["state"] == "RETIRED":
            raise ServiceError(
                422, "TEMPLATE_RETIRED",
                "a retired template cannot serve new candidates")

        # provider 在 `template` 行上，不在 revision 上（`_revision_out` 没有
        # 这个键）。曾经这里读 `revision["provider"]` 拿到 `""`，于是每条
        # probe 都以 `NO_ADAPTER_FOR_SNAPSHOT` 回答 —— 一个永远说"不兼容"的
        # 探测比没有探测更坏，调用方会据此放弃本来可编的快照。
        provider = self._template_row(tenant_id, revision["template_id"])[
            "provider"]
        scopes = set(revision.get("adapter_scopes") or [])
        rows = self.conn.execute(
            "SELECT * FROM adapter_binding WHERE tenant_id=? AND provider=? "
            "ORDER BY adapter_id, revision DESC",
            (tenant_id, provider)).fetchall()
        adapters = [self._adapter_out(dict(row)) for row in rows]
        if scopes:
            adapters = [a for a in adapters if a["adapter_id"] in scopes]

        optional_requirements = _optional_requirements(
            revision.get("body") or {})
        results: list = []
        for snapshot in snapshots:
            try:
                adapter, fallback = select_adapter(
                    adapters, provider=provider, model_snapshot=snapshot)
            except ServiceError as exc:
                results.append({"model_snapshot": snapshot,
                                "verdict": "incompatible",
                                "reason": exc.code})
                continue
            in_range = (revision["adapter_revision_min"]
                        <= adapter["revision"]
                        <= revision["adapter_revision_max"])
            if not in_range:
                results.append({
                    "model_snapshot": snapshot, "verdict": "incompatible",
                    "reason": "ADAPTER_NOT_APPLICABLE",
                    "adapter_binding_ref": _adapter_ref(adapter)})
                continue
            omitted = sorted(optional_requirements)
            results.append({
                "model_snapshot": snapshot,
                "verdict": ("compatible_with_omissions" if omitted
                            else "compatible"),
                "adapter_binding_ref": _adapter_ref(adapter),
                "missing_required_capabilities": [],
                "reason": ("ADAPTER_FALLBACK" if fallback else None),
                "omitted_optionals": omitted,
            })
        return {"results": results,
                "evaluated_at": _now(),
                "requirement_set_ref": body.get("requirement_set_ref"),
                "note": "capability support is not evaluated here; it needs a "
                        "capability_snapshot_ref, which this probe does not "
                        "take (CONTRACT_GAPS S20-41)"}

    # ------------------------------------------------------------ 编译

    def compile(self, tenant_id: str, command_id: str, body: dict,
                request_hash: str) -> dict:
        """七步（§3.1）。**任何一步失败都不写 manifest。**

        顺序不能换：
        - ③ 在 ④ 之前：参考图顺序由 adapter 的规则决定，adapter 未定就没有
          规则可依。
        - ④ 在 ⑤ 之前：token 编号是排序的产物，渲染时就要写进
          `references[]`；事后排序会得到"文本说 A 是主参考、参数把 B 放
          index 0"的分裂。
        - ⑥ 在 ⑤ 之后：能力校验会删掉可省略的段，删完才能定稿 prompt 字节。

        本方法只做编排；每一步的实际工作在 `_step_*` 里，序号对应 §8.1 的
        `s20_compile_step_duration_seconds{step}`。
        """
        target = body.get("target")
        if not isinstance(target, dict):
            raise ServiceError(400, "INVALID_SCHEMA", "target is required")
        options = body.get("options") or {}
        if not isinstance(options, dict):
            raise ServiceError(400, "INVALID_SCHEMA",
                               "options must be an object")

        # ① 校验（shot pin / schema / 模板状态）
        shot, shot_ref, template, template_doc = self._step_validate(
            tenant_id, target)

        # ② 固定绑定
        pinned, missing, binding_ref = self._step_pin(tenant_id, body, shot)

        # ③ adapter 选择
        adapter, fallback, adapter_ref = self._step_adapter(
            tenant_id, target, template_doc)

        # ④ 参考图顺序 + token 映射（缺角色的定码在这里，见 S20-44）
        references, warnings = self._step_order(pinned, missing, adapter)
        if fallback:
            # F8：通配降级允许，但必须留痕，且 warning 进 content_hash。
            warnings.append({"code": "ADAPTER_FALLBACK",
                             "field": target.get("model_snapshot")})

        # ⑤ 渲染
        fragments = self._step_render(shot=shot, template_doc=template_doc,
                                      adapter=adapter)

        # ⑥ 能力校验（会删段，删完必须重新拼接）
        capability = self._step_capability(tenant_id, target)
        mode = _resolve_mode(template_doc=template_doc,
                             capability=capability, options=options)
        parameters = self._parameters(shot=shot, adapter=adapter, body=body)
        kept, cap_warnings = check_capabilities(
            fragments=fragments, capability=capability, mode=mode,
            adapter=adapter, options=options, parameters=parameters)
        warnings.extend(cap_warnings)

        separator = (adapter.get("section_separator")
                     or DEFAULT_SECTION_SEPARATOR)
        prompt_text, derived_from = join_fragments(
            kept, target="main", separator=separator)
        negative, _ = join_fragments(kept, target="negative", separator="，")
        if not negative:
            # 没有 `{{#negative}}` 段时用模板声明的静态负向词：它们是模板
            # 字面量，不是从镜头来的数据。
            negative = "，".join(
                str(item) for item in (template_doc["body"].get("negative")
                                       or []) if item)
        check_coverage(prompt_text, derived_from)

        # 对白零变换在**定稿文本**上验：先验后删会放过"删掉了对白段但
        # manifest 仍声称有对白"的情况。
        dialogue_segments = self._step_dialogue(shot, prompt_text,
                                                derived_from)

        # 传本模态的 detail 而不是整个快照：不同模态的档位不同，混在一起会
        # 让 t2v 的目标时长拿到 i2v 的档位。
        duration = resolve_duration(
            target_seconds=_target_seconds(shot),
            capability=_mode_detail(capability, mode) or capability,
            aspect_ratio=parameters.get("aspect_ratio"))
        self._step_limits(prompt_text, template_doc=template_doc,
                          adapter=adapter, capability=capability, mode=mode)
        alignment_ref = self._step_alignment(tenant_id, body, shot)
        self._step_forbidden(prompt_text, tenant_id=tenant_id, shot=shot)

        return self._step_save(
            tenant_id=tenant_id, command_id=command_id, shot=shot,
            shot_ref=shot_ref, template=template, template_doc=template_doc,
            adapter=adapter, adapter_ref=adapter_ref, binding_ref=binding_ref,
            alignment_ref=alignment_ref, model_snapshot=target.get(
                "model_snapshot"), capability=capability, target=target,
            prompt_text=prompt_text, negative=negative,
            references=references, dialogue_segments=dialogue_segments,
            duration=duration, parameters=parameters,
            derived_from=derived_from, warnings=warnings)

    # ---- ① 校验

    def _step_validate(self, tenant_id: str, target: dict):
        template_ref = target.get("template_ref") or {}
        shot_ref = target.get("shot_ref") or {}
        template = self._revision_row(
            tenant_id, template_ref.get("id"), template_ref.get("revision"))
        template_row = self._template_row(tenant_id, template["template_id"])
        body = _loads(template["body"], {})
        template_doc = {
            "compatible_schema": _loads(template["compatible_schema"], {}),
            "modality": body.get("modality") or template_row["modality"],
            "provider": template_row["provider"],
            "model_snapshots": _loads(template["model_snapshots"], []),
            "adapter_scopes": _loads(template["adapter_scopes"], []),
            "body": body,
            "adapter_revision_min": template["adapter_revision_min"],
            "adapter_revision_max": template["adapter_revision_max"],
            "regression_set_ref": _loads(template["regression_set_ref"], {}),
            "mode": body.get("mode"),
        }
        pinned_hash = template_ref.get("content_hash")
        if pinned_hash and pinned_hash != template["content_hash"]:
            # §3.8 不变量 4：revision 不可变；hash 对不上说明有人改了它（P0）。
            raise ServiceError(
                409, "SOURCE_REVISION_MOVED",
                "template_ref.content_hash does not match the stored revision; "
                "template revisions are immutable",
                details={"source": "S20",
                         "current_content_hash": template["content_hash"]})
        if template["state"] == "DRAFT":
            raise ServiceError(422, "TEMPLATE_NOT_PUBLISHED",
                               "a DRAFT cannot be compiled (F2)")
        if template["state"] == "RETIRED":
            raise ServiceError(
                422, "TEMPLATE_RETIRED",
                "a RETIRED template cannot serve new compilations; publish a "
                "new revision (F3)")

        shot_id = shot_ref.get("id")
        revision = shot_ref.get("revision")
        if not isinstance(shot_id, str) or not shot_id:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "shot_ref.id is required")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise ServiceError(400, "INVALID_SCHEMA",
                               "shot_ref.revision must be an integer")
        payload = _upstream(
            lambda: self.shot_source(shot_id, revision)
            if self.shot_source else _missing_source("S06", "the shot"),
            "SHOT_SOURCE_UNAVAILABLE")
        shot = payload.get("shot_spec") if isinstance(payload, dict) else None
        meta = payload.get("s06_meta") if isinstance(payload, dict) else None
        if not isinstance(shot, dict) or not isinstance(meta, dict):
            raise ServiceError(503, "SHOT_SOURCE_UNAVAILABLE",
                               "S06 returned no {shot_spec, s06_meta}",
                               retryable=True)
        current_hash = meta.get("content_hash")
        if shot_ref.get("content_hash") and \
                shot_ref["content_hash"] != current_hash:
            # F1：乐观并发，不是失败。返回当前 ref 让调用方自己决定。
            raise ServiceError(
                409, "SHOT_REVISION_MOVED",
                "the shot revision moved since the caller read it",
                retryable=True,
                details={"current": {"service_id": "S06", "id": shot_id,
                                     "revision": revision,
                                     "content_hash": current_hash}})
        missing = [key for key in SHOT_REQUIRED if key not in shot]
        if missing:
            raise ServiceError(
                422, "SCHEMA_INCOMPATIBLE",
                f"ShotSpec is missing {missing}; S20 does not best-effort "
                f"parse (F4)", details={"missing": missing})
        compatible = template_doc["compatible_schema"]
        schema_id = compatible.get("id")
        if schema_id and schema_id not in ("shot-spec", "shot-spec.v1"):
            raise ServiceError(
                422, "SCHEMA_INCOMPATIBLE",
                f"template targets {schema_id!r}, but S20 compiles shot-spec "
                f"(F4)", details={"compatible_schema": compatible,
                                 "shot_schema_version":
                                 shot.get("schema_version")})
        # S20-20：`compatible_schema.revision` 与 `shot-spec.v1` 的
        # `schema_version: "1.0"` 不是同一编号体系，所以只在模板显式写了
        # `schema_version` 时才比较；`revision` 原样存、不参与判定。
        declared = compatible.get("schema_version")
        if declared and declared != shot.get("schema_version"):
            raise ServiceError(
                422, "SCHEMA_INCOMPATIBLE",
                f"template requires shot-spec {declared!r}, the upstream shot "
                f"is {shot.get('schema_version')!r} (F4)",
                details={"compatible_schema": compatible,
                         "shot_schema_version": shot.get("schema_version")})
        beats = shot.get("beats")
        if not isinstance(beats, list) or not beats:
            raise ServiceError(422, "SCHEMA_INCOMPATIBLE",
                               "beats must be a non-empty array (F5)")
        for beat in beats:
            if not isinstance(beat, dict) or "time_range" not in beat \
                    or "action" not in beat:
                # F5：散文不可编译 —— 解析器会把意图猜错。
                raise ServiceError(
                    422, "SCHEMA_INCOMPATIBLE",
                    "beats must be structured {time_range, action}; prose is "
                    "refused, not parsed (F5)", details={"beat": beat})
        return shot, {"service_id": "S06", "id": shot_id, "revision": revision,
                      "content_hash": current_hash}, template, template_doc

    # ---- ② 固定绑定

    def _step_pin(self, tenant_id: str, body: dict, shot: dict):
        """把 ShotSpec 的 `asset_bindings` 与 S10 的绑定集求交。

        ShotSpec 说"哪些资产参与"（权威是 S06），绑定集说"它们解析成了哪个
        artifact"（权威是 S10）。S20 只读这两者、**不解析资产本体**。

        S20-44：缺角色时**不在这里定码**。`REFERENCE_DANGLING`（F6，改 S10）
        还是 `REFERENCE_ORDER_UNRESOLVABLE`（F9，改 adapter 或绑定集）取决于
        adapter 有没有点名该角色，而 adapter 在步骤 ③ 才选出来 —— 所以缺角色
        先收集，到 ④ 再判。
        """
        binding_ref = body.get("binding_set_ref") or {}
        binding_id = binding_ref.get("id")
        if not isinstance(binding_id, str) or not binding_id:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "binding_set_ref.id is required")
        revision = binding_ref.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise ServiceError(400, "INVALID_SCHEMA",
                               "binding_set_ref.revision must be an integer")
        payload = _upstream(
            lambda: self.binding_source(binding_id, revision)
            if self.binding_source
            else _missing_source("S10", "the binding set"),
            "BINDING_SOURCE_UNAVAILABLE")
        if not isinstance(payload, dict):
            raise ServiceError(503, "BINDING_SOURCE_UNAVAILABLE",
                               "S10 returned no binding set revision",
                               retryable=True)
        if binding_ref.get("content_hash") and \
                binding_ref["content_hash"] != payload.get("content_hash"):
            raise ServiceError(
                409, "SOURCE_REVISION_MOVED",
                "binding_set_ref.content_hash does not match S10's current "
                "value", retryable=True,
                details={"source": "S10",
                         "current_content_hash": payload.get("content_hash")})
        entries = ((payload.get("payload") or {}).get("entries")) or []
        by_role: dict = {}
        for entry in entries:
            role_key = entry.get("role_key")
            if isinstance(role_key, str) and ":" in role_key:
                by_role[role_key] = entry

        pinned: list = []
        missing: list = []
        for role, asset_type, local_id, hint in _shot_asset_roles(shot):
            role_key = f"{asset_type}:{local_id}"
            entry = by_role.get(role_key)
            if entry is None:
                missing.append({"role": role, "role_key": role_key})
                continue
            pinned.append({
                "global_asset_id": role_key,
                "role": role,
                "order_hint": hint,
                "artifact_ref": {
                    "service_id": entry.get("owner_service"),
                    "id": entry.get("owner_local_id"),
                    "revision": entry.get("owner_revision"),
                    "content_hash": entry.get("owner_hash"),
                },
                "rights_ref": entry.get("rights_ref"),
                "variant_selector": entry.get("variant_selector"),
            })
        return pinned, missing, {
            "service_id": "S10", "id": binding_id, "revision": revision,
            "content_hash": payload.get("content_hash")}

    # ---- ③ adapter

    def _step_adapter(self, tenant_id: str, target: dict,
                      template_doc: dict):
        snapshot = target.get("model_snapshot")
        _check_snapshot(snapshot)
        provider = template_doc.get("provider") or ""
        scopes = set(template_doc.get("adapter_scopes") or [])
        rows = self.conn.execute(
            "SELECT * FROM adapter_binding WHERE tenant_id=? AND provider=? "
            "ORDER BY adapter_id, revision DESC", (tenant_id, provider)
        ).fetchall()
        adapters = [self._adapter_out(dict(row)) for row in rows]
        if scopes:
            adapters = [a for a in adapters if a["adapter_id"] in scopes]
        adapter, fallback = select_adapter(
            adapters, provider=provider, model_snapshot=snapshot)

        requested = target.get("adapter_binding_ref") or {}
        if requested.get("id") and requested["id"] != adapter["adapter_id"]:
            # 选择规则是权威（§3.3）；点名不一致说明调用方读了过期数据。
            raise ServiceError(
                409, "SOURCE_REVISION_MOVED",
                f"requested adapter {requested['id']!r} is not the adapter "
                f"selected for {snapshot!r} ({adapter['adapter_id']!r})",
                retryable=True,
                details={"source": "S20", "selected": _adapter_ref(adapter)})
        if requested.get("content_hash") and \
                requested["content_hash"] != adapter["content_hash"]:
            raise ServiceError(
                409, "SOURCE_REVISION_MOVED",
                "adapter_binding_ref.content_hash is stale", retryable=True,
                details={"source": "S20", "selected": _adapter_ref(adapter)})
        if not (template_doc["adapter_revision_min"]
                <= adapter["revision"]
                <= template_doc["adapter_revision_max"]):
            raise ServiceError(
                422, "ADAPTER_NOT_APPLICABLE",
                f"adapter revision {adapter['revision']} is outside the "
                f"template's [{template_doc['adapter_revision_min']}, "
                f"{template_doc['adapter_revision_max']}] (F27)",
                details={"adapter_revision": adapter["revision"],
                         "min": template_doc["adapter_revision_min"],
                         "max": template_doc["adapter_revision_max"]})
        return adapter, fallback, _adapter_ref(adapter)

    # ---- ④ 参考图

    def _step_order(self, pinned: list, missing: list, adapter: dict):
        """排序 + token 映射（§3.4）。缺角色的两种码在这里分开（S20-44）。

        - adapter 点名了却拿不到 → 本函数内 `REFERENCE_ORDER_UNRESOLVABLE`；
        - adapter 没点名、绑定集也没有 → 绑定集缺条目 → `REFERENCE_DANGLING`。
        """
        rule = adapter.get("reference_order_rule")
        named = {item["role"] for item in _rule_parts(rule)[0]}
        dangling = [m for m in missing if m["role"] not in named]
        if dangling:
            raise ServiceError(
                422, "REFERENCE_DANGLING",
                f"the binding set has no entry for "
                f"{[m['role_key'] for m in dangling]}; ShotSpec declares these "
                f"assets (F6)", details={"missing": dangling})
        if not pinned and not named:
            return [], []
        return order_references(
            pinned, rule, adapter_id=adapter["adapter_id"],
            token_format=adapter["reference_token_format"])

    # ---- ⑤ 渲染

    def _step_render(self, *, shot: dict, template_doc: dict,
                     adapter: dict) -> list:
        try:
            return render_sections(
                sections=template_doc["body"].get("sections") or [], root=shot,
                phrases=adapter.get("phrase_tables") or {},
                speaker_prefix=adapter.get("speaker_prefix") or "")
        except RenderError as exc:
            # 渲染器的错误码就是 §7 的码；这里只换异常类型，不重新解释。
            raise ServiceError(exc.status, exc.code, str(exc),
                               details=exc.details) from exc

    # ---- 对白

    @staticmethod
    def _step_dialogue(shot: dict, prompt_text: str, derived_from: list):
        """§4.2 第 1 条 + §3.8 不变量 5。返回 `dialogue_segments`。

        两条判定：

        1. 对白的每个输出区间必须是 `verbatim`。模板用普通插值
           （`{{ dialogue[0].text }}`）或 `join()` 输出对白也会产生正确字节，
           但那两条路上未来加任何格式化都会静默改写对白 —— 所以判 P0。
        2. `verbatim` 区间的字节必须与 ShotSpec 原文**逐字节相等**。只检查
           "原文是否出现在 prompt 里"会漏掉截断（前缀仍在）。
        """
        dialogue = shot.get("dialogue") or []
        spans_by_index: dict = {}
        for entry in derived_from:
            path = entry.get("path")
            index = _dialogue_index(path) if isinstance(path, str) else None
            if index is None:
                continue
            spans_by_index.setdefault(index, []).append(entry)

        segments: list = []
        for index, line in enumerate(dialogue):
            if not isinstance(line, dict):
                continue
            text = line.get("text")
            if not isinstance(text, str):
                raise ServiceError(
                    422, "SCHEMA_INCOMPATIBLE",
                    f"dialogue[{index}].text must be a string (F4)")

            for entry in spans_by_index.get(index, []):
                if entry.get("kind") != "verbatim":
                    raise ServiceError(
                        422, "DIALOGUE_TEXT_DRIFT",
                        f"dialogue[{index}] left the compiler as "
                        f"{entry.get('kind')!r}; dialogue may only leave via "
                        f"`verbatim` (F15, P0)",
                        details={"line_id": line.get("line_id"),
                                 "index": index,
                                 "reason": "DIALOGUE_NOT_VERBATIM"})
                start, end = entry["span"]
                rendered = prompt_text[start:end]
                if rendered != text:
                    raise ServiceError(
                        422, "DIALOGUE_TEXT_DRIFT",
                        f"dialogue[{index}] rendered bytes differ from the "
                        f"ShotSpec text; the compiler never rewrites dialogue "
                        f"(F15, P0)",
                        details={"line_id": line.get("line_id"),
                                 "index": index,
                                 "reason": "DIALOGUE_BYTES_DIFFER",
                                 "expected_sha256": _sha256_hex(text),
                                 "rendered_sha256": _sha256_hex(rendered)})
            if not spans_by_index.get(index):
                # 模板没输出这句对白：纯视觉镜头合法。但凡它出现在正文里
                # 而没走 verbatim（例如被 join() 合并），就是漂移。
                if len(text) >= 2 and _occurs_outside_verbatim(
                        prompt_text, text, derived_from):
                    raise ServiceError(
                        422, "DIALOGUE_TEXT_DRIFT",
                        f"dialogue[{index}] appears in prompt_text but not "
                        f"inside a `verbatim` span; dialogue may only leave via "
                        f"`verbatim` (F15, P0)",
                        details={"line_id": line.get("line_id"),
                                 "index": index,
                                 "reason": "DIALOGUE_NOT_VERBATIM"})
                continue
            segments.append({
                "line_id": line.get("line_id"),
                "speaker": line.get("character_id"),
                "text": text,
                "text_sha256": _sha256_hex(text),
                "emotion": line.get("emotion"),
                "render": "verbatim",
            })
        return segments

    # ---- ⑥ 能力

    def _step_capability(self, tenant_id: str, target: dict) -> dict:
        ref = target.get("capability_snapshot_ref") or {}
        snapshot_id = ref.get("id")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "capability_snapshot_ref.id is required")
        capability = _upstream(
            lambda: self.capability_source(snapshot_id)
            if self.capability_source
            else _missing_source("S24", "the capability snapshot"),
            "CAPABILITY_SOURCE_UNAVAILABLE")
        if not isinstance(capability, dict):
            raise ServiceError(503, "CAPABILITY_SOURCE_UNAVAILABLE",
                               "S24 returned no capability snapshot",
                               retryable=True)
        # S20-34：S24 的响应没有 content_hash，用 JCS 派生。调用方给了 pin
        # 就比对 —— 快照变了要能发现，而不是静默用新的能力集编旧模板。
        derived = _hash_of(capability)
        if ref.get("content_hash") and ref["content_hash"] != derived:
            raise ServiceError(
                409, "SOURCE_REVISION_MOVED",
                "capability_snapshot_ref.content_hash does not match "
                "sha256(JCS(S24 response)); the snapshot moved",
                retryable=True,
                details={"source": "S24", "derived_content_hash": derived})
        out = dict(capability)
        out["_derived_content_hash"] = derived
        return out

    @staticmethod
    def _parameters(*, shot: dict, adapter: dict, body: dict) -> dict:
        output = shot.get("output_spec") or {}
        parameters: dict = {"aspect_ratio": output.get("aspect_ratio")}
        resolution = {"width": output.get("width"),
                      "height": output.get("height")}
        if resolution["width"] is not None or resolution["height"] is not None:
            parameters["resolution"] = resolution
        if output.get("fps") is not None:
            parameters["fps"] = output["fps"]
        if isinstance(body.get("tones"), dict):
            parameters["tones"] = body["tones"]
        # S20-16：`audio_strategy` 在 generation-command 里，ShotSpec 没有。
        strategy = body.get("audio_strategy") or shot.get("audio_strategy")
        if strategy:
            parameters["audio_strategy"] = strategy
        field_map = adapter.get("field_map") or {}
        for key, value in (body.get("parameters") or {}).items():
            parameters[field_map.get(key, key)] = value
        return {k: v for k, v in parameters.items() if v is not None}

    # ---- 限额

    @staticmethod
    def _step_limits(prompt_text: str, *, template_doc: dict, adapter: dict,
                     capability: dict, mode) -> None:
        """F14。**绝不截断。** 返回实际值与上限，让调用方改模板。"""
        max_chars = template_doc["body"].get("max_chars")
        if isinstance(max_chars, int) and len(prompt_text) > max_chars:
            raise ServiceError(
                422, "RENDER_LIMIT_EXCEEDED",
                f"rendered prompt is {len(prompt_text)} chars, over the "
                f"template's max_chars={max_chars}; S20 never truncates (F14)",
                details={"kind": "max_chars", "actual": len(prompt_text),
                         "limit": max_chars})
        budget = adapter.get("token_budget")
        estimate = estimate_tokens(prompt_text)
        if isinstance(budget, int) and estimate > budget:
            raise ServiceError(
                422, "RENDER_LIMIT_EXCEEDED",
                f"estimated {estimate} tokens, over the adapter's "
                f"token_budget={budget} (F14)",
                details={"kind": "token_budget", "actual": estimate,
                         "limit": budget})
        prompt_max = (_mode_detail(capability, mode) or {}).get(
            "prompt_max_chars")
        if isinstance(prompt_max, int) and len(prompt_text) > prompt_max:
            raise ServiceError(
                422, "RENDER_LIMIT_EXCEEDED",
                f"rendered prompt is {len(prompt_text)} chars, over the "
                f"model's prompt_max_chars={prompt_max} (F14)",
                details={"kind": "prompt_max_chars",
                         "actual": len(prompt_text), "limit": prompt_max})

    # ---- 对齐

    def _step_alignment(self, tenant_id: str, body: dict, shot: dict):
        """只在"有对白且原生锁定音色"时必填（§2.4、F16、T20-72）。

        S20 不估对白时长：估出来的数字会在口型阶段变成可见错误，而且错得
        很晚。缺就报错，让上层去要 S37。
        """
        dialogue = shot.get("dialogue") or []
        strategy = body.get("audio_strategy") or shot.get("audio_strategy")
        needs = bool(dialogue) and strategy == "native_locked_voice"
        ref = body.get("dialogue_alignment_ref")
        if not needs:
            return ref if isinstance(ref, dict) and ref.get("id") else None
        if not isinstance(ref, dict) or not ref.get("id"):
            raise ServiceError(
                422, "DIALOGUE_ALIGNMENT_REQUIRED",
                "a native_locked_voice shot with dialogue requires "
                "dialogue_alignment_ref; S20 does not estimate dialogue "
                "duration (F16)")
        source = self.alignment_source
        if isinstance(source, dict):
            call = lambda: source["report"](ref["id"])  # noqa: E731
        elif callable(source):
            call = lambda: source(ref["id"])  # noqa: E731
        else:
            call = None
        payload = _upstream(
            lambda: call() if call is not None
            else _missing_source("S37", "the dialogue alignment"),
            "ALIGNMENT_SOURCE_UNAVAILABLE")
        if not isinstance(payload, dict):
            raise ServiceError(503, "ALIGNMENT_SOURCE_UNAVAILABLE",
                               "S37 returned no alignment report",
                               retryable=True)
        current = _hash_of(payload)
        if ref.get("content_hash") and ref["content_hash"] != current:
            raise ServiceError(
                409, "SOURCE_REVISION_MOVED",
                "dialogue_alignment_ref.content_hash is stale", retryable=True,
                details={"source": "S37", "derived_content_hash": current})
        return {"service_id": "S37", "id": ref["id"],
                "revision": ref.get("revision"), "content_hash": current}

    # ---- 禁用字段

    @staticmethod
    def _step_forbidden(prompt_text: str, *, tenant_id: str,
                        shot: dict) -> None:
        """F18，与 S19 §4.2 同一条：标识符不内联。

        查的是**值**，不是键名 —— 键名出现在模板字面量里也同样拒绝，因为那
        说明模板作者在往 prompt 里塞标识符。短于 3 字符的值不查：那种串在
        正文里必然是巧合，查了只会制造误报。
        """
        candidates = []
        if tenant_id:
            candidates.append(("tenant_id", tenant_id))
        project = shot.get("project_id")
        if isinstance(project, str) and project:
            candidates.append(("project_id", project))
        for field, value in candidates:
            if len(value) >= 3 and value in prompt_text:
                raise ServiceError(
                    422, "OUTPUT_CONTAINS_FORBIDDEN_FIELD",
                    f"prompt_text contains the {field} value; identifiers must "
                    f"stay as refs, never inline (F18)",
                    details={"field": field})
        for literal in ("tenant_id", "project_id"):
            if literal in prompt_text:
                raise ServiceError(
                    422, "OUTPUT_CONTAINS_FORBIDDEN_FIELD",
                    f"prompt_text mentions {literal!r} (F18)",
                    details={"field": literal})

    # ---- ⑦ 保存

    def _step_save(self, **kw) -> dict:
        tenant_id = kw["tenant_id"]
        manifest_inputs = {
            "shot_ref": kw["shot_ref"],
            "template_source_ref": {
                "id": kw["template"]["template_id"],
                "revision": kw["template"]["revision"],
                "content_hash": kw["template"]["content_hash"]},
            "adapter_ref": kw["adapter_ref"],
            # 用**请求的**快照，不是 adapter 的：通配 adapter 的
            # `model_snapshot` 是 `'*'`，记进 manifest 会丢掉"这次到底给谁编"。
            "model_snapshot": kw["model_snapshot"],
            "capability_snapshot_ref": {
                "service_id": "S24",
                "id": (kw["target"].get("capability_snapshot_ref")
                       or {}).get("id"),
                "revision": (kw["target"].get("capability_snapshot_ref")
                             or {}).get("revision"),
                "content_hash": kw["capability"]["_derived_content_hash"]},
            "binding_set_ref": kw["binding_ref"],
            "dialogue_alignment_ref": kw["alignment_ref"],
            "prompt_text": kw["prompt_text"],
            "negative_prompt": kw["negative"] or None,
            "references": kw["references"],
            "dialogue_segments": kw["dialogue_segments"],
            "duration": kw["duration"],
            "parameters": kw["parameters"],
            "derived_from": kw["derived_from"],
            "warnings": kw["warnings"],
        }
        content_hash = manifest_content_hash(manifest_inputs)

        execution = kw["target"].get("execution_context") or {}
        node_id = execution.get("node_id")
        node_epoch = execution.get("node_epoch")
        execution_fence = execution.get("execution_fence")
        for name in ("node_id", "node_epoch", "execution_fence"):
            if execution.get(name) is None:
                # C2：管理路径可省，编译路径不可省。
                raise ServiceError(
                    400, "INVALID_SCHEMA",
                    f"execution_context.{name} is required to compile a shot; "
                    f"template management may omit it, compilation may not")
        if not isinstance(node_id, str) or not node_id:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "execution_context.node_id must be a string")
        for name, value in (("node_epoch", node_epoch),
                            ("execution_fence", execution_fence)):
            if not isinstance(value, int) or isinstance(value, bool) \
                    or value < 1:
                raise ServiceError(
                    400, "INVALID_SCHEMA",
                    f"execution_context.{name} must be an integer >= 1")

        now = _now()
        manifest_id = _new_id("manifest")
        template_lock = {"service_id": SOURCE, "id": manifest_id,
                         "revision": 1, "content_hash": content_hash}
        tokens = estimate_tokens(kw["prompt_text"])
        modality = kw["template_doc"]["modality"]

        with Transaction(self.conn):
            # §6.2 第二层：不同 command_id、相同输入 → 同一份 manifest。
            # 先查再插而不是靠 ON CONFLICT —— 需要在同一事务里拿到既有 id。
            existing = self.conn.execute(
                "SELECT manifest_id FROM compiled_prompt_manifest WHERE "
                "tenant_id=? AND json_extract(shot_ref,'$.id')=? AND "
                "json_extract(template_source_ref,'$.id')=? AND "
                "json_extract(template_source_ref,'$.revision')=? AND "
                "model_snapshot=? AND json_extract(adapter_ref,'$.revision')=? "
                "AND json_extract(binding_set_ref,'$.content_hash')=? AND "
                "content_hash=?",
                (tenant_id, kw["shot_ref"].get("id"),
                 kw["template"]["template_id"], kw["template"]["revision"],
                 kw["model_snapshot"], kw["adapter_ref"]["revision"],
                 kw["binding_ref"].get("content_hash"), content_hash)
            ).fetchone()
            if existing is not None:
                return {"manifest_id": existing["manifest_id"], "revision": 1,
                        "content_hash": content_hash, "modality": modality,
                        "deduplicated": True, "warnings": kw["warnings"]}
            self.conn.execute(
                "INSERT INTO compiled_prompt_manifest (tenant_id, manifest_id,"
                " revision, modality, shot_ref, template_ref,"
                " template_source_ref, adapter_ref, model_snapshot,"
                " capability_snapshot_ref, binding_set_ref,"
                " dialogue_alignment_ref, prompt_text, negative_prompt,"
                " tokens_estimate, refs, dialogue_segments, duration,"
                " parameters, derived_from, warnings, node_id, node_epoch,"
                " execution_fence, content_hash, compiled_at)"
                " VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, manifest_id, modality, _dumps(kw["shot_ref"]),
                 _dumps(template_lock),
                 _dumps(manifest_inputs["template_source_ref"]),
                 _dumps(kw["adapter_ref"]), kw["model_snapshot"],
                 _dumps(manifest_inputs["capability_snapshot_ref"]),
                 _dumps(kw["binding_ref"]),
                 _dumps(kw["alignment_ref"]) if kw["alignment_ref"] else None,
                 kw["prompt_text"], kw["negative"] or None, tokens,
                 _dumps(kw["references"]), _dumps(kw["dialogue_segments"]),
                 _dumps(kw["duration"]), _dumps(kw["parameters"]),
                 _dumps(kw["derived_from"]), _dumps(kw["warnings"]), node_id,
                 node_epoch, execution_fence, content_hash, now))
            self._emit_compiled(
                tenant_id=tenant_id, manifest_id=manifest_id,
                warnings=kw["warnings"], shot_ref=kw["shot_ref"],
                template_source_ref=manifest_inputs["template_source_ref"],
                model_snapshot=kw["model_snapshot"], node_id=node_id,
                node_epoch=node_epoch, execution_fence=execution_fence,
                content_hash=content_hash, shot=kw["shot"],
                command_id=kw["command_id"])
        return {"manifest_id": manifest_id, "revision": 1,
                "content_hash": content_hash, "modality": modality,
                "template_ref": template_lock, "tokens_estimate": tokens,
                "warnings": kw["warnings"], "compiled_at": now,
                "deduplicated": False}

    def _emit_compiled(self, *, tenant_id, manifest_id, warnings, shot_ref,
                       template_source_ref, model_snapshot, node_id,
                       node_epoch, execution_fence, content_hash, shot,
                       command_id) -> None:
        """§4.3。**事件不带 `prompt_text`**（E31 的多副本留存风险）。

        `prompt.compiled.v2.schema.json` 的 `warnings` 是 `string[]`（PCM-2），
        而 manifest 存对象 —— 转换只在这里发生（S20-35）。

        `event_sdk` 的信封把序号写成 `sequence`，契约要求
        `aggregate_sequence`（S20-C12）。这里通过 `extra` 补上契约要求的键，
        `sequence` 仍由 SDK 输出 —— **不改平台 SDK**，差异登记在 CONTRACT_GAPS。
        """
        data = {
            "schema_version": SCHEMA_VERSION,
            "manifest_ref": {"id": manifest_id, "revision": 1,
                             "content_hash": content_hash},
            "shot_ref": shot_ref,
            "template_ref": {
                "id": template_source_ref["id"],
                "revision": template_source_ref["revision"],
                "content_hash": template_source_ref["content_hash"]},
            "model_snapshot": model_snapshot,
            "warnings": _event_warnings(warnings),
            "node_id": node_id,
            "node_epoch": node_epoch,
            "execution_fence": execution_fence,
        }
        envelope = new_event(
            source=SOURCE, type=EVENT_PROMPT_COMPILED,
            subject=f"compiled-prompt/{manifest_id}", tenant_id=tenant_id,
            home_cell=str(shot.get("home_cell") or "cell_unknown"),
            aggregate_id=manifest_id, sequence=1, correlation_id=command_id,
            causation_id=command_id, data=data,
            data_ref={"service_id": SOURCE, "id": manifest_id, "revision": 1,
                      "content_hash": content_hash})
        envelope.extra["aggregate_sequence"] = 1
        self.outbox.append(envelope)

    # ------------------------------------------------------------ 读取

    def get_manifest(self, tenant_id: str, manifest_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM compiled_prompt_manifest WHERE tenant_id=? AND "
            "manifest_id=?", (tenant_id, manifest_id)).fetchone()
        if row is None:
            # 跨租户一律 404（E28 / T20-47），不泄漏存在性。
            raise ServiceError(404, "NOT_FOUND",
                               f"manifest {manifest_id!r} not found")
        return self._manifest_out(dict(row))

    @staticmethod
    def _manifest_out(row: dict) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "manifest_id": row["manifest_id"],
            "revision": row["revision"],
            "tenant_id": row["tenant_id"],
            "modality": row["modality"],
            "shot_ref": _loads(row["shot_ref"], {}),
            "template_ref": _loads(row["template_ref"], {}),
            "template_source_ref": _loads(row["template_source_ref"], {}),
            "adapter_ref": _loads(row["adapter_ref"], {}),
            "model_snapshot": row["model_snapshot"],
            "capability_snapshot_ref": _loads(
                row["capability_snapshot_ref"], {}),
            "binding_set_ref": _loads(row["binding_set_ref"], {}),
            "dialogue_alignment_ref": _loads(row["dialogue_alignment_ref"]),
            "prompt_text": row["prompt_text"],
            "negative_prompt": row["negative_prompt"],
            "tokens_estimate": row["tokens_estimate"],
            "references": _loads(row["refs"], []),
            "dialogue_segments": _loads(row["dialogue_segments"], []),
            "duration": _loads(row["duration"], {}),
            "parameters": _loads(row["parameters"], {}),
            "derived_from": _loads(row["derived_from"], []),
            "warnings": _loads(row["warnings"], []),
            "node_id": row["node_id"],
            "node_epoch": row["node_epoch"],
            "execution_fence": row["execution_fence"],
            "content_hash": row["content_hash"],
            "compiled_at": row["compiled_at"],
        }


def _adapter_ref(adapter: dict) -> dict:
    return {"id": adapter["adapter_id"], "revision": adapter["revision"],
            "content_hash": adapter["content_hash"]}


def _validate_sections(sections: list, body_spec: dict) -> None:
    seen: set = set()
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ServiceError(400, "INVALID_SCHEMA",
                               f"section {index} must be an object")
        sid = section.get("id")
        if not isinstance(sid, str) or not sid:
            raise ServiceError(400, "INVALID_SCHEMA",
                               f"section {index} needs a non-empty id")
        if sid in seen:
            raise ServiceError(400, "INVALID_SCHEMA",
                               f"duplicate section id {sid!r}")
        seen.add(sid)
        render_as = section.get("render") or "verbatim"
        if render_as not in RENDER_TYPES:
            raise ServiceError(
                400, "TEMPLATE_BODY_INVALID",
                f"section {sid!r} render={render_as!r}; allowed: "
                f"{list(RENDER_TYPES)} (there is no llm_rewrite)")
        if not section.get("from") and not section.get("text"):
            raise ServiceError(400, "TEMPLATE_BODY_INVALID",
                               f"section {sid!r} has neither from nor text")
        if section.get("text") is not None and \
                not isinstance(section["text"], str):
            raise ServiceError(400, "TEMPLATE_BODY_INVALID",
                               f"section {sid!r} text must be a string")
        requires = section.get("requires") or []
        if not isinstance(requires, list):
            raise ServiceError(400, "TEMPLATE_BODY_INVALID",
                               f"section {sid!r} requires must be an array")
    negative = body_spec.get("negative")
    if negative is not None and (
            not isinstance(negative, list)
            or any(not isinstance(n, str) for n in negative)):
        raise ServiceError(400, "TEMPLATE_BODY_INVALID",
                           "body.negative must be an array of strings")
    max_chars = body_spec.get("max_chars")
    if max_chars is not None and (
            not isinstance(max_chars, int) or isinstance(max_chars, bool)
            or max_chars <= 0):
        raise ServiceError(400, "TEMPLATE_BODY_INVALID",
                           "body.max_chars must be a positive integer")


def _template_paths(text) -> list:
    """模板文本里引用的路径（不含 `#verbatim` 的内层字面量）。"""
    from prompt_compiler.render import _Parser  # 局部导入避免公开私有名

    if not text:
        return []
    out: list = []
    for node in _Parser(text).parse():
        out.extend(_node_paths(node))
    return out


def _node_paths(node) -> list:
    kind = node[0]
    if kind in ("ref", "phrase", "join", "verbatim"):
        return [node[1]]
    if kind == "if":
        out = [node[1]]
        for child in node[2]:
            out.extend(_node_paths(child))
        return out
    if kind == "negative":
        out: list = []
        for child in node[1]:
            out.extend(_node_paths(child))
        return out
    return []


def _path_declared(path: str, declared: set) -> bool:
    return any(path == root or path.startswith(root + ".")
               or path.startswith(root + "[") for root in declared)


def _optional_requirements(body: dict) -> list:
    out: list = []
    for section in body.get("sections") or []:
        if isinstance(section, dict) and section.get("optional"):
            for key in section.get("requires") or []:
                out.append(key)
    return out


# ---------------------------------------------------------------- 辅助

# 未配置的上游 → 错误码。**必须与 `deps._unconfigured` 用同一张表**：同一个
# 缺口从 HTTP 进来和从内核进来要拿到同一个码，否则调用方按码写的重试策略
# 在两条路径上行为不同（`S06_SOURCE_UNAVAILABLE` 与 `SHOT_SOURCE_UNAVAILABLE`
# 曾是两套名字，前者还不在 `api/app.py` 的 Retry-After 名单里）。
_MISSING_SOURCE_CODES = {
    "S06": "SHOT_SOURCE_UNAVAILABLE",
    "S10": "BINDING_SOURCE_UNAVAILABLE",
    "S24": "CAPABILITY_SOURCE_UNAVAILABLE",
    "S37": "ALIGNMENT_SOURCE_UNAVAILABLE",
}


def _missing_source(service_id: str, what: str):
    raise ServiceError(
        503, _MISSING_SOURCE_CODES.get(service_id,
                                       f"{service_id}_SOURCE_UNAVAILABLE"),
        f"{service_id} is not configured; {what} is a pinned input and cannot "
        f"be omitted", retryable=True)


def _resolve_mode(*, template_doc: dict, capability: dict, options: dict):
    """S20-22：S24 的 `mode`（t2v/i2v/…）与模板的 `modality`（video/…）之间
    没有契约映射，所以按「显式 `options.mode` → 模板声明的 `mode` → 快照第一条
    `mode_details` → `modality`」取，不发明映射表。
    """
    explicit = (options or {}).get("mode") or template_doc.get("mode")
    if explicit:
        return explicit
    details = (capability or {}).get("mode_details") or []
    if details:
        return details[0].get("mode")
    return template_doc.get("modality")


_DIALOGUE_PATH_RE = __import__("re").compile(
    r"^dialogue(?:\[(\d+)\]|\.(\d+))(?:\.text)?$")


def _dialogue_index(path: str):
    """`dialogue[3].text` / `dialogue.3.text` → 3；其它路径 → None。"""
    match = _DIALOGUE_PATH_RE.match(path)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _occurs_outside_verbatim(prompt_text: str, text: str,
                             derived_from: list) -> bool:
    """`text` 是否出现在正文里、且不在任何一个 `verbatim` 区间内。

    用于抓 `join()` / 普通插值把对白带出去的情况（T20-13）。
    """
    spans = []
    for entry in derived_from:
        if entry.get("kind") == "verbatim":
            start, end = entry["span"]
            spans.append((start, end))
    start = prompt_text.find(text)
    while start != -1:
        end = start + len(text)
        if not any(lo <= start and end <= hi for lo, hi in spans):
            return True
        start = prompt_text.find(text, start + 1)
    return False


def _target_seconds(shot: dict) -> float:
    value = (shot.get("duration") or {}).get("target_seconds")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ServiceError(
            422, "SCHEMA_INCOMPATIBLE",
            "shot.duration.target_seconds is required to resolve provider "
            "tiers (F4)")
    return float(value)


def _shot_asset_roles(shot: dict) -> list:
    """从 `ShotSpec.asset_bindings` 推出 `(role, asset_type, local_id, hint)`。

    S20-12 / S20-13：契约里没有 `reference_bindings[]` 也没有
    `global_asset_id`；实际是 `characters[]/scene/props[]/style`。而 `style`
    在 S10 的 `ASSET_TYPES` 里没有对应类型，写不进绑定集 —— 所以它**不**作为
    参考图解析。adapter 若点名 `style` 角色，会在 ④ 得到
    `REFERENCE_ORDER_UNRESOLVABLE`，而不是找一张图顶上。
    """
    bindings = shot.get("asset_bindings") or {}
    out: list = []

    def add(role, asset_type, node, hint):
        if isinstance(node, dict) and node.get("id"):
            out.append((role, asset_type, node["id"], hint))

    for index, entry in enumerate(bindings.get("characters") or []):
        if not isinstance(entry, dict):
            continue
        add("character", "character", entry.get("character"), index)
    for index, entry in enumerate(bindings.get("characters") or []):
        if not isinstance(entry, dict):
            continue
        add("costume", "costume", entry.get("costume"), 100 + index)
    for index, node in enumerate(bindings.get("props") or []):
        add("prop", "prop", node, 200 + index)
    add("scene", "scene", bindings.get("scene"), 300)
    return out


def _event_warnings(warnings: list) -> list:
    """manifest 的 warning 对象 → 事件的 `string[]`（PCM-2 / S20-35）。

    `CODE` 或 `CODE:field`。单一转换点：事件契约只接受字符串，而 manifest
    需要 `field` 才能说清省略了什么。
    """
    out: list = []
    for warning in warnings or []:
        if isinstance(warning, str):
            out.append(warning)
            continue
        code = warning.get("code")
        if code not in WARNING_CODES:
            # 不属于已知 warning 码的东西不该悄悄进事件。
            continue
        field = warning.get("field")
        out.append(f"{code}:{field}" if field else code)
    return out
