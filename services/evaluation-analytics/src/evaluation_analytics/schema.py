"""S44 自己的 schema。表来源是 `plans/S44-evaluation-analytics.md` §5。

五条结构性事实：

1. **S44 不是账本。** 本库没有 `ledger` / `balance` / `reservation`，也没有
   `qc_result` / `accepted_master`。原始金额只以 `cost_allocation` 的形式
   存在，每条带 `cost_entry_id` 指回 S23 的逻辑引用；删掉本库全部数据，
   S23 仍然能出账单。反过来不成立，这正是「读模型」的含义（§2.1）。
2. **金额一律整数 micro。** `amount_micro`、`*_micro` 全是 SQLite INTEGER。
   一笔原始金额分摊后必须 `sum(allocations) == original_amount`，最后一项
   吸收舍入余数；浮点分摊会在上万笔后产生分币漂移（§4.2）。
3. **快照不可变。** `metric_snapshot` 没有 UPDATE 路径。重算 = 新快照；
   天然键是 `tenant + cohort + metric_key + metric_version +
   watermarks_hash + allocation_policy_version`（§7）。旧快照永远不被改写。
4. **无数据不是 0。** DDL 的 `CHECK` 与事件契约的 `allOf` 是同一条规则：
   `n = 0` 时 `value` 必须为 NULL；`n > 0` 时必须有值。把「没有数据」写成
   0 会让看板给出相反判断（§2.4、E38-S44-06）。
5. **计划不使用 `execution_fence` / `node_epoch` 作为坐标。** 这两个值只出现
   在签名过的 execution context 里（校验、不落库），本服务没有任何一列
   存它们，也没有任何比较；分析不是 run/branch，也不推进接受指针。

方案里的 `jsonb` 存 TEXT（JCS）、`timestamptz` 存定宽 UTC 字符串、
`text[]` 存 JSON 数组。与 §5 的偏差都登记在 `CONTRACT_GAPS`：

- `metric_snapshot` 多 `watermarks_hash`（S44-9）：§7 的天然唯一键含
  watermarks，jsonb 不能直接进 UNIQUE。
- 多 `project_scope`（S44-10）：E28-S44-02 要求 cohort 里的 project_ids 属于
  同租户；S03 不在 service.yaml 的 depends_on 里，只能从已消费的 S08
  `snapshot.manifest.committed` 事件建本地归属投影。
- 多 `accepted_projection`（S44-10）：K1 的「被接受」事实来自 S08 事件；
  没有事件时**没有**「接受 = 0」的推断，快照会因缺源而 503。
- `metric_definition` 预置 8 个 KPI（S44-3）：§10 第 1 步要求口径写死并与
  docs/15 逐字对齐；定义仍是本服务的权威数据，可由调用方注册新版本。
- `metric_snapshot` 多 `numerator_value` / `denominator_value`（S44-17）：
  §6.5 要求导出「可被第三方复算」，但 §5 只有 `value` 与 `n`，复算者拿不到
  分子分母。这两列只是**复算凭据**（同一次计算的中间量），不是第二份权威。
"""

ID_PREFIX = {
    "cohort": "coh_",
    "snapshot": "snap_",
    "dataset": "bds_",
    "item": "bdi_",
    "evaluation": "evr_",
    "blind_review": "brv_",
    "vote": "bvt_",
    "experiment": "exp_",
    "assignment": "asm_",
    "projection": "prj_",
    "roi": "roi_",
}

SOURCE = "S44"
SCHEMA_VERSION = "2.0"
TOPIC = "drama.evaluation-analytics.v2"
SERVICE_NAME = "evaluation-analytics"

EVENT_SNAPSHOT = "analytics.metric.snapshot.created"
EVENT_EVALUATION = "analytics.evaluation.completed"
EVENT_PROJECTION = "analytics.cost.projection.updated"
EVENTS = (EVENT_SNAPSHOT, EVENT_EVALUATION, EVENT_PROJECTION)

