# S17 · 连续性校验 — 实现方案

状态：`design_only`。SLO 未验证。本文是实现方案，不是已实现的服务。

上游约束：[docs/05](../docs/05-cross-episode-memory.md)、[docs/11](../docs/11-quality-repair.md)、[S17 规格](../docs/design-package/services/S17-continuity.md)、[S16 实现方案](./S16-story-memory.md)。

---

## 0. 一句话边界

**S17 只能读，不能写。** 它产出的唯一东西是 `ContinuityReport` —— 一份带证据、带严重度、可被 S39 消费的缺陷清单。它没有任何写正史、写素材、写接受指针的路径。

> 硬规则：**S17 的数据库里不存在 `claim`、`state`、`holder` 这类表。** 只有报告、期望、矛盾、例外。数据模型里能"顺手存一份事实"的表，就是设计错误。

它要回答的问题：**画面/声音/文本里呈现的东西，和 S16 说的事实冲突了吗？** 以及更难的一问 —— **这个"冲突"是不是合法的（闪回、误导、特效、幻觉叙事）？**

---

## 1. 数据模型

数据库：独立 PostgreSQL 集群，schema `continuity`。所有表以 `tenant_id` 起键。

### 1.1 检查任务

```sql
CREATE TABLE continuity.check_run (
  tenant_id            text NOT NULL,
  check_id             text NOT NULL,
  command_id           text NOT NULL,
  series_id            text NOT NULL,
  branch_id            text NOT NULL,
  episode_id           text NOT NULL,
  episode_order        integer NOT NULL,
  manifest_ref         jsonb NOT NULL,             -- 被检查的 manifest
  memory_checkpoint    jsonb NOT NULL,             -- {branch_id, commit_sequence, content_hash}
  context_pack_ref     jsonb,                      -- S18 的 pack（可选，用于一致性交叉核对）
  check_profile        text NOT NULL,              -- text_logic|visual|audio|full
  scope                jsonb NOT NULL,             -- {shot_ids[], scene_ids[], artifact_refs[]}
  state                text NOT NULL,              -- queued|running|completed|abstained|failed
  started_at           timestamptz,
  completed_at         timestamptz,
  agent_task_ref       jsonb,                      -- S19 任务引用（多模态池）
  report_id            text,                       -- 完成后指向 report
  PRIMARY KEY (tenant_id, check_id)
);
CREATE UNIQUE INDEX check_run_idem ON continuity.check_run (tenant_id, command_id);
```

`memory_checkpoint` 是必填且**精确**的。S17 检查时用的正史版本必须和 manifest 声明的 checkpoint 一致，否则报告没有意义 —— 见 §6 `CHECKPOINT_MISMATCH`。

### 1.2 报告

```sql
CREATE TABLE continuity.continuity_report (
  tenant_id          text NOT NULL,
  report_id          text NOT NULL,
  check_id           text NOT NULL,
  series_id          text NOT NULL,
  branch_id          text NOT NULL,
  episode_id         text NOT NULL,
  manifest_ref       jsonb NOT NULL,
  memory_checkpoint  jsonb NOT NULL,
  verdict            text NOT NULL,   -- pass|pass_with_warnings|defects|abstained
  severity_max       text NOT NULL,   -- none|info|low|medium|high|blocking
  defect_count       integer NOT NULL,
  abstain_count      integer NOT NULL,
  covered_scope      jsonb NOT NULL,   -- 实际检查到的 shot/时间区间（覆盖率证据）
  coverage_ratio     numeric(4,3) NOT NULL,
  source_revision    integer NOT NULL,
  content_hash       bytea NOT NULL,
  created_at         timestamptz NOT NULL,
  supersedes_report_id text,
  PRIMARY KEY (tenant_id, report_id)
);
```

`coverage_ratio` + `covered_scope` 是必需的：**"没发现问题"和"没检查"必须能被区分。** 只报 `pass` 不报覆盖率，等于把未知当通过（对应 E25 的精神）。

### 1.3 缺陷

