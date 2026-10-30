# S22 · 调度与供应商配额 —— 实现方案

状态：实现方案（W1）。`maturity: design_only` 不变。

上游依据：[docs/03 §3](../docs/03-deployment-capacity.md)、[docs/04 §3](../docs/04-data-consistency.md)、[docs/09 §4](../docs/09-model-execution.md)、[docs/12 §2/§4](../docs/12-budget-scheduling.md)、[S22 规格](../docs/design-package/services/S22-scheduler-quota.md)。

配套：[S23 预算与成本账本](S23-budget-ledger.md)、[S08 版本快照与接受状态](S08-revision-snapshot.md)。

---

## 0. 边界与一句话职责

S22 是**配额许可**的唯一写者：`QuotaPool` 上限、`Permit` 生命周期、账号/区域在途槽、租户公平队列、执行租约。

S22 **不**拥有：

| 事实 | 归属 |
|---|---|
| 钱：预留、授权、实际费用 | S23 |
| 供应商能力、单价、生命周期、账户池元数据 | S24 |
| 任务状态、`SUBMISSION_UNKNOWN` | S26 |
| 供应商侧观测（回调、查询结果） | S27 |
| 当前生产版本 | S08 |

关键分界：**S22 管"能不能同时打这个账号"，S23 管"还有没有钱"。** 两者都不看对方的库；交叉判断只发生在一个地方——S26 的准入 Saga。S22 收到的 `reservation_id` 是**不透明引用**，S22 从不校验 S23 的余额。

判据：任何"这个账号现在还有几个在途槽"的疑问，答案只能来自 S22。

### 0.1 池隔离不等于可以降级（E18 的硬线）

供应商返回 429 / 5xx / 账号被撤销 / 模型退休时，本服务的**唯一动作是"不授予"**，绝不"换个池凑合"：

- 请求带 `route_decision_ref`，S25 已经把 `provider_id` / `channel_id` / `region` / `modality` 定死了。S22 只按这个路由找池，**找不到或池不可用就返回 `422 POOL_NOT_FOUND_FOR_ROUTE` / `503 POOL_SUSPENDED`**，不自行改路由。
- 不使用更便宜的模型、不使用更低画质的通道、不跨区域借账号（BYOK 池跨租户更是 404，见 T22-42）。这三条都是"硬能力降级"，docs/03 明令禁止。
- 若 S25 要换路由，那是 S25 重新决策并给出新的 `route_decision_ref`，S22 只认新的引用。**降级决策在 S25，不在 S22**——S22 一旦开始自己挑替代池，就成了第二个路由权威。
- 429 是**正常调度结果**（`retryable:true`），不是错误；S26 按 `retry_after` / `estimated_wait_ms` 有界退避。5xx 与账号撤销走 `:suspend`，池进 `SUSPENDED`，在途 permit 不动（§3.3 与 E22 同构）。

---

## 1. 数据模型

数据库 `scheduler_quota`，Postgres（**权威在 PG，Redis 只是派生缓存**）。整数计数，无浮点。

### 1.1 `quota_pool` —— 分片键 provider+account+region+modality

```sql
CREATE TABLE quota_pool (
  pool_id              text PRIMARY KEY,          -- 稳定 ID，S26/S23 只引用它
  tenant_scope         text NOT NULL,             -- 'shared' | tenant_id（BYOK 专用池）
  provider_id          text NOT NULL,             -- 'volcengine' / 'kling' / ...
  channel_id           text NOT NULL,             -- 'seedance_volcengine' / 'seedance_byteplus' / ...
  account_ref          text NOT NULL,             -- 指向 S24 ProviderAccountPool 的 id
  region               text NOT NULL,
  modality             text NOT NULL
    CHECK (modality IN ('video','image','speech','text','understanding',
                        'lipsync','sound_effect','music','training')),
  resource_class       text NOT NULL
    CHECK (resource_class IN ('api_request','api_token','gpu_second','cpu_media_second')),

  -- 三个独立维度，绝不合并
  rpm_limit            integer,                   -- NULL = 未设
  rpm_remaining_window integer NOT NULL DEFAULT 0,
  window_reset_at      timestamptz,
  tpm_limit            integer,
  tpm_remaining_window bigint  NOT NULL DEFAULT 0,
  inflight_limit       integer NOT NULL,          -- 在途并发，与 RPM 无关
  inflight_used        integer NOT NULL DEFAULT 0,

  -- GPU / CPU 池用资源秒，不用请求数
  resource_seconds_limit   numeric(20,3),
  resource_seconds_used    numeric(20,3) NOT NULL DEFAULT 0,

  state                text NOT NULL DEFAULT 'ACTIVE'
    CHECK (state IN ('ACTIVE','DRAINING','SUSPENDED','DISABLED')),
  suspend_reason       text,                      -- ACCOUNT_REVOKED / QUOTA_DRIFT / MANUAL
  revision             bigint NOT NULL DEFAULT 1, -- 池定义版本（S24 变更时 +1）
  updated_at           timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT qp_inflight_bounds CHECK (inflight_used BETWEEN 0 AND inflight_limit),
  CONSTRAINT qp_no_negative_rpm CHECK (rpm_remaining_window >= 0),
  CONSTRAINT qp_no_negative_tpm CHECK (tpm_remaining_window >= 0),
  CONSTRAINT qp_shared_needs_account CHECK (tenant_scope = 'shared' OR account_ref IS NOT NULL)
);
CREATE INDEX qp_route_idx ON quota_pool (provider_id, channel_id, region, modality, state);
CREATE INDEX qp_account_idx ON quota_pool (account_ref, region);

-- 分片键：同一 (provider, account, region) 的**全部** pool 必须落在同一分片，
-- 因为账号级上限是跨模态共享的。
CREATE TABLE pool_shard (
  shard_key   text PRIMARY KEY,     -- provider||'/'||account_ref||'/'||region
  shard_id    smallint NOT NULL,
  account_cap_inflight integer NOT NULL,   -- 账号级总在途上限（跨 modality）
  account_used_inflight integer NOT NULL DEFAULT 0,
  CONSTRAINT as_no_over CHECK (account_used_inflight BETWEEN 0 AND account_cap_inflight)
);
```

**为什么要有 `pool_shard`**：docs/03 说"超大共享账户池可委派有严格上限的子 permit，总额不变"，docs/12 说"账号跨租户共享上限仍严格守恒"。一个供应商账号的总并发是硬限制，不能因为按 modality 切成 9 个 pool 就变成 9 倍。所以存在**两级计数**：

```
账号级：account_used_inflight ≤ account_cap_inflight
池级：  pool.inflight_used    ≤ pool.inflight_limit
约束：  Σ pool.inflight_limit over 同一 shard_key ≤ account_cap_inflight
```

第二行是配置时校验的（池定义变更时校验），第一行是运行时强制的。**permit 领取必须同时在两级上 +1，且顺序固定（§5.3）。**

