# S42 · 混音与合成渲染 —— 实现方案

状态：实现方案（W4）。`maturity: design_only` 不变。容量数字（资源秒、并发）是规划值，未实测。

上游依据：[docs/07 §1/§2/§5](../docs/07-production-workflows.md)、[docs/11 §2](../docs/11-quality-repair.md)、[docs/12 §2/§3](../docs/12-budget-scheduling.md)、[S42 规格](../docs/design-package/services/S42-composition.md)、[S35 §2.2.1](../plans/S35-technical-qc.md)。

配套：[S41 剪辑](S41-editorial.md)（提供 `OutputPlan` / `MixPlan` / EDL）、[S28 媒体](S28-media-artifacts.md)（bytes 唯一权威）、[S08 快照](S08-revision-snapshot.md)（接受指针）、[S43 交付](S43-delivery.md)、[S39 返工](S39-repair-planning.md)。

---

## 0. 一句话边界

S42 把**已经决定好的时间线与混音计划**渲染成字节。

```
S41: 哪些片段、什么顺序、什么速度、要哪些 stem
S42: 跑滤镜图与编码器，产出候选字节、响度测量与渲染证据
S28: 保存这些字节，给它们 hash 与地址
S08: 决定哪一版 master 被接受
S43: 把被接受的 master 打包外发
```

**S42 不决定"剪成什么样"，不决定"接受哪一版"，不自己存字节。** 它是执行器：给定确定性输入，产出可复算的证据。

判据：S42 里任何"这一集现在是什么样子"的问题，答案都来自 S41；任何"这一版能不能交付"的问题，答案都来自 S08 + S43。

---

## 1. 责任与明确不负责

**唯一写者：**

- `RenderJob` / `RenderSegment` —— 一次渲染的执行记录与分段状态
- `MixJob` / `MixStem` —— 一次混音的执行记录与 stem 产物
- `FilterGraph` —— 规范化、可复算的滤镜图定义
- `RenderProfile` —— 编码/封装/色彩参数集合（**引用**项目规格，不发明）
- `MasterCandidate` —— 一个候选成片的 manifest（产物 refs + 计划 hash + 环境 + 测量）
- `MasterCandidateArtifact` —— 候选包含的每个 artifact 的角色与 ref
- `LoudnessMeasurement` —— 实际测量值（integrated LUFS / true peak / LRA）
- `RenderUsage` —— 本服务消耗的资源秒，作为**用量事实**投递给 S23 计量（不是账本）

**明确不写：**

| 事实 | 权威 | S42 的正确做法 |
|---|---|---|
| 时间线片段排列 / EDL / 速度 | **S41** | 只读 `timeline_revision` 与 `EDL`，不缓存后当作可变输入 |
| 媒体 bytes / hash / 存储 / TTL | **S28** | 渲染输入从 S28 取，输出写回 S28，只保存 ref |
| 字幕文本 / 时码 | **S38** | 消费 S38 的 subtitle artifact ref，不重排字幕 |
| 镜头内容 / 生成参数 | **S06 / S26** | 不关心 |
| 当前接受指针 / manifest / epoch | **S08** | 产出发事件，等 S08 决定 |
| QC 判定与缺陷 | **S35/S36/S37/S38** | 可触发 QC（经 S21），但不写 QC verdict |
| 交付包与外发 | **S43** | 不打包、不签名、不外发 |
| 金额 / 预算余额 | **S23** | 只上报用量，不预留、不扣减 |
| 配额许可 | **S22** | 申请 `cpu_media_second` / `gpu_second` 许可 |
| 目录价 / 供应商报价 | **S23 / S24** | 不自算成本 |

### 1.1 与 S26 的边界（最容易搞错）

S42 的渲染**不是**付费模型调用，**不走** S26 的 `Admission Saga`：

- 没有 provider、没有 `dispatch grant`、没有 `SUBMISSION_UNKNOWN`。
- 但**仍然要许可**：S42 消耗的是自建 GPU / CPU 媒体资源，许可来自 **S22** 的 `resource_class = gpu_second | cpu_media_second`。
- 用量上报 **S23**，但 S23 的账本只关心"花了多少"，不关心"渲染了哪一帧"。

docs/12 §2 的原话必须落成硬约束：

> CPU 媒体和自托管 GPU 按资源秒 / 内存另配，不能混成视频 API 并发。

实现上体现为：

| 错误做法 | 为什么错 | 正确做法 |
|---|---|---|
| 渲染任务占用 S26 的 provider 并发额度 | 视频 API 并发是**供应商**约束；渲染是本集群算力，混在一起会让 429 限流把本地渲染也卡死 | 独立 `resource_class` |
| 渲染失败退避与视频生成退避共用参数 | 供应商退避是分钟级；GPU OOM 需要换配置重试，不是等 | 独立重试策略 |
| 用渲染耗时估算生成成本 | 两条完全不同的成本线 | S23 分开归集 |

### 1.2 S42 不做的产品决定

- **不选编码器**。`master_spec_ref` 决定；规格缺失时**失败**，不默认 H.264。
- **不选响度目标**。`mix_plan.target_lufs` 决定；缺失时失败。
- **不决定 stem 要不要出**。`output_plan.stems` 决定。
- **不自动"修好"音频问题**。BGM 盖对白时，S42 能做的是按 `remix` 重新混音（见 §4.4），不能自己改对白音量曲线到"听起来还行"——那会把缺陷藏进 master。

---

## 2. 实体与关键区分

### 2.1 四个容易混淆的"版本"

| 概念 | 谁定义 | 语义 | 变化时 |
|---|---|---|---|
| `timeline_revision` | S41 | 剪成什么样 | 变 → 需要新渲染 |
| `mix_plan_hash` | S41 | 怎么混（增益、ducking、响度目标） | 变 → 只需 `remix` |
| `render_profile_hash` | S42（引用规格 + 本服务编码参数） | 怎么编码 | 变 → 需要重编码，但不必重剪 |
| `render_env_hash` | S42（编码器/驱动/容器镜像版本） | 在什么环境里编码 | 变 → 产物字节可能变，但**语义**不变 |

这四个共同决定"产物是否可复用"：

```
can_reuse_segment =
      timeline_segment_hash 相同
   AND source_artifact_hashes 相同
   AND render_profile_hash 相同
   AND render_env_hash 相同
```

