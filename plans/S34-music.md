# S34 · BGM 与音乐 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。所有供应商仍为 `disabled`，`slo.verified: false`。

上游依据：[docs/07 §1 步骤 7](../docs/07-production-workflows.md)（"S41 创建 EDL，S33/S34 准备音效音乐，S38 生成字幕，S42 混音合成"；返工时"重算 EDL、字幕时码、音效/BGM 切点、混音与 master"）、[docs/09 §1](../docs/09-model-execution.md)、[docs/11 §2](../docs/11-quality-repair.md)（"BGM 盖对白 | ducking/重混音"）、[S41 §4.2–§4.4](S41-editorial.md)、[S42 §4](S42-composition.md)、[S35 §2.2.1](S35-technical-qc.md)、[S39 §5](S39-repair-planning.md)、`contracts/generation-command.v2.schema.json`（`modality: music`）、`contracts/quality/action-families.v2.json`。

链上位置：`S07 分镜/情绪曲线 → S34 组音乐意图（情绪 + 时长 + 编制 + 许可）→ S25 估价路由 → S26 准入 → S27 执行 → S28 归档 → S34 检测节拍/调性/循环点、登记候选 → 导演选定 → S41 按 EDL 排切点 → S42 混音成 stem_bgm`。

本文重点回答三个问题：**"变时长重算切点"为什么不是"把音乐拉长"**（§4.2、§4.3）、**一首曲子的"许可"为什么是一个四元组而不是一个引用**（§4.4）、**"不遮盖对白"这件事到底由 S34 做还是由 S42 做**（§4.5）。

---

## 0. 一句话边界

**S34 拥有"这条 cue 要什么情绪、多长、什么编制、在什么许可范围内、生成了哪几个候选、每个候选的节拍/调性/循环点在哪、导演选了哪一个"。它不拥有分镜、不拥有时间线、不拥有音频字节、不拥有混音与 ducking、不做最终导出。**

```
S07:   分镜、情绪曲线、cue 意图（S34 只读）
S41:   EDL、clip 时间、`affected_output_ranges`（S34 只读）
S02:   权利策略（S34 只引用与解析，不裁定）
S34:   MusicCueRevision + MusicTake(候选) + BeatGrid + LoopPointSet + CueSelection + LicenseBinding + 缺口
S25:   可行路由与报价
S26:   attempt / ProviderJob / SUBMISSION_UNKNOWN（唯一付费通道）
S28:   音频 bytes 与 content_hash（唯一权威，`content_class='generated_audio'` 或 `'audio_stem'`）
S42:   混音、ducking、响度、stem_bgm 的最终形态
S43:   交付、发行物、权利申报清单
S23 / S22: 钱与 permit
```

判据：任何"这条 cue 现在用哪首"的问题，答案来自 S34 的 `CueSelection`。任何"它从第几小节进、在哪一小节收"的问题，答案来自 S34 的 `BeatGrid` + `CutPlan`。任何"这首能不能用"的问题，答案来自 S34 的 `LicenseBinding`（解析自 S02）。任何"混出来多响、对白被压了多少"的问题，答案来自 S42。

**必须守住的一条原则：**

> **音乐不拉伸，只换切点；切点换不动，就重做，不硬凑。**

这条原则与 S33 形成对照，也解释了为什么 S34 的"变时长重算切点"需要一整节来说明。

S33 的音效可以拉伸 2 倍（脚步走快一点是自然的）。**音乐不能。** 一段 90 BPM 的弦乐拉成 96 BPM，不只是变快——它改变了律动、把原本写好的和声进行提前撞上画面、让重音落在错误的位置。观众说不出来，但会觉得"配乐怪"。

所以 S34 面对"剪辑把这段从 42 秒变成 39 秒"时，正确的动作顺序是：

```
1. 在小节线上收（bar_trim）          —— 几乎无损
2. 不行就在乐句线（4/8 小节）上收      —— 通常可接受
3. 不行就用循环点拼接（loop_stitch）    —— 需要素材有可循环标记
4. 都不行 → 重新生成（resynthesize）    —— 花钱，且是最后手段
5. 连重新生成也不该做 → 记缺口          —— 让人决定
```

**绝不出现的一步：把音乐时间拉伸以适配画面。**

**S34 与 S33 的四处结构性差异：**

| 维度 | S33 音效 | S34 音乐 |
|---|---|---|
| 时长适配 | 可拉伸（按族有上限） | **不拉伸**，只换切点 |
| 候选数 | 一 job 一 take | **一 job 多候选**（导演要挑） |
| 时间结构 | 一个起点 + 时长 | **节拍网格 + 小节线 + 乐句线** |
| 许可 | 单一许可引用 | **四元组**（作曲/录音/表演/同步） |
| 与对白的关系 | 可能盖对白（靠 S42） | 必须避让对白（靠 S42 的 ducking，S34 只提供标记） |

---

## 1. 责任与明确不负责

### 1.1 本服务是唯一写者

| 事实 | 说明 |
|---|---|
| `MusicCue` / `MusicCueRevision` | 一条音乐意图：情绪、编制、时长需求、许可策略引用 |
| `MusicTake` | 一个已归档的候选曲目：`artifact_ref`、实测时长、节拍网格、调性、循环点、许可四元组 |
| `BeatGrid` | 一曲的节拍/小节/乐句网格（S34 检测并拥有） |
| `LoopPointSet` | 一曲的可循环区间与拼接点（S34 检测并拥有） |
| `CueSelection` | 导演/编辑对某条 cue 的选定（选定哪个 take、批准哪个 cut plan） |
| `CutPlan` | 某版 EDL 下，某 take 的进场/收尾方案（纯派生，可重算） |
| `LicenseBinding` | 本 take 使用的许可四元组快照与归属信息 |
| `MusicGap` | 缺口：哪条 cue 缺音乐、为什么、谁处理 |
| `MusicCheckpoint` | 每 `(scene_lineage_id, cue_index)` 的当前基线 take 指针 |

### 1.2 本服务明确不写

| 事实 | 权威 | S34 的正确做法 |
|---|---|---|
| 分镜、情绪曲线、场景定义 | **S06 / S07** | 只存 `scene_ref` / `beat_ref` + 意图 |
| 音乐在时间轴上的权威位置 | **S41** | 只算派生 `CutPlan`；不写回 EDL |
| 视频/音频 bytes | **S28** | 只存 `artifact_ref` |
| 混音、增益、ducking、响度 | **S42** | 完全不涉及 |
| `stem_bgm` 的最终形态 | **S42** | 只提供候选与切点 |
| 最终导出与发行 | **S43** | 不涉及 |
| 许可条款的裁定 | **S02** | 解析与记录，不自行解释 |
| 素材库的授权台账 | **S02 / 外部版权系统** | 只用 `rights_policy_ref` 约束 |
| 音频 QC | **S37** | 不自造质量评分 |
| 接受指针 / epoch / fence | **S08** | 只发事件 |
| 金额 / permit / attempt | **S23 / S22 / S26** | 只持引用 |
| 修复决策与上限 | **S39** | 只执行被指派的那一格 |
| 人工裁决 | **S40** | 不写 |

### 1.3 本服务不直连供应商（强制条款）

**本服务不直连供应商。** 不持有凭证、不出供应商域名、不配置供应商 SDK。

```
S34 组 GenerationCommand(modality=music)
  → POST /generation-jobs           (S26)
      → S23 reserve（按 quote_upper_bound_micro）
      → S22 permit
      → SubmissionIntent + Outbox
      → S27 claim dispatch grant → 付费提交
  → S26 观测 → S28 归档 → production.music.ready 回 S34
```

强制手段与 [S29 §1.3](S29-image-production.md) 相同（凭证隔离 / `require_dispatch_grant=true` 编译期常量 / 唯一付费入口）。

**S34 不参与准入 Saga 的内部步骤。**

**S34 的特殊风险一：一个 job 生成 N 个候选，成本是 N 倍。**

音乐生成的常见形态是"给我 3 个版本"，而 `generation-command` 没有 `candidate_count` 字段（§12 C2）。若 S34 自己循环发 3 次 `POST /generation-jobs`，会得到 3 个独立 job、3 次准入、3 次 reserve——**预算会被这一条 cue 吃掉三份**，而 `quote_upper_bound_micro` 只覆盖一份。

所以候选数必须进命令、进报价，或者 S34 必须显式地按候选数把 `quote_upper_bound_micro` 乘以 N 后写进 budget 请求。后者的风险是 S23 不理解"为什么这个 quote 要乘 3"。见 §4.7 与 §12 C2。

**S34 的特殊风险二：授权素材检索。**

`service.yaml` `runtime.scaling` 写的是"音乐生成**和授权素材检索**独立配额"。这意味着 S34 有两条付费路径：生成，以及从授权曲库检索/授权既有曲目。

检索同样是一次计费外部调用，且**返回的许可可能立即不可用**（曲目在检索后被下架）。它同样必须走 S26，`modality` 需要 `asset_search`（契约没有，同 [S33 §12 C2](S33-sound-effects.md)）。

**S34 的特殊风险三：音乐是唯一会被"发行"的音频。**

音效不需要在片尾署名，配音有配音演员合同，而 **BGM 必须在发行物里申报**——曲名、作曲、出版方、ISRC/ISWC、使用时长、使用方式（同步权）。这条信息由 S34 的 `LicenseBinding` 提供，由 S43 汇总进交付清单。

若 S34 只存一个 `rights_policy_ref` 而不存归属明细，**发行时无法生成权利申报清单**——这是法律风险，不是技术债。§4.4 展开。

### 1.4 动作归属（对照 S35 动作族）

音乐在 `action-families.v2.json` 的 `closed_type_set` 里**同样没有自己的 `defect_type`**，与 S33 相同的三个落点：

| 缺陷的表现 | `defect_type` | 族内动作链 | S34 的角色 |
|---|---|---|---|
| BGM 盖住对白 | `audio_mix` | `remix → regenerate_video` | **不参与**（S42 的 ducking/remix） |
| BGM 音量不稳 / 忽大忽小 | `audio_mix` | `remix → regenerate_video` | 不参与 |
| 曲子本身坏了 / 容器异常 | `technical` | `reingest → transcode → remix → temporal_edit → regenerate_video` | 不参与 |
| 音乐与画面切换错位 | `temporal` | `temporal_edit → regenerate_video` | 不参与（S41 的 EDL / S42 的 remix） |
| 音乐情绪不对（听着不该是这个调） | **无对应 defect_type** | — | **不是自动可修的问题**（§1.4 末） |

**结论与 S33 相同：S34 不是任何动作族的执行者。**

| 动作 | 执行者 | S34 的角色 |
|---|---|---|
| `remix` | **S42** | 不参与。原样交素材 |
| `regenerate_video` | **S30** | `audio_mix` 族第二步 |
| `reingest` / `transcode` | S28 / S42 | 不参与 |
| `temporal_edit` | S42 / S30 | 不参与 |
| `change_spec` | S06 / S07 | 不参与（改情绪是创作决定） |
| `human_review` | S40 | 不参与 |

**音乐比音效多一条必须写明的排除：**

**"BGM 情绪不对"不是任何 `defect_type`，也不该被自动修。** 它是创作判断。系统能做的是"给导演换一个候选"——这是 `CueSelection` 的变更，不是修复动作。

若把它塞进 `audio_mix`（"听着不对就 remix"），会导致一个荒谬的循环：remix 十次，曲子还是那一首，情绪还是不对，钱花了，问题没解决。

### 1.5 与 S42 的分工（本节是全文最重要的边界）

docs/11 的修复表写"BGM 盖对白 | ducking/重混音"。这条把答案指向 S42。

但"不遮盖对白"这句话出现在 `service.yaml` 的 `acceptance` 里：**"不遮盖对白并可追溯许可"**。所以在职责上它是 S34 的验收项。

两者的正确划分：

| 事项 | 谁做 | 为什么 |
|---|---|---|
| 生成时让曲子在"对白密集段"更轻（编曲层面） | **S34**（作为生成请求的一部分，若渠道支持） | 这是创作意图 |
| 提供"对白活跃区间"给混音 | **S34**（作为 metadata 透传） | S34 知道哪些区间该轻 |
| 实际的增益、ducking、侧链压缩 | **S42** | 混音是 S42 的唯一权威 |
| 测量成品是否遮盖对白 | **S37**（音频 QC） | 测量属于 QC |
| 判定"是否可接受" | **S40 / S43** | 主观与合规 |

