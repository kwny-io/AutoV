# S48 · 适配训练 —— 实现方案

状态：实现方案（W5）。`maturity: design_only` 不变。按 docs/16 §3，"W5 S48 按客户需要训练"，所以本服务的开关默认全关，且**只在客户明确订购时启用**。

上游依据：[docs/10 §资产](../docs/10-assets.md)、[docs/14 §2](../docs/14-enterprise-operations.md)（"训练样本与声音样本采取严格访问、保留和撤销流程"）、[docs/03 §容量](../docs/03-deployment-capacity.md)（"训练不允许挤占在线推理保底容量"）、[S48 规格](../docs/design-package/services/S48-adaptation-training.md)。

配套：[S15 音色](S15-voice-assets.md)（授权撤销）、[S11/S12/S13/S14](S11-character-assets.md)（人物/服装/场景/道具授权）、[S24 模型注册](S24-model-registry.md)（基座兼容性）、[S28 制品](S28-artifact-ingest.md)（数据集来源）、[S22](S22-scheduler-quota.md)（`modality='training'`）、[S23](S23-budget-ledger.md)（训练额度）、[S45](S45-audit.md)（训练审计）。

---

## 1. 边界：写什么 / 不写什么 / 一条必须坚持的原则

### 1.1 写什么

S48 是**训练作业**与**训练产物**的权威。它持有的五类事实：

| 事实 | 表 | 语义 |
|---|---|---|
| 训练作业 | `training_job` | 谁在训练什么、用什么数据、到哪一步 |
| 数据集清单 | `dataset_manifest` / `dataset_item` | 精确到每一项的来源与授权引用 |
| 基座兼容性 | `base_model_compatibility` | 这个 LoRA 能挂到哪些基座、为什么不能 |
| 训练产物 | `lora_artifact` | 权重 hash、评测结果、是否可挂载 |
| 训练额度消耗 | `training_slot_usage` | 用了多少 GPU 秒（**记账在 S23，这里只是本服务的作业视图**） |

### 1.2 不写什么

| 不是 S48 的事实 | 归属 |
|---|---|
| 素材本身 | **S28**（bytes） |
| 素材的授权状态 | **S11/S12/S13/S14/S15**。S48 只持有"我在提交训练时看到的是什么"这一条**历史快照** |
| 用户/角色权限 | **S01** |
| 模型注册表与能力 | **S24**。S48 说"这个 LoRA 兼容基座 X 的 revision Y"，S24 说"基座 X revision Y 存在且可用" |
| 钱 | **S23** |
| 配额 permit | **S22**（`modality='training'`、`resource_class='gpu_second'`） |
| 在线推理的模型加载 | **S25** 路由 + **S26** 执行。S48 不挂载任何 LoRA 到在线服务 |
| 数据集质量的最终裁决 | **S44** 评测（S48 提供训练侧指标，S44 做跨服务口径） |

### 1.3 一条必须坚持的原则

> **训练产物不是新的权威；训练也不能绕过任何一条素材授权。**

这句话拆成三条硬规则，每一条都是可判定的：

1. **`lora_artifact` 被注册到 S24 之后，它的效果好坏由 S24 的能力记录和 S25 的路由共同决定。** S48 不能说"这个 LoRA 很好，所有视频模型都能用"（docs/10 原文："S48独立训练适配，不把LoRA当所有视频模型通用参数"）。
2. **训练数据的每一项都要有一个 `rights_ref`，指向当时有效的授权。** 没有 `rights_ref` 的数据项**不能进数据集**。这不是运行时的可选校验，是 `dataset_item` 的 `NOT NULL`。
3. **授权在训练开始后撤销，不会让已完成的训练变成"合法"。** 它会让这个 LoRA **不能被挂载**、**不能被用于新生成**，并且触发一次影响评估。S48 不做"既然已经训了就继续用"的判断（E30 的同类纪律）。

### 1.4 与 S15 的关系：为什么这条最容易破

S15 的授权撤销（`revoked`）与供应商下架（`suspended`）是两件事（S15 §1.6）：前者是法律状态，后者是技术状态。S48 必须能区分，因为：

- 撤销的音色**永远不能**用于新训练（数据项本身不合法）。
- 已用撤销音色训练出的 LoRA **不能**被挂载使用，但 artifact 的字节仍在（删除要单独走 S28 的删除编排）。

最容易破的地方是"增量训练"：客户想"在已有 LoRA 上再训一点"。如果增量数据里有撤销素材，整个新 LoRA 无效——但已经烧掉的 GPU 秒退不回来。所以**授权复核必须在训练启动前做一次全量（不是增量）检查**（§3.3）。

### 1.5 与 S24 的关系：`LORA_INCOMPATIBLE`

S25 的行 19 有 `LORA_INCOMPATIBLE` 错误码（S24 + S48 共同产生）。分工：

- **S48 说**：这个 LoRA 训练时基于 `base_id=mdl_x`, `base_revision=7`, `base_content_hash=ab..`。
- **S24 说**：当前在线可用的 `mdl_x` 是 revision 9，且 S24 认为 revision 7 → 9 不兼容（或兼容）。
- **判定**：S25 在路由时**实时问 S24**，不是查 S48 的缓存。

所以 S48 的 `base_model_compatibility` 表是"训练时刻的判断记录"，**不是"当前是否兼容"的权威**。它可以过期。字段名与注释必须让人意识到这点（与 S47 的 `manifest_revision` 同类问题）。

---

## 2. 输入契约

### 2.1 命令入口 `POST /training-jobs`

```jsonc
{
  "command_id": "cmd_01J...",
  "request_hash": "9f2c…",
  "adaptation_type": "character_lora",        // character_lora | style_lora | voice_clone
  "target_ref": {
    "service_id": "S11",
    "id": "char_linlan",
    "revision": 4,
    "content_hash": "…"
  },
  "dataset": {
    "mode": "explicit",                        // explicit | derived_from_artifact_set | derived_from_run
    "artifact_refs": [
      { "service_id": "S28", "id": "art_01J...", "content_hash": "…" },
      { "service_id": "S28", "id": "art_01K...", "content_hash": "…" }
    ],
    "exclude_artifact_refs": []
  },
  "base_model": {
    "service_id": "S24",
    "id": "mdl_x",
    "revision": 7,
    "content_hash": "…"                        // 必须指定 revision + hash，不接受"最新"
  },
  "hyperparams": {
    "profile": "character_default_v3",         // 命名档位，不是自由参数
    "steps": 1200,
    "lora_rank": 32
  },
  "evaluation": {
    "rubric_ref": "…",
    "holdout_refs": [ … ]
  },
  "budget_authorization_ref": "bud_01J..."     // S23 的授权
}
```

三条硬规则：

- **`base_model` 必须指定 `revision` + `content_hash`。** 不接受"用最新的"。理由：训练完成后基座漂移了，无法复现，也无法判断兼容性。这与 S08 的 `manifest_ref{id,revision,content_hash}` 同一纪律。
- **`dataset.mode` 是枚举，不是自由字段。** `explicit` 是指定 artifact；`derived_from_run` 是从某次 run 的输入集推导。后者更危险（可能带进未授权的素材），需要更严的复核。
- **`budget_authorization_ref` 必填。** 训练必须走 S23 的授权；S48 不预扣、不记账、不缓存余额。

### 2.2 数据集的来源解析

```text
dataset.mode = explicit
  → 直接取 artifact_refs

dataset.mode = derived_from_run
  → 调 S28 查该 run 的输入集
  → 对每一项再查它的 rights_ref（见 §2.3）
  → 与 artifact_refs 求交/差

dataset.mode = derived_from_artifact_set
  → 调 S10 查该集合的成员
  → 同上
```

