# S28 · 素材归档与制品目录 —— 实现方案

状态：`design_only`。本文是实现方案，不是已实现代码。所有供应商仍为 `disabled`。

上游依据：[docs/01 §3](../docs/01-enterprise-architecture.md)（唯一权威表）、[docs/04 §1/§4/§6](../docs/04-data-consistency.md)（版本、事件标准、生命周期与删除）、[docs/10](../docs/10-assets.md)（资产与导入）、[docs/14 §2/§3](../docs/14-enterprise-operations.md)（隔离、导入限制、告警）、[docs/03 §2/§3](../docs/03-deployment-capacity.md)（容量、对象前缀）、[S28 规格](../docs/design-package/services/S28-artifact-ingest.md)。

配套：[S26 生成任务](S26-generation-jobs.md)（归档的唯一发起方之一）、[S35 技术质检](S35-technical-qc.md)（证据归档的消费者）、[S17 连续性](S17-continuity.md)（只读取画面证据）、[S08 版本快照](S08-revision-snapshot.md)（manifest 引用 artifact）。

链上位置：`S26 供应商产出 → S28 归档为不可变 artifact → S08 manifest 引用 → QC/交付/训练按引用读取`。

本文重点回答三个问题：**导入器怎么在拿到字节之前就把攻击面关掉**（§8）、**内容 hash 去重怎么做到不泄漏跨租户存在性**（§4.4）、**删除怎么做到先止血再编排**（§4.8）。

> 说明：`contracts/openapi.v2.yaml` 目前**没有任何 S28 路径**，`contracts/` 下也没有 artifact 的机器可读 schema。本文的请求/响应形态是建议值，落地前必须先进契约。见 §12。

---

## 0. 边界与一句话职责

### 0.1 本服务是唯一写者

| 事实 | 说明 |
|---|---|
| `ArtifactManifest` | 一条制品的逻辑身份、revision、当前内容 hash、`content_class` |
| 内容字节与其位置 | 字节在对象存储的哪个 key、哪个 region、哪个存储类 |
| `content_hash` | 字节的 sha256，**裸 64 位十六进制**，与 `episode-manifest.v2.schema.json` 的 `^[0-9a-f]{64}$` 一致 |
| 导入状态 | 一次导入从接收到发布/失败的全过程（`ArtifactImport`） |
| 代理与派生 | proxy、缩略图、波形、证据拼板等派生制品的身份与位置 |
| 媒体访问权限 | 谁能以什么用途、多长时间拿到短签名下载 URL |
| 保留策略绑定 | 该制品受哪条保留策略约束、是否在 legal hold |
| tombstone | "此制品不再接受新调用"这一事实，以及删除编排的进度 |

判据：任何"**这条媒体的字节在哪里、是什么、还在不在、能不能下载**"的疑问，答案只能来自 S28。

### 0.2 本服务不写

| 事实 | 归属 |
|---|---|
| 素材的业务身份、canonical 版本、权利引用 | S10 / S11–S15 |
| 成片是否被接受、manifest 指针 | S08 |
| 生成任务状态、`SUBMISSION_UNKNOWN` | S26 |
| 供应商能力、URL TTL 契约 | S24（快照）/ S27（执行） |
| 金额、存储费用 | S23（费用归因）；存储计费账单在商业系统 |
| 审计链、保留例外裁决 | S45（S28 只产生审计 outbox 行） |
| 交付包完整性、正式 Master 判定 | S43（S28 只标记 `content_class`） |
| 训练数据集组成 | S48（S28 只提供字节） |

S28 收到的是**一次导入请求**或**一次删除请求**，从不替别的服务判断"这个素材该不该用"。

### 0.3 与相邻服务的判据（防止第二权威）

| 问题 | 只能问 |
|---|---|
| 这个镜头的关键帧是哪个 artifact？ | S08 manifest / S06 ShotSpec 的引用 |
| 这个角色 canonical 图是哪张？ | S11 |
| 这条音色还在授权期吗？ | S15 + S10 rights，**不是 S28** |
| 这个文件能下载吗？ | S28 |
| 这个文件的内容是什么语义？ | 内容所有者的业务记录，S28 只回答 MIME/时长/尺寸/编码这类**可测量事实** |

S28 的元数据里**不存**"这个视频演的是哪场戏""属于哪个角色"这类业务字段——存了就是第二权威。业务归属通过 `artifact` 上的 `owner_ref`（一个不透明的 `service_id/id` 引用）表达，S28 不解析其内容。

### 0.4 明确不做

- 不做转码集群（只做**有限、白名单、无网络**的派生产出，见 §4.6）。
- 不做内容审核判断（技术层拒绝恶意/损坏文件，**不判断创作合规**）。
- 不做素材权利校验（S15/S10）。
- 不做"归档即接受"（接受是 S08 + S02）。
- 不做跨租户聚合、跨租户搜索、跨租户去重统计。
- 不做供应商重试与重新生成（S26 的 Saga；S28 只回明确错误码）。
- 不做长期保存供应商 signed URL（见 §2.5 的加密暂存与到期清除）。

---

## 1. 实体与关键区分

### 1.1 六个实体

| 实体 | 是什么 | 不是什么 |
|---|---|---|
| `artifact` | 逻辑身份（"第三集 shot_3_2_1 的成片"），可有多 revision | 不是字节；不是业务资产 |
| `artifact_revision` | 某一次发布的不可变记录（hash + 位置 + 可测量元数据） | 不可修改；回滚 = 新 revision |
| `content_blob` | 租户内按 `content_hash` 去重的物理字节记录 | 不是授权单元；不是业务对象 |
| `artifact_import` | 一次导入命令的执行记录（含状态机） | 不是制品；失败导入不产生 artifact |
| `artifact_derivative` | 从某个 revision 派生的 proxy/缩略图/波形/拼板 | 不是独立内容事实，`derived_from` 指向源 revision |
| `tombstone` / `deletion_job` | "停止新调用"与"删除编排" | 不是数据库删除动作本身 |

### 1.2 `artifact_id` / `revision` / `content_hash` 三个坐标

```
artifact_id   业务侧引用的稳定身份      例：art_01J9...
revision      该身份下第几次发布，>=1   同一 artifact 的两次不同字节
content_hash  sha256(字节)，裸 64 hex   跨 artifact 可相同（去重）
```

下游引用统一是 `{service_id:"S28", id, revision, content_hash}`，与 `episode-manifest.v2.schema.json` 的 `accepted_artifact_ref` 完全同构。**`revision` 只在 `artifact_id` 内有意义**（docs/04 §1）。

两条硬规则：

1. 一个 `(artifact_id, revision)` 一旦发布，其 `content_hash` 与 `storage_key` **永不变更**。
2. 同一个 `(tenant_id, content_hash)` 的字节物理上只有一份；不同 `artifact_id` 可以共享同一 `content_blob`。

### 1.3 `content_blob` 与去重边界（E28 的核心）

去重发生在 `content_blob`，键是 `(tenant_id, content_hash)`。**没有全局去重表**。

理由：全局表的存在性查询本身就是泄漏通道——攻击者上传一个已知 hash 的文件，若返回"已存在"，即证明某个租户持有该素材（可能是未公开的剧集画面）。docs/04 §6 原文："S28 内容 hash 去重只在租户/授权边界内进行，禁止通过跨租户 hash 存在性泄漏素材。"

租户内还有"授权边界"（项目 / series 级访问范围）。因此：

- **物理去重**：`content_blob` 租户级共享，不暴露给调用方。
- **`dedup_hit` 可见性**：只有当调用方对已有 artifact 所属 scope 有读权限时，响应里才允许出现 `dedup: {hit: true, artifact_id}`。否则一律按"新导入"返回，**不携带任何已存在对象信息**。
- **错误文案**：任何路径都不得出现 "already imported by tenant X / project Y"。

### 1.4 "归档完成" ≠ "可访问" ≠ "可被引用"

三个状态容易被实现者合并，必须分开：

| 阶段 | 含义 | 谁能用 |
|---|---|---|
| `PUBLISHED` | manifest 已原子发布，hash 可验 | S08 可以引用 |
| `ACCESSIBLE` | 存在有效保留策略绑定、未 tombstone | 可以申请访问 ticket |
| `REFERENCEABLE` | 未 tombstone 且 `content_class` 允许该类下游引用 | QC/交付/训练可读 |

一条制品可以是 `PUBLISHED` 但立即被 tombstone（比如导入后判定为误传），此时 S08 已经引用的 manifest 会指向一个不可访问对象——这时下游必须得到明确的 `ARTIFACT_TOMBSTONED`，而**不是 404**（404 的含义是"从未存在"，会诱导上层把它当成数据丢失）。

### 1.5 `content_class` 闭集

```text
generated_video    供应商生成的视频主产物
generated_image    供应商生成的图像
generated_audio    配音/音效/音乐的生成产物
rendered_master    本地渲染的正式成片（S41/S42 产出）
proxy              播放用低码率代理（派生）
thumbnail          缩略图/拼板（派生）
waveform           音频波形图（派生，S35/S37 证据）
subtitle_track     字幕文件（S38 产出）
audio_stem         分轨音频（S42 产出）
uploaded_reference 用户上传的参考素材
evidence           质检/审核证据包（S35–S37 固化）
model_archive      供应商原始返回归档（以证据用途保存）
```

闭集变更 = 全体发版，几乎不做。新增类别走 §12 的既有兼容流程（可选字段 + 双读）。

### 1.6 预览 / 正式 Master / 缺素材（E37）

E37 要求"包类型明确，正式 Master 满足完整性门"。S28 只提供**事实**：

- `content_class` 区分 `proxy` / `rendered_master` / `generated_video`。
- `is_preview: bool` 只对派生制品为 true；**正式 Master 的判定不在 S28**。
- "缺素材"表现为 S43/交付编排在引用图里找不到所需 `artifact_ref`，或取到 `ARTIFACT_TOMBSTONED` / `ARTIFACT_NOT_AVAILABLE`。S28 **不伪造占位文件**。

S28 绝不因为"交付要一个 4 秒视频"而返回一个 3.8 秒的 proxy 顶替。宁可报缺口。

---

## 2. 数据模型

数据库 `artifact_ingest`，Postgres。所有表带 `tenant_id`，所有查询强制带 `tenant_id` 谓词；跨租户访问返回 **404**（不是 403，见 §8.5）。

### 2.1 `artifact` —— 逻辑身份

