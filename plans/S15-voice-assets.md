# S15 · 音色资产 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码；SLO 与容量为 docs/03 的规划值，**未实测**。

上游依据：[docs/10](../docs/10-assets.md)（音色行 + 授权撤销）、[docs/01 §3](../docs/01-enterprise-architecture.md)、[docs/04](../docs/04-data-consistency.md)、[docs/06 §2–§4](../docs/06-automation-governance.md)、[docs/14 §2](../docs/14-enterprise-operations.md)、[S15 规格](../docs/design-package/services/S15-voice-assets.md)、[generation-command.v2](../contracts/generation-command.v2.schema.json)。

配套：[S10](S10-asset-catalog.md)（只读引用）、[S11](S11-character-assets.md) / [S12](S12-costume-assets.md) / [S13](S13-scene-assets.md) / [S14](S14-prop-assets.md)（**同级，不互相 import、不互相写库**）、[S28](S28-artifact-ingest.md)、[S02](S02-governance-policy.md)、[S16](S16-story-memory.md)、[S17](S17-continuity.md)、[S26](S26-generation-jobs.md)、[S48](S48-adaptation-training.md)。

---

## 0. 一句话边界

**S15 是"这个音色是什么、在哪个供应商用什么 ID、能不能被调用（授权）"的唯一权威。**

它是五类资产中**唯一持有"可撤销资格"状态的**。docs/10 明写：

> S10解析成功不代表权利永远有效，调用前S02/S15对可撤销资格做实时检查。

这句话给了 S15 一个别的资产服务都没有的职责：**它不是只读的资产目录，它是调用前的资格判定点。** 这意味着：

1. S15 的授权状态变更必须**立即可见**（不能靠最终一致的投影）。
2. S15 必须提供一个**低延迟、强一致**的资格查询接口。
3. 撤销事件禁止新调用；已付费/已排队的任务怎么办，是另一回事（见 §4.4）。

| 问题 | 归谁 |
|---|---|
| 这个音色的 `voice_id`、供应商映射 | **S15** |
| 克隆资格、授权状态、撤销 | **S15（唯一权威）** |
| 发音词典（多音字、专名读法） | **S15** |
| 语言、情绪能力、语速范围 | **S15** |
| 这个角色用什么声音 | S10 binding（`voice:` role_key） |
| 这句话说什么、带什么情绪 | S04/S19 剧本 + S26 的命令参数 |
| 音频 bytes | S28 |
| 预算是否够 | S23 |
| 是否允许调用（策略门） | S02（它实时问 S15） |
| 生成结果好不好 | S33/S34 QC |

### 0.1 写什么 / 不写什么

| 事实 | 唯一写者 |
|---|---|
| `VoiceRevision`（不可变） | **S15** |
| 平台 `voice_id` 与各供应商的映射 | **S15** |
| 发音词典 revision | **S15** |
| 语言/情绪/语速能力声明 | **S15** |
| **授权状态（granted/suspended/revoked）** | **S15** |
| 授权变更历史 | **S15** |
| 音色级权利引用 | **S15 存指针**，`rights_id` 指向外部的授权/合同对象 |
| 声纹样本 artifact 引用 | **S15 存引用**，bytes 归 S28 |
| 克隆训练的 LoRA/voice_clone 权重 | S48；S15 只存"可用的克隆 artifact"引用 |
| 某场戏谁说什么 | S04/S19 |
| 生成命令中的 `voice_binding{id,revision}` | S26（消费 S15 的 revision） |

### 0.2 四个明确不做

1. **不判定"这句话该用什么情绪"**。`emotion` 在 `generation-command` 的 speech input 里，由 S26/上游决定。S15 只声明"这个音色支持哪些情绪"。
2. **不判定预算/配额**。那归 S23。
3. **不替代 S02 做整体 policy 判定**。S02 会问 S15"这个音色授权有效吗"，但 S02 还要问 kill switch、预算、gate。**S15 只回答一个事实：资格状态。**
4. **不自动换相似声线**。`service.yaml` 的 recovery 明写："撤销状态实时验证；**不自动换成相似声线**；保留来源审计"。撤销后调用方要么停下、要么换一个用户显式指定的音色。自动替换是数据合规事故。

---

## 1. 关键区分

### 1.1 `VoiceRevision` vs 授权状态 vs 供应商映射

三层，生命周期不同：

```
VoiceRevision（不可变）          授权状态（可变，强一致）        供应商映射（可追加）
┌──────────────────────┐       ┌──────────────────────┐      ┌────────────────────────┐
│ voice_id 的平台定义   │       │ granted              │      │ provider_a: "va_123"   │
│ 语言/情绪/语速        │       │  ↓ suspend           │      │ provider_b: "vb_456"   │
│ 发音词典 revision     │       │ suspended            │      │ provider_c: "vc_789"   │
│ 权利对象引用          │       │  ↓ revoke            │      │ （各带回归证据）       │
│ 声纹样本引用          │       │ revoked（终态）       │      └────────────────────────┘
└──────────────────────┘       └──────────────────────┘
```

关键：

- **revision 不可变**，但**授权状态不属于 revision**。授权可以在 revision 不变的情况下撤销。
- 授权状态是**音色级**的（`voice_id`），不是 revision 级的。撤销一个音色，它的所有 revision 都不可新调用。
- 供应商映射**可追加**（新供应商上线），因为映射是"同一个声音在这个供应商的 ID"，不是内容变更。但**不能修改已有映射**（改了就无法解释历史音频是谁生成的）。

### 1.2 为什么授权状态必须是独立表且强一致

如果授权状态是 revision 的一个字段：

```
voice_revision(rev=2, authorization='granted')
```

撤销时只能追加 `rev=3, authorization='revoked'`。那么：

- 调用方引用 `rev=2` → 仍然看到 `granted` → **撤销失效**。
- 要让撤销生效，调用方必须每次都查"最新 revision 的授权"—— 那 revision 的语义就变成了"最新状态"，不再是不可变内容。

所以必须拆开：授权状态是**独立的、可变的、单行的**（每 `voice_id` 一行），revision 只记录内容。

### 1.3 "撤销" 与 "暂停" 与 "过期"

三个不同的状态，处理不同：

| 状态 | 含义 | 新调用 | 已排队任务 | 已生成内容 |
|---|---|---|---|---|
| `granted` | 授权有效 | 允许（仍需 S02 整体判定） | 继续 | 正常 |
| `suspended` | 临时不可用（供应商下架、复核中） | 拒绝 | 暂停（不取消，等待） | 保留 |
| `revoked` | 授权被撤回（终态） | 拒绝 | 取消 | 按保留合同处理 |
| `expired` | 授权到期，未续 | 拒绝 | 取消 | 按保留合同处理 |

`revoked` 和 `expired` 都禁止新调用，但语义不同：

- `revoked` 是主动撤回，通常伴随合规/法务事件，**可能要求删除历史音频**。
- `expired` 是自然到期，续约后可恢复为 `granted`。

