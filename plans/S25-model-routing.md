# S25 · 模型路由与估价 —— 实现方案

状态：`design_only`。本文是实现方案，不是已实现代码。

上游依据：[docs/09 §1](../docs/09-model-execution.md)、[docs/12 §1](../docs/12-budget-scheduling.md)、[docs/11 §3](../docs/11-quality-repair.md)、`plans/S24-model-registry.md`。
链上位置：`S24 注册事实 → S25 算可行路由 → S26 持久化任务与准入 Saga → S27 按渠道执行`。

---

## 1. 边界

### 1.1 本服务写什么

- `RouteDecision`：一次路由的完整结论（候选、评分、拒绝原因、报价上界、策略版本、有效期）。
- `CandidateRejection`：每个被排除候选的**唯一原因码**。
- `RoutingPolicySnapshot`：评分权重与策略版本（由 S02 授权，S25 只存其生效快照）。
- 报价有效期（`quote_valid_to`）。

### 1.2 本服务不写

- **不写余额**（S23），不写 permit（S22），不写任务（S26）。
- **不预留任何东西**：`RouteDecision` 是"如果现在派，会走哪条、大概多少钱"，不构成资金或配额承诺。
- **不写能力事实**（S24）。S25 只消费 S24 的 `model.capability.changed` 建投影，并在估价时用 `GET /capability-validity` 复核。
- **不生产 Prompt**（S20）。S25 消费 `CompiledPromptManifest` 的引用，用它做兼容性判断。

### 1.3 一个必须坚持的原则

> **硬过滤与评分是两段，中间没有回退路径。**

`docs/09`：`不允许低价覆盖硬约束`。实现上体现在数据结构：被硬过滤掉的候选**根本不进入评分列表**，不是"评分很低所以排后面"。S25 的输出里两者在**不同字段**（`eligible[]` 与 `rejected[]`），下游无法用"取排名第一"的方式意外选中一个被硬过滤的候选。

---

## 2. 输入：路由请求长什么样

`POST /route-decisions`（OpenAPI 已定义 `estimateRoute`，body 为 `{generation_command, command_id}`）。

S25 需要的字段全部来自 `generation-command.v2.schema.json`：

| 需求 | 来自 |
|---|---|
| 模态 | `modality` |
| 必需能力键 | `input.required_capabilities`（video/image）, `input.mode`（image), `input.audio_strategy`（video） |
| 参考组合 | `input.references[].role` + count |
| 时长 / 画幅 / 分辨率 | `input.duration_seconds` / `input.aspect_ratio` / `input.resolution` |
| 对白时长 | 见 §2.2 |
| 声音与音色 | `input.audio_strategy`, `input.voice_binding`, `input.text`（speech） |
| 期限 | `deadline` |
| 预算 scope | `budget_scope_id` |
| 政策 | `context.policy_ref` |
| 模板 | `template_ref` |

### 2.1 从命令派生的硬需求（`RequirementSet`）

```jsonc
{
  "modality": "video",
  "mode": "first_last_frame",
  "required_capabilities": ["image_to_video","preserve_identity","camera_control"],
  "references": [
    {"role":"character","count":1},
    {"role":"first_frame","count":1},
    {"role":"last_frame","count":1}
  ],
  "audio": {"strategy":"silent_then_tts","needs_native_dialogue":false,"needs_voice_lock":false},
  "duration_seconds": 4,
  "aspect_ratio": "9:16",
  "resolution": {"width":1080,"height":1920},
  "deadline": "2026-10-29T23:30:00+08:00",
  "tenant_id": "tenant_demo",
  "home_region": "region_demo",
  "policy_ref": {"service_id":"S02","id":"policy_demo","revision":3,"content_hash":"..."},
  "budget_scope_id": "budget_ep3",
  "lora": false
}
```

### 2.2 对白时长必须来自 S31/S37，不能由 S25 估

`docs/16 E04`："对白超过 4 秒 → 澄清或不可行，不猜目标、不删台词"。

规则：

- 若 `audio_strategy in (native_locked_voice)` 或模态为 `video` 且该镜头有对白：
  S25 从 `template_ref` / 上游传入的 `dialogue_duration_ms` 读取**已对齐的对白时长**（来自 S31 配音 + S37 字级对齐）。
