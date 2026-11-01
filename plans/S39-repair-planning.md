# S39 · 返工与修复决策 —— 实现方案

状态：`design_only`。QCReport v2 规范与 `type → 动作族` 定义在 [S35 §2](S35-technical-qc.md)；本文只写 S39 怎么消费这两样数据。

---

## 0. 本文要回答的两个问题

| 问题 | 章节 |
|---|---|
| 三个质检服务怎么保证输出一致 | 消费侧：§4.2 统一 ingest + validator + 拒收；规范侧见 [S35 §2.8](S35-technical-qc.md) |
| 返工上限怎么跨 Run 累计 | **§5** —— 计数器的键里没有 run/manifest/epoch/model |

---

## 1. 职责与不做什么

S39 拥有 `RepairPlan / 缺陷归因 / 修复动作 / 尝试上限 / 局部影响`。

**不做**（每一条都是设计错误）：

| 不做 | 理由 |
|---|---|
| 不执行修复 | 执行归 S21 → S26 → S27 |
| 不持有预算 | 金额唯一权威在 S23（docs/12 §1） |
| 不持有接受状态 | 接受指针在 S08 |
| 不改正史 | S16 |
| 不写 QC 报告 | S35–S38 |
| 不因上游 `suggested_action` / `recommended_actions` 而直接照做 | 建议是输入不是指令；动作由 §3 + §3.2 能力 + §5 上限决定 |
| 不把被上游抑制（`disposition≠reported`）的条目当缺陷去修 | 见 §4.3.1；那会绕过 S17 的合法例外判定 |
| 不自己合并多层 policy | `repair_limits` 取 `min` 的合并归 S02，S39 只读 `getEffectivePolicy` |
| 不因为"预算还够"就超上限 | 上限是硬规则，预算是另一道闸 |

S39 输出的是一个**计划**，不是一次执行、不是一次授权。计划可以被 S02 拒绝、被 S21 忽略、被预算挡住——S39 只管"应该怎么修"。

---

## 2. 计划的生命周期

```text
QC 报告（S35/S36/S37/S38 任一）
   │ quality.*.completed
   ▼
S39 ingest ──▶ 报告校验（§4.2）
   │              └─ 不合规 → quarantine + 告警，不生成计划
   ▼
QCSnapshot 聚合（等待窗口，§4.1）
   │
   ▼
缺陷归因 + 去重（§4.3）
   │
   ▼
上限检查（§5）── 超限 ──▶ ESCALATE
   │
   ▼
优先级排序 + 动作组合（§3）
   │
   ▼
RepairPlan · PLANNED
   │ quality.repair.planned
   ▼
S02 政策检查 → S21 编排 → S26 准入 → S27 执行
   │
   ▼
新 artifact → 复检（§6）
   │
   ├─ 复检 pass → RESOLVED
   ├─ 复检发现同类缺陷 → 计数 +1，循环
   └─ 计数耗尽 → ESCALATE
```

---

## 3. 缺陷 → 修复动作

### 3.1 路由算法：族由闭集定，细节由 registry 定

docs/11 §2 给了 8 行。**这不是一张 check_id 表**——S39 不认识任何具体的 `check_id`。它只做三件事，全部按数据走：

```text
1. family  = action_family[defect.defect_type]              ← contracts，14 值闭集
2. entry   = registry[defect.check_id]                       ← 质检服务的登记项
3. candidates = family ∩ entry.allowed_actions ∩ policy.allowed_actions
   起点 = entry.default_action
   升级 = entry.escalation_rule（数值阈值 + 升级后动作）
   窗口 = entry.window_scope（决定 keyframe_edit 能不能用、复检范围多大）
   candidates 为空 → human_review，reason=no_capable_route
```

新增一条 check 时，质检服务在 registry 加一行，S39 **不发版、不改代码**。`action_family` 只在"整个缺陷族要换处置策略"时才动，那是 contracts 层面的变更。

`action_family` 的权威定义在 [S35 §2.2.1](S35-technical-qc.md)，本文不复述——复述就会漂移。

### 3.1.1 registry 字段如何影响决策

| registry 字段 | 对 S39 的作用 | 缺省行为 |
|---|---|---|
| `default_action` | 动作链的起点 | 取 `family[0]` |
| `window_scope` | `frame`/`window` 且 ≤ 6 帧才允许 `keyframe_edit`；决定复检范围 | 取 `shot`，即最保守（不允许逐帧修） |
| `escalation_rule` | 族内升级的数值阈值（如"缺陷占 >30% 时长 → `regenerate_video`"） | 不自动升级，达上限后 `human_review` |
| `allowed_actions` | 与族取交集，收窄可选范围 | 等于族 |
| `severity_max` | 门禁判定：`blocker` 不可自动接受 | 取登记的 `severity_default` |

**登记无效时的处理**：registry 缺失该 `check_id`，或 `default_action ∉ family`，或 `allowed_actions ⊄ family` → 该缺陷按 `human_review` 处理并告警 `registry_entry_invalid_total{check_id}`。**不做 best-effort 猜测**——猜错的动作可能花钱、可能改坏已验收内容。

这条是强制约束：registry 是数据，数据可以写错，S39 必须能识别并拒绝，而不是拿错的数据继续跑。

### 3.1.2 几个必须在算法层表达、不能只写在 registry 里的规则

以下三条来自 `docs/11` 与 `service.yaml` 的 acceptance，属于**不可被 registry 数据覆盖**的硬规则：

1. **禁止逐帧修连续缺陷。** `keyframe_edit` 只在 `window_scope ∈ {frame, window}` 且窗口 ≤ 6 帧时可用（§3.2）。registry 把它登记到 `shot`/`all` 范围的 check 上 → 登记无效。
2. **授权失效类缺陷禁止自动重生成。** 落点在 registry：相关 check 登记 `never_auto_repair: true` + `no_auto_reason: rights_authorization`（§3.3）。族算出什么都不采纳——这是侵权风险，不是质量问题。S39 的代码里不写死任何授权类 check_id。
3. **动作必须属于该 type 的动作族。** 跨族动作（例如给 `technical` 缺陷开 `redo_lipsync`）一律拒绝，即使 registry 这么写。

