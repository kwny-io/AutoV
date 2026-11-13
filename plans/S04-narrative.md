# S04 · 创意与剧本 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。SLO 与容量均为 docs/03 的规划值，**未实测**。

上游依据：[docs/05 §2/§3](../docs/05-cross-episode-memory.md)（剧本→正史写入路径、剧本意图与成片观测分层）、[docs/01 §3](../docs/01-enterprise-architecture.md)、[docs/04](../docs/04-data-consistency.md)、[docs/06 §3](../docs/06-automation-governance.md)（创作接受门）、[docs/07 §1/§2](../docs/07-production-workflows.md)、[docs/08 §1/§2](../docs/08-agents-prompts.md)（Agent 只返回候选）、[docs/16 §5](../docs/16-migration-acceptance.md)、[S04 规格](../docs/design-package/services/S04-narrative.md)。

配套：[S03](S03-project-catalog.md)、[S05](S05-breakdown.md)、[S06](S06-storyboard.md)、[S08](S08-revision-snapshot.md)、[S16](S16-story-memory.md)、[S18](S18-context-retrieval.md)、[S19](S19-agent-runtime.md)、[S38](S38-subtitles.md)。

---

## 0. 一句话边界

**S04 是"这部剧的剧本文字长什么样、哪一版是作者写的那一版"的唯一权威。它不是正史、不是接受指针、不是分场、不是分镜、不是译文。**

S04 存在的原因：从大纲到成片之间有一段**纯文字**的生产过程。这段过程的产物必须有一个稳定身份与不可变版本，否则 S16 无法回答"这条正史来自剧本的哪一句话"（R1 规则要求来源可核验、hash 一致），S05 无法固定"这一场分解自哪一版剧本"，S38 无法回答"这句字幕对应哪一版原句"。

### 0.1 写什么 / 不写什么

| 事实 | 唯一写者 | S04 的角色 |
|---|---|---|
| `script_id` 稳定身份、剧本文档结构 | **S04** | 唯一写者 |
| `ScriptRevision`（不可变剧本正文） | **S04** | 唯一写者 |
| `BriefRevision`（创意简报） | **S04** | 唯一写者 |
| 对白行的稳定 `line_id` 与原文 revision | **S04** | 唯一写者（见 §1.2 的边界） |
| `StoryArcProposal`（故事弧候选结构） | **S04** | 唯一写者（作为**候选**） |
| 剧本是否已被接受 | **S08**（accepted pointer）/ **S40**（决定）/ **S02**（门） | 只读观测，不写 |
| 从剧本抽出的正史 claim | **S16** | 完全不写；只提供来源 revision |
| 场次切分（Scene） | **S05** | 完全不涉及 |
| 镜头切分、ShotSpec、对白在镜头内的绑定 | **S06** | 只提供 `line_id` 与原文 |
| 发音词典 | **S15** | 不涉及 |
| 译文 | **S38** | 不涉及 |
| 场景总数等结构统计 | **S05** | 不涉及（见 C04-5） |

判据：

- "这句话原本怎么写的" → S04 回答。
- "这句话在镜头里怎么说、什么情绪、什么时长" → **S06 回答**（ShotSpec 的 `dialogue[]`）。
- "这句话现在算不算正史" → S16 回答。
- "这条正史来自哪一句、哪一版" → S04 提供 `line_id + revision + content_hash`，S16 做核验。
- "这集分了几场" → S05 回答。

### 0.2 六个明确不做

1. **不判定接受。** 剧本 revision 一旦 seal 就不可变，但"被接受"这个事实在 S08。S04 存 `submitted` 状态只表示"作者认为可以送审了"，**不代表任何接受语义**。这条最容易被写错：一个 `accepted` 布尔字段会让 S16 的写入路径出现第二个触发源。
2. **不写正史。** 抽取出来的 claim 进入 S16 的 `claim_candidate` 隔离区，S04 连 handle 都不存（只存"这次抽取任务的 ID"用于审计）。S04 一旦缓存 claim，就成了第二正史权威。
3. **不做场景切分。** 剧本里有"第 3 场"这样的**文本标记**，但那是写作习惯，不是 `scene_id`。S05 读剧本后自己切场、自己铸 ID。S04 绝不因为文本里有"第 3 场"就生成 `scene_id`。
4. **不改对白行身份。** 一句台词改了字，`line_id` 不变、`revision` +1。删掉一句，`line_id` 标 `retired`，不物理删除（S16 的 claim 可能引用它）。
5. **不解释剧情含义。** `StoryArcProposal` 是 Agent 给的候选结构（幕、转折、伏笔埋点），S04 存它、版本化它，但**不把它当正史**，也不因为它的存在改写任何 claim。
6. **不生成译文、不调模型做本地化。** 译文归 S38；本地化作业是付费任务，必须走 S26 准入。

---

## 1. 关键区分（最容易写错的五处）

### 1.1 `Script` 与 `ScriptRevision`

| | `Script` | `ScriptRevision` |
|---|---|---|
| 是什么 | 一部剧的剧本文档身份 | 某一版正文，不可变 |
| 跨版本是否变 | 不变 | 每次改稿新增一条 |
| 是否含正文 | **否**（只有元数据） | **是**（正文分块存储） |
| 谁引用 | 人、UI、S03 的剧集 | S05（从哪版分解）、S06（对白来源）、S08（`source_revisions`）、S16（R1 核验） |
| 可否改 | 可改标题/可见性 | **永不修改** |

改稿的唯一方式是追加 revision。这与 S08 的 manifest、S10 的 BindingSetRevision、S16 的 claim revision 是同一个模式。**不允许"编辑草稿 revision"**：一旦允许，任何引用它的 `content_hash` 就失去意义。

"草稿"不是一个 revision 的状态，而是**未 seal 的 revision**（见 §3.1）。seal 之后正文冻结。

### 1.2 剧本原句 vs ShotSpec 对白（本服务最重要的边界）

同一句台词在生产链上出现两次：

| 位置 | 字段 | 拥有者 | 用途 |
|---|---|---|---|
| `ScriptRevision` 的对白块 | `line_id + text` | **S04** | 原文、作者意图、S16 的证据来源 |
| `ShotSpec.dialogue[]` | `line_id + character_id + text + locale + emotion + voice` | **S06** | 这一镜怎么念、谁念、什么情绪 |

`shot-spec.v1` 的 `dialogue[].text` 是**内嵌字符串**，不是引用。这意味着同一句话的文本在 S04 与 S06 各存一份。这不是设计疏忽，是刻意的：ShotSpec 必须是自包含的（S20 编译、S25 路由、S31 配音、S35 QC 全部只读 ShotSpec，不去读剧本），否则一次剧本改稿会让所有在途镜头的输入指纹全变。

那么"哪一份是权威"？

- **文字事实的权威在 S04。** `line_id` 由 S04 铸造；`text` 的每一次变更都必须是 S04 的新 revision。
- **S06 的 `dialogue[].text` 是派生的投影**，派生规则是"取 S04 该 `line_id` 在该 revision 下的 `text`，字节相等"。
- 因此 S06 写入 ShotSpec 时必须记录 `script_line_ref: {script_id, revision, line_id, text_hash}`，并做字节相等校验。**不一致则拒绝写入，不静默采用其中一份。**

这条写进 §10 C04-1：`shot-spec.v1` 没有 `script_line_ref` 字段，而 docs/08 说"编译器不会改写对白事实"。在契约补齐之前，S06 只能在自身库里存这个引用。这是本文与 S06 方案必须共同遵守的协议。

**什么是"改写对白事实"**：把"我不去"改成"我不想去"、把问句改成陈述、把两句话合并、把称谓从"你"改成"您"。这些都必须先在 S04 改、再让 S06 出新 revision。S20 编译时不许做任何文本变换（[S20 §1.2](S20-prompt-compiler.md)）；S25 不许为了塞进时长而删字（[S25 §2.2](S25-model-routing.md)）。

### 1.3 对白行（line）与 DialogueRevision

- `line_id` 是**稳定身份**：一句台词从诞生到退役，ID 不变，即使它移到别的场次、换了说话人。
- `DialogueRevision` 是**这条行的不可变内容版本**：`{text, speaker_id, parenthetical, locale}`。

为什么把对白单独版本化，而不是只跟着 `ScriptRevision` 走：