```sql
CREATE TABLE artifact (
  artifact_id      text PRIMARY KEY,                  -- art_<ulid>
  tenant_id        text NOT NULL,
  home_region      text NOT NULL,
  home_cell        text NOT NULL,

  owner_ref        jsonb,                             -- {"service_id":"S06","id":"shot_..."} 不透明
  content_class    text NOT NULL
    CHECK (content_class IN ('generated_video','generated_image','generated_audio',
                             'rendered_master','proxy','thumbnail','waveform',
                             'subtitle_track','audio_stem','uploaded_reference',
                             'evidence','model_archive')),

  current_revision integer NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
  state            text NOT NULL DEFAULT 'PENDING_PUBLISH'
    CHECK (state IN ('PENDING_PUBLISH','AVAILABLE','TOMBSTONED','DELETED')),

  retention_policy_ref text,                          -- 指向 retention_binding
  legal_hold       boolean NOT NULL DEFAULT false,

  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  tombstoned_at    timestamptz,
  deleted_at       timestamptz,

  CONSTRAINT artifact_tombstone_consistent
    CHECK (state <> 'TOMBSTONED' OR tombstoned_at IS NOT NULL),
  CONSTRAINT artifact_deleted_consistent
    CHECK (state <> 'DELETED' OR deleted_at IS NOT NULL)
);

CREATE INDEX ix_artifact_tenant_class ON artifact (tenant_id, content_class, created_at DESC);
CREATE INDEX ix_artifact_owner ON artifact (tenant_id, owner_ref);
CREATE INDEX ix_artifact_tombstone_pending ON artifact (tenant_id, tombstoned_at)
  WHERE state = 'TOMBSTONED';
```

`current_revision = 0` 表示尚未发布，此时 `artifact` 只是导入期间的占位；导入失败会直接删除该行（不产生用户可见 artifact）。

### 2.2 `artifact_revision` —— 不可变版本

```sql
CREATE TABLE artifact_revision (
  artifact_id      text NOT NULL REFERENCES artifact(artifact_id),
  revision         integer NOT NULL CHECK (revision >= 1),
  tenant_id        text NOT NULL,
  content_hash     char(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  size_bytes       bigint NOT NULL CHECK (size_bytes >= 0),

  -- 可测量元数据：允许为空（未探测），但一旦写入不可修改
  mime_type        text,
  container        text,                              -- mp4|mov|mkv|wav|png|jpeg|webvtt...
  duration_ms      bigint CHECK (duration_ms IS NULL OR duration_ms >= 0),
  width_px         integer CHECK (width_px IS NULL OR width_px > 0),
  height_px        integer CHECK (height_px IS NULL OR height_px > 0),
  frame_rate_num   integer CHECK (frame_rate_num IS NULL OR frame_rate_num > 0),
  frame_rate_den   integer CHECK (frame_rate_den IS NULL OR frame_rate_den > 0),
  audio_channels   smallint,
  sample_rate_hz   integer,

  storage_key      text NOT NULL,
  storage_class    text NOT NULL DEFAULT 'STANDARD'
    CHECK (storage_class IN ('STANDARD','INFREQUENT','ARCHIVE')),
  storage_region   text NOT NULL,

  source_import_id text NOT NULL,                     -- 产生本 revision 的导入
  probe_ok         boolean NOT NULL DEFAULT false,
  published_at     timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (artifact_id, revision),
  CONSTRAINT artifact_revision_hash_uniq UNIQUE (artifact_id, content_hash)
);

CREATE INDEX ix_rev_hash ON artifact_revision (tenant_id, content_hash);
CREATE INDEX ix_rev_unprobed ON artifact_revision (tenant_id, published_at)
  WHERE probe_ok = false;
```

`(artifact_id, content_hash)` 唯一：同一身份的同一份字节不会有两个 revision，重复导入同一内容到同一 artifact 是幂等的（见 §7.2）。

### 2.3 `content_blob` —— 租户内去重的物理字节

```sql
CREATE TABLE content_blob (
  blob_id       text PRIMARY KEY,                     -- blob_<ulid>
  tenant_id     text NOT NULL,
  content_hash  char(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  size_bytes    bigint NOT NULL CHECK (size_bytes >= 0),
  storage_key   text NOT NULL,
  storage_region text NOT NULL,
  storage_class text NOT NULL DEFAULT 'STANDARD',
  state         text NOT NULL DEFAULT 'LIVE'
    CHECK (state IN ('LIVE','PENDING_DELETE','DELETED')),
  ref_count     integer NOT NULL DEFAULT 0 CHECK (ref_count >= 0),
  created_at    timestamptz NOT NULL DEFAULT now(),
  verified_at   timestamptz,

  CONSTRAINT blob_tenant_hash_uniq UNIQUE (tenant_id, content_hash)
);

CREATE INDEX ix_blob_pending_delete ON content_blob (tenant_id, created_at)
  WHERE state = 'PENDING_DELETE';
```

**没有 `content_hash` 的全局唯一索引，也没有任何跨租户查询路径。** 这是 E28 的数据库级落点。

`ref_count` 在 `artifact_revision` 插入/删除时同事务增减；`ref_count = 0` 的 blob 进入 `PENDING_DELETE`，由回收作业在保留窗口后删字节。

### 2.4 `artifact_import` —— 导入命令记录

```sql
CREATE TABLE artifact_import (
  import_id        text PRIMARY KEY,                  -- imp_<ulid>
  tenant_id        text NOT NULL,
  command_id       text NOT NULL,
  state            text NOT NULL,                     -- §3.1

  content_class    text NOT NULL,
  target_artifact_id text,                            -- 指定身份时（新 revision）
  expected_sha256  char(64) CHECK (expected_sha256 IS NULL OR expected_sha256 ~ '^[0-9a-f]{64}$'),

  declared_size_bytes bigint CHECK (declared_size_bytes IS NULL OR declared_size_bytes >= 0),
  observed_size_bytes bigint CHECK (observed_size_bytes IS NULL OR observed_size_bytes >= 0),
  actual_sha256    char(64) CHECK (actual_sha256 IS NULL OR actual_sha256 ~ '^[0-9a-f]{64}$'),

  mime_declared    text,
  mime_detected    text,
  probe_result     jsonb,                             -- 沙箱探测输出（结构见 §4.2）

  source_kind      text NOT NULL
    CHECK (source_kind IN ('provider_url','upload_session','internal_object','derived')),
  source_ref       text,                              -- provider_job_id / upload_id / 派生源
  url_host         text,                              -- 仅记录 host，记录前已过 allowlist
  url_expires_at   timestamptz,                       -- 调用方声明的供应商 TTL
  fetch_started_at timestamptz,
  fetch_finished_at timestamptz,

  reject_code      text,                              -- SECURITY_* / FORMAT_* / HASH_*
  reject_detail    text,                              -- 不含 URL 与签名参数
  attempts         integer NOT NULL DEFAULT 0,
  next_action_at   timestamptz,

  staging_key      text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  terminal_at      timestamptz,

  CONSTRAINT import_command_uniq UNIQUE (tenant_id, command_id),
  CONSTRAINT import_size_cap CHECK (observed_size_bytes IS NULL OR observed_size_bytes <= 214748364800)
);

CREATE INDEX ix_import_due ON artifact_import (next_action_at)
  WHERE terminal_at IS NULL;
CREATE INDEX ix_import_ttl ON artifact_import (url_expires_at)
  WHERE terminal_at IS NULL AND url_expires_at IS NOT NULL;
CREATE INDEX ix_import_staging ON artifact_import (staging_key)
  WHERE terminal_at IS NULL;
```

200 GiB 的 CHECK 是**硬上限的兜底**，真正的大小限制在 §8.2 的流式读取里（先判 `Content-Length`，再边读边计数，超限立刻中断，不等读完）。

### 2.5 `import_source` —— 供应商 signed URL 的加密暂存

供应商 URL 里有签名参数，属于临时凭证，**不能长期留在普通列或日志里**（docs/10："禁止把供应商可访问URL长期写入公共Prompt日志"）。

```sql
CREATE TABLE import_source (
  import_id        text PRIMARY KEY REFERENCES artifact_import(import_id),
  tenant_id        text NOT NULL,
  url_ciphertext   bytea NOT NULL,                    -- KMS envelope encryption
  url_key_id       text NOT NULL,
  url_host         text NOT NULL,
  url_expires_at   timestamptz NOT NULL,
  purge_after      timestamptz NOT NULL,              -- = url_expires_at + 24h
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_import_source_purge ON import_source (purge_after);
```

- 只有导入 worker 的 ServiceAccount 有该表的读权限；API 进程没有。
- 到期后由作业把 `url_ciphertext` 覆写为 `NULL`（列可空化由迁移处理）并删除行。
- 日志里只出现 `url_host`，不出现 query string。

### 2.6 `artifact_derivative`

```sql
CREATE TABLE artifact_derivative (
  derivative_id    text PRIMARY KEY,
  tenant_id        text NOT NULL,
  source_artifact_id text NOT NULL,
  source_revision  integer NOT NULL,
  kind             text NOT NULL
    CHECK (kind IN ('proxy','thumbnail','waveform','contact_sheet')),
  artifact_id      text NOT NULL REFERENCES artifact(artifact_id),  -- 派生结果本身也是 artifact
  state            text NOT NULL DEFAULT 'PENDING'
    CHECK (state IN ('PENDING','READY','FAILED','SKIPPED')),
  reason_code      text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT derivative_uniq UNIQUE (tenant_id, source_artifact_id, source_revision, kind)
);

CREATE INDEX ix_derivative_pending ON artifact_derivative (tenant_id, created_at)
  WHERE state = 'PENDING';
```

派生结果**也是 artifact**（有 hash、有 revision、可被引用），因此下游不需要第二套寻址规则。`derivative` 表只是"派生关系"这一条事实。

### 2.7 `media_access_ticket` —— 访问审计（存 ticket，不存 URL）

```sql
CREATE TABLE media_access_ticket (
  ticket_id        text PRIMARY KEY,
  tenant_id        text NOT NULL,
  artifact_id      text NOT NULL,
  revision         integer NOT NULL,
  principal_id     text NOT NULL,
  principal_type   text NOT NULL
    CHECK (principal_type IN ('human','service','api_client','mcp_client')),
  purpose          text NOT NULL,                     -- playback|qc|delivery|train|audit|download
  scope_ref        jsonb,                             -- 项目/series/交付包引用
  issued_at        timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL,
  max_bytes        bigint,
  revoked_at       timestamptz,

  CONSTRAINT ticket_ttl CHECK (expires_at > issued_at),
  CONSTRAINT ticket_ttl_cap CHECK (expires_at <= issued_at + interval '24 hours')
);

CREATE INDEX ix_ticket_active ON media_access_ticket (tenant_id, artifact_id, expires_at)
  WHERE revoked_at IS NULL;
```

签发的 URL 本身**不入库**，只在响应里返回。ticket 是审计对象：谁在什么时候为哪个用途申请了访问；tombstone 时按 `artifact_id` 批量 `revoked_at`（见 §4.8 第 1 步）。

### 2.8 `retention_binding`

