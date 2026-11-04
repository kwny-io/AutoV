# S06 · 分镜 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。SLO 与容量均为 [docs/03](../docs/03-deployment-capacity.md) 的规划值，**未实测**。

上游依据：[docs/01 §3](../docs/01-enterprise-architecture.md)（唯一权威表）、[docs/04 §1/§2/§4](../docs/04-data-consistency.md)、[docs/07 §1/§2/§4](../docs/07-production-workflows.md)（整集生产、E03 改镜流程、执行层级）、[docs/08 §2/§3](../docs/08-agents-prompts.md)（AgentTask 与编译顺序）、[docs/16 §5](../docs/16-migration-acceptance.md)、[S06 规格](../docs/design-package/services/S06-storyboard.md)。

配套：[S05](S05-breakdown.md)（场次）、[S04](S04-narrative.md)（对白文本权威）、[S29](S29-image-production.md)（`shot_lineage_id` 的**消费者**）、[S08](S08-revision-snapshot.md)（manifest）、[S09](S09-lineage-impact.md)（字段边）、[S20](S20-prompt-compiler.md)（ShotSpec → Prompt）、[S39](S39-repair-planning.md)（返工）、[S41](S41-editorial.md)（EDL）。

---

## 0. 一句话边界

**S06 是"这一集有哪些镜头、每一镜长什么样、它们按什么顺序排"的唯一权威。它不写剧本，不写分场，也不做画面。**

S06 在本仓库里承担一个别处没有的职责：**它是 `shot_lineage_id` 的铸造者**。这个 ID 是跨 Run 的重试计数锚点——[S29 §8.2](S29-image-production.md) 明确规定"同一个 `shot_ref.id` 在不同 Run/manifest 中必须映射到同一 lineage，否则报 `SHOT_LINEAGE_DRIFT`"，而 [S03 C03-5](S03-project-catalog.md)、[S09 C09-8](S09-lineage-impact.md)、[S39 C4](S39-repair-planning.md) 三处都记录了"没有任何契约定义它由谁铸造"。本文补上这条定义，并且是**唯一一次**补。

如果这个 ID 被下游自己生成、被每次 Run 重新分配、或被"重新规划镜头"顺手换掉，后果是 [E26](../docs/16-migration-acceptance.md) 直接失效：跨 Run 的返工次数会从 1.8 次/镜重新从 0 开始算，镜头上限与集上限形同虚设，同一镜可以被无限次重试而不触发"该人工介入了"。

所以 S06 的设计目标有两条：**`shot_lineage_id` 铸造一次、永不重铸**；**对白与主动作不可被静默删改**。

### 0.1 写什么 / 不写什么

| 事实 | 唯一写者 | S06 的角色 |
|---|---|---|
| `shot_id` 稳定身份 | **S06** | 唯一写者（分支内唯一） |
| `shot_lineage_id` 跨 Run/跨分支身份 | **S06** | **唯一铸造者**，下游只读 |
| `ShotSpecRevision`（一镜的完整 DSL） | **S06** | 唯一写者 |
| 镜头顺序（`shot_order`） | **S06** | 唯一写者 |
| 动作节拍 `beats[]` | **S06** | 唯一写者 |
| 机位 / 构图 / 运镜 `camera` | **S06** | 唯一写者 |
| 时长与配时策略 `duration` | **S06** | 唯一写者 |
| 对白行的**文本** | S04 | 只读复制 + 字节校验 |
| 分场边界与场次身份 | S05 | 只读引用 `scene_id` |
| 资产的实际绑定 | S10 | 只读引用 `asset_id/revision`，不解析 |
| 编译后的 Prompt | S20 | 只输出 ShotSpec |
| 生成任务与尝试计数 | S26 / S29 | 完全不涉及 |
| accepted manifest | S08 | 完全不涉及 |
| 正史与连续性判定 | S16 / S17 | 只输出 `continuity` 声明，不判定 |
| 剪辑 EDL | S41 | 提供数据，不产生 EDL |

### 0.2 五个明确不做

1. **不做画面。** S06 产出的是"仰拍 4 秒、镜头从父亲摇到怀表"这样的结构化意图，不是图像。任何"预览图""故事板草图"都是 S26 的付费产物，S06 只保存引用。
2. **不判定资产可用性。** `ShotSpec.asset_bindings` 里写的是 `{id, revision}`，这些引用从哪来、是否还在有效期内是 S10 与 S08 的事。S06 只校验"引用的 revision 存在"。
3. **不编译 Prompt。** [docs/08 §3](../docs/08-agents-prompts.md) 的编译顺序在 S20。S06 输出的 DSL 里可以有 `emotion`、`lighting` 这类语义字段，但怎么变成供应商可消费的 prompt 不是它的事。
4. **不估算成本。** 时长变了会影响成本，但成本估算在 S25。S06 只说"这个改动的时长 +1.5s"。
5. **不判断时长是否"应该"被调整。** S06 校验"能不能塞进去"，不能就报告；能不能改是 S02 的策略与人的事（[docs/07 §2](../docs/07-production-workflows.md)："自动化档位不授权随意删台词"）。

### 0.3 两条必须坚持的原则

> **原则一：`shot_lineage_id` 在镜头第一次被创造时铸造一次，此后不论重规划、重生成、跨 Run、跨分支继承，都不改变、不复用。**

> **原则二：对白文本与 `constraints.must_preserve` 里的内容，S06 只能原样复制或明确拒绝，永不做"为了塞进时长"的删减。**

第二条的表述是"明确拒绝"而不是"报错"，因为拒绝必须带可执行的替代方案（§3.5）。只说"不行"的实现等于把问题丢回给人。

---

## 1. 关键区分（最容易写错的五处）

### 1.1 四个"镜头身份"

| | 作用域 | 变了意味着 | 谁使用 |
|---|---|---|---|
| `shot_id` | `(tenant, branch)` | 这一镜在这个分支里不存在了 | S08 manifest、S09 边、S20 编译、S41 EDL |
| `shot_lineage_id` | `(tenant, series)` | **不允许变**（除退役） | S26 的 `AttemptCounterKey`、S29 的漂移断言 |
| `ShotSpecRevision` | 单个 `shot_id` | 这一镜的内容改了一版 | S08 `source_revisions`、S20 |
| `shot_order` | `(tenant, branch, episode)` | 剪辑顺序变了 | S41 EDL、S09 相邻边 |

`shot_id` 与 `shot_lineage_id` 分开的理由：分支是从某个 manifest 分叉出来的，分叉后的两个分支各自演进同一个镜头（一个走写实风、一个走风格化），**它们是同一个镜头的两个版本，不是两个镜头**。跨 Run 的尝试计数必须落在同一个 lineage 上，否则"这一镜已经试了 8 次"这个事实会被分支切碎。

反过来 `shot_id` 必须按分支唯一：两个分支的镜头可以各自被删改，操作必须互不干扰。

`shot_lineage_id` 跨分支共享的直接后果容易搞反，写清楚：**lineage 跨分支共享，但尝试计数按分支独立。** 因为 `AttemptCounterKey` 含 `branch_id`，同一 lineage 在两个分支上是两个独立的计数器。§3.2.4 有完整说明。

### 1.2 对白：只能字节复制

`shot-spec.v1` 里 `dialogue[].text` 是一个**内嵌字符串**。这在契约上是个隐患，[S04 C04-1](S04-narrative.md) 已经记录：结构上它看起来像 S06 拥有的字段，实际上它是 S04 的 `line.text` 的投影。

S06 的处理规则：

```json
{"line_id": "lin_01J...", "character_id": "chr_...", "text": "我不去", "locale": "zh-CN",
 "emotion": "resigned", "voice": {"id": "vce_...", "revision": 3}}
```

- `text` **必须**与 S04 该 `line_id` 在当前 revision 下的文本逐字节相等。
- S06 在自己的库里存 `script_line_ref = {script_id, revision, line_id, text_hash}`（`shot-spec.v1` 没有这个字段，所以存在 S06 侧，见 §11 C2）。
- 写入时重算 `text_hash` 比对，不等 → 422 `DIALOGUE_TEXT_DRIFT`。
- `emotion` 是 S06 的字段（表演意图），不是 S04 的。**S06 可以给同一句台词在不同镜头里不同的 `emotion`**——那是分镜的判断，不是改写文本。

**什么算改写**（[S04 §1.2](S04-narrative.md) 清单的 S06 版本）：

| 行为 | 判定 |
|---|---|
| 把"我不去"改成"我不去。" | ❌ 加标点也是改 |
| 把繁体转简体 | ❌ |
| 把"她"改成"他" | ❌ |
| 给同一行加 `emotion='angry'` | ✅ 不改文本 |
| 把一行拆成两镜（前半镜/后半镜）但每镜都保留完整原文 | ✅ |
| 把一行拆成两镜、每镜只留半句 | ❌ 这是删改 |

最后一行是实践中最容易发生的：4 秒塞不下 12 秒的对白，于是"前半句放 A 镜、后半句放 B 镜"。**这在文本层面没有删字，但在每一镜里都不完整**，会直接导致 S31 配音切碎、S38 字幕错位、口型对不上。正确做法是 §3.5 的"延长/拆镜并保留完整行"。

### 1.3 `ShotSpec` 与 manifest

`ShotSpec` 是**镜头的内容**；manifest 是**整集所有内容的固定快照**（[S08 §1](S08-revision-snapshot.md)）。S06 写 `ShotSpecRevision` 并发事件；S08 提交时把用到的 `content_hash` 写进 `source_revisions`。**S06 不写 manifest，也不知道当前 accepted 是哪一版**——它甚至不知道自己的 revision 有没有进 accepted。

这条边界最常被破坏的地方是"自动应用"路径（[docs/07 §2](../docs/07-production-workflows.md)：S02 判断自动应用 → S08 CAS 提交）。S06 在这里只负责"准备好新 DSL"，**不负责决定它是否生效**。

