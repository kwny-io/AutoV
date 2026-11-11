"""S21 自己的 schema。别的服务不 import 这里，只透过 HTTP 契约交互。

与 plans/S21-orchestration.md §1 的 DDL 一一对应，Postgres 写法换 SQLite：

- `timestamptz` → `TEXT`（固定宽度 27 字符 ISO-8601 UTC，见 service 的 `_ts`）
- `jsonb` → `TEXT`（JCS 文本）
- `text[]` → `TEXT`（JCS 文本的数组）
- `bigint` / `integer` → `INTEGER`（SQLite 的 INTEGER 是 64 位）
- 部分唯一索引与部分索引 SQLite 都支持，原样保留

**这里没有执行状态表**（§1.9）。`workflow_binding` / `run_projection` /
`run_checkpoint` / `node_in_flight` / `run_command` / `repair_budget` 都是
"业务投影与操作痕迹"，不是 workflow 的状态机；执行 history 归 Temporal。
`node_blocker` 是 §5.4 的 `/blockers` 与 E06 失败报告要求持久化的事实表，
方案 §1 漏列（C21-15）。

`outbox` / `inbox` / `idempotency_record` 不在这里声明：平台 SDK 建表并写
（C21-16 / C21-18）。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §1.1 Run ↔ Temporal 的绑定。
--
-- `run_generation` 是 Continue-As-New 代数；`business_idem_key` 跨代恒定。
CREATE TABLE IF NOT EXISTS workflow_binding (
  tenant_id                  TEXT NOT NULL,
  run_id                     TEXT NOT NULL,
  workflow_id                TEXT NOT NULL,
  run_generation             INTEGER NOT NULL,
  namespace                  TEXT NOT NULL,
  task_queue                 TEXT NOT NULL,
  workflow_type              TEXT NOT NULL,
  business_idem_key          TEXT NOT NULL,
  series_id                  TEXT NOT NULL,
  episode_id                 TEXT NOT NULL,
  branch_id                  TEXT NOT NULL,
  manifest_id                TEXT NOT NULL,
  manifest_revision_at_start INTEGER NOT NULL,
  execution_fence_at_start   INTEGER NOT NULL,
  automation_mode            TEXT NOT NULL,
  policy_ref                 TEXT NOT NULL,
  budget_scope_id            TEXT NOT NULL,
  status                     TEXT NOT NULL,
  created_at                 TEXT NOT NULL,
  closed_at                  TEXT,

  PRIMARY KEY (tenant_id, run_id),
  CONSTRAINT wb_generation_positive CHECK (run_generation >= 1),
  CONSTRAINT wb_status_closed CHECK (status IN ('ACTIVE','COMPLETED','TERMINATED')),
  CONSTRAINT wb_closed_has_ts CHECK ((status <> 'ACTIVE') = (closed_at IS NOT NULL)),
  CONSTRAINT wb_mode_closed CHECK (automation_mode IN
    ('guided','stage_review','exception_assisted','unattended_batch'))
);
CREATE UNIQUE INDEX IF NOT EXISTS wb_workflow_uniq
  ON workflow_binding (namespace, workflow_id, run_generation);

-- 业务幂等：一个 (tenant, business_idem_key) 只能有一个 ACTIVE Run。
-- 取消后重建靠这条部分索引：旧 Run 是 TERMINATED，不在索引里（§6.1）。
CREATE UNIQUE INDEX IF NOT EXISTS wb_business_active_uniq
  ON workflow_binding (tenant_id, business_idem_key)
  WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS wb_by_episode_idx
  ON workflow_binding (tenant_id, series_id, branch_id, episode_id, created_at DESC);
CREATE INDEX IF NOT EXISTS wb_active_idx ON workflow_binding (tenant_id, status)
  WHERE status = 'ACTIVE';

-- §1.2 只读业务投影。11 个状态与 openapi `Run.state` 完全一致。
CREATE TABLE IF NOT EXISTS run_projection (
  tenant_id            TEXT NOT NULL,
  run_id               TEXT NOT NULL,
  state                TEXT NOT NULL,
  manifest_revision    INTEGER NOT NULL,
  execution_fence      INTEGER NOT NULL,
  completed_nodes      INTEGER NOT NULL DEFAULT 0,
  blocked_nodes        INTEGER NOT NULL DEFAULT 0,
  failed_nodes         INTEGER NOT NULL DEFAULT 0,
  waiting_reason       TEXT,
  blocked_reason       TEXT,
  current_stage        TEXT,
  projection_watermark INTEGER NOT NULL,
  last_event_at        TEXT NOT NULL,
  terminal_at          TEXT,
  version              INTEGER NOT NULL DEFAULT 1,

  PRIMARY KEY (tenant_id, run_id),
  CONSTRAINT rp_state_closed CHECK (state IN (
    'QUEUED','RUNNING','WAITING_INPUT','WAITING_REVIEW','WAITING_MEMORY',
    'WAITING_BUDGET','PAUSED','SUCCEEDED','INCOMPLETE','FAILED','CANCELED')),
  CONSTRAINT rp_terminal_has_ts CHECK (
    (state IN ('SUCCEEDED','INCOMPLETE','FAILED','CANCELED')) = (terminal_at IS NOT NULL)),
  CONSTRAINT rp_waiting_has_reason CHECK (
    (state IN ('WAITING_INPUT','WAITING_REVIEW','WAITING_MEMORY','WAITING_BUDGET'))
      = (waiting_reason IS NOT NULL)),
  CONSTRAINT rp_counts_nonneg CHECK (
    completed_nodes >= 0 AND blocked_nodes >= 0 AND failed_nodes >= 0),
  CONSTRAINT rp_watermark_positive CHECK (projection_watermark >= 1)
);
CREATE INDEX IF NOT EXISTS rp_active_idx ON run_projection (tenant_id, state)
  WHERE terminal_at IS NULL;
CREATE INDEX IF NOT EXISTS rp_by_branch_idx
  ON run_projection (tenant_id, state, last_event_at DESC);

-- §1.3 Continue-As-New 跨代的业务检查点。不替代 Temporal history。
CREATE TABLE IF NOT EXISTS run_checkpoint (
  tenant_id              TEXT NOT NULL,
  run_id                 TEXT NOT NULL,
  run_generation         INTEGER NOT NULL,
  checkpoint_seq         INTEGER NOT NULL,
  stage                  TEXT NOT NULL,
  completed_node_ids     TEXT NOT NULL DEFAULT '[]',
  in_flight_node_ids     TEXT NOT NULL DEFAULT '[]',
  pending_node_ids       TEXT NOT NULL DEFAULT '[]',
  budget_reserved_micro  INTEGER NOT NULL DEFAULT 0,
  spent_micro            INTEGER NOT NULL DEFAULT 0,
  provider_job_refs      TEXT NOT NULL DEFAULT '[]',
  continuation_payload   TEXT,
  created_at             TEXT NOT NULL,

  PRIMARY KEY (tenant_id, run_id, run_generation, checkpoint_seq),
  CONSTRAINT rc_money_nonneg CHECK (budget_reserved_micro >= 0 AND spent_micro >= 0)
);
CREATE INDEX IF NOT EXISTS rc_latest_idx
  ON run_checkpoint (tenant_id, run_id, run_generation DESC, checkpoint_seq DESC);

-- §1.5 在途节点索引。**不是执行状态的权威**，只回答"能不能取消"。
-- `node_epoch` / `execution_fence` 是派单时读到的**快照**，不参与判定。
--
-- `disposition` 与 `job_state` 是方案 §4.2/§4.5 要求落盘、但 §1.5 的 DDL 漏列
-- 的两列（C21-14）：
--
-- - `disposition`：取消时要把每项分成 CANCELED 与 DISCARD_ON_COMPLETION，
--   否则恢复/对账时无法知道"这条为什么还开着"。
-- - `job_state`：T21-24 要求"`SUBMISSION_UNKNOWN` 的 job 不调取消接口"，
--   而 §4.5 又说 "S21 不猜"——两句话只能靠本地记下上次从 S26 事件里读到的
--   状态来同时满足。它是**快照**，不是 S26 的权威。
CREATE TABLE IF NOT EXISTS node_in_flight (
  tenant_id       TEXT NOT NULL,
  run_id          TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  node_epoch      INTEGER NOT NULL,
  execution_fence INTEGER NOT NULL,
  stage           TEXT NOT NULL,
  provider_job_id TEXT,
  attempt_id      TEXT,
  job_state       TEXT,
  disposition     TEXT,
  dispatched_at   TEXT NOT NULL,
  settled_at      TEXT,

  PRIMARY KEY (tenant_id, run_id, node_id, node_epoch),
  CONSTRAINT nif_fence_positive CHECK (execution_fence >= 1),
  CONSTRAINT nif_epoch_positive CHECK (node_epoch >= 1),
  CONSTRAINT nif_disposition_closed CHECK (
    disposition IS NULL OR disposition IN ('CANCELED','DISCARD_ON_COMPLETION'))
);
CREATE INDEX IF NOT EXISTS nif_open_idx ON node_in_flight (tenant_id, run_id)
  WHERE settled_at IS NULL;
CREATE INDEX IF NOT EXISTS nif_by_epoch_idx
  ON node_in_flight (tenant_id, run_id, node_id, node_epoch)
  WHERE settled_at IS NULL;
CREATE INDEX IF NOT EXISTS nif_by_job_idx ON node_in_flight (provider_job_id)
  WHERE provider_job_id IS NOT NULL AND settled_at IS NULL;

-- §5.4 `/blockers` 与 E06 失败报告的事实表（C21-15）。
CREATE TABLE IF NOT EXISTS node_blocker (
  tenant_id     TEXT NOT NULL,
  run_id        TEXT NOT NULL,
  node_id       TEXT NOT NULL,
  node_epoch    INTEGER NOT NULL,
  stage         TEXT,
  blocked_reason TEXT NOT NULL,
  attempt_id    TEXT,
  suggestion    TEXT,
  detail        TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  resolved_at   TEXT,

  PRIMARY KEY (tenant_id, run_id, node_id, node_epoch, blocked_reason)
);
CREATE INDEX IF NOT EXISTS nb_open_idx ON node_blocker (tenant_id, run_id)
  WHERE resolved_at IS NULL;

-- §1.6 暂停/恢复/取消的命令流水。幂等由平台 SDK 的 (tenant, command_id) 表承担；
-- 这张表记的是**命令语义**（是否已被后来的命令覆盖、何时生效）——
-- 两者同一个事务写入，见 service.run_command 与 C21-16。
CREATE TABLE IF NOT EXISTS run_command (
  tenant_id    TEXT NOT NULL,
  command_id   TEXT NOT NULL,
  run_id       TEXT NOT NULL,
  action       TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  state        TEXT NOT NULL,
  result       TEXT,
  created_at   TEXT NOT NULL,
  applied_at   TEXT,

  PRIMARY KEY (tenant_id, command_id),
  CONSTRAINT rcmd_action_closed CHECK (action IN ('pause','resume','cancel')),
  CONSTRAINT rcmd_state_closed CHECK (state IN
    ('ACCEPTED','APPLIED','REJECTED','SUPERSEDED'))
);
CREATE INDEX IF NOT EXISTS rcmd_by_run_idx
  ON run_command (tenant_id, run_id, created_at DESC);
-- 同一 Run 同时只能有一条待生效的 pause。
CREATE UNIQUE INDEX IF NOT EXISTS rcmd_pending_pause_uniq
  ON run_command (tenant_id, run_id, action) WHERE state = 'ACCEPTED';

-- §1.7 Run 级修复额度。`ceiling_micro` 是 S23 的只读复制，不是权威。
CREATE TABLE IF NOT EXISTS repair_budget (
  tenant_id              TEXT NOT NULL,
  run_id                 TEXT NOT NULL,
  max_business_attempts  INTEGER NOT NULL,
  used_business_attempts INTEGER NOT NULL DEFAULT 0,
  max_recheck_rounds     INTEGER NOT NULL,
  used_recheck_rounds    INTEGER NOT NULL DEFAULT 0,
  ceiling_micro          INTEGER,
  updated_at             TEXT NOT NULL,

  PRIMARY KEY (tenant_id, run_id),
  CONSTRAINT rb_used_le_max CHECK (
    used_business_attempts <= max_business_attempts
    AND used_recheck_rounds <= max_recheck_rounds)
);
"""


def schema_sql() -> str:
    return SCHEMA
