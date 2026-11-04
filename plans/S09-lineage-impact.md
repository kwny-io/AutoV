# S09 · 依赖溯源与影响分析 —— 实现方案

状态：实现方案（W1）。`maturity: design_only` 不变；本文描述"照着能写代码"的形态，不是已实现系统。

上游依据：[docs/01](../docs/01-enterprise-architecture.md) §3 唯一权威表、§5 技术基线；[docs/04](../docs/04-data-consistency.md) §1 两层 epoch、§2 修改与接受 Saga；[docs/05](../docs/05-cross-episode-memory.md) §5 改前集传播；[docs/07](../docs/07-production-workflows.md) §2 局部改镜、§3 跨集改稿；[docs/16](../docs/16-migration-acceptance.md) E03 / E08 / E11；[S09 规格](../docs/design-package/services/S09-lineage-impact.md)。

配套：[S08 版本快照](S08-revision-snapshot.md)、[S16 跨集记忆](S16-story-memory.md)、[S18 上下文检索](S18-context-retrieval.md)、[S07 变更管理](S07-change-management.md)。

---

## 0. 边界与一句话职责

S09 是**依赖关系**的唯一写者：谁引用了谁、引用的是哪个字段、这个引用在哪个水位成立。它回答三个问题：

1. 一个字段变了，**下游哪些节点必须重做**？
2. 一个记忆事实变了，**跨到哪些集、哪些上下文包、哪些剧本与镜头**？
3. 这个答案**基于哪一版输入**，还有没有没补齐的边？

S09 **不**拥有：

| 事实 | 归属 | S09 的姿势 |
|---|---|---|
| 剧本 / 分镜 / 资产 / 制品的**内容** | S04 / S06 / S10 / S28 | 只存 `service/id/revision/content_hash` 引用；**从不复制正文** |
| 正史 claim 与 checkpoint | S16 | 消费 `memory.canon.committed` 的种子，不写回 |
| 当前生产状态、manifest、accepted pointer | S08 | 只读 `node-diff` 与 manifest 引用 |
| 修改计划与 Saga | S07 | 输出 impact 结果，由 S07 组装 ChangePlan |
| 政策判定 | S02 | 不判"能不能改"，只判"改了影响谁" |
| 生成任务与费用 | S26 / S23 | 完全不涉及 |

判据：**"这条内容是什么"回到内容所有者；"这一集现在是什么"回到 S08；"改这里会影响谁"只能问 S09。** 任何服务自己算闭包，都是第二个影响分析权威——这正是 AGENTS.md 禁止的（见 [S16 §5.1](S16-story-memory.md) 的职责切分表）。

### 0.1 三件绝对不能做

1. **不持有事实原文**。表里只有引用、字段路径、值的 hash，以及一份白名单标量投影。剧本对白、prompt、claim 陈述文本、资产描述一律不落库。理由不只是容量：一旦存了正文，S09 就会开始"顺便"判断内容是否等价，从而变成第二个内容权威。
2. **不写 accepted pointer、不写 `node_epoch`、不写 `execution_fence`**。S09 的结论是**建议**（`regenerate_node_ids`），最终由 S08 在 `:commit` 里推进 epoch、在 `:accept` 里改指针。
3. **不在图不完整时承诺局部失效**。缺必需边或水位落后时，输出 `mode=CONSERVATIVE` 并把闭包放大到安全上界；宁可多算，不可少算。docs/04 §2 原文："依赖索引落后时先补齐必需边或保守重检，不能按不完整图承诺局部失效。"

### 0.2 两套 epoch 里 S09 的角色

这是本服务最容易写错的地方，先钉死。

| 概念 | 作用域 | 谁写 | S09 做什么 |
|---|---|---|---|
| `execution_fence` | run / branch | **S08**（收 S21 的 `:fence` 命令） | **只读、只记录**。每个 `impact_analysis` 固定它看到时的 fence；fence 变了，该 analysis 标 `STALE_FENCE`，不能复用。S09 永远不递增 fence。 |
| `node_epoch` | 单个 `logical_node` | **S08**（`input_fingerprint` 变化时 +1） | **不读不写**。S09 只输出 `node_id` 级别的 `REGENERATE/RECHECK/REUSE`，epoch 由 S08 自行决定。S09 引用 epoch 会立刻变成第二权威。 |
| `lineage_watermark` | series / branch，S09 自己 | S09 | 这是 S09 的**唯一**版本坐标，与上面两个无关，不要混称"epoch"。 |

三者的区分在 §1.4 展开。

---

## 1. 核心对象与关键区分

### 1.1 五个对象

| 对象 | 是什么 | 不是什么 |
|---|---|---|
| `lineage_edge` | 一条"上游 revision 的某个字段 → 下游 revision 的某个字段"的**有向依赖** | 不是内容副本，不是调度依赖 |
| `field_projection` | 某个 revision 的某个字段的 `value_hash`（+ 白名单标量） | 不是内容快照 |
| `impact_analysis` | 一次影响计算的**不可变结论** + 它基于的水位 | 不是计划、不是授权、不是执行 |
| `lineage_gap` | 已知"这里应该有一条边但没有"的记录 | 不是错误日志，是**必须被消费的阻断信号** |
| `projection_watermark` | 每个上游事件源已应用到第几个 sequence | 不是 epoch |

### 1.2 边的方向与种类

方向统一为 **producer → consumer**（上游 → 下游）。`edge_kind` 是闭集：

| `edge_kind` | source | target | `field_path` 示例 | 产生方式 |
|---|---|---|---|---|
| `PACK_USES_CLAIM` | S16 `claim` revision | S18 `context_pack` revision | `null` | S09 从 `context_pack.created.claim_refs` 推导 |
| `SCRIPT_DERIVED_FROM_PACK` | S18 pack | S04 `script` revision | `null` | S04 事件自报 |
| `SCENE_DERIVED_FROM_SCRIPT` | S04 script | S05 `scene_breakdown` | `null` | S05 事件自报 |
| `SHOT_DERIVED_FROM_SCENE` | S05 scene | S06 `shot_spec` revision | `null` | S06 事件自报 |
| `SHOT_FIELD_DEPENDS` | S06 shot 字段 | S06 shot 字段 | `/camera/angle` → `/beats/2/action` | S06 自报（DSL 派生） |
| `ARTIFACT_FROM_SHOT` | S06 shot | S28 artifact | `/dialogue` → `null` | 生成完成时自报 |
| `ASSET_BINDING` | S10 asset revision | S06 shot / S28 artifact | `null` | S10 binding_set 事件自报 |
| `MEMORY_DEPENDS` | S16 claim | S06 shot / S07 change plan | `null` | S09 从 `context_pack` 反查 |
| `AUDIO_FROM_DIALOGUE` | S06 `/dialogue` | S31 TTS artifact | `/dialogue` | S31 事件自报 |
| `SUBTITLE_FROM_AUDIO` | S37 alignment | S38 subtitle | `null` | S38 事件自报 |
| `EDIT_FROM_ARTIFACT` | S28 artifact | S41 EDL entry | `null` | S41 事件自报 |

**只登记跨"语义阶段"的边和显式的字段级依赖边**，不为每个 `if` 分支建边。边的粒度决定闭包的可信度：太粗会让一次改镜重做整集，太细会漏。

`field_path` 是 JSON Pointer（RFC 6901），指向**下游对象里受影响的字段**。`null` 表示对象级依赖（整个对象引用了上游）。

### 1.3 影响分析为什么分 REGENERATE / RECHECK / REUSE 三档

docs/07 §2 要求"复用未改文本/音色与足够短的 TTS；重算 EDL、字幕时码"。若只有"重做/不重做"两档，这句话无法表达：TTS 是否需要重做**取决于对白文本有没有变**，而不是镜头角度有没有变。

所以闭包里每个节点给一个 `disposition`：

| disposition | 含义 | 谁执行 |
|---|---|---|
| `REGENERATE` | 输入确实变了，必须重跑 | S21 → S26 → S27 |
| `RECHECK` | 输入可能变了，但产物**可能仍然可用**；需要 QC/连续性复检后再定 | S21 先跑 QC/连续性，不直接付费重生成 |
| `REUSE` | 输入未变（按字段 hash 判定），产物可直接引用 | S21 直接复用，**不计费** |

`RECHECK` 是成本控制的关键。以"缩 4 秒"为例：