### 1.4 时长不可行：报告具体选项，不静默裁剪

`shot-spec.v1` 的 `duration` 是 `{target_seconds, trim_allowed, timing_policy}`。S06 必须回答：**这一镜的 `beats` + `dialogue` 在 `target_seconds` 内是否放得下？** 放不下时的四种情形，每种都给出具体数字：

| 情形 | S06 返回 |
|---|---|
| 台词朗读时间 > 目标时长 | 422 `DIALOGUE_EXCEEDS_DURATION`，附 `required_seconds` 与备选方案 |
| 节拍时间区间总和 > 目标时长 | 422 `BEATS_EXCEED_DURATION`，附溢出秒数 |
| `preserve_episode_duration` 下延长会使整集超时 | 200 + `feasibility.warnings`（"延长 1.5s 会使本集 +1.5s"） |
| 目标时长 < `min_shot_seconds`（默认 1.0s） | 422 `DURATION_BELOW_MINIMUM` |

**绝不允许的实现**：自动把 `target_seconds` 改成"刚好放下"的值然后静默返回 200。理由：`target_seconds` 是导演的意图（E03 的"4 秒"就是意图），改了它等于改了创作决定，而且下游 S25 的成本估算、S41 的 EDL 时长、E04 的"不删台词"全部跟着错。

### 1.5 `beats` 是"动作节拍"，不是"时间表"

`beats[{time_range: {start_seconds, end_seconds}, action}]` 看起来像时间表，实际语义是**这一镜里发生了哪几件事、每件大致占多久**。

三条约束：

1. `beats` 的 `time_range` 必须**在 `duration.target_seconds` 内**，且互不重叠（允许留空）。
2. `beats` 至少一条（schema `minItems: 1`）。
3. **`beats` 不描述镜头运动**——运镜在 `camera` 里。一条 `beats` 写"镜头推近"是错的，应该写"父亲抬眼"。

第 3 条是防止实现者把 `beats` 变成"分镜脚本的散文段落"。一旦 `beats` 变成自由文本，S20 的编译就没有稳定的输入结构，S35 的时序检查也没有可对标的锚点。

---

## 2. 输入契约

### 2.1 传输与身份

内部 mTLS + workload identity，HTTP/JSON。所有写命令带 [execution-context.v2](../contracts/execution-context.v2.schema.json) 全字段。`tenant_id` 从 TCT 派生（[S01 §3.2](S01-tenant-access.md)），`node_epoch` 与 `execution_fence` 均不参与 S06 的逻辑——S06 不感知 epoch（§3.8）。

### 2.2 命令清单

| 方法 | 路径 | 作用 | 权限 |
|---|---|---|---|
| `POST` | `/storyboards/{id}/revisions` | 为某集/某场生成一版分镜 | `creative.storyboard.create` |
| `POST` | `/shots/{shot_id}/revisions` | 单镜重写（局部改镜，E03） | `creative.storyboard.revise` |
| `POST` | `/storyboards/{id}/revisions/{rev}:seal` | 冻结一版分镜 | `creative.storyboard.seal` |
| `POST` | `/shots/{shot_id}:retire` | 退役一镜 | `creative.storyboard.revise` |
| `POST` | `/storyboards/{id}/revisions/{rev}/shot-order` | 调整镜头顺序 | `creative.storyboard.revise` |
| `POST` | `/shots/{shot_id}:split` | 一镜拆两镜（显式操作） | `creative.storyboard.revise` |

> `service.yaml` 只声明 `POST /storyboards/{id}/revisions` 与 `GET /shots/{id}/revisions/{revision}`。其余见 §11 C1。

### 2.3 查询清单

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/storyboards/{id}` | 分镜文档元数据 + head |
| `GET` | `/storyboards/{id}/revisions/{revision}` | 一版完整分镜 |
| `GET` | `/shots/{id}` | 单镜跨 revision 身份（含 `shot_lineage_id`） |
| `GET` | `/shots/{id}/revisions/{revision}` | 单镜某版 ShotSpec |
| `GET` | `/scenes/{scene_id}/shots` | 某场的镜头列表（当前有效） |
| `GET` | `/shots/{id}/lineage` | **`shot_lineage_id` 与尝试锚点信息**（S26/S29 使用） |

### 2.4 `POST /storyboards/{id}/revisions`

```json
{
  "schema_version": "2.0",
  "breakdown_ref": {"breakdown_id": "bkd_...", "revision": 4, "content_hash": "sha256:..."},
  "script_ref": {"script_id": "scr_...", "revision": 7, "content_hash": "sha256:..."},
  "scene_ids": ["scn_...", "scn_..."],
  "context_pack_ref": {"pack_id": "cpk_...", "content_hash": "sha256:..."},
  "asset_binding_source": {"binding_set_id": "bnd_...", "revision": 2, "content_hash": "sha256:..."},
  "policy_ref": {"service_id": "S02", "id": "pol_...", "revision": 3, "content_hash": "..."},
  "proposal_ref": {"agent_task_id": "agt_...", "content_hash": "sha256:..."},
  "options": {"shot_id_continuity": "inherit_by_beat_anchor", "reuse_lineage_ids": true, "max_shots_per_scene": 80}
}
```

- `breakdown_ref` 必须是 `SEALED` 分场 revision；`script_ref` 从分场里取，再交叉校验（§3.2）。
- `asset_binding_source` 引用 S10 的 `BindingSet`。S06 不解析需求，只接受已解析的绑定集。
- `proposal_ref` 指向 S19 `storyboard-director` 角色的输出；可空（人工分镜）。

### 2.5 `POST /shots/{shot_id}/revisions`（E03 局部改镜）

```json
{
  "schema_version": "2.0", "parent_revision": 3,
  "intent": {"kind": "camera_angle_change", "target": {"field": "camera.angle", "from": "eye_level", "to": "low"},
             "natural_language": "改成仰拍"},
  "patch": {"camera": {"angle": "low"}, "duration": {"target_seconds": 4.0}},
  "preserve": ["dialogue", "beats", "constraints.must_preserve"],
  "context_pack_ref": {"pack_id": "cpk_...", "content_hash": "..."}
}
```

- `patch` 是**字段级**的，不是整份 ShotSpec 替换。整份替换会让 S09 无法判断"到底哪些字段变了"，E03 的"字段差异"要求就落空。
- `preserve` 是显式的保护清单，默认 `["dialogue", "constraints.must_preserve"]`。§3.5 的可行性检查必须对照它。
- `intent.natural_language` 只是给人看的，**S06 不解析它**。解析"改成仰拍"是 S07 的意图解析职责，S07 会把结果转成 `patch`。

> 这条边界容易写错：如果 S06 自己解析自然语言，就变成了第二个意图理解权威，而且同一句"仰拍"在 S06 与 S07 可能解析成不同结果。S06 只接受结构化 `patch`。

### 2.6 错误码

| HTTP | `code` | 含义 | retryable |
|---|---|---|---|
| 400 | `INVALID_SCHEMA` | body 非法 | 否 |
| 403 | `PERMISSION_DENIED` | 无权限 | 否 |
| 404 | `NOT_FOUND` | 不存在或跨租户（E28） | 否 |
| 409 | `BREAKDOWN_REVISION_MOVED` | `breakdown_ref` 已非最新 sealed | 是 |
| 409 | `SHOT_REVISION_CONFLICT` | `parent_revision` 不匹配 | 是 |
| 409 | `COMMAND_IN_PROGRESS` | 同 command 执行中 | 是 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 同 command 异 body | 否 |
| 422 | `BREAKDOWN_NOT_SEALED` | 引用了 DRAFT 分场 | 否 |
| 422 | `SCENE_NOT_IN_BREAKDOWN` | `scene_id` 不属于该分场 revision | 否 |
| 422 | `DIALOGUE_TEXT_DRIFT` | 对白文本与 S04 不一致 | 否 |
| 422 | `DIALOGUE_EXCEEDS_DURATION` | 台词放不进目标时长 | 否 |
| 422 | `BEATS_EXCEED_DURATION` | 节拍总时长溢出 | 否 |
| 422 | `DURATION_BELOW_MINIMUM` | 低于单镜最小时长 | 否 |
| 422 | `MUST_PRESERVE_VIOLATION` | `patch` 触碰了 `must_preserve` | 否 |
| 422 | `LINE_COVERAGE_GAP` | 分场某行未被任何镜头覆盖 | 否 |
| 422 | `SHOT_LIMIT_EXCEEDED` | 超过 `max_shots_per_scene` | 否 |
| 422 | `ASSET_REVISION_UNKNOWN` | 引用了不存在的资产 revision | 否 |

**409/422 分界**：`BREAKDOWN_REVISION_MOVED` 是 409（换最新分场重试可能成功）；`DIALOGUE_EXCEEDS_DURATION` 是 422（重试多少次 4 秒也放不下 6 秒台词）。

### 2.7 幂等

与 [S08 §2.1](S08-revision-snapshot.md) 同构。`request_hash = sha256(JCS(body 去掉 command_id 与 request_hash))`；`(tenant_id, command_id)` 主键；同 hash 回放，异 hash 409。

S06 独有的第二道保护：`(tenant_id, shot_id, revision)` 主键 + `(tenant_id, shot_id, content_hash)` 唯一。`service.yaml` 的 recovery 要求"不修改旧镜头内容"——这条由 `ShotSpecRevision` 行一经插入即不可变来保证，而不只靠幂等表。

---

## 3. 核心逻辑与状态机

### 3.1 分镜生成流程

```
S05 SceneBreakdownRevision(SEALED)
   │
   ├─ S18 ContextPack（角色外观、前情、连续性、机位偏好）
   ├─ S10 BindingSet（角色/道具/场景的实际版本）
   │
   ├─ S19 storyboard-director 产出 ShotProposal[]（每场一组镜头）
   │
   ├─ S06 确定性校验（本服务实现）：
   │     ① 行覆盖：本场每条有对白的行至少被一个镜头覆盖且只覆盖一次
   │     ② 时长：每镜 beats 与 dialogue 放得下 target_seconds（§3.5）
   │     ③ 顺序：shot_order 连续唯一，且与 line_span 的推进一致
   │     ④ 对白字节：每句 text 与 S04 一致（§1.2）
   │     ⑤ 连续性：相邻镜的 state_out/state_in 可衔接（只做结构检查，不做语义判定）
   │     ⑥ 资产引用：每个 asset_binding 的 revision 存在
   │
   ├─ 铸造 shot_id / shot_lineage_id（§3.2、§3.3）
   ├─ 写 storyboard_revision + shot_revision（不可变）
   └─ 写 outbox
