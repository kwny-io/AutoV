# 实现方案统一验收 · 2026-10-30

范围：`plans/` 下 **48 份**服务实现方案 —— 48 个服务全部有了实现方案，由 8 个并行会话产出后合并到 `main`。

结论：**48 份全部与契约自洽。** 最后一处冲突 C9（事件命名，S42 / S43 / S44）已于 2026-11-05 裁决并落地
（§2.5，commit `4f1bdcb`）：发布侧补充登记 10 个 `event_type`、删除 2 个零消费方的旧名，
消费侧改正 6 处指向错误产出方的事件名。事件目录由 48 条增至 55 条。
C1 冲突已收敛（§2），G1 / G2 缺口已关闭（§3 / §4），C6 / C7 / C8 契约缺口已落（§2.4），
C45-7 的脱敏规则表也已落（§2.6，`status: proposal`）。
**payload schema 已全部落地（2026-11-08 复核）** —— 55 个 `event_type` 全部有独立的 `data` schema，
55 份互不相同、零空壳，跨服务的自动校验因此不再缺失。本报告 11-05 曾写「没有一条有」，
那是当时的实况，之后 `785b180`…`a172d89` 补完，见 §7 第 5 条与 §6.2 更正表第 3 行。
另有两处**新查实的文档缺陷**（不影响 C9 结论）：本报告此前写的「74 条 check」应为 99，
以及 3 条 `check_id` 违反 S35 自己的三段式命名规约 —— 见 §7 第 6、7 条。

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
| defect type 闭集 | 99 个 check 的 `defect_type` 对 `check-registry.v2.schema.json` 的 14 值闭集 | **97/99 合规**；S36:`VIS.KNOWLEDGE.SCOPE_RENDER` 与 S37:`AUD.TEXT.SCRIPT_CONFLICT` 的 `type` 是 `—`（两行都自述"不存在"/"不产生 defect"，**不登记为 check**，见 `check-registry-missing-fields-2026-11-05.md` 的 §11.5）。闭集里的 `audio_mix` 一次未现，13 个非空 type 覆盖 97 条。原写 99/99，见 §6.2 |
| `check_id` 命名规约 | 99 条首格对 `S35:157` 的 `<LAYER>.<GROUP>.<NAME>` | **96/99 合规**；S35 的 `TECH.BLACK_FRAME` / `TECH.FREEZE_FRAME` / `TECH.FLASH_FRAME` 只有两段，见 §7 第 7 条 |
| 动作族一致性 | S35 §2.2.1 的 14 行 `chain` + `fallback` 逐格比对 `action-families.v2.json` | **14/14 一致**；`lipsync` 是单动作链（`redo_lipsync`，无箭头），需单独比对 |
| QCReport 规范唯一性 | 检查是否被复述 | **唯一在 S35 §2**，S36/S37/S38 只引用 |
| permit 状态机三方一致 | S22 / S26 / `admission-record.v2.schema.json` | **一致**；S22 的 `EXPIRED` 明确不外露（C22-4 已记录） |
| reservation 状态机 | S23 / 契约枚举 | **一致**；`EXPIRED` 同样不外露（C23-4b 已记录） |
| outbox DDL 完整性 | 46 个内联 `CREATE TABLE ... outbox` 逐个解析列名 | **46/46 六列齐全**（`tenant_id` / `partition_key` / `aggregate_id` / `aggregate_seq` / `headers` / `published_at`） |
| 跨库写入 / 跨库 JOIN | 正负向扫描 | **0 处**（`JOIN` 共 6 个命中：4 处是"没有跨库 JOIN"的表述、1 处是 S22 同 schema 自连接、1 处是 `VIS.ANATOMY.JOINT_ARTIFACT` 的分词误命中） |
| 发布事件名 ↔ `event-topics.v2.json` | 48 份的"发布"行逐个比对目录的 55 个 `event_type` | **48/48 一致**（2026-11-05 补登后重跑）；反向检查也通过：catalog 声明的 55 条与目录 55 条双向无差 |
| 脱敏契约 ↔ 消费点 | 三份方案引用的 `redaction-policy.v2` 字段路径逐个解析 | **全部存在**；`applies_to[].rule_ref` 引的 **13 个章节号（S45 ×5 / S46 ×5 / S47 ×3）逐条对到原文**，外加契约里指向 `docs/04` 与 `docs/14` 的两处也都存在 |
| E01–E40 覆盖 | 全量扫描 | **40/40** 被引用 |
| 方案内细分编号唯一性 | 按「每篇方案的字母」逐条比对 | **不通过**：42 个编号在 2 篇以上方案里重复定义，且**行文全不相同**（同名不同事）。两套命名空间并存：16 篇用方案内局部编号（`E05-01`，306 个），S41–S44 用全局编号（`E05-S41-01`，114 个，0 冲突）。**S15 是唯一字母混用的方案**——自己的 22 条用 `F`，另 10 条 `E30-C1..C10` 用了 S12 的 `C`；其中 `E30-C1`/`E30-C2` 与 S12 的 `plans/S12-costume-assets.md:634-635` 真正撞号。见 §6.2 末条与 §7 第 9 条 |

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

同一轮还落了 C45-7（脱敏规则表），但不属于质量域，单列在 §2.6。

**落地的四个文件：**