**`revoked` 不可逆。** 要恢复必须新建 `voice_id`（新授权对象）。这与 S10 的 `tombstoned` 不可复活同理：撤销过一次的授权对象，历史必须有清晰分界。

### 1.4 平台 `voice_id` vs 供应商 `provider_voice_id`

- 平台 `voice_id`（`vox_...`）是 S15 内部与绑定层使用的稳定 ID。
- `provider_voice_id` 是每个供应商/模型快照上的实际 ID。

为什么必须分开：同一个声音在不同供应商可能有不同 ID，甚至同一供应商不同模型快照上 ID 不同（模型升级后声音变了）。S15 必须能回答"这个音色在 `model_snapshot=X` 上用哪个 ID、有没有回归证据"。

**没有跨供应商等价性假设。** `provider_a:va_123` 与 `provider_b:vb_456` 听起来可能差很多。所以每条映射都带**回归证据**（与声纹样本的相似度评测），且 `service.yaml` 的验收要求"跨供应商映射回归"。

### 1.5 发音词典

词典是**音色级**还是**项目级**？

本文的选择：**两份都支持，但分表**。

- `voice_lexicon_binding`：某音色默认使用的词典 revision（音色级）。
- `project_lexicon`：某项目/剧的词典（项目级，所有音色共用，用于专名统一读音）。

生成时，S26 的 `pronunciation_lexicon{id,revision}` 参数决定用哪份。S15 只**提供**词典内容，不决定用哪个。

为什么不做成"音色自带全部词典"：一部剧里 20 个角色对同一个专名的读法必须一致。如果每个音色各自维护，必然不一致。项目级词典是唯一能保证一致的方式。

### 1.6 授权撤销 vs 供应商下架

两回事：

- 授权撤销（`revoked`）：**法律/合同**层面不再允许使用这个声音。必须禁止所有调用。
- 供应商下架（`suspended`）：**技术**层面暂时用不了（API 下线、模型退役）。应触发 S24 的能力失效 + S25 的重新路由。

同一条映射同时可能遭遇两者。S15 分别建模：

- `authorization_state` → 授权。
- `provider_mapping.status` → 技术可用性。

调用前 S15 的资格检查**同时看两者**：任一不可用 → 拒绝（reason 不同）。

---

## 2. 数据模型

Postgres，schema `voice_assets`。所有表含 `tenant_id`，主键/唯一约束含 `tenant_id`（E28）。RLS 作为第二道防线（docs/14 §2）。

### 2.1 音色身份

```sql
CREATE TABLE voice_assets.voice (
  tenant_id     text NOT NULL,
  voice_id      text NOT NULL,               -- 'vox_01J...'
  project_id    text NOT NULL,
  series_scope  text NOT NULL,
  display_name  text NOT NULL,
  voice_kind    text NOT NULL CHECK (voice_kind IN (
                  'platform_preset','cloned','licensed_actor','synthetic','narrator')),
  character_ref jsonb,                       -- 软引用 {service_id:'S11', id:'chr_linlan'}
  status        text NOT NULL CHECK (status IN ('active','deprecated','tombstoned')),
  canonical_revision integer,
  current_revision   integer NOT NULL DEFAULT 0,
  created_by    jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, voice_id),
  UNIQUE (tenant_id, series_scope, display_name),
  CONSTRAINT voice_canonical_within_head CHECK (canonical_revision IS NULL OR canonical_revision <= current_revision)
);
CREATE INDEX voice_by_series ON voice_assets.voice (tenant_id, series_scope, status, voice_kind);
```

`voice_kind` 决定授权模型的严格程度：

- `platform_preset`：平台自有声音，授权由平台统一授予（通常长期有效）。
- `cloned`：从声纹样本克隆，**必须**有授权对象 + 克隆时长的限制。
- `licensed_actor`：演员本人授权，通常带**时间窗**和**用途限制**（例如"仅限本剧"）。
- `synthetic`：合成音，可能有"不得用于真人代言"等限制。
- `narrator`：旁白，可跨角色复用。

`voice_kind IN ('cloned','licensed_actor')` 时，`authorization` 必须有 `rights_ref` 和 `expires_at`。这是硬约束（§2.3 的 CHECK）。

### 2.2 不可变 revision

```sql
CREATE TABLE voice_assets.voice_revision (
  tenant_id          text NOT NULL,
  voice_id           text NOT NULL,
  revision           integer NOT NULL CHECK (revision >= 1),
  content_hash       char(64) NOT NULL,
  payload            jsonb NOT NULL,
  language_profile   jsonb NOT NULL,         -- {primary:'zh-CN', supported:[...], accent:'...'}
  emotion_profile    jsonb NOT NULL,         -- {supported:['neutral','happy',...], intensity_range:'...'}
  rate_profile       jsonb NOT NULL,         -- {min_syllables_per_sec, max, default}
  voice_sample_refs  jsonb NOT NULL,         -- 声纹样本 ArtifactManifest 引用（S28）
  clone_artifact_ref jsonb,                  -- S48 的 voice_clone artifact（若有）
  rights_ref         jsonb NOT NULL,
  source_kind        text NOT NULL CHECK (source_kind IN ('preset_registered','cloned','imported','derived')),
  parent_revision    integer,
  quality_gate_ref   jsonb NOT NULL,         -- 声线回归结果
  accepted_at        timestamptz NOT NULL,
  accepted_by        jsonb NOT NULL,
  PRIMARY KEY (tenant_id, voice_id, revision),
  CONSTRAINT vr_hash_len CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT vr_parent_lt CHECK (parent_revision IS NULL OR parent_revision < revision)
);
CREATE UNIQUE INDEX vr_hash_uniq ON voice_assets.voice_revision (tenant_id, content_hash);
```

### 2.3 授权状态（唯一可变、强一致）

```sql
CREATE TABLE voice_assets.voice_authorization (
  tenant_id        text NOT NULL,
  voice_id         text NOT NULL,
  state            text NOT NULL CHECK (state IN ('granted','suspended','revoked','expired')),
  rights_ref       jsonb NOT NULL,           -- {rights_owner,rights_id,rights_revision,rights_hash}
  authorized_uses  jsonb NOT NULL,           -- ['series:series_demo','modality:speech']
  scope_notes      text,                     -- 例如 "仅限本剧，不得用于广告"
  granted_at       timestamptz,
  expires_at       timestamptz,              -- NULL = 长期
  state_changed_at timestamptz NOT NULL DEFAULT now(),
  state_seq        bigint NOT NULL,          -- 单调递增，用于事件排序与谓词比对
  changed_by       jsonb NOT NULL,
  reason_code      text,
  reason_text      text,
  PRIMARY KEY (tenant_id, voice_id)
);
CREATE INDEX voice_auth_state ON voice_assets.voice_authorization (tenant_id, state);
CREATE INDEX voice_auth_expiry
  ON voice_assets.voice_authorization (tenant_id, expires_at)
  WHERE state = 'granted' AND expires_at IS NOT NULL;
```

