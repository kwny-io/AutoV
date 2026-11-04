# S10 · 资产目录与绑定解析 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码；SLO 与容量均为 docs/03 的规划值，**未实测**。

上游依据：[docs/10](../docs/10-assets.md)（核心）、[docs/01 §3](../docs/01-enterprise-architecture.md)、[docs/04](../docs/04-data-consistency.md)、[docs/06 §2–§4](../docs/06-automation-governance.md)、[docs/07 §1](../docs/07-production-workflows.md)、[docs/14 §2](../docs/14-enterprise-operations.md)、[S10 规格](../docs/design-package/services/S10-asset-catalog.md)、[episode-manifest.v2](../contracts/episode-manifest.v2.schema.json)。

配套：[S11](S11-character-assets.md) / [S12](S12-costume-assets.md) / [S13](S13-scene-assets.md) / [S14](S14-prop-assets.md) / [S15](S15-voice-assets.md)、[S08](S08-revision-snapshot.md)、[S02](S02-governance-policy.md)、[S17](S17-continuity.md)、[S48](S48-adaptation-training.md)。

---

## 0. 一句话边界

**S10 是"哪个资产 ID 属于谁、这一集这一镜用了哪一版、这些引用合起来是什么 hash"的唯一权威。它不是任何资产内容的权威。**

S10 存在的原因只有一个：`episode-manifest.v2` 只接受一个 `binding_set_ref`（`{service_id,id,revision,content_hash}`），而一集要固定五类资产、若干条 LoRA、以及它们各自的权利版本。如果没有一个跨类型的**不可变聚合对象**，S08 就必须在提交事务里去拼五个下游服务，那等于让 S08 持有资产内容 —— 这是越界。S10 把这个聚合做成**内容寻址、只读、可重建**的对象，S08 只 CAS 一个引用。

### 0.1 写什么 / 不写什么

| 事实 | 唯一写者 | S10 的角色 |
|---|---|---|
| 资产全局 ID、类型、owner-service 映射 | **S10** | 唯一写者 |
| 跨类型 `BindingSetRevision` | **S10** | 唯一写者 |
| 资产检索投影（按名称/别名/标签/使用集） | **S10** | 可重建投影，不是事实 |
| 绑定 → 已提交 manifest 的使用关系 | **S10 的投影** | 消费 S08 事件维护，可重建 |
| 角色外观、多视图、身份特征 | S11 | 只持有 `{id,revision,hash}` |
| 服装/妆造组合、破损湿度变体 | S12 | 同上 |
| 场景平面图、anchor、光照变体 | S13 | 同上 |
| 道具外观、状态变体 | S14 | 同上 |
| 音色、供应商映射、克隆资格、授权撤销 | S15 | 同上 |
| LoRA 权重、基座兼容、评测 | S48 | 同上 |
| 媒体 bytes、artifact hash | S28 | 只存 ArtifactManifest 引用 |
| **权利是否仍然有效** | **S02（政策）/ S15（音色资格）** | 只存 `rights_revision` 指针，**不做判定** |
| 道具当前持有者、角色伤势、知情范围 | **S16** | 完全不涉及 |

判据：

- 任何"这个 ID 是什么资产"的问题，S10 回答。
- 任何"这个资产长什么样、哪一版是 canonical"的问题，回到 S11–S15。
- 任何"这个资产现在还能不能用（权利/授权）"的问题，回到 S02/S15 的**实时**接口。
- 任何"这一集的钥匙归谁"的问题，回到 S16。

### 0.2 四个明确不做

1. **不做权利判定。** `resolve` 成功不等于权利有效（docs/10 原句）。S10 返回 `rights_revision` 指针和 `requires_rights_recheck: true`，调用方（S02/S26 的准入 Saga）必须在 dispatch 前实时复查。S10 一旦缓存"已授权"状态，就成了第二权利权威。
2. **不做影响闭包。** `usage` 投影只回答"哪些已提交 manifest 引用过这个 binding revision"，是**种子**，不是闭包。闭包归 S09（见 [S16 §5.1](S16-story-memory.md) 的同一分工）。
3. **不持有 canonical 指针。** canonical 在 S11–S15 各自的库里。S10 只知道"某个 revision 存在并且被绑定"，不知道它是不是 canonical。
4. **不复制内容。** 不存角色描述文本、不存参考图 URL、不存音色样例。存 URL 就等于存了一份过期后会骗人的副本。

---

## 1. 关键区分（最容易写错的三处）

### 1.1 `BindingSet` 与 `BindingSetRevision`

- `BindingSet` 是**稳定身份**：一个"这一集/这一场的资产组合"的容器，跨 revision 不变。
- `BindingSetRevision` 是**不可变值**：封存后永不修改，`content_hash` 内容寻址。

为什么必须分开：集在制作过程中会换服装、换道具版本、补一条 LoRA。如果每次换都生成新的 BindingSet，那么"这一集的资产组合"就没有稳定身份，S47 的资产页和 S09 的依赖边都无法挂靠。如果允许原地改 revision，那么已提交 manifest 引用的 hash 就失去意义。

**封存后修改的唯一方式是追加 revision。** 这跟 S16 的 claim 稳定 ID + 不可变 revision 是同一个模式，不是巧合 —— 两者都是"历史证据不能被后来的编辑抹掉"。

### 1.2 绑定条目（entry）与解析结果（resolution）

| | entry | resolution |
|---|---|---|
| 存在哪 | `binding_entry`（overlay 层，按 scope 存覆盖） | `binding_expansion`（展开后的有效条目，按 revision 存） |
| 什么时候产生 | 用户/S19 显式写入，或从 scope 默认继承 | **创建 revision 时一次性展开** |
| 是否可变 | 只在自己的 revision 内不变 | 随 revision 不可变 |
| 谁读 | 写路径 | 读路径（`resolve`） |

没有"运行时展开"。docs/10 说"在创建快照时展开" —— 这句话是硬约束。运行时展开意味着全剧默认值一改，所有在跑的集都变，直接把"运行中的快照不受全剧默认更新影响"违反掉。

### 1.3 三类 scope 与继承方向

```
shot    (最具体, specificity = 4)
  ↑ 覆盖
scene   (3)
  ↑ 覆盖
episode (2)
  ↑ 覆盖
series  (1, 全剧默认)
```

