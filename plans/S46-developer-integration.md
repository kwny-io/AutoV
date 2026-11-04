# S46 · 开发者集成（外部 API / Webhook / SDK）—— 实现方案

状态：实现方案（W4）。`maturity: design_only` 不变。

上游依据：[docs/13 §1–§2](../docs/13-api-product.md)、[docs/01 §3](../docs/01-enterprise-architecture.md)、[AGENTS.md](../AGENTS.md) 第 24 行（"把 LLM 提供的 `tenant_id` 当可信输入"禁令）、[S46 规格](../docs/design-package/services/S46-developer-integration.md)。

配套：[S01](S01-identity-tenancy.md)、[S02](S02-governance-policy.md)、[S07](S07-change-management.md)、[S21](S21-orchestration.md)、[S26](S26-model-gateway.md)（准入）、[S45](S45-audit.md)（外部调用的审计）。

---

## 1. 边界：写什么 / 不写什么 / 一条必须坚持的原则

### 1.1 写什么

S46 是**外部接入层**，不是业务服务。它持有的四类事实：

| 事实 | 表 | 语义 |
|---|---|---|
| 外部客户端身份 | `api_client` / `api_key` / `client_scope_grant` | 谁拿到了钥匙、能做什么、什么时候过期 |
| 命令回执 | `integration_command_receipt` | 外部发来的这条命令，平台受理了没有、映射到哪个内部命令 |
| Webhook 订阅与投递回执 | `webhook_subscription` / `webhook_delivery` | 往外推什么、推过没有、对方怎么说 |
| 外部标识映射 | `external_id_mapping` | 外部系统的 `order_id` ↔ 平台的 `run_id`，仅作查找 |

### 1.2 不写什么

| 不是 S46 的事实 | 归属 |
|---|---|
| 租户是谁、用户是谁、权限是什么 | **S01**。S46 只持有"某个 `api_client` 被授予了哪些 scope"这一条**授权凭证**，实际判定仍回源 S01/S02 |
| 政策判定 | **S02** |
| 运行状态、manifest、epoch | **S08** / **S21** |
| 费用、配额 | **S23** / **S22** |
| 模型调用 | **S26**。S46 不持有任何模型凭据，不能直连供应商 |
| 审计记录 | **S45** |
| 业务字段的语义校验规则 | 各业务服务。S46 只做**结构**校验（schema）与**能力**校验（scope） |

**S46 不建任何业务状态机。** 没有 `run_status`、没有 `budget_balance`、没有 `approval_state`。`GET /operations/{id}` 返回的是**转发自上游的状态快照 + 水位**，不是 S46 自己的状态。

### 1.3 一条必须坚持的原则

> **外部系统不是权威；`tenant_id` 只能由平台签名派生，永远不从请求体或 LLM 输出读取。**

这不是"校验输入"，是**结构上不可能**：

1. **`tenant_id` 不在任何外部 API 的请求体 schema 里。** 客户若要"确认自己发对了租户"，用 `x-expected-tenant` 头，S46 拿它与认证上下文比对，不一致返回 403 `TENANT_MISMATCH`，**但它不参与后续任何取数**。
2. **认证产物携带 `tenant_id`，由 S01 在验签时写入。** S46 只从 `AuthContext.tenant_id` 读，这个值只能来自 JWT 的 `tid` 声明或 API key 的绑定行，两者都不可由调用方构造。
3. **LLM/Agent 提供的任何 `tenant_id` 一律丢弃。** MCP 工具调用（`docs/13 §2`）的参数里即使出现 `tenant_id`，S46 在构造 `execution_context` 时**覆盖**为认证值，并记一条 `TENANT_PARAM_IGNORED` 审计。不报错、不采纳——报错会诱使调用方去"修参数"，覆盖则让误用无害。
4. **转发的每个内部调用都重新签 `execution_context`。** 不是透传外部 header。下游拿到的 `execution_context` 由 S46 用平台密钥签名，外部无法伪造，也无法重放到别的租户。

### 1.4 一句话说清 S46 与网关的关系

`api-gateway`（docs/16 §2 的 `apps/api-gateway`）做的是**路由、TLS、粗粒度限流**。S46 做的是**身份→租户绑定的解析、scope 判定、命令映射、幂等、回执、Webhook**。

两者都限流，但目的不同：网关保护边缘，S46 保护**份额**（E35 的 noisy neighbor）。S46 的限流必须是**按租户、按 scope、按端点族**的三维令牌桶，因为"一个客户用 100 个 import 任务打爆队列"和"一个客户用 100 个 generate 打爆付费"是两种完全不同的伤害。

---

## 2. 输入契约

### 2.1 认证：三种凭据，一条派生路径

| 凭据 | 场景 | 携带 `tenant_id` 的方式 | 轮换 |
|---|---|---|---|
| **API key** | 服务端到服务端 | `api_key` 行绑定的 `tenant_id`（数据库唯一真值） | 双活期，§3.4 |
| **OAuth2 access token**（企业 SSO 直连场景） | 用户在第三方应用里授权 | JWT `tid` 声明，由 S01 签发与验签 | 短期，S01 负责 |
| **MCP client** | Agent 编排 | 同 API key，`principal_type='mcp_client'` | 同 API key |

**派生规则只有一条**：

```text
AuthContext = verify(credential)
  → { tenant_id, principal_id, principal_type, scopes[], delegation_id?, home_region, exp }
```

`verify()` 的实现：API key 走"哈希查表"，JWT 走"验签 + `kid` 查表"。两者都不读请求体。

### 2.2 命令入口 `POST /integrations/commands`

**一个端点，多种命令。** 不按业务动词拆路由，因为外部系统最需要的是"一个稳定的入口 + 一个稳定的错误模型"。命令种类由 `command` 字段区分：

```jsonc
{
  "command_id": "ext_01J8...",          // 客户端生成，幂等键
  "request_hash": "9f2c…",              // 可选；缺省由服务端按 JCS 计算
  "command": "episode.generate",        // 见 §2.3 的命令目录
  "target": { "project_id": "prj_ep", "episode_id": "ep_03" },
  "params": { "generation_mode": "auto", "shot_ids": ["shot_3_2"] },
  "context_ref": { "manifest_revision": 13, "branch_id": "br_main" },
  "callback": { "subscription_id": "sub_01J..." },   // 可选，见 §4.4
  "client_trace_id": "trace-abc"
}
```

硬规则：

- **没有 `tenant_id` 字段。** 也没有 `actor`。两者都来自 `AuthContext`。
- `target` 里的 id 必须在认证租户内存在。不存在时返回 **404**（不是 403），不泄漏跨租户存在性（E28）。
- `params` 的 schema 由 `command` 决定，S46 **只做结构校验**，语义校验由下游做。S46 拒绝了不该拒绝的，比放过一个好查；但也不要在此重复下游的规则，否则两边会漂移。
- `command_id` 必须由客户端生成。S46 不为客户端生成——那会让客户端无法安全重试。
- `request_hash` 客户端可不填；服务端总能算。客户端若填了，服务端**校验它**（防客户端用自己的哈希算法导致幂等失效），不一致 400 `REQUEST_HASH_MISMATCH`。

### 2.3 命令目录（外部可见）

只有登记在册的命令能被外部调用。目录是**白名单**，未登记一律 404 `COMMAND_NOT_SUPPORTED`——不是 403，因为对外部调用方而言"这个动词不存在"比"你没权限"更少泄漏信息，且更符合"我们没开放这个 API"的实际语义。

| 外部命令 | 映射到 | 所需 scope | 是否计费 | 上游 |
|---|---|---|---|---|
| `episode.generate` | S21 `POST /runs` | `execute` | 是（S26 准入） | S21 → S26 |
| `run.get` | S21 `GET /runs/{id}` | `read` | 否 | S21 |
| `run.command` | S21 `POST /runs/{id}:pause|cancel|resume` | `execute` | 否（但会触发结算） | S21 |
| `change.plan` | S07 `POST /change-plans` | `plan` | 否 | S07 |
| `change.apply` | S07 `POST /change-plans/{id}:apply` | `execute` + 人工放行（S40） | 是 | S07 |
| `review.decide` | S40 `POST /decisions` | `review` | 否 | S40 |
| `delivery.prepare` | S43 | `execute` | 否 | S43 |
| `delivery.publish` | S43 | `execute` + S40 | 否 | S43 |
| `memory.query` | S16 | `read` | 否 | S16 |
| `context.preview` | S18 | `read` | 否 | S18 |
| `policy.get` | S02 | `read` | 否 | S02 |
| `cost.query` | S23/S44 | `read` | 否 | S23 / S44 |