**`render_env_hash` 进缓存键是个有意识的取舍。** 不同 GPU 驱动编出的 H.264 字节不同；如果缓存键不含环境，跨节点复用会得到"看起来一样但字节不同"的分段，拼起来可能有接缝。宁可多渲染一次，不要拼出带瑕疵的 master。

反过来：**`render_env_hash` 不进 `content_hash` 的语义层**。产物寻址用 S28 的内容 hash（字节事实），候选 manifest 记 `render_env_hash` 作为证据。两者不是一回事。

### 2.2 `FilterGraph`：必须是数据，不是代码

滤镜图不能是"渲染服务里的一段函数"，否则无法解释"这一版为什么和上一版不同"。

`FilterGraph` 是**规范化 JSON**，由 `mix_plan` + `master_spec` + 输入 artifact 机械生成：

```jsonc
{
  "graph_hash": "sha256:...",
  "graph_version": "ffgraph-v1",
  "nodes": [
    {"id":"in_v0","kind":"input","artifact_ref":{"service_id":"S28","id":"art_v0","revision":3,"content_hash":"..."}},
    {"id":"trim_v0","kind":"trim","src_in":0,"src_out":360000},
    {"id":"scale_v0","kind":"scale","width":1920,"height":1080,"flags":"lanczos"},
    {"id":"in_dlg","kind":"input","artifact_ref":{...}},
    {"id":"gain_dlg","kind":"volume","gain_db":0.0},
    {"id":"duck_dlg","kind":"sidechaincompress","threshold_db":-18,
     "ratio":4.0,"attack_ms":20,"release_ms":250,"key":"in_bgm"},
    {"id":"mix_av","kind":"amix","inputs":["gain_dlg","in_bgm","in_sfx"],"normalize":false},
    {"id":"loud","kind":"loudnorm","target_i":-16.0,"target_tp":-1.0,"target_lra":11.0},
    {"id":"enc_v","kind":"encode","codec":"h264","crf":18,"preset":"slow"},
    {"id":"mux","kind":"mux","container":"mp4","faststart":true}
  ],
  "edges": [["in_v0","trim_v0"],["trim_v0","scale_v0"],["scale_v0","enc_v"], ...]
}
```

三条硬规则：

1. **`graph_hash` 由节点与边确定性序列化得出**（键排序、数字规范化）。同一 `mix_plan` + `master_spec` + 输入，必须得到同一 `graph_hash`。
2. **节点 kind 是封闭集合**。新增 kind 需要契约升级；不允许"直接塞一段 ffmpeg 参数"——那等于把不可审计的代码放进数据。
3. **`loudnorm` 是两遍**：第一遍测量，第二遍按测量值线性增益 + 限幅。一遍 loudnorm 的动态增益会让不同片段响度不一致，短剧里非常明显。

**滤镜图里没有"自动修复"节点。** 不存在 `kind: "denoise_auto"` 这种会自行决定参数的节点。所有参数必须由上游计划给出。

### 2.3 分成哪些渲染单位

| 单位 | 粒度 | 用途 |
|---|---|---|
| `RenderJob` | 一个候选 master | 对外命令与状态 |
| `RenderSegment` | 一个 EDL 连续区间（通常 30–120 秒） | 并发、缓存复用、断点续跑 |
| `RenderArtifact` | 一个交付产物（视频/音频/stem/字幕封装） | S28 寻址单位 |

分段规则：

- 按 **GOP 对齐**切分，切点落在关键帧，保证拼接无损。
- 默认 60 秒；含转场的区间不切（转场需要前后 frame handle）。
- **分段边界由 S41 的 `output_plan.segments` 决定**，S42 不自行改分段——否则同一时间线在不同时间渲染出不同分段，缓存全失效。

### 2.4 `MasterCandidate` 与 `accepted master` 的区别

| | `MasterCandidate` | accepted master |
|---|---|---|
| 所有者 | **S42** | **S08** |
| 含义 | "我渲染出了一个东西" | "这是当前这一集的有效成片" |
| 可变 | 不可变（新渲染 = 新候选） | 指针可变（接受新版本） |
| 数量 | 可以有多个 | 每 (episode, branch) 最多一个 |

S42 发出 `post.master.rendered` 后，候选**可能永远不会被接受**。这是正常的：预览包、失败尝试、探索性编码都会留在库里作为历史。**S42 不因为候选未被接受就删除它**（E39 审计）。清理走 S28 的 artifact TTL，而不是 S42 删记录。

---

## 3. 状态机

### 3.1 `RenderJob`

```
        POST /render-jobs
              │ 幂等 + 输入校验 + 许可申请
              ▼
          ┌────────┐   S22 拒绝/预算拒绝
          │ QUEUED │──────────────────────────► REJECTED（终态）
          └────────┘
              │ 获得 permit lease
              ▼
          ┌─────────┐
          │ ADMITTED│
          └─────────┘
              │ worker 认领（租约）
              ▼
          ┌─────────┐   分段全部成功 + 产物写入 S28
          │ RUNNING │──────────────────────────► SUCCEEDED
          └─────────┘
            │   │   │
            │   │   └── 用户/策略取消 ──────────► CANCELLING → CANCELLED
            │   │                                  （已完成的段保留，已产生的用量照常上报）
            │   └────── 可重试失败（GPU OOM/节点抢占）→ 退避后回 RUNNING
            └────────── 不可重试失败 / 重试耗尽 ──► FAILED
              │
              └─ worker 租约过期（进程消失）
                     ▼
                 ┌──────────┐
                 │ ORPHANED │── 对账：查 S28 有无产物
                 └──────────┘      ├─ 有完整产物 → SUCCEEDED（补登记）
                                   ├─ 有部分产物 → RUNNING（续跑剩余段）
                                   └─ 无产物     → QUEUED（重新排队）
```

- **没有 `SUBMISSION_UNKNOWN`**。渲染是本地执行，没有"供应商可能已经做了但没回执"的问题。`ORPHANED` 的恢复依据是 **S28 的实际产物**，不是本地猜。
- `CANCELLED` 之后**不退款**：已消耗的 GPU 秒是真的消耗了。这对应 E22"取消不假退款"。
- `CANCELLING` 是必要状态：正在跑的编码器不能瞬间停，先置 `CANCELLING`，等当前段收尾或强杀。

### 3.2 `MixJob`

比 `RenderJob` 简单，且是**修复路径的主角**：

```
CREATED → RUNNING → SUCCEEDED | FAILED | CANCELLED
```

`MixJob` 不编码视频。它接受：

- 输入：已存在的 video artifact（或 S41 的 video 段）+ stems + `mix_plan`
- 输出：新的 audio artifact（可能还有新的混音后 stem）