同一个 `role_key` 在多层出现时，**最具体的一层胜出**。同一层出现两次是数据错误，唯一约束直接拒绝（`UNIQUE(revision_id, scope_kind, scope_id, role_key)`），不做"后者覆盖前者"。

`role_key` 的规范化（必须跨服务逐字节一致）：

```
role_key = "<asset_type>:<owner_local_id>"
例：character:chr_linlan  costume:cos_red_dress_01  scene:loc_office_3f
    prop:prop_key  voice:voice_linlan  adaptation:lora_linlan_v3
```

**`role_key` 用 owner 的本地 ID，不用 GlobalAssetID。** 原因：GlobalAssetID 是 S10 铸造的，但一个角色可以在不同剧里被"重新铸造"以避免跨剧串味；绑定列表在写的时候拿到的往往是 owner 给的本地 ID。两者在 `asset_identity` 里映射，但绑定层以 owner 本地 ID 为准 —— 这样即使 S10 的 ID 映射迁移过，历史 binding revision 依然可解析。

---

## 2. 数据模型

Postgres，schema `asset_catalog`。所有表带 `tenant_id`，所有主键/唯一约束含 `tenant_id`（防跨租户 hash 存在性泄漏，E28）。启用 RLS 作为第二道保护（docs/14 §2）。

### 2.1 类型注册表（代码发布，不可租户自造）

```sql
CREATE TABLE asset_catalog.asset_type_registry (
  asset_type      text PRIMARY KEY,          -- character|costume|scene|prop|voice|adaptation
  owner_service   text NOT NULL,             -- S11|S12|S13|S14|S15|S48
  id_prefix       text NOT NULL,             -- chr_|cos_|scn_|prp_|vox_|adp_（本地 ID 前缀）
  required_for    text[] NOT NULL,           -- {dialogue_shot} / {all_shots} / {series}
  max_active_per_role integer NOT NULL        -- 通常 1；见 §2.4
);
```

这是**随服务发布版本化的常量表**，不是租户可写表。新增资产类型必须发版并改 `contracts` —— 不能让某个租户"注册"一个新的资产类型，那会让 S08 manifest 的绑定语义失控。

### 2.2 全局资产身份

```sql
CREATE TABLE asset_catalog.asset_identity (
  tenant_id       text NOT NULL,
  asset_id        text NOT NULL,             -- 'ast_01J...'（ULID，永不复用）
  asset_type      text NOT NULL REFERENCES asset_catalog.asset_type_registry(asset_type),
  owner_service   text NOT NULL,
  owner_local_id  text NOT NULL,             -- S11 的 chr_xxx
  series_scope    text NOT NULL,             -- series_id 或 '*'（可跨剧复用，如通用道具）
  status          text NOT NULL,             -- active|deprecated|merged_into|tombstoned
  merged_into     text,                      -- status=merged_into 时的目标 asset_id
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, asset_id),
  UNIQUE (tenant_id, owner_service, owner_local_id),
  CONSTRAINT merged_has_target CHECK ((status = 'merged_into') = (merged_into IS NOT NULL))
);
CREATE INDEX asset_identity_lookup
  ON asset_catalog.asset_identity (tenant_id, owner_service, owner_local_id);
CREATE INDEX asset_identity_series
  ON asset_catalog.asset_identity (tenant_id, series_scope, asset_type, status);
```

`status` 与内容侧的 canonical 状态**不是一回事**。这里是"这个 ID 还存不存在、有没有被合并"，不是"这一版接不接受"。`tombstoned` 表示禁止新绑定；历史 binding revision 保留（见 §2.7）。

```sql
CREATE TABLE asset_catalog.asset_alias (
  tenant_id   text NOT NULL,
  asset_id    text NOT NULL,
  alias_text  text NOT NULL,                 -- '林岚' / 'linlan' / '岚姐'
  alias_kind  text NOT NULL,                 -- display|legacy_id|import_alias
  source      text NOT NULL,                 -- S03|S11|import_batch_id
  PRIMARY KEY (tenant_id, asset_id, alias_text, alias_kind)
);
CREATE INDEX asset_alias_lookup ON asset_catalog.asset_alias (tenant_id, alias_text);
```

别名只用于**检索与导入对账**，不用于绑定解析。绑定解析必须走 ID，别名解析歧义（"小琳"可能是两个角色）会让 manifest 引用到错误的人。

### 2.3 BindingSet 与不可变 revision

```sql
CREATE TABLE asset_catalog.binding_set (
  tenant_id        text NOT NULL,
  binding_set_id   text NOT NULL,            -- 'bs_01J...'，稳定
  project_id       text NOT NULL,
  series_id        text NOT NULL,
  episode_id       text NOT NULL,
  branch_id        text NOT NULL,
  head_revision    integer NOT NULL DEFAULT 0,   -- 0 = 尚未封存
  current_revision integer,                      -- 最近一次 SEALED 的 revision
  created_by       jsonb NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, binding_set_id),
  UNIQUE (tenant_id, series_id, episode_id, branch_id),
  CONSTRAINT head_not_ahead CHECK (current_revision IS NULL OR current_revision <= head_revision)
);
CREATE INDEX binding_set_by_episode
  ON asset_catalog.binding_set (tenant_id, series_id, episode_id, branch_id);
```

一个 `(series, episode, branch)` 只有一个 BindingSet。分支派生时新分支建新 BindingSet，父分支的那份不动 —— 与 S08 manifest 的分支语义对齐。

```sql
CREATE TABLE asset_catalog.binding_set_revision (
  tenant_id           text NOT NULL,
  binding_set_id      text NOT NULL,
  revision            integer NOT NULL CHECK (revision >= 1),
  state               text NOT NULL CHECK (state IN ('SEALED','SUPERSEDED')),
  content_hash        char(64) NOT NULL,     -- sha256(JCS(payload))
  payload             jsonb NOT NULL,        -- 完整 binding-set.v1（含展开结果）
  entry_count         integer NOT NULL,
  expanded_from       jsonb NOT NULL,        -- {series_default_rev, episode_override_rev, scene_count, shot_count}
  rights_pointer_set  jsonb NOT NULL,        -- 去重后的 [{rights_owner,rights_id,rights_revision,rights_hash}]
  base_revision       integer,               -- 本次封存的 expected head
  sealed_by           jsonb NOT NULL,
  sealed_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, binding_set_id, revision),
  CONSTRAINT bsr_hash_len CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT bsr_base_lt CHECK (base_revision IS NULL OR base_revision < revision)
);
CREATE UNIQUE INDEX bsr_hash_uniq
  ON asset_catalog.binding_set_revision (tenant_id, content_hash);
CREATE INDEX bsr_state_idx
  ON asset_catalog.binding_set_revision (tenant_id, binding_set_id, state, revision DESC);
```

