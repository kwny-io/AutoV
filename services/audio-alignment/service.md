# S37 · 音频理解与对齐质检

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 质量 |
| 团队 | 质量 |
| schema | `audio_alignment` |
| 命令入口 | `POST /alignment-jobs` |
| 查询入口 | `GET /alignment-reports/{id}` |

设计说明：[S37-audio-alignment.md](../../docs/design-package/services/S37-audio-alignment.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
