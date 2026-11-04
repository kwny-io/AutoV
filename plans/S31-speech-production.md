# S31 · 配音生产 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。所有供应商仍为 `disabled`，`slo.verified: false`。

上游依据：[docs/09 §1](../docs/09-model-execution.md)（"S29–S34 负责生产意图与阶段，不能绕过 S26 直连模型"）、[docs/07 §1 步骤 5](../docs/07-production-workflows.md)（"S31 配音与 S37 字级对齐确定对白长度，无对白分支使用显式 no-op，不创建空音频假成功"）、[docs/07 §3](../docs/07-production-workflows.md)（"复用未改文本/音色与足够短的 TTS"）、[docs/10 §1](../docs/10-assets.md)（"授权撤销事件禁止新调用"）、[docs/11 §2](../docs/11-quality-repair.md)、[S15 §1.3–§1.6](S15-voice-assets.md)（授权是音色级的、可撤销的单行事实）、[S35 §2.2.1](S35-technical-qc.md)（`voice` 动作族）、[S37 §3](S37-audio-alignment.md)（`AUD.TEXT.*` / `AUD.VOICE.*`）、[S39 §5](S39-repair-planning.md)、`contracts/generation-command.v2.schema.json`（`modality: speech`）。

链上位置：`S06 ShotSpec 锁定台词 → S31 组配音意图（文本 + 音色版本 + 词典）→ S25 估价路由 → S26 准入 Saga → S27 执行 → S28 归档 → S31 登记 take 并发布 ready → S37 字级对齐 → S38 字幕 / S32 口型 / S30 视频时长`。

本文重点回答三个问题：**音色授权撤销时为什么"不能自动重生成"而且系统要真做得到**（§4.2、§4.3）、**同一句台词在多次 Run 里怎么做到"没改就不花钱"又不被当成绕过上限**（§4.4、§8）、**"这句台词多长"这个数字谁说了算**（§4.5）。

---

## 0. 一句话边界

**S31 拥有"这一句台词用哪一版文本、哪个音色版本、哪份发音词典、什么情绪，合成出了哪几个音频候选，这一镜这一句当前用哪一个"。它不拥有音色本身、不拥有授权、不拥有音频字节、不拥有对齐时码、不拥有时间线、不拥有钱。**

```
S06:   台词文本与情绪标签（S31 只读 ShotSpec，不改剧本）
S15:   音色定义、授权状态、发音词典内容（S31 只读 + 实时复查）
S31:   SpeechJob + UtteranceRevision + VoiceTake + 词典绑定 + 配音检查点
S25:   可行路由与报价（S31 消费，不改）
S26:   attempt / ProviderJob / SUBMISSION_UNKNOWN（唯一付费通道）
S28:   音频 bytes 与 content_hash（唯一权威）
S37:   字级时间码、音素序列、`AUD.*` 判定（唯一权威）
S38:   字幕轨道（消费 S37，不消费 S31 的时间猜测）
S32:   口型（消费 S31 的音频 + S37 的时码）
S30:   视频片段（`silent_then_tts` 时消费 S31 的时长上界）
S41:   时间线（S31 只提供音频，不决定它在 EDL 上的位置）
S23 / S22: 钱与 permit
```

判据：任何"这句台词现在是什么声音"的问题，答案来自 S31 的配音检查点 + S28 的 artifact。任何"这句台词在成片里从第几毫秒到第几毫秒"的问题，答案来自 S37 的 `alignment_unit`。任何"这个音色还能不能用"的问题，答案来自 S15。任何"这花多少钱"的问题，答案来自 S23。

**必须守住的一条原则：**

> **同一个（文本, 音色版本, 词典版本, 情绪, 语言）只有一种声音；授权没了，声音就没了——不能换一个"像的"顶上。**

这条原则包含两个方向的约束，两边都必须用机制而不是约定来保证：

- **正向**：同一组输入不得因换 seed、换 Run、换 provider 而产生"听起来是另一个人"的 take 并被接受。所以音色绑定（`voice_binding.id + revision`）进入输入指纹，`variation_nonce` 不进。
- **反向**：授权状态变成 `revoked` / `expired` / `suspended` 时，S31 **不得**创建新合成任务，**不得**自动换用同角色的其他音色，**不得**把"相似声线"当作降级选项。只能标记为权利失效并转人审。

---

## 1. 责任与明确不负责

### 1.1 本服务是唯一写者

| 事实 | 说明 |
|---|---|
| `SpeechJob` | 一次配音生产意图：文本、音色绑定、词典绑定、情绪、输出规格、必要能力 |
| `UtteranceRevision` | 台词的一个不可变版本（文本 + 情绪 + 语言 + 文本 hash），跨 job 共享 |
| `VoiceTake` | 一个已归档的音频候选：`artifact_ref`、容器实测时长、provider 溯源、权利状态 |
| `VoiceBindingRef` | 提交时刻对 S15 音色绑定的快照：`voice_id`、`revision`、`authorization_state`、`state_seq` |
| `LexiconBindingRef` | 本 job 实际使用的发音词典解析结果：`lexicon_id`、`revision`、`content_hash`、来源（音色默认 / 显式指定） |
| `SpeechCheckpoint` | 每 `(shot_lineage_id, utterance_id, locale)` 的"当前生产基线 take"指针 |
| `RightsRecheckLog` | 每次向 S15 做资格复查的审计记录（何时、看到哪个 `state_seq`、结论） |

### 1.2 本服务明确不写

| 事实 | 权威 | S31 的正确做法 |
|---|---|---|
| 音色定义、声纹样本、`provider_voice_id` 映射 | **S15** | 只存 `{voice_id, revision}` + 提交时刻的授权快照 |
| 授权状态本身 | **S15** | 只**读**，且关键时点强一致复查（§4.2）；不缓存结论用于放行 |
| 发音词典内容 | **S15** | 只存 `{lexicon_id, revision, content_hash}` |
| 台词文本的权威版本 | **S06 / S04** | 只存 `text_hash` + `utterance_id`；文本变了就是新 `UtteranceRevision` |
| 音频 bytes、容器、编码 | **S28** | 只存 `artifact_ref`（`content_class = generated_audio`） |
| 字级/音素级时间码、对齐置信度 | **S37** | 不重算、不近似、不用 provider 返回的时间戳冒充 |
| 字幕轨道与断句 | **S38** | 完全不涉及 |
| 口型 | **S32** | 完全不涉及 |
| 时间线、EDL、cue 位置 | **S41** | 只提供音频与时长，不决定位置 |
| 混音、增益、响度标准化、ducking | **S42** | 完全不涉及。S31 输出的单声道/立体声音频是**素材**，不是成品 |
| 当前接受指针 / `node_epoch` / `execution_fence` | **S08** | 只发事件，不推进 |
| 金额、reservation | **S23** | 只传 `budget_scope_id` |
| permit | **S22** | 不持有 |
| `GenerationAttempt` / `ProviderJob` / `SUBMISSION_UNKNOWN` | **S26** | 只持引用 |
| 能力、价格、路由 | **S24 / S25** | 只消费 `RouteDecision` |
| QC 报告、缺陷、coverage | **S35 / S36 / S37** | 不自造；只附加引用 |
| 修复决策、上限计数 | **S39** | 只执行被指派的那一格；不自己数 attempt |
| 人工裁决 | **S40** | 不写 |

### 1.3 本服务不直连供应商（强制条款）

**本服务不直连供应商。** 不持有供应商凭证，不出供应商域名，不配置供应商 SDK，不接受"临时直连测试"。

S31 的每一次付费合成走同一链路：

```
S31 组 GenerationCommand(modality=speech)
  → POST /generation-jobs           (S26)
      → S23 reserve（按 quote_upper_bound_micro）
      → S22 permit（provider/channel/region 由 route_decision 定死）
      → SubmissionIntent + Outbox
      → S27 claim dispatch grant → 付费提交
  → S26 观测 → S28 归档 → production.speech.ready 回 S31
```

强制手段与 [S29 §1.3](S29-image-production.md) 完全相同，三条都是"不是约定"级的：

1. **凭证隔离**：供应商密钥只注入 `connectors/*` 的 Deployment（[docs/09 §2](../docs/09-model-execution.md)）。S31 的 ServiceAccount 不在供应商出站 `NetworkPolicy` 白名单里。
2. **dispatch grant**：S27 的 `require_dispatch_grant=true` 是编译期常量，无开关可关。
3. **命令形状**：S31 发出的唯一付费入口是 `POST /generation-jobs`，请求体是 `{command_id, route_decision_id, generation_command}`（`contracts/openapi.v2.yaml`）。S31 没有任何其他出站调用能产生费用。

**S31 不参与准入 Saga 的内部步骤**，不知道 reservation id、permit id、`dispatch_id`。

这一点对配音尤其重要：配音的单价低、句子数多，一集几百句。如果有人为了"省一次 Saga 往返"而在 S31 里直接并发调 TTS 接口，成本会在一天内失控，而且**这类绕过不会在任何账本里留下痕迹**——S23 只记它被通知过的花费。所以 §9 的 `s31_paid_calls_total` 必须与 S26 侧按 `modality=speech` 统计的调用数对齐，差异即是告警。

### 1.4 动作归属（对照 S35 动作族）

`voice` 族是 `re_synthesize_speech → regenerate_video`，兜底 `human_review`（[S35 §2.2.1](S35-technical-qc.md)、`contracts/quality/action-families.v2.json`）。

| 动作 | 执行者 | S31 的角色 |
|---|---|---|
| `re_synthesize_speech` | **S31（意图）→ S26（执行）** | **意图提供者，不是执行者**。创建新 `SpeechJob`，由 S26 付费 |
| `regenerate_video`（`voice` 族第二步） | **S30（意图）→ S26** | 不参与。仅在 S39 显式下发时由 S30 执行 |
| `redo_lipsync` | **S32（意图）→ S26** | 不参与。见 §4.6 的跨族边界 |
| `realign_subtitles` / `re_render_subtitles` | **S38** | 不参与 |
| `remix` | **S42** | 不参与 |
| `reingest` / `transcode` | **S28 / S42** | 不参与 |
| `change_spec` | **S06 / S07** | 不参与。改台词不是重合成 |
| `human_review` | **S40** | 不参与 |

**S31 与 `re_synthesize_speech` 的准确关系：**

S31 **不是执行者**——执行者是 S26 + S27。S31 是**意图提供者**：它把 S39 的 `RepairAction(re_synthesize_speech, ...)` 或 S06 的新 `UtteranceRevision` 翻译成一个具体的新 `SpeechJob`，带上新的 `variation_nonce`、新的 `SpeechJob.input_fingerprint`，然后提交 S26。

谁对"这次重做会不会超上限"负责，与 S30 §1.4 同构：

- S39 在**下发动作前**检查上限（[S39 §5.7](S39-repair-planning.md)），DENY 时不产生 `RepairAction`。
- S31 收到 `RepairAction` 后**不再检查上限**（它没有那个台账），只检查自己的本地前置条件：音色是否仍可用、文本版本是否存在、词典是否可读。
- S26 在 `POST /generation-jobs` 时检查 `attempt_no ≤ max_business_attempts` 与节点费用上限（[S26 §12.1 第 5–6 条](S26-generation-jobs.md)），这是最后一道。

**三道检查，三道都必须在，但只有 S39 用的是跨 Run 累计的键。**

**S31 明确不做的四件事：**

1. **不因 QC 失败而自动重合成。** 收到 `quality.alignment.completed` 且 decision=`repair` 时，S31 只记录"该 take 有缺陷引用"。是否重做由 S39 决策。自动重合成会绕过上限：配音的句子多，一句一句自动重做可以在一小时内烧掉整集预算。
2. **不在授权失效时"降级"到别的音色。** 这是 `voice` 族 `forbidden` 列表里第一条的直接落点（`action-families.v2.json`：`re_synthesize_speech` / "the voice licence is expired or revoked" / "licence state is not repairable by re-synthesis; it escalates to human review"）。S31 收到已撤销音色的请求时返回 `409 VOICE_AUTHORIZATION_REVOKED`，不排队、不等待、不挑替代（§4.3）。
3. **不在 `lipsync` 缺陷下重做语音。** `lipsync` 族**只有** `redo_lipsync`，且明令禁止 `re_synthesize_speech`（"inferring TTS redo from a lipsync defect is a cross-family reach"）。S31 收到 `type=lipsync` 的 `re_synthesize_speech` 请求时 `422 ACTION_TYPE_MISMATCH`（§4.6）。
4. **不为无对白镜头产出空音频。** [docs/07 §1 步骤 5](../docs/07-production-workflows.md) 要求"无对白分支使用显式 no-op，不创建空音频假成功"。`text` 为空、只含空白、或 `utterance_id` 对应的 ShotSpec 节拍标记为无对白时，S31 一律 `422 EMPTY_UTTERANCE`，绝不合成一个静音文件让下游"看起来有音轨"（§4.7 F1）。

---

## 2. 实体与关键区分

### 2.1 五层对象

