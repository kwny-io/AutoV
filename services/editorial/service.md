# S41 · 剪辑与时间线

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 后期 |
| 团队 | 后期 |
| schema | `editorial` |
| 命令入口 | `POST /timelines/{id}/revisions` |
| 查询入口 | `GET /timelines/{id}/revisions/{revision}` |

设计说明：[S41-editorial.md](../../docs/design-package/services/S41-editorial.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
