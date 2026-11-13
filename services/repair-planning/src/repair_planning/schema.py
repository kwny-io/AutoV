"""S39 自己的 schema。表来源是 `plans/S39-repair-planning.md` §7 与 §5.6。

五条结构性事实：

1. **S39 只出计划，不执行。** 没有 artifact 写入口、没有供应商调用入口、
   没有金额列。`estimated_cost` 是估计（§4.5 行 331），真正的钱在 S23
   reserve；本服务若记一份余额或"剩多少预算"就是第二权威（AGENTS.md）。
2. **`execution_fence` 与 `node_epoch` 是两列，永不合并。**
   `execution_fence` 属于 run/branch，`node_epoch` 属于单个 `logical_node`。
   两列分别写、分别读，**没有任何一处比较或换算**，也不由其中一列赋值给
   另一列。它们只用于给台账/计划打标，不是任何接受指针的依据——接受在 S08。
3. **计数器的键里没有 run / manifest / epoch / fence / 模型 / workflow**
   （§5.2）。键是 `(tenant_id, series_id, branch_id, shot_lineage_id,
   strategy_class)`。台账里那几个列（`run_id`、`manifest_id`、
   `node_epoch`、`execution_fence`）**只用于审计**，不进唯一键——进了就
   等于"换 Run 清零"。这是本域最容易做错的地方，所以键与归因列在 DDL 里
   分开放。
4. **唯一约束就是上限的执行者**（§5.6）。`attempt_ledger` 的唯一键含
   `attempt_seq`，`attempt_id` 另有唯一索引；并发两个计划同时要 seq=3 时
   数据库只放一个过。这比"先查再写"可靠。
5. **计划是确定性的**（§7.1）：`plan_fingerprint` 相同就返回已有计划。
   指纹含 `routing_algorithm_version`——算法改了得到的是**新**计划，不是冲突。

方案里的 `jsonb` 存 TEXT（JCS）、`timestamptz` 存定宽 UTC 字符串、
`uuid` 存 TEXT、`text[]` 存 JSON 数组。

与 §7 DDL 的偏差都在 `CONTRACT_GAPS` 里有条目，不自行选边。
"""

ID_PREFIX = {
    "plan": "rp_",
    "target": "tgt_",
    "action": "act_",
    "slot": "slot_",
    "ledger": "led_",
    "recheck": "rck_",
    "rejection": "rej_",
}

SOURCE = "S39"
SCHEMA_VERSION = "2.0"
EVENT_PLANNED = "quality.repair.planned"
TOPIC = "drama.repair-planning.v2"
# §9「发布 quality.repair.escalated（新增）」——原 catalog 只有 planned，
# 这条事件在 `contracts/event-topics.v2.json` 里还不存在（C1）。
EVENT_ESCALATED = "quality.repair.escalated"
TOPIC_ESCALATED = "drama.repair-planning.v2"

# 路由算法版本。**进 plan_fingerprint**（§7.1）：算法改了，同一缺陷得到
# 不同计划是合理的"新计划"，不是幂等冲突。
ROUTING_ALGORITHM_VERSION = "s39-routing.v1"

# inbox 的消费者名。不同来源分开，重投同一 event_id 不重复计数。
CONSUMER_GENERATION = "s39.generation-attempt.v1"
CONSUMER_REVIEW = "s39.review-decision.v1"

PLAN_STATES = (
    "planned", "planned_partial", "blocked_incomplete", "escalated",
    "human_resolved", "resolved", "superseded",
)

# §3.1.1 登记无效 / §3.3 人审 / §5.7 上限 / §5.9 死循环 / §4.3 冲突。
ESCALATION_REASONS = (
    "no_capable_route", "action_conflict", "registry_entry_invalid",
    "shot_attempt_cap", "identical_strategy_cap", "episode_attempt_cap",
    "run_cap_or_budget", "attempt_slot_taken", "no_progress",
    "coverage_gap", "exception_not_propagated", "missing_required_layer",
    "recheck_abstain", "recheck_rounds_exhausted",
    "human_review_requested", "manual_escalation",
    # §3.3 的 `no_auto_reason` 三值，直接沿用 registry 登记的理由。
    "creative_decision", "rights_authorization",
    "irreversible_or_cross_episode",
)

