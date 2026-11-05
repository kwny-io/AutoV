# 实现方案统一验收 · 2026-10-30

范围：`plans/` 下 **48 份**服务实现方案 —— 48 个服务全部有了实现方案，由 8 个并行会话产出后合并到 `main`。

结论：**48 份里 45 份与契约完全自洽；3 份（S42 / S43 / S44）有一处共同的事件命名冲突待裁决（C9，见 §2.5）。**
此前登记的 C1 冲突已收敛（§2），G1 / G2 缺口已关闭（§3 / §4），C6 / C7 / C8 契约缺口已落（§2.4）。
没有阻断实现的问题 —— C9 只影响订阅侧，不阻塞 S42 / S43 / S44 自身的写入路径。

---

## 1. 自动化检查结果

| 检查 | 方法 | 结果 |
|---|---|---|
| 原始设计包未被改动 | `git log -- docs/design-package` 仅有仓库初始化那一条 | **零改动** |
| 方案未残留进 `services/` | 扫 `services/**` 下 `*-plan.md` / `*方案*.md` | **0 处残留** |
| 文件名 ↔ 服务目录 ↔ service_id | 48 份逐一对齐 `services/*/service.yaml` | **48/48 一致** |
| 内部相对链接 | 跳过代码块后解析 | **0 损坏** |
| 服务 ID 引用有效性 | 全量正则扫描对比 catalog 48 项 | **0 越界引用** |
| 幂等键格式 | `tenant_id + command_id + request_hash` | **47/48 一致**；S27 的幂等入口不同（`dispatch_id` + `stable_client_key` 双唯一约束，见 §6.2），不算偏离 |
| `execution_fence` / `node_epoch` 混用 | 逐文件扫描误用句式 | **0 处混用** |
| defect type 闭集 | 97 个 check 的 `defect_type` 对 14 值闭集 | **97/97 合规** |
| 动作族一致性 | S35 §2.2.1 的 14 行 `chain` + `fallback` 逐格比对 `action-families.v2.json` | **14/14 一致**；`lipsync` 是单动作链（`redo_lipsync`，无箭头），需单独比对 |
| QCReport 规范唯一性 | 检查是否被复述 | **唯一在 S35 §2**，S36/S37/S38 只引用 |
| permit 状态机三方一致 | S22 / S26 / `admission-record.v2.schema.json` | **一致**；S22 的 `EXPIRED` 明确不外露（C22-4 已记录） |
| reservation 状态机 | S23 / 契约枚举 | **一致**；`EXPIRED` 同样不外露（C23-4b 已记录） |
| outbox DDL 完整性 | 44 个内联 `CREATE TABLE ... outbox` 逐个解析列名 | **44/44 六列齐全**（`tenant_id` / `partition_key` / `aggregate_id` / `aggregate_seq` / `headers` / `published_at`） |
| 跨库写入 / 跨库 JOIN | 正负向扫描 | **0 处**（命中 2 处均为"禁止跨库 JOIN"的表述） |
| 发布事件名 ↔ `event-topics.v2.json` | 48 份的"发布"行逐个比对目录 48 个 `event_type` | **45/48 一致**；S42/S43/S44 共 9 个类型未注册，见 §2.5 |
| E01–E40 覆盖 | 全量扫描 | **40/40** 被引用 |

---

## 2. C1（已按方向 1 收敛，2026-11-01）：S39 到底按什么维度做修复路由

### 2.1 冲突原貌

**S35 §2.2 原立场**：S39 按 `type` + `recommended_actions` 决策，**永不按 `check_id`** 决策 —— 这样任一质检服务新增检查项不需要 S39 发版。

**S39 §3.1 原做法**：主映射表第一列就是 check_id 分组（`TECH.CONTAINER.PARSE` / `VIS.IDENTITY.FACE_DRIFT` / `AUD.LIPSYNC.*` …），§3.3 的"不能自动修"清单逐条按 check_id 列举。没有 `type → action` 兜底表，`recommended_actions` 不参与决策。

