# S21 · 长任务编排 —— 实现方案

状态：实现方案（W1）。`maturity: design_only` 不变；本文描述"照着能写代码"的形态，不是已实现系统。

上游依据：[docs/01](../docs/01-enterprise-architecture.md) §3 唯一权威表、§5 技术基线；[docs/03](../docs/03-deployment-capacity.md) §2 量级、§4 弹性与背压；[docs/04](../docs/04-data-consistency.md) §1 epoch、§3 准入 Saga；[docs/06](../docs/06-automation-governance.md) §1 四档、§4 动态切换；[docs/07](../docs/07-production-workflows.md) §4 执行层级与停止；[docs/16](../docs/16-migration-acceptance.md) E06 / E13 / E22 / E33；[S21 规格](../docs/design-package/services/S21-orchestration.md)；[workflows/README](../workflows/README.md)。

配套：[S08 版本快照](S08-revision-snapshot.md)、[S26 生成任务](S26-generation-jobs.md)、[S23 预算账本](S23-budget-ledger.md)、[S22 调度配额](S22-scheduler-quota.md)、[S25 模型路由](S25-model-routing.md)、[S39 修复决策](S39-repair-planning.md)、[S02 治理策略](S02-governance-policy.md)。

---

## 0. 边界与一句话职责

S21 的职责只有一句：**把"这一集要产出什么"翻译成一棵可重放、可停止、可续跑的 Temporal 执行树，并维护一份只为查询而存在的业务投影。**

### 0.1 不做什么（按重要性排序）

| 不做 | 为什么 | 谁做 |
|---|---|---|
| **不自建执行状态机** | docs/01 §3 原文："工作流执行history \| Temporal \| S21保留业务投影，不自建另一套执行状态机" | Temporal |
| 不持有当前生产状态、manifest、accepted pointer、`node_epoch` | S08 是唯一写者 | S08 |
| 不判"能不能生成/接受" | 政策判定 | S02 |
| 不预留金额、不记余额 | 金额归 S23 | S23 |
| 不申请 permit | 配额归 S22 | S22 |
| 不创建/重提生成任务 | 生成任务归 S26 | S26 |
| 不直连供应商、不直连模型 | 付费只走 `S24 → S25 → S26 → S27` | S25/S26/S27 |
| 不算影响闭包 | 影响分析归 S09 | S09 |
| 不组装 prompt、不构建上下文包 | S18/S20 | S18/S20 |
| 不做 QC 判定 | S35–S37 | S35–S37 |
| 不写审核决定 | S40 | S40 |

判据：**"这个 Run 现在到哪一步了"的答案来自 Temporal 的 history，S21 的投影只是它的可读副本；"这一集现在是什么"来自 S08。** S21 的投影与 Temporal 冲突时，以 Temporal 为准，投影修复——反过来就是把自己变成了第二权威。

### 0.2 一个容易误读的点：投影 ≠ 状态机

`run_projection.state` 有 11 个取值，看起来像状态机。**它不是。**

- **状态转移的决定权在 workflow 代码里**（确定性控制流），
- **投影只记录 workflow 已经做出的决定**，写入路径是 workflow 内的一个 Activity `record_state`，
- 投影丢失时**可以从 history 重建**（§1.4），而真正的状态机丢失时只能恢复快照。

所以本方案里所有"状态机"表格描述的是 **workflow 的行为契约**，不是 S21 进程内的状态机实现。S21 进程里没有 `if state == RUNNING then ...` 这样的转移逻辑——只有"把 workflow 报上来的状态写进去"。

### 0.3 两套 epoch 里 S21 的角色

| 概念 | 作用域 | 谁写 | S21 做什么 |
|---|---|---|---|
| `execution_fence` | run / branch | **S08** | **触发者**。取消 Run 或故障切换时，S21 调 S08 `POST /branches/{series_id}/{branch_id}:fence`（`reason=RUN_CANCELED` / `RUN_FAILOVER`）。S21 **不自己递增**，也不缓存 fence 作为判定依据；每次派单前从 `execution_context` 里读。 |
| `node_epoch` | 单个 `logical_node` | **S08** | **消费者**。S21 消费 `snapshot.manifest.committed` 的 `changed_node_epochs[]`，只取消**这些节点**对应的在途子图。S21 从不写 epoch，也从不因为 "manifest revision 变了" 就取消全部任务。 |
| 部署级 `leader_epoch`（选主） | 部署 | 基础设施 | 无关。不要出现在本服务的任何表或代码里。 |

**最容易写错的地方**：把 `node_epoch` 当 `execution_fence` 用。典型错误写法是"manifest 变了就 fence 整个 run"。那会让"改了第 3 集第 2 镜"导致整个 Run 的所有在途任务作废——直接违反 docs/04 §1"改一个镜头只提升受影响节点的 node_epoch，不使其他在途镜头全部过期"。正确做法见 §4.4。

---

## 1. 数据模型

数据库 `orchestration`，Postgres 16。**没有执行状态表**——只有绑定、投影、检查点、幂等与 outbox。

### 1.1 `workflow_binding` —— Run ↔ Temporal 的绑定

```sql
CREATE TABLE workflow_binding (
  tenant_id            text NOT NULL,
  run_id               text NOT NULL,
  workflow_id          text NOT NULL,
  run_generation       integer NOT NULL,     -- Continue-As-New 代数，从 1 起
  namespace            text NOT NULL,
  task_queue           text NOT NULL,
  workflow_type        text NOT NULL,        -- episode | shot | memory-migration | repair
  business_idem_key    text NOT NULL,        -- 跨 generation 恒定的业务幂等键
  series_id            text NOT NULL,
  episode_id           text NOT NULL,
  branch_id            text NOT NULL,
  manifest_id          text NOT NULL,
  manifest_revision_at_start integer NOT NULL,
  execution_fence_at_start   bigint NOT NULL,
  automation_mode      text NOT NULL,
  policy_ref           jsonb NOT NULL,
  budget_scope_id      text NOT NULL,
  status               text NOT NULL,        -- ACTIVE | COMPLETED | TERMINATED
  created_at           timestamptz NOT NULL DEFAULT now(),
  closed_at            timestamptz,

  PRIMARY KEY (tenant_id, run_id),
  CONSTRAINT wb_generation_positive CHECK (run_generation >= 1),
  CONSTRAINT wb_status_closed CHECK (status IN ('ACTIVE','COMPLETED','TERMINATED')),
  CONSTRAINT wb_closed_has_ts CHECK ((status <> 'ACTIVE') = (closed_at IS NOT NULL)),
  CONSTRAINT wb_mode_closed CHECK (automation_mode IN
    ('guided','stage_review','exception_assisted','unattended_batch'))
);

-- Temporal workflow_id 在本 namespace 内唯一
CREATE UNIQUE INDEX wb_workflow_uniq
  ON workflow_binding (namespace, workflow_id, run_generation);

-- 业务幂等：一个 (tenant, business_idem_key) 只能有一个 ACTIVE Run
CREATE UNIQUE INDEX wb_business_active_uniq
  ON workflow_binding (tenant_id, business_idem_key)
  WHERE status = 'ACTIVE';

CREATE INDEX wb_by_episode_idx
  ON workflow_binding (tenant_id, series_id, branch_id, episode_id, created_at DESC);

CREATE INDEX wb_active_idx ON workflow_binding (tenant_id, status)
  WHERE status = 'ACTIVE';
```

**`workflow_id` 的确定性构造**：

```
workflow_id = "{workflow_type}:{tenant_id}:{business_idem_key}:g{run_generation}"
namespace   = "{home_cell}"           // 每 cell 一个 Temporal namespace（docs/03 §1）
task_queue  = "{workflow_type}.{automation_mode}"   // 按任务类型与档位分队列
```

确定性 `workflow_id` 是"重启后不重复启动"的第一道闸：Temporal 对同 id 的 `start_workflow` 是幂等的（workflow already running / already completed 都会返回既有句柄，取决于配置）。**不靠应用内存判断。**

**`business_idem_key` 不等于 `run_id`**。它由 Run 创建命令的内容决定：

```
business_idem_key = sha256(JCS({
  tenant_id, series_id, branch_id, episode_id,
  base_manifest_ref,                 // id + revision + content_hash
  automation_mode, policy_revision,
  budget_scope_id,
  intent                             // episode_production | change_subgraph | repair | memory_migration
}))
```

所以"对同一 manifest 再点一次开始生产"不会创建第二个 Run；"manifest 已经前进了"则会产生新的 key，是新 Run，正确。

### 1.2 `run_projection` —— 只读业务投影

