# S03 · 项目与剧集目录 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。SLO 与容量均为 docs/03 的规划值，**未实测**。

上游依据：[docs/01 §1/§3](../docs/01-enterprise-architecture.md)（四种顺序分开记录、唯一权威表）、[docs/04](../docs/04-data-consistency.md)（command envelope、幂等、Outbox）、[docs/06 §1/§4](../docs/06-automation-governance.md)（档位与归档）、[docs/07 §1](../docs/07-production-workflows.md)、[docs/16 §5](../docs/16-migration-acceptance.md)（E 矩阵）、[S03 规格](../docs/design-package/services/S03-project-catalog.md)。

配套：[S04](S04-narrative.md) / [S05](S05-breakdown.md) / [S06](S06-storyboard.md)、[S08](S08-revision-snapshot.md)、[S02](S02-governance-policy.md)、[S16](S16-story-memory.md)、[S21](S21-orchestration.md)。

---

## 0. 一句话边界

**S03 是"这个项目、这部剧、这一集到底叫什么 ID、属于谁、规格是什么、按什么顺序发行"的唯一权威。它不是任何内容、任何生产状态、任何资产、任何正史的权威。**

S03 存在的原因：整条生产链上每个服务都要引用 `project_id / series_id / episode_id`，而这三个 ID 必须在集序重排、改档期、跨集改稿之后**保持不变**。如果没有一个服务专门持有这份稳定身份，S05 分场时用的是"第 3 集"，S06 分镜时用的是"发行序列里第 3 集"，S09 建依赖边时又用了另一套序号——跨集依赖会在第一次调档期时全部错位。

### 0.1 写什么 / 不写什么

| 事实 | 唯一写者 | S03 的角色 |
|---|---|---|
| `project_id / series_id / episode_id` 稳定 ID 与生命周期 | **S03** | 唯一写者 |
| 项目/剧集/单集的规格（画幅、时长目标、语言、交付格式） | **S03** | 唯一写者 |
| 企业业务目标（目标集数、预算包络引用、里程碑） | **S03** | 唯一写者 |
| 成员引用（谁在哪个项目里担任什么角色） | **S03** | 唯一写者（角色定义在 S01） |
| 发行顺序、拍摄顺序（**作为各自独立的序列表**） | **S03** | 唯一写者 |
| 故事时间（story clock / tick 区间） | **S16** | 只存 `story_window_ref` 指针，不解释 |
| 模型执行顺序（node 拓扑序） | **S08 / S21** | 完全不涉及 |
| 场次（Scene）稳定 ID 与分场内容 | **S05** | 只存 `scene_ids` 的只读投影（可重建） |
| 镜头（Shot）稳定 ID 与分镜内容 | **S06** | 完全不涉及（连投影都不建） |
| 剧本内容与版本 | **S04** | 完全不涉及 |
| 当前 accepted manifest、epoch、锁 | **S08** | 完全不涉及 |
| 自动化档位、门、预算权限 | **S02** | 完全不涉及 |
| 用户身份、角色、授权 | **S01** | 只存 `principal_id` 引用，不判定 |
| 预算余额 | **S23** | 只存 `budget_scope_id` 引用，不判定 |

判据四问：

- "这个 ID 还在不在、属于哪个项目" → S03 回答。
- "这一集第几个播出、第几场第几镜" → **播出顺序 S03 回答；场次序号是 S05 的 `display_locator`；镜头序号是 S06 的 `shot_order`**。
- "这一集现在能不能开 Run" → S03 只回答 `lifecycle=archived` 这个事实；**能不能开 Run 由 S02/S21 判定**（见 §3.3 与 C03-4）。
- "这一集的正史 checkpoint 是多少" → S16 回答。

### 0.2 五个明确不做

1. **不铸造场次 ID、不铸造镜头 ID。** catalog 原文写的是"场次ID由S05负责"；镜头 ID 归 S06（见 §10 C03-2 的核实记录）。S03 一旦开始分配 `scene_order`/`shot_order`，就会成为第二个序号权威，改一次分场就要动目录版本。
2. **不持有 accepted 指针。** 剧本接不接受、分镜接不接受，指针在 S08。S03 不能因为"这一集剧本提交了"就把 episode 标成 `accepted`；那会让 S16 的写入路径失去唯一触发源。
3. **不解释故事时间。** 闪回集的 `episode_order` 是 7，但故事窗口可能是主时钟的 100–200（[S16 §1.1](S16-story-memory.md)）。S03 只存 `story_window_ref` 指针，**绝不按 `episode_order` 推断故事先后**。
4. **不做成员权限判定。** `member` 表回答"这个人挂在这个项目里、担任这个角色"，不回答"这个角色现在能不能读这个镜头"。后者是 S01 的授权上下文，且必须每次实时取。
5. **不写归档后的清理动作。** 归档是状态迁移；删数据、清媒体、撤权限是 S28/S45 与保留合同的事。S03 只发 `lifecycle` 变化，不级联删任何下游数据。

---

## 1. 关键区分（最容易写错的四处）

### 1.1 稳定 ID 与显示序号

这是本服务存在的全部理由，也是一切跨集 bug 的来源。

| | 稳定 ID | 显示序号 |
|---|---|---|
| 例子 | `ep_01J8ZQ...` | `display_number = 3` |
| 重排集序后 | **不变** | 可能变成 5 |
| 谁引用 | 所有下游服务、manifest、S16 claim、S09 依赖边 | 只有 UI 与字幕/交付封面 |
| 能否当外键 | 能 | **不能** |
| 数据库约束 | 主键 | `UNIQUE(series_id, order_kind, ordinal)` |

`ShotSpec.display_locator` 里已经有 `{episode, scene, shot}` 三个整数（[shot-spec.v1](../contracts/domain-v1/shot-spec.v1.schema.json)）。这三个整数**全部是显示值**，任何业务判断都不能用它们。E03「第 3 场第 2 镜改仰拍」的第一步是"S07 用明确集上下文解析稳定 `shot_id`"（docs/07 §2）——解析结果必须是 ID，不是 `(3,2)`。

### 1.2 四种顺序，四张表，绝不合并

docs/01 §1 原句："**拍摄顺序、故事时间、剧集发行顺序、模型执行顺序分别记录，不能用同一序号代替。**"

实现上这四种顺序的**拥有者不同**，S03 只拥有其中两个：

| 顺序 | 拥有者 | 存放在哪 | S03 的角色 |
|---|---|---|---|
| **发行顺序**（第几集播出） | S03 | `episode_order(order_kind='release')` | 唯一写者 |
| **拍摄顺序**（第几批拍） | S03 | `episode_order(order_kind='shooting')` | 唯一写者 |
| **故事时间** | S16 | `story_memory.episode_story_window` | 存 `story_window_ref` 指针 |
| **模型执行顺序** | S08/S21 | manifest `node_states` 顺序 + Temporal 拓扑 | 完全不涉及 |

为什么拍摄顺序归 S03 而不归 S16：拍摄顺序是**生产排期**，与故事无关。一个闪回集可以第一个拍。它影响的是 S22 的配额包络与 S21 的批量调度粒度，属于项目级计划事实，不是叙事事实。

为什么四张表而不是一行的四个整数列：一行四列无法表达"这批顺序还没排"（`NULL` 会让所有排序查询都要写 `COALESCE`），也无法表达"同一集在两个发行窗口各有一个序号"（重播/分季）。更重要的是：**合并成一行会让一次集序重排必须改动一个跨集共享的行**，而独立表可以把重排隔离在这一集的排序行上，`episode` 行完全不动。

### 1.3 `episode` 与 `episode_order`

- `episode` 是**身份**：一个单集存在过，就永远有一条记录（直到被真正删除，且删除要过保留合同）。
- `episode_order` 是**排期**：可以增、删、改序，可以有多套（发行 / 拍摄 / 重播窗口）。

E02「三集连续生产」与 E09「闪回与观众先知」都依赖这个分离。如果 `episode` 行里带 `number`，重排就会改写身份行，S09 建好的 `SCENE_DERIVED_FROM_SCRIPT` 边会因为 `episode_id` 没变但语义变了而全部失效。