**S34 不做的具体事项：** 不计算 ducking 曲线、不预设增益、不提供"已处理"版本的 BGM、不测量对白清晰度。

**S34 做的事：** 在 `MusicTake` 上标注 `dialogue_density_hint`（该区间的对白活跃度，来自 S37 的说话人分段或 S06 的台词密度），让 S42 在做 ducking 时有更好的输入。这是一个**提示**，不是决策。

§4.5 展开为什么这个划分是唯一可行的。

---

## 2. 实体与关键区分

### 2.1 对象图

```
MusicCueRevision      一条音乐意图的不可变版本（情绪 + 编制 + 时长需求 + 许可策略）
      │  N
      ▼
MusicJob              一次生成意图（绑定一个 CueRevision）
      │  1:N  ← 与 S33 最大的不同：一个 job 多个候选
      ▼
MusicTake             一个已归档候选（artifact_ref + BeatGrid + LoopPointSet + LicenseBinding）
      │  N:1
      ▼
CueSelection          某条 cue 选定哪个 take（导演/编辑的决定，S34 只记录）
      │
      ▼
MusicCheckpoint       每 (scene_lineage_id, cue_index) 的当前基线

CutPlan               旁挂：选定 take 在某版 EDL 上的进场/收尾（纯派生，可重算）
```

**`CutPlan` 与 S33 的 `CuePlacement` 同构但不同。** 相同之处：都是纯函数、都可随时重算、都不权威。不同之处：

- `CuePlacement` 的决断是"裁/拉/循环/放不下"，输出是**一段连续区间**。
- `CutPlan` 的决断是"从第几小节进、到第几小节收、中间是否拼接"，输出是**一个或多个区间 + 拼接方式**。

音乐可以被切成多段（副歌用在两个地方），音效通常不。所以 `CutPlan` 是一组 `Segments`，不是一对时间戳。

### 2.2 `MusicCue` 与 `MusicCueRevision`

与 S33 同样的理由：意图会被改（导演说"这一段不要太悲"），需要版本化。

但音乐的意图维度比音效多：

| 字段 | 作用 | 进 content 指纹？ |
|---|---|---|
| `mood` | 情绪标签 + 强度（闭集，§2.3） | 是 |
| `instrumentation` | 编制（闭集：orchestral / chamber / synth / hybrid / solo_piano / …） | 是 |
| `tempo_hint_bpm` | BPM 提示（区间） | 是 |
| `key_hint` | 调性提示（可选） | 是 |
| `dialogue_density_hint` | 对白活跃度 | **否**（不是内容） |
| `duration_seconds` | 时长需求 | **否**（时间线约束） |
| `rights_policy_ref` | 许可策略 | **否**（可用性约束） |

### 2.3 `mood` 必须是闭集，且带强度

`music.input.music_intent` 是自由字符串（同音效的 `cue_intent`），`musical_target` 之类字段在契约里没有。

**为什么必须闭集：**

1. **`strategy_class` 归一需要稳定维度。** 自由文本无法归一。
2. **候选比较需要可比较的轴。** 导演挑候选时，系统要能说"这三个版本的 `mood` 都是 `melancholic:0.7`，区别在编制"。
3. **情绪曲线一致性检查需要它。** 一场戏从 `tense` 走到 `cathartic`，若中间某个 cue 是 `joyful`，这是一个可自动发现的不连续——但只有在闭集上才能发现。

**建议的闭集**（S34 内部定义，契约里没有，§12 C1）：

```
mood ∈ {tender, melancholic, hopeful, tense, ominous, triumphant,
        playful, somber, awe, urgency, dissolution, neutral}
mood_intensity ∈ [0.0, 1.0]
```

**为什么不从 `music_intent` 猜：** 猜错会导致生成方向完全反（把"压抑"读成"庄严"），而且不可复现。

### 2.4 `BeatGrid`：为什么 S34 要自己算

一个曲目有节拍。节拍不是 S28 会探测的东西（S28 只管 bytes 完整性），也不是 S25 会的。它是音乐的结构信息。

**`BeatGrid` 的内容：**

```
bpm           实际 BPM（由 onset detection 得到，可能与请求的 tempo_hint 差 2-3 BPM）
meter         拍号（4/4、3/4、6/8…）
downbeat_ms[] 每个小节的第一拍时间戳（毫秒）
phrase_ms[]   乐句边界（通常每 4 或 8 小节）
confidence    检测置信度
```

**为什么 BPM 实测值可能与请求值不同：** 生成模型接受"90 BPM"的提示，输出的可能是 88.4 BPM。若 S34 相信请求值，切点会逐小节漂移——到第 16 小节时已经偏了半拍。

**所以 `CutPlan` 必须用实测 `BeatGrid`，不能用邀请值。** 这是 §4.3 的基础。

**`confidence` 的处理：** 若低于阈值（默认 0.85），标 `beat_grid_confidence='low'`，此时 `bar_trim` 不允许，只允许 `phrase_trim` 或 `loop_stitch`——因为小节线不可信时，按小节裁会切在半拍上。

### 2.5 `LoopPointSet`：可循环性是 S34 的产出，不是输入

音效有 `loopable_required`（S33 §4.7），音乐不同：**音乐总是有潜在的循环可能**（副歌可以再来一遍），但哪些区间能无缝循环要看实际音频。

`LoopPointSet` 记录：

```
loop_ranges[]     {in_ms, out_ms, crossfade_ms, measured_seam_score}
```

`measured_seam_score` 是首尾拼接处的样本连续性度量（越小越好）。

**为什么需要：** §4.3 的 `loop_stitch` 是"音乐变长"的唯一不花钱手段。若素材没有可用的循环区间，就只能重新生成。所以循环点是 S34 必须探测并拥有的核心事实。

### 2.6 许可为什么是四元组

一首音乐的"能不能用"由四个**互相独立**的权利组成：作曲权（composition）、录音权（master）、表演权（performance）、同步权（sync）。四者可以分别授权、分别过期、分别被撤销。

一首 AI 生成的曲子看起来"没有版权问题"——但如果模型在有版权的数据上训练，生成物可能与某个已有作品实质相似。这由 S02 的策略处理（训练数据合规声明），S34 只记录该声明。

所以 `LicenseBinding` 不是 `{policy_ref}` 而是四元组，每个维度独立带状态、持有方、依据与期限。**"可追溯许可"就是这四元组可查**，发行时 S43 拿它生成申报清单。§4.4 展开。

---

## 3. 状态机

### 3.1 `MusicJob` 状态

```
CREATED → CHECKING_POLICY → PREPARING → SUBMITTED → WAITING_PROVIDER → INGESTING → ANALYZING → CANDIDATES_READY
                 ↘                ↘              ↘                 ↘
          POLICY_BLOCKED     REJECTED_LOCAL     FAILED        WAITING_UNKNOWN
                                                                   ↓
                                                        RECONCILED_CANDIDATES
                                                        RECONCILED_FAILED
                                                                   ↓
                                                              GAP_RECORDED
任意非终态 → CANCELED
```

**与 S33 的两个状态差异：**

1. **多一个 `ANALYZING`。** 归档后、候选就绪前，S34 必须算 `BeatGrid` 与 `LoopPointSet`。这是真实工作（onset detection、调性分析、循环点搜索），可能耗时数十秒。
2. **终态是 `CANDIDATES_READY` 而不是 `READY`。** 一个有 N 个候选的 job，`READY` 说不清"哪一个是基线"。

### 3.2 转移表

| 从 | 到 | 触发 | 前置条件 | 副作用 |
|---|---|---|---|---|
| — | `CREATED` | `POST /music-jobs` | 校验 + 幂等未命中 | 写 job；写/复用 `MusicCueRevision` |
| `CREATED` | `CHECKING_POLICY` | worker 领取 | — | 查 S02 |
| `CHECKING_POLICY` | `POLICY_BLOCKED` | 无可用许可四元组 | — | 写 `LicenseBinding`（部分）；记缺口 |
| `CHECKING_POLICY` | `REJECTED_LOCAL` | 编制非法 / 时长非法 / mood 非法 | — | 不花钱 |
| `CHECKING_POLICY` | `PREPARING` | 许可可行 | — | 取 `RouteDecision` |
| `PREPARING` | `REJECTED_LOCAL` | 能力不足（候选数 / 时长上限 / 编制不支持） | — | 不花钱 |
| `PREPARING` | `SUBMITTED` | S26 返回 202 | `quote_valid_to > now()` | 存 `provider_job_id` |
| `SUBMITTED` | `WAITING_PROVIDER` | S26 → `QUEUED`/`RUNNING` | — | — |
| `SUBMITTED` | `WAITING_UNKNOWN` | S26 → `SUBMISSION_UNKNOWN` | — | 冻结 |
| `WAITING_PROVIDER` | `INGESTING` | S26 → `PROVIDER_SUCCEEDED` | — | — |
| `WAITING_PROVIDER` | `FAILED` | S26 → `PROVIDER_FAILED` | 无重试预算 | — |
| `INGESTING` | `INGESTING` | S28 失败 / 未到 | 重试未尽 | 只重归档 |
| `INGESTING` | `ANALYZING` | 全部候选归档完成 | hash 一致、时长已知 | — |
| `INGESTING` | `FAILED` | hash 不符 / 不可解码 / 候选数不符 | — | — |
| `ANALYZING` | `CANDIDATES_READY` | `BeatGrid` + `LoopPointSet` 全部就绪 | 许可复查通过 | 写 N 个 `MusicTake`；CAS 检查点 |
| `ANALYZING` | `FAILED` | 节拍检测失败且无候选可用 | — | — |
| `ANALYZING` | `GAP_RECORDED` | 全部候选的节拍检测都低置信 + 时长不可用 | — | 记缺口 |
| `WAITING_UNKNOWN` | `RECONCILED_CANDIDATES` | S26 → `ARCHIVED` | 同上 | 同上 |
| `WAITING_UNKNOWN` | `RECONCILED_FAILED` | 确认未收单且放弃 | — | — |
| `WAITING_UNKNOWN` | `GAP_RECORDED` | 对账不可恢复 | — | 记缺口 |
| 任意非终态 | `CANCELED` | 显式取消 | — | 调 S26 cancel；不承诺退款 |

**终态**：`CANDIDATES_READY`、`POLICY_BLOCKED`、`GAP_RECORDED`、`REJECTED_LOCAL`、`FAILED`、`RECONCILED_FAILED`、`CANCELED`。

### 3.3 `CueSelection` 的状态

`CueSelection` 是独立于 job 的对象，因为**同一个 job 的候选会在不同时间被不同的人选择**，而且选择会被改。

| 状态 | 含义 |
|---|---|
| `pending` | 有候选，没人选 |
| `selected` | 选定了某个 take |
| `superseded` | 被新的选择取代 |
| `rejected_all` | 全部候选被否（需要新 job） |

**只有 `selected` 的 selection 才会让 `MusicCheckpoint` 前进。**

**为什么 `rejected_all` 必须是显式状态：** 导演说"三个版本都不要"。若不记录这个，系统会认为"还没选"，可能一直等下去或自动选第一个。显式拒绝才能触发"需要新 job"。

### 3.4 状态持久化

同 S29/S30/S33：本地事务 + `state_seq` 乐观并发 + 同事务 Outbox + 表驱动转移。

补充：**`MusicTake` 与 `BeatGrid` / `LoopPointSet` 必须在同一事务写入。** 一个 take 没有节拍网格，`CutPlan` 就无法计算——而这会表现为"音乐放不进去"，追溯到 S34 时已经是 `CANDIDATES_READY` 状态，看不出哪里出了问题。

---

## 4. 关键流程

### 4.1 正常路径