```sql
CREATE TABLE run_projection (
  tenant_id       text NOT NULL,
  run_id          text NOT NULL,
  state           text NOT NULL,          -- 11 值，与 openapi Run.state 完全一致
  manifest_revision integer NOT NULL,     -- 最近一次被 S21 观察到的
  execution_fence bigint NOT NULL,
  completed_nodes integer NOT NULL DEFAULT 0,
  blocked_nodes   integer NOT NULL DEFAULT 0,
  failed_nodes    integer NOT NULL DEFAULT 0,
  waiting_reason  text,                   -- WAITING_* 的细分原因
  blocked_reason  text,
  current_stage   text,                   -- Temporal 当前所在的 ProductionStage
  projection_watermark bigint NOT NULL,   -- 单调递增，每次 record_state +1
  last_event_at   timestamptz NOT NULL,
  terminal_at     timestamptz,
  version         bigint NOT NULL DEFAULT 1,

  PRIMARY KEY (tenant_id, run_id),
  CONSTRAINT rp_state_closed CHECK (state IN (
    'QUEUED','RUNNING','WAITING_INPUT','WAITING_REVIEW','WAITING_MEMORY',
    'WAITING_BUDGET','PAUSED','SUCCEEDED','INCOMPLETE','FAILED','CANCELED')),
  CONSTRAINT rp_terminal_has_ts CHECK (
    (state IN ('SUCCEEDED','INCOMPLETE','FAILED','CANCELED')) = (terminal_at IS NOT NULL)),
  CONSTRAINT rp_waiting_has_reason CHECK (
    (state IN ('WAITING_INPUT','WAITING_REVIEW','WAITING_MEMORY','WAITING_BUDGET'))
      = (waiting_reason IS NOT NULL)),
  CONSTRAINT rp_counts_nonneg CHECK (
    completed_nodes >= 0 AND blocked_nodes >= 0 AND failed_nodes >= 0),
  CONSTRAINT rp_watermark_positive CHECK (projection_watermark >= 1)
);

CREATE INDEX rp_active_idx ON run_projection (tenant_id, state)
  WHERE terminal_at IS NULL;
CREATE INDEX rp_by_branch_idx ON run_projection (tenant_id, state, last_event_at DESC);
```

**`projection_watermark` 的语义**：每次 `record_state` Activity 成功写入就 +1，单调。它不是 Temporal 的 event id，也不用来做事件去重——它只回答"这个投影有多新"。S47 用它做 SSE 的 resubscribe 判定（旧投影必须带水位，见 docs/04 §5）。

**为什么投影里同时有 `completed_nodes` / `blocked_nodes` 两个计数而不是数组**：openapi 的 `Run` schema 就要求这两个字段（`*id004` = `integer minimum 0`）。节点明细不放投影——那是 manifest 与 S08 `node-diff` 的事。

### 1.3 `run_checkpoint` —— Continue-As-New 跨代的业务检查点

Temporal 的 Continue-As-New 会换 `run_generation`；**业务幂等必须跨代成立**（workflows/README）。

```sql
CREATE TABLE run_checkpoint (
  tenant_id        text NOT NULL,
  run_id           text NOT NULL,
  run_generation   integer NOT NULL,
  checkpoint_seq   integer NOT NULL,
  stage            text NOT NULL,
  completed_node_ids   text[] NOT NULL DEFAULT '{}',
  in_flight_node_ids   text[] NOT NULL DEFAULT '{}',
  pending_node_ids     text[] NOT NULL DEFAULT '{}',
  budget_reserved_micro bigint NOT NULL DEFAULT 0,
  spent_micro      bigint NOT NULL DEFAULT 0,
  provider_job_refs jsonb NOT NULL DEFAULT '[]',
  continuation_payload jsonb,           -- 传给下一代 workflow 的输入
  created_at       timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (tenant_id, run_id, run_generation, checkpoint_seq),
  CONSTRAINT rc_money_nonneg CHECK (budget_reserved_micro >= 0 AND spent_micro >= 0)
);

CREATE INDEX rc_latest_idx
  ON run_checkpoint (tenant_id, run_id, run_generation DESC, checkpoint_seq DESC);
```

**检查点不替代 Temporal history**。它只携带"下一代 workflow 需要知道、但 history 里查起来很贵"的东西：节点分组、在途 provider_job 引用、已花金额。**任何能从 history 重算出来的东西都不进检查点**，否则就成了第二份 history。

`provider_job_refs` 是恢复的关键：`PROVIDER_JOB_WAIT` 跨代后必须先查这些 job 的现状（§4.6），而不是重新生成。

### 1.4 `run_projection` 的重建

投影丢失（DB 恢复、bug 导致脏写）时的重建路径：

```
1. 遍历 workflow_binding 中 status='ACTIVE' 的 run
2. 对每个 run_id，用 Temporal Query（workflow 内定义的 @workflow.query）
   取 { state, completed_nodes, blocked_nodes, current_stage }
3. 用 describe_workflow_execution 取 { status, close_time }
4. 写 run_projection，projection_watermark = 现有 + 1（表示"重建了一次"）
```

**必须实现**这条路径，并且要有测试（T21-30）。理由：docs/16 E33 要求"Temporal 升级/全部 Worker 停机"后仍可用；如果投影是唯一事实，那就等于把执行状态放在了 S21 里——正是 0.2 要避免的。

### 1.5 `node_in_flight` —— 在途节点的索引（为停止服务）

```sql
CREATE TABLE node_in_flight (
  tenant_id      text NOT NULL,
  run_id         text NOT NULL,
  node_id        text NOT NULL,
  node_epoch     bigint NOT NULL,          -- 派单时读到的值，只读快照
  execution_fence bigint NOT NULL,          -- 派单时读到的值
  stage          text NOT NULL,
  provider_job_id text,                    -- 尚未派单时为 NULL
  attempt_id     text,
  dispatched_at  timestamptz NOT NULL DEFAULT now(),
  settled_at     timestamptz,

  PRIMARY KEY (tenant_id, run_id, node_id, node_epoch),
  CONSTRAINT nif_fence_positive CHECK (execution_fence >= 1),
  CONSTRAINT nif_epoch_positive CHECK (node_epoch >= 1)
);

CREATE INDEX nif_open_idx ON node_in_flight (tenant_id, run_id)
  WHERE settled_at IS NULL;
CREATE INDEX nif_by_epoch_idx ON node_in_flight (tenant_id, run_id, node_id, node_epoch)
  WHERE settled_at IS NULL;
CREATE INDEX nif_by_job_idx ON node_in_flight (provider_job_id)
  WHERE provider_job_id IS NOT NULL AND settled_at IS NULL;
```

这张表**不是执行状态的权威**，它只回答一个问题："这个节点现在有没有在途任务，能不能取消？"（§4.5 取消语义需要）。`node_epoch` 与 `execution_fence` 在这里是**快照**，不参与任何判定——判定用现在的值，快照只用于解释"为什么这条被取消了"。

### 1.6 `run_command` —— 暂停/恢复/取消的幂等与结果

```sql
CREATE TABLE run_command (
  tenant_id   text NOT NULL,
  command_id  text NOT NULL,
  run_id      text NOT NULL,
  action      text NOT NULL,             -- pause | resume | cancel
  request_hash text NOT NULL,
  state       text NOT NULL,             -- ACCEPTED | APPLIED | REJECTED | SUPERSEDED
  result      jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  applied_at  timestamptz,

  PRIMARY KEY (tenant_id, command_id),
  CONSTRAINT rcmd_action_closed CHECK (action IN ('pause','resume','cancel')),
  CONSTRAINT rcmd_state_closed CHECK (state IN
    ('ACCEPTED','APPLIED','REJECTED','SUPERSEDED'))
);
CREATE INDEX rcmd_by_run_idx ON run_command (tenant_id, run_id, created_at DESC);
```

**`SUPERSEDED` 是一个真实需要的状态**：pause 之后收到 cancel，pause 命令的语义已被后来的命令覆盖。openapi 的 `Operation.state` 里没有它（§9 C21-3），但内部必须有——否则重放一条被覆盖的 pause 会重新把 Run 推回 `PAUSED`。

### 1.7 `repair_budget` —— Run 级修复额度（S39 的 Level 4）

```sql
CREATE TABLE repair_budget (
  tenant_id     text NOT NULL,
  run_id        text NOT NULL,
  max_business_attempts integer NOT NULL,
  used_business_attempts integer NOT NULL DEFAULT 0,
  max_recheck_rounds integer NOT NULL,
  used_recheck_rounds integer NOT NULL DEFAULT 0,
  ceiling_micro bigint,                   -- 来自 S23 的 run 预算上限，只读复制
  updated_at    timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (tenant_id, run_id),
  CONSTRAINT rb_used_le_max CHECK (
    used_business_attempts <= max_business_attempts
    AND used_recheck_rounds <= max_recheck_rounds)
);
```

**`ceiling_micro` 是只读复制，不是权威**。真正的金额上限在 S23；这里复制一份是为了在派单前做**本地快速预检**，避免每次修复都打一次 S23。`used_*` 是 S21 自己的计数——S39 的 `RepairAttemptLedger` 记的是"决策维度的跨 Run 累计"，与这里不重叠（[S39 §5.1](S39-repair-planning.md)）。

### 1.8 基础设施表

