"""S09 自己的 schema。别的服务不 import 这里，只透过 HTTP 契约交互。

与 plans/S09-lineage-impact.md §2 的 DDL 一一对应，只把 Postgres 特有写法换成
SQLite：

- `timestamptz` → `TEXT`（ISO-8601 UTC）
- `jsonb` → `TEXT`（JCS 文本）
- `text[]` → `TEXT`（JSON 数组文本）
- `bigint` → `INTEGER`
- `~ '^(/[^/]*)+$'` → SQLite 没有正则，路径形态改在 service 层校验
  （`_check_field_path`），库里只保留能表达的长度与前缀约束
- plpgsql 触发器 → SQLite 的 `BEFORE INSERT` 触发器 + `RAISE(ABORT)`

白名单仍然**在数据库里**执行，不是代码约定：绕过 service 直接 INSERT 一个
非白名单标量同样会被拒绝。这一条是 §2.2 的原意，换实现不能换掉它。
"""

EDGE_KINDS = (
    "PACK_USES_CLAIM",
    "SCRIPT_DERIVED_FROM_PACK",
    "SCENE_DERIVED_FROM_SCRIPT",
    "SHOT_DERIVED_FROM_SCENE",
    "SHOT_FIELD_DEPENDS",
    "ARTIFACT_FROM_SHOT",
    "ASSET_BINDING",
    "MEMORY_DEPENDS",
    "AUDIO_FROM_DIALOGUE",
    "SUBTITLE_FROM_AUDIO",
    "EDIT_FROM_ARTIFACT",
)

EDGE_ORIGINS = ("EVENT_DERIVED", "SELF_REPORTED", "BACKFILL")

ANALYSIS_MODES = ("EXACT", "CONSERVATIVE")

ANALYSIS_STATUSES = ("COMPUTED", "INCOMPLETE", "FAILED", "STALE_FENCE", "EXPIRED")

DISPOSITIONS = ("REGENERATE", "RECHECK", "REUSE")

REASON_CODES = (
    "DIRECT_FIELD_CHANGED", "UPSTREAM_REGENERATED", "ASSET_BINDING_CHANGED",
    "CLAIM_SUPERSEDED", "MEMORY_CHECKPOINT_MOVED", "DURATION_DERIVED",
    "ORDER_CHANGED", "AUDIO_LENGTH_CHANGED", "TIMECODE_CHANGED",
    "REUSABLE_TEXT_UNCHANGED", "REUSABLE_FIRST_FRAME_UNCHANGED",
    "CONSERVATIVE_OVERAPPROX", "CONSERVATIVE_UPGRADE",
)

GAP_SEVERITIES = ("BLOCKING", "DEGRADED")

# §2.2：六个结构性标量。加字段必须改这张表并写清理由，不许在别处偷放。
WHITELIST = (
    ("S06", "/camera/angle", "string", "分镜机位角，用于判定是否重做关键帧"),
    ("S06", "/duration/target_seconds", "number", "目标时长，用于 TTS 复用判定"),
    ("S06", "/duration/trim_allowed", "boolean", "时长可裁性"),
    ("S06", "/duration/timing_policy", "string", "时序策略枚举"),
    ("S06", "/order", "number", "镜头顺序"),
    ("S04", "/scene_count", "number", "场次总数，仅结构统计"),
)

