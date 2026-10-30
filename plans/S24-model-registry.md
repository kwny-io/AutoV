# S24 · 模型能力与价格注册 —— 实现方案

状态：`design_only`。本文是**实现方案**，不是已实现代码。所有供应商仍为 `disabled`，本文不声明任何渠道已可用。

上游依据：[docs/09 §1](../docs/09-model-execution.md)、[docs/17](../docs/17-provider-evidence-baseline.md)、[docs/12 §1](../docs/12-budget-scheduling.md)、[docs/04](../docs/04-data-consistency.md)。
链上位置：`S24 注册事实 → S25 算可行路由 → S26 持久化任务与准入 Saga → S27 按渠道执行`。本文只写 S24。S25/S26/S27 的接口面见 `plans/S25-model-routing.md`、`plans/S26-generation-jobs.md`、`plans/S27-provider-connectors.md`。

---

## 1. 边界：本服务写什么、不写什么

### 1.1 本服务是唯一写者

| 事实 | 说明 |
|---|---|
| 能力快照 | 某 `provider/channel/model_id/model_snapshot/region/account_pool` 在某一时刻**按模式**成立的能力组合 |
| 价格快照 | 同一主键的计费单位、档位、失败计费、币种、税、有效期、报价上界 |
| 账户池 | 池的身份、归属（共享/BYOK）、地区、并发与速率上限、健康态 |
| 生命周期 | `disabled → validating → active → degraded → retired` 的当前值与迁移证据 |
| 能力证据 | 每条能力/组合结论的来源、方法、时间、到期时间 |
| 探测与健康 | 只读元数据探测、账户探测、付费探测的登记 |

### 1.2 本服务不写

- **不写余额、不预留预算**（S23）。
- **不写 permit、不记在途槽**（S22）。
- **不写任务状态**（S26）；本服务不知道某个 `provider_job` 是否存在。
- **不写供应商请求回执**（S27）。
- **不做路由决策**（S25）。本服务只回答"这个组合在这个版本上成立吗、多少钱、能不能用"。
- **不接收成片、不判断质量**（S28/S35/S36）。

### 1.3 精确回答三个问题

S24 对外的语义就是三条可判定的询问：

1. `GET /model-candidates` —— 给定模态与硬需求，**哪些候选在这台注册表里是可派的**（不是"最优"，是"合法"）。
2. `POST /capability-checks` —— 给定一个**具体输入形状**（模式 + 参考角色集合 + 音频策略 + 时长档 + 画幅 + 分辨率档），这个版本+地区+池**是否被验证支持**。
3. `GET /price-snapshots/{id}` —— 该候选在该形状下的**可验证报价上界**。

三个问题都必须能返回"否，原因码是什么"，而不是返回空集。

---

## 2. 数据模型

Postgres 权威库，schema `model_registry`。所有表带 `tenant_id` 列（共享目录行 `tenant_id = '*'`），启用 RLS 双重保护。金额一律 `bigint` 微单位（`*_micro`，1 USD = 1e6 micro），禁止 float。

### 2.1 渠道与账户池

```sql
CREATE TABLE provider_channel (
  provider              text NOT NULL,
  channel               text NOT NULL,
  connector_deployment  text NOT NULL,   -- 对应 connectors/<provider>/<channel>/
  egress_identity       text NOT NULL,   -- 独立网络出站身份，一渠道一身份
  credential_ref        text NOT NULL,   -- 密钥管理器引用，不含密钥值
  credential_version    integer NOT NULL,
  lifecycle             text NOT NULL,   -- disabled|validating|active|degraded|retired
  retired_reason        text,            -- 例：'Videos API discontinued 2026-09-24'
  updated_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, channel),
  CHECK (lifecycle IN ('disabled','validating','active','degraded','retired')),
  CHECK (lifecycle <> 'retired' OR retired_reason IS NOT NULL)
);

CREATE TABLE provider_account_pool (
  pool_id             text PRIMARY KEY,
  tenant_id           text NOT NULL,          -- shared 池为 '*'
  provider            text NOT NULL,
  channel             text NOT NULL,
  region              text NOT NULL,
  scope               text NOT NULL,          -- shared | tenant_byok
  -- BYOK：租户自有凭证，只能服务该企业；shared：平台账户，跨租户共享上限
  credential_ref      text,
  credential_version  integer,
  data_region         text NOT NULL,          -- 供应商实际处理地区（驻留判定用）
  max_inflight        integer NOT NULL,
  rpm                 integer NOT NULL,
  tpm                 bigint,
  health              text NOT NULL DEFAULT 'disabled',
  -- healthy|suspect|unauthorized|exhausted|disabled
  health_reason       text,
  health_checked_at   timestamptz,
  enabled             boolean NOT NULL DEFAULT false,
  created_at          timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (provider, channel) REFERENCES provider_channel,
  CHECK (scope IN ('shared','tenant_byok')),
  CHECK ((scope = 'tenant_byok') = (tenant_id <> '*')),
  CHECK (scope = 'shared' OR credential_ref IS NOT NULL),
  CHECK (health IN ('healthy','suspect','unauthorized','exhausted','disabled'))
);
CREATE UNIQUE INDEX ux_pool_natural
  ON provider_account_pool (provider, channel, region, scope, tenant_id);
```

**为什么池是独立表而不是快照上的一个字段**：`docs/09` 的主键含 `account_pool`，但池的并发上限、健康态、凭证版本是**跨模型共享**的事实；把它复制进能力快照会造成"同一个池在 30 个模型快照里各自记一份健康态"，违反单一权威。快照用外键引用池。

