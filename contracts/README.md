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
  event-topics.v2.json         55 个 topic，注册 55 个 event_type
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

**这个键今天 55 个 topic 全都带**（envelope-only 已归零）。此前没带的不全是遗漏：payload
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
| `post.mix.completed` | `post.mix-completed.v2.schema.json` | S42 §6.3 / §4.4 |
| `post.render.failed` | `post.render-failed.v2.schema.json` | S42 §6.3 / §4.6 |
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
| `asset.voice.authorization.changed` | `asset.voice-authorization-changed.v2.schema.json` | S15 §5.3 / §10 C15-2 |
| `model.capability.changed` | `model.capability-changed.v2.schema.json` | S24 §3.5 |
| `model.route.selected` | `model.route-selected.v2.schema.json` | S25 §7.1 |
| `change.plan.applied` | `change.plan-applied.v2.schema.json` | S07 §5.5 |
| `storyboard.shot.revision.created` | `storyboard.shot-revision-created.v2.schema.json` | S06 §4.3 / §11 C3 |
| `breakdown.revision.created` | `breakdown.revision-created.v2.schema.json` | S05 §4.2 / §11 C3 |
| `adaptation.training.completed` | `adaptation.training-completed.v2.schema.json` | S48 §4.3 / §11 C48-4 |
| `project.episode.created` | `project.episode-created.v2.schema.json` | S03 §5.3 |
| `audit.export.ready` | `audit.export-ready.v2.schema.json` | S45 §4.3 |
| `projection.cursor.advanced` | `projection.cursor-advanced.v2.schema.json` | S47 §4.4 |
| `integration.command.accepted` | `integration.command-accepted.v2.schema.json` | S46 §4.5 |
| `narrative.script.revision.created` | `narrative.script-revision-created.v2.schema.json` | S04 §5.3 |
| `asset.scene.revision.accepted` | `asset.scene-revision-accepted.v2.schema.json` | S13 §5.3 |
| `asset.prop.revision.accepted` | `asset.prop-revision-accepted.v2.schema.json` | S14 §5.3 |
| `asset.character.revision.accepted` | `asset.character-revision-accepted.v2.schema.json` | S11 §5.3 |
| `asset.costume.revision.accepted` | `asset.costume-revision-accepted.v2.schema.json` | S12 §5.3 |
| `policy.revision.published` | `policy.revision-published.v2.schema.json` | S02 §10.2 行 718 |
| `delivery.package.created` | `delivery.package-created.v2.schema.json` | S43 §5 表 `package` |
| `delivery.package.sealed` | `delivery.package-sealed.v2.schema.json` | S43 §5 表 `package` + `manifest` |
| `delivery.publish.failed` | `delivery.publish-failed.v2.schema.json` | S43 §5 表 `distribution_attempt` |
| `analytics.metric.snapshot.created` | `analytics.metric-snapshot-created.v2.schema.json` | S44 §5 表 `metric_snapshot` |
| `analytics.evaluation.completed` | `analytics.evaluation-completed.v2.schema.json` | S44 §5 表 `evaluation_run` |
| `analytics.cost.projection.updated` | `analytics.cost-projection-updated.v2.schema.json` | S44 §5 表 `cost_projection` |

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
写了字段**。S42 三条里只有 `post.master.rendered` 在计划里写了字段清单，另外两条后来由本仓
补成了 `post.mix-completed.v2` / `post.render-failed.v2`（形状来自 §4.4、§4.6 与 §5 的 DDL，
不是从 `post.master.rendered` 裁出来的）；S43 四条原本只有 `delivery.package.published` 有
payload schema，其余三条同批补齐（`delivery.package-created.v2` / `delivery.package-sealed.v2` /
`delivery.publish-failed.v2`，形状来自 §5 的 11 张表而不是从 `published` 裁）；S41 一条事件
有字段清单但消费方（S42）把它里面的字段叫成另一个名字。
`schema` 覆盖不到的那几条一律在 `x-needs-owner` 里点名而不猜——它们的名字已经在
`event-topics.v2.json` 里登记过了，缺的不是登记，是形状。

**envelope-only 已经归零**：S43 剩下三条（`delivery.package.created` / `delivery.package.sealed` /
`delivery.publish.failed`）与 S44 三条（`analytics.metric.snapshot.created` /
`analytics.evaluation.completed` / `analytics.cost.projection.updated`）原本只登记了名字与聚合键，
本批把它们补成 payload schema。六份的形状全部取自各自计划的 §5 DDL，不是从信封或同类事件裁的；
挡路的歧义都写进各自的 `x-needs-owner` / `x-conflicts`（PDC / PDS / PDF / AMS / AEC / ACP），**没有一条
是把散文里的建议值改写成 `enum` 得来的**。

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

### `asset.voice-authorization-changed.v2.schema.json`：唯一一条控制「停止花钱」的事件，而它发不出去

S15 只发布一个契约事件（§5.3 行 742「`service.yaml` 明写」），它是「撤销音色授权」对外
的全部信息：`to_state` 一落，S26 必须立刻停止为该音色提交付费任务（E30-C3）。9 个
payload 字段的形状都清楚，四个状态值也是真的闭集——但这条事件在最常见的两个时刻
（建音色、第二次授权变更）**都发不出去**。

**VAC-1** 是全批最硬的一条：`aggregate_sequence` 在方案里有两个定义。§5.3 行 744 说
`= state_seq`，§2.8 行 441 说 `= revision`（内容 revision）。按后者：(1) 建音色事务写
`voice.current_revision` 默认 0（行 175）而同事务的授权事件 `seq=1`，envelope 的
`aggregate_sequence` 是 `minimum: 1`——**每个新建音色的授权事件都被 schema 拒**，卡在
outbox；(2) 一个音色 `granted→suspended→granted→revoked` 三次授权变更而内容 revision
一直是 3，三次事件的 `(aggregate_id, aggregate_sequence, event_type)` 全等，outbox 的
`UNIQUE`（行 429）在第二次就抛错——而它与 `UPDATE voice_authorization` 在**同一事务**
（§4.3 行 610–615），于是 **suspend 和 revoke 整体回滚**。按 `= state_seq` 两个失败都
消失。一个方向让撤销失败在 DB 层，另一个方向只让一句注释错——但这是方案内部的两句话
互相矛盾，按 AGENTS.md 只报告不选边。

**VAC-2** 同样在热路径上：`authorization_event.reason_code` 是 `text NOT NULL`（行 281），
而建音色的命令体（§4.1 行 529–535）**没有这个参数**，§3.1 的创建转移（行 482）也不要求它。
`POST /voices` 的事务第 3 步因此必然违反 NOT NULL。有意思的是**同一个问题在 `from_state`
上已被承认过**：DDL 行 279 给它可空，于是创建事件是 `from_state: null` + `to_state: granted`
——本文件照 DDL 收 `null`，这也是本批第一份**不由 schema 制造、而由 DDL 自己解决**的首版
表达问题（对照 SMC-4 / MSC-3 / ABS-5 / AGC-4 那四处的同一形态）。只是没人把同样的处理
应用到 `reason_code` 上。

**VAC-4 / VAC-5** 是同一件事的两个面：9 个 payload 字段里有 3 个（`effective_at`、
`authorized_uses_before`、`expires_at`）**在 `authorization_event` 表里根本没有列**，
只能从 `voice_authorization` 的当前行快照。后果三个：(1) 按 `authorization_event` 重建历史
时它们全丢失，而那张表存在的理由就是审计（行 272），§4.3 行 642 还明说 S15「记录」了
`effective_at`——记录在哪个表？（2) `effective_at` 可以是**未来**且含义是「仅审计」，撤销
是**立即**生效；一个按名字推断的实现会写「`now < effective_at` 还能用」，那正是方案特意
排除的宽限期，在合规撤销上就是多用三十天。(3) 状态机的五条转移里**没有一条**修改
`authorized_uses`（唯一的写入点在建音色，行 534），所以 `authorized_uses_before` **恒等于
当前值**——`_before` 描述一个不存在的差量，而真正需要「变更前用途」的 `renew`/`resume`
恰好不需要它。

**VAC-6** 记一处形状例外：`evidence_ref` 三键、**没有 `content_hash`**，是全仓 91 处带
`service_id` 的引用里唯一的例外（envelope 的 `data_ref` 把 `content_hash` 列为 required）。
而同一张表的 `rights_ref` 描述**同一份权利文档**却是四键带 `rights_hash`（行 229）——两套
键名、一个有 hash 一个没有。S45 不允许回查（S45 §1 行 160），所以 E30-C6 要的「依据」在
审计手上只是 id 加数字。示例把 `service_id` 写死 `S02`，而 C15-5（§10 行 995）报告 S02 方案
把 `rights_current` 的来处写成 `S10:binding_set@r12`——两人的说法冲突未裁决。

**VAC-7** 是 S01 的 AGC-3 的第二例：payload **没有 `actor`**，而 `authorization_event.actor`
是 `jsonb NOT NULL`（行 284），E30-C6 明写「`authorization_event` 有 `evidence_ref`、`actor`、
`reason_code`」——三项里唯一没进事件的就是「谁」。S15 自己知道是谁（列 NOT NULL），只是
没发出来，而审计方（S45）不能回查。

**VAC-3 / VAC-8** 收剩下的语义与命名漂移：`to_state` 的四值 `CHECK` 在**驱动事件的那张表上
不存在**（`authorization_event.to_state` 是裸 `text`，带 CHECK 的是 `voice_authorization.state`），
所以 DB 只在更靠后的那张表上拦；`revoked` 是单向终态（触发器兜底，只能新建 `voice_id`），
与 S01 那种「状态可反复覆盖」相反，消费方必须按 `state_seq` 取最大而不是按到达序覆盖；
partition key 正文写 `tenant_id:voice_id` 而 catalog 写 `tenant_id:aggregate_id`（ABS-6 第二例）；
`voice_id` 在 DDL 注释里是 `vox_01J...`、在示例里是 `vox_linlan`；C15-2 报告**没有**
revision accepted 事件（S10 检索投影与 S17 音色缓存只能轮询）、C15-4 报告映射变更只有本地
事件（S24/S25 不知道映射下架）。

14 条负向探针复核了实际约束面：`to_state` 缺席/第五值被拒、`state_seq: 0` 被拒、
`evidence_ref` 带 `content_hash` 被拒（三键 `additionalProperties: false`）、
`authorized_uses_before: []` 被拒、外挂 `actor` 被拒；而 `from_state: null`、`evidence_ref: null`、
`expires_at: null`、`effective_at: null` 四者都被接受——正是上面那几条 DDL 语义的形状。

`tools/validate/validate_examples.py` 会读这个键：示例的 `type` 命中某个 topic，就先按
envelope 校验整条事件，再按 `data_schema_ref` 校验 `data` 子树。上表每个 topic 在
`contracts/examples/` 都有对应示例（`artifact-ingested.json`、`production-image-ready.json`……），
`SCHEMA_OF` 把它们的 stem 映射到 envelope，payload 子树由这个键自动接上。

### `model.capability-changed.v2.schema.json`：一条事件承载五件事，其中三件的实体不在信封里

S24 的 `change_kind` 有五个取值，而事件的 `aggregate_id` 只指能力快照：

| `change_kind` | 真正变了的东西 | 在 payload 里有吗 |
|---|---|---|
| `snapshot_published` | `model_capability_snapshot` 的一行 | 有（`snapshot_id`） |
| `lifecycle_changed` | 同一行的 `lifecycle` 列 | 有（但只是新值，旧值不在） |
| `combination_probe_added` | `mode_combination_probe` 的一行 | 只有 `changed_modes`，没有 `combination_key` |
| `price_published` | `price_snapshot` 的一行 | **没有** `price_snapshot_id` |
| `pool_health_changed` | `provider_account_pool` 的一行 | **没有** `pool_id`，也没有 `pool_health` |

后三行是**结构缺口**，不是精度问题：S25 §1 行 24 声明「只消费本事件建投影」，而它的投影
主键 `candidate_key`（S25 §3.2 行 503）是 `{provider}/{channel}/{model_id}@{model_snapshot}#{region}#{account_pool_id}`。
本事件的 8 个字段里，`provider`/`channel`/`model_id`/`model_snapshot` 只能从 `aggregate_id`
按 `/` 切出来，**`region` 与 `account_pool_id` 一个都没有**——而 §2.1 行 96 专门把池拆成独立表，
理由正是「不能把一份池健康态复制进 30 个快照」。事件于是同时踩了两头：需要按池发，却不让带池 id。