`bsr_hash_uniq` 是"同一内容不能有两个 revision"的物理保证。重复封存同一份内容（网络重投、两个前端同时点保存）会在数据库层撞唯一约束，返回 `DUPLICATE_BINDING_CONTENT` 而不是造出两个 hash 相同、revision 不同的对象 —— 后者会破坏 `content_hash` 作为 manifest 溯源键的作用。

**没有 `state='DRAFT'`。** 草稿在 `binding_entry` 层表达（head 未封存时 entry 可直接改），一旦封存就只进 `binding_set_revision`。这与 S08 "候选 manifest 提交前根本不在本库"是同一个理由：草稿不需要被 GC，因为它从未成为事实。

### 2.4 绑定条目（overlay）与展开

```sql
CREATE TABLE asset_catalog.binding_entry (
  tenant_id      text NOT NULL,
  binding_set_id text NOT NULL,
  revision_floor integer NOT NULL,           -- 该 entry 从哪个 revision 起有效（= 写入时的 head+1）
  scope_kind     text NOT NULL CHECK (scope_kind IN ('series','episode','scene','shot')),
  scope_id       text NOT NULL,              -- series_id / episode_id / scene_id / shot_id
  role_key       text NOT NULL,              -- 见 §1.3 规范化
  asset_type     text NOT NULL,
  owner_service  text NOT NULL,
  owner_local_id text NOT NULL,
  owner_revision integer NOT NULL CHECK (owner_revision >= 1),
  owner_hash     char(64) NOT NULL,
  rights_ref     jsonb NOT NULL,             -- {rights_owner,rights_id,rights_revision,rights_hash}
  variant_selector jsonb NOT NULL DEFAULT '{}', -- {state:'torn', light:'night', angle:'front'} 由 owner 解释
  required       boolean NOT NULL DEFAULT true,
  note           text,
  PRIMARY KEY (tenant_id, binding_set_id, revision_floor, scope_kind, scope_id, role_key)
);
CREATE INDEX binding_entry_scope
  ON asset_catalog.binding_entry (tenant_id, binding_set_id, scope_kind, scope_id);

CREATE TABLE asset_catalog.binding_expansion (
  tenant_id       text NOT NULL,
  binding_set_id  text NOT NULL,
  revision        integer NOT NULL,
  scope_kind      text NOT NULL,
  scope_id        text NOT NULL,
  role_key        text NOT NULL,
  owner_service   text NOT NULL,
  owner_local_id  text NOT NULL,
  owner_revision  integer NOT NULL,
  owner_hash      char(64) NOT NULL,
  rights_ref      jsonb NOT NULL,
  variant_selector jsonb NOT NULL,
  resolved_from   text NOT NULL,             -- shot|scene|episode|series（哪一层胜出）
  PRIMARY KEY (tenant_id, binding_set_id, revision, scope_kind, scope_id, role_key)
);
```

`binding_expansion` 是**可重建**的派生表（`rebuild-expansion` runbook 可以从 entry + 默认值重算）。但它同时被写进 `binding_set_revision.payload`，所以即使派生表丢了，历史 revision 仍能完整回答"当时这一镜绑了谁"。

**`variant_selector` 为什么由 owner 解释：** S10 不知道 S13 的光照变体叫什么名字，也不应该知道。S10 只保证"这个选择器被原样冻结并写进 hash"，语义由 S13 在 `resolve` 之后的读取里解释。S10 解析变体名字的那一刻，就把五类资产的领域知识搬进了目录服务。

### 2.5 scope 默认值（全剧默认从这里来）

```sql
CREATE TABLE asset_catalog.binding_default (
  tenant_id     text NOT NULL,
  series_id     text NOT NULL,
  scope_kind    text NOT NULL CHECK (scope_kind IN ('series','episode','scene')),
  scope_id      text NOT NULL,
  role_key      text NOT NULL,
  asset_type    text NOT NULL,
  owner_service text NOT NULL,
  owner_local_id text NOT NULL,
  owner_revision integer NOT NULL,
  owner_hash    char(64) NOT NULL,
  rights_ref    jsonb NOT NULL,
  variant_selector jsonb NOT NULL DEFAULT '{}',
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, series_id, scope_kind, scope_id, role_key)
);
```

默认值表刻意**不含 shot 层**：镜头级的绑定必须显式写，不能靠默认漂移出来。一个镜头要不要换装，是创作决定，不该被"全剧默认"顺带改掉。

默认值更新只影响**下一次封存**。已封存的 revision 不重算 —— 这是 docs/10 "运行中的快照不受全剧默认更新影响"在表结构层面的落实。

### 2.6 使用关系投影（可重建，非权威）

```sql
CREATE TABLE asset_catalog.binding_usage (
  tenant_id       text NOT NULL,
  binding_set_id  text NOT NULL,
  revision        integer NOT NULL,
  manifest_id     text NOT NULL,
  manifest_revision integer NOT NULL,
  project_id      text NOT NULL,
  series_id       text NOT NULL,
  episode_id      text NOT NULL,
  branch_id       text NOT NULL,
  first_seen_at   timestamptz NOT NULL,
  source_event_id text NOT NULL,
  PRIMARY KEY (tenant_id, binding_set_id, revision, manifest_id, manifest_revision)
);
CREATE INDEX binding_usage_by_owner
  ON asset_catalog.binding_usage (tenant_id, series_id, episode_id);
```

由 `snapshot.manifest.committed` 事件驱动，`inbox` 去重。这张表回答的是"**已经提交**的 manifest 用过哪些 binding revision"，是 S07/S09 做影响分析时的种子。

它**不是**权威：真正的权威是 S08 的 `manifest_revision.payload.binding_set_ref`。所以：

- 投影落后 → 返回 `projection_watermark` 与 `stale: true`，不假装完整。
- 投影与回源不一致 → 以 S08 为准，本表重建。

### 2.7 生命周期与 tombstone

```sql
CREATE TABLE asset_catalog.asset_lifecycle_event (
  tenant_id     text NOT NULL,
  asset_id      text NOT NULL,
  seq           bigint NOT NULL,
  event_kind    text NOT NULL,     -- registered|deprecated|merged|tombstoned|un_tombstoned
  reason_code   text NOT NULL,
  reason_text   text,
  actor         jsonb NOT NULL,
  occurred_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, asset_id, seq)
);
```

