# 14 · 企业治理、SRE、发布与容灾

## 1. 身份、权限与凭证

企业IdP经OIDC或SAML联邦；SSO、MFA由企业IdP政策落实，SCIM成员生命周期可选接入。人类、API client、MCP client与workload身份分开。RBAC给角色基线，ABAC约束tenant/project、资源状态、预算scope、region、目的、审批职责。高风险预算/外部分发/音色资格操作单独权限；组织管理员不自动获得素材创作版权。

服务mTLS/短期workload token，audience绑定、最小scope、NetworkPolicy禁止任意横向访问。供应商/BYOK凭证存密钥管理器，只注入对应S27渠道或获准推理池；前端、Agent上下文、日志与事件不含密钥值。轮转保留账户映射版本，不在在途任务中混用其他企业凭证。

## 2. 数据隔离与驻留

每服务专用DB角色、tenant复合键与RLS双重保护；管理/后台任务也带tenant scope。S3路径不等于授权，使用短签名与对象级检查；租户间去重与索引不能泄漏存在性。高等级客户专属DB/key/namespace/cell，按合同选择。

驻留覆盖剧本、参考图、音色、Prompt、记忆、向量索引、日志、备份与供应商处理地区；不仅是bucket所在地区。跨境模型不满足政策则路由剔除。共享全球目录仅放最小路由元数据，数据分类清单与出境接入由交付阶段确认。

S28导入限制协议、文件/MIME、重定向、大小、解码时间、DNS重绑定与内网/元数据访问。FFmpeg/图像/字幕解析运行非特权无网络沙箱，路径和滤镜参数结构化生成，禁止把用户文字拼shell。训练样本与声音样本采取严格访问、保留和撤销流程。

## 3. 观测与运行手册

统一trace：command→manifest→workflow→node→attempt→budget/permit→provider→ingest→QC→review→master→delivery→ledger。label避免原始Prompt和高基数用户文本；ID保留在trace/log而非全部作为metrics label。

告警：oldest queue age、permit与实际在途差、unknown submission、outbox/Inbox lag、memory watermark lag、下载TTL逼近、归档失败、QC误通过率、budget drift、render失败、SSE投影落后、跨租户拒绝激增。每服务有owner、值班、严重级别、恢复步骤、回放边界与客户通知模板。

审计S45保存谁以什么身份、基于何政策、改了哪些引用、批准多少费用、接受哪个版本。关键命令同事务审计Outbox；审计出口故障先落本地持久Outbox，严格客户达到保留缓冲上限时停止关键写，不静默丢审计。

## 4. 发布与契约演进

每服务独立CI、schema迁移、镜像签名/SBOM与部署。expand→migrate→contract，先兼容读取旧字段再清理；不能在长任务仍依赖时删除模型模板、代码版本或schema。OpenAPI/事件兼容检查、消费者契约、Temporal replay与样例回归进入发布门。

先staging→cell canary→租户canary→扩大。失败按服务/策略/模板分别回退；DB不兼容迁移使用前向修复而非随意降级。Temporal Worker版本固定到Run，旧Worker保留到迁移或任务结束。共享SDK升级按语义版本，不强制48服务同日发布。

## 5. 故障与容灾

单region多可用区，关键数据同步复制与PITR；跨region备份依驻留许可。规划目标：关键本地故障已确认命令不丢（需同步持久化配置验证）；灾难恢复RPO≤5分钟、RTO≤60分钟为待演练目标，非设计即保证。媒体备份与供应商临时URL不能互相替代。

恢复顺序：隔离旧执行面→恢复身份/租户home路由与数据→验证S08/S16/S23/S26权威版本→恢复事件与Temporal→对账所有SUBMITTING/UNKNOWN/INFLIGHT→重建索引投影→保守开放新准入→扩大流量。

同一付费任务跨cell只能有一个fenced写主；灾备异步复制可能丢失最近submit记录，因此仅有新region锁不证明旧请求未发生。若无法证明旧执行器停止、恢复最新提交意图或从供应商对账，保持付费提交冻结，并报告恢复范围。控制台可先恢复读与历史下载，不为了RTO盲重发付费任务。

必须演练Postgres故障、Kafka重放、Redis清空、Temporal Worker全停、对象存储局部失败、供应商中断、region迁移、凭证撤销、导入恶意媒体、队列热点与审计出口故障。恢复演练记录真实RPO/RTO与重复费用情况。
