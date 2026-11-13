# S13 · 场景资产 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码；SLO 与容量为 docs/03 的规划值，**未实测**。

上游依据：[docs/10](../docs/10-assets.md)（场景行）、[docs/01 §3](../docs/01-enterprise-architecture.md)、[docs/04](../docs/04-data-consistency.md)、[docs/06 §2–§4](../docs/06-automation-governance.md)、[docs/14 §2](../docs/14-enterprise-operations.md)、[S13 规格](../docs/design-package/services/S13-scene-assets.md)。

配套：[S10](S10-asset-catalog.md)（只读引用）、[S11](S11-character-assets.md) / [S12](S12-costume-assets.md) / [S14](S14-prop-assets.md) / [S15](S15-voice-assets.md)（**同级，不互相 import、不互相写库**）、[S28](S28-artifact-ingest.md)、[S02](S02-governance-policy.md)、[S16](S16-story-memory.md)、[S17](S17-continuity.md)。

---

## 0. 一句话边界

**S13 是"这个空间长什么样、门在哪、窗朝哪、轴线怎么走"的唯一权威。**

它是五类资产里**唯一带空间几何**的一类。这决定了它的质量门是 docs/10 里最特殊的一条："空间可解释、门窗与轴线一致"。别的资产错是"不像"，场景错是"这个镜头根本不该存在"。

| 问题 | 归谁 |
|---|---|
| 这个房间的平面图、门在哪、窗朝哪 | **S13** |
| 多个角度的 anchor 图（同一空间的一致性基准） | **S13** |
| 固定物（不能搬的家具/结构） | **S13** |
| 年代、日夜光照变体 | **S13** |
| 这个镜头从哪个机位、往哪个方向拍 | S09 分镜（S13 提供空间约束供其校验） |
| 谁在这个房间里 | S16 |
| 房间里的可移动道具（那把钥匙） | S14 |
| 参考图字节 | S28 |

### 0.1 写什么 / 不写什么

| 事实 | 唯一写者 |
|---|---|
| `SceneAssetRevision`（不可变） | **S13** |
| 平面图 / 空间拓扑（房间、走廊、门、窗、楼梯） | **S13** |
| 多角度 anchor（每张带位姿/朝向元数据） | **S13** |
| 固定物清单与位置 | **S13** |
| era（年代）与 lighting 变体（day/night/dusk/...） | **S13** |
| canonical 指针 | **S13** |
| 场景级权利引用 | **S13 存指针**，判定归 S02 |
| 某场戏在哪个场景 | S10 binding；S12/S13 不记 |
| 镜头轴线/越轴检查结论 | **S17**（S13 只提供可供检查的几何） |

### 0.2 四个明确不做

1. **不做越轴判定。** S13 提供平面图与 anchor 朝向；判断"这两个镜头是否越轴"是 S17 的连续性检查。S13 若自己判，就成了第二连续性权威。
2. **不记录镜头/机位。** 机位是 S09 分镜的产物。S13 的 anchor 是"空间在此方向看过去长什么样"，不是"第 12 镜从这拍"。
3. **不记录谁在场、发生了什么。** 全归 S16。
4. **不在场景里内嵌可移动道具资产。** 场景里有一张桌子（固定物）≠ 桌上有那把钥匙（S14 道具）。判据：**能不能被角色拿走？** 能 → S14；不能 → S13 固定物。

---

## 1. 关键区分

### 1.1 空间拓扑 vs 视觉 anchor vs 光照变体

| 层 | 形态 | 作用 | 是否可机器校验 |
|---|---|---|---|
| 拓扑 | 结构化：房间节点、墙段、门/窗开口、连通关系、坐标 | 空间可解释性、门位一致性、走位可行性 | 可（图 + 几何） |
| anchor | 每个朝向一组 artifact + 相机参数（yaw/pitch/roll/位置/hfov） | 多角度一致性、生成时的参考 | 部分（视觉相似度） |
| 光照 | 每个 anchor 在若干 lighting 下的变体 | 日夜/黄昏/室内灯 | 可（元数据一致性） |

三层分开存的原因：**拓扑是权威，anchor 是证据，光照是派生**。如果只存图，那么"门在左边还是右边"要靠视觉模型猜；如果只存拓扑，模型没法直接吃。

### 1.2 anchor 的"不可变性"（docs/10 的特殊要求）

`service.yaml` 明写："场景anchor不可变；跨镜头空间检查可复用"。

落实方式：

- 一个 `SceneAssetRevision` 里的 anchor **一旦封存，永不替换**。
- 要换 anchor（更清晰的图、修正穿帮），必须**接受新 revision**，并且**旧 revision 的 anchor 仍然可读**。
- 新 revision 如果改变了 anchor 的空间参数（例如门的位置），必须显式标记 `topology_changed: true`，触发 S07/S09 影响分析。

这与"资产更新追加revision"一致，但场景多一条硬约束：**revision 之间的 anchor 不能悄悄变**。如果 `topology_hash` 变了而调用方不知道，所有已生成的镜头都可能与新的空间不一致，而系统不会报错。

所以 `content_hash` 之外，S13 还单独发布 `topology_hash`：只覆盖拓扑部分的 hash。两者分开，让调用方能问"空间变了没有"而不必比对整包。

### 1.3 era 与 lighting 是"轴"不是"变体名"

- `era` 是**场景定义级**属性（这个空间属于 1990s 还是当代）。改 era = 新场景，不是新 revision。
- `lighting` 是**revision 级**轴（同一空间在 day/night/dusk 下分别长什么样）。

