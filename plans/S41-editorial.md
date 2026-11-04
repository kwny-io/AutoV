# S41 · 剪辑与时间线 —— 实现方案

状态：实现方案（W4）。`maturity: design_only` 不变；本文描述"照着能写代码"的形态，不是已实现系统。SLO 未实测。

上游依据：[docs/04](../docs/04-data-consistency.md)、[docs/07 §2/§5](../docs/07-production-workflows.md)、[docs/11 §2](../docs/11-quality-repair.md)、[docs/13 §3](../docs/13-api-product.md)、[S41 规格](../docs/design-package/services/S41-editorial.md)、`contracts/execution-context.v2.schema.json`、`contracts/episode-manifest.v2.schema.json`。

配套：[S38 字幕](S38-subtitles.md)（消费 timeline 差分）、[S39 返工决策](S39-repair-planning.md)（`temporal_edit` 动作）、[S08 版本快照](S08-revision-snapshot.md)（manifest 与 epoch）、[S42 混音合成](S42-composition.md)（执行渲染）。

---

## 0. 一句话边界

S41 拥有**时间线**：哪些片段、按什么顺序、在输出时间轴的哪个位置、以什么速度与转场拼接。

```
S41: 片段排列 + EDL + 差分（哪些输出区间变了）
S38: 按 EDL 把字幕时码映射到输出时间
S33/S34: 按 EDL 把音效/BGM 切点对齐到输出时间
S42: 按 EDL + 混音计划渲染出 master 候选
S08: 接受 master（唯一权威）
```

判据：任何"第 3 集第 7 分 12 秒现在是什么画面"的疑问，答案来自 S41 的 EDL + S28 的 artifact；任何"这一集现在被接受了吗"的疑问，答案来自 S08。**S41 从不推进接受指针。**

---

## 1. 责任与明确不负责

**唯一写者**（本服务凭证独占写）：

- `Timeline` —— 每 (tenant, series, episode, branch, timeline_kind) 一行，CAS 锚点
- `TimelineRevision` —— 不可变版本，含完整片段排列与参数
- `Clip` / `Track` / `LinkGroup` —— 版本内的结构
- `EDL` / `EDLEntry` —— 由 revision 确定性导出的输出时间清单
- `TimelineDiff` —— 相邻 revision 的差异与受影响输出区间
- `OutputPlan` / `MixPlan` —— 输出与混音的**计划**（不是执行）
- `Completeness` —— 该 revision 是否具备正式合成所需的全部素材

**明确不写：**

| 事实 | 权威 | S41 的正确做法 |
|---|---|---|
| 当前接受指针 / manifest / epoch / 节点锁 | **S08** | 提交后发事件，由 S08 决定是否推进 |
| 媒体 bytes、hash、存储位置、TTL | **S28** | 只保存 `artifact_ref` 与内容 hash，提交前只读校验 |
| 镜头内容（ShotSpec、对白原文、景别机位） | **S06** | 只引用 `shot_ref`，不复制字段 |
| 字幕文本 / 译文 / 版式 | **S38** | 只提供 EDL 与差分，S38 自己算时码 |
| 音效 / BGM 素材与切点内容 | **S33 / S34** | 只提供输出时间区间，S33/S34 自己切 |
| 实际渲染、编码、响度测量 | **S42** | 只产出 `OutputPlan` / `MixPlan`，不执行 |
| QC 报告与缺陷 | **S35 / S36 / S37 / S38** | 只引用报告，不自造分数 |
| 项目输出规格（分辨率/帧率/色彩/响度） | 未定（见 §11 C1） | 只引用 `master_spec_ref` |
| 金额、预算 | **S23** | 不估算、不预留、不记账 |
| 审批与权限判定 | **S02 / S01** | 拒绝时透传原因，不自行放行 |

S41 **不发起模型调用、不产生费用、不写媒体 bytes**。剪辑点可以由 `editor` Agent 提出（docs/08 §1），但那只是 `proposal_only` 的候选，由人或其他服务通过命令提交。

---

## 2. 实体与关键区分

### 2.1 三层对象：Timeline / TimelineRevision / EDL

| 对象 | 生命周期 | 可变性 | 作用 |
|---|---|---|---|
| `Timeline` | 长期 | 仅计数与指针可变 | 一集一分支一条时间线的并发锚点 |
| `TimelineRevision` | 一次提交 | **完全不可变** | 内容：片段排列 + 参数 + 来源引用 |
| `EDL` | 由 revision 派生 | **完全不可变** | 输出时间轴：record in/out、source in/out、速度、转场 |

三者的关系必须像 S08 的 `manifest / manifest_revision / node_state` 一样清楚：

- **`Timeline` 只有一行**，`current_revision` 单调递增。锁这一行 = 锁这一集这一分支的剪辑写序列。
- **`TimelineRevision` 一旦提交永不修改**。回滚 = 提交一个新 revision（内容等于旧 revision），而不是把指针拨回去。这与 docs/01 §3"回滚生成新 manifest，不改历史"同构。
- **`EDL` 不是独立事实**，它是 `TimelineRevision` 的确定性函数。任何时候都可以从 revision 重算 EDL；重算结果必须逐字段相同，否则是 bug。**不把 EDL 当作可以和 revision 分叉的第二份真相。**

**没有服务端 `DRAFT` 状态。** 和 S08 的候选 manifest 一样，草稿在客户端/编辑器手里；提交即 `COMMITTED`。这样"崩溃在提交前"的候选根本不存在于本库，E13 的"准备内容可回收"自动成立，不需要 GC 一个半成品 revision。

### 2.2 两种时间：source time 与 record time

这是本服务最容易写错的地方，必须先定死。

```
source time  = 素材文件自身的呈现时间（PTS），相对该 artifact 的第一帧
record time  = 时间线输出时间，从 0 开始
```

映射关系：

```
record_time(clip, source_time t) = rec_in + (t - src_in) * speed_den / speed_num
duration_record = duration_source * speed_den / speed_num
```

三条硬规则：

1. **内部统一用整数 tick**（建议 90 kHz，即 MPEG 基准）。所有 `src_in/src_out/rec_in/rec_out` 都是整数 tick。**中间步骤绝不取整到帧**，只在导出 EDL 时一次性取整。
2. **取整规则版本化**。`rounding_rule = "half_up_at_frame_boundary_v1"`，参与 `content_hash`。改取整规则 = 新的哈希 = 下游全部重算。不允许"悄悄升级取整实现"，那会让同一 revision 在不同时间导出不同 EDL。
3. **减速/加速不改变声音音高**这一点属于音频处理（S42 的滤镜图），不属于时间线。S41 只声明 `speed`，不声明"要不要 pitch-correct"——那是混音计划项，且**默认 true**（短剧里变调几乎总是缺陷）。

