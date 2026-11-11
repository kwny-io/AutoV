# S23 · 预算与成本账本 —— 实现方案

状态：实现方案（W1）。`maturity: design_only` 不变。

上游依据：[docs/04 §3](../docs/04-data-consistency.md)、[docs/09 §4](../docs/09-model-execution.md)、[docs/12 §1/§3/§4](../docs/12-budget-scheduling.md)、[S23 规格](../docs/design-package/services/S23-budget-ledger.md)。

配套：[S22 调度与供应商配额](S22-scheduler-quota.md)、[S08 版本快照与接受状态](S08-revision-snapshot.md)。

---

## 0. 边界与一句话职责

S23 是**金额**的唯一写者：预算额度、预留、授权、实际费用暂记、结算、调整、退款、汇率快照。

S23 **不**拥有：

| 事实 | 归属 |
|---|---|
| 配额许可、在途槽 | S22 |
| 任务状态、`SUBMISSION_UNKNOWN` | S26 |
| 单价快照、能力、供应商生命周期 | S24 |
| 路由决策与预期成本估算 | S25 |
| 经营分析投影 | S44 |
| 法定总账、开票、充值 | 独立商业计费系统（docs/12 §3 明确） |

**S23 不是财务总账**。它是生产成本预算账本，用于准入控制与成本归因。docs/12 §3 原文："不能把生产预算ledger冒充法定总账。" 这句话必须写进 API 文档和运维手册。

判据：任何"这个预算还剩多少钱"的疑问，答案只能来自 S23。

---

## 1. 数据模型

数据库 `budget_ledger`，Postgres。**所有金额是整数最小货币单位（micro-units），无 float，无 numeric 参与计算。**

### 1.1 金额表示（先定死，后面全部引用）

```
1 单位货币 = 1_000_000 micro
字段名一律以 _micro 结尾，类型 bigint
币种字段一律 currency char(3)，ISO 4217
跨币种永不相加 —— 见 §1.6
```

`contracts/openapi.v2.yaml` 的 `limit_micro` / `consumed_micro` / `outstanding_reserved_micro` 都是这个单位。`admission-record.v2.schema.json` 的 `reservation.amount_micro` 同此。

**负数**：`adjustment` 与 `refund` 用正负号表达方向，但 `limit_micro`、`consumed_micro`、`outstanding_reserved_micro` 永远 ≥ 0（数据库 CHECK）。

### 1.2 `budget` —— 多级预算行，锁序的基础

```sql
CREATE TABLE budget (
  budget_id        text PRIMARY KEY,
  tenant_id        text NOT NULL,
  scope_type       text NOT NULL
    CHECK (scope_type IN ('tenant','project','series','episode','run','shot','node')),
  scope_ref        text NOT NULL,          -- project_id / episode_id / run_id / node_id
  parent_budget_id text REFERENCES budget(budget_id),
  depth            smallint NOT NULL CHECK (depth BETWEEN 0 AND 6),

  currency         char(3) NOT NULL,
  limit_micro      bigint NOT NULL CHECK (limit_micro >= 0),

  -- 三个派生计数。三者都 >= 0；
  -- 但 consumed+outstanding+escrowed 允许短暂超过 limit（结算超支），
  -- 此时 available 为负 —— 不设 CHECK，见 §1.4
  consumed_micro            bigint NOT NULL DEFAULT 0 CHECK (consumed_micro >= 0),
  outstanding_reserved_micro bigint NOT NULL DEFAULT 0 CHECK (outstanding_reserved_micro >= 0),
  escrowed_out_micro        bigint NOT NULL DEFAULT 0 CHECK (escrowed_out_micro >= 0),

  period_start     date,
  period_end       date,                   -- NULL = 永久
  state            text NOT NULL DEFAULT 'ACTIVE'
    CHECK (state IN ('ACTIVE','FROZEN','EXHAUSTED','CLOSED')),
  revision         bigint NOT NULL DEFAULT 1,
  price_snapshot_policy text NOT NULL DEFAULT 'STRICT',
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT budget_scope_uniq UNIQUE (tenant_id, scope_type, scope_ref, period_start),
  CONSTRAINT budget_self_parent CHECK (budget_id <> parent_budget_id),
  CONSTRAINT budget_depth_parent CHECK ((parent_budget_id IS NULL) = (depth = 0)),
  CONSTRAINT budget_period_order CHECK (period_end IS NULL OR period_start IS NULL OR period_end > period_start)
);
CREATE INDEX budget_tenant_idx  ON budget (tenant_id, state);
CREATE INDEX budget_parent_idx  ON budget (parent_budget_id);
CREATE INDEX budget_scope_idx   ON budget (tenant_id, scope_type, scope_ref);

-- 遍历祖先链用递归 CTE；这条索引让祖先链查询走索引
CREATE INDEX budget_depth_idx   ON budget (tenant_id, depth, budget_id);
```

**三个计数的语义，必须背下来：**

| 字段 | 含义 |
|---|---|
| `limit_micro` | 该层被授予的总额度（人工/审批设定） |
| `consumed_micro` | **保守已消费**：只含已结算的实际费用（`F`）。暂记未结算的仍在 `outstanding_reserved_micro` 里 |
| `outstanding_reserved_micro` | **本层直接预留**：本层发起的、尚未结算的 reservation |
| `escrowed_out_micro` | **已委派给子级的 escrow 总额** |

### 1.3 `available` 的定义

docs/12 §1 原文：`available = limit - conservative_consumed - outstanding_reserved`

但多级预算还有 escrow，所以完整式子是：

```
available(b) = b.limit_micro
             - b.consumed_micro
             - b.outstanding_reserved_micro
             - b.escrowed_out_micro
```

**父级不重复计算子级的承诺**（docs/12 "父预算包含已委派escrow与全部子级承诺，不能重复计算可用额"）：

- 子级预留时**不向上传播**到祖先的 `outstanding_reserved_micro`。那样父子会双算。
- 子级花的是自己的 `limit`，而这份 `limit` 在 `grant_escrow` 时已经从父级的 available 里让出去了（父 `escrowed_out += x`）。**预留只落在目标行上。**

这条公式**不写成数据库 CHECK**——超支结算时它会中止事务，而我们的选择是"超支也要结算"（§2.5）。真正的闸门是 `reserve`/`grant_escrow` 事务内的预检，详见 §1.4。

`available` 因此可以为负，负值即"该层已透支"，全树恒等式仍配平（负值参与求和）。

### 1.4 `budget_escrow` —— 父子之间的委派额度

```sql
CREATE TABLE budget_escrow (
  escrow_id        text PRIMARY KEY,
  tenant_id        text NOT NULL,
  parent_budget_id text NOT NULL REFERENCES budget(budget_id),
  child_budget_id  text NOT NULL REFERENCES budget(budget_id),
  currency         char(3) NOT NULL,
  granted_micro    bigint NOT NULL CHECK (granted_micro >= 0),  -- 累计授予
  returned_micro   bigint NOT NULL DEFAULT 0 CHECK (returned_micro >= 0),  -- 累计退回
  escrow_balance_micro bigint GENERATED ALWAYS AS (granted_micro - returned_micro) STORED,
  -- 子级已经花掉但还没向上结算的部分
  child_consumed_micro bigint NOT NULL DEFAULT 0 CHECK (child_consumed_micro >= 0),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT escrow_parent_child CHECK (parent_budget_id <> child_budget_id),
  CONSTRAINT escrow_child_consumed_le_balance CHECK (child_consumed_micro <= escrow_balance_micro)
);
CREATE UNIQUE INDEX escrow_pair_uniq ON budget_escrow (parent_budget_id, child_budget_id, currency);
CREATE INDEX escrow_child_idx ON budget_escrow (child_budget_id);
```

**escrow 的作用**：让高频的 Run/Shot 级预留不每次都锁到 tenant 行。docs/03 §3："高频细粒度任务可预分配有限escrow到Run，父级outstanding总额含已委派额度，禁止父子重复消费。"

资金流：

```
父 budget
  ├─ consumed_micro             ← 最终实际花费（结算时向上结转）
  ├─ outstanding_reserved_micro ← 本层直接预留
  └─ escrowed_out_micro         ← 已委派给子级、尚未退回的净额
```

**额度是一棵纯分配树（allocation tree），没有第二条来源：**

- **根预算**的 `limit_micro` 由人工/审批设定（`set_limit`），这是唯一不从父级来的额度。
- **非根预算**的 `limit_micro` 完全由父级的 `grant_escrow` 决定。授予 `x` 时同一事务内：父 `escrowed_out += x`，子 `limit_micro += x`。**恒等式 `子.limit_micro == escrow_balance_micro` 永远成立。**
- 所以 `父.escrowed_out_micro = Σ_child 子.limit_micro` —— 父级让出的就是子级拥有的，一分不多一分不少。

这是关键简化：**没有"子级还有别的来源"这回事**。想把集预算调大，就是对集预算那一行做 `set_limit`，而 `set_limit` 本身会先检查它的父级是否有足够 available（必要时先 `grant_escrow`）。整条链上只有根是可以凭空变大的，且需要审批。

**每行独立的不变量**：

```
reserve / grant_escrow 时（准入前置检查，拒绝越界）：
    consumed + outstanding + escrowed + 新增额 <= limit

settle 之后（允许越界，越界即 EXHAUSTED）：
    available(b) = limit - consumed - outstanding - escrowed   可能为负
```

**不把上面那条写成数据库硬 CHECK**，因为它会在结算超支时中止事务。而我们明确选择"超支也要结算"（§2.5）。正确的做法是：

- `reserve` / `grant_escrow` 在事务内**预检**，不够就 409 —— 这是真正的准入闸门。
- `settle` 无条件推进计数；若结果使 `available < 0`，本行 `state = 'EXHAUSTED'` 并写 `adjustment`。

`available` 允许为负，是这套模型能自洽的关键：全树恒等式仍然成立（见下），且"负可用额"正好就是"这一层已经透支"的精确表达。

**父子不双花的证明**：子级能花的钱全部落在 `子.limit` 内；而 `子.limit == escrow_balance`，父级已在 `escrowed_out` 里等额让出，从父级 available 消失。于是全树恒等式：

```
Σ_{所有层} available(b) + Σ_{所有层} outstanding_reserved(b) + Σ_{所有层} consumed(b) = 根预算 limit
```

这是 §7 T23-08（E20）要断言的性质，也是 §9 每日一致性作业的第 1 项。**`escrowed_out` 不出现在等式中**——它只是父级把额度交给子级的记账动作，不是消耗；算进去就双算。

`escrow.child_consumed_micro` 记录"子级已消耗但尚未向父级结转的部分"，用于崩溃恢复时补齐。它不是第二权威——它只是"待向上结转"的标记，结算完成后清零。

