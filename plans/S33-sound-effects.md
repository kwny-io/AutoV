# S33 · 音效 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。所有供应商仍为 `disabled`，`slo.verified: false`。

上游依据：[docs/07 §1 步骤 7](../docs/07-production-workflows.md)（"S41 创建 EDL，S33/S34 准备音效音乐，S38 生成字幕，S42 混音合成"）、[docs/09 §1](../docs/09-model-execution.md)（"S29–S34 负责生产意图与阶段，不能绕过 S26 直连模型"）、[docs/11 §2](../docs/11-quality-repair.md)、[S41 §4.2–§4.3](S41-editorial.md)（EDL 变更传播、`affected_output_ranges`）、[S35 §2.2.1](S35-technical-qc.md)、[S39 §5](S39-repair-planning.md)、[S42 §4](S42-composition.md)（音效 stem 归属）、`contracts/generation-command.v2.schema.json`（`modality: sound_effect`）、`contracts/quality/action-families.v2.json`。

链上位置：`S07 分镜/节拍 → S33 组音效意图（cue 意图 + 时长 + 许可）→ S25 估价路由 → S26 准入 → S27 执行 → S28 归档 → S33 登记 cue 素材并发布 ready → S41 按 EDL 定位切点 → S42 混音成 stem_sfx`。

本文重点回答三个问题：**"脚步跟着剪辑重定位"怎么做到不重做音效、只重切时间**（§4.2、§4.4）、**素材不可用时"标记缺口"和"找一个替代"之间的界线在哪**（§4.6）、**一个音效 cue 的"许可"到底约束的是哪一件事**（§4.5）。

---

## 0. 一句话边界

**S33 拥有"这条 cue 要什么声音、多长、在什么许可范围内、生成了哪一个素材、这个素材的许可依据是什么、哪条 cue 现在还没有素材"。它不拥有分镜、不拥有时间线、不拥有音频字节的存储、不拥有混音、不拥有 stem 的最终形态。**

```
S07:   shot/beat 与 cue 意图（S33 只读 ShotSpec，不改分镜）
S41:   EDL、clip 时间、`affected_output_ranges`（S33 只读，不写）
S02:   权利策略（S33 只引用，不解释）
S33:   SfxCue + SfxTake + cue 意图版本 + 缺口记录 + 定位（placement）
S25:   可行路由与报价
S26:   attempt / ProviderJob / SUBMISSION_UNKNOWN（唯一付费通道）
S28:   音频 bytes 与 content_hash（唯一权威，`content_class='generated_audio'` 或 `'audio_stem'`）
S42:   混音、stem_sfx 的最终形态、ducking、响度
S23 / S22: 钱与 permit
```

判据：任何"这条 cue 有没有声音"的问题，答案来自 S33 的 take 与缺口记录。任何"它在成片的第几毫秒"的问题，答案来自 S41 的 EDL + S33 的 `placement` 换算。任何"这个素材能不能用"的问题，答案来自 S02 的权利策略。任何"混出来什么效果"的问题，答案来自 S42。

**必须守住的一条原则：**

> **音效跟着剪辑走，但重定位不是重生成。**

这条原则是 S33 与 S34 共享的、区别于 S31/S32 的核心。剪辑变化（镜头时长变了 4 秒、镜头顺序调了）会让音效的**位置**变，但通常不会让音效的**内容**变。关门声还是关门声，只是它现在发生在第 8.2 秒而不是第 7.8 秒。

所以 S33 的义务分两层：

- **内容层**：这条 cue 应该是什么声音、多长。变了才重生成（花钱）。
- **定位层**：这条 cue 的素材放在时间轴的哪里。几乎总是由 S41 的 EDL 决定，**不花钱**。

**混淆这两层的后果**：剪辑动一下，整集音效全部重生成。这既浪费钱，又会让每一版成片的音效"听起来不一样"——同样一声关门，第 1 版和第 5 版的音色、尾音长度都不一样，因为它们是两次独立生成。观众不一定说得出来，但会感觉"粗糙"。

---

## 1. 责任与明确不负责

### 1.1 本服务是唯一写者

| 事实 | 说明 |
|---|---|
| `SfxCue` | 一条音效意图：cue 类型、意图描述、目标时长、所属 shot/beat、许可策略引用 |
| `SfxCueRevision` | cue 意图的一个不可变版本（意图 hash），跨 Run 共享 |
| `SfxTake` | 一个已归档的音效素材：`artifact_ref`、实测时长、许可依据、可循环性 |
| `CuePlacement` | 某条 cue 在某版 EDL 中的**计算出的**位置与拉伸方式（**不是**权威时间，是派生结果） |
| `RightsPolicyBinding` | 本 job 使用的许可策略快照：`policy_ref`、解析出的 `allowed_license_kinds` |
| `SfxGap` | 缺口记录：哪条 cue、缺什么、为什么、谁需要处理 |
| `SfxCheckpoint` | 每 `(shot_lineage_id, cue_index)` 的当前基线 take 指针 |

### 1.2 本服务明确不写

| 事实 | 权威 | S33 的正确做法 |
|---|---|---|
| 分镜、shot、beat 定义 | **S06 / S07** | 只存 `shot_ref` / `beat_ref` + 意图 |
| cue 在时间轴上的**权威**位置 | **S41** | 只算派生 placement；不写回 EDL |
| clip 顺序、时长、转场 | **S41** | 只读 |
| 音频 bytes、编码、容器 | **S28** | 只存 `artifact_ref` |
| 混音、增益、ducking、响度 | **S42** | 完全不涉及 |
| stem 的最终形态 | **S42** | S33 提供素材与 stem 归属提示，不产出成品 stem |
| 权利策略的内容与判定 | **S02** | 只存 `policy_ref`，不解释、不缓存结论用于放行 |
| 素材库的检索与版权台账 | **S02 / S44**（视实现） | S33 只用 `rights_policy_ref` 约束自己的生成请求 |
| 视觉 QC / 音频 QC | **S36 / S37** | 不自造音频质量评分 |
| 当前接受指针 / epoch / fence | **S08** | 只发事件 |
| 金额、permit、attempt | **S23 / S22 / S26** | 只持引用 |
| 修复决策、上限计数 | **S39** | 只执行被指派的那一格 |
| 人工裁决 | **S40** | 不写 |

### 1.3 本服务不直连供应商（强制条款）

**本服务不直连供应商。** 不持有供应商凭证，不出供应商域名，不配置供应商 SDK。

S33 的每一次付费音效生成走同一链路：

```
S33 组 GenerationCommand(modality=sound_effect)
  → POST /generation-jobs           (S26)
      → S23 reserve（按 quote_upper_bound_micro）
      → S22 permit
      → SubmissionIntent + Outbox
      → S27 claim dispatch grant → 付费提交
  → S26 观测 → S28 归档 → production.sfx.ready 回 S33
```

强制手段与 [S29 §1.3](S29-image-production.md) 相同（凭证隔离 / `require_dispatch_grant=true` 编译期常量 / 唯一付费入口）。

**S33 不参与准入 Saga 的内部步骤。**

**S33 的特殊风险：素材检索也算"付费调用"。** 如果 S33 将来支持从素材库检索既有音效（而不是生成），那也是一次外部调用——有配额、有计费、可能失败、可能返回不可用的许可。它同样必须走 S26，`modality` 需要一个 `sound_effect_search` 值（契约里没有，§12 C2）。

**为什么这条重要：** 音效的直觉是"库里找一个就行，不用花钱"。但素材库的检索 API 通常按次计费，而且**检索结果的许可状态可能过期**（素材被下架、许可被收回）。如果检索绕开 S26，S23 就看不到这部分消耗，E38 的成本复算会漏掉整块。

### 1.4 动作归属（对照 S35 动作族）

音效在 `action-families.v2.json` 的 `closed_type_set` 里**没有自己的 `defect_type`**。音效问题会落到三个已有类型上：

| 缺陷的表现 | `defect_type` | 族内动作链 | S33 的角色 |
|---|---|---|---|
| 音效缺失 / 错了 / 太响盖对白 | `audio_mix` | `remix → regenerate_video` | **不参与**（S42 的 `remix`；`regenerate_video` 是 S30） |
| 音效文件本身坏了 / 容器异常 | `technical` | `reingest → transcode → remix → temporal_edit → regenerate_video` | 不参与（S28 / S42） |
| 音效位置整体偏（跟剪辑不同步） | `temporal` | `temporal_edit → regenerate_video` | **不参与**——由 S41 的 EDL 修正或 S42 的 remix |

**这是一条必须写明的结论：S33 不是任何动作族的执行者。**

| 动作 | 执行者 | S33 的角色 |
|---|---|---|
| `remix` | **S42** | 不参与。音效素材原样交给 S42 |
| `regenerate_video` | **S30** | 不参与（`audio_mix` 族第二步，用于"音效问题严重到只能重生成画面与声音"的极端情况） |
| `reingest` / `transcode` | **S28 / S42** | 不参与 |
| `temporal_edit` | **S42 / S30** | 不参与 |
| `re_synthesize_speech` | S31 | 不参与 |
| `redo_lipsync` | S32 | 不参与 |
| `realign_subtitles` / `re_render_subtitles` | S38 | 不参与 |
| `change_spec` | S06 / S07 | 不参与 |
| `human_review` | S40 | 不参与 |

**S33 唯一能被"请求去做事"的入口是**：S07/S06 产生了新的 cue 意图（正常生产），或者 S41 的 EDL 变更导致 placement 需要重算（不花钱）。两者都不是修复动作。

**为什么 S33 不在任何动作族里是对的：**

音效的可修复性几乎完全不在音效本身。一声关门声"不对"，可能是（a）cue 意图描述错了（S07）、（b）素材太响（S42 的 remix）、（c）放的位置不对（S41 的 EDL）、（d）素材本身坏了（S28）。这四种都不是"重新生成一个音效"能解决的。

若强行给 S33 一个 `regenerate_sfx` 动作，它会变成"任何音效相关问题都重生成"的万能出口——而重生成一个"更响的关门声"永远不会解决"它盖住了对白"这个问题。

**S33 明确不做的五件事：**

1. **不做混音。** 不调增益、不做 ducking、不测响度、不合并 stem。这些全部是 S42（[S42 §4](S42-composition.md)）。
2. **不决定音效在时间轴上的位置。** S33 计算 placement 是为了告诉 S42 "素材的哪一段该出现在哪"，但**位置本身来自 S41 的 EDL**。
3. **不找"差不多"的替代。** 素材不可用时标记缺口（§4.6），不做无许可的替代、不用已过期许可的素材、不生成一个"听起来像"的假素材。
4. **不重生成来解决定位问题。** 位置错了 → 重算 placement，不花钱（§4.4）。
5. **不把 cue 意图的变更当成"重做"。** cue 意图变了是一次新的生产意图，与"上一版音效不好"是两件事（§8.3）。

---

## 2. 实体与关键区分

### 2.1 四层对象

```
SfxCueRevision      一条 cue 意图的不可变版本（cue 类型 + 描述 + 目标时长 + 许可策略）
      │  N
      ▼
SfxJob              一次生成意图（绑定一个 CueRevision + 时长 + 可循环性要求）
      │  1:1
      ▼
SfxTake             一个已归档素材（artifact_ref + 实测时长 + 许可依据 + 循环标记）
      │  N:1
      ▼
SfxCheckpoint       每 (shot_lineage_id, cue_index) 的当前基线

CuePlacement        旁挂：take 在某版 EDL 上的位置（纯派生，可重算，无状态）
```

