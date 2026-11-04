# S30 · 视频片段生产 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。所有供应商仍为 `disabled`，`slo.verified: false`。

上游依据：[docs/09 §1](../docs/09-model-execution.md)（"S29–S34 负责生产意图与阶段，不能绕过 S26 直连模型"）、[docs/08 §3](../docs/08-agents-prompts.md)（**"目标 4 秒与供应商生成档位分开；剪辑裁切方案与成本在路由计划明确"**）、[docs/07 §1 步骤 5–7](../docs/07-production-workflows.md)、[docs/11 §2](../docs/11-quality-repair.md)、[docs/04 §3](../docs/04-data-consistency.md)、[S25 §5](../plans/S25-model-routing.md)（时长吸附与报价一致）、[S35 §2.2.1](S35-technical-qc.md)、[S39 §5](S39-repair-planning.md)、`contracts/generation-command.v2.schema.json`（`modality: video`）。

链上位置：`S29 检查点 → S30 组视频意图（首尾帧 + 时长映射）→ S25 估价路由 → S26 准入 → S27 执行 → S28 归档 → S30 登记候选并发布 ready → S35/S36 QC → S39 修复`。

本文重点回答三个问题：**目标 4 秒与供应商档位怎么映射且不撒谎**（§2.4、§4.2）、**裁切计划与成本为什么必须在路由阶段定死**（§4.3）、**"对白主动作不被截断"怎么变成可测的约束**（§4.4）。

---

## 0. 一句话边界

**S30 拥有"这一镜的视频片段长什么样、用了哪个关键帧基线、请求 4 秒实际生成几秒、从哪里裁到哪里、这一镜当前用哪一组候选"。它不拥有关键帧、不拥有音轨、不拥有时间线、不拥有钱。**

```
S29:   关键帧候选与检查点（S30 只读）
S30:   VideoJob + ClipCandidate + 时长映射 + 裁切计划 + 镜头视频检查点
S31:   对白音频（S30 只引用，不生成）
S25:   时长吸附与报价（S30 消费，不改）
S26:   attempt / ProviderJob / SUBMISSION_UNKNOWN
S28:   视频 bytes
S35/S36: QC（S30 不产分数）
S41:   时间线（S30 只提供片段，不决定位置）
S08:   当前接受指针
```

判据：任何"这一镜现在成片是哪一段"的问题，答案来自 S30 的镜头视频检查点 + S28 的 artifact。任何"它在整集里排第几位"的问题，答案来自 S41 的 EDL。

---

## 1. 责任与明确不负责

### 1.1 本服务是唯一写者

| 事实 | 说明 |
|---|---|
| `VideoJob` | 一次视频生产意图，绑定的首尾帧基线、时长策略、必要能力 |
| `ClipCandidate` | 一个已归档的视频片段，及其时长映射、裁切计划、provider 溯源 |
| `DurationMapping` | **目标时长 → 供应商档位 → 裁切区间**的三段映射，不可变 |
| `VideoEditPlan` | 裁切/变速/首尾处理计划的**意图**（不是执行；执行在 S42） |
| `ShotVideoCheckpoint` | 每 `shot_lineage_id` 的"当前生产基线片段集"指针 |
| `KeyframeBaselineRef` | 该 job 绑定的 S29 检查点快照（`checkpoint_seq` + 候选 id），用于检测关键帧漂移 |

### 1.2 本服务明确不写

| 事实 | 权威 | S30 的正确做法 |
|---|---|---|
| 关键帧图像 | **S29** | 只存 `{candidate_id, artifact_ref, checkpoint_seq}` |
| 对白音频、音色 | **S31 / S15** | 只引用 `audio_ref`；`audio_strategy` 表达策略，不生成音频 |
| 音效 / BGM | **S33 / S34** | 完全不涉及 |
| 时间线、EDL、片段顺序 | **S41** | 只提供片段，不决定它在整集里的位置 |
| 实际裁切执行、编码、混音 | **S42** | 只产出 `VideoEditPlan`，不执行 |
| 媒体 bytes、hash | **S28** | 只存 `artifact_ref` |
| 当前接受指针 / `node_epoch` / `execution_fence` | **S08** | 只发事件 |
| 金额、reservation | **S23** | 只传 `budget_scope_id` |
| permit | **S22** | 不持有 |
| `GenerationAttempt` / `ProviderJob` / `SUBMISSION_UNKNOWN` | **S26** | 只持引用 |
| 能力、价格、路由 | **S24 / S25** | 只消费 `RouteDecision` |
| QC 报告、缺陷、coverage | **S35 / S36 / S37** | 不自造 |
| 修复决策 | **S39** | 只执行被指派的那一格 |
| 人工裁决 | **S40** | 不写 |

### 1.3 本服务不直连供应商（强制条款）

**本服务不直连供应商。** 不持有供应商凭证，不出供应商域名，不配置供应商 SDK。

S30 的每一次付费生成走同一链路：

```
S30 组 GenerationCommand(modality=video)
  → POST /generation-jobs           (S26)
      → S23 reserve（按 quote_upper_bound_micro，不是 expected_cost_micro）
      → S22 permit（provider/channel/region 由 route_decision 定死）
      → SubmissionIntent + Outbox
      → S27 claim dispatch grant → 付费提交
  → S26 观测 → S28 归档 → production.video.ready 回 S30
```

强制手段与 S29 相同（[S29 §1.3](S29-image-production.md)）：凭证隔离、`require_dispatch_grant=true` 编译期常量、唯一付费入口是 `POST /generation-jobs`。

**S30 不参与准入 Saga 的内部步骤**，不持有 reservation id / permit id / dispatch_id。

### 1.4 动作归属（对照 S35 动作族）

视频是一等公民——S35 的多数动作族都以视频为执行对象。S30 的关系如下：

| 动作 | 执行者 | S30 的角色 |
|---|---|---|
| `regenerate_video` | **S30（意图提供者）→ S26（执行）** | **意图提供者，不是执行者**。S30 创建新的 `VideoJob`，由 S26 付费。S30 不调用任何模型 |
| `keyframe_edit` | **S29** | 不参与。若关键帧变了，S29 发 `production.image.ready`，S30 标记候选 stale |
| `temporal_edit` | **S41** | 不参与。时间线编辑不动视频片段本身 |
| `reingest` / `transcode` | **S28 / S42** | 不参与。容器问题不是片段问题 |
| `remix` | **S42** | 不参与 |
| `redo_lipsync` | **S32** | 不参与。口型不对时 S30 不自行重生成视频（§4.6） |
| `re_synthesize_speech` | **S31（意图）→ S26** | 不参与 |
| `realign_subtitles` / `re_render_subtitles` | **S38** | 不参与 |
| `change_spec` | **S06 / S07** | 不参与。需要改分镜/改台词时不是重生成 |
| `human_review` | **S40** | 不参与 |

**S30 与 `regenerate_video` 的准确关系（必须写清楚）：**

S30 **不是执行者**——执行者是 S26 + S27。S30 是**意图提供者**：它把 S39 的 `RepairAction(regenerate_video, ...)` 翻译成一个具体的新 `VideoJob`，带上新的 `variation_nonce`、新的 `prompt_intent`（若 S39 指定了调整）、新的 `duration_seconds`（若策略允许），然后提交 S26。

这个区分很重要，因为它决定了**谁对"这次重生成会不会超上限"负责**：
- S39 在**下发动作前**检查上限（[S39 §5.7](S39-repair-planning.md)），DENY 时不产生 `RepairAction`。
- S30 收到 `RepairAction` 后**不再检查上限**（它没有那个台账），只检查自己的本地前置条件（关键帧基线是否还存在、`budget_scope` 是否还有效）。
- S26 在 `POST /generation-jobs` 时检查 `attempt_no ≤ max_business_attempts` 与 `node_id` 费用上限（[S26 §12.1 第 5–6 条](S26-generation-jobs.md)），这是最后一道。

**三道检查，三道都必须在，但只有 S39 用的是跨 Run 累计的键。** S30 的本地检查只防"用了过期基线"这类技术问题，不承担上限职责。

**S30 明确不做的两件事：**

1. **不因 QC 失败而自动重生成。** 收到 `quality.*.completed` 且 decision=`repair` 时，S30 只记录"该候选有缺陷"。是否重生成由 S39 决策。自动重生成会绕过上限并可能无限循环（[docs/11 §3](../docs/11-quality-repair.md)：`unattended` 也不意味着可以无限重试）。
2. **不在 `lipsync` 缺陷下重生成视频。** [S35 §2.2.1](S35-technical-qc.md) 的 `lipsync` 族**只有** `redo_lipsync`，兜底 `human_review`。如果音频本身有问题，那由 `voice` 族先修。S30 收到 `redo_lipsync` 类修复时不创建 `VideoJob`。

---

## 2. 实体与关键区分

### 2.1 四层对象

```
VideoJob              一次"我要这一镜的 4 秒视频"的意图
  ├─ KeyframeBaselineRef        绑定的 S29 检查点快照
  ├─ DurationMapping            目标时长 → 档位 → 裁切
  └─ ClipCandidate              一段已归档的片段（不可变）
        └─ artifact_ref → S28

ShotVideoCheckpoint   每 shot_lineage_id 的基线指针（可变，CAS）
```

### 2.2 `VideoJob` 与 S25 的 `RouteDecision` 的关系

这是本服务最容易写错的地方。

| 问题 | 谁回答 |
|---|---|
| "这一镜需要几秒？" | **S30**（来自 S06 的 ShotSpec + S31/S37 的对白时长 + 策略） |
| "哪个供应商的哪个档位能生成？" | **S25** |
| "实际派单要写几秒？" | **S25 的 `normalized_input.duration_seconds`** |