`tombstoned` 后：

- **禁止新绑定**（封存时校验）。
- 历史 binding revision **保留可读**，因为已交付集要能解释"当时用的是哪一版"（docs/04 §6：先 tombstone 禁用新调用，再按引用编排删除）。
- 不删除 `asset_identity` 行。ID 永不复用 —— ID 复用会让历史 manifest 指向一个语义完全不同的东西。

### 2.8 基础设施表

```sql
CREATE TABLE asset_catalog.idempotency_record (
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

CREATE TABLE asset_catalog.outbox (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,
  aggregate_sequence bigint NOT NULL,
  event_type         text NOT NULL,
  partition_key      text NOT NULL,
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished ON asset_catalog.outbox (id) WHERE published_at IS NULL;

CREATE TABLE asset_catalog.inbox (
  consumer    text NOT NULL,       -- 's10.manifest-usage'
  event_id    text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

### 2.9 不建的表

- **不建 `asset_content_cache`。** 缓存 canonical 描述 = 第二份内容权威。
- **不建 `rights_status`。** 不存"当前是否已授权"，只存 `rights_ref` 指针。存了就得失效，失效就会错。
- **不建 `asset_canonical_pointer`。** canonical 在 S11–S15。S10 存了就无法解释"为什么两个服务都说自己是权威"。
- **不建 `binding_set_working_copy`。** 草稿就是 `binding_entry` 的当前 head，不需要第二张表。

---

## 3. 状态机

### 3.1 BindingSet（容器）

```
(无) ──create──► OPEN(head=0) ──seal──► OPEN(head=1,current=1) ──seal──► OPEN(head=2,current=2) ...
                     │
                     └──abandon──► ABANDONED（无任何 SEALED revision 时可弃）
```

```
OPEN ──(episode 归档且无引用)──► ARCHIVED（只读，不再接受新 revision）
```

`OPEN → ARCHIVED` 需要 S08 确认该集没有未完成分支。归档后 `POST /revisions` 返回 409 `BINDING_SET_ARCHIVED`。

### 3.2 BindingSetRevision

```
SEALED ──新 revision 封存──► SUPERSEDED
```

`SUPERSEDED` 只是"有更新的版本了"，**不是作废**。已提交 manifest 引用旧 revision 完全合法，`resolve` 依然返回它。作废只有一种：manifest 被 S08 的 `execution_fence` 置 `VOIDED` —— 那是 manifest 的事，不回溯到 binding revision。

**不存在 `REVOKED` 状态。** 撤销是权利侧的概念（S02/S15），binding revision 记录的是"当时绑了什么"，它不需要也不应该被撤销。要停止使用某个资产，做法是**封存一个不含它（或换成新版本）的新 revision**，并走 S07/S09 的迁移计划；而不是把历史改成"从未绑过"。

### 3.3 AssetIdentity

```
active ──deprecate──► deprecated ──tombstone──► tombstoned
   │                      │
   └──merge──► merged_into（指向目标）
```

| 从 | 到 | 触发 | 约束 |
|---|---|---|---|
| （无） | active | `POST /assets` | ID 全局唯一，不复用 |
| active | deprecated | 人工/S07 | 仍可被已有 revision 引用，新绑定告警 |
| active/deprecated | merged_into | 去重 | 目标必须 active；历史 binding 不改写 |
| deprecated | tombstoned | 授权撤销 / 数据删除请求 | 禁止新绑定；历史可读 |
| tombstoned | active | **不允许** | 复活要走新 ID + 别名，保留可解释性 |

`merged_into` 时，S10 的 `resolve` 对旧 `asset_id` 返回 `301` 语义的 `merged_to` 字段，但**不自动改 manifest 引用**。自动改写会让已提交 manifest 的 `content_hash` 与内容不一致。

---

## 4. 关键流程

### 4.1 铸造全局 ID

```
POST /assets
{command_id, asset_type, owner_service, owner_local_id, series_scope, alias_texts[]}
```

1. 校验 `asset_type` 在 registry 且 `owner_service` 匹配（不允许 S10 给 S14 的道具盖上 S13 的类型）。
2. `INSERT ... ON CONFLICT (tenant_id, owner_service, owner_local_id) DO NOTHING RETURNING`。
3. 冲突 → 回读返回既有 `asset_id`（幂等：同一 owner 对象重复注册返回同一 ID，不报错）。
4. 同事务写 Outbox（`asset.identity.registered`，本地事件，非契约必需，可用于下游索引）。

跨租户：另一个租户有同名 `owner_local_id` 不影响 —— 唯一约束含 `tenant_id`。**这也意味着 S10 不能跨租户做任何存在性查询**（E28）。

### 4.2 写 entry（草稿）

```
POST /binding-sets/{id}/entries
{command_id, scope_kind, scope_id, role_key, owner_revision, owner_hash, rights_ref,
 variant_selector, expected_head}
```

- 在 `binding_set` 行上加 `FOR UPDATE`，校验 `head_revision == expected_head`。
- 写 `binding_entry`（`revision_floor = head + 1`）。
- 同一 `(scope, scope_id, role_key)` 在**同一 revision_floor** 内唯一；重复写返回 409 `ENTRY_ALREADY_SET`，不静默覆盖（覆盖会让"谁最后写的"变成不可追溯）。
- 不校验 owner revision 是否存在（草稿阶段允许先搭骨架），但记录 `unverified: true`，封存时必须补齐验证。

### 4.3 封存 revision（核心路径）

```
POST /binding-sets/{id}/revisions
{command_id, expected_head, verify_owners: true}
```

事务内顺序固定：

```
BEGIN;
-- 1) 锁容器
SELECT head_revision, current_revision FROM binding_set
 WHERE tenant_id=$t AND binding_set_id=$b FOR UPDATE;
-- expected_head 不符 → ROLLBACK 409 VERSION_CONFLICT

-- 2) 计算展开结果
--    对每个 (scope, role_key)：
--      level = max(specificity) among entries/defaults applicable to that scope
--    同 level 多源 → 已由唯一约束阻止；此处断言再验一次
--    生成 binding_expansion 行（内存中先算，再批量插）

