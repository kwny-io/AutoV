# S21 · 长任务编排

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | 控制面 |
| 团队 | 工作流 |
| schema | `orchestration` |
| 命令入口 | `POST /runs` |
| 查询入口 | `GET /runs/{id}` |

设计说明：[S21-orchestration.md](../../docs/design-package/services/S21-orchestration.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