```sql
CREATE TABLE continuity.defect (
  tenant_id            text NOT NULL,
  report_id            text NOT NULL,
  defect_id            text NOT NULL,           -- 稳定 ID，跨重检可关联
  category             text NOT NULL,           -- 见 §2
  subcategory          text NOT NULL,
  severity             text NOT NULL,           -- info|low|medium|high|blocking
  confidence           numeric(4,3) NOT NULL,
  disposition          text NOT NULL,           -- raise|suppress|abstain|needs_human
  suppression_reason   text,                    -- 合法例外时必填
  -- 证据
  expected             jsonb NOT NULL,          -- 来自 S16：期望状态
  observed             jsonb NOT NULL,          -- 来自画面/文本/音频：观测状态
  evidence_refs        jsonb NOT NULL,          -- [{kind, service_id, id, revision, content_hash,
                                                --   shot_id, timecode_start_ms, timecode_end_ms,
                                                --   frame_hash, memory_claim_ref}]
  entity_ids           text[] NOT NULL DEFAULT '{}',
  claim_refs           jsonb NOT NULL DEFAULT '[]',   -- 关联的 S16 claim ref
  -- 位置
  shot_id              text,
  scene_id             text,
  timecode_start_ms    integer,
  timecode_end_ms      integer,
  -- 归因
  fault_hypothesis     text,                    -- generation|asset|script|memory|edit|unknown
  fault_owner_service  text,                    -- 推测责任方（S29/S31/S11/S04/...），S17 不判定
  suggested_action     text,                    -- repair_hint，S39 决定是否采纳
  PRIMARY KEY (tenant_id, report_id, defect_id)
);
CREATE INDEX defect_by_entity ON continuity.defect (tenant_id, entity_ids) USING gin;
CREATE INDEX defect_by_shot ON continuity.defect (tenant_id, shot_id);
```

`fault_hypothesis` / `fault_owner_service` 是**假设**，不是判定。S17 不决定谁来修、怎么修 —— 那是 S39 和 S02 的事。

### 1.4 期望状态（S17 自己的推导结果，不是事实副本）

```sql
CREATE TABLE continuity.expected_state (
  tenant_id        text NOT NULL,
  report_id        text NOT NULL,
  unit_id          text NOT NULL,          -- shot_id 或 scene_id
  entity_id        text NOT NULL,
  predicate        text NOT NULL,
  expected_value   jsonb NOT NULL,
  source_claim_ref jsonb NOT NULL,         -- 必须带 S16 claim ref + revision + hash
  checkpoint       bigint NOT NULL,
  derivation       text NOT NULL,          -- direct|inherited|inferred_from_event
  PRIMARY KEY (tenant_id, report_id, unit_id, entity_id, predicate)
);
```

关键设计：**`expected_state` 每一行都强制带 `source_claim_ref`。** 没有来源的期望无法写入。这让"凭空想象一个期望然后报缺陷"在结构上不可能 —— 也避免 S17 悄悄变成第二份正史：它存的是"某次检查在某个 checkpoint 上推导出的期望"，不是"当前事实"。

### 1.5 合法例外（压制规则）

```sql
CREATE TABLE continuity.exemption (
  tenant_id       text NOT NULL,
  series_id       text NOT NULL,
  exemption_id    text NOT NULL,
  category        text NOT NULL,         -- 被豁免的缺陷类别
  match_scope     jsonb NOT NULL,        -- {shot_ids[]|episode_ids[]|entity_ids[]|timecode_ranges[]}
  justification   text NOT NULL,          -- 为什么合法（必填，不能空）
  basis           text NOT NULL,          -- source_material|director_decision|genre_convention|world_rule
  world_rule_ref  jsonb,                  -- basis=world_rule 时指向 S16 的 rule claim
  approved_by_principal text NOT NULL,
  approved_at     timestamptz NOT NULL,
  expires_at      timestamptz,
  PRIMARY KEY (tenant_id, exemption_id)
);

CREATE TABLE continuity.exemption_application (
  tenant_id, report_id, defect_id, exemption_id, applied_at,
  PRIMARY KEY (tenant_id, report_id, defect_id)
);
```

例外必须**带理由、带批准人、带作用域**。不接受"全局关闭某类检查"这种用法 —— 那是策略层（S02）的事，不是 S17 的豁免表。

### 1.6 基线（跨报告比对）