-- 3) 校验每个 owner 引用（verify_owners=true 时）
--    对每个 distinct (owner_service, owner_local_id, owner_revision)：
--      GET owner: revision 存在 且 content_hash 一致 且 未 tombstoned
--    任一失败 → ROLLBACK 422 OWNER_REF_INVALID {service,id,revision,reason}
--    owner 不可达（超时/5xx）→ 503 DEPENDENCY_UNAVAILABLE（不封存）

-- 4) 计算 content_hash = sha256(JCS(binding-set payload))
--    唯一索引冲突 → 409 DUPLICATE_BINDING_CONTENT

-- 5) INSERT binding_set_revision (revision = head+1, state='SEALED')
-- 6) UPDATE 旧 revision SET state='SUPERSEDED'
-- 7) 批量 INSERT binding_expansion
-- 8) UPDATE binding_set SET head_revision=head+1, current_revision=head+1
-- 9) INSERT outbox (asset.binding-set.created, seq = revision)
COMMIT;
```

**为什么 owner 不可达时不封存：** 一条绑定指向不存在的 revision，会在 S08 提交时才被发现，那时已经花掉了整条准备流水线的钱。宁可封存失败、用户重试，也不产出"看起来封好了、其实悬空"的 binding set。这与 docs/10 的 `recovery`："缺必要版本停止新生成"一致。

**`verify_owners` 允许 false 的唯一场景：** 离线导入/灾备重建时，owner 服务整体不可用但历史事实需要恢复。此时必须带 `import_mode=true` 与授权，并在 `binding_set_revision.expanded_from` 中标记 `owner_verification: 'deferred'`。`resolve` 对这类 revision 返回 `verification: 'deferred'`，S02 的 `canonical_asset` gate 会因此判 `abstain`（不是 pass）。

### 4.4 `resolve`（读路径）

```
GET /assets/{id}/resolve?series_id=&episode_id=&branch_id=&scene_id=&shot_id=
                             &binding_set_revision=&variant_role=
```

两种用法必须区分清楚：

**(a) 按 binding revision 精确读取**（生产路径）：给 `binding_set_revision`，返回该 revision 中匹配 scope 的展开条目。

**(b) 按位置预览**（UI/预检）：不给 revision，S10 用当前 `current_revision` 并**显式标注** `is_preview: true, preview_basis: 'current_revision'`。预览结果永远不能直接进 manifest。

响应：

```jsonc
{
  "binding_set_ref": {"service_id":"S10","id":"bs_01J...","revision":7,"content_hash":"…"},
  "resolution_scope": {"episode_id":"ep3","scene_id":"sc2","shot_id":"sh2_1"},
  "entries": [
    {"role_key":"character:chr_linlan",
     "owner_ref":{"service_id":"S11","id":"chr_linlan","revision":12,"content_hash":"…"},
     "variant_selector":{"state":"default"},
     "rights_ref":{"rights_owner":"S02","rights_id":"rights_char_linlan","rights_revision":3,"rights_hash":"…"},
     "resolved_from":"episode"}
  ],
  "unresolved_required_roles": ["voice:voice_chenmo"],
  "rights_recheck_required": true,
  "source_revision": 7,
  "projection_watermark": "2026-10-31T09:00:00Z",
  "verification": "verified"
}
```

语义硬约束：

1. **`rights_recheck_required` 恒为 true**，没有开关。S10 永远不承诺权利有效。
2. **`unresolved_required_roles` 必须返回。** 只返回解析到的条目、不报告缺什么，调用方会把"缺音色"当成"这个镜头不需要音色"，然后交给模型脑补。这是 `existence_checks` 的同一思路（[S16 §6](S16-story-memory.md)）。
3. `source_revision` 是读到的 binding revision；`projection_watermark` 是 `binding_usage` 投影水位（与本次解析无关，但调用方需要区分"绑定是权威的、使用关系是投影的"）。
4. 跨租户 / 不存在的 `binding_set_id` → **404**，同码，不泄漏存在性（E28）。

### 4.5 资产升级 → 影响清单（种子，不是闭包）

```
GET /binding-sets/{id}/revisions/{rev}/usage
→ [{manifest_id, manifest_revision, episode_id, episode_order, branch_id}]
```

S07/S09 拿到这个清单后自行求闭包。S10 **不**回答"这些集要不要重做"，也**不**发"资产升级"事件给 S21。

为什么这条必须写死：如果 S10 在 owner revision 被接受时发一个"请重做这些镜头"的事件，S10 就变成了第二个影响分析权威（AGENTS.md 明令禁止）。S09 的职责是沿 `claim → context_pack → script → shot → artifact` 求闭包（见 [S16 §5.1](S16-story-memory.md)）。

### 4.6 失败矩阵

| # | 场景 | S10 行为 | 错误码 / 语义 |
|---|---|---|---|
| F1 | owner 服务超时 | 不封存 | 503 `DEPENDENCY_UNAVAILABLE`，retryable |
| F2 | owner 返回 404（revision 不存在） | 不封存 | 422 `OWNER_REF_INVALID {reason:"REVISION_NOT_FOUND"}` |
| F3 | owner 的 `content_hash` 与提交的不一致 | 不封存 | 422 `OWNER_REF_INVALID {reason:"HASH_MISMATCH"}` |
| F4 | owner 资产 `tombstoned` | 不封存 | 422 `OWNER_REF_INVALID {reason:"TOMBSTONED"}` |
| F5 | 同 `(scope,role_key)` 重复写 | 拒绝 | 409 `ENTRY_ALREADY_SET` |
| F6 | 封存时 `expected_head` 落后 | 拒绝 | 409 `VERSION_CONFLICT {head}` |
| F7 | 内容与既有 revision 完全相同 | 拒绝 | 409 `DUPLICATE_BINDING_CONTENT {existing_revision}` |
| F8 | 必需 role 在展开后仍缺失 | **允许封存**，但 `resolve` 报 `unresolved_required_roles` | 见下 |
| F9 | shot scope 的 scene_id 不属于本 episode | 拒绝 | 422 `SCOPE_INCONSISTENT` |
| F10 | 跨租户访问 | 404 | `NOT_FOUND`（不区分） |
| F11 | BindingSet 已 ARCHIVED | 拒绝新 revision | 409 `BINDING_SET_ARCHIVED` |
| F12 | 并发两次封存同 `expected_head` | 一个成功，一个 409 | 行锁保证无部分写 |
| F13 | Outbox 已写、发布前崩溃 | 重启后 publisher 补发 | 事实与事件一致（E13 同构） |
| F14 | 重复消费同一 `snapshot.manifest.committed` | inbox 去重 | `binding_usage` 不新增 |

**F8 为什么允许封存：** 一集可能在早期就封存骨架（角色+场景齐了，道具还没准备好），随后由 S11–S15 的准备分支补齐。硬拒会让"先定结构再补资产"的正常流程走不通。但 `resolve` 必须如实报告缺口，S02 的 `canonical_asset` gate 会据此判 `require_human` 或 `deny` —— 缺口由策略处理，不由 S10 擅自补全。

---

## 5. 对外契约

### 5.1 命令

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/assets` | 铸造全局 ID |
| POST | `/assets/{id}:deprecate` / `:tombstone` / `:merge` | 生命周期 |
| POST | `/binding-sets` | 创建容器（幂等，同 `(series,episode,branch)` 返回既有） |
| POST | `/binding-sets/{id}/entries` | 写草稿 entry |
| DELETE | `/binding-sets/{id}/entries/{scope_kind}/{scope_id}/{role_key}` | 删草稿 entry（仅 head 未封存） |
| POST | `/binding-sets/{id}/revisions` | **封存** |
| POST | `/binding-sets/{id}:archive` | 归档 |

