# AutoV

> An advanced video creates automatic flow

短剧 / 漫剧企业级生产平台的工程仓库。

本仓库当前处于 **骨架阶段向实现过渡**：目录、服务元数据、契约与设计文档已经就位。业务服务按 [docs/16 §6](docs/16-migration-acceptance.md) 的 W1–W5 波次逐个实现，目前 `tenant-access`（S01）、`revision-snapshot`（S08）、`lineage-impact`（S09）与 `artifact-ingest`（S28）已有可运行的最小内核与测试，其余 44 个服务仍为 `design_only`。

## 现在有什么

| 内容 | 位置 | 状态 |
|---|---|---|
| 48 个服务目录与元数据 | `services/<slug>/` | 元数据完整；4 个有实现（S01、S08、S09、S28），44 个 `src/` 为空 |
| 共享 SDK（幂等 / 事件 / TCT） | `platform/*-sdk/src/` | 已实现，有单测 |
| 13 个 Agent 角色 | `agents/<role>/` | 定义待填，限额未分配 |
| 9 个供应商连接器 | `connectors/<provider>/<channel>/` | 全部 `disabled` |
| 契约（OpenAPI / schema / 事件） | `contracts/` | 设计稿，已校验可解析 |
| 设计文档 17 篇 | `docs/` | 从设计包迁入，链接已修正 |
| 实现方案 | `plans/` | 按服务分文件，进行中 |
| 原始设计包存档 | `docs/design-package/` | **只读**，未改动内容 |

`services/<slug>/` 只放代码。实现方案一律放 `plans/`，原始设计包存档不动。

## 五条硬约束

写代码前请先读 `AGENTS.md`。核心是这五条：

1. **单一权威**：每个事实只有一个服务能写。当前生产状态归 S08，正史归 S16，金额归 S23，配额许可归 S22，生成任务归 S26，媒体字节归 S28，人工决策归 S40。
2. **不跨库**：不跨服务数据库写、不跨库 JOIN、不互相 import 业务 ORM。
3. **不可变内容**：内容按 `service/id/revision/content_hash` 寻址。回滚生成新 manifest，不改历史。
4. **事件用 Outbox/Inbox**：at-least-once，按 aggregate 保序，重放时 `side_effects=false`。
5. **付费调用只走协议**：`S24 → S25 → S26 → S27`，且必须先在 S23 预留预算、在 S22 取得许可。未知提交不盲目重提。

## 目录

```text
apps/                 studio-web / studio-bff / api-gateway / developer-api / mcp-server
services/<slug>/      src/ api/ events/ migrations/ tests/ deploy/ runbooks/ + service.yaml
agents/<role>/        definition/ prompts/ evals/ deploy/ + agent.yaml
connectors/<p>/<c>/   src/ tests/ deploy/ + connector.yaml
contracts/            openapi/ events/ schemas/ generated-clients/ + examples/
platform/             auth-sdk / event-sdk / telemetry-sdk / idempotency-sdk
workflows/            episode / shot / memory-migration / admission / repair
infra/                global/ regions/<region>/cells/<cell>/
evals/                datasets/ rubrics/ manifests/ reports/
customer-configs/     按租户的版本化策略与模板（无密钥）
delivery/             playbooks/ capacity/ acceptance/ training/
plans/                实现方案，按服务分文件
docs/                 17 篇设计文档 + 设计包存档（后者只读）
```

## 从哪开始读

按这个顺序，不要跳：

1. [docs/design-package/README.md](docs/design-package/README.md) —— 这份设计的适用范围与免责声明
2. [docs/01-enterprise-architecture.md](docs/01-enterprise-architecture.md) —— 服务边界与唯一权威表
3. [docs/02-service-catalog.md](docs/02-service-catalog.md) —— 48 个服务全表
4. [docs/04-data-consistency.md](docs/04-data-consistency.md) —— 版本、epoch、准入 Saga
5. [docs/16-migration-acceptance.md](docs/16-migration-acceptance.md) —— 波次计划与 E01–E40 验收矩阵

## 重要声明

以下是设计包里的原话，实现时不要忘记：

- 尚未部署任何业务服务，未调用任何付费生成模型。已实现的服务只在本机跑单测，没有连过真实供应商。
- 容量、SLO、RPO/RTO 都是**未验证的规划值**。`service.yaml` 里 `slo.verified` 一律为 `false`。
- **Schema 合法 ≠ 分布式行为正确。** 静态校验只能证明契约自洽，证明不了 Saga、幂等、fencing 真的对。
- 所有供应商默认 `disabled`。官方文档与账户探测通过后才能进入 `validating → active`。不要预填 `active`。

## 本地开发

目前 S01、S08、S09 有可运行的构建，其余 45 个服务的 `src/` 为空。`service.yaml` / `agent.yaml` / `connector.yaml` 均由 `contracts/service-catalog.v2.json` 派生，改动请改 catalog 后重新生成，不要手改生成物。

```bash
python -m pytest -q
```

需要 `fastapi`、`pytest`；Python 3.12。各服务的 `src/` 与 `api/` 在 `pyproject.toml` 的 `pythonpath` 里声明，不用先设环境变量。
