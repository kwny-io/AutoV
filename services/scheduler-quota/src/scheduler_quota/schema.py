"""S22 自己的 schema。别的服务不 import 这里，只透过 HTTP 契约交互。

与 plans/S22-scheduler-quota.md §1 的 DDL 一一对应，Postgres 写法换 SQLite：

- `timestamptz` → `TEXT`（固定宽度 ISO-8601 UTC，见 service 里的 `_ts`）
- `jsonb` → `TEXT`（JCS 文本）
- `smallint` / `bigint` / `integer` → `INTEGER`（SQLite 的 INTEGER 是 64 位）
- `numeric(20,3)` → `INTEGER`，单位是**毫资源秒**（1 秒 = 1000）。资源秒参与
  配额算术，绝不能用浮点；§1.1 的 `numeric` 只是 PG 的定点写法。
- `bigint GENERATED ALWAYS AS IDENTITY` → `INTEGER PRIMARY KEY AUTOINCREMENT`
- 部分唯一索引与部分索引 SQLite 都支持，原样保留

三处**故意与 §1 的 DDL 不同**，都有验收用例顶着：

1. `qp_inflight_bounds` 只保留下界。原文是
   `CHECK (inflight_used BETWEEN 0 AND inflight_limit)`，但 T22-14 要求
   「limit 从 40 下调到 20，当前 used=35，已有 35 个继续」—— 若上限也写进
   CHECK，那条下调语句本身就写不进去。上限是**发放入口的事务内预检**
   （service 的 `_check_capacity`），不是列约束。文档冲突见 C22-7。
2. `account_used_inflight` 同理，去掉 `BETWEEN` 的上界。
3. `modality` 的 CHECK 收 `understanding` 与 `training` —— S24 的池定义里这两种
   存在，但 S22 的 permit 不发放给它们（见 `POOL_MODALITY_MISMATCH`）。允许建池、
   拒绝发放，比建不了池更接近真实运维。

`outbox` / `inbox` / `idempotency_record` 不在这里声明：平台 SDK 建表并写。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §1.1 池。三个独立维度（rpm / tpm / 在途）绝不合并成一个"余量"。
--
-- `resource_seconds_used` 只有在 resource_class 是 gpu/cpu 时有意义；API 池恒 0。
CREATE TABLE IF NOT EXISTS quota_pool (
  pool_id              TEXT PRIMARY KEY,
  tenant_scope         TEXT NOT NULL,
  provider_id          TEXT NOT NULL,
  channel_id           TEXT NOT NULL,
  account_ref          TEXT NOT NULL,
  region               TEXT NOT NULL,
  modality             TEXT NOT NULL
    CHECK (modality IN ('video','image','speech','text','understanding',
                        'lipsync','sound_effect','music','training')),
  resource_class       TEXT NOT NULL
    CHECK (resource_class IN ('api_request','api_token','gpu_second','cpu_media_second')),

  -- 由 (provider_id, account_ref, region) 拼出，见 service.shard_key_for。
  -- 存成列而不是每次现拼：low-level 的 Σ pool.limit ≤ cap 校验要按它 GROUP BY。
  shard_key            TEXT NOT NULL REFERENCES pool_shard(shard_key),

  rpm_limit            INTEGER,
  rpm_remaining_window INTEGER NOT NULL DEFAULT 0 CHECK (rpm_remaining_window >= 0),
  window_reset_at      TEXT,
  tpm_limit            INTEGER,
  tpm_remaining_window INTEGER NOT NULL DEFAULT 0 CHECK (tpm_remaining_window >= 0),
  inflight_limit       INTEGER NOT NULL CHECK (inflight_limit >= 1),
  inflight_used        INTEGER NOT NULL DEFAULT 0 CHECK (inflight_used >= 0),

  resource_seconds_limit INTEGER,
  resource_seconds_used_milli INTEGER NOT NULL DEFAULT 0
    CHECK (resource_seconds_used_milli >= 0),

  state                TEXT NOT NULL DEFAULT 'ACTIVE'
    CHECK (state IN ('ACTIVE','DRAINING','SUSPENDED','DISABLED')),
  suspend_reason       TEXT,
  revision             INTEGER NOT NULL DEFAULT 1,
  source_revision      INTEGER NOT NULL DEFAULT 0,
  updated_at           TEXT NOT NULL,

  CONSTRAINT qp_resource_nonneg CHECK (resource_seconds_used_milli >= 0),
  CONSTRAINT qp_shared_needs_account
    CHECK (tenant_scope = 'shared' OR account_ref <> '')
);
CREATE INDEX IF NOT EXISTS qp_route_idx
  ON quota_pool (provider_id, channel_id, region, modality, state);
CREATE INDEX IF NOT EXISTS qp_account_idx ON quota_pool (account_ref, region);

-- §1.1 账号级二级计数。同一 (provider, account, region) 的全部池落在同一分片，
-- 因为供应商账号的并发是**跨 modality 共享**的硬上限。
CREATE TABLE IF NOT EXISTS pool_shard (
  shard_key             TEXT PRIMARY KEY,
  shard_id              INTEGER NOT NULL,
  account_cap_inflight  INTEGER NOT NULL CHECK (account_cap_inflight >= 1),
  account_used_inflight INTEGER NOT NULL DEFAULT 0
    CHECK (account_used_inflight >= 0)
);

-- §1.2 permit。这是本服务的唯一权威。
--
-- `reservation_ref` / `reservation_amount_micro` 是**不透明引用**：S22 从不
-- 拿它们去校验余额，也不读 S23。存下来只为对账时能把两条记录对上。
CREATE TABLE IF NOT EXISTS permit (
  permit_id          TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  pool_id            TEXT NOT NULL REFERENCES quota_pool(pool_id),
  shard_key          TEXT NOT NULL REFERENCES pool_shard(shard_key),

  reservation_ref    TEXT NOT NULL,
  reservation_amount_micro INTEGER NOT NULL CHECK (reservation_amount_micro >= 0),
  budget_scope_id    TEXT NOT NULL,

  attempt_id         TEXT NOT NULL,
  run_id             TEXT,
  execution_fence    INTEGER NOT NULL CHECK (execution_fence >= 1),
  node_id            TEXT,
  node_epoch         INTEGER,
  route_decision_ref TEXT NOT NULL,

  state              TEXT NOT NULL
    CHECK (state IN ('RESERVED','DISPATCH_AUTHORIZED','INFLIGHT','RELEASED','EXPIRED')),
  release_reason     TEXT
    CHECK (release_reason IS NULL OR release_reason IN
      ('SETTLED','SUPERSEDED_BY_BUDGET','SUPERSEDED_BY_FENCE',
       'NEVER_DISPATCHED','RECONCILED_ABSENT','OPERATOR')),

  grant_epoch        INTEGER NOT NULL DEFAULT 0,
  lease_expires_at   TEXT,
  dispatched_at      TEXT,
  released_at        TEXT,

  dispatch_id        TEXT,
  provider_job_ref   TEXT,
  reconcile_required_at TEXT,

  -- gpu_second / cpu_media_second 池领取时预扣的资源毫秒数。
  -- release/expire 要按**领取时记下的量**退还，而不是信任调用方再报一次。
  resource_milli_reserved INTEGER NOT NULL DEFAULT 0
    CHECK (resource_milli_reserved >= 0),

  deductible_key     TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,

  CONSTRAINT permit_live_needs_lease
    CHECK (state NOT IN ('RESERVED','DISPATCH_AUTHORIZED') OR lease_expires_at IS NOT NULL),
  CONSTRAINT permit_released_needs_reason
    CHECK (state <> 'RELEASED' OR release_reason IS NOT NULL),
  CONSTRAINT permit_terminal_ts
    CHECK (state <> 'RELEASED' OR released_at IS NOT NULL)
);
-- 「同一次领取重复调用返回同一 permit」靠这条约束，不靠应用内存 map。
-- EXPIRED 不挡：过期的 permit 从未真正提交过，重建是安全的（§5.4）。
CREATE UNIQUE INDEX IF NOT EXISTS permit_deduct_uniq
  ON permit (tenant_id, deductible_key) WHERE state <> 'EXPIRED';
CREATE INDEX IF NOT EXISTS permit_pool_state_idx ON permit (pool_id, state);
CREATE INDEX IF NOT EXISTS permit_inflight_idx
  ON permit (shard_key, state) WHERE state = 'INFLIGHT';
CREATE INDEX IF NOT EXISTS permit_lease_idx
  ON permit (state, lease_expires_at) WHERE state IN ('RESERVED','DISPATCH_AUTHORIZED');
CREATE INDEX IF NOT EXISTS permit_attempt_idx ON permit (tenant_id, attempt_id);
CREATE INDEX IF NOT EXISTS permit_fence_idx ON permit (tenant_id, run_id, execution_fence);

-- §1.3 公平队列。WAITING 不占任何计数 —— 这是「排队不等于预留」的落地。
CREATE TABLE IF NOT EXISTS queue_entry (
  queue_entry_id     TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  pool_id            TEXT NOT NULL REFERENCES quota_pool(pool_id),
  request_ref        TEXT NOT NULL,
  priority           INTEGER NOT NULL DEFAULT 10 CHECK (priority BETWEEN 0 AND 100),
  enqueued_at        TEXT NOT NULL,
  effective_at       TEXT NOT NULL,
  expected_run_seconds INTEGER NOT NULL DEFAULT 0,
  state              TEXT NOT NULL DEFAULT 'WAITING'
    CHECK (state IN ('WAITING','GRANTED','CANCELLED','ABANDONED')),
  granted_permit_id  TEXT,
  deadline           TEXT,
  CONSTRAINT qe_granted_has_permit
    CHECK ((state = 'GRANTED') = (granted_permit_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS qe_pick_idx
  ON queue_entry (pool_id, state, effective_at, enqueued_at) WHERE state = 'WAITING';
CREATE INDEX IF NOT EXISTS qe_tenant_idx ON queue_entry (tenant_id, state);

-- §1.3 租户份额（WFQ 虚拟时间）。virtual_time 用微秒整数，不用 numeric。
CREATE TABLE IF NOT EXISTS tenant_share (
  tenant_id        TEXT NOT NULL,
  pool_id          TEXT NOT NULL,
  weight           INTEGER NOT NULL DEFAULT 1 CHECK (weight > 0),
  virtual_time_micro INTEGER NOT NULL DEFAULT 0,
  inflight_granted INTEGER NOT NULL DEFAULT 0,
  guaranteed_share INTEGER NOT NULL DEFAULT 0,
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, pool_id)
);

-- §1.4 计数流水。**不是第二权威** —— 权威是 permit 状态的聚合。
-- 用途只有两个：Redis 全丢时核对重算结果，以及每日一致性作业。
CREATE TABLE IF NOT EXISTS pool_usage_ledger (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  pool_id            TEXT NOT NULL,
  shard_key          TEXT NOT NULL,
  tenant_id          TEXT NOT NULL,
  delta_inflight     INTEGER NOT NULL DEFAULT 0,
  delta_account      INTEGER NOT NULL DEFAULT 0,
  delta_rpm          INTEGER NOT NULL DEFAULT 0,
  delta_resource_milli INTEGER NOT NULL DEFAULT 0,
  reason             TEXT NOT NULL
    CHECK (reason IN ('ACQUIRE','DISPATCH','RELEASE','EXPIRE','RECONCILE')),
  permit_id          TEXT,
  at                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pul_pool_idx ON pool_usage_ledger (pool_id, at);
CREATE INDEX IF NOT EXISTS pul_permit_idx
  ON pool_usage_ledger (permit_id) WHERE permit_id IS NOT NULL;

-- 计数流水只追加。篡改它等于篡改对账依据。
CREATE TRIGGER IF NOT EXISTS pool_usage_ledger_no_update
  BEFORE UPDATE ON pool_usage_ledger
BEGIN
  SELECT RAISE(ABORT, 'pool_usage_ledger is append-only');
END;
CREATE TRIGGER IF NOT EXISTS pool_usage_ledger_no_delete
  BEFORE DELETE ON pool_usage_ledger
BEGIN
  SELECT RAISE(ABORT, 'pool_usage_ledger is append-only');
END;
"""