```
UtteranceRevision   一句台词的一个不可变版本（text_hash + emotion + locale）
      │  N
      ▼
SpeechJob           一次合成意图（绑定 UtteranceRevision + VoiceBindingRef + LexiconBindingRef）
      │  1:N（多 seed / 多次修复）
      ▼
VoiceTake           一个已归档的音频候选（artifact_ref + 时长 + 权利状态）
      │  N:1
      ▼
SpeechCheckpoint    每 (shot_lineage_id, utterance_id, locale) 的当前基线指针
      │
      ▼
   production.speech.ready
```

第五层是 `RightsRecheckLog`——它不在链上，是旁挂的审计表，回答"我是什么时候、基于哪个 `state_seq`、认为这个音色可用的"。

**为什么要单独拆出 `UtteranceRevision`：**

台词文本重复出现在四个地方：S04 的剧本、S06 的 ShotSpec、S37 的对齐报告、S31 的合成请求。如果 S31 只把 `text` 存成一个字符串，就无法回答"这次合成用的文本和那次 QC 用的文本是不是同一版"。

所以 S31 存 `text_hash` 与 `utterance_id`，并要求调用方带 `text_revision`。文本变一次 → 新的 `UtteranceRevision` → 新的 `input_fingerprint` → 必然重新合成。文本没变 → 指纹不变 → 可以复用已有 take（§4.4）。

**S31 不判断"文本该不该变"。** 剧本改动是 S04 / S07 的决定，S31 只接受新 revision。若调用方送来相同 `utterance_id` 但不同 `text` 而 `text_revision` 没提，S31 拒绝（`422 TEXT_REVISION_CONFLICT`）——否则会出现"同一个 revision 对应两份文本"，下游无法解释历史音频。

### 2.2 `VoiceTake` 不是"候选集"，是"候选"

S29/S30 的做法是一 job 一候选（各自的 U2）。S31 沿用：一个 `SpeechJob` 成功归档一个 `VoiceTake`。

**为什么配音不搞多候选：**

- 配音的判定标准不是"哪张更好看"，而是"台词对不对、音色对不对、时长对不对"。这三件事都由 S37 用绝对标准判定，不做审美排序。
- 一集几百句，多候选会把归档量、QC 量和存储量乘以候选数，而收益接近零——TTS 的可接受性分布远比图像窄。
- 多候选还会破坏"同一组输入只有一种声音"：如果两个 take 都可用，谁被接受就取决于时序，等于让 seed 影响交付。

如果业务确实需要"导演从 3 个情绪版本里挑一个"，那是三次独立的 `SpeechJob`，各自带不同的 `emotion` 或 `variation_nonce`，由 S40 或 S02 裁决。S31 不产生"同一次请求的多个候选"。

### 2.3 授权快照与实时复查：两件不同的事

这是 S31 最容易做错的地方，必须写死：

| | 授权快照（`VoiceBindingRef`） | 实时复查（`RightsRecheckLog` + S15 强一致读） |
|---|---|---|
| 是什么 | 提交那一刻看到的事实 | 每次要做"用这个声音"的动作前再问一次 |
| 存哪 | 本服务表里，不可变 | 只写审计日志，不做缓存、不做投影 |
| 用来做什么 | 事后解释"当时为什么认为可以用" | 放行 |
| 能不能用于放行 | **不能** | 能 |

[S15 §4.2](S15-voice-assets.md) 把 `POST /voices/{id}:check-eligibility` 定义为**强一致读**："直接查 `voice_authorization`（单行主键），**不允许**从缓存/投影读。整个 S15 只有这一处不能缓存。"

S31 消费 `asset.voice.authorization.changed` 事件，但**该事件只能用于"提前发现"，不能用于"确认可用"**：

- 收到 `revoked` 事件 → 立刻把相关 take 标 `rights_state='void'`，停止一切自动流程（这是**收紧**，安全）。
- 收到 `granted` 事件 → **不能**因此认为某个历史阻塞的 job 现在能提交了（这是**放宽**，必须复查）。事件是至少一次投递且可能乱序（[docs/04 §4](../docs/04-data-consistency.md)），一条 `granted` 可能早于一条 `revoked` 到达。

**一句话：事件只能让 S31 更保守，不能让它更激进。**

### 2.4 词典绑定的三级解析

`generation-command.v2` 的 `speech.input.pronunciation_lexicon` 是 `{id, revision} | null`。但 [S15 §1.6](S15-voice-assets.md) 有两类词典：

- `voice_lexicon_binding`：某音色默认使用的词典（音色级）
- `project_lexicon`：项目/剧级词典，所有音色共用，用于专名统一读音

"S15 只**提供**词典内容，不决定用哪个。"这个决定落在 S31 上（S31 是命令的组作者）。解析顺序必须是：

| 优先级 | 来源 | 何时生效 | 记录到 `LexiconBindingRef.source_kind` |
|---|---|---|---|
| 1 | 请求显式给出的 `pronunciation_lexicon` | 非 null | `explicit` |
| 2 | S15 的该音色默认绑定 `voice_lexicon_binding` | 请求为 null 且 S15 有绑定 | `voice_default` |
| 3 | 无 | 请求为 null 且 S15 无绑定 | `none`（`lexicon_id` 为 null） |

**为什么第 2 级必须存在：** 20 个角色、每集若干专名，如果每句都要调用方显式传词典，第一次漏传就出现"同一个人名在第 3 集念错"。项目级词典是唯一的统一手段，而它只有在 S31 主动解析时才生效。

**为什么必须记录 `source_kind`：** 事后有人问"这句为什么念 `lín lán` 而不是 `lín lǎn`"，答案要么是"调用方指定了"，要么是"该音色默认词典这么写的"。不记录来源，这个问题永远答不了，而它会每周出现一次。

**S31 必须校验词典属于本租户与同一 project/series**，否则会拿别的剧的专名表去合成（跨租户 → 404，同租户跨项目 → 422 `LEXICON_SCOPE_MISMATCH`）。

### 2.5 `require_alignment`：一个容易被误读为"对齐"的字段

`speech.input.require_alignment` 是 boolean。它的真实含义是**"要求供应商在返回音频时附带词级时间戳"**，不是"要求 S37 做对齐"。

这两件事的差别是 S31 必须守的边界：

| | provider 附带的时间戳 | S37 的 `alignment_unit` |
|---|---|---|
| 谁产出 | 供应商（黑盒） | S37 的 ASR + forced alignment |
| 可信度 | 未知，可能是估算 | 有 `confidence` 与 `ci_low/ci_high` |
| 下游能用吗 | **不能** | 能，且是唯一来源 |
| S31 存哪 | 若 S28 归档为 `evidence`，S31 只存引用 | 不存，只存 `alignment_report_ref` |

**S31 绝不能把 provider 时间戳写进 `VoiceTake` 的时间字段再让 S32/S38 用。** 那会造成第二个对齐权威，而且是最劣质的那种：一个无法审计、无置信度、在供应商换版本时会静默变化的数字。S37 的方案专门为"对齐必须可复现"设计了 `asr_model_snapshot` 与 `align_model_version`（[S37 §5](S37-audio-alignment.md)），provider 时间戳完全不满足这个要求。

`require_alignment=true` 在 S31 里的唯一作用是：告诉 S25/S26 需要 route 到**具备词级时间戳能力**的渠道，并且若该能力不可用则 `REJECTED_LOCAL`，而不是悄悄降级成 `false`（[docs/09 §4](../docs/09-model-execution.md)："完整对白……不能无声降级"——词级时间戳是 S38 中文断句的输入，属于必要能力）。

---

## 3. 状态机

### 3.1 `SpeechJob` 状态

```
CREATED → CHECKING_RIGHTS → PREPARING → SUBMITTED → WAITING_PROVIDER → INGESTING → READY
                 ↘                ↘              ↘                 ↘
            RIGHTS_BLOCKED   REJECTED_LOCAL     FAILED        WAITING_UNKNOWN
                                                                   ↓
                                                        RECONCILED_READY
                                                        RECONCILED_FAILED
                                                        RIGHTS_BLOCKED
任意非终态 → CANCELED
```

`CHECKING_RIGHTS` 是 S31 独有的状态（S29/S30 没有），理由见 §4.2。

| 从 | 到 | 触发 | 前置条件 | 副作用 |
|---|---|---|---|---|
| — | `CREATED` | `POST /speech-jobs` | 校验通过 + 幂等未命中 | 写 job；写/复用 `UtteranceRevision` |
| `CREATED` | `CHECKING_RIGHTS` | worker 领取 | — | 调 S15 `check-eligibility`（强一致），解析词典 |
| `CHECKING_RIGHTS` | `RIGHTS_BLOCKED` | S15 返回 `eligible=false` 且 `deny_reason ∈ {AUTHORIZATION_REVOKED, AUTHORIZATION_EXPIRED, USE_NOT_AUTHORIZED}` | — | 写 `RightsRecheckLog`；**不产生费用**；发 `speech.voice-authorization-lapsed` |
| `CHECKING_RIGHTS` | `REJECTED_LOCAL` | `deny_reason = MAPPING_UNAVAILABLE` / `REGRESSION_FAILED` / 词典范围不符 / 空文本 | — | 不产生费用 |
| `CHECKING_RIGHTS` | `PREPARING` | `eligible=true` | `state_seq` 已记录 | 取/复用 `RouteDecision` |
| `PREPARING` | `REJECTED_LOCAL` | `feasible=false` / 能力不含 `word_timestamps` 而 `require_alignment=true` / 指纹冲突 | — | 不产生费用 |
| `PREPARING` | `SUBMITTED` | S26 返回 202 | `quote_valid_to > now()`；**提交前最后一次授权复查通过** | 存 `provider_job_id` / `attempt_id` |
| `SUBMITTED` | `WAITING_PROVIDER` | S26 → `QUEUED`/`RUNNING` | — | — |
| `SUBMITTED` | `FAILED` | S26 → `PROVIDER_FAILED` | 无重试预算 | — |
| `SUBMITTED` | `WAITING_UNKNOWN` | S26 → `SUBMISSION_UNKNOWN` | — | 冻结（§4.6） |
| `WAITING_PROVIDER` | `INGESTING` | S26 → `PROVIDER_SUCCEEDED` | — | — |
| `WAITING_PROVIDER` | `FAILED` | S26 → `PROVIDER_FAILED` / `CANCELED` | — | — |
| `INGESTING` | `READY` | `artifact.ingested` 且 hash 一致 且 **`observed_duration_ms` 已知** 且 **归档后授权复查仍通过** | 时长上界可用于上报 | 写 `VoiceTake` + CAS 检查点 |
| `INGESTING` | `RIGHTS_BLOCKED` | 归档完成但复查发现授权已失效 | — | **take 仍登记**（bytes 在 S28），但 `rights_state='void'`；不发 `ready` |
| `INGESTING` | `INGESTING` | S28 失败 / 未到 | 重试预算未尽 | 只重归档 |
| `INGESTING` | `FAILED` | 重试耗尽 / hash 不符 / 时长为 0 | — | — |
| `WAITING_UNKNOWN` | `RECONCILED_READY` | S26 → `ARCHIVED` | 同 `INGESTING→READY` | 同 `INGESTING→READY` |
| `WAITING_UNKNOWN` | `RECONCILED_FAILED` | S26 → 确认未收单且放弃 | — | 允许 S39 决定新 attempt |
| 任意非终态 | `RIGHTS_BLOCKED` | `asset.voice.authorization.changed` 显示 `revoked` | — | 调 S26 cancel；不承诺退款；发 `speech.voice-authorization-lapsed` |
| 任意非终态 | `CANCELED` | 显式取消 | — | 调 S26 cancel；不承诺退款 |

**终态**：`READY`、`RIGHTS_BLOCKED`、`REJECTED_LOCAL`、`FAILED`、`RECONCILED_FAILED`、`CANCELED`。

`RIGHTS_BLOCKED` 是终态的一种，且**没有任何自动出口**。它只能由人（S40）或者"新建一个 `voice_id` 并走完整的新 job"离开——这两条都不是自动重试。

### 3.2 为什么 `RIGHTS_BLOCKED` 必须是一个独立状态

如果授权失效只写成 `FAILED(AUTHORIZATION)`，会有三个后果：

1. **重试队列会捡起它。** 所有 `FAILED` 的通路都要处理"可能需要重试"，而这一类**绝对不能重试**。独立状态让 `GET /speech-jobs?state=FAILED` 与重试任务在结构上就看不见它。
2. **运维告警会把它算成技术故障。** 技术故障的 SLO 应该告警到生成团队；权利失效应该告警到权利/法务与内容负责人。这两条升级路径完全不同。
3. **人审列表需要它。** S40 的待办里，"这一句因为权利问题需要人决定"和"这一句合成失败了"是两种不同的卡片。

**`RIGHTS_BLOCKED` 的 job 不产生 `VoiceTake`（除非已归档），但已归档的 take 不删除。** 字节的删除与保留是 S28 与 S45 的事；S31 只标记 `rights_state='void'` 并把它排除在检查点推进与 `ready` 事件之外。**"不再可用于新作品"与"删除历史"是两件事**，混为一谈会破坏审计（E39）。