### 1.5 `reservation` —— 预留

```sql
CREATE TABLE reservation (
  reservation_id   text PRIMARY KEY,
  tenant_id        text NOT NULL,
  budget_id        text NOT NULL REFERENCES budget(budget_id),   -- 直接扣减的那一层
  budget_chain     text[] NOT NULL,     -- [根, ..., budget_id]，按 depth 升序，快照
  currency         char(3) NOT NULL,

  -- 报价（原币）
  amount_micro     bigint NOT NULL CHECK (amount_micro > 0),              -- 预期费用，只用于展示与偏差分析
  amount_conservative_micro bigint NOT NULL CHECK (amount_conservative_micro >= amount_micro),  -- 报价上界

  -- 实际占用的额度（预算币种）。★ 预留永远按 amount_conservative 扣，不按 amount。
  budget_currency  char(3) NOT NULL,
  reserved_budget_currency_micro bigint NOT NULL CHECK (reserved_budget_currency_micro > 0),

  price_snapshot_ref jsonb NOT NULL,    -- {service_id:'S24', id, revision, content_hash}
  fx_snapshot_ref  jsonb,               -- 原币 ≠ 预算币种时必填
  quote_expires_at timestamptz NOT NULL,

  -- 归属（与 execution-context 同构）
  attempt_id       text NOT NULL,
  run_id           text,
  node_id          text,
  node_epoch       bigint,
  execution_fence  bigint NOT NULL,
  manifest_ref     jsonb NOT NULL,
  route_decision_ref jsonb NOT NULL,

  state            text NOT NULL
    CHECK (state IN ('RESERVED','AUTHORIZED','ACCRUED','SETTLED','RELEASED','EXPIRED')),
  authorized_at    timestamptz,
  accrued_at       timestamptz,
  settled_at       timestamptz,
  released_at      timestamptz,
  release_reason   text
    CHECK (release_reason IS NULL OR release_reason IN
      ('SETTLED','SAGA_ABORTED','QUOTA_FAILED','FENCE_SUPERSEDED',
       'QUOTE_EXPIRED','NEVER_DISPATCHED','RECONCILED_ABSENT','OPERATOR')),

  -- 幂等席位
  idempotency_scope text NOT NULL,      -- tenant_id||attempt_id（或外部业务键）
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT res_conservative_ge CHECK (amount_conservative_micro >= amount_micro),
  CONSTRAINT res_authorized_ts CHECK (state NOT IN ('AUTHORIZED','ACCRUED','SETTLED') OR authorized_at IS NOT NULL),
  CONSTRAINT res_released_reason CHECK (state <> 'RELEASED' OR release_reason IS NOT NULL)
);
CREATE UNIQUE INDEX res_scope_uniq
  ON reservation (tenant_id, idempotency_scope) WHERE state NOT IN ('RELEASED','EXPIRED');
CREATE INDEX res_budget_state_idx ON reservation (budget_id, state);
CREATE INDEX res_attempt_idx  ON reservation (tenant_id, attempt_id);
CREATE INDEX res_run_idx      ON reservation (tenant_id, run_id);
CREATE INDEX res_quote_exp_idx ON reservation (state, quote_expires_at)
  WHERE state IN ('RESERVED','AUTHORIZED');
CREATE INDEX res_reconcile_idx ON reservation (tenant_id, state, updated_at)
  WHERE state = 'ACCRUED';
```

`budget_chain` 是**预留时的祖先链快照**。它的用途是：结算时向上结转 `consumed`，不必重新遍历树（树可能在这期间被改过结构）。**同时它是"跨 run 返工不能绕过集预算"的证据**：set 级预算总在链里。

**预留金额的选择（必须写死，否则实现会各自发挥）：**

```
原币 == 预算币种：
    reserved_budget_currency_micro = amount_conservative_micro
原币 != 预算币种：
    reserved_budget_currency_micro = ceil( amount_conservative_micro * rate_micro / 1e6 )
                                     -- 整型运算：(x*rate + 999_999) / 1_000_000
```

即**永远按报价上界预留**，不按预期费用。理由：S23 §4.5 的超支路径存在但代价高；按上界预留把"超支"压缩成"结算时退回差额"，把风险留在 S23 内部而不是泄漏到预算外。`unused_released_micro` 就是这部分差额。

代价是某一瞬间 `available` 比"真实可能花费"更保守。这是有意为之——`available` 是准入闸门，宁可短暂偏保守也不能偏乐观。

### 1.6 费率与汇率快照

```sql
CREATE TABLE fx_snapshot (
  fx_snapshot_id   text PRIMARY KEY,
  tenant_id        text NOT NULL,
  base_currency    char(3) NOT NULL,
  quote_currency   char(3) NOT NULL,
  rate_micro       bigint NOT NULL CHECK (rate_micro > 0),  -- quote per base × 1e6
  source           text NOT NULL,        -- ECB / OANDA / CONTRACT
  observed_at      timestamptz NOT NULL,
  valid_until      timestamptz NOT NULL,
  CONSTRAINT fx_pair_diff CHECK (base_currency <> quote_currency)
);
CREATE INDEX fx_lookup_idx ON fx_snapshot (tenant_id, base_currency, quote_currency, observed_at DESC);
```

**跨币种永不相加。** 换算只在两处发生：

1. **预留**：原币 ≠ 预算币种 → 用 `fx_snapshot` 换算，结果存 `reserved_budget_currency_micro`，`fx_snapshot_ref` 留痕。
2. **结算**：实际费用币种 ≠ 预算币种 → 用**结算当时的**快照换算，结果存 `settlement.settled_budget_currency_micro`。

`rate_micro` 是整数（× 1e6），含义是 `1 单位 base = rate_micro/1e6 单位 quote`。

换算方向必须**单向合计保守**——凡是让 `available` 变小的方向一律向上取整：

| 场景 | 取整 |
|---|---|
| 预留：原型币 → 预算币（要占额度） | **ceil** |
| 结算：原型币 → 预算币（要扣消费） | **ceil** |
| 退回/释放：预算币 → 原型币（只是展示） | floor |

实现上统一成一句：`converted = ceil(amount_micro * rate_micro / 1_000_000)`，用整数运算，**不经过浮点**（`(amount * rate + 999_999) / 1_000_000` 用整型除法）。

`fx_snapshot.rate_micro` 已由来源方包含安全余量（`source` 标注 ECB/OANDA/CONTRACT 各自的余量策略）。**预留与结算各用各自时刻的快照**，差额不悄悄吞掉——写一条 `ADJUST(reason_code='FX_DIFF')`。

后续所有对 `budget.outstanding_reserved_micro` / `budget.consumed_micro` 的加减，一律使用预算币种的整数金额。

### 1.7 `ledger_entry` —— 只追加的分录（账本的账本）

```sql
CREATE TABLE ledger_entry (
  entry_id         text PRIMARY KEY,
  tenant_id        text NOT NULL,
  entry_seq        bigint NOT NULL,        -- 每 tenant 单调，由 ledger_sequence 分配
  budget_id        text NOT NULL REFERENCES budget(budget_id),
  currency         char(3) NOT NULL,

  entry_type       text NOT NULL
    CHECK (entry_type IN ('LIMIT_SET','LIMIT_ADJUST','RESERVE','RELEASE','SETTLE','ADJUST','REFUND','ESCROW_GRANT','ESCROW_RETURN')),
  -- 对 limit 的影响（只有 LIMIT_* 与 ESCROW_* 会用）
  delta_limit_micro     bigint NOT NULL DEFAULT 0,
  -- 对 outstanding_reserved 的影响
  delta_reserved_micro  bigint NOT NULL DEFAULT 0,
  -- 对 consumed 的影响
  delta_consumed_micro  bigint NOT NULL DEFAULT 0,
  -- 对 escrowed_out 的影响
  delta_escrowed_micro  bigint NOT NULL DEFAULT 0,

  reservation_id   text REFERENCES reservation(reservation_id),
  attempt_id       text,
  provider_job_ref text,
  price_snapshot_ref jsonb,
  fx_snapshot_ref  jsonb,

  -- 去重：业务唯一键
  business_key     text NOT NULL,
  -- 归因
  attribution      jsonb NOT NULL,   -- {project,series,episode,run,shot,attempt,provider_job,modality,model,channel}
  reason           text,
  actor            jsonb NOT NULL,   -- {principal_id, principal_type, delegation_id} 或 {service:'S26'}
  correlation_id   text NOT NULL,
  causation_id     text,
  posted_at        timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT le_seq_uniq UNIQUE (tenant_id, entry_seq),
  CONSTRAINT le_business_key_uniq UNIQUE (tenant_id, business_key),
  CONSTRAINT le_single_axis CHECK (
    (delta_limit_micro <> 0)::int
    + (delta_reserved_micro <> 0)::int
    + (delta_consumed_micro <> 0)::int
    + (delta_escrowed_micro <> 0)::int = 1
  )
);
CREATE INDEX le_budget_idx ON ledger_entry (budget_id, entry_seq);
CREATE INDEX le_res_idx    ON ledger_entry (reservation_id) WHERE reservation_id IS NOT NULL;
CREATE INDEX le_attempt_idx ON ledger_entry (tenant_id, attempt_id) WHERE attempt_id IS NOT NULL;
CREATE INDEX le_time_idx   ON ledger_entry (tenant_id, posted_at);

CREATE SEQUENCE ledger_sequence;  -- 或按 tenant 分区序列
```

**分录是不可变的：没有 UPDATE，没有 DELETE。** 对账差额追加 `ADJUST`，不修改历史（docs/12 "对账差额追加adjustment，不篡改历史"）。用 DB 权限（`REVOKE UPDATE, DELETE`）+ 触发器兜底。

`le_single_axis` 约束：一条分录只影响一个维度。因此一次业务动作可能写多条分录，各带自己的 `business_key`，**在同一事务内**：

| 业务动作 | 分录 |
|---|---|
| `set_limit`（根） | `LIMIT_SET`: `delta_limit = 新值`（只有根预算，需审批） |
| `set_limit`（非根） | 不存在这个动作。非根的 limit 只能来自 `grant_escrow` |
| `reserve` | `RESERVE`: `delta_reserved = +R` |
| `release` | `RELEASE`: `delta_reserved = −R` |
| `accrue` | **无分录**（不动计数，见 §3.1） |
| `settle` | `SETTLE`: `delta_reserved = −R` **和** `SETTLE`: `delta_consumed = +F`（两条） |
| `grant_escrow` | `ESCROW_GRANT`: **父** `delta_escrowed = +x` **和** **子** `delta_limit = +x`（两条，各自记在自己的 budget 下） |
| `return_escrow` | `ESCROW_RETURN`: 父 `delta_escrowed = −x` 和 子 `delta_limit = −x` |
| `adjustment` | `ADJUST`: 有两个子型——加额度写 `delta_limit`，销账写 `delta_consumed` |
| `refund` | `REFUND`: `delta_consumed = −x` |

