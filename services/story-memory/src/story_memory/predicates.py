"""谓词注册表。随服务一起发版，抽取器造不出新谓词（plan §3.3 R2）。

每条登记三件事：

- `value_kind`：`entity_id`（值必须是可解析的实体）/ `claim_ref`（值必须是本系列
  已存在的 claim id，例如 `causes` 指向另一条 story_event）/ `enum`（取值集合封闭）/
  `text` / `number` / `bool` / `any`
- `cardinality`：1 表示同一 `(subject, predicate)` 同一时刻只能有一个值。
  只有这类谓词会触发 C3 唯一性检查。
- `nullable`：值可以为 `null`（例如"钥匙被藏起来、暂时没有持有者"）。

`KIND_MIN_CONFIDENCE` 是 R6 的下限。`knowledge_state` 与 `plot_thread` 定得高，
因为这两类一旦抽错，会让下游的 `prohibited_assumptions` 或伏笔回收计划整条跑偏。
"""

from __future__ import annotations

PREDICATES: dict[str, dict] = {
    # --- entity_state ---------------------------------------------------
    "held_by": {"value_kind": "entity_id", "cardinality": 1, "nullable": True},
    "stored_at": {"value_kind": "entity_id", "cardinality": 1, "nullable": True},
    "located_at": {"value_kind": "entity_id", "cardinality": 1, "nullable": True},
    "life_state": {"value_kind": "enum", "cardinality": 1, "nullable": False,
                   "values": ("alive", "dead", "missing", "destroyed", "absent")},
    "existence": {"value_kind": "enum", "cardinality": 1, "nullable": False,
                  "values": ("exists", "destroyed", "absent")},
    "appearance": {"value_kind": "text", "cardinality": 1, "nullable": False},
    "wears": {"value_kind": "entity_id", "cardinality": 0, "nullable": True},
    "injury": {"value_kind": "text", "cardinality": 0, "nullable": False},

    # --- character_profile ----------------------------------------------
    "age": {"value_kind": "number", "cardinality": 1, "nullable": False},
    "occupation": {"value_kind": "text", "cardinality": 1, "nullable": False},
    "allegiance": {"value_kind": "entity_id", "cardinality": 0, "nullable": True},
    "speech_register": {"value_kind": "text", "cardinality": 1, "nullable": False},

    # --- relationship ---------------------------------------------------
    "relation": {"value_kind": "text", "cardinality": 0, "nullable": False},
    "trusts": {"value_kind": "entity_id", "cardinality": 0, "nullable": False},

    # --- world_rule -----------------------------------------------------
    "rule_statement": {"value_kind": "text", "cardinality": 1, "nullable": False,
                       "publicly_known_by_default": True},
    "cost_of_magic": {"value_kind": "text", "cardinality": 1, "nullable": False,
                      "publicly_known_by_default": True},

    # --- story_event ----------------------------------------------------
    # 指向另一条 story_event 的 claim（§1.6 `event_id(=claim_id)`），不是实体 id。
    "causes": {"value_kind": "claim_ref", "cardinality": 0, "nullable": False},
    "resurrection": {"value_kind": "claim_ref", "cardinality": 0, "nullable": False},
    "exclusive_group": {"value_kind": "text", "cardinality": 0, "nullable": False},
    "participants": {"value_kind": "any", "cardinality": 0, "nullable": False},

    # --- knowledge_state ------------------------------------------------
    "belief_about": {"value_kind": "entity_id", "cardinality": 0, "nullable": False},

    # --- plot_thread ----------------------------------------------------
    "thread_state": {"value_kind": "enum", "cardinality": 1, "nullable": False,
                     "values": ("open", "hinted", "resolved", "abandoned")},
    "resolved_by_event": {"value_kind": "claim_ref", "cardinality": 1,
                          "nullable": False},

    # --- creative_preference --------------------------------------------
    "tone": {"value_kind": "text", "cardinality": 1, "nullable": False},
    "pacing": {"value_kind": "text", "cardinality": 1, "nullable": False},
}

CARDINALITY_ONE = frozenset(
    name for name, spec in PREDICATES.items() if spec.get("cardinality") == 1)

KIND_MIN_CONFIDENCE: dict[str, float] = {
    "world_rule": 0.70,
    "character_profile": 0.60,
    "story_event": 0.65,
    "entity_state": 0.55,
    "relationship": 0.55,
    "knowledge_state": 0.70,
    "plot_thread": 0.70,
    "creative_preference": 0.50,
}
