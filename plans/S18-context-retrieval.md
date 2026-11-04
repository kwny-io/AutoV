# S18 · 上下文检索与组装 — 实现方案

状态：`design_only`。SLO 未验证。本文是实现方案，不是已实现的服务。

上游约束：[docs/05 §4](../docs/05-cross-episode-memory.md)、[docs/08 §2](../docs/08-agents-prompts.md)、[docs/04 §5](../docs/04-data-consistency.md)、[S18 规格](../docs/design-package/services/S18-context-retrieval.md)、[S16 实现方案](./S16-story-memory.md)、[context-pack.v2](../contracts/context-pack.v2.schema.json)。

---

## 0. 一句话边界

**S18 不拥有任何事实。** 它的输出 `ContextPack` 是一组**引用**（claim_ref / asset_ref）+ 一组**禁则**（`prohibited_assumptions`）+ 一个**水位**（`source_watermark`）+ 一个 `content_hash`。它连事实内容都不返回 —— 只返回指针和组装后的可读摘要。

这是刻意的：**如果 S18 返回事实正文，调用方就会开始相信副本；只要它返回引用，调用方就必须回 S16 才能拿到真相。**

它要回答的问题不是"哪些记忆相关"，而是：**在这个 branch、这个 checkpoint、这一集的这个视角下，哪些事实允许进入这个角色/这次生成的上下文，哪些必须被显式禁止假设。**

---

## 1. 数据模型

数据库：独立 PostgreSQL 集群，schema `context_retrieval`。所有表以 `tenant_id` 起键。

> 硬规则：**本 schema 里没有任何表叫 `claim` / `fact` / `state` / `holder`。** 有的只有 pack、引用、索引（可重建）、水位、禁则。

### 1.1 上下文包

```sql
CREATE TABLE context_retrieval.context_pack (
  tenant_id            text NOT NULL,
  context_pack_id      text NOT NULL,
  revision             integer NOT NULL,
  series_id            text NOT NULL,
  branch_id            text NOT NULL,
  task_role            text NOT NULL,          -- writer|storyboard|prompt|qc|continuity|...
  target_episode_id    text NOT NULL,
  target_episode_order integer NOT NULL,
  perspective          text NOT NULL,          -- omniscient|character|audience
  perspective_character_ids text[] NOT NULL DEFAULT '{}',
  -- 固定水位（package 有效性来源）
  checkpoint_sequence  bigint NOT NULL,
  checkpoint_hash      bytea NOT NULL,
  source_watermark     bigint NOT NULL,        -- 实际能达到的 S16 head
  manifest_ref         jsonb NOT NULL,
  policy_ref           jsonb,
  -- 组装结果
  claim_refs           jsonb NOT NULL,          -- [{service_id,id,revision,content_hash}]
  asset_refs           jsonb NOT NULL,
  plot_thread_refs     jsonb NOT NULL,
  episode_blocks       jsonb NOT NULL,          -- 摘要块（见 §4.6），只含引用不重复事实
  prohibited_assumptions jsonb NOT NULL,        -- [{id, text, basis, claim_ref, severity}]
  unresolved_conflicts jsonb NOT NULL,
  -- 预算
  token_budget         integer NOT NULL,
  tokens_used          integer NOT NULL,
  budget_breakdown     jsonb NOT NULL,          -- 按 section 的分账
  -- 完整性
  forbidden_leak_scan  jsonb NOT NULL,          -- 泄漏自检结果
  content_hash         bytea NOT NULL,
  stale                boolean NOT NULL DEFAULT false,
  supersedes_pack_id   text,
  created_at           timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, context_pack_id, revision)
);
CREATE UNIQUE INDEX pack_idem ON context_retrieval.context_pack (tenant_id, task_role, target_episode_id, checkpoint_sequence, (content_hash));
```

注意 `content_hash` 与 `claim_refs` 的关系：`content_hash = sha256(canonical_json({tenant, series, branch, episode_order, perspective, checkpoint, claim_refs, asset_refs, prohibited_assumptions}))`。**同样的输入永远得到同样的 hash。** 这让"S18 的组装是否可复现"成为一个可测试的性质，而不是口头承诺。

### 1.2 禁则（一等公民）