关键设计点：

1. **主键是 `(tenant_id, voice_id)`，不是 revision。** 一个音色一条授权状态。
2. **`state_seq` 单调递增。** 每次状态变更 +1。调用方可以带 `if_state_seq` 做乐观并发（见 §4.2）。
3. **`expires_at` 的 CHECK 在应用层**（不是 DB），因为 `cloned`/`licensed_actor` 必须非空。用一个触发器或应用层断言：

```sql
-- 应用层断言（也可用触发器）
-- voice_kind IN ('cloned','licensed_actor') → expires_at IS NOT NULL AND rights_ref IS NOT NULL
```

4. **`revoked` 是终态**，DB 层用触发器阻止从 `revoked` 转出（除了新建 voice_id）。

```sql
CREATE OR REPLACE FUNCTION voice_assets.deny_revoked_transition() RETURNS trigger AS $$
BEGIN
  IF OLD.state = 'revoked' AND NEW.state <> 'revoked' THEN
    RAISE EXCEPTION 'revoked is terminal for voice_id %', OLD.voice_id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
```

这条触发器是**最后一道防线**。即使应用层有 bug，数据库也不允许撤销过的音色复活。

### 2.4 授权变更历史（审计）

```sql
CREATE TABLE voice_assets.authorization_event (
  tenant_id   text NOT NULL,
  voice_id    text NOT NULL,
  seq         bigint NOT NULL,               -- = 变更后的 state_seq
  from_state  text,
  to_state    text NOT NULL,
  reason_code text NOT NULL,
  reason_text text,
  evidence_ref jsonb,                        -- 撤销依据（合同/法务函件）的引用
  actor       jsonb NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, voice_id, seq)
);
CREATE INDEX auth_event_occurred ON voice_assets.authorization_event (tenant_id, occurred_at DESC);
```

`evidence_ref` 必须保留。docs/10："授权撤销事件禁止新调用，历史交付处理按企业保留合同决定"—— 但"谁依据什么撤销"必须可追溯，否则撤销本身无法被审计（E39）。

### 2.5 供应商映射

```sql
CREATE TABLE voice_assets.provider_voice_mapping (
  tenant_id        text NOT NULL,
  voice_id         text NOT NULL,
  voice_revision   integer NOT NULL,
  provider_id      text NOT NULL,            -- S24 的 provider
  channel_id       text NOT NULL,
  model_snapshot   text NOT NULL,            -- S24 的 model_snapshot（不是 model_id）
  region           text NOT NULL,
  provider_voice_id text NOT NULL,
  mapping_status   text NOT NULL CHECK (mapping_status IN ('active','deprecated','unavailable')),
  regression       jsonb NOT NULL,           -- {similarity_score, evaluated_against:'sample_ref', evaluated_at, passed}
  added_at         timestamptz NOT NULL DEFAULT now(),
  unavailable_reason text,
  PRIMARY KEY (tenant_id, voice_id, voice_revision, provider_id, channel_id, model_snapshot, region)
);
CREATE INDEX pvm_lookup
  ON voice_assets.provider_voice_mapping (tenant_id, voice_id, mapping_status);
CREATE INDEX pvm_by_snapshot
  ON voice_assets.provider_voice_mapping (tenant_id, provider_id, model_snapshot, mapping_status);
```

关键：

- **映射粒度为 `model_snapshot`，不是 `model_id`。** 这直接对应 S24 的规则（能力按 `model_snapshot` 判定，不按 `model_id`）。同一 provider 的同一 model_id 升级到新 snapshot，声音可能变，必须有独立映射与回归。
- **`regression.passed` 为 false 的映射不得被 S25 选中。** S15 只存结论；路由由 S25 读。
- 映射**只能追加，不能改**。换了 provider_voice_id 就是新行（同一 PK 冲突则拒绝）。历史音频靠旧行解释。
- `mapping_status='unavailable'` 与授权无关：技术不可用，触发 S24 能力失效，但不是撤销。

### 2.6 发音词典

```sql
CREATE TABLE voice_assets.lexicon (
  tenant_id     text NOT NULL,
  lexicon_id    text NOT NULL,               -- 'lex_01J...'
  scope_kind    text NOT NULL CHECK (scope_kind IN ('project','series','voice')),
  scope_id      text NOT NULL,               -- project_id / series_id / voice_id
  language      text NOT NULL DEFAULT 'zh-CN',
  head_revision integer NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, lexicon_id)
);
CREATE INDEX lexicon_by_scope ON voice_assets.lexicon (tenant_id, scope_kind, scope_id, language);

CREATE TABLE voice_assets.lexicon_revision (
  tenant_id    text NOT NULL,
  lexicon_id   text NOT NULL,
  revision     integer NOT NULL CHECK (revision >= 1),
  content_hash char(64) NOT NULL,
  entries      jsonb NOT NULL,               -- [{term, pinyin|ipa, part_of_speech, note}]
  entry_count  integer NOT NULL,
  created_by   jsonb NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, lexicon_id, revision),
  CONSTRAINT lexr_hash_len CHECK (content_hash ~ '^[0-9a-f]{64}$')
);
CREATE UNIQUE INDEX lexr_hash_uniq ON voice_assets.lexicon_revision (tenant_id, content_hash);

CREATE TABLE voice_assets.voice_lexicon_binding (
  tenant_id   text NOT NULL,
  voice_id    text NOT NULL,
  lexicon_id  text NOT NULL,
  lexicon_revision integer NOT NULL,
  is_default  boolean NOT NULL DEFAULT false,
  PRIMARY KEY (tenant_id, voice_id, lexicon_id)
);
CREATE UNIQUE INDEX voice_default_lexicon
  ON voice_assets.voice_lexicon_binding (tenant_id, voice_id)
  WHERE is_default;
```

词典 revision 不可变。生成命令里的 `pronunciation_lexicon{id,revision}` 直接引用它（见 `generation-command.v2` speech input）。

**词典是内容资产，不是授权资产。** 撤销音色授权不影响词典；词典变更不影响授权。

### 2.7 候选与 canonical

```sql
CREATE TABLE voice_assets.voice_candidate (
  tenant_id     text NOT NULL,
  candidate_id  text NOT NULL,
  voice_id      text NOT NULL,
  content_hash  char(64) NOT NULL,
  payload       jsonb NOT NULL,
  voice_check   jsonb NOT NULL,              -- {regression_pass, authorization_present, clone_duration_ok, provider_coverage}
  state         text NOT NULL CHECK (state IN ('PENDING','ACCEPTED','REJECTED','EXPIRED')),
  created_by    jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, candidate_id)
);

CREATE TABLE voice_assets.canonical_pointer (
  tenant_id        text NOT NULL,
  voice_id         text NOT NULL,
  revision         integer NOT NULL,
  decided_at       timestamptz NOT NULL DEFAULT now(),
  decided_by       jsonb NOT NULL,
  decision_kind    text NOT NULL CHECK (decision_kind IN ('human_approved','policy_auto','initial_import')),
  reason_text      text,
  prior_canonical  integer,
  superseded_at    timestamptz,
  PRIMARY KEY (tenant_id, voice_id, revision)
);
CREATE UNIQUE INDEX voice_canonical_one
  ON voice_assets.canonical_pointer (tenant_id, voice_id)
  WHERE superseded_at IS NULL;
```

