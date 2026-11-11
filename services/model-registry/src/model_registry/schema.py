"""S24 自己的 schema。别的服务不 import 这里，只透过 HTTP 契约交互。

与 plans/S24-model-registry.md §2 的 DDL 一一对应，只把 Postgres 特有写法换成
SQLite：

- `timestamptz` → `TEXT`（固定宽度 ISO-8601 UTC，见 service 里的 `_ts`）
- `jsonb` → `TEXT`（JCS 文本）
- `text[]` → `TEXT`（JSON 数组文本）
- `char(64)` / `char(3)` → `TEXT` 加 CHECK 长度
- `boolean` → `INTEGER` 加 CHECK
- `numeric` → `TEXT`（十进制字符串，SQLite 没有精确小数；这两列是档位边界与
  时长，不是金额，金额一律 `*_micro` 的 INTEGER，与 §2 的约定一致）
- `billing_units ? 'request'` → `json_type(billing_units, '$.request')`

两处与 §2 的**加严**，都写在 DDL 注释里：
1. `mode_combination_probe.evidence_id` 加外键指向 `capability_evidence`。§2.4 的
   注释写「必须指向 capability_evidence」，但 DDL 只给了到 `capability_mode` 的
   外键。按注释加严，否则一条指向不存在证据的"支持"结论能落库。
2. `model_capability_snapshot.capability_bundle_rev` 加 UNIQUE。§3.5 用它当
   `aggregate_sequence`，重复的序号会让下游投影把两次发布当成一次。

`outbox` 不在这里声明：由平台事件 SDK 建表并写，字段形状以 SDK 为准（C24-9）。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §2.1 渠道
CREATE TABLE IF NOT EXISTS provider_channel (
  provider             TEXT NOT NULL,
  channel              TEXT NOT NULL,
  connector_deployment TEXT NOT NULL,
  egress_identity      TEXT NOT NULL,
  credential_ref       TEXT NOT NULL,
  credential_version   INTEGER NOT NULL CHECK (credential_version >= 1),
  lifecycle            TEXT NOT NULL
    CHECK (lifecycle IN ('disabled','validating','active','degraded','retired')),
  retired_reason       TEXT,
  updated_at           TEXT NOT NULL,
  PRIMARY KEY (provider, channel),
  -- §2.1 原话：retired 必须说明理由，否则「为什么停了」只能靠猜
  CHECK (lifecycle <> 'retired' OR retired_reason IS NOT NULL)
);

-- §2.1 账户池。为什么独立成表而不是快照上的字段：并发上限、健康态、凭证版本
-- 是跨模型共享的事实，复制进 30 个快照就是 30 份各自记住的健康态。
CREATE TABLE IF NOT EXISTS provider_account_pool (
  pool_id            TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  provider           TEXT NOT NULL,
  channel            TEXT NOT NULL,
  region             TEXT NOT NULL,
  scope              TEXT NOT NULL CHECK (scope IN ('shared','tenant_byok')),
  credential_ref     TEXT,
  credential_version INTEGER,
  data_region        TEXT NOT NULL,
  max_inflight       INTEGER NOT NULL CHECK (max_inflight >= 0),
  rpm                INTEGER NOT NULL CHECK (rpm >= 0),
  tpm                INTEGER,
  health             TEXT NOT NULL DEFAULT 'disabled'
    CHECK (health IN ('healthy','suspect','unauthorized','exhausted','disabled')),
  health_reason      TEXT,
  health_checked_at  TEXT,
  enabled            INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
  created_at         TEXT NOT NULL,
  FOREIGN KEY (provider, channel) REFERENCES provider_channel,
  -- BYOK 硬规则：自有凭证只能服务该企业，所以 tenant_id 必须不是 '*'
  CHECK ((scope = 'tenant_byok') = (tenant_id <> '*')),
  CHECK (scope = 'shared' OR credential_ref IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pool_natural
  ON provider_account_pool (provider, channel, region, scope, tenant_id);

-- §6.1/§6.2 capability_bundle_revision 是全局单调计数器，取一次加一。
-- 单行表：SQLite 的 BEGIN IMMEDIATE 已经把它串行化，不需要额外锁行。
CREATE TABLE IF NOT EXISTS registry_counter (
  name  TEXT PRIMARY KEY,
  value INTEGER NOT NULL CHECK (value >= 0)
);

-- §5.2 的「S01 已缓存授权投影」。**这是缓存，不是权威**：值只能来自 S01，
-- 连来源版本与水印一起存；缺行时读路径 503，绝不当成「全都允许」。
-- §2 没有给这张表 DDL（C24-2），这里只放服务需要的五个字段，不自己发明授权
-- 事实（例如不存「额度」、不存「可用模型列表」）。
CREATE TABLE IF NOT EXISTS authorization_projection_cache (
  tenant_id              TEXT PRIMARY KEY,
  home_region            TEXT,
  allowed_residency_tags TEXT NOT NULL DEFAULT '[]',
  source_revision        TEXT,
  resolved_at            TEXT NOT NULL
);

-- §2.2 能力快照
CREATE TABLE IF NOT EXISTS model_capability_snapshot (
  snapshot_id           TEXT PRIMARY KEY CHECK (length(snapshot_id) = 64),
  tenant_id             TEXT NOT NULL DEFAULT '*',
  provider              TEXT NOT NULL,
  channel               TEXT NOT NULL,
  model_id              TEXT NOT NULL,
  model_snapshot        TEXT NOT NULL,
  region                TEXT NOT NULL,
  account_pool_id       TEXT NOT NULL,
  capability_bundle_rev INTEGER NOT NULL CHECK (capability_bundle_rev >= 1),
  lifecycle             TEXT NOT NULL
    CHECK (lifecycle IN ('disabled','validating','active','degraded','retired')),
  lifecycle_reason      TEXT,
  data_region           TEXT NOT NULL,
  residency_tags        TEXT NOT NULL DEFAULT '[]',
  evidence_state        TEXT NOT NULL
    CHECK (evidence_state IN ('documented','probe_verified','field_failed')),
  evidence_valid_until  TEXT,
  effective_from        TEXT NOT NULL,
  effective_to          TEXT,
  created_at            TEXT NOT NULL,
  FOREIGN KEY (provider, channel) REFERENCES provider_channel,
  FOREIGN KEY (account_pool_id) REFERENCES provider_account_pool,
  UNIQUE (provider, channel, model_id, model_snapshot, region, account_pool_id,
          effective_from),
  CONSTRAINT snapshot_valid_range
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshot_bundle_rev
  ON model_capability_snapshot (capability_bundle_rev);
CREATE INDEX IF NOT EXISTS ix_snapshot_lookup
  ON model_capability_snapshot (provider, channel, model_id, model_snapshot, region)
  WHERE effective_to IS NULL;

-- §2.3 模式。一条 (snapshot, mode) 一行，不允许 supports_first_frame 这类
-- 全局 boolean —— 同一 model_id 换 model_snapshot 约束就变。
CREATE TABLE IF NOT EXISTS capability_mode (
  snapshot_id       TEXT NOT NULL
    REFERENCES model_capability_snapshot(snapshot_id),
  mode              TEXT NOT NULL,
  support           TEXT NOT NULL
    CHECK (support IN ('unsupported','documented','probe_verified','field_failed')),
  execution_kind    TEXT NOT NULL
    CHECK (execution_kind IN ('sync','poll','callback','hybrid')),
  prompt_required   INTEGER NOT NULL CHECK (prompt_required IN (0,1)),
  prompt_max_chars  INTEGER,
  native_audio      INTEGER NOT NULL CHECK (native_audio IN (0,1)),
  prompt_audio_lock INTEGER NOT NULL CHECK (prompt_audio_lock IN (0,1)),
  evidence_id       TEXT,
  PRIMARY KEY (snapshot_id, mode)
);

-- §2.3 参考输入规则：按模式、按角色给数量与互斥
CREATE TABLE IF NOT EXISTS mode_reference_rule (
  snapshot_id    TEXT NOT NULL,
  mode           TEXT NOT NULL,
  role           TEXT NOT NULL,
  min_count      INTEGER NOT NULL DEFAULT 0 CHECK (min_count >= 0),
  max_count      INTEGER NOT NULL DEFAULT 0 CHECK (max_count >= 0),
  ordering       TEXT NOT NULL DEFAULT 'unordered'
    CHECK (ordering IN ('unordered','ordered')),
  exclusive_with TEXT NOT NULL DEFAULT '[]',
  evidence_id    TEXT,
  PRIMARY KEY (snapshot_id, mode, role),
  CHECK (min_count <= max_count),
  FOREIGN KEY (snapshot_id, mode) REFERENCES capability_mode
);

-- §2.3 输出规格档位
CREATE TABLE IF NOT EXISTS mode_output_spec (
  snapshot_id      TEXT NOT NULL,
  mode             TEXT NOT NULL,
  aspect_ratio     TEXT NOT NULL,
  max_width        INTEGER CHECK (max_width IS NULL OR max_width > 0),
  max_height       INTEGER CHECK (max_height IS NULL OR max_height > 0),
  duration_tiers_s TEXT NOT NULL DEFAULT '[]',
  trims_to_nearest INTEGER NOT NULL DEFAULT 0 CHECK (trims_to_nearest IN (0,1)),
  PRIMARY KEY (snapshot_id, mode, aspect_ratio),
  FOREIGN KEY (snapshot_id, mode) REFERENCES capability_mode
);

-- §2.3 能力键
CREATE TABLE IF NOT EXISTS capability_claim (
  snapshot_id    TEXT NOT NULL,
  mode           TEXT NOT NULL,
  capability_key TEXT NOT NULL,
  support        TEXT NOT NULL
    CHECK (support IN ('unsupported','documented','probe_verified','field_failed')),
  params         TEXT NOT NULL DEFAULT '{}',
  evidence_id    TEXT,
  PRIMARY KEY (snapshot_id, mode, capability_key),
  FOREIGN KEY (snapshot_id, mode) REFERENCES capability_mode
);

-- §2.3 文件输入限制
CREATE TABLE IF NOT EXISTS mode_input_limit (
  snapshot_id    TEXT NOT NULL,
  mode           TEXT NOT NULL,
  media_kind     TEXT NOT NULL,
  max_bytes      INTEGER,
  max_pixels     INTEGER,
  max_duration_s TEXT,
  allowed_mime   TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (snapshot_id, mode, media_kind),
  FOREIGN KEY (snapshot_id, mode) REFERENCES capability_mode
);

-- §2.4 组合验证：布尔字段表达不了「首帧 + 主体 + 音频三者能否同时成立」，
-- 所以把具体输入形状规范化成 combination_key，留一条结论行。
CREATE TABLE IF NOT EXISTS mode_combination_probe (
  snapshot_id     TEXT NOT NULL,
  mode            TEXT NOT NULL,
  combination_key TEXT NOT NULL CHECK (length(combination_key) = 64),
  shape           TEXT NOT NULL,
  result          TEXT NOT NULL
    CHECK (result IN ('supported','unsupported','inconclusive')),
  -- §2.4 注释写「必须指向 capability_evidence」，但它的 DDL 只有到
  -- capability_mode 的外键。按注释加严：指向不存在证据的「支持」不能落库。
  evidence_id     TEXT NOT NULL REFERENCES capability_evidence(evidence_id),
  probed_at       TEXT NOT NULL,
  valid_until     TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, mode, combination_key),
  CHECK (valid_until > probed_at),
  FOREIGN KEY (snapshot_id, mode) REFERENCES capability_mode
);

-- §2.6 证据
CREATE TABLE IF NOT EXISTS capability_evidence (
  evidence_id     TEXT PRIMARY KEY,
  snapshot_id     TEXT,
  combination_key TEXT CHECK (combination_key IS NULL OR length(combination_key) = 64),
  method          TEXT NOT NULL
    CHECK (method IN ('doc_read','account_probe','paid_probe','field_observation')),
  result          TEXT NOT NULL
    CHECK (result IN ('supported','unsupported','inconclusive')),
  source_url      TEXT,
  fetched_at      TEXT NOT NULL,
  raw_ref         TEXT,
  account_pool_id TEXT,
  reviewer        TEXT,
  valid_until     TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

-- §2.5 价格快照
CREATE TABLE IF NOT EXISTS price_snapshot (
  price_snapshot_id    TEXT PRIMARY KEY CHECK (length(price_snapshot_id) = 64),
  tenant_id            TEXT NOT NULL DEFAULT '*',
  provider             TEXT NOT NULL,
  channel              TEXT NOT NULL,
  model_id             TEXT NOT NULL,
  model_snapshot       TEXT NOT NULL,
  region               TEXT NOT NULL,
  currency             TEXT NOT NULL CHECK (length(currency) = 3),
  tax_mode             TEXT NOT NULL CHECK (tax_mode IN ('inclusive','exclusive')),
  billing_units        TEXT NOT NULL,
  min_charge_micro     INTEGER NOT NULL DEFAULT 0 CHECK (min_charge_micro >= 0),
  failure_charge       TEXT NOT NULL
    CHECK (failure_charge IN ('none','full','partial','per_unit')),
  failure_charge_micro INTEGER NOT NULL DEFAULT 0 CHECK (failure_charge_micro >= 0),
  quotable             INTEGER NOT NULL CHECK (quotable IN (0,1)),
  margin_bps           INTEGER NOT NULL DEFAULT 1500 CHECK (margin_bps >= 0),
  valid_from           TEXT NOT NULL,
  valid_to             TEXT NOT NULL,
  source_ref           TEXT NOT NULL,
  source_hash          TEXT NOT NULL CHECK (length(source_hash) = 64),
  created_at           TEXT NOT NULL,
  -- §2.5 原话：没有请求费或 GPU 秒费的报价行说不清最低消费
  CHECK (json_type(billing_units, '$.request') IS NOT NULL
         OR json_type(billing_units, '$.gpu_second') IS NOT NULL),
  CHECK (valid_from < valid_to)
);
CREATE INDEX IF NOT EXISTS ix_price_lookup
  ON price_snapshot (provider, channel, model_id, model_snapshot, region, valid_to);

CREATE TABLE IF NOT EXISTS price_tier (
  price_snapshot_id TEXT NOT NULL REFERENCES price_snapshot(price_snapshot_id),
  dimension         TEXT NOT NULL
    CHECK (dimension IN ('duration_s','resolution_mp','duration_x_resolution')),
  from_inclusive    TEXT NOT NULL,
  to_exclusive      TEXT,
  unit_price_micro  INTEGER NOT NULL CHECK (unit_price_micro >= 0),
  PRIMARY KEY (price_snapshot_id, dimension, from_inclusive)
);

-- §2.6 生命周期迁移，全量进 S45
CREATE TABLE IF NOT EXISTS lifecycle_transition (
  snapshot_id TEXT NOT NULL
    REFERENCES model_capability_snapshot(snapshot_id),
  from_state  TEXT NOT NULL,
  to_state    TEXT NOT NULL,
  reason      TEXT NOT NULL,
  evidence_id TEXT,
  actor       TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, occurred_at, to_state)
);
"""


def schema_sql() -> str:
    return SCHEMA
