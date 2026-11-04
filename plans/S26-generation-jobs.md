# S26 · 统一生成任务协调 —— 实现方案

状态：`design_only`。本文是实现方案，不是已实现代码。所有供应商仍为 `disabled`。

上游依据：[docs/09 §3](../docs/09-model-execution.md)、[docs/04 §3](../docs/04-data-consistency.md)（准入 Saga 七步）、[docs/12 §2/§4](../docs/12-budget-scheduling.md)、[docs/07 §4](../docs/07-production-workflows.md)、`plans/S25-model-routing.md`、`plans/S27-provider-connectors.md`。
链上位置：`S24 注册事实 → S25 算可行路由 → S26 持久化任务与准入 Saga → S27 按渠道执行`。

本文重点回答两个问题：**S26 ⇄ S27 这个环怎么用事件拆开**（§10）和**未知提交怎么不重复花钱**（§7）。

---

## 1. 边界

### 1.1 本服务是唯一写者

| 事实 | 说明 |
|---|---|
| `GenerationAttempt` | 一次业务意义上的生成尝试（业务 attempt 计数在此） |
| `ProviderJob` | 一个具体派单的完整生命周期（含 network retry 计数） |
| `SubmissionIntent` | "我确实要拿这笔钱和这个 permit 去提交"的持久化意图 |
| `AdmissionSaga` | 准入 Saga 的七步状态与补偿 |
| `IdempotencyKey` | 业务幂等键的归属 |
| `SUBMISSION_UNKNOWN` 状态 | 以及它的对账结论 |

### 1.2 本服务不写

- 余额/预留/实际费用（S23）。S26 持有的是**token 引用**，不是金额权威。
- permit/在途槽（S22）。S26 持有 **permit 引用**。
- 供应商请求回执、回调 Inbox（S27）。
- 能力事实（S24）、路由决策（S25）。
- 成片、制品（S28）。S26 只知道 "artifact 已归档" 这个观测。

### 1.3 "所有付费调用只经本服务"

`docs/09` / `README`：S29–S34、S19 的 LLM/VLM、S36 评估器、S48 训练，全部经 S26。

实现上的强制手段（不是约定）：

1. 供应商凭证只注入 S27 的渠道 Deployment，且 S27 的 submit 必须携带 S26 签发的 **dispatch grant**（含 `request_hash` 绑定与 `fencing_token`）。没有 grant 的连接器直接拒绝（本地配置 `require_dispatch_grant=true`，无开关可关）。
2. `docs/14`：S27 的出站 `NetworkPolicy` 只允许从 `provider-connectors/*` 的 Pod 出，其他服务不具备供应商出网身份。
3. S19 的 Agent 工具沙箱不挂载任何供应商凭证，也不在允许的出站白名单里。

三条任一缺失，这条硬约束就退化成文档约定。

---

## 2. 实体与关键区分

### 2.1 Attempt / ProviderJob / NetworkRetry 三层

```
GenerationAttempt   (业务层：一次"我想生成这个镜头"的决定)
  └─ ProviderJob    (执行层：一次对某个供应商渠道的派单)
       └─ NetworkRetry  (传输层：一次 HTTP 尝试)
```

**三层不能合并**。理由：

- `docs/11 §3`：每镜头最多 3 个自动业务 attempt，网络重试与真实付费重生成**分开**。
- 换模型重试 = 新的 `ProviderJob`（同一 attempt 内）还是新的 `GenerationAttempt`？规则见 §2.2。
- 一个 `ProviderJob` 内可能有 N 次 network retry（连接超时、只读查询重试），只有 **submit 的 retry 会可能产生费用**。

```sql
CREATE TABLE generation_attempt (
  attempt_id        text PRIMARY KEY,
  tenant_id         text NOT NULL,
  project_id        text NOT NULL,
  series_id         text NOT NULL,
  episode_id        text NOT NULL,
  branch_id         text NOT NULL,
  node_id           text NOT NULL,
  node_epoch        integer NOT NULL,
  execution_fence   integer NOT NULL,
  modality          text NOT NULL,
  attempt_no        integer NOT NULL,          -- 业务 attempt 序号，1..N
  variation_nonce   text NOT NULL,
  idempotency_key   text NOT NULL,
  route_decision_id text NOT NULL,
  requirement_set   jsonb NOT NULL,
  max_business_attempts integer NOT NULL,      -- 来自 S39/S02 策略
  state             text NOT NULL,             -- OPEN|SUCCEEDED|FAILED|EXHAUSTED|ABANDONED
  created_at        timestamptz NOT NULL DEFAULT now(),
  closed_at         timestamptz,
  UNIQUE (tenant_id, idempotency_key),
  UNIQUE (tenant_id, node_id, node_epoch, attempt_no, variation_nonce),
  CHECK (attempt_no >= 1)
);
```

`idempotency_key` 由上游给（`generation-command.idempotency_key`，例 `ep3:node4:video:attempt1`）。同一 key 重投 → 同一 attempt，不新建。

### 2.2 什么算"同一个 ProviderJob"

| 变化 | 新 ProviderJob？ | 新 GenerationAttempt？ |
|---|---|---|
| 同参数、同渠道、network retry | 否 | 否 |
| 同参数、换账户池（同六元组其他要素） | 是（L1 降级） | 否 |
| 换模型（L2 降级） | 是 | **否**（仍是同一业务 attempt，因为用户意图未变） |
| 改参数（规格调整 L3a） | 是 | **是**（`attempt_no+1`，因为输入变了） |
| 拆镜 | 是 | 是（新 node） |
| QC 不通过要求重做 | 是 | 是 |
| 供应商返回可重试的 5xx（提交明确失败） | 否 | 否 |

规则一句话：**输入不变 → 同一 attempt；输入变了 → 新 attempt**。这样 `docs/11` 的"每镜头最多 3 个自动业务 attempt"才有意义。

### 2.3 ProviderJob 表

```sql
CREATE TABLE provider_job (
  provider_job_id   text PRIMARY KEY,
  attempt_id        text NOT NULL REFERENCES generation_attempt,
  tenant_id         text NOT NULL,
  job_no            integer NOT NULL,          -- 同一 attempt 内的第几个 job（换池/换模型）
  state             text NOT NULL,
  -- 见 §3 状态机
  provider          text NOT NULL,
  channel           text NOT NULL,
  model_id          text NOT NULL,
  model_snapshot    text NOT NULL,
  region            text NOT NULL,
  account_pool_id   text NOT NULL,
  requirement_hash  char(64) NOT NULL,         -- 规范化输入的 hash
  submission_nonce  text NOT NULL,             -- 每次真正 submit 尝试的 nonce（stable client key 的组成）
  provider_idempotency text NOT NULL,          -- supported|client_lookup_only|unsupported
  provider_job_ref  text,                      -- 供应商侧任务 id（拿到之前为 NULL）
  business_attempt  integer NOT NULL,          -- = attempt_no，冗余便于查询
  network_retry     integer NOT NULL DEFAULT 0,-- 传输层重试计数
  submit_attempts   integer NOT NULL DEFAULT 0,-- 真正发出过 submit 的次数（付费风险计数）
  last_error_code   text,
  last_error_at     timestamptz,
  next_action_at    timestamptz,               -- 下一轮轮询/退避的时间点
  poll_watermark    timestamptz,               -- 已消费到的供应商观测时间
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  closed_at         timestamptz,
  UNIQUE (attempt_id, job_no)
);
CREATE INDEX ix_job_due ON provider_job (next_action_at)
  WHERE state NOT IN ('ARCHIVED','FAILED','CANCELED');
CREATE INDEX ix_job_unknown ON provider_job (state, updated_at)
  WHERE state IN ('SUBMISSION_UNKNOWN','RECONCILING');
```

`submit_attempts` 是**关键计数器**：它统计"真的发出去过几次 submit 请求"。`network_retry` 只统计传输层重试（包括只读的 get_status）。两者分开，因为只有前者可能产生费用。

### 2.4 SubmissionIntent —— 花钱前的最后一道持久化

```sql
CREATE TABLE submission_intent (
  intent_id         text PRIMARY KEY,
  provider_job_id   text NOT NULL UNIQUE REFERENCES provider_job,
  tenant_id         text NOT NULL,
  request_hash      char(64) NOT NULL,
  route_decision_id text NOT NULL,
  -- 持有的外部承诺（引用，非权威）
  budget_reservation_id  text NOT NULL,
  budget_amount_micro    bigint NOT NULL,
  budget_currency        char(3) NOT NULL,
  budget_authorized_at   timestamptz NOT NULL,
  quota_permit_id        text NOT NULL,
  quota_pool_id          text NOT NULL,
  quota_state            text NOT NULL,        -- RESERVED|DISPATCH_AUTHORIZED|INFLIGHT|RELEASED
  -- 幂等与栅栏
  dispatch_id            text NOT NULL UNIQUE,
  fencing_token          bigint NOT NULL,
  stable_client_key      text NOT NULL,
  -- 版本绑定
  capability_bundle_rev  bigint NOT NULL,
  price_snapshot_id      text NOT NULL,
  node_epoch             integer NOT NULL,
  execution_fence        integer NOT NULL,
  -- 状态
  state                  text NOT NULL,        -- see §6
  created_at             timestamptz NOT NULL DEFAULT now(),
  submitted_at           timestamptz,
  resolved_at            timestamptz
);
CREATE UNIQUE INDEX ux_sik ON submission_intent (tenant_id, stable_client_key);
```