DDL_HEAD = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS lineage_edge (
  tenant_id         TEXT NOT NULL,
  branch_id         TEXT NOT NULL,
  edge_id           TEXT NOT NULL,
  edge_kind         TEXT NOT NULL,
  source_service_id TEXT NOT NULL,
  source_id         TEXT NOT NULL,
  source_revision   INTEGER NOT NULL CHECK (source_revision >= 1),
  source_hash       TEXT NOT NULL CHECK (length(source_hash) = 64),
  target_service_id TEXT NOT NULL,
  target_id         TEXT NOT NULL,
  target_revision   INTEGER NOT NULL CHECK (target_revision >= 1),
  target_hash       TEXT NOT NULL CHECK (length(target_hash) = 64),
  field_path        TEXT,
  origin            TEXT NOT NULL CHECK (origin IN
                      ('EVENT_DERIVED','SELF_REPORTED','BACKFILL')),
  origin_event_id   TEXT,
  valid_from        INTEGER NOT NULL CHECK (valid_from >= 0),
  valid_to          INTEGER,
  created_at        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, edge_id),
  -- 路径形态的正则在 service 层；这里保住前缀与长度这两件 SQLite 能表达的事。
  CONSTRAINT edge_field_path_shape CHECK (
    field_path IS NULL
    OR (substr(field_path, 1, 1) = '/' AND length(field_path) BETWEEN 2 AND 256)),
  CONSTRAINT edge_valid_range CHECK (valid_to IS NULL OR valid_to > valid_from),
  CONSTRAINT edge_not_self CHECK (
    NOT (source_service_id = target_service_id
         AND source_id = target_id
         AND source_revision = target_revision))
);

-- 向下求闭包（最热）：按 source 找 target
CREATE INDEX IF NOT EXISTS lineage_edge_down_idx
  ON lineage_edge (tenant_id, branch_id, edge_kind, source_service_id, source_id,
                   source_revision)
  WHERE valid_to IS NULL;

-- 向上溯源：按 target 找 source
CREATE INDEX IF NOT EXISTS lineage_edge_up_idx
  ON lineage_edge (tenant_id, branch_id, edge_kind, target_service_id, target_id,
                   target_revision)
  WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS lineage_edge_field_idx
  ON lineage_edge (tenant_id, branch_id, target_service_id, target_id, field_path)
  WHERE field_path IS NOT NULL AND valid_to IS NULL;

-- 同一条边在一个水位只能有一条生效记录
CREATE UNIQUE INDEX IF NOT EXISTS lineage_edge_active_uniq ON lineage_edge (
  tenant_id, branch_id, edge_kind, source_service_id, source_id, source_revision,
  target_service_id, target_id, target_revision, coalesce(field_path, '')
) WHERE valid_to IS NULL;

-- 事件来源去重（同一事件重放不产生第二条边）
CREATE UNIQUE INDEX IF NOT EXISTS lineage_edge_origin_uniq
  ON lineage_edge (tenant_id, origin_event_id)
  WHERE origin_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS scalar_projection_whitelist (
  service_id  TEXT NOT NULL,
  field_path  TEXT NOT NULL,
  value_type  TEXT NOT NULL,
  note        TEXT NOT NULL,
  PRIMARY KEY (service_id, field_path)
);

CREATE TABLE IF NOT EXISTS field_projection (
  tenant_id    TEXT NOT NULL,
  branch_id    TEXT NOT NULL,
  service_id   TEXT NOT NULL,
  object_id    TEXT NOT NULL,
  revision     INTEGER NOT NULL CHECK (revision >= 1),
  field_path   TEXT NOT NULL,
  value_type   TEXT NOT NULL CHECK (value_type IN
                 ('string','number','boolean','array','object','null')),
  value_hash   TEXT NOT NULL CHECK (length(value_hash) = 64),
  scalar_value TEXT,
  projected_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, branch_id, service_id, object_id, revision, field_path),
  CONSTRAINT fp_path_shape CHECK (substr(field_path, 1, 1) = '/')
);

CREATE INDEX IF NOT EXISTS fp_lookup_idx ON field_projection
  (tenant_id, service_id, object_id, revision);

-- 白名单在库里执行：非白名单字段带 scalar_value 一律写不进去。
CREATE TRIGGER IF NOT EXISTS fp_whitelist_insert
BEFORE INSERT ON field_projection
FOR EACH ROW WHEN NEW.scalar_value IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'SCALAR_PROJECTION_NOT_ALLOWED')
   WHERE NOT EXISTS (
     SELECT 1 FROM scalar_projection_whitelist w
      WHERE w.service_id = NEW.service_id AND w.field_path = NEW.field_path);