关键约束（[S25 §5.1 注释](../plans/S25-model-routing.md)）：

```
"normalized_input": {
  "duration_seconds": 5,               // 4s 请求吸附到 5s 档，S26 必须按这个派
  "aspect_ratio": "9:16",
  ...
}
```

**S30 必须按 `normalized_input.duration_seconds` 组命令，不能按自己想要的 4 秒。** 因为：
- 报价基于 5 秒算（`quote_upper_bound_micro` 是 5 秒的价）；
- S23 按这个上界预留；
- 如果 S30 派 4 秒而报价是 5 秒，实际费用与预留不匹配，S23 的结算会出 `FX_DIFF` 式的偏差，且 E15（quota 已 mark、预算确认超时）的语义边界被模糊。

**结构性约束**：S30 的 `generation_command.input.duration_seconds` 必须**逐字段等于** `route_decision.eligible[rank].normalized_input.duration_seconds`。实现上做一条断言 + 一条测试（T30-03）。

### 2.3 `VideoJob` 与 `ClipCandidate` 的 N:1

一个 `VideoJob` 可以产出多个 `ClipCandidate`：

| 场景 | 说明 |
|---|---|
| 多 seed 并行 | 若 S21 想拿多个候选做选择。当前默认**一 job 一候选**（同 S29 U2） |
| 同一 provider_job 的不同裁切 | **不是多个候选**，是一个候选 + 一个 `VideoEditPlan`。裁切在 S42 执行，S30 只描述计划 |
| 重生成 | **新的 `VideoJob`**，不是同一 job 的第二个候选 |

**裁切不产生多个候选。** 这一点容易错：有人会想"4 秒请求生成 5 秒，裁成 4 秒，那是不是有 5 秒版和 4 秒版两个候选？"答案：**只有 5 秒的 artifact 是候选**，4 秒是 `VideoEditPlan` 描述的输出区间。原因是 4 秒版没有独立的 artifact hash，它只是 5 秒 artifact 的一个视图（[docs/01 §2](../docs/01-enterprise-architecture.md)：内容按 `service/id/revision/content_hash` 寻址）。

### 2.4 `DurationMapping` —— 三段映射必须显式

[docs/08 §3](../docs/08-agents-prompts.md) 原话："**目标 4 秒与供应商生成档位分开；剪辑裁切方案与成本在路由计划明确。**"

落成一条不可变记录：

```
DurationMapping := {
  target_duration_ms,        // 我要的：4000
  tier_duration_ms,          // 供应商给的档位：5000
  trim: {                    // 裁切计划
    mode: "none" | "head" | "tail" | "both",
    trim_head_ms, trim_tail_ms,
    resulting_duration_ms    // 5000 - 0 - 1000 = 4000
  },
  cost_basis: "tier",        // 计费按档位，不按目标
  dialogue_duration_ms,      // 来自 S31/S37，用于"不截断"校验
  resolution_source: "s25_normalized_input"
}
```

**五个字段都必须存在，缺一不可：**

| 字段 | 缺了会怎样 |
|---|---|
| `target_duration_ms` | 无法回答"用户要了几秒" |
| `tier_duration_ms` | 无法回答"实际花了多少秒的钱" |
| `trim.*` | 无法回答"从哪裁到哪"，S42 只能猜 |
| `cost_basis: "tier"` | 有人会按 target 算成本，低估 25% |
| `dialogue_duration_ms` | 无法校验"对白主动作不被截断"（§4.4） |

**`trim.mode` 的语义必须写死，否则 S42 会各自发挥：**

| mode | 含义 | 什么时候用 |
|---|---|---|
| `none` | 不裁 | `tier == target` |
| `tail` | 去掉末尾 | 最常见：生成的镜头末尾多出停顿 |
| `head` | 去掉开头 | 生成的首帧不是可靠起点时（少用） |
| `both` | 两头都去 | 档位远大于目标时 |

**`both` 是危险模式**：它同时改变起点和终点，使得"对白起点在片段内的偏移"变化两次，S37 的音画对齐需要重新计算。默认策略禁止 `both`，只有 `policy.allow_both_trim=true` 时允许。

### 2.5 `KeyframeBaselineRef` —— 为什么要存 `checkpoint_seq`

S30 从 S29 拿关键帧时，拿的是"当前检查点指向的候选"。但检查点会推进（S29 可能在你生成视频期间做了 `keyframe_edit`）。

**如果不记录 `checkpoint_seq`：**

- 视频生成完成时，S30 不知道它用的是哪一版关键帧；
- 无法判断"这个片段是不是基于过时关键帧生成的"；
- 下游 S35/S36 的 QC 报告引用的是视频 hash，但视频的首帧与当前检查点不符，产生"看起来都对但实际不一致"的经典 bug。

**所以 `VideoJob` 必须冻结 `(candidate_id, checkpoint_seq, content_hash)` 三元组。** 生成完成后做一次比对：

```
if job.keyframe_baseline.checkpoint_seq < current_checkpoint_seq:
    candidate.stale_basis = true
    发 production.video.ready 时带 requires_recheck = true
    但**不自动重生成** —— 由 S39 决定
```

**S30 不自己重新生成"基于最新关键帧的版本"。** 重生成是 S39 的决策。S30 只把"基线已漂移"这个事实写进候选。

### 2.6 `ClipCandidate` 与 `ShotVideoCheckpoint`

与 S29 完全同构（[S29 §2.3](S29-image-production.md)）：

- `ClipCandidate` 不可变，`state ∈ {READY, ACCEPTED, REJECTED}`。
- `ShotVideoCheckpoint` 可变，CAS 推进，只允许指向 `READY`/`ACCEPTED` 的候选。
- 检查点推进 ≠ S08 接受。

**一个 S30 特有的问题：拆镜。**

`service.yaml` 的 recovery 说"长运镜可拆镜但需导演计划"。拆镜意味着一个 `shot_lineage_id` 有**多个**片段（`segment_no = 1..N`）。所以：

- `ShotVideoCheckpoint` 的主键是 `(tenant_id, shot_lineage_id, segment_no)`，不是只 `shot_lineage_id`。
- 一个"完整的镜头视频"= 该 lineage 下所有 `segment_no` 的候选按序拼接，拼接是 **S41 的事**，不是 S30。
- S30 只保证"每个 segment 都有一个可用候选"。

**拆镜不改变 `shot_lineage_id`。** 这是跨 Run 计数的关键：拆成 3 段仍然是一个镜头的 3 次 attempt，不是 3 个镜头。如果拆镜时给每段新的 lineage_id，[S39 §5.2](S39-repair-planning.md) 的"每镜 3 次"会失效。

---

## 3. 状态机

### 3.1 `VideoJob` 状态

```
CREATED → PREPARING → SUBMITTED → WAITING_PROVIDER → INGESTING → READY
                  ↘            ↘                 ↘
                   REJECTED_LOCAL  FAILED         WAITING_UNKNOWN
                                                        ↓
                                            RECONCILED_READY / RECONCILED_FAILED
任意非终态 → CANCELED
```

与 S29 的差别只有一处：**`PREPARING` 多一步"时长映射解析"**。

| 从 | 到 | 触发 | 前置条件 | 副作用 |
|---|---|---|---|---|
| — | `CREATED` | `POST /video-jobs` | 校验通过 + 幂等未命中 | 写 job；冻结 `KeyframeBaselineRef` |
| `CREATED` | `PREPARING` | worker 领取 | 首帧候选在 S28 可读 | 取/复用 RouteDecision |
| `PREPARING` | `REJECTED_LOCAL` | `feasible=false` / 关键帧缺失 / 时长映射失败 | — | 不产生费用 |
| `PREPARING` | `SUBMITTED` | S26 返回 202 | `normalized_input.duration_seconds` 一致 + `quote_valid_to > now()` | 存 `provider_job_id` / `attempt_id` |
| `SUBMITTED` | `WAITING_PROVIDER` | S26 → `QUEUED`/`RUNNING` | — | — |
| `SUBMITTED` | `FAILED` | S26 → `PROVIDER_FAILED` | 无重试预算 | — |
| `SUBMITTED` | `WAITING_UNKNOWN` | S26 → `SUBMISSION_UNKNOWN` | — | 冻结（§4.5） |
| `WAITING_PROVIDER` | `INGESTING` | S26 → `PROVIDER_SUCCEEDED` | — | — |
| `WAITING_PROVIDER` | `FAILED` | S26 → `PROVIDER_FAILED`/`CANCELED` | — | — |
| `INGESTING` | `READY` | `artifact.ingested` 且 hash 一致 且 **`observed_duration` 已知** | 时长映射可完成 | 写 `ClipCandidate` + `DurationMapping` + `VideoEditPlan` |
| `INGESTING` | `INGESTING` | S28 失败 / 未到 | 重试预算未尽 | 只重归档 |
| `INGESTING` | `FAILED` | 重试耗尽 / hash 不符 / **`observed_duration` 与 `tier_duration_ms` 不符** | — | `DURATION_MISMATCH` |
| `WAITING_UNKNOWN` | `RECONCILED_READY` | S26 → `ARCHIVED` | — | 同 `INGESTING→READY` |
| `WAITING_UNKNOWN` | `RECONCILED_FAILED` | S26 → 确认未收单且放弃 | — | 允许 S39 决定新 attempt |
| 任意非终态 | `CANCELED` | 显式取消 | — | 调 S26 cancel；不承诺退款 |

### 3.2 `INGESTING → READY` 的额外前置：`observed_duration`

**这是 S30 特有的硬校验**：归档完成时必须知道视频的实际时长，用于：