`resource_class = 'gpu_second' | 'cpu_media_second'` 的池**不参与** `inflight_used` 计数，只走 `resource_seconds_*`；它们也不与 API 池共享 `account_cap_inflight`。docs/12 "CPU媒体和自托管GPU按资源秒/内存另配，不能混成视频API并发"——这里就是那条界线。

### 1.2 `permit` —— 状态机主体

```sql
CREATE TABLE permit (
  permit_id          text PRIMARY KEY,
  tenant_id          text NOT NULL,
  pool_id            text NOT NULL REFERENCES quota_pool(pool_id),
  shard_key          text NOT NULL REFERENCES pool_shard(shard_key),

  -- 不透明交叉引用：S22 从不读 S23
  reservation_ref    text NOT NULL,
  reservation_amount_micro bigint NOT NULL CHECK (reservation_amount_micro >= 0),
  budget_scope_id    text NOT NULL,

  -- 归属
  attempt_id         text NOT NULL,
  run_id             text,
  execution_fence    bigint NOT NULL,
  node_id            text,
  node_epoch         bigint,
  route_decision_ref jsonb NOT NULL,

  state              text NOT NULL
    CHECK (state IN ('RESERVED','DISPATCH_AUTHORIZED','INFLIGHT','RELEASED','EXPIRED')),
  release_reason     text
    CHECK (release_reason IS NULL OR release_reason IN
      ('SETTLED','SUPERSEDED_BY_BUDGET','SUPERSEDED_BY_FENCE',
       'NEVER_DISPATCHED','RECONCILED_ABSENT','OPERATOR')),

  -- 租约（只影响尚未 dispatch 的 permit）
  lease_expires_at   timestamptz,
  dispatched_at      timestamptz,
  released_at        timestamptz,

  -- 与 S27 观测的关联；S22 只存引用，不判定供应商状态
  dispatch_id        text,
  provider_job_ref   text,

  -- 席位：同一请求的重复领取返回同一 permit
  deductible_key     text NOT NULL,   -- tenant_id||attempt_id||pool_id，或外部传入的业务键
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT permit_live_needs_lease
    CHECK (state NOT IN ('RESERVED','DISPATCH_AUTHORIZED') OR lease_expires_at IS NOT NULL),
  CONSTRAINT permit_released_needs_reason
    CHECK (state <> 'RELEASED' OR release_reason IS NOT NULL),
  CONSTRAINT permit_terminal_ts
    CHECK (state <> 'RELEASED' OR released_at IS NOT NULL)
);
CREATE UNIQUE INDEX permit_deduct_uniq
  ON permit (tenant_id, deductible_key) WHERE state <> 'EXPIRED';
CREATE INDEX permit_pool_state_idx ON permit (pool_id, state);
CREATE INDEX permit_inflight_idx ON permit (shard_key, state) WHERE state = 'INFLIGHT';
CREATE INDEX permit_lease_idx ON permit (state, lease_expires_at)
  WHERE state IN ('RESERVED','DISPATCH_AUTHORIZED');
CREATE INDEX permit_attempt_idx ON permit (tenant_id, attempt_id);
CREATE INDEX permit_fence_idx ON permit (tenant_id, run_id, execution_fence);
```

`deductible_key` 唯一索引是"重复领取返回同一 permit"的实现：不是靠应用内存 map，是数据库约束。

### 1.3 `queue_entry` —— 公平队列

```sql
CREATE TABLE queue_entry (
  queue_entry_id   text PRIMARY KEY,
  tenant_id        text NOT NULL,
  pool_id          text NOT NULL REFERENCES quota_pool(pool_id),
  request_ref      text NOT NULL,          -- S26 的 attempt_id
  priority         smallint NOT NULL       -- 0=interactive, 10=batch
    CHECK (priority BETWEEN 0 AND 100),
  enqueued_at      timestamptz NOT NULL DEFAULT now(),
  effective_at     timestamptz NOT NULL,   -- = enqueued_at - aging_bonus，用于排序
  expected_run_seconds integer NOT NULL DEFAULT 0,
  state            text NOT NULL DEFAULT 'WAITING'
    CHECK (state IN ('WAITING','GRANTED','CANCELLED','ABANDONED')),
  granted_permit_id text,
  deadline         timestamptz,
  CONSTRAINT qe_granted_has_permit
    CHECK ((state = 'GRANTED') = (granted_permit_id IS NOT NULL))
);
CREATE INDEX qe_pick_idx ON queue_entry (pool_id, state, effective_at, enqueued_at)
  WHERE state = 'WAITING';

-- 租户份额状态（WFQ 的虚拟时间）
CREATE TABLE tenant_share (
  tenant_id       text NOT NULL,
  pool_id         text NOT NULL,
  weight          integer NOT NULL DEFAULT 1 CHECK (weight > 0),
  virtual_time    numeric(20,6) NOT NULL DEFAULT 0,
  inflight_granted integer NOT NULL DEFAULT 0,
  guaranteed_share integer NOT NULL DEFAULT 0,  -- 企业保底份额
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, pool_id)
);
```

### 1.4 `pool_usage_ledger` —— 计数的可重建来源（Redis 丢失后的重建依据）

```sql
CREATE TABLE pool_usage_ledger (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pool_id        text NOT NULL,
  shard_key      text NOT NULL,
  tenant_id      text NOT NULL,
  delta_inflight smallint NOT NULL,
  delta_rpm      integer  NOT NULL DEFAULT 0,
  delta_tpm      bigint   NOT NULL DEFAULT 0,
  delta_resource_micro integer NOT NULL DEFAULT 0,
  reason         text NOT NULL,   -- ACQUIRE / DISPATCH / RELEASE / EXPIRE / RECONCILE
  permit_id      text,
  at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX pul_pool_idx ON pool_usage_ledger (pool_id, at DESC);
CREATE INDEX pul_permit_idx ON pool_usage_ledger (permit_id) WHERE permit_id IS NOT NULL;
```

这张表**不是**第二权威——它只是 permit 变更的流水。权威仍是 `permit.state` 的聚合。它的用途只有一个：Redis 全丢时，用 `permit` 当前状态重算 `inflight_used`，并用这张表核对重算结果。docs/12 "Redis丢失后保守暂停窗口或用持久请求日志重建"——**两者都实现**，见 §5.5。

### 1.5 幂等、Outbox、Inbox

与 S08 完全同构（同一份 `idempotency_record` / `outbox` / `inbox` DDL，不同 schema），见 S08 §1.6。S22 的 Outbox 约束：

```sql
UNIQUE (aggregate_id, aggregate_sequence, event_type)   -- aggregate_id = permit_id
```

`aggregate_sequence`：每个 permit 一个 1..N 的本地序列（RESERVED=1, DISPATCH_AUTHORIZED=2, INFLIGHT=3, RELEASED=4）。这让下游能按 permit 的 sequence 判断乱序。

---

## 2. 命令与查询 API

同 S08 的传输约定：mTLS + workload identity，`Idempotency-Key` = `command_id`，`request_hash = sha256(JCS(body − command_id − request_hash))`。

### 2.1 `POST /permits:acquire`

准入 Saga 第 2 步。S26 在 S23 预留成功后调用。