三处与 DDL 的直接对撞：

- **`lifecycle` 是这里唯一取值有依据的字段**（§2.2 行 125 真 CHECK），对照 S15 的 VAC-3（那边 CHECK 装在
  不驱动事件的那张表上）。但 §3.5 行 614 的示例写 `active`，而 `snapshot_published` 的唯一来源
  `POST /capability-snapshots` 只可能写出 `validating`（§3.1 行 414「不接受直接写 active」，行 468 的 201
  响应就是 `validating`）。示例是两条事件合成的一条。
- **§7 行 779 说能力快照「不可变」，§4.2 行 653 的扫描器对它原地 `UPDATE`**。于是
  `snapshot_id`（§2.2 行 104，`= sha256(claim 集)`）在多条事件里重复出现、每条带不同的
  `lifecycle`——它标识的是「内容 + 此刻状态」，不是「内容」。
- **`capability_bundle_revision` 是全局计数器**（§6.1 行 740）同时充当 `aggregate_sequence`
  （§3.5 行 596）。于是同一 `aggregate_id` 的事件序号稀疏（42、57、63…）。outbox 的
  `UNIQUE (aggregate_id, aggregate_seq, event_type)`（§2.7 行 389）因此永不冲突——**S15 的 VAC-1
  在这里不可能发生**，这是全局计数器的净收益。代价在 S25 §6.2 行 600：`WHERE revision < EXCLUDED.revision`
  按全局水位丢弃，一条晚到的 rev 42 会被已写入的 rev 43 吃掉，而 `projection_lag` 按水位算是 `false`。

`changed_modes` 在五个 `change_kind` 下分别是「若干 / 恰好一个 / 零 / 零 / 零」，而三种零含义不同
（确实没变 / 不适用 / 实体不是这个快照）。本 schema 允许 `[]` 并只要求元素唯一——`capability_mode.mode`
是 `mode text NOT NULL`（§2.3 行 151）**没有 CHECK**，16 个模式只活在行 141–146 的 ```text``` 块里，
所以拼错的 mode 在数据库层合法、在 S25 的投影里会静默地不匹配任何需求。

`change_kind` 的五个值只出现在 §3.5 的一条 JSONC 注释里：`model_capability_snapshot` 没有这一列，
`outbox` 也没有，全仓库没有对应的 `CHECK`。本文件照 `contracts/README.md` 的原则收 `minLength: 1`
而不枚举，词表归属记在 `x-needs-owner`。

`tenant_id: "*"` 是本仓库唯一一个通配租户（其余 41 份带 `tenant_id` 的示例里 35 份是 `tnt_` 前缀、6 份是 `tenant_demo`）。它有据：
§2.1 行 46 与 §2.2 行 105 都写「共享目录行 `tenant_id = '*'`」。envelope 只要求 `minLength: 1`，
所以 schema 不拦也不推。另外 `subject` 在这里恒等于 `aggregate_id`，是全仓库唯一这么做的生产者。

### `model.route-selected.v2.schema.json`：一条决策事件，三种身份共用 `route_id`，而它唯一的回源指针缺一个键

形状本身是干净的：23 个必填字段、`additionalProperties: false`、没有一处值是从散文里猜的。
问题全在它与自己引用的权威之间。

| 事件说的 | 方案里的权威 | 差在哪 |
|---|---|---|
| `data_ref` = `{service_id, id, revision}` | envelope 的 `data_ref` 要求四个键，`additionalProperties: false` | §7.1 行 570–574 的示例**过不了本仓库自己的校验器**（MRT-1） |
| `selected.expected_cost_micro` = 1180000 | `selected.quote_upper_bound_micro` = 1080000 | 期望成本**大于**「上界」，而上界按 S24 §2.5 行 330 的公式是档位最坏值 × 1.15（MRT-2） |
| `revision` = `aggregate_sequence`，outbox `UNIQUE (aggregate_id, aggregate_seq)` | `route_decision` 主键是 `route_id`（行 404），不是 `(route_id, revision)` | 这一列实际只可能是 1，重新路由只能换一个新 `route_id`（MRT-3） |
| `projection_lag: false` | 「`bundle_revision` 比事件序列小」（§9 行 610） | 「事件序列」未定义，且无 DDL 列；S24 的 MCG-1 让它在最该为 true 时说 false（MRT-4） |
| `degradation_offered: ["pool","spec_adjust"]` | `degradation_option.kind` 的六值 `CHECK`（行 464） | 这是本文件唯一有真 DDL 依据的枚举；但 `auto_executable` / `silent_degradation` 不在事件里（MRT-8 的反面） |

`data_ref` 缺 `content_hash` 这条最要紧，因为 §7.1 行 562 的约束 1 明写「事件只带 `data_ref`；
需要明细的一方回源查」——`data_ref` 是这条事件唯一的回源指针，而 payload 里**本来就有**
`content_hash`（行 531，注释「与 `route_decision.content_hash` 一致，供下游核对」）。
正确修法是把同一个值也放进 `data_ref`，不是新建字段。本仓库的示例照此写了四键；方案原文的差异记在 MRT-1。

`expected_cost_micro > quote_upper_bound_micro` 不是笔误量级：两数相差 10 万微单位，约等于 `margin_bps`
默认 1500 的 15%。S25 §3 硬过滤 #16 用 `quote_upper_bound_micro` 与 S23 的剩余可承诺额比来判 `OVER_BUDGET`，
所以这个方向系统性地错会漏判超预算。本文件**没有**把两者绑成 `expected <= upper_bound`——
绑了就会拒掉照 §7.1 写的一切事件，而不绑它就静默通过。哪一侧是权威，记在 MRT-2 等 owner。

`revision` 这一列的设计是自洽的（一次决策一条记录，`aggregate_seq` 恒为 1，`UNIQUE` 只在重投时生效），
但它假设一条决策永不需要第二版。而 `quote_valid_to` 会让决策作废：S26 §12 第 2 条要求
`now() <= quote_valid_to`，否则 409 `ROUTE_QUOTE_EXPIRED` 并要求重新路由。重新路由产生**新的 `route_id`**，
所以消费方看到两条独立的流、不是同一条流的 rev 2。「这次尝试被重路由了几次」因此不可从事件流推出——
`command_id` 唯一（行 425）而 `attempt_id` 可复用，两条约束方向相反。本文件收 `minimum: 1` 而不锁死 1。

`normalized_input` 是这份契约里最该优先解决的一处：§12 行 681 把它变成 S26 的硬校验
（「提交给 S27 的输入规格必须等于 `normalized_input`，不能是原始请求值」），而它的字段集没有任何 schema。
示例只有 `duration_seconds` 与 `aspect_ratio` 两个键，而视频路径的 `GenerationCommand.input` 还有
`resolution`、`mode`、`references[]`、`audio_strategy`。本文件收 `type: object` 不约束内容，记在 MRT-9 与 `x-needs-owner`。

`home_cell` 在 §7.1 的示例里是 `cn-east-1`，是本仓库唯一一个非 `cell_a1` 的示例（其余 38 份带这个键的全是
`cell_a1`，envelope 只要求 `minLength: 1`；S06 §4.2/§4.3 又写了一处 `cell-a1`，与 S25 这处并列）。本仓库的示例用 `cell_a1`，与其余示例一致。

### `storyboard.shot-revision-created.v2.schema.json`：一条事件的聚合键漏了分支，唯一的消费者建不出主键

八个字段，形状都不难。问题同样不在形状，而在**它和它自己的 outbox、和它唯一的消费者之间的关系**：

| 权威说的 | 另一处权威说的 | 后果 |
|---|---|---|
| `aggregate_id = shot_id`（§4.3 行 575） | `storyboard.shot` 主键是 `(tenant_id, branch_id, shot_id)`（§5.4 行 686） | 分支分叉沿用 `shot_id`（§3.2.3、T06-09），两分支同号 revision 撞 outbox 的 `UNIQUE`（SRC-2） |
| payload 八个键，没有 `branch_id` | S09 `lineage_edge` 的 `branch_id text NOT NULL`，全部索引首维 | 唯一的消费者（S09 写 `SHOT_FIELD_DEPENDS`）建不出这一行（SRC-3） |
| `changed_fields: ["camera.angle"]`（§4.3 行 577、T06-20） | S09 `CHECK (field_path ~ '^(/[^/]*)+$')`，示例 `/beats/2/action` | 点号与 JSON Pointer 两种写法，转换规则无人定义（SRC-4、SRC-5） |
| `source_storyboard_ref` 两个键 | 同文件 `breakdown_ref` 三个键（带 `content_hash`，§4.2 行 556） | 引用无法核对是否读到同一版（SRC-8） |
| `content_hash: "sha256:..."` | `char(64)`（§5.5 行 710）、`^[0-9a-f]{64}$`（envelope 的 `data_ref`、S09 的 `edge_hash_shape`） | 照示例抄，写库与过信封各拒一次（SRC-4 的同一根因） |

**SRC-2 是这一批事件里唯一一个会让写入直接失败的**，不是不一致而是回滚：`outbox` 没有
`branch_id` 列，`UNIQUE (aggregate_id, aggregate_sequence, event_type)` 里也没有（§5 行 789–801），
而 `partition_key` 是 `tenant_id:aggregate_id`，同样不含分支。于是分支 A 与分支 B 的同一镜
各写一次 rev 6，就撞同一条唯一键；由于 outbox 与业务写在同一事务里，**撞了等于整笔单镜重写回滚**。
根因是聚合键少了一个维度——`shot` 的身份本来就是 `(branch_id, shot_id)`，事件只用了后半截。
修法两条互斥（把 `branch_id` 拼进 `aggregate_id`，或在 outbox 唯一键里补列），本文件不选边。

`changed_fields` 这一列还藏着两个 E03 才能暴露的洞：`beats` / `dialogue` 是数组，
`changed_fields` 无法寻址元素——`/beats/2/action` 这种写法在 S09 的边定义里出现过（行 74），
在 S06 的示例里从没出现过；而 `beats` 的元素**没有 id**（`shot-spec.v1` 的 `items` 只有
`time_range` 与 `action`），只能按序号寻址，插入一条就整体平移。另一个洞是增、删、重排
三类变更在「这个字段的值变了」这个表示法里无处安放，而 `shot_order` / `display_shot` 正是
revision 自己的列（§5.5 行 704–705）。

与 S25 那一条同一类：**§4.3 的示例也过不了本仓库的校验器**——缺 12 个信封键，其中包括
`data_ref`。而同一节行 584 那句「这条事件在 `event-topics.v2.json` 中不存在」是错的：
括号里就是它自己的名字，目录里也登记着；真正缺席的是上一节 §4.2 的 `storyboard.revision.created`
（`drama.storyboard.v2` 上只登记了本事件一个类型）。§11 C3 记录了这个缺口，但给的路径
`contracts/events/...` 在本仓库不存在——payload schema 一律平铺在 `contracts/` 下。

### `breakdown.revision-created.v2.schema.json`：事件带了分支，表却不把分支当身份

这份和上一条正好互补。S06 的表键含 `branch_id` 而事件漏了它，S05 反过来——
payload 第十个字段就是 `branch_id`，而 `breakdown` / `breakdown_revision` / `scene`
三张表的主键都没有它（§5.1 行 543、§5.2 行 578、§5.3 行 609）。`branch_id` 只出现在二级索引里，
说明它被当成查询维度而不是身份维度。于是事件里那个值区分不了任何东西：同一
`(tenant_id, breakdown_id)` 只有一行、一个 `head_revision`，分支 B 改自己的第 5 版
会直接写掉分支 A 的第 5 版（BRK-1）。

`retired_scene_ids` 这条更绕。§4.2 称它是本服务唯一的「删除类」信号，而它对应的
三种库内操作在语义上是删、是重指向、是拆（§5.3 的 `retire_reason` 三值），
`merged_into_scene_id` 有列有 CHECK 却没进事件。S09 的 `SCENE_DERIVED_FROM_SCRIPT`
边由本事件自报产生、按 `(tenant_id, edge_id)` 唯一，所以 S05 **指不出**该关哪条边；
S09 能靠自检查出漏关（「`valid_to IS NULL` 但 `target_revision` 已不是最新」），
但关边需要按 `edge_id`，而它已经不知道最新 revision 是什么了（BRK-2）。

