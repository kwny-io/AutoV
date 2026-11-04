# S02 · 治理与自动化策略 —— 实现方案

状态：`design_only` 服务的实现规格。本文不含业务代码，目标是"照此可以直接写"。所有容量与 SLO 数字仍是 docs/03 的规划值，**未实测**。

依据：docs/06（核心）、docs/04 §1–§2、docs/07 §1、docs/11、docs/12、docs/14 §1、docs/16 §5、`contracts/automation-policy.v2.schema.json`、`contracts/examples/automation-policy.json`、`contracts/examples/service-interactions.yaml`、`contracts/execution-context.v2.schema.json`、`contracts/episode-manifest.v2.schema.json`。

---

## 0. 一句话边界

S02 只回答两个不同的问题，实现上必须**分开成两个 API、两份数据、两种缓存策略**：

| 问题 | 产物 | 是否可缓存 | 时间敏感 |
|---|---|---|---|
| "这个范围内的**规则**是什么？" | `EffectivePolicy`（纯函数结果） | 可缓存，按内容哈希定键 | 否 |
| "**这一次动作**现在能不能放行？" | `PolicyDecision`（一次性凭证） | **绝不缓存** | 是（毫秒级） |

把这两件事合成一个"evaluate 返回 allow"的接口，就是"策略批准 ≠ 放行"这条硬约束被破坏的地方。docs/06 §4 要求"账号撤销、资产/声音授权撤销、kill switch、数据驻留、实时预算余额在每次 dispatch 前重新检查"——这些都不是策略内容，是**决策时刻的外部事实**。

---

## 1. 责任与明确不负责

**唯一写者**（本服务凭证独占写）：

- `AutomationPolicyRevision` —— 不可变策略版本
- `GateRule` —— gate 定义（类型、必需角色、quorum、SLA、超时行为）
- `PolicyDecision` —— 决策记录（allow / deny / abstain / require_human）
- `PolicyException` —— 有时限的例外授权
- 策略发布指针（`policy_publish_pointer`）

**明确不写**（写了就是造第二权威，违反 AGENTS.md）：

| 事实 | 权威 | S02 的正确做法 |
|---|---|---|
| 接受指针 / manifest / node_epoch / execution_fence | S08 | 只读消费，或在决策里绑定调用方给的 epoch 并要求下游 CAS 校验 |
| 金额 / reservation / 余额 | S23 | 调用只读接口拿实时 `available` |
| 人工决定内容、审核任务、委派 | S40 | 只判定"这个决定是否满足权限与条件" |
| 租户/角色/授权/kill switch | S01（含 cell/provider 级停用读 S22） | 每次决策实时读取，读不到即 deny |
| 资产/音色权利状态 | S10–S15 | 实时读取版本与吊销标记 |
| 配额 permit | S22 | 只在决策里声明"需要 permit"，不代为持有 |

S02 不产生任何付费副作用，不发起模型调用，不发布对外通知。

---

## 2. 概念模型：`mode` 是预设，两个开关才是执行依据

这是全篇最容易写错的地方，先在数据模型上钉死。

```
mode                : guided | stage_review | exception_assisted | unattended_batch   ← 交互档位（预设）
generation_mode     : manual | auto                                                   ← 开关 A：要不要人按开始
acceptance_mode     : manual | auto_if_qc_pass                                        ← 开关 B：要不要人按接受
```

**决定性规则：**

1. `mode` 只提供**默认值**和**校验矩阵**，永远不反向推导已显式设置的开关。
2. 两个开关各自独立求值、独立继承、独立出现在 `explain` 里。
3. 运行期真正被 S21/S26/S40 读取的是两个开关和 `required_gates`，**不是 `mode`**。`mode` 只用于 UI 展示、默认值、以及兼容性校验。
4. 合法组合矩阵（下表即校验矩阵；标记 ❌ 的组合在发布期 422，在解析期按"收紧"方向修正并写入 `explain[].normalized`）：

| mode | generation_mode | acceptance_mode | exception_behavior |
|---|---|---|---|
| guided | **manual**（auto ❌） | manual / auto_if_qc_pass | wait_review |
| stage_review | manual / auto | **manual** 为默认 | wait_review |
| exception_assisted | auto 为默认 | **auto_if_qc_pass** 为默认 | wait_review |
| unattended_batch | **auto**（manual ❌） | **auto_if_qc_pass**（manual ❌） | **isolate_branch**（强制） |

矩阵里"❌ 但方向是收紧"的两个例子要特别处理，不许静默：

- `unattended_batch` + `acceptance_mode=manual`：语义矛盾（无人值守却要人接受）。**不许放宽成 auto**。解析结果保留 `acceptance_mode=manual`（这是收紧方向），`mode` 降级标记为 `exception_assisted`，`explain` 记一条 `NORMALIZED_MODE_DEMOTED`。这样运行会停在等待审核，而不是假装无人跑完。
- `guided` + `generation_mode=auto`：`guided` 的定义是"用户逐节点启动"，自动生成与之矛盾。发布期直接 **422 `POLICY_MODE_INCONSISTENT`**，不做自动修正——因为这条修正会把"用户明确要自动"悄悄变成"不自动"，属于行为变更，必须让人看见。

> 判据：**修正只能往更保守的方向走，且必须可见。**任何会自动放宽的"修正"都是设计错误。

`mode` 的默认推导只在字段**未显式出现**时生效：

```
DEFAULT_GENERATION_MODE = { guided: manual, stage_review: auto,
                            exception_assisted: auto, unattended_batch: auto }
DEFAULT_ACCEPTANCE_MODE = { guided: manual, stage_review: manual,
                            exception_assisted: auto_if_qc_pass, unattended_batch: auto_if_qc_pass }
DEFAULT_REQUIRED_GATES  = { guided: [script, canonical_asset, storyboard, rough_cut, final_master],
                            stage_review: [script, canonical_asset, storyboard, rough_cut, final_master],
                            exception_assisted: [canonical_asset, final_master],
                            unattended_batch: [final_master] }
```

注意 `required_gates` 的默认值只在该字段**从未被任何层设置**时使用；一旦某层显式设置，合并只做并集，不再回落到 mode 默认值。

---

## 3. 策略字段表

`contracts/automation-policy.v2.schema.json` 是权威形状；下表在它之上补出"哪个字段参与合并、收紧方向是什么、缺省怎么来"。表中 **`join`** 列是合并算子（§4 详解），**`缺省`** 列是任何层都没设置时的取值来源。

