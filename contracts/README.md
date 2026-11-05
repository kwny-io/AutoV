# 契约

这一层是**设计稿**，不是已发布的 API。它定义服务之间能说什么、事件长什么样、schema 长什么样。

原始契约说明已存档在
[docs/design-package/contracts/README.md](../docs/design-package/contracts/README.md)；
`docs/02-service-catalog.md` 是 48 服务全表。

## 布局

```text
contracts/
  openapi.v2.yaml              OpenAPI 3.1，21 个核心操作
  service-catalog.v2.json      48 服务元数据（services/*/service.yaml 的源）
  event-topics.v2.json         48 个 topic，注册 55 个 event_type
  mcp-tools.v2.design.json     12 个 MCP 工具（streamable-http）
  redaction-policy.v2.json     密钥脱敏规则表：secret 闭集、替换形态、兜底（见下）
  *.v2.schema.json             企业 schema + 单 topic 的 payload schema（见下）
  domain-v1/                   v1 领域 payload 基线，保留作字段参考
  examples/                    结构与示例（占位 hash / ID，不能当 fixture）
  quality/                     质检缺陷族与 check 登记项（见下）
```

## redaction-policy.v2.json：规则表，不是数据权威

S45 `PAYLOAD_SECRET_SCAN`、S46 `INGRESS_SECRET_SCAN`、S47 `PROJECTION_SECRET_SCAN` 和任意生产方的
`OUTBOUND_SECRET_SCAN` 都执行这一份。它定三件此前只有散文的事：

| 定什么 | 答案 |
|---|---|
| 什么算 secret | 6 类闭集（`api_key` / `bearer_token` / `signature_secret` / `provider_credential` / `private_key_material` / `connection_string`），外加 `not_secrets` 白名单（hash、opaque id、`kid_prefix`、`principal_id` 等，防止"越加固越没用"） |
| 替换成什么 | `class_a` → `REDACTED:hmac_<scope>:<first16hex>`（kms 密钥控的截断 HMAC，**可关联**）；`class_b` → `REDACTED:<class>`（**不可关联**） |
| 兜底 | `redact_and_store`，**唯一答案**。不拒收、不丢弃、不放行 |

**兜底只能是一个答案**，因为另外两个都是错的：拒收等于让生产方用一串密钥形状的字符串把自己的审计行抹掉（审计从"记录事实"变成"谁能控制自己被记录"）；放行等于把凭据存进保留期最长、导出路径最广的地方。

**顺序是硬约束**：脱敏在哈希之前。S45 若用明文算 `record_hash` 再存替换后的形态，链就复算不出来，该 stream 之后每条都校验失败；S46 若先算 `params_sha256` 再替换，合法重试会变成 `REQUEST_HASH_MISMATCH`。所以**存下来的就是 `canonical` 的原样**——验链器不需要知道这份契约，检测规则以后怎么改也只影响新记录。

这份文件是 `status: "proposal"`。四个 `open_questions`（R-1 KMS 密钥归属、R-2 S46 是否该把 `provider_credential` 当错误而非替换、R-3 标记里要不要带检测器版本、R-4 侧车 vs 每服务自实现）没有在方案层能给出的答案，需要平台/安全裁决。

## 事件 payload 怎么挂 schema：`data_schema_ref`

**envelope 不约束 payload。** `event-envelope.v2.schema.json` 里 `data` 只写了 `{"type": "object"}`
——所以 55 个 topic 虽然都填了 `schema_ref`，那指的是 envelope，`data` 里放什么形状的
东西，契约层从来没有说过。生产方按散文发，消费方按散文读，谁都没错，谁也校验不了。

要强类型就分两层：

```jsonc
{
  "event_type": "artifact.ingested",
  "schema_ref": "event-envelope.v2.schema.json",        // 信封：所有 topic 都指这一个
  "data_schema_ref": "artifact-ingested.v2.schema.json" // 载荷：只有需要时才加
}
```

**为什么是新增一个键，而不是塞进注册项现有字段：** `schema_ref` 的语义已经被 55 个 topic
钉死为 envelope，改它的含义等于静默改变全部 55 个 topic 的契约。加 `data_schema_ref`
是纯增量，缺省 = 没有 payload 契约（envelope-only）。

**这个键今天有 31 个 topic 带**（24 个仍是 envelope-only）。没带的不全是遗漏：payload
可能已经由自己的企业 schema 表达（例如 QCReport 走 `quality/qc-report.v2.schema.json`，
经 REST 传递），也可能本来就只发一个引用。**给某个 topic 加 `data_schema_ref` 是一次
契约变更**：生产方要按 schema 发，消费方可以开始校验，两边都得跟着动。

| topic | payload schema | 出处 |
|---|---|---|
| `artifact.ingested` | `artifact-ingested.v2.schema.json` | S28 §6.8 |
| `quality.technical.completed` | `quality.technical-completed.v2.schema.json` | S35 §7 |
| `quality.semantic.completed` | `quality.semantic-completed.v2.schema.json` | S36 §9 |
| `quality.alignment.completed` | `quality.alignment-completed.v2.schema.json` | S37 §7 |
| `post.subtitle.ready` | `post.subtitle-ready.v2.schema.json` | S38 §7 |
| `production.image.ready` | `production.image-ready.v2.schema.json` | S29 §6.4 |
| `production.video.ready` | `production.video-ready.v2.schema.json` | S30 §6.4 |
| `production.speech.ready` | `production.speech-ready.v2.schema.json` | S31 §6.4 |
| `production.lipsync.ready` | `production.lipsync-ready.v2.schema.json` | S32 §6.4 |
| `production.sfx.ready` | `production.sfx-ready.v2.schema.json` | S33 §6.4 |
| `production.music.ready` | `production.music-ready.v2.schema.json` | S34 §6.4 |
| `orchestration.run.state-changed` | `orchestration.run-state-changed.v2.schema.json` | S21 §5.5 |
| `scheduler.permit.changed` | `scheduler.permit-changed.v2.schema.json` | S22 §6.1 |
| `finance.ledger.entry-posted` | `finance.ledger-entry-posted.v2.schema.json` | S23 §6.1 |
| `generation.job.state-changed` | `generation.job-state-changed.v2.schema.json` | S26 §12.5 |
| `review.decision.recorded` | `review.decision-recorded.v2.schema.json` | S40 §10.2 |
| `delivery.package.published` | `delivery.package-published.v2.schema.json` | S43 §6.3 |
| `post.timeline.revision.created` | `post.timeline-revision-created.v2.schema.json` | S41 §6.3 |
| `post.master.rendered` | `post.master-rendered.v2.schema.json` | S42 §6.3 |
| `quality.repair.planned` | `quality.repair-planned.v2.schema.json` | S39 §9 |
| `connector.observation.received` | `connector.observation-received.v2.schema.json` | S27 §6.5 |
| `continuity.check.completed` | `continuity.check-completed.v2.schema.json` | S17 §5.2 |
| `context.pack.created` | `context.pack-created.v2.schema.json` | S18 §6.3 |
| `access.grant.changed` | `access.grant-changed.v2.schema.json` | S01 §4.4 / §11 C10 |
| `agent.task.completed` | `agent.task-completed.v2.schema.json` | S19 §4.3 |
| `prompt.compiled` | `prompt.compiled.v2.schema.json` | S20 §4.3 |
| `memory.canon.committed` | `memory.canon-committed.v2.schema.json` | S16 §3.7 |
| `snapshot.manifest.committed` | `snapshot.manifest-committed.v2.schema.json` | S08 §6.1 |
| `lineage.impact.computed` | `lineage.impact-computed.v2.schema.json` | S09 §5.4 |
| `asset.binding-set.created` | `asset.binding-set-created.v2.schema.json` | S10 §5.3 |
| `change.plan.applied` | `change.plan-applied.v2.schema.json` | S07 §5.5 |

六个 `production.*.ready` 是同一批加的，写法一致：字段名一律用生产方（S29–S34）的名字，
形状从各计划 §6.4 的示例取，**取值域只在 DDL 有 `CHECK` 时才收窄成 `enum`**。没有 DDL
依据的闭集一律留成非空字符串 + `x-needs-owner`——把计划里「建议的闭集」写成 `enum`
等于替契约做决定（见下面 `sfx_family` / `mood` 两条）。

