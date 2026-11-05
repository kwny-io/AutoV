# S19 · Agent 运行时与工具沙箱 · 实现方案

状态：design_only。服务标识 `agent-runtime`，schema `agent_runtime`，数据所有权：`AgentDefinitionRevision` / `AgentTask` / 执行步骤 / 工具调用审计 / 输出校验 / 允许工具集。

配套：[docs/08](../docs/08-agents-prompts.md)（角色与 AgentTask 契约）、[S01 §10.4](S01-tenant-access.md)（租户边界）、[S26](S26-generation-jobs.md)（唯一付费路径）、[S02](S02-governance-policy.md)（策略上下文）、[S18](S18-context-retrieval.md)（ContextPack）、[S05 §3.2](S05-breakdown.md) / [S06 §7.3](S06-storyboard.md)（有界纠正的两个消费者）、[S40](S40-human-review.md)（`NEEDS_REVIEW` 的去处）、[S35](S35-technical-qc.md)（缺陷与证据的分界）。

---

## 0. 一句话边界

**S19 是 `AgentDefinitionRevision`、`AgentTask`、执行步骤、工具调用审计、允许工具集的唯一写者；它把一次有界推理执行成一个不可变的候选结构交回发起方。它不写任何业务状态，不判定任何业务正确性，不持有任何供应商凭证，不授予任何 Agent 比发起者更大的权限。**

| 写 | 不写 |
|---|---|
| `agent_definition_revision`（13 个角色的版本化定义） | 剧本、场次、分镜、资产（S04/S05/S06/S10） |
| `agent_task` 及其状态机 | 预算余额、配额（S23/S22） |
| `agent_step`、`tool_call_audit` | `execution_fence`、`node_epoch`、accepted pointer（S08/S21） |
| `task_result`（候选结构的不可变字节） | 供应商任务、提交意图、未知提交态（S26） |
| `task_checkpoint` | 正史、记忆、ContextPack（S16/S18） |
| 角色→工具白名单 | 模型路由决策、候选筛选（S25） |

三个明确不做：

1. **不做内容权威。** 输出永远叫 `proposal`，永远带 `content_hash`，发起方永远要重校验并自己铸造 ID（[S05 §3.2](S05-breakdown.md) 第 ③ 步）。
2. **不做第二个路由。** S19 不选模型、不管价格、不算候选。它只说"我要一次 `text` 模态生成"，剩下交给 S25→S26。
3. **不做第二个预算账本。** `agent_task.cost_micro` 只用于**本任务内**的熔断，不跨任务累计、不充当余额、不对外声明为账单事实（§1.6）。

一条必须坚持的原则：**Agent 只能提建议，平台只采信结构化输出。** 任何"让 Agent 直接改状态""让 Agent 自带可信 `tenant_id`""给 Agent 一个比发起者更宽的 context"的实现，都是绕过 [AGENTS.md](../AGENTS.md) 红线，不是优化。

---

## 1. 关键区分（最容易写错的六处）

### 1.1 四个"身份"，少一个就出事

| 概念 | 作用域 | 谁写 | 混用后果 |
|---|---|---|---|
| `role` | 平台（13 个固定值） | `contracts` | 用角色名做权限判断 → 新增角色即越权 |
| `AgentDefinitionRevision` | `(tenant, definition_id, revision)` | **S19** | 跑 head 而不是 pinned revision → 不可复现 |
| `AgentTask` | `(tenant, task_id)` | **S19** | 一次重试变成两个 `task_id` → 费用翻倍 |
| `execution_context` | 发起方从 TCT 派生 | **发起方**，S19 只读 | S19 自己造 context → 租户边界失效 |

`role` 是**分类**，不是权限。权限永远来自 `execution_context` 与 S01 的判定。`agents/*/agent.yaml` 里的 `responsibility` 同理，是给人看的文字。

### 1.2 AgentDefinition 是内容，不是配置

一个 `AgentDefinitionRevision` 固定六件事：system 模板引用（S20 的 `TemplateRevision`）、输出 JSON Schema 引用、工具白名单、步数/token/时间/费用四道闸、评测集引用、模型能力要求。**六件事任何一个变化都必须产出新 revision。**

不能把 `limits` 放在 Deployment 的环境变量里当"运行时参数"——那样一次 HPA 滚动就会让同一批在途任务用不同的上限，`usage_trace` 就失去可比性。上限是定义的一部分，跟着 revision 走。

### 1.3 `proposal` 与业务记录

S19 的产出**不是**业务对象。它没有 `scene_id`，没有 `shot_id`，没有 `breakdown_id`。

发起方拿到 `proposal` 后必须：结构校验 → 语义校验 → 丢弃并重铸所有 ID → 落库。这条纪律在 [S05 §3.2](S05-breakdown.md) 与 [S06 §7.3](S06-storyboard.md) 各写了一遍。S19 侧的对应义务是：**绝不回填任何业务 ID**，即使模型"猜"出来一个格式正确的 `scn_...`，也原样留在 `proposal` 里当字符串，由发起方拒绝。

一个 `proposal` 里出现 `tenant_id` 字段，本身就是 prompt 被污染的强信号。发起方应 422 `AGENT_SUPPLIED_TENANT`（S05 已如此规定）。S19 的策略是**尽量不让它出现**：输出 schema `additionalProperties: false`，且 `agent_task.output_schema` 在定义期就禁止 `tenant_id`、`project_id`、`series_id` 出现在根级属性名里（§3.2 的定义期校验）。

### 1.4 有界纠正：一次纠正 = 一个新任务

docs/08 §2：结构非法允许有界纠正，超上限转失败或审核，**不通过循环"自我反思"无界增加账单**。

实现上最容易被写错的地方：把纠正做成同一次任务内部的 `for` 循环。那样有三个后果——幂等键覆盖了多次付费调用、`usage_trace` 无法按任务核对、取消一个任务无法停止纠正。

所以：**纠正是新的 `AgentTask`**，带 `correction_of_task_id` 与 `correction_round`；每个纠正有自己的 `command_id`、自己的预算、自己的结果字节。链最多 2 环，**由 S19 强制**（不是由调用方自觉）：

