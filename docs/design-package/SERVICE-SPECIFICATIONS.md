# 全部服务边界规格汇编

各服务文件仍可独立阅读与交接。

# S01 · 租户与访问服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`tenant-access`。

## 责任与所有权

tenant/org/member/service-principal/role-grant/quota-plan；企业IdP映射。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /access-grants`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /authorization-context`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`access.grant.changed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：企业身份提供商与平台基础设施，无业务服务前置依赖。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按认证QPS与租户数量扩容；企业IdP独立连接池。

故障恢复：IdP异常按令牌有效期与撤销策略处理；关键写操作不使用过期授权。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

跨租户对象、撤销凭证、服务身份越权均被拒绝。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

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


---

# S03 · 项目与剧集目录服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`project-catalog`。

## 责任与所有权

Project/Series/Episode稳定ID、成员引用、规格与业务目标；场次ID由S05负责。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /projects/{id}/episodes`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /projects/{id}/episodes`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`project.episode.created`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按租户/项目分区；目录查询缓存。

故障恢复：目录暂不可用时已有快照任务可继续；新项目创建停止。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

重排集序不改变稳定ID；归档项目阻止新Run。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S04 · 创意与剧本服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`narrative`。

## 责任与所有权

BriefRevision/ScriptRevision/DialogueRevision/StoryArcProposal；剧本草稿事实。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /scripts/{id}/revisions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /scripts/{id}/revisions/{revision}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`narrative.script.revision.created`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S03, S18, S19。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按创作请求与LLM额度独立Worker池。

故障恢复：创作失败保留上个版本与Agent检查点；候选不写入正式记忆。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

改稿追加版本；未接受剧本不能提交为正史。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S05 · 分集分场服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`breakdown`。

## 责任与所有权

Scene稳定ID、SceneBreakdownRevision、角色道具需求清单。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /breakdowns`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /breakdowns/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`breakdown.revision.created`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S04, S19。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按分解任务数与文本长度扩容。

故障恢复：重试相同command返回同一场次分解；不重复造场次ID。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

场次关联到固定剧本版本；显示序号与ID分离。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S06 · 分镜与导演规范服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`storyboard`。

## 责任与所有权

Shot稳定ID/ShotSpecRevision/动作节拍/机位构图/镜头顺序。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /storyboards/{id}/revisions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /shots/{id}/revisions/{revision}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`storyboard.shot.revision.created`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S05, S18, S19。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按镜头规划量扩容；独立导演任务池。

故障恢复：不修改旧镜头内容；时长不可行返回具体问题。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

仰拍4秒指令能产生合法DSL；对白及主动作不被默默删改。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S07 · 对话修改与变更计划服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`change-management`。

## 责任与所有权