# 消费事件名按实际产出方登记（§6.3；C8 已于 2026-11-05 裁决）。
EVENT_LEDGER = "finance.ledger.entry-posted"
EVENT_JOB_STATE = "generation.job.state-changed"
EVENT_TECHNICAL_QC = "quality.technical.completed"
EVENT_SEMANTIC_QC = "quality.semantic.completed"
EVENT_ALIGNMENT_QC = "quality.alignment.completed"
EVENT_SUBTITLE_READY = "post.subtitle.ready"
EVENT_REVIEW_DECISION = "review.decision.recorded"
EVENT_SNAPSHOT_COMMITTED = "snapshot.manifest.committed"
EVENT_PACKAGE_PUBLISHED = "delivery.package.published"

CONSUMERS = {
    EVENT_LEDGER: "s44.ledger-watermark.v1",
    EVENT_JOB_STATE: "s44.job-attempt.v1",
    EVENT_TECHNICAL_QC: "s44.technical-qc.v1",
    EVENT_SEMANTIC_QC: "s44.semantic-qc.v1",
    EVENT_ALIGNMENT_QC: "s44.alignment-qc.v1",
    EVENT_SUBTITLE_READY: "s44.subtitle-qc.v1",
    EVENT_REVIEW_DECISION: "s44.review-decision.v1",
    EVENT_SNAPSHOT_COMMITTED: "s44.accepted-master.v1",
    EVENT_PACKAGE_PUBLISHED: "s44.package-published.v1",
}

# 8 个 KPI（docs/15 §3；§11 C3 记录「7 个还是 8 个」的计数分歧，本服务按
# 8 项实现，不自行删项）。
METRIC_KEYS = (
    "usable_rate",                  # K1 成片可用率
    "first_pass_shot_rate",         # K2 首轮镜头可用率
    "cost_per_usable_minute",       # K3 每可用分钟成本
    "delivery_lead_time",           # K4 单集交付耗时
    "rework_rate",                  # K5 返工率
    "human_intervention_rate",      # K6 人工干预率
    "memory_correctness",           # K7 记忆正确性
    "customer_roi",                 # K8 客户项目毛利/ROI
)
METRIC_VERSION = 1

# 分母口径写死在这里，不留在文档里（§2.3 末段、§10 第 1 步）。
METRIC_DEFINITIONS = (
    {"metric_key": "usable_rate", "version": METRIC_VERSION, "unit": "ratio",
     "sample_unit": "episode", "ci_method": "wilson", "min_n": 1,
     "numerator_spec": {"accepted_master": True},
     "denominator_spec": {"entered_formal_render": True},
     "exclusion_rules": ["include_failed_runs=true", "abstain_is_not_pass"]},
    {"metric_key": "first_pass_shot_rate", "version": METRIC_VERSION,
     "unit": "ratio", "sample_unit": "shot", "ci_method": "wilson", "min_n": 1,
     "numerator_spec": {"first_qc_decision": "pass", "requires_no_rework": True},
     "denominator_spec": {"entered_first_qc": True},
     "exclusion_rules": ["abstain_is_not_pass"]},
    {"metric_key": "cost_per_usable_minute", "version": METRIC_VERSION,
     "unit": "CNY/min", "sample_unit": "minute",
     "ci_method": "bootstrap_percentile", "min_n": 1,
     "numerator_spec": {"production_allocations_micro": True},
     "denominator_spec": {"usable_minutes": True},
     "exclusion_rules": ["cost_type=evaluation_excluded",
                         "estimated_allocation_counted_separately"]},
    {"metric_key": "delivery_lead_time", "version": METRIC_VERSION,
     "unit": "seconds", "sample_unit": "episode",
     "ci_method": "bootstrap_percentile", "min_n": 1,
     "numerator_spec": {"published_at": True},
     "denominator_spec": {"first_run_created_at": True},
     "exclusion_rules": ["paused_time_not_counted"]},
    {"metric_key": "rework_rate", "version": METRIC_VERSION, "unit": "ratio",
     "sample_unit": "shot", "ci_method": "wilson", "min_n": 1,
     "numerator_spec": {"shots_with_rework_decision": True},
     "denominator_spec": {"all_shots": True},
     "exclusion_rules": ["counted_per_shot_not_per_attempt"]},
    {"metric_key": "human_intervention_rate", "version": METRIC_VERSION,
     "unit": "ratio", "sample_unit": "node", "ci_method": "wilson",
     "min_n": 1,
     "numerator_spec": {"nodes_with_human_decision": True},
     "denominator_spec": {"all_nodes": True},
     "exclusion_rules": ["automatic_pass_is_not_intervention"]},
    {"metric_key": "memory_correctness", "version": METRIC_VERSION,
     "unit": "ratio", "sample_unit": "fact", "ci_method": "wilson",
     "min_n": 1,
     "numerator_spec": {"unrefuted_referenced_facts": True},
     "denominator_spec": {"referenced_facts": True},
     "exclusion_rules": ["only_s17_flagged_and_s40_confirmed"]},
    {"metric_key": "customer_roi", "version": METRIC_VERSION, "unit": "ratio",
     "sample_unit": "contract", "ci_method": "none", "min_n": 1,
     "numerator_spec": {"revenue_micro_minus_allocated_cost": True},
     "denominator_spec": {"revenue_micro": True},
     "exclusion_rules": ["contract_revenue_is_external_input"]},
)