`orchestration.run.state-changed` 到 `review.decision.recorded` 这五个是「状态机/账本/
决策」这一类，它们暴露的是另一种缺口：**状态闭集缺失**。S21、
S22 有真 DDL `CHECK`，S26 有 16 个状态却没有任何机器可读的闭集（`provider_job.state`
只是一列裸 string），S40 的 `status` 五值只写在一句 JSON 注释里。所以这五份 schema 里
`state` 收成 `enum` 的只有 S22；S26 留非空字符串 + `x-needs-owner`——把 16 个状态抄成
`enum` 会让散文里的排版漂移变成契约，而 §13 的失败分支表恰恰已经在混入非状态名字
（见 `generation.job-state-changed` 的 `x-conflicts` PGJ-1）。S40 是唯一的例外：
`status` 收成 `enum`，因为那 5 个值在注释里是逐一列举的，不是散文推断。

S42 / S43 / S41 这三条是又一种缺口，值得单说：**生产方宣布了一条事件，然后只给其中一部分
写了字段**。S43 四条事件里只有 `delivery.package.published` 有字段清单，S42 三条里只有
`post.master.rendered`，S41 一条事件有字段清单但消费方（S42）把它里面的字段叫成另一个
名字。所以这三份 schema 都只覆盖「有形状的那一条」，其余的在 `x-needs-owner` 里点名
而不猜——它们的名字已经在 `event-topics.v2.json` 里登记过了，缺的不是登记，是形状。

`post.master.rendered` 还多一层：它的 `data` 块里有个 `qc_trigger_requested`，**全文只在
那一段示例里出现过一次**——没有对应 DDL 列、没有流程步骤、没有指标。一个必发字段的语义
完全悬空，见 PMO-2。这条事件的响度对象同样是三处拼接（事件三字段 / 测量表十二列 /
§4.5 七字段），而丢掉的那个恰好是唯一带语义的 `conforms_to_spec`，见 PMO-1。

`quality.repair.planned` 是第三类：**事件里的字段有一半没有落库处**。S39 的 plan 表有
`state` / `artifact_sha256` / `episode_id` / `run_id` / `escalation`，事件里也发了
`target_count` / `action_count` / `top_action` / `estimated_cost_band` 四个——这四个没有
任何 DDL 列，是「折叠值」，可从 `snapshot` 与 `action` 表重算出发出时刻的数值。其中
`top_action` 的「top」按什么排序全文没有定义（动作数 / 成本 / `sequence` 最前，三种口径
在多动作计划上答案不同），见 x-needs-owner。**取值域还是收窄了**（`action_enum` 12 值）：
枚举约束的是「哪个名字合法」，与「哪个是 top」是两个问题，前者有 S39 §11 C5b 自己要求的
机器可读闭集作依据。

另外三处是**主动收窄，不是照抄 DDL**，所以和上面六个 `production.*` 的口径不同，值得说明：
`artifact_sha256` 收 `^[0-9a-f]{64}$`（DDL 是裸 `text`），因为 S39 §4.2 步骤 2c 要拿它和
三份 QC payload 的 `artifact_sha256` 做相等比对，而那三份全是裸 hex——不收窄，schema 就
拦不住一个会让整条 ingest 永久 quarantine 的写法；`estimated_cost_band` 收 `low|medium|high`
（取自 §4.5 行 320 的竖线枚举，本仓库既有写法），且注明它只是档位词表、不是预算结论。
`recheck_scope` 则**不收** enum 也不设 `minItems`：三套语法互不兼容（见下），且 target 为空
时并集就是空数组。

更硬的缺口在 `shot_lineage_id` 与 `recheck_scope` 两个名字上。`shot_lineage_id` 事件里
只有一个，可 plan 表根本没有这一列（它只在 per-attempt 的 `attempt_ledger` /
`attempt_reservation` 上），而一条计划可以覆盖多个镜头——`target_count: 2` 的两个 target
未必同一个血缘（RPP-3）。顺带记一笔：§11 C4 说它「在任何契约里都没有定义」**前半句已过时**
（四份契约都有这个字段），但**后半句仍然成立**——四份都只收非空字符串，没有一份写生成规则
或不可变性，而它是 E26 跨 Run 累计唯一的锚点。`recheck_scope` 则是同一个名字在三个地方三个
域：事件是 `text[]` 用 `layer:aspect` 冒号语法，`check-registry.v2.schema.json` 是**单字符
串**用 `+` 连接，S39 §6.3 自己的枚举列表里又漏了它下一行示例用的 `hash` 与 §4.5 用的
`identity`。S39 §6.3 明说要「取所有 target 的并集」才算复检范围，也就是说它必须把注册表
的 `+` 串翻译成自己的 `:` 数组——而**翻译规则不存在**。拆错一层，少跑的那层恰好是
「修复是否引入新缺陷」的证据，所以这是 RPP-4 而不是排版问题。还有一处名字级别的错：
§7.1 行 744 把这个事件叫 `repair.plan.created`，而目录里只有 `quality.repair.planned`
（RPP-6）——照着写会产出一条无 topic 的事件，而「与计划落库同事务」看起来仍然满足。

`connector.observation.received` 是**第四类，也是最难受的一类**：它的 payload 是完整的
（11 个字段、S27 §6.5 有整段示例），可它**根本发不出来**——envelope 要求 `aggregate_id`
非空且 `aggregate_sequence >= 1`，而 S27 §9.2 明说 `aggregate_id` 可空、§6.5 明说
`aggregate_sequence` 由 S26 分配而不是 S27，因为「S27 看不到 job 的完整序列」。而
`aggregate_id` 为空的那条路径恰好是 docs/16 E17「回调早到」——`UNMAPPED` 观测
（§6.4 行 580–581），它必须发出去，否则 S26 在 deadline 前一直停在 `RUNNING`。示例里
那个 `aggregate_sequence: 1` 是占位，不代表 S27 的任何真实知识（PCR-7）。同一个计划里
`partition_key` 也写成 `tenant_id:aggregate_id`（catalog 与 §11 行 790 一致），在
`aggregate_id` 为空时同样退化。这条事件还有两处是「语义没定就先把字段发了」：
`status` 说是 6 个值、同一句话又要求未知值报 `inconclusive`（PCR-1）；
`provider_usage` 报供应商的计费秒数，而 S23 是唯一钱权威，它是观测还是计价输入没人划清
（PCR-4）。所以这份 schema 刻意**只约束到「S27 能发出什么形状」**，不替 S26 的状态机
裁决语义。

`continuity.check.completed` 是**第五类，缺口换了位置**：payload 完整、envelope 也发得出
（`aggregate_id` 是 `series:branch:episode` 这种真串），可它**进了 S39 的门也走不到修复**。
S39 的检查登记表 `check-registry.v2.schema.json` 在 `x-cross-checks` 里写死
「`owner_service` matches the layer: technical→S35, semantic→S36, alignment→S37,
subtitle→S38」，而 `check_id` 命名空间只有 `TECH`/`VIS`/`AUD`/`SUB`——S17 是第五个生产者，
`chk_` 是第五种前缀，**结构上不在表里**。后果按 S39 自己的两条规则取最坏的：§4.2 行 208 说
校验失败即 `quarantine`、不 best-effort，那报告直接进死信；§3.1 行 104 说登记项无效即
`human_review`，那每条连续性缺陷都丢掉自动化。而 S39 §11 C8 说这条事件存在的全部意义就是
「否则跨集冲突永远到不了修复决策」。**所以本 schema 只是把门修到「能被校验」，门后面那段
路仍是断的**（CCT-4）。

还有三处同类的形状缺口，都不是笔误而是两段散文各写各的：S39 §5.3 的六行分派表以
`fault_hypothesis` 为依据、并说 `fault_owner_service` 是左列，可 §5.2 的事件示例里**这两个
字段一个都没有**（CCT-2）；§5.3 行 382 把「script + blocking 必须附剧本 source span」写成
硬约束，同样没有出现在示例里，于是 S39 既看不见 span，也就无法执行它自己那条
「缺 span → `needs_human`」的降级路径（CCT-3）；`memory_checkpoint` 在 §1.1 行 35 是三键
（含 `branch_id`）、在 §5.2 行 353 是两键，而 S16 把同一个量叫 `checkpoint_hash`——少一个
`branch_id`，「checkpoint 27」在多分支项目上就是歧义的，而 §6 的 `CHECKPOINT_MISMATCH`
判定正要比它，比错分支会报假 mismatch 导致整条检查白 abstain（CCT-5）。示例文件里补了
前两个字段并有出处，`branch_id` 收成可选（两种写法都能过）。最要命的是严重度：
S17 是 `none|info|low|medium|high|blocking`，S39/S35/S36 与注册表 schema 是
`blocker|major|minor|info`——`blocking` 与 `blocker` 差两个字母，按错词表查表落空后最常见的
写法是取默认权重 0，于是最该先修的缺陷排在 `info`（10）后面（CCT-1）。