**`CuePlacement` 不是一张持久化的权威表。** 它是一个可以随时从 `(SfxTake, EDL revision)` 重算的派生量。持久化它只是为了查询性能与可观测，**任何以它为准的决策都是错的**。

这条区分是 S33 的核心设计。§4.2 会展开为什么。

### 2.2 `SfxCue` 与 `SfxCueRevision`：为什么意图也要版本化

`sound_effect.input` 是 `{cue_intent, duration_seconds, rights_policy_ref}`，都是扁平字段。

**问题：** 一次生产里，"这条 cue 要什么"会被改很多次：

- 导演说"这个关门声太轻了，要更重"→ 改 `cue_intent`
- 剪辑让镜头短了 0.4 秒 → 改 `duration_seconds`
- 法务要求换许可策略 → 改 `rights_policy_ref`

这三件事都会改变要生成的东西，但**性质完全不同**：

| 变化 | 性质 | 该怎么办 |
|---|---|---|
| `cue_intent` 变 | 创作意图变 | 重新生成（可能） |
| `duration_seconds` 变（因剪辑） | 时长需求变 | 可能只需拉伸/裁切已有素材（§4.4） |
| `rights_policy_ref` 变 | 合规要求变 | 已有素材可能不再可用（§4.5） |

**如果只存一个 `SfxCue` 行并原地 UPDATE，就永远说不清"这个 take 是用哪版意图生成的"。** 也不说不清"这次抖动是剪辑造成的还是导演改的"。

所以意图必须版本化，且**三个字段分别参与指纹的不同部分**：

```
content_fingerprint = sha256(cue_intent || sfx_family || loopable_required)
duration_basis      = duration_seconds           (单独存，不直接进 content 指纹)
rights_basis        = rights_policy_ref          (单独存，影响可用性判定)
```

§4.4 会用这个拆分来决定"什么变了可以复用素材、什么变了必须重生成"。

### 2.3 为什么 `sfx_family` 是一个显式字段而不是从 `cue_intent` 里猜

`cue_intent` 是自由字符串（`contracts` 里是 `type: string`）。"脚步声"、"脚踩碎石"、"靴子踏地"都是同一族。

**为什么需要族：**

1. **定位策略按族不同。** 脚步声通常需要跟角色走（可能循环或拉伸）；关门声是点事件（fixed）；环境底噪是长循环（loop）。族决定 `placement_mode` 的默认值（§4.3）。
2. **替换策略按族不同。** 同一族里换一个 take 是可接受的（"这声关门换成另一声关门"）；跨族换就是错的（用"抽屉关上"顶替"车门关上"）。
3. **缺口评估按族不同。** 缺一个环境底噪可以暂时留空（混音时可能盖住）；缺一个剧情关键点音效（枪响）不能交付。
4. **`strategy_class` 归一需要一个稳定维度。** 用自由文本归一等于没归一。

**`sfx_family` 的取值必须是一个闭集**，由 `contracts` 定义（当前没有，§12 C1）。本文的临时处理是用一个固定的粗分类（`foley` / `ambience` / `hard_effect` / `ui` / `stinger`），并且**要求调用方必须给**——S33 不从 `cue_intent` 猜。

**为什么不猜：** 猜错族会导致定位策略错（把环境音当点事件处理 → 每次剪辑变成一坨不连续的声音）。而这个错误很难被发现，因为声音"有"，只是不对。

### 2.4 `duration_seconds` 的三重身份

`sound_effect.input.duration_seconds` 是一个 `exclusiveMinimum: 0` 的数值。它同时承载三件事：

| 身份 | 含义 | 变了意味着 |
|---|---|---|
| **内容需求** | 这个声音自然该多长（关门声 0.4 秒，雨声 30 秒） | 换素材（族一样但实例不同） |
| **时间线约束** | 这个位置有多长可用 | 可能只是裁切/拉伸 |
| **报价依据** | 按秒计费 | 预算变 |

**三者混在一个字段里是契约的现实，但 S33 必须分开对待：**

```
SfxJob.requested_duration_ms        ← 请求里的值（报价依据）
SfxTake.natural_duration_ms         ← 素材实际长度（S28 探测）
SfxTake.trim_head_ms / trim_tail_ms ← 为了适配时间线而裁掉的部分
CuePlacement.stretch_ratio          ← 拉伸/压缩比（只在允许时）
```

**关键规则：`requested_duration_ms` 变了不必然要重生成。** 若已有素材的 `natural_duration_ms ≥ requested` 且族允许裁切，直接裁。只有当 `natural_duration_ms < requested` 且族不允许拉伸，或裁切会切掉关键部分时，才重生成。

这是 §4.4"不因剪辑重生成"的具体机制。

### 2.5 `rights_policy_ref` 的语义：约束"能不能用"，不约束"像不像"

`sound_effect.input.rights_policy_ref` 的示例指向 `service_id: "S02"`（治理策略）。

**它约束的是**：这个音效素材在成品里**能不能用**——是否可以商用、是否需要在片尾署名、是否限制分发地区、是否有期限。

**它不约束**：音效听起来像什么。

这两件事容易混。有人会认为"用了一个有版权的枪声合成器，所以要选'可商用'的策略"——这是对的。但反过来，"用了'可商用'策略"不代表任何音效都自动合规：如果 S33 生成的音效**听起来和某部电影的标志性音效一模一样**，策略合规不解决侵权。

所以 `rights_policy_ref` 是**必要条件**，不是充分条件。S33 的实现约束：

1. **只用它过滤生成请求**（provider 必须支持"可商用"输出，或素材库必须有对应许可）。
2. **把它存进 `RightsPolicyBinding` 快照**，用于事后解释。
3. **每次 take 被使用前（不是生成时）复查**（§4.5）——因为策略可能变、素材可能被下架。

**S33 不解释策略内容。** 它不知道"可商用"具体指什么、不知道署名要求的格式。这些是 S02 的领域。S33 只传 `policy_ref`、只记录 S02 返回的 `allowed_license_kinds`。

---

## 3. 状态机

### 3.1 `SfxJob` 状态

```
CREATED → CHECKING_POLICY → PREPARING → SUBMITTED → WAITING_PROVIDER → INGESTING → READY
                 ↘                ↘              ↘                 ↘
          POLICY_BLOCKED     REJECTED_LOCAL     FAILED        WAITING_UNKNOWN
                                                                   ↓
                                                        RECONCILED_READY
                                                        RECONCILED_FAILED
                                                                   ↓
                                                              GAP_RECORDED
任意非终态 → CANCELED
```

| 从 | 到 | 触发 | 前置条件 | 副作用 |
|---|---|---|---|---|
| — | `CREATED` | `POST /sfx-jobs` | 校验通过 + 幂等未命中 | 写 job；写/复用 `SfxCueRevision` |
| `CREATED` | `CHECKING_POLICY` | worker 领取 | — | 查 S02，解析许可约束 |
| `CHECKING_POLICY` | `POLICY_BLOCKED` | 无可用许可通道 / 策略禁止该族 | — | 写 `RightsPolicyBinding`；**记 `SfxGap(no_licensed_route)`** |
| `CHECKING_POLICY` | `REJECTED_LOCAL` | 时长非法 / 族非法 / 范围不符 | — | 不花钱 |
| `CHECKING_POLICY` | `PREPARING` | 许可可用 | — | 取/复用 `RouteDecision` |
| `PREPARING` | `REJECTED_LOCAL` | `feasible=false` / 能力不足（如"可循环"） | — | 不花钱 |
| `PREPARING` | `SUBMITTED` | S26 返回 202 | `quote_valid_to > now()` | 存 `provider_job_id` |
| `SUBMITTED` | `WAITING_PROVIDER` | S26 → `QUEUED`/`RUNNING` | — | — |
| `SUBMITTED` | `FAILED` | S26 → `PROVIDER_FAILED` | 无重试预算 | — |
| `SUBMITTED` | `WAITING_UNKNOWN` | S26 → `SUBMISSION_UNKNOWN` | — | 冻结 |
| `WAITING_PROVIDER` | `INGESTING` | S26 → `PROVIDER_SUCCEEDED` | — | — |
| `WAITING_PROVIDER` | `FAILED` | S26 → `PROVIDER_FAILED` / `CANCELED` | — | — |
| `INGESTING` | `INGESTING` | S28 失败 / 未到 | 重试未尽 | 只重归档 |
| `INGESTING` | `READY` | 归档完成 + hash 一致 + `natural_duration_ms` 已知 + 许可复查通过 | — | 写 `SfxTake` + CAS 检查点 |
| `INGESTING` | `FAILED` | hash 不符 / 时长为 0 / 不可解码 | — | — |
| `WAITING_UNKNOWN` | `RECONCILED_READY` | S26 → `ARCHIVED` | 同 | 同 |
| `WAITING_UNKNOWN` | `RECONCILED_FAILED` | 确认未收单且放弃 | — | — |
| `WAITING_UNKNOWN` | `GAP_RECORDED` | 对账发现不可恢复 | — | 记缺口（§4.6） |
| 任意非终态 | `CANCELED` | 显式取消 | — | 调 S26 cancel；不承诺退款 |

**终态**：`READY`、`POLICY_BLOCKED`、`GAP_RECORDED`、`REJECTED_LOCAL`、`FAILED`、`RECONCILED_FAILED`、`CANCELED`。

### 3.2 `POLICY_BLOCKED` 与 `GAP_RECORDED` 的区别

两者都表示"这条 cue 没有素材"，但它们要求不同的人做不同的事：

| | `POLICY_BLOCKED` | `GAP_RECORDED` |
|---|---|---|
| 何时 | 提交前就发现没许可通道 | 尝试过但最终没拿到可用素材 |
| 原因 | 策略不允许 / 没有合规供应商 | 供应商失败 / UNKNOWN 对账失败 / 素材不可解码 |
| 谁处理 | 法务 / 内容负责人（改策略或换素材来源） | 制作 / 重试决策（S39 / 人工） |
| 花钱 | 否 | **可能已花** |
| 能否重试 | 不能（除非策略变） | 可能（`RECONCILED_FAILED`）→ 新 job |

**为什么必须有 `GAP_RECORDED` 而不只是 `FAILED`：**

`FAILED` 的语义是"这次尝试失败了"，隐含"再试一次可能成功"。而一个音效缺口在**成片交付层面**是一个必须被跟踪的事实：这一集缺一声枪响，混音师要知道，验收人要决定"接受无音效"还是"补"。`FAILED` 的 job 不会被汇总成"这一集有 3 个音效缺口"。

所以 `GAP_RECORDED` 会写一条 `SfxGap`，而 `SfxGap` 是 `service.yaml` `recovery` 里"素材不可用则标记缺口"的直接落点，也是 §4.6 的核心产物。

### 3.3 状态持久化

本地事务 + `state_seq` 乐观并发 + 同事务 Outbox + 表驱动转移（同 S29/S30）。

补充：**`SfxGap` 的写入必须与产生它的状态转移同事务。** 一个 `GAP_RECORDED` 状态而没有缺口记录，等于"这条 cue 静默消失了"——混音时 S42 找不到素材，而没有任何地方说"这里本来就该缺"。

---

## 4. 关键流程

### 4.1 正常路径