```jsonc
{
  "command_id": "cmd_...",
  "reservation_ref":       "res_01H...",        // 不透明
  "reservation_amount_micro": 900000,
  "budget_scope_id":       "budget_ep3",
  "attempt_id":            "attempt_01H...",
  "run_id":                "run_...",
  "execution_fence":       2,
  "node_id":               "shot_3_2.video",
  "node_epoch":            4,
  "route_decision_ref":    { "service_id":"S25","id":"...","revision":1,"content_hash":"…" },
  "modality":              "video",
  "requested_pool_id":     "pool_seedance_volc_a1",   // 可空：空则由调度器选池
  "deductible_key":        "attempt_01H...",          // 可空，默认 attempt_id
  "priority":              10,
  "expected_run_seconds":  240,
  "deadline":              "2026-10-30T10:00:00Z",
  "lease_ttl_seconds":     120
}
```

两种模式，由 `wait` 决定：

```jsonc
// 默认：不等待，立即可授予或拒绝
{ "wait": false }

// 有界等待：入队，最多等 N 秒
{ "wait": true, "wait_timeout_seconds": 30 }
```

响应 `200`（授予）：

```jsonc
{
  "permit_id": "permit_01H...",
  "pool_id": "pool_seedance_volc_a1",
  "state": "RESERVED",
  "lease_expires_at": "2026-10-30T09:02:00Z",
  "grant_epoch": 7,                 // = queue 授予序号，用于 §2.3 的 CAS
  "queue_wait_ms": 0,
  "replayed": false
}
```

响应 `429`（不可授予，**这是正常的调度结果，不是错误状态**）：

```jsonc
{
  "code": "NO_CAPACITY",
  "message": "pool at inflight limit",
  "retryable": true,
  "correlation_id": "...",
  "details": {
    "pool_id": "pool_seedance_volc_a1",
    "bottleneck": "inflight",       // inflight | rpm | tpm | account | resource_seconds | drained
    "estimated_wait_ms": 45000,
    "queue_position": 12,
    "queue_entry_id": "qe_...",     // wait=true 时
    "account_saturated": true       // 命中账号级上限
  }
}
```

`estimated_wait_ms` 由 `pool.avg_service_seconds / (inflight_limit) * queue_ahead` 粗算。**这是规划值，不得当作 SLA 承诺给客户**（docs/03 §4：不可实现的 deadline 返回预计窗口与可选范围，并明确是估计）。

### 2.2 `POST /permits/{permit_id}:confirm-dispatch`

准入 Saga 第 4 步的后半：S26 确认资金仍已授权，要求 S22 把 permit 从 `DISPATCH_AUTHORIZED` 推入 `INFLIGHT` 并返回可签发 grant 的凭据。

```jsonc
{
  "command_id": "cmd_...",
  "expected_grant_epoch": 7,
  "dispatch_id": "disp_01H...",
  "reservation_still_authorized": true    // S26 已向 S23 确认的结果，S22 只记录不判定
}
```

响应 `200`：`{ "permit_id":"...", "state":"INFLIGHT", "dispatch_id":"disp_...", "inflight_ack": true }`。

**关键语义**：这个调用**不会**因为 permit 已经过期就失败。`DISPATCH_AUTHORIZED` 是**不可 TTL 回收**的（§3.2）。如果 permit 已经 `RELEASED` 或 `EXPIRED`，返回 409 `PERMIT_NOT_DISPATCHABLE`——此时 S26 必须走补偿，不能强行 dispatch。

### 2.3 `POST /permits/{permit_id}:release`

终态后释放。调用方是 S26（在 S23 结算后）。

```jsonc
{
  "command_id": "cmd_...",
  "expected_grant_epoch": 7,
  "reason": "SETTLED",           // SETTLED | RECONCILED_ABSENT | SUPERSEDED_BY_FENCE | OPERATOR
  "provider_job_ref": "job_xyz",  // 可空
  "terminal_evidence_ref": { "service_id":"S26","id":"...","revision":5,"content_hash":"…" }
}
```

`terminal_evidence_ref` 必填。**没有终态证据不能释放 INFLIGHT permit**——这是 E16/E21 的核心防线：租约到期不等于供应商停了。

响应 `{ "permit_id":"...", "state":"RELEASED", "release_reason":"SETTLED", "inflight_freed": true, "account_freed": true }`。

### 2.4 `POST /permits/{permit_id}:expire`

**只能由 S22 自己的 janitor 调用**（内部端点，外部 principal 拒绝）。把 `RESERVED` 的过期 permit 置 `EXPIRED` 并释放计数。

`DISPATCH_AUTHORIZED` 与 `INFLIGHT` **不允许** expire（返回 409 `PERMIT_NOT_EXPIRABLE`）。

### 2.5 查询

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/quota-pools/{id}` | 池状态：限值、已用、剩余、瓶颈 |
| GET | `/quota-pools?provider=&region=&modality=&state=` | 列表 |
| GET | `/permits/{id}` | 单 permit |
| GET | `/permits?attempt_id=&state=&run_id=` | attempt 下的 permit |
| GET | `/queues/{pool_id}?state=WAITING&limit=` | 队列快照（含租户分组） |
| GET | `/tenants/{tenant_id}/share?pool_id=` | 该租户的虚拟时间与份额 |
| GET | `/capacity?provider=&region=` | 汇总容量（给 S47 投影用，**带 watermark**） |

`GET /quota-pools/{id}` 响应：

```jsonc
{
  "pool_id": "...", "state": "ACTIVE", "revision": 3,
  "source_revision": 4412,               // 池计数的本地序列
  "read_at": "2026-10-30T09:00:00Z",
  "rpm":    { "limit": 600, "remaining": 143, "window_reset_at": "..." },
  "tpm":    { "limit": null, "remaining": null },
  "inflight": { "limit": 40, "used": 37, "reserved_not_dispatched": 2 },
  "account":  { "cap_inflight": 60, "used_inflight": 52 },
  "resource_seconds": { "limit": null, "used": null },
  "oldest_wait_ms": 12000,
  "avg_service_seconds": 180.0
}
```

`inflight.used` 与 `reserved_not_dispatched` **分开报**。docs/12"提交前普通租约可到期补偿，已经授权/提交或未知任务必须对账确认终态再释放"——运维必须能一眼看出"占着槽但还没发出去"的量。

### 2.6 内部运维端点

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/pools/{id}:suspend` | 隔离（账号撤销 / 配额漂移） |
| POST | `/pools/{id}:resume` | 恢复，**需 S02 权限** |
| POST | `/reconcile` | 手工触发一次对账 |
| POST | `/cache:rebuild` | 从 PG 重建 Redis 计数（§5.5） |

### 2.7 错误码