### 3.2 "视频修复需时序能力"（service.yaml acceptance）

`temporal_edit` 与 `regenerate_video` 的可用性不是"有工具就行"，而是必须证明供应商/模型支持时序一致性。判定：

```text
required_capability = {
  temporal_consistency: true,
  max_window_frames: >= 缺陷窗口帧数,
  preserves_identity: true       // 有主体参考输入
}
```

S39 在生成计划前向 S24/S25 查询候选能力（**只查不调用**），没有满足的候选时：

- 降级到 `regenerate_video`（全镜重生成，通常能力更强）
- 若连 `regenerate_video` 都无候选 → `human_review`，`decision_reason=no_capable_route`

**禁止**：用逐帧图像编辑（`keyframe_edit`）去修连续视频缺陷。docs/11 §2 明令"静态逐帧重绘不作为默认视频修复"。S39 的 validator 有一条硬断言：`keyframe_edit` 只能用于 `scope ∈ {frame, window}` 且窗口 ≤ 6 帧的缺陷。

### 3.3 不能自动修的缺陷

有些缺陷**不生成任何修复动作**，直接 `human_review`。判据是三条中的任意一条：

| 判据 | 含义 | 例子 |
|---|---|---|
| 创作决策 | 修法本身是内容选择，系统无权替创作者决定 | 台词比镜头长：缩台词 / 加快语速 / 加长镜头 / 拆镜，四条路都对 |
| 授权/合权 | 自动修复会绕过需要人批准的门 | 音色授权失效（`AUD.VOICE.AUTHORIZATION`） |
| 不可逆或跨集 | 修复会影响已验收内容或其它集 | 跨集事实冲突（S17 `continuity`） |

这些判据**不写在 check_id 上**，写在 registry 的 `escalation_rule` 里，用两个字段表达：

| registry 字段 | 含义 |
|---|---|
| `never_auto_repair: true` | 该 check 命中即进人审，不看动作族 |
| `no_auto_reason` | `creative_decision` / `rights_authorization` / `irreversible_or_cross_episode` |

按判据归类（示例，不是穷举——清单在 registry 里，S39 不维护）：

| 判据 | 典型 check |
|---|---|
| 创作决策 `creative_decision` | `AUD.ALIGN.DURATION_VS_SPEC`（台词比镜头长，怎么消解是导演的）、`AUD.TEXT.SUBSTITUTION`（换词改语义）、`VIS.CONTINUITY.AXIS_CROSSING`（改分镜还是重拍整场） |
| 授权/合权 `rights_authorization` | `AUD.VOICE.AUTHORIZATION`（重生成是侵权风险） |
| 不可逆或跨集 `irreversible_or_cross_episode` | 跨集事实冲突（改一个事实要重算所有依赖它的集） |

**S39 只认 `never_auto_repair` 这个布尔值**，不认具体 check_id。质检服务新增一条"必须人审"的检查，登记 `never_auto_repair: true` 即可，S39 不需要发版。

这类缺陷仍然进 `RepairTarget`、仍然参与去重和排序，但 `action_candidates = []`，`RepairPlan.state = escalated`，`escalation.reason` 取 registry 的 `no_auto_reason`。

**不要把它们当作"修不了所以放过"**：它们进人审队列，不推进 S08 接受指针。

---

## 4. 报告聚合与归因

### 4.1 四个质检服务是异步的

不能收到第一份报告就生成计划。`AggregationWindow`：

```text
打开条件：QCSnapshotKey 的第一份报告到达
等待：直到下列全部满足或 deadline 到
  - 该 artifact_sha256 的 S35 / S36 / S37 报告都已到（S38 只在有字幕时要求）
  - 或到达 aggregation_deadline（默认 10 分钟）
```

超时未齐：

- 已到的报告用于生成计划；缺失层在计划里标 `pending_layers`
- **计划状态为 `PLANNED_PARTIAL`**，S21 只执行与已到层相关的动作，其余待补
- 缺失层若是 `blocker` 级检查的必需层 → 计划状态 `BLOCKED_INCOMPLETE`，转人审

这防住了"先非阻塞地修技术问题，再发现语义是 blocker"的浪费。

### 4.2 统一 ingest：消费侧的一致性闸门

S39 是**唯一**的四层报告消费者，因此它承担契约守护职责：

```text
ingest(event):
  1. 取 data_ref → 拉完整报告
  2. ReportValidator(report, layer)：
       a. JSON Schema 校验
       b. report_id 由派生键重算一致
       c. subject.artifact_sha256 == 事件 data.artifact_sha256 ？ ← 报告绑定的 hash 必须等于事件声明的
       d. check_manifest 完备（对照 check-registry 的 required set）
       e. coverage_ratio == evaluated/planned，planned>0
       f. decision 与 defects/abstain 自洽（uses 共享 decide()，输出必须相同）
       g. time_range.end > start；bbox 在 [0,1]
       h. defect.type 与 check_registry[check_id].defect_type 一致
       i. check_registry 里该 check_id 存在，且登记项通过族校验
          （default_action/allowed_actions ⊆ action_family[defect_type]）
       j. defect.recommended_actions ⊆ 该 type 的动作族（建议不能跨族）
  3. 任一失败 → quarantine，metric qc_report_rejected_total{reason,layer}+1，P1 告警
     不生成计划、不 best-effort 解析
```

**这是"三个质检服务怎么保证输出一致"的下半句**：规范是单一 schema + 共享 SDK，保障是 S39 拒收。schema 漂移在 CI 里被 conformance 用例拦住；漏网的在这里被拦住。

### 4.3 缺陷归因与去重

```text
归因键 = (tenant_id, branch_id, shot_lineage_id, defect_fingerprint)
```

同一键的多条 defect 归为一条 `RepairTarget`：