### 5.2 查询

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/assets/{id}` | 身份与生命周期（不含内容） |
| GET | `/assets/{id}/resolve` | **绑定解析（主读接口）** |
| GET | `/assets:lookup?owner_service=&owner_local_id=` | 本地 ID → 全局 ID |
| GET | `/binding-sets/{id}` | 容器头 |
| GET | `/binding-sets/{id}/revisions/{rev}` | 单版本全文 |
| GET | `/binding-sets/{id}/revisions/{rev}/usage` | 使用关系（投影） |
| GET | `/binding-sets?series_id=&episode_id=&branch_id=` | 列表 |

### 5.3 事件

发布（`drama.asset-catalog.v2`，partition key `tenant_id:binding_set_id`，`aggregate_sequence = revision`）：

```jsonc
{
  "specversion":"1.0","schema_version":"2.0",
  "id":"evt_...","source":"S10","type":"asset.binding-set.created",
  "subject":"binding-set/bs_01J...","time":"2026-10-31T09:00:00Z",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"bs_01J...","aggregate_sequence":7,
  "correlation_id":"cmd_...","causation_id":"cmd_...","traceparent":"00-...-01",
  "data":{
    "binding_set_ref":{"service_id":"S10","id":"bs_01J...","revision":7,"content_hash":"…"},
    "series_id":"series_demo","episode_id":"ep3","branch_id":"canon",
    "entry_count":6,
    "owner_service_counts":{"S11":2,"S12":1,"S13":1,"S14":1,"S15":1},
    "rights_revision_set_hash":"…",        // 权利指针集合的 hash，便于 S02 快速比对
    "superseded_revision":6
  },
  "data_ref":null
}
```

消费：

| 事件 | 来源 | 处理 | 失败 |
|---|---|---|---|
| `snapshot.manifest.committed` | S08 | upsert `binding_usage`（按 `binding_set_ref`） | inbox 去重；缺口缓冲 + 回源 S08 |
| `asset.character.revision.accepted` 等 | S11–S15 | **不消费内容**，只刷新检索投影的 `owner_revision` 引用 | 重试；失败不影响解析 |

**S10 不订阅权利撤销事件。** 订阅了就要在自己库里记"已撤销"，那就成了第二权利权威。撤销只在 dispatch 前由 S02 实时查。

### 5.4 错误码

| HTTP | code | retryable | 场景 |
|---|---|---|---|
| 400 | `MALFORMED_REQUEST` | false | JCS/schema 失败 |
| 403 | `TENANT_SCOPE_DENIED` | false | header 与认证不一致 |
| 404 | `NOT_FOUND` | false | 含跨租户 |
| 409 | `VERSION_CONFLICT` | true | `expected_head` 不符 |
| 409 | `ENTRY_ALREADY_SET` | false | 同 scope 同 role 重复 |
| 409 | `DUPLICATE_BINDING_CONTENT` | false | 同 hash 已存在 |
| 409 | `BINDING_SET_ARCHIVED` | false | 归档后写 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同键不同 body |
| 422 | `OWNER_REF_INVALID` | false | owner revision/hash/tombstone |
| 422 | `SCOPE_INCONSISTENT` | false | shot 不属于该 scene/episode |
| 422 | `TYPE_OWNER_MISMATCH` | false | asset_type 与 owner_service 不匹配 |
| 422 | `ASSET_TOMBSTONED` | false | 新绑定指向 tombstoned |
| 429 | `WRITE_BACKPRESSURE` | true | 封存队列超限 |
| 503 | `DEPENDENCY_UNAVAILABLE` | true | owner 不可达 |

---

## 6. 幂等与并发规则

### 6.1 幂等

- 键：`(tenant_id, command_id)`，`request_hash = sha256(JCS(body 去掉 command_id/request_hash))`。
- 同键同 hash → 原样返回首次结果，`replayed: true`。
- 同键不同 hash → 409 `IDEMPOTENCY_KEY_REUSED`，不覆盖。
- `POST /assets` 另有自然幂等：`(owner_service, owner_local_id)` 唯一，重复注册返回既有 ID。

### 6.2 并发

| 竞争 | 机制 |
|---|---|
| 两次封存同 `expected_head` | `binding_set` 行 `FOR UPDATE` + CAS |
| 两个 entry 写同 scope/role | 唯一约束 `(…, scope_kind, scope_id, role_key)` |
| 两个 revision 算出同 hash | `bsr_hash_uniq` 唯一索引 |
| 封存与归档并发 | 归档要求 `current_revision` 稳定；封存持行锁，归档等待 |
| 使用投影并发 upsert | `ON CONFLICT DO NOTHING` + inbox 去重 |

### 6.3 锁顺序（防死锁，必须写进代码）

```
1. binding_set（容器行，FOR UPDATE）
2. binding_set_revision（仅读）
3. binding_entry（仅插入，不更新）
4. binding_expansion（仅插入）
5. outbox
```

跨服务调用（owner 校验）**在事务外先做**，拿到全部结果后再开事务写。把 HTTP 调用放在持有行锁的事务里，会在 owner 慢时把整个集的封存串行化。

### 6.4 隔离级别

`READ COMMITTED` + 显式行锁足够。原因：所有写路径都先在 `binding_set` 行上串行化，revision 内容由 CAS + 唯一索引双重保护。使用率投影是 `ON CONFLICT` 幂等写，不需要可串行化。

---

## 7. 可观测

指标：

- `s10_seal_total{result}`、`s10_seal_latency_seconds{phase=expand|verify|commit}`
- `s10_resolve_total{mode=by_revision|preview,result}`、`s10_resolve_latency_seconds`
- `s10_unresolved_required_role_total{role_key}`
- `s10_owner_verify_fail_total{service,reason}`
- `s10_idempotency_conflict_total`、`s10_usage_projection_lag_seconds`

告警：

- `s10_owner_verify_fail_total{reason="HASH_MISMATCH"}` > 0 → 上游有服务在自报 revision 但内容对不上，立即查（这是数据完整性事件，不是普通错误）。
- `s10_usage_projection_lag_seconds` > 300 → 影响种子缺失，S07/S09 影响分析不完整。
- `s10_seal_total{result="deferred_verification"}` 出现 → 有人在用 import_mode，须核对授权。

Runbook：

- `rebuild-expansion`：从 `binding_entry` + `binding_default` 重建 `binding_expansion`，与 `binding_set_revision.payload` 对比，不一致即数据损坏告警。
- `rebuild-usage`：从 S08 回放 manifest 事件重建 `binding_usage`。
- `verify-hash`：对每个 revision 重算 `sha256(JCS(payload))` 与存储值比对。
- `tombstone-stuck`：资产已被 tombstone 但仍有活跃 binding 引用时的处理路径（只能新封存，不能改历史）。

---

## 8. 测试清单

### 8.1 E02 · 三集连续生产

| 用例 | 期望 |
|---|---|
| E02-A1 第 1–3 集各自封存 binding revision，同一角色同一 revision | 三集 `owner_ref` 相同；`binding_set_id` 不同；`resolve` 都返回 `chr_linlan@12` |
| E02-A2 第 3 集改服装，角色不变 | 第 3 集多一条 `costume:*` entry；角色 `role_key` 未变；第 1–2 集 revision 不变 |
| E02-A3 音色跨集固定 | `voice:` entry 的 `owner_ref` 三集一致；S15 撤销后 `resolve` 仍返回指针但 `rights_recheck_required=true` |
| E02-A4 每集 `binding_set_ref` 可回源 | 用 manifest 里的 revision 调 `GET /binding-sets/{id}/revisions/{rev}` 得到同一 `content_hash` |

### 8.2 E08 · 改前集与绑定

| 用例 | 期望 |
|---|---|
| E08-A1 第 2 集换道具版本 → 新 revision | 旧 revision `SUPERSEDED`；旧 manifest 仍指向旧 revision；`usage` 同时列出两版 |
| E08-A2 已提交第 3 集不被自动改写 | 第 3 集 `binding_set_revision` 与 `content_hash` 不变 |
| E08-A3 `usage` 返回种子而非闭包 | 响应里没有"需要重做"字段；断言 payload 无受影响集列表 |
| E08-A4 全剧默认换角色外观 → 新集生效，旧集不变 | 未封存的集展开到新默认；已封存集 `resolve` 返回旧 revision |

### 8.3 E12 · 幂等

| 用例 | 期望 |
|---|---|
| E12-A1 同 command_id 同 body 重投封存 | 返回首次 revision，无新行 |
| E12-A2 同 command_id 不同 body | 409 `IDEMPOTENCY_KEY_REUSED` |
| E12-A3 `POST /assets` 重复注册同 owner 对象 | 返回同一 `asset_id`，不报错 |
| E12-A4 两个前端并发封存相同内容 | 一个 201，一个 409 `DUPLICATE_BINDING_CONTENT` |

### 8.4 E28 · 跨租户

| 用例 | 期望 |
|---|---|
| E28-A1 租户 B 用租户 A 的 `binding_set_id` 调 resolve | 404，响应体不含任何 A 的字段 |
| E28-A2 租户 B 用同名 `owner_local_id` 铸造资产 | 成功，得到不同 `asset_id` |
| E28-A3 用 `content_hash` 猜测存在性 | 任何按 hash 的查询都不跨租户；`bsr_hash_uniq` 含 `tenant_id` |
| E28-A4 跨租户 `usage` 查询 | 404 |

### 8.5 E30 · 权利实时复查

| 用例 | 期望 |
|---|---|
| E30-A1 S15 撤销音色后，S10 `resolve` 仍成功 | 返回 `rights_ref` 与 `rights_recheck_required=true`；S10 不缓存撤销 |
| E30-A2 S02 用 `resolve` 结果做 dispatch 前复查 | 复查走 S15 实时接口；S10 不参与判定 |
| E30-A3 `rights_revision_set_hash` 与 S02 记录的 hash 不一致 | S02 判 `RIGHTS_REVOKED` 或重新取 binding revision；S10 不改历史 |

### 8.6 E37 · 缺素材与交付

| 用例 | 期望 |
|---|---|
| E37-A1 `unresolved_required_roles` 非空时封存 | 允许；`resolve` 如实返回缺口 |
| E37-A2 S02 `canonical_asset` gate 遇缺口 | `require_human`/`deny`，不是 pass |
| E37-A3 交付包引用 binding revision | 该 revision 可长期读取（即使资产已 tombstone） |

### 8.7 通用

| 用例 | 期望 |
|---|---|
| 并发封存 10 次同 head | 1 成功 9 冲突；无部分写；`binding_expansion` 行数与 payload 一致 |
| 封存中途崩溃（Outbox 已写未发） | 重启补发；事件 seq 与 revision 相等 |
| owner 服务全挂 | 封存 503；**不产出 deferred revision**（除非显式 import_mode + 授权） |
| import_mode 封存 | `verification=deferred`；S02 gate 判 `abstain` |
| 重放 `snapshot.manifest.committed` | `binding_usage` 不新增；inbox 去重 |
| 乱序 manifest 事件（rev 5 先于 rev 4） | 按 aggregate_sequence 缓冲；缺口超时回源 S08 |
| 独立升级：新版本 read 旧 revision | 旧 `payload` 全部可解析（expand→migrate→contract） |
| 回滚：旧版本 read 新 revision | 新字段均为可选，旧代码忽略而非报错 |

---

## 9. 反模式清单（写了就是错）

1. ❌ `resolve` 返回角色描述/参考图 URL → 内容权威在 S11，S10 只返回指针。
2. ❌ 在 S10 存"当前是否已授权" → 第二权利权威；撤销必然滞后。
3. ❌ 运行时展开绑定（每次 resolve 重算继承） → 全剧默认一改，在跑的集全变。
4. ❌ 给 BindingSetRevision 加 `REVOKED` / 允许原地改 → 历史 manifest 的 `content_hash` 失去意义。
5. ❌ 资产升级时由 S10 发"请重做"事件 → 第二影响分析权威，S09 才是闭包所有者。
6. ❌ `resolve` 只返回解析到的条目、不返回缺口 → 调用方把"缺音色"当成"不需要音色"。
7. ❌ 用别名解析绑定 → "小琳"歧义导致绑错人；绑定必须走 ID。
8. ❌ 跨租户按 `content_hash` 查询去重 → 泄漏存在性（E28）。
9. ❌ 把 `binding_usage` 当权威 → 它只是投影，落后时必须 `stale=true` 并回源 S08。
10. ❌ 在持有 `binding_set` 行锁的事务里做 owner HTTP 校验 → owner 一慢，全集串行化。
11. ❌ 允许 `role_key` 用 `asset_id` → ID 迁移（merge）会让历史 revision 不可解析；必须用 owner 本地 ID。
12. ❌ `asset_id` 复用 → 历史 manifest 指向语义不同的对象，审计无法解释。

---

## 10. 冲突（报告，不选边）

| # | 冲突 / 缺口 | 现状 | 本文用法 | 需要谁裁决 |
|---|---|---|---|---|
| C10-1 | `service.yaml`/catalog 只给了一个命令 `POST /binding-sets`，但封存是独立动作 | catalog | 拆成 `POST /binding-sets`（建容器）+ `POST /binding-sets/{id}/revisions`（封存） | `contracts/service-catalog.v2.json` + `openapi.v2.yaml` |
| C10-2 | `openapi.v2.yaml` 完全没有 S10 的任何路径（只有 21 个跨域操作） | openapi | 按 §5 新增；未改契约 | `contracts/openapi.v2.yaml` |
| C10-3 | `event-topics.v2.json` 只登记 `asset.binding-set.created`，没有 revision 更新/生命周期事件；但 S07/S09 需要感知绑定变化 | topics | 复用 `created`（每次封存都是"新 revision created"）；生命周期事件作为本地事件，不新增 topic | 契约维护方 |
| C10-4 | docs/10 说"BindingSet 引用 `owner/id/revision/hash/rights_revision`"，但没定义 `rights_revision` 的拥有者与形态 | docs/10 | 定义为 `rights_ref{rights_owner,rights_id,rights_revision,rights_hash}`；音色指向 S15，其余指向 S02 政策 | docs/10 作者 |
| C10-5 | docs/01 §3 说"资产内容与 canonical 版本由 S11–S15 拥有"，docs/10 又说"S10 持有检索投影"；未定义投影可否包含 owner 的标签/别名 | docs/01 vs docs/10 | 投影只含 ID、别名、owner revision 指针、使用集；**不含外观描述** | docs/10 作者 |
| C10-6 | `episode-manifest.v2` 的 `binding_set_ref` 只要求 `service_id/id/revision/content_hash`，未要求 `rights_revision`；但 docs/10 要求绑定固定权利版本 | schema vs docs/10 | 权利指针集合的 hash 放进 binding revision 的 `content_hash`（因此 manifest 间接固定权利版本），不新增 manifest 字段 | 契约维护方 |
| C10-7 | `service.yaml` 的 `depends_on` 是 S11–S15，但 S10 实际还需要读 S08 事件维护 usage 投影；catalog 未列 S08 | catalog | 依赖表是运行协作关系，事件消费不算启动依赖；如需显式化需改 catalog | catalog 维护方 |

---

## 11. 实施顺序与未决问题

### 11.1 顺序（全部生产开关默认关闭）

1. **契约先行**：`contracts/assets/binding-set.v1.schema.json`（本文 §5.3 的 payload 形态）+ `asset-identity.v1.schema.json`。必须先于任何服务代码。
2. `platform/asset-sdk`：JCS canonicalization、`role_key` 规范化、`content_hash` 计算、binding 展开算法。**展开算法必须只有一份实现**，S10 与校验工具共用。
3. S10 最小闭合：`POST /assets` + `POST /binding-sets` + `POST /binding-sets/{id}/entries` + `POST /binding-sets/{id}/revisions` + `GET /assets/{id}/resolve`。用 mock S11–S15（按 §5.1 契约返回固定 revision），先验 E12/E28。
4. 接真实 S11–S15 的 `GET .../revisions/{rev}`；开 `verify_owners`。
5. 接 S08 事件 → `binding_usage` 投影；验乱序与重放。
6. 检索投影（名称/别名/标签）。**它是最后一步**，因为它是纯便利功能，不影响正确性。
7. `import_mode` 与离线重建路径（**最后**，且需要单独授权）。

### 11.2 未决问题（需要实施会话或设计方回答）

1. **`rights_revision` 对非音色资产到底谁拥有？** 本文假设 S02 的 `rights_*` 政策对象。如果实际是 S03（项目合同）或外部 DAMS，`rights_ref` 的形态要改。
2. **跨剧复用资产的 `series_scope='*'` 是否允许？** 本文留了字段，但 docs/10 没提"通用道具跨剧复用"。若不允许，删掉该值以消除歧义。
3. **一次封存最多多少 entry？** 未定。若一集合计数千个镜头级覆盖，`payload` 会很大。上限需要与 S08 的 manifest 体量一起定。
4. **`variant_selector` 是否需要 S10 做 schema 校验？** 本文选择"完全不解释"。若要在 UI 上做选择器联动，S10 需要一份 `asset_type → variant_axes` 的注册表，那会引入少量领域知识 —— 需要设计方确认可接受。
5. **`binding_usage` 是否需要按 branch 隔离？** 本文按 manifest 的 branch 记录。分支实验的绑定使用是否应对正史隐藏，取决于 S47 的资产页需求。
6. **资产 `tombstoned` 后，历史交付包是否仍可解析？** 本文保证 binding revision 可读，但 owner 服务若真删了媒体，S10 只能返回一个指向已删 artifact 的指针。这需要 docs/14 §2 的保留合同给出答案。