| # | 字段 | 类型 | 取值域 | join（收紧方向） | 缺省 | 可出现在哪些层 |
|---|---|---|---|---|---|---|
| 1 | `mode` | enum | 4 档 | `min`（档位序号小=自主性低=更严） | 企业层必须显式 | 全部 |
| 2 | `generation_mode` | enum | manual, auto | `manual` 胜 | mode 默认 | 全部 |
| 3 | `acceptance_mode` | enum | manual, auto_if_qc_pass | `manual` 胜 | mode 默认 | 全部 |
| 4 | `required_gates` | string[] | 7 类 gate | **并集** | mode 默认 | 全部 |
| 5 | `repair_limits.max_business_attempts_per_shot` | int ≥1 | — | **min** | 企业层必须显式 | 全部 |
| 6 | `repair_limits.max_identical_strategy_repeats` | int ≥0 | — | **min** | 企业层必须显式 | 全部 |
| 7 | `exception_behavior` | enum | wait_review, isolate_branch | `wait_review` 胜，再按 mode 归一 | mode 默认 | 全部 |
| 8 | `hard_rules.rights_required` | const true | — | AND（等价于不可变） | true | 全部（改动=422） |
| 9 | `hard_rules.enforce_data_residency` | const true | — | AND | true | 全部（改动=422） |
| 10 | `hard_rules.no_incomplete_formal_delivery` | const true | — | AND | true | 全部（改动=422） |
| 11 | `hard_rules.allow_silent_identity_downgrade` | const false | — | OR（false 胜） | false | 全部（true=422） |
| 12 | `hard_rules.budget_scope_id` | string | S23 预算 scope | **必须等于或严格下属于**祖先 scope；结果取最深 | 企业层必须显式 | 全部 |
| 13 | `overrides[].locked` | bool | — | **OR**（向下粘滞） | false | 全部 |
| 14 | `reviewer_roles` | string[] | S01 角色 id | **交集**；空集=422 | 由 gate 类型推导 | 全部（schema 目前只在 overrides 内） |
| 15 | `allowed_actions` | string[] | 动作白名单 | **交集** | 由 mode 推导 | 全部（**schema 缺口，见 §11**） |
| 16 | `expiry` | datetime | — | 有效窗口求交集，取 min | 企业层必须显式 | 全部（**schema 缺口**） |
| 17 | `policy_version` | string | — | 不合并，作为来源标识 | 用 `revision+content_hash` | 全部 |
| 18 | `quorum` / `separation_of_duties` | int / bool | — | `max(n)` / `OR` | 1 / 由 gate 类型定 | 全部（**schema 缺口**） |
| 19 | `approved_evaluators` | string[] (版本化 id) | — | **交集** | 企业层列表 | 企业/项目/集（**schema 缺口**） |
| 20 | `qc_min_coverage` | number 0–1 | — | **max** | 企业层 | 企业/项目/集（**schema 缺口**） |

**两个"不是纯格"的字段**要单独对待：

- **#12 `budget_scope_id`**：它是层级引用，不是单调数值。join 需要 S23 的预算 scope 树，且结果依赖顺序（"必须是祖先 scope 的后代"）。因此它是**唯一按顺序校验**的字段：合并时取最深合法值，校验时逐层确认 `is_descendant_of(child, parent)`，任一不成立 → 发布期 422 `BUDGET_SCOPE_ESCAPE`。
- **#14 `reviewer_roles` 与 #18 `quorum`**：纯交集/取大在数学上安全，但**会掩盖非法意图**。某层写 `["finance"]` 而父层只有 `["director"]`，交集为空 → 直接 422，而不是"没人能审"。

---

## 4. 策略继承与合并算法（核心）

### 4.1 三条不变量

- **I1 安全**：任何字段经 join 之后只会更严，不可能更松。这是纯数学性质，不依赖层序。
- **I2 合法**：每层覆盖必须落在祖先允许范围内。**这条不能靠 join 保证**，必须发布期做有序校验（§4.5）。join 是安全网，不是合法性证明。
- **I3 可解释**：每个字段输出"值 + 来源层 + 来源 revision + 采用的算子"，UI 才能回答 docs/06 §2 要求的"为什么需要人工"。

### 4.2 层的来源：不止一条链

v2 schema 把 `overrides[]` 放在**策略文档内部**（见 `contracts/examples/automation-policy.json`：`policy_demo` 在租户层文档里内嵌了 stage 与 node 两层覆盖）。同时 `POST /policies/{id}/revisions` 允许多个 `policy_id`，意味着项目也可以拥有自己的文档。

因此"层"不是一条单一文档链，而是**多文档、多层**的集合。解析时按下面方式收集：

```
SCOPE_CHAIN = [enterprise(tenant), project, episode, stage, node]   # 顺序仅用于 I2 校验与 explain

LayersFor(level) =
    A) 绑定在本层的策略文档的顶层字段（scope_binding 命中 level）
    B) 任何祖先层的策略文档中，overrides[] 里 scope_type+scope_id 恰好命中 level 的条目
    C) 若 level == node，还要包含 stage 层 overrides 中按通配（如 scope_id="shot_3.*"）命中的条目

同一 level 内的多个 layer **没有先后**，必须可交换合并（这正是 join 必须是 commutative 的原因）。
```

> **schema 缺口提示**：`overrides[].scope_type` 只有 project/episode/stage/node，**没有 run**。docs/06 §2 的层级是"企业 → 项目 → 单集/Run → 阶段 → 节点"。当前实现用"episode 级覆盖 + manifest 上的 `policy_ref` pin"近似 run 级，并把 run 级独立覆盖登记为缺口（§11）。**不要自己加一个 `scope_type: "run"` 就上线**——那是 enum 扩张，对严格校验的消费者是破坏性变更。

### 4.3 join 算子定义

每个算子都满足 `join(a,a)=a`（幂等）、`join(a,b)=join(b,a)`（交换）、`join(join(a,b),c)=join(a,join(b,c))`（结合）。这三条保证：**层级缺失、事件乱序重放、投影重建都不会改变结果**。

```python
RANK = {
  # 自主性越高序号越大；join 取最小值 = 最严格
  "mode":               {"guided":0, "stage_review":1, "exception_assisted":2, "unattended_batch":3},
  "generation_mode":    {"manual":0, "auto":1},
  "acceptance_mode":    {"manual":0, "auto_if_qc_pass":1},
  "exception_behavior": {"wait_review":0, "isolate_branch":1},
}

def join_enum(field, a, b):
    return min(a, b, key=lambda v: RANK[field][v])

def join_union(a, b):          # required_gates —— 更多门 = 更严
    return a | b

def join_intersect(a, b):      # allowed_actions, reviewer_roles, approved_evaluators —— 更少选择 = 更严
    return a & b

def join_min_int(a, b):        # repair_limits.*
    return min(a, b)

def join_max_num(a, b):        # qc_min_coverage, quorum
    return max(a, b)

def join_or(a, b):             # locked, separation_of_duties
    return a or b

def join_and(a, b):            # hard_rules 的 const true 项
    return a and b

# 未设置（None）在 join 中作为单位元，直接返回另一侧：
#   join(x, None) == x      ← 这是"缺省不参与合并"的实现方式
# 唯一例外：缺省来自 mode 的默认值时，必须先物化默认值再 join（见 §4.4）
```

`required_gates` 是**并集**（更多门=更严），`allowed_actions` / `reviewer_roles` 是**交集**（更少选择=更严）。两者方向相反，写错一个就等于放行。

### 4.4 解析伪代码

