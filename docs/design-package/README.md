# 企业级短剧生产平台 · 架构 v2

设计日期：2026-10-29（Asia/Taipei）。阶段：设计交接，未实现/部署业务服务，未调用付费生成模型。此版本取代v0.1的模块化单体、共享业务数据库、10集并发假设和粗粒度模块边界。

产品目标：让企业团队与外部Agent在固定质量、授权、预算与交付要求下，进行跨集连续短剧的创作、生成、审片、修改和批量交付。衡量每可用成片分钟成本、交付周期、成片可用率与人工投入。

## 本次全面升级

- **48个独立业务服务**：每个服务有独立数据所有权、部署单元、命令/查询、事件、扩缩容、故障恢复与验收；共享的是基础框架和契约，不共享可写业务模型。
- **完整跨集记忆域**：正史、事件、人物状态/关系/知情范围、伏笔、故事时间和分支；记忆与检索、连续性校验分别独立。
- **四档人工参与**：逐步制作、阶段审核、异常接管、无人值守；项目/集/阶段/节点覆盖，生成与接受分开，权限/授权/预算硬约束不可绕过。
- **大规模运行**：全局控制面+地域cell执行面，租户单home-cell、队列隔离、公平调度、供应商在途配额、成本准入Saga、重放与灾备。
- **企业治理**：SSO/OIDC/SAML适配、组织/项目权限、服务身份、专属租户部署、数据驻留、审计、BYOK凭证、保留与删除。
- **完整交付契约**：服务目录、v2 JSON Schema、核心OpenAPI设计、事件Topic目录、流程样例、自动化策略、容量计算、实施与验收矩阵。

## 入口与阅读顺序

1. [总文档](DESIGN.md)：所有架构章节合并，便于交接。
2. [企业架构总览](../../docs/01-enterprise-architecture.md)与[48服务目录](../../docs/02-service-catalog.md)。
3. [部署与容量](../../docs/03-deployment-capacity.md)、[数据与分布式事务](../../docs/04-data-consistency.md)。
4. [跨集记忆](../../docs/05-cross-episode-memory.md)、[人工参与分层](../../docs/06-automation-governance.md)。
5. [生产主流程](../../docs/07-production-workflows.md)、[Agent与Prompt](../../docs/08-agents-prompts.md)、[模型与任务](../../docs/09-model-execution.md)。
6. [资产域](../../docs/10-assets.md)、[质量域](../../docs/11-quality-repair.md)、[成本与调度](../../docs/12-budget-scheduling.md)。
7. [API与工作台](../../docs/13-api-product.md)、[企业治理与容灾](../../docs/14-enterprise-operations.md)。
8. [业务交付与评测](../../docs/15-fde-business-evaluation.md)、[迁移实施与验收](../../docs/16-migration-acceptance.md)。

`services/`：每个服务一份独立边界规格。`contracts/`：机器可读契约。`examples/`：策略、事件、场景与容量样例。`diagrams/`：Mermaid源图。`layout.html`：企业工作台静态布局。`VALIDATION.md`：静态检查范围与结果。

快速审阅：[升级摘要](UPGRADE.md) · [独立服务规格汇编](SERVICE-SPECIFICATIONS.md) · [企业工作台布局](../../apps/studio-web/layout.html) · [验证记录](VALIDATION.md) · [文件校验清单](MANIFEST.md)。

## 与旧版的关系

v0.1中的ShotSpec、GenerationRequest、QCReport作为[领域负载基线](../../contracts/domain-v1/README.md)保留，外包v2租户/分支/cell/manifest/epoch/授权上下文；不得继续采用旧版单体部署、全局共享业务DB或只靠episode_revision覆盖并发。

Sora/Veo等系列保留连接器位置，当前渠道可用性以实施时的官方生命周期与账户探测为准。旧版已核查的停用端点继续禁用，不借本次架构升级声称模型已经恢复。所有供应商生产开关默认关闭。

## 设计与实现边界

容量、SLO、RPO/RTO均是待基准测试和商业配额验证的规划值。该包没有可运行微服务、真实密钥或供应商能力认证，不是已完成的企业系统。48个边界可以分波次交付，但生产边界从第一波就遵循独立数据和契约规则。