**`MixJob` 是 `remix` 动作的执行体**（§4.4）。这是它存在的核心理由：BGM 盖对白时，重新混音不需要重渲染视频，成本差一到两个数量级。

### 3.3 `MasterCandidate`

```
        （渲染完成）
             ▼
        ┌───────────┐
        │ CANDIDATE │
        └───────────┘
          │    │    │
          │    │    └── S08 接受 ──────────────► ACCEPTED_BY_SNAPSHOT
          │    │                                  （仅 S42 本地投影，非权威）
          │    └─────── 出现更新的候选 ──────────► SUPERSEDED
          └──────────── QC 判定不可交付 ─────────► QC_REJECTED
                                  │
                                  └── execution_fence 推进 → VOIDED
```

**`ACCEPTED_BY_SNAPSHOT` 只是 S42 的投影**：它由消费 `snapshot.manifest.committed` 得出，权威在 S08。如果两者不一致，以 S08 为准，S42 修投影。这条要写进代码注释，否则下一任维护者会拿它当真相。

**`SUPERSEDED` 不删除。** 交付证据可能引用旧候选（"客户 3 月 5 日验收的是这一版"）。

### 3.4 非法转移

| 尝试 | 结果 |
|---|---|
| 用 `formal_master_eligible=false` 的 timeline 创建**正式**渲染 | 422 `INCOMPLETE_TIMELINE` |
| `master_spec_ref` 缺失 | 422 `MASTER_SPEC_REQUIRED` |
| `mix_plan.target_lufs` 缺失 | 422 `MIX_TARGET_REQUIRED` |
| 输入 artifact 在 S28 不存在 | 422 `INPUT_ARTIFACT_MISSING` |
| `CANCELLED` 后继续上传产物 | 409 `JOB_TERMINAL`（产物丢弃或降级为孤儿清理） |
| 对 `VOIDED` 候选请求接受 | 不允许（S42 无此命令）；若 S08 误接受，S08 侧拒绝 |
| 同 `command_id` 不同 body | 409 `IDEMPOTENCY_MISMATCH` |

---

## 4. 关键流程

### 4.1 正式渲染

```
POST /render-jobs
  1. 认证 + scope
  2. 幂等查 (tenant, command_id, request_hash)
  3. 读 S41 timeline_revision（只读）
       ├─ formal_master_eligible=false 且请求 formal → 422
       ├─ 读 output_plan / mix_plan
       └─ 实时复查 completeness（§4.2）
  4. 实时复查输入 artifact（S28）与字幕（S38）、音色授权（S15）
  5. 解析 master_spec_ref → RenderProfile
       ├─ 缺失 → 422 MASTER_SPEC_REQUIRED
       └─ 生成 render_profile_hash
  6. 申请 S22 许可：resource_class + 资源秒估算
       ├─ 拒绝 → REJECTED，不落 RUNNING
       └─ 预算不足 → 422 BUDGET_DENIED（透传 S23/S22 原因）
  7. 事务写 render_job(QUEUED) + segments + outbox + command_receipt
  8. 调度：worker 认领 → ADMITTED → RUNNING
  9. 每段：渲染 → 上传 S28 → 记录 artifact ref + 段用量
 10. 全段完成 → 拼接 → 两遍 loudnorm → 终检 → 写 MasterCandidate
 11. 发 post.master.rendered
 12. 异步上报 RenderUsage 给 S23
```

第 6 步的顺序很关键：**先申请许可再落 job**。反过来会造出一堆"排队但永远拿不到许可"的僵尸任务。

第 4 步是**渲染前的实时复查**，不是创建任务时的检查结果。素材可能刚被删除、音色授权可能刚被撤销（E30）、字幕可能刚被替换。docs/10 §"解析成功不代表权利永远有效"直接适用。

### 4.2 completeness 复查的两层

| 层 | 何时 | 依据 | 失败 |
|---|---|---|---|
| L1 结构完整性 | 创建 job | S41 revision 的 `completeness` | 422 |
| L2 实时素材完整性 | 每段开始前 | S28 `HEAD` artifact + hash | 段级失败，可重试；若持续缺失 → job FAILED + gap 报告 |

**L2 必须逐段做**，不能只在开始时做一次。一个 40 分钟成片渲染 20 分钟，中途素材被删，第 15 段才失败——如果只在开头检查，前 14 段白渲染。

### 4.3 增量渲染：只重渲染受影响区间

S41 给了 `affected_output_ranges`。S42 的复用判定：

```
for each segment s in new_plan.segments:
    key = (timeline_segment_hash, sorted(input_content_hashes),
           render_profile_hash, render_env_hash, subtitle_artifact_hash)
    if cache.has(key) and not s.intersects(affected_ranges):
        reuse(cache.get(key))
    else:
        render(s)
```

三条注意：

1. **交叠即重渲染**：段只要与 `affected_ranges` 有交叠就不复用。不要试图只重渲染半段——GOP 边界之外的东西必须一起编。
2. **`shifted` 片段可以"搬"不能"复用段"**：片段位移后它的输出位置变了，如果段边界按 EDL 切，整个段位置也变了。所以位移会重渲染该段——但它的**内容**没变，编码结果字节可能相同，S28 去重会直接命中。这是 S28 内容寻址的天然收益，S42 不需要额外优化。
3. **缓存不是正确性依赖**。缓存全空也必须渲染出正确结果。缓存只是提速。

### 4.4 `remix`：BGM 盖对白的正确修复路径

docs/11 §2 的典型缺陷：BGM 音量盖过对白。

**错误做法**：重新走一遍视频生成（`regenerate_video`）。代价是把一段 8 秒的镜头重新生成，可能换脸、换运镜、换口型，引入新缺陷。

**正确做法**：`remix`。

```
S36/S37 检出缺陷（audio_mix / bgm_over_dialogue）
   → S39 计划：action = remix（不是 reingest，不是 regenerate_video）
   → S21 编排：POST /mix-jobs
        inputs: 现有 video artifact + 原始 stems (dialogue / sfx / bgm)
        mix_plan: 调整 ducking 参数（threshold / ratio）
        target: 重新生成 mix audio artifact
   → S42 只跑音频滤镜图（秒级，不是分钟级）
   → 输出新 audio artifact → S28
   → 生成新 MasterCandidate（视频流复用，音频流替换）
   → 触发 S36 recheck
```

关键约束：

