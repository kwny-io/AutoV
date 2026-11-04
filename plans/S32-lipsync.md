# S32 · 口型 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。所有供应商仍为 `disabled`，`slo.verified: false`。

上游依据：[docs/07 §1 步骤 6](../docs/07-production-workflows.md)（"S32 必要时做口型，再 S37/S36 检查音画与边界"）、[docs/09 §1](../docs/09-model-execution.md)（"S29–S34 负责生产意图与阶段，不能绕过 S26 直连模型"）、[docs/09 §4](../docs/09-model-execution.md)（"角色身份、锁定音色、完整对白、权限和最低交付规格不能无声降级"）、[docs/11 §2](../docs/11-quality-repair.md)（"口型不同步 | 修正对齐、重口型"）、[S31 §4.5–§4.6](S31-speech-production.md)（音频时长与跨族边界）、[S37 §3](S37-audio-alignment.md)（`AUD.LIPSYNC.*` 与 `AUD.ALIGN.*`）、[S35 §2.2.1](S35-technical-qc.md)（`lipsync` 动作族）、[S39 §5](S39-repair-planning.md)、`contracts/generation-command.v2.schema.json`（`modality: lipsync`）、`contracts/quality/action-families.v2.json`。

链上位置：`S31 配音 → S37 字级对齐 → S32 组口型意图（视频 + 音频 + 人脸轨 + 音频窗口）→ S25 估价路由 → S26 准入 → S27 执行 → S28 归档 → S32 登记 take 并发布 ready → S37 复检音画 → S36 复检视觉 → S41 时间线 → S42 合成`。

本文重点回答三个问题：**"音频本身是错的"和"嘴没跟上"怎么在系统里被分开，而不是靠人眼看出来**（§4.3、§4.4）、**一次口型结果什么时候过期，为什么必须按"两个 hash 的乘积"而不是按时间戳判断**（§4.2）、**"不许动别人脸"这句话怎么变成一个能自动判定的输出约束**（§4.6）。

---

## 0. 一句话边界

**S32 拥有"这一段视频的这张脸，配上这一段音频，重做出了哪一个口型候选；它对哪一对（视频 hash, 音频 hash）有效；它是不是已经过期"。它不拥有视频、不拥有音频、不拥有人脸、不拥有对齐时码、不拥有人脸的身份授权、不拥有时间线、不拥有钱。**

```
S30:   video bytes 与 take（S32 只读引用）
S31:   audio bytes 与 take（S32 只读引用；绝不改、绝不重合成）
S37:   字级时间码、音素、audio offset、`AUD.*` 判定（唯一权威）
S10/S13: 角色身份与人脸资产、人脸授权（S32 只读）
S28:   bytes 与 content_hash（唯一权威）
S32:   LipsyncJob + LipsyncTake + 音频窗口 + 输出差异度量 + 失效依据
S25:   可行路由与报价
S26:   attempt / ProviderJob / SUBMISSION_UNKNOWN（唯一付费通道）
S36:   视觉 QC（身份一致性、遮挡、边缘）
S41:   时间线（S32 只提供片段，不决定位置）
S23 / S22: 钱与 permit
```

判据：任何"这个镜头做过几次口型、哪一次还有效"的问题，答案来自 S32 的 take 与 `stale_basis`。任何"嘴和声音对得上吗"的问题，答案来自 S37 的 `AUD.LIPSYNC.*`。任何"这段音频对不对"的问题，答案来自 S37 的 `AUD.TEXT.*`。任何"这张脸能不能用"的问题，答案来自 S10/S13 的人脸授权。

**必须守住的一条原则：**

> **口型只能改嘴，不能改声音；嘴对不上的时候，先问声音对不对，不先重做嘴。**

这条原则包含两个方向的约束：

- **正向**：S32 的输入音频是**只读**的。它拿到的 `audio_ref` 指向 S31 的 take 或 S28 的已有音频，`content_hash` 是锁定的。S32 没有任何通路能修改、重采样、重编码、重合成这段音频。
- **反向**：当 S37 判定音频本身的台词就不对（`AUD.TEXT.*` 有未关闭的 `voice` 缺陷），S32 **必须拒绝**做口型，并把调用方指向 `re_synthesize_speech`。给一段念错词的音频配口型，产出的是一段"嘴型精确匹配错误台词"的视频——它比原来的缺陷**更难被发现**，因为口型检查会通过。

---

## 1. 责任与明确不负责

### 1.1 本服务是唯一写者

| 事实 | 说明 |
|---|---|
| `LipsyncJob` | 一次口型生产意图：视频引用、音频引用、人脸轨引用、音频窗口、必要能力 |
| `LipsyncTake` | 一个已归档的口型候选：`artifact_ref`、输入对（两个 hash）、输出差异度量、失效依据 |
| `AudioWindow` | 音频在视频时间轴上的起止（对齐意图），是本 job 冻结的输入 |
| `FaceTrackBinding` | 提交时的人脸轨快照：`face_track_ref`、轨 id、帧范围、可见度统计 |
| `InputEligibilitySnapshot` | 提交时对"音频是否可做口型"的判断依据（引用了哪些 S37 报告、哪些缺陷 id） |
| `StaleBasis` | 使一个 take 失效的依据：哪个输入变了 |
| `LipsyncCheckpoint` | 每 `(shot_lineage_id, speaker_character_id)` 的当前基线 take 指针 |

### 1.2 本服务明确不写

| 事实 | 权威 | S32 的正确做法 |
|---|---|---|
| 视频 bytes / 片段 / 分镜语义 | **S30** | 只存 `video_ref` + hash |
| 音频 bytes / 台词 / 音色 | **S31** | 只存 `audio_ref` + hash；**只读** |
| 台词文本与对错 | **S06 / S04** + **S37** 的证据 | 只读 S37 的报告引用 |
| 字级时间码、音素、audio offset | **S37** | 只读；不重算、不用 provider 返回值冒充 |
| 人脸的身份、是否可授权使用 | **S10 / S13 / S15** | 只存 `speaker_character_id` + 人脸轨引用 |
| 人脸检测与换脸 | **S10 / S30**（生成时已定） | S32 只改嘴部区域，不换脸 |
| 音频字节、音频时长 | **S28 / S31** | 不生产音频，不测量时长作为权威 |
| bytes、hash、存储 | **S28** | 只存 `artifact_ref` |
| 时间线、EDL、剪辑位置 | **S41** | 不决定片段在哪 |
| 混音、响度、ducking | **S42** | 完全不涉及 |
| 视觉 QC 结论 | **S36** | 不自造身份评分 |
| 音画 QC 结论 | **S37** | 不自造 offset 判定 |
| 当前接受指针 / `node_epoch` / `execution_fence` | **S08** | 只发事件 |
| 金额、permit、attempt | **S23 / S22 / S26** | 只持引用 |
| 修复决策、上限计数 | **S39** | 只执行被指派的那一格 |
| 人工裁决 | **S40** | 不写 |

### 1.3 本服务不直连供应商（强制条款）

**本服务不直连供应商。** 不持有供应商凭证，不出供应商域名，不配置供应商 SDK，不接受"临时直连测试"。

S32 的每一次付费口型生成走同一链路：

```
S32 组 GenerationCommand(modality=lipsync)
  → POST /generation-jobs           (S26)
      → S23 reserve（按 quote_upper_bound_micro）
      → S22 permit（provider/channel/region 由 route_decision 定死）
      → SubmissionIntent + Outbox
      → S27 claim dispatch grant → 付费提交
  → S26 观测 → S28 归档 → production.lipsync.ready 回 S32
```

强制手段与 [S29 §1.3](S29-image-production.md) 相同，三条都是"不是约定"级的：

1. **凭证隔离**：供应商密钥只注入 `connectors/*` 的 Deployment（[docs/09 §2](../docs/09-model-execution.md)）。S32 的 ServiceAccount 不在供应商出站 `NetworkPolicy` 白名单里。
2. **dispatch grant**：S27 的 `require_dispatch_grant=true` 是编译期常量，无开关可关。
3. **命令形状**：S32 发出的唯一付费入口是 `POST /generation-jobs`，请求体是 `{command_id, route_decision_id, generation_command}`。

**S32 不参与准入 Saga 的内部步骤**，不知道 reservation id、permit id、`dispatch_id`。

口型的单价与**媒体时长**强相关（GPU 逐帧推理），所以一次"给整段视频做口型"和一次"只做 4 秒"差一个数量级。S32 必须在组命令前就确定**要处理多少帧**（§4.5 的音频窗口），否则 S25 的报价与 S23 的预留都会偏低，最终由 S23 的 `quote_upper_bound_micro` 兜底——那意味着预留永远不够，积压永远发生。

### 1.4 动作归属（对照 S35 动作族）

`lipsync` 族是 `["redo_lipsync"]`，兜底 `human_review`，且**禁止** `re_synthesize_speech`（`contracts/quality/action-families.v2.json`）。

| 动作 | 执行者 | S32 的角色 |
|---|---|---|
| `redo_lipsync` | **S32（意图）→ S26（执行）** | **意图提供者，不是执行者** |
| `re_synthesize_speech` | **S31（意图）→ S26** | **不参与，且必须主动把调用方挡回 S31**（§4.3） |
| `regenerate_video` | **S30（意图）→ S26** | 不参与 |
| `realign_subtitles` / `re_render_subtitles` | **S38** | 不参与 |
| `remix` | **S42** | 不参与 |
| `reingest` / `transcode` | **S28 / S42** | 不参与 |
| `change_spec` | **S06 / S07** | 不参与 |
| `human_review` | **S40** | 不参与 |

**S32 与 `redo_lipsync` 的准确关系：**

S32 **不是执行者**——执行者是 S26 + S27。S32 是**意图提供者**：把 S39 的 `RepairAction(redo_lipsync, ...)` 翻译成一个具体的新 `LipsyncJob`。

三道检查的分工与 S31 §1.4 同构：

- S39 在**下发动作前**检查上限（[S39 §5.7](S39-repair-planning.md)），DENY 时不产生 `RepairAction`。
- S32 收到后**不再检查上限**，只检查本地前置条件：视频是否还在、音频是否可做口型、人脸轨是否仍可解析、音频窗口是否仍成立。
- S26 在 `POST /generation-jobs` 时做最后一道。

**S32 明确不做的五件事：**

1. **不重做语音。** 这是 `lipsync` 族 `forbidden` 的第一条，也是本文最重要的一条。S32 收到"音频念错词"的信号时，唯一允许的动作是**拒绝并指向 S31**（§4.3）。
2. **不因 QC 失败而自动重做。** 收到 `quality.alignment.completed` 且 `AUD.LIPSYNC.*` 有缺陷时只记录引用；是否重做由 S39 决策。口型自动重做是最容易失控的一类：一个 30 秒的镜头做一次要几分钟 GPU，自动重试三轮就能压垮整个池。
3. **不在低可见度下"尽力而为"并声称成功。** `service.yaml` 的 `acceptance` 明确"低可见度不伪装精确通过"。人脸不可见、被遮挡、背对镜头时，S32 产出 `not_applicable` 的口型结论，**不产出一个碰巧嘴部区域很小的视频让检查误以为通过**（§4.7）。
4. **不改变非目标人脸。** `preserve_other_faces` 在契约里是 `const: true`——不可关闭。S32 必须能自证（§4.6）。
5. **不链式自我叠加。** 对 S32 自己的输出再做口型会产生"二次生成脸"，视觉上明显失真。默认拒绝，除非带显式 `repair_action_id`（§4.9）。

---

## 2. 实体与关键区分

### 2.1 四层对象

```
InputPair            (video_ref.hash, audio_ref.hash) —— 一次口型有效期的作用域
      │
      ▼
LipsyncJob           一次口型意图（输入对 + 音频窗口 + 人脸轨 + 能力要求）
      │  1:1
      ▼
LipsyncTake          一个已归档的口型候选（artifact_ref + 差异度量 + 失效依据）
      │  N:1
      ▼
LipsyncCheckpoint    每 (shot_lineage_id, speaker_character_id) 的当前基线
```

**`InputPair` 不是一张表，是一个复合键。** 它不是"某个对象"，而是"这两个 hash 的组合"——它必须出现在 `LipsyncTake` 上作为两列，而不是作为一个可写实体。原因见 §4.2：**有效期是输入的属性，不是产物的属性。** 如果把它建成一张 `input_pair` 表，就会有人给它加状态、加时间戳，然后开始"刷新"它——那一刻起，时效判断就从"hash 是否相同"变成了"谁最近更新过"，而后者永远会在某个环节出错。

### 2.2 为什么口型需要 `VideoRef` 和 `AudioRef` 两个引用而不是一个"媒体包"

有一种诱人的简化：让 S41 或 S30 出一个"待配口的镜头包"，里面装着视频、音频、人脸轨，S32 只处理一个引用。