**为什么两侧都不完整**：

- 照 S39 写 → 新增 check 必须同步发版 S39，"不需要发版"的承诺是假的。
- 照 S35 写 → 14 个 type 表达不出 `keyframe_edit`（仅静态单帧）与 `regenerate_video`（连续）的差别，同一个 `identity` type 在两种窗口下动作不同。

### 2.2 已选方向 1：`type` 定动作族，`check_id` 定窗口与升级

三样东西各管一件事，谁都不越界：

| 层 | 归属 | 管什么 | 变更成本 |
|---|---|---|---|
| `type`（14 值闭集） | `contracts` | 这一族缺陷**可以**用哪几种动作、兜底是什么 | 改闭集 = 全体发版，几乎不做 |
| `check_id` + 登记项 | 各质检服务 | 这一条检查的**最小动作**、**窗口范围**、**何时升级** | 注册即生效，S39 不发版 |
| `recommended_actions` | 质检服务运行时 | 该次检测的**建议**（输入，不是指令） | 无 |

S39 的解析变成三步交集：

```text
candidates = action_family[defect.defect_type]
           ∩ registry[defect.check_id].allowed_actions
           ∩ policy.allowed_actions
起点 = registry.default_action      升级 = registry.escalation_rule
窗口 = registry.window_scope       空集 → human_review, reason=no_capable_route
```

`human_review` 不出现在任何动作族里 —— 它是所有族的公共终点，不是某一族的成员。

**改动落点**：

- [S35 §2.2.1](../../plans/S35-technical-qc.md) 新增 14 行动作族表（含 `keyframe_edit` 仅在 `window_scope ≤ 6 帧` 可用的约束、音频类缺陷不得走 `reingest` 等硬约束）。
- [S35 §2.2.2](../../plans/S35-technical-qc.md) 定义登记项字段与两条硬约束：登记项的动作必须是族的子集；severity 只能往上抬不能往下压。
- [S35 §10.3.1](../../plans/S35-technical-qc.md) 新增 ROUTE-T1..T6 路由不变量，核心是「新 check 不需要 S39 改代码」这条被机械验证。
- [S39 §3.1](../../plans/S39-repair-planning.md) 整张 27 行 check_id 表删除，替换为上述三步交集。
- [S39 §3.3](../../plans/S39-repair-planning.md) "不能自动修"不再逐条列 check_id，改为读登记项 `never_auto_repair` + `no_auto_reason`。
- [S39 §10.1](../../plans/S39-repair-planning.md) 补 E24-R13..R18，其中 R13 直接验证「新增 check 不重建 S39」。

### 2.3 收敛带来的连带改动

`recommended_actions` 枚举从 10 值扩到 12 值，新增 `re_synthesize_speech`（S37 重合成语音）和 `re_render_subtitles`（S38 内部重渲染字幕版式，零生成费用）。原表用自然语言写"重排版/重新 TTS"，那些不是合法枚举值，无法实现。

`realign_subtitles` 只改时码，`re_render_subtitles` 才重建版式，两者不可互相替代；重译**不进**任何动作族 —— 翻译内容变更属于 `change_spec` 范畴。

### 2.4 契约缺口（C6 / C7 / C8 —— 已落）

三条缺口指向同一个东西：`action_family` 原来只是 S35 里的一张文档表格，没有任何机器可读形式，
所以「登记项不得越出动作族」这条约束只能靠人守。

| 编号 | 缺口 | 落点 |
|---|---|---|
| C6 | `action_family` 无机器可读契约，族校验无加载期执行点 | `contracts/quality/action-families.v2.json` + `validate_registry.py` |
| C7 | `recommended_actions` 缺 `re_synthesize_speech` / `re_render_subtitles` | `contracts/quality/qc-report.v2.schema.json`（12 值） |
| C8 | S35 C5 / S39 C5b：动作族与登记项字段都要有 schema | `check-registry.v2.schema.json` + `qc-report.v2.schema.json` |