### 2.8 基础设施

```sql
CREATE TABLE voice_assets.idempotency_record (
  tenant_id   text NOT NULL,
  command_id  text NOT NULL,
  request_hash char(64) NOT NULL,
  command_type text NOT NULL,
  state       text NOT NULL CHECK (state IN ('IN_PROGRESS', 'DONE')),
  response_code integer,
  response_body jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE voice_assets.outbox (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id         text NOT NULL,             -- 事件信封 required
  aggregate_id      text NOT NULL,              -- voice_id
  aggregate_sequence bigint NOT NULL,
  event_type        text NOT NULL,
  partition_key     text NOT NULL,             -- tenant_id || ':' || aggregate_id
  payload           jsonb NOT NULL,
  headers           jsonb NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  published_at      timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON voice_assets.outbox (id) WHERE published_at IS NULL;

CREATE TABLE voice_assets.inbox (
  consumer    text NOT NULL,
  event_id    text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

`aggregate_id = voice_id`，`aggregate_sequence = revision`。

### 2.9 不建的表

- ❌ `voice_usage_index`（"哪个角色用这个声音"归 S10 binding）。
- ❌ `voice_character_binding`（同上）。
- ❌ `voice_generation_history`（归 S26/S28）。
- ❌ `voice_budget` / `voice_quota`（归 S23）。
- ❌ `voice_policy_decision`（归 S02）。
- ❌ `voice_similar_substitute`（**禁止**自动换相似声线，连映射表都不建）。

---

## 3. 状态机

### 3.1 授权状态机（核心）

```
                    ┌───────────┐
      register ────►│  granted  │◄──── renew（expired → granted）
                    └─────┬─────┘
                          │
              ┌───────────┼───────────┐
              │           │           │
        suspend│      expire│      revoke│
              ▼           ▼           ▼
        ┌──────────┐ ┌─────────┐ ┌─────────┐
        │suspended │ │ expired │ │ revoked │  ← 终态
        └────┬─────┘ └────┬────┘ └─────────┘
             │            │
        resume│      renew│
             ▼            ▼
        ┌──────────┐      │
        │ granted  │◄─────┘
        └──────────┘
```

合法转移表：

| 从 | 到 | 条件 |
|---|---|---|
| （无） | `granted` | 有 `rights_ref`；`cloned`/`licensed_actor` 有 `expires_at` |
| `granted` | `suspended` | 供应商不可用 / 复核中；需 `reason_code` |
| `suspended` | `granted` | 原因解除；需 `reason_code` |
| `granted` | `revoked` | 有 `evidence_ref`；**不可逆** |
| `suspended` | `revoked` | 同上 |
| `granted` | `expired` | 定时任务：`expires_at < now()` |
| `expired` | `granted` | 续约，新 `expires_at` |
| `revoked` | * | **禁止**（DB 触发器） |

**`revoked` 转出的唯一方式是新建 `voice_id`。** `authorization_event` 记录完整链路。

### 3.2 映射状态机

```
active ──deprecate──► deprecated ──reactivate──► active
   │                     │
   └──unavailable（供应商下架/模型退役）──► unavailable ──restore──► active
