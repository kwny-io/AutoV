# S29 · 图像与关键帧生产 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。所有供应商仍为 `disabled`，`slo.verified: false`。

上游依据：[docs/09 §1](../docs/09-model-execution.md)（"S29–S34 负责生产意图与阶段，不能绕过 S26 直连模型"）、[docs/07 §1 步骤 3/5](../docs/07-production-workflows.md)、[docs/08 §3](../docs/08-agents-prompts.md)（导演 DSL 与 Prompt 编译、参考顺序）、[docs/11 §2](../docs/11-quality-repair.md)（静态关键帧局部错 → mask 图像编辑）、[docs/04 §3](../docs/04-data-consistency.md)（准入 Saga 七步）、[S35 §2.2.1](S35-technical-qc.md)（`identity` 动作族与 6 帧窗口）、[S39 §5](S39-repair-planning.md)（跨 Run 累计计数）、`contracts/generation-command.v2.schema.json`（`modality: image`）。

链上位置：`S10 解析资产绑定 → S29 组参考组合并产生生产意图 → S25 估价路由 → S26 准入 Saga → S27 执行 → S28 归档 → S29 登记候选并发布 ready`。

本文重点回答三个问题：**参考组合与 mask 怎么可追溯**（§2、§5）、**关键帧版本变化怎么只传播给相关视频而不越权作废别人**（§4.4）、**本服务在 `identity` 动作族里到底执行哪一格**（§1.4）。

---

## 0. 一句话边界

**S29 拥有"这一镜的关键帧长什么样、用的是哪几张参考图、哪一块被 mask 改过、这一镜当前用哪一组候选作为生产基线"。它不拥有视频、不拥有当前接受状态、不拥有钱。**

```
S10:   资产 ID → 版本 → 绑定解析（只读）
S29:   参考组合 + 图像意图 + KeyframeCandidate + 镜头图像检查点
S25:   可行路由与报价（只读）
S26:   attempt / ProviderJob / SUBMISSION_UNKNOWN（唯一权威）
S28:   图像 bytes 与 content_hash（唯一权威）
S08:   当前接受指针、manifest、node_epoch（唯一权威）
S30:   视频片段与其候选（S29 只提供 keyframe 引用）
```

判据：任何"这一镜的第 1 帧是哪张图"的问题，答案来自 S29 的镜头图像检查点 + S28 的 artifact。任何"这一集现在被接受了吗"的问题，答案来自 S08。任何"这次生成花了多少钱"的问题，答案来自 S23。

---

## 1. 责任与明确不负责

### 1.1 本服务是唯一写者

| 事实 | 说明 |
|---|---|
| `ImageJob` | 一次图像生产意图（generate 或 edit），跨若干次 S26 attempt |
| `KeyframeCandidate` | 一张已归档的候选图，及其参考组合、mask、seed、provider 溯源 |
| `ReferenceCombination` | 有序、内容寻址的参考集合 + `identity_constraints`，不可变 |
| `EditMask` | 空间 mask 的 artifact 引用、几何摘要、来源候选、编辑指令 |
| `ShotImageCheckpoint` | 每 `(shot_lineage_id, node_kind)` 的"当前生产基线候选集"指针 |
| `ImageIntentLedger` | 本服务的意图台账（谁在什么上下文下要过什么图），**不是费用台账** |

### 1.2 本服务明确不写

| 事实 | 权威 | S29 的正确做法 |
|---|---|---|
| 媒体 bytes、sha256、存储位置、URL TTL | **S28** | 只保存 `{service_id:"S28", id, revision, content_hash}` |
| 当前接受指针 / manifest / `node_epoch` / `execution_fence` | **S08** | 提交候选后发事件，由 S08 决定是否推进 |
| 金额、reservation、实际费用 | **S23** | 只传 `budget_scope_id`，不估算、不记账 |
| permit、在途槽 | **S22** | 不持有、不询问；S26 内部完成 |
| `GenerationAttempt` / `ProviderJob` / `SUBMISSION_UNKNOWN` | **S26** | 只持 `attempt_id` / `provider_job_id` 引用 |
| 能力事实、价格快照 | **S24** | 只消费 `RouteDecision` |
| 路由决策 | **S25** | 只传 `route_decision_id` |
| 资产内容（角色外观、场景、道具） | **S11–S14** | 只存 `artifact_id` + `sha256` + `role` |
| 权利是否仍然有效 | **S02 / S11–S15** | 只存 `rights_revision` 指针，派单前由 S26 实时复查 |
| 视频片段、剪辑、时间线 | **S30 / S41** | 只发"关键帧变了"的事实，不判定影响闭包 |
| 影响闭包 | **S09** | 不发"哪些视频作废"，只发"哪张图出现了新 revision" |
| QC 报告与缺陷 | **S35 / S36** | 不自造分数；只读引用 |
| 修复决策 | **S39** | 不自己决定"该不该重做"，只声明能力并执行被指派的那一格 |
| 人工裁决 | **S40** | 不写审核结果 |

### 1.3 本服务不直连供应商（强制条款）

**本服务不直连供应商。** 不持有任何供应商凭证，不出任何供应商域名，不配置任何供应商 SDK 的 API key，不接受"临时直连测试"。

S29 的每一次付费生成都必须走完整链路：

```
S29 组 GenerationCommand(modality=image)
  → POST /generation-jobs          (S26)
      → POST /budget-authorizations  (S23 reserve，按 quote_upper_bound_micro)
      → POST /permits:acquire        (S22，route_decision 定死 provider/channel/region)
      → 持久化 SubmissionIntent + Outbox
      → POST /dispatch-grants/{id}:claim  (S27 领取
             → S27 用 grant 里的 request_hash 与 fencing_token 付费提交)
  → S26 观测 → S28 归档 → production.image.ready 回 S29
```

具体的强制手段（不是约定）：

1. **凭证隔离**：供应商密钥只注入 `connectors/*` 的 Deployment（[docs/09 §2](../docs/09-model-execution.md)）。S29 的 ServiceAccount 不在供应商出站 `NetworkPolicy` 白名单里。
2. **dispatch grant**：S27 的 `require_dispatch_grant=true` 是编译期常量，无开关可关。没有 S26 签发的 grant，连接器本地直接拒绝。
3. **命令形状**：S29 发出的唯一付费入口是 `POST /generation-jobs`，请求体是 `{command_id, route_decision_id, generation_command}`（`contracts/openapi.v2.yaml`）。S29 没有任何其他出站调用能产生费用。

**S29 完全不参与准入 Saga 的内部步骤。** 它不知道 reservation id、不知道 permit id、不知道 `dispatch_id`。它只知道 `provider_job_id` 与 S26 报给它的状态。这样做的原因：一旦 S29 也持有 permit 引用，就会出现"两个服务都以为自己能释放它"的第二权威。

### 1.4 动作归属（对照 S35 动作族）

S35 §2.2.1 的 `identity` 族是有序候选链：

```
identity: keyframe_edit(仅 window_scope ≤ 6 帧) → temporal_edit → regenerate_video
兜底: regenerate_video        禁忌: 连续窗口禁止 keyframe_edit
```

本服务在其中的位置：

| 动作 | 执行者 | S29 的角色 |
|---|---|---|
| `keyframe_edit` | **S29（唯一执行者）** | 执行。接受 S39 下发的 `keyframe_edit` 动作：对指定候选图的指定空间区域做 mask 编辑，产出新 `KeyframeCandidate` |
| `temporal_edit` | **S41** | 不参与。S29 不提供时间线编辑能力 |
| `regenerate_video` | **S30（意图）→ S26（执行）** | 不参与。S29 只在 `keyframe_edit` 仍不达标时把"关键帧未变、请走视频重生成"作为事实交给 S39 判断 |
| `change_spec` | **S06 / S07** | 不参与 |
| `human_review` | **S40** | 不参与，只把证据链准备好 |

**S29 是 `keyframe_edit` 的执行者，且只执行这一格。** 它同时是 `identity` 缺陷在"单帧/短窗口"情形下的修复入口，但**不是判定者**——判定由 S39 做（[S39 §3](S39-repair-planning.md)：三交集 `action_family[type] ∩ registry[check_id].allowed_actions ∩ policy.allowed_actions`）。S29 收到的是一个具体的 `RepairAction`，不是一句"身份不对，你看着办"。

**6 帧窗口的硬约束落在 S29 的入参校验上**：

```
S29 接受 keyframe_edit 的充要条件（全部满足才进入 PREPARING）：
  repair_action.action == "keyframe_edit"
  repair_action.window_scope ∈ {"frame", "window"}
  repair_action.window.end_frame - repair_action.window.start_frame + 1 ≤ 6
  repair_action.target 解析到本服务某个 ShotImageCheckpoint 的当前候选
  对应 shot 的帧率已知（否则帧窗口无法换算到关键帧索引）
```

任何一条不满足 → `422 KEYFRAME_EDIT_OUT_OF_WINDOW`，**不创建 ImageJob，不消耗任何计数**。这条拒绝必须写进审计，因为"拒绝一次修复"本身是决策证据。

为什么是 6 帧：24fps 下 6 帧 = 0.25s，短于任一供应商的最短生成档位，因此"这个缺陷窗口内没有第二个关键帧的语义边界"。超过 6 帧说明缺陷跨越了镜头内的时间结构，静态图编辑无法只改一处而不引入跳变，必须升级到 `temporal_edit` 或 `regenerate_video`。这是 S35 的设定，S29 只执行。

