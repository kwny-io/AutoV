# S44 · 效果评测与经营分析服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`evaluation-analytics`。

## 责任与所有权

BenchmarkDataset/盲评/实验/质量成本时长投影/客户项目ROI；原账本由S23持有。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /evaluation-runs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /metrics`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`evaluation.report.published`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S23, S35, S36, S37, S40, S43。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：OLAP独立扩容；按tenant/时间分区；事件批量消费。

故障恢复：可重放投影与watermark；分析故障不阻断已授权制作。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

成片可用率/每可用分钟成本/人工干预/返工可从事件重算。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。