**这条路不能走，理由是权威。** 那个"包"会成为第二个媒体权威：它复制了视频与音频的 hash，而复制的东西会过期。当 S30 因为 `temporal_edit` 产出了新片段、或 S31 因为台词改了产出新音频时，包里的旧引用不会自动更新，S32 会拿一个已经没人认的旧片段去做口型，然后产出一个谁也对不上的结果。

**所以 S32 直接持有两个独立引用，各自的 `content_hash` 是判断"有没有变"的唯一依据。** 没有中间层，就没有中间层的过期问题。

### 2.3 `AudioWindow`：音频放在视频的哪一段

这是全篇最容易漏掉的输入。

`lipsync.input` 只有 `video_ref` 与 `audio_ref`，**没有任何时间字段**。这隐含了一个假设："音频从视频的第 0 毫秒开始"。

这个假设在大多数情况下是错的。一个 4 秒的镜头，角色在第 0.8 秒才开口，音频本身可能还带 200ms 的起始静音。若 S32 按 0 起算，产出的口型会整体偏移——而偏移量恰好等于"角色开口的延迟 + 音频前导静音"。S37 会把它报成 `AUD.LIPSYNC.OFFSET`，于是 S39 下发 `redo_lipsync`，S32 再做一次，**还是按 0 起算，还是偏**。

这就是"重做口型从来不解决问题"的典型机制。所以 `AudioWindow` 必须是显式输入：

| 字段 | 含义 | 来源 |
|---|---|---|
| `start_ms` | 音频第 0 毫秒对应视频的第几毫秒 | **调用方必须给**（见下） |
| `end_ms` | 音频结束对应视频的第几毫秒 | `start_ms + audio_duration_ms - trim_tail_ms` |
| `trim_head_ms` | 音频开头要跳过多少毫秒 | S37 的 `alignment_unit` 首字开始时间 |
| `trim_tail_ms` | 音频结尾要跳过多少毫秒 | 同理 |
| `source` | 这个窗口从哪来 | `explicit` / `s37_alignment` / `editorial` |

**第一轮（还没有 S37 报告时）由谁给 `start_ms`：** 这是契约缺口（§12 C2）。三条现实中可能的来源，各有代价：

| 候选来源 | 何时可用 | 代价 |
|---|---|---|
| S41 的 EDL（clip 内的音频偏移） | 时间线已建 | 首轮口型往往在建时间线之前（§0 的链路顺序） |
| S06 的 ShotSpec 节拍（"第 0.8 秒开口"） | 总是可用 | 是创作意图，不是实测；需要 S06 真的有这个字段 |
| S37 的对齐报告 | S37 跑过之后 | 首轮没有；但它是最准的 |

**本文的处理：`AudioWindow` 在 S32 的 API 上是必填，`source` 必须标注。** S32 不自己推、不默认 0、不"先按 0 做一版看看"。缺 `start_ms` 时返回 `422 AUDIO_WINDOW_REQUIRED`，错误信息里指向 S37 的 `GET /alignment-reports/{id}/units`。

**为什么不默认 0 也能跑（哪怕会偏）：** 默认 0 会让系统进入一个稳定的坏循环——偏 → 报缺陷 → 重做 → 还是偏。而每次重做都花钱、都消耗 attempt 额度、都让人以为"口型模型不行"。一个明确的 422 比一个稳定的坏产物便宜得多。

### 2.4 音频窗口与"有效帧范围"

`AudioWindow` 决定的是**要处理哪些帧**：

```
处理帧范围 = [floor(start_ms * fps / 1000) - handle_frames,
              ceil(end_ms  * fps / 1000) + handle_frames]
```

`handle_frames` 是前后各留的余量（默认 2 帧）。**为什么要留余量：** 嘴部运动是连续的，在音频起点那一帧，嘴已经在动了；模型需要看到"开口前"的几帧才能正确推断运动方向。不留余量会在窗口边界产生突跳，而这个突跳恰好会被 S36 报成 `temporal` 类缺陷，然后被误判成"口型失败"。

**处理帧范围必须进 `input_fingerprint`。** 同一个视频、同一段音频，窗口从 `[800, 4080]` 改成 `[820, 4100]`，是两次不同的生产。

### 2.5 人脸轨：谁提供、为什么契约指向了一个不存在的东西

`lipsync.input.face_track_ref` 的形状是 `{service_id, id, revision, content_hash}`，示例指向 `service_id: "S28"`。

**问题：S28 的 `content_class` 闭集里没有人脸轨。** 闭集是 `generated_video, generated_image, generated_audio, rendered_master, proxy, thumbnail, waveform, subtitle_track, audio_stem, uploaded_reference, evidence, model_archive`。人脸轨最接近的是 `evidence`，但那是 QC 证据的类别，不是生产输入。

（§12 C1 详述。）

**S32 的现实处理：** 接受 `face_track_ref` 指向 `S28` 且 `content_class ∈ {evidence, uploaded_reference}`，但要求同时给出：

```jsonc
"face_track": {
  "ref": { "service_id": "S28", "id": "…", "revision": 1, "content_hash": "…" },
  "track_id": "face_2",
  "frame_range": { "start": 0, "end": 119 },
  "visibility": { "visible_frames": 108, "covered_frames": 11, "min_face_px": 64 },
  "detector": { "model": "…", "version": "…" }
}
```

**为什么要 `track_id` 和 `visibility`：**

- **`track_id`**：一个镜头里可能有三张脸。`face_track_ref` 只指向一个文件/对象，不带"第几条轨"，S32 就无法知道该改哪张嘴。契约里 `speaker_character_id` 也在，但它是"角色"，而角色在镜头里可能背对镜头或不在画面里——角色 id 不能替代轨 id。
- **`visibility`**：见 §4.7。没有这个统计，S32 无法区分"做完了但脸只出现了 3 帧"和"做完了而且脸全程可见"。

**这两项在 `generation-command.v2` 里没有位置**（`additionalProperties: false`）。S32 只能在自己的 API 上要求它们，在组命令时丢掉。这是一个必须记录的缺口（§12 C1），因为它意味着**S26/S27 侧无法验证 S32 到底处理了哪张脸**。

### 2.6 `preserve_other_faces: const true` 的含义

契约把这个字段设成 `const: true`，即**永远为真，无法关闭**。

这是对的，而且理由不在质量，在权利：

- 换一张脸的视觉表现需要**那个人的**授权。S15 的语音授权是音色级的、可撤销的；人脸的在 S10/S13。两者是两套授权，走两套流程。
- S32 的任务是"让这张嘴跟着这段声音动"，不是"重新生成这一帧"。后者会重绘整张脸，包括背景里第三排那个群众演员——他没有授权，也没有出现在任何授权台账里。

所以 `preserve_other_faces` 不是可配置项，是 S32 的输出**必须满足的性质**。§4.6 给出它怎么被自动验证。

---

## 3. 状态机

### 3.1 `LipsyncJob` 状态

```
CREATED → CHECKING_INPUTS → PREPARING → SUBMITTED → WAITING_PROVIDER → INGESTING → READY
                 ↘                ↘              ↘                 ↘
          INPUTS_INELIGIBLE   REJECTED_LOCAL     FAILED        WAITING_UNKNOWN
                                                                   ↓
                                                        RECONCILED_READY
                                                        RECONCILED_FAILED
任意非终态 → CANCELED
```

`CHECKING_INPUTS` 是 S32 独有的状态（S31 叫 `CHECKING_RIGHTS`）：它检查的是**输入对是否合格**，而不只是权限。

| 从 | 到 | 触发 | 前置条件 | 副作用 |
|---|---|---|---|---|
| — | `CREATED` | `POST /lipsync-jobs` | 校验通过 + 幂等未命中 | 写 job |
| `CREATED` | `CHECKING_INPUTS` | worker 领取 | — | 查 S37 的报告、解析音频窗口、解析人脸轨 |
| `CHECKING_INPUTS` | `INPUTS_INELIGIBLE` | 音频有未关闭的 `voice` 缺陷 / 音频已被 S31 标 `void` / 视频已 tombstone | — | 写 `InputEligibilitySnapshot`；**指向正确动作**（§4.3） |
| `CHECKING_INPUTS` | `REJECTED_LOCAL` | 窗口缺失/越界 / 人脸轨不可解析 / 可见度不足 / 链式自我叠加 | — | 不产生费用 |
| `CHECKING_INPUTS` | `PREPARING` | 输入合格 | — | 取/复用 `RouteDecision` |
| `PREPARING` | `REJECTED_LOCAL` | `feasible=false` / 能力不足 / 帧数超上限 | — | 不产生费用 |
| `PREPARING` | `SUBMITTED` | S26 返回 202 | `quote_valid_to > now()`；**提交前输入对复查** | 存 `provider_job_id` / `attempt_id` |
| `SUBMITTED` | `WAITING_PROVIDER` | S26 → `QUEUED`/`RUNNING` | — | — |
| `SUBMITTED` | `FAILED` | S26 → `PROVIDER_FAILED` | 无重试预算 | — |
| `SUBMITTED` | `WAITING_UNKNOWN` | S26 → `SUBMISSION_UNKNOWN` | — | 冻结（§4.8） |
| `WAITING_PROVIDER` | `INGESTING` | S26 → `PROVIDER_SUCCEEDED` | — | — |
| `WAITING_PROVIDER` | `FAILED` | S26 → `PROVIDER_FAILED` / `CANCELED` | — | — |
| `INGESTING` | `INGESTING` | `artifact.ingested` 未到 / S28 失败 | 重试预算未尽 | 只重归档 |
| `INGESTING` | `READY` | 归档完成 + hash 一致 + **非目标区域未改变** + **输入对仍成立** | 帧数与时码可比 | 写 `LipsyncTake` + CAS 检查点 |
| `INGESTING` | `FAILED` | 非目标区域被改 / hash 不符 / 帧数不符 / 时码不可读 | — | — |
| `WAITING_UNKNOWN` | `RECONCILED_READY` | S26 → `ARCHIVED` | 同 `INGESTING→READY` | 同 |
| `WAITING_UNKNOWN` | `RECONCILED_FAILED` | S26 → 确认未收单且放弃 | — | 允许 S39 决定新 attempt |
| 任意非终态 | `CANCELED` | 显式取消 / 输入失效 | — | 调 S26 cancel；不承诺退款 |

**终态**：`READY`、`INPUTS_INELIGIBLE`、`REJECTED_LOCAL`、`FAILED`、`RECONCILED_FAILED`、`CANCELED`。

### 3.2 `INPUTS_INELIGIBLE` 与 `REJECTED_LOCAL` 为什么必须分开

两者都不花钱、都不重试，看起来可以合并。但它们回答的是**不同人的问题**：

| | `INPUTS_INELIGIBLE` | `REJECTED_LOCAL` |
|---|---|---|
| 含义 | 输入本身不该被做口型 | 请求参数或能力不允许 |
| 谁来修 | 上游（S31 重合成 / S30 重生成 / S10 补人脸轨） | 调用方（改窗口、改帧数、去掉链式叠加） |
| 谁该被通知 | **原缺陷的持有人** | 调用方 |
| 在 S39 看来 | 这是一次"族内无路可走"，应转 `human_review` 或跨族 | 这不是一次尝试，是参数错误 |
| 响应码 | `409 AUDIO_NOT_ELIGIBLE_FOR_LIPSYNC` | `422 AUDIO_WINDOW_REQUIRED` 等 |

合并成 `REJECTED_LOCAL` 会让"音频念错词"变成一个参数错误，于是它会被归因给 S32、被记进 S32 的错误率、被 S32 的 oncall 处理——而真正该处理它的是一条 `voice` 缺陷和一次 `re_synthesize_speech`。

`INPUTS_INELIGIBLE` **是终态，且没有自动出口**。它只能通过一次新的请求（带着合格的新音频）离开。

---

## 4. 关键流程

### 4.1 正常路径