为什么 era 不做轴：同一个房间换个年代，本质是不同的空间（家具、墙面、电器全变）。把它做成轴会让"1990 版"和"2020 版"共享拓扑，而实际拓扑往往不同（隔断、门宽）。

为什么 lighting 做轴：同一空间在不同光照下的几何不变，只是明暗和色温变化，且**必须是同一空间** —— 这正是需要绑定的。

### 1.4 固定物 vs 道具（最容易混）

判据三条，任一为"是"则该物体归 S14：

1. 角色能不能把它拿走、移动、藏起来？
2. 它的状态（开/合/损坏）会不会因剧情而变，且变化影响后续场次？
3. 它需要特写镜头来表达信息（信上的字、钥匙的形状）？

否则归 S13 固定物（沙发、窗框、墙上的画）。

`fixed_object` 表只存**位置与描述**，不存独立的可变状态。场景里"沙发被翻倒了"是 S16 的剧情状态，S13 的 revision 里的沙发是"正常摆放"。

### 1.5 canonical ≠ 最新

同 S11/S12。场景多一个陷阱：**同一地点的多个 revision 可能代表不同的"建造阶段"**（装修前/后）。这些应该建模为**两个 scene_asset_id**（`loc_office_before` / `loc_office_after`），而不是同一 ID 的两个 revision —— 因为它们的拓扑不同，用 revision 表达会让"第 4 集和第 9 集是同一个地点吗"这个问题无法回答。

---

## 2. 数据模型

Postgres，schema `scene_assets`。所有表含 `tenant_id`，主键/唯一约束含 `tenant_id`（E28）。RLS 作为第二道防线（docs/14 §2）。

### 2.1 场景定义

```sql
CREATE TABLE scene_assets.scene_asset (
  tenant_id       text NOT NULL,
  scene_asset_id  text NOT NULL,             -- 'scn_01J...'
  project_id      text NOT NULL,
  series_scope    text NOT NULL,
  display_name    text NOT NULL,
  scene_kind      text NOT NULL CHECK (scene_kind IN (
                    'interior','exterior','hybrid','abstract','vehicle')),
  era             jsonb NOT NULL,            -- {label:'contemporary', year_band:'2015-2025', region:'cn_urban'}
  location_class  text NOT NULL CHECK (location_class IN (
                    'primary_set','recurring_location','one_off','background')),
  status          text NOT NULL CHECK (status IN ('active','deprecated','tombstoned')),
  canonical_revision integer,
  current_revision   integer NOT NULL DEFAULT 0,
  topology_hash      char(64),               -- 当前 canonical 的拓扑 hash（冗余，便于快速比对）
  created_by      jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, scene_asset_id),
  UNIQUE (tenant_id, series_scope, display_name),
  CONSTRAINT scene_canonical_within_head CHECK (canonical_revision IS NULL OR canonical_revision <= current_revision)
);
CREATE INDEX scene_by_series ON scene_assets.scene_asset (tenant_id, series_scope, status, location_class);
CREATE INDEX scene_by_era ON scene_assets.scene_asset (tenant_id, (era->>'label'), (era->>'year_band'));
```

`location_class` 决定质量门强度：

- `primary_set`（主场景，全剧多次使用）：必须有完整拓扑 + 至少 4 向 anchor + 全 lighting 轴。
- `recurring_location`：至少 2 向 anchor + day/night。
- `one_off`：至少 1 向 anchor，可无完整拓扑。
- `background`：1 张参考即可，可无 canonical。

这个分级是必要的：如果所有场景都要求"完整平面图 + 8 向 anchor"，制作成本会高到没人用。但主场景必须严格，因为所有跨集一致性都挂在那里。

### 2.2 不可变 revision

```sql
CREATE TABLE scene_assets.scene_revision (
  tenant_id        text NOT NULL,
  scene_asset_id   text NOT NULL,
  revision         integer NOT NULL CHECK (revision >= 1),
  content_hash     char(64) NOT NULL,        -- 整包
  topology_hash    char(64) NOT NULL,        -- 仅拓扑
  payload          jsonb NOT NULL,           -- 完整 scene-revision.v1
  topology         jsonb NOT NULL,           -- 结构化空间（见 §2.3）
  anchor_manifest  jsonb NOT NULL,           -- {front:{artifact_ref,camera}, left:{...}, ...}
  fixed_objects    jsonb NOT NULL,           -- 固定物清单
  lighting_axes    jsonb NOT NULL,           -- ['day','night','dusk','interior_warm',...]
  lighting_manifest jsonb NOT NULL,          -- {anchor_direction: {lighting: artifact_ref}}
  era_snapshot     jsonb NOT NULL,           -- 封存时的 era，供历史解析
  rights_ref       jsonb NOT NULL,
  source_kind      text NOT NULL CHECK (source_kind IN ('manual_upload','generated','imported','derived','scanned_floorplan')),
  parent_revision  integer,
  topology_changed boolean NOT NULL,         -- 相对 parent_revision 是否改拓扑
  quality_gate_ref jsonb NOT NULL,
  accepted_at      timestamptz NOT NULL,
  accepted_by      jsonb NOT NULL,
  PRIMARY KEY (tenant_id, scene_asset_id, revision),
  CONSTRAINT sr_hash_len CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT sr_topohash_len CHECK (topology_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT sr_parent_lt CHECK (parent_revision IS NULL OR parent_revision < revision)
);
CREATE UNIQUE INDEX sr_hash_uniq ON scene_assets.scene_revision (tenant_id, content_hash);
CREATE INDEX sr_topology_hash ON scene_assets.scene_revision (tenant_id, scene_asset_id, topology_hash);
```

