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

请求体 `{"command_id": "...", "reason": "user_cancelled"}`。语义见 §6.4：**不假退款**——已经提交给 S26 的调用继续跑完并结算，S19 只是不再发起新步骤。

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

**S19 既不产生也不推进这两个值。** S19 的"重试"是任务内步骤重试（同一个 `task_id`）；它不改变 `execution_fence`（S08/S21 的），也不改变 `node_epoch`（被改节点的）。

唯一接触点是**读取**：`agent_task.execution_context` 带着创建时刻的 `execution_fence` 与 `node_epoch`，用于给结果打标。发起方落库前自己比对当前 fence/epoch（[S08 §2.1](S08-revision-snapshot.md) 的 `STALE_DECISION`）。S19 不替它判，也不缓存这个判断。

一个具体的错误实现：S19 收到 `manifest.committed` 就主动取消任务。**不要这样做**——取消的权威在 S21；S19 一取消就分不清"用户取消"与"epoch 推进"，`stop_reason` 统计也失真。

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

`stop_reason` 枚举：`GOAL_REACHED`、`STRUCTURE_INVALID`、`STEP_LIMIT`、`TOKEN_LIMIT`、`TIME_LIMIT`、`COST_LIMIT`、`POLICY_DENIED`、`GENERATION_FAILED`、`GENERATION_UNKNOWN`、`TOOL_DENIED_REPEATEDLY`、`USER_CANCELLED`、`UNRESOLVED_CONFLICTS`。`cost_state` 取 `PROVISIONAL`（仍有在途结算）或 `FINAL`（全部到账）；**只有 `FINAL` 能被 S44 当成本事实引用**（§1.6）。

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

---

## 6. 幂等与并发

### 6.1 创建事务模板

```python
with db.transaction() as tx:                       # REPEATABLE READ
    rec = tx.idempotency_begin(tenant_id, command_id, request_hash, 'CREATE_TASK')
    if rec.replayed:
        return rec.response_code, rec.response_body

    # 1. 本地校验（不产生外部调用）
    defn = load_published_definition(tx, role, definition_revision)   # 422 if not published
    assert limits_subset(body.limits, defn.limits_json)               # 422 LIMITS_EXCEED_DEFINITION

    # 2. 纠正链推导（服务端算，不信客户端）
    if body.correction_of_task_id:
        parent = load_task(tx, body.correction_of_task_id)            # 404 跨租户
        assert parent.role == role and parent.definition_revision == definition_revision
        round_ = parent.correction_round + 1
        if round_ > 2: raise ApiError(422, 'CORRECTION_LIMIT_REACHED')
        root = parent.root_task_id
    else:
        round_, root = 0, new_ulid('agt_')

    # 3. 外部校验（在事务内但只读，失败则整体 ROLLBACK）
    assert s01_permission_checks(...) == 'allow'                      # 403
    assert s18_pack_matches(defn.required_perspective, ...)            # 422 PERSPECTIVE_MISMATCH
    assert s18_pack_watermark_ok(...)                                  # 422 CONTEXT_PACK_STALE

    # 4. 写任务（此时不写 outbox——事件在终态才发）
    task_id = new_ulid('agt_')
    insert_agent_task(tx, task_id, ...)
    tx.idempotency_finish(tenant_id, command_id, 202, response_body(task_id))
    return 202, {"task_id": task_id, "state": "ACCEPTED", "replayed": False}
```

第 3 步的外部只读调用在事务里做，意味着**事务持有时间包含两次网络往返**。这是刻意的取舍：如果把外部校验挪到事务外，就会出现"校验通过 → 事务开始 → 期间权限被撤销"的窗口，而 E27 要求撤权及时生效。代价是 503 时事务回滚，客户端重试，可接受。

第 4 步不写 outbox，是 S19 与其它服务最不一样的地方：**任务创建不是业务事件**。只有进入终态才发 `agent.task.completed`。

### 6.2 步骤写入

```python
with db.transaction() as tx:
    task = tx.execute("""
        SELECT steps_used, tokens_in, tokens_out, cost_micro
          FROM agent_task WHERE tenant_id=%s AND task_id=%s
            AND state='RUNNING' FOR UPDATE
    """, (tenant_id, task_id)).one()
    if task is None:
        raise Abort("TASK_NOT_RUNNING")          # 被取消/被回收，本步不写

    step_index = tx.execute("""
        SELECT coalesce(max(step_index)+1, 0) FROM agent_step
         WHERE tenant_id=%s AND task_id=%s
    """, (tenant_id, task_id)).scalar()

    insert_agent_step(tx, step_index, ...)
    update_agent_task_counters(tx, ...)           # 熔断计数在同一事务内推进
```