```
1. POST /sfx-jobs
     {command_id, request_hash, context,
      cue: {shot_lineage_id, cue_index, beat_ref?, sfx_family,
            cue_intent, cue_revision?},
      duration_seconds,
      loopable_required: bool,
      rights_policy_ref,
      placement_hint?: {anchor: 'shot_start'|'shot_end'|'beat', offset_ms},
      route_policy_ref | route_decision_id,
      budget_scope_id, deadline}

2. 校验（顺序固定）：
   a. context 必填项 + 签名绑定的 tenant
   b. 幂等键
   c. sfx_family ∈ 闭集
   d. duration_seconds ∈ (0, max_duration]
   e. cue_intent 非空
   f. rights_policy_ref 属于本 tenant
   g. shot_lineage_id / beat_ref 属于本 tenant
   h. deadline > now()

3. CHECKING_POLICY：
   - 向 S02 查 rights_policy_ref 的 allowed_license_kinds
   - 若空 或 与 sfx_family 冲突 → POLICY_BLOCKED + SfxGap(no_licensed_route)
   - 写 RightsPolicyBinding

4. 复用判断（§4.4）：若 content_fingerprint 命中已有 take、
   且时长需求可用裁切/拉伸满足、
   且不带 repair context、且许可仍可用 → 直接返回该 take

5. PREPARING：取 RouteDecision；检查能力
   （loopable_required 需要渠道支持可循环生成；
     不支持 → REJECTED_LOCAL(CAPABILITY_MISSING)，不降级）

6. SUBMITTED → 等 S26/S28

7. INGESTING→READY：
   - 归档后许可复查
   - 写 SfxTake（natural_duration_ms、loopable）
   - CAS 推进 SfxCheckpoint
   - 重算并持久化 CuePlacement（§4.2）
   - outbox production.sfx.ready
```

### 4.2 定位：EOE 变更时只重算，不重生成

这是 S33 与 S34 最重要的机制。

[S41 §4.2](S41-editorial.md) 的传播链：剪辑变更 → `timeline_stale` → 候选新 revision（不自动提交）→ diff → "S33/S34 消费 diff → 只重切落在 affected ranges 的 SFX/BGM 切点"。

注意 S41 的原话是"**重切**"（re-cut），不是"重新生成"。

**S33 的实现：**

`CuePlacement` 是一个纯函数：

```
place(take, cue_revision, edl_revision) → Placement | Unplaceable

Placement = {
  output_start_ms, output_end_ms,
  source_in_ms, source_out_ms,      // take 里用哪一段
  stretch_ratio,                     // 1.0 = 原速
  placement_mode,                    // fixed | stretch | loop | trim | unplaceable
  reason
}
```

**输入**：take 的 `natural_duration_ms`、cue 的 `anchor` 与 `offset_ms`、cue 所属 shot 在该 EDL 里的位置、`sfx_family` 决定的允许模式。

**没有状态。** 同一个 `(take, cue_revision, edl_revision)` 永远得到同一个 placement。这是它可以被随时重算的前提。

**placement_mode 的判定顺序：**

| # | 条件 | 模式 | 说明 |
|---|---|---|---|
| 1 | `natural == requested`（±1 帧） | `fixed` | 原样放 |
| 2 | `natural > requested` 且族允许裁切 | `trim` | 裁 `trim_tail_ms`（或按 anchor 裁头） |
| 3 | `natural < requested` 且族允许拉伸 且 `requested/natural ≤ max_stretch`（默认 1.25） | `stretch` | 变调最小化的时间拉伸，由 S42 执行 |
| 4 | 族是 `ambience` 且 `loopable` | `loop` | 循环填充到所需长度 |
| 5 | 其他 | **`unplaceable`** | 记缺口 `duration_mismatch` |

**第 5 行是核心：`unplaceable` 不重生成，只记缺口。**

一条 `hard_effect`（枪响）需要 3 秒，但素材只有 0.6 秒——拉伸 5 倍会变成一声滑稽的"嗡"。正确做法是记缺口，让人决定：接受 0.6 秒、换素材、或者接受这个音效缺失。**S33 不做这个决定。**

**为什么 `stretch` 需要 `max_stretch`：** 声音的时间拉伸必然伴随音色变化。1.25 倍以内通常听不出来（脚步走快一点）；2 倍以上就明显（门关得很慢、像水下）。这个阈值按族不同：

| 族 | `max_stretch` | 理由 |
|---|---|---|
| `foley`（脚步） | 2.0 | 脚步速度变化是自然的 |
| `ambience`（环境） | 1.0（不用拉伸，用循环） | 拉伸环境音会有明显的相位伪影 |
| `hard_effect`（撞击、关门） | 1.15 | 几乎不能变 |
| `ui` | 1.0 | 界面音效长度是设计定的 |
| `stinger`（强调音） | 1.1 | 与画面节拍绑定 |

### 4.3 `anchor` 与"跟着剪辑走"

一个音效"应该在哪"的三种锚点：

| anchor | 含义 | 剪辑变化时的行为 |
|---|---|---|
| `shot_start` | 贴在所属 shot 的起始帧 | **自动跟随**——shot 挪了，音效跟着挪 |
| `shot_end` | 贴在 shot 的结束帧 | 自动跟随，但音效尾音可能跨到下一个 shot |
| `beat` | 贴在某个 beat 标记上 | 跟随 beat；beat 若被删则该 cue 变 `unplaceable` |
| `absolute` | 固定在输出的某个绝对时间 | **不跟随**（罕见，用于全片统一的音效） |

**默认必须是 `shot_start` 或 `beat`，不能是 `absolute`。**

这是"音效跟着剪辑走"能自动成立的前提：只要 anchor 是相对的，S41 的 EDL 一变，重算 placement 就自动得到新位置——**不需要任何人通知 S33**。

**为什么 `absolute` 要存在：** 少量音效确实固定在成片的绝对时间（比如片头 logo 音、全片统一的转场音）。但这类应该极少，且必须显式声明。

**S33 不做的事：** 不监听 `post.timeline.revision.created` 去批量重算所有 cue。

原因：重算是纯函数，成本几乎为零，而且**任何一次读取都可以现算**。批量预计算会引入"缓存过期"问题——EDL 变了但 placement 没重算，S42 拿到了旧位置。所以 `CuePlacement` 的持久化**只用于查询与可观测**，S42 拿位置时走 `GET /sfx-cues/{id}?edl_revision=N` 现算。

### 4.4 复用：内容指纹与时长需求分开

**`content_fingerprint`：**

```
content_fingerprint = sha256(
    cue_intent
  || sfx_family
  || loopable_required
)
```

**不进指纹的**：`duration_seconds`、`anchor`、`offset_ms`、`shot_lineage_id`、`variation_nonce`、`provider`、`model_id`、`attempt_id`、`manifest_id`、`node_epoch`、`execution_fence`。

**为什么时长不进 `content_fingerprint`：** 因为时长是 §4.2 的 placement 问题，不是内容问题。同一句"沉重的关门声"，需要 0.4 秒还是 0.6 秒，是剪辑的事。

**复用规则：**

```
若 存在同 tenant 的 SfxTake
   满足 take.content_fingerprint == 本请求的 content_fingerprint
   且 take.rights_state == 'usable'
   且 take.state IN ('READY','ACCEPTED')
   且 take.artifact_ref 在 S28 仍 REFERENCEABLE
   且 place(take, cue_revision, 当前 EDL).placement_mode != 'unplaceable'
则 复用该 take（200，reused=true）
   不创建 SfxJob，不花钱
```

**最后一条条件是 S33 独有的，也是最容易漏的。**

一个已有的 0.4 秒枪响，现在需要 3 秒——指纹相同（内容一样）、许可有效、artifact 可读，所以"能复用"。但 placement 会返回 `unplaceable`。

若复用了，S42 拿到一个 `unplaceable` 的 placement，它要么在混音时静默丢掉（用户听不到枪响），要么在那里报错（一个混音阶段的错误，追溯不到 S33）。两种都坏。

所以复用必须检查 placement 可行性，不可行就**走新建**——新建会真正生成一个 3 秒的枪响（或返回 `GAP`，看渠道能力）。

**复用时的返回形状要区分：**

```jsonc
{
  "reused": true,
  "sfx_take_id": "st_01J…",
  "artifact_ref": { … },
  "natural_duration_ms": 600,
  "placement": {
    "placement_mode": "trim",
    "source_in_ms": 0, "source_out_ms": 400,
    "output_start_ms": 8200, "output_end_ms": 8600
  }
}
```

**placement 直接算好返回**，让调用方（S42 或 S41）不需要再算。这也意味着 `GET` 与 `POST` 的复用分支共用同一段 placement 逻辑——只有一份实现。

### 4.5 许可复查：两个时点

| # | 时点 | 目的 | 失败时 |
|---|---|---|---|
| 1 | `CHECKING_POLICY`（提交前） | 确认存在合规通道 | `POLICY_BLOCKED` + 缺口；不花钱 |
| 2 | **每次 take 被使用前**（`GET /sfx-cues/{id}` 现算时、`ready` 事件前） | 确认这个素材现在仍可用 | 标 `rights_state='void'`，置 `SfxGap(rights_lapsed)` |

**为什么时点 2 不能省、且必须是"每次使用前"：**

音效素材的许可状态会独立于生产变化：素材库下架、许可到期、策略收紧（比如"这类音效不再允许用于海外发行"）。这些都不改变 bytes，也不产生 S33 能感知的事件——除了 S02 的 `policy.revision.published`，而那个事件说的是策略，不是"这个素材已经不可用"。

**所以 S33 在每次提供给 S42 的位置计算里，都要带上当前的 `rights_state`。** 若不可用，返回 `unplaceable` + 缺口，而不是返回一个位置。

**这让 S33 的 `GET` 变成一个有副作用的端点**（会写 void 标记）。这是可以接受的：副作用是**收紧**（把可用变成不可用），永不放松。同一个原则见 [S31 §2.3](S31-speech-production.md)。

**S33 不缓存"策略结论"。** `RightsPolicyBinding` 是提交时的快照，只用于解释；放行必须实时。

### 4.6 缺口：标记，还是替代

`service.yaml` `recovery`："**素材不可用则标记缺口，替代需满足既定许可与音效约束**"。

这句话允许替代，但给了两个条件。S33 实现必须把这两个条件做成可检验的：

**替代的四个必要条件（全部满足才允许）：**

| # | 条件 | 检验方式 |
|---|---|---|
| 1 | 同 `sfx_family` | 直接比较枚举 |
| 2 | 替代 take 的 `content_fingerprint` 不同但 `cue_intent` 语义相近 | **不可自动检验** → 必须由 S40 或 S07 显式指定 |
| 3 | 替代 take 的许可满足当前 `rights_policy_ref` | 实时查 S02 |
| 4 | placement 可行（时长可满足） | `place()` |

**第 2 条不可自动检验，这是关键结论：S33 不自动替代。**

一个音效"听起来差不多"是审美判断。自动化系统能做的是"同族里找一个时长合适的"，但它无法判断"这个关门声用在古装场景里是否违和"。

**所以 S33 的替代路径只有一条：请求里携带 `substitute_take_id` 且带显式授权（`substitution_authorized_by`）。** S33 只做条件 1/3/4 的检验，条件 2 由人负责。

**缺口记录 `SfxGap` 的 `kind` 闭集：**