**重排的原子性**：一次重排必须整体生效或整体不生效。`order_kind` 内的 `ordinal` 唯一性用 `DEFERRABLE INITIALLY DEFERRED` 的唯一约束表达，允许事务内先临时重复。不能用"先删后插"——中间态会被并发的读看到空集。

### 1.4 项目规格与集规格：继承是**物化**的

`project_spec` 是项目默认，`series_spec` 是剧集覆盖，`episode_spec` 是单集覆盖。三层合并结果**在写入时就物化**到 `episode_spec_effective`，不是一个读时 join。

理由：S06 的 `ShotSpec.output_spec`（画幅/宽高/fps）与 S20 的模板 `compatible_schema` 都必须在一集之内保持稳定。如果读时合并，项目经理改一次项目默认画幅，所有在跑的集会在下一次编译时悄悄换规格 —— 这与 [S10 §1.2](S10-asset-catalog.md)"没有运行时展开"是同一条原则。

物化时机：`episode` 创建时算一次；项目/剧集规格变更时**不自动重算**已有集，而是发 `catalog.spec.changed` 本地事件，由 S07 决定是否形成一次显式变更计划。

---

## 2. 数据模型

Postgres，schema `project_catalog`。所有表带 `tenant_id`，所有主键/唯一约束含 `tenant_id`（E28：跨租户不得通过唯一冲突探测存在性）。启用 RLS 作为第二道保护（docs/14 §2）。ID 一律 ULID 文本，前缀区分类型：`prj_ / ser_ / ep_ / ord_ / mem_ / spc_`。

### 2.1 项目

```sql
CREATE TABLE project_catalog.project (
  tenant_id        text NOT NULL,
  project_id       text NOT NULL,             -- 'prj_01J...'，永不复用
  slug             text NOT NULL,             -- 人类可读，可改
  display_name     text NOT NULL,
  lifecycle        text NOT NULL,             -- draft|active|suspended|archived
  org_unit_id      text NOT NULL,             -- S01 的组织单元引用
  created_by       jsonb NOT NULL,            -- {principal_id, principal_type}
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  head_revision    integer NOT NULL DEFAULT 1,  -- 元数据 CAS 用
  archived_at      timestamptz,
  archive_reason   text,
  PRIMARY KEY (tenant_id, project_id),
  UNIQUE (tenant_id, slug),
  CONSTRAINT project_lifecycle_chk
    CHECK (lifecycle IN ('draft','active','suspended','archived')),
  CONSTRAINT project_archived_chk
    CHECK ((lifecycle = 'archived') = (archived_at IS NOT NULL))
);
CREATE INDEX project_by_org
  ON project_catalog.project (tenant_id, org_unit_id, lifecycle);
```

`slug` 可改，`project_id` 不可改。`slug` 的唯一性只在租户内，且**不参与任何跨服务引用** —— 它只用于 URL 与人眼。

`head_revision` 是元数据 CAS 的乐观锁，与 S08 的 manifest revision **完全无关**，不要混用（见 §6.2）。

### 2.2 剧集

```sql
CREATE TABLE project_catalog.series (
  tenant_id        text NOT NULL,
  series_id        text NOT NULL,             -- 'ser_01J...'
  project_id       text NOT NULL,
  title            text NOT NULL,
  series_kind      text NOT NULL,             -- drama|comic_drama|anthology
  target_episodes  integer,                   -- 计划集数（业务目标，非事实）
  lifecycle        text NOT NULL,             -- draft|active|suspended|archived
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  head_revision    integer NOT NULL DEFAULT 1,
  branch_default   text NOT NULL DEFAULT 'canon',  -- 建议值，S08 才是分支权威
  PRIMARY KEY (tenant_id, series_id),
  FOREIGN KEY (tenant_id, project_id)
    REFERENCES project_catalog.project (tenant_id, project_id),
  UNIQUE (tenant_id, project_id, title),
  CONSTRAINT series_lifecycle_chk
    CHECK (lifecycle IN ('draft','active','suspended','archived')),
  CONSTRAINT series_target_chk CHECK (target_episodes IS NULL OR target_episodes > 0)
);
CREATE INDEX series_by_project
  ON project_catalog.series (tenant_id, project_id, lifecycle);
```

`branch_default` 只是"新建集时建议用哪个分支"的默认值，**不是分支权威**。分支、谱系、merge 状态全在 S16/S08。S03 存这个名字是因为创建集时要初始化一个默认值，而 S08 的 `manifest.branch_id` 是必填字符串；如果 S03 不存，每个调用方都要硬编码 `"canon"`。

### 2.3 单集（身份，不含序号）

```sql
CREATE TABLE project_catalog.episode (
  tenant_id          text NOT NULL,
  episode_id         text NOT NULL,           -- 'ep_01J...'
  series_id          text NOT NULL,
  project_id         text NOT NULL,           -- 冗余，防止跨项目集引用错位
  title              text,
  lifecycle          text NOT NULL,           -- draft|in_production|completed|archived
  story_window_ref   jsonb,                   -- {clock_id, section_hint}，S16 解释
  release_window     tstzrange,               -- 计划播出窗口（业务计划，非事实）
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  head_revision      integer NOT NULL DEFAULT 1,
  archived_at        timestamptz,
  PRIMARY KEY (tenant_id, episode_id),
  FOREIGN KEY (tenant_id, series_id)
    REFERENCES project_catalog.series (tenant_id, series_id),
  CONSTRAINT episode_lifecycle_chk
    CHECK (lifecycle IN ('draft','in_production','completed','archived')),
  CONSTRAINT episode_archive_chk
    CHECK ((lifecycle = 'archived') = (archived_at IS NOT NULL))
);
CREATE INDEX episode_by_series
  ON project_catalog.episode (tenant_id, series_id, lifecycle);
```

**注意这里没有 `episode_number` 列。** 这是刻意的：任何"第几集"都必须从 `episode_order` 查，不允许存在一个"顺手能用"的整数列。一个方便字段会被下游当成权威，这是历史上同类系统最常见的错误。

`project_id` 冗余存一份是为了让"这一集属于哪个项目"可以单表回答，避免跨表 join 才能做租户+项目过滤；它不是第二权威，因为写入时由 `series_id` 派生并在同一事务内校验。

### 2.4 顺序（关键表）

```sql
CREATE TABLE project_catalog.episode_order (
  tenant_id    text NOT NULL,
  order_id     text NOT NULL,                 -- 'ord_01J...'
  series_id    text NOT NULL,
  order_kind   text NOT NULL,                 -- release|shooting|rebroadcast
  window_key   text NOT NULL DEFAULT '',      -- 重播窗口标识；release 用 ''
  ordinal      integer NOT NULL,              -- 1 起，连续且唯一
  episode_id   text NOT NULL,
  assigned_by  jsonb NOT NULL,
  assigned_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, order_id),
  FOREIGN KEY (tenant_id, episode_id)
    REFERENCES project_catalog.episode (tenant_id, episode_id),
  UNIQUE (tenant_id, series_id, order_kind, window_key, ordinal)
    DEFERRABLE INITIALLY DEFERRED,
  UNIQUE (tenant_id, series_id, order_kind, window_key, episode_id),
  CONSTRAINT order_kind_chk CHECK (order_kind IN ('release','shooting','rebroadcast')),
  CONSTRAINT order_ordinal_chk CHECK (ordinal >= 1)
);
CREATE INDEX episode_order_by_episode
  ON project_catalog.episode_order (tenant_id, episode_id, order_kind);
```

两条唯一约束做的是不同的事：

- `(series, order_kind, window, ordinal)` —— 一个位置只能放一集。延迟到事务末检查，允许一次重排事务内部临时撞位。
- `(series, order_kind, window, episode_id)` —— 一集在一个顺序里只能出现一次。**这条不能延迟**：重复出现不是重排的中间态，是数据错误。

`ordinal` 的**连续性**不做 DB 级约束（相邻值之间的大间隔在重排时不可避免）。连续性由重排命令保证：命令接收完整列表，服务端把 ordinal 重写成 `1..N`。