`context.pack.created` 是**第六类：名字撞上了已有的企业契约，但装的不是那东西**。仓库里
早就有 `context-pack.v2.schema.json`（18 个必填字段、带 `claim_refs`/`asset_refs` 数组），
而 S18 §6.3 的事件是 9 个字段的摘要、只发 `claim_ref_count`。按本仓库给 topic 配
`data_schema_ref` 的默认做法「有企业 schema 就复用」，会得到一个**每条都失败**的契约：
把 §6.3 的示例喂给企业契约量到 12 个错误。失败模式最难查——生产方照示例发、消费者在
inbox 前全拒，S09 的依赖边一条也建不起来。所以这份 schema 独立成文件，示例里连
`claim_refs` 都不许出现（负向探针会拒），意思是「摘要就是摘要」。（CCP-1）

但这同时暴露了更要紧的一条：S09 §5.4 行 785 明写它的处理是「按 `claim_refs` 写
`PACK_USES_CLAIM`」——**计数没法建边**。所以这条事件现在「能被校验」和「能被使用」之间
还差一个字段（CCP-2）。同类还有 `tokens_used` 发了、`token_budget` 没发：3180 是好是坏
取决于预算，而看不到预算就看不出这个包有没有被裁剪（CCP-5）。另外这条事件与 S17 那条
用了**同一个** `aggregate_id` 字面量 `series_demo:canon:ep003`，却各自发 `31` 和 `12`——
按 catalog 的 `tenant_id:aggregate_id` 分区，两个只读服务的序号会在一处交错（CCP-4）。

`agent.task.completed` 是**第七类，也是最空的一类：消费者根本不存在**。前六类都还能指着某处
说「这里断了」——字段缺、取值缺、信封拦住、闸门排除、名字撞车。这一条不是：S19 §4.3 行 553
用加粗写了「**只有 S21（编排）与 S44（成本分析）**」，而这两个服务的方案里 grep 这个事件名
命中 **0 次**——S21 §5.6 的消费表 5 条没有它，S44 §6.3 的消费行 9 条也没有它。同一条链路
在 S19 自己的 §10.5 行 1167 又要求 S40 消费它建 `ReviewTask`，可 S40 §10.2 的消费行 3 条同样
没有，反倒在自己的缺口表 G4 里写着「补 task 生命周期事件」——**S40 把「没有这个事件」当成缺口
登记了**。仓库范围内它只出现在 5 个文件：S19 方案、只读设计包 ×2、`event-topics.v2.json`、
`service-catalog.v2.json`。后两个是登记表，前两个不产生消费者。所以这份 schema 让事件
「能被校验」，而它**仍然没有人收**（ATC-1）。§4.3 说「只有两个」、§10.5 又加上第三个，是同一份
方案内部的自相矛盾。

其余几处都源于「示例只画了一种终态」：§4.3 的示例是 SUCCEEDED 全套值（`result_ref` 非空、
`data_ref` 非空），而状态机有四个终态，DDL 的 `result_ref` 可空、`task_result_once` 唯一索引
就建在 `WHERE state = 'SUCCEEDED'` 上——所以另外三种终态**必然**是 `result_ref: null`。照示例
实现的人会在第一条 FAILED 事件上崩，而 FAILED 恰恰是告警最关心的一类（ATC-2）。`NEEDS_REVIEW`
还多一层：§3.6 行 404 的守卫要求「必须携带 `unresolved_issues[]`」，可那是 `task_result` 的列，
不是 `agent_task` 的列，而事件 payload 里没有这个字段——它到底有没有结果行，全文没说。
`state` 本身也两套：schema 收 4 个终态、DDL 的 CHECK 有 7 个（含三个非终态），两处取值集不同
而没有任何一处写明这是刻意的（ATC-3）；`stop_reason` 是扁平 12 值，与四个终态之间没有配对
约束，于是 `state=SUCCEEDED` + `stop_reason=STEP_LIMIT` 能通过校验——而 S19 §8 的告警正是按
`state` 与 `stop_reason` 两个标签一起立的（ATC-5）。`role` 的 13 值闭集只存在于
`tools/scaffold/generate_agents.py` 的 `ROLES` 列表里（catalog 里 grep 命中 0），而它是
路由键（ATC-4）。`usage` 则同名不同量：事件 5 键、查询响应 8 键（ATC-7），链也拼不出——
发了 `root_task_id` + `correction_round` 却不发 `correction_of_task_id`（ATC-6）。

`prompt.compiled` 是第七类的**第二次出现，而且这次连「谁是权威」都不止一句话**。它有两个
前六类都装不下的特征。第一个是名字撞车，但比 CCT/PCR 那几例更绕：**同一个 `template_ref`
在同一份方案里指两个不同实体**。§4.3 行 540 的事件里它是三键 `{id, revision, content_hash}`，
指向 `TemplateRevision`（示例值 `tpl_shot_video` rev 12）；而 §10.3 行 1189–1196 花一整节
锁定 `contracts/generation-command.v2.schema.json` 的 `template_ref`：四键、`service_id` 恒为
`"S20"`、**`id` 是 `manifest_id` 不是 `template_id`**。两者同名、形状不同、所指实体不同。
实测生成命令的契约里 `template_ref.required = ['service_id','id','revision','content_hash']`
且 `additionalProperties: false`，所以把事件里那个三键直接塞进命令会被契约拒收——**这个失败
是可观测的，因而是好的**；真正的坏路径是实现者「补上 `service_id: "S20"` 让它过校验」，
得到一份形状合法、语义错误的命令，S26 回读 manifest 时 404，而报错信息会把人引向
「manifest 不存在」而不是「字段填错了」。§10.3 只警告了「`id` 该填 manifest 不该填 template」，
没警告「事件和命令里这是两个字段」（PCM-1）。

第二个是 `warnings[]`：三个取值有名字（`ADAPTER_FALLBACK` §3.4 行 345、
`OPTIONAL_OMITTED` §3.6 行 411、`UNMAPPED_REFERENCE_APPENDED` §3.5 行 371）、**零个非空示例**
（§4.1 行 504 与 §4.3 行 542 两处都是 `[]`）、元素形状全文未定义，而 §4.1 行 510 说它进
`content_hash`。形状未定等于可复现性未定：hash 的唯一用途是相等比对，`manifest_input_once`
唯一索引的最后一个分量就是它——序列化顺序、键序、去重规则任一处没定，同一输入组就会建出
两份 manifest，而 §6.6 行 914 承诺「三者输入相同 → 全部返回同一 `manifest_id`」。§8 行 989
的指标又要按 `warning_code` 打标签，所以「纯字符串」和「对象」两种形状都不够用。schema 收
`string[]` 是三种可能里最保守的一种，不是裁决（PCM-2）。

消费侧与 ATC-1 同形，但三处说法互不相同：§4.3 行 551 说消费者是「S25（缓存失效）、S26
（可选：预编译校验）、S47（投影）」，§10.2 行 1185 的表只让 S47 消费、S25/S26 走同步读，§10.4
行 1215 又只提 S47。实测这三份方案里 grep `prompt.compiled` **全部 0 次**，仓库范围内它只出现在
`plans/S20-prompt-compiler.md`、只读设计包 ×2、`event-topics.v2.json`、`service-catalog.v2.json`
五个文件（PCM-3）。另外事件没有 `modality`，而 DDL 行 694 有这个列、§5.1 行 607 有 9 值
`CHECK`，S25/S26/S47 三者恰好都要按模态分流，只能经 `data_ref` 回查（PCM-4）；`manifest_ref`
与 envelope 的 `data_ref` 写的是同一份 manifest（同 id、同 revision、同 hash），JSON Schema
表达不了「两处必须相等」，两个字段由不同代码路径填，任一处漂移都会让下游按不同字段读到
不同 manifest（PCM-5，实测两处 hash 改成不同值时 envelope 与 payload 两层校验都通过）。