**S29 不是 `regenerate_video` 的执行者，也不是它的意图提供者。** S30 拥有视频意图。S29 能做的只有：当 `keyframe_edit` 失败时发 `production.image.keyframe-refresh-failed`，由 S39 决定下一步。

---

## 2. 实体与关键区分

### 2.1 五层对象

```
ImageJob                    一次"我要给某镜出图"的意图（跨多次 attempt）
  ├─ ReferenceCombination   有序参考集合（不可变，content-addressed）
  ├─ EditMask               仅 edit 模式有（不可变）
  └─ KeyframeCandidate      一张产出图（不可变）
        └─ artifact_ref     → S28

ShotImageCheckpoint         每 (shot_lineage_id, node_kind) 的基线指针（可变，CAS）
```

**这五层不能合并。** 理由逐条：

| 合并会出的问题 | 后果 |
|---|---|
| 把 `ReferenceCombination` 塞进 `ImageJob` | 同一镜重试时换了参考图，旧 job 的溯源被覆盖，无法回答"这张图当时用了哪版角色" |
| 把 `KeyframeCandidate` 塞进 `ImageJob` | 一个 job 产生了 3 个候选（多 seed），只能存 1 个 |
| 把 `EditMask` 塞进候选 | 同一张源图可以被改两次（两个 mask），第二次会覆盖第一次的 mask 溯源 |
| 把 `ShotImageCheckpoint` 塞进候选 | 候选是不可变的，指针是可变的，混在一起等于让不可变对象可变 |

### 2.2 `ImageJob` 与 `GenerationAttempt` 的区别

这是最容易写错的一处。

| 概念 | 归属 | 一行代表什么 |
|---|---|---|
| `ImageJob` | **S29** | "这一镜需要一张符合这些约束的图"的业务意图。可以有 1..N 次 attempt |
| `GenerationAttempt` | **S26** | 一次业务意义上的生成尝试。`attempt_no` 与上限校验在 S26 |

一个 `ImageJob` 在以下情况会推进到新的 `attempt`：

- 上一次 attempt 是 `PROVIDER_FAILED`（确定失败）→ 新的 attempt（业务重试，`attempt_no += 1`）
- 上一次 attempt 是 `SUBMISSION_UNKNOWN` → **不推进 attempt，不新建 job**（见 §4.3）
- 用户改了 prompt 或参考组合 → **新的 `ImageJob`**（`input_fingerprint` 变了）

**"输入没变 → 同一个 ImageJob；输入变了 → 新 ImageJob"** 是本服务判定重试与新建的唯一规则。这条规则的具体实现是 `ImageJob.input_fingerprint`（§5.2），它由 `ReferenceCombination.content_hash` + `prompt_intent` 规范化后 + `mode` + `width/height` + `identity_constraints` 排序后计算。

### 2.3 `KeyframeCandidate` 与 S08 的接受指针

必须分清两件事：

| 问题 | 答案来源 |
|---|---|
| "这一镜现在**生产上**用哪一组候选？" | **S29 的 `ShotImageCheckpoint`** |
| "这一集现在**被接受**了吗？哪个 artifact？" | **S08 的 accepted pointer** |

`ShotImageCheckpoint` 是 S29 自己的**生产检查点**：它回答"下游 S30 该拿哪张图当首帧"。它**不**推进 S08 的接受指针。S29 在推进检查点后发 `production.image.ready`，S08 可以选择把候选纳入 manifest 并 CAS accepted pointer——**决定权在 S08**。

为什么 S29 需要自己的检查点：S30 在 S08 接受之前就要开始跑视频（否则串行等待会让整集变慢）。S30 需要一个"当前可用的最佳关键帧"，而这个指针的语义是"生产基线"，不是"最终接受"。把这两个概念混在一起会导致两种错误：S30 拿不到图（等 S08），或者 S29 越权推进接受指针（第二权威）。

**约束：`ShotImageCheckpoint` 只允许指向 `state ∈ {READY, ACCEPTED}` 的候选。** 指向其他状态的候选是最常见的 bug，用外键 + 检查约束挡。

### 2.4 `ReferenceCombination` —— 顺序是有语义的

`references` 数组在 `generation-command.v2` 里是有序的（`array`，不是 `set`）。[docs/08 §3](../docs/08-agents-prompts.md)：编译顺序里有"参考图顺序与 token 映射"。所以：

- `ReferenceCombination.content_hash` 对**有序**数组计算。`[角色A, 场景B]` 与 `[场景B, 角色A]` 是两个不同的组合。
- `role` 是枚举：`character | scene | style | first_frame | last_frame | video_reference | audio_reference | mask`。
- `identity_constraints` 是 `string[]`，形如 `char_linlan@3`（角色 + 资产 revision），**排序后参与 hash**（约束集合本身无序）。

**不可变性**：`ReferenceCombination` 一旦被某个 `ImageJob` 引用就不可改。要换参考图 = 新的 `ReferenceCombination` + 新的 `ImageJob`。

**可追溯性验收（service.yaml）**："多视图参考和 mask 可追溯"的落点是：给定任意 `KeyframeCandidate`，必须能沿 `candidate → image_job → reference_combination` 拿到**每一个**参考的 `{artifact_id, sha256, role, 顺序索引}`，以及**每一个** `identity_constraint` 的字符串。这条链不得有任何一步是"运行时从 S10 现查的"——S10 的绑定会变，历史证据不能变。

### 2.5 `EditMask` 的四要素

mask 不做成单独 artifact 的裸引用，而是四要素绑定：

```
EditMask := (source_candidate_id, artifact_ref(S28), geometry_digest, instruction)
```

| 字段 | 为什么必须有 |
|---|---|
| `source_candidate_id` | mask 只对某一张具体的图有意义。换了源图，mask 的像素坐标不再对应同一语义区域 |
| `artifact_ref` + `sha256` | mask 本身的字节要能取回并可校验，否则"这块被改过"不可证明 |
| `geometry_digest` | 掩码几何的规范化摘要（bbox + 面积占比 + 是否接触画面边界），用于快速比对"两次编辑改的是不是同一块" |
| `instruction` | 编辑指令原文。同一 mask 配不同指令是两次不同的编辑 |

**mask 不能跨候选复用。** `source_candidate_id` 是 `EditMask` 主键的一部分（或唯一约束的一部分），从结构上禁止"拿 A 图的 mask 去改 B 图"。

### 2.6 `node_kind` 区分 keyframe 与 reference_still

`ShotImageCheckpoint` 的键是 `(shot_lineage_id, node_kind)`，`node_kind ∈ {first_frame, last_frame, keyframe_mid, reference_still}`。

- `first_frame` / `last_frame`：S30 的首尾帧（`generation-command.video.input.references` 里的 `first_frame` / `last_frame` role）。
- `keyframe_mid`：镜内中间关键帧，用于拆镜或长镜头的分段。
- `reference_still`：不属于任何帧位置、只作为风格/身份参考的静帧。

**混用会导致 S30 拿错图。** 例如把 `reference_still` 当成 `first_frame` 派给视频模型，会得到首帧不匹配的片段，而所有静态校验都通过。

---

## 3. 状态机

### 3.1 `ImageJob` 状态

```
CREATED → PREPARING → SUBMITTED → WAITING_PROVIDER → INGESTING → READY
                  ↘            ↘                 ↘
                   REJECTED_LOCAL  FAILED         WAITING_UNKNOWN
                                                        ↓
                                                   RECONCILED_READY / RECONCILED_FAILED
任意非终态 → CANCELED
任意终态 → SUPERSEDED（被更新的 ImageJob 取代，仅当 input_fingerprint 不同的新 job 已 READY）
```

| 从 | 到 | 触发 | 前置条件 | 副作用 |
|---|---|---|---|---|
| — | `CREATED` | `POST /image-jobs` | 上下文校验 + 幂等键未命中 | 写 job + `reference_combination`（若无）+ outbox `image.job.created` |
| `CREATED` | `PREPARING` | 异步 prepare worker 领取 | 参考 artifact 在 S28 可读（只读校验，不下载） | 调 S25 `route-decisions`（或复用已传入的） |
| `PREPARING` | `REJECTED_LOCAL` | S25 `feasible=false` / 参考不可读 / `mode=edit` 但缺 mask | — | 写拒绝原因码；**不产生任何费用**；不重试 |
| `PREPARING` | `SUBMITTED` | `POST /generation-jobs` 返回 202 | `route_decision.quote_valid_to > now()` | 存 `provider_job_id`、`attempt_id`；outbox `image.job.submitted` |
| `SUBMITTED` | `WAITING_PROVIDER` | S26 `generation.job.state-changed` → `QUEUED`/`RUNNING` | — | — |
| `SUBMITTED` | `FAILED` | S26 → `PROVIDER_FAILED`（且无重试预算） | — | 存失败码；允许 S39 决定是否新 attempt |
| `SUBMITTED` | `WAITING_UNKNOWN` | S26 → `SUBMISSION_UNKNOWN` | — | **只标记，不做任何动作**（§4.3） |
| `WAITING_PROVIDER` | `INGESTING` | S26 → `PROVIDER_SUCCEEDED` 且 S26 报 `INGESTING` | — | 等 `artifact.ingested` |
| `WAITING_PROVIDER` | `FAILED` | S26 → `PROVIDER_FAILED`/`CANCELED` | — | — |
| `WAITING_PROVIDER` | `WAITING_UNKNOWN` | 理论不应发生（已有 provider_ref） | — | 记告警，按 bug 处理 |
| `INGESTING` | `READY` | `artifact.ingested` 且 hash 与 S26 观测一致 | artifact 归属本租户 | 写 `KeyframeCandidate`；outbox `image.candidate.ready` |
| `INGESTING` | `INGESTING` | S28 失败 / artifact 未到 | 重试预算未尽 | **只重归档，不重生成**（§4.5） |
| `INGESTING` | `FAILED` | 归档重试耗尽 / hash 不一致 | — | 缺口可见，不伪装成功 |
| `WAITING_UNKNOWN` | `RECONCILED_READY` | S26 → `ARCHIVED`（对账证明供应商已收单并产出） | — | 写候选；标记 `reconciled=true` |
| `WAITING_UNKNOWN` | `RECONCILED_FAILED` | S26 → 对账证明未收单 且 S26 已放弃 | — | 允许 S39 决定新 attempt（计数照记） |
| 任意非终态 | `CANCELED` | `POST /image-jobs/{id}:cancel` | — | 调 S26 cancel（可取消才调）；**不承诺退款** |