```sql
CREATE TABLE context_retrieval.prohibited_assumption (
  tenant_id          text NOT NULL,
  context_pack_id    text NOT NULL,
  revision           integer NOT NULL,
  assumption_id      text NOT NULL,
  text               text NOT NULL,           -- 面向模型的自然语言禁则
  rule_kind          text NOT NULL,           -- KNOWLEDGE|STATE|RELATION|THREAD|CANDIDATE|BRANCH|SCENE
  severity           text NOT NULL,           -- hard|soft
  basis              jsonb NOT NULL,          -- [{claim_id, revision, kind, why}] 或 {reason_code}
  claim_refs         jsonb NOT NULL,
  entity_ids         text[] NOT NULL DEFAULT '{}',
  generated_by       text NOT NULL,           -- template_id@version
  PRIMARY KEY (tenant_id, context_pack_id, revision, assumption_id)
);
```

**禁则不是备注。** 每条禁则有：
- `rule_kind`：它禁的是哪一类错误（不知道某事 / 状态不符 / 关系错判 / 伏笔未到）；
- `severity`：`hard` = 生成/校验必须阻断；`soft` = 应避免，命中记为缺陷但不阻断；
- `basis`：**从哪条 claim 推导出来的**。没有 basis 的禁则不允许写入；
- `generated_by`：模板版本，模板改动可追溯；
- `entity_ids`：可用于 S17 交叉核对覆盖率。

### 1.3 索引（可重建，非事实源）

```sql
CREATE TABLE context_retrieval.claim_index (
  tenant_id       text NOT NULL,
  series_id       text NOT NULL,
  branch_id       text NOT NULL,
  claim_id        text NOT NULL,
  revision        integer NOT NULL,
  commit_sequence bigint NOT NULL,
  kind            text NOT NULL,
  subject_id      text NOT NULL,
  predicate       text NOT NULL,
  object_text     text,                        -- 从 fact 展开，仅用于检索
  entity_ids      text[] NOT NULL,
  clock_id        text NOT NULL,
  valid_from_tick bigint NOT NULL,
  valid_to_tick   bigint,
  audience_reveal_episode_order integer NOT NULL,
  epistemic_knows text[] NOT NULL DEFAULT '{}',  -- 谁 knows/believes
  epistemic_unaware text[] NOT NULL DEFAULT '{}',
  text_embedding  vector(1024),                 -- pgvector，可空
  text_lexical    tsvector,
  content_hash    bytea NOT NULL,
  indexed_at_commit bigint NOT NULL,            -- 索引到哪个 S16 提交
  PRIMARY KEY (tenant_id, series_id, branch_id, claim_id)
);
CREATE INDEX idx_semantic ON context_retrieval.claim_index
  USING ivfflat (text_embedding vector_cosine_ops) WITH (lists = 200);
CREATE INDEX idx_lexical ON context_retrieval.claim_index USING gin (text_lexical);
CREATE INDEX idx_entity ON context_retrieval.claim_index USING gin (entity_ids);
CREATE INDEX idx_interval ON context_retrieval.claim_index
  USING gist (tenant_id, series_id, branch_id, clock_id,
              int8range(valid_from_tick, valid_to_tick, '[)'));
```

关键：`claim_index` 带 `indexed_at_commit`。**每条索引行知道自己落后多少。** 这是 §5 watermark 逻辑的物理基础。

`materialized_branch_view` 表（同样可重建）缓存 `canon_effective_claim` 的本地副本，用于加速，**但任何正确性决策都不能只信它** —— 见 §5。

### 1.4 检索模板与预算

```sql
CREATE TABLE context_retrieval.role_profile (
  tenant_id       text NOT NULL,
  task_role       text NOT NULL,
  profile_version integer NOT NULL,
  weights         jsonb NOT NULL,   -- {relationship:0.9, plot_thread:0.8, entity_state:0.5, ...}
  required_kinds  text[] NOT NULL,  -- writer: knowledge_state, relationship, plot_thread
  section_budget  jsonb NOT NULL,   -- {identity:0.15, relationships:0.25, continuity:0.25, threads:0.2, summary:0.15}
  include_world_rules boolean NOT NULL DEFAULT true,
  PRIMARY KEY (tenant_id, task_role, profile_version)
);
```

角色 profile 对应 docs/05 §4 的四类包：编剧偏人物关系与伏笔、分镜偏动作道具空间、Prompt 偏身份与可见状态、QC 包含预期状态与允许例外。

### 1.5 与 context-pack.v2 契约的对齐

契约的 `prohibited_assumptions` 是 `array<string>`，本实现内部是结构化的 `prohibited_assumption` 表。序列化规则：