- 缺少该字段 → 整个路由返回 `INFEASIBLE`，`code=DIALOGUE_DURATION_UNKNOWN`。**不估算**。
- 有 `dialogue_duration_ms` 时，候选的可用生成时长必须 `≥ ceil(dialogue_duration_ms/1000) + tail_pause_ms`；否则该候选被硬过滤为 `DIALOGUE_EXCEEDS_DURATION`。

这条是"不删台词"的执行点：S25 没有任何权限把 4 秒对白塞进 4 秒镜头。

### 2.3 参考图 URL 的 TTL 约束进入硬过滤

供应商拉取参考素材有排队时间（docs/10："供应商拉取参考 URL 的 TTL 覆盖预期排队时间"）。S25 计算：

```text
required_ref_ttl = p90_queue_wait(候选) + p90_generation(候选) + 安全余量
```

若上游给出的参考签名 URL TTL < `required_ref_ttl` → 硬过滤 `REFERENCE_URL_TTL_INSUFFICIENT`。这条也决定了 S26 是否必须在派单前重新签名参考 URL（见 S26 §7）。

---

## 3. 硬过滤（HardFilter）

顺序固定，**第一个失败的原因成为该候选的唯一原因码**（便于统计与解释）。顺序按"最便宜、最不会变的判断在前"排列。

| # | 过滤 | 依据 | 原因码 |
|---|---|---|---|
| 1 | 租户授权（能否为该 tenant 使用） | S01 投影 | `CROSS_TENANT_DENIED` |
| 2 | 策略禁用（kill switch、region 禁用、模态禁用） | S02 `policy_ref` | `POLICY_DENIED` |
| 3 | 渠道生命周期 ≠ `active` | S24 投影 | `PROVIDER_DISABLED` / `PROVIDER_RETIRED` / `PROVIDER_DEGRADED` |
| 4 | 账户池不可用（`health != healthy`、BYOK 不匹配） | S24 | `ACCOUNT_UNAVAILABLE` / `BYOK_MISMATCH` |
| 5 | 数据驻留：`data_region` 含未授权地区 | S02 + S24 `residency_tags` | `RESIDENCY_DENIED` |
| 6 | 能力证据过期 / 组合未验证 | S24 `/capability-validity` | `EVIDENCE_EXPIRED` / `COMBINATION_UNVERIFIED` |
| 7 | 模态/模式不支持 | S24 | `MODE_UNSUPPORTED` |
| 8 | 必需能力键缺失或未 `probe_verified` | S24 | `CAPABILITY_MISSING:{key}` |
| 9 | 参考角色数量/顺序/互斥不满足 | S24 `mode_reference_rule` | `REFERENCE_SHAPE_UNSUPPORTED` |
| 10 | 画幅不支持 | S24 `mode_output_spec` | `ASPECT_RATIO_UNSUPPORTED` |
| 11 | 分辨率超过上限 | S24 | `RESOLUTION_UNSUPPORTED` |
| 12 | 时长档位放不下（含对白） | S24 + §2.2 | `DURATION_UNSUPPORTED` / `DIALOGUE_EXCEEDS_DURATION` |
| 13 | 声音：需要原生对白但 `native_audio=false`；需要锁定音色但 `prompt_audio_lock=false` 且无 TTS 分支 | S24 | `AUDIO_STRATEGY_UNSUPPORTED` / `VOICE_LOCK_UNSUPPORTED` |
| 14 | 参考 URL TTL 不足 | §2.3 | `REFERENCE_URL_TTL_INSUFFICIENT` |
| 15 | 报价不可验证（`quotable=false`）或报价已过期 | S24 | `NO_VERIFIABLE_UPPER_BOUND` / `PRICE_EXPIRED` |
| 16 | 报价上界超出 `budget_scope` 的**剩余可承诺额** | S23 只读查询 | `OVER_BUDGET`（无余额时） |
| 17 | 期限不可达：`p90_latency(候选) > deadline - now() - ingress_margin` | S24 + 历史延迟 | `DEADLINE_UNREACHABLE` |
| 18 | 供应商幂等能力与策略不符 | S24 `provider_idempotency` + S02 | `IDEMPOTENCY_POLICY_UNSATISFIED` |
| 19 | LoRA 兼容：`lora=true` 但该 `model_snapshot` 不兼容该 LoRA 基座 | S24 + S48 | `LORA_INCOMPATIBLE` |