ChangePlan/ChangeSaga/字段补丁/派生改动/成本影响摘要；不拥有接受指针。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /change-plans/{id}:apply`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /change-plans/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`change.plan.applied`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S08, S09, S18, S02。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按修改命令与影响分析规模扩容。

故障恢复：通过Saga恢复已创建内容与未提交manifest；最终指针只由S08 CAS提交。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

重复命令同结果；并发修改409；只重做相关子图。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S08 · 版本快照与接受状态服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`revision-snapshot`。

## 责任与所有权

EpisodeManifestRevision/accepted pointers/generation epoch/节点锁/AcceptanceCommit；当前生产状态唯一权威。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /manifests/{id}:commit`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /manifests/{id}/revisions/{revision}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`snapshot.manifest.committed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S03。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按project串行CAS，跨项目水平分区。

故障恢复：唯一写主与fencing；提交事务+Outbox；接受旧epoch时拒绝。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

旧结果、旧审核、旧QC不能切换当前指针；回滚生成新manifest而非改历史。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S09 · 依赖溯源与影响分析服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`lineage-impact`。

## 责任与所有权

Artifact/Revision/Memory claim引用边、字段投影、影响索引；内容事实由原服务持有。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /lineage-edges`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`POST /impact-queries`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`lineage.impact.computed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S08。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按项目分片；大型图按revision分段；拓扑查询CPU扩容。

故障恢复：投影落后时返回watermark；提交影响计划前缺边fail-closed或保守重检。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

记忆事实改动可定位后续集、上下文包、剧本和镜头。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S10 · 资产目录与绑定解析服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`asset-catalog`。

## 责任与所有权

GlobalAssetID/类型/owner-service映射/跨类型BindingSetRevision/检索投影；不拥有角色外观细节。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /binding-sets`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /assets/{id}/resolve`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`asset.binding-set.created`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S11, S12, S13, S14, S15。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按资产检索与绑定请求扩容；索引可重建。

故障恢复：各资产服务不齐时保留固定绑定，缺必要版本停止新生成。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

绑定必须固定revision与权利版本；升级资产显示影响清单。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S11 · 角色身份资产服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`character-assets`。

## 责任与所有权

CharacterRevision/多视图manifest/身份特征/canonical接受状态/角色权利引用。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /characters/{id}/revisions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /characters/{id}/revisions/{revision}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`asset.character.revision.accepted`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S28, S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按设定集生成与检索分池；身份特征提取GPU可选。

故障恢复：canonical独立审批；漂移生成物不能自动成为新身份基准。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

跨集同人物固定ID；妆造变化不偷偷改变身份。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S12 · 服装与妆造资产服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`costume-assets`。

## 责任与所有权

CostumeRevision/穿着组合/材质配色/破损湿度妆造变体。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /costumes/{id}/revisions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /costumes/{id}/revisions/{revision}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`asset.costume.revision.accepted`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S28, S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：服装制作与检索独立池。

故障恢复：服装服务失败只阻塞相关绑定；历史版本可继续读取。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

角色身份与服装拆开；场次换装与破损有明确状态。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

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


---

# S14 · 道具资产服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`prop-assets`。

## 责任与所有权

PropRevision/尺寸外观/文字/开合破损变体；持有者历史由S16管理。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /props/{id}/revisions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /props/{id}/revisions/{revision}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`asset.prop.revision.accepted`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S28, S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按道具数量和图像生成量扩容。

故障恢复：无法读取关键道具版本时禁止以文字临时代替必需参考。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

同道具跨集外观稳定；持有关系与外观来源可区分。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S15 · 音色与声音授权服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`voice-assets`。

## 责任与所有权

VoiceRevision/provider_voice映射/发音词典/克隆资格/授权撤销状态。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /voices/{id}/revisions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /voices/{id}/bindings`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`asset.voice.authorization.changed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S28, S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按语言与供应商池扩容；克隆准备独立任务。

故障恢复：撤销状态实时验证；不自动换成相似声线；保留来源审计。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

跨供应商映射回归；未授权音色不可新调用。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S16 · 跨集记忆与正史服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`story-memory`。

## 责任与所有权

CanonBranch/ClaimRevision/StoryEvent/EntityState/Relationship/KnowledgeState/PlotThread/双时间版本/记忆接受批次。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /memory-commits`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`POST /memory-queries`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`memory.canon.committed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S08, S19。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按tenant/series/branch分片；结构化查询主，搜索索引辅。

故障恢复：消费已接受manifest后幂等提取；记忆checkpoint未就绪时阻止依赖集生成。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

谁知道什么、钥匙归属、伤势、伏笔跨集正确；改前集触发失效。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S17 · 连续性校验服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`continuity`。

## 责任与所有权

ContinuityReport/预期state_in-out/跨集矛盾/轴线视线检查/规则例外。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /continuity-checks`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /continuity-reports/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`continuity.check.completed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S16, S06, S10, S28。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：文本逻辑与多模态校验分池。

故障恢复：冲突返回证据与严重度；不直接更改正史或素材。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

闪回、误导、合法特效与真实矛盾有区分；低置信度abstain。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S18 · 上下文检索与组装服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`context-retrieval`。

## 责任与所有权

ContextPackRevision/检索过滤/引用与token预算/记忆水位；不拥有正史。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /context-packs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /context-packs/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`context.pack.created`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S16, S10, S09。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按检索QPS/索引CPU扩容；租户隔离缓存。

故障恢复：索引落后按memory watermark回源；禁止用旧pack冒充当前事实。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

分集可见性、角色知情状态、分支、故事时间和权限过滤生效。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S19 · Agent运行与工具沙箱服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`agent-runtime`。

## 责任与所有权

AgentDefinitionRevision/AgentTask/执行步骤/工具调用审计/输出校验/允许工具集。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /agent-tasks`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /agent-tasks/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`agent.task.completed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S02, S26。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：每个Agent角色独立Deployment/Task Queue/配额，复用运行镜像。

故障恢复：有界循环与检查点；所有模型调用经S26，工具不直接拿密钥。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

编剧/分场/导演等角色工具权限不同；LLM不能绕过预算与接受门。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S20 · Prompt模板与编译服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`prompt-compiler`。

## 责任与所有权

TemplateRevision/供应商映射/CompiledPromptManifest/编译回归结果。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /prompt-compilations`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /templates/{id}/revisions/{revision}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`prompt.compiled`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S24, S10。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：编译CPU与模板评测分离；按模型版本缓存。