`FOR UPDATE` 锁任务行，让"步数递增"与"状态检查"原子。代价是单任务串行——这正确，因为一个任务本来就只有一个 worker。多 worker 抢同一任务只在 lease 失效时发生，那时 `state<>'RUNNING'` 会让第二方直接放弃。

### 6.3 并发

| 场景 | 行为 |
|---|---|
| 两个 worker 同时领同一 `ACCEPTED` 任务 | `UPDATE ... WHERE state='ACCEPTED' AND (lease_expires_at IS NULL OR lease_expires_at < now()) RETURNING`，只有一个拿到 |
| 取消与完成并发 | 终态先写入者生效；另一方的 `UPDATE ... WHERE state='RUNNING'` 影响 0 行 → 返回 409 `TASK_NOT_CANCELLABLE` 或丢弃结果 |
| 同一纠正被并发发起 | `command_id` 不同则两个都建；`UNIQUE (tenant_id, role, correction_of_task_id)` 不存在，允许多次尝试是错的 → 见下 |
| 结算事件延迟 | `cost_micro` 只增不减，`cost_state` 由"在途结算数 == 0"派生 |

第三行要展开：**同一父任务的同一轮纠正只允许一次**。加唯一索引：

```sql
CREATE UNIQUE INDEX correction_once
  ON agent_runtime.agent_task (tenant_id, correction_of_task_id)
  WHERE correction_of_task_id IS NOT NULL;
```

没有这条索引，S05 可以在一次网络抖动时对同一份违规列表发起两个 round-1 纠正，付两笔钱，然后只能用一个。这与"最多 2 次纠正"是同一件事的两半：**次数上限按轮，不按请求**。

### 6.4 取消语义

`:cancel` 是**软取消**：

1. `RUNNING → CANCELLING`，写 `stop_reason='USER_CANCELLED'` 的草案（终态时才正式写）；
2. worker 在下一个步骤边界发现 `CANCELLING` → 不再发起新步骤；
3. 在途的 S26 调用**继续跑完并结算**（不假退款，与 [S26 §1](S26-generation-jobs.md) 的"取消不假退款"一致）；
4. 全部在途调用终态后 → `CANCELLED`，`cost_state` 转 `FINAL`，发 `agent.task.completed`（`state=CANCELLED`）。

`ACCEPTED` 态的任务可以立即 `CANCELLED`（没有在途调用）。`CANCELLING` 再收到 `:cancel` → 200 幂等（同 `command_id` 回放）。

**取消不发"退款事件"**，因为 S19 从不持有钱。退款是 S23 的事，由 S26 的结算驱动。

### 6.5 事件量与顺序

每个任务最多 1 条 `agent.task.completed`（终态唯一）。`aggregate_sequence` 从 1 开始每次终态 +1——对单任务而言就是恒等于 1，但保留这个列是为了让 relay 与下游用统一逻辑（[S08 §6.4](S08-revision-snapshot.md)）而不是特例。

不会有跳号：S19 的 outbox 只在一个事务里被写一次，且该事务同时把 `state` 推到终态。若 relay 崩溃，`published_at IS NULL` 的行会被补发；若发布成功但 `UPDATE published_at` 失败，会重发一次，下游按 `event_id` 去重。

---

## 7. 失败分支

### 7.1 准入阶段

| 情况 | 返回 | 是否写入 | 备注 |
|---|---|---|---|
| `role` 未注册 | 422 `UNKNOWN_ROLE` | 否 | |
| 定义 revision 不是 `PUBLISHED` | 422 `DEFINITION_NOT_PUBLISHED` | 否 | 含"已 RETIRED" |
| `limits` 放宽 | 422 `LIMITS_EXCEED_DEFINITION` | 否 | 不放宽再降级处理 |
| 输出 schema 不符 | 422 `SCHEMA_NOT_ALLOWED` | 否 | |
| 纠正链已满 | 422 `CORRECTION_LIMIT_REACHED` | 否 | 交人工（S40） |
| pack 的 perspective 不符 | 422 `PERSPECTIVE_MISMATCH` | 否 | |
| pack watermark 落后 | 422 `CONTEXT_PACK_STALE` | 否 | 可重试（重取 pack） |
| S01 判定 deny | 403 `PERMISSION_DENIED` | 否 | 不写幂等 DONE（可重试） |
| S01 不可用 | 503 `AUTHORIZATION_UNAVAILABLE` | 否 | 不降级为放行 |
| S18 不可用 | 503 `CONTEXT_UNAVAILABLE` | 否 | 不降级为"无上下文" |