### 3.2 哪些是终态

`READY` / `REJECTED_LOCAL` / `FAILED` / `RECONCILED_FAILED` / `CANCELED` 是终态。
`SUPERSEDED` 是**逻辑终态**：状态字段不变，但检查点已不再指向它。
**`WAITING_UNKNOWN` 不是终态，也不是可取消状态。** 它不是"卡住了"，它是"等权威结论"。

### 3.3 状态转移的持久化

```sql
BEGIN;
  SELECT ... FROM image_job WHERE image_job_id = $1 FOR UPDATE;
  -- 应用层白名单校验（§3.1 表）
  UPDATE image_job
     SET state = $2, state_seq = state_seq + 1, updated_at = now()
   WHERE image_job_id = $1 AND state = $old_state;  -- 乐观并发
  INSERT INTO outbox (aggregate_id, aggregate_sequence, event_type, payload)
       VALUES ($1, <new state_seq>, 'image.job.state-changed', $payload);
COMMIT;
```

`state_seq` 单调递增，S30 / S09 / S47 用它拒绝旧事件把状态倒退（[docs/04 §4](../docs/04-data-consistency.md)："状态终态不得被旧事件倒退"）。

---

## 4. 关键流程

### 4.1 正常路径：generate

```
1. 上游（S21 / S39 / 人工）POST /image-jobs
     {context, shot_ref, node_kind, mode:"generate", prompt_intent,
      width, height, references[], identity_constraints[], route_policy_ref,
      budget_scope_id, deadline, command_id, request_hash}

2. S29 校验（顺序固定，失败即返回，不建 job）：
   a. context 完整性（execution-context.v2 必填项）
   b. 幂等键 tenant_id+command_id+request_hash
   c. 跨租户：所有 reference 的 artifact 必须属于 context.tenant_id（查 S28 只读）
   d. mode=generate 时 references 不得含 mask role
   e. references 每个元素的 sha256 与 S28 记录一致（防"同 id 不同字节"）
   f. node_kind 与 shot_ref 一致（first_frame 必须能对应到 S06 的镜头起点）
   g. identity_constraints 的每个条目可解析为 (character_id, revision)

3. 写 ImageJob(CREATED) + ReferenceCombination（若 content_hash 未存在）

4. PREPARING：取 RouteDecision（S25）。若 feasible=false → REJECTED_LOCAL
   注意：S29 只把 RouteDecision **引用**存下来（id + content_hash），不复制 eligible[]

5. SUBMITTED：组 generation-command(modality=image) 提交 S26

6. 等待 S26 事件；INGESTING 后由 S28 归档；READY 时写 KeyframeCandidate

7. 若该 (shot_lineage_id, node_kind) 的检查点为空或指向更低 priority 的候选
   → CAS 推进检查点 → outbox production.image.ready
```

### 4.2 正常路径：edit（`keyframe_edit` 的执行）

```
1. 来源必须是 S39 的 RepairAction，不是任意调用方
     POST /image-jobs
     {mode:"edit", source_candidate_id, mask:{artifact_ref, geometry},
      instruction, repair_action_id, window:{start_frame, end_frame}}

2. 校验（在 §4.1 的基础上追加）：
   a. window 帧数 ≤ 6                      → 否则 422 KEYFRAME_EDIT_OUT_OF_WINDOW
   b. source_candidate 的 shot_lineage_id 与 repair_action.target 一致
   c. mask.artifact 的 sha256 可读且走 S28
   d. source_candidate.state ∈ {READY, ACCEPTED}
   e. 该 repair_action_id 未被执行过（唯一约束，§7）

3. mode=edit 的 generation-command：
     input.references 必须包含：
       {artifact_id: source_candidate.artifact_id, sha256, role:"first_frame"}  // 或按模态约定
       {artifact_id: mask.artifact_id, sha256, role:"mask"}
     input.mode = "edit"

4. 产出新 KeyframeCandidate(source_candidate_id, mask_id, ...)
   —— 老候选**不删不改**，它仍是历史证据
```

**关键区分：`keyframe_edit` 产出的是"同一关键帧的新候选"，不是"同一候选的新 revision"。** 候选不可变。S29 的检查点随后可以指向新候选；旧候选保留，S39 的复检报告按 `attempt_slot_id` 关联到具体是哪一次。

### 4.3 `SUBMISSION_UNKNOWN` 的应对（本服务视角）

S29 **不判定** UNKNOWN，也不做对账。它只消费 S26 的事实。

**S29 在 UNKNOWN 期间必须做的：**

| 必须 | 理由 |
|---|---|
| 把 job 标成 `WAITING_UNKNOWN`，对外返回 `frozen=true` | 让上游知道"别等，但也别重试" |
| **不新建同 `input_fingerprint` 的 ImageJob** | 新建 = 绕过 S26 的铁律，可能第二次付费（E16） |
| **不发起 cancel** | 未收单的任务取消没有意义；已收单的任务取消可能仍计费 |
| **不推进检查点** | 结果未知，没有候选可推进 |
| **不释放任何东西** | 本服务本来就不持有 reservation / permit |
| 在 `GET /image-jobs/{id}` 暴露 `must_reconcile_before_resubmit=true` | 与 `admission-record.v2` 的语义一致 |

**实现约束（结构性，不是流程约束）：**

```sql
-- 同一 (tenant, shot_lineage_id, node_kind, input_fingerprint) 同时只能有一个
-- 未终结的 ImageJob。WAITING_UNKNOWN 算"未终结"。
CREATE UNIQUE INDEX image_job_open_uniq
  ON image_job (tenant_id, shot_lineage_id, node_kind, input_fingerprint)
  WHERE state NOT IN ('READY','REJECTED_LOCAL','FAILED','RECONCILED_FAILED','CANCELED');
```

这条索引是 E16 在 S29 侧的落点：**想绕过 UNKNOWN 重提，数据库不允许**。任何"再建一个 job 试试"的代码路径会撞唯一约束而不是悄悄成功。

**S29 不实现"对账完成后自动重提"。** 当 S26 报 `RECONCILED_FAILED`（确认未收单且已放弃），S29 把 job 置终态并发事件；是否重试由 **S39**（repair）或 **S21**（正常流水线）决定。如果决定重试，那是一个新 `ImageJob`，`attempt_no` 由 S26 校验，跨 Run 计数由 S39 累计（§8）。

### 4.4 关键帧版本变化 → 视频失效传播

这是 service.yaml `acceptance` 的第二条："关键帧版本变化传播相关视频失效"。实现的**正确**方式是发布事实、不作判定：

```
S29: 检查点推进
  → outbox: production.image.ready
      data: {
        image_job_id, candidate_id,
        shot_lineage_id, node_kind,
        artifact_ref (S28), reference_combination_hash,
        checkpoint: {from_candidate_id, to_candidate_id, checkpoint_seq},
        built_for: {manifest_ref, node_epoch_input, execution_fence},
        repaired_from: {repair_action_id, source_candidate_id} | null
      }
```

**S29 不发布"这些视频作废"。** 理由三条：

1. 影响闭包归 **S09**（[docs/04 §2](../docs/04-data-consistency.md)、[S16 §5.1](S16-story-memory.md) 的同一分工）。S29 不知道哪些视频引用了这张图——它不持有视频依赖图。
2. 判定"作废"需要比较 `node_epoch` 与 manifest，那是 **S08** 的语言。S29 越权判定会让"哪个 epoch 有效"出现两个答案。
3. S30 自己订阅 `production.image.ready`，用自己的规则决定当前视频是否还对应检查点。S30 的判断依据是它 `VideoJob` 里记录的 `keyframe_checkpoint_seq`。

**传播的实际链路：**

```
production.image.ready
  ├─ S30 订阅：若 video_job.keyframe_checkpoint_seq < 事件里的 checkpoint_seq
  │            → 标记 clip candidate 为 stale（S30 自己的状态，不改 S29 任何东西）
  ├─ S09 订阅：更新依赖边与影响种子
  ├─ S41 订阅：时间线可能需要重切（它是时间线所有者）
  └─ S47 订阅：实时状态投影
```

**一个必须防的错误**：S29 在 `keyframe_edit` 后把检查点指向新候选，但新候选**没有**经过 S35/S36 复检。此时下游如果直接把它当已接受会跳过复检。所以 `production.image.ready` 的事件里 `repaired_from != null` 时必须带 `requires_recheck: true`，S39 的 recheck 计划（[S39 §6](S39-repair-planning.md)）负责触发。**S29 不自己发 QC 请求**——QC 的触发者是 S21 或 S39，不是生产者。

### 4.5 归档失败：只重归档，不重生成

S28 可能在两种情况下失败：

