# S11 · 角色资产 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码；SLO 与容量为 docs/03 的规划值，**未实测**。

上游依据：[docs/10](../docs/10-assets.md)（角色行）、[docs/01 §3](../docs/01-enterprise-architecture.md)、[docs/04](../docs/04-data-consistency.md)、[docs/06 §2–§4](../docs/06-automation-governance.md)、[docs/14 §2](../docs/14-enterprise-operations.md)、[S11 规格](../docs/design-package/services/S11-character-assets.md)。

配套：[S10](S10-asset-catalog.md)（索引与绑定，**只读引用**）、[S12](S12-costume-assets.md) / [S13](S13-scene-assets.md) / [S14](S14-prop-assets.md) / [S15](S15-voice-assets.md)（**同级，不互相 import、不互相写库**）、[S28](S28-artifact-ingest.md)、[S02](S02-governance-policy.md)、[S16](S16-story-memory.md)、[S17](S17-continuity.md)、[S48](S48-adaptation-training.md)。

---

## 0. 一句话边界

**S11 是"这个角色长什么样、哪一版算数、它的身份特征是什么"的唯一权威。**

它回答三件事，且只回答这三件：

1. **角色身份（identity）**：这个角色是谁，身份特征（脸型、骨骼点、发际线、痣、年龄感）是什么。
2. **多视图设定集**：正面/侧面/背面/3/4 侧、表情表、体型表，各自是哪一版、由哪些 artifact 组成。
3. **canonical 指针**：哪一版被接受为"全剧通用身份基准"。

它不回答：

- **这个角色穿什么** → S12（服装是独立资产，有独立 canonical）。
- **这个角色现在在哪、什么伤势、知道什么** → S16。
- **这个角色用什么声音** → S15。
- **这个角色这一集绑了哪一版** → S10。
- **这个角色能不能被使用（权利是否仍有效）** → S02 实时判定。
- **这个角色的参考图字节** → S28。

### 0.1 写什么 / 不写什么

| 事实 | 唯一写者 |
|---|---|
| `CharacterRevision`（不可变） | **S11** |
| 多视图 manifest（每个视图一个 artifact 引用） | **S11** |
| 身份特征向量 / 结构化描述 | **S11** |
| canonical 接受状态（`canonical_pointer`） | **S11** |
| 角色级权利引用（`rights_ref`） | **S11 存指针**，判定归 S02 |
| 角色别名与检索词 | S10（S11 发布，S10 持有） |
| 角色的项目/剧归属 | S03 剧本侧 + S10 identity；S11 只存 `series_scope` |
| 角色与演员/原型人物的对应 | 合同侧（S03/S02 的 rights 对象）；S11 只存引用 |
| LoRA 身份保持微调 | S48；S11 只存"可用的 LoRA 引用"作为候选 |

### 0.2 四个明确不做

1. **不把"最近生成的图"当 canonical。** docs/10 原句："canonical不是最近生成图，不能让漂移结果污染后续集。" 见 §3 状态机。
2. **不在 S11 记录服装/妆造。** 换装、破损、湿度是 S12 的事。S11 一旦记录"这一集穿红裙"，就会出现两份服装事实。
3. **不在 S11 记录剧情状态。** 谁受伤了、谁在哪、谁知道什么，全在 S16。S11 的 `variant_selector` 里绝不出现 `injured: true` 这类字段。
4. **不判定权利。** 只存 `rights_ref` 指针；调用前由 S02 实时复查（E30）。

---

## 1. 关键区分

### 1.1 身份（identity）vs 外观（appearance）vs 造型（styling）

| 层 | 所有者 | 例子 | 变化频率 |
|---|---|---|---|
| identity | **S11** | 脸型、骨骼比例、眼距、痣的位置、发际线 | 全剧固定，换一次就是换人 |
| appearance base | **S11** | 发型长度形态、体型、年龄感 | 全剧固定（换发型是 S11 的新 revision，但发型"造型"是 S12） |
| styling | **S12** | 具体 outfit、妆造、配饰、破损/湿度 | 按集、按场变化 |
| story state | **S16** | 受伤、持物、知情 | 按场变化，不回写资产 |

关键判断：**发型归属最容易吵。** 本文的裁决（写死在代码里，不留给实现者临场决定）：

- "这个人天生什么发质、多长、能不能剪短" → S11（identity 层，因为它影响脸部轮廓识别）。
- "这场戏头发扎起来还是放下、有没有发饰" → S12（styling 层）。

判据：**换掉这个决定之后，模型还能认出是同一个人吗？** 能 → styling（S12）；不能 → identity（S11）。

### 1.2 `CharacterRevision` vs `canonical_pointer`

- `CharacterRevision` 是**不可变的外观快照**：一组多视图 artifact + 身份特征 + 权利指针。冻结后不改。
- `canonical_pointer` 是**一个显式的接受决定**：指向某个 revision，带审批人与理由。

两者分开的原因：如果 canonical 就是"最新 revision 标记"，那么任何一次候选生成都会自动改变全剧身份基准 —— 这正是 docs/10 禁止的漂移污染。canonical 必须是一次**人为/策略的接受**，可以落后于最新 revision。

