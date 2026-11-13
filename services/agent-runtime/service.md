# S19 · Agent运行与工具沙箱

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | Agent |
| 团队 | Agent |
| schema | `agent_runtime` |
| 命令入口 | `POST /agent-tasks` |
| 查询入口 | `GET /agent-tasks/{id}` |

设计说明：[S19-agent-runtime.md](../../docs/design-package/services/S19-agent-runtime.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