```
POST /music-jobs
  {command_id, request_hash, context,
   cue: {scene_lineage_id, cue_index, beat_ref?, mood, mood_intensity,
         instrumentation, tempo_hint, key_hint?, music_intent, cue_revision?,
         instrumental_only, dialogue_aware},
   duration_seconds, candidate_count, rights_policy_ref,
   route_policy_ref | route_decision_id, budget_scope_id, deadline}

校验（顺序固定）：context 与签名 tenant → 幂等键 → mood/mood_intensity →
instrumentation → duration → candidate_count ∈ [1,3] → rights_policy_ref 归属 →
scene/beat 归属 → deadline

CHECKING_POLICY：向 S02 解析许可四元组；任一维度不可用 → POLICY_BLOCKED + 缺口
PREPARING：RouteDecision 能力位（多候选 / 无人声 / 对白避让）；不支持即
           REJECTED_LOCAL(CAPABILITY_MISSING)，不降级
SUBMITTED → WAITING_PROVIDER → INGESTING（N 个候选并行归档）
ANALYZING：每候选 onset detection → BeatGrid；循环点搜索 → LoopPointSet；
           调性检测；许可复查
CANDIDATES_READY：写 N 个 MusicTake；candidate_count==1 时自动选定，否则 pending；
                  outbox production.music.ready
```

### 4.2 "变时长重算切点"的三层含义

`service.yaml` `recovery`："**变时长重算切点；不能用未获许可曲目填补**"。前半句容易被读成"重新计算音乐的时间位置"，实际有三层，**全都不花钱**：

| 层 | 内容 | 例子 |
|---|---|---|
| 1. 换收尾小节 | 重新选切入/切出的小节线 | 42 秒 → 39 秒：曲子 48 秒 / 90 BPM，一小节 2.67 秒；从"第 15 小节末收（40.0 秒）"改成"第 14 小节末收（37.3 秒）"，尾部留 1.7 秒或淡出 |
| 2. 换是否拼接 | 决定是否 `loop_stitch` | 39 秒比整小节短一点，若尾部留空不合适，可从副歌处跳接把 48 秒剪成 38.5 秒，但**拼接点必须在拍上** |
| 3. 换段落映射 | 重新决定哪一段对应哪个画面段落 | EDL 合并两个场景后，原来的两个 cue 可能并成一个，或需重新分配 |

**这是换小节线，不是拉伸。** 曲子还是原速。只有 §4.3 的第 5 步（重新生成）才花钱，而那是最后手段。

### 4.3 `CutPlan` 算法

```
cutplan(take, cue_revision, edl_revision) → CutPlan | Unplaceable

CutPlan = {
  segments: [
    { output_start_ms, output_end_ms, source_in_ms, source_out_ms }
  ],
  transition: 'none' | 'crossfade' | 'loop_stitch' | 'hard_cut',
  fade_in_ms, fade_out_ms,
  end_strategy: 'bar' | 'phrase' | 'loop' | 'hard',
  confidence: number
}
```

**判定顺序（严格按此顺序，不可跳过）：**

| # | 条件 | 策略 | 是否花钱 | 风险 |
|---|---|---|---|---|
| 1 | `required ≤ take.natural` 且落点可对齐小节线（容差 ±80ms） | `bar_trim` | 否 | 几乎无 |
| 2 | 否则落点可对齐乐句线（4/8 小节） | `phrase_trim` | 否 | 轻微，可能切断和声进行 |
| 3 | 否则可用 `LoopPointSet` 拼接出所需长度 | `loop_stitch` | 否 | 中，拼接点若不在拍上会露馅 |
| 4 | 否则 `take.natural < required` 但差值 < 1 小节 | `bar_extend`（尾音延长+淡出） | 否 | 中，尾音拖长 |
| 5 | 否则 | **`Unplaceable`** | — | 触发重生成或缺口 |

**第 1、2 条用实测 `BeatGrid`，容差按帧率换算。**

**`bar_trim` 的容差为什么是 ±80ms：** 90 BPM 下一拍是 667ms。80ms 是 1/8 拍的偏差，人耳在这个偏差内听不出"没对齐小节"。超过就明显。

**`loop_stitch` 的具体做法：**

```
1. 取 LoopPointSet 里 seam_score 最好的区间 [in_ms, out_ms]
2. 若要拼 2 段：段 A = [0, in_ms+tail]，段 B = [out_ms-head, out_ms]，交叉淡入淡出
3. 拼接点必须落在拍摄上（crossfade 窗口以拍为单位）
4. 输出 confidence = min(seam_score 归一, beat_confidence)
```

**`bar_extend` 是一个需要解释的策略：**

需求比素材长不到一小节。做法是让最后一个和弦/尾音自然延长（不改速度，只在尾部加混响尾巴或让持续音多延一小节）。

**这算不算"拉伸"？** 不算。拉伸改变的是中间所有内容的时长；`bar_extend` 只延长结尾的衰减。音乐里这是常规做法（曲子结束时的延长音）。

**但它有前提：** 曲子最后必须是"可延续"的（持续和弦、渐弱、没有明确的终止拍）。若最后是一个干脆的终止（`final chord` + 静音），延长会听起来像"卡住了"。

所以 `bar_extend` 需要一个标记：`take.tail_kind ∈ {sustaining, hard_stop, fade_out}`。只有 `sustaining` 和 `fade_out` 允许 `bar_extend`。

### 4.4 许可四元组与"可追溯"

#### 4.4.1 为什么不能只有一个 `rights_policy_ref`

`music.input.rights_policy_ref` 是一个引用。它回答的是"这套策略下允许什么"。但它**不包含**：

- 这首具体的曲子由谁作曲
- 这个录音版本属于谁
- 署名的确切文本
- ISRC / ISWC 标识

**发行时这些是必填的。** 没有它们，S43 无法生成权利申报清单，平台审核会打回。

所以 S34 必须在 take 上存**四个独立的状态 + 归属明细**，而不是一个策略引用。

#### 4.4.2 四元组

| 维度 | 状态取值 | 说明 |
|---|---|---|
| `composition` | `licensed_generated` / `licensed_third_party` / `public_domain` / `unknown` | 作曲权。`licensed_generated` = 生成模型的训练合规声明已备案 |
| `master` | 同上 | 录音权。生成的音频的 master 归谁 |
| `performance` | `licensed` / `not_applicable` / `unknown` | 表演权。合成音频通常 `not_applicable` |
| `sync` | `licensed` / `restricted` / `expired` / `unknown` | 同步权。**有地区与期限** |

**任何一个维度是 `unknown`，整个 take 不可用于正式交付。** 这是 §5.3 的硬约束。

**为什么 `unknown` 不能当"可能可用"：** 法律上，无法证明有许可就是没有许可。把 `unknown` 当宽松默认，等于把合规风险转嫁给发行方。

#### 4.4.3 `sync` 的地区与期限

同步权几乎总是"某地区、某期限"。所以 `LicenseBinding.sync` 必须带：

```
territory: ["CN", "HK", "TW"]
expires_at: 2030-12-31
media: ["streaming", "broadcast", "theatrical"]
```

**S34 的复查必须在"每次 take 被使用前"检查这三项是否覆盖当前交付目标。** 但 S34 不知道交付目标——那是 S43 的。

**所以 S34 的做法：** 把这三个字段完整暴露在 `GET /music-cues/{id}` 与 `production.music.ready` 里，让 S43 自己判断。S34 只检查"当前是否已经到期"（`expires_at < now()`），不去判断"这块地区能不能用"。

**为什么这样分：** 地区覆盖是发行决策，S34 不了解发行计划。它只负责"这个许可现在是什么状态"和"什么时候过期"。

#### 4.4.4 复查时点

| # | 时点 | 目的 | 失败时 |
|---|---|---|---|
| 1 | `CHECKING_POLICY` | 确认存在可用许可四元组 | `POLICY_BLOCKED` + 缺口；不花钱 |
| 2 | `ANALYZING`（归档后） | 确认这个 take 的许可仍有效 | take 标 `void`；不登记候选 |
| 3 | **每次 `GET` / `ready` 前** | 确认现在仍有效 | take 标 `void`；`CutPlan` 返回 `Unplaceable` |
| 4 | 收到 S02 策略变更事件 | 批量重查受影响 take | 标 void + 发 `music.take.rights-lapsed` |

**与 S33 一样，S34 的 `GET` 是"有收紧副作用"的端点。** 只收紧，永不放松。

### 4.5 "不遮盖对白"到底谁做

这是最容易出现职责真空的一条。

**三种可能的实现，逐一看：**

| 方案 | 做法 | 问题 |
|---|---|---|
| A. S34 在生成时就降低 BGM 音量 | 请求里带"更轻"的提示 | 生成模型不精确控制音量；且"轻"是相对对白的，混音前不知道对白多响 |
| B. S34 输出"已 ducking"的 BGM 版本 | S34 自己做增益包络 | **越界**：混音是 S42 的唯一权威，两个服务都改增益会互相覆盖 |
| C. S34 提供对白活跃度提示，S42 执行 ducking | S34 标 `dialogue_density_hint`，S42 用侧链压缩 | 正确 |

**S34 采用 C。**

**为什么 A 不行：** 混音的响度关系是全局的（对白 -23 LUFS，BGM 在 ducking 后 -30 LUFS）。S34 不知道对白最终多响，所以"降多少"无从决定。它只能大概"生成得安静一点"，这在某些段落会变成"听不见配乐"。

**为什么 B 严格不行：**

1. **S42 会再压一遍。** S42 的 `sidechaincompress` 以对白为 key。若 S34 已经压过，S42 再压，结果是双重压缩（对白时 BGM 几乎消失）。
2. **两个权威。** 增益包络是 `MixPlan` 的一部分，`MixPlan` 属于 S41/S42。S34 产出一个"带增益的 BGM"，等于把混音的一部分偷到上游。
3. **不可追溯。** 出问题时说不清是 S34 压的太多还是 S42 压的太多。

**方案 C 的具体契约：**

`MusicTake.dialogue_density_hint` 是可选的 `Array<{start_ms, end_ms, level}>`，`level ∈ {sparse, normal, dense}`。来源只能是上游（S37 的说话人分段，或 S37 缺席时 S06 的台词密度）——S34 不自己推断。

**这是签名透传字段**：S34 收到就存、就透出，不解读；S42 消费它。上游没给时标 `unknown`，S42 走保守策略（全程 ducking）。

**S34 的验收项"不遮盖对白"因此分解为**：S34 保证 `dialogue_density_hint` 透传不断、且不产出"已压过"的版本（避免与 S42 双重压缩）；S42 执行 ducking；S37 测量成品；S43 交付前复核。

**S34 单独无法证明"不遮盖对白"，它只能证明"我没有让它更难实现"。** 验收文档必须写明这一点，否则会要求 S34 提交一个它拿不到的证据。

### 4.6 自动选定：只在 `candidate_count == 1` 时

若只生成一个候选，S34 可以自动把它设为 `selected`。

**若 candidate_count > 1 且没有人在规定时间内选**（默认 24 小时，可配）：

- **不自动选第一个。**
- 状态保持 `pending`，`production.music.ready` 已经发出，`MusicGap` 记 `awaiting_selection`（`kind='awaiting_selection'`）。
- 超时后告警，但不阻塞其他 cue。

**为什么不自动选：** "哪一个配乐更好"是创作决定。自动选第一个会把一个随机的生成结果变成成片的配乐。这正是 [S41 §4.2](S41-editorial.md) 说的"剪辑是创作决定，不能替导演决定"的同类问题——选曲同样是创作决定。

**唯一的例外：** `candidate_count == 1` 时，"选它"没有选择余地，属机械动作，可自动。

### 4.7 多候选的成本问题

`generation-command` 没有 `candidate_count`。三种实现路径：

| 路径 | 做法 | 问题 |
|---|---|---|
| A | 发 N 次独立 `POST /generation-jobs` | N 倍准入、N 倍引号；预算口径不清 |
| B | 一个 job 带 `candidate_count` | **契约不支持**（`additionalProperties: false`） |
| C | 一个 job，provider 侧多候选（若支持） | 依赖渠道能力；报价可能按 N 计 |

**S34 的当前实现取 A，但要求显式记账：**

```
for i in 1..candidate_count:
    job = POST /generation-jobs {
      ...,
      generation_command: { modality: music, input: {...} },
      candidate_index: i        // 放进 context 或 metadata，不进 input
    }
```

**关键约束：**

1. **每个子 job 有独立的 `idempotency_key`，但共享一个 `selection_group_id`。**
2. **`MusicJob` 是逻辑对象，持有 N 个 provider job。** 所以 `music_job` 表存 N 行 `provider_job_id`，共享 `music_job_id`。
3. **预算按 N × quote 计。** 在 `PREPARING` 阶段就检查 `N × quote_upper_bound_micro ≤ budget_remaining`，不然后面会中途失败留下半个候选集。