```text
RepairTarget:
  target_id
  defect_fingerprint
  primary_source        // S17 > S36 > S38 > S37 > S35（见 S35 §1.1）
  contributing_reports[]   // 每份报告的 report_id + generation
  severity              // 取最高
  merged_evidence[]     // 合并的 artifact ids
  action_candidates[]   // 各来源 recommended_actions 的交集优先，并集兜底
  recheck_scope         // 并集
```

动作选择规则：

1. 各来源建议的**交集**非空 → 取交集里最"小"的（按 §4.4 的 `action_cost_penalty` 序）
2. 交集为空 → 按 `primary_source` 的建议
3. 仍冲突 → `human_review`，`decision_reason=action_conflict`

**建议只是候选，不是指令。** S17 报告里的 `suggested_action`、S36 的 `recommended_actions`，S39 一律当输入而非命令：最终动作由本服务按上表、按能力可达性（§3.2）、按上限（§5）决定。任何"上游说修什么就修什么"的实现都会把修复决策泄漏到质检服务里。

#### 4.3.1 被上游抑制的缺陷不生成计划

S17 会把"看着像缺陷但已证明合法"的条目**保留在报告里**，带 `disposition` 与 `reason`（例如闪回镜头穿了另一个时代的服装）。这不是"没检出"，审计上必须能区分。

S39 的规则：

```text
if defect.disposition != "reported":      // 例如 suppressed / legal_exception
    不进入 RepairTarget，不生成动作
    但记入 snapshot.suppressed_defects[]，带 report_id + disposition
```

- 抑制条目**不参与**覆盖断言（§4.3.2）的 `reported` 集合——它们是"查了，判为合法"，不是"没查"
- 抑制条目**不消耗**尝试计数
- 若同一指纹在另一份报告里是 `reported`（典型原因：S36 没收到 S17 的例外声明），**不是"以 S17 为准悄悄压掉"**，而是 `escalated` + `escalation.reason = exception_not_propagated`。理由：例外没送到下游说明声明链路断了，此时要么例外真的不适用、要么下游漏判，两种都不该由 S39 猜。人可以一次判掉，系统不该自动吞掉一条观测。

这条和 [S36 §7](S36-semantic-qc.md) 是同一个机制的两端：那边 S36 收到例外后不判，这边 S39 收到带 disposition 的条目后不修。两端都做，才不会出现"质检说合法、修复还是照修"。

#### 4.3.2 覆盖断言（"不重叠也不留空"的机器校验）

去重只解决"重复"，不解决"没人报"。所以 ingest 还有一步：

```text
expected = check-registry 里 owner ∈ {S35,S36,S37,S38,S17} 的 required check
reported = 四份报告 check_manifest 的并集（status ∈ {evaluated, not_applicable}）
gap      = expected - reported
```

- `gap` 非空 → 计划里写 `coverage_gap: [check_id…]`，`state = escalated`，`escalation.reason = coverage_gap`
- `status=not_applicable` 且 `reason_code` 合法（例如手部面积不足）**不算 gap**——那是"查了，不适用"
- `status=not_evaluated` / `failed` **算 gap**——那是"该查没查成"

这条断言是 [S36 §2.5](S36-semantic-qc.md) 承诺的落点。它同时是两个硬约束的守门人：

- **不重叠**：`owner` 在 registry 里唯一，CI 断言没有两个服务声称同一个 check
- **不留空**：`gap` 非空就拦下，不会出现"两边都以为对方在查"

`VIS.KNOWLEDGE.SCOPE_RENDER` 这类显式登记为"不存在"的行，registry 里 `owner=S17`、`status=not_applicable`，所以不产生 gap。

### 4.4 优先级表

优先级 = 缺陷严重度 × 阻断性 × 修复成本倒数。具体打分：

```text
priority_score = severity_weight
               + blocking_weight
               + recheck_cost_penalty
               - action_cost_penalty
               - dependency_penalty        // 修完会不会引出更多依赖

severity_weight:        blocker=1000, major=300, minor=80, info=10
blocking_weight:        阻断正式交付 +500；仅阻断预览 +0
recheck_cost_penalty:   复检范围含全片 +120；含连续窗口 +40；单帧 +5
action_cost_penalty:    regenerate_video=200, redo_lipsync=150, remix=60,
                        transcode=30, realign_subtitles=20, temporal_edit=80,
                        keyframe_edit=25, reingest=10
dependency_penalty:     该修复会改变上游（如剧本/资产）导致下游全失效 +400
```

排序后**按依赖排序而非纯分数**：一个 `regenerate_video` 可能同时消除多个缺陷，必须排在它们之前。所以：

```text
1. 先按 priority_score 排序
2. 再做"合并检查"：若某动作的复检范围覆盖另一 target 的缺陷位置，
   且动作类型能同时修复两者 → 合并，被合并者标记 by=target_id
3. 最终顺序 = 合并后的 DAG 拓扑序，同层按 score 降序
```

### 4.5 计划里的每个动作

```json
{
  "action_id": "act_…",
  "target_ids": ["tgt_…"],
  "action": "temporal_edit",
  "parameters": { "trim_start_seconds": 0.0, "trim_end_seconds": 0.25 },
  "capability_requirements": { "temporal_consistency": true, "max_window_frames": 8 },
  "estimated_cost": {
    "band": "low|medium|high",
    "driver": "per_generation_second",
    "quote_ref": { "service_id": "S25", "id": "quote_…", "revision": 1 }
  },
  "recheck_scope": ["technical", "semantic:continuity_window", "identity"],
  "attempt_slot_id": "slot_…",
  "depends_on": ["act_…"],
  "sequence": 1
}
```

`estimated_cost` 只是**估计**，不是授权。真正的金额由 S23 在执行前 reserve。S39 若声称"这个修完还剩 X 预算"就是第二权威——禁止。

---

## 5. 尝试上限与跨 Run 累计（核心）

### 5.1 谁拥有计数器

这是本域最容易做错的地方，所以先划边界：