1. 校验它等于 `tier_duration_ms`（否则供应商行为与报价不符，是 `DURATION_MISMATCH`）；
2. 计算 `trim` 区间是否有效（`trim_head + trim_tail ≤ observed_duration`）；
3. 回答"这段视频到底几秒"。

**`observed_duration` 从哪来？** 三个可能源，优先级从高到低：

| 源 | 可靠性 | 何时可用 |
|---|---|---|
| S28 归档时的容器探测（ffprobe 类） | 高 | 归档时即可 |
| S26 观测到的供应商输出元数据 | 中（供应商可能报错） | `PROVIDER_SUCCEEDED` 时 |
| S35 的技术检查报告 | 高（但慢） | QC 之后 |

**S30 的实现要求 S28 在 `artifact.ingested` 事件里带 `observed_duration_ms` 与 `frame_rate`。** 如果 S28 不带，S30 必须等 S35 的技术检查——但那会让 `READY` 延迟到 QC 之后，与"及时归档"（[docs/07 §1 步骤 7](../docs/07-production-workflows.md)）冲突。

**这是一个契约缺口（C2，见 §12）**：`artifact.ingested` 的 payload 在 `event-topics.v2.json` 里没有 schema，`docs/09 §3` 也没说 S28 要报时长。本文按"需要 S28 报"实现，并在缺口里记录。

### 3.3 状态持久化

同 S29 §3.3：本地事务 + 乐观并发 + 同事务 Outbox + 单调 `state_seq`。

---

## 4. 关键流程

### 4.1 正常路径

```
1. POST /video-jobs
     {context, shot_ref, shot_lineage_id, segment_no,
      keyframe_baseline: {candidate_id, checkpoint_seq} | null,
      prompt_intent, target_duration_ms, duration_policy,
      aspect_ratio, resolution, audio_strategy, required_capabilities,
      allow_trim, route_policy_ref | route_decision_id,
      budget_scope_id, deadline, command_id, request_hash}

2. 校验（顺序固定，失败即返回）：
   a. context 必填项
   b. 幂等键
   c. 跨租户：keyframe candidate 属于本租户（查 S29 只读）
   d. keyframe_baseline 与 S29 当前检查点兼容（不要求相等，但 checkpoint_seq 必须存在）
   e. target_duration_ms > 0
   f. duration_policy ∈ {native_only, allow_tier_snap, allow_tier_snap_with_trim}
   g. audio_strategy 与本镜是否有对白一致（§4.4）
   h. required_capabilities 非空
   i. deadline > now()

3. PREPARING：取 RouteDecision。若 feasible=false → REJECTED_LOCAL

4. 时长映射解析（§4.2）。若无法映射 → REJECTED_LOCAL(DURATION_UNMAPPABLE)

5. SUBMITTED：组命令，duration_seconds = normalized_input.duration_seconds

6. 等 S26/S28；INGESTING→READY 时写候选 + 映射 + 编辑计划

7. 检查点 CAS；若基线已漂移，候选标 stale_basis

8. outbox production.video.ready
```

### 4.2 时长映射的三个分支

```
target = 4000ms
S25 返回 normalized_input.duration_seconds = 5000（吸附到 5s 档）

分支 A：duration_policy = native_only
  → 只要有档位 ≠ target 就 REJECTED_LOCAL(DURATION_UNSUPPORTED_NATIVE_ONLY)
  → 适用：导演明确要求"必须原生 4 秒，不要裁切"

分支 B：duration_policy = allow_tier_snap
  → 接受 5000ms 的产出，不做裁切
  → 输出片段是 5 秒，下游 S41 在时间线上只用 4 秒
  → DurationMapping.trim = {mode: "none"}

分支 C：duration_policy = allow_tier_snap_with_trim
  → 接受 5000ms 的产出，并在 VideoEditPlan 里描述裁到 4000ms
  → trim 的分配由 §4.3 决定
```

**三条分支的成本差异必须在报价里体现。** [docs/08 §3](../docs/08-agents-prompts.md)："剪辑裁切方案与成本在路由计划明确。" 这句话的落点是：

- 分支 A：`infeasible`（如果无原生 4s 档），成本 0。
- 分支 B：按 5 秒计费，但下游要多用 1 秒的存储与后续处理。
- 分支 C：按 5 秒计费 + 裁切不额外计费（裁切是 S42 的 CPU 成本，进 `expected_cost_breakdown` 的 `ingest`/后期项）。

**S30 不自己算成本。** 它把 `duration_policy` 传给 S25（通过 `route_policy_ref` 或 requirement），由 S25 决定 `feasible` 与 `quote`。S30 只记录 S25 返回的 `normalized_input` 与 `quote_upper_bound_micro`。

**当前契约缺口（C1，见 §12）**：`generation-command.v2` 的 `video.input` 只有 `allow_trim: boolean`，**没有 `duration_policy` 的三值枚举**。本文用 `allow_trim` 表达分支 A vs (B∪C)：

| `duration_policy` | `allow_trim` | 需要额外表达 |
|---|---|---|
| `native_only` | — | 无法表达（`allow_trim` 是 bool） |
| `allow_tier_snap` | `false` | 可表达 |
| `allow_tier_snap_with_trim` | `true` | 可表达 |

所以 `native_only` 只能靠 S25 的 `route_policy_ref` 表达。**这是一个必须补的契约缺口**：`allow_trim` 是一个 bool 装着一个三态语义。

### 4.3 裁切区间怎么定（`VideoEditPlan`）

裁切不是任意选一段，必须满足四个约束：

```
给定:
  generated = [0, tier_ms]          // 供应商产出
  target    = [0, target_ms]        // 目标时长
  dialogue  = [d_start, d_end]      // 对白区间（相对生成片段，来自 S31 + S37）
  action    = [a_start, a_end]      // 主动作区间（来自 S06 的节拍，或 S36 检测）

裁切后必须满足:
  C1: trim.length == target_ms
  C2: dialogue ⊆ [trim_head, trim_head + target_ms]   // 对白完整包含
  C3: action ⊆ [trim_head, trim_head + target_ms]     // 主动作完整包含
  C4: trim_head ≥ 0 且 trim_head + target_ms ≤ tier_ms
```

**如果 C2 或 C3 无法满足 → `REJECTED_LOCAL(DIALOGUE_WOULD_BE_TRUNCATED)` / `ACTION_WOULD_BE_TRUNCATED`。**

**绝不允许"裁掉对白的一部分"来凑 4 秒。** 这是 [docs/16 E04](../docs/16-migration-acceptance.md) 与 [S25 §2](../plans/S25-model-routing.md) 的同一条原则：

> "对白超过 4 秒 → 澄清或不可行，不猜目标、不删台词"

S30 在裁切阶段的落点是：即使 S25 已经通过了 `dialogue_duration_ms` 的硬过滤（`DIALOGUE_EXCEEDS_DURATION`），裁切仍可能因为**对白在生成片段里的位置**而失败。例如：

```
生成 5 秒，对白在 [3000, 4800]（1.8 秒台词，但出现在片段后段）
目标 4 秒
C2 要求 [3000, 4800] ⊆ [trim_head, trim_head+4000]
  → trim_head ≤ 3000 且 trim_head ≥ 800
  → 可取 trim_head = 800 → [800, 4800]
C1 满足，C2 满足 → 可以裁
```

但如果对白在 `[100, 1700]`，而生成片段尾部有 2 秒无关内容：

```
C2: [100,1700] ⊆ [trim_head, trim_head+4000]
  → trim_head ≤ 100 且 trim_head ≥ -2300
  → 取 trim_head = 0 → [0,4000]，对白完整
C1 满足 → 可以裁（裁掉尾部 1 秒）
```

再如果对白在 `[4200, 5000]`（超出目标时长）：

```
C2: [4200,5000] ⊆ [trim_head, trim_head+4000]
  → trim_head ≤ 4200 且 trim_head ≥ 1000
  → 取 trim_head=1000 → [1000,5000]，对白完整
C1 满足 → 可以裁（裁掉头部 1 秒）
```

但 `head` 裁切通常不可接受（起点变得突兀），所以**默认策略禁止 `trim.mode = head`**。此时只能是：

```
trim_head = 0 → [0,4000]，对白 [4200,5000] 被截断 → C2 失败
→ REJECTED_LOCAL(DIALOGUE_WOULD_BE_TRUNCATED)
→ 交 S39 / S21 决定：拆镜、换档位、还是改台词
```

**这个判断在 `PREPARING` 阶段就能做（不需要等生成），前提是对白在生成片段里的位置已知。** 但对白位置依赖于生成结果，所以实际是：

- `PREPARING` 阶段：只能校验 `dialogue_duration_ms ≤ target_ms`（S25 已做）。
- `READY` 阶段（拿到 `observed_duration` 后）：用 S37 的对齐结果算 `dialogue` 区间，执行 C1–C4。

**如果 `READY` 阶段 C2 失败**，候选仍然是有效的 artifact（钱已经花了），但 `VideoEditPlan` 标 `truncates_dialogue: true`，候选标 `requires_rehuman_decision: true`。**S30 不丢弃它，也不自动重生成。**

### 4.4 `audio_strategy` 与"对白主动作不被截断"

`generation-command.v2` 的 `audio_strategy` 三值：

| 值 | 含义 | 谁提供音频 | S30 的责任 |
|---|---|---|---|
| `silent_then_tts` | 生成无音轨视频，音频后配 | **S31** | 视频必须留出对白时间窗 |
| `native_locked_voice` | 用供应商原生对白，声音已被锁定 | 供应商（但音色由 S15 锁定） | 必须声明 `required_capabilities` 含原生对白能力 |
| `non_dialogue_native` | 无对白镜头，用原生环境音 | 供应商 | 无需对白窗口 |

**S30 的校验规则（三选一必须与镜头内容一致）：**

