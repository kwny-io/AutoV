# 09 · 模型、路由与供应商执行域

## 1. 四个服务的协议

S24注册事实 → S25计算可行RouteDecision → S26持久化任务与准入Saga → S27按渠道协议执行并观测。S29–S34负责生产意图与阶段，不能绕过S26直连模型。S19的LLM/VLM、S36评估器、S48训练所需外部调用同样受统一预算和配额控制。

S24主键为provider/channel/model_id/model_snapshot/region/account_pool；记录模式组合、参考图/首尾帧/主体/LoRA/音频输入、时长档位、画幅、分辨率、文件限制、数据地区、生命周期、单价快照、幂等/回调/取消/任务与URL有效期、能力证据与验证到期时间。能力组合必须按版本和模式验证，不能只列一串boolean。

S25先过滤授权、状态、驻留、必需参考组合、画幅、声音、时长与对白、期限与报价上界，再按质量/身份/运动/可靠性/预期成本/延迟/不确定性评分。ExpectedCost包含下游口型、QC和业务重试，不允许低价覆盖硬约束。

## 2. 连接器隔离

Seedance的Volcengine和BytePlus分开Deployment；Kling、Wan官方API、Wan自托管、Veo各官方渠道、Sora历史占位、Vidu、MiniMax、图像、TTS、LLM/VLM等独立部署、网络出站身份、凭证引用、任务队列、限流和熔断。S27是共同契约与服务家族，不是把全部供应商塞进一个共享Worker。

协议：capabilities/validate/estimate/submit/get_status/cancel/verify_callback/fetch_manifest。submit必须带stable client key与dispatch grant；connector只做参数映射和协议处理，无权更换模型、调整预算或接受成片。

真实供应商全默认disabled，官方文档与账户探测后进入validating→active；degraded/retired剔除。v0.1已核对的Sora历史API和Gemini Veo3.1 Preview停用结论保留，其他渠道需独立验证。当前设计不新增供应商可用性声明。

## 3. 状态与幂等

ProviderJob：CREATED→RESERVING→ADMITTED→DISPATCH_AUTHORIZED→SUBMITTING→QUEUED/RUNNING→PROVIDER_SUCCEEDED/FAILED/CANCELED→INGESTING→ARCHIVED。旁路SUBMISSION_UNKNOWN/RECONCILING；状态必须含业务attempt与network retry分别计数。

S26与S27通过可重投Outbox命令协作；S27 receipt唯一约束同dispatch_id。供应商支持幂等时复用同键；不支持时一次领取后任何未知结果保留UNKNOWN，不能在另一副本“恢复重发”。回调先验签入Inbox并快速ACK，再异步确认映射与权威状态；无法可靠验签则仅当唤醒提示并authenticated query确认。

429尊重Retry-After与持久timer；5xx/timeout先判断提交是否不确定。401/403隔离账户并告警。政策拒绝不靠换渠道绕过。输入组合错误不无限重试，记录capability drift。输出下载失败只重归档；供应商URL过期先查询新URL，不能直接重做付费生成。

## 4. 降级与账户隔离

允许链：相同硬能力健康账户池→相同硬能力其他模型→策略已允许的规格调整/拆镜/分层合成→审核或隔离分支。角色身份、锁定音色、完整对白、权限和最低交付规格不能无声降级。

共享账户必须维护provider/account/region全局上限；BYOK企业账户只用于该企业，不能“借凭证容灾”。价格过期/变更重新报价，reserved不足时等待预算；账单未知保守暂记。每个route、rejection、compiled prompt和实际参数可追溯。
