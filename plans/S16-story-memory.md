# S16 · 跨集记忆与正史 — 实现方案

状态：`design_only`。本文是实现方案，不是已实现的服务；SLO 未验证，容量未基准测试。

上游约束：[docs/05](../docs/05-cross-episode-memory.md)、[docs/04](../docs/04-data-consistency.md)、[docs/06](../docs/06-automation-governance.md)、[S16 规格](../docs/design-package/services/S16-story-memory.md)、[memory-claim.v2](../contracts/memory-claim.v2.schema.json)。

---

## 0. 一句话边界

**S16 是"谁在什么时候知道什么、相信什么、持有什么"的唯一权威。** S17 只读做校验，S18 只读做组装，S10 只持有可视化变体，S09 只持有依赖边。任何其他服务出现第二张"钥匙归谁"的表，都是设计错误。

本方案要回答的核心问题：**怎么保证角色不知道自己不该知道的事。** 答案分三层，缺一层都不成立：

1. **写层**：知情范围是一等公民实体 `knowledge_state`，不是备注、不是 Prompt 里的一句话；没有证据的推测只能停在 `claim_candidate`，永远进不了 `claim_revision`。
2. **读层**：知识查询是闭世界语义 —— **没查到"知道"就等于"不知道"**，默认拒绝，而不是默认放行。
3. **时间层**：故事时间（story time）与系统记录时间（recorded time）分离，另外还有 `episode_order` 与 `audience_reveal_episode_order`，三者共同决定"这个角色在这一刻能不能知道"。

---

## 1. 数据模型

数据库：独立 PostgreSQL 集群（docs/04 §1 要求 S16 优先物理隔离），schema `story_memory`。所有表带 `tenant_id`，所有主键含 `tenant_id`（防跨租户 hash 存在性泄漏，E28）。

### 1.1 故事时钟与集窗口

```sql
CREATE TABLE story_memory.story_clock (
  tenant_id        text NOT NULL,
  series_id        text NOT NULL,
  clock_id         text NOT NULL,          -- story_main / story_flashback_ep7 / story_side_a
  clock_kind       text NOT NULL,          -- main | flashback | dream | side
  parent_clock_id  text,                   -- 闪回时钟挂在主时钟下，共享 entity 命名空间
  origin_episode_id text,
  tick_min         bigint NOT NULL,
  tick_max         bigint NOT NULL,
  PRIMARY KEY (tenant_id, series_id, clock_id)
);

CREATE TABLE story_memory.episode_story_window (
  tenant_id        text NOT NULL,
  series_id        text NOT NULL,
  episode_id       text NOT NULL,
  episode_order    integer NOT NULL,       -- 播出顺序，1 起
  clock_id         text NOT NULL,
  tick_start       bigint NOT NULL,
  tick_end         bigint NOT NULL,        -- 半开区间 [start, end)
  PRIMARY KEY (tenant_id, series_id, episode_id),
  UNIQUE (tenant_id, series_id, episode_order)
);
```

关键点：**闪回集不改变 `episode_order`，只改变 `clock_id + tick`。** 第 7 集播出顺序是 7，但它的故事窗口可能是主时钟 tick 100–200 的闪回。这就是 E09 的物理基础 —— 只靠 `episode_order` 排序的实现在这里必然出错。

### 1.2 分支（copy-on-write）

```sql
CREATE TABLE story_memory.canon_branch (
  tenant_id             text NOT NULL,
  series_id             text NOT NULL,
  branch_id             text NOT NULL,        -- 'canon' 为默认正史分支
  parent_branch_id      text,
  fork_commit_sequence  bigint,               -- 父分支在分叉时刻的 commit_sequence
  fork_checkpoint_hash  bytea,
  branch_kind           text NOT NULL,        -- canon | experiment | merge_preview
  status                text NOT NULL,        -- active | merged | abandoned | frozen
  merge_status          text NOT NULL DEFAULT 'none', -- none|pending|conflicted|merged
  head_commit_sequence  bigint NOT NULL DEFAULT 0,
  head_checkpoint_hash  bytea NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, series_id, branch_id),
  FOREIGN KEY (tenant_id, series_id, parent_branch_id)
    REFERENCES story_memory.canon_branch (tenant_id, series_id, branch_id)
);
```

分支**不复制任何 claim 行**。分叉 = 插入一行 `canon_branch`，`fork_commit_sequence` 指向父分支当时的 head。这是 copy-on-write 的全部实现代价。

```sql
-- 派生、可重建：分支谱系展开，避免递归 CTE 出现在热路径
CREATE TABLE story_memory.branch_ancestry (
  tenant_id          text NOT NULL,
  series_id          text NOT NULL,
  branch_id          text NOT NULL,
  ancestor_branch_id text NOT NULL,
  depth              integer NOT NULL,     -- 0 = 自己
  PRIMARY KEY (tenant_id, series_id, branch_id, ancestor_branch_id)
);
```

### 1.3 提交批次与 checkpoint

```sql
CREATE TABLE story_memory.memory_commit (
  tenant_id           text NOT NULL,
  series_id           text NOT NULL,
  branch_id           text NOT NULL,
  commit_sequence     bigint NOT NULL,        -- 每 series+branch 单调递增
  previous_sequence   bigint NOT NULL,
  previous_checkpoint_hash bytea NOT NULL,
  checkpoint_hash     bytea NOT NULL,
  commit_kind         text NOT NULL,          -- extract|supersede|revert|merge|branch_init|repair
  source_manifest_ref jsonb,                  -- {id, revision, content_hash}
  merge_source_refs   jsonb,                  -- merge 时：[{branch_id, commit_sequence, checkpoint_hash}]
  revert_of_sequence  bigint,                 -- revert 时：被回滚的 commit
  extraction_batch_id text,
  command_id          text NOT NULL,
  actor_principal     text NOT NULL,          -- 服务身份或人，来自认证派生，不取 header
  recorded_at         timestamptz NOT NULL,
  claim_count         integer NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id, commit_sequence)
);

-- 同一个已接受 manifest 只能产生一次 extract 提交（重复事件幂等，docs/05 §3）
CREATE UNIQUE INDEX memory_commit_manifest_once
  ON story_memory.memory_commit (tenant_id, series_id, branch_id,
                                 (source_manifest_ref->>'id'),
                                 (source_manifest_ref->>'revision'))
  WHERE commit_kind = 'extract';
```