1. **S38 的失效传播需要它。** [S38 §2.3](S38-subtitles.md) 的流程是"S06 对白 revision V3→V4 → 译文标 stale"。如果一句台词的变更只能通过整个 `ScriptRevision` 的新旧对比才能发现，那么每次改一个字都要全量 diff 整集剧本。
2. **S09 的字段级边需要它。** `AUDIO_FROM_DIALOGUE` 边的 `field_path` 是 `/dialogue`，粒度是"这条行"，不是"这版剧本"。
3. **S16 的 R1 需要精确来源。** 一条 `knowledge_state` claim 的证据是"第 2 集第 14 行的某句台词"，`source_refs[].span` 指向 `line_id`。如果只有 `ScriptRevision` 级来源，粒度太粗，无法判断"这一句是否还在"。

`DialogueRevision` 与 `ScriptRevision` 的关系：**一次 seal 会把当前所有 `line` 的 `DialogueRevision` 快照成一个集合，并把这个集合的 hash 写进 `ScriptRevision.content_hash` 的组成里**（§2.3 的 `script_revision_line` 表）。所以两者不是两套并行权威，是"行的版本"和"集合的版本"。

### 1.4 `StoryArcProposal` 是候选结构，不是正史

Agent（S19 的 `writer` 角色）可以产出"这个故事应该有三幕、第二个转折在第 5 集、伏笔在第 2 集埋在第 11 集回收"这样的结构建议。

- 它是**候选**，状态机只有 `PROPOSED / ADOPTED / REJECTED / SUPERSEDED`（§3.3）。
- `ADOPTED` 只表示"作者决定按这个结构写"，**不产生任何正史事实**。真正的正史是剧本正文里写出来的内容，经过 S08 接受、S16 提交。
- 因此 `StoryArcProposal` 的读者只有两个：写剧本的人（UI），和 S18 的编剧 ContextPack（作为"作者意图"背景，不是"已发生事实"）。

这条区分对应 docs/05 §2 的 `CreativePreference`：偏好与意图是独立类型，不和 `WorldRule`/`StoryEvent` 混在一起。

### 1.5 剧本意图 vs 成片观测（分层存储）

docs/05 §3 原句："**剧本意图与成片观测分层存储：成片中道具错误不直接覆盖剧本正史，形成 continuity defect 与待决策冲突。**"

S04 侧的执行：

- S04 只生产"意图"层的原始材料（剧本正文）。
- S16 在提交时按 `source_kind` 区分 `script_intent` 与 `film_observed`（[S16 §2](S16-story-memory.md) 的 `claim.source_kind`）。
- 成片里道具拿错了，S17 报 `continuity defect`，S39 出修复计划；**S04 的剧本一个字都不改**。
- 只有经过"改剧本 → 新 revision → 新接受 → 新 manifest"这条完整链路，正史才会变。

S04 因此**不消费任何成片事件**（QC、artifact、repair）。它不消费 `snapshot.manifest.committed` 来判断"我的剧本被接受了没有" —— 那是 S08 的指针，S04 需要时去查。这一条很容易写错，写成"消费 accepted 事件后把 revision 标 accepted"，就是第二权威。

---

## 2. 数据模型

Postgres，schema `narrative`。所有表带 `tenant_id`，主键/唯一约束含 `tenant_id`（E28）。RLS 作第二道保护。正文分块存储（见 §2.3），**不在事件里带正文**（docs/04 §4：完整剧本用权限化 `data_ref`）。

### 2.1 创意简报

```sql
CREATE TABLE narrative.brief (
  tenant_id      text NOT NULL,
  brief_id       text NOT NULL,               -- 'brf_01J...'
  series_id      text NOT NULL,
  head_revision  integer NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, brief_id),
  UNIQUE (tenant_id, series_id),              -- 一剧一份 brief（容器）
  CONSTRAINT brief_head_chk CHECK (head_revision >= 0)
);

CREATE TABLE narrative.brief_revision (
  tenant_id      text NOT NULL,
  brief_id       text NOT NULL,
  revision       integer NOT NULL,
  body           jsonb NOT NULL,              -- {logline, genre, tone, audience, constraints[], references[]}
  content_hash   char(64) NOT NULL,
  author         jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, brief_id, revision),
  UNIQUE (tenant_id, brief_id, content_hash),
  FOREIGN KEY (tenant_id, brief_id) REFERENCES narrative.brief (tenant_id, brief_id)
);
```

`brief_revision.body` 的键集固定：`logline / genre / tone / audience / constraints[] / references[]`。`additionalProperties: false`。新增键要发版。

### 2.2 剧本与剧本版本

```sql
CREATE TABLE narrative.script (
  tenant_id        text NOT NULL,
  script_id        text NOT NULL,             -- 'scr_01J...'
  series_id        text NOT NULL,
  episode_id       text,                      -- 可为空：系列级总纲剧本
  script_kind      text NOT NULL,             -- episode_script|series_outline|scene_sheet
  language         text NOT NULL DEFAULT 'zh-CN',
  head_revision    integer NOT NULL DEFAULT 0,
  sealed_revision  integer,                   -- 最近一次 SEALED 的 revision
  retired_at       timestamptz,               -- 被新剧本替代（不物理删除）
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, script_id),
  CONSTRAINT script_kind_chk
    CHECK (script_kind IN ('episode_script','series_outline','scene_sheet')),
  CONSTRAINT script_head_chk
    CHECK (sealed_revision IS NULL OR sealed_revision <= head_revision)
);
CREATE INDEX script_by_episode
  ON narrative.script (tenant_id, series_id, episode_id, retired_at);
```

一个 `(series, episode)` 可以有**多个** `script_kind='episode_script'` 的 script（分支试写、翻拍、不同编剧的版本）。所以不能加 `UNIQUE(series_id, episode_id)`；`head_revision` 在同 script 内单调。

```sql
CREATE TABLE narrative.script_revision (
  tenant_id        text NOT NULL,
  script_id        text NOT NULL,
  revision         integer NOT NULL,
  state            text NOT NULL,             -- DRAFT|SEALED|SUPERSEDED|WITHDRAWN
  title            text,
  body_ref         text NOT NULL,             -- 正文分块的逻辑引用（'scr_01J/r7'）
  body_bytes       bigint NOT NULL,
  structure        jsonb NOT NULL,            -- {scene_markers:[...], characters:[...], line_count, word_count}
  line_set_hash    char(64) NOT NULL,         -- 本版对白行集合的 hash（见 §2.3）
  content_hash     char(64) NOT NULL,         -- sha256(JCS(header) || line_set_hash)
  author           jsonb NOT NULL,            -- {principal_id, principal_type:human|agent, definition_revision?}
  produced_by      jsonb,                     -- Agent 产出时的 {task_id, agent_task_ref, context_pack_ref}
  parent_revision  integer,                   -- 派生自哪一版（改写/续写）
  sealed_at        timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, script_id, revision),
  UNIQUE (tenant_id, script_id, content_hash),
  FOREIGN KEY (tenant_id, script_id) REFERENCES narrative.script (tenant_id, script_id),
  CONSTRAINT srev_state_chk
    CHECK (state IN ('DRAFT','SEALED','SUPERSEDED','WITHDRAWN')),
  CONSTRAINT srev_sealed_chk CHECK ((state <> 'DRAFT') OR sealed_at IS NULL)
);
CREATE INDEX script_revision_by_state
  ON narrative.script_revision (tenant_id, script_id, state, revision DESC);
```

**`structure` 里的 `scene_markers` 是文本标记，不是 `scene_id`。** 字段注释必须写清楚，否则实现者会拿它当场景列表。它只用于 UI 跳转与 S05 的参考（S05 可以看，但自己切场）。

**`content_hash` 的组成**：`sha256( JCS( {script_id, revision, title, language, structure, parent_revision} ) || "\n" || line_set_hash )`。不含 `author`、`created_at`（这些是元数据，改了不该让内容 hash 变）。不含 `state`（状态不是内容）。这个定义必须与 S08 的 `source_revisions[].content_hash` 一致，否则 S16 的 R1 会全部失败。

### 2.3 对白行与行版本（核心）

正文本体不进主库表，进对象存储/分块表；**对白行进结构化表**，因为 S06/S09/S16/S38 都要按行查询。