`memory.canon.committed` 的缺口和上面三类都不一样：**它有四个真消费者，是「发了一半」**。
S09 §4.3 行 596 拿它当闭包种子、S18 §6.4 行 487 更新 `claim_index` 与 watermark、S07 行 912
标 `stale_memory`、S08 行 680 刷新 `max_checkpoint_sequence` 投影——grep 全部命中，不是空转。
问题是 DDL 有 18 列，§3.7 的事件只发 12 个，而缺的恰好是消费方判别的依据。最尖锐的是
`commit_kind`（六值只在列注释里，无 `CHECK`）：`merge` 与 `revert` 两种提交必须带
`merge_source_refs` / `revert_of_sequence` 才能解释自己做了什么，而事件里两个都没有——于是
**一条回滚事件与一条普通修改事件在 payload 上完全同形**（实测：只把 `commit_kind` 从
`extract` 改成 `revert`，两层校验都通过，键集完全相同）。回滚本是为了撤掉一次错误的改稿，
S09 却会照 `changed_claim_refs` 再跑一遍同样的重生成，代价正好相反（MSC-4）。

两处字段名对不上，且都是静默失效。`affected_story_window` 的区间字段：S16 §3.7 行 518 写
`from_tick`/`to_tick`，S09 §4.3 行 601 写 `tick_start`/`tick_end`，而 **S16 自己的
`commit_manifest_binding` DDL 行 148–149 也写 `tick_start`/`tick_end`**——DDL 站在消费方那边。
S09 读不到窗口就被当成「覆盖 0 集」，两个空集求差集为空，连它自己的 `DEGRADED` gap 都不会
记（MSC-1）。`branch_id` 则**根本不在 payload 里**：而序列号是按 `(series, branch)` 单调的
（DDL 行 98）、`checkpoint_hash` 的哈希输入含 `branch_id`（行 126）、S18 的水位是按分支推进
的（§6.4 行 493 + §6.5 行 500 的「只增不减」）。没有它，一个 `experiment` 分支跑到 seq 50
会把 `canon` 的水位顶到 50，此后 canon 真缺的 seq 30 永远不会被回源补——乱序保护反过来成了
丢数据机制；S09 §4.3 行 600 的示例却写着 `"branch_id": "main"`（S16 DDL 行 61 说默认分支叫
`canon`），两边对不上（MSC-2 / MSC-7）。schema 里加了 `branch_id`，**这是唯一一处超出 §3.7
的字段**，理由是缺了它 S18 的水位必然错——但加字段是替 S16 做决定，所以标了冲突而非静默补。

还有三处「送了个可推导的、缺了个有用的」：`previous_sequence` 恒等于 `commit_sequence - 1`
（`seq = head+1` + 分支内无洞 + 主键），真正能证明「中间没丢提交」的
`previous_checkpoint_hash` 却不在事件里，于是缺事件只能等 60 秒超时（MSC-5）；
`commit_manifest_binding` 的主键含 `episode_id`，说明一个提交可绑多集，而事件的 `episode_id`
是单值，跨集改稿这个 E08 主场景装不下（MSC-6）；`source_manifest_ref` 在 DDL 可空（`branch_init`
/ `merge` / `revert` 本来就无 manifest），事件把它画成必填，照 DDL 发的合法事件会被本契约拒
（MSC-3）。

`snapshot.manifest.committed` 是**唯一一条被自己的 outbox 约束挡在门外的事件**，而它不是没
人要：S21 §3.3 与 §4.4 有完整处理逻辑、S03 行 441 靠它判 `episode.completed`、S16 §3.5 拿它当
提交通知源、S09/S02/S04/S07/S10/S29 都有消费行。问题在于 §6.1 行 643 把 `aggregate_sequence`
定成 `revision`，而 outbox 的唯一键是 `(aggregate_id, aggregate_sequence, event_type)`
（DDL 行 211，行 222 还特意论证「一次提交只可能产生一条」）。可是 `manifest_revision` 的状态机
（§1.8 行 449–456）有三个**不推进 revision** 的出边：`:accept` 全接受（§3.3 行 511 明写「6) outbox：
data 里带 `accepted_pointers_delta`」）、`:fence` 置 VOIDED（§4.3 行 588 明写「**必须**……发布
事件」）。于是 `:commit` 之后再 `:accept`，第二次 `INSERT INTO outbox` **必然撞唯一约束**——
要么接受事件根本没发出（S21 的「只是指针变了」分支永远不触发、S03 的 `episode.completed` 永远
不成立），要么实现者改 `aggregate_sequence` 而破坏 §6.1 声明的语义，要么 `ACCEPT` 得是一条
**新事件**（catalog 里没有，正是 ATC 那一类「发了没人收」）（SMC-1 / SMC-7）。

`FENCE_VOID` 那一支更绕：一次 `:fence` 是**分支级**的，会把同分支下多个 manifest 的多个
revision 一起作废（§2.4 行 323 的响应是 `voided_revisions: [12, 13]`），产生 N 条事件，而每条
都带一个 `manifest_ref`（指向被作废的旧 revision、其行的 `execution_fence` 还是旧值）和一个
`execution_fence`（新值）——**这两个字段一条事件里必然不自洽**。payload 缺 `previous_fence`，
而同一事件的 `changed_node_epochs` 恰恰用了 `from`/`to` 一对，两种 epoch 的表达方式不一致；
S21 §4.4 行 696 的判据「`trigger = FENCE_VOID`，**或者** `execution_fence` 从 2 变成 3」要求的正是
一个差值，而 payload 只给一个数（SMC-2）。`branch_id` 也不在 payload 里，所以消费方连「这次
fence 影响了哪些集」都聚不起来（SMC-6）。

另有两个数组**完全没有形状**：`accepted_pointers_delta`（§6.1 行 660 示例 `[]`，全文零个非空
示例）与 `source_revisions`（行 662 是一句 `/* 有变化的引用 */` 注释占位，DDL 行 80 只说「冗余
存储，用于索引查询」）。前者被 S21 §3.3 行 551（判「要不要跑子图」）与 S03 行 441（判
`episode.completed`，需要知道「哪个节点成了 master」）两处消费，两者需求还不一样；后者要撑
索引就必须固定路径，而 `SOURCE_REVISION_STALE`（§3.1 行 420）正是按它查存在性。schema 对这两个
只收 `array`、元素不设防——**与其发明一个形状，不如把缺口留在冲突表里**（SMC-3）。

`changed_node_epochs` 有形状（`{node_id, from, to}`），但 §2.2 行 288 的响应示例里带着
`{"node_id":"shot_3_4.video","from":2,"to":2}`——**一个没变的 epoch**。按 §3.2 行 475，`input_fingerprint`
没变则 epoch 不动，在途任务**仍然有效**；而 S21 §3.3 行 546 与 §4.4 行 685 的匹配条件是
`node_epoch = from`，`from == to` 时条件成立，于是它会把没过期的任务取消掉——S21 自己犯了它
§4.4 行 690 明文禁止的「把 `node_epoch` 当 `execution_fence` 用」，只是触发路径不在禁止条款的
覆盖内（SMC-5）。

最后两处是「首版状态装不下」的第三次重复：`parent_manifest_ref` 的父版本在 DDL 可空
（`parent_revision IS NULL`，行 88，revision 1 就是这种）、`accepted_revision` 在 DDL 可空
（行 49，`NULL = 从未接受`，刚 `:commit` 的 rev 1 正是这种），而 §6.1 的示例是 rev 13 且已接受过
12，两个字段都恰好有值。schema 收成必填（照示例），代价是 DDL 明确允许的首次提交事件被拒
（SMC-4，探针确认 `null` 两处都被拒）。此外 `parent_manifest_ref` 收完整三键，而 DDL 只存一个
`parent_revision` 整数——`id` 与 `content_hash` 必须回查 `manifest_revision` 才拿得到，全文没
说要做这一步（SMC-4 后半）。

`lineage.impact.computed` 是**第四种缺口形状：字段是全的，消费者是零**。S09 自己就在 §9 的
C09-3 里登记了「注册了但无 payload schema」，所以补 schema 这件事是兑现它自己的待办。但补完之后
暴露出更难的问题——**这条事件没有消费者**。S09 §5.4 行 779 点名两家：S07「组装 ChangePlan、
校验 `analysis_hash`」、S47「影响面板」。S07 §5.6 行 914 的标题就是「**S07 不消费
`lineage.impact.computed`**」，理由是「影响分析是 S07 自己同步调用的（`POST /impact-queries`），
响应直接进 plan。消费事件会导致『用别人算的闭包做自己的计划』」；S47 §2.1 行 73–86 的订阅表列了
10 条事件，**没有这一条**，全文 grep `lineage` 零命中。catalog 里也没有任何服务的 `consumes`
含它。于是它 `aggregate_sequence` 恒为 1（§5.4 行 754「每次分析只发一次」）、`replay_side_effects:
false`、字段与 §5.2 的同步响应几乎完全重叠（`analysis_hash` / `mode` / `lineage_watermark` /
`expires_at` 四处都有）——**加它的成本是真实的，收益是零**（LNI-1）。但这不能靠删解决：
`services/lineage-impact/service.yaml` 已声明它，删要改 catalog。