**第 3 条是最重要的：** 若只检查 1 份报价，前两个候选成功、第三个因预算失败，会得到"一个不完整的候选集"，而"3 个候选"这个承诺已经对导演做出了。要么全有要么全无——所以**要么在提交前预留 N 份，要么 `candidate_count` 就是 1**。

### 4.8 失败矩阵

| # | 场景 | 检测点 | S34 动作 | 状态 | 花钱 | 计数 |
|---|---|---|---|---|---|---|
| F1 | `music_intent` 空 | 同步校验 | `422 EMPTY_MUSIC_INTENT` | — | 否 | 否 |
| F2 | `mood` 不在闭集 | 同步校验 | `422 INVALID_MOOD` | — | 否 | 否 |
| F3 | `instrumentation` 不在闭集 | 同步校验 | `422 INVALID_INSTRUMENTATION` | — | 否 | 否 |
| F4 | `duration_seconds <= 0` / 超上限 | 同步校验 | `422 DURATION_OUT_OF_RANGE` | — | 否 | 否 |
| F5 | `candidate_count` 越界 | 同步校验 | `422 CANDIDATE_COUNT_OUT_OF_RANGE` | — | 否 | 否 |
| F6 | `N × quote > budget` | `PREPARING` | `REJECTED_LOCAL(BUDGET_INSUFFICIENT_FOR_CANDIDATES)`，**不降级候选数** | `REJECTED_LOCAL` | 否 | 否 |
| F7 | 许可四元组任一 `unknown` | `CHECKING_POLICY` | `POLICY_BLOCKED` + 缺口 | `POLICY_BLOCKED` | 否 | 否 |
| F8 | 渠道不支持多候选 | `PREPARING` | `REJECTED_LOCAL(CAPABILITY_MISSING)`，**不降级为 1** | `REJECTED_LOCAL` | 否 | 否 |
| F9 | 渠道不支持 `instrumental_only` | `PREPARING` | `REJECTED_LOCAL(CAPABILITY_MISSING)`，**不降级** | `REJECTED_LOCAL` | 否 | 否 |
| F10 | 部分候选成功、部分失败 | `INGESTING` | 全部作废（不留半个候选集）→ `FAILED(PARTIAL_CANDIDATES)` | `FAILED` | 是 | 是 |
| F11 | 候选数少于请求 | `INGESTING` | 同 F10 | `FAILED` | 是 | 是 |
| F12 | hash 不符 | `INGESTING` | `FAILED(HASH_MISMATCH)` | `FAILED` | 是 | 是 |
| F13 | 归档 URL 过期 | `INGESTING` | 要新 URL，**不重新生成** | `INGESTING` | 否 | 否 |
| F14 | 节拍检测全部低置信 | `ANALYZING` | `GAP_RECORDED(beat_unanalyzable)` | `GAP_RECORDED` | 是 | 是 |
| F15 | 部分候选节拍检测失败 | `ANALYZING` | 该候选不登记，其余正常 | `CANDIDATES_READY` | 是 | 是 |
| F16 | 无限可循环区间 | `ANALYZING` | `LoopPointSet` 为空，`loop_stitch` 不可用 | 正常 | 否 | 否 |
| F17 | 许可在归档后失效 | `ANALYZING` | take 标 void + 缺口 `rights_lapsed` | `GAP_RECORDED` | 是 | 是 |
| F18 | 时长需求无法用任何 cut 策略满足 | cutplan | `Unplaceable`，触发重生成或缺口 | — | 视情况 | 否 |
| F19 | `bar_extend` 但 `tail_kind='hard_stop'` | cutplan | 跳过 `bar_extend`，走下一策略 | — | 否 | 否 |
| F20 | 超过 24h 无人选择 | 定时检查 | 缺口 `awaiting_selection` | 正常 | 否 | 否 |
| F21 | 全部候选被否 | 选择 | `CueSelection.rejected_all` + 缺口 `all_candidates_rejected` | — | 否 | 否 |
| F22 | 预算不足 | S26 事件 | `FAILED(BUDGET)`，不缩短时长 | `FAILED` | 否 | 是 |
| F23 | 配额拒绝 | S26 事件 | `FAILED(QUOTA)` | `FAILED` | 是 | 是 |
| F24 | 账户撤销 | S26 事件 | `FAILED(ACCOUNT_UNAUTHORIZED)`，不换渠道 | `FAILED` | 是 | 是 |
| F25 | `SUBMISSION_UNKNOWN` | S26 事件 | 冻结 | `WAITING_UNKNOWN` | 保留额度 | 是 |
| F26 | UNKNOWN 对账失败 | S26 事件 | `GAP_RECORDED(submission_unrecoverable)` | `GAP_RECORDED` | 是 | 是 |
| F27 | 同 `command_id` 异 body | 幂等 | `409 IDEMPOTENCY_CONFLICT` | — | 否 | 否 |
| F28 | 跨租户 cue / take / policy | 校验 | `404` | — | 否 | 否 |
| F29 | 数据驻留禁止 | `PREPARING` | `REJECTED_LOCAL(RESIDENCY_FORBIDDEN)` | `REJECTED_LOCAL` | 否 | 否 |
| F30 | 死线已过 | 同步校验 | `422 DEADLINE_PASSED` | — | 否 | 否 |

---

## 5. 数据模型

### 5.1 表清单

| 表 | 作用 | 可变性 |
|---|---|---|
| `music_cue_revision` | 音乐意图版本 | 不可变 |
| `license_binding` | 许可四元组快照 | 不可变（新增新版本） |
| `music_job` | 生成意图（一个逻辑 job 可含 N 个候选） | 状态可变 |
| `music_job_candidate` | 逻辑 job 与 provider job 的对应 | 不可变 |
| `music_take` | 已归档候选 | `rights_state` / `state` 可变 |
| `beat_grid` | 节拍网格 | 不可变（重新分析 = 新行） |
| `loop_point_set` | 循环点 | 不可变 |
| `cue_selection` | 导演选定 | 状态可变 |
| `music_checkpoint` | 生产基线指针 | 可变（CAS） |
| `music_gap` | 缺口记录 | 状态可变 |
| `cut_plan_cache` | CutPlan 派生缓存 | **可丢弃、可重算**（非权威） |
| `music_intent_ledger` | 生产意图台账 | 只追加 |
| `job_transition` | 状态转移历史 | 只追加 |
| `command_receipt` | 命令幂等回执 | 只追加 |
| `outbox` | 事件出站 | 只追加 |
| `inbox` | 事件入站去重 | 只追加 |

### 5.2 核心 DDL

