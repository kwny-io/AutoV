"""S17 自己的 schema。表的来源是 `plans/S17-continuity.md` §1.1–§1.7。

三条结构性事实，它们决定了这张库里能证明什么：

1. **这里没有 `claim` / `held_by` / `current_state` 之类的表。** S17 存的是
   「某次检查在某个 checkpoint 上推导出的期望」，不是事实。§0 的硬规则。
2. **`expected_state` 强制带 `source_claim_ref` + `report_id` + `checkpoint`。**
   没有来源的期望写不进来 —— 凭空想一个期望再报缺陷在结构上不可能（§1.4）。
3. **`defect` 不删行。** 被判定为合法例外的缺陷保留 `disposition='suppress'` 与
   `suppression_reason`，「压制」和「没检出」在审计上完全不同（§3 末段）。

本仓服务层一律 SQLite（`isolation_level=None` + 显式 `Transaction`），所以方案里的
`jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`text[]` 存 JSON 数组。
`outbox`/`inbox` 由 `event_sdk` 建表，这里不重复 DDL。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §1.1 检查任务。`check_id` 与 `command_id` 一起构成幂等索引。
CREATE TABLE IF NOT EXISTS check_run (
  tenant_id            TEXT NOT NULL,
  check_id             TEXT NOT NULL,
  command_id           TEXT NOT NULL,
  series_id            TEXT NOT NULL,
  branch_id            TEXT NOT NULL,
  episode_id           TEXT NOT NULL,
  episode_order        INTEGER NOT NULL,
  manifest_ref         TEXT NOT NULL,
  memory_checkpoint    TEXT NOT NULL,
  context_pack_ref     TEXT,
  check_profile        TEXT NOT NULL CHECK (check_profile IN
                         ('text_logic','visual','audio','full')),
  scope                TEXT NOT NULL,
  state                TEXT NOT NULL CHECK (state IN
                         ('queued','running','completed','abstained','failed')),
  started_at           TEXT,
  completed_at         TEXT,
  agent_task_ref       TEXT,
  report_id            TEXT,
  PRIMARY KEY (tenant_id, check_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS check_run_idem
  ON check_run (tenant_id, command_id);

-- §1.2 报告。`coverage_ratio` + `covered_scope` 是必需的：没有它们，
-- 「没发现问题」和「没检查」分不开。
CREATE TABLE IF NOT EXISTS continuity_report (
  tenant_id            TEXT NOT NULL,
  report_id            TEXT NOT NULL,
  check_id             TEXT NOT NULL,
  series_id            TEXT NOT NULL,
  branch_id            TEXT NOT NULL,
  episode_id           TEXT NOT NULL,
  manifest_ref         TEXT NOT NULL,
  memory_checkpoint    TEXT NOT NULL,
  verdict              TEXT NOT NULL CHECK (verdict IN
                         ('pass','pass_with_warnings','defects','abstained')),
  severity_max         TEXT NOT NULL CHECK (severity_max IN
                         ('none','info','low','medium','high','blocking')),
  defect_count         INTEGER NOT NULL CHECK (defect_count >= 0),
  abstain_count        INTEGER NOT NULL CHECK (abstain_count >= 0),
  covered_scope        TEXT NOT NULL,
  coverage_ratio       REAL NOT NULL CHECK (coverage_ratio >= 0
                         AND coverage_ratio <= 1),
  source_revision      INTEGER NOT NULL,
  content_hash         TEXT NOT NULL CHECK (length(content_hash) = 64),
  created_at           TEXT NOT NULL,
  supersedes_report_id TEXT,
  abstain_reason       TEXT,
  PRIMARY KEY (tenant_id, report_id)
);

CREATE INDEX IF NOT EXISTS report_by_check
  ON continuity_report (tenant_id, check_id, created_at);

-- §1.3 缺陷。`expected`/`observed`/`evidence_refs` 三列是缺陷成立的全部依据。
CREATE TABLE IF NOT EXISTS defect (
  tenant_id           TEXT NOT NULL,
  report_id           TEXT NOT NULL,
  defect_id           TEXT NOT NULL,
  category            TEXT NOT NULL,
  subcategory         TEXT NOT NULL,
  -- §2.3 的规则表里，suppress 与 abstain 两行的 severity 是「—」。
  -- 逼它们填一个值就是把"没判定"伪装成"判定为 low"。
  severity            TEXT CHECK (severity IS NULL OR severity IN
                        ('info','low','medium','high','blocking')),
  confidence          REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  disposition         TEXT NOT NULL CHECK (disposition IN
                        ('raise','suppress','abstain','needs_human')),
  suppression_reason  TEXT,
  expected            TEXT NOT NULL,
  observed            TEXT NOT NULL,
  evidence_refs       TEXT NOT NULL,
  entity_ids          TEXT NOT NULL DEFAULT '[]',
  claim_refs          TEXT NOT NULL DEFAULT '[]',
  shot_id             TEXT,
  scene_id            TEXT,
  timecode_start_ms   INTEGER,
  timecode_end_ms     INTEGER,
  fault_hypothesis    TEXT,
  fault_owner_service TEXT,
  suggested_action    TEXT,
  PRIMARY KEY (tenant_id, report_id, defect_id),
  -- 压制必须有理由，否则「合法例外」四个字什么也没证明。
  CONSTRAINT suppress_needs_reason CHECK (
    disposition <> 'suppress' OR suppression_reason IS NOT NULL
  ),
  -- 反过来说：只有 suppress 能带理由。理由挂在 raise 上是自相矛盾。
  CONSTRAINT reason_only_when_suppressed CHECK (
    suppression_reason IS NULL OR disposition = 'suppress'
  )
);

CREATE INDEX IF NOT EXISTS defect_by_shot
  ON defect (tenant_id, shot_id);

-- §1.4 期望。`source_claim_ref` 非空是硬约束 —— 每一行期望都能回指到 S16。
CREATE TABLE IF NOT EXISTS expected_state (
  tenant_id        TEXT NOT NULL,
  report_id        TEXT NOT NULL,
  unit_id          TEXT NOT NULL,
  entity_id        TEXT NOT NULL,
  predicate        TEXT NOT NULL,
  expected_value   TEXT NOT NULL,
  source_claim_ref TEXT NOT NULL,
  checkpoint       INTEGER NOT NULL,
  derivation       TEXT NOT NULL CHECK (derivation IN
                     ('direct','inherited','inferred_from_event')),
  PRIMARY KEY (tenant_id, report_id, unit_id, entity_id, predicate)
);

-- §1.5 合法例外。理由、批准人、作用域三样缺一不可。
CREATE TABLE IF NOT EXISTS exemption (
  tenant_id             TEXT NOT NULL,
  series_id             TEXT NOT NULL,
  exemption_id          TEXT NOT NULL,
  category              TEXT NOT NULL,
  match_scope           TEXT NOT NULL,
  justification         TEXT NOT NULL,
  basis                 TEXT NOT NULL CHECK (basis IN
                          ('source_material','director_decision',
                           'genre_convention','world_rule')),
  world_rule_ref        TEXT,
  approved_by_principal TEXT NOT NULL,
  approved_at           TEXT NOT NULL,
  expires_at            TEXT,
  PRIMARY KEY (tenant_id, exemption_id),
  CONSTRAINT exemption_needs_justification CHECK (length(justification) > 0),
  CONSTRAINT exemption_needs_approver CHECK (length(approved_by_principal) > 0),
  CONSTRAINT world_rule_basis_needs_ref CHECK (
    basis <> 'world_rule' OR world_rule_ref IS NOT NULL
  )
);

CREATE TABLE IF NOT EXISTS exemption_application (
  tenant_id    TEXT NOT NULL,
  report_id    TEXT NOT NULL,
  defect_id    TEXT NOT NULL,
  exemption_id TEXT NOT NULL,
  applied_at   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, report_id, defect_id)
);

-- §1.6 基线。只有基线报告的缺陷进 S39 的修复队列。
CREATE TABLE IF NOT EXISTS episode_baseline (
  tenant_id         TEXT NOT NULL,
  series_id         TEXT NOT NULL,
  branch_id         TEXT NOT NULL,
  episode_id        TEXT NOT NULL,
  report_id         TEXT NOT NULL,
  memory_checkpoint TEXT NOT NULL,
  established_at    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id, episode_id)
);
"""

