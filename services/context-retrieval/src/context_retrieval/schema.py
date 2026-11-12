"""S18 自己的 schema。表的来源是 `plans/S18-context-retrieval.md` §1.1–§1.6。

三条结构性事实：

1. **这里没有 `claim` / `fact` / `state` / `holder` 表。** S18 拥有的只有 pack、
   引用、禁则、水位。§0 与反模式 1。
2. **pack 带 `content_hash` 且不可变。** 同输入必须同 hash —— 这是"S18 的组装
   可复现"能被测试的唯一理由（§1.1）。
3. **禁则必须能追溯 basis。** `prohibited_assumption.basis` 非空的 CHECK 是
   反模式「模型凭空觉得应该禁止」的结构性堵口（§1.2 / §4.5）。

本仓服务层一律 SQLite（`isolation_level=None` + 显式 `Transaction`），所以方案里的
`jsonb` 存 TEXT、`bytea` 存 hex、`text[]` 存 JSON 数组、`timestamptz` 存定宽 UTC
字符串。`outbox` / `inbox` 由 `event_sdk` 建表，这里不重复 DDL。

**没有 `claim_index` 表。** 方案 §1.3 的索引是 pgvector + tsvector 的检索结构，
本实现里 S16 的 `query` 已经承担了结构化过滤（分支 / 故事时间 / 知情 / 观众揭示），
再建一份本地索引只会变成"第二份可见性判定"，正是 §5 要避免的。语义补充池因此在
本实现里为空，见 `plans/S18-context-retrieval.md` §12 的缺口登记。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §1.1 上下文包。不可变：需要新版本时 revision+1，绝不 UPDATE 已发出的行。
CREATE TABLE IF NOT EXISTS context_pack (
  tenant_id            TEXT NOT NULL,
  context_pack_id      TEXT NOT NULL,
  revision             INTEGER NOT NULL CHECK (revision >= 1),
  command_id           TEXT NOT NULL,
  series_id            TEXT NOT NULL,
  branch_id            TEXT NOT NULL,
  task_role            TEXT NOT NULL,
  target_episode_id    TEXT NOT NULL,
  target_episode_order INTEGER NOT NULL,
  perspective          TEXT NOT NULL CHECK (perspective IN
                         ('omniscient','character','audience')),
  perspective_character_ids TEXT NOT NULL DEFAULT '[]',
  -- 固定水位：pack 有效性的来源。
  checkpoint_sequence  INTEGER NOT NULL CHECK (checkpoint_sequence >= 0),
  checkpoint_hash      TEXT NOT NULL,
  source_watermark     INTEGER NOT NULL CHECK (source_watermark >= 0),
  required_checkpoint  INTEGER NOT NULL CHECK (required_checkpoint >= 0),
  manifest_ref         TEXT,
  policy_ref           TEXT,
  -- 组装结果（全是指针，不是事实正文）
  claim_refs           TEXT NOT NULL,
  asset_refs           TEXT NOT NULL,
  plot_thread_refs     TEXT NOT NULL DEFAULT '[]',
  episode_blocks       TEXT NOT NULL DEFAULT '[]',
  prohibited_assumptions TEXT NOT NULL DEFAULT '[]',
  unresolved_conflicts TEXT NOT NULL DEFAULT '[]',
  -- 预算
  token_budget         INTEGER NOT NULL CHECK (token_budget >= 1),
  tokens_used          INTEGER NOT NULL CHECK (tokens_used >= 0),
  budget_breakdown     TEXT NOT NULL,
  -- 完整性
  forbidden_leak_scan  TEXT NOT NULL,
  content_hash         TEXT NOT NULL CHECK (length(content_hash) = 64),
  -- §2.3：候选包永久不可用于交付，靠这一列在库里堵死。
  candidate            INTEGER NOT NULL DEFAULT 0 CHECK (candidate IN (0,1)),
  pack_for_delivery    INTEGER NOT NULL DEFAULT 0 CHECK (pack_for_delivery IN (0,1)),
  stale                INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0,1)),
  stale_reason         TEXT,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, context_pack_id, revision),
  -- 候选包不得被标记为可交付。这条不靠调用方自觉。
  CONSTRAINT candidate_never_deliverable CHECK (
    candidate = 0 OR pack_for_delivery = 0
  )
);

CREATE INDEX IF NOT EXISTS pack_by_command
  ON context_pack (tenant_id, command_id);

-- 不是 UNIQUE：确定性意味着"同输入 → 同 content_hash"，不意味着"同输入只能存
-- 一份"。两次不同的 command 得到同一份内容，是两个合法的 pack（§10.5 只要求
-- hash 相同）。幂等由 command_id + 幂等表负责，不由这张索引负责。
CREATE INDEX IF NOT EXISTS pack_by_content
  ON context_pack (tenant_id, task_role, target_episode_id,
                   checkpoint_sequence, content_hash, candidate);

-- §1.2 禁则是一等公民：`rule_kind` / `severity` / `basis` / `generated_by`。
CREATE TABLE IF NOT EXISTS prohibited_assumption (
  tenant_id        TEXT NOT NULL,
  context_pack_id  TEXT NOT NULL,
  revision         INTEGER NOT NULL,
  assumption_id    TEXT NOT NULL,
  text             TEXT NOT NULL,
  rule_kind        TEXT NOT NULL CHECK (rule_kind IN
                     ('KNOWLEDGE','KNOWLEDGE_BELIEF','STATE','STATE_ONGOING',
                      'RELATION','THREAD','THREAD_RESOLVED','CANDIDATE',
                      'BRANCH','SCENE')),
  severity         TEXT NOT NULL CHECK (severity IN ('hard','soft')),
  basis            TEXT NOT NULL,
  claim_refs       TEXT NOT NULL DEFAULT '[]',
  entity_ids       TEXT NOT NULL DEFAULT '[]',
  generated_by     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, context_pack_id, revision, assumption_id),
  -- 没有 basis 的禁则不允许写入 —— 否则就是"模型凭空觉得应该禁止"。
  CONSTRAINT basis_required CHECK (length(basis) > 2),
  CONSTRAINT text_not_empty CHECK (length(text) > 0)
);

CREATE INDEX IF NOT EXISTS assumption_by_pack
  ON prohibited_assumption (tenant_id, context_pack_id, revision, severity);

-- §1.6 水位。`applied_commit_sequence` 只增不减（§6.5 终态倒退保护）。
CREATE TABLE IF NOT EXISTS watermark (
  tenant_id              TEXT NOT NULL,
  series_id              TEXT NOT NULL,
  branch_id              TEXT NOT NULL,
  applied_commit_sequence INTEGER NOT NULL CHECK (applied_commit_sequence >= 0),
  applied_checkpoint_hash TEXT,
  updated_at             TEXT NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id)
);

-- §6.4：pack 失效标记。不改已发出的 pack，只记这一行。
CREATE TABLE IF NOT EXISTS pack_staleness (
  tenant_id        TEXT NOT NULL,
  context_pack_id  TEXT NOT NULL,
  revision         INTEGER NOT NULL,
  stale_reason     TEXT NOT NULL,
  marked_at        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, context_pack_id, revision)
);

-- §1.4 角色 profile。weights / section_budget 缺席时用代码里的默认表。
CREATE TABLE IF NOT EXISTS role_profile (
  tenant_id            TEXT NOT NULL,
  task_role            TEXT NOT NULL,
  profile_version      INTEGER NOT NULL CHECK (profile_version >= 1),
  required_kinds       TEXT NOT NULL,
  section_budget       TEXT NOT NULL,
  weights              TEXT NOT NULL,
  include_world_rules  INTEGER NOT NULL DEFAULT 1 CHECK (include_world_rules IN (0,1)),
  PRIMARY KEY (tenant_id, task_role, profile_version)
);
"""

