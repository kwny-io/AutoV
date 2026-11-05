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

**这个键今天有 23 个 topic 带**（32 个仍是 envelope-only）。没带的不全是遗漏：payload
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
                                        └──data_schema_ref──▶ 23 个 payload schema
                                                              （见上表；其余 32 个 envelope-only）
```

改服务元数据请改 catalog，然后重新生成 `service.yaml`，不要手改生成物。

## 一个入口跑完所有仓库级检查

```bash
python tools/check_all.py
python tools/check_all.py --list
python tools/check_all.py --only links,examples
```

九项检查：`json`（75 个 JSON 解析）、`payloads`（73 条 `schema_ref` / `data_schema_ref`
都指向真实文件，其中 23 个 topic 带 payload schema）、`checkids`（四份 QC 方案里 99 个
`check_id` 是否符合 schema 的三段命名）、`links`（1321 条相对链接全部可达）、`catalog`
（服务 id / topic 生产消费方 / `docs/02` 三方一致）、`families`（动作族文件自己声明的
5 条 invariant）、`scaffold`（120 个生成物与 catalog 一致）、`examples`（40 个示例对
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
   **但形状是全的**：40 个示例全部通过各自 schema 校验（`capacity-plan.json` 没有对应 schema，
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