```

`unavailable` 触发 S24 能力失效（S15 发本地事件，S24/S25 有自己的机制）。**这不影响授权状态。**

### 3.3 Voice 与 canonical

```
active ──deprecate──► deprecated ──tombstone──► tombstoned
```

canonical 独立指针。**授权 `revoked` 时，canonical 不清空**（历史 revision 仍可读，但新调用被拒）。

### 3.4 非法转移

| 从 | 到 | 原因 |
|---|---|---|
| `revoked` | 任何 | 终态；新建 ID |
| revision 覆写 | — | 不可变 |
| 映射 `provider_voice_id` 修改 | — | 追加新行 |
| `cloned`/`licensed_actor` 无 `expires_at` | `granted` | 必须有期限 |
| `revoked` 音色 | 新 canonical | 不应有新的内容基准 |
| 读接口对 `revoked` 返回"可用" | — | 必须返回 `eligible=false` |

---

## 4. 关键流程

### 4.1 注册音色与授权

```
POST /voices
{
  command_id, series_scope, display_name, voice_kind, character_ref,
  language_profile, emotion_profile, rate_profile,
  voice_sample_refs, rights_ref,
  authorization: {authorized_uses, scope_notes, expires_at}
}
```

事务外：

1. **声纹样本校验**：`voice_sample_refs` 逐个调 S28，存在 + hash 一致 + **来自隔离导入器**（E29）。若样本未经过恶意媒体检查 → 422 `SAMPLE_NOT_SCANNED`。
2. **授权完整性**：`voice_kind IN ('cloned','licensed_actor')` 且 `expires_at IS NULL` → 422 `AUTHORIZATION_EXPIRY_REQUIRED`。
3. **用途声明**：`authorized_uses` 必须非空。空表示"没有明确用途"，无法审计。

事务内：写 `voice` + `voice_authorization(state='granted', state_seq=1)` + `authorization_event(seq=1)` + outbox（`asset.voice.authorization.changed`）。

### 4.2 调用前资格检查（**S15 最重要的接口**）

```
POST /voices/{id}:check-eligibility
{
  requested_use: {series_id, modality:'speech', provider_id?, channel_id?, model_snapshot?, region?},
  voice_revision: int,
  if_state_seq?: int
}
```

响应：

```jsonc
{
  "voice_id":"vox_linlan",
  "eligible": true,
  "authorization_state":"granted",
  "state_seq": 7,
  "expires_at":"2027-03-01T00:00:00Z",
  "provider_mapping": {
    "provider_id":"p_azure", "channel_id":"c_default", "model_snapshot":"ms_2026_03",
    "provider_voice_id":"zh-CN-XiaoxiaoNeural", "regression_passed": true
  } | null,
  "deny_reason": null,
  "checked_at":"2026-10-31T09:00:00Z"
}
```

拒绝时：

```jsonc
{
  "voice_id":"vox_linlan",
  "eligible": false,
  "authorization_state":"revoked",
  "state_seq": 9,
  "deny_reason":"AUTHORIZATION_REVOKED",   // | AUTHORIZATION_EXPIRED | AUTHORIZATION_SUSPENDED
                                            // | USE_NOT_AUTHORIZED | MAPPING_UNAVAILABLE
                                            // | REGRESSION_FAILED | STATE_SEQ_STALE
  "detail":"..."
}
```

**接口语义（必须逐条实现）：**

1. **强一致读。** 直接查 `voice_authorization`（单行主键），**不允许**从缓存/投影读。整个 S15 只有这一处不能缓存。
2. **即使 `eligible=false`，仍返回有用的字段**（`state_seq`、`deny_reason`）。调用方要能区分"撤销了"和"这个用途没授权"。
3. **`if_state_seq` 是乐观检查。** 调用方（S26）可以在提交付费任务前记录 `state_seq`，在真正调用前带 `if_state_seq` 再查一次。若 `state_seq` 变了 → `STATE_SEQ_STALE`，调用方应重新评估（可能刚好被撤销了）。这不是必需的，但给了"长队列"场景一个强的二次确认点。
4. **`provider_mapping=null` 且 `eligible=true`：** 表示授权有效但请求的 provider/snapshot 没有映射。此时 `eligible` 应为 **false** + `MAPPING_UNAVAILABLE`。只有"授权有效且映射可用"才是完整可用。
5. **`requested_use` 不在 `authorized_uses` 内 → `USE_NOT_AUTHORIZED`。** 例如授权"仅限本剧"，请求另一部剧 → 拒绝。这条常被漏掉，但它是 licensed_actor 的核心限制。
6. **`expires_at < now()` 但定时任务还没跑 → 按 `expired` 处理**（读时判定，不依赖定时任务）。这是"实时检查"的严格要求：状态字段可能滞后，但判定不能滞后。

### 4.3 撤销授权

```
POST /voices/{id}:revoke
{command_id, reason_code, reason_text, evidence_ref, effective_at?}
```

事务内：

```
BEGIN;
-- 1) 锁 voice_authorization 行 FOR UPDATE
-- 2) OLD.state = 'revoked' → 409 ALREADY_REVOKED
-- 3) UPDATE voice_authorization
--      SET state='revoked', state_seq=state_seq+1, state_changed_at=now(), ...
-- 4) INSERT authorization_event (seq=new_seq, evidence_ref)
-- 5) INSERT outbox (asset.voice.authorization.changed, seq=new_seq)
COMMIT;
```

**不做的事（重要）：**

- 不调用 S26 去取消已提交的任务。那是 S26 自己消费事件后做的（见 §4.4）。
- 不删除任何音频。
- 不修改任何 revision。
- 不发"请重做这些镜头"的事件（那是 S09 的影响分析）。

S15 只做一件事：**把状态改掉并发事件。** 所有后续动作由消费者各自负责。

### 4.4 撤销后的在途任务（跨服务协作，S15 只定义契约）

S15 撤销后，可能出现：

| 任务状态 | 应该发生什么 | 由谁执行 |
|---|---|---|
| 尚未提交（`PREPARED`） | 不提交，任务失败 | S26 |
| 已提交、供应商未开始 | 取消（若供应商支持），否则等结果但**归档后不交付** | S26 |
| 已提交、`SUBMISSION_UNKNOWN` | **不盲重提**（AGENTS.md）；等待回查 | S26 |
| 已完成、已归档 | 保留（历史交付），按保留合同处理 | 企业策略 / S27 |
| 已进入成片 | **不自动重制**；走 S07/S09 影响计划 | S07/S09 |

S15 **不**驱动这些。它只保证：**任何新的 `check-eligibility` 都返回 `eligible=false`**。

`effective_at` 的语义：如果提供未来的 `effective_at`，S15 记录它，但**立即**开始拒绝新调用？本文的选择：**立即拒绝**，`effective_at` 仅作审计记录。理由：合规撤销的"生效时间"通常指"从此刻起不得再使用"，而不是"从此刻起不得再提交"。如果需要宽限期，应由 S02 的策略处理，不由 S15 的资格接口处理。见 §10 C15-3。

### 4.5 供应商映射登记与回归

```
POST /voices/{id}/provider-mappings
{
  command_id, voice_revision, provider_id, channel_id, model_snapshot, region,
  provider_voice_id, regression: {similarity_score, evaluated_against, passed}
}
```

校验：

1. `voice_revision` 存在（`GET /voices/{id}/revisions/{rev}` 的等价内部校验）。
2. `regression.passed=true` 才能建 `active` 映射；否则 `mapping_status='deprecated'`，仍可存但不可路由。
3. 同 PK 已存在 → 409 `MAPPING_EXISTS`（不允许改 `provider_voice_id`）。

发布本地事件 `voice.provider-mapping.changed`（**本地事件**，不进契约 topic；S24/S25 通过同步查询或各自的事件获取，见 C15-4）。

### 4.6 发音词典

```
POST /lexicons
POST /lexicons/{id}/revisions
GET  /lexicons/{id}/revisions/{revision}
POST /voices/{id}/lexicon-binding
```

词典条目：

```jsonc
{"term":"重","readings":[{"pinyin":"zhòng","context":"重量"},{"pinyin":"chóng","context":"重复"}],
 "part_of_speech":"verb","note":"专名中读 chóng"}
