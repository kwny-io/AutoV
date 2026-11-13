"""S19 自己的 schema。表来源是 `plans/S19-agent-runtime.md` §5。

四条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **`agent_task` 没有业务外键，也没有余额列。** 它不记余额（归 S23）、不记
   配额（归 S22）、不记 accepted pointer（归 S08）。`cost_micro` 只是本任务的
   熔断计数（§1.6），不是账本。
2. **定义 revision 不可变。** 触发器拒绝 UPDATE/DELETE，与 S06 的镜头内容表
   同一条纪律：要改就发新 revision。否则一个在途任务的
   `definition_content_hash` 会在它跑的时候悄悄变（§3.8 不变量 7）。
3. **`correction_once` 是"每轮纠正只允许一次"的数据库级保证。** 应用层的
   "本轮已经纠正过"检查会被并发绕过，唯一索引不会（§6.3）。
4. **终态才写 outbox。** 任务创建不是业务事件（§6.1 第 4 步）；`outbox` 行与
   `state` 推终态必须在同一个 `COMMIT` 里（§6.5）。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`text[]` 存 JSON
数组文本。`boolean` 存 INTEGER 0/1（SQLite 无原生布尔）。
"""

ID_PREFIX = {
    "definition": "def_",
    "task": "agt_",
    "result": "res_",
}

# 13 个角色。唯一另外两处登记：`docs/08-agents-prompts.md` 行 5（散文）与
# `tools/scaffold/generate_agents.py` 的 ROLES（生成器脚本）。契约没有登记它，
# 见 C7 / ATC-4——这里硬编码不是"角色权威"，而是 DDL 需要一个闭集才能让
# `UNIQUE (tenant_id, role)` 有意义；名册升格为契约前，改这里必须同步那两处。
ROLES = (
    "producer", "writer", "breakdown", "storyboard-director", "asset-curator",
    "memory-extractor", "continuity-reviewer", "prompt-director", "qc-reviewer",
    "repair-planner", "editor", "sound-director", "conversation-editor",
)

# S01 的 `SCOPE_RANK` 秩的是 `scope_kind`（tenant→episode）。方案 §3.2 行 294
# 拿它去比 `required_scope`（read/plan/execute/admin），这是两套词表，见
# S19-11。这里按各自的词表分开校验：
SCOPE_KINDS = ("tenant", "org_unit", "project", "series", "episode")
SCOPE_RANK = {"tenant": 0, "org_unit": 1, "project": 2, "series": 3, "episode": 4}
REQUIRED_SCOPES = ("read", "plan", "execute", "admin")
EGRESS_MODES = ("internal", "external_allowlisted")

# 角色允许的最大 `scope_kind`。方案写的是 `s01.role_max_scope(defn.role)`，
# 但 S01 没有这个面（见 S19-11），这里给出一份显式表并登记为待 owner 确认。
# 取 series 而不是 episode：`drama.memory.query` 的合法范围就是 series。
ROLE_MAX_SCOPE = {role: "series" for role in ROLES}

PERSPECTIVES = ("omniscient", "character", "audience")
ON_UNRESOLVED = ("fail", "review")
TERMINAL_STATES = ("SUCCEEDED", "FAILED", "NEEDS_REVIEW", "CANCELLED")
ALL_STATES = ("ACCEPTED", "RUNNING", "CANCELLING") + TERMINAL_STATES

# §4.1 行 489 的 12 个值。FAILED 家族是其中九个（除 GOAL_REACHED /
# USER_CANCELLED / UNRESOLVED_CONFLICTS 之外的九个里包含 STRUCTURE_INVALID 等）。
FAILED_REASONS = (
    "STRUCTURE_INVALID", "STEP_LIMIT", "TOKEN_LIMIT", "TIME_LIMIT",
    "COST_LIMIT", "POLICY_DENIED", "GENERATION_FAILED", "GENERATION_UNKNOWN",
    "TOOL_DENIED_REPEATEDLY",
)
STOP_REASONS = ("GOAL_REACHED", "USER_CANCELLED", "UNRESOLVED_CONFLICTS") \
    + FAILED_REASONS