**`read` scope 永远不能触发付费。** 这是 docs/13 §2 的硬约束（"readOnlyHint 等仅 UI 提示，强制授权在服务端"）。实现上不是靠"read 命令列表里没有付费动词"，而是靠**服务端在转发前检查这一次调用会产生几个 S26 准入**：若 `scope` 不含 `execute` 而调用链会走到 S26，直接拒绝，无论命令名是什么。这条检查在 `command` 目录的注册表里以 `requires_admission: true|false` 标注，注册表是**代码常量**，不是数据库配置（防止运维改错）。

### 2.4 Webhook 订阅入口

```jsonc
POST /webhook-subscriptions
{
  "url": "https://customer.example/hooks/drama",
  "event_types": ["orchestration.run.state-changed", "delivery.package.sealed"],
  "secret_ref": "whsec_...",            // 客户端生成，S46 只存 KMS 引用
  "active": true,
  "description": "生产回调"
}
```

- `url` 必须是 **HTTPS** 且**不是私网地址**（E29 SSRF）。校验在创建时与每次投递前各做一次——DNS 可以改，创建时校验不够。
- 每个租户的订阅数上限由 `plan` 决定（默认 20）。没有上限的 Webhook 是 DoS 放大器。
- `event_types` 只能是注册过的类型（`event-topics.v2.json` 的子集 + S46 自己的 `integration.command.accepted`）。

---

## 3. 核心逻辑与状态机

### 3.1 命令受理路径

```text
POST /integrations/commands
  │
  ├─1. verify(credential) → AuthContext           (无 → 401)
  ├─2. x-expected-tenant 一致性检查                 (不符 → 403 TENANT_MISMATCH)
  ├─3. 三维限流（tenant × scope × endpoint_family） (超 → 429 + Retry-After)
  ├─4. command 白名单查找                          (无 → 404 COMMAND_NOT_SUPPORTED)
  ├─5. request_hash 校验（若客户端提供）            (不符 → 400)
  ├─6. scope 判定 + requires_admission × scope     (不足 → 403 INSUFFICIENT_SCOPE)
  ├─7. 幂等预检 (tenant_id, command_id)             (异 hash → 409)
  │
  ├─8. BEGIN
  │     INSERT integration_command_receipt(ACCEPTED, request_hash, command, target)
  │     INSERT outbox(integration.command.accepted)      ← 同一事务
  │   COMMIT
  │
  ├─9. 转发上游（异步，不在事务里）
  │     · 构造 execution_context（平台签名，含 command_id/request_hash）
  │     · 调用 S21/S07/S40/… 的对应端点
  │     · 记录 upstream_ref / operation_id / run_id
  │
  └─10. 202 { operation_id, receipt_id, poll_url, accepted_at }
```

**关键取舍：先落回执、再转发。** 为什么不在转发成功后才写回执？

因为外部调用方拿到 202 之后会开始轮询。如果回执是转发后写的，那么"转发超时但实际成功"会变成"客户端重试 → 平台重复受理"。而先写回执 + 幂等键，重试得到同一个 `operation_id`。这与 S08/S26 的"先记录意图再执行"是同一模式。

代价：回执可能是 `ACCEPTED` 但转发最终失败。因此回执有 `state`：`ACCEPTED → FORWARDED | FORWARD_FAILED`，客户端通过轮询看到 `FORWARD_FAILED` 后按 §7.3 处理。**`ACCEPTED` 不等于"业务已受理"**，这一点在 API 文档里必须明说。

### 3.2 回执状态机

```text
   ACCEPTED ──(上游 2xx)──────────────► FORWARDED
      │
      ├──(上游 4xx 且明确未执行)──────► REJECTED
      │
      ├──(上游 5xx/超时，可重试)───────► FORWARD_RETRYING ──(成功)──► FORWARDED
      │                                        └──(超次数)──► FORWARD_UNKNOWN
      │
      └──(上游 409 明确未执行)─────────► REJECTED
```

`FORWARD_UNKNOWN` 是最重要的一档：**转发结果未知，不等于失败**。这与 S26 的 `SUBMISSION_UNKNOWN` 同一纪律（AGENTS.md 第 19 行禁令）。

| 状态 | 客户端看到 | S46 后续动作 |
|---|---|---|
| `ACCEPTED` | 202 / 轮询中 | 正在转发 |
| `FORWARDED` | 202 + `upstream_ref` | 停止重试；客户端改轮询 `upstream_ref` 对应的资源 |
| `REJECTED` | 4xx（原样映射上游语义） | 终态。**同 `command_id` 重试返回同一个 REJECTED**，客户端必须用新 `command_id` |
| `FORWARD_RETRYING` | 202 / 轮询中 | 有界退避重试，只在**上游明确未执行**时重试 |
| `FORWARD_UNKNOWN` | 202 + `status: "unknown"` + `next_action: "reconcile"` | 停止自动重试，等对账作业（§3.3） |

**`FORWARD_UNKNOWN` 绝不自动重提。** 这是 AGENTS.md 禁令在 S46 的落点：上游可能是 S21（会走到 S26 付费），盲目重提就是重复付费。

### 3.3 对账（`FORWARD_UNKNOWN` 的唯一出路）

```text
每 30s：
  SELECT receipt_id, upstream_ref, command, target, accepted_at
    FROM integration_command_receipt
   WHERE state = 'FORWARD_UNKNOWN'
     AND accepted_at > now() - interval '7 days'
   ORDER BY accepted_at LIMIT 100
   FOR UPDATE SKIP LOCKED;

  对每条：用 upstream_ref 或 (tenant, command_id) 回问上游"这条业务命令存在吗"
    · 存在 → FORWARDED
    · 不存在且上游确定未执行 → 允许**一次**重提（新 upstream command_id，同一 receipt）
    · 上游也说不清 → 保持 UNKNOWN，升级人工（不回退成失败）
```

7 天窗口：超过 7 天仍 UNKNOWN 的回执进 `STALE_UNKNOWN`，只读展示，不再自动处理。这不是放弃——是承认"外部调用方也需要自己的对账"，并把责任边界写清。

### 3.4 API key 轮换

```text
   ACTIVE ──(创建新 key)──► ACTIVE(新) + RETIRING(旧)
                                │
                                └──(overlap 窗口结束 / 或显式吊销)──► REVOKED
```

规则：

1. **同时最多 2 个 `ACTIVE`，1 个 `RETIRING`。** 超过 `MAX_KEYS_ACTIVE`（3）时创建新 key 返回 409 `KEY_LIMIT_REACHED`。
2. **`RETIRING` 的 key 有 `retire_at`（默认创建后 14 天），到期自动变 `REVOKED`。** 不设无限期 overlap。
3. **轮换不改变 `tenant_id`、`principal_id`、`scopes`。** key 只是凭据，权限在 `client_scope_grant` 上。这一点很重要：如果 scope 挂在 key 上，轮换就会意外改变权限。
4. **轮换立即让旧 key 的所有活跃会话失效吗？** 不。见 4 的取舍：`RETIRING` 期间旧 key 仍可用，这正是"零停机轮换"的意义。若客户要求立即吊销，用 `DELETE /api-keys/{id}`（`state='REVOKED'`，立即生效）。
5. **key 明文只在创建响应里出现一次。** 存储的是 `sha256(key)` + `kid` 前缀（用于查表）。**永不可再读回**。
6. **创建/轮换/吊销都写 S45 审计**（`action='integration.key.rotate'`）。`details` 里只有 `kid_prefix`，不含任何 key 片段。

E30 相关：S46 的 key 是**平台签发给客户的**，不是客户的 BYOK 供应商密钥。BYOK 归 S24/S26 管（§10.4）。两者绝不能混——S46 的 key 表里不得出现任何供应商凭据。

### 3.5 Webhook 投递

```text
   PENDING ──► DELIVERING ──(2xx)──► DELIVERED
                  │
                  ├──(非 2xx / 超时)──► PENDING（退避重排）
                  │                      └──(超过 24h 或 8 次)──► FAILED
                  │
                  └──(订阅被删/停用)──► CANCELLED
```

