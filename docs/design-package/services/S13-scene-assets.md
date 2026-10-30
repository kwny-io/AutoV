# S13 · 场景与空间资产服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`scene-assets`。

## 责任与所有权

SceneAssetRevision/平面图/多角度anchor/门窗固定物/光照变体。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /scene-assets/{id}/revisions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /scene-assets/{id}/revisions/{revision}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`asset.scene.revision.accepted`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S28, S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：高分图和空间处理独立Worker池。

故障恢复：场景anchor不可变；跨镜头空间检查可复用。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

日夜与闪回引用正确版本；门窗位置与运动轴可检查。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。