```
1. POST /lipsync-jobs
     {command_id, request_hash, context,
      video_ref, audio_ref,
      speaker_character_id,
      face_track: {ref, track_id, frame_range, visibility, detector},
      audio_window: {start_ms, trim_head_ms, trim_tail_ms, source},
      preserve_other_faces: true,       // 契约 const，只能为 true
      repair_action_id | null,
      route_policy_ref | route_decision_id,
      budget_scope_id, deadline}

2. 校验（顺序固定）：
   a. context 必填项 + 平台签名绑定的 tenant
   b. 幂等键
   c. video_ref / audio_ref / face_track_ref 均属本租户（跨租户 404）
   d. audio_window 存在且 source 合法；start_ms >= 0；end_ms > start_ms
   e. end_ms - start_ms 与 audio_ref 的时长相容（±1 帧）
   f. video_ref 的 origin_service 不是 S32，或带了 repair_action_id
   g. face_track.track_id 非空；visibility 统计存在
   h. deadline > now()

3. CHECKING_INPUTS：
   - 查 S37 是否对该 (audio_ref, utterance) 有未关闭的 voice 类缺陷
       → 有 → INPUTS_INELIGIBLE(AUDIO_TEXT_DEFECT_OPEN)，指向 re_synthesize_speech
   - 查 S31：audio_ref 对应的 take 是否 rights_state='usable'
       → 否 → INPUTS_INELIGIBLE(AUDIO_RIGHTS_VOID)
   - 查 S28：video_ref 是否 REFERENCEABLE
       → 否 → INPUTS_INELIGIBLE(VIDEO_TOMBSTONED)
   - 查 S10/S13：speaker_character_id 的人脸授权是否仍有效
       → 否 → INPUTS_INELIGIBLE(FACE_AUTHORIZATION_LAPSED)
   - 计算处理帧范围（§2.4）
       → 帧数 > max_frames_per_job → REJECTED_LOCAL(FRAME_BUDGET_EXCEEDED)
   - 写 InputEligibilitySnapshot + FaceTrackBinding

4. PREPARING：取 RouteDecision；检查路由是否支持
   preserve_other_faces=true 的约束（有些渠道不支持局部重绘）

5. SUBMITTED：提交前复查输入对（视频/音频 hash 未变、授权未失效）

6. 等 S26 / S28；INGESTING→READY 时：
   - 归档后复查输入对
   - 计算非目标区域差异（§4.6）
   - 校验输出帧数与时间码与输入一致
   - 写 LipsyncTake
   - CAS 推进 LipsyncCheckpoint
   - outbox production.lipsync.ready
```

### 4.2 有效期：按 hash 对，不按时间

**核心不变量：**

> 一个 `LipsyncTake` 对 `(video_hash, audio_hash, window)` 三元组有效。三者任一变化，take 即过期。

这三项都进 `input_fingerprint`，`input_fingerprint` 是 take 的不可变列。**没有"刷新时间戳"这种操作**，因为不存在可以刷新的东西——有效性是算出来的。

**三个必须由 hash 判断而不是由事件判断的场景：**

| 场景 | 事件可能到、可能不到 | hash 一定变 |
|---|---|---|
| S30 因 `temporal_edit` 改了片段时长 | 事件可能延迟或丢失 | 是 |
| S30 因 `regenerate_video` 出了新片段 | 是（`production.video.ready`） | 是 |
| S31 因台词改动重合成 | 是 | 是 |
| S31 因音色被撤销把 take 标 void（bytes 未变） | 是 | **否** |

最后一行是关键：**bytes 没变，但 take 不可用了。** 所以有效性判断必须是**两路合取**：

```
take.valid  ⟺  take.input_fingerprint == 当前输入的 fingerprint
              ∧ take.audio_rights_state == 'usable'      (查询时实时读 S31)
              ∧ video/audio/face_track 在 S28 仍 REFERENCEABLE
```

**为什么不能把 `audio_rights_state` 拷进 take 就完事：** 撤销发生在 take 归档之后，拷贝的值永远是 `usable`。S32 必须在**每次读取**（查询、复检、S41 拉取）时向 S31 查一次，或者依赖 S31 的 `speech.voice-authorization-lapsed` 事件把自己这边的引用标脏——两者都要有，事件用于及时，查询用于兜底。

**"过期"不是删除。** 过期的 take 仍然存在、仍然可查、仍然可解释（"2026-10-30 那次口型用的音频后来被撤销了"）。被删除的历史无法审计（E39）。

**为什么不做时间戳过期：** 有人会提议"口型结果 24 小时后失效，强制重做"。这有三个后果：重做整集要 24 小时内完成、缓存毫无意义、而且失效的理由与真实变化无关——它只是定时炸弹。有效期必须由"输入变了"驱动。

### 4.3 跨族边界：先问声音对不对

这是 S32 最重要的一段。

`lipsync` 族在 `action-families.v2.json` 里的 `forbidden`：

> `re_synthesize_speech` / when: `always` / why: "if the audio itself is wrong it is a `voice` defect and must be reported as one; inferring TTS redo from a lipsync defect is a cross-family reach"

注意这条禁令的方向：它禁止**从口型缺陷反推语音重做**。S32 侧还有一条**反方向**的义务，契约里没写但必须做：

> **当证据显示音频文本错了，S32 不得做口型。** 否则产出的是"嘴精确匹配错误台词"的视频。

两头都守住，这条边界才闭合：

```
      音频念错词（AUD.TEXT.*）
              │
              ├─ 正确动作：re_synthesize_speech（voice 族）→ 由 S31 执行
              │
              └─ 若此时做了 redo_lipsync：
                   产出 = 嘴形精确匹配错误台词
                   后果 = S37 的 AUD.LIPSYNC.* 全部通过
                          S36 的视觉全部通过
                          → 缺陷被"修复"成不可见的形态
```

**这就是为什么句型和口型必须由不同证据判定。** [S37 §3](S37-audio-alignment.md) 把检查分成三组不同的问题：

| 问题 | 检查 | 缺陷族 | 正确动作 |
|---|---|---|---|
| 文本念对了吗 | `AUD.TEXT.MISSING_WORD` / `EXTRA_WORD` / `MISPRONOUNCE` / `SUBSTITUTION` | `voice` | `re_synthesize_speech` |
| 每句在什么时间 | `AUD.ALIGN.*` | `temporal` / `technical` | 视情况 |
| 音画对得上吗 | `AUD.LIPSYNC.OFFSET` / `OPENNESS_MISMATCH` / `MOUTH_MOVES_SILENT` / `SPEAKS_CLOSED` | `lipsync` | `redo_lipsync` |

**S32 的执行规则：**

| 条件 | S32 的动作 |
|---|---|
| S37 对该音频有**未关闭的** `voice` 类缺陷（`AUD.TEXT.*` 中 severity ∈ {major, blocker}） | `409 AUDIO_NOT_ELIGIBLE_FOR_LIPSYNC`，`suggested_action=re_synthesize_speech`，带 `defect_ids` |
| 无 `voice` 缺陷，或有但已被关闭（修复后重检通过） | 允许 |
| S37 尚未跑过（首轮，无报告） | **允许**，但 `input_eligibility.voice_defect_check='not_yet_available'`，并在 ready 事件里显式标注（§4.4） |
| 有 `AUD.TEXT.SCRIPT_CONFLICT`（S37 明确"不产生缺陷"的那一类） | 允许。文本冲突不是念错，是剧本本身有歧义 |

**"尚未跑过"为什么允许、为什么要标注：** 链路顺序是 `S31 → S37 → S32`（§0），但实际上口型可以在 S37 之前跑——只要台词文本已在 S06 锁定、音频已归档。堵死这条路会让首轮生产必须等 QC，而 QC 的价值在于事后判定。所以首轮放行，但**把"我没检查过"这件事写进产物**，让 S36/S37 与 S40 都知道这一版的音频未经文本比对。

这是"允许，但留下证据"，不是"允许，然后假装没这回事"。

### 4.4 首轮放行的代价与补偿

首轮放行（§4.3 第三行）意味着一个真实风险：音频念错词、口型做完、S37 才发现。此时已花的钱收不回，take 也白做。

补偿手段不是"再查一次"，而是**让后续判断能区分**：

```jsonc
// LipsyncTake 上
"input_eligibility": {
  "voice_defect_check": "not_yet_available",     // 或 "clean" / "defects_open"
  "checked_at": "2026-11-02T10:03:00Z",
  "s37_report_ref": null
}
```

下游据此可以：S39 在规划修复时优先考虑"这一版的口型建立在未经检验的音频上"；S40 在人工验收时知道该先听音频；S36 可以在视觉检查里额外注意嘴形与语义是否矛盾。

**不做的事：S32 不因为"可能有问题"而拒绝。** 拒绝会把首轮生产卡死在一张不存在的报告上，而首轮是绝大多数。

### 4.5 帧数与时长

口型的成本 ≈ `处理帧数 × 每帧单价`。三个数字必须分清：

| 数字 | 定义 | 谁给 |
|---|---|---|
| `audio_duration_ms` | 音频容器实测总时长 | S31 / S28（[S31 §4.5](S31-speech-production.md)） |
| `window_duration_ms` | `end_ms - start_ms - trim_head_ms - trim_tail_ms` | S32 算 |
| `processed_frames` | §2.4 的处理帧范围的长度 | S32 算 |

**报价用的是 `processed_frames`。** 若 S25 按 `audio_duration_ms` 报价而 S32 实际处理的是视频里的一段帧，两者可能差很多——尤其是"整段视频 30 秒、音频只有 2 秒"的镜头（角色说一句话，其余是沉默动作）。按音频报价会让预留严重偏低。

所以 `LipsyncJob.command_summary` 里必须带 `processed_frames` 与 `duration_ms`，让 S25 的能力查询能拿到。若 `contracts` 无法承载（`additionalProperties: false`），S32 至少在本地记账并在 §12 C3 记录。

**帧数上限：** `max_frames_per_job`（默认 900，约 30 秒 @30fps）。超限 → `REJECTED_LOCAL(FRAME_BUDGET_EXCEEDED)`，并建议调用方拆分。**S32 不自动拆。** 自动拆会产生多个 job、多个 take、多个 checkpoint 竞争，而且拆分点的嘴部连贯性没法保证。

### 4.6 "不许动别人的脸"怎么被自动验证

`preserve_other_faces: const true` 是一个必须满足的输出性质。验证不能靠人看，必须是一个**确定性计算**。

**方法：**

```
1. 用 face_track.track_id 的目标人脸框 + 膨胀半径 r（默认 32px）
   构造目标掩膜 M_target（逐帧）
2. 补集掩膜 M_other = 全画面 \ M_target
3. 对每一帧 i：
     diff_i = max over p in M_other of |input[p] - output[p]|
4. 结果：
     other_region_max_diff = max_i diff_i
     other_region_changed_frame_ratio = count(diff_i > eps) / frames
```

**判定：**

| 条件 | 结论 |
|---|---|
| `other_region_max_diff <= eps_pixel`（默认 2/255） | 通过 |
| `eps_pixel < max_diff <= warn_pixel`（默认 8/255） | 通过但记 `other_face_drift='minor'`，供 S36 参考 |
| `max_diff > warn_pixel` | **`FAILED(OTHER_FACE_ALTERED)`**，不登记 take |

**为什么用逐像素最大差而不是 PSNR/SSIM：** 全局指标会被"大片未变区域"稀释。一张脸被重绘了，但在 1920×1080 里只占 3%，SSIM 可能还有 0.998——看起来完美。逐像素最大差在补集上取，等于"任何一个人脸之外的像素变了多少"，无法被稀释。

**"膨胀半径 r"为什么必要：** 模型在改嘴时不可避免地会碰到嘴边的一圈像素（下颌线、法令纹、下巴边缘）。不膨胀就会被自己的合理输出判为越界。r=32px 是在 1080p 下人脸宽度（约 120–200px）的一个保守值，应由实际的嘴部区域统计确定，不是拍脑袋。

**这不是 QC，是约束校验。** 它不需要模型、不需要评分、不需要置信度，是一个纯计算。所以 S32 在 `INGESTING` 阶段就做，**不登记一个违反硬约束的 take**，而不是等 S36 事后发现。

代价是：这个检查会拒绝掉一些"其实人眼看不出来"的越界。这是刻意的——`preserve_other_faces` 是权利问题，不是质量问题，权利不容忍"差别很小"。

**S36 的职责不因此消失：** S36 还要验身份一致性（这张脸还是同一个人吗）、遮挡处理、边缘融合。S32 只验"有没有越界"，不验"越界之外的部分好不好"。

### 4.7 可见度：不许把不可见当成通过

`service.yaml` 的 `acceptance`：**"低可见度不伪装精确通过"**。

`AUD.LIPSYNC.FACE_NOT_FOUND` 在 [S37 §3](S37-audio-alignment.md) 里是 `not_applicable` + `reason=no_face`。S32 侧必须有同样的语义，而且要更早。

**S32 的可见度分级（按目标人脸在窗口内的可见帧比例）：**

| `visible_frames / window_frames` | 分级 | S32 的动作 |
|---|---|---|
| ≥ 0.9 | `full` | 正常处理 |
| 0.5 – 0.9 | `partial` | 正常处理，但 take 标 `visibility_grade='partial'` |
| 0.1 – 0.5 | `sparse` | **`REJECTED_LOCAL(LOW_FACE_VISIBILITY)`**，不花钱 |
| < 0.1 | `absent` | **`REJECTED_LOCAL(NO_FACE_IN_WINDOW)`**，不花钱 |
| 任意，但 `min_face_px < 48` | `too_small` | 同上拒绝 |