```sql
CREATE TABLE continuity.episode_baseline (
  tenant_id        text NOT NULL,
  series_id        text NOT NULL,
  branch_id        text NOT NULL,
  episode_id       text NOT NULL,
  report_id        text NOT NULL,           -- 当前认定的基线报告
  memory_checkpoint jsonb NOT NULL,
  established_at   timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, series_id, branch_id, episode_id)
);
```

只有基线报告的缺陷会进 S39 的修复队列；重检产生的非基线报告用于对比与决策，不自动触发返工。

### 1.7 基础设施表

```sql
continuity.idempotency_key (tenant_id, command_id, request_hash, response_body, ...);
continuity.outbox_event    (tenant_id, event_id, aggregate_id, aggregate_sequence, payload, ...);
continuity.inbox_consume   (tenant_id, consumer, event_id, applied_at, ...);
```

---

## 2. 检查项分类

分两大池：**文本逻辑池**（读剧本+记忆，纯结构化/小模型）与**多模态池**（读关键帧/视频/音频，VLM/对齐模型）。池分开是因为扩容曲线差一个数量级（`service.yaml: scaling: 文本逻辑与多模态校验分池`）。

### 2.1 文本逻辑池

| category | 检查什么 | 期望来源 | 观测来源 |
|---|---|---|---|
| `KNOWLEDGE_VIOLATION` | 角色说出/做出只有"知道"才能做的行为 | `knowledge_state(knower, prop) = unaware` | 台词、动作描述 |
| `HOLDER_MISMATCH` | 剧本里道具在某人手上，与 `held_by` 不符 | `entity_state.held_by` | 剧本动作/道具描述 |
| `INJURY_REGRESSION` | 伤势在有效区间内消失 | `entity_state.injury_*` | 剧本动作、分镜描述 |
| `RELATION_MISMATCH` | 称呼/态度与 `relationship` 冲突（如已决裂仍称"亲爱的"） | `relationship` | 台词、人物关系描述 |
| `THREAD_ABANDONED` | 伏笔过 `due_to_tick` 仍未推进 | `plot_thread.due_to_tick` | 剧本整体 |
| `THREAD_DOUBLE_PAYOFF` | 同一伏笔被两个事件回收 | `plot_thread.resolved_by_event_id` | 剧本事件 |
| `WORLD_RULE_BREACH` | 违反 `world_rule`（生死、能力、年代） | `world_rule`（含 exceptions） | 剧本事件/能力使用 |
| `CAUSAL_ORPHAN` | 事件缺乏 `causes` 中的前因 | `story_event.causes` | 剧本事件链 |
| `TEMPORAL_INCONSISTENT` | 故事时间顺序自相矛盾 | `episode_story_window` + tick | 剧本时间标注 |
| `FLASHBACK_MISFRAME` | 闪回内容与主时钟记录冲突 | 主时钟 claim | 闪回段内容 |

### 2.2 多模态池

| category | 检查什么 | 期望来源 | 观测来源 |
|---|---|---|---|
| `VISUAL_STATE_MISMATCH` | 画面里的伤势/服装/道具与期望不符 | `entity_state` + S18 asset refs | 关键帧 VLM |
| `PROP_VISUAL_MISMATCH` | 道具外观与 canonical 变体不符（不同钥匙） | S10/S14 canonical ref | 关键帧 + 感知 hash |
| `SCREEN_DIRECTION_BREACH` | 180° 轴线、视线匹配、越轴 | 上一镜头轴线状态（S17 自算） | 镜头序列几何 |
| `CONTINUITY_OF_MOTION` | 相邻镜头动作/位置断裂 | 上一镜头末态 | 首帧/末帧比对 |
| `VOICE_MISMATCH` | 音色与 S15 canonical 授权不符 | S15 voice ref | 音频 embedding |
| `COSTUME_SWITCH` | 同一场景内服装无事件突变 | `entity_state.outfit` | 关键帧 |
| `LIGHT_TIME_JUMP` | 光照/时间标记与场景连续冲突 | 场景设定 | 关键帧 + 元数据 |

### 2.3 严重度与处置决策

```
severity = f(category, confidence, story_impact)
```

固定规则表（**不是模型自由判断**）：