**checkpoint hash 算法**（链式，只对本次增量取哈希，不是全量状态快照）：

```
checkpoint_hash(seq) = sha256(
  "memcp/v2\n" || series_id || "\n" || branch_id || "\n" || seq || "\n" ||
  hex(previous_checkpoint_hash) || "\n" ||
  join("\n", sorted([ claim_id, revision, hex(content_hash) ] over this commit's changed claims))
)
```

genesis：`commit_sequence = 0`，`checkpoint_hash = sha256("memcp/v2/genesis\n"+series_id+"\n"+branch_id)`。

链式的好处：任意两个 checkpoint 之间可以证明"没有中间提交被跳过"，而且可以离线重算校验（见 §9 runbook `verify-checkpoint-chain`）。

### 1.4 集 → checkpoint 绑定（历史可追溯）

```sql
CREATE TABLE story_memory.commit_manifest_binding (
  tenant_id        text NOT NULL,
  series_id        text NOT NULL,
  branch_id        text NOT NULL,
  commit_sequence  bigint NOT NULL,
  manifest_ref     jsonb NOT NULL,
  episode_id       text NOT NULL,
  episode_order    integer NOT NULL,
  clock_id         text NOT NULL,
  tick_start       bigint NOT NULL,
  tick_end         bigint NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id, commit_sequence, episode_id)
);
```

这张表是 E08 的关键：**第 2 集改稿后，已交付的第 3 集仍然指向它当时用的旧 checkpoint。** 不迁移就不重写，历史证据可解释（docs/07 §3）。

### 1.5 Claim：稳定 ID + 不可变 revision

```sql
CREATE TABLE story_memory.claim (
  tenant_id        text NOT NULL,
  series_id        text NOT NULL,
  claim_id         text NOT NULL,            -- 稳定，跨 revision 不变
  kind             text NOT NULL,            -- world_rule|character_profile|story_event|
                                             -- entity_state|relationship|knowledge_state|
                                             -- plot_thread|creative_preference
  subject_id       text NOT NULL,
  predicate        text NOT NULL,
  origin_branch_id text NOT NULL,
  created_sequence bigint NOT NULL,
  PRIMARY KEY (tenant_id, series_id, claim_id),
  -- 同一实体同一谓词只能有一个 claim 主体，避免"两把钥匙两张表"
  UNIQUE (tenant_id, series_id, kind, subject_id, predicate)
);
```

`(kind, subject_id, predicate)` 唯一，意味着 `held_by(prop_key)` 在整条 series 里**有且仅有一条 claim 谱系**。改归属是给同一个 `claim_id` 追加 revision，不是新建一条 claim。这条约束把"两个服务各记一个钥匙归谁"在数据库层面直接堵死。

```sql
CREATE TABLE story_memory.claim_revision (
  tenant_id            text NOT NULL,
  series_id            text NOT NULL,
  claim_id             text NOT NULL,
  revision             integer NOT NULL,
  status               text NOT NULL,         -- accepted|superseded|rejected
  fact                 jsonb NOT NULL,        -- {subject_id, predicate, value}
  clock_id             text NOT NULL,
  valid_from_tick      bigint NOT NULL,
  valid_to_tick        bigint,                -- NULL = 至今有效（开放区间）
  recorded_at          timestamptz NOT NULL,
  commit_sequence      bigint NOT NULL,
  branch_id            text NOT NULL,         -- 该 revision 在哪个分支提交的
  source_refs          jsonb NOT NULL,        -- [{service_id,id,revision,content_hash}]
  accepted_manifest_ref jsonb NOT NULL,
  epistemic_scope      jsonb NOT NULL,        -- {perspective,knower_ids,belief_type,audience_reveal_episode_order}
  supersedes           jsonb NOT NULL,        -- [{service_id,id,revision,content_hash}]
  confidence           numeric(4,3) NOT NULL,
  review_state         text NOT NULL,         -- auto|human_accepted|human_rejected
  content_hash         bytea NOT NULL,        -- sha256(canonical_json(本行业务字段))
  PRIMARY KEY (tenant_id, series_id, claim_id, revision),
  -- 契约要求：accepted 必须有 manifest 与正 commit_sequence
  CONSTRAINT accepted_needs_evidence CHECK (
    status <> 'accepted' OR (accepted_manifest_ref IS NOT NULL AND commit_sequence >= 1)
  ),
  CONSTRAINT interval_sane CHECK (valid_to_tick IS NULL OR valid_to_tick > valid_from_tick)
);
```

**这里没有 `candidate` 状态。** 契约允许 `candidate` 是给预览读用的，本服务把它物理隔离在 §1.7 的 `claim_candidate` 表里 —— 这是"草稿不能污染正史"的最强实现方式：候选根本没有 `commit_sequence` 字段可填。

### 1.6 类型化投影（可重建，不是权威）

`claim_revision.fact` 是事实本体；下列表只存**可索引、可做唯一约束的窄列**，全部带 `claim_revision` 外键，标注 `rebuildable`。

```sql
CREATE TABLE story_memory.knowledge_state (
  tenant_id             text NOT NULL,
  series_id             text NOT NULL,
  claim_id              text NOT NULL,
  revision              integer NOT NULL,
  knower_id             text NOT NULL,        -- 谁
  proposition_claim_id  text NOT NULL,        -- 对哪个命题
  belief_type           text NOT NULL,        -- fact|knows|believes|unaware|uncertain
  source_event_id       text NOT NULL,        -- 通过哪个事件得知/被误导
  clock_id              text NOT NULL,
  valid_from_tick       bigint NOT NULL,
  valid_to_tick         bigint,
  audience_reveal_episode_order integer NOT NULL,
  PRIMARY KEY (tenant_id, series_id, claim_id, revision)
);

CREATE INDEX knowledge_lookup
  ON story_memory.knowledge_state (tenant_id, series_id, knower_id, proposition_claim_id, clock_id, valid_from_tick);
```

这张表是"林岚尚不知道陈默身份"落地的地方。它不是备注：`knower_id = char_linlan`、`proposition_claim_id = claim_chenmo_identity`、`belief_type = unaware`、`source_event_id` 指向让她误判的那个事件。查询时它可以被 join、被过滤、被断言，也能被 S17 用来判断台词是否越界。