| HTTP | code | retryable | 场景 |
|---|---|---|---|
| 400 | `MALFORMED_REQUEST` | false | schema / JCS |
| 403 | `TENANT_SCOPE_DENIED` | false | 租户不一致，或外部 principal 调内部端点 |
| 404 | `NOT_FOUND` | false | **含跨租户**：别的租户的 permit/pool 一律 404 |
| 409 | `PERMIT_NOT_DISPATCHABLE` | false | `confirm-dispatch` 时 permit 已 RELEASED/EXPIRED |
| 409 | `PERMIT_NOT_EXPIRABLE` | false | 对 `DISPATCH_AUTHORIZED`/`INFLIGHT` 调 expire |
| 409 | `GRANT_EPOCH_CONFLICT` | true | `expected_grant_epoch` 不匹配（另一个执行器已领取） |
| 409 | `PERMIT_ALREADY_RELEASED` | false | 重复 release，**返回首次结果**（幂等） |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同键不同 body |
| 409 | `PERMIT_TERMINAL` | false | 对终态 permit 做任何状态推进 |
| 422 | `POOL_NOT_FOUND_FOR_ROUTE` | false | 路由决策指向的池不存在/已 DISABLED |
| 422 | `POOL_MODALITY_MISMATCH` | false | 请求 modality 与池 modality 不符 |
| 422 | `FENCE_SUPERSEDED` | false | `execution_fence` 已被更新的 fence 取代 |
| 422 | `INVALID_RESERVATION_REF` | false | `reservation_ref` 为空或金额为负 |
| 422 | `DEDUCTIBLE_KEY_CONFLICT` | false | 同 `deductible_key` 但 `attempt_id` / `pool_id` 不同 |
| 429 | `NO_CAPACITY` | true | 见 §2.1，`details.bottleneck` 必填 |
| 429 | `QUEUE_TIMEOUT` | true | `wait=true` 超时仍未授予 |
| 503 | `POOL_SUSPENDED` | true | 池被隔离，`details.suspend_reason` |
| 503 | `CACHE_DEGRADED` | true | Redis 丢失后的保守窗口内（§5.5） |
| 503 | `WRITER_EPOCH_STALE` | true | 旧写主 |

---

## 3. 状态机

### 3.1 Permit 主状态机

```
  (无)
   │ acquire（占 inflight 槽 + 账号槽）
   ▼
 RESERVED ──租约到期 + janitor──► EXPIRED        （释放两个槽）
   │
   │ confirm-dispatch（S26 已确认 S23 资金仍授权）
   ▼
 DISPATCH_AUTHORIZED ── 不可 TTL 回收 ──┐
   │                                    │
   │ S27 领取 grant 并真正提交           │
   ▼                                    │
 INFLIGHT ──────────────────────────────┘
   │
   │ release（必须带终态证据）
   ▼
 RELEASED
```

| 从 | 到 | 触发 | 允许的调用方 |
|---|---|---|---|
| （无） | RESERVED | `acquire` | S26（内部） |
| RESERVED | DISPATCH_AUTHORIZED | `confirm-dispatch` | S26 |
| RESERVED | EXPIRED | `expire`（租约到期） | S22 janitor |
| DISPATCH_AUTHORIZED | INFLIGHT | `confirm-dispatch` 的第二次推进（同一命令，见下） | S26 |
| DISPATCH_AUTHORIZED | RELEASED | `release` + `SUPERSEDED_BY_BUDGET` / `SUPERSEDED_BY_FENCE` | S26 |
| INFLIGHT | RELEASED | `release` + `SETTLED` / `RECONCILED_ABSENT` | S26 |
| 任意非终态 | RELEASED | `release` + `OPERATOR` | 运维（S02 授权） |

**非法转移一律 409 `PERMIT_TERMINAL` 或 `PERMIT_NOT_DISPATCHABLE`**，且不改变任何计数。

关于 `DISPATCH_AUTHORIZED → INFLIGHT`：`confirm-dispatch` 命令执行两次是合法的（一次返回 `DISPATCH_AUTHORIZED`，一次返回 `INFLIGHT`），因为 S26 在"确认 S22 与确认 S23"之间可能重试。用 `expected_grant_epoch` 保证只有最终那次推进状态；前一次只是幂等确认。

**注意一个反直觉但必要的点**：`DISPATCH_AUTHORIZED` 阶段 permit 已经占着 inflight 槽，即使从未真正提交到供应商。这是对的——它预占的是"即将要用的槽"，防止在 S26 完成两次确认的窗口里被别的任务抢走。

### 3.2 为什么 `DISPATCH_AUTHORIZED` 不可 TTL 回收

docs/12 原文："提交前普通租约可到期补偿，已经授权/提交或未知任务必须对账确认终态再释放。"

一旦 `DISPATCH_AUTHORIZED`，存在两种可能：
1. S27 从未收到 grant → 供应商确实没收到请求 → 安全释放。
2. S27 收到了 grant 但网络超时，请求可能已到供应商 → **未知提交**。

S22 **无法区分**这两种情况，所以要把它当成 2 处理。唯一能确定的是 S26 的对账结果，因此只有 `release` 命令（带 `terminal_evidence_ref`）能走到 `RELEASED`。

这不是保守到"永远泄漏"：`DISPATCH_AUTHORIZED` 超过 `authorized_stale_ttl`(默认 30min) 会**告警**并进入 §4.3 的对账队列，由 S26 驱动 S27 查询供应商后释放。**告警不等于自动释放。**

### 3.3 Pool 状态

```
ACTIVE ──suspend──► SUSPENDED ──resume──► ACTIVE
   │                    │
   │                    └──drain──► DRAINING ──所有 permit 终态──► DISABLED
   └──drain──► DRAINING
```

| 从 | 到 | 触发 |
|---|---|---|
| ACTIVE | SUSPENDED | `:suspend`（账号撤销 401/403、配额漂移、人工） |
| ACTIVE | DRAINING | 池下线流程 |
| SUSPENDED | ACTIVE | `:resume` + S02 授权 + 健康探测通过 |
| DRAINING | DISABLED | 该池所有 permit 终态 且 队列清空 |

- `SUSPENDED`：**拒绝新 acquire**（503 `POOL_SUSPENDED`），**已有 permit 不受影响**。这个区分很关键——账号被撤销时，在途任务若供应商侧已开始，我们必须继续结算（E22 的同构问题）。
- `DRAINING`：拒绝新 acquire，允许已有 permit 走完。用于计划性下线。
- `DISABLED`：终态。

### 3.4 Queue entry 状态

```
WAITING ──授予──► GRANTED
   │                  │
   │ 用户取消          │ 对应 permit 释放
   ▼                  ▼
CANCELLED          (entry 保持 GRANTED，只读)
   │
   │ 超时/请求方消失
   ▼
ABANDONED
```

`WAITING` 超过 `deadline` 或 `queue_ttl`(默认 10min) 转 `ABANDONED`，不占任何计数（WAITING 本来就不占 inflight）。

---

## 4. Saga 与补偿

S22 是准入 Saga 的第 2 步与第 4 步的两个参与者。**S22 从不主动发起补偿**；它只在被调用时正确响应，并靠自己的 janitor 清理 RESERVED。

### 4.1 七步准入中 S22 的位置

