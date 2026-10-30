# 评测

定义见 [docs/08-agents-prompts.md](../docs/08-agents-prompts.md) 第 4 节、
[docs/15-fde-business-evaluation.md](../docs/15-fde-business-evaluation.md)。

| 目录 | 内容 |
|---|---|
| `datasets/` | 评测数据集 |
| `rubrics/` | 评分标准 |
| `manifests/` | 评测运行清单（固定模型 snapshot 与模板版本） |
| `reports/` | 结果报告 |

## 评测什么

**Agent 角色**：任务正确性、结构合法率、约束遵循、token/费用、工具越界、人工接受率。

**Prompt 模板**：按机位动作遵循、身份保持、可用率、返工成本评测。

## 调度

在线修改走高优先级，离线大批量走低优先级，**评测单独预算**。

不能因为离线评测把生产堵住。

## 结果不能回灌生产

未通过回归的评测结果不能直接挂到线上关键角色。Run 固定模板版本；灰度和回退只影响新请求，历史产物不自动重做。