### 1.3 候选（candidate）与已接受（accepted）

候选 revision 存放在**隔离区**，不进入生产读路径。差别不是"状态字段不同"，而是：

- 候选不在 `character_revision` 表（在 `character_candidate` 表）。
- 候选没有 `canonical_eligible` 字段。
- 候选不能被 S10 绑定（`resolve` 只认 accepted revision）。

这与 [S16 §1](S16-story-memory.md) 的 `claim_candidate` 隔离是同一个模式：**未接受的东西在结构上无法被误用**，而不是靠代码里的 if 判断。

---

## 2. 数据模型

Postgres，schema `character_assets`。所有表含 `tenant_id`，主键/唯一约束含 `tenant_id`（E28）。RLS 作为第二道防线（docs/14 §2）。

### 2.1 角色身份

```sql
CREATE TABLE character_assets.character (
  tenant_id        text NOT NULL,
  character_id     text NOT NULL,            -- 'chr_01J...'（owner 本地 ID，S10 注册时用）
  project_id       text NOT NULL,
  series_scope     text NOT NULL,            -- series_id；跨剧复用暂不支持（见 §11）
  display_name     text NOT NULL,
  identity_kind    text NOT NULL CHECK (identity_kind IN ('fictional','actor_likeness','real_person')),
  status           text NOT NULL CHECK (status IN ('active','deprecated','tombstoned')),
  canonical_revision integer,                -- 指向 character_revision；NULL = 尚未接受
  current_revision   integer NOT NULL DEFAULT 0,
  series_of_record text NOT NULL,            -- 首次登记所在 series（用于审计）
  created_by       jsonb NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, character_id),
  UNIQUE (tenant_id, series_scope, display_name),
  CONSTRAINT canonical_within_head CHECK (canonical_revision IS NULL OR canonical_revision <= current_revision)
);
CREATE INDEX character_by_series ON character_assets.character (tenant_id, series_scope, status);
```

`identity_kind='actor_likeness'` / `'real_person'` 时，**必须有 `rights_ref` 且不可为空**。真人肖像权是最容易被漏掉的一类；在数据模型上强制它，比在代码评审里记得它可靠。见 §5.2 的约束触发器。

```sql
CREATE TABLE character_assets.character_alias (
  tenant_id     text NOT NULL,
  character_id  text NOT NULL,
  alias_text    text NOT NULL,
  alias_kind    text NOT NULL CHECK (alias_kind IN ('script_name','nickname','legacy_id','import_alias')),
  source        text NOT NULL,               -- S03|manual|import_batch
  PRIMARY KEY (tenant_id, character_id, alias_text, alias_kind)
);
```

别名支持剧本名对账（"林总" → `chr_linlan`）。**歧义不自动解析**：同名两个角色时，导入流程必须报 `AMBIGUOUS_ALIAS` 让人选，不能取第一个。

### 2.2 不可变 revision

```sql
CREATE TABLE character_assets.character_revision (
  tenant_id        text NOT NULL,
  character_id     text NOT NULL,
  revision         integer NOT NULL CHECK (revision >= 1),
  content_hash     char(64) NOT NULL,        -- sha256(JCS(payload))
  payload          jsonb NOT NULL,           -- 完整 character-revision.v1
  view_manifest    jsonb NOT NULL,           -- {front:{artifact_ref,...}, side_left:..., back:..., three_quarter:...}
  expression_manifest jsonb NOT NULL,        -- {neutral:..., smile:..., angry:...}
  body_profile     jsonb NOT NULL,           -- {height_cm_band, build, age_band}
  identity_features jsonb NOT NULL,          -- 结构化特征（见 §2.3）
  identity_feature_hash char(64) NOT NULL,   -- sha256(JCS(identity_features))
  rights_ref       jsonb NOT NULL,           -- {rights_owner,rights_id,rights_revision,rights_hash}
  lora_refs        jsonb NOT NULL DEFAULT '[]', -- [{artifact_id, base_snapshot, revision}] 来自 S48
  source_kind      text NOT NULL CHECK (source_kind IN ('manual_upload','generated','imported','derived')),
  parent_revision  integer,                  -- 派生来源
  quality_gate_ref jsonb NOT NULL,           -- 通过的质量门结果（S33/S34/S01）
  accepted_at      timestamptz NOT NULL,
  accepted_by      jsonb NOT NULL,
  PRIMARY KEY (tenant_id, character_id, revision),
  CONSTRAINT cr_hash_len CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT cr_parent_lt CHECK (parent_revision IS NULL OR parent_revision < revision)
);
CREATE UNIQUE INDEX cr_hash_uniq ON character_assets.character_revision (tenant_id, content_hash);
CREATE INDEX cr_identity_hash
  ON character_assets.character_revision (tenant_id, character_id, identity_feature_hash);
```

`content_hash` 内容寻址 + 唯一索引。同一份内容不能有两个 revision —— 否则"第 3 集用的是哪一版"会因重复注册而失去唯一答案。

`identity_feature_hash` 是**身份漂移检测的锚**：同一角色的任意两个 revision，如果 `identity_feature_hash` 不同，就意味着"身份发生了变化"。S17 与 S11 自己的门都用它做快速比对。

