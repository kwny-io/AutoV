# S44 · 效果评测与经营分析 —— 实现方案

状态：实现方案（W4）。`maturity: design_only` 不变。统计口径与 KPI 定义来自 [docs/15 §3](../docs/15-fde-business-evaluation.md)，**不是本服务发明**。

上游依据：[docs/15 §3/§4](../docs/15-fde-business-evaluation.md)、[docs/12 §2/§3](../docs/12-budget-scheduling.md)、[docs/04 §5](../docs/04-data-consistency.md)、[S44 规格](../docs/design-package/services/S44-evaluation-analytics.md)、[docs/16 E38](../docs/16-migration-acceptance.md)。

配套：[S23 预算账本](S23-budget-ledger.md)（**金额唯一权威**）、[S26 生成任务](S26-generation-jobs.md)、[S35–S38 质量](S35-technical-qc.md)、[S40 人工评审](S40-human-review.md)、[S25 模型路由](S25-model-routing.md)。

---

## 0. 一句话边界

S44 是**读模型 + 评测运行器**，不是账本。

```
S23: 原始费用账本（唯一权威，不可变）
S44: 读 S23 的费用事实 + 读 S35–S40 的质量事实 + 读 S26 的任务事实
     → 按固定的 allocation policy 做经营投影
     → 跑评测（benchmark / 盲评 / 实验）
     → 给出带样本量、时间范围、版本的统计
```

**三条硬边界：**

1. **S44 不拥有原始账本。** 任何金额的"原始值"来自 S23；S44 只做分摊与聚合。S44 的库中可以存**分摊结果**，但必须可追溯到 S23 的原始条目，且不可反过来影响 S23。
2. **S44 不改写任何生产事实。** 不给 QC 打分、不改任务状态、不改接受指针、不动预算。
3. **S44 的统计不允许"只报成功样本均值"。** 每个指标必须带 `n`、时间窗、版本、分位数、置信区间、失败样本计数。

判据：如果一个数字会影响"要不要继续这个客户/这个供应商/这个模型"，它是 S44 的输出；如果它会**扣钱或放行**，它是 S23/S02/S08 的输出。

---

## 1. 责任与明确不负责

**唯一写者：**

- `MetricDefinition` —— 指标定义（公式、分子分母口径、样本单位、排除规则）
- `MetricSnapshot` —— 某个 cohort/time window 下某指标的计算结果（含 n、分位数、CI）
- `Cohort` —— 分析用的样本集合定义（项目/系列/租户/时间窗/自动化档位/模型版本）
- `AllocationPolicy` —— 分摊规则（**固定**、版本化；改规则 = 新版本 = 新快照）
- `CostAllocation` —— 某条 S23 费用条目在某维度上的分摊结果
- `BenchmarkDataset` / `BenchmarkItem` —— 评测集与条目（引用真实产物 + 标注）
- `BlindReview` / `BlindReviewVote` —— 盲评会话与投票
- `Experiment` / `ExperimentArm` / `ExperimentAssignment` —— 实验设计与分组
- `EvaluationRun` —— 一次评测执行
- `GenerationCostProjection` —— 生成成本的时间投影（按日/按集/按模型）
- `CustomerProjectROI` —— 客户项目的 ROI 投影

**明确不写：**

| 事实 | 权威 | S44 的正确做法 |
|---|---|---|
| 原始费用条目 / 余额 / 预留 | **S23** | 只读 `cost-breakdown` 与费用事件 |
| 任务状态 / 尝试 / provider 用量 | **S26** | 只读 |
| QC 分数 / 缺陷 | **S35–S38** | 只读；不重算、不覆盖 |
| 人工决定 / 评审结论 | **S40** | 只读 |
| 模型路由选择 | **S25** | 只读（S25 反向消费 S44 的 per-model 首轮可用率，见 §6.4） |
| 接受指针 / manifest | **S08** | 只读 |
| 资产/音色授权 | **S15** | 只读 |
| 客户合同金额 | 未定（§11 C1） | 只引用 `contract_ref`；不解释条款 |
| 审计链 | **S45** | 只产生审计意图 |

**S44 不发起付费调用。** 评测若需要生成新样本（如"用新 prompt 生成 50 条对照"），必须**走 S21 编排 → S26 准入**，由 S23 预留预算、S22 许可。S44 只发起"评测请求"，费用由被评测的生成承担。

---

## 2. 实体与关键区分

### 2.1 原始费用 vs 分摊 vs 投影（三者绝不混）

```
S23 原始条目：  attempt_id, model, provider, 请求时间, 金额(micro), 币种, 计费口径
                     │
                     │  S44 按 AllocationPolicy v3 分摊
                     ▼
S44 分摊：       同一笔金额，按规则拆到 (项目, 集, 场景, 角色, 模型)
                     │
                     │  按维度聚合
                     ▼
S44 投影：       "第 3 集每可用分钟成本 = 4.21 元, n=12 个可用分钟,
                  时间窗 2026-10-01..2026-10-31, 版本 v7"
```

三条不可违反的规则：

1. **一笔原始金额只能被分摊一次到某个维度**。分摊不是"再记一笔"，是"同一笔的不同视角"。所以 `sum(allocations of cost_entry) = original_amount`（在分摊精度内），且 S44 的 `cost_allocation` 有 `cost_entry_id` 外键指回 S23 的条目 id（**不是跨库外键**，是逻辑引用 + 校验）。
2. **分摊规则变了，旧快照不重算**。`AllocationPolicy` 版本化；`MetricSnapshot` 记录 `allocation_policy_version`。客户问"为什么 10 月和 11 月的成本口径不同"时，答案是"分摊规则从 v2 升到 v3"，而不是偷偷重算历史。
3. **分摊不能凭空造出费用**。若 S23 只有总额没有明细（例如打包计费的 provider），S44 必须标 `allocation_basis: "estimated_by_policy"` 并在快照里单独计数。**估算分摊的金额不得与精确分摊的金额混在一个 KPI 里**——那会让"每可用分钟成本"看起来有小数点后两位的精度，实际是拍的。