# §3 判定顺序里「叙事框架」那一类：只有这些 diegetic_frame 才算合法的非现实框架。
FRAME_VALUES = ("flashback", "dream", "hallucination", "imagination", "vision")

# §2.3 固定规则表。**不是模型自由判断**：`(category, min_confidence)` → disposition / severity。
# 表里没有的 category 落到 `DEFAULT_RULE`。
RAISE_RULES = {
    "KNOWLEDGE_VIOLATION": (0.80, "blocking"),
    # §3.1：正史说知道、台词演不知道、剧本没标 deception —— 标注缺失本身就是
    # 缺陷，固定 medium（既不是"可能是编剧失误"可以压的 low，也不是阻断的 blocking）。
    "UNMARKED_KNOWLEDGE_CONTRADICTION": (0.70, "medium"),
    "HOLDER_MISMATCH": (0.75, "high"),
    "WORLD_RULE_BREACH": (0.80, "blocking"),
    "INJURY_REGRESSION": (0.70, "high"),
    "PROP_VISUAL_MISMATCH": (0.75, "high"),
    "THREAD_DOUBLE_PAYOFF": (0.70, "blocking"),
    "THREAD_ABANDONED": (0.70, "medium"),
    "RELATION_MISMATCH": (0.70, "low"),
    "CAUSAL_ORPHAN": (0.70, "low"),
    "TEMPORAL_INCONSISTENT": (0.70, "low"),
    "FLASHBACK_MISFRAME": (0.70, "medium"),
    "VISUAL_STATE_MISMATCH": (0.70, "high"),
    "CONTINUITY_OF_MOTION": (0.70, "low"),
    "VOICE_MISMATCH": (0.75, "high"),
    "COSTUME_SWITCH": (0.70, "medium"),
    "LIGHT_TIME_JUMP": (0.70, "low"),
}
# 结构性不可判定：缺资产 / 缺关键帧。不是「没检查」，是「查了但证不够」。
NEEDS_HUMAN_CATEGORIES = ("SCREEN_DIRECTION_BREACH", "MISSING_EVIDENCE")
DEFAULT_RULE = (0.70, "low")
# §2.3 末段：低于这个数一律 abstain，不降级成 low。
ABSTAIN_THRESHOLD = 0.55
# §7.1 覆盖率下限：低于它 verdict 最高只能 `pass_with_warnings`。
COVERAGE_FLOOR = 0.85