字段层面还有两处「事件比源头少」。`analysis_hash` 是 `sha256(JCS(result))`（§2.3 行 297），
`result` 有 7 个键（行 324–339），事件只带其中 5 个——**缺 `reuse_artifact_refs` 与
`derived_adjustments`**，所以消费方**无法重算哈希**，`analysis_hash` 退化成不透明令牌；
而唯一能拿来比对的 `GET /impact-analyses/{id}` 在 `openapi.v2.yaml` 里还不存在（C09-1）。
`watermark_source`（DDL 行 294）也不在 payload 里，而 `lineage_watermark` 的定义就是「那几路各自
`applied_sequence` 的 min」（§1.5 行 118）——**不给组成，这个 min 无法验证**，也看不出哪一路
`degraded`（行 125）（LNI-2）。

§2.3 行 342 说 `result` 与 `enterprise-change-plan.v2` 的 `impact`「逐字对齐」「不需要二次映射」，
但既有契约的 `impact` 只有 6 键且 `additionalProperties: false`，`derived_adjustments` 在**顶层**。
照字面直接赋值会校验失败（探针确认：给事件加 `derived_adjustments` 被拒）——这不是措辞不精确，
§2.3 明说「不需要二次映射」，实现者会真去这么写（LNI-3）。S09 §9 的 C09-5 讨论了同一片区域但
方向不同：它说的是 disposition/reason 无处安放，**没提键位错配**，所以它与 §2.3 行 342 互相矛盾。

两处失效通知的缺失也记在这里。`COMPUTED → STALE_FENCE`（§3.1 行 504）「不带副作用」，即**不发
事件**——而 S08 的 `:fence` 是故障切换的正常路径，一次就能让分支上所有在途 analysis 作废，每个的
`expires_at` 都还在未来。于是「还新鲜」「fence 提前作废」「自然到期」三种处境对消费方完全同形，
只能靠时间判断（LNI-6）。另外 `base_manifest_ref` 与 S08 事件的 `manifest_ref` 同形却无关联，且
`impact_analysis` 没有按 `base_manifest_ref` 建的索引（DDL 行 314–319 只有 by_plan 与 by_branch），
反向找「哪些 analysis 基于这一版」要全表扫 jsonb（LNI-4）。

`seed_kind` 三值只在行尾注释、**无 `CHECK`**，却出现在部分索引谓词里（`WHERE seed_kind =
'CHANGE_SEED'`，行 316）——写错大小写不会报错，只是索引静默失效。同一建表语句里 `mode` 与
`status` 都有 `CHECK`，唯独它没有，这更像遗漏而非刻意开放的集合。而 `seed_ref` 连形状注释都没有
（紧邻的 `base_manifest_ref` 有），三个 `seed_kind` 只有 `CHANGE_SEED` 的 `{plan_id}` 有出处
（LNI-7）。`superseded_claim_refs` 则与 S16 同名数组同形同 `content_hash` 语义，可复用，但 revision
的所指不同、且 S09 的分析最多滞后 30 分钟（LNI-8）。

`asset.binding-set.created`（S10）是**第八类的一次新变体：payload 形状是全的，但它的哈希
没有可哈希的对象**。七个字段在 §5.3 行 609–614 全有示例值，`entry_count` 与
`owner_service_counts` 之和还互相对得上——这份 schema 的困难不在抄形状，而在它引用了一个
仓库里不存在的契约。`binding_set_revision.content_hash = sha256(JCS(payload))`（DDL 行 186）
中的 `payload` 按 DDL 注释是「完整 binding-set.v1」，而 §11.1 行 816 把
`contracts/assets/binding-set.v1.schema.json` 列为「**必须先于任何服务代码**」的产出——今天
`contracts/assets/` 这个目录都不存在。JCS 能规范化键序和数字格式，规范化不了一个还没定义的
字段集，所以 `content_hash` 今天是**对一份未冻结文档的哈希**，而这个 hash 要经
`binding_set_ref` 进 `episode-manifest.v2` 当整集溯源的锚点（ABS-1）。这大概是全仓库最诚实的
一处缺口：S10 自己写了「契约先行」，只是那个文件从未被建出来。

`rights_revision_set_hash` 是同一片区域的第二个洞，形态不同：**哈希有字段，算法没出处**。
DDL 行 190 只说 `rights_pointer_set` 是「去重后的 `[{rights_owner,rights_id,rights_revision,
rights_hash}]`」，不说去重键是四键全等还是 `(rights_owner, rights_id)`、也不说哈希输入是
数组整体 JCS 还是只取 `rights_hash` 排序拼接。而 §8.5 的 E30-A3 要求「`rights_revision_set_hash`
与 S02 记录的 hash 不一致 → S02 判 `RIGHTS_REVOKED` 或重新取 binding revision」——**两方必须
独立算出同一个值**。算法不同则这条验收项稳定判「不一致」，于是 S02 要么永远拒绝派单（安全但
瘫痪），要么永远多一次回查（贵，且真撤销被掩盖）。方向上它和 C10-4 / C10-6 是同一片区域：
那两条解决了「`rights_ref` 的形状」和「权利版本放进 `content_hash` 所以 manifest 间接固定」，
**没有一条解决「怎么算」**（ABS-3）。

第三个洞是消费方侧的，方向和 ATC/LNI 相反。C10-3（§10 行 804）写「`event-topics.v2.json`
只登记 `asset.binding-set.created`……但 S07/S09 需要感知绑定变化」，而实际：S09 §5.4 行 790
订阅的名字是 **`asset.binding.changed`**——55 个 topic 里没有这个名字；S07 §5.6 的消费表只有
三条（S08/S02/S16），全文 `asset.binding` 零命中。于是 S09 按名字订阅会**永远收不到**绑定变化，
`ASSET_BINDING` 边在「换素材」这条路径上不写；就算它改订 `created`，payload 里也没有条目级的
引用细节（只有 `entry_count` 与按 owner 的计数），建不出具体的边。这和 S09 的 LNI-1 是镜像：
那边是「发了没人收」，这边是「有人要收、收了个不存在的名字」（ABS-2）。C10-3 只提 S07/S09
要感知，从没点名 S09 订阅的名字，所以这处不一致谁都没登记过。

剩下四处较小的：`entry_count` 数的是 overlay（`binding_entry`，草稿层）还是展开结果
（`binding_expansion`），事件里看不出来，而两者在 scope 复用下差一个量级（一条
`scope_kind='series'` 的 entry 展开成 40 个镜头）——示例的 6 与 `owner_service_counts` 之和
相等，恰好把两种解释都圆住了（ABS-4）；`superseded_revision` 照示例收成必填，而首次封存时
`head_revision` 还是默认 0、事务第 6 步 `UPDATE 旧 revision SET state='SUPERSEDED'` 无事可做，
于是**每个 binding set 的第一个 revision 都会被这份契约拒**——这是「首版状态装不下」的第四次
出现（SMC-4 的 `parent_manifest_ref`、MSC-3 的 `branch_init`、LNI 的 `seed_ref` 之后）（ABS-5）；
`aggregate_id` 在正文写作 `tenant_id:binding_set_id`、在 catalog 写作 `tenant_id:aggregate_id`、
示例里叫 `bs_01J...`，三处能对齐但写法不同，而 outbox 的
`UNIQUE (aggregate_id, aggregate_sequence, event_type)` 与 `aggregate_sequence = revision` 组合
起来**这里反而是安全的**——`state` 只有 `SEALED`/`SUPERSEDED` 两态且后者不另发事件，所以
S08 的 SMC-1 那个「同一 revision 发两条」的问题在 S10 不存在（ABS-6）；`episode_id` 在方案集里
写成 `ep3`（S10）、`ep003`（S09）、`manifest_ep3`（S08），词法从没被定义过，示例层面无害、
跨服务 join 会静默落空（ABS-7）。