| `kind` | 含义 | 谁能处理 |
|---|---|---|
| `no_licensed_route` | 没有合规的生成通道 | 法务 / 内容负责人 |
| `provider_failed` | 供应商失败且无重试预算 | 制作 |
| `submission_unrecoverable` | UNKNOWN 对账失败 | 制作 |
| `duration_mismatch` | 素材时长无法满足放置（拉伸超限） | 剪辑 / 制作 |
| `rights_lapsed` | 原可用素材许可失效 | 法务 / 内容负责人 |
| `artifact_unavailable` | 素材 tombstone 或不可读 | 制作 / 存储 |
| `family_unavailable` | 该族在当前渠道完全不可用 | 制作 |
| `no_substitute_authorized` | 需要替代但没有授权 | 人工 |

**`SfxGap` 是"可交付性"的输入。** S41 的 `completeness` 与 `formal_master_eligible` 需要知道音效缺口（[S41 §4.4](S41-editorial.md) 的 `gaps[]` 里有 `missing_stem` 等）。S33 通过 `production.sfx.ready` 的扩展字段或单独的缺口查询端点提供（§12 C6）。

**缺口不是失败，是状态。** 一个缺口可以：
- 被 S40 决定"接受这个音效缺失"（记录裁决）
- 被新的 job 填上（缺口关闭）
- 被替代填上（带授权）

**缺口永不被自动关闭。** 素材来了是"缺口有了对应 take"，但关闭是一次显式动作（S40 裁决或新 take 推进检查点后的自动关闭——后者需要明确规则，见 §11 U3）。

### 4.7 可循环性

`loopable_required` 是 S33 的 API 字段（不在契约里，§12 C3）。

**为什么需要：** 环境音（雨、风、人群）需要能无缝循环。生成的素材如果不是设计成可循环的，循环点会有明显的"咔哒"（相位不连续）。

**三种处理：**

| `loopable_required` | 渠道支持可循环 | 结果 |
|---|---|---|
| `true` | 是 | 正常 |
| `true` | 否 | `REJECTED_LOCAL(CAPABILITY_MISSING)`，**不降级为"生成一个长的"** |
| `false` | — | 正常 |

**为什么不降级：** "生成一个足够长的环境音"看起来能解决问题，但它把循环问题变成了成本问题——30 秒的环境音和 3 秒的循环素材差 10 倍成本，而且长素材在剪辑变化时不能复用（时长一变就要重生成）。循环素材才是正确的形态。

**`SfxTake.loopable_verified`** 是 S33 在归档后做的一次检验（检查首尾样本接近度）。这不是 QC，是约束校验——如果声明可循环但实际不可循环，S42 循环时会露馅。

### 4.8 失败矩阵

| # | 场景 | 检测点 | S33 动作 | 状态 | 花钱 | 计数 |
|---|---|---|---|---|---|---|
| F1 | `cue_intent` 空 | 同步校验 | `422 EMPTY_CUE_INTENT` | — | 否 | 否 |
| F2 | `sfx_family` 不在闭集 | 同步校验 | `422 INVALID_SFX_FAMILY` | — | 否 | 否 |
| F3 | `duration_seconds <= 0` 或超上限 | 同步校验 | `422 DURATION_OUT_OF_RANGE` | — | 否 | 否 |
| F4 | `rights_policy_ref` 跨租户 | 同步校验 | `404` | — | 否 | 否 |
| F5 | 策略无可用许可通道 | `CHECKING_POLICY` | `POLICY_BLOCKED` + 缺口 | `POLICY_BLOCKED` | 否 | 否 |
| F6 | 渠道不支持可循环但 `loopable_required` | `PREPARING` | `REJECTED_LOCAL(CAPABILITY_MISSING)`，**不降级** | `REJECTED_LOCAL` | 否 | 否 |
| F7 | 路由 `feasible=false` | `PREPARING` | 透传 `degradation_options` | `REJECTED_LOCAL` | 否 | 否 |
| F8 | 供应商失败 | S26 事件 | `FAILED(PROVIDER_FAILED)` | `FAILED` | 是 | 是 |
| F9 | 供应商政策拒绝 | S26 事件 | `FAILED(POLICY_REJECTED)`，不换渠道 | `FAILED` | 是 | 是 |
| F10 | 账户撤销 | S26 事件 | `FAILED(ACCOUNT_UNAUTHORIZED)`，不换渠道 | `FAILED` | 是 | 是 |
| F11 | 预算不足 | S26 事件 | `FAILED(BUDGET)`，不缩短时长 | `FAILED` | 否 | 是 |
| F12 | `SUBMISSION_UNKNOWN` | 事件 | 冻结 | `WAITING_UNKNOWN` | 保留额度 | 是 |
| F13 | UNKNOWN 对账失败 | 事件 | `GAP_RECORDED(submission_unrecoverable)` | `GAP_RECORDED` | 是 | 是 |
| F14 | 归档不带 `natural_duration_ms` | 事件 | 阻塞 `INGESTING` + 告警 | `INGESTING` | 是 | 是 |
| F15 | 归档 URL 过期 | 事件 | 向 S26 要新 URL，**不重新生成** | `INGESTING` | 否 | 否 |
| F16 | 归档 hash 不符 | 事件 | `FAILED(HASH_MISMATCH)` | `FAILED` | 是 | 是 |
| F17 | 素材时长 0 或不可解码 | 事件 | `FAILED(INVALID_AUDIO)` + 缺口 | `GAP_RECORDED` | 是 | 是 |
| F18 | 许可在归档后失效 | `INGESTING` | take 标 void + 缺口 `rights_lapsed` | `GAP_RECORDED` | 是 | 是 |
| F19 | 时长需求超出素材且不能拉伸 | placement | `unplaceable` + 缺口 `duration_mismatch`，**不重生成** | `READY`（take）/ 缺口 | 否 | 否 |
| F20 | 声明可循环但实际不可循环 | 归档后检验 | `FAILED(LOOPABILITY_UNVERIFIED)` + 缺口 | `GAP_RECORDED` | 是 | 是 |
| F21 | 替代请求无条件 2 的授权 | 同步校验 | `422 SUBSTITUTION_NOT_AUTHORIZED` | — | 否 | 否 |
| F22 | 替代跨族 | 同步校验 | `422 FAMILY_MISMATCH` | — | 否 | 否 |
| F23 | 同 `command_id` 不同 body | 幂等 | `409 IDEMPOTENCY_CONFLICT` | — | 否 | 否 |
| F24 | 死线已过 | 同步校验 | `422 DEADLINE_PASSED` | — | 否 | 否 |
| F25 | 数据驻留禁止 | `PREPARING` | `REJECTED_LOCAL(RESIDENCY_FORBIDDEN)` | `REJECTED_LOCAL` | 否 | 否 |

---

## 5. 数据模型

### 5.1 表清单

| 表 | 作用 | 可变性 |
|---|---|---|
| `sfx_cue_revision` | cue 意图版本 | 不可变 |
| `rights_policy_binding` | 许可策略快照 | 不可变 |
| `sfx_job` | 生成意图 | 状态可变；`content_fingerprint` / 许可绑定不可变 |
| `sfx_take` | 音效素材 | `rights_state` / `state` 可变，内容指纹不可变 |
| `sfx_checkpoint` | 生产基线指针 | 可变（CAS） |
| `sfx_gap` | 缺口记录 | 状态可变（开放/关闭），内容不可变 |
| `cue_placement_cache` | placement 派生缓存 | **可丢弃、可重算**（非权威） |
| `sfx_intent_ledger` | 生产意图台账 | 只追加 |
| `job_transition` | 状态转移历史 | 只追加 |
| `command_receipt` | 命令幂等回执 | 只追加 |
| `outbox` | 事件出站 | 只追加 |
| `inbox` | 事件入站去重 | 只追加 |

### 5.2 核心 DDL