```
round 0：原任务                    → SUCCEEDED / FAILED
round 1：correction_of=round0      → 允许
round 2：correction_of=round1      → 允许
round 3：correction_of=round2      → 422 CORRECTION_LIMIT_REACHED
```

`correction_round` 由服务端从链上推导，**不接受客户端传入**。这样 S05/S06 想"再来一次"也超不过 2，而 [S05 §3.2](S05-breakdown.md) 写"最多 2 次纠正"时不需要信任调用方。

### 1.5 工具不是函数调用

每一次工具调用都要过三道门，缺一门就变成"LLM 有 shell 权限"：

| 门 | 判据 | 失败 |
|---|---|---|
| ① 白名单 | `tool_name` ∈ 该 `definition_revision` 的允许集 | 422 `TOOL_NOT_ALLOWED`，不发请求 |
| ② 授权 | `POST /permission-checks`（S01）对该工具声明的 `permission_key` 在 `scope` 上返回 `allow` | 记 `deny` 到审计，回给 Agent 一条结构化拒绝，计入步数 |
| ③ 出网 | 工具属于外部检索类 → 必须匹配企业出网策略 | 同上，理由 `EGRESS_DENIED` |

**工具永远不持有密钥。** 需要凭证的工具由 per-call broker 发短 TTL 句柄，句柄不进模型上下文、不进 `tool_call_audit` 的可见字段、不进日志。

**工具返回的文本只是数据。** docs/08 §2：检索文本不改变执行策略。实现上：工具结果进入下一次模型调用的 `tool` 消息，**不进入** system 模板、不参与 `output_schema` 选择、不改变剩余步数或费用上限。任何"检索到'忽略之前的指令'"之类的内容都只能作为被引用的证据出现在 `proposal` 里。

### 1.6 `cost_micro` 是熔断，不是账本

`agent_task.cost_micro` 累加的是本任务已发生的、**已由 S26 结算**的金额。它的唯一用途是"到上限就停"。

三个不能：

- 不能拿它对账（对账归 S23；S19 的累加是最终一致的，事件可能晚到）。
- 不能跨任务求和当作租户花费（多算或漏算都会发生）。
- 不能因为 `cost_micro` 低于上限就认为一定还有钱（真正的准入在 S23/S22，每次调用都重新问）。

这与 [S26 §1.1](S26-generation-jobs.md) 的"单写者"是同一条纪律在预算维度上的应用。

---

## 2. 输入契约

### 2.1 传输与身份

内部 mTLS + workload identity，HTTP/JSON。写命令带 [execution-context.v2](../contracts/execution-context.v2.schema.json) 的全部必填字段。

```jsonc
{
  "schema_version": "2.0",
  "command_id": "cmd_...",
  "tenant_id": "tnt_...",            // 来自 TCT，不接受 body 自授
  "project_id": "prj_...",
  "series_id": "ser_...",
  "episode_id": "ep_...",
  "branch_id": "br_...",
  "home_region": "region_demo",
  "home_cell": "cell_a1",
  "manifest_ref": {"service_id":"S08","id":"man_...","revision":12,"content_hash":"..."},
  "node_id": "shot_3_2.script",
  "node_epoch": 4,
  "execution_fence": 2,
  "memory_checkpoint": {"commit_sequence": 88, "content_hash": "..."},
  "policy_ref": {"service_id":"S02","id":"pol_...","revision":3,"content_hash":"..."},
  "actor": {"principal_id":"prn_...","principal_type":"human","delegation_id":"dlg_..."},
  "command_id": "cmd_...",
  "request_hash": "sha256:...",
  "trace_id": "4bf92f35..."
}
```

`tenant_id` 与 `actor` 由发起方的 TCT 派生；S19 **只读不写**。S19 自己不带凭证链到任何下游：所有下游调用的身份是 S19 的 service principal，租户上下文通过请求体里的 `tenant_id` + S01 的 `permission-checks` 表达（[S01 §10.4](S01-tenant-access.md)）。

### 2.2 命令清单

| 方法 | 路径 | 作用 | 需要权限 |
|---|---|---|---|
| `POST` | `/agent-tasks` | 创建一个 Agent 任务（或一个纠正任务） | `agent.task.create` |
| `POST` | `/agent-tasks/{id}:cancel` | 取消（软取消，见 §6.4） | `agent.task.cancel` |
| `POST` | `/agent-definitions` | 创建角色定义容器 | `agent.definition.manage` |
| `POST` | `/agent-definitions/{id}/revisions` | 发布一版定义 | `agent.definition.manage` |

> catalog 只声明 `POST /agent-tasks`。其余路径见 §11 C3。