SEVERITIES = ("blocker", "major", "minor", "info")

DEFECT_TYPES = (
    "identity", "anatomy", "hands", "costume", "prop", "continuity",
    "camera_adherence", "action_adherence", "temporal", "subtitle",
    "lipsync", "voice", "technical", "audio_mix",
)

ACTIONS = (
    "reingest", "transcode", "keyframe_edit", "regenerate_video",
    "temporal_edit", "redo_lipsync", "realign_subtitles",
    "re_render_subtitles", "remix", "re_synthesize_speech", "human_review",
    "change_spec",
)

# §4.4 的打分权重。数据不是代码——但**方案没有把这张表放进 registry**，
# 所以它暂时是常量；改动要进 §4.4 而不是硬塞进某个 check 的登记项。
SEVERITY_WEIGHT = {"blocker": 1000, "major": 300, "minor": 80, "info": 10}
BLOCKING_WEIGHT = 500
RECHECK_COST = {"whole_film": 120, "continuous_window": 40, "single_frame": 5}
ACTION_COST = {
    "regenerate_video": 200, "redo_lipsync": 150, "remix": 60,
    "transcode": 30, "realign_subtitles": 20, "temporal_edit": 80,
    "keyframe_edit": 25, "reingest": 10,
}
# 方案 §4.4 没给这五个动作的代价。给 0 是**保守**：它们不会因为"便宜"被
# 排到前面，也不会被误当成免费。见 CONTRACT_GAPS C9。
ACTION_COST_UNDEFINED = {
    "re_render_subtitles": 0, "re_synthesize_speech": 0,
    "change_spec": 0, "human_review": 0,
}

# §5.4 Level 3/4 的缺省值。`automation-policy.v2` 的 `repair_limits` 只有
# shot 级两项（C2），读不到 episode/run 上限时用这两个数并记 policy_partial。
DEFAULT_MAX_ATTEMPTS_PER_EPISODE = 12
DEFAULT_MAX_ATTEMPTS_PER_RUN = 40

# §4.1 聚合窗口：默认 10 分钟。同步命令里 deadline 由调用方给的时刻决定，
# 但缺省值必须存在，否则"等待窗口"就没有终点。
DEFAULT_AGGREGATION_DEADLINE_SECONDS = 600

# §3.2 的时序能力要求。`max_window_frames` 由缺陷窗口帧数决定，另外两项固定。
REQUIRED_TEMPORAL_CAPABILITY = {
    "temporal_consistency": True,
    "preserves_identity": True,
}

# §3.1.2 第 1 条：`keyframe_edit` 只在 frame/window 且 ≤ 6 帧可用。
# 与 `contracts/quality/validate_registry.py` 的 `KEYFRAME_MAX_FRAMES` 同值。
KEYFRAME_MAX_FRAMES = 6

# 哪些层是聚合窗口的必需层（§4.1）。S38 只在有字幕时要求。
REQUIRED_LAYERS = ("technical", "semantic", "alignment")
OPTIONAL_LAYERS = ("subtitle",)
ALL_LAYERS = REQUIRED_LAYERS + OPTIONAL_LAYERS

# 层 → 产出该层的服务。只用于把"缺哪个报告"翻译成人能读的层名。
LAYER_OF_SERVICE = {
    "S35": "technical", "S36": "semantic", "S37": "alignment",
    "S38": "subtitle", "S17": "semantic",
}

# 这些是谁的表：idempotency_record 由 idempotency-sdk 建，outbox/inbox 由
# event-sdk 建。本 schema 不重复建它们（重复建会与 SDK 的列定义打架）。
SDK_TABLES = frozenset({"idempotency_record", "outbox", "inbox"})

# verdict / disposition 的合法值（§4.3.1）。`reported` 是缺省。
# 其余三值只在 S17 的连续性报告里出现（qc-report.v2 没有这个字段，见 C10）。
DISPOSITIONS = ("reported", "suppressed", "legal_exception", "not_reported")