```sql
CREATE TABLE idempotency_record (
  tenant_id     text NOT NULL,
  command_id    text NOT NULL,
  request_hash  text NOT NULL,
  state         text NOT NULL,        -- IN_PROGRESS | DONE
  response_code integer,
  response_body jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz,
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE outbox (
  tenant_id          text NOT NULL,
  event_id           text NOT NULL,
  aggregate_id       text NOT NULL,             -- run_id
  aggregate_sequence bigint NOT NULL,     -- = projection_watermark 快照
  event_type         text NOT NULL,
  partition_key      text NOT NULL,       -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  PRIMARY KEY (tenant_id, event_id)
);
CREATE UNIQUE INDEX outbox_seq_uniq
  ON outbox (tenant_id, aggregate_id, aggregate_sequence, event_type);
CREATE INDEX outbox_pending_idx ON outbox (published_at) WHERE published_at IS NULL;

CREATE TABLE inbox (
  consumer    text NOT NULL,     -- s21.jobs | s21.manifest | s21.permits | s21.budget
  event_id    text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

`aggregate_sequence = projection_watermark` 使**每个投影变更恰好对应一个事件**，且顺序与投影变更顺序一致。S47 与 S39 消费 `orchestration.run.state-changed` 时可以用 sequence 做缺口检测。

### 1.9 不建的表

| 不建 | 为什么 |
|---|---|
| `workflow_step` / `execution_state` | 那就是"另一套执行状态机"。history 在 Temporal |
| `node` / `stage` 定义表 | 来自 manifest 与 S06 DSL，S21 不持有 |
| `provider_job` 镜像 | S26 是权威；S21 只在 `node_in_flight` 存引用 |
| `budget_ledger` | S23 |
| `permit` | S22 |
| `run_result` | 交付物在 S08 manifest / S43 包；S21 只发状态 |

---

## 2. Temporal 执行树

### 2.1 五层层级与各自的边界

docs/07 §4 的层级：`SeriesBatch → Episode → Scene/Shot → ProductionStage → ProviderJobWait/Media/QC`。

| 层 | workflow 类型 | 扇出上限 | deadline | cancel scope | 预算 scope |
|---|---|---|---|---|---|
| `SeriesBatch` | `series_batch` | 每批 ≤ 50 集 | 24h | 取消整批 → 对每集发 `cancel` signal，不 fence | 无自己预算，向下委派 escrow |
| `Episode` | `episode` / 一次 Run 一个 | 1 | 按集时长规格，默认 6h | 该 Run 的 cancel scope | `budget_scope_id`（集级） |
| `Scene/Shot` | `shot`（子工作流） | 每集 ≤ 800 镜头；每 Scene ≤ 60 | 每镜头 ≤ 45 min | 单镜头取消 | 节点级 child budget（`grant_escrow`） |
| `ProductionStage` | Activity 序列（**不是**子工作流） | 每镜头 ≤ 12 个 stage | 每 stage 独立 deadline | 随镜头 | 该节点的 reservation 范围 |
| `ProviderJobWait` / `Media` / `QC` | Activity（长期等待用 signal） | — | 供应商超时 + 归档余量 | 随镜头 | 已在准入时预留 |

**为什么 `ProductionStage` 不做成子工作流**：一个镜头通常 6–12 个 stage，做成子工作流会让 history 体积翻 3–5 倍，而 stage 之间是严格线性的，没有并行可扇出。docs/07 §4 的层级描述的是**语义层**，不是"每层必须是一个 workflow"。

**为什么 `Scene/Shot` 必须是子工作流**：镜头之间有并行（一集的 200 个镜头可以并发跑），且单个镜头的 history 会超过 Temporal 推荐的 50k events（一个镜头 ~20 个 Activity + 重试 + 心跳，加上 provider 等待的 timer）。子工作流把每个镜头的 history 隔离，父 workflow 只看到 child 结果。这是 E33 的基础。

### 2.2 扇出控制

扇出上限不是"软约束"，是**准入闸门**：

```python
# 确定性代码，在 workflow 里执行
if len(active_children) >= MAX_CONCURRENT_SHOTS:
    await workflow.wait_condition(lambda: len(active_children) < MAX_CONCURRENT_SHOTS)
```

| 上限 | 默认值 | 来源 | 超限行为 |
|---|---|---|---|
| `MAX_CONCURRENT_SHOTS_PER_EPISODE` | 24 | 按 cell 容量与供应商配额反推，可配 | 排队，不拒绝 |
| `MAX_CONCURRENT_CHILDREN`（Temporal 限制） | 2000 | Temporal 硬上限 | 必须靠上面的软上限避免触及 |
| `MAX_SHOTS_PER_EPISODE` | 800 | 一集极长的情况 | 超限 → Run 创建时 422 `EPISODE_TOO_LARGE` |
| `MAX_EPISODES_PER_BATCH` | 50 | 批量产能 | 超限 → 422 `BATCH_TOO_LARGE` |

**排队在 workflow 里，不在 API 进程里**。docs/07 §4："不在 API 进程 sleep"。API 收 `POST /runs` 后立即 `start_workflow` 并返回 202，排队由 workflow 的 `wait_condition` 完成。

### 2.3 Continue-As-New 的触发条件

```python
CONTINUE_AS_NEW_EVENT_THRESHOLD = 20_000   # history events
CONTINUE_AS_NEW_AT_STAGE_BOUNDARY = True   # 只在 stage 边界换，不在 stage 中间换
```

触发时机：每完成一个 `ProductionStage`、准备进入下一个之前检查 `workflow.info().get_current_history_length()`。

写完新检查点（`run_checkpoint`，`run_generation + 1`）后 `workflow.continue_as_new(payload)`。

**只在 stage 边界换代**的原因：在 stage 中间换代意味着要把"这个 Activity 已经跑到哪了"完整序列化进 payload，而 Activity 本身可能正在等 provider。stage 边界是一个自然的一致点：所有 stage 内 Activity 都已终态或已落到 `node_in_flight`。

**跨代去重**：Signal / Update 的去重作用域受 Continue-As-New 影响（workflows/README）。所以：

- 所有 signal handler 的幂等键来自**信号内容**（`command_id` / `attempt_id` / `event_id`），不是来自 workflow 内存里的集合；
- 去重表在 `run_command` 与 `inbox` 里，跨代有效；
- **不依赖 Temporal 的 signal dedup**。

### 2.4 Activity 分类

| 类别 | 例子 | 确定性 | deadline | heartbeat | 重试 |
|---|---|---|---|---|---|
| **判定类**（纯计算） | 从 manifest 算"哪些节点该跑"、排序、分组 | 必须确定性 | 5s | 否 | 不重试（失败即 workflow bug，直接 FAILED） |
| **读类** | 读 S08 manifest、读 S02 policy、读 S09 impact | 非确定 | 10s | 否 | 指数退避，最多 4 次 |
| **写命令类**（有业务幂等） | S08 `:commit`、S23 reserve、S22 acquire | 非确定 | 30s | 否 | 有界重试；**UNKNOWN 类错误不重试**（§4.7） |
| **外部派单类** | S26 `POST /generation-jobs` | 非确定 | 15s | 否 | **绝不盲重试**，见 §4.7 |
| **长等待类** | `PROVIDER_JOB_WAIT` | 非确定 | 见 §2.5 | 否（靠 signal） | 不重试（等待不是失败） |
| **媒体/归档类** | S28 归档、下载 | 非确定 | 10 min | **是**（30s） | 有界重试 |
| **记录类** | `record_state` | 非确定 | 5s | 否 | 幂等重试（同 watermark 不重复写） |

**"判定类必须确定性"落地为一条纪律**：这类 Activity 的参数与返回值必须是可 JCS 序列化的纯数据；函数体里不能有 `now()`、随机数、环境变量读取。在 replay 测试里（§8 T21-31）必须逐字节比对。

### 2.5 `PROVIDER_JOB_WAIT` 的等待模型

docs/07 §4："等待模型使用 callback signal + 持久 timer 对账"。

```
workflow:
  await workflow.wait_condition(
      lambda: attempt_state in TERMINAL_STATES or timer_fired,
      timeout=hard_deadline)

signal: provider_job_state_changed(attempt_id, state, observed_at)
  来源：S21 消费 generation.job.state-changed 后 signal 到对应 workflow

timer: 持久 timer，默认 90s
  到点 → 调用 S26 GET /generation-jobs/{id} 对账一次
       → 仍 RUNNING → 再起 timer（指数退避，上限 10 min）
       → 终态 → 推进
```

**S21 不轮询 S26**（[S26 §6](S26-generation-jobs.md) 明示）。signal 是主路径，timer 是兜底：事件丢了、signal 没送到（worker 换代、Continue-As-New 边界）时，timer 保证不会永远挂着。

**timer 的代价**：每个在途 job 每 90s–10min 一次查询。按峰值 20,313 在途（docs/03）、平均退避 5 min 估算，约 68 QPS 打到 S26 的读接口。这个量级 S26 的读接口能承受，但要监控。若 signal 投递正常率 > 99.9%，可把初始 timer 拉到 5 min。

**`hard_deadline` 的来源**：供应商超时 + 归档余量，由 S25 的路由决策给出，S21 只消费。

### 2.6 Workflow 内的状态集合（行为契约，不是实现）

这 11 个状态与 openapi `Run.state` **完全一致**，不允许出现第 12 个。

```
QUEUED ──► RUNNING ──┬──► WAITING_INPUT ──┬──► RUNNING
                     ├──► WAITING_REVIEW ─┤
                     ├──► WAITING_MEMORY ─┤
                     ├──► WAITING_BUDGET ─┘
                     ├──► PAUSED ──────────► RUNNING
                     ├──► SUCCEEDED   (终态)
                     ├──► INCOMPLETE  (终态)
                     ├──► FAILED      (终态)
                     └──► CANCELED    (终态)
