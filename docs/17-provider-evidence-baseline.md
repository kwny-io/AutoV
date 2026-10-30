# 17 · 供应商证据基线与生命周期

检索日期：2026-10-29（Asia/Taipei）。通过已连接 Exa 插件检索官方资料；对 Sora、Gemini 视频生命周期和 Temporal Activity 读取了正文，其余为官方入口/检索片段核对，**未进行 API 调用或账户能力验证**。本包的架构决策是设计建议，不是官方对本系统的背书。

## 官方资料

| 资料 | 发现/用途 | 核对深度 |
|---|---|---|
| [OpenAI Video generation with Sora](https://developers.openai.com/api/docs/guides/video-generation) | 页面明确 Sora 2 与 Videos API 于2026-09-24停用；历史正文仍有旧示例，不能直接照搬 | 正文 |
| [Google Gemini API Video](https://ai.google.dev/gemini-api/docs/video) | Veo 3.1 Preview 在 Gemini API 于2026-10-22停用；其他渠道不由此推断 | 正文 |
| [Veo 历史说明](https://ai.google.dev/gemini-api/docs/veo) | 区分历史参数与当前端点生命周期 | 检索片段 |
| [Temporal Workflows](https://docs.temporal.io/workflows) | 持久工作流与恢复语义 | 检索片段 |
| [Temporal Activity Definition](https://docs.temporal.io/activity-definition) | Activity 可能执行多次，需要业务幂等；不能把观察到完成一次当执行一次 | 正文 |
| [Temporal Messages](https://docs.temporal.io/handling-messages) | Signal/Update 去重与 Continue-As-New 作用域 | 检索片段 |
| [火山方舟创建视频任务](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh) | Seedance 官方创建任务入口，模型开通和输入条件需检查 | 检索片段 |
| [BytePlus Video Tutorial](https://ai.byteplus.com/ark/region:ap-southeast-1/docs/video-generation-tutorial) | submit → task_id → poll/webhook → download 的异步流程 | 检索片段 |
| [BytePlus List tasks](https://docs.byteplus.com/en/docs/ModelArk/list-video-generation-tasks-api) | 输出 URL 有效期、任务有效期、取消状态需分别建模；具体值实施时复核 | 检索片段 |
| [Alibaba Wan3.0 API](https://www.alibabacloud.com/help/en/model-studio/wan3-video-generation-api-reference) | 区域/端点/Key需匹配；参考组合约束因版本变化 | 检索片段 |
| [Kling 官方文档](https://kling.ai/document-api/guides/get-started/overview) / [索引](https://kling.ai/document-api/llms.txt) | 能力图、并发、回调、版本使用指南入口 | 检索片段 |
| [Vidu 官方文档索引](https://platform.vidu.com/docs/llms.txt) | 主体、参考模式、任务、回调签名、声音与 MCP 文档入口 | 检索片段 |
| [MiniMax Video](https://platform.minimax.io/docs/guides/video-generation) | 当前视频模式与模型接入入口；需进一步逐项核验 | 检索片段 |

广泛搜索返回的第三方网关页面未作为供应商真实能力或价格的依据。未对七家全部 API 参数做完整实时审计，因此生产 registry 不预填“active”。本包覆盖其适配器设计，不声称已实现全部接入。


## v2使用规则

这是v0.1于同日检索的证据记录，本轮未重新进行供应商API探测。v2服务与部署按01–16；生产开关全部关闭。连接器实施前须重新核验官方生命周期、账户、地区、价格、并发、参考组合、幂等、回调、TTL与声音资格。历史教程中的示例不等于当前可用能力。