```sql
CREATE TABLE retention_binding (
  binding_id       text PRIMARY KEY,
  tenant_id        text NOT NULL,
  artifact_id      text NOT NULL REFERENCES artifact(artifact_id),
  policy_ref       text NOT NULL,                     -- 客户合同/地区规则定义
  retain_until     timestamptz,                       -- NULL = 跟随策略
  legal_hold       boolean NOT NULL DEFAULT false,
  hold_reason      text,
  hold_ref         text,                              -- 法务单号（S45 引用）
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT retention_one_per_artifact UNIQUE (tenant_id, artifact_id),
  CONSTRAINT retention_hold_consistent
    CHECK (legal_hold = false OR hold_ref IS NOT NULL)
);
```

保留策略的**内容**不在 S28（属于客户合同与地区规则），S28 只持有绑定与到期时间。

### 2.9 tombstone 与删除编排

```sql
CREATE TABLE artifact_tombstone (
  artifact_id      text PRIMARY KEY REFERENCES artifact(artifact_id),
  tenant_id        text NOT NULL,
  reason_code      text NOT NULL,     -- USER_REQUEST|RETENTION_EXPIRED|LEGAL|SECURITY|ORPHAN
  requested_by     text NOT NULL,     -- principal id
  requested_via    text NOT NULL,     -- command_id
  effective_at     timestamptz NOT NULL DEFAULT now(),
  note             text
);

CREATE TABLE deletion_job (
  deletion_id      text PRIMARY KEY,
  tenant_id        text NOT NULL,
  artifact_id      text NOT NULL REFERENCES artifact(artifact_id),
  state            text NOT NULL,     -- §3.4
  reference_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  blockers         jsonb NOT NULL DEFAULT '[]'::jsonb,
  attempts         integer NOT NULL DEFAULT 0,
  next_action_at   timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  completed_at     timestamptz,
  CONSTRAINT deletion_one_active UNIQUE (tenant_id, artifact_id)
);
CREATE INDEX ix_deletion_due ON deletion_job (next_action_at)
  WHERE completed_at IS NULL;

CREATE TABLE deletion_step (
  deletion_id      text NOT NULL REFERENCES deletion_job(deletion_id),
  step             text NOT NULL
    CHECK (step IN ('TOMBSTONE','ACCESS_REVOKE','REFERENCE_CHECK','BYTES_DELETE',
                    'INDEX_PURGE','BACKUP_EXPIRY','AUDIT_NOTIFY')),
  state            text NOT NULL CHECK (state IN ('PENDING','DONE','BLOCKED','SKIPPED')),
  detail           jsonb,
  occurred_at      timestamptz,
  PRIMARY KEY (deletion_id, step)
);
```

`deletion_job` 每个 artifact 只有一条活跃记录（`UNIQUE`），删除编排是**单写者**的（§7.5）。

### 2.10 幂等、Outbox、Inbox

```sql
CREATE TABLE idempotency_record (
  tenant_id      text NOT NULL,
  command_id     text NOT NULL,
  request_hash   char(64) NOT NULL,
  command_type   text NOT NULL,     -- artifact.import / artifact.tombstone / artifact.access
  state          text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code  integer,
  response_body  jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL DEFAULT now() + interval '90 days',
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE outbox (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,             -- artifact_id
  aggregate_sequence bigint NOT NULL,     -- = revision（删除事件用 tombstone 序号）
  event_type         text NOT NULL,       -- 目录里目前只有 artifact.ingested
  partition_key      text NOT NULL,       -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;

CREATE TABLE inbox (
  consumer     text NOT NULL,
  event_id     text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

导入的 `request_hash` 基于**请求体**，不含 URL：URL 会过期、会换，重试归档时 S26 会换新 URL 并换新 `command_id`（S26 §8.3）。同一 `command_id` 的 URL 换了但 body 变了 → 409 `IDEMPOTENCY_KEY_REUSED`，这是正确的：换 URL 就是换命令。

### 2.11 不建的表

- **不建跨租户 hash 索引/物化视图**。任何"全平台有多少个相同 hash"的查询都不提供。
- **不建 `artifact_projection`**。S28 自己是权威。
- **不存业务归属的展开字段**（角色、场次、镜头名）。`owner_ref` 是不透明引用。
- **不存供应商 signed URL 明文**（见 §2.5）。
- **不存文件的"内容描述"**（VLM 生成的 caption）。那是 S36 的证据，S28 只存证据 artifact。

---

## 3. 状态机

### 3.1 `ArtifactImport`

```text
ACCEPTED
  ├─ URL_REJECTED            （scheme/host/DNS 校验失败，未出网）
  └─ URL_CHECKED
       ├─ FETCHING
       │    ├─ FETCH_FAILED           （网络/超时/5xx，可重试）
       │    ├─ FETCH_BLOCKED          （重定向到内网/元数据、超大小、超时）
       │    └─ STAGED                  （字节已落到隔离区，未校验）
       ├─ URL_EXPIRED                 （调用方声明的 expires_at 已过，未出网）
       └─ INSPECTING
            ├─ REJECTED_SECURITY       （恶意/格式/探测失败）
            ├─ REJECTED_SIZE
            ├─ HASH_MISMATCH           （expected_sha256 不符，可重试一次）
            └─ PUBLISHING
                 ├─ PUBLISHED           （manifest 原子发布 + outbox）
                 └─ PUBLISH_FAILED      （DB/存储失败，可重试，不重复计费）
```

终态：`PUBLISHED` / `REJECTED_SECURITY` / `REJECTED_SIZE` / `HASH_MISMATCH` / `URL_REJECTED` / `URL_EXPIRED` / `FETCH_FAILED` / `FETCH_BLOCKED` / `PUBLISH_FAILED`。

**注意 `FETCH_FAILED` 是可重试的终态**：状态机上是终态（本次命令结束），但同一 `command_id` 的重投允许重新进入 `FETCHING`。区分"命令终态"与"制品终态"是必要的——S26 需要看到明确的"这次归档失败，可以再试"。

转移表（节选，完整表在实现时落 `artifact_import_transition`）：

| from | to | 触发 | 条件 |
|---|---|---|---|
| `ACCEPTED` | `URL_CHECKED` | 服务内部 | scheme/host 白名单通过，DNS 已解析并固定 |
| `ACCEPTED` | `URL_REJECTED` | 服务内部 | 任一项校验失败 |
| `URL_CHECKED` | `URL_EXPIRED` | 服务内部 | `url_expires_at <= now()` |
| `URL_CHECKED` | `FETCHING` | worker | 取得该 import 的 `advisory lock` |
| `FETCHING` | `STAGED` | worker | 字节写入隔离区，`observed_size_bytes` 写入 |
| `FETCHING` | `FETCH_BLOCKED` | worker | 重定向/大小/连接超时 |
| `STAGED` | `INSPECTING` | sandbox worker | 沙箱启动成功 |
| `INSPECTING` | `REJECTED_SECURITY` | sandbox | MIME 不符/解析失败/炸弹/超时/崩溃 |
| `INSPECTING` | `PUBLISHING` | 服务内部 | `actual_sha256` 与 expected 一致（或未提供） |
| `INSPECTING` | `HASH_MISMATCH` | 服务内部 | 不符且 `attempts >= 1` |
| `PUBLISHING` | `PUBLISHED` | 服务内部 | 同一事务写 revision + blob + outbox |
| `PUBLISHING` | `PUBLISH_FAILED` | 服务内部 | 事务失败 |

### 3.2 `Artifact`

```text
PENDING_PUBLISH ──publish──► AVAILABLE ──tombstone──► TOMBSTONED ──delete──► DELETED
                                 ▲                         │
                                 └────(禁止：不可回退)──────┘
```

- `TOMBSTONED` **不可回到** `AVAILABLE`。误 tombstone 只能新建 artifact 重新导入（保留审计）。
- `DELETED` 表示字节已按保留策略销毁，行保留作最小证明（`content_hash` 可以保留，`storage_key` 置 `NULL`——hash 本身不是媒体内容）。

### 3.3 `ArtifactDerivative`

```text
PENDING ──► READY
   ├──────► FAILED     （沙箱/编码失败，记录 reason_code，不重试超过 3 次）
   └──────► SKIPPED    （源 revision 不支持该派生，例如音频没有画面）
```

### 3.4 `DeletionJob`

```text
REQUESTED
  → TOMBSTONED            （artifact_tombstone 已写，新调用已拒）
  → ACCESS_REVOKE         （未到期 ticket 全部 revoked）
  → REFERENCE_CHECK       （向引用方查询，见 §4.8）
       ├─ BLOCKED_REFERENCES  （仍被引用，等待或人工）
       └─ REFERENCE_CLEAR
  → BYTES_DELETE          （blob ref_count 归零后删对象）
  → INDEX_PURGE           （搜索/向量索引清理通知）
  → BACKUP_EXPIRY         （等待备份保留期到期，异步）
  → COMPLETE
```

`LEGAL_HOLD` 可以在 `REFERENCE_CHECK` 之后、`BYTES_DELETE` 之前把 job 打回等待态；`legal_hold=true` 时**永远不进入** `BYTES_DELETE`。

### 3.5 非法转移（必须被数据库或代码拒绝）

| 非法转移 | 拒绝方式 |
|---|---|
| `TOMBSTONED → AVAILABLE` | 状态机代码 + 更新语句 `WHERE state='AVAILABLE'` |
| `PUBLISHED → PUBLISHING` | `artifact_revision` 主键冲突 |
| `DELETED → *` | `WHERE state <> 'DELETED'` |
| `BLOCKED_REFERENCES → BYTES_DELETE` | 必须有 `reference_evidence` 全绿 |
| `legal_hold=true → BYTES_DELETE` | 删除步骤显式检查 |

---

## 4. 关键流程

### 4.1 导入主流程

```text
1. 认证与租户：从 mTLS/workload identity 取 tenant，header 只做一致性校验（不符 403）
2. 幂等：INSERT idempotency_record (tenant, command_id) IN_PROGRESS
3. 入参校验：content_class 闭集、expected_sha256 格式、source_kind
4. URL 安全校验（未出网）：scheme=https、host allowlist、解析并固定 IP、禁私网/环回/链路本地/元数据
5. 持久化 ArtifactImport + import_source（加密 URL），提交
6. 返回 202 + import_id（此时还没有 artifact_id）
7. worker：取 import，advisory lock (tenant_id, import_id)
8. FETCHING：直连固定 IP，逐跳校验重定向，流式落隔离区，边读边计数
9. STAGED：写 observed_size_bytes；隔离区对象前缀独立（`staging/`），不可公开访问
10. INSPECTING：非特权无网络沙箱探测（§4.2），产出 MIME/时长/尺寸/编码
11. 计算 actual_sha256（在沙箱外的受信进程，读隔离区）
12. PUBLISHING（单事务）：
      a. upsert content_blob (tenant, content_hash)
      b. insert artifact_revision
      c. update artifact.current_revision / state='AVAILABLE'
      d. blob.ref_count += 1
      e. insert outbox('artifact.ingested', aggregate_seq = revision)
      f. 更新 import 终态
