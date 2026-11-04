# S14 · 道具资产 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码；SLO 与容量为 docs/03 的规划值，**未实测**。

上游依据：[docs/10](../docs/10-assets.md)（道具行 + 最后一段关于"S16 正史、S14 只持有可视化变体"）、[docs/01 §3](../docs/01-enterprise-architecture.md)、[docs/04](../docs/04-data-consistency.md)、[docs/06 §2–§4](../docs/06-automation-governance.md)、[docs/14 §2](../docs/14-enterprise-operations.md)、[S14 规格](../docs/design-package/services/S14-prop-assets.md)。

配套：[S10](S10-asset-catalog.md)（只读引用）、[S11](S11-character-assets.md) / [S12](S12-costume-assets.md) / [S13](S13-scene-assets.md) / [S15](S15-voice-assets.md)（**同级，不互相 import、不互相写库**）、[S28](S28-artifact-ingest.md)、[S02](S02-governance-policy.md)、[S16](S16-story-memory.md)、[S17](S17-continuity.md)。

---

## 0. 一句话边界

**S14 是"这个东西长什么样、上面写了什么字、开着关着破着分别是什么样"的唯一权威。**

它是最容易越界的一类资产，因为道具天然带着"归属"和"剧情意义"的诱惑。

docs/10 对 S14 的边界写得最明确：

> 道具当前持有者、角色伤势/关系/知情范围在S16正史，资产服务只持有可视化变体；S17检验画面与故事状态匹配，避免两个服务各记一个"钥匙归谁"。

所以：

| 问题 | 归谁 |
|---|---|
| 这把钥匙长什么样、刻着什么字 | **S14** |
| 钥匙开/合的视觉状态、破损的视觉状态 | **S14**（变体） |
| **钥匙现在归谁** | **S16**（claim） |
| 钥匙现在**是不是**被打开了 | **S16**（剧情状态） |
| 画面里钥匙在谁手上、是不是开着 | **S17**（检查，不记录） |
| 钥匙的参考图字节 | S28 |
| 这一镜绑了哪一版钥匙 | S10 |

### 0.1 写什么 / 不写什么

| 事实 | 唯一写者 |
|---|---|
| `PropRevision`（不可变） | **S14** |
| 尺寸/外观（物理规格） | **S14** |
| 文字细节（信上的字、标签、铭文） | **S14** |
| 开/合/破损**视觉变体** | **S14** |
| canonical 指针 | **S14** |
| 道具级权利引用 | **S14 存指针**，判定归 S02 |
| 道具当前持有者 / 位置 | **S16** |
| 道具有没有被角色使用过 | **S16** 的事件/claim |
| 道具的剧情意义（"这把钥匙能开保险箱"） | **S16** / S04 |
| 某场戏用了哪个道具 | S10 binding |

### 0.2 四个明确不做

1. **绝不记录持有者。** 不建 `prop_holder`、不建 `current_location`。有这两个字段，S14 就是第二个正史服务。
2. **绝不记录"道具现在是什么状态"。** 只有 `variant_selector` 的可选值，没有 `current_state`。谁决定现在用哪个变体？S16 的 claim + S17 的检查。
3. **不做"角色持用可行性"判定。** docs/10 道具质量门有"角色持用可行性"—— 这个检查的输入是 S14 的尺寸 + S11 的角色体型，由 **S33/S34 的 QC** 执行，S14 只读结论。S14 自己去查 S11 就把两类资产耦合了。
4. **不持有角色内容。** 即使道具是"某某的信物"，也不引用角色描述，只存可选的 `character_ref` 软引用。

---

## 1. 关键区分

### 1.1 视觉变体 vs 剧情状态（本方案的核心）

这是 S14 最容易写错的地方，比 S12 更危险，因为"钥匙归谁"是所有短剧里最常见的道具线索。

```
S14 拥有：                                  S16 拥有：
┌─────────────────────────────┐            ┌─────────────────────────────────┐
│ prop_key@3                  │            │ claim(prop_key, holder, T0..T1) │
│  variants:                  │            │ claim(prop_key, location, T1..) │
│    condition: intact|worn|  │            │ claim(prop_key, opened, T2..T3) │
│               broken        │            │ claim(prop_key, in_hand_of, T2) │
│    open_state: closed|open  │            └─────────────────────────────────┘
│ 文字: "保险箱 07"            │                          │
└─────────────────────────────┘                          │
             │                                             │
             └──────────────► S17 比对 ◄──────────────────┘
                    期望状态（S16）+ 可用变体（S14）→ 画面检查
```

关键规则：

- S14 的变体是**能力**（"我能画出破的样子"），不是**事实**（"它现在是破的"）。
- S16 的 claim 是**事实**（"第 6 场它被摔碎了，之后一直碎着"）。
- S17 负责把两者对上（"第 7 场的画面里它还完好 → `PROP_VISUAL_MISMATCH`"）。