```sql
CREATE TABLE narrative.line (
  tenant_id      text NOT NULL,
  line_id        text NOT NULL,               -- 'ln_01J...'，全剧唯一，跨改稿不变
  script_id      text NOT NULL,
  episode_id     text,
  life_state     text NOT NULL DEFAULT 'active', -- active|retired
  head_revision  integer NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  retired_at     timestamptz,
  PRIMARY KEY (tenant_id, line_id),
  FOREIGN KEY (tenant_id, script_id) REFERENCES narrative.script (tenant_id, script_id),
  CONSTRAINT line_state_chk CHECK (life_state IN ('active','retired')),
  CONSTRAINT line_retired_chk CHECK ((life_state = 'retired') = (retired_at IS NOT NULL))
);
CREATE INDEX line_by_script ON narrative.line (tenant_id, script_id, life_state);

CREATE TABLE narrative.line_revision (
  tenant_id        text NOT NULL,
  line_id          text NOT NULL,
  revision         integer NOT NULL,
  text             text NOT NULL,             -- 原句，逐字节是权威
  text_hash        char(64) NOT NULL,         -- sha256(utf8(text))
  speaker_id       text,                      -- 角色 id（S11 的 chr_* 或占位名）
  speaker_label    text,                      -- 剧本里的称呼（'林岚'/'VO'）
  parenthetical    text,                      -- 舞台提示（'低声'/'画外'）
  locale           text NOT NULL DEFAULT 'zh-CN',
  scene_marker     text,                      -- 文本标记（'3'），**不是 scene_id**
  order_in_scene   integer,                   -- 文本顺序，**不是 shot order**
  author           jsonb NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, line_id, revision),
  UNIQUE (tenant_id, line_id, text_hash),
  FOREIGN KEY (tenant_id, line_id) REFERENCES narrative.line (tenant_id, line_id)
);
```

三条硬规则：

1. **`line_revision` 不可变。** 改字追加 revision。
2. **`text_hash` 唯一约束的作用**是"同一行同一文本不重复建版本"——重复提交相同文本命中原 revision，幂等。
3. **`speaker_id` 可为空。** 写剧本时角色可能还没建（S11 尚未准备），此时用 `speaker_label`。**S04 不铸造角色 ID**：`speaker_id` 是 S11 的 `chr_*` 引用，为空时由 S06/S10 的绑定解析补全。这是 C04-3。

```sql
-- seal 时的行集合快照（保证"这一版剧本包含哪些行、各是什么内容"）
CREATE TABLE narrative.script_revision_line (
  tenant_id       text NOT NULL,
  script_id       text NOT NULL,
  revision        integer NOT NULL,
  line_id         text NOT NULL,
  line_revision   integer NOT NULL,
  text_hash       char(64) NOT NULL,
  seq             integer NOT NULL,           -- 在本版剧本中的出现顺序
  PRIMARY KEY (tenant_id, script_id, revision, line_id),
  UNIQUE (tenant_id, script_id, revision, seq)
);
CREATE INDEX srl_by_line
  ON narrative.script_revision_line (tenant_id, line_id, script_id, revision);
```

`line_set_hash = sha256( join("\n", sorted([line_id, line_revision, text_hash] for each row)) )`。排序后 join，保证行顺序变化不影响 hash（顺序变化只体现在 `seq`，不改变"这版包含哪些内容"）。

`srl_by_line` 索引服务于 S38 的失效传播："这个 `line_id` 在哪些剧本版本里出现过、各是什么 hash"，一次索引扫即可，不需要全表 diff。

### 2.4 故事弧候选

```sql
CREATE TABLE narrative.story_arc_proposal (
  tenant_id        text NOT NULL,
  proposal_id      text NOT NULL,             -- 'arc_01J...'
  series_id        text NOT NULL,
  revision         integer NOT NULL DEFAULT 1,
  state            text NOT NULL,             -- PROPOSED|ADOPTED|REJECTED|SUPERSEDED
  body             jsonb NOT NULL,            -- {acts:[], turning_points:[], foreshadow_plan:[]}
  produced_by      jsonb NOT NULL,            -- {task_id, definition_revision, context_pack_ref, model_ref}
  source_revision  jsonb,                     -- 基于哪版 brief/script 生成
  confidence       numeric(4,3),              -- 0..1，Agent 自报
  unresolved       text[],                    -- Agent 自报的未决问题（docs/08 §2）
  reviewed_by      jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  decided_at       timestamptz,
  PRIMARY KEY (tenant_id, proposal_id, revision),
  CONSTRAINT arc_state_chk
    CHECK (state IN ('PROPOSED','ADOPTED','REJECTED','SUPERSEDED')),
  CONSTRAINT arc_confidence_chk CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);
CREATE INDEX arc_by_series
  ON narrative.story_arc_proposal (tenant_id, series_id, state);
```

`body.acts[]` 与 `body.foreshadow_plan[]` 的 schema 固定（本文 §5.1 给出）。`unresolved` 是 docs/08 §2 要求的"未决问题"字段 —— **Agent 输出必须显式声明它不知道什么**，否则下游会把整份 proposal 当完整方案用。

### 2.5 抽取任务引用（不缓存候选）

```sql
CREATE TABLE narrative.claim_extraction_run (
  tenant_id       text NOT NULL,
  extraction_id   text NOT NULL,              -- 'xr_01J...'
  script_id       text NOT NULL,
  script_revision integer NOT NULL,
  agent_task_ref  jsonb NOT NULL,             -- S19 的 task ref
  result_state    text NOT NULL,              -- PENDING|SUCCEEDED|FAILED|BUDGET_EXCEEDED
  candidate_count integer,
  handoff_ref     jsonb,                      -- S16 的 claim_candidate 批次引用（只存引用）
  created_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz,
  PRIMARY KEY (tenant_id, extraction_id),
  CONSTRAINT extraction_state_chk
    CHECK (result_state IN ('PENDING','SUCCEEDED','FAILED','BUDGET_EXCEEDED'))
);
```

**这张表只存"抽取跑过没有、结果去哪了"，不存候选内容。** 候选在 S16 的 `claim_candidate` 表里。S04 若存一份，就是第二候选池，S16 的审查界面会出现两套"待审候选"。

### 2.6 基础设施表

```sql
CREATE TABLE narrative.idempotency_record (
  tenant_id     text NOT NULL,
  command_id    text NOT NULL,
  request_hash  char(64) NOT NULL,
  operation     text NOT NULL,
  state         text NOT NULL,                -- IN_PROGRESS|DONE|ABANDONED
  response_code integer,
  response_body jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz,
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE narrative.outbox (
  tenant_id     text NOT NULL,
  id            text NOT NULL,
  aggregate_id  text NOT NULL,                  -- script_id
  aggregate_seq bigint NOT NULL,
  event_type    text NOT NULL,
  partition_key text NOT NULL,                -- tenant_id || ':' || aggregate_id
  payload       jsonb NOT NULL,
  headers       jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, aggregate_id, aggregate_seq)
);
CREATE INDEX narrative_outbox_unpublished
  ON narrative.outbox (created_at) WHERE published_at IS NULL;

CREATE TABLE narrative.inbox (
  consumer    text NOT NULL,
  event_id    text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);

CREATE TABLE narrative.body_chunk (
  tenant_id   text NOT NULL,
  script_id   text NOT NULL,
  revision    integer NOT NULL,
  chunk_no    integer NOT NULL,
  bytes_ref   text NOT NULL,                  -- 对象存储 key（S28 域外，属于 S04 的私有桶）
  sha256      char(64) NOT NULL,
  byte_size   bigint NOT NULL,
  PRIMARY KEY (tenant_id, script_id, revision, chunk_no)
);
```

正文分块放 S04 自己的私有对象存储（不是 S28：S28 是**制品**归档，剧本正文不是制品）。`bytes_ref` 必须带租户前缀且不可公开访问。docs/04 §4："完整剧本不放普通事件，使用权限化 `data_ref`"。

### 2.7 不建的表

- **不建 `scene` / `shot` 表。** 分场归 S05、分镜归 S06。
- **不建 `claim` 表。** 正史归 S16。
- **不建 `acceptance` 表。** 接受指针归 S08。
- **不建 `translation` 表。** 译文归 S38。
- **不建 `lexicon` 表。** 发音词典归 S15。
- **不建 `qc` / `defect` 表。** 质量归 S35–S39。

---

## 3. 状态机

### 3.1 ScriptRevision

