# S01 · 租户与访问 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。SLO 与容量均为 [docs/03](../docs/03-deployment-capacity.md) 的规划值，**未实测**。

上游依据：[docs/01 §2/§3](../docs/01-enterprise-architecture.md)（服务独立性、唯一权威表）、[docs/04 §1/§4](../docs/04-data-consistency.md)（command envelope、事件标准、Outbox）、[docs/14 §1](../docs/14-enterprise-operations.md)（身份、权限与凭证）、[docs/13 §1/§2](../docs/13-api-product.md)（API/MCP 的授权分层）、[docs/16 §5](../docs/16-migration-acceptance.md)（E 矩阵）、[S01 规格](../docs/design-package/services/S01-tenant-access.md)。

配套：[S02](S02-governance-policy.md)（策略判定）、[S21](S21-orchestration.md)（Run 准入）、[S26](S26-generation-jobs.md)（派单）、[S46](S46-developer-integration.md)（Developer API / MCP）、[S45](S45-audit.md)（审计出口）。

---

## 0. 一句话边界

**S01 是"你是谁、你属于哪个租户、你现在有哪些权限"的唯一权威，也是整个平台唯一能签发租户上下文的组件。它不判定"这个操作现在该不该做"——那是 S02 的事。**

S01 存在的原因是结构性的，不是"顺手做个登录"：整个 48 服务架构里，每个服务的每条命令都要知道 `tenant_id`，而这个值一旦允许任何一个服务"自己解一下 token、自己取一下 header"来获得，多租户隔离就不再是架构属性，而是一份 48 个服务各自实现的约定。E28「跨租户素材/记忆/hash 存在性」与 E27「企业 SSO 撤权/越权 MCP」考的不是"有没有登录"，考的是"`tenant_id` 有没有单一来源"。

所以本服务的设计目标是**让"信任 `tenant_id`"这件事只有一条代码路径**，并且这条路径上的每一步都可验证、可撤销、可审计。

---

## 1. 边界

### 1.1 写什么

| 事实 | 唯一写者 | 说明 |
|---|---|---|
| `tenant_id` 与租户生命周期 | **S01** | `provisioning / active / suspended / terminated` |
| 组织单元树、成员归属 | **S01** | `org_unit`、`member` 的挂靠关系 |
| 人类主体、服务主体、API client、MCP client 的身份 | **S01** | 四类 `principal` 分开建模，不合并成"用户" |
| 企业 IdP 联邦配置与组到角色的映射 | **S01** | `idp_connection`、`idp_group_mapping` |
| 角色定义（role）与权限授予（grant） | **S01** | 含负向授予（`deny`） |
| 租户级 `revocation_epoch` 与个人级 `personal_revocation_epoch` | **S01** | 撤销传播的唯一计数源 |
| 平台签名密钥的**元数据**（`key_id`、公钥、状态） | **S01** | 私钥在 KMS，库里不存 |
| `TenantContextToken`（TCT）的签发 | **S01** | 全平台唯一签发者 |
| 合同层面的 entitlement（`quota_plan`） | **S01** | 见 §1.3 第 4 条的边界说明 |
| 授权判定的事实记录（`authorization_decision`） | **S01** | 这是判定日志，不是 grant 的权威副本 |

### 1.2 不写什么

| 事实 | 真正写者 | S01 的角色 |
|---|---|---|
| 档位、生成门、接受门、kill switch | S02 | S01 只提供"有没有这个 permission"，不提供"现在该不该放行" |
| 预算余额、预留、实际费用 | S23 | 完全不涉及 |
| 供应商并发 / 速率 permit | S22 | 完全不涉及（见 §1.3 第 4 条） |
| 项目 / 剧集 / 单集的身份与成员角色 | S03 | S01 定义 `role_key` 的含义；S03 存 `principal_id` 引用 |
| 资产版权与音色授权资格 | S11–S15 | S01 只判"这个主体有没有 `asset.voice.authorize` 这个权限"；**"这个声音当前还能不能商用"是 S15 的事实** |
| 审计成品与保留策略执行 | S45 | S01 只落自己的判定证据并导出 |
| 用户会话（浏览器 cookie / refresh token） | Studio BFF | S01 不持有端侧会话 |
| 任何业务内容 | S04 / S05 / S06 … | 完全没有 |

### 1.3 一条必须坚持的原则

> **一个服务获得 `tenant_id` 的合法路径只有一条：读 S01 签发的、绑定到本次请求 `request_hash`、未过期、且 `revocation_epoch` 未推进的 `TenantContextToken`。任何"我自己解 IdP token / 我从 header 取 tenant_id / 我把 TCT 缓存到过期之后"的实现都算越权，必须被 SDK 拒绝而不是被 code review 拦。**

这条原则的四个否定面，写在这里是为了让后续服务作者不必再讨论：

1. **不能自己验 IdP 的 token 然后信它的 claim。** IdP token 的 audience 是 S01，不是任何业务服务。业务服务拿到一个 OIDC access token 去解出 `tenant_id`，等于绕过了 S01 的 grant 判定、撤销检查与审计——这三样它一样都没做。
2. **不能从 header 取 `tenant_id` 当作身份。** header 只用于**交叉核对**：TCT 里的 `tenant_id` 与 header 不一致时返回 403 `TENANT_CONTEXT_MISMATCH`，而不是"以 header 为准"。
3. **不能把 LLM 提供的 `tenant_id` 当输入。** 这是 AGENTS.md 明写的红线。S19 的 `AgentTask` 里的租户字段一律来自 `execution_context`，而 `execution_context` 的 `tenant_id` 来自 TCT。
4. **不能缓存超过 TCT 的生命周期。** TCT `exp - iat ≤ 60s`；`authorization-context` 的 `ttl_seconds = 5`。撤销的传播上限就是这两个数，不是"最终一致"。

### 1.4 `quota_plan` 与 S22 的边界（容易写错）

`service.yaml` 的 `data_owner` 里有 `quota-plan`。这里的 `quota_plan` 是**合同层面的 entitlement**：这个租户买了多少个席位、允许多少个并发 Run、哪些模态可用、保留多少天、数据驻留在哪个 region。

它**不是**运行时并发控制。运行时"现在还能不能开第 N 个 Run""这个账户池还有几个 permit"是 S22 的事实（[docs/01 §3](../docs/01-enterprise-architecture.md)：`供应商并发/速率permit | S22`）。

两者的关系是单向的：S22 的账户池容量**配置**来自 S01 的 entitlement，但 S22 绝不每次去问 S01"我还有多少并发"，也绝不把 S01 的 `max_concurrent_runs` 当成运行时的 permit 计数。一旦 S01 也记一份"已用并发"，它立刻变成 S22 的第二个权威。

这条边界的裁决依据与遗留问题见 §11 C5。

---

## 2. 输入契约

### 2.1 传输与身份

内部 mTLS + workload identity，HTTP/JSON。所有写命令在 body 里带：

```json
{
  "schema_version": "2.0",
  "command_id": "cmd_01J8ZQ...",
  "tenant_id": "tnt_01J8ZQ...",
  "actor": {
    "principal_id": "prn_01J8ZQ...",
    "principal_type": "human",
    "delegation_id": "dlg_01J8ZQ..."
  },
  "purpose": "org.member.provision",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "payload": { }
}
```

- `tenant_id` 来自调用方自己的 TCT，**不接受 body 自授**：若 body 的 `tenant_id` 与调用方 TCT 的 `tenant_id` 不同，直接 403 `TENANT_CONTEXT_MISMATCH`（E28）。
- 只有一条路径例外：**平台运维主体**（`principal_type='service'` 且持有 `platform.tenant.provision` 权限）可以创建租户，此时 `tenant_id` 由 S01 生成，body 里给的是 `slug`。
- `actor.delegation_id` 在本服务内部调用链上是必填的（[execution-context.v2](../contracts/execution-context.v2.schema.json) 要求如此）。平台发起的纯服务间调用没有代理关系时，约定填 `dlg_platform`——这条约定没有契约依据，见 §11 C4。

### 2.2 命令清单

| 方法 | 路径 | 作用 | 需要权限 |
|---|---|---|---|
| `POST` | `/tenants` | 创建租户（平台运维） | `platform.tenant.provision` |
| `POST` | `/tenants/{tenant_id}:suspend` | 挂起租户：所有新 TCT 签发被拒 | `platform.tenant.suspend` |
| `POST` | `/tenants/{tenant_id}:terminate` | 终止租户 | `platform.tenant.terminate` |
| `POST` | `/org-units` | 建组织单元 | `org.unit.manage` |
| `POST` | `/members` | 邀请/挂靠成员 | `org.member.provision` |
| `POST` | `/members/{principal_id}:offboard` | 离职：撤销全部个人 grant | `org.member.offboard` |
| `POST` | `/service-principals` | 注册服务主体与 workload identity | `platform.principal.manage` |
| `POST` | `/api-clients` | 注册 API / MCP client | `platform.principal.manage` |
| `POST` | `/api-clients/{client_id}:rotate` | 轮换 secret / 证书指纹 | `platform.principal.manage` |
| `POST` | `/idp-connections` | 建 IdP 联邦配置 | `tenant.idp.manage` |
| `POST` | `/idp-connections/{id}:activate` | 探测通过后启用 | `tenant.idp.manage` |
| `POST` | `/idp-connections/{id}/group-mappings` | 绑定组 → 角色模板 | `tenant.idp.manage` |
| `POST` | `/roles` | 建自定义角色 | `tenant.role.manage` |
| `POST` | `/access-grants` | 授予角色 | `tenant.grant.manage` |
| `POST` | `/access-grants/{grant_id}:revoke` | 撤销授予 | `tenant.grant.manage` |
| `POST` | `/tenant-contexts` | 用外部凭证换 TCT | 无（凭证本身即证明） |
| `POST` | `/permission-checks` | 权限判定原语 | 无（只读判定） |

> 路径与 `service.yaml` 声明的 `POST /access-grants` / `GET /authorization-context` 一致；其余路径在 catalog 里没有，见 §11 C2。

