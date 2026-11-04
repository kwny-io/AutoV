# S07 · 对话修改与变更计划 —— 实现方案

状态：实现方案（W1）。`maturity: design_only` 不变；本文描述"照着能写代码"的形态，不是已实现系统。

上游依据：[docs/01](../docs/01-enterprise-architecture.md) §3 唯一权威表；[docs/04](../docs/04-data-consistency.md) §1 epoch、§2 修改与接受 Saga；[docs/05](../docs/05-cross-episode-memory.md) §5 改前集；[docs/06](../docs/06-automation-governance.md) §3 门与决定；[docs/07](../docs/07-production-workflows.md) §2 对话局部修改、§3 跨集改稿；[docs/16](../docs/16-migration-acceptance.md) E03 / E04 / E08；[S07 规格](../docs/design-package/services/S07-change-management.md)。

配套：[S09 影响分析](S09-lineage-impact.md)、[S08 版本快照](S08-revision-snapshot.md)、[S06 分镜](../services/storyboard/service.yaml)、[S16 跨集记忆](S16-story-memory.md)、[S02 治理策略](S02-governance-policy.md)、[S21 编排](S21-orchestration.md)、[S25 路由估价](S25-model-routing.md)。

---

## 0. 边界与一句话职责

S07 是 **ChangePlan** 的唯一写者：把一句人话（"第 3 场第 2 镜改成仰拍，时长缩到 4 秒"）变成一份**可校验、可估价、可过期、可审计**的变更方案，并在用户确认后按 Saga 推进到"内容已准备好、等待 S08 提交"。

它回答一个问题：**"我照这个改，会发生什么？"** —— 附带明确的前置条件、影响范围、成本上界和阻塞项。

### 0.1 不做什么

| 不做 | 为什么 | 谁做 |
|---|---|---|
| **不写 accepted pointer** | S08 是唯一写者（AGENTS.md 绝对禁止"自己存一份 accepted pointer"） | S08 |
| **不写 `node_epoch`、不写 `execution_fence`** | 同上。S07 只能**声明**"这些节点该重做"，epoch 由 S08 在 `:commit` 时按 `input_fingerprint` 自行推进 | S08 |
| 不改剧本 / 分镜 / 资产的**内容** | 内容归各所有者；S07 只发起"准备新 revision"的请求，不替它们写 | S04/S05/S06/S10 |
| 不算影响闭包 | 影响分析归 S09（S16 §5.1 的职责表已钉死） | S09 |
| 不判"能不能自动应用" | 政策归 S02 | S02 |
| 不预留预算、不下发任务 | 金额归 S23、任务归 S26、编排归 S21 | S23/S26/S21 |
| 不改记忆 checkpoint | S16 | S16 |
| 不合并冲突、不裁决正史 | S16 输出冲突清单，S40/规则裁决 | S16/S40 |
| 不做 prompt、不做模型选择 | S20/S25 | S20/S25 |

判据：**S07 的产物是一份"如果执行会怎样"的说明加一组"已经准备好的新 revision 引用"。它不是执行结果，也不是一次授权。**

### 0.2 三件绝对不能做

1. **不能把"计划已生成"当成"已修改"**。ChangePlan 是 `DRAFT`/`PLANNED` 时，manifest 一点没变。唯一改变生产状态的动作是 S08 的 `:commit`，而 S07 只是调用方。
2. **不能在缺集上下文时猜目标**。docs/07 §2："没有集上下文或4秒无法容纳对白时返回具体澄清选项，自动化档位不授权随意删台词。" E04 就是这条。
3. **不能把跨集改稿做成"改一个字段导致全剧悄悄重做"**。docs/07 §3："S07 先展示跨集影响。" 展示在前，执行在后；用户在没看到影响清单之前，没有任何东西被改。

### 0.3 两套 epoch 里 S07 的角色

| 概念 | 作用域 | 谁写 | S07 做什么 |
|---|---|---|---|
| `execution_fence` | run / branch | **S08** | **只读，且只做一致性校验**。ChangePlan 的 `context.execution_fence` 是生成计划时读到的值；`apply` 时若 S08 的 fence 已变，说明期间发生过取消/故障切换，计划作废（409 `STALE_EXECUTION_FENCE`）。S07 永不递增 fence。 |
| `node_epoch` | 单个 `logical_node` | **S08** | **不读不写，只在影响结果里出现 node_id**。`impact.regenerate_node_ids` 是"这些节点应该重做"，不是"这些节点的 epoch 会变成 X"。epoch 由 S08 在 `:commit` 时决定，S07 在 `:commit` 的响应里**才知道**实际变了哪些。 |
| `manifest_revision` | manifest | **S08** | **CAS 的期望值**。`apply` 必须带 `expected_manifest_revision`；不匹配 409。plan 里固定 `base_manifest_ref`，`plan_hash` 覆盖它，防篡改。 |

**最容易写错的地方**：把 `regenerate_node_ids` 当成"epoch 变更清单"传给 S08。S08 的 `:commit` 接受的是完整 `candidate_manifest`，epoch 由它自行判断；S07 给的是**意图**（哪些节点应该被重做），不是**结果**（哪些 epoch 变成几）。两者的差别在 §4.4 展开。

---

## 1. 核心对象与关键区分

### 1.1 六个对象

| 对象 | 是什么 | 不是什么 |
|---|---|---|
| `change_plan` | 一次修改的完整方案：解析结果、补丁、影响、成本、阻塞、有效期 | 不是授权，不是执行 |
| `change_plan_patch` | 字段级补丁（`target_ref` + `path` + `before` + `after`） | 不是内容 diff（不含正文） |
| `clarification_request` | 无法唯一解析时的具体问题与选项 | 不是错误（它是可回答的） |
| `change_saga` | 一次 `apply` 的推进过程 | 不是 workflow（它是 Saga 记录） |
| `change_saga_step` | Saga 的每一步及其结果 | 不是 outbox（它是审计） |
| `impact_summary` | 展示给用户的跨集影响摘要（含成本区间） | 不是 S09 的闭包（它由闭包派生） |

### 1.2 ChangePlan 与 ChangeSaga 的界限

```
POST /change-plans            → 生成 change_plan（可多次，每次一个 plan）
POST /change-plans/{id}:apply → 创建 change_saga，推进到"等 S08 提交"
```

| | ChangePlan | ChangeSaga |
|---|---|---|
| 生命周期 | 到 `expires_at` 为止 | 到 `APPLIED` / `FAILED` / `SUPERSEDED` |
| 可变性 | 生成后不可变（`plan_hash` 固定） | 逐步追加 `change_saga_step` |
| 幂等对象 | `(tenant, command_id)` 与内容 `request_hash` | `(tenant, plan_id)` 只能有一个生效 Saga |
| 失败后 | 重新生成一个新 plan | 保留记录，供审计 |

**plan 不可变是刻意设计**：用户点"应用"时，他确认的是那份他看过的计划。若计划可变，"我确认的东西"与"实际执行的东西"就会不一致，`plan_hash` 就失去意义。

### 1.3 为什么需要 `plan_hash`

`apply` 的 body 是 `{expected_manifest_revision, plan_hash, command_id}`。三道闸：

1. **`plan_hash`** = `sha256(JCS(plan 去掉 plan_hash 与 created_at))` → 确认"你确认的就是这份"。防止一个过期的 UI 页面提交了一份已经被重新生成过的计划。
2. **`expected_manifest_revision`** → 确认"基础没动"。防止 S08 已经前进了还按旧基础改。
3. **`expires_at`** → 确认"估价还新鲜"。成本区间随供应商价格与政策版本变化。

三者任一不符即 409，且**不自动重算**（S07 不会"贴心"地帮你用新基础重新生成一份计划然后执行——那是无人确认的修改）。

### 1.4 `clarification_request` 为什么是一等公民

E04 的两种情形都必须**可回答**，而不是"无法处理"：

| 情形 | 澄清内容 | 绝不做的 |
|---|---|---|
| 缺集上下文（"第3场第2镜"出现在多集） | 列出候选集 + 每集的镜头摘要引用，请选一个 | 默认选当前集 / 选最近一集 |
| 4 秒容不下对白（对白最短需 5.2 秒） | 列出具体选项：①延到 5.2 秒（成本 +X）②保留 4 秒但重写对白（需 S04 参与，额外成本）③取消 | 自动删台词 / 自动截断音频 / 静默把时长改回 5.2 |

选项必须带**可量化的后果**（时长、成本区间、涉及的节点）。"请提供更多信息"不是澄清，是推卸。

docs/06 §1 的"unattended 不意味着允许删对白"正是这条的硬约束：**即使是无人值守模式，也不能自动选"删台词"这个选项**。

