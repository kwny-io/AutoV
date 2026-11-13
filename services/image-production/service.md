# S29 · 图像与关键帧生产

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 生成 |
| 团队 | 生成 |
| schema | `image_production` |
| 命令入口 | `POST /image-jobs` |
| 查询入口 | `GET /image-jobs/{id}` |

设计说明：[S29-image-production.md](../../docs/design-package/services/S29-image-production.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