**S44 的库里有金额，但它不是账本。** 判据：删掉 S44 的全部数据，S23 的账本仍然完整、仍然能出账单。反过来删掉 S23，S44 的金额全部失去依据。这就是"唯一权威"的实际含义。

### 2.2 `cohort` 是一等公民

docs/15 §3 要求"统计显示样本量、时间范围、版本"。`cohort` 是这三件事的结构化表达：

```jsonc
{
  "cohort_id": "coh_...",
  "tenant_id": "t_...",
  "definition": {
    "project_ids": ["proj_a"],
    "series_ids": null,
    "episode_ids": null,
    "time_window": {"from":"2026-10-01T00:00:00Z","to":"2026-11-01T00:00:00Z"},
    "automation_mode": ["stage_review","exception_assisted"],
    "acceptance_mode": ["human_accept"],
    "model_revisions": [{"model_id":"m_x","revision":4}],
    "prompt_revisions": [{"prompt_id":"p_y","revision":9}],
    "policy_versions": [{"policy_ref":"...","revision":3}],
    "include_failed_runs": true,
    "include_abandoned": false
  },
  "frozen_at": "2026-11-01T00:00:00Z",
  "member_count": 1842
}
```

规则：

- **`cohort` 是可复现的**。给定 `definition` 与 `frozen_at`，任何人重跑必须得到同样的成员集合（在源数据未变的前提下）。因此 cohort 必须记录**源数据的 watermark**（S26/S23/S35 各自的投影水位）。
- **跨版本混算必须显式**。`model_revisions` 为空表示"不限"，此时快照必须标 `mixed_versions: true`，且**禁止**用它做"某个模型更好"的结论。
- **`include_failed_runs` 默认 true**。docs/15 的硬要求："不能只报成功样本均值"。分母必须包含失败。如果业务上要看"成功样本的表现"，那是另一个指标，必须单列并标 `denominator: "successful_only"`。

### 2.3 七个 KPI：定义、分母、陷阱

docs/15 §3 给出 7 个 KPI。实现上必须把每个的**分母**写死，否则不同人算出不同数。

| # | KPI | 分子 | 分母 | 最大的陷阱 |
|---|---|---|---|---|
| K1 | 成片可用率 | 被接受的 master 数 | 进入 `formal` 渲染的集数 | 把"渲染成功"当"可用"；把未进入渲染的集排除出分母 |
| K2 | 首轮镜头可用率 | 首次 QC 通过且无需返工的镜头数 | 首次进入 QC 的镜头数 | 分子分母跨了不同 revision；重复计入返工后的通过 |
| K3 | 每可用分钟成本 | S23 原始费用（按分摊） | **可用**成片分钟数 | 分母用了全部成片分钟而非可用分钟；把失败集成本摊到了成功集 |
| K4 | 单集交付耗时 | `delivery.package.published` 时间 | `episode` 首个 run 创建时间 | 用了"最后一次修改"而非"发布"；把暂停时间算成生产时间 |
| K5 | 返工率 | 发生 ≥1 次返工决策的镜头数 | 全部镜头数 | 返工定义不清（QC 重检算不算？）；按次计而非按镜头计 |
| K6 | 人工干预率 | 有人工决策（S40）的节点数 | 全部节点数 | 把"自动通过"里的人工抽查算成干预，或反之 |
| K7 | 记忆正确性 | 记忆事实被后续剧情证伪前未被发现的条数 | 被引用的记忆事实总数 | 分母只算"被检查过的"，漏掉没检查的 |

补充 **K8 企业毛利/ROI**（docs/15 §3 列为同一组）：

| # | KPI | 分子 | 分母 | 陷阱 |
|---|---|---|---|---|
| K8 | 毛利/ROI | 合同收入 − 分摊成本 | 合同收入 | 合同收入从哪来（§11 C1）；把平台费/人力算不算进去未定 |

**每个 KPI 的"可用"必须有操作定义。** K1 的"可用"= 被 accepted（S08）；K2 的"可用镜头"= QC `pass` 或 `abstain` 经人工判 `pass`（**abstain 不是 pass**，E25）。这个定义要进 `MetricDefinition`，不能留在文档里。

### 2.4 分位数与置信区间：不允许只给均值

`MetricSnapshot` 必须包含：

```jsonc
{
  "metric": "cost_per_usable_minute",
  "value": 4.21,
  "unit": "CNY/min",
  "n": 12,
  "denominator_unit": "usable_minutes",
  "distribution": {"p50": 3.90, "p90": 5.60, "p95": 6.30, "p99": 8.10, "max": 9.40},
  "ci95": {"low": 3.42, "high": 5.18},
  "method": "bootstrap_percentile",
  "time_window": {...},
  "cohort_id": "...",
  "version_set": {"model":[{"id":"m_x","revision":4}], "prompt":[...], "policy":[...]},
  "excluded": {"failed_runs": 3, "abandoned": 1, "insufficient_data": 0},
  "mixed_versions": false,
  "watermarks": {"s23": 10231, "s26": 8821, "s35": 4410, "s40": 2201},
  "computed_at": "..."
}
```

规则：

- **`n` 是样本单位数，不是记录数**。K3 的 n 是"可用分钟数"；K2 的 n 是"镜头数"。用错了会让 CI 假性收窄。
- **CI 方法必须写出来**。bootstrap percentile 与 t-interval 在小样本下差异巨大。n < 30 时**必须**用 bootstrap，且标 `low_confidence: true`。
- **`excluded` 必须非空字段（即使全 0）**。这是防止"悄悄过滤掉不利样本"的结构性保障。
- **无数据不是 0**。n=0 时 `value: null`，不是 `0`。把"没有数据"显示成 0 会让看板给出完全错误的判断（"这个月成本为零，太好了"）。

### 2.5 分位数用什么算

