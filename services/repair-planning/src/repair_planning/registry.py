"""S39 的注册表加载器与路由代数（方案 §3.1 / §3.1.1 / §3.1.2）。

**S39 不认识任何具体的 check_id。** 它只按数据走：

```text
family     = action_families[defect.type].chain          ← contracts 14 值闭集
entry      = registry[defect.check_id]                    ← 质检服务的登记项
candidates = family ∩ entry.allowed_actions ∩ policy.allowed_actions
起点        = entry.default_action
升级        = entry.escalation_rule（数值阈值 + 升级后动作）
```

数据文件 `contracts/quality/check-registry.v2.json` 还没落（§11 C5），所以本
模块是**加载器 + 加载期校验器**，不是一份内建注册表。未配置注册表时命令
503——编一份"默认注册表"等于 S39 替四个质检服务登记检查，且猜错的动作会
花钱、可能改坏已验收内容（§3.1.1）。

三条硬规则在代码里表达，且**不可被 registry 数据覆盖**（§3.1.2）：

1. `keyframe_edit` 只在 `window_scope ∈ {frame, window}` 且窗口 ≤ 6 帧可用。
   登记到 `shot`/`track`/`all` 的条目在加载期就判无效；运行期再挡一次。
2. `never_auto_repair: true` → 人审，`escalation.reason` 取 `no_auto_reason`。
   代码里**不写死任何授权类 check_id**。
3. 动作必须属于该 `type` 的族。跨族动作一律拒绝——即使 registry 这么写。

加载期校验复刻 `contracts/quality/validate_registry.py` 的规则（那份是参考
实现，JSON Schema 表达不了跨文件约束）。测试里会拿同一份校验器对拍。
"""

from __future__ import annotations

import json

KEYFRAME_MAX_FRAMES = 6

LAYER_OF_PREFIX = {"TECH": "technical", "VIS": "semantic",
                   "AUD": "alignment", "SUB": "subtitle"}
LAYER_OWNER = {"technical": "S35", "semantic": "S36",
               "alignment": "S37", "subtitle": "S38"}

SCOPE_WINDOW = ("frame", "window")

# 需要时序能力的动作（§3.2）。`keyframe_edit` 是逐帧编辑，明确不在其中。
TEMPORAL_ACTIONS = frozenset({"temporal_edit", "regenerate_video"})
ACTION_PREFERENCE = (
    "reingest", "transcode", "remix", "realign_subtitles", "keyframe_edit",
    "temporal_edit", "redo_lipsync", "re_render_subtitles",
    "re_synthesize_speech", "change_spec", "regenerate_video",
)


class RegistryError(Exception):
    """注册表/动作族数据不合格。**加载期拒绝**，不降级成"先跑起来"。"""


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def validate_families(families: dict) -> list[str]:
    """动作族内部自洽（`action-families.v2.json` 的 `invariants`）。"""
    errors = []
    enum = list(families.get("action_enum") or [])
    closed = families.get("closed_type_set") or []
    table = families.get("families") or {}
    if not enum or not closed:
        return ["action-families: action_enum / closed_type_set is empty"]
    if set(closed) != set(table):
        errors.append("action-families: families keys != closed_type_set")
    for kind in closed:
        fam = table.get(kind)
        if not isinstance(fam, dict):
            continue
        chain = list(fam.get("chain") or [])
        if not chain:
            errors.append(f"action-families[{kind}]: empty chain")
        for action in chain:
            if action not in enum:
                errors.append(
                    f"action-families[{kind}]: chain action {action!r} not in "
                    f"action_enum")
        if "human_review" in chain:
            errors.append(
                f"action-families[{kind}]: human_review must not be a chain "
                f"member (it is the shared terminal state)")
        fallback = fam.get("fallback")
        if fallback != "human_review" and fallback not in chain:
            errors.append(
                f"action-families[{kind}]: fallback {fallback!r} is neither "
                f"human_review nor a chain member")
        for item in fam.get("forbidden") or []:
            action = (item or {}).get("action")
            if action not in enum:
                errors.append(
                    f"action-families[{kind}]: forbidden action {action!r} "
                    f"not in action_enum")
    return errors