| 情形 | S29 的动作 |
|---|---|
| `provider_url` 过期（`provider_url_expires_at < now()`） | **先向 S26 查询新 URL**（S26 是 URL 的持有者，它有 S27 的 `refreshUrlSupported` 能力）。拿到新 URL 后重新调 S28。**绝不重新提交生成** |
| S28 返回 5xx / 超时 | 有界退避重试 `POST /artifact-imports`（同一 `idempotency_key`，S28 侧按业务键去重） |
| S28 返回 hash 不一致 | 终态失败，`FAILED(ARTIFACT_HASH_MISMATCH)`。不重试——这通常意味着供应商返回了不同内容，重试会掩盖问题 |

S29 **不保存**供应商 URL。URL 的生命周期（`url_ttl_seconds`）由 S26/S27 管理。S29 保存的是 `provider_job_id`，需要 URL 时问 S26。这样 URL TTL 只有一个权威。

### 4.6 失败矩阵

| # | 场景 | 检测点 | S29 动作 | 状态 | 是否花钱 | 计数 |
|---|---|---|---|---|---|---|
| F1 | 参考 artifact 不可读 | PREPARING 的只读校验 | 拒绝，不建 attempt | `REJECTED_LOCAL` | 否 | 不 +1 |
| F2 | `references[].sha256` 与 S28 不符 | 校验 e | 拒绝，`REFERENCE_HASH_MISMATCH` | `REJECTED_LOCAL` | 否 | 不 +1 |
| F3 | 跨租户 artifact | 校验 c | **404**（不泄漏存在性） | 不建 job | 否 | 不 +1 |
| F4 | S25 `feasible=false` | PREPARING | 透传 `infeasible_reason`，不猜默认模型 | `REJECTED_LOCAL` | 否 | 不 +1 |
| F5 | 报价过期（`quote_valid_to`） | SUBMITTED 前 | 重新取 RouteDecision，**不重用旧报价** | 停在 `PREPARING` | 否 | 不 +1 |
| F6 | S23 余额不足 | S26 返回 `REJECTED_BUDGET` | 透传；不降规格、不降分辨率（`docs/09 §4`：最低交付规格不能无声降级） | `FAILED(BUDGET)` | 否 | **不 +1** |
| F7 | S22 无 permit | S26 返回 `REJECTED_QUOTA` | 透传；本服务不换池 | `FAILED(QUOTA)` | 否 | 不 +1 |
| F8 | 供应商明确拒绝（4xx 业务） | S26 → `PROVIDER_FAILED(rejected)` | 记录失败码；交 S39 | `FAILED` | **是**（可能按 `failure_charge`） | **+1** |
| F9 | 提交结果未知 | S26 → `SUBMISSION_UNKNOWN` | 冻结，不重提 | `WAITING_UNKNOWN` | 未知（保留） | 冻结期间不新增 |
| F10 | 运行中失败 | S26 → `PROVIDER_FAILED` | 记录；交 S39 | `FAILED` | 是 | +1 |
| F11 | 归档 URL 过期 | `INGESTING` | 向 S26 要新 URL，重归档 | `INGESTING` | **否**（不重生成） | 不 +1 |
| F12 | 归档 hash 不符 | `INGESTING` | 终态失败 | `FAILED(ARTIFACT_HASH_MISMATCH)` | 是 | 不 +1（已计） |
| F13 | `keyframe_edit` 窗口 > 6 帧 | `POST /image-jobs` 校验 | 422，**不建 job** | — | 否 | 不 +1 |
| F14 | 同一 repair_action 重复执行 | 唯一约束 | 409，返回原 job | — | 否 | 不 +1 |
| F15 | 检查点 CAS 冲突 | 并发推进 | 重读后重试一次；仍冲突 → 让 S08 裁决 | `READY` 但未推进 | 否 | — |
| F16 | 服务重启（`PREPARING` 阶段） | 启动扫描 | 幂等重取 RouteDecision；幂等提交 S26 | — | 否 | 不 +1 |

**失败矩阵的读法**：`计数 +1` 只发生在"真的花了钱、真的试了一次"。F6/F7（预算/配额拒绝）**不 +1**——[S39 §5.5](S39-repair-planning.md)："计划生成后 S02 拒绝 / 预算不足 / 无能力路由 → 不 +1（没花钱，没试）"。

---

## 5. 数据模型

### 5.1 表清单

| 表 | 作用 | 可变性 |
|---|---|---|
| `image_job` | 生产意图 | 状态可变，业务字段不可变 |
| `reference_combination` | 有序参考集合 | 不可变 |
| `edit_mask` | 空间 mask | 不可变 |
| `keyframe_candidate` | 产出候选 | 不可变 |
| `shot_image_checkpoint` | 基线指针 | 可变（CAS） |
| `image_intent_ledger` | 意图台账 | 只追加 |
| `command_receipt` | 幂等记录 | 只追加 |
| `outbox` / `inbox` | 事件 | 只追加 |
| `job_transition` | 状态历史 | 只追加 |

### 5.2 核心 DDL