### 2.3 查询清单

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/agent-tasks/{id}` | 任务状态、步骤、用量、终止原因 |
| `GET` | `/agent-tasks/{id}/result` | 结果字节（含 `content_hash`） |
| `GET` | `/agent-tasks/{id}/steps` | 步骤时间线（含工具审计摘要） |
| `GET` | `/agent-definitions/{id}/revisions/{revision}` | 一版定义（含工具白名单） |
| `GET` | `/agent-roles` | 13 个角色与各自的默认定义 |

### 2.4 `POST /agent-tasks`

```jsonc
{
  "role": "breakdown",
  "definition_revision": 7,
  "goal": "把第 3 集已 seal 的剧本拆成场次",
  "input_refs": {
    "context_pack_ref": {"service_id":"S18","id":"cpk_...","revision":3,"content_hash":"..."},
    "source_refs": [
      {"service_id":"S04","id":"scr_...","revision":7,"content_hash":"..."}
    ]
  },
  "output_schema_ref": {"id":"scene-breakdown-proposal","revision":2,"content_hash":"..."},
  "budget_scope_id": "bsc_ep3_agent",
  "limits": {
    "max_steps": 24,
    "max_tokens": 120000,
    "max_wall_seconds": 300,
    "max_cost_micro": 400000
  },
  "correction_of_task_id": null,
  "correction_feedback": null,
  "deadline": "2026-11-05T12:00:00Z"
}
```

- `role` 必须是注册的 13 个之一。`definition_revision` 必须属于该 `(tenant, role)` 且 `state='PUBLISHED'`。**不接受"用最新版"这种省略**——那会让同一个 `command_id` 在不同时间重放得到不同结果（违反 E12）。
- `limits` 只能**收紧**定义里的上限，不能放宽。放宽 → 422 `LIMITS_EXCEED_DEFINITION`。这条防止调用方用一个大 `max_steps` 把有界纠正变成无界。
- `output_schema_ref` 必须与定义里登记的 schema 一致，或由定义显式允许覆盖（`allow_schema_override: true`）。不一致 → 422 `SCHEMA_NOT_ALLOWED`。
- `correction_of_task_id` 非空时，`correction_feedback` 必填，且必须是结构化的违规列表（`violations[]`），不是自由文本。自由文本会让模型把纠正当成"再想一遍"，而这正是 docs/08 禁止的无界反思。
- `context_pack_ref` 的 `perspective` 由 S19 向 S18 回读校验（[S18 §8](S18-context-retrieval.md)：pack 不可"降级使用"）。角色声明的 `required_perspective` 与 pack 不符 → 422 `PERSPECTIVE_MISMATCH`。

### 2.5 `POST /agent-tasks/{id}:cancel`

```jsonc
{ "command_id": "cmd_...", "reason": "user_cancelled" }
```

语义见 §6.4：**不假退款**。已经提交给 S26 的调用会继续跑完并结算，S19 只是不再发起新步骤。

### 2.6 `POST /agent-definitions/{id}/revisions`

```jsonc
{
  "parent_revision": 6,
  "system_template_ref": {"service_id":"S20","id":"tpl_writer","revision":9,"content_hash":"..."},
  "output_schema_ref": {"id":"script-proposal","revision":4,"content_hash":"..."},
  "tools": [
    {"tool_name":"drama.memory.query","permission_key":"memory.read","scope_kind":"series",
     "required_scope":"read","egress":"internal","max_calls":6},
    {"tool_name":"drama.context.preview","permission_key":"context.read","scope_kind":"episode",
     "required_scope":"read","egress":"internal","max_calls":3}
  ],
  "limits": {"max_steps": 24, "max_tokens": 120000, "max_wall_seconds": 300, "max_cost_micro": 400000},
  "required_capabilities": ["long_context", "json_schema_output"],
  "eval_set_ref": {"id":"eval_writer_v4","revision":2,"content_hash":"..."},
  "allow_schema_override": false
}
```

定义期校验（全部 422，不允许"运行时再说"）：工具名必须在平台工具注册表（§11 C7）；`permission_key` 必须在 S01 权限注册表；`required_scope` 不能宽于该角色的最大 scope；`output_schema_ref` 的根级属性不得包含 `tenant_id`/`project_id`/`series_id`/`branch_id`；`limits` 四项都必填。

### 2.7 错误码

| HTTP | `code` | 含义 | retryable |
|---|---|---|---|
| 400 | `INVALID_SCHEMA` | body 不符合 schema | 否 |
| 403 | `PERMISSION_DENIED` | 发起方无 `agent.task.create` | 否 |
| 404 | `NOT_FOUND` | 任务/定义不存在，或属于其他租户（E28） | 否 |
| 409 | `COMMAND_IN_PROGRESS` | 同 `command_id` 执行中 | 是 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 同 `command_id` 不同 body | 否 |
| 409 | `TASK_NOT_CANCELLABLE` | 已终态 | 否 |
| 422 | `UNKNOWN_ROLE` | 角色未注册 | 否 |
| 422 | `DEFINITION_NOT_PUBLISHED` | 引用了草稿/已退役定义 | 否 |
| 422 | `LIMITS_EXCEED_DEFINITION` | `limits` 放宽了定义上限 | 否 |
| 422 | `SCHEMA_NOT_ALLOWED` | 输出 schema 与定义不符且未允许覆盖 | 否 |
| 422 | `PERSPECTIVE_MISMATCH` | pack 的 perspective 与角色要求不符 | 否 |
| 422 | `CORRECTION_LIMIT_REACHED` | 纠正链已满 2 环 | 否 |
| 422 | `CONTEXT_PACK_STALE` | pack 的 watermark 落后于 manifest 的 `memory_checkpoint` | 是（重新取 pack） |
| 503 | `GENERATION_UNAVAILABLE` | S26/S25 不可用，未开始执行 | 是 |

**409 与 422 的分界**（沿用 [S08 §2.6](S08-revision-snapshot.md)）：409 是状态/版本冲突；422 是这个组合永远不成立。`CORRECTION_LIMIT_REACHED` 是 422——再试一万次也还是三环。

### 2.8 幂等语义

```
request_hash = sha256( JCS( body 去掉 command_id 与 request_hash ) )
```

`(tenant_id, command_id)` 主键；`IN_PROGRESS` 同 hash → 409 可重试；`DONE` 同 hash → 回放首次响应并附 `replayed: true`；任何不同 hash → 409 `IDEMPOTENCY_KEY_REUSED`。30 天过期。

**第二层保证**：`agent_task.create_idempotency_key` 上的唯一索引 `(tenant_id, role, correction_of_task_id, correction_round)` 之外，还有 `(tenant_id, task_id)` 主键。即使幂等表被清理，同一个 `task_id` 的重放仍返回既有任务而不是新建——任务一旦创建，身份就固定了。

一条 S19 独有的规则：**纠正任务的 `command_id` 必须与父任务不同**。同 `command_id` 携带 `correction_of_task_id` → 409 `IDEMPOTENCY_KEY_REUSED`（因为 body 变了）。这不是限制，是防止调用方用"重试"把纠正次数刷满。

---

## 3. 核心逻辑与状态机

### 3.1 一次任务的完整生命周期

```
① 准入（同步，≤200ms）
   ├─ 幂等 begin
   ├─ 角色 / 定义 / limits / schema 校验（纯本地）
   ├─ 租户与授权：POST /permission-checks（agent.task.create @ episode/project）
   ├─ 策略：POST /policy-decisions:evaluate（kind=agent_task_admission，§11 C4）
   ├─ 上下文：回读 S18 pack，校验 perspective 与 watermark
   └─ 写 agent_task(ACCEPTED) + outbox?（否：不发布，见 §4.2）
        ↓