### 2.3 查询清单

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/authorization-context` | 取调用方（或指定主体）在某个 scope 上的完整授权上下文 |
| `GET` | `/principals/{principal_id}` | 主体事实（不含权限） |
| `GET` | `/access-grants` | 按主体 / scope / 状态列授予 |
| `GET` | `/roles/{role_key}` | 角色定义 |
| `GET` | `/.well-known/jwks.json` | TCT 验签公钥 |
| `GET` | `/health/readiness` | 就绪探针（见 §8.4） |

### 2.4 `POST /tenant-contexts`

这是全平台最热的写路径（虽然它不写业务表）。

```json
{
  "schema_version": "2.0",
  "grant_type": "idp_token",
  "idp_connection_id": "idp_01J8ZQ...",
  "assertion": "<OIDC id_token 或 SAML assertion>",
  "audience": "s21.orchestration",
  "request_hash": "9f2c...64hex",
  "delegation_id": "dlg_01J8ZQ...",
  "purpose": "run.start",
  "trace_id": "4bf92f35..."
}
```

- `audience` 必须是 S01 注册表里的合法服务受众（`sNN.slug`）。S01 不认识的值 → 422 `UNKNOWN_AUDIENCE`。
- `request_hash` 是**被授权的那次请求**的 hash，不是本请求的。TCT 与它绑定，因此一个 TCT 不能被拿去重放另一个 body。
- `grant_type='workload'` 时无 `assertion`，改为由 mTLS 对端证书推导 workload identity，`idp_connection_id` 省略。
- `grant_type='api_client_secret'` / `'api_client_mtls'` 时用 `client_id` + secret / 证书指纹。

响应见 §4.1。

### 2.5 `GET /authorization-context`

| 参数 | 必填 | 说明 |
|---|---|---|
| `scope_kind` | 是 | `tenant / org_unit / project / series` |
| `scope_id` | 是 | 与 `scope_kind` 配套 |
| `principal_id` | 否 | 默认是调用方自己；查他人需 `tenant.grant.manage` |
| `purpose` | 否 | 影响条件型 grant 的匹配 |

`scope_kind` 与 `scope_id` 的组合必须自洽（`scope_kind='project'` 但 `scope_id` 不是 `prj_` 前缀 → 422 `SCOPE_KIND_MISMATCH`）。S01 **不校验 scope_id 是否真的存在**——那是 S03 的事；S01 只校验格式前缀与调用方是否被授予了该 scope。

响应见 §4.2。`Cache-Control: no-store`，`ttl_seconds` 是给调用方自己用的。

### 2.6 `POST /permission-checks`

```json
{
  "schema_version": "2.0",
  "principal_id": "prn_...",
  "checks": [
    {"permission_key": "creative.script.seal", "scope_kind": "episode", "scope_id": "ep_...", "purpose": "script.seal"},
    {"permission_key": "budget.commitment.approve", "scope_kind": "project", "scope_id": "prj_...", "budget_scope_id": "bsc_..."}
  ]
}
```

一次最多 32 条。响应见 §4.3。批量是刻意的：S02 一次策略判定经常要问 5–10 个 permission，逐条 HTTP 会让 P95 变成 N×RTT。

### 2.7 错误码

| HTTP | `code` | 含义 | retryable |
|---|---|---|---|
| 400 | `INVALID_SCHEMA` | body 不符合 schema | 否 |
| 401 | `CREDENTIAL_INVALID` | IdP assertion / secret / 指纹无效 | 否 |
| 403 | `TENANT_CONTEXT_MISMATCH` | TCT 与 header/body 的 tenant 不一致 | 否 |
| 403 | `PERMISSION_DENIED` | 判定为 deny | 否 |
| 404 | `NOT_FOUND` | 对象不存在**或属于其他租户**（E28，不区分） | 否 |
| 409 | `COMMAND_IN_PROGRESS` | 同 command_id 正在执行 | 是 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 同 command_id 不同 body | 否 |
| 409 | `GRANT_REVISION_CONFLICT` | 撤销/修改时 revision 不匹配 | 是（带新 revision） |
| 409 | `TENANT_SUSPENDED` | 租户已挂起，拒绝签发 | 否 |
| 422 | `UNKNOWN_PERMISSION_KEY` | 权限键不在注册表 | 否 |
| 422 | `SCOPE_EXCEEDS_ROLE_MAX` | 角色允许的最大 scope 小于请求的 scope | 否 |
| 422 | `SCOPE_KIND_MISMATCH` | scope_kind 与 scope_id 前缀不符 | 否 |
| 422 | `UNKNOWN_AUDIENCE` | 受众不在注册表 | 否 |
| 429 | `RATE_LIMITED` | 见 §6.5 | 是 |
| 503 | `IDP_UNAVAILABLE` | 上游 IdP 不可达且无有效缓存 | 是 |

**409 与 422 的分界**（沿用 [S08 §2.6](S08-revision-snapshot.md)）：409 是"状态/版本冲突，拿到最新状态后重试可能成功"；422 是"这个请求本身就不可能成立，重试多少次都一样"。`SCOPE_EXCEEDS_ROLE_MAX` 归 422 而不是 403，因为它不是"你没权限"，而是"你给的参数组合没有意义"——403 会让调用方以为换个身份就行。

---

## 3. 核心逻辑与状态机

### 3.1 认证路径：从外部凭证到 TCT

```
外部请求
  │  OIDC id_token / SAML assertion / client secret / mTLS
  ▼
① 定位租户与 IdP 连接
   idp_connection_id 已知 → 直接取
   否则按 issuer 或 email domain 反查（仅当 tenant.sso_enforced=false 时才允许 domain 反查）
  ▼
② 验证断言
   签名（JWKS / 证书）、iss、aud（=S01）、exp、nbf、jti 未使用过（Redis，TTL=exp）
  ▼
③ 定位 principal
   (tenant_id, idp_connection_id, idp_subject) → principal
   未命中且 jit_provisioning=true → 建 invited 状态 principal
   未命中且 jit_provisioning=false → 401
  ▼
④ 状态检查
   tenant.lifecycle='active'、principal.status='active'
   （invited 且 JIT 首次登录 → 直接置 active；suspended/offboarded → 403）
  ▼
⑤ 物化 grant
   按 idp_group_mapping 把当前组集合 diff 进 role_grant
   新增 → state='active'；组已不在 → state='expired'，personal_revocation_epoch + 1
  ▼
⑥ 签发 TCT
   读 tenant.revocation_epoch 与 principal.personal_revocation_epoch
   按 scope 预计算 permission 集合（只是加速提示，不是授权凭据）
   签名（KMS，非对称，私钥不出 KMS）
  ▼
⑦ 记录 authorization_decision（decision_kind='issue'）
```

**第 ⑤ 步是唯一允许在认证路径上写 grant 表的地方**，且只写"来自 IdP 组映射"的那部分 grant（`granted_by.source = 'idp_group'`）。手工授予的 grant 绝不被登录路径改动——否则一次登录会把管理员手工加的临时权限冲掉。

**第 ⑦ 步不是可选的。** 每次签发落一条 `authorization_decision`，字段至少含 `(principal_id, audience, request_hash, revocation_epoch, tenant_revocation_epoch, purpose, issued_at)`。E27 要求"撤权及时生效"，出事时要能回答"这个请求是哪一次签发批准的、当时 epoch 是多少"。

### 3.2 TCT 的结构与校验

TCT 是 JWS compact，ES256，`kid` 指向 §5.9 的 `signing_key`。claims 见 §4.1。

**消费侧 SDK 的六道检查**（顺序固定，前一道失败不进下一道）：

| # | 检查 | 失败返回 |
|---|---|---|
| 1 | `kid` 在 JWKS 中、签名有效 | 401 `TCT_SIGNATURE_INVALID` |
| 2 | `aud` == 本服务受众 | 401 `TCT_AUDIENCE_MISMATCH` |
| 3 | `exp > now` 且 `now - iat ≤ 60s + skew(5s)` | 401 `TCT_EXPIRED` |
| 4 | `req_hash` == sha256(body 去掉 `command_id` 与 `request_hash`) | 403 `TCT_REQUEST_BINDING_MISMATCH` |
| 5 | `revocation_epoch ≥ 本地缓存的 tenant epoch` 且 `personal_revocation_epoch ≥ 本地缓存` | 401 `TCT_REVOKED` |
| 6 | 若本地 epoch 缓存年龄 > 5s → 先 `GET /authorization-context` 刷新，再判 | 503 `EPOCH_STALE` |

第 4 道是本设计与"普通 JWT"最大的差别：**TCT 是请求级票据，不是会话票据**。它把一个 token 的可用面压缩到"一次具体请求"上，因此一个被截获的 TCT 最多能重放它自己那次请求，而产生不了新请求。

第 5 道的"本地 epoch 缓存"由 `access.grant.changed` 事件推送（payload 里带 `tenant_revocation_epoch`），并且**每个服务必须自己设 5s 的硬上限**：缓存过期就回源，不能"事件还没到所以我不知道"。这条是防止"事件总线挂了导致撤权不生效"的结构性保证——回源是兜底，事件只是加速。

### 3.3 授权判定路径

```
permission-checks(principal, [(permission_key, scope, purpose)])
  │
  ├─ 展开 scope 链：episode → series → project → org_unit → tenant
  │   （只看这一条祖先链，不做兄弟节点搜索）
  │
  ├─ 收集该 principal 在链上每一级的 active grant
  │   filter: valid_from ≤ now, (valid_until IS NULL OR valid_until > now)
  │
  ├─ 对每个 (permission_key, scope)：
  │     deny_grants = grants where effect='deny' and role 含该 permission（或含通配）
  │     allow_grants = grants where effect='allow' and role 含该 permission
  │
  │     if deny_grants 非空                     → deny  ('EXPLICIT_DENY')
  │     elif allow_grants 为空                  → deny  ('NO_GRANT')
  │     elif 任一 allow 的 conditions 全部满足     → allow (首个匹配的 grant_id)
  │     elif allow_grants 非空但 conditions 都不满足 → deny  ('CONDITION_NOT_MET')
  │
  └─ 记录 authorization_decision（decision_kind='check'）
