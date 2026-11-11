# S01 · 租户与访问

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 企业 |
| 团队 | 平台 |
| schema | `tenant_access` |
| 命令入口 | `POST /access-grants` |
| 查询入口 | `GET /authorization-context` |

设计说明：[S01-tenant-access.md](../../docs/design-package/services/S01-tenant-access.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