```
shot.has_dialogue && voice_locked_by_S15      → native_locked_voice
shot.has_dialogue && !voice_locked            → silent_then_tts
!shot.has_dialogue                            → non_dialogue_native
```

不一致 → `422 AUDIO_STRATEGY_INCONSISTENT`。

**为什么这条校验重要**：`service.yaml` 的 acceptance 是"对白主动作不被截断"。而对白在视频里的呈现方式取决于 `audio_strategy`：

- `silent_then_tts`：视频本身无对白，S31 的音频后期合上去。此时"对白不被截断"的意思是**视频要有足够的时长容纳 S31 的音频**，以及**口型要与音频对齐**（S32 的活）。
- `native_locked_voice`：视频自带对白。此时"对白不被截断"的意思是**供应商原生对白必须完整**，裁切不能切到对白。

**两种情形下的 C2 约束不同：**

| `audio_strategy` | `dialogue` 区间怎么来 |
|---|---|
| `silent_then_tts` | 来自 **S31 的音频时长** + S06 的对白位置（视频里的对白"应该"在哪） |
| `native_locked_voice` | 来自 **S37 对生成视频的 ASR/对齐**（视频里的对白"实际"在哪） |
| `non_dialogue_native` | 无，C2 跳过 |

**注意第二种依赖 S37 的异步结果**：`INGESTING → READY` 时 S37 还没跑，所以 `native_locked_voice` 的 C2 只能延后到 S37 之后。S30 的处理：

1. `READY` 时对所有策略做 **C3（主动作不截断）**，主动作区间来自 S06 的节拍（不需要 S37）。
2. 对 `silent_then_tts`，C2 用 S31 的音频时长（S31 在 S30 之前完成，[docs/07 §1 步骤 5](../docs/07-production-workflows.md)："S31 配音与 S37 字级对齐确定对白长度，S29 准备关键帧"）。
3. 对 `native_locked_voice`，C2 **延后**：S30 发布 `production.video.ready` 时带 `dialogue_window_unverified: true`，由 S39 在复检时用 S37 的结果判定。

**这是本服务唯一允许"带未验证约束就发 READY"的情形**，必须显式标记，不能静默。

### 4.5 `SUBMISSION_UNKNOWN` 的应对

与 S29 同构（[S29 §4.3](S29-image-production.md)），补充 S30 特有的两点：

| 必须 | S30 特有的理由 |
|---|---|
| 不新建同 fingerprint 的 VideoJob | `video_job_open_uniq` 唯一索引挡 |
| 不重生成 | 视频单价高，重提代价远大于图像 |
| **不因为 `deadline` 逼近而重提** | [S26 §7.2 第 6 条](S26-generation-jobs.md)。长视频生成耗时长，deadline 压力最大，这条最容易被违反 |
| **不拆镜重试** | 拆镜是"新请求"，同样可能第二次付费。`docs/09 §4` 的降级链只在**确定的失败**之后启动 |
| 不推进检查点 | 没结果 |

**S30 特有的一条：不把 UNKNOWN 的 job 的 `KeyframeBaselineRef` 释放或更新。** 若 S29 在此期间推进了检查点，UNKNOWN 的 job 恢复后产出的片段仍基于旧基线，会被标记 `stale_basis`——这是正确的，因为重提会二次付费。

### 4.6 口型缺陷时的处理（跨族越权的防线）

收到 S39 的 `RepairAction(redo_lipsync)` 时，**S30 不创建 VideoJob**。

收到 `RepairAction(regenerate_video)` 且缺陷类型是 `lipsync` 时——**这种情况不应该发生**，因为 [S35 §2.2.1](S35-technical-qc.md) 的 `lipsync` 族只有 `redo_lipsync`。如果真收到，S30 返回 `422 ACTION_TYPE_MISMATCH`，不执行。

**S30 不为 `voice` 缺陷重生成视频，除非 S39 显式下发了 `regenerate_video` 作为族的第二步。** `voice` 族是 `re_synthesize_speech → regenerate_video`，第二步才是重生成视频。S30 只看动作，不看缺陷类型——但会因为动作类型正确而执行。这符合设计。

### 4.7 失败矩阵

| # | 场景 | 检测点 | S30 动作 | 状态 | 花钱 | 计数 |
|---|---|---|---|---|---|---|
| F1 | 首帧候选不可读 | PREPARING | 拒绝 | `REJECTED_LOCAL` | 否 | 不 +1 |
| F2 | 跨租户 candidate | 校验 c | **404** | 不建 job | 否 | 不 +1 |
| F3 | S25 `feasible=false`（含 `DIALOGUE_EXCEEDS_DURATION`） | PREPARING | 透传 `infeasible_reason` + `degradation_options` | `REJECTED_LOCAL` | 否 | 不 +1 |
| F4 | `duration_policy=native_only` 但无原生档 | PREPARING | `DURATION_UNSUPPORTED_NATIVE_ONLY` | `REJECTED_LOCAL` | 否 | 不 +1 |
| F5 | `normalized_input.duration_seconds` 与提交值不符 | SUBMITTED 前断言 | **内部错误，拒绝提交** | `FAILED(SPEC_ASSERTION)` | 否 | 不 +1 |
| F6 | 报价过期 | SUBMITTED 前 | 重新取 RouteDecision | `PREPARING` | 否 | 不 +1 |
| F7 | 预算不足 | S26 `REJECTED_BUDGET` | 透传；**不降分辨率、不缩时长** | `FAILED(BUDGET)` | 否 | 不 +1 |
| F8 | 配额拒绝 | S26 `REJECTED_QUOTA` | 透传 | `FAILED(QUOTA)` | 否 | 不 +1 |
| F9 | 供应商明确拒绝 | S26 `PROVIDER_FAILED(rejected)` | 记录 | `FAILED` | **是** | **+1** |
| F10 | 提交未知 | S26 `SUBMISSION_UNKNOWN` | 冻结 | `WAITING_UNKNOWN` | 未知 | 冻结期不新增 |
| F11 | 运行中失败 | S26 `PROVIDER_FAILED` | 记录 | `FAILED` | 是 | +1 |
| F12 | 归档 URL 过期 | INGESTING | 向 S26 要新 URL | `INGESTING` | 否 | 不 +1 |
| F13 | 归档 hash 不符 | INGESTING | 终态失败 | `FAILED(HASH_MISMATCH)` | 是 | 不 +1 |
| F14 | `observed_duration ≠ tier_duration`（差 > 100ms） | INGESTING→READY | `DURATION_MISMATCH`，候选**不标 READY** | `FAILED` | 是 | 不 +1 |
| F15 | 裁切 C2 失败（对白会截断） | READY | 候选可用但标 `truncates_dialogue` + `requires_rehuman_decision` | `READY` | 是 | 不 +1 |
| F16 | 裁切 C3 失败（主动作会截断） | READY | 同 F15，标 `truncates_action` | `READY` | 是 | 不 +1 |
| F17 | 基线漂移（S29 检查点已前进） | READY | 候选标 `stale_basis` + `requires_recheck` | `READY` | 是 | 不 +1 |
| F18 | 收到 `lipsync` 类的 `regenerate_video` | 入口 | `422 ACTION_TYPE_MISMATCH` | 不建 job | 否 | 不 +1 |
| F19 | 重启 | 启动扫描 | 从 S26 拉权威状态 | — | 否 | 不 +1 |

**F14 的阈值**：允许 ±100ms 的容器封装误差（B 帧、音频 priming 等）。超过即视为供应商未按档位交付，这是**必须告警**的供应商行为偏差。

---

## 5. 数据模型

### 5.1 表清单

| 表 | 作用 | 可变性 |
|---|---|---|
| `video_job` | 生产意图 | 状态可变 |
| `keyframe_baseline_ref` | 引用的 S29 检查点快照 | 不可变 |
| `duration_mapping` | 三段映射 | 不可变 |
| `video_edit_plan` | 裁切/变速计划 | 不可变（修订追加） |
| `clip_candidate` | 产出片段 | 不可变 |
| `shot_video_checkpoint` | 基线指针 | 可变（CAS） |
| `video_intent_ledger` | 审计台账 | 只追加 |
| `command_receipt` / `outbox` / `inbox` / `job_transition` | 基础设施 | 只追加 |

### 5.2 核心 DDL