最容易的 bug：帧率是 `24000/1001` 这类非整数。**禁止把 fps 写成 float 再乘。** 全部用有理数 `fps_num/fps_den`，帧号 = `round_half_up(rec_ticks * fps_num / (fps_den * TICK_RATE))`。

### 2.3 trim / speed / ripple 的语义

| 操作 | 语义 | 对后续片段的影响 |
|---|---|---|
| `trim` | 改 `src_in` / `src_out`，不动 `rec_in` | 同轨及 linked tracks 的后续全部左移/右移（长度变了） |
| `speed` | 改 `speed_num/speed_den`，`src_in/src_out` 不变 | 同上，按新时长位移 |
| `ripple_insert` | 在 `at_rec` 插入时长 d | 该点之后（含 linked tracks）整体右移 d |
| `ripple_delete` | 删除区间 [a,b) | 之后整体左移 (b-a) |
| `move` | 改 `rec_in`，不改变时长 | **默认不 ripple**；越界与重叠由校验拒绝，除非显式 `mode: ripple` |
| `slip` | 改 `src_in`，`rec_in` 不变 | 不影响后续（源内容滑动） |
| `slide` | 前一片段 `src_out` 与后一片段 `src_in` 同向调整 | 不影响总长 |

**全部操作都产生一个新的完整 revision**，没有增量修改。`ripple` 是"生成新 revision 的算法"，不是一个可以回退的操作日志。理由：增量修改会让"当前时间线"依赖一长串操作历史的重放，重放一旦有实现差异就分叉；而完整 revision + CAS 让任何两个副本对同一 revision 的计算结果可比较。

### 2.4 LinkGroup：音画不同步的防线

短剧的时间线里，视频片段与它的同期声/对白音轨是**成组**的。`link_group` 把一组片段绑在一起：

- 对 `link_group` 中任一成员做 trim/speed/ripple/move，其余成员**同步位移与变速**。
- 解除链接是显式操作（`unlink`），且必须在 revision 里记录 `unlinked_at_revision`，供审计回答"哪一版把口型和声音拆开了"。
- 校验：同一 `link_group` 内成员的 `rec_in` 必须相等，`speed` 必须相等（除非显式 `allow_av_speed_drift: true`，此时**强制**在 revision 里写 `drift_reason`，因为这是口型缺陷的高发点）。

不加这条，最典型的事故是"视频剪掉 4 秒，音频没动"，到 S36 才发现口型全错，返工成本极高。

### 2.5 输出计划 / 混音计划 / master 的三分

| 对象 | 拥有者 | 内容 | 不含 |
|---|---|---|---|
| `OutputPlan` | **S41** | 时长、帧率、分辨率、segment 划分、要导出哪些 stem、字幕轨引用 | 编码器参数、滤镜链 |
| `MixPlan` | **S41**（计划） | 哪些 stem、目标响度引用、ducking 策略引用、segment 边界 | 实际滤波、实际测量 |
| `MasterCandidate` / `MixJob` | **S42**（执行） | 实际产物、实际测量值、渲染版本 | 片段排列 |

这条切分是**解释**，不是文档原话（见 §11 C2）。它成立的依据：`data_owner` 写 S41 持有"混音计划"，S42 持有"MixJob/成片候选"。**计划是数据，执行是行为**；计划可以有多个候选，执行必须绑定一个确定的计划快照。

`master_spec_ref`（分辨率/色彩/响度/编码）**不归 S41**：docs/07 §5 说"具体编码/色彩/响度由客户项目规格确定"，而 48 服务里没有"项目输出规格"服务（§11 C1）。S41 只引用，不定义，也不给默认值——给默认值就是造第二权威。

---

## 3. 状态机

### 3.1 `TimelineRevision`

```
        （客户端草稿，不在本库）
                 │ POST /timelines/{id}/revisions
                 ▼
           ┌───────────┐
           │ COMMITTED │────────── 被新 revision 取代 ──────► SUPERSEDED
           └───────────┘
                 │ execution_fence 失效 / 分支作废
                 └───────────────────────────────────────► VOIDED
```

- `COMMITTED` 是唯一入口状态。没有 `PENDING`：校验不通过的命令**不落库**。
- `SUPERSEDED` 仅表示"有更新的 revision"，历史仍可读、仍可作证据。**不删除。**
- `VOIDED` 用于 `execution_fence` 推进后把仍在途的候选标记为作废（与 S08 `manifest_revision.VOIDED` 同构）。`VOIDED` 的 revision **永远不能**再被 S42 用于渲染正式 master。
- **终态不回退。** 任何"把 SUPERSEDED 改回 COMMITTED"的路径都不存在。

### 3.2 `Timeline` 写序列

`Timeline` 本身没有业务状态机，只有计数与指针：

```
current_revision: 1 → 2 → 3 ...（严格单调）
lock_version:     每次提交 +1
execution_fence:  只由 S08 推进（消费 snapshot.manifest.committed）
```

**`execution_fence` 归 S08，S41 只是缓存它的投影。** 任何提交都必须带 `expected_execution_fence`；与本地投影不一致 → 409 `STALE_FENCE` 并回源 S08。这是"取消/灾备切主后旧写者不能继续写"的落点。

### 3.3 非法转移与拒绝码

| 尝试 | 结果 |
|---|---|
| 用旧 `expected_revision` 提交 | 409 `TIMELINE_REVISION_CONFLICT`（附 `current_revision`） |
| 提交时 `execution_fence` 已推进 | 409 `STALE_FENCE` |
| 引用不存在的 artifact | 422 `SOURCE_ARTIFACT_MISSING`（附 `clip_id`） |
| artifact hash 与引用不符 | 422 `SOURCE_ARTIFACT_MISMATCH` |
| speed ≤ 0 或非有理数 | 422 `INVALID_SPEED` |
| linked group 内 `rec_in`/`speed` 不一致且未声明 drift | 422 `LINK_GROUP_DESYNC` |
| 转场重叠超过素材 handle | 422 `INSUFFICIENT_HANDLES` |
| 片段超出轨道边界 / 负 `rec_in` | 422 `CLIP_OUT_OF_RANGE` |
| 同 `command_id` 不同 body | 409 `IDEMPOTENCY_MISMATCH` |
| 依赖 S28 暂时不可达 | 503 `DEPENDENCY_UNAVAILABLE`，**不落库** |
| S02 判定该动作不允许（如锁定节点） | 403 + S02 `reasons[].code`，**不自行解释** |