**`payload` 存全文 + 冗余列并存**：冗余列（`view_manifest` 等）服务于查询与索引，`payload` 服务于 hash 验证与回溯。两者必须在写入时来自同一份对象，且有测试断言 `sha256(JCS(payload)) == content_hash`。

### 2.3 身份特征的结构化形态

必须**结构化**，不能是自由文本。自由文本"瓜子脸、双眼皮"无法比较、无法回归。

```jsonc
{
  "face_shape": "oval|round|square|heart|long",
  "jaw": "soft|defined|angular",
  "eye_shape": "almond|round|hooded|monolid",
  "eye_distance_ratio": 0.31,          // 眼距/脸宽，0–1
  "brow": "straight|arched|thick|thin",
  "nose_bridge": "low|medium|high",
  "lip_fullness": "thin|medium|full",
  "skin_tone_band": "1|2|3|4|5|6",      // 分档而非精确色值（避免跨模型色偏争议）
  "hair": {"length":"short|medium|long","texture":"straight|wavy|curly","hairline":"straight|widows_peak|rounded"},
  "age_band": "teen|20s|30s|40s|50s+",
  "build": "slim|athletic|average|heavy",
  "distinguishing_marks": [{"kind":"mole","location":"left_cheek_lower","size_mm_band":"2-4"}],
  "height_cm_band": "155-160|160-165|..."
}
```

约束：

- `distinguishing_marks` 是**身份锚**，无论换装换场景都必须一致（S17 用它做 `VISUAL_STATE_MISMATCH` 检查）。
- 数值字段必须是区间/分档，不接受精确浮点 —— 精确值会诱导实现者做"像素级比对"，而不同模型的生成天然有细微差异。
- 新增字段必须**可选**且带默认，保证旧 revision 可解析（E01 回滚要求）。

### 2.4 候选隔离区

```sql
CREATE TABLE character_assets.character_candidate (
  tenant_id        text NOT NULL,
  candidate_id     text NOT NULL,            -- 'cand_01J...'
  character_id     text NOT NULL,
  content_hash     char(64) NOT NULL,
  payload          jsonb NOT NULL,
  evaluation       jsonb NOT NULL,           -- S33/S34 结果 + 身份一致性分数
  identity_drift   jsonb NOT NULL,           -- {vs_canonical: {...}, drift_class: 'none|minor|major'}
  state            text NOT NULL CHECK (state IN ('PENDING','ACCEPTED','REJECTED','EXPIRED')),
  created_by       jsonb NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, candidate_id)
);
CREATE INDEX candidate_pending
  ON character_assets.character_candidate (tenant_id, character_id, state, created_at DESC)
  WHERE state = 'PENDING';
```

候选表**没有** `revision`、**没有** `canonical_eligible`、**没有** `accepted_at`。它无法被 S10 绑定，因为 S10 只调 `GET /characters/{id}/revisions/{revision}`，而候选不在这条路径上。

`candidate_state='EXPIRED'` 由定时任务按 `expires_at` 置位；候选不会被无限期堆积（存储成本与误用风险）。

### 2.5 canonical 指针（显式接受）

```sql
CREATE TABLE character_assets.canonical_pointer (
  tenant_id      text NOT NULL,
  character_id   text NOT NULL,
  revision       integer NOT NULL,
  decided_at     timestamptz NOT NULL DEFAULT now(),
  decided_by     jsonb NOT NULL,             -- 人或策略
  decision_kind  text NOT NULL CHECK (decision_kind IN ('human_approved','policy_auto','initial_import')),
  reason_text    text,
  prior_canonical integer,
  superseded_at  timestamptz,                -- NULL = 当前有效
  PRIMARY KEY (tenant_id, character_id, revision)
);
CREATE UNIQUE INDEX canonical_one_current
  ON character_assets.canonical_pointer (tenant_id, character_id)
  WHERE superseded_at IS NULL;
```

`canonical_one_current` 是**"同一角色同时只有一个 canonical"的物理保证**。没有这条索引，两个并发接受会让角色有两个身份基准 —— 那是最坏的一类数据损坏，因为它不报错，只是悄悄让人物在两个方向漂。

`decision_kind='policy_auto'` 只允许在以下条件同时满足时使用（否则必须人审）：

- `identity_drift.drift_class = 'none'`
- 与当前 canonical 的 `identity_feature_hash` 完全相同
- 只改了视图（例如补一张背面图）或分辨率

### 2.6 引用与派生关系

```sql
CREATE TABLE character_assets.revision_reference (
  tenant_id       text NOT NULL,
  character_id    text NOT NULL,
  revision        integer NOT NULL,
  ref_role        text NOT NULL,             -- base_for_lora|derived_from|merged_from
  target_service  text NOT NULL,             -- S48|S11
  target_id       text NOT NULL,
  target_revision integer NOT NULL,
  target_hash     char(64),
  PRIMARY KEY (tenant_id, character_id, revision, ref_role, target_service, target_id)
);
```

只记录**出边**。入边（谁引用了这个角色）属于 S10 的 `binding_usage` 与 S09 的依赖图，S11 不维护反向表 —— 反向表就是第二份影响分析。

