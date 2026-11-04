# S40 · 人工审核与接管 —— 实现方案

状态：`design_only` 服务的实现规格。本文不含业务代码，目标是"照此可以直接写"。所有容量与 SLO 数字仍是 docs/03 的规划值，**未实测**。

依据：docs/06（核心）、docs/04 §1–§2、docs/07 §4、docs/11 §3、docs/13 §3、docs/14 §1、docs/16 §5、`contracts/openapi.v2.yaml`（`decideReview`）、`contracts/execution-context.v2.schema.json`、`contracts/episode-manifest.v2.schema.json`、`contracts/examples/service-interactions.yaml`（`accept_script_to_memory`）。

---

## 0. 一句话边界

S40 记录**人说了什么**，不决定**能不能生效**，更不写**接受指针**。

```
S40: 建档 → 派单 → 收决定 → 校验决定绑定 → 发事件
S02: 这个决定在权限/条件上是否成立（validate_review_decision）
S08: 用 CAS 把指针推过去（唯一权威）
```

三者的顺序是**固定**的，见 `contracts/examples/service-interactions.yaml` 的 `accept_script_to_memory`：

```yaml
- from: S40, to: S02, action: validate_review_decision     # 权限与条件
- from: S40, to: S08, action: request_acceptance_CAS       # 真正提交
```

注意：**S08 的 CAS 才是"推进"这个动作本身。** S40 的 `decision=accept` 只是"有人点了接受"，不构成状态变化。任何把 S40 的决定当作已接受、或者让 S40 直接写 manifest 的实现都是造第二权威。

---

## 1. 责任与明确不负责

**唯一写者**（本服务凭证独占写）：

- `ReviewTask` —— 任务本身（证据快照、上下文、选项、截止）
- `ReviewAssignment` —— 分派、认领、委派
- `ReviewDecision` —— 决定内容、理由、附带选项、投票
- `ReviewQuorumState` —— 多人审批的累计状态
- 审核截止（`deadline`、`expires_at`）

**明确不写：**

| 事实 | 权威 | S40 的正确做法 |
|---|---|---|
| 接受指针 / manifest / node_epoch / execution_fence | **S08** | 只发事件/调用 CAS，等 S08 返回结果 |
| 策略、gate 规则、权限判定、quorum 是否满足的**规则** | **S02** | 调 `validate_review_decision` 拿判定，自己不算 |
| 角色/成员/委派资格/SSO 状态 | **S01** | 每次投票实时校验 |
| 金额、预算额度追加 | **S23** | 预算类任务只记录"批准了多少"，金额由 S23 落地 |
| QC 报告与缺陷证据 | S35–S37 / S17 / S39 | 只引用不可变报告，不自造分数 |
| 资产/音色的 canonical 状态 | S10–S15 | 同上 |
| 用户在节点上的参数修改、重写对白 | S06 / S07 / S08 | S40 只记录"人选择了什么选项"，不执行创作变更 |

S40 **不发起模型调用、不产生费用、不写媒体**。

---

## 2. `ReviewTask`：任务内容

### 2.1 为什么任务必须"冻结"

docs/06 §3：`ReviewTask固定manifest/epoch、artifact hash、policy version、evidence、reason、options、assignee role和deadline`。

冻结不是性能考虑，是**正确性**考虑：审核员看到的东西如果可以在审核期间变化，那就无法判断他到底批了什么。所以任务一旦创建就是不可变快照；变化通过"新建任务 + 旧任务 SUPERSEDED"表达。

### 2.2 结构