**422 与 503 必须分开**：前者是"这个请求本身不成立"，重试无用；后者是"现在判断不了"，重试有用。把它们混成一个 500 会让 S07 的自动重试逻辑失效（docs/13 §1："明确 budget/permission 错误不能伪装可重试 500"）。

---

## 4. 关键流程

### 4.1 提交流程

```
POST /timelines/{timeline_id}/revisions
  1. 认证：tenant / actor / scope 从网关与 S01 派生，header 不自授身份
  2. 幂等：查 (tenant, command_id)
        ├─ 命中且 request_hash 相同 → 返回原 revision（200）
        └─ 命中且不同           → 409 IDEMPOTENCY_MISMATCH
  3. 锁 Timeline 行（SELECT ... FOR UPDATE）
  4. 校验 expected_revision / expected_execution_fence
  5. 结构化校验 revision payload（§5 内容约束）
  6. 只读校验每个 clip 的 artifact_ref（S28）：
        ├─ 不存在             → 422 SOURCE_ARTIFACT_MISSING
        ├─ hash 不符          → 422 SOURCE_ARTIFACT_MISMATCH
        └─ S28 503            → 503，不落库
  7. 计算 EDL（确定性函数）
  8. 计算 completeness（§4.4）
  9. 计算 diff（相对 current_revision）
 10. 同一事务写：timeline_revision + clip + edl + diff + completeness
                 + timeline.current_revision/lock_version + outbox
 11. COMMIT
 12. 异步投递 post.timeline.revision.created
```

第 6 步是**提交时的实时复查**，不是建任务时的检查结果缓存。理由和 S40 一样：素材可能在两分钟前被删/被替换。**"刚才校验过"不是放行依据。**

第 7–9 步都是纯函数，可以单测穷举；任何不确定性（读 S28、读时钟）都不能进入这三步。这样 `content_hash` 才是可复算的。

### 4.2 局部改镜的传播链（核心）

docs/07 §2 的场景："第3场第2镜改仰拍、缩4秒"。S41 在链条里的位置是**把镜头级变化翻译成输出时间轴的变化**。

```
S07 解析意图 → S06 新 ShotSpec → S09 影响闭包 → S02 判定
   → S08 CAS 提交新 manifest（本镜头 node_epoch +1）
   → post.timeline.revision.created
        │
        ├─ S41 收到新的镜头时长/顺序 → 生成新 TimelineRevision
        │     └─ EDL 变化：本 clip 时长 -4s，后续同轨左移 4s
        │     └─ TimelineDiff：affected_output_ranges = [旧位置, 新位置 的并集]
        │     └─ 发 post.timeline.revision.created（含 diff）
        │
        ├─ S38 消费 diff → 只重算落在 affected ranges 的 cue（S38 §3.1）
        ├─ S33/S34 消费 diff → 只重切落在 affected ranges 的 SFX/BGM 切点
        ├─ S17 消费 diff → 对新 cut 做轴线/运动/光线连续性检查
        ├─ S42 消费 diff → 只重渲染 affected segment（§4.3）
        └─ S08 收到新 timeline revision 作为 post 节点的 source_revision
```

**谁触发 S41 生成新 revision？** 不是"自动监听所有镜头变化就重剪"——那会让任何一次无关改动都重算 EDL。规则：

| 触发 | S41 行为 |
|---|---|
| `snapshot.manifest.committed` 显示**本集**某个已入片的镜头 `node_epoch` 变化且该镜头在 timeline 上 | 标记 `timeline_stale=true`，生成**候选**新 revision（不自动提交），发 `post.timeline.stale`（缺口，§11 C3） |
| 用户显式剪辑命令 | 直接提交 |
| S39 计划了 `temporal_edit` | 由 S21 编排成一个显式剪辑命令，S41 执行 |
| 无关镜头变化（不在本 timeline 上） | 仅更新投影，不产生任何 revision |

"自动生成候选、不自动提交"这个取舍很重要：**剪辑是创作决定，不能因为上游改了 4 秒就替导演决定怎么剪。** docs/07 §2 说"重算 EDL、字幕时码、音效/BGM 切点"，重算的是**由新时长决定的那部分**；至于多出来的 4 秒是补空镜、拉伸、还是保持黑场，必须有人（或明确的策略）决定。

### 4.3 差分结构与受影响区间

`TimelineDiff` 是 S41 对下游最重要的产出。结构：

```jsonc
{
  "from_revision": 9,
  "to_revision": 10,
  "diff_hash": "sha256:...",
  "clips": {
    "added":   [{"clip_id":"c_18","track_id":"v1","rec_in":120000,"rec_out":480000}],
    "removed": [{"clip_id":"c_12","track_id":"v1"}],
    "changed": [
      {"clip_id":"c_11","track_id":"v1",
       "fields":["src_out","rec_out"],
       "old":{"src_in":0,"src_out":360000,"rec_in":0,"rec_out":360000,"speed_num":1,"speed_den":1},
       "new":{"src_in":0,"src_out":0,  "rec_in":0,"rec_out":0,  "speed_num":1,"speed_den":1}}
    ],
    "shifted": [                      // 只位移、内容未变
      {"clip_id":"c_13","track_id":"v1","delta_ticks":-360000}
    ]
  },
  "affected_output_ranges": [         // 合并后的闭开区间，单位 tick
    {"start":0,"end":600000},
    {"start":3600000,"end":4200000}
  ],
  "affected_tracks": ["v1","a1","a_sfx","a_bgm"],
  "change_causes": [
    {"kind":"shot_duration_changed","shot_ref":{"id":"shot_3_2","revision":5},
     "delta_ticks":-360000}
  ]
}
```

三条规则：

1. **`affected_output_ranges` 是并集，不是逐 clip 列表。** 下游要的是"哪几段要重算"，逐 clip 会让 S38/S42 各写一套区间合并逻辑，容易漏重叠。S41 合并好再发。
2. **区间必须覆盖旧位置与新位置**。一个片段从第 10 秒移到第 2 秒，受影响区间是 `[2s, 10s+dur]` 的并集——否则下游会留下旧位置上的残影。
3. **`shifted` 单独一类。** 位移的片段内容没变，S42 可以复用它的已渲染 segment（只是搬位置）；把位移和内容变混为一谈会导致不必要的重渲染，直接推高成本。