`stable_client_key` 的构造（必须跨重试**逐字节相同**，这是供应商侧幂等的基础）：

```text
stable_client_key = "dr-" + base32_hex(
    sha256( tenant_id || "|" || attempt_id || "|" || job_no || "|"
          || requirement_hash || "|" || submission_nonce ))[0:40]
```

`submission_nonce` 在**第一次**准备 submit 时生成并持久化。此后所有重发（若允许）必须用同一个 key。**不允许**在重发时重新生成 nonce——那就是一个新请求，供应商会当成新任务。

---

## 3. ProviderJob 状态机

`docs/09` 给出的主干：

```
CREATED → RESERVING → ADMITTED → DISPATCH_AUTHORIZED → SUBMITTING
        → QUEUED / RUNNING → PROVIDER_SUCCEEDED / FAILED / CANCELED
        → INGESTING → ARCHIVED
旁路：SUBMISSION_UNKNOWN / RECONCILING
```

### 3.1 完整转移表

| from | to | 触发 | 守卫 | 副作用 |
|---|---|---|---|---|
| — | `CREATED` | `POST /generation-jobs` | 路由决策有效且未过期 | 写 attempt（若无）+ job + outbox `job.created` |
| `CREATED` | `RESERVING` | Saga step 1 开始 | — | 调 S23 `reserve_budget`（幂等，键 `{intent_id}:reserve`） |
| `RESERVING` | `ADMITTED` | S23 返回 `RESERVED` **且** S22 返回 permit `RESERVED` | 两者都成 | 写 `submission_intent`（state=`RESERVED`）+ outbox `job.admitted` |
| `RESERVING` | `REJECTED_BUDGET` | S23 明确拒绝（余额不足） | — | 补偿：无（S23 未占用）；写 attempt 失败原因 |
| `RESERVING` | `REJECTED_QUOTA` | S22 明确拒绝（无可派 permit） | — | 补偿：调 S23 `release`（此刻 reservation 尚未提交，可安全释放） |
| `RESERVING` | `RESERVING`（重试） | S23/S22 超时或 `UNKNOWN` | 重试预算未耗尽 | 不退化为"算成功"；见 §6.2 |
| `ADMITTED` | `DISPATCH_AUTHORIZED` | Saga step 3–4 完成，grant 已签发 | **实时**复查 S23 授权仍有效 + S22 permit 转 `DISPATCH_AUTHORIZED` 成功 | 写 grant（`dispatch_id`+`fencing_token`）+ outbox `connector.dispatch-requested` |
| `ADMITTED` | `RESERVING`（回退） | 预复查发现授权失效 | — | 重新走一遍（可能换池）或拒绝 |
| `DISPATCH_AUTHORIZED` | `SUBMITTING` | S27 领取 grant（`POST /dispatch-grants/{id}:claim`） | `fencing_token` 匹配且 grant 未领取过 | 记 `grant_state=CLAIMED`、`submit_attempts += 1`（在 S27 侧实际发出时由回执确认） |
| `SUBMITTING` | `QUEUED` | S27 回执 `accepted` 且供应商返回 job id | — | 存 `provider_job_ref` |
| `SUBMITTING` | `RUNNING` | 首个 `get_status` 显示在跑（有的供应商不提 QUEUED） | — | — |
| `SUBMITTING` | `PROVIDER_FAILED` | S27 回执 `rejected`（明确的 4xx 业务拒绝，如政策拒、参数非法） | — | 结算（可能按 `failure_charge` 计费）+ 释放 permit |
| `SUBMITTING` | `SUBMISSION_UNKNOWN` | **任何无法确定供应商是否已收单的情况**（见 §7.1） | — | **保留**预算与 permit，不释放，不重发 |
| `SUBMITTING` | `RETRYABLE`（回 `DISPATCH_AUTHORIZED`） | S27 回执明确 `not_submitted`（连接未建立、供应商明确 429 未受理） | `provider_idempotency != 'unsupported'` **或** 能证明请求未到达 | 同一 `stable_client_key` 重发；`network_retry += 1` |
| `QUEUED` | `RUNNING` | 观测 | — | — |
| `RUNNING` | `PROVIDER_SUCCEEDED` | 观测终态成功 + 有输出 manifest 摘要 | — | outbox `job.succeeded`；进入 `INGESTING` |
| `QUEUED`/`RUNNING` | `PROVIDER_FAILED` | 观测终态失败 | — | 结算实际费用；释放 permit |
| `QUEUED`/`RUNNING`/`SUBMITTING` | `CANCELED` | 观测取消成功 | — | 结算（供应商可能仍计费）；释放 permit；**不报退款** |
| `QUEUED`/`RUNNING` | `SUBMISSION_UNKNOWN`? | 不允许 | 已有 `provider_job_ref` 就不该 UNKNOWN | — |
| `PROVIDER_SUCCEEDED` | `INGESTING` | 归档请求已发 | — | 调 S28 `POST /artifact-imports`（携带供应商 URL + TTL） |
| `INGESTING` | `ARCHIVED` | S28 返回 `artifact.ingested` | artifact hash 与观测一致 | 释放 permit 前先确认已完成；发 `job.archived` |
| `INGESTING` | `INGESTING`（重试） | S28 失败 | — | **只重归档，不重生成**；URL 过期走 §8 |
| `SUBMISSION_UNKNOWN` | `RECONCILING` | 对账调度器到期 | — | 列出对账手段，逐个尝试（§7.3） |
| `RECONCILING` | `QUEUED`/`RUNNING` | 对账证明**供应商已收单** | 找到 `provider_job_ref` | 补写引用，恢复正常跟踪 |
| `RECONCILING` | `ADMITTED` | 对账证明**供应商未收单** 且 可安全重提 | 见 §7.4 | 保留预算/permit，重新走 dispatch 授权 |
| `RECONCILING` | `SUBMISSION_UNKNOWN` | 对账手段穷尽仍无法确认 | — | 保持冻结（§7.5） |
| 任意终态 | `ARCHIVED` | — | — | 不可逆 |

### 3.2 哪些状态是终态

`PROVIDER_SUCCEEDED` 不是终态（还要 `INGESTING → ARCHIVED`）。
`PROVIDER_FAILED` / `CANCELED` 在**结算完成前**也不是终态（`SETTLING` 子状态由 Saga 处理）。
真正不可逆的终态只有 `ARCHIVED`。

### 3.3 状态转移的持久化方式

每次转移是一个本地事务：

```sql
BEGIN;
  SELECT ... FROM provider_job WHERE provider_job_id=$1 FOR UPDATE;
  -- 校验允许的转移（应用层白名单，见 §3.1 表）
  UPDATE provider_job SET state=$2, ... WHERE provider_job_id=$1;
  UPDATE provider_job SET aggregate_sequence = nextval('job_seq_'||$1) ...
  INSERT INTO outbox (...) VALUES (...);   -- 同一事务
COMMIT;
```

`aggregate_sequence` 单调递增，S27 与 S47 用它拒绝旧事件倒退状态（`docs/04`：`状态终态不得被旧事件倒退`）。

---

## 4. 准入 Saga 七步

`docs/04 §3` 的七步，逐步落到表与补偿上。

### 步骤 0（前置）：固定 request_hash 与报价上界

在 `CREATED` 时完成。`request_hash` 由规范化后的 `GenerationCommand` + `RouteDecision.normalized_input` 计算，写入 `submission_intent`。同一 `attempt_id + job_no` 的 `request_hash` 一旦写入**不可变**——这是"S26 固定 attempt_id、request_hash、报价上界"的实现。

报价上界来自 `route_decision.eligible[rank].quote_upper_bound_micro`，写入 `budget_amount_micro`。**不用期望成本预留**（S25 §4.2）。

### 步骤 1：S23 创建预算 reservation

```http
POST /budget-authorizations  (S23)
{
  "command_id": "{intent_id}:reserve",
  "tenant_id": "...", "budget_scope_id": "budget_ep3",
  "amount_micro": 1080000, "currency": "USD",
  "purpose": "generation",
  "price_snapshot_id": "pr_7a1...",
  "quote_valid_to": "...",
  "expires_at": "...",
  "attempt_id": "...", "provider_job_id": "...",
  "request_hash": "..."
}
```

幂等键 `{intent_id}:reserve`：重投返回同一 `reservation_id`，不重复扣。

| 结果 | 处理 |
|---|---|
| `RESERVED` | 记 `budget_reservation_id`，进步骤 2 |
| `REJECTED_INSUFFICIENT` | job → `REJECTED_BUDGET`；尝试是一条可用记录，不消耗业务 attempt |
| `REJECTED_PERMISSION` | job → `REJECTED_BUDGET`，原因单独记 |
| 超时 / 5xx | **重试同一幂等键**，最多 N 次（指数退避，总时长受 `deadline` 约束）。重试期间 job 停在 `RESERVING`，不前进 |
| 重试耗尽 | job → `RESERVING_FAILED`，outbox 事件，S21 决定是否等待 |

