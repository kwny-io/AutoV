# S02 · 治理与自动化策略

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 企业 |
| 团队 | 平台 |
| schema | `governance_policy` |
| 命令入口 | `POST /policies/{id}/revisions` |
| 查询入口 | `POST /policy-decisions:evaluate` |

设计说明：[S02-governance-policy.md](../../docs/design-package/services/S02-governance-policy.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