`business_key` 加后缀区分同动作的多条：`settle:{id}:res` / `settle:{id}:con`。**多维度混合分录是账本事故的常见来源，这里直接从 schema 禁止。**

注意 `AUTHORIZE` 和 `ACCRUE` 不在 `entry_type` 里——它们**不改变任何金额**，只是状态推进。把它们记进账本会让账本不再是金额的权威。状态推进靠 `reservation` 的时间戳字段 + `finance.ledger.entry-posted` 之外的观测。

`ledger_entry` 与 `budget` 的四个计数必须恒等（§9 每日作业第 3 项）：

```
budget.limit_micro                 = Σ e.delta_limit_micro
budget.consumed_micro              = Σ e.delta_consumed_micro
budget.outstanding_reserved_micro  = Σ e.delta_reserved_micro
budget.escrowed_out_micro          = Σ e.delta_escrowed_micro
```

全部按 `budget_id` 分组。任一条不成立即为 P1 事故——**这意味着账本可以完全重放重建计数**，`budget` 表里的四个数只是缓存。这满足 docs/12 "每次状态变化可由快照和事件恢复"。

**孤儿检查**（每对 escrow 分录必须成对）：

```
Σ_{父侧 ESCROW_GRANT} x  ==  Σ_{子侧 ESCROW_GRANT} x   （按 escrow 对分组）
```

`business_key` 是幂等键的具体化，规则见 §1.9。

### 1.8 `usage_accrual` 与 `settlement`

```sql
CREATE TABLE usage_accrual (
  accrual_id       text PRIMARY KEY,
  tenant_id        text NOT NULL,
  reservation_id   text NOT NULL REFERENCES reservation(reservation_id),
  attempt_id       text NOT NULL,

  -- 供应商口径
  provider_job_ref text,
  billing_unit     text NOT NULL
    CHECK (billing_unit IN ('request','generation_second','character','token','pixel','gpu_second','cpu_second','megabyte')),
  billed_quantity  bigint NOT NULL CHECK (billed_quantity >= 0),
  quantity_scale   integer NOT NULL DEFAULT 1,       -- 例如 token 按 1000 计

  -- 金额（原币 + 预算币种）
  currency         char(3) NOT NULL,
  billed_micro     bigint NOT NULL CHECK (billed_micro >= 0),
  budget_currency  char(3) NOT NULL,
  billed_budget_currency_micro bigint NOT NULL CHECK (billed_budget_currency_micro >= 0),

  -- 保守暂记 vs 最终
  stage            text NOT NULL CHECK (stage IN ('PROVISIONAL','FINAL')),
  basis            text NOT NULL
    CHECK (basis IN ('PROVIDER_INVOICE','PROVIDER_QUOTE_CAP','ESTIMATE_CONSERVATIVE','RECONCILED')),
  evidence_ref     jsonb,
  invoice_ref      text,

  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ua_provisional_no_invoice CHECK ((stage='FINAL') OR invoice_ref IS NULL)
);
CREATE UNIQUE INDEX ua_business_uniq
  ON usage_accrual (tenant_id, reservation_id, stage, coalesce(invoice_ref,''));
CREATE INDEX ua_res_idx ON usage_accrual (reservation_id);

CREATE TABLE settlement (
  settlement_id    text PRIMARY KEY,
  tenant_id        text NOT NULL,
  reservation_id   text NOT NULL REFERENCES reservation(reservation_id),
  accrual_ids      text[] NOT NULL,
  currency         char(3) NOT NULL,
  settled_micro    bigint NOT NULL,
  budget_currency  char(3) NOT NULL,
  settled_budget_currency_micro bigint NOT NULL,
  overage_micro    bigint NOT NULL DEFAULT 0,     -- >0 = 实际超预留
  unused_released_micro bigint NOT NULL DEFAULT 0, -- 预留未被花掉的部分
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT st_one_per_reservation UNIQUE (reservation_id)
);
```

**`provisional` 是关键**：供应商账单延迟时，先用 `PROVIDER_QUOTE_CAP`（报价上界）或 `ESTIMATE_CONSERVATIVE` 暂记，`pending_reconciliation = true`。docs/12 §4："供应商账单延迟时保持pending_reconciliation。"

暂记金额 = `max(报价上界, 已观测用量 × 单价)`。**取大的**——保守方向。

### 1.9 幂等业务键规则

每条 `ledger_entry` 的 `business_key` 由服务端按固定规则生成，**不接受客户端指定**：

```
RESERVE        : reserve:{reservation_id}
AUTHORIZE      : authorize:{reservation_id}
ACCRUE         : accrue:{accrual_id}
SETTLE         : settle:{settlement_id}
RELEASE        : release:{reservation_id}:{reason}
ADJUST         : adjust:{adjustment_id}
REFUND         : refund:{refund_id}
ESCROW_GRANT   : escrow:{parent}:{child}:{grant_cmd_id}
ESCROW_RETURN  : escrow_ret:{parent}:{child}:{return_cmd_id}
```

`UNIQUE (tenant_id, business_key)` 让"重复投递无重复扣减"变成一个数据库约束，而不是应用层 if。**这是 docs/04 §3 第 7 步"每一步有唯一业务事件键，重复投递无重复扣减"的落地。**

### 1.10 幂等、Outbox、Inbox

同 S08 §1.6。S23 的 Outbox 约束：

```sql
UNIQUE (aggregate_id, aggregate_sequence, event_type)
-- aggregate_id = budget_id，aggregate_sequence = ledger_entry.entry_seq（budget 内）
```

**两层幂等，不要混为一谈：**

| 层 | 键 | 防的是什么 | 位置 |
|---|---|---|---|
| API 命令 | `tenant_id + command_id + request_hash` | 客户端重试把同一笔 RESERVE 提交两次 | `idempotency_record` |
| 账本事件 | `tenant_id + business_key`（§1.9） | **内部路径**重复落账 —— 重放的事件、补偿重试、跨服务重投 | `ledger_entry` 唯一约束 |

第二层不能代替第一层：`business_key` 由服务端按 `reserve:{reservation_id}` 这类规则生成，
两个**不同**的 `command_id` 如果指向同一 `reservation_id`，第二层会正确拒绝第二笔，
但那已经是业务冲突，应该返回 `409 RESERVATION_ALREADY_LEDGERED` 而不是假装成功。
第一层则要求"同 command 同 hash → 回放原响应"，语义是**重试**，不是冲突。

`SettleSaga` 里的每一步都同时受两层保护。docs/04 §3 第 7 步要求的"每一步有唯一业务事件键，
重复投递无重复扣减"是第二层；AGENTS.md 要求的幂等键是第一层。两者都要有。

### 1.11 其余表

```sql
CREATE TABLE adjustment (
  adjustment_id   text PRIMARY KEY,
  tenant_id       text NOT NULL,
  budget_id       text NOT NULL REFERENCES budget(budget_id),
  currency        char(3) NOT NULL,
  amount_micro    bigint NOT NULL,           -- 有符号
  reason_code     text NOT NULL
    CHECK (reason_code IN ('BILL_DRIFT','FX_DIFF','QUOTE_UNDERESTIMATE','CREDIT','MANUAL','REFUND')),
  detail          text NOT NULL,
  -- 调整来源：账单对账、汇率、超额、人工
  source_ref      jsonb,
  approval_ref    jsonb NOT NULL,            -- 必须有审批（S02 的预算增加权限）
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE refund (
  refund_id       text PRIMARY KEY,
  tenant_id       text NOT NULL,
  budget_id       text NOT NULL REFERENCES budget(budget_id),
  reservation_id  text REFERENCES reservation(reservation_id),
  currency        char(3) NOT NULL,
  amount_micro    bigint NOT NULL CHECK (amount_micro > 0),
  reason_code     text NOT NULL
    CHECK (reason_code IN ('PROVIDER_CREDIT','FRAUD','SERVICE_CREDIT','CONTRACT')),
  -- ★ 关键：退款必须来自外部确认，不能是我方"认为应该退"
  external_credit_ref text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE budget_freeze (
  freeze_id       text PRIMARY KEY,
  tenant_id       text NOT NULL,
  budget_id       text NOT NULL REFERENCES budget(budget_id),
  frozen_at       timestamptz NOT NULL DEFAULT now(),
  reason          text NOT NULL,
  unfrozen_at     timestamptz
);
```

---

## 2. 命令与查询 API

同 S08 §2 的传输与幂等约定。

### 2.1 `POST /budget-authorizations`（= reserve）

服务目录写 `POST /budget-authorizations` 作为命令入口。本文把它作为**主命令**，语义为"预留 + 授权"——因为准入 Saga 第 1 步和第 4 步都以它为先。实际拆成两个子操作：

- `reserve`（准入第 1 步）：`POST /budget-authorizations` 带 `intent: "RESERVE"`
- `confirm_authorization`（准入第 4 步）：`POST /budget-authorizations/{id}:authorize`

```jsonc
// 准入第 1 步：预留
POST /budget-authorizations
{
  "command_id": "cmd_...",
  "intent": "RESERVE",
  "budget_scope_id": "budget_ep3",        // 或 tenant/project 链上的任一层
  "attempt_id": "attempt_01H...",
  "run_id": "run_...",
  "execution_fence": 2,
  "node_id": "shot_3_2.video",
  "node_epoch": 4,
  "manifest_ref": {...},
  "route_decision_ref": {...},
  "currency": "USD",
  "amount_micro": 800000,
  "amount_conservative_micro": 900000,     // 报价上界，必填且 >= amount
  "budget_currency": "USD",                // 目标预算行的币种；与 currency 不同时必须带 fx_snapshot_ref
  "price_snapshot_ref": { "service_id":"S24","id":"...","revision":7,"content_hash":"…" },
  "quote_expires_at": "2026-10-30T09:15:00Z",
  "fx_snapshot_ref": null,                 // 非本币时必填
  "idempotency_scope": null                // 可空；默认 attempt_id
}
```

**客户端不传 `reserved_budget_currency_micro`**——它由服务端按 §1.5 的公式算。客户端无法通过调大调小这个值来绕过额度。

响应 `200`：

```jsonc
{
  "reservation_id": "res_01H...",
  "state": "RESERVED",
  "currency": "USD",
  "amount_micro": 800000,
  "amount_conservative_micro": 900000,
  "budget_currency": "USD",
  "reserved_budget_currency_micro": 900000,   // ← 按上界预留，不是按 800000
  "budget_id": "budget_ep3",
  "budget_chain": ["budget_tenant","budget_proj","budget_ser","budget_ep3"],
  "budget_after": {
    "budget_id": "budget_ep3",
    "limit_micro": 50000000,
    "consumed_micro": 12000000,
    "outstanding_reserved_micro": 3600000,    // 2700000 + 900000
    "available_micro": 34400000
  },
  "quote_expires_at": "2026-10-30T09:15:00Z",
  "replayed": false
}
```

