# AGENTS.md

给在本仓库写代码的实现 Agent。**先读这份，再动手。**

## 入口规则

按 [docs/16 §6](docs/16-migration-acceptance.md) 的要求：

1. 读 [README](README.md)、01 / 02 / 04 / 05 / 06 / 12 / 16 七篇文档，以及你要碰的那个服务的 `service.md`
2. 核实目标仓库的 AGENTS、云、账号、预算与客户合同
3. 按波次创建独立服务、契约与 mock
4. 先验证 E12–E23 和 E05 / E08–E11，再接真实模型
5. 所有生产开关默认关闭

## 绝对不能做

- 跨服务数据库写入、跨库 JOIN、互相 import 业务 ORM
- 让某个服务变成第二个权威（例如自己记一份余额、自己存一份 accepted pointer）
- 在 `SUBMISSION_UNKNOWN` / `SUBSESSION_UNKNOWN` 状态下盲目重提付费任务
- 绕过 S26 直连模型，或绕过 S23 / S22 直接付费
- 把"审核已通过"当成放行依据 —— 派单前必须实时复查撤销、kill switch、预算
- 把 schema 校验通过当作分布式正确性证明
- 预填供应商状态为 `active`
- 把 LLM 提供的 `tenant_id` 当可信输入；租户上下文必须由平台签名绑定

## 什么时候该停

- 需要真实付费生成 → 停下，等实施会话明确授权
- 需要部署或外部分发 → 停下，等授权
- 契约与文档冲突 → 停下，报告冲突，不要自行选边
- 依赖服务未实现 → 用 mock 按契约走，不要共用数据库凑合

## 版本与一致性速查

| 概念 | 作用域 | 用途 |
|---|---|---|
| `execution_fence` | run / branch | 取消、故障切换 |
| `node_epoch` | 单个 `logical_node` | 局部重生成 |

两者不能混用。旧 epoch 的结果永远不能推进当前接受指针。

幂等键统一为 `tenant_id + command_id + request_hash`，同键不同 body 拒绝（409），不是覆盖。

## 生成物

`services/*/service.yaml`、`agents/*/agent.yaml`、`connectors/*/*/connector.yaml` 从 `contracts/service-catalog.v2.json` 派生。改内容请改 catalog 再重新生成。

## 文档放哪里

| 目录 | 放什么 | 能不能改 |
|---|---|---|
| `services/<slug>/` | **只放代码**：`src/ api/ events/ migrations/ tests/ deploy/ runbooks/` | 能 |
| `plans/` | 实现方案，`plans/<服务ID>-<slug>.md` | 能 |
| `docs/01`–`17` | 设计文档 | 能，但改要同步 `docs/design-package/` 的对照关系 |
| `docs/design-package/` | **原始设计包存档，只读** | **不能** |

实现方案不要写进 `services/<slug>/`，会和代码抢位置。新写的任何文档都不放 `docs/design-package/` —— 那是原始输入，改了就没法对照了。

## 验收

交付前对照 [E01–E40](docs/16-migration-acceptance.md)。至少要能证明：跨租户拒绝、重复投递幂等、乱序/缺失事件、重启恢复、版本冲突、审计可追溯、独立升级回滚。