**落地的四个文件：**

| 文件 | 大小 | 内容 |
|---|---|---|
| `contracts/quality/action-families.v2.json` | 7.6 KB | 14 值闭集 → 动作族；每族 `chain` / `fallback` / `forbidden[]`（含理由）；`keyframe_edit` 的 ≤6 帧前置条件；三步交集公式；5 条不变量 |
| `contracts/quality/qc-report.v2.schema.json` | 24 KB | QCReport v2 完整 payload，**从 `plans/S35` §2.3 程序化提取**而非手抄，避免转录漂移 |
| `contracts/quality/check-registry.v2.schema.json` | 8.3 KB | 登记项形状；`check_id` 命名模式；两条 `allOf` 条件（`never_auto_repair` → 必须有理由；窗口类 → 必须有 `window_max_frames`）；`x-cross-checks` 列出 schema 表达不了的 7 条规则 |
| `contracts/quality/validate_registry.py` | 5.3 KB | 跨文件约束的参考实现。退出码 0=可加载 / 1=拒绝 / 2=环境问题 |

**三份枚举已交叉验证一致**（14 个 `defect_type`、12 个动作），且 `human_review` 不出现在任何
`chain` 里。校验器已用一条违规登记项实测：把 `SUB.TIMING.OVERLAP` 的 `default_action` 写成
`regenerate_video`，被拒并退出 1，两条错误分别指出「起点不在族链内」和「登记项不得拓宽族」。

**仍留一个人工把关点，而且比预想的大。** 原来记的是「`check-registry.v2.json` 数据文件没落」，
像是抄一遍就行。实际清点四份 QC 方案的表之后，**表里根本没有 schema 要求的字段**：

| 缺什么 | 范围 | 后果 |
|---|---|---|
| `window_scope` | 99/99 | 决定 `keyframe_edit` 能否使用，猜错会把长窗口交给逐帧修补 |
| `escalation_rule` | 99/99 | 只有散文，没有可执行的阈值 |
| `default_action` / `allowed_actions` | 76 条（S36/S37/S38） | 这三份表只有 4–5 列，完全没有路由字段 |
| `recheck_scope` | 76 条 | 复检成本无法计算 |
| 枚举外的自由文本 | S35 中 3 处动作、11 处复检范围 | `> 1 帧`、`—`、`remux→transcode`、`全片抽检`、`窗口±0.5s` 等 |

**这不能靠推断补。** 给 76 条 check 凭空填 `window_scope`，是把设计决策伪装成数据提取。
正确做法是四个 QC 服务各自按 S35 §2.2.2 的字段表补登记项，再用 `validate_registry.py` 把关。
在那之前，**族级约束有代码守着，check 级的窗口与起点动作仍靠人工** —— 这句话必须留着，
因为它正是 C1 当初想消灭的那类风险。已记入 [contracts/README](../../contracts/README.md) 缺口第 4 条。

### 2.5 C9（待裁决，2026-11-05）：三份方案的发布事件名未在 `event-topics.v2.json` 注册

48 份方案里有 45 份的"发布"行与目录逐字一致。剩下 3 份一共写了 **9 个目录里不存在的 `event_type`**：

| 服务 | 目录登记 | 方案实际发布 |
|---|---|---|
| S42 | `post.master.rendered` | 同上 + `post.mix.completed`、`post.render.failed` |
| S43 | `delivery.package.ready` | `delivery.package.created`、`.sealed`、`.published`、`delivery.publish.failed` |
| S44 | `evaluation.report.published` | `analytics.metric.snapshot.created`、`analytics.evaluation.completed`、`analytics.cost.projection.updated` |