最后两行是同一类：**依赖不可用时不许"先跑起来"**。一个没有 pack 的 Agent 会自己编前情，产出的提案看起来完整但全是虚构——这比失败更坏。

### 7.2 执行阶段

| 情况 | 结果 | 费用 | 后续 |
|---|---|---|---|
| S25 返回 `INFEASIBLE` | `FAILED(GENERATION_FAILED)` | 0 | 调用方换对照组的 `limits` 或缩 `goal` |
| S26 返回 409（预算/配额/额度） | `FAILED(POLICY_DENIED)` | 0 | 不自动重试 |
| S26 返回 `SUBMISSION_UNKNOWN` | `FAILED(GENERATION_UNKNOWN)` | 保留额度 | **不重提**，等对账（S26 §7.2） |
| S26 超时未到终态 | `FAILED(GENERATION_FAILED)` | 视状态 | checkpoint 保留，人工可查 |
| 模型输出结构非法 | `FAILED(STRUCTURE_INVALID)` | 已花 | 调用方决定是否发纠正（S05/S06） |
| 工具被拒绝 3 次连续 | `FAILED(TOOL_DENIED_REPEATEDLY)` | 已花 | 说明定义白名单与需求不匹配 |
| 闸命中（步数/token/时间/费用） | `FAILED(*_LIMIT)` | 已花 | 终态，不重试同 body |
| 结果含禁止字段 | `FAILED(STRUCTURE_INVALID)` | 已花 | 422 `OUTPUT_CONTAINS_FORBIDDEN_FIELD` |
| 有硬冲突且 `on_unresolved='review'` | `NEEDS_REVIEW` | 已花 | S40 建 ReviewTask |

### 7.3 恢复阶段

| 情况 | 行为 |
|---|---|
| worker 崩溃，lease 到期 | 另一 worker 接管，从最后 checkpoint 续 |
| checkpoint 指向的在途 S26 任务已成功 | 直接用结果继续，**不重提** |
| checkpoint 指向的在途 S26 任务未知 | `FAILED(GENERATION_UNKNOWN)`，不盲重提 |
| checkpoint 的 `messages_ref` 不可读 | `FAILED(STRUCTURE_INVALID)`，人工可查 state_digest |
| checkpoint 落后于 `agent_step` | 以 checkpoint 为准重放未提交的步骤；已提交的步骤不重做 |
| lease 被强夺（脑裂） | 旧 worker 的写全部被 `WHERE state='RUNNING' AND lease_owner=$me` 拒绝 |

最后一行是 E34（Region 灾备/旧执行器）的落点：**旧执行器的写入必须被栅栏挡住**，而不是靠"它应该已经停了"。

### 7.4 存储与事件

| 情况 | 行为 |
|---|---|
| 写 `task_result` 时 `task_result_once` 冲突 | 说明已有成功结果 → 200 返回既有结果，不报错 |
| 写结果成功、写 outbox 失败（同事务） | 事务回滚，任务回到 `RUNNING`，下个边界重试 |
| 事务提交、relay 前崩溃 | 重启补发 |
| 发布成功、`published_at` 更新失败 | 重发一次，下游按 `event_id` 去重 |
| `agent_step` 主键冲突（并发写同一步） | 唯一约束拒绝，worker 重读 `step_index` 重试一次；仍冲突则 `FAILED` |

### 7.5 依赖不可用

| 依赖 | 契约不可用时的行为 | 能否降级 |
|---|---|---|
| S26 | 任务 `FAILED(GENERATION_FAILED)`，未花费用 | 否（不许绕开） |
| S01 | 503，不创建任务 | 否 |
| S02 | 503，不创建任务 | 否 |
| S18 | 503，不创建任务 | 否 |
| S20（模板读取） | 503，不创建任务 | 否 |
| S25 | S26 侧报错 → `FAILED(GENERATION_FAILED)` | 否 |
| S45（审计投递） | 异步，不阻塞；审计本地先落 `tool_call_audit` | 是（审计补投） |