# §3 的过滤顺序：严格有序，重排就是错。这里写成常量是为了让代码与测试都引用同一份。
FILTER_ORDER = (
    "authorize",
    "branch_and_accepted",
    "story_validity",
    "perspective",
    "relevance",
    "conflicts",
    "summary",
)

PERSPECTIVES = ("omniscient", "character", "audience")
TASK_ROLES = ("writer", "storyboard", "prompt", "qc", "continuity", "preview",
              "review")
# §2 第 3 条：只有这两个 role 允许 include_candidates。
CANDIDATE_ROLES = ("preview", "review")

# §4.4 默认 section 预算。role_profile 里给了就以库里为准。
DEFAULT_SECTION_BUDGET = {
    "identity": 0.15,
    "relationships": 0.25,
    "continuity": 0.25,
    "threads": 0.20,
    "summary": 0.15,
}

# §4.4 第 1 条：硬事实无条件占位。它们进哪个 section 由 kind 决定。
SECTION_OF_KIND = {
    "character_profile": "identity",
    "entity_state": "continuity",
    "knowledge_state": "continuity",
    "relationship": "relationships",
    "plot_thread": "threads",
    "world_rule": "identity",
    "story_event": "summary",
    "episode_summary": "summary",
}
SECTION_ORDER = ("identity", "relationships", "continuity", "threads", "summary")

