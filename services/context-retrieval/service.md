# S18 · 上下文检索与组装

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | 记忆 |
| 团队 | 记忆 |
| schema | `context_retrieval` |
| 命令入口 | `POST /context-packs` |
| 查询入口 | `GET /context-packs/{id}` |

设计说明：[S18-context-retrieval.md](../../docs/design-package/services/S18-context-retrieval.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
