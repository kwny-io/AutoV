# S05 · 分集分场 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。SLO 与容量均为 [docs/03](../docs/03-deployment-capacity.md) 的规划值，**未实测**。

上游依据：[docs/01 §3](../docs/01-enterprise-architecture.md)（唯一权威表）、[docs/04 §1/§4](../docs/04-data-consistency.md)（command envelope、幂等、Outbox）、[docs/07 §1/§2/§4](../docs/07-production-workflows.md)（整集生产与执行层级）、[docs/16 §5](../docs/16-migration-acceptance.md)（E 矩阵）、[S05 规格](../docs/design-package/services/S05-breakdown.md)。

配套：[S04](S04-narrative.md)（剧本来源）、[S06](S06-storyboard.md)（下游）、[S18](S18-context-retrieval.md)（ContextPack）、[S19](S19-agent-runtime.md)（`breakdown` 角色）、[S08](S08-revision-snapshot.md)（manifest 固定）、[S10](S10-asset-catalog.md)（需求 → 资产解析）、[S09](S09-lineage-impact.md)（字段级影响边）。

---

## 0. 一句话边界

**S05 是"这一集分成哪几场、每场从哪句台词到哪句台词、这场需要哪些角色和道具"的唯一权威。它不写剧本，也不写镜头。**

分场看起来是"把文本切一切"，实际是本仓库里最容易变成第二个序号权威的地方。原因在于：`scene_id` 必须在一整集的改编、重排、补拍之后保持不变，而"第 3 场"这个数字每天都在变。一旦有服务把 `(episode, scene_ordinal)` 当成场景身份，一次插入场次就会让 S06 的分镜、S09 的依赖边、S08 的 manifest 全部错位。

所以 S05 的设计目标只有一条：**让 `scene_id` 成为唯一可以跨版本引用的场景身份，让一切"第几场"只是显示值。**

### 0.1 写什么 / 不写什么

| 事实 | 唯一写者 | S05 的角色 |
|---|---|---|
| `scene_id` 稳定身份与生命周期 | **S05** | 唯一写者 |
| `SceneBreakdownRevision`（某版分场结果） | **S05** | 唯一写者 |
| 每场的**显示定位**（`display_locator`） | **S05** | 唯一写者，且明确标注为显示值 |
| 每场对应的剧本行区间（`line_span`） | **S05** | 唯一写者（行内容仍归 S04） |
| 角色 / 道具 / 场景 / 服装的**需求清单** | **S05** | 唯一写者（是"需求"，不是"绑定"） |
| 场景间的关系（同地/同时/因果标注） | **S05** | 唯一写者（这是分场的推论，不是正史） |
| 剧本正文与对白行内容 | S04 | 只读引用 `line_id` 与 `text_hash` |
| 镜头、ShotSpec、镜头顺序 | S06 | 完全不涉及 |
| 资产的实际绑定与版本（`BindingSet`） | S10 | 只输出需求，由 S10 解析 |
| 正史状态、谁在第几场知道什么 | S16 | 完全不涉及（S17 检查） |
| accepted manifest 与指针 | S08 | 完全不涉及 |

### 0.2 五个明确不做

1. **不铸造 `shot_id`，也不预设镜头数。** 一场里有多少镜是 S06 的事。"这场预计 12 个镜头"这种估算可以出现在 `advisory` 字段里，但**不能成为 S06 的约束**——一旦 S05 给了镜头数，S06 要么被迫凑数，要么被迫解释为什么超了。
2. **不改写任何文本。** 分场输出里的 `opening_line_text` 等摘录字段是**从 S04 复制的字节**，必须带 `text_hash` 并在写入时校验。任何"顺手润色一下摘要"都是越权（§1.2）。
3. **不解析资产。** `requirements` 里写的是 `{kind: 'prop', label: '怀表', spec_hint: {...}}`，不是 `{prop_id: 'prp_xxx', revision: 3}`。需求到绑定的解析是 S10 的 `resolve`，且解析结果会随时变（权利过期、版本升级）。
4. **不判定这场能不能拍。** 预算、档位、资产就绪、连续性风险全部不在 S05。它只回答"这场需要什么"，不回答"现在能不能做"。
5. **不维护跨集连续性。** "这把钥匙在第 2 集已经出现过"是 S16 的正史 + S17 的检查。S05 最多在 `continuity_hints` 里写"本场出现钥匙"，不判断它是否与前面一致。

### 0.3 一条必须坚持的原则

> **`scene_id` 在第一次创建后永不改变、永不复用；任何"重排场次"操作只改 `display_locator`，不改 `scene_id`，也不改已被 S06 引用的 `SceneBreakdownRevision`。**

这条原则的直接后果：分场结果**修正**必须是一次新的 `SceneBreakdownRevision`，而"同一个 `scene_id` 在新 revision 里的内容变了"是允许的（它是同一场的新一版），"这一场消失了"用 `retired` 表达而不是删行。

---

## 1. 关键区分（最容易写错的四处）

### 1.1 场的身份、场的版本、场的顺序

| | 是什么 | 变了意味着 | 谁引用 |
|---|---|---|---|
| `scene_id` | 场在这个故事里的**身份** | 不存在了（retired） | S06 的 `scene_id`、S09 的 `SCENE_DERIVED_FROM_SCRIPT` 边、S08 manifest |
| `SceneBreakdownRevision` | 这一场在某一版分场里的**内容** | 内容改了（新版本） | S06 从哪版分镜、S08 `source_revisions` |
| `display_locator` | 这一场在 UI 上叫"第几场" | 只是显示变了 | 只有 UI 与导出封面 |
| `scene_order` | 一集内所有场的排列 | 叙事顺序变了 | S09 的相邻边、S41 的 EDL 顺序 |

四者分开的原因用一句话说清：**一次"在第 2 场和第 3 场之间插入一场"必然改变 `display_locator` 与 `scene_order`，但不应该改变任何已有场的身份或内容。** 如果场次身份是序号，插入就变成了一场全局重命名。

与 [S03 §1.1](S03-project-catalog.md) 的结构完全对称——这是刻意的，因为同一个 bug 在两个层级都会发生。

### 1.2 摘要与原文：只许复制，不许改写

分场的产出里需要引用文本（"这场从'我不去'开始"），否则人无法审阅。这类字段统一叫 `excerpt`，并且必须满足：

```json
{
  "line_id": "lin_01J...",
  "text": "我不去",
  "text_hash": "sha256:..."
}
```

- `text_hash` 来自 S04 的 `DialogueRevision.content_hash`（或其派生），**写入时校验**：S05 拿到 S04 的当前 revision 后重新计算，不相等 → 422 `SCRIPT_LINE_DRIFT`。
- 摘录**只能是某一条完整行**，不接受"半句话 + 省略号"。半句话无法校验字节相等，也就无法发现改写。
- S05 的摘录**不构成剧本的第二份权威**：读者要看原文去 S04；摘录在 UI 上有"snapshot at revision N"标记。

这条与 [S04 §1.2](S04-narrative.md) 的 `script_line_ref` 是同一条纪律在另一层的应用。S06 会重复一次，S20 再重复一次——三处都写是因为三处都会有人想"顺手优化一下文案"。

### 1.3 需求（requirement）与绑定（binding）

| | S05 输出的 | S10 解析出的 |
|---|---|---|
| 形态 | `{kind, label, spec_hint, scene_role}` | `{asset_id, revision, rights_ref}` |
| 例子 | `{kind:'prop', label:'怀表', spec_hint:{material:'brass'}}` | `{prop_id:'prp_01J...', revision:4}` |
| 是否稳定 | 稳定（需求不随资产版本变） | 随资产版本、权利状态变 |
| 谁负责正确性 | S05（需求是否完整） | S10（解析是否可商用） |

**为什么不能在分场时就绑定资产**：分场发生在整集生产的最前面（[docs/07 §1](../docs/07-production-workflows.md) 第 2 步），而资产解析在第 3 步，且可能触发 S11–S15 的新资产准备。如果 S05 写死 `prop_id`，一次资产替换就要改分场 revision，进而让所有下游 manifest 失效——为了一次道具换版本重跑整集分镜，这是不可接受的成本。

`requirements` 的 `label` 是**人可读的稳定键**（"怀表"、"父亲的遗物"），S10 用它做匹配，S05 **不保证** label 的全局唯一性，也不应对它做规范化（例如把"怀表"和"银怀表"合并）——那是资产命名的事。

### 1.4 分场是"有界推理 + 确定性校验"，不是纯 LLM 输出

