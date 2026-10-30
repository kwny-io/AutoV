# S13 · 场景与空间资产

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | 资产 |
| 团队 | 资产 |
| schema | `scene_assets` |
| 命令入口 | `POST /scene-assets/{id}/revisions` |
| 查询入口 | `GET /scene-assets/{id}/revisions/{revision}` |

设计说明：[S13-scene-assets.md](../../docs/design-package/services/S13-scene-assets.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