13. 隔离区对象删除 / 转正（copy 到正式前缀后删 staging）
14. 事件由 outbox relay 发出
```

**第 12 步是唯一的发布点。** 第 12 步之前崩溃，隔离区留下孤儿字节，由 §4.9 回收；第 12 步之后崩溃，outbox 未发出会由 relay 重发，不会产生重复 artifact（幂等键兜底）。

### 4.2 安全校验链（未出网就要挡掉的）

```text
scheme       必须 https；http/file/gopher/data/fd 一律拒绝
host         allowlist 为空 = 全部拒绝（fail closed）；不允许 IP 字面量
DNS          由 S28 自己的解析器解析；解析结果逐条检查：
               ├ 私网 10/8、172.16/12、192.168/16
               ├ 环回 127/8、::1
               ├ 链路本地 169.254/16（含 169.254.169.254 云元数据）
               ├ 唯一本地 fc00::/7、多播、0.0.0.0/8
               └ 解析失败
             通过后固定解析结果（IP pinning），连接时不再二次解析
redirect     最多 3 跳；每一跳重新做上述全部检查；禁止跳到不同 host（除非在新 allowlist）
TLS          校验证书与 SNI；不允许关闭校验；不允许走 HTTP_PROXY 环境变量
timeout      连接 5s，首字节 10s，总下载按 size 上限 / 最小吞吐下限（见下）
size         先看 Content-Length（若有）；边读边计数；超过 content_class 上限立即中断
rate         单 import 最大吞吐上限，防止慢速攻击占满 worker
```

`content_class` 大小上限是**配置**，不是硬编码：

| class | 默认上限 | 说明 |
|---|---:|---|
| `generated_video` | 8 GiB | 4–10 秒 8Mbps 远小于此 |
| `generated_image` | 200 MiB | |
| `generated_audio` | 1 GiB | |
| `rendered_master` | 64 GiB | 正式 Master 可能很长 |
| `uploaded_reference` | 2 GiB | 用户上传 |
| 其余 | 2 GiB | |

超过上限 → `REJECTED_SIZE`，**不进入解码**。

### 4.3 hash 校验

- 调用方给 `expected_sha256`：计算后比对。不符 → `HASH_MISMATCH`，同一 `command_id` 允许重试一次；仍不符 → 终态，S26 转人工（S26 §8.2）。
- 调用方不给：S28 计算后写入 `actual_sha256` 并在 `artifact.ingested` 里回传，调用方据此校验。
- hash 计算**在沙箱外**的受信进程里做，读的是隔离区已完成写入的文件；沙箱只做"解析与探测"，不产出 hash。

> 这一点容易写反：让沙箱进程算 hash，等于让可能被攻破的进程决定"字节是什么"。hash 必须是受信路径上的字节流算出来的。

### 4.4 租户内去重（E28）

```sql
BEGIN;
  INSERT INTO content_blob (blob_id, tenant_id, content_hash, size_bytes, storage_key, ...)
  VALUES (...)
  ON CONFLICT (tenant_id, content_hash) DO NOTHING;

  SELECT blob_id, storage_key, ref_count
    FROM content_blob
   WHERE tenant_id = $tenant AND content_hash = $hash
   FOR UPDATE;
  -- 命中则复用 storage_key，不重复写对象；新字节直接丢弃
COMMIT;
```

并发两个导入同一内容：`ON CONFLICT DO NOTHING` + `FOR UPDATE` 保证只有一个 blob 行，两者都能拿到同一 `storage_key`；后到的那个丢弃自己的 staging 对象。

**可见性规则**（§1.3 的落地）：

```text
dedup_hit = existing_blob exists
response.dedup.hit = dedup_hit AND caller_can_read(existing_artifact_of_blob, caller_context)
response.dedup.artifact_id = 仅当上一行为 true 时出现
```

`caller_can_read` 是本服务内的**本地授权判断**（依据 `media_access` 策略 + 调用方 tenant/scope），不是去问 S01——S01 不知道项目级 scope。调用方在导入时已经在 `scope_ref` 里声明归属，两处 scope 相同才允许暴露。

**跨租户**：唯一索引前缀是 `tenant_id`，另一个租户的同 hash 导入会插入**自己的** blob 行、写自己的对象。磁盘上可能有重复字节，这是刻意的代价——用存储换不泄漏。

### 4.5 供应商 URL 过期（E23，S28 侧）

S28 只负责**如实报告**，不做重生成决策：

| 观测 | S28 返回 |
|---|---|
| `provider_url_expires_at <= now()` 且尚未取 | `URL_EXPIRED`（`retryable:false`，`renewable:true`） |
| fetch 收到 401/403/410 | `FETCH_BLOCKED` + `reason=DETAIL_URL_REJECTED`，附 `retryable:true`、`renewable:true` |
| fetch 超时/DNS 失败 | `FETCH_FAILED`，`retryable:true` |
| 下载成功但 hash 不符 | `HASH_MISMATCH` |
| 解码/格式拒绝 | `REJECTED_SECURITY`，`retryable:false` |

`renewable:true` 是给 S26 的信号：去供应商换新 URL，**用新 `command_id`** 再调一次（S26 §8.3）。S28 **永远不自己去找供应商**——它没有供应商凭证，也不应该知道渠道。

TTL 逼近告警：`url_expires_at - now() < 15min` 且未完成 → 提高队列优先级并告警（docs/14 §3 "下载TTL逼近"）。这条在 S28 侧是 `ingest_ttl_warning`，与 S26 的同名巡检互补：S26 看任务侧，S28 看字节侧，任一触发都说明有风险。

### 4.6 派生与代理

派生是**异步、可跳过**的：

```text
PUBLISHED(generated_video, rendered_master)
  → 入队 artifact_derivative(kind=proxy)
  → 沙箱内转码（白名单 preset，参数由服务端生成）
  → 产出新 artifact(content_class='proxy') + 关系行
```

硬约束：

1. **派生失败不影响源 artifact 的可用性**。源已 `AVAILABLE`；proxy 只是播放优化。
2. 转码参数**不允许来自用户输入**。用户能选的只有枚举 preset（`p1080`/`p720`/`p480`），filter 字符串由代码生成（§8.2 命令注入防线）。
3. 派生有独立预算与并发上限（媒体 worker pool），不挤占导入 worker。
4. `derived_from` 记录源 `(artifact_id, revision)`；源 revision 不可变，所以派生不会"跟着源变"。

### 4.7 访问与短签名

```text
调用方 POST /media-access-tickets
  { artifact_ref, purpose, scope_ref, ttl_seconds<=86400, max_bytes? }
  → 校验：artifact AVAILABLE、未 tombstone、调用方对该 scope 有读权限、
          目的在允许集合（playback/qc/delivery/train/audit/download）
  → 写 media_access_ticket
  → 返回 { ticket_id, url, expires_at }
```

- URL 是对象存储的短签名 URL，TTL 与 ticket 一致，**不超过 24 小时**。
- 不提供目录列举接口（不发 bucket listing 签名）。
- 下载时对象存储侧再做一次对象级检查（docs/14 §2："S3路径不等于授权"）。
- `purpose=train` 额外要求调用方是 S48 的 workload identity，且 artifact 未在 legal hold 之外的训练禁止集（训练引用检查在 S48，S28 只记录 purpose）。

### 4.8 删除编排（docs/04 §6）

顺序不能变：**先 tombstone 止血，再编排删除**。

```text
Step 1 TOMBSTONE
  · 单事务：artifact.state = 'TOMBSTONED'，写 artifact_tombstone
  · 同事务：该 artifact 所有未到期 media_access_ticket.revoked_at = now()
  · 同事务：outbox 写 tombstone 事件（见 §12 的契约缺口）
  · 此后：任何新的 access ticket / 导入新 revision / 下游读取一律拒绝
  · 事务提交即生效，不等事件传播

Step 2 ACCESS_REVOKE
  · 复核没有漏掉的 ticket（并发签发竞态，见 §7.4）
  · 已签发但未使用的 URL 无法回收 → 记录其最晚过期时间作为删除窗口下界

Step 3 REFERENCE_CHECK（只读询问，不跨库）
  · 向引用方查询（S08 manifest / S43 交付包 / S48 训练集 / S45 审计保留）
  · 每次查询带 deadline；超时 = 未确认 = 阻塞
  · 结果写 deletion_job.reference_evidence：
      {"S08":{"checked":true,"references":0,"watermark":"..."},
       "S43":{"checked":false,"reason":"timeout"}}
  · 任一未确认 → BLOCKED_REFERENCES，指数退避重试，不自动放行

Step 4 BYTES_DELETE
  · blob.ref_count 减一；归零则 state='PENDING_DELETE'
  · 保留窗口（默认 7 天，配置）后删对象
  · 删的是对象；content_blob 行保留，state='DELETED'，storage_key 置空
  · artifact_revision 行的 storage_key 置空，content_hash 保留（最小证明）

Step 5 INDEX_PURGE
  · 通知索引方（S10 检索投影、S44 报表、向量索引）该 artifact 已删除
  · 通知是命令/事件，不是跨库删除

Step 6 BACKUP_EXPIRY
  · 备份按保留期自然过期；S28 记录"备份到期时间"供审计
  · 不主动删除备份（备份系统的权威在基础设施）

Step 7 COMPLETE
  · artifact.state='DELETED'
  · 审计事件发给 S45