### 3.3 状态持久化

同 S29 §3.3 / S30 §3.3：本地事务 + 乐观并发（`state_seq` 单调递增）+ 同事务 Outbox + 表驱动转移。

补充一条 S31 特有的：**`RightsRecheckLog` 的写入必须与状态转移同一事务**。理由：如果复查通过但状态转移回滚，"我复查过"这个事实就不该留下；如果状态转移了但复查日志丢了，"我凭什么放行"就无从解释（E39）。

---

## 4. 关键流程

### 4.1 正常路径

```
1. POST /speech-jobs
     {command_id, request_hash, context,
      utterance: {utterance_id, shot_lineage_id?, text, text_revision, locale},
      voice_binding: {id, revision},
      emotion,
      pronunciation_lexicon: {id, revision} | null,
      output_spec: {format, sample_rate_hz},
      require_alignment,
      route_policy_ref | route_decision_id,
      budget_scope_id, deadline}

2. 校验（顺序固定，失败即返回）：
   a. context 必填项 + 平台签名绑定的 tenant
   b. 幂等键（tenant + command_id + request_hash）
   c. 跨租户：voice_binding 属于本租户（查 S15，只读）
   d. text 非空且非纯空白；locale 合法
   e. text_revision 与已有 UtteranceRevision 一致（§2.1）
   f. utterance_id + locale 在本 tenant 内唯一确定一个 UtteranceRevision
   g. emotion 非空
   h. output_spec.format ∈ {wav, flac, mp3}，sample_rate_hz ≥ 8000
   i. pronunciation_lexicon 若给出，属于本 tenant 且 scope 匹配
   j. deadline > now()

3. CHECKING_RIGHTS：
   - POST /voices/{id}:check-eligibility（S15，强一致）
   - 解析词典（§2.4 三级）
   - 写 VoiceBindingRef（快照）+ LexiconBindingRef + RightsRecheckLog
   - eligible=false 且属权利类 → RIGHTS_BLOCKED（终态，§3.2）
   - eligible=false 且属技术类 → REJECTED_LOCAL

4. 复用判断（§4.4）：若 input_fingerprint 与已有 READY take 相同、
   且本请求不带 repair_action_id → 直接返回该 take，reused=true，结束。
   （不创建 S26 job，不花钱）

5. PREPARING：取 RouteDecision；若 require_alignment 而路由无
   word_timestamps 能力 → REJECTED_LOCAL(CAPABILITY_MISSING)

6. SUBMITTED：**提交前最后一次授权复查**（§4.2 第三时点）。
   若失败 → RIGHTS_BLOCKED，不提交。
   否则组 GenerationCommand 交 S26。

7. 等 S26 / S28；INGESTING→READY 时：
   - 归档后授权复查（§4.2 第四时点）
   - 写 VoiceTake（含 observed_duration_ms）
   - CAS 推进 SpeechCheckpoint
   - outbox production.speech.ready
```

### 4.2 授权复查的五个时点

这是 S31 的核心机制。少一个时点，就有一个时间窗口能用已撤销的音色完成一次付费合成。

| # | 时点 | 目的 | 失败时 | 花钱 |
|---|---|---|---|---|
| 1 | `POST /speech-jobs` 同步校验（可选快路径） | 给调用方即时否定，省一次 worker 唤醒 | `409 VOICE_AUTHORIZATION_REVOKED` | 否 |
| 2 | `CHECKING_RIGHTS`（worker 真正开工前） | 异步到达的请求可能已排队数分钟 | `RIGHTS_BLOCKED` | 否 |
| 3 | **`PREPARING → SUBMITTED` 之前**（提交给 S26 前最后一刻） | 从取路由到提交之间可能过了几分钟（等 permit） | `RIGHTS_BLOCKED` | 否 |
| 4 | **`INGESTING → READY`**（归档完成后、发事件前） | 合成可能跑了 30 秒到 2 分钟；这期间可能被撤销 | take 标 `rights_state='void'`，不发 `ready` | **是（已经花了）** |
| 5 | 消费 `asset.voice.authorization.changed` | 撤销后尽快止血，不等下一个请求 | 相关 take 全部标 void | 否 |

**时点 3 是最容易被漏掉的一个。** 直觉上"提交前查一次就够了"，但从 `PREPARING` 到真正向供应商发出 HTTP 之间，横着 S23 的 reserve、S22 的 permit、S26 的 SubmissionIntent——在配额紧张时这段可能是几分钟。撤销发生在这一段里，如果不复查，就会用一个已撤销的声音付一次钱。

**时点 4 不能省，即使它"已经晚了"。** 花掉的钱收不回来，但仍然要阻止的是**这个 take 被当作可用产物进入交付**。一个用已撤销音色合成、并被推进检查点、被 S41 剪进成片的音频，是真实的法律风险；一个只存在于 S28、被标记为 `rights_state='void'` 的音频文件不是。

**时点 5 的语义边界**：事件只用于收紧（§2.3）。收到 `revoked` 时立刻标 void 是安全的；收到 `granted` 时**绝不能**自动把 `RIGHTS_BLOCKED` 的 job 拉回可提交——那需要一次新的、带新 `state_seq` 的复查，而那必须由一次新的请求触发。

### 4.3 撤销时的四种反应（以及一个禁令）

收到授权失效（任一 `deny_reason ∈ {AUTHORIZATION_REVOKED, AUTHORIZATION_EXPIRED, AUTHORIZATION_SUSPENDED, USE_NOT_AUTHORIZED}`）时，S31 的**全部**允许反应：

| # | 反应 | 何时 | 说明 |
|---|---|---|---|
| R1 | 拒绝新 job | 提交前 | `409` 或 `RIGHTS_BLOCKED`，带 `deny_reason` 与 `state_seq` |
| R2 | 冻结在途 job | 提交后 | 调 S26 cancel；若取消不了，`DISCARD_ON_COMPLETION`（[S26 §13](../plans/S26-generation-jobs.md)），结果丢弃但不删除 |
| R3 | 使已完成的 take 不可用 | 任何时点 | `rights_state='void'`；排除出检查点；不影响已发布的历史事件 |
| R4 | 转人审 | 任何时点 | 发 `speech.voice-authorization-lapsed`（建议新增，§12 C6），带 `voice_id`、`state_seq`、受影响的 take 列表 |

**唯一的禁令：不得换一个音色。**

这条禁令不是"不推荐"，而是必须由代码结构与数据约束共同保证：

```sql
-- §5.2 摘要：SpeechJob 的音色由 input_fingerprint 冻结
CONSTRAINT sj_voice_frozen CHECK (
  voice_binding_id IS NOT NULL AND voice_binding_revision IS NOT NULL)
```

- **`voice_binding_id` / `voice_binding_revision` 是 NOT NULL 且不可 UPDATE**（只能新建 job）。没有任何代码路径能在 job 上"改一个字段换声线"。
- **`SpeechJob` 上没有 `fallback_voice_id` 这样的列。** 想加这类列的人必须先删掉这条注释。
- **`RIGHTS_BLOCKED` 是终态**（§3.2），重试队列结构上看不见它。
- **S15 侧已有 `revoked` 终态触发器**（[S15 §1.3](S15-voice-assets.md)：`revoked` 不可逆，恢复必须新建 `voice_id`），所以"同一个 `voice_id` 过一会儿又变回 granted"在数据层面不可能。

为什么这条禁令这么硬：配音的声音是**人格**。自动换成相似声线，在合同上可能是违约（演员授权通常按 `voice_id` 计），在法律上可能是对声音权/肖像权的侵害，在作品上是观众能听出来的错误。这不是"降级"，是"换演员"。任何自动化系统都没有资格做这个决定。

`AUTHORIZATION_SUSPENDED` 是唯一的例外情形，但它也不是 S31 决定重新可用：S15 定义为"临时技术问题，触发 S24 能力失败 + S25 重路由"（[S15 §1.3](S15-voice-assets.md)）。S31 遇到它时按 `RIGHTS_BLOCKED` 处理并转人审，**由 S24/S25 解决的是"这个音色在哪个渠道能跑"，不是"换一个人来配"**。两者不能混淆。

### 4.4 复用：没改就不花钱

[docs/07 §3](../docs/07-production-workflows.md) 要求"复用未改文本/音色与足够短的 TTS"。这条在 S31 里落成一个明确的、可测的规则。

**`input_fingerprint` 的定义：**

```
input_fingerprint = sha256(
    text_hash
  || voice_binding.id      || voice_binding.revision
  || lexicon.lexicon_id    || lexicon.revision || lexicon.content_hash
  || emotion
  || locale
  || output_spec.format    || output_spec.sample_rate_hz
  || require_alignment
)
```

**不进指纹的**：`variation_nonce`、`seed`、`attempt_id`、`provider`、`model_id`、`command_id`、`trace_id`、`deadline`、`budget_scope_id`、`node_epoch`、`execution_fence`、`manifest_revision`。理由与 [S39 §5.2](S39-repair-planning.md) 同构：这些都是"怎么做的"而不是"做的是什么"。

**复用规则：**

```
若 请求不带 repair_action_id
且 存在同 tenant 的 VoiceTake
   满足 take.input_fingerprint == 本请求的 input_fingerprint
   且 take.rights_state == 'usable'
   且 take.state IN ('READY','ACCEPTED')
   且 take.artifact_ref 在 S28 仍为 REFERENCEABLE
则 返回该 take（200，reused=true，reused_take_id=...）
   不创建 SpeechJob，不调用 S26，不花钱
```

**四个必须收窄的条件，逐个说明：**

| 条件 | 不加会怎样 |
|---|---|
| `不带 repair_action_id` | 修复请求必须真的重做。若允许复用，S39 下发的 `re_synthesize_speech` 会静默变成一个 no-op，QC 缺陷永远修不掉，而上限计数仍在累加——最坏的一种组合 |
| `rights_state == 'usable'` | 用一个已撤销的音色产物应答新请求，等于绕过 §4.3 |
| `artifact_ref` 仍 `REFERENCEABLE` | S28 可能已 tombstone。复用指向一个不可下载的对象，会让下游在 S42 渲染时才炸，错误信息完全无法追溯到配音（E23） |
| 同 tenant | 跨租户复用就是跨租户数据泄漏的另一种形式（E28） |

**复用不推进任何东西。** 它返回一个已有 take，不写 `VoiceTake`、不推进检查点、不发 `ready` 事件。因为检查点和 `ready` 已经在那个 take 第一次产生时推进/发过了；再发一次会让下游以为"又好了一个新的"，S41 会重剪一次，S32 会重做口型——全部是白花的钱。

**复用与"足够短"的关系：** docs/07 说"足够短的 TTS"可复用。S31 的判断是**结构性的**，不是基于时长的：只要输入指纹相同就复用。时长短不是复用的条件，而是复用的结果——同样的输入必然产生相近的时长。如果某次合成特别长（比如 8 秒），那是 `AUD.ALIGN.DURATION_VS_SPEC` 要报的问题（[S37 §3](S37-audio-alignment.md)），属于创作决策，不是复用策略要处理的。

### 4.5 "这句台词多长"——只有一个诚实的答案

这是 S31 与 S30 / S41 之间最容易产生误解的数字，必须写清楚证据链。

S31 能提供的**唯一**时长事实是：**归档音频容器的实测总时长** `observed_duration_ms`（来自 S28 的容器探测，§12 C2）。

S31 **不能**提供、也**不试图估算**的：

| 数字 | 为什么 S31 不能给 | 谁给 |
|---|---|---|
| 台词第一个字到最后一个字的跨度 | 需要 ASR + 对齐 | S37 的 `alignment_unit` 首尾 |
| 前后静音时长 | 同上 | S37 |
| 音素级时间码 | 同上 | S37 |
| "这句在 4 秒镜头里放得下吗" | 需要与 ShotSpec 的目标时长对比 + 创作判断 | S37 报偏差，S39 转人审 |

**所以 `production.speech.ready` 里发布的是：**

```jsonc
{
  "audio_duration_ms": 3280,          // 容器实测，是台词跨度的上界
  "dialogue_span_ms": null,           // 需要 S37；S31 不猜
  "dialogue_span_source": "pending_s37"
}
```

**`audio_duration_ms` 用作上界是正确的、安全的：** 它一定 ≥ 真实台词跨度（音频里除了台词只有静音或轻噪声）。拿上界去判断"放不放得下"，最坏结果是**多报一点时长**，可能让人以为放不下而多要 200ms 镜头；若反过来拿一个估算值，最坏结果是**以为放得下而实际截断了台词**——那是 E04（"不能删台词"）的方向。

S30 的 `dialogue_duration_ms`（[S30 §6.2](S30-video-production.md)）应当消费这里的 `audio_duration_ms`，并理解它是上界。S30 §4.4 已在做同样的事（"`dialogue_duration_ms ≤ target_duration_ms`"），本文确认那个值的来源是 S31 的这个字段，且语义是上界。

