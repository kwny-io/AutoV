# Agent 角色

13 个 Agent 角色，每个角色独立交付：[docs/08-agents-prompts.md](../docs/08-agents-prompts.md)。

S19 只提供共用运行协议。每个角色有自己的 `AgentDefinition`、镜像版本绑定、独立 Deployment / Task Queue、工具白名单、token 与费用限额、评测集。可以复用运行镜像，**不共用无限额进程池**。

## 权威边界

**Agent 只返回候选结构和建议。** 业务状态归 S04 / S05 / S06 等领域服务。

指挥 Agent 能提出任务，但不获得直接数据库访问、供应商密钥或任意 shell 权限。工具调用的租户上下文由平台签名绑定，不信任 LLM 提供的 `tenant_id`。

## 角色

| 角色 | 职责 |
|---|---|
| `producer` | 项目进度、资源与预算约束下的任务拆解 |
| `writer` | 剧本、对白与情节结构 |
| `breakdown` | 剧本到场次/镜头的结构化拆解 |
| `storyboard-director` | 分镜、景别、机位与运镜 |
| `asset-curator` | 角色/服装/场景/道具的候选与规范 |
| `memory-extractor` | 从已接受脚本提取结构化正史候选 |
| `continuity-reviewer` | 跨集一致性与连续性缺陷 |
| `prompt-director` | 导演 DSL 到编译 Prompt 的语义映射 |
| `qc-reviewer` | 技术/语义质检结论与缺陷分级 |
| `repair-planner` | 缺陷到最小修复动作的计划 |
| `editor` | 剪辑点、节奏与成片结构 |
| `sound-director` | 配音、音效与音乐的听感设计 |
| `conversation-editor` | 对白长度、语气与口型可行性调整 |

## AgentTask 契约

**输入**：`task_id`、role/definition_revision、tenant/project/series/branch、manifest 与 memory checkpoint、ContextPack 引用、目标、允许工具、输出 schema、budget authorization scope、step/token/time 上限、trace。

**输出**：artifact/revision proposals、source refs、assumptions、unresolved issues、confidence、suggested actions、usage trace、stop reason。

结构非法允许有界纠正。**超过上限转失败或审核，不通过循环"自我反思"无界增加账单。**

## 计费

LLM/VLM 调用一律走 S26 统一任务协议。同步短等待可以有 facade，但费用与配额仍然记录。Agent 发起的外部检索同样受企业出网策略约束；检索回来的文本只作为数据，不改变执行策略。