其余投影同构，只列关键列：

| 表 | 关键列 | 用途 |
|---|---|---|
| `story_event` | `event_id(=claim_id)`, `participants text[]`, `causes text[]`, `exclusive_group`, `diegetic_frame` | 因果、互斥事件检测 |
| `entity_state` | `entity_id`, `predicate`, `value_text`, `cardinality`(1=唯一持有者) | 持有者/伤势/位置 |
| `relationship` | `subject_id`, `object_id`, `relation`, `direction` | 信任/亲属/敌对/债务 |
| `plot_thread` | `thread_id`, `thread_state`, `due_from_tick`, `due_to_tick`, `resolved_by_event_id` | 伏笔回收 |
| `world_rule` | `rule_id`, `scope`, `exceptions jsonb`, `publicly_known bool` | 公共常识判定 |
| `character_profile` | `entity_id`, `profile_revision`, `abilities text[]` | 能力边界 |
| `creative_preference` | `owner_id`, `scope`, `explicit bool`, `expiry_tick` | 导演偏好 |

`world_rule.publicly_known = true` 是闭世界知识模型里唯一的"默认知道"出口 —— 年代、公共法律、剧中被广泛报道的事实。它必须被显式配置，不能靠模型推断。

### 1.7 候选隔离区

```sql
CREATE TABLE story_memory.claim_candidate (
  tenant_id          text NOT NULL,
  series_id          text NOT NULL,
  branch_id          text NOT NULL,
  batch_id           text NOT NULL,           -- 一次抽取任务的批次
  candidate_id       text NOT NULL,
  proposed_claim_id  text,                    -- 可能是新 claim
  kind               text NOT NULL,
  fact               jsonb NOT NULL,
  clock_id           text NOT NULL,
  valid_from_tick    bigint NOT NULL,
  valid_to_tick      bigint,
  epistemic_scope    jsonb,
  source_refs        jsonb NOT NULL,
  confidence         numeric(4,3) NOT NULL,
  extracted_by       text NOT NULL,           -- agent_definition_revision，可追溯是谁抽的
  extraction_manifest_ref jsonb NOT NULL,
  validation_state   text NOT NULL,           -- pending|structurally_valid|conflict|rejected|promoted
  validation_errors  jsonb NOT NULL DEFAULT '[]',
  created_at         timestamptz NOT NULL DEFAULT now(),
  expires_at         timestamptz NOT NULL,    -- 默认 30 天，过期由 GC 删除
  PRIMARY KEY (tenant_id, series_id, branch_id, batch_id, candidate_id)
);
```

硬规则：`claim_candidate` **没有** `commit_sequence`、**没有** `accepted_manifest_ref`、**没有** `status='accepted'`。它无法被 `claim_revision` 的读路径看见，也无法伪造一次提交。只有 §3 的晋升流程能把它变成 `claim_revision`。

### 1.8 实体命名空间（防抽取幻觉）

```sql
CREATE TABLE story_memory.entity_registry (
  tenant_id     text NOT NULL,
  series_id     text NOT NULL,
  entity_id     text NOT NULL,
  entity_type   text NOT NULL,               -- character|prop|location|org|thread
  display_name  text NOT NULL,
  owner_service text,                        -- S11/S14/... 可视化变体在谁那
  status        text NOT NULL                -- active|deprecated|merged_into
);
CREATE TABLE story_memory.entity_alias (
  tenant_id, series_id, alias_text text NOT NULL, entity_id, confidence, source
  PRIMARY KEY (tenant_id, series_id, alias_text, entity_id)
);
```

抽取出来的 `subject_id` 必须能在 `entity_registry` 里解析（直接命中或别名唯一命中）。解析不到 → 候选直接 rejected，`validation_errors = ["UNRESOLVED_ENTITY"]`。这一条挡掉大部分"记忆抽取幻觉"。

### 1.9 与 memory-claim.v2 契约的对齐

| 契约字段 | 本实现来源 | 说明 |
|---|---|---|
| `claim_id` / `revision` | `claim` / `claim_revision` | 稳定 ID + 不可变 revision |
| `status` | `claim_revision.status` | 本表只存 `accepted` / `superseded`；`rejected` 只存在于 `claim_candidate.validation_state`，**不产生 revision** |
| `fact` | `claim_revision.fact` | 原样 |
| `story_validity` | `clock_id` + `valid_from_tick` + `valid_to_tick` | 拆成窄列以便区间索引 |
| `recorded_at` / `commit_sequence` | 同名列 | |
| `source_refs` / `supersedes_claim_refs` | `source_refs` / `supersedes` | `supersedes_claim_refs` 缺省时序列化为 `[]`，不省略 |
| `accepted_manifest_ref` | 同名列 | 非空（所有 revision 都曾 accepted）；契约允许 null 是给预览候选用的，本服务不在此表表达候选 |
| `epistemic_scope` | 同名列 + `knowledge_state` 投影 | 契约要求的四个子字段全部必填；`knower_ids` 缺省 `[]` |
| `confidence` | 同名列 | `numeric(4,3)`，序列化为 number |

`content_hash` 不出现在契约里但必须存在：它是 checkpoint 链与 `supersedes` 校验的输入，取 `sha256(canonical_json(上表除 content_hash 外全部业务字段))`。

### 1.10 基础设施表

```sql
CREATE TABLE story_memory.idempotency_key (
  tenant_id       text NOT NULL, command_id text NOT NULL, request_hash bytea NOT NULL,
  response_body   jsonb, response_status int, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);  -- 同键同 hash 返回缓存；同键不同 hash → 409

CREATE TABLE story_memory.outbox_event (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,              -- series_id || ':' || branch_id
  aggregate_sequence bigint NOT NULL,
  event_type         text NOT NULL,
  partition_key      text NOT NULL,              -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON story_memory.outbox_event (id) WHERE published_at IS NULL;

CREATE TABLE story_memory.inbox_consume (
  tenant_id  text NOT NULL, consumer text NOT NULL, event_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, consumer, event_id)
);
```

### 1.11 生效视图（overlay 解析结果，可重建）