关键：**超时不能当成功**。S23 可能已创建 reservation 但响应丢失——那就保留在 `RESERVING` 继续用同一键查询，而不是假设没建过。

### 步骤 2：S22 申请 permit

```http
POST /permits:acquire  (S22)
{
  "command_id": "{intent_id}:permit",
  "tenant_id": "...", "pool_id": "pool_seedance_volc_cn_shared",
  "modality": "video",
  "provider": "seedance", "channel": "volcengine", "region": "cn-beijing",
  "budget_reservation_id": "...",       // 让 S22 能校验两者绑定
  "expected_inflight_seconds": 300,
  "attempt_id": "...", "provider_job_id": "..."
}
```

| 结果 | 处理 |
|---|---|
| `RESERVED` (permit) | 进步骤 3 |
| `REJECTED_NO_CAPACITY` | **补偿**：调 S23 `release(reservation_id)`；job → `REJECTED_QUOTA` |
| `REJECTED_TENANT_LIMIT` | 同上 |
| 超时 | 保持 `RESERVING` 重试同一键；**不释放预算**（可能 permit 已建） |
| 重试耗尽 | job → `RESERVING_FAILED`，**保留预算 reservation 不释放**，交对账扫描（§9）— 因为 permit 可能已建而响应丢失 |

**E14 的落点**：预算成功、quota 失败 → 必须补偿释放预算，且**不进入 dispatch**。实现上 `REJECTED_QUOTA` 转移路径上强制先调 `release` 成功（或进入 `RELEASING` 子状态）才允许写终态。

**E15 的落点**：quota 已 mark、预算确认超时。顺序上预算在前（步骤 1），所以这里的对称情形是"预算已 RESERVED、permit 申请超时"——处理见上表最后一行：停在 `RESERVING`，用同一幂等键反复确认，**绝不前进到 submit**。若在更后面的步骤发现预算侧 ack 不明，同样停在原地（§4 步骤 3 的守卫）。

### 步骤 3：持久化 SubmissionIntent + Outbox

两个外部承诺都拿到后，**同一本地事务**：

```sql
BEGIN;
  UPDATE provider_job SET state='ADMITTED' WHERE ...;
  INSERT INTO submission_intent (..., state='RESERVED') VALUES (...);
  INSERT INTO outbox (aggregate_id, aggregate_seq, event_type, payload)
    VALUES (job_id, nextval(...), 'job.admitted', {...});
COMMIT;
```

意图落库之前崩溃 → saga 恢复时从 `provider_job.state='RESERVING'` 重新查 S23/S22（同幂等键），不重复占用。

意图落库之后崩溃 → 恢复从 intent 继续，不再调 reserve/permit（它们是幂等的，但没必要再调）。

### 步骤 4：派单前实时复查 + 签发 dispatch grant

`ADMITTED → DISPATCH_AUTHORIZED` 之间，**必须**做三件实时检查（`docs/06 §4`："账号撤销、资产/声音授权撤销、企业 kill switch、数据驻留禁用和实时预算余额在每次 dispatch 前重新检查"）：

1. **S23**：`GET /budget-authorizations/{id}` 确认仍 `AUTHORIZED`/`RESERVED` 且金额未变；
2. **S22**：`POST /permits/{id}:authorize-dispatch`，permit `RESERVED → DISPATCH_AUTHORIZED`；这是 S22 的持久 ack；
3. **S24**：`POST /capability-validity`（批量），确认 `lifecycle=active`、`pool_health=healthy`、`evidence_valid_until>now()`、`combination=SUPPORTED`、`price_valid_to>now()`。

三者任一失败：

| 失败项 | 处理 |
|---|---|
| S23 余额不足 | job → `REJECTED_BUDGET`；补偿：S22 `release(permit)` |
| S22 拒绝 | job → `REJECTED_QUOTA`；补偿：S23 `release(reservation)` |
| S24 候选失效 | job → `ROUTE_INVALIDATED`；补偿：S23 `release` + S22 `release`；S21 重新路由 |
| 超时 | 停在 `ADMITTED` 重试；**不签发 grant** |

`docs/04`："任一 ack 不明时不提交，继续恢复 Saga；已有 submitted permit 与 reservation 不按普通 TTL 回收。"

三条都通过后，**同事务**写 grant：

```json
{
  "dispatch_id": "dsp_01J...",
  "provider_job_id": "pj_...",
  "attempt_id": "...",
  "tenant_id": "...",
  "fencing_token": 7,
  "request_hash": "…",
  "stable_client_key": "dr-…",
  "budget_reservation_id": "...",
  "quota_permit_id": "...",
  "candidate": {"provider":"seedance","channel":"volcengine","model_snapshot":"250528",
                "region":"cn-beijing","account_pool_id":"…"},
  "provider_idempotency": "client_lookup_only",
  "submit_payload_ref": "s26://submission-intents/{intent_id}/payload@sha256:…",
  "expires_at": "2026-10-30T03:17:00Z",
  "single_use": true
}
```

`grant_state`: `ISSUED → CLAIMED → (REVOKED)`。`ISSUED` 无领取、过期后可 `REVOKED` 并回到 `ADMITTED`（因为从未发出过请求，不产生费用）。`CLAIMED` 之后**永不撤销**（撤销没有意义，请求可能已经在路上）。

### 步骤 5：S27 领取并提交

见 §10 环解耦与 S27 方案。S26 的职责：

- 提供 `POST /dispatch-grants/{dispatch_id}:claim`（幂等，`{dispatch_id}` 唯一）；
- 领取时校验 `fencing_token` 与 job 当前 fence 一致，且 grant 未领取过；
- 领取成功即把 job 转 `SUBMITTING`，`submission_intent.state = 'CLAIMED'`；
- 开始一个 **提交确认 timer**（见 §7.1）。

`docs/04`："供应商不支持幂等时，dispatch grant 只允许一个 fenced 执行器领取一次；领取后崩溃产生未知提交，不重新领取同任务自动重发。"

### 步骤 6：结果不明 → `SUBMISSION_UNKNOWN`

见 §7。

### 步骤 7：权威终态后结算

```http
POST /budget-authorizations/{id}:settle  (S23)
{ "command_id": "{intent_id}:settle", "actual_amount_micro": ..., "usage_ref": {...} }

POST /permits/{id}:release  (S22)
{ "command_id": "{intent_id}:release-permit", "reason": "terminal" }
```

规则：

- 结算键 `{intent_id}:settle` 幂等，重复投递无重复扣减（`docs/04`："每一步有唯一业务事件键，重复投递无重复扣减"）。
- 结算发生在 `PROVIDER_SUCCEEDED` / `PROVIDER_FAILED` / `CANCELED` 之后，**不在 `ARCHIVED` 之后**——因为成片归档可能耗时很长，而费用已经确定。
- 但 permit 的释放必须在**归档完成或明确放弃**之后（`docs/12 §2`："已经授权/提交或未知任务必须对账确认终态再释放"）。
- 失败任务与取消任务：按 `price_snapshot.failure_charge` 结算；`none` 时 `actual=0` 但仍要记一条 settlement（0 元也是一条事实）。
- **取消不等于退款**（`docs/12 §4`）：`CANCELED` 只影响 permit 释放与后续调度，实际费用以 S23 结算为准。

### 步骤 8：`SUBMISSION_UNKNOWN` 下的额度处理

- 预算：`reservation` 保持 `AUTHORIZED`（不是 `RELEASED`），`docs/04`："调用结果不明进入 SUBMISSION_UNKNOWN，保持费用与在途占用"。
- permit：保持 `INFLIGHT`。`admission-record.v2.schema.json` 里 `SUBMISSION_UNKNOWN` 的 `allOf` 已经强制了这一点（`reservation.state ∈ {AUTHORIZED, ACCRUED}`、`quota_permit.state = INFLIGHT`、`grant_state = CLAIMED`、`must_reconcile_before_resubmit = true`）。
- 两者的**普通 TTL 不适用**：S22/S23 侧必须有"该 permit/reservation 被 UNKNOWN 任务持有"的标记，扫地任务跳过它们（§9）。

---

## 5. 准入 Saga 的编排方式：Temporal 还是本地表

结论：**准入 Saga 本身用本地表 + Outbox 驱动，不放在 Temporal Workflow 里**。

理由：

1. `docs/07 §4` 说 Temporal 承载生产流程（SeriesBatch → Episode → Scene/Shot → ProductionStage），S26 的准入是**每个 attempt 一个短状态机**，频率极高（`docs/03`：每日 500,000 镜头任务，~900,000 视频请求）。把每个都做成 Temporal Workflow 会在 history 上爆炸。
2. `docs/04 §3` 明确说"这是一组可恢复的本地事务与补偿，不宣称跨 DB 原子事务"——它要求的是可恢复，不是要求一个工作流引擎。
3. 未知提交的对账需要**长时间冻结**（可能几天，等供应商账单）。Temporal workflow 里挂几天比放在一个有 `next_action_at` 的表里昂贵得多。

所以：