**BYOK 硬规则**：`scope='tenant_byok'` 的池只能被 `tenant_id` 相同的路由使用。S25 过滤时按 `tenant_id` 匹配，S26 派单前再复查一次。禁止"借用其他企业凭证容灾"（docs/09 §4）。

### 2.2 能力快照（主键六元组）

```sql
CREATE TABLE model_capability_snapshot (
  snapshot_id            text PRIMARY KEY,       -- = sha256(规范化后的整个 claim 集)
  tenant_id              text NOT NULL DEFAULT '*',
  provider               text NOT NULL,
  channel                text NOT NULL,
  model_id               text NOT NULL,
  model_snapshot         text NOT NULL,          -- 供应商侧版本串，如 'seedance-1-0-pro-250528'
  region                 text NOT NULL,
  account_pool_id        text NOT NULL,
  capability_bundle_rev  bigint NOT NULL,        -- 单调，S25 投影水位
  lifecycle              text NOT NULL,          -- 模型+地区+池粒度的生命周期
  lifecycle_reason       text,
  data_region            text NOT NULL,
  residency_tags         text[] NOT NULL,        -- 例如 {cn-mainland} / {eu} / {global}
  evidence_state         text NOT NULL,          -- documented|probe_verified|field_failed
  evidence_valid_until   timestamptz,            -- 到期即视为未验证
  effective_from         timestamptz NOT NULL,
  effective_to           timestamptz,
  created_at             timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (provider, channel) REFERENCES provider_channel,
  FOREIGN KEY (account_pool_id) REFERENCES provider_account_pool,
  UNIQUE (provider, channel, model_id, model_snapshot, region, account_pool_id, effective_from),
  CHECK (lifecycle IN ('disabled','validating','active','degraded','retired')),
  CHECK (evidence_state IN ('documented','probe_verified','field_failed'))
);
CREATE INDEX ix_snapshot_lookup
  ON model_capability_snapshot (provider, channel, model_id, model_snapshot, region)
  WHERE effective_to IS NULL;
```

`snapshot_id` 内容寻址：相同 claim 集永远得到相同 id，重复投递天然幂等，也便于 S25 在事件里只引用 id。

### 2.3 能力必须按"版本 × 模式"建模

**这是本文档最重要的一条**：不允许用 `supports_first_frame boolean` 这类字段。一个模型在 `image_to_video` 模式下支持首帧，不代表在 `reference_to_video` 模式下也支持；同一 `model_id` 不同 `model_snapshot` 的约束也常常不同（docs/17：Wan3.0 "参考组合约束因版本变化"）。

模式枚举（`capability_mode.mode`）：

```text
text_to_video     image_to_video    first_last_frame   reference_to_video
video_extend      video_edit        lipsync            text_to_image
image_edit        tts               voice_clone        music
sound_effect      llm               vlm                lora_training
```

```sql
CREATE TABLE capability_mode (
  snapshot_id       text NOT NULL REFERENCES model_capability_snapshot,
  mode              text NOT NULL,
  support           text NOT NULL,     -- unsupported|documented|probe_verified|field_failed
  execution_kind    text NOT NULL,     -- sync|poll|callback|hybrid
  prompt_required   boolean NOT NULL,
  prompt_max_chars  integer,
  native_audio      boolean NOT NULL,  -- 该模式原生出声（含对白/环境声）
  prompt_audio_lock boolean NOT NULL,  -- 原生音频是否可与锁定音色绑定
  evidence_id       text,
  PRIMARY KEY (snapshot_id, mode),
  CHECK (support IN ('unsupported','documented','probe_verified','field_failed')),
  CHECK (execution_kind IN ('sync','poll','callback','hybrid'))
);

-- 参考输入规则：按模式、按角色给数量与互斥，不是一串 boolean
CREATE TABLE mode_reference_rule (
  snapshot_id       text NOT NULL,
  mode              text NOT NULL,
  role              text NOT NULL,     -- character|scene|style|first_frame|last_frame
                                       -- video_reference|audio_reference|mask
  min_count         integer NOT NULL DEFAULT 0,
  max_count         integer NOT NULL DEFAULT 0,   -- 0 表示该角色不允许
  ordering          text NOT NULL DEFAULT 'unordered', -- unordered|ordered
  exclusive_with    text[] NOT NULL DEFAULT '{}', -- 不能同时出现的角色
  evidence_id       text,
  PRIMARY KEY (snapshot_id, mode, role),
  CHECK (min_count <= max_count),
  CHECK (ordering IN ('unordered','ordered')),
  FOREIGN KEY (snapshot_id, mode) REFERENCES capability_mode
);

-- 输出规格档位
CREATE TABLE mode_output_spec (
  snapshot_id       text NOT NULL,
  mode              text NOT NULL,
  aspect_ratio      text NOT NULL,     -- '9:16' | '16:9' | '1:1' | 'adaptive'
  max_width         integer,
  max_height        integer,
  duration_tiers_s  integer[] NOT NULL DEFAULT '{}',  -- 允许的时长档，秒
  trims_to_nearest  boolean NOT NULL DEFAULT false,   -- 超出档位是截断还是拒绝
  PRIMARY KEY (snapshot_id, mode, aspect_ratio),
  FOREIGN KEY (snapshot_id, mode) REFERENCES capability_mode
);

-- 能力键（细粒度标签），供 required_capabilities 匹配
CREATE TABLE capability_claim (
  snapshot_id       text NOT NULL,
  mode              text NOT NULL,
  capability_key    text NOT NULL,     -- image_to_video|preserve_identity|multi_subject|
                                       -- camera_control|native_dialogue|...
  support           text NOT NULL,
  params            jsonb NOT NULL DEFAULT '{}',  -- 例如 {"max_subjects":3,"motion_band":"low"}
  evidence_id       text,
  PRIMARY KEY (snapshot_id, mode, capability_key),
  CHECK (support IN ('unsupported','documented','probe_verified','field_failed')),
  FOREIGN KEY (snapshot_id, mode) REFERENCES capability_mode
);

-- 文件输入限制
CREATE TABLE mode_input_limit (
  snapshot_id       text NOT NULL,
  mode              text NOT NULL,
  media_kind        text NOT NULL,     -- image|video|audio
  max_bytes         bigint,
  max_pixels        bigint,
  max_duration_s    numeric,
  allowed_mime      text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (snapshot_id, mode, media_kind),
  FOREIGN KEY (snapshot_id, mode) REFERENCES capability_mode
);
```

