# S27 · 供应商连接器 —— 实现方案

状态：`design_only`。本文是实现方案，不是已实现代码。**九个渠道全部 `disabled`。**

上游依据：[docs/09 §2–§4](../docs/09-model-execution.md)、[docs/17](../docs/17-provider-evidence-baseline.md)、[docs/14 §1/§2/§5](../docs/14-enterprise-operations.md)、`connectors/*/*/connector.yaml`、`contracts/generation-command.v2.schema.json`、`plans/S26-generation-jobs.md`。
链上位置：`S24 → S25 → S26 → S27`，S27 是链的末端执行者。

---

## 1. 边界

### 1.1 S27 是什么

**参数映射器 + 协议处理器 + 观测上报者。**

对每个渠道，把平台的规范输入翻译成该供应商的 HTTP 请求，把供应商的响应翻译成平台观测，写进自己的 Inbox/Outbox。

### 1.2 S27 绝对不做

| 禁止 | 落点 |
|---|---|
| **换模型** | `connector.yaml: may_change_model: false`。payload 里的 `model_id` 只能来自 `submit_payload`，S27 无配置项可覆盖。代码里没有"fallback model"分支。 |
| **调预算** | `may_adjust_budget: false`。S27 不知道预算存在；`submit_payload` 不含金额字段。 |
| **接受成片** | `may_accept_output: false`。S27 只上报"供应商说成功了 + manifest 摘要"；`accepted_manifest_authority: S08`（`runtime-safety.yaml`）。S27 没有写 `accepted_pointer` 的权限，也没有该接口。 |
| **决定是否提交** | 由 S26 的 dispatch grant 决定。S27 只是执行者。 |
| **判断业务是否成功** | S27 只上报供应商的终态语义；是否算平台成功由 S26/S36/S08 决定。 |
| **跨渠道重试** | 渠道 A 失败就报失败。换渠道是 S25/S26 的降级决策。 |
| **绕过政策拒绝** | 供应商说"政策不允许"，S27 原样上报 `policy_rejected`，不尝试别的措辞、别的参数、别的账户池。 |

`docs/09`："连接器只做参数映射与协议处理，无权换模型、调预算或接受成片。"

这条在代码结构上要能看出来：S27 的模块里没有 `select_model()`、没有 `budget` 相关字段、没有 `accept()`。

### 1.3 S27 写什么

- `connector_command_inbox` — 消费到的命令去重
- `connector_request_receipt` — 每次出站请求的回执（**这是幂等的锚点**）
- `connector_callback_inbox` — 回调原文
- `connector_observation_outbox` — 要发给 S26 的观测
- `channel_health` — 本地熔断状态（不是 S24 的账号健康事实，只是本渠道的瞬时状态）

---

## 2. 隔离：九个渠道，九个独立单元

`docs/09 §2`："共同协议、服务家族，不是共享 worker。"

| 渠道 | 部署单元 | 出网身份 | 凭证 | 队列 | 限速 | 熔断 |
|---|---|---|---|---|---|---|
| `kling/api` | 独立 Deployment | 独立 egress IP | 独立 secret | 独立分区 | 独立 | 独立 |
| `minimax/api` | 同上 | 同上 | 同上 | 同上 | 同上 | 同上 |
| `seedance/byteplus` | 同上 | 同上 | 同上 | 同上 | 同上 | 同上 |
| `seedance/volcengine` | 同上 | 同上 | 同上 | 同上 | 同上 | 同上 |
| `sora/historical` | **不部署**（历史占位） | — | — | — | — | — |
| `veo/official` | 独立 Deployment | 独立 egress IP | 独立 secret | 独立分区 | 独立 | 独立 |
| `vidu/api` | 同上 | 同上 | 同上 | 同上 | 同上 | 同上 |
| `wan/official` | 同上 | 同上 | 同上 | 同上 | 同上 | 同上 |
| `wan/self-hosted` | 同上（无外部出网，走内网推理集群） | 内网身份 | 内网 token | 同上 | 同上 | 同上 |

`sora/historical` 的处理（`docs/17`）：Sora 2 / Videos API 于 **2026-09-24** 停用。`connector.yaml` 里 `lifecycle: disabled`，`retired_reason` 已写。**不部署 Pod**；S24 的 `provider_channel` 表里不允许有 `retired → active` 的边（见 S24 §4）。如果事件流里出现 `channel_route=sora/historical` 的 `submit.requested`，S27 无消费者，事件滞留 → 由 S26 的 grant 过期机制兜底（job 回到 `ADMITTED`），并且产生一条 dead-letter 告警。这是正确的失败方式：不静默、不重试、不花钱。

### 2.1 为什么必须物理隔离

`docs/09`："一个供应商故障、限速或安全事件不得拖垮其他渠道。"

具体威胁：

1. **限速串扰**：如果共享 worker，Kling 的 429 退避会占住 worker，把 Veo 的正常请求也拖慢。
2. **凭证泄露爆炸半径**：共享 Pod 意味着一个 Pod 被攻破，九个供应商的 key 全泄。
3. **出网身份混用**：供应商侧的 IP 白名单/风控是按 IP 判定的。混用会让一个渠道的异常行为污染其他渠道的账户信誉。
4. **部署节奏**：某渠道的供应商 API 变更只需滚动该渠道。

### 2.2 部署描述（示意）

```yaml
# deploy/connectors/seedance-volcengine.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: s27-seedance-volcengine
  labels: {app: s27-connector, channel: seedance-volcengine}
spec:
  replicas: 3
  template:
    spec:
      serviceAccountName: s27-seedance-volcengine
      containers:
        - name: connector
          image: registry/autov/s27-connector:...
          env:
            - name: CHANNEL_ROUTE
              value: seedance/volcengine
            - name: KAFKA_COMMAND_TOPIC
              value: drama.provider-connectors.v2
            - name: KAFKA_COMMAND_PARTITION_KEYS
              value: "seedance/volcengine"
            - name: S26_CLAIM_URL
              value: http://s26.generation-jobs.svc/admissions
            - name: REQUIRE_DISPATCH_GRANT
              value: "true"          # 编译期常量，见 §5.2
            - name: CREDENTIAL_MOUNT
              value: /var/run/secrets/seedance-volcengine
          volumeMounts:
            - name: cred
              mountPath: /var/run/secrets/seedance-volcengine
              readOnly: true
      volumes:
        - name: cred
          secret:
            secretName: s27-seedance-volcengine
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: s27-seedance-volcengine-egress}
spec:
  podSelector: {matchLabels: {channel: seedance-volcengine}}
  policyTypes: [Egress]
  egress:
    - to: [{namespaceSelector: {matchLabels: {name: kafka}}}]     # 平台内部
      ports: [{port: 9093}]
    - to: [{namespaceSelector: {matchLabels: {name: s26}}}]       # claim
      ports: [{port: 8080}]
    - to: [{ipBlock: {cidr: 203.0.113.0/24}}]                     # 该供应商的 API 网段，白名单
      ports: [{port: 443}]
```