| 文件 | 大小 | 内容 |
|---|---|---|
| `contracts/quality/action-families.v2.json` | 7.6 KB | 14 值闭集 → 动作族；每族 `chain` / `fallback` / `forbidden[]`（含理由）；`keyframe_edit` 的 ≤6 帧前置条件；三步交集公式；5 条不变量 |
| `contracts/quality/qc-report.v2.schema.json` | 24 KB | QCReport v2 完整 payload，**从 `plans/S35` §2.3 程序化提取**而非手抄，避免转录漂移 |
| `contracts/quality/check-registry.v2.schema.json` | 8.3 KB | 登记项形状；`check_id` 命名模式；两条 `allOf` 条件（`never_auto_repair` → 必须有理由；窗口类 → 必须有 `window_max_frames`）；`x-cross-checks` 列出 schema 表达不了的 7 条规则 |
| `contracts/quality/validate_registry.py` | 7.0 KB | 跨文件约束的参考实现。退出码 0=可加载 / 1=拒绝 / 2=环境问题 |

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
| 枚举外 / 不可直接提取的取值 | S35 中 1 处动作（`TECH.AUDIO.OFFSET` 的 `remux→transcode`）、10 处复检范围；四表合计 8 处带条件严重度（S35 4 / S36 1 / S37 1 / S38 2），另 1 处二值（S37 `AUD.LIPSYNC.OFFSET`）、3 处为空（S36 / S37×2） | `remux→transcode`、`全片抽检`、`连续窗口 ±0.5s`、`major（master 时 blocker）` 等 |

**这不能靠推断补。** 给 76 条 check 凭空填 `window_scope`，是把设计决策伪装成数据提取。
正确做法是四个 QC 服务各自按 S35 §2.2.2 的字段表补登记项，再用 `validate_registry.py` 把关。
在那之前，**族级约束有代码守着，check 级的窗口与起点动作仍靠人工** —— 这句话必须留着，
因为它正是 C1 当初想消灭的那类风险。已记入 [contracts/README](../../contracts/README.md) 缺口第 4 条。

### 2.5 C9（已裁决，2026-11-05，commit `4f1bdcb`）：三份方案的发布事件名未在 `event-topics.v2.json` 注册

**原问题。** 48 份方案里有 45 份的"发布"行与目录逐字一致。剩下 3 份一共写了 **9 个目录里不存在的 `event_type`**：

| 服务 | 目录原登记 | 方案实际发布 |
|---|---|---|
| S42 | `post.master.rendered` | 同上 + `post.mix.completed`、`post.render.failed` |
| S43 | `delivery.package.ready` | `delivery.package.created`、`.sealed`、`.published`、`delivery.publish.failed` |
| S44 | `evaluation.report.published` | `analytics.metric.snapshot.created`、`analytics.evaluation.completed`、`analytics.cost.projection.updated` |

**这不是笔误，是设计包粒度与实现需要的粒度不一致。** 设计包 `services/S43-delivery.md` 第 15 行原文
只写了一条"发布事件：`delivery.package.ready`"；但 S43 的状态机有 `created → sealed → published`
三态，S44 的 K4（单集交付耗时）要的是 **published 那个时点**，S47 要的是 **sealed 那个时点**。
一条 `ready` 喂不饱两个下游。S42 同理：目录只有"渲染完成"，而 `remix` 的完成与渲染失败是两件事。

#### 裁决：发布侧补登，消费侧改名

关键区分是**这条错名是谁的错**。方案要发的状态，目录里没有 → 补登；方案把别人发的事件名写错了
→ 改方案，不能补登。补登一个没有生产者的名字，只会造出新的空流。

**发布侧（补登 10 条，删 2 条）。** `contracts/event-topics.v2.json` 由 48 条增至 55 条，
`contracts/service-catalog.v2.json` 的三个 `published_events` 同步，`service.yaml` 已重新生成：

| 服务 | 新增登记 | topic |
|---|---|---|
| S42 | `post.mix.completed`、`post.render.failed` | `drama.composition.v2` |
| S43 | `delivery.package.created`、`.sealed`、`.published`、`delivery.publish.failed` | `drama.delivery.v2` |
| S44 | `analytics.metric.snapshot.created`、`analytics.evaluation.completed`、`analytics.cost.projection.updated` | `drama.evaluation-analytics.v2` |

删除 `delivery.package.ready` 与 `evaluation.report.published` —— 全仓（`plans/` `contracts/` `docs/`
除设计包）**零消费方**，是设计包的粗粒度残名，留着只会诱使后来者订阅一个永远空的流。

**消费侧（改名 6 处，不补登）。** [S44](../../plans/S44-evaluation-analytics.md) §6.3 有 4 个、
[S43](../../plans/S43-delivery.md) §6.3 有 2 个名字，指向的产出方从来不发那个类型：

| 方案写的 | 实际产出方只发 | 处理 |
|---|---|---|
| `billing.cost.recorded`（S23） | `finance.ledger.entry-posted` | 改名，取 `data.entry_type` / `data.attribution.*` |
| `generation.attempt.completed`（S26） | `generation.job.state-changed` | 改名，按 `data.attempt_id` 聚合、`data.state` 判终态 |
| `quality.qc.completed`（S35–S38） | 四条各一：`quality.technical.completed`、`quality.semantic.completed`、`quality.alignment.completed`、`post.subtitle.ready` | 拆四条；K5 按 `defect_type` 闭集归并，不按服务归并 |
| `delivery.package.published`（S43） | 同左 —— 已随本轮补登 | 无需改 |
| `governance.asset.rights.changed`（S15） | `asset.voice.authorization.changed` | 改名；撤销语义在 `data.to_state` + `data.reason_code` |
| `production.subtitle.revision.created`（S38） | `post.subtitle.ready` | 改名；G6 用它的 `timeline_revision` |