为什么 `window_key` 在 `release` 下是 `''` 而不是 `NULL`：Postgres 的唯一约束把 `NULL` 视为互不相等，用 `NULL` 会让"同一集在 release 出现两次"绕过约束。空串让它参与唯一性。

### 2.5 规格（三层 + 物化）

```sql
CREATE TABLE project_catalog.spec_revision (
  tenant_id      text NOT NULL,
  spec_id        text NOT NULL,               -- 'spc_...'，稳定
  scope_kind     text NOT NULL,               -- project|series|episode
  scope_id       text NOT NULL,
  revision       integer NOT NULL,
  body           jsonb NOT NULL,              -- 见下方字段集
  content_hash   char(64) NOT NULL,           -- sha256(JCS(body))
  author         jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, spec_id, revision),
  UNIQUE (tenant_id, scope_kind, scope_id, content_hash),
  CONSTRAINT spec_scope_chk CHECK (scope_kind IN ('project','series','episode'))
);
CREATE INDEX spec_head
  ON project_catalog.spec_revision (tenant_id, scope_kind, scope_id, revision DESC);

CREATE TABLE project_catalog.episode_spec_effective (
  tenant_id      text NOT NULL,
  episode_id     text NOT NULL,
  derived_from   jsonb NOT NULL,              -- {project_spec:{revision,hash}, series_spec:{...}, episode_spec:{...}}
  body           jsonb NOT NULL,
  content_hash   char(64) NOT NULL,
  materialized_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, episode_id),
  FOREIGN KEY (tenant_id, episode_id)
    REFERENCES project_catalog.episode (tenant_id, episode_id)
);
```

`spec_revision.body` 的字段集（第一版，全部必填，`additionalProperties` 由应用层拒绝未知键）：

```jsonc
{
  "output": {
    "aspect_ratio": "9:16",
    "width": 1080, "height": 1920,
    "fps": 24,
    "max_episode_seconds": 180,
    "min_episode_seconds": 60
  },
  "language": { "primary": "zh-CN", "dub_targets": ["ja-JP","en-US"] },
  "delivery": { "loudness_target_lufs": -16, "color_primaries": "bt709" },
  "default_branch": "canon"
}
```

`output` 这一段与 [shot-spec.v1](../contracts/domain-v1/shot-spec.v1.schema.json) 的 `output_spec` 字段同名同义 —— 这是刻意的：S06 生成 ShotSpec 时用 `episode_spec_effective.output` 作为默认值，两边字段名一致就不会出现"目录说 1080×1920、分镜写 720×1280"这种静默不一致。

### 2.6 业务目标

```sql
CREATE TABLE project_catalog.business_goal (
  tenant_id      text NOT NULL,
  goal_id        text NOT NULL,
  scope_kind     text NOT NULL,               -- project|series
  scope_id       text NOT NULL,
  goal_kind      text NOT NULL,               -- episode_count|milestone|budget_envelope|delivery_date
  target         jsonb NOT NULL,              -- 结构化，不是自由文本
  due_at         timestamptz,
  status         text NOT NULL DEFAULT 'active',  -- active|met|missed|canceled
  source         jsonb NOT NULL,              -- 录入人与依据（合同引用等）
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, goal_id),
  CONSTRAINT goal_status_chk CHECK (status IN ('active','met','missed','canceled'))
);
CREATE INDEX goal_by_scope
  ON project_catalog.business_goal (tenant_id, scope_kind, scope_id, status);
```

`budget_envelope` 只存**引用与上界**（例如 `{"budget_scope_id":"budget_prj_x","cap_micro":50000000}`）；金额的权威在 S23。S03 不校验"实际花费有没有超"，只提供"计划是多少"。

### 2.7 成员

```sql
CREATE TABLE project_catalog.member (
  tenant_id      text NOT NULL,
  member_id      text NOT NULL,
  project_id     text NOT NULL,
  principal_id   text NOT NULL,               -- S01 的主体 ID
  role_key       text NOT NULL,               -- 在租户角色表里的键（S01 定义）
  scope_kind     text NOT NULL DEFAULT 'project', -- project|series|episode
  scope_id       text,                        -- project 时为空
  granted_by     jsonb NOT NULL,
  granted_at     timestamptz NOT NULL DEFAULT now(),
  revoked_at     timestamptz,
  PRIMARY KEY (tenant_id, member_id),
  FOREIGN KEY (tenant_id, project_id)
    REFERENCES project_catalog.project (tenant_id, project_id),
  UNIQUE (tenant_id, project_id, principal_id, role_key, scope_kind,
          COALESCE(scope_id, ''))
);
CREATE INDEX member_by_principal
  ON project_catalog.member (tenant_id, principal_id, revoked_at);
```

**成员表回答"谁被指派了"，不回答"谁现在有权限"。** 读权限时仍然要调 S01 拿授权上下文，因为 S01 还有撤权、委派过期、企业 kill switch 这些 S03 看不到的事实（E27）。S03 的 `member` 只用于：新建集时决定默认审核人、UI 上显示"这个项目有谁"、以及 S45 审计"谁在什么时候被指派"。

### 2.8 基础设施表

```sql
CREATE TABLE project_catalog.idempotency_record (
  tenant_id      text NOT NULL,
  command_id     text NOT NULL,
  request_hash   char(64) NOT NULL,
  operation      text NOT NULL,
  state          text NOT NULL,               -- IN_PROGRESS|DONE
  response_code  integer,
  response_body  jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz,
  PRIMARY KEY (tenant_id, command_id),
  CONSTRAINT idem_state_chk CHECK (state IN ('IN_PROGRESS','DONE'))
);

CREATE TABLE project_catalog.outbox (
  tenant_id        text NOT NULL,
  id               text NOT NULL,
  aggregate_id     text NOT NULL,               -- project_id / series_id / episode_id
  aggregate_seq    bigint NOT NULL,
  event_type       text NOT NULL,
  partition_key    text NOT NULL,             -- tenant_id || ':' || aggregate_id
  payload          jsonb NOT NULL,
  headers          jsonb NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  published_at     timestamptz,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, aggregate_id, aggregate_seq)
);
CREATE INDEX outbox_unpublished
  ON project_catalog.outbox (created_at) WHERE published_at IS NULL;

CREATE TABLE project_catalog.inbox (
  consumer       text NOT NULL,
  event_id       text NOT NULL,
  received_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);

CREATE TABLE project_catalog.projection_watermark (
  tenant_id  text NOT NULL,
  source     text NOT NULL,                   -- 'S05.scenes' / 'S01.access'
  sequence   bigint NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, source)
);
```

### 2.9 场次投影（可重建，非权威）

```sql
CREATE TABLE project_catalog.episode_scene_projection (
  tenant_id     text NOT NULL,
  episode_id    text NOT NULL,
  scene_id      text NOT NULL,                -- S05 铸造
  scene_ordinal integer NOT NULL,             -- S05 的显示序
  breakdown_ref jsonb NOT NULL,               -- {revision,content_hash}
  applied_seq   bigint NOT NULL,              -- S05 事件序列
  PRIMARY KEY (tenant_id, episode_id, scene_id)
);
```

这张表**只用于 UI 树形导航与"这一集有多少场"的统计**。任何业务判断（分镜、依赖边、影响闭包）都必须直接查 S05。投影落后时返回 `stale=true` 与 `projection_watermark`，不能当作"这一集没有场次"。

### 2.10 不建的表

- **不建 `shot` 表。** 镜头 ID 归 S06（见 §10 C03-2）。S03 连投影都不建 —— 一集几百个镜头，目录服务没有理由承担这份数据量与一致性负担。
- **不建 `script` 表。** 剧本版本归 S04。
- **不建 `acceptance` 表。** 接受指针归 S08。
- **不建 `run` 表。** 执行状态归 S21/S08。

---

## 3. 状态机

### 3.1 Project

```
draft ──activate──▶ active ──suspend──▶ suspended ──resume──▶ active
  │                   │                    │
  └──archive──────────┴──archive───────────┴──▶ archived
archived 是终态（对本服务而言）；"取消归档"不在本版范围（见 §11 未决 3）
```

