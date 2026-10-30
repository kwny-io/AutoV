# S47 · 实时状态与通知投影

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | 体验 |
| 团队 | 体验 |
| schema | `realtime_projection` |
| 命令入口 | `POST /subscriptions` |
| 查询入口 | `GET /runs/{id}/events` |

设计说明：[S47-realtime-projection.md](../../docs/design-package/services/S47-realtime-projection.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
