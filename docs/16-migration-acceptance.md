# 16 · 从v0.1到企业v2的实施与验收

## 1. 被替代与保留

| v0.1 | v2处理 |
|---|---|
| 模块化单体控制面+共享业务PG | 48服务+database/schema写隔离，按cell/负载物理隔离 |
| 单个资产模块 | S10目录+S11–S15五类资产+S48训练 |
| 圣经与连续性摘要 | S16结构化正史+S17校验+S18上下文，S09跨集依赖 |
| 配置式人工门 | S02独立策略+S40审核，四档与两开关 |
| 单model-gateway | 注册S24、路由S25、任务S26、每渠道连接器S27 |
| 合并QC/剪辑/成本 | S35–S39、S41–S43、S22/S23/S44独立 |
| 10集并行容量 | 可调cell容量模型与大规模规划，供应商配额另验 |
| 任意Agent直接业务工具 | S19角色独立池+typed tools+S26统一计费准入 |

保留：不可变素材、导演DSL、字段依赖图、Temporal执行、未知提交停止重提、预算有界返工。旧v1领域schema只作为细化payload；v2显式补上下文与权威边界。

## 2. 建议仓库与交付结构

```text
drama-enterprise/
  apps/studio-web/ studio-bff/ api-gateway/ developer-api/ mcp-server/
  services/<service-slug>/
    src/ api/ events/ migrations/ tests/ deploy/ runbooks/
    service.yaml              # owner、SLO、数据与依赖、发布版本
  agents/<role>/              # 独立definition、工具策略、队列与部署
  connectors/<provider>/<channel>/
  contracts/openapi/ events/ schemas/ generated-clients/
  platform/auth-sdk/ event-sdk/ telemetry-sdk/ idempotency-sdk/
  workflows/episode/ shot/ memory-migration/ admission/ repair/
  infra/global/ regions/<region>/cells/<cell>/
  evals/datasets/ rubrics/ manifests/ reports/
  customer-configs/<tenant>/   # 无密钥，版本化策略与模板
  delivery/playbooks/ capacity/ acceptance/ training/
```

services内不可互import对方业务ORM；SDK只承载primitive协议。目录可按团队改成多repo，服务契约和独立发布条件不变。

## 3. 分波次实现（不执行本包）

W0平台契约/租户身份/事件/密钥/部署脚手架；W1 S08/S09/S21/S22/S23/S24/S26/S27/S28，用模拟模型验证版本、成本、恢复；W2 S02/S40与S16–S18先贯通四档与跨集正史；W3 S03–S15/S19/S20/S29–S39实现完整创作生产质量；W4 S41–S47工作台/后期/交付/评测/审计及企业连接；W5 S48按客户需要训练，供应商扩展、性能、灾备与安全验收贯穿各波。

波次不是固定工期承诺。各服务可按契约使用mock依赖，不因同波未全部完成而共用数据库。只在实施会话明确授权后运行真实付费生成/部署/外部分发。

## 4. 升级已有v0.1实现时

若已有旧系统，先冻结写所有权、清点历史ID/资产/费用，再按strangler迁移。某事实始终单写：旧库CDC到新服务→对账→冻结相关旧写→切新写→事件投影供旧读→验收→移除旧代码。不要同时双向写两个余额/accepted pointer。

历史任务保留原provider IDs和幂等键，进入S26接管前查询对账；未知提交不重跑。历史记忆从已接受脚本提取候选，来源校验后提交；不可将所有聊天和未用候选导成正史。现有云工作区只有设计文件，本次没有实施迁库。

## 5. 企业验收矩阵