- `shot_3_2.video` → `REGENERATE`（输入 `duration.target_seconds` 变了）
- `shot_3_2.tts` → `REUSE`（`/dialogue` 未变，且 `duration.trim_allowed=true` 时音色与语速不受时长影响）
- `shot_3_2.subtitle` → `REGENERATE`（时码依赖音频长度）
- `shot_3_2.keyframe` → `REUSE`（关键帧取首帧，`/camera/angle` 变了则必须变 → 本例中 `REGENERATE`）
- `ep3.master` → `REGENERATE`（依赖 EDL）
- `episode_3.continuity_report` → `RECHECK`

保守模式（`mode=CONSERVATIVE`）下，`RECHECK` 一律降级为 `REGENERATE`，`REUSE` 保留（因为 REUSE 的依据是字段 hash，不依赖图完整性）。这条是 §1.5 水位规则的落地。

### 1.4 三个"代"，不要混

| 名称 | 谁写 | 变的原因 | S09 里的表现 |
|---|---|---|---|
| `execution_fence` | S08 | 取消 / 故障切换 / 分支废弃 | `impact_analysis.execution_fence` 只记录，变则 `STALE_FENCE` |
| `node_epoch` | S08 | 该节点 `input_fingerprint` 变 | **不出现**在 S09 的任何表里 |
| `lineage_watermark` | S09 | 每消费一个上游事件 sequence 就推进 | `impact_analysis.lineage_watermark` = 本次计算用的下界 |
| Temporal workflow generation | Temporal | Continue-As-New | 与本服务无关 |

`lineage_watermark` 的语义是"**下界**"：`analysis.lineage_watermark = min(各必需事件源已应用 sequence)`。它不表示"算得最新的"，只表示"至少基于这一版"。ChangePlan 用它做 `impact_watermark`（docs/04 §2 的必填字段），S07 在 `apply` 时校验它没有倒退。

### 1.5 水位落后的三种情形（E11 核心）

| 情形 | 判定 | 行为 |
|---|---|---|
| A. 事件源有积压但可追平 | `source.max_sequence - applied_sequence <= lag_budget` | 同步等待追平（有界，默认 2s），追平后 `mode=EXACT` |
| B. 积压超过预算或源不可达 | 同上超预算 / 5xx | `mode=CONSERVATIVE`，`watermark_source` 里标 `degraded:true`；闭包放大 |
| C. 必需边缺失 | 沿闭包发现 `lineage_gap` 未关闭 | **阻断**（`IMPACT_INCOMPLETE`），除非调用方显式接受保守模式 |

"保守"的定义必须可验证：保守模式下 S09 把闭包**向上合并整个 episode 的所有 shot 节点**，并把所有 `RECHECK` 提为 `REGENERATE`。这是一个可证明的安全上界（因为 episode 是最大的语义单元），代价是成本。S09 不发明"部分保守"——部分保守无法证明不漏。

`IMPACT_INCOMPLETE`（情形 C）是 409，`retryable:true`，带 `gap_ids[]`。**S09 不允许在没有调用方同意的情况下静默降级成保守模式**：那会让 S07 展示一个它没算过的成本区间。

---

## 2. 数据模型

数据库 `lineage_impact`，Postgres 16。全部表带 `tenant_id`，所有查询强制带 `tenant_id` 谓词（跨租户一律 404，见 §5.6）。无金额。

### 2.1 `lineage_edge` —— 边本身

```sql
CREATE TABLE lineage_edge (
  tenant_id         text NOT NULL,
  branch_id         text NOT NULL,
  edge_id           text NOT NULL,
  edge_kind         text NOT NULL,
  source_service_id text NOT NULL,
  source_id         text NOT NULL,
  source_revision   integer NOT NULL,
  source_hash       text NOT NULL,
  target_service_id text NOT NULL,
  target_id         text NOT NULL,
  target_revision   integer NOT NULL,
  target_hash       text NOT NULL,
  field_path        text,
  origin            text NOT NULL,      -- EVENT_DERIVED | SELF_REPORTED | BACKFILL
  origin_event_id   text,
  valid_from        bigint NOT NULL,    -- 本 branch 的投影水位
  valid_to          bigint,             -- NULL = 当前生效
  created_at        timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (tenant_id, edge_id),
  CONSTRAINT edge_kind_closed CHECK (edge_kind IN (
    'PACK_USES_CLAIM','SCRIPT_DERIVED_FROM_PACK','SCENE_DERIVED_FROM_SCRIPT',
    'SHOT_DERIVED_FROM_SCENE','SHOT_FIELD_DEPENDS','ARTIFACT_FROM_SHOT',
    'ASSET_BINDING','MEMORY_DEPENDS','AUDIO_FROM_DIALOGUE',
    'SUBTITLE_FROM_AUDIO','EDIT_FROM_ARTIFACT')),
  CONSTRAINT edge_field_path_shape CHECK (
    field_path IS NULL OR (field_path ~ '^(/[^/]*)+$' AND length(field_path) <= 256)),
  CONSTRAINT edge_valid_range CHECK (valid_to IS NULL OR valid_to > valid_from),
  CONSTRAINT edge_not_self CHECK (
    NOT (source_service_id = target_service_id
         AND source_id = target_id
         AND source_revision = target_revision)),
  CONSTRAINT edge_hash_shape CHECK (
    source_hash ~ '^[0-9a-f]{64}$' AND target_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT edge_revision_positive CHECK (source_revision >= 1 AND target_revision >= 1)
);

-- 向下求闭包（最热）：按 source 找 target
CREATE INDEX lineage_edge_down_idx
  ON lineage_edge (tenant_id, branch_id, edge_kind, source_service_id, source_id, source_revision)
  WHERE valid_to IS NULL;

-- 向上溯源：按 target 找 source
CREATE INDEX lineage_edge_up_idx
  ON lineage_edge (tenant_id, branch_id, edge_kind, target_service_id, target_id, target_revision)
  WHERE valid_to IS NULL;

-- 字段级：只查某字段的依赖
CREATE INDEX lineage_edge_field_idx
  ON lineage_edge (tenant_id, branch_id, target_service_id, target_id, field_path)
  WHERE field_path IS NOT NULL AND valid_to IS NULL;

-- 同一条边在一个水位只能有一条生效记录
CREATE UNIQUE INDEX lineage_edge_active_uniq ON lineage_edge (
  tenant_id, branch_id, edge_kind, source_service_id, source_id, source_revision,
  target_service_id, target_id, target_revision, coalesce(field_path, '')
) WHERE valid_to IS NULL;

-- 事件来源去重（同一事件重放不产生第二条边）
CREATE UNIQUE INDEX lineage_edge_origin_uniq
  ON lineage_edge (tenant_id, origin_event_id)
  WHERE origin_event_id IS NOT NULL;
```

**`edge_id` 是确定性的**：`sha256(tenant|branch|kind|source_key|target_key|field_path|valid_from)`，`source_key = service_id/id/revision`。确定性 id 让重放天然幂等：同一事件算出的 `edge_id` 相同，`ON CONFLICT DO NOTHING` 即可。这不是"借用 hash 当幂等键"——命令级幂等仍由 `idempotency_record` 负责（§6.1）。

**边是有生命周期的，不是 append-only 到无限**：内容的 revision 变了，旧边 `valid_to = 新水位`，新边 `valid_from = 新水位`。历史边保留（可回答"上一版为什么重做了这些"），但闭包只看 `valid_to IS NULL`。这与 S08 的"回滚生成新 manifest"同构：关闭有效期，不改历史行。

**`SHOT_FIELD_DEPENDS` 是唯一允许同服务内自环的 kind**，由 `edge_not_self` 之外的语义保证：`source_revision <> target_revision` 或 `field_path` 不同即可（约束只禁止完全同一对象）。

### 2.2 `field_projection` —— 字段 hash 与白名单标量

```sql
CREATE TABLE field_projection (
  tenant_id   text NOT NULL,
  branch_id   text NOT NULL,
  service_id  text NOT NULL,
  object_id   text NOT NULL,
  revision    integer NOT NULL,
  field_path  text NOT NULL,
  value_type  text NOT NULL,         -- string|number|boolean|array|object|null
  value_hash  text NOT NULL,         -- sha256(JCS(value))
  scalar_value jsonb,                -- 仅白名单允许
  projected_at timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (tenant_id, service_id, object_id, revision, field_path),
  CONSTRAINT fp_hash_shape CHECK (value_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT fp_path_shape CHECK (field_path ~ '^(/[^/]*)+$'),
  CONSTRAINT fp_scalar_shape CHECK (
    scalar_value IS NULL OR jsonb_typeof(scalar_value) IN ('string','number','boolean'))
);

CREATE INDEX fp_lookup_idx ON field_projection
  (tenant_id, service_id, object_id, revision);
```