- **成本/耗时**：按"每集"或"每镜头"分组后取分位数，不是对原始条目取。对原始条目取 p95 会得到"单次调用的 p95"，不是"每集成本的 p95"，两者差一个数量级。
- **可用率类**：比率型指标给 Wilson 区间，不是正态近似（小样本比率用正态近似会给出 [−0.02, 1.02] 这种荒谬结果）。
- **分布跨度大时**（成本必然如此），同时给 p50/p90/p95 与 max，不要只给 p50。

---

## 3. 状态机

### 3.1 `EvaluationRun`

```
CREATED → QUEUED → RUNNING → SUCCEEDED
                        │
                        ├── 部分条目失败 ──► PARTIAL（附 成功/失败 条数）
                        ├── 全部失败 ──────► FAILED
                        └── 取消 ──────────► CANCELLED
```

- `PARTIAL` 是**必须存在的状态**。评测跑 100 条，97 条成功、3 条超时——如果标 `SUCCEEDED`，下游会把 97 条当成完整结果；如果标 `FAILED`，97 条的有效结果白丢。
- `PARTIAL` 的结果**可用于分析**，但快照必须带 `excluded.failed_items`。

### 3.2 `MetricSnapshot`

```
（无状态机）
```

快照是**不可变计算结果**。重算 = 新快照（同 `cohort` + 新 `watermark`）。不修改旧快照。

理由与 S44 的定位一致：如果快照可变，"上个月的报告"就变成了"这个月重算出来的上个月"，客户对账时无法解释。

### 3.3 `Experiment`

```
DRAFT → RUNNING → STOPPED | COMPLETED
```

- 实验是**执行层**的东西（A/B、prompt 对照），不是评估层。S44 拥有实验的**分配与结果读取**，不拥有被实验的生成。
- `STOPPED` 用于止损（成本超限、发现明显劣化）。stopped 的实验保留全部数据。
- **实验不得绕过准入**：实验臂的每次生成仍走 S21→S26→S23/S22。S44 不能"为了对照多跑 100 次"而自己造付费任务。

---

## 4. 关键流程

### 4.1 指标计算

```
POST /metric-snapshots
  1. 解析 metric_definition（版本化）
  2. 冻结 cohort（写 member 集合 + 源 watermark）
  3. 拉取源数据（只读）：
       S23 cost-breakdown（按 cohort 的 episode/attempt）
       S26 任务与尝试（含失败、取消、未知）
       S35–S38 QC 报告（含 abstain）
       S40 人工决定
       S08 accepted pointer（判断"可用"）
       S43 交付记录（判断"已交付"）
  4. 校验 watermark 一致性：
       若任一源落后于 cohort.frozen_at → 标记 stale_sources，可选择等待或标 partial
  5. 计算分子/分母/分布/CI
  6. 写 snapshot（不可变）
  7. 发 analytics.metric.snapshot.created
```

第 4 步是最容易被忽略的：**跨源统计必须检查各源的水位**。S26 的事件可能比 S23 晚到 2 分钟，如果不等齐，"今天的成本"会显示偏低——然后用它做决策就错了。

**一致性策略**：不追求强一致（那会让指标永远出不来）。做法是：

- 快照记录每个源的 `watermark`。
- 查询响应给 `staleness: {"s23": "12s", "s26": "3s", ...}`。
- 工作台（S47）展示时必须显示"数据截至 XX:XX"，不能假装是实时的。

### 4.2 分摊

```
for each cost_entry in S23.cost_breakdown(cohort):
    targets = allocation_policy.allocate(cost_entry)
    # 典型规则：
    #   - 直接归属：attempt 已知 episode/shot → 100% 归集
    #   - 共享成本：重试/失败的成本按 policy 摊到同一集
    #   - 平台成本：按可用分钟数比例摊到各集
    assert sum(targets.amount) == cost_entry.amount  (整数 micro 单位)
    写 cost_allocation(cost_entry_id, target_dimension, amount_micro, basis)
```

整数一致性是硬要求：**用整数 micro 单位，且最后一项吸收舍入余数**。浮点分摊会在 10000 笔后产生分币级漂移，客户对账时被问住。

分摊规则示例（`AllocationPolicy`）：

```jsonc
{
  "policy_version": "alloc-3",
  "rules": [
    {"match":{"cost_type":"generation","shot_id":"*"},
     "basis":"direct","dimension":"shot_id"},
    {"match":{"cost_type":"generation","shot_id":null},
     "basis":"by_episode_share","dimension":"episode_id","weight":"attempt_count"},
    {"match":{"cost_type":"retry"},
     "basis":"to_parent_episode","dimension":"episode_id"},
    {"match":{"cost_type":"platform_fixed"},
     "basis":"by_usable_minutes","dimension":"episode_id"},
    {"match":{"cost_type":"evaluation"},
     "basis":"excluded_from_production","dimension":"tenant_id"}
  ]
}
```

**`evaluation` 类成本必须排除在生产成本之外**，否则评测跑得越多，K3 越难看。这是真实会发生的口径污染。

### 4.3 盲评

盲评是 K2/K7 等"需要有质量判断"的指标的数据来源之一（用于校准自动 QC 与人工判断的差异）。

```
POST /blind-reviews
  {cohort_id, item_count, vote_dimensions:["visual","audio","lipsync","story"], reviewers:[...]}

  → 系统随机抽样 BenchmarkItem
  → 隐藏：模型、prompt、版本、是否返工过
  → 评审者投票
  → 汇总：agreement (Krippendorff's alpha), 分维度均值/分布
```

硬规则：

1. **盲评必须真盲**。响应里不含 `model_id`、`prompt_revision`、`attempt_no`、`is_retry`。否则评审者会被"这是重生成的第 3 次"影响。
2. **评审者之间要有一致性度量**。没有 `alpha` 的盲评结果不可用——可能只是一个人随手打分。
3. **样本量太少不能出结论**。n < 5 的维度标 `insufficient_data`，不给均值（给均值就是误导）。
4. **盲评结果不能反向改 QC**。S44 可以说"自动 QC 与人工判断的一致率为 0.62"，但不能说"所以这个镜头应判 pass"。