**S31 不缓存 `dialogue_span_ms`。** 若将来 S37 的报告可以通过引用挂到 take 上（`alignment_report_ref`），S31 也只存**引用**，不复制数值。复制就是第二个权威，而且会在 S37 重跑（换 `asr_model_snapshot`）后静默过期。

### 4.6 跨族边界：口型与语音是两件事

`lipsync` 族的 `forbidden` 条目（`contracts/quality/action-families.v2.json`）：

> `re_synthesize_speech` / when: `always` / why: "if the audio itself is wrong it is a `voice` defect and must be reported as one; inferring TTS redo from a lipsync defect is a cross-family reach"

S31 侧的执行：

| 收到的请求 | S31 的反应 |
|---|---|
| `RepairAction(re_synthesize_speech)` 且 `defect_type ∈ {voice}` | 接受，创建新 job |
| `RepairAction(re_synthesize_speech)` 且 `defect_type ∈ {lipsync, audio_mix, subtitle, technical, temporal, identity, ...}` | **`422 ACTION_TYPE_MISMATCH`**，并回写 `expected_type=voice` |
| `RepairAction(redo_lipsync)` | 不参与，`422`（这是 S32 的入口） |
| `RepairAction(regenerate_video)` | 不参与，`422` |

**为什么这条守得住要注意：** 口型不对的**表象**常常看起来像"音频和嘴对不上"，而修复它的正确顺序是"先判断音频对不对"。如果音频本身念错了词，正确动作是 `re_synthesize_speech`（`voice` 族）；如果音频是对的、只是嘴没跟上，正确动作是 `redo_lipsync`（`lipsync` 族）。这两个判断的**证据不同**：前者要 ASR 文本比对（`AUD.TEXT.*`），后者要唇音时序比对（`AUD.LIPSYNC.*`）。[S37 §3](S37-audio-alignment.md) 把这两组检查分开设计，就是为了让这个判断有依据。

所以 S31 拒绝从 `lipsync` 缺陷反推重做语音，不是官僚，而是因为**那个推断没有证据基础**：一条 `AUD.LIPSYNC.OFFSET` blocker 完全不告诉你台词对不对。

**S31 也不做反向推断**：收到 `voice` 类缺陷时，S31 只做 `re_synthesize_speech`；`voice` 族的第二步 `regenerate_video` 由 S39 显式下发、由 S30 执行，S31 不代为升级（那会变成"改一句台词牵出一次视频重生成"的自动放大）。

### 4.7 失败矩阵

| # | 场景 | 检测点 | S31 动作 | 状态 | 花钱 | 计数 |
|---|---|---|---|---|---|---|
| F1 | `text` 为空或纯空白 | 同步校验 | `422 EMPTY_UTTERANCE` | — | 否 | 否 |
| F2 | 文本变了但 `text_revision` 没变 | 同步校验 | `422 TEXT_REVISION_CONFLICT` | — | 否 | 否 |
| F3 | 音色已被撤销 | 时点 1/2/3 | `409` 或 `RIGHTS_BLOCKED` | `RIGHTS_BLOCKED` | 否 | 否 |
| F4 | 音色已过期 | 时点 1/2/3 | 同上，`deny_reason=AUTHORIZATION_EXPIRED` | `RIGHTS_BLOCKED` | 否 | 否 |
| F5 | 音色停用（技术） | 时点 1/2/3 | 同上，`AUTHORIZATION_SUSPENDED` | `RIGHTS_BLOCKED` | 否 | 否 |
| F6 | 用途未授权（如只授权视频配音、未授权有声书） | 时点 1/2/3 | `RIGHTS_BLOCKED`，`USE_NOT_AUTHORIZED` | `RIGHTS_BLOCKED` | 否 | 否 |
| F7 | 供应商映射不可用 | 时点 2 | `REJECTED_LOCAL(MAPPING_UNAVAILABLE)` | `REJECTED_LOCAL` | 否 | 否 |
| F8 | 声纹回归未通过 | 时点 2 | `REJECTED_LOCAL(REGRESSION_FAILED)` | `REJECTED_LOCAL` | 否 | 否 |
| F9 | 词典 scope 与项目不符 | 时点 2 | `422 LEXICON_SCOPE_MISMATCH` | `REJECTED_LOCAL` | 否 | 否 |
| F10 | `require_alignment=true` 但路由无词级时间戳能力 | `PREPARING` | `REJECTED_LOCAL(CAPABILITY_MISSING)`，**不降级为 false** | `REJECTED_LOCAL` | 否 | 否 |
| F11 | 路由 `feasible=false` | `PREPARING` | 透传 `degradation_options`，不自己选 | `REJECTED_LOCAL` | 否 | 否 |
| F12 | 归档后在途被撤销 | 时点 4 | take 标 `rights_state='void'`，不发 `ready` | `RIGHTS_BLOCKED` | **是** | 是 |
| F13 | S26 `SUBMISSION_UNKNOWN` | 事件 | 冻结，**不发新请求** | `WAITING_UNKNOWN` | 保留额度 | 是 |
| F14 | S28 归档不带 `observed_duration_ms` | 事件 | 阻塞在 `INGESTING` 并告警，不退化成假 `READY` | `INGESTING` | 是 | 是 |
| F15 | 归档 URL 过期 | 事件 | 向 S26 要新 URL，**不重新合成** | `INGESTING` | 否 | 否 |
| F16 | 归档 hash 不符 | 事件 | `FAILED(HASH_MISMATCH)` | `FAILED` | 是 | 是 |
| F17 | 归档时长为 0 或不可读 | 事件 | `FAILED(INVALID_AUDIO)` | `FAILED` | 是 | 是 |
| F18 | 供应商政策拒绝 | S26 事件 | 透传 `FAILED(POLICY_REJECTED)`，**不换渠道重试** | `FAILED` | 是 | 是 |
| F19 | 供应商账户撤销 | S26 事件 | `FAILED(ACCOUNT_UNAUTHORIZED)`，**不换渠道** | `FAILED` | 是 | 是 |
| F20 | 预算不足 | S26 事件 | `FAILED(BUDGET)`，不降采样率不缩短文本 | `FAILED` | 否 | 是 |
| F21 | 配额拒绝 | S26 事件 | `FAILED(QUOTA)`，S23 已补偿 | `FAILED` | 否 | 是 |
| F22 | 收到 `lipsync` 类缺陷的 `re_synthesize_speech` | 同步校验 | `422 ACTION_TYPE_MISMATCH` | — | 否 | 否 |
| F23 | `RepairAction` 的 `shot_lineage_id` 与本 job 不符 | 同步校验 | `422 LINEAGE_MISMATCH` | — | 否 | 否 |
| F24 | 同一 `command_id` 不同 body | 幂等 | `409 IDEMPOTENCY_CONFLICT` | — | 否 | 否 |
| F25 | 死线已过 | 同步校验 | `422 DEADLINE_PASSED` | — | 否 | 否 |
| F26 | 数据驻留禁止该 locale/region | `PREPARING` | `REJECTED_LOCAL(RESIDENCY_FORBIDDEN)` | `REJECTED_LOCAL` | 否 | 否 |

---

## 5. 数据模型

### 5.1 表清单

| 表 | 作用 | 可变性 |
|---|---|---|
| `speech_job` | 合成意图 | 状态可变，音色/指纹不可变 |
| `utterance_revision` | 台词文本版本 | 不可变 |
| `voice_binding_ref` | S15 音色绑定快照 | 不可变 |
| `lexicon_binding_ref` | 词典解析结果 | 不可变 |
| `voice_take` | 音频候选 | `rights_state` / `state` 可变，其余不可变 |
| `speech_checkpoint` | 生产基线指针 | 可变（CAS） |
| `rights_recheck_log` | 复查审计 | 只追加 |
| `speech_intent_ledger` | 生产意图台账 | 只追加 |
| `job_transition` | 状态转移历史 | 只追加 |
| `command_receipt` | 命令幂等回执 | 只追加 |
| `outbox` | 事件出站 | 只追加（`published_at` 可变） |
| `inbox` | 事件入站去重 | 只追加 |

### 5.2 核心 DDL