```

四条规则必须写死，不能留给实现自由发挥：

1. **deny 优先于 allow，且 deny 在祖先链上向下继承。** 在 `project` 上 deny 一个 permission，`series` / `episode` 上同名 allow 不生效。这是"组织管理员不自动获得素材创作版权"（[docs/14 §1](../docs/14-enterprise-operations.md)）的实现方式。
2. **condition 不满足是 deny，不是 abstain。** `abstain` / `require_human` 属于 S02 的 `PolicyDecision` 四值语义，S01 只有两值。把三值混进来会让 S02 的 fail-closed 逻辑失去参照。
3. **通配只在角色定义里展开，不在判定时展开。** `creative.*` 在写 `role` 时就展开成具体 `permission_keys`。判定路径上做前缀匹配会让"新增一个 permission 自动授予所有人"成为一次静默的越权。
4. **判定不看时间窗口以外的信息。** S01 不查 manifest、不查预算、不查 kill switch。它只回答身份与权限。把这些混进来，S01 就变成第二个 S02。

### 3.4 撤销传播

撤销有四个层级，作用范围与传播方式不同：

| 层级 | 触发 | 影响 | 传播 |
|---|---|---|---|
| grant 级 | `:revoke` | 单条授予失效 | 写 `role_grant` + outbox；事件携带新 `tenant_revocation_epoch` |
| principal 级 | `:offboard` | 该主体全部 grant 失效 | `personal_revocation_epoch + 1`；事件同租户范围广播 |
| IdP 组变更 | 登录时 diff 或 SCIM 同步 | 组派生 grant 失效 | 同上 |
| tenant 级 | `:suspend` | 全租户停止签发 | `tenant.revocation_epoch + 1` |

`revocation_epoch` 是**单调递增的计数**，不是时间戳。原因：时间戳会撞（同一微秒内两次撤销）、会回拨（NTP）、不能跨地域比较。计数由 S01 单点递增，每个 `tenant` 行与每个 `principal` 行各持有自己的计数。

**为什么撤销不靠"删 grant 行"传播**：删行之后，一个已经拿着 TCT 的调用方在 60s 内仍然能通过六道检查（因为它的 TCT 里带着旧的 epoch，而消费侧本地缓存也还是旧的）。epoch 推进才是让第 5 道检查失败的东西。删行只是为了后续判定正确——两件事都要做，不能只做一件。

**suspend 的细节**：租户挂起时**不能**把已有 TCT 立刻作废到"连读都不行"的程度——否则运营人员在挂起期间无法查账、无法导出。约定：挂起后新签发一律拒绝，已有 TCT 的自然过期（≤60s），第 5 道检查只对 `tenant.lifecycle='terminated'` 强制作废。这是刻意的取舍，见 §12 U2。

### 3.5 状态机

#### 3.5.1 租户

```
provisioning ──activate──▶ active ──suspend──▶ suspended
                              ▲                   │
                              └──── reactivate ───┘
                    active/suspended ──terminate──▶ terminated
```

| 状态 | 能签发 TCT | 能读 | 能写业务 |
|---|---|---|---|
| `provisioning` | 否 | 否（只有平台运维） | 否 |
| `active` | 是 | 是 | 是 |
| `suspended` | 否 | 是（只读，由 S02 判） | 否 |
| `terminated` | 否 | 否 | 否 |

`terminated` 是终态，不可回退。删除数据不在这里做——那要过 S45 与保留合同（[docs/04 §6](../docs/04-data-consistency.md)）。

#### 3.5.2 主体

```
invited ──first_login / accept──▶ active ──suspend──▶ suspended
   │                                │                    │
   │                                └──── reactivate ────┘
   └──cancel──▶ offboarded ◀──offboard──┘
```

`offboarded` 是终态。`principal_id` 永不复用——复用会让历史审计记录指向错误的人（E39）。

#### 3.5.3 授予

```
pending ──activate──▶ active ──revoke──▶ revoked
   │                    │
   │                    └──valid_until 到期──▶ expired
   └──cancel──▶ revoked
```

| 状态 | 参与判定 |
|---|---|
| `pending` | 否（`valid_from` 未到或需审批） |
| `active` | 是 |
| `revoked` | 否 |
| `expired` | 否 |

`revoked` 与 `expired` 分开是为了审计：前者是人主动撤销，后者是时间到了。合成一个 `inactive` 会让 E39 查不清"是谁在什么时候撤的"。

#### 3.5.4 IdP 连接

```
draft ──validate──▶ validating ──ok──▶ active ──probe_fail×N──▶ degraded
                        │                  ▲                        │
                        └──fail──▶ draft    └──── probe_ok ──────────┘
                                   active ──disable──▶ disabled