`topology_changed` 是**显式声明**，不是自动比对得出的。封存时服务会**校验**它（比对 `topology_hash` 与 parent 是否相同），不一致则拒绝 —— 防止有人改了几何却声称没改。这样下游（S07/S09）可以信任这个布尔值。

### 2.3 拓扑结构

```jsonc
{
  "units": [
    {"unit_id":"office_main","kind":"room","polygon":[[0,0],[6,0],[6,4],[0,4]],"floor_z_band":"0"},
    {"unit_id":"corridor_a","kind":"corridor","polygon":[[6,1],[9,1],[9,3],[6,3]]}
  ],
  "openings": [
    {"opening_id":"door_1","kind":"door","unit_a":"office_main","unit_b":"corridor_a",
     "wall_segment":[[6,1.2],[6,2.2]],"width_cm":100,"swing":"inward_left"},
    {"opening_id":"win_1","kind":"window","unit_a":"office_main",
     "wall_segment":[[2,0],[4,0]],"sill_cm":90,"height_cm":140}
  ],
  "orientation": {"north_axis_deg": 0, "unit_north_ref": "office_main"},
  "structures": [
    {"structure_id":"col_1","kind":"column","footprint":[[5.7,0.3],[6,0.3],[6,0.6],[5.7,0.6]],"height_cm":320}
  ],
  "walkable": {"office_main":"polygon_ref","corridor_a":"polygon_ref"},
  "scale_unit": "meter",
  "source_trace": {"method":"hand_drawn|floorplan_import|generated","ref":null}
}
```

硬约束：

- **坐标单位固定为米，2D 顶视平面。** 不做 3D。3D 会引入大量建模依赖，而分镜校验只需要 2D 可行走性 + 朝向。
- `orientation.north_axis_deg` 必须有，且定义"哪个方向是北"。没有绝对朝向，日光方向无法解释。
- 每个 `opening` 必须挂在两个 unit 上（或一个 unit + 外部），且 `wall_segment` 必须落在该 unit 的 polygon 边界上。封存时校验 → 否则 422 `TOPOLOGY_INVALID {opening_id, reason}`。
- `walkable` 必须覆盖所有非墙 unit，保证走位可行性可计算。

为什么不用自由文本画一个示意图？因为"门在左边"在 4 个镜头里有 4 种"左边"。结构化坐标是唯一能跨镜头对齐的方式。

### 2.4 anchor 与光照

```sql
CREATE TABLE scene_assets.scene_anchor (
  tenant_id      text NOT NULL,
  scene_asset_id text NOT NULL,
  revision       integer NOT NULL,
  anchor_id      text NOT NULL,             -- 'front'|'back'|'left'|'right'|'from_door'|...
  position_m     jsonb NOT NULL,            -- {x,y,z}
  yaw_deg        numeric NOT NULL,
  pitch_deg      numeric NOT NULL DEFAULT 0,
  hfov_deg       numeric NOT NULL,
  unit_id        text NOT NULL,             -- 相机所在的 unit
  looks_at       text,                      -- 主要注视的 unit/structure
  artifact_ref   jsonb NOT NULL,            -- {service_id:'S28',id,content_hash}
  lighting_kind  text NOT NULL DEFAULT 'day',-- 该 anchor 的基准光照
  PRIMARY KEY (tenant_id, scene_asset_id, revision, anchor_id)
);
CREATE INDEX scene_anchor_by_unit ON scene_assets.scene_anchor (tenant_id, scene_asset_id, revision, unit_id);

CREATE TABLE scene_assets.scene_lighting_variant (
  tenant_id      text NOT NULL,
  scene_asset_id text NOT NULL,
  revision       integer NOT NULL,
  anchor_id      text NOT NULL,
  lighting       text NOT NULL,             -- day|night|dusk|dawn|interior_warm|interior_cold
  artifact_ref   jsonb NOT NULL,
  PRIMARY KEY (tenant_id, scene_asset_id, revision, anchor_id, lighting)
);
```

`camera` 参数必须落到每个 anchor 上。没有相机参数，"这个角度看到的门在哪"无法与拓扑对账；有了参数，S17 才能做"镜头朝向是否与门位一致"的机器检查。

`lighting_kind` 是 anchor 的基准光照（通常是 `day`），而 `scene_lighting_variant` 是该 anchor 在其他光照下的渲染。基准必须有，否则日夜切换时没有参照。

### 2.5 固定物

```sql
CREATE TABLE scene_assets.scene_fixed_object (
  tenant_id      text NOT NULL,
  scene_asset_id text NOT NULL,
  revision       integer NOT NULL,
  object_id      text NOT NULL,
  label          text NOT NULL,             -- 'desk'|'sofa'|'wall_clock'
  unit_id        text NOT NULL,
  footprint      jsonb NOT NULL,            -- 2D polygon（与拓扑同单位）
  height_cm      integer,
  is_load_bearing boolean NOT NULL DEFAULT false,  -- 角色能否站/坐其上
  blocks_walkable boolean NOT NULL DEFAULT true,
  description    text,
  PRIMARY KEY (tenant_id, scene_asset_id, revision, object_id)
);
CREATE INDEX scene_fixed_object_by_unit
  ON scene_assets.scene_fixed_object (tenant_id, scene_asset_id, revision, unit_id);
```

`blocks_walkable` 参与走位可行性计算：一张沙发挡住路径，角色就不能"从门直接走到窗"。这是 S09/S17 校验分镜可行性的输入。

**固定物没有状态字段。** 没有 `is_open`、没有 `is_damaged`。有这些就说明它其实是道具（§1.4 判据 2）。

### 2.6 候选与 canonical