### 2.4 组合验证：哈希化的探测结论

布尔字段表达不了"首帧 + 主体参考 + 音频输入三者能否同时成立"。做法是把**具体输入形状**规范化成一个 `combination_key`，对每个已验证过的形状留一条结论行。

```sql
CREATE TABLE mode_combination_probe (
  snapshot_id       text NOT NULL,
  mode              text NOT NULL,
  combination_key   char(64) NOT NULL,   -- 见下方规范化规则
  shape             jsonb NOT NULL,      -- 规范化后的形状，供人读与审计
  result            text NOT NULL,       -- supported|unsupported|inconclusive
  evidence_id       text NOT NULL,       -- 必须指向 capability_evidence
  probed_at         timestamptz NOT NULL,
  valid_until       timestamptz NOT NULL,
  PRIMARY KEY (snapshot_id, mode, combination_key),
  CHECK (result IN ('supported','unsupported','inconclusive')),
  FOREIGN KEY (snapshot_id, mode) REFERENCES capability_mode
);
```

`combination_key` 的规范化（必须跨服务逐字节一致，写进 `contracts/`）：

```json
{
  "mode": "first_last_frame",
  "refs": [{"role":"character","count":1},{"role":"first_frame","count":1},{"role":"last_frame","count":1}],
  "audio_strategy": "silent_then_tts",
  "duration_bucket": "4s",
  "aspect_ratio": "9:16",
  "resolution_bucket": "1080x1920",
  "lora": false
}
```

规则：
- `refs` 按 `role` 字典序，重复角色折叠成 `count`；
- `duration_bucket` = 实际时长**向上取到最近档位**；无档位时按 `ceil(s)` 成桶；
- `resolution_bucket` = `{宽}x{高}`，先按 `mode_output_spec.max_width/max_height` 下采样到允许档；
- `lora`、`audio_reference`、`mask` 参与哈希（它们改变可行性的概率很高）；
- 其余创作参数**不**参与：`combination_key` 是**可行性**键，不是内容键。

**判定语义**（`POST /capability-checks`）：

| 情况 | 返回 | 可付费派单？ |
|---|---|---|
| 有 `supported` 行且 `valid_until > now()` | `SUPPORTED` | 是 |
| 有 `unsupported` 行且未过期 | `UNSUPPORTED` + 原因 | 否 |
| 无行，但所有单维规则（数量、互斥、档位、文件限制）均通过 | `UNVERIFIED` | **否**（需先探测） |
| 有 `inconclusive` 行，或已过期 | `UNVERIFIED` | 否 |
| 任一单维规则不通过 | `UNSUPPORTED` + 具体字段 | 否 |

`UNVERIFIED` 不等于可用。默认禁止派单——因为未验证的组合正是最容易"提交成功但成片不能用"的地方，那笔钱花了也白花。例外通道见 §4.4。

### 2.5 价格快照

```sql
CREATE TABLE price_snapshot (
  price_snapshot_id  text PRIMARY KEY,      -- = sha256(规范化价格行)
  tenant_id          text NOT NULL DEFAULT '*',
  provider           text NOT NULL,
  channel            text NOT NULL,
  model_id           text NOT NULL,
  model_snapshot     text NOT NULL,
  region             text NOT NULL,
  currency           char(3) NOT NULL,
  tax_mode           text NOT NULL,         -- inclusive|exclusive
  billing_units      jsonb NOT NULL,
  -- {"request":120000,"generated_second":45000,"output_megapixel":3000,
  --  "input_token":3,"output_token":9,"character":15,"gpu_second":900}
  min_charge_micro   bigint NOT NULL DEFAULT 0,
  failure_charge     text NOT NULL,         -- none|full|partial|per_unit
  failure_charge_micro bigint NOT NULL DEFAULT 0,
  quotable           boolean NOT NULL,      -- 是否可算出可验证上界
  margin_bps         integer NOT NULL DEFAULT 1500,  -- 报价安全余量，15%
  valid_from         timestamptz NOT NULL,
  valid_to           timestamptz NOT NULL,
  source_ref         text NOT NULL,         -- 官方价格页 URL + 抓取时间
  source_hash        char(64) NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (billing_units ? 'request' OR billing_units ? 'gpu_second'),
  CHECK (tax_mode IN ('inclusive','exclusive')),
  CHECK (failure_charge IN ('none','full','partial','per_unit')),
  CHECK (valid_from < valid_to)
);

CREATE TABLE price_tier (
  price_snapshot_id  text NOT NULL REFERENCES price_snapshot,
  dimension          text NOT NULL,         -- duration_s|resolution_mp|duration_x_resolution
  from_inclusive     numeric NOT NULL,
  to_exclusive       numeric,
  unit_price_micro   bigint NOT NULL,       -- 覆盖 billing_units 对应单品价
  PRIMARY KEY (price_snapshot_id, dimension, from_inclusive)
);
```