唯一可降级的是审计外发，因为它不参与决策。其余六项任何一项消失，Agent 就不该跑。

### 7.6 回滚与独立升级（E01）

- 迁移只加列、加表、加索引（`CREATE INDEX CONCURRENTLY`），不改已有列语义。
- 旧版本 worker 与新版并存时，新版只写新列；旧版读不到新列不影响（全部有默认值）。
- `task_result.proposal` 的 schema 兼容由 `output_schema_ref.revision` 决定，不由部署版本决定——**已经产出的结果永远按它自己的 schema revision 解释**。
- 回滚一次部署不会中断在途任务：worker 从 checkpoint 续跑，checkpoint 里 `remaining` 是值不是增量。

---

## 8. 可观测

### 8.1 指标

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `s19_task_total` | counter | `role`, `state`, `stop_reason` | 角色成功率与终止原因分布 |
| `s19_task_duration_seconds` | histogram | `role` | P50/P95/P99 |
| `s19_step_total` | counter | `role`, `kind`, `outcome` | 工具拒绝率 |
| `s19_tool_denial_total` | counter | `role`, `tool_name`, `reason` | 白名单配置漂移 |
| `s19_correction_round_total` | counter | `role`, `round` | 纠正是否在增多（质量回归信号） |
| `s19_cost_micro` | histogram | `role` | 单任务成本分布 |
| `s19_cost_state_provisional_age_seconds` | gauge | `role` | `PROVISIONAL` 滞留时长（结算积压） |
| `s19_lease_reap_total` | counter | `role` | 崩溃频率 |
| `s19_checkpoint_age_seconds` | gauge | `role` | 长任务健康 |
| `s19_permission_check_latency_seconds` | histogram | — | S01 P95 是否拖慢步骤 |

### 8.2 告警

| 条件 | 级别 | 含义 |
|---|---|---|
| `s19_task_total{state="FAILED",stop_reason="STRUCTURE_INVALID"}` 占比 > 15%/1h | P1 | 定义或模板退化，不是模型随机性 |
| `s19_correction_round_total{round="2"}` > 0.1 × 任务数 | P2 | 纠正常态化，说明提示或 schema 有问题 |
| `s19_tool_denial_total{reason="NO_GRANT"}` > 0/5m | P2 | 白名单配了一个 Agent 实际没有的权限 |
| `s19_cost_state_provisional_age_seconds` > 1800 | P1 | S26 结算事件积压，成本不可信 |
| `s19_lease_reap_total` > 10/h | P2 | worker 不稳 |
| `outbox` 未发布最老条数 > 5 分钟 | P2 | relay 或 Kafka 异常 |
| `s19_step_total{outcome="DENIED"}` 单任务 > 5 | P3 | 任务在反复撞墙，浪费步数与钱 |

### 8.3 审计

- **每一步都进 trace**：`s19.task`（root span）→ `s19.step` → `s26.generation-job` / `s19.tool`。`traceparent` 从 `execution_context.trace_id` 起，贯穿到 S26。
- **工具调用落 `tool_call_audit`**，S45 通过 `GET /agent-tasks/{id}/steps` 或直接 CDC 拉取。
- **不记录**：完整 prompt、完整 messages、工具参数原文、模型原始响应。这些如果要留，走受限存储 + `data_ref`，并有独立保留期（§11 C5）。
- 审计行与任务同生命周期：任务归档（90 天）后审计默认保留 1 年（E39 要求操作链可证明），冷存。

### 8.4 每日对账

- `agent_task.steps_used == count(agent_step)`；
- `agent_step.step_index` 从 0 连续无空洞；
- `agent_task.cost_micro == sum(agent_step.cost_micro)`（允许 1 个结算周期的滞后，超过即告警）；
- `state='SUCCEEDED'` 的任务必须有 `result_ref` 且 `task_result` 存在；
- `state='ACCEPTED'` 超过 10 分钟的任务（队列积压或队列配置错误）；
- `outbox` 未发布条数与最老滞留时长。

---

## 9. 测试清单