```sql
CREATE SCHEMA IF NOT EXISTS speech_production;

-- ---------- 台词文本版本（不可变） ----------
CREATE TABLE speech_production.utterance_revision (
  tenant_id        text NOT NULL,
  utterance_id     text NOT NULL,          -- 剧本级标识，跨语言不变
  text_revision    integer NOT NULL CHECK (text_revision >= 1),
  locale           text NOT NULL,
  speaker_character_id text NOT NULL,
  text             text NOT NULL,
  text_hash        char(64) NOT NULL,      -- sha256(规范化后的文本)，裸 hex
  emotion          text NOT NULL,
  shot_lineage_id  text,                   -- 可为空：旁白/画外音不绑镜头
  source_ref       jsonb NOT NULL,         -- {service_id:'S06'|'S04', id, revision}
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, utterance_id, locale, text_revision),
  CONSTRAINT ur_text_not_blank CHECK (btrim(text) <> ''),
  CONSTRAINT ur_source_ref_known CHECK ((source_ref->>'service_id') IN ('S04','S06')),
  CONSTRAINT ur_text_hash_len CHECK (length(text_hash) = 64)
);
CREATE UNIQUE INDEX ur_hash_uniq
  ON speech_production.utterance_revision
     (tenant_id, utterance_id, locale, text_hash);

-- ---------- S15 音色绑定快照（不可变） ----------
CREATE TABLE speech_production.voice_binding_ref (
  voice_binding_ref_id  text PRIMARY KEY,
  tenant_id             text NOT NULL,
  voice_id              text NOT NULL,      -- S15 的 'vox_...'
  voice_revision        integer NOT NULL CHECK (voice_revision >= 1),
  authorization_state   text NOT NULL
    CHECK (authorization_state IN ('granted','suspended','revoked','expired')),
  authorization_state_seq integer NOT NULL CHECK (authorization_state_seq >= 1),
  authorization_expires_at timestamptz,
  captured_at           timestamptz NOT NULL DEFAULT now(),
  -- 快照只用于事后解释，不用于放行；放行必须实时复查（§2.3）
  CONSTRAINT vbr_seq_required CHECK (authorization_state_seq >= 1)
);
CREATE INDEX vbr_voice_idx
  ON speech_production.voice_binding_ref (tenant_id, voice_id);

-- ---------- 词典绑定（不可变） ----------
CREATE TABLE speech_production.lexicon_binding_ref (
  lexicon_binding_ref_id text PRIMARY KEY,
  tenant_id              text NOT NULL,
  lexicon_id             text,              -- null = 未使用词典
  lexicon_revision       integer CHECK (lexicon_revision IS NULL OR lexicon_revision >= 1),
  content_hash           char(64),
  source_kind            text NOT NULL
    CHECK (source_kind IN ('explicit','voice_default','none')),
  scope_kind             text CHECK (scope_kind IN ('project','series','voice')),
  scope_id               text,
  captured_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT lbr_none_consistent CHECK (
    (source_kind = 'none' AND lexicon_id IS NULL
      AND lexicon_revision IS NULL AND content_hash IS NULL)
    OR
    (source_kind <> 'none' AND lexicon_id IS NOT NULL
      AND lexicon_revision IS NOT NULL AND content_hash IS NOT NULL)
  ),
  CONSTRAINT lbr_hash_len CHECK (content_hash IS NULL OR length(content_hash) = 64)
);

-- ---------- 合成意图 ----------
CREATE TABLE speech_production.speech_job (
  speech_job_id        text PRIMARY KEY,
  tenant_id            text NOT NULL,
  project_id           text NOT NULL,
  series_id            text NOT NULL,
  episode_id           text NOT NULL,
  branch_id            text NOT NULL,

  utterance_id         text NOT NULL,
  shot_lineage_id      text,                -- 旁白可为空；计数键由 S39 持有
  locale               text NOT NULL,
  text_revision        integer NOT NULL,

  voice_binding_ref_id text NOT NULL REFERENCES speech_production.voice_binding_ref,
  lexicon_binding_ref_id text NOT NULL REFERENCES speech_production.lexicon_binding_ref,

  -- ↓ 冻结列：NOT NULL 且任何代码路径不得 UPDATE（§4.3）
  voice_binding_id       text NOT NULL,
  voice_binding_revision integer NOT NULL CHECK (voice_binding_revision >= 1),

  emotion              text NOT NULL,
  output_format        text NOT NULL CHECK (output_format IN ('wav','flac','mp3')),
  sample_rate_hz       integer NOT NULL CHECK (sample_rate_hz >= 8000),
  require_alignment    boolean NOT NULL,

  node_id              text NOT NULL,
  node_epoch_at_create integer NOT NULL,
  execution_fence      integer NOT NULL,

  input_fingerprint    text NOT NULL,
  request_hash         text NOT NULL,
  idempotency_key      text NOT NULL,

  repair_action_id     text,
  strategy_class       text,
  variation_nonce      text NOT NULL,

  route_decision_ref   jsonb,
  budget_scope_id      text NOT NULL,
  deadline             timestamptz NOT NULL,

  provider_job_id      text,
  attempt_id           text,
  business_attempt_no  integer NOT NULL DEFAULT 1,
  quote_upper_bound_micro bigint,          -- 审计用；S23 才是权威

  state                text NOT NULL,
  state_seq            bigint NOT NULL DEFAULT 0,
  failure_code         text,
  failure_detail       jsonb,
  reconciled           boolean NOT NULL DEFAULT false,

  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  state_entered_at     timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT sj_state_check CHECK (state IN (
    'CREATED','CHECKING_RIGHTS','PREPARING','REJECTED_LOCAL','RIGHTS_BLOCKED',
    'SUBMITTED','WAITING_PROVIDER','WAITING_UNKNOWN','INGESTING','READY',
    'FAILED','RECONCILED_READY','RECONCILED_FAILED','CANCELED')),
  CONSTRAINT sj_tenant_idem UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT sj_fingerprint_len CHECK (length(input_fingerprint) = 64),
  CONSTRAINT sj_text_rev_positive CHECK (text_revision >= 1)
);

CREATE UNIQUE INDEX speech_job_open_uniq
  ON speech_production.speech_job
     (tenant_id, utterance_id, locale, input_fingerprint)
  WHERE state NOT IN ('READY','REJECTED_LOCAL','RIGHTS_BLOCKED',
                      'FAILED','RECONCILED_FAILED','CANCELED');

CREATE INDEX speech_job_shot_idx
  ON speech_production.speech_job
     (tenant_id, shot_lineage_id, utterance_id, created_at DESC);
CREATE INDEX speech_job_state_idx
  ON speech_production.speech_job (state, state_entered_at)
  WHERE state IN ('CHECKING_RIGHTS','PREPARING','SUBMITTED',
                  'WAITING_UNKNOWN','INGESTING');

ALTER TABLE speech_production.speech_job
  ADD CONSTRAINT sj_repair_uniq UNIQUE (tenant_id, repair_action_id);

-- ---------- 音频候选 ----------
CREATE TABLE speech_production.voice_take (
  voice_take_id        text PRIMARY KEY,
  tenant_id            text NOT NULL,
  speech_job_id        text NOT NULL REFERENCES speech_production.speech_job,
  utterance_id         text NOT NULL,
  locale               text NOT NULL,
  shot_lineage_id      text,

  artifact_ref         jsonb NOT NULL,     -- {service_id:'S28', id, revision, content_hash}
  observed_duration_ms integer NOT NULL CHECK (observed_duration_ms > 0),
  container_format     text NOT NULL,
  sample_rate_hz       integer NOT NULL CHECK (sample_rate_hz >= 8000),

  voice_binding_id       text NOT NULL,
  voice_binding_revision integer NOT NULL,
  lex  jsonb NOT NULL,                     -- 词典绑定的冗余拷贝，仅供解释，不含数值判断
  input_fingerprint    text NOT NULL,

  provider_job_id      text NOT NULL,
  attempt_id           text NOT NULL,
  alignment_report_ref jsonb,              -- S37 报告引用；只存引用，不存数值（§4.5）

  rights_state         text NOT NULL
    CHECK (rights_state IN ('usable','void','unknown')),
  rights_state_seq     integer,
  repair_action_id     text,
  strategy_class       text,

  state                text NOT NULL CHECK (state IN ('READY','ACCEPTED','SUPERSEDED')),
  created_at           timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT vt_artifact_is_s28 CHECK ((artifact_ref->>'service_id') = 'S28'),
  CONSTRAINT vt_hash_len CHECK (length(artifact_ref->>'content_hash') = 64),
  CONSTRAINT vt_void_has_seq CHECK (
    rights_state <> 'void' OR rights_state_seq IS NOT NULL)
);

CREATE UNIQUE INDEX vt_artifact_uniq
  ON speech_production.voice_take
     (tenant_id, (artifact_ref->>'id'), (artifact_ref->>'content_hash'));
CREATE INDEX vt_utterance_idx
  ON speech_production.voice_take
     (tenant_id, utterance_id, locale, created_at DESC);
CREATE INDEX vt_reuse_idx
  ON speech_production.voice_take (tenant_id, input_fingerprint)
  WHERE rights_state = 'usable' AND state IN ('READY','ACCEPTED');

-- ---------- 配音检查点 ----------
CREATE TABLE speech_production.speech_checkpoint (
  tenant_id        text NOT NULL,
  shot_lineage_id  text NOT NULL,
  utterance_id     text NOT NULL,
  locale           text NOT NULL,
  voice_take_id    text NOT NULL REFERENCES speech_production.voice_take,
  checkpoint_seq   bigint NOT NULL,
  expected_revision integer NOT NULL DEFAULT 0,
  updated_by_job   text NOT NULL,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, shot_lineage_id, utterance_id, locale)
);

-- ---------- 权利复查审计 ----------
CREATE TABLE speech_production.rights_recheck_log (
  recheck_id           uuid PRIMARY KEY,
  tenant_id            text NOT NULL,
  speech_job_id        text NOT NULL,
  voice_id             text NOT NULL,
  check_point          text NOT NULL
    CHECK (check_point IN ('submit_sync','worker_start','pre_dispatch',
                           'post_ingest','event_driven')),
  observed_state       text NOT NULL
    CHECK (observed_state IN ('granted','suspended','revoked','expired',
                              'unreachable')),
  observed_state_seq   integer,
  eligible             boolean NOT NULL,
  deny_reason          text,
  checked_at           timestamptz NOT NULL DEFAULT now(),
  -- unreachable 时必须 eligible=false：S15 不可达不构成"可用"的证据
  CONSTRAINT rrl_unreachable_not_eligible CHECK (
    observed_state <> 'unreachable' OR eligible = false)
);
CREATE INDEX rrl_job_idx
  ON speech_production.rights_recheck_log (tenant_id, speech_job_id, checked_at DESC);
CREATE INDEX rrl_voice_idx
  ON speech_production.rights_recheck_log (tenant_id, voice_id, checked_at DESC);

-- ---------- 生产意图台账 ----------
CREATE TABLE speech_production.speech_intent_ledger (
  entry_id         uuid PRIMARY KEY,
  tenant_id        text NOT NULL,
  speech_job_id    text NOT NULL,
  utterance_id     text NOT NULL,
  shot_lineage_id  text,
  locale           text NOT NULL,
  action           text NOT NULL
    CHECK (action IN ('generate','re_synthesize_speech','locale_dub')),
  strategy_class   text,
  repair_action_id text,
  voice_id         text NOT NULL,
  run_id           text NOT NULL,
  node_epoch       integer NOT NULL,
  manifest_id      text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sil_shot_idx
  ON speech_production.speech_intent_ledger
     (tenant_id, shot_lineage_id, created_at DESC);

-- ---------- 状态转移历史 ----------
CREATE TABLE speech_production.job_transition (
  speech_job_id  text NOT NULL,
  seq            bigint NOT NULL,
  from_state     text,
  to_state       text NOT NULL,
  reason_code    text,
  detail         jsonb,
  at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (speech_job_id, seq)
);

-- ---------- 基础设施 ----------
CREATE TABLE speech_production.command_receipt (
  tenant_id     text NOT NULL,
  command_id    text NOT NULL,
  request_hash  char(64) NOT NULL,
  response_body jsonb NOT NULL,
  completed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);
CREATE UNIQUE INDEX cr_hash_uniq
  ON speech_production.command_receipt (tenant_id, command_id, request_hash);

CREATE TABLE speech_production.outbox (
  outbox_id          uuid PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,
  aggregate_sequence bigint NOT NULL,
  event_type         text NOT NULL,
  partition_key      text NOT NULL,        -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  published_at       timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx
  ON speech_production.outbox (outbox_id) WHERE published_at IS NULL;

CREATE TABLE speech_production.inbox (
  consumer     text NOT NULL,
  event_id     text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  PRIMARY KEY (consumer, event_id)
);
```

**中继**：与 [S08 §6.4](S08-revision-snapshot.md) 同一形状——`SELECT ... WHERE published_at IS NULL ORDER BY outbox_id LIMIT 100 FOR UPDATE SKIP LOCKED`，同一 `partition_key` 单线程发布；失败重试不阻塞其他 partition；`published_at + 7d` 归档。重启无需恢复逻辑，未发布的行走同一条路径。

**为什么 `production.speech.ready` 必须与 `VoiceTake` + 检查点同事务：**

若拆成两步，中间崩溃会留下两种坏状态：

1. **take 已写、`ready` 未发**：S30 以为这一镜没有对白音频，可能按 `silent_then_tts` 的"音频还没来"继续等，或（更糟）按无音频分支出片。S41 会把这一镜剪成静音。
2. **`ready` 已发、take 回滚**：S32 拿着一个 `voice_take_id` 去要音频，得到 404。S38 的对齐请求指向一个不存在的 take。

两种都能靠"以 S31 的表为准绳重查"发现，但都会让一集的部分镜头在几十分钟里处于无法解释的状态。同事务把这类状态从"可能"变成"不可能"。

**为什么 `voice_take.lex` 是一列 jsonb 而不是外键：**

它是 `lexicon_binding_ref` 的冗余拷贝，**只用于事后解释**（"这个 take 当时用的是哪版词典"），不参与任何判断。用外键会让"删不改"的历史 take 依赖一张可能被清理的绑定表。jsonb 拷贝是**故意的反规范化**，且约束上不加任何可用于推断的检查——判断必须走 `lexicon_binding_ref`。

### 5.3 约束理由

| 约束 | 防的是什么 |
|---|---|
| `sj_tenant_idem` | E12：同键同 body 重放，同键异 body 由 `command_receipt` 的 `cr_hash_uniq` 判 409 |
| `speech_job_open_uniq` | E16：UNKNOWN 期间另建 job；也防同一指纹并发双提交 |
| `sj_repair_uniq` | 同一个 `RepairAction` 被投递两次不能产生两个付费 job |
| `sj_state_check` | 状态枚举闭集；新增状态 = 发版 |
| `vt_artifact_is_s28` | 供应商直链或本地路径不能进 `artifact_ref` |
| `vt_artifact_uniq` | 同一 artifact 重复登记成两个 take |
| `vt_void_has_seq` | 标 void 必须说明依据哪个授权版本，否则不可审计 |
| `rrl_unreachable_not_eligible` | **S15 不可达等于不可用**。这条是 E30 的硬落点：不能因为"查不到"而放行 |
| `lbr_none_consistent` | `source_kind='none'` 却带着 `lexicon_id`，或反之，都会让"用了哪版词典"无法回答 |
| `ur_text_not_blank` | F1：空台词不能合成 |
| `observed_duration_ms > 0` | 允许 0 会退化成假 `READY`（F17） |
| `sj_voice_frozen`（由 NOT NULL + 不可 UPDATE 实现） | §4.3 的换声线禁令 |

**`rrl_unreachable_not_eligible` 值得展开：** 这是全文最重要的一条 CHECK。

"S15 挂了怎么办"有三种答案，只有一种是对的：

- 假设它大概还是 granted，先提交 → **错**。这就是用已撤销音色花钱的路径，而且日志上看起来完全正常。
- 阻塞并重试到 S15 可用 → 对，但要防死等（有 `deadline`，超时 `FAILED(DEPENDENCY_TIMEOUT)`）。
- 记成 `unreachable` 但 `eligible=false` → 对，且它把一个时间窗口性的事实变成了一行可查的审计记录。

约束把第一种从"技术上可能"变成"数据库拒绝"。

---

## 6. 对外契约

### 6.1 `POST /speech-jobs`

```jsonc
{
  "command_id": "cmd-sp-0001",
  "request_hash": "sha256:…",
  "context": { /* execution-context.v2 */ },
  "utterance": {
    "utterance_id": "utt_3_2_02",
    "shot_lineage_id": "lin_ep3_sc3_02",
    "text": "你终于来了。",
    "text_revision": 2,
    "locale": "zh-CN",
    "speaker_character_id": "char_linlan",
    "source_ref": { "service_id": "S06", "id": "shot_3_2", "revision": 8 }
  },
  "voice_binding": { "id": "vox_linlan", "revision": 2 },
  "emotion": "紧张克制",
  "pronunciation_lexicon": null,
  "output_spec": { "format": "wav", "sample_rate_hz": 48000 },
  "require_alignment": true,
  "repair_action_id": null,
  "route_policy_ref": { "service_id": "S25", "id": "routing_demo", "revision": 1, "content_hash": "…" },
  "budget_scope_id": "budget_ep3",
  "deadline": "2026-11-02T23:30:00+08:00"
}
```

响应 `202`：

```jsonc
{
  "speech_job_id": "sj_01J…",
  "state": "CREATED",
  "operation_id": "op_…",
  "reused": false
}
```

复用时 `200`：

```jsonc
{
  "speech_job_id": null,
  "reused": true,
  "voice_take_id": "vt_01J…",
  "artifact_ref": { "service_id": "S28", "id": "audio_…", "revision": 1, "content_hash": "…" },
  "audio_duration_ms": 3280,
  "note": "input_fingerprint 命中已有 take；未创建付费任务（§4.4）"
}
```