`change.plan.applied`（S07）是这批里**唯一一份标量字段全有 DDL 依据、却坏在外面**的契约。
`plan_hash`（`cp_hash_shape`）、`currency`（`cp_currency_shape`）、`execution_fence`
（`cp_fence_positive`）、`estimated_cost_upper_micro`（`cp_money_nonneg`）、`applied_at`
（`cp_applied_has_ts`，而且还是双向等价）五个字段都能指着建表语句的某条 `CHECK` 说「就是
这个取值域」——所以这份 schema 的取值域是全仓库最不含糊的（CPG-7 是唯一的例外）。它的问题
全部在**往外看**。

第一处最硬：S07 §5.5 行 895 用加粗写「**S21 消费这个事件吗？不**」，并给了理由（「S21 从
`snapshot.manifest.committed` 拿 `changed_node_epochs` 就够了」）；而 S21 §5.6 行 889 把同一
事件列进「消费的事件」五条之一，还配了处理动作和 inbox 键 `s21.changes`。**两边都写得很具体，
所以这是真矛盾**。若订阅，S21 会在两个 topic 上收到同一次修改的两条事实（本事件与 S08 的
manifest 事件都带 `changed_node_epochs`），跨 topic 无序，两个 inbox 各去各的重——同一批 epoch
变化可能起两个子图（CPG-1）。

第二处是四个声明的消费方里有三个在别的方案里根本不存在：S47 §2.1 的 10 条订阅没有它，S44
§6.3 的 9 条消费没有它，而 S45 **架构上不能有它**——S45 §1 行 160 写「**S45 不反向依赖这些
服务**」，它只消费调用方写进**自己** outbox 的 `audit.record.intent`（§3.1 行 184），若改订
S07 的领域事件就变成主动去拉业务事实，与它全部论证冲突。剩下那条「S07 自己（其他实例）」
也与它 §3.1 行 394 的本地 supersede 判定、§5.6 行 910 的 manifest 订阅两处重叠（CPG-2）。
按四份方案今天的事实，这个事件的有效消费者是**零**——但它和 S09 的 LNI-1 不同：那边是两份
方案都点名一个错的消费者，这边是 S07 单方面宣布四个，而三方从未同意。

第三处：S47 被指派的是「修改历史面板」（行 899），而面板要显示的「改了什么」在 `patches` 里
（§2.2 的 `/camera/angle`、`before_value`/`after_value`），事件**一个补丁都没搬**，只搬了
`impact` 六键中的四个。`regenerate_node_ids` 是后果不是改动——从中反推「改了镜头角度还是改了
时长」不可能。`derived_adjustments` 也不在，而 §2.2 行 206 明写 UI 必须区分「这条不是你要求的、
是推导出来的」。同理 `plan_hash` 是 `sha256(JCS(整份 plan))`（§1.3 行 84），事件只带它的碎片，
**没有任何消费方能复算或验证它**——若日后要给 S45 做审计，记录里有 `plan_hash` 而没有 plan，
事后无法证明「当时批准的是哪份补丁」（CPG-3、CPG-4）。

剩下四处：`memory_checkpoint_after` **没有来源**——`change_plan` 只有一列 checkpoint（行 125），
S08 的 `:commit` 响应里没有它（§4.4 行 615–630），S16 也不在 S07 的 `depends_on` 里
（`[S08,S09,S18,S02]`）；而示例 27→29 跳了 2，说明一次 apply 连推两次 canon，那层因果链在任何
契约里都不存在——照示例填就是猜的（CPG-5）。两个 checkpoint 都只有两键，而 DDL 注释（行 125）
与 `execution-context.v2` 都定义成三键且 `additionalProperties: false`，丢的是 `branch_id`——
而本事件再没有别处带分支，`fence` 与 manifest 又都是分支作用域的，这是 S08 的 SMC-6 同一缺口的
第二次出现（CPG-6）。`rebased` 与 `previous_revision` 都不在 payload，于是「线性推进」与「落在
别人 head 上」在事件里同形，`new.revision - 1` 这个消费方必然会做的假设在 rebase 时是错的
（CPG-8）。最后 `saga_id` 在 DDL 可空（行 134，没有 `cp_applied_has_saga`）、在事件必填——
「`APPLIED` 时非空」是读状态机读出来的推断，与 LNI-5 同类（CPG-7）。

### `access.grant-changed.v2.schema.json`：形状最扎实的一份，坏的全在「一个事件背四种语义」

S01 只发这一个事件（§4.4 行 564），而它有八个消费方。14 个字段里三个闭集（`state`、
`effect`、`scope_kind`）**每一个都有 DDL 的 `CHECK` 撑腰**（§5.6 行 752–760），是本批契约里
形状最牢的一份——所以这次的问题不在「字段从哪来」，而在**这个形状同时承担了四种撤销**：
grant 级 `:revoke`、principal 级 `:offboard`、IdP 组 diff、租户级 `:suspend`（§3.4 行 305–310）。
四种语义共用一份 payload，于是每种都缺一格填不进去的字段，而消费方也分不出自己收到的是哪一种。

最硬的一条是 **AGC-1**：离职广播按 §3.4 行 308 要让「该主体全部 grant 失效」，写成一个事务就是
N 行 `role_grant` 各 `+1`、各写一行 outbox；因为 `aggregate_id = grant_id` 且 partition key 是
`tenant_id:aggregate_id`（§5.9 行 941），这 N 条事件落在 **N 个不同分区**，跨分区无序，而
`at_least_once` 还会重放。行 561 的设计意图是「任意一条相关事件到达就更新 epoch」，可**合并规则
（epoch 取 max）全仓库没有写过一处**；按到达顺序覆盖的实现在乱序时会把 epoch 写回小值，而
§3.2 第 5 道检查是 `TCT epoch ≥ 本地缓存`——本地被写小之后，一条**已被撤销**的 TCT 重新通过。
这是 fail-open，不是「慢」。（C8 只承认了「从未有 grant 的租户挂起不发事件」这一条代价，
没承认「从未有 grant 的 principal 离职」同样零事件，而广播的前提正是有 grant 可广播。）

**AGC-2** 是同类：S19 §5.5 行 715 说「拿 `access.grant.changed` 的 grant revision 与这个
epoch 比对」，可 `grant_revision` 是单条 grant 内 1..3 的版本号，`tenant_revocation_epoch` 是
租户级 812 级别的计数——两套量纲，任何比较都无意义，而 E39 的「操作链可证明」压在这上面。
**AGC-3**：事件的 `changed_by` 只有两键，DDL 的 `granted_by`/`revoked_by` 是四键，丢掉的
`source` 恰好是 §3.1 行 253 那条不变量的判据（「手工授予的 grant 绝不被登录路径改动」的条件
就是 `granted_by.source = 'idp_group'`），而 S45 §1 行 160 明写它不反向依赖这些服务，回源的路
对审计是关着的。**AGC-6**：挂起与终止都 bump `tenant.revocation_epoch`，事件里**没有任何字段
携带 `tenant.lifecycle`**，于是 S23 §6.2 行 1188 的「撤销租户 → 释放该租户全部 RESERVED」会把
一次**挂起**当成终止——而 §3.4 行 316 恰恰要求挂起期间运营人员能查账。

**AGC-4** 是同一形态的第四次（前三次：S08 SMC-4、S09 MSC-3、S10 ABS-5）：示例画的是 revision 2，
而 §4.4 行 560 明写序列是 `1(granted) → 2(revoked)`——**revision 1 就是创建**，没有前态可填
`previous_state`，而本契约把它收成必填。前三次的后果是「某个生命周期阶段不可表达」，这次更重：
新建 grant 是 `POST /access-grants` 与 JIT 首次登录两条**日常热路径**，不是边界情形。

**AGC-5**：`reason` 是自由文本，对应 DDL 列却叫 `revoke_reason`（名字不同、无 `CHECK`、无注释），
而同方案的 `reason_code` 是另一套大写下划线闭集——三套形态并存。更实质的是它允许人写字，而
catalog 对这个 topic 声明 `pii_payload: references_only_by_default`：声明说只带引用，字段却在
带散文，14 天保留期内所有订阅方都能读到。