| ID | 场景 | 必须满足 |
|---|---|---|
| E01 | 48服务独立构建发布 | 无跨服务DB写/ORM导入；owner、迁移、告警可见 |
| E02 | 三集连续生产 | 人物/声音/状态/伏笔有版本来源与最终交付 |
| E03 | 仰拍4秒局部改镜 | 字段差异、派生节拍、成本、最小依赖闭包正确 |
| E04 | 缺集上下文/对白超过4秒 | 澄清或不可行，不猜目标/删台词 |
| E05 | 四档×项目/集/阶段/节点 | 继承、覆盖、自动生成/接受两开关符合策略 |
| E06 | 无人值守关键镜头失败 | 其他分支可继续；INCOMPLETE报告，不假完整交付 |
| E07 | 手工锁定/旧审核晚到 | 旧epoch不能改变当前接受指针 |
| E08 | 改前集钥匙归属 | 新checkpoint与后续集影响计划可追溯 |
| E09 | 闪回/观众先知/角色不知 | story time和knowledge scope正确 |
| E10 | 正史分支与候选抽取 | 草稿隔离、合并冲突可裁决、回滚留历史 |
| E11 | 记忆/依赖索引落后 | watermark可见，强依赖等待/回源，不漏关键事实 |
| E12 | 同command重复/不同body | 同结果或409，无重复版本/费用 |
| E13 | S08提交前后服务崩溃 | 准备内容可回收、manifest与Outbox一致、恢复无假接受 |
| E14 | 预算成功、quota失败 | 预算补偿，不能进入dispatch |
| E15 | quota已mark、预算确认超时 | 不付费提交，Saga恢复后明确结果 |
| E16 | submit响应丢失/供应商无幂等 | UNKNOWN保留额度，先对账，不自动再提交 |
| E17 | 回调先到/重复/乱序/伪签名 | Inbox与映射恢复正确、终态不倒退、伪签名拒绝 |
| E18 | 429/5xx/账号撤销/模型退休 | 有界退避、隔离、硬能力不降级、禁用路由 |
| E19 | 候选便宜但无主体参考 | 硬过滤、有拒绝原因 |
| E20 | 并发争抢多级最后额度 | 不超授权总额，父子escrow不双花 |
| E21 | Redis清空/调度重启 | 不丢Run，不超provider在途和速率 |
| E22 | 暂停取消不可取消任务 | 不再准入、继续结算，取消不假退款 |
| E23 | 供应商URL即将过期/下载失败 | 优先归档、重下载不重生成、最终缺口可见 |
| E24 | 静态/时序/口型/字幕缺陷 | 证据时间段与coverage、修复后复检、预算上限 |
| E25 | 评估器超时与冲突 | abstain/审核，不将未知当pass |
| E26 | 跨Run返工 | 尝试和费用累计，不能绕过镜头/集上限 |
| E27 | 企业SSO撤权/越权MCP | 用户与服务身份权限及时生效 |
| E28 | 跨租户素材/记忆/hash存在性 | 拒绝且不泄漏存在性 |
| E29 | 恶意媒体/SSRF/命令注入 | 导入器与媒体沙箱隔离 |
| E30 | BYOK轮转与音色授权撤销 | 不借其他租户Key，不用撤销声音新调用 |
| E31 | 数据驻留与备份 | Prompt/向量/日志/供应商/备份全部符合地区政策 |
| E32 | Kafka重放与DLQ恢复 | 投影可恢复，不重发付费/发布/通知副作用 |
| E33 | Temporal升级/全部Worker停机 | 回放兼容、历史版本可用、检查点续跑 |
| E34 | Region灾备/旧执行器未知 | 单写fence和对账，不能盲重提付费 |
| E35 | 所设容量与noisy neighbor | 模拟负载下SLO、份额、背压，真实配额独验 |
| E36 | UI断线与SSE cursor过期 | 重同步不重复提交，旧投影有水位 |
| E37 | 缺素材/预览/正式交付 | 包类型明确，正式Master满足完整性门 |
| E38 | 费用与效果重算 | 原始账本、分摊、cohort、失败样本、人工指标一致 |
| E39 | 审计/保留/删除/训练数据 | 可证明操作链、来源、销毁和例外保留 |
| E40 | FDE移交 | 客户验收、培训、值班、容量与责任边界签字 |

## 6. 实施Agent入口

先读README、01/02/04/05/06/12/16与相关service spec；核实目标仓库AGENTS、云、账号、预算与客户合同。按波次创建独立服务、契约与mock，先验证E12–E23和E05/E08–E11，再接真实模型。所有生产开关默认关闭。输出可审阅变更与验证证据；不能把设计schema合法等同于分布式行为已正确。