**`reused: true` 时不返回 `speech_job_id`。** 返回一个 job id 会让调用方以为可以轮询它，而那个 job 可能是几天前的。`voice_take_id` 才是要用的东西。

**校验顺序**见 §4.1 第 2 步，逐条对应 F1–F26。

### 6.2 `GET /speech-jobs/{id}`

```jsonc
{
  "speech_job_id": "sj_01J…",
  "source_revision": 12,
  "state": "READY",
  "business_attempt_no": 1,
  "provider_job_id": "pj_01J…",
  "utterance": {
    "utterance_id": "utt_3_2_02", "text_revision": 2, "locale": "zh-CN",
    "text_hash": "…"
  },
  "voice_binding": {
    "id": "vox_linlan", "revision": 2,
    "authorization_state_at_submit": "granted",
    "authorization_state_seq_at_submit": 7
  },
  "lexicon": {
    "lexicon_id": "lex_proj_ep", "revision": 4,
    "content_hash": "…", "source_kind": "voice_default", "scope_kind": "project"
  },
  "input_fingerprint": "…",
  "take": {
    "voice_take_id": "vt_01J…",
    "artifact_ref": { "…": "…" },
    "observed_duration_ms": 3280,
    "rights_state": "usable",
    "state": "READY",
    "alignment_report_ref": null
  },
  "checkpoint": {
    "shot_lineage_id": "lin_ep3_sc3_02",
    "utterance_id": "utt_3_2_02",
    "locale": "zh-CN",
    "voice_take_id": "vt_01J…",
    "checkpoint_seq": 3
  },
  "frozen": false,
  "must_reconcile_before_resubmit": false,
  "rights_rechecks": [
    { "check_point": "worker_start", "observed_state": "granted",
      "observed_state_seq": 7, "eligible": true, "checked_at": "…" },
    { "check_point": "pre_dispatch", "observed_state": "granted",
      "observed_state_seq": 7, "eligible": true, "checked_at": "…" }
  ],
  "history": [ { "seq": 5, "from": "INGESTING", "to": "READY", "at": "…" } ]
}
```

**`rights_rechecks` 出现在查询响应里是刻意的。** 这是 S31 唯一能自证"我没有用撤销的音色"的地方。运维、法务、审计问的是"你凭什么说这次合成是有授权的"，答案必须能从 API 直接拿到，而不是"去数据库里翻日志"。

### 6.3 其他查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/speech-jobs?shot_lineage_id=&utterance_id=&locale=&state=` | 列表 |
| GET | `/shots/{shot_lineage_id}/speech-checkpoint?locale=` | 该镜全部台词的当前基线 take（**S30 / S41 / S32 的主读口**） |
| GET | `/voice-takes/{id}` | take 详情 + 词典绑定 + 复查链 |
| GET | `/voice-takes/{id}/reuse-eligibility` | 该 take 能否被指纹复用（`usable` + `REFERENCEABLE` + 未 tombstone） |
| POST | `/speech-jobs/{id}:cancel` | 取消 |
| POST | `/speech-jobs/{id}:retry` | 仅 `{FAILED, RECONCILED_FAILED}`；`WAITING_UNKNOWN` 与 `RIGHTS_BLOCKED` 时 **409** |

`RIGHTS_BLOCKED` 对 `:retry` 返回 409 而不是 422，是为了让"不可重试"这一类与"参数错了"在客户端可区分：前者需要人来改授权，后者改请求就行。

### 6.4 事件

**发布：**

| event_type | aggregate | 何时 |
|---|---|---|
| `production.speech.ready` | `shot_lineage_id`（旁白为 `utterance_id`） | 检查点推进 |
| `speech.job.state-changed`（建议新增） | `speech_job_id` | 每次转移 |
| `speech.voice-authorization-lapsed`（建议新增） | `voice_id` | `RIGHTS_BLOCKED` 产生或 take 变 void |
| `speech.utterance-revision.recorded`（建议新增） | `utterance_id` | 新文本版本落库 |

`production.speech.ready` 的 `data`：

```jsonc
{
  "speech_job_id": "sj_01J…",
  "voice_take_id": "vt_01J…",
  "utterance_id": "utt_3_2_02",
  "utterance_index": 2,
  "shot_lineage_id": "lin_ep3_sc3_02",
  "locale": "zh-CN",
  "text_hash": "…",
  "artifact_ref": { "service_id": "S28", "id": "…", "revision": 1, "content_hash": "…" },
  "audio_duration_ms": 3280,
  "dialogue_span_ms": null,
  "dialogue_span_source": "pending_s37",
  "voice_binding": { "id": "vox_linlan", "revision": 2 },
  "lexicon_ref": { "id": "lex_proj_ep", "revision": 4, "source_kind": "voice_default" },
  "checkpoint": { "from": "vt_00X…", "to": "vt_01J…", "checkpoint_seq": 3 },
  "require_alignment": true,
  "rights_state": "usable"
}
```

**不含**：音频 URL、音频 bytes、供应商信息、声纹样本引用、授权 `evidence_ref`。这些要走 S28 的短签名访问与 S15 的受控读口。

**消费：**

| 来源 | 事件 | 用途 |
|---|---|---|
| S26 | `generation.job.state-changed` | 驱动状态机 |
| S28 | `artifact.ingested` | 完成 `INGESTING → READY`；**需要 `observed_duration_ms`、`container`、`sample_rate_hz`、`channel_count`**（§12 C2） |
| S15 | `asset.voice.authorization.changed` | **只用于收紧**（§2.3）：撤销时标 void、冻结在途 |
| S08 | `snapshot.manifest.committed` | 只读投影；`node_epoch` 变化不改本地状态 |
| S39 | `quality.repair.planned` | 只读，用于提前知道有 `re_synthesize_speech` 在路上；**不据此自动创建 job** |
| S37 | `quality.alignment.completed` | 把 `alignment_report_ref` 挂到 take 上；**不据此改任何状态** |
| S41 | `post.timeline.revision.created` | 只读。镜头被删时 take 变 `SUPERSEDED`（不是 void）；S31 不重切音频 |

**S31 不消费 `quality.*` 来自动重做。**

---

## 7. 幂等与并发

### 7.1 幂等

| 层面 | 键 | 冲突 |
|---|---|---|
| 命令 | `tenant + command_id + request_hash` | 同体返回原响应；异体 `409 IDEMPOTENCY_CONFLICT` |
| 业务 | `(tenant, utterance_id, locale, input_fingerprint)` 未终结 | 返回已有 job（不新建） |
| 修复 | `(tenant, repair_action_id)` | `409 REPAIR_ALREADY_APPLIED`，返回首次 job |
| 复用 | `(tenant, input_fingerprint)` 且 `rights_state='usable'` | 返回已有 take，`reused=true`（§4.4） |
| 事件入站 | `(consumer, event_id)` | 丢弃重复 |

`request_hash` 含：`context` 业务字段、`utterance` 全部字段、`voice_binding`、`emotion`、`pronunciation_lexicon`、`output_spec`、`require_alignment`、`repair_action_id`、`budget_scope_id`、`deadline`。**不含** `trace_id`、`command_id`。

**业务幂等与复用的区别：** 业务幂等是"同一个未完成的意图不要建两个 job"；复用是"已经完成的同一意图不要再花钱". 两者都收敛到同一个 take，但前者发生在 job 生命周期内，后者发生在 job 已经不存在（或早已终结）之后。

### 7.2 并发

| 场景 | 机制 |
|---|---|
| 同 utterance 两个 job 同时 READY | `speech_checkpoint` 乐观锁（`expected_revision`）CAS |
| 一镜多句并发合成 | 互不影响（不同 PK 行；检查点按 `utterance_id` 分行） |
| 同一 `RepairAction` 并发投递两次 | `sj_repair_uniq` 唯一约束 |
| 复用与新建同时发生 | 复用走 `REUSE_IDX` 部分索引读；新建走 `speech_job_open_uniq`。极端情况下两个都成功——但只有一个 take 能推进检查点，另一个变 `SUPERSEDED` |
| `asset.voice.authorization.changed` 与 `INGESTING→READY` 同时到 | 两者都要写 `rights_state`。规则：**void 优先**。先处理事件者写 void，后到者读到的 job 已 `RIGHTS_BLOCKED`，不推进检查点 |
| 词典 revision 在 job 运行中被 S15 更新 | 无影响：`LexiconBindingRef` 已冻结。新 revision 只影响新 job |
| 文本 revision 在 job 运行中变化 | 无影响：`speech_job.text_revision` 冻结。新 revision 产生新指纹 |

**"void 优先"这条规则必须写进代码而不是靠时序运气。** 实现方式：`UPDATE voice_take SET rights_state='void' WHERE ... AND rights_state <> 'void'` 与 `READY` 转移在同一个 `SERIALIZABLE` 事务里，且 `READY` 转移前**必须**在事务内重读 `rights_state`。若读不到 `usable`，就不推进。

### 7.3 重启恢复

- `CHECKING_RIGHTS` 重启后**必须重做**复查，不能沿用崩溃前的内存结论（那正是 §4.2 时点 2 要求的）。
- `PREPARING` 重启后必须**重新取 RouteDecision**——报价可能已过期（`quote_valid_to`）。
- `SUBMITTED` / `WAITING_PROVIDER` 重启后向 S26 查状态（`GET /generation-jobs/{id}`），不自造状态。
- `INGESTING` 重启后必须**重新探测 `observed_duration_ms`**，不能沿用崩溃前的值（与 [S30 §7.3](S30-video-production.md) 同）。
- `WAITING_UNKNOWN` 重启后**什么都不做**，等 S26 对账（E16）。
- `RIGHTS_BLOCKED` 重启后**什么都不做**，它是终态。

---

## 8. 跨 Run 尝试计数与"复用不计数"

S31 不数数——上限在 S39。但 S31 有三个地方**会破坏** S39 的计数，必须各自防住。

### 8.1 计数键是 `shot_lineage_id`，不是 `utterance_id`

[S39 §5.2](S39-repair-planning.md) 的 `AttemptCounterKey = (tenant, series, branch, shot_lineage_id, strategy_class)`。

配音的特殊之处：**一镜多句**。一个 4 秒镜头可能有 3 句台词（不同角色或短句连说）。如果 S31 把 `utterance_id` 也送进计数，就会出现"这个镜头的第 2 句重做了 3 次，但镜头总计看起来还是 0 次"。

**S31 的实现约束：**

- `speech_intent_ledger.shot_lineage_id` **必须与 S39 的键一致**，且**不能从 `utterance_id` 派生**——由 `RepairAction` 或 `UtteranceRevision.source_ref` 带来。
- `speech_intent_ledger.utterance_id` 只作审计列，**不进任何上限键**。
- 旁白、画外音（`shot_lineage_id IS NULL`）不参与镜头级上限；它们归 S21 的 run 预算约束。这是一个已知的计数盲区（§11 U4）。

### 8.2 复用不产生台账行

§4.4 的复用**不写 `speech_intent_ledger`**。

理由：台账记录的是"产生了一次付费生产意图"。复用没有产生生产意图，它只是把已有产物递给调用方。如果复用写台账，S39 会把它算成一次 attempt，于是：

- 导演重跑一遍同一集（什么都没改），所有配音都"复用"，但记账上消耗了全部 attempt 额度；
- 真正需要修复时，额度已被"没做事"消耗光。

这是最容易犯的错，因为"写一条台账"看起来是良性的、只是在多记一笔。

**反过来，由 `repair_action_id` 触发的 job 一定写台账，且一定不复用**（§4.4 第 1 条条件）。两者互补，覆盖了"不该记的别记、该记的一定记"。

### 8.3 换语言不是重做，是另一次生产

多语言配音：同一 `utterance_id`、不同 `locale`，产生不同的 `input_fingerprint`、不同的 take、**不同的台账行**。

这不是"重做"，因为产出物是另一个语种的音频，两者都要交付。计数语义上它们共享同一个 `shot_lineage_id`，所以**翻译配音会消耗该镜头的 attempt 额度**——这一点需要 S39 / S21 在预算规划时知道（§11 U5）。

S31 不做例外处理：它只如实把 `shot_lineage_id` 与 `locale` 都写进台账，谁数、怎么数由 S39 决定。

### 8.4 例子

| Run | 事件 | `action` | `utterance_id` | `shot_lineage_id` | `strategy_class` | 台账 | S39 累计 |
|---|---|---|---|---|---|---|---|
| Run 1 | 初始合成 3 句 | `generate` | `utt_a`,`utt_b`,`utt_c` | `lin_02` | — | 3 行 | 该镜 3 次（每句策略不同） |
| Run 2 | 重跑整集，什么都没改 | —（复用） | — | — | — | **0 行** | 不变 |
| Run 2 | `voice` 缺陷：念错音 | `re_synthesize_speech` | `utt_b` | `lin_02` | `re_synthesize_speech:...` | 1 行 | +1 |
| Run 3 | 导演改了台词文本 | `re_synthesize_speech` | `utt_b` | `lin_02` | 同上（文本变化不进策略类） | 1 行 | +1 |
| Run 4 | 音色被撤销 | 拒绝 | — | — | — | 0 行 | **不变，转人审** |