### 4.4 实验

```
Experiment:
  arms: [{name:"baseline", config_ref:{prompt_revision:9}},
         {name:"treatment", config_ref:{prompt_revision:12}}]
  assignment: "hash(episode_id + salt) % 100 < 50"
```

规则：

- **分配必须确定性**（同 episode 永远同臂），否则跨集比较会混入 episode 差异。
- **实验数据不得污染生产 KPI**。`MetricSnapshot` 可指定 `exclude_experiment_arms: [...]`，或按 `experiment_id` 单独出快照。
- **提前终止需要多重比较校正**。看 10 个指标，总有一个"显著"。文档不给做法，本方案要求 `Experiment` 记录 `planned_metrics`，非计划指标的结果标 `exploratory=true`。

### 4.5 成本时间投影

`GenerationCostProjection` 不是预测模型，是**"按当前速率外推"**的保守估计：

```
projected_month_cost =
    actual_to_date
  + remaining_days * p50_daily_rate
  ± CI
```

规则：

- 必须给区间，不给单点。
- 必须标明基于哪个窗口的速率（近 7 天 / 近 30 天），两者可能差 3 倍。
- **投影不用于放行**。S23/S22 的预算校验用实际值，不用 S44 的投影。S44 的投影是给经营看板和人看的。

### 4.6 失败矩阵

| 失败 | 行为 |
|---|---|
| 源服务 503 | 快照 `PARTIAL`，标 `stale_sources`；不给出"看起来完整"的数字 |
| watermark 落后于 frozen_at | 可选等待（最长 60s）或标 `partial` |
| cohort 成员为空 | `value: null`，`n: 0`，不返回 0 |
| 分摊规则不覆盖某类成本 | `unallocated_micro` 非零 + 告警；**不静默丢弃** |
| 分摊舍入不一致 | 拒绝生成快照，报 `ALLOCATION_INVARIANT_VIOLATION` |
| 评测条目全部失败 | `FAILED`，不产出快照 |
| 评测部分失败 | `PARTIAL`，快照带 `excluded.failed_items` |
| 盲评 α 过低 | 结果标 `low_agreement`，不产出 KPI 校准结论 |
| 指标定义版本变化 | 新快照用新版本；旧快照保留但标 `superseded_definition` |
| 同一 cohort 并发重算 | 允许；结果应相同（除非源变了），不同则告警 `nondeterministic_snapshot` |

最后一条是重要的自检：**同 cohort 同 watermark 重算必须得到同结果**。不同说明有非确定性（比如用了当前时间做窗口）。这是 bug，不是"统计本来就有波动"。

---

## 5. 数据模型

数据库 `evaluation_analytics`，Postgres。金额一律 `bigint` micro 单位（与 S23 一致）；比率用 `numeric`。