```sql
CREATE SCHEMA IF NOT EXISTS video_production;

CREATE TABLE video_production.video_job (
  video_job_id         text PRIMARY KEY,
  tenant_id            text NOT NULL,
  project_id           text NOT NULL,
  series_id            text NOT NULL,
  episode_id           text NOT NULL,
  branch_id            text NOT NULL,
  shot_ref             jsonb NOT NULL,
  shot_lineage_id      text NOT NULL,
  segment_no           integer NOT NULL DEFAULT 1 CHECK (segment_no >= 1),
  node_id              text NOT NULL,
  node_epoch_at_create integer NOT NULL,
  execution_fence      integer NOT NULL,

  prompt_intent        text NOT NULL,
  target_duration_ms   integer NOT NULL CHECK (target_duration_ms > 0),
  duration_policy      text NOT NULL
    CHECK (duration_policy IN ('native_only','allow_tier_snap','allow_tier_snap_with_trim')),
  aspect_ratio         text NOT NULL,
  resolution           jsonb NOT NULL,     -- {width,height}
  audio_strategy       text NOT NULL
    CHECK (audio_strategy IN ('silent_then_tts','native_locked_voice','non_dialogue_native')),
  required_capabilities text[] NOT NULL,
  allow_trim           boolean NOT NULL,

  keyframe_baseline_id text,               -- FK 下方；null = 无首帧（文生视频）
  reference_combination_hash text NOT NULL,

  input_fingerprint    text NOT NULL,
  request_hash         text NOT NULL,
  idempotency_key      text NOT NULL,
  route_decision_ref   jsonb,
  budget_scope_id      text NOT NULL,
  deadline             timestamptz NOT NULL,

  provider_job_id      text,
  attempt_id           text,
  business_attempt_no  integer NOT NULL DEFAULT 1,
  submitted_duration_ms integer,           -- = normalized_input.duration_seconds * 1000
  quote_upper_bound_micro bigint,          -- 审计用，S23 才是权威

  state                text NOT NULL,
  state_seq            bigint NOT NULL DEFAULT 0,
  failure_code         text,
  failure_detail       jsonb,
  reconciled           boolean NOT NULL DEFAULT false,

  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  state_entered_at     timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT vj_state_check CHECK (state IN (
    'CREATED','PREPARING','REJECTED_LOCAL','SUBMITTED','WAITING_PROVIDER',
    'WAITING_UNKNOWN','INGESTING','READY','FAILED','RECONCILED_READY',
    'RECONCILED_FAILED','CANCELED')),
  CONSTRAINT vj_tenant_idem UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT vj_submitted_dur_positive CHECK (
    submitted_duration_ms IS NULL OR submitted_duration_ms > 0)
);

CREATE UNIQUE INDEX video_job_open_uniq
  ON video_production.video_job
     (tenant_id, shot_lineage_id, segment_no, input_fingerprint)
  WHERE state NOT IN ('READY','REJECTED_LOCAL','FAILED','RECONCILED_FAILED','CANCELED');

CREATE INDEX video_job_shot_idx
  ON video_production.video_job (tenant_id, shot_lineage_id, segment_no, created_at DESC);
CREATE INDEX video_job_state_idx
  ON video_production.video_job (state, state_entered_at)
  WHERE state IN ('PREPARING','SUBMITTED','WAITING_UNKNOWN','INGESTING');

-- ---------- 关键帧基线（不可变） ----------
CREATE TABLE video_production.keyframe_baseline_ref (
  keyframe_baseline_id text PRIMARY KEY,
  tenant_id            text NOT NULL,
  candidate_id         text NOT NULL,        -- S29
  checkpoint_seq       bigint NOT NULL,
  artifact_ref         jsonb NOT NULL,       -- S28
  content_hash         text NOT NULL,
  captured_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT kbr_artifact_is_s28 CHECK ((artifact_ref->>'service_id') = 'S28'),
  CONSTRAINT kbr_hash_uniq UNIQUE (tenant_id, candidate_id, checkpoint_seq)
);

-- ---------- 时长映射（不可变） ----------
CREATE TABLE video_production.duration_mapping (
  duration_mapping_id   text PRIMARY KEY,
  tenant_id             text NOT NULL,
  video_job_id          text NOT NULL REFERENCES video_production.video_job,
  target_duration_ms    integer NOT NULL CHECK (target_duration_ms > 0),
  tier_duration_ms      integer NOT NULL CHECK (tier_duration_ms > 0),
  trim_mode             text NOT NULL CHECK (trim_mode IN ('none','head','tail','both')),
  trim_head_ms          integer NOT NULL DEFAULT 0 CHECK (trim_head_ms >= 0),
  trim_tail_ms          integer NOT NULL DEFAULT 0 CHECK (trim_tail_ms >= 0),
  resulting_duration_ms integer NOT NULL CHECK (resulting_duration_ms > 0),
  cost_basis            text NOT NULL DEFAULT 'tier' CHECK (cost_basis = 'tier'),
  dialogue_duration_ms  integer,
  action_window         jsonb,              -- {start_ms,end_ms} | null
  created_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT dm_math CHECK (
    resulting_duration_ms = tier_duration_ms - trim_head_ms - trim_tail_ms),
  CONSTRAINT dm_target_match CHECK (resulting_duration_ms = target_duration_ms),
  CONSTRAINT dm_both_forbidden_default CHECK (
    trim_mode <> 'both')       -- 需要显式策略时由应用层先记录 policy 再放宽
);
CREATE UNIQUE INDEX dm_job_uniq ON video_production.duration_mapping (video_job_id);

-- ---------- 编辑计划（裁切意图，不是执行） ----------
CREATE TABLE video_production.video_edit_plan (
  edit_plan_id          text PRIMARY KEY,
  tenant_id             text NOT NULL,
  clip_candidate_id     text NOT NULL,
  revision              integer NOT NULL DEFAULT 1,
  trim                  jsonb NOT NULL,     -- {head_ms, tail_ms, resulting_duration_ms}
  speed                 jsonb,              -- {factor, curve} | null，默认 null
  first_frame_handling  text NOT NULL DEFAULT 'keep'
    CHECK (first_frame_handling IN ('keep','trim_to_next_iframe')),
  last_frame_handling   text NOT NULL DEFAULT 'keep',
  truncates_dialogue    boolean NOT NULL DEFAULT false,
  truncates_action      boolean NOT NULL DEFAULT false,
  executed_by           text,              -- 必须是 'S42'
  executed_at           timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT vep_executor CHECK (executed_by IS NULL OR executed_by = 'S42'),
  CONSTRAINT vep_rev_uniq UNIQUE (clip_candidate_id, revision)
);

-- ---------- 候选（不可变） ----------
CREATE TABLE video_production.clip_candidate (
  clip_candidate_id     text PRIMARY KEY,
  tenant_id             text NOT NULL,
  video_job_id          text NOT NULL REFERENCES video_production.video_job,
  shot_lineage_id       text NOT NULL,
  segment_no            integer NOT NULL,
  artifact_ref          jsonb NOT NULL,
  observed_duration_ms  integer NOT NULL CHECK (observed_duration_ms > 0),
  frame_rate            numeric(8,3) NOT NULL,
  duration_mapping_id   text NOT NULL REFERENCES video_production.duration_mapping,
  edit_plan_id          text REFERENCES video_production.video_edit_plan,
  keyframe_baseline_id  text REFERENCES video_production.keyframe_baseline_ref,
  provider_job_id       text NOT NULL,
  attempt_id            text NOT NULL,

  stale_basis            boolean NOT NULL DEFAULT false,
  truncates_dialogue     boolean NOT NULL DEFAULT false,
  truncates_action       boolean NOT NULL DEFAULT false,
  dialogue_window_unverified boolean NOT NULL DEFAULT false,
  requires_recheck       boolean NOT NULL DEFAULT false,
  requires_rehuman_decision boolean NOT NULL DEFAULT false,
  repair_action_id       text,
  strategy_class         text,

  state                  text NOT NULL CHECK (state IN ('READY','ACCEPTED','REJECTED')),
  created_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT cc_artifact_is_s28 CHECK ((artifact_ref->>'service_id') = 'S28')
);

CREATE UNIQUE INDEX cc_artifact_uniq
  ON video_production.clip_candidate
     (tenant_id, (artifact_ref->>'id'), (artifact_ref->>'content_hash'));
CREATE INDEX cc_shot_idx
  ON video_production.clip_candidate
     (tenant_id, shot_lineage_id, segment_no, created_at DESC);
CREATE INDEX cc_needs_attention_idx
  ON video_production.clip_candidate (tenant_id, created_at)
  WHERE requires_recheck OR requires_rehuman_decision
     OR truncates_dialogue OR truncates_action OR stale_basis;

-- ---------- 镜头视频检查点 ----------
CREATE TABLE video_production.shot_video_checkpoint (
  tenant_id        text NOT NULL,
  shot_lineage_id  text NOT NULL,
  segment_no       integer NOT NULL,
  clip_candidate_id text NOT NULL REFERENCES video_production.clip_candidate,
  checkpoint_seq   bigint NOT NULL,
  expected_revision integer NOT NULL DEFAULT 0,
  updated_by_job   text NOT NULL,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, shot_lineage_id, segment_no)
);

-- ---------- 审计台账 ----------
CREATE TABLE video_production.video_intent_ledger (
  entry_id         uuid PRIMARY KEY,
  tenant_id        text NOT NULL,
  video_job_id     text NOT NULL,
  attempt_id       text,
  provider_job_id  text,
  shot_lineage_id  text NOT NULL,
  segment_no       integer NOT NULL,
  action           text NOT NULL,      -- generate | regenerate_video | split_shot_regenerate
  strategy_class   text,
  repair_action_id text,
  run_id           text NOT NULL,
  node_epoch       integer NOT NULL,
  manifest_id      text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX vil_shot_idx
  ON video_production.video_intent_ledger (tenant_id, shot_lineage_id, created_at DESC);

-- ---------- 基础设施（与 S29 §5.2 同构） ----------
CREATE TABLE video_production.command_receipt (
  tenant_id     text NOT NULL,
  command_id    text NOT NULL,
  request_hash  char(64) NOT NULL,          -- 裸 hex，与 contracts schema 一致
  response_body jsonb NOT NULL,
  completed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE video_production.outbox (
  outbox_id    uuid PRIMARY KEY,
  tenant_id    text NOT NULL,               -- 事件信封 required；也用于中继分区
  aggregate_id text NOT NULL,
  aggregate_sequence bigint NOT NULL,
  event_type   text NOT NULL,
  partition_key text NOT NULL,              -- tenant_id || ':' || aggregate_id
  payload      jsonb NOT NULL,
  headers      jsonb NOT NULL,
  published_at timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);

CREATE INDEX outbox_unpublished_idx ON video_production.outbox (id) WHERE published_at IS NULL;

CREATE TABLE video_production.inbox (
  consumer     text NOT NULL,
  event_id     text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  PRIMARY KEY (consumer, event_id)
);
```