```sql
CREATE SCHEMA IF NOT EXISTS image_production;

-- ---------- 生产意图 ----------
CREATE TABLE image_production.image_job (
  image_job_id        text PRIMARY KEY,
  tenant_id           text NOT NULL,
  project_id          text NOT NULL,
  series_id           text NOT NULL,
  episode_id          text NOT NULL,
  branch_id           text NOT NULL,
  shot_ref            jsonb NOT NULL,        -- {service_id:"S06", id, revision}
  shot_lineage_id     text NOT NULL,         -- 跨 Run 累计的锚点（见 §8）
  node_id             text NOT NULL,         -- logical_node
  node_epoch_at_create integer NOT NULL,
  execution_fence     integer NOT NULL,
  node_kind           text NOT NULL
    CHECK (node_kind IN ('first_frame','last_frame','keyframe_mid','reference_still')),

  mode                text NOT NULL CHECK (mode IN ('generate','edit')),
  prompt_intent       text NOT NULL,
  width               integer NOT NULL CHECK (width > 0),
  height              integer NOT NULL CHECK (height > 0),
  identity_constraints text[] NOT NULL DEFAULT '{}',

  reference_combination_id text NOT NULL,    -- FK 下方
  source_candidate_id text,                  -- 仅 edit
  edit_mask_id        text,                  -- 仅 edit
  repair_action_id    text,                  -- 仅 edit，来自 S39

  input_fingerprint   text NOT NULL,         -- §2.2
  request_hash        text NOT NULL,
  idempotency_key     text NOT NULL,

  route_decision_ref  jsonb,                 -- {id, content_hash}，不复制 eligible[]
  budget_scope_id     text NOT NULL,
  deadline            timestamptz NOT NULL,

  provider_job_id     text,                  -- S26 引用
  attempt_id          text,                  -- S26 引用
  business_attempt_no integer NOT NULL DEFAULT 1,
  reconciled          boolean NOT NULL DEFAULT false,

  state               text NOT NULL,
  state_seq           bigint NOT NULL DEFAULT 0,
  failure_code        text,
  failure_detail      jsonb,

  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  state_entered_at    timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ij_state_check CHECK (state IN (
    'CREATED','PREPARING','REJECTED_LOCAL','SUBMITTED','WAITING_PROVIDER',
    'WAITING_UNKNOWN','INGESTING','READY','FAILED','RECONCILED_READY',
    'RECONCILED_FAILED','CANCELED')),
  CONSTRAINT ij_edit_needs_mask CHECK (
    mode <> 'edit' OR (source_candidate_id IS NOT NULL AND edit_mask_id IS NOT NULL
                       AND repair_action_id IS NOT NULL)),
  CONSTRAINT ij_generate_no_mask CHECK (
    mode <> 'generate' OR (source_candidate_id IS NULL AND edit_mask_id IS NULL)),
  CONSTRAINT ij_tenant_scoped_idem UNIQUE (tenant_id, idempotency_key)
);

-- 同一 (shot_lineage, node_kind, fingerprint) 只能有一个未终结 job（§4.3）
CREATE UNIQUE INDEX image_job_open_uniq
  ON image_production.image_job
     (tenant_id, shot_lineage_id, node_kind, input_fingerprint)
  WHERE state NOT IN ('READY','REJECTED_LOCAL','FAILED','RECONCILED_FAILED','CANCELED');

-- 同一 repair_action 只能被执行一次（E12 的变体）
CREATE UNIQUE INDEX image_job_repair_uniq
  ON image_production.image_job (tenant_id, repair_action_id)
  WHERE repair_action_id IS NOT NULL;

CREATE INDEX image_job_shot_idx
  ON image_production.image_job (tenant_id, shot_lineage_id, created_at DESC);
CREATE INDEX image_job_state_idx
  ON image_production.image_job (state, state_entered_at)
  WHERE state IN ('PREPARING','SUBMITTED','WAITING_UNKNOWN','INGESTING');
CREATE INDEX image_job_provider_idx
  ON image_production.image_job (provider_job_id) WHERE provider_job_id IS NOT NULL;

-- ---------- 参考组合（不可变） ----------
CREATE TABLE image_production.reference_combination (
  reference_combination_id text PRIMARY KEY,
  tenant_id                text NOT NULL,
  content_hash             text NOT NULL,   -- 对有序数组 + 排序后的 constraints 求 sha256
  references               jsonb NOT NULL,  -- [{ordinal, artifact_id, sha256, role}]
  identity_constraints     text[] NOT NULL,
  created_at               timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT rc_hash_uniq UNIQUE (tenant_id, content_hash),
  CONSTRAINT rc_refs_nonempty CHECK (jsonb_array_length(references) > 0)
);

-- ---------- 编辑 mask（不可变，绑定源候选） ----------
CREATE TABLE image_production.edit_mask (
  edit_mask_id        text PRIMARY KEY,
  tenant_id           text NOT NULL,
  source_candidate_id text NOT NULL REFERENCES image_production.keyframe_candidate,
  artifact_ref        jsonb NOT NULL,        -- S28 {service_id,id,revision,content_hash}
  geometry_digest     text NOT NULL,
  instruction         text NOT NULL,
  window_start_frame  integer NOT NULL CHECK (window_start_frame >= 0),
  window_end_frame    integer NOT NULL,
  constraint_6frames  boolean NOT NULL DEFAULT true,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT em_window_order CHECK (window_end_frame >= window_start_frame),
  CONSTRAINT em_window_max6 CHECK (window_end_frame - window_start_frame + 1 <= 6),
  CONSTRAINT em_same_tenant UNIQUE (tenant_id, edit_mask_id)
);

-- ---------- 候选（不可变） ----------
CREATE TABLE image_production.keyframe_candidate (
  candidate_id             text PRIMARY KEY,
  tenant_id                text NOT NULL,
  image_job_id             text NOT NULL REFERENCES image_production.image_job,
  shot_lineage_id          text NOT NULL,
  node_kind                text NOT NULL,
  artifact_ref             jsonb NOT NULL,   -- 必须 service_id="S28"
  reference_combination_id text NOT NULL REFERENCES image_production.reference_combination,
  source_candidate_id      text REFERENCES image_production.keyframe_candidate,
  edit_mask_id             text REFERENCES image_production.edit_mask,
  provider_job_id          text NOT NULL,
  attempt_id               text NOT NULL,
  observed_output          jsonb NOT NULL,   -- {width,height,format,bytes} 供应商实际产出
  repair_action_id         text,
  requires_recheck         boolean NOT NULL DEFAULT false,
  state                    text NOT NULL CHECK (state IN ('READY','ACCEPTED','REJECTED')),
  created_at               timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT kc_artifact_is_s28 CHECK ((artifact_ref->>'service_id') = 'S28')
);

CREATE UNIQUE INDEX kc_artifact_uniq
  ON image_production.keyframe_candidate
     (tenant_id, (artifact_ref->>'id'), (artifact_ref->>'content_hash'));
CREATE INDEX kc_shot_idx
  ON image_production.keyframe_candidate (tenant_id, shot_lineage_id, node_kind, created_at DESC);
CREATE INDEX kc_job_idx ON image_production.keyframe_candidate (image_job_id);

-- ---------- 镜头图像生产检查点（可变，CAS） ----------
CREATE TABLE image_production.shot_image_checkpoint (
  tenant_id          text NOT NULL,
  shot_lineage_id    text NOT NULL,
  node_kind          text NOT NULL,
  candidate_id       text NOT NULL REFERENCES image_production.keyframe_candidate,
  checkpoint_seq     bigint NOT NULL,
  expected_revision  integer NOT NULL DEFAULT 0,   -- CAS 锚
  updated_by_job     text NOT NULL,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, shot_lineage_id, node_kind)
);

-- ---------- 意图台账（只追加，不是费用台账） ----------
CREATE TABLE image_production.image_intent_ledger (
  entry_id         uuid PRIMARY KEY,
  tenant_id        text NOT NULL,
  image_job_id     text NOT NULL,
  attempt_id       text,
  provider_job_id  text,
  shot_lineage_id  text NOT NULL,
  node_kind        text NOT NULL,
  action           text NOT NULL,       -- generate | keyframe_edit | regenerate_from_intent
  strategy_class   text,                -- 与 S39 同构，仅用于审计对齐
  repair_action_id text,
  run_id           text NOT NULL,       -- 仅审计归因，不进任何上限键
  node_epoch       integer NOT NULL,
  manifest_id      text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX iil_shot_idx
  ON image_production.image_intent_ledger (tenant_id, shot_lineage_id, created_at DESC);

-- ---------- 幂等 & 事件 ----------
CREATE TABLE image_production.command_receipt (
  tenant_id       text NOT NULL,
  command_id      text NOT NULL,
  request_hash    text NOT NULL,
  response_ref    jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE image_production.job_transition (
  image_job_id  text NOT NULL,
  seq           bigint NOT NULL,
  from_state    text,
  to_state      text NOT NULL,
  reason        text,
  at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (image_job_id, seq)
);

CREATE TABLE image_production.outbox (
  outbox_id    uuid PRIMARY KEY,
  tenant_id    text NOT NULL,            -- 事件信封 required；也用于中继分区
  aggregate_id text NOT NULL,
  aggregate_sequence bigint NOT NULL,
  event_type   text NOT NULL,
  partition_key text NOT NULL,           -- tenant_id || ':' || aggregate_id
  payload      jsonb NOT NULL,
  published_at timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);

CREATE INDEX outbox_unpublished_idx ON image_production.outbox (id) WHERE published_at IS NULL;

CREATE TABLE image_production.inbox (
  consumer     text NOT NULL,
  event_id     text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  PRIMARY KEY (consumer, event_id)
);
```

### 5.3 索引与约束的理由

| 约束 | 防的是什么 |
|---|---|
| `image_job_open_uniq` | E16：UNKNOWN 期间另建 job 绕过重提铁律 |
| `image_job_repair_uniq` | E12 变体：同一 RepairAction 重复投递导致两次付费编辑 |
| `kc_artifact_uniq` | 同一 artifact 被重复登记成两个候选，导致检查点来回跳 |
| `em_window_max6` | §1.4 的 6 帧约束落到数据库层，应用层 bug 也挡得住 |
| `em_same_tenant` + 所有表的 `tenant_id` 前缀 | E28：跨租户不可见 |
| `kc_artifact_is_s28` | 防止有人把供应商直链塞进 artifact_ref（绕过 S28 的第二权威） |
| `rc_hash_uniq` | 同内容参考组合只存一份，保证 `reference_combination_id` 可跨 job 复用 |

**没有外键到其他服务的表。** `provider_job_id` / `attempt_id` / `repair_action_id` 都是**文本引用**，不是 FK。跨服务 FK 在 database-per-service 下不可实现，假装有会更糟（会诱导开发者写跨库 JOIN 的替代方案）。

### 5.4 保留与清理

- `keyframe_candidate` / `reference_combination` / `edit_mask`：**不删**。它们是历史证据，删除会破坏"多视图参考和 mask 可追溯"的验收。
- `image_job`：终态后保留，`failure_detail` 可压缩。
- `outbox`：发布后 7 天清理（`docs/04 §4` 的 14 天事件保留是 Kafka 侧，本地 outbox 不必等那么久）。
- `inbox`：30 天。重放靠 Kafka 的独立 consumer group，不靠本地 inbox 长存。
- 媒体 bytes 的保留与删除归 **S28**，S29 只持有引用（[docs/04 §6](../docs/04-data-consistency.md)）。

---

## 6. 对外契约

### 6.1 `POST /image-jobs`

```jsonc
{
  "command_id": "cmd-img-0001",
  "request_hash": "sha256:…",
  "context": { /* execution-context.v2，必填 18 项 */ },
  "shot_ref": { "service_id": "S06", "id": "shot_ep3_sc2_07", "revision": 4 },
  "shot_lineage_id": "lin_ep3_sc2_07",
  "node_kind": "first_frame",
  "mode": "generate",
  "prompt_intent": "林岚站在雨夜巷口，回头，冷光侧逆，浅景深",
  "width": 1080,
  "height": 1920,
  "references": [
    { "ordinal": 0, "artifact_id": "art_char_linlan_v3", "sha256": "…", "role": "character" },
    { "ordinal": 1, "artifact_id": "art_scene_alley_night", "sha256": "…", "role": "scene" }
  ],
  "identity_constraints": ["char_linlan@3"],
  "route_policy_ref": { "id": "policy_route_default", "revision": 7 },
  "budget_scope_id": "bs_ep3",
  "deadline": "2026-11-01T12:00:00Z"
}
```

edit 模式：

```jsonc
{
  "mode": "edit",
  "source_candidate_id": "kfc_01J…",
  "edit_mask": {
    "artifact_id": "art_mask_01J…", "sha256": "…", "geometry": { "bbox": [812,340,240,180] }
  },
  "instruction": "把左手手指数量改成五根，保持手腕角度和光照",
  "repair_action_id": "ra_01J…",
  "window": { "start_frame": 118, "end_frame": 121, "fps": 24 }
}
```

**响应** `202 Accepted`：

```jsonc
{ "image_job_id": "ij_01J…", "state": "CREATED", "operation_id": "op_…" }
```

**校验顺序**（失败即返回，不建 job、不产生任何费用）：

1. `Idempotency-Key` 头 + `command_id` 幂等（同键同 body → 返回原 job；同键不同 body → `409 IDEMPOTENCY_MISMATCH`）
2. `context` 必填项与签名绑定（`docs/08 §1`：不信任 LLM 提供的 tenant 字段）
3. 跨租户 artifact 检查 → **404**，不是 403
4. `shot_ref.revision` 在 S06 存在且属于本租户
5. `node_kind` 与 `shot_ref` 时间结构一致
6. `references` 的 sha256 与 S28 一致
7. `mode=edit` 时窗口 ≤ 6 帧、`source_candidate` 归属一致、mask 绑定正确
8. `identity_constraints` 可解析
9. `deadline > now()`

### 6.2 `GET /image-jobs/{id}`