1. **`remix` 必须保留原 video 流**。候选 manifest 里 video artifact ref 与上一版逐字段相同。如果实现上"顺手重编码一遍视频"，那就退化成 regeneration，成本与风险都回来了。
2. **stems 必须可用**。如果原始 stems 已过期/删除，`remix` **不可行**，必须显式失败并报告 `STEMS_UNAVAILABLE`，让 S39 改计划——**绝不允许**从混好的成品里"猜"出对白轨再分离（那会引入新的分离伪影）。
3. **不得用 `reingest` 修音频**。`reingest` 是重新导入素材的语义（S28），用在音频缺陷上会掩盖问题。S35 §2.2.1 已明确 `audio_mix` 族的 `action_family` 是 `remix → regenerate_video`，且 `reingest` 是**禁止项**。
4. **每次 `remix` 的 `mix_plan` 变化必须记录**，因为交付时需要能回答"这一版和上一版音频差在哪"。

### 4.5 响度门

`loudnorm` 两遍完成后，必须**实测**并记录：

```jsonc
{
  "integrated_lufs": -16.0,
  "true_peak_dbtp": -1.2,
  "lra_lu": 7.4,
  "measured_at": "...",
  "measurement_tool": "ebur128",
  "measurement_version": "...",
  "conforms_to_spec": true
}
```

规则：

- **`conforms_to_spec` 由实测值比较 master_spec 的容差得出**，不是由编码器自报。
- 不达标 → 候选仍可生成，但标记 `loudness_gate: FAILED`，`deliverable=false`。S43 打包正式包时必须重新校验（§7）。
- **不做"测量失败就跳过"**。测不出 = 门失败，不是门通过。
- 响度是**整片**测量，不是逐段。逐段测完再合会在边界产生响度跳变，短剧的连续对白场景尤其明显。

### 4.6 失败矩阵

| 失败点 | 检测 | 行为 | 不变量 |
|---|---|---|---|
| GPU OOM | 渲染进程非零退出 + 显存日志 | 降分辨率？**不。** 降 `encoder.preset` 或改分段长度后重试，最多 2 次 | 不改输出的语义参数（分辨率/帧率） |
| 节点被抢占 | worker 心跳丢失 | job → ORPHANED → 对账 S28 → 续跑 | 已上传段不重渲染 |
| 编码器崩溃 | 段级非零退出 | 同段重试 3 次，仍失败 → job FAILED | 不把损坏段静默跳过 |
| 磁盘满 | 写临时文件 ENOSPC | 该 worker 标记不可调度；job 回 QUEUED | 不产生截断的产物 |
| S28 上传失败 | HTTP 5xx/timeout | 段级重试（指数退避，最多 8 次）；仍失败 → job FAILED，保留已上传段 | 产物 ref 只在 S28 确认后写入 |
| 拼接后时长与 EDL 不符 | 自检（帧数比较） | **job FAILED**，不产出候选 | master 时长必须等于 EDL 时长 |
| loudnorm 不达标 | 实测 | 候选生成但 `deliverable=false` | 不"调参硬凑"到刚好达标 |
| 输入产物在渲染中被删除 | L2 复查 | 段失败 → job FAILED + `INPUT_ARTIFACT_MISSING` | 不缓存"曾经存在" |
| 许可在渲染中到期 | S22 lease 续期失败 | **继续渲染并结算**（不可取消任务），但不再接受新段；job 完成 | E22：取消不假退款 |
| worker 磁盘上的中间文件残留 | 周期性清理 | 按 `render_env_hash` 隔离的临时目录，TTL 2h | 不跨租户共享临时目录 |
| 同时两个相同的 render 命令 | 幂等 + 缓存 | 第二个命中 `command_receipt` 或段缓存 | 不重复消耗 GPU |
| 字幕 artifact 在渲染后变化 | 候选 manifest 记录 subtitle hash | 候选仍有效，但 `stale_inputs` 标记非空；S43 打包时拒绝 | 不用旧字幕配新 master |

最后一条对应一个真实场景：渲染 40 分钟的片，第 35 分钟时字幕被修了一版。正确行为不是"回滚渲染"，而是**记录这个候选基于哪版字幕，让交付门去判断**。自动重渲染可能永远追不上字幕的修改速度。

---

## 5. 数据模型

数据库 `composition`，Postgres。资源用量用 `bigint` 存毫秒/毫瓦秒；响度用 `numeric`（展示与比较，不参与金额）。