**中继**：与 S08 §6.4 同一形状 —— `SELECT ... WHERE published_at IS NULL ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED`，
同一 `partition_key` 单线程发布；失败重试不阻塞其他 partition；`published_at + 7d` 归档。
重启无需恢复逻辑，未发布的行走同一条路径。

**为什么 `production.video.ready` 必须同事务**：视频就绪与「S41/S08 能看见它就绪」若拆成两步，
中间崩溃会留下「字节已归档、无人知道」的孤儿剪辑，或「事件已发、候选行回滚」的幽灵剪辑。
两种都以 S08 的接受指针为准绳重查即可发现，但都会让人以为片子好了。

### 5.3 约束理由

| 约束 | 防的是什么 |
|---|---|
| `dm_math` | 三段映射自洽：`resulting = tier - head - tail` |
| `dm_target_match` | `resulting == target` —— 否则"裁了还是不对" |
| `dm_both_forbidden_default` | §2.4 的 `both` 危险模式默认禁止 |
| `vep_executor` | `executed_by` 只能是 S42 —— 防止 S30 自己执行裁切变成第二个执行权威 |
| `video_job_open_uniq` | E16：UNKNOWN 期间另建 job |
| `cc_artifact_uniq` | 同 artifact 重复登记 |
| `cc_artifact_is_s28` | 供应商直链不能进 artifact_ref |
| `observed_duration_ms > 0` | 允许"未知"会退化成假成功 |

**`vep_executor` 这条约束值得展开**：S30 拥有 `VideoEditPlan`（计划），S42 拥有执行（渲染）。如果 S30 写了 `executed_by='S30'`，就意味着 S30 在跑 ffmpeg——这违反"本服务不产生媒体 bytes"。约束把它变成数据库层面的不可能。

---

## 6. 对外契约

### 6.1 `POST /video-jobs`

```jsonc
{
  "command_id": "cmd-vid-0001",
  "request_hash": "sha256:…",
  "context": { /* execution-context.v2 */ },
  "shot_ref": { "service_id": "S06", "id": "shot_3_2", "revision": 8 },
  "shot_lineage_id": "lin_ep3_sc3_02",
  "segment_no": 1,
  "keyframe_baseline": {
    "candidate_id": "kfc_01J…",
    "checkpoint_seq": 12
  },
  "prompt_intent": "低机位仰拍林岚，保持角色和服装，轻微推进，目标四秒。对白另行驱动。",
  "target_duration_ms": 4000,
  "duration_policy": "allow_tier_snap_with_trim",
  "aspect_ratio": "9:16",
  "resolution": { "width": 1080, "height": 1920 },
  "audio_strategy": "silent_then_tts",
  "required_capabilities": ["image_to_video", "preserve_identity"],
  "allow_trim": true,
  "route_policy_ref": { "service_id": "S25", "id": "routing_demo", "revision": 1, "content_hash": "…" },
  "budget_scope_id": "budget_ep3",
  "deadline": "2026-11-01T23:30:00+08:00"
}
```

响应 `202`：

```jsonc
{
  "video_job_id": "vj_01J…",
  "state": "CREATED",
  "operation_id": "op_…",
  "duration_mapping_preview": {
    "target_duration_ms": 4000,
    "note": "档位吸附与裁切在 PREPARING 后确定；此处仅为请求值"
  }
}
```

**注意 `duration_mapping_preview` 只是回显，不是承诺。** 真实映射要等 RouteDecision 回来。这样设计是为了防止调用方把响应里的 4000 当成"最终会生成 4 秒"。

**校验顺序：**

1. `Idempotency-Key` + `command_id`
2. `context` 必填项 + 签名绑定
3. 跨租户 → 404
4. `shot_ref.revision` 存在
5. `keyframe_baseline` 与 S29 检查点兼容（`checkpoint_seq` 存在且 ≤ 当前）
6. `target_duration_ms > 0`
7. `duration_policy` 与 `allow_trim` 一致（§4.2 表）
8. `audio_strategy` 与镜头是否有对白一致（§4.4）
9. 若 `audio_strategy = silent_then_tts`，`dialogue_duration_ms ≤ target_duration_ms`（来自 S31，若已知）
10. `required_capabilities` 非空
11. `deadline > now()`

### 6.2 `GET /video-jobs/{id}`

```jsonc
{
  "video_job_id": "vj_01J…",
  "source_revision": 5,
  "state": "READY",
  "business_attempt_no": 1,
  "provider_job_id": "pj_01J…",
  "submitted_duration_ms": 5000,
  "duration_mapping": {
    "target_duration_ms": 4000,
    "tier_duration_ms": 5000,
    "trim_mode": "tail",
    "trim_head_ms": 0,
    "trim_tail_ms": 1000,
    "resulting_duration_ms": 4000,
    "cost_basis": "tier",
    "dialogue_duration_ms": 3200
  },
  "candidates": [{
    "clip_candidate_id": "cc_01J…",
    "artifact_ref": {…},
    "observed_duration_ms": 5000,
    "frame_rate": 24.0,
    "stale_basis": false,
    "truncates_dialogue": false,
    "truncates_action": false,
    "dialogue_window_unverified": false,
    "requires_recheck": false,
    "state": "READY"
  }],
  "checkpoint": { "shot_lineage_id": "lin_…", "segment_no": 1,
                  "clip_candidate_id": "cc_01J…", "checkpoint_seq": 7 },
  "frozen": false,
  "must_reconcile_before_resubmit": false,
  "history": […]
}
```

### 6.3 其他查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/video-jobs?shot_lineage_id=&segment_no=&state=` | 列表 |
| GET | `/shots/{shot_lineage_id}/video-checkpoint?segment_no=` | 当前检查点（**S41 的主读口**） |
| GET | `/clip-candidates/{id}` | 候选详情 + 完整映射链 |
| GET | `/clip-candidates/{id}/edit-plan` | 具体裁切计划（S42 读这个） |
| POST | `/video-jobs/{id}:cancel` | 取消 |
| POST | `/video-jobs/{id}:retry` | 仅 `{FAILED, RECONCILED_FAILED}`；`WAITING_UNKNOWN` 时 409 |

### 6.4 事件

**发布：**

| event_type | aggregate | 何时 |
|---|---|---|
| `video.job.state-changed` | `video_job_id` | 每次转移 |
| `production.video.ready` | `shot_lineage_id` | 检查点推进 |
| `video.clip-attention-needed`（建议新增） | `clip_candidate_id` | 候选带 `stale_basis`/`truncates_*`/`requires_rehuman_decision` |
| `video.duration-mismatch`（建议新增） | `video_job_id` | F14 |

`production.video.ready` 的 `data`：

```jsonc
{
  "video_job_id": "vj_01J…",
  "clip_candidate_id": "cc_01J…",
  "shot_lineage_id": "lin_…",
  "segment_no": 1,
  "artifact_ref": {…},
  "observed_duration_ms": 5000,
  "frame_rate": 24.0,
  "duration_mapping": {…},
  "edit_plan_ref": {…},
  "checkpoint": { "from": null, "to": "cc_01J…", "checkpoint_seq": 7 },
  "keyframe_baseline": { "candidate_id": "kfc_…", "checkpoint_seq": 12 },
  "stale_basis": false,
  "requires_recheck": false,
  "flags": { "truncates_dialogue": false, "truncates_action": false,
             "dialogue_window_unverified": false }
}
```

**不含**媒体 URL、不含 prompt 原文、不含供应商信息。

**消费：**

| 来源 | 事件 | 用途 |
|---|---|---|
| S26 | `generation.job.state-changed` | 驱动状态机 |
| S28 | `artifact.ingested` | 完成 `INGESTING → READY`；**需要 `observed_duration_ms` 与 `frame_rate`** |
| S29 | `production.image.ready` | 标记候选 `stale_basis`（不动候选本身） |
| S31 | `production.speech.ready` | `silent_then_tts` 的对白时长更新 |
| S08 | `snapshot.manifest.committed` | 只读投影 |

**S30 不消费 `quality.*` 来自动重做。**

---

## 7. 幂等与并发

### 7.1 幂等

| 层面 | 键 | 冲突 |
|---|---|---|
| 命令 | `tenant + command_id + request_hash` | 同体返回原 job；异体 409 |
| 业务 | `(tenant, shot_lineage_id, segment_no, input_fingerprint)` 未终结 | 返回已有 job |
| 修复 | `(tenant, repair_action_id)` | 409，返回首次 job |

`request_hash` 含：`context` 业务字段、`shot_ref`、`segment_no`、`keyframe_baseline.candidate_id`、`prompt_intent`、`target_duration_ms`、`duration_policy`、`aspect_ratio`、`resolution`、`audio_strategy`、`required_capabilities`（排序后）、`allow_trim`、`budget_scope_id`、`deadline`。**不含** `trace_id`。

### 7.2 并发

| 场景 | 机制 |
|---|---|
| 同镜同 segment 两个 job 同时 READY | 检查点 CAS |
| 拆镜时多个 segment 并发 | 互不影响（不同 PK 行） |
| `production.image.ready` 与 READY 同时到 | 候选写入后再判 `stale_basis`；检查点推进独立 |
| `dialogue_duration_ms` 在 PREPARING 后被 S31 更新 | 用 `READY` 时的最新值重算裁切；映射不可变 → 新 revision？否：**映射在 `INGESTING→READY` 时一次性确定**，之后不变 |

### 7.3 重启恢复

同 S29 §7.3。补充：`INGESTING` 恢复时必须**重新确认 `observed_duration_ms`**——不能沿用崩溃前内存里的值。

---

## 8. 跨 Run 尝试计数不能清零

与 S29 §8 同构，S30 的三条特有约束：

### 8.1 `shot_lineage_id` + `segment_no` 的组合才是镜头身份