SCHEMA = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS plan (
  plan_id              TEXT PRIMARY KEY,
  tenant_id            TEXT NOT NULL,
  series_id            TEXT NOT NULL,
  branch_id            TEXT NOT NULL,
  episode_id           TEXT NOT NULL,
  run_id               TEXT NOT NULL,
  manifest_id          TEXT NOT NULL,
  manifest_revision    INTEGER NOT NULL CHECK (manifest_revision >= 1),
  -- AGENTS.md：fence 是 run/branch 的，epoch 是单个 logical_node 的。
  -- 两列分别写、分别读；本服务没有任何一处比较或换算它们。
  execution_fence      INTEGER NOT NULL CHECK (execution_fence >= 1),
  node_epoch           INTEGER NOT NULL CHECK (node_epoch >= 1),
  artifact_sha256      TEXT NOT NULL,
  state                TEXT NOT NULL CHECK (state IN
    ('planned','planned_partial','blocked_incomplete','escalated',
     'human_resolved','resolved','superseded')),
  aggregation_deadline TEXT NOT NULL,
  policy_ref           TEXT NOT NULL,
  policy_version       TEXT NOT NULL,
  policy_partial       INTEGER NOT NULL DEFAULT 0 CHECK (policy_partial IN (0,1)),
  registry_version     INTEGER NOT NULL,
  routing_algorithm_version TEXT NOT NULL,
  -- §7.1 的确定性指纹；同指纹返回已有计划，不同即新计划。
  plan_fingerprint     TEXT NOT NULL,
  -- 同一组缺陷指纹的稳定摘要：§5.9 死循环判据只看"集合有没有变"。
  fingerprint_set      TEXT NOT NULL,
  snapshot             TEXT NOT NULL,
  escalation           TEXT,
  dry_run              INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  -- §4.1 的 `blocked_incomplete` 也是「不能继续自动处理，转人审」（E25-R1/R5），
  -- 它必须能带上 escalation 交接信息；原写法只允许 `escalated` 携带，表达不了。
  -- 见 CONTRACT_GAPS C24。两条 CHECK 合起来仍是：escalated 必须带交接信息，
  -- 其余状态只有 blocked_incomplete 允许带。
  CHECK (state <> 'escalated' OR escalation IS NOT NULL),
  CHECK (escalation IS NULL OR state IN ('escalated', 'blocked_incomplete'))
);
CREATE UNIQUE INDEX IF NOT EXISTS plan_fingerprint_uidx
  ON plan (tenant_id, plan_fingerprint, dry_run);
CREATE INDEX IF NOT EXISTS plan_scope_idx
  ON plan (tenant_id, branch_id, episode_id, created_at DESC);
CREATE INDEX IF NOT EXISTS plan_artifact_idx ON plan (artifact_sha256);
CREATE INDEX IF NOT EXISTS plan_fingerprintset_idx
  ON plan (tenant_id, fingerprint_set, created_at DESC);

CREATE TABLE IF NOT EXISTS action (
  plan_id         TEXT NOT NULL REFERENCES plan(plan_id),
  action_id       TEXT NOT NULL,
  sequence        INTEGER NOT NULL CHECK (sequence >= 1),
  action_type     TEXT NOT NULL,
  target_ids      TEXT NOT NULL,
  parameters      TEXT NOT NULL,
  capability_req  TEXT NOT NULL,
  recheck_scope   TEXT NOT NULL,
  attempt_slot_id TEXT,
  depends_on      TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (plan_id, action_id)
);
CREATE INDEX IF NOT EXISTS action_plan_idx ON action (plan_id, sequence);

