"""S08 自己的 schema。别的服务不 import 这里，只透过 HTTP 契约交互。

与 plans/S08-revision-snapshot.md §1 的 DDL 一一对应，只把 Postgres 特有写法
换成 SQLite：`timestamptz` → `TEXT`（ISO-8601 UTC）、`jsonb` → `TEXT`、
`text[]` → 逗号分隔的 `TEXT`、`char(64)` → `TEXT` 加 CHECK 长度。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS manifest (
  manifest_id        TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  project_id         TEXT NOT NULL,
  series_id          TEXT NOT NULL,
  episode_id         TEXT NOT NULL,
  branch_id          TEXT NOT NULL,
  home_cell          TEXT NOT NULL,
  current_revision   INTEGER NOT NULL CHECK (current_revision >= 1),
  accepted_revision  INTEGER,
  execution_fence    INTEGER NOT NULL CHECK (execution_fence >= 1),
  parent_manifest_id TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  lock_version       INTEGER NOT NULL DEFAULT 0,
  UNIQUE (tenant_id, series_id, episode_id, branch_id),
  -- 接受水位永不超前于当前版本
  CHECK (accepted_revision IS NULL OR accepted_revision <= current_revision)
);

CREATE INDEX IF NOT EXISTS manifest_project_idx
  ON manifest (tenant_id, project_id, current_revision);

CREATE TABLE IF NOT EXISTS manifest_revision (
  manifest_id       TEXT NOT NULL,
  revision          INTEGER NOT NULL CHECK (revision >= 1),
  tenant_id         TEXT NOT NULL,
  content_hash      TEXT NOT NULL CHECK (length(content_hash) = 64),
  parent_revision   INTEGER,
  base_revision     INTEGER NOT NULL,
  execution_fence   INTEGER NOT NULL,
  state             TEXT NOT NULL CHECK (state IN
                      ('COMMITTED','ACCEPTED','SUPERSEDED','VOIDED')),
  payload           TEXT NOT NULL,
  source_revisions  TEXT NOT NULL,
  change_plan_ref   TEXT,
  policy_decision_ref TEXT,
  committed_by      TEXT NOT NULL,
  committed_at      TEXT NOT NULL,
  PRIMARY KEY (manifest_id, revision),
  FOREIGN KEY (manifest_id) REFERENCES manifest (manifest_id),
  CHECK (parent_revision IS NULL OR parent_revision < revision)
);

-- 同 hash 不能出现在两个 revision 上：防重复提交的第二道闸（第一道是幂等）。
CREATE UNIQUE INDEX IF NOT EXISTS mr_hash_uniq
  ON manifest_revision (tenant_id, manifest_id, content_hash);

CREATE INDEX IF NOT EXISTS mr_state_idx
  ON manifest_revision (manifest_id, state, revision DESC);

CREATE TABLE IF NOT EXISTS node_state (
  manifest_id           TEXT NOT NULL,
  node_id               TEXT NOT NULL,
  tenant_id             TEXT NOT NULL,
  node_epoch            INTEGER NOT NULL CHECK (node_epoch >= 1),
  input_fingerprint     TEXT NOT NULL,
  locked                INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
  lock_reason           TEXT,
  accepted_artifact_ref TEXT,
  accepted_at_revision  INTEGER,
  accepted_at           TEXT,
  updated_at            TEXT NOT NULL,
  PRIMARY KEY (manifest_id, node_id),
  FOREIGN KEY (manifest_id) REFERENCES manifest (manifest_id),
  -- 指针与版本必须同时有值或同时为空
  CHECK ((accepted_artifact_ref IS NULL) = (accepted_at_revision IS NULL))
);

CREATE INDEX IF NOT EXISTS ns_accepted_idx
  ON node_state (tenant_id, manifest_id, accepted_at_revision);

CREATE TABLE IF NOT EXISTS acceptance_commit (
  commit_id        TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  manifest_id      TEXT NOT NULL,
  revision         INTEGER NOT NULL,
  node_ids         TEXT NOT NULL,              -- 空串 = 整集接受
  expected_revision INTEGER NOT NULL,
  expected_fence   INTEGER NOT NULL,
  decision_ref     TEXT NOT NULL,
  policy_ref       TEXT NOT NULL,
  qc_report_refs   TEXT NOT NULL,
  evidence_refs    TEXT NOT NULL,
  actor            TEXT NOT NULL,
  result           TEXT NOT NULL CHECK (result IN
                     ('ACCEPTED','REJECTED_STALE','REJECTED_EVIDENCE',
                      'REJECTED_POLICY','REJECTED_LOCKED')),
  reject_detail    TEXT,
  rebased_from_revision INTEGER,
  created_at       TEXT NOT NULL,
  FOREIGN KEY (manifest_id, revision)
    REFERENCES manifest_revision (manifest_id, revision)
);

CREATE INDEX IF NOT EXISTS ac_manifest_idx
  ON acceptance_commit (tenant_id, manifest_id, created_at DESC);

-- 证据只增不改：晚到的事件只是「多一条证据」，永远不推动 accepted pointer。
CREATE TABLE IF NOT EXISTS qc_evidence (
  tenant_id         TEXT NOT NULL,
  report_id         TEXT NOT NULL,
  report_revision   INTEGER NOT NULL,
  content_hash      TEXT NOT NULL DEFAULT '',
  manifest_id       TEXT NOT NULL,
  manifest_revision INTEGER NOT NULL,
  execution_fence   INTEGER NOT NULL,
  node_id           TEXT NOT NULL,
  node_epoch        INTEGER NOT NULL,
  artifact_hash     TEXT NOT NULL,
  received_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, report_id, report_revision, node_id)
);

CREATE INDEX IF NOT EXISTS qc_lookup_idx
  ON qc_evidence (tenant_id, manifest_id, node_id, artifact_hash);

CREATE TABLE IF NOT EXISTS review_evidence (
  tenant_id         TEXT NOT NULL,
  decision_id       TEXT NOT NULL,
  decision_revision INTEGER NOT NULL,
  content_hash      TEXT NOT NULL DEFAULT '',
  manifest_id       TEXT NOT NULL,
  manifest_revision INTEGER NOT NULL,
  execution_fence   INTEGER NOT NULL,
  verdict           TEXT NOT NULL CHECK (verdict IN ('approved','rejected')),
  node_epochs       TEXT NOT NULL,
  artifact_hashes   TEXT NOT NULL,
  received_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, decision_id, decision_revision)
);

CREATE TABLE IF NOT EXISTS branch_state (
  tenant_id       TEXT NOT NULL,
  series_id       TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  execution_fence INTEGER NOT NULL CHECK (execution_fence >= 1),
  fence_reason    TEXT,
  fence_ref       TEXT,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id)
);
"""

TOPIC = "drama.revision-snapshot.v2"
EVENT_TYPE = "snapshot.manifest.committed"

VALID_REVISION_STATES = ("COMMITTED", "ACCEPTED", "SUPERSEDED", "VOIDED")