`quotable=false` 的场景：供应商按"实际生成秒"计费但拒绝给出上限，或按 GPU 秒计费且时长取决于收敛。这类候选**不能被准入**（docs/12："无可验证上界的任务不能以 0 元估计准入"）。

报价上界公式（S24 计算并随快照返回，S25/S26 只引用不重算）：

```text
raw = Σ_units ( worst_case_units(unit) × unit_price_micro )
    + min_charge_micro
    + failure_charge_reserve(failure_charge, raw)
upper_bound_micro = ceil_to_currency_minor( raw × (1 + margin_bps/10000) )
```

`worst_case_units` 必须由快照自身的档位给出（时长取 `duration_tiers_s` 的最大值，像素取 `max_width×max_height`），不能由调用方传入上限——否则调用方可以压低上界把贵任务塞进预算。

### 2.6 证据

```sql
CREATE TABLE capability_evidence (
  evidence_id      text PRIMARY KEY,
  snapshot_id      text,
  combination_key  char(64),
  method           text NOT NULL,     -- doc_read|account_probe|paid_probe|field_observation
  result           text NOT NULL,     -- supported|unsupported|inconclusive
  source_url       text,
  fetched_at       timestamptz NOT NULL,
  raw_ref          text,              -- 归档后的原文/响应引用，不存全文
  account_pool_id  text,
  reviewer         text,
  valid_until      timestamptz NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (method IN ('doc_read','account_probe','paid_probe','field_observation')),
  CHECK (result IN ('supported','unsupported','inconclusive'))
);

CREATE TABLE lifecycle_transition (
  snapshot_id   text NOT NULL,
  from_state    text NOT NULL,
  to_state      text NOT NULL,
  reason        text NOT NULL,
  evidence_id   text,
  actor         text NOT NULL,       -- 人 或 service principal
  occurred_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_id, occurred_at, to_state)
);
```

`evidence_valid_until` 是**能力证据的保质期**，与 `price_snapshot.valid_to` 是两回事。两者任一过期，候选即从 `GET /model-candidates` 的可用集消失，原因码分别是 `EVIDENCE_EXPIRED` / `PRICE_EXPIRED`。

### 2.7 幂等与 Outbox

```sql
CREATE TABLE command_receipt (
  tenant_id      text NOT NULL,
  command_id     text NOT NULL,
  request_hash   char(64) NOT NULL,
  response_code  integer NOT NULL,
  response_body  jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);
-- 同键同 hash → 回放既有响应；同键不同 hash → 409 IDEMPOTENCY_KEY_REUSED

CREATE TABLE outbox (
  id             bigserial PRIMARY KEY,
  aggregate_id   text NOT NULL,
  aggregate_seq  bigint NOT NULL,
  event_type     text NOT NULL,
  payload        jsonb NOT NULL,
  published_at   timestamptz,
  UNIQUE (aggregate_id, aggregate_seq)
);
```

---

## 3. 契约

### 3.1 `POST /capability-snapshots`（写）

用途：登记一个新的能力快照版本。调用方是模型团队的工具链与探测流水线，不是业务服务。

```jsonc
// 请求
{
  "command_id": "cmd-s24-0001",
  "expected_revision": 41,              // capability_bundle_revision 的 CAS，可选
  "snapshot": {
    "provider": "seedance",
    "channel": "volcengine",
    "model_id": "seedance-1-0-pro",
    "model_snapshot": "250528",
    "region": "cn-beijing",
    "account_pool_id": "pool_seedance_volc_cn_shared",
    "lifecycle": "validating",          // 不接受直接写 active
    "data_region": "cn-mainland",
    "residency_tags": ["cn-mainland"],
    "modes": [
      {
        "mode": "image_to_video",
        "support": "probe_verified",
        "execution_kind": "poll",
        "prompt_required": true,
        "prompt_max_chars": 2000,
        "native_audio": false,
        "prompt_audio_lock": false,
        "reference_rules": [
          {"role": "first_frame", "min_count": 1, "max_count": 1, "ordering": "unordered", "exclusive_with": []},
          {"role": "character",   "min_count": 0, "max_count": 3, "ordering": "unordered", "exclusive_with": []},
          {"role": "audio_reference", "min_count": 0, "max_count": 0}
        ],
        "output_specs": [
          {"aspect_ratio": "9:16", "max_width": 1080, "max_height": 1920,
           "duration_tiers_s": [5, 10], "trims_to_nearest": true}
        ],
        "capability_claims": [
          {"capability_key": "image_to_video", "support": "probe_verified", "params": {}},
          {"capability_key": "preserve_identity", "support": "probe_verified", "params": {"min_refs": 1}},
          {"capability_key": "camera_control",   "support": "documented",   "params": {"max_intensity": 0.6}}
        ],
        "input_limits": [
          {"media_kind": "image", "max_bytes": 10485760, "max_pixels": 4096000, "allowed_mime": ["image/jpeg","image/png"]}
        ]
      }
    ],
    "evidence": [
      {"evidence_id": "ev_seedance_250528_i2v_ff", "method": "account_probe",
       "result": "supported", "source_url": "https://.../create-video-generation-task-api",
       "fetched_at": "2026-10-30T02:00:00Z", "valid_until": "2026-11-29T00:00:00Z",
       "account_pool_id": "pool_seedance_volc_cn_shared"}
    ],
    "combination_probes": [
      {"mode": "image_to_video", "result": "supported",
       "shape": {"mode":"image_to_video",
                 "refs":[{"role":"first_frame","count":1},{"role":"character","count":1}],
                 "audio_strategy":"silent_then_tts","duration_bucket":"5s",
                 "aspect_ratio":"9:16","resolution_bucket":"1080x1920","lora":false},
       "evidence_id": "ev_seedance_250528_i2v_ff",
       "probed_at": "2026-10-30T02:00:00Z", "valid_until": "2026-11-29T00:00:00Z"}
    ]
  },
  "prices": [ /* price_snapshot + tiers，同上 */ ]
}
```

