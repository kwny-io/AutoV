# S27 · 供应商连接器

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 执行 |
| 团队 | 执行 |
| schema | `provider_connectors` |
| 命令入口 | `POST /connector-commands` |
| 查询入口 | `GET /connector-receipts/{id}` |

设计说明：[S27-provider-connectors.md](../../docs/design-package/services/S27-provider-connectors.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