[S39 §5.2](S39-repair-planning.md) 的键是 `shot_lineage_id`，**不含 segment**。所以拆镜后：

- 3 个 segment 的 attempt **全部计入同一个 `shot_lineage_id` 的总数**。
- 这符合 [docs/11 §3](../docs/11-quality-repair.md) 的"每镜头最多 3 个自动业务 attempt"——拆镜不是逃逸通道。

**结构性约束**：`video_intent_ledger.shot_lineage_id` 跨 segment 相同；`segment_no` 只作审计列，**不进任何上限键**。

### 8.2 不因换 segment 而清零

拆镜重试时，如果 S21 给每个 segment 都发一个新 job，三次尝试看起来像"每个 segment 1 次"。若编译成"每个 segment 3 次"，总 attempt 变成 9 次，**绕过上限**。

**S30 的实现约束**：拆镜计划本身由 S21 下发（`service.yaml` recovery："长运镜可拆镜但需导演计划"）。S30 **不自行拆镜**。收到一个 `video-jobs` 请求时，`segment_no` 由调用方指定；S30 不改它，也不派生新 segment。

### 8.3 重生成时不改变 lineage

`regenerate_video` 的 `RepairAction` 里带 `shot_lineage_id`。S30 用它，不从 `shot_ref` 重新派生。若 `RepairAction` 的 lineage 与 job 的不一致 → `422 LINEAGE_MISMATCH`。

### 8.4 例子

| Run | 事件 | action | `shot_lineage_id` | `segment_no` | `strategy_class` | S39 累计 |
|---|---|---|---|---|---|---|
| Run 1 | 初始生成 | `generate` | `lin_02` | 1 | — | 起算 |
| Run 1 | 长运镜拆成 2 段 | `split_shot_regenerate` | `lin_02` | 1, 2 | — | 各 1 次，**合计 2** |
| Run 2 | 第 2 段手部缺陷 | `regenerate_video` | `lin_02` | 2 | `regenerate_video:hand` | seq=1，`shot_total=3` |
| Run 2 | 换 seed 再审 | `regenerate_video` | `lin_02` | 2 | 同上（seed 不进键） | 若已达 `max_identical+1` → DENY |

第 3 行说明拆镜已经用了 2 次额度。第 4 行说明换 seed 不换策略 → 计数继续。**S30 只负责把 `shot_lineage_id` 带对，不负责数数。**

---

## 9. 可观测

| 指标 | 类型 | 用途 | 告警 |
|---|---|---|---|
| `vid_jobs{state}` | gauge | 堆积 | `WAITING_UNKNOWN` > 0 |
| `vid_job_duration_seconds{duration_policy,resolution}` | histogram | 延迟 | p99 超 SLO |
| `vid_duration_snap_ratio{tier,target}` | histogram | 吸附比例 | 4s→5s 占比 > 80% = 档位覆盖不足 |
| `vid_trim_mode{basis}` | counter | 裁切模式分布 | `both` > 0 且策略未允许 = bug |
| `vid_duration_mismatch_total` | counter | 供应商未按档位交付 | **任何一次都告警** |
| `vid_truncates_dialogue_total` | counter | 对白会截断 | 持续 > 0 = 上游对白时长不准 |
| `vid_truncates_action_total` | counter | 主动作会截断 | 同上 |
| `vid_stale_basis_total` | counter | 基线漂移 | 突增 = S29 与 S30 节奏不匹配 |
| `vid_unknown_age_seconds` | gauge | 冻结时长 | > 1h 升级 |
| `vid_regenerate_per_shot{shot_lineage_id}` | gauge | 单镜重生成次数 | 超上限（S39 侧应已拦） |
| `vid_quote_vs_tier_drift_ratio` | gauge | 报价与实际档位偏差 | 持续 ≠ 1 = 报价 bug |
| `vid_assertion_failures_total{kind}` | counter | F5 类内部断言 | **任何一次都告警** |

### 9.2 追踪

`traceparent` 贯穿 `POST /video-jobs → route → S26 → S27 → S28 → production.video.ready → S35/S36`。

必须能回答："这段视频的目标几秒、实际几秒、裁了多少、按几秒计费、对白落在哪里、有没有被截断"。前五项 S30 的表能答，最后一项需要 S31/S37。

### 9.3 日志

不记 prompt 原文、不记供应商 URL。记 `input_fingerprint`、`duration_mapping_id`、`normalized_input` 摘要。

---

## 10. 测试清单

| 编号 | 场景 | 期望 | E |
|---|---|---|---|
| T30-01 | 同 command 重复 | 返回原 job | E12 |
| T30-02 | 同 command 异 body | 409 | E12 |
| T30-03 | 4s 请求吸附到 5s | 提交的 `duration_seconds == normalized_input.duration_seconds == 5` | E03 |
| T30-04 | 提交值 ≠ normalized | 内部断言失败，不提交（F5） | E03 |
| T30-05 | `duration_policy=native_only` 无原生档 | `REJECTED_LOCAL` | E04 |
| T30-06 | 对白 4.2s，目标 4s，S25 返回 `DIALOGUE_EXCEEDS_DURATION` | 透传 `degradation_options`，**不缩短对白** | E04 |
| T30-07 | 生成 5s，对白在 [4200,5000]，目标 4s，禁止 head trim | `REJECTED_LOCAL(DIALOGUE_WOULD_BE_TRUNCATED)` 或候选标 `truncates_dialogue` | E04 |
| T30-08 | 生成 5s，对白在 [3000,4800]，目标 4s | `trim_head=800, trim_tail=200`，C1–C4 全满足 | E24 |
| T30-09 | `dm_math` 违反（`resulting ≠ tier - head - tail`） | DB 拒绝 | E24 |
| T30-10 | `trim_mode=both` 且策略未允许 | DB 拒绝 | E24 |
| T30-11 | `observed_duration` 与 tier 差 200ms | `FAILED(DURATION_MISMATCH)` + 告警 | E23 |
| T30-12 | `observed_duration` 与 tier 差 80ms | 接受（容差内），映射按实际值 | E23 |
| T30-13 | S28 归档不带 `observed_duration` | 阻塞在 `INGESTING` 并告警；不退化成假 READY | E23 |
| T30-14 | S26 `SUBMISSION_UNKNOWN` | `WAITING_UNKNOWN`，**不发新请求** | E16 |
| T30-15 | UNKNOWN 期间同 fingerprint | 唯一约束 / 返回原 job | E16 |
| T30-16 | UNKNOWN 期间 deadline 逼近 | **不重提**，到点 `FAILED(DEADLINE)` 或保持冻结 | E16 |
| T30-17 | UNKNOWN 期间想"拆镜更快" | S30 不自行拆镜；调用方必须新下发 segment | E16 |
| T30-18 | 归档 URL 过期 | 向 S26 要新 URL，不重生成 | E23 |
| T30-19 | `production.image.ready` 在生成中途到达 | 候选标 `stale_basis=true` + `requires_recheck=true`，**不自动重生成** | E24 |
| T30-20 | `stale_basis` 候选被检查点推进 | 允许（候选可用），下游 S39 决定 | E24 |
| T30-21 | 收到 `redo_lipsync` | 不建 VideoJob | E24 |
| T30-22 | 收到 `lipsync` 类的 `regenerate_video` | `422 ACTION_TYPE_MISMATCH` | E24 |
| T30-23 | `audio_strategy` 与镜头有对白矛盾 | `422 AUDIO_STRATEGY_INCONSISTENT` | E02 |
| T30-24 | `native_locked_voice` 的 C2 延后 | `dialogue_window_unverified=true`，事件带该 flag | E24 |
| T30-25 | `vep_executor='S30'` | DB 拒绝 | E01 |
| T30-26 | 检查点并发 CAS | 一个成功一个重读 | E20 |
| T30-27 | 拆镜 3 段，S39 `shot_total` | 累计 3 次，**不清零** | E26 |
| T30-28 | 换 segment 重试 | 计入同一 `shot_lineage_id` | E26 |
| T30-29 | `RepairAction` 的 lineage 与 job 不符 | `422 LINEAGE_MISMATCH` | E26 |
| T30-30 | 重启于 `INGESTING` | 重新探测 `observed_duration` | E13 |
| T30-31 | 跨租户 candidate / artifact | 404 | E28 |
| T30-32 | 预算不足 | `FAILED(BUDGET)`，不降分辨率不缩时长 | E14 |
| T30-33 | 配额拒绝 | `FAILED(QUOTA)`，S23 已补偿 | E14 |
| T30-34 | 供应商账户撤销 | 明确失败，不换渠道 | E18 |
| T30-35 | 数据驻留禁止 | `REJECTED_LOCAL(RESIDENCY_FORBIDDEN)` | E31 |
| T30-36 | 独立升级回滚 | 状态机表驱动，新旧版本一致 | E01 |
| T30-37 | 取消不可取消任务 | `CANCEL_PENDING`，不报退款 | E22 |
| T30-38 | Kafka 重放 `production.video.ready` | 幂等消费，不触发二次生成 | E32 |

---

## 11. 实施顺序与未决

### 11.1 顺序（W3）

1. 表 + 状态机骨架 + `dm_*` 约束（T30-01/02/09/10）。
2. `POST /video-jobs` + 校验 + 幂等（T30-23）。
3. 时长映射解析（三分支）+ 与 RouteDecision 的一致性断言（T30-03/04/05/06）。
4. S25 对接（mock）（T30-06）。
5. S26 对接（mock）：预算/配额/UNKNOWN（T30-14…17、32、33）。
6. S28 对接（mock）：`observed_duration`、URL 过期、hash（T30-11…13/18）。
7. 裁切 C1–C4 + `VideoEditPlan`（T30-07/08/25）。
8. 基线漂移检测（T30-19/20）。
9. 动作族守卫（T30-21/22）。
10. 跨 Run lineage（T30-27…29）。
11. 巡检与告警。
12. 真实渠道需授权。