```sql
CREATE TABLE composition.render_profile (
  tenant_id            text NOT NULL,
  profile_hash         char(64) NOT NULL,
  master_spec_ref      jsonb NOT NULL,          -- 引用项目规格，不内联定义
  container            text NOT NULL,           -- mp4 | mov | mxf
  video_codec          text NOT NULL,           -- h264 | hevc | prores
  video_params         jsonb NOT NULL,
  audio_codec          text NOT NULL,           -- aac | pcm_s24le
  audio_params         jsonb NOT NULL,
  color_primaries      text,
  transfer_function    text,
  matrix_coefficients  text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, profile_hash)
);

CREATE TABLE composition.render_job (
  tenant_id            text NOT NULL,
  render_job_id        text NOT NULL,
  project_id           text NOT NULL,
  series_id            text NOT NULL,
  episode_id           text NOT NULL,
  branch_id            text NOT NULL,
  timeline_id          text NOT NULL,
  timeline_revision    integer NOT NULL,
  timeline_content_hash char(64) NOT NULL,
  mix_plan_hash        char(64) NOT NULL,
  render_profile_hash  char(64) NOT NULL,
  render_env_hash      char(64) NOT NULL,
  purpose              text NOT NULL CHECK (purpose IN ('formal','preview','draft')),
  state                text NOT NULL CHECK (state IN
                       ('QUEUED','ADMITTED','RUNNING','CANCELLING','CANCELLED',
                        'SUCCEEDED','FAILED','REJECTED','ORPHANED')),
  permit_ref           jsonb,                   -- S22 permit
  worker_id            text,
  lease_expires_at     timestamptz,
  estimated_gpu_ms     bigint,
  consumed_gpu_ms      bigint NOT NULL DEFAULT 0,
  consumed_cpu_ms      bigint NOT NULL DEFAULT 0,
  retry_count          integer NOT NULL DEFAULT 0,
  failure_code         text,
  failure_detail       jsonb,
  stale_inputs         jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, render_job_id)
);
CREATE INDEX render_job_state_idx ON composition.render_job (state, lease_expires_at)
  WHERE state IN ('ADMITTED','RUNNING','ORPHANED','CANCELLING');
CREATE INDEX render_job_episode_idx ON composition.render_job
  (tenant_id, episode_id, branch_id, created_at DESC);

CREATE TABLE composition.render_segment (
  tenant_id            text NOT NULL,
  render_job_id        text NOT NULL,
  segment_no           integer NOT NULL,
  rec_in_ticks         bigint NOT NULL,
  rec_out_ticks        bigint NOT NULL,
  segment_key          char(64) NOT NULL,       -- 缓存键（§2.1）
  state                text NOT NULL CHECK (state IN ('PENDING','RUNNING','DONE','FAILED','REUSED')),
  output_artifact_ref  jsonb,                   -- S28 ref
  attempts             integer NOT NULL DEFAULT 0,
  consumed_gpu_ms      bigint NOT NULL DEFAULT 0,
  started_at           timestamptz,
  finished_at          timestamptz,
  PRIMARY KEY (tenant_id, render_job_id, segment_no),
  FOREIGN KEY (tenant_id, render_job_id)
    REFERENCES composition.render_job (tenant_id, render_job_id),
  CONSTRAINT seg_order CHECK (rec_out_ticks > rec_in_ticks)
);
CREATE INDEX render_segment_cache_idx ON composition.render_segment (tenant_id, segment_key)
  WHERE state IN ('DONE','REUSED') AND output_artifact_ref IS NOT NULL;

CREATE TABLE composition.filter_graph (
  tenant_id            text NOT NULL,
  graph_hash           char(64) NOT NULL,
  graph_version        text NOT NULL,
  graph                jsonb NOT NULL,          -- 规范化 JSON
  built_from           jsonb NOT NULL,          -- {timeline_revision, mix_plan_hash,
                                                --  render_profile_hash, input_artifact_hashes}
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, graph_hash)
);

CREATE TABLE composition.mix_job (
  tenant_id            text NOT NULL,
  mix_job_id           text NOT NULL,
  episode_id           text NOT NULL,
  branch_id            text NOT NULL,
  source_video_ref     jsonb NOT NULL,          -- S28 ref（remix 时逐字段复用）
  mix_plan_hash        char(64) NOT NULL,
  graph_hash           char(64) NOT NULL,
  target_lufs          numeric(5,2) NOT NULL,
  true_peak_ceiling    numeric(5,2) NOT NULL,
  reason               text NOT NULL CHECK (reason IN
                       ('initial_mix','remix_defect','spec_change','manual')),
  repair_plan_ref      jsonb,                   -- S39 引用（remix_defect 时必填）
  state                text NOT NULL CHECK (state IN
                       ('CREATED','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  output_audio_ref     jsonb,
  consumed_cpu_ms      bigint NOT NULL DEFAULT 0,
  created_at           timestamptz NOT NULL DEFAULT now(),
  finished_at          timestamptz,
  PRIMARY KEY (tenant_id, mix_job_id),
  CONSTRAINT mix_remix_needs_plan
    CHECK (reason <> 'remix_defect' OR repair_plan_ref IS NOT NULL)
);
CREATE INDEX mix_job_episode_idx ON composition.mix_job
  (tenant_id, episode_id, branch_id, created_at DESC);

CREATE TABLE composition.loudness_measurement (
  tenant_id            text NOT NULL,
  measurement_id       text NOT NULL,
  render_job_id        text,                    -- 与 mix_job_id 二选一
  mix_job_id           text,
  artifact_ref         jsonb NOT NULL,
  integrated_lufs      numeric(6,2) NOT NULL,
  true_peak_dbtp       numeric(6,2) NOT NULL,
  lra_lu               numeric(6,2),
  measurement_tool     text NOT NULL,
  measurement_version  text NOT NULL,
  conforms_to_spec     boolean NOT NULL,
  spec_ref             jsonb NOT NULL,
  measured_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, measurement_id),
  CONSTRAINT loudness_one_parent
    CHECK ((render_job_id IS NULL) <> (mix_job_id IS NULL))
);

CREATE TABLE composition.master_candidate (
  tenant_id            text NOT NULL,
  candidate_id         text NOT NULL,
  episode_id           text NOT NULL,
  branch_id            text NOT NULL,
  timeline_id          text NOT NULL,
  timeline_revision    integer NOT NULL,
  manifest_content_hash char(64) NOT NULL,      -- 候选内容的规范哈希
  render_job_id        text,
  mix_job_id           text,
  video_artifact_ref   jsonb NOT NULL,
  audio_artifact_ref   jsonb NOT NULL,
  duration_ticks       bigint NOT NULL,
  frame_count          bigint NOT NULL,
  loudness_measurement_id text,
  loudness_gate        text NOT NULL CHECK (loudness_gate IN ('PASSED','FAILED','NOT_RUN')),
  qc_report_refs       jsonb NOT NULL DEFAULT '[]'::jsonb,
  stale_inputs         jsonb NOT NULL DEFAULT '[]'::jsonb,
  deliverable          boolean NOT NULL,
  state                text NOT NULL CHECK (state IN
                       ('CANDIDATE','ACCEPTED_BY_SNAPSHOT','SUPERSEDED',
                        'QC_REJECTED','VOIDED')),
  execution_fence      bigint NOT NULL,
  render_env_hash      char(64) NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, candidate_id),
  CONSTRAINT candidate_deliverable_consistent
    CHECK (deliverable = false OR (loudness_gate = 'PASSED' AND jsonb_array_length(stale_inputs) = 0))
);
CREATE UNIQUE INDEX candidate_hash_uniq
  ON composition.master_candidate (tenant_id, episode_id, branch_id, manifest_content_hash);
CREATE INDEX candidate_episode_idx ON composition.master_candidate
  (tenant_id, episode_id, branch_id, created_at DESC);

CREATE TABLE composition.master_candidate_artifact (
  tenant_id            text NOT NULL,
  candidate_id         text NOT NULL,
  role                 text NOT NULL CHECK (role IN
                       ('master_video','master_audio','stem_dialogue','stem_sfx',
                        'stem_bgm','subtitle','thumbnail','qc_overlay')),
  artifact_ref         jsonb NOT NULL,
  ordinal              integer NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, candidate_id, role, ordinal)
);

CREATE TABLE composition.render_usage (
  tenant_id            text NOT NULL,
  usage_id             text NOT NULL,           -- 确定性：render_job_id + segment_no
  render_job_id        text,
  mix_job_id           text,
  resource_class       text NOT NULL CHECK (resource_class IN ('gpu_second','cpu_media_second')),
  quantity_ms          bigint NOT NULL CHECK (quantity_ms >= 0),
  occurred_at          timestamptz NOT NULL,
  reported_to_s23      boolean NOT NULL DEFAULT false,
  PRIMARY KEY (tenant_id, usage_id)
);
CREATE INDEX render_usage_unreported_idx ON composition.render_usage (occurred_at)
  WHERE reported_to_s23 = false;

CREATE TABLE composition.command_receipt (
  tenant_id            text NOT NULL,
  command_id           text NOT NULL,
  request_hash         char(64) NOT NULL,
  response_body        jsonb NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE composition.outbox (
  event_id             uuid PRIMARY KEY,
  tenant_id            text NOT NULL,
  aggregate_id         text NOT NULL,
  aggregate_sequence   bigint NOT NULL,
  event_type           text NOT NULL,
  payload              jsonb NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  published_at         timestamptz
);
CREATE INDEX outbox_unpublished_idx ON composition.outbox (created_at)
  WHERE published_at IS NULL;

CREATE TABLE composition.inbox (
  consumer             text NOT NULL,
  event_id             uuid NOT NULL,
  received_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

**不建的表：**

- 不建 `render_cache`。缓存即 `render_segment` 里 `DONE` 的段，按 `segment_key` 查；单独的表会与段记录不一致。
- 不建 `cost_ledger`。用量上报 S23，S42 只留自己消耗的原始记录（`render_usage`），这不是账本。
- 不建 `accepted_master`。那是 S08 的表。

---

## 6. 对外契约

### 6.1 命令

`POST /render-jobs`（service.yaml 已声明）

```jsonc
{
  "context": { ...execution-context v2，含 tenant/project/series/episode/branch/
               manifest_ref/execution_fence/node_id/node_epoch/actor/command_id/
               request_hash/trace_id... },
  "timeline_ref": {"timeline_id":"tl_ep3","revision":10,"content_hash":"sha256:..."},
  "purpose": "formal",
  "mix_plan_hash": "sha256:...",
  "master_spec_ref": {"service_id":"...","id":"...","revision":1,"content_hash":"..."},
  "requested_stems": ["dialogue","sfx","bgm"],
  "subtitle_ref": {"service_id":"S38","id":"sub_ep3","revision":7,"content_hash":"sha256:..."},
  "command_id": "...",
  "request_hash": "sha256:..."
}
```

响应 `202`：

```jsonc
{
  "render_job_id":"rj_...", "state":"QUEUED",
  "estimated_gpu_ms": 480000,
  "permit_ref":{"permit_id":"..."},
  "operation_id":"...",              // docs/13 §1：长任务必须返回可轮询标识
  "realtime_ref":{"run_id":"...","sse_topic":"run:..."}   // 供 S47 投影（缺口见 §11 C3）
}
```

`POST /mix-jobs`（**service.yaml 未声明** → §11 C2）

```jsonc
{
  "context": {...},
  "source_video_ref": {"service_id":"S28","id":"art_master_v","revision":3,"content_hash":"..."},
  "stems": [{"role":"dialogue","artifact_ref":{...}}, {"role":"bgm","artifact_ref":{...}},
            {"role":"sfx","artifact_ref":{...}}],
  "mix_plan_hash":"sha256:...",
  "target_lufs": -16.0,
  "true_peak_ceiling": -1.0,
  "reason":"remix_defect",
  "repair_plan_ref":{"service_id":"S39","id":"rp_...","revision":2,"content_hash":"..."},
  "command_id":"...", "request_hash":"sha256:..."
}
```

`POST /render-jobs/{id}:cancel` / `POST /mix-jobs/{id}:cancel`

- 有 `command_id`；幂等。
- 返回 `state: "CANCELLING"`，不承诺立即停止。
- 已消耗用量照常上报（E22）。

### 6.2 查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/render-jobs/{id}` | 状态、段进度、`stale_inputs`、用量 |
| GET | `/render-jobs?episode_id=&branch_id=` | 列表 |
| GET | `/master-candidates/{id}` | 候选 manifest（含全部 artifact 角色） |
| GET | `/master-candidates?episode_id=&branch_id=&deliverable=true` | 可交付候选 |
| GET | `/master-candidates/{id}/evidence` | 渲染证据包：graph_hash、render_env、响度、QC refs、输入 hashes |