```
              seal
  DRAFT ─────────────▶ SEALED ─────────▶ SUPERSEDED
    │                     │
    │                     └────────────▶ WITHDRAWN
    └──── withdraw ───────▶ WITHDRAWN
```

| 迁移 | 触发 | 守卫 |
|---|---|---|
| `DRAFT→SEALED` | `POST /scripts/{id}/revisions/{rev}:seal` | 正文完整、行集合已物化、`line_set_hash` 已算；`structure.line_count > 0` |
| `DRAFT→WITHDRAWN` | 作者放弃草稿 | 无 |
| `SEALED→SUPERSEDED` | 同 script 有新 revision seal | 新 revision 的 `revision >` 本版 |
| `SEALED→WITHDRAWN` | 作者撤回（发现严重错误） | **必须没有任何 manifest 引用它**（见下） |

**关键设计：`SEALED→WITHDRAWN` 的守卫不是"没人接受"，而是"没有 manifest 引用"。**

理由：S08 的 sanity 检查只认 `content_hash`。如果一份已 seal 的剧本被某个 manifest 引用过（哪怕那个 manifest 从未被接受），撤回会让"历史上存在过一个引用不存在对象的 manifest"。docs/04 §2 的要求是"不回滚已经提交的事实"。所以：

- 有 manifest 引用 → **不能撤回**，只能追加新 revision（让旧的自然 SUPERSEDED）。
- 无 manifest 引用 → 可撤回。

实现上 S04 需要一个"被引用"的检查。它**不直接查 S08 的库**（禁止跨库）。两种合规方式：

1. 消费 `snapshot.manifest.committed` 维护 `script_reference_projection`（`script_id, revision, manifest_id, manifest_revision`），撤回前查投影。
2. 调 S08 的 `GET /manifests?...source_revision=S04/script/rev`（需新增查询，见 C04-6）。

本文采用 (1) + 投影落后时**保守拒绝**（`stale=true` 时不允许撤回）。保守拒绝是安全的：撤不掉的代价是"多一版历史"，撤错的代价是"manifest 指向不存在的对象"。

```sql
CREATE TABLE narrative.script_reference_projection (
  tenant_id        text NOT NULL,
  script_id        text NOT NULL,
  script_revision  integer NOT NULL,
  manifest_id      text NOT NULL,
  manifest_revision integer NOT NULL,
  execution_fence  integer NOT NULL,
  applied_seq      bigint NOT NULL,           -- S08 事件序列水位
  PRIMARY KEY (tenant_id, script_id, script_revision, manifest_id, manifest_revision)
);
```

这张表**只增不删**（即使 manifest 变 VOIDED 也保留 —— 它证明"曾经引用过"）。它可重建，非权威。

### 3.2 Script（容器）

```
active ──▶ retired
```

`retired` 表示这部剧不再用这份剧本（换了新剧本）。`retired` 的 script 的所有 revision 仍可读，仍可被已提交的 manifest 引用。

**没有 `accepted` 状态。** 这一点必须写在代码注释里。Script 的"被接受"不是 script 的状态，是**某个 manifest revision 的 accepted pointer**（S08）。同一个 script revision 可能在分支 A 已接受、分支 B 未接受 — 状态机放不下这个事实。

### 3.3 StoryArcProposal

```
PROPOSED ──adopt──▶ ADOPTED ──supersede──▶ SUPERSEDED
    │
    ├──reject──▶ REJECTED
    └──supersede──▶ SUPERSEDED
```

`ADOPTED` 只影响 UI 默认展示与 S18 编剧 pack 的"作者意图"段落。**不产生正史。** 同一 series 可以同时有多个 `ADOPTED`（不同季、不同候选路线），不加强制唯一。

### 3.4 没有的状态：`accepted`、`published`、`in_production`

S04 的三个状态机都不含这三个值。原因分别是：

- `accepted`/`published`：归 S08。
- `in_production`：归 S21 的 Run 与 S03 的 `episode.lifecycle`。

---

## 4. 关键流程

### 4.1 写一版剧本（人工路径）

```jsonc
POST /scripts/{script_id}/revisions
{
  "command_id": "cmd_...",
  "expected_head_revision": 6,
  "state": "DRAFT",
  "title": "第 3 集",
  "body": "……完整剧本正文……",          // 或 body_chunks[]
  "structure": {"scene_markers":["1","2","3"], "characters":["林岚","陈默"]},
  "lines": [
    {"line_id": null, "text":"你终于来了。", "speaker_label":"陈默",
     "parenthetical":"低声", "scene_marker":"3", "order_in_scene":2}
  ],
  "parent_revision": 5
}
```

事务：

1. 幂等检查。
2. 从签名上下文取 tenant/principal。
3. CAS `expected_head_revision`。
4. 写 `script_revision(state=DRAFT)`。
5. 正文分块写 `body_chunk`（每块 ≤1MB，内容寻址）。
6. 对 `lines[]`：
   - `line_id=null` → 铸造新 `ln_`,`line.head_revision=1`，写 `line_revision(r=1)`。
   - `line_id` 存在且 `text_hash` 与最新 revision 相同 → 复用，不新建 revision。
   - `line_id` 存在且 text 变了 → **拒绝**（422 `LINE_MUTATION_IN_DRAFT`）。改字必须走 `POST /lines/{id}/revisions`（§4.6）。
7. 写 `script_revision_line` 快照，算 `line_set_hash`，回填 `script_revision.content_hash`。
8. 推进 `script.head_revision`。
9. 写 outbox。**注意：DRAFT 不发布 `narrative.script.revision.created`**（见 §5.3）。
10. 幂等记录置 DONE，提交。

第 6 步的拒绝理由是：DRAFT 里改字很容易写成"重新提交整份 lines 数组"，那样会让 `line_revision` 编号混乱。**行内容变更必须是一条独立命令**，有独立的 command_id 与审计。

### 4.2 Agent 生成一版剧本（S19 路径）

```
S19 writer AgentTask
  → 产出的不是 ScriptRevision，而是 "revision-proposal"
  → S04 POST /scripts/{id}/revisions {produced_by:{task_id,...}}
```

关键点：

1. **Agent 的输出 schema 由 S04 声明**（docs/08 §2：`expected_output`）。S19 负责校验结构，S04 负责校验**语义**（行 ID、hash、文本一致性）。
2. **Agent 不能提交 `line_id`。** 新建行由 S04 铸造；改写行必须先把 `line_id` 交给 S04，由 S04 决定是否允许。
3. **Agent 的产出永远是 DRAFT。** 不存在 `state=SEALED` 的 Agent 产出。seal 是一次人类（或有 `script.seal` 权限主体）的动作。
4. **`produced_by` 必填**（`task_id / definition_revision / context_pack_ref`），否则无法回答"这句话是谁写的、依据什么上下文"（E39 审计）。
5. **Agent 任务失败 → 不产生任何 revision。** docs/08 §2："超过上限转失败/审核，不通过循环'自我反思'无界增加账单"。S04 只看到 `FAILED`，不重试。

`POST /scripts/{id}/revisions` 因此有两条调用来源（人、Agent），但**同一条命令**。这样"人写的"和"Agent 写的"在数据模型上只有 `author.principal_type` 一个差异，不会出现两套 ID 空间。

### 4.3 seal

```jsonc
POST /scripts/{script_id}/revisions/{revision}:seal
{ "command_id":"cmd_...", "expected_head_revision": 7 }
```

事务：

1. CAS。
2. 校验该 revision 是 `DRAFT` 且是 head（不允许 seal 非 head，避免"seal 了一版又有人接着改"）。
3. 重新核算 `line_set_hash`（防提交后行被别的事务改过 —— 理论上不会，但不能假设）。
4. `state='SEALED'`, `sealed_at=now()`。
5. 同 script 的更早 `SEALED` revision → `SUPERSEDED`。
6. 写 outbox：`narrative.script.revision.created`（**只有 seal 才发事件**）。
7. 提交。

### 4.4 接受链路（S04 只是上游，不参与决策）

```
sealed revision
  → (可选) S19 memory-extractor 抽候选 → S16 claim_candidate（隔离区）
  → S05 分场（读此 revision）
  → S06 分镜（读此 revision 的 line_id）
  → S07/S09（若为改稿）/ S21 组装 manifest
  → S08 :commit（manifest.source_revisions 含 S04/script_id/revision/content_hash）
  → S02 判定 → S40 决定（若 manual）
  → S08 :accept
  → S08 发 snapshot.manifest.committed
  → S16 核验来源（R1：source_refs 必须出现在 manifest.source_revisions 且 hash 一致）
  → S16 原子提交 MemoryCommit
```