| 事实 | 权威 | 说明 |
|---|---|---|
| 某次业务尝试发生了、是什么策略、花了多少 | S26（`GenerationAttempt`） | 执行的唯一记录者 |
| 本次 Run 还剩多少业务尝试额度 | S21（编排）+ S02（policy） | 编排层是否继续 |
| **跨 Run 的累计业务尝试次数与"同类同策略"次数** | **S39** | 本文 |

S39 记 `RepairAttemptLedger`。它不是"第二份 attempt 记录"——它只记**决策维度**：某个稳定目标上，历史上一共消耗了多少次、每次用了什么策略。S26 不知道"跨 Run 累计"，因为它只看当前 job；S21 不知道"跨 Run"，因为它一次只看一个 Run。

docs/11 §3 的要求："子工作流、换模型、换 Run 不能清零已消耗的总费用"。费用在 S23，**次数在 S39**。

### 5.2 计数器的键（关键设计）

```text
AttemptCounterKey = (tenant_id, series_id, branch_id, shot_lineage_id, strategy_class)
```

**注意键里没有什么**：

- 没有 `run_id` ← 换 Run 不清零
- 没有 `manifest_id` / `manifest_revision` ← 换 manifest 不清零
- 没有 `node_epoch` ← 局部重生成（提 epoch）不清零
- 没有 `execution_fence` ← 取消重启、故障切换不清零
- 没有 `model_snapshot` / provider ← 换模型不清零
- 没有 `workflow_id` ← 套子工作流不清零
- 没有 `defect_fingerprint`

**有 `shot_lineage_id`**：稳定镜头血缘。同一个逻辑镜头在多次重生成中保持同一 ID，这是"每镜 3 次"能跨 Run 累计的基础。

**有 `strategy_class`**：归一化的修复策略类，不是自由文本。定义见 §5.3。

最后一个"没有 `defect_fingerprint`"是有意的：计数针对**策略**，不是针对缺陷。否则：同一个镜头先后出现 3 个不同缺陷，每个都是"第一次"，上限失效。

### 5.3 strategy_class

```text
strategy_class = f(action, normalized_parameters)
```

归一化参数（去掉不影响本质的差异）：

| action | 参与归一化的参数 | 说明 |
|---|---|---|
| `regenerate_video` | prompt_template_id, shot_spec_revision | 换模板算不同策略；只因 seed 不同算同一策略（seed 记在 variation_nonce，不进键） |
| `keyframe_edit` | mask_region_bucket, target_frame_bucket | 位置桶 0.1 归一化 |
| `temporal_edit` | edit_kind (trim/pad/retime) | |
| `redo_lipsync` | lipsync_model_family | |
| `remix` | fix_kind (loudness/ducking/peak) | |
| `transcode` | target_profile_id | |
| `reingest` | source_kind | |
| `realign_subtitles` | retime_kind | |

**`variation_nonce`、`seed`、`attempt_id`、`provider`、`model_id` 一律不参与。** 这是"同类同策略额外重复最多 1 次"（`max_identical_strategy_repeats: 1`）可执行的前提——否则每次换 seed 都变成全新策略，上限形同虚设。

### 5.4 上限的四级

```text
Level 1  每镜头每策略类    max_identical_strategy_repeats   (policy, 默认 1)
Level 2  每镜头总计        max_business_attempts_per_shot   (policy, 默认 3)
Level 3  每集总计          max_business_attempts_per_episode (扩展)
Level 4  每 Run / 全局     S21 的 run 预算与 S23 的费用上限
```

四级**全部**要满足才能继续；任一不满足即停。docs/11 §3 原文："同时服从 shot/scene/episode/run 多级预算、截止时间和全局累计计数"。

注意 Level 3/4 在上面的 policy schema 里还没有——`automation-policy.v2.schema.json` 的 `repair_limits` 只有 `max_business_attempts_per_shot` 和 `max_identical_strategy_repeats`。这是契约缺口（§11 C2）。本文按"需要另有 episode/run 级上限"实现，读不到时按默认值 `episode=12, run=40`，并记 `policy_partial=true` 到计划里——**不静默忽略**。

**读取 policy 时不做自己的层级合并。** S02 的合并算子里 `repair_limits.*` 取 `min`（跨企业/项目/系列/集各层），S39 只消费 `getEffectivePolicy` 已经合并好的结果。自己再"从企业层逐层 apply"会与 S02 的幂等/交换/结合性质不一致，层级缺失或乱序时结果会漂。同理，S39 不缓存策略结果以外的派生物——`PolicyDecision` 也不缓存（那是 S02 的放行凭证，短寿一次性）。

### 5.5 计数何时 +1

只有**业务尝试被真正提交**时 +1。规则：

| 事件 | 计数 |
|---|---|
| `quality.repair.planned`（计划生成） | **不 +1**。计划只是打算 |
| S21 接受计划、S26 创建 attempt 并拿到 dispatch 授权 | **+1**（在 `REPAIR_ATTEMPT_STARTED` 时） |
| 网络重试 / 5xx 重试 | **不 +1**（docs/11 §3：网络重试与真实付费重生成分开） |
| `SUBMISSION_UNKNOWN` 后对账确认真的提交了 | **+1**（确认时补记） |
| `SUBMISSION_UNKNOWN` 后确认没提交 | **不 +1** |
| 计划生成后 S02 拒绝 / 预算不足 / 无能力路由 | **不 +1**（没花钱，没试） |
| attempt 失败（供应商报错，已付费） | **+1**（已消耗） |

`REPAIR_ATTEMPT_STARTED` 由 S39 消费 S26 的 `generation.job.state-changed`（`→DISPATCH_AUTHORIZED`）后写入。S39 是这条事件的消费者，不是生产者。

### 5.6 台账表与预留

