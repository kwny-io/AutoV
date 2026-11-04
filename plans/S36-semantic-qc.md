# S36 · 视觉语义与时序质检 —— 实现方案

状态：`design_only`。QCReport v2 的规范在 [S35 §2](S35-technical-qc.md)，本文只写 S36 的扩展与边界，不复述 envelope。

---

## 1. 职责

S36 回答一个问题：**画面和我们绑定给它的一切对得上吗？**

- 对 `ShotSpec` 对得上吗（景别、机位、运镜、人物位置、动作节拍）
- 对资产 binding 对得上吗（角色身份、服装 revision、道具 revision）
- 对 S17 给的**预期状态**对得上吗（伤势、服装破损、道具归属、知情范围的可视化后果）
- 自己内部前后一致吗（跨镜连续性、轴线、背景突变）

S36 不回答：这个故事逻辑对不对、伏笔该不该收、谁该知道什么。那是 S17。

---

## 2. S17 / S36 边界（硬约束）

这是本方案最容易做错的地方，所以先定义清楚，再谈实现。

### 2.1 一句话划界

> **S17 只看"期望值"和它们的来源，S36 只看"像素"。**
> 凡是判定需要读像素的，S36 做；凡是判定需要"这个角色此刻应该是什么状态"的，S17 给。
> 两者都不许自己算另一半。

### 2.2 数据流向

```text
S16 正史 ──▶ S17 连续性校验 ──expected_state_set──▶ S36 语义质检 ──▶ QCReport
                    │                                    ▲
                    │ ContinuityReport                   │
                    └──────────────▶ S39 ◀───────────────┘
                                       │
                                  归并去重（§2.5）
```

S17 的输出里有两类东西：

| S17 产出 | 给谁 | 例子 |
|---|---|---|
| **ContinuityReport**（跨集矛盾，自己的 defect 结论） | 直接给 S39 | "第 3 集第 2 镜的钥匙归属与第 2 集正史冲突" |
| **expected_state_set**（机器可读的期望，供 S36 比对） | 给 S36 | `{character: char_linlan, predicate: left_hand_injury, value: bandaged, valid_range: [ep2_shot_4, ep5_end]}` |

S36 拿到 `expected_state_set` 后做的是**视觉验证**：画面上她的左手是不是包扎的。它不判断"第 2 集受伤了所以第 3 集该包扎"——那是 S17 已经算完的。

### 2.3 职责矩阵

| 现象 | S17 | S36 | 谁出 defect |
|---|---|---|---|
| 同一镜内角色脸在第 2.3 秒变了 | — | ✅ 看帧 | S36 |
| 一个角色在第 3 镜穿了第 1 镜的裙子 | — | ✅ 比对 binding revision | S36（`costume`） |
| 正史说"第 2 集起穿红裙"，第 5 集穿蓝裙 | ✅ 出 expected | ✅ 比对 | **S17 出**（`continuity`），S36 只出 `costume` |
| 画面里 3 个人，ShotSpec 说 2 个 | ✅ 校验 ShotSpec vs 剧本 | ✅ 数人 | S36 |
| 角色左手包扎，但第 2 集没受伤 | ✅ 出 expected（未受伤） | ✅ 看画面 | **S17 出**（`continuity`） |
| 手指数目不对 | — | ✅ | S36（`hands`） |
| 角色把钥匙交给 A，正史说交给了 B | ✅ 出 expected | ✅ 看画面 | **S17 出** |
| 同一场景两个镜头的轴线反了 | ✅ 有 shot 间关系 | ✅ 看画面 | S36（`continuity`，`check_id=VIS.AXIS.SHOT_CROSSING`） |
| 镜头说"仰拍"实际平拍 | ✅ 校验 DSL 内部一致 | ✅ 看画面 | S36（`camera_adherence`） |
| 闪回镜头的光线/服装与"当下"不同 | ✅ 知道这是闪回 | ✅ 看画面 | 都不出——S17 标记 `legal_exception`，S36 收到后不判 |
| 角色"不知道对方身份"但表情像认识 | ✅ 出 expected | — | S17 |
| 合法特效（变身、魔法光效） | ✅ 标记例外 | ✅ 收到例外后不判 | 都不出 |

关键：**矩阵里每一行恰好有一个主体**。凡是两个 ✅ 的行，S17 是"出结论"的一方，S36 是"出证据"的一方，S39 归并时以 S17 为准（见 §2.5）。