**退避**：`30s, 2m, 10m, 30m, 1h, 3h, 6h, 12h`（8 次，总计约 24h）。抖动 ±20%。

**签名**（`X-Drama-Signature`）：

```text
signed_payload = f"{timestamp}.{raw_body}"
signature = "v1=" + hex(hmac_sha256(secret, signed_payload))
header: X-Drama-Signature: t=<timestamp>,v1=<sig>
```

三条硬规则：

1. **签名基于原始字节，不是重新序列化的 JSON。** 否则对方按自己的 JSON 库重算会不一致。
2. **timestamp 超过 5 分钟 → 接收方应拒绝**（防重放）；我们在文档里写明。S46 侧不做这判断，它是对收方的义务。
3. **每个订阅一个 secret，不复用。** 泄露一个不影响其他订阅。

**去重**：`X-Drama-Delivery-Id` = `<subscription_id>:<event_id>:<attempt>`. 收方按 `(subscription_id, event_id)` 去重即可。**我们保证至少一次，不保证只一次**（docs/04 §4）。文档必须明说。

**投递顺序**：同一订阅、同一 `aggregate_id` 的事件**串行投递**（`FOR UPDATE SKIP LOCKED` 按 `partition_key` 取，见 §6.4）。不同 `aggregate_id` 并发。这保证"同一 run 的状态更新不会乱序到达客户"——但**不保证**跨 run 的顺序，因为那本来就没有语义。

### 3.6 限流（E35 的核心）

三维桶，每维独立：

| 维度 | key | 默认（`standard` plan） | 超限行为 |
|---|---|---|---|
| 租户总 QPS | `tenant_id` | 50 | 429 `TENANT_RATE_LIMITED` |
| scope 并发长任务 | `tenant_id:scope` | `execute`: 10；`plan`: 20；`read`: 50 | 429 `SCOPE_CONCURRENCY_LIMITED` |
| 端点族 QPS | `tenant_id:family`（`command`/`query`/`webhook_admin`） | 10 / 40 / 5 | 429 `ENDPOINT_FAMILY_LIMITED` |

**令牌桶在 Redis，但 Redis 只是"快速拒绝"，不是"快速放行"**（与 S22 同一纪律）：

```text
try_consume(redis)        → 允许 → 仍要在下游做真实配额检查
                          → 拒绝 → 立即 429（可能是误拒，可重试）
Redis 不可用              → **放行**（fail-open for rate limiting only）
                          → 真实配额仍由 S22/S23 把关
```

**限流的 fail-open 与授权的 fail-closed 方向相反，这是故意的**：限流是可用性保护，授权是安全边界。Redis 挂掉时若限流 fail-closed，会把整个外部接入打死；而放行的后果只是上游要多扛一点，且真正的付费闸门在 S22/S23。

**Noisy neighbor 隔离**（E35）：

- 每个租户的桶是独立的，一个租户打满不影响其他租户的桶值。
- **但共享资源（上游连接池、DB 连接）需要第二道闸**：S46 的每个上游调用有独立的 `tenant_id` 绑定的连接池配额；某个租户占满自己的配额后排队，不占别人的。
- **`plan` 分级**：`standard` / `enterprise` / `dedicated` 三档 QPS 上限不同。`dedicated` 有独立 Deployment（`service.yaml` scaling 提到"API与MCP独立Deployment"）。

### 3.7 外部 ID 映射

```text
POST 时若 body 带 external_id（可选），S46 记 (tenant, external_system, external_id) → internal_id
```

- 映射只用于**查找便利**，不用于授权。查到一条映射不等于有权限——权限永远来自 `AuthContext`。
- 同一 `external_id` 在同一租户下映射到不同内部 id → 409 `EXTERNAL_ID_CONFLICT`，不覆盖。
- `external_system` 由 `api_client` 的 `client_name` 派生，不由请求体自报。

---

## 4. 输出契约

### 4.1 命令端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/integrations/commands` | 受理命令，202 + `operation_id` |
| GET | `/operations/{operation_id}` | 轮询回执 + 上游状态快照 + 水位 |
| GET | `/operations?state=&command=&from=&to=&cursor=` | 列表 |
| POST | `/operations/{id}:cancel` | 仅对 `FORWARD_RETRYING` 有效；`FORWARDED` 后取消走上游 |

### 4.2 `GET /operations/{operation_id}` 响应

```jsonc
{
  "operation_id": "op_01J...",
  "receipt_id": "rcpt_01J...",
  "command": "episode.generate",
  "state": "FORWARDED",                  // 回执状态，见 §3.2
  "accepted_at": "2026-10-30T08:00:00Z",
  "forwarded_at": "2026-10-30T08:00:01Z",

  "upstream": {
    "service_id": "S21",
    "ref": { "id": "run_01H...", "revision": 3 },
    "state": "RUNNING",
    "state_as_of": "2026-10-30T08:05:12Z",     // 必填：上游快照的水位
    "projection_watermark": 88213
  },

  "tenant_id_derived": "t_demo",          // 显式告诉客户端"平台用的就是你"
  "client_trace_id": "trace-abc"
}
```

`upstream.state_as_of` 必填（docs/04 §5 的要求："每个投影返回 watermark 与 source_revision"）。缺了它就是让客户端把旧状态当成实时状态。

### 4.3 Webhook 订阅端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/webhook-subscriptions` | 创建 |
| GET | `/webhook-subscriptions` / `/{id}` | 查询（**不回显 secret**） |
| PATCH | `/webhook-subscriptions/{id}` | 改 url/event_types/active，**不能改 secret**（改 secret = 新建） |
| DELETE | `/webhook-subscriptions/{id}` | 停用并取消未投递 |
| POST | `/webhook-subscriptions/{id}:rotate-secret` | 轮换 secret（同 key 轮换的双活期语义） |
| POST | `/webhook-subscriptions/{id}:test` | 发一条测试事件 |
| GET | `/webhook-deliveries?subscription_id=&state=` | 投递记录（**不含**已投递的 payload 全文，只含 `event_id` + `response_code`） |

### 4.4 API key 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api-keys` | 创建，响应里**唯一一次**含明文 |
| GET | `/api-keys` | 列表（`kid_prefix`、`state`、`created_at`、`retire_at`、`last_used_at`） |
| DELETE | `/api-keys/{id}` | 立即吊销（`REVOKED`） |
| POST | `/api-keys/{id}:retire` | 转 `RETIRING`，`retire_at` 生效 |

### 4.5 发布事件

`integration.command.accepted`（topic `drama.developer-integration.v2`，`aggregate_id = operation_id`）：

```jsonc
{
  "specversion": "1.0", "schema_version": "2.0",
  "id": "evt_...", "source": "S46", "type": "integration.command.accepted",
  "subject": "integration-command/op_01J...",
  "time": "2026-10-30T08:00:00Z",
  "tenant_id": "t_demo", "home_cell": "cell_a1",
  "aggregate_id": "op_01J...", "aggregate_sequence": 1,
  "correlation_id": "ext_01J8...", "causation_id": null,
  "traceparent": "00-...-...-01",
  "data": {
    "operation_id": "op_01J...",
    "receipt_id": "rcpt_01J...",
    "command": "episode.generate",
    "state": "ACCEPTED",
    "target": { "project_id": "prj_ep", "episode_id": "ep_03" },
    "api_client_id": "clic_01J...",
    "principal_type": "api_client",
    "client_trace_id": "trace-abc"
  },
  "data_ref": "integration-command/op_01J..."
}
```

**不含**：params 全文（可能含剧情细节）、外部 URL 签名、任何凭据。消费方（S45 审计、S44 统计）需要细节时用 `data_ref` 回查。

**只发 `accepted`，不发状态变更。** 上游状态变更由各上游自己的事件表达（S21 的 `orchestration.run.state-changed`）。S46 若再发一份 `command.completed`，就变成第二个状态源（§1.2）。

### 4.6 错误模型

外部 API 的错误模型必须稳定且可编程——客户会写 `if (code === 'X')`。