**白名单是数据库约束，不是代码约定**：

```sql
CREATE TABLE scalar_projection_whitelist (
  service_id  text NOT NULL,
  field_path  text NOT NULL,
  value_type  text NOT NULL,
  note        text NOT NULL,
  PRIMARY KEY (service_id, field_path)
);

INSERT INTO scalar_projection_whitelist VALUES
  ('S06','/camera/angle','string','分镜机位角，用于判定是否重做关键帧'),
  ('S06','/duration/target_seconds','number','目标时长，用于 TTS 复用判定'),
  ('S06','/duration/trim_allowed','boolean','时长可裁性'),
  ('S06','/duration/timing_policy','string','时序策略枚举'),
  ('S06','/order','number','镜头顺序'),
  ('S04','/scene_count','number','场次总数，仅结构统计');
```

写入时由触发器校验：

```sql
CREATE OR REPLACE FUNCTION fp_enforce_whitelist() RETURNS trigger AS $$
BEGIN
  IF NEW.scalar_value IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM scalar_projection_whitelist w
     WHERE w.service_id = NEW.service_id AND w.field_path = NEW.field_path
  ) THEN
    RAISE EXCEPTION 'SCALAR_PROJECTION_NOT_ALLOWED: % %', NEW.service_id, NEW.field_path
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
```

**为什么允许这六个标量**：闭包判定需要读值，而不只是 hash。`/duration/trim_allowed=true` 是"TTS 可复用"的前提；只有 hash 时 S09 只能说"变了"，说不出"变了以后能不能复用"。这六个字段是**结构性枚举/数值**，不含创作内容，不构成"持有事实原文"。

对白、prompt、claim 文本、资产描述**永远只有 hash**。想加白名单字段必须改这张表并写清理由——这是一次明确的决定，不是随手加。

### 2.3 `impact_analysis` —— 不可变结论

```sql
CREATE TABLE impact_analysis (
  tenant_id        text NOT NULL,
  analysis_id      text NOT NULL,
  branch_id        text NOT NULL,
  series_id        text NOT NULL,
  base_manifest_ref jsonb NOT NULL,        -- {id,revision,content_hash}
  execution_fence  bigint NOT NULL,        -- 快照值，只读
  seed_kind        text NOT NULL,          -- CHANGE_SEED | MEMORY_COMMIT | QUERY
  seed_ref         jsonb NOT NULL,
  request_hash     text NOT NULL,
  mode             text NOT NULL,          -- EXACT | CONSERVATIVE
  status           text NOT NULL,          -- COMPUTED | INCOMPLETE | FAILED | STALE_FENCE | EXPIRED
  lineage_watermark bigint NOT NULL,
  watermark_source jsonb NOT NULL,         -- {"S16":27,"S18":31,"S08":13,"S06":44}
  gap_ids          text[],                 -- INCOMPLETE 时非空
  result           jsonb,                  -- 见 §2.4 的聚合结构
  analysis_hash    text,                   -- sha256(JCS(result))
  expires_at       timestamptz NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  completed_at     timestamptz,

  PRIMARY KEY (tenant_id, analysis_id),
  CONSTRAINT ia_mode_closed CHECK (mode IN ('EXACT','CONSERVATIVE')),
  CONSTRAINT ia_status_closed CHECK (status IN
    ('COMPUTED','INCOMPLETE','FAILED','STALE_FENCE','EXPIRED')),
  CONSTRAINT ia_incomplete_has_gaps CHECK
    ((status = 'INCOMPLETE') = (gap_ids IS NOT NULL AND cardinality(gap_ids) > 0)),
  CONSTRAINT ia_computed_has_hash CHECK
    ((status IN ('COMPUTED','EXPIRED')) = (analysis_hash IS NOT NULL)),
  CONSTRAINT ia_watermark_positive CHECK (lineage_watermark >= 0),
  CONSTRAINT ia_ttl CHECK (expires_at > created_at)
);

CREATE INDEX ia_by_plan_idx ON impact_analysis
  (tenant_id, (seed_ref->>'plan_id'), created_at DESC)
  WHERE seed_kind = 'CHANGE_SEED';
CREATE INDEX ia_by_branch_idx ON impact_analysis
  (tenant_id, branch_id, created_at DESC);
CREATE UNIQUE INDEX ia_idempotent_uniq ON impact_analysis (tenant_id, request_hash);
```

`ia_idempotent_uniq` 让"同一次查询"永远只有一个 `analysis_id`。重试返回同一条，不重算——重算会得到不同的 `analysis_hash`，下游无法区分"图变了"与"我重试了"。

`result` 的聚合结构（`analysis_hash` 计算的是它）：

```jsonc
{
  "regenerate_node_ids": ["shot_3_2.video", "shot_3_2.keyframe", "ep3.master"],
  "recheck_node_ids":    ["ep3.continuity_report"],
  "reuse_artifact_refs": [
    {"service_id":"S31","id":"tts_3_2","revision":2,"content_hash":"…"}
  ],
  "affected_episode_ids": ["ep003"],
  "superseded_claim_refs": [],
  "lineage_watermark": 220,
  "derived_adjustments": [
    "SHOT_3_2/end_pause: 0.6s → 0.2s（时长由 6s 缩到 4s，结尾停顿按 timing_policy 派生）"
  ]
}
```

字段名与 [enterprise-change-plan.v2](../contracts/enterprise-change-plan.v2.schema.json) 的 `impact` 子对象**逐字对齐**。S07 可以直接把 `result` 塞进 ChangePlan 的 `impact`，不需要二次映射。这是刻意的：两份结构一旦分叉，"计划里的影响"和"分析算出的影响"就会开始不一致。

### 2.4 `impact_analysis_node` —— 逐节点明细与理由

```sql
CREATE TABLE impact_analysis_node (
  tenant_id    text NOT NULL,
  analysis_id  text NOT NULL,
  node_id      text NOT NULL,
  disposition  text NOT NULL,      -- REGENERATE | RECHECK | REUSE
  reason_code  text NOT NULL,      -- 闭集，见下
  path         jsonb NOT NULL,     -- 从 seed 到该节点的最短依赖链（边 id 列表）
  evidence     jsonb NOT NULL,     -- {field_paths:[...], value_hashes:{...}}

  PRIMARY KEY (tenant_id, analysis_id, node_id),
  CONSTRAINT ian_disposition_closed CHECK (disposition IN
    ('REGENERATE','RECHECK','REUSE')),
  CONSTRAINT ian_reason_closed CHECK (reason_code IN (
    'DIRECT_FIELD_CHANGED','UPSTREAM_REGENERATED','ASSET_BINDING_CHANGED',
    'CLAIM_SUPERSEDED','MEMORY_CHECKPOINT_MOVED','DURATION_DERIVED',
    'ORDER_CHANGED','AUDIO_LENGTH_CHANGED','TIMECODE_CHANGED',
    'REUSABLE_TEXT_UNCHANGED','REUSABLE_FIRST_FRAME_UNCHANGED',
    'CONSERVATIVE_OVERAPPROX','CONSERVATIVE_UPGRADE'))
);

CREATE INDEX ian_by_analysis_disp_idx
  ON impact_analysis_node (tenant_id, analysis_id, disposition);
```

`reason_code` 的用途不只是给人看：S39 修复计划与 S21 调度都会按 reason 分类（`REUSABLE_*` 说明是零成本复用，`CONSERVATIVE_*` 说明是"为了安全多做的"，成本报告里要单列）。**成本归因不应该靠解析自由文本。**

### 2.5 `lineage_gap` —— 缺失的边（阻断信号）

```sql
CREATE TABLE lineage_gap (
  tenant_id     text NOT NULL,
  gap_id        text NOT NULL,
  branch_id     text NOT NULL,
  expected_kind text NOT NULL,
  anchor_ref    jsonb NOT NULL,     -- 从哪个对象发现的缺口
  detected_by   text NOT NULL,      -- CLOSURE | RECONCILE | BACKFILL
  severity      text NOT NULL,      -- BLOCKING | DEGRADED
  resolved_at   timestamptz,
  resolved_edge_ids text[],
  first_seen_at timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (tenant_id, gap_id),
  CONSTRAINT gap_severity_closed CHECK (severity IN ('BLOCKING','DEGRADED')),
  CONSTRAINT gap_resolved_consistency CHECK
    ((resolved_at IS NULL) = (resolved_edge_ids IS NULL))
);

CREATE INDEX gap_open_idx ON lineage_gap (tenant_id, branch_id, severity)
  WHERE resolved_at IS NULL;
```