```

校验①的精确含义是：**本场 `line_span` 内的每一行，必须恰好出现在某一条 `shot.dialogue` 里**。画外音也算"覆盖"——那一行仍是某镜的 `dialogue`，只是画面里没有人。

这与 S05 的"每行恰好属于一个场"是同一条不变量在镜头层的投影。S05 保证不漏场，S06 保证不漏镜；两层都做是因为中间可能有人手工删了一镜。

### 3.2 `shot_lineage_id` 铸造规则（本文最重要的部分）

#### 3.2.1 定义

```
shot_lineage_id ::= 'shl_' + ULID     scope ::= (tenant_id, series_id)     唯一性 ::= 永不复用
```

它表示**"这一镜在这场电影里"**，与"这一镜在这个分支的哪一版"无关。

#### 3.2.2 铸造时机（只有一处）

```python
def mint_or_inherit_lineage(proposal, parent_shots, parent_branch_id, branch_id):
    if branch_id != parent_branch_id and proposal.inherits_shot_id:
        return parent_shots[proposal.inherits_shot_id].shot_lineage_id  # 情形 1：分支继承
    if proposal.shot_id in existing_shots:
        return existing_shots[proposal.shot_id].shot_lineage_id         # 情形 2：同分支重规划
    return new_ulid('shl_')                                            # 情形 3：新镜头
```

三种情形之外**没有第四种**。任何"重新分镜""重新规划这场""Regenerate"都走情形 2 或 1，不铸造新 ID。

#### 3.2.3 继承的判定键

| 操作 | 判定键 | lineage |
|---|---|---|
| 新生成整场 | 无父 | 新铸 |
| 局部改镜（E03） | `shot_id` 不变 | 沿用 |
| 重规划整场（重新分镜） | `shot_id_continuity` 策略 | 见 §3.3.2 |
| 分支分叉 | 父分支的 `shot_id` | 继承父的 lineage |
| 一镜拆两镜（显式 `:split`） | 原 `shot_id` | 原镜沿用；新镜**新铸** |
| 同一场补一镜 | 新 `shot_id` | 新铸 |
| 退役后重建 | 不复用旧 `shot_id` | 新铸 |

**"一镜拆两镜"的规则值得单独说明**：原镜保留 lineage（它还是"那个镜头"），新镜获得新 lineage。因为拆分后两镜会各自独立试错、独立计数——如果共享 lineage，它们的尝试次数会互相污染，"这一镜已经试了 8 次"就无法解释是拆前还是拆后。

#### 3.2.4 跨分支共享与尝试计数

`AttemptCounterKey = (tenant_id, series_id, branch_id, shot_lineage_id, strategy_class)`（[S29 §8.2](S29-image-production.md)）。

- **lineage 跨分支共享**：分支 B 从分支 A 的 manifest 分叉，B 里的同一镜沿用 A 的 `shot_lineage_id`，这样 S09 与 S41 能回答"这两个分支的这两镜是同一镜"。
- **尝试计数按分支独立**：`AttemptCounterKey` 含 `branch_id`，B 分支的重试从 0 开始。这是**正确的**——实验分支的试错不该消耗主线的镜头上限。
- **跨分支累计上限**（[E26](../docs/16-migration-acceptance.md)："尝试和费用累计，不能绕过镜头/集上限"）由 S23/S44 按 `shot_lineage_id` 聚合，**不是 S06 的事**；但 S06 必须保证 lineage 在分支间可对齐，否则聚合无从下手。

#### 3.2.5 不可变性的实现

业务层禁止任何 `UPDATE shot_lineage_id`（§5.4 的 `UNIQUE (tenant_id, branch_id, shot_id, shot_lineage_id)` 只保证一个 `shot_id` 不会挂两个 lineage，挡不住改）。数据库层用触发器挡：

```sql
CREATE FUNCTION storyboard.reject_lineage_change() RETURNS trigger AS $$
BEGIN
  IF NEW.shot_lineage_id <> OLD.shot_lineage_id THEN
    RAISE EXCEPTION 'shot_lineage_id is immutable (shot_id=%)', OLD.shot_id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER shot_lineage_immutable
  BEFORE UPDATE ON storyboard.shot
  FOR EACH ROW EXECUTE FUNCTION storyboard.reject_lineage_change();
```

选择触发器而不是"靠代码纪律"，是因为这条约束被三份文档（S03/S09/S39）标注为"未定义"，一旦有人"顺手修一下"，破坏是不可见的——它不会报错，只会让 E26 静默失效。

#### 3.2.6 给下游的契约

S26/S29 调用 `POST /agent-tasks` 或 `/generation-jobs` 时，`shot_ref` 必须带 `shot_lineage_id`：

```json
{"shot_ref": {"id": "sht_...", "revision": 5, "shot_lineage_id": "shl_...", "content_hash": "..."}}
```

- S06 **提供**这个字段（`GET /shots/{id}/lineage` 与所有 ShotSpec 响应）。
- S29 **校验**这个字段：缺失 → 422 `SHOT_LINEAGE_REQUIRED`；与 `shot_id` 的映射不一致 → `SHOT_LINEAGE_DRIFT`。
- S29 **永不自行生成**（[S29 U1](S29-image-production.md)）。

### 3.3 `shot_id` 铸造与继承

#### 3.3.1 铸造

```
shot_id ::= 'sht_' + ULID
scope   ::= (tenant_id, branch_id)
```

候选由 S19 提出时可能带 `shot_id`（重规划同一场时，指示"这一镜还是原来那一镜"）。S06 的处理：

- 候选带 `shot_id` 且该 ID 存在于本分支 → 视为**继承候选**，走 §3.3.2 的校验。
- 候选带 `shot_id` 但不存在 → 422 `UNKNOWN_SHOT_ID`（不接受"凭空指定 ID"）。
- 候选不带 → 新铸。

**S06 不信任候选里的 `shot_id` 对应关系**：候选说"这一镜是原来的 sht_A"还不够，还要过 §3.3.2 的锚点校验。

#### 3.3.2 继承策略

`options.shot_id_continuity`：

| 值 | 判定 | 适用 |
|---|---|---|
| `fresh` | 全部新铸，旧镜退役 | 整场重导 |
| `inherit_by_beat_anchor`（默认） | 新镜的首个 beat 覆盖的 `line_id` 落在旧某镜的行区间内 → 继承该镜 `shot_id` | 常规重规划 |
| `inherit_explicit` | 请求体给 `shot_id_map` | 人工指定 |
| `inherit_by_proposal` | 直接采信候选的 `shot_id`（仅校验存在性） | **不允许**用于自动路径（§3.3.3） |

`inherit_by_beat_anchor` 的锚点选"首个 beat 覆盖的行"而不是"镜头时长最接近"或"位置序号相同"：前者在边界移动时稳定，后两者在插入一镜时会连锁错位。

#### 3.3.3 为什么 `inherit_by_proposal` 在自动路径被禁

如果直接采信 LLM 给的 `shot_id`，模型可以在一次重规划里把 A 镜的 ID 分配给一个完全不同的画面（"把原来第 2 镜的 ID 给新的特写"）。后果是 S09 认为"第 2 镜内容变了"，于是只重算依赖第 2 镜的子图——而实际上整场都变了。**漏掉的影响面不会报错，只会产出一个内部不一致的集。**

人工路径（UI 上拖拽确认）允许 `inherit_explicit`，因为那是人做的显式决定。

### 3.4 顺序与重排

`shot_order` 在 `(tenant_id, branch_id, storyboard_id, storyboard_revision)` 内唯一，`DEFERRABLE INITIALLY DEFERRED`。规则与 [S05 §3.4](S05-breakdown.md) 同构：只改顺序不改内容、不推进 `ShotSpecRevision`、必须提交完整列表、跨场不能重排。

**跨场镜头的顺序是两层**：`(scene_order, shot_order)`。这与 [S41](S41-editorial.md) 的 EDL 组织一致——EDL 的段落对应场，段落内的项对应镜。S06 不产生 EDL，但保证两层都有唯一顺序。

### 3.5 时长与对白可行性（E03 / E04 核心）

#### 3.5.1 朗读时长估算

```python
def dialogue_seconds(lines, locale, emotion):
    total = 0.0
    for line in lines:
        rate = CHARS_PER_SECOND[locale]        # zh-CN: 4.2, en-US: 14.0（音节折算）
        base = len(strip_punctuation(line.text)) / rate
        base *= EMOTION_FACTOR.get(emotion, 1.0)   # 默认 1.0；'whisper' 1.35、'shout' 1.15
        total += base + INTER_LINE_PAUSE_SECONDS   # 默认 0.25
    return total
```

`CHARS_PER_SECOND` 与 `EMOTION_FACTOR` 是**服务配置**，按 locale 与情绪查表，不硬编码在代码里。默认值基于中文配音经验的估算，**未经实测**，需要配音侧校准（§12 U2）。

#### 3.5.2 可行性判定

```
required = dialogue_seconds(dialogue) + max(0, 最长 beat 时长 - 与对白重叠的部分) + tail_pause
if required > target_seconds and 台词可压缩（beat 与对白并行）:
    返回 200 + feasibility.warnings（建议并行）