### 2.7 基础设施

```sql
CREATE TABLE character_assets.idempotency_record (
  tenant_id text NOT NULL, command_id text NOT NULL, request_hash char(64) NOT NULL,
  command_type text NOT NULL, state text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code integer, response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE character_assets.outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id text NOT NULL, aggregate_id text NOT NULL, aggregate_sequence bigint NOT NULL,
  event_type text NOT NULL, partition_key text NOT NULL, payload jsonb NOT NULL,
  headers jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);

CREATE TABLE character_assets.inbox (
  consumer text NOT NULL, event_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

### 2.8 不建的表

- ❌ `character_costume`（服装归 S12）。
- ❌ `character_story_state` / `character_injury`（归 S16）。
- ❌ `character_voice`（归 S15）。
- ❌ `character_usage_index`（归 S10 投影 / S09 依赖边）。
- ❌ `character_rights_status`（只存指针，不存判定）。
- ❌ `character_latest_generation`（canonical 不是最新生成）。

---

## 3. 状态机

### 3.1 候选（Candidate）

```
PENDING ──accept──► ACCEPTED（并写入 character_revision）
   │
   ├──reject──► REJECTED
   └──ttl────► EXPIRED
```

只有 `PENDING` 可接受。`ACCEPTED` 之后候选行保留（审计），但不再参与任何读路径。

### 3.2 CharacterRevision（不可变）

```
(accepted) ──► immutable forever
```

没有状态字段。revision 一旦写入即终态。这是刻意的：可变 revision 会让 `content_hash` 变成谎言。

### 3.3 角色（Character）与 canonical

```
active ──deprecate──► deprecated ──tombstone──► tombstoned
   │
   └── merge ──► merged_into（status='deprecated', merged_into=target）
```

canonical 单独建模：

```
canonical: (无) ──accept rev1──► rev1 ──accept rev3──► rev3（rev1 superseded_at set）
```

**非法转移（明确拒绝，返回 409/422）**：

| 从 | 到 | 为什么非法 |
|---|---|---|
| `tombstoned` | `active` | 复活要走新 character_id，否则历史 manifest 指向的东西变了 |
| `revision` 已存在 | 覆写 | revision 不可变；要改就追加新 revision |
| `canonical` 指向 `candidate` | 任意 | 候选不在 revision 表，结构上不可能 |
| 角色已 `tombstoned` | 接受新 canonical | 已下线角色不应有新的身份基准 |

### 3.4 与 docs/10 的状态词映射

docs/10 的四态 `draft → candidate → accepted canonical → deprecated/tombstoned` 在本文的落点：

| docs/10 词 | 本文实现 |
|---|---|
| `draft` | `character_candidate(PENDING)`，尚未有任何 revision |
| `candidate` | 同上；已有 revision 但 canonical 未指向它 |
| `accepted canonical` | `canonical_pointer` 当前行 |
| `deprecated` | `character.status='deprecated'` |
| `tombstoned` | `character.status='tombstoned'` |

注意：**revision 可以存在而 canonical 指向另一版**，此时 revision 处于"候选已接受为 revision、但非 canonical"。docs/10 没有区分这两层，本文按 §1.2 的理由区分。这是**文档表述不足**，不是矛盾，但需要设计方确认（§10 C11-2）。

---

## 4. 关键流程

### 4.1 从设定集生成到接受 canonical（主路径）

```
S19/S31 触发设定集生成
  → S26 生成（multi-view + expression，受 S23 预算）
  → S28 归档 artifact，返回 ArtifactManifest
  → S33/S34 QC（身份一致性、多角度一致性）
  → S11 起草 candidate（本服务不调模型，只收 artifact 引用）
  → 人工/策略接受
  → 事务：写 character_revision + 更新 canonical_pointer + outbox
```

**S11 不生成图片。** 它不持有模型调用能力，也不绕过 S26。这一点必须写进代码结构：`character_assets` 的依赖里没有模型连接器，只有 S28 的 artifact 读取和 S26 的事件。

### 4.2 起草候选

```
POST /characters/{id}/candidates
{
  command_id,
  expected_current_revision,
  source_kind,
  view_manifest: {front:{artifact_id,content_hash}, ...},
  expression_manifest: {...},
  body_profile, identity_features,
  rights_ref,
  lora_refs: [...],
  quality_gate_ref
}
```

事务外先做：

1. **artifact 存在性校验**：逐个调 S28 `HEAD /artifacts/{id}`，hash 必须一致。不存在 → 422 `ARTIFACT_NOT_FOUND`；hash 不符 → 422 `ARTIFACT_HASH_MISMATCH`。
2. **必需视图完整性**：`front` 必须有；`side_left`/`back`/`three_quarter` 在 `require_full_turnaround=true` 时必须有。缺 → 422 `VIEW_MANIFEST_INCOMPLETE {missing:[...]}`。
3. **权利引用形态**：`identity_kind IN ('actor_likeness','real_person')` 时 `rights_ref` 必须非空且有 `rights_id` → 否则 422 `RIGHTS_REF_REQUIRED`。

事务内：

4. 锁 `character` 行，校验 `current_revision == expected_current_revision`。不符 → 409 `VERSION_CONFLICT`。
5. 计算 `payload` 与 `content_hash`。
6. `INSERT character_candidate (state='PENDING')`。
7. Outbox：`asset.character.candidate.created`（**本地事件**，不发布到契约 topic；见 §10 C11-4）。

### 4.3 接受候选（写 revision + 可能改 canonical）

```
POST /characters/{id}/candidates/{candidate_id}:accept
{command_id, expected_current_revision, set_canonical: bool, decision_kind, reason_text}
```

事务内顺序（固定，防死锁）：

```
BEGIN;
-- 1) 锁角色行
SELECT current_revision FROM character WHERE tenant_id=$t AND character_id=$c FOR UPDATE;
-- expected_current_revision 不符 → 409 VERSION_CONFLICT