| 契约字段 | 内部来源 | 序列化 |
|---|---|---|
| `prohibited_assumptions` | `prohibited_assumption.text` | 只输出 `text`，按 `severity='hard'` 在前、`assumption_id` 升序排列 |
| `unresolved_conflicts` | `unresolved_conflict.conflict_id` | 输出 id 字符串 |
| `checkpoint` | `{commit_sequence, content_hash}` | 契约只有这两个字段，`branch_id` 在 pack 顶层 |
| `perspective` | `perspective` 列 | `character` 时具体角色集在内部表，契约不表达（调用方用 pack_id 回查） |
| `source_watermark` | `source_watermark` | 契约是整数，即实际 checkpoint 序列 |
| `content_hash` | 计算值 | 见 §1.1 |

结构化字段（`rule_kind` / `severity` / `basis`）只在 `GET /context-packs/{id}` 的扩展视图中返回，契约对象本身保持严格 `additionalProperties: false`。**扩展输出不得被当作契约的一部分传给下游 Agent**，否则 S19 会开始依赖未版本化字段。

### 1.6 基础设施表

```sql
-- 与 S08 §6.4 同构，列名一字不差（不同 schema）
CREATE TABLE context_retrieval.idempotency_record (
  tenant_id text NOT NULL, command_id text NOT NULL, request_hash char(64) NOT NULL,
  command_type text NOT NULL, state text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code integer, response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE context_retrieval.outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id text NOT NULL, aggregate_id text NOT NULL, aggregate_sequence bigint NOT NULL,
  event_type text NOT NULL, partition_key text NOT NULL, payload jsonb NOT NULL,
  headers jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON context_retrieval.outbox (id) WHERE published_at IS NULL;

CREATE TABLE context_retrieval.inbox (
  consumer text NOT NULL, event_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);

context_retrieval.watermark (
  tenant_id, series_id, branch_id,
  applied_commit_sequence bigint NOT NULL,
  applied_checkpoint_hash bytea,
  index_lag          bigint NOT NULL,     -- = head - applied（缓存，来自 S16 事件）
  updated_at         timestamptz,
  PRIMARY KEY (tenant_id, series_id, branch_id)
);
```

---

## 2. 输入

`POST /context-packs`

```json
{
  "command_id": "cmd_ctx_ep3_writer",
  "tenant_id": "derived",
  "series_id": "series_demo",
  "branch_id": "canon",
  "episode_id": "ep003",
  "episode_order": 3,
  "task_role": "writer",
  "perspective": {"mode": "character", "character_ids": ["char_linlan"]},
  "story_clock_id": "story_main",
  "story_tick": 2100,
  "required_checkpoint": {"commit_sequence": 27, "content_hash": "..."},
  "token_budget": 4000,
  "focus": {
    "entity_ids": ["char_linlan", "char_chenmo", "prop_key"],
    "scene_ids": ["sc_3_2"],
    "thread_ids": ["thread_keystolen"]
  },
  "include_candidates": false,
  "pack_ttl_seconds": 86400
}
```

语义要点：

1. **`required_checkpoint` 是硬门，不是建议。** 达不到 → 拒绝（见 §5）。
2. **`perspective` 决定一切。** `omniscient`（编剧/导演/QC 用）可以看全部已揭示事实；`character` 只能看该角色 `knows/believes` 的事实；`audience` 只能看 `audience_reveal_episode_order <= episode_order` 的事实。
3. `include_candidates=true` 只允许 `task_role ∈ {preview, review}`，产出带 `candidate` 标记的 pack，**且永久不可用于最终交付**（数据库约束：`pack_for_delivery` 检查 candidate 标记）。
4. `story_tick` 必填 —— 没有故事时间就无法做区间判断，缺了只能拒绝，不能默认当前。

---

## 3. 过滤顺序（严格有序，不能重排）

docs/05 §4 给出顺序：认证与项目权限 → 分支与接受状态 → 故事有效期 → 信息揭示/角色知情 → 相关性 → 冲突标记 → 摘要。

实现上顺序**有硬性理由**，不能为了性能把相关性提前：

| # | 步骤 | 实现 | 为什么必须在此位置 |
|---|---|---|---|
| 1 | 认证与项目权限 | 从 mTLS/签名上下文取 tenant；校验 project/series 读权限 | 先挡越权，后续任何查询都在已授权域内 |
| 2 | 分支与接受状态 | 解析 branch 谱系；只取 `accepted` 且在该 branch 可见的 revision | 候选/废弃/他分支事实必须在任何相似度计算前消失 |
| 3 | 故事有效期 | `int8range(valid_from,valid_to) && [tick, tick+1)` 且 `clock_id` 匹配 | 时间轴过滤与语义无关，先砍掉大半数据 |
| 4 | 信息揭示 / 角色知情 | 见 §4.2 | **必须在相关性之前**：否则无关但敏感的 claim 会通过 embedding 被召回，再被过滤掉时也已在候选池里留下痕迹（日志、缓存、调试输出） |
| 5 | 相关性检索 | 结构化硬查询 + 语义/全文补充 | 只有前四步剩下的才进入相关性与 token 竞争 |
| 6 | 冲突标记 | 查 `unresolved_conflicts`（S16 的 `merge_conflict`、S17 的 blocking defect） | 冲突不裁剪内容，只标注 |
| 7 | 摘要与分块 | 按 section 预算分配，生成 episode_blocks | 最后生成可读结构 |