标注 `[E##]` 对应 [docs/16 §5](../docs/16-migration-acceptance.md)。全部用 mock 依赖（mock S01/S02/S18/S20/S25/S26），不接真实模型。

### 9.1 幂等与并发 `[E12]`

| # | 用例 | 期望 |
|---|---|---|
| T19-01 `[E12]` | 同 `command_id` + 同 body 两次 | 第二次返回同一 `task_id`，`replayed:true`，只 1 个任务 |
| T19-02 `[E12]` | 同 `command_id` + 改 `goal` | 409 `IDEMPOTENCY_KEY_REUSED`，无写入 |
| T19-03 | 不同 `command_id` + 同 body 两次 | 建两个任务（**允许**：这是两次真实付费调用） |
| T19-04 | 并发 20 次同 `command_id` | 1 个任务，其余全部回放或 409 |
| T19-05 | 两个 worker 抢同一 `ACCEPTED` | 只有一个取得 lease |
| T19-06 | 同一父任务并发发两个 round-1 纠正 | 一个成功，一个 409/唯一冲突（`correction_once`） |

### 9.2 有界纠正（本文核心，被 S05/S06 引用）

| # | 用例 | 期望 |
|---|---|---|
| T19-07 | round 0 结构非法 → 发起 round 1 | 新 `task_id`，`correction_round=1`，`root_task_id` 指向 round 0 |
| T19-08 | round 1 仍非法 → round 2 | 允许 |
| T19-09 | round 2 仍非法 → 发起 round 3 | 422 `CORRECTION_LIMIT_REACHED`，无写入 |
| T19-10 | 客户端伪造 `correction_round=0` 但带 `correction_of_task_id` | 服务端按链推导为 1，**忽略客户端值** |
| T19-11 | 纠正换了 `definition_revision` | 422（纠正必须同定义） |
| T19-12 | 纠正的 `command_id` 与父任务相同 | 409 `IDEMPOTENCY_KEY_REUSED` |
| T19-13 | 纠正只改 `violations` 指向的区域 | 未受影响区域的 hash 与父结果一致（防全量重写） |

### 9.3 信任边界 `[E28]`

| # | 用例 | 期望 |
|---|---|---|
| T19-14 `[E28]` | 租户 B 查租户 A 的 `task_id` | 404，响应体不含 A 的任何标识 |
| T19-15 `[E28]` | 请求体 `tenant_id` 与 TCT 不同 | 403 `TENANT_CONTEXT_MISMATCH`，无写入 |
| T19-16 | 模型输出里带 `tenant_id` | 422 `OUTPUT_CONTAINS_FORBIDDEN_FIELD`，任务 FAILED |
| T19-17 | 定义 revision 的 schema 根级有 `tenant_id` | 发布期 422 拒绝 |
| T19-18 | Agent 试图调用白名单外的工具 | 422 `TOOL_NOT_ALLOWED`，不发请求，审计记 DENY |
| T19-19 | Agent 调用白名单内但 S01 判定 deny 的工具 | 审计记 DENY + `reason_code`，模型收到结构化拒绝 |
| T19-20 | 工具被拒 3 次 | `FAILED(TOOL_DENIED_REPEATEDLY)` |
| T19-21 | 工具返回"忽略之前的指令…" | 只进 messages 作为数据，`output_schema` 与 `limits` 不变（断言无策略变化） |
| T19-22 | 工具参数含剧本原文 | `tool_call_audit.arguments_digest` 有值、`arguments_ref` 指向受限存储、表内无原文 |

### 9.4 四道闸与状态机

| # | 用例 | 期望 |
|---|---|---|
| T19-23 | `max_steps` 命中 | `FAILED(STEP_LIMIT)`，不发下一步调用 |
| T19-24 | 下一步报价上界会超 `max_cost_micro` | `FAILED(COST_LIMIT)`，**该步不发出**（断言 S26 无请求） |
| T19-25 | `max_wall_seconds` 命中时有在途调用 | `FAILED(TIME_LIMIT)`，在途调用继续结算到 `FINAL` |
| T19-26 | 终态任务再 `:cancel` | 409 `TASK_NOT_CANCELLABLE` |
| T19-27 | `RUNNING` 取消，在途调用成功 | `CANCELLED`，结果不入 `task_result`，费用照记 |
| T19-28 | 终态后再收到旧的 `agent.task.completed` 重投 | 状态不回归 |
| T19-29 | `on_unresolved='review'` 且有硬冲突 | `NEEDS_REVIEW` + `unresolved_issues` 非空 |