```

三条硬规则：

1. **`REFERENCE_CHECK` 超时不是"没有引用"**。fail closed。
2. **tombstone 与读取的竞态必须由数据库裁决**：读取路径在事务里 `SELECT ... FOR SHARE` artifact 行并检查 `state`；tombstone 用 `UPDATE ... WHERE state='AVAILABLE' RETURNING`。见 §7.4。
3. **删除不删除审计与财务的最小证明**（docs/04 §6、E39）：hash、时间、谁请求、哪些引用方确认过，这些留；媒体原文不留。

### 4.9 崩溃恢复与暂存孤儿

| 崩溃点 | 后果 | 恢复 |
|---|---|---|
| 写完 staging、未发布 | 隔离区有孤儿字节 | `orphan_staging` 作业：staging key 不在任何活跃 import 上且超过 2h → 删除 |
| 发布事务提交后、outbox 未发 | 制品可读但下游不知道 | outbox relay 重发；下游 Inbox 去重 |
| 发布事务回滚 | import 停在 `PUBLISHING` | 重试同 `command_id`，幂等记录是 `IN_PROGRESS` → 继续；超过 30min 置 `PUBLISH_FAILED` |
| blob 行已插、对象未写 | 后续去重会指向不存在的对象 | 发布事务里**先写对象再写 DB**；对象写失败 → 整个事务不提交。若对象写成功而事务回滚 → 孤儿对象由 GC 按 blob 不存在回收 |
| 删除作业中途崩溃 | 停在某步 | 每步幂等，见 §7.5 |

**"绝不把 DB 提交放在对象写入之前"** 是这里唯一需要背下来的一条：DB 里有一条指向不存在对象的记录，比留一个孤儿对象难查得多。

### 4.10 S01 依赖

S28 `depends_on: [S01]`，但依赖面很窄：

| 用在 | 方式 | 失败语义 |
|---|---|---|
| 导入命令的调用方身份 | S01 `GET /authorization-context`（action=`artifact.import`，scope=owner_ref） | S01 不可达 → **503 `AUTH_DEPENDENCY_UNAVAILABLE`**，不接收导入 |
| 访问 ticket | 同上（action=`artifact.read`，purpose 参与属性） | 同上 |
| tombstone/删除 | 同上（action=`artifact.delete`，高权限） | 同上 |
| 租户 kill switch / suspension | S01 的租户状态（可随 authorization-context 一并返回） | 不可达 → fail closed |

**不做降级放行。** 这与 S24 的 `AUTH_DEPENDENCY_UNAVAILABLE` 处理一致（S24 的"读 S01 超时"）。

S28 不在本地缓存"谁能做什么"的完整授权——缓存只允许是**短期、有界、可按 revision 失效**的 decision，且记录 `source_revision`；S01 的 `access.grant.changed` 到达即失效（见 §12 的事件缺口）。

---

## 5. 失败矩阵

| # | 场景 | 检测点 | S28 行为 | 对外可见 | 是否重试 |
|---|---|---|---|---|---|
| F1 | URL 指向内网/元数据 | 解析后 IP 检查 | `URL_REJECTED` | `URL_BLOCKED` | 否 |
| F2 | 重定向跳到私网 | 每跳复检 | `FETCH_BLOCKED` | `FETCH_BLOCKED` | 否 |
| F3 | DNS 重绑定（先公网后私网） | IP pinning，连接不再解析 | 连接失败 → `FETCH_FAILED` | `retryable:true` | 是（重新校验） |
| F4 | 超大小 | `Content-Length` 或流式计数 | `REJECTED_SIZE` | `SIZE_EXCEEDED` | 否 |
| F5 | 慢速攻击 | 吞吐下限 + 总超时 | `FETCH_FAILED`（超时） | `FETCH_TIMEOUT` | 是 |
| F6 | MIME 与声明不符 | 魔数检测 | `REJECTED_SECURITY` | `FORMAT_MISMATCH` | 否 |
| F7 | ZIP 炸弹 / APNG 炸弹 / 超大帧数 | 沙箱资源限额 | 沙箱被杀 → `REJECTED_SECURITY` | `UNSAFE_MEDIA` | 否 |
| F8 | 解码超时 | 沙箱 wall-clock | `REJECTED_SECURITY` | `DECODE_TIMEOUT` | 否 |
| F9 | 解码器崩溃 | 沙箱退出码 | `REJECTED_SECURITY` | `DECODER_CRASH` | 否 |
| F10 | 命令注入尝试（路径/滤镜） | 结构化参数，无 shell | 不存在该路径；可疑输入 `REJECTED_SECURITY` | `INVALID_PARAMETER` | 否 |
| F11 | hash 不符（首次） | 比对 | `HASH_MISMATCH` | `HASH_MISMATCH` | 是（一次） |
| F12 | hash 不符（二次） | 比对 | 终态 `HASH_MISMATCH` | 同上 + `terminal:true` | 否 |
| F13 | 供应商 403/410 | HTTP 状态 | `FETCH_BLOCKED` | `URL_REJECTED_BY_PROVIDER` + `renewable:true` | 换 URL 后新命令 |
| F14 | 供应商 5xx | HTTP 状态 | `FETCH_FAILED` | `UPSTREAM_5XX` | 是（退避） |
| F15 | 对象存储写失败 | 写返回 | 不提交事务 → `PUBLISH_FAILED` | `retryable:true` | 是 |
| F16 | DB 提交失败 | 事务 | `PUBLISH_FAILED`；对象成孤儿 | `retryable:true` | 是（GC 回收孤儿） |
| F17 | Outbox relay 停 | 积压 | 制品已发布，事件延迟 | 下游查 `GET /artifacts` 兜底 | relay 恢复后重发 |
| F18 | 并发同内容导入 | `ON CONFLICT` | 复用 blob | 两个 artifact 都可读 | — |
| F19 | 跨租户同 hash | 不同 tenant 前缀 | 各自建 blob | **无任何跨租户信息** | — |
| F20 | 调用方无权限 | S01 拒绝/本地 scope 检查 | 403（租户内）/ 404（跨租户） | 见 §8.5 | 否 |
| F21 | S01 不可达 | 调用超时 | 503 `AUTH_DEPENDENCY_UNAVAILABLE` | 明确可重试 | 是（短退避） |
| F22 | 租户被 suspend | S01 返回 | 403 `TENANT_SUSPENDED` | 不含其他租户信息 | 否 |
| F23 | tombstone 与读取竞态 | 行级锁 | 读取方要么看到 AVAILABLE，要么看到 TOMBSTONED | 不出现"读到了已删对象" | — |
| F24 | 删除时仍被 S08 引用 | REFERENCE_CHECK | `BLOCKED_REFERENCES` | `references:[{service:"S08",count:n}]` | 定期重查 |
| F25 | 删除时引用方超时 | deadline | `BLOCKED_REFERENCES`（未确认） | `blockers:[{service:"S43",reason:"timeout"}]` | 重试 |
| F26 | legal hold 中请求删除 | 绑定检查 | 拒绝 `LEGAL_HOLD` | 明确 | 否（需人工解除） |
| F27 | 导入期间 revision 冲突 | `expected_revision` | 409 `REVISION_CONFLICT` | 冲突 | 否 |
| F28 | 同一 command 异 body | 幂等 | 409 `IDEMPOTENCY_KEY_REUSED` | 冲突 | 否 |

---

## 6. 对外契约

传输：内部 mTLS + workload identity，HTTP/JSON。统一头：

```
Idempotency-Key: <command_id>
X-Tenant-Id: <tenant_id>     # 必须与认证上下文一致，否则 403
traceparent: 00-<trace>-<span>-01
```

### 6.1 `POST /artifact-imports`

```jsonc
{
  "command_id": "int_01J...:ingest:1",
  "tenant_id": "t_01J...",                 // 仅一致性校验
  "content_class": "generated_video",
  "source": {
    "kind": "provider_url",
    "provider_url": "https://cdn.provider.example/…signed…",
    "provider_url_expires_at": "2026-11-01T12:00:00Z",
    "provider_job_id": "pj_01J...",
    "attempt_id": "att_01J..."
  },
  "expected_sha256": "3f2a…",              // 可选
  "declared_size_bytes": 31457280,         // 可选
  "target_artifact_id": null,              // 指定则新增 revision
  "expected_revision": null,               // 与 target 同用时必填
  "owner_ref": {"service_id": "S06", "id": "shot_3_2_1"},
  "scope_ref": {"project_id": "p_01J...", "series_id": "sr_01J...", "episode_id": "ep_01J..."},
  "retention": {"policy_ref": "ret_default_v1"},
  "purpose": "generation_output"
}
```

`202 Accepted`：

```jsonc
{
  "import_id": "imp_01J...",
  "state": "ACCEPTED",
  "artifact_id": null,
  "replayed": false
}
```

`202` 是同步受理，导入是异步的。S26 通过 `GET /artifact-imports/{id}` 或直接等 `artifact.ingested` 观测终态。

校验顺序（失败即返回）：

1. `Idempotency-Key` / `command_id` 幂等；
2. `tenant_id` 与认证上下文一致；
3. `content_class` 闭集、`source.kind` 闭集；
4. `expected_sha256` 格式（裸 64 hex）；
5. `provider_url_expires_at > now()`，否则 409 `URL_EXPIRED`（**未出网**）；
6. `target_artifact_id` 存在、属于本租户、`state='AVAILABLE'`，否则 404/409；
7. `expected_revision` 匹配 `artifact.current_revision`，否则 409 `REVISION_CONFLICT`；
8. S01 授权 `artifact.import`。

### 6.2 `GET /artifacts/{id}`

```jsonc
{
  "artifact_id": "art_01J...",
  "tenant_id": "t_01J...",
  "content_class": "generated_video",
  "state": "AVAILABLE",
  "current_revision": 2,
  "source_revision": 2,                    // 本响应基于的权威 revision
  "watermark": "2026-11-01T09:00:00Z",
  "content_hash": "3f2a…",                 // current revision 的 hash
  "size_bytes": 31457280,
  "mime_type": "video/mp4",
  "duration_ms": 4000,
  "width_px": 1080,
  "height_px": 1920,
  "frame_rate": {"num": 24, "den": 1},
  "storage": {"region": "ap-northeast-1", "class": "STANDARD"},
  "derivatives": [{"kind": "proxy", "artifact_id": "art_01K...", "state": "READY"}],
  "retention": {"policy_ref": "ret_default_v1", "retain_until": null, "legal_hold": false},
  "provenance": {"source_kind": "provider_url", "import_id": "imp_01J..."},
  "permission_filtered": true
}
```

- `content_hash` 只返回 current revision 的；历史 revision 走 `/artifacts/{id}/revisions/{rev}`。
- 响应**不含**下载 URL；URL 必须单独申请（§6.5）。这是"读取元数据"与"获取字节"两件事。
- `permission_filtered`：响应已按调用方权限过滤（例如无权限的字段省略），与规格的"权限过滤结果"对应。
- 跨租户 → 404 `ARTIFACT_NOT_FOUND`（不区分"不存在"与"不是你的"）。
- 已 tombstone → 200 + `state:"TOMBSTONED"` + `tombstoned_at`（调用方需要知道它曾存在；这是**同租户**语义）。

### 6.3 `GET /artifacts/{id}/revisions/{revision}`

返回该 revision 的不可变记录（hash、大小、探测元数据、发布人、`source_import_id`）。用于 S08/S40 复核"审的是不是这一版"（S40 的"所指 artifact 仍存在且 hash 匹配"）。

已删除字节但未删行时返回 200 + `storage:{deleted:true}`；完全无此 revision → 404。

### 6.4 `POST /artifacts/{id}:tombstone`

```jsonc
{
  "command_id": "cmd_tomb_01J...",
  "reason_code": "USER_REQUEST",
  "note": "误传，替换为 art_01K...",
  "delete_bytes": true
}
```

`200` + `{artifact_id, state:"TOMBSTONED", deletion_id}`（`delete_bytes=false` 时 `deletion_id:null`）。

语义：`tombstone` 立即生效（事务提交即可见），删除编排异步启动。**不提供"取消 tombstone"。**

### 6.5 `POST /media-access-tickets`

```jsonc
{
  "command_id": "cmd_acc_01J...",
  "artifact_ref": {"id": "art_01J...", "revision": 2, "content_hash": "3f2a…"},
  "purpose": "qc",
  "scope_ref": {"project_id": "p_01J..."},
  "ttl_seconds": 900,
  "max_bytes": null
}
```

`200`：

```jsonc
{
  "ticket_id": "tkt_01J...",
  "url": "https://obj.example/…signed…",
  "expires_at": "2026-11-01T09:15:00Z",
  "method": "GET"
}
```

拒绝条件：artifact 不存在（404）、tombstoned（409 `ARTIFACT_TOMBSTONED`）、purpose 不在允许集（422）、TTL 超 24h（422）、无权限（403 租户内 / 404 跨租户）。

### 6.6 查询

| 路径 | 用途 |
|---|---|
| `GET /artifact-imports/{import_id}` | 导入状态、失败原因、`retryable`、`renewable` |
| `GET /artifacts/{id}/derivatives` | 派生状态 |
| `GET /deletions/{deletion_id}` | 删除编排进度、`blockers`、`reference_evidence` |
| `GET /artifacts?scope_ref=…` | **仅租户内**、按 scope 过滤的列举（不提供跨租户） |

`GET /artifacts` 必须与调用方的 scope 绑定；不做"全租户搜索"给无权限主体。

### 6.7 错误码

| HTTP | code | retryable | 说明 |
|---:|---|---|---|
| 400 | `INVALID_REQUEST` | false | 结构/格式 |
| 403 | `TENANT_SUSPENDED` | false | 租户被停用 |
| 403 | `FORBIDDEN_SCOPE` | false | 租户内跨 scope |
| 404 | `ARTIFACT_NOT_FOUND` | false | 含跨租户（不泄漏） |
| 404 | `IMPORT_NOT_FOUND` | false | |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同键异 body |
| 409 | `REVISION_CONFLICT` | false | `expected_revision` 不符 |
| 409 | `URL_EXPIRED` | false | 未出网就发现 TTL 已过 |
| 409 | `ARTIFACT_TOMBSTONED` | false | 已 tombstone |
| 409 | `LEGAL_HOLD` | false | 删除被 legal hold 阻止 |
| 422 | `UNSAFE_MEDIA` | false | 安全拒绝 |
| 422 | `FORMAT_MISMATCH` | false | MIME/魔数不符 |
| 422 | `HASH_MISMATCH` | conditional | 首次可重试 |
| 422 | `SIZE_EXCEEDED` | false | |
| 503 | `AUTH_DEPENDENCY_UNAVAILABLE` | true | S01 不可达 |
| 503 | `INGEST_BACKLOG` | true | 队列过载，`Retry-After` |

错误响应**不包含** URL、query string、其他租户 ID、artifact 是否存在这类信息。

### 6.8 事件：`artifact.ingested`

Topic `drama.artifact-ingest.v2`，envelope 按 `event-envelope.v2.schema.json`：

```jsonc
{
  "specversion": "1.0",
  "schema_version": "2.0",
  "id": "evt_01J...",
  "source": "S28",
  "type": "artifact.ingested",
  "subject": "art_01J...",
  "time": "2026-11-01T09:00:01Z",
  "tenant_id": "t_01J...",
  "home_cell": "cell-a1",
  "aggregate_id": "art_01J...",
  "aggregate_sequence": 2,                 // = revision
  "correlation_id": "int_01J...",
  "causation_id": "imp_01J...",
  "traceparent": "00-…-…-01",
  "data": {
    "artifact_ref": {"service_id":"S28","id":"art_01J...","revision":2,"content_hash":"3f2a…"},
    "content_class": "generated_video",
    "size_bytes": 31457280,
    "mime_type": "video/mp4",
    "duration_ms": 4000,
    "width_px": 1080,
    "height_px": 1920,
    "source": {"provider_job_id":"pj_01J...","attempt_id":"att_01J..."},
    "dedup": {"hit": false},
    "derivative_hint": ["proxy"]
  },
  "data_ref": null
}
```

- **事件里没有媒体**（docs/04 §4）。
- `dedup.hit` 只有在同租户同 scope 下才为 true；跨 scope 一律 `false`（§4.4）。
- 大缩略图/证据包不进事件，用 `data_ref` 指向另一个 artifact。

消费方（S08 之外的 S10 索引、S44 报表、S47 投影）必须用 Inbox 去重，按 `aggregate_sequence` 判序。

---

## 7. 幂等与并发

### 7.1 幂等语义（与其他服务统一）

```
request_hash = sha256( JCS( body 去掉 command_id 与 request_hash ) )
```

| 已有 | 本次 | 结果 |
|---|---|---|
| 无 | — | 执行，先插 `IN_PROGRESS` |
| `IN_PROGRESS` | 同 hash | 409 `COMMAND_IN_PROGRESS`，`retryable:true` |
| `DONE` | 同 hash | 原样返回首次响应，`replayed:true` |
| 任意 | 异 hash | 409 `IDEMPOTENCY_KEY_REUSED` |

导入是异步的，所以 `DONE` 的响应是**受理响应**（202 + import_id），不是终态。终态通过查询或事件观测。这条容易被误解成"幂等 = 结果幂等"，必须是"受理幂等"。

### 7.2 同一 content 并发导入

```text
T1: INSERT blob ON CONFLICT DO NOTHING → 插入成功
T2: INSERT blob ON CONFLICT DO NOTHING → 冲突，不插入
T1/T2: SELECT ... FOR UPDATE → 同一 blob_id、同一 storage_key
T1: 发布 artifact A
T2: 发布 artifact B（同一 blob，ref_count=2）
```

两个 artifact 的 `(artifact_id, revision)` 不同，互不干扰。若两者是同一 `target_artifact_id` + 同一 revision，则 `(artifact_id, content_hash)` 唯一约束会让第二个直接复用（不新建 revision），返回已存在 revision。

### 7.3 artifact revision CAS

新增 revision 必须带 `expected_revision`：

```sql
UPDATE artifact
   SET current_revision = current_revision + 1, updated_at = now()
 WHERE artifact_id = $1 AND tenant_id = $2
   AND current_revision = $3 AND state = 'AVAILABLE';