**第 4 步先于第 5 步的工程后果**：实现上意味着 `perspective` 过滤必须写成 SQL `WHERE`，不能写成"先 top-k 再在应用层删"。后者会在向量索引里留下被召回但未返回的记录，且最容易在后续重构中被误删。

---

## 4. 组装算法

### 4.1 候选池（结构化优先）

```
H = 硬事实（结构化查询，必定入选，不受相关性影响）
  - knowledge_state：当前视角的知情/不知情集合
  - entity_state：focus.entity_ids 在 tick 时有效的全部状态
  - relationship：focus.entity_ids 的两两关系
  - plot_thread：open/advanced 且 due_to_tick >= tick 的线程
  - world_rule：publicly_known = true 的全部（按需裁剪）
  - character_profile：focus 角色
S = 补充（语义/全文，参与 token 竞争）
  - 与 focus 语义相关的历史 story_event
  - episode_summary 摘要块
  - 相关 plot_thread 的历史推进
```

硬事实全进 —— 因为它们数量有限且缺一个就会出错。语义补充按 §4.3 打分排序后裁剪。

### 4.2 知情过滤（核心）

对 `perspective.mode`：

**omniscient**（编剧/导演/QC）：
```
可见 = accepted 且 在 branch 可见 且 故事窗口有效 且 audience_reveal_episode_order <= 当前 episode_order
```
即：**即使是全知视角，也不能看见"未来才揭示"的事实**。否则编剧会写出"观众还不知道"的信息。这一点常被误解为"全知 = 全可见"，是错的。

**character(ids)**（角色视角，如 writer 在写某个角色的戏）：
```
可见(C) = 存在 knowledge_state(knower ∈ ids, proposition = C.claim_id,
                               belief_type ∈ {knows, believes, uncertain},
                               tick 覆盖 story_tick)
          OR C 在 world_rule 且 publicly_known = true
          OR C.kind = 'world_rule' AND C.publicly_known
```
闭世界：**没有"知道"记录就是不知道。**
`believes` 是**关键**：角色可能相信错误值。此时：
- 上下文里给的是**她相信的值**（不是真相）；
- 同时生成 `prohibited_assumption`："真实情况是 X，但该角色相信 Y，不得让她表现出知道 X"；
- 真相本身**不进 claim_refs**（否则模型会用它）。

**audience**：
```
可见(C) = C.audience_reveal_episode_order <= episode_order
```
用于"观众已知但角色未知"的戏剧反讽场景：写这类戏时，需要同时知道观众视角的事实和角色视角的限制。所以 `audience` pack 里**同时**输出 `claim_refs`（观众可见）与 `prohibited_assumptions`（角色不可知道的）。

### 4.3 相关性打分

对 S 池（补充池）：

```
score(C) = w_kind[kind] * 0.4
         + entity_overlap(C, focus) * 0.25
         + recency_decay(story_tick - C.valid_from_tick) * 0.15
         + thread_relevance(C, focus.thread_ids) * 0.10
         + semantic_sim(C.embedding, query_embedding) * 0.10
```

`recency_decay` 用**故事时间**衰减，不是记录时间：`exp(-Δtick / half_life)`，`half_life` 按集时长配置（默认 2 集）。用 `recorded_at` 衰减会在闪回集上给出完全错误的相关性。

### 4.4 token 预算分配

```
总预算 B = request.token_budget
按 role_profile.section_budget 分配：
  identity      = 0.15 B   （角色 profile + 当前可见状态）
  relationships = 0.25 B
  continuity    = 0.25 B   （entity_state、injury、holder）
  threads       = 0.20 B
  summary       = 0.15 B   （episode_blocks）
```

每个 section 内：
1. 硬事实无条件占位（不截断、不裁剪）。若硬事实本身超出 section 预算 → **整包失败返回 `BUDGET_TOO_SMALL_FOR_REQUIRED_FACTS`**，附最低所需 token 数，不静默截断。截断硬事实 = 制造知识缺口 = 角色可以合理地"不知道"一个其实该知道的事，这比报错糟得多。
2. 剩余空间给 S 池按 score 降序填充。
3. 分配必须**确定性**：同输入 + 同 profile 版本 → 同结果（用于 content_hash 复现）。