### 3.1 关于第 3/4 条的精度

`PROVIDER_DEGRADED` 不等于"完全不能用"。S02 的策略可以有"允许 degraded 但只用于非关键节点"。所以：

- `lifecycle='degraded'` 默认硬过滤；
- 若策略 `allow_degraded_for_non_critical=true` 且节点 `criticality='non_critical'`，则放行，但打分时 `reliability` 分量记 0 并给 `-0.15` 惩罚；
- **`retired` 永远硬过滤**，没有任何策略能放行。

### 3.2 硬过滤绝不因价格放宽

第 16 条（`OVER_BUDGET`）是硬过滤，不是评分项。一个便宜但预算不够的候选与一个贵但预算够的候选，前者不出现在 `eligible[]`。

反方向也成立：`eligible[]` 里有多个候选时，评分可以把贵的排前面（如果质量好得多）。预算上界只是**准入资格**，不是排序依据。

---

## 4. 软评分

只对 `eligible[]` 内的候选评分。评分是加权和，权重来自 `RoutingPolicySnapshot`（S02 授权、按租户/项目可覆盖）。

### 4.1 分量与默认权重

| 分量 | 符号 | 默认权重 | 数据来源 | 说明 |
|---|---|---:|---|---|
| 质量 | `Q` | 0.30 | S36/S44 的历史 QC 通过率（按 `model_snapshot` + `mode` + 节点类型分层） | 带样本数 `n` 与可信水位 |
| 身份保持 | `I` | 0.22 | S11 canonical 比对通过率（角色镜头专用） | 无身份需求时为 0 权重并归一化 |
| 运动/时序 | `M` | 0.12 | S36 时序窗口缺陷率 | 静态镜头时权重下调 |
| 可靠性 | `R` | 0.16 | 最近 7 天该渠道的 `PROVIDER_FAILED` / `UNKNOWN` 率、`429` 率 | 用 S26 观测，不用供应商自报 |
| 预期总成本 | `C` | 0.12 | `ExpectedCost`（§4.2） | 归一化后**取反** |
| 延迟 | `L` | 0.05 | `p90_latency` vs 可用窗口 | |
| 不确定性 | `U` | 0.03 | 样本量、证据新鲜度、报价宽度 | 越高越减分 |

`score = Σ w_i · x_i`，`x_i ∈ [0,1]`（`C` 与 `U` 用 `1 - normalized`）。权重在 `RoutingPolicySnapshot` 里固定并随 `RouteDecision` 记录，保证可复算。

### 4.2 ExpectedCost —— 必须含下游

`docs/09`：`ExpectedCost 包含下游口型、QC 和业务重试`。

```text
ExpectedCost(candidate) =
    generation_cost(candidate)                     # 本期供应商费（用 S24 报价上界，不是均值）
  + lipsync_cost(candidate)                        # 若该模型原生不出对白 → 必做 S32 口型
  + qc_cost(candidate)                             # S35 技术QC + S36 语义QC + S37 对齐
  + expected_retry_cost(candidate)                 # (1 - p_first_pass) × generation_cost × attempts_budget
  + ingest_cost(candidate)                         # S28 归档、转码
```

其中：

- `lipsync_cost`：`audio_strategy='silent_then_tts'` 或视频模型无原生对白时，按该模态 S32 的报价算。**这是低价陷阱的主要来源**：一个 60% 便宜的模型如果不原生出声，口型费可能抵消全部差额。
- `expected_retry_cost`：`p_first_pass` 来自 S44 的按模型分层的首次通过率（不是全局平均）。初值用 0.7 并在样本 < 30 时把 `U` 记高。业务重试上限取自 S39 的策略（`docs/11 §3`：每镜头最多 3 个自动业务 attempt）。
- `qc_cost`：按 `docs/12 §3` 的 QC/VLM 单独计价，不能并入供应商费。

`ExpectedCost` 是**排序信号**，不是准入依据。准入用的是 S24 的 `quote_upper_bound_micro`（上界），两者分开存：

```
RouteDecision.eligible[i].expected_cost_micro   # 排序
RouteDecision.eligible[i].quote_upper_bound_micro  # 给 S26 → S23 预留
```

用上界做预留、用期望做排序，这是"报价留安全余量"（docs/12）的正确落地方式。