`state` 的四值枚举有真 DDL `CHECK` 撑腰，但 §6.3 规定只有 `SEALED` 发事件，而
「旧 revision 变 SUPERSEDED」发生在写 outbox 之后、不发事件——所以 §6.5 那条
「已 SUPERSEDED 的 revision 收到更早事件不得恢复」在事件流上无法执行（BRK-3）。
`aggregate_sequence = revision` 会跳号（草稿不发），而 §6.5 把跳号当丢事件，
两者对同一个观测给出相反诊断，消费方分不出「这版是草稿」和「这版丢了」（BRK-6）。

打包的示例有个优点值得记：**信封是完整的**，16 个键一个不缺，`data_ref` 也在——和 S06 §4.3
那条相反。剩下的偏差都在取值层（`home_cell` 用连字符、`source` 用点号前缀、时间戳带毫秒），
只有 `data_ref.content_hash` 的 `sha256:` 前缀会真的校验失败，与 S06 的 SRC-4 同一根因（BRK-4、BRK-8）。

另外 `content_hash` 的输入含 `revision`（§4.1 行 467），而它同时是
`UNIQUE (tenant_id, breakdown_id, content_hash)` 的一部分——「同一内容不能出现两版」
这条约束因此在跨 revision 时挡不住重复提交，而重复提交恰恰会产生新 revision（BRK-9）。

### `adaptation.training-completed.v2.schema.json`：一条事件承载三个终局，而四个消费者等的是别的名字

这是本仓库唯一一条**把状态机当事件体**的发布事件。`state` 在 DDL 里有十四值
（S48 §5.1 行 513–516），但 §4.3 行 435 的注释只点了三个：`EVAL_FAILED`、`CANDIDATE`、
`FAILED`。中间七个阶段（`REQUESTED`→`EVALUATING`）要发的是 §4.4 行 453–460 那两个
事件，`REJECTED` / `EXPIRED` 的出口全是 HTTP 422，`PUBLISHED` / `BLOCKED` 由 §3.7 行 317
与 §3.8 行 330 指定发另两个名字。**所以可达集是 3 / 14**，而 §3.1 行 216 花整段论证
`EVAL_FAILED` 与 `FAILED` 必须分开——它们正好都在这三个里，判别面只有一个字段（ATR-2）。

最硬的一条是 **ATR-1**：§4.3 行 445 把 `data_ref` 写成裸字符串
（`"training-job/job_01J..."`，与 `subject` 逐字相同），而信封的 `data_ref` 是
`oneOf`：四键对象或 `null`，没有字符串分支。实测这条示例**过不了信封**。就算改成四键
也填不出来——`training_job` 既没有 `revision` 列也没有 `content_hash` 列；唯一像 hash 的
`request_hash` 是**命令**的摘要（§6.1 行 724），拿它冒充会把「同一命令重放」与
「同一 job 的两个阶段」混为一谈。所以本仓库的示例写 `"data_ref": null`：不是偏好，
是三个选项里唯一不伪造的那个。

**ATR-10** 是这条事件目前几乎没有消费者真正接收它的原因，也解释了为什么给它写完整
payload schema 有点讽刺。同一个事件在四个地方叫四个名字：registry 里是
`adaptation.training.completed`（唯一注册的），S11 的消费表等 `training.lora.registered`，
S15 的消费表等 `training.completed`（连 `adaptation.` 前缀都没有，还按
`adaptation_type=voice_clone` 过滤），S48 自己要发 `lora.blocked` 通知 S24/S25。
逐个核对 registry 的 55 个 topic：后三个**一个都不存在**。失败模式全静默——
S11 的 `lora_refs` 建议链路永远空转，S15 按 `type` 精确匹配会把这条直接丢掉，
没有任何一处报错。C48-4（§11 行 1085）已经如实上报了这件事，所以这是**已知缺口**而不是新发现；
schema 只是把它变得显眼：我们为一条无人订阅的事件写了 11 个字段的契约。

**ATR-3** 藏在信封上，任何 payload 校验都不会碰到：`aggregate_sequence` 被定义为
「job 内部的阶段序号（每个状态迁移 +1）」（§4.3 行 451），但 `training_job` **没有保存
这个序号的列**。唯一落点是 `outbox.aggregate_sequence` 自己，而 `outbox` 是发布队列、
`published_at` 一写行就能清；registry 给的 `normal_retention_days` 是 14。于是「单调」
这个前提没有持久化载体，而 `UNIQUE (aggregate_id, aggregate_sequence, event_type)`
（行 699）全靠它。

**ATR-9** 是一次实际读代码才发现的：§6.3 行 747 的队列认领 SQL 里
`tenant_id IN (SELECT tenant_id FROM tenant_training_enabled)`，可 §5 的数据模型
（行 484–715）**从未创建这张表**，§5.6「不建的表」也没把它列为「不建」。
「训练默认关」这个纪律是对的（§4.5 行 467 的 `TRAINING_NOT_ENABLED`），
但它在库里没有落点，也就没有「谁为哪个租户开了训练」的审计路径。

`metrics_summary` 是本文件唯一一个**完全不约束**的对象：计划给了三次、三个键集
（§3.6 行 289–292 四个指标、§4.2 行 395–399 三个、§4.3 行 442 两个），没有一处说
哪些必备，方向与量纲也没写，而 `evaluation_passed` 的全部依据就是它（ATR-5）。
不收 `properties` 是刻意的：把可能的值写进契约会把一个计划明确留白的决定固化成约束。

负向探针 27 例全部符合预期（九个必填字段缺一各拒、`state` 与 `adaptation_type` 越界拒、
三种引用形状互换拒、`sha256:` 前缀拒、`artifact_ref` 写成四键拒、`gpu_seconds_consumed`
负数与浮点拒、`data` 顶层多键拒），另单独在信封层复现了 ATR-1 与 ATR-11。
**ATR-11** 值得单记：§4.3 的示例把 `causation_id` 写成 `null`，而信封要求
`{"type": "string", "minLength": 1}` 且在 16 个必备键里——37 份带这个键的示例里
本文件初稿曾是唯一的 `null`。`plans/` 下写 `null` 的共三篇（S48 行 431、S45、S46），
动机都一样：「没有上游命令触发它」。动机合理，但信封没为根事件留出口。

### `project.episode-created.v2.schema.json`：唯一一条「有字段清单、没有出处」的事件

在当时的 17 个 envelope-only topic 里（今天已是 0 个），这一条最该先做，理由很朴素：**§5.3 行 656 真的写出了字段清单**。
`{episode_id, series_id, project_id, release_ordinal|null, shooting_ordinal|null, spec_hash}`——七个值，
六个能在 DDL 里指到列，第七个指不到。这不是巧合，是本仓库写作顺序的反面：多数 topic 只有「何时发」，
这条连「发什么」都有，于是它的缺陷一眼可见，而不是藏在散文里。

先说结论免得误导：**schema 是完整的，字段是有毛病的。**

`spec_hash` 是全文最大的空洞。`git grep -F spec_hash -- plans/S03-project-catalog.md` 只命中一处，
就是它自己的字段清单（行 656）。没有 DDL 列、没有命令体字段、没有流程步骤产出它。
创建事务（§4.1 行 480–482）依次写 `project` → `spec_revision(scope=project)` →
每个 series 的 `spec_revision(scope=series)` → `episode` + `episode_order` + 物化
`episode_spec_effective`，**没有 episode 级的 `spec_revision` 行**——尽管 `spec_scope_chk`
明明允许 `'episode'`（行 251）。于是候选只剩两个：`spec_revision.content_hash`（行 246，
`sha256(JCS(body))`）和 `episode_spec_effective.content_hash`（行 261，PK 是 `(tenant, episode_id)`，
每集恰好一行）。§1.4 行 97「三层合并结果**在写入时就物化**」和 §2.5 行 286「与 shot-spec.v1
同名字同义」都指向后者——但「更合理」不是出处，所以 schema 只锁形状（`^[0-9a-f]{64}$`，
照 `char(64)` 列型），语义登记为 PEC-1，等 S03 owner 二选一。

PEC-2 是同一个错误在别处的镜像。§2.3 行 197 刻意不建 `episode_number` 列，原话是
「一个方便字段会被下游当成权威，这是历史上同类系统最常见的错误」——方案防住了 DDL。
但事件把 `episode_order.ordinal` 这个**可变**值搬进了**不可变**的事件流，而改变它的动作
（重排）恰恰不发事件（行 550）。§4.2 行 505 又明说 `release_ordinal` 由调用方给、支持
「第 3.5 集」插入，所以创建后**必然**会重排。结果：消费方存下这个值 → 永远正确到第一次重排 →
之后静默过期，而且过期没有信号。§5.3 行 662 点名的 S21 批量调度、S28 归档命名、S43 交付清单
三个服务全都按发行序工作——**三个靠顺序吃饭的服务，拿到的顺序在第一次重排后即失效**。
schema 保留这两个字段（§5.3 列了它们），把 `null` 写成正当取值（行 507：纯草稿集在
`episode_order` 里没有行），并在描述里写明「只在创建时刻成立」。

PEC-4 是跨服务的。S08 §6.2 行 682 用这条事件建 manifest 壳，而 S08 的 manifest 唯一键是
`UNIQUE (tenant_id, series_id, episode_id, branch_id)`，`branch_id text NOT NULL`（行 46、56）。
事件里没有 `branch_id`。S03 唯一的汇报是 `series.branch_default text NOT NULL DEFAULT 'canon'`
（§2.2 行 154），并且行 167 明确「**不是分支权威**」。所以 S08 只有两条路：写死 `'canon'`，
或逐集回查。两条都能跑，但**没有一条写进契约**——而 §4.1 行 483 是「每个 episode 一条」，
一个 24 集项目就是 24 次回查。这是分歧发生的地方不在违约、而在无契约可依的典型。

PEC-3 小一点但仍值得记：三个名字都带「窗口」的东西指三件事——`release_window tstzrange`
（行 180，时间区间）、`episode_order.window_key`（行 209，重播窗口标识，`release` 用 `''`）、
`story_window_ref`（行 179，叙事时间，S16 解释）。事件里三个都没有。`release_ordinal`
之所以不歧义，只是因为字段名里写了 `release_`——名字在承担语义，这层已经由方案自己付过账，
schema 不改。

PEC-6 是这批里唯一需要往外补的：S03 §5.3 行 654–658 列了三条事件，registry 只登记了一条。
`project.episode.reordered` 和 `project.lifecycle.changed` 都标着「**新增，待登记**」，
逐条核对 55 个 topic 确认都不存在。讽刺的是，登记的那条恰好是**唯一不需要额外信息就能消费**
的（新集出现），两条携带真正变化的反而缺席。C03-3（§10.2 行 914）把补登记派给「契约维护方」；
本批次**不补**——登记一条没有生产方在跑的事件，和登记一条没有形状的事件，是同一种
「先造契约再造事实」（AGENTS.md）。

顺带修一处引用：§3.1 行 425 的 `draft→active` 副作用栏写「发 `project.episode.created`？**否**（见
§5.3 的事件缺口）」。§5.3 讲的是事件粒度与为什么需要 reorder，里面唯一说「不发」的是行 550——
**重排**不发，不是激活不发。结论碰巧对，引用是错的。这种「错误引用 + 巧合正确结论」最容易被
后来者当证据链，记为 PEC-5。

示例的信封这次是完整且自洽的：16 键齐备，`data_ref` 填四键对象（S03 既有 `head_revision=1`
也有 `content_hash`，和 S48 只能写 `null` 的情况不同）。负向探针 payload 层 16 例、信封层 3 例全部被拒：七个必填键缺一各拒、`release_ordinal: 0` 与
`shooting_ordinal: 0` 与 `"8"` 拒、`sha256:` 前缀 / 大写 / 短串三种 hash 形状各拒、多余 `branch_id` 拒、
空 `episode_id` 拒、`schema_version` 不等于 `"2.0"` 拒；信封层 `data_ref` 写字符串拒、
`causation_id: null` 拒、`aggregate_sequence: 0` 拒。正向探针 3 例如预期通过：`release_ordinal: null`
（行 507 的纯草稿集）、`data_ref: null`、`source` 不被信封钉死在 `S03`。