# §4.1 H 池：硬事实。缺一个就会出错的那几类。
HARD_KINDS = ("knowledge_state", "entity_state", "relationship", "plot_thread",
              "world_rule", "character_profile")

# ---------------------------------------------------------------- 缺口登记
# 这些不是实现自由度，是契约/上游能力的缺口。跑起来的每个 pack 都会带上它们，
# 免得"能返回"被误读成"契约完整"。
CONTRACT_GAPS = [
    "S16 POST /memory-queries does not return per-claim content_hash "
    "(`_render_claim` emits 19 keys and content_hash is not one of them), but "
    "context-pack.v2 requires claim_refs[].content_hash to be 64-hex and "
    "additionalProperties:false. claim_refs[] therefore carry a deterministic "
    "`sha256('unavailable:<claim_id>:<revision>')` placeholder when the "
    "upstream omits the field. The value is shape-legal but is NOT a content "
    "hash: a consumer that dedupes claims by it is deduping by claim id.",
    "There is no S16 read path for entity_registry display_name/alias, so the "
    "forbidden_leak_scan can only match entity ids and claim text; alias-based "
    "leak detection is not implemented (plan §4.8 step 2).",
    "context-pack.v2 has no `candidate`, `stale`, `source_watermark` reason or "
    "`pack_for_delivery` field; candidate/stale packs are blockable only inside "
    "this service and must be re-checked by the S43 delivery gate.",
    "Plan §4.6 requires EpisodeSummary from S16, but S16 has no episode-summary "
    "read or compute surface; episode_blocks are therefore always emitted as "
    "`{block_id, missing: true, requires: 'plan:compute'}`.",
    "Plan §1.3 requires a pgvector claim_index for semantic recall; this "
    "implementation has none, so the S supplement pool is empty and every pack "
    "reports semantic_coverage='disabled'.",
    "Neither `contracts/openapi.v2.yaml` nor the S18 design package defines the "
    "`task_role → required_kinds` defaults used by §7; the table here follows "
    "plan §7 verbatim.",
    "S18 C18-1: openapi.v2:404 declares POST /context-packs with body "
    "{context: execution-context.v2, task_role, token_budget, command_id} and "
    "additionalProperties:false. execution-context.v2 has 18 required fields "
    "and also forbids extra properties, so it cannot carry plan §2's "
    "episode_order, perspective, story_clock_id, story_tick, focus or "
    "include_candidates. This service takes plan §2's flat body and answers "
    "the contract envelope with CONTRACT_BODY_UNSUPPORTED (400) rather than "
    "inventing a perspective. Needs an S18-specific request schema in "
    "contracts/openapi.v2.yaml; same class as S09 C09-4.",
    "S18 C18-2: openapi.v2 declares only POST /context-packs for S18. "
    "GET /context-packs/{id}, which the design package lists as the query "
    "entry (returning source_revision/projection_watermark), is absent. This "
    "service serves it and stamps source_watermark in the response, but the "
    "path is contract-less.",
    "S18 C18-3: openapi.v2:427 binds `token_budget: *id003`, where id003 is "
    "the anchor declared on /manifests/{id}/commits as "
    "`expected_manifest_revision: {type: integer, minimum: 1}`. The type "
    "happens to fit a token budget but the anchor is semantically the "
    "manifest revision; retyping id003 would silently change an unrelated "
    "endpoint. Needs its own anchor.",
]