CI_METHODS = ("wilson", "bootstrap_percentile", "none")

# §5 cost_allocation 的闭集。旧版本遇到不认识的新 basis 必须拒绝分摊，
# 不能静默归零（C-S44-03）。
ALLOCATION_BASES = ("direct", "by_episode_share", "to_parent_episode",
                    "by_usable_minutes", "estimated_by_policy", "excluded")
ALLOCATION_DIMENSIONS = ("project", "series", "episode", "scene", "shot",
                         "role", "model", "tenant")
COST_TYPES = ("generation", "retry", "platform_fixed", "evaluation", "other")

EVALUATION_STATES = ("CREATED", "QUEUED", "RUNNING", "SUCCEEDED", "PARTIAL",
                     "FAILED", "CANCELLED")
TERMINAL_EVALUATION_STATES = ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED")
EXPERIMENT_STATES = ("DRAFT", "RUNNING", "STOPPED", "COMPLETED")
BLIND_REVIEW_STATES = ("OPEN", "CLOSED", "ABANDONED")
DATASET_PURPOSES = ("qc_calibration", "model_selection", "regression")
DIFFICULTIES = ("easy", "medium", "hard")
VOTE_DIMENSIONS = ("visual", "audio", "lipsync", "story")
PROJECTION_WINDOWS = ("7d", "30d")

# 快照事件的 aggregate_sequence 恒为 1：一个 snapshot_id 只有一条事件。
SEQUENCE_ONE = 1

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metric_definition (
  tenant_id            TEXT NOT NULL DEFAULT '',
  metric_key           TEXT NOT NULL,
  version              INTEGER NOT NULL,
  unit                 TEXT NOT NULL,
  numerator_spec       TEXT NOT NULL,
  denominator_spec     TEXT NOT NULL,
  sample_unit          TEXT NOT NULL,
  exclusion_rules      TEXT NOT NULL DEFAULT '[]',
  ci_method            TEXT NOT NULL CHECK (ci_method IN
                       ('wilson','bootstrap_percentile','none')),
  min_n                INTEGER NOT NULL DEFAULT 1 CHECK (min_n >= 0),
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, metric_key, version)
);

CREATE TABLE IF NOT EXISTS cohort (
  tenant_id            TEXT NOT NULL,
  cohort_id            TEXT NOT NULL,
  definition           TEXT NOT NULL,
  frozen_at            TEXT NOT NULL,
  member_count         INTEGER NOT NULL CHECK (member_count >= 0),
  source_watermarks    TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, cohort_id)
);
CREATE INDEX IF NOT EXISTS cohort_project_idx ON cohort (tenant_id, frozen_at);