| 层 | 承载 |
|---|---|
| S21 Temporal | 生产流程（episode/shot/stage）、等待信号、`ProviderJobWait` |
| S26 本地表 | 单个 attempt 的准入 Saga、ProviderJob 状态机、对账 |
| S26 定时器 | `next_action_at` 扫描（polling/backoff/reconcile due） |

S21 的 `ProviderJobWait` activity 通过**信号**唤醒：S26 发 `generation.job.state-changed`，S21 消费后 signal 到对应 workflow。S21 不轮询 S26。

---

## 6. Saga 各步失败的完整矩阵

| 步骤 | 失败 | 已完成的外部事实 | 补偿 | job 状态 | 是否可能已花钱 |
|---|---|---|---|---|---|
| 1 预算 | 明确拒绝 | 无 | 无 | `REJECTED_BUDGET` | 否 |
| 1 预算 | 超时/不明 | 未知（可能已建 reservation） | 用同键查询；确认未建则放弃 | `RESERVING`（保持） | 否 |
| 2 配额 | 明确拒绝 | reservation 已建 | S23 `release` | `REJECTED_QUOTA` | 否 |
| 2 配额 | 超时/不明 | reservation 已建；permit 未知 | 同键查询；**预算不释放** | `RESERVING`（保持） | 否 |
| 3 意图落库 | DB 失败 | reservation + permit 已建 | 无（事务回滚）；恢复时重查两者 | `RESERVING` | 否 |
| 4 复查 | S23 失效 | reservation + permit | S22 release | `REJECTED_BUDGET`（或重新路由） | 否 |
| 4 复查 | S22 拒绝 | 同上 | S23 release | `REJECTED_QUOTA` | 否 |
| 4 复查 | S24 失效 | 同上 | 双向 release | `ROUTE_INVALIDATED` | 否 |
| 4 复查 | 超时 | 同上 | 无；重试 | `ADMITTED`（保持） | 否 |
| 5 提交 | S27 明确 `not_submitted` | grant 已领取 | 若供应商幂等可重发；否则**转 UNKNOWN**（§7.1） | `DISPATCH_AUTHORIZED` 或 `SUBMISSION_UNKNOWN` | 否（但不敢断言） |
| 5 提交 | S27 明确 `rejected`（业务 4xx） | 请求已达供应商 | 结算（按失败计费） | `PROVIDER_FAILED` | 可能（失败计费） |
| 5 提交 | 响应丢失 / 超时 | 未知 | **冻结** | `SUBMISSION_UNKNOWN` | **可能** |
| 6 等待 | 观测终态 | 全部 | 结算 + release | `PROVIDER_*` | 是 |
| 7 归档 | S28 失败 | 已付费成功 | 只重归档 | `INGESTING` | 已花（不重复） |

---

## 7. 未知提交：怎么不重复花钱

这是本文最重要的一节。

### 7.1 什么情况判定为 `SUBMISSION_UNKNOWN`

S27 执行 submit 时，只有以下几类结论是**确定**的：

| S27 观测 | 结论 | S26 处理 |
|---|---|---|
| HTTP 2xx + 拿到供应商 job id | **已提交** | `QUEUED`（记 `provider_job_ref`） |
| HTTP 4xx 且供应商 body 明确"未受理"（参数非法、政策拒绝、额度不足） | **未提交** | `PROVIDER_FAILED`（可能按失败计费） |
| HTTP 429 且供应商明确未受理 | **未提交** | 退避重试（同 key，同 grant 不重发） |
| **连接未建立**（DNS/TCP 失败、TLS 握手失败、本地连接池拒绝） | **未发送** | 可安全重试 |
| 请求已写出、等待响应时超时 | **未知** | `SUBMISSION_UNKNOWN` |
| 收到 5xx | **未知**（供应商往往已经创建了任务才 500） | `SUBMISSION_UNKNOWN` |
| 连接被对端重置（请求可能已解析） | **未知** | `SUBMISSION_UNKNOWN` |
| 本进程在写出后崩溃 | **未知** | `SUBMISSION_UNKNOWN` |

规则：**只有能证明"字节没有离开本机"的失败才是可重试的**。其余全部进 UNKNOWN。这个判定的实现放在 S27（它知道 socket 层发生了什么），但**决策权在 S26**：S27 只上报 `{outcome: not_sent | unknown | rejected | accepted}`，S26 决定状态。

S27 必须在 submit 前落一条 `connector_request_receipt(state='SENDING')`，发出后更新为 `SENT`（或收到响应时直接终态）。S26 侧有一个 **submit-confirm timer**：`DISPATCH_AUTHORIZED → SUBMITTING` 时起 2×p99(submit_latency)，到期未见回执 → 主动把 job 转 `SUBMISSION_UNKNOWN`。这是"提交确认"与"超时"分离的落点（docs/09："5xx/timeout 先判断提交是否不确定"）。

### 7.2 `SUBMISSION_UNKNOWN` 下的铁律

1. **不重发**。没有例外，没有"我觉得大概没成功"。
2. **不释放预算**。reservation 保持 `AUTHORIZED`。
3. **不释放在途槽**。permit 保持 `INFLIGHT`。
4. **不重新生成 grant**。原 grant 已 `CLAIMED`，`fencing_token` 已消耗。
5. **不换账户池重试**。换池 = 新请求 = 可能第二次付费。这一点容易被"降级链"误导——降级链只在**确定的失败**之后才启动。
6. **不因 deadline 逼近而重提**。deadline 到了就报 `INCOMPLETE`，见 §7.6。

这六条写在 S26 的状态转移白名单里：`SUBMISSION_UNKNOWN` 只有 3 条出边（`RECONCILING`、`QUEUED`/`RUNNING`（对账成功后）、`ARCHIVED`（放弃后归档）），没有通往 `DISPATCH_AUTHORIZED` 的边。

### 7.3 对账流程

`RECONCILING` 是一个有穷的、按渠道能力降序尝试的过程。每轮尝试写一条 `reconciliation_attempt`：

```sql
CREATE TABLE reconciliation_attempt (
  recon_id        text PRIMARY KEY,
  provider_job_id text NOT NULL REFERENCES provider_job,
  round_no        integer NOT NULL,
  method          text NOT NULL,     -- list_by_client_key | list_recent | query_by_metadata
                                     -- | manual_provider_console | ledger_snapshot | bill_reconcile
  outcome         text NOT NULL,     -- found | not_found | inconclusive | unsupported | error
  evidence_ref    text,
  attempted_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider_job_id, round_no, method),
  CHECK (method IN ('list_by_client_key','list_recent','query_by_metadata',
                    'manual_provider_console','ledger_snapshot','bill_reconcile')),
  CHECK (outcome IN ('found','not_found','inconclusive','unsupported','error'))
);
```

按 `provider_idempotency` 分派：

#### A. `provider_idempotency = 'supported'`

供应商接受 `client_key`/`idempotency-key` 头并保证同键只建一个任务。

对账方法 `list_by_client_key`：`GET /tasks?client_key={stable_client_key}`。

- `found` → 已收单，转 `QUEUED`，补写 `provider_job_ref`。
- `not_found` 且距离首次提交已过 `2 × p99_submit_latency`：可以**用同一个 key 重发**。因为供应商保证幂等，重发不会双扣。这是这一档唯一允许的自动重提路径，且重发时 `submit_attempts += 1` 照记。
- `unsupported`（该渠道没有按 client key 查询的接口）→ 降到 B 的处理。

#### B. `provider_idempotency = 'client_lookup_only'`

供应商**不保证**幂等，但提供按业务元数据（如 `external_id` 或有时间窗的任务列表）查询的能力。典型情形：供应商接受自定义 metadata，但重复提交会创建两个任务。

对账方法 `list_recent` / `query_by_metadata`：在 `[首次提交时间 - 1min, now]` 窗口内列出该账户的任务，按 `stable_client_key`（放在 metadata 里）或按请求内容指纹匹配。

- `found` → 已收单，转 `QUEUED`。
- `not_found`：**仍然不能自动重发**。原因：任务列表本身可能有延迟、分页、或该供应商在任务完成后从列表里移除已完成的项。"列表里没有"不等于"没建过"。
  处理：至少观察 `max(1h, 3 × p99_task_list_lag)` 后，若仍 `not_found`，才允许进入**人工/策略确认的重提**：写一条 `resubmission_approval` 记录，需要 `policy.allow_reconcile_resubmit=true` 或者人审。默认 `false`。
- `unsupported` → 降到 C。

#### C. `provider_idempotency = 'unsupported'`

供应商既不幂等、也不提供可用的查询。

对账方法只剩：

1. **`manual_provider_console`**：运营在供应商后台查（有凭证的人）。结论必须由人录入并有 `reviewer`。
2. **`ledger_snapshot`**：等供应商账单/用量接口。若该供应商的账单粒度能定位到任务，则 `found`。
3. **`bill_reconcile`**：日终账单对账（S23 侧，见 §9）。

- `found` → 转 `QUEUED`（或如果任务已完成且结果是临时 URL 已过期，转 `INGESTING` 的失败分支）。
- `not_found` 且账单已覆盖该时间窗 → 可以判定"未收单"，允许重提。
- 永远 `inconclusive` → 保持 UNKNOWN，走 §7.5。