| 步 | 动作 | S22 侧 |
|---|---|---|
| 1 | S26 → S23 `reserve` | 不参与 |
| 2 | S26 → S22 `acquire` | 占池槽 + 账号槽，生成 RESERVED permit |
| 3 | S26 落 SubmissionIntent + Outbox | 不参与 |
| 4 | S26 → S23 `confirm_authorization`；S26 → S22 `confirm_dispatch_permit` | RESERVED → DISPATCH_AUTHORIZED → INFLIGHT |
| 5 | S26 → S27 `issue_fenced_dispatch_grant` | 不参与（但 permit 已 INFLIGHT） |
| 6 | S27 → S26 `connector.observation.received` | 不参与 |
| 7 | S26 → S23 `accrue_and_settle`；S26 → S22 `release` | INFLIGHT → RELEASED |

### 4.2 各步失败的补偿路径

| 失败点 | 已发生 | S22 的动作 | 说明 |
|---|---|---|---|
| **步 2 失败**（无容量 / 池隔离） | S23 已预留 | 返回 429/503，**不创建 permit**。S26 调 S23 `release` | E14 场景。S22 侧无补偿需要，因为它什么都没占 |
| **步 2 成功、步 3 失败**（S26 崩溃） | permit RESERVED | 租约到期 → janitor `expire` → 释放槽。S26 恢复后重试 `acquire`：**同 `deductible_key` 若 permit 已 EXPIRED，允许新建** | 见 §5.4 |
| **步 4 S23 确认失败** | permit DISPATCH_AUTHORIZED | S26 调 `release` + `SUPERSEDED_BY_BUDGET` | 资金没了就不能占槽 |
| **步 4 S22 确认超时（S22 已提交但响应丢失）** | permit 可能已 INFLIGHT | S26 重试 `confirm-dispatch`（同 command_id）→ 拿到 `INFLIGHT` 与相同 `dispatch_id` | E15 场景：**不付费提交**，Saga 恢复后明确结果 |
| **步 4 S22 确认超时（S22 未提交）** | permit 仍 DISPATCH_AUTHORIZED | S26 重试 → 正常推进 | 同上，幂等记录决定 |
| **步 5 grant 签发失败** | permit INFLIGHT | **不释放**。S26 仍需对账（可能 S27 收到了 grant） | 保守 |
| **步 6 提交结果未知** | permit INFLIGHT | **不释放**，等 S26 对账。超时告警 | E16 |
| **步 7 release 失败** | permit INFLIGHT | 计数不释放。S26 重试；若最终失败，janitor 告警并进入对账队列 | 泄漏可见但不自动处理 |

### 4.3 未知提交的对账流程（`SUBMISSION_UNKNOWN`）

S22 侧的对账是**被动的**：它不查供应商（那是 S27 的事），它只提供 permit 状态并接收释放。

```
S26 进入 SUBMISSION_UNKNOWN
  │
  ├─ S26 查询 S22: GET /permits?attempt_id=...   
  │    → permit.state = INFLIGHT, dispatch_id = disp_xxx
  │    → S22 返回 inflight_ack_at, dispatched_at, provider_job_ref(若 S27 已回填)
  │
  ├─ S26 驱动 S27 查询供应商（用 dispatch_id / client key）
  │
  ├─ 情况 A: 供应商确认已生成
  │    → S26 归档产物、S23 accrue/settle
  │    → S26 → S22 release(reason=SETTLED, terminal_evidence_ref=...)
  │
  ├─ 情况 B: 供应商确认从未收到
  │    → S23 release 预留
  │    → S26 → S22 release(reason=RECONCILED_ABSENT, terminal_evidence_ref=...)
  │
  └─ 情况 C: 供应商也无法确认（无幂等、无查询能力）
       → permit 永远停在 INFLIGHT，槽位泄漏
       → 超过 unknown_grace(默认 24h) 后，池被标记容量缺口，
         运维人工裁决；S22 记录 operator_release 审计
       → 绝不自动释放（否则超并发）
```

`SUBSESSION_UNKNOWN` 同理：S22 不知道"subsession"这个概念，它只知道 permit。凡是"可能已经打到供应商"的 permit，一律按 `INFLIGHT` 保守占用。

**S22 绝不因为以下理由释放 INFLIGHT permit**：租约到期、时间过去很久、请求方崩溃、队列有新任务在等。只有 `release` 命令（带终态证据）或运维 `OPERATOR` 释放。

### 4.4 与 S23 的补偿边界

一个必须写清楚的规则：**S22 释放 permit 不代表 S23 退款，反之亦然。** 两者的补偿独立：

- S23 结算失败 → permit 仍然 INFLIGHT 直到 S26 明确终态。S22 不知道钱的事。
- S22 池隔离 → S23 的预留不受影响，S26 会在看到 503 后释放 S23。
- 顺序固定：**先 S23 settle/release，后 S22 release**（S26 编排）。反序会导致"槽已释放但钱还挂着"，虽然安全但会掩盖问题。

---

## 5. 并发与锁

### 5.1 分片键

```
shard_key = provider_id || '/' || account_ref || '/' || region
```

理由（docs/03 §3）：同一供应商账号的并发上限是全局硬限制，跨 modality 共享。所有 `(provider, account, region)` 相同的池必须落在同一分片，才能在一处强制 `account_used_inflight`。

跨分片完全并行。分片数由账号池数量决定，通常 < 200，无需哈希。

`pool_shard` 是分片的根；`quota_pool` 和 `permit` 都按 `shard_key` 分区（PG 声明式分区或按 schema 拆分），保证单分片内可单写。

### 5.2 两把 Redis 令牌桶（只做快速拒绝）

Redis 里每个池两个 key：

```
sq:{shard}:{pool}:inflight   # 计数器 + TTL 心跳
sq:{shard}:{pool}:rpm        # 滑动窗口
```

**Redis 只用做"快速拒绝"，不用做"快速授予"。** 即：

- 如果 Redis 显示已满 → 直接 429，省一次 PG 事务。这是纯优化。
- 如果 Redis 显示有空位 → **仍然必须走 PG 事务**做权威扣减。

这个方向选得反直觉但必要：授予必须先过数据库，否则 Redis 丢数据会超发（E21）。而快速拒绝即使误判（Redis 数据偏紧）也只是多等一会，不会超发。**误判方向必须偏向保守。**

### 5.3 锁顺序（防止死锁）

一次 `acquire` 触及 `pool_shard` 和 `quota_pool` 两行。固定顺序：

```
1. pool_shard 行锁        (按 shard_key 升序；单次 acquire 只有一个)
2. quota_pool 行锁        (按 pool_id 升序；委派场景可能有多个)
3. tenant_share 行锁      (按 (pool_id, tenant_id) 升序)
4. 队列授予时：queue_entry 行锁
```

**只看池、不看 permit**——permit 行是插入而非更新，不需要预锁。

`release` / `expire` 只触及 `permit`（行锁）+ `pool_shard` + `quota_pool`，顺序同上。

避免"A acquire 持 shard 等 pool，B release 持 pool 等 shard"：所有路径严格按 `shard → pool → share → entry` 的顺序拿锁。

### 5.4 幂等与 deductible_key