**设计包不动。** `docs/design-package/` 只读，`services/S43-delivery.md:15` 与
`S44-evaluation-analytics.md:15` 的旧名保持原样。差异由本节和三份方案的 §11 C8 记录 ——
那是原始输入，改了就没法对照。

**仍缺的是 payload schema。** 登记只给了 `event_type` + topic + 保留策略，`data` 的结构仍只写在
各方案的事件定义里（[S42](../../plans/S42-composition.md) §6.3、[S43](../../plans/S43-delivery.md) §6.3、
[S44](../../plans/S44-evaluation-analytics.md) §6.3）。这三份方案的 §11 C8 都留了这句话，
不要因为"已登记"就当 payload 也齐了。

### 2.6 C45-7（已落，2026-11-05）：密钥脱敏此前只有散文

S45 §11 记的 C45-7 是「无载密钥扫描的共享规则」。它此前散在 S45 §10.6、S46 §8.3、S47 §5.2
三句话里，而且**三处口径不同**：S45 说"拒绝"，S46 说"负责脱敏"，S47 的 DDL 注释直接写
"已脱敏，可直接下发"。三句话各自都像对的，合起来没有一处能回答"什么算密钥"。

落成 **`contracts/redaction-policy.v2.json`**（手写，`status: "proposal"`，`generated_from: null`）：
6 类 secret 闭集 + `not_secrets` 白名单、两种替换形态、一个兜底。四个执行点
（S45 `PAYLOAD_SECRET_SCAN` / S46 `INGRESS_SECRET_SCAN` / S47 `PROJECTION_SECRET_SCAN` /
任意生产方 `OUTBOUND_SECRET_SCAN`）读同一份，不各自定义。

**三处口径统一到一个答案（`redact_and_store`），因为另外两条候选都错：**

| 候选口径 | 为什么不行 |
|---|---|
| 命中就拒绝（S45 原写法） | 拒收 = 让任何生产方用一串密钥形状的字符串把自己的审计行抹掉。E39 从"记录事实"变成"谁能控制自己被记录" |
| 命中就放行（原状，靠上游自觉） | S47 的 DDL 注释写"已脱敏，可直接下发"——那是**结论**不是**机制**，上游没脱敏时它没有任何执行点 |
| 整条记录作废、只留"我被脱敏了" | 通不过 E39。所以只换命中的叶子，`action`/`actor`/`targets`/`outcome` /`occurred_at` 一律不动 |

**顺序是硬约束，不是实现建议：** 脱敏在哈希之前。S45 若先算 `record_hash` 再替换，链复算不出来，
该 stream 之后每条都校验失败；S46 若先算 `params_sha256` 再替换，合法重试变成
`REQUEST_HASH_MISMATCH`。契约的写法是 `scan_execution.timing = "synchronous, in the write path,
before hashing or persisting"`，理由写在 `invariants[2]`。

**未落的是四问，不是文件。** `open_questions` 的 R-1…R-4（KMS 密钥归属、S46 是否该把
`provider_credential` 当错误而非替换、标记里要不要带检测器版本、侧车 vs 每服务自实现）
在方案层给不出答案。其中 **R-2 是真实张力**：S46 §5.6 说供应商凭据出现在 S46 表里是设计错误，
本契约说不拒收只替换 —— 两句都成立，因为一个是"数据不该存在"、一个是"客户端塞进来的怎么处理"。
按 AGENTS.md 记为冲突待裁决，没有自行选边。

**子会话交回来时带了范围外的东西。** 派的是"三份方案补脱敏"，回来的 S45 diff 里却夹着
§3.3 状态表、§3.5 完整性校验的代码块、§3.6 导出的状态图三处**与脱敏无关的散文压缩**。
已整份 `git checkout --` 回滚，再只补脱敏那六处。这类"顺带优化"是最难发现的一种：
diff 总量看起来合理，被删掉的结构却分散在几个不相邻的章节里，逐行读才发现。
**所以子会话的"完成了"只能当线索，不能当结论。**

---

## 3. G1（已修，2026-11-01）：S25 声明发布事件但方案里没有 outbox

**原问题。** `services/model-routing/service.yaml` 声明：

```yaml
publishes:
  - event_type: model.route.selected
    topic: drama.model-routing.v2
    retention_days: 14
```

但 `plans/S25-model-routing.md` 全文没有 `outbox` 字样，数据模型只有 4 张表（policy_snapshot / route_decision / route_candidate / degradation_option）。其余 16 份方案都提到 outbox（`01d23e5` 逐篇 `re.search("outbox", t, re.I)` 得 16/17），所以这不是"本服务不需要事件"的有意选择 —— 注意"提到"不等于"有可用 DDL"，当时 17 份里六列齐全的只有 S08 一份，见下面那段。

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

**G1 关闭后的 outbox 覆盖：当时那 17 份里只有 11 份写了 outbox（9 份内联 DDL + S22 / S23 两份落到 S08 那节的引用）—— 缺 S02 / S16 / S17 / S18 / S38 / S40；**
`0e79249` 给其中 4 份（S02 / S18 / S38 / S40）补了 outbox，剩下的 **S16 与 S17** 拖到 2026-11-08 才补齐（见 §7 更正表第 9 行；全库现为 48/48，见 §3.2）。
（「有 outbox」与「列齐全」是两个量：那 11 份里按六列严格判**只有 1 份**（S08 自己）—— S24 / S25 / S26 / S35 / S36 / S37 / S39 由 `0afca3e`「Make every outbox emit-able and every idempotency table complete」补齐列，S27 到 `0e79249` 才齐。）