---

## 2. 数据模型

数据库 `change_management`，Postgres 16。全部表带 `tenant_id`；跨租户一律 404。

### 2.1 `change_plan`

```sql
CREATE TABLE change_plan (
  tenant_id        text NOT NULL,
  plan_id          text NOT NULL,
  plan_hash        text NOT NULL,
  branch_id        text NOT NULL,
  series_id        text NOT NULL,
  episode_id       text NOT NULL,          -- 主目标集（跨集时是修改发起的那一集）
  instruction      text NOT NULL,          -- 原始人话
  parsed_intent    jsonb NOT NULL,         -- 结构化解析结果，见 §4.1
  base_manifest_ref jsonb NOT NULL,        -- {id, revision, content_hash}
  base_revision    integer NOT NULL,       -- = base_manifest_ref.revision，冗余便于索引
  context_snapshot jsonb NOT NULL,         -- execution-context 的关键子集（只读快照）
  execution_fence  bigint NOT NULL,        -- 生成计划时读到的值，只读
  memory_checkpoint jsonb NOT NULL,        -- {branch_id, commit_sequence, content_hash}
  policy_version   text NOT NULL,          -- 生成计划时 pin 的策略 revision
  policy_ref       jsonb NOT NULL,
  impact_analysis_id text,                 -- S09 的 analysis_id
  impact_watermark bigint NOT NULL,
  state            text NOT NULL,
  memory_migration_required boolean NOT NULL DEFAULT false,
  estimated_cost_upper_micro bigint NOT NULL,
  currency         text NOT NULL,
  saga_id          text,                   -- 唯一生效 Saga
  created_by       jsonb NOT NULL,         -- actor
  created_at       timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL,
  applied_at       timestamptz,

  PRIMARY KEY (tenant_id, plan_id),
  CONSTRAINT cp_state_closed CHECK (state IN (
    'DRAFT','PLANNING','PLANNED','CLARIFICATION_REQUIRED','POLICY_DENIED',
    'INFEASIBLE','EXPIRED','APPLYING','APPLIED','FAILED','SUPERSEDED')),
  CONSTRAINT cp_hash_shape CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT cp_base_revision_positive CHECK (base_revision >= 1),
  CONSTRAINT cp_fence_positive CHECK (execution_fence >= 1),
  CONSTRAINT cp_money_nonneg CHECK (estimated_cost_upper_micro >= 0),
  CONSTRAINT cp_currency_shape CHECK (currency ~ '^[A-Z]{3}$'),
  CONSTRAINT cp_ttl CHECK (expires_at > created_at),
  CONSTRAINT cp_applied_has_ts CHECK ((state = 'APPLIED') = (applied_at IS NOT NULL))
);

CREATE INDEX cp_by_episode_idx
  ON change_plan (tenant_id, series_id, branch_id, episode_id, created_at DESC);
CREATE INDEX cp_open_idx ON change_plan (tenant_id, state)
  WHERE state IN ('PLANNED','CLARIFICATION_REQUIRED','APPLYING');
CREATE UNIQUE INDEX cp_hash_uniq ON change_plan (tenant_id, plan_id, plan_hash);
```

**`context_snapshot` 只存关键子集**，不存完整 `execution-context`：存的是 `tenant_id, project_id, series_id, branch_id, episode_id, home_cell, manifest_ref, execution_fence, memory_checkpoint, policy_ref, actor`。**不存 `node_id` / `node_epoch`**——一次修改通常跨多个节点，把某个节点的 epoch 塞进计划会误导（§9 C07-2）。

**`base_revision` 冗余**：`base_manifest_ref->>'revision'` 上的索引在 Postgres 里可行但不如独立列直观，且 CAS 校验是热路径。冗余一份，由写入时从 jsonb 取，不手填。

### 2.2 `change_plan_patch`

```sql
CREATE TABLE change_plan_patch (
  tenant_id     text NOT NULL,
  plan_id       text NOT NULL,
  patch_seq     integer NOT NULL,
  target_service_id text NOT NULL,
  target_id     text NOT NULL,
  target_revision integer NOT NULL,       -- 修改前（before）的 revision
  target_hash   text NOT NULL,
  path          text NOT NULL,            -- JSON Pointer
  before_value  jsonb NOT NULL,
  after_value   jsonb NOT NULL,
  derived_from  text,                     -- NULL = 用户直接要求；非空 = 派生（指向 patch_seq）
  derivation_rule text,                  -- 派生规则名，例如 END_PAUSE_FROM_DURATION

  PRIMARY KEY (tenant_id, plan_id, patch_seq),
  CONSTRAINT cpp_path_shape CHECK (path ~ '^(/[^/]*)+$'),
  CONSTRAINT cpp_target_rev_positive CHECK (target_revision >= 1),
  CONSTRAINT cpp_derived_rule CHECK (
    (derived_from IS NULL) = (derivation_rule IS NULL)),
  CONSTRAINT cpp_derived_is_earlier CHECK (
    derived_from IS NULL OR derived_from::integer < patch_seq)
);

CREATE INDEX cpp_by_target_idx
  ON change_plan_patch (tenant_id, target_service_id, target_id);
```

**`before_value` / `after_value` 是 `jsonb NOT NULL`，但值是**小标量**（字符串、数字、布尔或短数组），不是整份内容。对白长文本不允许出现在这里——那会让 `change_management` 成为第二个剧本库。

约束层面能做什么？只能靠写入前的长度检查：

```sql
ALTER TABLE change_plan_patch ADD CONSTRAINT cpp_value_size
  CHECK (octet_length(before_value::text) <= 4096
     AND octet_length(after_value::text) <= 4096);
```

4 KB 足够容纳任何结构性字段（`/camera/angle`、`/duration/target_seconds`、`/beats/2/action` 的短描述），远不足以装一整段对白。

**`derived_from` 表达"派生改动"**：用户只说"缩 4 秒"，但 `end_pause` 必须跟着变。这条派生关系必须可解释（UI 要显示"这条不是你要求的，是推导出来的"），也必须可追溯到触发它的那条补丁。

### 2.3 `clarification_request`

```sql
CREATE TABLE clarification_request (
  tenant_id    text NOT NULL,
  plan_id      text NOT NULL,
  request_id   text NOT NULL,
  kind         text NOT NULL,          -- MISSING_EPISODE_CONTEXT | DIALOGUE_EXCEEDS_DURATION
                                       -- | AMBIGUOUS_SHOT | UNSUPPORTED_INSTRUCTION
                                       -- | MISSING_ASSET | RIGHTS_BLOCKED
  question     text NOT NULL,
  options      jsonb NOT NULL,          -- [{option_id, label, consequence, cost_delta_micro}]
  selected_option_id text,
  answered_by  jsonb,
  answered_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (tenant_id, request_id),
  CONSTRAINT cr_kind_closed CHECK (kind IN (
    'MISSING_EPISODE_CONTEXT','DIALOGUE_EXCEEDS_DURATION','AMBIGUOUS_SHOT',
    'UNSUPPORTED_INSTRUCTION','MISSING_ASSET','RIGHTS_BLOCKED')),
  CONSTRAINT cr_options_nonempty CHECK (jsonb_array_length(options) >= 2),
  CONSTRAINT cr_answered_consistent CHECK (
    (selected_option_id IS NULL) = (answered_at IS NULL))
);

CREATE INDEX cr_open_idx ON clarification_request (tenant_id, plan_id)
  WHERE answered_at IS NULL;
```

**`options` 的 `minItems: 2` 是 DB 约束**：一个只有一种选项的"澄清"是命令，不是问题。若某个情形真的只有一个合理选项，那就不该问——直接做，并在 `derived_adjustments` 里说明。

**`cost_delta_micro` 可以是正、负或 NULL**（"这个选项的成本取决于你选哪个" 时用 NULL，并要求展开子选项）。

### 2.4 `change_saga` 与 `change_saga_step`