```jsonc
// 响应 201
{
  "snapshot_id": "9f2c...e1",
  "capability_bundle_revision": 42,
  "lifecycle": "validating",
  "accepted_modes": ["image_to_video"],
  "rejected_claims": [
    {"mode":"image_to_video","capability_key":"camera_control",
     "reason":"EVIDENCE_MISSING_FOR_PROBE_VERIFIED"}
  ]
}
```

写入校验（服务端，非 schema 校验）：

1. `lifecycle` 为 `active` 时，必须至少存在一条 `method in (account_probe, paid_probe)` 且 `result=supported` 且 `valid_until > now()` 的证据；否则 422 `ACTIVE_REQUIRES_PROBE_EVIDENCE`。
2. `support='probe_verified'` 的每个 claim 必须有 `evidence_id` 指向真实证据，否则该 claim 降级为 `documented` 并在响应里列出（不静默接受）。
3. `mode_reference_rule` 的 `min_count ≤ max_count`；`exclusive_with` 必须是对称的（写 A 排 B 时自动补 B 排 A）。
4. 新快照把旧快照 `effective_to` 置为新的 `effective_from`；**旧快照不删除**（历史 attempt 要能回溯）。
5. `sora/historical` 渠道：任何提交若 `lifecycle != 'retired'` 一律 422 `HISTORICAL_ENDPOINT_CANNOT_BE_ACTIVATED`。`lifecycle_transition` 不允许从 `retired` 回到任何状态（没有 `retired → *` 的边）。

### 3.2 `GET /model-candidates`（读）

```http
GET /model-candidates?modality=video&home_region=ap-east-1
    &required_capabilities=image_to_video,preserve_identity
    &mode=image_to_video&aspect_ratio=9:16&duration_seconds=4
    &tenant_id=tenant_demo&allow_byok=true
```

```jsonc
// 200
{
  "capability_bundle_revision": 42,
  "projection_watermark": "2026-10-30T03:11:00Z",
  "candidates": [
    {
      "candidate_key": "seedance/volcengine/seedance-1-0-pro@250528#cn-beijing#pool_seedance_volc_cn_shared",
      "snapshot_id": "9f2c...e1",
      "provider": "seedance", "channel": "volcengine",
      "model_id": "seedance-1-0-pro", "model_snapshot": "250528",
      "region": "cn-beijing", "account_pool_id": "pool_seedance_volc_cn_shared",
      "scope": "shared", "data_region": "cn-mainland",
      "residency_tags": ["cn-mainland"],
      "lifecycle": "active",
      "evidence_valid_until": "2026-11-29T00:00:00Z",
      "price_snapshot_id": "pr_7a1...",
      "price_valid_to": "2026-11-15T00:00:00Z",
      "quote_upper_bound_micro": 1080000,
      "currency": "USD",
      "modes": ["image_to_video"],
      "provider_idempotency": "client_lookup_only",
      "execution_kind": "poll",
      "url_ttl_seconds": 86400,
      "job_ttl_seconds": 172800,
      "cancel_supported": true,
      "callback_scheme": "hmac-sha256",
      "max_inflight": 120, "rpm": 600
    }
  ],
  "excluded": [
    {"candidate_key": "veo/official/veo-3-1@2026-10#us#pool_veo_us",
     "code": "EVIDENCE_EXPIRED", "detail": "valid_until 2026-10-22"},
    {"candidate_key": "sora/historical/...@2026-09#us#pool_sora",
     "code": "PROVIDER_RETIRED", "detail": "Videos API discontinued 2026-09-24"},
    {"candidate_key": "kling/api/kling-2-5@...", "code": "RESIDENCY_DENIED"}
  ]
}
```

`excluded` 不是可选项：S25 的"候选拒绝原因可解释"直接依赖它。空 `candidates` + 非空 `excluded` 是合法且有意义的响应。

### 3.3 `POST /capability-checks`（读，但按 command_id 幂等）

```jsonc
// 请求
{
  "command_id": "cmd-s24-ck-0007",
  "snapshot_id": "9f2c...e1",
  "mode": "image_to_video",
  "refs": [{"role":"first_frame","count":1},{"role":"character","count":2}],
  "audio_strategy": "silent_then_tts",
  "duration_seconds": 4,
  "aspect_ratio": "9:16",
  "resolution": {"width": 1080, "height": 1920},
  "lora": false,
  "required_capabilities": ["image_to_video","preserve_identity"]
}
```

```jsonc
// 200
{
  "verdict": "SUPPORTED",              // SUPPORTED | UNSUPPORTED | UNVERIFIED
  "combination_key": "6b3d...0f",
  "normalized_shape": { "...": "..." },
  "checks": [
    {"rule":"reference.max_count","role":"character","limit":3,"actual":2,"ok":true},
    {"rule":"output.duration_tier","allowed":[5,10],"requested":4,"ok":true,"note":"snap_to=5s"},
    {"rule":"combination.probe","probed_at":"2026-10-30T02:00:00Z",
     "valid_until":"2026-11-29T00:00:00Z","ok":true}
  ],
  "reasons": [],
  "evidence_id": "ev_seedance_250528_i2v_ff"
}
```