def validate_entry(entry: dict, families: dict) -> list[str]:
    """单条登记项的族校验。与 `validate_registry.py` 的规则逐条对应。

    这里是**加载期**闸门：整份注册表有任何一条不合格就不加载。运行期
    `Registry.entry_errors` 会再算一次，防止有人绕过加载器直接塞数据。
    """
    errors = []
    check_id = entry.get("check_id", "<entry>")
    closed = set(families["closed_type_set"])
    table = families["families"]

    kind = entry.get("defect_type")
    if kind not in closed:
        return [f"{check_id}: defect_type {kind!r} not in the closed set"]

    chain = list(table[kind]["chain"])
    allowed_by_family = set(chain)

    default = entry.get("default_action")
    if default not in allowed_by_family:
        errors.append(
            f"{check_id}: default_action {default!r} is not in the {kind} "
            f"family chain {chain}")

    for action in entry.get("allowed_actions") or []:
        if action not in allowed_by_family:
            errors.append(
                f"{check_id}: allowed_actions contains {action!r}, outside "
                f"the {kind} family {sorted(allowed_by_family)}")

    rule = entry.get("escalation_rule")
    if rule is not None:
        target = rule.get("escalate_to")
        if target not in allowed_by_family:
            errors.append(
                f"{check_id}: escalation_rule.escalate_to {target!r} is "
                f"outside the {kind} family")

    if entry.get("never_auto_repair") and not entry.get("no_auto_reason"):
        errors.append(f"{check_id}: never_auto_repair=true requires "
                      f"no_auto_reason")

    # ROUTE-T4 / §3.1.2 第 1 条：逐帧动作不能登记到连续缺陷上。
    names_keyframe = "keyframe_edit" in (
        [entry.get("default_action")] + list(entry.get("allowed_actions") or []))
    if (rule or {}).get("escalate_to") == "keyframe_edit":
        names_keyframe = True
    if names_keyframe:
        scope = entry.get("window_scope")
        ceiling = entry.get("window_max_frames")
        if scope not in SCOPE_WINDOW:
            errors.append(
                f"{check_id}: names keyframe_edit but window_scope is "
                f"{scope!r}; per-frame edits need a continuous window of at "
                f"most {KEYFRAME_MAX_FRAMES} frames")
        elif ceiling is None or ceiling > KEYFRAME_MAX_FRAMES:
            errors.append(
                f"{check_id}: names keyframe_edit but window_max_frames is "
                f"{ceiling!r}; the precondition is at most "
                f"{KEYFRAME_MAX_FRAMES} frames")

    prefix = str(check_id).split(".")[0]
    layer = LAYER_OF_PREFIX.get(prefix)
    if layer is None:
        errors.append(f"{check_id}: unknown check_id prefix {prefix!r}")
    else:
        if entry.get("layer") != layer:
            errors.append(
                f"{check_id}: layer {entry.get('layer')!r} does not match "
                f"prefix {prefix!r} (expected {layer!r})")
        owner = LAYER_OWNER[layer]
        if entry.get("owner_service") != owner:
            errors.append(
                f"{check_id}: owner_service {entry.get('owner_service')!r} "
                f"does not match layer {layer!r} (expected {owner!r})")

    order = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
    low, high = entry.get("severity_default"), entry.get("severity_max")
    if low in order and high in order and order[low] > order[high]:
        errors.append(
            f"{check_id}: severity_default {low!r} exceeds severity_max "
            f"{high!r}")
    return errors


def validate_registry(document: dict, families: dict) -> list[str]:
    errors = []
    seen = set()
    for entry in document.get("checks") or []:
        check_id = entry.get("check_id")
        if check_id in seen:
            errors.append(f"{check_id}: duplicate check_id")
        seen.add(check_id)
        errors += validate_entry(entry, families)
    if not document.get("checks"):
        errors.append("registry: checks is empty")
    return errors


class Registry:
    """加载后的注册表。构造即校验；不合格直接抛，不返回半份数据。"""

    def __init__(self, document: dict, families: dict, *, path=None):
        problems = validate_families(families)
        problems += validate_registry(document, families)
        if problems:
            raise RegistryError("; ".join(problems))
        self.path = path
        self.version = int(document["registry_version"])
        self.schema_version = document.get("schema_version", "2.0")
        self.checks = list(document["checks"])
        self.by_id = {c["check_id"]: c for c in self.checks}
        self.families = families
        self.family_version = families.get("schema_version", "2.0")
        self.closed_types = tuple(families["closed_type_set"])
        self.action_enum = tuple(families["action_enum"])

    # ---------------------------------------------------------- 查询
    def entry(self, check_id: str) -> dict | None:
        return self.by_id.get(check_id)

    def entry_errors(self, entry: dict) -> list[str]:
        """运行期再校验一次（数据可能来自热加载/人工写入）。"""
        return validate_entry(entry, self.families)

    def chain(self, defect_type: str) -> list[str]:
        family = (self.families.get("families") or {}).get(defect_type)
        return list(family["chain"]) if family else []

    def fallback(self, defect_type: str) -> str | None:
        family = (self.families.get("families") or {}).get(defect_type)
        return family.get("fallback") if family else None

    def checks_owned_by(self, owner_service: str) -> list[str]:
        return [c["check_id"] for c in self.checks
                if c.get("owner_service") == owner_service]

    def document(self) -> dict:
        return {"schema_version": self.schema_version,
                "registry_version": self.version,
                "checks": self.checks}


def load_registry(registry_path: str | None,
                  families_path: str | None) -> Registry | None:
    """两个路径都给了才加载；否则返回 `None`（由调用方 503）。

    **不内建默认注册表**：没有数据时正确的行为是"路由不可用"，不是"猜"。
    """
    if not registry_path or not families_path:
        return None
    problems = []
    try:
        document = load_json(registry_path)
    except (OSError, ValueError) as exc:
        raise RegistryError(f"cannot read registry {registry_path}: {exc}")
    try:
        families = load_json(families_path)
    except (OSError, ValueError) as exc:
        raise RegistryError(f"cannot read action families {families_path}: {exc}")
    if problems:
        raise RegistryError("; ".join(problems))
    return Registry(document, families, path=registry_path)