如果 S14 也存"当前 broken"，那么在第 6 场和第 7 场之间，这个字段由谁改？S14 改 → 它就必须消费 S16 的剧情事件并有自己的状态机 → 第二正史。不改 → 它就永远是错的。**没有第三种选择。** 所以只能不存。

### 1.2 道具 vs 服装配饰 vs 场景固定物

三类边界容易混。判据按顺序问：

1. **能被角色拿走/移动/藏起来吗？**
   - 能 → 道具（S14）
   - 不能 → 继续
2. **它的状态会因剧情改变吗（开/合/破损/被点燃）？**
   - 会 → 道具（S14）
   - 不会 → 继续
3. **它需要特写来传达信息吗（信上的字、戒指内侧刻字）？**
   - 需要 → 道具（S14）
   - 不需要 → 场景固定物（S13）

服装配饰（S12 的 `accessories`）额外一条：**它是否附着于某套服装、且不单独出现？** 是 → 归 S12 的配饰清单（视觉上随服装走）；但**如果它同时是关键道具**（那枚戒指），则两处都有引用，S14 拥有可特写/被持物的定义，S12 拥有"配这套衣服"的搭配关系。这是 §10 的 C14-3 冲突，需要设计方裁决。

### 1.3 文字细节的两种形态

道具上的文字（信、标签、铭文、屏幕）是 S14 的独特资产。

```jsonc
"text_detail": {
  "surface": "front",                      // 哪个面
  "content_kind": "handwritten|printed|engraved|screen",
  "canonical_text": "保险箱 07",
  "language": "zh-Hans",
  "layout": {"anchor":"center","orientation":"horizontal","size_band":"readable_medium"},
  "legibility": "must_be_readable|may_be_blurred|background_only",
  "sensitive": false
}
```

三条硬约束：

1. **`canonical_text` 是唯一权威。** 生成/修复时，文字必须来自这个字段，不允许模型自由发挥。S33/S34 的 OCR 检查以此为准（S14 不做 OCR）。
2. **`legibility` 决定检查强度。** `must_be_readable` 的道具如果 OCR 不通过 → 候选拒绝；`may_be_blurred` → 只检查"像文字"；`background_only` → 不检查。
3. **`sensitive` 的道具不进入公共 Prompt 日志。** docs/10 的"禁止把供应商可访问URL长期写入公共Prompt日志"同理适用于敏感文字（合同条款、私人信件）。`sensitive=true` 时，`canonical_text` 在返回给非特权调用方时被替换为 `"[redacted]"` + hash。

### 1.4 尺寸与"角色持用可行性"

`service.yaml` 的质量门包含"角色持用可行性"。这条检查需要：

- S14 的物理尺寸（真实厘米）。
- S11 的角色体型/身高带。
- 姿势/镜头景别（S09）。

三者都由别的服务提供。S14 只存**尺寸**，不判断可行性。

尺寸必须用**真实单位**（cm/g），不能用"小/中/大"：

```jsonc
"physical": {
  "length_cm": 8.5, "width_cm": 5.4, "height_cm": 1.2,
  "weight_g": 120,
  "handheld": true,                         // 设计上是否预期手持
  "rigid": true
}
```

为什么必须真实单位：不同场景的"大小感"由镜头和参照物决定。如果只存"小"，模型在特写里可能画成一只手能握住、在中景里画成一块砖头。真实尺寸让 S17 能做"画面里这把钥匙与手的比例是否合理"的检查。

### 1.5 canonical ≠ 最新

同 S11–S13。道具特殊之处：**文字一旦有误，整条线索就废了**。所以 canonical 变更的门更严：`canonical_text` 改变必须 `human_approved`，不允许 `policy_auto`。

---

## 2. 数据模型

Postgres，schema `prop_assets`。所有表含 `tenant_id`，主键/唯一约束含 `tenant_id`（E28）。RLS 作为第二道防线（docs/14 §2）。

### 2.1 道具定义

```sql
CREATE TABLE prop_assets.prop (
  tenant_id     text NOT NULL,
  prop_id       text NOT NULL,               -- 'prp_01J...'
  project_id    text NOT NULL,
  series_scope  text NOT NULL,
  display_name  text NOT NULL,
  prop_kind     text NOT NULL CHECK (prop_kind IN (
                  'hero_prop','story_prop','set_dressing','background_prop','wearable_prop')),
  story_critical boolean NOT NULL DEFAULT false,  -- 是否为剧情线索道具（影响门强度）
  status        text NOT NULL CHECK (status IN ('active','deprecated','tombstoned')),
  canonical_revision integer,
  current_revision   integer NOT NULL DEFAULT 0,
  text_fingerprint   char(64),               -- canonical 的 canonical_text 指纹（便于比对）
  created_by    jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, prop_id),
  UNIQUE (tenant_id, series_scope, display_name),
  CONSTRAINT prop_canonical_within_head CHECK (canonical_revision IS NULL OR canonical_revision <= current_revision)
);
CREATE INDEX prop_by_series ON prop_assets.prop (tenant_id, series_scope, status, prop_kind);
CREATE INDEX prop_story_critical ON prop_assets.prop (tenant_id, series_scope) WHERE story_critical;
```