```
BEGIN;
-- 1. 幂等记录（同 S08 §2.1）
-- 2. 写主租约
-- 3. 先查已有 permit
SELECT * FROM permit
 WHERE tenant_id=$t AND deductible_key=$k AND state <> 'EXPIRED'
 FOR UPDATE;
-- 命中 → 直接返回既有 permit（replayed=true），不占第二份计数
-- 4. 占位
SELECT * FROM pool_shard WHERE shard_key=$s FOR UPDATE;
IF shard.account_used_inflight + 1 > shard.account_cap_inflight THEN 429 (bottleneck=account)
SELECT * FROM quota_pool WHERE pool_id=$p FOR UPDATE;
IF pool.state <> 'ACTIVE' THEN 503 POOL_SUSPENDED
IF pool.inflight_used + 1 > pool.inflight_limit THEN 429 (bottleneck=inflight)
-- RPM/TPM 另判
UPDATE pool_shard SET account_used_inflight = account_used_inflight + 1;
UPDATE quota_pool SET inflight_used = inflight_used + 1, updated_at=now();
INSERT INTO permit(... state='RESERVED', lease_expires_at=now()+ttl ...);
INSERT INTO pool_usage_ledger(... delta_inflight=+1, reason='ACQUIRE' ...);
-- 5. outbox: scheduler.permit.changed (RESERVED)
COMMIT;
```

`permit_deduct_uniq` 唯一索引是第二道闸：并发两笔同 `deductible_key` 时，第二笔在 INSERT 处撞唯一约束，回滚后重查并返回既有 permit。**不需要应用层分布式锁。**

**EXPIRED 后允许重建**：索引的 `WHERE state <> 'EXPIRED'` 子句让过期 permit 不挡新 permit。这是正确的——过期的 permit 从未真正提交过，重建是安全的。

### 5.5 Redis 丢失与重建（E21）

docs/12："Redis只缓存令牌桶，丢失后保守暂停窗口或用持久请求日志重建。"

实现两条都走：

```
检测：Redis key 缺失率 > 阈值，或 reload 后 sq:* 扫描为空
  │
  ├─ 步骤 1（立即，0–2s）：进入 CACHE_DEGRADED 保守窗口
  │    所有 acquire 返回 503 CACHE_DEGRADED
  │    ★ 这是"保守暂停"，宁可等待也不超发
  │
  ├─ 步骤 2（异步，通常 <30s）：从 PG 重建
  │    inflight_used 无需重算 —— PG 的 quota_pool.inflight_used 就是权威，
  │    因为每次变更都在同一事务里改了它。
  │    Redis 只是它的镜像。重建 = 按 pool 把 PG 的值写回 Redis。
  │
  │    ★ 关键设计：Redis 里没有"PG 不知道的计数"。
  │      这消除了"重建时漏算在途"的整类 bug。
  │
  ├─ 步骤 3：对账校验
  │    SELECT pool_id, count(*) FROM permit
  │     WHERE state IN ('RESERVED','DISPATCH_AUTHORIZED','INFLIGHT') GROUP BY pool_id
  │    必须等于 quota_pool.inflight_used（RESERVED 计入）——不等则告警并
  │    以 permit 聚合值为准修正 quota_pool，同时写 adjustment 审计
  │
  └─ 步骤 4：退出 DEGRADED，恢复正常
```

因为权威计数在 PG 里且与 permit 状态同事务更新，**Redis 丢失不会导致超发，只会导致短暂的拒绝服务**。这是 E21 的正解，也是"Redis 不持有唯一事实"的落地。

如果连 PG 的 `quota_pool.inflight_used` 都怀疑（例如从中恢复点重建），用 `permit` 聚合重算——这就是 `pool_usage_ledger` 的用途。

### 5.6 租约

```sql
-- janitor，每 5s
WITH expired AS (
  SELECT permit_id FROM permit
   WHERE state = 'RESERVED' AND lease_expires_at < now()
   ORDER BY lease_expires_at LIMIT 200
   FOR UPDATE SKIP LOCKED
)
UPDATE permit SET state='EXPIRED', release_reason='NEVER_DISPATCHED', released_at=now()
 WHERE permit_id IN (SELECT permit_id FROM expired)
RETURNING permit_id, pool_id, shard_key;
-- 随后批量扣减计数并写 ledger + outbox
```

`SKIP LOCKED` 让多个 janitor 副本可并行。**只处理 `RESERVED`**——`DISPATCH_AUTHORIZED` 与 `INFLIGHT` 绝不进入这个 UPDATE。

### 5.7 公平调度

`WAITING` 队列的选择：

```sql
-- 每池一次授予时执行
SELECT qe.*, ts.virtual_time, ts.weight, ts.guaranteed_share
  FROM queue_entry qe
  JOIN tenant_share ts ON ts.tenant_id = qe.tenant_id AND ts.pool_id = qe.pool_id
 WHERE qe.pool_id = $p AND qe.state = 'WAITING'
 ORDER BY
   -- 1) 保底份额未用完的租户优先
   (ts.inflight_granted < ts.guaranteed_share) DESC,
   -- 2) 加权公平：虚拟时间最小者优先
   ts.virtual_time ASC,
   -- 3) aging：等待越久 effective_at 越早
   qe.effective_at ASC,
   -- 4) 优先级
   qe.priority ASC,
   qe.enqueued_at ASC
 LIMIT 1
 FOR UPDATE SKIP LOCKED;
```

授予后：

```sql
UPDATE tenant_share
   SET virtual_time = virtual_time + (expected_run_seconds::numeric / weight),
       inflight_granted = inflight_granted + 1
 WHERE tenant_id = $t AND pool_id = $p;
```

- `effective_at = enqueued_at - aging_bonus`，`aging_bonus` 随等待时长线性增长，实现 aging。
- **优先级只影响尚未 dispatch 的任务**（docs/12）。已经 INFLIGHT 的 permit 不受后续高优先级任务影响——所以优先级在 `queue_entry` 上，不在 `permit` 上做任何抢占。
- 在线人工修改可以在租户份额内插队（`priority=0`），但不能突破 `guaranteed_share` 或全局上限。

---

## 6. 事件

### 6.1 发布：`scheduler.permit.changed`

Topic `drama.scheduler-quota.v2`，partition key `tenant_id:aggregate_id`，`aggregate_id = permit_id`。

```jsonc
{
  "specversion":"1.0","schema_version":"2.0",
  "id":"evt_...","source":"S22","type":"scheduler.permit.changed",
  "subject":"permit/permit_01H...","time":"...",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"permit_01H...","aggregate_sequence":3,
  "correlation_id":"cmd_...","causation_id":"att_...","traceparent":"...",
  "data": {
    "permit_id":"permit_01H...",
    "pool_id":"pool_seedance_volc_a1",
    "attempt_id":"attempt_01H...",
    "previous_state":"DISPATCH_AUTHORIZED",
    "state":"INFLIGHT",
    "release_reason":null,
    "dispatch_id":"disp_01H...",
    "execution_fence":2,
    "inflight_used":38,
    "inflight_limit":40,
    "account_used_inflight":53,
    "account_cap_inflight":60
  },
  "data_ref": null
}
```