-- 影响 0 行 → 409 REVISION_CONFLICT
```

`artifact_revision.revision = new_current_revision`。这一步在发布事务里，与 blob、outbox 同事务。

### 7.4 tombstone 与并发读

读取路径：

```sql
BEGIN ISOLATION LEVEL READ COMMITTED;
  SELECT state, current_revision FROM artifact
   WHERE artifact_id=$1 AND tenant_id=$2 FOR SHARE;
  -- 已 TOMBSTONED → 返回 tombstone 语义，不给 URL
  SELECT * FROM artifact_revision WHERE artifact_id=$1 AND revision=$3;
COMMIT;
```

tombstone：

```sql
BEGIN;
  UPDATE artifact SET state='TOMBSTONED', tombstoned_at=now()
   WHERE artifact_id=$1 AND tenant_id=$2 AND state='AVAILABLE'
  RETURNING current_revision;
  -- 0 行 → 已经 tombstone 或不存在 → 幂等返回当前状态
  INSERT INTO artifact_tombstone (...) VALUES (...);
  UPDATE media_access_ticket SET revoked_at=now()
   WHERE tenant_id=$2 AND artifact_id=$1 AND revoked_at IS NULL;
  INSERT INTO outbox (...) VALUES (...);
COMMIT;
```

`FOR SHARE` 与 `UPDATE` 互相排斥：要么读事务先拿到共享锁（tombstone 等待），要么 tombstone 先提交（读事务看到 `TOMBSTONED`）。不会出现"读到了 AVAILABLE 并签发 ticket，而 ticket 不在撤销集合里"的窗口——因为 ticket 插入也在读事务内，且 tombstone 的 `UPDATE media_access_ticket` 会覆盖它（RC 隔离下，tombstone 等待读事务结束后才能取得行锁，随后它看到的是已提交的 ticket 行）。

> 实现时必须写这个并发测试（T28-22），不能靠推理。

### 7.5 删除单写者

- `deletion_job` 每个 artifact 一行（`UNIQUE`），删除推进用 `SELECT ... FOR UPDATE SKIP LOCKED`。
- 每个 step 幂等：重复执行 `BYTES_DELETE` 对已删除对象是 no-op；重复 `INDEX_PURGE` 通知由下游 Inbox 去重。
- 引用检查结果带 `checked_at`；超过 24h 的 `checked:true` 视为过期，重新查（防止引用在检查后新增）。

### 7.6 锁顺序

统一顺序，防死锁：

```
artifact → artifact_revision → content_blob → media_access_ticket → outbox
```

删除作业按 `deletion_job → artifact → blob`，与导入路径的 `artifact → blob` 一致（deletion_job 在最后加入，不破坏前缀顺序）。

---

## 8. 安全（本服务最危险的部分）

docs/14 §2 原文要求：S28 导入限制协议、文件/MIME、重定向、大小、解码时间、DNS 重绑定与内网/元数据访问；FFmpeg/图像/字幕解析运行非特权无网络沙箱，路径和滤镜参数结构化生成，禁止把用户文字拼 shell。

### 8.1 SSRF 防线（四层，缺一不可）

| 层 | 措施 |
|---|---|
| 协议 | 只允许 `https`；禁 `http`、`file`、`gopher`、`data`、`ftp` |
| 地址 | 解析后逐条判私网/环回/链路本地/元数据/唯一本地；**IP pinning**，连接用解析出的 IP，不再让库二次解析 |
| 重定向 | 最多 3 跳，每跳重跑全部检查；跨 host 必须在新 allowlist 内；不跟随 `Refresh`/meta refresh |
| 网络 | 导入 worker 所在命名空间的 `NetworkPolicy` 只允许出到 allowlist 与对象存储；**禁止访问集群内 Service CIDR** |

额外：不允许通过环境变量 `HTTP_PROXY` 出网；不允许 `CONNECT` 隧道；TLS 校验不可关闭（`verify=False` 属实现缺陷）。

### 8.2 媒体沙箱

```text
Pod/容器：
  · runAsNonRoot、readOnlyRootFilesystem、drop ALL capabilities、seccomp RuntimeDefault
  · NetworkPolicy: 出站全部拒绝
  · 只读挂载输入（隔离区对象以只读卷或本地拷贝挂入）
  · 可写只有 tmpfs（尺寸受限）
  · cgroup：memory max、cpu max、pids max、wall-clock 超时可杀（SIGKILL）
  · 无 ServiceAccount token 挂载（automountServiceAccountToken=false）