| HTTP | code | retryable | 含义 |
|---|---|---|---|
| 400 | `MALFORMED_REQUEST` | false | schema 不符 |
| 400 | `REQUEST_HASH_MISMATCH` | false | 客户端算的哈希与服务端不符 |
| 401 | `UNAUTHENTICATED` | false | 无凭据/凭据无效 |
| 401 | `CREDENTIAL_EXPIRED` | false | 凭据过期，需换新的 |
| 403 | `TENANT_MISMATCH` | false | `x-expected-tenant` 与认证租户不符 |
| 403 | `INSUFFICIENT_SCOPE` | false | scope 不足；**响应含 `required_scope`** |
| 404 | `COMMAND_NOT_SUPPORTED` | false | 不在白名单 |
| 404 | `NOT_FOUND` | false | 目标不存在，**含跨租户**（E28） |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同 `command_id` 异 hash |
| 409 | `COMMAND_IN_PROGRESS` | true | 同键同 hash 正在转发 |
| 409 | `KEY_LIMIT_REACHED` | false | key 数超限 |
| 409 | `EXTERNAL_ID_CONFLICT` | false | 外部 id 已映射到别的内部 id |
| 422 | `COMMAND_NOT_FEASIBLE` | false | 上游明确说这个请求本身不可行（不是暂时失败） |
| 429 | `TENANT_RATE_LIMITED` | true | 带 `Retry-After` 与 `X-RateLimit-*` |
| 429 | `SCOPE_CONCURRENCY_LIMITED` | true | 同上 |
| 429 | `ENDPOINT_FAMILY_LIMITED` | true | 同上 |
| 502 | `UPSTREAM_UNAVAILABLE` | true | 上游不可达，回执进 `FORWARD_RETRYING` |
| 503 | `UPSTREAM_UNKNOWN` | true | 上游结果未知，回执进 `FORWARD_UNKNOWN`，**不要换个 command_id 重试** |

最后两条的区别至关重要，要在文档里用粗体写：`503 UPSTREAM_UNKNOWN` 意味着"可能已经发生了"，用新 `command_id` 重试会造成重复付费。正确动作是轮询同一个 `operation_id`。

---

## 5. 数据模型

数据库 `developer_integration`。

### 5.1 客户端与凭据

```sql
CREATE TABLE api_client (
  api_client_id   text PRIMARY KEY,          -- 'clic_01J...'
  tenant_id       text NOT NULL,
  name            text NOT NULL,
  plan            text NOT NULL DEFAULT 'standard'
    CHECK (plan IN ('standard','enterprise','dedicated')),
  state           text NOT NULL DEFAULT 'ACTIVE'
    CHECK (state IN ('ACTIVE','SUSPENDED','DELETED')),
  contact_email   text,
  created_by      jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, name)
);
CREATE INDEX ac_tenant_idx ON api_client (tenant_id, state);

CREATE TABLE api_key (
  kid             text PRIMARY KEY,          -- 'key_01J...'，公开前缀
  api_client_id   text NOT NULL REFERENCES api_client(api_client_id),
  tenant_id       text NOT NULL,             -- 冗余，便于强制租户谓词
  secret_hash     char(64) NOT NULL,         -- sha256(明文)，明文永不存储
  kid_prefix      text NOT NULL,             -- 前 8 字符，用于日志与 UI
  state           text NOT NULL DEFAULT 'ACTIVE'
    CHECK (state IN ('ACTIVE','RETIRING','REVOKED')),
  scopes_snapshot jsonb NOT NULL,            -- 创建时的 scope 快照，仅作展示
  created_at      timestamptz NOT NULL DEFAULT now(),
  created_by      jsonb NOT NULL,
  retire_at       timestamptz,
  revoked_at      timestamptz,
  last_used_at    timestamptz,
  CONSTRAINT ak_retiring_has_time CHECK (state <> 'RETIRING' OR retire_at IS NOT NULL)
);
CREATE INDEX ak_client_idx  ON api_key (api_client_id, state);
CREATE INDEX ak_tenant_idx  ON api_key (tenant_id, state);
CREATE UNIQUE INDEX ak_hash_uniq ON api_key (secret_hash);

CREATE TABLE client_scope_grant (
  api_client_id   text NOT NULL REFERENCES api_client(api_client_id),
  scope           text NOT NULL
    CHECK (scope IN ('read','plan','execute','review','admin')),
  granted_by      jsonb NOT NULL,
  granted_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz,
  revoked_at      timestamptz,
  PRIMARY KEY (api_client_id, scope)
);
```

`api_key.scopes_snapshot` 与 `client_scope_grant` 的关系必须说清：**授权真值在 `client_scope_grant`**，快照只是"创建 key 时客户以为的权限"，用于客服解释"为什么我的 key 权限变了"。判定时读 `client_scope_grant`，且每次判定都回源 S01 的授权上下文（§10.2）。

### 5.2 命令回执

```sql
CREATE TABLE integration_command_receipt (
  receipt_id      text PRIMARY KEY,          -- 'rcpt_01J...'
  tenant_id       text NOT NULL,
  operation_id    text NOT NULL,             -- 对外 id，'op_01J...'
  command_id      text NOT NULL,             -- 客户端提供，幂等键
  request_hash    char(64) NOT NULL,

  api_client_id   text NOT NULL,
  kid             text NOT NULL,
  principal_type  text NOT NULL CHECK (principal_type IN ('api_client','mcp_client')),
  delegation_id   text,

  command         text NOT NULL,
  target          jsonb NOT NULL,
  params_sha256   char(64) NOT NULL,         -- params 只存摘要，不存正文
  params_bytes    integer NOT NULL,          -- 大小，用于容量与限额

  state           text NOT NULL
    CHECK (state IN ('ACCEPTED','FORWARD_RETRYING','FORWARDED',
                     'REJECTED','FORWARD_UNKNOWN','STALE_UNKNOWN','CANCELLED')),
  attempt_count   smallint NOT NULL DEFAULT 0,
  upstream_service text,
  upstream_ref    jsonb,                     -- {id, revision}
  upstream_state  text,                      -- 最近一次快照
  upstream_state_as_of timestamptz,
  rejection       jsonb,                     -- {code, message, upstream_service}
  client_trace_id text,

  accepted_at     timestamptz NOT NULL DEFAULT now(),
  forwarded_at    timestamptz,
  last_attempt_at timestamptz,
  next_attempt_at timestamptz,

  CONSTRAINT icr_op_uniq UNIQUE (tenant_id, operation_id),
  CONSTRAINT icr_cmd_uniq UNIQUE (tenant_id, command_id)
);
CREATE INDEX icr_state_idx ON integration_command_receipt (state, next_attempt_at)
  WHERE state IN ('ACCEPTED','FORWARD_RETRYING','FORWARD_UNKNOWN');
CREATE INDEX icr_tenant_idx ON integration_command_receipt (tenant_id, accepted_at DESC);
CREATE INDEX icr_upstream_idx ON integration_command_receipt (tenant_id, upstream_service, forwarded_at)
  WHERE upstream_ref IS NOT NULL;
```

`poll_receipts_idx` 是给对账用的；`icr_cmd_uniq` 是幂等的数据库级保证——**不是靠应用判断短路，而是靠唯一约束**。

`params` 只存 `sha256` 与字节数，这是刻意的：外部调用的 params 里可能有剧集剧情、台词草稿。S46 不是它们的权威（S04/S03 是），存全文会造成第二份内容副本，且扩大 E31 的驻留范围。需要重放时，`forwarded` 的 `upstream_ref` 指向真正的资源。

### 5.3 Webhook

```sql
CREATE TABLE webhook_subscription (
  subscription_id text PRIMARY KEY,          -- 'sub_01J...'
  tenant_id       text NOT NULL,
  api_client_id   text,                      -- NULL = 租户级（控制台创建）
  url             text NOT NULL,
  event_types     text[] NOT NULL,
  secret_ref      text NOT NULL,             -- KMS 引用；secret 明文不在库中
  state           text NOT NULL DEFAULT 'ACTIVE'
    CHECK (state IN ('ACTIVE','SUSPENDED','DELETED')),
  description     text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  rotated_at      timestamptz,
  previous_secret_ref text,                  -- 轮换双活期
  previous_secret_expires_at timestamptz,
  CONSTRAINT ws_events_nonempty CHECK (array_length(event_types, 1) >= 1)
);
CREATE INDEX ws_tenant_idx ON webhook_subscription (tenant_id, state);

CREATE TABLE webhook_delivery (
  delivery_id     text PRIMARY KEY,          -- 'wd_01J...'
  subscription_id text NOT NULL REFERENCES webhook_subscription(subscription_id),
  tenant_id       text NOT NULL,
  event_id        text NOT NULL,
  event_type      text NOT NULL,
  partition_key   text NOT NULL,             -- tenant:aggregate_id，串行投递用
  state           text NOT NULL DEFAULT 'PENDING'
    CHECK (state IN ('PENDING','DELIVERING','DELIVERED','FAILED','CANCELLED')),
  attempt_count   smallint NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_response_code integer,
  last_error      text,
  delivered_at    timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT wd_uniq UNIQUE (subscription_id, event_id)   -- 同一事件不重复入队
);
CREATE INDEX wd_pending_idx ON webhook_delivery (next_attempt_at)
  WHERE state = 'PENDING';
CREATE INDEX wd_tenant_idx  ON webhook_delivery (tenant_id, created_at DESC);
```

