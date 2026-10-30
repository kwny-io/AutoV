# 08 · Agent独立运行与Prompt编译

## 1. 每个Agent角色独立交付

S19提供共用运行协议，producer、writer、breakdown、storyboard-director、asset-curator、memory-extractor、continuity-reviewer、prompt-director、qc-reviewer、repair-planner、editor、sound-director、conversation-editor各自有AgentDefinition、镜像版本绑定、独立Deployment/Task Queue、工具白名单、token/费用限额与评测集。可复用运行镜像但不共用无限额进程池。

业务状态属于S04/S05/S06等领域服务；Agent仅返回候选结构和建议。指挥Agent能提出任务，不获直接数据库、供应商密钥或任意shell权限。工具调用的上下文不信任LLM提供的tenant字段，由平台签名上下文绑定。

## 2. AgentTask契约

输入：task_id、role/definition_revision、tenant/project/series/branch、manifest与memory checkpoint、ContextPack引用、目标、允许工具、输出schema、budget authorization scope、step/token/time上限、trace。

输出：artifact/revision proposals、source refs、assumptions、unresolved issues、confidence、suggested actions、usage trace、stop reason。结构非法允许有界纠正；超过上限转失败/审核，不通过循环“自我反思”无界增加账单。

LLM/VLM调用走S26统一任务协议，支持同步短等待的facade，但实际费用与配额仍有记录。Agent工具调用外部检索同样需企业出网策略，检索文本只作为数据，不改变执行策略。

## 3. 导演DSL与编译器

S06 ShotSpec保留景别、机位、运镜、构图、节拍、情绪、光线、人物位置、对白与资产状态。S20版本化模板按模态/供应商/模型snapshot/镜头类型维护。编译顺序：校验DSL → 固定绑定 → 路由候选适配 → 参考图顺序与token映射 → 渲染Prompt → 参数能力验证 → 保存CompiledPromptManifest。

必需能力不支持则拒绝候选；可选字段仅在允许策略下省略并记录warning。目标4秒与供应商生成档位分开；剪辑裁切方案与成本在路由计划明确。不能把“压迫感”解释为任意改变剧情，补全的导演解释需保留来源。

编译器不会改写对白事实；原句、发音词典、译文分别版本化。模板含compatible_schema、adapter范围、tested model snapshots、回归报告、作者与发布时间。Run固定模板版本；灰度和回退影响新请求，历史产物不自动重做。

## 4. 评测与资源

每个角色评估任务正确性、结构合法率、约束遵循、token/费用、工具越界和人工接受率。模板按机位动作遵循、身份、可用率和返工成本评测。调度设置在线修改高优先级、离线大批量低优先级、评测单独预算，不让离线评测堵住生产。