### 2.4 三个必守契约

1. **S36 不得从 S16 直接取正史**。`service.yaml` 的 `depends_on` 是 `S28, S17, S26`——没有 S16。S36 只能吃 S17 给的 `expected_state_set`，若 S17 没给，S36 把对应 check 标 `not_applicable`/`dependency_unavailable`，绝不自己去查正史。
2. **S17 不得读像素**。S17 的 `depends_on` 是 `S16, S06, S10, S28`——有 S28 是为了读 artifact manifest 元数据（不是解码画面）。S17 若需要画面证据，只能请 S36 提供，不能自己抽帧。S17 的 `ContinuityReport` 里引用 S36 的 `evidence_artifact_ids` 是合法的。
3. **合法例外由 S17 声明，不由 S36 猜测**。闪回、变身、妆造推进、道具损坏这些"看起来不一致但其实对"的情况，S17 在 `expected_state_set` 里带 `legal_exceptions[]`。S36 收到后：命中例外的帧不产生缺陷，但必须在 `check_manifest` 记 `not_applicable` + `reason_code=no_such_content`（或新增 `legal_exception`，需改 schema），并**保留原始观测**在 dimension 里以便审计。S36 自己猜"这可能是特效"是非法的——docs/11 §2 "合法变形特效与妆造例外在报告输入明确"。

### 2.5 去重：S39 用指纹归并

同一条缺陷可能同时出现在 S17 的 ContinuityReport 和 S36 的 QCReport。S39 的归并规则：

```text
归并键 = (tenant, series, branch, shot_lineage_id, defect_fingerprint)
主来源优先级 = S17（期望更强） > S36（观测） > S35（测量）
```

指纹算法在 [S35 §2.4](S35-technical-qc.md) 里，S17 的 report 若没有 `defect_fingerprint`，S39 用同一函数从 S17 的 defect 算出来再比。这样"钥匙归属"这一条在 S17 和 S36 两边算出同一个指纹，自动合并，修复动作取并集。

**不许留空的兜底**：如果某类检查两边都没覆盖，S39 在 RepairPlan 里标 `coverage_gap` 并转人审。S39 的 ingest 会对照 `check-registry` 里标记 `owner` 的检查项做覆盖断言（见 [S39 §4.3.2](S39-repair-planning.md)）。

---

## 3. 流水线

```text
POST /semantic-checks
  │
  ├─ 1. 上下文 + artifact 校验；取 ShotSpec、binding set、S17 expected_state_set
  │      └─ S17 不可达 → 所有依赖期望的 check 记 dependency_unavailable → abstain
  ├─ 2. 采样规划（§4）：按镜头类型选窗口、抽帧、抽段
  ├─ 3. 本地检测器（无模型，CPU/GPU 池）
  │      face_detect / pose / hand / person_count / shot_boundary / color_hist / OCR
  ├─ 4. VLM 评估（走 S26）：
  │      for each window: vision_qc(prompt, frames, expected, allowed_vocab)
  │      └─ 每次调用 = 一个 S26 generation job，budget_scope 独立
  ├─ 5. 双评估器交叉验证（§6）：高风险 check 跑第二个 evaluator
  ├─ 6. 证据固化：关键帧、bbox 裁剪、观测 JSON、模型原始响应 → S28
  ├─ 7. ReportValidator（含 §2 边界断言）→ decide()
  └─ 8. 落库 + Outbox + quality.semantic.completed
```

### 3.1 为什么第 3 步在前

本地检测器能给出 `person_count`、`bbox`、`face_embedding_distance`、`shot_boundary` 这类确定性信号。先跑它们，再把结果**作为结构化提示**塞给 VLM（而不是让 VLM 从零猜）：

- 降低 VLM 幻觉：让模型判断"这个 bbox 里的人和 char_linlan 像不像"，而不是"这个镜头里有谁"。
- 降低费用：VLM 只需要看命中的窗口。
- 让 abstain 有意义：本地检测器失败（无 face）时，VLM 即使给结论也不可信，直接标 `not_applicable`。

### 3.2 VLM 调用必须走 S26