```jsonc
{
  "schema_version": "2.0",
  "review_task_id": "rt_01J9...",
  "tenant_id": "tenant_demo",
  "kind": "gate_acceptance",        // gate_acceptance | quality_exception | budget_increase
                                    // | external_publish | repair_choice | manual_takeover
                                    // | evidence_abstain | conflict_arbitration

  // ── 绑定（不可变，全部参与 stale 判定）────────────────────────
  "binding": {
    "manifest_ref":       {"id": "mf_ep3", "revision": 7, "content_hash": "sha256:..."},
    "execution_fence":    2,
    "node_id":            "shot_3_2.video",
    "node_epoch":         4,
    "lock_version":       11,
    "artifact":           {"service_id":"S26","id":"art_...","revision":3,"content_hash":"sha256:..."},
    "policy_ref":         {"service_id":"S02","id":"policy_p1","revision":3,"content_hash":"sha256:..."},
    "gate":               "canonical_asset",        // 或 null
    "pack_hash":          "sha256:..."              // 证据包整体哈希，见 2.4
  },

  // ── 上下文（给人看的） ────────────────────────────────────────
  "subject": {
    "episode_id": "ep_03", "series_id": "sr_01", "branch_id": "main",
    "stage": "canonical_asset", "shot_label": "第3场第2镜",
    "artifact_kind": "video", "duration_ms": 4000,
    "preview_url_ref": "s3://...?sig=..."          // 短签名，过期时间 ≤ 任务 deadline
  },

  "reason": {                                       // 为什么需要人
    "code": "GATE_REQUIRED",                        // 或 QC_CONFLICT / EVALUATOR_TIMEOUT
                                                    //   / REPAIR_EXHAUSTED / BUDGET_INCREASE_REQUESTED
                                                    //   / LOW_CONFIDENCE / MANUAL_LOCK
    "text": "企业策略要求 canonical_asset 门人工接受",
    "explain_ref": {                                // 指向 S02 的 explain，用于"为什么需要人工"
      "service_id": "S02", "policy_ref": {"revision": 3}, "field": "required_gates"
    }
  },

  // ── 证据（不可变引用 + 摘要） ─────────────────────────────────
  "evidence": {
    "qc_reports": [
      {"service_id":"S35","id":"qc_t_...","revision":2,"content_hash":"sha256:...",
       "decision":"pass","coverage":0.98,"evaluator_version":"s35@1.4.0"},
      {"service_id":"S36","id":"qc_v_...","revision":1,"content_hash":"sha256:...",
       "decision":"human_review","coverage":0.71,"evaluator_version":"s36@0.9.2"}
    ],
    "defects": [                                    // 从 QC 报告展开，带时间码
      {"defect_id":"d1","category":"hand_anomaly","severity":"S2",
       "time_range_ms":[1200,1680],"bbox":[0.42,0.31,0.58,0.77],
       "expected":"右手持杯","observed":"手指数量异常",
       "evidence_artifact_ref":{"id":"art_crop_...","content_hash":"sha256:..."},
       "suggested_action":"local_edit_or_regenerate"}
    ],
    "continuity_ref": {"service_id":"S17","id":"ct_...","revision":1,"content_hash":"sha256:..."},
    "cost_context": {"attempts_used":2,"max_attempts":3,"spent_micro":820000,
                     "budget_available_micro":9400000},     // 只读快照，权威仍是 S23
    "memory_checkpoint": {"branch_id":"main","commit_sequence":88,"content_hash":"sha256:..."}
  },

  // ── 可选项（人能看到的选择，逐项带后果） ──────────────────────
  "options": [
    {"option_id":"accept",       "label":"接受当前版本",
     "effect":{"gate":"satisfied","advances_pointer":true,"cost_delta_micro":0},
     "requires_permission":"review.accept"},
    {"option_id":"reject",       "label":"驳回",
     "effect":{"gate":"failed","advances_pointer":false,"cost_delta_micro":0}},
    {"option_id":"request_repair","label":"要求修复（第3次，最后一次）",
     "effect":{"gate":"blocked_unmet","creates_repair_plan":true,
               "cost_estimate_micro":[300000,900000],"advances_pointer":false}},
    {"option_id":"replace",      "label":"换用候选 B",
     "effect":{"advances_pointer":true,"candidate_ref":{"id":"art_alt_...","content_hash":"sha256:..."},
               "cost_delta_micro":0}},
    {"option_id":"change_spec",  "label":"改规格（改分镜）",
     "effect":{"creates_change_plan":true,"invalidates_downstream":["video","lipsync","subtitles"],
               "advances_pointer":false}},
    {"option_id":"exception_accept","label":"例外接受（需说明理由）",
     "effect":{"gate":"waived","advances_pointer":true,"requires_reason":true},
     "requires_permission":"review.exception_accept",
     "not_available_when":"gate ∈ {final_master} 且 no_incomplete_formal_delivery"}  // 安全门不可豁免
  ],

  // ── 派单与时限 ────────────────────────────────────────────────
  "assignment": {
    "assignee_roles": ["director"],                 // 来自 S02 的 reviewer_roles ∩ gate 要求
    "candidate_principals": ["u_101","u_207"],      // S01 解析；不硬编码
    "assigned_to": null,                            // 认领后填
    "delegation_chain": [],
    "quorum": {"required": 2, "separation_of_duties": true,
               "must_include_roles": ["director","producer"]},
    "sla": {"deadline":"2026-10-30T18:00:00+08:00","reminder_at":"2026-10-30T16:00:00+08:00"}
  },

  "state": "OPEN",
  "created_at": "2026-10-30T09:00:00Z",
  "created_by": {"principal_id":"svc_orchestrator","principal_type":"service"},
  "source_event": {"event_id":"evt_...","type":"snapshot.manifest.committed"}
}
```

### 2.3 证据包哈希 `pack_hash`

```
pack_hash = sha256(canonical_json({
   binding.manifest_ref, binding.node_epoch, binding.lock_version,
   binding.artifact.content_hash, binding.policy_ref,
   evidence.qc_reports[*].{id,revision,content_hash},
   evidence.defects[*].defect_id, evidence.continuity_ref
}))
```

作用：**决定提交时，S40 用 `pack_hash` 一次性确认"人批的还是现在这个"**。任何一项被替换 → 哈希不匹配 → 决定 stale。这比逐字段比较更难漏。

### 2.4 选项的可信性

`options` 里每项都带 `effect`，含义是"选它会发生什么"。实现要点：

- `effect.advances_pointer` **必须为 false**，除非该选项会走 S08 CAS。S40 自己绝不推进。
- `cost_estimate_micro` 来自 S25/S23 的只读估算，标注 `estimate_source` 与 `expires_at`。**报价过期后该选项应被禁用**，否则人会基于失效成本做决定。
- `not_available_when` 由 S02 的条件求值填充（例：安全门不可例外接受）。S40 不做这个判断，只渲染与在提交时复核。

---

## 3. 决定结构与状态机

### 3.1 提交接口

`POST /review-tasks/{id}/decisions`（OpenAPI `decideReview`）的请求体已固定为 `{context, decision, reason, command_id}`，`decision ∈ {accept, reject, request_repair}`。实现需要在**不破坏该契约**的前提下扩展（见 §10 G1）：

```jsonc
{
  "context": { ...execution-context 子集，含 manifest_ref / node_epoch / execution_fence / policy_ref... },
  "decision": "accept",                 // 契约三值；扩展值走 option_id
  "option_id": "accept",                // 扩展（可选）：枚举外的 replace/change_spec/exception_accept
  "reason": "第3场第2镜手部异常已修复，接受",
  "payload": {"selected_candidate_ref": null, "exception_scope": null},   // 扩展（可选）
  "expected_task_revision": 1,          // 乐观并发：任务被 supersede 后拒绝
  "command_id": "cmd_..."
}
```

### 3.2 `ReviewDecision`（存下来的记录）