### 4.3 归一化与不可比性

不同分量的原始量纲不同（micro、秒、比率）。做法：

- 对同一 `RouteDecision` 内的候选集做 min-max 归一化；
- 候选集只有 1 个时，所有分量记 0.5（避免"唯一候选自动满分"）；
- 归一化区间写进 `RouteDecision.scoring`，便于复算审计。

### 4.4 样本不足的处理

`n < 30` 时该分量的可信度低。规则：

- `Q`、`I`、`M` 用贝叶斯平滑：`(passes + α·prior) / (n + α)`，`α = 20`，`prior` 取同 `modality` 的全局均值；
- `U` 分量记录 `1 - min(n/100, 1)`，样本越少不确定性越高、排序越靠后；
- 输出里带 `sample_size` 与 `watermark`，让下游知道这个分数有多可信（docs/09："评估数据带样本数与水位"）。

---

## 5. 输出：RouteDecision

### 5.1 结构

```jsonc
{
  "route_id": "route_01J...",
  "revision": 1,
  "content_hash": "…",
  "created_at": "2026-10-30T03:12:00Z",
  "quote_valid_to": "2026-10-30T03:27:00Z",      // 15 分钟，过期必须重新准入
  "feasible": true,

  "policy": {
    "policy_ref": {"service_id":"S02","id":"policy_demo","revision":3,"content_hash":"…"},
    "weights": {"Q":0.30,"I":0.22,"M":0.12,"R":0.16,"C":0.12,"L":0.05,"U":0.03},
    "allow_degraded_for_non_critical": false,
    "allow_unverified_combination": false
  },

  "eligible": [
    {
      "rank": 1,
      "candidate_key": "seedance/volcengine/seedance-1-0-pro@250528#cn-beijing#pool_…",
      "snapshot_id": "9f2c…e1",
      "price_snapshot_id": "pr_7a1…",
      "mode": "first_last_frame",
      "combination_key": "6b3d…0f",
      "combination_verdict": "SUPPORTED",
      "normalized_input": {
        "duration_seconds": 5,               // 4s 请求吸附到 5s 档，S26 必须按这个派
        "aspect_ratio": "9:16",
        "resolution": {"width":1080,"height":1920},
        "references": [{"role":"character","count":1},{"role":"first_frame","count":1},{"role":"last_frame","count":1}]
      },
      "score": 0.812,
      "score_parts": {"Q":0.88,"I":0.91,"M":0.74,"R":0.93,"C":0.71,"L":0.80,"U":0.85},
      "sample_size": 412, "watermark": "2026-10-30T03:00:00Z",
      "expected_cost_micro": 1180000,
      "expected_cost_breakdown": {
        "generation": 900000, "lipsync": 120000, "qc": 80000,
        "expected_retry": 60000, "ingest": 20000
      },
      "quote_upper_bound_micro": 1080000,   // 给 S23 预留用
      "currency": "USD",
      "provider_idempotency": "client_lookup_only",
      "execution": {
        "kind": "poll",
        "callback_scheme": "hmac-sha256",
        "cancel_supported": true,
        "url_ttl_seconds": 86400,
        "job_ttl_seconds": 172800
      },
      "estimated_latency_p90_s": 285,
      "deadline_margin_s": 1250,
      "requires_refreshed_reference_urls": false
    }
  ],

  "rejected": [
    {"candidate_key":"kling/api/kling-2-5@…","code":"DURATION_UNSUPPORTED",
     "detail":{"rule":"output.duration_tier","allowed":[5,10],"requested":5,"dialogue_ms":4200}},
    {"candidate_key":"vidu/api/vidu-q3@…","code":"CAPABILITY_MISSING:preserve_identity"},
    {"candidate_key":"veo/official/veo-3-1@…","code":"EVIDENCE_EXPIRED",
     "detail":{"valid_until":"2026-10-22T00:00:00Z"}},
    {"candidate_key":"minimax/api/minimax-video@…","code":"OVER_BUDGET",
     "detail":{"quote_upper_bound_micro":2400000,"budget_remaining_micro":1500000}}
  ],

  "degradation_offer": null,
  "infeasible_reason": null
}
```

### 5.2 `feasible=false` 时