```sql
CREATE TABLE story_memory.canon_effective_claim (
  tenant_id        text NOT NULL,
  series_id        text NOT NULL,
  branch_id        text NOT NULL,
  claim_id         text NOT NULL,
  revision         integer NOT NULL,
  kind             text NOT NULL,
  clock_id         text NOT NULL,
  valid_from_tick  bigint NOT NULL,
  valid_to_tick    bigint,
  commit_sequence  bigint NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id, claim_id)
);

CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE INDEX eff_interval
  ON story_memory.canon_effective_claim
  USING gist (tenant_id, series_id, branch_id, clock_id,
              int8range(valid_from_tick, valid_to_tick, '[)'));
```

**分支解析规则**（写进代码，不是写进文档就算）：

> 对 branch B 的 claim C，取满足以下条件的 revision 中 `depth` 最大者（子分支优先），`depth` 相同取 `commit_sequence` 最大者：
> 1. 该 revision 的 `branch_id` 是 B 的祖先（含 B 自身）；
> 2. `commit_sequence <= ` 该分支在 B 谱系上的可见上界（自身分支为 head，祖先分支为 `fork_commit_sequence`）；
> 3. revision 未被同一分支谱系中更深层的 revision supersede。

实验分支的提交 → 只写自己的 `canon_effective_claim` 行，正史分支的视图完全不受影响（E10 分支隔离）。

---

## 2. 双时间与三轴可见性

一条事实的可见性由三个**正交**的轴决定，缺一个就会出 bug：

| 轴 | 字段 | 解决的问题 |
|---|---|---|
| 故事时间 | `clock_id`, `valid_from_tick`, `valid_to_tick` | 事情在剧情里什么时候成立（闪回、时间跳跃） |
| 记录时间 | `recorded_at`, `commit_sequence` | 系统什么时候知道（乱序到达、补拍、改稿） |
| 揭示范围 | `epistemic_scope.audience_reveal_episode_order`, `episode_story_window.episode_order` | 观众/角色在第几集知道（先拍后播、观众先知） |

**禁止**任何"按 `created_at` 或 `commit_sequence` 取最新"来回答"当前状态是什么"。正确写法是：

```sql
SELECT * FROM story_memory.canon_effective_claim e
WHERE e.tenant_id = $1 AND e.series_id = $2 AND e.branch_id = $3
  AND e.clock_id = $4
  AND e.commit_sequence <= $checkpoint              -- 记录时间轴：只用 checkpoint 之前的
  AND int8range(e.valid_from_tick, e.valid_to_tick, '[)') && int8range($tick, $tick+1, '[)')  -- 故事时间轴
```

两个条件同时出现，才是"在第 checkpoint 个提交所知道的、故事时间 tick 时成立的事实"。

**闪回**：用 `clock_id` 区分。闪回集的事实可以写进主时钟，也可以写进闪回时钟 —— 默认写主时钟（因为闪回内容也是正史的一部分），`diegetic_frame = 'flashback'` 记录叙事框架。查询第 3 集上下文时，若第 7 集是闪回且尚未播出，则 `audience_reveal_episode_order = 7 > 3`，第 3 集不可见。

---

## 3. 写入路径

### 3.1 全链路

```
S04 剧本新 revision
  → (S19 memory-extractor Agent，受约束任务) 抽候选
  → S16 claim_candidate（隔离区）
  → 结构校验 + 冲突预检
  → 人工/自动接受剧本（S40 决定 → S02 政策 → S08 CAS）
  → S08 发布 snapshot.manifest.committed
  → S16 消费：核验来源 → 原子 MemoryCommit → 发布 memory.canon.committed
  → S18/S09 推进索引与依赖水位
```

顺序不能反：**S16 只在 S08 的 manifest 已接受之后提交。** 抽取可以先跑（省钱、快），但提交必须等接受事件。

### 3.2 抽取（付费任务，受预算准入）

抽取是 LLM 任务，必须走 S19 → S26 → S23/S22 的准入协议，不能顺手调模型。请求体：

```json
{
  "role": "memory-extractor",
  "definition_revision": "mem-extract@sha256:...",
  "target": {"series_id": "...", "branch_id": "canon", "episode_id": "ep002"},
  "input_manifest_ref": {...},
  "expected_output": "claim-candidate-batch.v1",
  "budget_scope": {"cap_usd": 0.8, "token_cap": 60000},
  "tool_allowlist": ["drama.memory.read_entity_registry", "drama.script.read_span"],
  "memory_checkpoint": {...}
}
```

抽取器的输出 schema 是封闭的：每条形如
`{proposed_claim_id|null, kind, fact, clock_id, valid_from_tick, valid_to_tick, source_refs[{span}], epistemic_scope, confidence}`。
**输出里没有 `tenant_id`**（平台签名绑定），**没有 `commit_sequence`**（抽取器无权决定提交序号）。LLM 提供的 tenant 字段一律忽略并记录告警。

抽取失败/超预算/超出重试上限 → 该集候选为空 → 正史不变。**失败必须是"没有新事实"，不是"用旧的凑"。**

### 3.3 结构校验规则（候选 → 可提交）

按顺序执行，任一失败则 `validation_state = 'rejected'` 并写 `validation_errors`：

| 规则 | 检查 | 失败码 |
|---|---|---|
| R1 来源可核验 | 每条 `source_refs` 必须出现在 `source_manifest_ref.source_revisions` 里且 `content_hash` 一致 | `SOURCE_NOT_IN_MANIFEST` |
| R2 谓词已注册 | `predicate` 在 `predicate_registry`，且 `value` 符合该谓词的值 schema | `UNKNOWN_PREDICATE` / `VALUE_SCHEMA` |
| R3 主体可解析 | `subject_id` 在 `entity_registry`（或别名唯一命中） | `UNRESOLVED_ENTITY` |
| R4 时钟合法 | `clock_id` 存在，`tick` ∈ `[tick_min, tick_max]` | `CLOCK_OUT_OF_RANGE` |
| R5 区间合法 | `valid_from < valid_to`（若 `valid_to` 非空） | `BAD_INTERVAL` |
| R6 有证据跨度 | `confidence >= kind_min_confidence` 且至少一条 `source_ref` 带 span（剧本行/镜头 id） | `INSUFFICIENT_EVIDENCE` |
| R7 无幻觉 ID | 所有出现的 id（entity、event、thread）都能解析 | `DANGLING_REF` |
| R8 知情完整 | `kind=knowledge_state` 时 `proposition_claim_id` 能解析 | `UNRESOLVED_PROPOSITION` |

