# S02 · 治理与自动化策略服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`governance-policy`。

## 责任与所有权

AutomationPolicyRevision/GateRule/PolicyDecision/PolicyException；策略发布指针。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /policies/{id}/revisions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`POST /policy-decisions:evaluate`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`policy.revision.published`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按策略决策QPS扩容；版本化本地缓存。

故障恢复：已固定策略可读取；无法确认新授权时停止新增付费动作；语义策略热更需显式checkpoint。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

项目→单集→阶段→节点覆盖正确；安全/预算硬约束不能被低级覆盖。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。