```sql
CREATE SCHEMA IF NOT EXISTS sound_effects;

-- ---------- cue 意图版本（不可变） ----------
CREATE TABLE sound_effects.sfx_cue_revision (
  tenant_id        text NOT NULL,
  cue_id           text NOT NULL,
  cue_revision     integer NOT NULL CHECK (cue_revision >= 1),
  shot_lineage_id  text NOT NULL,
  cue_index        integer NOT NULL CHECK (cue_index >= 0),
  beat_ref         text,
  sfx_family       text NOT NULL
    CHECK (sfx_family IN ('foley','ambience','hard_effect','ui','stinger')),
  cue_intent       text NOT NULL,
  intent_hash      char(64) NOT NULL,
  anchor           text NOT NULL
    CHECK (anchor IN ('shot_start','shot_end','beat','absolute')),
  anchor_offset_ms integer NOT NULL DEFAULT 0,
  loopable_required boolean NOT NULL DEFAULT false,
  source_ref       jsonb NOT NULL,          -- {service_id:'S06'|'S07', id, revision}
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, cue_id, cue_revision),
  CONSTRAINT scr_intent_not_blank CHECK (btrim(cue_intent) <> ''),
  CONSTRAINT scr_hash_len CHECK (length(intent_hash) = 64),
  CONSTRAINT scr_beat_anchor CHECK (
    anchor <> 'beat' OR beat_ref IS NOT NULL),
  CONSTRAINT scr_source_known CHECK ((source_ref->>'service_id') IN ('S06','S07'))
);
CREATE UNIQUE INDEX scr_intent_uniq
  ON sound_effects.sfx_cue_revision (tenant_id, cue_id, intent_hash);

-- ---------- 许可策略快照（不可变） ----------
CREATE TABLE sound_effects.rights_policy_binding (
  rights_policy_binding_id text PRIMARY KEY,
  tenant_id            text NOT NULL,
  policy_ref           jsonb NOT NULL,
  allowed_license_kinds text[] NOT NULL,
  attribution_required boolean NOT NULL,
  territory_restrictions text[] NOT NULL DEFAULT '{}',
  expires_at           timestamptz,
  resolved_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT rpb_ref_known CHECK ((policy_ref->>'service_id') = 'S02'),
  CONSTRAINT rpb_hash_len CHECK (length(policy_ref->>'content_hash') = 64),
  CONSTRAINT rpb_kinds_present CHECK (cardinality(allowed_license_kinds) > 0)
);

-- ---------- 生成意图 ----------
CREATE TABLE sound_effects.sfx_job (
  sfx_job_id         text PRIMARY KEY,
  tenant_id          text NOT NULL,
  project_id         text NOT NULL,
  series_id          text NOT NULL,
  episode_id         text NOT NULL,
  branch_id          text NOT NULL,

  cue_id             text NOT NULL,
  cue_revision       integer NOT NULL,
  shot_lineage_id    text NOT NULL,
  cue_index          integer NOT NULL,
  sfx_family         text NOT NULL,

  requested_duration_ms integer NOT NULL CHECK (requested_duration_ms > 0),
  loopable_required  boolean NOT NULL,

  rights_policy_binding_id text NOT NULL
    REFERENCES sound_effects.rights_policy_binding,

  -- ↓ 冻结：内容指纹不含时长（§4.4）
  content_fingerprint text NOT NULL,

  substitute_take_id     text,
  substitution_authorized_by text,

  node_id            text NOT NULL,
  node_epoch_at_create integer NOT NULL,
  execution_fence    integer NOT NULL,

  request_hash       text NOT NULL,
  idempotency_key    text NOT NULL,
  variation_nonce    text NOT NULL,

  route_decision_ref jsonb,
  budget_scope_id    text NOT NULL,
  deadline           timestamptz NOT NULL,

  provider_job_id    text,
  attempt_id         text,
  business_attempt_no integer NOT NULL DEFAULT 1,
  quote_upper_bound_micro bigint,

  state              text NOT NULL,
  state_seq          bigint NOT NULL DEFAULT 0,
  failure_code       text,
  failure_detail     jsonb,
  reconciled         boolean NOT NULL DEFAULT false,

  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  state_entered_at   timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT sfj_state_check CHECK (state IN (
    'CREATED','CHECKING_POLICY','POLICY_BLOCKED','PREPARING','REJECTED_LOCAL',
    'SUBMITTED','WAITING_PROVIDER','WAITING_UNKNOWN','INGESTING','READY',
    'FAILED','RECONCILED_READY','RECONCILED_FAILED','GAP_RECORDED','CANCELED')),
  CONSTRAINT sfj_tenant_idem UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT sfj_fingerprint_len CHECK (length(content_fingerprint) = 64),
  -- 替代必须同时带授权人：不可自动替代（§4.6）
  CONSTRAINT sfj_substitute_paired CHECK (
    (substitute_take_id IS NULL AND substitution_authorized_by IS NULL)
    OR (substitute_take_id IS NOT NULL AND substitution_authorized_by IS NOT NULL))
);

CREATE UNIQUE INDEX sfx_job_open_uniq
  ON sound_effects.sfx_job
     (tenant_id, shot_lineage_id, cue_index, content_fingerprint, requested_duration_ms)
  WHERE state NOT IN ('READY','POLICY_BLOCKED','GAP_RECORDED','REJECTED_LOCAL',
                      'FAILED','RECONCILED_FAILED','CANCELED');

CREATE INDEX sfx_job_shot_idx
  ON sound_effects.sfx_job (tenant_id, shot_lineage_id, cue_index, created_at DESC);
CREATE INDEX sfx_job_state_idx
  ON sound_effects.sfx_job (state, state_entered_at)
  WHERE state IN ('CHECKING_POLICY','PREPARING','SUBMITTED',
                  'WAITING_UNKNOWN','INGESTING');

-- ---------- 音效素材 ----------
CREATE TABLE sound_effects.sfx_take (
  sfx_take_id        text PRIMARY KEY,
  tenant_id          text NOT NULL,
  sfx_job_id         text NOT NULL REFERENCES sound_effects.sfx_job,
  cue_id             text NOT NULL,
  shot_lineage_id    text NOT NULL,
  cue_index          integer NOT NULL,
  sfx_family         text NOT NULL,

  artifact_ref       jsonb NOT NULL,        -- S28
  natural_duration_ms integer NOT NULL CHECK (natural_duration_ms > 0),
  sample_rate_hz     integer NOT NULL CHECK (sample_rate_hz >= 8000),
  channel_count      integer NOT NULL CHECK (channel_count >= 1),
  container_format   text NOT NULL,

  content_fingerprint char(64) NOT NULL,

  loopable           boolean NOT NULL,
  loopable_verified  boolean NOT NULL DEFAULT false,

  rights_policy_binding_id text NOT NULL
    REFERENCES sound_effects.rights_policy_binding,
  rights_state       text NOT NULL CHECK (rights_state IN ('usable','void','unknown')),
  rights_checked_at  timestamptz,

  provider_job_id    text NOT NULL,
  attempt_id         text NOT NULL,
  source_kind        text NOT NULL CHECK (source_kind IN ('generated','retrieved','substituted')),

  state              text NOT NULL CHECK (state IN ('READY','ACCEPTED','SUPERSEDED')),
  created_at         timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT st_artifact_is_s28 CHECK ((artifact_ref->>'service_id') = 'S28'),
  CONSTRAINT st_hash_len CHECK (length(artifact_ref->>'content_hash') = 64),
  CONSTRAINT st_loop_verified CHECK (NOT loopable_verified OR loopable)
);

CREATE UNIQUE INDEX st_artifact_uniq
  ON sound_effects.sfx_take
     (tenant_id, (artifact_ref->>'id'), (artifact_ref->>'content_hash'));
CREATE INDEX st_cue_idx
  ON sound_effects.sfx_take
     (tenant_id, shot_lineage_id, cue_index, created_at DESC);
CREATE INDEX st_reuse_idx
  ON sound_effects.sfx_take (tenant_id, content_fingerprint)
  WHERE rights_state = 'usable' AND state IN ('READY','ACCEPTED');

-- ---------- 音效检查点 ----------
CREATE TABLE sound_effects.sfx_checkpoint (
  tenant_id       text NOT NULL,
  shot_lineage_id text NOT NULL,
  cue_index       integer NOT NULL,
  sfx_take_id     text NOT NULL REFERENCES sound_effects.sfx_take,
  checkpoint_seq  bigint NOT NULL,
  expected_revision integer NOT NULL DEFAULT 0,
  updated_by_job  text NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, shot_lineage_id, cue_index)
);

-- ---------- 缺口记录 ----------
CREATE TABLE sound_effects.sfx_gap (
  gap_id          uuid PRIMARY KEY,
  tenant_id       text NOT NULL,
  shot_lineage_id text NOT NULL,
  cue_index       integer NOT NULL,
  cue_id          text NOT NULL,
  sfx_family      text NOT NULL,
  kind            text NOT NULL CHECK (kind IN (
    'no_licensed_route','provider_failed','submission_unrecoverable',
    'duration_mismatch','rights_lapsed','artifact_unavailable',
    'family_unavailable','no_substitute_authorized')),
  detail          jsonb NOT NULL,
  source_job_id   text,
  state           text NOT NULL CHECK (state IN ('open','closed','waived')),
  closed_by_take_id text,
  waived_decision_id text,                 -- S40 的裁决引用
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  -- open 的缺口不能同时有闭合依据
  CONSTRAINT sg_state_consistent CHECK (
    (state = 'open' AND closed_by_take_id IS NULL AND waived_decision_id IS NULL)
    OR (state = 'closed' AND closed_by_take_id IS NOT NULL)
    OR (state = 'waived' AND waived_decision_id IS NOT NULL))
);
CREATE INDEX sg_open_idx
  ON sound_effects.sfx_gap (tenant_id, shot_lineage_id, cue_index)
  WHERE state = 'open';

-- ---------- placement 缓存（非权威，可丢弃） ----------
CREATE TABLE sound_effects.cue_placement_cache (
  tenant_id       text NOT NULL,
  sfx_take_id     text NOT NULL,
  edl_revision    integer NOT NULL,
  placement_mode  text NOT NULL CHECK (placement_mode IN
    ('fixed','trim','stretch','loop','unplaceable')),
  output_start_ms integer,
  output_end_ms   integer,
  source_in_ms    integer,
  source_out_ms   integer,
  stretch_ratio   numeric(6,4),
  reason          text,
  computed_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, sfx_take_id, edl_revision)
);
-- 这张表可以被 TRUNCATE，不影响任何正确性（§4.2）

-- ---------- 生产意图台账 ----------
CREATE TABLE sound_effects.sfx_intent_ledger (
  entry_id        uuid PRIMARY KEY,
  tenant_id       text NOT NULL,
  sfx_job_id      text NOT NULL,
  shot_lineage_id text NOT NULL,
  cue_index       integer NOT NULL,
  action          text NOT NULL CHECK (action IN ('generate','fill_gap')),
  sfx_family      text NOT NULL,
  run_id          text NOT NULL,
  node_epoch      integer NOT NULL,
  manifest_id     text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sil_shot_idx
  ON sound_effects.sfx_intent_ledger (tenant_id, shot_lineage_id, created_at DESC);

-- ---------- 状态转移历史 ----------
CREATE TABLE sound_effects.job_transition (
  sfx_job_id  text NOT NULL,
  seq         bigint NOT NULL,
  from_state  text,
  to_state    text NOT NULL,
  reason_code text,
  detail      jsonb,
  at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sfx_job_id, seq)
);

-- ---------- 基础设施 ----------
CREATE TABLE sound_effects.command_receipt (
  tenant_id     text NOT NULL,
  command_id    text NOT NULL,
  request_hash  char(64) NOT NULL,
  response_body jsonb NOT NULL,
  completed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);
CREATE UNIQUE INDEX srec_hash_uniq
  ON sound_effects.command_receipt (tenant_id, command_id, request_hash);

CREATE TABLE sound_effects.outbox (
  outbox_id          uuid PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,             -- 见 §6.4 事件表
  aggregate_sequence bigint NOT NULL,
  event_type         text NOT NULL,
  partition_key      text NOT NULL,        -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  published_at       timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx
  ON sound_effects.outbox (outbox_id) WHERE published_at IS NULL;

CREATE TABLE sound_effects.inbox (
  consumer     text NOT NULL,
  event_id     text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  PRIMARY KEY (consumer, event_id)
);
```

**索引命名前缀：** `scr_` 用于 `sfx_cue_revision`，`srec_` 用于 `command_receipt`，`sfj_` 用于 `sfx_job`，`st_` 用于 `sfx_take`，`sg_` 用于 `sfx_gap`，`rpb_` 用于 `rights_policy_binding`。同一 schema 内索引名不可重复，实现时按此前缀。

**为什么 `cue_placement_cache` 可以被 TRUNCATE：** 它是纯派生数据。任何读取都应当能现算。缓存只是为了在"一集 200 条 cue、S42 逐条拉取"时避免 200 次重算。丢了只是慢一点，不会错。

**这张表的存在本身有风险：** 有人会把它当权威，在 EDL 变了之后忘记失效。防御手段是**所有对外读取都不查这张表**，只在内部批量计算时用它；`GET` 端点一律现算。§11 U1 记录了这个权衡。

### 5.3 约束理由

| 约束 | 防的是什么 |
|---|---|
| `sfj_tenant_idem` | E12 |
| `sfx_job_open_uniq` | E16；同一内容 + 同一时长需求并发双提交 |
| `sfj_substitute_paired` | **替代必须有人授权**（§4.6）。这条把"自动找个差不多的"从数据层面堵死 |
| `st_artifact_is_s28` | 供应商直链不能进 `artifact_ref` |
| `st_loop_verified` | 声明不可循环却标已验循环 |
| `sg_state_consistent` | 缺口说"已闭合"却没有闭合依据 |
| `rpb_kinds_present` | 空许可集合被当成"没有限制"——它应该是"什么都不能用"，所以构造时直接拒绝 |
| `scr_beat_anchor` | `anchor='beat'` 但没有 `beat_ref`，会导致 placement 无从计算 |
| `scr_intent_not_blank` | F1 |
| `requested_duration_ms > 0` | 契约已保证，但 DoS 防御 |
| `natural_duration_ms > 0` | 允许 0 会退化成假 `READY` |

**`rpb_kinds_present` 值得展开：** 一个空的 `allowed_license_kinds` 有两种读法——"没有限制"（宽松）或"什么都不能用"（严格）。歧义是危险的：宽松读法会让所有素材都可用。

S33 的立场是**严格**：空集合表示"没有任何许可可用"，所以它在构造时就被拒绝（`rpb_kinds_present`）。若 S02 真的返回空，S33 走 `POLICY_BLOCKED`。

---

## 6. 对外契约

### 6.1 `POST /sfx-jobs`

```jsonc
{
  "command_id": "cmd-sfx-0001",
  "request_hash": "sha256:…",
  "context": { /* execution-context.v2 */ },
  "cue": {
    "cue_id": "cue_ep3_sc3_02_door",
    "cue_revision": 2,
    "shot_lineage_id": "lin_ep3_sc3_02",
    "cue_index": 0,
    "beat_ref": "beat_door_close",
    "sfx_family": "hard_effect",
    "cue_intent": "厚重的木门快速关上，带门闩落下的咔嗒声",
    "anchor": "beat",
    "anchor_offset_ms": 0,
    "loopable_required": false
  },
  "duration_seconds": 0.6,
  "rights_policy_ref": { "service_id": "S02", "id": "pol_sfx_standard", "revision": 4, "content_hash": "…" },
  "substitute_take_id": null,
  "substitution_authorized_by": null,
  "route_policy_ref": { "service_id": "S25", "id": "routing_demo", "revision": 1, "content_hash": "…" },
  "budget_scope_id": "budget_ep3",
  "deadline": "2026-11-03T23:30:00+08:00"
}
```