| 迁移 | 守卫 | 副作用 |
|---|---|---|
| `draft→active` | 必须至少有一个 `series`，且 `series.lifecycle ∈ {draft,active}` | 发 `project.episode.created`？**否**（见 §5.3 的事件缺口） |
| `active→suspended` | S02/S01 的 kill switch 是**另一条路径**，此处只处理计划性暂停 | 不停止在途任务；S21 自行判断 |
| `suspended→active` | 无 | 无 |
| `*→archived` | 需 S01 的 `project.admin` 权限 + 归档原因 | 写 `archived_at`；发本地事件；**不级联停 Run**（C03-4） |

`suspended` 与 S01 的租户级 kill switch 不是一回事。前者是项目经理的计划性暂停，后者是平台安全动作。S03 不消费 kill switch 事件，也不因 kill switch 改 `project.lifecycle` —— 那是把两个不同层级的事实混在一起。

### 3.2 Series / Episode

```
Series:  draft ──▶ active ──▶ suspended ──▶ active
                    └────────┬─────────────┴──▶ archived
Episode: draft ──▶ in_production ──▶ completed
            └──────────┴──────────────┴──▶ archived
```

`episode.completed` 的判定条件**只有一条**：这一集有至少一个 S08 accepted 的 master 节点（通过消费 `snapshot.manifest.committed` 里 `accepted_pointers_delta` 与 S02 的 gate 判定）。S03 **不自己判断"做完了"** —— `completed` 是投影状态，事实在 S08。

因此 `Episode: completed` 必须可回退：S08 新建 revision 并把 master 退回未接受时，S03 的投影应回到 `in_production`。这是"终态不倒退"原则的一个**例外**，因为 `completed` 本身不是 S03 的事实，是投影。**这一点必须在代码里写注释**：S03 的 `episode.lifecycle` 不是终态机，只有 `archived` 是。

### 3.3 归档是否阻止新 Run（关键裁决点）

`service.yaml` 的 acceptance 写"归档项目阻止新 Run"。但 S03 没有能力阻止 S21 开 Run —— 它不持有 Run 状态，也不该持有。

本方案的实现方式：

1. S03 提供**事实**：`GET /episodes/{id}` 返回 `lifecycle` 与 `project_lifecycle`（派生）。
2. S21/S26 的准入路径在创建 Run 前查询该事实；`archived` → 拒绝，`reason=PROJECT_ARCHIVED`。
3. S02 的 `PolicyDecision` 里增加一条 `project_active` 检查（[S02 §7.2](S02-governance-policy.md) 的 `checks[]` 已有同构位置）。

也就是说：**"阻止"是调用方的义务，S03 的义务是让这个事实可查、可审计、不滞后。** 这条写进 §10 C03-4，因为它与 `service.yaml` 的措辞不完全一致。

### 3.4 EpisodeOrder 没有状态机

排序行没有 `draft/published`。它要么存在（生效），要么不存在。理由是：一份"草稿排序"和"生效排序"并存时，每个读方都要判断读哪一份，而排序的读取点有十几个（UI、S21 批量、S28 归档命名、S43 交付清单）。两份排序会让其中至少一个读错。

重排的原子性由命令 + `DEFERRABLE` 唯一约束保证，不需要状态机。

---

## 4. 关键流程

### 4.1 创建项目 + 剧 + 首批集（核心写路径）

```
POST /projects
  {command_id, name, org_unit_id, spec:{...}, goals:[...], series:[{title, kind}], episodes:[{title, story_window_ref}]}
```

事务：

1. 校验 `command_id` 幂等（§6.1）。命中 `DONE` → 原样返回。
2. 从 mTLS/签名上下文取 `tenant_id` 与 principal；**不接受 body 里的 `tenant_id`**（AGENTS.md）。
3. 调 S01 `GET /authorization-context`（deadline 800ms）确认 `project.create` 权限。超时 → 503，不建任何行。
4. 写 `project`（`lifecycle='draft'`，`head_revision=1`）。
5. 写 `spec_revision(scope=project, revision=1)`。
6. 每个 `series`：写 `series` + `spec_revision(scope=series, revision=1)`。
7. 每个 `episode`：写 `episode` + `episode_order(order_kind='release', ordinal=1..N)` + 物化 `episode_spec_effective`。
8. 写 `outbox`：**每个 episode 一条** `project.episode.created`（见 §5.3 的事件粒度说明）。
9. 写 `idempotency_record(state='DONE', response_body=...)`。
10. 提交。

**"首批集"与"后续加集"用同一条命令路径。** 不设单独的 `POST /projects/{id}/episodes` 语义差异 —— catalog 声明的命令就是 `POST /projects/{id}/episodes`，它既用于批量补齐，也用于单集追加。批量时是 `episodes: [...]`，单集时是 `episodes: [ {...} ]`。

### 4.2 追加单集

```jsonc
POST /projects/{project_id}/episodes
{
  "command_id": "cmd_...",
  "expected_series_revision": 4,
  "episodes": [
    { "title": "第 8 集", "story_window_ref": {"clock_id":"story_main"},
      "release_ordinal": 8, "shooting_ordinal": 3 }
  ]
}
```

关键点：

- `release_ordinal` 由调用方给，**不是"自动排在最后"**。短剧经常先拍后播，也有插入集（"第 3.5 集"补拍）。自动排最后会逼着调用方发第二条重排命令，两次请求之间可能失败，产生"集存在但没有序号"的中间态。
- `expected_series_revision` 是 CAS：校验 `series.head_revision`。不匹配 → 409 `REVISION_CONFLICT`。
- 一个 episode 可以**不**带任何 ordinal（纯草稿集）。此时它在 `episode_order` 里没有行，UI 显示在"未排期"区。这不会破坏任何下游，因为下游只按 ID 引用。
- `episode_spec_effective` 在此事务内物化。

### 4.3 重排（最有风险的路径）

```jsonc
POST /series/{series_id}:reorder-episodes
{
  "command_id": "cmd_...",
  "order_kind": "release",
  "window_key": "",
  "expected_head_revision": 12,
  "order": ["ep_A", "ep_C", "ep_B"]          // 完整列表，不是 delta
}
```

为什么是完整列表而不是 `{move: ep_B, to: 2}`：delta 在并发重排时会静默产生非预期结果（两个人各移一个，结果取决于顺序）。完整列表 + CAS 让"两个人同时重排"变成一次 409，而不是一次错排。

事务：

```sql
-- 1) 行锁序列
SELECT 1 FROM project_catalog.series
 WHERE tenant_id=$1 AND series_id=$2 FOR UPDATE;

-- 2) 校验：列表必须恰好覆盖当前该 order_kind 下的全部 episode
--    （不允许重排顺手增删；增删走 §4.2）

-- 3) 延迟约束允许临时重复：先整体 +1000000 偏移
UPDATE project_catalog.episode_order
   SET ordinal = ordinal + 1000000
 WHERE tenant_id=$1 AND series_id=$2 AND order_kind=$3 AND window_key=$4;

-- 4) 按新顺序写回 1..N
UPDATE project_catalog.episode_order
   SET ordinal = $new_ordinal
 WHERE tenant_id=$1 AND order_id=$any($order_ids);

-- 5) CAS 推进 series.head_revision，写 outbox（本地事件），写幂等记录
```

`ordinal + 1000000` 这一步不是多余：虽然约束是 `DEFERRABLE`，但某些驱动/连接池设置会把约束提前检查；显式偏移让实现不依赖 `SET CONSTRAINTS` 的时机。偏移量只需大于最大集数，`1e6` 对短剧足够；若未来有超大系列，用 `max(ordinal)+1`。

**重排不发 `project.episode.created`。** 它不是"新集"，发错了会让下游把老集当新集处理（例如 S21 重复建 Run 模板）。见 C03-3：已登记的事件缺口。

### 4.4 查询：集上下文解析

这是全链路最热的读路径。S07 解析"第 3 场第 2 镜"、S21 建 Run、S28 归档命名，都要先拿集上下文。