END;

CREATE TRIGGER IF NOT EXISTS fp_whitelist_update
BEFORE UPDATE ON field_projection
FOR EACH ROW WHEN NEW.scalar_value IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'SCALAR_PROJECTION_NOT_ALLOWED')
   WHERE NOT EXISTS (
     SELECT 1 FROM scalar_projection_whitelist w
      WHERE w.service_id = NEW.service_id AND w.field_path = NEW.field_path);
END;

CREATE TABLE IF NOT EXISTS impact_analysis (
  tenant_id         TEXT NOT NULL,
  analysis_id       TEXT NOT NULL,
  branch_id         TEXT NOT NULL,
  series_id         TEXT NOT NULL,
  base_manifest_ref TEXT NOT NULL,
  execution_fence   INTEGER NOT NULL CHECK (execution_fence >= 1),
  seed_kind         TEXT NOT NULL,
  seed_ref          TEXT NOT NULL,
  request_hash      TEXT NOT NULL,
  mode              TEXT NOT NULL CHECK (mode IN ('EXACT','CONSERVATIVE')),
  status            TEXT NOT NULL CHECK (status IN
                      ('COMPUTED','INCOMPLETE','FAILED','STALE_FENCE','EXPIRED')),
  lineage_watermark INTEGER NOT NULL CHECK (lineage_watermark >= 0),
  watermark_source  TEXT NOT NULL,
  gap_ids           TEXT,
  result            TEXT,
  analysis_hash     TEXT,
  expires_at        TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  completed_at      TEXT,
  PRIMARY KEY (tenant_id, analysis_id),
  CONSTRAINT ia_incomplete_has_gaps CHECK ((status = 'INCOMPLETE') = (gap_ids IS NOT NULL)),
  CONSTRAINT ia_computed_has_hash CHECK (
    (status IN ('COMPUTED','EXPIRED')) = (analysis_hash IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS ia_idempotent_uniq
  ON impact_analysis (tenant_id, request_hash);
CREATE INDEX IF NOT EXISTS ia_by_branch_idx
  ON impact_analysis (tenant_id, branch_id, created_at DESC);

CREATE TABLE IF NOT EXISTS impact_analysis_node (
  tenant_id    TEXT NOT NULL,
  analysis_id  TEXT NOT NULL,
  node_id      TEXT NOT NULL,
  disposition  TEXT NOT NULL CHECK (disposition IN
                 ('REGENERATE','RECHECK','REUSE')),
  reason_code  TEXT NOT NULL,
  path         TEXT NOT NULL,
  evidence     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, analysis_id, node_id)
);

CREATE INDEX IF NOT EXISTS ian_by_analysis_disp_idx
  ON impact_analysis_node (tenant_id, analysis_id, disposition);

CREATE TABLE IF NOT EXISTS lineage_gap (
  tenant_id         TEXT NOT NULL,
  gap_id            TEXT NOT NULL,
  branch_id         TEXT NOT NULL,
  expected_kind     TEXT NOT NULL,
  anchor_ref        TEXT NOT NULL,
  detected_by       TEXT NOT NULL CHECK (detected_by IN
                      ('CLOSURE','RECONCILE','BACKFILL')),
  severity          TEXT NOT NULL CHECK (severity IN ('BLOCKING','DEGRADED')),
  detail            TEXT NOT NULL DEFAULT '',
  resolved_at       TEXT,
  resolved_edge_ids TEXT,
  first_seen_at     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, gap_id),
  CONSTRAINT gap_resolved_consistency CHECK (
    (resolved_at IS NULL) = (resolved_edge_ids IS NULL))
);

CREATE INDEX IF NOT EXISTS gap_open_idx
  ON lineage_gap (tenant_id, branch_id, severity) WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS projection_watermark (
  tenant_id            TEXT NOT NULL,
  branch_id            TEXT NOT NULL,
  source_service       TEXT NOT NULL,
  applied_sequence     INTEGER NOT NULL CHECK (applied_sequence >= 0),
  applied_event_id     TEXT,
  -- 最近一次观测到的源侧 sequence（来自事件自身或对账回源），不是「源的真值」：
  -- 只用来在同步闭包里判 backlog，丢了这个列就只能靠猜。
  source_head_sequence INTEGER NOT NULL DEFAULT 0 CHECK (source_head_sequence >= 0),
  lag_budget_ms        INTEGER NOT NULL DEFAULT 2000,
  -- 最近一次对账量到的落后毫秒数。与预算分列：混用一列会让「预算 2s」
  -- 在某次写入后变成「落后 2s」，水位判定就反了。
  observed_lag_ms      INTEGER NOT NULL DEFAULT 0 CHECK (observed_lag_ms >= 0),
  updated_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, branch_id, source_service)
);