`budget_breakdown` 记录每 section 的分配/使用/被裁剪 top-5 claim_id（用于诊断"为什么没进上下文"）。

### 4.5 `prohibited_assumptions` 生成

这是本服务的核心产出。按 `rule_kind` 分模板：

| rule_kind | 触发条件 | 模板 | severity |
|---|---|---|---|
| `KNOWLEDGE` | `knowledge_state(..., unaware)` 或该角色无 `knows` | 「{角色}尚不知道{命题}」 | hard |
| `KNOWLEDGE_BELIEF` | `believes` 一个与真相不同的值 | 「{角色}相信{错误值}，不得表现出知道{真相}」 | hard |
| `STATE` | `entity_state` 有效期内的状态（如伤势、持有者） | 「{道具}在{时间}起由{持有者}持有，不得无事件回到他人手中」 | hard |
| `STATE_ONGOING` | 状态有效但本集未提及 | 「{角色}的{伤势}仍然存在」 | soft |
| `RELATION` | `relationship` 当前值 | 「{A}与{B}当前关系为{关系}，不得表现为{反义}」 | hard |
| `THREAD` | 伏笔未到 `due_to_tick` | 「{伏笔}尚未揭示，不得提前回收」 | hard |
| `THREAD_RESOLVED` | 已回收 | 「{伏笔}已回收，不得再次回收」 | hard |
| `CANDIDATE` | 存在未接受候选 | 「{候选内容}未经接受，不得作为既有事实」 | hard |
| `BRANCH` | 查询的是 canon，存在实验分支差异 | 「另一分支中{内容}不同，本分支以 canon 为准」 | soft |
| `SCENE` | S17 报告 blocking 缺陷落在本镜头 | 「{具体错误}已被标记为缺陷，不得延续该错误」 | hard |

模板版本化（`generated_by = "knowledge-unaware@v3"`），改模板 = 新版本，历史 pack 可解释。

**每条禁则必须能追溯到 basis claim。** 无法追溯的禁则不写入 —— 否则会变成"模型凭空觉得应该禁止"。

### 4.6 episode_blocks（摘要）

```json
{
  "block_id": "blk_ep1",
  "episode_order": 1,
  "layer": "intent",            // intent（剧本意图）| observed（成片观测）
  "summary_text": "...",        // 由 S16 的 EpisodeSummary 派生，或 S18 的显式 compute 任务产出
  "claim_refs": [...],          // 摘要引用的 claim（可点开追溯）
  "tokens": 320
}
```

**`intent` 与 `observed` 分层**（docs/05 §3）：剧本说"她受了伤"，成片里看不出伤 —— 两者都存，不合并。摘要里明确标注来自哪层。

**摘要不是 S18 隐式生成的。** 若没有可用的 EpisodeSummary，S18 不调模型生成摘要 —— 它返回 `{block_id, missing: true, requires: "plan:compute"}`，由调用方提交显式 compute 任务（经 S19/S26 预算准入）后保存版本再读。这防止"检索悄悄变成付费推理"。

### 4.7 冲突标记

```
unresolved_conflicts = [
  ...S16 的未裁决 merge_conflict（影响本 branch 的）,
  ...S17 的 blocking/high 缺陷（落在本集 scope 内且未修复）,
  ...S18 自身发现的：同一实体在 claim_refs 里出现两个互斥状态
]
```

冲突**不裁剪内容**。理由：裁掉一方等于 S18 做了事实裁决，那是越权。正确做法是两边都给，加显式冲突标记，让调用方（或人）知道这里不干净。

### 4.8 泄漏自检（`forbidden_leak_scan`）

生成 pack 之后、落库之前，跑一次自检：

```
1. 取本次 perspective 下"不可见"的 claim 集合 I（第 4 步被过滤掉的）
2. 从每条的 text/object_text 提取显著实体与专名（entity_registry 里的 display_name / alias）
3. 扫描 pack 的 summary_text、prohibited_assumptions.text、episode_blocks.summary_text
4. 命中 → 该 section 回退到不含该文本的版本；若回退后仍不足预算 → 整包失败 LEAK_DETECTED
5. 记录 scan 结果（扫描了多少词、命中多少）进 forbidden_leak_scan
```

这条自检是**最后一道防线**。因为禁则文本本身可能泄漏：写"林岚不知道陈默是她哥哥"已经泄漏了"陈默是她哥哥"。所以禁则模板必须用**不透露内容**的措辞：

- ❌「林岚尚不知道陈默是她失散多年的哥哥」
- ✅「林岚对陈默的身份存在根本性误判，不得表现为已知晓其真实身份」