响应 `202`：

```jsonc
{
  "sfx_job_id": "sfj_01J…",
  "state": "CREATED",
  "operation_id": "op_…",
  "content_fingerprint": "…",
  "requested_duration_ms": 600
}
```

复用 `200`：

```jsonc
{
  "sfx_job_id": null,
  "reused": true,
  "sfx_take_id": "st_01J…",
  "artifact_ref": { "service_id": "S28", "id": "…", "revision": 1, "content_hash": "…" },
  "natural_duration_ms": 640,
  "placement": {
    "placement_mode": "trim",
    "source_in_ms": 0,
    "source_out_ms": 600,
    "output_start_ms": 8200,
    "output_end_ms": 8800,
    "edl_revision": 10
  },
  "note": "content_fingerprint 命中已有 take；未创建付费任务（§4.4）"
}
```

**`placement` 在复用响应里就返回，且必须带 `edl_revision`。** 不带 `edl_revision` 的 placement 是无法解释的——调用方不知道它是按哪版剪辑算的。

### 6.2 `GET /sfx-cues/{id}`

注意路径**不是** `/sfx-jobs/{id}`——这是 `service.yaml` 的声明（`command: POST /sfx-jobs`, `query: GET /sfx-cues/{id}`）。这个不对称是刻意的或至少是既成事实：**cue 是查询的主体，job 只是生产它在的一次尝试**。

一条 cue 可能对应多个 job（多次尝试）、多个 take（多个候选）、零或一个缺口。所以查询以 cue 为主体。

```jsonc
{
  "cue_id": "cue_ep3_sc3_02_door",
  "cue_revision": 2,
  "shot_lineage_id": "lin_ep3_sc3_02",
  "cue_index": 0,
  "sfx_family": "hard_effect",
  "intent": {
    "cue_intent": "厚重的木门快速关上，带门闩落下的咔嗒声",
    "intent_hash": "…",
    "anchor": "beat",
    "anchor_offset_ms": 0,
    "loopable_required": false
  },
  "checkpoint": {
    "sfx_take_id": "st_01J…",
    "checkpoint_seq": 2
  },
  "take": {
    "sfx_take_id": "st_01J…",
    "artifact_ref": { "…": "…" },
    "natural_duration_ms": 640,
    "loopable": false,
    "loopable_verified": false,
    "rights_state": "usable",
    "rights_checked_at": "2026-11-02T09:00:00Z",
    "source_kind": "generated",
    "state": "READY"
  },
  "placement": {
    "edl_revision": 10,
    "placement_mode": "trim",
    "source_in_ms": 0,
    "source_out_ms": 600,
    "output_start_ms": 8200,
    "output_end_ms": 8800,
    "stretch_ratio": 1.0
  },
  "gap": null,
  "history": [
    { "sfx_job_id": "sfj_00X…", "state": "FAILED", "failure_code": "PROVIDER_FAILED", "at": "…" },
    { "sfx_job_id": "sfj_01J…", "state": "READY", "at": "…" }
  ]
}
```

**`placement` 必须接受 `?edl_revision=` 参数**，缺省用当前的 EDL revision。不传就返回"按最新已知 EDL 算的"，并在响应里标明 revision。

**`gap` 与 `take` 可以同时非 null**：一个已闭合的缺口 + 填上它的 take。这样查询能同时看到"曾经缺过"和"现在有了"。

### 6.3 其他查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/sfx-cues?shot_lineage_id=&family=&has_gap=` | 列表 |
| GET | `/sfx-cues?episode_id=&has_gap=true` | **一集的全部缺口**（S41 的 `completeness` 输入） |
| GET | `/shots/{shot_lineage_id}/sfx-checkpoint` | 该镜全部 cue 的基线（S42 的主读口） |
| GET | `/sfx-takes/{id}` | take 详情 + 许可依据 |
| POST | `/sfx-cues/{id}/placement:recompute?edl_revision=` | 强制重算并写入缓存 |
| POST | `/sfx-gaps/{id}:waive` | 需 S40 裁决引用；S33 不自己 waive |
| POST | `/sfx-jobs/{id}:cancel` | 取消 |
| POST | `/sfx-jobs/{id}:retry` | 仅 `{FAILED, RECONCILED_FAILED}`；`POLICY_BLOCKED` / `WAITING_UNKNOWN` / `GAP_RECORDED` 时 **409** |

### 6.4 事件

**发布：**

| event_type | aggregate | 何时 |
|---|---|---|
| `production.sfx.ready` | `shot_lineage_id` | 检查点推进 |
| `sfx.job.state-changed`（建议新增） | `sfx_job_id` | 每次转移 |
| `sfx.gap.recorded`（建议新增） | `shot_lineage_id` | 缺口产生 |
| `sfx.gap.closed`（建议新增） | `shot_lineage_id` | 缺口闭合 |
| `sfx.take.rights-lapsed`（建议新增） | `sfx_take_id` | take 变 void |

`production.sfx.ready` 的 `data`：

```jsonc
{
  "sfx_job_id": "sfj_01J…",
  "sfx_take_id": "st_01J…",
  "cue_id": "cue_ep3_sc3_02_door",
  "cue_revision": 2,
  "shot_lineage_id": "lin_ep3_sc3_02",
  "cue_index": 0,
  "sfx_family": "hard_effect",
  "artifact_ref": { "service_id": "S28", "id": "…", "revision": 1, "content_hash": "…" },
  "natural_duration_ms": 640,
  "loopable": false,
  "rights_state": "usable",
  "source_kind": "generated",
  "checkpoint": { "from": "st_00X…", "to": "st_01J…", "checkpoint_seq": 2 },
  "content_fingerprint": "…",
  "gap_state": "none"
}
```

**不含**：音频 URL、`cue_intent` 原文（可能含未公开剧情描述）、供应商信息、许可策略明细、归属署名文本。

**`cue_intent` 为什么不进事件：** 它是创作意图的自然语言描述，往往直接泄露剧情（"主角发现尸体时的一声闷响"）。事件会流经 Kafka、被多个消费者存档。需要看意图的人走 S33 的受控读口。

**消费：**

| 来源 | 事件 | 用途 |
|---|---|---|
| S26 | `generation.job.state-changed` | 驱动状态机 |
| S28 | `artifact.ingested` | 完成 `INGESTING → READY`；**需要 `natural_duration_ms`、采样率、通道数、`ingesting_service`**（§12 C4） |
| S02 | `policy.revision.published` | 重新解析许可；**可能导致 take 变 void** |
| S41 | `post.timeline.revision.created` | 只读。**不批量重算 placement**（§4.3）；只让缓存失效 |
| S06 / S07 | `shot.spec.revised`（若存在） | cue 意图变更 → 新 revision |
| S39 | `quality.repair.planned` | 只读；S33 不是任何动作族的执行者，此事件通常与其无关 |
| S08 | `snapshot.manifest.committed` | 只读投影 |

**S33 不消费 `quality.*` 来自动重做。** 它没有对应的动作，也无法判断音效"对不对"。

---

## 7. 幂等与并发

### 7.1 幂等

| 层面 | 键 | 冲突 |
|---|---|---|
| 命令 | `tenant + command_id + request_hash` | 同体返回原响应；异体 `409` |
| 业务 | `(tenant, shot_lineage_id, cue_index, content_fingerprint, requested_duration_ms)` 未终结 | 返回已有 job |
| 复用 | `(tenant, content_fingerprint)` + placement 可行 | 返回已有 take |
| 替代 | `(tenant, substitute_take_id, cue_id, cue_revision)` | 返回首次结果 |
| 事件入站 | `(consumer, event_id)` | 丢弃重复 |

**业务幂等的键里包含 `requested_duration_ms`，而 `content_fingerprint` 不包含它。** 这是刻意的：

- 两个请求内容相同、时长都是 600ms → 是同一次生产意图，业务幂等命中
- 两个请求内容相同、时长一个 600ms 一个 3000ms → **不是**同一次生产意图，但可以复用同一素材（若裁切可行）

前者防止重复提交，后者允许复用。两个机制用不同的键，各司其职。

### 7.2 并发

| 场景 | 机制 |
|---|---|
| 同 cue 两个 job 同时 READY | `sfx_checkpoint` CAS（按 `cue_index`） |
| 同镜多个 cue 并发 | 互不影响 |
| 同一缺口被两个 job 同时闭合 | `sfx_gap` 行锁；先到的写 `closed_by_take_id`，后到的读后跳过 |
| EDL 变更与 `GET` 并发 | `GET` 现算，不读缓存，天然一致 |
| 许可变更与 `ready` 同时到 | **void 优先**：`READY` 事务内重读 `rights_state` |
| 替代请求与缺口闭合并发 | 缺口行锁 |

### 7.3 重启恢复

- `CHECKING_POLICY` 重启后**重做**（策略可能已变）。
- `PREPARING` 重启后重取 RouteDecision。
- `SUBMITTED` / `WAITING_PROVIDER` 重启后向 S26 查状态。
- `INGESTING` 重启后重新探测 `natural_duration_ms` 与循环性。
- `WAITING_UNKNOWN` 重启后什么都不做。
- `POLICY_BLOCKED` / `GAP_RECORDED` 重启后什么都不做（终态）。
- placement 缓存**不需要恢复**（可重算）。

---

## 8. 跨 Run 尝试计数

S33 不是任何动作族的执行者（§1.4），所以它几乎不参与 S39 的上限计数。但有两处会污染计数，必须防住。

### 8.1 剪辑驱动的时长变化不是"重做"

这是最容易造成台账虚高的一处。

```
Run 1: 镜头 4.0 秒，cue 需要 0.6 秒音效 → 生成
Run 2: 镜头变 3.6 秒，cue 需要 0.6 秒音效（内容没变）
       → content_fingerprint 相同 → 复用，placement 变 trim
       → 台账 0 行
Run 3: 镜头变 3.2 秒，可用空间只有 0.3 秒
       → 裁切后只剩 0.3 秒，可能不再"够响"或"够完整"
       → 但 content_fingerprint 仍相同 → 仍复用，placement 变 trim(0.3s)
       → 台账 0 行
```

**如果 S33 把第 2、3 轮记成 `generate`，一集里剪辑动 20 次，音效台账就虚增 20 倍。**

**S33 的规则：** `sfx_intent_ledger` 只在**真正提交 S26 并产生付费意图**时写。复用、裁切、重算 placement、记缺口，**都不写台账**。

### 8.2 缺口填补要写台账，但不是"重做"

一条 `GAP_RECORDED` 的 cue 后来被填上（新 take），这次填补是一次真实的生产意图，`action='fill_gap'`。

**它算不算消耗该镜头的 attempt 额度？** S39 的键是 `(tenant, series, branch, shot_lineage_id, strategy_class)`，而音效不在任何动作族里，没有对应的 `strategy_class`。

**当前处理：** S33 如实写 `action='fill_gap'` 与 `shot_lineage_id`，**不构造 `strategy_class`**（因为不存在）。S39 若要把音效缺口纳入预算，需要自己定义规则（§11 U4、§12 C7）。