请求 `duration_seconds=4`、档位是 `[5,10]` 且 `trims_to_nearest=true` 时：返回 `SUPPORTED` 并附 `snap_to=5s`，同时 S25 的估价必须按 **5 秒**计价（不是 4 秒）。`trims_to_nearest=false` 时直接 `UNSUPPORTED` + `OUTPUT_DURATION_UNSUPPORTED`。

### 3.4 `POST /capability-probes`（探测请求）

```jsonc
{
  "command_id": "cmd-s24-probe-0031",
  "snapshot_id": "9f2c...e1",
  "mode": "first_last_frame",
  "shape": { "...": "..." },
  "tool": "account_probe",             // account_probe | paid_probe
  "budget_scope_id": "platform.capability_probe",
  "requested_by": "models-team"
}
```

- `account_probe`：只读/免费调用（列模型、列音色、查询账户配额）。由 S24 自有的 `probe-worker` 用**独立的只读凭证**执行，不经过 S27 业务出站身份。
- `paid_probe`：需要真实计费的验证（例如"这个组合能不能出可用的首尾帧"）。**必须经 S26** 提交一个 `modality=video|image` 的 GenerationCommand，`budget_scope_id=platform.capability_probe`，`purpose=capability_probe`。S24 不直连供应商付费端点。

结果写入 `capability_evidence(method='paid_probe')` 与 `mode_combination_probe`，并发布 `model.capability.changed`。

### 3.5 发给下游的事件

Topic `drama.model-registry.v2`，`event_type = model.capability.changed`，`aggregate_id = {provider}/{channel}/{model_id}/{model_snapshot}`，`aggregate_sequence = capability_bundle_revision`。

```jsonc
{
  "specversion": "1.0", "schema_version": "2.0",
  "id": "evt_01J...", "source": "S24", "type": "model.capability.changed",
  "subject": "seedance/volcengine/seedance-1-0-pro/250528",
  "time": "2026-10-30T03:11:00Z",
  "tenant_id": "*", "home_cell": "cell_a1",
  "aggregate_id": "seedance/volcengine/seedance-1-0-pro/250528",
  "aggregate_sequence": 42,
  "correlation_id": "cmd-s24-0001", "causation_id": "cmd-s24-0001",
  "traceparent": "00-...",
  "data": {
    "snapshot_id": "9f2c...e1",
    "capability_bundle_revision": 42,
    "change_kind": "snapshot_published",   // snapshot_published|lifecycle_changed|
                                           // combination_probe_added|price_published|pool_health_changed
    "lifecycle": "active",
    "effective_from": "2026-10-30T03:11:00Z",
    "changed_modes": ["image_to_video"],
    "evidence_valid_until": "2026-11-29T00:00:00Z"
  }
}
```

S25 只消费本事件维护投影；S26 在派单授权时**实时**回查（见 §5.3）。事件不能代替实时复查。

---

## 4. 关键流程

### 4.1 生命周期迁移

```text
disabled ──(登记快照+只读探测)──▶ validating
validating ──(至少一条 probe_verified 证据且未过期 AND 组合探测通过)──▶ active
active ──(证据过期 / 组合 field_failed / 账户 unauthorized / 人工)──▶ degraded
degraded ──(重新探测通过)──▶ active
active|degraded|validating ──(人工 + 官方生命周期证据)──▶ retired
retired ──▶ (终态，无边)
```

不可跳过的守卫：

| 迁移 | 守卫 | 失败结果 |
|---|---|---|
| `validating → active` | 存在 `method in (account_probe,paid_probe) AND result=supported AND valid_until>now()` 的证据；且每条被使用的能力键都有 `probe_verified` | 422 `ACTIVE_REQUIRES_PROBE_EVIDENCE` |
| `active → degraded` | 任意：证据过期、连续 N 次 `field_observation` 失败、池 401/403 | 自动，附 `lifecycle_reason` |
| `* → retired` | 官方文档证据（`doc_read`，`raw_ref` 归档）| 缺失则 422 `RETIRE_REQUIRES_OFFICIAL_EVIDENCE` |

自动降级只看证据，不看"上次成功过"。`docs/09`：`degraded/retired` 一律剔除候选。

### 4.2 证据过期扫描

每分钟一次（低优先级 worker）：

```sql
UPDATE model_capability_snapshot s
   SET lifecycle = 'degraded', lifecycle_reason = 'EVIDENCE_EXPIRED'
 WHERE s.lifecycle = 'active'
   AND s.evidence_valid_until <= now()
RETURNING ...;
-- 同事务写 lifecycle_transition + outbox
```

一次成功的迁移只发一次事件（`lifecycle_transition` 主键挡住重复）。

### 4.3 探测调度

`probe-worker` 是 S24 内**独立 Deployment**，独立 ServiceAccount、独立只读凭证、独立低配额（每天 N 次，N 由平台预算给出），与业务流量完全隔离。

```
每 15 分钟：对 lifecycle in (validating, active, degraded) 且 evidence_valid_until < now()+7d 的快照排一次探测
每 24 小时：对 active 全量刷新一次账户级探测（列模型、查配额），更新池 health
探测失败连续 3 次 → 池 health='suspect'，发 model.capability.changed(change_kind=pool_health_changed)
401/403 → 立即 health='unauthorized'，S24 通知 S22/S26 停用该池
```