#### 对账调度

```
round 1   : T+0        （立即）      方法：该档位的最快方法
round 2   : T+60s
round 3   : T+5m
round 4   : T+30m
round 5   : T+2h
round 6   : T+6h
round 7   : T+24h
round 8+  : 每 24h，直到 T+7d
T+7d      : 转人工队列（`SUBMISSION_UNKNOWN` 长期冻结），告警
```

`next_action_at` 驱动，不占用长连接。

### 7.4 从 RECONCILING 回到可提交状态的条件

只有全部满足才允许 `RECONCILING → ADMITTED`（重走步骤 4）：

1. 至少一种方法给出 `not_found`，且该方法在**该渠道上被判定为可靠**（`list_by_client_key` 可靠；`list_recent` 需等观察窗；`manual_provider_console` 需人签字）；
2. 或者供应商幂等 = `supported`（此时不需要 1）；
3. 预算与 permit 仍有效（重新走步骤 4 会再查一遍，若已过期则必须重新准入整个 attempt）；
4. 该 `attempt_id` 的 `submit_attempts` 未超上限（S02 策略，默认同一 attempt 最多 2 次真实 submit）；
5. 写入 `submission_intent.state = 'RESUBMIT_APPROVED'` 与 `reconciliation_attempt` 证据链。

第 4 条是费用上限的实际执行点：即使每次都能确认"上次没成功"，也不能无限重试。

### 7.5 长期冻结

超过观察期仍 UNKNOWN：

- job 保持 `SUBMISSION_UNKNOWN`，`next_action_at` 设为下一个对账点；
- 预算与 permit **继续持有**（这是"保守暂记"）；
- 产生一条 `unknown_submission_alert`，按 `docs/14 §3`：`unknown submission` 是明确的告警项；
- 若该业主持有量超过阈值（`policy.max_frozen_amount_micro`），S23 的**新任务准入被冻结**（`docs/12`："需保守暂记与新任务冻结机制"）；但**正在跑的任务继续**；
- 该 attempt 对上游报 `UNKNOWN`，不是 `FAILED`。S21 得到 `UNKNOWN` 时不得把它当成功，也不得把它当可重试失败——按 INCOMPLETE 处理（`docs/07 §4`）。

**账务上**：冻结金额不是实际消费。若最终确认未收单，释放；若确认收单，结算；若永远无法确认，按客户合同处理（这是合同问题，不是代码问题）——但代码上必须能证明"我们确实不知道"，而不是猜。

### 7.6 与 deadline 的关系

`deadline` 到了而 job 仍在 UNKNOWN：

- 不重提、不放弃额度；
- attempt 标 `INCOMPLETE_UNKNOWN`；
- 上游分支继续跑其他节点（`docs/06` unattended：`无法解决的必要分支标 blocked，继续独立分支`）；
- 若后来对账成功且结果可用，仍可被 S08 作为**历史候选**使用（带 `late_result=true`），但不推进当前接受指针（`docs/04`：`旧 epoch 的结果永远不能推进当前接受指针`）。

---

## 8. 输出侧：下载失败与 URL 过期

`docs/09`："输出下载失败只重归档；供应商 URL 过期先查询新 URL，不能直接重做付费生成。"

### 8.1 归档流程

`PROVIDER_SUCCEEDED` 后：

```http
POST /artifact-imports  (S28)
{
  "command_id": "{intent_id}:ingest:{artifact_seq}",
  "tenant_id": "...",
  "provider_url": "https://.../signed?...",
  "provider_url_expires_at": "2026-10-30T12:00:00Z",
  "expected_sha256": "…",          // 供应商若提供；不提供则 S28 计算后回写
  "content_class": "generated_video",
  "source": {"provider_job_id":"pj_...","attempt_id":"...","model_snapshot":"…"},
  "retention": {"policy_ref": "..."}
}
```

S28 的安全要求（`docs/14 §2`：SSRF/内网/元数据访问）由 S28 负责，S26 只传 URL。

### 8.2 归档失败的分支

| 失败 | 处理 |
|---|---|
| S28 下载失败（网络/超时） | 重试**归档**（同 `command_id`），指数退避。**不重生成** |
| 供应商 URL 已过期（403/410） | 见 8.3 |
| S28 校验 hash 不符 | 重试一次；仍不符 → `INGEST_FAILED`，告警，人工 |
| S28 拒绝（恶意/格式） | `INGEST_REJECTED`，人工；**不重生成**（重生成会得到同样问题） |
| 归档重试耗尽 | `INGEST_FAILED`，缺口可见（E23：`最终缺口可见`） |

### 8.3 URL 过期的处理顺序

```
1. 该渠道有 GET /tasks/{id} 且返回新 URL？
     → 是：取新 URL，重置 S28 的重试（新 command_id），不产生费用
2. 该渠道有独立的 download/export 接口？
     → 是：用之
3. 供应商支持"延长 URL 有效期"？
     → 是：调用后重试
4. 以上都没有，且任务记录仍在供应商 TTL 内
     → 尝试用同一 provider_job_ref 再查一次（延迟/最终一致）
5. 全部失败（任务记录也过期了）
     → INGEST_FAILED，报缺口
     → **绝不**因"拿不到成片"而重新生成
```

第 5 条是硬约束：重新生成要花第二次钱，而且新结果可能与旧的不同，会让同一 attempt 出现两个不同成片。宁可报缺口。

`job_ttl_seconds` / `url_ttl_seconds` 来自 S24 快照，S26 用它算"最晚什么时候必须开始归档"，并提前告警（`docs/14 §3`：`下载TTL逼近`）。

**归档前置优先**：`PROVIDER_SUCCEEDED → INGESTING` 应立即发起，不排在其他工作之后。`docs/16 E23` 的"优先归档"就是这个意思。

---

## 9. 对账与巡检（S26 侧）

`docs/12 §4`："每天/分钟级扫描未知提交、超期 permit、预留泄漏、账单漂移、价格过期"。

S26 负责其中三项（其余在 S23/S22）：

### 9.1 扫描任务

| 巡检 | 周期 | 动作 |
|---|---|---|
| `due_jobs` | 10s | `next_action_at <= now()` 的 poll / backoff / reconcile |
| `stuck_submitting` | 30s | `SUBMITTING` 超过 `2×p99_submit` 且无回执 → 转 `SUBMISSION_UNKNOWN` |
| `unknown_submissions` | 5min | 按 §7.3 排对账轮次；超 7 天转人工 |
| `ingest_ttl_warning` | 1min | `provider_url_expires_at - now() < 15min` 且未归档 → 告警并提高优先级 |
| `ingest_backlog` | 1min | `INGESTING` 超过阈值 → 告警 |
| `orphan_intents` | 5min | `submission_intent.state='RESERVED'` 超过 `30min` 未推进 → 检查是否 saga 卡住 |
| `reservation_leak_check` | 1h | 与 S23 对账：S26 认为持有但 S23 已释放 / 反之 → 告警，不自动改 |
| `fence_mismatch` | 实时 | 收到 `execution_fence` 更高的取消时，撤销未领取的 grant，已领取的转 `CANCEL_REQUESTED` |

### 9.2 execution_fence 与取消

`docs/04` / `AGENTS.md`：`execution_fence` 是 run/branch 级，`node_epoch` 是单节点级，不能混用。

| 事件 | S26 动作 |
|---|---|
| 收到 `execution_fence` 提升（run 取消/切主） | 所有该 fence 下的 job：`ISSUED` grant → `REVOKED`；`DISPATCH_AUTHORIZED` 未领取 → 回到 `ADMITTED` 并终止；`QUEUED`/`RUNNING` → 尝试 `cancel`（能力允许时），否则 `discard_on_completion` |
| 收到 `node_epoch` 提升（局部重做） | 仅该节点的 job 受影响；其他节点不动。已完成的结果保留为历史 |
| 旧 fence 的事件到达 | 拒绝，不倒退状态（`aggregate_sequence` + fence 双重校验） |

`discard_on_completion`：任务继续跑完（不能取消），成片归档后**不进入候选/接受**，仅作为历史。费用照结（`docs/12`："不可取消任务继续结算"）。

### 9.3 单写者与栅栏

- `provider_job` 按 `provider/account_pool/region` 分片，同一 job 只有其 home shard 能写。
- `fencing_token` 在每个 job 每次签发 grant 时 `+1`（单调）。
- S27 的 submit 必须带 `fencing_token`；S26 在 `claim` 时校验。
- 灾备切主：新主从 `submission_intent` 恢复；`docs/14 §5`："仅有新 region 锁不证明旧请求未发生" → 所有 `SUBMITTING` 一律进 `RECONCILING`，**不重提**。

---

## 10. S26 ⇄ S27 的环怎么解耦

这是 `docs/09`、`services/README.md`（"依赖关系是运行协作关系，不是启动拓扑要求；异步环通过事件拆解"）都点名的问题。

### 10.1 环长什么样

静态依赖图：

```
S26 depends_on S27    (要把命令发给连接器)
S27 depends_on S26    (要领取 dispatch grant)
```