```jsonc
{
  "image_job_id": "ij_01J…",
  "source_revision": 3,
  "state": "WAITING_UNKNOWN",
  "business_attempt_no": 1,
  "provider_job_id": "pj_01J…",
  "provider_job_state": "SUBMISSION_UNKNOWN",
  "frozen": true,
  "must_reconcile_before_resubmit": true,
  "candidates": [ { "candidate_id": "kfc_…", "artifact_ref": {…}, "state": "READY" } ],
  "checkpoint": { "shot_lineage_id": "lin_…", "node_kind": "first_frame",
                  "candidate_id": "kfc_…", "checkpoint_seq": 12 },
  "failure": null,
  "history": [ {"seq":4,"from":"SUBMITTED","to":"WAITING_UNKNOWN","at":"…"} ]
}
```

`must_reconcile_before_resubmit` 恒等于 `provider_job_state == 'SUBMISSION_UNKNOWN'`。与 `admission-record.v2.schema.json` 的 `allOf` 语义一致。

### 6.3 其他查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/image-jobs?shot_lineage_id=&node_kind=&state=` | 列表，带 `projection_watermark` |
| GET | `/shots/{shot_lineage_id}/image-checkpoint` | 当前检查点（S30 的主读口） |
| GET | `/keyframe-candidates/{id}` | 候选详情 + 完整参考链（可追溯验收的读口） |
| POST | `/image-jobs/{id}:cancel` | 取消；只调 S26 cancel，**不承诺退款** |
| POST | `/image-jobs/{id}:retry` | **仅当 `WAITING_UNKNOWN` 之外**且 `state ∈ {FAILED, RECONCILED_FAILED}`。新建 job，`attempt_no` 由 S26 校验 |

`POST /image-jobs/{id}:retry` 在 `WAITING_UNKNOWN` 时返回 `409 MUST_RECONCILE_FIRST`。这条拒绝是 E16 在 API 层的落点。

### 6.4 发布事件

| event_type | topic | aggregate | 何时 |
|---|---|---|---|
| `image.job.state-changed` | `drama.image-production.v2` | `image_job_id` | 每次状态转移 |
| `production.image.ready` | `drama.image-production.v2` | `shot_lineage_id` | 检查点推进且候选 `READY` |
| `image.candidate.rejected` | 同上 | `candidate_id` | 候选被 S29 自己判为不可用（如 hash 不符） |
| `image.keyframe-refresh-failed`（建议新增） | 同上 | `shot_lineage_id` | `keyframe_edit` 失败且需要 S39 升级判定 |

`production.image.ready` 的 `data` 见 §4.4。**不含**供应商 URL、不含参考图二进制、不含 prompt 原文（prompt 通过 `data_ref` 指向 `reference_combination` + job，按权限读）。

`event-topics.v2.json` 当前只登记了 `production.image.ready`，其余三个是**建议补充**（C2，见 §12）。

### 6.5 消费事件

| 来源 | 事件 | 用途 |
|---|---|---|
| S26 | `generation.job.state-changed` | 驱动 `SUBMITTED → WAITING_PROVIDER → INGESTING → READY` 与 `WAITING_UNKNOWN` |
| S28 | `artifact.ingested` | 完成 `INGESTING → READY` |
| S15 | `asset.voice.authorization.changed` | **不消费**（S29 不涉及音色） |
| S10 | `binding.resolved` / 绑定变更 | 只用于刷新只读投影，**不触发自动重生成** |
| S08 | `snapshot.manifest.committed` | 标记候选是否被纳入某 manifest（只读投影） |
| S02 | `policy.revision.published` | 刷新 `route_policy_ref` 可用性（只读） |

**S29 不消费 `quality.*` 事件来自动重做。** 修复由 S39 决策、S21 执行；S29 只响应 `POST /image-jobs` 的 `repair_action_id`。

---

## 7. 幂等与并发

### 7.1 三条幂等键

| 层面 | 键 | 冲突行为 |
|---|---|---|
| 命令 | `tenant_id + command_id + request_hash` | 同键同 body → 返回原响应；同键不同 body → `409` |
| 业务 | `(tenant, shot_lineage_id, node_kind, input_fingerprint)` 且未终结 | 第二个请求返回**已有 job**，不新建 |
| 修复 | `(tenant, repair_action_id)` | `409 REPAIR_ACTION_ALREADY_EXECUTED`，返回首次的 job |

`request_hash` 的计算**不含** `trace_id`、`created_at`、`Idempotency-Key` 本身。含 `context` 业务字段、`shot_ref`、`prompt_intent`、`references`（有序）、`identity_constraints`（排序后）、`mode`、`width/height`、`budget_scope_id`、`deadline`。

### 7.2 并发场景

| 场景 | 机制 |
|---|---|
| 同镜同 node_kind 两个 job 同时 READY | 检查点 CAS：`UPDATE ... WHERE expected_revision = $expected`。失败方重读，按 `candidate.priority` 比较后决定是否重试 |
| 两个 `keyframe_edit` 对同一候选 | 各自产出新候选（不同 mask），检查点串行推进。两次都保留 |
| 同一 `repair_action` 并发投递 | 唯一索引 `image_job_repair_uniq` 只放一个过 |
| 检查点推进与 S08 提交并发 | **互不阻塞**。它们是两个指针。S08 用 manifest 的 `node_epoch` 做自己的 CAS |
| 同一 artifact 被两个 job 登记 | `kc_artifact_uniq` 挡住；第二个 job 复用已有候选 |

### 7.3 重启恢复

| 重启时状态 | 恢复动作 |
|---|---|
| `CREATED` | 异步 worker 重新领取，重新走 PREPARING |
| `PREPARING` | 幂等重取 RouteDecision；若已提交 S26 则回到 `SUBMITTED`（靠 S26 的命令幂等） |
| `SUBMITTED` / `WAITING_PROVIDER` | 对每个 job `GET /generation-jobs/{id}` 拉一次权威状态，按返回修正。**不盲信本地状态** |
| `INGESTING` | 幂等重发 `POST /artifact-imports`（S28 侧按 `idempotency_key` 去重） |
| `WAITING_UNKNOWN` | **什么都不做**。等 S26 的事件或对账结论 |

**启动扫描的实现约束**：扫描必须限制并发与速率（避免重启雪崩打爆 S26/S28）。默认每批 100 个 job，间隔 5s，按 `state_entered_at` 升序。

---

## 8. 跨 Run 尝试计数不能清零

### 8.1 权威分工

| 计数 | 权威 | S29 的角色 |
|---|---|---|
| `attempt_no`（单 job 内业务尝试序号） | **S26**（`generation_attempt.attempt_no` + `max_business_attempts`） | 传递，不判定 |
| 跨 Run 累计（每镜 / 每策略 / 每集） | **S39**（`repair_planning.attempt_ledger`，键不含 `run_id`） | 提供 `shot_lineage_id` 与 `strategy_class`，不计数 |
| 节点累计费用 | **S26**（`attempt_ledger` UNIQUE `(tenant_id, node_id)`） | 只读观测 |
| 金额 | **S23** | 不涉及 |

**S29 不建立第四个计数器。** 本服务的 `image_intent_ledger` 是审计台账，只追加、不裁决，任何上限检查都不读它。

### 8.2 为什么 S29 必须把 `shot_lineage_id` 带到底

[S39 §5.2](S39-repair-planning.md)：

```
AttemptCounterKey = (tenant_id, series_id, branch_id, shot_lineage_id, strategy_class)
```

这个键**故意不含** `run_id` / `manifest_id` / `manifest_revision` / `node_epoch` / `execution_fence` / `model_snapshot` / `provider` / `workflow_id` / `defect_fingerprint`。

S29 的 `image_job.shot_lineage_id` 是这条键在图像侧的输入。如果 S29 在"新 Run"里给同一镜生成新的 lineage_id（例如用 job_id 代替），跨 Run 计数立刻被清零：[docs/11 §3](../docs/11-quality-repair.md) 的"子工作流、换模型、换 Run 不能清零已消耗的总费用"就被绕过。

**结构性约束（三条）：**

1. `shot_lineage_id` **必须由上游传入**，S29 不生成。它来自 S06/S08 在 shot 创建时的稳定 ID（契约缺口 C4：[S39 §11](S39-repair-planning.md)）。
2. 同一 `shot_ref.id` 在不同 Run / 不同 manifest 下，`shot_lineage_id` 必须相同。**S29 实现一条断言**：若某 `shot_ref.id` 曾以别的 `shot_lineage_id` 出现过，拒绝并告警 `SHOT_LINEAGE_DRIFT`。这是防"换 Run 换 id"的最后一道。
3. `image_job` 的所有查询、唯一索引、检查点都以 `shot_lineage_id` 为键，**不以 `run_id` 为键**。`run_id` 只出现在 `image_intent_ledger` 的审计列里。

### 8.3 `strategy_class` 的对齐

S29 在有 `repair_action_id` 时，把 S39 下发的 `strategy_class`（`f(action, normalized_parameters)`）原样写进 `image_intent_ledger`。**S29 不自己计算 `strategy_class`**——计算规则归 S39（[S39 §5.3](S39-repair-planning.md)），因为 `variation_nonce` / `seed` / `attempt_id` / `provider` / `model_id` 不能参与归一化，这些恰恰是 S29 会产生的东西。两个服务各算一套必然不一致。

### 8.4 一个具体例子