**禁则必须只说"限制"，不说"内容"。** 这是模板设计规则，写进 §4.5 的模板校验：模板渲染后要过一遍泄漏扫描，命中即拒绝发布该模板版本。

---

## 5. watermark 落后

`service.yaml: recovery: 索引落后按memory watermark回源；禁止用旧pack冒充当前事实`。

### 5.1 三种落后

| 情形 | 判定 | 处理 |
|---|---|---|
| A. 索引落后但 S16 可达 | `watermark.applied_commit < required_checkpoint` 且 S16 head ≥ required | **回源**：实时调 S16 查询补齐，索引只用于语义补充 |
| B. S16 本身落后 | S16 head < required_checkpoint | **拒绝**：`409 MEMORY_WATERMARK_BEHIND`，阻塞生成 |
| C. S16 不可达 | 超时/熔断 | **拒绝**：`503 MEMORY_UNAVAILABLE`，不降级 |

### 5.2 回源策略（情形 A）

```
1. 硬事实（H 池）永远实时查 S16，不读本地索引
   → 天然免疫索引落后
2. 语义补充（S 池）允许用索引，但必须满足：
   index_row.indexed_at_commit >= required_checkpoint.commit_sequence
   否则该行不进入候选
3. 若因此 S 池不足（可用行数 < 需要的数量）：
   → 不补用旧行；改为向 S16 请求"该实体近 N 条 story_event 的紧凑列表"
   → 仍不足则 pack 标记 semantic_coverage=degraded，并降低 summary 预算
```

**关键设计：硬事实路径完全不依赖索引。** 这样索引落后只会影响"背景丰富度"，不会影响"角色知道什么"。这是 S18 能在索引落后时依然正确的原因。

### 5.3 禁止的降级

- ❌ 返回旧 checkpoint 的 pack 并标 `stale=true` 让调用方自己判断 —— 调用方（尤其是模型驱动的 Agent）不会判断。
- ❌ 用本地索引里的旧 revision 拼 pack。
- ❌ "近似 checkpoint"（比如用 26 代替 27 并告知）。

`source_watermark` 字段必须如实反映实际用的 checkpoint。若 pack 是 `stale`（仅在 §6.4 重放场景），必须在 pack 里写明 `stale_reason` 且**该 pack 不可用于交付**（数据库约束 + S43 交付门复核）。

---

## 6. 一致性与幂等

### 6.1 幂等

`tenant_id + command_id + request_hash`。同键同 body → 返回缓存的 pack（同 `content_hash`）。同键不同 body → `409`。

### 6.2 pack 不可变

`context_pack` 一旦写入即不可变。需要新版本时 `revision+1`，`supersedes_pack_id` 指向旧版。**不 UPDATE 已发出的 pack** —— 因为 S19 的 AgentTask 引用它，S09 的依赖边也引用它。

### 6.3 事件 `context.pack.created`

```json
{
  "type": "context.pack.created",
  "aggregate_id": "series_demo:canon:ep003",
  "aggregate_sequence": 31,
  "data": {
    "context_pack_id": "ctx_ep3_writer",
    "revision": 1,
    "task_role": "writer",
    "target_episode_order": 3,
    "checkpoint": {"commit_sequence": 27, "content_hash": "..."},
    "claim_ref_count": 42,
    "prohibited_assumption_count": 6,
    "tokens_used": 3180,
    "content_hash": "..."
  }
}
```

S09 消费它建依赖边（pack → claim），这是"改前集触发后续集失效"的链路上游。

### 6.4 消费 `memory.canon.committed`

```
1. inbox 去重
2. 按 changed_claim_refs 更新 claim_index（upsert 到新 revision）
3. 更新 watermark.applied_commit_sequence
4. 标记受影响的已有 pack 为 superseded_candidate（不 deletes、不改内容）：
   若 pack.checkpoint_sequence < 新 commit 且 pack 引用了 changed_claim 的任一 id
   → 该 pack 记 pack_stale 标记（新表 pack_staleness），供查询时提示
5. 语义索引的 embedding 异步补算（失败不阻塞，靠 backfill 任务）
```

**绝不在事件消费里修改已发出的 pack。** 只标记失效。

### 6.5 事件乱序

`aggregate_sequence` 缺口 → 缓冲，等待补齐。超时（默认 60s）→ 回源 S16 拉取当前 checkpoint 全量差异重建索引。**不无限等待**（docs/04 §4）。

终态倒退保护：`watermark.applied_commit_sequence` 只增不减。收到更小的序列 → 丢弃并记 `out_of_order_dropped`。