# ------------------------------------------------------------ 路由代数

def _ordered(candidates, chain) -> list[str]:
    index = {name: i for i, name in enumerate(chain)}
    return sorted(candidates, key=lambda a: index.get(a, len(chain)))


def outward(chain: list[str], start: str | None, candidates) -> list[str]:
    """从 `start` 沿族链向"更重"的一侧走，只保留候选（§3.1 routing）。

    `start` 不在候选里时，取它之后最近的候选；若它之后没有候选，取链上最后
    一个候选（"走到头了"）。这个顺序是 `keyframe_edit → temporal_edit →
    regenerate_video` 这类链的默认推进方向。
    """
    ordered = _ordered(candidates, chain)
    if not ordered:
        return []
    if start not in chain:
        return ordered
    start_index = chain.index(start)
    beyond = [a for a in ordered if chain.index(a) >= start_index]
    return beyond or ordered[-1:]


def candidate_actions(*, chain, entry, policy_allowed=None,
                      window_scope=None, window_frames=None) -> list[str]:
    """`family ∩ entry.allowed_actions ∩ policy.allowed_actions`，再被硬规则裁剪。

    硬规则 1 在运行期再挡一次：`keyframe_edit` 只在 frame/window 且
    ≤ `KEYFRAME_MAX_FRAMES` 帧时留下。登记到 `shot` 的条目加载期就已被拒；
    这里防的是"已加载、但缺陷实际窗口很长"。
    """
    allowed = set(entry.get("allowed_actions") or chain)
    candidates = [a for a in chain if a in allowed]
    if policy_allowed is not None:
        permitted = set(policy_allowed)
        candidates = [a for a in candidates if a in permitted]

    if "keyframe_edit" in candidates:
        scope = window_scope
        frames = window_frames
        ceiling = entry.get("window_max_frames")
        ok = (scope in SCOPE_WINDOW and frames is not None
              and frames <= KEYFRAME_MAX_FRAMES
              and (ceiling is None or frames <= ceiling))
        if not ok:
            candidates = [a for a in candidates if a != "keyframe_edit"]
    return candidates


def compare(value, comparator: str, threshold) -> bool:
    try:
        left = float(value)
        right = float(threshold)
    except (TypeError, ValueError):
        return False
    return {
        "lt": left < right, "lte": left <= right,
        "gt": left > right, "gte": left >= right,
        "eq": left == right, "ne": left != right,
    }.get(comparator, False)


def escalation_metric(metric: str, *, defect: dict, media: dict | None,
                      extra: dict | None = None):
    """把 `escalation_rule.metric` 解析成一个数。

    方案没给 metric 的闭集（§3.1.1 只写"数值阈值"）。这里支持一小撮可
    从缺陷报告本身算出来的量；**算不出来就返回 None → 不升级**，这正是
    §3.1.1 表里 `escalation_rule` 的缺省行为（"不自动升级，达上限后
    human_review"）。不猜。
    """
    span = defect.get("time_range") or {}
    start = span.get("start_seconds")
    end = span.get("end_seconds")
    duration = None
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        duration = float(end) - float(start)

    if metric in ("duration_seconds", "defect_duration_seconds"):
        return duration
    if metric in ("duration_ratio", "defect_duration_ratio"):
        total = (media or {}).get("duration_seconds")
        if duration is None or not total:
            return None
        return duration / float(total)
    if metric == "measured_value":
        measured = defect.get("measured") or {}
        return measured.get("value")
    if metric == "confidence":
        return defect.get("confidence")
    if metric in ("consecutive_windows", "consecutive_windows_over_threshold"):
        extra = extra or {}
        value = extra.get("consecutive_windows")
        return value
    if metric == "coverage_ratio":
        return (extra or {}).get("coverage_ratio")
    if metric == "severity_rank":
        return {"info": 0, "minor": 1, "major": 2, "blocker": 3}.get(
            defect.get("severity"))
    return None


def escalate(entry: dict, *, defect: dict, media: dict | None,
             extra: dict | None) -> str | None:
    """返回升级后的动作；条件不满足或算不出量时返回 None（保持起点）。"""
    rule = entry.get("escalation_rule")
    if not rule:
        return None
    value = escalation_metric(rule.get("metric", ""), defect=defect,
                              media=media, extra=extra)
    if value is None:
        return None
    if compare(value, rule.get("comparator", ""), rule.get("threshold")):
        return rule.get("escalate_to")
    return None


def split_recheck_scope(value: str) -> list[str]:
    """`"semantic+continuity"` → `["semantic", "continuity"]`（§6.3）。"""
    if not value:
        return []
    return [part.strip() for part in str(value).split("+") if part.strip()]
