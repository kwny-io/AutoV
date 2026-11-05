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
  *.v2.schema.json             10 个 schema（JSON Schema 2020-12）：9 个企业，
                               + artifact-ingested（单 topic 的 payload，见下）
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
是纯增量，缺省 = 没有 payload 契约（envelope-only），也就是今天 54/55 个 topic 的状态。

**这个键目前只有 `artifact.ingested` 一个 topic 带**，这是有意的，不是遗漏。其余 topic
要么 payload 已经由自己的企业 schema 表达（例如 QCReport 走 `quality/qc-report.v2.schema.json`，
经 REST 传递），要么本来就只发一个引用。**给某个 topic 加 `data_schema_ref` 是一次
契约变更**：生产方要按 schema 发，消费方可以开始校验，两边都得跟着动。

`tools/validate/validate_examples.py` 会读这个键：示例的 `type` 命中某个 topic，就先按
envelope 校验整条事件，再按 `data_schema_ref` 校验 `data` 子树。`contracts/examples/artifact-ingested.json`
是这条路径的现有用例。

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
                                        └──data_schema_ref──▶ artifact-ingested.v2.schema.json
                                                              （仅 artifact.ingested，见上）
```

改服务元数据请改 catalog，然后重新生成 `service.yaml`，不要手改生成物。

## 一个入口跑完所有仓库级检查

```bash
python tools/check_all.py
python tools/check_all.py --list
python tools/check_all.py --only links,examples
```

八项检查：`json`（41 个 JSON 解析）、`checkids`（四份 QC 方案里 99 个 `check_id`
是否符合 schema 的三段命名）、`links`（1317 条相对链接全部可达）、
`catalog`（服务 id / topic 生产消费方 / `docs/02` 三方一致）、`families`
（动作族文件自己声明的 5 条 invariant）、`scaffold`（120 个生成物与 catalog 一致）、
`examples`（17 个示例对 schema 校验）、`registry`（`check-registry.v2.json` 存在时才跑）。

退出码 0 = 全过，1 = 有真问题，2 = 某项没跑成。**后两者要分开**：一个没跑成的检查
不能读成绿灯，这和上面 `jsonschema` 缺失返回 2 是同一条原则。

写这个入口之前，这些检查是五条各自为政的命令，没人一起跑，也就等于「族校验必须在加载期
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
   **但形状是全的**：17 个示例全部通过各自 schema 校验（`capacity-plan.json` 没有对应 schema，
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