```jsonc
{
  "decision_id": "rd_01J...",
  "review_task_id": "rt_01J...",
  "tenant_id": "tenant_demo",
  "command_id": "cmd_...",
  "request_hash": "sha256:...",         // tenant+command_id+request_hash 幂等键

  // ── 决定时看到的版本（冻结副本，不是引用）────────────────────
  "decided_against": {
    "manifest_ref": {...}, "execution_fence": 2, "node_epoch": 4,
    "lock_version": 11, "artifact_content_hash": "sha256:...",
    "policy_ref": {...}, "pack_hash": "sha256:..."
  },

  "decision": "accept",
  "option_id": "accept",
  "reason": "...",                      // 必填，规则见 3.4
  "payload": {...},

  "actor": {"principal_id":"u_101","principal_type":"human",
            "delegation_id":"dlg_...", "on_behalf_of": null},
  "authz_snapshot": {"service_id":"S01","source_revision":88,
                     "roles":["director"],"checked_at":"...","watermark":"..."},
  "recorded_at": "2026-10-30T10:12:03.4Z",

  "status": "RECORDED",                 // RECORDED | APPLIED | STALE | REJECTED_INVALID | WITHDRAWN
  "stale_reason": null,
  "validation": null,                   // S02 validate_review_decision 的结果，见 §4
  "downstream": {"cas_request_id": null, "cas_result": null}   // S08 的结果，见 §5
}
```

**`decided_against` 是冻结副本**，不是指向任务的引用。理由：任务被 supersede 后仍然要能回答"这个人当时批的是什么版本"。这与 docs/04 §1"内容对象 immutable；revision 只在对象内有意义"一致。

### 3.3 任务与决定的状态机

任务（`ReviewTask`）：

```
        ┌────────┐  S02 说这个 gate 不再需要
        │ OPEN   │───────────────────────────► WITHDRAWN
        └───┬────┘
            │ 认领 / 直接决定
            ▼
        ┌────────┐  quorum 未满         ┌──────────────┐
        │IN_REVIEW│─── 还有票 ─────────►│ PARTIALLY_DECIDED │
        └───┬────┘                      └──────┬───────┘
            │                                   │ quorum 满
            │ deadline 到                       ▼
            ▼                            ┌─────────────┐
        ┌────────┐                       │ DECIDED     │
        │EXPIRED │                       └──────┬──────┘
        └───┬────┘                              │ 绑定失效
            │ 逾期到达的票                       ▼
            ▼                            ┌─────────────┐
      （只进历史）                        │ SUPERSEDED  │
                                         └─────────────┘
   绑定失效可发生在任意非终态 ──────────► SUPERSEDED
```

决定（`ReviewDecision`）：`RECORDED → APPLIED` 或 `RECORDED → STALE` 或 `RECORDED → REJECTED_INVALID`。**终态不回退。**

各迁移守卫：

| 迁移 | 条件 |
|---|---|
| `OPEN→IN_REVIEW` | 有合格 principal 认领；S01 实时校验其角色 ∈ `assignee_roles` |
| `IN_REVIEW→PARTIALLY_DECIDED` | 收到有效票且 quorum 未满 |
| `*→DECIDED` | quorum 满 **且** 职责分离满足 **且** S02 `validate_review_decision` 返回 `allow` |
| `*→EXPIRED` | 持久 timer 到 `deadline`（见 §7） |
| `*→SUPERSEDED` | 收到 `snapshot.manifest.committed` 且 `node_epoch` / `lock_version` / artifact hash / policy_ref 任一变化 |
| `*→WITHDRAWN` | S02 说该 gate 不再 required（新策略） |
| 决定用 `expected_task_revision` 提交但任务已 supersede | 409 `TASK_SUPERSEDED`，**不落库** |

### 3.4 理由与例外

| 选项 | `reason` 要求 |
|---|---|
| accept / reject | 必填，最少 4 字（防"。"占位） |
| request_repair | 必填，且需至少一个 `defect_id` 或 `repair_hint` |
| exception_accept | 必填，且 ≥ 20 字；同时写 `exception_scope`（本条缺陷范围）与 `expires_at`；S02 校验 `review.exception_accept` 权限 |
| 其他选项 | 按 `options[].effect.requires_reason` |

理由文本进审计（S45），进事件 `data_ref`，**不进 metrics label**（docs/14 §3 禁高基数用户文本）。

---

## 4. 决定校验：`validate_review_decision`（S02 调用）

S40 收票后**先调 S02，不自行判断**：

```
POST S02 /policy-decisions:evaluate
{ kind: "review_decision",
  subject: { review_task_id, decision_id,
             decided_against: <决定里的冻结副本>,
             option_id, actor, payload, reason } }
```

S02 返回（S40 只消费，不重算）：

| S02 verdict | 含义 | S40 行为 |
|---|---|---|
| `allow` | 权限够、条件满足、quorum 规则满足、职责分离通过 | 决定 → `RECORDED`，若 quorum 满则任务 → `DECIDED` |
| `deny` | 明确不允许（角色不匹配、职责分离冲突、选项在安全门上不可用、例外权限缺失） | 决定 → `REJECTED_INVALID`，返回 403/422 + `reasons[].code`，**不落为有效票** |
| `require_human` | 需要第二个人（quorum 未满，或职责分离要求不同角色） | 决定 `RECORDED`，任务 → `PARTIALLY_DECIDED`，继续等待 |
| `abstain` | 依赖不可达 / 证据不全 | 决定**不落库**（不产生票），任务不前移；向上返回 503 可重试 |

**关键：`deny` 与 `abstain` 的决定都不进入 quorum 计数。** 否则"五个没有权限的人点接受"就能凑满 quorum；而"未知"被记成一张有效票，等价于把未知当通过（E25）。

`abstain` 与 `deny` 的区别：`deny` 落库为 `REJECTED_INVALID`（审计需要"有人试图越权"的证据），`abstain` 不落库（没有决定内容可言，只有一次失败的调用，记在调用日志里）。

`authz_snapshot` 必须持久化：一次投票的有效性以**投票时刻**的授权为准，但审计要能重建那一刻。

---

## 5. 决定生效：S08 CAS