```sql
CREATE SCHEMA IF NOT EXISTS music;

-- ---------- 音乐意图版本（不可变） ----------
CREATE TABLE music.music_cue_revision (
  tenant_id        text NOT NULL,
  cue_id           text NOT NULL,
  cue_revision     integer NOT NULL CHECK (cue_revision >= 1),
  scene_lineage_id text NOT NULL,
  cue_index        integer NOT NULL CHECK (cue_index >= 0),
  beat_ref         text,
  mood             text NOT NULL CHECK (mood IN (
    'tender','melancholic','hopeful','tense','ominous','triumphant',
    'playful','somber','awe','urgency','dissolution','neutral')),
  mood_intensity   numeric(3,2) NOT NULL CHECK (mood_intensity >= 0 AND mood_intensity <= 1),
  instrumentation  text NOT NULL CHECK (instrumentation IN (
    'orchestral','chamber','synth','hybrid','solo_piano','solo_strings',
    'band','percussive','ambient')),
  tempo_hint_low_bpm  integer CHECK (tempo_hint_low_bpm IS NULL OR tempo_hint_low_bpm >= 20),
  tempo_hint_high_bpm integer CHECK (tempo_hint_high_bpm IS NULL OR tempo_hint_high_bpm >= 20),
  key_hint         text,
  music_intent     text NOT NULL,
  intent_hash      char(64) NOT NULL,
  instrumental_only boolean NOT NULL DEFAULT true,
  dialogue_aware   boolean NOT NULL DEFAULT true,
  source_ref       jsonb NOT NULL,          -- {service_id:'S06'|'S07', id, revision}
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, cue_id, cue_revision),
  CONSTRAINT mcr_intent_not_blank CHECK (btrim(music_intent) <> ''),
  CONSTRAINT mcr_hash_len CHECK (length(intent_hash) = 64),
  CONSTRAINT mcr_tempo_order CHECK (
    tempo_hint_low_bpm IS NULL OR tempo_hint_high_bpm IS NULL
    OR tempo_hint_low_bpm <= tempo_hint_high_bpm),
  CONSTRAINT mcr_source_known CHECK ((source_ref->>'service_id') IN ('S06','S07'))
);
CREATE UNIQUE INDEX mcr_intent_uniq
  ON music.music_cue_revision (tenant_id, cue_id, intent_hash);

-- ---------- 许可四元组快照（不可变） ----------
CREATE TABLE music.license_binding (
  license_binding_id text PRIMARY KEY,
  tenant_id          text NOT NULL,
  policy_ref         jsonb NOT NULL,

  -- 作曲权
  composition_status text NOT NULL CHECK (composition_status IN
    ('licensed_generated','licensed_third_party','public_domain','unknown')),
  composition_holder text,
  -- 录音权
  master_status      text NOT NULL CHECK (master_status IN
    ('licensed_generated','licensed_third_party','public_domain','unknown')),
  master_holder      text,
  -- 表演权
  performance_status text NOT NULL CHECK (performance_status IN
    ('licensed','not_applicable','unknown')),
  -- 同步权
  sync_status        text NOT NULL CHECK (sync_status IN
    ('licensed','restricted','expired','unknown')),
  sync_territories   text[] NOT NULL DEFAULT '{}',
  sync_media         text[] NOT NULL DEFAULT '{}',
  sync_expires_at    timestamptz,

  -- 发行申报所需
  work_title         text,
  isrc               text,
  iswc               text,
  attribution_text   text,
  training_basis_ref jsonb,                 -- 生成模型的训练合规声明

  resolved_at        timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT lb_ref_known CHECK ((policy_ref->>'service_id') = 'S02'),
  CONSTRAINT lb_hash_len CHECK (length(policy_ref->>'content_hash') = 64),
  -- 任一 unknown 的绑定不允许被 take 采用（§5.3）；这里只做形状校验
  CONSTRAINT lb_sync_has_territory CHECK (
    sync_status <> 'licensed' OR cardinality(sync_territories) > 0),
  CONSTRAINT lb_generated_has_basis CHECK (
    composition_status <> 'licensed_generated' AND master_status <> 'licensed_generated'
    OR training_basis_ref IS NOT NULL)
);

-- ---------- 生成意图 ----------
CREATE TABLE music.music_job (
  music_job_id       text PRIMARY KEY,
  tenant_id          text NOT NULL,
  project_id         text NOT NULL,
  series_id          text NOT NULL,
  episode_id         text NOT NULL,
  branch_id          text NOT NULL,

  cue_id             text NOT NULL,
  cue_revision       integer NOT NULL,
  scene_lineage_id   text NOT NULL,
  cue_index          integer NOT NULL,

  requested_duration_ms integer NOT NULL CHECK (requested_duration_ms > 0),
  candidate_count    integer NOT NULL CHECK (candidate_count BETWEEN 1 AND 3),
  candidate_index_base integer NOT NULL DEFAULT 0,

  license_binding_id text NOT NULL REFERENCES music.license_binding,

  -- ↓ 冻结：内容指纹不含时长（§4.4 / §8.1）
  content_fingerprint text NOT NULL,

  node_id            text NOT NULL,
  node_epoch_at_create integer NOT NULL,
  execution_fence    integer NOT NULL,

  request_hash       text NOT NULL,
  idempotency_key    text NOT NULL,
  variation_nonce    text NOT NULL,

  route_decision_ref jsonb,
  budget_scope_id    text NOT NULL,
  deadline           timestamptz NOT NULL,

  -- ↓ 逻辑 job 的汇总状态；provider job 明细在 music_job_candidate
  quote_upper_bound_micro_total bigint,

  state              text NOT NULL,
  state_seq          bigint NOT NULL DEFAULT 0,
  failure_code       text,
  failure_detail     jsonb,
  reconciled         boolean NOT NULL DEFAULT false,

  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  state_entered_at   timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT mj_state_check CHECK (state IN (
    'CREATED','CHECKING_POLICY','POLICY_BLOCKED','PREPARING','REJECTED_LOCAL',
    'SUBMITTED','WAITING_PROVIDER','WAITING_UNKNOWN','INGESTING','ANALYZING',
    'CANDIDATES_READY','FAILED','RECONCILED_CANDIDATES','RECONCILED_FAILED',
    'GAP_RECORDED','CANCELED')),
  CONSTRAINT mj_tenant_idem UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT mj_fingerprint_len CHECK (length(content_fingerprint) = 64)
);

CREATE UNIQUE INDEX music_job_open_uniq
  ON music.music_job
     (tenant_id, scene_lineage_id, cue_index, content_fingerprint, requested_duration_ms)
  WHERE state NOT IN ('CANDIDATES_READY','POLICY_BLOCKED','GAP_RECORDED',
                      'REJECTED_LOCAL','FAILED','RECONCILED_FAILED','CANCELED');

-- ---------- 逻辑 job ↔ provider job ----------
CREATE TABLE music.music_job_candidate (
  music_job_id    text NOT NULL REFERENCES music.music_job,
  candidate_index integer NOT NULL CHECK (candidate_index >= 0),
  provider_job_id text NOT NULL,
  attempt_id      text NOT NULL,
  quote_upper_bound_micro bigint,
  state           text NOT NULL CHECK (state IN
    ('SUBMITTED','WAITING','ARCHIVED','FAILED','UNKNOWN','CANCELED')),
  failure_code    text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (music_job_id, candidate_index),
  UNIQUE (provider_job_id)
);

-- ---------- 已归档候选 ----------
CREATE TABLE music.music_take (
  music_take_id      text PRIMARY KEY,
  tenant_id          text NOT NULL,
  music_job_id       text NOT NULL REFERENCES music.music_job,
  candidate_index    integer NOT NULL,
  cue_id             text NOT NULL,
  scene_lineage_id   text NOT NULL,
  cue_index          integer NOT NULL,

  artifact_ref       jsonb NOT NULL,        -- S28
  natural_duration_ms integer NOT NULL CHECK (natural_duration_ms > 0),
  sample_rate_hz     integer NOT NULL CHECK (sample_rate_hz >= 8000),
  channel_count      integer NOT NULL CHECK (channel_count >= 1),
  container_format   text NOT NULL,

  detect_bpm         numeric(6,2) CHECK (detect_bpm IS NULL OR detect_bpm > 0),
  detected_key       text,
  tail_kind          text NOT NULL CHECK (tail_kind IN
    ('sustaining','hard_stop','fade_out','unknown')),
  vocal_presence     boolean NOT NULL DEFAULT false,
  instrumental_required boolean NOT NULL,   -- 来自 cue_revision，冻结在 take 上

  content_fingerprint char(64) NOT NULL,

  license_binding_id text NOT NULL REFERENCES music.license_binding,
  rights_state       text NOT NULL CHECK (rights_state IN ('usable','void','unknown')),
  rights_checked_at  timestamptz,

  dialogue_density_hint jsonb,              -- 透传，不解读（§4.5）
  dialogue_density_source text CHECK (dialogue_density_source IN
    ('s37_segments','s06_script','provided','unknown')),

  provider_job_id    text NOT NULL,
  attempt_id         text NOT NULL,
  source_kind        text NOT NULL CHECK (source_kind IN ('generated','licensed_library')),

  state              text NOT NULL CHECK (state IN ('READY','SELECTED','REJECTED','SUPERSEDED')),
  created_at         timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT mt_artifact_is_s28 CHECK ((artifact_ref->>'service_id') = 'S28'),
  CONSTRAINT mt_hash_len CHECK (length(artifact_ref->>'content_hash') = 64),
  -- 声明无人声的 take 若探测到人声，不允许进入候选池（§4.8 F9 / §11 U7）
  CONSTRAINT mt_no_unwanted_vocal CHECK (
    NOT (instrumental_required AND vocal_presence))
);

CREATE UNIQUE INDEX mt_artifact_uniq
  ON music.music_take
     (tenant_id, (artifact_ref->>'id'), (artifact_ref->>'content_hash'));
CREATE INDEX mt_cue_idx
  ON music.music_take (tenant_id, scene_lineage_id, cue_index, created_at DESC);
CREATE INDEX mt_group_idx
  ON music.music_take (tenant_id, music_job_id, candidate_index);

-- ---------- 节拍网格（不可变） ----------
CREATE TABLE music.beat_grid (
  beat_grid_id    text PRIMARY KEY,
  tenant_id       text NOT NULL,
  music_take_id   text NOT NULL REFERENCES music.music_take,
  bpm             numeric(6,2) NOT NULL CHECK (bpm > 0),
  meter_num       integer NOT NULL CHECK (meter_num > 0),
  meter_den       integer NOT NULL CHECK (meter_den > 0),
  downbeat_ms     integer[] NOT NULL,
  phrase_ms       integer[] NOT NULL DEFAULT '{}',
  confidence      numeric(3,2) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  low_confidence  boolean NOT NULL DEFAULT false,
  detector_model  text NOT NULL,
  detector_version text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT bg_downbeats_ascending CHECK (
    array_length(downbeat_ms,1) > 0),
  CONSTRAINT bg_unique_take UNIQUE (music_take_id)
);
CREATE INDEX bg_lowconf_idx ON music.beat_grid (tenant_id) WHERE low_confidence;

-- ---------- 循环点（不可变） ----------
CREATE TABLE music.loop_point_set (
  loop_point_set_id text PRIMARY KEY,
  tenant_id         text NOT NULL,
  music_take_id     text NOT NULL REFERENCES music.music_take,
  ranges            jsonb NOT NULL,         -- [{in_ms,out_ms,crossfade_ms,seam_score}]
  best_seam_score   numeric(5,4),
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT lps_has_take UNIQUE (music_take_id),
  CONSTRAINT lps_ranges_array CHECK (jsonb_typeof(ranges) = 'array')
);

-- ---------- 选定 ----------
CREATE TABLE music.cue_selection (
  selection_id    text PRIMARY KEY,
  tenant_id       text NOT NULL,
  scene_lineage_id text NOT NULL,
  cue_index       integer NOT NULL,
  cue_id          text NOT NULL,
  cue_revision    integer NOT NULL,
  music_job_id    text NOT NULL REFERENCES music.music_job,
  music_take_id   text REFERENCES music.music_take,

  state           text NOT NULL CHECK (state IN
    ('pending','selected','superseded','rejected_all')),
  selected_by     text,
  selection_kind  text CHECK (selection_kind IN ('auto_single','human','fallback')),
  reason          text,

  created_at      timestamptz NOT NULL DEFAULT now(),
  decided_at      timestamptz,
  superseded_by   text REFERENCES music.cue_selection,

  CONSTRAINT ms_selected_has_take CHECK (
    state <> 'selected' OR music_take_id IS NOT NULL),
  CONSTRAINT ms_selected_has_decider CHECK (
    state <> 'selected' OR selected_by IS NOT NULL)
);
CREATE UNIQUE INDEX ms_current_idx
  ON music.cue_selection (tenant_id, scene_lineage_id, cue_index)
  WHERE state IN ('pending','selected');

-- ---------- 检查点 ----------
CREATE TABLE music.music_checkpoint (
  tenant_id       text NOT NULL,
  scene_lineage_id text NOT NULL,
  cue_index       integer NOT NULL,
  music_take_id   text NOT NULL REFERENCES music.music_take,
  selection_id    text NOT NULL REFERENCES music.cue_selection,
  checkpoint_seq  bigint NOT NULL,
  expected_revision integer NOT NULL DEFAULT 0,
  updated_by_job  text NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, scene_lineage_id, cue_index)
);

-- ---------- 缺口 ----------
CREATE TABLE music.music_gap (
  gap_id          uuid PRIMARY KEY,
  tenant_id       text NOT NULL,
  scene_lineage_id text NOT NULL,
  cue_index       integer NOT NULL,
  cue_id          text NOT NULL,
  kind            text NOT NULL CHECK (kind IN (
    'no_licensed_route','license_incomplete','provider_failed',
    'submission_unrecoverable','beat_unanalyzable','duration_mismatch',
    'rights_lapsed','artifact_unavailable','all_candidates_rejected',
    'awaiting_selection','not_instrumental')),
  detail          jsonb NOT NULL,
  source_job_id   text,
  state           text NOT NULL CHECK (state IN ('open','closed','waived')),
  closed_by_take_id text,
  waived_decision_id text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT mg_state_consistent CHECK (
    (state = 'open' AND closed_by_take_id IS NULL AND waived_decision_id IS NULL)
    OR (state = 'closed' AND closed_by_take_id IS NOT NULL)
    OR (state = 'waived' AND waived_decision_id IS NOT NULL))
);
CREATE INDEX mg_open_idx
  ON music.music_gap (tenant_id, scene_lineage_id, cue_index)
  WHERE state = 'open';
CREATE INDEX mg_episode_idx
  ON music.music_gap (tenant_id, cue_id) WHERE state = 'open';

-- ---------- CutPlan 缓存（非权威） ----------
CREATE TABLE music.cut_plan_cache (
  tenant_id       text NOT NULL,
  music_take_id   text NOT NULL,
  edl_revision    integer NOT NULL,
  requested_duration_ms integer NOT NULL,
  end_strategy    text NOT NULL CHECK (end_strategy IN
    ('bar','phrase','loop','bar_extend','hard','unplaceable')),
  segments        jsonb NOT NULL,
  transition      text,
  fade_in_ms      integer,
  fade_out_ms     integer,
  confidence      numeric(3,2),
  reason          text,
  computed_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, music_take_id, edl_revision, requested_duration_ms)
);
-- 这张表可以被 TRUNCATE，不影响任何正确性（§4.3）

-- ---------- 生产意图台账 ----------
CREATE TABLE music.music_intent_ledger (
  entry_id        uuid PRIMARY KEY,
  tenant_id       text NOT NULL,
  music_job_id    text NOT NULL,
  scene_lineage_id text NOT NULL,
  cue_index       integer NOT NULL,
  action          text NOT NULL CHECK (action IN
    ('generate','resynthesize_for_duration','fill_gap','licensed_retrieval')),
  candidate_count integer NOT NULL,
  run_id          text NOT NULL,
  node_epoch      integer NOT NULL,
  manifest_id     text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX mil_scene_idx
  ON music.music_intent_ledger (tenant_id, scene_lineage_id, created_at DESC);

-- ---------- 基础设施 ----------
CREATE TABLE music.job_transition (
  music_job_id text NOT NULL,
  seq          bigint NOT NULL,
  from_state   text,
  to_state     text NOT NULL,
  reason_code  text,
  detail       jsonb,
  at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (music_job_id, seq)
);

CREATE TABLE music.command_receipt (
  tenant_id     text NOT NULL,
  command_id    text NOT NULL,
  request_hash  char(64) NOT NULL,
  response_body jsonb NOT NULL,
  completed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);
CREATE UNIQUE INDEX mcr_hash_uniq
  ON music.command_receipt (tenant_id, command_id, request_hash);

CREATE TABLE music.outbox (
  outbox_id          uuid PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,             -- 见 §6.4 事件表
  aggregate_sequence bigint NOT NULL,
  event_type         text NOT NULL,
  partition_key      text NOT NULL,
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  published_at       timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx
  ON music.outbox (outbox_id) WHERE published_at IS NULL;

CREATE TABLE music.inbox (
  consumer     text NOT NULL,
  event_id     text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  PRIMARY KEY (consumer, event_id)
);
```

### 5.3 约束理由

| 约束 | 防的是什么 |
|---|---|
| `mj_tenant_idem` | E12 |
| `music_job_open_uniq` | E16；同内容同需求并发双提交 |
| `mj_fingerprint_len` | 指纹污染 |
| `mt_artifact_is_s28` | 供应商直链进 `artifact_ref` |
| `ms_selected_has_take` | "已选定"但没有 take |
| `ms_selected_has_decider` | **"已选定"但没人选过**——防自动选择伪装成人选 |
| `mg_state_consistent` | 缺口说闭合却没依据 |
| `lb_sync_has_territory` | `licensed` 但不知道在哪些地区可用 → 发行时才发现 |
| `lb_generated_has_basis` | 生成物声称 `licensed_generated` 但没有训练合规声明 |
| `bg_unique_take` | 一个 take 多个节拍网格 → `CutPlan` 不确定 |
| `lps_has_take` | 同上 |
| `mcr_tempo_order` | 区间上下界颠倒 |

