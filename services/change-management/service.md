# S07 · 对话修改与变更计划

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | 控制面 |
| 团队 | 工作流 |
| schema | `change_management` |
| 命令入口 | `POST /change-plans/{id}:apply` |
| 查询入口 | `GET /change-plans/{id}` |

设计说明：[S07-change-management.md](../../docs/design-package/services/S07-change-management.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