`predicate_registry` 是代码里的常量表（schema 名 + 值 JSON Schema + 基数），随服务一起发布版本化。**新增谓词要发版，不能被抽取器凭空造出来。**

### 3.4 冲突预检（提交前）

| 规则 | 判定 | 处理 |
|---|---|---|
| C1 区间值冲突 | 同 `claim_id`，有效区间重叠，值不同 | 必须带 `supersedes`（指向被替代 revision），否则拒 |
| C2 知情矛盾 | 同 `(knower, proposition)` 同区间同时 `knows` 与 `unaware` | 拒并转人工 |
| C3 唯一性违反 | `cardinality=1` 的谓词（如 `held_by`）出现两个不同值且区间重叠 | 拒第二个（除非带 `supersedes`） |
| C4 无事件复活 | 角色/道具在 `dead`/`destroyed` 状态下重新出现且无 `resurrection` 事件 | 拒并转人工 |
| C5 伏笔双回收 | `plot_thread` 已在区间内 `resolved`，再来一条 `resolved` | 拒并转人工 |
| C6 因果倒置 | `story_event.causes` 指向的事件 tick 晚于本事件 | 拒 |

自动接受（`acceptance_mode=auto_if_qc_pass`）也**不能**绕过 C1–C6：这些是结构规则，不是质量判断。策略只决定"拒了之后落到哪"（自动重抽 / 人审 / 标缺）。

### 3.5 原子提交

一个数据库事务内完成：

```sql
BEGIN;
-- 1) 串行化：锁定分支 head（series+branch 粒度）
SELECT head_commit_sequence, head_checkpoint_hash
  FROM story_memory.canon_branch
 WHERE tenant_id=$1 AND series_id=$2 AND branch_id=$3
 FOR UPDATE;

-- 2) CAS：期望 checkpoint 不符 → ROLLBACK，返回 409 CHECKPOINT_CONFLICT
--    (expected_checkpoint 为 commit_sequence；可选校验 content_hash)
-- 3) 插入 memory_commit (seq = head+1, checkpoint_hash 按 §1.3 计算)
-- 4) 插入 claim_revision（status='accepted'），把被 supersede 的旧 revision 置 'superseded'
-- 5) upsert claim（新建 claim 才插）
-- 6) 刷新 canon_effective_claim（本分支）
-- 7) 刷新类型化投影（knowledge_state 等）
-- 8) 插入 commit_manifest_binding
-- 9) 更新 canon_branch.head_* 
-- 10) 同事务写 outbox_event（memory.canon.committed）
COMMIT;
```

Outbox 与本地事实同事务（docs/04 §4），由 publisher 至少一次投递。重复的 `snapshot.manifest.committed` 事件由 §1.3 的 `memory_commit_manifest_once` 唯一索引兜底 —— 第二次插入直接冲突，消费逻辑返回"已处理"并 ack，不重复生成事实。

### 3.6 提交前实时复查（不可跳过）

对着 [AGENTS.md](../AGENTS.md) 的硬约束：**"审核已通过"不是放行依据。** 消费 `snapshot.manifest.committed` 后、写 commit 前，必须实时复查：

1. manifest 在 S08 仍是 accepted pointer（回源查询，不是信事件）；
2. 来源脚本的 `content_hash` 仍与 manifest 一致（防"事件之后又被改"）；
3. 该 branch 未处于 `frozen`（冻结用于灾备切主/审计保全）；
4. 租户 kill switch 未对该 series 生效。

任一项失败 → 不提交，写 `commit_blocked` 审计记录，返回 `BLOCKED`。**宁可 checkpoint 不推进、下游等待，也不猜。**

### 3.7 发布事件

`memory.canon.committed` 的 `data` 至少包含：

```json
{
  "commit_sequence": 27,
  "checkpoint_hash": "hex...",
  "commit_kind": "extract",
  "previous_sequence": 26,
  "changed_claim_refs": [{"service_id":"S16","id":"claim_key_owner","revision":2,"content_hash":"..."}],
  "superseded_claim_refs": [{"service_id":"S16","id":"claim_key_owner","revision":1,"content_hash":"..."}],
  "changed_subject_ids": ["prop_key", "char_linwen"],
  "affected_story_window": {"clock_id":"story_main","from_tick":2000,"to_tick":2200},
  "source_manifest_ref": {"id":"manifest_ep2","revision":8,"content_hash":"..."},
  "episode_id": "ep002",
  "episode_order": 2
}
```

`affected_story_window` 与 `changed_subject_ids` 是给 S09 的**种子**，不是影响闭包本身。S16 不计算闭包 —— 闭包归 S09（见 §5）。

---

## 4. 分支与合并

### 4.1 分叉

```
POST /branches  {branch_id, from_branch_id, from_commit_sequence|null}
```
- `from_commit_sequence` 为空 = 父分支当前 head。
- 新分支写 `canon_branch` 一行 + `branch_ancestry` 展开行 + 一条 `commit_kind='branch_init'` 的提交（`claim_count=0`，checkpoint 链起始指向父 checkpoint）。
- 之后所有写入只落新分支，正史 `canon_effective_claim` 不变。

### 4.2 合并

合并 = 在目标分支上生成一个 `commit_kind='merge'` 的提交，`merge_source_refs` 记录源分支与序列。**合并从不删除历史。**

冲突分类（`merge_conflict` 表，每次合并一个 batch）：

| 类别 | 判定 | 默认裁决 |
|---|---|---|
| `VALUE_CONFLICT` | 同 `claim_id`，区间重叠，值不同 | `require_human` |
| `HOLDER_CONFLICT` | 不同 `claim_id` 但同 `(subject,predicate)` 基数=1，值不同 | `require_human` |
| `EVENT_CONFLICT` | 同 `exclusive_group` 的两个事件（如两种死亡方式） | `require_human` |
| `CAUSAL_BREAK` | 目标分支中被替代的事件仍被已接受事件的 `causes` 引用 | `require_human` |
| `EPISTEMIC_CONFLICT` | 同 `(knower,proposition)` 同区间 `knows` vs `unaware` | `require_human` |
| `THREAD_CONFLICT` | 同一伏笔两侧都以不同 `resolved_by_event` 回收 | `require_human` |
| `IDENTITY_DRIFT` | 同一 story_event 两侧 participants 不同且无 supersedes | `warn` |
| `BENIGN_ADD` | 仅一侧新增，无重叠 | `auto_merge` |