`NetworkPolicy` 是"绕过 S26 直连模型"这条硬约束的技术落点之一：只有 connector Pod 有供应商出网权限，且它的凭证只能驱动该渠道的 API。

### 2.3 速率与并发限制放在哪

**本地令牌桶（每渠道每 Pod）+ 全局并发闸（每渠道）**：

- 本地桶：秒级限速，削峰。Redis 或进程内都行，进程内更简单——丢了只影响平滑度。
- 全局并发闸：`max_inflight` 来自 S24 的 `provider_account_pool`，但 S22 已经按池发 permit。**S27 不重复计数**，只用 permit 作为闸门——permit 就是并发许可。S27 再本地限一道 `max_inflight_per_pod` 防止单 Pod 打完。

`docs/09`："429 尊重 Retry-After，用持久定时器，不用忙轮询。" S27 *不*自己退避重试。它把 `retry_after_ms` 上报给 S26，由 S26 的 `next_action_at` 驱动。S27 收到 429 就结束这次命令处理。

---

## 3. 协议：8 个方法

`docs/09` 定义的连接器协议。每个方法都必须能在"无副作用"下被调用，除了 `submit`。

| 方法 | 幂等 | 付费 | 调用方 |
|---|---|---|---|
| `capabilities` | 是 | 否 | S24（注册/探针） |
| `validate` | 是 | 否 | S26（提交前本地校验） |
| `estimate` | 是 | 否 | S25（估算，也可用于交叉核对报价） |
| `submit` | **受限**（见 §5） | **是** | S26（经事件） |
| `get_status` | 是 | 否 | S26（轮询） |
| `cancel` | 是 | 可能计费 | S26 |
| `verify_callback` | 是 | 否 | S27 自身（回调入口） |
| `fetch_manifest` | 是 | 否 | S26/S28（取输出清单） |

### 3.1 `capabilities`

S24 用它注册"文档宣称的能力"，也用 probe 验证。

```jsonc
// request
{ "channel_route": "seedance/volcengine", "model_snapshot": "250528" }
// response
{
  "channel_route": "seedance/volcengine",
  "model_id": "seedance",
  "model_snapshot": "250528",
  "modes": ["text_to_video","image_to_video","first_last_frame","reference_to_video"],
  "execution_kind": "poll",
  "supports_callback": true,
  "callback_scheme": "hmac_sha256",
  "native_audio": true,
  "output": {
    "aspect_ratios": ["9:16","16:9","1:1"],
    "max_resolution": {"width": 1080, "height": 1920},
    "duration_tiers_s": [5, 10],
    "trims_to_nearest": true
  },
  "input_limits": {"prompt_chars": 2000, "reference_images": 4, "video_refs": 1},
  "idempotency": "client_lookup_only",
  "cancel_supported": true,
  "url_ttl_seconds": 86400,
  "job_ttl_seconds": 604800,
  "pricing": { "billing_units": [{"unit":"generated_second","tiers_s":[5,10]}], "currency":"USD" },
  "evidence": { "source": "vendor_docs", "fetched_at": "...", "doc_hash": "..." }
}
```

**关键**：`capabilities` 返回的是**该渠道部署配置里声明的映射表**，不是实时探测。实时探测是 `account_probe`，由 S24 的 probe-worker 通过 `capabilities` 之外的手段（或 `capabilities(probe=true)`）完成。S27 不主动探测——探测是有成本的，由 S24 决定何时探。

`idempotency` 字段是 S24 要的（见 S24 未决问题）：它必须来自**实测**，不是文档推断。S27 的实现里，每个渠道适配器硬编码一个 `IDEMPOTENCY_CLASS` 常量，并有单测断言它与 S24 注册表一致。

### 3.2 `validate`

纯本地、零网络。S26 在签发 grant 前可以调（可选，S24 的 `combination_check` 已经覆盖大部分）。

```jsonc
// request
{ "submit_payload": { /* 平台规范化输入 */ } }
// response
{ "ok": false, "errors": [
    { "code":"DURATION_TIER_MISMATCH", "path":"duration_seconds", "detail":"5s 不是该快照的合法档位" }
] }
```

`validate` **不能**替代 S24 的 `combination_check`：`validate` 只看该渠道的静态映射，`combination_check` 看的是"这个组合在这个版本这个区域被验证过"。两者都要过。

### 3.3 `estimate`

```jsonc
// request
{ "submit_payload": {...} }
// response
{
  "estimated_units": {"generated_second": 5},
  "estimated_cost_micro": 900000,
  "currency": "USD",
  "worst_case_cost_micro": 1800000,
  "breakdown": [{"component":"generation","amount_micro":900000}],
  "confidence": "documented"
}
```

用途：
1. S25 可以用它做交叉核对（S25 的报价来自 S24 的 `price_snapshot`，`estimate` 是供应商侧的独立视角；两者偏差大 → 告警）。
2. S26 在 `settle` 时可以拿它对账。

`estimate` **不产生费用**（该渠道的 estimate 是纯计算）。若某供应商的 estimate 是付费的（少数有），S27 在 `capabilities.pricing.billing_units` 里标注，S25 不调用它。

### 3.4 `submit` —— 唯一付费的方法

见 §5（完整协议与幂等约束）。

### 3.5 `get_status`