② 入队（按 role → task_queue = agent-<role>）
        ↓
③ 执行循环（worker，每步落 agent_step）
   ├─ 组 prompt（定义绑定的 S20 模板 + pack 摘要 + 上一步结果）
   ├─ 模型调用 → 经 S25 路由 → POST /generation-jobs（S26）→ 等待
   ├─ 工具调用 → 三道门（§1.5）→ tool_call_audit
   ├─ 输出结构校验（JSON Schema）
   └─ 收敛判定：合法 → ④；非法 → ⑤
        ↓
④ 终态 SUCCEEDED：写 task_result + 事件 agent.task.completed
        ↓
⑤ 非法但不是死路：FAILED(reason=STRUCTURE_INVALID)，交回调用方
   调用方自行决定是否发起纠正（新任务，round+1）
```

第 ⑤ 步的措辞很重要：**S19 不自己发起纠正。** 它不知道什么算"语义上应该纠正"（那是 S05/S06 的业务判断），只知道"结构不合法"。把纠正决定权留在发起方，S19 就永远不会自行放大账单。

### 3.2 角色分派与定义期校验

13 个角色各自一个 Deployment、一个 Task Queue、一套限额。S19 的调度器只做一件事：按 `role` 把任务投到 `agent-<role>`。

不能共用一个无限额进程池，理由在 docs/08 §1 已写，这里补实现层的后果：共用池会让 `writer` 的一次长上下文任务挤掉 `qc-reviewer` 的短任务，而 `qc-reviewer` 在无人值守链路上是阻塞点（[S02](S02-governance-policy.md) 的 gate 依赖它）。分开队列是可用性设计，不只是配额设计。

定义期校验清单（发布时全部执行，任一失败即 422）：

```python
def validate_definition_revision(defn):
    for t in defn.tools:
        assert t.tool_name in TOOL_REGISTRY                     # C7
        assert s01.permission_key_exists(t.permission_key)
        assert scope_rank(t.required_scope) <= s01.role_max_scope(defn.role)
        assert t.egress in ("internal", "external_allowlisted")
    assert defn.limits.max_steps <= HARD_MAX_STEPS[defn.role]    # 平台硬顶，防止单角色被配成无界
    assert defn.limits.max_cost_micro > 0
    assert "tenant_id" not in root_properties(defn.output_schema_ref)
    assert "project_id" not in root_properties(defn.output_schema_ref)
    assert defn.system_template_ref.content_hash is not None
    assert eval_set_exists(defn.eval_set_ref)                   # 没有评测集的定义不许发布
```

最后一条是刻意的：docs/08 §4 要求每个角色有评测集。允许"先上线再补评测"的定义发布，等于允许一个没有任何质量基线的角色进入生产。

### 3.3 工具调用门

```python
def call_tool(task, defn, call):
    grant = pick_tool(defn.tools, call.tool_name)          # ① 白名单
    if grant is None or grant.used >= grant.max_calls:
        return deny(call, "TOOL_NOT_ALLOWED")

    verdict = s01.permission_checks(                        # ② 授权
        principal_id=task.execution_context.actor.principal_id,
        checks=[{"permission_key": grant.permission_key,
                 "scope_kind": grant.scope_kind,
                 "scope_id": scope_id_of(task, grant.scope_kind),
                 "purpose": f"agent.{defn.role}"}])
    if verdict.results[0].verdict != "allow":
        return deny(call, verdict.results[0].reason_code)    # 结构化拒绝回给模型，计入步数

    if grant.egress == "external_allowlisted":               # ③ 出网
        if not egress_allows(task.tenant_id, call.target): return deny(call, "EGRESS_DENIED")

    result = tool_broker.invoke(grant.tool_name, call.arguments, ttl=60)
    audit.write(task, call, result, grant)                   # 先审计后返回
    return ok(result)   # 结果只作为数据，不改变策略（§1.5）