### `audit.export-ready.v2.schema.json`：事件名只有一个态，状态机有六个

S45 只有一条发布事件（§4.3 行 443「只有一条」），而它的状态机有两条终点出口不产生任何信号。
这份 schema 的每一个字段都有出处——七字段列表加完整示例（§4.3 行 445–466），是当时剩下 16 个
envelope-only topic 里证据最全的一条（当时的口径；本段写完之后 S46 也补上了 payload schema，此后资产五族与 S02、S42、S43、S44 相继补上，今天已是 0 个——每次变动这段括号里的数都要跟着改，它记录的是一个会移动的口径）——**问题不在字段，在字段背后的那台状态机**。

§3.6 行 305–310 的六个态是 `REQUESTED → BUILDING → SEALED → READY`，另有 `FAILED`（不可重试
错误直接终止，可恢复错误重试后仍失败）和 `EXPIRED`（`expires_at` 到点）。事件名是 `ready`，
行 326 说「完成时发」，所以**实际流里 `state` 恒为 `READY`**；`FAILED` 与 `EXPIRED` 都是终点，
却永远不会被通知。`POST /audit-exports` 只回 `202 + export_id`，`GET /audit-exports/{id}` 能查——
于是失败和过期**只能轮询**（AEX-2）。这与 S03 的 reorder 缺口同一类：有变化、有消费方、没有信号。
不对称更明显的是 `EXPIRED`：消费方收到「可以下载」之后，没有任何事件告诉它「窗口关了」。
`expires_at` 写在 payload 里，所以能自己算——但那要求消费方持久化这个时刻，而事件
`normal_retention_days` 是 14 天、示例的下载窗口是 24 小时，**事件还在、导出物已经没了**是常态。

AEX-1 是同一个字段名下的两个形状。`bundle_ref` 在计划示例里是信封 `data_ref` 那种四键引用
（`service_id` / `id` / `revision` / `content_hash`，行 461），在 DDL 注释里是对象存储三元组
（`{storage_ref, sha256, size_bytes}`，行 718）。两组键**无一重合**，所以不是同一形状的两种写法。
两个都不是笔误样式：四键那组与信封逐键同构，三键那组与对象存储实际产物一致（`storage_ref`
是 `s3://…` 路径，见行 613 对分段 `storage_ref` 的注法）。实现时二选一都说得通，消费方解出来的
东西却不同——四键只能拿 `content_hash` 比对，三键能直接去取文件。schema 按示例收四键（示例是
唯一给出取值的地方，且能与信封对齐）。

同一条里 `chain_continuous` 是**可为 NULL 的布尔**（DDL 行 719 只写 `boolean`），而 §3.6 行 324
说 `manifest.json` 里**必须有** `true|false`，§8 行 910 还说 `strict_audit` 租户「不允许
`chain_continuous=false`」。事件发在 manifest 写完之后，所以 `null` 在正确实现里取不到——但
DDL 允许它，而 E39 的「不伪造完整性」正靠这个字段做判定。schema 收 `boolean|null`（照 DDL，
不替 S45 收紧），三值布尔对「导出是否完整」无法当依据这件事登记在 `x-needs-owner`。

`segment_count` 在 DDL 里**没有列**，只有 `segment_ids text[]`（行 716）——它是数组长度，不是独立
事实。而它是数「参与本次导出的分段」还是「成功读完的分段」，计划没说：行 324 允许某段因
`INTEGRITY_FAILED` 被跳过并记进 `gaps`，被跳过的段仍可能在 `segment_ids` 里。全文 `segment_count`
只出现一次（行 459）。schema 只收非负整数，不解释分母。