```

`degraded` 时：**已签发的 TCT 继续有效到过期**，新签发按 §7.3 的降级策略处理。这是唯一允许"降级放行"的地方，且只在 IdP 侧，不涉及 grant 判定。

### 3.6 破窗（break-glass）

平台与企业客户都需要一个"IdP 彻底挂了但必须在 5 分钟内进系统"的通道。设计上必须存在，但必须满足四条：

1. **必须绑定具体 principal 与具体时长**（≤4 小时），不能给一个"管理员账号"。
2. **创建即告警**：`break_glass_created` 进 S45 与运营队列，不给静默期。
3. **TCT 的 `delegation_id` 固定为 `dlg_breakglass_<id>`**，任何下游审计都能一眼看出这次操作不是正常委托。
4. **不能绕过 S02**：破窗只解决"身份从哪来"，不解决"这个操作该不该放行"。预算、外部分发、音色资格三个高风险权限**在破窗期间一律 deny**（写死在 §3.3 的 deny 列表中）。

第 4 条是本节最重要的一句。如果破窗能拿到 `budget.commitment.approve`，"IdP 挂了"就会变成一个绕过预算的常规借口。

### 3.7 不变量

实现时按这七条写断言（§9.3 有对应测试）：

1. 一个 TCT 只能签发一次用一次；`jti` 在 `exp` 内不可重用。
2. `tenant.revocation_epoch` 与 `personal_revocation_epoch` 只增不减。
3. 任一 `role_grant` 的 `(scope_kind, scope_id)` 必须落在授予者自己的可授范围内——不能授出自己没有的 scope（防横向越权）。
4. `deny` grant 的 `role_key` 必须存在于本租户，且不能是"全权限"通配。
5. `principal.tenant_id` 与 `role_grant.tenant_id` 必须一致；跨租户的 grant 在写入时拒绝。
6. 任何写 grant 的命令，必须在同一个本地事务里写 outbox 行。
7. `authorization_decision` 一旦写入不可修改，`decided_at` 不可回填。

---

## 4. 输出契约

### 4.1 `TenantContextToken` claims

```json
{
  "iss": "s01.tenant-access",
  "aud": "s21.orchestration",
  "sub": "prn_01J8ZQ...",
  "jti": "tct_01J8ZQ...",
  "iat": 1762300800,
  "exp": 1762300855,
  "schema_version": "2.0",
  "principal_type": "human",
  "tenant_id": "tnt_01J8ZQ...",
  "home_region": "cn-north",
  "home_cell": "cell-a1",
  "delegation_id": "dlg_01J8ZQ...",
  "purpose": "run.start",
  "req_hash": "9f2c...",
  "revocation_epoch": 812,
  "personal_revocation_epoch": 3,
  "scope_hint": {
    "project_ids": ["prj_01J8ZQ..."],
    "permissions": ["production.run.start", "production.run.read"]
  },
  "break_glass": false
}
```

`scope_hint` 明确标注是**提示**：它用于让下游在本地做粗筛（例如提前拒绝明显越界的 project），但**任何一次实际准入都要走 `authorization-context` 或 `permission-checks`**。把 `scope_hint` 当授权凭据是本设计能犯的最严重的实现错误。

`exp - iat = 55s`，留 5s 给时钟偏斜。

### 4.2 `GET /authorization-context` 响应

```json
{
  "schema_version": "2.0",
  "tenant": {
    "tenant_id": "tnt_...",
    "lifecycle": "active",
    "home_region": "cn-north",
    "data_residency": "cn"
  },
  "principal": {
    "principal_id": "prn_...",
    "principal_type": "human",
    "status": "active",
    "org_unit_id": "org_..."
  },
  "scope": {"kind": "project", "id": "prj_..."},
  "grants": [
    {
      "grant_id": "grn_...",
      "role_key": "director",
      "effect": "allow",
      "scope_kind": "series",
      "scope_id": "ser_...",
      "conditions": {"region": "cn-north"},
      "valid_until": null
    }
  ],
  "permissions": [
    "creative.shot.read",
    "creative.shot.revision.create",
    "production.run.start"
  ],
  "denied_permissions": [
    "budget.commitment.approve"
  ],
  "revocation_epoch": 812,
  "personal_revocation_epoch": 3,
  "evaluated_at": "2026-11-05T09:00:00.000Z",
  "ttl_seconds": 5,
  "context_hash": "sha256:5b1e..."
}
```

`context_hash` 覆盖 `(principal, scope, permissions, denied_permissions, revocation_epoch, personal_revocation_epoch)`。S02 可以把它写进 `PolicyDecision` 的证据里，从而让"这次放行基于哪份授权上下文"可追溯（E39）。

`grants` 返回**原始授予**而不是只返回 `permissions`——因为 S02 需要知道"这个 allow 是来自哪个 role、在哪个 scope 上"，才能写出可读的拒绝理由与正确的门判定。

### 4.3 `POST /permission-checks` 响应

```json
{
  "schema_version": "2.0",
  "context_hash": "sha256:5b1e...",
  "revocation_epoch": 812,
  "results": [
    {
      "permission_key": "creative.script.seal",
      "scope_kind": "episode",
      "scope_id": "ep_...",
      "verdict": "allow",
      "matched_grant_id": "grn_...",
      "reason_code": "GRANT_MATCHED"
    },
    {
      "permission_key": "budget.commitment.approve",
      "scope_kind": "project",
      "scope_id": "prj_...",
      "verdict": "deny",
      "matched_grant_id": "grn_deny_...",
      "reason_code": "EXPLICIT_DENY"
    }
  ]
}
```

`reason_code` 枚举：`GRANT_MATCHED`、`NO_GRANT`、`EXPLICIT_DENY`、`CONDITION_NOT_MET`、`SCOPE_OUT_OF_RANGE`、`PRINCIPAL_INACTIVE`、`TENANT_SUSPENDED`。

判定结果**不进 Redis 做跨请求缓存**。理由是它带 `revocation_epoch`，缓存就等于把撤销延迟从 5s 放大到缓存 TTL；而判定本身是纯内存计算（一个主体的 grant 数在几十条量级），不需要缓存。

### 4.4 `access.grant.changed` 事件

topic `drama.tenant-access.v2`，分区键 `tenant_id:aggregate_id`，`aggregate_id = grant_id`，`aggregate_sequence = grant_revision`。

```json
{
  "specversion": "1.0",
  "schema_version": "2.0",
  "id": "evt_01J8ZQ...",
  "source": "s01.tenant-access",
  "type": "access.grant.changed",
  "subject": "prn_01J8ZQ...",
  "time": "2026-11-05T09:00:00.000Z",
  "tenant_id": "tnt_...",
  "home_cell": "cell-a1",
  "aggregate_id": "grn_...",
  "aggregate_sequence": 2,
  "correlation_id": "cmd_01J8ZQ...",
  "causation_id": "cmd_01J8ZQ...",
  "traceparent": "00-4bf92f35...-00f067aa...-01",
  "data": {
    "grant_id": "grn_...",
    "grant_revision": 2,
    "state": "revoked",
    "previous_state": "active",
    "subject_principal_id": "prn_...",
    "role_key": "director",
    "effect": "allow",
    "scope_kind": "series",
    "scope_id": "ser_...",
    "tenant_revocation_epoch": 812,
    "personal_revocation_epoch": 3,
    "changed_by": {"principal_id": "prn_admin...", "principal_type": "human"},
    "reason": "role.reassigned",
    "occurred_at": "2026-11-05T09:00:00.000Z"
  },
  "data_ref": null
}
```

三条设计要点：

1. **`aggregate_id` 是 `grant_id` 而不是 `principal_id`。** 一个主体可能同时有几十条 grant，用主体做聚合会让所有这些无关的变更挤在同一个 sequence 上，从而人为制造乱序与阻塞。用 grant 做聚合，每个聚合的 sequence 就是 `1(granted) → 2(revoked)`。
2. **`tenant_revocation_epoch` 与 `personal_revocation_epoch` 必须随每个事件带出。** 这是让消费侧 5s 缓存在"任意一条相关事件到达"时就能立刻更新 epoch 的原因，不需要额外的 epoch 事件。
3. **`data_ref = null`。** 这个事件的 payload 很小，没必要走引用。大负载（例如完整角色定义）不发事件——消费侧需要时回源 `GET /roles/{role_key}`。

本服务**只发这一个事件**。租户挂起、主体离职都通过它携带的 epoch 变化表达，不另发 topic（§11 C8 讨论了这个取舍的代价）。

### 4.5 JWKS

```
GET /.well-known/jwks.json
```

返回 `{"keys": [{"kty":"EC","crv":"P-256","kid":"k_2026_11_a","x":"...","y":"...","use":"sig","alg":"ES256"}]}`。

- 同时最多有 2 个 `current`（轮换重叠期），旧的标 `retired` 但**保留发布 90 天**（覆盖最长 TCT 生命周期 60s + 消费侧 JWKS 缓存 60s，留巨大余量是为了应对某服务长期不重启）。
- 消费侧 JWKS 缓存 TTL 60s；遇未知 `kid` 必须立即刷新一次再判（`kid` 命中失败可能只是轮换竞态）。

---

## 5. 数据模型

Postgres，schema `tenant_access`。所有表带 `tenant_id`，主键/唯一约束一律含 `tenant_id`（E28：跨租户不能通过唯一冲突探测存在性）。启用 RLS 作为第二道保护（[docs/14 §2](../docs/14-enterprise-operations.md)）。ID 用 ULID 文本，前缀区分：`tnt_ / org_ / prn_ / grn_ / rol_ / idp_ / apl_ / qpl_ / spn_ / dcs_`。

`signing_key` 是全局表（跨租户共用一套签名密钥），不参与 RLS。

### 5.1 租户

```sql
CREATE TABLE tenant_access.tenant (
  tenant_id            text PRIMARY KEY,          -- 'tnt_01J...'，永不复用
  slug                 text NOT NULL UNIQUE,
  display_name         text NOT NULL,
  lifecycle            text NOT NULL
                       CHECK (lifecycle IN ('provisioning','active','suspended','terminated')),
  home_region          text NOT NULL,
  home_cell            text NOT NULL,
  data_residency       text NOT NULL,             -- 'cn' / 'eu' / 'us'，E31
  sso_enforced         boolean NOT NULL DEFAULT true,
  revocation_epoch     bigint  NOT NULL DEFAULT 0,
  plan_id              text,                      -- -> quota_plan.plan_id（当前生效）
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  version              integer NOT NULL DEFAULT 1,   -- 元数据 CAS
  suspended_at         timestamptz,
  terminated_at        timestamptz,
  CONSTRAINT tenant_lifecycle_time_chk CHECK (
    (lifecycle = 'suspended')  = (suspended_at  IS NOT NULL) AND
    (lifecycle = 'terminated') = (terminated_at IS NOT NULL)
  ),
  CONSTRAINT tenant_epoch_nonneg_chk CHECK (revocation_epoch >= 0)
);
```

`slug` 全局唯一，因此**跨租户可以探测 slug 是否被占用**。这是刻意的：slug 出现在客户自己的登录地址里，必须全局可路由。除此之外没有任何跨租户可探测的字段（E28 的测试用例 T01-19 专门验证这一点）。

### 5.2 组织单元

```sql
CREATE TABLE tenant_access.org_unit (
  tenant_id    text NOT NULL,
  org_unit_id  text NOT NULL,                     -- 'org_01J...'
  parent_id    text,
  path         text NOT NULL,                     -- '/org_root/org_a/org_b'，物化路径
  display_name text NOT NULL,
  depth        smallint NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  archived_at  timestamptz,
  PRIMARY KEY (tenant_id, org_unit_id),
  UNIQUE (tenant_id, path),
  FOREIGN KEY (tenant_id, parent_id)
    REFERENCES tenant_access.org_unit (tenant_id, org_unit_id),
  CONSTRAINT org_unit_depth_chk CHECK (depth BETWEEN 0 AND 8)
);
CREATE INDEX org_unit_path_gist ON tenant_access.org_unit USING gist (path gist_trgm_ops);
```

`path` 用物化路径而不是递归查询：scope 链展开是热路径（每次判定都要走），递归 CTE 在 8 层深度上也能跑但会把 P95 从微秒级推到毫秒级。代价是移动组织单元要重写整棵子树的 `path`——移动是低频运维操作，可以接受。

`depth ≤ 8` 是硬约束。超过 8 层的组织树在生产里从来没见人真正用过，而每多一层就多一次祖先链遍历。

### 5.3 主体

```sql
CREATE TABLE tenant_access.principal (
  tenant_id                text NOT NULL,
  principal_id             text NOT NULL,         -- 'prn_01J...'，永不复用
  principal_type           text NOT NULL
                           CHECK (principal_type IN ('human','service','api_client','mcp_client')),
  display_name             text NOT NULL,
  status                   text NOT NULL
                           CHECK (status IN ('invited','active','suspended','offboarded')),
  org_unit_id              text,
  idp_connection_id        text,
  idp_subject              text,
  personal_revocation_epoch bigint NOT NULL DEFAULT 0,
  created_at               timestamptz NOT NULL DEFAULT now(),
  updated_at               timestamptz NOT NULL DEFAULT now(),
  offboarded_at            timestamptz,
  PRIMARY KEY (tenant_id, principal_id),
  UNIQUE (tenant_id, idp_connection_id, idp_subject),
  FOREIGN KEY (tenant_id, org_unit_id)
    REFERENCES tenant_access.org_unit (tenant_id, org_unit_id),
  CONSTRAINT principal_type_identity_chk CHECK (
    (principal_type = 'human') = (idp_connection_id IS NOT NULL)
  ),
  CONSTRAINT principal_offboarded_chk CHECK (
    (status = 'offboarded') = (offboarded_at IS NOT NULL)
  )
);
```

`UNIQUE (tenant_id, idp_connection_id, idp_subject)` 在 Postgres 里允许多个 NULL，正好服务主体与 API client 都没有 IdP 身份，不受影响。

`principal_type_identity_chk` 保证人类主体一定绑了 IdP——一个没有 IdP 的 `human` 只可能来自破窗，而破窗不建 principal（复用一个既有 principal 并打 `break_glass=true` 标记）。

### 5.4 服务主体与 API client

```sql
CREATE TABLE tenant_access.service_principal (
  tenant_id          text NOT NULL,
  principal_id       text NOT NULL,
  service_id         text NOT NULL CHECK (service_id ~ '^S[0-9]{2}$'),
  workload_identity  text NOT NULL,               -- 'spiffe://drama.internal/s21/orchestration'
  allowed_audiences  text[] NOT NULL,             -- 它能被哪些受众调用
  created_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, principal_id),
  UNIQUE (tenant_id, workload_identity),
  FOREIGN KEY (tenant_id, principal_id)
    REFERENCES tenant_access.principal (tenant_id, principal_id)
);