`story_critical=true` 的道具（钥匙、证据、信物）走更严的门：

- 文字变更必须人审。
- 必须有 `must_be_readable` 或明确说明为何不需要。
- 变体必须覆盖 S16 可能要求的全部状态（见 §2.2 与 §10 C14-2）。

`text_fingerprint = sha256(canonical_text)`，用于跨 revision 快速比对"文字变了没有"，不暴露原文（对 `sensitive` 道具尤其重要）。

### 2.2 不可变 revision

```sql
CREATE TABLE prop_assets.prop_revision (
  tenant_id        text NOT NULL,
  prop_id          text NOT NULL,
  revision         integer NOT NULL CHECK (revision >= 1),
  content_hash     char(64) NOT NULL,
  text_fingerprint char(64),                 -- NULL = 无文字
  payload          jsonb NOT NULL,
  physical         jsonb NOT NULL,           -- 见 §1.4
  appearance       jsonb NOT NULL,           -- 材质、颜色、形状描述（结构化）
  text_details     jsonb NOT NULL,           -- [{surface,content_kind,canonical_text,...}]
  variant_manifest jsonb NOT NULL,           -- {condition:{intact:ref,broken:ref}, open_state:{closed:ref,open:ref}}
  variant_axes     jsonb NOT NULL,           -- 本 revision 支持的轴与取值
  rights_ref       jsonb NOT NULL,
  source_kind      text NOT NULL CHECK (source_kind IN ('manual_upload','generated','imported','derived')),
  parent_revision  integer,
  quality_gate_ref jsonb NOT NULL,
  accepted_at      timestamptz NOT NULL,
  accepted_by      jsonb NOT NULL,
  PRIMARY KEY (tenant_id, prop_id, revision),
  CONSTRAINT pr_hash_len CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT pr_parent_lt CHECK (parent_revision IS NULL OR parent_revision < revision)
);
CREATE UNIQUE INDEX pr_hash_uniq ON prop_assets.prop_revision (tenant_id, content_hash);
CREATE INDEX pr_text_fp ON prop_assets.prop_revision (tenant_id, prop_id, text_fingerprint);
```

**没有 `condition` / `open_state` / `current_holder` 列。** 这是刻意的结构防御（见 §1.1）。

`variant_axes` 存进 revision，保证历史可解析：如果将来新增轴（`burnt`），旧 revision 的轴集合仍是旧的。

**变体必须全部有 artifact。** 声明了 `broken` 却没有 `broken` 的图，等于 S16 说"碎了"、画面却画不出来。封存时强制。

### 2.3 变体覆盖与"必需变体"

`story_critical` 道具的变体覆盖有额外要求：

```sql
CREATE TABLE prop_assets.required_variant (
  tenant_id     text NOT NULL,
  prop_id       text NOT NULL,
  axis          text NOT NULL,               -- condition|open_state|...
  required_value text NOT NULL,
  required_by   text NOT NULL,               -- 'policy:story_critical'|'shot:sh_12'
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, prop_id, axis, required_value, required_by)
);
```

这张表由**外部需求**驱动：S09 的分镜说"第 12 镜要特写钥匙的破口"，就会写一条 `condition:broken required_by=shot:sh_12`。S14 接受新 revision 时必须覆盖这些 required 变体，否则：

- 拒绝 → 422 `REQUIRED_VARIANT_MISSING {axis, value, required_by}`

这解决了一个真实问题：S16 说道具碎了，S14 却没有"碎的样子"。有了这张表，缺口在**资产侧**被发现，而不是等到 S17 报画面不符。

`required_by` 用字符串而不是外键，因为需求方可能是 S09（镜头）、S02（政策）、人工。S14 不解析它，只记录。

### 2.4 文字细节表（检索用，payload 为权威）

```sql
CREATE TABLE prop_assets.prop_text_detail (
  tenant_id    text NOT NULL,
  prop_id      text NOT NULL,
  revision     integer NOT NULL,
  surface      text NOT NULL,
  content_kind text NOT NULL,
  language     text NOT NULL,
  legibility   text NOT NULL CHECK (legibility IN ('must_be_readable','may_be_blurred','background_only')),
  sensitive    boolean NOT NULL DEFAULT false,
  text_hash    char(64) NOT NULL,
  text_plain   text,                         -- sensitive=true 时为 NULL
  PRIMARY KEY (tenant_id, prop_id, revision, surface)
);
CREATE INDEX prop_text_hash ON prop_assets.prop_text_detail (tenant_id, text_hash);
```

`sensitive=true` 时 `text_plain` 为 NULL，原文只存在 `payload` 里（`payload` 本身加密存储或按租户 KMS 加密，见 docs/14）。这张派生表永远不含敏感明文，即使被误查询也不泄漏。

