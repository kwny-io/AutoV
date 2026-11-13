# S33 · 音效生产

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 生成 |
| 团队 | 生成 |
| schema | `sound_effects` |
| 命令入口 | `POST /sfx-jobs` |
| 查询入口 | `GET /sfx-cues/{id}` |

设计说明：[S33-sound-effects.md](../../docs/design-package/services/S33-sound-effects.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