**这不是笔误，是设计包粒度与实现需要的粒度不一致。** 设计包 `services/S43-delivery.md` 第 15 行原文
只写了一条"发布事件：`delivery.package.ready`"；但 S43 的状态机有 `created → sealed → published`
三态，S44 的 K4（单集交付耗时）要的是 **published 那个时点**，S47 要的是 **sealed 那个时点**。
一条 `ready` 喂不饱两个下游。S42 同理：目录只有"渲染完成"，而 `remix` 的完成与渲染失败是两件事。

**已经在按不存在的类型建订阅的地方**（这才是这条冲突值得单列的原因）：

| 消费方 | 订阅的未注册类型 | 后果 |
|---|---|---|
| [S47](../../plans/S47-realtime-projection.md) §4 | `delivery.package.sealed` | 交付进度投影永远收不到事件 |
| [S44](../../plans/S44-evaluation-analytics.md) K4 | `delivery.package.published` | K4 时长恒为空 |
| [S44](../../plans/S44-evaluation-analytics.md) §6.3 | `billing.cost.recorded`、`generation.attempt.completed`、`quality.qc.completed` | K3/K5 的分母全部落空 |

后三个名字尤其要注意：S23 在目录里只有 `finance.ledger.entry-posted`，S26 只有
`generation.job.state-changed`，四份 QC 服务各只有一条 `completed`/`ready`。
S44 写的这六条消费事件里，**四条在目录里不存在** —— 它的 KPI 口径是照着不存在的流设计的。

**为什么不自行选边**（[AGENTS.md](../../AGENTS.md)「契约与文档冲突 → 停下，报告冲突」）：
补登 9 个 `event_type` 需要连 payload schema 一起补，那是契约变更；退回单条事件则需要
把状态塞进 `data.state`，那是让 S42/S43 的 outbox 与状态机脱节。两条路各有代价，
**且与 C1 同一性质：改的是契约，不该由实现方案单方面决定**。

已按仓库既有格式记入三份方案各自的 `§11 冲突` 节（S42 C8 / S43 C8 / S44 C8，编号互相指向），
待契约维护方裁决。**在裁决前，实现只按方案写入，不按目录订阅 —— 两边一旦不一致，宁可空订阅也不要静默错值。**

---

## 3. G1（已修，2026-11-01）：S25 声明发布事件但方案里没有 outbox

**原问题。** `services/model-routing/service.yaml` 声明：

```yaml
publishes:
  - event_type: model.route.selected
    topic: drama.model-routing.v2
    retention_days: 14
```

但 `plans/S25-model-routing.md` 全文没有 `outbox` 字样，数据模型只有 4 张表（policy_snapshot / route_decision / route_candidate / degradation_option）。其余 16 份方案都有 outbox，所以这不是"本服务不需要事件"的有意选择。

影响：`RouteDecision` 写入与事件发布之间没有事务保证。下游按事件驱动的路径会漏掉路由决策，只能轮询 `GET /route-decisions/{id}` 兜底 —— 这违反 README 第 4 条硬约束。

**修法。** 按原定的补法补齐，并与 S08 §6.4 / S24 §2.7 的既有写法保持一致：

| 补了什么 | 位置 | 内容 |
|---|---|---|
| outbox 表 | §7 数据模型 | `id / aggregate_id / aggregate_seq / event_type / payload / created_at / published_at`，`UNIQUE (aggregate_id, aggregate_seq)` + 部分索引 `WHERE published_at IS NULL` |
| 同事务写入 | §7.1 | 决策头 + 全部候选 + outbox 行三步一个 `COMMIT`；写明为什么必须同事务（两种拆分各留下一种一致性空洞） |
| 事件体 | §7.1 | `data` 只放指针与派单必需字段，**不放 `score_parts`、不放 `rejected[]` 明细**；`feasible=false` 也发事件 |
| relay | §7.2 | `FOR UPDATE SKIP LOCKED` 批量取、按 `tenant_id:aggregate_id` 单线程发布、失败重试、`published_at + 7d` 归档 |
| 幂等语义 | §8 | 幂等回放路径跳过 outbox 插入；至少一次投递，按 `event_id` 去重、按 `(aggregate_id, aggregate_seq)` 判序 |
| 可观测 | §10 | `route_event_outbox_lag_seconds`（> 60s 告警）、`route_event_outbox_depth` |
| 测试 | §11 | T25-24 中继恢复补发、T25-25 重投去重、T25-26 `feasible=false` 仍发事件、T25-27 幂等回放不产生第二条事件 |