### 2.5 候选与 canonical

```sql
CREATE TABLE prop_assets.prop_candidate (
  tenant_id     text NOT NULL,
  candidate_id  text NOT NULL,
  prop_id       text NOT NULL,
  content_hash  char(64) NOT NULL,
  payload       jsonb NOT NULL,
  prop_check    jsonb NOT NULL,              -- {text_ocr_pass, size_plausible, variant_coverage, holder_feasibility}
  evaluation    jsonb NOT NULL,
  state         text NOT NULL CHECK (state IN ('PENDING','ACCEPTED','REJECTED','EXPIRED')),
  created_by    jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, candidate_id)
);

CREATE TABLE prop_assets.canonical_pointer (
  tenant_id        text NOT NULL,
  prop_id          text NOT NULL,
  revision         integer NOT NULL,
  text_fingerprint char(64),
  decided_at       timestamptz NOT NULL DEFAULT now(),
  decided_by       jsonb NOT NULL,
  decision_kind    text NOT NULL CHECK (decision_kind IN ('human_approved','policy_auto','initial_import')),
  reason_text      text,
  prior_canonical  integer,
  superseded_at    timestamptz,
  PRIMARY KEY (tenant_id, prop_id, revision)
);
CREATE UNIQUE INDEX prop_canonical_one
  ON prop_assets.canonical_pointer (tenant_id, prop_id)
  WHERE superseded_at IS NULL;
```

### 2.6 基础设施

与 S11–S13 同构（`idempotency_record` / `outbox` / `inbox`），在 S14 自己的库里。照抄如下：

```sql
CREATE TABLE prop_assets.idempotency_record (
  tenant_id text NOT NULL, command_id text NOT NULL, request_hash char(64) NOT NULL,
  command_type text NOT NULL, state text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code integer, response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE prop_assets.outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id text NOT NULL, aggregate_id text NOT NULL  /* prop_id */, aggregate_sequence bigint NOT NULL,
  event_type text NOT NULL, partition_key text NOT NULL, payload jsonb NOT NULL,
  headers jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON prop_assets.outbox (id) WHERE published_at IS NULL;

CREATE TABLE prop_assets.inbox (
  consumer text NOT NULL, event_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

`partition_key = tenant_id || ':' || aggregate_id`，中继按它单线程发布（[S08 §6.4](S08-revision-snapshot.md)）。

### 2.7 不建的表（本方案最重要的一节）

| 不建的表 | 为什么 |
|---|---|
| `prop_holder` / `prop_current_holder` | **第二正史权威**。持有者在 S16。 |
| `prop_location` | 同上。位置是剧情事实。 |
| `prop_state` / `prop_condition_current` | "现在是不是破的"归 S16。 |
| `prop_usage_history` | "在哪几场出现过"归 S16/S09。 |
| `prop_relationship` | "这把钥匙开那个箱子"是剧情关系，归 S16。 |
| `prop_character_owner` | "这是谁的信物"是剧情，归 S16。 |
| `prop_rights_status` | 只存指针，判定归 S02。 |
| `prop_binding_usage` | 绑定与使用归 S10。 |

这八条不是建议，是 AGENTS.md "让某个服务变成第二个权威"的直接禁止项。任何人想加这些表，都应该先读 docs/10 最后那段。

---

## 3. 状态机

### 3.1 候选

```
PENDING ──accept──► ACCEPTED
   ├──reject──► REJECTED
   └──ttl────► EXPIRED
```

### 3.2 道具与 canonical

```
active ──deprecate──► deprecated ──tombstone──► tombstoned
```

### 3.3 文字变更的特殊门

```
revision N (text_fingerprint = F1)
   │
   ├── 换图/补变体，文字不变 ──► N+1 (fingerprint F1)，可 policy_auto
   │
   └── 改 canonical_text ──► N+1 (fingerprint F2)
                                │
                                └── 必须 human_approved；story_critical 时必须带
                                    reason_text 与（若已用于已交付集）impact_plan_ref