```json
POST /generation-jobs  (S26)
{
  "modality": "understanding",
  "idempotency_key": "qc:s36:{artifact_sha256}:{check_id}:{window_idx}:{evaluator_version}",
  "input": {
    "model_role": "qc_vision_evaluator",
    "images": [ {"data_ref": "S28:…"} ],
    "prompt_template_ref": {"service_id":"S20","id":"tpl_qc_identity","revision":3,"content_hash":"…"},
    "response_schema_ref": "contracts/quality/vlm-response.v2.schema.json",
    "allowed_vocab": { "type": [...14 值...], "severity": [...4 值...] }
  },
  "budget_scope_id": "budget_qc_{episode_id}",
  "deadline": "..."
}
```

三条纪律：

1. **S36 没有供应商凭证**。只有 S26 的 job_id；任何直连都是设计错误（AGENTS.md "不能绕过 S26 直连模型"）。
2. **QC 有独立预算 scope**。返工要花预算（docs/12），质检本身也要。`budget_scope_id` 与生产生成分开，否则质检费用会挤占生成预算且无法归因。docs/12 §3 明确要求经营报表分列 "QC/VLM"。
3. **`response_schema_ref` 是强制的**。模型输出必须能被 S26 的结构校验挡下来；不合法的响应计入该 check 的 `evaluator_error`，不是我方"尽力解析"。

### 3.3 超时与降级阶梯

```text
VLM 调用超时（deadline 到）
  ├─ 1 次有界重试（S26 负责，network retry 不计业务 attempt）
  ├─ 仍超时 → 该 check status = failed, reason_code = evaluator_timeout
  └─ 该 check 归入 abstain.failed_check_ids

本地检测器失败（OOM/异常）
  ├─ 该 check status = failed, reason_code = evaluator_error
  └─ 归入 abstain
```

**永不发生**：超时后"按 minor 处理"或"跳过继续判 pass"。docs/16 E25 与 docs/11 §3 都要求 abstain。

---

## 4. 采样策略

`sampling_policy_version` 必须随报告记录，因为换采样策略等于换了一个评估器。

| 镜头类型 | 窗口规划 | 抽帧 | 理由 |
|---|---|---|---|
| `dialogue` | 每 1.0s 一个窗口 | 窗口首/中/末 3 帧 | 口型交给 S37，S36 只查身份/服装/手部 |
| `action` | 每 0.5s 一个窗口 | 5 帧 + 运动峰值帧 | 动作/肢体出问题概率高 |
| `camera_motion` | 每 1.5s 一个窗口 | 4 帧 | 运镜遵循 |
| `vfx` | 每 1.0s 一个窗口 | 4 帧 | 例外多，需更多上下文 |
| `establishing` | 每 2.0s 一个窗口 | 2 帧 | 少人、少动作 |
| `transition` | 切点前后各 0.5s | 切点帧 + ±3 帧 | 边界连续性 |

`planned_windows` 由上表算出，`temporal_windows_evaluated` 是实际完成的。`coverage_ratio` 低于 policy 下限 → abstain。

**连续窗口**（service.yaml: "连续窗口抽检"）：同一类 check 在相邻窗口的结果要做平滑，单窗口孤立命中不直接升级为 blocker，需要连续 ≥2 窗口命中才升级。这条用于压制 VLM 单帧抖动，阈值记在 `check-registry` 的 `promotion_windows`。

---

## 5. 检查项注册表（S36，前缀 `VIS.`）

严重度上限由 registry 固定；VLM 只能上提不能下压。

### 5.1 身份与形象

| check_id | type | 严重度 | 判定 | 依据 |
|---|---|---|---|---|
| `VIS.IDENTITY.FACE_DRIFT` | identity | **blocker** | 窗口内人脸嵌入与 char 参考集距离 > τ | 本地 embedding + VLM 确认 |
| `VIS.IDENTITY.FACE_SWAP_WITHIN_SHOT` | identity | **blocker** | 同一 bbox 轨道的人脸嵌入在窗口间跳变 | 轨道跟踪 |
| `VIS.IDENTITY.BODY_PROPORTION` | identity | major | 体型/身高比例与 CharacterProfile 偏差超阈值 | pose |
| `VIS.IDENTITY.OUTFIT_REVISION` | costume | major | 服装与 ShotSpec binding 的 costume revision 视觉不符 | 颜色直方图 + VLM |
| `VIS.IDENTITY.ACCESSORY_MISSING` | costume | major | binding 指定的配饰缺失 | VLM |
| `VIS.IDENTITY.ACCESSORY_EXTRA` | costume | minor | 出现 binding 未声明的显著配饰 | VLM |