```

| 从 | 到 | 触发 | 副作用 |
|---|---|---|---|
| — | `QUEUED` | `POST /runs` 后 workflow 启动 | 无 |
| `QUEUED` | `RUNNING` | 第一个可执行 Activity 开始 | 写 `node_in_flight` |
| `RUNNING` | `WAITING_*` | 需要外部输入 | `waiting_reason` 必填 |
| `WAITING_*` | `RUNNING` | 输入到达（signal） | 清空 `waiting_reason` |
| `RUNNING`/`WAITING_*` | `PAUSED` | `pause` 命令 | **不中止**在途 Activity |
| `PAUSED` | `RUNNING` | `resume` 命令 | 先查已存在 provider_job |
| 任意非终态 | `SUCCEEDED` | 所有必需节点完成且全部接受/可接受 | 结算 escrow |
| 任意非终态 | `INCOMPLETE` | 有 `blocked_nodes` 且策略允许结束 | 产失败报告 |
| 任意非终态 | `FAILED` | workflow bug / 不可恢复错误 | 结算、告警 |
| 任意非终态 | `CANCELED` | `cancel` 命令 | 调 S08 `:fence` |

**终态不可倒退**（docs/04 §4）。`SUCCEEDED → RUNNING` 是禁止的，即使收到迟到的事件。

**`WAITING_*` 与 `PAUSED` 的区别**（容易混）：

| | 含义 | 谁恢复 | 是否继续结算在途任务 |
|---|---|---|---|
| `WAITING_INPUT` / `_REVIEW` / `_MEMORY` / `_BUDGET` | 缺**外部条件**，workflow 在等一个 signal | 条件满足时自动 | 是（在途任务继续跑） |
| `PAUSED` | **人主动暂停**，停止新准入 | 只有 `resume` 命令 | 是（docs/07 §4：继续已有任务归档结算） |

---

## 3. 关键流程

### 3.1 创建 Run（E13 的起点）

`POST /episodes/{episode_id}/runs`，body `{context, budget_scope_id, command_id}`。

```
1. 认证 → tenant_id / actor（header 只校验不授权）
2. 幂等：request_hash；命中 idempotency_record 直接返回
3. 校验 context（execution-context.v2 全 required）：
   - manifest_ref 的存在性与 revision 由 S08 复查
   - execution_fence 必须等于 S08 的当前 fence（否则 409 STALE_EXECUTION_FENCE）
4. 读 S02：GET /projects/{project_id}/effective-policy?scope=...（带 episode/stage/node）
   → 得到 automation_mode、generation_mode、acceptance_mode、required_gates
   → generation_mode=manual 时**不启动 workflow**，返回 202 但产品侧应该是"等人工"
5. 算 business_idem_key
6. 写 workflow_binding（status=ACTIVE）+ run_projection(state=QUEUED) + outbox，同一事务
7. 事务提交后 start_workflow（确定性 workflow_id）
8. 返回 202 {operation_id, state:"accepted", run_id}
```

**步骤 6 与 7 的顺序是刻意的**：先落库再启动 workflow。若反过来（先启动、后落库），启动成功但落库失败会产生一个"有 Run 但没绑定"的孤儿——Temporal 里跑着，S21 查不到。反过来（先落库、启动失败）只需重试 `start_workflow`，因为 `workflow_id` 确定性，重试天然幂等。

**`generation_mode=manual` 的处理**：不启动 workflow，但**仍然创建 binding 与 projection**（`state=WAITING_INPUT`，`waiting_reason=GENERATION_MODE_MANUAL`）。这样"这一集打算生产但等人工确认"是可见的，而不是"什么都没发生"。

### 3.2 Run 的主干执行

```
episode workflow（一次 Run）
├─ 读 manifest（S08）→ 得到 node_states[] 与 source_revisions[]
├─ 读影响分析（若是 change_subgraph，从 ChangePlan 拿 regenerate/recheck/reuse）
├─ 算执行计划（判定类 Activity，确定性）
│    → 只有 REGENERATE 与 RECHECK 进执行集合；REUSE 直接复用
├─ 按 stage 分批
│   ├─ SHOT 子工作流（每镜头一个）
│   │   ├─ 读 context pack（S18）
│   │   ├─ 编译 prompt（S20）
│   │   ├─ 路由（S25）→ 拿 RouteDecision + degradation_level
│   │   ├─ 若 degradation 选项 auto_executable=false → WAITING_REVIEW / 直接失败
│   │   ├─ 创建生成任务（S26，含准入 Saga）
│   │   ├─ PROVIDER_JOB_WAIT（signal + timer）
│   │   ├─ 归档（S28）
│   │   ├─ QC（S35/S36/S37）
│   │   ├─ 缺陷 → S39 修复计划 → 有界返工（§4.3）
│   │   └─ 提交（S08 :commit）
│   └─ 汇总：S02 acceptance → S40 若需人 → S08 :accept
├─ EDL / 字幕 / 混音（S41 / S38 / S42）
├─ 最终 QC 与交付门
└─ 结算、终态
```

**每一层的 gate 来自 S02，不来自 S21**。S21 只是在 gate 需要人时把 Run 推进 `WAITING_REVIEW`，把 ReviewTask 交给 S40。

### 3.3 局部修改的子图执行

从 S07 的 ChangePlan 或 S08 的 `snapshot.manifest.committed` 触发。

```
1. 消费 snapshot.manifest.committed（trigger=COMMIT）
2. 若 data.changed_node_epochs 非空：
   - 对每个 {node_id, from, to}：
     - 找 node_in_flight 中该 node_id 且 node_epoch = from 的在途项
     - 按 §4.4 决定：取消 / discard_on_completion
   - 只为 changed_node_epochs 里的节点启动子图
3. 若 changed_node_epochs 为空、只有 accepted_pointers_delta：
   - **不启动任何子图**（只是指针变了，没有新的生产工作）
4. 启动的子图范围来自 S09 的 impact（若事件带 change_plan_ref）或 S08 的 changed_node_epochs
```

**这里绝不做的事**：因为 `trigger=COMMIT` 就重启整个 episode 子图。那违背"只重做相关子图"（service.yaml acceptance）。判断依据只有 `changed_node_epochs`。

### 3.4 失败矩阵

| # | 场景 | 检测 | S21 行为 | Run 状态 | 恢复 |
|---|---|---|---|---|---|
| F-01 | workflow 启动前进程崩溃 | `workflow_binding` 已写、workflow 未启 | 重试 `start_workflow`（确定性 id） | `QUEUED` | 客户端重试同 command_id |
| F-02 | workflow 已启、响应丢失 | 幂等记录 DONE | 返回首次响应 | 正常推进 | 客户端重试 |
| F-03 | Activity 重试耗尽（读类） | Temporal retry policy | 依赖不可达 → 该节点 `blocked` | 可能 `INCOMPLETE` | 人工或等依赖恢复后新 Run |
| F-04 | 写命令 Activity 超时（S08 `:commit`） | Activity timeout | **不重试**，转 `WAITING_*` + 查 S08 实际结果 | 保持非终态 | 用 `change_plan_ref`/`command_id` 查 S08 |
| F-05 | `POST /generation-jobs` 超时 | Activity timeout | **绝不重试**，记 `DISPATCH_UNKNOWN`，转 `WAITING_BUDGET` 或直接停该节点 | 非终态 | 按 `attempt_id` 查 S26 现状（§4.7） |
| F-06 | provider 事件丢失 | timer 到期未见 signal | 主动查 S26 | 非终态 | 对账 |
| F-07 | S26 返回 `SUBMISSION_UNKNOWN` | 响应 body | 该节点挂起，**不重新派单** | `WAITING_BUDGET`（细分 `waiting_reason=SUBMISSION_UNKNOWN`） | S26 对账后再推进 |
| F-08 | S23 预留失败（超预算） | S23 409 | 该节点 `blocked`，`blocked_reason=BUDGET_EXHAUSTED` | `WAITING_BUDGET` 或 `INCOMPLETE` | 加预算后新 Run |
| F-09 | S22 429 无容量 | S22 429 | 有界退避（`retry_after`），不无限等 | `RUNNING` | 容量恢复后自动 |
| F-10 | 节点 epoch 变化（同 Run 内） | manifest 事件 | 取消该节点在途任务（§4.4），重跑 | `RUNNING` | 自动 |
| F-11 | execution_fence 前进（取消） | 取消命令 | 调 S08 `:fence`，取消可取消任务 | `CANCELED` | 终态 |
| F-12 | workflow 代码 bug，确定性破坏 | replay 失败 | Temporal 报非确定性错误 | `FAILED` | 修代码 + `reset` workflow |
| F-13 | Worker 全部停机 | 无 worker | workflow 挂起，timer 不触发 | 非终态 | Worker 恢复后 replay 续跑 |
| F-14 | Continue-As-New 期间崩溃 | 新代未启 | 旧代已终态（continue_as_new 是原子的） | 非终态 | Temporal 自动；检查点已在 §2.3 写过 |
| F-15 | 投影写入失败 | `record_state` 失败 | workflow 继续（投影不是权威） | 正确，投影滞后 | 投影重建（§1.4） |

**F-05 是整份方案最重要的一行**。S21 是 `POST /generation-jobs` 的调用方，超时时它**不知道**S26 有没有创建 attempt。重试可能造成双重付费。这条约束在 AGENTS.md 的"绝对不能做"里明确列出。正确做法见 §4.7。

### 3.5 无人值守结束（E06）

`automation_mode=unattended_batch` 且 `exception_behavior=isolate_branch`（schema 的 `allOf` 强制）：

```
1. 关键节点失败且策略不允许降级
   → 该节点 blocked，**不影响独立分支**