**只有 `BENIGN_ADD` 和显式带 `supersedes` 证据的 `VALUE_CONFLICT` 可以自动合并。** 其余一律产出冲突清单交 S40 裁决（`POST /merge-decisions` 由 S40 决定写回，S16 只接受决定，不判断谁对）。合并预览（`branch_kind=merge_preview`）可以随便跑，不落正史。

裁决记录：`merge_decision(conflict_id, decided_by, decision, rationale, decided_at, decision_ref)`。合并提交时把每条裁决写进 `commit_kind='merge'` 的 `actor_principal` 与审计。

### 4.3 回滚

```
POST /memory-commits  {commit_kind:"revert", revert_of_sequence: N, expected_checkpoint: head}
```
- 不回退 head，不删除任何行。
- 为 `revert_of_sequence = N` 中被改变的所有 claim，生成**新 revision**：值恢复成 N 之前生效的值，`status='accepted'`，`supersedes` 指向 N 产生的 revision。
- 新增一条 `commit_kind='revert'` 的提交，`checkpoint_hash` 正常链下去。
- 结果：历史里能同时看到"改过"和"改回来了"，时间线不断裂（E10 回滚留历史）。

---

## 5. 失效传播与边界

改前集的核心难点不是"知道要失效"，是**算出失效边界且不越权**。

### 5.1 职责切分

| 服务 | 做什么 | 不做什么 |
|---|---|---|
| S16 | 发布 `changed_subject_ids` + `affected_story_window` + `superseded_claim_refs`；维护 `commit_manifest_binding` | 不算依赖闭包，不决定哪些集要重做 |
| S09 | 沿 claim → context_pack → script → shot → artifact 求闭包，出 impact plan | 不改任何事实 |
| S07 | 组织 ChangePlan，区分"必须改剧情/仅重做QC/可复用资产" | 不改 checkpoint |
| S08 | CAS 提交 manifest 与新 epoch | 不改记忆 |

S16 一旦开始计算"哪些集要重做"，就变成了第二个影响分析权威 —— 这是 AGENTS.md 明令禁止的。

### 5.2 记忆侧失效边界（S16 自己的算法）

对一次提交中每个改变/废弃的 claim C：

```
影响起点 episode 集合 =
  A. 故事窗口重叠：{ E | E.clock_id = C.clock_id
                        AND [E.tick_start, E.tick_end) ∩ [C.valid_from, C.valid_to∪{∞}) ≠ ∅ }
  B. 实体引用：    { E | E.episode_order >= order(E0)
                        AND (C.subject_id ∈ E.touched_entities
                             或 C 的 object 实体 ∈ E.touched_entities) }
  C. 揭示变化：    { E | E.episode_order = C.audience_reveal_episode_order }
      其中 E0 是产生 C 的那一集。
  
失效下界 B(C) = min( A ∪ B ∪ C 的 episode_order )
```

`touched_entities` 是 S16 从已接受剧本的 breakdown 里记的（`episode_entity_touch` 表，只存集合，不存内容）。

### 5.3 "已交付集不悄悄重写"

```
若 episode_order < B(C)：                    → 不失效，保持原 checkpoint 绑定
若 episode_order >= B(C) 且已交付：          → 标记 memory_stale=true，等 S07 的迁移决定
若 episode_order >= B(C) 且未交付/未生成：   → 正常失效，下次生成用新 checkpoint
```

已交付集的 `commit_manifest_binding` 永远指向它当时的 checkpoint。回查"这一版成片基于哪个正史"永远有答案。要迁移就必须显式走 ChangePlan，不存在隐式重写。

### 5.4 下游行为

- 未生成的下游节点：`required_canon_checkpoint` 指向新 checkpoint，未达到则 `WAITING_MEMORY`。
- 已进入生成队列但未提交的：不自动取消，等其 manifest 引用判定；若引用的 checkpoint 已被 supersede，结果标 `historical`，不进最终交付。

---

## 6. 查询 API

`POST /memory-queries`

```json
{
  "tenant_id": "derived",
  "series_id": "series_demo",
  "branch_id": "canon",
  "minimum_checkpoint": 27,
  "as_of_commit_sequence": null,
  "story_clock_id": "story_main",
  "story_from_tick": 2000,
  "story_to_tick": 2001,
  "perspective": {"mode": "character", "character_ids": ["char_linlan"],
                  "audience_as_of_episode_order": 3},
  "entity_ids": ["prop_key", "char_chenmo"],
  "kinds": ["entity_state", "knowledge_state", "plot_thread"],
  "predicates": ["held_by", "belief_about"],
  "include_superseded": false
}
```

响应：

```json
{
  "checkpoint": 27,
  "checkpoint_hash": "hex...",
  "claims": [ ... memory-claim.v2 对象 ... ],
  "withheld_claim_ids": ["claim_x"],        // 因知情/时间/集可见性被过滤
  "existence_checks": [                      // 显式"不存在"的证据，供调用方区分"没查到"与"确实没有"
    {"predicate": "held_by", "subject_id": "prop_knife", "found": false}
  ],
  "unresolved_conflicts": ["mc_12"]
}
```

语义要点：

1. **`minimum_checkpoint` 是硬门。** 若本分支 head < 它 → `409 MEMORY_WATERMARK_BEHIND {current_checkpoint}`。**绝不降级返回旧数据。**
2. **`withheld_claim_ids` 必须返回。** 调用方（S18）据此生成 `prohibited_assumptions`；只返回过滤后的结果而不告诉调用方"滤掉了什么"，等于让模型自由脑补。
3. **`existence_checks` 解决"检索没找到 ≠ 不存在"。** 对调用方声明的每个必需谓词，S16 显式回答 found/not-found，且带 checkpoint。这样"没有"是一个有出处的结论。
4. 按 `character` 视角查询时，闭世界默认：
   ```
   可见(C) = ∃ knowledge_state(knower ∈ character_ids, proposition = C.claim_id,
                               belief_type ∈ {knows, believes, uncertain},
                               tick 覆盖查询点)
              OR ∃ world_rule(publicly_known = true) 覆盖 C
              OR C.kind ∈ {world_rule}                 -- 世界规则本身是公共的
   ```
   默认为不可见。`unaware` 是显式的"确认不知道"，比"没记录"更强，两者都导致不可见，但 `unaware` 能生成更精确的禁则文案。