故障恢复：必需字段不支持报错；固定模板版本可离线重编译。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

导演意图保留；主体参考顺序正确；对白文本不被编译改写。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S21 · 长任务编排服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`orchestration`。

## 责任与所有权

WorkflowBinding/Run业务投影/任务阶段检查点；执行history由Temporal持有。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /runs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /runs/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`orchestration.run.state-changed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S08, S02, S22, S23。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按cell和任务类型独立Temporal队列；限制扇出与历史大小。

故障恢复：Replay-safe Workflow；所有I/O在Activities；Continue-As-New跨run幂等。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

数千子任务重启后续跑；暂停阻止新准入并继续结算已提交任务。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S22 · 调度与供应商配额服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`scheduler-quota`。

## 责任与所有权

PriorityQueue/QuotaPool/Permit/账号与区域在途槽/公平调度/执行租约。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /permits:acquire`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /quota-pools/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`scheduler.permit.changed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S01, S24。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按account-pool单写计数，跨池水平扩展；租户WFQ+aging。

故障恢复：租约到期不直接释放供应商在途槽；查询确认终态后释放。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

Redis丢失不超并发；账号跨租户共享上限仍严格守恒。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S23 · 预算与成本账本服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`budget-ledger`。

## 责任与所有权

Budget/Reservation/Authorization/UsageAccrual/Settlement/Adjustment/退款与汇率快照。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /budget-authorizations`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /budgets/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`finance.ledger.entry-posted`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按预算scope单写事务；跨租户分区；账单对账独立池。

故障恢复：幂等追加记账；未知提交保留额度；DR先对账再恢复付费准入。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

并发抢额度不越界；预留和实际分离；跨run返工不能绕过集预算。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S24 · 模型能力与价格注册服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`model-registry`。

## 责任与所有权

CapabilitySnapshot/PriceSnapshot/ProviderAccountPool/生命周期/能力探测与健康。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /capability-snapshots`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /model-candidates`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`model.capability.changed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按渠道/版本分片缓存；探测独立低配额。

故障恢复：过期/退役候选被过滤；探测不消耗业务预留外费用。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

七家渠道能力按版本与地区建模；不把历史端点当active。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S25 · 模型路由与估价服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`model-routing`。

## 责任与所有权

RouteDecision/候选拒绝原因/策略版本/报价有效期；不拥有余额或最终任务。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /route-decisions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /route-decisions/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`model.route.selected`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S24, S20, S02。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按候选评分QPS扩容；评估数据带样本数与水位。

故障恢复：无可行候选明确失败；过期报价重新准入；不自动丢必需参考。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

能力先过滤再评分；成本含下游口型和预计返工；决策可解释。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S26 · 统一生成任务协调服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`generation-jobs`。

## 责任与所有权

GenerationAttempt/ProviderJob/SubmissionIntent/准入Saga/幂等键/提交不确定状态。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /generation-jobs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /generation-jobs/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`generation.job.state-changed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S25, S22, S23, S27。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按provider/account/region任务分片；轮询合并退避。

故障恢复：使用预算token与quota permit后持久化提交意图；无供应商幂等的未知提交不重提。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