[docs/07 §1](../docs/07-production-workflows.md) 第 2 步写明"各内容服务校验与保存"。对 S05 具体化为：

```
S04 ScriptRevision(SEALED)
   │
   ├─ S18 取 ContextPack（角色、前情、连续性提示）
   │
   ├─ S19 breakdown 角色产出 SceneBreakdownProposal（候选，一次或多次）
   │
   ├─ S05 确定性校验（本服务实现，不是 LLM）：
   │     ① 行覆盖完整性：每一条 SEALED 行必须恰好属于一个场，不重不漏
   │     ② 行顺序单调：场的 line_span 按场的顺序严格递增且不重叠
   │     ③ 发言人在本场角色列表内：dialogue.speaker ∈ scene.characters[].requirement_ref
   │     ④ 定位合法：display_locator 与 scene_order 一致
   │     ⑤ 需求引用完整：每个 requirement_ref 都能解析到一条 requirements 条目
   │
   ├─ 校验通过 → 写 SceneBreakdownRevision（不可变）
   └─ 校验失败 → 422 + 具体违规列表（不静默修补）
```

**校验①是本服务最重要的不变量。** 它保证"整集剧本的每一行都被分配到了某一场"，因此任何下游都能从 `scene_id` 反查到完整台词，不需要猜。缺失行会导致 S38 字幕、S31 配音、S35 QC 在后续步骤里静默丢内容——这些缺陷在最末端才暴露，代价是一个已经渲染完的镜头。

**校验失败不自动修复**：不能"把漏掉的行塞进上一场"。分场边界是创作判断，自动补全会让错误安静地变成事实。

---

## 2. 输入契约

### 2.1 传输与身份

内部 mTLS + workload identity，HTTP/JSON。所有写命令带 [execution-context.v2](../contracts/execution-context.v2.schema.json) 的全部必填字段（`tenant_id` 从 TCT 派生，不接受 body 自授，见 [S01 §3.2](S01-tenant-access.md)）。

```json
{
  "schema_version": "2.0",
  "command_id": "cmd_01J8ZQ...",
  "tenant_id": "tnt_01J8ZQ...",
  "project_id": "prj_...",
  "series_id": "ser_...",
  "episode_id": "ep_...",
  "branch_id": "br_...",
  "manifest_ref": {"id": "man_...", "revision": 12, "content_hash": "..."},
  "actor": {"principal_id": "prn_...", "principal_type": "human", "delegation_id": "dlg_..."},
  "trace_id": "4bf92f35...",
  "payload": { }
}
```

### 2.2 命令清单

| 方法 | 路径 | 作用 | 需要权限 |
|---|---|---|---|
| `POST` | `/breakdowns` | 以某版剧本创建一版分场 | `creative.breakdown.create` |
| `POST` | `/breakdowns/{id}/revisions` | 追加一版分场（重新分场） | `creative.breakdown.create` |
| `POST` | `/breakdowns/{id}/revisions/{rev}:seal` | 冻结一版分场 | `creative.breakdown.seal` |
| `POST` | `/scenes/{scene_id}:retire` | 退役一场（合并/删除） | `creative.breakdown.revise` |
| `POST` | `/breakdowns/{id}/revisions/{rev}/scene-order` | 调整场次顺序（只改 `scene_order`） | `creative.breakdown.revise` |
| `POST` | `/scenes/{scene_id}/requirement-resolutions` | 回写 S10 的解析结果（可选缓存，**非权威**） | — |

> `service.yaml` 只声明 `POST /breakdowns`。其余路径见 §11 C1。

### 2.3 查询清单

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/breakdowns/{id}` | 分场文档元数据 + 当前 head |
| `GET` | `/breakdowns/{id}/revisions/{revision}` | 一版分场完整内容 |
| `GET` | `/episodes/{episode_id}/scenes` | 某集当前有效的场次列表（带 `authoritative_source`） |
| `GET` | `/scenes/{scene_id}` | 单场详情（跨 revision 的身份） |
| `GET` | `/scenes/{scene_id}/requires` | 该场的需求清单（S10 的输入） |

### 2.4 `POST /breakdowns`

```json
{
  "schema_version": "2.0",
  "script_ref": {
    "script_id": "scr_...",
    "revision": 7,
    "content_hash": "sha256:...",
    "line_set_hash": "sha256:..."
  },
  "context_pack_ref": {"pack_id": "cpk_...", "content_hash": "sha256:..."},
  "policy_ref": {"service_id": "S02", "id": "pol_...", "revision": 3, "content_hash": "..."},
  "proposal_ref": {"agent_task_id": "agt_...", "content_hash": "sha256:..."},
  "options": {
    "allow_reuse_scene_ids": true,
    "max_scenes": 400
  }
}
```

- `script_ref.revision` 必须是 `SEALED`（§7.1）。`DRAFT` 剧本可以分场做预览，但**不能落库**——预览走 `options.preview=true` 返回不持久化的结果。
- `content_hash` 与 `line_set_hash` 都会被校验：与 S04 的当前值不符 → 409 `SCRIPT_REVISION_MOVED`，提示调用方用最新 revision 重试。**这是乐观并发，不是失败**。
- `proposal_ref` 指向 S19 的 `AgentTask` 输出。S05 保存它作为"这版分场是谁生成的"的证据，但**不信任它的任何 ID**（§3.2）。
- `allow_reuse_scene_ids` 见 §3.4：重新分场时是否尝试把 `scene_id` 继承下来。

### 2.5 `POST /breakdowns/{id}/revisions`

同 `/breakdowns`，但多两个字段：

```json
{
  "parent_revision": 3,
  "scene_id_continuity": "inherit_by_line_anchor"
}
```

`scene_id_continuity` 决定重新分场时 `scene_id` 怎么处理：

| 值 | 行为 | 适用 |
|---|---|---|
| `fresh` | 全部新 `scene_id`，旧场 `retired` | 结构性重写（整集重新切分） |
| `inherit_by_line_anchor`（默认） | 新场的**首行**落在旧某场的行区间内 → 继承该 `scene_id` | 常规改稿 |
| `inherit_explicit` | 由请求体给出 `scene_id_map` | 人工指定 |

这条字段存在的理由是 E03 与 E07 的共同要求：一次局部改稿不能把整集的分镜全部作废。如果每次重新分场都换 `scene_id`，S06 的 `SHOT_DERIVED_FROM_SCENE` 边会全部断掉，S08 会认为整集都变了。

### 2.6 错误码

| HTTP | `code` | 含义 | retryable |
|---|---|---|---|
| 400 | `INVALID_SCHEMA` | body 不符合 schema | 否 |
| 403 | `PERMISSION_DENIED` | 无权限 | 否 |
| 404 | `NOT_FOUND` | 不存在或跨租户（E28） | 否 |
| 409 | `SCRIPT_REVISION_MOVED` | `script_ref` 已不是最新 sealed | 是（带最新 ref） |
| 409 | `BREAKDOWN_REVISION_CONFLICT` | `parent_revision` 不匹配 | 是 |
| 409 | `COMMAND_IN_PROGRESS` | 同 command_id 执行中 | 是 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 同 command_id 不同 body | 否 |
| 422 | `SCRIPT_NOT_SEALED` | 引用了 DRAFT 剧本 | 否 |
| 422 | `LINE_COVERAGE_GAP` | 有行未被分配到任何场 | 否 |
| 422 | `LINE_COVERAGE_OVERLAP` | 同一行被分配到两个场 | 否 |
| 422 | `LINE_ORDER_VIOLATION` | 场顺序与行顺序矛盾 | 否 |
| 422 | `SPEAKER_NOT_IN_SCENE` | 对白发言人不在本场角色需求内 | 否 |
| 422 | `SCRIPT_LINE_DRIFT` | 摘录文本与 S04 不一致 | 否 |
| 422 | `REQUIREMENT_REF_DANGLING` | 需求引用不存在 | 否 |
| 422 | `SCENE_LIMIT_EXCEEDED` | 超过 `max_scenes` | 否 |

**409 与 422 的分界**：`SCRIPT_REVISION_MOVED` 是 409 而不是 422，因为剧本被改了是"世界变了"，拿到新 revision 重试可能成功。`SCRIPT_NOT_SEALED` 是 422，因为不管重试多少次，DRAFT 剧本都不该被落库。

### 2.7 幂等语义

与 [S08 §2.1](S08-revision-snapshot.md) 完全相同：

```
request_hash = sha256( JCS( body 去掉 command_id 与 request_hash ) )
```

`(tenant_id, command_id)` 主键；`IN_PROGRESS` 同 hash → 409 可重试；`DONE` 同 hash → 回放首次响应并附 `replayed: true`；任何不同 hash → 409 `IDEMPOTENCY_KEY_REUSED`。

**S05 独有的一条**：`service.yaml` 的 `recovery` 写"重试相同 command 返回同一场次分解；不重复造场次 ID"。这由两层保证：第一层是上面的幂等表；第二层是 `(tenant_id, breakdown_id, revision)` 的主键——即使幂等表被清理（30 天），重试仍会因为主键冲突返回既有 revision 而不是新建。

---

## 3. 核心逻辑与状态机

### 3.1 分场的输入来源

分场需要三样东西，缺一样都不能产出可靠结果：

| 输入 | 来源 | 缺失时 |
|---|---|---|
| 已 seal 的剧本 revision + 完整行列表 | S04 | 422 `SCRIPT_NOT_SEALED` |
| 前情上下文（角色、已发生事件、连续性提示） | S18 `ContextPack` | **不阻断**，但 `context_coverage` 标记为 `partial`，并在 revision 上记录 |
| `breakdown` 角色的候选分场 | S19 | 可以人工分场（`proposal_ref` 为 null） |

第三行说明一件事：**S19 是可选加速，不是必需依赖**。人工可以直接给出分场结果（UI 上拖拽 + 确认），S05 的校验与存储完全一样。这条保证了"Agent 不可用时不能停产"。

缺少 ContextPack 时允许继续，是因为分场边界的判断主要靠剧本本身；但要在 `revision.context_coverage` 里明确写 `partial`，让 S06 与人知道这版分场没有看到前情。这与 [S18 §8](S18-context-retrieval.md) 的"不能把尚未投影的记忆当不存在"是同一条：**不知道就说不知道**。

### 3.2 对 Agent 输出的处理（有界纠正）

S19 的 `breakdown` 角色返回一个结构化的 `SceneBreakdownProposal`。S05 的处理顺序固定：

```
收到 proposal
  │
  ├─ ① 结构校验（JSON Schema）         失败 → 422 STRUCTURE_INVALID，退回 S19
  ├─ ② 语义校验（§1.4 的五条）          失败 → 有界纠正（最多 2 次），仍失败 → 422 + 违规列表
  ├─ ③ 信任边界                        proposal 里的任何 ID 一律丢弃并重铸
  └─ ④ 落库