参数：
  · 全部通过 argv 数组传递，**永不拼接 shell 字符串**
  · 文件路径由服务端生成（ULID），不含用户输入
  · ffmpeg filter 由白名单 preset 生成；用户可选值只有枚举
  · 禁止 ffmpeg 的 `-f lavfi`/`concat`/`protocol_whitelist` 之外的自定义协议
产出：
  · 探测结果（ffprobe JSON 裁剪到白名单字段）
  · 派生产物写回 staging，由受信进程再计算 hash 并发布
```

沙箱**不产生 hash、不写正式前缀、不调用任何外部服务**。

### 8.3 恶意文件类型

| 类型 | 防御 |
|---|---|
| ZIP/7z/RAR 炸弹 | 不自动解压；字幕包/素材包若必须解，限制条目数、单条大小、总解压比 ≤ 100:1、嵌套深度 1 |
| APNG/GIF 炸弹 | 帧数上限、解码内存上限 |
| 超大分辨率图 | 像素数上限（如 100MP），超限不解码 |
| 畸形容器 | 先解析结构再解码；解析失败即拒 |
| 字幕（SRT/VTT/ASS） | 字体不加载、脚本标签剥离、长度上限、编码检测 |
| SVG | **不渲染**；只当字节保存，若需缩略图走位图转换并禁外部引用 |
| PDF（若允许） | 禁 JS、禁外部引用、页数上限 |
| 文件名/路径 | 不使用上传文件名做任何路径；一律 ULID |

### 8.4 对象存储与访问

- 对象 key 前缀：`{tenant_id}/{region}/{series_id}/{artifact_id}/{revision}/{ulid}`（docs/03 §3）。**路径不是授权**——即使知道 key 也不能直接读，必须短签名 + 对象级检查。
- bucket 策略拒绝公共读、拒绝列举；签名 URL 只签单对象、单方法、短 TTL。
- 下载出口经过统一网关做速率限制，防大规模拖库。
- 对象存储开启版本控制与对象锁（保留策略）；删除是"删当前版本 + 按保留期删旧版本"。

### 8.5 跨租户拒绝不泄漏存在性（E28）

| 场景 | 返回 |
|---|---|
| 访问其他租户的 artifact_id | **404** `ARTIFACT_NOT_FOUND`（与真不存在完全相同） |
| 其他租户的 import_id | 404 |
| 用其他租户 hash 探测 | 无接口可探测；导入总是受理并产生自己的 blob |
| 响应时间差异 | 404 路径做固定延迟阻尼（如统一 30–50ms），避免时序侧信道 |
| 错误详情 | 不含对方 ID、不含"belongs to tenant" |
| 事件 | 事件按 `tenant_id` 分区，消费方只能订阅自己租户 |

租户内的跨 scope 访问返回 403 `FORBIDDEN_SCOPE`——同租户内不需要隐藏存在性，但也不返回内容。

---

## 9. 可观测

### 9.1 Metrics

| 指标 | 类型 | 说明 |
|---|---|---|
| `s28_import_accepted_total` | counter | 按 `content_class`、`source_kind` |
| `s28_import_state_total` | counter | 按终态与 `reject_code` |
| `s28_import_duration_seconds` | histogram | 从 ACCEPTED 到终态 |
| `s28_fetch_bytes_total` | counter | 按 class |
| `s28_fetch_duration_seconds` | histogram | 含首字节与总时长 |
| `s28_sandbox_kill_total` | counter | 按原因（timeout/oom/pids） |
| `s28_hash_mismatch_total` | counter | |
| `s28_dedup_hit_ratio` | gauge | **按 tenant 聚合，不跨租户** |
| `s28_staging_orphan_bytes` | gauge | 隔离区孤儿 |
| `s28_tombstone_total` | counter | 按 reason |
| `s28_deletion_blocked_total` | counter | 按 blockers 服务 |
| `s28_access_ticket_total` | counter | 按 purpose |
| `s28_cross_tenant_denied_total` | counter | 跨租户 404（E28 监控） |
| `s28_outbox_lag_seconds` | gauge | |
| `s28_authz_denied_total` | counter | 按 reason |

### 9.2 告警

| 告警 | 条件 | 级别 |
|---|---|---|
| 归档失败率 | 5min 内 `reject_code` 非安全类 > 5% | P2 |
| 安全拒绝激增 | `UNSAFE_MEDIA` 5min > 10 或环比 5× | P1（可能是攻击） |
| 跨租户拒绝激增 | `cross_tenant_denied_total` 环比 3× | P1 |
| 下载 TTL 逼近 | `url_expires_at - now() < 15min` 且未完成 | P2 |
| 导入积压 | 最老 pending > 10min | P2 |
| 沙箱被杀率 | > 1% | P2（解码器或攻击） |
| 孤儿字节 | > 50 GiB 或 24h 未清 | P3 |
| 删除阻塞 | `BLOCKED_REFERENCES` > 7 天 | P2 |
| Outbox lag | > 5min | P2 |
| S01 依赖 | 5xx/timeout > 1% | P1 |

### 9.3 Trace

`traceparent` 贯穿：S26 任务 → S28 `POST /artifact-imports` → worker fetch → sandbox → publish → outbox → 下游消费。

trace 标签只放 ID（`import_id`、`artifact_id`、`content_class`、`reject_code`），**不放 URL、不放文件名、不放内容 hash 作为高基数 metric label**。hash 可以进 span attribute 与日志（它是内容标识，不是内容本身），但不进 metrics。

---

## 10. 测试清单

覆盖 E13、E23、E28、E29、E37、E39；附带 E01、E12、E32、E34。

### 10.1 幂等与版本（E12）

| # | 场景 | 断言 |
|---|---|---|
| T28-01 | 同 `command_id` 同 body 重投 3 次 | 同一个 `import_id`，`attempts` 不虚增，只有一次 fetch |
| T28-02 | 同键异 body | 409 `IDEMPOTENCY_KEY_REUSED` |
| T28-03 | 换 URL 但复用 `command_id` | 409（换命令必须换 key） |
| T28-04 | 新增 revision 时 `expected_revision` 过期 | 409 `REVISION_CONFLICT`，不写新 revision |
| T28-05 | `IN_PROGRESS` 期间重投 | 409 `COMMAND_IN_PROGRESS` + `Retry-After` |

### 10.2 崩溃与恢复（E13、E34）

| # | 场景 | 断言 |
|---|---|---|
| T28-06 | 发布事务提交前 kill worker | 无 artifact、无 outbox；staging 成孤儿并被 GC |
| T28-07 | 对象写入成功后事务回滚 | DB 无记录；孤儿对象被 GC；无"指向不存在对象的 manifest" |
| T28-08 | 发布提交后、outbox 未发即崩溃 | 制品可读；relay 恢复后事件恰好一次可见（下游去重） |
| T28-09 | 导入中 Pod 重启 | 从 Postgres 恢复状态，不重复下载已 STAGED 的部分 |
| T28-10 | 删除作业中途崩溃 | 每步可重入，最终 COMPLETE；不重复删已删对象导致 404 报错 |
| T28-11 | Redis 全清 | 无影响（S28 写路径不依赖 Redis） |
| T28-12 | 新 region 启动、旧 cell 未知 | 已发布制品可读；未完成导入标 `PUBLISH_FAILED` 不自动重试跨区 |

### 10.3 供应商 URL 过期与下载（E23）

| # | 场景 | 断言 |
|---|---|---|
| T28-13 | `expires_at` 已过 | 409 `URL_EXPIRED`，**零出网** |
| T28-14 | 供应商返 410 | `FETCH_BLOCKED` + `renewable:true`；不重试、不重生成 |
| T28-15 | 供应商 5xx | `FETCH_FAILED` + `retryable:true`，指数退避 |
| T28-16 | 下载中断后重试成功 | 归档成功；`attempts=2`；不产生第二个 artifact |
| T28-17 | TTL 剩 5 分钟 | 告警触发，队列优先级提升 |
| T28-18 | 下载成功但 `expected_sha256` 不符（一次后修正） | 首次 `HASH_MISMATCH` 可重试；第二次成功 |
| T28-19 | 二次仍不符 | 终态，`terminal:true`；S26 转人工路径可识别 |

### 10.4 安全（E29）

| # | 场景 | 断言 |
|---|---|---|
| T28-20 | URL 为 `http://` | `URL_REJECTED`，未出网 |
| T28-21 | URL 解析到 `169.254.169.254` | `URL_REJECTED`（元数据服务） |
| T28-22 | URL 解析到 `127.0.0.1` / `10.x` / `::1` | 全部拒绝 |
| T28-23 | 公网 URL 302 到 `http://10.0.0.5` | 第二跳被拒，`FETCH_BLOCKED` |
| T28-24 | DNS 先返回公网、连接时返回私网 | IP pinning 生效，连接用第一次解析结果，攻击失败 |
| T28-25 | 超大文件（超上限） | 边读边计数，达到上限即断；`REJECTED_SIZE`；不落完整文件 |
| T28-26 | ZIP 炸弹（高压缩比） | 不自动解压；若为素材包则拒绝 |
| T28-27 | 畸形 MP4 触发解码器崩溃 | 沙箱退出，`DECODER_CRASH`；宿主不受影响 |
| T28-28 | 解码超时 | 沙箱被 SIGKILL，`DECODE_TIMEOUT` |
| T28-29 | 文件名为 `../../etc/passwd` | 不使用文件名；路径由 ULID 生成 |
| T28-30 | 用户试图注入 ffmpeg filter 字符串 | 接口不接受自由字符串；仅枚举 preset |
| T28-31 | 沙箱内尝试出网 | `NetworkPolicy` 拒绝，沙箱无网络 |
| T28-32 | 沙箱内读取 SA token | 未挂载，读取失败 |
| T28-33 | MIME 声明 `video/mp4` 实为可执行文件 | 魔数检测拒绝 `FORMAT_MISMATCH` |

### 10.5 跨租户（E28）

| # | 场景 | 断言 |
|---|---|---|
| T28-34 | 租户 B 用租户 A 的 artifact_id 查询 | 404，响应体与"不存在"逐字节一致 |
| T28-35 | 租户 B 导入与租户 A 相同 hash | 受理成功；创建 B 自己的 blob；响应无任何 A 的信息 |
| T28-36 | 租户 B 的 `dedup.hit` | 恒为 false（跨租户不可见） |
| T28-37 | 租户内跨 project 的 `dedup.hit` | 无该 project 权限时 false |
| T28-38 | 跨租户访问的响应时间 | 与不存在路径同分布（阻尼生效） |
| T28-39 | 跨租户 `:tombstone` | 404，不泄露是否已存在 |

### 10.6 删除与保留（E39）