### 3.1 同样的缺口在同一批新落地的 21 份方案里又出现了（已修，2026-11-02）

`0e79249` 把 `plans/` 从 17 份推到 38 份（该提交新增 21 份、改动 5 份，`git diff --name-status 0e79249^ 0e79249 -- plans/` 可数）。重扫同一项发现 G1 不是孤立事件 —— 新增的 21 份里 **9 份落地即六列齐全，12 份仍列不全**；同提交补齐的旧方案是 S02 / S18 / S27 / S38 / S40 这 5 份。同一个模式：

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
| S16 | `outbox_event` 是裸列名表，缺 `partition_key` / `headers` | 补全 DDL（2026-11-08 补做，见 §7 更正表第 9 行） |
| S17 | 三张基础设施表全是 `(…)` 伪 DDL，`outbox_event` 无 `event_type` / `partition_key` / `headers` / `published_at` | 补全 DDL（同上） |

（表里前八行是这一批落地的方案，S16 / S17 两行不是 —— 它们属于 §3 那份 17 份的旧集合，补做时间也晚得多。放同一张表只是因为这十处是同一个模式的十次复发。另：「原问题」一列记的是并行会话交出来的稿子，那个稿子没有进 git，所以只能由落地后的文件反推；`0e79249` 落地时 S29 仍缺 `headers`，到 `0afca3e` 才齐。）

**为什么 `tenant_id` 必须有（而不是可省）：** `event-envelope.v2.schema.json` 的 `required`
里有 `tenant_id`。outbox 行没有它，中继就无法填出合法信封 —— 这不是风格问题，是**发不出去**。

**为什么 `partition_key` 必须有：** [S08 §6.4](../../plans/S08-revision-snapshot.md) 的中继按
`partition_key` 保证同一聚合的顺序。缺了它，`aggregate_sequence` 会乱，下游要么缓冲要么丢弃。

**S22 / S23 属于合法引用**：两者明确写「同 S08 §1.6」（S22 另有「完全同构」四字），而该节确有完整
DDL。这与 §3.1 表里那些只写「与 S11 同构」却不落 DDL 的写法不同 —— 前者能落到确定内容，后者不能。

（更正：S23 原文只写了「同 S08 …」再加本服务自己的 Outbox 约束，没有「完全同构」那半句 ——
它只存在于 S22。见 §7 更正表第 8 行。）

### 3.2 全量复核结果

| 检查 | 结果 |
|---|---|
| 声明 `publishes` 的方案是否都有 outbox | **48/48**（S22/S23 为合法引用；其余 46 份内联） |
| 内联 outbox 是否含 `tenant_id` / `partition_key` / `published_at` | **46/46 三列齐全**（六列亦齐全） |
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
| 文档自洽性检查 | 见 §1，全部通过（C9 已裁决，2026-11-05） |
| 全仓方案内部链接 | **0 损坏** |
| 事件目录 | 48 → **55** 条 `event_type`，48 个 topic 不变 |
| 密钥脱敏契约 | 已落 `contracts/redaction-policy.v2.json`（`status: proposal`），四个执行点读同一份；四个 `open_questions` 待安全侧裁决（§2.6） |
| 与设计包的**有意**偏差 | 事件粒度更细（设计包每服务一条，方案按状态机状态发）。设计包文件本身**零改动** —— 差异记在本报告与三份方案的 §11 C8 |

### 6.2 本轮修过但没单列的