**AGC-7** 收四处命名漂移，其中两处是空操作级的：S23 §6.3 行 1206 的 `grant.revision` 在本事件里
不存在（payload 是扁平 `grant_revision`，没有 `grant` 子对象），按字面取值得到 `undefined`，
「旧版本丢弃」于是在任何语言里都恒真或恒假；S22 的 T22-50 前提里那个「池定义版本」也不在本事件里
——这条 `[E18]` 用例因此**不验证任何东西**。另外 §4.4 示例的 `source: "s01.tenant-access"`、
`home_cell: "cell-a1"`（32/32 其余示例是 `cell_a1`）、`.000Z` 毫秒时间（30/30 其余是秒精度）
三处都在 envelope 的裸 `minLength: 1` 下无法被拦。**C10（行 1310）要求写到
`contracts/events/access-grant-changed.v2.schema.json`，而那个子目录不存在**，本仓库 31 份
payload schema 全部平铺在 `contracts/` 下且用点号分隔——本文件按既有约定取名，这条偏离记在
AGC-7 里，因为文档指定了一处与全仓库不一致的位置，按 AGENTS.md 应当报告而非默默选边。

13 条负向探针复核了这个 schema 的实际约束面：`previous_state` 缺席被拒、四键 `changed_by` 被拒、
`tenant_lifecycle` 无法外挂、`grant.revision` 路径被拒、`scope_kind: "episode"` 被拒（DDL 上界是
`series`）、`role_revision` 无法外挂；而自由文本 `reason`、deny-撤销与 allow-撤销同形、
`grant_revision: 1` 三者都被接受——正是上面几条缺口的形状。

`tools/validate/validate_examples.py` 会读这个键：示例的 `type` 命中某个 topic，就先按
envelope 校验整条事件，再按 `data_schema_ref` 校验 `data` 子树。上表每个 topic 在
`contracts/examples/` 都有对应示例（`artifact-ingested.json`、`production-image-ready.json`……），
`SCHEMA_OF` 把它们的 stem 映射到 envelope，payload 子树由这个键自动接上。

### `artifact-ingested.v2.schema.json`：字段全部有出处，缺口单独标

字段名一律用 S28 的名字，不用消费方的别名。**已知缺口写在文件里的 `x-needs-owner`
和 `x-conflicts` 两个扩展键里**，不走散文：

| 消费方要的 | S28 有吗 | 结论 |
|---|---|---|
| `codec` | 只有散文「探测产出……编码」，无字段名、无列 | `NEEDS_OWNER` |
| `frame_count` | 无（「帧数上限」是炸弹防护限额，不是元数据） | `NEEDS_OWNER` |
| `timecode_start` | 无 | `NEEDS_OWNER` |
| `ingesting_service` | 无，**且 §6.1 请求体根本没有请求方字段** | `NEEDS_OWNER`，而且是 S28 的输入缺口 |

`duration_ms` 允许 0 与 NULL（S28 DDL）、四家消费方要求 `> 0 NOT NULL`——这是**真冲突**，
写进 `x-conflicts`，不在 schema 里选边：schema 按权威（S28）的取值域写，消费方的
「阻塞 + 告警」是防御而非契约。

## quality/：修复路由的两份数据 + 一个加载期校验器

`plans/S39-repair-planning.md` 的路由不是按 `check_id` 决策的，也不是只按 `type` 决策的。
两样东西分工，谁都不越界：

| 文件 | 管什么 | 改动成本 |
|---|---|---|
| `quality/action-families.v2.json` | 14 个 `type` 闭集各自**允许**哪几种动作、兜底是什么 | 改 = 全体 QC 服务与 S39 重新部署 |
| `quality/check-registry.v2.schema.json` | 单条 check 登记的字段形状：起点动作、窗口、升级条件 | 新增 check = 只改数据，S39 不重新部署 |
| `quality/qc-report.v2.schema.json` | QCReport v2 的完整 payload（从 `plans/S35` §2.3 提取，不手抄） | contracts 变更 |
| `quality/validate_registry.py` | 跨文件约束的参考实现：登记项不得越出所属动作族 | 随规则变 |

S39 的解析是三步交集，不是查表：

```text
candidates = families[defect.type].chain
           ∩ registry[defect.check_id].allowed_actions
           ∩ policy.allowed_actions
起点 = registry.default_action   升级 = registry.escalation_rule
窗口 = registry.window_scope     空集 → human_review, reason=no_capable_route
```

`human_review` 是每个族的公共终点，**不出现在任何 chain 里** —— 否则「没有可达的自动动作」
和「人主动选择人审」会被混成同一件事。

```bash
python contracts/quality/validate_registry.py <registry.json>
```

退出码 0 = 可加载，1 = 拒绝（**不是**记警告后继续），2 = 环境问题。
族校验必须是加载期自动执行的：只写在散文里的规则，第一次有人赶着加 check 就会被绕过。

**`jsonschema` 缺失时返回 2，不返回 0。** 校验器原先在没有 `jsonschema` 的环境里
打印一句"shape checks skipped"就照常返回 0——形状校验整层不跑，调用方却看到"通过"。
半套校验器比没有更糟：它会给出一个它没验过的绿灯。现在缺依赖直接 rc=2，
在 CI 里就是失败而不是静默降级。注意 `python -I`（隔离模式）不加载用户
`site-packages`，装了 `jsonschema` 也会落到这条路——拿它做冒烟测试要记住这点。

冒烟测过的 11 条（全部按预期退出）：族内合法的 registry → 0；缺 `window_max_frames`
（`window_scope=window`）、缺 `recheck_scope`、`window_scope` 非枚举值、`schema_version`
写成 `"2"`、`default_action` 越族、`allowed_actions` 越族、`owner_service` 与前缀不符、
`check_id` 重复、`never_auto_repair` 无理由、`severity_default` 高于 `severity_max` → 全 1；
`--families` / `--schema` 路径不存在 → 2。`owner_service` 那条要挑**真的不符**的项改，
把 `S36` 的项改成 `S36` 不构成用例。

### `keyframe_edit` 的 6 帧前置条件是加载期规则（ROUTE-T4）

`plans/S35:1010` 写的是「给 `window_scope=shot` 的 check 登记 `keyframe_edit` → 加载期拒绝」，
同一条件在三处各说了一遍：`check-registry.v2.schema.json:90`（`window_max_frames`
"Gates keyframe_edit, whose precondition is <= 6"）、`action-families.v2.json:68`
（`identity` 把 `keyframe_edit` 列进 `forbidden`，"when is continuous over more than 6 frames"）、
`plans/S29:109-122`（执行期 `end_frame - start_frame + 1 ≤ 6`，否则 422）。
但在补上这条之前，校验器只认「动作不得越族」，于是

```json
{"defect_type": "identity", "default_action": "keyframe_edit", "window_scope": "shot"}
{"defect_type": "identity", "default_action": "keyframe_edit",
 "window_scope": "window", "window_max_frames": 30}
```

两种都返回 **0「OK: 1 check(s) loaded」**。schema 表达不了它——这条规则把动作成员资格和
两个兄弟字段（`window_scope`、`window_max_frames`）关联起来，是跨字段的；S29 只在执行期挡，
意味着错误配置能一路装到线上，直到第一次真修才 422。

现在 `validate()` 里有一条 `KEYFRAME_MAX_FRAMES = 6` 的闸：凡是 `default_action`、
`allowed_actions`、`escalation_rule.escalate_to` 任一提到 `keyframe_edit` 的登记项，
`window_scope` 必须是 `frame` / `window`，且 `window_max_frames ≤ 6`。四件事一起判，
因为只查 `default_action` 会放过 `escalation_rule` 升级到 `keyframe_edit` 的路径。

回归矩阵（`window_scope` / `window_max_frames` / 期望退出码）：
`shot` / — → 1；`window` / 30 → 1；`window` / 6 → 0；`frame` / 1 → 0；
不含 `keyframe_edit` 的 `identity` + `shot` → 0；`escalate_to: keyframe_edit` + `shot` → 1；
`escalate_to: regenerate_video` + `shot` → 0。

## 三层文件的关系

```
service-catalog.v2.json  ──生成──▶  services/<slug>/service.yaml
        │
        └──引用──▶ event-topics.v2.json ──schema_ref──▶ event-envelope.v2.schema.json
                                        └──data_schema_ref──▶ 31 个 payload schema
                                                              （见上表；其余 24 个 envelope-only）
```

改服务元数据请改 catalog，然后重新生成 `service.yaml`，不要手改生成物。

## 一个入口跑完所有仓库级检查

```bash
python tools/check_all.py
python tools/check_all.py --list
python tools/check_all.py --only links,examples
```