`aggregate_sequence`: RESERVED=1, DISPATCH_AUTHORIZED=2, INFLIGHT=3, RELEASED=4, EXPIRED=5。

事件里带**池的计数快照**是刻意的：S47 的队列看板需要"实时容量"，但 S22 不提供高频轮询接口给 UI。让 UI 从事件流构建即可，并附带 `aggregate_sequence` 保证顺序。UI 必须显示水位时间（docs/04 §5），不能把旧事件当当前值。

### 6.2 消费

| 事件 | 来源 | S22 的处理 | 失败策略 |
|---|---|---|---|
| `model.capability.changed` | S24 | 更新池的 `revision`、`inflight_limit`、`rpm/tpm_limit`；账户池禁用 → `SUSPENDED`；账户新增 → 建池 | 重试；乱序按 `revision` 丢弃旧的（**池定义版本单调**） |
| `access.grant.changed` | S01 | 撤销的租户/principal → 其 `WAITING` 队列项 `CANCELLED`；已授予 permit 不动 | 重试 |
| `generation.job.state-changed` | S26 | **不用它推进 permit**。只在 `state=SUBMISSION_UNKNOWN` 时把对应 permit 标记 `reconcile_required_at`（一个审计标记，不改状态） | 重试 |
| `connector.observation.received` | S27 | 回填 `provider_job_ref`；**不改变 permit state** | 重试 |
| `audit.export.ready` | S45 | 无关 | — |

**S22 绝不消费 `finance.ledger.entry-posted`**。S22 不知道钱，也不应该知道。

**S22 绝不因为 `generation.job.state-changed: SUCCEEDED` 就释放 permit**。终态释放必须走 `release` 命令带证据。事件是通知，命令是事实。这条防线挡住"事件乱序导致提前释放"。

### 6.3 Inbox / Outbox

与 S08 §6.3/§6.4 相同。S22 的 consumer 名分：`s22.model`、`s22.access`、`s22.observation`。

`model.capability.changed` 的乱序处理要特别写：事件里带池 `revision`，只在 `event.revision > pool.revision` 时应用。**account pool 元数据的重建是幂等的**（整体覆盖），不需要缓冲 gap。

---

## 7. 测试清单

### 7.1 幂等与版本（E12）

| # | 用例 | 期望 |
|---|---|---|
| T22-01 `[E12]` | 同 `command_id` 同 body `acquire` ×2 | 返回同一 `permit_id`，`inflight_used` 只 +1，Outbox 只 1 条 RESERVED |
| T22-02 `[E12]` | 同 `command_id` 不同 body | 409 `IDEMPOTENCY_KEY_REUSED`，计数不变 |
| T22-03 | 不同 `command_id` 相同 `deductible_key` | 返回同一 permit，`replayed:true`，不占第二份 |
| T22-04 | 同 `deductible_key` 不同 `attempt_id` | 422 `DEDUCTIBLE_KEY_CONFLICT` |
| T22-05 | permit 已 EXPIRED 后同 `deductible_key` 重 acquire | 允许新建，占新槽 |

### 7.2 配额守恒（E20 / E21）

| # | 用例 | 期望 |
|---|---|---|
| T22-06 `[E20]` | 池 `inflight_limit=5`，并发 100 个 acquire | 恰好 5 个 200，95 个 429；`inflight_used=5`；无任何时刻 >5 |
| T22-07 `[E20]` | 两个 modality 池共享账号，`account_cap=10`，两池各 limit=8，并发 50+50 | 总授予 ≤ 10，无一池超过账号上限 |
| T22-08 | RPM 限流与 inflight 独立：inflight 有空位但 RPM 用尽 | 429 `bottleneck=rpm` |
| T22-09 `[E21]` | 正常运行中 `FLUSHALL` Redis | 2s 内 acquire 返回 503 `CACHE_DEGRADED`；重建后恢复；全程 `inflight_used` 不变、无超发 |
| T22-10 `[E21]` | Redis 清空 + 同时 200 并发 acquire | 全部在 DEGRADED 窗口被拒；窗口结束后授予数 = 剩余容量 |
| T22-11 `[E21]` | 调度器进程全部重启，等待期中有 500 个 WAITING | 重启后队列完整；不丢 entry；已 INFLIGHT permit 计数不变 |
| T22-12 `[E21]` | 重启期间 Redis 也丢了 | 重建后对账校验 `permit 聚合 == inflight_used`，通过 |
| T22-13 | `resource_class=gpu_second` 池 | 不占 `account_cap_inflight`；按资源秒计；与 API 池互不影响 |
| T22-14 | 池 limit 从 40 下调到 20，当前 used=35 | 拒绝新的；已有 35 个继续；used 降到 <20 后恢复授予 |
| T22-15 | `pool_shard.account_cap` 校验：配置时 Σ pool.limit > cap | 配置阶段拒绝（DDL/管理 API 422） |

### 7.3 预算/配额 Saga（E14 / E15）

| # | 用例 | 期望 |
|---|---|---|
| T22-16 `[E14]` | S23 reserve 成功，S22 acquire 返回 429 | S26 收到 429；S22 无 permit 创建；S23 预留被 S26 释放；不进入 dispatch |
| T22-17 `[E14]` | 同上但 S22 池 SUSPENDED | S26 收到 503；同上 |
| T22-18 `[E15]` | `confirm-dispatch` 在 S22 已提交后响应丢失 | S26 重试同 command_id → 得到相同 `dispatch_id`，状态 INFLIGHT；不产生第二个 permit |
| T22-19 `[E15]` | `confirm-dispatch` 响应 S23 确认超时（S26 内部超时） | 不进入 dispatch；permit 停在 DISPATCH_AUTHORIZED；Saga 恢复后明确推进或 release |
| T22-20 `[E15]` | 对已 RELEASED permit 调 `confirm-dispatch` | 409 `PERMIT_NOT_DISPATCHABLE`；S26 走补偿 |
| T22-21 | `expected_grant_epoch` 被另一个执行器抢先 | 后到者 409 `GRANT_EPOCH_CONFLICT`；只有一个执行器进入 INFLIGHT |

### 7.4 未知提交与终态（E16 / E22）

| # | 用例 | 期望 |
|---|---|---|
| T22-22 `[E16]` | permit INFLIGHT，S26 进入 SUBMISSION_UNKNOWN | permit 保持 INFLIGHT；超 24h 告警；**不自动释放** |
| T22-23 `[E16]` | 供应商确认从未收到 → release(RECONCILED_ABSENT) | permit RELEASED；槽释放；`release_reason` 正确 |
| T22-24 `[E16]` | 供应商无法确认 | permit 永久 INFLIGHT；池显示容量缺口；运维 OPERATOR 释放留审计 |
| T22-25 `[E22]` | 任务被取消（不可取消供应商任务） | permit 不释放；S23 继续结算；S22 侧 state 不变直到终态 |
| T22-26 `[E22]` | 执行 fence 递增 | 未 dispatch 的 permit → `release(SUPERSEDED_BY_FENCE)`；**已 INFLIGHT 的不动** |
| T22-27 | `release` 缺 `terminal_evidence_ref` | 422；permit 不变 |
| T22-28 | 重复 `release` 同 command_id | 返回首次结果 |
| T22-29 | 重复 `release` 不同 command_id | 返回首次结果（幂等），不重复扣减 |