**为什么 `sparse` 就拒绝而不是"做一下试试"：**

在只有 15% 帧可见的情况下，模型有 85% 的帧没有依据。它会做两件事之一：把嘴部区域按相邻可见帧外推（产出一个在不可见区域里"凭空长出的嘴"），或保持原样（产出大部分未变、少数几帧变了的视频）。前者在视觉上是明显的跳变，后者是"看起来做了但其实没做"。

两种都不是"精确通过"，而如果 S32 放行，S37 在可见帧上检查会通过，S36 在整体上看不出问题——**缺陷就藏在一个统计意义上"通过"的结果里**。

`service.yaml` 的恢复策略"多人/遮挡不支持则转策略决策"要求的正是这个：**不支持就明说，不勉强**。

**`partial` 为什么放行：** 0.5–0.9 是常见的（角色偶尔背头、被前景遮挡）。全部拒绝会让大量正常镜头卡住。但必须打标，因为这一版的可靠性低于 `full`，S39 在规划时应该知道。

### 4.8 `SUBMISSION_UNKNOWN` 的冻结

与 [S26 §13](../plans/S26-generation-jobs.md) / [S30 §4.7](S30-video-production.md) 同语义：`WAITING_UNKNOWN` 期间**不得**发出新的付费生成；`GET /generation-jobs/{id}` 的 `must_reconcile_before_resubmit` 为真时，S32 的 `POST /lipsync-jobs` 若命中同一 `input_fingerprint` 也必须拒绝。

**S32 特有的一条：** UNKNOWN 期间视频与音频可能都变了（上游继续推进）。此时对账回来的结果对应的是旧输入对，**必须标为过期 take，不得推进检查点**。对账成功 ≠ 结果可用。

### 4.9 链式自我叠加

对 S32 自己的输出再做口型（`video_ref.origin_service == 'S32'`）会产生两代生成伪影：第一次生成已经重绘了嘴部，第二次在重绘过的像素上再重绘，皮肤纹理、牙齿细节会明显劣化（俗称"糊"）。

**规则：**

| `video_ref.origin_service` | `repair_action_id` | S32 动作 |
|---|---|---|
| `S30` | 任意 | 允许 |
| `S32` | **有** | 允许（这是 S39 显式决定的修复） |
| `S32` | **无** | `422 SELF_CHAIN_FORBIDDEN` |
| 其他 | 任意 | `422 UNEXPECTED_VIDEO_ORIGIN`，并报告（说明有人绕过了 S30） |

**`origin_service` 从哪来：** S28 在归档时记录 `ingesting_service`（谁提交的归档）。若 S28 不记录，S32 无法判断——这是 §12 C4 的缺口。

### 4.10 失败矩阵

| # | 场景 | 检测点 | S32 动作 | 状态 | 花钱 | 计数 |
|---|---|---|---|---|---|---|
| F1 | 音频有未关闭的 `voice` 缺陷 | `CHECKING_INPUTS` | `409 AUDIO_NOT_ELIGIBLE_FOR_LIPSYNC`，指向 S31 | `INPUTS_INELIGIBLE` | 否 | 否 |
| F2 | 音频对应 take `rights_state='void'` | `CHECKING_INPUTS` | `INPUTS_INELIGIBLE(AUDIO_RIGHTS_VOID)` | `INPUTS_INELIGIBLE` | 否 | 否 |
| F3 | 视频已 tombstone | `CHECKING_INPUTS` | `INPUTS_INELIGIBLE(VIDEO_TOMBSTONED)` | `INPUTS_INELIGIBLE` | 否 | 否 |
| F4 | 人脸授权失效 | `CHECKING_INPUTS` | `INPUTS_INELIGIBLE(FACE_AUTHORIZATION_LAPSED)` | `INPUTS_INELIGIBLE` | 否 | 否 |
| F5 | 缺 `audio_window` | 同步校验 | `422 AUDIO_WINDOW_REQUIRED` | — | 否 | 否 |
| F6 | `end_ms` 与音频时长不符 | 同步校验 | `422 AUDIO_WINDOW_MISMATCH` | — | 否 | 否 |
| F7 | 窗口越出视频边界 | 同步校验 | `422 AUDIO_WINDOW_OUT_OF_RANGE` | — | 否 | 否 |
| F8 | 帧数超上限 | `CHECKING_INPUTS` | `REJECTED_LOCAL(FRAME_BUDGET_EXCEEDED)`，建议拆分 | `REJECTED_LOCAL` | 否 | 否 |
| F9 | 人脸缺席 | `CHECKING_INPUTS` | `REJECTED_LOCAL(NO_FACE_IN_WINDOW)` | `REJECTED_LOCAL` | 否 | 否 |
| F10 | 可见帧 < 50% | `CHECKING_INPUTS` | `REJECTED_LOCAL(LOW_FACE_VISIBILITY)` | `REJECTED_LOCAL` | 否 | 否 |
| F11 | 人脸过小 | `CHECKING_INPUTS` | `REJECTED_LOCAL(FACE_TOO_SMALL)` | `REJECTED_LOCAL` | 否 | 否 |
| F12 | 人脸轨不可解析 | `CHECKING_INPUTS` | `422 FACE_TRACK_UNREADABLE` | `REJECTED_LOCAL` | 否 | 否 |
| F13 | 链式自我叠加且无 `repair_action_id` | 同步校验 | `422 SELF_CHAIN_FORBIDDEN` | — | 否 | 否 |
| F14 | `video_ref` 来源非 S30/S32 | 同步校验 | `422 UNEXPECTED_VIDEO_ORIGIN` | — | 否 | 否 |
| F15 | 路由不支持局部重绘 | `PREPARING` | `REJECTED_LOCAL(CAPABILITY_MISSING)`，**不降级为整帧重绘** | `REJECTED_LOCAL` | 否 | 否 |
| F16 | 路由 `feasible=false` | `PREPARING` | 透传 `degradation_options` | `REJECTED_LOCAL` | 否 | 否 |
| F17 | 非目标区域被改 | `INGESTING` | `FAILED(OTHER_FACE_ALTERED)`，**不登记 take** | `FAILED` | 是 | 是 |
| F18 | 输出帧数与输入不符 | `INGESTING` | `FAILED(FRAME_COUNT_MISMATCH)` | `FAILED` | 是 | 是 |
| F19 | 输出时间码与输入不符 | `INGESTING` | `FAILED(TIMECODE_MISMATCH)` | `FAILED` | 是 | 是 |
| F20 | 归档时输入对已变 | `INGESTING` | take 标 `stale_basis`，**不推进检查点**，不发 `ready` | `READY`（take 过期） | 是 | 是 |
| F21 | S28 归档不带帧数/时码 | 事件 | 阻塞 `INGESTING` + 告警 | `INGESTING` | 是 | 是 |
| F22 | 归档 URL 过期 | 事件 | 向 S26 要新 URL，**不重新生成** | `INGESTING` | 否 | 否 |
| F23 | 归档 hash 不符 | 事件 | `FAILED(HASH_MISMATCH)` | `FAILED` | 是 | 是 |
| F24 | S26 `SUBMISSION_UNKNOWN` | 事件 | 冻结，不发新请求 | `WAITING_UNKNOWN` | 保留额度 | 是 |
| F25 | 供应商政策拒绝 | S26 事件 | `FAILED(POLICY_REJECTED)`，**不换渠道** | `FAILED` | 是 | 是 |
| F26 | 账户撤销 | S26 事件 | `FAILED(ACCOUNT_UNAUTHORIZED)`，**不换渠道** | `FAILED` | 是 | 是 |
| F27 | 预算不足 | S26 事件 | `FAILED(BUDGET)`，不降帧率不缩窗口 | `FAILED` | 否 | 是 |
| F28 | 收到 `voice` 类的 `redo_lipsync` | 同步校验 | `422 ACTION_TYPE_MISMATCH` | — | 否 | 否 |
| F29 | 同 `command_id` 不同 body | 幂等 | `409 IDEMPOTENCY_CONFLICT` | — | 否 | 否 |
| F30 | 死线已过 | 同步校验 | `422 DEADLINE_PASSED` | — | 否 | 否 |
| F31 | 数据驻留禁止 | `PREPARING` | `REJECTED_LOCAL(RESIDENCY_FORBIDDEN)` | `REJECTED_LOCAL` | 否 | 否 |

---

## 5. 数据模型

### 5.1 表清单

| 表 | 作用 | 可变性 |
|---|---|---|
| `lipsync_job` | 口型意图 | 状态可变；输入对/窗口/帧范围不可变 |
| `face_track_binding` | 人脸轨快照 | 不可变 |
| `input_eligibility_snapshot` | 提交时输入合格性依据 | 不可变 |
| `lipsync_take` | 口型候选 | `stale_basis` / `state` 可变，输入对不可变 |
| `lipsync_checkpoint` | 生产基线指针 | 可变（CAS） |
| `output_diff_measurement` | 非目标区域差异度量 | 不可变 |
| `lipsync_intent_ledger` | 生产意图台账 | 只追加 |
| `job_transition` | 状态转移历史 | 只追加 |
| `command_receipt` | 命令幂等回执 | 只追加 |
| `outbox` | 事件出站 | 只追加 |
| `inbox` | 事件入站去重 | 只追加 |

### 5.2 核心 DDL