第 2 行与第 3 行的对比是核心：**复用看起来像什么都没做，而它确实是**。第 4 行说明权利失效不消耗额度——它不是一次修复尝试，是一次"不能做"。

---

## 9. 可观测

| 指标 | 类型 | 用途 | 告警 |
|---|---|---|---|
| `s31_jobs{state}` | gauge | 堆积 | `WAITING_UNKNOWN` > 0 |
| `s31_rights_blocked_total{deny_reason}` | counter | 权利失效频率 | **任何 `AUTHORIZATION_REVOKED` 都告警到权利负责人** |
| `s31_revoked_after_submit_total` | counter | 提交后才撤销 | **任何一次都告警**（说明复查窗口还不够紧或有绕过） |
| `s31_rights_recheck_fail_open_total` | counter | S15 不可达却放行 | 恒为 0；非 0 即严重缺陷 |
| `s31_reuse_hit_ratio` | gauge | 复用率 | 重跑整集时应接近 1；持续 < 0.3 = 指纹设计有问题或上游文本抖动 |
| `s31_paid_calls_total` | counter | 本服务发起的付费合成数 | 与 S26 按 `modality=speech` 的计数**每日对齐**，差异 > 0 = 存在绕过 S26 的调用 |
| `s31_job_duration_seconds{locale,sample_rate_hz}` | histogram | 延迟 | p99 超 SLO |
| `s31_audio_duration_ms{locale}` | histogram | 单句时长分布 | 分布右移 = 文本或情绪异常 |
| `s31_alignment_required_but_missing_total` | counter | 需要词级时间戳但没有 | **任何一次都告警**（S38 会缺输入） |
| `s31_lexicon_resolution{source_kind}` | counter | 词典来源分布 | `voice_default` 占比骤降 = 调用方开始乱传词典 |
| `s31_lexicon_scope_reject_total` | counter | 词典范围不符 | 突增 = 上游传错项目 |
| `s31_empty_utterance_reject_total` | counter | 空台词被拒 | 突增 = 上游把无对白当有对白处理 |
| `s31_hash_mismatch_total` | counter | 归档 hash 不符 | 任何一次都告警 |
| `s31_missing_observed_duration_total` | counter | S28 未报时长 | 任何一次都告警（§12 C2） |
| `s31_assertion_failures_total{kind}` | counter | 内部断言 | **任何一次都告警** |

**`s31_rights_recheck_fail_open_total` 恒为 0 这件事本身要被监控。** 一个永不触发的告警看起来是浪费，但这条指标是 §5.3 那条 CHECK 约束的运行时孪生：约束防数据库写入，指标防"有人在应用层先判了 eligible 再写库"。

### 9.2 追踪

`traceparent` 贯穿 `POST /speech-jobs → S15 check-eligibility → PREPARING → S26 → S27 → S28 → production.speech.ready → S37`。

必须能回答："这一句台词用的哪版文本、哪个音色版本、哪份词典、哪个情绪、提交时和归档时授权是什么状态、合成花了多少钱、实际多长、对齐报告在哪"。前者由 S31 的表答，对齐由 S37 答，钱由 S23 答。

追踪维度沿用 `docs/design-package/services/S31-speech-production.md` 的定义：`tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace`。

### 9.3 日志

不记台词原文（可能是未公开剧情）、不记声纹样本引用、不记授权 `evidence_ref`、不记供应商 URL。

记：`input_fingerprint`、`text_hash`、`voice_id`、`voice_revision`、`lexicon_id`、`state_seq`、`deny_reason`、`observed_duration_ms`。

**台词文本属于敏感内容。** 它比图像更容易被 grep、被日志聚合工具索引、被误发到工单。日志里只出现 `text_hash` 与长度，需要看原文的人走 S04 的受控读口（[docs/14](../docs/14-enterprise-operations.md) 最小化原则）。

---

## 10. 测试清单

| 编号 | 场景 | 期望 | E |
|---|---|---|---|
| T31-01 | 同 command 重复 | 返回原响应 | E12 |
| T31-02 | 同 command 异 body | `409` | E12 |
| T31-03 | 空文本 | `422 EMPTY_UTTERANCE`，不建 job | E04 |
| T31-04 | 纯空白文本 | 同上 | E04 |
| T31-05 | 文本变但 `text_revision` 未变 | `422 TEXT_REVISION_CONFLICT` | E12 |
| T31-06 | 相同指纹重跑整集 | 全部 `reused=true`，`paid_calls=0` | E38 |
| T31-07 | 相同指纹但带 `repair_action_id` | **不复用**，创建新 job | E24 |
| T31-08 | 复用命中但 artifact 已 tombstone | 不复用，走新 job | E23 |
| T31-09 | 复用命中但 `rights_state='void'` | 不复用，`409 VOICE_AUTHORIZATION_REVOKED` | E30 |
| T31-10 | 音色 `revoked`，提交前 | `RIGHTS_BLOCKED`，不花钱，发 lapse 事件 | E30 |
| T31-11 | 音色 `revoked`，`PREPARING` 之后才发生 | 时点 3 复查拦截，`RIGHTS_BLOCKED`，不提交 | E30 |
| T31-12 | 音色 `revoked`，提交后、归档前 | 时点 4 复查：take 标 void，**不发 `ready`** | E30 |
| T31-13 | 音色 `revoked`，已 `READY` | 事件驱动标 void，排除出检查点 | E30 |
| T31-14 | 音色 `expired` | 同上，`deny_reason=AUTHORIZATION_EXPIRED` | E30 |
| T31-15 | 音色 `suspended` | `RIGHTS_BLOCKED`，**不换声线**，不重路由 | E30 |
| T31-16 | 请求里带 `use=audiobook`，但只授权影视 | `RIGHTS_BLOCKED`，`USE_NOT_AUTHORIZED` | E30 |
| T31-17 | S15 不可达 | `unreachable` + `eligible=false`，不提交；日志有记录 | E30 |
| T31-18 | 尝试用相似音色替换被撤销的音色 | **没有代码路径**：`voice_binding_*` NOT NULL 且不可 UPDATE | E30 |
| T31-19 | `RIGHTS_BLOCKED` 的 job 调 `:retry` | `409` | E30 |
| T31-20 | 撤销后 `granted` 事件迟到 | 不自动复活 `RIGHTS_BLOCKED` | E30 |
| T31-21 | 词典显式给出 | `source_kind='explicit'`，用的就是它 | E02 |
| T31-22 | 词典为 null 且 S15 有音色默认绑定 | `source_kind='voice_default'`，**不是** `none` | E02 |
| T31-23 | 词典为 null 且 S15 无绑定 | `source_kind='none'`，不吃词表 | E02 |
| T31-24 | 词典属别的项目 | `422 LEXICON_SCOPE_MISMATCH` | E28 |
| T31-25 | 词典属别的租户 | `404` | E28 |
| T31-26 | `require_alignment=true`，路由无词级时间戳 | `REJECTED_LOCAL(CAPABILITY_MISSING)`，**不降级** | E18 |
| T31-27 | provider 返回词级时间戳 | 只记 `evidence` 引用，**不进 take 时间字段** | E24 |
| T31-28 | S26 `SUBMISSION_UNKNOWN` | `WAITING_UNKNOWN`，不发新请求 | E16 |
| T31-29 | UNKNOWN 期间同指纹 | 唯一约束/返回原 job | E16 |
| T31-30 | UNKNOWN 期间 deadline 逼近 | **不重提**；到点 `FAILED(DEADLINE)` | E16 |
| T31-31 | 归档 URL 过期 | 向 S26 要新 URL，**不重新合成** | E23 |
| T31-32 | S28 不带 `observed_duration_ms` | 阻塞 `INGESTING` + 告警，不假 READY | E23 |
| T31-33 | `observed_duration_ms = 0` | `FAILED(INVALID_AUDIO)` | E23 |
| T31-34 | 归档 hash 不符 | `FAILED(HASH_MISMATCH)` | E23 |
| T31-35 | `voice_take.artifact_ref` 的 `service_id='S31'` 或 `'openai'` | DB 拒绝 | E01 |
| T31-36 | 收到 `lipsync` 类的 `re_synthesize_speech` | `422 ACTION_TYPE_MISMATCH` | E24 |
| T31-37 | 收到 `redo_lipsync` | `422`，不参与 | E24 |
| T31-38 | `RepairAction` 的 lineage 与 job 不符 | `422 LINEAGE_MISMATCH` | E26 |
| T31-39 | 一镜 3 句，其中 1 句重做 | 台账 4 行；`shot_lineage_id` 全同 | E26 |
| T31-40 | 重跑整集（全复用） | 台账 **0 行**，累计不变 | E26 |
| T31-41 | 多语言配音 | `locale` 进指纹，产生新 take + 新台账行 | E02 |
| T31-42 | 检查点并发 CAS | 一个成功一个重读 | E20 |
| T31-43 | 撤销事件与 `READY` 并发 | void 优先，一个结果 | E30 |
| T31-44 | 重启于 `CHECKING_RIGHTS` | 重做复查，不沿用内存结论 | E13 |
| T31-45 | 重启于 `INGESTING` | 重新探测时长 | E13 |
| T31-46 | 跨租户 take / job | `404` | E28 |
| T31-47 | 预算不足 | `FAILED(BUDGET)`，不降采样率不缩文本 | E14 |
| T31-48 | 配额拒绝 | `FAILED(QUOTA)`，S23 已补偿 | E14 |
| T31-49 | 供应商账户撤销 | `FAILED(ACCOUNT_UNAUTHORIZED)`，不换渠道 | E18 |
| T31-50 | 数据驻留禁止该 locale | `REJECTED_LOCAL(RESIDENCY_FORBIDDEN)` | E31 |
| T31-51 | Kafka 重放 `production.speech.ready` | 幂等消费，不触发二次合成 | E32 |
| T31-52 | 独立升级回滚 | 状态机表驱动，新旧版本一致 | E01 |

---

## 11. 实施顺序与未决

### 11.1 顺序（W3）

1. 表 + 状态机骨架 + `sj_*` / `vt_*` / `rrl_*` 约束（T31-01/02/03/35/42）。
2. `UtteranceRevision` + 文本版本冲突检测（T31-05）。
3. S15 对接（mock）：复查五个时点、`deny_reason` 全枚举、不可达（T31-10…20）。
4. **`RIGHTS_BLOCKED` 终态与"无换声线路径"的形式化验证**（T31-18/19/20）。这一步要在任何付费通路接通之前完成。
5. 词典三级解析（T31-21…25）。
6. 输入指纹 + 复用（T31-06/07/08/09/40）。
7. S25 对接（mock）：`word_timestamps` 能力（T31-26）。
8. S26 对接（mock）：预算/配额/UNKNOWN（T31-28…30、47、48）。
9. S28 对接（mock）：时长、URL 过期、hash（T31-31…34）。
10. 动作族守卫（T31-36…38）。
11. 跨 Run 台账（T31-39/40/41）。
12. 巡检、告警、runbook。
13. 真实渠道需授权。

### 11.2 未决

| # | 问题 | 当前处理 |
|---|---|---|
| U1 | `observed_duration_ms` 由谁提供 | **阻塞**（§12 C2）。暂按 S28 提供；若不提供，需改为"等 S37"或"用 S26 元数据（可靠性中）" |
| U2 | `emotion` 是自由字符串，S31 无法归一化 | 现按原样透传并进指纹。若 S39 需要对 `re_synthesize_speech` 做策略类归一，需要 S06/S02 给出情绪词表（§12 C5） |
| U3 | `re_synthesize_speech` 的 `strategy_class` 归一参数 | **未定义**。[S39 §5.3](S39-repair-planning.md) 的表里没有这个 action（§12 C7）。当前 S31 写 `re_synthesize_speech:<emotion>`，是临时方案 |
| U4 | 旁白/画外音的计数归谁 | 当前 `shot_lineage_id IS NULL`，不参与镜头级上限。由 S21 的 run 预算约束。是否要给旁白一个"虚拟镜头"身份，未决 |
| U5 | 多语言配音是否消耗镜头 attempt 额度 | 当前会（共享 `shot_lineage_id`）。需求方需确认：翻译配音是"重做"还是"另一次生产" |
| U6 | 一句台词超长（如 12 秒）如何处理 | 当前只报 `audio_duration_ms`，由 S37 的 `AUD.ALIGN.DURATION_VS_SPEC` 判定。S31 不切分、不加速语速 |
| U7 | 音色的 `provider_voice_mapping` 换行后旧 take 的解释 | S15 的映射"只能追加"（[S15 §1.5](S15-voice-assets.md)），旧 take 靠旧行解释。S31 不存映射，也不判断 |
| U8 | 是否支持"同一句多情绪版本由导演挑" | 当前不支持（§2.2）。若需要，走三次独立 `SpeechJob` + S40 裁决 |
| U9 | 音频是否需要通道数/响度规格 | `output_spec` 只有 `format` + `sample_rate_hz`。响度由 S42 统一（§12 C9） |
| U10 | 语音的 `channel_count` 默认值 | 未定。暂按 mono 输出，由 S42 上混 |