```sql
CREATE TABLE repair_planning.attempt_ledger (
  ledger_id            uuid PRIMARY KEY,
  tenant_id            text NOT NULL,
  series_id            text NOT NULL,
  branch_id            text NOT NULL,
  episode_id           text NOT NULL,
  shot_lineage_id      text NOT NULL,
  strategy_class       text NOT NULL,
  -- 计数
  attempt_seq          integer NOT NULL,      -- 该 (key) 的第几次，从 1 开始
  attempt_id           text NOT NULL,         -- S26 的 attempt_id
  provider_job_id      text,
  -- 归因（仅用于审计，不进唯一键）
  run_id               text NOT NULL,
  manifest_id          text NOT NULL,
  node_epoch           integer NOT NULL,
  repair_plan_id       text NOT NULL,
  action_id            text NOT NULL,
  defect_fingerprints  text[] NOT NULL,
  -- 结果
  outcome              text NOT NULL CHECK (outcome IN ('pending','succeeded','failed','settled')),
  created_at           timestamptz NOT NULL,
  settled_at           timestamptz,
  UNIQUE (tenant_id, series_id, branch_id, shot_lineage_id, strategy_class, attempt_seq)
);
CREATE UNIQUE INDEX ON repair_planning.attempt_ledger (attempt_id);   -- 同一 attempt 只记一次
CREATE INDEX ON repair_planning.attempt_ledger
  (tenant_id, series_id, branch_id, shot_lineage_id, created_at DESC);

-- 预留：防止并发计划同时通过上限检查
CREATE TABLE repair_planning.attempt_reservation (
  reservation_id   uuid PRIMARY KEY,
  tenant_id        text NOT NULL,
  series_id        text NOT NULL,
  branch_id        text NOT NULL,
  shot_lineage_id  text NOT NULL,
  strategy_class   text NOT NULL,
  attempt_seq      integer NOT NULL,
  repair_plan_id   text NOT NULL,
  state            text NOT NULL CHECK (state IN ('reserved','committed','released')),
  expires_at       timestamptz NOT NULL,
  created_at       timestamptz NOT NULL,
  UNIQUE (tenant_id, series_id, branch_id, shot_lineage_id, strategy_class, attempt_seq)
);
```

**唯一约束就是上限的执行者**：

```sql
-- 同一个 (shot_lineage, strategy_class, attempt_seq) 只能有一条
-- attempt_seq 超过 max_identical_strategy_repeats+1 时，插入失败 → 拒绝
```

这比"先查再写"可靠：两个并发计划同时读到 seq=2、都想写 seq=3，唯一约束只放一个过。E26 的"不能绕过镜头/集上限"在数据库层就有保证。

### 5.7 上限检查算法

```text
authorize(plan, policy):
  targets = plan.targets

  for t in targets:
    key = (tenant, series, branch, t.shot_lineage_id, t.strategy_class)

    # Level 2: 每镜总计
    shot_total = count(ledger where shot_lineage_id = t.shot_lineage_id)
    if shot_total + 1 > policy.max_business_attempts_per_shot:
        return DENY("shot_attempt_cap")

    # Level 1: 同类同策略
    strategy_total = count(ledger where key = key)
    if strategy_total + 1 > policy.max_identical_strategy_repeats + 1:
        return DENY("identical_strategy_cap")

    # Level 3: 每集
    ep_total = count(ledger where episode_id = plan.episode_id)
    if ep_total + 1 > policy.max_business_attempts_per_episode:
        return DENY("episode_attempt_cap")

    # Level 4: Run —— 权威在 S21/S23，S39 只做读取预检
    run_state = query S21(run_id)
    if run_state.remaining_repair_attempts < 1 or run_state.budget_exhausted:
        return DENY("run_cap_or_budget")

  # 同 (key, seq) 的并发抢占由唯一约束兜底
  return ALLOW
```

被 DENY 时的输出不是"失败"，而是 **`ESCALATE`**：

```json
{
  "plan_id": "rp_…",
  "state": "escalated",
  "escalation": {
    "reason": "identical_strategy_cap",
    "capped_at": { "shot_lineage_id": "lin_…", "strategy_class": "regenerate_video:tpl_a:rev4",
                   "attempts_used": 2, "limit": 2 },
    "remaining_targets": ["tgt_…"],
    "handoff": "S40"          // 或 "isolate_branch"，取决于 policy.exception_behavior
  }
}
```

按 `policy.exception_behavior`：

- `wait_review` → 创建 S40 ReviewTask，等待人工
- `isolate_branch` → 该分支标 blocked，继续其他分支；Run 可结束 INCOMPLETE（docs/06）

**绝不**：换模型/换种子/套子工作流/提 epoch 后"重新计数"。§5.2 的键设计已经把这些路径堵死。

### 5.8 跨 Run 的具体证据（回答 E26）

设一个镜头 `lin_shot_3_2_v1`：

| Run | 事件 | strategy_class | attempt_seq | 累计本次 |
|---|---|---|---|---|
| Run 1 | 首生成发现身份漂移 | `regenerate_video:tpl_a:rev4` | 1 | 1 |
| Run 1 | 复检仍漂移，换 seed | `regenerate_video:tpl_a:rev4` | 2 | 2 |
| Run 1 | 复检仍漂移 | 需要 seq=3 → 超 `max_identical_strategy_repeats=1`（即同策略最多 2 次） | — | DENY |
| — | 换模板重试 | `regenerate_video:tpl_b:rev2` | 1 | 3 |
| Run 2（新 Run） | 又发现漂移 | `regenerate_video:tpl_b:rev2` | 2 | 4 |
| Run 2 | 再要 | shot_total=4 > 3 → DENY(`shot_attempt_cap`) | — | ESCALATE |

看这张表：

- 第 5 行换 Run 后 `attempt_seq` 继续从 2 开始，**没有清零**——因为键里没有 `run_id`。
- 第 6 行 `shot_total=4` 是**跨 Run 求和**——因为查询按 `shot_lineage_id` 不用 `run_id` 过滤。
- 第 3 行"换 seed"进不了 seq=3——因为 `seed` 不在 `strategy_class` 里。