2. 所有可执行节点跑完
3. 有 blocked_nodes > 0
   → state = INCOMPLETE
   → 产出失败报告：blocked_nodes[]、原因、建议动作
   → **不生成正式交付**
4. 无 blocked
   → SUCCEEDED
```

docs/06 §1："unattended_batch | 按预设策略自动选候选、返工、合成 | 无法解决的必要分支标blocked，继续独立分支；Run可结束INCOMPLETE并产失败报告，不伪造成片成功"。

**"继续独立分支"的落地**：`isolate_branch` 意味着失败的镜头不阻塞其他镜头，Run 整体仍可能 `INCOMPLETE`。S21 不创建新的 branch（那是 S08 / S07 的事）；它只是**不因一个节点失败而中止整个 workflow**。

**INCOMPLETE 不是 FAILED**：

| | INCOMPLETE | FAILED |
|---|---|---|
| 原因 | 部分节点不可完成 | 编排本身坏了 |
| 产物 | 有失败报告 + 部分可用产物 | 无保证 |
| 可续跑 | 是（补素材后新 Run） | 需先修代码 |
| 交付 | 禁止正式交付（`no_incomplete_formal_delivery=true`） | 禁止 |

---

## 4. 停止、取消、恢复（E22 核心）

docs/07 §4 原文："暂停停止新准入，继续已有任务归档结算；取消可取消渠道任务，其余discard_on_completion；恢复先查已存在provider_job。"

三句话对应三个流程。

### 4.1 暂停

```
pause 命令
1. 幂等（run_command）
2. 发 signal pause_requested 到 workflow
3. workflow：
   - 不再启动新的 child workflow / 新的 Activity
   - **不取消**已在跑的 Activity
   - 等所有在途 Activity 到终态或落到 node_in_flight
   - record_state(PAUSED)
4. 不调 S08、不调 S23、不调 S22
```

**"继续已有任务归档结算"**：在途的 provider job 继续跑、继续等、继续归档、继续 QC。暂停**不是**冻结账本。原因：那些任务已经花了钱，中止只会浪费；而且供应商侧不保证能取消。

**`PAUSED` 与"取消"的成本差异必须对用户可见**：暂停会继续花钱（在途任务），取消可能也退不回来（已提交的任务）。S47 的界面必须显示在途数量与已花金额。这是 docs/13 §3"顶栏显示费用"的落点。

### 4.2 取消

```
cancel 命令
1. 幂等（run_command）
2. 调 S08 POST /branches/{series_id}/{branch_id}:fence
   body { command_id, expected_fence, reason:"RUN_CANCELED", fence_ref:run_id }
   → 得到新 fence；409 FENCE_CONFLICT → 说明别人先 fence 了，读当前 fence 再决定
3. 遍历 node_in_flight（settled_at IS NULL）：
   对每一项分类：
     a. 有 provider_job_id 且 S26 报 CANCELABLE → 调 S26 取消接口
     b. 有 provider_job_id 但不可取消 → 标 discard_on_completion
     c. 无 provider_job_id（尚未派单）→ 直接取消 Activity
4. 发 signal cancel_requested 到 workflow
5. workflow 等所有 child 到终态 → record_state(CANCELED)
6. 结算：S23 release 未消耗的 reservation、S22 release permit
```

**步骤 2 在步骤 4 之前**是关键的：先 fence，再取消。若反过来，会出现"workflow 已经 canceled 但 S08 还允许新提交"的窗口——在这个窗口里，一个迟到的 `:commit` 会成功，把已取消 Run 的产物写进 manifest。

**`cancel` 不退款**。E22 明确："取消不假退款"。已提交给供应商的任务照常结算（$0 或全额，取决于供应商）。S21 只负责触发结算，不负责金额判断。

**S26 是否报告"可取消"**：S21 不猜。它调 S26 的取消接口，S26 返回 `cancelled` / `not_cancellable` / `unknown`。`unknown` 按 `discard_on_completion` 处理（与 S26 的 UNKNOWN 铁律一致）。

### 4.3 恢复

```
resume 命令
1. 幂等（run_command）
2. **先查 provider_job 现状**：
   - 从 run_checkpoint 的 provider_job_refs 与 node_in_flight 拿所有在途引用
   - 对每个调 S26 GET /generation-jobs/{id}
   - 终态 → 直接推进（不重新生成）
   - 仍在跑 → 重新挂 PROVIDER_JOB_WAIT
   - UNKNOWN → 保持挂起，**不重提**
3. 发 signal resume_requested
4. workflow 从检查点续跑
```

**"恢复先查已存在 provider_job"** 是这句话的全部意义：恢复不是"重新跑一遍"。如果跳过这一步，暂停 1 小时后恢复会重新派单，造成重复付费与重复产物。

**恢复后的第一个 Activity 必须是读，不是写**。代码层面强制：`resume` 的 handler 里第一件事是 `await workflow.execute_activity(query_provider_jobs)`。

### 4.4 `node_epoch` 变化时的部分取消

这是"两套 epoch 不能混用"的落地。

```
消费 snapshot.manifest.committed：
  data.changed_node_epochs = [{node_id:"shot_3_2.video", from:4, to:5}]
  data.execution_fence    = 2（未变）

对每条 {node_id, from, to}：
  1. 找 node_in_flight 中 node_id 匹配 且 node_epoch = from 且 settled_at IS NULL 的项
  2. 有 → 取消该节点在途任务（按 §4.2 的分类 a/b/c）
  3. 重新派单（node_epoch 现在是 to）
  4. **不影响其他节点**：shot_3_4 的 node_epoch 没变，它的在途任务继续
```

**禁止**：`if manifest.revision > binding.manifest_revision_at_start: cancel_all()`。这是把 node_epoch 当 execution_fence 用了。

**唯一需要 fence 的情况**：`data.trigger = FENCE_VOID`，或者 `execution_fence` 从 2 变成 3。那时整个 Run 在这条 branch 上的所有候选都作废，S21 应当把 Run 推向 `CANCELED`——但**即使是这种情况，S21 也是"响应 fence"，不是"发起 fence"**。发起者是取消命令或故障切换流程。

### 4.5 取消分类表

| 在途状态 | 可取消？ | 行为 | 成本 |
|---|---|---|---|
| Activity 尚未派单到 S26 | 是 | 取消 Activity | 0 |
| S26 job `QUEUED`（未提交供应商） | 是 | S26 取消 | 0 |
| S26 job `RUNNING`，供应商支持取消 | 是 | S26 → S27 → provider cancel | 可能计费 |
| S26 job `RUNNING`，供应商不支持 | 否 | `discard_on_completion` | 全额 |
| S26 job `SUBMISSION_UNKNOWN` | **否** | `discard_on_completion` + 保持 UNKNOWN | 可能全额 |
| 等待 QC / 归档 | 是 | 取消，产物变候选 | 已花 |
| 等待人工审核 | 是 | 取消 ReviewTask | 0 |

**`SUBMISSION_UNKNOWN` 不能取消**：这是 S26 的铁律，S21 必须尊重。取消一个 UNKNOWN 的 job 会让"钱可能已经花了但没人跟踪"变成现实。正确行为是 `discard_on_completion`：等它自己有个结论，然后丢弃产物、照常结算。

### 4.6 故障切换（E13 的另一半）

Worker 全挂或 cell 故障后：

```
1. Temporal 的 workflow 仍在（namespace 在别的 cell 或恢复后）
2. 新 worker 起来 → replay → 从中断处继续
3. 不需要 fence（除非旧 cell 的执行器可能还在跑，见 §4.7）
```

**什么时候需要 fence**：docs/16 E34"Region 灾备/旧执行器未知"。当无法确认旧 cell 的执行器已停时，必须：

```
1. 调 S08 :fence（reason=RUN_FAILOVER, fence_ref=run_id）
2. 旧 fence 下的一切提交 VOIDED
3. 新 Run 用新 fence
```

**S21 不自己判断"旧执行器是否还在跑"**。它只能根据运维信号（cell 被判定失联超过 `failover_grace`）决定。这个判断是运维动作，不是 workflow 逻辑。

### 4.7 派单的 UNKNOWN 处理

S21 → S26 的 `POST /generation-jobs` 超时/连接重置：

```
1. 不重试（Temporal retry policy 里该 Activity 设 maximum_attempts=1）
2. 记 attempt_id（由 S21 生成并持久化，作为业务幂等键）
3. 该节点标 blocked_reason = DISPATCH_UNKNOWN
4. Run 进 WAITING_BUDGET，waiting_reason = SUBMISSION_UNKNOWN
5. 定时（或由 S26 事件）查询 attempt_id 现状
6. S26 的最终结论到达后：
   - CREATED/RUNNING → 继续等
   - FAILED → 该节点 blocked
   - 长期 UNKNOWN → 保持挂起，**不重新派单**