```

三条实现约束：

1. **`permission-checks` 的 key 一次最多 32 条**，所以批量按"本步要用的工具"聚合，不要逐次往返（S01 §2.6 已说明理由：P95 会变成 N×RTT）。
2. **`scope_hint` 不是授权**（[S01 §4.1](S01-tenant-access.md)）。`execution_context.scope_hint` 只用于本地粗筛，任何一次真实调用都必须走 `permission-checks`。
3. **审计先于返回。** 工具结果落 `tool_call_audit` 成功之后才交给模型。否则一次崩溃就会产生"用过但无记录"的调用，E39 的审计链断在那里。

### 3.4 有界纠正（S05/S06 共同引用）

[S05 §3.2](S05-breakdown.md) 与 [S06 §7.3](S06-storyboard.md) 都指向本节。规格如下：

| 项 | 值 |
|---|---|
| 每版产物的纠正上限 | **2 次**（`correction_round ∈ {1,2}`） |
| 一次纠正的计量 | **一个独立 `AgentTask`**，独立 `command_id`、独立预算、独立结果字节 |
| 谁发起 | 调用方（S05/S06）。S19 不自动发起 |
| 谁强制上限 | **S19**。第 3 环 → 422 `CORRECTION_LIMIT_REACHED` |
| 纠正输入 | `correction_of_task_id` + `correction_feedback.violations[]`（结构化） |
| 定义是否可换 | **不可**。纠正必须用与父任务相同的 `definition_revision` |
| pack 是否可换 | **可**，但只能在 watermark 前进时换；换 pack 必须记入 `agent_task.input_refs` |
| 上游行为 | 超过上限 → 失败并交人工（[S05 §7.3](S05-breakdown.md)、[S40](S40-human-review.md)） |

`correction_feedback` 的结构：

```jsonc
{
  "violations": [
    {"rule":"SPEAKER_NOT_IN_SCENE","path":"/scenes/3/dialogue_lines/2","detail":"speaker '沈青' 不在本场角色需求内"},
    {"rule":"LINE_COVERAGE_GAP","path":"/scenes","detail":"第 88–91 行未被分配"}
  ],
  "previous_result_ref": {"id":"res_...","revision":1,"content_hash":"..."}
}
```

为什么必须是结构化列表而不是"再检查一遍"：自由文本会让模型倾向于重写整份产物，把 2 次纠正变成"两次全量重生成"，费用是结构化的 3–5 倍，而且上一次已经正确的部分会漂移。**只修 `violations` 指向的位置**，其余部分字节不变——这一点靠 `correction_feedback.previous_result_ref` 让模型看到原稿，并在校验时对比未受影响区域的 hash。

纠正链的记账：`agent_task.root_task_id` 指向 round 0 的任务，`correction_round` 是链上的序号。费用按任务分别记（因为每笔都有自己的 S26 结算），但**根任务可以聚合查询整条链**（`GET /agent-tasks/{id}` 返回 `chain: [{task_id, round, cost_micro, state}]`），给 S44 做"一次分场到底花了多少"。

### 3.5 四道闸

| 闸 | 触发 | 动作 |
|---|---|---|
| `max_steps` | `steps_used == max_steps` 且仍有未完成目标 | `FAILED(STEP_LIMIT)` |
| `max_tokens` | 累计 tokens（in+out）超过上限 | `FAILED(TOKEN_LIMIT)`，不再发起新调用 |
| `max_wall_seconds` | 任务创建至今超过上限 | `FAILED(TIME_LIMIT)`，在途调用继续结算 |
| `max_cost_micro` | `cost_micro + 下一步报价上界 > 上限` | `FAILED(COST_LIMIT)`，**不做这一步** |

第四道闸必须用**报价上界**判断，不能用"已经花了多少"：如果先发出去再算，一次 4 秒视频的价格就能把上限捅穿。S26 的 reserve 带 `amount_conservative_micro`，S19 用那个值预判。

`FAILED` 的四个理由都是**终态**，且都带 `stop_reason`。调用方看到 `STEP_LIMIT` / `TOKEN_LIMIT` 应该考虑换一个更窄的 `goal`，而不是直接重试同一个 body（重试同 body 只会再撞一次）。

### 3.6 状态机

```
            ┌──────────┐
            │ ACCEPTED │
            └────┬─────┘
                 │ lease 取得
            ┌────▼─────┐   cancel   ┌────────────┐
            │ RUNNING  ├───────────►│ CANCELLING │
            └────┬─────┘            └──────┬─────┘
                 │                         │ 在途调用结算完
        ┌────────┼─────────┐               │
        │        │         │               │
   ┌────▼────┐ ┌─▼──────┐ ┌▼────────────┐ ┌▼──────────┐
   │SUCCEEDED│ │ FAILED │ │NEEDS_REVIEW │ │ CANCELLED │
   └─────────┘ └────────┘ └─────────────┘ └───────────┘