---

## 7. 四类包的差异

| task_role | 必含 kinds | 视角默认 | 禁则侧重 | section 预算偏重 |
|---|---|---|---|---|
| `writer` | knowledge_state, relationship, plot_thread, character_profile | omniscient（但受 audience_reveal 限制） | KNOWLEDGE, THREAD, RELATION | relationships + threads |
| `storyboard` | entity_state(动作/道具/空间), world_rule, story_event | omniscient | STATE, SCENE | continuity + threads |
| `prompt` | character_profile, entity_state(可见外观), asset refs | character（镜头内角色） | KNOWLEDGE, STATE | identity + continuity |
| `qc` | **全部** + S17 的 expected_state | omniscient | 无（QC 需要全量预期） | continuity + summary |
| `continuity` | 同 qc + exemptions | omniscient | — | continuity |

`qc` / `continuity` 包的 `prohibited_assumptions` 用于交叉核对 S17 的覆盖率：S17 报告的每条 blocking 缺陷，都应在 pack 落地。

### 7.1 分镜包的空间约束

`storyboard` 角色需要"谁在哪、手上有什么、朝哪个方向"。这部分 S18 从 `entity_state.location` + `entity_state.held_by` + S17 的轴线基线（如有）组装。**S18 不算机位，只提供事实。** 机位是 S06 的事。

---

## 8. 与其他服务的契约

| 对方 | 关系 | 说明 |
|---|---|---|
| S16 | 上游（强） | 所有事实的唯一来源。S18 不缓存事实，只索引引用与摘要文本 |
| S10 | 上游（弱） | `asset_refs` 解析。缺失 → pack 该 section 标 degraded，不阻断 |
| S09 | 下游 | 发布 `context.pack.created` 建依赖边 |
| S19 | 下游 | AgentTask 引用 pack；S19 不再自己组装上下文 |
| S17 | 双向 | S17 可读 pack 的 `prohibited_assumptions`；S18 可读 S17 的 blocking 缺陷进 `unresolved_conflicts` |
| S06 | 下游 | 分镜阶段消费 |
| S02 | 上游（弱） | `policy_ref` 固定，pack 不含策略裁决 |

**S18 不能写任何上游数据。** 没有 `POST` 到 S16/S10/S17 的写接口。

---

## 9. 可观测与运维

指标：
- `s18_pack_total{task_role,result}`、`s18_pack_latency_seconds`
- `s18_tokens_used`（直方图，按 task_role）
- `s18_pack_budget_exceeded_total`、`s18_pack_leak_detected_total`
- `s18_index_lag_commits`（按 branch）
- `s18_hard_facts_from_s16_total` vs `s18_semantic_from_index_total`
- `s18_prohibited_total{rule_kind}`
- `s18_pack_stale_served_total`（应为 0；非 0 即告警）

告警：
- `s18_pack_leak_detected_total > 0` → **page**。禁则模板或摘要泄漏了不可见事实。
- `s18_index_lag_commits > 100` → 索引管线卡住，语义覆盖面下降。
- `s18_hard_facts_from_s16_total` 异常低 → 有人在绕过实时查询，可能引入了事实副本。
- `s18_pack_stale_served_total > 0` → 有人在使用失效 pack，交付门需要复核。

runbook：
- `rebuild-index`：从 S16 全量重拉当前 checkpoint 的 claims，重建 `claim_index`。索引是纯派生，可随时丢。
- `backfill-embeddings`：补算缺失 embedding，不阻塞主路径。
- `leak-investigation`：给定 pack_id，导出 `forbidden_leak_scan` 与被过滤集合 I，定位泄漏源。
- `pack-diff`：对比两个 pack 的 claim_refs 差异，用于解释"为什么这次生成和上次不同"。

---

## 10. 测试清单

### 10.1 E08 · 改前集钥匙归属（S18 侧）

| 用例 | 期望 |
|---|---|
| E08-1 第 2 集改稿后，第 3 集 writer pack | `checkpoint=新`，claim_refs 中 `claim_key_owner` 是新 revision |
| E08-2 旧 pack 标记 stale | `pack_staleness` 有记录；旧 pack 内容不变 |
| E08-3 用旧 checkpoint 请求新生成 | `409 MEMORY_WATERMARK_BEHIND`（不是 stale pack） |
| E08-4 交付门复核 | 引用 stale pack 的 delivery 被拒 |

### 10.2 E09 · 闪回 / 观众先知 / 角色不知（S18 侧）