`budget_after.available_micro` 是**这一层**的可用额，不是整链的。链上每一层的可用额单独查（§2.6 `GET /budgets/{id}/availability`）。

**契约冲突注意**：`contracts/openapi.v2.yaml` 里没有 `POST /budget-authorizations`（见 §8 C23-1），上面这个请求体与响应形状是本文定义的提案。

### 2.2 `POST /budget-authorizations/{id}:authorize`

准入第 4 步前半。把 `RESERVED → AUTHORIZED`，表示资金已锁定可用、等待 dispatch。**这只是状态推进，不再改计数**（计数在 RESERVE 时已经加了）。

```jsonc
{ "command_id":"cmd_...", "expected_revision": 1, "quote_still_valid": true }
```

响应 `{ "reservation_id":"...", "state":"AUTHORIZED", "authorized_at":"..." }`。

**`quote_still_valid: false` 时返回 409 `QUOTE_EXPIRED`**。S26 必须重新报备（新 reservation），不能沿用过期报价继续付费。

### 2.3 `POST /reservations/{id}:release`

补偿。释放未结算的预留。

```jsonc
{
  "command_id": "cmd_...",
  "reason": "SAGA_ABORTED",   // SAGA_ABORTED|QUOTA_FAILED|FENCE_SUPERSEDED|QUOTE_EXPIRED|NEVER_DISPATCHED|RECONCILED_ABSENT|OPERATOR
  "evidence_ref": { "service_id":"S22","id":"permit_...","revision":2,"content_hash":"…" }
}
```

`evidence_ref` 对 `QUOTA_FAILED` / `SAGA_ABORTED` 必填（证明 Saga 真的中止了）。响应 `{ "state":"RELEASED", "released_micro":..., "budget_after":{...} }`。

**重要**：`ACCRUED` 状态的 reservation **不能 release**（返回 409 `RESERVATION_NOT_RELEASABLE`）——已经暂记了实际费用，必须先 settle 或 adjust。

### 2.4 `POST /reservations/{id}:accrue`（内部，S26 调用）

记录实际用量。这是"暂记"，不是结算。

```jsonc
{
  "command_id": "cmd_...",
  "accrual": {
    "billing_unit": "generation_second",
    "billed_quantity": 240,
    "quantity_scale": 1,
    "currency": "USD",
    "billed_micro": 780000,
    "stage": "PROVISIONAL",              // PROVIDER_INVOICE 还没到手时用 PROVISIONAL
    "basis": "PROVIDER_QUOTE_CAP",
    "evidence_ref": { "service_id":"S27","id":"...","revision":1,"content_hash":"…" },
    "provider_job_ref": "job_xyz"
  }
}
```

响应 `200`：

```jsonc
{
  "accrual_id": "ua_...",
  "reservation_state": "ACCRUED",
  "pending_reconciliation": true,
  "budget_after": {
    "budget_id": "budget_ep3",
    "limit_micro": 50000000,
    "consumed_micro": 12000000,          // ← 不变
    "outstanding_reserved_micro": 3600000, // ← 不变，仍占着上界 R
    "available_micro": 34400000
  }
}
```

**`accrue` 不改任何 budget 计数**（见 §3.1）。`PROVISIONAL` 只把 `pending_reconciliation` 置为 true，并把金额写进 `usage_accrual`。真正的计数变化发生在 `settle`。

### 2.5 `POST /reservations/{id}:settle`

终态结算。可以重复调用（幂等，同 `settlement_id`）。

```jsonc
{
  "command_id": "cmd_...",
  "terminal_state": "PROVIDER_SUCCEEDED",   // PROVIDER_SUCCEEDED|FAILED|CANCELED
  "final_accruals": [
    { "billing_unit":"generation_second","billed_quantity":240,"currency":"USD",
      "billed_micro":780000,"stage":"FINAL","basis":"PROVIDER_INVOICE",
      "invoice_ref":"inv_123","provider_job_ref":"job_xyz" }
  ],
  "evidence_ref": { "service_id":"S26","id":"...","revision":9,"content_hash":"…" }
}
```

响应：

```jsonc
{
  "settlement_id": "st_...",
  "reservation_state": "SETTLED",
  "settled_micro": 780000,              // F，原币
  "reserved_micro": 900000,             // R，预算币种（= 上界，不是 800000）
  "settled_budget_currency_micro": 780000,
  "unused_released_micro": 120000,      // R − F
  "overage_micro": 0,                   // max(0, F − R)
  "budget_after": {
    "budget_id":"budget_ep3",
    "limit_micro": 50000000,
    "consumed_micro": 12780000,
    "outstanding_reserved_micro": 2700000,
    "available_micro": 34420000
  },
  "pending_reconciliation": false
}
```

`unused_released_micro` 与 `overage_micro` 互斥，且恒有 `unused_released_micro − overage_micro = R − F`。

**`overage_micro > 0` 时仍在同一事务内结算**（docs/12 "最终供应商费用可能超报价，需保守暂记与新任务冻结机制"）：

1. `consumed += F`。若结果使 `consumed + outstanding + escrowed > limit`，本层 `state = 'EXHAUSTED'`。
2. 在 `adjustment` 表写一行 `(amount_micro = overage, reason_code='QUOTE_UNDERESTIMATE', source_ref = settlement_id)`。**不写 `ledger_entry`**——`SETTLE` 已经把 `F` 全额记进 `consumed`，再记一条 ADJUST 就是重复计数。`adjustment` 表是"原因说明"，不是金额流水；§1.7 的 `le_single_axis` 约束强制了这条边界。
3. 发 `finance.ledger.entry-posted`，`data.budget_after.state = EXHAUSTED`。
4. **不向上传播**（见 §3.3 与 §4.5）。若父级因此越界，说明 `grant_escrow` 的校验有 bug，属 P1 事故而非正常运行路径。

**不承诺"绝不超预算"**，只承诺"超了立刻冻结，不让下一笔再花"。这是 docs/12 明确接受的取舍：供应商实际费可能超报价，宁可事后冻结也不在准入时过度保守到无法工作。

### 2.6 查询

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/budgets/{budget_id}` | 单层：limit/consumed/outstanding/pending_reconciliation（openapi 已定义） |
| GET | `/budgets/{budget_id}/availability` | 单层 `available_micro` + 分解 |
| GET | `/budgets/{budget_id}/tree?depth=` | 子树各层可用额 |
| GET | `/budgets?tenant_id=&scope_type=&scope_ref=` | 列表 |
| GET | `/reservations/{id}` | 单预留 |
| GET | `/reservations?attempt_id=&state=&run_id=&from=&to=` | 列表 |
| GET | `/ledger-entries?budget_id=&from_entry_seq=&limit=` | 分录流（审计/导出） |
| GET | `/attempts/{attempt_id}/cost` | 单 attempt 的全部费用（含失败返工） |
| GET | `/episodes/{episode_id}/cost-breakdown?cohort=` | 集级成本归因（给 S44 用） |
| GET | `/reconciliation-queue?severity=&limit=` | 待对账列表 |

`GET /budgets/{id}` 响应必须严格符合 openapi：

```jsonc
{
  "budget_id": "...", "currency": "USD",
  "limit_micro": 50000000,
  "consumed_micro": 12000000,
  "outstanding_reserved_micro": 2700000,
  "pending_reconciliation": false
}
```

这里**没有 `available_micro`**——openapi 定死了 `additionalProperties:false`。可用额只能通过 `/availability` 拿到。这是有意的：docs/04 §5 "需要强一致的余额…回源权威服务"，而 UI 常常把 `available` 缓存成静态值。强制它单独取，能减少误用。

### 2.7 错误码

| HTTP | code | retryable | 场景 |
|---|---|---|---|
| 400 | `MALFORMED_REQUEST` | false | schema/JCS |
| 403 | `TENANT_SCOPE_DENIED` | false | 租户不一致 |
| 404 | `NOT_FOUND` | false | **含跨租户** |
| 409 | `INSUFFICIENT_BUDGET` | **false** | 可用额不足。**不是 402，不是 500** |
| 409 | `BUDGET_EXHAUSTED` | false | 该层已 EXHAUSTED |
| 409 | `BUDGET_FROZEN` | false | 本行 FROZEN（或祖先 FROZEN 且本次需要 `grant_escrow`） |
| 409 | `BUDGET_PERIOD_CLOSED` | false | 账期已关 |
| 409 | `QUOTE_EXPIRED` | false | `quote_expires_at` 已过 |
| 409 | `PRICE_SNAPSHOT_STALE` | false | `price_snapshot_ref` 指向的报价已失效 |
| 409 | `FX_SNAPSHOT_MISSING` | false | 跨币种缺汇率 |
| 409 | `RESERVATION_NOT_RELEASABLE` | false | 对 ACCRUED/SETTLED 调 release |
| 409 | `RESERVATION_NOT_SETTLEABLE` | false | 对 RELEASED/EXPIRED 调 settle |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同键不同 body |
| 409 | `VERSION_CONFLICT` | true | `expected_revision` 不匹配 |
| 409 | `RESERVATION_ALREADY_SETTLED` | false | 重复 settle 但 accrual 不同（金额冲突） |
| 422 | `AMOUNT_NOT_POSITIVE` | false | `amount_micro <= 0` |
| 422 | `CONSERVATIVE_BELOW_AMOUNT` | false | `amount_conservative_micro < amount_micro` |
| 422 | `CURRENCY_MISMATCH` | false | 预算币种与报价币种不同且无 fx snapshot |
| 422 | `SCOPE_NOT_ON_CHAIN` | false | `budget_scope_id` 不在该 episode/run 的祖先链上 |
| 422 | `ACCRUAL_EXCEEDS_CONSERVATIVE_CAP` | false | `PROVISIONAL` 暂记超过报价上界 × 超标容忍 |
| 429 | `LEDGER_BACKPRESSURE` | true | 该 budget 的写入队列超限 |
| 503 | `WRITER_EPOCH_STALE` | true | 旧写主 |

**`INSUFFICIENT_BUDGET` 用 409 而不是 402**：402 在多数网关里被当成计费错误，且语义上暗示"要先付款"，容易误导客户端去重试。409 明确定位为状态冲突。docs/13 §1 "明确budget/permission错误不能伪装可重试500"——所以 `retryable:false`。

---

## 3. 状态机

### 3.1 Reservation

```
  (无)
   │ reserve（占 available）
   ▼
 RESERVED ──quote 过期 + janitor──► EXPIRED        （释放）
   │
   │ authorize（准入第 4 步）
   ▼
 AUTHORIZED ──quote 过期──► EXPIRED                （释放；但若 S22 permit 已 INFLIGHT 则禁止）
   │
   │ accrue（S26 拿到观测或暂记）
   ▼
 ACCRUED ────────────────────────┐
   │                             │
   │ settle（终态）               │ release（只在 RECONCILED_ABSENT 时）
   ▼                             ▼
 SETTLED                       RELEASED

 RESERVED / AUTHORIZED ──Saga 中止──► RELEASED
