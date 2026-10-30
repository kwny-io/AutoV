# S08 · 版本快照与接受状态

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | 控制面 |
| 团队 | 工作流 |
| schema | `revision_snapshot` |
| 命令入口 | `POST /manifests/{id}:commit` |
| 查询入口 | `GET /manifests/{id}/revisions/{revision}` |

设计说明：[S08-revision-snapshot.md](../../docs/design-package/services/S08-revision-snapshot.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
