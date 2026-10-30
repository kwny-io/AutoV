# 13 · 企业API、MCP与产品布局

## 1. 入口分离

API Gateway/WAF处理传输保护、粗粒度限流与认证入口；S01/S02持有细粒度授权与政策。Studio BFF聚合只读投影、短命令结果，不写领域DB。S46 Developer API/MCP共用领域契约，独立Deployment和client scopes；长任务返回operation_id/run_id，后续查询或订阅。S47独立SSE/Webhook投影，网页断线不影响后台制作。

v2领域上下文含branch、manifest、memory checkpoint、epoch、home-cell；旧版只传episode revision不足以安全执行。企业请求中tenant从认证上下文派生；外部指定值只能作一致性校验，不能改变权限。

核心OpenAPI设计与MCP工具见contracts。每个服务的内部command/query入口见service catalog；这些是实施契约设计，不是已运行API。所有写操作command_id+request_hash幂等；同键不同请求409；expected_manifest不符409；不可行422；明确budget/permission错误不能伪装可重试500。

## 2. MCP能力与权限

工具按read/plan/execute/review/admin分scope。project.get、memory.query、context.preview、run.get、artifact.compare、model.estimate只读且不得隐式付费。episode.plan、change.plan、memory.extract可能有有界推理费用，要求plan:compute预算。episode.generate、change.apply、review.decide、policy.change分别授权。

工具输入schema与SDK输出schema版本化。MCP Streamable HTTP用官方SDK；readOnlyHint等仅UI提示，强制授权在服务端。外部Agent不能通过“我是管理员”的工具参数获权。callback/webhook签名、时间窗、重投与去重，默认不发送未授权通知。

## 3. 企业工作台

顶栏tenant/组织/项目/分支/集、当前manifest、自动化档位、费用与容量。主导航分制作、资产与记忆、运行治理、企业运营四组；每页有权限与水位，避免分析投影冒充实时余额。

| 页面 | 必须可见的能力 |
|---|---|
| 企业总览 | 项目产能、队列SLA、预算燃尽、失败与待审、cell健康 |
| 创作与分镜 | 剧本/场次/镜头版本、导演DSL、对话修改、版本比较 |
| 资产中心 | 五类资产独立页、canonical/权利/使用集、升级影响 |
| 跨集记忆 | 正史分支、事件线、人物关系/知情范围、道具状态、伏笔、来源与冲突 |
| 自动化治理 | 四档选择、阶段/节点覆盖、生成与接受两开关、继承来源 |
| 生成队列 | provider/cell/tenant分组、在途/平台等待/供应商等待/待归档/待QC |
| 审片返工 | 缺陷时间码证据、版本固定、分工与quorum、预算/创作不同审批 |
| 时间线与交付 | 轨道、ripple、字幕、音轨stems、正式/预览包区分 |
| 成本与评测 | 每可用分钟综合成本、返工、人工、样本分层、策略效果 |
| 管理与审计 | 成员/服务账号、BYOK引用、驻留、保留、审计导出、连接器状态 |

镜头工作台保留左树/中播放器/右参数与证据/底时间线，但加memory checkpoint、branch、策略与manifest标签。修改计划呈现跨集影响，操作前有成本区间与不可行原因。角色canonical、节点锁、旧审核与stale结果均明确标识。

layout.html是本版本静态线框，没有真实进度、真实价格或模型调用。