费用侧：每次 attempt 的实际金额在 S23 按 `provider_job_id` 记；S39 的台账只记 `attempt_id`，S23 可通过它关联。**S39 不存金额**（docs/12 §1：S23 是唯一权威）。

### 5.9 死循环防护

```text
若同一 plan 的复检报告在 24h 内产生了 ≥3 个新 plan，
且 defect_fingerprint 集合无变化
→ 强制 ESCALATE("no_progress")
```

这防住"修不动但每次都算新缺陷"的隐性死循环——指纹不变就是没进展，无论计数怎么写。

---

## 6. 复检

### 6.1 复检是强制的

docs/11 §2 末句："修复后必须重检相关层，旧 QC 不适用于新 hash。"

```text
修复产物归档（新 artifact_sha256）
  └─ S39 自动创建复检作业：
       recheck_scope 决定调哪些服务
       报告带 repair_context：
         repair_plan_id / attempt_slot_id / attempt_seq / defect_fingerprints
```

`attempt_slot_id` 来自 §5.6 的 `attempt_reservation.reservation_id`。这样复检报告能精确关联到"第几次尝试"。

### 6.2 复检不过怎么办

```text
复检报告到达
  │
  ├─ decision = pass 且无原指纹缺陷
  │     → RepairPlan.state = RESOLVED
  │     → 通知 S08 候选可接受（S08 才是权威）
  │
  ├─ decision = pass 但出现了原指纹之外的新缺陷
  │     → 生成新 plan（新指纹，新计数器）；不消耗原计划额度
  │
  ├─ decision = repair 且含原缺陷指纹
  │     → 这算"同类重复"：attempt_seq 已消耗 → 走 §5.7 检查
  │       未超限 → 新 plan，继续修
  │       已超限 → ESCALATE
  │
  ├─ decision = human_review
  │     → ESCALATE("human_review_requested")
  │     → 不自动再修（即使额度还有）
  │
  └─ decision = abstain
        → 不能当作"没修好"也不能当作"修好了"
        → 按 policy：
             wait_review   → ESCALATE("recheck_abstain")
             isolate_branch → 分支 blocked
        → 若 abstain 是 evaluator_timeout，先重跑复检（不消耗业务尝试）
```

最后一行的区分是关键的：**复检弃权 ≠ 修复失败**。重跑复检是网络/算力问题，不消耗业务尝试额度；但也不能当 pass 放行。

### 6.3 复检的"相关层"

`recheck_scope` 不是"全查一遍"。docs/11 §2 的表格列了每类问题的复检范围，`check-registry` 里每个 check 带 `recheck_scope` 字段。S39 取所有 target 的并集：

```text
scope 元素：technical | semantic:{aspect} | alignment:{aspect} | subtitle
例：
  修口型 → [alignment:lipsync, semantic:boundary, technical:audio]
  修字幕时码 → [subtitle, semantic:safe_area]
  修技术容器 → [technical, hash]          // hash 是完整性检查
```

**全查一遍是错误的**：既浪费，又会让无关层的评估器抖动变成"修复失败"。

### 6.4 复检也有上限

复检本身要花 QC 预算（VLM/ASR 调用）。S39 给每次 plan 的复检设 `max_recheck_rounds = max_business_attempts_per_shot + 1`（业务尝试 + 1 次最终确认）。超了 → ESCALATE。

---

## 7. 数据模型（schema `repair_planning`）

```sql
CREATE TABLE repair_planning.plan (
  plan_id             text PRIMARY KEY,        -- rp_ + ulid
  tenant_id           text NOT NULL,
  series_id           text NOT NULL,
  branch_id           text NOT NULL,
  episode_id          text NOT NULL,
  run_id              text NOT NULL,
  manifest_id         text NOT NULL,
  manifest_revision   integer NOT NULL,
  execution_fence     integer NOT NULL,
  artifact_sha256     text NOT NULL,           -- 修复前的
  state               text NOT NULL CHECK (state IN
    ('planned','planned_partial','blocked_incomplete','escalated',
     'human_resolved','resolved','superseded')),
  aggregation_deadline timestamptz NOT NULL,
  policy_ref          jsonb NOT NULL,
  policy_partial      boolean NOT NULL DEFAULT false,
  snapshot            jsonb NOT NULL,          -- QCSnapshot：四份报告引用 + 去重后的 targets
  escalation          jsonb,
  created_at          timestamptz NOT NULL,
  updated_at          timestamptz NOT NULL
);
CREATE INDEX ON repair_planning.plan (tenant_id, branch_id, episode_id, created_at DESC);
CREATE INDEX ON repair_planning.plan (artifact_sha256);

CREATE TABLE repair_planning.action (
  plan_id        text NOT NULL REFERENCES repair_planning.plan(plan_id),
  action_id      text NOT NULL,
  sequence       integer NOT NULL,
  action_type    text NOT NULL,
  target_ids     text[] NOT NULL,
  parameters     jsonb NOT NULL,
  capability_req jsonb NOT NULL,
  recheck_scope  text[] NOT NULL,
  attempt_slot_id text,
  depends_on     text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (plan_id, action_id)
);

CREATE TABLE repair_planning.attempt_ledger (...);        -- §5.6
CREATE TABLE repair_planning.attempt_reservation (...);   -- §5.6

CREATE TABLE repair_planning.recheck_job (
  recheck_id      uuid PRIMARY KEY,
  plan_id         text NOT NULL,
  attempt_slot_id text NOT NULL,
  artifact_sha256 text NOT NULL,              -- 修复后的
  scope           text[] NOT NULL,
  round           integer NOT NULL,
  state           text NOT NULL CHECK (state IN ('requested','running','completed','failed')),
  report_refs     jsonb,
  created_at      timestamptz NOT NULL
);

CREATE TABLE repair_planning.outbox (...);
CREATE TABLE repair_planning.inbox (...);
```

报告本体不落 S39：`snapshot` 只存引用 + 摘要。**S39 不是报告的第二个家。**

---

## 8. API

### `POST /repair-plans`