```jsonc
// request
{ "provider_job_ref": "task_abc", "client_key": "dr-…" }
// response
{
  "provider_job_ref": "task_abc",
  "status": "running",            // queued|running|succeeded|failed|canceled|not_found
  "progress": 0.42,
  "observed_at": "2026-10-30T03:07:11Z",
  "terminal": false,
  "error": null,
  "output": null,
  "retry_after_ms": null
}
```

`succeeded` 时：

```jsonc
"output": {
  "manifest": [
    {"role":"video","url":"https://.../out.mp4?sig=...","url_expires_at":"...",
     "sha256":"...","bytes":12345678,"content_type":"video/mp4",
     "width":1080,"height":1920,"duration_seconds":5.0}
  ],
  "provider_usage": {"generated_seconds": 5, "currency":"USD"}
}
```

`not_found` 是**对账的关键信号**（S26 §7.3）。S27 必须如实上报，不能把 `not_found` 翻译成 `failed`——两者含义完全不同。

`retry_after_ms` 从供应商 429 头翻译过来；S27 不上报"你应该等多久然后重试"，只上报"供应商说了 30 秒"。

### 3.6 `cancel`

```jsonc
// request
{ "provider_job_ref": "task_abc" }
// response
{ "canceled": true,  "effective": "immediate" }     // 或
{ "canceled": false, "reason": "not_cancelable", "note": "任务已完成" }  // 或
{ "canceled": false, "reason": "cancel_requested", "note": "供应商接受取消请求，终态待定" }
```

三种语义必须区分：

- `canceled: true` → S26 转 `CANCELED`（仍要结算）
- `not_cancelable` → S26 保持 `RUNNING`，加 `discard_on_completion`
- `cancel_requested` → S26 保持 `RUNNING`，跟踪到终态

`cancel` **不代表退款**（`docs/12`）。

### 3.7 `verify_callback`

不是 HTTP 方法，是回调入口的处理步骤。见 §6。

### 3.8 `fetch_manifest`

```jsonc
// request
{ "provider_job_ref": "task_abc", "refresh_urls": true }
// response
{ "manifest": [ { "role":"video","url":"https://.../out.mp4?sig=NEW...", "url_expires_at":"..." } ] }
```

`refresh_urls: true` 是 **E23 的关键**：供应商 URL 过期时，先查新 URL（`docs/09`："URL 过期先查询新 URL，不能直接重做付费生成"）。

对不支持刷新 URL 的渠道，`refresh_urls: true` 返回 `{"refresh_supported": false}`。S26 据此走 §8.3 的后续分支。

---

## 4. 输入结构：`submit_payload`

`submit_payload` 由 S26 生成（来自 S25 的 `normalized_input`），S27 只翻译。定义（与 `generation-command.v2` 的 `input` 对齐并已吸附）：

```jsonc
{
  "modality": "video",
  "model_id": "seedance",
  "model_snapshot": "250528",
  "mode": "first_last_frame",
  "prompt": {"text": "...", "negative": null},
  "duration_seconds": 5,
  "aspect_ratio": "9:16",
  "resolution": {"width": 1080, "height": 1920},
  "references": [
    {"role":"first_frame","artifact_id":"art_...","sha256":"…","url":"…","url_expires_at":"…"}
  ],
  "audio": {"strategy":"silent_then_tts","voice_binding":null},
  "required_capabilities": ["image_to_video","preserve_identity"],
  "allow_trim": true,
  "seed": 42,
  "callback_url": "https://s27-<channel>.<host>/callbacks/<channel_route>",
  "metadata": {
    "tenant_id": "tenant_demo",
    "attempt_id": "attempt_video_demo",
    "provider_job_id": "pj_...",
    "dispatch_id": "dsp_...",
    "node_id": "shot_3_2.video",
    "node_epoch": 4,
    "execution_fence": 2
  }
}
```

### 4.1 三种 modality 的差异

S27 的每个渠道适配器实现一个 `SubmitPayloadMapper`：

```ts
interface SubmitPayloadMapper<NativeRequest> {
  readonly channelRoute: string;
  readonly supportedModes: ReadonlySet<Mode>;
  toNative(payload: SubmitPayload, ctx: SubmitContext): NativeRequest;
  fromNativeResponse(res: NativeResponse): SubmitReceipt;
  fromNativeStatus(res: NativeStatus): Observation;
  readonly idempotencyClass: 'supported' | 'client_lookup_only' | 'unsupported';
  readonly cancelSemantics: 'immediate' | 'request_only' | 'unsupported';
  readonly refreshUrlSupported: boolean;
}
```

映射的**硬规则**（代码检查，不是文档）：

1. `toNative` 不得读取任何环境变量或配置来决定 `model`。模型只能来自 `payload.model_id`/`model_snapshot`。若供应商 API 用别的名字（如 `model_name`），映射表里写死映射，不存在"默认模型"。
2. `toNative` 不得丢弃 `required_capabilities` 中的任何一项。若某渠道无法表达某个 required capability，`validate` 必须返回错误——不能静默忽略。
3. `fromNativeStatus` 必须把供应商的所有状态映射到 6 个平台状态之一（`queued|running|succeeded|failed|canceled|not_found`）。**未知状态值必须报 `inconclusive`，不能猜。**
4. `fromNativeResponse` 的 `submit` 结果必须是 `accepted | rejected | unknown | not_sent` 四选一（§5.3）。

各渠道的差异举例（真实实现时以 `docs/17` 的实测为准）：

| 渠道 | 模式命名 | 时长档 | 回调 | 幂等 |
|---|---|---|---|---|
| kling/api | `img2video` / `txt2video` | 5s/10s | HMAC | 待实测 |
| seedance/byteplus | 与 volcengine 同源但不同 endpoint | 5s/10s | 支持 | 待实测 |
| seedance/volcengine | `i2v` / `flf2v` | 5s/10s | 支持 | 待实测 |
| veo/official | `predictLongRunning` operation | 4s/6s/8s | 轮询 operation | 待实测 |
| wan/official | 通义万相 | 5s | 支持 | 待实测 |
| vidu/api | — | — | — | 待实测 |

`docs/17`："以上均未执行任何 API 调用或账户能力验证。" 上表里的"待实测"就是这句话的落点：**实现时必须先跑账号探针，把实测值写进 `connector.yaml` 的 `evidence`，才允许 `status: active`**。