```sql
CREATE SCHEMA IF NOT EXISTS lipsync;

-- ---------- 人脸轨快照（不可变） ----------
CREATE TABLE lipsync.face_track_binding (
  face_track_binding_id text PRIMARY KEY,
  tenant_id             text NOT NULL,
  face_track_ref        jsonb NOT NULL,     -- {service_id, id, revision, content_hash}
  track_id              text NOT NULL,      -- 同一对象里的第几条轨
  frame_range_start     integer NOT NULL CHECK (frame_range_start >= 0),
  frame_range_end       integer NOT NULL,
  visible_frames        integer NOT NULL CHECK (visible_frames >= 0),
  covered_frames        integer NOT NULL CHECK (covered_frames >= 0),
  min_face_px           integer NOT NULL CHECK (min_face_px >= 0),
  detector_model        text NOT NULL,
  detector_version      text NOT NULL,
  captured_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ftb_range_order CHECK (frame_range_end > frame_range_start),
  CONSTRAINT ftb_frames_fit CHECK (
    visible_frames + covered_frames <= frame_range_end - frame_range_start + 1),
  CONSTRAINT ftb_ref_known CHECK ((face_track_ref->>'service_id') IN ('S28')),
  CONSTRAINT ftb_hash_len CHECK (length(face_track_ref->>'content_hash') = 64)
);

-- ---------- 输入合格性快照（不可变） ----------
CREATE TABLE lipsync.input_eligibility_snapshot (
  snapshot_id        text PRIMARY KEY,
  tenant_id          text NOT NULL,
  video_ref          jsonb NOT NULL,
  audio_ref          jsonb NOT NULL,
  audio_take_ref     jsonb,                 -- 指向 S31 的 voice_take（可空：非 S31 来源）
  voice_defect_check text NOT NULL
    CHECK (voice_defect_check IN ('clean','defects_open','not_yet_available')),
  s37_report_ref     jsonb,
  open_defect_ids    text[] NOT NULL DEFAULT '{}',
  audio_rights_state text NOT NULL
    CHECK (audio_rights_state IN ('usable','void','unknown')),
  video_referenceable boolean NOT NULL,
  face_authorization_state text NOT NULL
    CHECK (face_authorization_state IN ('granted','lapsed','unknown')),
  captured_at        timestamptz NOT NULL DEFAULT now(),
  -- defects_open 必须有具体缺陷 id：不允许"我觉得有问题"而无证据
  CONSTRAINT ies_defects_have_ids CHECK (
    voice_defect_check <> 'defects_open' OR cardinality(open_defect_ids) > 0)
);

-- ---------- 口型意图 ----------
CREATE TABLE lipsync.lipsync_job (
  lipsync_job_id     text PRIMARY KEY,
  tenant_id          text NOT NULL,
  project_id         text NOT NULL,
  series_id          text NOT NULL,
  episode_id         text NOT NULL,
  branch_id          text NOT NULL,

  shot_lineage_id    text NOT NULL,
  speaker_character_id text NOT NULL,

  -- ↓ 输入对：不可变（§4.2）
  video_ref          jsonb NOT NULL,
  audio_ref          jsonb NOT NULL,
  audio_take_id      text,                  -- S31 的 voice_take_id（若来源是 S31）

  -- ↓ 音频窗口：不可变（§2.3）
  window_start_ms    integer NOT NULL CHECK (window_start_ms >= 0),
  window_end_ms      integer NOT NULL,
  trim_head_ms       integer NOT NULL DEFAULT 0 CHECK (trim_head_ms >= 0),
  trim_tail_ms       integer NOT NULL DEFAULT 0 CHECK (trim_tail_ms >= 0),
  window_source      text NOT NULL
    CHECK (window_source IN ('explicit','s37_alignment','editorial')),

  -- ↓ 帧范围：不可变（§2.4）
  process_frame_start integer NOT NULL CHECK (process_frame_start >= 0),
  process_frame_end   integer NOT NULL,
  processed_frames    integer NOT NULL CHECK (processed_frames > 0),

  face_track_binding_id text NOT NULL REFERENCES lipsync.face_track_binding,
  eligibility_snapshot_id text NOT NULL REFERENCES lipsync.input_eligibility_snapshot,

  preserve_other_faces boolean NOT NULL DEFAULT true,
  fps                  numeric(8,3) NOT NULL CHECK (fps > 0),

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
  quote_upper_bound_micro bigint,

  state                text NOT NULL,
  state_seq            bigint NOT NULL DEFAULT 0,
  failure_code         text,
  failure_detail       jsonb,
  reconciled           boolean NOT NULL DEFAULT false,

  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  state_entered_at     timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT lj_state_check CHECK (state IN (
    'CREATED','CHECKING_INPUTS','INPUTS_INELIGIBLE','PREPARING','REJECTED_LOCAL',
    'SUBMITTED','WAITING_PROVIDER','WAITING_UNKNOWN','INGESTING','READY',
    'FAILED','RECONCILED_READY','RECONCILED_FAILED','CANCELED')),
  CONSTRAINT lj_tenant_idem UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT lj_window_order CHECK (window_end_ms > window_start_ms),
  CONSTRAINT lj_frame_order CHECK (process_frame_end > process_frame_start),
  CONSTRAINT lj_fingerprint_len CHECK (length(input_fingerprint) = 64),
  -- 契约 const：不允许关闭
  CONSTRAINT lj_preserve_faces CHECK (preserve_other_faces = true),
  CONSTRAINT lj_video_is_s28 CHECK ((video_ref->>'service_id') = 'S28'),
  CONSTRAINT lj_audio_is_s28 CHECK ((audio_ref->>'service_id') = 'S28')
);

CREATE UNIQUE INDEX lipsync_job_open_uniq
  ON lipsync.lipsync_job
     (tenant_id, shot_lineage_id, speaker_character_id, input_fingerprint)
  WHERE state NOT IN ('READY','INPUTS_INELIGIBLE','REJECTED_LOCAL',
                      'FAILED','RECONCILED_FAILED','CANCELED');

CREATE INDEX lipsync_job_shot_idx
  ON lipsync.lipsync_job (tenant_id, shot_lineage_id, created_at DESC);
CREATE INDEX lipsync_job_state_idx
  ON lipsync.lipsync_job (state, state_entered_at)
  WHERE state IN ('CHECKING_INPUTS','PREPARING','SUBMITTED',
                  'WAITING_UNKNOWN','INGESTING');

ALTER TABLE lipsync.lipsync_job
  ADD CONSTRAINT lj_repair_uniq UNIQUE (tenant_id, repair_action_id);

-- ---------- 口型候选 ----------
CREATE TABLE lipsync.lipsync_take (
  lipsync_take_id     text PRIMARY KEY,
  tenant_id           text NOT NULL,
  lipsync_job_id      text NOT NULL REFERENCES lipsync.lipsync_job,
  shot_lineage_id     text NOT NULL,
  speaker_character_id text NOT NULL,

  artifact_ref        jsonb NOT NULL,       -- S28，content_class='generated_video'
  video_hash          char(64) NOT NULL,    -- 输入视频 hash（有效期之一）
  audio_hash          char(64) NOT NULL,    -- 输入音频 hash（有效期之一）
  input_fingerprint   char(64) NOT NULL,    -- 含 window 与帧范围（有效期之三）

  output_frame_start  integer NOT NULL,
  output_frame_end    integer NOT NULL,
  output_frame_count  integer NOT NULL CHECK (output_frame_count > 0),
  output_fps          numeric(8,3) NOT NULL,

  visibility_grade    text NOT NULL
    CHECK (visibility_grade IN ('full','partial')),
  other_region_max_diff    numeric(6,3) NOT NULL CHECK (other_region_max_diff >= 0),
  other_face_drift         text NOT NULL
    CHECK (other_face_drift IN ('none','minor')),

  voice_defect_check_at_build text NOT NULL
    CHECK (voice_defect_check_at_build IN ('clean','not_yet_available')),
  s37_report_ref      jsonb,

  provider_job_id     text NOT NULL,
  attempt_id          text NOT NULL,
  repair_action_id    text,
  strategy_class      text,

  stale_basis         text
    CHECK (stale_basis IS NULL OR stale_basis IN
      ('video_changed','audio_changed','window_changed',
       'audio_rights_void','video_tombstoned','face_track_changed')),
  stale_detected_at   timestamptz,

  state               text NOT NULL CHECK (state IN ('READY','ACCEPTED','SUPERSEDED')),
  created_at          timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT lt_artifact_is_s28 CHECK ((artifact_ref->>'service_id') = 'S28'),
  CONSTRAINT lt_artifact_hash_len CHECK (length(artifact_ref->>'content_hash') = 64),
  CONSTRAINT lt_frames_fit CHECK (output_frame_end >= output_frame_start),
  CONSTRAINT lt_stale_has_time CHECK (
    stale_basis IS NULL OR stale_detected_at IS NOT NULL)
);

CREATE UNIQUE INDEX lt_artifact_uniq
  ON lipsync.lipsync_take
     (tenant_id, (artifact_ref->>'id'), (artifact_ref->>'content_hash'));
CREATE INDEX lt_shot_idx
  ON lipsync.lipsync_take
     (tenant_id, shot_lineage_id, speaker_character_id, created_at DESC);
CREATE INDEX lt_live_idx
  ON lipsync.lipsync_take (tenant_id, input_fingerprint)
  WHERE stale_basis IS NULL AND state IN ('READY','ACCEPTED');

-- ---------- 输出差异度量（不可变） ----------
CREATE TABLE lipsync.output_diff_measurement (
  measurement_id     uuid PRIMARY KEY,
  tenant_id          text NOT NULL,
  lipsync_take_id    text NOT NULL REFERENCES lipsync.lipsync_take,
  eps_pixel          numeric(6,3) NOT NULL,
  warn_pixel         numeric(6,3) NOT NULL,
  mask_dilate_px     integer NOT NULL CHECK (mask_dilate_px >= 0),
  max_diff_complement numeric(6,3) NOT NULL,
  max_diff_target_region numeric(6,3) NOT NULL,
  changed_frame_ratio_complement numeric(5,4) NOT NULL,
  frames_compared    integer NOT NULL CHECK (frames_compared > 0),
  measured_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT odm_threshold_order CHECK (warn_pixel >= eps_pixel)
);

-- ---------- 口型检查点 ----------
CREATE TABLE lipsync.lipsync_checkpoint (
  tenant_id            text NOT NULL,
  shot_lineage_id      text NOT NULL,
  speaker_character_id text NOT NULL,
  lipsync_take_id      text NOT NULL REFERENCES lipsync.lipsync_take,
  checkpoint_seq       bigint NOT NULL,
  expected_revision    integer NOT NULL DEFAULT 0,
  updated_by_job       text NOT NULL,
  updated_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, shot_lineage_id, speaker_character_id)
);

-- ---------- 生产意图台账 ----------
CREATE TABLE lipsync.lipsync_intent_ledger (
  entry_id             uuid PRIMARY KEY,
  tenant_id            text NOT NULL,
  lipsync_job_id       text NOT NULL,
  shot_lineage_id      text NOT NULL,
  speaker_character_id text NOT NULL,
  action               text NOT NULL
    CHECK (action IN ('generate','redo_lipsync')),
  strategy_class       text,
  repair_action_id     text,
  audio_take_id        text,
  run_id               text NOT NULL,
  node_epoch           integer NOT NULL,
  manifest_id          text NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX lil_shot_idx
  ON lipsync.lipsync_intent_ledger (tenant_id, shot_lineage_id, created_at DESC);

-- ---------- 状态转移历史 ----------
CREATE TABLE lipsync.job_transition (
  lipsync_job_id text NOT NULL,
  seq            bigint NOT NULL,
  from_state     text,
  to_state       text NOT NULL,
  reason_code    text,
  detail         jsonb,
  at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (lipsync_job_id, seq)
);

-- ---------- 基础设施 ----------
CREATE TABLE lipsync.command_receipt (
  tenant_id     text NOT NULL,
  command_id    text NOT NULL,
  request_hash  char(64) NOT NULL,
  response_body jsonb NOT NULL,
  completed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);
CREATE UNIQUE INDEX lcr_hash_uniq
  ON lipsync.command_receipt (tenant_id, command_id, request_hash);

CREATE TABLE lipsync.outbox (
  outbox_id          uuid PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,             -- 见 §6.4 事件表
  aggregate_sequence bigint NOT NULL,
  event_type         text NOT NULL,
  partition_key      text NOT NULL,         -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  published_at       timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx
  ON lipsync.outbox (outbox_id) WHERE published_at IS NULL;

CREATE TABLE lipsync.inbox (
  consumer     text NOT NULL,
  event_id     text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  PRIMARY KEY (consumer, event_id)
);
```

**中继**：与 [S08 §6.4](S08-revision-snapshot.md) 同形——`FOR UPDATE SKIP LOCKED`、按 `partition_key` 单线程、失败重试不阻塞其他 partition、`published_at + 7d` 归档。

**为什么 `lipsync_take` 上要同时存 `video_hash` / `audio_hash` / `input_fingerprint` 三列：**

`input_fingerprint` 已经能区分不同的输入对，理论上只存它就够。但存两个原始 hash 有三个好处：

1. **能直接回答"这一版口型配的是哪一段音频"**，不用重算指纹。
2. **撤销检查可以按 `audio_hash` 批查**：S31 报告"这段音频的 take 变 void"，S32 按 `audio_hash` 一次找到所有受影响的口型，不需要知道那些 job 的指纹。
3. **审计可读**：法务问"这个片段里用的声音是哪一份"，`audio_hash` 是直接答案。

冗余是**故意的**，且不构成第二权威——hash 本身就是 S28 的 `content_hash`，S32 只是引用它。

### 5.3 约束理由

| 约束 | 防的是什么 |
|---|---|
| `lj_tenant_idem` | E12：同键同 body 重放，异 body 由 `lcr_hash_uniq` 判 409 |
| `lipsync_job_open_uniq` | E16：UNKNOWN 期间另建；同一输入对并发双提交 |
| `lj_repair_uniq` | 一个 `RepairAction` 被投递两次产生两个付费 job |
| `lj_preserve_faces` | 契约 `const: true` 的数据库落点；有人加"允许改别人脸"的开关时直接拒绝 |
| `lj_video_is_s28` / `lj_audio_is_s28` | 供应商直链或本地路径不能进输入引用 |
| `lj_window_order` / `lj_frame_order` | 反了的窗口会产生负帧数、负时长 |
| `ies_defects_have_ids` | **"音频有问题"必须有具体缺陷 id**。这条防的是"我觉得音频不对所以做口型没意义"这种无证据拒绝——它会变成 S32 用来逃避工作的借口 |
| `ftb_frames_fit` | 可见帧 + 遮挡帧超过总帧数 = 统计造错，会让 §4.7 的分级判断失真 |
| `lt_frames_fit` | 输出帧范围反了 |
| `lt_stale_has_time` | 标过期必须说明何时发现 |
| `odm_threshold_order` | `warn_pixel < eps_pixel` 会让 §4.6 的判定表自相矛盾 |
| `output_frame_count > 0` | 允许 0 帧会退化成假 `READY` |

---

## 6. 对外契约

### 6.1 `POST /lipsync-jobs`

```jsonc
{
  "command_id": "cmd-ls-0001",
  "request_hash": "sha256:…",
  "context": { /* execution-context.v2 */ },
  "shot_lineage_id": "lin_ep3_sc3_02",
  "speaker_character_id": "char_linlan",
  "video_ref": { "service_id": "S28", "id": "vid_…", "revision": 2, "content_hash": "…" },
  "audio_ref": { "service_id": "S28", "id": "aud_…", "revision": 1, "content_hash": "…" },
  "audio_take_id": "vt_01J…",
  "face_track": {
    "ref": { "service_id": "S28", "id": "ft_…", "revision": 1, "content_hash": "…" },
    "track_id": "face_2",
    "frame_range": { "start": 0, "end": 119 },
    "visibility": { "visible_frames": 108, "covered_frames": 11, "min_face_px": 96 },
    "detector": { "model": "…", "version": "…" }
  },
  "audio_window": {
    "start_ms": 820,
    "trim_head_ms": 180,
    "trim_tail_ms": 60,
    "source": "s37_alignment"
  },
  "preserve_other_faces": true,
  "fps": 30.0,
  "repair_action_id": null,
  "route_policy_ref": { "service_id": "S25", "id": "routing_demo", "revision": 1, "content_hash": "…" },
  "budget_scope_id": "budget_ep3",
  "deadline": "2026-11-03T23:30:00+08:00"
}
```

