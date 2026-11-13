# S46 · 开发者API与MCP

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 体验 |
| 团队 | 体验 |
| schema | `developer_integration` |
| 命令入口 | `POST /integrations/commands` |
| 查询入口 | `GET /operations/{id}` |

设计说明：[S46-developer-integration.md](../../docs/design-package/services/S46-developer-integration.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