-- 2) 锁候选行，校验 state='PENDING'
--    非 PENDING → 409 CANDIDATE_NOT_PENDING

-- 3) 身份漂移门（set_canonical=true 时）
--    drift = compare(identity_features, canonical 的 identity_features)
--    drift_class='major' 且 decision_kind='policy_auto' → 422 IDENTITY_DRIFT_REQUIRES_HUMAN

-- 4) revision = current_revision + 1
--    INSERT character_revision
--    content_hash 唯一索引冲突 → 409 DUPLICATE_CHARACTER_CONTENT

-- 5) set_canonical:
--    UPDATE canonical_pointer SET superseded_at=now() WHERE ... AND superseded_at IS NULL
--    INSERT canonical_pointer (revision, decision_kind, ...)
--    UPDATE character SET canonical_revision=rev

-- 6) UPDATE character SET current_revision=rev
-- 7) UPDATE candidate SET state='ACCEPTED'
-- 8) INSERT outbox (asset.character.revision.accepted, seq=rev)
COMMIT;
```

**`set_canonical=false` 的用途：** 补一张更清晰的背面图、换一个更好的表情，但身份不变。此时 revision 前进，canonical 不动。这是允许的，也是必要的 —— 否则每次微调都会动全剧身份基准。

### 4.4 拒绝候选

```
POST /characters/{id}/candidates/{candidate_id}:reject
{command_id, reason_code, reason_text}
```

只改 `state='REJECTED'`，不写 revision，不发事件。被拒候选保留 30 天供审计，之后随 TTL 清理（docs/14 保留策略）。

### 4.5 查询（读路径）

```
GET /characters/{id}
GET /characters/{id}/revisions/{revision}
GET /characters/{id}/revisions            ?after=&limit=
GET /characters/{id}/canonical
GET /characters:lookup?series_scope=&alias=
```

`GET /characters/{id}/revisions/{revision}` 的响应（S10 `resolve` 后由 S17/S26 调用）：

```jsonc
{
  "character_ref": {"service_id":"S11","id":"chr_linlan","revision":12,"content_hash":"…"},
  "identity_feature_hash":"…",
  "view_manifest":{"front":{"service_id":"S28","id":"art_...","content_hash":"…"},"..."},
  "expression_manifest":{...},
  "rights_ref":{"rights_owner":"S02","rights_id":"rights_char_linlan","rights_revision":3,"rights_hash":"…"},
  "lora_refs":[...],
  "is_canonical": false,
  "canonical_revision": 9,
  "rights_recheck_required": true
}
```

硬约束：

1. **`is_canonical` 与 `canonical_revision` 必须同时返回。** 只返回 `is_canonical=false` 的调用方会以为"这个角色没有 canonical"。返回一个指针让它知道该回退到哪一版。
2. **`rights_recheck_required` 恒为 true。**
3. 返回的 artifact 引用**不含 URL**。URL 由 S28 短签名发放，TTL 覆盖排队时间；S11 自己不签发也不缓存 URL（docs/10："对象访问短签名"）。
4. 跨租户 / 不存在 → 404（E28）。
5. canonical 不存在时（新角色尚未接受），`GET /canonical` 返回 404 `CANONICAL_NOT_SET`，**不是**返回 200 + null。前者让调用方明确知道"没基准"，后者会被静默当成空对象。

### 4.6 失败矩阵

| # | 场景 | 行为 | 错误码 |
|---|---|---|---|
| F1 | S28 artifact 不存在 | 拒候选 | 422 `ARTIFACT_NOT_FOUND` |
| F2 | artifact hash 与提交不符 | 拒 | 422 `ARTIFACT_HASH_MISMATCH` |
| F3 | 缺必需视图 | 拒 | 422 `VIEW_MANIFEST_INCOMPLETE {missing}` |
| F4 | 真人类缺 `rights_ref` | 拒 | 422 `RIGHTS_REF_REQUIRED` |
| F5 | `expected_current_revision` 落后 | 拒 | 409 `VERSION_CONFLICT {current}` |
| F6 | 同内容重复接受 | 拒 | 409 `DUPLICATE_CHARACTER_CONTENT {existing_revision}` |
| F7 | 身份大幅漂移但 `policy_auto` | 拒 | 422 `IDENTITY_DRIFT_REQUIRES_HUMAN` |
| F8 | 候选已非 PENDING | 拒 | 409 `CANDIDATE_NOT_PENDING {state}` |
| F9 | 角色已 tombstoned，接受 canonical | 拒 | 422 `CHARACTER_TOMBSTONED` |
| F10 | S28 超时 | 不落候选 | 503 `DEPENDENCY_UNAVAILABLE`，retryable |
| F11 | 跨租户 | 404 | `NOT_FOUND` |
| F12 | 两个并发接受同 `expected_current_revision` | 一成功一 409 | 行锁 |
| F13 | 两个并发接受同角色不同候选（不同 expected） | 后者 409（行版本已变） | 行锁 |
| F14 | Outbox 已写未发 | 重启补发 | — |
| F15 | 重复消费 `artifact.archived` 事件 | inbox 去重 | — |

---

## 5. 对外契约

### 5.1 命令

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/characters` | 登记角色（不产生 revision） |
| POST | `/characters/{id}/candidates` | 起草候选 |
| POST | `/characters/{id}/candidates/{cid}:accept` | 接受 → 写 revision |
| POST | `/characters/{id}/candidates/{cid}:reject` | 拒绝 |
| POST | `/characters/{id}:set-canonical` | 显式改 canonical（不产生 revision，仅重指向已有 revision） |
| POST | `/characters/{id}:deprecate` / `:tombstone` / `:merge` | 生命周期 |
| POST | `/characters/{id}/aliases` | 加别名 |

