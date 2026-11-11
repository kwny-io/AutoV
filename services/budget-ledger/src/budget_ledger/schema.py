"""S23 自己的 schema。别的服务不 import 这里，只透过 HTTP 契约交互。

与 plans/S23-budget-ledger.md §1 的 DDL 一一对应，只把 Postgres 特有写法换成
SQLite：

- `timestamptz` → `TEXT`（固定宽度 ISO-8601 UTC，见 service 里的 `_ts`）
- `jsonb` → `TEXT`（JCS 文本）
- `text[]` → `TEXT`（JSON 数组文本）
- `char(3)` → `TEXT` 加 CHECK 长度
- `bigint` → `INTEGER`（SQLite 的 INTEGER 就是 64 位）
- `date` → `TEXT`
- `boolean` → `INTEGER` 加 CHECK
- `(x <> 0)::int` → `CASE WHEN x <> 0 THEN 1 ELSE 0 END`
- `GENERATED ALWAYS AS (...) STORED` 保持原样，SQLite 3.31+ 支持

`ledger_entry` 的不可变用触发器兜底（§1.7 要求"没有 UPDATE，没有 DELETE"）。
Postgres 上是 REVOKE + 触发器；SQLite 没有角色权限，所以只有触发器这一层，
但这已经足够让"篡改历史"在测试里直接失败（T23-35）。

`outbox` 不在这里声明：由平台事件 SDK 建表并写，字段形状以 SDK 为准（C23-15）。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §1.2 多级预算行。深度 0..6，锁序键是 (depth ASC, budget_id ASC)。
--
-- 三个计数都 >= 0，但 consumed+outstanding+escrowed 允许超过 limit：
-- 结算超支时 available 变负，这是模型的一部分，所以**不给 available 设 CHECK**
-- （§1.3）。真正的闸门是 reserve / grant_escrow 事务内的预检。
CREATE TABLE IF NOT EXISTS budget (
  budget_id        TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  scope_type       TEXT NOT NULL
    CHECK (scope_type IN ('tenant','project','series','episode','run','shot','node')),
  scope_ref        TEXT NOT NULL,
  parent_budget_id TEXT REFERENCES budget(budget_id),
  depth            INTEGER NOT NULL CHECK (depth BETWEEN 0 AND 6),

  currency         TEXT NOT NULL CHECK (length(currency) = 3),
  limit_micro      INTEGER NOT NULL CHECK (limit_micro >= 0),

  consumed_micro             INTEGER NOT NULL DEFAULT 0 CHECK (consumed_micro >= 0),
  outstanding_reserved_micro INTEGER NOT NULL DEFAULT 0 CHECK (outstanding_reserved_micro >= 0),
  escrowed_out_micro         INTEGER NOT NULL DEFAULT 0 CHECK (escrowed_out_micro >= 0),

  period_start     TEXT,
  period_end       TEXT,
  state            TEXT NOT NULL DEFAULT 'ACTIVE'
    CHECK (state IN ('ACTIVE','FROZEN','EXHAUSTED','CLOSED')),
  revision         INTEGER NOT NULL DEFAULT 1,
  price_snapshot_policy TEXT NOT NULL DEFAULT 'STRICT',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,

  CONSTRAINT budget_scope_uniq UNIQUE (tenant_id, scope_type, scope_ref, period_start),
  CONSTRAINT budget_self_parent CHECK (budget_id <> parent_budget_id),
  CONSTRAINT budget_depth_parent CHECK ((parent_budget_id IS NULL) = (depth = 0)),
  CONSTRAINT budget_period_order CHECK (
    period_end IS NULL OR period_start IS NULL OR period_end > period_start)
);
CREATE INDEX IF NOT EXISTS budget_tenant_idx ON budget (tenant_id, state);
CREATE INDEX IF NOT EXISTS budget_parent_idx ON budget (parent_budget_id);
CREATE INDEX IF NOT EXISTS budget_scope_idx ON budget (tenant_id, scope_type, scope_ref);
CREATE INDEX IF NOT EXISTS budget_depth_idx ON budget (tenant_id, depth, budget_id);

-- §1.4 父子委派额度。额度是一棵纯分配树：非根 limit 完全由父级 grant 决定，
-- 恒等式 子.limit_micro == escrow_balance_micro 永远成立。
CREATE TABLE IF NOT EXISTS budget_escrow (
  escrow_id        TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  parent_budget_id TEXT NOT NULL REFERENCES budget(budget_id),
  child_budget_id  TEXT NOT NULL REFERENCES budget(budget_id),
  currency         TEXT NOT NULL CHECK (length(currency) = 3),
  granted_micro    INTEGER NOT NULL CHECK (granted_micro >= 0),
  returned_micro   INTEGER NOT NULL DEFAULT 0 CHECK (returned_micro >= 0),
  escrow_balance_micro INTEGER GENERATED ALWAYS AS
    (granted_micro - returned_micro) STORED,
  child_consumed_micro INTEGER NOT NULL DEFAULT 0 CHECK (child_consumed_micro >= 0),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  CONSTRAINT escrow_parent_child CHECK (parent_budget_id <> child_budget_id),
  -- §1.4 原式是 `child_consumed_micro <= escrow_balance_micro`。这里展开成
  -- `child_consumed + returned <= granted`：等价，且不引用生成列（SQLite 的
  -- CHECK 不能读本行的 GENERATED 列）。
  CONSTRAINT escrow_child_consumed_le_balance
    CHECK (child_consumed_micro + returned_micro <= granted_micro)
);
CREATE UNIQUE INDEX IF NOT EXISTS escrow_pair_uniq
  ON budget_escrow (parent_budget_id, child_budget_id, currency);
CREATE INDEX IF NOT EXISTS escrow_child_idx ON budget_escrow (child_budget_id);

-- §1.5 预留。R = reserved_budget_currency_micro（永远按报价上界算）。
-- budget_chain 是预留时的祖先链快照：结算向上结转不必再遍历树，
-- 同时它是"跨 run 返工不能绕过集预算"的证据（集级预算总在链里）。
CREATE TABLE IF NOT EXISTS reservation (
  reservation_id   TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  budget_id        TEXT NOT NULL REFERENCES budget(budget_id),
  budget_chain     TEXT NOT NULL,
  currency         TEXT NOT NULL CHECK (length(currency) = 3),

  amount_micro                INTEGER NOT NULL CHECK (amount_micro > 0),
  amount_conservative_micro   INTEGER NOT NULL CHECK (amount_conservative_micro >= amount_micro),

  budget_currency TEXT NOT NULL CHECK (length(budget_currency) = 3),
  reserved_budget_currency_micro INTEGER NOT NULL CHECK (reserved_budget_currency_micro > 0),

  price_snapshot_ref TEXT NOT NULL,
  fx_snapshot_ref    TEXT,
  quote_expires_at   TEXT NOT NULL,

  attempt_id        TEXT NOT NULL,
  run_id            TEXT,
  node_id           TEXT,
  node_epoch        INTEGER,
  execution_fence   INTEGER NOT NULL,
  manifest_ref      TEXT NOT NULL,
  route_decision_ref TEXT NOT NULL,

  state            TEXT NOT NULL
    CHECK (state IN ('RESERVED','AUTHORIZED','ACCRUED','SETTLED','RELEASED','EXPIRED')),
  revision         INTEGER NOT NULL DEFAULT 1,
  authorized_at    TEXT,
  accrued_at       TEXT,
  settled_at       TEXT,
  released_at      TEXT,
  release_reason   TEXT
    CHECK (release_reason IS NULL OR release_reason IN
      ('SETTLED','SAGA_ABORTED','QUOTA_FAILED','FENCE_SUPERSEDED',
       'QUOTE_EXPIRED','NEVER_DISPATCHED','RECONCILED_ABSENT','OPERATOR')),

  idempotency_scope TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,

  CONSTRAINT res_conservative_ge CHECK (amount_conservative_micro >= amount_micro),
  CONSTRAINT res_authorized_ts CHECK (
    state NOT IN ('AUTHORIZED','ACCRUED','SETTLED') OR authorized_at IS NOT NULL),
  CONSTRAINT res_released_reason CHECK (state <> 'RELEASED' OR release_reason IS NOT NULL)
);
-- 同一个 (tenant, scope) 下只允许一条"还活着"的预留。这正是 T23-03：
-- 不同 command、相同 scope 返回同一 reservation。
CREATE UNIQUE INDEX IF NOT EXISTS res_scope_uniq
  ON reservation (tenant_id, idempotency_scope)
  WHERE state NOT IN ('RELEASED','EXPIRED');
CREATE INDEX IF NOT EXISTS res_budget_state_idx ON reservation (budget_id, state);
CREATE INDEX IF NOT EXISTS res_attempt_idx ON reservation (tenant_id, attempt_id);
CREATE INDEX IF NOT EXISTS res_run_idx ON reservation (tenant_id, run_id);
CREATE INDEX IF NOT EXISTS res_quote_exp_idx
  ON reservation (state, quote_expires_at) WHERE state IN ('RESERVED','AUTHORIZED');
CREATE INDEX IF NOT EXISTS res_reconcile_idx
  ON reservation (tenant_id, state, updated_at) WHERE state = 'ACCRUED';

-- 每 tenant 单调的分录序号。允许空洞（回滚会消耗序号），只要单调且唯一。
CREATE TABLE IF NOT EXISTS ledger_counter (
  tenant_id TEXT PRIMARY KEY,
  next_seq  INTEGER NOT NULL DEFAULT 1
);

-- §1.7 只追加的分录。le_single_axis 强制一条分录只动一个维度 ——
-- 多维度混合分录是账本事故的常见来源，这里直接从 schema 禁止。
CREATE TABLE IF NOT EXISTS ledger_entry (
  entry_id         TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  entry_seq        INTEGER NOT NULL,
  budget_id        TEXT NOT NULL REFERENCES budget(budget_id),
  currency         TEXT NOT NULL CHECK (length(currency) = 3),

  entry_type       TEXT NOT NULL
    CHECK (entry_type IN ('LIMIT_SET','LIMIT_ADJUST','RESERVE','RELEASE','SETTLE',
                          'ADJUST','REFUND','ESCROW_GRANT','ESCROW_RETURN')),

  delta_limit_micro     INTEGER NOT NULL DEFAULT 0,
  delta_reserved_micro  INTEGER NOT NULL DEFAULT 0,
  delta_consumed_micro  INTEGER NOT NULL DEFAULT 0,
  delta_escrowed_micro  INTEGER NOT NULL DEFAULT 0,

  reservation_id   TEXT REFERENCES reservation(reservation_id),
  attempt_id       TEXT,
  provider_job_ref TEXT,
  price_snapshot_ref TEXT,
  fx_snapshot_ref  TEXT,

  business_key     TEXT NOT NULL,
  attribution      TEXT NOT NULL,
  reason           TEXT,
  actor            TEXT NOT NULL,
  correlation_id   TEXT NOT NULL,
  causation_id     TEXT,
  posted_at        TEXT NOT NULL,

  CONSTRAINT le_seq_uniq UNIQUE (tenant_id, entry_seq),
  CONSTRAINT le_business_key_uniq UNIQUE (tenant_id, business_key),
  CONSTRAINT le_single_axis CHECK (
    (CASE WHEN delta_limit_micro     <> 0 THEN 1 ELSE 0 END)
  + (CASE WHEN delta_reserved_micro  <> 0 THEN 1 ELSE 0 END)
  + (CASE WHEN delta_consumed_micro  <> 0 THEN 1 ELSE 0 END)
  + (CASE WHEN delta_escrowed_micro  <> 0 THEN 1 ELSE 0 END) = 1
  )
);
CREATE INDEX IF NOT EXISTS le_budget_idx ON ledger_entry (budget_id, entry_seq);
CREATE INDEX IF NOT EXISTS le_res_idx ON ledger_entry (reservation_id)
  WHERE reservation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS le_attempt_idx ON ledger_entry (tenant_id, attempt_id)
  WHERE attempt_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS le_time_idx ON ledger_entry (tenant_id, posted_at);

-- §1.7「分录是不可变的：没有 UPDATE，没有 DELETE」。
CREATE TRIGGER IF NOT EXISTS ledger_entry_no_update
BEFORE UPDATE ON ledger_entry
BEGIN
  SELECT RAISE(ABORT, 'ledger_entry is append-only');
END;
CREATE TRIGGER IF NOT EXISTS ledger_entry_no_delete
BEFORE DELETE ON ledger_entry
BEGIN
  SELECT RAISE(ABORT, 'ledger_entry is append-only');
END;

-- §1.8 用量暂记。PROVISIONAL 只把 pending_reconciliation 置真，
-- 不改任何 budget 计数；只有 settle 才动 consumed。
CREATE TABLE IF NOT EXISTS usage_accrual (
  accrual_id       TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  reservation_id   TEXT NOT NULL REFERENCES reservation(reservation_id),
  attempt_id       TEXT NOT NULL,

  provider_job_ref TEXT,
  billing_unit     TEXT NOT NULL
    CHECK (billing_unit IN ('request','generation_second','character','token',
                            'pixel','gpu_second','cpu_second','megabyte')),
  billed_quantity  INTEGER NOT NULL CHECK (billed_quantity >= 0),
  quantity_scale   INTEGER NOT NULL DEFAULT 1 CHECK (quantity_scale > 0),

  currency         TEXT NOT NULL CHECK (length(currency) = 3),
  billed_micro     INTEGER NOT NULL CHECK (billed_micro >= 0),
  budget_currency  TEXT NOT NULL CHECK (length(budget_currency) = 3),
  billed_budget_currency_micro INTEGER NOT NULL CHECK (billed_budget_currency_micro >= 0),

  stage            TEXT NOT NULL CHECK (stage IN ('PROVISIONAL','FINAL')),
  basis            TEXT NOT NULL
    CHECK (basis IN ('PROVIDER_INVOICE','PROVIDER_QUOTE_CAP',
                     'ESTIMATE_CONSERVATIVE','RECONCILED')),
  evidence_ref     TEXT,
  invoice_ref      TEXT,

  created_at       TEXT NOT NULL,
  CONSTRAINT ua_provisional_no_invoice CHECK ((stage = 'FINAL') OR invoice_ref IS NULL),
  CONSTRAINT ua_final_needs_basis CHECK (stage <> 'FINAL' OR basis IN
                                          ('PROVIDER_INVOICE','RECONCILED'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ua_business_uniq
  ON usage_accrual (tenant_id, reservation_id, stage, coalesce(invoice_ref, ''));
CREATE INDEX IF NOT EXISTS ua_res_idx ON usage_accrual (reservation_id);

-- §1.8 结算。一条 reservation 只有一条 settlement。
CREATE TABLE IF NOT EXISTS settlement (
  settlement_id    TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  reservation_id   TEXT NOT NULL REFERENCES reservation(reservation_id),
  accrual_ids      TEXT NOT NULL,
  currency         TEXT NOT NULL CHECK (length(currency) = 3),
  settled_micro    INTEGER NOT NULL,
  budget_currency  TEXT NOT NULL CHECK (length(budget_currency) = 3),
  settled_budget_currency_micro INTEGER NOT NULL,
  overage_micro    INTEGER NOT NULL DEFAULT 0 CHECK (overage_micro >= 0),
  unused_released_micro INTEGER NOT NULL DEFAULT 0 CHECK (unused_released_micro >= 0),
  created_at       TEXT NOT NULL,
  CONSTRAINT st_one_per_reservation UNIQUE (reservation_id)
);

-- §1.11 调整。**不是金额流水**：金额已由 SETTLE 全额记进 consumed，
-- 再记 ADJUST 分录就是重复计数。这张表只解释"为什么差"。
CREATE TABLE IF NOT EXISTS adjustment (
  adjustment_id   TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  budget_id       TEXT NOT NULL REFERENCES budget(budget_id),
  currency        TEXT NOT NULL CHECK (length(currency) = 3),
  amount_micro    INTEGER NOT NULL,
  reason_code     TEXT NOT NULL
    CHECK (reason_code IN ('BILL_DRIFT','FX_DIFF','QUOTE_UNDERESTIMATE',
                           'CREDIT','MANUAL','REFUND')),
  detail          TEXT NOT NULL,
  source_ref      TEXT,
  approval_ref    TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

-- §1.11 退款。必须来自外部确认 —— 不能是我方"认为应该退"。
CREATE TABLE IF NOT EXISTS refund (
  refund_id       TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  budget_id       TEXT NOT NULL REFERENCES budget(budget_id),
  reservation_id  TEXT REFERENCES reservation(reservation_id),
  currency        TEXT NOT NULL CHECK (length(currency) = 3),
  amount_micro    INTEGER NOT NULL CHECK (amount_micro > 0),
  reason_code     TEXT NOT NULL
    CHECK (reason_code IN ('PROVIDER_CREDIT','FRAUD','SERVICE_CREDIT','CONTRACT')),
  external_credit_ref TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_freeze (
  freeze_id       TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  budget_id       TEXT NOT NULL REFERENCES budget(budget_id),
  frozen_at       TEXT NOT NULL,
  reason          TEXT NOT NULL,
  unfrozen_at     TEXT
);

-- §1.6 汇率快照。rate_micro 是 ×1e6 的整数：1 base = rate_micro/1e6 quote。
-- 换算一律单向保守（让 available 变小的方向向上取整），不经过浮点。
CREATE TABLE IF NOT EXISTS fx_snapshot (
  fx_snapshot_id   TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  base_currency    TEXT NOT NULL CHECK (length(base_currency) = 3),
  quote_currency   TEXT NOT NULL CHECK (length(quote_currency) = 3),
  rate_micro       INTEGER NOT NULL CHECK (rate_micro > 0),
  source           TEXT NOT NULL,
  observed_at      TEXT NOT NULL,
  valid_until      TEXT NOT NULL,
  CONSTRAINT fx_pair_diff CHECK (base_currency <> quote_currency)
);
CREATE INDEX IF NOT EXISTS fx_lookup_idx
  ON fx_snapshot (tenant_id, base_currency, quote_currency, observed_at DESC);
"""


def schema_sql() -> str:
    return SCHEMA