---

## 5. `submit`：付费路径的完整协议

### 5.1 触发方式

S26 通过 Kafka 事件 `connector.submit.requested` 触发（S26 §10.3）。不是 S26 的同步 RPC。

S27 侧的处理：

```
consume(event) →
  if inbox has (consumer, event.id): return           # 去重
  if receipt exists for dispatch_id: return           # 已处理过（更强的去重）
  claim grant: POST {S26}/dispatch-grants/{id}:claim
    on 409/410/403: mark command done, emit observation "not_submitted", return
    on network error: retry claim with backoff; DO NOT submit
  on 200:
    INSERT receipt(dispatch_id, state='SENDING', ...)  # 先落库
    COMMIT
    send HTTP
    UPDATE receipt(...)
    INSERT outbox(observation)
    COMMIT
```

### 5.2 两道门

1. **`REQUIRE_DISPATCH_GRANT`**：编译期常量 `true`。不是 env，不是配置项。单测断言没有把它读成变量。
2. **`connector_request_receipt` 的 `dispatch_id` 唯一约束**：即使 claim 被重放，第二次 INSERT 会冲突，S27 直接结束。

```sql
CREATE TABLE connector_request_receipt (
  receipt_id       text PRIMARY KEY,
  dispatch_id      text NOT NULL UNIQUE,        -- 幂等锚点
  provider_job_id  text NOT NULL,
  tenant_id        text NOT NULL,
  channel_route    text NOT NULL,
  stable_client_key text NOT NULL,
  fencing_token    bigint NOT NULL,
  request_hash     char(64) NOT NULL,
  state            text NOT NULL,               -- SENDING|SENT|RESPONDED|ABANDONED
  outcome          text,                        -- accepted|rejected|unknown|not_sent
  http_status      integer,
  provider_job_ref text,
  error_class      text,                        -- transport|rate_limit|auth|policy|invalid_input|provider_5xx|timeout
  request_bytes_sha256 char(64) NOT NULL,
  sent_at          timestamptz,
  responded_at     timestamptz,
  bytes_written_at timestamptz,                 -- 见 §5.4
  created_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (state IN ('SENDING','SENT','RESPONDED','ABANDONED')),
  CHECK (outcome IS NULL OR outcome IN ('accepted','rejected','unknown','not_sent'))
);
CREATE UNIQUE INDEX ux_receipt_client_key ON connector_request_receipt (tenant_id, stable_client_key);
CREATE INDEX ix_receipt_stale ON connector_request_receipt (created_at) WHERE state='SENDING';
```

`ux_receipt_client_key` 是第二重保护：即使有人错误地生成了新的 `dispatch_id`，同一个 `stable_client_key` 也只能有一条回执。这就是"不允许在重发时重新生成 nonce"的数据库级执行点。

### 5.3 四种结果与判定

`submit` 的结论必须是这四者之一，且**由 socket 层事实决定**，不是猜测：

| 结论 | 判定条件 | S26 的后续 |
|---|---|---|
| `accepted` | HTTP 2xx 且能解析出 `provider_job_ref` | `QUEUED` / `RUNNING` |
| `rejected` | HTTP 4xx 且供应商明确表示未受理（有错误体，且错误码在"未受理"集合内） | `PROVIDER_FAILED`（可能按失败计费） |
| `not_sent` | **能证明请求字节没有离开本机**：DNS 解析失败、TCP connect 失败、TLS 握手失败、连接池拒绝、请求构造异常 | 可安全重试（同 key） |
| `unknown` | 其余一切：响应超时、连接被重置（写完之后）、5xx、本进程在写完之后崩溃、收到无法解析的 2xx | `SUBMISSION_UNKNOWN` |

实现要点：

```ts
function classify(err: unknown, phase: 'before_write' | 'after_write'): Outcome {
  if (phase === 'before_write') {
    // DNS/TCP/TLS/连接池 —— 字节未发出
    return 'not_sent';
  }
  // 已写出：任何失败都是 unknown，哪怕看起来像"连接被拒"
  return 'unknown';
}
```

`phase` 的确定方式：发请求前写 `receipt.state='SENDING'`；真正写出请求体后更新 `bytes_written_at`。**`bytes_written_at IS NULL` 是 `not_sent` 的唯一充分条件**。这在进程崩溃后也成立——回执表里 `state='SENDING' AND bytes_written_at IS NULL` 表示"我们不知道有没有写出去"，按最坏情况处理为 `unknown`。

等等，这里要精确：如果崩溃时 `bytes_written_at IS NULL`，我们其实不知道有没有写出去（可能刚好要写）。所以**崩溃恢复后的 SENDING 一律按 `unknown` 处理**。只有**运行中的进程**能可靠说"我在 connect 阶段失败"，那时 `bytes_written_at` 确实是 null 且有明确的错误类型。

这条区别必须写进代码：

```ts
// 运行中：可以判定 not_sent
if (err.phase === 'connect' || err.phase === 'dns' || err.phase === 'tls') return 'not_sent';
// 恢复时：一律 unknown
if (receipt.state === 'SENDING' && receipt.bytes_written_at === null) return 'unknown';
```

### 5.4 供应商幂等性的三种处理

| `idempotencyClass` | S27 行为 |
|---|---|
| `supported` | 把 `stable_client_key` 放进供应商的幂等头/字段。重发安全。 |
| `client_lookup_only` | 供应商接受自定义 metadata；把 `stable_client_key` 放进去，供后续查询。重发**不**自动进行（S26 决定）。 |
| `unsupported` | 无幂等机制。`stable_client_key` 仍记录在本地回执，但供应商不知道。重发 = 新任务。 |

`unsupported` 的渠道，S27 必须**额外**做一件事：在 submit 成功后，立刻把 `provider_job_ref` 与 `stable_client_key` 的映射写进**本地** `connector_client_key_index`，这样即使 S26 的对账要查，S27 也能提供"我发出过这个 key 吗"的答案。

```sql
CREATE TABLE connector_client_key_index (
  stable_client_key text PRIMARY KEY,
  dispatch_id       text NOT NULL,
  provider_job_ref  text,
  channel_route     text NOT NULL,
  tenant_id         text NOT NULL,
  first_seen_at     timestamptz NOT NULL DEFAULT now()
);
```

