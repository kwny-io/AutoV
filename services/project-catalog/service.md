# S03 · 项目与剧集目录

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | 创作 |
| 团队 | 创作 |
| schema | `project_catalog` |
| 命令入口 | `POST /projects/{id}/episodes` |
| 查询入口 | `GET /projects/{id}/episodes` |

设计说明：[S03-project-catalog.md](../../docs/design-package/services/S03-project-catalog.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