九项检查：`json`（100 个 JSON 解析）、`payloads`（86 条 `schema_ref` / `data_schema_ref`
都指向真实文件，其中 31 个 topic 带 payload schema）、`checkids`（四份 QC 方案里 99 个
`check_id` 是否符合 schema 的三段命名）、`links`（1321 条相对链接全部可达）、`catalog`
（服务 id / topic 生产消费方 / `docs/02` 三方一致）、`families`（动作族文件自己声明的
5 条 invariant）、`scaffold`（120 个生成物与 catalog 一致）、`examples`（47 个示例对
schema 校验）、`registry`（`check-registry.v2.json` 存在时才跑）。

`payloads` 是后加的，因为它抓的错比 `json` 更晚才暴露：一个 topic 指向不存在的 schema
文件时，消费方看到的是「有类型契约」，而校验器在读取时才会炸——失败落在别人服务里。

退出码 0 = 全过，1 = 有真问题，2 = 某项没跑成。**后两者要分开**：一个没跑成的检查
不能读成绿灯，这和上面 `jsonschema` 缺失返回 2 是同一条原则。

写这个入口之前，这些检查是几条各自为政的命令，没人一起跑，也就等于「族校验必须在加载期
自动执行」这句话没有执行入口——校验器只有在被调用时才约束任何东西。
`registry` 这一项现在报「还没有文件」而不是失败，因为 99 条 check 的字段确实还没补齐
（缺口 4）；文件一落地，加载期校验立刻生效，不需要再接线。

`checkids` 里三条 `TECH.*_FRAME` 走白名单放行——改名要动 `E24-T4/T5` 验收项，是 S35
owner 的决定，不是我能在检查里替它选的。白名单**双向**校验：新冒出来的不合规 id 会报，
白名单里的 id 一旦不再出现也会报（说明改名已落地，条目该删）。单向的抑制清单会慢慢变成
一个没人敢动的黑名单。

两处实现细节容易踩：

- **生成物比对不是覆盖写。** 生成器总是写盘，所以检查把 `ROOT` 重定向到临时目录渲染，
  再逐个比对。这样它可以在别的会话正在写 `services/` 时安全运行。已验证注入一行漂移
  会报 FAIL、还原后干净。
- **比对前必须归一化换行。** 仓库是 `core.autocrlf=true` 且没有 `.gitattributes`，
  生成器写 `\n`，检出回来是 `\r\n`——直接比字节会让每一个生成物在任何一次新检出上
  都报漂移。已验证：只把某个文件转成 CRLF 不会报，再加一行内容会报。

## 设计时留下的缺口

实现前需要先补齐，否则代码生成或归属会出问题：

1. ~~**OpenAPI 21 个操作没有 `x-owner` 扩展字段**~~ —— **这条不成立，已撤销。**
   归属字段叫 **`x-service-id`**，21 个操作 21 个全带，取值全部命中
   `service-catalog.v2.json` 的 `service_id`，工具可以直接按它自动归属。
   另有 5 处 `x-access-boundary: internal_or_explicit_service_scope` 标不该对客户端直开的内部边界。
   原先写「所有权只存在于散文里」是因为按 `x-owner` 这个名字去找，字段名不对不代表字段没有。
   见 [contracts-gap-list-corrections-2026-11-06.md](../delivery/acceptance/contracts-gap-list-corrections-2026-11-06.md)。

2. **MCP 工具与 `docs/13` 不是同一份清单。** 12 个工具都带 `required_scope`，五档里
   `read` / `plan` / `execute` / `review` / `admin` 各有实例——**但**：
   - `drama.delivery.publish` 用 `publish`，不在五档闭集内（第六个值）；
   - 工具集合对不上：`docs/13` 具名 13 个，JSON 12 个，交集只有 8 个。
     `docs/13` 有而 JSON 无：`artifact.compare` / `episode.plan` / `memory.extract` /
     `model.estimate` / `project.get`；JSON 有而 `docs/13` 无：`delivery.prepare` /
     `delivery.publish` / `policy.get` / `run.command`；
   - `model.estimate` / `episode.plan` / `memory.extract` 在 `docs/13` 里承担「要 `plan:compute`
     预算」的角色，JSON 无对应工具——**能力清单差异，不是措辞差异**。

   补/删工具、改 scope 都属于授权决策，标 `NEEDS_OWNER`，不从任何一侧推断。
   原文「没有 scope 字段……只有 annotations 和 input schema」是错的。
3. **`examples/` 里所有 hash 和金额都是占位符**（`aaaa…`、`bbbb…`）—— 结构示意，不能直接当测试 fixture。
   **但形状是全的**：47 个示例全部通过各自 schema 校验（`capacity-plan.json` 没有对应 schema，
   是纯结构示意）。用 `python tools/validate/validate_examples.py` 复核。

   注意这份校验**必须先建本地注册表**。schema 之间用 `.invalid` 的 `$id` 互相引用
   （今天只有 `execution-context.v2.schema.json` 被引用 3 次），`jsonschema` 遇到未知
   `$id` 会去联网拉，而那个域名不存在——于是直接在第一个跨文件引用处崩掉，而不是报告
   示例本身的问题。设计包写着「解析使用本地注册表；不允许联网拉取未知 schema」，
   仓库里此前没有这个注册表，`tools/validate/validate_examples.py` 就是它。
4. **`quality/check-registry.v2.json` 数据文件还没有，且方案里的表不足以生成它。**
   schema 与校验器已在，但 99 条 check（TECH 23 / VIS 32 / AUD 20 / SUB 24）只存在于四份
   QC 方案的表里，缺的不是"抄一遍"，是 schema 的必填字段本来就没写：

   | 缺什么 | 范围 | 说明 |
   |---|---|---|
   | `window_scope` | **99/99 条** | 四份表都没有这一列。它决定 `keyframe_edit` 是否可用，是路由的必要输入 |
   | `escalation_rule` | **99/99 条** | 只有散文描述，没有 `metric` / `comparator` / `threshold` / `escalate_to` |
   | `default_action`、`allowed_actions` | **76 条**（S36/S37/S38） | 这三份表只有 4–5 列：`check_id` / `type` / 严重度 / 判定 / 依据 |
   | `recheck_scope` | **76 条** | 同上 |
   | 枚举外 / 不可直接提取 | S35 中 2 处动作、10 处复检范围；四表合计 8 处带条件严重度（S35 4 / S36 1 / S37 1 / S38 2）、1 处二值、3 处为空 | 动作列 `remux→transcode`（`TECH.AUDIO.OFFSET`）、`—`（`TECH.META.MISSING`）；复检列 `全片抽检`、`连续窗口 ±0.5s`、`全轨`；严重度 `major（master 时 blocker）` |

   **不做的事**：不从散文推断这些值。给 76 条 check 凭空补 `window_scope` 和
   `default_action`，等于把设计决策伪装成数据提取 —— 尤其 `window_scope` 直接决定
   `keyframe_edit` 能不能用，猜错会让 S39 把一个 20 帧的连续窗口交给逐帧修补。

   正确的下一步是四个 QC 服务的负责人按 §2.2.2 的字段表把登记项补全，再用
   `validate_registry.py` 把关。在那之前，S39 的族约束有代码守住，**check 级的窗口与
   起点动作仍只靠人工对照**。

   逐条清单见 `delivery/acceptance/check-registry-missing-fields-2026-11-05.md`：
   99 行带行号，写明每条缺哪个字段、方案里有没有出处、没出处就标 `NEEDS_OWNER`。
   **它不给值**，只把"人工对照"变成有 owner 的待办。

   **数这四张表要注意转义管道符。** `plans/S35:765` 的判定列写
   `|actual − ShotSpec.duration| > 1 帧`，绝对值符号是裸 `|`；朴素 `split('|')`
   会把这行切成 9 列，动作列与复检列整体右移。须用 `(?<!\\)\|` 切分。
   本表此前的"3 处动作 / 11 处复检"就是切错列数出来的，实为 **2 处 / 10 处**。

## schema 管不到什么

Schema 能证明结构合法，**证明不了**：

- 时间区间的先后关系
- 引用是否真实存在
- 金额守恒
- 状态转移是否合法
- 策略是否被降级

这些只能靠测试和分布式验证。**schema 合法 ≠ 行为正确。**

## v1 为什么还在

`domain-v1/` 只作为 payload 细化基线（ShotSpec / GenerationRequest / QCReport / ChangePlan / EventEnvelope v1）。v2 显式补了上下文与权威边界，但字段定义仍然有参考价值。
