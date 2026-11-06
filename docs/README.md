# 设计文档

17 篇，从设计包迁入。**内容未改动，仅修正了链接路径。**

## 阅读顺序

按这个顺序，不要跳：

| # | 文档 | 内容 |
|---|---|---|
| 1 | [design-package/README.md](design-package/README.md) | 适用范围与免责声明 |
| 2 | [01-enterprise-architecture.md](01-enterprise-architecture.md) | 服务边界与唯一权威表 |
| 3 | [02-service-catalog.md](02-service-catalog.md) | 48 服务全表 |
| 4 | [04-data-consistency.md](04-data-consistency.md) | 版本、epoch、准入 Saga |
| 5 | [16-migration-acceptance.md](16-migration-acceptance.md) | 波次计划与 E01–E40 |

## 全部文档

| 文档 | 内容 |
|---|---|
| [01](01-enterprise-architecture.md) | 企业架构、域划分、EADR 决策 |
| [02](02-service-catalog.md) | 48 服务目录 |
| [03](03-deployment-capacity.md) | 控制面与 cell、容量、SLO |
| [04](04-data-consistency.md) | 身份与版本字段、manifest CAS、两套 epoch、Saga |
| [05](05-cross-episode-memory.md) | 跨集记忆十类实体、双时间、分支 |
| [06](06-automation-governance.md) | 四档模式、双开关、策略继承 |
| [07](07-production-workflows.md) | 整集流程、局部改镜、停止语义、交付物 |
| [08](08-agents-prompts.md) | 13 个 Agent 角色、AgentTask、导演 DSL 编译 |
| [09](09-model-execution.md) | S24→S25→S26→S27、连接器协议、降级链 |
| [10](10-assets.md) | 资产服务与生命周期、BindingSet |
| [11](11-quality-repair.md) | QC 契约、修复优先级、尝试上限 |
| [12](12-budget-scheduling.md) | 多级预算、配额许可、成本归因 |
| [13](13-api-product.md) | 网关/BFF/开发者 API/MCP/工作台 |
| [14](14-enterprise-operations.md) | SSO、RBAC/ABAC、BYOK、追踪、DR |
| [15](15-fde-business-evaluation.md) | FDE 交付物、KPI、灰度 |
| [16](16-migration-acceptance.md) | 仓库结构、W0–W5、E01–E40 |
| [17](17-provider-evidence-baseline.md) | 供应商生命周期证据（2026-10-29） |

## 存档

`design-package/` 是原始设计包。保留了 `DESIGN.md`（849 行合并版）、
`SERVICE-SPECIFICATIONS.md`（48 份拼合）、`MANIFEST.md`、`VALIDATION.md`
和 `services/` 下 48 份原始服务规格。

这些文件的内容与原始 zip 一致，只改过链接路径。
