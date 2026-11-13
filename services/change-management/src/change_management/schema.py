"""S07 自己的 schema。表来源是 `plans/S07-change-management.md` §2。

四条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **这里没有 accepted pointer，也没有 manifest / node_epoch 镜像。** 方案 §2.7
   把这几张表列为"不建"：S08 是接受指针与 epoch 的唯一写者，S07 建一份就是
   AGENTS.md 明令禁止的"第二个权威"。`change_plan.execution_fence` 是**生成计划
   那一刻读到的值**，只用于 apply 时比对"期间有没有被 fence"，它不是游标，
   也没有任何代码会推进它。
2. **`change_plan.plan_body` 是契约实例的唯一副本。** §2.1 的列足够做索引与
   CHECK，但不足以重建 `enterprise-change-plan.v2` 的 16 个必填字段（缺
   `prepared_revision_refs` / `derived_adjustments` / `blocking_issues` /
   `required_policy_decisions`）。把契约实例整份存一列，`get_plan` 直接读它，
   计划不可变就是**存储层的事实**，不是"约定不要改"。
3. **澄清的选项数下限在库里，不在代码里。** `cr_options_nonempty` 要求
   `json_array_length(options) >= 2`。一个只有一种选项的"澄清"是命令，不是问题。
4. **Saga 的唯一性靠部分索引。** `cs_plan_active_uniq` 让"一个 plan 只有一个
   生效 Saga"成为数据库约束，两个 apply 打同一个 plan 时第二个撞索引，而不是
   靠应用层先查后写。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串
（`"%Y-%m-%dT%H:%M:%S.%f"[:-3] + "Z"`，字典序即时间序）、`boolean` 存 INTEGER
0/1、`bigint` 存 INTEGER。`outbox` / `inbox` / `idempotency_record` 由平台 SDK
建表，这里不重复 DDL —— 方案 §2.6 给的这三张表结构与 SDK 不一致，以 SDK 为准
（见 `CONTRACT_GAPS` 的 S07-16）。

两处 Postgres 到 SQLite 的折算要在读代码时留意：

- `plan_hash ~ '^[0-9a-f]{64}$'` → `length=64 AND NOT GLOB '*[^0-9a-f]*'`；
- `octet_length(x::text) <= 4096` → `length(x) <= 4096`。SQLite 的 `length()`
  对 TEXT 数的是**字符**，不是字节，所以中文比 Postgres 宽松；这里的用途是
  "挡住整段对白"，字符口径只会更严一点点（一个汉字 3 字节），不会放宽到
  装得下一段台词。

占位值：`DRAFT` / `PLANNING` 行的 `plan_hash` / `currency` / `expires_at` 是
占位（`'0'*64` / `'XXX'` / 创建时 +TTL）。这些行**没有** `plan_body`，而
`cp_body_when_plannable` 保证 `PLANNED` 之后必须有——占位 hash 不可能被当成
一份真计划读出来。
"""

ID_PREFIX = {
    "plan": "change_",
    "request": "clar_",
    "saga": "saga_",
    "local_event": "levt_",
}

PLAN_STATES = (
    "DRAFT", "PLANNING", "PLANNED", "CLARIFICATION_REQUIRED", "POLICY_DENIED",
    "INFEASIBLE", "EXPIRED", "APPLYING", "APPLIED", "FAILED", "SUPERSEDED",
)

SAGA_STATES = ("RUNNING", "SUCCEEDED", "FAILED", "ABANDONED")

STEP_STATES = ("PENDING", "RUNNING", "DONE", "FAILED", "SKIPPED")

CLARIFICATION_KINDS = (
    "MISSING_EPISODE_CONTEXT", "DIALOGUE_EXCEEDS_DURATION", "AMBIGUOUS_SHOT",
    "UNSUPPORTED_INSTRUCTION", "MISSING_ASSET", "RIGHTS_BLOCKED",
)

TERMINAL_PLAN_STATES = frozenset({
    "POLICY_DENIED", "INFEASIBLE", "EXPIRED", "APPLIED", "FAILED", "SUPERSEDED",
})