```jsonc
GET /episodes/{episode_id}?fields=context
// 200
{
  "episode_id": "ep_01J...",
  "series_id": "ser_01J...",
  "project_id": "prj_01J...",
  "tenant_id": "tenant_demo",
  "lifecycle": "in_production",
  "project_lifecycle": "active",
  "orders": {
    "release": {"ordinal": 3, "window_key": ""},
    "shooting": {"ordinal": 1, "window_key": ""}
  },
  "story_window_ref": {"clock_id": "story_main"},
  "spec": { "content_hash": "...", "output": {...} },
  "source_revision": 7,
  "read_at": "2026-11-01T07:00:00Z"
}
```

- `source_revision` = `episode.head_revision`，用于调用方的 CAS 与"读到的是哪一版"的审计。
- 无 `projection_watermark`：本接口不含投影字段。
- 若调用方同时要场次列表 → 另调 S05；本接口**不返回 scene_ids**（避免调用方误当权威）。

### 4.5 规格变更 → 显式变更计划

```
POST /series/{series_id}/spec-revisions
  {command_id, expected_head_revision, body:{...}, apply_to_existing:false}
```

`apply_to_existing` 恒为 `false`（本版）。变更只影响**之后**物化的集。要影响已有集，必须走 S07 的 `ChangePlan`（[S07](../plans/S07-change-management.md)），由 S09 算闭包。S03 发 `catalog.spec.changed` 到**本地** Kafka topic（不登记在 `event-topics.v2.json`），S07 可选消费。

理由：一集画幅从 9:16 变成 16:9 意味着重做全部关键帧、全部视频、重算字幕安全区。这**不是目录服务能自己决定的事**，也不是一条命令能"顺手"生效的事。

### 4.6 失败矩阵

| # | 失败点 | 行为 | 是否重试 | 对外表现 |
|---|---|---|---|---|
| 1 | S01 授权超时 | 不写任何行 | 是（调用方有界退避） | 503 `AUTH_CONTEXT_UNAVAILABLE` |
| 2 | 幂等记录 `IN_PROGRESS` 未完成（并发同 command） | 等待 ≤2s 后返回 409 `COMMAND_IN_FLIGHT` | 否 | 409 |
| 3 | 幂等记录同 command 不同 hash | 立即 409 | 否 | 409 `IDEMPOTENCY_KEY_REUSED` |
| 4 | 集序重排时 `expected_head_revision` 不匹配 | 不写 | 否 | 409 `REVISION_CONFLICT`，带当前 `head_revision` |
| 5 | 重排列表与实际集集合不一致 | 不写 | 否 | 422 `ORDER_SET_MISMATCH`，带 `missing[]` / `extra[]` |
| 6 | 同一 episode 在同一 order_kind 出现两次 | 不写（非延迟约束立即拒） | 否 | 422 `DUPLICATE_EPISODE_IN_ORDER` |
| 7 | 物化规格时上游 spec revision 已被并发改 | 重读一次并重算；仍冲突 → 409 | 是（1 次） | 409 |
| 8 | Outbox 写入失败 | 整个事务回滚 | 是 | 500，无部分写入 |
| 9 | 提交成功但响应丢失 | 客户端重发同 `command_id` → 命中幂等记录原样返回 | 是 | 200（`replayed:true`） |
| 10 | 创建集时引用的 `series_id` 不存在/跨租户 | 不写 | 否 | 404 `REF_NOT_FOUND`（**跨租户同码**，E28） |
| 11 | `story_window_ref.clock_id` 在 S16 不存在 | **不阻断创建**，只记 `warning` | — | 200，带 `warnings[]` |
| 12 | 数据库主库切换 | 事务回滚，幂等记录可能停在 `IN_PROGRESS` | 是 | 恢复后由后台清扫器置 `ABANDONED` 并允许重发 |
| 13 | 归档时仍有 active Run | **不阻断归档**，返回 `warnings:[{code:"ACTIVE_RUNS_EXIST", count:N}]` | 否 | 200 |
| 14 | 读路径：`episode_scene_projection` 落后 | 返回数据 + `stale=true` + watermark | — | 200 |

第 11 条的理由：`story_window_ref` 是给 S16 的提示，S16 自己的 `episode_story_window` 才是权威。如果 S16 还没建窗口就拒绝建集，会让"先建目录再排故事时间"的正常顺序走不通。**但必须在响应里告诉调用方这个引用暂时不可解析**，不能让调用方以为它已经生效。

第 13 条的理由：归档是目录状态，Run 是执行状态。强行要求"先停全部 Run 才能归档"会让归档变成一个协调动作，而归档经常是**为了**让在途任务自然结束。C03-4 记录这个取舍。

---

## 5. 对外契约

### 5.1 命令

| 方法 | 路径 | 用途 | 幂等 | CAS |
|---|---|---|---|---|
| POST | `/projects` | 建项目 + 剧 + 首批集 | 必需 | 无 |
| POST | `/projects/{project_id}/episodes` | 追加集 | 必需 | `expected_series_revision` |
| POST | `/series/{series_id}:reorder-episodes` | 重排某一 `order_kind` | 必需 | `expected_head_revision` |
| POST | `/series/{series_id}/spec-revisions` | 发布剧集规格 revision | 必需 | `expected_head_revision` |
| POST | `/projects/{project_id}:archive` | 归档项目 | 必需 | `expected_head_revision` |
| POST | `/projects/{project_id}/members` | 指派成员 | 必需 | 无（幂等键包含内容） |

catalog 只声明了 `POST /projects/{id}/episodes`。其余四条是本文根据 `service.yaml` 的 acceptance（重排不改 ID、归档阻止新 Run）与 docs/06（成员）推出的**必要命令**。这条缺口登记为 C03-1。