S04 在这条链路里的职责只有两条：

1. **`content_hash` 必须与 manifest 里写的完全一致。** 这是 S16 R1 的输入。任何对 `content_hash` 计算规则的改动（例如加了 `language` 字段）都必须版本化，否则已提交 manifest 的引用会失效。
2. **seal 后正文不可改。** 否则"manifest 引用了一个后来变了的对象"。

**S04 不消费 `snapshot.manifest.committed` 来判定剧本是否被接受。** 它只消费该事件维护 `script_reference_projection`（§3.1），用于撤回守卫。两个用途的代码必须在不同模块，否则很容易在重构时把投影读成"接受状态"。

### 4.5 局部改一句台词（E03 的变体）

```
PATCH line 原文 "我不去" → "我不想去"
```

```jsonc
POST /lines/{line_id}/revisions
{
  "command_id": "cmd_...",
  "expected_head_revision": 2,
  "text": "我不想去。",
  "reason": "导演要求语气软化"
}
```

事务：

1. CAS `line.head_revision`。
2. `text_hash` 与最新相同 → 幂等返回既有 revision，不新建。
3. 新 `line_revision(r+1)`；`line.head_revision=r+1`。
4. 写 outbox：`narrative.line.revision.created`（**新增，待登记**，见 C04-2）。
5. 提交。

**S04 到此为止。** 它不修改任何 `ScriptRevision`（已 seal 的不动），不改 S06 的 ShotSpec，不发"请重做"事件。

后续由谁驱动：

- 若这次改台词属于"对话局部修改"，S07 会生成 `ChangePlan`，其中 `patches[].target_ref` 指向 `S04 / line_id / revision`。
- S09 按 `AUDIO_FROM_DIALOGUE` 边找受影响的 TTS 与字幕。
- S06 出新的 ShotSpec revision（因为 `dialogue[].text` 变了）。
- S38 把对应译文标 `stale`（[S38 §2.3](S38-subtitles.md)）。

这条链路上 **S04 只做第一步**。它不发起、不编排、不判断影响范围 —— 否则就成了第二个 S09。

**DRAFT 期间改台词的例外**：如果剧本还是 DRAFT（未 seal），改台词就在同一次 `POST /revisions` 里带上新的 `lines[]`，因为那版正文还没被任何人引用。`LINE_MUTATION_IN_DRAFT` 的拒绝只针对"已有 revision 的 line 在新 DRAFT 里被直接改写"这种情况——正确做法是调 `POST /lines/{id}/revisions` 后再提交新 DRAFT。

### 4.6 失败矩阵

| # | 失败点 | 行为 | 重试 | 对外 |
|---|---|---|---|---|
| 1 | 幂等：同 command 同 hash | 原样返回 | 是 | 200 `replayed:true` |
| 2 | 幂等：同 command 异 hash | 拒绝 | 否 | 409 `IDEMPOTENCY_KEY_REUSED` |
| 3 | 幂等记录 `IN_PROGRESS` | 等 ≤2s | 是 | 409 `COMMAND_IN_FLIGHT` |
| 4 | `expected_head_revision` 不匹配 | 不写 | 否 | 409 `REVISION_CONFLICT` + `current_revision` |
| 5 | seal 时该 revision 不是 head | 不写 | 否 | 409 `NOT_HEAD_REVISION` |
| 6 | seal 时该 revision 不是 DRAFT | 不写 | 否 | 409 `ILLEGAL_STATE_TRANSITION` |
| 7 | `lines[]` 里引用不存在的 `line_id` | 不写 | 否 | 422 `LINE_NOT_FOUND` |
| 8 | 同 `line_id` 在同一次提交里出现两次 | 不写 | 否 | 422 `DUPLICATE_LINE_IN_REVISION` |
| 9 | DRAFT 里直接改写已有行文本 | 不写 | 否 | 422 `LINE_MUTATION_IN_DRAFT`，带 `required_endpoint` |
| 10 | `speaker_label` 与 `speaker_id` 同时为空 | 不写 | 否 | 422 `SPEAKER_REQUIRED` |
| 11 | 正文分块写对象存储失败 | 事务回滚（主库无行） | 是 | 500 |
| 12 | 分块已写、主库提交失败 | 孤儿分块；后台 GC 按 `body_chunk` 反查（无行则删） | 是 | 500 |
| 13 | 撤回时投影显示有 manifest 引用 | 拒绝 | 否 | 409 `REVISION_REFERENCED_BY_MANIFEST` |
| 14 | 撤回时投影 `stale=true` | 保守拒绝 | 否 | 409 `REFERENCE_CHECK_STALE` |
| 15 | Agent 任务 `FAILED` / 超预算 | 不产生 revision | 否 | 由 S19 侧返回；S04 无记录 |
| 16 | `produced_by.task_id` 不存在（伪造） | 不写 | 否 | 422 `UNKNOWN_AGENT_TASK`（回查 S19） |
| 17 | 剧本正文超过单 revision 上限（见 §11 未决 2） | 拒绝 | 否 | 413 `BODY_TOO_LARGE` |
| 18 | 读路径：正文分块读取失败 | 返回元数据 + `body_unavailable:true` | 是 | 200（**不返回空正文**） |

第 18 条尤其重要：正文读不出来时返回**空字符串**会让 S05 切出 0 场，然后一路"成功"地生成空集。必须显式标记不可用。

第 16 条：`produced_by` 是审计依据，必须回查 S19 的任务是否存在且属于本租户。S04 不能信任客户端自报的 `task_id`。

---

## 5. 对外契约

### 5.1 命令

| 方法 | 路径 | 用途 | 幂等 | CAS |
|---|---|---|---|---|
| POST | `/scripts/{script_id}/revisions` | 追加一版（DRAFT） | 必需 | `expected_head_revision` |
| POST | `/scripts/{script_id}/revisions/{revision}:seal` | 封存 | 必需 | `expected_head_revision` |
| POST | `/scripts/{script_id}/revisions/{revision}:withdraw` | 撤回（受 §3.1 守卫） | 必需 | 无 |
| POST | `/lines/{line_id}/revisions` | 改一句台词 | 必需 | `expected_head_revision` |
| POST | `/lines/{line_id}:retire` | 退役一行（不物理删） | 必需 | `expected_head_revision` |
| POST | `/scripts` | 建剧本文档容器 | 必需 | 无 |
| POST | `/briefs/{brief_id}/revisions` | 追加 pending 简报版本 | 必需 | `expected_head_revision` |
| POST | `/story-arcs` | 提交候选故事弧 | 必需 | 无 |
| POST | `/story-arcs/{id}/revisions/{rev}:decide` | adopt / reject / supersede | 必需 | `expected_revision` |
| POST | `/scripts/{script_id}/revisions/{revision}:extract-claims` | 发起抽取（S19 任务） | 必需 | 无 |

catalog 只声明了 `POST /scripts/{id}/revisions`。其余是本文根据 acceptance（"改稿追加版本"）与 docs/05 §3（抽取路径）推出的必要命令。登记为 C04-4。

`:extract-claims` 的特殊性：它是一个**付费任务发起命令**，本身不写正史，只写 `claim_extraction_run(PENDING)` 并把任务交给 S19。**预算准入在 S19/S26 侧**，S04 不碰 S23/S22。