# 平台工具注册表的**临时**替身。`contracts/mcp-tools.v2.design.json` 只给了
# 12 个名字与 `required_scope`，没有 `permission_key`（C7），所以这里只收录
# 名字用来做"工具名是否在平台注册表"的校验；`permission_key` 必须由定义作者
# 在发布时给出，并由 S01 的权限注册表核对。见 S19-12。
TOOL_NAMES = (
    "drama.memory.query", "drama.context.preview", "drama.policy.get",
    "drama.policy.change", "drama.episode.generate", "drama.change.plan",
    "drama.change.apply", "drama.run.get", "drama.run.command",
    "drama.review.decide", "drama.delivery.prepare", "drama.delivery.publish",
)

# §4.2 行 520：`proposal` 里出现即视为输出污染。
FORBIDDEN_OUTPUT_KEYS = frozenset({
    "tenant_id", "project_id", "series_id", "branch_id",
    "scene_id", "shot_id", "breakdown_id",
})

# §3.5：四道闸的字段名。
LIMIT_FIELDS = ("max_steps", "max_tokens", "max_wall_seconds", "max_cost_micro")

# 平台硬顶。方案 §3.2 行 296 的 `HARD_MAX_STEPS[role]` 在仓库里不存在；
# 取一个统一值并登记，防止单角色被配成无界。
HARD_MAX_STEPS = 200