CREATE TABLE tenant_access.api_client (
  tenant_id        text NOT NULL,
  principal_id     text NOT NULL,
  client_id        text NOT NULL,                 -- 'apl_01J...'，对外可见
  secret_hash      text,                          -- argon2id；mTLS-only 时为 NULL
  cert_thumbprints text[] NOT NULL DEFAULT '{}',  -- SHA-256 指纹
  scopes           text[] NOT NULL,               -- permission_key 子集
  status           text NOT NULL
                   CHECK (status IN ('active','rotating','suspended','revoked')),
  created_at       timestamptz NOT NULL DEFAULT now(),
  rotated_at       timestamptz,
  expires_at       timestamptz,
  PRIMARY KEY (tenant_id, principal_id),
  UNIQUE (tenant_id, client_id),
  FOREIGN KEY (tenant_id, principal_id)
    REFERENCES tenant_access.principal (tenant_id, principal_id),
  CONSTRAINT api_client_secret_or_cert_chk CHECK (
    secret_hash IS NOT NULL OR array_length(cert_thumbprints, 1) > 0
  )
);
```

`scopes` 直接是 `permission_key` 列表而不是 role 引用：外部 client 的授权面必须**可以被逐条审阅**，套一层 role 会让"这个 client 到底能干什么"需要两次 join 才能回答。E27「越权 MCP」的取证就靠这张表。

### 5.5 角色

```sql
CREATE TABLE tenant_access.role (
  tenant_id            text NOT NULL,
  role_key             text NOT NULL,             -- 'director' / 'custom.director_v2'
  display_name         text NOT NULL,
  builtin              boolean NOT NULL DEFAULT false,
  permission_keys      text[] NOT NULL,
  deny_permission_keys text[] NOT NULL DEFAULT '{}',
  max_scope_kind       text NOT NULL
                       CHECK (max_scope_kind IN ('tenant','org_unit','project','series')),
  revision             integer NOT NULL DEFAULT 1,
  created_at           timestamptz NOT NULL DEFAULT now(),
  archived_at          timestamptz,
  PRIMARY KEY (tenant_id, role_key),
  CONSTRAINT role_no_wildcard_chk CHECK (
    NOT EXISTS (
      SELECT 1 FROM unnest(permission_keys || deny_permission_keys) AS p
      WHERE p ~ '[%*]'
    )
  ),
  CONSTRAINT role_deny_not_all_chk CHECK (
    NOT (deny_permission_keys @> ARRAY['*']::text[])
  )
);
```

两条约束对应 §3.3 规则 3 与 §3.7 不变量 4：**通配在写入时就展开/拒绝，判定路径上永不展开**。`role_no_wildcard_chk` 里的子查询在 Postgres 的 CHECK 里不是合法写法（CHECK 不能含子查询），实现时应改为触发器或应用层校验——这里保留是为了表达意图，落地时见 §12 U4。

`max_scope_kind` 是防横向越权的第一道：`series` 级角色不能授出 `project` 级 grant。

### 5.6 授予

```sql
CREATE TABLE tenant_access.role_grant (
  tenant_id             text NOT NULL,
  grant_id              text NOT NULL,            -- 'grn_01J...'
  subject_principal_id  text NOT NULL,
  role_key              text NOT NULL,
  effect                text NOT NULL CHECK (effect IN ('allow','deny')),
  scope_kind            text NOT NULL
                        CHECK (scope_kind IN ('tenant','org_unit','project','series')),
  scope_id              text NOT NULL,
  conditions            jsonb NOT NULL DEFAULT '{}'::jsonb,
  state                 text NOT NULL
                        CHECK (state IN ('pending','active','revoked','expired')),
  valid_from            timestamptz NOT NULL DEFAULT now(),
  valid_until           timestamptz,
  grant_revision        integer NOT NULL DEFAULT 1,
  granted_by            jsonb NOT NULL,           -- {principal_id, principal_type, source, source_ref}
  granted_at            timestamptz NOT NULL DEFAULT now(),
  revoked_at            timestamptz,
  revoked_by            jsonb,
  revoke_reason         text,
  PRIMARY KEY (tenant_id, grant_id),
  FOREIGN KEY (tenant_id, subject_principal_id)
    REFERENCES tenant_access.principal (tenant_id, principal_id),
  FOREIGN KEY (tenant_id, role_key)
    REFERENCES tenant_access.role (tenant_id, role_key),
  CONSTRAINT role_grant_state_chk CHECK (
    (state = 'revoked') = (revoked_at IS NOT NULL)
  ),
  CONSTRAINT role_grant_window_chk CHECK (
    valid_until IS NULL OR valid_until > valid_from
  )
);
CREATE INDEX role_grant_subject_idx
  ON tenant_access.role_grant (tenant_id, subject_principal_id, state, scope_kind, scope_id);
CREATE INDEX role_grant_scope_idx
  ON tenant_access.role_grant (tenant_id, scope_kind, scope_id, state);
CREATE INDEX role_grant_expiry_idx
  ON tenant_access.role_grant (tenant_id, valid_until)
  WHERE state = 'active' AND valid_until IS NOT NULL;
```

`conditions` 的允许键（不在白名单内的键在写入时 422）：`region`、`data_residency`、`purpose`、`budget_scope_id`、`approval_duty`、`time_window`。这些是**主体属性与请求属性的比对**，不是策略判定——区别在于它们随 grant 一起授予、一起撤销，而 S02 的策略是独立的、可版本化的。

`grant_revision` 每次状态变化 +1，作为 `aggregate_sequence`。**撤销不会删行**：删行会让 `aggregate_sequence` 断裂，也会让审计失去"曾经授予过"的证据。

### 5.7 IdP 连接与组映射

```sql
CREATE TABLE tenant_access.idp_connection (
  tenant_id            text NOT NULL,
  idp_connection_id    text NOT NULL,             -- 'idp_01J...'
  protocol             text NOT NULL CHECK (protocol IN ('oidc','saml')),
  issuer               text NOT NULL,
  metadata             jsonb NOT NULL,            -- 端点 + 公钥/证书（公开信息）
  jit_provisioning     boolean NOT NULL DEFAULT false,
  default_org_unit_id  text,
  group_claim          text,                      -- 'groups' / 'memberOf'
  state                text NOT NULL
                       CHECK (state IN ('draft','validating','active','degraded','disabled')),
  revision             integer NOT NULL DEFAULT 1,
  last_probe_at        timestamptz,
  last_probe_result    jsonb,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, idp_connection_id),
  UNIQUE (tenant_id, issuer),
  FOREIGN KEY (tenant_id, default_org_unit_id)
    REFERENCES tenant_access.org_unit (tenant_id, org_unit_id)
);

CREATE TABLE tenant_access.idp_group_mapping (
  tenant_id         text NOT NULL,
  mapping_id        text NOT NULL,
  idp_connection_id text NOT NULL,
  group_value       text NOT NULL,                -- IdP 侧的组标识，原样
  role_key          text NOT NULL,
  effect            text NOT NULL CHECK (effect IN ('allow','deny')),
  scope_kind        text NOT NULL
                    CHECK (scope_kind IN ('tenant','org_unit','project','series')),
  scope_id          text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, mapping_id),
  UNIQUE (tenant_id, idp_connection_id, group_value, role_key, scope_kind, scope_id),
  FOREIGN KEY (tenant_id, idp_connection_id)
    REFERENCES tenant_access.idp_connection (tenant_id, idp_connection_id),
  FOREIGN KEY (tenant_id, role_key)
    REFERENCES tenant_access.role (tenant_id, role_key)
);
```

`metadata` 里的证书只存公开部分；任何 private key / client secret 走 KMS 引用（`kms://…`），不落库。这个约束要在 migration 的 review checklist 上。

### 5.8 合同 entitlement

```sql
CREATE TABLE tenant_access.quota_plan (
  tenant_id         text NOT NULL,
  plan_id           text NOT NULL,                -- 'qpl_01J...'
  plan_key          text NOT NULL,                -- 'enterprise-standard'
  seats             integer NOT NULL,
  entitlements      jsonb NOT NULL,
  -- { "modalities": ["text","image","video","voice"],
  --   "max_concurrent_runs": 200,
  --   "retention_days": 400,
  --   "export_formats": ["master_h264","prores"] }
  effective_from    timestamptz NOT NULL,
  effective_until   timestamptz,
  state             text NOT NULL CHECK (state IN ('active','superseded','expired')),
  contract_ref      text,                         -- 合同编号，审计用
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, plan_id),
  CONSTRAINT quota_plan_window_chk CHECK (
    effective_until IS NULL OR effective_until > effective_from
  )
);
CREATE INDEX quota_plan_active_idx
  ON tenant_access.quota_plan (tenant_id, effective_from DESC)
  WHERE state = 'active';
```

`entitlements` 是**声明**，不是**执行**。S22 在配置账户池时读它，S01 自己从不按 `max_concurrent_runs` 拒绝任何请求——否则 S01 会变成第二个调度器（§1.4）。

### 5.9 签名密钥

```sql
CREATE TABLE tenant_access.signing_key (
  key_id      text PRIMARY KEY,                   -- 'k_2026_11_a'
  algorithm   text NOT NULL CHECK (algorithm IN ('ES256')),
  public_jwk  jsonb NOT NULL,
  kms_key_ref text NOT NULL,                      -- 'kms://.../tct-sign-2026-11'
  state       text NOT NULL CHECK (state IN ('next','current','retired')),
  not_before  timestamptz NOT NULL,
  not_after   timestamptz,                        -- retired 后仍发布 90 天
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX signing_key_single_current
  ON tenant_access.signing_key ((state)) WHERE state = 'current';
```

`signing_key_single_current` 的唯一索引写成 `((state))` 是错的（state 恒定，只能有一行）——落地时用部分索引 + 排他约束表达"最多两个 current"，见 §12 U5。保留这条是为了明确"同一时刻不允许出现第三个 current"。

私钥永不入库。`kms_key_ref` 只是引用，`public_jwk` 用于发布 JWKS。

### 5.10 判定记录

```sql
CREATE TABLE tenant_access.authorization_decision (
  tenant_id                text NOT NULL,
  decision_id              text NOT NULL,         -- 'dcs_01J...'
  decision_kind            text NOT NULL CHECK (decision_kind IN ('issue','check')),
  principal_id             text NOT NULL,
  audience                 text,
  permission_key           text,
  scope_kind               text,
  scope_id                 text,
  verdict                  text CHECK (verdict IN ('allow','deny')),
  reason_code              text,
  matched_grant_id         text,
  tenant_revocation_epoch  bigint NOT NULL,
  personal_revocation_epoch bigint NOT NULL,
  request_hash             char(64),
  purpose                  text,
  break_glass              boolean NOT NULL DEFAULT false,
  decided_at               timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, decision_id, decided_at)
) PARTITION BY RANGE (decided_at);
```

按月分区，本地保留 90 天，之后由 S45 的导出流程接管（E39）。**这张表不是 grant 的副本**：它记录"当时判定成了什么"，与"现在应该是什么"无关。任何代码路径都不得读取它来做授权判定——否则一次错误的历史判定会被永久复读。

### 5.11 幂等、Outbox

```sql
CREATE TABLE tenant_access.idempotency_record (
  tenant_id     text NOT NULL,
  command_id    text NOT NULL,
  request_hash  char(64) NOT NULL,
  command_type  text NOT NULL,
  state         text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code integer,
  response_body jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, command_id)
);
CREATE INDEX idempotency_expiry_idx ON tenant_access.idempotency_record (expires_at);

CREATE TABLE tenant_access.outbox (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,               -- grant_id
  aggregate_sequence bigint NOT NULL,             -- grant_revision
  event_type         text NOT NULL,
  partition_key      text NOT NULL,               -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx
  ON tenant_access.outbox (id) WHERE published_at IS NULL;
```

`(aggregate_id, aggregate_sequence, event_type)` 唯一：一次 grant 状态变化只可能产生一条 `access.grant.changed`，重复写会被约束挡住。

### 5.12 不建的表

- **不建 `inbox`。** S01 零依赖、不消费任何事件。建一张永远为空的 inbox 会让"这个服务到底消费什么"这个问题每年被问一次。如果将来要消费（例如 S03 的归档通知），那要连同消费逻辑一起加，而不是先建表。
- **不建 `permission_cache`。** 见 §4.3：判定不进缓存。
- **不建 `session`。** 端侧会话归 Studio BFF。
- **不建 `tenant_usage`。** 运行时用量归 S22/S23/S44。S01 记一份"已用席位"就是第二个权威。
- **不建 `role_projection`。** S01 自己就是权威。