| 情形 | disposition | severity |
|---|---|---|
| 命中合法例外（§1.5）且作用域匹配 | `suppress` | — |
| `confidence < 0.55` | `abstain` | — |
| `KNOWLEDGE_VIOLATION` + conf ≥ 0.80 | `raise` | `blocking` |
| `HOLDER_MISMATCH` + conf ≥ 0.75 | `raise` | `high` |
| `WORLD_RULE_BREACH` + conf ≥ 0.80 | `raise` | `blocking` |
| `INJURY_REGRESSION` + conf ≥ 0.70 | `raise` | `high` |
| `SCREEN_DIRECTION_BREACH` + conf ≥ 0.70 | `needs_human` | `medium` |
| 其余 `raise` 条件 | `raise` | `low` |
| 结构性不可判定（缺资产/缺关键帧） | `needs_human` | `medium` |

**低置信度必须 abstain，不能降级成"小问题"。** 把 0.4 置信度的知识越界报成 low，会训练团队忽略告警。abstain 记录仍进报告且计入 `abstain_count`，便于后续调整阈值。

---

## 3. 合法例外的判定（最难的部分）

S17 的核心验收是"闪回、误导、合法特效与真实矛盾有区分"。判定顺序是**先证明合法，再报缺陷**：

```
对每个候选缺陷 d：
  1. 叙事框架检查
     ├─ 所在 shot 的 diegetic_frame ∈ {flashback, dream, hallucination, imagination, vision}
     ├─ 且该框架在剧本中被显式标注（S04 的 scene_intent）
     └─ 且框架内的"矛盾"不推进主时钟事实
        → suppress, reason=DREAM_OR_FLASHBACK_FRAME
  2. 视角/欺骗检查
     ├─ 观测来自角色的主观镜头（POV）
     ├─ 且 S16 有 knowledge_state(knower, prop) = believes(错误值)
     └─ 且剧本意图标注为"误导性呈现"
        → suppress, reason=SUBJECTIVE_MISREPRESENTATION
  3. 观众先知检查
     ├─ 观测体现的信息 audience_reveal_episode_order ≤ 当前集
     └─ 角色视角的越界不在观测所在帧中
        → 不是缺陷（区分叙事视角与角色视角）
  4. 特效/超能力检查
     ├─ 有 world_rule 允许该能力
     └─ 且在 exception 内
        → suppress, reason=WORLD_RULE_ALLOWS
  5. 显式豁免检查
     ├─ 命中 exemption.match_scope
     └─ 且未过期
        → suppress, reason=EXEMPTION_<id>
  6. 以上都不成立
     → 按 §2.3 报
```

**判定必须留下证据行**：每次 suppress 都写 `defect.disposition='suppress'` + `suppression_reason` + `evidence_refs`，进报告。**被压制的不是"没检出"，是"检出后判定合法"。** 这在审计上完全不同 —— 不能因为最终报告干净就说检查通过。

### 3.1 误导性对白的特殊处理

"林岚说'我不认识他'，实际她知道"这种：

- 若 `knowledge_state(char_linlan, claim_x) = knows` 且台词表达 `unaware`
- → `KNOWLEDGE_VIOLATION` 候选
- → 但若剧本标注 `dialogue_intent = deception`（撒谎）且说话人可信动机存在（`story_event` 中有保护/威胁等）→ `suppress, reason=CHARACTER_DECEPTION`
- 若剧本**没有** deception 标注 → `raise`，severity `medium`（可能是编剧失误，也可能漏标注），`subcategory=UNMARKED_KNOWLEDGE_CONTRADICTION`

这条规则的意义：**S17 不猜意图，它要求意图被显式标注。** 标注缺失本身就是缺陷。

---

## 4. 输入与调用

### 4.1 命令

`POST /continuity-checks`

```json
{
  "command_id": "cmd_cont_ep3_r1",
  "tenant_id": "derived",
  "series_id": "series_demo",
  "branch_id": "canon",
  "episode_id": "ep003",
  "manifest_ref": {"id":"manifest_ep3","revision":4,"content_hash":"..."},
  "memory_checkpoint": {"branch_id":"canon","commit_sequence":27,"content_hash":"..."},
  "check_profile": "full",
  "scope": {"shot_ids": ["shot_3_2_1","shot_3_2_2"], "artifact_refs": [...]},
  "exemption_ids": ["exm_017"],
  "expected_report_revision": null
}
```

### 4.2 依赖读取（全部只读）