这张表是 S26 对账 `provider_idempotency=unsupported` 时的**唯一本地证据**：S27 能回答"这个 key 我发出过，回执是 accepted，provider_job_ref 是 X"或"这个 key 我从未发出过"。但注意：它只能证明"S27 这边发生了什么"，不能证明"供应商是否收到"。后者仍需 S26 走账单对账。

### 5.5 `submit` 的超时

S27 的 HTTP 超时应当**长于**供应商的可能处理时间（提交是异步任务，通常 1–30s 返回 job id）。设 `submit_http_timeout = 60s`。

超时后 S27 上报 `unknown`，**不重试**。S26 起 `submit-confirm timer`（更长），到期仍无回执 → 也是 `unknown`。

两层的超时不是冗余：S27 的超时是"我等不到响应"，S26 的 timer 是"我连 S27 的回执都没收到"（S27 崩溃/卡死）。后者在 S27 完全失联时才触发。

---

## 6. 回调：验签、先 ACK、再异步映射

`docs/09`："回调验签进 Inbox 并快速 ACK，异步确认映射。"

### 6.1 入口

```
POST /callbacks/{channel_route}
Headers: X-Signature, X-Timestamp, X-Event-Id
Body: 供应商原始 JSON（不解析就存）
```

### 6.2 处理顺序（顺序不能变）

```
1. 校验 X-Timestamp 在 ±300s 内                    → 否则 401（防重放）
2. 从 secret store 取该渠道的 callback secret
3. HMAC-SHA256(raw_body, secret) 与 X-Signature 比对（常数时间）
                                                   → 否则 401 + 审计
4. 按 (X-Event-Id) 或 body hash 去重                → 已存在则直接 200（重复回调必须 ACK）
5. INSERT connector_callback_inbox(raw_body, sig_verified=true, state='RECEIVED')
6. COMMIT
7. 返回 200 {"ok":true}          ← ACK 在映射之前！
   后台：异步任务解析 → 映射到 observation → outbox → S26
```

**第 7 步在映射之前**是刻意的：供应商的回调常有 5s 超时和重试；如果先映射再 ACK，映射出问题（比如 DB 慢）会导致供应商重试，放大问题。先 ACK 表示"我收到了"，映射失败在本地重试，不依赖供应商回调重试。

代价：如果 S27 在 ACK 后崩溃、映射没做，回调数据仍在 `connector_callback_inbox`（`state='RECEIVED'`），恢复后继续映射。**Inbox 落库必须先于 ACK**，否则 ACK 后崩溃会丢回调。这就是第 5 步必须在第 7 步之前的原因。

### 6.3 签名细节

```ts
function verify(rawBody: Buffer, headers: Headers, secret: Buffer): boolean {
  const ts = headers.get('x-timestamp');
  if (!ts || Math.abs(Date.now()/1000 - Number(ts)) > 300) return false;
  const signed = Buffer.concat([Buffer.from(ts), Buffer.from('.'), rawBody]);
  const expected = crypto.createHmac('sha256', secret).update(signed).digest('hex');
  const got = headers.get('x-signature') ?? '';
  return expected.length === got.length &&
         crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(got));
}
```

要点：

- **必须用原始字节**，不能用 `JSON.stringify(parsed)`——键序、空格、转义都会变。
- 常数时间比较（`timingSafeEqual`）。
- 时间窗防重放。
- 每渠道独立 secret（隔离）。
- 验签失败：401 + 审计 + 指标 `callback_signature_invalid_total{channel}`。**不落到 Inbox**（否则伪造的回调会污染数据）。

有些供应商的签名方案不同（有的只在 body 里签名，有的用非对称）。每个渠道适配器实现自己的 `CallbackVerifier`，但接口一致。

### 6.4 映射

```ts
interface CallbackVerifier {
  verify(rawBody: Buffer, headers: Headers): { ok: boolean; reason?: string };
}
interface CallbackMapper {
  // 供应商回调 → 平台观测（可能多个，例如 status + output 同时到）
  toObservations(event: unknown): Observation[];
  // 回调里能拿到关联键吗？
  correlate(event: unknown): { client_key?: string; provider_job_ref?: string };
}
```

关联是回调的难点：**回调可能先于 submit 的响应到达**（E17）。

处理：
1. 回调里若有 `stable_client_key`（我们放在 metadata 里的），直接关联；
2. 若只有 `provider_job_ref`，查 `connector_request_receipt`（若 submit 已返回）；
3. 都关联不上 → 标记 `state='UNMAPPED'`，保留 24h，等 submit 回执到达后回填；
4. 超过 24h 仍无法关联 → 告警 + 进 dead-letter，**不丢弃**。

`UNMAPPED` 的回调是 `docs/16 E17`（"回调早到"）的落点。S26 收到带 `provider_job_ref` 但 job 尚未记录的观测时，也必须能按 `client_key` 挂起（S26 §7.3）。

### 6.5 回调的观测结构

```jsonc
{
  "specversion":"1.0","schema_version":"2.0",
  "id":"obs_...","source":"S27","type":"connector.observation.received",
  "subject":"connector-observation/obs_...",
  "tenant_id":"tenant_demo","home_cell":"cell_a1",
  "aggregate_id":"pj_...",                     // 能关联上就填，否则为空（UNMAPPED）
  "aggregate_sequence": null,                  // 由 S26 分配
  "data": {
    "channel_route":"seedance/volcengine",
    "dispatch_id":"dsp_...",
    "stable_client_key":"dr-…",
    "provider_job_ref":"task_abc",
    "source":"poll" | "callback",
    "observed_at":"...",
    "provider_status_raw":"SUCCESS",
    "status":"succeeded",
    "terminal":true,
    "output": { "manifest":[ ... ] },
    "provider_usage": {...}
  }
}
```

`aggregate_sequence` 由 S26 分配而不是 S27——因为 S27 看不到 job 的完整序列。S26 消费时用自己的 `job_transition.seq`。

---

## 7. 轮询

对 `execution_kind=poll` 的渠道，S27 不主动定时轮询。**S26 驱动**：S26 的 `due_jobs` 扫描发现某 job 到 `next_action_at`，发 `connector.status.requested` 命令事件，S27 执行一次 `get_status` 并上报。