- **42 个 outbox 补齐为可发信**（`0afca3e`，23 份方案）：`tenant_id` / `partition_key` / `headers` 三列此前缺失。缺 `tenant_id` 是信封 `required` 挡住的，缺 `partition_key` 是 S08 §6.4 的中继排序挡住的 —— 都不是风格问题。
- **42 个 outbox 的 `aggregate_id` 加了语义标注**（`1e59f53`）：只写 `aggregate_id text` 而不说是哪个实体，实现者挑错一个字段，`aggregate_sequence` 就变成"看起来有序其实是错的"。S29–S34、S42、S44 的聚合键**逐事件不同**，那些地方没有写死一个值，而是指向各自的事件表。
- **S38 §11 的契约缺口表被一条 blockquote 劈成两半**：`> **G2 已关闭（2026-11-01）**…` 夹在 C4 与 C5 之间，导致 C4 出现两次、C5 脱表。已把 G2 说明移到表后，C4 两条合并为一条。这类缺陷不会出现在链接检查里 —— 表格仍然"像是完整的"，只是少渲染两行。
- **S35 §12 / S39 §11 的 C5–C8 仍标为未落**：`991ccee` 已落 `action-families.v2.json` / `qc-report.v2.schema.json` / `check-registry.v2.schema.json` / `validate_registry.py`，但两份方案的缺口表还写着"没有"。已补「已落项」小节并保留原缺口描述 —— **C5 只落了一半**（`check-registry.v2.json` 数据文件仍未落），不能整条划掉。
- **本报告「74 条 check」是错的**（`4f1bdcb` 之后重数）：实际 **99 条**，`contracts/README.md:73` 一直是对的。74 漏算了一整份 QC 方案的表。已改 §7 第 6 条并附分层计数。
- **本报告「3 处动作在枚举外、11 处复检范围」也是错的**（2026-11-05 复核）：实际是 **1 处**（`TECH.AUDIO.OFFSET` 的 `remux→transcode`）和 **10 处**。原因是 `plans/S35:765` 的判定列写的是 `|actual − ShotSpec.duration| > 1 帧` —— 绝对值符号是**裸管道符**，朴素 `split('|')` 把这一行切成 9 列，把动作列和复检列整体右移一格，于是相邻行被误判成"枚举外"。必须用 `(?<!\\|)\\|` 切分（[缺字段清单](check-registry-missing-fields-2026-11-05.md) §8 记了这条）。**这个错和上面那条 74 是同一类**：不是没数，是数错了没复核，而两次都是子会话的独立复算把它抓出来的。
- **3 条 `check_id` 违反自己文件里定的命名规约**：`plans/S35-technical-qc.md:773-775` 的 `TECH.BLACK_FRAME` / `TECH.FREEZE_FRAME` / `TECH.FLASH_FRAME` 只有两段，而同文件 157 行的规约是 `<LAYER>.<GROUP>.<NAME>`。这类缺陷不会出现在链接检查里，也不会出现在"check 总数"里 —— **它恰恰是让计数变成 20 而不是 23 的原因**，因为按三段式正则数就漏掉这 3 行。已记入 §7 第 7 条，待 owner 确认改名。
- **方案内细分编号有 42 处同名不同事（2026-11-08 复核，未改任何编号）。** 全库 20 篇方案定义了细分编号，两套命名空间并存：S02 / S10–S18 / S35–S40 这 16 篇用方案内局部编号（`E05-01`，306 个），S41–S44 用带服务号的全局编号（`E05-S41-01`，114 个）。全局那套 0 冲突；局部那套有 42 个编号被 2 篇以上方案各自定义，**逐条比对行文，42 条全部内容不同**——`E05-01` 在 S02 是「企业 `unattended_batch` 收紧为 `stage_review`」，在 S40 是「同一 artifact 在 4 档下的 `reason.code`」。按行内编号看它们不是同一个用例，只是撞了名字。**只有 S15 一篇是字母混用**：S10–S15 的字母按服务号顺序排 A/B/C/D/E/F，S15 自己的 22 条用 `F`，却有 10 条 `E30-C1..C10` 用了 S12 的字母 `C`。其中 `E30-C1`/`E30-C2` 与 S12 的 `plans/S12-costume-assets.md:634-635` 是同一对编号、两套断言，而 `contracts/asset.costume-revision-accepted.v2.schema.json` 与 `contracts/asset.voice-authorization-changed.v2.schema.json` 各自引了同名的 `E30-C1` 却不带服务限定——**任何按编号建索引的脚本都会把它们合成一条**。改名方向有依据（S15 该用 `F`），但会牵动 **23 处**引用——S15 表内 10 条定义、`asset.voice-authorization-changed` schema 10 处、`contracts/README.md` 3 处（该文件当前有另一会话的未提交改动），故**只记录不施改**，见 §7 第 9 条。
- **派活时我自己差点犯 AGENTS.md 禁的那类错**：第一版任务单让子会话「没有出处就按语义给保守值」补 `window_scope`，直接撞上 `contracts/README.md:84` 的「不从散文推断这些值」。已撤回并改成产出「缺字段清单 + `NEEDS_OWNER` 标注」。**这条值得记下来**：`check-registry.v2.json` 的诱惑就是"看起来很接近完成"，补上就绿色了。但 `window_scope` 猜错会让 S39 把 20 帧的连续窗口交给逐帧修补 —— 一个绿色比一片诚实更贵。