**G1 关闭后的 outbox 覆盖：17/17。**

### 3.1 同样的缺口在 18 份新方案里又出现了（已修，2026-11-02）

18 份新方案落盘后重扫同一项，发现 G1 不是孤立事件 —— 它是**同一个模式**重复了七次：

| 方案 | 原问题 | 补法 |
|---|---|---|
| S12 / S13 / S14 | 只写「与 S11 同构」，无 DDL | 内联三张基础设施表 |
| S18 | `outbox_event (...)` 省略号是空的 | 补全 DDL |
| S27 | `connector_observation_outbox` 无 `tenant_id` | 补 `tenant_id` + `partition_key` + 索引 |
| S29 | `outbox` 无 `tenant_id` / `partition_key` | 同上 |
| S30 | 全文只提 outbox，无任何 DDL | 补 `command_receipt` / `outbox` / `inbox` 三张表 |
| S38 | 流程写「落库 + Outbox」，数据模型里没有 | 补三张基础设施表 + 同事务理由 |
| S40 | `outbox(…)` 省略号，且「重试 S08」与「重发事件」未区分 | 补全 DDL + 区分两类写入 |
| S02 | `outbox(…), inbox(…)` 省略号 | 补全 DDL |

**为什么 `tenant_id` 必须有（而不是可省）：** `event-envelope.v2.schema.json` 的 `required`
里有 `tenant_id`。outbox 行没有它，中继就无法填出合法信封 —— 这不是风格问题，是**发不出去**。

**为什么 `partition_key` 必须有：** [S08 §6.4](../../plans/S08-revision-snapshot.md) 的中继按
`partition_key` 保证同一聚合的顺序。缺了它，`aggregate_sequence` 会乱，下游要么缓冲要么丢弃。

**S22 / S23 属于合法引用**：两者明确写「与 S08 完全同构 … 见 S08 §1.6」，而该节确有完整
DDL。这与 S12 的空引用不同 —— 前者能落到确定内容，后者不能。

### 3.2 全量复核结果

| 检查 | 结果 |
|---|---|
| 声明 `publishes` 的方案是否都有 outbox | **33/33**（S02/S22/S23 为合法引用；其余内联） |
| 内联 outbox 是否含 `tenant_id` / `partition_key` / `published_at` | **10/10 三列齐全** |
| 跨库写入 / 跨库 JOIN | **0 处** |
| `docs/design-package/` 改动 | **0** |

一个仍然保留的边界：事件**不是授权书**。S26 收到 `model.route.selected` 后仍必须回查 `route_decision` 校验 `content_hash` / `quote_valid_to` / `rank`（§12）。这与 `docs/04`「事件不能直接推动 accepted pointer」是同一条纪律。

---

## 4. G2（已关闭，2026-11-01）：S38 声明依赖 S28 但方案未提

**原问题。** `services/subtitles/service.yaml` 的 `depends_on` 含 `S28`（媒体字节权威），但 `plans/S38-subtitles.md` 全文没有出现 `S28`。当时的假设是两条之一：catalog 声明过宽（应改 catalog），或 S38 缺了一环（应补方案）。

**确认结果：是后者，catalog 不改。**

S38 的输入链确实不需要媒体字节（`S37 时间码 + S06 对白 + S15 词典 + S41 EDL` 足够）。漏掉的是**输出侧**：

