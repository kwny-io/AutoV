# S12 · 服装资产 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码；SLO 与容量为 docs/03 的规划值，**未实测**。

上游依据：[docs/10](../docs/10-assets.md)（服装行）、[docs/01 §3](../docs/01-enterprise-architecture.md)、[docs/04](../docs/04-data-consistency.md)、[docs/06 §2–§4](../docs/06-automation-governance.md)、[docs/14 §2](../docs/14-enterprise-operations.md)、[S12 规格](../docs/design-package/services/S12-costume-assets.md)。

配套：[S10](S10-asset-catalog.md)（只读引用）、[S11](S11-character-assets.md) / [S13](S13-scene-assets.md) / [S14](S14-prop-assets.md) / [S15](S15-voice-assets.md)（**同级，不互相 import、不互相写库**）、[S28](S28-artifact-ingest.md)、[S02](S02-governance-policy.md)、[S16](S16-story-memory.md)、[S17](S17-continuity.md)。

---

## 0. 一句话边界

**S12 是"这个角色这一幕穿什么、衣服长什么样、破损到什么程度"的唯一权威。**

它拥有的是**可被画出来的衣物状态**，不是"谁穿着它"。

| 问题 | 归谁 |
|---|---|
| 这套衣服有几颗扣子、什么材质、什么配色 | **S12** |
| 这一场林岚穿红裙还是职业装 | **S12**（binding 由 S10 固定） |
| 这件衣服在第 6 场被撕破了，第 7 场还是破的 | **S16**（持续性事实） |
| 这套衣服的参考图字节 | S28 |
| 林岚是谁、脸长什么样 | S11 |
| 这一镜绑了哪一版服装 | S10 |
| 这套衣服的版权/授权是否仍有效 | S02（S12 只存指针） |

### 0.1 写什么 / 不写什么

| 事实 | 唯一写者 |
|---|---|
| `CostumeRevision`（不可变） | **S12** |
| 穿着组合（outfit 的部件构成） | **S12** |
| 材质、配色、配饰清单 | **S12** |
| 破损 / 湿度 / 妆造变体（**视觉变体**） | **S12** |
| 服装级权利引用 | **S12 存指针**，判定归 S02 |
| 服装与角色的关联 | S10 的 binding；S12 只存 `character_ref` 建议（见 §1.3） |
| 破损的**剧情原因**与**持续期** | **S16** |
| canonical 指针 | **S12** |

### 0.2 四个明确不做

1. **不改角色身份。** docs/10 服装质量门第一条："角色身份不变"。S12 的任何 revision 都不能让 S11 的 `identity_feature_hash` 失效 —— 它也不该知道那个 hash 是多少，只能"不声称"。
2. **不记录剧情事实。** "这件外套是凶手的"、"她一直戴着这条项链" 是 S16 的 claim。S12 只有 `variant_selector`。
3. **不做批量设定集生成。** 所有生成走 S26 + S23 预算。S12 只收 artifact。
4. **不持有角色内容。** 不存角色外观描述，只存 `character_ref` 指针。

---

## 1. 关键区分

### 1.1 服装资产 vs 服装绑定 vs 服装状态

三层必须分清，否则最容易出现"两个服务各记一份：她这场穿红裙"：

| 层 | 拥有者 | 形态 | 例子 |
|---|---|---|---|
| **服装定义** | S12 | `CostumeRevision`，含 outfit/材质/配饰/变体 | `cos_linlan_reddress@2` 是一套可复用的衣物定义 |
| **谁在哪一场穿它** | S10 binding（事实）+ S16（剧情叙述） | `binding_entry(role_key='costume:cos_linlan_reddress')` | 第 3 集第 2 场绑定了它 |
| **这套衣服此刻是什么状态** | S16 | claim：`costume_state(cos_x, torn)` 在 story time T 有效 | 第 6 场被撕破，第 7 场仍破 |

S12 持有的是**"破的样子长什么样"**（视觉变体），S16 持有的是**"现在是不是破的"**（剧情状态）。S17 用 S16 的期望状态去选 S12 的变体，检查画面。

这条分工是 docs/10 的原意："资产服务只持有可视化变体；S17检验画面与故事状态匹配，避免两个服务各记一个'钥匙归谁'"。服装版的等价说法：**避免两个服务各记一份"她这场穿什么"**。

### 1.2 为什么 `character_ref` 是"建议"而非"约束"

`CostumeRevision` 上有一个 `character_ref`，表示"这套衣服是为谁做的"。但：