| 依赖 | 用途 | 失败行为 |
|---|---|---|
| S16 `POST /memory-queries` | 期望状态的地面真值 | **强依赖**：watermark 落后 → 整个 check 转 `abstained`（见 §6） |
| S06 `GET /shots/{id}/revisions/{rev}` | 镜头 DSL、动作节拍 | 单镜头缺失 → 该镜头 skip，计入未覆盖 |
| S10 `GET /assets/{id}/resolve` | 资产 canonical 引用 | 同上 |
| S28 `GET /artifacts/{id}` | 关键帧/视频/音频 | 同上；缺关键帧时改用视频抽帧，再不行 skip |
| S19（经 S26） | VLM/抽取/对齐模型 | 预算不足 → 转 `needs_human`，不静默跳过 |

**S17 不缓存 S16 的事实。** 每次 check 实时查询，只把结果写进 `expected_state`（带 report_id 与 checkpoint）。这是"不存第二份事实"的具体落地方式。

### 4.3 上下文包交叉核对

若请求带 `context_pack_ref`，S17 额外做一次核对：S18 的 pack 里声称的 `prohibited_assumptions` 是否被本次检查覆盖。未覆盖 → 报告 `coverage_gap`，且该 pack 不得用于最终交付。

---

## 5. 输出与回给 S39

### 5.1 报告读取

`GET /continuity-reports/{id}` → `continuity_report` + `defect[]` + `expected_state[]` + `exemption_application[]`。

### 5.2 事件 `continuity.check.completed`

```json
{
  "type": "continuity.check.completed",
  "subject": "series_demo/canon/ep003",
  "aggregate_id": "series_demo:canon:ep003",
  "aggregate_sequence": 12,
  "data": {
    "check_id": "chk_...",
    "report_id": "rep_...",
    "verdict": "defects",
    "severity_max": "blocking",
    "defect_count": 2,
    "abstain_count": 1,
    "coverage_ratio": 0.94,
    "memory_checkpoint": {"commit_sequence": 27, "content_hash": "..."},
    "manifest_ref": {"id":"manifest_ep3","revision":4,"content_hash":"..."},
    "defect_summary": [
      {"defect_id":"d_1","category":"KNOWLEDGE_VIOLATION","severity":"blocking",
       "shot_id":"shot_3_2_1","claim_ref":{"id":"claim_chenmo_identity","revision":3},
       "suggested_action":"rewrite_dialogue_or_add_reveal"},
      {"defect_id":"d_2","category":"INJURY_REGRESSION","severity":"high",
       "shot_id":"shot_3_2_3","claim_ref":{"id":"claim_linlan_injury","revision":2},
       "suggested_action":"regenerate_shot_with_injury_state"}
    ]
  }
}
```

### 5.3 缺陷如何回给 S39

S39（`repair-planning`）消费 `continuity.check.completed`，按 `fault_hypothesis` 分派：

| fault_owner_service | 修复路径 | S39 决策依据 |
|---|---|---|
| S04（剧本） | 改剧本 → 新 manifest → 重抽记忆 → 重检 | blocking 缺陷必须走剧本变更，不能靠重生成绕过 |
| S06（分镜） | 改镜头 DSL → 局部生成 | 动作/机位类 |
| S11–S15（资产） | 换 canonical 变体或新增变体 | 外观类 |
| S29–S31（生成） | 局部重生成（受 node_epoch 约束） | 画面状态类 |
| S41/S42（剪辑/合成） | 重剪/重混 | 轴线/运动/光类 |
| unknown | 人审 | S17 abstain 时 |

**S17 只给 `suggested_action` 提示，不给修复计划。** S39 必须自己结合尝试上限、预算、node_epoch 决定。S17 的 `suggested_action` 不是可执行指令。

关键约束（写在 S17 侧）：**若 `fault_hypothesis = script` 且 severity = blocking，S17 必须同时在 `evidence_refs` 里附上剧本 source span**，否则 S39 无法定位。缺 span 的阻塞缺陷 → `needs_human`，不阻断生成流程（避免因为证据不足把整条流水线卡死）。

---

## 6. 正史水位与降级

这是 S17 与 S18 最容易出错、也最容易被忽略的地方。