LLM/VLM/图像/视频/TTS全经本服务；单任务不会因超时盲目重复付费。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S27 · 供应商连接器服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`provider-connectors`。

## 责任与所有权

ConnectorRequestReceipt/供应商响应映射/回调Inbox/轮询水位；ProviderJob权威在S26。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /connector-commands`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /connector-receipts/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`connector.observation.received`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S26。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：Seedance/Kling/Wan/Veo/Sora/Vidu/MiniMax及图像语音渠道逐个独立Deployment与网络身份。

故障恢复：稳定client key、fencing、验签和对账；不能承诺不支持幂等供应商的exactly-once。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

单一供应商故障隔离；重复乱序回调不倒退状态；无业务路由权限。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S28 · 素材归档与制品目录服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`artifact-ingest`。

## 责任与所有权

ArtifactManifest/hash/位置/TTL导入状态/代理/媒体访问权限与保留策略。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /artifact-imports`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /artifacts/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`artifact.ingested`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按媒体bytes/s与导入队列扩容；存储region隔离。

故障恢复：先上传暂存后校验再原子发布manifest；下载失败不重生成。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

供应商URL到期前归档；SSRF/内网/恶意媒体拒绝；不可变hash可验。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S29 · 图像与关键帧生产服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`image-production`。

## 责任与所有权

ImageJob/KeyframeCandidate/编辑mask/参考组合/镜头图像生产检查点。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /image-jobs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /image-jobs/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`production.image.ready`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S26, S28, S10。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：图像生成与编辑按渠道独立队列。

故障恢复：只恢复对应阶段；不将静态修复直接套到所有视频帧。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

多视图参考和mask可追溯；关键帧版本变化传播相关视频失效。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S30 · 视频片段生产服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`video-production`。

## 责任与所有权

VideoJob/ClipCandidate/生成时长与目标时长映射/视频编辑计划。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /video-jobs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /video-jobs/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`production.video.ready`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S26, S28, S29。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：视频请求按时长/分辨率/供应商队列隔离。

故障恢复：已归档候选复用；未知提交交S26；长运镜可拆镜但需导演计划。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

4s目标可选原生或允许裁切档位；对白主动作不被截断。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S31 · 配音生产服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`speech-production`。

## 责任与所有权

SpeechJob/UtteranceRevision/语音候选/韵律与词典绑定。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /speech-jobs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /speech-jobs/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`production.speech.ready`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S15, S26, S28。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按语言、音色账户池与音频时长扩容。

故障恢复：音色授权变更重新验证；失败不偷偷换声线。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

每句对白固定文本和声音版本；原生视频人声不覆盖锁定声音。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S32 · 口型生产服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`lipsync`。

## 责任与所有权

LipsyncJob/说话人-人脸绑定/驱动音频引用/时序编辑结果。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /lipsync-jobs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /lipsync-jobs/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`production.lipsync.ready`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S26, S28, S31。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按媒体时长、嘴部可见窗口/GPU池扩容。

故障恢复：多人/遮挡不支持则转策略决策；边界必须复检。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

口型与锁定音频对齐；低可见度不伪装精确通过。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S33 · 音效生产服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`sound-effects`。

## 责任与所有权

SfxCueRevision/音效素材/位置/授权与生成来源。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /sfx-jobs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /sfx-cues/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`production.sfx.ready`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S26, S28。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按声效生成量与素材检索扩容。

故障恢复：素材不可用则标记缺口，替代需满足既定许可与音效约束。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

脚步/关门等跟随EDL重定位；有独立stem与来源。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S34 · BGM与音乐服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`music`。

## 责任与所有权

MusicCueRevision/曲目授权/生成音乐候选/节拍与情绪/循环点。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /music-jobs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /music-cues/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`production.music.ready`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S26, S28。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：音乐生成和授权素材检索独立配额。

故障恢复：变时长重算切点；不能用未获许可曲目填补。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

BGM版本可替换、独立导出；不遮盖对白并可追溯许可。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S35 · 媒体技术质检服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`technical-qc`。

## 责任与所有权

TechnicalQCReport/解码与黑帧/实际时长/编码/分辨率/响度等测量。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /technical-checks`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /technical-reports/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`quality.technical.completed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S28。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：CPU/IO按媒体时长扩容；媒体沙箱。