---

## 7. 与其他服务的契约

| 对方 | 方向 | 内容 | 禁止 |
|---|---|---|---|
| S08 | 订阅 | `snapshot.manifest.committed` | 不写 S08；不自行认定 manifest 已接受 |
| S08 | 查询 | 回源确认 accepted pointer | 不缓存"已接受"状态超过单次提交 |
| S19 | 调用 | 抽取 Agent 任务（经 S26 准入） | 不让 Agent 直接写表 |
| S18 | 发布/查询 | `memory.canon.committed`；`POST /memory-queries` | S18 不存事实副本 |
| S09 | 发布 | `memory.canon.committed` 作为依赖种子 | S16 不算闭包 |
| S10 | 引用 | 只引用 `entity_id`，不存可视化变体 | 不复制 canonical 资产字段 |
| S07 | 查询 | `commit_manifest_binding`、失效下界 | 不改接受指针 |

---

## 8. 可观测与运维

指标（Prometheus 命名）：
- `s16_commit_total{kind,result}`、`s16_commit_latency_seconds`
- `s16_checkpoint_lag_seconds`（head 提交到事件发布）
- `s16_candidate_total{validation_state}`、`s16_candidate_promotion_ratio`
- `s16_conflict_total{rule}`、`s16_merge_conflict_total{class}`
- `s16_query_withheld_ratio`（被知情过滤挡掉的比例 —— 异常升高可能意味着抽取过度）
- `s16_idempotency_conflict_total`

告警：
- checkpoint 落后超过 5 分钟 → page（下游全体 `WAITING_MEMORY`）
- `s16_commit_total{result="checkpoint_conflict"}` 突增 → 并发写入热点或上游重放
- `s16_query_withheld_ratio` 单集骤降到 0 → 知情过滤可能失效，立即人工核查

runbook：
- `verify-checkpoint-chain`：从 genesis 重算每个 commit 的 `checkpoint_hash`，与存储值比对。
- `rebuild-effective-view`：从 `memory_commit` + `claim_revision` 重建 `canon_effective_claim` 与类型化投影（纯可重建，不碰事实）。
- `branch-freeze`：灾备切主前冻结分支写入，`execution_fence` 提升后恢复。
- `candidate-gc`：清理过期候选，永不触碰 `claim_revision`。

---

## 9. 测试清单

### 9.1 E08 · 改前集钥匙归属

| 用例 | 期望 |
|---|---|
| E08-1 第 2 集提交 `held_by(prop_key)=char_chenmo` | 新 revision=2，旧 revision=1 → `superseded`；commit seq 27；`checkpoint_hash` 可重算 |
| E08-2 已交付第 3 集的 `commit_manifest_binding` | 仍指向旧 checkpoint；`memory_stale=true` |
| E08-3 第 2 集改为"藏起钥匙"（无持有者） | `held_by` 的 `valid_to_tick` 闭合；新增 `stored_at`；C3 不触发 |
| E08-4 未带 supersedes 的冲突值提交 | 拒绝，`CLAIM_CONFLICT`，正史不变 |
| E08-5 影响下界计算 | 只含故事窗口重叠且 order ≥ E0 的集；不含已交付且无迁移决定的历史集 |
| E08-6 S09 收到事件后用 `changed_subject_ids` 求闭包 | S16 不输出闭包本身（断言事件 payload 无下游集列表） |

### 9.2 E09 · 闪回 / 观众先知 / 角色不知

| 用例 | 期望 |
|---|---|
| E09-1 第 7 集闪回，故事窗口 tick 100–200，播出序 7 | `episode_order=7`，`clock_id=story_main`，`tick_start=100` |
| E09-2 查询第 3 集上下文 | 第 7 集的事实不可见（`audience_reveal_episode_order=7 > 3`） |
| E09-3 `knowledge_state(char_linlan, claim_chenmo_identity) = unaware` | 角色视角查询返回空，且进入 `withheld_claim_ids` |
| E09-4 同一命题 `audience_reveal_episode_order=2` | 观众视角在第 3 集可见，角色视角仍不可见 |
| E09-5 `publicly_known=true` 的世界规则 | 角色视角可见（闭世界的唯一默认放行口） |
| E09-6 无 `unaware` 记录、仅有 `believes` 情形 | 不可见；禁则文案用 `believes` 模板而非 `unaware` |
| E09-7 时间跳跃：第 5 集跳 3 年后 | 主时钟 tick 直接跃迁；旧区间 `valid_to` 显式闭合，不靠"取最新" |

### 9.3 E10 · 分支与候选抽取

| 用例 | 期望 |
|---|---|
| E10-1 分叉实验分支并提交 | 正史 `canon_effective_claim` 逐行不变（快照比对） |
| E10-2 候选未接受时查询正史 | 候选不可见；`claim_revision` 中无该 `claim_id` |
| E10-3 候选缺来源 / 主体无法解析 | `rejected`，`SOURCE_NOT_IN_MANIFEST` / `UNRESOLVED_ENTITY` |
| E10-4 合并：仅一侧新增 | `BENIGN_ADD` 自动合并 |
| E10-5 合并：`held_by` 两侧不同 | `HOLDER_CONFLICT` → `require_human`，不自动选边 |
| E10-6 合并：两侧都以不同事件回收同一伏笔 | `THREAD_CONFLICT` → `require_human` |
| E10-7 回滚 commit 27 | 新增 commit 28，`commit_kind=revert`；值恢复；27 与 2 均仍在历史中 |
| E10-8 回滚后重算 checkpoint 链 | 链完整，`revert_of_sequence=27` 可追溯 |

### 9.4 E11 · 索引落后（S16 侧）

| 用例 | 期望 |
|---|---|
| E11-1 head=27，外部查询 `minimum_checkpoint=30` | `409 MEMORY_WATERMARK_BEHIND{current:27}`，不返回 27 的数据 |
| E11-2 下游 claim 不存在 | `existence_checks.found=false` 且带 checkpoint，不是空数组 |
| E11-3 事件重放（同 event_id 两次） | inbox 去重；`memory_commit` 不新增 |