`affected_output_ranges` 是**保守超集**：宁可多算一段，不可少算。多算的代价是一次多余重渲染；少算的代价是交付里字幕对不上口型。

### 4.4 completeness：缺素材与"不能提交正式合成"

service.yaml 验收："缺必要素材不能提交正式合成"。实现为 revision 上的计算字段：

```jsonc
{
  "completeness": "complete" | "incomplete",
  "gaps": [
    {"kind":"missing_artifact","clip_id":"c_12","expected_ref":{...}},
    {"kind":"hash_mismatch","clip_id":"c_14","expected":"sha256:...","actual":"sha256:..."},
    {"kind":"missing_subtitle_track","locale":"zh-CN"},
    {"kind":"missing_stem","stem":"dialogue"},
    {"kind":"insufficient_handles","clip_id":"c_13","required_ticks":24000,"available_ticks":0},
    {"kind":"rights_expired","asset_ref":{"service_id":"S15","id":"voice_...","revision":3}}
  ]
}
```

规则：

- **`incomplete` 的 revision 可以提交、可以预览**，但 `formal_master_eligible=false`。
- S42 收到 `formal_master_eligible=false` 的 revision 做正式 master → **422 `INCOMPLETE_TIMELINE`**，不是"尽力而为"。这是 E37 在 S41/S42 之间的落点。
- `gaps` 是提交时刻的**快照**，不是永远有效。S42 渲染前必须重新向 S28/S38/S15 复查（素材可能刚补上，也可能刚撤销）。**快照用于解释，实时复查用于放行。**
- 权利类 gap（`rights_expired`）**只能由 S15/S02 解除**，S41 不缓存"已授权"结论（docs/10："S10 解析成功不代表权利永远有效"）。

### 4.5 失败矩阵

| 失败点 | 检测 | 行为 | 不变量 |
|---|---|---|---|
| S28 读超时 | deadline 500ms | 提交返回 503，**不落库** | 不产生引用不存在素材的 revision |
| S28 返回 hash 不符 | 提交校验 | 422 + gap 明细 | 不静默用旧 hash |
| 提交事务内崩溃 | 服务重启 | 事务整体回滚；幂等表也回滚 | 无半成品 revision |
| 事务提交后、outbox 投递前崩溃 | relay 重启 | outbox 幂等重投 | 下游至少一次收到，按 event_id 去重 |
| 下游 S38 处理失败 | DLQ | S41 不做补偿；S38 自己重试 | S41 不因下游失败回滚已提交 revision |
| `execution_fence` 推进 | 提交时校验 | 409 `STALE_FENCE` | 旧 fence 的 revision 永不用于正式渲染 |
| 同一 revision 重算 EDL 结果不同 | 自检 | 告警 + 拒绝提交 | EDL 是 revision 的纯函数 |
| 并发两个剪辑命令 | 行锁 + CAS | 后到者 409 | 单写序列 |
| 大量片段的 diff 计算超时 | 计时 | 降级为"全量受影响" | 保守超集可接受，漏算不可接受 |

最后一条值得展开：diff 的最坏情况是 O(n) 对齐（用 clip_id 做键）。如果一集 800 个片段、10 条轨，diff 仍在毫秒级。真正会炸的是**把区间合并写成 O(n²)**。实现上用按 `start` 排序后线性扫描合并，并在 diff 超过 500 个区间时打 `diff_explosion` 告警——那说明时间线已经碎到人无法维护，是产品问题不是性能问题。

---

## 5. 数据模型

数据库 `editorial`，Postgres。所有表带 `tenant_id`，主键含 `tenant_id`（防跨租户 hash 存在性探测，E28）。无金额、无浮点计算（tick 用 bigint，`numeric` 只用于展示）。