# 本轮**不解决**的缺口。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要
# 自行选边」：本服务按方案散文实现，把偏差记在这里，不修改契约、不代填决定。
CONTRACT_GAPS = [
    # --- 方案 §11 已登记的 ---
    {"id": "C1", "kind": "conflict",
     "summary": "§11 C1 说 agent.task.completed 没有 payload schema；实际上 "
                "contracts/agent.task-completed.v2.schema.json 已经存在且与本"
                "服务的 data 字段集逐字一致",
     "handling": "按该 payload schema 发事件；§11 C1 可视为已被契约补齐，"
                 "不再是 S19 的阻塞项。"},
    {"id": "C2", "kind": "needs_owner",
     "summary": "proposal 正文落受限存储，其保留期/地域策略没有契约；"
                "data_ref 的可读范围未定",
     "handling": "事件只带 data_ref，不带正文；正文存储由调用方与对象存储约定。"
                 "本服务不实现保留期。"},
    {"id": "C3", "kind": "conflict",
     "summary": "catalog 只声明 POST /agent-tasks，缺 :cancel、"
                "/agent-definitions、/agent-definitions/{id}/revisions、"
                "/agent-tasks/{id}/result、/agent-tasks/{id}/steps；"
                "contracts/openapi.v2.yaml 里一个 S19 路径都没有",
     "handling": "按方案实现上述路径；不动 catalog、不新增 openapi 文件，"
                 "等 owner 决定契约面。"},
    {"id": "C4", "kind": "conflict",
     "summary": "准入需要 POST /policy-decisions:evaluate（kind="
                "agent_task_admission），S02 只有 :resolve-effective；"
                "kind 枚举里也没有 agent_task_admission",
     "handling": "准入走 :resolve-effective：只核验 pin 的 policy_ref 仍是当前版"
                 "且企业策略存在。**没有实现「策略拒绝任务」这条分支**，"
                 "stop_reason=POLICY_DENIED 在真实上游下不可达。"},
    {"id": "C5", "kind": "needs_owner",
     "summary": "Agent 推理费用该按哪个 budget purpose 报没有机器可读答案："
                "S23 无 purpose 字段，S26 用 generation",
     "handling": "按 §11 C5 的临时约定：预算一律按 generation 报，在 "
                 "budget_scope_id 上区分（bsc_ep3_agent）。不是最终形态。"},
    {"id": "C6", "kind": "gap",
     "summary": "outbox.aggregate_id 恒为 task_id，取决于本服务只发一种事件",
     "handling": "只在 outbox 写 agent.task.completed；新增以 definition_id 为"
                 "聚合的事件必须加 aggregate_kind 列。"},
    {"id": "C7", "kind": "conflict",
     "summary": "contracts/mcp-tools.v2.design.json 的 12 个工具没有 "
                "permission_key，也没有角色→工具的可读表；§3.2 的定义期校验"
                "依赖 TOOL_REGISTRY",
     "handling": "TOOL_NAMES 只用于「名字是否注册」；permission_key 由定义作者"
                 "给出并在发布时对 S01 的 permission-registry 核对。"},
    {"id": "C8", "kind": "needs_owner",
     "summary": "execution-context 把 actor.delegation_id 列为 required，但 "
                "S19 内部恢复没有代理关系",
     "handling": "按契约要求必填，不自行放宽；内部恢复用发起方带来的原值。"},
    {"id": "C9", "kind": "gap",
     "summary": "13 份 agents/*/agent.yaml 的 limits 全是 unassigned、"
                "tools/evals 为空；§3.2 的「没有评测集不许发布」会让所有角色"
                "都发不出去",
     "handling": "定义发布要求 eval_set_ref 非空，因此**不预置任何角色定义**；"
                 "由 owner 填出至少 writer/breakdown/storyboard-director。"},
    {"id": "C10", "kind": "needs_owner",
     "summary": "镜像版本绑定属于 AgentDefinitionRevision 还是 Deployment",
     "handling": "按方案假定属于 Deployment，不进 content_hash，也不存。"},

    # --- 实现时新发现的 ---
    {"id": "S19-11", "kind": "conflict",
     "summary": "§3.3 假定 S01 返回 verdict.results[0].verdict 与 reason_code，"
                "真实 S01 返回 [{permission_key, allowed, reason, decision_id}]；"
                "§3.2 还用 SCOPE_RANK 比 required_scope，而 SCOPE_RANK 秩的是 "
                "scope_kind，两套词表",
     "handling": "按真实的 allowed/reason 判定；`scope_kind` 用 SCOPE_RANK，"
                 "`required_scope` 用 REQUIRED_SCOPES 单独校验；"
                 "`role_max_scope` 用 ROLE_MAX_SCOPE 临时表（待 owner）。"},
    {"id": "S19-12", "kind": "conflict",
     "summary": "S26 没有「提交前询价」接口；§3.5 的 COST_LIMIT 闸要求用报价"
                "上界预判，而 amount_conservative_micro 是 submit 之后才出现的",
     "handling": "quote 做成必需注入源；未配置即 503 GENERATION_UNAVAILABLE"
                 "（任务 FAILED(GENERATION_FAILED)）。真实 S26 下这条闸走不到，"
                 "**不能声称 COST_LIMIT 已端到端验证**。"},
    {"id": "S19-13", "kind": "conflict",
     "summary": "event_sdk 的 EventEnvelope.data_ref 是 str|None，§4.3 的"
                "data_ref 是 {service_id,id,revision,content_hash} 对象",
     "handling": "与 model-routing 一致传对象；envelope 不做类型校验，"
                 "序列化后是对象。契约侧待统一。"},
    {"id": "S19-14", "kind": "conflict",
     "summary": "§2.4 说「回读 pack 校验 perspective 与 watermark」，S18 的"
                "pack 字段实际是标量 perspective、checkpoint.commit_sequence 与 "
                "source_watermark；且 S18 的 task_role 只有 7 个值，S19 有 13"
                "个角色，没有映射表",
     "handling": "perspective 与 pack.perspective 比对；新鲜度用 "
                 "source_watermark < execution_context.memory_checkpoint."
                 "commit_sequence 判 CONTEXT_PACK_STALE。角色→task_role 不做映射"
                 "（会自己发明一张表）。"},
    {"id": "S19-15", "kind": "conflict",
     "summary": "catalog 的 S19 depends_on 只有 [S02, S26]，实现还依赖 S01、"
                "S18、S20（模板）与 proposal schema 源",
     "handling": "不擅自改 catalog.depends_on（那是契约）；未配置的依赖一律"
                 "503，不降级、不套默认值。"},
    {"id": "S19-16", "kind": "gap",
     "summary": "`output_schema_ref` 只有 {id, revision, content_hash}，没有"
                "schema 正文；仓库里也没有 proposal schema 注册表，§3.1 的"
                "「输出结构校验（JSON Schema）」无从执行",
     "handling": "schema_source 注入；未配置或取不到正文时任务"
                 "FAILED(STRUCTURE_INVALID)——未知不能当通过（E25）。"
                 "校验器是 stdlib 子集实现（type/required/properties/"
                 "additionalProperties/enum/items/minimum/maximum），"
                 "运行期不 import jsonschema。"},
    {"id": "S19-17", "kind": "conflict",
     "summary": "§2.7 没有「纠正换了 definition_revision」的错误码，而 T19-11 "
                "要求 422",
     "handling": "用 422 CORRECTION_DEFINITION_MISMATCH；码表待 owner 补。"},
    {"id": "S19-18", "kind": "conflict",
     "summary": "§5.7 的 outbox DDL 有 aggregate_sequence / partition_key / "
                "headers 三列，event_sdk 的 OUTBOX_DDL 只有 event_id 主键与 "
                "sequence",
     "handling": "用 SDK 的 outbox（与其它服务一致）；同聚合不重号由"
                 "agent_task.publish_sequence 保证，不靠 outbox 唯一约束。"},
    {"id": "S19-19", "kind": "gap",
     "summary": "§5.7 的 idempotency_record 有 response_code / response_body / "
                "expires_at（30 天），SDK 只有 result TEXT、无过期",
     "handling": "用 SDK；30 天过期与按命令类型分列未实现，"
                 "清理策略待 owner。"},
    {"id": "S19-20", "kind": "needs_owner",
     "summary": "ATC-2 问 NEEDS_REVIEW 到底有没有 task_result 行",
     "handling": "本服务写一行 task_result（带 unresolved_issues），因此"
                 "NEEDS_REVIEW 的 result_ref 非空、unresolved_issues 可回查；"
                 "与 task_result_once（只在 SUCCEEDED 上）不冲突。"},
    {"id": "S19-21", "kind": "conflict",
     "summary": "contracts/agent.task-completed.v2.schema.json 的 ATC-5 说"
                "state 与 stop_reason 的配对表待 owner 定",
     "handling": "本服务的 CHECK 只写 §3.6/§6.4 隐含的四条配对"
                 "（SUCCEEDED→GOAL_REACHED、NEEDS_REVIEW→UNRESOLVED_CONFLICTS、"
                 "CANCELLED→USER_CANCELLED、FAILED→九个之一）。owner 放宽时"
                 "必须同步改这条 CHECK，否则合法值会被库拒绝。"},
    {"id": "S19-22", "kind": "conflict",
     "summary": "§7.2 有两行都写「结构非法 → FAILED(STRUCTURE_INVALID)」，"
                "但只有「结果含禁止字段」那行同时要求 422，另一行没有错误码；"
                "而 T19-16 写的是 422 + 任务 FAILED，与「任务停在 RUNNING」的"
                "读法冲突",
     "handling": "两种非法都让任务进 FAILED(STRUCTURE_INVALID)，再区成交付："
                 "禁止字段抛 422 OUTPUT_CONTAINS_FORBIDDEN_FIELD（§7.2 最后"
                 "一行明写）；纯 schema 违规没有码，走 `structure_error` 字段"
                 "返回。终态先落库再抛，避免异常把 FAILED 一起回滚。错误码表"
                 "（§2.7）待 owner 补一条。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 定义容器 ----------
CREATE TABLE IF NOT EXISTS agent_definition (
  tenant_id        TEXT NOT NULL,
  definition_id    TEXT NOT NULL,
  role             TEXT NOT NULL,
  current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
  state            TEXT NOT NULL CHECK (state IN ('ACTIVE', 'RETIRED')),
  created_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, definition_id),
  -- §5.1：一个角色只能有一个定义容器。
  UNIQUE (tenant_id, role)
);

-- ---------- 定义 revision（不可变） ----------
CREATE TABLE IF NOT EXISTS agent_definition_revision (
  tenant_id             TEXT NOT NULL,
  definition_id         TEXT NOT NULL,
  revision              INTEGER NOT NULL CHECK (revision >= 1),
  role                  TEXT NOT NULL,
  system_template_ref   TEXT NOT NULL,
  output_schema_ref     TEXT NOT NULL,
  required_capabilities TEXT NOT NULL DEFAULT '[]',
  limits_json           TEXT NOT NULL,
  required_perspective  TEXT NOT NULL
                          CHECK (required_perspective IN
                                 ('omniscient', 'character', 'audience')),
  on_unresolved         TEXT NOT NULL CHECK (on_unresolved IN ('fail', 'review')),
  allow_schema_override INTEGER NOT NULL DEFAULT 0
                          CHECK (allow_schema_override IN (0, 1)),
  eval_set_ref          TEXT NOT NULL,
  content_hash          TEXT NOT NULL CHECK (length(content_hash) = 64),
  published_by          TEXT NOT NULL,
  published_at          TEXT NOT NULL,
  PRIMARY KEY (tenant_id, definition_id, revision),
  FOREIGN KEY (tenant_id, definition_id)
    REFERENCES agent_definition (tenant_id, definition_id)
);

-- §5.1 行 619：定义 revision 不可变。要改就发新 revision。
CREATE TRIGGER IF NOT EXISTS agent_definition_revision_no_update
BEFORE UPDATE ON agent_definition_revision
BEGIN
  SELECT RAISE(ABORT, 'agent_definition_revision is immutable');
END;

CREATE TRIGGER IF NOT EXISTS agent_definition_revision_no_delete
BEFORE DELETE ON agent_definition_revision
BEGIN
  SELECT RAISE(ABORT, 'agent_definition_revision is immutable');
END;

-- ---------- 工具白名单（随 revision 一起不可变） ----------
CREATE TABLE IF NOT EXISTS agent_definition_tool (
  tenant_id      TEXT NOT NULL,
  definition_id  TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  tool_name      TEXT NOT NULL,
  permission_key TEXT NOT NULL,
  scope_kind     TEXT NOT NULL CHECK (scope_kind IN
                   ('tenant', 'org_unit', 'project', 'series', 'episode')),
  required_scope TEXT NOT NULL CHECK (required_scope IN
                   ('read', 'plan', 'execute', 'admin')),
  egress         TEXT NOT NULL CHECK (egress IN
                   ('internal', 'external_allowlisted')),
  max_calls      INTEGER NOT NULL CHECK (max_calls > 0),
  PRIMARY KEY (tenant_id, definition_id, revision, tool_name),
  FOREIGN KEY (tenant_id, definition_id, revision)
    REFERENCES agent_definition_revision (tenant_id, definition_id, revision)
);

CREATE TRIGGER IF NOT EXISTS agent_definition_tool_no_update
BEFORE UPDATE ON agent_definition_tool
BEGIN
  SELECT RAISE(ABORT, 'agent_definition_tool is immutable');
END;

CREATE TRIGGER IF NOT EXISTS agent_definition_tool_no_delete
BEFORE DELETE ON agent_definition_tool
BEGIN
  SELECT RAISE(ABORT, 'agent_definition_tool is immutable');
END;

-- ---------- 任务 ----------
CREATE TABLE IF NOT EXISTS agent_task (
  tenant_id               TEXT NOT NULL,
  task_id                 TEXT NOT NULL,
  role                    TEXT NOT NULL,
  definition_id           TEXT NOT NULL,
  definition_revision     INTEGER NOT NULL,
  -- §3.8 不变量 7：创建时 pin 死，scheduler 不得"顺手升级到最新版"。
  definition_content_hash TEXT NOT NULL CHECK (length(definition_content_hash) = 64),
  root_task_id            TEXT NOT NULL,
  correction_of_task_id   TEXT,
  correction_round        INTEGER NOT NULL DEFAULT 0
                            CHECK (correction_round BETWEEN 0 AND 2),
  state                   TEXT NOT NULL CHECK (state IN
                            ('ACCEPTED', 'RUNNING', 'CANCELLING',
                             'SUCCEEDED', 'FAILED', 'NEEDS_REVIEW', 'CANCELLED')),
  stop_reason             TEXT CHECK (stop_reason IS NULL OR stop_reason IN (
                            'GOAL_REACHED', 'STRUCTURE_INVALID', 'STEP_LIMIT',
                            'TOKEN_LIMIT', 'TIME_LIMIT', 'COST_LIMIT',
                            'POLICY_DENIED', 'GENERATION_FAILED',
                            'GENERATION_UNKNOWN', 'TOOL_DENIED_REPEATEDLY',
                            'USER_CANCELLED', 'UNRESOLVED_CONFLICTS')),
  goal                    TEXT NOT NULL,
  -- 发起方派生，S19 只读（§2.1）。tenant_id 必须与本行一致（不变量 5）。
  execution_context       TEXT NOT NULL,
  input_refs              TEXT NOT NULL,
  output_schema_ref       TEXT NOT NULL,
  budget_scope_id         TEXT NOT NULL,
  limits_json             TEXT NOT NULL,
  result_ref              TEXT,
  steps_used              INTEGER NOT NULL DEFAULT 0 CHECK (steps_used >= 0),
  tokens_in               INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
  tokens_out              INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
  cost_micro              INTEGER NOT NULL DEFAULT 0 CHECK (cost_micro >= 0),
  cost_state              TEXT NOT NULL DEFAULT 'PROVISIONAL'
                            CHECK (cost_state IN ('PROVISIONAL', 'FINAL')),
  publish_sequence        INTEGER NOT NULL DEFAULT 0 CHECK (publish_sequence >= 0),
  lease_owner             TEXT,
  lease_expires_at        TEXT,
  created_at              TEXT NOT NULL,
  finished_at             TEXT,
  command_id              TEXT NOT NULL,
  PRIMARY KEY (tenant_id, task_id),
  -- §5.2 行 655：根任务自指，否则必须指出纠正的是谁。
  CHECK (task_id = root_task_id OR correction_of_task_id IS NOT NULL),
  -- 终态必须有 stop_reason；非终态必须没有（§3.6 行 403 的守卫）。
  CHECK (
    (state IN ('ACCEPTED', 'RUNNING', 'CANCELLING') AND stop_reason IS NULL)
    OR (state IN ('SUCCEEDED', 'FAILED', 'NEEDS_REVIEW', 'CANCELLED')
        AND stop_reason IS NOT NULL)),
  -- §3.6 行 404 / §6.4 行 888 / §3.5 的四道闸。配对表本身待 owner 定
  -- （ATC-5）；这里只写方案明确说过的四条，见 S19-21。
  CHECK (
    (state <> 'SUCCEEDED' OR stop_reason = 'GOAL_REACHED')
    AND (state <> 'NEEDS_REVIEW' OR stop_reason = 'UNRESOLVED_CONFLICTS')
    AND (state <> 'CANCELLED' OR stop_reason = 'USER_CANCELLED')),
  -- 结果指针与终态配对：只有 SUCCEEDED / NEEDS_REVIEW 会有 task_result 行。
  CHECK (result_ref IS NOT NULL
         OR state IN ('ACCEPTED', 'RUNNING', 'CANCELLING', 'FAILED', 'CANCELLED'))
);

CREATE INDEX IF NOT EXISTS task_queue_idx
  ON agent_task (tenant_id, role, state, created_at)
  WHERE state = 'ACCEPTED';
CREATE INDEX IF NOT EXISTS task_root_idx
  ON agent_task (tenant_id, root_task_id, correction_round);
-- §6.3 行 877：同一父任务的同一轮纠正只允许一次。没有它，一次网络抖动
-- 可以对同一份违规列表发起两个 round-1 纠正，付两笔钱。
CREATE UNIQUE INDEX IF NOT EXISTS correction_once
  ON agent_task (tenant_id, correction_of_task_id)
  WHERE correction_of_task_id IS NOT NULL;

-- ---------- 步骤 ----------
CREATE TABLE IF NOT EXISTS agent_step (
  tenant_id          TEXT NOT NULL,
  task_id            TEXT NOT NULL,
  step_index         INTEGER NOT NULL CHECK (step_index >= 0),
  kind               TEXT NOT NULL CHECK (kind IN
                       ('MODEL', 'TOOL', 'VALIDATE', 'RECOVER')),
  started_at         TEXT NOT NULL,
  finished_at        TEXT,
  outcome            TEXT NOT NULL CHECK (outcome IN
                       ('OK', 'DENIED', 'ERROR', 'TIMEOUT')),
  generation_job_ref TEXT,
  model_snapshot     TEXT,
  tokens_in          INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
  tokens_out         INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
  cost_micro         INTEGER NOT NULL DEFAULT 0 CHECK (cost_micro >= 0),
  error_code         TEXT,
  PRIMARY KEY (tenant_id, task_id, step_index),
  FOREIGN KEY (tenant_id, task_id)
    REFERENCES agent_task (tenant_id, task_id)
);

-- ---------- 工具调用审计（不存原文） ----------
CREATE TABLE IF NOT EXISTS tool_call_audit (
  tenant_id        TEXT NOT NULL,
  task_id          TEXT NOT NULL,
  step_index       INTEGER NOT NULL,
  tool_name        TEXT NOT NULL,
  -- docs/14 最小化：参数只存摘要，原文留在受限存储。
  arguments_digest TEXT NOT NULL CHECK (length(arguments_digest) = 64),
  arguments_ref    TEXT,
  verdict          TEXT NOT NULL CHECK (verdict IN ('ALLOW', 'DENY')),
  deny_reason      TEXT,
  permission_key   TEXT,
  scope_kind       TEXT,
  scope_id         TEXT,
  revocation_epoch INTEGER,
  egress_target    TEXT,
  result_digest    TEXT,
  result_ref       TEXT,
  duration_ms      INTEGER,
  called_at        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, task_id, step_index, tool_name),
  FOREIGN KEY (tenant_id, task_id)
    REFERENCES agent_task (tenant_id, task_id),
  -- §5.4：DENY 必须带原因，ALLOW 不该带。
  CHECK (
    (verdict = 'DENY' AND deny_reason IS NOT NULL)
    OR (verdict = 'ALLOW' AND deny_reason IS NULL))
);

