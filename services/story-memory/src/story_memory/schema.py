"""S16 自己的 schema。别的服务不 import 这里，只透过 HTTP 契约交互。

对应 `plans/S16-story-memory.md` §1.1–§1.11 的 DDL，按本仓服务层惯例把 Postgres
写法换成 SQLite：`timestamptz` → 定宽 ISO-8601 UTC `TEXT`、`jsonb` → `TEXT`、
`bytea` → 小写 hex `TEXT`、`numeric(4,3)` → `REAL`、`text[]` → 逗号分隔 `TEXT`。

两条结构性约束在这里就落地，而不是留给业务层：

1. `claim_revision` 有 `CHECK (status <> 'accepted' OR (accepted_manifest_ref IS
   NOT NULL AND commit_sequence >= 1))` —— 没有证据的接受行插不进去。
2. `claim_candidate` **没有** `commit_sequence`、**没有** `accepted_manifest_ref`、
   没有 `status='accepted'` 这一态 —— 候选在物理上无法伪造一次提交（§1.7）。

`outbox`/`inbox` 由 `event_sdk` 建表，这里不重复 DDL。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §1.1 故事时钟
CREATE TABLE IF NOT EXISTS story_clock (
  tenant_id         TEXT NOT NULL,
  series_id         TEXT NOT NULL,
  clock_id          TEXT NOT NULL,
  clock_kind        TEXT NOT NULL CHECK (clock_kind IN
                      ('main','flashback','dream','side')),
  parent_clock_id   TEXT,
  origin_episode_id TEXT,
  tick_min          INTEGER NOT NULL,
  tick_max          INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, series_id, clock_id),
  CHECK (tick_max >= tick_min)
);

-- §1.1 集窗口。闪回集不改变 episode_order，只改变 clock_id + tick。
CREATE TABLE IF NOT EXISTS episode_story_window (
  tenant_id     TEXT NOT NULL,
  series_id     TEXT NOT NULL,
  episode_id    TEXT NOT NULL,
  episode_order INTEGER NOT NULL CHECK (episode_order >= 1),
  clock_id      TEXT NOT NULL,
  tick_start    INTEGER NOT NULL,
  tick_end      INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, series_id, episode_id),
  UNIQUE (tenant_id, series_id, episode_order),
  CHECK (tick_end > tick_start)
);

-- §1.2 分支。分叉不复制任何 claim 行，只插入这一行。
CREATE TABLE IF NOT EXISTS canon_branch (
  tenant_id             TEXT NOT NULL,
  series_id             TEXT NOT NULL,
  branch_id             TEXT NOT NULL,
  parent_branch_id      TEXT,
  fork_commit_sequence  INTEGER,
  fork_checkpoint_hash  TEXT,
  branch_kind           TEXT NOT NULL CHECK (branch_kind IN
                          ('canon','experiment','merge_preview')),
  status                TEXT NOT NULL CHECK (status IN
                          ('active','merged','abandoned','frozen')),
  merge_status          TEXT NOT NULL DEFAULT 'none' CHECK (merge_status IN
                          ('none','pending','conflicted','merged')),
  head_commit_sequence  INTEGER NOT NULL DEFAULT 0,
  head_checkpoint_hash  TEXT NOT NULL,
  created_at            TEXT NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id)
);

-- §1.2 派生、可重建：分支谱系展开（depth 0 = 自己）。
CREATE TABLE IF NOT EXISTS branch_ancestry (
  tenant_id          TEXT NOT NULL,
  series_id          TEXT NOT NULL,
  branch_id          TEXT NOT NULL,
  ancestor_branch_id TEXT NOT NULL,
  depth              INTEGER NOT NULL CHECK (depth >= 0),
  PRIMARY KEY (tenant_id, series_id, branch_id, ancestor_branch_id)
);

-- §1.3 提交批次与 checkpoint 链。
CREATE TABLE IF NOT EXISTS memory_commit (
  tenant_id                TEXT NOT NULL,
  series_id                TEXT NOT NULL,
  branch_id                TEXT NOT NULL,
  commit_sequence          INTEGER NOT NULL CHECK (commit_sequence >= 1),
  previous_sequence        INTEGER NOT NULL,
  previous_checkpoint_hash TEXT NOT NULL,
  checkpoint_hash          TEXT NOT NULL,
  commit_kind              TEXT NOT NULL CHECK (commit_kind IN
                             ('extract','supersede','revert','merge',
                              'branch_init','repair')),
  source_manifest_ref      TEXT,
  merge_source_refs        TEXT,
  revert_of_sequence       INTEGER,
  extraction_batch_id      TEXT,
  command_id               TEXT NOT NULL,
  actor_principal          TEXT NOT NULL,
  recorded_at              TEXT NOT NULL,
  claim_count              INTEGER NOT NULL CHECK (claim_count >= 0),
  PRIMARY KEY (tenant_id, series_id, branch_id, commit_sequence),
  CHECK (previous_sequence = commit_sequence - 1)
);

-- 同一已接受 manifest 只能产生一次 extract 提交。SQLite 的表达式索引不支持
-- 直接取 jsonb 字段，所以把两个键显式存成列，唯一约束落在这两列上。
CREATE TABLE IF NOT EXISTS memory_commit_manifest (
  tenant_id        TEXT NOT NULL,
  series_id        TEXT NOT NULL,
  branch_id        TEXT NOT NULL,
  commit_sequence  INTEGER NOT NULL,
  manifest_id      TEXT NOT NULL,
  manifest_revision INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id, commit_sequence),
  UNIQUE (tenant_id, series_id, branch_id, manifest_id, manifest_revision)
);

-- §1.4 集 → checkpoint 绑定（E08 的关键：已交付集永远指向它当时的 checkpoint）。
CREATE TABLE IF NOT EXISTS commit_manifest_binding (
  tenant_id       TEXT NOT NULL,
  series_id       TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  commit_sequence INTEGER NOT NULL,
  manifest_ref    TEXT NOT NULL,
  episode_id      TEXT NOT NULL,
  episode_order   INTEGER NOT NULL,
  clock_id        TEXT NOT NULL,
  tick_start      INTEGER NOT NULL,
  tick_end        INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id, commit_sequence, episode_id)
);

-- §1.5 Claim：稳定 ID，跨 revision 不变。
CREATE TABLE IF NOT EXISTS claim (
  tenant_id         TEXT NOT NULL,
  series_id         TEXT NOT NULL,
  claim_id          TEXT NOT NULL,
  kind              TEXT NOT NULL CHECK (kind IN
                      ('world_rule','character_profile','story_event',
                       'entity_state','relationship','knowledge_state',
                       'plot_thread','creative_preference')),
  subject_id        TEXT NOT NULL,
  predicate         TEXT NOT NULL,
  origin_branch_id  TEXT NOT NULL,
  created_sequence  INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, series_id, claim_id),
  -- 同一实体同一谓词只有一条 claim 谱系：堵死"两把钥匙两张表"。
  UNIQUE (tenant_id, series_id, kind, subject_id, predicate)
);

-- §1.5 Claim revision：不可变。没有 candidate 状态 —— 候选物理隔离在另一张表。
CREATE TABLE IF NOT EXISTS claim_revision (
  tenant_id             TEXT NOT NULL,
  series_id             TEXT NOT NULL,
  claim_id              TEXT NOT NULL,
  revision              INTEGER NOT NULL CHECK (revision >= 1),
  status                TEXT NOT NULL CHECK (status IN
                          ('accepted','superseded','rejected')),
  fact                  TEXT NOT NULL,
  clock_id              TEXT NOT NULL,
  valid_from_tick       INTEGER NOT NULL,
  valid_to_tick         INTEGER,
  recorded_at           TEXT NOT NULL,
  commit_sequence       INTEGER NOT NULL,
  branch_id             TEXT NOT NULL,
  source_refs           TEXT NOT NULL,
  accepted_manifest_ref TEXT,
  epistemic_scope       TEXT NOT NULL,
  supersedes            TEXT NOT NULL,
  confidence            REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  review_state          TEXT NOT NULL CHECK (review_state IN
                          ('auto','human_accepted','human_rejected')),
  content_hash          TEXT NOT NULL CHECK (length(content_hash) = 64),
  PRIMARY KEY (tenant_id, series_id, claim_id, revision),
  CONSTRAINT accepted_needs_evidence CHECK (
    status <> 'accepted'
    OR (accepted_manifest_ref IS NOT NULL AND commit_sequence >= 1)
  ),
  CONSTRAINT interval_sane CHECK (
    valid_to_tick IS NULL OR valid_to_tick > valid_from_tick
  )
);

CREATE INDEX IF NOT EXISTS claim_revision_branch_idx
  ON claim_revision (tenant_id, series_id, branch_id, commit_sequence);

-- §1.6 类型化投影（可重建，不是权威）。只存可索引窄列。
CREATE TABLE IF NOT EXISTS knowledge_state (
  tenant_id        TEXT NOT NULL,
  series_id        TEXT NOT NULL,
  claim_id         TEXT NOT NULL,
  revision         INTEGER NOT NULL,
  knower_id        TEXT NOT NULL,
  proposition_claim_id TEXT NOT NULL,
  belief_type      TEXT NOT NULL CHECK (belief_type IN
                     ('fact','knows','believes','unaware','uncertain')),
  source_event_id  TEXT NOT NULL,
  clock_id         TEXT NOT NULL,
  valid_from_tick  INTEGER NOT NULL,
  valid_to_tick    INTEGER,
  audience_reveal_episode_order INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, series_id, claim_id, revision)
);

CREATE INDEX IF NOT EXISTS knowledge_lookup
  ON knowledge_state (tenant_id, series_id, knower_id,
                      proposition_claim_id, clock_id, valid_from_tick);

-- §1.7 候选隔离区。没有 commit_sequence / accepted_manifest_ref。
CREATE TABLE IF NOT EXISTS claim_candidate (
  tenant_id        TEXT NOT NULL,
  series_id        TEXT NOT NULL,
  branch_id        TEXT NOT NULL,
  batch_id         TEXT NOT NULL,
  candidate_id     TEXT NOT NULL,
  proposed_claim_id TEXT,
  kind             TEXT NOT NULL,
  fact             TEXT NOT NULL,
  clock_id         TEXT NOT NULL,
  valid_from_tick  INTEGER NOT NULL,
  valid_to_tick    INTEGER,
  epistemic_scope  TEXT,
  source_refs      TEXT NOT NULL,
  -- C1 要求替换旧值必须显式带 supersedes；这一列是它在候选期的落脚点。
  supersedes_claim_refs TEXT NOT NULL DEFAULT '[]',
  -- knowledge_state 必须指明"对哪个命题"，否则知识投影无从落地（§1.6 R8）。
  proposition_claim_id TEXT,
  confidence       REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extracted_by     TEXT NOT NULL,
  extraction_manifest_ref TEXT NOT NULL,
  validation_state TEXT NOT NULL CHECK (validation_state IN
                     ('pending','structurally_valid','conflict','rejected',
                      'promoted')),
  validation_errors TEXT NOT NULL DEFAULT '[]',
  created_at       TEXT NOT NULL,
  expires_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id, batch_id, candidate_id)
);

-- §1.8 实体命名空间（防抽取幻觉）。
CREATE TABLE IF NOT EXISTS entity_registry (
  tenant_id     TEXT NOT NULL,
  series_id     TEXT NOT NULL,
  entity_id     TEXT NOT NULL,
  entity_type   TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  owner_service TEXT,
  status        TEXT NOT NULL CHECK (status IN
                  ('active','deprecated','merged_into')),
  PRIMARY KEY (tenant_id, series_id, entity_id)
);

CREATE TABLE IF NOT EXISTS entity_alias (
  tenant_id   TEXT NOT NULL,
  series_id   TEXT NOT NULL,
  alias_text  TEXT NOT NULL,
  entity_id   TEXT NOT NULL,
  confidence  REAL NOT NULL,
  source      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, series_id, alias_text, entity_id)
);

-- §1.11 生效视图（overlay 解析结果，可重建，不是权威）。
CREATE TABLE IF NOT EXISTS canon_effective_claim (
  tenant_id       TEXT NOT NULL,
  series_id       TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  claim_id        TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  kind            TEXT NOT NULL,
  clock_id        TEXT NOT NULL,
  valid_from_tick INTEGER NOT NULL,
  valid_to_tick   INTEGER,
  commit_sequence INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id, claim_id)
);

-- §5.2 记忆侧失效边界要用的 touched_entities（只存集合，不存内容）。
CREATE TABLE IF NOT EXISTS episode_entity_touch (
  tenant_id       TEXT NOT NULL,
  series_id       TEXT NOT NULL,
  episode_id      TEXT NOT NULL,
  episode_order   INTEGER NOT NULL,
  entity_id       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, series_id, episode_id, entity_id)
);

-- §3.6 提交被拒的审计记录。不推进 checkpoint 也要留下证据。
CREATE TABLE IF NOT EXISTS commit_blocked (
  tenant_id       TEXT NOT NULL,
  series_id       TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  command_id      TEXT NOT NULL,
  reason          TEXT NOT NULL,
  details         TEXT NOT NULL,
  recorded_at     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id, command_id)
);
"""