```

| 迁移 | 触发 | 守卫 |
|---|---|---|
| `ACCEPTED→RUNNING` | worker 取得 lease | `lease_expires_at` 未被他人持有 |
| `RUNNING→SUCCEEDED` | 结果合法且持久化 | `task_result` 已写入且 hash 校验通过 |
| `RUNNING→FAILED` | 闸命中 / 不可恢复错误 | 已写 `stop_reason` |
| `RUNNING→NEEDS_REVIEW` | 定义声明 `on_unresolved='review'` 且结果带硬冲突 | 必须携带 `unresolved_issues[]` |
| `RUNNING→CANCELLING` | `:cancel` | — |
| `CANCELLING→CANCELLED` | 在途 S26 调用到达终态 | 结算完成 |
| `*→` 其他终态 | — | 不允许（终态不回归，与 [S08 §6.5](S08-revision-snapshot.md) 一致） |

`NEEDS_REVIEW` 是 S19 的**终态**，不是等待态：S19 已经交出了它能交的东西，`unresolved_issues` 才是产物。S40 收到通知后建 `ReviewTask`。这条区分很重要——如果 `NEEDS_REVIEW` 是被 S19 阻塞的等待态，那么 S40 停机就会让 S19 的 worker 全部占满，进而拖垮其他角色。

### 3.7 检查点与恢复

每一步结束写一条 `task_checkpoint`：

```jsonc
{
  "task_id": "agt_...",
  "checkpoint_seq": 7,
  "step_index": 6,
  "state_digest": "sha256:...",          // messages + tool 结果引用 + 剩余预算的规范化摘要
  "pending_generation_job_ref": "pj_...", // 非空表示正在等一次模型调用
  "messages_ref": {"id":"msg_6","revision":1,"content_hash":"..."},
  "remaining": {"steps": 17, "tokens": 84000, "cost_micro": 250000}
}
```

恢复顺序（worker 重启 / 节点漂移 / lease 被回收）：

1. 读最后一条 checkpoint → 拿到 `pending_generation_job_ref`；
2. `GET /generation-jobs/{id}`（S26）→ 若已终态，直接用结果继续；若仍在跑，继续等；若 `SUBMISSION_UNKNOWN`，**不重提**（[S26 §7.2](S26-generation-jobs.md) 铁律），任务转 `FAILED(GENERATION_UNKNOWN)` 等对账后人工处置；
3. 读 `messages_ref` 重建上下文，**不重新拼 prompt**——重拼会让同一 `task_id` 产生不同 `messages_hash`，结果不可复现；
4. `remaining` 用 checkpoint 里的值而不是重算，防止"重启后步数清零"把熔断绕开；
5. `checkpoint_seq` 单调，旧 checkpoint 永远覆盖不了新的（`WHERE checkpoint_seq > current`）。

### 3.8 不变量

1. `task_result` 一旦写入不可变；同一 `(task_id)` 只能有一条 SUCCEEDED 结果。
2. `step_index` 在任务内连续从 0 递增，跳号即视为 bug（不是"允许空洞"）。
3. 终态任务的 `cost_micro` 只增不减（迟到的结算事件只能加，不能减）。
4. 任何 `tool_call_audit` 行的 `task_id` 必须存在且属于同租户。
5. `agent_task.execution_context.tenant_id == agent_task.tenant_id`，否则拒绝创建（防"上下文与任务不同租户"）。
6. `correction_round ≤ 2` 且 `correction_of_task_id` 的链深等于 `correction_round`。
7. `agent_task.definition_revision` 的 `content_hash` 与创建时一致到任务结束——scheduler 不得"顺手升级到最新版"。

### 3.9 S19 与 `execution_fence` / `node_epoch`

**S19 既不产生也不推进这两个值。** S19 的"重试"是任务内步骤重试（同一个 `task_id`）；它不改变 `execution_fence`（那是 S08/S21 的），也不改变 `node_epoch`（那是被改节点的）。

唯一的接触点是**读取**：`agent_task.execution_context` 里带着创建时刻的 `execution_fence` 与 `node_epoch`，用来在结束时给结果打标。发起方拿结果落库前会自己比对当前 fence/epoch（[S08 §2.1](S08-revision-snapshot.md) 的 `STALE_DECISION`）。S19 不替它判，也不缓存这个判断。

一个具体的错误实现：S19 在任务创建后收到 `manifest.committed` 事件，就主动取消任务。**不要这样做**——取消的权威在 S21，S19 一取消就无法区分"用户取消"与"epoch 推进"，`stop_reason` 的统计也会失真。

---

## 4. 输出契约

### 4.1 `AgentTask` 查询响应

```jsonc
{
  "task_id": "agt_01J...",
  "role": "breakdown",
  "definition_ref": {"id":"def_breakdown","revision":7,"content_hash":"..."},
  "root_task_id": "agt_01J...",
  "correction_round": 0,
  "state": "SUCCEEDED",
  "stop_reason": "GOAL_REACHED",
  "goal": "把第 3 集已 seal 的剧本拆成场次",
  "input_refs": {
    "context_pack_ref": {"service_id":"S18","id":"cpk_...","revision":3,"content_hash":"..."},
    "source_refs": [{"service_id":"S04","id":"scr_...","revision":7,"content_hash":"..."}]
  },
  "result_ref": {"id":"res_01J...","revision":1,"content_hash":"sha256:..."},
  "usage": {
    "steps_used": 7,
    "tokens_in": 51200, "tokens_out": 8100,
    "cost_micro": 182000,
    "cost_state": "FINAL",
    "wall_seconds": 96,
    "tool_calls": 4,
    "tool_denials": 1
  },
  "chain": [{"task_id":"agt_01J...","round":0,"state":"SUCCEEDED","cost_micro":182000}],
  "created_at": "2026-11-05T09:12:00Z",
  "finished_at": "2026-11-05T09:13:36Z"
}
```

`stop_reason` 枚举：`GOAL_REACHED`、`STRUCTURE_INVALID`、`STEP_LIMIT`、`TOKEN_LIMIT`、`TIME_LIMIT`、`COST_LIMIT`、`POLICY_DENIED`、`GENERATION_FAILED`、`GENERATION_UNKNOWN`、`TOOL_DENIED_REPEATEDLY`、`USER_CANCELLED`、`UNRESOLVED_CONFLICTS`。

`cost_state`：`PROVISIONAL`（仍有在途 S26 结算）/ `FINAL`（全部结算到账）。**只有 `FINAL` 才能被 S44 当成本事实引用**，`PROVISIONAL` 只是本任务内熔断用的数（§1.6）。

### 4.2 `task_result`（候选结构）

```jsonc
{
  "schema_version": "2.0",
  "result_id": "res_01J...",
  "task_id": "agt_01J...",
  "role": "breakdown",
  "output_schema_ref": {"id":"scene-breakdown-proposal","revision":2,"content_hash":"..."},
  "proposal": { },
  "source_refs": [
    {"service_id":"S04","id":"scr_...","revision":7,"content_hash":"..."},
    {"service_id":"S18","id":"cpk_...","revision":3,"content_hash":"..."}
  ],
  "assumptions": ["第 3 集没有闪回，按线性时间拆分"],
  "unresolved_issues": [
    {"code":"AMBIGUOUS_LOCATION","path":"/scenes/5","detail":"剧本未说明内外景"}
  ],
  "confidence": 0.72,
  "suggested_actions": ["confirm_location:scene_5"],
  "usage_trace": {"steps": 7, "tool_calls": 4, "model_snapshots": ["m_...@2026-10"]},
  "content_hash": "sha256:..."
}
```

`content_hash = sha256(JCS(proposal + source_refs + assumptions + unresolved_issues))`。**不含 `usage_trace`**：用量是运行事实，不该改变内容指纹，否则同一份提案重跑一次 hash 就变了，"同输入复现"无法测。

`assumptions` 与 `unresolved_issues` 必须分开：前者是我做了但没被授权的猜测（"按线性时间拆分"），若判错整份产物可能作废；后者是我发现但无法解决的问题（"没说内外景"），只需要人补一个值。混用会让调用方分不清"要不要重跑"与"要不要补一条信息"。

**`proposal` 里绝不出现**：`tenant_id`、`project_id`、`series_id`、`branch_id`、任何业务对象 ID（`scene_id`/`shot_id`/`breakdown_id`）、任何金额。出现即视为输出污染，写结果时 422 `OUTPUT_CONTAINS_FORBIDDEN_FIELD`，任务转 `FAILED(STRUCTURE_INVALID)`。

### 4.3 发布事件：`agent.task.completed`

catalog 与 `contracts/event-topics.v2.json` 都声明 S19 发布 `agent.task.completed`，topic `drama.agent-runtime.v2`，`partition_key = tenant_id:aggregate_id`，`aggregate_id = task_id`，`aggregate_sequence` 取 `agent_task.publish_sequence`（每次进入终态 +1）。

```jsonc
{
  "specversion": "1.0", "schema_version": "2.0",
  "id": "evt_...", "source": "S19",
  "type": "agent.task.completed",
  "subject": "agent-task/agt_01J...",
  "time": "2026-11-05T09:13:36Z",
  "tenant_id": "tnt_...", "home_cell": "cell_a1",
  "aggregate_id": "agt_01J...", "aggregate_sequence": 1,
  "correlation_id": "cmd_...", "causation_id": "cmd_...",
  "traceparent": "00-4bf92f35-...-01",
  "data": {
    "role": "breakdown",
    "state": "SUCCEEDED",
    "stop_reason": "GOAL_REACHED",
    "root_task_id": "agt_01J...",
    "correction_round": 0,
    "result_ref": {"id":"res_01J...","revision":1,"content_hash":"sha256:..."},
    "usage": {"steps_used":7,"tokens_in":51200,"tokens_out":8100,"cost_micro":182000,"cost_state":"FINAL"},
    "node_id": "shot_3_2.script", "node_epoch": 4, "execution_fence": 2
  },
  "data_ref": {"service_id":"S19","id":"res_01J...","revision":1,"content_hash":"sha256:..."}
}
```

**事件只带引用，不带 `proposal` 正文。** 两个理由：产物可能有几十 KB，塞进事件会撞 Kafka 单条消息上限；更关键的是 `proposal` 里是未脱敏的剧情内容，而事件保留 14 天、副本更多（§11 C2 记录了这个权衡）。

消费者：**只有 S21（编排）与 S44（成本分析）**。S05/S06 **不消费这个事件**——它们是同步等待结果的调用方（`GET /agent-tasks/{id}`）。改消费事件会引入"事件到了但调用方已超时"的双路径问题。

### 4.4 不发布的事件

S19 **不发布**任何过程性事件（`agent.task.started`、`agent.step.completed`、`agent.tool.called`）：

- 它们是运行遥测，不是业务事实。进 OpenTelemetry trace（§8.3），不进 Kafka。
- 一旦发布，下游会有人拿它做进度条（S21 已有自己的进度模型）、做计费（对账归 S23）、做审计（审计归 S45 从 `tool_call_audit` 拉）。三个都会变成第二个权威。

`agent.task.completed` 是唯一出站事件，且它只是"这个任务结束了"的通知，不是"某业务状态变了"。

---

## 5. 数据模型

数据库：独立 PostgreSQL，schema `agent_runtime`。所有表以 `tenant_id` 起键。

### 5.1 角色定义

```sql
CREATE TABLE agent_runtime.agent_definition (
  tenant_id        text NOT NULL,
  definition_id    text NOT NULL,          -- def_<role>
  role             text NOT NULL,          -- 13 个注册角色之一
  current_revision integer NOT NULL DEFAULT 0,
  state            text NOT NULL CHECK (state IN ('ACTIVE','RETIRED')),
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, definition_id),
  UNIQUE (tenant_id, role)
);