```sql
CREATE TABLE editorial.timeline (
  tenant_id          text NOT NULL,
  timeline_id        text NOT NULL,
  project_id         text NOT NULL,
  series_id          text NOT NULL,
  episode_id         text NOT NULL,
  branch_id          text NOT NULL,
  timeline_kind      text NOT NULL DEFAULT 'master'
                     CHECK (timeline_kind IN ('master','rough_cut','trailer','alt')),
  current_revision   integer NOT NULL CHECK (current_revision >= 1),
  lock_version       bigint  NOT NULL DEFAULT 0,
  execution_fence    bigint  NOT NULL CHECK (execution_fence >= 1),
  master_spec_ref    jsonb,                 -- 引用，不定义（§11 C1）
  fps_num            integer NOT NULL CHECK (fps_num > 0),
  fps_den            integer NOT NULL CHECK (fps_den > 0),
  tick_rate          integer NOT NULL DEFAULT 90000 CHECK (tick_rate > 0),
  state              text NOT NULL DEFAULT 'ACTIVE'
                     CHECK (state IN ('ACTIVE','FROZEN','CLOSED')),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, timeline_id),
  CONSTRAINT timeline_identity_uniq
    UNIQUE (tenant_id, series_id, episode_id, branch_id, timeline_kind),
  CONSTRAINT timeline_fps_reduced CHECK (fps_num > 0 AND fps_den > 0)
);
CREATE INDEX timeline_episode_idx ON editorial.timeline (tenant_id, episode_id, branch_id, timeline_kind);

CREATE TABLE editorial.timeline_revision (
  tenant_id          text NOT NULL,
  timeline_id        text NOT NULL,
  revision           integer NOT NULL CHECK (revision >= 1),
  content_hash       char(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  base_revision      integer NOT NULL,          -- 本次 CAS 的 expected_revision
  state              text NOT NULL
                     CHECK (state IN ('COMMITTED','SUPERSEDED','VOIDED')),
  -- 绑定：这个 revision 是"基于哪一版生产状态"剪出来的
  built_against      jsonb NOT NULL,            -- {manifest_ref, execution_fence, node_epoch,
                                                --  shot_revisions:[...], memo_checkpoint}
  -- 完整内容（片段排列 + 参数），不可变
  payload            jsonb NOT NULL,
  -- 派生
  edl_hash           char(64) NOT NULL,
  duration_ticks     bigint NOT NULL CHECK (duration_ticks >= 0),
  completeness       text NOT NULL CHECK (completeness IN ('complete','incomplete')),
  gaps               jsonb NOT NULL DEFAULT '[]'::jsonb,
  rounding_rule      text NOT NULL DEFAULT 'half_up_at_frame_boundary_v1',
  change_cause       jsonb,                     -- 从 S07 透传；无则为手工剪辑
  committed_by       jsonb NOT NULL,
  committed_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, timeline_id, revision),
  CONSTRAINT tr_base_lt CHECK (base_revision < revision)
);
CREATE UNIQUE INDEX tr_hash_uniq ON editorial.timeline_revision (tenant_id, timeline_id, content_hash);
CREATE INDEX tr_state_idx ON editorial.timeline_revision (tenant_id, timeline_id, state, revision DESC);
CREATE INDEX tr_manifest_idx ON editorial.timeline_revision
  USING gin ((built_against -> 'manifest_ref'));

CREATE TABLE editorial.clip (
  tenant_id          text NOT NULL,
  timeline_id        text NOT NULL,
  revision           integer NOT NULL,
  clip_id            text NOT NULL,             -- 稳定 ID，跨 revision 不变
  track_id           text NOT NULL,
  track_kind         text NOT NULL
                     CHECK (track_kind IN ('video','dialogue','sfx','bgm','subtitle','overlay')),
  link_group_id      text,                       -- 音画同步组
  ordinal            integer NOT NULL,           -- 同轨顺序
  src_artifact_ref   jsonb NOT NULL,             -- {service_id,id,revision,content_hash}
  src_in_ticks       bigint NOT NULL CHECK (src_in_ticks >= 0),
  src_out_ticks      bigint NOT NULL,
  rec_in_ticks       bigint NOT NULL CHECK (rec_in_ticks >= 0),
  rec_out_ticks      bigint NOT NULL,
  speed_num          integer NOT NULL DEFAULT 1 CHECK (speed_num > 0),
  speed_den          integer NOT NULL DEFAULT 1 CHECK (speed_den > 0),
  transition_in      jsonb,                      -- {kind,frames}|null
  transition_out     jsonb,
  unlinked_at_revision integer,
  drift_reason       text,
  PRIMARY KEY (tenant_id, timeline_id, revision, clip_id),
  FOREIGN KEY (tenant_id, timeline_id, revision)
    REFERENCES editorial.timeline_revision (tenant_id, timeline_id, revision),
  CONSTRAINT clip_src_order CHECK (src_out_ticks > src_in_ticks),
  CONSTRAINT clip_rec_order CHECK (rec_out_ticks > rec_in_ticks),
  CONSTRAINT clip_av_drift_needs_reason
    CHECK (drift_reason IS NOT NULL OR link_group_id IS NULL
           OR unlinked_at_revision IS NOT NULL)
);
CREATE INDEX clip_track_idx ON editorial.clip (tenant_id, timeline_id, revision, track_id, ordinal);
CREATE INDEX clip_link_idx  ON editorial.clip (tenant_id, timeline_id, revision, link_group_id)
  WHERE link_group_id IS NOT NULL;
CREATE UNIQUE INDEX clip_track_pos_uniq ON editorial.clip
  (tenant_id, timeline_id, revision, track_id, rec_in_ticks);
-- 注：同一时刻同轨只能有一个片段起点；重叠由应用层按 transition 判定，不塞进唯一约束
```

`clip_track_pos_uniq` 只约束"起点不重合"，不约束"区间不重叠"——因为转场本来就要重叠。区间重叠的合法性由应用层校验（转场必须有 handle），把它写成排他约束会误杀合法转场。