故障恢复：坏媒体隔离；确定性检查可幂等重跑。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

截断/空文件/错误时长必须检出；报告不依赖VLM主观判断。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S36 · 视觉语义与时序质检服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`semantic-qc`。

## 责任与所有权

VisualQCReport/身份肢体手部动作机位/时序缺陷证据/采样覆盖。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /semantic-checks`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /semantic-reports/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`quality.semantic.completed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S28, S17, S26。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：VLM任务与本地检测器分池；连续窗口抽检。

故障恢复：评估器超时不当pass；保留abstain和人工复核。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

缺陷有时间段与证据；合法特效/妆造变化不简单误判。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S37 · 音频理解与对齐质检服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`audio-alignment`。

## 责任与所有权

ASR/forced alignment/字音素时间码/说话人/音画偏移与音色评估。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /alignment-jobs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /alignment-reports/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`quality.alignment.completed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S28, S26, S15。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按音频秒数、语言与GPU扩容。

故障恢复：对齐不确定保留置信区间；文本冲突不自动改剧本。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

口型检查使用连续音视频；错读/漏词与字幕偏移有独立证据。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S38 · 字幕与本地化服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`subtitles`。

## 责任与所有权

SubtitleRevision/字幕文本排版/语言/时间码/安全区与交付格式。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /subtitle-jobs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /subtitle-tracks/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`post.subtitle.ready`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S37, S41, S28。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按字幕条数和本地化作业扩容。

故障恢复：EDL变化只重算相关时码；翻译新revision需验收。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

字幕后期合成；对白文本与字幕版本关联，不交给视频模型画字。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S39 · 返工与修复决策服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`repair-planning`。

## 责任与所有权

RepairPlan/缺陷归因/修复动作/尝试上限/局部影响；费用权威在S23。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /repair-plans`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /repair-plans/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`quality.repair.planned`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S35, S36, S37, S09, S02。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按缺陷积压与修复计划QPS扩容。

故障恢复：跨run计数与预算由权威服务检查；次数耗尽交审核或隔离。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

视频修复需时序能力；策略不能套子工作流绕过返工上限。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S40 · 人工审核与接管服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`human-review`。

## 责任与所有权

ReviewTask/分配/决定/理由/委派/审核截止；决定不直接写接受指针。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /review-tasks/{id}/decisions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /review-tasks`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`review.decision.recorded`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S01, S02, S08。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按待审任务/团队队列扩容；审批与读取分池。

故障恢复：旧版本审核标stale；超时按模式等待或阻塞分支，不能伪造通过。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

四档模式门一致；预算增加与创作接受使用不同权限。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S41 · 剪辑与时间线服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`editorial`。

## 责任与所有权

TimelineRevision/EDL/轨道与切点/trim/speed/ripple/混音计划。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /timelines/{id}/revisions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /timelines/{id}/revisions/{revision}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`post.timeline.revision.created`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S08, S28, S19。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按时间线编辑QPS和预览任务扩容。

故障恢复：同timeline乐观并发；保留源媒体不可变。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

4秒修改使下游时码变化；缺必要素材不能提交正式合成。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S42 · 混音与合成渲染服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`composition`。

## 责任与所有权

RenderJob/MixJob/分片检查点/滤镜图/成片候选manifest。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /render-jobs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /render-jobs/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`post.master.rendered`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S41, S38, S33, S34, S28。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按输出像素秒/GPU/CPU独立资源池；长渲染分片。

故障恢复：heartbeat+分片hash恢复；资源限制沙箱；不重复转码原始源。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

音轨与字幕可分离导出；帧边界、色彩、响度与画幅一致。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S43 · 交付与外部分发服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`delivery`。

## 责任与所有权

DeliveryPackage/交付接受状态/签名下载/发布意图与外部回执。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /delivery-packages`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /delivery-packages/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`delivery.package.ready`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S08, S28, S35, S36, S37。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按下载带宽与外部分发渠道独立池。

故障恢复：包准备与外部发布分操作；发布需独立授权和幂等回执。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