-- §2.2 / §4.1 第 2 步：冻结 cohort 要「写 member 集合」。只存 member_count
-- 无法复现成员集合，重算与导出都会漂（S44-16）。
CREATE TABLE IF NOT EXISTS cohort_member (
  tenant_id            TEXT NOT NULL,
  cohort_id            TEXT NOT NULL,
  episode_id           TEXT NOT NULL,
  project_id           TEXT,
  PRIMARY KEY (tenant_id, cohort_id, episode_id),
  FOREIGN KEY (tenant_id, cohort_id)
    REFERENCES cohort (tenant_id, cohort_id)
);

CREATE TABLE IF NOT EXISTS metric_snapshot (
  tenant_id                 TEXT NOT NULL,
  snapshot_id               TEXT NOT NULL,
  metric_key                TEXT NOT NULL,
  metric_version            INTEGER NOT NULL,
  cohort_id                 TEXT NOT NULL,
  value                     REAL,
  -- §6.5 要求导出的表格「可被第三方复算」：只有 value 与 n 时，复算者拿不到
  -- 分子分母。两列是**复算凭据**，不是第二份权威（源仍在 S23/S26/QC）。
  numerator_value           REAL,
  denominator_value         REAL,
  unit                      TEXT NOT NULL,
  n                         INTEGER NOT NULL CHECK (n >= 0),
  distribution              TEXT NOT NULL,
  ci95                      TEXT,
  excluded                  TEXT NOT NULL,
  mixed_versions            INTEGER NOT NULL CHECK (mixed_versions IN (0,1)),
  stale_sources             TEXT NOT NULL DEFAULT '{}',
  source_watermarks         TEXT NOT NULL,
  -- §7 的天然键含 watermarks；jsonb 不能直接进 UNIQUE，这里存其 JCS 摘要。
  watermarks_hash           TEXT NOT NULL,
  allocation_policy_version TEXT,
  low_confidence            INTEGER NOT NULL DEFAULT 0
                            CHECK (low_confidence IN (0,1)),
  computed_at               TEXT NOT NULL,
  PRIMARY KEY (tenant_id, snapshot_id),
  UNIQUE (tenant_id, cohort_id, metric_key, metric_version,
          watermarks_hash, allocation_policy_version),
  -- §2.4 / 事件契约 allOf：无数据不是 0。
  CHECK ((n = 0 AND value IS NULL) OR (n > 0 AND value IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS snapshot_lookup_idx ON metric_snapshot
  (tenant_id, metric_key, cohort_id, computed_at DESC);

CREATE TABLE IF NOT EXISTS allocation_policy (
  tenant_id            TEXT NOT NULL,
  policy_version       TEXT NOT NULL,
  rules                TEXT NOT NULL,
  effective_from       TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, policy_version)
);

CREATE TABLE IF NOT EXISTS cost_allocation (
  tenant_id            TEXT NOT NULL,
  cost_entry_id        TEXT NOT NULL,
  policy_version       TEXT NOT NULL,
  dimension            TEXT NOT NULL CHECK (dimension IN
                       ('project','series','episode','scene','shot','role',
                        'model','tenant')),
  dimension_value      TEXT NOT NULL,
  amount_micro         INTEGER NOT NULL,
  basis                TEXT NOT NULL CHECK (basis IN
                       ('direct','by_episode_share','to_parent_episode',
                        'by_usable_minutes','estimated_by_policy','excluded')),
  PRIMARY KEY (tenant_id, cost_entry_id, policy_version, dimension,
               dimension_value)
);
CREATE INDEX IF NOT EXISTS cost_alloc_episode_idx ON cost_allocation
  (tenant_id, dimension, dimension_value);

CREATE TABLE IF NOT EXISTS benchmark_dataset (
  tenant_id            TEXT NOT NULL,
  dataset_id           TEXT NOT NULL,
  name                 TEXT NOT NULL,
  version              INTEGER NOT NULL CHECK (version >= 1),
  purpose              TEXT NOT NULL CHECK (purpose IN
                       ('qc_calibration','model_selection','regression')),
  item_count           INTEGER NOT NULL CHECK (item_count >= 0),
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, dataset_id, version)
);

CREATE TABLE IF NOT EXISTS benchmark_item (
  tenant_id            TEXT NOT NULL,
  dataset_id           TEXT NOT NULL,
  dataset_version      INTEGER NOT NULL,
  item_id              TEXT NOT NULL,
  artifact_refs        TEXT NOT NULL,
  ground_truth         TEXT NOT NULL,
  difficulty           TEXT CHECK (difficulty IN ('easy','medium','hard')),
  source_ref           TEXT,
  PRIMARY KEY (tenant_id, dataset_id, dataset_version, item_id),
  FOREIGN KEY (tenant_id, dataset_id, dataset_version)
    REFERENCES benchmark_dataset (tenant_id, dataset_id, version)
);

CREATE TABLE IF NOT EXISTS evaluation_run (
  tenant_id            TEXT NOT NULL,
  evaluation_run_id    TEXT NOT NULL,
  dataset_id           TEXT NOT NULL,
  dataset_version      INTEGER NOT NULL,
  evaluator_ref        TEXT NOT NULL,
  state                TEXT NOT NULL CHECK (state IN
                       ('CREATED','QUEUED','RUNNING','SUCCEEDED','PARTIAL',
                        'FAILED','CANCELLED')),
  item_total           INTEGER NOT NULL CHECK (item_total >= 0),
  item_succeeded       INTEGER NOT NULL DEFAULT 0
                       CHECK (item_succeeded >= 0),
  item_failed          INTEGER NOT NULL DEFAULT 0 CHECK (item_failed >= 0),
  result_ref           TEXT,
  budget_ref           TEXT,
  -- 重复启动必须收敛到同一个 run，不能产生第二次付费（E12-S44-02）。
  command_id           TEXT NOT NULL,
  request_hash         TEXT NOT NULL,
  started_at           TEXT,
  finished_at          TEXT,
  PRIMARY KEY (tenant_id, evaluation_run_id),
  UNIQUE (tenant_id, command_id),
  CHECK (item_succeeded + item_failed <= item_total)
);

CREATE TABLE IF NOT EXISTS blind_review (
  tenant_id            TEXT NOT NULL,
  blind_review_id      TEXT NOT NULL,
  dataset_id           TEXT NOT NULL,
  dataset_version      INTEGER NOT NULL,
  dimensions           TEXT NOT NULL,
  item_count           INTEGER NOT NULL CHECK (item_count >= 0),
  reviewer_count       INTEGER NOT NULL CHECK (reviewer_count >= 0),
  state                TEXT NOT NULL CHECK (state IN
                       ('OPEN','CLOSED','ABANDONED')),
  agreement_alpha      REAL,
  -- §5 只有 reviewer_count；投票要校验投票人属于本次会话，抽样集合也要
  -- 可复现。两列存抽样集合与允许的投票人（S44-15）。
  item_ids             TEXT NOT NULL DEFAULT '[]',
  reviewers            TEXT NOT NULL DEFAULT '[]',
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, blind_review_id)
);

