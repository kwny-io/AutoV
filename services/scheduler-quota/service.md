# S22 · 调度与供应商配额

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | 执行 |
| 团队 | 执行 |
| schema | `scheduler_quota` |
| 命令入口 | `POST /permits:acquire` |
| 查询入口 | `GET /quota-pools/{id}` |

设计说明：[S22-scheduler-quota.md](../../docs/design-package/services/S22-scheduler-quota.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