无缺口且验收通过才标完整交付；预览水印与正式包明确区分。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

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


---

# S45 · 审计与证据保全服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`audit`。

## 责任与所有权

AppendOnlyAuditRecord/访问与策略决定证据/保留与导出清单。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /audit-records`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /audit-exports/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`audit.export.ready`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：追加写高吞吐；分租户分日归档，可配置WORM。

故障恢复：写入关键命令本地Outbox后异步保全；严格租户配置审计阻塞策略。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

谁改何版本、谁接受、谁授权费用均可追踪；导出可校验完整性。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S46 · 开发者API与MCP服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`developer-integration`。

## 责任与所有权

APIClient/IntegrationCommandReceipt/WebhookSubscription/外部映射；无核心领域事实。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /integrations/commands`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /operations/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`integration.command.accepted`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S01, S02, S07, S21。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：API与MCP独立Deployment；按连接数与QPS扩容。

故障恢复：服务端command_id去重；长工具立即返回operation；支持限流与权限撤销。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

外部Agent可计划和查询；执行需scope与预算；只读不能付费。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S47 · 实时状态与通知投影服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`realtime-projection`。

## 责任与所有权

RunDashboardProjection/SSECursor/NotificationPreference/通知投递回执。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /subscriptions`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /runs/{id}/events`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`projection.cursor.advanced`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S01。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：按长连接数/事件扇出扩容；按tenant/run分片。

故障恢复：投影可重建；cursor过期要求snapshot重同步；断线不重提生成。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

SSE重复乱序去重；通知只按用户配置渠道与权限发送。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---

# S48 · LoRA与模型适配训练服务

状态：企业架构v2设计；独立部署、数据所有权与契约边界，尚未实现。服务标识：`adaptation-training`。

## 责任与所有权

TrainingJob/DatasetManifest/BaseModelCompatibility/LoRAArtifact/Evaluation/训练额度。

仅本服务凭证写入自己的schema/database。其他服务通过契约读取或维护带source_revision的投影；不跨库JOIN，不共享可写ORM模型。媒体bytes存对象存储，业务记录引用不可变ArtifactManifest。

## 输入、输出与依赖

- 命令入口：`POST /training-jobs`。所有写命令包含command_id、expected_revision（适用时）、租户授权上下文与trace_id。
- 查询入口：`GET /adaptation-artifacts/{id}`。返回source_revision、projection_watermark（投影适用）与权限过滤结果。
- 发布事件：`adaptation.training.completed`，遵循企业EventEnvelope v2、Outbox与消费者Inbox。
- 依赖服务：S23, S22, S28, S11, S24。依赖表是运行协作关系，不是所有服务同时启动的拓扑要求；异步环通过事件拆解。
- 事件订阅与命令时序由docs/04及examples/service-interactions.yaml约束；禁止通过调用链直接访问下游数据库。

## 独立运行

扩容：专用GPU队列、长checkpoint、训练与在线推理隔离。

故障恢复：训练checkpoint续跑；基座不兼容禁止挂载；授权撤销停止新训练。

本服务独立ServiceAccount、连接池、迁移、发布版本、资源限制、SLO仪表盘和runbook。外部依赖调用有deadline、熔断与有界重试；付费调用只经生成/预算/配额协议；普通业务读不能自行启动模型推理。

## 一致性与可观测

命令幂等键为tenant_id+command_id+request_hash；同键不同body拒绝。数据库提交与Outbox同事务。消费事件至少一次、按aggregate顺序或版本补偿；最终接受状态由S08、金额由S23、正史由S16分别持有，禁止本服务创建第二权威。

追踪维度：tenant/series/episode/branch/manifest_revision/run/epoch/node/attempt/command/trace。敏感Prompt、音色样本与密钥按docs/14最小化处理。每次状态变化可由快照和事件恢复，Redis不持有唯一事实。

## 专项验收

LoRA基座/hash与数据来源可追溯；训练不占满在线生成资源。

还必须通过：跨租户拒绝、相同命令重复投递、乱序/缺失事件、重启恢复、依赖超时、版本冲突、审计可追溯、独立升级和回滚兼容。


---