---

## 6. 幂等与并发

### 6.1 幂等语义（与 S08/S22/S23 同一定义）

```
request_hash = sha256( JCS( body 去掉 command_id 与 request_hash 两个键 ) )
```

JCS = RFC 8785 JSON Canonicalization Scheme。

| 已有记录 | 本次请求 | 结果 |
|---|---|---|
| 无 | — | 正常执行，先插 `IN_PROGRESS` |
| `IN_PROGRESS` | 同 hash | 409 `COMMAND_IN_PROGRESS`，`retryable: true`，`Retry-After: 1` |
| `DONE` | 同 hash | 原样返回首次的 `response_code` + `response_body`，附 `replayed: true` |
| 任意 | 不同 hash | 409 `IDEMPOTENCY_KEY_REUSED`，`retryable: false` |

**树内唯一的例外是 `POST /tenant-contexts`。** 它不写 idempotency 表，原因有二：

1. 它每次调用都产出一个**新的一次性票据**（新 `jti`），重放同一个请求体在语义上不是"重复执行同一个命令"，而是"再签发一次"——按幂等语义回放旧 TCT 会把一个已经快到期的票据还回去。
2. 它是最热路径（每次外部请求一次），写一张 30 天保留的表会把它变成写放大最严重的接口。

代价：调用方重试会拿到多个有效 TCT。这没有问题，因为 TCT 失效的判定靠 `revocation_epoch` 与 `exp`，多个并存不产生越权。这条取舍写在这里是为了让审阅者知道它是**刻意的**，不是漏了。

### 6.2 写入事务模板

每个写命令的实现骨架固定为：

```python
with db.transaction() as tx:                      # 本地事务，REPEATABLE READ
    rec = tx.idempotency_begin(tenant_id, command_id, request_hash, cmd_type)
    if rec.replayed:                              # DONE + 同 hash
        return rec.response_code, rec.response_body
    # ... 业务写入 ...
    tx.execute("UPDATE tenant_access.tenant SET revocation_epoch = revocation_epoch + 1 WHERE tenant_id = %s", ...)
    evt = build_event(...)
    tx.insert_outbox(evt)                         # 与业务写入同一事务
    body = build_response(...)
    tx.idempotency_finish(tenant_id, command_id, 200, body)
    return 200, body
```

顺序不可调换：**epoch 递增、业务写入、outbox 插入必须在同一个事务里**。如果 epoch 先提交而 outbox 后提交，就存在"撤销已生效但下游收不到事件"的窗口——这个窗口由 §3.2 第 6 道的 5s 回源兜住，但不能靠它兜。

### 6.3 并发控制

| 场景 | 机制 |
|---|---|
| 同一 grant 的并发撤销 | `UPDATE … WHERE grant_id=? AND grant_revision=?`，影响 0 行 → 409 `GRANT_REVISION_CONFLICT` |
| 同一 grant 并发 `:revoke` | 先到的成功并推进 revision；后到的因 revision 不匹配失败 |
| 同一主体并发授予不同 grant | 无冲突，各自独立聚合 |
| 同一主体的组映射物化 | 按 `(tenant_id, principal_id)` 取 `SELECT … FOR UPDATE` 串行，避免两次登录并发产生重复 grant |
| 租户 epoch 并发递增 | 单行 `UPDATE … SET revocation_epoch = revocation_epoch + 1`，行锁天然串行 |

### 6.4 锁顺序

固定为 `tenant → principal → role_grant → outbox`。反过来（先锁 grant 再锁 tenant）会在"离职同时挂起租户"时死锁，虽然罕见但可复现。

### 6.5 限流与背压

- `POST /tenant-contexts`：按 `(tenant_id, idp_connection_id)` 与按源 IP 双维度限流。超限 429，**带 `Retry-After`**，不排队——登录路径排队会变成雪崩。
- `POST /permission-checks`：按调用方服务限流，默认 5000 QPS/实例。
- 批量上限 32 条是硬约束：一个请求带 500 个 check 会让单次判定从 O(grants) 变成 O(grants × checks)，把 P95 打穿。

### 6.6 部署与恢复

- 无状态多副本，读多写少。写路径的瓶颈是 `tenant` 行的 epoch 递增（同一租户的写天然串行）。按规划值（1,000 租户 / 2,000 峰值用户）这不是瓶颈；单一超大租户的连续撤销是唯一热点，用队列串行化该租户的写即可。
- 恢复顺序见 [docs/14 §5](../docs/14-enterprise-operations.md)：**灾备恢复时 S01 必须最先起**，否则任何服务都无法建立租户上下文。S01 的 readiness 不依赖任何下游（零依赖），但依赖 KMS——KMS 不可用时进入只读模式（可以验签，不能签发），见 §7.6。
- 独立升级（E01）：S01 的 schema 迁移必须向后兼容一个版本（`current` 与 `current-1` 的服务实例可同时运行），因为升级期间旧实例仍在签发 TCT，而所有下游都在验签。

---

## 7. 失败分支

### 7.1 认证与签发

| 场景 | 行为 | 返回 | 备注 |
|---|---|---|---|
| IdP 不可达，且该主体 5 分钟内有成功签发 | 用 KMAC 保护的本地缓存断言（只缓存 claim 摘要，不缓存原 token），签发 | 200 | 缓存键 `(tenant, idp, subject)`；缓存内容不含任何凭证 |
| IdP 不可达，无缓存 | 拒绝 | 503 `IDP_UNAVAILABLE` | 不做"先放进来再说" |
| IdP 证书轮换导致验签失败 | 立即刷新 JWKS 一次；仍失败则拒绝 | 401 | 刷新限速 1 次/分钟，防打崩 IdP |
| assertion `jti` 已用过 | 拒绝 | 401 | Redis TTL = `exp`，不用内存 |
| principal 不存在且 JIT 关闭 | 拒绝（不透露租户是否存在） | 401 | E28 |
| principal 为 `offboarded` | 拒绝 | 403 | 同时 epoch 已被推进 |
| 租户 `suspended` | 拒绝签发 | 409 `TENANT_SUSPENDED` | 已有 TCT 自然过期 |
| 组映射物化时发现角色已归档 | 跳过该映射，落 warn 日志，**不阻断登录** | 200 | 角色归档是运维动作，不该锁死用户 |

### 7.2 判定

| 场景 | 行为 | 返回 |
|---|---|---|
| `permission_key` 不在注册表 | 拒绝整批（不部分成功） | 422 `UNKNOWN_PERMISSION_KEY` |
| scope 链在某级断裂（org_unit 已归档） | 该级不贡献 grant，继续向上 | 正常判定 |
| grant 的 `conditions` 无法评估（缺属性） | 视为 `CONDITION_NOT_MET` | deny |
| 调用方查他人上下文但无 `tenant.grant.manage` | 拒绝 | 403 |
| 请求的 `scope_id` 格式对但对象不存在 | **照常判定**（S01 不查对象存在性） | 200 |
| 单批 32 条全部命中 deny | 正常返回 32 条 deny，不合并 | 200 |

最后一条值得多说一句：S01 返回逐条 verdict，不返回"整体 allow/deny"。合并是 S02 的事，因为"5 个 permission 里 4 个 allow 1 个 deny"在不同策略下含义不同（有的操作需要全部 allow，有的只需任一）。

### 7.3 IdP 降级

| 状态 | 新签发 | 已有 TCT | 告警 |
|---|---|---|---|
| `active` | 正常 | 正常 | — |
| `degraded`（连续 3 次探测失败） | 只允许 5 分钟缓存路径 | 正常 | P2 |
| `disabled`（人工） | 拒绝 | 正常到期 | P1 |
| `draft` / `validating` | 拒绝 | 正常到期 | — |

`degraded` 是刻意的宽松：IdP 短时抖动不应该让整个租户停工。但缓存窗口 5 分钟是硬上限，且**缓存命中会在 `authorization_decision` 里打 `idp_cache_hit=true`**，让事后能看出这段时间的登录不是实时验证的。

### 7.4 事件与 Outbox

| 场景 | 行为 |
|---|---|
| Outbox 写入失败 | 整个事务回滚，命令失败（不能"业务成功、事件丢了"） |
| Relay 发布失败 | 保留 `published_at IS NULL`，指数退避重试，不丢 |
| Relay 在 publish 后、`UPDATE published_at` 前崩溃 | 重复投递一次，下游按 `event_id` 去重（E13/E32） |
| Kafka 不可用超过 15 分钟 | 本地 outbox 堆积告警；**写路径继续**（outbox 是本地表），但订阅方会 epoch 落后，靠 5s 回源兜住 |
| `aggregate_sequence` 跳号 | 消费侧缓冲 30s 后回源 `GET /access-grants`，不无限等 |

### 7.5 数据库

| 场景 | 行为 |
|---|---|
| 主库切换（failover） | 依赖 PG 自身；写路径在切换窗口返回 503，客户端重试（幂等保护） |
| RLS 配置错误导致跨租户可见 | 启动自检：带错误 `tenant_id` 的查询必须返回 0 行，否则**拒绝启动** |
| `idempotency_record` 表膨胀 | 每日分区/清理 `expires_at < now()`；清理失败告警但不阻断 |

RLS 自检是 §9.4 的 T01-20：一个服务最关键的安全属性必须有一个"错了就起不来"的检查，而不是一条依赖 code review 的约定。

### 7.6 KMS 不可用

| 阶段 | 行为 |
|---|---|
| 已有 `current` key 且 KMS 可达 | 正常 |
| KMS 短暂不可达 | 签名失败 → 503；**缓存 key 材料仅限 10 分钟**，超过即拒绝签发 |
| 长期不可用（>10 分钟） | 进入只读模式：`GET /authorization-context` 与 `permission-checks` 仍可用（它们是纯本地计算），签发一律 503 |

只读模式的存在是为了让"已在跑的生产 Run 继续跑"：它们持有的是尚未过期的 TCT 与已缓存的上下文，不需要新签发。如果 KMS 挂了就连判定都停，一次密钥服务故障会变成全平台停产。

---

## 8. 可观测