`wd_uniq` 保证同一事件对同一订阅只入队一次。**入库幂等，投递至少一次**：`UNIQUE` 挡的是"事件被消费两次导致入队两次"，不挡"投递失败重试"（那由 `attempt_count` + 同一 `delivery_id` 表达）。

### 5.4 外部 ID 映射与限流配置

```sql
CREATE TABLE external_id_mapping (
  tenant_id       text NOT NULL,
  external_system text NOT NULL,             -- 由 api_client.name 派生
  external_id     text NOT NULL,
  internal_kind   text NOT NULL,             -- 'run' | 'project' | 'episode' | 'delivery'
  internal_id     text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, external_system, external_id)
);
CREATE INDEX eim_internal_idx ON external_id_mapping (tenant_id, internal_kind, internal_id);

CREATE TABLE tenant_quota_profile (
  tenant_id          text PRIMARY KEY,
  plan               text NOT NULL,
  qps_total          integer NOT NULL,
  qps_command        integer NOT NULL,
  qps_query          integer NOT NULL,
  qps_webhook_admin  integer NOT NULL,
  concurrency_execute smallint NOT NULL,
  concurrency_plan   smallint NOT NULL,
  concurrency_read   smallint NOT NULL,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  updated_by         jsonb NOT NULL
);
```

`tenant_quota_profile` 是**限流参数**，不是配额权威。真实配额（生成次数、GPU 秒）在 S22/S23。混淆这两者会导致"S46 说可以但上游说没钱"。

### 5.5 幂等、Inbox、Outbox

```sql
CREATE TABLE idempotency_record (
  tenant_id      text NOT NULL,
  command_id     text NOT NULL,
  request_hash   char(64) NOT NULL,
  command_type   text NOT NULL,     -- 'integration.command' | 'webhook.sub.create' | ...
  state          text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code  integer,
  response_body  jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE inbox (
  consumer    text NOT NULL,        -- 's46.webhook' | 's46.reconcile'
  event_id    text NOT NULL,        -- 或 '<topic>:<partition>:<offset>' 用于非事件流
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
CREATE INDEX inbox_ttl_idx ON inbox (received_at);

CREATE TABLE outbox (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id         text NOT NULL,
  aggregate_id      text NOT NULL,   -- operation_id
  aggregate_sequence bigint NOT NULL,
  event_type        text NOT NULL,
  partition_key     text NOT NULL,   -- tenant_id || ':' || aggregate_id
  payload           jsonb NOT NULL,
  headers           jsonb NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  published_at      timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;
```

`integration.command.accepted` 在 §3.1 步骤 8 的同一本地事务里写入 `outbox`，与 `integration_command_receipt` 同生共死。

### 5.6 不建的表

| 不建 | 为什么 |
|---|---|
| `run_state_cache` / `budget_cache` | 第二权威（§1.2） |
| `request_body_log` | params 含剧情正文；驻留与泄漏面（E31） |
| `provider_credential` | BYOK 归 S24/S26；S46 存供应商凭据等于绕开 S26 |
| 全局跨租户 `external_id_mapping` 唯一索引 | 不同租户可以让同一个外部 `order_id` 指向不同内部 id；跨租户唯一会泄漏（E28） |
| `webhook_payload_archive` | 客户自己负责留存；S46 只保证至少一次投递 |

---

## 6. 幂等与并发

### 6.1 幂等键

```text
request_hash = sha256(JCS(body 去掉 command_id、request_hash、client_trace_id))
```

`client_trace_id` 参与排除，因为它是观测字段，客户改它不该导致 409。这是本文的一个判断——若契约裁决要求纳入，改一行即可。

### 6.2 受理的原子性

```sql
BEGIN;
  -- 幂等预检已在此事务内重做一次（防止并发窗口）
  INSERT INTO integration_command_receipt(...) VALUES (...);
  INSERT INTO outbox(... event_type='integration.command.accepted' ...);
COMMIT;
```

`icr_cmd_uniq` 唯一约束在 `INSERT` 时兜底：并发两个相同 `command_id` 的请求，一个成功，另一个收到唯一冲突 → 查已有行 → 回放。**不依赖先查后插。**

### 6.3 转发阶段的并发

转发不在事务里（§3.1）。并发控制靠 `next_attempt_at` 与行级锁：

```sql
SELECT receipt_id, attempt_count
  FROM integration_command_receipt
 WHERE state IN ('ACCEPTED','FORWARD_RETRYING')
   AND next_attempt_at <= now()
 ORDER BY next_attempt_at
 LIMIT 50
 FOR UPDATE SKIP LOCKED;
```

`FOR UPDATE SKIP LOCKED` 与 S08 §6.4 的 relay 同构。

**同一 `receipt_id` 不会有两个 worker 同时转发**——行锁保证了。若 worker 崩溃，锁释放，回执仍是 `ACCEPTED`/`FORWARD_RETRYING`，下一轮再取。

### 6.4 Webhook 投递的串行点

同一小区（`partition_key`）的事件必须按序投递：

```sql
-- 取待投递，但只取每个 partition_key 的最早一条
SELECT DISTINCT ON (partition_key) *
  FROM webhook_delivery
 WHERE state = 'PENDING'
   AND next_attempt_at <= now()
 ORDER BY partition_key, created_at
 FOR UPDATE SKIP LOCKED
 LIMIT 100;
```

`DISTINCT ON` + `FOR UPDATE SKIP LOCKED` 组合在 Postgres 里的行为需要实测：`SKIP LOCKED` 会在被锁的行上跳过，`DISTINCT ON` 的窗口则在取到行之后。若实测发现顺序错乱（较少见但存在），退化方案是**每 partition 一个 advisory lock**，worker 取锁成功才投递——但那条路要接受 advisory lock 的 MVCC 语义问题。**建议先用行锁方案并在测试里覆盖 T46-31。**

### 6.5 限流令牌桶（Redis）

```text
key = "{tenant}:{scope}:{family}:{window_start}"
INCR + EXPIRE（滑动窗口近似）
```

三处必须写明：

1. **Redis 不可用 → 放行**（§3.6）。日志记 `RATE_LIMIT_DEGRADED`，指标告警。
2. **时钟漂移**：用 Redis `TIME` 而不是应用时钟，避免多实例窗口错位。
3. **窗口边界突发**：固定窗口允许 2× 突发。这不是 bug，是取舍（简单）；若客户抱怨，用两个重叠窗口取 max。不引入滑动日志——成本不值得。

### 6.6 并发场景清单

| 场景 | 期望 |
|---|---|
| 同 `command_id` 10 并发 | 1 个受理，9 个回放同一 `operation_id` |
| 不同 `command_id` 指向同一 ep 的 generate | 全部受理，由 S21/S26 自己串行（S46 不替上游去重） |
| 100 个租户并发 50 QPS | 各租户 429 独立，互不影响（E35） |
| 一个租户打满 `execute` 并发 | 该租户 429，其他租户正常 |
| Webhook 同 partition 5 条事件 | 严格按 `created_at` 顺序投递 |
| key 轮换期间新旧 key 同时用 | 都可用，直到 `retire_at` |
| 同 `external_id` 两个并发映射 | 1 成功，1 得 409 |

---

## 7. 失败分支

### 7.1 入口层