```

**第 ③ 步是本节的核心。** `proposal` 里可能出现 `scene_id`、`tenant_id`、`breakdown_id`——这些字段一律**不采信**：

- `tenant_id` 用 execution context 的，proposal 里的值出现即拒绝（422 `AGENT_SUPPLIED_TENANT`），这比"忽略它"更严格，因为出现说明 prompt 被污染了。
- `scene_id` 由 S05 铸造（`scn_` + ULID）。Agent 不可能知道合法格式之外的东西，采信它就是让 LLM 决定对象身份。
- `breakdown_id` / `revision` 由 S05 分配。

有界纠正的规格见 [S19 §3.4](S19-agent-runtime.md)：**每版分场最多 2 次纠正**，每次纠正是一次新的 S19 调用（有费用），超过上限直接失败并交人工。不做"循环自我反思"——那会让一次分场账单无界增长（[docs/08 §2](../docs/08-agents-prompts.md)）。

### 3.3 场次身份铸造与继承

```
新建一版分场：
  │
  ├─ scene_id_continuity = 'fresh'
  │     → 每个场铸造新 scene_id
  │
  ├─ scene_id_continuity = 'inherit_by_line_anchor'
  │     → 对每个新场 S：
  │          anchor = S.line_span.start_line_id
  │          找父 revision 中 line_span 包含 anchor 的场 P
  │          if 找到 且 P 未被本版其他场继承 → S.scene_id = P.scene_id
  │          else                            → S.scene_id = 新铸造
  │
  └─ scene_id_continuity = 'inherit_explicit'
        → 按请求体的 scene_id_map；未覆盖的场铸造新 ID
```

继承规则的三条约束：

1. **一个旧 `scene_id` 最多被一个新场继承**（`inherit_by_line_anchor` 的"未被本版其他场继承"）。否则两个场共享身份，后续所有引用都歧义。
2. **未继承的旧场标记 `retired_at`，不删行。** 它的 `scene_id` 永久保留，任何历史 manifest 仍能解析到它。
3. **继承不改变内容归属。** 继承了 `scene_id` 的新场，其内容仍是新 revision 的内容；`scene_id` 只表示"这还是同一场戏"。

`anchor` 选**首行**而不是"行区间重叠最大"：后者在边界移动时不稳定（同一场微调边界就可能不再"最大重叠"），首行规则更可预测，也更容易向用户解释"因为你这场还是从这句开始，所以还是第 3 场"。

### 3.4 场次重排

`POST /breakdowns/{id}/revisions/{rev}/scene-order` 只改 `scene_order` / `display_locator`，不改任何内容、不改 `scene_id`。

```sql
UPDATE breakdown.scene_revision
   SET scene_order = NULLIF(new_ordinal, -1)
 WHERE tenant_id = $1 AND breakdown_id = $2 AND revision = $3 AND scene_id = ANY($4);
-- 唯一约束 DEFERRABLE INITIALLY DEFERRED，允许事务内临时重复
```

三条约束：

- `scene_order` 在 `(tenant_id, breakdown_id, revision)` 内唯一，用 `DEFERRABLE INITIALLY DEFERRED` 表达（与 [S03 §1.3](S03-project-catalog.md) 的重排同一手法）。
- **不允许"先删后插"**：中间态会被并发读看到空集。
- 重排**必须提交完整列表**而非 delta。delta 在并发重排下会静默错排（[S03 §9](S03-project-catalog.md) 反模式 7 的同一理由）。

重排**不推进 `revision`**：`scene_order` 是同一 revision 内的显示属性变化，不是内容变化。它产生一条 `scene_order_adjusted` 本地审计记录（不进 outbox，见 §6.4 的讨论）。

这里有一个刻意的取舍：顺序改变在叙事上是有意义的（"这两个场对调了"），但它不改变任何场的 `line_span`。如果把它当作内容变更，一次重排会让整集的分镜全部失效，而 S06 的镜头内容其实一个字都不用改。**顺序由 S41 的 EDL 与 `scene_order` 共同表达，不需要新 revision。**

### 3.5 状态机

#### 3.5.1 `Breakdown`（容器）

```
active ──supersede──▶ superseded
   └──archive──▶ archived
```

一个 `(episode_id, branch_id)` 可以有多个 `Breakdown`（不同分支、不同编剧版本）。**不能加 `UNIQUE(episode_id)`**——这与 [S04 §2.2](S04-narrative.md) 的 `script` 同一考虑。

#### 3.5.2 `SceneBreakdownRevision`

```
DRAFT ──seal──▶ SEALED ──supersede──▶ SUPERSEDED
  └──withdraw──▶ WITHDRAWN