**`/master-candidates/{id}/evidence` 是给 S43 与 S45 用的核心接口**：交付包要求"可复现的渲染证据"，答案在这里。它必须包含"这次渲染的每个输入是什么 hash、用的什么滤镜图、什么环境"。

### 6.3 事件

| 方向 | 事件 |
|---|---|
| 发布 | `post.master.rendered`（topic `drama.composition.v2`）、`post.mix.completed`、`post.render.failed` |
| 消费 | `post.timeline.revision.created`（S41）、`quality.repair.planned`（S39，`remix` 动作）、`snapshot.manifest.committed`（S08，投影候选状态）、`artifact.ingested`（S28） |
| 调用 | S22 许可、S23 用量上报、S28 读写 artifact、S15 授权复查、S41 读 timeline、S38 读字幕 |

`post.master.rendered` 的 `data`：

```jsonc
{
  "candidate_id":"mc_...",
  "episode_id":"ep_3","branch_id":"main",
  "timeline_revision":10,
  "video_artifact_ref":{...},"audio_artifact_ref":{...},
  "duration_ticks": 5400000,
  "loudness":{"integrated_lufs":-16.0,"true_peak_dbtp":-1.2,"gate":"PASSED"},
  "deliverable": true,
  "execution_fence": 2,
  "render_env_hash":"sha256:...",
  "graph_hash":"sha256:...",
  "stale_inputs": [],
  "qc_trigger_requested": true
}
```

**`post.master.rendered` 不是"已成片"**，只是"有一个候选可以评审了"。消费者不得据此推进接受指针——那要 S08 的 CAS。

---

## 7. 幂等与并发

### 7.1 幂等

| 对象 | 幂等键 | 说明 |
|---|---|---|
| `RenderJob` 创建 | `tenant + command_id + request_hash` | 同键同 body 返回原 job |
| `MixJob` 创建 | 同上 | |
| 段渲染 | `(render_job_id, segment_no)` + `segment_key` | 段状态机天然幂等；重复执行只覆盖同段 |
| 产物登记 | S28 内容寻址 | 同内容重复上传 = 同一 artifact |
| 用量上报 | `usage_id = render_job_id + ":" + segment_no` | S23 侧按 id 去重 |
| outbox 投递 | `event_id` | 下游 inbox 去重 |