### 5.2 身体与手部

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `VIS.ANATOMY.LIMB_COUNT` | anatomy | **blocker** | 四肢/手指数量异常且置信度 > 0.75 |
| `VIS.ANATOMY.JOINT_ARTIFACT` | anatomy | major | 关节反向/扭曲超过 `pose_artifact_threshold` |
| `VIS.HANDS.FINGER_DEFORM` | hands | **blocker** | 手部特写窗口手指结构崩坏 |
| `VIS.HANDS.OCCLUSION_ARTIFACT` | hands | major | 手与物体交界处融合/穿透 |
| `VIS.HANDS.INTERACTION` | hands | minor | 手握持方式与动作节拍不符 |

手部只有在**手的像素面积 ≥ `hand_min_area_ratio`**（默认 2%）时才判；远景小手不判，记 `not_applicable`。这条直接对应 docs/16 E24 的"合法变形特效不误判"——远景手部模糊不是缺陷。

### 5.3 人数与空间

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `VIS.SPACE.PERSON_COUNT` | continuity | major | 画面人数 ≠ ShotSpec 声明（含背景模糊人物容差 ±1） |
| `VIS.SPACE.RELATIVE_POSITION` | continuity | major | 人物左右/前后关系与分镜描述相反 |
| `VIS.SPACE.PROP_STATE` | prop | major | 道具开合/损坏状态与 S17 `expected_state_set` 不符 |
| `VIS.SPACE.PROP_OWNERSHIP` | prop | major | 道具在 S17 期望的角色手上以外的人手上 |
| `VIS.SPACE.BACKGROUND_JUMP` | continuity | major | 同场景前后镜头背景结构突变（直方图 + 特征点匹配 < τ） |

### 5.4 机位与动作

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `VIS.CAMERA.SHOT_TYPE` | camera_adherence | major | 景别与 ShotSpec 不符（远景被拍成特写等） |
| `VIS.CAMERA.ANGLE` | camera_adherence | major | 机位角度（仰/俯/平）不符 |
| `VIS.CAMERA.MOTION` | camera_adherence | major | 运镜方向/类型不符（推/拉/摇/跟） |
| `VIS.ACTION.BEAT_MISSING` | action_adherence | major | ShotSpec 声明的节拍未出现 |
| `VIS.ACTION.BEAT_EXTRA` | action_adherence | minor | 出现未声明的显著动作 |
| `VIS.ACTION.TIMING` | action_adherence | minor | 节拍发生时间与分镜节拍表偏差 > 0.5s |

### 5.5 时序与跨镜

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `VIS.TEMPORAL.FLICKER` | temporal | minor | 亮度/色彩逐帧跳变超阈值 |
| `VIS.TEMPORAL.MORPH_ARTIFACT` | temporal | major | 形体在相邻窗口间非物理形变 |
| `VIS.TEMPORAL.BOUNDARY_POP` | temporal | major | 切点前后 3 帧内画面元素突变（本应连续） |
| `VIS.CONTINUITY.SHOT_BOUNDARY` | continuity | major | 与前一镜的接续关系（视线、光线、动作连续）断裂 |
| `VIS.CONTINUITY.AXIS_CROSSING` | continuity | **blocker**（对白场） | 越轴且 ShotSpec 未声明越轴意图 |
| `VIS.CONTINUITY.LIGHT_DIRECTION` | continuity | minor | 同场景光源方向与前一镜不符 |
| `VIS.SAFE_AREA.TEXT_OVERLAP` | subtitle | major | 烧录文字/图形进入字幕安全区 |

### 5.6 S17 期望校验

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `VIS.STATE.EXPECTED_VISIBLE` | continuity | major | S17 期望"可见某状态"（包扎、污渍），画面未呈现 |
| `VIS.STATE.UNEXPECTED_VISIBLE` | continuity | major | 画面呈现了 S17 明确排除的状态 |
| `VIS.KNOWLEDGE.SCOPE_RENDER` | — | 不存在 | 知情范围**不由 S36 判**；S17 全权负责。此行为“不许留空”的显式记录 |

最后一行是刻意的：把"我们不查这个"写进注册表，S39 的覆盖断言才有地方读。

---

## 6. 双评估器与冲突

高风险 check（全部 blocker 项 + `VIS.IDENTITY.*`）默认双评估：