探测**不消耗任何业务预留**：`paid_probe` 用 `budget_scope=platform.capability_probe` 的平台预算，与租户预算完全隔离。

### 4.4 未验证组合的例外通道

只有同时满足以下条件，`UNVERIFIED` 才可派单：

1. 节点在 S02 有效策略里标 `allow_unverified_combination=true`；
2. 该候选的 `lifecycle='active'`（模型本身可信）；
3. S26 的该 `attempt_no ≤ 1`，且该 `combination_key` 在 7 天内失败次数 = 0；
4. 估价按**最高档位**计算，不按请求档位（避免未验证组合的超时只按短的算）；
5. 结果无论成败都回写 `capability_evidence(method=field_observation)`；失败则把该 `combination_key` 记为 `unsupported` 并发布事件。

没有这五条，`UNVERIFIED` 就是 `UNSUPPORTED`。默认关闭。

---

## 5. 失败分支与错误码

### 5.1 错误码

| code | HTTP | 含义 | 调用方该怎么办 |
|---|---|---|---|
| `IDEMPOTENCY_KEY_REUSED` | 409 | 同 `command_id` 不同 `request_hash` | 换 command_id 重新构造，别覆盖 |
| `REVISION_CONFLICT` | 409 | `expected_revision` 不符 | 重读后重试 |
| `ACTIVE_REQUIRES_PROBE_EVIDENCE` | 422 | 想写 active 但无探测证据 | 先探测 |
| `HISTORICAL_ENDPOINT_CANNOT_BE_ACTIVATED` | 422 | 想激活已停用端点 | 不允许，改注册表 |
| `RETIRE_REQUIRES_OFFICIAL_EVIDENCE` | 422 | 缺停用证据 | 补证据 |
| `SNAPSHOT_NOT_FOUND` | 404 | `snapshot_id` 不存在 | 重新拉候选 |
| `PRICE_EXPIRED` | 409 | 报价已过期 | 重新取价并重新准入 |
| `NO_VERIFIABLE_UPPER_BOUND` | 422 | `quotable=false` | 不能准入 |
| `CROSS_TENANT_DENIED` | 403 | BYOK 池与租户不匹配 | 不得重试 |

### 5.2 依赖超时

S24 只在 `POST /capability-snapshots` 与探测时读 S01（授权）。读 S01 超时 → **503 `AUTH_DEPENDENCY_UNAVAILABLE`**，不降级放行。读路径（`/model-candidates`）只用本地库 + S01 的已缓存授权投影；投影缺失时同样 503，不给"看起来全都能用"的结果。

### 5.3 与 S26 的实时复查约定

S24 提供 `GET /capability-validity`（批量、低延迟、只读、无副作用）：

```jsonc
// POST /capability-validity  （批量体，POST 只因为参数长，不是命令，不需要 command_id）
{
  "items": [
    {"snapshot_id":"9f2c...e1","pool_id":"pool_seedance_volc_cn_shared",
     "mode":"image_to_video","combination_key":"6b3d...0f",
     "price_snapshot_id":"pr_7a1..."}
  ]
}
// 200
{"items":[{"snapshot_id":"9f2c...e1","usable":true,
           "lifecycle":"active","pool_health":"healthy",
           "evidence_valid_until":"2026-11-29T00:00:00Z",
           "price_valid_to":"2026-11-15T00:00:00Z","combination":"SUPPORTED"}]}
```

S26 必须在**签发 dispatch grant 前**调用它。这是"审核已通过不等于现在能派"在模型侧的落点：撤销、kill switch、凭证失效、价格过期都可能在路由之后发生。它只回答"现在还能不能用"，不预留任何东西。

---

## 6. 幂等、并发与缓存

### 6.1 幂等

- 所有写命令：`tenant_id + command_id + request_hash`。同键同 hash 返回首次响应（含 `201/200`），同键异 hash 409。
- `snapshot_id`/`price_snapshot_id` 内容寻址，重复登记同一份内容不产生新行。`capability_bundle_revision` 是全局单调计数器，由 `outbox` 的序列驱动，不是时间戳。

### 6.2 并发

- `capability_bundle_revision` 用 `SELECT ... FOR UPDATE` 在 `registry_counter` 单行上取，串行化（QPS 极低，不需要更细的粒度）。
- 快照登记与 `effective_to` 收尾在同一事务。
- 探测结果写入用 `INSERT ... ON CONFLICT (snapshot_id, mode, combination_key) DO UPDATE`，但只在 `probed_at` 更新时覆盖——旧的 probe 不能被乱序到达的旧结果覆盖：

```sql
INSERT INTO mode_combination_probe (...) VALUES (...)
ON CONFLICT (snapshot_id, mode, combination_key) DO UPDATE
  SET result=EXCLUDED.result, evidence_id=EXCLUDED.evidence_id,
      probed_at=EXCLUDED.probed_at, valid_until=EXCLUDED.valid_until, shape=EXCLUDED.shape
WHERE mode_combination_probe.probed_at < EXCLUDED.probed_at;
```

### 6.3 缓存

- 渠道/版本分片缓存（docs/09 的扩容要求）：Redis 缓存**完整能力包**（bundle），key 为 `bundle:{revision}`，内容是改写即失效的公开只读数据。
- 缓存命中不代表可用：`lifecycle`、`evidence_valid_until`、`pool.health` 三个字段有**独立的短 TTL（30s）**，且校验时以库为准。Redis 清空 → 回源，不影响正确性，只影响延迟。
- **绝不**把"缓存里没有 = 不允许"用作拒绝依据；也没有相反方向。