quorum 满且 S02 判 `allow` 后：

```
S40 → S08  request_acceptance_CAS
  { manifest_ref, expected_revision, node_id, node_epoch, lock_version,
    artifact_ref, policy_ref, decision_refs: [rd_1, rd_2], command_id }
S08 → S40  { result: "committed" | "conflict" | "stale_epoch" | "locked" }
```

| S08 结果 | S40 行为 |
|---|---|
| `committed` | 所有相关决定 → `APPLIED`；任务 → `DECIDED`；发 `review.decision.recorded` |
| `conflict` (revision 不符) | 决定 → `STALE`，`stale_reason=MANIFEST_ADVANCED`；任务 → `SUPERSEDED`；**必要时按新 manifest 重建任务** |
| `stale_epoch` | 决定 → `STALE`，`stale_reason=NODE_EPOCH_ADVANCED` |
| `locked` | 决定 → `STALE`，`stale_reason=NODE_LOCKED`；提示调 `policy.unlock` |

**`APPLIED` 只能由 S08 的成功结果赋。** 不得因为"我们觉得应该能过"就写。这是 S40 最容易造第二权威的一步。

### 5.1 失败重试

S08 不可达 → 决定保持 `RECORDED`，进入本地持久重试队列（`outbox` + 重试表），**不重复扣 quorum、不重复计票**。重试用同一 `command_id`（S08 幂等）。持续失败超过阈值 → 告警 + 任务标记 `DOWNSTREAM_BLOCKED`，仍不改 `RECORDED` 状态，**绝不降级为本地接受**。

---

## 6. Stale 判定：旧审核晚到怎么挡住（核心）

### 6.1 四道闸，缺一不可

旧审核晚到有四种形态，都会发生：

| 形态 | 场景 | 挡住它的闸 |
|---|---|---|
| A. 旧 epoch 的任务被批 | 提出后 epoch 从 4 升到 5 | **决定提交时**的 binding 校验 |
| B. 任务本身已 supersede，人还在旧页面上点 | UI 未刷新 | `expected_task_revision` → 409 |
| C. 决定通过校验后、CAS 前 epoch 变了 | 竞态窗口 | **S08 的 CAS 条件**（最后一道，也是唯一权威） |
| D. quorum 投票跨越了一次 epoch 变化 | 第 1 票在 epoch 4，第 2 票在 epoch 5 | **每次投票各自校验** + quorum 汇总时再验一次 |

**核心原则：epoch 的所有权在 S08，S40 的判定只是"提前劝退"，不是"保证"。** 所以 S40 必须**同时**做本地判定（给用户快速反馈）和依赖 S08 CAS（给正确性）。

### 6.2 提交时判定（闸 A / B）

```python
def on_decision_submit(task, req, ctx) -> DecisionResult:
    # 闸 B：任务版本（乐观并发）
    if req.expected_task_revision != task.revision:
        raise Conflict(409, "TASK_SUPERSEDED",
                       current=task.revision, current_state=task.state)

    if task.state in ("SUPERSEDED", "EXPIRED", "WITHDRAWN", "DECIDED"):
        return record_history_only(req, stale_reason=f"TASK_{task.state}")

    # 幂等：同 command_id 同 body → 返回原决定，不重复计票
    if (prior := find_idempotent(task.tenant_id, req.command_id)) is not None:
        if prior.request_hash != hash(req): raise Conflict(409, "IDEMPOTENCY_MISMATCH")
        return prior

    # 同一用户重复点击 = 同一次投票（docs/06 §3），同 command 也算
    if existing_vote := find_vote(task, req.actor.principal_id):
        if existing_vote.option_id == req.option_id:
            return existing_vote            # 幂等，不重算 quorum
        raise Conflict(409, "DUPLICATE_VOTE_DIFFERENT_OPTION",
                       prior_option=existing_vote.option_id)

    # 闸 A：绑定的所有维度（用上下文与任务冻结副本比对）
    mismatches = compare_binding(task.binding, req.context)
    if mismatches:
        return record_history_only(req, stale_reason="BINDING_MISMATCH",
                                   detail=mismatches)      # 决定 status=STALE

    # 闸 D（投票时那部分）：这个投票自身的授权
    authz = S01.get_authorization_context(req.actor)      # 实时，不缓存
    if not authz.valid or not set(task.assignment.assignee_roles) & set(authz.roles):
        return reject_invalid(req, code="NOT_AUTHORIZED_FOR_TASK")

    # 交给 S02 判权限/条件/quorum 规则
    verdict = S02.validate_review_decision(task, req, authz)
    ...
```

### 6.3 `compare_binding` 的确切规则

**硬绑定（变了就 stale，没有例外）：**

```python
STALE_IF_DIFFER = [
    ("manifest_ref.id",            "MANIFEST_CHANGED"),
    ("execution_fence",            "EXECUTION_FENCE_ADVANCED"),   # 取消/灾备切主
    ("node_epoch",                 "NODE_EPOCH_ADVANCED"),
    ("lock_version",               "NODE_LOCK_CHANGED"),
    ("artifact.content_hash",      "ARTIFACT_REPLACED"),
    ("policy_ref.content_hash",    "POLICY_SUPERSEDED"),
    ("pack_hash",                  "EVIDENCE_CHANGED"),
]
```

`manifest_ref.revision` / `content_hash` **不在这张表里**，因为它需要节点级差异判断才能精确处理（§6.4）。这是本方案唯一一处"不是无条件 stale"的绑定，也是唯一需要额外能力的绑定。

特别说明两条容易写错的：