| # | 场景 | 断言 |
|---|---|---|
| T28-40 | tombstone 后立即申请访问 | 409 `ARTIFACT_TOMBSTONED`，无 URL |
| T28-41 | tombstone 前已签发未过期 ticket | `revoked_at` 被写入；已发出的 URL 记最晚过期时间 |
| T28-42 | tombstone 与并发读 | 不出现"读到 AVAILABLE 并拿到可用 ticket"（§7.4） |
| T28-43 | 删除时 S08 仍引用 | `BLOCKED_REFERENCES`，`references` 含 S08 与数量 |
| T28-44 | 删除时 S43 查询超时 | `BLOCKED_REFERENCES`，`blockers` 标 timeout，**不删字节** |
| T28-45 | legal hold 中请求删除 | 409 `LEGAL_HOLD`；不进入 BYTES_DELETE |
| T28-46 | blob 被两个 artifact 引用，删其一 | ref_count 减一，字节保留 |
| T28-47 | 删除完成后查 revision | 200 + `storage.deleted=true`，hash 仍可验（最小证明） |
| T28-48 | 备份到期前查删除状态 | `BACKUP_EXPIRY` 未完成，`COMPLETE` 不提前 |
| T28-49 | 审计事件 | 谁请求、何时、哪些引用方确认、最终删除时间齐全 |

### 10.7 交付与预览（E37）

| # | 场景 | 断言 |
|---|---|---|
| T28-50 | 交付需要 master，只有 proxy | 查询返回 `content_class='proxy'`，S28 不冒充 master |
| T28-51 | 引用已 tombstone 的 artifact | 409 `ARTIFACT_TOMBSTONED`，不是 404（不伪装成数据从未存在） |
| T28-52 | 派生失败 | 源 artifact 仍 AVAILABLE；derivative=`FAILED` |

### 10.8 事件（E32）

| # | 场景 | 断言 |
|---|---|---|
| T28-53 | 同一事件投递 3 次 | 下游 Inbox 去重；不重复建索引 |
| T28-54 | 事件乱序（revision 3 先于 2） | 下游按 `aggregate_sequence` 处理；S28 侧不发出乱序（同 aggregate 串行） |
| T28-55 | Kafka 重放 1 小时 | 不新建 artifact、不改 revision、不重发通知副作用 |
| T28-56 | Outbox 积压后恢复 | 事件最终全部送达；`aggregate_sequence` 单调 |

### 10.9 独立升级（E01）

| # | 场景 | 断言 |
|---|---|---|
| T28-57 | 构建只依赖契约 SDK | 依赖图无其他服务 domain 包；无跨库连接 |
| T28-58 | 回滚到上一版本（schema 只加可选列） | 旧版本可读写；旧版本不认识的新 `content_class` 不被旧版本误判 |

### 10.10 并发

| # | 场景 | 断言 |
|---|---|---|
| T28-59 | 100 个并发导入同一 hash | 只有 1 个 blob，100 个 artifact 全部成功，ref_count=100 |
| T28-60 | 同一 artifact 并发新增 revision | 一个成功，一个 409 `REVISION_CONFLICT` |
| T28-61 | 两个删除作业同时请求 | 只有一个活跃 job（UNIQUE） |

---

## 11. 实施顺序与未决

### 11.1 顺序（W1）

1. **契约先行**：补 `contracts/openapi.v2.yaml` 的四条路径、`artifact-*.v2.schema.json`、`media-access-ticket` schema。没有 schema 不写业务代码（§12）。
2. **数据模型 + 迁移 + RLS**：本方案 §2 的表；每张表 `tenant_id` + RLS 策略；跨租户测试先于功能。
3. **导入安全链（无发布）**：URL 校验、IP pinning、大小限制、沙箱探测。用 mock 供应商与本地 mock 对象存储跑通 E29 全部用例。
4. **发布事务 + 去重 + Outbox**：E12/E28/E13 用例。
5. **访问 ticket + tombstone + 删除编排（骨架）**：删除的引用检查先接 mock S08/S43/S48，E39 用例可跑。
6. **派生**：proxy/缩略图，放在导入稳定之后。
7. **对 S01 的真实依赖**：先用 mock `GET /authorization-context`，S01 落地后切换。
8. **容量与压测**：媒体 worker pool 独立；背景任务（GC、删除）用独立 Deployment。

### 11.2 未决问题

| # | 问题 | 需要谁回答 |
|---|---|---|
| Q28-1 | `content_class` 的上限是否按 class 写死在契约里，还是配置化 | 平台 + 媒体团队 |
| Q28-2 | 删除引用检查要问哪些服务，是否是固定清单 | 架构（见 §12 C28-5） |
| Q28-3 | `GET /artifacts` 是否需要对 BFF 提供"跨 scope 的只读投影" | S46/产品 |
| Q28-4 | 派生是否允许客户自定义 preset（影响沙箱参数面） | 产品 + 安全 |
| Q28-5 | 存储分层（INFREQUENT/ARCHIVE）的迁移策略与取回延迟是否影响交付 SLA | 基础设施 |
| Q28-6 | 证据类 artifact 的保留期是否与源媒体绑定 | 法务 + 交付 |
| Q28-7 | 是否需要对 S48 提供"训练集中删除单样本"的批量接口 | S48 |

---

## 12. 冲突与缺口

按 AGENTS.md「契约与文档冲突 → 停下、报告、不自行选边」。以下是落地前必须裁决的点。本文的写法是**建议值**，最终以裁决为准。

| # | 冲突/缺口 | 现状 | 本文怎么处理 | 需要谁改 |
|---|---|---|---|---|
| C28-1 | `contracts/openapi.v2.yaml` **没有** `POST /artifact-imports`、`GET /artifacts/{id}`，也没有任何 S28 路径 | openapi 21 个操作里一个 S28 都没有；`service.yaml` 却声明了命令与查询入口 | 按 `service.yaml` 的路径写契约（§6） | contracts：补路径 |
| C28-2 | 没有 artifact / import / ticket 的机器可读 schema | `contracts/*.v2.schema.json` 9 个文件里没有 artifact | 新增 `contracts/media/artifact.v2.schema.json`、`artifact-import.v2.schema.json`、`media-access-ticket.v2.schema.json` | contracts |
| C28-3 | `event-topics.v2.json` 每个服务只允许一种事件，但 tombstone、派生完成、访问拒绝都需要对外可见 | 目录里 S28 只有 `artifact.ingested` | 全部折叠进 `artifact.ingested` 的 `data.event_kind`（`ingested`/`tombstoned`），或新增 topic；本文建议折叠以免新增硬依赖 | contracts + 架构（需裁决） |
| C28-4 | S26 的方案（§8.1）已经**单方面定义**了 `POST /artifact-imports` 的 body（`provider_url`、`expected_sha256`、`content_class`、`source`、`retention`） | S26 是调用方，但 schema 应归 S28 | 本文 §6.1 在其基础上补齐 `owner_ref`、`scope_ref`、`target_artifact_id`、`expected_revision`、`purpose`；字段名与语义保持兼容 | S26 方案 + contracts 对齐（不冲突，只是缺口） |
| C28-5 | **删除的引用检查没有定义"问谁"**。docs/04 §6 说"按服务编排删除"，但没有引用方清单，也没有查询契约 | 无 | 本文 §4.8 列出 S08/S43/S48/S45 并规定超时即阻塞；清单需裁决 | 架构（需明确清单与查询接口） |
| C28-6 | docs/04 §6 说删除后"审计与财务记录可保留最小证明"，但没定义 S28 保留什么、以什么形式给 S45 | 无 | 本文 §4.8 保留 hash/时间/请求者/引用确认结果，媒体原文删除 | docs/04 补充 + S45 方案 |
| C28-7 | **S38 依赖 S28 但方案未提**（`delivery/acceptance/plans-consistency-2026-10-30.md` G2） | S38 的 `depends_on` 含 S28，方案全文无 S28 | 若 S38 需要音轨做 `SUB.TIMING`，它应通过 S28 的 `GET /artifacts/{id}` + `POST /media-access-tickets(purpose=qc)` 获取；本文已提供该路径 | S38 方案或 catalog（见 G2 两种处理） |
| C28-8 | **S28 的"媒体访问权限"与 S10/S15 的"素材权利"边界**：两处都叫"授权"，但一个是"能不能下载字节"，一个是"能不能用于本次生成/交付" | docs/10 要求 S15 实时检查可撤销资格；S28 的 `data_owner` 也写"媒体访问权限" | 本文 §0.3/§1 明确：S28 只判"字节访问"，**不判权利**；权利由 S15/S10 判，S02 在 dispatch 复查 | 文档澄清（无代码冲突） |
| C28-9 | E37 的"预览/正式 Master/缺素材"三者，S28 只能提供 `content_class` 与"不存在/已 tombstone"两类事实，"正式 Master 完整性门"属于 S43 | 无 | 本文 §1.6 划清；S28 不判定 master 完整性 | S43 方案 |
| C28-10 | 全局控制面（Tenant Directory、`home_region`/`home_cell`）归属未在 service catalog 里写清 | docs/03 说是"全局控制面"，S01 的 `data_owner` 没有这两个字段 | 本文按"S28 存**自己的** `home_region`/`home_cell` 副本，权威在控制面"实现；不在 S28 做租户路由 | 架构（与 S01 的 C01-3 同一缺口） |
| C28-11 | 对象存储的 region 隔离与"单 S28 部署"的关系：`service.yaml` 没有 region 维度，但 docs/03 要求 media 在租户 cell | catalog 生成物无法表达多 cell | 本文按"每 cell 一个 S28 部署实例、共享一个逻辑服务 ID"实现；这是部署形态，不改契约 | 架构（部署文档） |

**没有发现"两个设计文档互相矛盾、必须二选一"的硬冲突**（与 C1 不同）。以上都是契约缺失或边界未写清。C28-3 是唯一的"语义要选边"项：是折叠进单一事件还是新增 topic，需要裁决。

---

## 附：与 `service.yaml` 的逐项对照

| `service.yaml` 字段 | 本文落点 |
|---|---|
| `data_owner: ArtifactManifest/hash/位置/TTL导入状态/代理/媒体访问权限与保留策略` | §0.1、§1.1、§2 |
| `api.command: POST /artifact-imports` | §6.1 |
| `api.query: GET /artifacts/{id}` | §6.2 |
| `publishes: artifact.ingested / drama.artifact-ingest.v2 / 14d` | §6.8 |
| `depends_on: [S01]` | §4.10 |
| `runtime.scaling: 按媒体bytes/s与导入队列扩容；存储region隔离` | §8.4、§9.1、§11 |
| `runtime.recovery: 先上传暂存后校验再原子发布manifest；下载失败不重生成` | §4.1、§4.5、§4.9 |
| `acceptance: 供应商URL到期前归档；SSRF/内网/恶意媒体拒绝；不可变hash可验` | §4.5、§8、§10.3–10.4 |
| `slo.verified: false` | 未验证；本文不承诺任何 SLO 数字 |