```json
{
  "command_id": "cmd_…",
  "execution_context": { "...": "execution-context v2" },
  "qc_snapshot": {
    "artifact_sha256": "…",
    "reports": [
      { "service_id": "S35", "id": "qc_…", "revision": 1, "content_hash": "…" },
      { "service_id": "S36", "id": "qc_…", "revision": 1, "content_hash": "…" },
      { "service_id": "S37", "id": "qc_…", "revision": 1, "content_hash": "…" }
    ]
  },
  "aggregation_mode": "wait_all",
  "max_actions": 10,
  "dry_run": false
}
```

`dry_run=true`：只做归因、排序、上限检查，返回计划但不发布事件、不预留计数。用于工作台预览。

响应 `202` → `plan_id`。

### `GET /repair-plans/{id}`

返回计划全文 + 当前 state + 已消耗计数摘要：

```json
{
  "plan_id": "rp_…",
  "state": "planned",
  "targets": [
    { "target_id": "tgt_…", "defect_fingerprint": "a1b2…",
      "primary_source": "S36", "severity": "major",
      "attempts_used": { "shot_total": 1, "this_strategy": 1 },
      "limits": { "shot": 3, "this_strategy": 2 } }
  ],
  "actions": [ "…" ],
  "estimated_cost_band": "medium"
}
```

### `GET /repair-plans?shot_lineage_id=…` — 计费/审计查询

返回该镜头的完整尝试历史（跨 Run），供运营与 E26 取证。

### `POST /repair-plans/{id}:escalate`

人工或系统强制升级（例如工作台点"放弃自动修复"）。幂等。

---

## 9. 事件

### 发布 `quality.repair.planned`

```json
{
  "data": {
    "plan_id": "rp_…", "state": "planned",
    "artifact_sha256": "…",              // 修复前
    "shot_lineage_id": "lin_…",
    "episode_id": "ep3", "run_id": "run_…",
    "target_count": 2, "action_count": 3,
    "top_action": "temporal_edit",
    "estimated_cost_band": "medium",
    "recheck_scope": ["technical","semantic:continuity_window"],
    "escalation": null
  },
  "data_ref": { "service_id": "S39", "id": "rp_…", "revision": 1, "content_hash": "…" }
}
```

### 发布 `quality.repair.escalated`（新增）

原 catalog 只有 `quality.repair.planned`。ESCALATE 是独立语义（要转人审/隔离），必须可被 S21/S40/S02 订阅，不能塞进 planned 的字段里。需改 `contracts/event-topics.v2.json`（§11 C1）。

### 订阅

| 事件 | 用途 |
|---|---|
| `quality.technical.completed` / `quality.semantic.completed` / `quality.alignment.completed` | ingest |
| `post.subtitle.ready` | ingest（取其 `qc_report_ref`） |
| S17 `ContinuityReport`（事件名待定，见 §11 C8） | ingest：跨集事实冲突的 defect 结论 |
| `generation.job.state-changed` | `REPAIR_ATTEMPT_STARTED` 时计数 +1（§5.5） |
| `review.decision.recorded` | 人工结论回填。**`recorded` ≠ `accepted`**：S40 只记录决定，推进接受指针是 S08 的 CAS。S39 据此把计划标 `human_resolved`，但不得自行推进任何接受状态 |
| `finance.ledger.entry-posted`（可选） | 金额归因（只读，不落金额） |

**不订阅** `snapshot.manifest.committed` 用于计数：manifest 变化不影响计数器（§5.2）。

---

## 10. 测试清单

### 10.1 E24 —— 缺陷 → 修复 → 复检 → 预算上限

| # | 用例 | 断言 |
|---|---|---|
| E24-R1 | 静态单帧身份漂移 | 动作 = `keyframe_edit`（不是 `regenerate_video`）；复检范围含 identity |
| E24-R2 | 8 帧连续身份漂移 | 动作 = `regenerate_video`；`keyframe_edit` 被 validator 拒绝（窗口 > 6 帧） |
| E24-R3 | 手部异常，无时序能力路由 | 降级 `regenerate_video`；若也无 → `human_review` + `no_capable_route` |
| E24-R4 | 口型偏移 150ms | `redo_lipsync`；复检含连续音视频 |
| E24-R5 | 字幕时码重叠 | `realign_subtitles`；复检只跑 `SUB.TIMING.*`，不跑技术层 |
| E24-R6 | 修复后复检 pass | `state=RESOLVED`；不推进 S08 指针（只是"候选可接受"通知） |
| E24-R7 | 修复后复检含同指纹缺陷 | 计数 +1；未超限 → 新 plan |
| E24-R8 | 修复预算在上限内但质量仍不过 | ESCALATE(`..._cap`)，不再自动修 |
| E24-R9 | 同一 `regenerate_video` 消除两个缺陷 | 合并为一个 action，`target_ids` 两条，不消耗两次计数 |
| E24-R10 | 修复后 hash 变了但复用旧 QC | ingest 拒收（`subject.artifact_sha256` 不等于新 artifact hash） |
| E24-R11 | S17 报告里 `disposition=suppressed` 的闪回服装条目 | 不生成计划；记入 `suppressed_defects`；不消耗计数 |
| E24-R12 | 同一指纹 S17 说 suppressed、S36 说 reported（S36 没收到例外） | `escalated` + `exception_not_propagated`；不静默压掉 S36 的观测 |
| E24-R13 | **新增一条 check（registry 加一行，S39 代码不变）** | 该 check 的缺陷能正常产出计划；断言 S39 二进制未重新构建 |
| E24-R14 | registry 缺该 `check_id` 条目 | `human_review` + `registry_entry_invalid_total{check_id}` 告警；不猜动作 |
| E24-R15 | registry 把 `redo_lipsync` 登记到 `technical` 类 check 上 | 登记被拒（跨族）；即使强行写入，运行期按族裁剪后为空 → `human_review` |
| E24-R16 | registry 给 `shot` 范围的 check 登记 `keyframe_edit` | 登记无效（§3.1.2 第 1 条）；不允许逐帧修连续缺陷 |
| E24-R17 | `never_auto_repair: true` 的新 check（S39 代码不变） | `escalated`，`escalation.reason` 取登记的 `no_auto_reason`，不生成任何动作 |
| E24-R18 | 同 `defect_type`、不同 `check_id`、不同 `window_scope` 的两条缺陷 | 分别走各自 registry 的起点与升级，互不影响（族相同但细节不同） |