```sql
CREATE TABLE analytics.metric_definition (
  metric_key           text NOT NULL,           -- 'usable_rate','cost_per_usable_minute',...
  version              integer NOT NULL,
  unit                 text NOT NULL,
  numerator_spec       jsonb NOT NULL,          -- 结构化，不是 SQL 字符串
  denominator_spec     jsonb NOT NULL,
  sample_unit          text NOT NULL,           -- 'episode'|'shot'|'minute'|'node'
  exclusion_rules      jsonb NOT NULL DEFAULT '[]'::jsonb,
  ci_method            text NOT NULL,           -- 'wilson'|'bootstrap_percentile'|'none'
  min_n                integer NOT NULL DEFAULT 1,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (metric_key, version)
);

CREATE TABLE analytics.cohort (
  tenant_id            text NOT NULL,
  cohort_id            text NOT NULL,
  definition           jsonb NOT NULL,
  frozen_at            timestamptz NOT NULL,
  member_count         integer NOT NULL,
  source_watermarks    jsonb NOT NULL,          -- {s23:.., s26:.., s35:.., s40:.., s08:.., s43:..}
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, cohort_id)
);
CREATE INDEX cohort_project_idx ON analytics.cohort
  USING gin ((definition -> 'project_ids'));

CREATE TABLE analytics.metric_snapshot (
  tenant_id            text NOT NULL,
  snapshot_id          text NOT NULL,
  metric_key           text NOT NULL,
  metric_version       integer NOT NULL,
  cohort_id            text NOT NULL,
  value                numeric,
  unit                 text NOT NULL,
  n                    integer NOT NULL,
  distribution         jsonb NOT NULL,          -- {p50,p90,p95,p99,max}
  ci95                 jsonb,
  excluded             jsonb NOT NULL,          -- {failed_runs,failed_items,abandoned,...}
  mixed_versions       boolean NOT NULL,
  stale_sources        jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_watermarks    jsonb NOT NULL,
  allocation_policy_version text,
  low_confidence       boolean NOT NULL DEFAULT false,
  computed_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, snapshot_id),
  CONSTRAINT snapshot_n_nonneg CHECK (n >= 0),
  CONSTRAINT snapshot_value_null_iff_zero_n
    CHECK ((n = 0 AND value IS NULL) OR (n > 0))
);
CREATE INDEX snapshot_lookup_idx ON analytics.metric_snapshot
  (tenant_id, metric_key, cohort_id, computed_at DESC);

CREATE TABLE analytics.allocation_policy (
  tenant_id            text NOT NULL,
  policy_version       text NOT NULL,
  rules                jsonb NOT NULL,
  effective_from       timestamptz NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, policy_version)
);

CREATE TABLE analytics.cost_allocation (
  tenant_id            text NOT NULL,
  cost_entry_id        text NOT NULL,           -- S23 的逻辑引用
  policy_version       text NOT NULL,
  dimension            text NOT NULL CHECK (dimension IN
                       ('project','series','episode','scene','shot','role','model','tenant')),
  dimension_value      text NOT NULL,
  amount_micro         bigint NOT NULL,
  basis                text NOT NULL CHECK (basis IN
                       ('direct','by_episode_share','to_parent_episode',
                        'by_usable_minutes','estimated_by_policy','excluded')),
  PRIMARY KEY (tenant_id, cost_entry_id, policy_version, dimension, dimension_value)
);
CREATE INDEX cost_alloc_episode_idx ON analytics.cost_allocation
  (tenant_id, dimension, dimension_value);

CREATE TABLE analytics.benchmark_dataset (
  tenant_id            text NOT NULL,
  dataset_id           text NOT NULL,
  name                 text NOT NULL,
  version              integer NOT NULL,
  purpose              text NOT NULL,           -- 'qc_calibration'|'model_selection'|'regression'
  item_count           integer NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, dataset_id, version)
);

CREATE TABLE analytics.benchmark_item (
  tenant_id            text NOT NULL,
  dataset_id           text NOT NULL,
  dataset_version      integer NOT NULL,
  item_id              text NOT NULL,
  artifact_refs        jsonb NOT NULL,          -- S28 refs（真实产物）
  ground_truth         jsonb NOT NULL,          -- 标注（缺陷类别/期望判定）
  difficulty           text CHECK (difficulty IN ('easy','medium','hard')),
  source_ref           jsonb,                   -- 来自哪次生产（供追溯）
  PRIMARY KEY (tenant_id, dataset_id, dataset_version, item_id)
);

CREATE TABLE analytics.evaluation_run (
  tenant_id            text NOT NULL,
  evaluation_run_id    text NOT NULL,
  dataset_id           text NOT NULL,
  dataset_version      integer NOT NULL,
  evaluator_ref        jsonb NOT NULL,          -- 评测器（可能是 S19 agent 或规则）
  state                text NOT NULL CHECK (state IN
                       ('CREATED','QUEUED','RUNNING','SUCCEEDED','PARTIAL',
                        'FAILED','CANCELLED')),
  item_total           integer NOT NULL,
  item_succeeded       integer NOT NULL DEFAULT 0,
  item_failed          integer NOT NULL DEFAULT 0,
  result_ref           jsonb,                   -- 结果 artifact
  budget_ref           jsonb,                   -- 若产生了推理成本，引用 S23/S26
  started_at           timestamptz,
  finished_at          timestamptz,
  PRIMARY KEY (tenant_id, evaluation_run_id)
);

CREATE TABLE analytics.blind_review (
  tenant_id            text NOT NULL,
  blind_review_id      text NOT NULL,
  dataset_id           text NOT NULL,
  dataset_version      integer NOT NULL,
  dimensions           text[] NOT NULL,
  item_count           integer NOT NULL,
  reviewer_count       integer NOT NULL,
  state                text NOT NULL CHECK (state IN ('OPEN','CLOSED','ABANDONED')),
  agreement_alpha      numeric(4,3),
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, blind_review_id)
);

CREATE TABLE analytics.blind_review_vote (
  tenant_id            text NOT NULL,
  blind_review_id      text NOT NULL,
  reviewer_id          text NOT NULL,
  item_id              text NOT NULL,
  dimension            text NOT NULL,
  score                integer NOT NULL CHECK (score BETWEEN 1 AND 5),
  comment_ref          jsonb,
  voted_at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, blind_review_id, reviewer_id, item_id, dimension)
);

CREATE TABLE analytics.experiment (
  tenant_id            text NOT NULL,
  experiment_id        text NOT NULL,
  name                 text NOT NULL,
  arms                 jsonb NOT NULL,
  planned_metrics      text[] NOT NULL,
  assignment_rule      jsonb NOT NULL,          -- 确定性哈希规则
  state                text NOT NULL CHECK (state IN ('DRAFT','RUNNING','STOPPED','COMPLETED')),
  started_at           timestamptz,
  stopped_at           timestamptz,
  PRIMARY KEY (tenant_id, experiment_id)
);

CREATE TABLE analytics.experiment_assignment (
  tenant_id            text NOT NULL,
  experiment_id        text NOT NULL,
  subject_id           text NOT NULL,           -- episode_id 等
  arm                  text NOT NULL,
  assigned_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, experiment_id, subject_id)
);

CREATE TABLE analytics.cost_projection (
  tenant_id            text NOT NULL,
  projection_id        text NOT NULL,
  scope                jsonb NOT NULL,          -- {project_id} 或 {tenant_id}
  window               text NOT NULL CHECK (window IN ('7d','30d')),
  actual_to_date_micro bigint NOT NULL,
  projected_total_micro bigint NOT NULL,
  ci_low_micro         bigint,
  ci_high_micro        bigint,
  based_on_rate        numeric NOT NULL,
  computed_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, projection_id)
);

CREATE TABLE analytics.customer_roi (
  tenant_id            text NOT NULL,
  roi_id               text NOT NULL,
  contract_ref         jsonb NOT NULL,
  cohort_id            text NOT NULL,
  revenue_micro        bigint,                  -- 来自合同（外部输入，见 §11 C1）
  allocated_cost_micro bigint NOT NULL,
  gross_margin         numeric,
  as_of                timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, roi_id)
);

CREATE TABLE analytics.outbox (
  event_id             uuid PRIMARY KEY,
  tenant_id            text NOT NULL,
  aggregate_id         text NOT NULL,           -- 见 §6.3 事件表
  aggregate_sequence   bigint NOT NULL,
  event_type           text NOT NULL,
  partition_key        text NOT NULL,     -- tenant_id || ':' || aggregate_id
  payload              jsonb NOT NULL,
  headers              jsonb NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  published_at         timestamptz
);
CREATE INDEX outbox_unpublished_idx ON analytics.outbox (created_at)
  WHERE published_at IS NULL;

CREATE TABLE analytics.inbox (
  consumer             text NOT NULL,
  event_id             uuid NOT NULL,
  received_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

**不建的表：**

- 不建 `ledger` / `balance` / `reservation`。归 S23。
- 不建 `qc_result`。归 S35–S38。
- 不建 `accepted_master`。归 S08。

---

## 6. 对外契约

### 6.1 查询（主要形态）

`GET /series/{series_id}/metrics`（`contracts/openapi.v2.yaml` 已定义，但**响应过于贫瘠**，见 §11 C2）

当前契约只返回：

```jsonc
{"series_id":"...","cohort_id":"...","projection_watermark":123,"report_ref":{...}}
```

本方案要求的响应（**提案**）：

```jsonc
{
  "series_id":"ser_1",
  "cohort_id":"coh_...",
  "projection_watermark": 123,
  "data_as_of": "2026-11-01T09:59:00Z",
  "staleness": {"s23":"12s","s26":"3s","s35":"45s"},
  "metrics": [
    {
      "key":"usable_rate","version":2,"value":0.81,"unit":"ratio","n":24,
      "distribution":{"p50":1.0,"p90":1.0,"p95":1.0,"max":1.0},
      "ci95":{"low":0.62,"high":0.92},"method":"wilson",
      "excluded":{"failed_runs":2,"abandoned":1}
    },
    {
      "key":"cost_per_usable_minute","version":3,"value":4.21,"unit":"CNY/min","n":1130,
      "distribution":{"p50":3.90,"p90":5.60,"p95":6.30,"p99":8.10,"max":9.40},
      "ci95":{"low":3.42,"high":5.18},"method":"bootstrap_percentile",
      "excluded":{"failed_runs":2,"estimated_allocation_minutes":40}
    },
    {
      "key":"first_pass_shot_rate","version":2,"value":0.67,"unit":"ratio","n":312,
      "ci95":{"low":0.62,"high":0.72},"method":"wilson",
      "excluded":{"abstain_treated_as":"not_pass"}
    }
  ],
  "mixed_versions": false,
  "version_set": {"model":[{"id":"m_x","revision":4}],"prompt":[...],"policy":[...]},
  "allocation_policy_version":"alloc-3",
  "report_ref": {"package_id":"dp_report_...","manifest_hash":"sha256:..."}
}
```

**`data_as_of` 与 `staleness` 是必需的**。docs/04 §5："每个投影返回 watermark 与 source_revision… 不能把旧进度显示成当前已完成"。指标同理：不能把 20 分钟前的成本显示成"现在"。

### 6.2 命令

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/cohorts` | 冻结 cohort |
| POST | `/metric-snapshots` | 计算快照 |
| POST | `/evaluation-runs` | 启动评测（可能产生推理成本 → 走 S26） |
| POST | `/blind-reviews` | 开盲评 |
| POST | `/blind-reviews/{id}/votes` | 投票（批量） |
| POST | `/experiments` | 建实验 |
| POST | `/experiments/{id}:stop` | 停止 |

