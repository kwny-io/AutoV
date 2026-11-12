"""§4 的合并算法。纯函数，不碰数据库、不碰时钟。

三条不变量（§4.1）靠这里的算子保证：

- I1 安全：任何 join 之后只会更严。每个 `JOIN` 算子在定义域上都单调。
- I2 合法：join **不**保证合法性，发布期另做有序校验（`service._validate_overrides`）。
- I3 可解释：每个字段回 provenance（值 + 来源层 + revision + 算子）。

算子必须满足幂等、交换、结合。三条一起保证「层缺失、乱序、投影重建」不改变结果
（§4.3）——`test_merge.py` 对每个算子都有一组代数性质用例。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

# §4.3：自主性越高序号越大，join 取最小 = 最严。
RANK: dict[str, dict[str, int]] = {
    "mode": {"guided": 0, "stage_review": 1,
             "exception_assisted": 2, "unattended_batch": 3},
    "generation_mode": {"manual": 0, "auto": 1},
    "acceptance_mode": {"manual": 0, "auto_if_qc_pass": 1},
    "exception_behavior": {"wait_review": 0, "isolate_branch": 1},
}

GATES = ("script", "canonical_asset", "storyboard", "rough_cut", "final_master",
         "budget_increase", "external_publish")

# §2 的默认推导。**只在字段从未被任何层显式设置时**才物化。
DEFAULT_GENERATION_MODE = {
    "guided": "manual", "stage_review": "auto",
    "exception_assisted": "auto", "unattended_batch": "auto",
}
DEFAULT_ACCEPTANCE_MODE = {
    "guided": "manual", "stage_review": "manual",
    "exception_assisted": "auto_if_qc_pass", "unattended_batch": "auto_if_qc_pass",
}
DEFAULT_REQUIRED_GATES = {
    "guided": ["script", "canonical_asset", "storyboard", "rough_cut", "final_master"],
    "stage_review": ["script", "canonical_asset", "storyboard", "rough_cut",
                     "final_master"],
    "exception_assisted": ["canonical_asset", "final_master"],
    "unattended_batch": ["final_master"],
}
DEFAULT_EXCEPTION_BEHAVIOR = {
    "guided": "wait_review", "stage_review": "wait_review",
    "exception_assisted": "wait_review", "unattended_batch": "isolate_branch",
}

# 层序仅用于 I2 有序校验与 explain；合成本身与顺序无关。
# 首层用 `tenant` 而不是方案文字里的 `enterprise`：`overrides[].scope_type` 的
# 枚举是四个下级 scope，企业层唯一的绑定方式是 tenant，DDL 的 CHECK 也是它。
SCOPE_CHAIN = ("tenant", "project", "episode", "stage", "node")


def join_enum(field: str, a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b, key=lambda v: RANK[field][v])


def join_union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return set(a) | set(b)


def join_intersect(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return set(a) & set(b)


def join_min(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def join_max(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def join_or(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return bool(a) or bool(b)


# `hard_rules` 的四个 const 不参与 join：改动一律 422 `HARD_RULE_IMMUTABLE`
# （§4.5 (e)），所以也没有 `join_and`——留一个没人调的算子是给后来人挖坑。
# 字段 → 算子。`budget_scope_id` 也不在这里：它是层级引用，只能有序校验
# （§3 #12），join 它等于假装它是个格。
MERGE: dict[str, Callable[[Any, Any], Any]] = {
    "mode": lambda a, b: join_enum("mode", a, b),
    "generation_mode": lambda a, b: join_enum("generation_mode", a, b),
    "acceptance_mode": lambda a, b: join_enum("acceptance_mode", a, b),
    "exception_behavior": lambda a, b: join_enum("exception_behavior", a, b),
    "required_gates": join_union,
    "allowed_actions": join_intersect,
    "reviewer_roles": join_intersect,
    "approved_evaluators": join_intersect,
    "max_business_attempts_per_shot": join_min,
    "max_identical_strategy_repeats": join_min,
    "qc_min_coverage": join_max,
    "quorum": join_max,
    "separation_of_duties": join_or,
    "locked": join_or,
}

MERGE_OP = {
    "mode": "MIN_RANK", "generation_mode": "MIN_RANK",
    "acceptance_mode": "MIN_RANK", "exception_behavior": "MIN_RANK",
    "required_gates": "UNION", "allowed_actions": "INTERSECT",
    "reviewer_roles": "INTERSECT", "approved_evaluators": "INTERSECT",
    "max_business_attempts_per_shot": "MIN", "max_identical_strategy_repeats": "MIN",
    "qc_min_coverage": "MAX", "quorum": "MAX", "separation_of_duties": "OR",
    "locked": "OR",
}

# 这些字段的「更严」方向是「更大」还是「更小」，发布期校验 (b) 要用。
_MORE_RESTRICTIVE_IS_SMALLER = frozenset({
    "mode", "generation_mode", "acceptance_mode", "exception_behavior",
    "allowed_actions", "reviewer_roles", "approved_evaluators",
    "max_business_attempts_per_shot", "max_identical_strategy_repeats",
})


def is_stricter(field: str, candidate, parent) -> bool:
    """`candidate` 相对 `parent` 是不是更严（或相等）。

    发布期校验 (b) 实际用的是 `MERGE[field](parent, candidate) == parent`，那是
    更直接的定义。这个函数留给 explain 与测试断言用：表达"方向"而不是"结果"。
    """
    if candidate == parent:
        return True
    if field in _MORE_RESTRICTIVE_IS_SMALLER:
        if isinstance(candidate, (set, frozenset, list, tuple)):
            return set(candidate) < set(parent)
        return RANK[field][candidate] <= RANK[field][parent]
    if field == "required_gates":
        return set(candidate) > set(parent)
    if field in ("qc_min_coverage", "quorum"):
        return candidate >= parent
    if field in ("locked", "separation_of_duties"):
        return bool(candidate) >= bool(parent)
    return candidate == parent


def normalize_mode_switches(effective: dict, normalized: list[dict]) -> None:
    """§2 的矩阵后置归一。**只许往更保守的方向修，且必须可见。**

    两个方向不对称，这是方案特意分开的：

    - `unattended_batch` + `acceptance_mode=manual`：语义矛盾但方向是**收紧**，
      保留 `manual`，把 `mode` 降级为 `exception_assisted` 并记一条 normalization。
    - `guided` + `generation_mode=auto`：修正会**放宽**（把用户明确要的 auto 改掉
      本身是行为变更），发布期该 422，这里不处理。
    """
    mode = effective.get("mode")
    if mode == "unattended_batch" and effective.get("acceptance_mode") == "manual":
        effective["mode"] = "exception_assisted"
        normalized.append({"code": "NORMALIZED_MODE_DEMOTED",
                           "from": "unattended_batch",
                           "to": "exception_assisted",
                           "field": "mode",
                           "reason": "unattended_batch cannot be acceptance_mode=manual"})
    # unattended ⇒ isolate_branch 是 schema 里那条 allOf 的落地，强制且不记录
    # 成 normalization：它是定义的一部分，不是一次修正。
    if effective.get("mode") == "unattended_batch":
        effective["exception_behavior"] = "isolate_branch"


def apply_defaults(effective: dict, explicit: set[str]) -> dict[str, str]:
    """§2：`mode` 的默认推导只在字段**未显式出现**时生效。

    返回 field → 默认来源说明，供 provenance 标 `source=default`。
    """
    from_default: dict[str, str] = {}
    mode = effective.get("mode")
    if mode is None:
        # 企业层没给 mode。默认值无从推导，交给调用方按
        # `ENTERPRISE_POLICY_MISSING` 拒绝——这里不替它编一个档位。
        return from_default

    def _fill(field, table):
        if field in explicit or effective.get(field) is not None:
            return
        value = table[mode]
        effective[field] = sorted(value) if isinstance(value, list) else value
        from_default[field] = f"default:{mode}"

    _fill("generation_mode", DEFAULT_GENERATION_MODE)
    _fill("acceptance_mode", DEFAULT_ACCEPTANCE_MODE)
    _fill("required_gates", DEFAULT_REQUIRED_GATES)
    _fill("exception_behavior", DEFAULT_EXCEPTION_BEHAVIOR)
    return from_default


def merge_layers(layers: Iterable[dict]) -> tuple[dict, dict]:
    """把已按层展开的 dict 列表合成 (effective, provenance)。

    每个 layer 形状：`{"field": value, ...}` 加上 `"_source"`（来源描述）。
    `None` 是 join 的单位元，所以「未设置」不需要特殊处理。
    """
    layers = list(layers)
    effective: dict[str, Any] = {}
    provenance: dict[str, dict] = {}
    explicit: set[str] = set()

    # §4.4 注 1：先求 mode（它是其它默认值的前提），再求其余。
    order = ["mode"] + [f for f in MERGE if f != "mode"]
    for field in order:
        values = [(l, l[field]) for l in layers if l.get(field) is not None]
        if not values:
            continue
        explicit.add(field)
        winner = None
        winner_source = None
        for layer, value in values:
            merged = MERGE[field](winner, value)
            if winner is None or merged != winner:
                winner, winner_source = merged, layer["_source"]
        if isinstance(winner, set):
            winner = sorted(winner)
        effective[field] = winner
        provenance[field] = {
            "value": winner,
            "source": winner_source,
            "rule": MERGE_OP[field],
            "also_sources": [l["_source"] for l, _ in values],
        }

    from_default = apply_defaults(effective, explicit)
    for field, marker in from_default.items():
        provenance[field] = {"value": effective[field], "source": None,
                             "rule": "DEFAULT", "default": marker,
                             "also_sources": []}

    normalized: list[dict] = []
    normalize_mode_switches(effective, normalized)
    for item in normalized:
        provenance.setdefault(item["field"], {})["normalized"] = item["code"]

    return effective, {"fields": provenance, "normalized": normalized}