```
收到 check 请求：
  r = S16.query({minimum_checkpoint: request.memory_checkpoint.commit_sequence})
  if r.status == 409 MEMORY_WATERMARK_BEHIND:
       → check_run.state = 'abstained'
       → 写 continuity_report(verdict='abstained', abstain_count=全量候选)
       → 发布 continuity.check.completed { verdict: 'abstained', reason: 'MEMORY_WATERMARK_BEHIND' }
       → 不返回任何 pass
```

**S17 绝不能"用能查到的旧 checkpoint 凑合检查"。** 那样会：
1. 用过期事实当期望 → 报出大量假缺陷；
2. 或更糟：过期事实里角色"不知道"，检查通过，但真实正史里角色已经知道了 → 漏报知识越界。

后者是产品事故级别的问题。所以：**水位不够 = 明确 abstain = 阻塞交付门（由 S02/S08 判定），而不是"看起来通过"。**

同理，若 manifest 声明的 `memory_checkpoint` 与本次实际查询到的 checkpoint 不一致 → `CHECKPOINT_MISMATCH`，整个 check `abstained`。

---

## 7. 多模态池的工程要点

### 7.1 覆盖率

音频/视频检查按时间切片。片切片 `[t0,t2)`，`coverage_ratio = Σ(covered_shot_duration × weight) / total_duration`。

- `coverage_ratio < 0.85` → 报告不能是 `pass`，最高 `pass_with_warnings`，并写 `coverage_gap` 缺陷。
- 缺失资产（关键帧/视频/音频）→ 逐项记 `MISSING_EVIDENCE`，不是静默 skip。

### 7.2 确定性

多模态检查有模型随机性。同一输入两次检查可能有不同结果。处理方法：

1. 每次报告固定 `agent_task_ref`（definition revision + model snapshot + seed）；
2. 报告之间用 `defect_id` 稳定关联（`stable_defect_key = sha256(category|shot_id|entity_id|predicate)`）；
3. 重检时对比 `supersedes_report_id`，统计 `new / resolved / still_present`；
4. **不要求两次完全一致**（那是不可能的产品承诺），但要求：同 seed + 同输入 → 一致（可测的确定性）。

### 7.3 视觉状态检查的实现方式

不是让 VLM 自由描述然后比对。而是：

```
1. 从 S16 取期望：entity_state(char_linlan, injury_left_hand) = "bandaged"
2. 从 S10 取 canonical 变体：S11 的角色外观 + S12 的服装 + S14 的道具
3. 从 S18 的 pack 取该镜头的 prohibited_assumptions（这是 S18 生成的，S17 只读）
4. 用结构化 VQA 问询：「画面中 char_linlan 的左手是否可见？是否缠有绷带？」
   → 返回 {visible: bool, bandaged: bool, confidence: float}
5. 只对 visible=true 的帧判断 bandaged
6. visible=false 的帧不产生缺陷，但降低覆盖权重
```

**不 asks "这个画面和正史冲突吗"** —— 那会让模型产生自由联想。问封闭问题，拿结构化答案。

---

## 8. 可观测与运维

指标：
- `s17_check_total{state,check_profile}`、`s17_check_latency_seconds{pool}`
- `s17_defect_total{category,disposition,severity}`
- `s17_abstain_ratio`、`s17_coverage_ratio`（直方图）
- `s17_exemption_applied_total{exemption_id}`
- `s17_memory_watermark_behind_total`

告警：
- `s17_memory_watermark_behind_total` 持续 > 0 → S16 提交链落后，检查全线 abstain。
- 某 `exemption_id` 应用次数占比 > 30% → 豁免被滥用，人工复核。
- `s17_abstain_ratio` 突升 → 模型退化或证据质量下降，暂停该 profile。

runbook：
- `recheck-with-new-checkpoint`：正史推进后重跑，保留旧报告，`supersedes_report_id` 串起来。
- `exemption-audit`：按月导出所有生效豁免与理由，交合规复核。
- `coverage-gap-report`：列出 coverage < 0.85 的集，供制作排期补检查。

---

## 9. 测试清单

### 9.1 E09 · 闪回 / 观众先知 / 角色不知（S17 侧）