-- §5 没有把"本次会话抽了哪些条目"落成表；只存 count 会让投票无法校验
-- 条目属于本次会话（S44-15）。
CREATE TABLE IF NOT EXISTS blind_review_item (
  tenant_id            TEXT NOT NULL,
  blind_review_id      TEXT NOT NULL,
  item_id              TEXT NOT NULL,
  artifact_refs        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, blind_review_id, item_id),
  FOREIGN KEY (tenant_id, blind_review_id)
    REFERENCES blind_review (tenant_id, blind_review_id)
);

CREATE TABLE IF NOT EXISTS blind_review_vote (
  tenant_id            TEXT NOT NULL,
  blind_review_id      TEXT NOT NULL,
  reviewer_id          TEXT NOT NULL,
  item_id              TEXT NOT NULL,
  dimension            TEXT NOT NULL,
  score                INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
  comment_ref          TEXT,
  voted_at             TEXT NOT NULL,
  PRIMARY KEY (tenant_id, blind_review_id, reviewer_id, item_id, dimension),
  FOREIGN KEY (tenant_id, blind_review_id)
    REFERENCES blind_review (tenant_id, blind_review_id)
);

CREATE TABLE IF NOT EXISTS experiment (
  tenant_id            TEXT NOT NULL,
  experiment_id        TEXT NOT NULL,
  name                 TEXT NOT NULL,
  arms                 TEXT NOT NULL,
  planned_metrics      TEXT NOT NULL,
  assignment_rule      TEXT NOT NULL,
  state                TEXT NOT NULL CHECK (state IN
                       ('DRAFT','RUNNING','STOPPED','COMPLETED')),
  started_at           TEXT,
  stopped_at           TEXT,
  PRIMARY KEY (tenant_id, experiment_id)
);