```

记 `R = reserved_budget_currency_micro`（预留时锁定的上界），`P` = 暂记额，`F` = 最终实际额。

| 从 | 到 | 触发 | 计数影响 |
|---|---|---|---|
| （无） | RESERVED | `reserve` | `outstanding += R` |
| RESERVED | AUTHORIZED | `authorize` | 无 |
| RESERVED | RELEASED | `release` | `outstanding -= R` |
| AUTHORIZED | RELEASED | `release`（Saga 中止/配额失败/fence） | `outstanding -= R` |
| AUTHORIZED | ACCRUED | `accrue` | **无**。`R` 继续占用，`P` 只写进 `usage_accrual` 与 `pending_reconciliation` |
| ACCRUED | SETTLED | `settle` | `outstanding -= R`；`consumed += F` |
| ACCRUED | RELEASED | `release(RECONCILED_ABSENT)` | **只在确认供应商从未收到时**：`outstanding -= R`，`consumed` 不变 |
| RESERVED | EXPIRED | janitor（quote 过期且从未 authorize） | `outstanding -= R` |
| AUTHORIZED | EXPIRED | **禁止**，见下 | — |

于是 `settlement.unused_released_micro = R − F`（`F ≤ R` 时）；`settlement.overage_micro = max(0, F − R)`。两者互斥。

**accrue 不改任何 budget 计数，这是刻意的。** 三个理由：

1. docs/12 §4 与 E16 要求"未知提交保留额度"。如果 accrue 就把 `R` 放掉，那么 `ACCRUED` 状态下的 reservation 只占 `F`，而 `F` 当时可能只是保守暂记——真正超支时额度已经不在手上了。
2. `ACCRUED` 只表示"已经知道产生了多少费用"，不表示"这笔费用的归属已经查清"。账单未到时 `P` 可能小于真实，也可能大于真实。
3. 只有一个释放点（settle）比两个释放点更容易证明没有泄漏。

```sql
-- settle 事务里的计数更新，一次做完
UPDATE budget
   SET outstanding_reserved_micro = outstanding_reserved_micro - $R,
       consumed_micro             = consumed_micro + $F,
       updated_at = now()
 WHERE budget_id = $b;
```

`R − F` 自动回到 available，不需要单独的 RELEASE 分录。但**要写一条 `SETTLE` 分录**，其中 `delta_reserved = -R`、`delta_consumed = +F`——等等，这违反 §1.7 的 `le_single_axis` 约束。所以 settle 实际写**两条**分录：

```
SETTLE_RESERVED_RELEASE : delta_reserved = -R
SETTLE_CONSUMED         : delta_consumed = +F
```

`business_key` 分别为 `settle:{settlement_id}:res` 与 `settle:{settlement_id}:con`。这两条在同一事务内，要么都进要么都不进。

**`AUTHORIZED` 不能 expire。** 原因：S22 的 permit 可能已经 `DISPATCH_AUTHORIZED` 或 `INFLIGHT`，钱不能单方面放手。要比对 S22 permit 状态后才允许 —— 但 S23 **不读 S22 的库**，所以这条判断只能通过：`release` 命令由 S26 在确认 S22 permit 已释放后调用。S23 不主动 expire `AUTHORIZED`。

`EXPIRED` 与 `RELEASED` 的区别只在审计语义：`EXPIRED` 是时间驱动，`RELEASED` 是命令驱动。两者计数影响相同。

### 3.2 非法转移

一律 409，且**不改变任何计数**，不写分录：

- `SETTLED → *`：终态。
- `EXPIRED → *`：终态。要再用钱就新建 reservation。
- `ACCRUED → AUTHORIZED`：反向。
- 对 `SETTLED` 调 `release`：409 `RESERVATION_NOT_RELEASABLE`。

### 3.3 Budget

```
ACTIVE ──available 用尽──► EXHAUSTED ──adjust 补充──► ACTIVE
  │                            │
  │──freeze（人工/超支/审计）──►│
  ▼                            ▼
FROZEN ──unfreeze──► ACTIVE   (EXHAUSTED 也可 freeze)
  │
  │ 账期结束
  ▼
CLOSED   （终态；期末余额结转走新 period 的新 budget 行）
```

- `EXHAUSTED`：**本层**拒绝新 reserve，已有 reservation 正常结算（docs/12 "暂停不等于退款"）。
- `FROZEN`：**本层**拒绝新 reserve 与新的 `grant_escrow`，同样不影响已有。
- **祖先被冻结不直接阻断子级**。因为额度在 `grant_escrow` 时已经下推，子级 `limit` 是它自己的。祖先冻结只是"不再给新额度"。
- **但整棵树的冻结传播由 S02 governance 的 kill switch 层负责**：需要"全停"时，S02 会令 S26 停止发起新 Saga，而不是靠 S23 逐级级联。把级联冻结放进 S23 会让同一件事有两处实现。

  这是刻意的边界：**S23 只管钱，不管"要不要干活"。** 若真要让子级也停，正确做法是在子级的 budget 行上直接 `FROZEN`（运维动作，可审计），而不是由父级隐式推导。

### 3.4 `pending_reconciliation` 的语义

`GET /budgets/{id}.pending_reconciliation` 在该 budget 下存在 `state='ACCRUED'` 且 `basis != 'PROVIDER_INVOICE'` 的 reservation 时为 true。

这是**只读派生值**，不落库为可写字段（避免第二权威）。查询时算，或由物化视图维护并带 `source_revision`。

注意范围是**该 budget 行自己的 reservation**，不含子级的。要判断整棵树有没有未对账，查 `/budgets/{id}/tree` 或看父级的 rollup。

---

## 4. Saga 与补偿

### 4.1 七步准入里 S23 的位置

| 步 | 动作 | S23 侧 |
|---|---|---|
| 1 | S26 → S23 `reserve` | RESERVED，占 available |
| 2 | S26 → S22 `acquire` | 不参与 |
| 3 | S26 落 SubmissionIntent | 不参与 |
| 4 | S26 → S23 `authorize`；S26 → S22 `confirm-dispatch` | RESERVED → AUTHORIZED |
| 5 | S26 → S27 grant | 不参与 |
| 6 | S27 → S26 观测 | 不参与 |
| 7 | S26 → S23 `accrue`/`settle`；S26 → S22 `release` | ACCRUED → SETTLED |

### 4.2 各步失败补偿

| 失败点 | S23 已发生 | 补偿 | 关键点 |
|---|---|---|---|
| **步 1 失败**（额度不足） | 无 | 无 | 409 `INSUFFICIENT_BUDGET`，S26 不进 Saga |
| **步 2 失败**（S22 无容量） | RESERVED | S26 → `release(QUOTA_FAILED)`，带 S22 的拒绝证据 | E14 |
| **步 3 失败**（S26 崩溃） | RESERVED | janitor 按 `quote_expires_at` 过期 | 只在从未 AUTHORIZED 时 |
| **步 4 S23 authorize 失败**（报价过期） | RESERVED | S26 → `release(QUOTE_EXPIRED)` | 不继续 |
| **步 4 S22 confirm 失败** | AUTHORIZED | S26 → `release(QUOTA_FAILED)` | 需 S22 确认 permit 已释放 |
| **步 5 grant 失败** | AUTHORIZED | **不释放**。等对账 | 可能已提交 |
| **步 6 结果未知** | AUTHORIZED | **不释放，保持占用** | E16 核心 |
| **步 7 accrue 失败** | AUTHORIZED | 重试；若永久失败则告警 | 不能悄悄释放。`R` 仍占着，本来就安全 |
| **步 7 settle 失败** | ACCRUED | 重试（幂等）；金额不变 | `R` 仍占着，`P` 已记录 |
| **步 7 S22 release 失败** | SETTLED | S26 重试；S23 侧已终态，不受影响 | 槽泄漏可见但不自动处理（S22 §4.2） |

**为什么 S23 在未知态"什么都不做"是对的**：因为 `accrue` 不动计数（§3.1），`R` 从 `reserve` 起就一直占着，直到 `settle` 或 `release`。未知期间不需要额外的"冻结"动作——它天然就是冻结的。这比"进入未知态时加锁、退出时解锁"少一套状态，也少一类 bug。

### 4.3 `SUBMISSION_UNKNOWN` 的对账流程（S23 视角）

这是整条链最容易出错的地方。S23 的规则：**在未知态，钱既不能退也不能再花。**

```
状态：reservation = AUTHORIZED，attempt = SUBMISSION_UNKNOWN
      钱已从 available 扣掉（outstanding），不能给别的任务用。

S23 侧动作：什么都不做。不写分录，不改计数。
           仅当 S26 发来 reconcile 结果时才动。

对账结果的三种走向：

A) 供应商确认已生成（S26 拿到 provider_job_id + 归档产物）
   → S26 → S23 accrue(PROVISIONAL 或 FINAL) → settle
   → 走正常路径

B) 供应商确认从未收到（有查询证据：list jobs 为空 / 查无此 id）
   → S26 → S23 release(reason='RECONCILED_ABSENT', evidence_ref=S26 的查询结果)
   → RESERVED/AUTHORIZED → RELEASED，available 恢复
   → ★ 必须有 evidence_ref，否则 422

C) 供应商无法确认（无幂等、无查询）
   → 永远保持 AUTHORIZED
   → S23 每 24h 告警：attempt 卡在未知态超过 N 小时，涉及金额 X
   → 运维可 `release(OPERATOR)`，但必须：
       · 记 audit（S45）
       · 写 ADJUST 分录，reason_code='MANUAL'，备注销金额与理由
       · 该 attempt 的后续重试必须用**新的** reservation
   → 绝不自动释放
```

**为什么 C 不能自动释放**：释放了 available 就变大，下一笔就可能在同一账号上再花一次，而第一笔可能真的发生了。**额度泄漏比重复付费便宜。** 这是整条链的价值取向，要写进 runbook。

### 4.4 `SUBSESSION_UNKNOWN`

S23 没有 subsession 概念。所有"可能已产生费用的 attempt"都表现为 `AUTHORIZED` 或 `ACCRUED`。对账流程同上。

如果子系统（S27 的某渠道）内部有"部分提交"的概念（例如批量提交 10 个变体，只有 3 个确认），那属于 S26/S27 的职责：它们对**每一个确认的变体**各发一次 accrue（各带 `provider_job_ref`），对未确认的保持不动。

### 4.5 超支的补偿

`settle` 时 `settled > reserved`：

```
1. 同事务：consumed += F，outstanding -= R
2. 若 consumed + outstanding + escrowed > limit：
     budget.state = 'EXHAUSTED'
     adjustment 表插入 (amount_micro = F − R, reason_code='QUOTE_UNDERESTIMATE')