**这与 S31/S32 不同，是刻意的不对称。** S31/S32 是动作族的执行者，它们的生产意图直接对应 S39 的一格；S33 不是，所以它不该伪造一个策略类去挤进那个台账。

### 8.3 例子

| Run | 事件 | 台账 | S39 累计 |
|---|---|---|---|
| Run 1 | 首轮 200 条 cue 生成 | 200 行 `generate` | 无关（音效不在族里） |
| Run 2 | 剪辑微调，全部复用 | **0 行** | 不变 |
| Run 3 | 导演改了 5 条 cue 的意图 | 5 行 `generate` | 无关 |
| Run 4 | 3 条 cue 许可失效 | 0 行，3 条缺口 | 无关 |
| Run 5 | 填补 2 条缺口 | 2 行 `fill_gap` | 无关 |

**"无关"不是"不记账"**，而是 S33 的账本不参与 S39 的 attempt 上限。成本仍然进 S23，那才是钱的地方。

---

## 9. 可观测

| 指标 | 类型 | 用途 | 告警 |
|---|---|---|---|
| `s33_jobs{state}` | gauge | 堆积 | `WAITING_UNKNOWN` > 0 |
| `s33_policy_blocked_total{family}` | counter | 许可阻塞 | 突增 = 策略收紧或配置错误 |
| `s33_gap_open{kind,family}` | gauge | **开放缺口数** | 任何 `open` 缺口存在超过 1 小时 |
| `s33_gap_recorded_total{kind}` | counter | 缺口产生速率 | `rights_lapsed` 突增 = 素材库有批量下架 |
| `s33_placement_mode{mode,family}` | counter | 放置模式分布 | `unplaceable` 突增 = 剪辑与素材时长严重不匹配 |
| `s33_stretch_ratio` | histogram | 拉伸比分布 | 超过 `max_stretch` 的比例应恒为 0（超了就变 `unplaceable`） |
| `s33_reuse_hit_ratio` | gauge | 复用率 | 剪辑频繁变动时应接近 1 |
| `s33_generated_for_duration_only_total` | counter | **仅因时长而重生成** | 恒为 0；非 0 说明 §4.4 的复用检查有漏洞 |
| `s33_paid_calls_total` | counter | 付费调用 | 与 S26 按 `modality=sound_effect` 的计数每日对齐 |
| `s33_retrieved_total` | counter | 素材检索次数 | 若检索走 S26，应与 `modality=sound_effect_search` 对齐 |
| `s33_loopability_unverified_total` | counter | 声明可循环但验不过 | 任何一次都告警 |
| `s33_rights_recheck_void_total` | counter | 使用前发现直接失效 | 突增 = 有批量素材下架 |
| `s33_take_rights_stale_age_seconds` | histogram | 距上次许可复查的时长 | p99 过长 = 复查不勤 |
| `s33_cache_hit_ratio` | gauge | placement 缓存命中 | 仅内部指标；**不用于正确性判断** |
| `s33_assertion_failures_total{kind}` | counter | 内部断言 | 任何一次都告警 |

**`s33_generated_for_duration_only_total` 是这一节最重要的指标。** 它是 §4.4 复用的守门人：任何一次"因为时长需求变了就重新生成"都说明复用路径没走通，而这会直接表现为成本翻倍。

**`s33_cue_placement_cache` 的命中率不参与正确性告警。** 若它归零，系统只是慢，不是错。这条指标存在只是为了让运维知道缓存有没有在起作用。

### 9.2 追踪

`traceparent` 贯穿 `POST /sfx-jobs → S02 → S25 → S26 → S27 → S28 → production.sfx.ready → S41 → S42`。

必须能回答："这条 cue 要什么、什么时候生成、用的什么素材、许可依据是什么、现在放在哪一版的哪个位置、为什么是这个位置"。

placement 计算必须在 trace 里有自己的 span，带 `edl_revision`、`placement_mode`、`reason`。否则"为什么它被裁成 0.3 秒"永远查不清。

### 9.3 日志

不记 `cue_intent` 原文（剧情泄露）、不记媒体 URL、不记许可策略明细、不记归属署名文本。

记：`cue_id`、`cue_index`、`intent_hash`、`sfx_family`、`natural_duration_ms`、`requested_duration_ms`、`placement_mode`、`stretch_ratio`、`rights_state`、`gap_kind`。

---

## 10. 测试清单

| 编号 | 场景 | 期望 | E |
|---|---|---|---|
| T33-01 | 同 command 重复 | 返回原响应 | E12 |
| T33-02 | 同 command 异 body | `409` | E12 |
| T33-03 | `cue_intent` 空 | `422 EMPTY_CUE_INTENT` | E24 |
| T33-04 | `sfx_family` 不在闭集 | `422 INVALID_SFX_FAMILY` | E24 |
| T33-05 | `duration_seconds <= 0` | `422 DURATION_OUT_OF_RANGE` | E24 |
| T33-06 | `duration_seconds` 超上限 | `422 DURATION_OUT_OF_RANGE` | E35 |
| T33-07 | 策略无可用许可 | `POLICY_BLOCKED` + `SfxGap(no_licensed_route)`，不花钱 | E30 |
| T33-08 | S02 返回空 `allowed_license_kinds` | DB 拒绝构造 binding → `POLICY_BLOCKED` | E30 |
| T33-09 | 渠道不支持可循环但要求可循环 | `REJECTED_LOCAL(CAPABILITY_MISSING)`，**不降级** | E18 |
| T33-10 | 内容相同、时长相同，重跑 | 复用，`paid_calls=0`，台账 0 行 | E38 |
| T33-11 | 内容相同、时长变短（裁切可行） | 复用，`placement_mode='trim'`，台账 0 行 | E38 |
| T33-12 | 内容相同、时长变长（拉伸在限内） | 复用，`placement_mode='stretch'` | E38 |
| T33-13 | 内容相同、时长变长（拉伸超限） | **不复用**，走新 job | E38 |
| T33-14 | 内容相同、时长变长（拉伸超限）且渠道无能力 | 缺口 `duration_mismatch`，**不重生成** | E38 |
| T33-15 | 剪辑只改位置，不改时长 | 复用，placement 重算，`paid_calls=0` | E38 |
| T33-16 | 剪辑改镜头顺序 | 所有 cue 的 placement 重算，零生成 | E38 |
| T33-17 | `anchor='shot_start'`，shot 前移 2 秒 | placement `output_start_ms` 跟随 -2000 | E38 |
| T33-18 | `anchor='beat'`，beat 被删 | `placement_mode='unplaceable'`，缺口 | E38 |
| T33-19 | `anchor='absolute'`，剪辑变动 | placement 不变 | E38 |
| T33-20 | `ambience` 需要 30 秒，素材 5 秒，可循环 | `placement_mode='loop'` | E38 |
| T33-21 | `hard_effect` 需要 3 秒，素材 0.6 秒 | `unplaceable` + 缺口，**不拉伸 5 倍** | E38 |
| T33-22 | `foley` 需要 2 秒，素材 1 秒（拉伸 2.0，在限内） | `stretch` | E38 |
| T33-23 | `ambience` 需要 8 秒，素材 5 秒（拉伸 1.6 超限） | `loop`（族允许循环优先于拉伸） | E38 |
| T33-24 | 声明 `loopable=true` 但首尾不连续 | `GAP_RECORDED(loopability_unverified)` | E24 |
| T33-25 | 许可在归档后失效 | take 标 void，缺口 `rights_lapsed` | E30 |
| T33-26 | 许可在 `GET` 时发现失效 | `placement_mode='unplaceable'`，返回缺口 | E30 |
| T33-27 | 替代请求无条件 2 授权 | `422 SUBSTITUTION_NOT_AUTHORIZED` | E30 |
| T33-28 | 替代跨族 | `422 FAMILY_MISMATCH` | E30 |
| T33-29 | 替代同族 + 有授权 + 许可可用 | 接受，`source_kind='substituted'` | E30 |
| T33-30 | 缺口被新 take 闭合 | `SfxGap.state='closed'`，`closed_by_take_id` 非空 | E24 |
| T33-31 | 缺口被 S40 waive | `state='waived'`，`waived_decision_id` 非空 | E40 |
| T33-32 | 缺口无依据就被标 closed | DB 约束拒绝 | E24 |
| T33-33 | S26 `SUBMISSION_UNKNOWN` | `WAITING_UNKNOWN`，不发新请求 | E16 |
| T33-34 | UNKNOWN 对账失败 | `GAP_RECORDED(submission_unrecoverable)` | E16 |
| T33-35 | 归档 URL 过期 | 向 S26 要新 URL，**不重新生成** | E23 |
| T33-36 | 归档不带 `natural_duration_ms` | 阻塞 `INGESTING` + 告警 | E23 |
| T33-37 | 归档 hash 不符 | `FAILED(HASH_MISMATCH)` | E23 |
| T33-38 | `artifact_ref.service_id='elevenlabs'` | DB 拒绝 | E01 |
| T33-39 | 跨租户 cue / take / policy | `404` | E28 |
| T33-40 | 检查点并发 CAS | 一个成功一个重读 | E20 |
| T33-41 | 许可变更与 `ready` 并发 | void 优先 | E30 |
| T33-42 | 重启于 `CHECKING_POLICY` | 重做许可解析 | E13 |
| T33-43 | 重启于 `INGESTING` | 重新探测时长与循环性 | E13 |
| T33-44 | 预算不足 | `FAILED(BUDGET)`，不缩短时长 | E14 |
| T33-45 | 配额拒绝 | `FAILED(QUOTA)`，S23 已补偿 | E14 |
| T33-46 | 账户撤销 | `FAILED(ACCOUNT_UNAUTHORIZED)`，不换渠道 | E18 |
| T33-47 | 数据驻留禁止 | `REJECTED_LOCAL(RESIDENCY_FORBIDDEN)` | E31 |
| T33-48 | Kafka 重放 `production.sfx.ready` | 幂等消费 | E32 |
| T33-49 | TRUNCATE placement 缓存后查询 | 结果不变（现算） | E13 |
| T33-50 | 独立升级回滚 | 表驱动状态机一致 | E01 |
| T33-51 | `GET /sfx-cues?episode_id=&has_gap=true` | 返回该集全部开放缺口 | E37 |
| T33-52 | 台账：复用/裁切/重算 | **不写台账** | E26 |
| T33-53 | 台账：真实生成 | 写 `generate` | E26 |

---

## 11. 实施顺序与未决

### 11.1 顺序（W3）

1. 表 + 状态机 + 全部 CHECK（T33-01/02/32/38/40）。
2. `content_fingerprint` 与复用（T33-10…12、52）。
3. **placement 纯函数 + 族策略表**（T33-15…23）。这是 S33 的核心逻辑，应在任何供应商接入前做完，并作为独立单测覆盖。
4. `SfxGap` 全生命周期（T33-07、24、30…32）。
5. S02 对接（mock）：许可解析、空集合、失效（T33-07/08/25/26/41）。
6. S25（mock）：可循环能力（T33-09）。
7. S26（mock）：预算/配额/UNKNOWN（T33-33/34、44/45/46）。
8. S28（mock）：时长、循环性、URL 过期、hash（T33-35…37）。
9. 替代路径（T33-27…29）。
10. 缺口汇总端点（T33-51），供 S41 用。
11. 巡检、告警、runbook。
12. 真实渠道需授权。

### 11.2 未决