- **§1 表里两个数已就地改正（2026-11-08 复核）。** 记录格式同缺字段清单的 §7：原写 → 正确 → 错因。

  | # | 位置 | 原写 | 正确 | 错因 |
  |---|---|---|---|---|
  | 1 | §1「defect type 闭集」 | **99/99 合规**（98 行处又附「重数 TECH 23 / VIS 32 / AUD 20 / SUB 24」） | **97/99 合规**；2 条 `type` 是 `—` | 分层重数（23/32/20/24）是**行数**，与「闭集合规数」是两个量，我把前者的"全数都对"读成了后者。`set()` 出 14 个取值就当成"14 个合法取值"——第 14 个正是 `—`，它恰好占掉闭集里 `audio_mix` 的那个位置，于是"14 对 14"看起来严丝合缝。**同一目录的缺字段清单 §7 一直写的是 97/99，两份报告矛盾了 7 天没人发现**，因为它不在任何 check 的管辖里 |
  | 2 | §1「脱敏契约 ↔ 消费点」 | 引的 **9 个**章节号（S45 ×4 / S46 ×5 / S47 ×2） | **13 个**：S45 ×5 / S46 ×5 / S47 ×3 | 原括号里的 4+5+2 就只有 **11**，和它自己写的 9 都没对上；实际逐条数 `§` 是 5/5/3=13。`applies_to` 共 4 条 `rule_ref`，其中 3 条是 `plans/Sxx §a, §b, §c…` 的**逗号列表**——我数的是列表项而不是章节号。**"几个 rule_ref"与"几个章节号"中间差一个列表展开因子**，和 §6.2 结尾那条"claim 条数 vs 失败行数"是同一族错 |
  | 3 | §0 摘要 / §7 第 5 条残留项 | 「55 个 `event_type` 的 `data` payload schema **仍未落**，结构只在各方案正文里」 | **已全部落地**：55/55 有 `data_schema_ref`，55 个文件互不相同、零空壳 | 这条是 11-05 写的**当时为真**，之后 `785b180`…`a172d89` 陆续补完，我没回表复查就沿用了旧结论。**同一事实在 §0 与 §7 各写一遍，第一版更正只改了 §7**，摘要里那份留到 11-08 第二轮才抓到 —— 和第 6 节记的「同一事实写两遍、改动只落一遍」是同一个失效模式 |
  | 4 | §7 第 10 条（**当轮写下、当轮改正**） | 引文「`docs/03:53` 确有…、`docs/03:55` 确有验收目标」 | `docs/03:23`（第 53 行是空行）、`docs/03:54`（第 55 行是空行） | 我写完这一条时**没有回表**，凭「刚才看的时候大概在这一带」填了行号，而那两个数恰好一个指向空行、一个指向空行。**同一错误的第六次**：契约缺口更正报告（`contracts-gap-list-corrections-2026-11-06.md`）的 §11（S14 的 41 处指错）、§12（S11 的 94 处里 8 处指错）、§16.11（改 README 时写成过期行号）、以及 `docrefs` 失败信息少报 1 行那 30 处 —— 全是同一个动作缺失。行号必须 `grep -n` 现查，不能从先前 `sed -n` 的窗口里推 |
  | 5 | §7 第 6 条「清单已交」 | 「commit `717e7c1`，分支 `claude/magical-mendeleev-14bc20`，**未合入 main、未推送**」+「要看这份清单，得 checkout 那个分支」 | 首版 `717e7c1` 在分支 `claude/check-registry-data`（**不是** `magical-mendeleev-14bc20`），且已随 `4254bdb` 收进本地 `main`；main 上是 **936 行**，首版 461 行 | 两处都是陈述过期：把提交所在分支写成了产物所有者的分支（两个名字都好记，就写混了），而「未合入」在 `4254bdb` 之后不再成立。**核对方式**是 `git merge-base --is-ancestor 4254bdb main`（YES）+ `git ls-tree claude/magical-mendeleev-14bc20 -- <path>`（空，文件不在那分支）+ `git show <ref>:<path> \| wc -l` 三个数。和第 3 行同族：**「当时为真」写成「现在为真」** |
  | 6 | §6.2 末条的范围标签（2026-11-08 第二轮复核） | 「S02–S18 / S35–S40 这 16 篇」 | 「S02 / S10–S18 / S35–S40 这 16 篇」 | **总数 16 是对的，范围写错**：把 S02–S18 整段当成了一个连续区间，那是 **23** 篇，其中 S03–S09 这 7 篇一个 `E\<NN\>-` 编号都没有（逐篇 `grep -cE 'E[0-9]{2}-[A-Za-z0-9]'` 全为 0）。16 = 1（S02）+ 9（S10–S18）+ 6（S35–S40）。错因是**用「跨度」代替「集合」**——和 §6.2 记的「用行数代替合规数」是同一族：都在拿一个方便得到的近似量顶替真正要数的那个 |
  | 7 | §2.4「落地的四个文件」表的 `validate_registry.py` 大小（2026-11-08 第三轮复核） | **5.3 KB** | **7.0 KB**（7024 字节，十进制 KB） | 该文件在报告写完之后又改过两次：`2534923`（jsonschema 缺失时 fail loud）→ 5401 字节，`ce90429`（`keyframe_edit` 的 ≤6 帧前置条件进加载期校验）→ 7024 字节。同表另外三个文件自 `991ccee` 起一次未改，所以只有这一格过期。和第 3、5 行同族：**「当时为真」写成「现在为真」**。核对方式是 `git log -- contracts/quality/validate_registry.py` 逐个 `git show <ref>:<path> \| wc -c`，不是 `stat` 一次就填 |
  | 8 | §3「G1 关闭后的 outbox 覆盖：17/17」+ §3.1 末段 + §3.2 前两行（2026-11-08 第四/第五轮复核） | **17/17**；**33/33**（S02/S22/S23 为合法引用）；内联 **10/10** 三列齐全；「S22 / S23 属于合法引用：两者明确写『与 S08 完全同构 … 见 S08 那节』」 | **按六列严格判**：17 份时可用 **1/17**（9 份有 DDL 但 8 份列不全，另有 S02/S16/S17/S18/S38/S40 无可用 DDL）；38 份时（`0e79249`，33/33 落笔那次）**15/38**；补完后 48/48、内联 46/46 六列齐全。「S22 / S23 与 S08 完全同构」这层说法只在 S22 原文里（S23 写的是「同 S08 …」再加本服务自己的约束）；`plans/` 里另有两处「完全同构」都与 S08 无关（S28 讲 `accepted_artifact_ref`、S30 讲 S29） | 三处都是「没回表」，但不是同一种：① **17/17 与 33/33 在任何一次提交里都对不出实际数字** —— 逐提交扫 `plans/`，内联 DDL 数依次 8→9→34→37→38→42→43→44，从不出现 33；17 份那次是 9 内联，从不出现 17。它们不是"数错的近似值"而是**没有对应量的占位断言**，算法不可还原；能确定的是把「有 outbox 字样 / 有 CREATE TABLE」当成了「有可用的 outbox」。② **33/33 另有个分母问题**：48 份落盘后没重算，和第 3、5、7 行同族。③ **10/10 对不出**：按"有 `发布` 表格行"得 18 份、其中三列齐全 9 份，按六列严格判 38 份时是 18 份，都不是 10。**我上一版在这里写的「`grep -c outbox` 大小写敏感」是编的，已被证伪**（S16/S17/S22 都有小写 `outbox`，只有 S23/S38 没有）；**再上一版以为「只有 S17 缺」也不对**——六列严格判时 38 份里 20 份列不全。核对方式是 `re.findall(r'CREATE TABLE[^;]{0,200}outbox', t, re.S\|re.I)` 再逐列校验 |
  | 9 | §3 第 254 行「当时那 17 份里实际只有 16 份」+ 第 8 行的「只有 S17 缺」+ 第 238 行「其余 16 份都有 outbox」（2026-11-08 第六轮复核） | **16 份 / 只有 S17 缺 / 其余 16 份都有 outbox** | 按 `0e79249^` 的实际文件内容：**11/17 有 outbox**（9 内联 DDL + S22/S23 引用），缺 S02 / S16 / S17 / S18 / S38 / S40；六列严格判只有 **1/17**（S08 自己）。`0e79249` 补了 4 份，剩 **S16 与 S17** 两份到 `2d1dc00`（2026-11-08）才齐。第 238 行那句本身是对的（`01d23e5` 逐篇 `re.search("outbox",t,re.I)` 得 16/17，只有 S25 没有），但「都提到」与「都有可用 DDL」不是一回事 | 前半句的错因是我**用 `HEAD` 的内容套那 17 个文件名**（`git ls-tree` 拿名单、`git show HEAD:` 读正文），所以看到的是今天的 S02/S38/S40 —— 它们早在 `0e79249` 就补过了；**判据抽了两层：名单来自旧提交、正文来自新提交**。后半句的错因是这次复核我又错了一次：第一版写「缺 S02/S17/S18/S38/S40」和「4/17」，那是我用 `re.search("S08")` 当"引用 S08 那节"的判据，把 S16/S40 里无关的 S08 提及也算进去了，还把 `aggregate_sequence` 当成了 `aggregate_seq` 去找。**判据比结论先要验证：宽松的正则比错误的结论更容易骗人。** 正确做法：对同一个 ref 既取名单又取正文（`git ls-tree <ref> plans/` + `git show <ref>:<path>`），引用用 `S08\s*§\s*1\.6|同\s*S08` 精确匹配，列名用 `aggregate_(seq|sequence)` |

  第 1、2 两处都已用 `§` 正则重新逐条核对：13 个 `plans/` 章节号 + 契约指向的两份 `docs` 章节全部命中真实标题，**没有一条是悬空的**——错的只是计数，不是引用。第 3 处不是计数错而是**结论过期**，核对方式是回表跑 `event-topics.v2.json` 的 55 条 `data_schema_ref` 并逐个 `json.load`，不是数文档。

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
5. ~~C9 待裁决~~ 已于 2026-11-05 裁决并落地（§2.5，`4f1bdcb`）：补登 10 条、删 2 条、
   消费侧改名 6 处，目录 48 → 55 条。**这项已补齐（2026-11-08 复核）**：55 个 `event_type`
   现在逐个有 `data_schema_ref`，**55/55 指向真实文件、55 个文件互不相同、零个空壳**
   （每份都有 `properties`，无一份 ≤2 个字段）。落地过程分了多次提交（`785b180` 起，
   `a172d89` 收尾最后 6 条），本报告 11-05 写「仍未落」之后才补完，属**陈述过时**而非当时的错。
   当初担心的 S42 聚合键逐事件不同（`candidate_id` / `mix_job_id` / `render_job_id`）
   已按事件各写一份解决，没有套通用 schema。
