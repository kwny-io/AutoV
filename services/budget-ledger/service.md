# S23 · 预算与成本账本

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | 财务 |
| 团队 | 财务 |
| schema | `budget_ledger` |
| 命令入口 | `POST /budget-authorizations` |
| 查询入口 | `GET /budgets/{id}` |

设计说明：[S23-budget-ledger.md](../../docs/design-package/services/S23-budget-ledger.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