### 11.2 未决

| # | 问题 | 当前处理 |
|---|---|---|
| U1 | `observed_duration_ms` 由谁提供 | **阻塞**（C2）。暂按 S28 提供；若 S28 不提供，需改为"等 S35"或"用 S26 元数据（可靠性中）" |
| U2 | `duration_policy` 三值如何进契约 | **阻塞**（C1）。当前用 `allow_trim` bool + `route_policy_ref` 组合表达，语义不精确 |
| U3 | 主动作区间 `action_window` 的来源 | 当前用 S06 的节拍（ShotSpec 的 beat）。若 S06 不提供毫秒级区间，退化为只用对白约束 + S36 事后判定 |
| U4 | 拆镜由谁决策、`segment_no` 如何分配 | `service.yaml` 说"需导演计划"，即 S21/S06。S30 不自行拆 |
| U5 | `both` trim 的策略开关名 | 未定。当前在 DB 层禁止，放开需要显式 policy，名字待 S02 定义 |
| U6 | 变速（`speed`）是否属于 `VideoEditPlan` | 当前字段存在但恒 `null`。变速改变时长语义，可能与"对白不被截断"冲突，需要单独设计 |
| U7 | 一个 job 多候选（多 seed） | 同 S29 U2，暂定一 job 一候选 |
| U8 | 关键帧缺失时是否允许文生视频 | 当前允许（`keyframe_baseline_id` 可空），但 `required_capabilities` 必须相应改变。需要与 S29 C5 一起裁决 |

---

## 12. 冲突与契约缺口

### C1 `video.input.allow_trim` 是一个 bool 装三态语义

`generation-command.v2` 的 `video.input.allow_trim` 是 `boolean`。但 [docs/08 §3](../docs/08-agents-prompts.md) 的语义是三态：

- 只要原生时长（不接受吸附）→ 无法用 `allow_trim=false` 表达（`false` 只是"不裁"，但仍可能接受吸附后的 5 秒）
- 接受吸附，不裁 → `allow_trim=false`
- 接受吸附，可裁 → `allow_trim=true`

**冲突点**：`allow_trim=false` 在两种不同意图下含义不同（"原生 4 秒" vs "5 秒就 5 秒"）。

**不选边**：本文在 **S30 自己的 API** 上引入 `duration_policy` 三值，映射到契约字段时：

```
native_only             → allow_trim=false + route_policy 里声明 require_native_duration=true
allow_tier_snap         → allow_trim=false
allow_tier_snap_with_trim → allow_trim=true
```

`require_native_duration` 这个 route policy 字段**当前不存在**，属建议新增。在它落地前，`native_only` 只能靠"检查 `normalized_input.duration_seconds == target`"事后判定（F4）。

**建议**：把 `allow_trim` 改成 `duration_policy` 三值枚举，或在 `video.input` 增加 `duration_policy`。

### C2 `artifact.ingested` 没有 schema，S30 依赖它的 `observed_duration`

`event-topics.v2.json` 里 `artifact.ingested` 的 `schema_ref` 是 `event-envelope.v2.schema.json`——即 payload 没有类型约束。S30 的 `INGESTING → READY` 强依赖 `observed_duration_ms` 与 `frame_rate`。

**冲突点**：`docs/09 §3` 只说"输出下载失败只重归档"，没说 S28 要探测并报告媒体时长。S28 的 `service.yaml` 里 `acceptance` 也没有这一条。

**不选边**：本文按"S28 报"实现，并把"不报时阻塞"（T30-13）作为防御。若 S28 不报，替代方案是 S30 在归档后调 S35 的技术检查拿时长——但那会让 `READY` 依赖 QC，与"及时归档"冲突，是更差的设计。

**建议**：S28 的 `artifact.ingested` payload 增加 `{observed_duration_ms, frame_rate, container, codec}`，或在 `contracts/` 下补 `artifact-ingested.v2.schema.json`。

### C3 `docs/08 §3` 说"成本在路由计划明确"，但 RouteDecision 没有裁切成本字段

`route_decision.expected_cost_breakdown` 有 `{generation, lipsync, qc, expected_retry, ingest}`。**没有 `trim` 或 `post_processing`**。

**冲突点**：分支 C（`allow_tier_snap_with_trim`）的裁切是 S42 的算力成本，应该进 breakdown，但没有字段。

**不选边**：S30 不自己算，只记录 `cost_basis: "tier"`（生成成本按档位）。裁切成本归 S42/S23 的实际计量。若需要在报价阶段体现，需在 `expected_cost_breakdown` 加字段。

### C4 `service.yaml` 的 `depends_on` 缺 S25 / S31 / S06

S30 的 `depends_on = [S26, S28, S29]`。但本文的实际依赖：

- **S25**：`POST /generation-jobs` 强制要求 `route_decision_id`（同 S29 C7）
- **S31**：`silent_then_tts` 的对白时长（[docs/07 §1 步骤 5](../docs/07-production-workflows.md) 明确"S31 配音与 S37 字级对齐确定对白长度，S29 准备关键帧"）
- **S06**：`shot_ref` 与主动作节拍

**与 S39 C7 同类**：契约图看不出实际依赖关系，部署顺序与故障域分析会漏。

**不选边**：本文按实际需要实现，但保持 `service.yaml` 不变（生成物不能手改，[AGENTS.md](../AGENTS.md)："改内容请改 catalog 再重新生成"）。缺口在此记录。

### C5 `docs/11 §2` 把"视频手部/道具异常"的优先修复写成"有时序能力的局部编辑或重生成"，与 S35 的族不一致

[docs/11 §2](../docs/11-quality-repair.md)：

> 视频手部/道具异常 | 有时序能力的局部编辑或重生成 | 连续窗口、边界、身份、切点

[S35 §2.2.1](S35-technical-qc.md)：

> `hands`: `temporal_edit` → `regenerate_video`

措辞一致（都是"局部编辑或重生成"），但 `docs/11` 没说 `temporal_edit` 归 S41。**这不是矛盾，是粒度差异**。

**不选边**：S30 按 S35 的族表实现——`hands` 族的第二步 `regenerate_video` 才是 S30 的入口，第一步 `temporal_edit` 不是。

### C6 `docs/09 §4` 的降级链与 S30 的"不降规格"

`docs/09 §4`：

> 允许链：相同硬能力健康账户池→相同硬能力其他模型→策略已允许的规格调整/拆镜/分层合成→审核或隔离分支。角色身份、锁定音色、完整对白、权限和最低交付规格不能无声降级。

**降级链的执行者是 S25/S26，不是 S30。** S30 收到 `feasible=false` 时只能透传 `degradation_options`，不能自己选一个。

**冲突点**：`degradation_options` 里"拆镜"需要 S30 派生多个 segment job，而 S30 的 `service.yaml` recovery 说"长运镜可拆镜但需导演计划"——即拆镜需要额外授权，不是 S25 能自动选的。

**不选边**：S30 **不执行** `degradation_options` 里的任何选项。`feasible=false` 时透传给 S21/S39 决策。若策略允许，由 S21 重新下发拆分后的请求。

### C7 S30 需要"当前检查点"的读口，S29/S30 都没定义跨服务的检查点 API

S30 需要 `GET /shots/{shot_lineage_id}/image-checkpoint`（S29 提供）来冻结 `KeyframeBaselineRef`。本文假定 S29 提供该接口（[S29 §6.3](S29-image-production.md)），但 `services/image-production/service.yaml` 的 `api` 只声明了 `POST /image-jobs` 与 `GET /image-jobs/{id}`。

**同类**：`GET /shots/{shot_lineage_id}/video-checkpoint`（S30 给 S41 用）也没在 `service.yaml` 里。

**不选边**：两份方案都声明该接口为"建议补充的查询口"，并在服务目录里记录缺口。

### C8 生产组统一的 job 生命周期事件缺口（与 S29 C8 同）

`event-topics.v2.json` 只有各自的 `production.*.ready`。失败/冻结/取消无事件，S21 的 `ProductionStage` 层只能等 deadline。

**建议**：`production.stage.failed` / `production.stage.blocked`。S30 需要 `video.duration-mismatch` 与 `video.clip-attention-needed` 两个自身事件。

---

## 13. 与其他方案的交叉引用

| 方案 | 关系 |
|---|---|
| [S29](S29-image-production.md) | 提供关键帧检查点。S30 只读，冻结 `checkpoint_seq` |
| [S25](S25-model-routing.md) | `normalized_input.duration_seconds` 是提交值的唯一来源 |
| [S26](S26-generation-jobs.md) | 唯一付费入口；`attempt_no` 与节点费用上限的最后一道闸 |
| [S28](S28-artifact-ingest.md) | 归档 bytes；**需要它报 `observed_duration_ms`**（C2） |
| [S31](S31-speech-production.md) | `silent_then_tts` 的对白时长来源 |
| [S35](S35-technical-qc.md) | 动作族；`regenerate_video` 的执行者是 S26 不是 S30 |
| [S39](S39-repair-planning.md) | 下发 `RepairAction`；跨 Run 累计与 `strategy_class` |
| [S41](S41-editorial.md) | 时间线所有者；消费 `production.video.ready`；读 `GET /shots/{id}/video-checkpoint` |
| [S42](S42-composition.md) | 执行 `VideoEditPlan` 的裁切；S30 只出计划（`vep_executor` 约束） |
| [S08](S08-revision-snapshot.md) | 接受权威；检查点推进 ≠ 接受 |