### 10.2 E25 —— abstain 不当 pass

| # | 用例 | 断言 |
|---|---|---|
| E25-R1 | 一份报告 `abstain`，其余 pass | 不得 `state=RESOLVED`；ESCALATE 或 `BLOCKED_INCOMPLETE` |
| E25-R2 | 报告 validator 失败 | quarantine + 告警；不生成计划 |
| E25-R3 | 复检 abstain（evaluator_timeout） | 重跑复检，不消耗业务尝试 |
| E25-R4 | 复检 abstain（low_confidence） | ESCALATE，不放行 |
| E25-R5 | 聚合窗口到期只到 2/3 层 | `PLANNED_PARTIAL` + `pending_layers` |
| E25-R6 | 动作冲突（两源建议不兼容） | `human_review` + `action_conflict` |

### 10.3 E26 —— 跨 Run 累计（核心）

| # | 用例 | 断言 |
|---|---|---|
| E26-R1 | Run 1 用策略 A 两次 | `attempt_seq` = 1, 2；第 3 次 DENY(`identical_strategy_cap`) |
| E26-R2 | Run 2 再要策略 A | 仍 DENY（键无 `run_id`） |
| E26-R3 | Run 2 用策略 B | `attempt_seq`=1（不同 strategy_class），但 `shot_total`=3 继续累计 |
| E26-R4 | 换模型/换 seed 重试策略 A | 仍是同一 strategy_class → DENY |
| E26-R5 | 提 node_epoch 后重试 | 仍 DENY（键无 `node_epoch`） |
| E26-R6 | 取消后新 Run（新 execution_fence） | 仍 DENY（键无 `execution_fence`） |
| E26-R7 | 套子工作流 | 仍 DENY（键无 `workflow_id`） |
| E26-R8 | 并发两个计划同时要 seq=3 | 唯一约束只放一个过；另一个被拒 |
| E26-R9 | 网络重试 3 次 | 计数只 +1 |
| E26-R10 | 计划生成但 S02 拒绝 | 计数 +0 |
| E26-R11 | `SUBMISSION_UNKNOWN` 后确认已提交 | 计数 +1（补记） |
| E26-R12 | `SUBMISSION_UNKNOWN` 后确认未提交 | 计数 +0 |
| E26-R13 | 费用跨 Run 累计 | 由 S23 保证；S39 台账的 attempt_id 能关联到全部 provider_job |
| E26-R14 | 同一镜头 3 个不同缺陷各修一次 | shot_total=3，第 4 次 DENY |

### 10.4 死循环与异常

| # | 用例 | 断言 |
|---|---|---|
| D1 | 24h 内 3 个 plan、指纹无变化 | 强制 ESCALATE(`no_progress`) |
| D2 | 计划引用了不存在的 shot_lineage_id | 422，不消耗计数 |
| D3 | 缺陷无 `recommended_actions` | validator 拒收（QCReport 要求 minItems 1） |
| D4 | 跨租户查询 attempt_ledger | 404 不泄漏 |
| D5 | 重放 `quality.*` 事件 | 计数不重复 +1（`attempt_id` 唯一索引） |

---

## 11. 契约缺口

| # | 缺口 | 建议 |
|---|---|---|
| C1 | `event-topics.v2.json` 无 `quality.repair.escalated` | 新增（§9） |
| C2 | `automation-policy.v2.schema.json` 的 `repair_limits` 只有 shot 级两项 | 补 `max_business_attempts_per_episode`、`max_business_attempts_per_run`、`max_recheck_rounds`；缺省时用 §5.4 默认值并记 `policy_partial` |
| C3 | 无 `contracts/quality/repair-plan.v2.schema.json` | 新增；S21 需要机器可读的计划契约 |
| C4 | `shot_lineage_id` 在任何契约里都没有定义 | **必须新增**——它是跨 Run 累计的锚点。建议由 S06/S08 在 shot 创建时生成并在 manifest 里固定；`contracts` 需给出生成规则与不可变性约束 |
| C5 | **无 `contracts/quality/check-registry.v2.json`** —— 它是 S39 修复路由的输入，不是可选项 | 必须新增，字段见 [S35 §2.2.2](S35-technical-qc.md)：`layer` / `defect_type` / `default_action` / `window_scope` / `escalation_rule` / `severity_default` / `severity_max` / `allowed_actions` / `recheck_scope` / `never_auto_repair` / `no_auto_reason`。**加载期强制族校验**（S35 §12 C6），越族拒绝加载 |
| C5b | `action_family[type]` 没有机器可读定义，只有 S35 §2.2.1 的表格 | 加进 `check-registry.v2.json` 的顶层或单独 `quality/action-families.v2.json`。S39 读它，不硬编码——否则闭集改动要改代码 |
| C6 | S21 无"剩余修复额度"查询接口 | §5.7 Level 4 需要；在补齐前 S39 只能预检 S23 预算，run 级上限靠 S21 在执行时兜底 |
| C7 | `service.yaml` 的 `depends_on` 只有 S35/S36/S37/S09/S02，但本文实际依赖 S26（`generation.job.state-changed` 计数）、S23（预算预检）、S24/S25（能力查询）、S40（人审交接）、S17（`ContinuityReport`） | 补 catalog 的 `depends_on`。当前缺口意味着契约图里看不出这些依赖，部署顺序与故障域分析会漏 |
| C8 | S17 的 `ContinuityReport` 没有 schema 定义，S39 也未订阅其事件 | 见 semantic-qc C1；S39 需要它能被 ingest，否则跨集冲突永远到不了修复决策 |