`BLOCKING` 的 gap 让闭包返回 `INCOMPLETE`；`DEGRADED` 的 gap 只让 `mode` 降为 `CONSERVATIVE`。

典型 BLOCKING gap：一个 `shot` 引用了 `asset_id`，但 `ASSET_BINDING` 边缺失——这时 S09 不知道资产换版会不会影响这个镜头，**必须阻断**。典型 DEGRADED gap：`SUBTITLE_FROM_AUDIO` 边缺失——字幕肯定要重算（保守重算即可证明安全），不需要阻断。

**gap 不是靠人工关的**：`resolved_at` 只在 backfill 写入边后自动置位（§4.4）。允许手工关 gap 就等于允许"我认为这没关系"，这正是 E11 要证明它不会发生的事。

### 2.6 `projection_watermark`

```sql
CREATE TABLE projection_watermark (
  tenant_id       text NOT NULL,
  branch_id       text NOT NULL,
  source_service  text NOT NULL,
  applied_sequence bigint NOT NULL,
  applied_event_id text,
  lag_budget_ms    integer NOT NULL DEFAULT 2000,
  updated_at      timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (tenant_id, branch_id, source_service),
  CONSTRAINT pw_sequence_positive CHECK (applied_sequence >= 0)
);
```

`lineage_watermark = min(applied_sequence)`，但**只对本次闭包实际用到的源取 min**，不是全局 min。一个从不消费 S41 的项目不该被 S41 的积压拖成 `CONSERVATIVE`。用到的源集合由 `seed_kind` 与遍历路径决定（§4.1 步骤 3）。

### 2.7 基础设施表

```sql
CREATE TABLE idempotency_record (
  tenant_id      text NOT NULL,
  command_id     text NOT NULL,
  request_hash   text NOT NULL,
  state          text NOT NULL,        -- IN_PROGRESS | DONE
  response_code  integer,
  response_body  jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz,
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE outbox (
  tenant_id          text NOT NULL,
  event_id           text NOT NULL,
  aggregate_id       text NOT NULL,
  aggregate_sequence bigint NOT NULL,
  event_type         text NOT NULL,
  partition_key      text NOT NULL,       -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  PRIMARY KEY (tenant_id, event_id)
);
CREATE INDEX outbox_pending_idx ON outbox (published_at) WHERE published_at IS NULL;

CREATE TABLE inbox (
  consumer   text NOT NULL,
  event_id   text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

`consumer` 必须按用途分开：`s09.edges.claim`、`s09.edges.pack`、`s09.edges.content`、`s09.manifest`、`s09.memory`。共用会造成"重放 edges 时挡住 memory"。

### 2.8 不建的表

| 不建 | 为什么 |
|---|---|
| `node` / `artifact` 内容表 | S09 不持有内容；节点身份来自 manifest 与各服务引用 |
| `impact_plan` | 那是 S07 的 `change_plan`。S09 只有 `impact_analysis` |
| `closure_cache` / 结果缓存 | 缓存的失效条件就是"图变了"，而图变化的判定成本等于重算。热路径用 `ia_idempotent_uniq` 覆盖 |
| `epoch` 表 | `lineage_watermark` 已在 `projection_watermark`；不自建 epoch |

---

## 3. 状态机

### 3.1 `impact_analysis` 状态机

```
          POST /impact-queries
                   │
                   ▼
              [查询幂等] ──同 request_hash 已 COMPUTED──► 返回原 analysis（replayed:true）
                   │
                   ▼
              COMPUTING（仅内存态，不落库）
                   │
      ┌────────────┼─────────────┬──────────────────┐
      ▼            ▼             ▼                  ▼
  COMPUTED     INCOMPLETE      FAILED         （超时）
      │            │             │                  │
      │            │             │                  ▼
      │            │             │              不落行；返回 503
      │            │             │              retryable:true
      ▼            ▼             ▼
   EXPIRED     （补边后重算）  （不可恢复错误）
```

| 从 | 到 | 触发 | 副作用 |
|---|---|---|---|
| — | `COMPUTED` | 闭包完成，无 BLOCKING gap | 写 outbox `lineage.impact.computed` |
| — | `INCOMPLETE` | 发现 BLOCKING gap | 不收费、不发事件；返回 409 |
| — | `FAILED` | 图结构非法（成环、kind 越界） | 发 `DEGRADED` 告警；返回 500，`retryable:false` |
| `COMPUTED` | `EXPIRED` | `expires_at` 到期（默认 30 min） | 不带副作用；S07 用它时必须重算 |
| `COMPUTED` | `STALE_FENCE` | S09 消费到 manifest 事件，fence 已前进 | 不带副作用；S07 看到的 `impact` 作废 |

**没有 `APPLIED` 状态**。S09 不知道也不关心影响结果有没有被应用——那是 S07 与 S08 的事。

### 3.2 边状态

边不是状态机，是有效期：

```
   (无) ──写边──► ACTIVE (valid_to IS NULL)
                    │
                    ├──上游 revision 前进──► SUPERSEDED (valid_to = 水位)
                    └──对象被删除/tombstone──► SUPERSEDED（不物理删）
```

**不物理删边**。删除历史边会让"上一版为什么重做了这些"无法回答，违反 docs/04 §5"最终交付的证据包固定 manifest+asset bindings"的可解释性要求。

### 3.3 `lineage_gap` 状态机

```
OPEN(BLOCKING | DEGRADED) ──backfill 写入边──► RESOLVED
        │
        └──长期未解（>24h）──► 升级告警，仍然 OPEN（不自动降级）