为什么不让 S27 自己轮询：
1. 轮询节奏属于任务策略（`docs/09`：退避、优先归档），策略在 S26；
2. S27 重启后不需要恢复"我在轮询哪些任务"的状态；
3. 渠道隔离更容易：S27 只处理"给我查这一个"的请求，不持有调度状态。

S26 侧的轮询退避（示意）：`5s → 10s → 20s → 40s → 60s cap`，接近 deadline 时加快，`succeeded` 立即触发归档。

---

## 8. 渠道健康与熔断

### 8.1 本地熔断（S27 侧）

```sql
CREATE TABLE channel_health (
  channel_route     text PRIMARY KEY,
  breaker_state     text NOT NULL,        -- closed|open|half_open
  consecutive_fail  integer NOT NULL DEFAULT 0,
  opened_at         timestamptz,
  retry_after_ms    integer,
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CHECK (breaker_state IN ('closed','open','half_open'))
);
```

- 连续 5 次 `transport` 或 `provider_5xx` → `open`，持续 30s → `half_open`（放一个请求试探）。
- `auth`（401/403）→ 立即 `open` 且**不自动恢复**，等人处理。同时上报 S24 `pool_health='unauthorized'`（S26 也会报，双保险）。
- `rate_limit`（429）→ 不熔断，用 `retry_after_ms`。

熔断打开时，S27 收到 `submit.requested` 怎么办？

**关键决策：不能静默丢弃，也不能假装失败。**

- 若 grant 尚未 claim → 不 claim，直接上报 `not_sent`（`error_class='channel_circuit_open'`），S26 会把 job 放回 `ADMITTED`，可以重新签发（未来重试）。
- 若 grant 已 claim（不应发生，因为 claim 在 submit 之前，熔断检查在 claim 之前）→ 按 `not_sent` 处理仍安全，因为确实没发。

熔断必须发生在 claim **之前**。顺序：`检查熔断 → claim → 落 SENDING → 发 HTTP`。

### 8.2 与 S24 的关系

`channel_health` 是**瞬时的传输层状态**，不是账号能力事实。S24 的 `provider_account_pool.health` 是**持久的业务事实**（`healthy|suspect|unauthorized|exhausted|disabled`）。

- S27 熔断 → 上报 S24（`connector.health.degraded` 事件或 S24 的 probe 周期发现），S24 决定池健康。
- S24 池健康 → S25 在硬过滤阶段排除，S26 在步骤 4 复查时拦截。

S27 不直接改 S24 的表（`database-per-service`）。S27 **不**把自己的熔断状态用于路由——它只上报。

---

## 9. S27 ⇄ S26 的环（另一半）

S26 侧见 §10。这里只补 S27 的视角，确保两侧对同一套事件名与语义一致。

### 9.1 S27 消费的事件

| 事件 | Topic/分区 | 触发动作 |
|---|---|---|
| `connector.submit.requested` | `drama.provider-connectors.v2`，按 `channel_route` 分区 | claim → submit |
| `connector.status.requested` | 同上 | `get_status` → 上报 |
| `connector.cancel.requested` | 同上 | `cancel` → 上报 |
| `connector.manifest.requested` | 同上 | `fetch_manifest` → 上报 |

### 9.2 S27 发出的事件

全部发到 `drama.provider-connectors.v2`，`type = connector.observation.received`，`aggregate_id = provider_job_id`（可空）。

S26 是唯一消费者（除 S47 投影）。

### 9.3 环的形式化（合并两侧）

```
         ┌────────────── Kafka: connector.submit.requested ──────────────┐
         │                                                                ▼
      ┌──┴───┐                      HTTP :claim                       ┌────┴───┐
      │ S26  │ ◀───────────────────────────────────────────────────  │  S27   │
      └──┬───┘                                                        └────┬───┘
         │                                                                 │
         └─────────── Kafka: connector.observation.received ───────────────┘
```

两条 Kafka 边是异步的、可重复投递的、由 Outbox 保证原子性。
唯一同步边是 `:claim`，方向 S27→S26，S26 是服务端，**没有反向同步依赖**。

所以：

- 启动顺序任意：S26 先起 → 事件堆着；S27 先起 → claim 失败重试。
- S26 挂了 → S27 claim 失败，任务停在 `DISPATCH_AUTHORIZED`，grant 过期后由 S26 撤销重签，不花钱。
- S27 挂了 → 事件堆着，grant 过期，可重签。
- 两边都挂 → 恢复后从各自 Outbox/表继续，Kafka 保留 14 天（`event-topics.v2.json`）。

**关键前提**：所有"我做了什么"都先落本地表 + Outbox，再发事件。不能"先发事件再落库"。这是 Outbox 模式的全部意义。

### 9.4 为什么不能"同时启动就行"

如果靠"无事件、S26 启动后调 S27"：

1. 两个 Deployment 滚动重启时必然有窗口其中一方不在；
2. 不在的那个窗口里的任务是丢失而不是延迟；
3. 无法处理"发出请求后不知道结果"——因为同步调用本身产生未知。

事件化之后，**"延迟"是唯一的失败形态**，而延迟可以被 grant 过期 + 重签安全吸收，因为从未领取过 grant 就没有付费风险。

---

## 10. 安全

`docs/14` 的要求落到 S27：

| 威胁 | 措施 |
|---|---|
| 凭证泄露 | 每渠道独立 Secret，仅该渠道 Pod 挂载；只读；轮换不影响代码 |
| SSRF | S27 只对白名单域名出网（`NetworkPolicy` + 应用层 `allowed_hosts`）；`callback_url` 等外部 URL 不主动访问（S28 负责） |
| 回调伪造 | HMAC + 时间窗 + 常数时间比较；失败不入 Inbox |
| 回调重放 | `X-Event-Id` 去重 + 时间窗 |
| 恶意响应体 | 上游响应有大小上限（如 2MB）；只解析必要字段；不信任 `content_type` |
| 供应链 | 连接器镜像独立构建、独立签名、独立扫描 |
| Prompt 泄露 | S27 日志**不记录 prompt 全文**；只记 hash 与长度。请求体存对象存储用服务端加密，S27 无长期读取权限 |
| 跨租户 | S27 不按 tenant 分区数据但也**从不跨 dispatch 混用**；每份凭证只服务其企业/共享池 |