**推导模式必须显式列出解析结果。** 数据集清单生成后，响应里要给出完整的 `resolved_items`，让调用方确认"这些就是你要的"。不允许"我按描述推了 800 张，你就这么用吧"。

### 2.3 每一项的授权引用

`rights_ref` 的形状（本文提案，C48-2）：

```jsonc
{
  "service_id": "S15",              // 授权权威所在服务
  "id": "voice_linlan_cn",
  "revision": 3,
  "content_hash": "…",
  "grant_kind": "voice_clone_training",   // 明确区分"可用于生成"与"可用于训练"
  "checked_at": "2026-10-30T08:00:00Z",
  "state": "active"                  // active | revoked | suspended | expired
}
```

`grant_kind` 是关键：一个音色可能允许"用于生成"但**不**允许"用于训练"。S15/S11–S14 的授权必须能回答这个区分。若某服务的授权模型没有这个区分，那是 C48-3——S48 不能自己发明一个。

### 2.4 谁可以发起训练

| 来源 | 需要的 scope | 说明 |
|---|---|---|
| 控制台用户 | `training.create` + 目标资产的 `manage` | 需 S01 判定 |
| 外部 API（S46） | `execute` | S46 的 `plan` 级别**不能**触发训练（训练是重度、不可逆的资源消耗） |
| MCP / Agent | **不允许**（本文建议） | 训练不是"快速动作"，Agent 自动发起风险过高。需产品确认 |

最后一条是本文的判断：MCP 工具目录（`mcp-tools.v2.design.json`）里确实没有训练工具，这与本文一致。

---

## 3. 核心逻辑与状态机

### 3.1 训练作业状态机

```text
   REQUESTED
      │
      ├──(授权复核失败)──────────────────────────► REJECTED
      │
      ├──(数据集解析为空/超限)───────────────────► REJECTED
      │
      ▼
   ADMITTED ──(S22 permit + S23 授权通过)──► QUEUED
      │                                          │
      │                                          ├──(GPU 队列无容量)──► (等待)
      │                                          ▼
      │                                      PREPARING
      │                                          │  数据固化、冻结数据集清单
      │                                          ▼
      │                                      TRAINING ──(checkpoint 中断)──► RESUMING ──► TRAINING
      │                                          │
      │                                          ├──(失败不可恢复)──► FAILED
      │                                          ▼
      │                                      EVALUATING
      │                                          │
      │                                          ├──(评测不达标)──► EVAL_FAILED
      │                                          ▼
      │                                      CANDIDATE ──(人工/策略接受)──► PUBLISHED
      │                                          │
      │                                          └──(授权在训练后被撤销)──► BLOCKED
      │
      └──(超时未准入)──► EXPIRED
```

| 状态 | 含义 | 资源占用 | 可否取消 |
|---|---|---|---|
| `REQUESTED` | 已受理，未复核 | 无 | 是 |
| `REJECTED` | 复核失败（终态） | 无 | — |
| `ADMITTED` | 复核通过，等待准入 | 无 | 是 |
| `QUEUED` | 拿到 permit，等 GPU | 有 permit | 是（释放 permit） |
| `PREPARING` | 固化数据集 | GPU 未用 | 是 |
| `TRAINING` | 在训 | **GPU** | 是（部分 checkpoint 保留） |
| `RESUMING` | 从 checkpoint 续 | GPU | 是 |
| `EVALUATING` | 跑评测 | GPU（小） | 否（短） |
| `EVAL_FAILED` | 评测不达标（终态） | 无 | — |
| `CANDIDATE` | 产物待接受 | 存储 | — |
| `PUBLISHED` | 已注册到 S24 | 存储 | — |
| `BLOCKED` | 授权撤销，禁止挂载 | 存储 | — |
| `FAILED` | 训练失败（终态） | 无 | — |
| `EXPIRED` | 未准入超时（终态） | 无 | — |

**`EVAL_FAILED` 不是 `FAILED`。** 前者是"训出来了但不好"，后者是"没训出来"。两者对客户的意义不同：前者说明数据或超参有问题，后者说明基础设施/基座有问题。合并成一个码会让排查无从下手（docs/10："未通过回归的训练结果不能直接挂到线上关键角色"）。

### 3.2 准入（S22 + S23）

训练不走 S26（它不是模型生成），但**必须**走 S22 的 permit 与 S23 的授权：

```text
1. 复核通过 → 记 dataset_manifest（冻结）
2. S23: 校验 budget_authorization_ref 有效、额度足够（训练按 GPU 秒估价）
3. S22: 申请 modality='training' 的 permit，resource_class='gpu_second'
4. 拿到 permit → QUEUED
5. 真正开始 TRAINING 前，再确认一次 permit 未过期
```

**S22 的 permit 是训练的硬闸。** 没有 permit 不能进 `TRAINING`（即使 GPU 空闲）。理由：permit 是"这次训练有权占用多少 GPU 秒"的授权，它和 S23 的钱是两件事（一边是资源，一边是成本）。

`resource_class='gpu_second'` 已在 S22 方案里定义，`modality='training'` 也已在 `quota_pool.modality` 枚举里——这两条是 S48 能工作的前提，且都已就位。

### 3.3 授权复核（本服务最重要的逻辑）

```text
resolve_dataset(items)
  ├─ 对每一项：
  │    rights_ref ← 查权威（S15/S11/S12/S13/S14）
  │    if state != 'active'  → 该项排除，记入 excluded[]
  │    if grant_kind 不含 'training' → 该项排除，记入 excluded[]
  │    if 权威不可达 → **整个作业 REJECTED**（fail closed）
  │
  ├─ 若剩余项 < min_items 或 剩余项 / 原始项 < min_ratio → REJECTED
  │
  └─ 生成 dataset_manifest（冻结 hash）
```

三条必须坚持的点：

1. **权威不可达 → 拒绝，不是"跳过这一项"。** 跳过会让数据集静默变小，训出来的 LoRA 效果差且无法解释。fail closed 是唯一正确选择。
2. **`min_ratio` 默认 0.9。** 若有 30% 的素材被排除，说明这个数据集的前提已经不成立，应该重新设计数据而不是"用剩下的 70% 凑合"。
3. **复核结果写进 `dataset_manifest.exclusions`。** 谁被排除、为什么、当时的状态是什么——这是 E30/E39 要求"可追溯"的落点。

### 3.4 数据固化

`PREPARING` 阶段做的事：

```text
1. 对每个 dataset_item，从 S28 取一个**不可变引用**（不是临时下载 URL）
2. 计算 items_hash = sha256(排序后的 (artifact_id, content_hash) 列表的 JCS)
3. 写入 dataset_manifest(state='FROZEN', items_hash)
4. 训练时只读这个 manifest，不再回查 S28 的"当前状态"
```

**为什么要 `items_hash`**：训练完成后要能回答"这个 LoRA 是用哪 812 张图训的"。仅记录 artifact id 不够——artifact 的 bytes 可能被替换（虽然 S28 保证不可变，但跨服务要能在 S48 侧独立验证）。

若 `PREPARING` 时发现某项的 `content_hash` 与 `artifact_refs` 里给的不一致 → `REJECTED`（数据在复核之后变了）。这是 S08 式的 hash 校验。

### 3.5 训练执行

```text
TRAINING
  ├─ 每 N 步写 checkpoint（到对象存储，S48 的桶，不是 S28）
  ├─ 上报进度事件（节流，每 30s）
  ├─ 定期心跳 S22（证明仍在占用 GPU）
  └─ 完成 → EVALUATING
```

**checkpoint 存在 S48 自己的存储里。** 它不是"制品"（S28 的 domain），是一次训练的内部中间态。训练成功后才把最终权重作为 artifact 注册到 S28（见 §3.7）。