```sql
CREATE TABLE change_saga (
  tenant_id      text NOT NULL,
  saga_id        text NOT NULL,
  plan_id        text NOT NULL,
  state          text NOT NULL,        -- RUNNING | SUCCEEDED | FAILED | ABANDONED
  current_step   integer NOT NULL,
  started_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz,
  failure_code   text,
  failure_detail jsonb,

  PRIMARY KEY (tenant_id, saga_id),
  CONSTRAINT cs_state_closed CHECK (state IN
    ('RUNNING','SUCCEEDED','FAILED','ABANDONED')),
  CONSTRAINT cs_finished_has_ts CHECK ((state = 'RUNNING') = (finished_at IS NULL))
);

-- 一个 plan 只能有一个生效 Saga（重试复用同一个）
CREATE UNIQUE INDEX cs_plan_active_uniq
  ON change_saga (tenant_id, plan_id) WHERE state = 'RUNNING';

CREATE TABLE change_saga_step (
  tenant_id    text NOT NULL,
  saga_id      text NOT NULL,
  step_seq     integer NOT NULL,
  step_name    text NOT NULL,
  target_service_id text,
  command_id   text,
  state        text NOT NULL,          -- PENDING | RUNNING | DONE | FAILED | SKIPPED
  request_hash text,
  response     jsonb,
  error_code   text,
  started_at   timestamptz,
  finished_at  timestamptz,

  PRIMARY KEY (tenant_id, saga_id, step_seq),
  CONSTRAINT css_state_closed CHECK (state IN
    ('PENDING','RUNNING','DONE','FAILED','SKIPPED'))
);
CREATE INDEX css_by_plan_step_idx ON change_saga_step (tenant_id, saga_id, step_seq);
```

`change_saga_step` 是**审计记录**，不是调度状态。它记"哪一步在什么时候用哪个 command_id 调谁、返回了什么"，用来在 E13 类崩溃后回答"到底做到哪了"。

**Saga 状态不是 S21 的 Run 状态**。两者词汇表不同、生命周期不同、权威不同。S07 的 Saga 是一个同步推进的短流程（秒级到分钟级），S21 的 Run 是长任务（小时级）。

### 2.5 `impact_summary` —— 展示用摘要

```sql
CREATE TABLE impact_summary (
  tenant_id    text NOT NULL,
  plan_id      text NOT NULL,
  analysis_id  text NOT NULL,
  totals       jsonb NOT NULL,       -- {regenerate:42, recheck:8, reuse:120}
  by_episode   jsonb NOT NULL,       -- [{episode_id, regenerate, recheck, reuse, delivered}]
  by_stage     jsonb NOT NULL,       -- [{stage, count, cost_upper_micro}]
  cost_breakdown jsonb NOT NULL,     -- [{kind, node_count, cost_upper_micro}]
  warnings     jsonb NOT NULL,       -- 已交付集、跨集、记忆迁移等
  lineage_watermark bigint NOT NULL,

  PRIMARY KEY (tenant_id, plan_id)
);
```

`impact_summary` 是**派生数据**，可以从 `change_plan` + S09 的 `impact_analysis` 重建。放表里只是为了 UI 读得快，不是权威。

### 2.6 基础设施表

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
  aggregate_id       text NOT NULL,       -- plan_id
  aggregate_sequence bigint NOT NULL,
  event_type         text NOT NULL,
  partition_key      text NOT NULL,       -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  PRIMARY KEY (tenant_id, event_id)
);
CREATE INDEX outbox_pending_idx ON outbox (published_at) WHERE published_at IS NULL;