| Run | 事件 | S29 动作 | `shot_lineage_id` | `strategy_class` | S39 累计 |
|---|---|---|---|---|---|
| Run 1 | 首帧生成 | `generate` | `lin_s07` | — | 起算 |
| Run 1 | 首帧手部缺陷 → `keyframe_edit` | edit | `lin_s07` | `keyframe_edit:mask_v1` | seq=1 |
| Run 2 | 复检仍不过 → 再 `keyframe_edit`（换 seed） | edit | `lin_s07` | `keyframe_edit:mask_v1`（**seed 不进键**） | seq=2 |
| Run 2 | 换 mask 区域再试 | edit | `lin_s07` | `keyframe_edit:mask_v2` | seq=1（不同策略类），但 `shot_total=3` |

第 3 行说明：换 seed 不换策略 → 同一 `strategy_class` → 计入"同类同策略最多 1 次额外重复"（[docs/11 §3](../docs/11-quality-repair.md)），`seq=2` 若超过 `max_identical_strategy_repeats + 1` 直接被 S39 DENY，**S29 根本收不到这个 repair_action**。

第 4 行说明：换 mask 区域是不同策略，`strategy_class` 不同，`seq` 重新从 1 起——但 `shot_total` 继续累计，不因换策略清零。S29 的 `image_job_open_uniq` 与 `image_job_repair_uniq` 都不干扰这个统计，因为它们只防重复，不做上限判定。

---

## 9. 可观测

### 9.1 指标

| 指标 | 类型 | 用途 | 告警 |
|---|---|---|---|
| `img_jobs{state}` | gauge | 各状态堆积 | `WAITING_UNKNOWN` > 0 立即告警 |
| `img_job_duration_seconds{mode,node_kind}` | histogram | p50/p95/p99 | p99 超 SLO |
| `img_rejected_local_total{reason}` | counter | 本地拒绝分布 | `REFERENCE_HASH_MISMATCH` 突增 = 上游缓存问题 |
| `img_attempts_per_job` | histogram | 重试分布 | > 2 占比高 = 路由或 prompt 有问题 |
| `img_unknown_age_seconds` | gauge | 冻结时长 | > 1h 升级，> 6h 转人 |
| `img_edit_window_rejected_total` | counter | `keyframe_edit` 窗口越界 | > 0 说明 S35 registry 与 S39 路由不一致 |
| `img_candidate_requires_recheck_total` | counter | 待复检候选数 | 长期 > 0 = 复检链路断 |
| `img_checkpoint_cas_conflicts_total` | counter | 检查点竞争 | 突增 = 并发写同一镜 |
| `img_ingest_retry_total` | counter | 归档重试 | — |
| `img_ingest_ttl_remaining_seconds` | gauge | URL 剩余寿命 | < 900s 告警 |
| `img_reference_combo_cache_hit_ratio` | gauge | 参考组合复用率 | 低 = hash 计算不稳（有序性 bug） |
| `img_artifact_hash_mismatch_total` | counter | hash 不符 | **任何一次都告警** |

### 9.2 追踪

`traceparent` 贯穿：`POST /image-jobs → route → POST /generation-jobs → S26 Saga → S27 submit → S28 import → production.image.ready`。

必须能回答："这张 `kfc_xxx` 是哪一次 attempt 产的、用的哪版参考、走的哪条路由、花了多少"。前四项 S29 自己的表能答，最后一项查 S23。

### 9.3 日志

- **不记 prompt 原文**（docs/14 最小化）。记 `prompt_intent_hash` + 长度。
- **不记供应商 URL**（TTL 敏感，且属于 S26）。
- 记 `reference_combination.content_hash`，可复现。
- 失败记 `failure_code` + `failure_detail` 的结构化字段，不打整段异常栈。

---

## 10. 测试清单

| 编号 | 场景 | 期望 | E |
|---|---|---|---|
| T29-01 | 同 `command_id` 同 body 重复投递 | 返回原 `image_job_id`，不新建 | E12 |
| T29-02 | 同 `command_id` 不同 body | `409 IDEMPOTENCY_MISMATCH` | E12 |
| T29-03 | 跨租户 `reference.artifact_id` | **404**，响应体与"不存在"完全一致 | E28 |
| T29-04 | `references[].sha256` 与 S28 不符 | `REJECTED_LOCAL(REFERENCE_HASH_MISMATCH)`，不建 attempt | E19 |
| T29-05 | 参考有序性：`[A,B]` vs `[B,A]` | 两个不同 `content_hash`，两个不同候选 | E02 |
| T29-06 | `identity_constraints` 顺序不同但集合相同 | **同一** `content_hash` | E02 |
| T29-07 | S25 `feasible=false` | `REJECTED_LOCAL`，透传 `infeasible_reason`，**不返回默认模型** | E19 |
| T29-08 | S26 返回 `REJECTED_BUDGET` | job `FAILED(BUDGET)`，S39 侧计数**不 +1** | E14 |
| T29-09 | S26 返回 `REJECTED_QUOTA` | job `FAILED(QUOTA)`，预算已补偿（S23 侧断言） | E14 |
| T29-10 | S26 进入 `SUBMISSION_UNKNOWN` | job `WAITING_UNKNOWN`、`frozen=true`、**不发任何新请求** | E16 |
| T29-11 | UNKNOWN 期间 `POST /image-jobs` 同 fingerprint | 唯一约束拒绝 / 返回原 job；**不产生第二次提交** | E16 |
| T29-12 | UNKNOWN 期间 `POST /image-jobs/{id}:retry` | `409 MUST_RECONCILE_FIRST` | E16 |
| T29-13 | S26 对账后 `ARCHIVED` | `RECONCILED_READY`，候选写入，检查点可推进 | E16 |
| T29-14 | S28 归档时 URL 已过期 | 向 S26 取新 URL，**不重新生成** | E23 |
| T29-15 | S28 归档返回 hash 不符 | `FAILED(ARTIFACT_HASH_MISMATCH)`，不重试 | E23 |
| T29-16 | `keyframe_edit` 窗口 7 帧 | `422 KEYFRAME_EDIT_OUT_OF_WINDOW`，不建 job，不 +1 | E24 |
| T29-17 | `keyframe_edit` 窗口 6 帧 | 接受，产出新候选，`requires_recheck=true` | E24 |
| T29-18 | 同一 `repair_action_id` 两次 | `409`，只执行一次 | E24 |
| T29-19 | mask 跨候选复用 | DB 约束拒绝 | E24 |
| T29-20 | 同一 artifact 被两个 job 产出 | `kc_artifact_uniq` 挡住，复用已有候选 | E12 |
| T29-21 | 检查点并发 CAS | 一个成功，另一个重读后按 priority 决定 | E20 |
| T29-22 | 服务在 `PREPARING` 重启 | 幂等恢复，不产生第二个 `provider_job` | E13 |
| T29-23 | 服务在 `INGESTING` 重启 | 幂等重归档，**不重生成** | E13 |
| T29-24 | 检查点推进后 S30 订阅 | S30 标记 clip stale；**S29 不写任何 S30 表** | E01 |
| T29-25 | `production.image.ready` 乱序到达下游 | 下游按 `checkpoint_seq` 拒绝旧事件；S29 侧 `state_seq` 单调 | E17 |
| T29-26 | 关键帧新 revision 但 S08 未接受 | `GET /shots/{id}/image-checkpoint` 返回新候选；S08 accepted pointer **不变** | E07 |
| T29-27 | Kafka 重放 `production.image.ready` | 消费者 `side_effects=false`，不触发二次生成 | E32 |
| T29-28 | 不同 Run 同一 shot 的 `shot_lineage_id` 不同 | 拒绝 + `SHOT_LINEAGE_DRIFT` 告警 | E26 |
| T29-29 | `strategy_class` 换 seed | 与原策略类相同 → S39 DENY，S29 收不到该 repair_action | E26 |
| T29-30 | 同一 shot 换 Run 重试 | S39 `shot_total` 继续累计，不清零 | E26 |
| T29-31 | 供应商账户撤销 / 模型退休 | S26 侧阻断；S29 收到明确失败，不自行换渠道 | E18 |
| T29-32 | 数据驻留：参考 artifact 在禁止 region | `REJECTED_LOCAL(RESIDENCY_FORBIDDEN)` | E31 |
| T29-33 | 独立升级回滚 | 新旧版本对同一 `image_job` 状态机解释一致（表驱动） | E01 |
| T29-34 | 人工取消 | 调 S26 cancel；不可取消时返回 `CANCEL_PENDING`，**不报退款** | E22 |
| T29-35 | 恶意 mask artifact（SSRF 伪装成图像） | S28 侧沙箱拦截；S29 不自行下载和解析 mask | E29 |

---

## 11. 实施顺序与未决

### 11.1 顺序（W3）

1. **表与状态机骨架**（无外部调用）：`image_job` / `reference_combination` / `keyframe_candidate` / `shot_image_checkpoint` + 纯状态转移测试（T29-01/02/21/22）。
2. `POST /image-jobs` + 校验顺序 + 幂等（T29-03…06）。
3. `ReferenceCombination` 内容寻址 + 有序性（T29-05/06）。
4. S25 路由对接（mock）：`feasible=false` 分支（T29-07）。
5. S26 提交对接（mock）：`REJECTED_BUDGET` / `REJECTED_QUOTA` / `SUBMISSION_UNKNOWN`（T29-08…12）。
6. S28 归档对接（mock）：URL 过期、hash 不符（T29-14/15）。
7. 检查点 CAS + `production.image.ready`（T29-21/24/26）。
8. `keyframe_edit` 路径 + 6 帧校验 + repair 幂等（T29-16…19）。
9. 跨 Run lineage 断言 + `strategy_class` 透传（T29-28…30）。
10. 巡检：`WAITING_UNKNOWN` 年龄、URL TTL、recheck 积压。
11. **真实渠道接入需实施会话明确授权**（`docs/16 §6`）。

