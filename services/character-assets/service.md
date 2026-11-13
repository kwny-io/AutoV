# S11 · 角色身份资产

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 资产 |
| 团队 | 资产 |
| schema | `character_assets` |
| 命令入口 | `POST /characters/{id}/revisions` |
| 查询入口 | `GET /characters/{id}/revisions/{revision}` |

设计说明：[S11-character-assets.md](../../docs/design-package/services/S11-character-assets.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