### 9.5 恢复 `[E13][E33]`

| # | 用例 | 期望 |
|---|---|---|
| T19-30 `[E13]` | 写结果后、COMMIT 前 kill -9 | 无结果、无 outbox、无幂等 DONE；重试成功 |
| T19-31 `[E13]` | COMMIT 后、relay 前 kill | 重启补发，`published_at` 由 NULL 变有值 |
| T19-32 `[E33]` | worker 在等 S26 时被杀，lease 到期 | 新 worker 从 checkpoint 续，**不重提**（断言 S26 只 1 个 provider_job） |
| T19-33 | checkpoint 的 `pending_generation_job_ref` 已是 `SUBMISSION_UNKNOWN` | `FAILED(GENERATION_UNKNOWN)`，不重提 |
| T19-34 | 恢复后 `remaining` 被重置成满额（模拟 bug） | 断言测试失败——恢复必须用 checkpoint 的值 |
| T19-35 | lease 被强夺后旧 worker 继续写 | 写入被 `lease_owner` 条件拒绝 |
| T19-36 | `step_index` 并发写同一步 | 唯一冲突后重试成功，最终连续 |

### 9.6 事件 `[E32]`

| # | 用例 | 期望 |
|---|---|---|
| T19-37 `[E32]` | 同一事件投递 3 次 | 下游按 `event_id` 去重 |
| T19-38 `[E32]` | Kafka 重放 1 小时 | 不新建任务、不重提付费、`publish_sequence` 不变 |
| T19-39 | 事件 `data` 不含 `proposal` 正文 | 断言 `data` 只有引用与计数 |
| T19-40 | 事件信封按 `event-envelope.v2` 校验 | 全部必填字段齐备（含 `home_cell`、`traceparent`、`data_ref`） |

### 9.7 独立升级 `[E01]`

| # | 用例 | 期望 |
|---|---|---|
| T19-41 `[E01]` | 只 import 契约 SDK，无其他服务 ORM | 构建通过 |
| T19-42 | 新版加列、旧版 worker 在跑 | 旧版正常写；新版回滚后数据可读 |
| T19-43 | 旧任务按自己的 `output_schema_ref.revision` 解读 | 新 schema 发布不影响历史结果 |

---

## 10. 与上下游服务的接口约定

### 10.1 上游（谁调用 S19）

| 服务 | 用法 | 契约 |
|---|---|---|
| S21 编排 | 发起创意/审核类任务 | `POST /agent-tasks` |
| S04 | `writer` 角色产出剧本提案 | 同上 |
| S05 | `breakdown` 角色产出分场提案 + 纠正 | 同上，纠正按 §3.4 |
| S06 | `storyboard-director` 产出分镜提案 + 纠正 | 同上 |
| S18 | 请求摘要计算任务（`plan:compute`） | 同上 |
| S17 | `continuity-reviewer` | 同上 |
| S39/S41/S42 | `repair-planner` / `editor` / 协调 | 同上 |

**S19 是可选加速，不是必需依赖。** 这条在 [S05 §3.1](S05-breakdown.md) 与 [S06 §3.1](S06-storyboard.md) 各写了一次：人工路径必须能在 S19 完全不可用时跑通。S19 侧的义务是**明确返回 503 而不是挂住**，让调用方快速落到人工。

### 10.2 下游（S19 调用谁）

| 服务 | 用途 | 不可用时 |
|---|---|---|
| S01 | `POST /permission-checks`（每步工具调用） | 503，任务不创建/不推进 |
| S02 | `POST /policy-decisions:evaluate`（准入） | 503 |
| S18 | 读 ContextPack + 校验 perspective | 503 |
| S20 | 读 `TemplateRevision`（system 模板） | 503 |
| S25 | 路由（由 S26 内部调用，S19 不直接调） | — |
| S26 | `POST /generation-jobs`（唯一付费路径） | 任务 `FAILED(GENERATION_FAILED)` |
| S45 | 审计（异步） | 不阻塞 |

### 10.3 S19 → S26 的严格边界

S19 **不持有任何供应商凭证，也不在允许的出站白名单里**（[S26 §1.3](S26-generation-jobs.md) 第三条强制执行机制）。这意味着：

