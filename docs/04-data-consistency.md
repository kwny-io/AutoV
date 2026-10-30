# 04 · 数据所有权、事件与分布式一致性

## 1. 标准身份与版本

每条命令携带command_id、tenant_id、project/series、branch、home_cell、manifest_ref、epoch、trace_id、actor/service principal、目的与预算scope。tenant/actor从认证派生，header不能自授身份。service-to-service用mTLS/workload identity，授权上下文绑定请求hash与audience。

内容对象immutable；revision整数只在对象内有意义，跨服务以`service/id/revision/content_hash`定位。S08保存manifest，将各服务内容引用拼成一致的生产视图，并以manifest_revision和epoch做CAS。各服务之间不要求同时提交数据库。

epoch分两层：Run/branch的execution_fence用于取消或主写切换；每个logical_node的node_epoch用于局部生成版本。改一个镜头只提升受影响节点的node_epoch，不使其他在途镜头全部过期。S08接入结果时检查node_epoch、input_fingerprint、asset/memory/policy引用和execution_fence；若仅其他镜头改变manifest，可基于最新manifest重新CAS合并本节点，不覆盖其他指针。manifest_ref是溯源和并发基础，不能单凭“不是最新manifest”丢掉所有无关任务。

## 2. 修改与接受Saga

S07生成计划，S09验证影响闭包，S02验证规则 → 各内容所有者准备新revision（未接受） → S08原子提交manifest+epoch+Outbox → S21消费事件调度新子图。S08提交失败时准备产物保持unreferenced，可在保留期后GC；不回滚已经提交的事实为“原地旧值”。

关键前置版本在S08 commit时再次校验；ChangePlan包含base_manifest、source hashes、derived adjustments、impact_watermark、policy_version、memory_checkpoint与expiry。依赖索引落后时先补齐必需边或保守重检，不能按不完整图承诺局部失效。

接受候选：S35–S37的QC report引用同一artifact hash；S40人工决定引用同一manifest/epoch；S02判定接受门；S08 CAS更新accepted pointer。S40不写S08数据库，QC服务也不能自动改全剧资产canonical。

## 3. 预算与配额准入Saga

1. S26固定attempt_id、request_hash、报价上界；请求S23创建预算reservation。
2. S22按预算reservation和路由账户池申请permit；失败补偿释放尚未提交的reservation。
3. 两者成功后S26持久化SubmissionIntent+Outbox，绑定token、permit、报价、epoch、执行fence。
4. S27执行器在付费前调用S26的dispatch授权；S26幂等确认S23资金仍已授权、S22 permit转为submitted/inflight，双方返回持久ack后签发绑定request_hash的一次性dispatch grant。
5. 任一ack不明时不提交，继续恢复Saga；已有submitted permit与reservation不按普通TTL回收。
6. 调用结果不明进入SUBMISSION_UNKNOWN，保持费用与在途占用，查询/对账后决定；不将网络重试当免费安全操作。
7. 权威终态后S23暂记/对账实际费用、释放剩余预留，S22释放在途permit。每一步有唯一业务事件键，重复投递无重复扣减。

这是一组可恢复的本地事务与补偿，不宣称跨DB原子事务。供应商不支持幂等时，dispatch grant只允许一个fenced执行器领取一次；领取后崩溃产生未知提交，不重新领取同任务自动重发。供应商侧exactly-once不能由本平台单方面保证。

## 4. 事件标准

区域Kafka兼容总线；事件envelope采用CloudEvents1.0字段加领域上下文：id/source/type/subject/time/specversion、schema_version、tenant/home_cell、aggregate_id/sequence、correlation_id/causation_id、traceparent、data、data_ref（可选大负载引用）。Topic目录见contracts/event-topics.v2.json。

Outbox与本地事实同事务；CDC或publisher至少一次投递。消费者Inbox按consumer+event_id去重；聚合sequence缺口可缓冲并回源，不能无限等待；状态终态不得被旧事件倒退。回放使用独立consumer group和side_effects=false，避免重发外部消息、费用调用或发布。

事件正常保留示例14日，长期事实恢复靠service snapshots+授权归档，不把Kafka唯一当永久账本。财务/审计保留期由客户合同和地区规则配置。DLQ携带错误分类、重试次数、原引用与重放权限；告警与运营队列可见。

Schema Registry执行兼容规则；字段新增为可选，变语义/删除走新major topic/schema，双读双写迁移有结束时间。媒体bytes、克隆音频、密钥和完整剧本不放普通事件，使用权限化data_ref。

## 5. 事件最终一致性的可见性

每个投影返回watermark与source_revision。需要强一致的余额、当前manifest、音色授权、正史checkpoint回源权威服务。S47页面不能把旧进度显示成当前已完成；S18不能把尚未投影的记忆当不存在。

读到未提交候选只可用于预览，必须带candidate标记。最终交付的证据包固定manifest+memory checkpoint+asset bindings+QC+政策+成本时间点，确保后续重算和审计可解释。

## 6. 生命周期与删除

S28内容hash去重只在租户/授权边界内进行，禁止通过跨租户hash存在性泄漏素材。删除按引用图、法律保留、发布引用和训练引用检查；先tombstone禁用新调用，再按服务编排删除/索引清除/备份到期清除。审计与财务记录可保留最小证明，不保留被删除媒体原文。密钥销毁是否满足删除要求由部署政策定义并验证。