```

**词典不做自动应用。** 生成时由 S26 传 `pronunciation_lexicon{id,revision}`，S15 只提供内容。S15 不知道"这句话里有没有这个词" —— 那是 S26/供应商的 TTS 前端的事。

### 4.7 失败矩阵

| # | 场景 | 行为 | 错误码 / deny_reason |
|---|---|---|---|
| F1 | 授权 `revoked` | 拒绝调用 | `eligible=false`, `AUTHORIZATION_REVOKED` |
| F2 | 授权 `expired`（无论定时任务是否跑过） | 拒绝 | `AUTHORIZATION_EXPIRED` |
| F3 | 授权 `suspended` | 拒绝 | `AUTHORIZATION_SUSPENDED` |
| F4 | 用途不在 `authorized_uses` | 拒绝 | `USE_NOT_AUTHORIZED` |
| F5 | 请求 provider/snapshot 无映射 | 拒绝 | `MAPPING_UNAVAILABLE` |
| F6 | 映射回归未通过 | 拒绝 | `REGRESSION_FAILED` |
| F7 | `if_state_seq` 与当前不符 | 拒绝 | `STATE_SEQ_STALE` |
| F8 | 声纹样本未过恶意媒体检查 | 拒注册 | 422 `SAMPLE_NOT_SCANNED` |
| F9 | `cloned`/`licensed_actor` 无 `expires_at` | 拒注册/授权 | 422 `AUTHORIZATION_EXPIRY_REQUIRED` |
| F10 | `authorized_uses` 为空 | 拒 | 422 `USE_SCOPE_REQUIRED` |
| F11 | 撤销已撤销的音色 | 拒 | 409 `ALREADY_REVOKED` |
| F12 | 撤销缺 `evidence_ref` | 拒 | 422 `REVOCATION_EVIDENCE_REQUIRED` |
| F13 | 尝试把 `revoked` 改回 | 拒 | 422 `REVOCATION_TERMINAL`（DB 触发器兜底） |
| F14 | 映射改了 `provider_voice_id` | 拒 | 409 `MAPPING_EXISTS` |
| F15 | 同内容重复接受 revision | 拒 | 409 `DUPLICATE_VOICE_CONTENT` |
| F16 | 跨租户查资格 | 404 | `NOT_FOUND` |
| F17 | S28 超时 | 不落候选 | 503 `DEPENDENCY_UNAVAILABLE` |
| F18 | 词典条目格式错误 | 拒 | 422 `LEXICON_ENTRY_INVALID` |

---

## 5. 对外契约

### 5.1 命令

| 方法 | 路径 |
|---|---|
| POST | `/voices` |
| POST | `/voices/{id}/candidates` |
| POST | `/voices/{id}/candidates/{cid}:accept` |
| POST | `/voices/{id}/candidates/{cid}:reject` |
| POST | `/voices/{id}:set-canonical` |
| POST | `/voices/{id}:revoke` / `:suspend` / `:resume` / `:renew` |
| POST | `/voices/{id}/provider-mappings` |
| POST | `/voices/{id}/provider-mappings/{pk}:deprecate` / `:unavailable` / `:restore` |
| POST | `/voices/{id}:check-eligibility` | **调用前资格检查** |
| POST | `/lexicons` / `/lexicons/{id}/revisions` |
| POST | `/voices/{id}/lexicon-binding` |

`service.yaml` 的命令是 `POST /voices/{id}/revisions`。本文按 §5 拆分（候选/接受），并新增 `check-eligibility`（C15-1）。

### 5.2 查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/voices/{id}` | 音色 + 授权摘要 |
| GET | `/voices/{id}/revisions/{revision}` | 内容 revision |
| GET | `/voices/{id}/bindings` | **`service.yaml` 原定的查询**：音色绑定（角色/项目/词典） |
| GET | `/voices/{id}/authorization` | 当前授权状态 + 历史 |
| GET | `/voices/{id}/provider-mappings?provider_id=&model_snapshot=` | 映射 |
| GET | `/voices/{id}/canonical` | |
| GET | `/lexicons/{id}/revisions/{revision}` | |
| GET | `/voices:lookup?series_scope=&alias=` | |

注意 `service.yaml` 的查询是 `/voices/{id}/bindings` 而不是 `/revisions/{revision}` —— 与 S11–S14 不一致。本文保留它（返回音色与角色/项目/词典的绑定关系），同时**额外**提供 revision 读取。这是 §10 的 C15-2。

### 5.3 事件

**S15 只发布一个契约事件**（`service.yaml` 明写）：

`asset.voice.authorization.changed` on `drama.voice-assets.v2`，partition key `tenant_id:voice_id`，`aggregate_sequence = state_seq`：

```jsonc
{
  "specversion":"1.0","schema_version":"2.0","id":"evt_...","source":"S15",
  "type":"asset.voice.authorization.changed",
  "subject":"voice/vox_linlan","time":"2026-10-31T09:00:00Z",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"vox_linlan","aggregate_sequence":9,
  "correlation_id":"cmd_...","causation_id":"cmd_...","traceparent":"00-...",
  "data":{
    "voice_id":"vox_linlan",
    "from_state":"granted",
    "to_state":"revoked",
    "state_seq":9,
    "reason_code":"RIGHTS_WITHDRAWN",
    "effective_at":"2026-10-31T09:00:00Z",
    "evidence_ref":{"service_id":"S02","id":"rights_doc_01J...","revision":2},
    "authorized_uses_before":["series:series_demo","modality:speech"],
    "expires_at":"2027-03-01T00:00:00Z"
  },
  "data_ref":null
}
```

消费方（S26/S02/S10 投影/S17）的语义：

- `to_state IN ('revoked','expired')` → **禁止新调用**。
- `to_state IN ('suspended')` → 停止路由，等待 resume。
- 事件**只是通知**。真正的判定必须走 `check-eligibility`（实时）。这是文档里"审核已通过不能当放行依据"的同构规则。

**没有 `asset.voice.revision.accepted` 事件。** 这是本文必须报告的重要缺口（C15-2）：S11–S14 都有 revision accepted 事件，只有 S15 没有。这意味着 S10 的检索投影、S17 的音色缓存都没有接受事件可订阅。本文的应对：`check-eligibility` 同时返回 `voice_revision` 的可用性；订阅方只能靠轮询/显式查询。

消费：

| 事件 | 来源 | 处理 |
|---|---|---|
| `artifact.archived` | S28 | 审计 |
| `training.completed`（adaptation_type=voice_clone） | S48 | 把 `clone_artifact_ref` 加入候选建议；**不自动接受** |

**不消费** S11 的角色事件。音色与角色是两类资产，绑定由 S10 决定。

### 5.4 错误码汇总

| HTTP | code | retryable |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | false |
| 403 | `TENANT_SCOPE_DENIED` | false |
| 404 | `NOT_FOUND` / `CANONICAL_NOT_SET` | false |
| 409 | `VERSION_CONFLICT`(true) / `ALREADY_REVOKED` / `MAPPING_EXISTS` / `DUPLICATE_VOICE_CONTENT` / `CANDIDATE_NOT_PENDING` / `IDEMPOTENCY_KEY_REUSED` | 见括号 |
| 422 | `SAMPLE_NOT_SCANNED` / `AUTHORIZATION_EXPIRY_REQUIRED` / `USE_SCOPE_REQUIRED` / `REVOCATION_EVIDENCE_REQUIRED` / `REVOCATION_TERMINAL` / `LEXICON_ENTRY_INVALID` / `ARTIFACT_NOT_FOUND` / `ARTIFACT_HASH_MISMATCH` | false |
| 429 | `WRITE_BACKPRESSURE` | true |
| 503 | `DEPENDENCY_UNAVAILABLE` | true |

`check-eligibility` 的拒绝**不返回 4xx**，返回 200 + `eligible=false`。理由：调用方需要完整的状态信息来做决策（记录、重试、切换预案），4xx 会丢失响应体语义上的细节；而且"不可用"不是请求错误。

---

## 6. 幂等与并发

### 6.1 幂等

- 键 `(tenant_id, command_id)` + JCS hash。
- `POST /voices`：「自然幂等」在这里必须说清按什么判等，否则并发两次建出两条记录。
  取 `UNIQUE (tenant_id, lower(display_name))` 是**错的** —— 两个不同演员可以同名，而
  §1.4 已经说清平台的 `voice_id` 与供应商的 `provider_voice_id` 是两层。建 `voice` 时
  `provider_mapping` 还不存在（映射是后续登记的 1:N），所以没有天然的判等键。
  **结论：`POST /voices` 不是自然幂等，必须走 `command_id + request_hash`。**
  客户端重试靠幂等记录，不靠名字；想避免"同名两个音色"是产品问题，用
  `UNIQUE (tenant_id, project_id, series_scope, lower(display_name))` 做**软性**约束时
  要允许 `409 NAME_CONFLICT` 并可显式 override，不能偷偷合并。