### 9.5 通用分布式验收

| 用例 | 期望 |
|---|---|
| 同 `command_id` 同 body 重投 | 返回首次结果，无新 commit |
| 同 `command_id` 不同 body | `409 IDEMPOTENCY_KEY_REUSED` |
| 并发两写同 `expected_checkpoint` | 一个成功，一个 `409 CHECKPOINT_CONFLICT`；无部分写入 |
| 提交中途崩溃（Outbox 已写未发布） | 重启后 publisher 补发；事实与事件一致（E13） |
| 跨租户 `series_id` 访问 | `404`（不是 403，不泄漏存在性，E28） |
| 乱序 manifest 事件 | 按 `aggregate_sequence` 缓冲；缺口超时回源，不无限等待 |
| 事件倒退 | 终态不被旧事件覆盖 |

---

## 10. 反模式清单（写了就是错）

1. ❌ 用 `created_at` / `MAX(revision)` 回答"当前状态" → 必须 `checkpoint + story tick` 双条件。
2. ❌ 给候选加 `status='accepted'` 就当地事实 → 候选表根本没有提交字段。
3. ❌ 在 `knowledge_state` 之外再存一份"角色知道什么"的物化表给 S17 用 → S17 只能查 S16。
4. ❌ S16 计算"哪些集受影响"并直接通知 S07 → 闭包归 S09。
5. ❌ 回滚用 `DELETE` 或 `UPDATE ... status='deleted'` 抹掉 revision → 只能追加。
6. ❌ checkpoint 落后时返回旧数据 + `stale` 标记让调用方自己判断 → 必须 409，调用方无法判断就不会判断。
7. ❌ 把 `depends_on: S19` 理解成 S16 可以直接调模型 → 抽取必须经 S26 准入。
8. ❌ 跨库 JOIN S08 的 manifest 表 → 只能通过 API 或事件。

---

## 11. 实现登记（本轮）

实现落在 `services/story-memory/`：`src/story_memory/{schema,predicates,service}.py`、
`api/app.py`、`tests/test_story_memory.py`（37 个用例）。catalog 的 `maturity` 已从
`design_only` 改为 `implemented`（「已实现最小内核，尚未部署」）。

### 11.1 已实现并有用例钉住

- §1.1–§1.11 全部 DDL（Postgres → SQLite 翻译，两条 `CHECK` 原样保留）。
- §3.5 checkpoint 链、§3.6 四项实时复查、§3.3 原子提交顺序（含同事务 outbox）。
- R1–R8 结构校验、C1–C6 冲突预检、§4.2 八类合并冲突。
- §4.3 回滚（只追加）、§6 三轴查询与硬门 `MEMORY_WATERBEHIND`。
- §9.1–§9.4 的 E08/E09/E10/E11 用例，以及 E05 幂等与跨租户拒绝。

### 11.2 本轮修掉的实现缺陷

| 缺陷 | 症状 | 修法 |
|---|---|---|
| `_load_revision` 读 `claim_revision.kind` | 该列不存在（`kind` 在 `claim` 上） | JOIN `claim` |
| 合并产生的 revision 没有 `accepted_manifest_ref` | 撞 `accepted_needs_evidence` | 用源分支 head 作为证据 |
| `_revert` 只筛 `status='accepted'` | 要回退的那条恰已被置成 `superseded`，一条也没写 | 去掉该过滤 |
| `_revert` 的 prior 只按 `revision DESC` 跨分支取 | 别分支编号更大的 revision 被当成本分支的上一版 | 限定 `branch_id`，按 `commit_sequence` |
| `_check_causality` 用全库 `MAX(revision)` 读被引用事件 | 别的分支能让本分支的校验换结果 | 改读本分支 `canon_effective_claim` |
| `_visible_to_character` 读 claim 自己的 `epistemic_scope` 放行 | 一条事实可自证"谁能看见我"，等于第二个可见性权威 | 删掉该放行口，只留 §6.4 的两个 |
| IDENTITY_DRIFT 读 `fact["participants"]` | 该键不存在，永远不漂移 | 从 `value` 取；并要求它是 `participants` 谓词 |
| `causes` / `resurrection` / `resolved_by_event` 登记为 `entity_id` | §1.6 里它们指向 claim id，合法引用被判悬空 | 新增 `claim_ref` 值域，查 `claim` 表存在性 |
| story_event 的 participants 变更同时报 `VALUE_CONFLICT` | `warn` 类的 IDENTITY_DRIFT 永远观察不到 | 该谓词走专门那条 |

### 11.3 范围裁剪（未实现，交付前须处理）

- **S08 / S01 接线未做**：`manifest_authority` 与 `kill_switch` 只接受注入的
  callback，默认不注入即不放行（§3.6）。接真实服务不在本轮范围。
- **S09 / S07 发布未做**：只写 `outbox` 行，没有真实 publisher；`changed_subject_ids`
  与 `affected_story_window` 作为失效种子随事件带出，闭包不在 S16。
- **`POST /merge-decisions` 未做**：§4.2 说由 S40 裁决、S16 只接受决定，接口未实现，
  合并目前一律产出冲突清单并停在 `require_human`。
- **`claim_candidate` 增加一列 `proposition_claim_id`**：§1.6 的 `knowledge_state`
  必须指明"对哪个命题"，原 §1.7 DDL 没有落点，R8 无从校验。属与原方案的结构性差异。

### 11.4 与 openapi 的契约冲突（待 owner 裁决，未自行选边）

`contracts/openapi/story-memory.v2.yaml` 与本文档不一致，实现按本文档写，响应体额外
带 `x-contract-gaps` 说明：

1. `GET /series/{series_id}/memory/query` 响应 `additionalProperties: false` 且只允许
   `{checkpoint, claims}`；§6 承诺 `checkpoint_hash` / `withheld_claim_ids` /
   `existence_checks` / `unresolved_conflicts`。
2. `POST /memory/commits` 的 `claim_refs` items 是裸 `type: object`，未约束
   `{service_id,id,revision,content_hash}`；body 也没有 `branch_id`，而提交必须有分支。
3. §4.1 的 `POST /branches` 在 openapi 中不存在。