elif required > target_seconds:
    返回 422 DIALOGUE_EXCEEDS_DURATION {
        required_seconds: 6.4, target_seconds: 4.0, overflow_seconds: 2.4,
        options: [{kind: 'extend', to_seconds: 6.5, episode_delta_seconds: 2.5},
                  {kind: 'split', at_line_ids: ['lin_57'], into_shots: 2},
                  {kind: 'cut_lines', line_ids: ['lin_58'], requires_human: true}]}
```

`options` 的三条是**给人和给上层策略的**，不是 S06 自动选的：

- `extend`：延长本镜。带 `episode_delta_seconds` 让人知道对整集时长的影响。
- `split`：拆镜并**保留完整台词**（每镜都有完整行）。S06 给出建议拆分点，由调用方确认后走 `:split`。
- `cut_lines`：**必须 `requires_human: true`**。自动化档位不许选这条（[docs/07 §2](../docs/07-production-workflows.md)）。

**S06 永不自己执行任何一个 option。** 它的职责是"给出准确的问题与选项"，选择权在人和 S02。

#### 3.5.3 `preserve` 与 `must_preserve`

E03 的 `patch` 带 `preserve: ["dialogue", "beats"]`，`ShotSpec.constraints.must_preserve` 里可能还有 `["character_costume", "prop:怀表"]`。

校验规则：

```
for field in patch 里的每个被改动的字段:
    if field 命中 preserve 或 constraints.must_preserve:
        → 422 MUST_PRESERVE_VIOLATION
```

命中判定按字段路径前缀匹配：`camera.angle` 命中 `camera`；`beats[2].action` 命中 `beats`。

**这是 E03 "对白及主动作不被默默删改"的实现点。** 如果一次仰拍改造的 `patch` 里顺带改了 `beats`，没有这条校验就会静默通过。

### 3.6 状态机

#### 3.6.1 四个状态机

| 对象 | 状态迁移 |
|---|---|
| `Storyboard` | `active → superseded` ／ `active → archived` |
| `ShotSpecRevision` | `DRAFT → SEALED → SUPERSEDED`；`DRAFT → WITHDRAWN` |
| `Shot` | `active → retired` |
| `ShotLineage` | `live → retired`（所有分支都退役时级联） |

- `Storyboard` 与 [S05 §3.5.1](S05-breakdown.md) 同构：`(episode_id, branch_id)` 可有多个。`ShotSpecRevision` 行一经插入不可变，"改"是新行。
- `Shot` 退役后：不再出现在新 revision、`GET /shots/{id}` 仍返回、`shot_lineage_id` 保留且不复用。
- **`ShotLineage` 的 retire 是级联结果**：该 lineage 在所有分支都退役时才 `retired`。退役后仍可被历史 manifest 解析，但不能出现在任何新 `GenerationJob` 里（S26 校验）。

### 3.7 不变量

1. 每一版 `SEALED` 分镜覆盖其 `breakdown_ref` 涉及场的全部对白行，不重不漏。
2. `shot_order` 在 `(branch, episode, scene)` 内连续唯一。
3. `shot_lineage_id` 一经写入永不变更（数据库触发器强制）。
4. 同一 `(tenant, series)` 内，`shot_lineage_id` 唯一，退役后不复用。
5. `ShotSpecRevision` 的 `content_hash` 在 `(tenant, shot_id)` 内唯一。
6. 每句 `dialogue[].text` 与 `script_line_ref.text_hash` 一致；`text_hash` 与 S04 一致。
7. `beats` 的 `time_range` 均在 `[0, target_seconds]` 内且互不重叠。
8. `target_seconds >= min_shot_seconds`。
9. 任何写操作在同一本地事务内写 outbox。
10. `ShotSpec.constraints.must_preserve` 里的项在 `patch` 中未被修改。

### 3.8 S06 与 `execution_fence` / `node_epoch`

**S06 不感知这两个概念。** S06 是**内容服务**，不是执行服务——它不知道当前是哪个 Run、哪个 node；`execution_fence` 与 `node_epoch` 由 S08/S21/Temporal 管理，S06 收到的命令只带 `command_id`。一次"旧 epoch 的结果晚到"在 S06 表现为：一条 `POST /shots/{id}/revisions` 带着旧的 `parent_revision`。S06 用乐观并发拒绝（409 `SHOT_REVISION_CONFLICT`），**不需要理解 epoch**。

这条边界必须写清，否则实现者会想在 ShotSpec 上记 `node_epoch`——那会让 S06 变成执行状态的一部分，且违背[AGENTS.md](../AGENTS.md)"两者不能混用"。

---

## 4. 输出契约

### 4.1 `ShotSpec`（遵循 `shot-spec.v1`）+ S06 扩展

标准字段完全按 [contracts/domain-v1/shot-spec.v1.schema.json](../contracts/domain-v1/shot-spec.v1.schema.json)。S06 在**响应包装层**附加 lineage 与来源信息：

```json
{
  "schema_version": "1.0", "shot_id": "sht_01J8ZQ...", "revision": 5,
  "episode_id": "ep_...", "scene_id": "scn_...",
  "display_locator": {"episode": 2, "scene": 3, "shot": 4},
  "narrative_purpose": "父亲的权威第一次出现裂痕", "shot_type": "medium_close_up",
  "asset_bindings": {
    "characters": [{"character": {"id": "chr_...", "revision": 4}, "costume": "cst_...",
                    "voice": {"id": "vce_...", "revision": 3}, "screen_role": "primary"}],
    "scene": {"id": "loc_...", "revision": 2}, "props": [{"id": "prp_...", "revision": 6}],
    "style": {"id": "sty_...", "revision": 1}},
  "camera": {"angle": "low", "focal_length_mm": 35, "movement": "slow_push_in", "framing": "medium_close_up"},
  "duration": {"target_seconds": 4.0, "trim_allowed": false, "timing_policy": "preserve_episode_duration"},
  "output_spec": {"aspect_ratio": "16:9", "resolution": "1920x1080", "fps": 24},
  "beats": [
    {"time_range": {"start_seconds": 0.0, "end_seconds": 1.8}, "action": "父亲抬眼看向儿子"},
    {"time_range": {"start_seconds": 1.8, "end_seconds": 4.0}, "action": "父亲把怀表按在桌上"}],
  "dialogue": [
    {"line_id": "lin_57", "character_id": "chr_...", "text": "我不去", "locale": "zh-CN",
     "emotion": "resigned", "voice": {"id": "vce_...", "revision": 3}}],
  "emotion": "cold_restraint", "lighting": {"key": "window_side", "mood": "dusk", "contrast": "high"},
  "continuity": {
    "axis_of_action": "left_to_right", "screen_direction": "son_left",
    "state_in": {"watch_on_table": false}, "state_out": {"watch_on_table": true},
    "previous_shot_ref": {"id": "sht_...", "revision": 4},
    "reference_edges": [{"kind": "eyeline_match", "to_shot_ref": {"id": "sht_...", "revision": 2}}]},
  "constraints": {"must_preserve": ["dialogue", "beats"], "negative": ["no_on_screen_text"],
                  "locked_fields": ["asset_bindings.characters[0].costume"]}
}
```

S06 的响应包装（`GET /shots/{id}/revisions/{rev}`）：

```json
{
  "shot_spec": { "...": "上面的完整 ShotSpec" },
  "s06_meta": {
    "shot_lineage_id": "shl_01J8ZQ...", "branch_id": "br_main", "state": "SEALED",
    "storyboard_ref": {"storyboard_id": "sbd_...", "revision": 8},
    "breakdown_ref": {"breakdown_id": "bkd_...", "revision": 4, "content_hash": "sha256:..."},
    "script_line_refs": [{"script_id": "scr_...", "revision": 7, "line_id": "lin_57", "text_hash": "sha256:..."}],
    "feasibility": {"dialogue_seconds": 2.4, "beats_seconds": 4.0,
                    "required_seconds": 4.0, "verdict": "feasible", "warnings": []},
    "content_hash": "sha256:...", "sealed_at": "2026-11-05T09:30:00.000Z"
  }
}
```

**为什么 lineage 放在 `s06_meta` 而不是 ShotSpec 里**：`shot-spec.v1` 的 `additionalProperties` 是 false，加不进去。这不是绕开契约，而是把"契约缺一个字段"这件事显式暴露在包装层——同一个信息出现在两处会让人分不清哪处是权威。§11 C2 记录了正式的修复建议。

`content_hash` 组成：

```
sha256( JCS(ShotSpec 去掉 s06_meta)
        || "\n" || sha256(concat(sorted(script_line_refs.map(text_hash)))) )
```

把对白 hash 拼进去，是为了让"文本被改过但 ShotSpec 看起来没变"这种情况改变 content_hash。

### 4.2 `storyboard.revision.created` 事件

topic `drama.storyboard.v2`，`aggregate_id = storyboard_id`，`aggregate_sequence = revision`。

```json
{
  "specversion": "1.0", "schema_version": "2.0", "id": "evt_...", "source": "s06.storyboard",
  "type": "storyboard.revision.created", "subject": "ep_...", "time": "2026-11-05T09:30:00.000Z",
  "tenant_id": "tnt_...", "home_cell": "cell-a1", "aggregate_id": "sbd_...", "aggregate_sequence": 8,
  "correlation_id": "cmd_...", "causation_id": "cmd_...", "traceparent": "00-...-01",
  "data": {
    "storyboard_id": "sbd_...", "revision": 8, "state": "SEALED",
    "episode_id": "ep_...", "branch_id": "br_main",
    "breakdown_ref": {"breakdown_id": "bkd_...", "revision": 4, "content_hash": "sha256:..."},
    "shot_count": 42, "changed_shot_ids": ["sht_..."], "retired_shot_ids": [],
    "shot_lineages": [{"shot_id": "sht_...", "shot_lineage_id": "shl_...", "revision": 5, "content_hash": "sha256:..."}],
    "content_hash": "sha256:...", "occurred_at": "2026-11-05T09:30:00.000Z"},
  "data_ref": {"service_id": "S06", "id": "sbd_...", "revision": 8, "content_hash": "sha256:..."}
}
```

三条要点：

1. **`shot_lineages` 必须发出来。** 这是 S29 获取 lineage 的第一条路径（第二条是 `GET /shots/{id}/lineage`）。带上它是为了让 S26 在派单前不必额外查询就能构造 `AttemptCounterKey`。
2. **`changed_shot_ids` / `retired_shot_ids`**：S09 用它决定重建哪些边。E03 的"字段差异 → 最小依赖闭包"从这里起步。
3. `data_ref` 非空——完整分镜是正文级负载。

### 4.3 `storyboard.shot.revision.created` 事件

单镜重写（E03 路径）**不发** `storyboard.revision.created`（整集分镜没变），而是发：

```json
{"type": "storyboard.shot.revision.created", "aggregate_id": "sht_...", "aggregate_sequence": 5,
 "data": {"shot_id": "sht_...", "shot_lineage_id": "shl_...", "revision": 5,
          "changed_fields": ["camera.angle", "duration.target_seconds"],
          "previous_revision": 4, "content_hash": "sha256:...",
          "source_storyboard_ref": {"storyboard_id": "sbd_...", "revision": 8}}}