```sql
CREATE TABLE editorial.edl_entry (
  tenant_id          text NOT NULL,
  timeline_id        text NOT NULL,
  revision           integer NOT NULL,
  entry_no           integer NOT NULL,
  clip_id            text NOT NULL,
  track_id           text NOT NULL,
  record_in_frame    bigint NOT NULL,
  record_out_frame   bigint NOT NULL,
  src_in_ticks       bigint NOT NULL,
  src_out_ticks      bigint NOT NULL,
  speed_num          integer NOT NULL,
  speed_den          integer NOT NULL,
  transition_in      jsonb,
  transition_out     jsonb,
  PRIMARY KEY (tenant_id, timeline_id, revision, track_id, entry_no),
  CONSTRAINT edl_frame_order CHECK (record_out_frame > record_in_frame)
);
CREATE INDEX edl_revision_idx ON editorial.edl_entry (tenant_id, timeline_id, revision);

CREATE TABLE editorial.timeline_diff (
  tenant_id          text NOT NULL,
  timeline_id        text NOT NULL,
  from_revision      integer NOT NULL,
  to_revision        integer NOT NULL,
  diff_hash          char(64) NOT NULL,
  affected_ranges    jsonb NOT NULL,            -- [{start,end}] ticks，已合并
  affected_tracks    text[] NOT NULL,
  payload            jsonb NOT NULL,            -- 完整 TimelineDiff
  created_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, timeline_id, from_revision, to_revision),
  CONSTRAINT diff_forward CHECK (to_revision > from_revision)
);

CREATE TABLE editorial.output_plan (
  tenant_id          text NOT NULL,
  timeline_id        text NOT NULL,
  revision           integer NOT NULL,
  plan_hash          char(64) NOT NULL,
  segments           jsonb NOT NULL,            -- [{segment_no, rec_in, rec_out, est_seconds}]
  stems              jsonb NOT NULL,            -- ["dialogue","sfx","bgm","mix"]
  subtitle_track_ref jsonb,
  master_spec_ref    jsonb,
  PRIMARY KEY (tenant_id, timeline_id, revision)
);

CREATE TABLE editorial.mix_plan (
  tenant_id          text NOT NULL,
  timeline_id        text NOT NULL,
  revision           integer NOT NULL,
  plan_hash          char(64) NOT NULL,
  target_lufs        numeric(5,2),              -- 引用 master_spec 的值，不发明
  true_peak_ceiling  numeric(5,2),
  ducking_policy_ref jsonb,                     -- 引用 S02/S34 的策略
  stem_gains         jsonb NOT NULL,
  notes              text,
  PRIMARY KEY (tenant_id, timeline_id, revision)
);

CREATE TABLE editorial.command_receipt (
  tenant_id          text NOT NULL,
  command_id         text NOT NULL,
  request_hash       char(64) NOT NULL,
  timeline_id        text NOT NULL,
  revision           integer NOT NULL,
  response_body      jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);
CREATE UNIQUE INDEX command_receipt_hash_uniq
  ON editorial.command_receipt (tenant_id, command_id, request_hash);

CREATE TABLE editorial.outbox (
  event_id           uuid PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,
  aggregate_sequence bigint NOT NULL,
  event_type         text NOT NULL,
  partition_key      text NOT NULL,      -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz
);
CREATE INDEX outbox_unpublished_idx ON editorial.outbox (created_at)
  WHERE published_at IS NULL;

CREATE TABLE editorial.inbox (
  consumer           text NOT NULL,
  event_id           uuid NOT NULL,
  received_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

**不建的表（刻意的）：**

- 不建 `clip_operation_log`。操作历史由 revision 链表达；建操作日志等于承认"当前状态 = 历史重放"，与 CAS 模型冲突。
- 不建 `edl_cache`。EDL 是纯函数，缓存只会引入"缓存与 revision 不一致"。
- 不建 `render_state`。渲染状态归 S42；S41 只看 `post.master.rendered` 的引用。

---

## 6. 对外契约

### 6.1 命令

`POST /timelines/{timeline_id}/revisions`（service.yaml，OpenAPI 未定义 → §11 C3）

```jsonc
{
  "context": { ...execution-context v2 子集，必含 tenant/project/series/episode/branch/
               manifest_ref/execution_fence/node_id/node_epoch/actor/command_id/
               request_hash/trace_id... },
  "expected_revision": 9,
  "expected_execution_fence": 2,
  "timeline_kind": "master",
  "fps": {"num": 24000, "den": 1001},
  "tracks": [{"track_id":"v1","kind":"video"}, {"track_id":"a1","kind":"dialogue"}],
  "clips": [
    {
      "clip_id": "c_11", "track_id": "v1", "link_group_id": "lg_3", "ordinal": 0,
      "src_artifact_ref": {"service_id":"S28","id":"art_...","revision":3,
                           "content_hash":"sha256:..."},
      "src_in_ticks": 0, "src_out_ticks": 360000,
      "rec_in_ticks": 0,
      "speed": {"num":1,"den":1},
      "transition_in": null,
      "transition_out": {"kind":"crossfade","frames":12}
    }
  ],
  "output_plan": {"stems":["dialogue","sfx","bgm","mix"], "subtitle_locale":"zh-CN"},
  "mix_plan": {"target_lufs":-16.0,"true_peak_ceiling":-1.0,
               "ducking_policy_ref":{"service_id":"S02","id":"mix_default","revision":1,
                                     "content_hash":"..."}},
  "change_cause": {"kind":"shot_duration_changed",
                   "shot_ref":{"id":"shot_3_2","revision":5}},
  "command_id": "...",
  "request_hash": "sha256:..."
}
```

响应 `201`：

```jsonc
{
  "timeline_id":"tl_ep3", "revision":10, "content_hash":"sha256:...",
  "duration_ticks": 5400000, "completeness":"complete", "gaps":[],
  "formal_master_eligible": true,
  "diff":{"from_revision":9,"to_revision":10,"affected_ranges":[...]},
  "edl_ref":{"timeline_id":"tl_ep3","revision":10,"edl_hash":"sha256:..."},
  "outbox_event_id":"..."
}
```

**`201` 不表示"已接受"**，只表示"新 revision 已提交"。接受由 S08 决定。UI 文案必须区分这两件事（否则会重演 S40 §5 的陷阱）。

### 6.2 查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/timelines/{id}/revisions/{revision}` | revision + EDL + completeness；带 `source_revision` 与 `projection_watermark`（若读投影） |
| GET | `/timelines/{id}/diff?from=&to=` | 差分 |
| GET | `/timelines/{id}/edl/{revision}?format=json\|otio` | EDL 导出（OTIO 是映射，不是新事实） |
| GET | `/timelines?episode_id=&branch_id=` | 列表 |

**跨租户一律 404，不返回 403。** 403 会泄漏"这个 id 存在但不属于你"（E28）。

### 6.3 事件

| 方向 | 事件 / 调用 |
|---|---|
| 发布 | `post.timeline.revision.created`（topic `drama.editorial.v2`，key `tenant_id:timeline_id`） |
| 建议补充（缺口 C3） | `post.timeline.stale`（上游变化导致当前 timeline 需要重剪）、`post.timeline.completeness.changed` |
| 消费 | `snapshot.manifest.committed`（S08，epoch/fence 与镜头变化）、`artifact.ingested`（S28，素材可用性）、`quality.repair.planned`（S39，`temporal_edit`）、`production.sfx.ready`/`production.music.ready`（S33/S34） |
| 调用 | S28 `GET /artifacts/{id}`（只读校验）；S01 `get_authorization_context` |

`post.timeline.revision.created` 的 `data`：

```jsonc
{
  "timeline_id":"tl_ep3","revision":10,"content_hash":"sha256:...",
  "duration_ticks":5400000,
  "completeness":"complete","formal_master_eligible":true,
  "built_against":{"manifest_ref":{...},"execution_fence":2,"node_epoch":4},
  "diff_ref":{"timeline_id":"tl_ep3","from_revision":9,"to_revision":10,
              "diff_hash":"sha256:..."},
  "affected_ranges":[...],      // 冗余进事件：下游不必再回源拉 diff 才能决定要不要重算
  "affected_tracks":["v1","a1","a_sfx","a_bgm"]
}
```

**`data` 不含片段完整 payload 与媒体 URL**。大负载用 `data_ref` 指向 revision（docs/04 §4）。把 800 个片段塞进事件会让 Kafka 消息爆掉，也会让每个消费者都解析一遍它不关心的东西。

---

## 7. 幂等与并发

### 7.1 幂等键

统一 `tenant_id + command_id + request_hash`（AGENTS.md）：

- 同键同 body → 返回原 `revision`，**不新建**。
- 同键不同 body → **409 `IDEMPOTENCY_MISMATCH`**，不是覆盖。
- `request_hash` 的计算**不含** `trace_id` 与时间戳；含 `context` 的业务字段、`clips`、`expected_revision`。否则同一逻辑命令换个 trace 就是不同的幂等键。
- 幂等记录与 revision 同事务写入。事务回滚则幂等记录也回滚——不能出现"幂等表说有这个 command，但 revision 不存在"。

### 7.2 并发

| 场景 | 机制 |
|---|---|
| 同 timeline 两个剪辑命令 | `SELECT ... FOR UPDATE` 锁 `timeline` 行，`expected_revision` CAS |
| 跨 timeline 并发 | 互不影响（不同行） |
| 与 S08 的 epoch 推进并发 | 提交时校验 `expected_execution_fence`；S08 的推进是权威 |
| 两个服务实例同时 relay outbox | `FOR UPDATE SKIP LOCKED` + `published_at` 幂等标记 |
| 同一事件被 S41 重复消费 | `inbox(consumer, event_id)` 唯一约束 |