CREATE TABLE IF NOT EXISTS experiment_assignment (
  tenant_id            TEXT NOT NULL,
  experiment_id        TEXT NOT NULL,
  subject_id           TEXT NOT NULL,
  arm                  TEXT NOT NULL,
  assigned_at          TEXT NOT NULL,
  PRIMARY KEY (tenant_id, experiment_id, subject_id)
);

CREATE TABLE IF NOT EXISTS cost_projection (
  tenant_id            TEXT NOT NULL,
  projection_id        TEXT NOT NULL,
  scope                TEXT NOT NULL,
  window               TEXT NOT NULL CHECK (window IN ('7d','30d')),
  actual_to_date_micro INTEGER NOT NULL CHECK (actual_to_date_micro >= 0),
  projected_total_micro INTEGER NOT NULL CHECK (projected_total_micro >= 0),
  ci_low_micro         INTEGER NOT NULL,
  ci_high_micro        INTEGER NOT NULL,
  based_on_rate        REAL NOT NULL,
  computed_at          TEXT NOT NULL,
  PRIMARY KEY (tenant_id, projection_id),
  CHECK (ci_low_micro <= ci_high_micro)
);

CREATE TABLE IF NOT EXISTS customer_roi (
  tenant_id            TEXT NOT NULL,
  roi_id               TEXT NOT NULL,
  contract_ref         TEXT NOT NULL,
  cohort_id            TEXT NOT NULL,
  revenue_micro        INTEGER,
  allocated_cost_micro INTEGER NOT NULL,
  gross_margin         REAL,
  as_of                TEXT NOT NULL,
  PRIMARY KEY (tenant_id, roi_id)
);

-- E28-S44-02：cohort 的 project_ids 必须属于同租户。S03 不在 depends_on
-- 里，归属从已消费的 S08 `snapshot.manifest.committed` 事件学习；没学过
-- 的 project 一律拒绝，而不是假设它属于本租户。
CREATE TABLE IF NOT EXISTS project_scope (
  tenant_id            TEXT NOT NULL,
  project_id           TEXT NOT NULL,
  event_id             TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, project_id)
);

-- 每个源最近一次消费到的事件水位。service.yaml 的 recovery 要求
-- 「可重放投影与 watermark」；没有这张表，重启后无法判断自己的水位。
-- 它只是**本地观测**，不是上游水位权威。
CREATE TABLE IF NOT EXISTS source_watermark (
  tenant_id            TEXT NOT NULL,
  source               TEXT NOT NULL,
  watermark            INTEGER NOT NULL,
  event_id             TEXT NOT NULL,
  observed_at          TEXT NOT NULL,
  PRIMARY KEY (tenant_id, source)
);