```jsonc
{
  "feasible": false,
  "eligible": [],
  "rejected": [ /* 全部原因 */ ],
  "infeasible_reason": {
    "code": "NO_ELIGIBLE_CANDIDATE",
    "summary": "5 个候选因对白时长被过滤，2 个因能力缺失，1 个因预算不足",
    "dominant_cause": "DIALOGUE_EXCEEDS_DURATION",
    "degradation_options": [ /* 见 §6 */ ]
  }
}
```

**没有可行候选时明确失败，不返回"默认模型"**（docs/09 恢复要求）。S21 收到 `feasible=false` 时按 `degradation_options` 决定是等待、拆镜还是交人审。

### 5.3 `degradation_offer`

当 `feasible=false` 或排名第一的候选明显差时，S25 计算**降级选项**（§6），放进 `degradation_options`：

```jsonc
[
  {"kind":"spec_adjust","option_id":"opt_dur_7s",
   "description":"将本镜拆为 4s + 4s 两段，对白按句切分",
   "requires":["S06 拆镜","S07 变更计划"],
   "cost_delta_micro": 340000, "quality_risk":"low",
   "silent_degradation": false},
  {"kind":"model_swap","option_id":"opt_model_b",
   "description":"换用 X 模型（身份保持略低但原生出声）",
   "cost_delta_micro": -90000, "quality_risk":"medium",
   "silent_degradation": true, "blocked_reason":"身份保持低于节点要求，须人工确认"},
  {"kind":"human_review","option_id":"opt_review",
   "description":"交 S40 决定是否接受较低规格", "silent_degradation": false}
]
```

`silent_degradation=true` 的选项**只能作为提议**，不能自动执行——见 §6。

---

## 6. 降级链

`docs/09 §4` 的允许链：

```
同硬能力健康账户池 → 同硬能力其他模型 → 策略已允许的规格调整/拆镜/分层合成 → 审核或隔离分支
```

S25 只负责**生成候选与排序**，不执行降级。执行由 S21/S26 按链路推进。但 S25 必须精确标注每一档"动了什么"。

### 6.1 四档的判定

| 档 | 动作 | S25 是否可直接给可行结果 | 条件 |
|---|---|---|---|
| L1 | 换同一 `model_snapshot` 的另一个健康账户池 | 是 | `account_pool` 不同，其他六元组要素相同。`combination_key` 不变 |
| L2 | 换 `model_id`/`model_snapshot`（硬能力相同） | 是 | 所有 `required_capabilities` 与参考形状完全一致 |
| L3a | 规格调整（降低分辨率档、吸附到更长时长档） | **否**，需策略显式允许 | `policy.allow_spec_adjust=true` 且 `min_delivery_spec` 仍满足 |
| L3b | 拆镜 / 分层合成 | **否**，必须走 S06+S07 | 属于创作变更，不是执行变更 |
| L4 | 审核 / 隔离分支 | 是（返回 `degradation_options`） | 总是允许 |

### 6.2 绝不能无声降级的项

S25 用一张显式清单判定"这个降级动了能不能动的"：

```jsonc
// RouteDecision.guard
{
  "identity_locked": true,          // 节点要求角色身份保持
  "voice_lock": {"voice_id":"voice_…","revision":4},   // 锁定音色
  "full_dialogue_required": true,   // 对白一个词都不能少
  "permission_constraint": "…",     // 权限/授权约束
  "min_delivery_spec": {"width":1080,"height":1920,"fps":24,"loudness_lufs":-23}
}
```

判定规则：

| 降级动作 | `identity_locked` | `voice_lock` | `full_dialogue` | `min_delivery_spec` |
|---|---|---|---|---|
| L1 换池 | 允许（六元组其余不变） | 允许 | 允许 | 允许 |
| L2 换模型，`I` 分量低于节点阈值 | **禁止自动**，只能提议 | 允许（音色在 TTS 侧） | 允许（若对白长度不变） | 允许 |
| L2 换模型，原生不出声 → 需 TTS | 允许 | **禁止**（音色可能不迁移） | 允许 | 允许 |
| L3a 降分辨率 | 允许 | 允许 | 允许 | **禁止**低于 `min_delivery_spec` |
| L3a 缩时长 | 允许 | 允许 | **禁止**（会删台词） | 允许 |
| L4 人审 | 允许 | 允许 | 允许 | 允许 |