**进度上报不走 S47。** S48 发事件到 `drama.adaptation-training.v2`，S47 消费后投影。这与其它服务同一模式。

### 3.6 评测

```text
EVALUATING
  ├─ 跑训练侧指标（不是 S44 的评测）：
  │    · identity_preservation（人物一致性）
  │    · overfit_score（过拟合）
  │    · holdout_loss
  │    · 若 adaptation_type=voice_clone：音色相似度 + 可懂度
  ├─ 与 base model 的 baseline 对比
  └─ 达标 → CANDIDATE；不达标 → EVAL_FAILED
```

docs/10 的 S48 行明确列了"兼容、身份保持、过拟合、训练来源"四个维度。S48 负责前三，第四是 §3.3 的复核。

**评测结果必须带 holdout 集引用。** 用训练集自评没有意义。`evaluation.holdout_refs` 必须与 `dataset` 无交集——S48 在 `PREPARING` 时检查这一点，有交集 → `REJECTED`。

### 3.7 接受与注册

```text
CANDIDATE ──(接受)──► PUBLISHED
```

接受是**人工或策略**决定的（与 S08 的 `acceptance_mode` 同构）。两条路：

1. **人工接受**：S40 审核决定。
2. **策略自动接受**：S02 的 `acceptance_mode='auto'` 且评测指标超过阈值。

**`PUBLISHED` 之前必须做**：

```text
1. 把权重注册到 S28，拿 artifact_id
2. 写 base_model_compatibility（基于训练时的 base revision + hash）
3. 发 training.lora.registered 给 S11/S15（建议，不是自动接受）
```

第 3 条是 S11 方案里的约定：`training.lora.registered` 只写 `revision_reference` / `lora_refs` **建议**，永不自动接受。S15 同理（`clone_artifact_ref` 候选建议）。S48 不能替资产服务做决定。

### 3.8 授权撤销后的影响

```text
收到 S15/S11/S12/S13/S14 的 revoke 事件
  → 查哪些 dataset_manifest 引用了该授权
  → 查哪些 lora_artifact 基于这些 manifest
  → 对每个 PUBLISHED 的 artifact：
      · 状态 → BLOCKED
      · 发 lora.blocked 事件（S24/S25 消费，禁止路由）
      · 写 S45 审计
  → 不删除 artifact bytes（删除走 S28）
```

**`BLOCKED` 是终态的一种，不能回到 `PUBLISHED`。** 即使后来重新授权了，也应该训一个新的 LoRA——因为无法证明"重授权覆盖了训练期间"。这与 S15 的"撤销不能复活"（§1.6）同一纪律。

### 3.9 与在线推理的资源隔离

docs/03 原文："训练不允许挤占在线推理保底容量"。

实现：

- **独立 Deployment**（`service.yaml`: "专用GPU队列、长checkpoint、训练与在线推理隔离"）。
- **独立 node pool**，与 S26 的推理池物理隔离。
- **S22 的 `modality='training'` permit 是独立池**，不会抢 `video`/`image` 的 permit。
- **背压**：训练队列满时，新作业在 `QUEUED` 等待，不抢占推理资源。

这条要在部署里体现，不只是配置。若训练与推理共用 GPU 池，"保底容量"就是一句空话。

---

## 4. 输出契约

### 4.1 命令与查询

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/training-jobs` | 创建，201 + `job_id` |
| GET | `/training-jobs/{id}` | 详情（含 `dataset_manifest`、`evaluation`、`artifact_ref`） |
| GET | `/training-jobs?state=&from=&to=&cursor=` | 列表 |
| POST | `/training-jobs/{id}:cancel` | 取消（§3.1 的可取消状态） |
| POST | `/training-jobs/{id}:accept` | `CANDIDATE` → `PUBLISHED`（需 S40 决定引用） |
| POST | `/training-jobs/{id}:reject` | `CANDIDATE` → `EVAL_FAILED`（人工否决） |
| GET | `/datasets/{id}` | 数据集清单（含 `exclusions`） |
| GET | `/datasets/{id}/items?cursor=` | 逐项（含 `rights_ref` 历史快照） |
| GET | `/adaptation-artifacts/{id}` | LoRA artifact（权重 hash、兼容性、评测） |
| GET | `/adaptation-artifacts/{id}/compatibility` | **训练时刻的**兼容性记录（§1.5） |

### 4.2 `GET /adaptation-artifacts/{id}`

```jsonc
{
  "artifact_id": "lora_01J...",
  "state": "PUBLISHED",                    // PUBLISHED | BLOCKED | SUPERSEDED
  "adaptation_type": "character_lora",
  "target_ref": { "service_id": "S11", "id": "char_linlan", "revision": 4 },

  "base_model": {
    "service_id": "S24",
    "id": "mdl_x",
    "revision": 7,
    "content_hash": "…",
    "compat_recorded_at": "2026-10-30T09:00:00Z"
  },

  "weights": {
    "artifact_ref": { "service_id": "S28", "id": "art_01L..." },
    "weights_hash": "sha256:…",
    "size_bytes": 145600000
  },

  "dataset_ref": { "service_id": "S48", "id": "ds_01J...", "items_hash": "…" },
  "evaluation": {
    "holdout_items_hash": "…",
    "metrics": {
      "identity_preservation": 0.87,
      "overfit_score": 0.14,
      "holdout_loss": 0.32
    },
    "baseline_ref": { "service_id": "S24", "id": "mdl_x", "revision": 7 },
    "passed": true,
    "evaluated_at": "2026-10-30T09:10:00Z"
  },

  "as_of": "2026-10-30T09:15:00Z",
  "__hints": {
    "authority": {
      "current_compatibility": "S24",       // ← 本表的兼容性是训练时刻的记录
      "base_available": "S24",
      "rights_current": "S11/S15"
    },
    "usable_for_generation": false           // 见下
  }
}
```

`usable_for_generation` 是**计算出来的**（当前 rights 全部 active 且 S24 说兼容），但它是**快照**，不是判定。真正的判定在 S25 路由时实时做。这个字段只用来避免 UI 显示"可用"而实际被拒——它等于"我上次检查时可用"。

### 4.3 发布事件

`adaptation.training.completed`（topic `drama.adaptation-training.v2`，`aggregate_id = job_id`）：

```jsonc
{
  "specversion": "1.0", "schema_version": "2.0",
  "id": "evt_...", "source": "S48", "type": "adaptation.training.completed",
  "subject": "training-job/job_01J...",
  "time": "2026-10-30T09:10:00Z",
  "tenant_id": "t_demo", "home_cell": "cell_a1",
  "aggregate_id": "job_01J...", "aggregate_sequence": 7,
  "correlation_id": "cmd_01J...", "causation_id": null,
  "traceparent": "00-...-...-01",
  "data": {
    "job_id": "job_01J...",
    "state": "EVAL_FAILED",               // 或 CANDIDATE / FAILED
    "adaptation_type": "character_lora",
    "target_ref": { "service_id": "S11", "id": "char_linlan", "revision": 4 },
    "base_model_ref": { "service_id": "S24", "id": "mdl_x", "revision": 7, "content_hash": "…" },
    "artifact_ref": null,                 // EVAL_FAILED 时为 null
    "dataset_ref": { "service_id": "S48", "id": "ds_01J...", "items_hash": "…" },
    "evaluation_passed": false,
    "metrics_summary": { "identity_preservation": 0.61, "overfit_score": 0.48 },
    "gpu_seconds_consumed": 18400
  },
  "data_ref": "training-job/job_01J..."
}
```

**事件里不含**：训练数据、权重 URL、prompt、任何素材内容。只有引用与指标。`gpu_seconds_consumed` 是给 S23/S44 做成本归集的。

`aggregate_sequence` 用 job 内部的阶段序号（每个状态迁移 +1），单调。

### 4.4 与 S11/S15 的建议事件

S48 还应发（本文建议，C48-4）：

- `training.lora.registered` → S11 写 `lora_refs` 建议
- `training.voice_clone.ready` → S15 写 `clone_artifact_ref` 建议

这两个事件**不是** `adaptation.training.completed` 的替代，而是它之后的"建议性通知"。S11/S15 的方案都已声明会消费它们，且都不自动接受。

### 4.5 错误码

| HTTP | code | 说明 |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | |
| 403 | `TRAINING_NOT_ENABLED` | 客户未订购 S48（默认关） |
| 403 | `TENANT_SCOPE_DENIED` | |
| 404 | `NOT_FOUND` | **含跨租户**（E28） |
| 409 | `IDEMPOTENCY_KEY_REUSED` | |
| 409 | `COMMAND_IN_PROGRESS` | |
| 409 | `JOB_STATE_CONFLICT` | 对不可取消状态发 cancel |
| 422 | `BASE_MODEL_NOT_FOUND` | S24 查不到该 revision |
| 422 | `BASE_MODEL_INCOMPATIBLE` | S24 明确说不兼容（训练前就拒，省 GPU） |
| 422 | `EMPTY_DATASET_AFTER_FILTER` | 授权复核后无剩余项 |
| 422 | `HOLDOUT_OVERLAP` | holdout 与训练集有交集 |
| 422 | `DATASET_TOO_LARGE` | 超上限 |
| 422 | `RIGHTS_UNVERIFIABLE` | 权威服务不可达，fail closed |
| 429 | `TRAINING_QUEUE_FULL` | 队列满，带 `Retry-After` |
| 503 | `DEPENDENCY_UNAVAILABLE` | S22/S23/S24 不可达 |

---

## 5. 数据模型

数据库 `adaptation_training`。

### 5.1 训练作业

```sql
CREATE TABLE training_job (
  job_id             text PRIMARY KEY,           -- 'job_01J...'
  tenant_id          text NOT NULL,
  command_id         text NOT NULL,
  request_hash       char(64) NOT NULL,

  adaptation_type    text NOT NULL
    CHECK (adaptation_type IN ('character_lora','style_lora','voice_clone')),
  target_service     text NOT NULL,              -- 'S11' | 'S15'
  target_id          text NOT NULL,
  target_revision    integer NOT NULL,
  target_content_hash char(64) NOT NULL,

  base_service       text NOT NULL DEFAULT 'S24',
  base_id            text NOT NULL,
  base_revision      integer NOT NULL,
  base_content_hash  char(64) NOT NULL,

  dataset_id         text,                       -- PREPARING 后回填
  hyperparams        jsonb NOT NULL,
  evaluation_spec    jsonb NOT NULL,

  state              text NOT NULL
    CHECK (state IN ('REQUESTED','REJECTED','ADMITTED','QUEUED','PREPARING',
                     'TRAINING','RESUMING','EVALUATING','EVAL_FAILED',
                     'CANDIDATE','PUBLISHED','BLOCKED','FAILED','EXPIRED')),
  state_since        timestamptz NOT NULL DEFAULT now(),
  rejection          jsonb,

  budget_auth_ref    jsonb NOT NULL,             -- S23 授权引用
  quota_permit_ref   jsonb,                      -- S22 permit
  gpu_seconds_consumed bigint NOT NULL DEFAULT 0,
  checkpoint_ref     text,                       -- 最近 checkpoint 的对象存储 key

  artifact_id        text,                       -- PUBLISHED 后
  evaluation         jsonb,
  requested_by       jsonb NOT NULL,
  trace_id           text,

  created_at         timestamptz NOT NULL DEFAULT now(),
  admitted_at        timestamptz,
  started_at         timestamptz,
  finished_at        timestamptz,

  CONSTRAINT tj_cmd_uniq UNIQUE (tenant_id, command_id)
);
CREATE INDEX tj_state_idx  ON training_job (tenant_id, state, created_at DESC);
CREATE INDEX tj_active_idx ON training_job (state, state_since)
  WHERE state IN ('QUEUED','PREPARING','TRAINING','RESUMING','EVALUATING');