**`ms_selected_has_decider` 值得展开：** `selection_kind='auto_single'` 的自动选定也必须写 `selected_by`（记 `system:auto_single`）。这样审计时能区分"导演选了 A"和"只有一个所以自动选了它"。若允许 `selected_by IS NULL`，事后无法区分"忘了记"和"自动选的"。

**`lb_*` 与 `unknown` 的关系：** DDL 允许 `unknown` 状态存在（因为快照要如实记录 S02 返回的东西），但 **`music_take` 不允许引用含 `unknown` 的 binding**。这条不能在 DDL 里用简单的 CHECK 表达（要查另一张表），所以用触发器或应用层校验，并在 §10 的 T34-14 里覆盖。

---

## 6. 对外契约

### 6.1 `POST /music-jobs`

```jsonc
{
  "command_id": "cmd-mus-0001",
  "request_hash": "sha256:…",
  "context": { /* execution-context.v2 */ },
  "cue": {
    "cue_id": "cue_ep3_sc7_bgm",
    "cue_revision": 1,
    "scene_lineage_id": "lin_ep3_sc7",
    "cue_index": 0,
    "beat_ref": "beat_sc7_turn",
    "mood": "melancholic",
    "mood_intensity": 0.7,
    "instrumentation": "solo_piano",
    "tempo_hint": { "low_bpm": 80, "high_bpm": 92 },
    "key_hint": "D minor",
    "music_intent": "独奏钢琴，缓慢的三连音左手分解和弦，右手段落稀疏，整体克制不煽情",
    "instrumental_only": true,
    "dialogue_aware": true
  },
  "duration_seconds": 39,
  "candidate_count": 3,
  "rights_policy_ref": { "service_id": "S02", "id": "pol_music_standard", "revision": 7, "content_hash": "…" },
  "route_policy_ref": { "service_id": "S25", "id": "routing_music", "revision": 2, "content_hash": "…" },
  "budget_scope_id": "budget_ep3",
  "deadline": "2026-11-06T02:00:00+08:00"
}
```

响应 `202`：

```jsonc
{
  "music_job_id": "mj_01J…",
  "state": "CREATED",
  "operation_id": "op_…",
  "content_fingerprint": "…",
  "candidate_count": 3,
  "requested_duration_ms": 39000,
  "quote_upper_bound_micro_total": 5400000
}
```

**`quote_upper_bound_micro_total` 必须返回。** 调用方要能看见"这一次 cue 会花三份的钱"，否则预算对话永远对不上（§4.7）。

### 6.2 `GET /music-cues/{id}`

```jsonc
{
  "cue_id": "cue_ep3_sc7_bgm",
  "cue_revision": 1,
  "scene_lineage_id": "lin_ep3_sc7",
  "cue_index": 0,
  "intent": {
    "mood": "melancholic",
    "mood_intensity": 0.7,
    "instrumentation": "solo_piano",
    "tempo_hint": { "low_bpm": 80, "high_bpm": 92 },
    "key_hint": "D minor",
    "intent_hash": "…",
    "instrumental_only": true
  },
  "selection": {
    "selection_id": "sel_01J…",
    "state": "selected",
    "music_take_id": "mt_01J…",
    "selected_by": "human:dop_042",
    "selection_kind": "human",
    "decided_at": "2026-11-05T14:02:00Z"
  },
  "candidates": [
    {
      "music_take_id": "mt_01J…",
      "artifact_ref": { "…": "…" },
      "natural_duration_ms": 47000,
      "detected_key": "D minor",
      "detect_bpm": 88.4,
      "meter": { "num": 4, "den": 4 },
      "tail_kind": "sustaining",
      "rights_state": "usable",
      "state": "SELECTED"
    },
    { "music_take_id": "mt_01K…", "state": "READY", "…": "…" },
    { "music_take_id": "mt_01M…", "state": "READY", "…": "…" }
  ],
  "license": {
    "composition": { "status": "licensed_generated", "holder": "synthetic",
                     "training_basis_ref": { "…": "…" } },
    "master":      { "status": "licensed_generated", "holder": "synthetic" },
    "performance": { "status": "not_applicable" },
    "sync":        { "status": "licensed", "territories": ["CN","HK","TW"],
                     "media": ["streaming","broadcast"], "expires_at": "2030-12-31" },
    "isrc": "…", "iswc": "…", "attribution_text": "…"
  },
  "cut_plan": {
    "edl_revision": 12,
    "requested_duration_ms": 39000,
    "end_strategy": "phrase",
    "segments": [
      { "output_start_ms": 0, "output_end_ms": 38400,
        "source_in_ms": 0, "source_out_ms": 38400 }
    ],
    "transition": "none",
    "fade_in_ms": 250, "fade_out_ms": 800,
    "confidence": 0.91
  },
  "gap": null
}
```

**`license` 在 cue 级别返回，而不是在候选级别。** 理由：同一 job 的候选共享同一次许可解析。若不同候选的许可确实不同（例如部分来自授权曲库），`candidates[]` 里各自带 `license_binding_ref`，调用方按需再查。

**`cut_plan` 必须接受 `?edl_revision=` 参数**，缺省用当前 EDL revision，响应里标明实际使用的 revision。

### 6.3 其他查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/music-cues?scene_lineage_id=` | 列表 |
| GET | `/music-cues?episode_id=&has_gap=true` | 一集的全部缺口（S41 `completeness` 输入） |
| GET | `/scenes/{scene_lineage_id}/music-checkpoint` | 该场景全部 cue 的基线（S42 主读口） |
| GET | `/music-takes/{id}` | 候选详情 + `beat_grid` + `loop_points` |
| GET | `/music-takes/{id}/license` | 完整许可四元组 + 申报字段（S43 用） |
| POST | `/music-cues/{id}/cut-plan:recompute?edl_revision=` | 强制重算并写缓存 |
| POST | `/music-selections` | 选定；`{selection_id, music_take_id, selected_by, reason}` |
| POST | `/music-selections/{id}:reject-all` | 全部否 → `rejected_all` + 缺口 |
| POST | `/music-gaps/{id}:waive` | 需 S40 裁决引用 |
| POST | `/music-jobs/{id}:cancel` | 取消 |
| POST | `/music-jobs/{id}:retry` | 仅 `{FAILED, RECONCILED_FAILED}`；其余 **409** |

**`GET /music-takes/{id}/license` 是 S43 的专用读口。** 它返回的是**发行申报所需的最小集**：`work_title`、`isrc`、`iswc`、四元组、`attribution_text`、`sync` 的地区/媒体/期限。

这条端点存在的原因是**权限分离**：S43 不需要看音乐意图（泄露剧情），只需要申报信息。

### 6.4 事件

**发布：**

| event_type | aggregate | 何时 |
|---|---|---|
| `production.music.ready` | `scene_lineage_id` | 候选就绪（`CANDIDATES_READY`） |
| `music.cue.selected`（建议新增） | `scene_lineage_id` | 选定 |
| `music.job.state-changed`（建议新增） | `music_job_id` | 每次转移 |
| `music.gap.recorded` / `.closed`（建议新增） | `scene_lineage_id` | 缺口变化 |
| `music.take.rights-lapsed`（建议新增） | `music_take_id` | take 变 void |

`production.music.ready` 的 `data`：

```jsonc
{
  "music_job_id": "mj_01J…",
  "cue_id": "cue_ep3_sc7_bgm",
  "cue_revision": 1,
  "scene_lineage_id": "lin_ep3_sc7",
  "cue_index": 0,
  "mood": "melancholic",
  "mood_intensity": 0.7,
  "instrumentation": "solo_piano",
  "requested_duration_ms": 39000,
  "candidates": [
    {
      "music_take_id": "mt_01J…",
      "artifact_ref": { "service_id": "S28", "id": "…", "revision": 1, "content_hash": "…" },
      "natural_duration_ms": 47000,
      "detect_bpm": 88.4,
      "meter": "4/4",
      "detected_key": "D minor",
      "tail_kind": "sustaining",
      "loop_available": true,
      "rights_state": "usable"
    }
  ],
  "selection_state": "pending",
  "content_fingerprint": "…",
  "gap_state": "none"
}
```

**不含**：音频 URL、`music_intent` 原文、`attribution_text`（可能含出版方内部信息）、ISRC/ISWC（走 `/music-takes/{id}/license`）、供应商信息、许可策略明细。

**`attribution_text` 为什么不进事件：** 它可能包含未公开的出版方关系或代理条款。事件流被多个消费者存档，不适合承载合同细节。

**消费：**

| 来源 | 事件 | 用途 |
|---|---|---|
| S26 | `generation.job.state-changed` | 驱动状态机（按 `provider_job_id` 归到 `music_job_candidate`） |
| S28 | `artifact.ingested` | 完成 `INGESTING → ANALYZING`；**需要 `natural_duration_ms`、采样率、通道数**（§12 C4） |
| S02 | `policy.revision.published` | 重新解析许可；可能使 take 变 void |
| S41 | `post.timeline.revision.created` | 只读。**只让 `cut_plan_cache` 失效**，不批量重算（§4.3） |
| S06 / S07 | `shot.spec.revised` / 情绪曲线变更 | cue 意图变更 → 新 revision |
| S37 | `qc.audio.report.published` | **只用于 `dialogue_density_hint` 的透传**（§4.5） |
| S40 | 选择裁决 | 驱动 `CueSelection` |
| S08 | `snapshot.manifest.committed` | 只读投影 |

**S34 不消费 `quality.*` 来自动重做。** 它没有对应的动作族。

---

## 7. 幂等与并发

### 7.1 幂等

| 层面 | 键 | 冲突 |
|---|---|---|
| 命令 | `tenant + command_id + request_hash` | 同体返回原响应；异体 `409` |
| 业务 | `(tenant, scene_lineage_id, cue_index, content_fingerprint, requested_duration_ms)` 未终结 | 返回已有 job |
| 候选子 job | `(music_job_id, candidate_index)` | 不重复提交 |
| 选定 | `(tenant, scene_lineage_id, cue_index)` 当前 selection | 返回已有 |
| 事件入站 | `(consumer, event_id)` | 丢弃重复 |

**业务幂等的键含 `requested_duration_ms`，不含 `candidate_count`。**

为什么：`candidate_count` 是"要几个选项"的偏好，不是内容需求。若第一次要 1 个、第二次要 3 个，内容相同——应该**复用已有的 1 个 + 补充 2 个**（一个新的 job，共享 `cue_id`），而不是拒绝或生成 3 个全新的。

见 §11 U2。

### 7.2 并发

| 场景 | 机制 |
|---|---|
| 同 cue 两个 selection 并发 | `ms_current_idx` 部分唯一索引；后到的 `409` 或 `superseded` |
| 同 scene 多 cue 并发 | 互不影响 |
| `CANDIDATES_READY` 与 `rights-lapsed` 并发 | **void 优先**：事务内重读 |
| EDL 变更与 `GET` 并发 | `GET` 现算，不读缓存 |
| 同一缺口被两个新 job 同时闭合 | `music_gap` 行锁 |
| 多候选并行归档 | 每个 `music_job_candidate` 独立；全部到齐才进 `ANALYZING` |

### 7.3 重启恢复

- `CHECKING_POLICY` 重启后重做。
- `PREPARING` 重启后重取 RouteDecision。
- `SUBMITTED` / `WAITING_PROVIDER` 重启后查 S26（按 N 个 `provider_job_id`）。
- `INGESTING` 重启后重新探测 `natural_duration_ms`。
- `ANALYZING` 重启后**重新分析**（`BeatGrid` / `LoopPointSet` 是幂等的纯计算，重做无害）。
- `WAITING_UNKNOWN` 重启后什么都不做。
- `CANDIDATES_READY` / `POLICY_BLOCKED` / `GAP_RECORDED` 重启后什么都不做（终态）。
- `cut_plan_cache` 不需要恢复。

---

## 8. 跨 Run 尝试计数

S34 不是任何动作族的执行者（§1.4），所以与 S33 相同：不参与 S39 的上限计数。但音乐的计数问题比音效多一层——**多候选**。

