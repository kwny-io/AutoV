# S38 · 字幕与本地化

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 后期 |
| 团队 | 后期 |
| schema | `subtitles` |
| 命令入口 | `POST /subtitle-jobs` |
| 查询入口 | `GET /subtitle-tracks/{id}` |

设计说明：[S38-subtitles.md](../../docs/design-package/services/S38-subtitles.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