响应 `202`：

```jsonc
{
  "lipsync_job_id": "lj_01J…",
  "state": "CREATED",
  "operation_id": "op_…",
  "processed_frames": 108,
  "estimated_window_ms": 3600
}
```

`processed_frames` 在 202 里就返回，是让调用方能**立刻**知道这次花了多少帧——而不是等账单。

`409`（输入不合格）的响应形状最重要：

```jsonc
{
  "code": "AUDIO_NOT_ELIGIBLE_FOR_LIPSYNC",
  "detail": "audio has 2 open voice defects; lip-syncing to this audio would produce a mouth that matches the wrong words",
  "open_defect_ids": ["dft_…", "dft_…"],
  "suggested_action": "re_synthesize_speech",
  "suggested_service": "S31",
  "s37_report_ref": { "service_id": "S37", "id": "ar_…", "revision": 1, "content_hash": "…" }
}
```

**`suggested_action` / `suggested_service` 是给调用方和 S39 看的。** 一个不带指引的 409 会让调用方（尤其是自动化的 S39）把"音频错了"当成"口型暂时做不了"，然后重试口型——正是 §4.3 要防的。

### 6.2 `GET /lipsync-jobs/{id}`

```jsonc
{
  "lipsync_job_id": "lj_01J…",
  "source_revision": 12,
  "state": "READY",
  "business_attempt_no": 1,
  "provider_job_id": "pj_01J…",
  "inputs": {
    "video_ref": { "…": "…" },
    "audio_ref": { "…": "…" },
    "audio_take_id": "vt_01J…",
    "face_track_track_id": "face_2"
  },
  "audio_window": {
    "start_ms": 820, "end_ms": 4100,
    "trim_head_ms": 180, "trim_tail_ms": 60,
    "source": "s37_alignment"
  },
  "frames": {
    "process_start": 23, "process_end": 125,
    "processed": 108, "fps": 30.0
  },
  "input_fingerprint": "…",
  "eligibility": {
    "voice_defect_check": "clean",
    "audio_rights_state": "usable",
    "face_authorization_state": "granted",
    "s37_report_ref": { "…": "…" }
  },
  "take": {
    "lipsync_take_id": "lt_01J…",
    "artifact_ref": { "…": "…" },
    "visibility_grade": "full",
    "other_region_max_diff": 1.2,
    "other_face_drift": "none",
    "voice_defect_check_at_build": "clean",
    "stale_basis": null,
    "state": "READY"
  },
  "checkpoint": {
    "shot_lineage_id": "lin_ep3_sc3_02",
    "speaker_character_id": "char_linlan",
    "lipsync_take_id": "lt_01J…",
    "checkpoint_seq": 2
  },
  "frozen": false,
  "must_reconcile_before_resubmit": false,
  "history": [ { "seq": 7, "from": "INGESTING", "to": "READY", "at": "…" } ]
}
```

**`eligibility` 出现在查询响应里是刻意的**，与 [S31 §6.2](S31-speech-production.md) 同理：这是 S32 唯一能自证"我做过口型的那段音频当时是对的"的地方。

**`other_region_max_diff` 出现在响应里同样刻意。** 它是 §4.6 的实测值，让 S36 与 S40 能复核"这个结果真的没动别人脸"，而不是信任一个 `true`。

### 6.3 其他查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/lipsync-jobs?shot_lineage_id=&speaker_character_id=&state=` | 列表 |
| GET | `/shots/{shot_lineage_id}/lipsync-checkpoint` | 该镜全部说话人的当前基线（**S41 / S42 的主读口**） |
| GET | `/lipsync-takes/{id}` | take 详情 + 差异度量 + 失效依据 |
| GET | `/lipsync-takes/{id}/validity?video_hash=&audio_hash=` | **实时判定有效期**（§4.2 三路合取） |
| POST | `/lipsync-jobs/{id}:cancel` | 取消 |
| POST | `/lipsync-jobs/{id}:retry` | 仅 `{FAILED, RECONCILED_FAILED}`；`WAITING_UNKNOWN` 与 `INPUTS_INELIGIBLE` 时 **409** |

`/validity` 单独做成一个端点而不是靠客户端比对 hash，是因为三路合取里有**一路要实时查 S31 与 S28**（授权与 tombstone）。让客户端自己拼，等于让每个消费者各自实现一遍，且一定会有一个漏掉授权这一路。

### 6.4 事件

**发布：**

| event_type | aggregate | 何时 |
|---|---|---|
| `production.lipsync.ready` | `shot_lineage_id` | 检查点推进 |
| `lipsync.job.state-changed`（建议新增） | `lipsync_job_id` | 每次转移 |
| `lipsync.inputs-ineligible`（建议新增） | `shot_lineage_id` | `INPUTS_INELIGIBLE` 产生 |
| `lipsync.take.stale`（建议新增） | `lipsync_take_id` | take 过期 |

`production.lipsync.ready` 的 `data`：

```jsonc
{
  "lipsync_job_id": "lj_01J…",
  "lipsync_take_id": "lt_01J…",
  "shot_lineage_id": "lin_ep3_sc3_02",
  "speaker_character_id": "char_linlan",
  "artifact_ref": { "service_id": "S28", "id": "…", "revision": 1, "content_hash": "…" },
  "source_video_ref": { "id": "…", "content_hash": "…" },
  "source_audio_ref": { "id": "…", "content_hash": "…" },
  "audio_take_id": "vt_01J…",
  "audio_window": { "start_ms": 820, "end_ms": 4100 },
  "frames": { "process_start": 23, "process_end": 125, "fps": 30.0 },
  "visibility_grade": "full",
  "other_face_drift": "none",
  "voice_defect_check_at_build": "clean",
  "checkpoint": { "from": "lt_00X…", "to": "lt_01J…", "checkpoint_seq": 2 },
  "input_fingerprint": "…"
}
```

**不含**：媒体 URL、人脸轨引用、`speaker_character_id` 之外的生物特征、供应商信息、差异掩膜。人脸轨引用尤其不能出——它是一份人脸位置数据，属于生物特征派生信息（[docs/14](../docs/14-enterprise-operations.md) 最小化）。

**消费：**

| 来源 | 事件 | 用途 |
|---|---|---|
| S26 | `generation.job.state-changed` | 驱动状态机 |
| S28 | `artifact.ingested` | 完成 `INGESTING → READY`；**需要帧数、fps、时码**（§12 C4） |
| S31 | `production.speech.ready` | 新音频可用 → 作废相关 take（`audio_changed`） |
| S31 | `speech.voice-authorization-lapsed`（建议） | 音频权利失效 → 作废相关 take（`audio_rights_void`） |
| S30 | `production.video.ready` | 新片段 → 作废相关 take（`video_changed`） |
| S37 | `quality.alignment.completed` | 记录 `s37_report_ref`；**不据此改状态** |
| S39 | `quality.repair.planned` | 只读，提前知道有 `redo_lipsync` 在路上 |
| S41 | `post.timeline.revision.created` | 只读；镜头被删时 take 变 `SUPERSEDED` |
| S08 | `snapshot.manifest.committed` | 只读投影 |

**S32 不消费 `quality.*` 来自动重做。** 尤其不因为 `AUD.LIPSYNC.OFFSET` 出现就自己再排一次口型——那正是 §8 说的失控路径。

---

## 7. 幂等与并发

### 7.1 幂等

| 层面 | 键 | 冲突 |
|---|---|---|
| 命令 | `tenant + command_id + request_hash` | 同体返回原响应；异体 `409` |
| 业务 | `(tenant, shot_lineage_id, speaker_character_id, input_fingerprint)` 未终结 | 返回已有 job |
| 修复 | `(tenant, repair_action_id)` | `409 REPAIR_ALREADY_APPLIED` |
| 事件入站 | `(consumer, event_id)` | 丢弃重复 |

`request_hash` 含：`context` 业务字段、`video_ref`、`audio_ref`、`audio_take_id`、`face_track`（全部）、`audio_window`（全部）、`fps`、`repair_action_id`、`budget_scope_id`、`deadline`。**不含** `trace_id`、`command_id`。`preserve_other_faces` 恒为 true，不参与。

### 7.2 并发

| 场景 | 机制 |
|---|---|
| 同镜同人两个 job 同时 READY | `lipsync_checkpoint` 乐观锁 CAS |
| 同镜不同人并发 | 互不影响（检查点按 `speaker_character_id` 分行） |
| 同一 `RepairAction` 并发投递 | `lj_repair_uniq` |
| 音频在 job 运行中被替换 | `audio_changed` 事件标 stale；`INGESTING→READY` 前复查输入对，不符则不推进 |
| 视频在 job 运行中被替换 | 同上 |
| 撤销事件与 `READY` 同时到 | **作废优先**：`READY` 转移事务内必须重读输入对与授权状态 |
| 两个 job 用同一输入对、不同窗口 | 指纹不同，允许并存；一个推进检查点，另一个 `SUPERSEDED` |
| 人脸轨被重新检测（新 revision） | `face_track_changed` 标 stale。**必须是失效依据之一**——换了人脸轨可能意味着之前框错了脸 |

**"作废优先"的实现方式**：`UPDATE lipsync_take SET stale_basis=… WHERE lipsync_take_id=… AND stale_basis IS NULL` 与 `READY` 转移在同一 `SERIALIZABLE` 事务，且转移前在事务内重读。

### 7.3 重启恢复

- `CHECKING_INPUTS` 重启后**必须重做**全部检查（S37 报告可能新增、S31 可能已撤销）。
- `PREPARING` 重启后**重新取 RouteDecision**（报价可能过期）。
- `SUBMITTED` / `WAITING_PROVIDER` 重启后向 S26 查状态。
- `INGESTING` 重启后**重新计算非目标区域差异**，不能沿用内存值。
- `WAITING_UNKNOWN` 重启后**什么都不做**，等 S26 对账。
- `INPUTS_INELIGIBLE` 重启后**什么都不做**，它是终态。

---

## 8. 跨 Run 尝试计数：为什么重做口型会失控

S32 不数数——上限在 S39。但 S32 是这个平台上**最容易把上限吃光**的一类生产，必须说清楚机制。

### 8.1 失控链条

```
第 1 轮：音频偏了 300ms（实际上音频开头有 300ms 静音没被 trim）
        → S37 报 AUD.LIPSYNC.OFFSET (blocker)
        → S39 下发 redo_lipsync
        → S32 用同一个音频、同一个窗口再做一次
        → 还是偏 300ms（输入没变，输出必然没变）
        → S39 又看到 offset
        → 下发 redo_lipsync（第 2 次）
        → ...
```

**这条链上唯一变化的是 `attempt_no` 和账单。** 输入对完全没变，所以按定义输出也不该变——`variation_nonce` 变了，但口型模型对同一输入的输出差异主要在纹理细节，不在时间对齐上。

[S39 §5](S39-repair-planning.md) 的 `max_identical_strategy_repeats: 1` 就是为这条链设的：同策略最多重复 1 次。但**那需要 `strategy_class` 真的把"同样的输入"识别为同一策略**。

S32 的贡献是：把窗口与帧范围放进 `input_fingerprint`（§2.3、§2.4），从而让"窗口改了"和"窗口没改"成为可区分的策略。若窗口不在指纹里，S39 无法知道第 2 次的输入和第一次一样。

### 8.2 `strategy_class` 的归一参数

`lipsync` 族的归一参数在 [S39 §5.3](S39-repair-planning.md) 里是 `lipsync_model_family`。S32 实现时**必须**补上两项，否则 §8.1 的链条照旧：

建议的归一参数（§11 U2、§12 C5）：

```
strategy_class = redo_lipsync
                 : model_family = {route 侧模型族}
                 : window_source = {explicit|s37_alignment|editorial}
                 : window_changed = {true|false}
                 : face_track_revision = {n}
```

**`window_changed` 是这条的关键。** 它把"我们改了窗口再试"与"我们什么都没改又试了一次"分成两个策略类。前者是一次真实的诊断步骤，值得重复；后者是空转，必须被 `max_identical_strategy_repeats` 掐掉。

### 8.3 S32 自己的防呆：同指纹并发拒绝

`lipsync_job_open_uniq` 保证同一 `input_fingerprint` 不会有两个未终结的 job。这挡住了**同一时刻**的空转，但挡不住**一个失败、再来一个**。

所以还需要一条运行时规则：

> **若纯 `redo_lipsync`（`window_changed=false` 且 `model_family` 相同）在一个 `shot_lineage_id` 上已经失败过一次，S32 仍执行（上限是 S39 的事），但必须在 `lipsync_intent_ledger` 里记 `repeat_without_input_change=true`，并 emit `lipsync.repeat-without-change`。**