### 11.2 未决

| # | 问题 | 当前处理 |
|---|---|---|
| U1 | `shot_lineage_id` 由谁生成、规则是什么 | **阻塞**。契约缺口 C4（[S39 §11](S39-repair-planning.md)）。在补齐前，S29 要求调用方**必须**显式传入，缺失即 `422`，不猜 |
| U2 | 一个 `ImageJob` 能否产出多个候选（多 seed 并行） | 当前设计允许（`keyframe_candidate` 对 `image_job_id` 是 1:N）。但 `contracts/openapi.v2.yaml` 的 `generation-jobs` 是**一 job 一 provider_job**，所以 N 候选需要 N 个 `ImageJob` 或一个 job 串行提交。暂定：**一 job 一候选**，多 seed 由 S21 发多个 job。需要在实施时与 S21 确认 |
| U3 | `reference_still` 的 `priority` 排序规则 | 未定。当前用 `created_at` 后到者优先。需要导演策略给出（可能进 `route_policy_ref`） |
| U4 | `keyframe_edit` 的"成功"判定门槛 | 依赖 S35/S36 的复检。S29 不判。但"复检不通过时 S29 是否自动降级到 `regenerate_video` 意图"**未决**——当前是**否**，必须 S39 重新决策 |
| U5 | 检查点能否回退（新候选更差） | 当前允许人工 `POST /shots/{id}/image-checkpoint:rollback`，需要 S40 角色权限。自动化档位不能回退 |
| U6 | `image` 模态是否支持 `video_reference` role | 契约枚举里有，但图像模型一般不接受视频参考。疑为契约过宽，见 C4 |
| U7 | `observed_output` 与请求 `width/height` 不一致时的处理 | 当前：记录偏差，`READY` 但 `requires_recheck=true`。S35 的技术检查会独立判。S29 不自行缩放（缩放是 S42/交付层的事） |

---

## 12. 冲突与契约缺口

> 按 [AGENTS.md](../AGENTS.md) 与 [plans/README.md](README.md)：方案与设计文档冲突时**停下报告，不自行选边**。以下每条都标出"我方不选边"的具体表现。

### C1 `keyframe_edit` 在 S35 与 S39 里没有"执行者"字段

[S35 §2.2.1](S35-technical-qc.md) 定义动作族，[S39 §3](S39-repair-planning.md) 定义路由，但**没有任何一处写明 `keyframe_edit` 由谁执行**。本文按职责推定为 S29（它是唯一拥有 `EditMask` 与图像 edit 能力的服务），并把这一推定显式写进 §1.4。

**冲突点**：如果设计意图是 S30 或 S41 执行 `keyframe_edit`，那么 6 帧窗口的语义会不同（视频帧 vs 关键帧索引），S29 的实现会整体错位。

**不选边**：S29 实现成"能执行也能拒绝"，拒绝码 `KEYFRAME_EDIT_NOT_OWNED`。接入时若发现执行者另有其人，只需把 `POST /image-jobs` 的 edit 分支关掉，其余不影响。

### C2 `event-topics.v2.json` 只有 `production.*.ready`

S29 需要至少三个额外事件（`image.job.state-changed` 已在 S26 侧以 `generation.job.state-changed` 形式存在，但本服务自己的 job 状态需要独立事件；`image.candidate.rejected`；`image.keyframe-refresh-failed`）。

**现状**：registry 里只有 `production.image.ready` 一条。S21/S47 无法在本服务 job 状态变化时被唤醒，只能轮询 `GET /image-jobs/{id}`。

**建议**：补充登记，或在 `docs/04 §4` 层面明确"生产服务的 job 状态只走 `production.*.ready` + 轮询"，那 S29 就只发一个事件。**本文按"只有 ready"实现，其余标注为建议**。

### C3 `generation-command.v2` 的 `image.references[].role` 包含 `audio_reference`

图像模态接受音频参考在物理上不成立（除极少数多模态模型）。这是契约从"通用 references"复制过来的残留。

**不选边**：S29 在校验 e 之外增加一条本地规则——`mode=generate` 且 `role ∈ {audio_reference, video_reference}` 时 `422 ROLE_NOT_APPLICABLE_FOR_IMAGE`，并在响应里引用本缺口。若契约确实要支持，删掉这条本地规则即可。

### C4 `shot_lineage_id` 未在契约中定义（沿用 S39 C4）

**这是本服务的关键阻塞项**：§8 的跨 Run 计数、`image_job_open_uniq`、检查点主键全部依赖它。

**建议**：由 S06/S08 在 shot 创建时生成并在 manifest 里固定；契约给出生成规则与不可变性约束（[S39 §11 C4](S39-repair-planning.md) 已提出）。

**本文的处理**：`shot_lineage_id` 为必填入参，缺失 → `422 SHOT_LINEAGE_REQUIRED`。**不自行生成**——自行生成会在多实例部署下产生不同 ID，彻底破坏跨 Run 累计。

### C5 `image` 模态缺少"无图"的显式空值表达

对比 S31 的"无对白分支必须显式 no-op，不能创建空音频假成功"（[docs/07 §1 步骤 5](../docs/07-production-workflows.md)），图像的对应情形是"这一镜不需要关键帧"（例如纯黑场、纯色转场）。

**现状**：`generation-command.v2` 的 `image` 分支要求 `prompt_intent` 等必填项，无法表达"无图"。

**不选边**：S29 **不创建** `ImageJob` 来表达"无图"，也不创建占位候选。调用方不调 `POST /image-jobs` 即表示无图。S30 在 `references` 里不出现 `first_frame` 时按自己的规则处理（可能回退到文生视频）。**这依赖 S30 的配合，不是 S29 能单方面保证的**，需要在 S30 的方案里确认。

### C6 `docs/11 §2` 说"静态关键帧局部错 → mask 图像编辑"，但没说 mask 的窗口上限

`docs/11` 的修复表只说"mask 图像编辑"，6 帧上限来自 [S35 §2.2.1](S35-technical-qc.md)。两处文档的措辞粒度不同。

**不选边**：本文以 S35 的 6 帧为准（它是更新的、更具体的），但在 §1.4 显式引用两处来源，并在 `edit_mask.em_window_max6` 上留注释指向本缺口。若 `docs/11` 的意图是"图像 mask 编辑不受帧窗口限制"，那么 S29 的约束应改为"仅当缺陷来自视频帧窗口时受 6 帧限制"。这需要设计负责人裁决。

### C7 S29 的 `depends_on` 缺 S25

`services/image-production/service.yaml` 的 `depends_on` 是 `[S26, S28, S10]`。但本文的逻辑是 S29 必须先拿到 `RouteDecision` 才能提交 S26（`POST /generation-jobs` 强制要求 `route_decision_id`）。

**冲突点**：要么 S29 直接调 S25（那 `depends_on` 少了 S25），要么 `route_decision_id` 由调用方（S21）传入（那 S29 不调 S25，`depends_on` 正确）。

**不选边**：本文两者都支持——`POST /image-jobs` 接受可选的 `route_decision_id`；若未提供且 `route_policy_ref` 存在，则 S29 调 S25。**当前默认走 S21 传入**，以保持 `service.yaml` 的依赖图不变。这与 [S39 §11 C7](S39-repair-planning.md) 记录的"契约图看不出实际依赖"是同一类问题。

### C8 生产组六个服务（S29–S34）的 job 状态事件没有统一契约

S26 有 `generation.job.state-changed`，S41 有 `post.timeline.revision.created`，但 S29–S34 的 job 生命周期事件在 `event-topics.v2.json` 里只有各自的 `production.*.ready`。S21 的 `ProviderJobWait` 靠 S26 的信号（[S26 §10](S26-generation-jobs.md)），这没问题；但 S21 的 `ProductionStage` 层需要知道"S31 配音阶段完成了"，而 `production.speech.ready` 只覆盖成功路径。

**缺口**：失败 / 冻结 / 取消没有事件，S21 只能等 deadline。E06（无人值守关键镜头失败 → INCOMPLETE 报告）在这一层会变慢但不会错。

**建议**：六份方案共同提出 `production.stage.failed` / `production.stage.blocked` 两个事件类型，由 S29–S34 统一发布。本文按此建议实现，标注为待 registry 补齐。

---

## 13. 与其他方案的交叉引用

| 方案 | 关系 |
|---|---|
| [S26](S26-generation-jobs.md) | **唯一付费执行入口**。S29 提交 `POST /generation-jobs`，消费 `generation.job.state-changed`。S29 不碰 Saga 内部任何一步 |
| [S25](S25-model-routing.md) | 产出 `RouteDecision`。`normalized_input.width/height` 是实际派单规格，S29 不得改 |
| [S28](S28-artifact-ingest.md) | 归档图像 bytes。S29 只调 `POST /artifact-imports` 与 `GET /artifacts/{id}` |
| [S30](S30-video-production.md) | 消费 `production.image.ready` 与 `GET /shots/{id}/image-checkpoint`。S29 不写 S30 任何表 |
| [S35](S35-technical-qc.md) | `identity` 动作族与 6 帧窗口的来源。S29 只读 registry，不改 |
| [S39](S39-repair-planning.md) | 下发 `RepairAction` 与 `strategy_class`。S29 是 `keyframe_edit` 的执行者，不是决策者 |
| [S08](S08-revision-snapshot.md) | 唯一接受权威。检查点推进**不**等于接受 |
| [S23](S23-budget-ledger.md) / [S22](S22-scheduler-quota.md) | 只通过 S26 间接交互。S29 不持有 reservation / permit 引用 |