3. 发 finance.ledger.entry-posted，data.budget_after.state = EXHAUSTED
4. 返回 200（结算成功），响应里 overage_micro > 0
```

**超支不拒绝结算**——钱已经花了，拒绝结算只是丢失记录。拒绝的是**下一笔** reserve。

第 2 步写 `adjustment` 而不是 `ledger_entry`：金额已通过 `SETTLE` 全额记账，再记一笔就是重复计数。`adjustment` 表承担"解释为什么超"的职责，不参与计数。

**为什么不向上传播**：§1.4 的分配树模型里，子级的花费只记在子级自己那行。父级的 `escrowed_out` 在 `grant_escrow` 时就已把 `x` 从自身 available 移走，此后父级不再关心子级怎么花。

这样做的结果是：**子级超支只让子级的 `available` 变负，父级不受影响。** 全树恒等式依然成立，因为负的 available 会计入求和：

```
父 limit=1000, escrowed_out=300（一个子）
子 limit=300, consumed=340（超支 40）, outstanding=0  → 子 available = -40
父 available = 1000 - 0 - 0 - 300 = 700

Σavailable + Σoutstanding + Σconsumed = (700 - 40) + 0 + 340 = 1000 ✓
```

**这正是选这个模型的原因**：超支不需要人为补记就能自然配平，而且责任落点清晰——负的是子级那行，不会污染父级。

代价是父级的 `limit` 不再是对真实花费的硬上限，而是一个"分配额度"。要获得真正的硬上限，只能在 `grant_escrow` 时更保守。这是 docs/12 已接受的取舍。

**告警规则**：任何 `available < 0` 的 budget 行都是 P1 告警。它不是错误（结算路径正常），但意味着该层的分配额需要人工复核。§9 每日作业第 1 条断言的是配平，第 1b 条单独列出所有负值行。

### 4.6 取消与暂停

- 取消 run（S21）→ S08 fence 递增 → S26 对未 dispatch 的 attempt 发 `release(FENCE_SUPERSEDED)`。
- **已 INFLIGHT 的任务，S23 继续结算**（docs/12 "暂停/取消不意味着供应商退款；不可取消任务继续结算"）。
- **绝不因为取消就写 refund**。`refund` 表要求 `external_credit_ref`——只有供应商真的给了信用，才有退款。

---

## 5. 并发与锁

### 5.1 锁顺序（核心）

docs/12 "多级行按固定顺序锁"。锁序键：

```
(depth ASC, budget_id ASC)     -- 从根往下，同深度按 ID 升序
```

**但绝大多数写入只需要一把锁。** 因为 §1.4 的模型把额度在 `grant_escrow` 时就下推给了子级：

| 命令 | 触及的 budget 行 | 说明 |
|---|---|---|
| `reserve` | **1 行**（`budget_id` 本身） | 子级的花的是自己的 `limit`，父级早已让出 |
| `release` | 1 行 | 同上 |
| `settle` | 1 行 | 同上 |
| `grant_escrow` | **2 行**（父、子），按 `(depth, budget_id)` 升序 | 频率低（后台预分配） |
| `return_escrow` | 2 行 | 频率低 |
| `set_limit`（仅根）/ `adjust` | 1 行 | 低频、需审批 |

**这才是 escrow 的真正价值**：不是"少锁几行"，而是把"每次 shot 级预留都要锁 tenant 行"的热点消除掉。不做 escrow 时所有并发都挤在根行上；做了之后每个 run 花自己的预分配，互不相干。

两条链时也不会死锁——因为只有 `grant_escrow` 会同时拿父子两行，且永远按 `(depth ASC, budget_id ASC)` 拿。用一个 SQL 保证：

```sql
-- grant_escrow：唯一会拿两行的常规路径
SELECT * FROM budget
 WHERE budget_id IN ($parent, $child)
 ORDER BY depth ASC, budget_id ASC
 FOR UPDATE;
```

**不要用递归 CTE 锁整条链。** 那是"没有 escrow 时的写法"，在本文模型下会把所有并发又挤回根行，白做了 escrow。

### 5.2 为什么 reserve 不需要看祖先

有人会问："子级在花自己的 limit，那怎么保证不会花超出根的能力？"

答案在 `grant_escrow` 那一刻已经保证了：授予 `x` 时，父级 `escrowed_out += x` 且必须满足父级的不变量 `consumed + outstanding + escrowed ≤ limit`；不满足则 `grant_escrow` 返回 409 `INSUFFICIENT_BUDGET`。所以**任何时刻一个子级的 `limit` 都在父级的 `escrowed_out` 里有对应占位**。子级后来怎么花，都花不掉父级已经预留出去的那部分。

代价：如果 run 的实际花费远小于预分配的 escrow，那部分额度在 run 结束前一直锁着不能给别的 run 用。这是刻意的——`return_escrow` 是显式动作，由 S21/S26 在 run 结束时调用。**宁可额度闲置，不要额度共享时算错。**

**形状校验**：`grant_escrow` 必须验证父级的 `consumed + outstanding + escrowed_out + x ≤ limit`，否则 409。子级侧无需再校验——它的 limit 就是 escrow_balance，恒等成立（§1.4）。

### 5.3 锁顺序总表

```
1. writer_lease            (scope = 'budget:<budget_id>'，FOR SHARE)
2. budget 行               (depth ASC, budget_id ASC)——只有 grant/return escrow 拿两行
3. budget_escrow 行        (parent_budget_id, child_budget_id 升序)
4. reservation 行          (reservation_id 升序)
5. usage_accrual / ledger  (插入，不预锁)
```

`ledger_sequence` 的 `nextval` **不持有锁**（PG sequence 无事务锁），因此不会因为顺序问题死锁。但 entry_seq 可能因回滚产生空洞——**允许空洞**，只要单调且唯一。

### 5.4 隔离级别

`READ COMMITTED` + 显式行锁。不用 `SERIALIZABLE`：所有竞争点在 budget 行锁上，且行锁 + `CHECK` 约束已足以保证不超发。

**关键**：`consumed/outstanding/escrowed` 的更新必须用**相对增量**而非绝对值：

```sql
UPDATE budget
   SET outstanding_reserved_micro = outstanding_reserved_micro + $delta,
       updated_at = now()
 WHERE budget_id = $id;
```

绝不能用 `SET outstanding = <应用算出的值>`。后者在 READ COMMITTED 下会丢更新。

### 5.5 乐观 CAS 版本

`budget.revision` 用于 `authorize`/`release` 等带 `expected_revision` 的命令，以及给 S44 投影判断新鲜度。计数更新也递增 `revision`。

### 5.6 并发争抢最后额度（E20）

场景：budget 剩 100，两个并发 reserve 各要 80。

```
T1: SELECT ... FOR UPDATE budget(b)    → 拿到旧值 avail=100，锁住
T2: SELECT ... FOR UPDATE budget(b)    → 阻塞
T1: CHECK 80 <= 100 → UPDATE outstanding += 80 → COMMIT
T2: 拿到锁，读到 outstanding 已 +80，avail=20 → CHECK 80 > 20 → 409 INSUFFICIENT_BUDGET
```

因为 `FOR UPDATE` 在 READ COMMITTED 下会**重读最新已提交版本**，所以 T2 看到的是 T1 提交后的值。**没有超发窗口。**

必须验证的点：如果 ORM 把 SELECT 缓存了或用了 `REPEATABLE READ`，就会出错。**写死在实现规范里：预算计数读取一律走 `FOR UPDATE`，不用快照读。**

### 5.7 父子 escrow 不双花（E20 第二半）

```
父 limit=1000, consumed=0, outstanding=0, escrowed_out=600（两个子各 300）
子A limit=300, consumed=250, outstanding=40 → available = 300-250-40-0 = 10
子B limit=300, consumed=0,   outstanding=0  → available = 300