### 8.1 台账只记"真正付费的意图"，候选在明细里

`music_intent_ledger` 一行对应一个 `music_job`（一次意图），**不是**一行对应一个候选。

`candidate_count` 是一个字段。

**为什么：** 一次"生成 3 个候选"是一个决策，不是三个。若记三行，S39 或成本分析会看到"这个 cue 重做了 3 次"，而实际是"一次要了 3 个版本"——两个完全不同的事实。

**成本仍然按 3 份进 S23。** 台账记"意图数"，S23 记"钱"。两者不该一致。

### 8.2 时长变化触发的重生成必须记 `resynthesize_for_duration`

§4.3 的第 4 步（重新生成）是一个真实的花钱动作。它必须写 `action='resynthesize_for_duration'`，且带上 `detail.reason`（为什么所有不花钱的策略都不行）。

**这条是成本异常的主要来源：** 一集 20 条 cue，每条因剪辑抖动重生成一次，就是 20 次额外生成。若主体是 3 候选，就是 60 份。

**必须有指标盯住它**（§9 的 `s34_resynth_for_duration_total`）。

### 8.3 例子

| Run | 事件 | 台账 | 候选 | 花费 |
|---|---|---|---|---|
| Run 1 | 首轮 20 条 cue，每条 3 候选 | 20 行 `generate` | 60 | 60 份 |
| Run 2 | 剪辑微调，全部 `bar_trim` / `phrase_trim` | **0 行** | 0 | 0 |
| Run 3 | 1 条 cue 时长多 5 秒，无法 cut 满足 | 1 行 `resynthesize_for_duration` | 3 | 3 份 |
| Run 4 | 导演否掉 2 条 cue 的全部候选 | 2 行 `generate` | 6 | 6 份 |
| Run 5 | 2 条 cue 许可失效 | 0 行，2 缺口 | 0 | 0 |
| Run 6 | 填补 2 条缺口 | 2 行 `fill_gap` | 2 | 2 份 |

**Run 2 是最关键的一行：0 行台账、0 花费。** 剪辑变了，音乐位置变了，但没有一分钱。这是 §4.3 全部努力的目标。

---

## 9. 可观测

| 指标 | 类型 | 用途 | 告警 |
|---|---|---|---|
| `s34_jobs{state}` | gauge | 堆积 | `WAITING_UNKNOWN` > 0 |
| `s34_candidates_pending_selection` | gauge | 等待选择的候选组数 | 超过 24h 仍 `pending` |
| `s34_selection_latency_seconds` | histogram | 从 ready 到 selected | p90 过长 = 流程卡住 |
| `s34_rejected_all_total{cue}` | counter | 全部候选被否 | 突增 = 生成质量或意图描述有问题 |
| `s34_cut_strategy{strategy}` | counter | 收尾策略分布 | `unplaceable` 突增 |
| `s34_resynth_for_duration_total` | counter | **因时长重生成** | **任何一次都告警** |
| `s34_paid_candidates_total` | counter | 付费候选数 | 与 S26 `modality=music` 每日对齐 |
| `s34_paid_per_cue` | histogram | 每条 cue 的花费份数 | 应集中在 `candidate_count` |
| `s34_beat_confidence` | histogram | 节拍检测置信度 | 低置信比例过高 = 生成质量差或检测器不适配 |
| `s34_loop_availability_ratio` | gauge | 有可用循环区间的 take 占比 | 过低 = `loop_stitch` 不可用，重生成风险上升 |
| `s34_license_incomplete_total{dimension}` | counter | 四元组缺失 | 任何一次都告警（法律风险） |
| `s34_rights_lapsed_total` | counter | 使用前发现失效 | 突增 = 曲库批量下架/策略收紧 |
| `s34_gap_open{kind}` | gauge | 开放缺口 | 任何 `open` 超过 1h |
| `s34_dialogue_hint_missing_total` | counter | `dialogue_density_hint` 未知 | 占比高 = S42 只能全程 ducking |
| `s34_assertion_failures_total{kind}` | counter | 内部断言 | 任何一次都告警 |

**`s34_resynth_for_duration_total` 是这一节最重要的指标**，与 S33 的 `s33_generated_for_duration_only_total` 对应。区别在于：S33 的这个指标应该**恒为 0**（因为它能拉伸），S34 的**允许非 0 但必须罕见**（因为它不拉伸）。

**`s34_paid_per_cue` 是预算对话的核心指标。** 它的分布应该明显聚在 `candidate_count` 上。若出现长尾（某条 cue 花了 8 份），说明反复重生成——那才是要查的地方。

### 9.2 追踪

`traceparent` 贯穿 `POST /music-jobs → S02 → S25 → (S26 × N) → S27 → S28 → production.music.ready → S41 → S42`。

N 个候选的 span 必须是同一 trace 的兄弟 span，带 `candidate_index`。

`CutPlan` 计算要有独立 span，带 `edl_revision`、`end_strategy`、`beat_confidence`、`reason`。否则"为什么这段音乐在第 14 小节收"永远查不清。

### 9.3 日志

不记 `music_intent` 原文（剧情）、不记媒体 URL、不记 `attribution_text`、不记 ISRC/ISWC、不记许可合同条款。

记：`cue_id`、`cue_index`、`intent_hash`、`mood`、`instrumentation`、`natural_duration_ms`、`detect_bpm`、`end_strategy`、`rights_state`、`gap_kind`、`candidate_index`。

---

## 10. 测试清单

| 编号 | 场景 | 期望 | E |
|---|---|---|---|
| T34-01 | 同 command 重复 | 返回原响应 | E12 |
| T34-02 | 同 command 异 body | `409` | E12 |
| T34-03 | `music_intent` 空 | `422 EMPTY_MUSIC_INTENT` | E24 |
| T34-04 | `mood` 不在闭集 | `422 INVALID_MOOD` | E24 |
| T34-05 | `mood_intensity` 越界 | `422 INVALID_MOOD_INTENSITY` | E24 |
| T34-06 | `instrumentation` 不在闭集 | `422 INVALID_INSTRUMENTATION` | E24 |
| T34-07 | `duration_seconds <= 0` | `422 DURATION_OUT_OF_RANGE` | E24 |
| T34-08 | `candidate_count = 0` 或 `> 3` | `422 CANDIDATE_COUNT_OUT_OF_RANGE` | E35 |
| T34-09 | `tempo_hint.low > high` | `422 TEMPO_HINT_INVALID` | E24 |
| T34-10 | 3 候选，预算只够 2 份 | `REJECTED_LOCAL(BUDGET_INSUFFICIENT_FOR_CANDIDATES)`，**不降级为 2** | E14 |
| T34-11 | 渠道不支持多候选 | `REJECTED_LOCAL(CAPABILITY_MISSING)`，**不降级为 1** | E18 |
| T34-12 | 渠道不支持 `instrumental_only` | `REJECTED_LOCAL(CAPABILITY_MISSING)` | E18 |
| T34-13 | 许可 `sync_status='unknown'` | `POLICY_BLOCKED` + 缺口 `license_incomplete` | E30 |
| T34-14 | take 引用含 `unknown` 的 binding | 触发器 / 应用层拒绝登记 | E30 |
| T34-15 | `composition_status='licensed_generated'` 但无 `training_basis_ref` | DB 约束拒绝 | E30 |
| T34-16 | `sync_status='licensed'` 但地区为空 | DB 约束拒绝 | E30 |
| T34-17 | 3 候选全部成功归档 | `CANDIDATES_READY`，3 个 `MusicTake` | E24 |
| T34-18 | 3 候选里 1 个失败 | **全部作废** `FAILED(PARTIAL_CANDIDATES)`，不交付 2 个 | E24 |
| T34-19 | 归档 hash 不符 | `FAILED(HASH_MISMATCH)` | E23 |
| T34-20 | 归档 URL 过期 | 要新 URL，**不重新生成** | E23 |
| T34-21 | `candidate_count=1` | 自动选定，`selection_kind='auto_single'`，`selected_by` 非空 | E24 |
| T34-22 | `candidate_count=3` 无人选 | `pending`，`MusicGap(awaiting_selection)` | E24 |
| T34-23 | `candidate_count=3` 超时 | **不自动选第一个** | E24 |
| T34-24 | 导演选定 | `CueSelection.state='selected'`，检查点推进 | E24 |
| T34-25 | 全部候选被否 | `rejected_all` + 缺口 `all_candidates_rejected` | E24 |
| T34-26 | 二次选定 | 旧 selection `superseded`，`ms_current_idx` 只留一个 | E20 |
| T34-27 | 节拍置信度低 | `low_confidence=true`，禁止 `bar_trim` | E24 |
| T34-28 | 全部候选节拍检测失败 | `GAP_RECORDED(beat_unanalyzable)` | E24 |
| T34-29 | 部分候选节拍失败 | 该候选不登记，其余正常 | E24 |
| T34-30 | 时长 39s，曲子 47s，可对齐乐句 | `end_strategy='phrase'` | E38 |
| T34-31 | 时长 39s，曲子 47s，可对齐小节 | `end_strategy='bar'` | E38 |
| T34-32 | 时长 60s，曲子 47s，有循环区间 | `end_strategy='loop'`，segments 多段 | E38 |
| T34-33 | 时长 48s，曲子 47s（差 <1 小节），`tail_kind='sustaining'` | `end_strategy='bar_extend'` | E38 |
| T34-34 | 同上但 `tail_kind='hard_stop'` | 跳过 `bar_extend`，走 `unplaceable` 或重生成 | E38 |
| T34-35 | 时长 90s，曲子 47s，无循环区间 | `unplaceable` → 缺口或 `resynthesize_for_duration` | E38 |
| T34-36 | 剪辑只改时长，可 `bar_trim` | 不重生成，台账 0 行 | E38 |
| T34-37 | 剪辑改镜头顺序 | 全部 `cut_plan` 重算，零生成 | E38 |
| T34-38 | 实测 BPM 与提示值差 3 | `cut_plan` 用实测值，切点不漂移 | E38 |
| T34-39 | TRUNCATE `cut_plan_cache` 后查询 | 结果不变 | E13 |
| T34-40 | 许可在归档后失效 | take 标 void + 缺口 | E30 |
| T34-41 | 许可在 `GET` 时已过期 | `rights_state='void'`，`cut_plan.unplaceable` | E30 |
| T34-42 | S02 策略变更事件 | 批量重查，标 void，发 `music.take.rights-lapsed` | E30 |
| T34-43 | 跨租户 cue / take / license | `404` | E28 |
| T34-44 | `artifact_ref.service_id='suno'` | DB 拒绝 | E01 |
| T34-45 | 检查点并发 CAS | 一个成功一个重读 | E20 |
| T34-46 | 重启于 `ANALYZING` | 重新分析，结果一致 | E13 |
| T34-47 | 重启于 `WAITING_UNKNOWN` | 什么都不做 | E13 |
| T34-48 | 预算不足（S26） | `FAILED(BUDGET)`，不缩短时长 | E14 |
| T34-49 | 账户撤销 | `FAILED(ACCOUNT_UNAUTHORIZED)`，不换渠道 | E18 |
| T34-50 | 数据驻留禁止 | `REJECTED_LOCAL(RESIDENCY_FORBIDDEN)` | E31 |
| T34-51 | Kafka 重放 `production.music.ready` | 幂等消费 | E32 |
| T34-52 | 独立升级回滚 | 表驱动状态机一致 | E01 |
| T34-53 | `GET /episode?has_gap=true` | 返回该集全部开放缺口 | E37 |
| T34-54 | 台账：`bar_trim` / 复用 | **不写台账** | E26 |
| T34-55 | 台账：3 候选 | 写 **1 行** `generate`，`candidate_count=3` | E26 |
| T34-56 | `GET /music-takes/{id}/license` | 返回四元组 + ISRC + 归属，**不含 `music_intent`** | E30 |
| T34-57 | `dialogue_density_hint` 上游提供 | 透传存储，`source='s37_segments'` | E24 |
| T34-58 | `dialogue_density_hint` 上游未提供 | `'unknown'`，S42 走全程 ducking | E24 |

---

## 11. 实施顺序与未决

### 11.1 顺序（W3）