**全部不带金额写入能力。** S44 的写操作只影响分析对象。

### 6.3 事件

| 方向 | 事件 |
|---|---|
| 发布 | `analytics.metric.snapshot.created`、`analytics.evaluation.completed`、`analytics.cost.projection.updated` |
| 消费 | `finance.ledger.entry-posted`（S23）、`generation.job.state-changed`（S26）、`quality.technical.completed`（S35）、`quality.semantic.completed`（S36）、`quality.alignment.completed`（S37）、`post.subtitle.ready`（S38）、`review.decision.recorded`（S40）、`snapshot.manifest.committed`（S08）、`delivery.package.published`（S43） |
| 调用 | S23 `GET /episodes/{id}/cost-breakdown?cohort=`、S26 查询、S35–S38 查询、S40 查询、S08 查询 |

消费侧的事件名按产出方实际登记的名字写，不是设计包里的粗粒度名。三处折叠关系：

| 本方案要的口径 | 目录里实际的事件 | 取哪个字段 |
|---|---|---|
| 每笔成本 | `finance.ledger.entry-posted`（S23 只发这一条） | `data.entry_type`；金额取 `data.settled_micro` / `delta_consumed_micro`；归属取 `data.attribution.*`；预算耗尽看 `data.budget_after.state` |
| 每次生成尝试的结局 | `generation.job.state-changed`（S26 只发这一条） | 按 `data.attempt_id` 聚合，用 `data.state` 判终态，`data.provider`/`data.channel`/`data.node_id`/`data.node_epoch` 做维度 |
| 质检通过/缺陷 | 四条各发一条：`quality.technical.completed`（S35）、`quality.semantic.completed`（S36）、`quality.alignment.completed`（S37）、`post.subtitle.ready`（S38） | 四个 QC 服务的报告结构不同，K5 返工率的分母按 **defect_type 的闭集**归并（`contracts/quality/action-families.v2.json` 的 14 类），不按服务归并 |

**这四条 QC 事件不合并成一条 `quality.qc.completed`**：那是设计包的粗粒度名，没有任何服务产出，
订阅它只会静默收到空流。若将来要合并，应该新建一个聚合投影，而不是让 S44 假装有一个统一事件。

发布事件的聚合键：

| event_type | aggregate_id |
|---|---|
| `analytics.metric.snapshot.created` | `snapshot_id` |
| `analytics.evaluation.completed` | `evaluation_run_id` |
| `analytics.cost.projection.updated` | `projection_id` |

### 6.4 S44 的反向使用者：S25

S25 的模型路由会消费 S44 的 **per-model 首轮可用率**（S25 方案已记录）。这不是 S44 越界，是正常的读模型被消费。约束：

- S25 只能用**已经算完的、带 n 与 CI 的快照**，不能自己从原始数据算。
- S25 用它做**排序/加权**，不能做**硬性禁用**（禁用模型是 S24/S27 的职责）。
- 快照的 `low_confidence=true` 时，S25 不得据此调整（否则小样本噪声会驱动路由抖动）。

### 6.5 口径导出（给客户与 FDE）

`GET /metric-snapshots/{id}/export?format=csv|xlsx` 导出的不是"数字"，是**带口径的表格**：

- 首行：指标定义版本、cohort 定义、时间窗、watermark、分摊策略版本。
- 每个数字旁：n、CI、排除项。
- 缺数据：空单元格 + 注释，不是 0。