这不是拒绝，是**留证据**。S39 与 S40 需要看到"这个镜头在输入完全没变的情况下被要求做了 3 次口型"，而不是只看到一条 attempt 曲线。

### 8.4 例子

| Run | 事件 | `action` | 窗口变了吗 | 台账 | S39 累计 |
|---|---|---|---|---|---|
| Run 1 | 首轮口型 | `generate` | — | 1 行 | 1 |
| Run 2 | S37 报 offset，窗口没改就重做 | `redo_lipsync` | `false` | 1 行 + `repeat_without_input_change=true` | 2 |
| Run 3 | S39 改了 `trim_head_ms` 后重做 | `redo_lipsync` | `true` | 1 行 | 3（新策略类） |
| Run 4 | 音频被重合成，口型需重做 | `redo_lipsync` | `true`（指纹整体变） | 1 行 | 4 |
| Run 5 | 音频文本有缺陷，请求口型 | — | — | **0 行** | **不变，指向 S31** |

第 2 行与第 5 行的对比是核心：**空转要留证据但不算新策略；音频错了根本不该进这个计数器**。

---

## 9. 可观测

| 指标 | 类型 | 用途 | 告警 |
|---|---|---|---|
| `s32_jobs{state}` | gauge | 堆积 | `WAITING_UNKNOWN` > 0 |
| `s32_inputs_ineligible_total{reason}` | counter | 输入不合格分布 | `AUDIO_TEXT_DEFECT_OPEN` 突增 = 上游在音频有缺陷时批量请求口型 |
| `s32_audio_not_eligible_suggested_s31_total` | counter | 被挡回 S31 的次数 | **持续 > 0 说明 S39 规划在跨族越权** |
| `s32_low_visibility_reject_total{grade}` | counter | 可见度拒绝 | 突增 = 上游送来大量背对镜头的片段 |
| `s32_other_face_altered_total` | counter | 越界被拒 | **任何一次都告警**（权利约束被违反） |
| `s32_other_face_drift_minor_total` | counter | 轻微漂移 | 突增 = 掩膜半径不足 |
| `s32_stale_take_total{basis}` | counter | 过期原因分布 | `audio_changed` 突增 = 上游抖动 |
| `s32_repeat_without_change_total` | counter | 空转重做 | **任何一次都告警到 S39 负责人** |
| `s32_processed_frames_total` | counter | 帧数消耗 | 与 S23 的 `modality=lipsync` 计费帧数**每日对齐** |
| `s32_paid_calls_total` | counter | 本服务发起的付费生成 | 与 S26 计数对齐，差异 > 0 = 绕过 |
| `s32_job_duration_seconds` | histogram | 延迟 | p99 超 SLO |
| `s32_frame_count_per_job` | histogram | 单次帧数 | 持续接近 `max_frames_per_job` = 有镜头需要拆分策略 |
| `s32_window_source{source}` | counter | 窗口来源 | `explicit` 占比过高 = 调用方在猜 |
| `s32_frame_count_mismatch_total` | counter | 输出帧数不符 | 任何一次都告警 |
| `s32_fingerprint_mismatch_on_ingest_total` | counter | 归档时输入已变 | 突增 = 上游并发推进太快 |
| `s32_assertion_failures_total{kind}` | counter | 内部断言 | 任何一次都告警 |

**`s32_processed_frames_total` 必须按帧对齐，不能按次对齐。** 一次 4 秒口型和一次 30 秒口型在"次数"上是 1:1，在成本上是 1:7.5。按次对齐会让账单差异一直藏在平均数里。

### 9.2 追踪

`traceparent` 贯穿 `POST /lipsync-jobs → S37 读 → S25 → S26 → S27 → S28 → production.lipsync.ready → S37 复检 → S36`。

必须能回答："这一版口型用了哪段视频、哪段音频、视频里哪几帧、音频从第几毫秒开始、当时音频文本检查过没有、所在人脸轨是哪一条、可见度如何、有没有动别人脸、花了多少帧、多少钱"。

追踪维度沿用 `docs/design-package/services/S32-lipsync.md` 的定义：`tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace`。

### 9.3 日志

不记人脸轨内容、不记人脸框坐标、不记 `speaker_character_id` 对应的真实身份、不记媒体 URL。

记：`input_fingerprint`、`video_hash`、`audio_hash`、`window_start_ms`、`process_frame_start/end`、`processed_frames`、`visibility_grade`、`other_region_max_diff`、`stale_basis`、`deny` 原因码。

**人脸位置数据属于生物特征派生信息。** 日志里出现"某人脸在第 23–125 帧的 (x,y,w,h)"，就等于在日志系统里建了一份人脸轨迹库。需要这些数据的只有 S32 的运行时与受控的调试通道。

---

## 10. 测试清单

| 编号 | 场景 | 期望 | E |
|---|---|---|---|
| T32-01 | 同 command 重复 | 返回原响应 | E12 |
| T32-02 | 同 command 异 body | `409` | E12 |
| T32-03 | 缺 `audio_window` | `422 AUDIO_WINDOW_REQUIRED`，错误指向 S37 | E24 |
| T32-04 | `end_ms` 与音频时长差 > 1 帧 | `422 AUDIO_WINDOW_MISMATCH` | E24 |
| T32-05 | 窗口越出视频边界 | `422 AUDIO_WINDOW_OUT_OF_RANGE` | E24 |
| T32-06 | 音频有未关闭 `AUD.TEXT.MISPRONOUNCE`(major) | `409 AUDIO_NOT_ELIGIBLE_FOR_LIPSYNC`，`suggested_action=re_synthesize_speech` | E24 |
| T32-07 | 同上但缺陷已关闭（修复后重检通过） | 允许 | E24 |
| T32-08 | S37 尚未跑过 | 允许，`voice_defect_check='not_yet_available'` 且进 ready 事件 | E24 |
| T32-09 | 只有 `AUD.TEXT.SCRIPT_CONFLICT` | 允许（文本冲突不是念错） | E24 |
| T32-10 | 音频 take `rights_state='void'` | `INPUTS_INELIGIBLE(AUDIO_RIGHTS_VOID)` | E30 |
| T32-11 | 视频 tombstone | `INPUTS_INELIGIBLE(VIDEO_TOMBSTONED)` | E23 |
| T32-12 | 人脸授权失效 | `INPUTS_INELIGIBLE(FACE_AUTHORIZATION_LAPSED)` | E30 |
| T32-13 | 人脸缺席（可见帧 0） | `REJECTED_LOCAL(NO_FACE_IN_WINDOW)`，不花钱 | E24 |
| T32-14 | 可见帧 30%（`sparse`） | `REJECTED_LOCAL(LOW_FACE_VISIBILITY)`，**不"尽力而为"** | E24 |
| T32-15 | 可见帧 70%（`partial`） | 允许，take 标 `visibility_grade='partial'` | E24 |
| T32-16 | `min_face_px=30` | `REJECTED_LOCAL(FACE_TOO_SMALL)` | E24 |
| T32-17 | 帧数 1200 > 上限 900 | `REJECTED_LOCAL(FRAME_BUDGET_EXCEEDED)`，不自动拆 | E35 |
| T32-18 | 输出在补集区域最大差 6/255（< warn 8） | 通过，`other_face_drift='minor'` | E01 |
| T32-19 | 输出在补集区域最大差 40/255 | `FAILED(OTHER_FACE_ALTERED)`，**不登记 take** | E01 |
| T32-20 | 尝试把 `preserve_other_faces` 设为 false | DB 约束拒绝 / schema 拒绝 | E01 |
| T32-21 | 输出帧数少于输入 | `FAILED(FRAME_COUNT_MISMATCH)` | E23 |
| T32-22 | 输出时间码与输入不符 | `FAILED(TIMECODE_MISMATCH)` | E23 |
| T32-23 | 归档时音频已换 | take 标 `stale_basis='audio_changed'`，不推进检查点 | E13 |
| T32-24 | 归档时视频已换 | take 标 `stale_basis='video_changed'` | E13 |
| T32-25 | 窗口改动后重做 | 指纹不同，`window_changed=true`，新策略类 | E26 |
| T32-26 | 窗口未改的空转重做 | 记 `repeat_without_input_change=true` | E26 |
| T32-27 | 同一 `RepairAction` 投递两次 | `409 REPAIR_ALREADY_APPLIED` | E12 |
| T32-28 | 对 S32 输出再做口型，无 `repair_action_id` | `422 SELF_CHAIN_FORBIDDEN` | E24 |
| T32-29 | 对 S32 输出再做口型，带 `repair_action_id` | 允许 | E24 |
| T32-30 | `video_ref` 来源是供应商直链 | `422 UNEXPECTED_VIDEO_ORIGIN` | E01 |
| T32-31 | `video_ref.service_id='openai'` | DB 约束拒绝 | E01 |
| T32-32 | `voice_defect_check='defects_open'` 但 `open_defect_ids` 为空 | DB 约束拒绝 | E24 |
| T32-33 | 路由不支持局部重绘 | `REJECTED_LOCAL(CAPABILITY_MISSING)`，**不降级为整帧重绘** | E18 |
| T32-34 | S26 `SUBMISSION_UNKNOWN` | `WAITING_UNKNOWN`，不发新请求 | E16 |
| T32-35 | UNKNOWN 期间同指纹新请求 | 拒绝，指向对账中的 job | E16 |
| T32-36 | 对账回来的结果输入对已变 | 标过期，不推进检查点 | E16 |
| T32-37 | 归档 URL 过期 | 向 S26 要新 URL，**不重新生成** | E23 |
| T32-38 | S28 不带帧数/fps | 阻塞 `INGESTING` + 告警，不假 READY | E23 |
| T32-39 | 归档 hash 不符 | `FAILED(HASH_MISMATCH)` | E23 |
| T32-40 | 收到 `voice` 类的 `redo_lipsync` | `422 ACTION_TYPE_MISMATCH` | E24 |
| T32-41 | 收到 `re_synthesize_speech` | `422`，不参与 | E24 |
| T32-42 | 同镜两个说话人并发 | 两个检查点互不影响 | E20 |
| T32-43 | 同镜同人两个 job 同时 READY | CAS：一个成功一个重读 | E20 |
| T32-44 | 撤销事件与 `READY` 并发 | 作废优先 | E30 |
| T32-45 | 重启于 `CHECKING_INPUTS` | 重做全部检查 | E13 |
| T32-46 | 重启于 `INGESTING` | 重算差异度量 | E13 |
| T32-47 | 跨租户 video/audio/take | `404` | E28 |
| T32-48 | 跨租户 `face_track_ref` | `404` | E28 |
| T32-49 | 预算不足 | `FAILED(BUDGET)`，不降帧率不缩窗口 | E14 |
| T32-50 | 账户撤销 | `FAILED(ACCOUNT_UNAUTHORIZED)`，不换渠道 | E18 |
| T32-51 | 数据驻留禁止 | `REJECTED_LOCAL(RESIDENCY_FORBIDDEN)` | E31 |
| T32-52 | Kafka 重放 `production.lipsync.ready` | 幂等消费，不触发二次生成 | E32 |
| T32-53 | 独立升级回滚 | 表驱动状态机一致 | E01 |
| T32-54 | `GET /lipsync-takes/{id}/validity` 在音频被撤销后 | 返回 `false` + `basis=audio_rights_void` | E30 |

---

## 11. 实施顺序与未决

### 11.1 顺序（W3）

1. 表 + 状态机骨架 + 全部 CHECK 约束（T32-01/02/20/32/43）。
2. `AudioWindow` 必填与校验（T32-03…05）。这一步在接任何模型之前做完——它是报价正确性的前提。
3. 输入合格性检查的 mock（S37 / S31 / S28 / S10），覆盖全枚举（T32-06…12）。
4. **跨族拒绝路径 + `suggested_action` 响应形状**（T32-06）。必须早于任何付费通路。
5. 可见度分级（T32-13…16）。
6. 帧范围计算与上限（T32-17）。
7. 非目标区域差异度量（T32-18/19/21/22）。这是硬约束，必须先于真实模型。
8. S25（mock）：局部重绘能力（T32-33）。
9. S26（mock）：预算/配额/UNKNOWN（T32-34…36、49、50）。
10. S28（mock）：帧数、时码、URL 过期、hash（T32-37…39）。
11. 有效期三路合取 + `/validity` 端点（T32-23/24/54）。
12. 链式叠加与 origin 校验（T32-28…31）。
13. 台账与空转标记（T32-25/26）。
14. 巡检、告警、runbook。
15. 真实渠道需授权。

### 11.2 未决