**锁顺序**：本服务只锁一行（timeline），不涉及跨表锁序。如果将来要跨表（例如批量重排多集），锁序固定为 `series → episode → timeline_id` 字典序，与 S23 的多级预算锁序同构。

**隔离级别**：`READ COMMITTED` 足够，因为并发保护靠显式行锁与 CAS，不靠快照隔离。用 `SERIALIZABLE` 会在高冲突时间线上产生大量重试，得不偿失。

### 7.3 与 S08 的边界（最容易越界的地方）

- S41 **不写** `accepted_pointer`，**不发**"已接受"语义的事件。
- `built_against.node_epoch` 是 S41 记录的"我基于哪一版剪的"，**不是**当前 epoch 的权威。S08 在 CAS 时会重新校验。
- S41 的 `VOIDED` 只影响自己：`execution_fence` 推进后，S41 把在途候选标记作废；S08 那边是否接受由 S08 决定。**两边都做，但不是同一把锁。**
- 如果 S08 接受了基于旧 epoch 的 timeline revision，那是 S08 的 bug，不是 S41 可以"顺手修正"的东西。

---

## 8. 可观测

### 8.1 指标（低基数，无用户文本）

| 指标 | 类型 | 用途 |
|---|---|---|
| `editorial_revision_commit_latency_ms` | histogram | 提交 p50/p95/p99 |
| `editorial_diff_size` | histogram | 受影响区间数；>500 告警 |
| `editorial_completeness_incomplete_total` | counter（按 `gap_kind`） | 素材缺口 |
| `editorial_link_group_reject_total` | counter | 音画同步校验命中 |
| `editorial_stale_fence_reject_total` | counter | 旧 fence 写入被挡 |
| `editorial_downstream_recompute_range_seconds` | histogram | 传播链成本 |
| `editorial_outbox_lag_seconds` | gauge | Outbox 积压 |
| `editorial_timeline_revision_rate` | counter（按 tenant） | 热点租户 |

**`gap_kind` 与 `track_kind` 可以进 label；`clip_id`、`shot_id`、用户文本不进**（docs/14 §3）。ID 留在 trace/log。

### 8.2 trace

span 结构：

```
editorial.commit
  ├─ idempotency.lookup
  ├─ timeline.lock
  ├─ s28.validate_clip_artifacts   (fan-out, 每个 clip 一个子 span)
  ├─ edl.compute
  ├─ diff.compute
  ├─ db.commit (含 outbox)
  └─ outbox.publish (异步)
```

trace 维度：`tenant/series/episode/branch/manifest_revision/execution_fence/timeline_id/revision/command_id/trace_id`。与 docs/14 §3 的统一 trace 对齐：S41 的 span 挂在 `manifest → node` 之下。

### 8.3 日志与审计

每次提交写一条结构化日志与一条审计意图（S45）：

```
actor / tenant / timeline_id / revision / base_revision / content_hash /
completeness / gap_kinds / affected_ranges_count / change_cause / trace_id
```

**不记片段全文、不记媒体 URL 签名。** 审计需要"谁改了哪一版"和"改了什么类别"，不需要重放整个时间线（重放走 revision payload）。

---

## 9. 测试清单

> 全部可在不接真实模型、不接真实渲染的前提下跑。S28/S38/S42 用契约 mock。

### E03 —— 仰拍 4 秒局部改镜：下游时码正确

| 用例 | 期望 |
|---|---|
| E03-S41-01 | 某 clip 时长 -4s，同轨后续片段 `rec_in` 全部 -4s；`affected_ranges` 覆盖旧尾与新尾 |
| E03-S41-02 | linked group 的视频与同期声同时位移；单独位移一个成员 → 422 `LINK_GROUP_DESYNC` |
| E03-S41-03 | 位移的片段进入 `shifted` 而非 `changed`；S42 mock 断言"只搬 segment、不重渲染" |
| E03-S41-04 | 新 revision 的 EDL frame 边界与 `rounding_rule` 一致；重算两次字节相同 |
| E03-S41-05 | 非整数帧率（24000/1001）下，累计 20 个片段后总时长与逐段求和误差为 0 tick |
| E03-S41-06 | 受影响区间合并正确：两个相邻变更合并成一个区间，不产生重叠区间 |

### E37 —— 缺素材 / 正式交付

| 用例 | 期望 |
|---|---|
| E37-S41-01 | 引用不存在的 artifact → 422，revision 不落库 |
| E37-S41-02 | artifact hash 不符 → 422 `SOURCE_ARTIFACT_MISMATCH`，gap 里带 clip_id |
| E37-S41-03 | 缺 stem → revision 可提交但 `formal_master_eligible=false` |
| E37-S41-04 | S42 mock 收到 `formal_master_eligible=false` → 拒绝正式 master，返回 422 |
| E37-S41-05 | 提交时 S28 503 → 503，不落库，可重试 |
| E37-S41-06 | rights_expired gap 在 S42 渲染前复查时仍存在 → 拒绝；补上后重新复查通过 |

### E07 —— 旧 epoch 不能推进当前接受指针

| 用例 | 期望 |
|---|---|
| E07-S41-01 | 提交带旧 `expected_execution_fence` → 409 `STALE_FENCE` |
| E07-S41-02 | revision 的 `built_against.node_epoch=4`，S08 已到 5 → S08 CAS 拒绝；S41 不改任何本地状态 |
| E07-S41-03 | `VOIDED` 的 revision 请求正式渲染 → 422 |
| E07-S41-04 | S41 从不写 accepted pointer（静态断言：无该字段、无该调用） |

### E12 —— 幂等与版本冲突

| 用例 | 期望 |
|---|---|
| E12-S41-01 | 同 command_id 同 body 重投 → 同一个 `revision`，不新建 |
| E12-S41-02 | 同 command_id 不同 body → 409 |
| E12-S41-03 | 不同 command 同 `expected_revision` 并发 → 一个成功一个 409 |
| E12-S41-04 | 幂等记录写入与 revision 同事务：事务回滚后重投仍能成功 |

### E13 —— 崩溃恢复