-- 节点身份索引：只存引用，不存内容。由 snapshot.manifest.committed 喂，
-- 闭包靠它把边的 (service,id,revision) 落回 manifest 里的 node_id。
-- 这不是「第二份 manifest」——S09 不回答「这一集现在是什么」，只记
-- 「这个 revision 在这份 manifest 里叫哪个节点」。
CREATE TABLE IF NOT EXISTS node_ref (
  tenant_id        TEXT NOT NULL,
  branch_id        TEXT NOT NULL,
  manifest_id      TEXT NOT NULL,
  manifest_revision INTEGER NOT NULL CHECK (manifest_revision >= 1),
  node_id          TEXT NOT NULL,
  service_id       TEXT NOT NULL,
  object_id        TEXT NOT NULL,
  object_revision  INTEGER NOT NULL CHECK (object_revision >= 1),
  content_hash     TEXT NOT NULL,
  execution_fence  INTEGER NOT NULL,
  episode_id       TEXT NOT NULL DEFAULT '',
  binding_set_ref  TEXT,
  -- 节点自己声明的资产引用（JSON 数组文本）。只用来判"这里本该有一条
  -- ASSET_BINDING 边"——§2.5 的 BLOCKING 例子就是这一种。
  asset_refs       TEXT NOT NULL DEFAULT '[]',
  -- 节点声明的「这里本该有哪些入边」：`[{edge_kind, service_id, id, severity}]`。
  -- 缺边检测的唯一依据。没有这张声明表，"缺边"就只能靠猜哪类节点该有下游，
  -- 而猜错的两个方向都危险：多猜会把正常叶子报成阻断，少猜会静默漏节点。
  expected_edges   TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (tenant_id, branch_id, manifest_id, manifest_revision, node_id)
);

CREATE INDEX IF NOT EXISTS node_ref_object_idx
  ON node_ref (tenant_id, branch_id, service_id, object_id, object_revision);
CREATE INDEX IF NOT EXISTS node_ref_manifest_idx
  ON node_ref (tenant_id, branch_id, manifest_id, manifest_revision);

CREATE TABLE IF NOT EXISTS manifest_head (
  tenant_id       TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  manifest_id     TEXT NOT NULL,
  revision        INTEGER NOT NULL CHECK (revision >= 1),
  execution_fence INTEGER NOT NULL,
  series_id       TEXT NOT NULL DEFAULT '',
  episode_id      TEXT NOT NULL DEFAULT '',
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, branch_id, manifest_id)
);

CREATE TABLE IF NOT EXISTS memory_change (
  tenant_id      TEXT NOT NULL,
  branch_id      TEXT NOT NULL,
  commit_sequence INTEGER NOT NULL,
  event_id       TEXT NOT NULL,
  payload        TEXT NOT NULL,
  received_at    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, branch_id, commit_sequence)
);
"""


def schema_sql() -> str:
    return DDL_HEAD


def whitelist_insert_sql() -> str:
    return (
        "INSERT OR IGNORE INTO scalar_projection_whitelist "
        "(service_id, field_path, value_type, note) VALUES (?, ?, ?, ?)"
    )