```python
def get_effective_policy(tenant_id, path) -> EffectivePolicy:
    # path = {project_id, series_id, branch_id, episode_id, stage_id, node_id, manifest_ref, node_epoch}
    levels = expand_scope_chain(tenant_id, path)          # [enterprise, project, episode, stage, node]

    # 1) 收集所有 layer，并逐层做"绑定是否过期"检查
    layers = []
    for lvl in levels:
        for layer in LayersFor(lvl):
            rev = load_revision(layer.policy_id, layer.revision)
            if rev.tenant_id != tenant_id: raise CrossTenantRejected()
            if rev.content_hash != layer.content_hash:
                raise RevisionSuperseded(layer)            # 指针漂移：拒绝，不猜
            layers.append(Layer(lvl, rev, layer.fields))

    # 2) 分字段 join（顺序无关）
    acc, prov = {}, {}
    for field in MERGEABLE_FIELDS:
        values = [(l, l.fields[field]) for l in layers if field in l.fields]
        if not values:
            acc[field] = DEFAULTS[field](effective_mode_so_far)   # 只在完全缺失时物化默认
            prov[field] = Provenance(source="default", rule=DEFAULTS_RULE[field])
            continue
        winner = None
        for l, v in values:
            new = MERGE[field](winner, v)
            # 记录"是谁把值变严的"：只有真正改变结果的那一层进 provenance 主记录
            if winner is None or new != winner: prov[field] = Provenance.of(l, MERGE_OP[field])
            winner = new
        acc[field] = winner
        prov[field].also_sources = [l for l, v in values]     # 全量来源，供"为什么需要人工"展开

    # 3) 后置归一与校验（只许往严的方向修）
    normalize_mode_switch_matrix(acc, prov)      # 见 §2 矩阵；修正必写 prov[].normalized
    enforce_mode_exception_behavior(acc, prov)   # unattended ⇒ isolate_branch
    require_nonempty(acc.reviewer_roles, acc.required_gates)   # 空 ⇒ 422 at publish；resolve 期 ⇒ fail closed
    acc.effective_window = intersect_windows(layers)           # [max(start), min(expiry)]

    # 4) 内容寻址
    acc.policy_hash = sha256(canonical_json(acc.mergeable_fields))
    return EffectivePolicy(fields=acc, provenance=prov, sources=[l.ref for l in layers])
```

**关键实现注意：**

1. `DEFAULTS` 里依赖 `mode` 的默认值，必须在 `mode` 求完之后才能物化。因此字段求值顺序固定为：先在缺失时求 `mode`（企业层必须有，否则 422 `ENTERPRISE_POLICY_MISSING`），再求 `generation_mode`/`acceptance_mode`/`required_gates`/`exception_behavior`，再求其余。其余字段与顺序无关，可以并行。
2. `provenance` 记录"最后一层把值变严的来源"是 UI 需要的；全量来源单独存，用于展开解释。
3. **不要**把 `acc` 写成"从企业层开始逐层 apply"的循环。那样在层级缺失或乱序时结果会漂移，而且无法处理同一 level 的多文档合并（§4.2）。

### 4.5 节点级覆盖的合法性校验（发布期）

join 自动挡掉放宽，但**它会静默失效**——用户以为 node 覆盖生效了，实际被上层压住。这是必须显式拒绝的场景。

```python
def validate_revision(policy_doc, previous_revision, catalog, budget_tree):
    jsonschema_validate(policy_doc)

    for ov in policy_doc.overrides:
        parent = get_effective_policy_excluding(policy_doc, ov.scope)   # 祖先层 join 结果

        for field, v in ov.items():
            if field not in MERGEABLE_FIELDS: continue
            if parent[field] is None: continue

            candidate = MERGE[field](parent[field], v)

            # (a) 放宽：join 结果比父层更松 —— 不可能，但显式断言防算子写错
            assert not LOOSER(field, candidate, parent[field])

            # (b) 该覆盖不产生任何效果（意图是放宽，被 join 吃掉）→ 422，不要静默
            if candidate == parent[field] and v != parent[field]:
                raise PolicyError(422, "LOOSENING_NOT_ALLOWED",
                                  field=field, scope=ov.scope, requested=v, enforced=parent[field],
                                  source=parent.provenance[field].source)

            # (c) 角色范围逃逸
            if field == "reviewer_roles" and not set(v) <= set(parent["reviewer_roles"]):
                raise PolicyError(422, "REVIEWER_ROLE_ESCAPE", extra=set(v)-set(parent["reviewer_roles"]))

            # (d) 预算 scope 逃逸（有序校验）
            if field == "hard_rules.budget_scope_id" and not is_descendant_or_equal(v, parent_val, budget_tree):
                raise PolicyError(422, "BUDGET_SCOPE_ESCAPE", requested=v, allowed_root=parent_val)

        if ov.locked is True and previous_revision_locked(ov.scope):
            # 已锁定的范围，只能由具备 unlock 权限的主体改
            require_permission(actor, "policy.unlock", ov.scope)

    # (e) 硬规则不可变
    for k in ("rights_required","enforce_data_residency","no_incomplete_formal_delivery"):
        if policy_doc.hard_rules[k] is not True: raise PolicyError(422, "HARD_RULE_IMMUTABLE", field=k)

    # (f) 模式/开关矩阵
    check_mode_switch_matrix(policy_doc, strict=True)

    # (g) 引用存在性（跨服务只读契约，不读对方库）
    for ov in policy_doc.overrides: assert_scope_exists(ov.scope_type, ov.scope_id, catalog)
```

`expect_revision` 用 CAS：`POST /policies/{id}/revisions` 带 `expected_revision`，与 `policy_publish_pointer` 不等 → 409 `REVISION_CONFLICT`。

### 4.6 合并示例（可写成单测）

企业层：`mode=exception_assisted, generation_mode=auto, acceptance_mode=auto_if_qc_pass, gates=[canonical_asset, final_master], attempts=5, repeats=2, roles=[director, producer, finance]`

项目层：`mode=stage_review, gates=[script], attempts=3`

集层：`acceptance_mode=manual`

节点 `shot_3_2.video`：`generation_mode=auto, acceptance_mode=auto_if_qc_pass, attempts=8`

结果：`mode=stage_review`（项目层胜）、`generation_mode=auto`（节点无冲突）、`acceptance_mode=manual`（集层胜，节点想放宽被 join 吃掉 → **发布期已 422**）、`gates={canonical_asset, final_master, script}`、`attempts=min(5,3,8)=3`、`repeats=2`、`roles=[director,producer,finance]`。

节点那条 `acceptance_mode=auto_if_qc_pass` 若真被提交，必须在发布时被拒——它是"策略只能收紧"这条硬约束的最直接测试点。

---

## 5. 求值 API：`getEffectivePolicy`

`GET /projects/{project_id}/effective-policy`（OpenAPI 已定义）。实现层扩展为下面的请求形状，**路径参数不够，必须带 epoch 上下文**：