实现上：`CandidateRejection` 与 `degradation_options` 里，凡是触犯上表"禁止"单元格的，`silent_degradation=true` 且 `auto_executable=false`。S21 看到 `auto_executable=false` 必须走 S40 或直接失败，不能自己拍板。

### 6.3 降级链的搜索顺序

`POST /route-decisions` 带 `degradation_level`（默认 0）：

- `level=0`：只做 L1（在候选集内按池区分）。
- `level=1`：加入 L2（同硬能力其他模型，含 `I` 略低的）。
- `level=2`：加入 L3a 规格调整；`degradation_options` 里列出 L3b/L4 供 S21 决策。
- `level=3`：只返回 `degradation_options`，不做自动选。

S21 按 Run 的自动化档位映射 level：`guided/stage_review → 0`，`exception_assisted → 1`，`unattended_batch → 2`。**`unattended` 也不自动走 L3a**，除非 `policy.allow_spec_adjust=true`（`docs/06`："unattended 不意味着允许删对白、换音色或扩大预算"）。

---

## 7. 数据模型

```sql
CREATE TABLE routing_policy_snapshot (
  policy_snapshot_id text PRIMARY KEY,
  tenant_id          text NOT NULL,
  policy_ref_id      text NOT NULL,
  policy_ref_rev     integer NOT NULL,
  policy_ref_hash    char(64) NOT NULL,
  weights            jsonb NOT NULL,
  thresholds         jsonb NOT NULL,   -- min_identity_score, min_quality_score, ...
  flags              jsonb NOT NULL,   -- allow_degraded_for_non_critical, allow_spec_adjust, ...
  effective_from     timestamptz NOT NULL,
  UNIQUE (tenant_id, policy_ref_id, policy_ref_rev, policy_ref_hash)
);

CREATE TABLE route_decision (
  route_id           text PRIMARY KEY,
  revision           integer NOT NULL DEFAULT 1,
  tenant_id          text NOT NULL,
  project_id         text NOT NULL,
  series_id          text NOT NULL,
  episode_id         text NOT NULL,
  branch_id          text NOT NULL,
  node_id            text NOT NULL,
  node_epoch         integer NOT NULL,
  execution_fence    integer NOT NULL,
  attempt_id         text NOT NULL,
  request_hash       char(64) NOT NULL,
  command_id         text NOT NULL,
  policy_snapshot_id text NOT NULL REFERENCES routing_policy_snapshot,
  capability_bundle_revision bigint NOT NULL,   -- 建决策时的 S24 水位
  feasible           boolean NOT NULL,
  requirement_set    jsonb NOT NULL,            -- RequirementSet 规范化
  scoring            jsonb NOT NULL,            -- 归一化区间、样本水位
  quote_valid_to     timestamptz NOT NULL,
  content_hash       char(64) NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, command_id)
);
CREATE INDEX ix_route_lookup ON route_decision (tenant_id, attempt_id, created_at DESC);

CREATE TABLE route_candidate (
  route_id            text NOT NULL REFERENCES route_decision,
  rank                integer,                  -- NULL = rejected
  candidate_key       text NOT NULL,
  snapshot_id         text NOT NULL,
  price_snapshot_id   text,
  combination_key     char(64),
  combination_verdict text,                     -- SUPPORTED|UNVERIFIED
  decision            text NOT NULL,            -- eligible|rejected
  rejection_code      text,
  rejection_detail    jsonb,
  score               numeric(6,4),
  score_parts         jsonb,
  sample_size         integer,
  watermark           timestamptz,
  expected_cost_micro bigint,
  expected_cost_breakdown jsonb,
  quote_upper_bound_micro bigint,
  currency            char(3),
  normalized_input    jsonb,
  execution_profile   jsonb,
  PRIMARY KEY (route_id, candidate_key),
  CHECK (decision IN ('eligible','rejected')),
  CHECK (decision <> 'rejected' OR rejection_code IS NOT NULL)
);

CREATE TABLE degradation_option (
  route_id         text NOT NULL REFERENCES route_decision,
  option_id        text NOT NULL,
  kind             text NOT NULL,   -- pool|model_swap|spec_adjust|split_shot|layered|human_review
  auto_executable  boolean NOT NULL,
  silent_degradation boolean NOT NULL,
  blocked_reason   text,
  cost_delta_micro bigint,
  quality_risk     text,
  payload          jsonb NOT NULL,
  PRIMARY KEY (route_id, option_id),
  CHECK (kind IN ('pool','model_swap','spec_adjust','split_shot','layered','human_review')),
  CHECK (auto_executable = false OR silent_degradation = false)
);
```

