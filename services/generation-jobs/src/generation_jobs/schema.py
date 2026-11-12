"""S26 自己的 schema。别的服务不 import 这里，只透过 HTTP 契约与事件交互。

与 plans/S26-generation-jobs.md §2.1 / §2.3 / §2.4 / §7.3 / §11 的 DDL 一一对应，
Postgres 写法换 SQLite（同 S21 的规则）：

- `timestamptz` → `TEXT`（固定宽度 27 字符 ISO-8601 UTC，见 service 的 `_ts`）
- `jsonb` → `TEXT`（JCS 文本）
- `bigint` / `integer` → `INTEGER`（SQLite 的 INTEGER 是 64 位）
- `GENERATED ALWAYS AS IDENTITY` → `INTEGER PRIMARY KEY AUTOINCREMENT`（本服务不需要，
  outbox 由平台 SDK 建）

**服务自己建的表**：`generation_attempt` / `provider_job` / `submission_intent` /
`dispatch_grant` / `job_transition` / `reconciliation_attempt` / `resubmission_approval` /
`attempt_ledger`。

**平台 SDK 建的表**：`outbox` / `inbox` / `idempotency_record`（C26-1）。
方案 §11 又手写了一份 `outbox` DDL，与 `event_sdk` 的真实列名不同（`aggregate_seq` vs
`sequence`、`partition_key`/`headers` 只在这份草稿里存在）。以 SDK 为准，这里不重复声明。

## 与方案的偏离（都登记为 C26-*）

- **`provider_job.state` 不加 CHECK**：§13 的失败表把 `ACCOUNT_UNAUTHORIZED` /
  `CAPABILITY_DRIFT` / `ROUTE_INVALIDATED` / `DISCARD_ON_COMPLETION` 写在"状态"列里，
  但它们都不在 §3.1 的 16 个状态中（PGJ-1）。加了 CHECK 会让这些列变成非法数据；
  不加 CHECK 又让数据库拦不住它们。折中：**状态白名单只在应用层**（`service.TRANSITIONS`），
  且 §13 的四个名字**一个都不写进 `provider_job.state`**——它们分别落成
  `last_error_code` 或 `disposition`。见 C26-2。
- **`submission_intent.state` 补全枚举**（§16 明说留给实现）：见下方 CHECK。
- **`attempt_ledger` 加 `max_micro`**：§11 的 DDL 只有 `micro_spent`，但 §12.1 第 6 条要
  「未超节点上限」——上限存在哪没写。放本表，默认 NULL = 不限。见 C26-3。
- **`dispatch_grant` 加 `submit_payload_ref` / `channel_route` / `deadline`**：§12.4 的
  `:claim` 响应要返回这三个字段，§11 的 grant DDL 却只存了 `stable_client_key`。见 C26-4。
- **`provider_job` 加三列**：`output_snapshot`（§8.1 要 provider_url 与
  provider_url_expires_at，§8.3 的五步全部依赖它；不落库就没法重归档）、
  `last_observed_at`（§12.2 的查询体明文要求）、`saga_attempts`（§4 的「重试耗尽」
  需要一个计数，§11 没有；`network_retry` 是传输层计数、`submit_attempts` 是付费
  风险计数，都不能拿来当 Saga 重试数，PGJ-2）。见 C26-5。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §2.1 业务层：一次"我想生成这个镜头"的决定。
-- 输入不变 → 同一 attempt；输入变了 → 新 attempt（§2.2）。
CREATE TABLE IF NOT EXISTS generation_attempt (
  attempt_id        TEXT NOT NULL,
  tenant_id         TEXT NOT NULL,
  project_id        TEXT NOT NULL,
  series_id         TEXT NOT NULL,
  episode_id        TEXT NOT NULL,
  branch_id         TEXT NOT NULL,
  node_id           TEXT NOT NULL,
  node_epoch        INTEGER NOT NULL,
  execution_fence   INTEGER NOT NULL,
  modality          TEXT NOT NULL,
  attempt_no        INTEGER NOT NULL,
  variation_nonce   TEXT NOT NULL,
  idempotency_key   TEXT NOT NULL,
  route_decision_id TEXT NOT NULL,
  requirement_set   TEXT NOT NULL DEFAULT '{}',
  -- Saga 从 `RESERVING` 恢复时要重放 S23/S22 的同一幂等键（§4 步骤 1 末段、
  -- 步骤 3「意图落库之前崩溃 → 从 `RESERVING` 重新查」）。重放需要 `budget_scope_id`
  -- 与 `deadline`，而它们只在 `GenerationCommand` 里；命令本体不落库（含 prompt），
  -- 所以把 Saga 必需的这两个标量单列存下来。缺了它们，崩溃后只能凭猜重建请求。
  budget_scope_id   TEXT NOT NULL,
  deadline          TEXT NOT NULL,
  max_business_attempts INTEGER NOT NULL,
  state             TEXT NOT NULL,
  -- admission-record.v2 的 `context` 是 `$ref: execution-context.v2.schema.json`，
  -- 18 个必填字段一个都不能少，而 `GET /admissions/{id}` 必须原样回吐。
  -- `context` 是 S21/S07 签名的租户上下文（AGENTS.md 红线：tenant_id 不可由
  -- LLM 提供），存原文而不是拆列——拆开再拼回去就是亲手重造一遍签名对象。
  context           TEXT NOT NULL,
  -- `GET /admissions` 的 `request_hash` 原样回吐 context 里那一个：
  -- `contracts/examples/admission-unknown.json` 两个字段是同一个值。
  -- S26 自己算的载荷 hash 不入本列，它在 `submission_intent.request_hash`
  -- （绑定 grant 用的那个）。两把尺子分开存，别用一个冒充另一个。
  request_hash      TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  closed_at         TEXT,

  PRIMARY KEY (tenant_id, attempt_id),
  CONSTRAINT ga_attempt_no_positive CHECK (attempt_no >= 1),
  CONSTRAINT ga_epoch_positive CHECK (node_epoch >= 1),
  CONSTRAINT ga_fence_positive CHECK (execution_fence >= 1),
  CONSTRAINT ga_max_attempts_positive CHECK (max_business_attempts >= 1),
  CONSTRAINT ga_state_closed CHECK (state IN
    ('OPEN','SUCCEEDED','FAILED','EXHAUSTED','ABANDONED')),
  CONSTRAINT ga_closed_has_ts CHECK ((state <> 'OPEN') = (closed_at IS NOT NULL))
);
-- §2.1 的两条 UNIQUE，带上 tenant 前缀（Postgres 版第一条已带）。
CREATE UNIQUE INDEX IF NOT EXISTS ga_idem_uniq
  ON generation_attempt (tenant_id, idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS ga_variation_uniq
  ON generation_attempt (tenant_id, node_id, node_epoch, attempt_no, variation_nonce);
CREATE INDEX IF NOT EXISTS ga_by_node_idx
  ON generation_attempt (tenant_id, node_id, node_epoch, attempt_no DESC);

-- §2.3 执行层：一次对某个供应商渠道的派单。
--
-- 计数器语义（§2.3 末段 + PGJ-2）：
--   submit_attempts  真正发出过 submit 请求的次数 —— **付费风险计数**，每题只增不减
--   network_retry    传输层重试（含只读 get_status）—— 不产生费用
-- 两者之间没有等式关系，别拿一个推另一个。
CREATE TABLE IF NOT EXISTS provider_job (
  provider_job_id   TEXT NOT NULL,
  attempt_id        TEXT NOT NULL,
  tenant_id         TEXT NOT NULL,
  job_no            INTEGER NOT NULL,
  state             TEXT NOT NULL,
  provider          TEXT NOT NULL,
  channel           TEXT NOT NULL,
  model_id          TEXT NOT NULL,
  model_snapshot    TEXT NOT NULL,
  region            TEXT NOT NULL,
  account_pool_id   TEXT NOT NULL,
  requirement_hash  TEXT NOT NULL,
  submission_nonce  TEXT NOT NULL,
  provider_idempotency TEXT NOT NULL,
  provider_job_ref  TEXT,
  business_attempt  INTEGER NOT NULL,
  network_retry     INTEGER NOT NULL DEFAULT 0,
  submit_attempts   INTEGER NOT NULL DEFAULT 0,
  -- §9.2 / §13：不可取消的任务继续跑完，成片归档后不进候选。
  disposition       TEXT,
  last_error_code   TEXT,
  last_error_at     TEXT,
  -- §12.2 查询体要 `last_observation_at`；§14 的 `gj_poll_watermark_lag_seconds`
  -- 也要拿它跟 provider 状态算差值。
  last_observed_at  TEXT,
  -- §8.1 归档载荷里的 provider_url / provider_url_expires_at / expected_sha256
  -- 全部来自一次成功观测的 `output`。§8.3 的五步补救全部要先读回它，
  -- 所以必须落库——否则重启之后连"URL 什么时候过期"都答不上来。
  output_snapshot   TEXT,
  -- §12.2 查询体的 `output.artifact_refs`，也是 §8.2 重归档时要找的那份清单。
  -- 归档成功的 id 只写在 S28 与 `artifact.ingested` 事件里，S26 必须自己记一份
  -- 引用，否则"这个 job 产出了哪些成片"永远答不上来。
  artifact_refs     TEXT,
  -- §4 的「重试耗尽」要一个 Saga 重试计数。PGJ-2 明说两个计数器之间没有等式，
  -- 也不能拿它们兼任 Saga 步数，所以单开一列（C26-5）。
  saga_attempts     INTEGER NOT NULL DEFAULT 0,
  next_action_at    TEXT,
  poll_watermark    TEXT,
  aggregate_sequence INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  closed_at         TEXT,

  PRIMARY KEY (tenant_id, provider_job_id),
  CONSTRAINT pj_attempt_fk FOREIGN KEY (tenant_id, attempt_id)
    REFERENCES generation_attempt (tenant_id, attempt_id),
  CONSTRAINT pj_job_no_positive CHECK (job_no >= 1),
  CONSTRAINT pj_business_attempt_positive CHECK (business_attempt >= 1),
  CONSTRAINT pj_counters_nonneg CHECK (network_retry >= 0 AND submit_attempts >= 0
                                       AND saga_attempts >= 0),
  CONSTRAINT pj_seq_positive CHECK (aggregate_sequence >= 1),
  CONSTRAINT pj_idem_closed CHECK (provider_idempotency IN
    ('supported','client_lookup_only','unsupported')),
  CONSTRAINT pj_disposition_closed CHECK (disposition IS NULL OR disposition IN
    ('DISCARD_ON_COMPLETION')),
  -- 有 provider_job_ref 就不该是 UNKNOWN（§3.1 第 17 行）。这是数据库能证明的那一半：
  -- UNKNOWN 且已有 ref 的行不该存在。
  CONSTRAINT pj_unknown_has_no_ref CHECK (
    NOT (state IN ('SUBMISSION_UNKNOWN','RECONCILING') AND provider_job_ref IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS pj_job_no_uniq
  ON provider_job (tenant_id, attempt_id, job_no);
CREATE INDEX IF NOT EXISTS pj_due_idx ON provider_job (tenant_id, next_action_at)
  WHERE state NOT IN ('ARCHIVED','PROVIDER_FAILED','CANCELED');
CREATE INDEX IF NOT EXISTS pj_unknown_idx ON provider_job (tenant_id, state, updated_at)
  WHERE state IN ('SUBMISSION_UNKNOWN','RECONCILING');
CREATE INDEX IF NOT EXISTS pj_by_ref_idx ON provider_job (tenant_id, provider_job_ref)
  WHERE provider_job_ref IS NOT NULL;

-- §2.4 花钱前的最后一道持久化。
-- 持有的外部承诺都是**引用**，不是权威：金额与 permit 状态以 S23/S22 为准。
CREATE TABLE IF NOT EXISTS submission_intent (
  intent_id             TEXT NOT NULL,
  provider_job_id       TEXT NOT NULL,
  tenant_id             TEXT NOT NULL,
  request_hash          TEXT NOT NULL,
  route_decision_id     TEXT NOT NULL,
  budget_reservation_id TEXT NOT NULL,
  budget_amount_micro   INTEGER NOT NULL,
  budget_currency       TEXT NOT NULL,
  budget_authorized_at  TEXT NOT NULL,
  quota_permit_id       TEXT NOT NULL,
  quota_pool_id         TEXT NOT NULL,
  quota_state           TEXT NOT NULL,
  dispatch_id           TEXT NOT NULL,
  fencing_token         INTEGER NOT NULL,
  stable_client_key     TEXT NOT NULL,
  capability_bundle_rev INTEGER NOT NULL,
  price_snapshot_id     TEXT NOT NULL,
  node_epoch            INTEGER NOT NULL,
  execution_fence       INTEGER NOT NULL,
  state                 TEXT NOT NULL,
  -- §8.1：归档的 command_id 含 artifact_seq；重归档要换新 command_id 但 job 不变。
  artifact_seq          INTEGER NOT NULL DEFAULT 0,
  submit_payload_ref    TEXT,
  created_at            TEXT NOT NULL,
  submitted_at          TEXT,
  resolved_at           TEXT,

  PRIMARY KEY (tenant_id, intent_id),
  CONSTRAINT si_job_fk FOREIGN KEY (tenant_id, provider_job_id)
    REFERENCES provider_job (tenant_id, provider_job_id),
  CONSTRAINT si_job_uniq UNIQUE (tenant_id, provider_job_id),
  CONSTRAINT si_dispatch_uniq UNIQUE (tenant_id, dispatch_id),
  CONSTRAINT si_amount_positive CHECK (budget_amount_micro > 0),
  CONSTRAINT si_fence_positive CHECK (fencing_token >= 1),
  CONSTRAINT si_epoch_positive CHECK (node_epoch >= 1),
  CONSTRAINT si_exec_fence_positive CHECK (execution_fence >= 1),
  CONSTRAINT si_quota_state_closed CHECK (quota_state IN
    ('RESERVED','DISPATCH_AUTHORIZED','INFLIGHT','RELEASED')),
  CONSTRAINT si_state_closed CHECK (state IN
    ('RESERVED','CLAIMED','SUBMITTED','UNKNOWN','RESUBMIT_APPROVED',
     'SETTLING','SETTLED','ABANDONED'))
);
CREATE UNIQUE INDEX IF NOT EXISTS si_sik_uniq
  ON submission_intent (tenant_id, stable_client_key);

-- §11 dispatch grant。`ISSUED → CLAIMED → (REVOKED)`。
-- CLAIMED 之后永不撤销（撤销没有意义，请求可能已经在路上）。
CREATE TABLE IF NOT EXISTS dispatch_grant (
  dispatch_id       TEXT NOT NULL,
  provider_job_id   TEXT NOT NULL,
  tenant_id         TEXT NOT NULL,
  request_hash      TEXT NOT NULL,
  fencing_token     INTEGER NOT NULL,
  stable_client_key TEXT NOT NULL,
  state             TEXT NOT NULL,
  provider_idempotency TEXT NOT NULL,
  channel_route     TEXT NOT NULL,
  submit_payload_ref TEXT NOT NULL,
  deadline          TEXT NOT NULL,
  issued_at         TEXT NOT NULL,
  expires_at        TEXT NOT NULL,
  claimed_at        TEXT,
  claimed_by        TEXT,

  PRIMARY KEY (tenant_id, dispatch_id),
  CONSTRAINT dg_job_fk FOREIGN KEY (tenant_id, provider_job_id)
    REFERENCES provider_job (tenant_id, provider_job_id),
  CONSTRAINT dg_state_closed CHECK (state IN ('ISSUED','CLAIMED','REVOKED')),
  CONSTRAINT dg_claimed_has_ts CHECK (state <> 'CLAIMED' OR claimed_at IS NOT NULL),
  CONSTRAINT dg_fence_positive CHECK (fencing_token >= 1)
);
-- 一个 job 同时只能有**一张活的** grant（`ISSUED` 或 `CLAIMED`）。§10.6 的
-- "过期撤销 → 回 ADMITTED → 重签"靠这一条保证真的只有一张：撤销后旧行变
-- `REVOKED`，新行才是 `ISSUED`。只锁 `CLAIMED` 会放过"两张 ISSUED 同时在飞"。
CREATE UNIQUE INDEX IF NOT EXISTS dg_live_uniq
  ON dispatch_grant (tenant_id, provider_job_id)
  WHERE state IN ('ISSUED', 'CLAIMED');
CREATE INDEX IF NOT EXISTS dg_expiring_idx ON dispatch_grant (tenant_id, expires_at)
  WHERE state = 'ISSUED';

-- §11 状态转移审计，不可变。
CREATE TABLE IF NOT EXISTS job_transition (
  tenant_id       TEXT NOT NULL,
  provider_job_id TEXT NOT NULL,
  seq             INTEGER NOT NULL,
  from_state      TEXT NOT NULL,
  to_state        TEXT NOT NULL,
  reason_code     TEXT,
  actor           TEXT NOT NULL,
  occurred_at     TEXT NOT NULL,

  PRIMARY KEY (tenant_id, provider_job_id, seq)
);
CREATE INDEX IF NOT EXISTS jt_recent_idx
  ON job_transition (tenant_id, provider_job_id, seq DESC);

-- §7.3 每轮对账一条，证据链。
CREATE TABLE IF NOT EXISTS reconciliation_attempt (
  recon_id        TEXT NOT NULL,
  provider_job_id TEXT NOT NULL,
  tenant_id       TEXT NOT NULL,
  round_no        INTEGER NOT NULL,
  method          TEXT NOT NULL,
  outcome         TEXT NOT NULL,
  evidence_ref    TEXT,
  attempted_at    TEXT NOT NULL,

  PRIMARY KEY (tenant_id, recon_id),
  CONSTRAINT ra_job_fk FOREIGN KEY (tenant_id, provider_job_id)
    REFERENCES provider_job (tenant_id, provider_job_id),
  CONSTRAINT ra_round_positive CHECK (round_no >= 1),
  CONSTRAINT ra_method_closed CHECK (method IN
    ('list_by_client_key','list_recent','query_by_metadata',
     'manual_provider_console','ledger_snapshot','bill_reconcile')),
  CONSTRAINT ra_outcome_closed CHECK (outcome IN
    ('found','not_found','inconclusive','unsupported','error'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ra_round_uniq
  ON reconciliation_attempt (tenant_id, provider_job_id, round_no, method);

-- §7.3B：B 档的 not_found 不能自动重发，要一条人工/策略批准。
-- 默认 false，所以这张表大多数时候是空的——这正是它存在的意义。
CREATE TABLE IF NOT EXISTS resubmission_approval (
  approval_id     TEXT NOT NULL,
  provider_job_id TEXT NOT NULL,
  tenant_id       TEXT NOT NULL,
  reason          TEXT NOT NULL,
  approved_by     TEXT NOT NULL,
  evidence_ref    TEXT NOT NULL,
  created_at      TEXT NOT NULL,

  PRIMARY KEY (tenant_id, approval_id),
  CONSTRAINT rsa_job_fk FOREIGN KEY (tenant_id, provider_job_id)
    REFERENCES provider_job (tenant_id, provider_job_id)
);

-- §11 业务 attempt 上限与费用累计。按 (tenant, node) 累计，**跨 Run 不重置**（E26）。
CREATE TABLE IF NOT EXISTS attempt_ledger (
  tenant_id     TEXT NOT NULL,
  node_id       TEXT NOT NULL,
  attempts_used INTEGER NOT NULL DEFAULT 0,
  micro_spent   INTEGER NOT NULL DEFAULT 0,
  -- §12.1 第 6 条要"未超节点上限"，§11 没给上限列（C26-3）。
  -- NULL = 不限；由策略/合同填。
  max_micro     INTEGER,
  max_attempts  INTEGER,
  updated_at    TEXT NOT NULL,

  PRIMARY KEY (tenant_id, node_id),
  CONSTRAINT al_nonneg CHECK (attempts_used >= 0 AND micro_spent >= 0)
);
"""


def schema_sql() -> str:
    return SCHEMA