- 它**不阻止**别的角色绑定这套服装（群演服、制服、可复用礼服）。
- 它**不参与** canonical 判定。
- 它**不用于**身份一致性检查 —— 那由 S17 通过画面 + S11 的 identity 特征判断。

原因：如果把 `character_ref` 做成强外键约束，就会出现"这套军装是给 A 做的，但 B 也需要穿"的合法场景被拒。而如果做成强校验，S12 就得在封存时查 S11 —— 那就有了一次跨服务同步依赖，且 S11 挂掉时服装也封不了。

所以：`character_ref` 是**软引用 + 审计用途**，允许为空（通用服装）。

### 1.3 变体的命名权属于 S12

`variant_selector` 的键（`damage`、`wetness`、`makeup`）在 S12 的 schema 里定义，由 S10 原样冻结进 binding hash，由 S12 在读取时解释。

```
cos_linlan_reddress@2
  variants:
    damage:   none | light | torn_shoulder | bloodied
    wetness:  dry | damp | soaked
    makeup:   natural | evening | injured_smudged
```

S12 **不**定义"什么时候该切到 `torn_shoulder`" —— 那是 S16 的正史 + S17 的一致性判断。S12 只保证每个变体都有对应 artifact。

### 1.4 canonical ≠ 最新

与 S11 同理，但服装多一层坑：**同一角色的多套服装，每套各自有 canonical**。不存在"角色的唯一服装"。

```
cos_linlan_reddress  → canonical@2
cos_linlan_office    → canonical@1
cos_linlan_pajama    → canonical@3
```

三个 canonical 同时存在，互不冲突。唯一的约束是**同一 `costume_id` 同时只有一个 canonical**。

---

## 2. 数据模型

Postgres，schema `costume_assets`。所有表含 `tenant_id`，主键/唯一约束含 `tenant_id`（E28）。RLS 作为第二道防线（docs/14 §2）。

### 2.1 服装定义

```sql
CREATE TABLE costume_assets.costume (
  tenant_id      text NOT NULL,
  costume_id     text NOT NULL,              -- 'cos_01J...'
  project_id     text NOT NULL,
  series_scope   text NOT NULL,
  display_name   text NOT NULL,
  costume_kind   text NOT NULL CHECK (costume_kind IN (
                   'character_outfit','uniform','group_costume','generic_wardrobe')),
  character_ref  jsonb,                      -- {service_id:'S11', id:'chr_linlan'} 软引用，可空
  status         text NOT NULL CHECK (status IN ('active','deprecated','tombstoned')),
  canonical_revision integer,
  current_revision   integer NOT NULL DEFAULT 0,
  created_by     jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, costume_id),
  UNIQUE (tenant_id, series_scope, display_name, costume_kind),
  CONSTRAINT canonical_within_head CHECK (canonical_revision IS NULL OR canonical_revision <= current_revision)
);
CREATE INDEX costume_by_series ON costume_assets.costume (tenant_id, series_scope, status, costume_kind);
CREATE INDEX costume_by_character
  ON costume_assets.costume (tenant_id, (character_ref->>'id'))
  WHERE character_ref IS NOT NULL;
```

`costume_kind` 的区分很重要：

- `character_outfit`：某角色的某套造型，`character_ref` 通常非空。
- `uniform`：制服，多个角色共用同一定义。
- `group_costume`：群像/同款不同人。
- `generic_wardrobe`：背景人物/临时服装，可以没有 canonical（只要求"能用"）。

`generic_wardrobe` 允许 `canonical_revision IS NULL`，因为背景人物的服装不需要全剧一致的唯一基准 —— 强行要求会让 500 个背景角色的登记成本爆炸。

### 2.2 不可变 revision

```sql
CREATE TABLE costume_assets.costume_revision (
  tenant_id         text NOT NULL,
  costume_id        text NOT NULL,
  revision          integer NOT NULL CHECK (revision >= 1),
  content_hash      char(64) NOT NULL,
  payload           jsonb NOT NULL,          -- 完整 costume-revision.v1
  outfit            jsonb NOT NULL,          -- 部件清单（见 §2.3）
  material_palette  jsonb NOT NULL,          -- 材质 + 色板
  accessories       jsonb NOT NULL,          -- 配饰清单
  variant_manifest  jsonb NOT NULL,          -- {damage:{none:ref,torn_shoulder:ref}, wetness:{...}, makeup:{...}}
  variant_axes      jsonb NOT NULL,          -- 本 revision 支持的轴与取值（复制自 schema 版本）
  rights_ref        jsonb NOT NULL,
  source_kind       text NOT NULL CHECK (source_kind IN ('manual_upload','generated','imported','derived')),
  parent_revision   integer,
  quality_gate_ref  jsonb NOT NULL,
  accepted_at       timestamptz NOT NULL,
  accepted_by       jsonb NOT NULL,
  PRIMARY KEY (tenant_id, costume_id, revision),
  CONSTRAINT costr_hash_len CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT costr_parent_lt CHECK (parent_revision IS NULL OR parent_revision < revision)
);
CREATE UNIQUE INDEX costr_hash_uniq ON costume_assets.costume_revision (tenant_id, content_hash);
CREATE INDEX costr_variant_axis
  ON costume_assets.costume_revision (tenant_id, costume_id, revision)
  INCLUDE (variant_axes);
```