```

**`attempt_id` 由 S21 生成还是 S26 生成？** S26 的 §12.1 是 `POST /generation-jobs`，attempt_id 由 S26 在创建时生成。但 S21 在超时时拿不到它。

解法：S21 在调用前生成 `dispatch_request_id`（作为请求体的 `client_dispatch_id`），并持久化在 `node_in_flight.attempt_id`。超时后用这个 id 查 S26 "有没有用这个 id 创建过 attempt"。**这要求 `POST /generation-jobs` 支持客户端提供的幂等键**——S26 的契约里 `command_id` 已经承担这个角色（§9 C21-6）。

---

## 5. 对外契约

### 5.1 `POST /episodes/{episode_id}/runs`

```jsonc
{
  "context": { /* execution-context.v2 完整对象 */ },
  "budget_scope_id": "budget_ep3",
  "command_id": "cmd_01H..."
}
```

响应 `202`：

```jsonc
{
  "operation_id": "op_...",
  "state": "accepted",
  "run_id": "run_01H...",
  "replayed": false
}
```

`state` 取 `Operation.state` 枚举（`accepted|waiting_input|waiting_review|waiting_budget|waiting_memory`）。`generation_mode=manual` 时返回 `waiting_input`。

**`operation_id` 与 `run_id` 是两个 id**：前者标识"这次请求"，后者标识"这次生产"。同一 Run 可以有多个 operation（创建后又有 pause/resume）。openapi 的 `Operation` 有可选 `run_id`，这里必须填。

### 5.2 `GET /runs/{run_id}`

响应 `200`，严格符合 openapi `Run`：

```jsonc
{
  "run_id": "run_01H...",
  "manifest_revision": 13,
  "execution_fence": 2,
  "state": "RUNNING",
  "completed_nodes": 41,
  "blocked_nodes": 1,
  "projection_watermark": 1287
}
```

**这六个字段是契约的下界，不是上界**。S21 内部还有 `failed_nodes`、`waiting_reason`、`current_stage`——它们**不能**加进这个响应（`additionalProperties:false`）。要暴露它们需要新增查询接口（§9 C21-2）。

### 5.3 `POST /runs/{run_id}/commands`

```jsonc
{ "action": "pause", "command_id": "cmd_01H..." }
```

`action ∈ {pause, resume, cancel}`。

响应 `202`：`{operation_id, state:"accepted", run_id}`。

**`action` 之外没有参数**。`cancel` 不能带 "只取消某几个节点"——那是 S39/局部修改的语义，走 ChangePlan，不走 Run 命令。这是一个刻意的收窄：Run 级命令只有三个，且都是全局的。

### 5.4 补充查询（契约缺口，§9 C21-1/2）

按 S39 C6 与运维需要，本服务需要以下查询。**它们目前不在 openapi，本文按建议形态描述**：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/runs/{run_id}/nodes?state=&stage=&after=` | 节点级进度（含 `node_epoch`、`blocked_reason`、`attempt_id`） |
| `GET` | `/runs/{run_id}/blockers` | 失败清单（E06 的失败报告数据源） |
| `GET` | `/runs/{run_id}/repair-budget` | **S39 C6**：`{remaining_business_attempts, remaining_recheck_rounds, budget_exhausted}` |
| `GET` | `/runs?episode_id=&branch_id=&state=&after=` | 列表 |
| `POST` | `/runs/{run_id}:signal` | 内部：S21 消费事件后 signal 到 workflow（由本服务自己的 event consumer 调用，不对外） |

`GET /runs/{run_id}/repair-budget` 的响应：

```jsonc
{
  "run_id": "run_01H...",
  "remaining_business_attempts": 3,
  "remaining_recheck_rounds": 1,
  "budget_exhausted": false,
  "ceiling_micro": 9000000,
  "spent_micro": 6120000,
  "source_watermark": 1287
}
```

`budget_exhausted` 的判定是"`remaining_business_attempts == 0` **或** 本地预检认为 `ceiling_micro - spent_micro` 不足以支撑一次最小修复"。后者是估算，S39 拿到后仍要问 S23。**S21 不把估算当拒绝依据**。

### 5.5 发布事件 `orchestration.run.state-changed`

Topic `drama.orchestration.v2`，`aggregate_id = run_id`，`aggregate_sequence = projection_watermark`。

```jsonc
{
  "specversion":"1.0","schema_version":"2.0","id":"evt_...","source":"S21",
  "type":"orchestration.run.state-changed","subject":"run/run_01H...",
  "time":"2026-11-01T09:11:00Z",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"run_01H...","aggregate_sequence":1287,
  "correlation_id":"cmd_...","causation_id":"evt_...",
  "traceparent":"00-...-...-01",
  "data": {
    "run_id":"run_01H...",
    "previous_state":"RUNNING",
    "state":"WAITING_REVIEW",
    "manifest_revision":13,
    "execution_fence":2,
    "completed_nodes":41,
    "blocked_nodes":1,
    "failed_nodes":0,
    "waiting_reason":"QC_REQUIRES_HUMAN",
    "blocked_reason":null,
    "current_stage":"SHOT_QC",
    "terminal":false
  },
  "data_ref": null
}
```

**每次状态变更都发**（不只是终态）。消费者：

| 消费者 | 用途 |
|---|---|
| S47 | 实时面板；SSE 的数据源 |
| S39 | 知道 Run 到了哪个 stage，决定修复计划是否可以下发 |
| S44 | 产能与返工分析（终态事件） |
| S03 | 项目进度（终态事件） |

**`aggregate_sequence` 用 `projection_watermark`** 而不是"第 n 次状态变更"。理由：投影每次写都会 +1（包括非状态变化的更新，如计数），所以 sequence 是稠密的、无缺口的，S47 的缺口检测才有意义。

### 5.6 消费的事件

| 事件 | 源 | 处理 | 幂等 |
|---|---|---|---|
| `generation.job.state-changed` | S26 | signal 到对应 workflow（按 `attempt_id` → `node_in_flight` 反查 run_id） | `s21.jobs` inbox |
| `snapshot.manifest.committed` | S08 | `trigger=COMMIT` 且 `changed_node_epochs` 非空 → 部分取消 + 子图（§4.4） | `s21.manifest` inbox |
| `scheduler.permit.changed` | S22 | permit 被外部释放/过期 → 影响该节点的判定 | `s21.permits` inbox |
| `review.decision.recorded` | S40 | 若该 ReviewTask 属于本 Run → signal resume/wait | 按 `task_id` |
| `change.plan.applied` | S07 | 若为 change_subgraph，准备子图执行 | `s21.changes` inbox |

**S21 不消费 `lineage.impact.computed`**。影响分析经 S07 的 ChangePlan 进来；直接消费 S09 的事件会让 S21 在计划还没被批准时就开始排程。

### 5.7 错误码

| HTTP | code | retryable | 说明 |
|---|---|---|---|
| 400 | `INVALID_CONTEXT` | false | execution-context 缺字段/形态错 |
| 403 | `TENANT_MISMATCH` | false | header 与认证不一致 |
| 404 | `RUN_NOT_FOUND` | false | 跨租户同码 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同键不同 body |
| 409 | `STALE_EXECUTION_FENCE` | true | 请求里的 fence 已过期 |
| 409 | `RUN_NOT_ACTIVE` | false | 对终态 Run 发命令 |
| 409 | `ILLEGAL_STATE_TRANSITION` | false | 如对 `PAUSED` 发 `pause` |
| 422 | `EPISODE_TOO_LARGE` | false | 镜头数超上限 |
| 422 | `POLICY_DENIES_GENERATION` | false | `generation_mode=manual` 且调用方要求自动 |
| 422 | `DEPENDENCY_PRECHECK_FAILED` | false | 预算/配额/权限预检明确失败 |
| 503 | `TEMPORAL_UNAVAILABLE` | true | Temporal 不可达 |
| 503 | `DEPENDENCY_UNAVAILABLE` | true | S08/S02 不可达 |

**注意 `422` 与 `409` 的分工**：`422` 是"这个请求本身不可行"（不重试有意义），`409` 是"现在的状态不允许，但改一下可能行"。docs/13 §1："不可行 422；明确 budget/permission 错误不能伪装可重试 500"。

---

## 6. 幂等与并发

### 6.1 创建 Run 的幂等（三层）