- S19 不能"顺手"直接调一个便宜的模型做格式修复；
- S19 的 `pending_generation_job_ref` 永远是 S26 的 `provider_job_id`，不是供应商 task id；
- S19 的 `cost_micro` 来自 S26 的结算事件，不是自己估的。

一个具体的实现陷阱：给 Agent 一个"本地小模型"做 JSON 修补，看起来省一次付费调用，实际上把一条付费路径搬到了预算体系外——S23 记不到，S44 算不到，客户账单上会出现无法解释的空白。

### 10.4 S19 → S05/S06 的边界

| 问题 | 谁答 |
|---|---|
| 输出结构是否合法 | **S19**（JSON Schema） |
| 输出语义是否正确（发言人、行覆盖、时长） | **S05/S06** |
| 纠正几次 | **S19** 强制上限，**S05/S06** 决定要不要发起 |
| 业务 ID 怎么铸 | **S05/S06**，S19 永不做 |
| 结果要不要落库 | **S05/S06** |

第五行是最容易被越过的：Agent 提案会被人在 UI 上"直接采纳"。采纳动作必须走 S05/S06 的正常写命令，由它们重新校验，不能因为"Agent 已经校验过结构"就跳过语义校验。S19 的 `content_hash` 只证明"这份字节没被改过"，不证明"这份字节是对的"。

### 10.5 S19 → S40 的边界

`NEEDS_REVIEW` 不是"等 S40 回复"。S19 的任务已经结束，`unresolved_issues` 是产物。S40 从 `agent.task.completed`（`state=NEEDS_REVIEW`）建 `ReviewTask`，人做出决定后由**发起方**（S05/S06/S21）去落库。

S19 **不阻塞在 S40 上**：如果 `NEEDS_REVIEW` 是等待态，S40 停机就会让 S19 的 worker 全部占满，进而拖垮其他 12 个角色。这也意味着 S19 不重试、不轮询 S40——它已经交付完了。

---

## 11. 契约缺口（需要先行修改，不要自行补）

按 [docs/16 §6](../docs/16-migration-acceptance.md) 与 [AGENTS.md](../AGENTS.md)「契约与文档冲突 → 停下报告」：