`variant_axes` 冗余存一份到 revision 里，是为了**历史可解析**：如果将来 S12 的 schema 增加了新轴（例如 `weather_stain`），旧 revision 的 `variant_axes` 仍是旧集合，读取方知道"这一版没有这个轴"，而不是假设它有。

**变体必须全部有 artifact，不允许"变体声明了但没有图"。** 否则 `resolve` 到 `damage=torn_shoulder` 时只能退回 `none`，而那正是"画面与故事状态不匹配"的直接来源。封存时强制校验每个变体值都有 `artifact_ref`。

### 2.3 outfit 结构

```jsonc
{
  "pieces": [
    {"piece_id":"top",       "garment":"blouse",   "color_primary":"#B23A48","color_secondary":null,
     "material":"silk",      "pattern":"solid",    "fit":"fitted",   "coverage":"long_sleeve"},
    {"piece_id":"bottom",    "garment":"skirt",    "color_primary":"#1A1A1A", "material":"wool",
     "pattern":"solid",      "fit":"a_line",       "length":"knee"},
    {"piece_id":"shoes",     "garment":"pumps",    "color_primary":"#000000", "material":"leather",
     "heel_cm_band":"5-8"},
    {"piece_id":"outer",     "garment":"coat",     "color_primary":"#2F4F6F", "material":"wool",
     "removable": true}
  ],
  "layering_order": ["top","bottom","outer","shoes"],
  "silhouette": "structured_waist",
  "era_style": "contemporary_2010s_office"
}
```

约束：

- `pieces` 的 `piece_id` 在 revision 内唯一。
- `layering_order` 必须覆盖所有 piece，且必须能解释遮挡关系（S17 的 `VISUAL_STATE_MISMATCH` 会用到顺序）。
- 颜色用 hex 主色 + 可选辅色，**不接受"深红色"这种自由文本**作为主数据；自由文本只放在 `display_name` 与 `note` 里。
- `era_style` 必须与 S13 的场景 `era` 兼容 —— 但 S12 **不查询** S13。这条兼容性是 S17/S33 的检查项，以及 S12 封存时的**人工确认项**。

### 2.4 材质与配色

```sql
CREATE TABLE costume_assets.costume_material_ref (
  tenant_id    text NOT NULL,
  costume_id   text NOT NULL,
  revision     integer NOT NULL,
  piece_id     text NOT NULL,
  material     text NOT NULL,
  sheen        text CHECK (sheen IN ('matte','satin','metallic','sheer')),
  texture_note text,
  PRIMARY KEY (tenant_id, costume_id, revision, piece_id)
);
CREATE INDEX costume_material_lookup
  ON costume_assets.costume_material_ref (tenant_id, material, sheen);
```

材质单独成表是为了**检索**（"所有丝质长裙"），但它是 `payload` 的派生物，可重建。写入时必须与 `payload.outfit` 一致，有断言测试。

### 2.5 候选与 canonical（与 S11 同构，但各存一份）

```sql
CREATE TABLE costume_assets.costume_candidate (
  tenant_id     text NOT NULL,
  candidate_id  text NOT NULL,
  costume_id    text NOT NULL,
  content_hash  char(64) NOT NULL,
  payload       jsonb NOT NULL,
  evaluation    jsonb NOT NULL,              -- S33/S34 结果 + 身份不变性检查 + 图文匹配分数
  variant_check jsonb NOT NULL,              -- {missing_variants:[...], extra_variants:[...]}
  state         text NOT NULL CHECK (state IN ('PENDING','ACCEPTED','REJECTED','EXPIRED')),
  created_by    jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, candidate_id)
);

CREATE TABLE costume_assets.canonical_pointer (
  tenant_id        text NOT NULL,
  costume_id       text NOT NULL,
  revision         integer NOT NULL,
  decided_at       timestamptz NOT NULL DEFAULT now(),
  decided_by       jsonb NOT NULL,
  decision_kind    text NOT NULL CHECK (decision_kind IN ('human_approved','policy_auto','initial_import')),
  reason_text      text,
  prior_canonical  integer,
  superseded_at    timestamptz,
  PRIMARY KEY (tenant_id, costume_id, revision)
);
CREATE UNIQUE INDEX costume_canonical_one
  ON costume_assets.canonical_pointer (tenant_id, costume_id)
  WHERE superseded_at IS NULL;
```