-- ---------- 检查点 ----------
CREATE TABLE IF NOT EXISTS task_checkpoint (
  tenant_id                  TEXT NOT NULL,
  task_id                    TEXT NOT NULL,
  checkpoint_seq             INTEGER NOT NULL CHECK (checkpoint_seq >= 1),
  step_index                 INTEGER NOT NULL CHECK (step_index >= 0),
  messages_ref               TEXT NOT NULL,
  state_digest               TEXT NOT NULL CHECK (length(state_digest) = 64),
  pending_generation_job_ref TEXT,
  remaining_json             TEXT NOT NULL,
  created_at                 TEXT NOT NULL,
  PRIMARY KEY (tenant_id, task_id, checkpoint_seq),
  FOREIGN KEY (tenant_id, task_id)
    REFERENCES agent_task (tenant_id, task_id)
);

-- ---------- 结果（一旦写入不可变） ----------
CREATE TABLE IF NOT EXISTS task_result (
  tenant_id         TEXT NOT NULL,
  result_id         TEXT NOT NULL,
  task_id           TEXT NOT NULL,
  revision          INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  proposal          TEXT NOT NULL,
  source_refs       TEXT NOT NULL,
  assumptions       TEXT NOT NULL,
  unresolved_issues TEXT NOT NULL,
  confidence        REAL CHECK (confidence IS NULL
                                OR (confidence >= 0 AND confidence <= 1)),
  suggested_actions TEXT NOT NULL,
  usage_trace       TEXT NOT NULL,
  content_hash      TEXT NOT NULL CHECK (length(content_hash) = 64),
  created_at        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, result_id, revision),
  -- §3.8 不变量 1：一个任务只有一行结果。
  UNIQUE (tenant_id, task_id),
  FOREIGN KEY (tenant_id, task_id)
    REFERENCES agent_task (tenant_id, task_id)
);

CREATE TRIGGER IF NOT EXISTS task_result_no_update
BEFORE UPDATE ON task_result
BEGIN
  SELECT RAISE(ABORT, 'task_result is immutable');
END;
"""