- **`execution_fence` 变了也必须 stale**，哪怕 `node_epoch` 没变。docs/04 §1：`execution_fence` 是 run/branch 级的取消与主写切换；旧 fence 下的任何产物（包括人工决定）都不能作用在当前执行面。AGENTS.md 明确"旧 epoch 的结果永远不能推进当前接受指针"，这里 `execution_fence` 与 `node_epoch` 不能混用，但**两者都是"过期就作废"**。
- **`policy_ref` 变了也 stale**。理由：如果策略从 `manual` 收紧成更严的规则，或者 gate 要求变了，依据旧策略作出的决定不该自动适用。docs/06 §4 "在下一安全 checkpoint 应用，只影响未准入动作"——人工决定是"未准入动作"，所以应当在旧策略下作废重来。反向（策略放宽）也一样作废，因为"放宽"不能追溯使旧决定变成多余。

### 6.4 决定入库时**不是** stale 的边界

有三种情况**看起来像 stale 但不该 stale**：

1. **manifest revision 前进但 node_epoch 未变**。docs/04 §1：改别的镜头只提升受影响的 node_epoch。若本节点没变、artifact hash 没变，决定依然有效。所以 `STALE_IF_DIFFER` 里 `manifest_ref.revision` 单独成一条是**故意保守**的吗？——**不是。** 这里必须**精确**：

```python
# 正确版本：manifest revision 前进本身不 stale，只有"影响本节点的东西变了"才 stale
def compare_binding(task, ctx) -> list[Mismatch]:
    out = []
    # 硬绑定：任何变化都使决定失效
    for f in ("execution_fence","node_epoch","lock_version"):
        if get(ctx, f) != get(task.binding, f): out.append(Mismatch(f, R[ f ]))
    if ctx.artifact.content_hash != task.binding.artifact.content_hash:
        out.append(Mismatch("artifact.content_hash","ARTIFACT_REPLACED"))
    if ctx.policy_ref.content_hash != task.binding.policy_ref.content_hash:
        out.append(Mismatch("policy_ref.content_hash","POLICY_SUPERSEDED"))
    if ctx.pack_hash != task.binding.pack_hash:
        out.append(Mismatch("pack_hash","EVIDENCE_CHANGED"))
    # 软绑定：仅当 manifest 的 content_hash 与"影响本节点"的子集不一致时才算
    if ctx.manifest_ref.id != task.binding.manifest_ref.id:
        out.append(Mismatch("manifest_ref.id","MANIFEST_CHANGED"))
    elif not node_subset_unchanged(ctx.manifest_ref, task.binding.manifest_ref, task.binding.node_id):
        out.append(Mismatch("manifest_ref","MANIFEST_AFFECTS_NODE"))
    return out
```

`node_subset_unchanged` 调用 S08 的只读比较（或其发布的 `snapshot.manifest.committed` 事件里的 node 差异），核对：本节点的 `input_fingerprint`、`accepted_artifact_ref`、`locked` 未变。**这依赖 S08 提供节点级差异能力——登记为缺口 G3。**

保守退化：若 S08 没有该能力，则退回"manifest revision 一变就 stale"。这**更严**，可以接受；反过来（放宽成"revision 变了但大概没事"）不可接受。

2. **dedup：同一人重复点击**。是幂等，不是 stale。
3. **审核在 deadline 之后到达，但绑定完全没变**。此时任务 → `EXPIRED`，决定 → `STALE`，`stale_reason=DEADLINE_PASSED`。仍然不推进指针（docs/06 §3 "过期任务、旧epoch决定返回stale，不更新当前版"）。但**允许**按同一 binding 建**新任务**并继承理由文本——人不用重新描述，只需再点一次。这是产品上唯一可接受的"复用"。

### 6.5 闸 C：最后一道是 S08

竞态窗口演示（必须写成测试）：

```
t0  S40 比对 binding：全部一致 → RECORDED
t1  S07 提交局部修改，S08 提交新 manifest，node_epoch 4→5
t2  S40 调 S08 CAS，带 node_epoch=4
t3  S08 检查 expected node_epoch=4 ≠ 当前 5 → 返回 stale_epoch
t4  S40 决定 → STALE；任务 → SUPERSEDED
```

S40 在 t0 的判定**没有错**——它反映的是那一瞬间。错误只可能来自"S40 以为自己的判定能替代 CAS"。所以：

> **不变量：S40 从不因为本地判定通过就写 `APPLIED`，也不发"已接受"事件。`APPLIED` 与 `review.decision.recorded` 的语义是"记录了一个决定"，与"已接受"必须区分开。**

事件命名陷阱：`review.decision.recorded` 里的 `recorded` 是刻意的——它记录的是**决定**，不是接受结果。消费者不得把它当"已接受"用。

### 6.6 旧审核的最终归宿

```
旧 epoch 的决定 ──► 历史上存着，status=STALE
                     ├─ 可在 UI "历史候选" 里看到（docs/06 §4）
                     ├─ 可被审计引用（S45）
                     └─ 永远不参与当前 accepted pointer 的任何计算
```

任何查询接受状态的代码路径都**不该**读 `ReviewDecision`。它读 S08。S40 只是证据。

---

## 7. 超时

| 模式 | 超时行为（docs/06 §3、docs/11 §3） |
|---|---|
| `guided` | `WAITING_REVIEW`，一直等（不自动批准） |
| `stage_review` | `WAITING_REVIEW` |
| `exception_assisted` | `WAITING_REVIEW` |
| `unattended_batch` | 不建人审任务；不可解的必要分支 `BLOCKED`，其余继续；Run 可结束 `INCOMPLETE` |

实现：

- `deadline` 用**持久 timer**（Temporal timer / DB 扫表二者选一，见 §9.1），**不在 API 进程里 sleep**（docs/07 §4）。
- 超时只改状态、发事件，**不自动接受**。任何"无人审就默认通过"的配置项都不存在。
- `unattended_batch` 必须在**策略解析阶段**就确定：这个 gate 会 `require_human` 的话，Run 开始时就该知道它只能产出 `INCOMPLETE`，或该节点走 `isolate_branch`。不要"跑到一半才发现要人等"。

---

## 8. 派单前实时复查（S40 关心什么）