同步调用会死锁：S26 启动等 S27，S27 启动等 S26。更糟的是运行时环：S26 调 S27 submit，S27 回调 S26 领取 grant，S26 又得回 S27……

### 10.2 解法：把"调用"变成"命令 + 观测"，两侧都不阻塞

关键洞察：**这个环里没有真正的同步依赖**。S26 不需要 S27 的返回值来完成自己的事务，S27 也不需要 S26 在线才能处理一条已有命令。

拆成两条**单向事件流**：

```
S26 ──(Outbox 命令事件)──▶ S27        方向 A：派单
S27 ──(Outbox 观测事件)──▶ S26        方向 B：回执与状态
```

两者都经过 Kafka，**没有同步 RPC 在关键路径上**。

### 10.3 方向 A：S26 → S27 的派单命令

S26 在步骤 4 完成后，**同一本地事务**写 Outbox：

```json
{
  "type": "connector.submit.requested",
  "subject": "dispatch/dsp_01J...",
  "data": {
    "dispatch_id": "dsp_01J...",
    "provider_job_id": "pj_...",
    "tenant_id": "...",
    "channel_route": "seedance/volcengine",     // 决定哪个渠道 Deployment 消费
    "stable_client_key": "dr-…",
    "fencing_token": 7,
    "request_hash": "…",
    "submit_payload_ref": "s26://submission-intents/{id}/payload@sha256:…",
    "submit_payload_inline": { /* 小的直接内联，大媒体走 ref */ },
    "provider_idempotency": "client_lookup_only",
    "deadline": "..."
  }
}
```

Topic 分区：`drama.provider-connectors.v2` 的**命令 topic** 按 `channel_route` 分区，每个渠道的 Deployment 只消费自己的分区。S27 是"共同契约 + 服务家族"，不是共享 worker（`docs/09 §2`）——事件路由到独立消费者。

**S26 发完事件就结束这次操作**，job 停在 `SUBMISSION_UNKNOWN` 的**前置状态** `DISPATCH_AUTHORIZED`（grant 已签发，等 S27 领取）。S26 不需要 S27 在线。

### 10.4 S27 侧：领取 grant 用什么方式

S27 消费到 `connector.submit.requested` 后，**必须**在真正发出 HTTP 之前向 S26 领取 grant。这一步是同步调用（`POST /dispatch-grants/{id}:claim`），不是事件。原因：

- 领取必须是**即时、单次、有原子性**的。用事件做不到"恰好一个执行器领到"，会退化成分布式锁。
- 领取失败（grant 已领、fence 不符、已撤销）必须立刻知道，不能"发个事件然后猜"。

但这不构成运行时的环，因为：**S26 在方向上永远是同步调用的服务端，S27 是客户端**。S26 不需要调用 S27 来完成 claim。方向的单向性保住了。

```
时间线：
T0  S26: 写 outbox (submit.requested) + grant(ISSUED)      [本地事务]
T1  S26: 事务提交，返回
T2  Kafka 投递（at-least-once，可能重复）
T3  S27: 消费 → POST :claim(grant)  ────────────────▶ S26
T4  S26: 校验 fence、grant 未被领 → CLAIMED, job=SUBMITTING  ◀── 200
T5  S27: 落 connector_request_receipt(SENDING)
T6  S27: 发出供应商 HTTP
T7  S27: 写 receipt(SENT/RESPONSE) + outbox (observation.received)
T8  Kafka 投递
T9  S26: 消费观测 → 推进状态
```

T4 是唯一的同步调用，方向 S27→S26。T0/T9 是事件。**没有任何路径需要 S26 同步调 S27。**

### 10.5 重复投递怎么办

Kafka at-least-once → `submit.requested` 可能重复。S27 侧的防护：

1. `connector_command_inbox` 按 `(consumer, event_id)` 去重；
2. `connector_request_receipt` 唯一约束 `dispatch_id`——同一 dispatch 只有一条回执（`docs/09`："receipt 唯一约束同 dispatch_id"）；
3. 第二次消费同一 `dispatch_id` 时，领取会被拒（`grant_state=CLAIMED`），S27 回一条 `connector.submit.duplicate-ignored`（可选），不重发 HTTP。

**这就是 receipt 唯一约束的作用**：把"事件重复"降级为"一次 HTTP"。

### 10.6 S27 宕机 / 重启

- S27 停着 → `submit.requested` 堆在 Kafka，S26 的 job 停在 `DISPATCH_AUTHORIZED`，grant `ISSUED` 会过期。
- grant 过期（`expires_at`）→ S26 侧 `ISSUED → REVOKED`，job 回到 `ADMITTED`，可以重新签发（新 dispatch_id、新 fence）。因为**从未领取过、从未发出过请求**，不产生费用。
- 这样"连接器故障"自然变成"派单延迟"，而不是"任务丢失"。

`docs/09` 的隔离要求（单供应商故障不影响其他渠道）由此成立：一个渠道 Deployment 挂了，其他渠道的分区照常消费。

### 10.7 S26 宕机 / 重启

- S27 消费到命令但 claim 失败（S26 不可用）→ S27 按退避重试 claim，**不发出 HTTP**（这是关键：claim 是 submit 的前置门）。
- claim 一直失败 → S27 最终把命令放入自己的重试队列，事件留在 Kafka，不丢。
- S26 恢复 → 从 `provider_job.state='DISPATCH_AUTHORIZED'` 与 `submission_intent` 继续。`ISSUED` grant 若已过期则撤销重发。

**付款保护的第一原则**：没有成功 claim，就不发 HTTP。S27 的 `require_dispatch_grant=true` 是不可配置关闭的编译期常量。

### 10.8 为什么不用 S26 直接同步调 S27 的 submit

可以工作，但退化：

1. S26 必须处理"调用了但不知道结果"（这就是 §7 的问题，只是从供应商转移到了 S27）；
2. 渠道隔离要靠超时/熔断，而不是队列隔离；
3. S26 的可用性与每个渠道的可用性耦合——所有渠道同时挂的概率远高于任一渠道挂。

用 Outbox 之后，"S26 不知道结果"只在 **S27 实际发出了 HTTP** 时出现，而 S27 对"发出去了吗"有更精确的观测（socket 层）。这才是 correct-by-design 的分工。

### 10.9 环解除的形式化表述

| 边 | 类型 | 阻塞？ |
|---|---|---|
| S26 → S27（命令） | Kafka 事件 | 否 |
| S26 → S27（无其他调用） | — | — |
| S27 → S26（`claim`） | 同步 HTTP | **是**，但 S26 是服务端，不会反向依赖 |
| S27 → S26（观测） | Kafka 事件 | 否 |
| S26 → S23 / S22（准入） | 同步 HTTP | 是（准入必须同步拿 ack） |
| S26 → S24（复查） | 同步 HTTP | 是（必须实时） |
| S26 → S28（归档） | 同步 HTTP + 事件回报 | 部分 |

环只剩 S26→S27（事件）与 S27→S26（claim）两条边，方向相反且一条是事件。**没有同步环，可以任意顺序启动。**

---

## 11. 数据模型汇总（S26 schema）

已在上文给出 `generation_attempt`、`provider_job`、`submission_intent`、`reconciliation_attempt`。补充：

```sql
-- dispatch grant（可与 submission_intent 合并，此处独立便于按 dispatch_id 查询与过期扫描）
CREATE TABLE dispatch_grant (
  dispatch_id      text PRIMARY KEY,
  provider_job_id  text NOT NULL REFERENCES provider_job,
  tenant_id        text NOT NULL,
  request_hash     char(64) NOT NULL,
  fencing_token    bigint NOT NULL,
  stable_client_key text NOT NULL,
  state            text NOT NULL,   -- ISSUED|CLAIMED|REVOKED
  issued_at        timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL,
  claimed_at       timestamptz,
  claimed_by       text,            -- S27 实例 id（审计）
  CHECK (state IN ('ISSUED','CLAIMED','REVOKED')),
  CHECK (state <> 'CLAIMED' OR claimed_at IS NOT NULL)
);
CREATE UNIQUE INDEX ux_grant_claim ON dispatch_grant (dispatch_id) WHERE state='CLAIMED';
CREATE INDEX ix_grant_expiring ON dispatch_grant (expires_at) WHERE state='ISSUED';

-- 状态转移审计（每次转移一行，不可变）
CREATE TABLE job_transition (
  provider_job_id text NOT NULL,
  seq             bigint NOT NULL,
  from_state      text NOT NULL,
  to_state        text NOT NULL,
  reason_code     text,
  actor           text NOT NULL,      -- service|worker|operator:<id>
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider_job_id, seq)
);

-- 业务 attempt 上限与费用累计（跨 Run 不重置）
CREATE TABLE attempt_ledger (
  tenant_id       text NOT NULL,
  node_id         text NOT NULL,
  attempts_used   integer NOT NULL,
  micro_spent     bigint NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, node_id)
);

CREATE TABLE command_receipt (
  tenant_id     text NOT NULL,
  command_id    text NOT NULL,
  request_hash  char(64) NOT NULL,
  response_code integer NOT NULL,
  response_body jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE outbox (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id     text NOT NULL,          -- 事件信封 required
  aggregate_id  text NOT NULL,          -- job_id
  aggregate_seq bigint NOT NULL,        -- job.revision
  event_type    text NOT NULL,
  partition_key text NOT NULL,          -- tenant_id || ':' || aggregate_id
  payload       jsonb NOT NULL,
  headers       jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz,
  UNIQUE (aggregate_id, aggregate_seq, event_type)
);
CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;
```