```

| 状态 | 可被 S06 引用 | 可修改 |
|---|---|---|
| `DRAFT` | 否 | 是（但只作为工作草稿，见下） |
| `SEALED` | 是 | **否** |
| `SUPERSEDED` | 否（新引用应该用新的） | 否 |
| `WITHDRAWN` | 否 | 否 |

**`DRAFT` 的"可修改"是受限的**：不能在落库后编辑——而是"未 seal 的 revision 可以被新 DRAFT revision 取代"。这与 [S04 §1.1](S04-narrative.md) 的"不允许编辑草稿 revision"是同一条，写在这里是为了避免实现者把 `DRAFT` 理解成一个可变对象。**技术上 `SceneBreakdownRevision` 行一经插入即不可变，任何改动都是新行。**

#### 3.5.3 `Scene`

```
active ──retire──▶ retired
```

`retired` 后不再出现在任何新 revision 里，但历史 revision 仍然引用它，且 `GET /scenes/{id}` 仍返回。**永不物理删除**。

### 3.6 不变量

1. 每一版 `SEALED` 的 `SceneBreakdownRevision` 覆盖 `script_ref` 的全部行，不重不漏（§1.4 校验①）。
2. `scene_order` 在 revision 内连续、唯一。
3. 一个 `scene_id` 在同一个 `breakdown_id` 的任意两版 revision 中，最多是"继承关系"，不能出现在两个并行 revision 里（分支除外，见下）。
4. `requirements[].requirement_id` 在 revision 内唯一；每个 `requirement_ref` 必须解析到一条。
5. `retired_at` 一旦设置不可清除。
6. 任何写操作在同一个本地事务里写 outbox（§6.3）。
7. `source_script.content_hash` 与 `line_set_hash` 都必须与写入时的 S04 状态一致。

第 3 条需要补充：不同 `branch_id` 的 breakdown 可以复用同一个 `scene_id`（分支是同一场戏的不同走向）。判定键是 `(tenant_id, branch_id, scene_id)`。

---

## 4. 输出契约

### 4.1 `SceneBreakdownRevision` 结构

```json
{
  "schema_version": "2.0",
  "breakdown_id": "bkd_...",
  "revision": 4,
  "state": "SEALED",
  "episode_id": "ep_...",
  "branch_id": "br_...",
  "source_script": {
    "script_id": "scr_...",
    "revision": 7,
    "content_hash": "sha256:...",
    "line_set_hash": "sha256:..."
  },
  "context": {
    "context_pack_ref": {"pack_id": "cpk_...", "content_hash": "sha256:..."},
    "coverage": "full"
  },
  "produced_by": {
    "kind": "agent",
    "agent_task_id": "agt_...",
    "definition_revision": 3,
    "correction_rounds": 0
  },
  "scenes": [
    {
      "scene_id": "scn_01J8ZQ...",
      "scene_order": 3,
      "display_locator": {"episode": 2, "scene": 3},
      "heading": {
        "place": "INT",
        "location_label": "父亲的旧书房",
        "time_of_day": "NIGHT"
      },
      "line_span": {
        "start_line_id": "lin_...",
        "end_line_id": "lin_...",
        "line_count": 14
      },
      "opening_excerpt": {
        "line_id": "lin_...",
        "text": "我不去",
        "text_hash": "sha256:..."
      },
      "summary": "儿子拒绝继承家业，父亲拿出怀表",
      "characters": [
        {"requirement_ref": "rq_a1", "speaking": true, "enters": true, "exits": false},
        {"requirement_ref": "rq_a2", "speaking": true, "enters": false, "exits": true}
      ],
      "continuity_hints": ["怀表首次出现"],
      "advisory": {"estimated_shot_count": 9, "estimated_seconds": 52}
    }
  ],
  "requirements": [
    {"requirement_id": "rq_a1", "kind": "character", "label": "陈守义",
     "spec_hint": {"age_band": "60s", "scene_role": "protagonist"}},
    {"requirement_id": "rq_p1", "kind": "prop", "label": "怀表",
     "spec_hint": {"material": "brass", "close_up": true}},
    {"requirement_id": "rq_s1", "kind": "scene", "label": "父亲的旧书房",
     "spec_hint": {"era": "1990s"}}
  ],
  "content_hash": "sha256:...",
  "sealed_at": "2026-11-05T09:00:00.000Z",
  "created_at": "2026-11-05T08:40:00.000Z"
}
```

要点：

- `advisory.estimated_shot_count` 明确是**建议值**，S06 可以完全无视。
- `requirements` 是 revision 级（不是一个场一个数组），场里用 `requirement_ref` 引用。这样同一个"怀表"在三个场出现时只有一条需求、一个 `requirement_id`，S10 解析一次。
- `heading.place` 用 `INT/EXT/INT-EXT` 这种行业惯例值，但 S05 不据此做任何判定——它只是给人看的。

`content_hash` 组成：

```
sha256( JCS({breakdown_id, revision, source_script, scenes, requirements})
        || "\n" || line_set_hash_of_span )
```

不含 `created_at`、`produced_by`、`context`——元数据变化不该改变内容 hash。

### 4.2 `breakdown.revision.created` 事件

topic `drama.breakdown.v2`，分区键 `tenant_id:aggregate_id`，`aggregate_id = breakdown_id`，`aggregate_sequence = revision`。

```json
{
  "specversion": "1.0", "schema_version": "2.0",
  "id": "evt_...", "source": "s05.breakdown",
  "type": "breakdown.revision.created", "subject": "ep_...",
  "time": "2026-11-05T09:00:00.000Z",
  "tenant_id": "tnt_...", "home_cell": "cell-a1",
  "aggregate_id": "bkd_...", "aggregate_sequence": 4,
  "correlation_id": "cmd_...", "causation_id": "cmd_...",
  "traceparent": "00-...-01",
  "data": {
    "breakdown_id": "bkd_...", "revision": 4, "state": "SEALED",
    "episode_id": "ep_...", "branch_id": "br_...",
    "source_script": {"script_id": "scr_...", "revision": 7, "content_hash": "sha256:..."},
    "scene_ids": ["scn_...", "scn_..."],
    "retired_scene_ids": ["scn_..."],
    "requirement_count": 12,
    "content_hash": "sha256:...",
    "occurred_at": "2026-11-05T09:00:00.000Z"
  },
  "data_ref": {
    "service_id": "S05", "id": "bkd_...", "revision": 4,
    "content_hash": "sha256:..."
  }
}
```

三条设计要点：

1. **`data_ref` 非空。** 完整分场结果是正文级负载，走权限化引用（[docs/04 §4](../docs/04-data-consistency.md)："完整剧本不放普通事件"）。`data` 里只放足以让下游决定"要不要去读"的字段。
2. **`retired_scene_ids` 必须发出来。** 消费方（S06/S09）需要知道哪些场没了，否则它们会保留已经失效的引用。这是本服务唯一一个"删除类"信号。
3. **`aggregate_sequence = revision`。** 一版分场只产生一条事件；重排不产生事件（§3.4）。

### 4.3 查询响应的 `authoritative_source`

所有查询（尤其 `GET /episodes/{episode_id}/scenes`）的响应体必须带：

```json
{"authoritative_source": "s05.breakdown", "projection_watermark": "2026-11-05T09:00:00Z"}
```

理由与 [S03 §9](S03-project-catalog.md) 反模式 12 相同：任何代理/投影接口不带这个标记，未来就会有人往这里写数据。

---

## 5. 数据模型

Postgres，schema `breakdown`。所有表带 `tenant_id`，主键/唯一约束一律含 `tenant_id`（E28）。RLS 作为第二道保护。ID 前缀：`bkd_ / scn_ / rq_`。

### 5.1 Breakdown 容器

```sql
CREATE TABLE breakdown.breakdown (
  tenant_id     text NOT NULL,
  breakdown_id  text NOT NULL,              -- 'bkd_01J...'
  project_id    text NOT NULL,
  series_id     text NOT NULL,
  episode_id    text NOT NULL,
  branch_id     text NOT NULL,
  script_id     text NOT NULL,              -- 本 breakdown 面向的剧本
  lifecycle     text NOT NULL CHECK (lifecycle IN ('active','superseded','archived')),
  head_revision integer NOT NULL DEFAULT 0,
  sealed_revision integer,
  created_by    jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, breakdown_id),
  CONSTRAINT bkd_sealed_le_head
    CHECK (sealed_revision IS NULL OR sealed_revision <= head_revision)
);
CREATE INDEX bkd_by_episode
  ON breakdown.breakdown (tenant_id, episode_id, branch_id, lifecycle);
```

不加 `UNIQUE(episode_id, branch_id)`：分支内也可能有多个分场版本（不同编剧、A/B 版）。

### 5.2 分场版本

```sql
CREATE TABLE breakdown.breakdown_revision (
  tenant_id        text NOT NULL,
  breakdown_id     text NOT NULL,
  revision         integer NOT NULL,
  state            text NOT NULL
                   CHECK (state IN ('DRAFT','SEALED','SUPERSEDED','WITHDRAWN')),
  episode_id       text NOT NULL,
  branch_id        text NOT NULL,
  script_id        text NOT NULL,
  script_revision  integer NOT NULL,
  script_content_hash  char(64) NOT NULL,
  script_line_set_hash char(64) NOT NULL,
  context_pack_id  text,
  context_pack_hash char(64),
  context_coverage text NOT NULL CHECK (context_coverage IN ('full','partial','none')),
  produced_by      jsonb NOT NULL,           -- {kind, agent_task_id?, definition_revision?, correction_rounds}
  parent_revision  integer,
  scene_count      integer NOT NULL,
  requirement_count integer NOT NULL,
  content_hash     char(64) NOT NULL,
  sealed_at        timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, breakdown_id, revision),
  UNIQUE (tenant_id, breakdown_id, content_hash),
  FOREIGN KEY (tenant_id, breakdown_id)
    REFERENCES breakdown.breakdown (tenant_id, breakdown_id),
  CONSTRAINT br_state_sealed_chk
    CHECK ((state = 'DRAFT') = (sealed_at IS NULL)),
  CONSTRAINT br_context_full_chk
    CHECK (context_coverage <> 'full' OR context_pack_id IS NOT NULL)
);
CREATE INDEX br_by_state
  ON breakdown.breakdown_revision (tenant_id, breakdown_id, state, revision DESC);