父 available = 1000 - 0 - 0 - 600 = 400
```

断言（§1.4 的全树恒等式）：

```
Σ available + Σ outstanding + Σ consumed = 根 limit
(400 + 10 + 300) + (0 + 40 + 0) + (0 + 250 + 0) = 710 + 40 + 250 = 1000 ✓
```

**注意：`escrowed_out` 不出现在这个等式里**——它是父级的分配动作，不是消耗。父级让出的 600 已经在子级的 `limit` 里体现为可以花的额度，计一次就够了。如果把 `escrowed_out` 也算进去就会双算。

§7 T23-08 断言这条。**它是"父子不双花"的完整数学表达**，作为每日一致性作业。

**反例（错误模型）**：如果实现成"子级预留时向上传播到祖先的 `outstanding`"，上面就会变成父级 outstanding 也有 40，等式变成 1040 ≠ 1000，而且父级永远不知道自己真实可用额。这条反例值得写进代码注释。

### 5.8 币种与取整

- 单币种内：整数加减，无取整问题。
- 跨币种：一律 `ceil(amount_micro * rate_micro / 1_000_000)`，整型运算（见 §1.6 表格）。
- **绝不把不同币种的 budget 相加**。有多币种需求时，为每个币种建一组预算行，各自独立计数。事件里带 `currency`，下游自己分组。

---

## 6. 事件

### 6.1 发布：`finance.ledger.entry-posted`

Topic `drama.budget-ledger.v2`，partition key `tenant_id:aggregate_id`，`aggregate_id = budget_id`，`aggregate_sequence = entry_seq`（budget 内）。

```jsonc
{
  "specversion":"1.0","schema_version":"2.0",
  "id":"evt_...","source":"S23","type":"finance.ledger.entry-posted",
  "subject":"budget/budget_ep3","time":"...",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"budget_ep3","aggregate_sequence":1042,
  "correlation_id":"cmd_...","causation_id":"res_...","traceparent":"...",
  "data": {
    "entry_id":"le_...",
    "entry_type":"SETTLE",
    "budget_id":"budget_ep3",
    "currency":"USD",
    "delta_reserved_micro": -800000,
    "delta_consumed_micro": 780000,
    "reservation_id":"res_...",
    "attempt_id":"attempt_...",
    "provider_job_ref":"job_xyz",
    "settled_micro": 780000,
    "overage_micro": 0,
    "pending_reconciliation": false,
    "attribution": {
      "project_id":"...", "series_id":"...", "episode_id":"ep3",
      "run_id":"...", "shot_id":"shot_3_2", "attempt_id":"...",
      "provider_job":"job_xyz", "modality":"video",
      "model":"...", "channel":"seedance_volcengine"
    },
    "budget_after": {
      "limit_micro":50000000, "consumed_micro":12780000,
      "outstanding_reserved_micro":1900000, "available_micro":35220000,
      "state":"ACTIVE"
    }
  },
  "data_ref": null
}
```

`attribution` 必须完整（docs/12 §3 列了全部维度）。S44 靠它做成本归因，缺一个维度就得回源。

S23 **只发这一种 event_type**。其他语义（预算耗尽、对账告警）通过 `data.entry_type` + `data.budget_after.state` 表达，或走独立运维告警通道（不进业务总线）。

理由：topic 目录里 S23 只有一个事件类型；新增会被 schema registry 判为不兼容变更。而且所有变化本质上都是"记了一笔账"。

### 6.2 消费

| 事件 | 来源 | S23 的处理 |
|---|---|---|
| `access.grant.changed` | S01 | 撤销租户 → 该租户所有 `RESERVED` reservation `release(OPERATOR)`；已 INFLIGHT 的不动 |
| `model.capability.changed` | S24 | 只更新 `price_snapshot` 的**有效标记缓存**；不触碰既有 reservation（报价已固定） |
| `connector.observation.received` | S27 | **不消费**。费用只能经 S26 的 accrue/settle 进入 |
| `generation.job.state-changed` | S26 | **不消费**。理由同上——状态机推进不等于记账 |
| `audit.export.ready` | S45 | 无关 |

**S23 绝不直接消费 S27/S26 的事件来记账。** 记账必须走显式命令。原因：

1. 事件是"通知"，命令是"事实"。事件重复/乱序/重放不能导致重复记账。
2. `business_key` 的幂等只在命令层有确定的业务键。
3. 事件里没有 `reservation_id` 的权威映射（只有 S26 知道）。

docs/04 §3 第 7 步写"S23 暂记/对账实际费用"，主语是 S26 发起。保持一致。

### 6.3 事件顺序与 Inbox

同 S08 §6.3。S23 的 consumer：`s23.access`、`s23.price`。

`access.grant.changed` 的乱序：以 `grant.revision` 为准，旧版本丢弃。

### 6.4 账单对账（外部数据，不是事件）

供应商账单通常以文件/API 拉取，不是事件。S23 独立对账进程：

```
每日（或按供应商周期）：
  拉账单 → 按 provider_job_ref 匹配 usage_accrual
  │
  ├─ 匹配上且金额一致 → 把 PROVISIONAL 升级为 FINAL，pending_reconciliation=false
  ├─ 匹配上但金额不同 → 追加 ADJUST(reason='BILL_DRIFT')，金额差额
  ├─ 账单有但我们没有 → 建一条 ADJUST(reason='BILL_DRIFT')，并告警（可能是漏记或跨租户）
  └─ 我们有但账单没有 → 保持 PROVISIONAL，进入待对账队列，超期告警
