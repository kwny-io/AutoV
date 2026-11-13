# S48 · LoRA与模型适配训练

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 资产 |
| 团队 | 资产 |
| schema | `adaptation_training` |
| 命令入口 | `POST /training-jobs` |
| 查询入口 | `GET /adaptation-artifacts/{id}` |

设计说明：[S48-adaptation-training.md](../../docs/design-package/services/S48-adaptation-training.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