| 用例 | 期望 |
|---|---|
| E09-1 闪回镜头中伤势状态"回到从前" | `suppress, DREAM_OR_FLASHBACK_FRAME`，报告无 raise |
| E09-2 闪回段含未标注的框架 | `raise, FLASHBACK_MISFRAME, medium`（标注缺失算缺陷） |
| E09-3 角色 POV 中被骗看到的错误身份 | `suppress, SUBJECTIVE_MISREPRESENTATION`（需 `believes` + 意图标注） |
| E09-4 全知镜头中角色说出不该知道的事 | `raise, KNOWLEDGE_VIOLATION, blocking` |
| E09-5 观众视角先知但角色视角正常 | 不报缺陷（区分叙事层次） |
| E09-6 `world_rule` 允许的超能力 | `suppress, WORLD_RULE_ALLOWS` |
| E09-7 deception 未标注的撒谎台词 | `raise, UNMARKED_KNOWLEDGE_CONTRADICTION, medium` |

### 9.2 E10 · 分支与候选（S17 侧）

| 用例 | 期望 |
|---|---|
| E10-1 对实验分支检查 | 用实验分支的 checkpoint，不读正史 |
| E10-2 对未接受候选的剧本检查 | 报告带 `candidate` 标记，不得作为交付门依据 |
| E10-3 分支合并后重检 | 用合并后 checkpoint；旧报告 `supersedes` 关系保留 |

### 9.3 E11 · 索引/水位落后（S17 侧）

| 用例 | 期望 |
|---|---|
| E11-1 S16 head=27，检查请求要求 30 | `abstained`，`reason=MEMORY_WATERMARK_BEHIND`，不返回 pass |
| E11-2 manifest 声明 checkpoint 27，实际查询得 26 | `CHECKPOINT_MISMATCH`，abstain |
| E11-3 检查过程中 S16 推进到 28 | 本次检查用 27 完成（快照一致性），报告明确写 27 |

### 9.4 检查能力（正确性）

| 用例 | 期望 |
|---|---|
| 道具换了（不同钥匙外观） | `PROP_VISUAL_MISMATCH, high`，带帧 hash + 双方 claim ref |
| 伤势在有效区间内消失 | `INJURY_REGRESSION, high` |
| 同一伏笔两集都回收 | `THREAD_DOUBLE_PAYOFF, blocking` |
| 越轴 | `SCREEN_DIRECTION_BREACH, medium, needs_human` |
| 低置信度（0.4） | `abstain`，不降级为 low |
| 缺关键帧 | `MISSING_EVIDENCE` + coverage 下降，不静默通过 |
| coverage = 0.6 | verdict 最高 `pass_with_warnings`，不能 `pass` |

### 9.5 边界与防越权

| 用例 | 期望 |
|---|---|
| 尝试通过 S17 写 S16 事实 | 无此 API；任何写事实的代码路径都不存在 |
| 豁免无理由 / 无批准人 | 拒绝创建 |
| 豁免过期后应用 | 不生效，正常报缺陷 |
| 跨租户 report 查询 | `404`（不泄漏存在性） |
| 同 command_id 重投 | 幂等，不重复建 check_run |
| 对 block 缺陷尝试"忽略并交付" | S17 不提供该能力；只能由 S02/S40 走策略/人工例外 |

---

## 10. 反模式清单

1. ❌ 在 S17 库里存 `held_by` / `current_state` 供后续查询用 → 只能存 `expected_state` 且强制带 `source_claim_ref` + `report_id`。
2. ❌ 水位落后时用旧 checkpoint 检查 → 必须 abstain。
3. ❌ 只返回 `pass` 不返回覆盖率 → 无法区分"检查通过"与"没有检查"。
4. ❌ 低置信度降级为 low severity → 必须 abstain。
5. ❌ 让模型回答"有没有冲突" → 只能回答封闭的结构化问题。
6. ❌ 在 S17 里做修复决策或写接受指针 → 那是 S39 / S02 / S08。
7. ❌ 用"全局关闭某类检查"代替豁免 → 豁免必须有作用域、理由、批准人、有效期。
8. ❌ 把 suppress 的缺陷从报告里删掉 → 必须保留 disposition + reason 作为审计证据。
9. ❌ S17 自己写一份 `prohibited_assumptions` → 那是 S18 的产出，S17 只核对其被覆盖情况。