| # | 缺口 | 建议 |
|---|---|---|
| C1 | `agent.task.completed` 在 `contracts/event-topics.v2.json` 里的 `schema_ref` 指向通用的 `event-envelope.v2.schema.json`，**没有 payload schema**；本文 §4.3 的 `data` 字段集是提案 | 新增 `contracts/agents/event-payload/agent-task-completed.v2.schema.json`，`additionalProperties: false` |
| C2 | `pii_payload: references_only_by_default` 只是保底约定；`proposal` 正文在对象存储里，其保留期与地域策略没有契约 | 需要一份"受限存储引用"的通用规则（`data_ref` 的可读范围 + 保留期 + 地域），否则 E31 无法验证 |
| C3 | `service-catalog.v2.json` 只声明 `POST /agent-tasks`，没有 `:cancel`、`/agent-definitions`、`/agent-definitions/{id}/revisions`、`/agent-tasks/{id}/result`、`/agent-tasks/{id}/steps` | 补 catalog 的 command/query 摘要；完整形状落 `contracts/openapi/s19-agent-runtime.v2.yaml`（`openapi.v2.yaml` 里一个 S19 路径都没有） |
| C4 | 准入需要 `policy-decisions:evaluate` 的 `kind` 里**没有** `agent_task_admission`。现有枚举是 `dispatch_admission/acceptance/budget_increase/external_publish/quality_exception` | 在 [S02 §7.2](S02-governance-policy.md) 的 `kind` 枚举新增 `agent_task_admission`；若不新增，S19 只能用 `dispatch_admission`，会把"问一次策略"混成"问一次派单" |
| C5 | **Agent 预算的 `purpose` 值不存在**。docs/05 §、docs/13 §、[S01 §10.4](S01-tenant-access.md) 与 [S18 §5](S18-context-retrieval.md) 都写 `plan:compute`，但 S23 的 `POST /budget-authorizations` 里没有 `purpose` 字段，S26 用的是 `"purpose": "generation"`。Agent 的推理费用属于哪一类没有机器可读答案 | 裁决：要么 S23 加 `purpose` 枚举（含 `plan_compute`），要么统一改用 S26 的 `generation`。**在裁决前，S19 按 `generation` 报，并在 `budget_scope_id` 上区分**（本文 §2.4 的 `bsc_ep3_agent`）。这是临时约定，不是最终形态 |
| C6 | outbox 的 `aggregate_id` 语义依赖"本服务只发布一种事件"。若将来加 `definition_id` 为聚合的事件，现有 DDL 会静默混用两种聚合 | 在契约注释里写明：`agent_runtime.outbox.aggregate_id` 恒为 `task_id`；新增聚合类型必须加 `aggregate_kind` 列 |
| C7 | `contracts/mcp-tools.v2.design.json` 里的 12 个工具是设计期名字，**没有 `permission_key` 映射**，也没有"哪些角色允许哪些工具"的机器可读表。本文 §2.6 的定义期校验依赖 `TOOL_REGISTRY` 与 `permission_key` | 新增 `contracts/agents/tool-registry.v2.json`：`tool_name → permission_key + scope_kind + egress`。没有它，§3.2 的定义期校验无法实现，只能硬编码角色名 |
| C8 | `execution-context.v2.schema.json` 把 `actor.delegation_id` 列为 `required`，但 S19 内部恢复（worker 换代）没有代理关系 | 同 [S01 C4](S01-tenant-access.md)：把 `delegation_id` 改可选，或定义 `dlg_platform` 保留值。本文沿用 S01 的临时立场（`dlg_platform`），**未裁决** |
| C9 | 13 个 `agents/*/agent.yaml` 的 `limits.tokens/cost/steps` 全是 `unassigned`，`tools: []`，`evals: []` | 这些是"设计期占位"而非契约。在 S19 实现前必须填出至少 `writer`/`breakdown`/`storyboard-director` 三套，否则 §3.2 的"没有评测集不许发布"会让所有角色都发不出去 |
| C10 | `agents/*/agent.yaml` 的 `deployment.image_binding: unassigned`，但 docs/08 §1 要求"镜像版本绑定"是定义的一部分 | 需明确：镜像绑定属于 `AgentDefinitionRevision`（本文 §5.1 的 `limits_json` 旁）还是属于 Deployment。本文假定属于 Deployment，因此**不进 `content_hash`**；这与 docs/08 §1 的"每个角色有镜像版本绑定"在措辞上一致但不精确，需裁决 |

---

## 12. 未决

1. **`plan:compute` 预算用途的裁决**（§11 C5）。这决定 S19 是否要新增一条与 S26 不同的预算上报路径，影响面覆盖 S18/S21/S23/S26。
2. **工具注册表归属**（§11 C7）。它是平台级数据、跨域变化快（MCP 工具会新增）。放在 `contracts/` 意味着改它要发版；放在某个服务意味着那个服务成了工具权威。第二条路更灵活但违反"配置即数据"的一致性问题——**未定**。
3. **`NEEDS_REVIEW` 是否应是终态**（§3.6）。本文取终态。若 S40 的产品形态要求"人改完继续跑"而不是"人改完交发起方落库"，则需要一个新的 `RESUMED` 语义，那会引入"同一 `task_id` 两个执行段"的复杂度。**需要产品确认**。
4. **纠正的粒度**。本文规定"只改 `violations` 指向的区域"，靠对比 hash 校验。但模型很少能严格局部重写。是否应该改成"重新生成整份但保留未违规区域的字节"（可行性存疑），或"允许整份重写、把漂移报给调用方判"——**未定**，倾向后者并在 S05/S06 侧加漂移检测。
5. **审计保留期**。§8.3 写了"审计 1 年、任务 90 天"，但没有客户合同依据。E39 要求"可证明操作链"，没有期限。**需要与法务/合同对齐**。
6. **`agent.task.completed` 是否该按角色拆分**。现在是一个事件带 `role`。若 S44 的分账需要极高吞吐，按角色拆 topic 更好，但 `partition_key` 的聚合语义会变。**暂时不拆**（§6.5）。
7. **灰度与回退的影响面**（docs/08 §3 对模板有明确要求，对 Agent 定义没有）。一个 `AgentDefinitionRevision` 灰度后回退，在途任务用哪一版？本文答案是"创建时 pin 的那一版"（§3.8 不变量 7），意味着回退只影响新任务——**需确认这符合产品预期**。