### 8.1 指标

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `s01_tct_issue_total` | counter | `tenant_tier`, `principal_type`, `result` | 登录量、失败率 |
| `s01_tct_issue_latency_p95` | histogram | `grant_type` | 目标 ≤ 150ms（本地）；含 IdP 往返 ≤ 600ms |
| `s01_idp_roundtrip_latency` | histogram | `idp_connection_id` | 区分 S01 自身慢与 IdP 慢 |
| `s01_idp_cache_hit_total` | counter | `tenant_id` | 降级期可见性（§7.3） |
| `s01_permission_check_total` | counter | `verdict`, `reason_code` | 拒绝原因分布 |
| `s01_permission_check_latency_p95` | histogram | `check_count_bucket` | 目标 ≤ 20ms |
| `s01_authorization_context_latency_p95` | histogram | `scope_kind` | 目标 ≤ 30ms |
| `s01_revocation_epoch` | gauge | `tenant_id` | 单调性告警（下降即 P1） |
| `s01_epoch_lag_seconds` | histogram | — | 消费侧上报的回源延迟，目标 P95 ≤ 5s |
| `s01_grant_active_count` | gauge | `tenant_id`, `role_key` | 权限膨胀检测 |
| `s01_deny_grant_count` | gauge | `tenant_id` | 异常 deny 增长 |
| `s01_outbox_unpublished_age_seconds` | gauge | — | 目标 P99 ≤ 10s |
| `s01_outbox_publish_failures_total` | counter | `error_class` | — |
| `s01_cross_tenant_rejection_total` | counter | `source_service` | **E28 核心指标**，激增即攻击或实现 bug |
| `s01_break_glass_active` | gauge | `tenant_id` | 必须为 0 才算正常 |
| `s01_jwks_unknown_kid_total` | counter | `requesting_service` | 轮换问题的早期信号 |

### 8.2 告警阈值

| 条件 | 级别 | 动作 |
|---|---|---|
| `s01_tct_issue_total{result="error"}` 5 分钟 > 5% | P1 | 登录故障，先看 IdP 指标 |
| `s01_revocation_epoch` 任一租户下降 | P1 | 数据损坏，立即冻结写 |
| `s01_cross_tenant_rejection_total` 1 分钟 > 50 | P1 | 疑似越权扫描或 SDK 缺陷 |
| `s01_outbox_unpublished_age_seconds` P99 > 60s | P2 | 事件总线检查 |
| `s01_break_glass_active > 0` > 4h | P1 | 破窗未关闭 |
| `s01_idp_cache_hit_total` 占比 > 10% | P2 | IdP 持续降级 |
| `s01_jwks_unknown_kid_total` > 100/min | P2 | 轮换不同步 |

### 8.3 审计导出

每次签发与每次 deny 判定都进 `authorization_decision`，由 S45 按批次拉取。导出契约：S01 提供 `GET /audit-events?since=…&until=…`（分页，游标为 `(decided_at, decision_id)`），S45 负责校验链与保留。

**S01 不自己实现审计存储的完整性证明**——那是 S45 的事。S01 只保证"写入顺序即时间顺序"（`decided_at` 由 DB 时钟生成，不用应用时钟）。

### 8.4 健康检查

| 端点 | 检查内容 |
|---|---|
| `/health/liveness` | 进程存活 |
| `/health/readiness` | DB 可写 + 至少一个 `current` key 可用于验签 + JWKS 端点可服务 |
| `/health/readiness?strict=1` | 额外要求 KMS 签发可用（用于判断能否接流量） |

`readiness` 不检查 IdP：IdP 挂了降级路径仍能服务（§7.3）。把 IdP 放进 readiness 会让一次 IdP 抖动把 S01 全部摘流，进而全平台失去签发能力——这是最典型的"依赖检查做过头"。

---

## 9. 测试清单

标注 `[E##]` 的对应 [docs/16 §5](../docs/16-migration-acceptance.md) 验收项。全部用 mock 依赖（mock IdP、mock KMS、内存 PG），不接真实企业 IdP。

### 9.1 幂等与并发 `[E12]`

| # | 用例 | 期望 |
|---|---|---|
| T01-01 | 同 `command_id` 同 body 重复 `POST /access-grants` | 第二次返回 `replayed: true`，只产生一条 grant、一条 outbox |
| T01-02 | 同 `command_id` 不同 body | 409 `IDEMPOTENCY_KEY_REUSED`，原 grant 不变 |
| T01-03 | 并发两个 `:revoke` 同 revision | 一个 200，一个 409 `GRANT_REVISION_CONFLICT` |
| T01-04 | 并发"离职"与"挂起租户" | 不死锁；两个 epoch 都推进 |
| T01-05 | 32 条 check 与 33 条 check | 32 条 200；33 条 400 |
| T01-06 | `POST /tenant-contexts` 同 body 两次 | 两个不同 `jti`，都有效（§6.1 的刻意例外） |

### 9.2 撤销与时效 `[E27]`

| # | 用例 | 期望 |
|---|---|---|
| T01-07 | 撤销 grant 后 5s 内，客户端用旧 TCT 调下游 | 下游第 5 道检查失败，401 `TCT_REVOKED` |
| T01-08 | 撤销后立即 `GET /authorization-context` | `revocation_epoch` 已推进，`permissions` 不再含该项 |
| T01-09 | 主体离职 | `personal_revocation_epoch + 1`；全部 grant `revoked` |
| T01-10 | 组映射移除后登录 | 对应 grant `expired`，epoch 推进 |
| T01-11 | 手工授予的临时 grant + 一次 IdP 登录 | 临时 grant 不被登录路径改动（§3.1） |
| T01-12 | 事件总线断开 30s，期间撤销 | 消费侧 5s 后回源，仍拒绝（不依赖事件送达） |
| T01-13 | 挂起租户后已有 TCT 未过期 | 抖音级读操作仍可（按 §3.4 约定），但新签发 409 |

### 9.3 授权语义

| # | 用例 | 期望 |
|---|---|---|
| T01-14 | project 级 deny + series 级 allow 同一 permission | deny（deny 向下继承） |
| T01-15 | role 含通配 `creative.*` | 写入时 422/拒绝 |
| T01-16 | series 级角色请求 project 级 grant | 422 `SCOPE_EXCEEDS_ROLE_MAX` |
| T01-17 | 授予者授出自己没有的 scope | 403（不变量 3） |
| T01-18 | 条件 `region` 不匹配 | deny `CONDITION_NOT_MET`（不是 abstain） |

### 9.4 跨租户 `[E28]`

| # | 用例 | 期望 |
|---|---|---|
| T01-19 | 用 A 租户 TCT 查 B 租户的 principal / grant | 404，响应体与"不存在"完全一致（含耗时抖动 < 5ms） |
| T01-20 | 故意去掉 RLS 策略启动 | **拒绝启动**（§7.5） |
| T01-21 | body 的 `tenant_id` 与 TCT 不一致 | 403 `TENANT_CONTEXT_MISMATCH` |
| T01-22 | 跨租户 slug 探测 | 允许（唯一的有意例外，§5.1）；断言其他字段不可探测 |
| T01-23 | 跨租户 `command_id` 相同 | 各自独立幂等（主键含 `tenant_id`） |

### 9.5 事件 `[E13][E32]`

| # | 用例 | 期望 |
|---|---|---|
| T01-24 | 业务提交后、outbox 写入前杀掉进程 | 事务回滚，grant 与 outbox 都不存在 |
| T01-25 | outbox 写入后、publish 前杀掉 | 重启后 relay 补发，只发一次 |
| T01-26 | publish 后、`published_at` 更新前杀掉 | 重复投递；下游按 `event_id` 去重 |
| T01-27 | 重复 replay 全量 topic（`replay_side_effects=false`） | 下游投影幂等，无重复副作用 |
| T01-28 | 事件 payload 校验 | 符合 [event-envelope.v2](../contracts/event-envelope.v2.schema.json)，`additionalProperties:false` |
| T01-29 | `aggregate_sequence` 跳号 | 下游 30s 后回源 `GET /access-grants` |

### 9.6 独立升级与恢复 `[E01][E34]`

| # | 用例 | 期望 |
|---|---|---|
| T01-30 | 滚动升级期间旧新实例同时签发 | 下游都能验签（JWKS 双 key 重叠） |
| T01-31 | 回滚到上一版本 | schema 向后兼容，无失败迁移 |
| T01-32 | KMS 不可达 10 分钟后 | 进入只读模式，`permission-checks` 仍 200，签发 503 |
| T01-33 | 单实例最小权限 | 只有本 schema 的读写角色；无跨库连接配置 |

### 9.7 安全 `[E29][E39]`

| # | 用例 | 期望 |
|---|---|---|
| T01-34 | IdP assertion 重放（同 `jti`） | 401 |
| T01-35 | 伪造签名 / 改 `aud` | 401，且不透露校验细节 |
| T01-36 | TCT 被拿去调另一个 body | 403 `TCT_REQUEST_BINDING_MISMATCH` |
| T01-37 | 破窗期间尝试高风险权限 | deny（§3.6 第 4 条） |
| T01-38 | 判定记录不可改 | `UPDATE` / `DELETE` 在 DB 权限层被拒 |
| T01-39 | 审计导出分页无重复无遗漏 | 游标推进正确 |

---

## 10. 与上下游服务的接口约定

### 10.1 上游（S01 依赖谁）

**没有。** S01 是零依赖服务：不消费事件、不调用任何业务服务。它依赖的是基础设施（KMS、企业 IdP、Postgres、Kafka），不是服务。

这条"零依赖"是架构上的要求而不是巧合：S01 在灾备恢复顺序里排第一（[docs/14 §5](../docs/14-enterprise-operations.md)）。如果 S01 依赖任何业务服务，恢复链就会出现环。

### 10.2 下游（谁依赖 S01）

| 服务 | 用 S01 做什么 | 频率 | 契约 |
|---|---|---|---|
| 全部服务 | 验 TCT（SDK 内），`auth-sdk` | 每请求 | §3.2 六道检查 |
| S02 | 取 `authorization-context` 作为策略判定的身份输入 | 每 dispatch | §4.2 |
| S21 | Run 准入前取上下文 | 每 Run 启动 | §4.2 |
| S26 | 派单前取上下文（[S02 §7](S02-governance-policy.md) 的实时复查） | 每 dispatch | §4.2 |
| S40 | 审核人资格判定 | 每决定 | §4.3 |
| S46 | API client / MCP client 的 scope 校验 | 每请求 | §4.3 |
| S47 | 投影里的操作者身份解析 | 事件消费 | `GET /principals/{id}` |
| S45 | 拉取判定证据 | 批量 | §8.3 |
| S19 | 为 AgentTask 绑定租户上下文 | 每 task | §4.1（task 创建时已带 execution_context，不需再签） |