CREATE TABLE inbox (
  consumer    text NOT NULL,   -- s07.policy | s07.manifest | s07.impact
  event_id    text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

### 2.7 不建的表

| 不建 | 为什么 |
|---|---|
| `accepted_pointer` / `accepted_state` | S08 是唯一写者。S07 建这张表就是第二个权威 |
| `node_epoch` / `manifest` 镜像 | 同上；需要的都从 S08 读 |
| `prepared_content`（新 revision 的正文） | 内容在各服务；S07 只有 `prepared_revision_refs` |
| `closure` / 依赖边 | S09 |
| `route_decision` / `quote` | S25 |
| `budget_reservation` | S23 |

---

## 3. 状态机

### 3.1 `change_plan` 状态机

```
 POST /change-plans
        │
        ▼
    DRAFT ──► PLANNING ──┬──► PLANNED ──apply──► APPLYING ──┬──► APPLIED
                         │                                   ├──► FAILED
                         │                                   └──► (补偿) FAILED
                         ├──► CLARIFICATION_REQUIRED ──(答复)──► PLANNING
                         ├──► POLICY_DENIED
                         ├──► INFEASIBLE
                         └──► FAILED
   任意非终态 ──(expires_at 到)──► EXPIRED
   PLANNED ──(同 base+同意图的新 plan 生成)──► SUPERSEDED
```

| 从 | 到 | 触发 | 副作用 |
|---|---|---|---|
| — | `DRAFT` | 收到 `POST /change-plans` | 落行，便于回答"正在算什么" |
| `DRAFT` | `PLANNING` | 解析完成，开始并发准备内容与影响分析 | 无 |
| `PLANNING` | `PLANNED` | 全部前置成功（补丁、影响、政策、成本） | 发 `change.plan.planned`（内部） |
| `PLANNING` | `CLARIFICATION_REQUIRED` | 解析或可行性需要用户选择 | 建 `clarification_request` |
| `PLANNING` | `POLICY_DENIED` | S02 `evaluate_change_policy` 返回 `deny` | `blocking_issues` 非空 |
| `PLANNING` | `INFEASIBLE` | S25/S06 明确不可行且无降解选项 | 同上 |
| `PLANNING` | `FAILED` | 依赖不可达、超时、内部错误 | 可重试（同 command_id 重放） |
| `PLANNED` | `APPLYING` | `:apply` 通过三道闸 | 建 `change_saga` |
| `APPLYING` | `APPLIED` | S08 `:commit` 返回 200 | 发 `change.plan.applied` |
| `APPLYING` | `FAILED` | 任一步失败且不可补偿 | 记 `failure_code` |
| `PLANNED` | `EXPIRED` | `expires_at` 到 | 无 |
| `PLANNED` | `SUPERSEDED` | 同 base、同集、同意图的新 plan 生成 | 无 |

**`POLICY_DENIED` 与 `INFEASIBLE` 分开**：前者是"不允许"（换个人/改策略可能行），后者是"做不到"（改指令才可能行）。UI 给的下一步完全不同。

**`CLARIFICATION_REQUIRED` 是唯一能从中间回到 `PLANNING` 的状态**：答复后重新解析。答复本身不是新命令——它带 `command_id`，走同一套幂等。

**没有 `CANCELED`**：用户想放弃就让它过期，或者不管它。取消一个计划不产生任何副作用（没有东西被创建），所以不需要状态。

### 3.2 `change_saga` 状态机

```
RUNNING ──► SUCCEEDED
   │
   ├──► FAILED（某步终态失败，且补偿已完成）
   └──► ABANDONED（外部条件消失：plan 过期、fence 变了、被 supersede）
```

`ABANDONED` 与 `FAILED` 的区别：前者没出错（是"不该继续"），后者出错了。

### 3.3 `clarification_request` 状态机

```
OPEN ──► ANSWERED（selected_option_id 非空）
  └──► ABANDONED（plan 过期或被 supersede）
```

**没有"超时自动选默认"**。澄清超时 = plan 过期 = 用户没决定。默认选项会在无人注意时改变剧本——这正是 E04 要防的。

---

## 4. 关键流程

### 4.1 "第3场第2镜改仰拍、缩4秒" 的完整链路（E03 主路径）

**步骤 0：解析**

输入：`instruction` + `context`（含 `series_id`, `branch_id`, `episode_id`, `manifest_ref`）。

解析的产物 `parsed_intent`：

```jsonc
{
  "targets": [
    {"kind":"SHOT","scene_no":3,"shot_no_in_scene":2,
     "resolved_shot_id":"shot_3_2","resolution":"PROJECT_LOCAL_NUMBERING"}
  ],
  "changes": [
    {"field":"/camera/angle","op":"SET","value":"low","source_text":"仰拍"},
    {"field":"/duration/target_seconds","op":"SET","value":4,"source_text":"缩4秒"}
  ],
  "ambiguities": [],
  "unparsed_fragments": []
}
```

**解析必须区分"能唯一解析"与"不能"**：

| 条件 | resolution | 后续 |
|---|---|---|
| `context.episode_id` 确定，编号在集内唯一 | `PROJECT_LOCAL_NUMBERING` | 继续 |
| `context.episode_id` 缺失，但 series 内只有一个集有"第3场第2镜" | `SERIES_UNIQUE_MATCH` | 继续，但记 warning |
| `episode_id` 缺失且多集都有 | — | `CLARIFICATION_REQUIRED(MISSING_EPISODE_CONTEXT)` |
| `episode_id` 有，但"第3场第2镜"在集内指代多个（分镜未定稿） | — | `CLARIFICATION_REQUIRED(AMBIGUOUS_SHOT)` |
| 字段无法映射到 S06 DSL | — | `CLARIFICATION_REQUIRED(UNSUPPORTED_INSTRUCTION)` |

**"仰拍" → `/camera/angle: low`** 是枚举映射，见 `contracts/domain-v1/shot-spec.v1.schema.json` 的 `camera.angle` enum（`eye_level|low|high|overhead|dutch`）。映射表是 S06 的 DSL 词汇，不是 S07 的私货——**S07 必须从 S06 的 schema 读取**，不能硬编码（否则 S06 加一个 `worm_eye` 就要改 S07）。

**"缩4秒" → `/duration/target_seconds: 4`** 是绝对值不是相对值。若用户说"缩 2 秒"，S07 需要读当前值做算术——这需要读 S06 的 shot revision，允许（只读）。

**步骤 1：并发做三件事**

```
A. S06 POST /storyboards/{id}/revisions   （准备新 DSL，未接受）
B. S09 POST /impact-queries               （影响闭包）
C. S02 POST /policy-decisions:evaluate    （kind=change_application）
```

**为什么 A 与 B 并发**：S06 准备新 revision 需要时间（它可能调 S19 做创意推理），S09 算闭包也需要时间。串行会白白等两段。

**但 A 的需求来自解析，B 的种子也来自解析**——两者互相不依赖，可并发。B 的种子是"哪些字段变了"，这在解析完成后就知道了，不需要等 S06 的新 revision（S06 的 revision 号只是 `prepared_revision_refs` 的一部分）。

**A 的产物是"未接受的新 revision"**：S06 写入一个 revision，`state=prepared`，不被任何 manifest 引用。若 apply 失败，它就是 unreferenced，保留期后 GC（S08 §4.1 步 4 已说明）。

**步骤 2：S06 返回后的派生改动**

S06 返回新 DSL（revision 8）。此时 S07 才发现需要派生：

```
/duration/target_seconds: 6 → 4
  → /end_pause: 0.6 → 0.2  （规则 END_PAUSE_FROM_DURATION，S06 的 timing_policy）
  → /beats[1]/duration: 2.0 → 1.6 （若 beat 时长是相对分配）
```

这些派生是 **S06 算的，S07 只是记录**。`derived_from` 指向触发它的 `patch_seq`，`derivation_rule` 是 S06 返回的规则名。**S07 不自己算派生**——否则它会成为第二个 DSL 语义权威。

S07 的角色是：把 S06 的派生结果**放进 `derived_adjustments` 与 `change_plan_patch`**，让用户在应用前看见"你只说了缩时长，但结尾停顿会跟着变"。

**步骤 3：对白可行性与澄清（E04）**

S06 返回 `prepared` 但**标记了 `dialogue_min_duration_ms: 5200`**（或 S37 在 recheck 时给出）。S07 判定：

```
若 target_seconds * 1000 < dialogue_min_duration_ms:
    → CLARIFICATION_REQUIRED(DIALOGUE_EXCEEDS_DURATION)
    options:
      1. "延到 5.2 秒"            cost_delta: +180000
      2. "保留 4 秒并重写对白"     cost_delta: +620000, requires: S04 参与
      3. "取消这次修改"           cost_delta: 0
```

**选项 2 的 `requires` 字段很重要**：它告诉用户"这个选项会牵动剧本"。UI 应明确显示"需要改台词"，因为 docs/06 说 unattended 也不允许随意删台词——用户必须知道自己选了什么。

**选项里绝不含"自动删掉超出的部分"**。即使技术上可以做（截断 TTS），也不做：那会改变剧情内容，需要剧本层的决定。

**步骤 4：组装 ChangePlan**

```jsonc
{
  "schema_version": "2.0",
  "plan_id": "change_01H...",
  "plan_hash": "…",
  "context": { /* execution-context 的关键子集 */ },
  "base_manifest_ref": {"id":"manifest_ep3","revision":12,"content_hash":"…"},
  "instruction": "把第3场第2镜改成仰拍，时长缩到4秒",
  "prepared_revision_refs": [
    {"service_id":"S06","id":"shot_3_2","revision":8,"content_hash":"…"}
  ],
  "patches": [
    {"target_ref":{"service_id":"S06","id":"shot_3_2","revision":7,"content_hash":"…"},
     "path":"/camera/angle","before":"eye_level","after":"low"},
    {"target_ref":{"service_id":"S06","id":"shot_3_2","revision":7,"content_hash":"…"},
     "path":"/duration/target_seconds","before":6,"after":4}
  ],
  "derived_adjustments": [
    "结尾停顿由0.6秒缩到0.2秒（规则 END_PAUSE_FROM_DURATION）；固定对白不改"
  ],
  "impact": { /* S09 的 result，逐字 */ },
  "memory_migration_required": false,
  "required_policy_decisions": ["node_change_allowed"],
  "blocking_issues": [],
  "estimated_cost_upper_micro": 900000,
  "currency": "USD",
  "expires_at": "2026-11-01T10:11:00Z"
}
```

**`patches[].target_ref.revision` 是修改前的 revision（7）**，不是准备后的（8）。理由：补丁描述的是"从什么改成什么"；准备后的 revision 在 `prepared_revision_refs` 里。

**步骤 5：`apply` 的三道闸 + Saga**

```
POST /change-plans/{plan_id}:apply
body: {expected_manifest_revision: 12, plan_hash: "…", command_id}

1. 幂等（command_id + request_hash）
2. 读 change_plan（必须 state=PLANNED）
3. 闸 1：plan_hash 匹配
4. 闸 2：expected_manifest_revision == plan.base_revision
5. 闸 3：now() < expires_at
6. 读 S08 manifest 头：current_revision 与 fence
   - current_revision ≠ 12 → 409 MANIFEST_REVISION_CONFLICT
   - fence ≠ plan.execution_fence → 409 STALE_EXECUTION_FENCE
7. 重新 evaluate policy（kind=change_application，同 plan 的 policy_version）
   - deny → 409 POLICY_DENIED_NOW（并置 plan 为 POLICY_DENIED）
   - abstain → 409 POLICY_ABSTAIN（可重试）
   - require_human → 建 ReviewTask（S40），Saga 停在等审批
8. 建 change_saga（RUNNING）+ step 表
9. 顺序推进（§4.2）
10. S08 :commit 成功 → plan 置 APPLIED，发 change.plan.applied
```

**步骤 7 的"重新 evaluate"不能省**：docs/04 与 AGENTS.md 都要求"把审核已通过当成放行依据"是禁止行为。从 `PLANNED` 到 `APPLYING` 之间可能过了几分钟，期间策略可能被改、资产权利可能撤销、kill switch 可能被打开。**审核一次不够，派单前必须复查**。

**步骤 6 的 fence 校验**同样不能省：计划生成时的 fence 是 2，apply 时如果变成 3，说明这个 branch 发生过取消/故障切换，旧的准备产物已经 VOIDED，计划作废。

### 4.2 Saga 的七步

| 步 | 动作 | 服务 | 幂等键 | 失败处理 |
|---|---|---|---|---|
| 1 | 复查 manifest 与 fence（读） | S08 | — | 409，Saga `ABANDONED` |
| 2 | 复查 policy（写? 不，是 query） | S02 | `command_id` | `deny` → `FAILED`；`abstain` → 重试 |
| 3 | 确认准备产物仍有效（读 S06 revision 8） | S06 | — | 已 GC → `FAILED(PREPARED_REVISION_GONE)` |
| 4 | 校验补丁可应用到 revision 7（纯计算） | 本地 | — | 不匹配 → `FAILED(PATCH_PRECONDITION_FAILED)` |
| 5 | 准备其余内容所有者的新 revision（若影响含 S31/S28 等） | 各自 | `command_id` 派生 | 保留 unreferenced，重试 |
| 6 | **S08 `:commit`** | S08 | `command_id` | 见 §4.3 |
| 7 | 发 `change.plan.applied`（经 outbox） | 本地 | `aggregate_sequence` | relay 补发 |

**步骤 5 的"准备其余内容"是什么意思？** 一次改镜可能只改 S06，也可能需要 S31 重配音（如果对白变了）。S07 不预先知道全部——它靠 S09 的 `regenerate_node_ids` 反查每个节点属于哪个服务，然后调对应的"准备"接口。

但这里有个边界：**S07 不负责"生成"任何内容**。步骤 5 是"准备"（`prepare`），产物是未接受的 revision。实际的生成（付费、走 S26）在 `commit` 之后由 S21 调度——因为生成需要当前 manifest 的 epoch，而 epoch 在 commit 之后才确定。

**这正是为什么链路是"S07 → S06 prepare → S09 → S02 → S08 commit → S21 execute"而不是"S07 直接生成"**：先让内容可用（便宜的、确定性部分），再提交（建立新的 epoch），最后生成（昂贵的、依赖 epoch 的部分）。

### 4.3 S08 `:commit` 的失败分支（E13）

| 崩溃点 | S07 看到 | 系统状态 | 恢复 |
|---|---|---|---|
| commit 请求未到达 | 超时/连接失败 | 无变化 | 用同 `command_id` 重试 |
| commit 执行中崩溃 | 超时 | S08 事务回滚，无记录 | 重试 |
| commit 成功、响应丢失 | 超时 | **manifest 已前进** | 重试 → S08 返回 `replayed:true` |
| commit 返回 409 `REVISION_CONFLICT` | 明确失败 | 无变化 | plan 置 `FAILED`，需重新生成 |
| commit 返回 409 `STALE_EXECUTION_FENCE` | 明确失败 | 期间被 fence | plan 置 `FAILED`，`ABANDONED` |
| commit 成功但 S07 进程崩溃（未写 Saga 步 6 DONE） | — | manifest 已前进，Saga 步 6 是 RUNNING | **恢复时先查 S08**：`GET /manifests/{id}?revision=13` 看是否有 `change_plan_ref` 指向本 plan |

**最后一行是 E13 的核心**：S07 重启后不知道 commit 成没成。**它不能假设失败重试**（可能重复提交），**也不能假设成功**（可能没提交）。它只能**去 S08 查**：

```
GET /manifests/{manifest_id}/revisions?limit=5
→ 找 payload.change_plan_ref.id == 本 plan_id 的 revision
→ 找到 → 步 6 已成功，继续步 7
→ 未找到 → 步 6 未成功，用同 command_id 重试
```

这要求 S08 的 `manifest_revision.payload` 里有 `change_plan_ref`。S08 方案 §2.2 的 `:commit` body 已有 `change_plan_ref` 字段，它在 commit 时被写进 revision 的溯源信息。**这是 S07 与 S08 之间一条必须存在的约定**（§9 C07-4）。

**不允许的做法**：S07 自己在本地记"我发过 commit 了"。那条记录在崩溃时可能丢，也可能写了但 commit 没执行——与 S08 的事实无关。

### 4.4 `apply` 之后：S07 与 epoch 的关系

`apply` 成功后，S08 返回：

```jsonc
{
  "manifest": { /* 新 manifest revision 13 */ },
  "previous_revision": 12,
  "rebased": false,
  "changed_node_epochs": [
    {"node_id":"shot_3_2.video","from":4,"to":5},
    {"node_id":"shot_3_2.keyframe","from":2,"to":3},
    {"node_id":"ep3.edl","from":1,"to":2},
    {"node_id":"ep3.subtitle_timing","from":1,"to":2},
    {"node_id":"ep3.mix","from":1,"to":2},
    {"node_id":"ep3.master","from":1,"to":2}
  ],
  "outbox_event_id": "evt_..."
}
```

**S07 拿这个响应做什么**：

1. 写进 `change_saga_step[6].response`（审计）
2. 发 `change.plan.applied` 事件，`data` 里带 `changed_node_epochs`
3. **不把它当成自己的状态**——它只是转述 S08 的结论

**S07 拿这个响应不做什么**：

- 不缓存 epoch（下次要读就再读 S08）
- 不把 epoch 写进任何 S07 的表
- 不用它判断"哪些节点真的要重做"（那是 S21 消费事件后的事；S08 的 epoch 变不等于"一定会重跑"——可能是被复用了）

**`impact.regenerate_node_ids` 与 `changed_node_epochs` 的差集是正常的**：

| 情况 | 表现 |
|---|---|
| S09 说重做 `shot_3_2.video`，S08 推进了它的 epoch | 一致 |
| S09 说重做 `shot_3_2.lipsync`，但 S08 发现它的 `input_fingerprint` 没变（对白没变） | **S08 不推进 epoch**，差集出现 |
| S09 保守模式下多算了 `shot_3_4.*`，S08 判定无关 | 差集出现，S21 不会跑它们 |

**这个差集不是 bug，是设计**。S09 给的是"安全上界"（宁多勿少），S08 给的是"事实"（只推真正变了的）。S21 按 S08 的事实调度，所以不会做无谓的生成。**如果 S07 把 S09 的清单当成 epoch 清单传给 S08，S08 会拒绝（epoch 不能由客户端指定）**——这正是 AGENTS.md 禁止的"自己存一份 accepted pointer"的变体。

### 4.5 跨集改稿（E08）

docs/07 §3："修改已接受的前集剧情需同时创建 memory impact plan；S16 的新事实checkpoint与旧manifest保留映射，S09 定位后续集所依赖的claim。S07 先展示跨集影响，不用修改一个字段导致全剧悄悄重做。"

**流程与局部改镜的区别**：多了一个前置的"记忆影响"阶段。

```
1. 解析：识别出"这改动会改变剧情事实"（不是纯镜头参数）
   → 判据：补丁目标是否在 S04 剧本层，或补丁涉及角色状态/道具归属/知情范围
2. 若涉及：先调 S16 POST /series/{series_id}/memory/commits（preview 模式）
   → 得到"如果这样改，记忆会变成什么"
   → **不实际提交**（S16 的 preview 返回 diff，不写）
3. 用 S16 的候选改成 seed，调 S09 求跨集闭包
   → affected_episode_ids（可能多集）
4. 组装 ChangePlan，memory_migration_required = true
   → impact_summary 里列出：
     - 哪些集未交付 → 会重做
     - 哪些集已交付 → 只标 memory_stale，不重做
     - 新旧 checkpoint 的映射关系
5. 用户看到影响后确认 → apply
6. apply 的 Saga 多一步：S16 正式提交新的 memory commit
   → 得到新 checkpoint
   → 再 S08 commit（新 manifest 引用新 checkpoint）
```

**步骤 2 的 preview 是必需的，也是 S07 最容易做错的地方**：如果直接提交 S16，然后再算影响，那么"用户还没看到影响，事实就已经变了"——这违反 docs/05 §5"未明确迁移前，已交付集保留原checkpoint和历史证据，不悄悄重写"。

**步骤 4 的 `impact_summary` 是"展示"的落地**。它必须答三个问题：

| 问题 | 字段 |
|---|---|
| 有多少集要重做，各花多少？ | `by_episode[]` + `cost_breakdown[]` |
| 哪些已交付的集受影响？ | `by_episode[].delivered=true` + `warnings[]` |
| 记忆 checkpoint 怎么迁移？ | `memory_migration_required` + S16 的 diff 引用 |

**已交付集的处置**：S07 提供三个显式选项（不是自动决定）：

```
1. "只标记，不迁移"（默认推荐）
   → 已交付集保持原 checkpoint，标记 memory_stale
   → 不重做、不重渲染
2. "迁移并重做"
   → 已交付集进入 regenerate 集合，成本最高
   → 需要额外权限（policy 的 budget_increase gate）
3. "取消这次修改"
```

**用户不选就不动**。docs/05 §5 的"不悄悄重写"就落在这里。

### 4.6 `CLARIFICATION_REQUIRED` 的答复流程

```
POST /change-plans/{plan_id}/clarifications/{request_id}:answer
body: {selected_option_id, command_id}
1. 幂等
2. 校验 request 未答复、plan 仍是 CLARIFICATION_REQUIRED
3. 记录答复
4. plan 回到 PLANNING，重新解析（带着答复）
5. 重新走 §4.1 的步骤 1–4
```

**答复不创建新 plan**：同一个 `plan_id` 继续。理由：用户确认的是"同一个修改意图"，答案只是补全缺失信息。若每次都新 plan，`plan_hash` 会不断变化，UI 的"这是你刚才看的那份"就失效了。

**答复可以带 `command_id` 多次重放**，返回首次结果。

### 4.7 过期与 supersede

**过期**：`expires_at` 默认 30 分钟（可配 5–120）。到点后：

```
定时任务（每分钟）扫描 state=PLANNED AND expires_at < now()
→ 置 EXPIRED，无副作用
→ 若有人 apply → 409 PLAN_EXPIRED
```

**为什么需要过期**：`estimated_cost_upper_micro` 是估价上界，随供应商价格、汇率、政策版本变化。一份 3 小时前的"上界 90 万"可能现在要 150 万。用户确认的是那份上界，过期后必须重新确认。

**supersede**：同一 `(branch, episode, base_revision)` 上生成新 plan 时，旧 plan 置 `SUPERSEDED`。这不阻止旧 plan 被 apply（三道闸会拦），只是让列表干净。

---

## 5. 对外契约

### 5.1 `POST /change-plans`

```jsonc
{
  "context": { /* execution-context.v2 */ },
  "instruction": "把第3场第2镜改成仰拍，时长缩到4秒",
  "planning_budget_scope_id": "budget_planning_proj",
  "command_id": "cmd_01H..."
}
```

响应 `200`（同步）或 `202`（超预算转异步）：

```jsonc
{
  "plan_id": "change_01H...",
  "plan_hash": "…",
  "state": "PLANNED",
  "replayed": false,
  // 以下为 enterprise-change-plan.v2 的完整内容
  "schema_version": "2.0",
  "context": {…},
  "base_manifest_ref": {…},
  "instruction": "…",
  "prepared_revision_refs": [...],
  "patches": [...],
  "derived_adjustments": [...],
  "impact": {...},
  "memory_migration_required": false,
  "required_policy_decisions": [...],
  "blocking_issues": [],
  "estimated_cost_upper_micro": 900000,
  "currency": "USD",
  "expires_at": "2026-11-01T10:11:00Z"
}
```

**响应体就是 `enterprise-change-plan.v2.schema.json` 的实例**，没有额外包装。`state` 与 `replayed` 不在 schema 里（`additionalProperties:false`），所以：

- 契约层面响应 = 纯 plan
- 状态与重放信息放在 **header**（`X-Plan-State`、`X-Idempotent-Replay: true`）

这是 §9 C07-5 登记的冲突：schema 不允许包装，而实现需要状态。**建议**：不改 schema，用 header。

**`CLARIFICATION_REQUIRED` 时**仍返回 `200` + 一个 plan（`patches` 为空？不行——`patches` 的 `minItems: 1`）。所以这种情形**必须返回非 2xx**：

```jsonc
HTTP 409
{
  "code": "CLARIFICATION_REQUIRED",
  "message": "需要确认后才能生成计划",
  "retryable": false,
  "correlation_id": "…",
  "details": {
    "plan_id": "change_...",
    "clarification": {
      "request_id": "clar_...",
      "kind": "DIALOGUE_EXCEEDS_DURATION",
      "question": "4秒容不下这段对白（最短需5.2秒），请选择：",
      "options": [...]
    }
  }
}
```

这是 §9 C07-6 的另一个冲突：**"澄清"既是一个成功的业务结果，又不符合 plan schema 的成功形态**。本文选择 409 + details，因为 409 的语义（当前状态不允许完成）最接近。

### 5.2 `POST /change-plans/{plan_id}:apply`

```jsonc
{
  "expected_manifest_revision": 12,
  "plan_hash": "…",
  "command_id": "cmd_01H..."
}
```

响应 `202`：

```jsonc
{ "operation_id": "op_...", "state": "accepted", "plan_id": "change_..." }
```

**`202` 而不是 `200`**：apply 要推进 Saga（可能有 5–6 次跨服务调用），不能同步等。客户端轮询 `GET /change-plans/{id}` 或订阅 `change.plan.applied`。

**`Operation.state` 的枚举不含 `applied`**（§9 C21-3 同款问题）。apply 后应查 `GET /change-plans/{id}` 的 `state`，不看 Operation。

### 5.3 `GET /change-plans/{plan_id}`

**openapi 里没有这条**（catalog 声明有）。按契约应返回：

```jsonc
{
  // enterprise-change-plan.v2 的完整实例（同 POST 响应）
  "plan_id": "...", "plan_hash": "...", ...
}
```

外加通过 header 传 `X-Plan-State: APPLIED`。

**问题**：`GET` 需要回答的问题（"到底应用了没"）不在 schema 里。这是 §9 C07-1 的冲突。

**建议补充查询**（不在 openapi，本文描述）：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/change-plans/{plan_id}/saga` | Saga 的逐步状态（E13 审计） |
| `GET` | `/change-plans/{plan_id}/impact-summary` | 展示用摘要（§2.5） |
| `GET` | `/change-plans/{plan_id}/clarifications` | 未答复的澄清 |
| `GET` | `/change-plans?episode_id=&state=&after=` | 列表 |

### 5.4 `POST /change-plans/{plan_id}/clarifications/{request_id}:answer`

```jsonc
{ "selected_option_id": "opt_extend_to_5_2", "command_id": "cmd_..." }
```

响应 `200`：`{plan_id, state:"PLANNING", replayed:false}`。

### 5.5 发布事件 `change.plan.applied`

Topic `drama.change-management.v2`，`aggregate_id = plan_id`，`aggregate_sequence = 1`（每 plan 只发一次）。

```jsonc
{
  "specversion":"1.0","schema_version":"2.0","id":"evt_...","source":"S07",
  "type":"change.plan.applied","subject":"change-plan/change_01H...",
  "time":"2026-11-01T09:41:00Z",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"change_01H...","aggregate_sequence":1,
  "correlation_id":"cmd_...","causation_id":"evt_manifest_committed",
  "traceparent":"00-...-...-01",
  "data": {
    "plan_id":"change_01H...",
    "plan_hash":"…",
    "base_manifest_ref":{"id":"manifest_ep3","revision":12,"content_hash":"…"},
    "new_manifest_ref":{"id":"manifest_ep3","revision":13,"content_hash":"…"},
    "execution_fence":2,
    "changed_node_epochs":[
      {"node_id":"shot_3_2.video","from":4,"to":5}
    ],
    "regenerate_node_ids":[…],
    "recheck_node_ids":[…],
    "reuse_artifact_refs":[…],
    "affected_episode_ids":["ep3"],
    "memory_checkpoint_before":{"commit_sequence":27,"content_hash":"…"},
    "memory_checkpoint_after":{"commit_sequence":29,"content_hash":"…"},
    "estimated_cost_upper_micro":900000,
    "currency":"USD",
    "saga_id":"saga_...",
    "applied_at":"2026-11-01T09:41:00Z"
  },
  "data_ref": null
}
```

**`changed_node_epochs` 来自 S08 的 `:commit` 响应，不是 S09 的清单**（§4.4）。

**`S21 消费这个事件吗？** 不。S21 消费 S08 的 `snapshot.manifest.committed`。`change.plan.applied` 给：

| 消费者 | 用途 |
|---|---|
| S47 | 修改历史面板 |
| S44 | 修改成本与效果分析 |
| S45 | 审计 |
| S07 自己（其他实例） | 若同集的另一个 plan 正在生成，把它 supersede |

**S21 从 `snapshot.manifest.committed` 拿 `changed_node_epochs` 就够了**。S07 的事件是补充（带 change plan 的语义），不是调度依据。

### 5.6 消费的事件

| 事件 | 源 | 处理 | 幂等 |
|---|---|---|---|
| `snapshot.manifest.committed` | S08 | 若某 `PLANNED` plan 的 `base_revision` 落后 → 置 `SUPERSEDED`（不主动改，只是标记） | `s07.manifest` |
| `policy.revision.published` | S02 | 失效本地 policy 缓存；不主动重建任何 plan | `s07.policy` |
| `memory.canon.committed` | S16 | 若与某 `PLANNED` plan 的 `memory_checkpoint` 相关 → 标 `stale_memory`，apply 时会被拦 | `s07.impact` |

**S07 不消费 `lineage.impact.computed`**。影响分析是 S07 自己同步调用的（`POST /impact-queries`），响应直接进 plan。消费事件会导致"用别人算的闭包做自己的计划"，且顺序无法保证。

### 5.7 错误码

| HTTP | code | retryable | 说明 |
|---|---|---|---|
| 400 | `INVALID_CONTEXT` | false | execution-context 缺字段 |
| 403 | `TENANT_MISMATCH` | false | header 与认证不一致 |
| 404 | `PLAN_NOT_FOUND` | false | 跨租户同码 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同键不同 body |
| 409 | `CLARIFICATION_REQUIRED` | false | 带 `details.clarification` |
| 409 | `PLAN_NOT_PLANNED` | false | 状态不对 |
| 409 | `PLAN_EXPIRED` | true | 重新生成 |
| 409 | `PLAN_HASH_MISMATCH` | false | 用户确认的不是这份 |
| 409 | `MANIFEST_REVISION_CONFLICT` | false | 基础已动 |
| 409 | `STALE_EXECUTION_FENCE` | false | 期间被 fence |
| 409 | `CONCURRENT_CHANGE` | false | 同集另一个 apply 在进行 |
| 422 | `INSTRUCTION_UNPARSEABLE` | false | 无法解析（`UNSUPPORTED_INSTRUCTION` 的另一形态） |
| 422 | `CHANGE_INFEASIBLE` | false | 明确不可行且无降解 |
| 422 | `POLICY_DENIED` | false | 政策拒绝 |
| 500 | `PATCH_PRECONDITION_FAILED` | false | 补丁与基础 revision 不匹配 |
| 503 | `DEPENDENCY_UNAVAILABLE` | true | S06/S09/S02/S08 不可达 |

---

## 6. 幂等与并发

### 6.1 命令幂等

```
request_hash = sha256( JCS( body 去掉 command_id 与 request_hash ) )
```

| 已有 | 本次 | 结果 |
|---|---|---|
| 无 | — | 执行 |
| `IN_PROGRESS` | 同 hash | 409 `COMMAND_IN_PROGRESS`，`retryable:true` |
| `DONE` | 同 hash | 原样返回首次响应 |
| 任意 | 不同 hash | 409 `IDEMPOTENCY_KEY_REUSED` |

### 6.2 计划的生成幂等

**同一个 `POST /change-plans` 重试**不能生成两个 plan。命令幂等已经保证（同 command_id）。

**不同 command_id、同 instruction、同 base** 会生成两个 plan——这是允许的（用户可能想比较两个方案）。两个 plan 都 `PLANNED`，但只有一个能被 apply（§6.3 的串行约束）。第一个 apply 后，第二个的 `expected_manifest_revision` 就对不上了。

**`plan_hash` 必须确定性**：同样的输入（instruction + base + 影响 + 成本）必须得到同样的 hash。这意味着：

- 不把 `created_at` 纳入 hash（时间戳必然不同）
- `plan_id` 是随机的，但不在 hash 输入里
- `impact_analysis_id` 也不在（它是随机 id，且同一语义的查询应得到同一 analysis——S09 §6.2 保证）

hash 输入 = `{schema_version, context(稳定子集), base_manifest_ref, instruction, prepared_revision_refs, patches, derived_adjustments, impact, memory_migration_required, required_policy_decisions, blocking_issues, estimated_cost_upper_micro, currency}`。

### 6.3 同一集上的并发修改

两个 apply 同时针对 `manifest_ep3` revision 12：

| 时序 | 结果 |
|---|---|
| 两个都读 revision 12 | 都通过闸 2 |
| 两个都调 S08 `:commit`（expected_revision=12） | 一个成功（返回 13），一个 409 `REVISION_CONFLICT` |
| 后者 | plan 置 `FAILED`，用户需重新生成 |

**S07 不需要自己的分布式锁**：S08 的 CAS 就是锁。S07 加一层锁只会让吞吐变差，且不解决"两个 S07 实例"的问题。

**但 Saga 需要防重入**：`cs_plan_active_uniq` 保证"一个 plan 只有一个 RUNNING Saga"。两个 apply 用不同 command_id 打同一个 plan → 第二个撞唯一索引 → 409 `CONCURRENT_CHANGE`。

### 6.4 Saga 步骤的幂等

每一步的 `command_id` 必须**确定性派生**，以便崩溃后重放同一命令：

```
command_id(step_n) = "cmd_" + base32(sha256(plan_id | "saga" | step_n))[:26]
```

确定性派生让"重放步骤"天然幂等——调 S06/S08 时带同一个 command_id，对方返回 `replayed:true`。

**不能每步生成随机 command_id**：崩溃恢复时会生成新的，导致重复创建内容。

### 6.5 `change_plan_patch` 的校验

`apply` 的步骤 4 是"补丁可应用"的纯计算校验：

```
对每条 patch：
  1. 读 target_ref 指向的对象（S06 revision 7）
  2. 用 path 取当前值，必须 == before_value
  3. 若不等 → PATCH_PRECONDITION_FAILED
```

这一步在**本地**做（读 S06 的 revision 是只读）。它防的是"prepared revision 8 与 revision 7 之间的字段已经变了"——虽然 revision 7 是不可变的，但 `before_value` 可能是错的（比如解析时读的是另一个 revision）。

---

## 7. 可观测

### 7.1 指标

| 指标 | 类型 | 用途 |
|---|---|---|
| `s07_plan_total{state}` | counter | `CLARIFICATION_REQUIRED` / `POLICY_DENIED` 占比 |
| `s07_plan_duration_seconds{phase}` | histogram | `parse` / `prepare` / `impact` / `policy` 各段耗时 |
| `s07_plan_to_apply_seconds` | histogram | 计划生成到用户确认的时间 |
| `s07_apply_total{result}` | counter | `applied` / `failed` / `abandoned` |
| `s07_saga_duration_seconds` | histogram | Saga 总耗时 |
| `s07_saga_step_duration_seconds{step}` | histogram | 哪步最慢（通常是 S06 或 S09） |
| `s07_clarification_total{kind}` | counter | 哪类澄清最多 |
| `s07_clarification_age_seconds` | histogram | 用户多久答复 |
| `s07_plan_expired_total` | counter | 过期率；高说明用户犹豫或估价不吸引人 |
| `s07_patch_precondition_failed_total` | counter | > 0 说明解析或并发有问题 |
| `s07_impact_regen_nodes` | histogram | 一次修改重做多少节点 |
| `s07_cross_episode_plans_total` | counter | 跨集修改频率 |
| `s07_cost_estimate_upper_micro{currency}` | histogram | 修改成本分布 |

### 7.2 审计要求

`change_saga_step` 的每一行必须能在事后回答：

- 谁发的命令（`actor`，来自 `context`）
- 什么时候
- 调了哪个服务、哪个 command_id
- 返回了什么（响应摘要，不是全文）

这是 docs/16 E39"可证明操作链"的落点之一。

### 7.3 每日检查

1. `state='APPLYING'` 超过 10 分钟的 plan（Saga 卡住）
2. `state='PLANNED'` 且已过期但定时任务没扫到的（扫描器故障）
3. `change_plan` 中 `impact_analysis_id` 对应的 S09 analysis 已过期/`STALE_FENCE` 的比例
4. Saga 步 6（commit）状态为 RUNNING 且超过 3 分钟的（E13 风险点）

---

## 8. 测试清单

### 8.1 E03 · 仰拍 4 秒局部改镜

| 编号 | 场景 | 断言 |
|---|---|---|
| T07-01 `[E03]` | "第3场第2镜改仰拍" | 解析出 `shot_3_2`；`/camera/angle: eye_level → low`；`camera.angle` 值在 S06 schema 的 enum 内 |
| T07-02 `[E03]` | "缩到4秒"（绝对值） | `/duration/target_seconds: 6 → 4` |
| T07-03 `[E03]` | "缩2秒"（相对值） | 读当前值算得 4；`before_value` 正确 |
| T07-04 `[E03]` | 派生改动 | `derived_adjustments` 含 `end_pause`，`change_plan_patch.derived_from` 指向 `/duration/target_seconds` 的 seq |
| T07-05 `[E03]` | 派生改动的来源 | `derivation_rule` 是 S06 返回的规则名，不是 S07 硬编码 |
| T07-06 `[E03]` | 影响与 S09 一致 | `impact` 逐字段等于 S09 的 `result`；`unused` 无 |
| T07-07 `[E03]` | 成本上界 | `estimated_cost_upper_micro` 来自 S25 的估价，不是 S07 估的 |
| T07-08 `[E03]` | 只重做相关子图 | `impact.regenerate_node_ids` 不含 `shot_3_4.*` |
| T07-09 `[E03]` | apply 后 | S08 的 `changed_node_epochs` 与 S09 的清单**不必相同**；S07 原样记录 |

### 8.2 E04 · 缺集上下文 / 对白超过 4 秒

| 编号 | 场景 | 断言 |
|---|---|---|
| T07-10 `[E04]` | 无 `episode_id`，series 内唯一匹配 | `resolution=SERIES_UNIQUE_MATCH`，继续，带 warning |
| T07-11 `[E04]` | 无 `episode_id`，多集匹配 | 409 `CLARIFICATION_REQUIRED`，`kind=MISSING_EPISODE_CONTEXT`，options ≥ 2 个候选集 |
| T07-12 `[E04]` | 有 `episode_id` 但镜头编号不唯一 | `kind=AMBIGUOUS_SHOT`，options 是候选 shot |
| T07-13 `[E04]` | 对白最短 5.2s，目标 4s | `kind=DIALOGUE_EXCEEDS_DURATION`，options 含"延到 5.2"/"重写对白"/"取消" |
| T07-14 `[E04]` | 同上，**不出现**任何"删台词"选项 | 断言 options 的 label 与 consequence 里没有删除对白的动作 |
| T07-15 `[E04]` | 无人值守模式遇到对白超长 | 仍是 `CLARIFICATION_REQUIRED`，**不自动选** |
| T07-16 `[E04]` | 澄清答复"延到 5.2" | 同 `plan_id` 回到 PLANNING；新 plan_hash；时长变 5.2 |
| T07-17 `[E04]` | 澄清超时不答复 | plan 过期，`EXPIRED`；**不自动选默认** |
| T07-18 `[E04]` | 只有 1 个候选选项的情形 | 不允许建 clarification（DB 约束 `>= 2`）；应改为直接做并记 derived |

### 8.3 E08 · 改前集钥匙归属

| 编号 | 场景 | 断言 |
|---|---|---|
| T07-20 `[E08]` | 修改涉及剧情事实 | 先调 S16 **preview**，不提交 |
| T07-21 `[E08]` | preview 后 | `memory_migration_required=true`；`impact_summary` 有跨集信息 |
| T07-22 `[E08]` | 影响含已交付集 | `by_episode[].delivered=true`；`warnings` 明确 |
| T07-23 `[E08]` | 已交付集默认处置 | **不重做**；只标 `memory_stale` |
| T07-24 `[E08]` | 用户选"迁移并重做" | 需要 `budget_increase` gate；成本显著上升且展示 |
| T07-25 `[E08]` | 用户没看到影响就 apply | 不可能——apply 只能在 `PLANNED`（影响已算完）之后 |
| T07-26 `[E08]` | apply 时 Saga 多一步 S16 提交 | 新 checkpoint 与旧 manifest 的映射可查 |
| T07-27 `[E08]` | 跨集修改取消 | S16 未被提交（preview 无副作用） |

### 8.4 E13 / 幂等 / 并发

| 编号 | 场景 | 断言 |
|---|---|---|
| T07-30 `[E13]` | commit 成功但 S07 崩溃 | 重启后查 S08 的 `change_plan_ref` 找到本 plan → 判定已成功 |
| T07-31 `[E13]` | commit 未到、S07 崩溃 | 重启后查 S08 未找到 → 用同 command_id 重试 |
| T07-32 `[E13]` | `IN_PROGRESS` 僵死 | 60s 后 janitor 依据 S08 是否已有该 revision 判定并重置 |
| T07-33 `[E12]` | 同 command_id 同 body | 返回首次响应；不生成第二个 plan |
| T07-34 `[E12]` | 同 command_id 不同 body | 409 `IDEMPOTENCY_KEY_REUSED` |
| T07-35 并发 | 两个 apply 不同 command_id 打同一 plan | 一个 success，一个 409 `CONCURRENT_CHANGE` |
| T07-36 并发 | 两个 plan 同时 apply 打同一 revision | 一个成功，一个 409 `MANIFEST_REVISION_CONFLICT` |
| T07-37 `[E28]` | 跨租户读 plan | 404 |
| T07-38 `[E01]` | 独立升级 S07 | 不需要 S06/S09 同时升级 |
| T07-39 `[E32]` | 重放全部事件 | plan 与 saga 状态不变；不发外部副作用 |
| T07-40 计划不可变 | 生成后再查 | `plan_hash` 不变；`patches` 不变 |
| T07-41 三道闸 | 用旧 plan_hash apply | 409 `PLAN_HASH_MISMATCH` |
| T07-42 三道闸 | 用过期 plan apply | 409 `PLAN_EXPIRED` |
| T07-43 三道闸 | 期间 fence 前进 | 409 `STALE_EXECUTION_FENCE` |
| T07-44 政策复查 | 生成后政策被改严 | apply 时重新 evaluate → `deny` → 409 `POLICY_DENIED` |
| T07-45 政策复查 | 生成后 kill switch 打开 | apply 时 `abstain` 或 `deny`；**不因"计划已批准"而放行** |

---

## 9. 契约冲突与待裁决（W0 必修）

| # | 冲突 | 本文采用 | 需要谁改 |
|---|---|---|---|
| C07-1 | catalog 声明 `GET /change-plans/{id}`；**`contracts/openapi.v2.yaml` 没有这条**（只有 POST 与 `:apply`） | 按 catalog 补 `GET` | `contracts/openapi.v2.yaml` |
| C07-2 | `execution-context.v2` 的 `node_id` + `node_epoch` 是单节点语义，但 change plan 是集级/多节点操作。示例 `enterprise-change-plan.json` 里塞了 `node_id:"shot_3_2.video"`，但 `impact` 含 7 个节点 | 只借用 context 的集级字段；`node_id`/`node_epoch` 不作为计划语义 | 需要"集级 execution context"或把两字段改可选 |
| C07-3 | docs/04 §2 要求 ChangePlan 含 **`source hashes`、`impact_watermark`（顶层）、`policy_version`、`memory_checkpoint`（顶层）**；schema 里这些只在 `context` 或 `impact` 的嵌套位置 | 用 `context.memory_checkpoint`、`context.policy_ref`、`impact.lineage_watermark`；无顶层的 source hashes | 若要逐字符合 docs/04，需扩 schema（新增可选顶层字段） |
| C07-4 | S07 的崩溃恢复依赖"能从 S08 的 revision 里查到 `change_plan_ref`"，但 `episode-manifest.v2` 的 `manifest_revision.payload` 结构没有显式要求包含它 | S08 `:commit` body 有 `change_plan_ref`；需明确它落进 revision 溯源 | `contracts/episode-manifest.v2.schema.json` 或 S08 实现约定 |
| C07-5 | `enterprise-change-plan.v2` 是 `additionalProperties:false` 的全 required schema，**无法包装状态**（`state`、`replayed`、`saga_id`） | 状态走 header（`X-Plan-State`、`X-Idempotent-Replay`、`X-Saga-Id`） | 契约注释；或新增 `change-plan-envelope.v2` |
| C07-6 | `CLARIFICATION_REQUIRED` 是一个**成功的业务结果**，但不满足 plan schema 的 `patches.minItems:1`，无法用 2xx 表达 | 409 + `details.clarification` | 需要"澄清"的独立响应形态 |
| C07-7 | `apply` 的 `Operation.state` 枚举（`accepted/waiting_*/…`）没有 `applied`，且 `Operation` 与 `Run.state` 是两套词汇 | apply 后查 `GET /change-plans/{id}` | 契约注释或扩 Operation |
| C07-8 | `automation-policy.v2` 的 `overrides[].scope_type` 无 `run`（S02 G1）。跨集修改时"这次 Run 的策略"无法表达 | 按 episode 覆盖 | `contracts/automation-policy.v2.schema.json` |
| C07-9 | 无 `contracts/change-plan-patch.v2.schema.json`；`patches[]` 内嵌在 `enterprise-change-plan.v2` 里，缺独立的 `derived_from`/`derivation_rule` 字段（schema 里没有） | 本文 §2.2 的表结构有这两列，但**契约里表达不了派生** | 扩 `patches[]` 的字段，或新增 schema |
| C07-10 | docs/07 §3 要求"同时创建 memory impact plan"；没有契约定义它是什么（是 `change_plan` 的一个 `kind`？还是独立对象？） | 用 `change_plan.memory_migration_required=true` 近似 | 需裁决是否有独立 `memory-impact-plan` 契约 |

---

## 10. 未决问题

1. **解析的边界**。"第3场第2镜"这类编号是 S06 的稳定 id 还是人类可读编号？如果是后者，编号到 id 的映射由谁维护、编号变化时怎么办？本文假设有 `resolved_shot_id`，但映射的实现不在任何服务里。
2. **`plan_hash` 的跨实现一致性**。与 S09 的 `analysis_hash` 同一问题：JCS 需在 Python 与前端 JS 里得到同一结果。建议只由服务端计算，前端只透传。
3. **`prepared_revision` 的 GC 责任**。S07 生成了 S06 的 revision 8，若 plan 过期，revision 8 无人引用。谁负责回收？S08 §4.1 说"保留期后由所有者 GC"，但"保留期"是多久、由谁的配置决定，未定。
4. **跨集修改的成本展示精度**。`estimated_cost_upper_micro` 是单值上界。跨集时用户可能需要"逐集成本"来决定迁移哪些集。`impact_summary.cost_breakdown` 有按 kind 的拆分，但没有按集的。需要成本数据支撑后再定。
5. **`SERIES_UNIQUE_MATCH` 是否应该允许**。当前设计允许"series 内唯一匹配"继续执行并带 warning。另一种更保守的做法是一律澄清。E04 的措辞是"没有集上下文…返回具体澄清选项"，倾向后者。**本方案选择了较宽松的做法，需要裁决。**
6. **`change_saga_step` 的重跑**。若第 3 步（确认 S06 revision 8 仍有效）成功、第 4 步失败，重试时应该从第 3 步还是第 4 步开始？当前设计是"从失败步开始"，但若依赖已变（revision 8 被 GC），需要回到第 3 步。Saga 的"重入点"规则未写死。
7. **`unattended_batch` 下跨集修改是否允许**。unattended 不授权删台词，那它是否授权"改前集剧情"？本文假设不允许（必须有人的显式选择），但 docs 没有明确。