AEX-3 是另一个方向的问题：`audit_export` / `audit_segment` / `audit_access_log` 三张表的 id
是**裸主键**（行 710、594、733），即全局唯一。本服务 §10 的 T45-34（行 1084）要求「租户 B 查
租户 A 的 record/stream/**导出** → 404，不泄漏存在性」（E28）。同仓至少八个服务在数据模型开头
写着同一句「所有主键/唯一约束含 `tenant_id`（E28：跨租户不得通过唯一冲突探测存在性）」——
`git grep -F '主键/唯一约束含' -- plans/` 命中的就是这些，**S45 不在其中**。读取路径有 RLS 兜着，
但 E28 要的是「不泄漏存在性」，而唯一冲突发生在写入路径。导出是最可能跨租户泄露的 id 类型
（会出现在客户工单、SIEM 规则、下载脚本里），所以这条值得裁。

AEX-5 是示例层的适配：计划示例的 `data_ref` 写裸字符串 `"audit-export/exp_01J.../manifest.json"`
（行 464），信封的 `data_ref` 是 `oneOf` [四键对象 | `null`]，没有字符串分支；`causation_id` 写
`null`（行 453），而信封要求非空字符串且在 16 个必备键里。前者想表达的信息本身有用（对象存储
路径），只是信封的 `data_ref` 不是放路径的地方——`bundle_ref` 三键那一支就有 `storage_ref`。
后者在计划里写 `null` 的共三篇（S45 行 453、S46 行 379、S48 行 431），动机都是「没有上游命令」，
信封一致拒绝。

负向探针 25 例、正向 5 例，结果与预期一致：八个必填键缺一各拒、`state` 小写与未知值拒
（`READY` / `FAILED` 通过）、两个计数 `-1` 拒（`0` 通过）、`chain_continuous` 收 `"true"` 字符串拒
（`null` 通过）、`bundle_ref` 多 `storage_ref` 键拒 / 缺 `revision` 拒 / `service_id` 非 `S\\d{2}` 拒 /
`sha256:` 前缀拒 / 写成字符串拒、`expires_at` 收 `null` 拒、payload 顶层多 `gaps` 或
`failure_reason` 拒、空 `export_id` 拒、`schema_version` 不等于 `"2.0"` 拒。
唯一一条**预期外**的是 `expires_at: "tomorrow"` 通过了：`format: date-time` 在本仓库只是注解，
`tools/validate/validate_examples.py` 用 `Draft202012Validator(target, registry=reg)` 且不传
`format_checker`。这个行为与全仓其余带 `format` 的字段一致，已在 schema 描述里写明，免得
被当成强约束。

这里有两层原因，别只记住第一层。**即使传了 `format_checker`，`date-time` 仍然不校验**：
`jsonschema` 把这个检查器包在 `with suppress(ImportError)` 里，只有装了可选的
`rfc3339_validator` 才会注册。所以「本机没装那个包」与「`date-time` 只是注解」这两件事
会同时成立，也同时会同时失效——`validate_examples.py` 里加一行 `format_checker=` 并不能
把 `tomorrow` 拦下来。真正会生效的格式检查只有 `date` / `time` / `email` / `uuid` /
`regex` / `ipv4` / `ipv6` / `idn-*` 这些不依赖第三方包的内置项。

全仓 30 处 `format: date-time`（分布在 71 个 schema 文件中的 27 个）因此全是注解；
88 个示例里的时间值本身都是合法 RFC3339，所以现在启不启用都看不到差别——**差别只在下一次
有人写坏的时候**。要不要为此加一个依赖、或改用 `pattern` 强约束，是 owner 决定（`pattern`
是硬校验，`format` 不是）；本仓现在不改语义，只把这两层原因写清楚。

### `integration.command-accepted.v2.schema.json`：事件是受理那一刻的快照，可唯一的消费方要的是流水

这是当时剩下 16 个 envelope-only topic 里字段证据最全的一条（今天已是 0 个）：§4.5 行 369–392 给了九个字段加完整示例，
八个值能在 DDL 里指到列。**字段是全的，缺的是时间。**

先说最锋利的一处。§10.6 行 1052 是全篇唯一一条对消费方的**硬性呈现要求**：

> S47 订阅 `integration.command.accepted`，用于外部客户端面板的「最近调用」列表。
> S47 **必须显示 `accepted_at` 作为水位**，不得把「最新受理」呈现为「最新完成」。

plans/S47-realtime-projection.md 行 85 确实把这条事件登记进了订阅表（用途「外部调用列表」）。
而 `accepted_at` 在本事件的字段清单里**没有**——它只在另外三处出现：DDL §5.2 行 517
（`accepted_at timestamptz NOT NULL DEFAULT now()`）、202 响应 §3.1 行 167、轮询响应 §4.2 行 328。
S47 自己又写着「事件里没有的字段，投影里就没有，而不是按上一次的值保留」（plans/S47 §2.1 行 88）。
两条合起来是个死结：**S47 被要求显示一个它按自己的规则不该有的字段**（ICA-1）。

「不得把最新受理呈现为最新完成」这半句还有第二层。§4.5 行 399 明说「只发 `accepted`，
不发状态变更」——回执后来变成 `FORWARDED` / `REJECTED` / `FORWARD_UNKNOWN` 时**没有任何信号**。
于是 S47 的面板不但拿不到水位，还会把每一条调用永久显示成受理态：不是把「受理」说成「完成」，
而是**把「已完成」也说成「受理」**，比那句禁令想防的呈现更糟。C46-6（§11 行 1067）把
「没有命令状态流转事件」登记成缺口，但影响栏只写「外部客户只能轮询」，没写这条内部硬要求也跟着塌了。
同样缺席的还有 `forwarded_at`（DDL 行 518）与 `upstream_ref` / `upstream_state` /
`upstream_state_as_of`（行 511–513）——§4.2 的轮询响应把它们当必填展示（行 335「必填：上游快照的水位」）。

**ICA-2 是一个不存在的态名，配一个碰巧正确的结论。** §3.1 行 174 写「回执有 `state`：
`ACCEPTED → FORWARDED | FORWARD_FAILED`」，而 §3.2 行 178–187 的状态机与 §5.2 行 506–508 的
`CHECK` 里都没有 `FORWARD_FAILED`。真实出口有四个：上游 2xx → `FORWARDED`、上游 4xx/409 且明确未执行
→ `REJECTED`、可重试失败 → `FORWARD_RETRYING` → 超次数 → `FORWARD_UNKNOWN`。行 174 后面那句
「按 §7.3 处理」指的也是 `FORWARD_UNKNOWN`，不是它写的那个名字。结论碰巧对，引用是错的——
这类「错误引用 + 巧合正确结论」最容易被后来者当证据链。真正要紧的是别反过来往 DDL 里补
`FORWARD_FAILED`：`FORWARD_UNKNOWN` 与「失败」的区分正是行 189–199 那条纪律的全部内容
（AGENTS.md 第 19 行禁令在 S46 的落点），给它换个带「失败」的名字，等于给出「可以重试」的暗示，
而重试会走到 S26 重复付费。

`state` 的 enum 照 DDL 收全七值，其中 `CANCELLED` **在回执状态机里没有入边**（ICA-7）：
它来自 §3.5 行 242–247 的 webhook 投递状态机（那边有「订阅被删/停用」这条边），
更可能是从 `webhook_delivery` 那份 `CHECK` 抄过来时多带的值。`STALE_UNKNOWN` 则有明确入边
（§3.3 行 218 的 7 天窗口）。两个状态机共享了 `CANCELLED` 这个值名却装两件事——
回执的态是「这条命令走到哪了」，投递的态是「这个 webhook 发出去没有」。

**`target` 是全仓第二处放开内部键的字段。** DDL 行 502 只写 `target jsonb NOT NULL`，
§2.3 行 106–119 的十二条命令分别打到 S21 / S07 / S40 / S43 / S16 / S18 / S02 / S23 / S44，
各自要的 id 不同（`run.get` 要 `run_id`，`change.apply` 要 `change_plan_id`，
`cost.query` 要时间窗与维度），而全文只给了 `episode.generate` 那一套
（`{project_id, episode_id}`，§2.2 行 85 与 §4.5 行 386 同形）。**没有第二套示例**——
找不出 `run_id` 作为 target 键的任何一处。一个字段、十二种形状、零份定义（ICA-5）。
消费方要按它建索引、校验、渲染 UI（S47 行 85、S45 §10.5 行 1042），做不到。
C46-2（§11 行 1063）提的 `contracts/integration-commands.v1.json` 正是缺的那份东西。
schema 只要求非空对象、内部键全放开；`minProperties: 1` 是我的判断不是出处，
理由在 ICA-5 里写了。

**ICA-4：讲防控的那一段引用了两个不在清单里的字段。** §4.5 行 397 说
「字段形状上：事件只带 `params_sha256` / `params_bytes`，正文根本没有可落的位置」——
这两个字段既不在行 381–389 的清单里，也不在示例里；DDL 行 503–504 有。
第一道防控（字段形状）在文件自己的字段清单里就不成立，第二道（`INGRESS_SECRET_SCAN` 就地替换）
是实的，§2.2 行 100 的顺序论证（先替换后哈希，否则合法重试变 `REQUEST_HASH_MISMATCH`）也扎实。
而「用 `data_ref` 回查」这条退路同样堵着：回执表只存摘要不存正文（行 503 注释），
§5.6 行 659 又明确不建 `request_body_log`，**所以 params 正文在整个 S46 里不存在**。
回查拿得到回执、拿不到入参。

**ICA-6 是一处比 C46-3 更早的空白。** §2.1 行 61–65 列了三种凭据：API key、OAuth2 access token
（企业 SSO 直连）、MCP client。而 DDL 行 498 的 `CHECK` 只允许 `api_client` / `mcp_client` 两个值——
**OAuth2 那一种无处落**。回执表里没有任何列能表达「这次调用是 SSO 用户授权的」，
`delegation_id`（行 499）是委派链不是主体类型。而 §10.5 行 1042 要求审计 details 带 `principal_type`，
审计要区分机器与人在。把 OAuth2 记成 `api_client`，审计里就分不出「客户的服务在调」还是「客户的员工在调」。
C46-3 问的是「`api_client` 在 S01 侧怎么映射」，比它更早的一层还没人问：OAuth2 该记成什么。

另外两处是过程性的，不影响 payload 形状但影响实现：§3.1 行 158 的 `INSERT` 只给四个值，
DDL 有十三个 `NOT NULL` 无默认值的列（ICA-3），而 `operation_id` 由谁在哪一步生成全文没写——
它恰好就是本事件的 `aggregate_id`，一次生成还是两次生成决定分区键稳不稳。ICA-8 是示例层的适配：
计划写 `causation_id: null`（行 379）与裸字符串 `data_ref`（行 391），信封两者都拒
（`causation_id` 无非空字符串之外的分支，`data_ref` 是 `oneOf`[四键对象 | `null`]，没有字符串分支）。
写 `null` 的动机是「这次调用没有上游命令」，可 S46 恰好有一条真实存在的 `command_id`
（客户端生成，§2.2 行 82），所以示例填它。`data_ref` 只能写 `null`——回执**有** id（`receipt_id`）
与真实服务 id（`S46`），但**全表没有 revision 列**（§5.2 行 489–524），也没有整行的 `content_hash`
（`request_hash` 是请求的哈希，不是回执的），四键对象拼不出来。这与 S48 的处境相同、理由不同。

示例的信封这次是完整且自洽的：16 键齐备，`data_ref` 写 `null`，`aggregate_sequence` 写 `1`
（每个 `operation_id` 只发一条）。负向探针 payload 层 34 例全部符合预期：九个必填键缺一各拒、
`state` 小写 / `FORWARD_FAILED` / 未知值各拒（`FORWARDED`、`CANCELLED` 通过——照 DDL 收全）、
`principal_type` 收 `oauth_user` 与 `human` 各拒（`mcp_client` 通过）、`target` 收空对象 /
字符串 / `null` 各拒（`{run_id}` 与带嵌套的任意键通过——内部键放开）、`client_trace_id` 收整数拒
（`null` 通过）、四个 id 收空串各拒、`operation_id` 收整数拒、顶层多 `accepted_at` / `params_sha256` /
`upstream_ref` / `run_id` 各拒。信封层 3 例负向（`causation_id: null`、裸字符串 `data_ref`、
`aggregate_sequence: 0`）全部被拒；正向 2 例通过，且**确认了两件本来就想确认的事**：
`source` 不被信封钉死在某个服务 id 上，`data_ref` 收四键对象（本例没用到，因为拼不出来）。

### `projection.cursor-advanced.v2.schema.json`：一条事件既是监控数据又是序号源，而两件事互相拆台

这是 S47 唯一一条发布事件，也是全仓唯一一个「事件本身就是可观测数据」的 topic：SSE 推送不进 Kafka
（§10.7 行 1054「**不写**『推送了一条 SSE』。那是海量、低价值的」），流上只留节流后的水位。
所以 `data` 的五个字段不是业务事实，是**投影健康度的一帧读数**。字段清单在 §4.4 行 443–449，
示例完整、五个值全有，DDL 支撑在 §5.1 行 483–513（投影表，`projection_state` 的 `CHECK` 在行 491）
与 §5.3 行 550–576（`sse_cursor` / `subscription_registration`）。这一条能先做，理由和 S46 一样：
**有完整示例，不是只有散文**。

**最要紧的一处是序号语义被节流拆掉了。** §4.4 行 456 规定 `aggregate_sequence = watermark`
并写明理由「与 S21 的语义一致，便于下游做缺号检测」；而行 454 又规定「每订阅每秒最多 1 条」。
在 §3.4 行 226 描述的实际速率下（`node.state-changed` 「可能每秒几十条」），节流后相邻两条本事件的
`watermark` 之差通常是几十到几百，不会是 1。一个照 §2.2 行 96 写缺号检测的下游（收到 seq 17、当前 15
→ 判缺 16 并标 `GAPPED`）会在**每一条**本事件上误报。这不是实现者的错——计划给了两个各自自洽的规则，
合起来把一个字段的既定用途取消了。PCA-1 记这件事，并给了两条修法（换一个每订阅 +1 的计数器，
或保留 watermark 但改掉行 456 给的理由）。

**第二处是 `lag_ms` 没有算式。** 这个字段在整份方案里只出现一次，就是行 447 那个 `2100`。
§8.1 行 838 的 `s47_projection_lag_seconds` 看起来像它的定义，其实不是：那是按 `event_class`
打标签的直方图、单位秒，本字段是无维度单帧读数、单位毫秒。两种自洽读法（投影处理延迟 vs 端到端延迟）
在 `GAPPED` 时差几个数量级，而 SLO 的 P95 ≤ 5s 只对得上其中一种——**选错的那一种会让「投影滞后监控」
监控到一个恒等于推送耗时的数**，永远绿灯。而 §9.1/§9.2 的测试清单里没有一条断言它的取值语义，
所以这个错不会被测试抓住。见 PCA-2。

**状态值三处口径不一致**（PCA-3）。§3.2 行 172–189 的状态机有五个（`ABSENT`/`LIVE`/`GAPPED`/
`TERMINAL`/`EVICTED`），DDL 的 `CHECK` 有四个（没有 `ABSENT`），示例用了 `LIVE`。本 schema 收**五个**
并说明 `ABSENT` 不是 CHECK 值而是「没有这一行」的表达——因为那正是 UI 必须回源、最该被上报的一帧。
只收四个的话，实现者会遇到「无值可发」，然后静默改用默认值 `LIVE`，于是「没有投影」被伪装成
「投影可信」——与 §3.2 行 191「**禁止**在缺号时『用上一个值继续显示成 LIVE』」是同一类伪装，换了个入口。

**还有一处是信封与 DDL 对不上，本 schema 修不了**（PCA-4）。DDL 行 492 是
`applied_watermark bigint NOT NULL DEFAULT 0`，而 §4.4 行 456 让 `aggregate_sequence = watermark`，
信封的 `aggregate_sequence` 却是 `minimum: 1`。于是**水位 0 的那一帧发不出去**。本 schema 把
`data.watermark` 收成 `minimum: 0` 照 DDL（不是照信封），信封不动——它是全仓共享的一份，为 55 个
topic 放宽下界不是这个文件能决定的事。

`run_id` 这一条我在登记时写错过两次，PCA-5 留的是第二次的结论。第一次写「`sse_cursor` 没有 `run_id`」，
回读 §5.3 行 554 发现它有（`run_id text NOT NULL`）。改成「两张表各存一遍同一事实、没有约束让它们相等」
之后又错了第二次——`subscription_registration` **根本没有 `run_id` 列**，它用 `scope_kind IN ('run','tenant')`
加可空的 `scope_id`（行 568–569）表达范围。真正的冲突比我先写的两版都硬：**DDL 承认租户级订阅，
而 `sse_cursor.run_id` 是 `NOT NULL`、本事件的 `run_id` 也是必需项——租户级订阅在这两处都无处安放**，
且一个租户级订阅要同时跟 N 个 run 的水位，而 `last_sent_watermark` 只有一个值。
两次错因是同一个：我凭印象推断表里有什么，没回读 DDL 就写进了结论。第一次改的时候也只回读了一半。

`subscription_id` 在两张表里都是裸主键（PCA-6）。它与 S46 的 `receipt_id` 同类，但暴露面小：
§4.3 行 424 的 `Last-Event-ID` 是 watermark 不是订阅 id。留白在另一处——§4.5 行 464 给
`RUN_NOT_FOUND` 注明「**含跨租户**（E28）」，行 466 的 `SUBSCRIPTION_NOT_FOUND` 没有这句注解。
裸主键 + 无注解，正是 E28 需要先定「一律 404」还是「先判租户」的地方。

示例的信封这次是完整且自洽的，`correlation_id` **必须**有值（PCA-8）：计划示例行 441 写的是 `null`，
而信封要求非空字符串。本事件的因果链天然有值（`causation_id` 指向上游那条推进水位的事件），
但 `correlation_id` 在发布路径上**没有来源**——outbox（§5.5 行 653–665）与三张表都没有这一列，
所以示例里填的是与 `causation_id` 相同的值，并在 PCA-8 标明这是「暂按同值」，不是定义。

负向探针：payload 层 17 例全部符合预期——六个必填键缺一各拒、多一个未声明键拒、
`watermark` 收 `-1` 与字符串各拒、`lag_ms` 收 `-1` 与浮点各拒（`integer` 不是 `number`）、
`state` 收 `LIVE2` / 小写 `live` / `null` 各拒、`subscription_id` 与 `run_id` 收空串各拒、
`schema_version` 收 `2.1` 拒。正向 8 例通过：五个 `state` 值全过（`ABSENT` 也在内，这是本 schema
有意收的第五个）、`watermark: 0` 与 `lag_ms: 0` 过。信封层 4 例负向全拒——`correlation_id: null`
（就是计划示例那一行）、`aggregate_sequence: 0`（PCA-4 的那一帧）、裸字符串 `data_ref`、空 `source`。
信封层正向 2 例通过，其中一例确认**信封 schema 本身**不把 `aggregate_id` 与 `data.subscription_id`
钉成相等——JSON Schema 的 `additionalProperties: false` 只管键，管不了同一份文档里不同位置的值
之间的关系。这条恒等式现在由 `tools/validate/validate_examples.py` 的
`aggregate_identity_problems()` 在**示例层**校验（要求 `data` 里某处有一个值等于
`aggregate_id`），但它比「主键字段相等」弱，且该事件不在六个豁免里。

### `narrative.script-revision-created.v2.schema.json`：事件说自己带够了核验用的 hash，实际一个都复算不出来

S04 这条事件是「一个已经不可变的版本出现了」的通知：它**只在 seal 时发**（§4.3 行 552、§5.3
行 686），DRAFT 不发，理由是 §5.3 行 690 那句「S05 会去分解一个还在改的剧本，S09 会建边，
S16 的抽取会跑在一个不存在的版本上」。所以拿到这条事件的一方可以确信：分块取到的正文从此
不会再变（§4.4 行 574）。

问题在于它**自带的核验材料是残缺的**。`content_hash` 的算式（§2.2 行 218）是
`sha256( JCS( {script_id, revision, title, language, structure, parent_revision} ) || "\n" || line_set_hash )`
——六个输入。本事件给了 `script_id`、`revision`、`parent_revision` 三个，`title` 不在 payload，
`structure` 被换成了一个没有定义的 `structure_hash`，而 `language` **根本不在
`script_revision` 表里**，它在 `narrative.script`（行 171 `language text NOT NULL DEFAULT 'zh-CN'`），
可读路径（§5.2 行 669–676）没有一处返回它，写路径（§5.1 行 648–659 的十条命令）也没有
一条能改它。§4.4 行 573 把「`content_hash` 必须与 manifest 里写的完全一致」定为 S16 R1 的
输入，于是 S16 想按规则自己算一遍，缺三个输入（NAR-3）。

`line_set_hash` 同理：它的算式（§2.3 行 284）要吃**每一行**的 `(line_id, line_revision, text_hash)`，
而本事件一个行身份都不带（`line_count` 是计数、`scene_markers` 是文本标记，二者都不是身份），
行级事件 `narrative.line.revision.created` 在 §5.3 行 687 与 §10 C04-2 里都还标着「新增，
待登记」——也就是说它**还不存在**（NAR-4）。

**`structure_hash` 是这份契约里最典型的「看起来有出处」的字段。** 它在整个
plans/S04-narrative.md 里只出现**一次**，就是 §5.3 行 702 那个示例值；`script_revision` 的
DDL（行 189–211）没有这一列，`content_hash` 的算式里也没有它（那里放的是整个 `structure`
对象，不是它的 hash）。所以它没有算法、没有输入键集、没有存储位置、在 `GET /scripts/{id}/
revisions/{rev}` 上读不到。本 schema 因此**只收 `string, minLength: 1`，没有收 `^[0-9a-f]{64}$`**
——收成 hex 就等于替计划编造了一个算法与长度。同一份文件里 `content_hash` / `line_set_hash`
都收了那个 pattern，依据是 DDL 的 `char(64)` 与 `episode-manifest.v2.schema.json` 的
`source_revisions[].content_hash`（S08 拿它做比对，两侧必须同形）（NAR-2）。

**`produced_by` 在方案里有三个互不相同的定义**，这是本份 schema 唯一一处「收并集」的地方：

| 出处 | 键集 |
|---|---|
| §2.2 行 201（`script_revision` 列注释） | `{task_id, agent_task_ref, context_pack_ref}` |
| §2.4 行 298（`story_arc_proposal` 列注释，**另一张表**） | `{task_id, definition_revision, context_pack_ref, model_ref}` |
| §4.2 行 533（散文） | `task_id / definition_revision / context_pack_ref`，还说「**必填**」 |

三者只在 `task_id` 与 `context_pack_ref` 上一致。本 schema 把五个键名全列出来、**一个都不设为
`required`**，`additionalProperties: false` 兜住键名之外的东西——把任何一组定成必填都等于替
owner 在三种写法里选了一种（NAR-1）。而 §5.3 行 704 的示例本身给的是 `null`，与行 533 的「必填」
直接冲突（NAR-6）。

**最后是一件本 payload 没有的字段造成的缺口：`state`。** `script_revision.state` 是 DDL 的
`NOT NULL` 列并带四值 `CHECK`（行 193、行 208–209：`DRAFT|SEALED|SUPERSEDED|WITHDRAWN`），
但本事件不带它，接收方只能靠「本 topic 只在 seal 时发」这条文档约定推出「这是 SEALED」。
而 `SEALED→SUPERSEDED`（§3.1 行 419）与 `SEALED→WITHDRAWN`（行 420）**都不发事件**——
§5.3 的事件表只有三行，`narrative.script.withdrawn` 还标着「新增待登记」，**superseded 连打算
都没有**。所以一个已经撤回的版本，在已经收到过 `created` 的下游那里永远是 SEALED（NAR-7）。

本文件 `x-needs-owner` 四条、`x-conflicts` 七条（NAR-1..NAR-7）。探针实测：payload 侧 25 条
负例（缺必填、空串、`revision` 为 0、hash 大写成 63 位、`scene_markers` 给整数、
`author.principal_type` 给 `service`、`author` 多一个键、`produced_by` 给 `{"foo":"bar"}`、
`parent_revision` 为 0、多一个 `script_kind`）全部被拒；envelope 侧 6 条负例（`correlation_id`
为 `null`/空串、`aggregate_sequence` 为 0、缺 `traceparent`、`data_ref` 给字符串、信封多一个键）
全部被拒；正例 8 条全过，其中关键的是 `produced_by` 取 `null`、只给 `{"task_id"}`、或给满五个
键三种形状都放行——这是「不选边」在测试上的表现。

### `asset.scene-revision-accepted.v2.schema.json`：事件说两个订阅方靠它重算，两个订阅方的方案里都没有它

S13 这条事件的存在理由是 §5.3 行 590 那一句：`topology_hash` 与 `topology_changed` **必须**
在事件里，「订阅方（S09 依赖图、S17 缓存）靠它决定是否要重算空间检查」。§3.3 行 402 又补一层：
「场景拓扑变了，所有在该场景生成的镜头都可能作废」。整条事件的设计就是围绕这两句话。

**这两句话在三份方案里都找不到落点。** `plans/S09-lineage-impact.md` 全文对
`asset.scene.revision.accepted`、`topology_hash`、`scene_asset` 的命中是 **0**；
`plans/S17-continuity.md` 对本事件与 `topology_hash` 的命中也是 **0**。真正消费它的是 S10
（§5.3 行 625 的 `asset.character.revision.accepted 等 | S11–S15`，而 S10 §1.1 行 27 把场景资产
列在 S13 名下）——S10 读的只有 `scene_ref.revision` 与 `scene_ref.content_hash`，用途是刷新检索
投影的引用，`**不消费内容**`。于是本事件被读的键，与它宣称存在的理由，是两组不相交的键。

第二层问题在血缘图上更硬：S09 的 `edge_kind` 是闭集 11 项（§1.2 行 70–80），**没有任何一项的
source 是 S13**。最接近的 `SHOT_DERIVED_FROM_SCENE`（行 73）是 `S05 scene → S06 shot_spec`，
而 S05 的 `scene` 是**分场**、S13 的是**场景资产**，不是同一个对象。所以「拓扑变了该重做哪些
镜头」这条因果，在 S09 的图上没有一条边能承载——上游节点根本不在图里（SCN-7）。

`impact_plan_ref` 那一路同样接不上。事件写 `{"service_id":"S09","id":"impact_01J...","revision":1}`
（行 577），但 S09 方案 §2.8 行 468 明写「`impact_plan` | 那是 S07 的 `change_plan`。S09 只有
`impact_analysis`」，而 S09 自己的 id 列叫 `analysis_id`（§2.3 行 283）、S07 的 `change_plan`
主键叫 `plan_id`（§2.1 行 114）。三个名字，示例用了第四个（`impact_`）。而 F18 补充（行 631）
说 S13 只校验「有值」、从不 dereference——所以行 400 那道「没有影响计划就不接受」的硬闸门，
在数据上退化成「非空字符串即可」。S14 子方案（行 436 / 449 / 564）用了同一字段名、同样没定义
键集，说明这是 docs/10 那一层的概念缺口，不是 S13 的笔误（SCN-1）。

还有一条更早的时序问题：`impact_plan_ref` 必须在 accept 命令里就有值（行 459），而影响分析要
基于这次改的拓扑——候选内容在 `scene_candidate.payload`（行 275–289）里，但 §4.3 的七条读路径
全部作用在 `revisions`/`canonical`/`lookup` 上，**没有一条能读候选**。外部服务推不出「这次拓扑
改了什么」，只能由调用方转述或塞占位值（SCN-6）。

**canonical 也有洞。** `POST /scene-assets/{id}:set-canonical`（§5.1 行 554）是独立命令，能不产生
新 revision、也不发任何事件地移动 canonical，而 `event-topics.v2.json` 里 S13 只有本主题一个
topic。按本事件缓存「当前 canonical 是哪一版」的一方，会在那次命令之后永久陈旧，且没有任何信号。
`canonical_pointer` 里真正决定「谁定、凭什么定」的 `decision_kind`/`decided_by`/`prior_canonical`
（行 291–305）一个都不进事件，所以连「这次换 canonical 是否经过人审」都拿不到——而 E05-D1
（行 706）恰恰拿场景 canonical 当 gate 依据（SCN-2）。

**收得紧的地方确实收紧了。** 三个键有真 DDL 依据，就不留活口：`topology_hash` 收
`^[0-9a-f]{64}$`（行 170 的 `sr_topohash_len` 是真 CHECK，不是注释——这一点与 S04 的
`structure_hash` 正相反，那个字段在 DDL 里根本不存在）；`location_class` 收四值 `enum`（行 120–121
是真 CHECK）；`anchor_ids` 的下限按 `location_class` 分档进 `allOf/if-then`（行 435：`primary_set`
≥4、`recurring_location` ≥2、`one_off` ≥1），这是本事件里唯一一条能从载荷自身字段完整推出的
不变量——探针实测三种分档各给一条越界负例，全部被拒。反过来，`lighting_axes` 与 `anchor_ids`
的**元素**都不收 `enum`：六个光照值与六个 anchor 名只在行 238 / 行 219 的注释里，
`scene_lighting_variant` 与 `scene_anchor` 的建表语句都没有 CHECK，按本仓库既定口径不构成 enum
依据（同 S09 的 `seed_kind`）。

`rights_ref` 是例外：S13 全文**没有一处写出它的键集**（DDL 行 161 只有 `rights_ref jsonb NOT NULL`，
事件行 584 是三字符 `{...}`）。S10/S11 的四键形 `{rights_owner, rights_id, rights_revision,
rights_hash}` 是一个可疑的类比，但照抄就是替 S13 编造契约，所以收成 `object` + `minProperties: 1`
+ 允许任意键，形式与 `integration.command-accepted.v2.schema.json` 的 `target` 同。更麻烦的是
E30 的防线只在读响应里：`rights_recheck_required=true` 出现在 §4.3 行 496 的 `/topology`，
**不在本事件里**。只吃事件的消费方拿不到「该不该复查权利」的提示，按构造就无法满足 E30-D1
（行 701）（SCN-4）。

本文件 `x-needs-owner` 五条、`x-conflicts` 八条（SCN-1..SCN-8）。探针实测：payload 侧 36 条负例
（12 个必填各缺一次、`topology_hash` 非 hex 与 63 位、`location_class` 给 `hero_set`、
`canonical_revision` 为 0、`anchor_ids` 空/重复、`lighting_axes` 含整数/重复、`era` 缺 `label`/
多一个键、`rights_ref` 给 `null`/`{}`、`scene_ref` 多键/`revision` 为 0/`service_id` 给 `S1`/
hash 给 `zz`、`impact_plan_ref` 给字符串/拓扑变更时为 `null`/多一个 `content_hash`/少一个
`revision`，以及三种 `location_class` 分档各一条越界）全部被拒；envelope 侧 6 条负例
（`causation_id: null`、`aggregate_sequence: 0`、缺 `data_ref`、信封多一个键、`data_ref` 给字符串）
全部被拒；正例 18 条全过，其中关键的是五组「不选边」形态都放行——`impact_plan_ref` 取 `null`
（且 `topology_changed=false`）、`became_canonical=false` 配 `canonical_revision=null`、
`era` 只给 `label`、`rights_ref` 给 S10 四键或给一个不透明单键、`impact_plan_ref` 指 `S07` 的
`plan_` 或 `S09` 的 `analysis_`。

一条**已知漏检**（不是本 schema 的缺陷）：信封的 `source` 与 `type` 都只收非空字符串
（`event-envelope.v2.schema.json` 的 `source` 是 `{"type":"string","minLength":1}`），所以把
`source` 改成 `S14` 而载荷仍是场景事件时，两层 schema 校验都放行。

**这层校验已经补上，但不在 schema 层。** `catalog` 检查管的是 `event-topics.v2.json` 里的
`producer` 字段；实例数据由 `tools/validate/validate_examples.py` 的
`envelope_identity_problems()` 管——它要求每个带 `type` 的示例命中的 `event_type` 在
`event-topics.v2.json` 里存在，且 `source`（裸 `S28` 或 `/cells/<cell>/services/S28` 两种写法）
解析出的服务等于该 topic 的 `producer`、URI 里的 cell 等于 `home_cell`。所以上面那两种改法现在会
被 `check_all` 的 `examples` 一项拒掉。`traceparent` 仍是裸字符串，仍无校验。

### `asset.character-revision-accepted.v2.schema.json`：说给订阅方加的那个键没有定义，点名的两个订阅方一个不订阅、一个不消费

S11 这条事件只有 7 个 data 键（§5.3 行 553–559 的七行值，`"data":{` 本身在 552），而本服务的核心产出是两个 manifest：
`view_manifest`（行 141 的注释列了 `front`/`side_left`/`back`/`three_quarter`）与
`expression_manifest`（行 142）。两个都不进事件。占掉一个键位的是 `view_count`。

**`view_count` 在整份方案里只出现一次**——就是事件示例里的那一行（行 558）。`git grep -F
view_count plans/` 只命中它自己。`character_revision` 的 17 列（行 135–156）里没有这一列，
`character` 的 12 列（行 98–109）里也没有，散文里没有一句说「计数要单独发出来」。行 141 说明
视图的名字与构成存在 jsonb 里，但没说要另发一个数。所以事件里最专为订阅方准备的那个键，是
示例里凭空写的。

第二层在行 565：`became_canonical` 必须显式给出，「订阅方（S10 投影、S17 缓存）靠它区分
『补图』和『换身份基准』」。**这两个订阅方都没有落点。** `plans/S17-continuity.md`
全文对 `asset.character.revision.accepted`、`became_canonical`、`identity_feature_hash`、
`character_revision` 的命中**全是 0**，且没有订阅表：§4.2 行 313「依赖读取（全部只读）」的
五条全是同步 GET，行 323 还说「**S17 不缓存 S16 的事实。** 每次 check 实时查询」——它连事实
都不缓存，「S17 缓存」无处落地。`plans/S10-asset-catalog.md` §5.3 行 625 确实登记了本事件，
但处理是「**不消费内容**，只刷新检索投影的 `owner_revision` 引用」，而刷新 `owner_revision`
（S10 行 221 的列）只需要 `character_ref.revision`。

与 S14 的 PRP-2 完全同构：**「某某需要它」这类句子没有被下游方案回执过**。区别是这里更进一层
——S17 连事件机制都没有。`became_canonical` 本身不是坏设计（把一次比较的结果显式化，省消费方
一次读），问题在于**它存在的理由是浮的**：一条字段的理由如果指不到具体消费方，重构时就会被
当作「没人用的字段」删掉，而那时代码里可能已经有人依赖它了。而且它回答的是「canonical 有没有
动」，不是「身份有没有变」——行 565 把这两个问题说成了同一个。

**收得紧的地方确实有真 DDL 依据，不留活口。** `character_ref.content_hash` 收
`^[0-9a-f]{64}$`：行 139 的 `char(64)` 加行 154 的 `cr_hash_len CHECK` 是真约束；
`source_kind` 收四值 `enum`：行 148 是真 CHECK；`rights_ref` 收成闭合四键：行 146 的行尾注释
逐字写出了四个键名，行 477 的读响应给了同形实例，C11-5（行 740）明说「与 S10 的 `rights_ref`
形态一致」。**这一点是 S11 与 S14 的关键差别**——S14 的 `rights_ref` 全文只有 `jsonb NOT NULL`
与事件里的 `{...}`，键集从未写出，所以那份 schema 只能收成开放对象；S11 自己写出了那一行，
所以这里能收四键。不是照抄 S10，是照抄 S11 自己的注释。

`identity_feature_hash` 收同一个 pattern，但**这比 DDL 更严**：行 145 只有 `char(64)`，没有
CHECK（唯一有长度 CHECK 的是 `content_hash` 的 `cr_hash_len`）。两个语义相同的 hash 约束不同等，
是 CRC-5 记的那条。差别的另一半是**故意的**：`content_hash` 有唯一索引 `cr_hash_uniq`（行 157，
防同一内容注册成两个 revision），`identity_feature_hash` 只有普通索引（行 158–159，因为同一角色
的不同 revision 可以有相同身份 hash）。

`canonical_revision` 收 `oneOf [null, integer ≥ 1]`：行 105 明写 NULL 是合法状态（「尚未接受」）。
两条 `allOf` 只把「`became_canonical=true` 时不能是 `null`」这一条可表达的不变量写死；
「等于 `character_ref.revision`」表达不了——Draft 2020-12 无法跨兄弟字段比较，所以探针里
`became_canonical=true` 配 `canonical_revision=3`（而 revision 是 12）**放行**，这是刻意留的。

本文件 `x-needs-owner` 六条、`x-conflicts` 六条（CRC-1..CRC-6）。探针实测 78 条、0 失败：
payload 侧 56 条负例（8 个必填各缺一次；根、`character_ref`、`rights_ref` 各多一个键；
`character_ref` 与 `rights_ref` 的子键各缺一次；`content_hash` 给 3 字符/63 位/65 位/大写/
非 hex/`null` 六种，`identity_feature_hash` 给 1 字符/非 hex/`null`，`rights_hash` 给 4 字符；
`revision` 给 0/负数/浮点/字符串/`true`，`view_count` 给 0/负数/浮点/字符串，`rights_revision`
给 0/负数/浮点；`source_kind` 给七种非法值；`rights_ref` 给 `null`/`{}`/空 `rights_owner`；
`became_canonical` 给字符串或 `0`；`true` 配 `canonical_revision=null` 或 `0`）全部被拒；
envelope 侧 10 条负例全部被拒；正例 12 条（payload 11 + 信封 1）全过，其中关键
的是四组「不选边」形态放行——`source_kind` 四个合法值、`became_canonical=false` 配
`canonical_revision=null`、`rights_owner` 给 `S02` 或给任意服务编号、以及上面那条 `true` 配 3。

一条**已知漏检**，与 S13 那条同源：信封对 `source`/`type`/`traceparent` 都只收非空字符串，
所以把 `source` 改成 `S14`、把 `type` 改成任意主题名、把 `traceparent` 写成垃圾串，两层校验都
放行。**前两种现在由 `envelope_identity_problems()` 拦住**（见 S13 那一节的说明），
`traceparent` 仍然无人管。

### `asset.costume-revision-accepted.v2.schema.json`：为一个「必须发出去」的键，事件只发了三分之二

S12 §5.3 行 530 写得斩钉截铁：「`variant_axes` 必须发出去。否则订阅方（S17 的期望状态校验）
不知道『这一版支持哪些破损档』，就只能猜。」而行 522 的事件示例给的是
`{"damage":[...],"wetness":[...]}`——两轴。§1.3 行 84–86 声明的是三轴，第三轴是 `makeup`
（`natural` / `evening` / `injured_smudged`）；`variant_manifest` 的 DDL 注释（行 157）与 §4.1
的起草命令（行 379）也都是三轴；§4.3 行 447 的读响应回显更是带 `makeup` 的。**事件是本服务
唯一一处只发两轴的地方。** 一个为消除猜测而加的字段，在它自己身上制造了一次猜测（CCA-2）。

**被点名要读它的 S17 对它和 `costume` 双双零命中。** `plans/S17-continuity.md`（532 行）全文
对 `variant_axes`、`costume`、`asset.costume.revision.accepted` 的命中均为 **0**，且没有订阅表
（对 `订阅` 的命中为 0）。它 §7.3 行 434 的视觉状态检查第二步是「从 S10 取 canonical 变体：
S11 的角色外观 + S12 的服装 + S14 的道具」——**经过 S10**。而 S10 明说 `variant_selector` 由
owner 解释、自己不知道变体叫什么（S10 行 224 / 252 / 829），所以走 S10 也拿不到「这一版支持
哪些轴」。全仓对 `asset.costume.revision.accepted` 的命中 **3 处，全部在 S12 自己的方案里**。

**九处把 `S33/S34` 当身份质检来源，而它们是音效生产与 BGM 制作。** 行 234 / 422 / 665 / 681 /
686 / 697 六处字面 `S33/S34`，另有行 204 / 320 / 615 三处裸 `S33`，合计 **9 行**（资产族四篇合计
25 行：S12 九、S14 八、S11 六、S13 二）。`service-catalog.v2.json`
里 S33 是 `音效生产`、S34 是 `BGM与音乐`（DESIGN.md 行 159–160 同），两篇方案对 `costume` 的
命中均为 0。改成真正做视觉质检的 S35/S36 **也接不上**：`QCReport v2` 的 `required` 里有顶层
`shot_ref`（S35 行 195–203，定义在行 326–334 且四键全必填、`additionalProperties:false`），
一张服装资产图没有 `shot_id` 也没有 `node_epoch`，过不了这个 schema；S35 与 S36 两篇方案对
「入库」的命中也都是 **0**。于是 `costume_revision.quality_gate_ref`（行 162，`NOT NULL`）与
`policy_auto` 的硬门（行 409–412）在当前文档树里**没有可执行的产出方**（CCA-1）。

**两个判据在全仓各只出现一次。** `identity_invariance_check` 只出现在行 411 的事务伪代码里，
`costume_candidate`（行 229–240）没有这一列，取值只见过 `'pass'`；`IDENTITY_CHANGED` 只出现在
行 640 的 E24-C2，不在 S35/S36 任一份 check 表里——S36 行 187 的真实 id 是
`VIS.IDENTITY.FACE_SWAP_WITHIN_SHOT`（`blocker`）。所以这条质量门在两个方向上都没有可执行的
定义：判据读一个不存在的东西，断言产出一个不存在的 id（CCA-5）。

**`unverified_refs` 四处提到、零处定义元素形状。** 行 391 / 479 / 523 / 537 都提它，没有一处
说它长什么样；`costume_revision` 的 16 列里没有它，`costume_candidate` 里也没有——它只活在
事件与 HTTP 响应里，接受事务之后不可回查。与之对应的表叫 `revision_reference`（§2.6 行 267）
是另一个名字。声明的消解方 S14 对 `unverified_refs` 的命中是 **0**，于是「允许悬空 + 事后消解」
这条折中在生产侧发出去、在消费者侧不存在（CCA-3）。

**10 个 revision 列不进事件，`unverified_refs` 这种无列派生值占了一个键位。** `costume_revision`
16 列（行 149–164），事件 8 键（行 517–524）：缺席的是 `payload` / `outfit` / `material_palette` /
`accessories` / `variant_manifest` / `source_kind` / `parent_revision` / `quality_gate_ref` /
`accepted_at` / `accepted_by`；事件另有 6 键不是 revision 列（`costume_ref` 折叠四列、
`character_ref` 与 `costume_kind` 来自 `costume` 表、`became_canonical` 与 `canonical_revision`
来自 `canonical_pointer`、`unverified_refs` 无来源表）。这个折算是合法的——事件是通知不是镜像
——但 `decision_kind`（行 249 的真三值 CHECK）不进事件，于是「这一版凭什么成了权威」在
`became_canonical=true` 里看不见，而 `policy_auto` 溜过去正是最需要被看见的那种成法（CCA-4）。

逐键收窄的依据：`costume_kind` 收四值 `enum`（行 118–119 是真 CHECK）、`costume_ref.content_hash`
收 64 位小写 hex（行 152 `char(64)` + 行 166 `costr_hash_len`）、`revision` 收 `minimum: 1`
（行 151 真 CHECK）、`became_canonical=true` 时 `canonical_revision` 不许为 `null`（行 128 的
`canonical_within_head` 只给上界，这条 if/then 是本 schema 的补充判断）。**`variant_axes` 的键
与值都不收 `enum`**（行 158 只有注释、无 CHECK；§11.2 行 704 把「变体轴是否全局固定」列为未决并
选「可扩展」），`source_kind` 的四值 CHECK（行 160）不在事件里所以无从收，`rights_ref` 与
`unverified_refs` 都只收结构——前者全文无键集（行 159 只有 `jsonb NOT NULL`），后者连元素
形状都没有。

本文件 `x-needs-owner` 六条、`x-conflicts` 七条（CCA-1..CCA-7）。探针实测 89 条、0 失败：
payload 侧 68 条负例（7 个必填各缺一次；根与 `costume_ref` 各多一个键；`costume_ref` 四子键
各缺一次；`costume_ref.service_id` 给 `S1` / `s12`，`id` 给空串，`revision` 给 0/-1/1.5/字符串
/`true`，`content_hash` 给 3 字符/63 位/65 位/大写/非 hex/`null`；`character_ref` 给缺键、
多键、空 id、`S111`、字符串；`costume_kind` 给六种非法值；`became_canonical` 给字符串/数字/
`null`；`canonical_revision` 给 0 与 `became_canonical=true` 两组合；`variant_axes` 给空对象/
空数组/非数组/空串/重复值/数字/`null`；`unverified_refs` 给 `{}` 元素/字符串元素/`null`/对象；
`rights_ref` 给 `{}`/`null`/字符串；`schema_version` 给 `1.0` 或缺键）全部被拒；envelope 侧
5 条负例全部被拒；正例 16 条（payload 15 + 信封 1）全过，其中关键的是四组「不选边」形态放行
——`character_ref` 给 `null`、`variant_axes` 带第三个未声明轴（`makeup`）或未列名的轴名/取值、
`unverified_refs` 任一开对象元素、`rights_ref` 任意两键。

一条**已知漏检**，与 S11 / S13 / S14 四条同源：信封对 `source`/`type`/`traceparent` 都只收非空
字符串，所以把 `source` 改成 `S11`、把 `type` 改成任意主题名、把 `traceparent` 写成垃圾串，两层
校验都放行。**前两种现在由 `envelope_identity_problems()` 拦住**（见 S13 那一节的说明），
`traceparent` 仍然无人管。

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
                                        └──data_schema_ref──▶ 55 个 payload schema
                                                              （见上表；envelope-only 已归零）
```

改服务元数据请改 catalog，然后重新生成 `service.yaml`，不要手改生成物。

## 一个入口跑完所有仓库级检查

```bash
python tools/check_all.py
python tools/check_all.py --list
python tools/check_all.py --only links,examples
```

十一项检查：`json`（148 个 JSON 解析）、`payloads`（110 条 `schema_ref` / `data_schema_ref`
都指向真实文件，其中 55 个 topic 带 payload schema）、`checkids`（四份 QC 方案里 99 个
`check_id` 是否符合 schema 的三段命名，以及 146 处正文引用是否都有登记）、`links`（1325 条相对链接全部可达；另有 `docs/design-package/` 的 56 个 md、94 条链接按 AGENTS.md 是只读存档，不核但报数）、`catalog`
（服务 id、topic `producer`、`published_events`、`depends_on` 的目标都存在，
`docs/02` 镜像的五列与 catalog 逐字段一致）、`families`（动作族文件自己声明的
5 条 invariant 里的 **4 条**）、`scaffold`（118 个生成物与 catalog 一致）、`examples`（**71** 个示例对
schema 校验，另 1 个刻意不映射）、`maturity`（catalog 的 `maturity` 与 `services/*/src/`
里到底有没有代码一致）、`readme`（上面这段里的数必须还是各检查真跑出来的数）、
`registry`（`check-registry.v2.json` 存在时才跑）。

`readme` 这一格是在核**这段自身**，以及本文件开头那张「布局」表。一是核开头的
「十一项检查」这个数等于 `--list` 真列出的条数；二是把本段里 9 个手敲的数字逐条和
对应检查自己算出的数比；三是核布局块里 5 个数（48 服务 / 55 topic / 55 event_type /
12 MCP 工具 / 21 个 OpenAPI 操作）—这三处不一致都 FAIL 并指出**是哪一个**。

布局块那 5 个数不是从某个检查的 `note` 读的，而是直接读源文件（catalog、topics、
mcp-tools、openapi.yaml），因为没有检查报这几个数。加这一格时它当场抓到：布局块写着
「48 个 topic，注册 55 个 event_type」，而 `event-topics.v2.json` 里是 **55 个 topic**，
本文件另外 9 行也都写 55——同一份文件里，第五节之后全对，开头那张表错了一个数。

那 9 个数是 `json` 148、`schema_ref` 110、payload topic 55、`check_id` 99、`links` 1325、
stated invariant 5、`scaffold` 118、`examples` 71、unmapped 1。加这一格当天就抓到一个：
`scaffold` 原写着 120，而它实际只比对了 **118** 个生成物——多出来的 2 个是 `check_scaffold`
自己拷进临时根做输入的 `service-catalog.v2.json` 和 `event-topics.v2.json`，
被 glob 一起数进去了。这个数在 README 里挂了很久没人核，因为没人会去数目录里有几个文件。

反向测过十个方向（上面 9 个数各改 1，再加开头「十一」改成「十」），十次全部被抓到，
报的是 `README says 149 for JSON 解析, json says 148` 这种话；还原后 `git diff` 为空。
布局块另测了六个方向（5 个数各改 1，加上把上面那个真错原样放回去，再加改掉节标题），
六次全抓到，例如 `README layout block says 48 for topics, the source file has 55`。
另外还测了段落锚点消失（改节标题、删掉「N 项检查：」开头）两种，各自报自己的错。

`checkids` 这一格做两件事。一是上面说的：四份 QC 方案的**表格行**里的 id 必须符合三段命名。
二是**正文引用**：任何 3 段及以上的 id 被引用了，就必须登记在它所前缀的那份方案的注册表里。
第二件事是后补的，因为第一件只看形状——`SUB.LAYOUT.CPS_EXCEEDED` 形状完全合法，只是
S38 的 §5 表里没有这一行，`SUB.CUE.VERSION` 更是连 `CUE` 组都不存在。这两处加上
S38:241 的正文缩写，是三处已知悬空引用，列在 `KNOWN_DANGLING_REFS` 里，等对应 owner 决定
是改文案还是补注册表。**这不是替任何人选边**：三处正文一个字没改，改的是「一旦有人改了，
这一格必须提醒把条目删掉」——owner 按两种修法里任何一种修好，陈化测试都会 FAIL 要求删条目，
否则条目就陈了，将来同一个 id 再被误引用反而抓不到。

两段式的提及**不算**引用，有意为之：`VIS.SAFE_AREA` 是组引用（S36 有
`VIS.SAFE_AREA.TEXT_OVERLAP` 等），`SUB.TIMING.CPS` 是同一句后半截就点了完整 id 的缩写。
按这个口径，四份方案里 3 段及以上的提及共 146 处，其中 3 处悬空。

`links` 这一格有两个看不见的边界，两个都写进 `note` 了，因为**覆盖得比看上去少**
恰恰是这个模块开头那句「没跑成的检查不能读成绿灯」要防的事：

- `docs/design-package/` 按 AGENTS.md 是原始设计包存档、只读，里面的断链本仓库不能修，
  所以不 FAIL。但它被数出来报在 `note` 里（56 个 md、94 条链接），有断链时 `note` 会追加
  `-- N broken there`——**看得见，但不拦**。
- `glob("**/*.md")` 不下钻以 `.` 开头的目录。这恰好把 `.claude/worktrees/`（别的会话的检出，
  不是本仓库的内容）排除掉了，但那是 glob 的副作用，不是决定；`_tmp` 之类的临时目录也会
  藏在那里。

刻意不 pin 的：`families` 的「4 of 5」里那个 4（是语义判断，不是计数）、
`checkids` 的不合规条数（白名单本来就该缩短，写死等于要求它别缩短）、
`checkids` 的悬空引用条数（同上，`KNOWN_DANGLING_REFS` 本来就该缩短）、
`links` 的只读存档条数（存档是冻结的，数它没有意义）、
passed/skipped 的拆分（数据文件一落地就变）。

十一项里 `registry` 那一格今天**什么都没校验**（数据文件不存在），所以它不计进 `passed`：
结尾那行现在是 `10 check(s) passed, 1 skipped (registry): nothing was verified there`，
那一格自己的状态也打成 `skip` 而不是 `ok`。这条和「一项没跑成不能读成绿灯」是同一条原则，
只是形态不同——前者是跑失败了，这里是没东西可跑。按旧写法「十项全过」会把一个空转的格子
算成一票通过，而这个文件里恰好有一段（下一节）在说 `families` 那第 5 条一次都没执行过。

`maturity` 这一格是后补的，补之前那两句话没有任何东西在核：本文件开头的 README 说
「所有服务的 `maturity` 均为 `design_only`」，而 `maturity` 的值是 `scaffold` 从 catalog
照抄的——它说的是**编 catalog 的人当时的意图**，不是磁盘上的事实。两边都可能歪：代码先到而
catalog 没跟，仓库看起来比实际更没做（没人会去跑它的测试）；catalog 先改而 `src/` 还空，
就是在报假进度——和 AGENTS.md 那句「不能把 schema 校验通过当作分布式正确性证明」是同一种错，
只低一层。所以这一格两个方向都报 FAIL，并单独核 README 那句话是否仍然成立。
反向测过：往 `budget-ledger/src/` 放一个文件 → FAIL（「holds 1 file(s) but the catalog still
says maturity=design_only」）；把 catalog 某条改成 `implemented` → FAIL 两条（空 `src/` +
README 与目录不符）。

`families` 为什么是 4 条而不是 5 条：那第 5 条（`registry entries MUST NOT name an action
outside families[defect_type].chain ∪ {fallback}`）说的是**登记项**，只能对着
`check-registry.v2.json` 才能判，所以它归 `registry` 那格；而那个数据文件至今不存在，
`registry` 打的是 `skip` 不是 `ok`，结尾也不把它算进 `passed`。也就是说第 5 条今天一次都没
执行过，写「5 条 invariant」会让这一格读起来比实际覆盖得更多。

`examples` 这一格的数一直随加示例往上走，所以比别的格更容易过期：这里曾经写 63、写 66，
而磁盘上已经是 71 校验 + 1 无 schema = 72 个文件。数法固定为校验器自己打印的那句
`{ok} example(s) valid, {failing} failing, {skipped} without a schema`——`ok` 是要写进这一格的数，
`skipped` 是那 1 个 `capacity-plan.json`（`SCHEMA_OF` 里显式映射成 `None`：它是容量测算输入，
不是事件，没有对应 schema）。`ok + skipped` 必须等于 `contracts/examples/` 下的 `.json` 个数；
不相等说明有文件没被映射到，校验器会另打 `UNMAPPED` 行并以 1 退出。

`catalog` 里还有一条**从没执行过**的分支：它会校验 topic 的 `consumers` 是否都是真实
服务 id，但 `event-topics.v2.json` 的 55 个 topic **没有一个带 `consumers` 键**，那条
`for c in t.get("consumers", []) or []` 至今 0 次进入。所以这一项的绿只覆盖生产者一侧。
现在 `catalog` 的 note 会显式写出 `[producer-only: no topic declares consumers]`，
免得这行绿读成「生产消费方都查过了」。要不要给 topic 加 `consumers` 是 owner 决定
（见 `policy.revision-published.v2.schema.json` 的 POL-* `fix_shape`），不是这里能补的。

`payloads` 是后加的，因为它抓的错比 `json` 更晚才暴露：一个 topic 指向不存在的 schema
文件时，消费方看到的是「有类型契约」，而校验器在读取时才会炸——失败落在别人服务里。

退出码 0 = 全过，1 = 有真问题，2 = 某项没跑成。**后两者要分开**：一个没跑成的检查
不能读成绿灯，这和上面 `jsonschema` 缺失返回 2 是同一条原则。

写这个入口之前，这些检查是几条各自为政的命令，没人一起跑，也就等于「族校验必须在加载期
自动执行」这句话没有执行入口——校验器只有在被调用时才约束任何东西。
`registry` 这一项现在报 `skip`（不是失败，也不是通过），因为 99 条 check 的字段确实还没补齐
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
     预算」的角色，JSON 无对应工具——**能力清单差异，不是措辞差异**；
   - **同一件事有四处各自独立的 scope 枚举，没有两处集合相同**：`docs/13` 正文 5 档、
     [S46:475](../plans/S46-developer-integration.md) 5 档（与正文一致）、
     `mcp-tools` JSON 6 个值、[S19:610](../plans/S19-agent-runtime.md) **只有 4 个值**
     （少 `review`）。S19 的表要让每个工具都落一行 `required_scope`，于是
     `drama.review.decide`（`review`）与 `drama.delivery.publish`（`publish`）
     **在 S19 里注册不进去**——把 `publish` 的争议按 `execute` 解决掉，`review` 仍然被拒。

   补/删工具、改 scope 都属于授权决策，标 `NEEDS_OWNER`，不从任何一侧推断；S19 是漏写
   `review` 还是有意与客户端授权不同轴，同样两读都成立，不选边。
   原文「没有 scope 字段……只有 annotations 和 input schema」是错的。
   逐处比对见 [§16.28](../delivery/acceptance/contracts-gap-list-corrections-2026-11-06.md)。
3. **`examples/` 里所有 hash 和金额都是占位符**（`aaaa…`、`bbbb…`）—— 结构示意，不能直接当测试 fixture。
   **但形状是全的**：71 个示例全部通过各自 schema 校验（`capacity-plan.json` 没有对应 schema，
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
