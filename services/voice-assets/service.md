# S15 · 音色与声音授权

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 资产 |
| 团队 | 资产 |
| schema | `voice_assets` |
| 命令入口 | `POST /voices/{id}/revisions` |
| 查询入口 | `GET /voices/{id}/bindings` |

设计说明：[S15-voice-assets.md](../../docs/design-package/services/S15-voice-assets.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
