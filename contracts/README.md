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
  *.v2.schema.json             9 个企业 schema（JSON Schema 2020-12）
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

## 三层文件的关系

```
service-catalog.v2.json  ──生成──▶  services/<slug>/service.yaml
        │
        └──引用──▶ event-topics.v2.json ──引用──▶ event-envelope.v2.schema.json
```

改服务元数据请改 catalog，然后重新生成 `service.yaml`，不要手改生成物。

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