| 层 | 键 | 作用 |
|---|---|---|
| 命令层 | `(tenant_id, command_id)` + `request_hash` | 同一次请求重试不重复创建 |
| 业务层 | `(tenant_id, business_idem_key)` where `status=ACTIVE` | 同一次"意图"不产生第二个 Run |
| 编排层 | `workflow_id` 确定性 | `start_workflow` 重试不产生第二个 workflow |

三层的必要性：只做命令层，则"用户点了两次开始，command_id 不同"会创建两个 Run；只做业务层，则取消后重新开始会撞唯一索引；只做编排层，则 Temporal 里的重复 workflow 无法被业务查询发现。

**业务层唯一索引与"取消后重建"的冲突**：`status=ACTIVE` 的部分索引解决了它——旧 Run 是 `CANCELED`（`status=TERMINATED`），不在索引里，新 Run 可以创建。**这正是为什么 `status` 有 `TERMINATED` 而不只是 `COMPLETED`**。

### 6.2 命令的幂等

```sql
-- 与业务写入同事务
INSERT INTO idempotency_record(tenant_id, command_id, request_hash, state)
VALUES ($t, $c, $h, 'IN_PROGRESS')
ON CONFLICT (tenant_id, command_id) DO NOTHING
RETURNING command_id;
-- 无返回 → 已存在 → 读旧记录：
--   同 hash + DONE    → 返回首次响应（replayed:true）
--   同 hash + IN_PROGRESS → 409 COMMAND_IN_PROGRESS
--    不同 hash          → 409 IDEMPOTENCY_KEY_REUSED
```

### 6.3 `record_state` 的幂等

```sql
UPDATE run_projection
   SET state = $state, ..., projection_watermark = projection_watermark + 1,
       last_event_at = now(), version = version + 1
 WHERE tenant_id = $t AND run_id = $r
   AND projection_watermark = $expected_watermark    -- CAS
RETURNING projection_watermark;
```

**用 CAS 而不是"直接覆盖"** 的原因：Activity 可能重试，重试时必须知道"上一次到底成没成"。`projection_watermark` 由 workflow 在调用前读出并作为参数传入（确定性），重试时带同一个 expected 值——第一次成功则第二次返回 0 行，workflow 认为已成功。

**`projection_watermark` 的递增**：`projection_watermark + 1` 在 SQL 里做，不在 workflow 里算。workflow 只提供 expected 值。

### 6.4 并发：同一 Run 的命令串行

`pause` / `resume` / `cancel` 必须串行。实现不是行锁，是 **workflow 单一事件循环**：

- 三个命令都通过 signal 进入 workflow（单线程确定性执行）
- workflow 按 signal 到达顺序处理
- 投影的 CAS 保证即使 workflow 重启，也不会出现两个并发写

**API 侧不需要分布式锁**。两个并发的 `pause` 会产生两个 signal，workflow 处理第一个后状态已是 `PAUSED`，第二个触发 `ILLEGAL_STATE_TRANSITION`（但幂等表里 command_id 不同，所以是真实的第二个命令，返回 409 而不是重放）。

**`pause` 与 `cancel` 并发**：先到的赢；后到的如果是 `cancel`，从 `PAUSED` 到 `CANCELED` 是合法的（§2.6 的"任意非终态 → CANCELED"）。所以拒绝 pause 之前先看 cancel 有没有排上队——这一点在 workflow 里是自然顺序的，不需要额外逻辑。

### 6.5 读隔离与投影的"旧读"

`GET /runs/{run_id}` 读的是投影，可能比 Temporal 滞后几秒（`record_state` 的传播）。

- 投影带 `projection_watermark`，S47 可以判断"我在看多新的"
- **不提供"strong read"**（直接查 Temporal）。理由：撞到 Temporal 的读限流会拖垮整个 Run 的调度；而"刚才 pause 了没"这个问题的答案不需要毫秒级
- 例外：`GET /runs/{run_id}` 在 `state` 是终态后必须是准确的——终态写入 `record_state` 后，workflow 才 return。所以终态之前的滞后不影响"这个 Run 结束了没"

---

## 7. 可观测

### 7.1 指标

| 指标 | 类型 | 用途 |
|---|---|---|
| `s21_run_active{automation_mode,workflow_type}` | gauge | 与 docs/03 的 5,000 活跃 Run 对比 |
| `s21_run_terminal_total{state,automation_mode}` | counter | `INCOMPLETE` 占比是核心质量指标 |
| `s21_run_duration_seconds{workflow_type,terminal_state}` | histogram | 按 P50/P95/P99 |
| `s21_node_in_flight{stage}` | gauge | 在途节点数，与 S22 的 permit 在途对账 |
| `s21_dispatch_unknown_total` | counter | **> 0 就要查**；对应 S26 的 UNKNOWN |
| `s21_dispatch_total{result}` | counter | `created/unknown/rejected` |
| `s21_waiting_total{reason}` | gauge | `WAITING_BUDGET` 长期高说明预算配置有问题 |
| `s21_continue_as_new_total{workflow_type}` | counter | 换代频率 |
| `s21_history_length{workflow_type}` | histogram | 接近阈值要提前换代的信号 |
| `s21_activity_retry_total{activity,attempt}` | counter | 某 Activity 重试暴增 = 依赖服务不稳 |
| `s21_signal_lag_seconds{event_type}` | histogram | 事件到 signal 的延迟 |
| `s21_timer_reconcile_total{result}` | counter | timer 兜底触发的比例；高了说明 signal 丢 |
| `s21_projection_rebuild_total` | counter | 投影重建次数，> 0 需排查 |
| `s21_command_total{action,result}` | counter | pause/resume/cancel |

### 7.2 与 S22/S26 的对账指标

**必须有的跨服务对账**：

```
s21_node_in_flight 中 provider_job_id 非空的项数
  vs
S26 的 job 在途数（按 run_id 过滤）
```

差异 > 1% 持续 5 min → 告警。差异的常见原因：S21 的 `settled_at` 没写、S26 的 job 已终态但事件丢了。这条对账是 E22"取消不假退款"的证据之一：如果 S21 认为取消了但 S26 还在跑，钱会继续花。

### 7.3 日志与追踪

- 每个 Run 一条"生命周期摘要"日志：创建、每个状态变更、终态、耗时、节点计数
- 每个 `DISPATCH_UNKNOWN` 一条 WARN，带 `run_id` / `node_id` / `attempt_id` / `dispatch_request_id`
- `traceparent` 从 API 透传到 Activity，再透传到 S26/S23/S22 的调用
- 不在日志里打印 prompt、剧本、媒体 URL

### 7.4 运维入口

| 场景 | 手段 |
|---|---|
| Run 卡住 | 先 `GET /runs/{id}`（投影）→ 再 Temporal `describe_workflow_execution` → 再 `GET /runs/{id}/blockers` |
| 投影滞后 | 对比 `projection_watermark` 与事件的 `aggregate_sequence` |
| workflow 非确定性 | Temporal 的 `WorkflowTaskFailed` 事件；用 `reset` 到出错前的 event id |
| 全 cell 停机 | Temporal namespace 恢复 → worker 起来 → replay；投影重建 |

---

## 8. 测试清单

### 8.1 E06 · 无人值守关键镜头失败

| 编号 | 场景 | 断言 |
|---|---|---|
| T21-01 `[E06]` | `unattended_batch` + `isolate_branch`，镜头 3 的素材缺失 | 镜头 3 `blocked`；其他镜头 `SUCCEEDED`；Run 终态 `INCOMPLETE` |
| T21-02 `[E06]` | 同上，查 `GET /runs/{id}/blockers` | 返回镜头 3 的具体原因与建议 |
| T21-03 `[E06]` | 同上，尝试生成正式交付 | 被拒（`no_incomplete_formal_delivery=true`） |
| T21-04 `[E06]` | INCOMPLETE 后补素材并新建 Run | 新 Run 只跑缺失节点（manifest 里有已接受的产物） |
| T21-05 `[E06]` | `guided` 档位，关键镜头失败 | `WAITING_REVIEW`，**不是** INCOMPLETE（档位决定行为） |

### 8.2 E13 · S08 提交前后崩溃

| 编号 | 场景 | 断言 |
|---|---|---|
| T21-10 `[E13]` | `:commit` Activity 超时 | **不重试**；Run 进 `WAITING_*`；用 `change_plan_ref` 查 S08 得到实际结果 |
| T21-11 `[E13]` | `:commit` 成功但 Activity 响应丢失 | 重试带同 `command_id` → S08 返回 `replayed:true`；S21 继续 |
| T21-12 `[E13]` | workflow 在 stage 中间崩溃后 replay | 从最后一次 `record_state` 之后重放；**不重复调用已成功的写命令** |
| T21-13 `[E13]` | Continue-As-New 期间崩溃 | 检查点已写；新代从检查点续跑；无重复派单 |
| T21-14 `[E13]` | 投影写失败 | workflow 继续；投影滞后；重建后一致 |

### 8.3 E22 · 暂停取消不可取消任务