| # | 问题 | 当前处理 |
|---|---|---|
| U1 | 首轮 `audio_window.start_ms` 由谁提供 | **阻塞**（§12 C2）。当前要求调用方显式给，缺则 422。三条候选来源见 §2.3 |
| U2 | `strategy_class` 的归一参数 | [S39 §5.3](S39-repair-planning.md) 只给了 `lipsync_model_family`，不够（§8.2）。当前 S32 额外记 `window_changed` 到台账，未进 `strategy_class`（§12 C5） |
| U3 | 掩膜膨胀半径 `r` 与阈值 `eps` / `warn` | 当前取 32px / 2 / 8（/255）。应基于真实片段统计后定档，且可能需要按分辨率分档 |
| U4 | 人脸轨由谁产出 | **阻塞**（§12 C1）。当前接受 `S28` + `content_class ∈ {evidence, uploaded_reference}` + 显式 `track_id` |
| U5 | 多人同镜的处理 | 当前一 job 一 `speaker_character_id`；多说话人 = 多 job。若同一时刻两人都在说话，两个 job 的嘴部区域可能重叠——重叠时谁赢未定（§12 C6） |
| U6 | 整帧重绘类模型能否满足 `preserve_other_faces` | 当前靠 §4.6 度量事后拒绝。若主流渠道都做不到，会影响路由可行性（§12 C7） |
| U7 | 输出是否需要重新编码 | 当前要求输出与输入同编解码参数，避免 S42 侧拼接问题。若渠道必须重编码，需要 S28 记 `transcode` 溯源 |
| U8 | `fps` 是请求给的还是从视频读 | 当前请求必填并进指纹，且要与 S28 探测值一致（±0.01） |
| U9 | 口型是否需要 `variation_nonce` 的多候选 | 当前一 job 一 take（同 S31 §2.2 的理由） |

---

## 12. 冲突与契约缺口

### C1 `lipsync.input.face_track_ref` 指向 S28，但 S28 没有人脸轨类别

`contracts/generation-command.v2.schema.json` 的 `lipsync.input` 要求 `face_track_ref`，形状是标准 `{service_id, id, revision, content_hash}`，示例指向 `service_id: "S28"`。

**冲突点**：S28 的 `content_class` 是闭集 `generated_video, generated_image, generated_audio, rendered_master, proxy, thumbnail, waveform, subtitle_track, audio_stem, uploaded_reference, evidence, model_archive`——**没有人脸轨**。而且 S28 的契约没有"对象内多条轨"的概念，也没有帧范围/可见度统计。

更根本的问题：**谁产出人脸轨？** 现实中可以是 S10（资产解析）、S30（视频生成时已知道在生成谁）、S36（QC 时检测）。三者的时机、精度、语义都不同，契约没有指定任何一个。

**不选边**：本文按"S28 + `evidence`/`uploaded_reference` + 显式 `track_id` + `visibility` 统计"实现（§2.5），并在组 `generation-command` 时丢掉 `track_id`/`visibility`（schema 拒绝额外字段）。这意味着 **S26/S27 侧无法验证 S32 处理的是哪张脸**。

**建议**：S28 的 `content_class` 补 `face_track`，并定义其对象内结构（轨 id、逐帧框、置信度）；或明确人脸轨由 S10 产出并通过 S28 归档，在 `lipsync.input.face_track_ref` 的 `service_id` 允许 `S10`。

### C2 `lipsync.input` 没有任何时间字段

契约的 `lipsync.input` 是 `{video_ref, audio_ref, speaker_character_id, face_track_ref, preserve_other_faces}`，`additionalProperties: false`。

**冲突点**：音频在视频时间轴上的位置**无法表达**。系统只能假设"音频从第 0 毫秒开始"，而这对任何"角色中途开口"的镜头都是错的（§2.3）。更糟的是这个假设不会报错，只会稳定地产生 offset 缺陷，然后进入 §8.1 的空转链条。

**第二个后果**：S25 无法按帧数报价。`processed_frames` 完全由窗口决定，而窗口在契约里不存在。

**不选边**：本文在 S32 自己的 API 上把 `audio_window` 设成必填（§6.1），在组命令时丢弃。

**建议**：`lipsync.input` 增加 `audio_window: {start_ms, trim_head_ms, trim_tail_ms, source}` 与 `fps`，并把 `start_ms` 设为 required。这是四个缺口里影响最直接的一个——它决定第一版能不能真的对齐。

### C3 命令里没有 `processed_frames`，S25 报价只能看到时长

即使补了 C2，`processed_frames` 仍是从窗口 + `fps` 算出来的派生量，不是命令字段。

**冲突点**：S25 的报价依赖 `duration_seconds` 或类似字段（见 `sound_effect.input.duration_seconds`）。口型的成本以帧计，且"30 秒视频里只有 2 秒在说话"与"30 秒全程说话"的成本差 15 倍。按音频时长报价会系统性低估。

**不选边**：S32 在本地记账并按 `processed_frames` 统计消耗（§9），不假设 S25 知道。

**建议**：`lipsync.input` 加 `processed_frame_count`（或让 S25 从 `audio_window` + `fps` 推导），并在 S24 的能力登记里让口型渠道按帧计价。

### C4 `artifact.ingested` 没有 payload schema，S32 需要帧数、fps、时码

同 [S31 §12 C2](S31-speech-production.md)。S32 的 `INGESTING → READY` 依赖 `frame_count`、`fps`、`timecode_start`、`codec`，以及**`ingesting_service`**（§4.9 的链式判断）。

**冲突点**：`docs/09 §3` 只要求"输出下载失败只重归档"，没有要求 S28 探测视频结构。

**不选边**：按 S28 提供实现，不提供时阻塞 + 告警（T32-38）。

**建议**：与 S31 C2 一起补齐 `artifact.ingested` 的 payload，包含 `ingesting_service`（这是 E01"谁提交的"审计的必要字段，不止 S32 需要）。

### C5 `strategy_class` 归一参数不足以阻止空转

[S39 §5.3](S39-repair-planning.md) 给 `redo_lipsync` 的归一参数是 `lipsync_model_family`。但 §8.1 的空转链条里，模型族**从来没变过**——变的只是次数。

**冲突点**：`max_identical_strategy_repeats: 1` 依赖 `strategy_class` 的粒度。若归一参数里没有"输入是否变化"，那么"改了窗口再试"和"完全没改再试"是同一个策略类，前者会被后者连累（第一次重复就被掐掉）；若反过来只有"输入是否变化"没有模型族，则"换模型"与"没换模型"混在一起。

**不选边**：本文建议的归一参数是 `model_family + window_source + window_changed + face_track_revision`（§8.2），但不改 S39 的方案。

**建议**：S39 §5.3 补 `redo_lipsync` 的完整归一参数，至少包含 `window_changed`。

### C6 同镜多说话人、嘴部区域重叠时无规则

S32 的粒度是 `(shot_lineage_id, speaker_character_id)`。一个镜头里两人对话且同时出现在画面里时，两个 job 会各自处理同一批帧，但改的是**不同的嘴**。

**冲突点**：两种失败情形都没有规则：

1. **嘴部区域重叠**（侧脸对话、近距离双人）——两个 job 都会在那个区域写像素，后完成的覆盖先完成的，或者两者混合产出鬼影。
2. **同一时刻两人都在说话**（抢话）——口型模型通常只支持一个驱动音频，第二个 job 的音频对第一个是干扰。

`preserve_other_faces` 在这里救不了：另一个说话人**不是"别人"**，他是这次生产的另一个合法目标。

**不选边**：当前不做特殊处理，靠 `lipsync_checkpoint` 的 CAS 让后完成的获胜，并依赖 S36 事后发现鬼影。

**建议**：明确"同镜多说话人"是**一次 job 多轨驱动**还是**多次 job 串行**。若是后者，需要 S32 在处理第二个 speaker 时把第一个 speaker 的嘴部区域也纳入保护掩膜（即 `M_other` 要排除所有已处理的说话人区域）。这需要一个跨 job 的"已处理区域"记录，契约里没有。

### C7 路由能力没有"支持局部重绘"这一项

`preserve_other_faces: true` 要求模型只改嘴部区域。有些口型渠道的实现是整帧重绘再合成，实际会改动大面积像素。

**冲突点**：S24 的能力登记（`docs/05` / `contracts`）没有"局部重绘"或"修改区域受限"这类能力位。S32 只能在 `INGESTING` 阶段靠 §4.6 的度量**事后发现**——那时钱已经花了。

**不选边**：本文按"事后度量 + 拒绝登记"实现（T32-19），并在路由阶段不设能力过滤。

**建议**：S24 增加能力位 `face_region_localized_edit`（布尔）或 `editable_region`（枚举），S25 在口型路由时必须要求它。

### C8 `service.yaml` 的 `depends_on` 缺 S30 / S37 / S25 / S10 / S36 / S39 / S41

`depends_on = [S26, S28, S31]`。实际还依赖：

- **S30**：`video_ref` 的来源
- **S37**：`AUD.TEXT.*` 的判定（§4.3 的拒绝依据）与 `AUD.LIPSYNC.*` 的复检
- **S25**：`route_decision_id` 必需
- **S10 / S13**：人脸身份与授权
- **S36**：视觉复检（身份一致性、遮挡、边缘）
- **S39**：`RepairAction` 来源
- **S41**：时间线与镜头删除

**不选边**：按实际依赖实现，`service.yaml` 是生成物，不手改（[AGENTS.md](../AGENTS.md)）。

**建议**：catalog 补 `depends_on`。

### C9 口型的"必做"与"可做"没有判定标准

`docs/07 §1 步骤 6` 说"S32 **必要时**做口型"。"必要"是什么？

[S30 §4.4](S30-video-production.md) 有 `native_locked_voice` 等音频策略，暗示了"什么时候需要口型"的语义空间，但没有一条规则说"哪些镜头必须过 S32"。

**冲突点**：如果不做口型，那么 S37 的 `AUD.LIPSYNC.*` 检查会对原生视频跑——可能报大量缺陷，而正确做法可能是"这个镜头本来就不该做口型"。反过来，如果全做，成本会翻倍。

**不选边**：S32 只回答"能不能做"，不回答"该不该做"。`need_lipsync` 是一个由 S06/S07 或 S21 决策的上游字段，S32 不猜。

**建议**：在 ShotSpec 或 orchestrator 层明确 `lipsync_required` 的判定规则（例如：`audio_strategy ∈ {silent_then_tts, dubbed}` 且角色正脸可见 → 必须）。S32 可提供 `GET /lipsync-eligibility?vshot=…` 辅助判断，但不做决策。

---

## 13. 与其他方案的交叉引用

| 方案 | 关系 |
|---|---|
| [S30](S30-video-production.md) | `video_ref` 的来源；`silent_then_tts`/`native_locked_voice` 决定是否需要口型；S30 的 take 变了即 S32 失效 |
| [S31](S31-speech-production.md) | `audio_ref` 的来源与**唯一**音频权威；音频文本缺陷是 S32 的拒绝依据（§4.3）；**S32 绝不重合成语音** |
| [S37](S37-audio-alignment.md) | `AUD.TEXT.*` 决定音频能不能做口型；`AUD.LIPSYNC.*` 复检结果；`alignment_unit` 是 `audio_window.trim_*` 的来源 |
| [S35](S35-technical-qc.md) | `lipsync` 动作族只有 `redo_lipsync`，禁止跨族；S32 的拒绝路径是这条禁令的镜像 |
| [S36](S36-semantic-qc.md) | 视觉复检：身份一致性、遮挡、边缘融合。S32 只验"没动别人脸"，不验"改得好不好" |
| [S39](S39-repair-planning.md) | 下发 `RepairAction`；跨 Run 累计与 `strategy_class`（**归一参数不足**，C5） |
| [S25](S25-model-routing.md) | `route_decision_id` 必需；**缺 `face_region_localized_edit` 能力位**（C7） |
| [S26](S26-generation-jobs.md) | 唯一付费入口；`attempt_no` 与节点费用上限；`SUBMISSION_UNKNOWN` 冻结 |
| [S28](S28-artifact-ingest.md) | 视频/音频/人脸轨/blob 的唯一权威；**缺 `face_track` 类别**（C1）；**缺 payload schema**（C4） |
| [S10](S10-asset-catalog.md) / [S11](S11-character-assets.md) | 人脸身份与授权；S32 只读，不改脸 |
| [S41](S41-editorial.md) | 时间线所有者；镜头删除时 take 变 `SUPERSEDED` |
| [S42](S42-composition.md) | 合成唯一执行者；S32 不碰音频、不混音 |
| [S08](S08-revision-snapshot.md) | 接受权威；检查点推进 ≠ 接受 |
| [S29](S29-image-production.md) / [S30](S30-video-production.md) | 同构风格与共享缺口（C1 OpenAPI、C4 `artifact.ingested`、生产组事件） |