**不 import S11 的表结构。** 两个服务各自定义一份 `canonical_pointer`。这是数据库-per-service 的必然结果，不是重复设计：两份表没有任何共享行、没有外键、没有跨库 JOIN。相同的是**模式**（pattern），不是**数据**。

`variant_check.missing_variants` 非空时，**`policy_auto` 接受被禁止**。理由见 §2.2：缺变体会导致下游退回 `none`，而那会被 S17 判成画面错误。

### 2.6 引用关系

```sql
CREATE TABLE costume_assets.revision_reference (
  tenant_id       text NOT NULL,
  costume_id      text NOT NULL,
  revision        integer NOT NULL,
  ref_role        text NOT NULL CHECK (ref_role IN ('worn_with','derived_from','merged_from','accessory_of')),
  target_service  text NOT NULL,
  target_id       text NOT NULL,
  target_revision integer NOT NULL,
  target_hash     char(64),
  PRIMARY KEY (tenant_id, costume_id, revision, ref_role, target_service, target_id)
);
```

`worn_with` 用来表达"这套配这套"的**推荐组合**（不是强制）。S10 的 binding 决定实际组合。

### 2.7 基础设施

与 S11 同构（`idempotency_record` / `outbox` / `inbox`），表名与列名一字不差，但**在 S12 自己的库里**。
照抄一遍，免得落地时还要跨文档翻 S11：

```sql
CREATE TABLE costume_assets.idempotency_record (
  tenant_id text NOT NULL, command_id text NOT NULL, request_hash char(64) NOT NULL,
  command_type text NOT NULL, state text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code integer, response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE costume_assets.outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id text NOT NULL, aggregate_id text NOT NULL, aggregate_sequence bigint NOT NULL,
  event_type text NOT NULL, partition_key text NOT NULL, payload jsonb NOT NULL,
  headers jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON costume_assets.outbox (id) WHERE published_at IS NULL;

CREATE TABLE costume_assets.inbox (
  consumer text NOT NULL, event_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

`partition_key = tenant_id || ':' || aggregate_id`，中继按它单线程发布（[S08 §6.4](S08-revision-snapshot.md)）。

### 2.8 不建的表

- ❌ `costume_character_identity`（身份归 S11）。
- ❌ `costume_story_state` / `costume_damage_history`（剧情归 S16）。
- ❌ `costume_wearing_binding`（绑定归 S10；S12 存了就是第二份"她这场穿什么"）。
- ❌ `costume_rights_status`（只存指针）。
- ❌ `costume_scene_compat`（scene 的 era 归 S13；跨类型兼容性归 S17/S33 检查）。

---

## 3. 状态机

### 3.1 候选

```
PENDING ──accept──► ACCEPTED
   ├──reject──► REJECTED
   └──ttl────► EXPIRED
```

### 3.2 Costume 与 canonical

```
active ──deprecate──► deprecated ──tombstone──► tombstoned
```

canonical：

```
(无) ──accept rev1──► rev1 ──accept rev3──► rev3
       ↑                          │
       └────── 回退（set-canonical，留审计）┘