S40 本身不派发生成任务，但它参与两个"再确认"点：

### 8.1 建任务时

| 复查 | 权威 | 失败 |
|---|---|---|
| gate 是否仍 required | S02 | 不建任务（或建了立刻 `WITHDRAWN`） |
| 所指 artifact 仍存在且 hash 匹配 | S28/S26 只读 | 建 `evidence_abstain` 类任务，而不是让人审一个不存在的版本 |
| `reviewer_roles` 在本租户有在职成员 | S01 | 建任务但标记 `NO_ELIGIBLE_ASSIGNEE` + 告警（**不要**自动放宽角色） |

### 8.2 决定提交时（= 释放到 S08 之前的最后一次）

| 复查 | 权威 | 失败 |
|---|---|---|
| actor 授权仍有效、角色仍匹配、委派未过期 | S01 实时 | `deny AUTH_REVOKED` → `REJECTED_INVALID`（E27） |
| 职责分离：批准预算的人 ≠ 批准创作的人 | S02（用双方 decision 的主体） | `require_human`（要换人） |
| 例外权限仍在 | S01/S02 | `deny` |
| 成本估算未过期（若选项带成本） | S25/S23 | 该选项不可选，要求刷新任务 |
| gate 仍 required 且未 SATISFIED | S02 | `deny GATE_ALREADY_SATISFIED`（幂等友好） |

**复审失败不能"用旧快照继续"。** 例如 actor 在 10:00 有 `director`，10:05 被撤销；他在 10:06 点接受 → 必须拒。这就是 E27 在 S40 的落点。

---

## 9. 动态切换与人工接管

### 9.1 实现载体

- `deadline` 与 `reminder_at`：优先 Temporal timer（与 docs/07 的执行层级一致）；若 S40 不跑 Temporal worker，则用 DB 扫表 + `SELECT ... FOR UPDATE SKIP LOCKED`，扫表间隔 ≤ 15s 且必须幂等（同一 `review_task_id` 只能产生一次 `EXPIRED`）。
- `snapshot.manifest.committed` 消费：按 `aggregate_id`(manifest) 顺序；乱序用 sequence 缓冲 + 回源；缺口不无限等待。
- 缓存：任务列表可缓（投影），**单任务的决定写入路径绝不缓**。

### 9.2 运行中改模式对已建任务的影响

| 变化 | 已建任务 |
|---|---|
| 策略变严（新增 gate / 角色收窄） | 仍 required 的任务保留；`assignee_roles` 收紧后，**已投的票若来自不再合格的 actor → 该票作废**（`REJECTED_INVALID`，`stale_reason=POLICY_ROLE_NARROWED`），重开投票 |
| 策略放宽（gate 不再 required） | 任务 → `WITHDRAWN`，**已投的票不转成自动接受**；gate 由新的自动路径满足，且必须在证据里记"因策略变更而自动通过" |
| `policy_ref` 变化 | 按 §6.3 **全部现有任务 → `SUPERSEDED`**（新策略下重新建） |

第 3 条会让人觉得"太严"。但放开它就是允许"旧策略下的人的意图"在"新策略"下生效，而人并不知道新策略是什么。docs/06 §4 的原则是"只影响未准入动作"，人工决定属于未准入动作。

### 9.3 人工接管（manual_takeover）

docs/06 §4：用户可在任意节点暂停、改参数、重写对白、替换媒体、选候选、分支试做、解锁。

S40 承载的是"记录这次接管"：

- `kind=manual_takeover` 的任务，`options` 是接管动作清单（pause / edit_params / rewrite_dialogue / replace_media / select_candidate / branch_trial / unlock）。
- **S40 只记录"谁在什么版本上做了什么选择"**；实际执行由 S06/S07/S08/S21 完成。
- 接管必然改变 `input_fingerprint` 或 `node_epoch` → 触发 S08 新 manifest → 所有该节点的旧任务 `SUPERSEDED`。这是**自动挡住的**，不需要 S40 主动清理。
- 接管要过权限：`edit_params` / `rewrite_dialogue` 是创作权限，`unlock` 是 `policy.unlock`，`replace_media` 涉及资产权利。S40 不判断，交 S02/S01。

---

## 10. 存储、事件、幂等、契约缺口

### 10.1 表

```sql
review_task(
  tenant_id, review_task_id, kind, state, revision,
  binding jsonb, subject jsonb, reason jsonb, evidence jsonb, options jsonb,
  assignment jsonb, pack_hash, deadline, created_at, created_by jsonb, source_event jsonb,
  PRIMARY KEY (tenant_id, review_task_id)
) -- binding/pack_hash 建立后不可变；UPDATE 仅限 state/revision/assignment

review_decision(
  tenant_id, decision_id, review_task_id, command_id, request_hash,
  decided_against jsonb, decision, option_id, reason, payload jsonb,
  actor jsonb, authz_snapshot jsonb, recorded_at,
  status, stale_reason, validation jsonb, downstream jsonb,
  PRIMARY KEY (tenant_id, decision_id),
  UNIQUE (tenant_id, command_id)
)
CREATE UNIQUE INDEX ON review_decision (tenant_id, review_task_id,
        (actor->>'principal_id')) WHERE status IN ('RECORDED','APPLIED');   -- 一人一票

review_quorum_state(
  tenant_id, review_task_id, required, separation_of_duties,
  votes_valid, votes_invalid, must_include_roles jsonb, satisfied_at,
  PRIMARY KEY (tenant_id, review_task_id)
)

review_assignment_event(tenant_id, review_task_id, seq, kind, from_principal, to_principal,
                        reason, at)   -- 分派/认领/委派/收回，只 INSERT
```

**基础设施表**（与 [S08 §6.4](S08-revision-snapshot.md) 同构）：