```text
primary   = VLM A (model_snapshot_a)
secondary = VLM B (model_snapshot_b)   // 不同厂商或不同代际
agreement = (type, severity_bucket, time_bucket) 逐条比对
disagreement_rate = 1 - Jaccard(primary_defects, secondary_defects)
```

- `disagreement_rate ≤ tau_disagreement`（0.30）→ 取严重度高者，`confidence = 0.5*(c_a + c_b)`。
- `disagreement_rate > tau_disagreement` → `decision=human_review`，`decision_reason` 记两端各自结论；不平均、不投票。
- 一端超时 → 不得用另一端的结果冒充；`abstain(reason_code=evaluator_timeout)`。

为什么是 0.30 而不是更严：两次 VLM 调用在时间桶边界上天然会抖，阈值太严会让 human_review 泛滥。0.30 是起点，需在 evals 里回归校准（docs/15）。

**不许自己做仲裁 LLM**。用第三个模型投票会把"两个模型谁对"变成"三个模型谁对"，成本涨而可靠性不涨。

---

## 7. 合法例外

S17 在 `expected_state_set` 里给 `legal_exceptions[]`：

```json
{
  "exception_id": "exc_flashback_ep3",
  "kind": "flashback",
  "applies_to": { "shot_ids": ["shot_3_2"], "time_ranges": [{"start": 0, "end": 4.0}] },
  "suppresses_check_ids": ["VIS.IDENTITY.OUTFIT_REVISION", "VIS.CONTINUITY.LIGHT_DIRECTION"],
  "declared_by": { "service_id": "S17", "ref_id": "cont_...", "ref_revision": 7 }
}
```

S36 收到后：

1. 命中 suppress 范围的 check → `status=not_applicable`，`reason_code` 用新的 `legal_exception`（需扩 schema 枚举，见 §11）。
2. **原始观测仍写入 `dimensions`**，附 `suppressed_by_exception` 标记。审计能看到"我们看到了不一致，但 S17 声明它合法"。
3. 例外本身若缺少 `declared_by` → 拒绝该例外，按普通检查判。**S36 不信任无来源的例外**。

这是"不许重复也不许留空"的落点：重复由 suppress 消除，留空由"观测仍保留在 dimension"防住。

---

## 8. 数据模型（schema `semantic_qc`）