```jsonc
// POST /policy-decisions:resolve-effective   （内部/等价入口；路径版只用于 UI 粗查）
{
  "context": {                      // execution-context.v2 子集
    "tenant_id": "...", "project_id": "...", "series_id": "...", "branch_id": "...",
    "episode_id": "...", "manifest_ref": {"id":"...","revision":7,"content_hash":"..."},
    "node_id": "shot_3_2.video", "node_epoch": 4, "execution_fence": 2,
    "policy_ref": {"service_id":"S02","id":"policy_p1","revision":3,"content_hash":"..."}
  },
  "stage_id": "canonical_asset",
  "want": ["fields", "provenance", "gate_plan"]
}
```

输出：

```jsonc
{
  "schema_version": "2.0",
  "policy_ref": {"service_id":"S02","id":"policy_p1","revision":3,"content_hash":"sha256:..."},
  "evaluated_for": {"manifest_ref":{...}, "node_epoch":4, "execution_fence":2},  // 原样回显，用于绑定
  "effective": {
    "mode": "stage_review",
    "generation_mode": "auto",
    "acceptance_mode": "manual",
    "required_gates": ["canonical_asset","final_master","script"],
    "repair_limits": {"max_business_attempts_per_shot":3,"max_identical_strategy_repeats":2},
    "exception_behavior": "wait_review",
    "reviewer_roles": ["director","producer","finance"],
    "allowed_actions": ["generate","select_candidate","request_repair"],
    "hard_rules": {...},
    "locked": false,
    "effective_window": {"from":"2026-10-29T20:00:00+08:00","until":"2026-12-31T00:00:00+08:00"}
  },
  "provenance": {
    "acceptance_mode": {"value":"manual","source":{"scope_type":"episode","scope_id":"ep_03",
                        "policy_id":"policy_p1","revision":2,"content_hash":"..."},
                        "rule":"MIN_RANK","also_sources":[...]}
  },
  "explain": [
    {"code":"GATE_REQUIRED","gate":"script","reason":"enterprise policy requires script gate","action":"why_human"}
  ],
  "normalized": [ {"code":"MODE_DEMOTED","from":"unattended_batch","to":"exception_assisted"} ],
  "gate_plan": [
    {"gate":"script","phase":"pre_dispatch","stop_at":"WAITING_GATE"},
    {"gate":"final_master","phase":"post_generation","stop_at":"WAITING_GATE"}
  ],
  "source_revision": 3,
  "computed_at": "2026-10-29T20:01:02.123Z"
}
```

**语义：**

- 输入 `node_epoch` **不参与合并**（策略不因 epoch 改变），但必须回显并绑定到后续 `PolicyDecision`。epoch 只用于"这个结果是为哪一版算的"，实际并发保护在 S08 CAS。
- `policy_ref` 若传了，则必须是当前有效 revision；不等 → 409 `POLICY_SUPERSEDED`（附当前指针）。这挡住"拿着旧策略去推进"。
- 没有绑定 `policy_ref` 时，按当前发布指针解析，并在响应里给出实际用的 `policy_ref`，调用方必须存下来。
- `effective_window` 与当前时间不交 → 409 `POLICY_EXPIRED`（不是回落到更松的版本）。

### 5.1 缓存

| 缓存对象 | 键 | TTL | 失效 |
|---|---|---|---|
| revision 文档 | `(tenant_id, policy_id, revision, content_hash)` | 无（LRU，按内存上限淘汰） | 无需失效——内容寻址，revision 不可变 |
| 解析结果 | `sha256(tenant_id ‖ policy_content_hash ‖ canonical(scope_path))` | 无（LRU） | 新 revision 产生新 content_hash → 自然新键 |
| 发布指针 | `(tenant_id, scope_binding)` | 5s + `policy.revision.published` 事件主动失效 | 事件驱动 |

要点：

1. **`tenant_id` 必须进键前缀**，即使 `content_hash` 理论上已唯一。防哈希碰撞、也防跨租户存在性探测（docs/16 E28）。
2. 解析结果是 `content_hash + scope_path` 的纯函数，所以**不存在"失效"问题**——任何覆盖变化都会产生新 revision/新哈希。不要做 TTL 驱逐式失效，那会引入"同输入不同输出"。
3. **`PolicyDecision` 不进任何缓存**，包括本地进程内 memo。它是时间点事实。
4. 缓存未命中且回源失败 → **fail closed**：拒绝新增付费动作（service.yaml `recovery` 已写明），已固定策略的读取可用本地缓存继续。

---

## 6. Gate 类型与状态机

### 6.1 Gate 类型与放行条件

schema 枚举 7 种；docs/06 §3 列举的是 6 类（创作接受、资产 canonical、质量例外、预算增加、音色/资产资格、外部分发）。映射关系如下——**"质量例外"和"音色/资产资格"在 schema 里缺失**，见 §11。

| gate | 阶段 | 放行条件（全部满足） | 缺失时停在哪 |
|---|---|---|---|
| `script` | **pre_dispatch** | S04 剧本 revision 已生成；affected nodes 的输入指纹匹配；`acceptance_mode=manual` 时需 ReviewTask 批准 | 不创建 GenerationAttempt（S26 准入直接拒绝，reason=`GATE_UNMET`）；节点 `WAITING_GATE` |
| `canonical_asset` | **pre_dispatch** | 所需的角色/服装/场景/道具/声音 canonical revision 全部已接受；权利版本有效且未吊销；绑定集合 `binding_set_ref` 完整 | 同上；缺资产走 S11–S15 准备分支 |
| `storyboard` | **pre_dispatch** | S06 shot DSL revision 已接受；派生节拍（结尾停顿等）已应用 | 同上 |
| `rough_cut` | post_generation | S41/S42 粗剪 artifact 的 QC 通过（见 §7.3）；人工接受 | attempt 已结算，artifact 保持 unreferenced；工作流等待 signal |
| `final_master` | post_generation | 全片 QC 通过；**所有前置 gate 均已 SATISFIED 且未 SUPERSEDED**；无 severity=S1 未决缺陷；`no_incomplete_formal_delivery` 满足 | 同上；正式交付包不得生成，可出预览包 |
| `budget_increase` | **pre_dispatch** | S23 增量额度已批准；批准主体**具有 finance 权限**且与创作批准主体不同（职责分离） | S26 不得 reserve 超额度；节点 `WAITING_REVIEW` |
| `external_publish` | post_generation | S43 交付包完整；外部分发独立授权；`rights_required` 满足 | 交付物停在"已生成未分发" |
| `quality_exception`（缺） | post_generation | 例外接受需具备例外权限的角色 + 必填原因 + 有时限 | — |
| `asset_eligibility` / `voice_eligibility`（缺） | pre_dispatch | 音色/资产资格未吊销、未过期、未被替换 | — |

**"gate 未满足时任务停在哪"统一回答：**