```sql
CREATE TABLE scene_assets.scene_candidate (
  tenant_id     text NOT NULL,
  candidate_id  text NOT NULL,
  scene_asset_id text NOT NULL,
  content_hash  char(64) NOT NULL,
  topology_hash char(64) NOT NULL,
  payload       jsonb NOT NULL,
  spatial_check jsonb NOT NULL,             -- {topology_valid, openings_attached, axis_check, anchor_coverage}
  evaluation    jsonb NOT NULL,
  state         text NOT NULL CHECK (state IN ('PENDING','ACCEPTED','REJECTED','EXPIRED')),
  created_by    jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, candidate_id)
);

CREATE TABLE scene_assets.canonical_pointer (
  tenant_id        text NOT NULL,
  scene_asset_id   text NOT NULL,
  revision         integer NOT NULL,
  topology_hash    char(64) NOT NULL,
  decided_at       timestamptz NOT NULL DEFAULT now(),
  decided_by       jsonb NOT NULL,
  decision_kind    text NOT NULL CHECK (decision_kind IN ('human_approved','policy_auto','initial_import')),
  reason_text      text,
  prior_canonical  integer,
  superseded_at    timestamptz,
  PRIMARY KEY (tenant_id, scene_asset_id, revision)
);
CREATE UNIQUE INDEX scene_canonical_one
  ON scene_assets.canonical_pointer (tenant_id, scene_asset_id)
  WHERE superseded_at IS NULL;
```

`canonical_pointer` 冗余 `topology_hash`：让"当前空间长什么样"的比对不需要读整包。

### 2.7 空间检查结果（供 S17 复用，不存结论）

```sql
CREATE TABLE scene_assets.anchor_relationship (
  tenant_id       text NOT NULL,
  scene_asset_id  text NOT NULL,
  revision        integer NOT NULL,
  from_anchor     text NOT NULL,
  to_anchor       text NOT NULL,
  relative_yaw_deg numeric NOT NULL,        -- to 相对 from 的偏航
  shared_features jsonb NOT NULL,           -- 两个 anchor 都应看到的物体/开口
  PRIMARY KEY (tenant_id, scene_asset_id, revision, from_anchor, to_anchor)
);
```

这张表是**从 anchor 相机参数推导出的关系**，可重建。它让 S17 能问"从 front 转到 left 应该看到什么"，而不必自己算几何。

**它不是连续性结论。** 没有任何"是否越轴"的字段。S17 拿这张表 + 实际镜头参数自己做判断。

### 2.8 基础设施

与 S11/S12 同构（`idempotency_record` / `outbox` / `inbox`），在 S13 自己的库里。照抄如下：

```sql
CREATE TABLE scene_assets.idempotency_record (
  tenant_id text NOT NULL, command_id text NOT NULL, request_hash char(64) NOT NULL,
  command_type text NOT NULL, state text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code integer, response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE scene_assets.outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id text NOT NULL, aggregate_id text NOT NULL  /* scene_id */, aggregate_sequence bigint NOT NULL,
  event_type text NOT NULL, partition_key text NOT NULL, payload jsonb NOT NULL,
  headers jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON scene_assets.outbox (id) WHERE published_at IS NULL;

CREATE TABLE scene_assets.inbox (
  consumer text NOT NULL, event_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

`partition_key = tenant_id || ':' || aggregate_id`，中继按它单线程发布（[S08 §6.4](S08-revision-snapshot.md)）。

### 2.9 不建的表

- ❌ `scene_prop`（可移动道具归 S14）。
- ❌ `scene_occupant` / `scene_story_state`（归 S16）。
- ❌ `scene_shot`（机位归 S09）。
- ❌ `scene_axis_violation`（越轴结论归 S17）。
- ❌ `scene_rights_status`（只存指针）。
- ❌ 3D 模型/点云表（本文不做 3D，见 §10 C13-3）。

---

## 3. 状态机

### 3.1 候选

```
PENDING ──accept──► ACCEPTED
   ├──reject──► REJECTED
   └──ttl────► EXPIRED
```

### 3.2 场景与 canonical

```
active ──deprecate──► deprecated ──tombstone──► tombstoned
```

### 3.3 拓扑变更的特殊路径

```
revision N (topology_hash H1)
   │
   ├── 补图/换光照，拓扑不变 ──► revision N+1 (topology_hash H1, topology_changed=false)
   │
   └── 改门位/改隔断，拓扑变 ──► revision N+1 (topology_hash H2, topology_changed=true)
                                    │
                                    └── 必须触发 S07/S09 影响分析，且 policy_auto 禁止