`attempt_ledger` 是 `docs/11 §3` 与 `docs/16 E26` 的落点：`换模型、换 Run 不能清零已消耗的总费用`。它按 `(tenant, node)` 累计，不按 Run 重置。

---

## 12. 契约

### 12.1 `POST /generation-jobs`

```jsonc
{
  "command_id": "cmd-gj-0001",
  "route_decision_id": "route_01J...",
  "generation_command": { /* generation-command.v2.schema.json */ }
}
```

返回 `202` + `Operation`（`{operation_id, state: accepted, run_id?}`），其中 `operation_id` 即 `provider_job_id` 的包装。

校验顺序（失败即返回，不进入 Saga）：

1. `Idempotency-Key` 头 + `command_id` 幂等；
2. schema 校验（`generation-command.v2`）；
3. `route_decision_ref.content_hash` 匹配 S25 的记录；
4. `quote_valid_to > now()`，否则 409 `ROUTE_QUOTE_EXPIRED`；
5. `attempt_no` 未超 `max_business_attempts`，否则 409 `ATTEMPT_LIMIT_EXCEEDED`；
6. 该 `(tenant, node_id)` 的 `attempt_ledger.micro_spent` 未超节点上限，否则 409 `NODE_COST_LIMIT_EXCEEDED`；
7. `deadline` 未过，否则 422 `DEADLINE_PASSED`；
8. `requirement_hash` 与 `route_decision.normalized_input` 一致（防止调用方绕过 S25 的规格吸附）。

### 12.2 `GET /generation-jobs/{id}`

```jsonc
{
  "provider_job_id": "pj_...",
  "source_revision": 12,
  "state": "RUNNING",
  "business_attempt": 1,
  "network_retry": 2,
  "submit_attempts": 1,
  "provider_job_ref": "task_abc",
  "submitted_at": "...",
  "last_observation_at": "...",
  "poll_watermark": "...",
  "output": {"artifact_refs": null},
  "must_reconcile_before_resubmit": false,
  "frozen": false,
  "history": [ {"seq":11,"from":"SUBMITTING","to":"QUEUED","at":"..."} ]
}
```

`must_reconcile_before_resubmit` 恒等于 `state == 'SUBMISSION_UNKNOWN'`（与 `admission-record.v2.schema.json` 的 `allOf` 一致）。

### 12.3 `GET /admissions/{admission_id}`

返回 `admission-record.v2.schema.json` 结构。S26 是 `admission_id` 的生成者。示例见 `contracts/examples/admission-unknown.json`——**该示例正是 `SUBMISSION_UNKNOWN` 的规范形态**：`reservation.state=AUTHORIZED`、`quota_permit.state=INFLIGHT`、`grant_state=CLAIMED`、`provider_job_id=null`、`must_reconcile_before_resubmit=true`。实现必须能逐字段产出这个记录。

### 12.4 `POST /dispatch-grants/{dispatch_id}:claim`

```jsonc
// 请求（来自 S27）
{
  "executor_id": "s27-seedance-volcengine-7f9c",
  "fencing_token": 7,
  "request_hash": "…"
}
// 200
{
  "dispatch_id": "dsp_01J...",
  "provider_job_id": "pj_...",
  "stable_client_key": "dr-…",
  "submit_payload_ref": "…",
  "provider_idempotency": "client_lookup_only",
  "channel_route": "seedance/volcengine",
  "deadline": "..."
}
// 409 GATE_ALREADY_CLAIMED  { "claimed_by": "...", "claimed_at": "..." }
// 409 GATE_FENCE_MISMATCH
// 410 GATE_EXPIRED
// 403 GATE_REVOKED
```

`claim` 必须在一个 DB 事务里完成：

```sql
BEGIN;
SELECT * FROM dispatch_grant WHERE dispatch_id=$1 FOR UPDATE;
-- state must be ISSUED, expires_at > now(), fencing_token matches
UPDATE dispatch_grant SET state='CLAIMED', claimed_at=now(), claimed_by=$2 WHERE dispatch_id=$1;
UPDATE provider_job SET state='SUBMITTING', updated_at=now() WHERE provider_job_id=$3 AND state='DISPATCH_AUTHORIZED';
-- 若上面的 UPDATE 影响 0 行 → ROLLBACK，返回 409
INSERT INTO outbox (...) VALUES ('job.submitting', ...);
COMMIT;
```

`UPDATE ... WHERE state='DISPATCH_AUTHORIZED'` 是第二道门：即使 grant 表被并发搞乱，job 状态机也不允许从别的状态跳到 `SUBMITTING`。

### 12.5 发出的事件

Topic `drama.generation-jobs.v2`，`event_type = generation.job.state-changed`，`aggregate_id = provider_job_id`。

```jsonc
{
  "specversion":"1.0","schema_version":"2.0",
  "id":"evt_...","source":"S26","type":"generation.job.state-changed",
  "subject":"provider-job/pj_...",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"pj_...","aggregate_sequence":12,
  "correlation_id":"...","causation_id":"...",
  "data":{
    "attempt_id":"attempt_video_demo","state":"SUBMISSION_UNKNOWN",
    "previous_state":"SUBMITTING",
    "business_attempt":1,"network_retry":0,"submit_attempts":1,
    "provider":"seedance","channel":"volcengine",
    "node_id":"shot_3_2.video","node_epoch":4,"execution_fence":2,
    "must_reconcile":true,
    "reason_code":"SUBMIT_TIMEOUT_AFTER_SEND"
  }
}
```

消费者别：S21（signal 到 workflow）、S47（投影）、S44（成本分析）、S45（审计）。

**不发事件的内容**：供应商原始响应体、Prompt 全文、密钥。用 `data_ref` 指向受限存储。

---

## 13. 失败分支汇总

| 情况 | 状态 | 预算 | permit | 用户可见 | 后续 |
|---|---|---|---|---|---|
| 429 | `DISPATCH_AUTHORIZED`（退避） | 保留 | 保留 | "等待配额" | timer 重试，尊重 `Retry-After` |
| 5xx（提交前，明确未发） | 同上 | 保留 | 保留 | "供应商异常" | 有界退避 |
| 5xx（提交后/不明） | `SUBMISSION_UNKNOWN` | **保留** | **保留** | "结果确认中" | 对账 |
| 401/403 | `ACCOUNT_UNAUTHORIZED` | 释放 | 释放 | "账户异常" | 隔离账户池（S24 `pool_health='unauthorized'`），告警；**不换渠道绕过** |
| 政策拒绝（供应商侧） | `PROVIDER_FAILED` | 结算（可能 0） | 释放 | "策略拒绝" | **不换渠道重试**（`docs/09`："政策拒绝不靠换渠道绕过"） |
| 输入组合错误（供应商说参数不行） | `PROVIDER_FAILED` / `CAPABILITY_DRIFT` | 结算 | 释放 | "能力不符" | **不无限重试**；写 `capability drift` 回 S24（§13.1） |
| 模型退休（提交时发现） | `ROUTE_INVALIDATED` | 释放 | 释放 | "模型已停用" | 重新路由 |
| 取消成功 | `CANCELED` | 结算实际 | 释放 | "已取消" | 不报退款 |
| 取消失败（不可取消） | `RUNNING` → `DISCARD_ON_COMPLETION` | 结算 | 释放（完成后） | "取消不可用，结果将丢弃" | 继续 |
| 归档失败 | `INGESTING` | 已结算 | 保留至终态 | "归档重试中" | 只重归档 |
| URL 过期 | `INGESTING` | —— | —— | "取新 URL" | §8.3 |

### 13.1 Capability drift 的回写

当供应商返回"参数不支持"而 S24 说支持时，说明 S24 的事实过期了。S26 是发现它的地方：

```http
POST /capability-drift-reports  (S24)
{
  "command_id": "...",
  "snapshot_id": "9f2c...e1",
  "mode": "first_last_frame",
  "combination_key": "6b3d...0f",
  "observed_error": {"provider_code":"invalid_parameter","message_hash":"…","occurred_at":"…"},
  "provider_job_id": "pj_...",
  "attempt_id": "..."
}
```

S24 收到后写 `capability_evidence(method='field_observation', result='unsupported')`，可能触发 `active → degraded`。

**S26 不得重试同一组合超过 1 次**（`docs/09`："输入组合错误不无限重试，记录 capability drift"）。

---

## 14. 可观测