-- §5.6：计数维度。唯一键里**没有** run/manifest/epoch/fence/model/defect。
CREATE TABLE IF NOT EXISTS attempt_ledger (
  ledger_id           TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  series_id           TEXT NOT NULL,
  branch_id           TEXT NOT NULL,
  episode_id          TEXT NOT NULL,
  shot_lineage_id     TEXT NOT NULL,
  strategy_class      TEXT NOT NULL,
  attempt_seq         INTEGER NOT NULL CHECK (attempt_seq >= 1),
  attempt_id          TEXT NOT NULL,
  provider_job_id     TEXT,
  -- 归因列：仅用于审计，**不进唯一键**。
  run_id              TEXT NOT NULL,
  manifest_id         TEXT NOT NULL,
  node_epoch          INTEGER NOT NULL CHECK (node_epoch >= 1),
  execution_fence     INTEGER NOT NULL CHECK (execution_fence >= 1),
  repair_plan_id      TEXT NOT NULL,
  action_id           TEXT NOT NULL,
  defect_fingerprints TEXT NOT NULL,
  outcome             TEXT NOT NULL CHECK (outcome IN
    ('pending','succeeded','failed','settled')),
  created_at          TEXT NOT NULL,
  settled_at          TEXT,
  UNIQUE (tenant_id, series_id, branch_id, shot_lineage_id,
          strategy_class, attempt_seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS attempt_ledger_attempt_uidx
  ON attempt_ledger (attempt_id);
CREATE INDEX IF NOT EXISTS attempt_ledger_shot_idx
  ON attempt_ledger (tenant_id, series_id, branch_id, shot_lineage_id,
                     created_at DESC);

CREATE TABLE IF NOT EXISTS attempt_reservation (
  reservation_id   TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  series_id        TEXT NOT NULL,
  branch_id        TEXT NOT NULL,
  shot_lineage_id  TEXT NOT NULL,
  strategy_class   TEXT NOT NULL,
  attempt_seq      INTEGER NOT NULL CHECK (attempt_seq >= 1),
  repair_plan_id   TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN
    ('reserved','committed','released')),
  expires_at       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  UNIQUE (tenant_id, series_id, branch_id, shot_lineage_id,
          strategy_class, attempt_seq)
);
CREATE INDEX IF NOT EXISTS attempt_reservation_plan_idx
  ON attempt_reservation (repair_plan_id);

CREATE TABLE IF NOT EXISTS recheck_job (
  recheck_id      TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  plan_id         TEXT NOT NULL,
  attempt_slot_id TEXT NOT NULL,
  artifact_sha256 TEXT NOT NULL,
  scope           TEXT NOT NULL,
  round           INTEGER NOT NULL CHECK (round >= 1),
  state           TEXT NOT NULL CHECK (state IN
    ('requested','running','completed','failed')),
  report_refs     TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recheck_job_plan_idx ON recheck_job (plan_id, round);

-- §7 没有拒收记录表（C11）。拒收是审计事实：P1 告警 + 不生成计划，
-- 只留在内存 metric 里的话重启就没了，审计无法追溯。
CREATE TABLE IF NOT EXISTS qc_rejection (
  rejection_id TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  service_id   TEXT NOT NULL,
  report_id    TEXT NOT NULL,
  layer        TEXT NOT NULL,
  reason       TEXT NOT NULL,
  detail       TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS qc_rejection_scope_idx
  ON qc_rejection (tenant_id, created_at DESC);
"""

# 本轮**不解决**的缺口。编号沿用方案 §11（C1–C8），C9 起是本实现新发现的。
# 按 AGENTS.md：契约与文档冲突 → 停下，报告冲突，不自行选边；这里只登记
# 处理方式，不冒充 owner 的决定。
CONTRACT_GAPS = [
    {"id": "C1", "kind": "gap",
     "summary": "`contracts/event-topics.v2.json` 没有 `quality.repair.escalated`，"
                "而 §9 要求 ESCALATE 独立成事件（要能被 S21/S40/S02 订阅，"
                "不能塞进 planned 的字段里）",
     "handling": "本服务按 §9 写 outbox（`EVENT_ESCALATED`），事件类型名先按方案；"
                 "catalog 补登前，下游的订阅白名单里没有它。不改 contracts。"},
    {"id": "C2", "kind": "gap",
     "summary": "`automation-policy.v2` 的 `repair_limits` 只有 "
                "`max_business_attempts_per_shot` 与 `max_identical_strategy_repeats`；"
                "§5.4 的 Level 3（每集）与 `max_recheck_rounds` 没有字段",
     "handling": "读不到时用 §5.4 的缺省值（episode=12、run=40、"
                 "recheck=shot+1），并在计划上记 `policy_partial=true`——"
                 "不静默忽略，也不自己造合并规则（合并归 S02）。"},
    {"id": "C3", "kind": "gap",
     "summary": "没有 `contracts/quality/repair-plan.v2.schema.json`；S21 要机器可读"
                "的计划契约，本服务只能按 §7/§8 的形状输出",
     "handling": "计划全文按 §7 + §8 输出；事件 payload 过现有 "
                 "`quality.repair-planned.v2.schema.json`。计划契约待 owner 补。"},
    {"id": "C4", "kind": "gap",
     "summary": "`shot_lineage_id` 在任何契约里都没有定义，而它是跨 Run 累计的锚点"
                "（§5.2 明确要求键里有它）",
     "handling": "本服务把它当**不透明字符串**：只从 qc-report 的 `context` 取、"
                 "只用于键与查询，不生成、不推断、不改写。生成规则与不可变性由"
                 "S06/S08 定。"},
    {"id": "C5", "kind": "gap",
     "summary": "数据文件 `contracts/quality/check-registry.v2.json` 仍未落；"
                "schema 与 `validate_registry.py` 已在，20+ 条登记项缺 "
                "`window_scope` / `escalation_rule` / `recheck_scope` 等字段",
     "handling": "registry 由 `CHECK_REGISTRY_PATH` 注入，加载期跑与 "
                 "`validate_registry.py` 同规则的族校验；未配置时命令 503，"
                 "**不内建一份「默认注册表」**——那等于 S39 替各服务登记检查。"},
    {"id": "C5b", "kind": "gap",
     "summary": "`action_family[type]` 的机器可读定义已落 "
                "`contracts/quality/action-families.v2.json`（commit 991ccee）",
     "handling": "本服务读文件、不硬编码；14 值闭集与族链以文件为准。"},
    {"id": "C6", "kind": "gap",
     "summary": "S21 没有「剩余修复额度」查询接口；§5.7 Level 4 的预检无处可查",
     "handling": "Level 4 只做**可选的读取预检**：注入了 run 状态源才查，"
                 "查不到时计划记 `run_precheck=unavailable` 并在 escalation 时"
                 "交给 S21 执行期兜底。**不伪造**「额度还够」。"},
    {"id": "C7", "kind": "gap",
     "summary": "`service.yaml` 的 `depends_on` 只有 S35/S36/S37/S09/S02，"
                "实际还依赖 S26（计数）、S23（预算预检）、S24/S25（能力）、"
                "S40（人审交接）、S17（ContinuityReport）",
     "handling": "deps 里按真实依赖建工厂，但不改 service.yaml（生成物）。"
                 "契约图缺口由 catalog owner 补。"},
    {"id": "C8", "kind": "gap",
     "summary": "S17 的 `ContinuityReport` 没有 schema，S39 也没有可订阅的"
                "事件名；跨集冲突按现状到不了修复决策",
     "handling": "本服务接受一份**按 qc-report.v2 形状**的 S17 文档"
                 "（`source_service_id=S17`），把它当私有适配；事件名待 owner"
                 "定。不宣称这已经是契约。"},
    {"id": "C9", "kind": "gap",
     "summary": "§4.4 的 `action_cost_penalty` 只给了 8 个动作；"
                "`re_render_subtitles`、`re_synthesize_speech`、`change_spec`、"
                "`human_review` 没有代价",
     "handling": "这四个按 0 计（保守：不会因「便宜」被提前），常量在 "
                 "`ACTION_COST_UNDEFINED`；等 §4.4 补齐再改。"},
    {"id": "C10", "kind": "gap",
     "summary": "§4.3.1 要用 defect 的 `disposition`，但 `qc-report.v2` 的 "
                "defect 是 `additionalProperties:false`，没有这个字段——"
                "带 disposition 的报告过不了自己的 schema",
     "handling": "只对 S17 文档接受 `disposition`；校验时把它剥掉再跑 "
                 "qc-report.v2（形状其余部分照验）。S35–S38 的报告带这个字段"
                 "一律 quarantine。字段与事件名一并待 owner 落契约。"},
    {"id": "C11", "kind": "gap",
     "summary": "§7 没有拒收记录表；§4.2 要求 quarantine 且 P1 告警，"
                "但没说拒收事实落在哪",
     "handling": "新增 `qc_rejection` 表只存拒收事实（服务/报告/层/理由/详情），"
                 "**不存报告正文**——S39 不是报告的第二个家（§7 行 720）。"},
    {"id": "C12", "kind": "conflict",
     "summary": "§7 的 `plan` DDL 只有 `execution_fence`，没有 `node_epoch`；"
                "但命令体带完整 execution-context，两个游标都要能审计，且 "
                "AGENTS.md 要求它们分别落列",
     "handling": "`plan` 与 `attempt_ledger` 都加 `node_epoch` 列，与 "
                 "`execution_fence` 并列存放、互不换算。多出来的列不参与任何"
                 "判定或唯一键。"},
    {"id": "C13", "kind": "conflict",
     "summary": "§7 的 `plan` DDL 没有 `plan_fingerprint`，而 §7.1 要求"
                "同指纹返回已有计划、不同即新计划",
     "handling": "加 `plan_fingerprint` 与 `fingerprint_set` 两列，并加唯一索引 "
                 "`(tenant_id, plan_fingerprint, dry_run)`——dry_run 计划不能"
                 "顶掉真实计划。"},
    {"id": "C14", "kind": "gap",
     "summary": "§5.5 的 `REPAIR_ATTEMPT_STARTED` 要从 S26 的 "
                "`generation.job.state-changed` 推出，但该事件不带 "
                "`repair_plan_id` / `attempt_slot_id`，台账无法把一次 attempt "
                "关联回哪张计划",
     "handling": "消费者接受事件 data 里的可选 `repair_plan_id` / "
                 "`attempt_slot_id` / `strategy_class`，缺失时按 attempt_id 落"
                 "一条「未归属」台账并记 metric。事件加不加这三个键由 S26 owner 定。"},
    {"id": "C15", "kind": "gap",
     "summary": "`SUBMISSION_UNKNOWN` 之后「确认已提交/未提交」在 "
                "`generation.job.state-changed` 里没有专门的 `reason_code` 闭集"
                "（该字段在契约里明确写着「未收窄为 enum」）",
     "handling": "本服务按约定读 `reason_code` 的 "
                 "`CONFIRMED_SUBMITTED` / `CONFIRMED_NOT_SUBMITTED`；读不到时"
                 "保持 pending，既不 +1 也不放行重提（AGENTS.md：不盲目重提）。"},
    {"id": "C16", "kind": "gap",
     "summary": "`quality.repair-planned.v2` 的 `shot_lineage_id` 是单值，"
                "而一张计划可以覆盖同一 artifact 的多个 target；报告里若出现"
                "多个 lineage，事件无法表达",
     "handling": "一张计划只接受一个 shot_lineage_id；出现多个时 422 "
                 "`MULTIPLE_SHOT_LINEAGES`，不静默取第一个。"},
    {"id": "C17", "kind": "conflict",
     "summary": "`platform/idempotency-sdk` 的 `idempotency_record` 只有 "
                "`(tenant_id, command_id, request_hash, result, created_at)`，"
                "而 §7 抄了 S08 的五列形状（含 `state` / `response_code` / "
                "`response_body`）",
     "handling": "用 SDK 的表（平台唯一实现），命令幂等的语义不变："
                 "同键同 body 重放、同键异 body 409。计划响应体存 `result`。"},
    {"id": "C18", "kind": "gap",
     "summary": "recheck 报告如何回到本服务没有定义入口：S35–S38 的复检报告"
                "带 `repair_context`，但没有「这是一次复检」的事件",
     "handling": "提供 `POST /repair-plans/{id}:recheck` 投递复检报告"
                 "（由编排层调用）；报告必须带 `repair_context`，否则按新报告"
                 "走 ingest。"},
    {"id": "C19", "kind": "gap",
     "summary": "§3.2 的时序能力要求是 "
                "`{temporal_consistency, max_window_frames, preserves_identity}`，"
                "但 S24 的能力词表里没有这三项——"
                "`model-candidates.required_capabilities` 用的是它自己的词表，"
                "`max_window_frames` 更是一个数值门槛而不是能力名",
     "handling": "只把词表里真实存在的项发出去（`temporal_consistency` 在 "
                 "`services/lipsync/src/lipsync/deps.py` 的 profile 里出现过）；"
                 "`max_window_frames` **不发**，改在结果回收后按候选声明的窗口"
                 "上界自行判定，判定不出一律当作不满足。有新能力词表前，"
                 "时序类动作会偏保守地降级。不改 contracts。"},
    {"id": "C20", "kind": "gap",
     "summary": "方案假设 `GET /qc-reports/{id}` 这个统一读面存在，实际没有："
                "S35 是 `/technical-reports/{id}`、S36 是 `/semantic-reports/{id}`、"
                "S37 是 `/alignment-reports/{id}`；且响应外壳不统一——"
                "S35/S37 把报告放在 `payload`，S36 放在 `report`；"
                "S36 的读面还没有 `generation` 参数，只能取最新一代",
     "handling": "`deps.REPORT_PATHS` 按服务分表，逐层摘取；S36 无 revision 参数"
                 "是它自己的形状，不在这里补。取不到声明的那一代时"
                 "**不降级成「读当前」**，直接失败（读当前会把已 supersede 的"
                 "结论当成输入）。统一读面与统一外壳待 owner 定。"},
    {"id": "C21", "kind": "gap",
     "summary": "S38 的 `api/` 目录还是空的，没有字幕报告的读面；"
                "但 §4.1 把 subtitle 列为可选的聚合层、§6 的复检也要它",
     "handling": "`deps.REPORT_PATHS` 里的 S38 项按 S35/S37 的命名预留，但 "
                 "`S38_BASE_URL` 现在应保持未配置；未配置时该层缺席，"
                 "覆盖断言按缺层处理并登记到 `pending_layers`。实现落地后只需"
                 "配置 base URL，不改本服务代码（除非路径与预留不符）。"},
    {"id": "C22", "kind": "gap",
     "summary": "§4.2 第 (f) 步要求用「共享的 `decide()`」复核报告结论，"
                "但三个质检服务各自写了一份：S35 是"
                "`technical_qc/evaluation.py` 里收 `policy` 字典的函数，"
                "S36 是返回四元组的方法，S37 是模块级函数且阈值不同"
                "（`TAU_LOW=0.40`）",
     "handling": "本服务按**层无关**的方式重算同一套优先级阶梯（报告字段里"
                 "已有的 `decision` / `defect_count` / `coverage` / "
                 "`check_manifest` 就够），不复用任何一家的实现——import 别家"
                 "的业务模块违反 AGENTS.md。三份实现是否等价无法由本服务证明，"
                 "只登记为缺口，不改口说「共享」。",
     "risk": "重算的结果可能与生产方自带的 decision 不一致；不一致时本服务"
             "**拒收**（validator f），不会两边各说一套。"},
    {"id": "C23", "kind": "gap",
     "summary": "S17 的 `continuity.check.completed` 的严重度用 "
                "`blocking`/`medium` 等 6 值词表，S39 的 `severity` 是 "
                "`blocker`/`major`/`minor`/`info` 4 值；"
                "且它的 `check_id` 不在 `(TECH|VIS|AUD|SUB)` 注册表命名空间里",
     "handling": "不接受 S17 原生事件（C8 已说它没有 schema）：只接受"
                 "**按 qc-report.v2 形状适配过的** S17 文档，严重度由适配方"
                 "映射。本服务不做 `blocking→blocker` 这类隐式映射——"
                 "映射错了会把「阻断」降成「主要」，静默改变修复优先级。"},
    {"id": "C24", "kind": "conflict",
     "summary": "§7 的 `plan` DDL 只有 `escalated` 允许携带 `escalation`，"
                "但 §4.1 的 `blocked_incomplete` 同样是「缺必需层 → 转人审」"
                "（E25-R1 / E25-R5），也必须能带交接信息（缺哪层、交谁）",
     "handling": "把约束放宽为「`escalated` 必须带，`blocked_incomplete` 可以带，"
                 "其余状态必须不带」。不新增状态、不改状态机的语义。"},
    {"id": "C25", "kind": "gap",
     "summary": "§4.2 第 (b) 步要求「从派生键重算 report_id」，但 "
                "`qc-report.v2` 里没有派生键的材料：S37 的派生键要 "
                "`audio_track_hash` + `language`，S38 的要 `track_id` + "
                "`subtitle_revision` + `attempt_key`，两样都不在报告里",
     "handling": "只对 technical / semantic 两层重算（它们的派生材料在报告内）；"
                 "alignment / subtitle 两层改用「`content_hash` + "
                 "`report_generation` 与投递引用一致」来钉住同一代结论——"
                 "读不到的 generation 直接失败，**不降级成读当前**（同 C20）。"
                 "派生键材料待 owner 补进报告契约或另给读面。"},
    {"id": "C26", "kind": "conflict",
     "summary": "§4.2 第 (i) 步把「check_id 不存在 / 登记项无效」列为**拒收**"
                "（quarantine），而 §3.1.1 与 E24-R14/R15/R16 要求登记无效时"
                "**运行期路由到 human_review + 告警**（`registry_entry_invalid_total`），"
                 "不生成任何动作",
     "handling": "按验收用例实现：登记项本身无效是**路由层**的人审，不是报告的"
                 "错；ingest 拒收只留给「报告自身不自洽」（a–h、j）。理由："
                 "报告是合法的，坏的是登记表——拒收报告等于让上游为别人的错误"
                 "重跑。E24-R14 的 metric 名写 `registry_entry_invalid_total{check_id}`，"
                 "本服务按 `registry_entry_invalid_total|<check_id>` 打点。"},
    {"id": "C27", "kind": "gap",
     "summary": "§9 的订阅表列了 `quality.technical.completed` / "
                "`quality.semantic.completed` / `quality.alignment.completed` / "
                "`post.subtitle.ready`，但没说这些事件如何映射成 "
                "`POST /repair-plans` 的 `qc_snapshot`（层 → 报告引用、artefact "
                "hash 从哪来、四层如何聚合到一个快照），而四个 QC 服务的读面"
                "路径与外壳各不相同（C20/C21）",
     "handling": "本服务的 ingest 是**命令驱动**：出计划只走显式的 "
                 "`POST /repair-plans` 快照，`/events:consume` 对上述四类事件"
                 "返回 422 `UNSUPPORTED_EVENT_TYPE`（不假装已 ingest）。只消费"
                 "`generation.job.state-changed`（计数）与 "
                 "`review.decision.recorded`（人审回填）。聚合编排由 S21 在重投"
                 "命令前完成——它才是知道「四层到齐没有」的那一层。映射规则"
                 "待 owner 在契约里给出。"},
    {"id": "C28", "kind": "gap",
     "summary": "`execution-context.v2` 的 18 个键里**没有 `run_id`**"
                "（`additionalProperties:false`），而 §5.7 Level 4 要按 "
                "`run_id` 查 S21、§9 的事件 payload 与 `plan`/`attempt_ledger` "
                "的列都要它；`shot_lineage_id` 同样不在里面（C4 已登记其定义缺口）",
     "handling": "与 S37/S38 同一约定：两个坐标放在**命令体顶层**"
                 "（`body.run_id` 与 `body.shot_lineage_id`），"
                 "`execution_context` 只按 `execution-context.v2` 原样校验。"
                 "校验通过后**在内存里**把这两个值并入 context 供本服务使用——"
                 "线缆上的 body 不变，也不把非契约字段塞进被签名的上下文。"
                 "缺 `run_id` 时 422 `RUN_UNKNOWN`，缺 `shot_lineage_id` 时 422 "
                 "`SHOT_LINEAGE_UNKNOWN`（C16），都不静默用 "
                 "`manifest_ref.id` 之类的近似值顶替。",
     "risk": "若 owner 之后把 `run_id` 加进 execution-context v2，"
             "本服务的顶层读取仍兼容（context 优先），但需同步去掉这条缺口。"},
]