-- K1 的「被接受」来自 S08 事件。空表不是「接受数为 0」，而是「还没看到
-- S08 的事实」；计算 K1 时缺这一源会让快照失败，不填 0。
CREATE TABLE IF NOT EXISTS accepted_projection (
  tenant_id            TEXT NOT NULL,
  manifest_id          TEXT NOT NULL,
  node_id              TEXT NOT NULL,
  episode_id           TEXT,
  accepted_revision    INTEGER NOT NULL,
  event_id             TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, manifest_id, node_id)
);
"""

# 本轮不解决的缺口。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要
# 自行选边」，这里只登记处理方式，不替 owner 裁决。
CONTRACT_GAPS = [
    {"id": "S44-1", "kind": "needs_owner",
     "summary": "§11 C1：8 个 KPI 里的 K8 需要合同收入，但 48 个服务里没有"
                "合同/收入权威，`customer-configs/` 不是服务",
     "handling": "`revenue_micro` 只作外部输入，必须随请求显式给出并带 "
                 "`contract_ref`；缺它就是 422 REVENUE_REQUIRED，绝不填 0。"},
    {"id": "S44-2", "kind": "conflict",
     "summary": "§11 C2：OpenAPI 的 `/series/{id}/metrics` 只返回 series_id/"
                "cohort_id/projection_watermark/report_ref，无法表达 n、分位数"
                "与 CI；docs/15 §3 要求这些",
     "handling": "按方案 §6.1 的扩展响应实现 `GET /metrics`（含 data_as_of / "
                 "staleness / metrics[]）；契约面不同步属待办，不在本服务内改。"},
    {"id": "S44-3", "kind": "needs_owner",
     "summary": "§11 C3：docs/15 §3 列了 8 项，任务描述称『7 个 KPI』",
     "handling": "按 8 项实现（K1–K8），`METRIC_KEYS` 是闭集；是否删项由文档"
                 "owner 定。"},
    {"id": "S44-4", "kind": "needs_owner",
     "summary": "§11 C4：分摊规则由谁定义、谁批准未定；可能应由 S02 治理",
     "handling": "`AllocationPolicy` 目前归 S44 且版本化；迁移到 S02 属于后续"
                 "变更，本实现不假设任何外部 policy 服务存在。"},
    {"id": "S44-5", "kind": "needs_owner",
     "summary": "§11 C5：评测用 LLM 评判时的推理成本算生产还是评测；"
                "service.yaml 的 depends_on 也没有 S26",
     "handling": "`cost_type=evaluation` 一律 `basis=excluded`，不计入 K3；"
                 "评测执行是显式开关（默认关），本服务不自行发起付费生成。"},
    {"id": "S44-6", "kind": "conflict",
     "summary": "§11 C6：OpenAPI 只有一个 projection_watermark，实际有 S23/"
                "S26/S35/S40/S08/S43 六个源",
     "handling": "快照与查询都用 `source_watermarks`/`staleness` 分源表达；"
                 "单值字段保留为最慢源的值。"},
    {"id": "S44-7", "kind": "needs_owner",
     "summary": "§11 C7：K7『记忆正确性』的『证伪』判定归 S17/S40，S44 无权"
                "判定",
     "handling": "只统计 S17 标记冲突且 S40 确认的条数，不自行判定；分子"
                 "口径写进 MetricDefinition。"},
    {"id": "S44-8", "kind": "gap",
     "summary": "§11 C8：发布事件名与设计包不一致；消费侧四个名字在 "
                "`event-topics.v2.json` 里不存在。2026-11-05（`4f1bdcb`）已"
                "裁决，2026-11-06（`a172d89`）补齐 payload schema",
     "handling": "发布侧按已登记的三条 `analytics.*`（topic "
                 "`drama.evaluation-analytics.v2`）；消费侧用实际产出方的"
                 "事件名（`finance.ledger.entry-posted`、`generation.job."
                 "state-changed`、四条 QC 事件、`delivery.package.published`），"
                 "不订阅不存在的 `quality.qc.completed`。"},
    {"id": "S44-9", "kind": "gap",
     "summary": "§7 的快照天然键含 watermarks，但 §5 的 `metric_snapshot` 没有"
                "可进 UNIQUE 的列",
     "handling": "加 `watermarks_hash = sha256(JCS(source_watermarks))` 并进"
                 "唯一键；cohort 与 metric 版本仍在键里。"},
    {"id": "S44-10", "kind": "gap",
     "summary": "E28-S44-02 要求校验 cohort 的 project_ids 归属，K1 需要 S08 的"
                "接受事实，但 S08/S03 都不在 service.yaml 的 depends_on 里",
     "handling": "从已消费的 `snapshot.manifest.committed` 事件建逐租户的 "
                 "`project_scope` 与 `accepted_projection`；没有事实时拒绝"
                 "（cohort 归属无法验证 / K1 缺源），不假设为空。"},
    {"id": "S44-11", "kind": "needs_owner",
     "summary": "事件 schema 的 AMS-2：`stale_sources` 的键值形状在三处文档"
                "不一致，契约未定义",
     "handling": "本实现用 `{source_id: observed_watermark}`，只列落后于 "
                 "cohort.frozen_at 的源；不发明 `lag_seconds`。"},
    {"id": "S44-12", "kind": "needs_owner",
     "summary": "事件 schema 的 AMS-3：`low_confidence` 的阈值按指标族不同且"
                "不在契约里",
     "handling": "成本/耗时类 n<30、盲评维度 n<5 标低置信；事件只暴露布尔，"
                 "阈值随 MetricDefinition 版本走。"},
    {"id": "S44-13", "kind": "gap",
     "summary": "§6.5 要求导出 csv|xlsx，但仓内没有 xlsx 依赖，表格口径格式"
                "也未定义",
     "handling": "CSV 实现完整口径头（指标版本/cohort/时间窗/watermark/分摊"
                 "策略/n/CI/排除项）；xlsx 返回 422 EXPORT_FORMAT_UNSUPPORTED，"
                 "不生成一个伪装成 Excel 的 CSV。"},
    {"id": "S44-14", "kind": "gap",
     "summary": "§10 第 8 步的评测运行器要走 S21→S26→S23/S22，本服务的 "
                "depends_on 里没有 S26/S21/S22",
     "handling": "评测执行通过注入的 `evaluator_source`，并由 "
                 "`evaluation_execution_enabled`（默认 false）控制；未配置或"
                 "未打开一律 503，不自行发起付费生成（AGENTS.md 停止条件）。"},
    {"id": "S44-15", "kind": "gap",
     "summary": "§4.3 要『系统随机抽样 BenchmarkItem』并校验投票人，但 §5 的 "
                "`blind_review` 只有 item_count/reviewer_count，没有抽样集合、"
                "条目表或投票人清单",
     "handling": "加 `blind_review_item` 与 `blind_review.item_ids/reviewers`；"
                 "抽样用 `sha256(blind_review_id + item_id)` 排序（可复现），"
                 "投票必须落在本次会话的条目与投票人内。"},
    {"id": "S44-16", "kind": "gap",
     "summary": "§2.2 要求 cohort 可复现、§4.1 第 2 步要『写 member 集合』，"
                "但 §5 的 `cohort` 只有 member_count",
     "handling": "加 `cohort_member`；冻结成员集合从 S26 读面取，取不到就"
                 "503，不按 definition 现场重算（那样两次冻结会得到不同集合）。"},
    {"id": "S44-17", "kind": "gap",
     "summary": "§6.5 要求导出的表格『可被第三方复算』，但 §5 的 "
                "`metric_snapshot` 只有 `value` 与 `n`，没有分子分母",
     "handling": "加 `numerator_value`/`denominator_value`，存同一次计算的"
                 "中间量；导出头与每行都带它们。它们不是第二份权威 —— 原始"
                 "事实仍在 S23/S26/QC/S08。"},
]
