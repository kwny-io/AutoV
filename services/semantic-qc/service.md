# S36 · 视觉语义与时序质检

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | 质量 |
| 团队 | 质量 |
| schema | `semantic_qc` |
| 命令入口 | `POST /semantic-checks` |
| 查询入口 | `GET /semantic-reports/{id}` |

设计说明：[S36-semantic-qc.md](../../docs/design-package/services/S36-semantic-qc.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
