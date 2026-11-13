# S32 · 口型生产

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 生成 |
| 团队 | 生成 |
| schema | `lipsync` |
| 命令入口 | `POST /lipsync-jobs` |
| 查询入口 | `GET /lipsync-jobs/{id}` |

设计说明：[S32-lipsync.md](../../docs/design-package/services/S32-lipsync.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
