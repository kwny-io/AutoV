# S45 · 审计与证据保全

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 企业 |
| 团队 | 平台 |
| schema | `audit` |
| 命令入口 | `POST /audit-records` |
| 查询入口 | `GET /audit-exports/{id}` |

设计说明：[S45-audit.md](../../docs/design-package/services/S45-audit.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
