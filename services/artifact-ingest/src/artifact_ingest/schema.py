"""S28 自己的 schema。别的服务不 import 这里，只透过 HTTP 契约交互。

与 plans/S28-artifact-ingest.md §2 的 DDL 一一对应，只把 Postgres 特有写法换成
SQLite：`timestamptz` → `TEXT`（ISO-8601 UTC）、`jsonb` → `TEXT`、`char(64)` →
`TEXT` 加 CHECK 长度、`bytea` → `BLOB`、`boolean` → `INTEGER` 加 CHECK。

一处**加列**：`artifact.scope_ref`。§4.4 的去重可见性规则要求「调用方对已有
artifact 所属 scope 有读权限」才允许 `dedup.hit=true`，但 §2.1 只给了 `owner_ref`、
没有 scope 列，§2.4 的 `artifact_import` 也没有。不存 scope，E28 的可见性规则
无从实现。已登记为 C28-13，等 owner 决定是加列还是另设 scope 表。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS artifact (
  artifact_id      TEXT PRIMARY KEY,                  -- art_<ulid>
  tenant_id        TEXT NOT NULL,
  home_region      TEXT NOT NULL,
  home_cell        TEXT NOT NULL,

  owner_ref        TEXT,                              -- JSON {"service_id":"S06","id":"shot_..."} 不透明
  scope_ref        TEXT,                              -- JSON {"project_id":...}；见模块 docstring 的 C28-13
  retention_policy_ref TEXT,                          -- §2.1 有这个字段；导入时从 retention 拷入
  content_class    TEXT NOT NULL
    CHECK (content_class IN ('generated_video','generated_image','generated_audio',
                             'rendered_master','proxy','thumbnail','waveform',
                             'subtitle_track','audio_stem','uploaded_reference',
                             'evidence','model_archive')),

  current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
  state            TEXT NOT NULL DEFAULT 'PENDING_PUBLISH'
    CHECK (state IN ('PENDING_PUBLISH','AVAILABLE','TOMBSTONED','DELETED')),

  legal_hold       INTEGER NOT NULL DEFAULT 0 CHECK (legal_hold IN (0,1)),

  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  tombstoned_at    TEXT,
  deleted_at       TEXT,

  -- 状态与时间戳必须一致，否则「TOMBSTONED 但没时间」会让审计说不清何时停用
  CHECK (state <> 'TOMBSTONED' OR tombstoned_at IS NOT NULL),
  CHECK (state <> 'DELETED'    OR deleted_at   IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_artifact_tenant_class
  ON artifact (tenant_id, content_class, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_artifact_tombstone_pending
  ON artifact (tenant_id, tombstoned_at) WHERE state = 'TOMBSTONED';

CREATE TABLE IF NOT EXISTS artifact_revision (
  artifact_id      TEXT NOT NULL REFERENCES artifact(artifact_id),
  revision         INTEGER NOT NULL CHECK (revision >= 1),
  tenant_id        TEXT NOT NULL,
  content_hash     TEXT NOT NULL CHECK (length(content_hash) = 64),
  size_bytes       INTEGER NOT NULL CHECK (size_bytes >= 0),

  -- 可测量元数据：允许为空（未探测），一旦写入不可修改
  mime_type        TEXT,
  container        TEXT,
  duration_ms      INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  width_px         INTEGER CHECK (width_px IS NULL OR width_px > 0),
  height_px        INTEGER CHECK (height_px IS NULL OR height_px > 0),
  frame_rate_num   INTEGER CHECK (frame_rate_num IS NULL OR frame_rate_num > 0),
  frame_rate_den   INTEGER CHECK (frame_rate_den IS NULL OR frame_rate_den > 0),
  audio_channels   INTEGER,
  sample_rate_hz   INTEGER,

  storage_key      TEXT NOT NULL,
  storage_class    TEXT NOT NULL DEFAULT 'STANDARD'
    CHECK (storage_class IN ('STANDARD','INFREQUENT','ARCHIVE')),
  storage_region   TEXT NOT NULL,

  source_import_id TEXT NOT NULL,
  probe_ok         INTEGER NOT NULL DEFAULT 0 CHECK (probe_ok IN (0,1)),
  published_at     TEXT NOT NULL,

  PRIMARY KEY (artifact_id, revision),
  -- 同一身份的同一份字节不会有两个 revision
  UNIQUE (artifact_id, content_hash)
);

CREATE INDEX IF NOT EXISTS ix_rev_hash
  ON artifact_revision (tenant_id, content_hash);

CREATE TABLE IF NOT EXISTS content_blob (
  blob_id        TEXT PRIMARY KEY,                    -- blob_<ulid>
  tenant_id      TEXT NOT NULL,
  content_hash   TEXT NOT NULL CHECK (length(content_hash) = 64),
  size_bytes     INTEGER NOT NULL CHECK (size_bytes >= 0),
  storage_key    TEXT NOT NULL,
  storage_region TEXT NOT NULL,
  storage_class  TEXT NOT NULL DEFAULT 'STANDARD',
  state          TEXT NOT NULL DEFAULT 'LIVE'
    CHECK (state IN ('LIVE','PENDING_DELETE','DELETED')),
  ref_count      INTEGER NOT NULL DEFAULT 0 CHECK (ref_count >= 0),
  created_at     TEXT NOT NULL,
  verified_at    TEXT,

  -- E28 的数据库级落点：唯一索引前缀是 tenant_id，没有全局 hash 唯一索引
  UNIQUE (tenant_id, content_hash)
);

CREATE TABLE IF NOT EXISTS artifact_import (
  import_id          TEXT PRIMARY KEY,                -- imp_<ulid>
  tenant_id          TEXT NOT NULL,
  command_id         TEXT NOT NULL,
  state              TEXT NOT NULL,

  content_class      TEXT NOT NULL,
  target_artifact_id TEXT,
  expected_sha256    TEXT
    CHECK (expected_sha256 IS NULL OR length(expected_sha256) = 64),
  expected_revision  INTEGER,

  declared_size_bytes INTEGER CHECK (declared_size_bytes IS NULL OR declared_size_bytes >= 0),
  observed_size_bytes INTEGER CHECK (observed_size_bytes IS NULL OR observed_size_bytes >= 0),
  actual_sha256      TEXT CHECK (actual_sha256 IS NULL OR length(actual_sha256) = 64),

  mime_declared      TEXT,
  mime_detected      TEXT,
  probe_result       TEXT,

  -- 归属声明：去重可见性（§4.4）与审计都靠它，§2.4 漏了这两列
  owner_ref          TEXT,
  scope_ref          TEXT,
  -- §6.1 的 retention.policy_ref，§2.4 同样没有对应列；丢了它 §6.2 的
  -- retention.policy_ref 永远只能是 null（E39）
  retention_policy_ref TEXT,

  source_kind        TEXT NOT NULL
    CHECK (source_kind IN ('provider_url','upload_session','internal_object','derived')),
  source_ref         TEXT,
  -- §6.8 的 source.attempt_id，§2.4 也没有列（source_ref 只够放一个 id）。见 C28-16。
  attempt_id         TEXT,
  url_host           TEXT,
  url_expires_at     TEXT,
  fetch_started_at   TEXT,
  fetch_finished_at  TEXT,

  reject_code        TEXT,
  reject_detail      TEXT,
  attempts           INTEGER NOT NULL DEFAULT 0,
  next_action_at     TEXT,

  staging_key        TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  terminal_at        TEXT,

  UNIQUE (tenant_id, command_id),
  CHECK (observed_size_bytes IS NULL OR observed_size_bytes <= 214748364800)
);

CREATE INDEX IF NOT EXISTS ix_import_due
  ON artifact_import (next_action_at) WHERE terminal_at IS NULL;

-- §2.6。派生结果本身是 artifact（有自己的 hash/revision），这张表只记派生关系。
-- 本轮不产出派生（§4.6 依赖无网沙箱转码），但表在：`GET /artifacts/{id}` 的
-- `derivatives` 必须是真查询，不能是硬编码的空数组。
CREATE TABLE IF NOT EXISTS artifact_derivative (
  derivative_id      TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  source_artifact_id TEXT NOT NULL,
  source_revision    INTEGER NOT NULL,
  kind               TEXT NOT NULL
    CHECK (kind IN ('proxy','thumbnail','waveform','contact_sheet')),
  artifact_id        TEXT NOT NULL REFERENCES artifact(artifact_id),
  state              TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (state IN ('PENDING','READY','FAILED','SKIPPED')),
  reason_code        TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (tenant_id, source_artifact_id, source_revision, kind)
);

CREATE TABLE IF NOT EXISTS media_access_ticket (
  ticket_id      TEXT PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  artifact_id    TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  principal_id   TEXT NOT NULL,
  principal_type TEXT NOT NULL
    CHECK (principal_type IN ('human','service','api_client','mcp_client')),
  purpose        TEXT NOT NULL
    CHECK (purpose IN ('playback','qc','delivery','train','audit','download')),
  scope_ref      TEXT,
  issued_at      TEXT NOT NULL,
  expires_at     TEXT NOT NULL,
  max_bytes      INTEGER,
  revoked_at     TEXT,
  CHECK (expires_at > issued_at)
);

CREATE INDEX IF NOT EXISTS ix_ticket_active
  ON media_access_ticket (tenant_id, artifact_id, expires_at)
  WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS artifact_tombstone (
  artifact_id  TEXT PRIMARY KEY REFERENCES artifact(artifact_id),
  tenant_id    TEXT NOT NULL,
  reason_code  TEXT NOT NULL
    CHECK (reason_code IN ('USER_REQUEST','RETENTION_EXPIRED','LEGAL','SECURITY','ORPHAN')),
  requested_by TEXT NOT NULL,
  requested_via TEXT NOT NULL,
  effective_at TEXT NOT NULL,
  note         TEXT
);

CREATE TABLE IF NOT EXISTS retention_binding (
  binding_id   TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  artifact_id  TEXT NOT NULL REFERENCES artifact(artifact_id),
  policy_ref   TEXT NOT NULL,
  retain_until TEXT,
  legal_hold   INTEGER NOT NULL DEFAULT 0 CHECK (legal_hold IN (0,1)),
  hold_reason  TEXT,
  hold_ref     TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,

  UNIQUE (tenant_id, artifact_id),
  CHECK (legal_hold = 0 OR hold_ref IS NOT NULL)
);
"""

TOPIC = "drama.artifact-ingest.v2"
EVENT_TYPE = "artifact.ingested"

CONTENT_CLASSES = (
    "generated_video", "generated_image", "generated_audio", "rendered_master",
    "proxy", "thumbnail", "waveform", "subtitle_track", "audio_stem",
    "uploaded_reference", "evidence", "model_archive",
)

SOURCE_KINDS = ("provider_url", "upload_session", "internal_object", "derived")

DERIVATIVE_KINDS = ("proxy", "thumbnail", "waveform", "contact_sheet")

PURPOSES = ("playback", "qc", "delivery", "train", "audit", "download")

# §3.1 终态集合。FETCH_FAILED 是「本次命令结束」而非「制品不可达」。
TERMINAL_IMPORT_STATES = (
    "PUBLISHED", "REJECTED_SECURITY", "REJECTED_SIZE", "HASH_MISMATCH",
    "URL_REJECTED", "URL_EXPIRED", "FETCH_FAILED", "FETCH_BLOCKED",
    "PUBLISH_FAILED",
)
