# S17 · 连续性校验

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 记忆 |
| 团队 | 记忆 |
| schema | `continuity` |
| 命令入口 | `POST /continuity-checks` |
| 查询入口 | `GET /continuity-reports/{id}` |

设计说明：[S17-continuity.md](../../docs/design-package/services/S17-continuity.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