| 编号 | 场景 | 断言 |
|---|---|---|
| T21-20 `[E22]` | pause 时 12 个在途 job | 不取消任何 job；全部归档结算后才 `PAUSED` |
| T21-21 `[E22]` | pause 时查 `GET /runs/{id}` | `PAUSED` 之前是 `RUNNING`，不出现"暂停中途丢弃在途任务" |
| T21-22 `[E22]` | cancel 时部分 job 支持取消 | 调 S08 `:fence`（先）→ 再取消可取消的 → 其余 `discard_on_completion` |
| T21-23 `[E22]` | cancel 后查账 | 已提交任务的费用照常结算，**不产生退款记录** |
| T21-24 `[E22]` | `SUBMISSION_UNKNOWN` 的 job 遇 cancel | 不调取消接口；标 `discard_on_completion`；状态保持 UNKNOWN |
| T21-25 `[E22]` | cancel 后迟到的事件 | 终态不倒退（`CANCELED` 保持） |
| T21-26 `[E22]` | 先 fence 再 cancel 的顺序 | 在 fence 完成后、cancel 前，S08 拒绝新提交（用测试用的假延迟验证窗口不存在） |
| T21-27 `[E22]` | resume 时有 3 个在途 job | **先查** `GET /generation-jobs/{id}`；终态的推进、在跑的重新挂等、UNKNOWN 不动 |

### 8.4 E33 · Temporal 升级 / 全部 Worker 停机

| 编号 | 场景 | 断言 |
|---|---|---|
| T21-30 `[E33]` | 全部 worker 停机 30 min 后恢复 | workflow replay 续跑；无重复派单；`node_in_flight` 与 S26 一致 |
| T21-31 `[E33]` | 同一 history 在两个 worker 版本上 replay | 结果逐字节一致（确定性测试） |
| T21-32 `[E33]` | history 超过 20k events | Continue-As-New 在 stage 边界触发；新代从检查点续跑 |
| T21-33 `[E33]` | 投影表被清空 | 从 Temporal Query 重建；`projection_watermark` 不回退 |
| T21-34 `[E33]` | 升级后 signal 丢失 | timer 兜底对账；最终状态正确 |

### 8.5 通用

| 编号 | 场景 | 断言 |
|---|---|---|
| T21-40 `[E12]` | 同 command_id 同 body 创建 Run | 返回同 `run_id`；不启动第二个 workflow |
| T21-41 `[E12]` | 同 command_id 不同 body | 409 `IDEMPOTENCY_KEY_REUSED` |
| T21-42 业务幂等 | 两次不同 command_id、同 manifest/意图 | 第二次返回 409 `RUN_ALREADY_ACTIVE` 或原 Run（按契约决定） |
| T21-43 取消后重建 | cancel 后同 key 再创建 | 成功（旧 Run 是 TERMINATED，不在部分索引内） |
| T21-44 `[E07]` | 局部修改：只有 `shot_3_2` 的 epoch 变 | 只取消 `shot_3_2` 的在途任务；`shot_3_4` 继续 |
| T21-45 `[E07]` | `execution_fence` 从 2 变 3 | 整 Run 的候选作废；Run 推向 CANCELED |
| T21-46 两套 epoch | manifest revision 前进但 fence 与 node_epoch 都没变 | **不取消任何任务**，不启动子图（只有 accepted pointer 变） |
| T21-47 `[E28]` | 跨租户查 Run | 404，与不存在同码 |
| T21-48 `[E01]` | 独立升级 S21 | 不需要 S26/S08 同时升级；事件 schema 向后兼容 |
| T21-49 扇出 | 一集 800 镜头 | 并发不超过 `MAX_CONCURRENT_SHOTS_PER_EPISODE`；全部完成 |
| T21-50 派单 UNKNOWN | `POST /generation-jobs` 超时 | `maximum_attempts=1`；`dispatch_unknown_total` +1；不产生第二次 HTTP |

---

## 9. 契约冲突与待裁决（W0 必修）

| # | 冲突 | 本文采用 | 需要谁改 |
|---|---|---|---|
| C21-1 | `service.yaml` / catalog 写 `POST /runs`、`GET /runs/{id}`；`contracts/openapi.v2.yaml` 写 `POST /episodes/{episode_id}/runs`（episode 在 path，`context.episode_id` 也在 body，两处冗余） | openapi 形态（更 RESTful） | 统一 catalog 或 openapi，二选一 |
| C21-2 | `Run` schema `additionalProperties:false`，只有 6 个字段；`INCOMPLETE` 的失败报告、`waiting_reason`、节点级进度都无处可查 | 新增 §5.4 的 5 条查询；**不改** `Run` | `contracts/openapi.v2.yaml` |
| C21-3 | `Operation.state` 只有 `accepted\|waiting_input\|waiting_review\|waiting_budget\|waiting_memory`，与 `Run.state` 的 11 值**不是同一套词汇表**，且没有 `succeeded`/`failed`/`canceled`。异步操作完成后状态无处表达 | Operation 只用于"受理瞬间"，后续一律查 `GET /runs/{id}` | 契约注释说明；或给 Operation 加终态 |
| C21-4 | `GET /runs/{run_id}/events` 的 `x-service-id` 是 **S47** 不是 S21。同一路径下混合了两个服务的所有权 | SSE 归 S47；S21 只提供 `run_projection` 与事件 | 契约注释；文档里写清"读 Run 归 S21，流归 S47" |
| C21-5 | catalog 只声明一个事件 `orchestration.run.state-changed`，但 docs/07 §4 要求暂停/恢复/取消/INCOMPLETE 全部可观测。本文把全部信息塞进这一个事件的 `data` | 单事件多字段 | 若需独立事件（如 `run.blocked`），扩 topic 目录 |
| C21-6 | `POST /generation-jobs`（S26）的超时恢复依赖"S21 提供的幂等键"，但 S26 §12.1 的 body 是否支持客户端 `dispatch_request_id` 未定 | 用 `command_id` 承担 | S26 契约；`contracts/` |
| C21-7 | **S39 C6**：S21 缺"剩余修复额度"查询接口 | §5.4 `GET /runs/{id}/repair-budget` | `contracts/openapi.v2.yaml` |
| C21-8 | **S39 C3**：无 `contracts/quality/repair-plan.v2.schema.json`，S21 无法机器读取修复计划 | S21 依赖它；缺失时只能按 `blocked` 处理 | `contracts/quality/` |
| C21-9 | `automation-policy.v2` 的 `overrides[].scope_type` 无 `run`（S02 方案 G1 已登记）。docs/06 §2 写"单集/Run覆盖"，schema 表达不了 Run 级覆盖 | 按 episode 覆盖近似；**不自行加枚举** | `contracts/automation-policy.v2.schema.json` |
| C21-10 | `execution-context.v2` 的 `node_id` + `node_epoch` 是**单节点**语义，但创建 Run 是 **集级**操作。用集级请求携带一个节点的 epoch 是语义错误的 | 创建 Run 时忽略这两个字段的"当前值"语义，只做 fence 校验；节点级上下文由 workflow 在派单时重新组装 | 需要"集级 execution context"或把这两个字段改为可选 |
| C21-11 | `slo.verified: false` 与 docs/03 的规划量级（5,000 活跃 Run / 52 次/秒峰值）都没有实测。`MAX_CONCURRENT_SHOTS_PER_EPISODE=24` 等默认值是基于这些未验证值的推导 | 全部标为可配、默认保守 | 容量验证后再定 |

---

## 10. 未决问题

1. **`MAX_CONCURRENT_SHOTS_PER_EPISODE` 的正确值**。24 是按 5,000 活跃 Run × 平均每集 200 镜头 ÷ 供应商配额反推的粗估。真实值取决于 S22 的池大小与 S25 的路由结果。在容量验证（E35）前不能承诺。
2. **Continue-As-New 的 stage 边界是否总是存在**。若某个 stage 本身极长（如整集混音），换代仍可能被迫在 stage 中间。需要一个"强制换代点"的兜底设计与测试。
3. **Temporal 版本兼容（E33）**。`workflow.patched()` 的使用规范没有在本文展开。升级策略（`AutoUpgrade` vs `Pinned`）需与运维共同定。
4. **暂停期间的容量占用**。`PAUSED` 的 Run 仍持有在途 job 与 permit，按 docs/03 的容量规划这会挤占别的 Run。是否需要"暂停超时自动降级为取消"？目前没有。
5. **`INCOMPLETE` 的 Run 会不会永远不清理**。失败报告留着，但 Temporal 的 history 有保留期。报告需要落到持久存储（哪一层？S45 审计？）——本文未定。
6. **`SeriesBatch` 是否要做**。docs/07 §4 列了它，但当前 service.yaml 的入口是 episode 级。批量生产可能由 S46 循环调用 episode Run，而不需要真正的 SeriesBatch workflow。这与 docs/07 有张力，需要裁决。
7. **`repair_budget` 与 S39 计数的一致**。S21 记 Run 内的次数，S39 记跨 Run 的累计。两者的 +1 时机都是"attempt 开始"（S39 §5.5）。若 S21 与 S39 的计数不同步（一个 +1 了另一个没有），上限会被绕过。需要一条对账规则，本文未定。