| 场景 | 行为 | 对外 |
|---|---|---|
| 凭据无效 | 401 | 不区分"不存在"与"过期"的信息量？**区分**：`UNAUTHENTICATED` vs `CREDENTIAL_EXPIRED`，因为客户需要知道"换 key 就行" |
| `x-expected-tenant` 不符 | 403 `TENANT_MISMATCH` | 不含任何租户标识 |
| 限流 | 429 + `Retry-After` + `X-RateLimit-Limit/Remaining/Reset` | 客户端可编程退避 |
| Redis 不可用 | 放行 + 告警 | 客户无感 |
| S01 授权服务不可达 | **503**（fail closed） | 不接受命令。授权不可用不能放行（与限流相反，§3.6） |

### 7.2 上游转发

| 上游响应 | S46 行为 | 回执状态 |
|---|---|---|
| 2xx | 记录 `upstream_ref` | `FORWARDED` |
| 400 / 422（请求本身不可行） | 原样映射，`rejection` 存上游 code | `REJECTED` |
| 401 / 403（S46 自己的凭据出问题） | 记录 `UPSTREAM_AUTH_FAILED`，**不重试**，告警 | `REJECTED` |
| 404（目标不存在） | 映射为 404 | `REJECTED` |
| 409（上游明确说状态冲突且未执行） | 映射为 422 `COMMAND_NOT_FEASIBLE` | `REJECTED` |
| 429 / 5xx / 超时 | 有界退避重试（最多 5 次，总计 ≤ 2 分钟） | `FORWARD_RETRYING` |
| 重试用尽 或 超时无法判定 | 停止 | `FORWARD_UNKNOWN` |

**关键区分**：`503 UPSTREAM_UNKNOWN` 与 `502 UPSTREAM_UNAVAILABLE` 必须是两个不同的 code。前者回执进 `FORWARD_UNKNOWN`（不自动重提），后者进 `FORWARD_RETRYING`（自动重试）。把两者混成一个 5xx 是这类系统最常见的重复付费来源。

### 7.3 客户端在 `FORWARD_UNKNOWN` 之后该做什么

写进 API 文档：

1. **不要**换新的 `command_id` 重试。
2. 轮询同一个 `operation_id`，直到 `state` 变成 `FORWARDED`（成功）或 `REJECTED`（未执行）。对账作业会在 7 天内给出结论。
3. 若要紧急处置，用 `run.command: cancel`（如果上游已有 run）。

这条在文档里用加粗 + 代码示例，因为它是唯一会造成真实损失的误用路径。

### 7.4 Webhook 失败

| 场景 | 行为 |
|---|---|
| 客户返回 5xx | 重试（§3.5 退避） |
| 客户返回 4xx | **仍重试**（4xx 可能是对方临时 bug）；8 次后 `FAILED` |
| DNS 解析到私网地址 | 拒绝投递，`FAILED`，告警（SSRF 防护，E29） |
| TLS 握手失败 | 重试；连续失败 3 次告警（可能是证书过期） |
| 客户 URL 不存在的 404 | 重试；8 次后 `FAILED` |
| 8 次后 | `FAILED`，保留记录，**不回滚业务**。Webhook 是通知，不是事务 |
| 客户 24h 后才发现 | 用 `GET /webhook-deliveries` 查 `FAILED` 记录，用 S21 的查询接口回补 |

**Webhook 失败绝不影响业务。** 这一点必须显式：S46 是通知通道，不是业务链路的一环。

### 7.5 崩溃与重启

| 崩溃点 | 后果 | 恢复 |
|---|---|---|
| 受理事务 COMMIT 前 | 无回执 | 客户端重试，幂等 |
| COMMIT 后、转发前 | `ACCEPTED`，未转发 | 轮询 job 扫到，转发 |
| 转发成功、写回执前 | 回执仍 `ACCEPTED` | **危险**：下次转发会重复。缓解：转发时传同一个上游 `command_id`（用 receipt 的），上游幂等接收 |
| `FORWARD_UNKNOWN` 的 worker 崩溃 | 状态不变 | 对账 job 重新取 |
| Webhook DELIVERING 中崩溃 | 停在 DELIVERING | janitor 5 分钟后重置为 PENDING（`attempt_count` 不增，因为它可能已送达 → 收方按 delivery_id 去重） |
| key 轮换中途崩溃 | 新 key 已建、旧 key 未转 RETIRING | 双活可接受，客户两条都能用；janitor 补转 |

第三行是这类系统的固有窗口。正确做法：**转发请求携带 `Idempotency-Key: <receipt.command_id>`**，让上游按自己的幂等记录去重。这不是回到"信任上游"，而是利用上游已有的幂等能力。

### 7.6 失败矩阵

| # | 场景 | 检测 | 行为 |
|---|---|---|---|
| F46-01 | 凭据泄露 | 客户报障/异常用量 | 立即 `REVOKED`，审计，扫描该 key 的历史调用 |
| F46-02 | key 轮换遗漏 | `RETIRING` 超 `retire_at` | 自动 `REVOKED`（janitor） |
| F46-03 | 上游持续 5xx | 退避 5 次 | `FORWARD_UNKNOWN` + 告警 |
| F46-04 | 上游认证失败 | 401/403 | `REJECTED` + P2 告警（S46 的凭据配置错） |
| F46-05 | Webhook 目标 SSRF | DNS 解析 | 拒绝 + P2 告警 |
| F46-06 | 单租户打满 | 限流计数 | 429，不影响他人 |
| F46-07 | Redis 挂 | 连接失败 | 放行，告警 |
| F46-08 | S01 挂 | 授权失败 | 503，fail closed |
| F46-09 | 回执暴涨（客户失控） | 回执表增长 | 告警；不自动限其账户，客户可自行暂停 |

---

## 8. 可观测

### 8.1 指标

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `s46_requests_total` | counter | `command`, `outcome`(2xx/4xx/5xx), `plan` | 总量与错误率 |
| `s46_request_duration_seconds` | histogram | `command` | 受理 P95/P99 |
| `s46_auth_failures_total` | counter | `reason`(invalid/expired/tenant_mismatch) | 凭据问题 |
| `s46_rate_limited_total` | counter | `dimension`(tenant/scope/family), `plan` | E35 的主要观测点 |
| `s46_rate_limit_degraded_total` | counter | — | Redis 不可用降级次数 |
| `s46_scope_denied_total` | counter | `required_scope` | 客户在试超出权限的调用 |
| `s46_forward_total` | counter | `upstream_service`, `state` | 转发结果分布 |
| `s46_forward_latency_seconds` | histogram | `upstream_service` | 上游速度 |
| `s46_forward_unknown_total` | counter | `upstream_service` | **最重要的一条**：未知转发 |
| `s46_forward_unknown_age_seconds` | histogram | — | 未知转发的处置延迟 |
| `s46_reconcile_resolved_total` | counter | `resolution`(forwarded/rejected/retry) | 对账成效 |
| `s46_reconcile_stale_total` | counter | — | 超过 7 天未决 |
| `s46_webhook_pending_count` | gauge | `plan` | 积压 |
| `s46_webhook_delivery_total` | counter | `event_type`, `result` | 投递质量 |
| `s46_webhook_failed_total` | counter | `reason`(dns/tls/5xx/4xx) | 客户侧问题 |
| `s46_webhook_latency_seconds` | histogram | — | 从事件到投递完成 |
| `s46_api_keys_by_state` | gauge | `state` | 凭据健康 |
| `s46_idempotent_replays_total` | counter | `command` | 客户端重试行为 |
| `s46_tenant_isolation_violations_total` | counter | — | **必须恒为 0** |

**不进 label**：`tenant_id`、`api_client_id`、`url`（含客户域名）。这些进 trace 与日志。

### 8.2 追踪

```text
s46.http.request
  ├─ s46.auth
  ├─ s46.ratelimit
  ├─ s46.receipt.write (DB tx)
  └─ s46.forward (异步；独立 trace，父 span 用 link 而非 child)
       └─ <upstream span>
```

转发跨请求边界，用 **span link** 而不是 child span——因为转发发生在受理响应返回之后，属不同 trace。

`traceparent` 传给上游，让上游的 trace 能反查到外部调用。

### 8.3 审计

S46 必须写 S45 审计的动作（§10.5）：

| 动作 | 何时 |
|---|---|
| `integration.key.create` / `rotate` / `revoke` | 凭据变更 |
| `integration.scope.grant` | 权限变更 |
| `integration.command.accept` | 每条外部命令（`outcome` = 受理结果） |
| `integration.tenant_param_ignored` | LLM/外部传了 `tenant_id` 被覆盖 |
| `integration.webhook.create` / `delete` | 订阅变更 |
| `integration.forward.unknown` | 进入未知转发 |

