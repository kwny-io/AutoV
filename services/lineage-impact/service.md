# S09 · 依赖溯源与影响分析

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 控制面 |
| 团队 | 工作流 |
| schema | `lineage_impact` |
| 命令入口 | `POST /lineage-edges` |
| 查询入口 | `POST /impact-queries` |

设计说明：[S09-lineage-impact.md](../../docs/design-package/services/S09-lineage-impact.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