- **pre_dispatch gate**：不产生预算 reservation，不产生 ProviderJob。阻塞点在 S26 的准入判定，对应节点状态 `WAITING_GATE`（`guided`/`stage_review`/`exception_assisted`）或 `BLOCKED`（`unattended_batch`，且只阻塞该分支）。
- **post_generation gate**：attempt 已结算并归档，artifact 存在于对象存储但**不可被接受指针引用**；工作流挂在持久 timer + callback signal 上，不占 API 进程。
- 两种情况下 Run 都不会被判成 SUCCEEDED；`unattended_batch` 结束时是 `INCOMPLETE` + 失败清单（docs/07 §4）。

### 6.2 GateInstance 状态机

```
                    ┌──────────────────┐
        gate ∉ policy│ NOT_REQUIRED     │
                    └──────────────────┘
   required ┌──────────────┐
   ────────►│ BLOCKED_UNMET│◄──────────── 前置产物缺失 / 上游 gate 未满足
            └──────┬───────┘
                   │ 条件就绪，生成 ReviewTask
                   ▼
            ┌──────────────┐   timeout(deadline)  ┌───────────┐
            │AWAITING_DECISION│──────────────────►│ EXPIRED   │
            └──┬────────┬──┘                       └─────┬─────┘
   accept      │        │ reject / request_repair       │ 超时后到达的决定
               ▼        ▼                                ▼
        ┌───────────┐ ┌──────────┐              （只进历史，不改指针）
        │ SATISFIED │ │ FAILED   │
        └─────┬─────┘ └────┬─────┘
              │            │ 新 epoch / 新 artifact hash / 手工锁定
              ▼            ▼
        ┌────────────┐  ┌──────────────┐
        │ SUPERSEDED │  │ BLOCKED_UNMET│（重新走修复或人工）
        └────────────┘  └──────────────┘
   SATISFIED 还可 ──► WAIVED（仅 PolicyException，有时限、有权限，法定/安全门不可豁免）
```

| 迁移 | 触发者 | 守卫 |
|---|---|---|
| `NOT_REQUIRED→BLOCKED_UNMET` | 策略变更 / 新 run | 策略里出现该 gate |
| `BLOCKED_UNMET→AWAITING_DECISION` | S21/S40 | 前置产物存在且 hash 绑定 |
| `AWAITING_DECISION→SATISFIED` | **S08 CAS 成功后**由事件驱动 | S08 确认 epoch/hash 未变 |
| `AWAITING_DECISION→FAILED` | S40 决定 reject / request_repair | 决定已通过 S02 权限校验 |
| `*→EXPIRED` | 持久 timer | 到 `deadline` |
| `*→SUPERSEDED` | `snapshot.manifest.committed` 且 node_epoch 变化 | 旧 epoch |
| `SATISFIED→WAIVED` | 具备 `policy.exception` 权限的主体 | 不在不可豁免集合内 |

**SATISFIED 的三个绑定**缺一不可：`artifact_content_hash` + `node_epoch` + `policy_ref`。任一变化即 `SUPERSEDED`。这是 E07 的第一道闸。

---

## 7. `PolicyDecision`：策略批准 ≠ 放行

### 7.1 两种对象不要混

| | EffectivePolicy | PolicyDecision |
|---|---|---|
| 性质 | 规则快照，纯函数 | 时间点凭证，一次性 |
| 生命周期 | 与 revision 同寿 | `expires_at`（默认 ≤ 60s，可配到单次 dispatch） |
| 可否复用 | 可，随便读 | **不可**，S26 签发 dispatch grant 时消费一次 |
| 内容 | 字段 + 来源 | 检查清单逐项结果 + 事实来源 + 水位 |

### 7.2 决策 API 与结构

`POST /policy-decisions:evaluate`（service.yaml 的 query 入口）

```jsonc
// 请求
{
  "kind": "dispatch_admission",          // dispatch_admission|acceptance|budget_increase|external_publish|quality_exception
  "command_id": "...",
  "request_hash": "sha256:...",          // 幂等键 tenant+command_id+request_hash
  "context": { ...execution-context 子集... },
  "subject": {
    "attempt_id": "att_...",             // dispatch_admission 时必填
    "artifact": {"id":"...","revision":3,"content_hash":"sha256:..."},   // acceptance 时必填
    "route_decision_ref": {...},         // 用于数据驻留核对
    "budget_scope_id": "budget_ep3",
    "amount_upper_micro": 1200000
  },
  "evidence": {                          // acceptance/quality_exception 时必填
    "qc_reports": [{"service_id":"S35","id":"...","revision":2,"content_hash":"..."}],
    "review_decision": {"task_id":"rt_...","decision_id":"rd_..."}      // 人工门时
  }
}
```

```jsonc
// 响应
{
  "decision_id": "pd_01J...",
  "kind": "dispatch_admission",
  "verdict": "allow",                    // allow | deny | abstain | require_human
  "subject": { ...原样回显，作为绑定... },
  "policy_ref": {"id":"policy_p1","revision":3,"content_hash":"..."},
  "checks": [
    {"name":"policy_not_superseded","result":"pass","source":"S02:pointer@r3"},
    {"name":"authorization_current","result":"pass","source":"S01:ctx@r88","watermark":"2026-10-30T09:00:00Z"},
    {"name":"kill_switch","result":"pass","source":"S01:tenant#suspended=false"},
    {"name":"budget_available","result":"pass","source":"S23:budget_ep3",
     "detail":{"available_micro":9400000,"required_micro":1200000}},
    {"name":"quota_permit_possible","result":"pass","source":"S22:pool_video_a"},
    {"name":"data_residency","result":"pass","source":"route:region=ap-northeast-1"},
    {"name":"rights_current","result":"pass","source":"S10:binding_set@r12"},
    {"name":"node_not_locked","result":"pass","source":"S08:manifest@r7"},
    {"name":"gate_satisfied","result":"pass","gate":"canonical_asset","evidence":"gate_inst_41"}
  ],
  "reasons": [],
  "issued_at": "2026-10-30T09:00:00.100Z",
  "expires_at": "2026-10-30T09:00:30.100Z",
  "single_use": true,
  "decision_hash": "sha256:..."
}
```

**verdict 语义（四值，不许压成布尔）：**

- `allow` —— 所有必需检查 pass，且 `expires_at` 内有效，且 `single_use` 未被消费。
- `deny` —— 有检查 **明确失败**（撤销、kill switch、超预算、锁定、gate 失败）。附 `reasons[].code`，让上游能区分"永久拒绝"与"等待后可恢复"。
- `abstain` —— **无法判定**：证据缺失、评估器超时、依赖不可达、报告冲突。**绝不等于 allow**（E25）。
- `require_human` —— 策略本身要求人工（gate 未满足或 `acceptance_mode=manual`）。S40 据此创建 ReviewTask。

`allow` 的三个消费前提（缺一即失效）：`decision_hash` 校验通过、`expires_at` 未过、`policy_ref` 与调用方 pin 的 policy 一致。

### 7.3 证据聚合规则（E25 落点）

