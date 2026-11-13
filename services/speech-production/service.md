# S31 · 配音生产

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 生成 |
| 团队 | 生成 |
| schema | `speech_production` |
| 命令入口 | `POST /speech-jobs` |
| 查询入口 | `GET /speech-jobs/{id}` |

设计说明：[S31-speech-production.md](../../docs/design-package/services/S31-speech-production.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
