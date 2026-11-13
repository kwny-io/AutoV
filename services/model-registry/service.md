# S24 · 模型能力与价格注册

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 模型 |
| 团队 | 模型 |
| schema | `model_registry` |
| 命令入口 | `POST /capability-snapshots` |
| 查询入口 | `GET /model-candidates` |

设计说明：[S24-model-registry.md](../../docs/design-package/services/S24-model-registry.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