### 5.2 查询

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/scripts/{script_id}/revisions/{revision}` | 单版元数据（**不含正文**） |
| GET | `/scripts/{script_id}/revisions/{revision}/body?chunk=0..n` | 正文分块（权限化，`data_ref`） |
| GET | `/scripts/{script_id}/revisions/{revision}/lines?after_line_id=&limit=` | 该版的行列表（分页） |
| GET | `/lines/{line_id}` | 行头 + 最新 revision |
| GET | `/lines/{line_id}/revisions/{revision}` | 单行单版 |
| GET | `/lines:batch?ids=` | 批量取行（S06/S38 用；上限 500） |
| GET | `/scripts?series_id=&episode_id=&state=` | 剧本列表 |
| GET | `/story-arcs/{id}?revision=` | 故事弧候选 |

`GET /scripts/{id}/revisions/{rev}` **默认不返回正文**。正文走单独的分块接口，原因是权限粒度不同：元数据可以给任何有项目读权限的人，正文可能含敏感内容（docs/14）。

`/lines:batch` 的上限 500 是刻意的：S06 写一集 ShotSpec 时会用到几十到几百行，一次取完；但上限必须存在，否则一个 bug 会把整部剧的行拉进一个请求。

### 5.3 事件

| 事件 | 何时发 | 状态 |
|---|---|---|
| `narrative.script.revision.created` | **seal 时**（不是 DRAFT 时） | 已登记（`drama.narrative.v2`） |
| `narrative.line.revision.created` | 单行内容变更 | **新增，待登记**（C04-2） |
| `narrative.script.withdrawn` | 撤回 | **新增，待登记** |

**为什么 DRAFT 不发事件**：DRAFT 是草稿，随时可废。如果 DRAFT 就发 `revision.created`，S05 会去分解一个还在改的剧本，S09 会建边，S16 的抽取会跑在一个不存在的版本上。**事件应表示"可被引用的版本出现了"**，那就是 seal。

**为什么必须单独发 line 级事件**：`narrative.script.revision.created` 只在 seal 时发，而改一句台词（§4.5）不产生新 seal。如果只有 script 级事件，S38 与 S09 只能靠轮询发现行变了。C04-2 记录这个缺口。

事件 `data` 结构（script 级）：

```jsonc
{
  "script_id": "scr_...", "revision": 7, "episode_id": "ep_...",
  "series_id": "ser_...",
  "content_hash": "…", "line_set_hash": "…",
  "line_count": 42, "scene_markers": ["1","2","3"],
  "structure_hash": "…",
  "author": {"principal_id":"…","principal_type":"human"},
  "produced_by": null,
  "parent_revision": 6
}
```

**不含正文，不含任何台词文本。** docs/04 §4：完整剧本不放普通事件。

`aggregate_id = script_id`，`aggregate_seq` 单调。

事件 `data`（line 级）：

```jsonc
{
  "line_id": "ln_...", "revision": 3, "script_id": "scr_...",
  "text_hash": "…", "prev_text_hash": "…",
  "speaker_id": "chr_...", "locale": "zh-CN",
  "life_state": "active"
}
```

**也不含 `text` 正文。** 下游需要文本时用 `GET /lines/{id}/revisions/{rev}`。这个选择有代价（S38 收到事件后要回源一次），但把每句台词发到 Kafka 会让 14 天保留期内任何有 topic 读权限的主体都能重建整份剧本。C04-7 记录这个取舍。

### 5.4 错误码

| HTTP | code | retryable | 说明 |
|---|---|---|---|
| 400 | `INVALID_BODY` | false | schema 不合法 |
| 403 | `TENANT_MISMATCH` | false | header 与签名上下文不符 |
| 404 | `SCRIPT_NOT_FOUND` / `LINE_NOT_FOUND` | false | **跨租户同码**（E28） |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同键异 body |
| 409 | `COMMAND_IN_FLIGHT` | true | 同键同 body 正在跑 |
| 409 | `REVISION_CONFLICT` | false | CAS 失败 |
| 409 | `NOT_HEAD_REVISION` | false | 只能 seal head |
| 409 | `ILLEGAL_STATE_TRANSITION` | false | 状态机不允许 |
| 409 | `REVISION_REFERENCED_BY_MANIFEST` | false | 撤回守卫 |
| 409 | `REFERENCE_CHECK_STALE` | true | 引用投影落后，稍后重试 |
| 413 | `BODY_TOO_LARGE` | false | 正文超限 |
| 422 | `LINE_MUTATION_IN_DRAFT` | false | 草稿里直接改写已有行 |
| 422 | `DUPLICATE_LINE_IN_REVISION` | false | 同次提交重复行 |
| 422 | `SPEAKER_REQUIRED` | false | 说话人必填其一 |
| 422 | `UNKNOWN_AGENT_TASK` | false | `produced_by` 不可核验 |
| 503 | `BODY_STORE_UNAVAILABLE` | true | 对象存储不可用 |
| 503 | `NARRATIVE_UNAVAILABLE` | true | 主库不可写 |

`REFERENCE_CHECK_STALE` 是 409 + `retryable:true`：它不是"请求错了"，是"守卫依据的投影暂时不可信"。调用方等待后重试是同一条请求。

---

## 6. 幂等与并发规则

### 6.1 幂等

- 键：`tenant_id + command_id + request_hash`。
- `request_hash = sha256(JCS(body 去 command_id / request_hash))`。
- 同键同 hash `DONE` → 原样返回 + `replayed:true`。
- 同键同 hash `IN_PROGRESS` → 等 ≤2s → 409。
- 同键异 hash → 409，永不覆盖。

**正文参与 `request_hash`**：正文是大字段，直接 JCS 会让 hash 体积爆炸。做法是先算 `sha256(正文)` 再放进 hash 输入。这一点必须写进 SDK，不能让每个服务各写一遍。

### 6.2 并发

| 冲突面 | 机制 |
|---|---|
| 同 script 两个并发 revision | `script.head_revision` CAS + `SELECT ... FOR UPDATE` on `script` |
| 同 line 两个并发改字 | `line.head_revision` CAS + 行锁 |
| seal 与改字并发 | 都锁 `script` 行（改字虽锁 line，但 seal 会锁 script 并校验 head 一致）；先拿 script 锁的一方胜 |
| 撤回与 commit 并发 | 撤回先查投影再锁；S08 的 commit 不读 S04，所以不存在跨服务锁。**窗口内可能撤回一份正被 commit 的剧本** → 由 S08 的 `source_revisions` 校验兜底（commit 时回源 S04 拿 hash，见 C04-8） |
| 抽取与 seal 并发 | 抽取锁定 `script_revision.revision`；seal 后 revision 不变，抽出来的候选引用的 hash 仍有效 |

**锁顺序**（写进代码）：

```
script (FOR UPDATE) → line (FOR UPDATE) → script_revision → line_revision → body_chunk → outbox
```

### 6.3 隔离级别

`READ COMMITTED` + 行锁。理由同 S03 §6.4。

### 6.4 与 S08 的 `source_revisions` 竞态（必须记录）

S08 的 `:commit` 接受调用方传入的 `candidate_manifest`，其中 `source_revisions[]` 含 `{service_id:"S04", id, revision, content_hash}`。S08 需要校验这个 hash 是否真的对应 S04 的那一版。

合规做法是 S08 在 commit 时**回源** S04 的 `GET /scripts/{id}/revisions/{rev}` 比对 hash。如果 S04 没提供该查询，S08 只能信任调用方传入的值 —— 那就是把"内容版本"的权威外包给了调用方。

本文要求 S04 提供该查询（§5.2 已有），并在 C04-8 记录"S08 必须回源校验，不能只做 schema 校验"。

---

## 7. 可观测

### 7.1 指标

| 指标 | 类型 | 用途 |
|---|---|---|
| `narrative_revision_total{state}` | counter | 生产量；DRAFT 远多于 SEALED 是正常的 |
| `narrative_seal_total{result}` | counter | 封存成功/失败 |
| `narrative_seal_latency` | histogram | 封存耗时（含行集合核算） |
| `narrative_line_revision_total` | counter | 台词改动频次；异常高说明剧本不稳 |
| `narrative_withdraw_blocked_total{reason}` | counter | 被引用阻止的撤回；>0 说明有人在撤回已投产的剧本 |
| `narrative_agent_revision_ratio` | gauge | Agent 产出占比（按 `author.principal_type`） |
| `narrative_extraction_total{result}` | counter | 抽取成功率/超预算率 |
| `narrative_outbox_lag_seconds` | gauge | 最老未发布事件年龄；>60s 告警 |
| `narrative_reference_projection_lag_seconds` | gauge | S08 事件延迟；影响撤回守卫 |
| `narrative_body_store_error_total` | counter | 对象存储错误 |

### 7.2 日志与追踪

- 日志带 `trace_id / tenant / script_id / revision / line_id / command_id`。
- **绝不记录台词正文、剧本正文。** 只记 `text_hash` 与行数。这是 docs/14 的硬要求，且在 §5.3 的事件设计上已经对齐。
- `produced_by.task_id` 记日志，便于回答"这句话是哪个 Agent 在哪个 ContextPack 下写的"。
- 错误日志可记 `line_id`，不可记 `text`。

### 7.3 审计

- 每次 seal、withdraw、line revision 都有不可变事件。
- `script_revision_line` 是"这一版包含哪些行、各是什么 hash"的永久证据。
- `claim_extraction_run` 记录每次抽取的 AgentTask 引用与结果状态，可追溯"这条正史是从哪次抽取来的"（配合 S16 的 `commit_manifest_binding`）。

---

## 8. 测试清单

### 8.1 E02 · 三集连续生产

| 序号 | 场景 | 断言 |
|---|---|---|
| T04-01 | 三集各写一版、seal | 三个 `script_id` 独立；`content_hash` 互不相同；事件各一条 |
| T04-02 | 第 2 集的 `line_id` 跨改稿保持 | 改稿后同一句的 `line_id` 不变、`line_revision` +1 |
| T04-03 | 从三版剧本生成 manifest | `source_revisions` 含三条 S04 引用，hash 与 `GET /revisions/{rev}` 一致 |

### 8.2 E03/E04 · 改镜与对白时长

| 序号 | 场景 | 断言 |
|---|---|---|
| T04-04 | 改一句台词（`POST /lines/{id}/revisions`） | 新 `line_revision`；已 seal 的 `ScriptRevision` 内容 hash **不变** |
| T04-05 | 改台词后 S06 的 ShotSpec | S06 必须出新 revision；S04 **不发**"重做"事件（不是它的职责） |
| T04-06 | 对白文本超过镜头时长 | S04 不做任何判断；由 S06/S25 返回不可行（E04 的判定点在别处） |
| T04-07 | 剧本缺集上下文 | 查询不存在的 `episode_id` → 404，不返回近似集 |

### 8.3 E08/E10 · 改稿与正史

| 序号 | 场景 | 断言 |
|---|---|---|
| T04-08 | 改前集剧本（第 2 集新 revision） | 第 2 集旧 revision 仍可读；S16 的 claim 引用旧 revision 时仍是有效来源 |
| T04-09 | 新 revision 上抽取候选 | 候选进 S16 `claim_candidate`；S04 只存 `handoff_ref`，不存候选内容 |
| T04-10 | 未 seal 的 DRAFT 被抽取 | 拒绝（只能从 SEALED 抽取，见 §5.1 的 `:extract-claims` 前置） |
| T04-11 | 撤回已 seal 但未被引用的 revision | 成功，状态 `WITHDRAWN` |
| T04-12 | 撤回已被 manifest 引用的 revision | 409 `REVISION_REFERENCED_BY_MANIFEST`；旧 revision 保持 SEALED |

### 8.4 E09 · 闪回与知情

| 序号 | 场景 | 断言 |
|---|---|---|
| T04-13 | 闪回集的剧本 | S04 不记录任何故事时间；`structure` 里只有文本标记 |
| T04-14 | 同一段对话在第 7 集（闪回）与第 2 集重复出现 | 两个独立 `line_id`（不同 script）；S04 不做去重合并（合并会让 S16 的证据来源错乱） |

### 8.5 E12 · 幂等

| 序号 | 场景 | 断言 |
|---|---|---|
| T04-15 | 同 command 同 body 两次 seal | 第二次 `replayed:true`，`sealed_at` 不变，事件不重复发 |
| T04-16 | 同 command 异 body | 409 `IDEMPOTENCY_KEY_REUSED` |
| T04-17 | 并发两次 seal | 一个成功，另一个 409 `NOT_HEAD_REVISION` 或 `ILLEGAL_STATE_TRANSITION` |
| T04-18 | 同文本重复改字 | 命中原 `line_revision`，不建新版本（`text_hash` 唯一约束） |
| T04-19 | 同一 DRAFT 提交两次（同 command） | 只产生一版 |

### 8.6 E13 · 崩溃恢复

| 序号 | 场景 | 断言 |
|---|---|---|
| T04-20 | 分块写完、主库提交前崩溃 | 无 `script_revision` 行；孤儿分块被 GC |
| T04-21 | 主库提交后、响应前崩溃 | 重发命中幂等，返回原 revision |
| T04-22 | outbox 写后发布前崩溃 | 中继续发；下游按 `event_id` 去重 |
| T04-23 | seal 事务中途崩溃 | 状态仍是 DRAFT 或已是 SEALED，不存在"半个 SEALED"（单事务） |

### 8.7 E28 · 跨租户

| 序号 | 场景 | 断言 |
|---|---|---|
| T04-24 | 租户 A 读租户 B 的 script | 404，与"不存在"同码 |
| T04-25 | 租户 A 用租户 B 的 `line_id` 改字 | 404，无写入 |
| T04-26 | body 里伪造 `tenant_id` | 403 `TENANT_MISMATCH` |
| T04-27 | 正文分块 URL 跨租户 | 拒绝（签名 URL 绑定 tenant） |

### 8.8 E27/E39 · Agent 与审计

| 序号 | 场景 | 断言 |
|---|---|---|
| T04-28 | Agent 产出 DRAFT，`produced_by` 完整 | 落库；`author.principal_type='agent'` |
| T04-29 | Agent 产出伪造 `task_id` | 422 `UNKNOWN_AGENT_TASK`（回查 S19） |
| T04-30 | Agent 直接提交 `state=SEALED` | 拒绝（Agent 只能产 DRAFT） |
| T04-31 | 审计：任意 revision 能追溯到作者/Actor/上下文 | `author` + `produced_by` 齐全；日志无正文 |

### 8.9 E04/E32 · 乱序与重放

| 序号 | 场景 | 断言 |
|---|---|---|
| T04-32 | `snapshot.manifest.committed` 乱序到达 | `script_reference_projection` 不回退（`applied_seq` 单调） |
| T04-33 | 回放 narrative topic（`side_effects=false`） | 不产生新 outbox、不重复抽取 |
| T04-34 | 回放期间有 DRAFT 写入 | 回放不影响写入（独立 consumer group） |

### 8.10 对白一致性（跨服务，见 C04-1）

| 序号 | 场景 | 断言 |
|---|---|---|
| T04-35 | S06 用 S04 的 `line_id` 写 ShotSpec | `dialogue[].text` 与 `GET /lines/{id}/revisions/{rev}` 字节相等 |
| T04-36 | S06 写入与 S04 不一致的文本 | **S06 侧拒绝**（本文要求的协议）；若放行，则 S17 报 `TEXT.SCRIPT_DIVERGENCE`（需新增 check） |
| T04-37 | S04 改字后 S06 未出新 revision | 已提交 manifest 仍引用旧 hash；S09 的 `AUDIO_FROM_DIALOGUE` 边应指到新 revision（跨服务验收） |

---

## 9. 反模式清单（写了就是错）

1. ❌ 在 `script` 或 `script_revision` 上加 `accepted` 布尔 → 第二接受权威，S16 的写入触发源变成两个。
2. ❌ R1 之外的"软接受"：`state='APPROVED'`、`approved_by` 字段 → 同上，且会让 S02 的门形同虚设。
3. ❌ DRAFT 就发 `revision.created` → S05 分解一个还在改的剧本。
4. ❌ 允许原地编辑 SEALED revision → 已提交 manifest 的 `content_hash` 失去意义。
5. ❌ 改稿时新建 `line_id` 当作"改了一句" → S16 的历史证据断链，S09 的边断。
6. ❌ 物理删除 `line` → S16 claim 的 `source_refs` 指向不存在的对象。
7. ❌ S04 铸造 `scene_id` → 第二序号/结构权威；S05 的 ID 空间会被污染。
8. ❌ 把 `structure.scene_markers` 当场景列表暴露给下游 → 下游会当 `scene_id` 用。
9. ❌ 把候选 claim 缓存一份到 S04 → 两个待审池，审计无法对齐。
10. ❌ 消费 `snapshot.manifest.committed` 后标"我的剧本被接受了" → 第二权威。
11. ❌ 在事件里带台词正文 → Kafka 保留期内任何 topic 读者可重建剧本（docs/14 违规）。
12. ❌ `content_hash` 里含 `author`/`created_at`/`state` → 元数据变更让内容 hash 变，S16 R1 全部失败。
13. ❌ 撤回时不检查 manifest 引用 → manifest 指向不存在的对象。
14. ❌ 信任客户端自报的 `produced_by.task_id` → 审计可伪造（E39）。
15. ❌ Agent 能 seal → 无人审的剧本进生产。
16. ❌ 正文读失败时返回空字符串 → 下游"成功"生成空集。

---

## 10. 冲突（报告，不选边）

| # | 冲突 / 缺口 | 现状 | 本文用法 | 需要谁裁决 |
|---|---|---|---|---|
| C04-1 | `shot-spec.v1.dialogue[]` 内嵌 `text`，S04 也持有同一句的 `text`；契约里没有 `script_line_ref` 表达"这个字段派生自 S04 的哪一行哪一版"。docs/08 只说"编译器不会改写对白事实"，没说文字权威如何在两个服务间保持一致 | shot-spec.v1 vs docs/08 | S04 是文字权威；S06 的 `dialogue[].text` 为派生投影，写时必须字节相等校验并记录 `script_line_ref`（S06 侧自存，契约待补）。新增一致性测试 T04-35/36/37 | `contracts/domain-v1/shot-spec.v1.schema.json` + S06 方案 |
| C04-2 | `event-topics.v2.json` 只有 `narrative.script.revision.created`，没有行级变更事件；但 S38（译文 stale）、S09（`AUDIO_FROM_DIALOGUE` 边）、S06（对白来源）都需要感知单行变更 | topics | 新增 `narrative.line.revision.created`（§5.3）。在此之前 S38/S09 只能靠 script 级事件 + 全量 diff | 契约维护方 |
| C04-3 | `line.speaker_id` 的 ID 空间属于 S11（`chr_*`），但写剧本时角色常常尚未铸造；契约未定义"未绑定的说话人"如何表达 | 无契约 | `speaker_id` 可空 + `speaker_label` 文本；绑定解析在 S06/S10（§2.3） | S11/S10 方案作者 |
| C04-4 | catalog 只声明 `POST /scripts/{id}/revisions`；seal、withdraw、line revision、抽取发起、story-arc 全部无契约 | catalog | 补全 §5.1 的 10 条命令 | `contracts/service-catalog.v2.json` + `openapi.v2.yaml` |
| C04-5 | [S09 §3](S09-lineage-impact.md) 的字段依赖注册表里登记了 `('S04','/scene_count','number')`，但场次总数是 S05 的事实；S04 只知道 `structure.scene_markers.length`（文本标记数），两者不一定相等 | S09 registry vs 所有权 | S04 不提供 `/scene_count` 作为业务事实；若 S09 需要该字段，应改为 S05 的 `scene_count` | S09 方案 + 字段注册表维护方 |
| C04-6 | 没有"按 source_revision 反查 manifest"的查询（S04 撤回守卫需要）。`episode-manifest.v2` 没有反向索引契约 | contracts | 用 S08 事件投影（§3.1）+ 落后时保守拒绝；若 S08 愿提供反向查询则更优 | S08 方案作者 |
| C04-7 | 台词正文是否进事件有取舍：进事件则下游免回源，但 Kafka 保留期内可重建剧本；不进则每次变更下游要回源一次 | docs/04 §4 vs 性能 | 不进事件（§5.3），用 `text_hash` + 回源 | 安全/合规负责人确认 |
| C04-8 | S08 `:commit` 校验 `source_revisions[].content_hash` 时是否需要回源 S04；若只做 schema 校验，等于把内容版本权威外包给调用方 | docs/04 §2 vs openapi | 要求 S08 回源 `GET /scripts/{id}/revisions/{rev}` 比对 hash；S04 已提供该查询 | S08 方案作者 |
| C04-9 | `ScriptRevision` 与 `S05` 的 `SceneBreakdownRevision` 之间缺少"基于哪一版剧本"的强绑定契约。S05 的 acceptance 说"场次关联到固定剧本版本"，但 `service.yaml` 的命令体没有该字段 | S05 acceptance vs catalog | 要求 S05 的命令体带 `source_script_ref{script_id,revision,content_hash}`；S04 提供查询 | S05 方案作者 |
| C04-10 | `depends_on` 是 `S03,S18,S19`，但实际还需要 S08（撤回守卫的投影）、S16（抽取交接）、S02（seal 权限判定） | catalog | 依赖表是运行协作关系，事件消费与只读查询不构成启动依赖 | catalog 维护方 |

**关于"镜头 ID 归属"**：与本服务无关，但为完整性记录 —— 仓库中 S03 的 `data_owner` 写的是"场次ID由S05负责"（Scene），S06 拥有"Shot稳定ID"。逐字核对见 [S03 §10.1](S03-project-catalog.md)。S04 不涉及任何 Scene/Shot ID 的铸造。

---

## 11. 实施顺序与未决问题

### 11.1 顺序（全部生产开关默认关闭）

1. **契约先行**：`script-revision.v1.schema.json`（元数据 + `structure`）、`line-revision.v1.schema.json`、`story-arc-proposal.v1.schema.json`；§5.1/§5.2 的 OpenAPI 路径。改 catalog 后**重新生成** `service.yaml`。
2. **`platform/content-address-sdk`**：`content_hash` / `line_set_hash` / `request_hash`（含大字段先 hash）三件套。**必须与 S05/S06/S08 共用同一实现**，否则同一份剧本在四个服务算出四个 hash。
3. **最小闭环（无 Agent）**：`POST /scripts` + `POST /scripts/{id}/revisions` + `:seal` + `GET .../revisions/{rev}` + `GET .../body`。用 mock S03。验 E12 / E28 / T04-01..03。
4. **行模型**：`line` + `line_revision` + `script_revision_line` + `/lines:batch`。验 T04-02 / T04-18 / T04-35。
5. **撤回守卫**：消费 `snapshot.manifest.committed` 维护 `script_reference_projection`，实现 §3.1 守卫。验 T04-11/12/32。
6. **接 S19**：`produced_by` 回查、Agent DRAFT 路径。验 T04-28..30。
7. **抽取发起**：`:extract-claims` → S19 任务 → S16 候选。**此时只验"任务被正确发起、结果进了 S16"**，不验正史内容。
8. **StoryArc**：proposal 的提交与决定。**最后做**，因为它不影响生产正确性。
9. 接真实 S03/S08/S16；打开 RLS；补齐指标。

### 11.2 未决问题（需要实施会话或设计方回答）

1. **正文的存储介质**：本文用 S04 私有对象桶 + 分块。是否应该统一到 S28？S28 是"制品字节"的权威，而剧本正文是**源内容**，不是制品。混在一起会让"删除制品"与"删除剧本"的保留策略打架。需要 S28 方案明确边界。
2. **单 revision 正文上限**：本文假设 ≤5MB（约 20 万字）。短剧一集 1–3 分钟通常远小于此，但"系列总纲"可能很长。上限需要一个数字，且要与 S05 的分解超时一起定。
3. **同一集能否有多份并行 `episode_script`**：本文允许（分支试写、不同编剧）。但 S03 的 `episode` 与 script 是 1:N，UI 需要"哪份是主版本"的概念。是否需要一个 `primary_script_id`？本文刻意不设（会变成第二接受权威），改用 UI 侧记忆。
4. **`scene_marker` 的格式**：本文是自由文本（`"3"`、`"3A"`、`"闪回-1"`）。S05 切场时如何对齐？需要一个规范化规则（例如只取前导数字），否则 S05 与 S04 的"第 3 场"会错位。这属于 docs/07 §2"明确集上下文"的一部分，目前无定义。
5. **对白的 `locale`**：本文一行为一个 `locale`。若同一句需要多语言原文（例如剧中人讲日语），需要一行多 locale 还是多行？本文按多行（不同 `line_id`），因为字幕/配音的失效传播是按行的。
6. **`parenthetical`（舞台提示）是否算对白事实**：改"（低声）"为"（嘶吼）"要不要走 line revision？本文要求走（因为它影响 TTS 与 S06 的 `emotion`），但 docs/08 只说"不会改写对白事实"，没有定义范围。
7. **抽取的时机与成本**：docs/05 §3 允许"抽取可以先跑，提交必须等接受"。但每 seal 一版就跑一次抽取，成本按集算会很高。是否应该只在"可能被接受"时才跑（例如进入 review 门）？本文按"显式命令发起"，由 UI/S21 决定何时调，不自动触发。
8. **`StoryArcProposal` 与 S16 的 `PlotThread` 的关系**：前者是意图（"打算在第 2 集埋伏笔"），后者是事实（"第 2 集确实埋了"）。目前没有服务连接这两者。若 S17 要检查"伏笔是否按计划埋了"，需要一个 `intent_ref`。本文不实现。