CREATE TABLE agent_runtime.agent_definition_revision (
  tenant_id             text NOT NULL,
  definition_id         text NOT NULL,
  revision              integer NOT NULL,
  role                  text NOT NULL,
  system_template_ref   jsonb NOT NULL,    -- S20 TemplateRevision ref
  output_schema_ref     jsonb NOT NULL,
  required_capabilities text[] NOT NULL DEFAULT '{}',
  limits_json           jsonb NOT NULL,
  required_perspective  text NOT NULL CHECK (required_perspective IN ('omniscient','character','audience')),
  on_unresolved         text NOT NULL CHECK (on_unresolved IN ('fail','review')),
  allow_schema_override boolean NOT NULL DEFAULT false,
  eval_set_ref          jsonb NOT NULL,
  content_hash          char(64) NOT NULL,
  published_by          text NOT NULL,
  published_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, definition_id, revision)
);

CREATE TABLE agent_runtime.agent_definition_tool (
  tenant_id       text NOT NULL,
  definition_id   text NOT NULL,
  revision        integer NOT NULL,
  tool_name       text NOT NULL,
  permission_key  text NOT NULL,
  scope_kind      text NOT NULL CHECK (scope_kind IN ('tenant','org_unit','project','series','episode')),
  required_scope  text NOT NULL CHECK (required_scope IN ('read','plan','execute','admin')),
  egress          text NOT NULL CHECK (egress IN ('internal','external_allowlisted')),
  max_calls       integer NOT NULL CHECK (max_calls > 0),
  PRIMARY KEY (tenant_id, definition_id, revision, tool_name)
);
```

`content_hash` 覆盖 `system_template_ref`、`output_schema_ref`、`required_capabilities`、`limits_json`、`required_perspective`、`on_unresolved`、`allow_schema_override`、`eval_set_ref` 与整个工具集；不含 `published_by`/`published_at`（元数据变了不该改内容指纹）。

定义 revision **不可变**：触发器拒绝 `UPDATE`/`DELETE`（与 [S06 §5.5](S06-storyboard.md) 的镜头内容表同一条纪律）。要改就发新 revision。

### 5.2 任务

```sql
CREATE TABLE agent_runtime.agent_task (
  tenant_id               text NOT NULL,
  task_id                 text NOT NULL,
  role                    text NOT NULL,
  definition_id           text NOT NULL,
  definition_revision     integer NOT NULL,
  definition_content_hash char(64) NOT NULL,   -- 创建时 pin 死
  root_task_id            text NOT NULL,
  correction_of_task_id   text,
  correction_round        integer NOT NULL DEFAULT 0 CHECK (correction_round BETWEEN 0 AND 2),
  state                   text NOT NULL CHECK (state IN
                          ('ACCEPTED','RUNNING','CANCELLING','SUCCEEDED','FAILED','NEEDS_REVIEW','CANCELLED')),
  stop_reason             text,
  goal                    text NOT NULL,
  execution_context       jsonb NOT NULL,      -- 发起方派生，S19 只读
  input_refs              jsonb NOT NULL,
  output_schema_ref       jsonb NOT NULL,
  budget_scope_id         text NOT NULL,
  limits_json             jsonb NOT NULL,
  result_ref              jsonb,
  steps_used              integer NOT NULL DEFAULT 0,
  tokens_in               integer NOT NULL DEFAULT 0,
  tokens_out              integer NOT NULL DEFAULT 0,
  cost_micro              bigint  NOT NULL DEFAULT 0,
  cost_state              text NOT NULL DEFAULT 'PROVISIONAL' CHECK (cost_state IN ('PROVISIONAL','FINAL')),
  publish_sequence        bigint  NOT NULL DEFAULT 0,
  lease_owner             text,
  lease_expires_at        timestamptz,
  created_at              timestamptz NOT NULL DEFAULT now(),
  finished_at             timestamptz,
  PRIMARY KEY (tenant_id, task_id),
  CHECK (task_id = root_task_id OR correction_of_task_id IS NOT NULL)
);
CREATE INDEX task_queue_idx ON agent_runtime.agent_task (tenant_id, role, state, created_at)
  WHERE state = 'ACCEPTED';