```sql
CREATE TABLE human_review.idempotency_record (
  tenant_id text NOT NULL, command_id text NOT NULL, request_hash char(64) NOT NULL,
  command_type text NOT NULL, state text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code integer, response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE human_review.outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id text NOT NULL, aggregate_id text NOT NULL  /* review_task_id */, aggregate_sequence bigint NOT NULL,
  event_type text NOT NULL, partition_key text NOT NULL, payload jsonb NOT NULL,
  headers jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON human_review.outbox (id) WHERE published_at IS NULL;

CREATE TABLE human_review.inbox (
  consumer text NOT NULL, event_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

**`review.decision.recorded` 与 `review_decision` 行必须同一本地事务。** 这是 §5「决定生效」
的前提：若事件先于落库发出，S08 会收到一个查不到的决定；若落库先于事件，决定永久停在
`RECORDED` 而无人推动下游。

`aggregate_id = review_task_id`，`aggregate_sequence = review_task.revision`。

**注意 S40 的 outbox 承担两类写入，不要混：**

| 用途 | 语义 | 对应 §5.1 |
|---|---|---|
| 发布 `review.decision.recorded` | 与决定落库同事务的正常 outbox | 主路径 |
| S08 不可达时的重试 | `status` 停在 `RECORDED`，**按同一 `command_id` 重试 S08** | 失败路径 |

第二类**不是** outbox 重发：那是本地持久重试队列，重试的是对 S08 的 CAS 调用，不是重发事件。
两者共用 `outbox` 表容易在实现时混淆，所以 §5.1 的重试表单独记录 `attempt_count` 与
`next_attempt_at`，且**绝不**因为 S08 不可达而把决定降级为本地接受。

### 10.2 事件

| 方向 | 事件/调用 |
|---|---|
| 发布 | `review.decision.recorded`（topic `drama.human-review.v2`，key `tenant_id:aggregate_id`） |
| 建议补充（缺口 G4） | `review.task.created`、`review.task.state_changed`、`review.task.expired` |
| 消费 | `snapshot.manifest.committed`（S08，supersede 判定）、`policy.revision.published`（S02，withdraw / supersede）、`access.grant.changed`（S01，票失效） |
| 调用 | S02 `validate_review_decision`；S08 `request_acceptance_CAS`；S01 `get_authorization_context` |

事件 payload **不含**理由全文与预览 URL（敏感），用 `data_ref`（docs/04 §4）。`review.decision.recorded` 的 `data` 含 `decision_id / review_task_id / decided_against / status / stale_reason`，不含 `reason` 原文。

### 10.3 幂等

- 写命令：`tenant_id + command_id + request_hash`；同键不同 body → **409**（匹配 OpenAPI `Idempotency-Key`）。
- **同一用户重复点击算同 command**（docs/06 §3）：先查 `(tenant, review_task_id, principal_id)` 的有效票；选项相同 → 返回原决定；选项不同 → 409。
- `review.decision.recorded` 重复投递 → 消费者 Inbox 去重；S40 自己不因重放重复调 CAS（CAS 请求也带 `command_id`，S08 幂等）。

### 10.4 契约缺口（报告，不擅自改）

| # | 缺口 | 影响 | 临时路径 | 建议 |
|---|---|---|---|---|
| G1 | `decideReview` 的请求体只允许 `decision ∈ {accept, reject, request_repair}` + 4 个字段，`additionalProperties: false` | docs/06 §3 与 docs/11 §3 要求 accept/reject/request_repair/**replace**/**change_spec**/例外接受，无法表达 | 用 `decision` 三值 + 内部 `option_id`（走 `payload` 会被 schema 拒；因此要么先上游加字段，要么临时把 `option_id` 塞进 `reason` 前缀——**后者不可接受**） | 加可选 `option_id` / `payload` / `expected_task_revision`（additive） |
| G2 | 无 `GET /review-tasks/{id}` 单条查询（只有列表） | UI 无法按 id 拉冻结快照 | 列表接口按 id 过滤 | 加单条 GET |
| G3 | S08 无"节点级 manifest 差异"只读能力 | §6.4 的精确 stale 判定做不了，只能退化为"revision 变就 stale" | 保守退化（更严） | S08 提供 node diff 或事件里带 node 变更集 |
| G4 | 事件只声明 `review.decision.recorded` | 待审队列、超时告警无法事件驱动 | 轮询 | 补 task 生命周期事件 |
| G5 | `ReviewTask` 无 schema 文件（只有 OpenAPI 里的一行 `Operation`） | 任务/决定结构没有机器可读契约 | 本文 §2/§3 作为待固化草案 | 新增 `review-task.v2.schema.json` / `review-decision.v2.schema.json` |
| G6 | quorum 与职责分离只在 docs/06 §3 出现，无字段定义 | S02 `reviewer_roles` 只有角色列表，无 quorum | 任务级 `assignment.quorum` 先由 S02 的 gate 规则派生 | S02 gate 规则里补 quorum/SoD 字段 |

---

## 11. 测试清单

> 全部可在不接真实模型的前提下跑；跨服务部分用契约 mock。

### E05 —— 四档模式门一致

| 用例 | 期望 |
|---|---|
| E05-01 | 同一 artifact 在 4 档下生成的 `reason.code` 与 `options` 分别符合该档语义（`guided`/`stage_review`/`exception_assisted` 有任务；`unattended_batch` 对该 gate 不建任务或建 `isolate_branch` 任务） |
| E05-02 | `acceptance_mode=manual` 时必然有任务；`auto_if_qc_pass` 且 QC pass → **无任务**（且不因"顺手建一个"而产生） |
| E05-03 | 项目/集/阶段/节点四层覆盖后，任务里的 `assignee_roles` 与 `options` 反映的是**有效策略**而非企业默认 |
| E05-04 | 同一 gate 在阶段层要求 `director`、节点层要求 `director+producer` → 任务 quorum 反映合并结果 |

### E07 —— 手工锁定与旧审核晚到

| 用例 | 期望 |
|---|---|
| E07-01 | 任务绑定 epoch=4；S08 提交 epoch=5 后提交决定 → `STALE`，`stale_reason=NODE_EPOCH_ADVANCED`；S08 未被调用 |
| E07-02 | 同上，但决定在校验通过后才发生 epoch 变化（mock S08 返回 `stale_epoch`）→ `APPLIED` **未**被写入；决定 `STALE` |
| E07-03 | `execution_fence` 变化而 `node_epoch` 不变 → 仍 `STALE`（`EXECUTION_FENCE_ADVANCED`） |
| E07-04 | `lock_version` 变化 → `STALE`；且不尝试 CAS |
| E07-05 | `pack_hash` 变化（证据被替换）→ `STALE EVIDENCE_CHANGED` |
| E07-06 | `policy_ref` 变化 → 现有任务全部 `SUPERSEDED`；其后的票 → `STALE` |
| E07-07 | 旧任务页面用 `expected_task_revision=1` 提交而任务已 revision=2 → 409，不落库 |
| E07-08 | 同一 epoch 下 manifest revision 前进但本节点 `input_fingerprint` 未变（mock node diff 能力）→ **不 stale**，决定有效 |
| E07-09 | `stale` 决定不进 quorum 计数；两人投票其中一人 stale → quorum 仍缺 |
| E07-10 | 极重要：**任何 stale 路径下 S08 的 accepted pointer 不变**（用 S08 mock 断言无 CAS 调用或 CAS 失败） |
| E07-11 | 同一人用不同 command_id 重复点 accept → 幂等，只有一个有效票 |
| E07-12 | 同一人先 accept 后 reject（不同 command）→ 409 `DUPLICATE_VOTE_DIFFERENT_OPTION` |

### E25 —— 评估器超时与冲突要 abstain，不能当 pass

| 用例 | 期望 |
|---|---|
| E25-01 | 收到 `kind=evidence_abstain`（QC 全 abstain）→ 建任务，且 `options` 里**没有**"自动通过" |
| E25-02 | 收到 `conflict_arbitration`（两份报告冲突）→ 任务证据里两份报告都在，带 `conflict` 标记 |
| E25-03 | 任务超时（`unattended_batch`）→ 分支 `BLOCKED`，Run `INCOMPLETE`；**不因超时生成 accept 决定** |
| E25-04 | 任务超时（其他三档）→ `WAITING_REVIEW`，不产生决定 |
| E25-05 | 超时后到达的 accept 且 binding 未变 → 决定 `STALE DEADLINE_PASSED`，指针不变 |
| E25-06 | S02 对某决定返回 `abstain`（依赖不可达）→ 决定不落库，不进 quorum，不调 S08，返回 503 |
| E25-07 | `options[].effect.advances_pointer` 在任何情况下都不得由 S40 置 true（静态断言） |

### E27 —— SSO 撤权及时生效

| 用例 | 期望 |
|---|---|
| E27-01 | 用户投票后、CAS 前被撤权 → 提交时实时校验拒绝 `REJECTED_INVALID AUTH_REVOKED` |
| E27-02 | 投票时刻已撤权 → 拒绝，票不入 quorum |
| E27-03 | 委派（delegation）过期 → 拒绝，`stale_reason` 不适用（是 invalid 不是 stale） |
| E27-04 | 近百人同时投票，其中 1 人被撤权 → 只影响他那票，quorum 重新计算 |
| E27-05 | 角色被回收后，其已投的票 → `REJECTED_INVALID POLICY_ROLE_NARROWED`（§9.2），quorum 重算 |

### 一致性与恢复

| 用例 | 期望 |
|---|---|
| C-01 | 跨租户读任务 → 404（不泄漏存在性） |
| C-02 | 同 `command_id` 同 body 重投 → 同 `decision_id`，不重复计票 |
| C-03 | 同 `command_id` 不同 body → 409 |
| C-04 | `snapshot.manifest.committed` 重复/乱序 → supersede 判定幂等，任务状态不倒退 |
| C-05 | S08 不可达 → 决定 `RECORDED` + 重试队列；恢复后一次成功；期间不产生第二个 CAS 副作用 |
| C-06 | S08 持续不可达超阈值 → 告警，`DOWNSTREAM_BLOCKED`，**不降级为本地接受** |
| C-07 | 重启后：未决定任务仍在、timer 重建、不重复超时 |
| C-08 | `review.decision.recorded` 重放（side_effects=false）不触发再次 CAS |
| C-09 | 两人并发投票之最后一个 → quorum 只满足一次，只调一次 CAS |
| C-10 | 职责分离：同一人批 `budget_increase` 又批 `final_master` → S02 返回 `require_human`，不进 quorum |

---

## 12. 未验证与已知风险

- 全部 SLO 未实测（`service.yaml` `verified: false`）。
- §6.4 的精确 stale 判定依赖 S08 提供节点级差异（缺口 G3）。退化方案（revision 变即 stale）更严，可接受但会产生"无谓的重审"，需要产品确认可接受的误报率。
- §9.2 "策略变化使现有任务 SUPERSEDED" 在策略频繁微调的项目里会造成审核返工。本方案选择正确性优先；若实测代价过高，**唯一允许的放宽是"仅当 `required_gates` / `reviewer_roles` / quorum 实际变化时 supersede"**，不允许放宽到"策略 revision 变了但忽略"。
- 缺口 G1（`decideReview` 无法表达 6 种决定）是**阻塞实现**的：当前 schema 下 `replace` / `change_spec` / 例外接受只能靠改契约或塞进 `reason`。建议优先解决。
- 缺 G5（无 review-task schema）：§2/§3 的结构是本文档提案，需固化为契约后再生成代码。
- `quorum` 与职责分离的规则归属（S02 gate 规则 vs S40 任务字段）需要与 S02 所有者对齐；本方案假定规则在 S02、状态在 S40。