`CHECK (auto_executable = false OR silent_degradation = false)` 是硬约束：数据库层面禁止"既要求人工确认又能自动执行"的选项存在。

---

## 8. 幂等与并发

- `tenant_id + command_id + request_hash`：同键同 hash 回放，同键异 hash 409。
- 路由是**纯函数**（给定 `RequirementSet` + `capability_bundle_revision` + `policy_snapshot_id` + 候选快照，输出确定），因此同 `request_hash` 天然幂等；`content_hash` 由输出规范化后求 sha256，可被 S26 引用校验。
- 并发：无共享可变状态，仅在 S24 投影更新时用 `capability_bundle_revision` 单调推进（`WHERE revision < EXCLUDED.revision`）。乱序事件不倒退投影。

---

## 9. 失败分支

| 情况 | 行为 |
|---|---|
| S24 `/capability-validity` 超时 | 503 `CAPABILITY_SOURCE_UNAVAILABLE`，**不用缓存的旧结论**产生可付费决策（一次决策的误判 = 一次白花钱） |
| S24 投影落后（`bundle_revision` 比事件序列小） | 仍可出决策，但 `route_decision.capability_bundle_revision` 记录真实水位，且响应带 `projection_lag=true`；S26 签发 grant 时必须实时复查 |
| S20 模板缺失/不兼容 | 该候选 `COMPILED_PROMPT_INCOMPATIBLE`；若全部候选都失败 → `INFEASIBLE` |
| S02 策略读取失败 | 503，不套用默认策略（默认策略可能放宽了租户的硬约束） |
| S23 只读余额超时 | 无法判断第 16 条硬过滤 → 503 `BUDGET_SOURCE_UNAVAILABLE`。**不能跳过 OVER_BUDGET 过滤** |
| 历史 QC 数据缺失 | `Q` 分量用平滑先验，`U` 记高，`sample_size=0` 明示 |
| `deadline` 已过 | `INFEASIBLE` / `DEADLINE_ALREADY_PASSED`，不发任何可行候选 |
| 报价在决策过程中过期 | 重新取价；取不到则降级为 `PRICE_EXPIRED` 拒绝 |

---

## 10. 可观测

| 指标 | 用途 |
|---|---|
| `route_feasible_ratio{modality}` | 可行性骤降 = 供应商事故 |
| `route_rejection_total{code}` | 原因分布；`OVER_BUDGET` 占比高说明预算规划有问题 |
| `route_rank1_expected_cost_micro`（按 modality） | 成本趋势 |
| `route_score_gap_between_rank1_rank2` | 差距小 = 排名不稳定，值得关注 |
| `route_degradation_offered_total{kind}` | 降级频次 |
| `route_silent_degradation_blocked_total` | 被拦下的无声降级尝试数（> 0 是好的） |
| `route_decision_latency` | SLO |
| `route_projection_lag_seconds` | S24 事件延迟 |

审计：每个 `route_id` 存的拒绝原因、权重、候选集必须能复算。S44/`docs/15` 的经营分析依赖这份可复算性。

---

## 11. 测试清单

