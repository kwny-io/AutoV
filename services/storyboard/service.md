# S06 · 分镜与导演规范

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 创作 |
| 团队 | 创作 |
| schema | `storyboard` |
| 命令入口 | `POST /storyboards/{id}/revisions` |
| 查询入口 | `GET /shots/{id}/revisions/{revision}` |

设计说明：[S06-storyboard.md](../../docs/design-package/services/S06-storyboard.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