---

## 12. 冲突与契约缺口

### C1 `contracts/openapi.v2.yaml` 完全没有 S31 的路径

`openapi.v2.yaml`（885 行）里没有任何 `/speech-jobs` 路径。本文 §6 的接口（包括 `POST /speech-jobs`、`GET /speech-jobs/{id}`）在 `service.yaml` 的 `api` 里声明了，但**没有对应的 OpenAPI 定义**，也没有请求/响应 schema。

**同类**：S29 / S30 的接口同样不在 `openapi.v2.yaml`（各自方案已在缺口里记录）。

**不选边**：本文按 `service.yaml` 声明的路径实现，不自行补一份 OpenAPI。真实客户端只能依 `generation-command.v2` 与本文 §6 的 JSONC 例子。

**建议**：补 `contracts/openapi/` 分服务定义，或至少在 `openapi.v2.yaml` 里补这 48 个服务的路径骨架。

### C2 `artifact.ingested` 没有 payload schema，S31 依赖它的 `observed_duration_ms`

`event-topics.v2.json` 里 `artifact.ingested` 的 `schema_ref` 是 `event-envelope.v2.schema.json`——payload 无类型约束。S31 的 `INGESTING → READY` 强依赖 `observed_duration_ms`、`container`、`sample_rate_hz`、`channel_count`。

**冲突点**：`docs/09 §3` 只说"输出下载失败只重归档"，没要求 S28 探测并报告音频时长；S28 的 `service.yaml` `acceptance` 也没有这一条。而 `docs/07 §1 步骤 5` 要求 S31 与 S37"确定对白长度"——S31 只能提供容器时长，而这个值要么来自 S28，要么来自 S37 事后测量。

**不选边**：本文按"S28 报"实现，并把"不报时阻塞"（T31-32）作为防御。若 S28 不报，`READY` 会延迟到 S37 之后，这与"及时归档"冲突，是更差的设计。

**建议**：`artifact.ingested` 补 `{observed_duration_ms, container, codec, sample_rate_hz, channel_count}`，或补 `contracts/artifact-ingested.v2.schema.json`。这是 S30 C2、S31 C2、S32、S33、S34 共同依赖的同一条缺口——一次补齐，五个服务受益。

### C3 `pronunciation_lexicon` 没有 `content_hash`，也没有 `scope_kind`

`generation-command.v2` 的 `speech.input.pronunciation_lexicon` 是 `{id, revision}`，**没有 `content_hash`**。但 S15 的 `lexicon_revision` 是内容寻址的（`lexr_hash_uniq` on `content_hash`，[S15 §1.6](S15-voice-assets.md)），且明确"词典 revision 不可变"。

**冲突点**：S31 的输入指纹需要能检测"词典内容变了"。只带 `{id, revision}` 时，如果 S15 侧发生违规的原地改写（或跨环境 revision 编号错位），S31 会把两个不同词典当成同一份，从而**错误复用**一个用旧词典合成的 take。

**第二个缺口**：契约只有一个 `pronunciation_lexicon` 字段，没有 `scope_kind`。而 S15 有 `voice_lexicon_binding`（音色级）与 `project_lexicon`（项目级）两类。调用方无法表达"我要项目级那个"。

**不选边**：本文在 S31 自己的 API 上加 `content_hash` 与 `source_kind`/`scope_kind`（§6.1、§5.2），并在组装 `generation-command` 时**丢掉**它们（因为契约 `additionalProperties: false` 会拒）。这意味着实际发给 S26 的命令里，词典信息不足以复现。

**建议**：`speech.input.pronunciation_lexicon` 改为 `{id, revision, content_hash, scope_kind}`，或新增 `lexicon_ref` 指向 S15 的完整引用。

### C4 `speech.input` 没有音色授权字段，S26/S27 无从复核

`generation-command.v2` 的 `speech.input` 只有 `voice_binding{id, revision}`，**没有授权状态、没有 `state_seq`**。

**冲突点**：E30 要求"不用撤销声音新调用"。S31 在五个时点复查（§4.2），但那是**一个服务的自律**。S26 在准入时无法区分"一个带已撤销音色的 speech 命令"和正常命令；S27 在提交时同样无法区分。如果 S31 的复查有漏洞（或有人绕过 S31 直接调 S26），没有任何下一条防线。

**不选边**：本文在 S31 侧做完整复查并在 `GET /speech-jobs/{id}` 里暴露 `rights_rechecks`（§6.2），但不改契约、不让 S26 承担这个职责。

**建议**：`speech.input` 增加 `voice_authorization: {state, state_seq, checked_at}`，并让 S26 的 `POST /generation-jobs` 在准入时对 `AUTHORIZATION_REVOKED`/`EXPIRED` 直接 409。这样 E30 就有两道防线，且第二道不依赖 S31 的正确性。

### C5 `emotion` 是自由字符串

`speech.input.emotion` 是 `string, minLength: 1`。没有任何枚举、没有情绪词表、没有强度刻度。

**三个后果：**

1. **策略类无法归一。** S39 的 `strategy_class = f(action, normalized_params)` 对 `re_synthesize_speech` 需要归一参数（§11 U3）。"紧张克制"和"紧张的克制"会被当成两种策略，上限失效。
2. **QC 无法判定。** `AUD.TEXT.PROSODY_FLAT`（[S37 §3](S37-audio-alignment.md)）要判"情感与 ShotSpec 情绪标签严重不符"，但两边都是自由字符串，判定只能靠 LLM 主观打分，不可复现。
3. **跨语言不一致。** `zh-CN` 的"紧张克制"与 `en-US` 的"tense restraint"是不是同一个情绪？没有词表就无法回答，多语言版本的情绪会漂移。

**不选边**：S31 原样透传并进指纹。

**建议**：`docs/` 或 `contracts/` 定义一份有限情绪词表（可与 S36 的语义 QC 共用），`emotion` 改为 `{label, intensity}` 或 `enum`。

### C6 缺 `production.stage.failed` / `speech.voice-authorization-lapsed`

`event-topics.v2.json` 里 S31 只有一个 `production.speech.ready`。失败、冻结、取消、权利失效都没有事件通道。

**冲突点**：权利失效是一个**需要跨服务扩散**的事实。S41 的时间线里可能有大量用该音色合成的片段，S43 的交付包里可能有它。如果只能等某个服务主动查询才发现，坏结果会在交付前才暴露。

**不选边**：本文建议新增两个事件（§6.4），并把它们写在 `production.speech.ready` 之外，不混在 ready 里——ready 的语义是"好了"，把它变成"好了或坏了"会让所有消费者都要改。

**建议**：与 S29 C8 / S30 C8 一起，补一组生产组统一的生命周期事件：`production.stage.failed`、`production.stage.blocked`、`production.stage.canceled`。S31 另需 `speech.voice-authorization-lapsed`。

### C7 `service.yaml` 的 `depends_on` 缺 S25 / S37 / S06；S39 §5.3 缺 `re_synthesize_speech`

**依赖缺口**：S31 的 `depends_on = [S15, S26, S28]`。实际依赖还有：

- **S25**：`POST /generation-jobs` 强制要求 `route_decision_id`（同 S29 C7、S30 C4）
- **S37**：`dialogue_span_ms` 与 `AUD.*` 判定的来源（§4.5）
- **S06 / S04**：`utterance` 的文本版本与 `source_ref`
- **S39**：`RepairAction` 的来源
- **S41**：镜头被删时 take 变 `SUPERSEDED`

**策略类缺口**：[S39 §5.3](S39-repair-planning.md) 的归一化表列出了 `redo_lipsync`、`remix`、`regenerate_video`、`keyframe_edit`、`temporal_edit`、`transcode`、`reingest`、`realign_subtitles`，**没有 `re_synthesize_speech`**。而 `voice` 族的第一步就是它。

**冲突点**：没有归一参数，`strategy_class` 就无法为 `re_synthesize_speech` 计算，"同类同策略额外重复最多 1 次"（`max_identical_strategy_repeats: 1`）对它失效。

**不选边**：本文按实际需要实现依赖，保持 `service.yaml` 不变（生成物不能手改，[AGENTS.md](../AGENTS.md)）；S31 临时用 `re_synthesize_speech:<emotion>` 作策略类，并在 §11 U3 记为未决。

**建议**：S39 §5.3 补 `re_synthesize_speech` 的归一参数（建议 `emotion_label` + `text_revision`，**不含** `voice_revision`——换音色版本是换策略还是同策略需与 S15 的 revision 语义一起定）；catalog 的 `depends_on` 补齐。

### C8 `dialogue_duration_ms` 没有契约字段，S30 与 S41 各自猜

S30 §6.2 的查询响应里有 `duration_mapping.dialogue_duration_ms`，S30 §4.4 用它判断"对白主动作不被截断"。但**没有任何契约定义这个值从哪来、精度如何、是上界还是实测**。

**冲突点**：S30 §6.1 第 9 条写"若 `audio_strategy = silent_then_tts`，`dialogue_duration_ms ≤ target_duration_ms`（来自 S31，若已知）"。这个"来自 S31"是散文，没有字段、没有事件、没有 API。

**不选边**：本文在 `production.speech.ready` 里发布 `audio_duration_ms` 与 `dialogue_span_ms: null`（§4.5），并明确 `audio_duration_ms` 是**上界**。S30 若要用这个值，必须按上界语义使用。

**建议**：`production.speech.ready` 的 payload 补 schema，明确 `audio_duration_ms`（容器实测，上界）与 `dialogue_span_ms`（S37 后才能填，前期为 null + `dialogue_span_source`）。

### C9 `output_spec` 只有格式与采样率，没有通道数与响度

`speech.input.output_spec` 是 `{format, sample_rate_hz}`。没有 `channel_count`、没有位深、没有响度目标。

**冲突点**：S42 的响度门（`integrated_lufs` / `true_peak_dbtp` / `lra_lu`）在混音层统一测量与调整，所以**单个配音 take 不需要自带响度目标**——这一点实际上是对的。但通道数会影响 S42 的 stem 处理与 S32 的输入假设，而当前位置只能靠 S28 的容器探测事后知道。

**不选边**：本文暂按 mono 输出（§11 U10），由 S42 上混。这是保守选择：mono 上混 stereo 不会损失信息，反过来不行。

**建议**：`output_spec` 补 `channel_count`，与 C2 的 `artifact.ingested` 补齐一起做。

---

## 13. 与其他方案的交叉引用

| 方案 | 关系 |
|---|---|
| [S15](S15-voice-assets.md) | 音色定义、授权状态、发音词典。**授权必须强一致复查**（§4.2）；`revoked` 不可逆；绝不换相似声线 |
| [S06](plans/S06-shot-spec.md) / [S04](plans/S04-narrative.md) | 台词文本与情绪标签的权威；S31 只接受 `text_revision`，不改剧本 |
| [S25](S25-model-routing.md) | `route_decision_id` 是 `POST /generation-jobs` 的必需项；`word_timestamps` 能力必须进路由 |
| [S26](S26-generation-jobs.md) | 唯一付费入口；`attempt_no` 与节点费用上限的最后一道闸；`SUBMISSION_UNKNOWN` 的冻结语义 |
| [S28](S28-artifact-ingest.md) | 音频 bytes（`content_class='generated_audio'`）；**需要它报 `observed_duration_ms`**（C2）；tombstone 语义 |
| [S37](S37-audio-alignment.md) | 字级时间码与 `AUD.TEXT.*` / `AUD.VOICE.*` 的唯一权威；`AUD.VOICE.AUTHORIZATION` 只能转人审——S31 的 `RIGHTS_BLOCKED` 是它的上游保险 |
| [S35](S35-technical-qc.md) | `voice` 动作族：`re_synthesize_speech → regenerate_video`，授权失效时禁止自动重做 |
| [S39](S39-repair-planning.md) | 下发 `RepairAction`；跨 Run 累计与 `strategy_class`（**缺 `re_synthesize_speech`**，C7） |
| [S30](S30-video-production.md) | `silent_then_tts` 时消费本服务的时长上界（C8）；`native_locked_voice` 时**不得**调 S31 |
| [S32](S32-lipsync.md) | 消费本服务的音频 + S37 的时码；口型缺陷**不得**反推语音重做（§4.6） |
| [S38](S38-subtitles.md) | 消费 S37 的 `alignment_unit`，**不消费** S31 的时长时间猜测 |
| [S41](S41-editorial.md) | 时间线所有者；消费 `production.speech.ready`；镜头删除时 take 变 `SUPERSEDED` |
| [S42](S42-composition.md) | 混音、响度、ducking 的唯一执行者；S31 不碰 |
| [S08](S08-revision-snapshot.md) | 接受权威；检查点推进 ≠ 接受 |
| [S29](S29-image-production.md) / [S30](S30-video-production.md) | 同构风格与共享缺口（C1 OpenAPI、C2 `artifact.ingested`、C6 生产组事件） |