6. **99 条 check 的登记项字段仍缺**（§2.4 末尾）：`window_scope` / `escalation_rule` /
   `default_action` / `allowed_actions` / `recheck_scope` 要由四个 QC 服务各自补。
   **不能由别处推断** —— `contracts/README.md:84` 的原话是「不从散文推断这些值」，
   `window_scope` 决定 `keyframe_edit` 能不能用，猜错会让 S39 把 20 帧的连续窗口交给逐帧修补。
   **已派给 S35–S39 会话产出「缺字段清单」**（不是 registry 数据文件），把人工对照变成有 owner 的待办。

   **清单已交**：`delivery/acceptance/check-registry-missing-fields-2026-11-05.md`。首版是 `717e7c1`
   （在 `claude/check-registry-data` 分支），**该版已随 `4254bdb` 收进本地 `main`，main 上当前是 936 行，
   不是首版的 461 行** —— 本报告此前写「未合入 main、未推送」是**陈述过期**（2026-11-08 复核）。
   结论比预想的更差 ——
   99 条里没有一条能从表格机械提出全部字段；`window_scope` 与 `escalation_rule` 是 **0/99**，
   S36/S37/S38 的 76 条连动作列都不存在。它把缺口变成了 99 行带行号的对照表，
   每行写清"缺哪个字段 / 方案里有没有出处 / 没有就 `NEEDS_OWNER`"，**但没有填任何值**。

   本节此前写「74 条」，是**错的**。重数结果（按表格首格计，含不合规命名）：

   | 层 | 条数 | 其中 `check_id` 不合规 |
   |---|---|---|
   | TECH（S35） | **23** | 3 条 |
   | VIS（S36） | **32** | 0 |
   | AUD（S37） | **20** | 0 |
   | SUB（S38） | **24** | 0 |
   | 合计 | **99** | 3 |

   `contracts/README.md:73` 的 99 是对的；此前这里的 74 漏算了一整份方案的表。

7. **3 条 `check_id` 违反命名规约**（新发现）：[S35](../../plans/S35-technical-qc.md) 第
   773–775 行的 `TECH.BLACK_FRAME` / `TECH.FREEZE_FRAME` / `TECH.FLASH_FRAME` 只有两段，
   而同文件第 157 行定的规约是 `<LAYER>.<GROUP>.<NAME>`（全大写下划线，全局唯一）。
   三段式的其他 96 条都合规。两条影响：按规约写的解析器会漏掉这三条；
   `check-registry.v2.json` 若按规约分组，这三条无处归组。
   按文件语义应是 `TECH.FRAME.BLACK` / `TECH.FRAME.FREEZE` / `TECH.FRAME.FLASH` —— **但这要 owner 确认**，
   改名是契约变更，不能由验收报告单方面决定。