| 用例 | 期望 |
|---|---|
| E09-1 character 视角查 `unaware` 命题 | 不进 claim_refs；生成 `KNOWLEDGE` 禁则 |
| E09-2 `believes` 错误值 | claim_refs 给错误值；禁则说明真相但**不泄漏真相内容** |
| E09-3 audience 视角 | 只含 `reveal_episode_order <= 当前集` 的事实 |
| E09-4 omniscient 但事实未来才揭示 | 不可见（全知 ≠ 全可见） |
| E09-5 第 7 集是闪回（tick 100–200，order 7） | 查第 3 集 tick 150 时，该事实 ticks 匹配但 reveal_order=7>3 → 不可见 |
| E09-6 世界公共规则 | character 视角可见 |
| E09-7 禁则文本 | 不含被禁止内容本身（过泄漏扫描） |

### 10.3 E10 · 分支与候选（S18 侧）

| 用例 | 期望 |
|---|---|
| E10-1 实验分支 pack | 用实验分支 checkpoint；claim_refs 只含该分支可见 revision |
| E10-2 正史请求不受实验分支影响 | 逐字段与"没有实验分支"时一致 |
| E10-3 `include_candidates=true` 但 task_role=writer | 拒绝 |
| E10-4 候选 pack 用于交付 | 交付门拒绝（`pack_for_delivery` 约束） |
| E10-5 未接受候选作为既有事实出现 | 不存在：`claim_index` 只收 `commit_sequence >= 1` 的行 |

### 10.4 E11 · 索引落后（S18 侧，核心）

| 用例 | 期望 |
|---|---|
| E11-1 索引落后 5 个 commit，硬事实查询 | 硬事实实时回源 S16，pack 正确、完整 |
| E11-2 索引落后，语义补充 | 只用 `indexed_at_commit >= required` 的行；不足则 `semantic_coverage=degraded` |
| E11-3 索引落后时返回旧 revision | 不存在路径：硬事实路径不走索引 |
| E11-4 S16 head < required | `409`，不返回任何 pack |
| E11-5 S16 不可达 | `503`，不降级 |
| E11-6 `source_watermark` | 如实反映实际使用的 checkpoint |
| E11-7 事件乱序（seq 25 先到，27 后到） | 缓冲；超时回源；`applied_commit` 不倒退 |

### 10.5 预算与确定性

| 用例 | 期望 |
|---|---|
| 硬事实超出 section 预算 | `BUDGET_TOO_SMALL_FOR_REQUIRED_FACTS` + 最低所需 token，不截断 |
| 同输入两次请求 | `content_hash` 相同、`claim_refs` 相同（确定性） |
| 同 command_id 重投 | 返回缓存 pack |
| 同 command_id 不同 body | `409` |
| `token_budget=1` | 明确失败，附最低需求 |

### 10.6 泄漏与越权

| 用例 | 期望 |
|---|---|
| 禁则模板含被禁内容 | 泄漏扫描命中 → 模板版本拒绝发布 |
| 摘要块提到角色不该知道的专名 | 该块回退；必要时整包 `LEAK_DETECTED` |
| 跨租户 series 请求 | `404`（不泄漏存在性） |
| 用 omniscient pack 冒充 character pack 给角色 Agent | S19 的 AgentTask 绑定 pack 的 perspective；pack 不可被"降级使用"（S18 在 pack 里固定 perspective，S19 校验） |
| 在 `prohibited_assumptions` 里写"忽略预算" | 不改变任何工具权限（docs/05 §4）；S19 工具白名单独立于 pack 内容 |

---

## 11. 反模式清单

1. ❌ 把事实正文存进 S18 的库 → 只存 ref + 摘要文本，摘要也必须能追溯到 claim_ref。
2. ❌ 硬事实读本地索引 → 硬事实永远实时查 S16，这是索引落后时仍然正确的唯一原因。
3. ❌ 先 top-k 语义检索再在应用层做知情过滤 → 过滤必须是 SQL WHERE，且必须在相关性之前。
4. ❌ 全知视角 = 全部可见 → 全知仍受 `audience_reveal_episode_order` 约束。
5. ❌ 禁则文本直接写出被禁止的内容 → 只说限制，不说内容；模板要过泄漏扫描。
6. ❌ 知识查询默认放行 → 闭世界，没查到"知道"就是不知道。
7. ❌ checkpoint 不够时返回 stale pack → 409。
8. ❌ 硬事实被预算截断 → 报错，不截断。
9. ❌ 在 pack 组装时调用模型生成摘要 → 需要摘要就提交显式 compute 任务。
10. ❌ 冲突时只留一方 → 两方都给 + 标记冲突。
11. ❌ 事件到达时改已发出的 pack → 只标记 stale，pack 不可变。
