# S40 · 人工审核与接管

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | 治理 |
| 团队 | 平台 |
| schema | `human_review` |
| 命令入口 | `POST /review-tasks/{id}/decisions` |
| 查询入口 | `GET /review-tasks` |

设计说明：[S40-human-review.md](../../docs/design-package/services/S40-human-review.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