8. **`redaction-policy.v2` 的四个 `open_questions` 待裁决**（§2.6）：R-1 KMS 密钥归属与轮换、
   R-2 S46 的 `provider_credential` 该替换还是该 422、R-3 标记里要不要带检测器版本、
   R-4 侧车 vs 每服务自实现。**R-2 与 S46 §5.6 是真实张力**，按 AGENTS.md 两条都留在原地，
   等 S46 owner + 安全侧选边。契约现在是 `status: proposal`，实现期可以照它写，
   但**不能当最终答案** —— 尤其 R-1 没定，`class_a` 的 HMAC 密钥就没处放。

9. **方案内细分编号需要一套命名规约，目前不存在**（新发现，§6.2 末条）。42 个局部编号
   在 2 篇以上方案里同名不同事，全部行文不同；S15 另有 10 条用了 S12 的字母。
   这和上面第 7 条（3 条 `check_id` 少一段）是同一个问题的两个层次：`check_id` 有规约
   （`S35:157` 的 `<LAYER>.<GROUP>.<NAME>`）只是没遵守，细分编号则是**根本没定过规约**。
   两条路都成立——要么规定「方案内的细分编号只在本文件内有意义，跨文件引用必须带服务号」
   （改 3 处 README + 2 份 schema 的引用写法），要么规定「细分编号全局唯一」（S15 改 10 条 `C`→`F`，
   S12 不动）。**前者不动任何编号，后者动 23 处引用**（S15 10 + voice schema 10 + README 3；
   S12 自己的 6 处不受影响）。按 AGENTS.md「契约与文档冲突 → 停下」，
   这一条留给 owner 选边，本轮不施改。

10. **7 处 `§` 后面跟的不是章节号，两个检查器都看不见**（新发现，已就地改掉，2026-11-08）。
    `check_docrefs` 的正则是 `docs[\s/](\d{1,2})\s*§\s*(\d+(?:\.\d+)*)`，`check_planrefs` 是
    `S(\d{2})\s*§\s*(\d+(?:\.\d+)*)` —— **两条都要求 `§` 后紧跟数字**。于是 `docs/03 §SLO`、
    `S24 §"读 S01 超时"` 这类写法既不匹配也不算失败，`refs` 总数里没有它们，悬空检查也不会碰它们。
    全仓（跳过 `docs/design-package/`、`.claude/`、`_inbox/`，屏蔽围栏）共 7 处（下表 6 行 ——
    S48 那一行含两个引用）：

    | 文件:行 | 原写 |
    |---|---|
    | `plans/S22-scheduler-quota.md:925` | `docs/14 §恢复顺序——"…"` |
    | `plans/S42-composition.md:292` | `docs/10 §"解析成功不代表权利永远有效"` |
    | `plans/S47-realtime-projection.md:5` | `[docs/03 §SLO]` |
    | `plans/S48-adaptation-training.md:5` | `[docs/10 §资产]`、`[docs/03 §容量]` |
    | `plans/S28-artifact-ingest.md:831` | `S24 §"读 S01 超时"` |
    | `plans/S28-artifact-ingest.md:965` | `S40 §"所指 artifact 仍存在且 hash 匹配"` |

    **引文本身全对**，逐条回表核过（行号用 `grep -n` 现查，不是凭印象）：`docs/03:23` 有
    「训练不允许挤占在线推理保底容量」、`docs/03:54` 有验收目标（所在小节 `## 4. 服务等级与弹性`，
    成立于第 52 行）、`docs/10:14` 有那句话（`docs/10` 通篇只有 H1，无编号小节）、`docs/14:35` 有
    那串恢复顺序（所在小节 `## 5. 故障与容灾`，成立于第 31 行）、`plans/S24-model-registry.md:709`
    与 `plans/S40-human-review.md:522` 也对得上。**错的只是把 `§` 当成了"那段话"的引导符**，
    而不是章节号。所以改法是**留着引文、去掉 `§`**（`docs/03 的容量约束`、`S24 的"读 S01 超时"`），
    不是补一个章节号 —— 补号会把这 5 处 `docs/NN` 从"看不见"变成"计入 refs"，`docrefs` / `planrefs`
    的四个数会一起变，而 `contracts/README.md` 里钉着这些数、此刻有另一会话的未提交改动。
    改后实测 **`planrefs` 491/456/470、`docrefs` 468/444/488 与改前逐字相同，22 项全过**。

**给 `check_all.py` owner 的两条（本文件不归我改）：** 一、上述盲区是正则写法造成的，
把 `(\d+(?:\.\d+)*)` 放宽成"数字或非空非数字 token"就能看见它们，但会立刻在这 7 处转红 ——
应该先定「`§` 只能跟章节号」这条规约，再收紧；二、`docs/` 正文里 `contracts/README.md:1496,1555-1568`
的 `§N.N` / `§X` 是**格式说明里的占位符**，不是引用，放开正则时要把它们排除，否则会误报。

方案是设计稿。以上检查证明的是**文档之间自洽**，不是**设计正确**。E01–E40 里真正要证明的东西（跨租户拒绝、重复投递幂等、Saga 补偿、fencing、重启恢复）只能靠实现加测试证明 —— `docs/16` 的原话是「schema 校验通过 ≠ 分布式正确性」。