- `check-eligibility` 是只读语义的 POST（带复杂 body），**完全幂等**，不需要 idempotency_record（但要注意它不走缓存）。

### 6.2 并发

| 竞争 | 机制 |
|---|---|
| 两次撤销 | `voice_authorization` 行锁 → 第二个 409 `ALREADY_REVOKED` |
| 撤销与 resume 并发 | 行锁；`revoked` 终态 |
| 撤销与资格查询并发 | 资格查询直接读已提交行；撤销提交后立即对后续读可见 |
| 两次接受 revision | `voice` 行锁 |
| 两次设 canonical | `voice_canonical_one` |
| 映射并发登记 | 主键冲突 → 409 |
| 默认词典并发设置 | `voice_default_lexicon` 部分唯一索引 |

### 6.3 撤销与资格查询的时序保证

这是 S15 最重要的正确性属性：

```
T1: S26 调 check-eligibility            → eligible=true, state_seq=7
T2: S15 执行 revoke（提交 state_seq=8）
T3: S26 提交付费生成任务
```

在 T2 与 T3 之间，S26 基于一个已失效的判定提交了付费任务。这是**无法完全消除**的竞态（除非 S15 与 S26 共享事务，那违反服务边界）。

S15 能提供的缓解：

1. **`state_seq` 单调递增**，S26 可以在提交前带 `if_state_seq=7` 再查一次。若返回 `STATE_SEQ_STALE`，说明期间有变更。
2. **撤销事件发布在本地事务内**（Outbox），保证不丢。
3. **文档明确这个窗口存在**（§10），让 S26 知道需要"提交前二次确认 + 事后取消"。

**不能做的事：** 声称 S15 的资格检查可以绝对防止撤销后的付费调用。那需要分布式事务，本架构没有。

### 6.4 锁顺序

```
1. voice (FOR UPDATE)              -- 仅内容变更路径
2. voice_authorization (FOR UPDATE) -- 仅状态变更路径
3. canonical_pointer
4. voice_revision (INSERT)
5. authorization_event (INSERT)
6. outbox
```

内容变更与状态变更**不共享锁行**（`voice` vs `voice_authorization`），所以它们可以并发。这是刻意的：撤销不应等待一次内容接受完成。

---

## 7. 可观测

指标：

- `s15_eligibility_check_total{result}`、`s15_eligibility_check_latency_seconds{quantile}` ← **P99 必须低**
- `s15_eligibility_deny_total{reason}`
- `s15_authorization_change_total{from,to}`
- `s15_revocation_total{voice_kind,reason_code}`
- `s15_expiring_within_days_total{voice_kind,days}`
- `s15_mapping_regression_fail_total`
- `s15_lexicon_revision_total`

告警（按严重性）：

| 告警 | 条件 | 为什么 |
|---|---|---|
| `VoiceEligibilityDependencyDown` | `check-eligibility` 5xx 率 > 1% | 调用链会 fail closed，全线停摆 |
| `VoiceEligibilityLatencyHigh` | P99 > 50ms | 它在每次 dispatch 前的关键路径上 |
| `VoiceRevokedButStillCalled` | 撤销后仍有 `eligible=true`（不应发生） | **P0**：状态机或缓存 bug |
| `VoiceExpiringSoon` | 7 天内到期且 `licensed_actor` | 提前续约 |
| `MappingRegressionFailed` | 任一映射回归失败 | 该 provider 路径不可路由 |
| `RevocationWithoutEvidence` | `evidence_ref` 为空 | 合规缺口 |

Runbook：

- `revocation-drill`：演练撤销 → 验证新调用全部被拒 → 验证在途任务被 S26 正确处理。
- `eligibility-consistency-check`：对每个 `granted` 音色，比对 `voice_authorization.state` 与 `expires_at` 的实时判定是否一致（防止"状态字段说 granted、实际已过期"）。
- `expiry-sweep`：定时任务，把 `expires_at < now()` 的 `granted` 置 `expired` 并发事件。**注意**：即使它失败，读路径仍按 `expires_at` 实时判定（§4.2 第 6 条）。
- `mapping-regression-audit`：列出 `regression.passed=false` 却仍 `active` 的映射。

---

## 8. 测试清单

### E30 · BYOK 轮换 + 音色授权撤销（**本服务的核心验收**）

| 用例 | 期望 |
|---|---|
| E30-C1 撤销后 `check-eligibility` 立即返回 `eligible=false` | 同一秒内可读到；`deny_reason=AUTHORIZATION_REVOKED` |
| E30-C2 撤销后所有 revision 均不可用 | `rev_1..rev_n` 全部 `eligible=false` |
| E30-C3 撤销后 S26 新任务被拒 | S26 消费事件 + 调资格接口 → 不提交 |
| E30-C4 撤销后不自动换相似声线 | 系统内不存在任何"替代音色"映射；调用方必须显式指定 |
| E30-C5 `revoked` 不可复活 | 任何试图改回的操作 422；DB 触发器兜底 |
| E30-C6 撤销留审计 | `authorization_event` 有 `evidence_ref`、`actor`、`reason_code` |
| E30-C7 暂停与撤销区分 | `suspended` 可 resume；`revoked` 不可 |
| E30-C8 `expires_at` 已过但定时任务未跑 | 仍返回 `eligible=false`（实时判定） |
| E30-C9 `if_state_seq` 陈旧 | 返回 `STATE_SEQ_STALE` |
| E30-C10 撤销与资格查询并发 | 提交后所有后续读均见新状态；无"读到旧值"的窗口超出单次读 |

### E02 · 三集连续

| E02-F1 同一音色三集 canonical 不变 | 三集 `resolve` 同一 `voice_ref` |
| E02-F2 跨供应商映射回归 | 两个 provider 的映射都 `regression.passed=true`；相似度在一个合理区间 |
| E02-F3 发音词典跨集一致 | 项目级词典 revision 固定；三集读同一份 |

### E08 · 改前集

| E08-F1 撤销后已提交第 1 集不被自动重制 | S15 不驱动；S09 影响计划负责 |
| E08-F2 替换音色 → 新 voice_id | 旧集不受影响（binding 未改）；新集用新 ID |

### E12 · 幂等

| E12-F1 重复撤销 | 第二次 409 `ALREADY_REVOKED`；不重复发事件 |
| E12-F2 同 command 重复注册 | 返回既有 |
| E12-F3 `check-eligibility` 重复调用 | 结果一致；无副作用 |

### E28 · 跨租户

| E28-F1 跨租户查资格 | 404（不泄漏音色存在） |
| E28-F2 跨租户撤销 | 404 |
| E28-F3 同一 `voice_id` 在不同租户 | 独立；`voice_id` 含租户隔离的唯一约束 |

### E29 · 恶意样本