# 状态机的边，逐条来自 §3.1 的转移表。"任意非终态 → EXPIRED" 在下面统一加。
PLAN_TRANSITIONS = {
    "DRAFT": {"PLANNING", "EXPIRED"},
    "PLANNING": {"PLANNED", "CLARIFICATION_REQUIRED", "POLICY_DENIED",
                 "INFEASIBLE", "FAILED", "EXPIRED"},
    "PLANNED": {"APPLYING", "SUPERSEDED", "EXPIRED"},
    # 澄清答复是唯一能回到 PLANNING 的边；EXPIRED/SUPERSEDED 是"用户没决定"。
    "CLARIFICATION_REQUIRED": {"PLANNING", "SUPERSEDED", "EXPIRED"},
    # §3.1 只写了 APPLYING → APPLIED / FAILED，但 §4.2 要求可重试的失败
    # （abstain、依赖不可达）"重试"——而重试 = 再 apply 一次 = 计划必须回到
    # PLANNED。缺这条边会把 409 POLICY_ABSTAIN 变成 500 STATE_MACHINE_VIOLATION。
    # 该边只在**没有任何外部事实被声明成功**时走（S07-34）。
    "APPLYING": {"APPLIED", "FAILED", "PLANNED"},
}

SAGA_TRANSITIONS = {
    "RUNNING": {"SUCCEEDED", "FAILED", "ABANDONED"},
    "SUCCEEDED": set(), "FAILED": set(), "ABANDONED": set(),
}

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §2.1 change_plan。`plan_body` 是契约实例（S07-17：§2.1 的列重建不出它）。
CREATE TABLE IF NOT EXISTS change_plan (
  tenant_id        TEXT NOT NULL,
  plan_id          TEXT NOT NULL,
  plan_hash        TEXT NOT NULL,
  branch_id        TEXT NOT NULL,
  series_id        TEXT NOT NULL,
  episode_id       TEXT NOT NULL,
  instruction      TEXT NOT NULL,
  parsed_intent    TEXT NOT NULL,
  base_manifest_ref TEXT NOT NULL,
  base_revision    INTEGER NOT NULL,
  context_snapshot TEXT NOT NULL,
  execution_fence  INTEGER NOT NULL,
  memory_checkpoint TEXT NOT NULL,
  policy_version   TEXT NOT NULL,
  policy_ref       TEXT NOT NULL,
  impact_analysis_id TEXT,
  impact_watermark INTEGER NOT NULL DEFAULT 0,
  state            TEXT NOT NULL,
  memory_migration_required INTEGER NOT NULL DEFAULT 0,
  -- §5.6 的 `memory.canon.committed` 只做标记；apply 时据此拦截。
  memory_stale     INTEGER NOT NULL DEFAULT 0,
  -- §4.5 已交付集的处置：`MARK_STALE`（默认）或 `MIGRATE_AND_REDO`。
  -- 它是**用户的选择**，不是 S07 推出来的结论，所以必须落库可查（S07-27）。
  memory_disposition TEXT NOT NULL DEFAULT 'MARK_STALE',
  estimated_cost_upper_micro INTEGER NOT NULL DEFAULT 0,
  currency         TEXT NOT NULL,
  saga_id          TEXT,
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  expires_at       TEXT NOT NULL,
  applied_at       TEXT,
  plan_body        TEXT,

  PRIMARY KEY (tenant_id, plan_id),
  CONSTRAINT cp_state_closed CHECK (state IN (
    'DRAFT','PLANNING','PLANNED','CLARIFICATION_REQUIRED','POLICY_DENIED',
    'INFEASIBLE','EXPIRED','APPLYING','APPLIED','FAILED','SUPERSEDED')),
  CONSTRAINT cp_hash_shape CHECK (
    length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  CONSTRAINT cp_base_revision_positive CHECK (base_revision >= 1),
  CONSTRAINT cp_fence_positive CHECK (execution_fence >= 1),
  CONSTRAINT cp_money_nonneg CHECK (estimated_cost_upper_micro >= 0),
  CONSTRAINT cp_currency_shape CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
  CONSTRAINT cp_ttl CHECK (expires_at > created_at),
  CONSTRAINT cp_applied_has_ts CHECK ((state = 'APPLIED') = (applied_at IS NOT NULL)),
  -- 能进入 PLANNED 的行必须有一份真实的契约实例。
  CONSTRAINT cp_body_when_plannable CHECK (
    state NOT IN ('PLANNED','APPLYING','APPLIED') OR plan_body IS NOT NULL),
  CONSTRAINT cp_memory_flag CHECK (memory_stale IN (0, 1)),
  CONSTRAINT cp_migration_flag CHECK (memory_migration_required IN (0, 1)),
  CONSTRAINT cp_disposition_closed CHECK (
    memory_disposition IN ('MARK_STALE','MIGRATE_AND_REDO'))
);

CREATE INDEX IF NOT EXISTS cp_by_episode_idx
  ON change_plan (tenant_id, series_id, branch_id, episode_id, created_at DESC);
CREATE INDEX IF NOT EXISTS cp_open_idx ON change_plan (tenant_id, state)
  WHERE state IN ('PLANNED','CLARIFICATION_REQUIRED','APPLYING');
CREATE UNIQUE INDEX IF NOT EXISTS cp_hash_uniq
  ON change_plan (tenant_id, plan_id, plan_hash);

-- §2.2 change_plan_patch。§2.2 末尾的 ALTER TABLE ... cpp_value_size 折进 CREATE，
-- 因为 SQLite 没有 ALTER TABLE ADD CONSTRAINT。
CREATE TABLE IF NOT EXISTS change_plan_patch (
  tenant_id     TEXT NOT NULL,
  plan_id       TEXT NOT NULL,
  patch_seq     INTEGER NOT NULL,
  target_service_id TEXT NOT NULL,
  target_id     TEXT NOT NULL,
  target_revision INTEGER NOT NULL,
  target_hash   TEXT NOT NULL,
  path          TEXT NOT NULL,
  before_value  TEXT NOT NULL,
  after_value   TEXT NOT NULL,
  derived_from  TEXT,
  derivation_rule TEXT,

  PRIMARY KEY (tenant_id, plan_id, patch_seq),
  CONSTRAINT cpp_path_shape CHECK (path LIKE '/%'),
  CONSTRAINT cpp_target_rev_positive CHECK (target_revision >= 1),
  CONSTRAINT cpp_derived_rule CHECK (
    (derived_from IS NULL) = (derivation_rule IS NULL)),
  CONSTRAINT cpp_derived_is_earlier CHECK (
    derived_from IS NULL
    OR (derived_from NOT GLOB '*[^0-9]*'
        AND CAST(derived_from AS INTEGER) < patch_seq)),
  CONSTRAINT cpp_value_size CHECK (
    length(before_value) <= 4096 AND length(after_value) <= 4096)
);

CREATE INDEX IF NOT EXISTS cpp_by_target_idx
  ON change_plan_patch (tenant_id, target_service_id, target_id);

-- §2.3 clarification_request。§3.3 的 ABANDONED 在 §2.3 的 DDL 里没有列，
-- 补 `abandoned_at`（S07-18）：答复与放弃是互斥的两种结局。
CREATE TABLE IF NOT EXISTS clarification_request (
  tenant_id    TEXT NOT NULL,
  plan_id      TEXT NOT NULL,
  request_id   TEXT NOT NULL,
  kind         TEXT NOT NULL,
  question     TEXT NOT NULL,
  options      TEXT NOT NULL,
  selected_option_id TEXT,
  answered_by  TEXT,
  answered_at  TEXT,
  abandoned_at TEXT,
  created_at   TEXT NOT NULL,

  PRIMARY KEY (tenant_id, request_id),
  CONSTRAINT cr_kind_closed CHECK (kind IN (
    'MISSING_EPISODE_CONTEXT','DIALOGUE_EXCEEDS_DURATION','AMBIGUOUS_SHOT',
    'UNSUPPORTED_INSTRUCTION','MISSING_ASSET','RIGHTS_BLOCKED')),
  CONSTRAINT cr_options_nonempty CHECK (json_array_length(options) >= 2),
  CONSTRAINT cr_answered_consistent CHECK (
    (selected_option_id IS NULL) = (answered_at IS NULL)),
  CONSTRAINT cr_abandoned_consistent CHECK (
    abandoned_at IS NULL OR answered_at IS NULL),
  CONSTRAINT cr_answered_by CHECK (
    (answered_at IS NULL) = (answered_by IS NULL))
);

CREATE INDEX IF NOT EXISTS cr_open_idx ON clarification_request (tenant_id, plan_id)
  WHERE answered_at IS NULL AND abandoned_at IS NULL;

-- §2.4 change_saga / change_saga_step。
CREATE TABLE IF NOT EXISTS change_saga (
  tenant_id      TEXT NOT NULL,
  saga_id        TEXT NOT NULL,
  plan_id        TEXT NOT NULL,
  state          TEXT NOT NULL,
  current_step   INTEGER NOT NULL,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  failure_code   TEXT,
  failure_detail TEXT,

  PRIMARY KEY (tenant_id, saga_id),
  CONSTRAINT cs_state_closed CHECK (state IN
    ('RUNNING','SUCCEEDED','FAILED','ABANDONED')),
  CONSTRAINT cs_finished_has_ts CHECK ((state = 'RUNNING') = (finished_at IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS cs_plan_active_uniq
  ON change_saga (tenant_id, plan_id) WHERE state = 'RUNNING';

CREATE TABLE IF NOT EXISTS change_saga_step (
  tenant_id    TEXT NOT NULL,
  saga_id      TEXT NOT NULL,
  step_seq     INTEGER NOT NULL,
  step_name    TEXT NOT NULL,
  target_service_id TEXT,
  command_id   TEXT,
  state        TEXT NOT NULL,
  request_hash TEXT,
  response     TEXT,
  error_code   TEXT,
  started_at   TEXT,
  finished_at  TEXT,

  PRIMARY KEY (tenant_id, saga_id, step_seq),
  CONSTRAINT css_state_closed CHECK (state IN
    ('PENDING','RUNNING','DONE','FAILED','SKIPPED'))
);

CREATE INDEX IF NOT EXISTS css_by_plan_step_idx
  ON change_saga_step (tenant_id, saga_id, step_seq);

-- §2.5 impact_summary。派生数据，可重建，不是权威。
CREATE TABLE IF NOT EXISTS impact_summary (
  tenant_id    TEXT NOT NULL,
  plan_id      TEXT NOT NULL,
  analysis_id  TEXT NOT NULL,
  totals       TEXT NOT NULL,
  by_episode   TEXT NOT NULL,
  by_stage     TEXT NOT NULL,
  cost_breakdown TEXT NOT NULL,
  warnings     TEXT NOT NULL,
  lineage_watermark INTEGER NOT NULL,

  PRIMARY KEY (tenant_id, plan_id)
);

-- 未登记事件（`change.plan.planned`）走本地表，不进 outbox：把没有 schema 的
-- 事件交给中继去投递，消费方按契约校验必然失败，而且没人知道该找谁。
CREATE TABLE IF NOT EXISTS local_event (
  tenant_id      TEXT NOT NULL,
  local_event_id TEXT NOT NULL,
  event_type     TEXT NOT NULL,
  aggregate_id   TEXT NOT NULL,
  payload        TEXT NOT NULL,
  created_at     TEXT NOT NULL,

  PRIMARY KEY (tenant_id, local_event_id)
);
"""

# 本轮**不解决**的缺口。契约与文档冲突按 AGENTS.md 处理：不自行选边，
# 按方案散文实现，把偏差记在这里。
CONTRACT_GAPS = [
    # --- plans/S07-change-management.md §9 已登记的十项 ---
    {"id": "C07-1", "kind": "conflict",
     "summary": "catalog 声明 GET /change-plans/{id}，contracts/openapi.v2.yaml 没有这条",
     "handling": "按 catalog 实现 GET；契约文件待补。"},
    {"id": "C07-2", "kind": "needs_owner",
     "summary": "execution-context.v2 的 node_id/node_epoch 是单节点语义，"
                "而 change plan 是集级/多节点操作",
     "handling": "只借用 context 的集级字段；context_snapshot 刻意不存 "
                 "node_id/node_epoch，apply 也不碰 epoch。"},
    {"id": "C07-3", "kind": "conflict",
     "summary": "docs/04 §2 要求 plan 含顶层 source hashes / impact_watermark / "
                "policy_version / memory_checkpoint，schema 里只在 context/impact 嵌套",
     "handling": "用 context.memory_checkpoint、context.policy_ref、"
                 "impact.lineage_watermark；无顶层 source hashes。"},
    {"id": "C07-4", "kind": "needs_owner",
     "summary": "E13 恢复要从 S08 的 revision 查到 change_plan_ref，但 "
                "episode-manifest.v2 没有显式要求 payload 含它",
     "handling": "commit 时带 change_plan_ref；恢复时若查不到就按「未提交」重试，"
                 "绝不假设已提交。"},
    {"id": "C07-5", "kind": "conflict",
     "summary": "enterprise-change-plan.v2 是 additionalProperties:false 的全必填 "
                "schema，无法包装 state/replayed/saga_id",
     "handling": "响应体是纯 plan；state 走 X-Plan-State，重放走 "
                 "X-Idempotent-Replay，Saga 走 GET /change-plans/{id}/saga。"},
    {"id": "C07-6", "kind": "conflict",
     "summary": "CLARIFICATION_REQUIRED 是成功的业务结果，但不满足 "
                "patches.minItems:1，无法用 2xx 表达",
     "handling": "409 + details.clarification（§5.1 的选择）。"},
    {"id": "C07-7", "kind": "conflict",
     "summary": "apply 的 Operation.state 枚举没有 applied，Operation 与 Run.state "
                "又是两套词汇",
     "handling": "apply 返回 202 accepted，结果一律看 GET /change-plans/{id}；"
                 "Saga 逐步状态另开查询。"},
    {"id": "C07-8", "kind": "conflict",
     "summary": "automation-policy.v2 的 overrides[].scope_type 无 run（S02 G1）",
     "handling": "按 episode 覆盖，不发明 run 级覆盖。"},
    {"id": "C07-9", "kind": "conflict",
     "summary": "无 change-plan-patch.v2.schema.json；patches[] 内嵌在 "
                "enterprise-change-plan.v2 里，缺 derived_from / derivation_rule",
     "handling": "DB 有这两列并用于追溯；响应里的 patches 严格按 schema 只给四条"
                 "（target_ref/path/before/after）。派生关系在 DB 与 "
                 "derived_adjustments 里可见。"},
    {"id": "C07-10", "kind": "needs_owner",
     "summary": "docs/07 §3 要求「同时创建 memory impact plan」，没有契约定义它",
     "handling": "用 change_plan.memory_migration_required=true 近似；不伪造独立对象。"},

    # --- 实现时新发现的 ---
    {"id": "S07-11", "kind": "conflict",
     "summary": "方案 §4.1 步骤 C 与 §4.2 步骤 2 要求 "
                "POST /policy-decisions:evaluate，但 S02 只实现了 "
                "POST /policy-decisions:resolve-effective",
     "handling": "依赖注入 policy_source 闭包；deps.build_policy_source 调用 "
                 "resolve-effective 并按 kind 取结论。不猜 endpoint，未配置即 503。"},
    {"id": "S07-12", "kind": "conflict",
     "summary": "S09 的 impact 载荷把 derived_adjustments 放进了 impact 内部，"
                "而 enterprise-change-plan.v2 的 impact 是 additionalProperties:false",
     "handling": "读取时提升到顶层 impact 之外；impact 只保留 schema 的 6 个键。"},
    {"id": "S07-13", "kind": "conflict",
     "summary": "S08 的 GET /manifests/{id}/revisions 只返回 "
                "{revision,state,content_hash,execution_fence,committed_at}，"
                "没有 payload，E13 恢复无法一次列表找到 change_plan_ref",
     "handling": "逐 revision 调 GET /manifests/{id}/revisions/{rev} 读 payload；"
                 "只扫最近 N 条，N 由调用方给。"},
    {"id": "S07-14", "kind": "needs_owner",
     "summary": "contracts/change.plan-applied.v2.schema.json 的 x-conflicts "
                "CPG-1…CPG-8 与两条 x-needs-owner（audit.record.intent topic、"
                "new_manifest_ref 在 rebase 下的语义）",
     "handling": "事件按 §5.5 的 data 原样发；不写 audit.record.intent（该 topic "
                 "不在 event-topics.v2.json 里）；new_manifest_ref 只填单个对象，"
                 "跨集场景在 gap 里留白。"},
    {"id": "S07-15", "kind": "conflict",
     "summary": "§5.7 的码表与 §4.1 步骤 7 / §8 测试清单三处对 POLICY_DENIED 的"
                "状态码不一致（422 / 409 POLICY_DENIED_NOW / 409 POLICY_DENIED）",
     "handling": "生成阶段用 §5.7 的 422 POLICY_DENIED；apply 阶段用 T07-44 的 "
                 "409 POLICY_DENIED。两处都登记在这里，不合并成一个解释。"},
    {"id": "S07-16", "kind": "conflict",
     "summary": "§2.6 的 idempotency_record / outbox / inbox DDL 与平台 SDK 的"
                "表结构不同（列名、列数都不同），且 SQLite 无法并存两套",
     "handling": "以 platform/idempotency-sdk 与 platform/event-sdk 为准；"
                 "本服务不重复建这三张表。"},
    {"id": "S07-17", "kind": "conflict",
     "summary": "§2.1 的列重建不出 enterprise-change-plan.v2 的 16 个必填字段"
                "（缺 prepared_revision_refs / derived_adjustments / "
                "blocking_issues / required_policy_decisions）",
     "handling": "增 change_plan.plan_body 存契约实例；查询与重放都读它，"
                 "计划不可变因此是存储层事实。"},
    {"id": "S07-18", "kind": "conflict",
     "summary": "§3.3 的 clarification 状态机有 ABANDONED，§2.3 的 DDL 没有对应列",
     "handling": "增 abandoned_at，并加约束「答复与放弃互斥」。"},
    {"id": "S07-19", "kind": "conflict",
     "summary": "§4.2 的七步里没有 S16 记忆提交，§4.5 又说跨集 apply「多一步」；"
                "且 §6.4 的 command_id 按 step_n 派生，两种步数下同一个 n 指不同步",
     "handling": "跨集 Saga 用八步（第 5 步 COMMIT_MEMORY）；command_id 改按 "
                 "**步骤名**派生，使崩溃恢复不因步数变化而换键。"},
    {"id": "S07-20", "kind": "needs_owner",
     "summary": "方案 §10 的 7 个未决问题（编号到 id 的映射、跨实现 plan_hash、"
                "prepared revision 的 GC、逐集成本、SERIES_UNIQUE_MATCH 是否允许、"
                "Saga 重入点、unattended 下跨集修改）",
     "handling": "按方案当前选择实现（允许 SERIES_UNIQUE_MATCH 并记 warning；"
                 "重入点从失败步开始；跨集修改在有 memory preview 的前提下允许），"
                 "等 owner 裁决后调整。"},
    {"id": "S07-21", "kind": "conflict",
     "summary": "S25 的 route-decision 没有「一次计划级成本上界」这个字段，"
                "只有逐候选的 quote_upper_bound_micro",
     "handling": "cost_source 闭包接收计划意图并返回单一上界；未配置即 503，"
                 "S07 不自己估。"},
    {"id": "S07-22", "kind": "conflict",
     "summary": "S08 的 :commit 需要完整 candidate_manifest，而其形状由 "
                "episode-manifest.v2 / S08 拥有；S07 自行拼装就是第二个 manifest 权威",
     "handling": "committer 闭包接收 {manifest_id, expected_revision, "
                 "change_plan_ref, prepared_revision_refs, patches}，由 "
                 "deps.build_committer 读基线 revision 并在适配层合成 manifest。"},
    {"id": "S07-23", "kind": "conflict",
     "summary": "§4.1 步骤 7 的 abstain 分支返回 POLICY_ABSTAIN，§5.7 的码表没有它；"
                "§5.6 的 memory.canon.committed 会「标 stale_memory 并在 apply 时拦」，"
                "码表里也没有对应码",
     "handling": "补两个码：409 POLICY_ABSTAIN（retryable）、409 STALE_MEMORY。"
                 "两者都不在「审核已通过」的放行路径上。"},
    {"id": "S07-24", "kind": "needs_owner",
     "summary": "§4.1 要求「S07 必须从 S06 的 schema 读取 camera.angle 枚举，不能硬编码」，"
                "但没有任何服务暴露「给我 S06 的 DSL 词汇表」这个读接口",
     "handling": "注入 storyboard_source 闭包，由它返回可写入的字段与枚举；"
                 "S07 只做中文词→词元的映射，词元不在上游枚举里就 422。"
                 "未配置即 503，不用硬编码兜底。"},
    {"id": "S07-25", "kind": "conflict",
     "summary": "§4.1 的解析表把「字段无法映射到 S06 DSL」判为 "
                "CLARIFICATION_REQUIRED(UNSUPPORTED_INSTRUCTION)，§5.7 又把"
                "「无法解析」定为 422 INSTRUCTION_UNPARSEABLE，两者的边界没有写",
     "handling": "词法层就抽不出目标或字段（没有可问的选项）→ 422 "
                 "INSTRUCTION_UNPARSEABLE；抽出字段但取值不在上游枚举/当前值"
                 "读不到 → 422，因为这类情形给不出两个有意义的选项。"
                 "CLARIFICATION_REQUIRED 只留给真正可选的三种（缺集、镜头歧义、"
                 "对白超长）。"},
    {"id": "S07-26", "kind": "needs_owner",
     "summary": "§4.1 步骤 7 的 require_human 要「建 ReviewTask（S40），Saga 停在等审批」，"
                "但 S40 没有可注入的建任务接口，Operation 也没有 waiting 态可停",
     "handling": "按 abstain 同形处理：409 POLICY_ABSTAIN、retryable=true，"
                 "details.decision=\"require_human\"。不放行、不伪造审批。"},
    {"id": "S07-27", "kind": "conflict",
     "summary": "§4.5 给已交付集三个处置选项，但 clarification 的 kind 枚举是封闭的六个值，"
                "没有 MEMORY_MIGRATION 之类可表达它",
     "handling": "改为 POST /change-plans 的可选字段 memory_disposition ∈ "
                 "{MARK_STALE, MIGRATE_AND_REDO}；默认 MARK_STALE（只标 stale）。"
                 "「取消」不建对象——§3.1 已明确没有 CANCELED 态。"},
    {"id": "S07-28", "kind": "conflict",
     "summary": "execution-context.v2 必填 request_hash，而 request_hash 是"
                "整个 body（含这份 context）的哈希，调用方无法先填",
     "handling": "本服务不校验 context.request_hash，只用它做透传快照；"
                 "幂等由 Idempotency-Key + canonical_request_hash 承担。"},
    {"id": "S07-29", "kind": "conflict",
     "summary": "§4.5 步骤 2 要求先调 S16 preview「不实际提交」，但 S16 只有 "
                "POST /series/{series_id}/memory/commits（提交）与 merge-preview"
                "（分支合并预览），没有「如果这样改正史会变成什么」的 dry-run",
     "handling": "deps.build_memory_preview 仍调 commits 加 dry_run=true，并把"
                 "上游是否认这个标记写进响应（dry_run_honored / warning）；"
                 "不静默改真提交，也不假装 preview 过。E08 的「先看影响」因此"
                 "只能保证到「S07 侧先算 S09 闭包」，跨集改稿在 S16 补 preview "
                 "前不能上线。"},
    {"id": "S07-30", "kind": "conflict",
     "summary": "方案 §4.1 的解析表假设 S06 有「按 scene_no/shot_no_in_scene "
                "查 shot」的接口，但 S06 未实现，全仓只有 GET "
                "/shots/{id}/revisions/{revision}（按 id 读），编号→id 的映射"
                "没有任何服务负责",
     "handling": "deps.build_shot_source 定义 GET /shots?series_id=&scene_no=&"
                 "shot_no_in_scene= 的请求形状（S07 侧约定，上游未实现）；未配置"
                 "即 503。解析表本身（四种 resolution）已按方案实现，等 S06 上线"
                 "即可用。"},
    {"id": "S07-31", "kind": "conflict",
     "summary": "§3.1 写「任意非终态 → EXPIRED」，但 APPLYING 不在其中：Saga 在飞时"
                "可能已经改了一部分外部事实（S16 checkpoint、S08 manifest），"
                "把它置 EXPIRED 会让「已提交但没写回」的计划被当成没发生；"
                "§4.7 的扫描条件又只写 state='PLANNED'，与 T07-17 要求的"
                "「澄清超时也过期」不一致",
     "handling": "sweep_expired 覆盖 DRAFT/PLANNING/PLANNED/CLARIFICATION_REQUIRED，"
                 "**不含 APPLYING**；APPLYING 的停滞由 recover_apply 与 "
                 "sweep_stale_commands 处理。过期时把未答复澄清标 ABANDONED，"
                 "绝不自动选默认。"},
    {"id": "S07-32", "kind": "conflict",
     "summary": "§4.1 步骤 7 要求 apply 时 deny「并置 plan 为 POLICY_DENIED」，"
                "但 §3.1 的转移表里 POLICY_DENIED 只能从 PLANNING 进入；"
                "让 APPLYING → POLICY_DENIED 会绕过状态机（也抹掉「已经执行到"
                "第几步」这个事实）",
     "handling": "apply 时 deny 返回 409 POLICY_DENIED 并把 plan 置 FAILED"
                 "（Saga FAILED，failure_code=POLICY_DENIED），保留审计链；"
                 "不新增状态机外的边。UI 用 failure_code 区分「不允许」与"
                 "「依赖故障」。"},
    {"id": "S07-33", "kind": "needs_owner",
     "summary": "§4.5 的「迁移并重做」要求过 budget_increase gate。该闸名确实"
                "在 automation-policy.v2 的 GATES 里（七选一），但 S07 没有预算"
                "权威可以判断「成本上升够不够多」，而 require_human 之后要建的"
                "ReviewTask（S40）也没有可注入接口（S07-26），所以「要求人工」"
                "在 S07 侧只能等价于「不放行」",
     "handling": "把 estimated_cost_upper_micro 与 memory_disposition 一并交给 "
                 "policy_source；S02 若把 budget_increase 列进 required_gates，"
                 "build_policy_source 折成 require_human，apply 阶段得到 409 "
                 "POLICY_ABSTAIN（retryable，不自动放行）。S07 不自己比成本，"
                 "也不把「已通过预算闸」当放行依据。"},
    {"id": "S07-34", "kind": "conflict",
     "summary": "§4.2 说 abstain / 依赖不可达「可重试」，但两处前提都没写：(1) §3.1 "
                "的转移表没有 APPLYING → PLANNED，而「重试」= 再 apply 一次 = 计划"
                "必须回到 PLANNED；(2) §5.7 只有 DEPENDENCY_UNAVAILABLE 一个 503，"
                "不区分「上游不可达」与「上游答了但没给出可用结论」（S25 无成本上界、"
                "S02 结论不是 deny/allow/abstain）",
     "handling": "补一条边 APPLYING → PLANNED，仅在**没有任何外部事实被声明成功**"
                 "（即 Saga 在 COMMIT 之前失败）时走；已提交的计划绝不回退。传输"
                 "层失败统一 503 DEPENDENCY_UNAVAILABLE(retryable=true)；上游作答但"
                 "结论不可用另给 COST_SOURCE_UNAVAILABLE / POLICY_UNAVAILABLE，"
                 "同为 503 且 retryable。两者都不被当成「已通过」。"},
    {"id": "S07-35", "kind": "conflict",
     "summary": "§6.2 写「同 base 的两份计划都还是 PLANNED，只有一份能 apply 成」"
                "（T07-36 也按这个前提写：第二份应得 MANIFEST_REVISION_CONFLICT），"
                "但 §3.1 的转移表有 PLANNED → SUPERSEDED，实现按转移表在**生成时**"
                "就把同 base/同集/同分支的旧计划置 SUPERSEDED",
     "handling": "按 §3.1 的转移表实现：旧计划在生成时 SUPERSEDED，对它的 apply"
                 "得到 409 PLAN_NOT_PLANNED(details.state=SUPERSEDED)，而不是"
                 "MANIFEST_REVISION_CONFLICT。T07-36 的「两份都 PLANNED」前提只"
                 "在同一 base 上**不同集**时才成立（本实现里那种情形确实两份都"
                 "PLANNED，冲突在 CHECK_MANIFEST 里以 MANIFEST_REVISION_CONFLICT"
                 "出现）。两条路径都覆盖了，但触发条件与 §6.2 的散文不同。"},
    {"id": "S07-36", "kind": "conflict",
     "summary": "§4.3 把 S08 的 REVISION_CONFLICT 列为一种独立的 commit 失败，"
                "但没有说 S07 要不要把它改写成本服务的 MANIFEST_REVISION_CONFLICT"
                "（§5.7 的码表里有后者、没有前者）",
     "handling": "不改写：S08 在它自己的边界上给出的码原样透传（Saga "
                 "failure_code=REVISION_CONFLICT），S07 只补审计与状态。理由：改"
                 "写会让「manifest 动了」这个 S08 的事实与 S07 自己算出的 "
                 "MANIFEST_REVISION_CONFLICT（前置检查失败）看起来是同一件事，而"
                 "两者一个代表「有人抢先提交了」、一个代表「你确认的基础已经旧了」。"
                 "调用方靠 HTTP 状态与 details 区分。"},
    {"id": "S07-37", "kind": "conflict",
     "summary": "T07-18 要求「单一候选时不要建澄清，直接做并记 derived」"
                "（`_create_clarification` 的 500 CLARIFICATION_NEEDS_TWO_OPTIONS "
                "与 DB 约束 `json_array_length(options) >= 2` 都按这句写的），但方案"
                "没写「直接做」时那条派生改动应该记在哪、`derivation_rule` 该填"
                "什么名字",
     "handling": "只实现了一半：少于两个选项的澄清会被 500 拒绝（不会退化成命令），"
                 "但**没有**把它自动转成一条 derived 补丁——单候选的三种情形"
                 "（NO_MATCH、字段读不到、枚举不接受）在 `_build_patches` / `_prepare` "
                 "里都被判成 422（INSTRUCTION_UNPARSEABLE / CHANGE_INFEASIBLE），"
                 "因为方案没有规定被自动选中的默认值的来源与规则名，自行发明一个"
                 "就是伪造 S06 的派生规则（T07-05 明令规则名只能来自上游）。等 owner "
                 "给出规则名的来源后再补。"},
]