```

**对账永远只追加，不篡改历史。** 这是 docs/12 §1 "对账差额追加adjustment，不篡改历史"。

---

## 7. 测试清单

### 7.1 幂等与版本（E12）

| # | 用例 | 期望 |
|---|---|---|
| T23-01 `[E12]` | 同 `command_id` 同 body `reserve` ×3 | 同一 reservation，`outstanding` 只 +1 次，`ledger_entry` 只 1 条 |
| T23-02 `[E12]` | 同 `command_id` 不同 body | 409 `IDEMPOTENCY_KEY_REUSED`，无写入 |
| T23-03 | 不同 command 相同 `idempotency_scope` | 返回同一 reservation |
| T23-04 | 重复 `accrue` 同 accrual_id | `usage_accrual` 只 1 条；`budget` 计数**不变** |
| T23-05 | 重复 `settle` 同 settlement_id | 幂等返回；`consumed` / `outstanding` 均不变 |
| T23-06 | 重复 `settle` 同 reservation 不同金额 | 409 `RESERVATION_ALREADY_SETTLED` |
| T23-07 | 重复 `release` 同 command | 返回首次结果，`outstanding` 只减一次 |
| T23-07b | `settle` 事务内两条分录（`:res` + `:con`）原子性 | 注入第二条插入失败 → 第一条也回滚，计数不变 |

### 7.2 多级预算与 escrow（E20）

| # | 用例 | 期望 |
|---|---|---|
| T23-08 `[E20]` | 树形预算，多处并发预留 | 任意时刻 `Σavailable + Σoutstanding + Σconsumed = 根 limit` |
| T23-09 `[E20]` | 父限 100，两子各 50，叶子并发 200 个各 1 元 | 恰好 100 成功，100 个 409；无一层 available < 0 |
| T23-10 `[E20]` | 父子同时预留最后额度 | 无超发；父子不双花 |
| T23-11 `[E20]` | 同一 budget 100 并发 reserve 各 1（剩 10） | 恰好 10 成功 |
| T23-12 | run 的预分配 escrow 用完，再要额度 | `grant_escrow` 锁父+子两行；父级不足则 409；父仍有额度则成功且父 `escrowed_out` 增加 |
| T23-13 `[E26]` | 跨 run 返工：同 episode 下 3 个 run 各自尝试 | 集预算汇总；超限时第四个 run 被拒 |
| T23-14 | 本层 FROZEN | reserve → 409 `BUDGET_FROZEN`，`details.blocking_budget_id` 指向该行 |
| T23-14b | 祖先 FROZEN、子级 ACTIVE | 子级 reserve **仍成功**（额度已下推）；祖先不再接受新的 `grant_escrow` |

### 7.3 准入 Saga 补偿（E14 / E15）

| # | 用例 | 期望 |
|---|---|---|
| T23-15 `[E14]` | reserve 成功 → S22 无容量 → release(QUOTA_FAILED) | `outstanding` 恢复，`available` 复原，无 consumed |
| T23-16 `[E14]` | 同上但 release 命令丢失 | janitor 在 quote 过期后 `EXPIRED`；或 S26 重试 release |
| T23-17 `[E15]` | authorize 后 S22 confirm 超时 | reservation 停在 AUTHORIZED，**不自动过期** |
| T23-18 `[E15]` | authorize 时报价已过期 | 409 `QUOTE_EXPIRED`；S26 走补偿 |
| T23-19 | release 缺 `evidence_ref`（QUOTA_FAILED） | 422，状态不变 |
| T23-20 | 对 ACCRUED 调 release | 409 `RESERVATION_NOT_RELEASABLE` |

### 7.4 未知提交（E16 / E22）

| # | 用例 | 期望 |
|---|---|---|
| T23-21 `[E16]` | SUBMISSION_UNKNOWN 期间 | reservation 保持占用；`available` 不恢复；**没有任何分录** |
| T23-22 `[E16]` | 未知 24h | 告警产生；金额列出；不自动释放 |
| T23-23 `[E16]` | 对账结果 = 已生成 | 正常 accrue → settle；费用正确 |
| T23-24 `[E16]` | 对账结果 = 从未收到 | release(RECONCILED_ABSENT) 成功；`available` 恢复 |
| T23-25 `[E16]` | 对账结果 = 无法确认 | 永久 AUTHORIZED；运维 OPERATOR release 需要 audit + adjustment |
| T23-26 `[E22]` | 取消 run / 不可取消任务 | 已 INFLIGHT 的继续 settle；**无 refund 记录** |
| T23-27 `[E22]` | 暂停 tenant | 新 reserve 拒绝；已有继续结算 |
| T23-27b `[E16]` | `ACCRUED` 状态挂 24h 未 settle | `outstanding` 仍占着 `R`；告警；available 不恢复 |

### 7.5 金额与币种

| # | 用例 | 期望 |
|---|---|---|
| T23-28 | `F > R`（超支） | 结算成功；`overage_micro>0`；本层 EXHAUSTED；`adjustment` 行；**无多余 ledger 分录** |
| T23-29 | 超支后本层 `available < 0` | 全树恒等式仍配平（负值参与求和）；父级不受影响；告警产生 |
| T23-30 | 跨币种 reserve，缺 fx snapshot | 422 `FX_SNAPSHOT_MISSING` |
| T23-31 | 跨币种换算取整 | 一律 ceil（保守），用整型运算可复算 |
| T23-32 | 两个不同币种的预算 | 各自独立计数，绝不互相加减 |
| T23-33 | `amount_conservative < amount` | 422 |
| T23-34 | 金额为 0 或负 | 422 |
| T23-35 | 分录不可变 | 尝试 UPDATE/DELETE ledger_entry → DB 拒绝（权限 + 触发器） |
| T23-35b | 预留按上界 | `amount=800000, conservative=900000` → `outstanding += 900000`，非 800000 |
| T23-35c | `settle` 退回差额 | `R=900000, F=780000` → `consumed += 780000`，`unused_released=120000`，available 净增 120000 |

### 7.6 对账与账单

| # | 用例 | 期望 |
|---|---|---|
| T23-36 | 账单金额 > 暂记，且已 settle | 追加 `ADJUST(BILL_DRIFT, delta_consumed = 差额)`；`pending_reconciliation` 变 false |
| T23-37 | 账单有、本地无 | `ADJUST` + 告警；不重建 reservation |
| T23-38 | 本地有、账单无 | 保持 PROVISIONAL，进待对账队列 |
| T23-39 | 同一账单导入两次 | 幂等（`business_key` 含 invoice_ref） |
| T23-40 | 对账后查 `GET /budgets/{id}` | `pending_reconciliation` 正确 |
| T23-40b | 账单早于 settle 到达（仍 ACCRUED） | 升级 accrual 为 FINAL；settle 时按 FINAL 金额算 `F`；无 ADJUST |

### 7.7 安全与租户（E28）

| # | 用例 | 期望 |
|---|---|---|
| T23-41 `[E28]` | 租户 B 读租户 A 的 budget/reservation | 404 |
| T23-42 `[E28]` | 用别租户的 `budget_scope_id` reserve | 404 |
| T23-43 | 外部 principal 调 accrue/settle | 403（仅内部服务身份） |
| T23-44 | 客户端指定 `business_key` | 被忽略（服务端生成） |

### 7.8 崩溃与恢复

| # | 用例 | 期望 |
|---|---|---|
| T23-45 | reserve 事务在 UPDATE budget 后、INSERT reservation 前崩溃 | 回滚；无残留计数 |
| T23-46 | 事务已提交、Outbox 未发 | relay 补发；计数已正确 |
| T23-47 | `entry_seq` 因回滚产生空洞 | 不影响；下游按 seq 排序仍正确 |
| T23-48 | 从备份恢复 | 用 ledger_entry 重放重建 budget 计数；与备份值一致 |
| T23-49 `[E34]` | DR 切换，旧执行器未知 | 付费准入冻结，直到对账完成；不自动恢复 |

---

## 8. 契约冲突与待裁决

| # | 冲突 | 本文采用 | 需要谁改 |
|---|---|---|---|
| C23-1 | openapi 只有 `GET /budgets/{budget_id}`；`service.yaml` 写命令 `POST /budget-authorizations`，但 openapi 里**没有这个 POST** | 本文 §2.1 | `contracts/openapi.v2.yaml` 补 POST |
| C23-2 | openapi `GET /budgets/{id}` 的 4 个金额字段不足以表达 escrow / available / 多级 | 加 `/availability` 与 `/tree`；**不改既有响应**（`additionalProperties:false`） | openapi 新增路径 |
| C23-3 | `admission-record.reservation` 只有 `amount_micro` + `currency`，无法区分"报价额"与"实际占用额度"（§1.5 里是两个不同的数） | S26 写 `amount_micro` 时填 **`reserved_budget_currency_micro`**（实际占用额），`currency` 填 `budget_currency`。这样 admission-record 上的数是"这单占了多少"，与 `GET /budgets/{id}.outstanding_reserved_micro` 可直接对账 | 契约注释 + S26 实现 |
| C23-4 | docs/12 公式 `available = limit - conservative_consumed - outstanding_reserved` 未含 escrow | 本文 §1.3 给出含 escrow 的完整式 | docs/12 补一句 |
| C23-4b | `admission-record.reservation.state` 只有 5 值，**没有 `EXPIRED`** | S23 的 `EXPIRED` 不外露给 S26：`EXPIRED` 只发生在"从未 authorize"的 reservation 上，此时 S26 的 Saga 已中止、不会再读这条记录。S26 读到的终态只有 `RELEASED`/`SETTLED`。若 S26 需要区分，用 `reason` | 无（与 S22 的 EXPIRED 处理一致） |
| C23-5 | 事件目录里 S23 只允许 `finance.ledger.entry-posted` | 所有语义（耗尽/对账）折叠进该事件的 `data` | 无 |
| C23-6 | `INSUFFICIENT_BUDGET` 无定义的 HTTP 码 | 409（非 402、非 500） | openapi 错误表 |
| C23-7 | `pending_reconciliation` 是 openapi 的必填字段但未定义计算方式 | 本文 §3.4 | 契约注释 |

---

## 9. 运行与运维

- **部署**：独立 Deployment + ServiceAccount；**按 tenant 分区**（S23 优先物理隔离，docs/01 §2）；账本库独立集群。
- **写路径**：单层 budget 锁 + 相对增量 UPDATE；每 tenant 一个写主（`writer_lease`），跨 tenant 并行。
- **对账**：独立 Deployment，独立低峰时段，独立连接池。**绝不与在线准入共享连接池**——账单导入可能很慢。
- **每日一致性作业**（每条独立告警，全部只读）：
  1. `Σavailable + Σoutstanding + Σconsumed = 根 limit`（每棵树）。`available` 可为负，负值也计入求和；
  1b. 列出所有 `available < 0` 的 budget 行 → P1 告警（分配额需复核）；
  2. `父.escrowed_out_micro = Σ 子级 limit_micro`；
  3. `budget` 的四个计数 = 对应 `ledger_entry` 的四个 delta 之和；
  4. `Σ live reservation.reserved_budget_currency_micro = budget.outstanding_reserved_micro`；
  5. `pending_reconciliation` 超过 7 天的 reservation → 告警；
  6. `ACCRUED` 状态停留超过 `unknown_grace`（默认 24h）的 reservation → 告警（E16 兜底）；
  7. `fx_snapshot` 过期但仍被 live reservation 引用 → 告警（跨币种漂移风险）。

  第 1、2 条不成立 = 额度守恒被破坏，最高优先级。
- **runbook 必备**：
  - 额度泄漏（未知提交长期占用）如何人工裁决与留痕；
  - 账单漂移如何定位到 attempt；
  - 汇率快照过期如何处理已预留的 reservation；
  - DR 后如何先对账再开放准入；
  - **`INSUFFICIENT_BUDGET` 的正确响应方式**（不能自动改额度；预算增加是独立权限，自动化档位不能提高额度 —— docs/12 §4）。
- **保留**：财务/审计保留期由客户合同和地区规则配置（docs/04 §4）；媒体原文不保留，只保留最小证明。

---

## 10. 明确不做的

- 不做法定总账、不开票、不处理充值（独立商业计费系统）。
- 不持有配额许可（S22）。
- 不直接消费 S26/S27 事件记账。
- 不因取消/暂停写 refund（必须有 `external_credit_ref`）。
- 不承诺绝不超预算；只承诺超了立刻冻结并留痕。
- 不做跨币种求和。
- 不自动释放未知态的预留。
- 不改历史分录——只追加 ADJUST。
- 记账永不使用 float / numeric 参与金额运算。

## 11. 实现偏差与契约缺口登记（实施轮）

本轮落地的是**金额内核**：§1 的数据模型与不变量、§2 的命令入口、§3 的状态机、
§4 的 Saga 补偿、§5 的并发与锁、§6 的事件发布、§9 第 3 条只读重建。
**没有**落地 §5.3 第 1 层的单写主选主、§6.4 的对账流程、§9 其余每日作业、
§4.3 的告警与 runbook。这些方法有的写成了可调用函数（过期清扫、只读重建、
对账队列查询），但没有 worker 进程与调度。

下列条目是**本轮没自行裁决或必须偏离原文**的地方，按 AGENTS.md 的
「契约与文档冲突 → 停下，报告冲突」登记，不自行改契约。

- **C23-8 类型映射。** 方案 §1 是 Postgres 写法，本仓库唯一可跑的存储是 SQLite。
  `bigint` / `numeric` 一律落成 `INTEGER`（SQLite 的 INTEGER 是 64 位有符号），
  时间戳落成定宽 27 字符的 UTC 文本 —— 定宽是为了让字典序等于时间序，
  `isoformat()` 在微秒为 0 时会省掉小数段，那样比较会出错。金额全程整数微单位，
  从不出现 float。
- **C23-9 只追加只能靠触发器。** Postgres 用「收回 UPDATE/DELETE 权限 + 触发器」
  双保险；SQLite 没有 REVOKE，本轮只有 BEFORE UPDATE / BEFORE DELETE 两个触发器。
  也就是说库的所有者仍能改历史分录，append-only 是应用层承诺而非存储层强制。
  上真库时必须补回权限那一半。
- **C23-10 托管约束改写。** §1.4 原式是「子已消耗 ≤ 托管余额」。SQLite 的表级
  CHECK 读不到本行的生成列，所以展开成「子已消耗 + 已退回 ≤ 已发放」——
  与生成列的表达式等价，且不引用生成列。
- **C23-11 终态计提必须有凭证基础。** §2 只有「暂估阶段不得带发票号」一条约束。
  本轮加严为：标成终态时基础只能是发票或已对账，不能用估算值冒充发票。
  否则「终态」这个词就失去意义，对账队列也永远清不完。这是本服务自己的推断，
  需要 owner 确认。
- **C23-12 幂等用平台 SDK。** §2.7 的幂等记录表未建，改用平台幂等存储：同租户、
  同命令号、同请求哈希重放，同键异体 409。语义一致，落库形态不同。与 S24 的
  C24-10 是同一处偏离。
- **C23-13 发件箱不在这里建表。** 平台事件 SDK 自己拥有发件箱并保证
  「业务事实与事件同事务」。本服务的 schema 不重复声明，否则会有两份发件箱。
- **C23-14 单写主未实现。** §5.3 第 1 层要求每租户一个写主（租约选举）。
  本内核假设单写，靠第二层的行锁与相对增量保证正确；多副本同时写同一租户
  时的正确性**未验证**，部署形态必须先满足单写。
- **C23-15 事件信封字段名不符。** 平台 SDK 发 `sequence`，已发布的信封契约要
  `aggregate_sequence`；SDK 的 `causation_id` 缺省写空，契约要求最短一字符。
  本服务内不规避平台缺口，只保证事件 `data` 部分能过本事件自己的 schema，
  并把 `causation_id` 退回到分录号。同一问题在 S09 登记为 C09-9、S24 登记为 C24-9。
- **C23-16 行工厂必须是 `sqlite3.Row`。** 幂等 SDK 与本内核共用一条连接，SDK 用
  `stored_hash, result = row` 按下标解包。若本服务把连接的行工厂换成 dict 工厂，
  解包出来的是**列名**，SDK 就会拿 "request_hash" 当摘要去比对，于是同键同 body
  也被判成「键被复用」。这是跨组件的真实缺陷，不是本服务内部问题，已加回归用例。
- **C23-17 预留不拒已过期的报价。** 验收表要求先建出一条会过期的预留，再让它被
  清扫器收走（T23-16）或被授权拒掉（T23-18）。若在预留时就拒，这两个用例无从
  构造。过期闸门只有两处：授权时返回报价过期，清扫器把未授权的预留置为过期。
- **C23-18 汇率快照引用是提示，不是必填。** 报价币种与预算币种不同时必须换算，
  但「用哪一份快照」是可选提示。缺它属于「此刻没有可用汇率」，按 409 返回，
  不是 422 参数错误 —— 后者会让调用方以为是自己填错而反复重试同一份请求。
- **C23-19 事件粒度是业务动作，不是分录。** 结算在账本上必须写两条分录
  （单轴约束禁止一条分录同时动两个维度），但只发一个事件，`data` 里两个增量
  都带；`aggregate_sequence` 取该动作**最后一条**分录的序号。因此同一聚合的
  事件序号允许有洞，这是设计而非缺陷。
- **C23-20 超支不写分录。** 结算仍成功：已消耗增加、在途减少。若此后该预算超出
  额度，状态转耗尽，并追加一条 reason_code 为报价低估的调整行。调整行**不是**
  分录 —— 额度本身没有变化，变的是「实际花的比报价多」这一事实。超支不向上传播。
- **C23-21 可用额允许为负，因此没有 CHECK。** docs/12 的公式与方案 §1.3 都允许
  超支结算后可用额为负；给这一列加非负约束会让正确的超支路径写不进去。真正的
  闸门是预留与托管发放事务内的前置检查，不是列约束。这是有意的取舍，
  不是漏写。
- **C23-22 事务必须显式开。** `sqlite3` 在自动提交模式下，进入连接的上下文管理器
  只是 try/except/commit，而每条语句已经各自提交了 —— 它**不**开启事务。所以内核
  里所有写路径都发显式的立即事务。S01 / S08 / S09 里用连接上下文当事务的地方
  也有同样的隐患，属于既有问题，本轮只在 S23 修正。

新增未决（承接 §8 与文末清单）：

- C23-11 的加严是否采纳为契约约束，需 owner 确认。
- C23-9 在真库上的权限收口方案未定。
- C23-14 的单写主选主与租约表结构未定。