```python
def aggregate_evidence(reports, policy, artifact_hash, deadline_hit) -> Verdict:
    if deadline_hit or len(reports) < len(policy.required_report_kinds):
        return ABSTAIN("EVIDENCE_INCOMPLETE")

    for r in reports:
        if r.artifact_content_hash != artifact_hash:   return ABSTAIN("HASH_MISMATCH")
        if r.evaluator_version not in policy.approved_evaluators:
            return ABSTAIN("EVALUATOR_NOT_APPROVED")   # docs/11：新评估器要先回归+灰度，不追溯
        if r.decision in ("abstain", "human_review"):  return REQUIRE_HUMAN(r.reason)
        if r.decision == "repair":                     return REQUIRE_HUMAN("REPAIR_REQUIRED")
        if r.coverage < policy.qc_min_coverage:        return ABSTAIN("COVERAGE_BELOW_MIN")
        if r.status == "timeout":                      return ABSTAIN("EVALUATOR_TIMEOUT")

    if conflicting(reports):                           # 同 hash 不同结论 / 同一缺陷一方断言一方否认
        return REQUIRE_HUMAN("EVALUATOR_CONFLICT")

    return ALLOW
```

**这条函数的顺序不能改。**"先算平均分再看是否 ≥0.8"是实现者最常犯的错：它把 `abstain`/`timeout` 变成了数值上的中性项，等价于把未知当 pass（docs/11 §1 明确禁止）。

---

## 8. 派单前实时复查清单

`dispatch_admission` 的检查必须**每次都做**，不能因为 Run 已固定 policy 而跳过（docs/06 §4）。

| # | 检查 | 权威 | 实时性要求 | 失败 → |
|---|---|---|---|---|
| 1 | 策略未被紧急收紧取代 | S02 指针 + `safety_tightening` | 读 DB，不走缓存 | `deny` `POLICY_SUPERSEDED` |
| 2 | 策略 `effective_window` 未过期 | S02 | — | `deny` `POLICY_EXPIRED` |
| 3 | 主体授权仍有效（用户/service principal 未撤销、角色未回收、委派未过期） | S01 `GET /authorization-context` | **每次实时**，带 deadline | `deny` `AUTH_REVOKED`（E27） |
| 4 | kill switch（tenant / provider / cell / global） | S01；provider 级读 S22 | 每次实时 | `deny` `KILL_SWITCH` |
| 5 | 数据驻留：实际路由 region 是否被允许 | S25/S26 的 route decision + 策略 | 按**路由结果**核对，不是按意图 | `deny` `RESIDENCY_VIOLATION` |
| 6 | 预算余量：`available = limit - consumed - outstanding_reserved` | S23 | 每次实时，必须含 outstanding | `deny` `BUDGET_EXCEEDED` |
| 7 | 配额池可容纳 | S22 | 只判"是否可能"，不代为持有 | `deny` `QUOTA_EXHAUSTED` |
| 8 | 资产/音色权利未吊销、未过期 | S10–S15 | 每次实时（E30） | `deny` `RIGHTS_REVOKED` |
| 9 | 节点未手工锁定，且 lock_version 与请求一致 | S08 manifest 只读 | 每次实时 | `deny` `NODE_LOCKED` |
| 10 | 所依赖 gate 为 SATISFIED 且绑定当前 epoch/hash | S02 GateInstance | — | `require_human` / `deny` |
| 11 | 修复尝试未超 `repair_limits`（跨 Run 累计，不是本 Run） | S39 计数只读 | 每次实时 | `deny` `REPAIR_EXHAUSTED`（E26） |
| 12 | 人工决定（若为接受类）的 quorum 与职责分离已满足 | S40 | — | `require_human` |

**依赖不可达的处理：`deny`（fail closed），不是 `abstain` 也不是重试到 succeed。** 但要区分可恢复性：

```
RETRYABLE  = {DEPENDENCY_TIMEOUT}            → deny + retry_after，S26 可短暂退避后重试同一 command_id
PERMANENT  = {AUTH_REVOKED, KILL_SWITCH, RESIDENCY_VIOLATION, NODE_LOCKED, RIGHTS_REVOKED}
             → deny，且该 attempt 不得自动重提；转人工
BUDGET     = {BUDGET_EXCEEDED}               → deny + 触发 budget_increase 人工门（不自动提额）
```

**复查失败的回退语义：**

- **在 reservation 之前失败** → 什么都没发生，S26 准入拒绝，节点进 `WAITING_GATE` 或 `BLOCKED`。
- **在 reservation 之后、dispatch grant 之前失败** → S26 必须走补偿：释放 reservation、释放 permit（docs/04 §3 第 5 步）。S02 只负责拒绝签发 grant 并给出 reason code，**不代为补偿**（补偿是 S26 Saga 的责任，金额权威是 S23）。
- **在 grant 已签发、已提交之后** → 复查窗口已过，不再有"撤回"概念。此时走的是已经成立的提交，只能：kill switch 停新 dispatch、对账已提交任务、按渠道能力尝试取消且**不承诺退款**（docs/06 §4）。

**这就是为什么 `PolicyDecision` 必须短寿 + 一次性**：一个 60 秒前签发的 allow 不能给 60 秒后的 dispatch 用。

---

## 9. 动态切换与人工接管

### 9.1 策略在 Run 开始固定

`episode-manifest.policy_ref` + `execution-context.policy_ref` 双处 pin（id + revision + content_hash）。所有决策必须带同一 `policy_ref`。

### 9.2 运行中改模式

```
POST /policies/{id}/revisions  →  新 immutable revision（旧的不改）
                              →  policy.revision.published（Outbox 同事务）
                              →  S02 写 policy_switch_request(run_id, new_revision, requested_checkpoint)
                              →  下一安全 checkpoint 时 S21 调 S02 拿新 effective policy
                              →  S08 提交新 manifest，policy_ref 前进
```

**"下一安全 checkpoint"的具体定义**（必须写死，否则会变成"随时生效"）：

1. 没有 `SUBMITTING` / `SUBMISSION_UNKNOWN` 状态的 attempt；
2. 当前 node 的 `input_fingerprint` 未变；
3. 无未结算的 reservation；
4. 工作流处在节点边界（不切断一个已开始的 provider 等待）。

**生效范围：**

| 对象 | 影响 |
|---|---|
| 已提交供应商的任务 | **继续对账与结算**。策略变严不能让在途任务停付。 |
| 已授权未提交的 attempt | 在 grant 签发时按新策略复查；不通过则 deny + 补偿 |
| 未准入的节点 | 完全按新策略 |
| 已产出的 artifact 与已接受的指针 | **不追溯重判**。已接受的在旧策略下合法，保留并在证据包记录当时的 `policy_version` |
| 已产生的 ReviewTask | 若新策略降低了 gate 要求 → 任务标 `WITHDRAWN`（不自动接受，只是不需要了）；若提高 → 新开任务，旧任务决定仍绑定旧 epoch |

**"历史产物是否重做"：默认不重做。** 需要重做必须显式发起"重审行动"（re-review campaign）：S02 生成影响清单 → S40 批量建任务 → 人决定 → S08 CAS 新 manifest。**不允许**因为改了策略就自动提升 epoch 重跑（那会直接烧钱，且违反"局部修改只提升受影响 node_epoch"）。