```sql
CREATE TABLE semantic_qc.report (
  report_id           text PRIMARY KEY,
  report_generation   integer NOT NULL,
  tenant_id           text NOT NULL,
  series_id           text NOT NULL,
  episode_id          text NOT NULL,
  branch_id           text NOT NULL,
  shot_id             text NOT NULL,
  shot_lineage_id     text NOT NULL,
  logical_node_id     text NOT NULL,
  node_epoch          integer NOT NULL,
  manifest_id         text NOT NULL,
  manifest_revision   integer NOT NULL,
  run_id              text NOT NULL,
  execution_fence     integer NOT NULL,
  artifact_sha256     text NOT NULL,
  shot_spec_revision  integer NOT NULL,
  expected_state_ref  jsonb,               -- S17 ref + revision，null 表示未取得
  evaluator_version   text NOT NULL,
  policy_version      text NOT NULL,
  sampling_version    text NOT NULL,
  decision            text NOT NULL,
  coverage_ratio      numeric(5,4) NOT NULL,
  attempt_slot_id     text,
  payload             jsonb NOT NULL,
  content_hash        text NOT NULL,
  created_at          timestamptz NOT NULL,
  UNIQUE (report_id, report_generation)
);
CREATE INDEX ON semantic_qc.report (tenant_id, branch_id, shot_lineage_id, created_at DESC);
CREATE INDEX ON semantic_qc.report USING gin (payload jsonb_path_ops);

-- VLM 调用明细：费用归因与复现
CREATE TABLE semantic_qc.evaluator_call (
  call_id             uuid PRIMARY KEY,
  report_id           text NOT NULL,
  check_id            text NOT NULL,
  window_index        integer NOT NULL,
  provider_job_id     text NOT NULL,        -- S26 job
  attempt_ref         text NOT NULL,        -- S26 attempt
  model_snapshot      text NOT NULL,
  prompt_template_rev integer NOT NULL,
  input_frame_count   integer NOT NULL,
  latency_ms          integer,
  outcome             text NOT NULL CHECK (outcome IN ('ok','timeout','schema_error','error')),
  raw_response_ref    jsonb,                -- S28 artifact ref
  cost_micro_units    bigint                -- 从 S23 投影，非权威
);
CREATE UNIQUE INDEX ON semantic_qc.evaluator_call (report_id, check_id, window_index, model_snapshot);

-- 与 S08 §1.6 同构
CREATE TABLE semantic_qc.idempotency_record (
  tenant_id text NOT NULL, command_id text NOT NULL, request_hash char(64) NOT NULL,
  command_type text NOT NULL, state text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code integer, response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE semantic_qc.outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id text NOT NULL, aggregate_id text NOT NULL, aggregate_sequence bigint NOT NULL,
  event_type text NOT NULL, partition_key text NOT NULL, payload jsonb NOT NULL,
  headers jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON semantic_qc.outbox (id) WHERE published_at IS NULL;

CREATE TABLE semantic_qc.inbox (
  consumer text NOT NULL, event_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

`evaluator_call` 是 E25 取证的关键：评估器超时后必须有记录说明"这次调用发生了什么"，否则无法区分"模型说没问题"和"模型没回答"。

### 8.1 幂等

`POST /semantic-checks` 的幂等键是 `tenant_id + command_id + request_hash`：

| 情况 | 行为 |
|---|---|
| 同 `command_id`、同 `request_hash`，前次 `DONE` | 返回同一个 `operation_id` 与同一份 `report_id`，**不重跑评估器** |
| 同 `command_id`、不同 `request_hash` | `409 COMMAND_PAYLOAD_MISMATCH`，不执行 |
| 同 `command_id`、前次 `IN_PROGRESS` | `202`，返回同一 `operation_id`（不重复投递 VLM） |

这条特别重要，因为一次语义质检要调几十次 VLM（`evaluator_calls: 41` 是常见量级）。没有幂等，
网络重试一次就是几十次重复付费调用。`evaluator_call` 上的
`UNIQUE (report_id, check_id, window_index, model_snapshot)` 是第二道防线：
即便 `idempotency_record` 被绕过，同一报告同一窗口也不会产生第二条调用记录。

**`quality.semantic.completed` 与报告落库同事务**（`aggregate_id = report_id`，
`aggregate_sequence = report_generation`）。否则下游会收到一份查不到的报告，或者报告生效了
而 S39 永远等不到事件。

---

## 9. API 与事件

### `POST /semantic-checks`

```json
{
  "command_id": "cmd_…",
  "request_hash": "…",          // 对除 command_id 外的规范化请求体求 sha256
  "execution_context": { "...": "execution-context v2 全文" },
  "artifact": { "service_id": "S28", "id": "clip_x", "revision": 1, "content_hash": "…" },
  "shot_spec_ref": { "service_id": "S06", "id": "shot_3_2", "revision": 4, "content_hash": "…" },
  "binding_set_ref": { "service_id": "S10", "id": "bs_…", "revision": 3, "content_hash": "…" },
  "expected_state_ref": { "service_id": "S17", "id": "cont_…", "revision": 7, "content_hash": "…" },
  "check_selection": { "include": ["VIS.IDENTITY.*", "VIS.HANDS.*"], "exclude": [] },
  "evaluator_profile": "dual_high_risk",
  "repair_context": null,
  "deadline": "..."
}
```

`expected_state_ref` 为 `null` 时：依赖期望的 check（`VIS.SPACE.PROP_*`, `VIS.STATE.*`）标 `dependency_unavailable` → 若这些 check 是 required，则 abstain。**不允许 silent skip**。

响应 `202` + `operation_id`。

### `GET /semantic-reports/{id}`

同 S35，加 `?include=evaluator_calls` 供 E25 取证。

### 事件 `quality.semantic.completed`

`data` 与 S35 同结构（`qc_layer="semantic"`），额外字段：

```json
{
  "includes_continuity_defects": true,
  "expected_state_consumed": { "service_id": "S17", "ref_revision": 7 },
  "model_disagreement": 0.12,
  "evaluator_calls": 41,
  "cost_micro_units": 128400
}
```

---

## 10. 测试清单

### 10.1 E24 —— 静态/时序缺陷 + 证据时间段 + coverage

| # | 用例 | 断言 |
|---|---|---|
| E24-S1 | 第 2.0–2.6s 人脸换成另一角色 | `VIS.IDENTITY.FACE_SWAP_WITHIN_SHOT` blocker；`time_range` 精确到该段；bbox 落在该角色脸；证据含裁剪帧 |
| E24-S2 | 第 1.0s 服装与 binding revision 不符 | `VIS.IDENTITY.OUTFIT_REVISION`；`expected_source` 指向 S10 binding revision |
| E24-S3 | 手部特写六指 | `VIS.HANDS.FINGER_DEFORM` blocker |
| E24-S4 | 手部占画面 0.5% | 该 check `not_applicable`，`decision` 不受影响 |
| E24-S5 | 越轴对白 | `VIS.CONTINUITY.AXIS_CROSSING` blocker，除非 ShotSpec 声明越轴 |
| E24-S6 | 仅 60% 窗口完成 | `coverage_ratio=0.60` → abstain |
| E24-S7 | 修复后复检，同类缺陷仍在 | `defect_fingerprint` 相同 → S39 identical-strategy 计数 +1 |
| E24-S8 | 修复后复检通过 | 新报告 `decision=pass`，`repair_context` 指向上次 slot |
| E24-S9 | 修复预算达到上限后仍出缺陷 | 报告照出；是否继续修由 S39 判（S36 不做计数） |

### 10.2 E25 —— 评估器超时与冲突

| # | 用例 | 断言 |
|---|---|---|
| E25-S1 | 主评估器超时 | `abstain(evaluator_timeout)`，`evaluator_call.outcome='timeout'` 有记录；**不 pass** |
| E25-S2 | 主成功、副超时 | 不用单边结果；abstain |
| E25-S3 | 双评估器分歧 0.5 | `decision=human_review`，双端结论都写入 |
| E25-S4 | VLM 返回 schema 不符 | `evaluator_call.outcome='schema_error'`；该 check failed → abstain |
| E25-S5 | S17 不可达 | `VIS.SPACE.PROP_*` / `VIS.STATE.*` = `dependency_unavailable` → abstain |
| E25-S6 | S17 给例外但无 declared_by | 例外失效，按普通检查判 |

### 10.3 边界专项（S17/S36 不重叠不留空）

| # | 用例 | 断言 |
|---|---|---|
| B1 | 正史说钥匙在 A 手上，画面显示在 B 手上 | S17 出 `continuity` defect；S36 出 `VIS.SPACE.PROP_OWNERSHIP`；S39 归并为一条，主来源 S17 |
| B2 | 同一镜内身份漂移 | 仅 S36 出；S17 不出 |
| B3 | 闪回镜头穿错时代服装 | S17 声明 flashback 例外 → S36 `not_applicable` 且保留 dimension；S39 不生成修复 |
| B4 | 闪回镜头**没有**声明例外 | S36 正常判 `VIS.IDENTITY.OUTFIT_REVISION` |
| B5 | S36 报告里出现 `type=continuity` 但 `expected_source` 指向 S16 | validator 拒绝：S36 不得直接消费 S16 |
| B6 | 词汇表覆盖：注册表里每个 check 都有 `owner`（S17 或 S36） | CI 断言 registry 无 owner 缺口 |

### 10.4 计费与合规

| # | 用例 | 断言 |
|---|---|---|
| F1 | VLM 调用 | 只产生 S26 job；无供应商凭证直连（网络策略断言） |
| F2 | QC 预算耗尽 | 后续 VLM 调用被 S26 拒绝 → 该 check failed → abstain；**不得降级到"跳过"** |
| F3 | 跨租户 artifact ref | 404 且不泄漏存在性 |
| F4 | 相同 command 重投 | 返回同一 report_id，不重复调用 VLM |

---

## 11. 契约缺口

| # | 缺口 | 建议 |
|---|---|---|
| C1 | 无 `contracts/quality/s17-expected-state.v2.schema.json` | 新增；S17 与 S36 的接缝必须有正式 schema，否则就是散文接口 |
| C2 | `check_manifest.reason_code` 缺 `legal_exception` | 扩枚举（向后兼容，纯新增值） |
| C3 | 无 `contracts/quality/vlm-response.v2.schema.json` | 新增；VLM 输出结构与 QCReport defect 的映射必须显式 |
| C4 | `event-topics.v2.json` 无 `quality.*` 的 payload schema | 同 S35 缺口 C2 |
| C5 | S36 `service.yaml` 的 `depends_on` 已含 S17/S26，但其 API 契约未声明 `expected_state_ref` | 需补 S17 的 `GET /expected-states?...` 或把它做成 S36 的 command 入参（本文取后者，不改 S17 的 API） |