`POST /characters/{id}:set-canonical` 与 `:accept{set_canonical:true}` 的区别：前者用于**回退**（发现第 9 版才是对的，把 canonical 从 12 指回 9）。回退必须留审计（`canonical_pointer` 有完整历史），且必须带 `reason_text`。

### 5.2 查询

见 §4.5。补充：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/characters/{id}/drift?from=&to=` | 两个 revision 的身份漂移对比（供 S17/S33 用） |
| GET | `/characters/{id}/candidates?state=` | 候选列表（仅内部/管理面） |

### 5.3 事件

发布（`drama.character-assets.v2`，partition key `tenant_id:character_id`，`aggregate_sequence = revision`）：

```jsonc
{
  "specversion":"1.0","schema_version":"2.0","id":"evt_...","source":"S11",
  "type":"asset.character.revision.accepted",
  "subject":"character/chr_linlan","time":"2026-10-31T09:00:00Z",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"chr_linlan","aggregate_sequence":12,
  "correlation_id":"cmd_...","causation_id":"cmd_...","traceparent":"00-...",
  "data":{
    "character_ref":{"service_id":"S11","id":"chr_linlan","revision":12,"content_hash":"…"},
    "identity_feature_hash":"…",
    "became_canonical": false,
    "canonical_revision": 9,
    "rights_ref":{...},
    "view_count": 4,
    "source_kind":"generated"
  },
  "data_ref":null
}
```

`became_canonical` 必须显式给出。订阅方（S10 投影、S17 缓存）靠它区分"补图"和"换身份基准"，不能靠比较 `canonical_revision` 猜（那需要额外一次读）。

消费：

| 事件 | 来源 | 处理 |
|---|---|---|
| `artifact.archived` | S28 | 校验引用完整性（仅审计用，不自动拒） |
| `training.lora.registered`（S48） | S48 | 写入 `revision_reference` / 候选的 `lora_refs` 建议列表；**不自动接受** |

**不消费**：S16 的正史事件、S12 的服装事件、S15 的音色事件。角色资产不因剧情/造型/声音变化而变化。哪怕"角色在第 8 集剪短头发"，那也是一次显式的 S11 revision 接受（identity 层），不是被别的服务通知着改。

### 5.4 错误码汇总

| HTTP | code | retryable |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | false |
| 403 | `TENANT_SCOPE_DENIED` | false |
| 404 | `NOT_FOUND` / `CANONICAL_NOT_SET` | false |
| 409 | `VERSION_CONFLICT` / `CANDIDATE_NOT_PENDING` / `DUPLICATE_CHARACTER_CONTENT` / `IDEMPOTENCY_KEY_REUSED` | 前者 true，余 false |
| 422 | `ARTIFACT_NOT_FOUND` / `ARTIFACT_HASH_MISMATCH` / `VIEW_MANIFEST_INCOMPLETE` / `RIGHTS_REF_REQUIRED` / `IDENTITY_DRIFT_REQUIRES_HUMAN` / `CHARACTER_TOMBSTONED` / `AMBIGUOUS_ALIAS` | false |
| 429 | `WRITE_BACKPRESSURE` | true |
| 503 | `DEPENDENCY_UNAVAILABLE` | true |

---

## 6. 幂等与并发

### 6.1 幂等

- 键 `(tenant_id, command_id)`；`request_hash = sha256(JCS(body 去掉 command_id/request_hash))`。
- 同键同 hash → 返回首次结果 + `replayed: true`。
- 同键不同 hash → 409 `IDEMPOTENCY_KEY_REUSED`。
- `POST /characters` 自然幂等：`(tenant_id, series_scope, display_name)` 唯一，重复登记返回既有 ID（但**不**合并别名，别名单独幂等）。

### 6.2 并发

| 竞争 | 机制 |
|---|---|
| 两次接受同 `expected_current_revision` | `character` 行 `FOR UPDATE` + CAS |
| 两次设 canonical | `canonical_one_current` 部分唯一索引 + 行锁 |
| 同内容重复接受 | `cr_hash_uniq` |
| 候选与 TTL 清理并发 | 接受时锁候选行；清理跳过已 ACCEPTED |
| 别名并发写 | 主键冲突 → 视为幂等成功 |

### 6.3 锁顺序

```
1. character (FOR UPDATE)
2. character_candidate (FOR UPDATE)
3. canonical_pointer (UPDATE existing / INSERT new)
4. character_revision (INSERT only)
5. revision_reference (INSERT only)
6. outbox
```

S28 的 artifact 校验在事务外完成。

### 6.4 缓存的唯一允许形态

S17/S26 会在进程内缓存 `GET /characters/{id}/revisions/{rev}`。S11 的接口必须无副作用、可安全缓存，且响应带 `ETag = content_hash`。**S11 自己绝不缓存另一服务的数据**（不做本地镜像表）。

---

## 7. 可观测

指标：

- `s11_candidate_total{result}`、`s11_accept_total{set_canonical}`、`s11_accept_latency_seconds{phase=verify|commit}`
- `s11_identity_drift_total{class}`（`none|minor|major`）
- `s11_canonical_change_total{decision_kind}`
- `s11_view_manifest_incomplete_total{missing_view}`
- `s11_artifact_verify_fail_total{reason}`
- `s11_alias_ambiguity_total`

告警：

- `s11_identity_drift_total{class="major"}` > 0 且 `decision_kind="policy_auto"` 被接受 → **不该发生**，立即查（说明门失效）。
- `s11_canonical_change_total` 在单集制作周期内 > 2 → 身份基准在抖，会污染下游所有已生成镜头。
- `s11_artifact_verify_fail_total{reason="HASH_MISMATCH"}` > 0 → S28 与 S11 的引用不一致。

Runbook：

- `rebuild-canonical-view`：从 `canonical_pointer` 历史重算当前行，与 `character.canonical_revision` 比对。
- `rehash-revision`：逐 revision 重算 `sha256(JCS(payload))` 与 `identity_feature_hash` 一致性检查。
- `drift-audit`：列出所有 `identity_feature_hash` 与 canonical 不同的 revision，确认它们都 `set_canonical=false`。

---

## 8. 测试清单

### E02 · 三集连续

| 用例 | 期望 |
|---|---|
| E02-B1 角色 canonical 在第 1 集前接受，第 1–3 集都用它 | 三集 `resolve` 返回同一 `character_ref`；`is_canonical=true` |
| E02-B2 第 2 集补一张背面图（`set_canonical=false`） | 第 2 集绑定 revision+1；第 1、3 集仍指向旧 revision；三集身份 `identity_feature_hash` 相同 |
| E02-B3 第 3 集之前换身份（新 canonical） | 第 3 集用新 canonical；第 1、2 集不变；`canonical_change_total` 记录 |
| E02-B4 妆造变化不改 identity | S12 换装后 S11 无新 revision；`identity_feature_hash` 不变 |

### E08 · 改前集

| 用例 | 期望 |
|---|---|
| E08-B1 接受新 canonical 后，已提交第 1 集不被改写 | 第 1 集 manifest 的 `character_ref` 不变 |
| E08-B2 回退 canonical（12→9） | 只改指针；无新 revision；审计记录两条 |
| E08-B3 旧 revision 仍可读 | `GET .../revisions/9` 返回相同 hash |

### E12 · 幂等

| E12-B1 同 command 重复接受 | 返回首次结果，无新 revision |
| E12-B2 同键不同 body | 409 |
| E12-B3 重复登记同角色 | 返回既有 ID |

### E28 · 跨租户

| E28-B1 跨租户取 revision | 404，无字段泄漏 |
| E28-B2 同名角色在两租户 | 各自独立，`character_id` 不同 |
| E28-B3 按 `content_hash` 撞查 | 唯一索引含 `tenant_id` |

### E30 · 权利

| E30-B1 肖像权撤销后 S11 仍返回 revision | `rights_recheck_required=true`；S11 不判定 |
| E30-B2 真人类角色无 `rights_ref` | 拒候选 422 |
| E30-B3 权利版本变化 | S11 不自动改；新 revision 需要新 `rights_ref` |

### E24/E25 · QC

| E24-B1 QC 未通过 → 候选 `REJECTED` | 不写 revision |
| E24-B2 多角度一致性失败 | 拒；错误指明哪个视图 |
| E25-B1 修复后重新起草 | 新 `candidate_id`，旧候选保留 |

### E29 · 恶意媒体

| E29-B1 artifact 来自 S28 隔离导入器 | S11 只读 S28 的 `ArtifactManifest`，不直接解析字节 |
| E29-B2 伪造 artifact_id | 422 `ARTIFACT_NOT_FOUND` |

### E01 · 独立升级

| E01-B1 新版本读旧 revision payload | 全部字段可解析（新增字段可选） |
| E01-B2 回滚到上一版本 | 旧代码忽略新字段，不报错 |

### 通用

| 并发接受 100 次 | 1 成功，99 冲突；canonical 唯一 |
| 事务中崩溃 | 无半写；revision 与 outbox 一致 |
| 事件乱序（rev 5 先到） | 按 seq 缓冲；缺口超时回源 |
| 重放 `revision.accepted` | 不重复写引用 |

---

## 9. 反模式

1. ❌ 把最新候选自动设为 canonical。
2. ❌ 在 S11 存"这一集穿什么"（归 S12）。
3. ❌ 在 S11 存"这个角色受伤了"（归 S16）。
4. ❌ 允许原地修改 `character_revision`。
5. ❌ 在 S11 判定权利是否有效。
6. ❌ 缓存 S28 的签名 URL 或自己签发 URL。
7. ❌ 用自由文本描述身份特征（无法比较、无法回归）。
8. ❌ 让 `identity_kind='real_person'` 可以没有 `rights_ref`。
9. ❌ 别名歧义时取第一个（必须报错让人选）。
10. ❌ 在 S11 维护"哪些集用了这个角色"的反向索引（归 S10/S09）。
11. ❌ 让 S11 直接调模型生成（必须经 S26）。
12. ❌ 两个 canonical 同时有效（部分唯一索引已阻止）。

---

## 10. 冲突（报告，不选边）

| # | 冲突 / 缺口 | 现状 | 本文用法 |
|---|---|---|---|
| C11-1 | `service.yaml` 只给了 `POST /characters/{id}/revisions` + `GET /characters/{id}/revisions/{revision}`；没有候选、接受、canonical 的路径 | catalog | 按 §5 拆出 candidate/accept/set-canonical；**未改契约**，需契约方确认 |
| C11-2 | docs/10 只说"accepted canonical"，未区分"revision 已接受"与"candidate 指针" | docs/10 | 本文区分两层（§3.4）；这是表述不足，需设计方确认 |
| C11-3 | `event-topics.v2.json` 只有 `asset.character.revision.accepted`，没有 canonical 变更事件 | topics | 在同一事件里带 `became_canonical`，不新增 topic |
| C11-4 | `service.yaml` 未声明 S11 是否订阅事件；本文只订阅 S28 `artifact.archived` 与 S48 的 LoRA 注册 | catalog | 需要 catalog 显式化（当前 `depends_on: [S28, S01]` 不含 S48） |
| C11-5 | docs/10 说"角色"质量门含"权利引用"，但没说权利对象归谁 | docs/10 + S02 | 本文假设 S02 的 `rights_*` 对象（与 S10 的 `rights_ref` 形态一致） |
| C11-6 | 发型归属在 docs/10 未明确（角色行含"发型"，服装行含"妆造"） | docs/10 | 按 §1.1 判据拆分：影响识别的归 S11，纯造型的归 S12；需设计方确认 |
| C11-7 | docs/01 的 sole-authority 表未列 S11 的 canonical | docs/01 | 按 docs/10 的"内容/canonical 归 S11"实现；建议 docs/01 补行 |

---

## 11. 实施顺序与未决问题

### 11.1 顺序（开关默认关闭）

1. 契约：`contracts/assets/character-revision.v1.schema.json`（含 `view_manifest`、`identity_features` 的 schema）。`identity_features` 必须**先定型**，它是 S33/S34/S17/S48 的共同输入。
2. 平台 SDK：JCS + hash + `rights_ref` 形态校验（与 S10/S12–S15 共用一份）。
3. S11 最小闭合：登记 + 起草候选（mock S28）+ 接受 + 读 revision。先验 E12/E28。
4. 接真实 S28；接 S26 的生成完成事件（走 S33/S34 的 QC 结果）。
5. 接 S48 的 LoRA 注册事件（`lora_refs` 建议列表）。
6. 漂移检测与 `policy_auto` 门（**最后**，因为它最容易放行错东西）。

### 11.2 未决

1. **`identity_features` 的字段集要不要跨类型统一？** S48 的 LoRA 评测也用身份特征。如果 S11 与 S48 各定义一套，就会有两份"身份"定义。建议抽到 `contracts/assets/identity-features.v1`，但需要设计方确认。
2. **跨剧复用角色是否允许？** 当前 `series_scope` 单值。若允许"同一角色在续集复用"，`series_scope` 需要变成数组，且 canonical 是否共享要定。
3. **`three_quarter` 是否必需？** 本文默认非必需（`require_full_turnaround` 可开）。实际拍摄轴线上 3/4 侧很关键，可能应改成必需。
4. **canonical 回退是否需要 S07/S09 影响计划？** 本文允许直接回退指针（不产生 revision）。但回退会让后续集用旧身份 —— 这是不是需要影响分析？需要 docs/10 作者回答。
5. **真人肖像权的 `rights_ref` 失效后，已有 revision 怎么办？** 本文只保证"不判定"，但 S02 会 deny 新调用。历史交付是否要重制，取决于企业保留合同（docs/14 §2）。
6. **`expression_manifest` 的枚举要不要收敛？** 开放集合会导致"每个剧一套表情名"。建议固定一组（neutral/smile/sad/angry/surprised/fear/disgust），但需要与剧本标注（S04/S19）对齐。
