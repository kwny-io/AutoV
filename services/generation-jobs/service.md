# S26 · 统一生成任务协调

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 执行 |
| 团队 | 执行 |
| schema | `generation_jobs` |
| 命令入口 | `POST /generation-jobs` |
| 查询入口 | `GET /generation-jobs/{id}` |

设计说明：[S26-generation-jobs.md](../../docs/design-package/services/S26-generation-jobs.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