```

`UNIQUE (tenant_id, breakdown_id, content_hash)`：同一份内容不能出现两版。这条会在"用户重复点了一次重新分场、Agent 又给出相同结果"时挡住重复版本，比幂等表更耐久（幂等表 30 天清理）。

### 5.3 场次身份

```sql
CREATE TABLE breakdown.scene (
  tenant_id     text NOT NULL,
  scene_id      text NOT NULL,              -- 'scn_01J...'，永不复用
  project_id    text NOT NULL,
  series_id     text NOT NULL,
  episode_id    text NOT NULL,
  branch_id     text NOT NULL,
  first_breakdown_id text NOT NULL,         -- 哪次分场首次铸造
  first_revision     integer NOT NULL,
  retired_at    timestamptz,
  retire_reason text,                       -- 'merged_into' / 'removed' / 'split'
  merged_into_scene_id text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, scene_id),
  FOREIGN KEY (tenant_id, merged_into_scene_id)
    REFERENCES breakdown.scene (tenant_id, scene_id),
  CONSTRAINT scene_merged_chk
    CHECK ((retire_reason = 'merged_into') = (merged_into_scene_id IS NOT NULL))
);
CREATE INDEX scene_by_episode
  ON breakdown.scene (tenant_id, episode_id, branch_id, retired_at);
```

### 5.4 场次内容（每个 revision 一行）

```sql
CREATE TABLE breakdown.scene_revision (
  tenant_id        text NOT NULL,
  breakdown_id     text NOT NULL,
  revision         integer NOT NULL,
  scene_id         text NOT NULL,
  scene_order      integer NOT NULL,        -- 显示用序号，DEFERRABLE 支持重排
  display_episode  integer NOT NULL,
  display_scene    integer NOT NULL,
  heading_place    text NOT NULL CHECK (heading_place IN ('INT','EXT','INT-EXT')),
  heading_location text NOT NULL,
  heading_tod      text,
  start_line_id    text NOT NULL,
  end_line_id      text NOT NULL,
  line_count       integer NOT NULL,
  opening_line_id  text,
  opening_text     text,
  opening_text_hash char(64),
  summary          text NOT NULL,
  advisory         jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (tenant_id, breakdown_id, revision, scene_id),
  CONSTRAINT scene_rev_order_uq
    UNIQUE (tenant_id, breakdown_id, revision, scene_order)
    DEFERRABLE INITIALLY DEFERRED,
  CONSTRAINT scene_rev_display_uq
    UNIQUE (tenant_id, breakdown_id, revision, display_episode, display_scene),
  CONSTRAINT scene_rev_line_count_chk CHECK (line_count > 0),
  CONSTRAINT scene_rev_excerpt_chk
    CHECK ((opening_line_id IS NULL) = (opening_text IS NULL)
       AND (opening_line_id IS NULL) = (opening_text_hash IS NULL)),
  FOREIGN KEY (tenant_id, breakdown_id, revision)
    REFERENCES breakdown.breakdown_revision (tenant_id, breakdown_id, revision),
  FOREIGN KEY (tenant_id, scene_id)
    REFERENCES breakdown.scene (tenant_id, scene_id)
);
CREATE INDEX scene_rev_by_scene
  ON breakdown.scene_revision (tenant_id, scene_id, revision DESC);
```

`scene_order` 与 `display_scene` 是两个字段而不是一个：`scene_order` 是叙事顺序（可重排），`display_scene` 是 UI 上印出来的数字（通常与 scene_order 一致，但补拍场可能显示为"3A"）。两者都有唯一约束，都有 `DEFERRABLE` 需求——但 `display_scene` 的重排需求更弱，这里先只给 `scene_order` 加 `DEFERRABLE`。

### 5.5 场次角色/道具参与

```sql
CREATE TABLE breakdown.scene_participant (
  tenant_id       text NOT NULL,
  breakdown_id    text NOT NULL,
  revision        integer NOT NULL,
  scene_id        text NOT NULL,
  requirement_id  text NOT NULL,
  speaking        boolean NOT NULL,
  enters          boolean NOT NULL,
  exits           boolean NOT NULL,
  PRIMARY KEY (tenant_id, breakdown_id, revision, scene_id, requirement_id),
  FOREIGN KEY (tenant_id, breakdown_id, revision, scene_id)
    REFERENCES breakdown.scene_revision (tenant_id, breakdown_id, revision, scene_id),
  FOREIGN KEY (tenant_id, breakdown_id, revision, requirement_id)
    REFERENCES breakdown.requirement (tenant_id, breakdown_id, revision, requirement_id)
);
```

`speaking` / `enters` / `exits` 是分场的**事实性观察**，不是 S06 的指令。S06 会用自己的方式表达画面，但 `speaking=true` 的场必须至少有一条对白——这条由 S05 校验（§1.4 校验③的反向）。

### 5.6 需求清单

```sql
CREATE TABLE breakdown.requirement (
  tenant_id       text NOT NULL,
  breakdown_id    text NOT NULL,
  revision        integer NOT NULL,
  requirement_id  text NOT NULL,           -- 'rq_...'
  kind            text NOT NULL
                  CHECK (kind IN ('character','prop','scene','costume','vehicle','animal','sfx')),
  label           text NOT NULL,           -- 人可读稳定键，不保证全局唯一
  spec_hint       jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_scene_id  text NOT NULL,           -- 首次出现的场（便于 S10 推断）
  notes           text,
  PRIMARY KEY (tenant_id, breakdown_id, revision, requirement_id),
  FOREIGN KEY (tenant_id, breakdown_id, revision)
    REFERENCES breakdown.breakdown_revision (tenant_id, breakdown_id, revision)
);
CREATE INDEX requirement_by_label
  ON breakdown.requirement (tenant_id, breakdown_id, revision, kind, label);