---

## 7. 可观测

指标：

| 指标 | 用途 |
|---|---|
| `registry_candidates_found{candidate_set}` / `registry_candidates_excluded{code}` | 候选集健康度；`excluded` 突然占满说明大面积证据过期 |
| `registry_evidence_days_to_expiry` (histogram) | 提前发现"下周全部候选失效" |
| `registry_probe_total{method,result}` | 探测成功率 |
| `registry_probe_budget_used_micro` | 探测费用不越界 |
| `registry_lifecycle{state}` | `active` 数量骤降 = 事故 |
| `registry_price_age_seconds{channel}` | 价格陈旧度 |

告警：`active` 候选数 < 2（单一供应商故障即无路可走）、`evidence_days_to_expiry < 3` 的占比 > 20%、探测连续失败、`pool_health='unauthorized'`。

审计：`lifecycle_transition` 全量进 S45；`price_snapshot` 与 `capability_snapshot` 不可变，证据原文按 `raw_ref` 归档。

---

## 8. 测试清单

| 序号 | 场景 | 断言 | 映射 |
|---|---|---|---|
| T24-01 | 同一 `command_id` 同 body 重投 | 返回首次响应，不产生新快照、不递增 revision | E12 |
| T24-02 | 同一 `command_id` 不同 body | 409 `IDEMPOTENCY_KEY_REUSED`，原快照不变 | E12 |
| T24-03 | 想直接写 `lifecycle=active` 且无探测证据 | 422，`lifecycle_transition` 无记录 | 专项验收 |
| T24-04 | `sora/historical` 试图置 active | 422 `HISTORICAL_ENDPOINT_CANNOT_BE_ACTIVATED` | E18 / docs/17 |
| T24-05 | 证据 `valid_until` 到期 1 秒 | 候选从 `/model-candidates` 消失，`excluded.code=EVIDENCE_EXPIRED`，发一次事件 | 专项验收 |
| T24-06 | 同一 `model_id` 两个 `model_snapshot`，约束不同 | 两个候选分别按自己的规则过滤，不串 | 专项验收 |
| T24-07 | `image_to_video` 支持首帧、`reference_to_video` 不支持 | 同一快照内按 mode 分别判定，不因一个 boolean 串味 | 专项验收 |
| T24-08 | 首帧 + 主体 + 音频三者并存且无组合探测行 | `POST /capability-checks` 返回 `UNVERIFIED`，不可派单 | E19 |
| T24-09 | 组合探测行过期 | 返回 `UNVERIFIED`，不是 `SUPPORTED` | 专项验收 |
| T24-10 | 请求 4 秒、档位 5 秒、`trims_to_nearest=true` | 返回 `SUPPORTED` 且 `snap_to=5s`；报价按 5 秒算 | E04 相关 |
| T24-11 | `quotable=false` 的候选 | `/model-candidates` 不返回，或返回且标 `NO_VERIFIABLE_UPPER_BOUND` 并在 S25 被硬过滤 | docs/12 |
| T24-12 | 价格 `valid_to` 过期 | 原因码 `PRICE_EXPIRED` | 专项验收 |
| T24-13 | BYOK 池 + 其他租户查询 | 不出现在该租户候选集；直接 `POST /capability-checks` 返回 403 | E28 / E30 |
| T24-14 | Redis 清空 | 全部候选仍能从库重建，结果与清空前一致 | E21 |
| T24-15 | 乱序到达的旧 probe 结果 | 不覆盖较新的 probe（`WHERE probed_at <`） | 乱序事件 |
| T24-16 | S01 超时 | 写命令 503，不静默放行 | 依赖超时 |
| T24-17 | 重启恢复 | 从 outbox + 快照表重建 `capability_bundle_revision`，事件序不断档 | 重启恢复 |
| T24-18 | `GET /capability-validity` 在证据过期 30 秒后 | `usable=false`，S26 据此拒绝签发 grant | E18 |
| T24-19 | 探测费用 | `platform.capability_probe` 预算内的支出，不出现在任何租户预算里 | docs/12 §1 |
| T24-20 | 静态校验通过 ≠ 行为正确 | 集成测试用真实库跑 T24-03/05/15，不靠 schema 校验 | docs/16 §6 |

---

## 9. 实施顺序与未决

实现顺序（W1 内）：

1. `provider_channel` / `provider_account_pool` / `model_capability_snapshot` + 只读查询（先给 S25 一个可用的假数据源）。
2. `capability_checks` 判定引擎 + `combination_key` 规范化（把规范化函数抽到 `platform/` 共享库，S25 与 S26 必须用同一实现）。
3. `price_snapshot` + 上界计算。
4. 探测 worker（只读）。
5. `capability-validity` 批量接口。
6. 付费探测接 S26（在 S26 可用之后）。

未决，需在实施会话里定：

- `evidence_valid_until` 的默认时长。按渠道分别设，不设全局默认值——不同供应商的接口稳定性差一个数量级。建议首次实施时全部为 30 天并强制人工复核。
- 七家渠道的 `provider_idempotency` 分类（`supported` / `client_lookup_only` / `unsupported`）必须**逐家**用账户探测确认，不能从文档推断。这个分类直接决定 S26 的未知提交流程，填错的代价是重复扣费。`docs/17` 只给了入口文档，没有结论。
- `sora/historical` 是否保留连接器目录。设计上保留（`docs/09` 称"历史占位"），但 `lifecycle` 永远是 `retired`，路由永远不会选它。