### 10.1 secret 引用

```yaml
# connector.yaml 片段
credentials:
  ref: "secret://providers/seedance-volcengine"
  version: "2026-10-01"
  rotation_policy: "90d"
```

S27 启动时解析 `ref` 为挂载路径。**不在代码里内联任何 key**，不在日志里打印。

---

## 11. 数据模型汇总

```sql
CREATE TABLE connector_command_inbox (
  consumer_id   text NOT NULL,           -- s27-<channel>-<replica>
  event_id      text NOT NULL,
  event_type    text NOT NULL,
  aggregate_id  text,
  payload_hash  char(64) NOT NULL,
  received_at   timestamptz NOT NULL DEFAULT now(),
  processed_at  timestamptz,
  PRIMARY KEY (consumer_id, event_id)
);

CREATE TABLE connector_callback_inbox (
  event_key     text PRIMARY KEY,        -- X-Event-Id 或 sha256(raw_body)
  channel_route text NOT NULL,
  tenant_id     text,
  raw_body      bytea NOT NULL,
  signature_ok  boolean NOT NULL,        -- 恒为 true（验签失败不入库）
  correlation   jsonb NOT NULL,          -- {client_key?, provider_job_ref?}
  state         text NOT NULL,           -- RECEIVED|MAPPED|UNMAPPED|DEAD_LETTER
  received_at   timestamptz NOT NULL DEFAULT now(),
  mapped_at     timestamptz,
  CHECK (state IN ('RECEIVED','MAPPED','UNMAPPED','DEAD_LETTER'))
);

CREATE TABLE connector_request_receipt ( ... );   -- §5.2
CREATE TABLE connector_client_key_index ( ... );  -- §5.4
CREATE TABLE channel_health ( ... );              -- §8.1

CREATE TABLE connector_observation_outbox (
  id            bigserial PRIMARY KEY,
  tenant_id     text NOT NULL,          -- 事件信封 required
  channel_route text NOT NULL,
  aggregate_id  text,
  aggregate_sequence bigint,
  event_type    text NOT NULL,
  partition_key text NOT NULL,          -- tenant_id || ':' || aggregate_id
  payload       jsonb NOT NULL,
  headers       jsonb NOT NULL DEFAULT '{}'::jsonb,
  published_at  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX coo_unpublished_idx ON connector_observation_outbox (id) WHERE published_at IS NULL;
```

---

## 12. `connector.yaml` 的生成与状态

`AGENTS.md`："`connectors/*/*/connector.yaml` 从 `contracts/service-catalog.v2.json` 派生。改内容请改 catalog 再重新生成。"

当前九个文件的结构一致：

```yaml
channel: seedance/volcengine
status: disabled
lifecycle: disabled
evidence_checked_at: null
protocol: [capabilities, validate, estimate, submit, get_status, cancel, verify_callback, fetch_manifest]
may_change_model: false
may_adjust_budget: false
may_accept_output: false
```

**激活流程（必须三步都完成）**：

1. 跑 `account_probe`（S24 的 probe-worker，只读、免费）→ 填 `evidence_checked_at`
2. 跑 `combination_probe`（对每个要用的组合，用 S26 + `budget_scope=platform.capability_probe` 的真实付费探针，一次，结果写回 S24）
3. 改 catalog 里该渠道的 `status: active` 与 `lifecycle: active`，重新生成 yaml

**第 2 步是付费的**，需要实施会话明确授权（`AGENTS.md`："需要真实付费生成 → 停下，等实施会话明确授权"）。

在这三步完成前：

- `connector.yaml` 保持 `disabled`
- S24 的 `provider_channel` 保持不可派单
- S25 的硬过滤会排除它
- S26 即使收到命令也签发不了 grant（步骤 4 的 S24 复查会失败）

四层防护，任何一层单独失效都不导致误付费。

---

## 13. 失败分支

| 情况 | S27 上报 | S26 处理 | 付费风险 |
|---|---|---|---|
| 熔断打开 | `not_sent` / `channel_circuit_open` | 回 `ADMITTED`，稍后重签 | 无 |
| claim 失败（S26 不可用） | 不 submit，重试 claim | — | 无 |
| claim 被拒（已领/过期/撤销） | `not_sent` / `grant_unavailable` | 检查状态 | 无 |
| DNS/TCP/TLS 失败 | `not_sent` / `transport` | 可重试 | 无 |
| 供应商 429 | `not_sent`（未受理）+ `retry_after_ms` | 定时器退避 | 无 |
| 供应商 4xx 参数错误 | `rejected` / `invalid_input` | `PROVIDER_FAILED` + capability drift | 无（或最低收费） |
| 供应商 4xx 政策拒绝 | `rejected` / `policy` | `PROVIDER_FAILED`，**不换渠道** | 无 |
| 供应商 401/403 | `rejected` / `auth` + 熔断 | `ACCOUNT_UNAUTHORIZED`，隔离池 | 无 |
| 供应商 5xx（写完之后） | `unknown` / `provider_5xx` | `SUBMISSION_UNKNOWN` | **有** |
| 响应超时（写完之后） | `unknown` / `timeout` | `SUBMISSION_UNKNOWN` | **有** |
| 连接重置（写完之后） | `unknown` / `transport` | `SUBMISSION_UNKNOWN` | **有** |
| 2xx 但无法解析 job ref | `unknown` / `bad_response` | `SUBMISSION_UNKNOWN` | **有** |
| 进程在 SENDING 崩溃 | 恢复后标 `unknown` | `SUBMISSION_UNKNOWN` | **有** |
| 回调验签失败 | 401，审计 | S26 不知情 | 无 |
| 回调无法关联 | `UNMAPPED` 保留 | — | — |
| 回调乱序 | 原样上报，不排序 | S26 按 `aggregate_sequence` 拒绝倒退 | — |
| 归档 URL 过期 | `fetch_manifest(refresh)` 结果 | S26 §8.3 | 无（不重生成） |

---

## 14. 可观测