CREATE UNIQUE INDEX task_result_once ON agent_runtime.agent_task (tenant_id, task_id)
  WHERE state = 'SUCCEEDED';
```

`task_result_once` 是"一个任务只能有一个成功结果"的数据库级保证。应用层 `WHERE state <> 'SUCCEEDED'` 检查会被并发绕过，索引不会。

### 5.3 步骤

```sql
CREATE TABLE agent_runtime.agent_step (
  tenant_id          text NOT NULL,
  task_id            text NOT NULL,
  step_index         integer NOT NULL,      -- 从 0 连续
  kind               text NOT NULL CHECK (kind IN ('MODEL','TOOL','VALIDATE','RECOVER')),
  started_at         timestamptz NOT NULL,
  finished_at        timestamptz,
  outcome            text NOT NULL CHECK (outcome IN ('OK','DENIED','ERROR','TIMEOUT')),
  generation_job_ref text,                  -- S26 provider_job_id，MODEL 步骤必填
  model_snapshot     text,
  tokens_in          integer NOT NULL DEFAULT 0,
  tokens_out         integer NOT NULL DEFAULT 0,
  cost_micro         bigint  NOT NULL DEFAULT 0,
  error_code         text,
  PRIMARY KEY (tenant_id, task_id, step_index)
);
```

`step_index` 连续由写入路径保证（§6.1 里对任务行加锁后取 `max+1`），不靠应用层计数器。跳号检测放进每日对账（§8.4）。

### 5.4 工具调用审计

```sql
CREATE TABLE agent_runtime.tool_call_audit (
  tenant_id        text NOT NULL,
  task_id          text NOT NULL,
  step_index       integer NOT NULL,
  tool_name        text NOT NULL,
  arguments_digest char(64) NOT NULL,        -- 参数只存摘要
  arguments_ref    jsonb,                    -- 含敏感参数时指向受限存储
  verdict          text NOT NULL CHECK (verdict IN ('ALLOW','DENY')),
  deny_reason      text,
  permission_key   text,
  scope_kind       text,
  scope_id         text,
  revocation_epoch bigint,                   -- 判定时 S01 返回的撤销水位
  egress_target    text,
  result_digest    char(64),
  result_ref       jsonb,
  duration_ms      integer,
  called_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, task_id, step_index, tool_name)
);
```

审计表**不存工具参数原文**。参数里可能有剧本片段、记忆内容甚至用户输入。存 `arguments_digest` + 受限存储引用，让审计可验证（能证明"调用了哪个工具、参数是哪一份"）而不把内容复制进审计库（docs/14 最小化）。

`revocation_epoch` 存判定时的值。事后要证明"当时确实有权限"，拿 `access.grant.changed` 的 grant revision 与这个 epoch 比对即可——E39"操作链可证明"的落点。

### 5.5 检查点

```sql
CREATE TABLE agent_runtime.task_checkpoint (
  tenant_id                  text NOT NULL,
  task_id                    text NOT NULL,
  checkpoint_seq             integer NOT NULL,
  step_index                 integer NOT NULL,
  messages_ref               jsonb NOT NULL,
  state_digest               char(64) NOT NULL,
  pending_generation_job_ref text,
  remaining_json             jsonb NOT NULL,
  created_at                 timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, task_id, checkpoint_seq)
);
```

### 5.6 结果

```sql
CREATE TABLE agent_runtime.task_result (
  tenant_id         text NOT NULL,
  result_id         text NOT NULL,
  task_id           text NOT NULL,
  revision          integer NOT NULL DEFAULT 1,
  proposal          jsonb NOT NULL,
  source_refs       jsonb NOT NULL,
  assumptions       jsonb NOT NULL,
  unresolved_issues jsonb NOT NULL,
  confidence        numeric(4,3),
  suggested_actions jsonb NOT NULL,
  usage_trace       jsonb NOT NULL,
  content_hash      char(64) NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, result_id, revision),
  UNIQUE (tenant_id, task_id)
);
```

### 5.7 幂等与 Outbox

```sql
CREATE TABLE agent_runtime.idempotency_record (
  tenant_id     text NOT NULL,
  command_id    text NOT NULL,
  request_hash  char(64) NOT NULL,
  command_type  text NOT NULL,
  state         text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code integer,
  response_body jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE agent_runtime.outbox (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,        -- 本服务固定装 task_id
  aggregate_sequence bigint NOT NULL,      -- agent_task.publish_sequence
  event_type         text NOT NULL,        -- 目前只有 'agent.task.completed'
  partition_key      text NOT NULL,        -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,       -- correlation_id / causation_id / traceparent
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx
  ON agent_runtime.outbox (id) WHERE published_at IS NULL;
```

`aggregate_id` 在本服务**固定是 `task_id`**——S19 只发布一种事件，没有"不同事件类型聚合不同"的情形。将来若新增以 `definition_id` 为聚合的事件，必须加 `aggregate_kind` 列而不是继续复用这一行（§11 C6）。

**订阅侧：S19 目前不消费任何事件，因此不建 inbox。** 将来若要消费 `manifest.committed` 做自动取消，必须连同 inbox 与去重逻辑一起加；§3.9 已说明为什么现在不做。

### 5.8 不建的表

- **不建 `agent_message`（完整对话历史）。** messages 落对象存储，表里只有 `messages_ref`。一次 20 步任务的 messages 可能几 MB，放进 PG 会让 WAL 与备份成本失控，而它只在恢复时读一次。
- **不建 `agent_memory` / `agent_state`。** 记忆归 S16。Agent 没有跨任务状态——一个有持久记忆的 Agent 就是新的权威。
- **不建 `token_ledger` / `cost_account`。** 金额归 S23；`cost_micro` 只是本任务熔断数（§1.6）。
- **不建 `tool_result_cache`。** 工具结果可能含租户内容，缓存会带来"一个任务的工具结果被另一个任务读到"的跨租户风险（E28）。