`integration.command.accept` 的量可能很大。`details` 里只放 `command` / `params_sha256` / `params_bytes` / `target` 的引用，不含 params 正文。

### 8.4 告警

| 级别 | 条件 |
|---|---|
| P1 | `s46_tenant_isolation_violations_total > 0`（租户隔离被突破） |
| P1 | `s46_forward_unknown_total` 5 分钟 > 10 |
| P2 | `s46_auth_failures_total` 突增 3× 基线（疑似撞库/泄露） |
| P2 | Webhook `FAILED` 比例 > 20%（1h） |
| P2 | `s46_rate_limit_degraded_total > 0` 持续 5min |
| P3 | 单租户 429 比例 > 50%（1h）→ 通知客户经理 |

---

## 9. 测试清单

### 9.1 租户绑定（AGENTS.md 禁令 + E27 / E28）

| # | 用例 | 期望 |
|---|---|---|
| T46-01 | 请求体带 `tenant_id` 字段 | schema 拒绝（`additionalProperties: false`） |
| T46-02 | MCP 调用参数里带 `tenant_id`（LLM 生成） | 被覆盖为认证值；`integration.tenant_param_ignored` 审计；**不报错** |
| T46-03 | `x-expected-tenant` 与凭据不符 | 403 `TENANT_MISMATCH`，不泄漏真实租户 |
| T46-04 `[E28]` | 租户 B 用合法凭据访问租户 A 的 `run_id` | 404，响应不含 A 的任何标识 |
| T46-05 `[E28]` | 租户 B 的 `external_id` 与 A 的重名 | 各自独立解析，互不可见 |
| T46-06 `[E27]` | S01 撤权后立即调用 | 403 `INSUFFICIENT_SCOPE` |
| T46-07 `[E27]` | API key 吊销后立即调用 | 401 `UNAUTHENTICATED` |
| T46-08 | 转发给上游的 `execution_context` | `tenant_id` 与认证值一致，签名有效 |
| T46-09 | 客户端伪造 `execution_context` header | 被 S46 覆盖，下游看到的是 S46 签的 |

### 9.2 幂等（E12）

| # | 用例 | 期望 |
|---|---|---|
| T46-10 `[E12]` | 同 `command_id` 同 body 两次 | 同一 `operation_id`，回执 1 条，`replayed:true` |
| T46-11 `[E12]` | 同 `command_id` 异 body | 409 `IDEMPOTENCY_KEY_REUSED` |
| T46-12 `[E12]` | 同 `command_id` 10 并发 | 1 条回执，1 条 outbox 事件 |
| T46-13 | 客户端提供错误 `request_hash` | 400 `REQUEST_HASH_MISMATCH` |
| T46-14 | `client_trace_id` 变化 | 不影响幂等 |

### 9.3 转发与未知（E16 同源纪律）

| # | 用例 | 期望 |
|---|---|---|
| T46-15 | 上游 200 | `FORWARDED`，`upstream_ref` 有值 |
| T46-16 | 上游 422 | `REJECTED`，错误原样映射 |
| T46-17 | 上游超时 | `FORWARD_RETRYING` → 重试 → 最终 `FORWARDED` |
| T46-18 | 上游 503 无法判定 | `FORWARD_UNKNOWN`，**不自动重试** |
| T46-19 | 对账发现上游已执行 | `FORWARDED` |
| T46-20 | 对账发现上游未执行 | 允许一次重提；重提后 `FORWARDED` |
| T46-21 | 对账 7 天无结论 | `STALE_UNKNOWN`，不自动处理 |
| T46-22 | 转发成功但回执写失败（模拟） | 重试时用同 `Idempotency-Key`，上游去重 |

### 9.4 限流与隔离（E35）

| # | 用例 | 期望 |
|---|---|---|
| T46-23 `[E35]` | 单租户 200 QPS（上限 50） | 约 50% 通过 + 429，`Retry-After` 合理 |
| T46-24 `[E35]` | 租户 A 打满时租户 B 正常 | B 的延迟与错误率不变 |
| T46-25 `[E35]` | 单租户 `execute` 并发 20（上限 10） | 10 通过，10 得 429 |
| T46-26 `[E35]` | 模拟 1 小时持续负载 | SLO 不退化；上游错误率不变 |
| T46-27 | Redis 不可用 | 放行，业务成功，告警 |
| T46-28 | `read` scope 调用 `episode.generate` | 403 `INSUFFICIENT_SCOPE`，无 S26 准入 |
| T46-29 | `execute` scope 调 `run.command: pause` | 允许，不触发新付费 |

### 9.5 Webhook

| # | 用例 | 期望 |
|---|---|---|
| T46-30 | 签名可被独立验证 | 用文档里的算法复算一致 |
| T46-31 | 同 partition 5 条事件 | 严格按序送达 |
| T46-32 | 客户返回 500 8 次 | `FAILED`，业务不受影响 |
| T46-33 | URL 解析到 10.0.0.1 | 拒绝投递（SSRF，E29） |
| T46-34 | 同一事件重复消费 | 只入队一次（`wd_uniq`） |
| T46-35 | key 轮换 overlap 期 | 新旧 secret 都能验签 |
| T46-36 `[E32]` | Kafka 重放 1 小时 | 不重复入队，不重复投递 |
| T46-37 | 投递失败后客户端补查 | `GET /webhook-deliveries` 可见 |
| T46-38 | 订阅 DELETE | 未投递的进 `CANCELLED` |

### 9.6 凭据与轮换

| # | 用例 | 期望 |
|---|---|---|
| T46-39 | 创建 key | 响应含明文一次；再次 GET 无明文 |
| T46-40 | 创建第 4 个 key | 409 `KEY_LIMIT_REACHED` |
| T46-41 | 轮换后 overlap 期内旧 key | 可用 |
| T46-42 | `retire_at` 到期 | 旧 key 自动 `REVOKED` |
| T46-43 | key 表内容审查 | 无任何供应商凭据（BYOK 不在此） |
| T46-44 | 轮换/吊销 | 写 S45 审计，含 `kid_prefix`，不含 key 片段 |

### 9.7 崩溃与升级

| # | 用例 | 期望 |
|---|---|---|
| T46-45 `[E13]` | 受理事务中 kill | 无回执、无事件；重试成功 |
| T46-46 `[E13]` | 转发中 kill | 回执仍 `ACCEPTED`，恢复后转发（上游幂等接收） |
| T46-47 `[E13]` | Webhook DELIVERING 中 kill | 5 分钟后重置，收方按 delivery_id 去重 |
| T46-48 `[E01]` | 只 import 契约 SDK | 构建通过 |
| T46-49 `[E01]` | 回滚到旧版本 | 新字段可选，旧版本可读写 |
| T46-50 `[E31]` | 客户请求 API 的地域 | 只能命中自己 `home_region` 的 S46 |

---

## 10. 与上下游服务的接口约定

### 10.1 对 S01（身份）

| 用途 | 约定 |
|---|---|
| 验凭据 | API key：S46 自己哈希查表。JWT：S01 提供 `kid` 与公钥，S46 本地验签 |
| 授权上下文 | `GET /authorization-context`，带 `principal_id`；返回 `scopes`、`tenant_state`、`kill_switch` |
| 失败语义 | 不可达 → 503，**fail closed** |
| 缓存 | 只允许 ≤30s 的授权判定缓存，且按 `access.grant.changed` 失效；记录 `source_revision` |

S46 **不自建用户表、不自建角色表**。`api_client` 是 S46 的，`principal` 不是——`api_client` 在 S01 侧对应一个 `principal_id`，由 S01 在创建时分配。

### 10.2 对 S02（策略）

| 用途 | 约定 |
|---|---|
| 外部调用的 policy 判定 | 转发前用 `evaluate(policy_ref, action, context)`；`deny` → 403 |
| `policy_ref` | 每条外部命令请求都带（`context_ref` 或默认租户策略）；缺失时用租户默认策略 |
| 失败语义 | `abstain` **不放行**（与 S40 同理：`abstain` 是未知，不是通过） |

### 10.3 对 S07 / S21 / S40 / S43（命令映射目标）