| E29-F1 声纹样本未经 S28 扫描 | 422 `SAMPLE_NOT_SCANNED` |
| E29-F2 样本指向非隔离导入的 artifact | 拒绝 |
| E29-F3 声纹样本被删除/篡改 | 注册时 422 |

### E39 · 审计与保留

| E39-F1 每次授权变更可追溯 | `authorization_event` 完整链 |
| E39-F2 撤销依据可查 | `evidence_ref` 可回源 S02/法务 |
| E39-F3 保留期到期后的样本删除 | 不删 `authorization_event`（审计必须保留）；删样本字节需 S28/Legal |
| E39-F4 训练/克隆样本的访问审计 | 谁读过样本（走 S28 的审计） |

### E31 · 数据驻留

| E31-F1 音色映射限 region | `provider_voice_mapping.region` 决定可用区域 |
| E31-F2 跨区请求 | 无该 region 映射 → `MAPPING_UNAVAILABLE` |

### E01 · 独立升级

| E01-F1 新版本读旧 revision | 可读 |
| E01-F2 回滚 | 旧代码忽略新字段 |

### 通用

| 撤销后 100 次并发资格查询 | 全部 `eligible=false` |
| 撤销事件乱序（seq 8 先于 7） | 消费者按 seq 处理；最终状态以最大 seq 为准 |
| Outbox 崩溃恢复 | 事件不丢 |
| `expired → granted` 续约 | `state_seq` 继续递增，不重置 |

---

## 9. 反模式

1. ❌ **撤销后自动换相似声线**（AGENTS/docs 双重禁止）。
2. ❌ 缓存资格状态（`check-eligibility` 必须强一致）。
3. ❌ 把授权状态放进 `voice_revision`（撤销就对旧 revision 失效）。
4. ❌ 允许 `revoked` 转回 `granted`。
5. ❌ 撤销时 S15 自己去取消 S26 的任务（越界）。
6. ❌ 撤销时 S15 发"请重做镜头"事件（第二影响分析权威）。
7. ❌ 用 `model_id` 而不是 `model_snapshot` 建映射。
8. ❌ 修改已有映射的 `provider_voice_id`。
9. ❌ 依赖定时任务才能让 `expires_at` 生效（必须读时判定）。
10. ❌ `authorized_uses` 为空就放行。
11. ❌ `check-eligibility` 返回 4xx 表示不可用（应 200 + `eligible=false`）。
12. ❌ 把旁白音色与角色音色混为一类（`voice_kind` 区分）。
13. ❌ 词典让每个音色各存一份（项目级统一）。
14. ❌ S15 判定预算/配额（归 S23）。

---

## 10. 冲突（报告，不选边）

| # | 冲突 / 缺口 | 现状 | 本文用法 |
|---|---|---|---|
| C15-1 | `service.yaml` 命令只有 `POST /voices/{id}/revisions`；**没有调用前资格检查接口**，但 docs/10 要求"S02/S15 对可撤销资格做实时检查" | catalog vs docs/10 | 本文新增 `POST /voices/{id}:check-eligibility`（§4.2）。这是**本文最关键的新增**，必须进契约 |
| C15-2 | `service.yaml` 查询是 `GET /voices/{id}/bindings`，与 S11–S14 的 `/revisions/{revision}` 不一致；且 S15 只发 `authorization.changed`，**没有 revision accepted 事件** | catalog | 本文保留 `/bindings` 并额外提供 `/revisions/{rev}`；revision 接受无事件，订阅方只能显式查询。需契约方裁决 |
| C15-3 | docs/10 说"撤销事件禁止新调用"，未定义 `effective_at` 的未来生效语义 | docs/10 | 本文选择"立即拒绝新调用，`effective_at` 仅审计"；若需宽限期应由 S02 策略处理 |
| C15-4 | S15 的供应商映射变更（下架/恢复）没有契约事件；S24/S25 如何得知未定义 | topics | 本文发本地事件 + 提供同步查询；需契约方确认 |
| C15-5 | docs/10 说资格检查由"S02/S15"执行，`plans/S02-governance-policy.md` 的 `rights_current` 来源写的是 `S10:binding_set@r12` | docs/10 vs S02 方案 | 本文按"判定归 S15，S02 实时调用"实现；但 S02 方案指向 S10，两者不一致，**必须裁决**（C10 系列同源） |
| C15-6 | `service.yaml` 的 `depends_on` 为 `[S28,S01]`，但 S15 的资格检查会被 S02/S26 高频调用（反向依赖），catalog 未表达 | catalog | 运行协作关系，非启动依赖 |
| C15-7 | docs/10 音色质量门"声线回归"未说明回归的样本与阈值 | docs/10 | 本文用 `regression{similarity_score, evaluated_against, passed}`，阈值由策略配置；需设计方给基线 |
| C15-8 | 克隆音色的授权时长/样本保留期在 docs/14 只有原则，没有具体数值 | docs/14 | 本文只建模字段，不设默认值 |

---

## 11. 实施顺序与未决问题

### 11.1 顺序（**与其他资产服务不同**）

1. **先做 `check-eligibility`，再做内容管理。** 理由：这是其他服务（S02/S26）的硬依赖，且它只依赖 `voice_authorization` 一张表。先把它做对，整个调用链的合规性就能先成立。
2. 契约：`contracts/assets/voice-revision.v1.schema.json` + `voice-eligibility.v1.schema.json`（资格响应形态）。
3. 平台 SDK 复用。
4. 授权状态机 + `revoke`/`suspend`/`resume`/`renew` + `authorization.changed` 事件。**先验 E30。**
5. 音色注册 + revision + canonical。
6. 供应商映射 + 回归（接 S24 的 `model_snapshot` 命名）。
7. 发音词典。
8. 克隆准备（接 S48）。

### 11.2 未决

1. **`check-eligibility` 的调用量级？** 若每次 dispatch 都调，将是全平台最高频的资产接口之一。需要容量规划（docs/03）给出 QPS 目标与缓存策略（即使是"不缓存状态"，也可以缓存**静态部分**如 `expires_at`，但必须在读时重新判定）。
2. **`rights_ref` 指向 S02 的哪个对象？** 本文假设 S02 有 `rights_*` 政策对象。若授权实际由外部合同系统持有，形态要变。
3. **撤销后历史音频的删除由谁执行？** S15 不删。S28 删字节，S27 删交付包。需要一份明确的保留合同（docs/14）。
4. **`effective_at` 的宽限期是否需要？** 见 C15-3。
5. **多语言音色（同一声音多语言）如何建模？** 当前 `language_profile` 是一个 revision。若同一 voice_id 可支持多语言且各语言需要独立回归，可能需要 `voice_revision` 按语言分列。
6. **`voice_kind='licensed_actor'` 是否应强制绑定 `character_ref`？** 演员授权通常限定了角色。当前是软引用。
7. **克隆音色的"声纹样本"保留多久？** 合规上样本是敏感生物特征，通常要求用后即删或短期保留。本文只建模引用，不设保留期。