`docs/01` §唯一权威表写死「制品bytes、hash、存储位置 → S28；业务域只保存 artifact_id 和内容 hash」。S38 渲染出的 `.srt` / `.vtt` / `.ass` 就是制品字节，与视频帧、音轨、探针截图同类。S38 自己存这些文件就是第二个字节权威，触犯 `AGENTS.md` 第一条。

**补法。** 方案侧补五处，catalog 与 service.yaml 一字未改：

| 补了什么 | 位置 |
|---|---|
| 说明 S28 是输出侧依赖，不是输入依赖 | S38 §3.0 |
| `subtitles.rendition` 表：只有 `artifact_id` + `content_hash` + `byte_size`，无正文字节 | S38 §2.2 |
| `post.subtitle.ready` 加 `artifacts[]`（每格式一个 S28 指针） | S38 §7 |
| 状态机加 `rendered` / `rendered_pending_archive` / `archived` | S38 §9 |
| E25-U5（S28 不可达 → 停在待归档、不发事件、不重渲染）、V6（扫库证明无字幕正文字节） | S38 §10 |

**G2 关闭。** 一个附带的契约缺口记入 S38 §11 C4：`post.subtitle.ready` 没有 payload schema，`artifacts[].artifact_id` 必须明确属 S28 的 ID 空间。

---

## 5. 已确认无问题的三处

- **`sha256:` 前缀**：S02/S40 的示例里 `content_hash` 写成 `"sha256:..."`，契约 schema 是 `^[0-9a-f]{64}$`（裸 hex）。这是**示例占位符写法**，不是字段定义冲突 —— 所有方案在字段定义处都按裸 hex 写。实现时以 schema 为准，示例按 schema 填充。不构成待改项，但实现时注意别把示例照抄进代码。
- **`grant_state` 字段位置**：S26 说 `admission-record` 里有 `grant_state`，实际它在 `dispatch` 子对象内。S26 的表述正确，是我初查时误判。
- **S27 的幂等键不是 `command_id`**：全仓统一格式是 `tenant_id + command_id + request_hash`，S27 却用 `dispatch_id` 与 `stable_client_key` 两个唯一约束。**这不算偏离** —— S27 没有 `POST /xxx` 形态的业务命令，它的写入口是消费 S26 事件后的出站请求，重放是"同一个 dispatch 被投递两次"而不是"同一个 command 被提交两次"。`dispatch_id` 唯一挡住重放，`(tenant_id, stable_client_key)` 唯一挡住"错误地生成新 dispatch_id 再发一次"，后者正是重复付费的实际路径（`unsupported` 渠道上新 key = 新任务）。两重都在 [S27 §5.2](../../plans/S27-provider-connectors.md) 的 DDL 里，且 T27-02/T27-03 直接测这两条。**但 S27 的 `request_hash` 目前只落库不参与判定** —— 若将来要挡"同 dispatch 不同 body"，需要补一条显式比较，现在没有。

---

## 6. 合并状态

| 分支 | 提交 | 方案 | 状态 |
|---|---|---|---|
| `claude/keen-brahmagupta-8e2308` | `d01de44` | S02, S40 | 此前已入 main |
| `claude/suspicious-bhabha-944438` | `72310ed` | S16, S17, S18 | 此前已入 main |
| `claude/amazing-bhabha-570e99` | `9d09eed` | S08, S22, S23 | 本次提取入 main |
| `claude/strange-murdock-f3e52f` | `d8f6055` | S24, S25, S26, S27 | 本次提取入 main |
| `claude/magical-mendeleev-14bc20` | `81eaac1` | S35–S39 | 本次提取入 main |

5 个会话分支本身**未推送**，方案内容已全部收进 `main:plans/`。

### 6.1 至此累计（2026-11-05）