`request_hash` 覆盖：`timeline_ref`、`purpose`、`mix_plan_hash`、`master_spec_ref`、`subtitle_ref`、`requested_stems`、`context` 的业务字段。**不覆盖** `trace_id`、时间戳、`estimated_*`。

### 7.2 并发

| 场景 | 机制 |
|---|---|
| 同 episode 两个 formal 渲染 | **允许并发**。不设"每集只能一个"的锁——那会让重试路径卡死。由 S08 决定接受哪个候选。 |
| 同段被两个 job 同时渲染 | 段缓存读：后到者可能重复渲染一次，无副作用；写入 S28 时内容寻址去重 |
| worker 认领竞争 | `UPDATE ... WHERE state='ADMITTED' AND worker_id IS NULL RETURNING *` |
| 租约续期与抢占 | 租约 TTL 90s，心跳 30s；过期由 reaper 转 `ORPHANED` |
| 同一 `timeline_revision` 的 `mix_plan_hash` 变化 | 允许，因为 `remix` 不改时间线 |
| 取消与完成竞争 | 先到者赢；`CANCELLING` 收到 `SUCCEEDED` 时以 `SUCCEEDED` 为准并记录 `cancel_lost_race` |

**`ORPHANED` 对账必须查 S28，不能查本地临时文件。** 节点被抢占后本地磁盘可能整个消失。

### 7.3 分段缓存的一致性

```
segment_key = sha256(
    canonical(timeline_segment),          # 含 clip 排列、in/out、speed、transition
    sorted(input_artifact_content_hashes),
    render_profile_hash,
    render_env_hash,
    subtitle_artifact_hash_or_null
)
```

- 缓存命中只表示"这段的字节可复用"，不表示"整个 master 可复用"。
- **缓存永不过期**它不是"旧的"，它是"内容相同"。清理靠 S28 的 artifact TTL 与引用计数。
- 跨租户**禁止**共享缓存。`segment_key` 的计算不含 tenant，但查询必须带 `tenant_id` 谓词，且 S28 的 artifact 本身有租户隔离。两者都要有。

---

## 8. 可观测

| 指标 | 类型 | 用途 |
|---|---|---|
| `composition_render_job_duration_seconds` | histogram（按 purpose） | formal 的 p50/p95 |
| `composition_segment_cache_hit_ratio` | gauge | 增量渲染效果 |
| `composition_gpu_utilization_ratio` | gauge（按 region/pool） | 容量 |
| `composition_render_failure_total` | counter（按 failure_code） | 分类失败 |
| `composition_orphaned_job_total` | counter | 节点稳定性 |
| `composition_loudness_gate_fail_total` | counter（按 tenant） | 规格漂移 |
| `composition_remix_ratio` | gauge | remix 占修复比例；过低说明在滥重生成 |
| `composition_s28_upload_retry_total` | counter | 存储压力 |
| `composition_usage_report_lag_seconds` | gauge | S23 记账延迟 |

**不回填 `tenant_id` 到 label 的无限基数集合**：按 tenant 的指标只在聚合层，或做 top-N。高基数会打爆监控。

trace span：

```
composition.render_job
  ├─ timeline.read
  ├─ s22.acquire_permit
  ├─ s28.head_inputs
  ├─ segment.render  (fan-out)
  │    ├─ filter_graph.execute
  │    └─ s28.upload
  ├─ concat
  ├─ loudnorm.measure
  └─ candidate.create
```

trace 维度：`tenant/episode/branch/timeline_revision/render_job_id/segment_no/candidate_id/trace_id`。

**日志不含**：媒体路径签名、滤镜图全文（改记 `graph_hash`）、用户文本。

---

## 9. 测试清单

### E03 —— 局部改镜只重渲染受影响区间

| 用例 | 期望 |
|---|---|
| E03-S42-01 | S41 diff 给 1 个区间 → 只有相交段重渲染，其余 `REUSED` |
| E03-S42-02 | 位移片段的重渲染字节与 S28 已有 artifact 相同 → 上传命中内容寻址，不新增对象 |
| E03-S42-03 | 断点续跑：kill worker 后 ORPHANED → 已完成段不重渲染 |
| E03-S42-04 | 拼接后总帧数 == EDL 计算帧数；差 1 帧即 FAILED |
| E03-S42-05 | 转场段不被切分（分段边界避开转场） |

### E24 —— 音频缺陷与 remix

| 用例 | 期望 |
|---|---|
| E24-S42-01 | `remix_defect` 的 mix job 输出候选，video artifact ref 与上一版逐字段相同 |
| E24-S42-02 | stems 不可用 → `STEMS_UNAVAILABLE`，不尝试从成品分离 |
| E24-S42-03 | `remix` 只消耗 `cpu_media_second`，不消耗 `gpu_second`（或按实际过滤） |
| E24-S42-04 | `mix_plan` 变化的两次 remix 都记录，且 `mix_plan_hash` 不同 |
| E24-S42-05 | S39 计划 `regenerate_video` 而非 `remix` 时，S42 走完整渲染路径（不擅自降级为 remix） |

### E37 —— 正式 Master 完整性门

| 用例 | 期望 |
|---|---|
| E37-S42-01 | `formal_master_eligible=false` + `purpose=formal` → 422 |
| E37-S42-02 | 缺 `master_spec_ref` → 422 `MASTER_SPEC_REQUIRED`，不默认 H.264 |
| E37-S42-03 | `stale_inputs` 非空 → `deliverable=false`，即使 `loudness_gate=PASSED` |
| E37-S42-04 | 响度不达标 → `loudness_gate=FAILED`，`deliverable=false` |
| E37-S42-05 | 渲染后字幕被改 → 候选记录 subtitle hash + stale 标记；S43 拒绝 |

### E22 —— 取消与结算

| 用例 | 期望 |
|---|---|
| E22-S42-01 | 取消 RUNNING job → `CANCELLING`，已用 GPU 秒仍上报 S23 |
| E22-S42-02 | 取消与完成竞争 → 只有一个终态，不双份用量 |
| E22-S42-03 | 许可到期后继续收尾，不中途终止；新段不再开始 |

### E12 —— 幂等

