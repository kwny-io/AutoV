# 平台 SDK

四个 primitive SDK。**只承载协议原语，不承载业务逻辑。**

| SDK | 承载 |
|---|---|
| `auth-sdk` | 租户上下文、受信身份绑定、权限判定原语 |
| `event-sdk` | EventEnvelope v2、Outbox / Inbox、幂等消费、重放 |
| `telemetry-sdk` | OpenTelemetry 追踪、统一 trace 链 |
| `idempotency-sdk` | `tenant_id + command_id + request_hash` 幂等键 |

## 边界

SDK 不许包含任何服务的数据模型或业务规则。服务之间**不可互相 import 对方业务 ORM**；SDK 只提供协议原语，服务各自实现自己的 schema。

## 幂等键

统一为 `tenant_id + command_id + request_hash`。

- 同键同 body → 返回相同结果
- 同键不同 body → **409 拒绝**，不是覆盖

## 事件

Outbox 与数据库提交在**同一个事务**里。消费侧 Inbox 至少一次、按 aggregate 保序或版本补偿。

重放时 `side_effects=false` —— 不能重发付费、发布、通知这类副作用。

## 统一追踪链

```
command → manifest → workflow → node → attempt → budget/permit
→ provider → ingest → QC → review → master → delivery → ledger
```

链断了就等于事故定位不到源头。