### 10.3 S01 → S02 的边界（最容易写错）

| 问题 | 谁答 |
|---|---|
| 这个人有没有 `production.run.start` 这个权限？ | **S01** |
| 这个项目现在是第四档还是第二档？ | S02 |
| 这个 Run 现在能不能开（预算够不够、有没有 kill switch）？ | S02（依赖 S23/S22 的事实） |
| 这个人的审核资格够不够审核这一集？ | S01 给"有没有 `review.decide`"，S02 给"该不该由他审"（例如不能审自己写的） |

S02 的 `PolicyDecision` 里必须引用 S01 的 `context_hash`（§4.2）。没有这个引用的策略判定，事后无法回答"当时那个人的权限是什么"（E39）。

### 10.4 S01 → S19 的边界

[docs/08 §1](../docs/08-agents-prompts.md) 原句："工具调用的上下文不信任 LLM 提供的 tenant 字段，由平台签名上下文绑定。"

实现上：`AgentTask` 创建时由发起方（S21 或 S05/S06 的编排）从自己的 TCT 派生一个**受限 execution_context** 传给 S19。这个 context 的 `tenant_id` 来自 TCT，`scope_hint.permissions` 是父权限的**子集**（Agent 不能比它的发起者权限更大）。S19 在每次工具调用时用这个 context 去 `POST /permission-checks`。

**不能做的事**：让 Agent 自己生成 `tenant_id`、让 Agent 的输出里带 `tenant_id` 并被下游采信、给 Agent 一个比发起者更大的 context。这三条任何一条成立，[AGENTS.md](../AGENTS.md) 的红线就被绕过了。

### 10.5 S01 → S46 的边界

MCP 的 12 个工具按 `read / plan / execute / review / admin` 分 scope（[docs/13 §2](../docs/13-api-product.md)）。映射到 S01 的 `permission_key` 时必须满足：

- `read` 类工具（`project.get`、`memory.query`、`context.preview`、`run.get`、`artifact.compare`、`model.estimate`）→ 只读 permission，且**不得隐式付费**。
- `plan` 类（`episode.plan`、`change.plan`、`memory.extract`）→ 需要 `plan:compute` 预算 scope，因此 S01 要判 `budget.purpose.plan_compute`。
- `execute` 类（`episode.generate`、`change.apply`）→ 高风险，需要单列 permission。
- `review` / `admin` → 单列。

`readOnlyHint` 等 MCP 提示只影响 UI，**强制授权在 S01/S02 侧**（[docs/13 §2](../docs/13-api-product.md) 原句）。这条映射表没有机器可读契约，见 §11 C11。

---

## 11. 契约缺口（需要先行修改，不要自行补）

按 [AGENTS.md](../AGENTS.md)「契约与文档冲突 → 停下报告」与 [docs/16 §6](../docs/16-migration-acceptance.md)：

| # | 缺口 | 建议 |
|---|---|---|
| **C1** | **任务简报要求 S01 发布 `tenant.access.changed`，但仓库三处产物一致写 `access.grant.changed`**：`contracts/service-catalog.v2.json`、`services/tenant-access/service.yaml`、`contracts/event-topics.v2.json`（topic `drama.tenant-access.v2`） | **本文按仓库契约写 `access.grant.changed`。** 这是简报与契约的冲突，**需要简报发出方裁决**：要么改简报，要么三处一起改成 `tenant.access.changed` 并走事件 schema 的 major 迁移。**不允许**只有 S01 代码用新名字而 topics 目录还用旧名字——那会让消费方订阅到空 topic 且无任何报错 |
| C2 | `openapi.v2.yaml` 没有 `/access-grants`、`/authorization-context`、`/tenant-contexts`、`/permission-checks` 等任何 S01 路径（全文件 21 个 path，无一属于 S01） | 新增 `contracts/openapi/s01-tenant-access.v2.yaml`，由本方案 §2 派生；catalog 的 `api.command/query` 只是两行摘要，不足以生成 client |
| C3 | 没有 `TenantContextToken` 的 JSON Schema，也没有 `authorization-context` 响应 schema | 新增 `contracts/identity/tenant-context-token.v2.schema.json` 与 `authorization-context.v2.schema.json`。**这两个是 §3.2 六道检查的实现依据，缺了就只能各服务自己解释 claims** |
| C4 | [execution-context.v2](../contracts/execution-context.v2.schema.json) 把 `actor.delegation_id` 列为 `required`，但纯服务间调用（S01 自己、S19 内部、S20 编译）没有代理关系 | 二选一：把 `delegation_id` 改为可选，或定义 `dlg_platform` 这一保留值并在 schema 里加 `pattern` 约束。本文暂用 `dlg_platform`（§2.1），**这是未裁决的临时约定** |
| C5 | `quota-plan` 归 S01 写，但 [docs/01 §3](../docs/01-enterprise-architecture.md) 说 permit 归 S22。没有任何文档说明两者的边界（是"合同 entitlement"还是"运行时并发"） | 本文按"合同 entitlement"实现（§1.4）。**需要 docs/01 或 S22 方案作者明确**：若 S22 要按 S01 的 `max_concurrent_runs` 做硬上限，则必须定义"entitlement 变更如何通知 S22"，目前无此事件 |
| C6 | E27 只说"权限及时生效"，没有任何文档给出传播上限 | 本文取 5s（`ttl_seconds`）与 60s（TCT `exp`）。**需要 docs/14 把这两个数写成 SLO**，否则每个服务会自己挑一个 |
| C7 | 没有 JWKS 端点的契约，也没有密钥轮换的重叠期规范 | 新增 `contracts/identity/jwks.v2.md`（本方案 §4.5 可作为草稿）：`kid` 命名、重叠窗口、`retired` 保留 90 天 |
| C8 | `event-topics.v2.json` 只有 `access.grant.changed`，没有租户挂起/终止、主体离职的独立事件。S47 的投影与 S45 的审计无法订阅这些事实 | 本文让 epoch 变化搭车在 grant 事件上（§4.4）。**代价**：一个从未有 grant 的租户被挂起，不发任何事件，下游只能靠 5s 回源发现。若要求推送，需新增 `tenant.lifecycle.changed` |
| C9 | SCIM 成员生命周期在 [docs/14 §1](../docs/14-enterprise-operations.md) 是"可选接入"，但没有 SCIM 端点的契约 | 本文只做登录时组 diff，不做 SCIM 推送接收。若客户要求实时离职同步，需新增 `POST /scim/v2/Users` 等契约 |
| C10 | `access.grant.changed` 没有 `data` 最小字段集的 schema | 按本方案 §4.4 新增 `contracts/events/access-grant-changed.v2.schema.json` |
| C11 | `permission_key` 命名空间没有注册表；[mcp-tools.v2.design.json](../contracts/mcp-tools.v2.design.json) 的 12 个工具 scope 与 `permission_key` 没有映射 | 新增 `contracts/identity/permission-registry.v2.json`（键 → 风险等级 → 是否需要预算 scope → 对应 MCP tool）。**`role` 表的通配校验与 S02 的门判定都依赖它**，没有它就只能硬编码 |
| C12 | 没有受众注册表：`aud` 的合法值（`s21.orchestration` 等）只存在于本方案的散文里 | 由 `service-catalog.v2.json` 派生一个 `contracts/identity/audiences.v2.json`，或在 catalog 里加 `audience` 字段 |
| C13 | 破窗流程（§3.6）在 docs 里没有任何依据 | 需要 docs/14 增补：谁有权发起、最长时长、哪些权限强制 deny、如何关闭 |

---

## 12. 未决

按"需要谁裁决"排序。每条都写清了**本文当前采用的临时立场**，以便实施不被阻塞。

| # | 未决问题 | 本文临时立场 | 需要谁裁决 |
|---|---|---|---|
| U1 | C1 的事件命名最终用哪个 | `access.grant.changed`（与仓库一致） | 简报发出方 + 契约维护方 |
| U2 | 租户挂起后，已有 TCT 是否允许只读 | 允许，只拒绝新签发（§3.4） | 产品 + 安全 |
| U3 | `permission_key` 的粒度 | `<domain>.<resource>.<action>`，如 `creative.script.seal` | 契约维护方（C11） |
| U4 | `role_no_wildcard_chk` 用触发器还是应用层 | 应用层校验 + 加载期 registry 校验（C11），DB 层只留 `deny_not_all` | S01 实施会话 |
| U5 | "最多两个 `current` 签名 key" 的 DB 层表达 | 部分唯一索引 + 运维检查 | S01 实施会话 |
| U6 | 破窗最长时长 | 4 小时（§3.6） | 安全 + 客户成功 |
| U7 | 组织树深度上限 8 是否够 | 够；超过需重新评估祖先链遍历成本 | 产品 |
| U8 | `POST /tenant-contexts` 不进幂等表（§6.1）是否可接受 | 可接受：TCT 是一次性票据 | 安全评审 |
| U9 | 是否要给 S01 加 `tenant.lifecycle.changed` 事件（C8） | 暂不加，用 epoch 搭车 | 契约维护方 |
| U10 | `authorization_decision` 本地保留 90 天是否满足客户合同 | 90 天本地 + S45 长期；具体按合同 | 法务 + 客户 |
| U11 | S01 要不要为"授权上下文"提供订阅式推送（避免 5s 轮询） | 暂不：5s 只在缓存过期时回源，不是轮询 | 架构 |
| U12 | 破窗期间是否允许 `asset.voice.authorize` | 不允许（与预算、外部分发同列高风险） | 安全 |

### 12.1 实施顺序建议

1. 契约先行：C2 / C3 / C11 / C12 四个文件必须先落地，否则实现只能硬编码。
2. `tenant` / `principal` / `org_unit` / `role` / `role_grant` 五张表 + `permission-checks`（纯判定，无认证）。
3. `auth-sdk` 的六道检查 + 一个"故意写错"的示例服务，用于 E27 的负向验证。
4. IdP 联邦 + 组映射物化 + TCT 签发。
5. Outbox + relay + 5s 回源兜底。
6. 破窗 + 审计导出 + `authorization_decision` 分区。
7. RLS 自检与启动守卫（T01-20）。

第 3 步刻意排在 IdP 之前：**没有下游真的按六道检查去验，签发做对了也没人用**。先让一个假服务因为错误地取 header 而失败，比先做完整 IdP 更有价值。