```

**文字变更是最贵的变更。** 文字错了，所有依赖它的镜头（特写、读信、仲裁证据）全废。所以：

- `story_critical=true` 且 `canonical_text` 变化 → 强制人审 + 影响计划引用。
- 非 `story_critical` → 仍需人审，但不强制影响计划。

### 3.4 非法转移

| 从 | 到 | 原因 |
|---|---|---|
| `tombstoned` | `active` | 复活走新 ID |
| revision 覆写 | — | 不可变 |
| `story_critical` 改文字 + `policy_auto` | 接受 | 文字必须人审 |
| 缺 `required_variant` 覆盖 | 接受 canonical | 已声明的需求必须满足 |
| `tombstoned` 道具 | 新 canonical | 已下线 |
| 写"当前持有者" | — | 表都不建 |

---

## 4. 关键流程

### 4.1 起草候选

```
POST /props/{id}/candidates
{
  command_id, expected_current_revision, source_kind,
  physical, appearance, text_details,
  variant_manifest, variant_axes,
  rights_ref, quality_gate_ref
}
```

事务外校验：

1. **artifact 校验**（S28 `HEAD`）：变体里每个 artifact 存在 + hash 一致。
2. **变体覆盖完整性**：`variant_axes` 每个取值有 artifact → 否则 422 `VARIANT_COVERAGE_INCOMPLETE {axis, missing_values}`。
3. **`required_variant` 覆盖**：本道具已登记的 required 变体全部被覆盖 → 否则 422 `REQUIRED_VARIANT_MISSING {axis, value, required_by}`。
4. **物理尺寸合法**：`length_cm/width_cm/height_cm` 在 `(0, 500]`；`weight_g` 在 `(0, 50000]` → 否则 422 `PHYSICAL_DIMENSION_INVALID`。
5. **文字细节**：
   - `legibility='must_be_readable'` 时，`canonical_text` 非空。
   - `sensitive=true` 时，必须有 `text_hash`。
   - 同一 `surface` 不能有两个 `text_detail` → 422 `DUPLICATE_TEXT_SURFACE`。
6. **文字 OCR 结论**：从 `quality_gate_ref` 读取 S33/S34 的 OCR 结果；`must_be_readable` 且 OCR 不通过 → 拒绝 422 `TEXT_OCR_MISMATCH {surface, expected_hash, ocr_text_hash}`。

第 6 条**特别说明**：S14 不做 OCR。它读 QC 结论。S14 自己跑 OCR 会把图像处理依赖拉进资产服务，且与 S33/S34 出现两套判定。

事务内：锁 `prop` → CAS → 写候选。

### 4.2 接受候选

```
POST /props/{id}/candidates/{cid}:accept
{command_id, expected_current_revision, set_canonical, decision_kind, reason_text, impact_plan_ref}
```

事务内：

```
BEGIN;
-- 1) 锁 prop，CAS expected_current_revision
-- 2) 锁 candidate，state='PENDING'
-- 3) 文字变更判定：
--      new_fp = fingerprint(payload.text_details)
--      if new_fp != prop.text_fingerprint:
--         decision_kind != 'human_approved' → 422 TEXT_CHANGE_REQUIRES_HUMAN
--         story_critical AND impact_plan_ref IS NULL → 422 IMPACT_PLAN_REQUIRED
-- 4) set_canonical 且 prop.story_critical：
--      若 candidate.prop_check.text_ocr_pass != true → 422 TEXT_OCR_INCOMPLETE
-- 5) INSERT prop_revision (revision=current+1)
--    hash 冲突 → 409 DUPLICATE_PROP_CONTENT
-- 6) 批量 INSERT prop_text_detail（sensitive 时 text_plain=NULL）
-- 7) set_canonical → supersede 旧 pointer，插新，更新 prop（含 text_fingerprint）
-- 8) UPDATE candidate.state='ACCEPTED'
-- 9) INSERT outbox (asset.prop.revision.accepted, seq=rev)
COMMIT;
```

### 4.3 读路径

```
GET /props/{id}
GET /props/{id}/revisions/{revision}
GET /props/{id}/revisions/{revision}?variant=condition:broken,open_state:open
GET /props/{id}/revisions/{revision}/text?surface=front&redact=false
GET /props/{id}/canonical
GET /props:lookup?series_scope=&alias=
```

变体查询硬约束（与 S12/S13 同）：

- 请求不存在的变体 → **422 `VARIANT_NOT_AVAILABLE {axis, value, available}`**，不静默退回默认。
- 理由尤其重要：S16 说"钥匙现在是开的"，S14 退回 `closed`，S17 检查画面时看到"关着"但拿的是默认图，可能判"通过"—— 剧情线索就断了。

文字读取：

- `sensitive=true` 且调用方无特权 → `{"redacted": true, "text_fingerprint": "..."}`，不含原文。
- `sensitive=false` → 返回 `canonical_text`。
- 特权判定走平台签名的作用域（docs/11），不信任请求体里的任何 `is_admin`。

响应：

```jsonc
{
  "prop_ref":{"service_id":"S14","id":"prop_key","revision":3,"content_hash":"…"},
  "variant":{"condition":"broken","open_state":"open"},
  "artifacts":{"front":{...},"detail_break":{...}},
  "physical":{"length_cm":8.5,"handheld":true},
  "text":[{"surface":"front","legibility":"must_be_readable","content_kind":"engraved",
           "text":"保险箱 07","text_fingerprint":"…"}],
  "is_canonical": true,
  "canonical_revision": 3,
  "rights_recheck_required": true
}
```

### 4.4 失败矩阵

| # | 场景 | 行为 | 错误码 |
|---|---|---|---|
| F1 | artifact 缺失/hash 不符 | 拒 | 422 `ARTIFACT_NOT_FOUND` / `ARTIFACT_HASH_MISMATCH` |
| F2 | 变体轴缺取值 | 拒 | 422 `VARIANT_COVERAGE_INCOMPLETE` |
| F3 | 外部登记的 required 变体缺 | 拒 | 422 `REQUIRED_VARIANT_MISSING` |
| F4 | 物理尺寸越界 | 拒 | 422 `PHYSICAL_DIMENSION_INVALID` |
| F5 | 同 surface 重复文字 | 拒 | 422 `DUPLICATE_TEXT_SURFACE` |
| F6 | `must_be_readable` 但 OCR 不符 | 拒 | 422 `TEXT_OCR_MISMATCH` |
| F7 | 文字变更 + 非人审 | 拒 | 422 `TEXT_CHANGE_REQUIRES_HUMAN` |
| F8 | `story_critical` 文字变更缺影响计划 | 拒 | 422 `IMPACT_PLAN_REQUIRED` |
| F9 | 读时请求不存在变体 | 拒 | 422 `VARIANT_NOT_AVAILABLE` |
| F10 | 敏感文字被非特权读取 | 脱敏 | 200 + `redacted:true` |
| F11 | 同内容重复接受 | 拒 | 409 `DUPLICATE_PROP_CONTENT` |
| F12 | `expected_current_revision` 落后 | 拒 | 409 `VERSION_CONFLICT` |
| F13 | S28 超时 | 不落候选 | 503 `DEPENDENCY_UNAVAILABLE` |
| F14 | 跨租户 | 404 | `NOT_FOUND` |
| F15 | 两个并发接受 | 一成功一 409 | 行锁 |
| F16 | 请求"谁持有这个道具" | **没有这个接口** | 404 `NOT_FOUND`（路由不存在） |

F16 是一个刻意的设计：如果有人试图 `GET /props/{id}/holder`，S14 应该**没有这个路由**，而不是返回 501 或空。没有路由是最强的边界声明 —— 它让错误实现在编译/联调期就被发现。

---

## 5. 对外契约

### 5.1 命令

| 方法 | 路径 |
|---|---|
| POST | `/props` |
| POST | `/props/{id}/candidates` |
| POST | `/props/{id}/candidates/{cid}:accept` |
| POST | `/props/{id}/candidates/{cid}:reject` |
| POST | `/props/{id}:set-canonical` |
| POST | `/props/{id}:deprecate` / `:tombstone` / `:merge` |
| POST | `/props/{id}/required-variants` | 外部登记必需变体 |

### 5.2 查询

见 §4.3。

### 5.3 事件

发布（`drama.prop-assets.v2`，partition key `tenant_id:prop_id`，`aggregate_sequence = revision`）：

```jsonc
{
  "specversion":"1.0","schema_version":"2.0","id":"evt_...","source":"S14",
  "type":"asset.prop.revision.accepted",
  "subject":"prop/prop_key","time":"2026-10-31T09:00:00Z",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"prop_key","aggregate_sequence":3,
  "correlation_id":"cmd_...","causation_id":"cmd_...","traceparent":"00-...",
  "data":{
    "prop_ref":{"service_id":"S14","id":"prop_key","revision":3,"content_hash":"…"},
    "prop_kind":"story_prop",
    "story_critical": true,
    "became_canonical": true,
    "canonical_revision": 3,
    "text_changed": false,
    "text_fingerprint":"…",
    "variant_axes":{"condition":["intact","worn","broken"],"open_state":["closed","open"]},
    "required_variants_covered": true,
    "impact_plan_ref": null,
    "rights_ref":{...}
  },
  "data_ref":null
}
```

`variant_axes` 与 `text_fingerprint` 必须在事件里：S17 需要知道"这一版支持哪些状态"，S19/S33 需要知道"文字有没有变"。

消费：

| 事件 | 来源 | 处理 |
|---|---|---|
| `artifact.archived` | S28 | 审计 |
| `continuity.required_variant.registered` | S09 | 写入 `required_variant`（若契约存在；见 C14-4） |

**不消费** S16 的任何事件。S16 说"钥匙被摔碎了"，S14 不因此生成 `broken` 变体 —— 那是 S07/S39 的修复流程或人工补图。S14 订阅 S16 会让资产服务跟着剧情自动变，那是隐式状态同步，无法审计。

### 5.4 错误码汇总

| HTTP | code | retryable |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | false |
| 403 | `TENANT_SCOPE_DENIED` | false |
| 404 | `NOT_FOUND` / `CANONICAL_NOT_SET` | false |
| 409 | `VERSION_CONFLICT`(true) / `CANDIDATE_NOT_PENDING` / `DUPLICATE_PROP_CONTENT` / `IDEMPOTENCY_KEY_REUSED` | 见括号 |
| 422 | `ARTIFACT_NOT_FOUND` / `ARTIFACT_HASH_MISMATCH` / `VARIANT_COVERAGE_INCOMPLETE` / `VARIANT_NOT_AVAILABLE` / `REQUIRED_VARIANT_MISSING` / `PHYSICAL_DIMENSION_INVALID` / `DUPLICATE_TEXT_SURFACE` / `TEXT_OCR_MISMATCH` / `TEXT_CHANGE_REQUIRES_HUMAN` / `IMPACT_PLAN_REQUIRED` / `PROP_TOMBSTONED` | false |
| 429 | `WRITE_BACKPRESSURE` | true |
| 503 | `DEPENDENCY_UNAVAILABLE` | true |

---

## 6. 幂等与并发

### 6.1 幂等

- 键 `(tenant_id, command_id)` + JCS hash。
- `POST /props` 自然幂等：`(tenant_id, series_scope, display_name)` 唯一。
- `POST /props/{id}/required-variants` 幂等：主键含 `(axis, required_value, required_by)`，重复登记无副作用。

### 6.2 并发

| 竞争 | 机制 |
|---|---|
| 两次接受 | `prop` 行 `FOR UPDATE` |
| 两次设 canonical | `prop_canonical_one` |
| 同内容重复 | `pr_hash_uniq` |
| required_variant 与接受并发 | required 写入先提交；接受时读已提交集合（READ COMMITTED 下可见）。若 req 在 accept 事务中途写入，下一次接受才生效 —— 可接受，因为 required 是需求而非事务约束 |

### 6.3 锁顺序

```
1. prop (FOR UPDATE)
2. prop_candidate (FOR UPDATE)
3. canonical_pointer
4. prop_revision (INSERT)
5. prop_text_detail (INSERT)
6. outbox
```

---

## 7. 可观测

指标：

- `s14_candidate_total{result}`、`s14_accept_total{set_canonical,story_critical}`
- `s14_text_change_total{story_critical}`、`s14_text_ocr_mismatch_total{surface}`
- `s14_required_variant_missing_total{axis,value}`
- `s14_variant_not_available_total{axis,value}`
- `s14_sensitive_text_redacted_total`
- `s14_canonical_change_total{decision_kind}`

告警：

- `s14_text_change_total{story_critical="true"}` > 0 且无 `impact_plan_ref` → 门失效。
- `s14_required_variant_missing_total` 上升 → S16/S09 在要求 S14 没有的状态。这是"剧情说碎了但画不出碎样"的早期信号，必须人工补图。
- `s14_variant_not_available_total` 上升 → 生产在要求不存在的变体，画面会不符。
- 敏感文字被多次读取 → 审计（不是错误，但要可追溯）。

Runbook：

- `required-variant-audit`：列出所有 `required_variant` 与最近 revision 的覆盖情况。
- `text-fingerprint-audit`：跨 revision 比对 `text_fingerprint`，找出未声明的人审变更。
- `rehash-revision`：`content_hash` 与 `prop_text_detail` 一致性。
- `sensitive-access-report`：谁读了哪些敏感文字（审计用）。

---

## 8. 测试清单

### E02 · 三集连续

| E02-E1 同一道具三集 canonical 不变 | 三集同一 `prop_ref`；`text_fingerprint` 相同 |
| E02-E2 文字跨集一致 | 三集读出的 `canonical_text` 相同 |
| E02-E3 第 3 集需要 broken 变体 | S14 提供；S16 决定何时用（测试只验 S14 有该变体） |
| E02-E4 尺寸一致 | 三集 `physical` 相同 |

### E08 · 改前集 / E09 · 影响

| E08-E1 改文字 → 新 revision，人审 | 旧集不变；`text_changed=true` |
| E08-E2 `story_critical` 改文字缺影响计划 | 422 |
| E08-E3 补一张破损变体图（文字不变） | `policy_auto` 允许；`text_fingerprint` 不变 |

### E12 · 幂等

重复接受返回首次结果；同键不同 body 409；重复登记道具返回既有 ID。

### E28 · 跨租户

| E28-E1 跨租户读 revision | 404 |
| E28-E2 跨租户读文字 | 404（不是脱敏 200 —— 跨租户根本不该知道存在） |
| E28-E3 同名道具两租户 | 独立 |

### E30 · 权利

| E30-E1 授权撤销后仍返回 revision | `rights_recheck_required=true` |

### E24/E25 · QC

| E24-E1 文字/形态不符 | 候选拒绝，`TEXT_OCR_MISMATCH` |
| E24-E2 角色持用可行性失败（钥匙比手大） | QC 报 `HOLDER_FEASIBILITY_FAIL`；S14 只读结论，不自己判 |
| E24-E3 变体图之间形态不一致（broken 图和 intact 图不是同一把钥匙） | QC 报 `VARIANT_INCONSISTENT`；拒 |

### E29 · 恶意媒体

| E29-E1 道具图来自 S28 隔离导入器 | S14 只读 ArtifactManifest，不解码字节 |
| E29-E2 伪造 artifact_id | 422 |

### E37 · 缺素材

| E37-E1 `required_variant` 未覆盖时 `resolve` | 422，不退回默认 |
| E37-E2 S02 gate 遇缺变体 | 判 `require_human`/`deny` |

### 通用

| 道具无 holder 字段 | schema 断言：`prop_revision` 无 `holder`/`current_state` 列 |
| 变体矩阵全 | 每个轴值有 artifact |
| 读不存在变体 | 422 |
| 敏感文字非特权读 | 脱敏 |
| 并发接受 | 1 成功 |
| 崩溃恢复 | 派生表与 payload 一致 |
| 独立升级 | 旧 payload 可读 |

---

## 9. 反模式

1. ❌ **记录道具持有者**（第二正史权威，这是本服务最严重的错误）。
2. ❌ 记录"道具现在是什么状态"（归 S16）。
3. ❌ 记录"道具在哪几场出现"（归 S16/S09）。
4. ❌ 记录"道具与角色的关系/道具与道具的关系"（剧情，归 S16）。
5. ❌ 读变体缺失时静默退回默认。
6. ❌ S14 自己做 OCR 或视觉比对（归 S33/S34）。
7. ❌ S14 自己判"角色持用可行性"（需 S11 输入，归 QC）。
8. ❌ 尺寸用"小/中/大"代替真实厘米。
9. ❌ 敏感文字进派生表明文。
10. ❌ 文字变更允许 `policy_auto`。
11. ❌ 订阅 S16 事件并自动生成变体。
12. ❌ 原地改 revision。
13. ❌ 在 S14 判权利。

---

## 10. 冲突（报告，不选边）

| # | 冲突 / 缺口 | 现状 | 本文用法 |
|---|---|---|---|
| C14-1 | `service.yaml` 只给 `POST /props/{id}/revisions` + `GET /props/{id}/revisions/{revision}`；没有候选/接受/变体/文字 | catalog | 按 §5 拆分；未改契约 |
| C14-2 | docs/10 说 S16 持有"道具当前持有者"，但**没有机制**让 S14 知道"S16 需要哪些视觉变体" | docs/10 | 本文用 `required_variant` 表 + 外部登记；这是**本文新增的概念**，需设计方确认是否进入契约 |
| C14-3 | 道具与服装配饰的双重定义（§1.2）；docs/10 未裁决 | docs/10 | 本文允许 S14 与 S12 各持一份引用，但**没有**自动同步；需裁决 |
| C14-4 | 没有 `continuity.required_variant.registered` 这样的契约事件；S09 如何向 S14 登记需求未定义 | topics | 本文假设存在或走同步 API；需契约方确认 |
| C14-5 | docs/10 质量门"角色持用可行性"需要 S11 的体型，但 S14 的 `depends_on` 只有 `[S28,S01]` | docs/10 + catalog | 本文由 S33/S34 的 QC 承担（QC 自己调 S11），S14 只读结论 |
| C14-6 | docs/10 未提"敏感文字/脱敏" | docs/10 | 本文新增 `sensitive` 与脱敏语义（源于 docs/14 的最小化原则）；需确认 |
| C14-7 | `event-topics.v2.json` 只有 `asset.prop.revision.accepted`；没有"文字变更"独立事件 | topics | 用 `text_changed`/`text_fingerprint` 字段承载 |
| C14-8 | docs/10 的"道具"表未区分 `hero_prop`/`story_prop`/`set_dressing` | docs/10 | 本文新增 `prop_kind` 与 `story_critical`；需确认是否进契约 |

---

## 11. 实施顺序与未决问题

### 11.1 顺序

1. 契约：`contracts/assets/prop-revision.v1.schema.json`（含 `text_details`、`variant_axes`、`physical`）。
2. 平台 SDK 复用（JCS/hash/`rights_ref`）。
3. 最小闭合：登记 + 起草候选（mock S28）+ 接受 + 读 revision + 变体查询 + 文字读取（含脱敏）。先验 E12/E28。
4. 接 S28；接 S33/S34 QC（OCR、持用可行性、变体一致性）。
5. `required_variant` 与 S09 对接（依赖 C14-4 的契约）。
6. 文字变更门 + 影响计划引用（最后）。

### 11.2 未决

1. **`required_variant` 的登记方到底是谁？** S09 分镜？S16 正史？S02 政策？本文假设 S09 为主，但 S16 才是"什么时候需要 broken 变体"的权威。这是本方案最大的未决项（C14-2）。
2. **敏感文字的加密与保留策略？** 本文只说"派生表不含明文"，没说 `payload` 的加密方式与保留期。归 docs/14 的保留合同。
3. **道具跨剧复用（`series_scope` 单值）？** 同其他资产。
4. **`prop_kind` 是否足够？** 可能需要 `weapon`（受分级/合规约束）独立一类。
5. **文字的语言/字体是否要约束？** 目前只存 `language`。生成模型对小语种/古文字可能不支持，需要在 QC 里体现。
6. **道具尺寸与镜头景别的匹配检查归谁？** 本文归 QC。但 QC 需要 S09 的镜头参数，链路较长。是否应该由 S17 做？