```

### 5.2 数据集清单

```sql
CREATE TABLE dataset_manifest (
  dataset_id         text PRIMARY KEY,           -- 'ds_01J...'
  tenant_id          text NOT NULL,
  job_id             text NOT NULL,              -- 一个 manifest 属于一个 job
  mode               text NOT NULL CHECK (mode IN ('explicit','derived_from_run','derived_from_artifact_set')),
  source_ref         jsonb,                      -- derived 模式下的来源
  items_hash         char(64) NOT NULL,          -- 冻结后的摘要
  item_count         integer NOT NULL,
  excluded_count     integer NOT NULL DEFAULT 0,
  exclusions         jsonb NOT NULL DEFAULT '[]',-- [{artifact_id,reason,rights_state}]
  state              text NOT NULL DEFAULT 'DRAFT'
    CHECK (state IN ('DRAFT','FROZEN','SUPERSEDED')),
  frozen_at          timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX dm_job_idx ON dataset_manifest (job_id);

CREATE TABLE dataset_item (
  dataset_id         text NOT NULL REFERENCES dataset_manifest(dataset_id),
  artifact_id        text NOT NULL,
  artifact_service   text NOT NULL DEFAULT 'S28',
  content_hash       char(64) NOT NULL,

  rights_service     text NOT NULL,              -- 'S15' | 'S11' | ...
  rights_id          text NOT NULL,
  rights_revision    integer NOT NULL,
  rights_content_hash char(64) NOT NULL,
  rights_grant_kind  text NOT NULL,              -- 必须含 'training'
  rights_state_at_check text NOT NULL,           -- 'active'（被排除的不入库）
  rights_checked_at  timestamptz NOT NULL,

  ordinal            integer NOT NULL,
  PRIMARY KEY (dataset_id, artifact_id)
);
CREATE INDEX di_rights_idx ON dataset_item (rights_service, rights_id);

-- 被排除的项单独记录，因为它们也是证据
CREATE TABLE dataset_exclusion (
  dataset_id         text NOT NULL REFERENCES dataset_manifest(dataset_id),
  artifact_id        text NOT NULL,
  reason             text NOT NULL
    CHECK (reason IN ('REVOKED','SUSPENDED','EXPIRED','NO_TRAINING_GRANT',
                      'HASH_MISMATCH','MISSING','EXPLICITLY_EXCLUDED')),
  rights_ref         jsonb,
  PRIMARY KEY (dataset_id, artifact_id)
);
```

**`dataset_item` 的 `rights_grant_kind` 与 `rights_state_at_check` 是 NOT NULL**——没有授权的项在数据库层面就进不来。这是 §1.3 第 2 条的结构保证，不靠应用代码。

### 5.3 兼容性与产物

```sql
CREATE TABLE base_model_compatibility (
  compat_id          text PRIMARY KEY,
  tenant_id          text NOT NULL,
  artifact_id        text NOT NULL,
  base_service       text NOT NULL DEFAULT 'S24',
  base_id            text NOT NULL,
  base_revision      integer NOT NULL,
  base_content_hash  char(64) NOT NULL,
  verdict            text NOT NULL
    CHECK (verdict IN ('COMPATIBLE','INCOMPATIBLE','UNKNOWN')),
  reason             text,                       -- 'ARCH_CHANGED' | 'TOKENIZER_CHANGED' | ...
  checked_by         text NOT NULL DEFAULT 'S48',
  checked_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT bmc_uniq UNIQUE (artifact_id, base_id, base_revision)
);
CREATE INDEX bmc_base_idx ON base_model_compatibility (tenant_id, base_id, base_revision);

CREATE TABLE lora_artifact (
  artifact_id        text PRIMARY KEY,           -- 'lora_01J...'
  tenant_id          text NOT NULL,
  job_id             text NOT NULL,
  adaptation_type    text NOT NULL,
  target_service     text NOT NULL,
  target_id          text NOT NULL,
  target_revision    integer NOT NULL,

  storage_ref        text NOT NULL,              -- S28 的 artifact ref（不是 S48 的 checkpoint）
  weights_hash       char(64) NOT NULL,
  size_bytes         bigint NOT NULL,
  dataset_id         text NOT NULL,
  base_id            text NOT NULL,
  base_revision      integer NOT NULL,
  base_content_hash  char(64) NOT NULL,

  state              text NOT NULL DEFAULT 'CANDIDATE'
    CHECK (state IN ('CANDIDATE','PUBLISHED','BLOCKED','SUPERSEDED')),
  blocked_reason     jsonb,
  published_at       timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT la_weights_uniq UNIQUE (tenant_id, weights_hash)
);
CREATE INDEX la_target_idx ON lora_artifact (tenant_id, target_service, target_id, state);
CREATE INDEX la_base_idx   ON lora_artifact (tenant_id, base_id, base_revision, state);
```

`la_weights_uniq`：同一租户下相同权重 hash 只注册一次。若两次训练产出完全相同的权重（极小概率，但确定性训练可能），复用同一条 artifact。

### 5.4 训练额度视图

```sql
CREATE TABLE training_slot_usage (
  usage_id           text PRIMARY KEY,
  tenant_id          text NOT NULL,
  job_id             text NOT NULL,
  permit_ref         jsonb NOT NULL,             -- S22 permit
  gpu_seconds        bigint NOT NULL,
  resource_class     text NOT NULL DEFAULT 'gpu_second',
  started_at         timestamptz NOT NULL,
  recorded_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tsu_job_idx    ON training_slot_usage (job_id);
CREATE INDEX tsu_tenant_idx ON training_slot_usage (tenant_id, recorded_at DESC);
```

**这张表不是账本。** 它是"本服务上报给 S23 的用量记录"，权威在 S23。金额与余额永远以 S23 为准。同 S45 的纪律：不能变成第二权威。

### 5.5 幂等、Inbox、Outbox

```sql
CREATE TABLE idempotency_record (
  tenant_id      text NOT NULL,
  command_id     text NOT NULL,
  request_hash   char(64) NOT NULL,
  command_type   text NOT NULL,
  state          text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code  integer,
  response_body  jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE inbox (
  consumer    text NOT NULL,     -- 's48.rights' | 's48.base' | 's48.eval'
  event_id    text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
CREATE INDEX inbox_ttl_idx ON inbox (received_at);

CREATE TABLE outbox (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id         text NOT NULL,
  aggregate_id      text NOT NULL,              -- job_id
  aggregate_sequence bigint NOT NULL,   -- 状态迁移序号
  event_type        text NOT NULL,
  partition_key     text NOT NULL,      -- tenant_id || ':' || aggregate_id
  payload           jsonb NOT NULL,
  headers           jsonb NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  published_at      timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;
```

`outbox` 承载 `adaptation.training.completed`（以及 §4.4 的两个建议事件）。状态迁移与 outbox 写入在**同一本地事务**。

### 5.6 不建的表

| 不建 | 为什么 |
|---|---|
| `balance` / `quota_remaining` | S23/S22 是权威（AGENTS.md 第 18 行） |
| `current_rights_state` | 授权态属 S15/S11，S48 只有历史快照（§1.2） |
| `base_model_availability` | S24 是权威（§1.5） |
| `dataset_bytes`（素材副本） | 字节归 S28（§1.2） |
| `online_loaded_lora` | 在线加载归 S25/S26（§1.2） |
| `shared_dataset_pool`（跨租户共享） | E28/E31：跨租户共享训练数据是灾难，即使技术上可行 |

---

## 6. 幂等与并发

### 6.1 幂等键

```text
request_hash = sha256(JCS(body 去掉 command_id 与 request_hash))
```

`tj_cmd_uniq`（`tenant_id, command_id`）是数据库级保证。

### 6.2 并发场景

| 场景 | 期望 |
|---|---|
| 同 `command_id` 重复提交 | 回放同一 `job_id` |
| 两个 job 训练同一 target + 同一 base | **都允许**。它们会产出不同 LoRA，由客户选择接受哪个。S48 不做"同 target 只能有一个 job"的限制——那会阻碍对比实验 |
| 同一 job 的 cancel 与进度上报并发 | cancel 优先；进度上报在 `TRAINING` 之外被拒 |
| 授权撤销与训练**同时**发生 | 撤销事件到达时若 job 在 `TRAINING`，**不中断**（中断会浪费已消耗的 GPU），但完成后立刻 `BLOCKED` |
| 两个 runner 从同一 checkpoint 续 | 通过 checkpoint 行的 `FOR UPDATE` 保证只有一个续（§6.3） |

第三行是重要取舍：撤销不中断在训的任务。理由：中断不能撤回已花的钱，且可能产出半成品。但**产物不能发布**（§3.8）。这条要在客户文档里写明，否则客户会期待"撤销立刻停"。

### 6.3 GPU 队列的认领

```sql
SELECT job_id, checkpoint_ref
  FROM training_job
 WHERE state = 'QUEUED'
   AND tenant_id IN (SELECT tenant_id FROM tenant_training_enabled)
 ORDER BY created_at
 LIMIT 1
 FOR UPDATE SKIP LOCKED;
```

与 S08 §6.4 的 relay 同构。

**公平性**：`ORDER BY created_at` 会让大客户长期占位。实际实现应加**每租户并发上限**（默认 2 个小作业或 1 个大作业），超出的先排队，给其他租户留位。这是 E35 在训练侧的体现。

### 6.4 checkpoint 续跑

```sql
UPDATE training_job
   SET state = 'RESUMING', checkpoint_ref = $new
 WHERE job_id = $id
   AND state = 'TRAINING'
   AND checkpoint_ref = $expected_old;   -- CAS：防止两个 runner 同时续
```

**训练 runtime 崩溃后，job 不会自动续**。由 janitor 检测心跳超时（默认 10 分钟）后置为可续，再交给队列。这样避免"两个 runner 都以为自己该续"。

### 6.5 评测的并发

一个 job 同时只能有一个评测。评测是短任务，串行即可。若评测本身失败（基础设施问题）→ 可重试 1 次；再失败 → job `FAILED`（不是 `EVAL_FAILED`——那是指标不达标）。

---

## 7. 失败分支

### 7.1 复核阶段

| 场景 | 行为 | 对外 |
|---|---|---|
| 授权权威不可达 | 整个 job `REJECTED`，`reason=RIGHTS_UNVERIFIABLE` | 422 `RIGHTS_UNVERIFIABLE`（fail closed） |
| 某项授权 `revoked` | 排除该项，进 `dataset_exclusion` | 若剩余 ≥ `min_ratio` 则继续 |
| 剩余项不足 | `REJECTED` | 422 `EMPTY_DATASET_AFTER_FILTER` |
| S24 说不兼容 | `REJECTED` | 422 `BASE_MODEL_INCOMPATIBLE`（在花 GPU 之前） |
| holdout 与训练集有交集 | `REJECTED` | 422 `HOLDOUT_OVERLAP` |
| 数据集超上限 | `REJECTED` | 422 `DATASET_TOO_LARGE` |
| 素材 hash 与提交时不一致 | `REJECTED` | 422 `HASH_MISMATCH` |

### 7.2 准入阶段

| 场景 | 行为 |
|---|---|
| S23 授权无效 | `REJECTED`，`reason=BUDGET_AUTH_INVALID` |
| S22 无 permit | 保持 `ADMITTED`，重试（不拒绝——配额可能稍后释放） |
| `ADMITTED` 超过 24h 仍未准入 | `EXPIRED`，释放所有预占 |
| S22 宕机 | 保持 `ADMITTED`，退避重试 |

`ADMITTED` 与 `QUEUED` 的区别在这里：前者还在等 permit，后者有 permit 在等 GPU。混淆会让"配额不足"和"GPU 不足"看起来一样。

### 7.3 训练阶段

| 场景 | 行为 |
|---|---|
| GPU OOM | 若 `lora_rank` 高于配置的上限 → `FAILED` 并给建议；否则降 batch 重试 1 次 |
| checkpoint 写失败 | 重试 3 次；仍失败 → `FAILED`（不能无 checkpoint 跑长训练） |
| 训练进程崩溃 | 心跳超时 → `RESUMING` → 从 checkpoint 续 |
| 基座在训练中被 S24 退役 | **不中断**（已拿到的权重仍基于该 revision），完成后 `base_model_compatibility.verdict='INCOMPATIBLE'` + `BLOCKED` |
| 授权撤销 | 不中断，完成后 `BLOCKED`（§6.2） |
| 超时（超过 `max_training_hours`） | 强制停止 → `FAILED`，保留最后 checkpoint |

### 7.4 评测与接受

| 场景 | 行为 |
|---|---|
| 评测不达标 | `EVAL_FAILED`，保留 artifact 字节（可能有用）但标 `CANDIDATE`→`EVAL_FAILED` |
| 评测基础设施失败 | 重试 1 次；再失败 → `FAILED` |
| 人工否决 | `EVAL_FAILED`，`rejection` 记理由 |
| S28 注册失败 | `CANDIDATE` 状态保持，重试；不置 `PUBLISHED` |
| 接受时授权已变 | 拒绝接受，job → `BLOCKED` |

### 7.5 崩溃恢复

| 崩溃点 | 后果 | 恢复 |
|---|---|---|
| 复核中崩溃 | `REQUESTED` 滞留 | janitor 重做；幂等 |
| 数据固化中崩溃 | `PREPARING`，manifest 未 `FROZEN` | 重做；`items_hash` 由输入决定，确定 |
| 训练中崩溃 | 心跳停 | janitor 10min 后置可续 |
| 评测中崩溃 | `EVALUATING` 滞留 | 重跑评测 |
| 注册中崩溃（S28 已成功，本地未写） | 本地无 `artifact_id` | 重试时用 `weights_hash` 查重（S28 幂等），拿到同一 artifact |
| 发事件前崩溃 | outbox 有未发布的 | relay 重试 |

### 7.6 失败矩阵

| # | 场景 | 检测 | 行为 |
|---|---|---|---|
| F48-01 | 授权撤销 | revoke 事件 | 完成在训任务，产物 `BLOCKED` |
| F48-02 | 基座退役 | S24 事件 / 回源 | 产物 `BLOCKED` |
| F48-03 | 训练挤爆推理池 | GPU 池隔离指标 | **不可能**（物理隔离）；若共用则告警 |
| F48-04 | checkpoint 存储满 | 写失败 | `FAILED`，不静默 |
| F48-05 | 队列长期满 | 等待时长 | 429 `TRAINING_QUEUE_FULL` |
| F48-06 | 数据集解析出意外内容 | 人工确认 | 未确认前不进入 `PREPARING` |
| F48-07 | 同租户占满队列 | 每租户并发上限 | 排队，不阻塞他人 |

---

## 8. 可观测

### 8.1 指标

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `s48_jobs_total` | counter | `adaptation_type`, `final_state` | 作业结局分布 |
| `s48_job_duration_seconds` | histogram | `adaptation_type`, `state` | 各阶段耗时 |
| `s48_queue_wait_seconds` | histogram | `adaptation_type` | 排队时长 |
| `s48_queued_jobs` | gauge | `adaptation_type` | 积压 |
| `s48_gpu_seconds_total` | counter | `adaptation_type`, `plan` | 资源消耗 |
| `s48_gpu_utilization` | gauge | `pool` | 训练池利用率（与推理池分开） |
| `s48_rights_excluded_total` | counter | `rights_service`, `reason` | 授权复核结果 |
| `s48_rights_unverifiable_total` | counter | `rights_service` | 权威不可达 |
| `s48_eval_pass_rate` | gauge | `adaptation_type` | 训练质量趋势 |
| `s48_eval_metric` | histogram | `metric_name`（有限枚举） | 指标分布 |
| `s48_blocked_artifacts_total` | counter | `reason` | 撤销/退役影响 |
| `s48_checkpoint_failures_total` | counter | — | 存储健康 |
| `s48_resume_total` | counter | `result` | 续跑成败 |
| `s48_base_incompat_rejections_total` | counter | — | 训练前拦截效果（省下的 GPU 秒） |
| `s48_inference_pool_borrow_total` | counter | — | **必须恒为 0**（§3.9） |

**不进 label**：`artifact_id`、`job_id`、`target_id`、`tenant_id`。`metric_name` 是有限枚举（`identity_preservation` 等 4–6 个），不是自由文本。

### 8.2 追踪

```text
training.http.create
  ├─ s48.rights.check (×n，可能批量)
  ├─ s48.base.compat.check
  ├─ s48.dataset.freeze
  ├─ s48.admit (S22/S23)
  └─ s48.enqueue

training.runner (独立 trace)
  ├─ s48.checkpoint.write (周期)
  ├─ s48.progress.report
  ├─ s48.evaluate
  └─ s48.register (S28)
```

runner 的 trace 与创建请求分开——训练可能跑几小时。

### 8.3 审计（S45）

| 动作 | 内容 |
|---|---|
| `training.job.create` | actor / target / base / budget_auth_ref / trace_id |
| `training.job.reject` | reason / 具体哪一项复核失败 |
| `training.dataset.freeze` | `dataset_id` / `items_hash` / `item_count` / `excluded_count` |
| `training.job.start` / `complete` / `fail` | 状态迁移 + `gpu_seconds_consumed` |
| `training.artifact.publish` | `artifact_id` / `weights_hash` / `base_ref` |
| `training.artifact.block` | reason（撤销/退役）/ 影响的 `dataset_id` |

**不记**：训练超参全文（在 `training_job.hyperparams`，查询可得）、数据集内容、权重 URL。

### 8.4 告警

| 级别 | 条件 |
|---|---|
| P1 | `s48_inference_pool_borrow_total > 0`（资源隔离被破坏） |
| P1 | `s48_rights_unverifiable_total` 突增（授权服务故障） |
| P2 | `s48_eval_pass_rate` 连续 3 次 < 基线一半（数据或超参退化） |
| P2 | `s48_checkpoint_failures_total` > 0 持续 |
| P2 | `s48_queued_jobs` > 100 持续 30min |
| P3 | 单租户占满队列 > 6h |

---

## 9. 测试清单

### 9.1 授权链（E30 / E39）

| # | 用例 | 期望 |
|---|---|---|
| T48-01 `[E30]` | 数据集中含已撤销音色的素材 | 该项被排除；若剩余不足 → `REJECTED` |
| T48-02 `[E30]` | 音色 `suspended`（技术下架，非撤销） | **不排除**：技术下架不等于法律撤销（§1.4） |
| T48-03 `[E30]` | 授权含"可用于生成"但不含"可用于训练" | 排除，`reason=NO_TRAINING_GRANT` |
| T48-04 `[E30]` | 授权服务不可达 | 整个 job `REJECTED`（fail closed），不是跳过 |
| T48-05 `[E30]` | 训练完成、接受后授权被撤销 | artifact → `BLOCKED`；发 `lora.blocked`；S25 不能路由 |
| T48-06 `[E30]` | `BLOCKED` 后重新授权 | **仍 `BLOCKED`**，不能复活（§3.8） |
| T48-07 `[E30]` | 撤销发生在 `TRAINING` 中 | 不中断；完成后 `BLOCKED`（§6.2） |
| T48-08 `[E39]` | `dataset_manifest` 可回答"这批数据依据哪些授权" | 逐项 `rights_ref` 可查 |
| T48-09 `[E39]` | 训练审计链 | S45 里有 create/freeze/start/complete 全链 |
| T48-10 `[E30]` | S48 的存储审查 | 无任何供应商凭据（BYOK 归 S24/S26） |

### 9.2 数据集与复核

| # | 用例 | 期望 |
|---|---|---|
| T48-11 | `explicit` 模式 10 项全部 active | 全部入 `dataset_item`，`items_hash` 稳定 |
| T48-12 | 同输入两次解析 | 同一 `items_hash`（确定性） |
| T48-13 | 排除 1/10（10%） | 通过（`min_ratio=0.9` 边界） |
| T48-14 | 排除 2/10 | `REJECTED` |
| T48-15 | `derived_from_run` 模式 | 返回完整 `resolved_items` 供确认 |
| T48-16 | holdout 与训练集交集 | `REJECTED` `HOLDOUT_OVERLAP` |
| T48-17 | 数据在复核后、固化前变了 | `REJECTED` `HASH_MISMATCH` |
| T48-18 | `dataset_item` 直插无 `rights_ref` | 数据库拒绝（NOT NULL） |

### 9.3 基座兼容（E01 / C11-4）

| # | 用例 | 期望 |
|---|---|---|
| T48-19 | 指定 base revision + hash | 训练记录两者 |
| T48-20 | 不指定的 revision（"最新"） | 400 `MALFORMED_REQUEST` |
| T48-21 | S24 说不兼容 | 训练前 `REJECTED`（省 GPU） |
| T48-22 | 基座在训练中退役 | 完成后 `BLOCKED`，`verdict=INCOMPATIBLE` |
| T48-23 | 兼容性记录被当作"当前"读 | API 带 `__hints.authority.current_compatibility=S24`（§1.5） |

### 9.4 准入与资源（E14 / E15 / E35）

| # | 用例 | 期望 |
|---|---|---|
| T48-24 `[E14]` | S23 授权成功、S22 permit 失败 | 保持 `ADMITTED`，不占 GPU；S23 无扣费 |
| T48-25 `[E15]` | permit 已 mark、S23 确认超时 | 不开始训练；Saga 恢复后明确 |
| T48-26 `[E35]` | 训练池满时推理正常 | 推理延迟不变（物理隔离） |
| T48-27 `[E35]` | 单租户提交 20 个 job | 受每租户并发上限约束，其他租户不被饿死 |
| T48-28 | `ADMITTED` 超 24h | `EXPIRED`，释放预占 |
| T48-29 | `s48_inference_pool_borrow_total` | 恒为 0 |

### 9.5 训练与恢复（E13 / E33）

| # | 用例 | 期望 |
|---|---|---|
| T48-30 `[E13]` | 训练中 kill | 心跳超时 → `RESUMING` → 从 checkpoint 续 |
| T48-31 `[E13]` | checkpoint 写失败 | `FAILED`，不静默续 |
| T48-32 `[E13]` | 两 runner 同时续同一 checkpoint | CAS 保证只有一个 |
| T48-33 `[E33]` | runner 全部停机后恢复 | 从 checkpoint 续，不重头 |
| T48-34 | 超时 | `FAILED`，保留最后 checkpoint |

### 9.6 评测与接受

| # | 用例 | 期望 |
|---|---|---|
| T48-35 | 指标达标 | `CANDIDATE` |
| T48-36 | 指标不达标 | `EVAL_FAILED`（不是 `FAILED`） |
| T48-37 | 评测基础设施失败 | 重试 1 次；再失败 → `FAILED` |
| T48-38 | 人工否决 | `EVAL_FAILED`，记理由 |
| T48-39 | 接受后 S28 注册失败 | 保持 `CANDIDATE`，不置 `PUBLISHED` |
| T48-40 | 同权重 hash 二次注册 | 复用同一 `artifact_id` |

### 9.7 与资产服务的建议（C11-4 / S15）

| # | 用例 | 期望 |
|---|---|---|
| T48-41 | `PUBLISHED` 后 S11 收到 `training.lora.registered` | S11 只写建议，**不自动接受** |
| T48-42 | `voice_clone` 完成 | S15 收到建议，不自动接受 |
| T48-43 | S48 尝试直接写 S11/S15 的资产 | **不可能**：无跨服务 DB 写权限 |

### 9.8 跨租户与升级

| # | 用例 | 期望 |
|---|---|---|
| T48-44 `[E28]` | 租户 B 查 A 的 job / dataset / artifact | 404，不含 A 的任何标识 |
| T48-45 `[E28]` | 跨租户共享数据集 | 结构上不存在（§5.6） |
| T48-46 `[E31]` | 训练数据的地域 | 数据集与 checkpoint 在 tenant `home_region` |
| T48-47 `[E01]` | 只 import 契约 SDK | 构建通过 |
| T48-48 `[E01]` | 回滚到旧版本 | 新字段可选，旧版本可读写 |
| T48-49 | 未订购 S48 的租户 | 403 `TRAINING_NOT_ENABLED` |
| T48-50 `[E40]` | 客户验收：一次完整训练可追溯 | 数据来源、授权、基座、评测、产物全部可查 |

**关于 E 编号的说明**：docs/16 的 E01–E40 里**没有专门的"训练"条目**。最接近的是 E39（"审计/保留/删除/训练数据"——明确提到训练数据）、E30（授权撤销）、E10（"正史分支与候选抽取…回滚留历史"，与"候选不自动接受"同构）。本文的测试按这三条映射，其余映射到通用条目（E13/E28/E31/E35）。**这是一个真实的缺口**，见 C48-1。

---

## 10. 与上下游服务的接口约定

### 10.1 对 S15 / S11 / S12 / S13 / S14（授权权威）

| 约定 | 内容 |
|---|---|
| 查询授权 | 每项一次，返回 `state` + `grant_kind` + `content_hash` |
| 撤销事件 | 订阅 `voice.authorization.revoked` 等；收到后影响评估（§3.8） |
| **不缓存授权态超过 1 小时** | 训练启动前的复核必须是新鲜的 |
| **fail closed** | 不可达 → 拒绝训练 |
| 建议事件 | `training.lora.registered` / `training.voice_clone.ready` 只写建议，不自动接受 |

**`grant_kind` 的区分（生成 vs 训练）是 S48 能工作的前提。** 若某资产的授权模型没有这个区分，S48 必须**拒绝使用该资产训练**（默认不放行），而不是"假设允许"。这是 C48-3。

### 10.2 对 S24（模型注册）

| 约定 | 内容 |
|---|---|
| 查基座 | `GET /models/{id}?revision=&content_hash=` |
| 兼容性 | S24 提供 `compatible_base_changes(from_rev, to_rev)` 或等价能力；S48 训练时记录，S25 路由时实时查 |
| 基座退役 | 订阅事件，触发 `BLOCKED` |
| **S48 不写 S24 的模型表** | 只通过 S24 的公开接口注册 artifact 的兼容性记录 |

### 10.3 对 S28（制品）

| 约定 | 内容 |
|---|---|
| 取训练素材 | 用不可变引用；训练时不做"当前状态"查询 |
| 注册 LoRA 权重 | `POST` 到 S28，拿 `artifact_id`；按 `weights_hash` 幂等 |
| 删除 | S48 **不删除** S28 的 artifact；删除走 S28 的删除编排（含 `REFERENCE_CHECK` 会问 S48"这 artifact 还被训练引用吗"） |

最后一条要求 S48 提供 `retention-check` 端点（与 S45 §10.3 同构）：

```text
GET /retention-checks?kind=lora_artifact&id=<artifact_id>
→ { "referenced": bool, "reason": "DATASET_SOURCE"|"PUBLISHED_LORA"|null, "checked_at": ... }
```

**S28 删除的是"训练数据"，不是"LoRA"**。LoRA 一旦 `PUBLISHED`，它的字节就不该因为"训练数据集里的某张图被删"而消失——除非 S48 的 `BLOCKED` 流程触发。这个区分要写清楚。

### 10.4 对 S22 / S23（配额与钱）

| 约定 | 内容 |
|---|---|
| permit | `modality='training'`, `resource_class='gpu_second'` |
| 用量上报 | `training_slot_usage` 定期上报给 S23；S23 是账本 |
| **不预扣、不缓存余额** | 训练前查 S23 的授权，训练中不本地记账 |
| 超时释放 | `EXPIRED` 时释放 permit |

### 10.5 对 S26 / S25（在线推理）

**S48 与在线推理的唯一交集是"注册"与"事件"。**

- S48 不调用 S26（训练不走模型网关）。
- S48 不挂载任何 LoRA（那是 S25 的路由 + S26 的加载）。
- S48 发 `lora.blocked`，S25 消费后禁止路由。

### 10.6 对 S45（审计）

见 §8.3。训练相关审计是 E39 "训练数据"的直接证据。

### 10.7 对 S44（评测口径）

S48 产出的是**训练侧指标**；S44 做跨服务的评测口径与对比。两者的数值可能不一致（S44 用自己的 rubric 重跑），S48 不声称自己是最终评价。`evaluation.baseline_ref` 让 S44 能复现对比。

---

## 11. 契约缺口（如实报告，不自行补）

| # | 缺什么 | 影响 | 需要谁改 |
|---|---|---|---|
| **C48-1** | `docs/16` 的 E01–E40 里**没有专门的训练验收条目**；只有 E39 顺带提到"训练数据" | S48 的验收无基准；`service.yaml` 的 acceptance（"LoRA基座/hash与数据来源可追溯"）没有对应的 E 编号 | `docs/16` 补一条，或明确并入 E39 |
| **C48-2** | 无 `rights-ref.v1` schema；授权引用的形状（`grant_kind` 概念）不存在于任何契约 | §2.3 是本文提案；跨服务的授权引用无法统一 | `contracts/` 新增 |
| **C48-3** | `S15`/`S11`–`S14` 的授权模型**没有"生成 vs 训练"的区分**（至少没有在契约里） | 无法判断一份授权是否覆盖训练；S48 只能默认拒绝或默认允许，两者都不对 | S15/S11–S14 的授权 schema |
| **C48-4** | `event-topics.v2.json` 里 `drama.adaptation-training.v2` 只登记 `adaptation.training.completed`；`training.lora.registered` / `lora.blocked` 等建议事件未登记 | S11/S15 的"建议"链路无法工作；`BLOCKED` 无法通知 S24/S25 | `contracts/event-topics.v2.json` |
| **C48-5** | `openapi.v2.yaml` **没有 `/training-jobs`、`/datasets/{id}`、`/adaptation-artifacts/{id}` 任何一条路径**；`service.yaml` 声明了前两条中的 POST 与 GET | **阻塞实现** | `contracts/openapi.v2.yaml` |
| **C48-6** | `service.yaml` 的 `depends_on: [S23, S22, S28, S11, S24]` **不含 S15**，但 `voice_clone` 必须有 S15 的授权 | 部署依赖不全；voice_clone 的授权复核可能漏 | catalog |
| **C48-7** | `dataset-manifest.v1` 与 `evaluation-report.v1`（训练侧）无 schema | 数据集的可追溯性（E39）无契约载体 | `contracts/` |
| **C48-8** | `hyperparams.profile` 的命名档位（`character_default_v3`）无定义 | 每个部署要自己发明；跨环境不可复现 | `contracts/` 或 S48 内部规范 |
| **C48-9** | S24 是否提供"基座 revision 间兼容性"的查询接口未定义（S48 需要它记录，S25 需要它判定） | `LORA_INCOMPATIBLE`（S25 行 19）的判定依据不明 | S24 方案 |
| **C48-10** | S48 的 `retention-check`（§10.3）在 S28 的引用方清单里没有（与 C28-5 同类） | S28 删除训练数据时可能漏查 LoRA 引用 | S28 §4.8 + catalog |
| **C48-11** | S10 的开放问题"`binding_usage` 分支隔离取决于 S47 的资产页需求"会牵动 S48 | 若资产页要显示"这个 LoRA 用在哪些分支"，S48 需要按分支索引 | S10 + S47 |

**C48-1、C48-3、C48-5 是阻塞级。** C48-3 尤其危险：它不阻塞编译，只会让训练数据合规在运行时静默失效。

---

## 12. 未决

1. **MCP / Agent 是否允许发起训练。** 本文建议禁止（§2.4），理由是重度且不可逆。若产品要"Agent 自动调优"，需要另一套更严的审批。

2. **训练中授权撤销是否应当中断。** 本文选"不中断、完成后 BLOCKED"（§6.2），理由是已消耗的 GPU 退不回来。但客户可能期待"立即停"。需要法务与产品共同定。

3. **`min_ratio` 默认 0.9 是否合适。** 太严会让"一两个素材过期"就整体拒绝；太松会让数据集质量悄悄下降。可能需要按 `adaptation_type` 分档。

4. **`derived_from_run` 是否该开放。** 它最方便，也最容易带进未授权素材。本文保留但要求完整 `resolved_items` 确认。若实际使用中问题多，可能应该下线。

5. **训练侧评测指标的阈值。** `identity_preservation > 0.85` 这类数字本文没有给出。它们应该来自客户验收与 evals 数据集，不是实现时的拍脑袋。

6. **`BLOCKED` 的 artifact 字节何时删。** 本文说"删除走 S28"（§3.8），但没说**是否应该**删。保留可能违反客户的数据删除承诺；删除会让"万一重授权"无法恢复（虽然本文说不能复活）。需要合规裁决。

7. **S48 的 GPU 池规模。** docs/03 只有在线推理的规划（52.08 video req/s），没有训练池。W5 的训练需求必须单独估。

8. **是否需要"训练数据集共享库"（跨 job 复用）。** 目前一个 manifest 属于一个 job。若客户反复训练同一角色，重建数据集是浪费。但共享会引入"集 A 更新是否影响 job B"的复杂性问题。本文倾向不共享（`job_id NOT NULL`），保持简单。

---

## 13. 明确不做的

- **不把 LoRA 当通用参数。** 兼容性按 `(base_id, base_revision, content_hash)` 记录（docs/10、§1.5）。
- **不绕过授权复核。** 无 `rights_ref` 的数据项数据库层面进不来（§5.2）。
- **不做在线模型加载。** 归 S25/S26（§10.5）。
- **不预扣/记账。** 钱在 S23，配额在 S22（§10.4）。
- **不直连供应商训练 API 绕过 S26 的模式。** 训练走 S48 自己的 GPU 池；若未来用外部训练服务，必须走 S24 的模型注册与 S26 的准入模式，不能另开一条绕过付费的路。
- **不自动接受训练产物。** `CANDIDATE → PUBLISHED` 需人工或策略（§3.7），且对 S11/S15 只发建议。
- **不让 `BLOCKED` 复活。** 撤销后要重训（§3.8）。
- **不共用 GPU 池。** 训练与推理物理隔离（§3.9）。
- **不做跨租户数据集共享。** 即使技术上可行（§5.6）。
- **不在 S48 里存素材字节。** 归 S28（§1.2）。