| # | 问题 | 当前处理 |
|---|---|---|
| U1 | placement 缓存要不要存在 | 当前存在但非权威，所有对外读现算。若性能不可接受，需重新设计失效策略 |
| U2 | `sfx_family` 闭集由谁定义 | 当前 S33 内部定五类（§2.3），契约里没有（§12 C1） |
| U3 | 缺口何时自动关闭 | 当前：新 take 推进检查点时关闭。但"关闭"应否需要人确认（*音效有了，但它对了吗*）未定。默认自动关闭，记审计 |
| U4 | 音效缺口是否消耗 attempt 额度 | 当前不消耗（S33 不在动作族里）。若 S39 要把音效纳入预算，需要新规则（§12 C7） |
| U5 | 素材检索是否走 S26 | 当前设计上要求走（§1.3），但 `modality` 没有 `sound_effect_search` 值（§12 C2） |
| U6 | 一个 cue 多个候选（导演挑） | 当前不支持（一 job 一 take）。若需要，走多次独立 job + S40 |
| U7 | 音效是否需要跟画面动作帧精确对齐（foley） | 当前只支持 `anchor` 到 shot/beat。逐帧对齐需要 S37 类的分析，未规划 |
| U8 | 长环境音的循环点由谁确定 | 当前由 S42 循环时决定；S33 只保证"素材可循环" |
| U9 | 缺口在 S41 的 `completeness` 里如何映射 | 当前通过 `GET /sfx-cues?episode_id=&has_gap=true` 暴露，由 S41 自行映射（§12 C6） |

---

## 12. 冲突与契约缺口

### C1 `sfx_family` 在契约里不存在

`sound_effect.input` 是 `{cue_intent, duration_seconds, rights_policy_ref}`，`additionalProperties: false`。**没有族、没有 anchor、没有 loopable。**

**冲突点**：§2.3 说明了族是定位策略、替代策略、缺口评估、策略类归一四个用途的前提。没有它，S33 要么从自由文本猜（不可靠且不可复现），要么放弃这四项能力。

**第二个缺口**：`anchor` 完全缺失，意味着契约隐含"音效钉在某处"但没有说是哪。§4.3 的"跟着剪辑走"在没有 anchor 时无从实现——只能假设 `absolute`，而那正是最坏的默认（剪辑一变，全部音效错位）。

**不选边**：本文在 S33 自己的 API 上要求 `sfx_family`、`anchor`、`loopable_required`（§6.1），在组命令时丢弃。

**建议**：`sound_effect.input` 补 `sfx_family`（枚举）、`anchor`（枚举）、`loopable`（布尔）。这三项是 S33 能不能"不重生成而重定位"的前提。

### C2 `modality` 没有素材检索的值

`generation-command.v2` 的 `modality` 枚举是 `video|image|speech|text|understanding|lipsync|sound_effect|music|training`。

**冲突点**：§1.3 论证了素材检索也是一次付费外部调用，必须走 S26。但没有任何 `modality` 值对应"检索既有素材"。若强行用 `sound_effect`，S23/S24/S25 会按"生成"计价与路由，能力模型完全不同（检索需要的是素材库索引，不是生成能力）。

**不选边**：当前 S33 的设计只做生成，不做检索。检索路径在本文中作为"若将来支持"的说明，不落地。

**建议**：若产品需要素材检索，`modality` 补 `asset_search`，并在 S24 单独登记检索渠道的能力与价格。

### C3 `duration_seconds` 是必填，但剪辑变化时它是从属量

`sound_effect.input.duration_seconds` 是 required 且 `exclusiveMinimum: 0`。

**冲突点**：在 S33 的模型里，`duration_seconds` 是**时间线约束**（§2.4），主要由剪辑决定，不是创作输入。把它设成必填且是生成命令的一部分，会导致每次剪辑变化都要发一次新的生成命令——正是 §4.4 要避免的。

**不选边**：S33 在自己的 API 上把它拆成"内容指纹（不含时长）"与"时长需求（单独传）"，在组命令时合并回 `duration_seconds`。

**建议**：`sound_effect.input` 区分 `natural_duration_seconds`（内容需求，进指纹）与 `target_duration_seconds`（时间线约束，不进）。或者明确 `duration_seconds` 是"目标放置长度"，允许生成结果短于它。

### C4 `artifact.ingested` 没有 payload schema

同 [S31 §12 C2](S31-speech-production.md) / [S32 §12 C4](S32-lipsync.md)。S33 的 `INGESTING → READY` 依赖 `natural_duration_ms`、`sample_rate_hz`、`channel_count`、`container`、`ingesting_service`。

**S33 特有的一项**：`loopable` 的循环点信息（首尾样本是否连续、循环点在哪一毫秒）。S28 不太可能探测这个，所以 S33 在归档后自己算（§4.7）。但如果 S28 能提供波形/采样信息会更快。

**不选边**：按 S28 提供基础字段实现，循环性 S33 自己验。

**建议**：同 S31 C2，一次补齐。

### C5 `rights_policy_ref` 没有解析结果，S33 无法在提交前判断可行性

`rights_policy_ref` 是一个引用。S33 必须调用 S02 才能知道"这个策略允许哪些许可类型"。

**冲突点**：`docs/` 没有定义"S02 如何回答这个问题"的接口。S33 在 `CHECKING_POLICY` 需要一个"给定策略 + 音效族 → 是否有合规通道"的查询。

**不选边**：本文假设 S02 提供某种解析能力（`allowed_license_kinds` / `attribution_required` / `territory_restrictions`），并在 `RightsPolicyBinding` 里记录。若 S02 无此接口，S33 只能把 `rights_policy_ref` 原样透传给 S26，此时"合规"完全依赖供应商侧声明，S33 无法在提交前拦截。

**建议**：S02 补一个策略解析读口（类似 S15 的 `check-eligibility`），返回解析后的约束集。这是 S33、S34 共同依赖的接口。

### C6 缺口如何进入 S41 的 `completeness`

[S41 §4.4](S41-editorial.md) 的 `gaps[]` 有 `kind ∈ {missing_artifact, hash_mismatch, missing_subtitle_track, missing_stem, insufficient_handles, rights_expired}`。

**冲突点**：这个 `kind` 集合里**没有"音效缺失"**。S33 的 `SfxGap.kind` 有八种，都不在这个集合里。而"这一集缺一声枪响"直接影响 `formal_master_eligible`（[S41 §4.4](S41-editorial.md)：`incomplete` 时 `formal_master_eligible=false`）。

**不选边**：S33 通过 `GET /sfx-cues?episode_id=&has_gap=true` 暴露缺口，不试图映射到 S41 的 `kind` 闭集。

**建议**：S41 的 `gaps[]` 补一个 `kind='missing_sfx_cue'` 或在 `missing_stem` 之下细分。否则音效缺口永远进不了 `completeness`，`formal_master_eligible` 会在缺音效时仍然为真——那正是 [docs/11](../docs/11-quality-repair.md) 说的"必要内容失败时不交付假完整 master"要防的。

### C7 `service.yaml` 的 `depends_on` 缺 S25 / S02 / S07 / S41

`depends_on = [S26, S28]`。实际还依赖：

- **S25**：`route_decision_id` 必需
- **S02**：`rights_policy_ref` 的解析（§4.5）
- **S06 / S07**：cue 意图与 anchor 的来源
- **S41**：EDL revision（placement 的输入）
- **S39**：虽然 S33 不是动作族执行者，但缺口填补的优先级可能需要 S39 的信息

**不选边**：按实际依赖实现，`service.yaml` 是生成物。

**建议**：catalog 补齐。

### C8 `service.yaml` 的 `api` 不对称：`POST /sfx-jobs` 但 `GET /sfx-cues/{id}`

这不是缺口，是**已存在的事实**，但需要确认它不是笔误。

`sound-effects/service.yaml`：
```yaml
api:
  command: POST /sfx-jobs
  query: GET /sfx-cues/{id}
```

**对比**：S31/S32 是 `POST /speech-jobs` + `GET /speech-jobs/{id}`（对称）。S33/S34 都是 command 用 `-jobs`、query 用 `-cues`（不对称）。

**§6.2 已论证这个不对称其实是更准确的建模**（cue 是主体，job 是尝试）。但它是 catalog 里的既成事实，不是本文的设计权。

**不选边**：按 `service.yaml` 实现。

**建议**：确认这是刻意的（若否，统一成对称；若是，S31/S32 的不对称也许值得考虑——但语音/口型一镜一句，job 与语料单元基本一一对应，不一定需要）。

### C9 音效没有对应的 `defect_type`，QC 缺陷归因不清

`action-families.v2.json` 的 `closed_type_set` 是 14 项闭集，没有 `sfx`。

**冲突点**：一个音效问题在 QC 报告里必须落到某个 `defect_type`。§1.4 的表给出三种可能的落点（`audio_mix`、`technical`、`temporal`），但这意味着：

1. **"音效缺失"没有落点。** 缺失是一个"没有东西"的事实，不是任何一类缺陷——QC 通常只能检查"有东西但不对"。
2. **归因会漂移。** 同一个问题可能被不同的人/工具判成 `audio_mix` 或 `temporal`，导致修复动作不同。

**不选边**：S33 不产生缺陷。缺口的表达走 `SfxGap`，不是 QC 缺陷。

**建议**：在 S35/S36/S37 的检查注册表里明确"音效缺失"由哪个检查、哪个 `defect_type` 表达；或承认它不在 QC 体系内，由 S41 的 `completeness` 与 S40 的人工验收负责。

---

## 13. 与其他方案的交叉引用

| 方案 | 关系 |
|---|---|
| [S41](S41-editorial.md) | EDL 与 `affected_output_ranges` 的权威；**placement 的输入**；缺口需进它的 `completeness`（C6） |
| [S42](S42-composition.md) | 混音、ducking、响度、`stem_sfx` 的唯一执行者；S33 只提供素材与位置；**`stretch` 与 `loop` 由 S42 执行** |
| [S02](S02-governance-policy.md) | `rights_policy_ref` 的权威；需要解析读口（C5） |
| [S06](S06-storyboard.md) / [S07](S07-change-management.md) | cue 意图、`anchor`、beat 的来源 |
| [S25](S25-model-routing.md) | `route_decision_id` 必需；**缺可循环能力位**（C1 相关） |
| [S26](S26-generation-jobs.md) | 唯一付费入口；`SUBMISSION_UNKNOWN` 冻结 |
| [S28](S28-artifact-ingest.md) | 音频 bytes 唯一权威；**缺 payload schema**（C4） |
| [S31](S31-speech-production.md) / [S32](S32-lipsync.md) | 同构风格；S33 比它们少一层（没有授权撤销、没有跨族约束），因为音效没有"人格"与"对齐"问题 |
| [S34](S34-music.md) | **最直接的同构**：BGM 也是"跟着 EDL 重切、不重生成"；两者共享 placement 模型与族策略的思路 |
| [S35](S35-technical-qc.md) | **S33 不在任何动作族**（§1.4）；音效缺陷落在 `audio_mix` / `technical` / `temporal` |
| [S39](S39-repair-planning.md) | 上限计数；S33 不构造 `strategy_class`（§8.2） |
| [S40](S40-human-review.md) | 缺口 waive、替代授权 |
| [S08](S08-revision-snapshot.md) | 接受权威；检查点推进 ≠ 接受 |
| [docs/11 §2](../docs/11-quality-repair.md) | "BGM 盖对白 | ducking/重混音"——这确认了音效/音乐的修复手段在 S42，不在 S33 |