| 用例 | 期望 |
|---|---|
| E12-S42-01 | 同 command 同 body 重投 → 同一 `render_job_id`，不二次消耗 GPU |
| E12-S42-02 | 同 command 不同 body → 409 |
| E12-S42-03 | 用量上报重复 → S23 按 `usage_id` 去重 |

### E13 —— 崩溃恢复

| 用例 | 期望 |
|---|---|
| E13-S42-01 | 创建事务中崩溃 → 无 job、无 receipt |
| E13-S42-02 | 段上传成功后崩溃 → 重启对账 S28 命中，段置 `DONE` |
| E13-S42-03 | outbox 重投 → 候选事件不重复创建（S28 内容寻址 + 下游 inbox） |

### E28 —— 跨租户

| 用例 | 期望 |
|---|---|
| E28-S42-01 | 跨租户读 job → 404 |
| E28-S42-02 | 跨租户引用输入 artifact → S28 拒绝，S42 转 422 |
| E28-S42-03 | 临时目录按 `tenant/render_job_id` 隔离；并发租户不共享 |

### E35 —— 容量与 noisy neighbor

| 用例 | 期望 |
|---|---|
| E35-S42-01 | 模拟高负载下 worker 池按租户份额排队，无饥饿 |
| E35-S42-02 | 渲染资源与 S26 视频 API 并发额度互不影响（压测 S26 mock 限流时渲染不受阻） |

### 一致性与恢复

| 用例 | 期望 |
|---|---|
| C-S42-01 | 同输入重算 `graph_hash` 100 次一致 |
| C-S42-02 | 同 `segment_key` 跨 job 命中，字节一致 |
| C-S42-03 | `master_candidate` 的 `manifest_content_hash` 可从 manifest 复算 |
| C-S42-04 | 升级：新版本读旧 `filter_graph`（graph_version 兼容）可执行 |
| C-S42-05 | 回滚：旧版本不认识新节点 kind → 拒绝该 job，不静默产出 |

---

## 10. 实施顺序

| 步骤 | 内容 | 前置 |
|---|---|---|
| 1 | 固化 `filter-graph.v1.schema.json` / `master-candidate.v2.schema.json` / 事件 payload | W0 |
| 2 | `RenderProfile` 解析（引用 `master_spec_ref`）+ `render_env_hash` | 1 |
| 3 | `MixJob` + `FilterGraph` 音频路径 + 两遍 loudnorm（**先做音频，成本最低、验证最快**） | 2 |
| 4 | `RenderJob` 视频分段渲染 + S28 上传 | 3 |
| 5 | 拼接 + 总时长自检 + `MasterCandidate` | 4 |
| 6 | 段缓存与增量渲染（消费 S41 diff） | 4 |
| 7 | `remix` 路径（消费 S39） | 3, 5 |
| 8 | 用量上报 S23 + S22 许可 | 4 |
| 9 | `ORPHANED` 对账与续跑 | 4 |
| 10 | 压测与容量标定（真实 GPU 池） | 9 |

**第 3 步先做音频**：音频路径没有 GPU、没有编码器版本差异、验证极快，能最早暴露"计划 → 滤镜图 → 测量 → 候选"这条主干的问题。

**不接真实 S26**：S42 与 S26 无调用关系。**需要真实 GPU 才能验证的部分**（编码器版本差异、OOM 行为）属于实施会话授权范围，方案层面只用 mock 与规格档。

---

## 11. 冲突（报告，不选边）

| # | 冲突 | 双方原文 | 影响 |
|---|---|---|---|
| C1 | `master_spec_ref` 无权威 | docs/07 §5 说规格由"客户项目规格"确定；48 服务无"项目规格"服务；S41 方案 C1 已登记 | S42 无法解析规格 → 只能 422。不能在 S42 里发明默认值 |
| C2 | `MixJob` / `remix` 无命令入口 | service.yaml 只声明 `POST /render-jobs`；docs/11 §2 要求 BGM 盖对白走 `remix`；S35 §2.2.1 的 `audio_mix` 族 `action_family = remix → regenerate_video` | 本方案扩展 `POST /mix-jobs`（提案，§6.1）。冻结前 S39/S21 无法按契约调用 |
| C3 | 渲染进度如何进入实时投影未定义 | S47 方案要求"投影不能把旧进度显示成当前已完成"；S42 无事件定义 `progress` 字段；`run.events` 的 `Run.completed_nodes` 是节点级，不是段级 | 本方案在响应里给 `realtime_ref`（提案）。段级进度事件需与 S47 共同定义 |
| C4 | "混音计划"归属 | S41 `data_owner` 含混音计划；S42 需要它作为输入 | 按"S41 计划、S42 执行"实现（S41 §2.5），是解释不是裁决 |
| C5 | 渲染产物的可复现性无定义 | docs/04 要求"不可变内容按 hash 寻址"；但 GPU 编码本就非比特可复现 | 本方案区分"字节 hash（S28 事实）"与"语义复现（graph_hash + render_env_hash）"。若验收要求比特级复现，需要固定编码器与硬件 |
| C6 | S42 是否消耗 S23 预算无明文 | docs/12 §2 说 CPU 媒体与自托管 GPU"另配"；未说是否从 S23 预算扣 | 本方案上报用量但不预留；若需扣预算，应在 S22 许可阶段一并预留 |
| C7 | `depends_on` 未含 S41 | service.yaml 的 `depends_on` 与实现需要的 S41 timeline 读取不一致 | 本方案按事件 + 只读 API 依赖；需 owner 确认依赖表 |

---

## 12. 未决问题

1. **比特级复现**：客户是否要求"同一交付包可重建出字节相同的 master"？若要求，需要固定编码器版本与硬件，且 `render_env_hash` 必须进入交付证据。
2. **分段长度策略**：60 秒是规划值。短剧单集 2–5 分钟，分段过细会让拼接开销超过渲染收益；需要实测。
3. **GPU 池的租户份额**：S22 管许可，但**队列**在 S42。份额策略（公平排队 / 优先级 / 预留）需 S22 owner 与 S42 共同定义。
4. **预览渲染的降规格**：`purpose=preview` 是否允许降分辨率？降了之后预览与正式的一致性由谁保证？本方案不降，代价是预览也贵。
5. **stem 保留期**：`remix` 依赖 stems 存在。stem 的 TTL 归 S28，但"多久内必须能 remix"是产品承诺，需要与 S28 的保留策略对齐。
6. **`stale_inputs` 的处置策略**：是自动重渲染还是留给 S43 拒绝？本方案选后者（记录 + 交付门拒绝），因为自动重渲染在长片上可能永不收敛。