| 用例 | 期望 |
|---|---|
| E13-S41-01 | 提交事务中崩溃 → 无 revision、无幂等记录、无 outbox |
| E13-S41-02 | 提交后 relay 前崩溃 → 重启后 outbox 重投，下游只收到一次有效事件 |
| E13-S41-03 | relay 重复投递 → 下游 inbox 去重，S41 不重复计 revision |
| E13-S41-04 | 重启后未完成的 diff 计算不留下半写行 |

### E28 —— 跨租户

| 用例 | 期望 |
|---|---|
| E28-S41-01 | 跨租户读 timeline → 404（不是 403） |
| E28-S41-02 | 跨租户引用 artifact → S28 侧拒绝；S41 转 422，不泄漏存在性 |
| E28-S41-03 | 所有 SQL 断言带 `tenant_id` 谓词（静态检查） |

### E24 —— 字幕/时码协同（与 S38 联合）

| 用例 | 期望 |
|---|---|
| E24-S41-01 | EDL 缩短 1s → S38 mock 收到 `affected_ranges` 并只重算落在其中的 cue |
| E24-S41-02 | 只改一个 clip → 其他 cue 的 start/end 逐字段不变（联合断言） |

### 一致性与恢复

| 用例 | 期望 |
|---|---|
| C-S41-01 | 同一 revision 的 EDL 重算 100 次结果一致 |
| C-S41-02 | 大时间线（800 clip × 6 track）diff < 200ms，区间合并线性 |
| C-S41-03 | `snapshot.manifest.committed` 乱序/重复 → 本地 fence 投影不回退 |
| C-S41-04 | 升级：新版本读旧 revision payload（expand 兼容），EDL 结果不变 |
| C-S41-05 | 回滚：旧版本读新 revision（字段可选），不崩溃 |

---

## 10. 实施顺序

S41 在 docs/16 的 **W4** 波次。但它的 EDL 契约要先于 S42 冻结。

| 步骤 | 内容 | 前置 |
|---|---|---|
| 1 | 固化 `timeline-revision.v2.schema.json` / `timeline-diff.v2.schema.json` / 事件 payload schema | W0 契约框架 |
| 2 | `timeline` + `timeline_revision` + CAS + 幂等 | 1 |
| 3 | 结构化校验 + linked group 校验 | 2 |
| 4 | EDL 确定性导出 + 取整规则 | 2 |
| 5 | diff + 区间合并 | 4 |
| 6 | completeness（S28 只读复查） | 3 |
| 7 | `post.timeline.revision.created` + outbox | 2 |
| 8 | S08 epoch/fence 投影（消费事件） | 7 |
| 9 | S38/S42 联调（mock） | 5, 6 |
| 10 | OTIO 导出、性能与压测 | 9 |

**第 1 步是阻塞项**：没有差分 schema，S38 的 C3 缺口无法关闭，S42 也无法按区间做增量渲染。

**不做真实渲染、不做真实媒体处理**：S41 全程只操作引用与时间数字。任何需要解码媒体才能回答的问题（如"这个转场有没有足够 handle"）应改为读 S28 的 artifact 元数据，而不是自己抽帧。

---

## 11. 冲突（报告，不选边）

| # | 冲突 | 双方原文 | 影响 |
|---|---|---|---|
| C1 | 项目输出规格的归属 | docs/07 §5："具体编码/色彩/响度由客户项目规格确定"；docs/02 的 48 服务里没有"项目输出规格"服务；S41 `data_owner` 也不含它 | S41/S42 都需要 `master_spec_ref`，但无权威可引用。本方案只留引用，不定义默认值 |
| C2 | "混音计划"的归属 | S41 `data_owner` 含"混音计划"；S42 `data_owner` 含 `MixJob`；docs/07 §5 把 stems/EDL 列为标准输出 | 本方案按"S41 计划、S42 执行"实现（§2.5），这是解释，不是文档裁决 |
| C3 | S41 事件 payload 无 schema；且 OpenAPI 无 S41 的 `POST /timelines/{id}/revisions` | `contracts/openapi.v2.yaml` 只有 S41 的查询入口（实际也没有），service.yaml 声明了命令入口；S38 方案 C3 已登记"timeline 事件未定义差分语义" | 本文 §6.3 给出的 payload 是**提案**；冻结前 S38/S42 只能按字段名约定联调 |
| C4 | "复用未改文本/音色与足够短的 TTS"无归属 | docs/07 §2 要求复用；48 服务里没有"复用决策"字段或服务 | 本方案不实现复用决策，只登记为 S21/S39 职责；若要求 S41 提供"哪些片段可复用"的输入，需要文档明确 |
| C5 | timeline 是否作为 manifest 的节点 | `episode-manifest.v2` 的 `node_states` 是通用节点数组，但未规定 timeline 的 `node_id` 命名；S08 方案也未定义 post 节点 | 本方案假设 `node_id = "post.timeline"`、timeline revision 作为 `source_revisions` 一项；需 S08 owner 确认 |
| C6 | `post.timeline.revision.created` 的 `created` 语义 | 事件名是 created，但实现语义是"新 revision 已提交（committed）" | 消费者不得把 `created` 理解为"已被接受"。本文在 §6.3 明确 payload 语义；若要改名为 `committed` 需走事件契约升级 |

---

## 12. 未决问题

1. **C1 的项目规格服务**：如果不新增服务，`master_spec_ref` 指向哪里？`customer-configs/<tenant>/` 是版本化配置目录（README），但它不是服务、没有 API、没有 content_hash。需要一个可寻址的权威。
2. **自动重剪策略**：上游镜头变化后，S41 生成"候选 revision"的规则（保持时长 / 补空镜 / 黑场）是产品决定，不是 S41 能决定的。需要 S02 或项目级策略字段。
3. **多语言字幕轨**：一个 timeline 对应多个 `target_locale` 的 subtitle track，还是每个 locale 一条 timeline？本方案假设"一条 timeline、多条字幕轨"，但 `output_plan.subtitle_track_ref` 目前是单值，需扩成数组或拆 plan 版本。
4. **OTIO 导出是否算"新事实"**：导出文件的 hash 不稳定（序列化差异）。当前决定：OTIO 是视图，不参与 `content_hash`；但客户交付里若要求 OTIO 可复现，需要固定序列化规范。
5. **diff 的语义版本**：`TimelineDiff` 结构变了之后，旧 S38 能否安全忽略新字段？需要 schema 的 `additionalProperties: false` 与 expand 策略明确。
6. **超大时间线的 diff 上限**：是否允许"全量受影响"降级？本文允许，但需要在响应里标 `diff_mode: "coarse"`，否则下游会以为这是精确区间。