### 7.5 租约与 janitor

| # | 用例 | 期望 |
|---|---|---|
| T22-30 | RESERVED 租约到期 | janitor 置 EXPIRED，两槽释放，事件发出 |
| T22-31 | DISPATCH_AUTHORIZED 租约到期 | **不** expire；告警 |
| T22-32 | INFLIGHT 永久不释放 | **不** expire；告警 |
| T22-33 | 5 个 janitor 副本并发扫描 | 无重复处理（`SKIP LOCKED`） |
| T22-34 | janitor 处理中崩溃 | 事务回滚，permit 仍 RESERVED，下次扫描重来 |

### 7.6 公平与排序

| # | 用例 | 期望 |
|---|---|---|
| T22-35 | 大租户 batch + 小租户 interactive | 小租户不被饿死；`guaranteed_share` 生效 |
| T22-36 | 同租户 100 个任务 vs 另一租户 1 个 | 虚拟时间使两者交替获得 |
| T22-37 | 高优先级任务到达，已有 INFLIGHT | 不抢占；只影响队列顺序 |
| T22-38 | WAITING 超 deadline | 转 ABANDONED，不占计数 |

### 7.7 安全与租户（E28）

| # | 用例 | 期望 |
|---|---|---|
| T22-39 `[E28]` | 租户 B 读租户 A 的 permit | 404 |
| T22-40 `[E28]` | 租户 B 用租户 A 的 `reservation_ref` acquire | 404/422，不泄漏 A 的存在 |
| T22-41 | 外部 principal 调 `:expire` / `:suspend` | 403 |
| T22-42 | BYOK 池（`tenant_scope=other_tenant`）被别的租户请求 | 404（**不能借凭证容灾**） |

### 7.8 供应商异常与降级（E18）

| # | 用例 | 期望 |
|---|---|---|
| T22-45 `[E18]` | `route_decision_ref` 指向的池不存在 | 422 `POOL_NOT_FOUND_FOR_ROUTE`；**不搜索其他池**、不建 permit、不占任何计数 |
| T22-46 `[E18]` | 唯一匹配池 `SUSPENDED`（账号被撤销） | 503 `POOL_SUSPENDED` 带 `suspend_reason`；**不借用同 provider 的其他账号、不跨区域**；已有 permit 不受影响 |
| T22-47 `[E18]` | 供应商 429，池与账号均有余量但 RPM 用尽 | 429 `bottleneck=rpm`，`retryable:true`；不占用 inflight 槽（回滚后 `inflight_used` 不变） |
| T22-48 `[E18]` | 请求 `modality=video` 4K，S25 给的路由只匹配到低画质池 | 拒绝（422 `POOL_MODALITY_MISMATCH` 或按路由找不到）；**不得静默降到低画质/便宜模型** |
| T22-49 `[E18]` | 连续 5 次 5xx 后运维 `:suspend` | 池 `SUSPENDED`；之后 acquire 全部 503；`resume` 需 S02 授权 + 健康探测通过（§3.3） |
| T22-50 `[E18]` | `access.grant.changed` 到达但池定义版本更旧 | 按 `revision` 丢弃，池不倒退（§6 乱序规则） |
| T22-51 `[E18]` | 检查代码路径：任何 acquire 分支是否存在"换池重试" | 静态断言不存在——降级决策只能在 S25（§0.1） |

---

## 8. 契约冲突与待裁决

| # | 冲突 | 本文采用 | 需要谁改 |
|---|---|---|---|
| C22-1 | `service.yaml` 命令 `POST /permits:acquire`；`contracts/openapi.v2.yaml` **完全缺失** S22 的 permits/quota-pools 路径 | catalog 形态 + 本文 §2 | `contracts/openapi.v2.yaml` |
| C22-2 | S22 的查询入口 `GET /quota-pools/{id}` 在 openapi 里不存在 | 本文 §2.5 | 同上 |
| C22-3 | openapi 无 `index`/`permits:acquire` 的请求体定义 | 本文 §2.1 | 同上 |
| C22-4 | `admission-record.v2.schema.json` 的 `quota_permit.state` 只有 4 态，本文 permit 有 5 态（多 `EXPIRED`） | **`EXPIRED` 不对 admission 暴露**：已过期的 permit 绝不会出现在 admission record 里（S26 只在 permit 有效时才写） | 无需改 |
| C22-5 | S22 `depends_on: [S01, S24]`，但准入要求 S22 理解 reservation——本文用**不透明引用**避免新增依赖 | 保持 | 无需改 |
| C22-6 | 本文要求 `pool_shard` 两级计数（账号级 + 池级），docs/03 只提"不可被租户分片重复计算" | 本文是那句话的落地形式 | docs/12 可补一句 |

---

## 9. 运行与运维

- **部署**：独立 Deployment + ServiceAccount；PG 连接池按 shard 分（每 shard 一个写池）；Redis 独立实例（**不是共享缓存**，因为丢数据影响面不同）。
- **janitor**：独立 Deployment，`SKIP LOCKED` 多副本。监控：每次扫描处理数、最大滞留时长、`DISPATCH_AUTHORIZED` 超 30min 的数量。
- **对账作业**（每分钟 + 每日）：
  - 每分钟：`permit` 聚合 vs `quota_pool.inflight_used`；
  - 每分钟：`DISPATCH_AUTHORIZED` + `INFLIGHT` 超过阈值的 permit 列表 → 告警队列；
  - 每日：`pool_usage_ledger` 与 `permit` 状态一致性；账号池与 S24 元数据一致性。
- **runbook 必备**：Redis 丢失处置（§5.5）；账号被撤销（`:suspend` + 已有 permit 处理）；池容量下调；`DISPATCH_AUTHORIZED` 长期挂起；跨地域切换时 `pool_shard` 的写主确认。
- **灾备**：docs/14 §恢复顺序——"恢复身份/租户home路由与数据→验证 S08/S16/S23/S26 权威版本→恢复事件与Temporal→**对账所有 SUBMITTING/UNKNOWN/INFLIGHT**→…→保守开放新准入"。S22 在这一步负责：所有 INFLIGHT permit 保持占用，直到 S26 对账完成；**不以 RTO 为由自动释放**。

---

## 10. 明确不做的

- 不校验余额、不读 S23 的库、不消费 `finance.ledger.entry-posted`。
- 不判定供应商任务是否成功（S27/S26 的事）。
- 不做抢占式调度——已 INFLIGHT 的任务永不被打断。
- 不用 Redis 做权威计数。
- 不自动释放 `DISPATCH_AUTHORIZED` / `INFLIGHT`。
- 不把 `estimated_wait_ms` 当作 SLA 承诺。