| 指标 | 告警 |
|---|---|
| `s27_submit_total{channel,outcome}` | `unknown` 占比高 |
| `s27_submit_latency_seconds{channel}` | p99 超时 |
| `s27_http_status_total{channel,status}` | 4xx/5xx 突增 |
| `s27_callback_total{channel,verified,state}` | 验签失败数 |
| `s27_callback_unmapped_age_seconds` | 超 24h |
| `s27_breaker_state{channel}` | `open` 持续 |
| `s27_rate_limited_total{channel}` | 429 频繁 → 需要调 rps |
| `s27_receipt_stale_sending` | SENDING 超 5min |
| `s27_claim_failed_total{reason}` | S26 可用性 |
| `s27_payload_mapper_errors_total{channel,code}` | 映射 bug |
| `s27_credential_age_days{channel}` | 接近轮换期 |

日志脱敏规则：不记 prompt、不记 key、不记完整 URL（记 host + path 的 hash）、响应体只记状态与错误码。

追踪：`traceparent` 从 S26 的命令事件带进来，一直传到供应商 HTTP 与回调。

---

## 15. 测试清单

| 序号 | 场景 | 断言 |
|---|---|---|
| T27-01 | `submit` 无 grant | 拒绝，`not_sent`，无 HTTP |
| T27-02 | 同一 `dispatch_id` 命令重复投递 5 次 | 只 1 次 HTTP，1 条 receipt |
| T27-03 | 同一 `stable_client_key` 不同 `dispatch_id` | 第二条 receipt 冲突，拒绝 |
| T27-04 | claim 返回 409 | 不 submit |
| T27-05 | claim 网络失败 3 次后成功 | 只 1 次 HTTP（在成功 claim 之后） |
| T27-06 | connect 阶段失败 | `not_sent` |
| T27-07 | 写出后超时 | `unknown`，receipt 有 `bytes_written_at` |
| T27-08 | 写出后连接重置 | `unknown` |
| T27-09 | SENDING 时进程崩溃，恢复 | 标 `unknown`，**不重发** |
| T27-10 | 429 + `Retry-After: 30` | `not_sent` + `retry_after_ms=30000`，不睡、不重试 |
| T27-11 | 供应商 5xx | `unknown` |
| T27-12 | 2xx 无 job ref | `unknown` |
| T27-13 | 401 | 熔断 `open`，`rejected/auth` |
| T27-14 | 政策拒绝 | `rejected/policy`，无重试、无换渠道 |
| T27-15 | 熔断打开时收到命令 | `not_sent`，无 HTTP，无 claim |
| T27-16 | 回调伪签名 | 401，不进 Inbox，审计 + 指标 |
| T27-17 | 回调超时窗（±300s 外） | 拒绝 |
| T27-18 | 回调重复 5 次 | 1 条 Inbox，1 条观测 |
| T27-19 | 回调先于 submit 响应 | Inbox 存为 `UNMAPPED`，submit 回执到达后映射 |
| T27-20 | 回调乱序 | 原样上报，S26 拒绝倒退 |
| T27-21 | 回调无法关联 24h | `DEAD_LETTER` + 告警，不丢弃 |
| T27-22 | `fetch_manifest(refresh_urls)` 支持 | 返回新 URL |
| T27-23 | `fetch_manifest(refresh_urls)` 不支持 | `refresh_supported: false`，S26 走 §8.3 |
| T27-24 | 映射器丢弃 required capability | `validate` 必须拒绝（单测覆盖每个渠道） |
| T27-25 | `toNative` 试图读配置改 model | 代码级单测：mapper 无此访问路径 |
| T27-26 | 九个渠道 yaml 全 `disabled` | CI 断言 `status != active` 且 `evidence_checked_at == null` |
| T27-27 | `sora/historical` | 无 Deployment，无消费者；事件滞留 + dead-letter 告警 |
| T27-28 | 渠道隔离：kling Pod 全挂 | minimax/veo 的 submit 正常 |
| T27-29 | 日志脱敏 | 扫描日志断言无 prompt、无 key、无完整 URL |
| T27-30 | 每渠道独立 Secret | 删除 seedance Secret 不影响 veo |

---

## 16. 实施顺序与未决

顺序：

1. `SubmitPayloadMapper` 接口 + `FakeChannel`（用于本地开发与契约测试）。
2. `connector_request_receipt` + claim 流程 + 四态判定（`not_sent/accepted/rejected/unknown`）。
3. Inbox/Outbox + 去重 + claim 重试。
4. 回调入口：验签 + ACK-first + 异步映射 + UNMAPPED。
5. 轮询命令处理（由 S26 驱动）。
6. 熔断与 `channel_health`。
7. 第一个真实渠道适配器（需授权与探针）。
8. 其余渠道按优先级（`docs/17` 的生命周期与合同约束决定顺序）。
9. `sora/historical` 保持不部署。

未决：

- **九个渠道的 `idempotencyClass` 必须实测**。`sora` 已停用不用管；其余八个要靠 `account_probe` 确认。这是 S24 未决问题的另一半。搞错的后果是重复付费（以为能同键去重，实际供应商建了两个任务）。
- **`submit_http_timeout` 各渠道不同**。有的供应商 3s 返回 job id，有的要 30s。先统一 60s，实测后按渠道调。
- **回调 secret 的轮换**。供应商通常允许双 secret 并存（旧 + 新），S27 的验签要支持"任一匹配即通过"在轮换窗口内。需要各供应商确认。
- **`wan/self-hosted` 没有"供应商回调"**，它的观测完全靠轮询内网推理集群。`cancel` 语义也可能是"尽力"。需要在适配器里显式处理这种"没有回调"的渠道——`verify_callback` 对该渠道返回 404，不注册路由。
- **`docs/17` 说的 "Veo 3.1 Preview in Gemini API 于 2026-10-22 停用"** —— 需要确认 `veo/official` 指向的是不是这个已停用的端点的继任者，还是根本还没确定。如果是后者，`veo/official` 与 `sora/historical` 处境相同：占位，不部署。
- **回调的 `UNMAPPED` 保留期 24h** 是拍的。如果某渠道的 submit 回执可能更晚（极慢的提交），需要延长。取决于实测 p99。