| 项 | 值 |
|---|---|
| 方案总数 | **48 / 48** |
| 会话分支 | 8 个。5 个带 worktree（S08/S22/S23、S24–S27、S02/S40、S16–S18、S35–S39），3 个直接写在 main（S01/S05/S06/S19/S20、S29–S34、S45–S48） |
| 文档自洽性检查 | 见 §1，除 C9 外全部通过 |
| 全仓方案内部链接 | **0 损坏** |
| 与设计包的偏差 | 除 C9 的 9 个事件名外，无 |

### 6.2 本轮修过但没单列的

- **42 个 outbox 补齐为可发信**（`0afca3e`，23 份方案）：`tenant_id` / `partition_key` / `headers` 三列此前缺失。缺 `tenant_id` 是信封 `required` 挡住的，缺 `partition_key` 是 S08 §6.4 的中继排序挡住的 —— 都不是风格问题。
- **42 个 outbox 的 `aggregate_id` 加了语义标注**（`1e59f53`）：只写 `aggregate_id text` 而不说是哪个实体，实现者挑错一个字段，`aggregate_sequence` 就变成"看起来有序其实是错的"。S29–S34、S42、S44 的聚合键**逐事件不同**，那些地方没有写死一个值，而是指向各自的事件表。
- **S38 §11 的契约缺口表被一条 blockquote 劈成两半**：`> **G2 已关闭（2026-11-01）**…` 夹在 C4 与 C5 之间，导致 C4 出现两次、C5 脱表。已把 G2 说明移到表后，C4 两条合并为一条。这类缺陷不会出现在链接检查里 —— 表格仍然"像是完整的"，只是少渲染两行。
- **S35 §12 / S39 §11 的 C5–C8 仍标为未落**：`991ccee` 已落 `action-families.v2.json` / `qc-report.v2.schema.json` / `check-registry.v2.schema.json` / `validate_registry.py`，但两份方案的缺口表还写着"没有"。已补「已落项」小节并保留原缺口描述 —— **C5 只落了一半**（`check-registry.v2.json` 数据文件仍未落），不能整条划掉。

---

## 7. 下一步

1. ~~补 S25 的 outbox。~~ 已于 2026-11-01 完成，见 §3。
2. ~~确认 G2（S38 是否真依赖 S28）。~~ 已于 2026-11-01 关闭：catalog 正确，方案补了输出侧，见 §4。
3. ~~落 `action_family` 与登记项的机器可读契约（C6/C7/C8），族校验进加载期。~~
   已于 2026-11-02 完成，见 §2.4。族校验的**参考实现**在 `contracts/quality/validate_registry.py`；
   实现期需要把它移植进 `platform/qc-sdk`，并补 `check-registry.v2.json` 数据文件。
4. ~~剩余 31 个服务尚无方案 —— 正在并行编写。~~ 已于 2026-11-05 达到 **48/48**。
   S19 / S20 落盘时已对齐本轮统一过的 outbox 形状；S20 里两处指向不存在文件名的链接
   （`S08-proposal-queue.md`、`S18-workflow-orchestrator.md`）已改为实际文件名，
   全仓方案内部链接现为 **0 损坏**。
5. **C9 待裁决**（§2.5）：3 份方案共 9 个发布事件类型未在 `event-topics.v2.json` 注册，
   已有两个下游按不存在的类型建了订阅。这是契约问题，不归实现方案单方面决定。
6. **74 条 check 的登记项字段仍缺**（§2.4 末尾）：`window_scope` / `escalation_rule` /
   `default_action` / `allowed_actions` / `recheck_scope` 要由四个 QC 服务各自补，
   不能由别处推断。

方案是设计稿。以上检查证明的是**文档之间自洽**，不是**设计正确**。E01–E40 里真正要证明的东西（跨租户拒绝、重复投递幂等、Saga 补偿、fencing、重启恢复）只能靠实现加测试证明 —— `docs/16` 的原话是「schema 校验通过 ≠ 分布式正确性」。