```

---

## 4. 关键流程

### 4.1 局部改镜影响闭包（E03 主路径）

输入：`{base_manifest_ref, execution_fence, seed:{service_id:"S06", id:"shot_3_2", revision:8, fields:["/camera/angle","/duration/target_seconds"]}, mode_hint:"EXACT"}`

**步骤：**

1. **组幂等**：`request_hash = sha256(JCS(body 去掉 command_id))`；命中 `ia_idempotent_uniq` 直接返回。
2. **读 manifest**：`GET /manifests/{id}/revisions/{revision}` 拿 `node_states[]` 与 `source_revisions[]`；同时记下 fence。若 fence 与请求不符 → 409 `STALE_EXECUTION_FENCE`（不是 S09 判的，是 S08 的读端已经对不上）。
3. **确定必需事件源集合**：
   - 只要走 `MEMORY_DEPENDS` / `PACK_USES_CLAIM` → 需要 S16、S18
   - 只要走 `ASSET_BINDING` → 需要 S10
   - 只要走 `ARTIFACT_FROM_SHOT` → 需要 S28
   - 恒需要 S06、S08
   取这些源的 `applied_sequence` 求 min → `lineage_watermark`；按 §1.5 判定 `EXACT` / `CONSERVATIVE`。
4. **种子解析**：把 `seed.fields` 落到 `field_projection`，取每个字段的 `value_hash` 与白名单标量。种子集合 = `{(shot_3_2, r8, /camera/angle), (shot_3_2, r8, /duration/target_seconds)}`。
5. **向下闭包（BFS）**：
   - 从 `SHOT_FIELD_DEPENDS` 找同 shot 内受影响字段（例如 `/duration/target_seconds` → `/beats/*/duration`、`/end_pause`）
   - 从 `ARTIFACT_FROM_SHOT` 找产物节点
   - 从 `AUDIO_FROM_DIALOGUE` / `SUBTITLE_FROM_AUDIO` / `EDIT_FROM_ARTIFACT` 逐层向下
   - 到 EDL / master 为止
   - **深度上限 12 层**；超过即 `FAILED(GRAPH_TOO_DEEP)`——闭包超过 12 层说明边建错了（正常情况下 8 层足够到 master）
6. **逐节点判定 disposition**（§4.2）
7. **跑一次向上校验**：对每个 `REUSE` 节点，向上反向走一遍，确认它**没有**经过任何已变化字段。这是 REUSE 的唯一依据，必须独立验证一次，不能只看 BFS 有没有到达。
8. **聚合**：写 `impact_analysis` + `impact_analysis_node`，同事务写 outbox `lineage.impact.computed`。
9. **返回** `result`。

**复杂度上限**：单次闭包最多遍历 `2 × 10^5` 条边、`2 × 10^4` 个节点。超过 → `FAILED(CLOSURE_TOO_LARGE)`，要求带 `node_id` 缩范围。这与 S08 `node-diff` 的 2000 上限同理：不做"自动分页影响分析"，分页会给出不自洽的闭包。

### 4.2 disposition 判定规则

对闭包内每个节点 `N`：

```text
若 N 在向下闭包中被"字段级变化"直接命中
    → REGENERATE, reason=DIRECT_FIELD_CHANGED

否则若 N 的上游是 REUSE，且 N 的字段级输入全部未变
    → REUSE, reason 按输入类型：
        REUSABLE_TEXT_UNCHANGED   （/dialogue 未变）
        REUSABLE_FIRST_FRAME_UNCHANGED（/camera/angle 未变）

否则若 N 依赖的上游是 REGENERATE，但 N 自身有"可裁窗口"
    且 scalar_projection 证明可裁（如 /duration/trim_allowed=true 且
      新时长仍 >= 对白最小可容时长）
    → RECHECK, reason=DURATION_DERIVED

否则若 N 是 QC/连续性报告类节点
    → RECHECK, reason=UPSTREAM_REGENERATED

否则
    → REGENERATE, reason=UPSTREAM_REGENERATED

CONSERVATIVE 模式的后处理（必须最后执行）：
    RECHECK → REGENERATE, reason=CONSERVATIVE_UPGRADE
    并并入整个 episode 的 shot 节点, reason=CONSERVATIVE_OVERAPPROX
```

**"对白最小可容时长"不是 S09 算的**。S09 没有对白文本（只有 hash）。它靠 S37 已发布的 `speech_duration_min_ms` 字段投影（白名单之外，通过 `field_projection` 的 `value_hash` + S37 的 `GET /alignments/{id}` 现查）。若查不到 → 该节点降为 `RECHECK`，不做"我猜 4 秒够"。

这条是 E04 的一半：**"4 秒容不下对白"必须由有文本的服务判定**（S06/S37），S09 只负责把"可能是这种情况"标出来。真正的澄清在 S07（§4.2 of S07 方案）。

### 4.3 跨集改稿（E08 主路径）

输入不是字段，是 S16 的 `memory.canon.committed`：

```jsonc
{
  "type": "memory.canon.committed",
  "data": {
    "branch_id": "main",
    "commit_sequence": 27,
    "changed_subject_ids": ["char_linlan"],
    "affected_story_window": {"clock_id":"story","tick_start":120,"tick_end":480},
    "superseded_claim_refs": [{"service_id":"S16","id":"claim_key_owner","revision":2,"content_hash":"…"}],
    "changed_claim_refs": [ ... ]
  }
}
```

**步骤：**

1. 用 `changed_claim_refs` + `superseded_claim_refs` 作为**种子**（注意：S16 已经给了"哪些 claim 变了"，S09 不重算这个——S16 算的是"记忆侧失效边界"，S09 算的是"依赖闭包"）。
2. 向上找哪些 `context_pack` 引用了这些 claim：`PACK_USES_CLAIM` 的 target 侧。
3. 从 pack 向下：`SCRIPT_DERIVED_FROM_PACK` → 剧本 → `SCENE_DERIVED_FROM_SCRIPT` → `SHOT_DERIVED_FROM_SCENE` → 产物。
4. 得到 `affected_episode_ids`（跨集！这是 E08 的核心产出）。
5. 对每集分别生成 disposition 集合。

**跨集时的额外规则：**

- `affected_episode_ids` 必须**同时**包含 S16 给出的 `affected_story_window` 覆盖的集，与图上算出的集。两者的并集是最终答案。差集非空 → 记 `DEGRADED` gap（说明 S16 与 S09 的边界认识不一致，需要人工看，但**不阻断**——阻断会让一次改稿卡死在两个服务的分歧上）。
- **已交付集**（S16 标 `memory_stale=true`）只出现在 `affected_episode_ids` 里，其节点一律 `RECHECK`，永不 `REGENERATE`。这是 docs/07 §3"已发布成片若不迁移，仍标记其历史 canon checkpoint"的落点。S09 不决定"要不要重做已交付的集"——那是 S07 拿给用户看的选项。

**S09 不消费 `memory.canon.committed` 就直接改闭包**：收到事件只做两件事——补边（把新 claim revision 的 `PACK_USES_CLAIM` 边写进去）与登记"有新的记忆变化"。闭包永远按需计算。理由：全量预计算会在一次改稿时遍历整个 series，而 docs/03 的量级是 5,000 活跃 Run、每日 50 万镜头任务；预计算撑不住，也没有必要。

### 4.4 边写入与 backfill（E11）

**三个写入来源：**

| 来源 | 触发 | 幂等键 |
|---|---|---|
| `EVENT_DERIVED` | 消费 `context.pack.created` 等事件 | `origin_event_id` 唯一索引 |
| `SELF_REPORTED` | 内容服务调 `POST /lineage-edges` 主动上报 | `(tenant, command_id, request_hash)` |
| `BACKFILL` | 水位落后时回源拉取 | 同 `SELF_REPORTED` |

**backfill 流程（情形 A 追平后仍缺边时）：**

1. 闭包发现 `lineage_gap(expected_kind='ASSET_BINDING', anchor_ref=shot_3_2)`
2. S09 调 S10 `GET /assets/{id}/resolve?at_revision=` 与 S06 `GET /shots/{id}` 拿 `asset_bindings[]`
3. 生成边，写库，`resolved_at = now()`
4. **重新计算闭包**（不是补上边就继续用旧的半成品结果）
5. 若回源也拿不到 → 保持 OPEN，返回 `INCOMPLETE`

**回源必须有界**：单次闭包最多回源 50 次、总耗时 3s。超时 → `INCOMPLETE`，不做"部分补齐后继续"。半补齐的图是最危险的：它看起来完整，实际漏掉了恰好超时的那几条。

### 4.5 失败矩阵

| # | 场景 | 检测 | 系统行为 | 用户/调用方看到 | 事后恢复 |
|---|---|---|---|---|---|
| F-01 | 请求重放（同 command_id 同 body） | `idempotency_record` DONE | 返回首次结果 | `replayed:true`，`analysis_id` 不变 | — |
| F-02 | 同 command_id 不同 body | hash 不同 | 拒绝 | 409 `IDEMPOTENCY_KEY_REUSED` | 换 command_id |
| F-03 | 同一次查询重试（不同 command_id，同语义） | `ia_idempotent_uniq` | 返回原 analysis | `replayed:true` | — |
| F-04 | 计算中进程崩溃 | 无 COMPUTING 行落库 | 无半成品 | 客户端超时后重试 | 重算（幂等） |
| F-05 | 写 `impact_analysis` 成功、outbox 未发 | relay 扫 `published_at IS NULL` | 补发 | 最终可见 | 自动 |
| F-06 | fence 在计算期间前进 | 写库前二次读 S08 头 | 标 `STALE_FENCE` | 409 `STALE_EXECUTION_FENCE` | S07 重建计划 |
| F-07 | 必需边缺失 | 闭包内 `lineage_gap` | 不透支 | 409 `IMPACT_INCOMPLETE` + `gap_ids` | backfill 后重算 |
| F-08 | 事件源积压超预算 | `applied_sequence` 落后 | `mode=CONSERVATIVE` | 200 但 `mode` 字段是 `CONSERVATIVE`，成本区间更大 | 追平后重算 |
| F-09 | 图出现环 | BFS 访问集检测 | 停止该分支，记 `DEGRADED` gap | 200，该分支标 `CONSERVATIVE` | 修边 |
| F-10 | 闭包超规模 | 边数/节点数上限 | 拒绝 | 422 `CLOSURE_TOO_LARGE` | 带 node 缩范围 |
| F-11 | S08 不可达 | 读 manifest 超时 | 不猜 | 503 `DEPENDENCY_UNAVAILABLE`，`retryable:true` | 重试 |
| F-12 | 事件乱序（后序先到） | aggregate sequence 缺口 | 缓冲，最多 30s | 无感知 | 回源补齐 |
| F-13 | 同一事件重放 | inbox + `origin_event_id` 唯一 | 忽略 | 无 | — |
| F-14 | 边指向未知 revision | 写入时校验 | **拒绝写入** | 500 `EDGE_DANGLING_REF`（这是上游 bug） | 上游修 |

F-06 的顺序很重要：**先算、后校验 fence 再写**。若在开始时校验一次就写，计算期间被取消的 run 依然会得到一个 `COMPUTED`，S07 会拿它去做计划——那是给已取消的 run 生成修改计划。

---

## 5. 对外契约

### 5.1 `POST /lineage-edges`（命令，自报名）

```jsonc
{
  "command_id": "cmd_01H...",
  "edges": [
    {
      "edge_kind": "ARTIFACT_FROM_SHOT",
      "source": {"service_id":"S06","id":"shot_3_2","revision":8,"content_hash":"…"},
      "target": {"service_id":"S28","id":"art_9f2","revision":1,"content_hash":"…"},
      "field_path": "/duration/target_seconds"
    }
  ]
}
```

响应 `200`：`{ "accepted": 3, "duplicates": 1, "superseded": 0, "edge_ids": ["e_..."] }`。

单次最多 500 条边（一个 shot revision 的完整 provenance 约 5–20 条）。超过 → 422 `BATCH_TOO_LARGE`。

**写入前的校验**（任一失败即整批拒绝，不做部分成功）：

1. `source` / `target` 的 revision ≥ 1，hash 为 64 位 hex
2. `edge_kind` 在闭集内
3. `field_path` 指向 `target` 对象里真实存在的字段（能证明时）——拿不到 target 对象则跳过此项，记 `DEGRADED` gap
4. `source` 的 revision 必须 ≤ 该服务当前已知的最新 revision（防止写"未来的边"）
5. `tenant_id` 与认证上下文一致

**整批原子**的原因：provenance 是一组，缺一条会让闭包悄悄漏节点。部分成功比全失败危险。

### 5.2 `POST /impact-queries`（查询，但有计算成本）

```jsonc
{
  "command_id": "cmd_01H...",
  "base_manifest_ref": {"id":"mf_ep3","revision":13,"content_hash":"…"},
  "seed": {
    "kind": "CHANGE_SEED",
    "refs": [
      {"service_id":"S06","id":"shot_3_2","revision":8,"content_hash":"…",
       "fields":["/camera/angle","/duration/target_seconds"]}
    ]
  },
  "mode_hint": "EXACT",          // EXACT | ALLOW_CONSERVATIVE
  "include_derived_adjustments": true
}
```

响应 `200`：

```jsonc
{
  "analysis_id": "ia_...",
  "analysis_hash": "…",
  "mode": "EXACT",
  "status": "COMPUTED",
  "lineage_watermark": 220,
  "watermark_source": {"S06":44,"S08":13,"S10":9,"S16":27,"S18":31,"S28":88},
  "impact": { /* 与 §2.3 result 完全一致 */ },
  "expires_at": "2026-11-01T09:41:00Z",
  "replayed": false
}
```

`mode_hint=EXACT` 时若只能保守 → **409 `CONSERVATIVE_REQUIRED`**，body 带 `would_be_mode:"CONSERVATIVE"` 与 `reason`。这是让 S07 显式选择"接受更大成本区间"还是"放弃这次修改"的钩子。默认 `EXACT`。

### 5.3 查询接口

| 方法 | 路径 | 用途 | 备注 |
|---|---|---|---|
| `GET` | `/impact-analyses/{analysis_id}` | 取结论（可被 S47 与审计复用） | 纯读，带 `source_watermark` |
| `GET` | `/impact-analyses?plan_id=&branch_id=&after=` | 列表 | 按 `seed_ref->>'plan_id'` |
| `GET` | `/lineage/edges?target_ref=&kind=&field_path=` | 溯源 | 上限 2000，超过 413 |
| `GET` | `/lineage/upstream?ref=&depth=` | 向上溯源（"这条内容来自什么"） | 深度上限 8 |
| `GET` | `/lineage/downstream?ref=&depth=` | 向下依赖（"改这个影响谁"） | 深度上限 12 |
| `GET` | `/projection/watermarks?branch_id=` | 水位可见 | E11 的调试入口 |
| `GET` | `/lineage-gaps?branch_id=&severity=` | 未解决的缺口 | 运维 |

**所有查询必须带 `tenant_id`**，跨租户返回 404（不是 403，避免泄漏存在性，E28）。

### 5.4 事件

**发布** `lineage.impact.computed`（topic `drama.lineage-impact.v2`，`aggregate_id = analysis_id`，`aggregate_sequence = 1`——每次分析只发一次）：

```jsonc
{
  "specversion":"1.0","schema_version":"2.0","id":"evt_...","source":"S09",
  "type":"lineage.impact.computed","subject":"impact/ia_...",
  "time":"2026-11-01T09:11:00Z",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"ia_...","aggregate_sequence":1,
  "correlation_id":"cmd_...","causation_id":"plan_...",
  "traceparent":"00-...-...-01",
  "data": {
    "analysis_id":"ia_...","analysis_hash":"…","mode":"EXACT",
    "base_manifest_ref":{…},"execution_fence":2,
    "lineage_watermark":220,
    "seed_kind":"CHANGE_SEED","seed_ref":{"plan_id":"plan_..."},
    "regenerate_node_ids":[…],"recheck_node_ids":[…],
    "affected_episode_ids":["ep003"],
    "superseded_claim_refs":[…],
    "expires_at":"2026-11-01T09:41:00Z"
  },
  "data_ref": null
}
```

消费者：S07（组装 ChangePlan，校验 `analysis_hash` 与自己拿到的一致）、S47（影响面板）。**S21 不消费**——S21 只从 S07 拿已提交的 ChangePlan 或从 S08 拿 manifest 事件。

**消费**的事件：

| 事件 | 源 | S09 的处理 | 幂等 |
|---|---|---|---|
| `context.pack.created` | S18 | 按 `claim_refs` 写 `PACK_USES_CLAIM`（`origin=EVENT_DERIVED`） | `origin_event_id` |
| `memory.canon.committed` | S16 | 更新 claim 最新 revision 索引；关闭被 supersede 的 `PACK_USES_CLAIM` 边；推进 `s09.memory` 水位 | inbox |
| `snapshot.manifest.committed` | S08 | 推进 `s09.manifest` 水位；把 `execution_fence` 变更记录下来（使在算的 analysis 变 stale） | inbox |
| `storyboard.shot.revision.created` | S06 | 写 `SHOT_DERIVED_FROM_SCENE` / `SHOT_FIELD_DEPENDS` | `origin_event_id` |
| `script.revision.created` | S04 | 写 `SCRIPT_DERIVED_FROM_PACK` | 同上 |
| `asset.binding.changed` | S10 | 写 / 关闭 `ASSET_BINDING` | 同上 |
| `artifact.archived` | S28 | 写 `ARTIFACT_FROM_SHOT` | 同上 |
| `alignment.completed` | S37 | 写 `AUDIO_FROM_DIALOGUE` / `SUBTITLE_FROM_AUDIO` | 同上 |

**S09 不消费 `generation.job.state-changed`。** 生成任务状态与依赖关系无关；消费它会让"任务完成"被误当成"依赖关系变化"。

### 5.5 错误码

| HTTP | code | retryable | 说明 |
|---|---|---|---|
| 400 | `INVALID_FIELD_PATH` | false | JSON Pointer 形态非法 |
| 403 | `TENANT_MISMATCH` | false | header 与认证上下文不一致 |
| 404 | `ANALYSIS_NOT_FOUND` / `REF_NOT_FOUND` | false | 跨租户同码 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同键不同 body |
| 409 | `IMPACT_INCOMPLETE` | true | 有 BLOCKING gap，带 `gap_ids` |
| 409 | `CONSERVATIVE_REQUIRED` | false | `mode_hint=EXACT` 但只能保守 |
| 409 | `STALE_EXECUTION_FENCE` | true | fence 在计算期间前进 |
| 409 | `ANALYSIS_EXPIRED` | true | `expires_at` 已过 |
| 413 | `EDGE_LIST_TOO_LARGE` | false | 单次 > 2000 条 |
| 422 | `CLOSURE_TOO_LARGE` | false | 闭包超规模 |
| 422 | `BATCH_TOO_LARGE` | false | 上报边 > 500 条 |
| 500 | `EDGE_DANGLING_REF` | false | 边指向未知 revision（上游 bug） |
| 500 | `GRAPH_CYCLE` | false | 成环 |
| 503 | `DEPENDENCY_UNAVAILABLE` | true | 上游不可达 |

---

## 6. 幂等与并发

### 6.1 命令级幂等（统一规则）

```
request_hash = sha256( JCS( body 去掉 command_id 与 request_hash ) )
```

JCS = RFC 8785。`(tenant_id, command_id)` 主键。

| 已有 | 本次 | 结果 |
|---|---|---|
| 无 | — | 执行，先插 `IN_PROGRESS` |
| `IN_PROGRESS` | 同 hash | 409 `COMMAND_IN_PROGRESS`，`retryable:true`，`Retry-After: 1` |
| `DONE` | 同 hash | 原样返回首次响应（`replayed:true`） |
| 任意 | 不同 hash | 409 `IDEMPOTENCY_KEY_REUSED`，`retryable:false` |

### 6.2 查询级幂等（S09 特有）

`POST /impact-queries` 有计算成本但没有副作用，所以除命令幂等外还有第二道：`ia_idempotent_uniq (tenant_id, request_hash)`。

**为什么两道都要**：命令幂等保证"同一次重试不重复计费"；查询幂等保证"两个不同 command_id 的等价查询得到同一个 `analysis_hash`"。后者是 S07 能在 `apply` 时校验 `plan_hash` 的前提——如果两次查询给不同 hash，S07 的计划就没法绑定到一份确定的 impact。

### 6.3 边的并发写入

两条边可能来自两个事件消费者（例如 `context.pack.created` 与 `memory.canon.committed`）同时写 `PACK_USES_CLAIM`。

- 唯一索引 `lineage_edge_active_uniq` 兜底 → `ON CONFLICT DO NOTHING`
- 关闭旧边用 `UPDATE ... WHERE valid_to IS NULL`，两个事务同时关 → 后到的影响 0 行，无害
- **不需要分布式锁**。边的正确性由唯一索引 + 有效期单调性保证

### 6.4 水位推进

`projection_watermark` 用**单调 CAS**：

```sql
UPDATE projection_watermark
   SET applied_sequence = $new, applied_event_id = $eid, updated_at = now()
 WHERE tenant_id = $t AND branch_id = $b AND source_service = $s
   AND applied_sequence < $new