**这是 E40（FDE 移交）的实际落点**：客户拿到报表要能自己解释口径，而不是问"这个 4.21 是怎么来的"。

---

## 7. 幂等与并发

| 操作 | 幂等 |
|---|---|
| 快照计算 | `tenant + cohort_id + metric_key + version + watermarks` 作为天然唯一键；重复请求返回已有快照 |
| 评测启动 | `tenant + command_id + request_hash` |
| 盲评投票 | `(blind_review_id, reviewer_id, item_id, dimension)` 主键；重复投票覆盖同一条（投票允许改，但记录 `voted_at`）|
| 实验分配 | `(experiment_id, subject_id)` 主键；确定性规则保证同结果 |
| 事件消费 | inbox |

**并发要点：**

- 同 cohort 并发计算 → 允许，靠唯一键收敛；若两个并发结果不同（源数据在窗口内变了），以 watermark 较新的为准，另一个标 `discarded_nondeterministic` 并告警。
- 分摊写入按 `(cost_entry_id, policy_version, dimension, dimension_value)` 幂等；重跑分摊不产生重复金额。
- **绝不能**因为"重算"而删除旧分摊再写新的——那会让"为什么这个月成本变了"无法回答。新 policy = 新 `policy_version`，并存。

---

## 8. 可观测

| 指标 | 类型 | 用途 |
|---|---|---|
| `analytics_snapshot_compute_seconds` | histogram（按 metric） | 计算耗时 |
| `analytics_snapshot_partial_total` | counter（按 stale_source） | 数据新鲜度问题 |
| `analytics_allocation_unallocated_micro` | gauge | 分摊覆盖率（应趋近 0） |
| `analytics_allocation_invariant_violation_total` | counter | 分摊 bug（必须为 0） |
| `analytics_eval_run_partial_total` | counter | 评测稳定性 |
| `analytics_blind_review_low_alpha_total` | counter | 标注质量问题 |
| `analytics_snapshot_nondeterministic_total` | counter | 非确定性 bug |
| `analytics_projection_lag_seconds` | gauge | 事件消费延迟 |

**告警**：`allocation_invariant_violation_total > 0`、`snapshot_nondeterministic_total > 0`、`allocation_unallocated_micro` 持续非零。

trace：

```
analytics.snapshot
  ├─ cohort.freeze
  ├─ s23.cost_breakdown
  ├─ s26.query_attempts
  ├─ s35..s38.query_qc
  ├─ s08.query_accepted
  ├─ allocation.apply
  └─ stats.compute
```

**日志不含**：客户合同金额、评审者身份明细（用 reviewer_id 哈希）、prompt 全文。

---

## 9. 测试清单

### E38 —— 费用与效果重算

| 用例 | 期望 |
|---|---|
| E38-S44-01 | S23 原始条目不变，改变 `AllocationPolicy` 版本 → 新快照，旧快照保留，两者可对比 |
| E38-S44-02 | `sum(cost_allocation) == sum(cost_entry)`（整数 micro 精确） |
| E38-S44-03 | 同 cohort + 同 watermark 重算 → 结果逐字段相同 |
| E38-S44-04 | cohort 含失败 run，K1 分母包含它们（不被悄悄剔除） |
| E38-S44-05 | `excluded` 字段在所有快照都出现，且计数与实际剔除一致 |
| E38-S44-06 | n=0 → `value=null`，不返回 0 |
| E38-S44-07 | 成本类指标同时给 p50/p95/max 与 CI；n<30 标 `low_confidence` |
| E38-S44-08 | 估算分摊与精确分摊在同一 KPI 中被标记区分，不混成一个数 |
| E38-S44-09 | 评测成本类别不计入 K3 的生产成本 |
| E38-S44-10 | S44 无任何写入 S23 的路径（静态断言 + 网络策略） |

### E25 —— abstain 不等于 pass

| 用例 | 期望 |
|---|---|
| E25-S44-01 | K2 的分母包含 abstain 的镜头，分子不计入 abstain |
| E25-S44-02 | 快照里 `abstain_treated_as: "not_pass"` 显式可见 |
| E25-S44-03 | 盲评 n<5 的维度标 `insufficient_data`，不给均值 |

### E28 —— 跨租户

| 用例 | 期望 |
|---|---|
| E28-S44-01 | 跨租户查询快照/cohort → 404 |
| E28-S44-02 | cohort 定义中的 project_ids 必须属于同租户，否则拒绝 |
| E28-S44-03 | 聚合结果不带可识别他租户的信息（如"行业平均"若含跨租户数据需聚合到 k≥10） |

### E36 —— 水位与陈旧

| 用例 | 期望 |
|---|---|
| E36-S44-01 | 源 watermark 落后 → 响应带 `staleness`，工作台可显示"截至"时间 |
| E36-S44-02 | 快照 attempt 带旧 watermark → 不覆盖新 watermark 的快照 |

### E12 / E13

| 用例 | 期望 |
|---|---|
| E12-S44-01 | 同 command 重投快照请求 → 返回同一 snapshot_id |
| E12-S44-02 | 重复评测启动 → 不产生第二次付费（经 S26 幂等） |
| E13-S44-01 | 计算中崩溃 → 无半写快照（事务） |
| E13-S44-02 | outbox 重投 → 下游去重 |

### E19 / E26 —— 评测与返工成本

| 用例 | 期望 |
|---|---|
| E26-S44-01 | 跨 run 返工成本正确归集到同一镜头，不因 run 不同而分账 |
| E26-S44-02 | 返工率 K5 按镜头计，不按次计 |

### 一致性与恢复

| 用例 | 期望 |
|---|---|
| C-S44-01 | 分摊整数不变量在 10000 笔随机数据下成立 |
| C-S44-02 | 升级：新指标版本不破坏旧快照查询 |
| C-S44-03 | 回滚：旧版本不认识新 `basis` → 拒绝分摊，不静默归零 |
| C-S44-04 | 导出 CSV 口径头完整，可被第三方复算 |

---

## 10. 实施顺序

