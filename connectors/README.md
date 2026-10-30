# 供应商连接器

定义见 [docs/09-model-execution.md](../docs/09-model-execution.md)，证据基线见
[docs/17-provider-evidence-baseline.md](../docs/17-provider-evidence-baseline.md)。

**全部连接器默认 `disabled`。** 这是设计约束不是待办事项。

## S27 是服务家族，不是一个共享 Worker

每个渠道独立部署、独立网络出站身份、独立凭证引用、独立任务队列、独立限流与熔断。

Seedance 的 Volcengine 和 BytePlus 是两个独立部署 —— 同一家的不同渠道也分开。

## 协议

`capabilities` / `validate` / `estimate` / `submit` / `get_status` / `cancel` /
`verify_callback` / `fetch_manifest`

`submit` 必须带 stable client key **与 dispatch grant**。

## 连接器不能做什么

- 换模型
- 调预算
- 接受成片

连接器只做参数映射和协议处理。

## 状态机

```
CREATED → RESERVING → ADMITTED → DISPATCH_AUTHORIZED → SUBMITTING
→ QUEUED / RUNNING → PROVIDER_SUCCEEDED / FAILED / CANCELED
→ INGESTING → ARCHIVED
```

旁路：`SUBMISSION_UNKNOWN` / `RECONCILING`。

状态必须**分别计数**业务 attempt 与 network retry。

## 几条容易踩的

- 429 尊重 `Retry-After` 与持久 timer；5xx/timeout 先判断提交是否不确定
- 401/403 隔离账户并告警
- 政策拒绝**不能靠换渠道绕过**
- 输入组合错误不无限重试，记录 capability drift
- 输出下载失败只重归档；供应商 URL 过期先查新 URL，**不能直接重做付费生成**
- 供应商支持幂等时复用同键；不支持时一次领取后任何未知结果保留 UNKNOWN，**不能在另一副本"恢复重发"**

## 当前渠道

| Provider | Channel | 说明 |
|---|---|---|
| seedance | volcengine | 火山方舟官方渠道 |
| seedance | byteplus | BytePlus ModelArk，与 Volcengine 独立部署 |
| kling | api | Kling 官方 API |
| wan | official | Wan 官方 API |
| wan | self-hosted | Wan 自托管部署 |
| veo | official | Veo 各官方渠道 |
| sora | historical | 历史占位；Videos API 已于 2026-09-24 停用 |
| vidu | api | Vidu 官方 API |
| minimax | api | MiniMax 官方 API |

图像、TTS、LLM/VLM 连接器待建，尚未分配目录。

## 上线条件

连接器实施前必须重新核验：官方生命周期、账户能力、地区、价格、并发、参考组合、幂等、回调、TTL、声音资格。

历史教程里的示例**不等于当前可用能力**。
