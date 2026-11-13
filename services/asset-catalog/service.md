# S10 · 资产目录与绑定解析

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 资产 |
| 团队 | 资产 |
| schema | `asset_catalog` |
| 命令入口 | `POST /binding-sets` |
| 查询入口 | `GET /assets/{id}/resolve` |

设计说明：[S10-asset-catalog.md](../../docs/design-package/services/S10-asset-catalog.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
