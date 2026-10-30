# S20 · Prompt模板与编译

状态：`design_only` —— 尚未实现。

| 项 | 值 |
|---|---|
| 域 | Agent |
| 团队 | Agent |
| schema | `prompt_compiler` |
| 命令入口 | `POST /prompt-compilations` |
| 查询入口 | `GET /templates/{id}/revisions/{revision}` |

设计说明：[S20-prompt-compiler.md](../../docs/design-package/services/S20-prompt-compiler.md)

本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。
