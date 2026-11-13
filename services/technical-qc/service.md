# S35 · 媒体技术质检

状态：`implemented` —— 已实现最小内核，尚未部署。

| 项 | 值 |
|---|---|
| 域 | 质量 |
| 团队 | 质量 |
| schema | `technical_qc` |
| 命令入口 | `POST /technical-checks` |
| 查询入口 | `GET /technical-reports/{id}` |

设计说明：[S35-technical-qc.md](../../docs/design-package/services/S35-technical-qc.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