| 约定 | 内容 |
|---|---|
| **一律走它们自己的对外端点** | 不直连它们的数据库，不走内部 RPC 绕过授权 |
| **`execution_context` 由 S46 签** | 含 `command_id`/`request_hash`（与外部命令相同）、`actor` 来自 `AuthContext` |
| **上游幂等** | S46 的转发带 `Idempotency-Key: <receipt.command_id>`；上游按自己的幂等记录去重 |
| **不重试不可重试的错误** | 上游 4xx 不重试 |
| **状态查询** | `GET /operations/{id}` 每次实时查上游，不缓存超过 5s |

### 10.4 对 S24 / S26（BYOK 与模型）

**S46 不碰供应商凭据。** BYOK 密钥归 S24 注册、S26 使用（E30）。S46 的 `api_key` 表里出现任何形如 `sk-`、供应商 region endpoint、组织的供应商 secret 的字段都算设计错误。

外部客户若要"用自己的模型 key"，走的是 S24 的 BYOK 流程，S46 只是一个入口 facade——它把请求转给 S24，不保存 key。**"不借其他租户 Key"（E30）在 S46 侧的表现是：S46 没有任何可借的 key。**

### 10.5 对 S45（审计）

| 动作 | 内容 |
|---|---|
| 凭据变更 | `integration.key.*` / `integration.scope.grant` |
| 外部命令 | `integration.command.accept`，含 `command`、`target`、`params_sha256`、`outcome`、`principal_type='api_client'`、`delegation_id` |
| 租户参数被忽略 | `integration.tenant_param_ignored`，含原始值（作为**证据**，不是权威） |
| 未知转发 | `integration.forward.unknown` |

审计意图通过 S46 自己的 outbox 发送（与 §3.1 步骤 8 的 outbox 是同一张表，不同 `event_type`）。

### 10.6 对 S47（实时投影）

S47 订阅 `integration.command.accepted`，用于外部客户端面板的"最近调用"列表。S47 必须显示 `accepted_at` 作为水位，不得把"最新受理"呈现为"最新完成"。

S46 **不推送 SSE 给外部客户**——外部客户的实时通知走 Webhook（§3.5），不共用 S47 的内部 SSE。

---

## 11. 契约缺口（如实报告，不自行补）

| # | 缺什么 | 影响 | 需要谁改 |
|---|---|---|---|
| **C46-1** | `openapi.v2.yaml` **没有 `/integrations/commands`、`/operations/{id}`、`/webhook-subscriptions`、`/api-keys` 中的任何一条路径**；`service.yaml` 声明了前两条 | **阻塞实现**：无契约无法生成客户端 SDK，而 S46 存在的意义正是给外部 SDK | `contracts/openapi.v2.yaml` |
| **C46-2** | 无"外部命令目录"的机器可读定义（§2.3 是本文的表格） | 客户端 SDK 无法生成类型；新增命令要改代码 | 新增 `contracts/integration-commands.v1.json` |
| **C46-3** | 无实体内 `api_client` / `api_key` 的 schema；无 `client_scope_grant` 与 S01 `principal` 的对应关系 | `principal_type='api_client'`（execution-context schema 里的 enum）在 S01 侧如何映射未定义 | `contracts/` + S01 方案 |
| **C46-4** | `docs/13 §2` 列了 MCP scopes，但没定义 "scope → 可调用命令" 的映射表 | §2.3 的 `required_scope` 是本文的判断；与 MCP 工具定义（`mcp-tools.v2.design.json`）可能不一致 | `docs/13` + `contracts/mcp-tools.v2.design.json` |
| **C46-5** | 无 Webhook 签名算法的正式定义（`X-Drama-Signature`） | 客户无法实现验签；§3.5 的格式是本文提案 | `docs/13` 补规范 + `contracts/` |
| **C46-6** | `event-topics.v2.json` 里 `drama.developer-integration.v2` 只登记了 `integration.command.accepted`；**没有命令状态流转事件** | 外部客户只能轮询；若产品要求"命令完成时推 Webhook"，需要新事件类型 | `contracts/event-topics.v2.json` |
| **C46-7** | 无限流维度与 plan 的正式定义（§3.6 是本文提案） | E35 的验收没有基准；`tenant_quota_profile` 的默认值无法与合同对齐 | `docs/03` 或 `customer-configs/` 规范 |
| **C46-8** | `service.yaml` 的 `depends_on` 是 `[S01, S02, S07, S21]`，但 §2.3 的命令目录还调 S40 / S43 / S16 / S18 / S23 / S24 | 部署依赖图不全；S46 与这些服务的版本兼容性无人负责 | catalog |
| **C46-9** | 无"外部 API 的版本策略"（`/v2/` 路径？`Accept` 头？） | 客户升级无依据；S46 的 `schema_version` 与 HTTP API 版本是两套 | `docs/13` |
| **C46-10** | `x-expected-tenant` 头的语义（docs/13 §1 提到"外部指定值只能作一致性校验"）没有 header 名与错误码 | 客户不知道该怎么"确认租户" | `docs/13` + openapi |
| **C46-11** | `integration_command_receipt.params` 不存全文是本方案的决定；若下游需要重放，需确认"回查链路"可行 | 若某命令无法从 `upstream_ref` 重建入参，"重放"能力就缺失 | 各命令目标服务确认 |

**C46-1、C46-2、C46-5 是阻塞级。**

---

## 12. 未决

1. **`ACCEPTED` 是否应当算"已受理"。** 本文取"回执已记录、转发未完成"的语义，客户端必须轮询。备选是"同步转发，只有成功才 202"——但那会让一个慢上游拖死所有外部调用，且无法覆盖 `FORWARD_UNKNOWN`。本文坚持异步，但需要在 API 文档里把"202 ≠ 已执行"讲得非常清楚。

2. **外部命令的 `params` 是否该存全文。** 本文按"只存摘要"处理（§5.2），理由是驻留与泄漏。但调试体验会差：客户报障时，客服看不到他发了什么。折中方案是"存 24 小时的密文副本"，但那是新的敏感数据类别，需要安全评审。

3. **Webhook 的 `secret` 存放方式。** 本文写 `KMS 引用`，但没定具体 KMS（云厂商、信封加密、轮换策略）。`service.yaml` 没提 KMS 依赖，这可能是 W0 平台层的东西。

4. **`plan` 分级的具体阈值。** `standard`/`enterprise`/`dedicated` 的 QPS 与并发数需要商业与容量共同定（docs/03 只有整体规划，没有分级）。

5. **MCP 与 HTTP API 是否共用限流桶。** 本文假设共用（按 `tenant_id`）。若产品认为"MCP 是内部 Agent，不该占客户的 API 配额"，则需要分开。这取决于 MCP 是卖给客户还是内部用。

6. **`FORWARD_UNKNOWN` 的 7 天窗口。** 这个数字是本文的判断。太短会让长任务（视频生成可能数小时）过早进 `STALE`；太长会让脏数据累积。需要结合 S21 的最长 run 时长确定。

7. **是否要为外部客户提供"批量查询"端点。** 目前只有单 `operation_id` 查询与列表。若客户有大量 run，逐个轮询会很痛。批量端点会放大跨租户泄漏风险，需要专门设计。

8. **`api_client` 与 S01 的 `principal` 的 1:1 还是 1:N。** 本文假设 1:1（一个 client 一个 principal）。若一个客户想给不同团队不同 API client 但共用权限，则可能是 N:1。影响授权上下文的构造。

---

## 13. 明确不做的

- **不做业务状态机。** 没有 `run_state`、没有 `budget`、没有 `approval`（§1.2）。
- **不直连模型。** 外部调用一律经 S21 → S26；S46 不持有任何供应商凭据，也不调用 S25/S27（AGENTS.md 禁令）。
- **不绕过 S23/S22 计费。** `execute` 命令转发后由 S26 走准入 Saga；S46 不做"先扣后补"。
- **不信任外部 `tenant_id`。** 结构上不存在该入参，且 LLM 提供的值被覆盖（§1.3）。
- **不做"审核已通过即放行"。** 外部触发的 `delivery.publish` 仍要 S40 的实时复查（AGENTS.md 第 21 行）。
- **不缓存上游状态超过 5s。** 不做本地状态镜像（§1.2）。
- **不让 Webhook 失败影响业务。** 通知是通知（§7.4）。
- **不做跨租户的外部 ID 全局唯一。** 那会泄漏存在性（E28）。