| 序号 | 场景 | 断言 | 映射 |
|---|---|---|---|
| T25-01 | 同 `command_id` 同 body | 返回同一 `route_id`，无新行 | E12 |
| T25-02 | 同 `command_id` 异 body | 409 | E12 |
| T25-03 | 候选便宜但缺 `preserve_identity`（无主体参考） | 硬过滤 `CAPABILITY_MISSING:preserve_identity`，不进 `eligible`、不参与评分 | **E19** |
| T25-04 | 合法候选仅一个、价格极高 | 仍出现在 `eligible`（预算够时），不因贵被排除 | 硬约束 |
| T25-05 | 便宜候选预算够、贵候选预算够，便宜的质量极差 | 便宜的仍 `eligible` 但 `rank` 靠后 | 软评分 |
| T25-06 | 便宜候选超预算、贵候选刚好够 | 便宜的被 `OVER_BUDGET` 硬过滤，不因"期望成本低"复活 | 硬约束 |
| T25-07 | 对白 4.2s，候选时长档只有 4s | `DIALOGUE_EXCEEDS_DURATION`，且 `degradation_options` 里"缩时长"标 `auto_executable=false` | E04 |
| T25-08 | 对白时长未知 | 整体 `INFEASIBLE` / `DIALOGUE_DURATION_UNKNOWN`，不估算 | E04 |
| T25-09 | 模型原生不出声 | `expected_cost_breakdown.lipsync > 0`，排序被拉低 | docs/09 |
| T25-10 | `lifecycle=degraded` + 非关键节点 + 策略允许 | 出现在 `eligible` 且 `R` 分受损；`retired` 绝不出现 | E18 |
| T25-11 | 证据过期 | `EVIDENCE_EXPIRED` 拒绝 | E18 |
| T25-12 | 组合 `UNVERIFIED` 且策略不允许 | `COMBINATION_UNVERIFIED` 拒绝 | E19 |
| T25-13 | 组合 `UNVERIFIED` 且策略允许 + `attempt_no=1` | 放行，按最高档位估价 | §4.4 |
| T25-14 | 跨租户 BYOK 候选 | `BYOK_MISMATCH` / `CROSS_TENANT_DENIED`，且不泄漏该池存在 | E28/E30 |
| T25-15 | 数据驻留不匹配 | `RESIDENCY_DENIED`，不因价格低放行 | E31 |
| T25-16 | 排序第一要求降分辨率 | 若低于 `min_delivery_spec` → 不出现在 `eligible`，只进 `degradation_options` | 降级链 |
| T25-17 | 排名第一的候选需换音色才能用 | `silent_degradation=true`、`auto_executable=false` | 降级链 |
| T25-18 | S24 超时 | 503，不返回缓存结论 | 依赖超时 |
| T25-19 | S23 只读超时 | 503，不跳过预算过滤 | 依赖超时 |
| T25-20 | S24 事件乱序 | 投影不倒退（`bundle_revision` 单调） | 乱序事件 |
| T25-21 | 决策可复算 | 用 `route_decision.scoring` + `score_parts` 重算，`score` 一致 | 专项验收 |
| T25-22 | 重启 | 重启前后对同一 `request_hash` 给出一致决策 | 重启恢复 |
| T25-23 | 报价过期 | `quote_valid_to` 过了之后 S26 拒绝使用该决策 | §12 |

---

## 12. 与 S26 的接口约定

S26 收到 `POST /generation-jobs` 时必须校验：

1. `route_decision_ref` 的 `content_hash` 与 S25 存的 `route_decision.content_hash` 一致；
2. `now() <= route_decision.quote_valid_to`，否则 409 `ROUTE_QUOTE_EXPIRED`，要求重新路由；
3. `candidate_key` 在 `route_decision.eligible` 内且 `rank` 匹配；
4. 提交给 S27 的输入规格必须等于 `normalized_input`（含吸附后的时长），不能是原始请求值。

第 4 条是"报价与实派一致"的保证：请求 4 秒、路由吸附到 5 秒并因此报价 5 秒，那么 S26 必须派 5 秒。否则报价失去意义，S23 的预留也不成立。

第 2 条对应 `docs/09` 恢复要求"过期报价重新准入"。

---

## 13. 未决

- **权重初值**。上表是起点，不是结论。需要在有真实 QC 数据后用离线回放校准，且校准过程本身要记录版本（否则"为什么当时这么排"无法回答）。
- **`p_first_pass` 的冷启动**。样本 < 30 时用全局先验会把新模型排后，可能导致新渠道长期无法积累样本。需要一个显式的探索策略（例如按租户可选的小比例 exploitation quota），但这会花钱——属于产品决策，不是实现细节。
- **`OVER_BUDGET` 硬过滤与 S23 只读的时序**。S25 读到的余额可能已过期 1 秒；S26 的准入才是权威。S25 这里只是"明显不行的先筛掉"，不作为承诺。是否需要 S25 完全不做预算过滤（全部交给 S26）？不做的话便宜候选会在明显超预算时仍参与排序，浪费 S21 的时间；做的话要接受一次假阴性。当前选择：做，且 `rejection_detail` 带上读数时间戳，方便事后判断是不是误杀。