```

`generic_wardrobe` 允许永久无 canonical。

### 3.3 非法转移

| 从 | 到 | 原因 |
|---|---|---|
| `tombstoned` | `active` | 复活走新 `costume_id` |
| 已存在 revision | 覆写 | revision 不可变 |
| 变体声明缺 artifact | 接受 | 会让下游静默退回 `none` |
| `tombstoned` 服装 | 新 canonical | 已下线不应有新基准 |

### 3.4 与 docs/10 四态映射

| docs/10 | 本文 |
|---|---|
| `draft` | `costume_candidate(PENDING)` |
| `candidate` | 已有 revision 但非 canonical（或 `generic_wardrobe` 无 canonical） |
| `accepted canonical` | `canonical_pointer` 当前行 |
| `deprecated` / `tombstoned` | `costume.status` |

---

## 4. 关键流程

### 4.1 起草候选

```
POST /costumes/{id}/candidates
{
  command_id, expected_current_revision, source_kind,
  outfit, material_palette, accessories,
  variant_manifest: {damage:{none:{artifact_ref}, torn_shoulder:{artifact_ref}}, wetness:{...}, makeup:{...}},
  variant_axes,
  rights_ref, quality_gate_ref
}
```

事务外：

1. 逐个校验 `variant_manifest` 里的 `artifact_ref`（S28 `HEAD`）：存在 + hash 一致。
2. **变体覆盖完整性**：`variant_axes` 中的每个轴，每个取值必须有 artifact。缺 → 422 `VARIANT_COVERAGE_INCOMPLETE {axis,missing_values}`。
3. **outfit 自洽**：`layering_order` 覆盖所有 `piece_id`；`piece_id` 唯一。不成立 → 422 `OUTFIT_INCONSISTENT`。
4. **颜色格式**：`color_primary` 必须是 hex。不成立 → 422 `COLOR_FORMAT_INVALID`。
5. `accessories` 里的每一项如果引用 S14 的道具（例如"项链"在 S14 有独立定义），需要 `target_service/id/revision`；S12 **不校验**它存在（S14 可能还没建），只记录 `unverified_refs`。

事务内：锁 `costume` → CAS `expected_current_revision` → 写候选人。

### 4.2 接受候选

```
POST /costumes/{id}/candidates/{cid}:accept
{command_id, expected_current_revision, set_canonical, decision_kind, reason_text}
```

事务内：

```
BEGIN;
-- 1) 锁 costume 行，CAS
-- 2) 锁 candidate 行，state='PENDING'
-- 3) set_canonical=true 时：
--      variant_check.missing_variants 非空 且 decision_kind='policy_auto'
--        → 422 VARIANT_COVERAGE_INCOMPLETE
--      identity_invariance_check != 'pass' 且 policy_auto
--        → 422 IDENTITY_CHECK_REQUIRED_FOR_AUTO
-- 4) INSERT costume_revision (revision = current+1)
--    唯一索引冲突 → 409 DUPLICATE_COSTUME_CONTENT
-- 5) set_canonical → 更新 canonical_pointer（先 sUPERSEDE 旧行，再插入新行）
-- 6) UPDATE costume.current_revision / canonical_revision
-- 7) UPDATE candidate.state='ACCEPTED'
-- 8) INSERT outbox (asset.costume.revision.accepted, seq=rev)
COMMIT;
```

**身份不变性检查从哪来？** `quality_gate_ref` 指向 S33/S34 的 QC 结果，其中包含"角色身份不变"这一项（docs/10 服装质量门第一条）。S12 不做视觉比对，只**读取 QC 结论**。S12 自己调模型做比对就是绕过 S33/S34，且会让服装服务依赖 GPU。

### 4.3 读路径

```
GET /costumes/{id}
GET /costumes/{id}/revisions/{revision}
GET /costumes/{id}/revisions/{revision}?variant=damage:torn_shoulder,wetness:damp
GET /costumes/{id}/canonical
GET /costumes:lookup?series_scope=&alias=
```

变体查询的语义：

- 给定 `variant` 组合 → 返回该组合对应的 artifact 集合。
- 组合中某个轴缺值 → **不静默退回 `none`**，而是返回 422 `VARIANT_NOT_AVAILABLE {axis,value,available:[...]}`。

这条是硬约束。静默退回会让 S17 的画面检查"通过"，而实际画面与故事状态不符（第 6 场说撕破了，画面却是完好的）。

响应：

```jsonc
{
  "costume_ref":{"service_id":"S12","id":"cos_linlan_reddress","revision":2,"content_hash":"…"},
  "character_ref":{"service_id":"S11","id":"chr_linlan"},   // 软引用，可能为 null
  "variant":{"damage":"torn_shoulder","wetness":"dry","makeup":"natural"},
  "artifacts":{
    "front":{"service_id":"S28","id":"art_...","content_hash":"…"},
    "detail_shoulder":{...}
  },
  "outfit":{...},
  "rights_ref":{...},
  "is_canonical": true,
  "canonical_revision": 2,
  "rights_recheck_required": true
}
```

`variant` 回显实际解析到的组合（可能包含"未指定轴取默认"），让调用方知道自己拿到的是什么。

### 4.4 失败矩阵

| # | 场景 | 行为 | 错误码 |
|---|---|---|---|
| F1 | artifact 缺失 / hash 不符 | 拒候选 | 422 `ARTIFACT_NOT_FOUND` / `ARTIFACT_HASH_MISMATCH` |
| F2 | 变体轴缺取值 | 拒候选 | 422 `VARIANT_COVERAGE_INCOMPLETE {axis,missing_values}` |
| F3 | outfit 分层顺序不覆盖 | 拒 | 422 `OUTFIT_INCONSISTENT` |
| F4 | 颜色非 hex | 拒 | 422 `COLOR_FORMAT_INVALID` |
| F5 | 读时请求不存在的变体值 | **不退回默认** | 422 `VARIANT_NOT_AVAILABLE {axis,value,available}` |
| F6 | 同内容重复接受 | 拒 | 409 `DUPLICATE_COSTUME_CONTENT` |
| F7 | `expected_current_revision` 落后 | 拒 | 409 `VERSION_CONFLICT` |
| F8 | 身份 QC 未通过而 `policy_auto` | 拒 | 422 `IDENTITY_CHECK_REQUIRED_FOR_AUTO` |
| F9 | 候选非 PENDING | 拒 | 409 `CANDIDATE_NOT_PENDING` |
| F10 | 服装 tombstoned 后接受 canonical | 拒 | 422 `COSTUME_TOMBSTONED` |
| F11 | S28 超时 | 不落候选 | 503 `DEPENDENCY_UNAVAILABLE` |
| F12 | 跨租户 | 404 | `NOT_FOUND` |
| F13 | 两个并发接受 | 一成功一 409 | 行锁 |
| F14 | 引用了 S14 的道具但 S14 无该 revision | **允许**，记 `unverified_refs` | 200/201，响应带警告列表 |
| F15 | Outbox 已写未发 | 重启补发 | — |

F14 的取舍：服装上的配饰常常先在服装侧出现、后进道具库。硬拒会让流程卡住；但不记录就没人知道它是悬空的。折中是"允许 + 显式记录"，由 S09/S17 在需要时看到。

---

## 5. 对外契约

### 5.1 命令

| 方法 | 路径 |
|---|---|
| POST | `/costumes` |
| POST | `/costumes/{id}/candidates` |
| POST | `/costumes/{id}/candidates/{cid}:accept` |
| POST | `/costumes/{id}/candidates/{cid}:reject` |
| POST | `/costumes/{id}:set-canonical` |
| POST | `/costumes/{id}:deprecate` / `:tombstone` / `:merge` |
| POST | `/costumes/{id}/revision-references` |

### 5.2 查询

见 §4.3。

### 5.3 事件

发布（`drama.costume-assets.v2`，partition key `tenant_id:costume_id`，`aggregate_sequence = revision`）：

```jsonc
{
  "specversion":"1.0","schema_version":"2.0","id":"evt_...","source":"S12",
  "type":"asset.costume.revision.accepted",
  "subject":"costume/cos_linlan_reddress","time":"2026-10-31T09:00:00Z",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"cos_linlan_reddress","aggregate_sequence":2,
  "correlation_id":"cmd_...","causation_id":"cmd_...","traceparent":"00-...",
  "data":{
    "costume_ref":{"service_id":"S12","id":"cos_linlan_reddress","revision":2,"content_hash":"…"},
    "character_ref":{"service_id":"S11","id":"chr_linlan"},
    "costume_kind":"character_outfit",
    "became_canonical": true,
    "canonical_revision": 2,
    "variant_axes":{"damage":["none","light","torn_shoulder","bloodied"],"wetness":["dry","damp","soaked"]},
    "unverified_refs":[],
    "rights_ref":{...}
  },
  "data_ref":null
}
```

`variant_axes` 必须发出去。否则订阅方（S17 的期望状态校验）不知道"这一版支持哪些破损档"，就只能猜。

消费：

| 事件 | 来源 | 处理 |
|---|---|---|
| `artifact.archived` | S28 | 审计 |
| `asset.prop.revision.accepted` | S14 | 把 `unverified_refs` 里匹配的项标记已验证（仅本地标记） |

**不消费** S11 的事件。服装服务不知道也不该知道角色 identity 变了；如果角色换了人，是 S10 的 binding 要重新指向新服装，不是 S12 自动跟着改。

### 5.4 错误码汇总

| HTTP | code | retryable |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | false |
| 403 | `TENANT_SCOPE_DENIED` | false |
| 404 | `NOT_FOUND` / `CANONICAL_NOT_SET` | false |
| 409 | `VERSION_CONFLICT`(true) / `CANDIDATE_NOT_PENDING` / `DUPLICATE_COSTUME_CONTENT` / `IDEMPOTENCY_KEY_REUSED` | 见括号 |
| 422 | `ARTIFACT_NOT_FOUND` / `ARTIFACT_HASH_MISMATCH` / `VARIANT_COVERAGE_INCOMPLETE` / `VARIANT_NOT_AVAILABLE` / `OUTFIT_INCONSISTENT` / `COLOR_FORMAT_INVALID` / `IDENTITY_CHECK_REQUIRED_FOR_AUTO` / `COSTUME_TOMBSTONED` | false |
| 429 | `WRITE_BACKPRESSURE` | true |
| 503 | `DEPENDENCY_UNAVAILABLE` | true |

---

## 6. 幂等与并发

### 6.1 幂等

- 键 `(tenant_id, command_id)` + JCS hash。
- `POST /costumes` 自然幂等：`(tenant_id, series_scope, display_name, costume_kind)` 唯一。

### 6.2 并发

| 竞争 | 机制 |
|---|---|
| 两次接受同 `expected_current_revision` | `costume` 行 `FOR UPDATE` |
| 两次设 canonical | `costume_canonical_one` 部分唯一索引 |
| 同内容重复 | `costr_hash_uniq` |
| 变体声明并发改 | 只在候选里改；revision 不可变 |

### 6.3 锁顺序

```
1. costume (FOR UPDATE)
2. costume_candidate (FOR UPDATE)
3. canonical_pointer
4. costume_revision (INSERT)
5. costume_material_ref (INSERT, 派生)
6. outbox
```

---

## 7. 可观测

指标：

- `s12_candidate_total{result}`、`s12_accept_total{set_canonical,costume_kind}`
- `s12_variant_coverage_missing_total{axis}`
- `s12_variant_not_available_total{axis,value}` ← **重点告警指标**
- `s12_unverified_prop_ref_total`
- `s12_canonical_change_total{decision_kind}`

告警：

- `s12_variant_not_available_total` 上升 → S16 的期望状态在要求 S12 没有的变体。这是跨服务语义脱节的早期信号，必须查（否则 S17 会开始报画面不符）。
- `s12_variant_coverage_missing_total` > 0 且被 `policy_auto` 接受 → 门失效。
- 单集内 `costume_canonical_change_total` > 3 → 服装基准在抖。

Runbook：

- `variant-coverage-report`：列出每个 revision 的轴覆盖，找缺口。
- `rehash-revision`：校验 `payload` hash 与 `costume_material_ref` 一致性。
- `canonical-audit`：确认 `canonical_pointer` 当前行与 `costume.canonical_revision` 一致。

---

## 8. 测试清单

### E02 · 三集连续

| E02-C1 同一套服装三集 canonical 不变 | 三集 `resolve` 同一 `costume_ref` |
| E02-C2 第 3 集换新服装（新 costume_id） | 第 1、2 集旧 ID 不变；第 3 集新 ID；S11 无新 revision |
| E02-C3 破损变体跨集 | 第 6 场 `damage=torn_shoulder`，第 7 场同一 binding revision → 同结果（S16 决定持续性，S12 只提供变体） |
| E02-C4 角色身份随妆造变化检测 | S33 报告身份保持；S12 只记录 QC 结论，不改 identity |

### E08 · 改前集

| E08-C1 第 2 集补一张细节图（`set_canonical=false`） | 第 1、3 集不变 |
| E08-C2 canonical 回退 | 指针改，无新 revision |

### E12 · 幂等

与 S11 同构：重复接受返回首次结果；同键不同 body 409；重复登记返回既有 ID。

### E28 · 跨租户

| E28-C1 跨租户读 revision | 404 |
| E28-C2 同一服装名两租户 | 独立 |
| E28-C3 变体查询跨租户 | 404 |

### E30 · 权利

| E30-C1 服装授权撤销后 S12 仍返回 revision | `rights_recheck_required=true` |
| E30-C2 权利版本变化 | 不自动改；新 revision 需新 `rights_ref` |

### E24/E25 · QC

| E24-C1 图文匹配失败 | 候选 REJECTED，不写 revision |
| E24-C2 身份变化（换脸） | QC 报 `IDENTITY_CHANGED`；`policy_auto` 被拒 |
| E25-C1 修复后重起草 | 新 candidate_id |

### E26 · 修复

| E26-C1 S39 提议替换某变体 artifact | S12 生成新 revision（不是原地改）；旧 revision 保留 |
| E26-C2 修复只影响一个变体 | 其余变体 hash 不变；`content_hash` 整体变（因为 payload 变了） |

### 通用

| 变体矩阵全覆盖 | 每个 `variant_axes` 取值都有 artifact；`variant_check.missing_variants=[]` |
| 读不存在的变体 | 422，不静默退回 |
| 并发接受 | 1 成功 |
| 崩溃恢复 | 无半写 |
| 乱序事件 | 缓冲/回源 |
| 独立升级 | 旧 payload 可读 |

---

## 9. 反模式

1. ❌ 读变体缺失时静默退回 `none`。
2. ❌ 在 S12 记录"这一场角色穿什么"（归 S10 binding + S16 叙述）。
3. ❌ 记录"这件衣服在哪几场被穿过"（反向索引归 S10/S09）。
4. ❌ 让 `character_ref` 成为强外键/强校验（通用服装会卡死，且引入同步依赖）。
5. ❌ S12 自己调视觉模型判断"身份是否变化"（归 S33/S34）。
6. ❌ 在 S12 存"衣服破损的剧情原因"（归 S16）。
7. ❌ 颜色用自由文本作主数据。
8. ❌ 允许变体声明但没有 artifact。
9. ❌ 原地改 revision。
10. ❌ 在 S12 判定权利。
11. ❌ 与 S11 共享表/库/ORM 模型。
12. ❌ 把 `variant_axes` 写死成代码常量（必须随 revision 存，保证历史可解析）。

---

## 10. 冲突（报告，不选边）

| # | 冲突 / 缺口 | 现状 | 本文用法 |
|---|---|---|---|
| C12-1 | `service.yaml` 只给 `POST /costumes/{id}/revisions` + `GET /costumes/{id}/revisions/{revision}`；没有候选/接受/变体查询 | catalog | 按 §5 拆分；未改契约 |
| C12-2 | docs/10 服装质量门写"角色身份不变"，但没说这个结论由谁产出 | docs/10 | 本文假设 S33/S34 的 QC 产出，S12 只读结论（§4.2） |
| C12-3 | docs/10 说"S12 持有破损湿度变体"，但破损何时发生/持续多久在 S16；两者接口无显式对齐 | docs/10 | 本文定义 `variant_axes` 由 S16 的期望状态选择（S17 执行）；需设计方确认 compliment |
| C12-4 | `event-topics.v2.json` 只有 `asset.costume.revision.accepted`；变体轴变化没有单独事件 | topics | 在同一事件的 `variant_axes` 字段承载；不新增 topic |
| C12-5 | 服装配饰如果也是 S14 的道具，存在双重定义风险（同一项链既是 costume accessory 又是 prop） | docs/10 | 本文允许 `accessories[]` 引用 S14 revision 并记 `unverified_refs`；**谁拥有配饰**需设计方裁决 |
| C12-6 | docs/10 未定义 `costume_kind`（制服/群像/背景） | docs/10 | 本文新增枚举；需确认是否进入契约 |
| C12-7 | `service.yaml` 的 `depends_on` 是 `[S28,S01]`，但 S12 实际还需要 S33/S34 的 QC 结论 | catalog | 事件消费不算启动依赖；若走同步调用需改 catalog |

---

## 11. 实施顺序与未决问题

### 11.1 顺序

1. 契约：`contracts/assets/costume-revision.v1.schema.json`（含 `variant_axes` 与 `variant_manifest` 的关系）。
2. 平台 SDK 复用（JCS/hash/`rights_ref`）。
3. 最小闭合：登记 + 起草候选（mock S28）+ 接受 + 读 revision + 变体查询。先验 E12/E28。
4. 接真实 S28 与 S33/S34 的 QC 结论。
5. 接 S14 道具引用（`unverified_refs` 消解）。
6. `policy_auto` 门（最后）。

### 11.2 未决

1. **配饰到底归 S12 还是 S14？** 项链、戒指既是服装配饰也可能是关键道具。本文选择"服装侧持有搭配关系，道具侧持有可被特写/被持物的定义"。需设计方裁决（C12-5）。
2. **变体轴是否全局固定？** 本文允许 per-revision 的 `variant_axes`。若固定，跨服务比较更容易；若可扩展，更容易支持新需求。当前选后者。
3. **`generic_wardrobe` 是否真的不需要 canonical？** 若 S17 要做"背景人物服装一致"，就需要。需 docs/10 作者确认。
4. **伦理/分级（bloodied 变体）是否需要单独门？** 本文只做数据建模，未加分级字段。若平台有内容分级要求，`variant_axes` 需要带 `content_rating`。
5. **同一套服装跨季复用（不同 `series_scope`）？** 同 S11 的未决问题。
6. **`unverified_refs` 的消解时限？** 长期悬空会让 S17 无法判断配饰是否匹配。是否需要在 N 天后告警/阻断？