### 9.3 人工锁定

`locked=true` 的语义：该范围内的自动覆盖被阻止。

- 锁定由 S08 的 `node_states[].locked` 与 lock_version 承载（S08 是 manifest 权威），S02 的 `locked` 只是**策略意图**，两者必须一致才生效。不一致 → 以更严的一方为准，并告警 `LOCK_STATE_DIVERGED`。
- 锁定时 S02 对 dispatch_admission 返回 `deny NODE_LOCKED`（不是 require_human——锁定意味着连人工自动流程也不该覆盖，必须显式 unlock）。
- 解锁需要 `policy.unlock` 权限，产生新 lock_version；所有绑定旧 lock_version 的 ReviewTask 与 PolicyDecision 立即失效。

---

## 10. 存储、事件、幂等

### 10.1 表

```sql
automation_policy_revision(
  tenant_id, policy_id, revision, content_hash, document jsonb,
  effective_at timestamptz, created_by, created_at,
  PRIMARY KEY (tenant_id, policy_id, revision)
) -- 只 INSERT，永不 UPDATE/DELETE

policy_publish_pointer(
  tenant_id, policy_id, current_revision, updated_at,
  PRIMARY KEY (tenant_id, policy_id)
)

policy_scope_binding(
  tenant_id, scope_type, scope_id, policy_id, revision, content_hash, updated_at,
  PRIMARY KEY (tenant_id, scope_type, scope_id)
) -- scope_type ∈ {tenant, project, episode, stage, node}；run 缺口见 §11

gate_instance(
  tenant_id, gate_instance_id, run_id, manifest_ref_id, node_id, node_epoch,
  gate_type, state, artifact_content_hash, policy_ref_revision,
  review_task_id, deadline, satisfied_at, superseded_reason,
  PRIMARY KEY (tenant_id, gate_instance_id)
) -- UNIQUE (tenant_id, run_id, node_id, node_epoch, gate_type)

policy_decision(
  tenant_id, decision_id, kind, command_id, request_hash,
  subject_hash, policy_ref_revision, verdict, checks jsonb,
  issued_at, expires_at, consumed_at,
  PRIMARY KEY (tenant_id, decision_id),
  UNIQUE (tenant_id, command_id)          -- 幂等
)

policy_exception(
  tenant_id, exception_id, scope_type, scope_id, gate_type,
  granted_by, reason, granted_at, expires_at, max_uses, used_count
)

safety_tightening(                        -- 紧急收紧，绕过常规 checkpoint
  tenant_id, tightening_id, scope_type, scope_id, field, forced_value,
  reason, created_by, effective_from, expires_at
)

CREATE TABLE governance_policy.idempotency_record (
  tenant_id text NOT NULL, command_id text NOT NULL, request_hash char(64) NOT NULL,
  command_type text NOT NULL, state text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code integer, response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

-- 与 S08 §1.6 同构
CREATE TABLE governance_policy.outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id text NOT NULL, aggregate_id text NOT NULL, aggregate_sequence bigint NOT NULL,
  event_type text NOT NULL, partition_key text NOT NULL, payload jsonb NOT NULL,
  headers jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON governance_policy.outbox (id) WHERE published_at IS NULL;

CREATE TABLE governance_policy.inbox (
  consumer text NOT NULL, event_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

`aggregate_id = policy_revision_id`，`aggregate_sequence = revision`。
中继与 S08 §6.4 同一形状：`FOR UPDATE SKIP LOCKED` 批量取，同一 `partition_key` 单线程发布。

### 10.2 事件与一致性

- 发布：`policy.revision.published`（topic `drama.governance-policy.v2`，partition key `tenant_id:aggregate_id`），与 DB 提交同事务写 Outbox。
- 建议补充（当前 catalog 未声明，见 §11）：`policy.decision.recorded`、`policy.safety_tightened`、`policy.gate.state_changed`。
- 消费：`access.grant.changed`（S01，失效授权缓存 + 触发在途复查）、`snapshot.manifest.committed`（S08，重算 gate 绑定与 supersede）、`asset.*.accepted` / 权利吊销、`budget.*`（若订阅）。
- Inbox 按 `consumer + event_id` 去重；聚合按 `sequence`，缺口缓冲 + 回源，不无限等待；终态不倒退。

**紧急收紧（kill switch 类）走旁路：** `safety_tightening` 表在每次决策时**直接读**，不经过 revision 解析。这样即使解析缓存/Pub-Sub 落后，收紧也能立刻生效。只有"放宽"才需要走 revision + checkpoint。

### 10.3 幂等

- 所有写命令：`tenant_id + command_id + request_hash`；同键不同 body → **409**，不是覆盖。
- `PolicyDecision` 的 `evaluate` 也幂等：同 `command_id` 返回**同一个** `decision_id` 与 `expires_at`（不刷新有效期，否则重复点击可以无限续命一次 allow）。已消费 → 409 `DECISION_ALREADY_CONSUMED`。

---

## 11. 契约缺口（报告，不擅自改）

按 AGENTS.md"契约与文档冲突 → 停下，报告冲突，不要自行选边"，下面逐条登记。**本方案在不改 schema 的前提下已给出可实现的替代路径**，但缺口本身需要 catalog/schema 所有者决策。

| # | 缺口 | 影响 | 本方案的临时路径 | 建议 |
|---|---|---|---|---|
| G1 | `overrides[].scope_type` 无 `run`，也无 `tenant`/`enterprise` | docs/06 §2 的"单集/Run"层无法表达 | 用 episode 级覆盖近似 + manifest `policy_ref` pin | 在 v2.1 加 `run`；注意 enum 扩张对严格消费者是破坏性的，建议同时把 `scope_type` 改成开放字符串 + `x-known-values` |
| G2 | 根级无 `reviewer_roles`、`locked`、`allowed_actions`、`expiry`、`quorum` | 企业层无法直接声明这些 | 强制企业层用一个 `scope_type=project` 的"企业模板"文档当基座，或用 overrides 覆盖；`quorum` 无表达 | 加为可选根级字段（additive，符合 docs/04 §4 expand 阶段） |
| G3 | `required_gates` 枚举缺 `quality_exception`、`asset_eligibility`/`voice_eligibility` | docs/06 §3 的 6 类门与 schema 7 项对不上（质量例外、音色/资产资格无处表达） | 用 `locked` + 专用 PolicyException 近似，语义不完整 | 扩枚举；同样建议改为开放 gate id + 注册表 |
| G4 | 无 `approved_evaluators` / `qc_min_coverage` / `repair_limits` 的跨 Run 累计口径 | E25 的"评估器超时/冲突"判定缺少策略参数 | 用常量 + S39 计数 | 新增字段 |
| G5 | `mode` 与两个开关的兼容矩阵未在 schema 表达（只有 unattended→isolate_branch 一条 `allOf`） | 非法组合能通过 schema 校验 | 在应用层实现 §2 矩阵 | 用 `allOf` 补齐或在 v2.1 文档化 |
| G6 | 事件只有 `policy.revision.published` | UI/下游无法感知决策与 gate 状态 | 内部投影 + 轮询 | 补 `policy.decision.recorded` 等 |
| G7 | `hard_rules.budget_scope_id` 无层级语义定义 | 无法机器判定"下属于" | 依赖 S23 的 budget tree 接口 | 在 S23 契约里明确 scope 树查询 |

---

## 12. 测试清单

> 只列 S02 侧可独立验证的断言。跨服务的部分标注依赖的 mock。所有用例都必须在**不接真实模型**的前提下可跑（docs/16 §6）。

### E05 —— 四档 × 项目/集/阶段/节点 + 两个开关

| 用例 | 输入 | 期望 |
|---|---|---|
| E05-01 | 企业 `unattended_batch`，项目想要 `stage_review` | 有效 `mode=stage_review`（收紧成功），`provenance.mode.source=project` |
| E05-02 | 企业 `stage_review`，节点想要 `unattended_batch` | **422 `LOOSENING_NOT_ALLOWED`**，不是静默压回 |
| E05-03 | 企业 `acceptance_mode=manual`，节点想要 `auto_if_qc_pass` | 422；解析（如果绕过发布）仍为 manual |
| E05-04 | 企业 `generation_mode=auto`，节点 `manual` | 有效值 `manual`，`provenance.source=node` |
| E05-05 | 两个开关交叉四组合 × 4 档 = 16 组 | 逐组对照 §2 矩阵；`guided+auto`(gen) 与 `unattended+manual`(acc) 的期望见 §2 |
| E05-06 | `repair_limits` 企业 5 / 项目 3 / 节点 8 | `min=3` |
| E05-07 | `required_gates` 三层各不同 | 并集，且顺序不影响结果（乱序重放 100 次结果一致） |
| E05-08 | `reviewer_roles` 企业 `[d,p,f]`，项目 `[d]` | `[d]`；项目写 `[finance]` → 422 `REVIEWER_ROLE_ESCAPE` |
| E05-09 | `budget_scope_id` 节点指向兄弟项目 scope | 422 `BUDGET_SCOPE_ESCAPE` |
| E05-10 | 四档 × 无任何覆盖 | 等于各档默认值，`provenance` 全部标 `default` |
| E05-11 | 同一 scope 有多份文档（租户文档 overrides + 项目文档） | 结果与文档应用顺序无关 |
| E05-12 | `mode` join 的交换律 | `resolve(a,b)` 与 `resolve(b,a)` 的 `policy_hash` 相同 |

### E07 —— 手工锁定与旧审核晚到

| 用例 | 期望 |
|---|---|
| E07-01 | 节点 locked=true 时 `dispatch_admission` → `deny NODE_LOCKED`，不产生 reservation |
| E07-02 | 锁定后 unlock 产生新 lock_version；绑定旧 lock_version 的 `PolicyDecision` → `deny DECISION_STALE` |
| E07-03 | 为 node_epoch=3 签发的 allow，在 epoch 升到 4 后使用 → **拒绝**（subject 绑定不一致） |
| E07-04 | 过期（`expires_at` 已过）的 allow → `deny DECISION_EXPIRED` |
| E07-05 | 同一 `decision_id` 被消费两次 → 第二次 409 `DECISION_ALREADY_CONSUMED` |
| E07-06 | S08 侧 epoch CAS 失败（mock 返回 conflict）→ S02 gate 保持 `AWAITING_DECISION`，不写 SATISFIED |

### E25 —— 评估器超时与冲突

| 用例 | 期望 |
|---|---|
| E25-01 | 必需报告缺失 → `abstain`，且**不产生 allow** |
| E25-02 | 报告 `decision=abstain` → `require_human` |
| E25-03 | 评估器超时（deadline_hit=true）→ `abstain EVIDENCE_INCOMPLETE` |
| E25-04 | 两份报告对同一 hash 结论相反 → `require_human EVALUATOR_CONFLICT` |
| E25-05 | 报告 hash ≠ subject hash → `abstain HASH_MISMATCH` |
| E25-06 | 评估器版本不在 `approved_evaluators` → `abstain EVALUATOR_NOT_APPROVED` |
| E25-07 | `coverage < qc_min_coverage` → `abstain COVERAGE_BELOW_MIN` |
| E25-08 | **反例**：所有分数都 ≥0.9 但有一份 `abstain` 报告 → 不得 `allow`（防止"评分门"实现） |
| E25-09 | S23 超时 → `deny DEPENDENCY_TIMEOUT`（fail closed），**不得** `allow` |

### E27 —— 企业 SSO 撤权及时生效

| 用例 | 期望 |
|---|---|
| E27-01 | 用户授权在两次 dispatch 之间被撤销；第二次 → `deny AUTH_REVOKED`（不依赖缓存过期） |
| E27-02 | S01 不可达 → `deny`，不返回 `allow`，不返回 `abstain`（必须 fail closed） |
| E27-03 | `POST /policies/{id}/revisions` 由已撤权主体发起 → 401/403，且不产生 revision |
| E27-04 | 撤权后，已签发但未消费的 allow → 消费时重新校验授权（`single_use` 检查包含授权） |

### E12 / 跨租户 / 恢复

| 用例 | 期望 |
|---|---|
| E12-01 | 同 `command_id` 同 body 重投 → 同一 `decision_id`，`expires_at` **不延长** |
| E12-02 | 同 `command_id` 不同 body → 409 |
| E12-03 | 跨租户读取 policy → 404（不泄漏存在性） |
| E12-04 | `expected_revision` 不匹配 → 409 `REVISION_CONFLICT`，无新 revision |
| E12-05 | 重启后：已发布 revision 可读；本地缓存为空时回源成功；回源失败 → 拒绝新增付费动作 |
| E12-06 | `policy.revision.published` 重复投递 / 乱序 → 指针单调（不回退），缓存最终一致 |
| E12-07 | Kafka 重放（side_effects=false）不重复发事件、不重复写 decision |

---

## 13. 未验证与已知风险

- 全部 SLO（吞吐、p95）未实测；`service.yaml` 标 `verified: false`。
- "每次 dispatch 实时调用 S01/S23/S22/S10" 意味着 S02 处在关键路径上。若实测延迟不可接受，**唯一允许的优化是并行化与本地只读副本**，不允许"把复查改成 30 秒一次的后台刷新"——那会让"策略批准 ≠ 放行"失效。若要承担这个成本，应在 docs/03 重新核算容量，而不是弱化语义。
- §2 的兼容矩阵（尤其 `unattended_batch + acceptance_mode=manual` 的处理）需要产品签认；本方案选择"保留收紧方向 + 降级标记"，理由是不许自动放宽。
- §9.2 的 "安全 checkpoint" 定义需要与 S21 工作流实现对齐；本方案给的是必要条件，不是已实现的检查点契约。
- 缺口 G1/G2/G3 若最终按不同方式解决，§4.2 的 Layer 收集逻辑与 §6.1 的 gate 清单需要同步修改。