```

`topology_changed=true` 的接受**强制要求** `decision_kind='human_approved'`，且必须带 `impact_plan_ref`（S07/S09 的影响计划引用）。没有影响计划就不接受。

这条是 docs/10 "迁移旧镜头要有S07/S09影响计划"在场景上的最硬落点，因为场景拓扑变了，所有在该场景生成的镜头都可能作废。

### 3.4 非法转移

| 从 | 到 | 原因 |
|---|---|---|
| `tombstoned` | `active` | 复活走新 ID |
| revision 覆写 | — | 不可变 |
| `topology_changed=true` + `policy_auto` | 接受 | 拓扑变更必须人审 + 影响计划 |
| `topology_changed` 声明与实际不符 | 接受 | 封存时校验，不一致 422 |
| `primary_set` 缺拓扑 | 接受为 canonical | 主场景必须有完整空间 |
| `tombstoned` 场景 | 新 canonical | 已下线 |

---

## 4. 关键流程

### 4.1 起草候选

```
POST /scene-assets/{id}/candidates
{
  command_id, expected_current_revision, source_kind,
  era_snapshot, topology,
  anchor_manifest, lighting_manifest, lighting_axes,
  fixed_objects, rights_ref, quality_gate_ref,
  topology_changed: bool
}
```

事务外校验（全部在 S13 本地完成，不调下游）：

1. **拓扑合法性**：unit polygon 闭合、不重叠（除相邻共享边）、opening 两端 unit 存在、opening 的 `wall_segment` 落在 unit 边界、`walkable` 覆盖非墙 unit、`orientation.north_axis_deg` 存在。失败 → 422 `TOPOLOGY_INVALID {opening_id|unit_id, reason}`。
2. **anchor 覆盖**：按 `location_class` 检查最低 anchor 数。`primary_set` 要求 ≥4 向（覆盖 360°），`recurring_location` ≥2，`one_off` ≥1。失败 → 422 `ANCHOR_COVERAGE_INSUFFICIENT {required, actual}`。
3. **anchor 相机与 unit 一致**：每个 anchor 的 `position_m` 必须在其 `unit_id` 的 polygon 内（或边界容差内）。失败 → 422 `ANCHOR_OUTSIDE_UNIT {anchor_id}`。
4. **光照覆盖**：`lighting_axes` 里每个值，每个 anchor 都必须有变体。失败 → 422 `LIGHTING_COVERAGE_INCOMPLETE {anchor_id, lighting}`。
5. **artifact 校验**（S28 `HEAD`）：每个 artifact 存在且 hash 一致。
6. **固定物足迹**：`footprint` 必须落在其 `unit_id` 内。失败 → 422 `FIXED_OBJECT_OUT_OF_BOUNDS {object_id}`。
7. **topology_changed 自洽**：与 parent 的 `topology_hash` 比对；声明 false 但 hash 变了 → 422 `TOPOLOGY_CHANGE_NOT_DECLARED`；声明 true 但 hash 相同 → 422 `TOPOLOGY_CHANGE_DECLARED_BUT_NONE`。

事务内：锁 `scene_asset` → CAS → 写候选。

### 4.2 接受候选

```
POST /scene-assets/{id}/candidates/{cid}:accept
{command_id, expected_current_revision, set_canonical, decision_kind, reason_text, impact_plan_ref}
```

事务内：

```
BEGIN;
-- 1) 锁 scene_asset，CAS expected_current_revision
-- 2) 锁 candidate，state='PENDING'
-- 3) topology_changed=true：
--      decision_kind != 'human_approved' → 422 TOPOLOGY_CHANGE_REQUIRES_HUMAN
--      impact_plan_ref IS NULL           → 422 IMPACT_PLAN_REQUIRED
-- 4) location_class='primary_set' 且 canonical 待设：
--      spatial_check.topology_valid != true → 422 TOPOLOGY_INVALID_FOR_PRIMARY
-- 5) INSERT scene_revision (revision=current+1)
--    hash 冲突 → 409 DUPLICATE_SCENE_CONTENT
-- 6) 批量 INSERT scene_anchor / scene_lighting_variant /
--    scene_fixed_object / anchor_relationship（从 payload 展开）
-- 7) set_canonical → supersede 旧 pointer，插新 pointer，更新 scene_asset
-- 8) UPDATE candidate.state='ACCEPTED'
-- 9) INSERT outbox (asset.scene.revision.accepted, seq=rev)
COMMIT;
```

第 6 步的展开是**派生**，可从 `payload` 重建（`rebuild-scene-derivations` runbook）。派生表用于查询，`payload` 是权威。

### 4.3 读路径

```
GET /scene-assets/{id}
GET /scene-assets/{id}/revisions/{revision}
GET /scene-assets/{id}/revisions/{revision}/anchors?direction=front&lighting=night
GET /scene-assets/{id}/revisions/{revision}/topology
GET /scene-assets/{id}/revisions/{revision}/fixed-objects?unit_id=
GET /scene-assets/{id}/canonical
GET /scene-assets:lookup?series_scope=&alias=
```

响应（`/topology`）：

```jsonc
{
  "scene_ref":{"service_id":"S13","id":"scn_office_3f","revision":4,"content_hash":"…"},
  "topology_hash":"…",
  "topology":{...},                         // 见 §2.3
  "fixed_objects":[{"object_id":"desk_1","blocks_walkable":true,"footprint":[...]}],
  "anchor_cameras":{"front":{"position_m":{...},"yaw_deg":0,...}},
  "era":{"label":"contemporary","year_band":"2015-2025"},
  "rights_recheck_required": true
}
```

响应（`/anchors`）：

```jsonc
{
  "scene_ref":{...},
  "anchor":{"anchor_id":"from_door","position_m":{...},"yaw_deg":90,"artifacts":{
     "day":{"service_id":"S28","id":"art_...","content_hash":"…"},
     "night":{...}}},
  "is_canonical": true,
  "canonical_revision": 4
}
```

读路径硬约束：

- **请求不存在的 lighting** → 422 `LIGHTING_NOT_AVAILABLE {lighting, available}`，不静默退回 `day`。退回会让夜景镜头用白天光照生成，且 S17 可能判"通过"。
- **请求不存在的 anchor** → 404 `ANCHOR_NOT_FOUND`。
- 跨租户 → 404（E28）。

### 4.4 失败矩阵

| # | 场景 | 行为 | 错误码 |
|---|---|---|---|
| F1 | 拓扑非法（opening 悬空/墙段不在边界） | 拒候选 | 422 `TOPOLOGY_INVALID` |
| F2 | anchor 数不足 | 拒 | 422 `ANCHOR_COVERAGE_INSUFFICIENT` |
| F3 | anchor 不在其 unit 内 | 拒 | 422 `ANCHOR_OUTSIDE_UNIT` |
| F4 | 光照轴缺变体 | 拒 | 422 `LIGHTING_COVERAGE_INCOMPLETE` |
| F5 | 固定物越界 | 拒 | 422 `FIXED_OBJECT_OUT_OF_BOUNDS` |
| F6 | 拓扑变了但声明未变 | 拒 | 422 `TOPOLOGY_CHANGE_NOT_DECLARED` |
| F7 | 声明变了但实际未变 | 拒 | 422 `TOPOLOGY_CHANGE_DECLARED_BUT_NONE` |
| F8 | 拓扑变更 + `policy_auto` | 拒 | 422 `TOPOLOGY_CHANGE_REQUIRES_HUMAN` |
| F9 | 拓扑变更缺影响计划 | 拒 | 422 `IMPACT_PLAN_REQUIRED` |
| F10 | 主场景无拓扑 | 拒 canonical | 422 `TOPOLOGY_INVALID_FOR_PRIMARY` |
| F11 | 读时请求不存在的光照 | 拒 | 422 `LIGHTING_NOT_AVAILABLE` |
| F12 | 读时请求不存在的 anchor | 拒 | 404 `ANCHOR_NOT_FOUND` |
| F13 | artifact 缺失/hash 不符 | 拒 | 422 `ARTIFACT_NOT_FOUND` / `ARTIFACT_HASH_MISMATCH` |
| F14 | 同内容重复接受 | 拒 | 409 `DUPLICATE_SCENE_CONTENT` |
| F15 | `expected_current_revision` 落后 | 拒 | 409 `VERSION_CONFLICT` |
| F16 | S28 超时 | 不落候选 | 503 `DEPENDENCY_UNAVAILABLE` |
| F17 | 跨租户 | 404 | `NOT_FOUND` |
| F18 | 两个并发接受 | 一成功一 409 | 行锁 |

---

## 5. 对外契约

### 5.1 命令

| 方法 | 路径 |
|---|---|
| POST | `/scene-assets` |
| POST | `/scene-assets/{id}/candidates` |
| POST | `/scene-assets/{id}/candidates/{cid}:accept` |
| POST | `/scene-assets/{id}/candidates/{cid}:reject` |
| POST | `/scene-assets/{id}:set-canonical` |
| POST | `/scene-assets/{id}:deprecate` / `:tombstone` / `:merge` |

### 5.2 查询

见 §4.3。

### 5.3 事件

发布（`drama.scene-assets.v2`，partition key `tenant_id:scene_asset_id`，`aggregate_sequence = revision`）：

```jsonc
{
  "specversion":"1.0","schema_version":"2.0","id":"evt_...","source":"S13",
  "type":"asset.scene.revision.accepted",
  "subject":"scene/scn_office_3f","time":"2026-10-31T09:00:00Z",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"scn_office_3f","aggregate_sequence":4,
  "correlation_id":"cmd_...","causation_id":"cmd_...","traceparent":"00-...",
  "data":{
    "scene_ref":{"service_id":"S13","id":"scn_office_3f","revision":4,"content_hash":"…"},
    "topology_hash":"…",
    "topology_changed": true,
    "impact_plan_ref":{"service_id":"S09","id":"impact_01J...","revision":1},
    "became_canonical": true,
    "canonical_revision": 4,
    "location_class":"primary_set",
    "era":{"label":"contemporary"},
    "lighting_axes":["day","night","dusk"],
    "anchor_ids":["front","back","left","right","from_door"],
    "rights_ref":{...}
  },
  "data_ref":null
}
```

`topology_hash` 与 `topology_changed` **必须**在事件里。订阅方（S09 依赖图、S17 缓存）靠它决定是否要重算空间检查。`impact_plan_ref` 在拓扑变更时必填，让消费者能追溯"这次变更经过了影响分析"。

消费：

| 事件 | 来源 | 处理 |
|---|---|---|
| `artifact.archived` | S28 | 审计 |
| `asset.prop.revision.accepted` | S14 | 若道具被误登记为固定物，仅记录审计告警（不自动迁移） |

**不消费** S16 的剧情事件、S09 的镜头事件。场景的空间属性不因剧情或分镜而变。

### 5.4 错误码汇总

| HTTP | code | retryable |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | false |
| 403 | `TENANT_SCOPE_DENIED` | false |
| 404 | `NOT_FOUND` / `ANCHOR_NOT_FOUND` / `CANONICAL_NOT_SET` | false |
| 409 | `VERSION_CONFLICT`(true) / `CANDIDATE_NOT_PENDING` / `DUPLICATE_SCENE_CONTENT` / `IDEMPOTENCY_KEY_REUSED` | 见括号 |
| 422 | `TOPOLOGY_INVALID` / `ANCHOR_COVERAGE_INSUFFICIENT` / `ANCHOR_OUTSIDE_UNIT` / `LIGHTING_COVERAGE_INCOMPLETE` / `LIGHTING_NOT_AVAILABLE` / `FIXED_OBJECT_OUT_OF_BOUNDS` / `TOPOLOGY_CHANGE_NOT_DECLARED` / `TOPOLOGY_CHANGE_DECLARED_BUT_NONE` / `TOPOLOGY_CHANGE_REQUIRES_HUMAN` / `IMPACT_PLAN_REQUIRED` / `TOPOLOGY_INVALID_FOR_PRIMARY` / `ARTIFACT_NOT_FOUND` / `ARTIFACT_HASH_MISMATCH` / `SCENE_TOMBSTONED` | false |
| 429 | `WRITE_BACKPRESSURE` | true |
| 503 | `DEPENDENCY_UNAVAILABLE` | true |

---

## 6. 幂等与并发

### 6.1 幂等

- 键 `(tenant_id, command_id)` + JCS hash。
- `POST /scene-assets` 自然幂等：`(tenant_id, series_scope, display_name)` 唯一。

### 6.2 并发

| 竞争 | 机制 |
|---|---|
| 两次接受 | `scene_asset` 行 `FOR UPDATE` |
| 两次设 canonical | `scene_canonical_one` 部分唯一索引 |
| 同内容重复 | `sr_hash_uniq` |
| 拓扑变更与影响计划未完成并发 | 接受时校验 `impact_plan_ref` 存在性**仅形态**；计划实际完成的复核归 S09 |

**F18 补充：** S13 只校验 `impact_plan_ref` 有值，不校验影响计划已执行完毕。这是刻意的 —— 影响计划执行是 S07/S09 的事，S13 如果等它完成才能接受新拓扑，就形成了跨服务同步阻塞。但这也意味着**存在一个窗口**：新拓扑已 canonical，旧镜头尚未按影响计划处理。这个窗口由 S09 的追踪暴露，不由 S13 阻止。这条取舍必须写进文档，否则实施者会以为可以靠 S13 保证一致。

### 6.3 锁顺序

```
1. scene_asset (FOR UPDATE)
2. scene_candidate (FOR UPDATE)
3. canonical_pointer
4. scene_revision (INSERT)
5. scene_anchor / scene_lighting_variant / scene_fixed_object / anchor_relationship (INSERT)
6. outbox
```

---

## 7. 可观测

指标：

- `s13_candidate_total{result}`、`s13_accept_total{set_canonical,location_class}`
- `s13_topology_invalid_total{reason}`、`s13_anchor_coverage_insufficient_total{location_class}`
- `s13_topology_change_total{class=changed|unchanged}`、`s13_topology_change_requires_human_total`
- `s13_lighting_not_available_total{lighting}`
- `s13_anchor_outside_unit_total`
- `s13_canonical_change_total{decision_kind}`

告警：

- `s13_topology_change_total{class="changed"}` > 0 且无 `impact_plan_ref` → **不该发生**（门失效）。
- `s13_lighting_not_available_total` 上升 → 分镜在要求场景没有的光照。
- `s13_anchor_outside_unit_total` > 0 被接受 → 相机参数与拓扑不一致，S17 的空间检查会全部失真。

Runbook：

- `rebuild-scene-derivations`：从 `scene_revision.payload` 重建 anchor / lighting / fixed_object / anchor_relationship，与库中比对。
- `topology-diff`：给两个 revision，输出 unit/opening 差异（供 S09 影响分析引用）。
- `topology-audit`：列出所有 `topology_changed=true` 的 revision，确认都有 `impact_plan_ref`。
- `rehash-revision`：校验 `content_hash` / `topology_hash`。

---

## 8. 测试清单

### E02 · 三集连续

| E02-D1 同一场景三集 canonical 不变 | 三集同一 `scene_ref`，`topology_hash` 相同 |
| E02-D2 第 3 集夜景使用 night 变体 | 返回 night artifact；`LIGHTING_NOT_AVAILABLE` 不触发 |
| E02-D3 第 1–3 集门位一致 | 三集读出的 `openings[door_1].wall_segment` 相同 |
| E02-D4 第 2 集补一张 `from_door` anchor（拓扑不变） | 第 2 集 revision+1，`topology_changed=false`；第 1、3 集不变 |

### E08 · 改前集 / E09 · 拓扑影响

| E08-D1 改门位 → 新 revision，`topology_changed=true` | 事件带 `impact_plan_ref`；旧集不变（S13 不改历史） |
| E08-D2 拓扑变更缺影响计划 | 422 |
| E08-D3 拓扑变更 `policy_auto` | 422 |
| E09-D1 S09 消费 `topology_changed` 事件 | 能拿到新旧 `topology_hash` 做差异 |
| E09-D2 补图不触发影响分析 | `topology_changed=false`；S09 不重算闭包 |

### E12 · 幂等

重复接受返回首次结果；同键不同 body 409；重复登记返回既有 ID。

### E28 · 跨租户

| E28-D1 跨租户读 topology | 404 |
| E28-D2 同名场景两租户 | 独立 |
| E28-D3 跨租户读 anchor | 404 |

### E30 · 权利

| E30-D1 授权撤销后仍返回 revision | `rights_recheck_required=true` |

### E24/E25 · QC

| E24-D1 空间不可解释（家具穿墙） | QC 报 `SPATIAL_INEXPLICABLE`；候选 REJECTED |
| E24-D2 门窗与轴线不一致 | QC 报 `OPENING_AXIS_MISMATCH`；拒 |
| E24-D3 多角度 anchor 不自洽（同一门在两个 anchor 的墙段不同） | 拒 |

### E05 · 策略门

| E05-D1 `canonical_asset` gate 需要场景 canonical | S13 `GET /canonical` 404 时 gate 判 `require_human`/`deny`，不是 pass |
| E05-D2 主场景无拓扑 | 无法成为 canonical，gate 必然失败 |

### E11 · 分支

| E11-D1 分支不复制场景资产 | 分支共享场景资产；分支只改 binding |
| E11-D2 分支实验改场景拓扑 | 新 revision 只对分支生效（由 binding 决定），正史 canonical 不变 |

### 通用

| 拓扑图校验（20 个随机有效/无效拓扑） | 有效全过，无效全拒且错误定位到具体 opening/unit |
| 相机在 unit 内 | 全部 anchor 通过 `ANCHOR_OUTSIDE_UNIT` 校验 |
| 光照矩阵全覆盖 | 每个 anchor × lighting 有 artifact |
| 并发接受 | 1 成功 |
| 崩溃恢复 | 派生表与 payload 一致 |
| 乱序事件（rev 5 先到） | 缓冲/回源 |
| 独立升级 | 旧 payload 可读；新增拓扑字段可选 |

---

## 9. 反模式

1. ❌ 在 S13 判越轴（归 S17）。
2. ❌ 在 S13 存机位/镜头（归 S09）。
3. ❌ 把可移动道具当固定物（§1.4 判据）。
4. ❌ 给固定物加状态字段（开/合/损坏 → 那是道具）。
5. ❌ 用自由文本"门在左边"代替拓扑坐标。
6. ❌ 拓扑变更时不发 `topology_hash`，只发 `content_hash`（下游无法区分"换了光照"和"门挪了"）。
7. ❌ 拓扑变更允许 `policy_auto` 或免影响计划。
8. ❌ 读不存在的 lighting 时静默退回 day。
9. ❌ 把 era 做成 revision 的轴（不同年代 = 不同场景）。
10. ❌ 原地替换 anchor（docs/10 明令不可变）。
11. ❌ 在 S13 维护"哪些镜头用了这个场景"（归 S10/S09）。
12. ❌ 做 3D 建模/点云（本文明确 2D 拓扑）。

---

## 10. 冲突（报告，不选边）

| # | 冲突 / 缺口 | 现状 | 本文用法 |
|---|---|---|---|
| C13-1 | `service.yaml` 只给 `POST /scene-assets/{id}/revisions` + `GET /scene-assets/{id}/revisions/{revision}`；没有 topology/anchors 子资源、没有候选/接受 | catalog | 按 §5 拆分；未改契约 |
| C13-2 | docs/10 场景质量门"空间可解释、门窗与轴线一致"没说由谁判定 | docs/10 | 本文假设 S33/S34 的 QC 产出 `spatial_check`，S13 只读结论；**另外** S13 自己做了**结构化**拓扑合法性校验（不需要视觉模型的那部分） |
| C13-3 | docs/10 未说明是否支持 3D/点云 | docs/10 | 本文明确只做 2D 拓扑 + anchor 相机参数；若需要 3D 需重新设计 |
| C13-4 | `event-topics.v2.json` 只有 `asset.scene.revision.accepted`，没有"拓扑变更"独立事件 | topics | 在同一事件带 `topology_hash`/`topology_changed`/`impact_plan_ref` |
| C13-5 | docs/10 说"多角度anchor"，未定义角度集合与相机参数 | docs/10 | 本文按 `location_class` 分级要求，并要求 yaw/pitch/hfov/position |
| C13-6 | docs/10 将"年代"列为场景内容，但未说明 era 变化是 revision 还是新场景 | docs/10 | 本文按 §1.3 判为"新场景"；需设计方确认 |
| C13-7 | S10 的 `variant_selector` 由 owner 解释；S13 的 lighting 轴与 S10 的 selector 语义需对齐 | docs/10 + S10 | 本文约定 `lighting` 作为 selector 键；需与 S10 契约统一 |
| C13-8 | `service.yaml` 的 `depends_on` 为 `[S28,S01]`，但拓扑变更实际依赖 S09 的影响计划 | catalog | 本文只校验 `impact_plan_ref` 形态，不做同步依赖；若要让 S13 阻断式等待需改 catalog |

---

## 11. 实施顺序与未决问题

### 11.1 顺序

1. **契约与几何模型先行**：`contracts/assets/scene-revision.v1.schema.json` + 拓扑 JSON Schema。拓扑的坐标单位、开口表示、朝向定义必须在写任何代码前定死 —— 它是 S09/S17 的共同输入，改一次要三方联动。
2. 拓扑校验库（纯函数，无数据库依赖）：polygon 闭合/重叠、opening 挂接、walkable 覆盖、anchor 在 unit 内。**必须有大量单元测试**，这是 S13 最容易出 bug 的地方。
3. 平台 SDK 复用（JCS/hash）。
4. 最小闭合：登记 + 起草候选（mock S28）+ 接受 + 读 topology/anchors。先验 E12/E28。
5. 接真实 S28 与 S33/S34 QC。
6. `anchor_relationship` 派生与 `topology-diff`（供 S09）。
7. 拓扑变更门（人审 + 影响计划引用）（最后）。

### 11.2 未决

1. **拓扑坐标是否要与真实美术资产对齐？** 若场景实际由 3D 模型/实景拍摄构成，2D 平面图可能与实际不符。当前假设"2D 拓扑是创作层面的约定"。
2. **`location_class` 的阈值是否需要按项目配置？** 本文写死（4/2/1 向）。大规模剧组可能需要可配。
3. **多楼层/室外大片场如何建模？** 当前 `units` 只能在单一楼层平面（`floor_z_band`）。多层楼需要层间连接（楼梯/电梯）的显式建模。
4. **拓扑变更后的旧镜头处理由谁驱动？** 本文只保证"新 revision 有影响计划引用"。实际重做由 S07/S09 编排。窗口期的处理策略需要设计方明确。
5. **`era` 的 `year_band` 是否与 S03/S04 剧本的时代标注统一？** 当前是自由 jsonb。
6. **背景场景是否也进 S10 binding？** §2.1 允许 `background` 无 canonical，但 binding 需要 revision。若背景不走 binding，S10 的 `unresolved_required_roles` 会把它们报成缺失。