| 步骤 | 内容 | 前置 |
|---|---|---|
| 1 | `MetricDefinition` 七个 KPI + 口径写死（与 docs/15 逐字对齐） | W0 |
| 2 | `cohort` 冻结 + watermark 记录 | 1 |
| 3 | S23 只读接入 + `AllocationPolicy` + 整数分摊 | 2 |
| 4 | K1/K3/K4 计算（数据最全的三项） | 3 |
| 5 | K2/K5/K6（依赖 S35–S40） | 4 |
| 6 | 分位数与 CI（Wilson / bootstrap） | 4 |
| 7 | 快照查询 + 导出口径头 | 6 |
| 8 | 评测运行器 + benchmark 数据集 | 2 |
| 9 | 盲评 + alpha | 8 |
| 10 | 实验与分配 | 8 |
| 11 | K7/K8（依赖链路最长） | 5 |

**第 1 步必须先做且必须与文档逐字对齐**：七个 KPI 的分子分母如果实现时"顺手优化"，后续所有报表都会和客户对不上。

**S23 兼容是阻塞项**：S23 方案已提供 `GET /episodes/{id}/cost-breakdown?cohort=`，需要按其实际字段实现，不能自建费用查询。

---

## 11. 冲突（报告，不选边）

| # | 冲突 | 双方原文 | 影响 |
|---|---|---|---|
| C1 | 合同收入无权威 | docs/15 §3 列"企业毛利/ROI"；48 服务无合同/收入服务；`customer-configs/` 非服务 | K8 的分子无从获取。本方案留 `revenue_micro` 为外部输入并标来源 |
| C2 | `/series/{id}/metrics` 响应无法表达七个 KPI | OpenAPI 只返回 `series_id/cohort_id/projection_watermark/report_ref`；docs/15 要求样本量、时间范围、版本、p50/p95、CI | 本方案给出扩展响应（提案）。冻结前看板只能拿 `report_ref` 去取离线报告 |
| C3 | KPI 数量口径 | docs/15 §3 列了"成片可用率、首轮镜头可用率、每可用分钟成本、单集交付耗时、返工率、人工干预、记忆正确性、企业毛利/ROI"（8 项），任务描述称"7 个 KPI" | 本方案按 8 项实现（K1–K8），并在 §2.3 注明差异。需文档 owner 确认是 7 还是 8 |
| C4 | 分摊规则的归属 | docs/15 说"S44 按固定 allocation policy 做经营投影"；未说 policy 由谁定义、谁批准 | 本方案把 `AllocationPolicy` 放在 S44 自身且版本化；若应由 S02 治理，需要迁移 |
| C5 | 评测产生的推理成本归属 | docs/13 说 S19 的 `episode.plan`/`memory.extract` 可产生有界推理成本；S44 的评测若用 LLM 评判，成本算生产还是评测 | 本方案要求走 S26 + 标 `cost_type=evaluation`，并排除出 K3 |
| C6 | `projection_watermark` 的单值 vs 多源 | OpenAPI 只给一个 `projection_watermark`；实际有 S23/S26/S35/S40/S08/S43 六个源 | 本方案用 `staleness` 分源表达；单值字段保留为"最慢源"的投影 |
| C7 | K7「记忆正确性」的可计算性 | docs/15 定义"记忆事实被后续剧情证伪前未被发现"；但"证伪"的判定归 S17/S40，S44 无权判定 | 本方案只统计"被 S17 标为冲突且 S40 确认"的条数，不自行判定证伪 |
| C8 | 发布事件名与目录不一致，且消费侧有四个名字在目录里不存在 | 目录对 S44 只登记 `evaluation.report.published`；本方案 §6.3 发 `analytics.metric.snapshot.created` / `analytics.evaluation.completed` / `analytics.cost.projection.updated`。§6.3 消费侧 `billing.cost.recorded`（S23）、`generation.attempt.completed`（S26）、`quality.qc.completed`（S35–S38）、`delivery.package.published`（S43）**四个名字在 `event-topics.v2.json` 里都没有** | **已裁决（2026-11-05，commit `4f1bdcb`）**，分两侧处理：<br>**发布侧（补登）**——三条 `analytics.*` 全登记，topic `drama.evaluation-analytics.v2`，删除无消费方的 `evaluation.report.published`。设计包那一行保持原样（只读），差异由本行与 [验收报告](../delivery/acceptance/plans-consistency-2026-10-30.md) 记录。<br>**消费侧（改名，不补登）**——四个错名是方案写错了产出方，登记它们会造出没有生产者的事件：`billing.cost.recorded` → `finance.ledger.entry-posted`；`generation.attempt.completed` → `generation.job.state-changed`；`quality.qc.completed` → 拆成 `quality.technical.completed` / `quality.semantic.completed` / `quality.alignment.completed` / `post.subtitle.ready` 四条；`delivery.package.published` 本身已随 S43 补登，无需改。字段映射见 §6.3 的表。<br>**payload schema 仍缺**（与 [S43 §11 C8](S43-delivery.md) 同一条） |

---

## 12. 未决问题

1. **合同的收入与成本边界**：平台费、人力、渠道分成是否计入？不同客户合同可能不同，`AllocationPolicy` 是否要按租户定制版本？
2. **KPI 的刷新频率**：每天算一次够不够？实时看板需要"近 1 小时"的指标，但小样本 CI 会极宽。需要一个"低延迟、低置信"的展示层。
3. **cohort 冻结的性能**：一个 24 集项目 × 6 个月的 cohort 可能几十万条记录。冻结成员集合是存 id 列表还是存可重放的谓词？前者大，后者要求源数据不变。
4. **盲评的评审者来源**：内部员工？外聘？S44 只记录 reviewer_id，身份与权限归 S01。若需要匿名化与反偏见设计，需要产品定义。
5. **实验与生产的隔离**：实验臂的产物是否会进入正式交付？当前假设"不会"，但没有强制机制。
6. **报表的合规口径**：客户可能要求特定会计准则的成本分类。S44 的分摊维度是否够用，需要财务确认。