1. 表 + 状态机 + 全部 CHECK（T34-01/02/14…16/43/44/45）。
2. `content_fingerprint` 与复用（T34-36/54）。
3. **`CutPlan` 纯函数 + 五种策略**（T34-30…38）。这是 S34 的核心逻辑，必须在接供应商前做完并独立单测。
4. 节拍检测与循环点检测的接口（先 mock，后换真检测器）（T34-27…29）。
5. 多候选：`music_job_candidate`、全部或全无（T34-10/11/17/18）。
6. `CueSelection` 全生命周期（T34-21…26）。
7. S02 对接（mock）：四元组解析、`unknown` 拒绝、失效（T34-13/40…42）。
8. S25（mock）：多候选 / 无人声 / 对白避让能力位（T34-11/12）。
9. S26（mock）：N 份报价、预算、UNKNOWN（T34-45…49）。
10. S28（mock）：时长、URL 过期、hash（T34-19/20）。
11. `dialogue_density_hint` 透传（T34-57/58）。
12. 缺口汇总端点（T34-53），供 S41。
13. `/music-takes/{id}/license`（T34-56），供 S43。
14. 巡检、告警、runbook。
15. 真实渠道需授权。

### 11.2 未决

| # | 问题 | 当前处理 |
|---|---|---|
| U1 | `mood` / `instrumentation` 闭集由谁定义 | 当前 S34 内部定（§2.3），契约没有（§12 C1） |
| U2 | 先要 1 个候选、后要 3 个，怎么处理 | 当前：新 job 复用已有 take + 补 2 个（§7.1）。是否应允许"扩展已有 job"未定 |
| U3 | `BeatGrid` 检测器用哪个 | 当前接口化，未选型。置信度阈值 0.85 是经验值 |
| U4 | `bar_trim` 容差 ±80ms 是否合适 | 与帧率有关（24fps 一帧 41.7ms，两点帧）。实现时按 `ceil(2 帧, 80ms)` 取 |
| U5 | `CueSelection` 超时后是否该升级为 S40 人工 | 当前只记缺口 + 告警，不自动升级 |
| U6 | 授权曲库检索与生成的优先级 | 当前未实现检索路径（`source_kind` 留了 `licensed_library` 但无入口） |
| U7 | `vocal_presence` 由谁探测 | 当前 S34 在 `ANALYZING` 用音高/人声检测（与节拍检测同一批），置信度阈值未定 |
| U8 | 情绪曲线一致性检查是否需要 | 当前不做。若做，需要 S07 提供曲线定义 |
| U9 | 多段 `CutPlan` 是否该由 S41 而不是 S34 决定 | 当前 S34 算，理由是它懂节拍。若 S41 也有节拍信息，职责可能该挪 |

---

## 12. 冲突与契约缺口

### C1 `mood` / `instrumentation` / `mood_intensity` 在契约里不存在

`music.input` 是 `{music_intent, duration_seconds, instrumental_only, rights_policy_ref}`，`additionalProperties: false`。

**冲突点**：§2.3 论证了 `mood` 闭集是 `strategy_class` 归一、候选比较、情绪曲线一致性三项能力的前提。没有它，S34 只能从自由文本猜，不可复现。

`instrumentation` 同理：它决定成本档位（独奏钢琴 vs 管弦乐差一个量级）与生成方向。

**第二个缺口**：`candidate_count` 完全不存在，导致 §4.7 的成本问题无解。

**第三个缺口**：`beat_ref` / `scene_lineage_id` 不存在——`music.input` 没有任何与场景/时间的联系字段。

**不选边**：S34 在自己的 API 上要求这些字段（§6.1），组命令时只传契约允许的部分。

**建议**：`music.input` 补 `mood`（枚举 + 强度）、`instrumentation`（枚举）、`candidate_count`（整数）、`anchor`（场景/beat 引用）。前两项决定生成方向与成本，后两项决定多候选与定位。

### C2 `candidate_count` 与多候选成本

见 §4.7 与 C1。这是 S34 独有的、成本影响最大的缺口。

**当前处理**：发 N 次独立命令，共享 `selection_group_id`，在 `PREPARING` 阶段按 `N × quote` 检查预算。

**为什么这是"凑合"而非"正确"**：N 次独立准入意味着 N 份 reserve、N 份 permit、N 个 dispatch grant。中途失败会留下部分候选，S34 必须在 `INGESTING` 阶段整组作废（F10）——钱已经花了，货作废。若契约支持 `candidate_count`，S26/S23 可以整组预留、整组失败回滚。

### C3 `rights_policy_ref` 只给一个引用，不给四元组

`music.input.rights_policy_ref` 是一个 ref。S34 必须调 S02 才知道四个维度各自的状态。

**冲突点**：`docs/` 没有定义"S02 如何回答一首曲子的四元组许可"的接口。而发行申报需要的是四元组 + ISRC/ISWC + 归属文本。

**不选边**：S34 假设 S02 提供这个解析能力，并定义自己的 `LicenseBinding` 结构（§5.2）。若 S02 只能回答"策略允许什么"而不能回答"这首曲子的作曲/录音/表演/同步分别是什么状态"，S34 的 `composition_status` 等字段就只能填 `unknown`——**而 `unknown` 会导致所有 take 不可用**（§4.4.2）。

**建议**：S02 补一个"给定曲目/生成结果 → 四元组许可状态"的查询。这是 S34 能不能产出"可追溯许可"（`service.yaml` acceptance 原文）的硬前提。

**这不是技术债，是合规前提。** 若 S02 无此能力，S34 的验收项无法达成。

### C4 `artifact.ingested` 没有 payload schema

同 [S31 §12 C2](S31-speech-production.md) / [S32 §12 C4](S32-lipsync.md) / [S33 §12 C4](S33-sound-effects.md)。S34 的 `INGESTING → ANALYZING` 依赖 `natural_duration_ms`、`sample_rate_hz`、`channel_count`、`container`。

**S34 特有的需求**：一次 `generation.job` 可能对应多个 artifact（多候选）。`artifact.ingested` 是单个 artifact 的事件，S34 必须能把它归到 `(music_job_id, candidate_index)`。这需要一个稳定的关联字段——`provider_job_id` 或某个 `candidate_index`。没有它，多候选归档无法归位。

**不选边**：假设 `artifact.ingested` 带 `provider_job_id`，S34 通过 `music_job_candidate` 反查。

**建议**：`artifact.ingested` 的 payload schema 必须包含 `provider_job_id`，否则 S34 的多候选模型无法实现。

### C5 `service.yaml` 的 `depends_on` 缺 S02 / S25 / S07 / S41 / S43 / S37

`depends_on = [S26, S28]`。实际还依赖：

- **S25**：`route_decision_id` 必需
- **S02**：许可四元组解析（§4.4）
- **S06 / S07**：cue 意图、情绪曲线、`beat_ref`
- **S41**：EDL revision（`CutPlan` 的输入）
- **S43**：发行申报（`/music-takes/{id}/license`）
- **S37**：`dialogue_density_hint` 的来源

**不选边**：按实际依赖实现，`service.yaml` 是生成物。

**建议**：catalog 补齐。

### C6 缺口如何进入 S41 的 `completeness`

同 [S33 §12 C6](S33-sound-effects.md)。S41 的 `gaps[]` 的 `kind ∈ {missing_artifact, hash_mismatch, missing_subtitle_track, missing_stem, insufficient_handles, rights_expired}` 里**没有"音乐缺失"**。

**音乐比音效更严重：** 一整条 BGM 缺失，`formal_master_eligible` 却可能为真——成片会被当成"完整"交付出去。这正是 docs/11"必要内容失败时不交付假完整 master"要防的。

**建议**：S41 补 `kind='missing_bgm_cue'`，或把 BGM 缺口映射到 `missing_stem`（因为 `stem_bgm` 会缺失）。后者更省事，但会与"stem 本身存在但内容为空"混淆。

### C7 音乐没有 `defect_type`，"BGM 情绪不对"无处安放

同 [S33 §12 C9](S33-sound-effects.md)，但音乐多一类问题：**情绪/风格不对**。

这不是 `audio_mix`（不是响度问题），不是 `temporal`（不是时间问题），不是 `technical`（文件没问题）。

**当前处理**：不表达为缺陷。导演换候选（`CueSelection`），或提 `change_spec`（改 S07 的情绪曲线）。

**建议**：承认"创作判断"不在 QC 体系内，由 S40 的人工验收与 `CueSelection` 承担。不要为它硬造一个 `defect_type`。

### C8 `service.yaml` 的 `recovery` 里的"不能用未获许可曲目填补"如何检验

原文："变时长重算切点；**不能用未获许可曲目填补**"。

后半句是一个否定约束。S34 的实现靠三处：

1. `take` 引用含 `unknown` 的 `LicenseBinding` → 拒绝登记（§5.3）
2. 替代/填补走 `fill_gap` 时，必须实时复查许可（§4.4.4 时点 3）
3. `source_kind='licensed_library'` 的 take 必须有完整的 `sync` 地区与期限

**当前缺口**：没有独立的"授权曲库"入口（§11 U6）。若将来加，需要一个"检索结果 → 许可可用性"的即时检查，且这个检查必须走 S02。

**建议**：在引入检索路径前，先确认 S02 能做"给定曲目 ID → 当前许可状态"的实时查询。

### C9 `music.input.duration_seconds` 的语义冲突

同 [S33 §12 C3](S33-sound-effects.md)。`duration_seconds` 是必填，但它主要是时间线约束（剪辑决定），不是创作输入。

**音乐比音效更明显：** 39 秒和 42 秒的 BGM 在创作上是同一首曲子，只是收尾位置不同。把它设成生成命令的一部分，等于每次剪辑抖动都要发一次新命令。

**不选边**：S34 在指纹里排除时长（§8.1），用自己的 API 把"内容"与"目标时长"分开。

**建议**：同 S33 C3。

### C10 `service.yaml` 的 `api` 不对称：`POST /music-jobs` 但 `GET /music-cues/{id}`

同 [S33 §12 C8](S33-sound-effects.md)。S33/S34 都是这个模式，S31/S32 是对称的。

**§6.2 已论证这个不对称对音乐更贴切**（一条 cue 可能有多个 job、多个候选、多次选择）。但它是 catalog 既成事实。

**建议**：确认是被刻意设计的。

---

## 13. 与其他方案的交叉引用

| 方案 | 关系 |
|---|---|
| [S41](S41-editorial.md) | EDL 与 `affected_output_ranges` 的权威；**`CutPlan` 的输入**；缺口需进它的 `completeness`（C6） |
| [S42](S42-composition.md) | 混音、ducking、响度、`stem_bgm` 的唯一执行者；**ducking 由它做，S34 只透传 `dialogue_density_hint`**（§4.5） |
| [S43](S43-delivery.md) | 发行申报；消费 `/music-takes/{id}/license`（T34-56） |
| [S02](S02-governance-policy.md) | **许可四元组的权威**；需要"给定曲目 → 四元组状态"的解析读口（C3，合规前提） |
| [S06](S06-storyboard.md) / [S07](S07-change-management.md) | cue 意图、情绪曲线、`beat_ref` 的来源 |
| [S37](S37-audio-alignment.md) | `dialogue_density_hint` 的来源；不参与自动修复 |
| [S25](S25-model-routing.md) | `route_decision_id`；**缺多候选/无人声/对白避让能力位**（C1 相关） |
| [S26](S26-generation-jobs.md) | 唯一付费入口；多候选 = N 次准入（C2） |
| [S28](S28-artifact-ingest.md) | 音频 bytes 唯一权威；**`artifact.ingested` 必须带 `provider_job_id`**（C4） |
| [S33](S33-sound-effects.md) | **最直接的同构**：共享"跟着 EDL 重切、不重生成"的原则；差异在 S34 不拉伸、多候选、许可四元组 |
| [S35](S35-technical-qc.md) | **S34 不在任何动作族**（§1.4）；BGM 缺陷落在 `audio_mix` |
| [S39](S39-repair-planning.md) | 上限计数；S34 不构造 `strategy_class`（§8） |
| [S40](S40-human-review.md) | 选曲裁决、缺口 waive |
| [S08](S08-revision-snapshot.md) | 接受权威；检查点推进 ≠ 接受 |
| [docs/11 §2](../docs/11-quality-repair.md) | "BGM 盖对白 \| ducking/重混音"——确认修复手段在 S42，不在 S34 |