```

### 5.7 场间关系

```sql
CREATE TABLE breakdown.scene_relation (
  tenant_id      text NOT NULL,
  breakdown_id   text NOT NULL,
  revision       integer NOT NULL,
  from_scene_id  text NOT NULL,
  to_scene_id    text NOT NULL,
  relation       text NOT NULL
                 CHECK (relation IN ('adjacent','same_location','same_time',
                                     'flash_forward','flash_back','parallel')),
  note           text,
  PRIMARY KEY (tenant_id, breakdown_id, revision, from_scene_id, to_scene_id, relation),
  FOREIGN KEY (tenant_id, breakdown_id, revision, from_scene_id)
    REFERENCES breakdown.scene_revision (tenant_id, breakdown_id, revision, scene_id),
  FOREIGN KEY (tenant_id, breakdown_id, revision, to_scene_id)
    REFERENCES breakdown.scene_revision (tenant_id, breakdown_id, revision, scene_id),
  CONSTRAINT scene_relation_no_self CHECK (from_scene_id <> to_scene_id)
);
```

`flash_back` / `flash_forward` 是 S09 建跨集依赖边与 S16 判故事时间的重要输入，但**S05 不做故事时间判定**——它只记录"这两场在叙事上是什么关系"。真正的 story clock 在 S16。

### 5.8 需求解析缓存（非权威）

```sql
CREATE TABLE breakdown.requirement_resolution_cache (
  tenant_id       text NOT NULL,
  breakdown_id    text NOT NULL,
  revision        integer NOT NULL,
  requirement_id  text NOT NULL,
  resolved_json   jsonb NOT NULL,          -- S10 返回的 BindingSet 片段
  resolved_at     timestamptz NOT NULL,
  valid_until     timestamptz NOT NULL,    -- 短 TTL，默认 15 分钟
  PRIMARY KEY (tenant_id, breakdown_id, revision, requirement_id)
);
```

**这张表是缓存，不是权威。** 任何代码路径不得用它做"资产是否可用"的判定——[S10 §0](S10-asset-catalog.md) 明确"resolve ≠ rights valid"，权利必须实时复查。表名里带 `cache` 是为了让审阅者一眼看出它的地位。

### 5.9 幂等与 Outbox

```sql
CREATE TABLE breakdown.idempotency_record (
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

CREATE TABLE breakdown.outbox (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,        -- breakdown_id
  aggregate_sequence bigint NOT NULL,      -- revision
  event_type         text NOT NULL,
  partition_key      text NOT NULL,        -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx
  ON breakdown.outbox (id) WHERE published_at IS NULL;
```

### 5.10 不建的表

- **不建 `inbox`。** S05 目前不消费任何事件。若将来要消费 S04 的 `narrative.script.revision.created` 做"剧本变了"的提示，必须连同消费逻辑一起加（§11 C6 讨论了是否该加）。
- **不建 `shot_projection`。** 镜头归 S06。S05 连投影都不建——一旦建了，就会有人拿它做"这场有几个镜头"的判断。
- **不建 `asset_binding`。** 绑定归 S10。
- **不建 `scene_canon` / `continuity_state`。** 归 S16。

---

## 6. 幂等与并发

### 6.1 写入事务模板

```python
with db.transaction() as tx:                       # REPEATABLE READ
    rec = tx.idempotency_begin(tenant_id, command_id, request_hash, cmd_type)
    if rec.replayed:
        return rec.response_code, rec.response_body

    # 1. 校验 S04 当前 revision 与 hash（读 S04，不写）
    script = s04.get_revision(script_id, script_revision)
    assert script.state == 'SEALED'
    assert script.content_hash == body.script_ref.content_hash      # 否则 409

    # 2. 校验五条语义规则（纯本地计算）
    validate_coverage(...); validate_order(...); validate_speakers(...)

    # 3. 铸造 / 继承 scene_id
    scenes = mint_or_inherit_scene_ids(...)

    # 4. 写 breakdown_revision + scene_revision + requirement + participant
    # 5. 写 outbox（同一事务）
    # 6. 标记旧 revision SUPERSEDED、旧场 retired
    tx.idempotency_finish(tenant_id, command_id, 200, response)
```

第 1 步读 S04 是**外部读**，不参与事务。这意味着在"读完 S04、写入之前"剧本可能又变了。处理方式：`breakdown_revision` 里存下读到的 `script_content_hash`，S08 在提交 manifest 时会再校验一次（[docs/04 §2](../docs/04-data-consistency.md)："关键前置版本在 S08 commit 时再次校验"）。S05 不试图消除这个窗口——它无法跨库加锁。

### 6.2 并发

| 场景 | 机制 |
|---|---|
| 同一 breakdown 并发两次 `revisions` | `UPDATE breakdown SET head_revision = head_revision + 1 WHERE ... AND head_revision = $parent`；0 行 → 409 `BREAKDOWN_REVISION_CONFLICT` |
| 并发重排 | 唯一约束 `DEFERRABLE` + 完整列表提交；两个并发重排中先提交者胜，后者的 `scene_order` 冲突 → 409 |
| 并发 `:seal` | `UPDATE ... WHERE state = 'DRAFT'`；0 行 → 409（已 seal 或已 withdraw） |
| 并发 `:retire` 同一场 | 幂等：第二次仍是 200，不报错 |

### 6.3 事件与人员的比例

每一版 `SEALED` revision 发一条事件。`DRAFT` revision **不发事件**——草稿不该触发下游。

因此 `aggregate_sequence = revision` 会有跳号（revision 3 是 DRAFT 不发，4 是 SEALED 发）。消费侧按 §6.5 的规则处理缺口，不能假设连续。

这条取舍的原因：分场期间会有大量草稿（人在 UI 上反复调），每版都发事件会让 S06/S09 收到几十条无效变更。

### 6.4 重排为什么不发事件

`scene-order` 改变会影响 S41 的 EDL 与 S09 的相邻边。但它不发事件，理由是：

- 发出事件会让消费方重新拉取整版分场，而那版分场的 `content_hash` 没变——消费方无法区分"顺序变了"与"内容变了"。
- 顺序的正确来源是 S41 直接查 `GET /episodes/{id}/scenes`（带 `authoritative_source`），而不是靠事件累积。

**代价**：S09 的相邻边在重排后不会自动更新，需要 S09 自己定期核对或由 S41 触发。这是已知的延迟，见 §11 C5。

### 6.5 事件消费侧的缺口规则（写给下游）

本服务不消费事件，但下游消费 `breakdown.revision.created` 时必须：

- 按 `(aggregate_id, aggregate_sequence)` 检测缺口，缓冲最多 30s，超时后回源 `GET /breakdowns/{id}/revisions/{revision}`。
- **终态不倒退**：已 `SUPERSEDED` 的 revision 收到更早的事件不得恢复成当前版本。
- 重放（`replay_side_effects=false`）时不得触发新的下游副作用。

---

## 7. 失败分支

### 7.1 输入阶段

| 场景 | 行为 | 返回 |
|---|---|---|
| `script_ref.revision` 已非最新 sealed | 拒绝，回执带最新 ref | 409 `SCRIPT_REVISION_MOVED` |
| 引用 DRAFT 剧本（非 preview） | 拒绝 | 422 `SCRIPT_NOT_SEALED` |
| `context_pack_ref` 缺失 | 允许，`context_coverage='partial'` | 200 |
| `context_pack_ref` 指向已失效 pack | 拒绝，提示重新取 | 422 `CONTEXT_PACK_EXPIRED` |
| `proposal_ref` 的 AgentTask 失败 | 由人工分场或重新调 S19 | 无 proposal 即可 200 |

### 7.2 校验阶段

| 场景 | 行为 | 返回 |
|---|---|---|
| 有行未分配（漏行） | 拒绝，列出未分配的 `line_id` | 422 `LINE_COVERAGE_GAP` |
| 同一行在两场 | 拒绝，列出重复的 `line_id` | 422 `LINE_COVERAGE_OVERLAP` |
| 场顺序与行顺序矛盾 | 拒绝 | 422 `LINE_ORDER_VIOLATION` |
| 对白发言人不在本场角色内 | 拒绝（不自动加人） | 422 `SPEAKER_NOT_IN_SCENE` |
| 摘录文本与 S04 不一致 | 拒绝 | 422 `SCRIPT_LINE_DRIFT` |
| 角色列表非空但无人说话 | **允许**（纯动作场） | 200 |
| 场数超过 `max_scenes` | 拒绝 | 422 `SCENE_LIMIT_EXCEEDED` |
| 场的 `line_count = 0` | 拒绝（DB CHECK 也会挡） | 422 |

最后两行值得说明：一个场必须有至少一行。空场在叙事上没有意义，在实现上会让 `line_span` 无法表达——它会导致 §1.4 校验①的"每行恰好属于一个场"出现歧义。

### 7.3 Agent 阶段

| 场景 | 行为 |
|---|---|
| S19 超时 | 有界重试 1 次；仍失败 → 交由人工或返回 503 |
| S19 返回结构非法 | 结构校验失败 → 有界纠正（最多 2 次） |
| 纠正 2 次仍非法 | 422 + 完整违规列表 + `agent_task_id`（便于人工查看轨迹） |
| Agent 输出含 `tenant_id` | 422 `AGENT_SUPPLIED_TENANT`，且**告警**（prompt 可能被污染） |
| Agent 输出含 `scene_id` | 丢弃并重铸，不报错（模型常见行为，不构成安全事件） |

### 7.4 存储与事件

| 场景 | 行为 |
|---|---|
| 主键冲突（并发同 revision） | 409 `BREAKDOWN_REVISION_CONFLICT` |
| `content_hash` 冲突（重复内容） | 返回既有 revision 并附 `deduplicated: true` |
| Outbox 写入失败 | 整个事务回滚 |
| Relay 失败 | 保留 `published_at IS NULL`，指数退避 |
| 写库成功、响应丢失 | 幂等表在 30 天内保证重试回放；之后靠 `(breakdown_id, revision)` 主键 |

### 7.5 依赖不可用

| 依赖 | 不可用时的行为 |
|---|---|
| S04 | 无法分场（422/503）。**不允许**用本地缓存的分场结果继续——那会用过期剧本 |
| S18 | 允许，`context_coverage='partial'` |
| S19 | 人工分场可用；自动分场 503 |
| S10 | 完全不影响（S05 不做解析） |

### 7.6 回滚与独立升级（E01）

- 迁移必须向后兼容一个版本（`current` 与 `current-1` 同时运行）。
- 回滚不撤销已 `SEALED` 的 revision——它是事实。回滚只影响"新命令如何处理"。
- `scene_revision.scene_order` 的 `DEFERRABLE` 唯一约束在回滚时不需要改动（结构稳定）。

---

## 8. 可观测

### 8.1 指标

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `s05_breakdown_revision_total` | counter | `state`, `produced_by_kind` | 草稿/封版比例、Agent vs 人工 |
| `s05_breakdown_latency_p95` | histogram | `path` | 目标 ≤ 8s（不含 Agent）；含 Agent ≤ 90s |
| `s05_scene_count` | histogram | `episode_id` | 分场粒度异常检测（一集 400 场是坏数据） |
| `s05_validation_failure_total` | counter | `rule` | 五条校验各自的失败率 |
| `s05_line_coverage_gap_lines` | histogram | — | 漏行数量分布（应恒为 0） |
| `s05_agent_correction_rounds` | histogram | `agent_task_id` | 纠正轮次分布，>1 说明 prompt 有问题 |
| `s05_scene_id_inherit_ratio` | gauge | — | 改稿时身份继承率；骤降说明重新分场策略退化 |
| `s05_script_revision_moved_total` | counter | — | 并发改稿压力 |
| `s05_requirement_count` | histogram | `kind` | 需求规模 |
| `s05_outbox_unpublished_age_seconds` | gauge | — | P99 ≤ 10s |
| `s05_cross_tenant_rejection_total` | counter | — | E28 |

### 8.2 告警

| 条件 | 级别 | 动作 |
|---|---|---|
| `s05_line_coverage_gap_lines` 出现非 0 | P1 | 校验有 bug，立即回滚该版本 |
| `s05_validation_failure_total{rule="SPEAKER_NOT_IN_SCENE"}` > 20%/h | P2 | Agent prompt 或 S19 输出结构退化 |
| `s05_agent_correction_rounds` P95 > 1 | P2 | 同上 |
| `s05_scene_count` P99 > 200 | P2 | 可能把整集当成一个场或反向碎片化 |
| `s05_scene_id_inherit_ratio` 单次改稿 < 0.3 | P2 | 重新分场策略可能退化成 `fresh`，会导致整集重做 |
| `s05_outbox_unpublished_age_seconds` P99 > 60s | P2 | 事件总线 |

### 8.3 审计

每次 `SEALED` / `SUPERSEDED` / `retire` 都落一条本地审计（谁、何时、基于哪个 `command_id`、`agent_task_id` 是什么）。S45 拉取。**S05 不实现审计完整性证明**——只保证写入顺序即时间顺序（`created_at` 用 DB 时钟）。

---

## 9. 测试清单

标注 `[E##]` 的对应 [docs/16 §5](../docs/16-migration-acceptance.md)。全部用 mock 依赖（mock S04、mock S18、mock S19）。

### 9.1 幂等与并发 `[E12]`

| # | 用例 | 期望 |
|---|---|---|
| T05-01 | 同 `command_id` 同 body 两次 `POST /breakdowns` | 第二次 `replayed: true`；只有一版 revision、一条 outbox |
| T05-02 | 同 `command_id` 不同 body | 409 `IDEMPOTENCY_KEY_REUSED` |
| T05-03 | 幂等表已清理（30 天后）重试同 body | 因 `(breakdown_id, revision)` 或 `content_hash` 返回既有结果，不新建 |
| T05-04 | 并发两版 `revisions` 同 `parent_revision` | 一个 200，一个 409 `BREAKDOWN_REVISION_CONFLICT` |
| T05-05 | 并发两次 `:seal` | 一个 200，一个 409 |
| T05-06 | 同内容提交两次 | 返回既有 revision，`deduplicated: true` |

### 9.2 分场语义（E03 / E04 核心）

| # | 用例 | 期望 |
|---|---|---|
| T05-07 | 剧本 100 行，分场漏掉第 57 行 | 422 `LINE_COVERAGE_GAP`，列出 `lin_...` |
| T05-08 | 同一行被分到两场 | 422 `LINE_COVERAGE_OVERLAP` |
| T05-09 | 场的 `start_line` 晚于 `end_line` | 422 `LINE_ORDER_VIOLATION` |
| T05-10 | 三段的分场顺序是 1,3,2（行区间 1-10,21-30,11-20） | 422 `LINE_ORDER_VIOLATION` |
| T05-11 | 对白 `lin_57` 的发言人是"陈守义"，但本场 `characters` 里没有他 | 422 `SPEAKER_NOT_IN_SCENE` |
| T05-12 | 纯动作场（无对白，有两个角色） | 200 |
| T05-13 | 摘录文本被 Agent 改了一个字 | 422 `SCRIPT_LINE_DRIFT` |
| T05-14 | 场 `line_count=0` | 422 + DB CHECK |

### 9.3 身份与序号（E03 / E07）

| # | 用例 | 期望 |
|---|---|---|
| T05-15 | 改稿后重新分场，某场首行不变 | 该场继承原 `scene_id` |
| T05-16 | 改稿后两场合并 | 一场继承、一场 `retired` 且 `merged_into_scene_id` 正确 |
| T05-17 | 一场拆成两场 | 两个新 `scene_id`，原场 `retired`（`retire_reason='split'`） |
| T05-18 | `scene_id_continuity='fresh'` | 全部新 ID，旧场全 `retired` |
| T05-19 | 重排场次顺序 | `scene_order` 变、`scene_id` 不变、`content_hash` 不变 |
| T05-20 | 重排提交 delta（只给一个场的序号） | 400（必须完整列表） |
| T05-21 | 两场继承同一个旧 `scene_id` | 拒绝（§3.3 约束 1） |
| T05-22 | retired 场在新 revision 中被引用 | 拒绝 |

### 9.4 信任边界 `[E28]`

| # | 用例 | 期望 |
|---|---|---|
| T05-23 | Agent 输出含 `tenant_id` | 422 `AGENT_SUPPLIED_TENANT` + 告警 |
| T05-24 | Agent 输出含 `scene_id` | 丢弃重铸，200 |
| T05-25 | 用 A 租户 TCT 读 B 租户 breakdown | 404，与不存在无差别 |
| T05-26 | `script_ref` 指向另一租户的剧本 | 404（S04 侧发起拒绝，S05 不透传） |

### 9.5 事件 `[E13][E32]`

| # | 用例 | 期望 |
|---|---|---|
| T05-27 | 业务写入后、outbox 前崩溃 | 事务回滚，无 revision 也无 outbox |
| T05-28 | publish 后、`published_at` 更新前崩溃 | 重复投递，下游去重 |
| T05-29 | DRAFT revision | 不发事件 |
| T05-30 | revision 3 DRAFT、4 SEALED | 下游收到 `aggregate_sequence=4`，按缺口规则回源 |
| T05-31 | 事件 payload 校验 | 符合 envelope v2，`additionalProperties:false` |
| T05-32 | `data_ref` 指向的分场可被授权读取 | 无权限调用方读不到 |

### 9.6 依赖降级

| # | 用例 | 期望 |
|---|---|---|
| T05-33 | S18 不可用 | 200，`context_coverage='partial'`，revision 上有记录 |
| T05-34 | S19 不可用 + 人工分场 | 200，`produced_by.kind='human'` |
| T05-35 | S04 不可用 | 503，不使用本地缓存 |
| T05-36 | S04 revision 在读写之间变化 | 409 `SCRIPT_REVISION_MOVED` |

### 9.7 独立升级 `[E01]`

| # | 用例 | 期望 |
|---|---|---|
| T05-37 | 滚动升级期间新旧实例并发写 | 无主键冲突泄漏，唯一约束按预期 |
| T05-38 | 回滚后新命令的处理 | 已 SEALED 的 revision 不受影响 |
| T05-39 | 最小权限 | 只连本 schema；无跨库连接 |

---

## 10. 与上下游服务的接口约定

### 10.1 上游

| 服务 | 提供什么 | 调用方式 | 失败影响 |
|---|---|---|---|
| S01 | TCT 与授权上下文 | SDK 内 | 无法处理任何请求 |
| S04 | `ScriptRevision`（SEALED）+ 行列表 | `GET /scripts/{id}/revisions/{rev}` | 无法分场 |
| S18 | `ContextPack` | `GET /context-packs/{id}` | 降级为 `partial` |
| S19 | `breakdown` 角色的候选分场 | `POST /agent-tasks` | 人工分场可用 |
| S02 | 策略上下文（档位是否允许自动分场） | `GET /authorization-context` + 策略判定 | 无法自动分场 |

### 10.2 下游

| 服务 | 用 S05 什么 | 契约 |
|---|---|---|
| S06 | 从哪版分场生成镜头；`scene_id` | 本文 §4.1 |
| S09 | `SCENE_DERIVED_FROM_SCRIPT` 边的两端 | `line_span` + `source_script` |
| S10 | `requirements` 作为 `resolve` 的输入 | `GET /scenes/{id}/requires` |
| S08 | manifest 的 `source_revisions` 里引用 `breakdown_revision.content_hash` | §4.1 |
| S41 | 场次顺序（EDL 的基础） | `GET /episodes/{id}/scenes` |
| S16 | 正史抽取的场次上下文（只读） | `scene_id` + `line_span` |
| S21 | 子工作流的扇出粒度（每场一个子工作流） | `scene_id` 列表 |

### 10.3 S05 → S06 的严格边界

| 问题 | 谁答 |
|---|---|
| 这一集有几场、每场从哪句到哪句 | **S05** |
| 每一场需要哪些角色和道具（需求） | **S05** |
| 每一场有几个镜头 | **S06** |
| 每一镜的机位、景别、运镜 | S06 |
| 这一场的最小可用镜头数 | S06 |

`advisory.estimated_shot_count` 存在，但 S06 **不得**把它当成目标或约束。它的唯一用途是让人在 UI 上有个规模感。

### 10.4 S05 → S10 的需求契约

`requirements` 的 `label` 是 S10 的匹配键。这条契约有两个必须写清的约定：

1. **同一 revision 内同 `kind` 的 label 语义上唯一**，但由于 label 是人写的，允许近似重复（"父亲"与"陈守义"可能是同一人）。S10 负责合并，S05 不预先规范化。
2. **S05 不保证 label 跨 revision 稳定**。改稿可能把"怀表"改成"银怀表"。S10 不能把 label 当资产身份——它必须先解析成 `asset_id` 再判断。

第 2 条是 S10 的实现约束，写在这里是因为没有别的地方会写。

### 10.5 S05 → S09 的边

S05 提供两条边的端点信息（但边由 S09 建）：

| 边 | 从哪里来 |
|---|---|
| `SCENE_DERIVED_FROM_SCRIPT` | `scene.line_span` + `source_script.script_id/revision` |
| `SCENE_FOLLOWS_SCENE`（场间相邻） | `scene_order` + `scene_relation(adjacent)` |

S05 **不写 S09 的边表**，也不在重排时主动通知 S09（§6.4）。S09 需要自己核对。

---

## 11. 契约缺口（需要先行修改，不要自行补）

按 [AGENTS.md](../AGENTS.md)「契约与文档冲突 → 停下报告」与 [docs/16 §6](../docs/16-migration-acceptance.md)：

| # | 缺口 | 建议 |
|---|---|---|
| C1 | `service.yaml` / `service-catalog.v2.json` 只声明 `POST /breakdowns`、`GET /breakdowns/{id}`；本文需要的 `:seal`、`/revisions`、`scene-order`、`:retire`、`/requires` 全部没有 | 新增 `contracts/openapi/s05-breakdown.v2.yaml`，由本文 §2 派生 |
| C2 | 没有 `SceneBreakdownRevision` 的 JSON Schema。`contracts/domain-v1/` 下只有 `shot-spec.v1`、`change-plan.v1`、`generation-request.v1`、`qc-report.v1` | 新增 `contracts/domain-v1/scene-breakdown.v1.schema.json`（本文 §4.1 可作为基线）。**S06/S09/S10 都以它为准** |
| C3 | `breakdown.revision.created` 没有 `data` 最小字段集 schema | 新增 `contracts/events/breakdown-revision-created.v2.schema.json`（本文 §4.2） |
| C4 | 没有任何契约定义"`scene_id` 由谁铸造、如何跨 revision 继承"。这与 [S03 C03-5](S03-project-catalog.md)（`shot_id`/`shot_lineage_id`）是同一类缺口 | 本文采纳：S05 铸造 `scene_id`，继承规则见 §3.3。**需要写进 catalog 的 `data_owner` 说明**，否则下一个人还会问 |
| C5 | 重排不发事件（§6.4），但 S09 的相邻边与 S41 的 EDL 依赖顺序。没有文件说明谁负责在重排后刷新 | 二选一：(a) 新增 `breakdown.scene-order.changed` 事件；(b) 明确 S09/S41 必须主动查询。本文取 (b)，但**需要 docs/07 确认** |
| C6 | S05 的 `depends_on` 只有 S04/S19，缺 S18（ContextPack）、S02（自动分场策略）、S01（身份）。且 `service.yaml` 的 acceptance 隐含需要 S10 | catalog 的 `depends_on` 是启动依赖还是运行协作未定义。[S35 C3](S35-technical-qc.md) 取"读 ShotSpec 走 query 可接受"。本文同此立场，但需契约维护方明确 |
| C7 | `line_span` 用 `start_line_id` / `end_line_id` 表达，但没有任何契约保证这两个 ID 在 S04 里是"可比较顺序"的。S04 的行顺序目前只隐含在 `line_ordinal` 里 | 需要 S04 明确：`line_ordinal` 在一版 revision 内单调，且 `(script_id, revision, line_ordinal)` 唯一。否则 S05 的校验②无法实现 |
| C8 | `requirements[].label` 的匹配规则没有任何契约；S10 的 `resolve` 输入契约里也没有"需求清单"这个形态 | 见 [S10 §0](S10-asset-catalog.md) 与本文 §1.3。需要新增 `contracts/domain-v1/asset-requirement.v1.schema.json`，明确 `kind`/`label`/`spec_hint` 的语义，**否则 S05 与 S10 会各写一套 label 规范化** |
| C9 | `generation-command.v2` 与 [`event-envelope.v2`](../contracts/event-envelope.v2.schema.json) 都要求 `manifest_ref`，但 S05 的分场发生在 manifest 创建之前（[docs/07 §1](../docs/07-production-workflows.md) 第 2 步 vs 第 1 步） | 若命令必须带 `manifest_ref`，则分场只能发生在 Run 里，人工预分场无法进行。**需要 docs/07 或 execution-context 明确"哪些命令允许 `manifest_ref` 为空"**。本文暂按"必填"实现，见 §12 U3 |
| C10 | `scene_relation` 的 `flash_back`/`flash_forward` 与 S16 的 story clock 有语义重叠，没有任何文档说明谁是权威 | 本文立场：S05 只记录"叙事关系"，story clock 归 S16（[S16 §1.1](S16-story-memory.md)）。需要 docs/01 补一句 |
| C11 | `max_scenes` 上限（本文取 400）没有依据 | 见 §12 U1 |

---

## 12. 未决

| # | 未决问题 | 本文临时立场 | 需要谁裁决 |
|---|---|---|---|
| U1 | 单集场数上限 | 400（超过几乎必然是数据错误） | 产品 |
| U2 | `scene_id_continuity` 默认值 | `inherit_by_line_anchor` | 创作团队 |
| U3 | 预分场（无 manifest）能否落库 | 暂按"必须带 `manifest_ref`"实现，可能阻塞人工预分场 | docs/07 作者（C9） |
| U4 | 重排后 S09/S41 的一致性责任 | S09/S41 主动查询（C5） | docs/07 / S09 / S41 |
| U5 | `requirements` 是否要支持"同一 label 多版本"（例如"怀表（旧）"与"怀表（新）"） | 不支持；用 `spec_hint` 区分 | 创作 + S10 |
| U6 | 场的 `summary` 由谁写、是否允许 Agent 生成后人工改 | 允许 Agent 生成，人工可在 `DRAFT` 阶段改；`SEALED` 后不可改 | 产品 |
| U7 | `advisory.estimated_shot_count` 是否保留 | 保留但明确非约束 | 创作团队 |
| U8 | 校验失败时是否允许"人工强制通过" | **不允许**。漏行必须修 | 架构（这条关系 E04） |
| U9 | 分场是否要在 E 矩阵里新增专门条目 | E03 覆盖了局部改镜，但"分场漏行"没有专门验收项 | docs/16 作者 |

### 12.1 实施顺序建议

1. **C2（schema）+ C7（行顺序契约）先行**：没有这两样，五条校验里有一半写不了。
2. `breakdown` / `breakdown_revision` / `scene` / `scene_revision` / `requirement` 五张表 + 五条校验。
3. 人工分场路径（不依赖 S19）——先打通"人能做"，再做"Agent 能做"。
4. `:seal` + outbox + 事件。
5. `scene_id_continuity` 的三种策略，重点测 `inherit_by_line_anchor`。
6. 接 S19 `breakdown` 角色 + 有界纠正。
7. 重排 + `DEFERRABLE` 唯一约束的并发测试。

第 3 步的顺序是刻意的：**先让人工路径跑通，Agent 只是加速**。如果反过来，S19 不稳定时整个分场功能不可用，而它其实是可选的（§3.1）。