```

`applied_sequence < $new` 是防倒退的关键。乱序事件先到不会把水位往回拉，只会被 inbox 记下后丢弃或缓冲（§4.5 F-12）。

### 6.5 读隔离

闭包计算在**一个 REPEATABLE READ 只读事务**里完成：

- 所有边的读取在同一个快照内 → 闭包自洽（不会出现"读到一半边被关掉"）
- 不用 SERIALIZABLE：闭包不写业务表，写的是新的 `impact_analysis` 行，不存在写冲突
- 唯一的例外是回源（§4.4）——它在事务外，回来后重新开事务重算

---

## 7. 可观测

### 7.1 指标

| 指标 | 类型 | 用途 |
|---|---|---|
| `s09_closure_duration_seconds{seed_kind,mode}` | histogram | 闭包耗时；`CONSERVATIVE` 明显更大是预期的 |
| `s09_closure_edges_scanned` | histogram | 图规模趋势 |
| `s09_closure_nodes{disposition}` | counter | REGENERATE/RECHECK/REUSE 比例；REUSE 骤降说明边建粗了 |
| `s09_impact_mode_total{mode}` | counter | `CONSERVATIVE` 占比是**核心健康指标**，> 5% 要查 |
| `s09_lineage_watermark_lag_seconds{source}` | gauge | 各源积压 |
| `s09_open_gaps{severity}` | gauge | `BLOCKING` > 0 持续 10 min 告警 |
| `s09_edge_write_total{origin,result}` | counter | `duplicates` 高说明上游重复发事件 |
| `s09_backfill_total{result}` | counter | 回源频率 |
| `s09_idempotent_replay_total` | counter | 重放率 |
| `s09_analysis_expired_total` | counter | 计划生成后动作太慢的信号 |

### 7.2 日志与追踪

- 每个 `analysis_id` 一条结构化摘要日志：`seed_kind`、`mode`、`watermark`、`edges_scanned`、`dispositions` 计数、`duration_ms`
- `traceparent` 从命令透传到回源调用（S06/S10/S28 的查询），这样"这次闭包为什么慢"能一跳到底
- **不打印边内容、不打印字段值**——只有 id 与 hash

### 7.3 每日对账

1. `projection_watermark.applied_sequence` 与各源 `max(sequence)` 的差（超过 1 小时告警）
2. 未解决的 `BLOCKING` gap 数量与年龄
3. `lineage_edge` 中 `valid_to IS NULL` 但 `target_revision` 已不是该对象的最新 revision 的边数（说明关边漏了）
4. `impact_analysis` 中 `status='COMPUTED'` 但已过 `expires_at` 的比例

第 3 项是最重要的：**漏关的边会让闭包多算**（安全方向），但持续增长说明事件消费有洞。

---

## 8. 测试清单

### 8.1 E03 · 仰拍 4 秒局部改镜

| 编号 | 场景 | 断言 |
|---|---|---|
| T09-01 `[E03]` | `/camera/angle` `eye_level → low` | 闭包含 `shot_3_2.keyframe`、`shot_3_2.video`、`ep3.master` 为 `REGENERATE`；不含 `shot_3_4.*` |
| T09-02 `[E03]` | `/duration/target_seconds` `6 → 4`，`trim_allowed=true`，对白最小 3.2s | `shot_3_2.tts` 为 `REUSE`（`/dialogue` 未变）；`shot_3_2.subtitle` 为 `REGENERATE` |
| T09-03 `[E03]` | 同上，但 `trim_allowed=false` | `shot_3_2.tts` 为 `RECHECK`，不是 `REUSE` |
| T09-04 `[E03]` | 对白最小 4.5s，目标 4s | 闭包不判"删台词"；`shot_3_2` 的部分节点标 `RECHECK`，`reason=DURATION_DERIVED`；整体不返回可行结论（由 S07 澄清） |
| T09-05 `[E03]` | 派生改动 | `derived_adjustments` 含 `end_pause` 的变化，且与 S06 的 `timing_policy` 一致 |
| T09-06 `[E03]` | 未改字段 | `shot_3_2` 的 `/dialogue` hash 不变 → 任何依赖它的 TTS 不出现 `DIRECT_FIELD_CHANGED` |

### 8.2 E08 · 改前集钥匙归属

| 编号 | 场景 | 断言 |
|---|---|---|
| T09-10 `[E08]` | S16 提交 `claim_key_owner` 新 revision | 闭包含 `ep003`、`ep004`；`affected_episode_ids` 与 S16 的 `affected_story_window` 求并后无遗漏 |
| T09-11 `[E08]` | 同一 claim 被两集引用 | 两集的 shot 都进闭包；不重复计（`impact_analysis_node` 主键去重） |
| T09-12 `[E08]` | 已交付集 `memory_stale=true` | 该集节点只出现 `RECHECK`，无 `REGENERATE` |
| T09-13 `[E08]` | S16 与 S09 边界不一致（差集非空） | 记 `DEGRADED` gap；闭包仍返回 `COMPUTED`（不阻断） |
| T09-14 `[E08]` | 改前集但下游未生成 | 下游节点进 `regenerate_node_ids`，但 S09 不预分配任何预算/任务 |
| T09-15 `[E08]` | `superseded_claim_refs` 为空但有 `changed_claim_refs` | 仍走完整闭包（新增事实也会影响下游） |

### 8.3 E11 · 记忆/依赖索引落后

| 编号 | 场景 | 断言 |
|---|---|---|
| T09-20 `[E11]` | S16 水位落后 500ms，预算 2s | 等待追平，返回 `mode=EXACT` |
| T09-21 `[E11]` | S16 水位落后 10s，超预算 | 返回 `mode=CONSERVATIVE`，闭包并入整个 episode，`RECHECK` 全升 `REGENERATE` |
| T09-22 `[E11]` | `mode_hint=EXACT` 且只能保守 | 409 `CONSERVATIVE_REQUIRED`，**不静默保守** |
| T09-23 `[E11]` | `ASSET_BINDING` 边缺失 | 409 `IMPACT_INCOMPLETE`，`gap_ids` 指向该 gap |
| T09-24 `[E11]` | 回源补齐后可算 | 重算得 `COMPUTED`；gap 置 `RESOLVED`；`analysis_hash` 与补齐前不同 |
| T09-25 `[E11]` | `SUBTITLE_FROM_AUDIO` 缺失（DEGRADED） | 不阻断，`mode=CONSERVATIVE` |
| T09-26 `[E11]` | 水位回退的乱序事件 | `applied_sequence` 不倒退 |
| T09-27 `[E11]` | 只有 S41 落后，闭包不涉及 S41 | 不降级（只对用到的源取 min） |

### 8.4 通用分布式验收

| 编号 | 场景 | 断言 |
|---|---|---|
| T09-30 `[E12]` | 同 command_id 同 body 重放 | `replayed:true`，`analysis_id` 不变，不新增 outbox |
| T09-31 `[E12]` | 同 command_id 不同 body | 409 `IDEMPOTENCY_KEY_REUSED` |
| T09-32 `[E12]` | 不同 command_id、同语义查询 | 同一 `analysis_id`、同一 `analysis_hash` |
| T09-33 `[E28]` | 跨租户读 `analysis_id` | 404，与不存在同码 |
| T09-34 `[E28]` | 跨租户写边 | 403 `TENANT_MISMATCH` |
| T09-35 `[E32]` | 重放全部历史事件 | 边与水位与重放前一致；`side_effects=false` 不发外部消息 |
| T09-36 `[E01]` | 独立升级 S09 | 不需要 S06/S10 同时升级；边契约向后兼容 |
| T09-37 崩溃 | 写 analysis 后立即 kill | outbox relay 补发事件；无重复事件（`aggregate_sequence=1` + 唯一约束） |
| T09-38 崩溃 | 边写入事务中途 kill | 整批回滚，无部分边 |
| T09-39 成环 | 人工构造 A→B→A | 标 `DEGRADED` 并停止该分支，不无限循环 |
| T09-40 规模 | 2 万条边闭包 | 在 `x-closure-budget`（默认 800ms）内完成或返回 `CLOSURE_TOO_LARGE` |

---

## 9. 实施顺序

| 阶段 | 内容 | 完成判据 |
|---|---|---|
| W1-a | 表结构 + 幂等 + outbox/inbox + `POST /lineage-edges` | T09-30/31/37/38 通过 |
| W1-b | `field_projection` + 白名单触发器 | 写非白名单标量被数据库拒绝 |
| W1-c | 事件消费：`context.pack.created`、`snapshot.manifest.committed` | 边与水位正确；T09-26 通过 |
| W1-d | 闭包引擎（`EXACT` 模式）+ `POST /impact-queries` | T09-01/02/05、T09-10/11 通过 |
| W1-e | `lineage_gap` + 保守模式 + 回源 backfill | T09-20/21/23/24/25、T09-22 通过 |
| W1-f | 其余事件的边自报（S06/S04/S10/S28/S37） | T09-06、T09-15 通过 |
| W1-g | 跨集闭包 + `affected_episode_ids` 并集规则 | T09-10~14 通过 |
| W1-h | 可观测 + 每日对账 | 指标齐备；对账 4 项无误报 |
| W2 | 换图引擎（可选，内部实现替换） | 外部契约零变更；T09-01~40 全部仍通过 |

**先做 W1-d 再做 W1-e 是错的**：没有 gap 检测的闭包会在缺边时静默给出"更小的闭包"，看起来更快更省钱，实际漏节点。W1-d 必须同时带"缺边即 `INCOMPLETE`"的最小版本。

---

## 10. 契约冲突与待裁决（W0 必修）

按 AGENTS.md「契约与文档冲突 → 停下、报告、不自行选边」，以下各点在本方案落地前必须统一。本文写法是**建议值**。

| # | 冲突 | 本文采用 | 需要谁改 |
|---|---|---|---|
| C09-1 | **`contracts/openapi.v2.yaml` 完全没有 S09 端点**。`services/lineage-impact/service.yaml` 声明 `POST /lineage-edges`、`POST /impact-queries`，contracts 里 `grep 'lineage\|impact-queries\|lineage-edges'` 无结果 | 按 catalog 的两条命令 + §5.3 的查询补全 | `contracts/openapi.v2.yaml` |
| C09-2 | `service.yaml` 的 `depends_on` 只有 `S08`，但 E08 链路必须消费 S16 `memory.canon.committed` 与 S18 `context.pack.created`；实际还消费 S06/S04/S10/S28/S37 | 补 `S16`、`S18`、`S06`、`S10`、`S28`、`S37` | `contracts/service-catalog.v2.json` → 重新生成 |
| C09-3 | `lineage.impact.computed` 在 `event-topics.v2.json` 注册，但**无 payload schema**；§5.4 的字段只能停留在方案里 | 新增 `contracts/events/lineage-impact-computed.v2.schema.json` | `contracts/` |
| C09-4 | `execution-context.v2.schema.json` 是**单 `node_id` + 单 `node_epoch`** 结构，与"闭包是多节点"直接冲突。若把 `POST /impact-queries` 的 body 做成 `execution-context`，要么塞一个假几点的 node，要么把 node 数组塞进一个 node 字段 | 不复用 execution-context；用 §5.2 的专用 body，只借其中的 `manifest_ref`/`branch_id`/`execution_fence` | `contracts/openapi.v2.yaml`（S09 需要独立的 request schema） |
| C09-5 | `enterprise-change-plan.v2` 的 `impact` 没有 `disposition` 三档，只有 `regenerate_node_ids`/`recheck_node_ids`/`reuse_artifact_refs` 三个数组。S09 的 `RECHECK` 与 `REUSE` 语义能映射，但**节点级 reason 无处安放** | 映射为三数组（可表达）；reason 留在 `impact_analysis_node`，S47 按需查 S09 | 暂不改；如要可解释性进计划，需扩 schema |
| C09-6 | docs/04 §2 要求 ChangePlan 含 `impact_watermark`，`enterprise-change-plan.v2` 里只有 `impact.lineage_watermark`（值来源相同但位置不同，且是 required 的嵌套字段） | 用 `impact.lineage_watermark`，值取 S09 的 `lineage_watermark` | S07 方案同步此决定 |
| C09-7 | `service.yaml` 的 `api.query` 是 `POST /impact-queries`（POST 但归类为 query），而 openapi 的 `Operation`/`Error` 语义假定写命令才有幂等头 | `POST /impact-queries` **要求** `Idempotency-Key`（因为有计算成本与 `analysis_id` 复用） | 契约注释说明"有成本的查询也要幂等" |
| C09-8 | 没有任何契约定义 `shot_lineage_id`（[S39 C4](S39-repair-planning.md) 已登记）。S09 用 `service_id/id/revision` 定位，不依赖它；但 S39 的跨 Run 累计需要它与 S09 的节点 id 对齐 | S09 不引入该字段；缺口留给 S39/S06 裁决 | 见 S39 C4 |

---

## 11. 未决问题

1. **边的粒度由谁定？** 本文把 `SHOT_FIELD_DEPENDS` 的生成权给了 S06（自报）。若 S06 的 DSL 派生规则变化，边会跟着变，而 S09 无法验证自报的边对不对。需要一个"边的一致性抽检"——目前没有设计。
2. **`CONSERVATIVE` 的放大单元是 episode**。对短剧（一集 1–3 分钟）合适；对长剧（一集 45 分钟、数百镜头）可能过于昂贵。是否要按 `production_stage` 放大而非整集，需要成本数据支撑后才能定。
3. **跨集闭包的规模**。一次改前集可能触达 20+ 集。当前 `CLOSURE_TOO_LARGE` 的上限（2 万节点）是否够，只有真实数据能回答。docs/03 的容量是规划值，`slo.verified=false`。
4. **图引擎替换的时机**。docs/01 §5 说"达到量级后内部替换图引擎，不改变外部契约"。本文的边表与查询接口是按此约束设计的，但"量级"没有数值定义。
5. **`analysis_hash` 的稳定性**。JS 对象键序会影响 JCS 的哈希，Python dict 保序。若 S07 在不同语言实现里重算 hash，结果会不同。建议统一由 S09 计算、其他服务只校验，不重算——但这条要写进 SDK。
6. **已交付集的 `RECHECK` 会不会永远不执行？** 当前设计里 S09 只标 `RECHECK`，执行与否由 S07/S21 决定。若没人处理，`memory_stale=true` 的集会一直挂着。需要一条"已交付集的 stale 清理"策略，S09 不做这件事。