### 5.2 查询

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/projects/{project_id}` | 项目头 + 剧列表 |
| GET | `/projects/{project_id}/episodes` | 集列表（含各 order_kind 的 ordinal） |
| GET | `/episodes/{episode_id}` | 集上下文（§4.4），**全链路最热** |
| GET | `/episodes/{episode_id}/scenes` | **代理** S05 的场次投影（带 `stale`） |
| GET | `/series/{series_id}/orders?order_kind=release` | 某一顺序的完整列表 |
| GET | `/series/{series_id}/spec-revisions/{revision}` | 规格版本全文 |
| GET | `/projects?org_unit_id=&lifecycle=` | 列表 |

所有查询返回 `source_revision` 与 `read_at`；带投影的额外返回 `projection_watermark` 与 `stale`。

**`GET /episodes/{episode_id}/scenes` 是代理，不是权威。** 它存在的唯一理由是 UI 树形导航不想为了展开一层而跨服务调两次。响应里必须带 `authoritative_source: "S05"` 字符串，让调用方（和未来的重构者）知道改数据要改 S05。

### 5.3 事件

`event-topics.v2.json` 只登记了一个：`project.episode.created`（topic `drama.project-catalog.v2`，partition `tenant_id:aggregate_id`，保留 14 天）。

本文的用法：

| 事件 | 何时发 | data |
|---|---|---|
| `project.episode.created` | 每个 episode 首次落库 | `{episode_id, series_id, project_id, release_ordinal\|null, shooting_ordinal\|null, spec_hash}` |
| `project.episode.reordered`（**新增，待登记**） | 重排成功后 | `{series_id, order_kind, window_key, order:[episode_id...], head_revision}` |
| `project.lifecycle.changed`（**新增，待登记**） | 任意 lifecycle 迁移 | `{scope:"project"\|"series"\|"episode", id, from, to, reason}` |

三个事件都用 `aggregate_seq` 单调，`aggregate_id` 分别是 `episode_id` / `series_id` / 对应 scope 的 id。

**为什么必须有 reorder 事件**：S09 的 `SCENE_DERIVED_FROM_SCRIPT` / `SHOT_DERIVED_FROM_SCENE` 边不带顺序；但 S21 的批量调度、S28 的归档命名、S43 的交付清单都按发行序。没有 reorder 事件，这三个服务只能靠轮询或"每次用前查一次"，前者浪费、后者在批量场景会被打爆。

**为什么 `project.episode.created` 一条事件一个 episode 而不是一条事件带 N 个**：partition key 是 `tenant_id:aggregate_id`，如果一条事件带 N 个 episode，`aggregate_id` 就没法填（填 series 会让所有 episode 事件串行到一个分区）。一条一个 `aggregate_id=episode_id` 是唯一自洽的形态。

### 5.4 错误码

| HTTP | code | retryable | 说明 |
|---|---|---|---|
| 400 | `INVALID_BODY` | false | schema 不合法 |
| 403 | `TENANT_MISMATCH` | false | header 与签名上下文不一致 |
| 404 | `REF_NOT_FOUND` | false | 项目/剧/集不存在（**跨租户同码**） |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同键不同 body |
| 409 | `COMMAND_IN_FLIGHT` | true | 同键同 body 正在执行 |
| 409 | `REVISION_CONFLICT` | false | CAS 失败，带 `current_revision` |
| 409 | `ORDER_LOCKED` | true | 该 `order_kind` 正在被另一重排持有 |
| 422 | `ORDER_SET_MISMATCH` | false | 重排列表与当前集合不一致 |
| 422 | `DUPLICATE_EPISODE_IN_ORDER` | false | 同一集在同一顺序出现两次 |
| 422 | `SPEC_FIELD_UNKNOWN` | false | `spec.body` 含未注册键 |
| 503 | `AUTH_CONTEXT_UNAVAILABLE` | true | S01 超时/熔断 |
| 503 | `CATALOG_UNAVAILABLE` | true | 本服务只读副本落后或主库不可写 |

**409 与 422 的分界**（沿用 [S08 §2.6](S08-revision-snapshot.md)）：409 是"状态/版本冲突，改了状态再试可能成功"；422 是"请求本身不可行，重试一万次也一样"。`ORDER_SET_MISMATCH` 是 422 而非 409，因为调用方需要先读最新集列表再重组请求，那不是"重试"，是"改请求"。

---

## 6. 幂等与并发规则

### 6.1 幂等

- 键：`tenant_id + command_id + request_hash`。
- `request_hash = sha256(JCS(body 去掉 command_id 与 request_hash))`，JCS 用 RFC 8785。
- 同键同 hash 且 `state=DONE` → 返回原 `response_code` + `response_body`，并加 `replayed: true`。
- 同键同 hash 且 `state=IN_PROGRESS` → 等待 ≤2s；仍未完成 → 409 `COMMAND_IN_FLIGHT`。
- 同键异 hash → 409 `IDEMPOTENCY_KEY_REUSED`，**永不覆盖**。

`IN_PROGRESS` 的清扫：后台任务每 60s 扫 `created_at < now()-5min AND state='IN_PROGRESS'`，置为 `ABANDONED` 并记告警。之后同 command_id 的重发按"新请求"处理（因为原事务确定已回滚）。

### 6.2 三条互不相同的版本概念

| 概念 | 归属 | 用途 | 绝不能用于 |
|---|---|---|---|
| `project/series/episode.head_revision` | S03 | 元数据 CAS | 判断"这一集内容变没变" |
| `spec_revision.revision` | S03 | 规格版本 | 判断"画幅是否与 manifest 一致"（要比 `content_hash`） |
| `manifest.revision` | S08 | 生产版本 | 目录侧任何写操作 |

E12 的常见误写：用 `head_revision` 当幂等依据。`head_revision` 会因无关的成员变更 +1；用它做幂等会把两次不同命令判成同一次。

### 6.3 锁顺序（必须写进代码）

```
series (FOR UPDATE)  →  episode  →  episode_order  →  spec_revision  →  outbox
```

- 只允许从外到内。跨层加锁一律按此顺序，防死锁。
- `reorder-episodes` 只锁 `series` 一行；它需要锁全部 `episode_order` 行时，因为已经持有父行锁，其他重排会先在父行上排队，不需要逐行加锁。
- `POST /projects/{id}/episodes` 锁 `series` 一行（CAS）后写 `episode` 与 `episode_order`；与重排争的是同一把 `series` 锁。
- 创建项目时不需要 `series` 锁（行还不存在）；靠 `UNIQUE(tenant_id, project_id, title)` 兜底。

### 6.4 隔离级别

`READ COMMITTED` + 显式行锁。不用 `REPEATABLE READ`：重排需要在事务内看到"偏移后"的 ordinal 才能写回，`RR` 的快照会让第二次 UPDATE 看到旧值。不用 `SERIALIZABLE`：本服务的冲突面已经被 `series` 行锁完全覆盖，`SERIALIZABLE` 只会带来无谓的重试。

### 6.5 并发场景与结果

| 场景 | 结果 |
|---|---|
| 两个重排同时到达同一 series | 一个成功，另一个在 `series` 行锁上排队；拿到锁后 CAS 失败 → 409 |
| 建集与重排并发 | 建集先拿 `series` 锁；重排拿到锁后看到新集，但它的 `order` 列表不含新集 → 422 `ORDER_SET_MISMATCH`，调用方重新取列表 |
| 规格变更与建集并发 | 建集物化时读到的是最新已提交 spec（`READ COMMITTED` 语句级快照）；若 spec 在物化后提交，新集用的是旧 spec，但 `derived_from` 记录了具体 revision，可解释 |
| 归档与建集并发 | 归档改 `project.lifecycle`；建集校验父项目 `lifecycle != 'archived'`。竞态窗口内可能建出一集"归档项目下的新集"，由后台一致性任务标记告警（不自动删） |

---

## 7. 可观测

### 7.1 指标

| 指标 | 类型 | 用途 |
|---|---|---|
| `catalog_command_total{operation,result}` | counter | 命令量与成功率 |
| `catalog_command_latency{operation}` | histogram | SLO；`GET /episodes/{id}` p99 是关键 |
| `catalog_order_reorder_total{order_kind}` | counter | 重排频次；突增说明计划不稳 |
| `catalog_order_set_mismatch_total` | counter | 调用方拿旧列表重排的频次 |
| `catalog_idempotency_replay_total` | counter | 重放率；突增说明客户端在网络问题上重试 |
| `catalog_idempotency_conflict_total` | counter | 同键异 hash；>0 说明有客户端在复用 command_id |
| `catalog_outbox_lag_seconds` | gauge | 最老未发布事件年龄；>60s 告警 |
| `catalog_outbox_depth` | gauge | 未发布行数 |
| `catalog_scene_projection_lag_seconds` | gauge | S05 事件延迟；影响 UI 场次树 |
| `catalog_auth_dependency_latency` | histogram | S01 依赖健康 |
| `catalog_archived_project_write_attempt_total` | counter | 归档后仍尝试写；应为 0 |

### 7.2 日志与追踪

- 每条命令日志带 `trace_id / tenant_id / command_id / operation / project_id / series_id / episode_id`。
- **不记录**项目/剧/集标题以外的任何自由文本（标题属于内容，按 docs/14 最小化）。成员 `principal_id` 可记，`principal` 的邮箱/姓名不记。
- 日志里**不出现** `story_window_ref` 的展开结果 —— 它可能泄漏剧情。

### 7.3 审计

S45 需要的审计证据由 S03 提供：

- 谁在什么时候创建/归档了项目（`created_by` + `outbox` 的不可变事件流）。
- 谁在什么时候重排了集序（`episode_order.assigned_by` + reorder 事件）。
- 规格每一次 revision 的全文与作者（`spec_revision` 表本身就是审计表）。

`outbox` 行保留至 `published_at + 30d`，之后归档到 S45 的冷存储，不删。审计链的连续性靠 `aggregate_seq`，不靠 Kafka（docs/04 §4：Kafka 不是永久账本）。

---

## 8. 测试清单

### 8.1 E02 · 三集连续生产（ID 稳定性）

| 序号 | 场景 | 断言 |
|---|---|---|
| T03-01 | 建项目 + 3 集，连续创建两次 | 第二次命中幂等，`episode_id` 不变、数量仍为 3 |
| T03-02 | 三集按 1,2,3 发行；重排为 3,1,2 | 三个 `episode_id` 全部不变；`episode_order.ordinal` 正确；旧 ID 的所有查询仍返回同一集 |
| T03-03 | 重排后按 `release ordinal` 查询 | 返回新顺序；按 `episode_id` 查询返回同一对象 |
| T03-04 | 重排后 S05 场次投影 | 投影不受影响（场次挂 `episode_id`，不挂序号） |

### 8.2 E03/E04 · 局部改镜与缺上下文

| 序号 | 场景 | 断言 |
|---|---|---|
| T03-05 | `GET /episodes/{id}?fields=context` 被 S07 用于解析"第 3 场第 2 镜" | 返回 `episode_id/series_id/project_id` 与 `spec.output`；不返回 `scene_id`（必须另查 S05） |
| T03-06 | 集存在但 `story_window_ref.clock_id` 在 S16 不可解析 | 200 + `warnings[]`，不阻断 |
| T03-07 | 缺集上下文（查询不存在的 episode） | 404 `REF_NOT_FOUND`，**不返回任何近似集**（E04：不猜） |

### 8.3 E08/E10 · 集序与分支

| 序号 | 场景 | 断言 |
|---|---|---|
| T03-08 | 改前集后重排发行序 | 前集的 `episode_id` 与 S16 checkpoint 映射不变；S09 边不断 |
| T03-09 | 同一集在 `release` 与 `shooting` 各有一个 ordinal | 两条记录独立；改其一不影响另一 |
| T03-10 | `rebroadcast` 窗口：同一集在 `window_key='2027Q1'` 有第二个 release 序 | 允许；`release/window_key=''` 的那条不受影响 |

### 8.4 E09 · 闪回与故事时间

| 序号 | 场景 | 断言 |
|---|---|---|
| T03-11 | 第 7 集是闪回，`release ordinal=7`，`story_window_ref.clock_id='story_main'` | S03 不推断故事先后；返回 `release=7` 与指针，**不含任何 story tick** |
| T03-12 | 按 `release ordinal` 排序集列表 | 顺序正确；S16 按 `episode_order`（S16 自己的表）取故事窗口，不读 S03 的 ordinal |

### 8.5 E12 · 幂等

| 序号 | 场景 | 断言 |
|---|---|---|
| T03-13 | 同 `command_id` 同 body 建项目 | 返回同一 `project_id/series_id/episode_id` 集合，`replayed:true`，无新行 |
| T03-14 | 同 `command_id` 异 body | 409 `IDEMPOTENCY_KEY_REUSED`，无写入 |
| T03-15 | 并发两个同 `command_id` | 一个执行，另一个 409 `COMMAND_IN_FLIGHT` 或等完后 `replayed:true`；**绝不产生两套 ID** |
| T03-16 | `reorder` 重复投递 | 第二次原样返回，ordinal 不被再次偏移 |

### 8.6 E13 · 崩溃恢复

| 序号 | 场景 | 断言 |
|---|---|---|
| T03-17 | 提交后、响应前进程崩溃 | 重发同 command → `replayed:true`，数据一致 |
| T03-18 | 幂等记录写 `IN_PROGRESS` 后崩溃 | 5min 后清扫为 `ABANDONED`；重发执行成功，不产生重复集 |
| T03-19 | Outbox 写入前崩溃 | 事务回滚，无孤立 episode；重发成功，事件恰好一条 |
| T03-20 | Outbox 已写、发布前崩溃 | 中继重启后按 `published_at IS NULL` 续发；下游按 `event_id` 去重 |

### 8.7 E28 · 跨租户

| 序号 | 场景 | 断言 |
|---|---|---|
| T03-21 | 租户 A 用租户 B 的 `episode_id` 查询 | 404 `REF_NOT_FOUND`，与"不存在"同码 |
| T03-22 | 租户 A 试图用租户 B 的 `series_id` 建集 | 404，无写入 |
| T03-23 | 两个租户创建同名 `slug` | 双方都成功（`UNIQUE(tenant_id, slug)`） |
| T03-24 | body 里塞 `tenant_id` 与签名上下文不符 | 403 `TENANT_MISMATCH`，不读 body 的 tenant |

### 8.8 归档与生命周期

| 序号 | 场景 | 断言 |
|---|---|---|
| T03-25 | 归档项目 | `lifecycle='archived'`，`archived_at` 非空；已有集仍可查 |
| T03-26 | 归档后仍有 active Run | 归档成功 + `warnings:[ACTIVE_RUNS_EXIST]`；**不级联停 Run** |
| T03-27 | 归档后 S21 尝试开 Run | S21 通过 `GET /episodes/{id}` 看到 `project_lifecycle=archived` → 拒绝（**跨服务测试**） |
| T03-28 | `suspended` 项目 | 仍可查、可读；是否可写由 S02 判定，S03 只暴露事实 |

### 8.9 E32 · 重放

| 序号 | 场景 | 断言 |
|---|---|---|
| T03-29 | 用 `side_effects=false` 回放 S03 topic 到 S03 自己的投影消费者 | 幂等；不产生新 outbox 行 |
| T03-30 | S05 事件乱序到达（seq 5 到 seq 3 前） | `episode_scene_projection` 不倒退（`applied_seq` 只前进） |
| T03-31 | S05 事件缺口超时 | 回源 S05 `GET /episodes/{id}/scenes`；仍失败则投影标 `stale=true` |

### 8.10 规格与继承

| 序号 | 场景 | 断言 |
|---|---|---|
| T03-32 | 项目 spec 9:16，剧 spec 16:9，集无 spec | 该集物化结果为 16:9；`derived_from` 记录来源 revision |
| T03-33 | 改项目 spec 为 1:1 | **已有集不变**（物化值不动）；之后新建的集为 1:1 |
| T03-34 | `spec.body` 含未知键 | 422 `SPEC_FIELD_UNKNOWN`，不写 |
| T03-35 | 同一 spec 内容重复发布 | 命中 `UNIQUE(scope,scope_id,content_hash)`，返回既有 revision（幂等） |

---

## 9. 反模式清单（写了就是错）

1. ❌ 在 `episode` 表加 `episode_number` 列 → 下游会用它当权威，重排后全部错位。序号只能在 `episode_order`。
2. ❌ 重排时改 `episode_id` → S16 claim、S09 边、S08 manifest 全部指向不存在的集。
3. ❌ S03 铸造 `scene_id` 或 `shot_id` → 第二序号权威；改动分场就要改目录版本。
4. ❌ 在 S03 存 `accepted` 标志 → 第二接受权威，S16 的写入路径失去唯一触发源。
5. ❌ 用 `episode_order` 推断故事时间 → 闪回集必错（E09）。
6. ❌ 读时合并三层 spec → 改一次项目默认，在跑的集悄悄换画幅。
7. ❌ 重排用 delta 而非完整列表 → 并发重排静默错排。
8. ❌ 把 `member` 表当权限判定 → 撤权/委派过期/ kill switch 全看不到（E27）。
9. ❌ 归档时级联删/改下游数据 → 目录不是保留合同的所有者。
10. ❌ 用 `head_revision` 做幂等键 → 无关的成员变更会让幂等失效。
11. ❌ 在重排事务里调 S05/S06 校验场次 → 一次外部慢调用把整个 series 串行化。
12. ❌ 代理 `GET /episodes/{id}/scenes` 时不带 `authoritative_source` → 未来重构者会往这里写数据。
13. ❌ 一条 `project.episode.created` 带 N 个 episode → `aggregate_id` 无法填，分区与保序全乱。
14. ❌ 把 `suspended` 与 S01 的 kill switch 混成一个状态 → 计划暂停与安全动作无法区分。

---

## 10. 冲突（报告，不选边）

### 10.1 核实：镜头 ID 与场次 ID 的归属

任务简报称"catalog 里写的是'镜头 ID 归 S05'"，并要求核实是否与 S06 的"Shot 稳定 ID"冲突。**逐字核对后的结论：仓库里没有任何文件写"镜头ID由S05负责"。**

证据：

| 文件 | 行 | 原文 |
|---|---|---|
| `contracts/service-catalog.v2.json` | 51 | `Project/Series/Episode稳定ID、成员引用、规格与业务目标；场次ID由S05负责` |
| `services/project-catalog/service.yaml` | 15 | 同上（生成物，与 catalog 一致） |
| `docs/design-package/services/S03-project-catalog.md` | 93 段 | 同上 |
| `docs/design-package/SERVICE-SPECIFICATIONS.md` | 93 | 同上 |
| `contracts/service-catalog.v2.json`（S06） | — | `Shot稳定ID/ShotSpecRevision/动作节拍/机位构图/镜头顺序` |

即：**S05 拥有的是"场次（Scene）"稳定 ID，S06 拥有"镜头（Shot）"稳定 ID。两者不冲突，也没有交叉。** S03 的 `data_owner` 字符串里出现的是 `场次ID`，`镜头` 二字在整个 S03 元数据中不出现。

旁证（互相独立、结论一致）：

- [docs/01 §3](../docs/01-enterprise-architecture.md) 唯一权威表："剧本/分镜内容版本 | S04/S06"。
- [docs/07 §2](../docs/07-production-workflows.md)："S07 用明确集上下文解析稳定 `shot_id` → S06 准备新 DSL"，`shot_id` 由 S06 侧提供。
- [S09](S09-lineage-impact.md) 的边类型：`SCENE_DERIVED_FROM_SCRIPT`（S05 报）、`SHOT_DERIVED_FROM_SCENE`（S06 报），两条边方向明确区分了 Scene 与 Shot 的拥有者。
- `git log -S"镜头ID由S05"` 在本仓库**无任何提交**；`git log -S"场次ID由S05"` 只命中脚手架提交 `8c94e5f`。

**本文采用（与仓库一致）**：S03 = Project/Series/Episode；S05 = Scene；S06 = Shot。简报的表述判定为转写误差，不构成设计冲突。

**真正待裁决的那一半**：`shot_id` 由谁铸造、`shot_lineage_id` 是否存在，仓库确实没有定义。见 C03-5。

### 10.2 冲突与缺口表

| # | 冲突 / 缺口 | 现状 | 本文用法 | 需要谁裁决 |
|---|---|---|---|---|
| C03-1 | catalog 只声明 `POST /projects/{id}/episodes`，但 `service.yaml` 的 acceptance 要求"重排集序不改变稳定ID"与"归档项目阻止新Run"，两者都需要独立命令 | catalog vs acceptance | 补 `:reorder-episodes`、`/spec-revisions`、`:archive`、`/members` 四条（§5.1） | `contracts/service-catalog.v2.json` + `openapi.v2.yaml` |
| C03-2 | 简报称"镜头 ID 归 S05"；仓库所有产物写的是"场次 ID 归 S05"、"Shot 稳定 ID 归 S06" | 简报 vs 仓库 | 按仓库：S03 不碰 Scene/Shot（§10.1 有逐字证据） | 任务简报的发出方（非设计冲突） |
| C03-3 | `event-topics.v2.json` 只登记 `project.episode.created`，没有重排、生命周期变更事件；但 S21/S28/S43 依赖发行序 | topics | 新增 `project.episode.reordered`、`project.lifecycle.changed`（§5.3） | 契约维护方 |
| C03-4 | `service.yaml` acceptance 说"归档项目阻止新Run"，但 S03 无能力停止 S21/S26；docs 未指定由谁执行该拒绝 | acceptance vs 职责 | S03 只提供可查事实；S21/S26/S02 准入时拒绝（§3.3），并新增测试 T03-27 | docs/07 或 S21 方案作者 |
| C03-5 | 仓库未定义 `shot_id` 的铸造者与 `shot_lineage_id` 的存在性（[S39 §11 C4](S39-repair-planning.md)、[S09 C09-8](S09-lineage-impact.md) 均已登记） | 无契约 | 建议 S06 在创建 Shot 时铸造 `shot_id` 与 `shot_lineage_id`，S08 在 manifest 固定；S03 不参与 | S06/S08/S39 的实施会话 |
| C03-6 | `depends_on` 只有 `S01`，但实际运行期要读 S16（`story_window_ref` 校验）、S05（场次投影）、S02（归档后的写权限） | catalog | 依赖表是运行协作关系，事件消费与只读查询不构成启动依赖；若要求显式化需改 catalog | catalog 维护方 |
| C03-7 | docs/01 §1 要求"拍摄顺序"记录，但没有任何设计文档说明拍摄顺序属于哪个域 | docs/01 | 归 S03 的 `episode_order(order_kind='shooting')`；理由是它与叙事无关、属排期计划 | docs/01 作者 |
| C03-8 | `episode.lifecycle='completed'` 的判定依据未定义（docs/07 只说"最终门通过"） | docs/07 | 定义为 S08 accepted master 的投影，非 S03 事实，因而可回退（§3.2） | docs/07 作者 |
| C03-9 | `spec.body` 没有 JSON Schema，只有本文的字段集 | contracts | 建议新增 `contracts/domain-v1/project-spec.v1.schema.json` | 契约维护方 |

---

## 11. 实施顺序与未决问题

### 11.1 顺序（全部生产开关默认关闭）

1. **契约先行**：`project-spec.v1.schema.json` + `episode-order.v1.schema.json` + 本方案 §5 的 OpenAPI 路径。改 `service-catalog.v2.json` 后**重新生成** `service.yaml`，不手改。
2. **平台依赖**：`platform/idempotency-sdk`（幂等三态 + 清扫器）、`platform/event-sdk`（Outbox 中继）。这两块与 S08/S10 共用同一实现，**不能各写一份**。
3. **最小闭环**：`POST /projects` + `POST /projects/{id}/episodes` + `GET /episodes/{id}`。用 mock S01（固定授权上下文）。先验 E12 / E28。
4. **重排**：`:reorder-episodes` + `DEFERRABLE` 唯一约束 + 偏移写回。验 T03-02 / T03-15 / T03-16。
5. **规格继承**：三层 `spec_revision` + `episode_spec_effective` 物化。验 T03-32..35。
6. **S05 场次投影**：消费 `breakdown.revision.created`，维护 `episode_scene_projection`。验乱序与回源（T03-30/31）。
7. **生命周期与归档**：`:archive` + 事件 + `warnings`。验 T03-25..28。
8. **成员**：`/members`。**最后做**，因为它不影响任何生产正确性，只影响 UI 与默认审核人。
9. 接真实 S01；打开 RLS；补齐指标与告警。

### 11.2 未决问题（需要实施会话或设计方回答）

1. **组织的层级深度**：`project.org_unit_id` 只引用一层。docs/01 写的是 `Tenant → Organization → Project/Series`，但 Organization 内部是否还有部门/团队层级，S01 的方案里没有闭合。若有多层，S03 需要 `org_path` 还是只存叶子 ID？本文按叶子 ID 实现。
2. **一个 project 能否有多个 series**：本文允许（`UNIQUE(tenant_id, project_id, title)`）。短剧常见"一部剧多季"，但季与 series 的关系没有设计文档定义。若"一季=一个 series"，则 `target_episodes` 是单季计划；若"全剧=一个 series"，则季需要另一层对象。
3. **取消归档**：`archived` 本文定义为 S03 侧的终态。业务上是否需要"恢复归档项目"？若需要，是新建 project 还是状态回退？状态回退会让 S09 的历史边重新"复活"，风险较高。
4. **`release_window` 的时区语义**：`tstzrange` 带时区，但"上线时间"经常是"某平台在某地区的本地时间"。是否需要 `{platform, region, local_date}` 三元组？本文按 `tstzrange` 实现并留待验证。
5. **`story_window_ref` 的形态**：本文只用 `{clock_id, section_hint}`。S16 的 `episode_story_window` 需要 `tick_start/tick_end`，而这两个值在 S05 分场完成后才可能精确。是否应该让 S16 建窗口、S03 只存 `story_window_ref.story_window_id`？这需要 S16 方案确认。
6. **业务目标与合同的映射**：`budget_envelope` 的 `cap_micro` 是否要与 S23 的 `budget_scope` 一一对应？若不对应，两个"上界"会在报表里打架。
7. **归档的保留期**：`archived` 之后多久可以真正删除项目行？docs/04 §6 要求按引用图与法律保留检查，但 S03 不是引用图的所有者（S09 是）。需要在 S09 提供"这个 project 还有没有下游引用"的查询之后才能实现删除。本版**不实现删除**。