| 指标 | 用途 | 告警 |
|---|---|---|
| `gj_submission_unknown_total{channel}` | 未知提交量 | > 阈值立即告警（`docs/14 §3`） |
| `gj_submission_unknown_age_seconds` | 冻结时长 | 超 1h 升级 |
| `gj_frozen_amount_micro{tenant}` | 冻结金额 | 超策略上限 → 该租户新准入冻结 |
| `gj_submit_attempts_per_job`（histogram） | 重复提交分布 | > 1 的占比异常 = 幂等有问题 |
| `gj_state{state}`（gauge） | 各状态任务数 | `SUBMITTING` 堆积 |
| `gj_poll_watermark_lag_seconds` | 观测延迟 | 超时 |
| `gj_ingest_ttl_remaining_seconds` | URL 剩余寿命 | < 900s 告警 |
| `gj_admission_saga_step_duration{step}` | 每步耗时 | S23/S22 慢 |
| `gj_saga_compensation_total{reason}` | 补偿次数 | E14 类问题的暴露面 |
| `gj_grant_expired_unclaimed_total` | grant 过期未领 | S27 消费能力不足 |
| `gj_reconcile_rounds_per_job` | 对账轮次 | > 8 转人工 |
| `gj_settle_lag_seconds` | 结算延迟 | |
| `gj_provider_cost_vs_quote_ratio` | 实际/报价 | > 1 持续 → 报价余量不足 |

追踪：`traceparent` 贯穿 command → attempt → job → dispatch → connector → ingest → settle（`docs/14 §3`）。

---

## 15. 测试清单

覆盖 E14–E19、E21、E23。

| 序号 | 场景 | 断言 | 映射 |
|---|---|---|---|
| T26-01 | 同 `Idempotency-Key` 同 body 重投 | 同一 `provider_job_id`，`submit_attempts` 不增，不重复预留 | E12 |
| T26-02 | 同 key 异 body | 409 | E12 |
| T26-03 | 预算成功、quota 明确拒绝 | 预算被 `release`（S23 侧可用额恢复），job=`REJECTED_QUOTA`，**从未发 submit** | **E14** |
| T26-04 | quota 已 mark、预算确认超时 | job 停在 `RESERVING`；重试同幂等键后结果明确；全程无 submit | **E15** |
| T26-05 | S23 reserve 超时但实际已建 | 重试同键拿到同一 `reservation_id`，不双预留 | E15/E20 |
| T26-06 | submit 响应丢失（供应商无幂等） | `state=SUBMISSION_UNKNOWN`，`reservation=AUTHORIZED`，`permit=INFLIGHT`，`grant=CLAIMED`，`must_reconcile=true`；**无第二次 HTTP** | **E16** |
| T26-07 | UNKNOWN 后调 `POST /generation-jobs` 同 key | 返回原 job，不新建、不重提 | E16 |
| T26-08 | UNKNOWN 后 S27 重投同一命令事件 | claim 被拒，无第二次 submit | E16/E17 |
| T26-09 | 对账 `list_by_client_key` 返回 found | 转 `QUEUED`，`provider_job_ref` 补齐，预算/permit 继续持有 | E16 |
| T26-10 | 对账 found 但任务已完成且 URL 过期 | 转 `INGESTING` → §8.3 分支，不重生成 | E16/E23 |
| T26-11 | `provider_idempotency=supported` + `not_found` | 允许同 key 重发，`submit_attempts` 增到 2 | §7.3A |
| T26-12 | `unsupported` + `not_found`（无账单证据） | **不重发**，保持 UNKNOWN | §7.3C |
| T26-13 | 回调先于 submit 响应到达 | S27 Inbox 先存；S26 收到观测时若 `provider_job_ref` 未知仍能按 `client_key` 关联；不倒退 | **E17** |
| T26-14 | 回调重复 5 次 | 状态推进一次，`aggregate_sequence` 不动 | E17 |
| T26-15 | 回调乱序（RUNNING 后收到 QUEUED） | 拒绝倒退，状态保持 RUNNING | **E17** |
| T26-16 | 伪签名回调 | S27 侧 401 拒绝，S26 收不到任何事件 | **E17** |
| T26-17 | 429 带 `Retry-After: 30` | `next_action_at = now+30s`，不重发 submit，预算/permit 保留 | **E18** |
| T26-18 | 5xx（提交后不明） | 进 UNKNOWN，不进重试 | E18 |
| T26-19 | 401 | `ACCOUNT_UNAUTHORIZED`，池隔离，预算/permit 释放，告警，**不换渠道** | E18 |
| T26-20 | 模型在派单前被 S24 标 retired | 步骤 4 复查失败 → `ROUTE_INVALIDATED`，释放两侧，无 submit | **E18** |
| T26-21 | 政策拒绝（供应商侧） | `PROVIDER_FAILED`，**不自动换渠道** | 硬约束 |
| T26-22 | 输入组合错误，反复失败 | 最多 1 次，写 capability drift 到 S24，不再重试 | 硬约束 |
| T26-23 | Redis 全清 | 所有 job 从 Postgres 恢复，`next_action_at` 重建，`SUBMISSION_UNKNOWN` 不丢 | **E21** |
| T26-24 | S22 重启、permit 表在（Postgres） | 在途计数不超；`INFLIGHT` permit 不被回收 | E21 |
| T26-25 | 供应商 URL 过期，渠道支持重新取 URL | 取新 URL，重归档，**无新付费任务** | **E23** |
| T26-26 | 供应商 URL 过期且任务记录已过期 | `INGEST_FAILED`，缺口可见，**不重生成** | E23 |
| T26-27 | 下载失败一次后成功 | 重归档成功，`submit_attempts` 仍为 1 | E23 |
| T26-28 | 归档 TTL 剩 5 分钟未归档 | 告警触发，优先调度 | E23 |
| T26-29 | 报价过期后调用 `POST /generation-jobs` | 409 `ROUTE_QUOTE_EXPIRED`，不预留 | §12.1 |
| T26-30 | 请求 4 秒但路由吸附到 5 秒 | 提交给 S27 的 payload 是 5 秒；`request_hash` 基于 5 秒 | §12.1 第 8 条 |
| T26-31 | 业务 attempt 超上限 | 409，不创建新 attempt | E26 |
| T26-32 | 跨 Run 的重试 | `attempt_ledger.micro_spent` 累计不重置 | **E26** |
| T26-33 | execution_fence 提升 | `ISSUED` grant 撤销；`RUNNING` 尝试 cancel 或 discard；结果不进候选 | E07/E34 |
| T26-34 | node_epoch 提升 | 只影响该节点，其他节点 job 不变 | E03/E07 |
| T26-35 | 灾难恢复：新区域启动 | 所有 `SUBMITTING` 进 `RECONCILING`，无重提 | **E34** |
| T26-36 | S27 全挂、S26 正常 | job 停在 `DISPATCH_AUTHORIZED`，grant 过期后撤销重签，不花钱 | §10.6 |
| T26-37 | S26 全挂、S27 正常 | S27 claim 失败重试，不发 HTTP | §10.7 |
| T26-38 | `admission-record` 逐字段 | 对 UNKNOWN 场景产出与 `examples/admission-unknown.json` 同构的记录 | §12.3 |
| T26-39 | 取消不可取消任务 | 状态 `RUNNING`+`discard_on_completion`，仍结算，不报退款 | E22 |
| T26-40 | 静态校验通过 | 以上行为测试必须跑真实 Postgres + mock S22/S23/S27，不靠 schema 校验 | docs/16 §6 |

---

## 16. 实施顺序与未决

顺序（W1）：

1. `generation_attempt` / `provider_job` / `job_transition` / 状态机骨架（无外部调用，纯状态转移测试）。
2. `POST /generation-jobs` + 幂等 + 路由决策校验。
3. 准入 Saga step 1–4 对接 S23/S22 的 mock（E14/E15 全部可测）。
4. `submission_intent` + `dispatch_grant` + `:claim` 接口。
5. Outbox 事件 `submit.requested` 与观测消费（接 S27 mock）。
6. UNKNOWN 判定 + 对账状态机（E16/E17 可测，无需真实供应商）。
7. 归档对接 S28 mock（E23 可测）。
8. 巡检任务与告警。
9. 真实渠道接入（需授权）。

未决：

- **`p99_submit_latency` 各渠道实测值**。`submit-confirm timer` 的时长依赖它。没有实测前用一个保守的 60s，代价是 UNKNOWN 判定偏晚，但不会误判成"未发送"。
- **`attempt_ledger` 的上限由谁定**。`docs/11` 给了"每镜头 3 个自动业务 attempt"作为示例，但跨 Run 的累计上限需要客户合同约束。实现上做成可配，默认取 `docs/11` 的示例值。
- **对账的人工队列归属**。S26 产生 `unknown_submission` 工单，但谁处理？需要接 S40 还是独立的运营队列，`docs/14 §3` 说"告警与运营队列可见"但没指定服务。当前设计：S26 发事件，由 S47/运营台呈现，S26 不假设有 S40 依赖（S40 现在不在 `depends_on` 里）。
- **`submission_intent.state` 的完整枚举**需要在实现时补齐：`RESERVED|CLAIMED|SUBMITTED|UNKNOWN|RESUBMIT_APPROVED|SETTLING|SETTLED|ABANDONED`。
- **S23 结算金额可能超报价**（`docs/12`："最终供应商费用可能超报价"）。S26 必须在 `settle` 时把 `actual > quote_upper_bound` 的情况显式上报，并触发"价格余量不足"指标。这不是错误，是已知风险。