```

`changed_fields` 是 E03 的直接产物：S09 拿它在字段依赖图上找受影响子图，不需要 diff 两份完整 ShotSpec。

> **这条事件在 [event-topics.v2.json](../contracts/event-topics.v2.json) 中不存在**（只有 `storyboard.shot.revision.created`）。见 §11 C3——本文沿用 `storyboard.shot.revision.created` 这个名字，但语义按本节定义。

---

## 5. 数据模型

Postgres，schema `storyboard`。ID 前缀：`sbd_ / sht_ / shl_`。

### 5.1 Storyboard 容器

```sql
CREATE TABLE storyboard.storyboard (
  tenant_id      text NOT NULL,
  storyboard_id  text NOT NULL,
  project_id     text NOT NULL,
  series_id      text NOT NULL,
  episode_id     text NOT NULL,
  branch_id      text NOT NULL,
  breakdown_id   text NOT NULL,
  lifecycle      text NOT NULL CHECK (lifecycle IN ('active','superseded','archived')),
  head_revision  integer NOT NULL DEFAULT 0,
  sealed_revision integer,
  created_by     jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, storyboard_id),
  CONSTRAINT sbd_sealed_le_head CHECK (sealed_revision IS NULL OR sealed_revision <= head_revision)
);
CREATE INDEX sbd_by_episode ON storyboard.storyboard (tenant_id, episode_id, branch_id, lifecycle);
```

### 5.2 分镜版本

```sql
CREATE TABLE storyboard.storyboard_revision (
  tenant_id            text NOT NULL,
  storyboard_id        text NOT NULL,
  revision             integer NOT NULL,
  state                text NOT NULL CHECK (state IN ('DRAFT','SEALED','SUPERSEDED','WITHDRAWN')),
  episode_id           text NOT NULL,
  branch_id            text NOT NULL,
  breakdown_id         text NOT NULL,
  breakdown_revision   integer NOT NULL,
  breakdown_hash       char(64) NOT NULL,
  script_id            text NOT NULL,
  script_revision      integer NOT NULL,
  script_hash          char(64) NOT NULL,
  binding_set_id       text,
  binding_set_revision integer,
  binding_set_hash     char(64),
  context_pack_id      text,
  context_pack_hash    char(64),
  produced_by          jsonb NOT NULL,
  parent_revision      integer,
  shot_count           integer NOT NULL,
  content_hash         char(64) NOT NULL,
  sealed_at            timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, storyboard_id, revision),
  UNIQUE (tenant_id, storyboard_id, content_hash),
  FOREIGN KEY (tenant_id, storyboard_id) REFERENCES storyboard.storyboard (tenant_id, storyboard_id),
  CONSTRAINT sbr_sealed_chk CHECK ((state = 'DRAFT') = (sealed_at IS NULL))
);
```

### 5.3 镜头身份

```sql
CREATE TABLE storyboard.shot_lineage (
  tenant_id        text NOT NULL,
  shot_lineage_id  text NOT NULL,          -- 'shl_01J...'
  series_id        text NOT NULL,
  origin_branch_id text NOT NULL,          -- 首次铸造的分支
  origin_shot_id   text NOT NULL,
  retired_at       timestamptz,            -- 所有分支都退役时才设置
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, shot_lineage_id)
);
CREATE INDEX shot_lineage_by_series ON storyboard.shot_lineage (tenant_id, series_id, retired_at);
```

### 5.4 镜头（分支内身份）

```sql
CREATE TABLE storyboard.shot (
  tenant_id        text NOT NULL,
  branch_id        text NOT NULL,
  shot_id          text NOT NULL,          -- 'sht_01J...'
  shot_lineage_id  text NOT NULL,
  storyboard_id    text NOT NULL,
  episode_id       text NOT NULL,
  scene_id         text NOT NULL,
  state            text NOT NULL CHECK (state IN ('active','retired')),
  origin_shot_id   text,                   -- 从哪个 shot 继承（分支分叉/重规划）
  first_revision   integer NOT NULL,
  retired_at       timestamptz,
  retire_reason    text,                   -- 'removed'/'merged'/'replaced'
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, branch_id, shot_id),
  UNIQUE (tenant_id, branch_id, shot_id, shot_lineage_id),
  FOREIGN KEY (tenant_id, shot_lineage_id) REFERENCES storyboard.shot_lineage (tenant_id, shot_lineage_id)
);
CREATE INDEX shot_by_scene ON storyboard.shot (tenant_id, branch_id, scene_id, state);
CREATE INDEX shot_by_lineage ON storyboard.shot (tenant_id, shot_lineage_id);
```

**顺序不在这张表上。** `shot_order` 只存在于 `shot_revision`（§5.5）：顺序是"某一版分镜里的排列"，属于内容快照。若放在 `shot` 上，退役镜会与新镜争夺同一个序号，且历史 revision 的顺序无从保留。

### 5.5 镜头内容（每 revision 一行）

```sql
CREATE TABLE storyboard.shot_revision (
  tenant_id        text NOT NULL,
  branch_id        text NOT NULL,
  shot_id          text NOT NULL,
  revision         integer NOT NULL,
  state            text NOT NULL CHECK (state IN ('DRAFT','SEALED','SUPERSEDED','WITHDRAWN')),
  storyboard_id    text NOT NULL,
  storyboard_revision integer NOT NULL,
  scene_id         text NOT NULL,
  shot_order       integer NOT NULL,
  display_shot     integer NOT NULL,
  shot_spec        jsonb NOT NULL,         -- 完整 shot-spec.v1 文档
  content_hash     char(64) NOT NULL,
  target_seconds   numeric(8,3) NOT NULL,
  min_seconds_ok   boolean NOT NULL,
  feasibility      jsonb NOT NULL,         -- {dialogue_seconds, beats_seconds, required_seconds, verdict, warnings}
  dialogue_texts   jsonb NOT NULL,         -- [{line_id, text_hash}]，用于漂移检测
  parent_revision  integer,
  produced_by      jsonb NOT NULL,
  sealed_at        timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, branch_id, shot_id, revision),
  UNIQUE (tenant_id, branch_id, shot_id, content_hash),
  CONSTRAINT shot_rev_order_uq
    UNIQUE (tenant_id, branch_id, storyboard_id, storyboard_revision, shot_order)
    DEFERRABLE INITIALLY DEFERRED,
  CONSTRAINT shot_rev_duration_chk CHECK (target_seconds > 0),
  CONSTRAINT shot_rev_sealed_chk CHECK ((state = 'DRAFT') = (sealed_at IS NULL)),
  FOREIGN KEY (tenant_id, branch_id, shot_id) REFERENCES storyboard.shot (tenant_id, branch_id, shot_id),
  FOREIGN KEY (tenant_id, storyboard_id, storyboard_revision) REFERENCES storyboard.storyboard_revision (tenant_id, storyboard_id, revision)
);
CREATE INDEX shot_rev_by_storyboard ON storyboard.shot_revision (tenant_id, storyboard_id, storyboard_revision, shot_order);
```

在同一 `storyboard_revision` 内，顺序唯一且连续——这样历史 revision 的顺序完整保留，退役镜也留着当时的序号。

### 5.6 对白行引用（防漂移）

```sql
CREATE TABLE storyboard.shot_dialogue_ref (
  tenant_id     text NOT NULL,
  branch_id     text NOT NULL,
  shot_id       text NOT NULL,
  revision      integer NOT NULL,
  line_id       text NOT NULL,
  script_id     text NOT NULL,
  script_revision integer NOT NULL,
  text_hash     char(64) NOT NULL,
  text_copy     text NOT NULL,             -- 字节副本，仅用于校验与审计，不作权威
  emotion       text,
  PRIMARY KEY (tenant_id, branch_id, shot_id, revision, line_id),
  FOREIGN KEY (tenant_id, branch_id, shot_id, revision) REFERENCES storyboard.shot_revision (tenant_id, branch_id, shot_id, revision)
);
CREATE INDEX shot_dialogue_by_line ON storyboard.shot_dialogue_ref (tenant_id, script_id, line_id);
```

`text_copy` 只用于**校验与审计**（S35 核对、S45 取证），任何读取"当前台词"的路径必须去 S04。表名与注释都要让这一点显式。

### 5.7 节拍

```sql
CREATE TABLE storyboard.shot_beat (
  tenant_id     text NOT NULL,
  branch_id     text NOT NULL,
  shot_id       text NOT NULL,
  revision      integer NOT NULL,
  beat_index    integer NOT NULL,
  start_seconds numeric(8,3) NOT NULL,
  end_seconds   numeric(8,3) NOT NULL,
  action        text NOT NULL,
  PRIMARY KEY (tenant_id, branch_id, shot_id, revision, beat_index),
  CONSTRAINT shot_beat_range_chk CHECK (end_seconds > start_seconds AND start_seconds >= 0),
  FOREIGN KEY (tenant_id, branch_id, shot_id, revision) REFERENCES storyboard.shot_revision (tenant_id, branch_id, shot_id, revision)
);
```

**重叠检查在应用层做**，不用 `EXCLUDE` 约束——`EXCLUDE USING gist (int4range)` 需要 `btree_gist` 扩展，而重叠允许"恰好在边界相接"的语义（`[0,2)` 与 `[2,4)` 不算重叠），应用层更容易表达。

### 5.8 幂等与 Outbox

```sql
CREATE TABLE storyboard.idempotency_record (
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

CREATE TABLE storyboard.outbox (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,
  aggregate_sequence bigint NOT NULL,
  event_type         text NOT NULL,
  partition_key      text NOT NULL,
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON storyboard.outbox (id) WHERE published_at IS NULL;
```

### 5.9 不建的表

- **不建 `inbox`。** S06 不消费事件。若将来要消费 S05 的 `breakdown.revision.created` 做"分场变了"的提示，须连消费逻辑一起加（§11 C6）。
- **不建 `attempt_counter`。** 尝试计数归 S26/S29（[S29 §8.2](S29-image-production.md)）。S06 只提供 lineage，**绝不自己记一次尝试**——那会变成第二个权威。
- **不建 `cost_estimate`。** 归 S25。
- **不建 `render_preview`。** 归 S26/S28；S06 最多存 `media_ref` 指向 S28 的制品。

---

## 6. 幂等与并发

### 6.1 写入事务模板

```python
with db.transaction() as tx:                        # REPEATABLE READ
    rec = tx.idempotency_begin(tenant_id, command_id, request_hash, cmd_type)
    if rec.replayed:
        return rec.response_code, rec.response_body

    # 1. 读外部：S05 分场 revision、S04 剧本文本、S10 绑定集
    bd = s05.get_breakdown_revision(breakdown_id, breakdown_revision)
    assert bd.state == 'SEALED'                     # 否则 422
    assert bd.content_hash == body.breakdown_ref.content_hash   # 否则 409

    # 2. 对白字节校验（逐行）
    for line in proposed_dialogue:
        assert sha256(s04.get_line_text(line.line_id)) == line.expected_hash

    # 3. 可行性校验（§3.5）
    feas = check_feasibility(...)
    if feas.verdict == 'infeasible':
        raise Unprocessable('DIALOGUE_EXCEEDS_DURATION', feas.options)

    # 4. 铸造 / 继承 shot_id 与 shot_lineage_id
    shots = mint_or_inherit(...)

    # 5. 写 storyboard_revision + shot_revision + shot_beat + shot_dialogue_ref
    # 6. 写 outbox（同一事务）
    # 7. 标记旧 revision SUPERSEDED / 旧镜 retired
    tx.idempotency_finish(tenant_id, command_id, 200, response)
```

第 1–2 步是外部读，不参与事务，因此存在窗口：读完 S04 之后剧本可能被改。S06 把读到的 `text_hash` 与 `script_revision` 一起存进 `shot_dialogue_ref`，S08 在 commit 时会再校验（[docs/04 §2](../docs/04-data-consistency.md)）。S06 不试图跨库加锁。

### 6.2 并发

| 场景 | 机制 |
|---|---|
| 同 storyboard 并发两版 | `UPDATE storyboard SET head_revision = head_revision + 1 WHERE ... AND head_revision = $parent`；0 行 → 409 |
| 同 shot 并发两版 | `UPDATE shot SET state='active' WHERE ...` 配合 `parent_revision` 乐观锁；0 行 → 409 `SHOT_REVISION_CONFLICT` |
| 并发 `:split` | `UPDATE shot SET state='retired' WHERE shot_id=$1 AND state='active'`；0 行 → 409 |
| 并发 lineage 铸造 | ULID 无碰撞；`shot_lineage` 主键兜底 |
| 并发重排 | `DEFERRABLE` 唯一约束 + 完整列表提交 |
| 并发 `:seal` | `UPDATE ... WHERE state='DRAFT'`；0 行 → 409 |

### 6.3 事件量与跳号

整场生成 → 每个 `SEALED` `storyboard_revision` 发一条 `storyboard.revision.created`；单镜重写 → 每个 `SEALED` `shot_revision` 发一条 `storyboard.shot.revision.created`，`aggregate_id = shot_id`。`DRAFT` 不发，跳号由消费侧按 §6.5 处理。

### 6.4 重排不发事件

同 [S05 §6.4](S05-breakdown.md)：顺序改变不改变 `content_hash`，发事件会让消费方无法区分。代价是 S41 的 EDL 需要主动查询（§11 C5）。

### 6.5 消费侧缺口规则（写给下游）

按 `(aggregate_id, aggregate_sequence)` 检测缺口，缓冲 30s，超时回源；终态不倒退；重放时 `replay_side_effects=false`。

---

## 7. 失败分支

### 7.1 输入阶段

| 场景 | 行为 | 返回 |
|---|---|---|
| `breakdown_ref` 已非最新 sealed | 拒绝，回执带最新 ref | 409 `BREAKDOWN_REVISION_MOVED` |
| 引用 DRAFT 分场 | 拒绝 | 422 `BREAKDOWN_NOT_SEALED` |
| `scene_id` 不属于该分场 revision | 拒绝 | 422 `SCENE_NOT_IN_BREAKDOWN` |
| `asset_binding_source` 缺失 | 允许，但 `asset_bindings` 必须由 `patch`/提案内联给出 | 200 |
| `context_pack_ref` 缺失 | 允许，`context_coverage='partial'`，并在 revision 记录 | 200 |
| `proposal_ref` 缺失 | 人工分镜，允许 | 200 |

### 7.2 校验阶段

| 场景 | 行为 | 返回 |
|---|---|---|
| 分场某有对白的行未被任何镜头覆盖 | 拒绝，列出行 | 422 `LINE_COVERAGE_GAP` |
| 同一行被两个镜头覆盖 | 拒绝（不允许双覆盖） | 422 `LINE_COVERAGE_OVERLAP` |
| 对白文本与 S04 不一致 | 拒绝 | 422 `DIALOGUE_TEXT_DRIFT` |
| 台词放不进目标时长 | 拒绝 + 三条 options | 422 `DIALOGUE_EXCEEDS_DURATION` |
| `beats` 总时长溢出 | 拒绝 + 溢出秒数 | 422 `BEATS_EXCEED_DURATION` |
| `beats` 互不重叠但超出 `target_seconds` | 同上 | 422 |
| `target_seconds` < 1.0s | 拒绝 | 422 `DURATION_BELOW_MINIMUM` |
| `patch` 触碰 `must_preserve` | 拒绝 | 422 `MUST_PRESERVE_VIOLATION` |
| 资产 revision 不存在 | 拒绝 | 422 `ASSET_REVISION_UNKNOWN` |
| 场镜头数超上限 | 拒绝 | 422 `SHOT_LIMIT_EXCEEDED` |
| `beats` 为空 | 拒绝（schema `minItems:1`） | 400 `INVALID_SCHEMA` |
| **纯对白场**（无 beat 之外的视觉动作） | 允许，但 `beats` 必须至少有一条描述说话状态 | 200 |

### 7.3 Agent 阶段

| 场景 | 行为 |
|---|---|
| S19 超时 | 有界重试 1 次；仍失败 → 503 或人工 |
| 结构非法 | 有界纠正（最多 2 次，[S19 §3.4](S19-agent-runtime.md)） |
| 2 次仍非法 | 422 + 违规列表 + `agent_task_id` |
| 输出含 `tenant_id` | 422 `AGENT_SUPPLIED_TENANT` + 告警 |
| 输出含 `shot_lineage_id` | **丢弃并重铸/继承**。lineage 是 S06 独占的，Agent 给的任何 lineage 都不可信 |
| 输出含 `shot_id` | 视为继承候选，须过 §3.3.2 锚点校验 |

"输出含 `shot_lineage_id` 丢弃"这条与 `shot_id` 的处理不同：`shot_id` 可能是合法的"这还是原来那镜"的意图，而 `shot_lineage_id` 对 Agent 完全不可知（它不来自任何 prompt 上下文），出现即说明污染。

### 7.4 存储与事件

| 场景 | 行为 |
|---|---|
| 主键冲突（并发同 revision） | 409 |
| `content_hash` 冲突 | 返回既有 revision，`deduplicated: true` |
| 触发器拒绝 lineage 变更 | 500（这是代码 bug，必须告警，不能静默） |
| Outbox 写失败 | 事务回滚 |
| Relay 失败 | 保留未发布，指数退避 |

**触发器拒绝是 500 而不是 4xx。** 它只会在实现有 bug 时触发（业务代码不该尝试改 lineage），把它当 4xx 会让 bug 被当成正常拒绝。

### 7.5 依赖不可用

| 依赖 | 行为 |
|---|---|
| S05 | 无法分镜（503）。**不允许**用本地缓存的分场 |
| S04 | 无法校验对白 → 503。不允许跳过校验 |
| S10 | 可以用内联 `asset_bindings` 继续（人工分镜场景） |
| S18 | 降级 `partial` |
| S19 | 人工分镜可用 |

**S04 不可用时的立场与其他依赖不同**：S04 是文本权威，跳过校验就等于没有防漂移。宁可停。

### 7.6 回滚与独立升级（E01）

- 迁移向后兼容一个版本。
- 回滚不撤销已 `SEALED` 的 revision。
- **回滚不得重建 `shot_lineage` 表或清空它**——那会摧毁所有跨 Run 的尝试锚点。任何涉及 lineage 表的迁移必须是纯增量。
- `shot_lineage_immutable` 触发器在回滚时保留。

---

## 8. 可观测

### 8.1 指标

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `s06_shot_revision_total` | counter | `state`, `path`（整场/单镜） | 产出量 |
| `s06_storyboard_latency_p95` | histogram | `path` | 整场 ≤ 20s（不含 Agent），单镜 ≤ 5s |
| `s06_shot_count_per_episode` | histogram | — | 规模异常 |
| `s06_validation_failure_total` | counter | `rule` | 六条校验各自失败率 |
| `s06_dialogue_overflow_seconds` | histogram | `locale` | 溢出分布，用于校准朗读速率 |
| `s06_feasibility_warning_total` | counter | `kind` | `preserve_episode_duration` 下的整集偏移 |
| `s06_lineage_mint_total` | counter | `reason`（new/inherit_branch/inherit_replan） | 铸造原因分布 |
| `s06_lineage_drift_rejected_total` | counter | — | 下游传入不一致的 lineage |
| `s06_shot_id_inherit_ratio` | gauge | — | 重规划时身份保留率 |
| `s06_dialogue_text_drift_total` | counter | — | 防漂移命中数 |
| `s06_outbox_unpublished_age_seconds` | gauge | — | P99 ≤ 10s |

### 8.2 告警

| 条件 | 级别 | 动作 |
|---|---|---|
| `s06_lineage_mint_total{reason="new"}` 在重规划命令中 > 0 | **P1** | 重规划不该铸造新 lineage，说明继承逻辑坏了 |
| `s06_lineage_drift_rejected_total` > 0/h | P2 | 下游传入不一致的 lineage，查 S26/S29 的传递链 |
| `s06_dialogue_text_drift_total` > 0/h | P1 | 有路径在改写对白，查 S19/S07 |
| `s06_validation_failure_total{rule="MUST_PRESERVE_VIOLATION"}` > 5%/h | P2 | S07 的 patch 生成在越权 |
| `s06_shot_id_inherit_ratio` 单次重规划 < 0.3 | P2 | 退化到 `fresh`，会导致整集重做 |
| `s06_dialogue_overflow_seconds` P95 > 3s | P2 | 朗读速率配置偏低或导演给的目标时长普遍太短 |
| `s06_outbox_unpublished_age_seconds` P99 > 60s | P2 | 事件总线 |

`lineage_mint_total{reason="new"}` 出现在重规划中是 P1 的原因：它意味着某个镜头的尝试计数会在下次 Run 时归零，[E26](../docs/16-migration-acceptance.md) 静默失效。这是本服务**唯一**一个能直接摧毁验收项的指标。

### 8.3 审计

`SEALED` / `SUPERSEDED` / `retire` / `:split` / lineage 铸造各落一条本地审计。**lineage 铸造单独留痕**（谁、何时、哪个 `command_id`、从哪个父继承或新铸），因为它是唯一不可逆的身份事件。

---

## 9. 测试清单

标注 `[E##]` 对应 [docs/16 §5](../docs/16-migration-acceptance.md)。mock S05/S04/S10/S18/S19。

### 9.1 幂等与并发 `[E12]`

| # | 用例 | 期望 |
|---|---|---|
| T06-01 | 同 command 同 body 两次 | `replayed: true`，一版 revision |
| T06-02 | 同 command 异 body | 409 `IDEMPOTENCY_KEY_REUSED` |
| T06-03 | 并发两版同 `parent_revision` | 一个 200 一个 409 |
| T06-04 | 幂等表清理后重试 | `content_hash` 唯一约束返回既有 |
| T06-05 | 并发 `:split` 同镜 | 一个 200 一个 409 |

### 9.2 `shot_lineage_id`（本文核心，E26）

| # | 用例 | 期望 |
|---|---|---|
| T06-06 | 新镜头 | 铸造一个 `shl_`，写 `shot_lineage` |
| T06-07 | 同镜新 `ShotSpecRevision` | lineage 不变 |
| T06-08 | 重规划整场，`inherit_by_beat_anchor` 命中 | lineage 不变，`shot_id` 不变 |
| T06-09 | 分支分叉继承 | 新分支的 `shot_id` 沿用父的 lineage |
| T06-10 | 分支分叉后新加一镜 | 新 lineage |
| T06-11 | `:split` 一镜拆两 | 原镜 lineage 保留，新镜新 lineage |
| T06-12 | 退役后重建同位置镜头 | **新** lineage（不复用） |
| T06-13 | 尝试 `UPDATE shot SET shot_lineage_id=...` | 触发器抛异常（500 + 告警） |
| T06-14 | Agent 输出含 `shot_lineage_id` | 丢弃，按正常规则铸造 |
| T06-15 | 跨 3 个 Run 同一 `shot_id` | 三次查询同一 lineage |
| T06-16 | S29 mock 收到错误的 lineage | mock 侧应报 `SHOT_LINEAGE_DRIFT`（S06 侧只保证传对） |

T06-12 值得展开：退役后在同一位置重建一镜，**必须是新 lineage**。陷阱在于"位置相同"容易让人想复用——但复用会让它与已退役镜头共享尝试计数，而历史尝试已经用掉了上限。

### 9.3 E03 局部改镜

| # | 用例 | 期望 |
|---|---|---|
| T06-17 | `patch: {camera.angle: 'low'}`，`target_seconds: 4`，1 句短台词 | 200，产出合法 DSL（`camera.angle='low'`） |
| T06-18 | 同上，但 `patch` 顺带改了 `beats` 且 `preserve` 含 `beats` | 422 `MUST_PRESERVE_VIOLATION` |
| T06-19 | 同上，`patch` 改了 `asset_bindings.characters[0].costume`，`locked_fields` 含它 | 422 `MUST_PRESERVE_VIOLATION` |
| T06-20 | `changed_fields` 只含 `camera.angle` | 事件里 `changed_fields` 精确匹配 |
| T06-21 | 改镜后 lineage 不变 | 断言 |
| T06-22 | 改镜后旧 revision 仍可读 | `GET /shots/{id}/revisions/{old}` 200 |

### 9.4 时长与对白（E03 / E04）

| # | 用例 | 期望 |
|---|---|---|
| T06-23 | 4s 目标，台词 6.4s 朗读 | 422 `DIALOGUE_EXCEEDS_DURATION` + 三条 options |
| T06-24 | 同上，自动化档位试图选 `cut_lines` | 上层拒绝（S06 侧 `requires_human: true` 已标注） |
| T06-25 | `beats` 总时长 5s，目标 4s | 422 `BEATS_EXCEED_DURATION` |
| T06-26 | `beats` 与对白并行，总需求 4.0s | 200 + `feasibility.warnings` |
| T06-27 | `target_seconds=0.5` | 422 `DURATION_BELOW_MINIMUM` |
| T06-28 | `timing_policy='preserve_episode_duration'`，延长 1.5s | 200 + warning 含 `episode_delta_seconds` |
| T06-29 | 对白分两条，各带完整文本 | 200 |
| T06-30 | 对白只留半句（文本截断） | 422 `DIALOGUE_TEXT_DRIFT` |

### 9.5 对白防漂移（E02 / E04）

| # | 用例 | 期望 |
|---|---|---|
| T06-31 | `text` 与 S04 逐字节相等 | 200 |
| T06-32 | `text` 多一个句号 | 422 `DIALOGUE_TEXT_DRIFT` |
| T06-33 | 繁体转简体 | 422 |
| T06-34 | 同一 `line_id` 在两个镜头（重叠） | 422 `LINE_COVERAGE_OVERLAP` |
| T06-35 | 分场某行未被任何镜头覆盖 | 422 `LINE_COVERAGE_GAP` |
| T06-36 | 同行在不同镜头不同 `emotion` | 200（`emotion` 是 S06 字段） |
| T06-37 | `text_copy` 与当前 S04 不符（S04 改了） | 读取时 `stale=true` 标记（不阻断，S08 会校验） |

### 9.6 信任边界 `[E28]`

| # | 用例 | 期望 |
|---|---|---|
| T06-38 | Agent 输出含 `tenant_id` | 422 `AGENT_SUPPLIED_TENANT` + 告警 |
| T06-39 | A 租户 TCT 读 B 租户 shot | 404 |
| T06-40 | `shot_id` 存在但属于其他分支 | 404 |

### 9.7 事件 `[E13][E32]`

| # | 用例 | 期望 |
|---|---|---|
| T06-41 | 写库后 outbox 前崩溃 | 全回滚 |
| T06-42 | publish 后 `published_at` 前崩溃 | 重复投递，下游去重 |
| T06-43 | 整场事件含 `shot_lineages` | 字段完整，可据以构造 `AttemptCounterKey` |
| T06-44 | 单镜事件 `aggregate_id = shot_id` | 与整场事件分区不同 |
| T06-45 | DRAFT 不发事件 | 断言 |
| T06-46 | envelope 校验 | 符合 v2，`additionalProperties:false` |

### 9.8 顺序与独立升级

| # | 用例 | 期望 |
|---|---|---|
| T06-47 | 重排镜头 | `shot_order` 变、content_hash 不变、lineage 不变 |
| T06-48 | 重排 delta 提交 | 400 |
| T06-49 | 跨场混排 | 拒绝 |
| T06-50 | 滚动升级期间并发写 | 无约束泄漏 |
| T06-51 | 回滚后 lineage 表完整 | 行数不变、触发器仍在 |
| T06-52 | 最小权限 | 只连本 schema |

---

## 10. 与上下游服务的接口约定

### 10.1 上游

| 服务 | 提供 | 调用 | 失败影响 |
|---|---|---|---|
| S01 | TCT / 授权 | SDK | 无法处理 |
| S05 | `SceneBreakdownRevision` + `scene_id` | `GET /breakdowns/{id}/revisions/{rev}` | 无法分镜 |
| S04 | 对白行文本 | `GET /scripts/{id}/revisions/{rev}/lines` | 无法校验，停 |
| S10 | `BindingSet` | `GET /binding-sets/{id}` | 可用内联绑定 |
| S18 | `ContextPack` | `GET /context-packs/{id}` | 降级 |
| S19 | `storyboard-director` 输出 | `POST /agent-tasks` | 人工可用 |
| S02 | 策略 | `GET /authorization-context` | 无法自动分镜 |

### 10.2 下游

| 服务 | 用 S06 什么 | 契约 |
|---|---|---|
| S09 | `changed_fields` / `retired_shot_ids` → 字段边 | §4.3 |
| S20 | 完整 `ShotSpec` → 编译 | §4.1 |
| S08 | `ShotSpecRevision.content_hash` 进 `source_revisions` | §4.1 |
| S26 | `shot_ref.shot_lineage_id` 构造 `AttemptCounterKey` | §3.2.6 |
| S29 | 同上，且校验漂移 | §3.2.6 |
| S35 | 时序/口型校验的基准（`beats` + `dialogue` + `duration`） | §4.1 |
| S38 | `dialogue[].text` 与 `line_id` → 字幕 | §1.2 |
| S41 | `shot_order` + `duration` → EDL | §3.4 |
| S17 | `continuity` 声明 → 连续性校验 | §4.1 |
| S39 | `ShotSpecRevision` 差异 → 返工计划 | §4.1 |

### 10.3 S06 → S20 的严格边界

| 问题 | 谁答 |
|---|---|
| 这一镜拍什么、多长、什么机位 | **S06** |
| 这一镜的 prompt 怎么写 | S20 |
| 模板选哪个 | S20 |
| 供应商能力是否支持 | S24（S20 查） |
| 对白文本 | S04（S06 只复制） |

S06 输出里**不含任何 prompt 文本**。`emotion`、`lighting.mood` 是语义标注，S20 决定怎么变成词。

### 10.4 S06 → S41 的时序契约

EDL 需要每镜的**实际可用时长**。S06 提供 `duration.target_seconds`，但真实时长取决于生成结果（S28 的制品元数据）。

契约边界：

- S06 提供**意图时长**与该镜的 `timing_policy`。
- S28/S26 提供**实际时长**。
- **S41 以实际时长为准生成 EDL**，意图时长只用于对齐。
- 如果实际时长与意图偏差 > `trim_allowed` 允许的容忍度，S35 报缺陷；S06 **不自动改 `target_seconds`**。

这条必须在 S06 侧写清，否则实现者会在 S06 里加"自动纠正时长"——那会让 S06 依赖渲染结果，破坏"内容服务不依赖执行"的边界。

### 10.5 S06 → S39 的返工输入

S39 需要知道"这一镜的哪些字段属于 `must_preserve`"，以判断一次返工是否越界。S06 提供 `constraints.must_preserve` 与 `locked_fields`，并保证它们**在同一 lineage 的所有 revision 上单调不减**——任取 `r1 < r2`，`must_preserve(r1) ⊆ must_preserve(r2)`、`locked_fields(r1) ⊆ locked_fields(r2)`，不可通过新 revision 悄悄移除一项。

解除锁定需要一个显式的 `:unlock` 命令（`creative.storyboard.revise` 权限 + 审计），不能靠新 revision 顺手去掉。§11 C8 记录该命令未在契约中。

---

## 11. 契约缺口（需要先行修改，不要自行补）

| # | 缺口 | 建议 |
|---|---|---|
| C1 | `service.yaml` 只声明 `POST /storyboards/{id}/revisions` / `GET /shots/{id}/revisions/{revision}`；本文需要的 `:seal`、`/shots/{id}/revisions`、`:retire`、`:split`、`shot-order`、`/lineage`、`/scenes/{id}/shots` 都没有 | 新增 `contracts/openapi/s06-storyboard.v2.yaml`，由 §2 派生 |
| C2 | **`shot-spec.v1.schema.json` 没有 `shot_lineage_id`，也没有 `script_line_ref`**；`additionalProperties: false` 导致两者都放不进去 | 出 `shot-spec.v2.schema.json`，至少新增 `shot_lineage_id`（required）、`script_line_ref`（per-dialogue）。本文用 `s06_meta` 包装层过渡，**必须在 v2 落定后移除包装层**，否则同一信息有两处权威 |
| C3 | `event-topics.v2.json` 里是 `storyboard.shot.revision.created`，但没有任何 payload schema；且 `subject` 应是 `shot_id` 还是 `episode_id` 未定义 | 新增 `contracts/events/storyboard-shot-revision-created.v2.schema.json`；本文取 `subject = episode_id`，`aggregate_id = shot_id` |
| C4 | **`shot_lineage_id` 的铸造权威全仓未定义**（[S03 C03-5](S03-project-catalog.md)、[S09 C09-8](S09-lineage-impact.md)、[S39 C4](S39-repair-planning.md) 三处记录） | 本文确立：S06 铸造，规则见 §3.2。**需写进 `service-catalog.v2.json` 的 S06 `data_owner`**，并把 `shot_lineage_id` 加进 S06 的 `data_owner` 文本；否则下一份方案还会重新讨论 |
| C5 | 重排不发事件（§6.4），S41 EDL 与 S09 相邻边的刷新责任未定义 | 二选一：(a) 新增 `storyboard.shot-order.changed`；(b) 明确 S41/S09 主动查。本文取 (b)，需 docs/07 确认 |
| C6 | S06 的 `depends_on` 是 `[S05, S18, S19]`，缺 S04（对白权威）、S10（绑定集）、S01、S02 | `depends_on` 的语义（启动依赖 vs 运行协作）未定义。[S35 C3](S35-technical-qc.md) 取"读内容走 query 可接受"。本文同此立场，但需契约维护方明确 |
| C7 | `shot-spec.v1` 的 `display_locator{episode, scene, shot}` 没有唯一性约束，也没有说明 `shot` 是场内序号还是全集序号 | 本文取"场内序号"（§3.4 两层顺序）。需要补一句，否则两个实现会选出不同含义 |
| C8 | `constraints.locked_fields` 的**解除**路径未定义（§10.5 要求单调不减） | 新增 `POST /shots/{id}:unlock`，带权限与审计；或明确"锁定永不可解除、只能新建镜头" |
| C9 | `beats[].time_range` 是秒，但没有 fps 精度约定（24fps 下 1/24 ≈ 0.0417s）。`duration.target_seconds` 也是浮点 | 需要明确"所有时长以此秒为单位存储，渲染侧自行对齐帧"。否则 S06 与 S28 会各自动一次舍入 |
| C10 | `camera` 的字段集合（`angle`/`focal_length_mm`/`movement`/`framing`）在 schema 里没有 `additionalProperties:false` 的说明，也没有枚举完整性保证 | 补 `movement` 与 `framing` 的枚举，并把 E03 用到的 `low` 明确列在 `angle` 里（现状有 `low`，但没说明它与 `camera.position` 的关系） |
| C11 | `shot-spec.v1` 没有 `scene_id`/`episode_id` 的**必填**校验说明（现状 required 含它们，但没有"必须与 breakdown_ref 一致"的约束） | 本文在 §3.1 校验里自加。建议写进 schema 的描述 |
| C12 | `min_shot_seconds`（本文取 1.0s）无依据 | 见 §12 U1 |

---

## 12. 未决

| # | 未决 | 本文临时立场 | 需要谁裁决 |
|---|---|---|---|
| U1 | 单镜最短时长 | 1.0s | 创作 + 供应商（部分模型有最小时长） |
| U2 | 各 locale 的朗读速率与情绪系数 | zh-CN 4.2 字/秒，`whisper` 1.35，`shout` 1.15；未实测 | 配音（见 §8.1 的校准指标） |
| U3 | `shot_id_continuity` 默认值 | `inherit_by_beat_anchor` | 创作团队 |
| U4 | 重排后 S09/S41 的一致性责任 | S41/S09 主动查（C5） | docs/07 |
| U5 | `split` 的拆分点由谁建议 | S06 按 `line_id` 与 `beats` 给出候选，调用方确认 | 创作 |
| U6 | `locked_fields` 能否解除 | 需显式 `:unlock`（C8） | 架构 |
| U7 | 分支分叉时是否复制 `shot_revision` 还是引用父分支的 | **复制**（分支可独立演进） | 架构；复制会让存储增大约 N 倍分支数 |
| U8 | 跨分支的累计尝试上限在哪聚合 | S23/S44 按 `shot_lineage_id` 聚合，非 S06 | S23 / S44 |
| U9 | `subject` 字段语义（`shot_id` vs `episode_id`） | `episode_id`（C3） | 契约维护方 |
| U10 | 是否需要 `shot-spec.v2` 的正式迁移窗口 | 需要，且包装层在 v2 落地后必须删除（C2） | 契约维护方 |

### 12.1 实施顺序建议

1. **C2（schema v2）+ C4（lineage 权威）先行**。没有 v2，`shot_lineage_id` 只能活在包装层；没有 C4，下游会自己生成。
2. `shot_lineage` / `shot` 两张表 + 不可变触发器。**先写 T06-13，再写其他业务逻辑**——让"改 lineage 会炸"成为第一天就成立的事实。
3. `storyboard_revision` / `shot_revision` / `shot_beat` / `shot_dialogue_ref` 四张表 + 六条校验。
4. 人工分镜路径（不依赖 S19）：能导入一份手写 ShotSpec 并通过全部校验。
5. 对白防漂移（T06-31…T06-37）与时长可行性（T06-23…T06-30）。**这两组是 E03/E04 的核心，必须在接真实模型前全绿。**
6. `:seal` + outbox + 两条事件。
7. `:split` 与 `:retire` 的 lineage 行为（T06-11、T06-12）。
8. 接 S19 `storyboard-director` + 有界纠正 + 信任边界（T06-38）。
9. 顺序与 `DEFERRABLE` 并发。

第 2 步排在最前是刻意的：**lineage 的不可变性是本服务唯一一条"事后无法补救"的约束**。先把它变成数据库事实，比先写生成逻辑重要得多。
