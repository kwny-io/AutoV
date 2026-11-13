# S20 · Prompt 模板与编译服务 · 实现方案

状态：design_only。服务标识 `prompt-compiler`，schema `prompt_compiler`，数据所有权：`TemplateRevision` / 供应商映射 / `CompiledPromptManifest` / 编译回归结果。

配套：[docs/08 §3](../docs/08-agents-prompts.md)（导演 DSL 与编译顺序）、[S06 §10.3](S06-storyboard.md)（ShotSpec 的严格边界）、[S04 §1.2](S04-narrative.md)（对白文本权威）、[S24](S24-model-registry.md)（能力快照）、[S10](S10-asset-catalog.md)（资产解析）、[S25 §2.2](S25-model-routing.md)（模板兼容性与对白时长）、[S26](S26-generation-jobs.md)（唯一付费路径）、[S35](S35-technical-qc.md)（证据与缺陷分界）。

---

## 0. 一句话边界

**S20 是 `TemplateRevision`、供应商映射、`CompiledPromptManifest` 与编译回归结果的唯一写者；它把一份 `ShotSpec`（或等价的语义 DSL）编译成某个具体模型快照可消费的 Prompt 与参数，并证明这次编译是可复现的。它不改写任何事实，不选模型，不发起任何付费调用。**

| 写 | 不写 |
|---|---|
| `template` / `template_revision` | 剧本、场次、分镜（S04/S05/S06） |
| `adapter_binding`（模板×供应商×模型快照） | 路由决策、候选排序（S25） |
| `compiled_prompt_manifest` | 供应商任务、费用（S26） |
| `compile_regression_result` | 资产绑定集、身份约束来源（S10/S11） |
| 编译 warning 与省略记录 | QC 结论、缺陷（S35） |

三个明确不做：

1. **不改写内容。** 对白零变换（§1.2），`emotion`/`lighting` 这类语义字段只做**映射**，不做**扩写**到改变剧情（§1.3）。
2. **不选模型。** 模型由 S25 选，S20 只回答"这个模板能不能服务这个候选"和"服务时具体要渲染成什么"。
3. **不发起调用。** S20 没有供应商凭证，不在出站白名单里（与 [S19 §10.3](S19-agent-runtime.md)、[S26 §1.3](S26-generation-jobs.md) 同一条）。

一条必须坚持的原则：**编译是纯函数。** 同一 `(shot revision, template revision, model snapshot, adapter revision)` 永远产出同一 `content_hash`。做不到这一点，"历史产物不自动重做"（docs/08 §3）就是空话——重编译会得到不同字节，你没法证明没变。

---

## 1. 关键区分（最容易写错的六处）

### 1.1 `TemplateRevision` 与 `CompiledPromptManifest`

两个东西经常被混为一谈，后果是"改模板把历史产物改坏"。

| | `TemplateRevision` | `CompiledPromptManifest` |
|---|---|---|
| 是什么 | 一份**可复用的**渲染规则与约束 | 一次**具体的**编译结果 |
| 作用域 | `(tenant, template_id, revision)` | `(tenant, shot_revision, template_revision, model_snapshot, adapter)` |
| 变不变 | 发布后不可变，新改动 = 新 revision | 内容寻址，永不修改 |
| 谁读 | S20 自己、S25（兼容性）、S19（system 模板） | S25/S26（生成命令的 `template_ref`）、S29/S31/S33/S34（模态执行） |
| 数量级 | 每个模态 × 供应商 × 模型快照 × 镜头类型一份，几十到几百 | 每个镜头每次尝试一份，几十万到几百万 |

`CompiledPromptManifest` **引用** `TemplateRevision`，但**不包含**它的可变部分。模板里写"人物描述按 `characters[]` 顺序展开"，manifest 里就存展开后的实际字节 + 模板 revision 引用。这样：
- 改模板只影响新编译（§3.7）；
- 历史 manifest 按自己的字节解释，永远可复现；
- S26 的 `template_ref` 指向 manifest，不是模板——否则 S26 无法证明"这次提交用的是哪段文字"。

### 1.2 对白：编译期零文本变换

[S04 §1.2](S04-narrative.md) 明写："S20 编译时不许做任何文本变换"。这条是本节存在的全部理由。

**什么是文本变换**：改标点、加省略号、把"我不去"改成"我不想去"、把两句话合并、删掉语气词、为了塞进时长而缩短、翻译成英文再翻回来、给每句话加"（愤怒地）"这类舞台提示。

**允许做的事**：
- 把 `dialogue[].text` 原样放进 prompt 的对白段；
- 把 `dialogue[].speaker` 映射成该模型要求的说话人标记格式（`{"speaker":"林岚","text":"我不去"}` → `林岚: 我不去`）——**只加格式，不动内容**；
- 把 `emotion` 作为**独立字段**传给支持它的模型，而不是拼进对白文本；
- 引用发音词典（`pronunciation_lexicon`），但那是在 TTS 侧生效，不进 prompt 文本。

**强制手段**：manifest 里存 `dialogue_segment` 的逐段 `text_sha256`，与 ShotSpec 的 `dialogue[].text` 哈希逐一比对。任一不等 → 编译失败 `DIALOGUE_TEXT_DRIFT`（422），**不存在"修复它"的分支**。这条检查在 §3.5 渲染之后立即执行，不在最后做。

原句、发音词典、译文**分别版本化**（docs/08 §3）：

| 东西 | 版本在哪 | 谁改 |
|---|---|---|
| 原句 | S04 `script_revision` → S06 `ShotSpec.dialogue[].text` | S04 → S06 |
| 发音词典 | S31 的 `pronunciation_lexicon` revision | S31 |
| 译文 | S38 的 `dialogue_translation` revision | S38 |

S20 **不合并**这三者。它可以在 prompt 里同时引用（`text` + `lexicon_ref` + `translation_ref`），但每一份都带自己的 revision。把译文写进 prompt 顶替原句，是这一节禁止的最常见错误。

### 1.3 导演意图保留 vs. 补全

docs/08 §3："不能把'压迫感'解释为任意改变剧情，补全的导演解释需保留来源。"

`ShotSpec` 给的是语义标注：`framing: "medium_close"`、`camera_movement: "dolly_in_slow"`、`lighting.mood: "压迫"`、`emotion: "压抑克制"`。模板要把这些变成某些模型的自然语言或参数值。

规则：

| 允许 | 禁止 | 保留来源 |
|---|---|---|
| 把枚举值映射成模型的词表（`medium_close` → `medium close-up shot`） | 把 `压迫` 扩写成"她双眼通红，攥紧拳头" | 映射表在 `adapter_binding` 里，可查 |
| 用模板里**已发布**的短语补一个模型必需的修饰 | 根据情绪"推断"出剧情细节 | manifest 里记 `derived_from: [{path, value, phrase_id}]` |
| 省略可选字段并记 warning | 因为字段没有对应词汇就改写它 | §3.6 |

`derived_from` 是这节的落点。manifest 里每一条非直接映射的文本片段都必须能回指到 `(ShotSpec path, 原值, 模板里的 phrase_id)`。没有 `derived_from` 的自由文本 = 不可审计的补全，编译失败 `UNSOURCED_COMPLETION`（422）。

判据：**一个审片人能否从 manifest 反推回 ShotSpec。** 如果某段文字在 manifest 里出现但在 ShotSpec 里没有对应来源，也不能指回模板 phrase，那它就是模型编的——而 S20 里根本没有模型。

### 1.4 参考图顺序是内容，不是参数

`ShotSpec.reference_bindings` 给出一组 `(global_asset_id, role, order_hint)`。多数图像/视频模型对参考图顺序敏感：第 1 张是主参考，第 2 张是风格，后面的权重递减。顺序错了，身份就漂。

所以：

- **顺序是 manifest 的一部分**，进 `content_hash`。
- 顺序由 `adapter_binding.reference_order_rule` 决定，不由"AI 觉得哪个重要"决定。
- 同一份 ShotSpec + 同一 adapter 永远得到同一顺序（纯函数，§0）。
- 顺序变化必须在 §3.4 里能解释为"adapter 变了"或"绑定集变了"，不能是"这次跑的不一样"。

`role` 到位置的映射按 adapter 声明（例如某供应商要求 `character` 必须在 index 0，`style` 在最后）。适配不了（例如要求 `first_frame` 但绑定集里没有）→ 该候选被拒绝，不是"随便挑一张顶上"。

### 1.5 目标时长与供应商生成档位是两件事

docs/08 §3："目标4秒与供应商生成档位分开；剪辑裁切方案与成本在路由计划明确。"

- `ShotSpec.duration_seconds = 4.0` 是**创作目标**。
- 供应商只能生成 5s / 8s 档位。那么"4 秒"要么靠裁剪，要么靠变速，要么不可行。

S20 的责任是**把目标与档位都写进 manifest**，让 S25/S26 与剪辑去决定：

```jsonc
"duration": {
  "target_seconds": 4.0,
  "provider_tier_seconds": 5,
  "overrun_seconds": 1.0,
  "handling": "trim",              // trim | retime | unacceptable
  "trim_window": {"from": 0.0, "to": 4.0}
}
```

S20 **不**擅自把 4.0 改成 5 再往下走——那会让 S41 的 EDL 少 1 秒而没人知道。`handling` 取 `unacceptable` 时，编译仍然成功（manifest 是事实记录），但**这个候选不可用**，由 S25 的硬过滤拒绝（[S25 §2.2](S25-model-routing.md) 的 `DIALOGUE_EXCEEDS_DURATION` 是同一族的判断）。

### 1.6 编译必须是可复现的纯函数

输入的每一项都比对一遍：

| 输入 | 形态 | 变了会怎样 |
|---|---|---|
| `shot_revision` + `content_hash` | 不可变 | 新 manifest |
| `template_revision` + `content_hash` | 不可变 | 新 manifest |
| `model_snapshot` | S24 的字符串快照（含日期） | 新 manifest |
| `adapter_binding.content_hash` | 不可变 | 新 manifest |
| `capability_snapshot_ref` | S24 的 revision + hash | 新 manifest |
| 环境（时区、区域、语言） | **不得影响输出** | 若影响 = 缺陷 |

最后一行是硬约束。渲染不得使用 `now()`、随机数、`locale` 默认值、哈希表迭代顺序。时间戳只出现在 manifest 的元数据里，**不进 `content_hash`**。

测试手段（§9.1）：同输入跑 100 次、跨两个区域、跨两个进程，`content_hash` 必须完全一致。

---

## 2. 输入契约

### 2.1 传输与身份

内部 mTLS + workload identity，HTTP/JSON。写命令带 [execution-context.v2](../contracts/execution-context.v2.schema.json) 的必填字段。`tenant_id` 来自 TCT，不接受 body 自授。

S20 是少数几个可以在**没有 run 上下文**时被调用的服务（模板管理、离线回归），因此 `execution-context` 的两个字段需要特别说明：

- `node_id` / `node_epoch` / `execution_fence`：**编译时**必填（编译一定属于某个节点、某个 epoch）；**模板管理**时可空。
- 这条分裂是 §11 C2 记录的契约缺口：`execution-context.v2` 目前全部字段都是 `required`。

### 2.2 命令清单

| 方法 | 路径 | 作用 | 需要权限 |
|---|---|---|---|
| `POST` | `/prompt-compilations` | 编译一次，产出 manifest | `creative.prompt.compile` |
| `POST` | `/templates` | 创建模板容器 | `creative.template.manage` |
| `POST` | `/templates/{id}/revisions` | 发布一版模板（需回归通过） | `creative.template.publish` |
| `POST` | `/templates/{id}/revisions/{rev}:retire` | 退役（新编译不可用，历史仍可读） | `creative.template.publish` |

> catalog 只声明 `POST /prompt-compilations`。其余路径见 §11 C3。

### 2.3 查询清单

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/templates/{id}/revisions/{revision}` | 一版模板（docs/08 §3 要求的 `compatible_schema`、adapter 范围、tested snapshots、回归报告、作者、发布时间） |
| `GET` | `/templates/{id}/revisions` | 该模板的版本列表与状态 |
| `GET` | `/prompt-compilations/{id}` | 一次编译的状态与 manifest 引用 |
| `GET` | `/templates/{id}/revisions/{rev}/regression-report` | 回归结果（§4.4） |
| `GET` | `/prompt-compilations:probe` | 候选兼容性探测（S25 用，§2.6） |

### 2.4 `POST /prompt-compilations`

```jsonc
{
  "target": {
    "template_ref":   {"id":"tpl_shot_video","revision":12,"content_hash":"..."},
    "shot_ref":       {"service_id":"S06","id":"shot_3_2","revision":5,"content_hash":"..."},
    "model_snapshot": "seedance-1.0-pro@2026-10",
    "adapter_binding_ref": {"id":"adb_volcengine_seedance_video","revision":3,"content_hash":"..."},
    "capability_snapshot_ref": {"service_id":"S24","id":"cap_...","revision":7,"content_hash":"..."}
  },
  "binding_set_ref": {"service_id":"S10","id":"bs_...","revision":12,"content_hash":"..."},
  "dialogue_alignment_ref": {"service_id":"S37","id":"aln_...","revision":2,"content_hash":"..."},
  "tones": {"pace":"slow", "intensity":0.6},
  "options": {"allow_optional_omission": true, "strict_capabilities": true}
}
```

- `shot_ref` 的 `content_hash` 必须与 S06 当前值一致，否则 409 `SHOT_REVISION_MOVED`（乐观并发，不是失败）。
- `template_ref` 必须 `PUBLISHED` 且未 `RETIRED` → `RETIRED` 时 422 `TEMPLATE_RETIRED`（历史 manifest 仍可读，只是不能再编译新的）。
- `adapter_binding_ref` 必须属于 `(template, model_snapshot)` 的合法组合 → 否则 422 `ADAPTER_NOT_APPLICABLE`。
- `dialogue_alignment_ref` 只在镜头有对白且 `audio_strategy='native_locked_voice'` 时必填；缺失 → 422 `DIALOGUE_ALIGNMENT_REQUIRED`。这条接 [S25 §2.2](S25-model-routing.md) 的"不估对白时长"。
- `binding_set_ref` 是参考图与身份的来源。**S20 不解析资产本体**——它只拿 `S10` 的绑定集（含 `global_asset_id` → 具体 artifact 的映射），版本钉死。
- `options.allow_optional_omission=false` 时，任何"可选字段无对应能力"的情况都变成硬失败（§3.6）。

### 2.5 模板发布

```jsonc
{
  "parent_revision": 11,
  "compatible_schema": {"id":"shot-spec","revision":3,"content_hash":"..."},
  "modality": "video",
  "provider": "volcengine",
  "model_snapshots": ["seedance-1.0-pro@2026-10"],
  "adapter_scopes": ["adb_volcengine_seedance_video"],
  "body": {
    "sections": [
      {"id":"subject", "from":"characters[*].appearance_summary", "order":"as_is"},
      {"id":"camera",  "from":"camera", "render":"phrase_table"},
      {"id":"lighting","from":"lighting", "render":"phrase_table", "optional":true},
      {"id":"dialogue","from":"dialogue", "render":"verbatim"}
    ],
    "negative": ["低分辨率","多余手指"],
    "max_chars": 1200
  },
  "tested_model_snapshots": ["seedance-1.0-pro@2026-10"],
  "regression_set_ref": {"id":"reg_video_v3","revision":4,"content_hash":"..."}
}
```

`body.render` 只允许四个值，这是刻意的限制：

| `render` | 语义 |
|---|---|
| `verbatim` | 原样拼接，**逐字节**（对白唯一允许的方式） |
| `phrase_table` | 查 adapter 的词表做枚举→短语映射，缺失即报错 |
| `template_string` | 用受限模板语法（§3.5），变量只能引用声明过的路径 |
| `omit` | 不输出该段 |

**没有 `llm_rewrite`。** 任何形式的"让模型润色这段 prompt"都不在模板语言里。这不是遗漏——加上它就是给编译器开了一条改写事实的后门。

### 2.6 `GET /prompt-compilations:probe`（S25 的兼容性探测）

```jsonc
// 入参
{"template_ref": {...}, "model_snapshots": ["a@2026-10","b@2026-09"], "requirement_set_ref": {...}}
// 出参
{
  "results": [
    {"model_snapshot":"a@2026-10","verdict":"compatible","adapter_binding_ref":{...},
     "missing_required_capabilities":[]},
    {"model_snapshot":"b@2026-09","verdict":"incompatible","reason":"NO_ADAPTER_FOR_SNAPSHOT"},
    {"model_snapshot":"c@2026-08","verdict":"compatible_with_omissions",
     "omitted_optionals":["lighting.mood"]}
  ],
  "evaluated_at": "2026-11-05T09:00:00Z"
}
```

这是纯读、无写入、不产生 manifest。**它是 S20 与 S25 之间解环的唯一方式**：S25 需要"模板能不能服务这个候选"才能出路由决策，而完整编译需要"已经选定候选"。探测只回答兼容性，不产出可提交的字节。

`verdict` 三值不许压成布尔：`compatible_with_omissions` 意味着这个候选可用但会丢可选项，S25 可以据此调整质量或成本估算。

### 2.7 错误码

完整清单与触发条件见 §7（那里按失败分支组织，不重复列举）。状态码的划分原则：

| HTTP | 语义 | 典型 code | retryable |
|---|---|---|---|
| 400 | 请求体不符合 schema | `INVALID_SCHEMA` | 否 |
| 403 / 404 | 越权 / 不存在（跨租户一律 404，E28） | `PERMISSION_DENIED`、`NOT_FOUND` | 否 |
| 409 | 状态或版本冲突，**重发可能成功** | `SHOT_REVISION_MOVED`、`TEMPLATE_REVISION_CONFLICT`、`COMMAND_IN_PROGRESS`、`IDEMPOTENCY_KEY_REUSED` | 前三个是 |
| 422 | 请求**永远**不可行 | `REQUIRED_FIELD_UNSUPPORTED`、`DIALOGUE_TEXT_DRIFT`、`SCHEMA_INCOMPATIBLE`、`RENDER_LIMIT_EXCEEDED`、`REGRESSION_NOT_PASSED` 等 | 否 |
| 503 | 依赖不可用 | `CAPABILITY_SOURCE_UNAVAILABLE`、`BINDING_SOURCE_UNAVAILABLE` | 是（带 `Retry-After`） |

**409 与 422 的分界是本服务最要紧的一条**：改输入或改模板可能让 422 变成成功，但重发同一个请求不会。所以 422 不进重试队列。

### 2.8 幂等语义

```
request_hash = sha256( JCS( body 去掉 command_id 与 request_hash ) )
```

`(tenant_id, command_id)` 主键；`IN_PROGRESS` 同 hash → 409 可重试；`DONE` 同 hash → 回放首次响应并附 `replayed: true`；不同 hash → 409 `IDEMPOTENCY_KEY_REUSED`。30 天过期。

**第二层**：`manifest_input_once` 唯一索引（§5.3）。同一组输入重编译，命中既有 manifest 直接返回，不产生第二份。这让"重试"和"缓存命中"是同一件事。

**编译是纯函数，所以幂等可以强到这个程度**：同一输入组的 manifest 内容必然相同，唯一索引不会误伤。

---

## 3. 核心逻辑与状态机

### 3.1 七步编译顺序

docs/08 §3 定的顺序，逐步对应到实现：

```
① 校验 DSL        ShotSpec 符合模板声明的 compatible_schema
② 固定绑定        解析 S10 绑定集 → 具体 artifact + sha256，钉死版本
③ 路由候选适配    选定 adapter_binding，检查模板×快照×adapter 合法
④ 参考图顺序与 token 映射   按 adapter 规则排序，映射成该模型的引用编号
⑤ 渲染 Prompt     受限模板语法，确定性求值
⑥ 参数能力验证    必需字段必须在 S24 快照里；可选字段按策略省略并记 warning
⑦ 保存 CompiledPromptManifest   写库 + outbox
```

顺序不能交换。最典型的错误是把 ⑥ 提前到 ③：那样会在还没确定 adapter 的时候判断能力，得到的结论是"某个抽象模型的抽象能力"，与实际提交给供应商的东西无关。另一个常见错误是把 ④ 放到 ⑤ 之后：参考图编号在渲染时就写进文本，事后再排序会得到"文本说 A 是主参考、参数把 B 放 index 0"的分裂。

下面逐步展开。任何一步失败都**不写 manifest**，返回对应错误码。

### 3.2 步骤 ①–②：校验与固定绑定

**① DSL 校验**：`shot.content_hash` 与 `shot_ref` 一致；`ShotSpec.schema_version` 满足 `template.compatible_schema`；必需字段齐备（`framing`、`camera_movement`、`lighting`、`beats`、`dialogue` 至少为空数组）。

`beats` 必须是结构化动作节拍（[S06 §1.5](S06-storyboard.md)），若发现是散文段落 → 422 `SCHEMA_INCOMPATIBLE`，不做"尽力解析"。

**② 固定绑定**：

```python
def pin_bindings(binding_set_ref, shot):
    bs = s10.get_binding_set(binding_set_ref)          # 必须 hash 一致
    pinned = []
    for ref in shot.reference_bindings:                # 顺序来自 ShotSpec
        hit = bs.resolve(ref.global_asset_id)
        if hit is None:
            raise ApiError(422, "REFERENCE_DANGLING")
        pinned.append({
            "global_asset_id": ref.global_asset_id,
            "role": ref.role,
            "order_hint": ref.order_hint,
            "artifact_ref": hit.artifact_ref,           # {service_id,id,revision,content_hash}
            "identity_constraint_ref": hit.identity_ref
        })
    return pinned
```

`artifact_ref.content_hash` 进 manifest 的 `content_hash`。这样"同一个镜头、同一份 ShotSpec、但角色图换了一版"会得到不同 manifest —— 正确，因为产物确实不同。

`identity_ref` 原样带过去，**S20 不判定身份是否达标**。身份一致性归 S35/S36。

### 3.3 步骤 ③：adapter 选择

一个 `adapter_binding` 描述"这个模板在这家供应商的这个模型快照上怎么落地"（字段见 §5.2）：`field_map`（语义字段 → 该模型的参数名/词表）、`reference_order_rule`（角色 → 位置）、`token_budget`、`unsupported_optionals`、`content_hash`。

选择规则（顺序固定）：

1. `(adapter_id, model_snapshot)` 有精确匹配 → 用它。
2. 否则有 `model_snapshot='*'` 的通配 → 用它，但记 warning `ADAPTER_FALLBACK`。
3. 否则 → 422 `NO_ADAPTER_FOR_SNAPSHOT`。

**没有"自动从相似模型推断"的分支。** 通配只是显式的降级声明，由 adapter 作者写进去，不是运行时猜的。

adapter 有自己的 revision，模板版本引用一个 `adapter_revision` **区间**（`adapter_revision_min/max`），这样换一个模型的映射不必重发模板。代价是 manifest 必须同时钉住 `template_revision` 与 `adapter_ref.revision`——已经这么做了（§2.4）。

### 3.4 步骤 ④：参考图顺序与 token 映射

```python
def order_references(pinned, rule):
    out = []
    for role, pos in rule.items():                    # 固定顺序的 dict，见下
        for ref in [r for r in pinned if r.role == role]:
            out.append((pos, ref))
    leftover = [r for r in pinned if r.role not in rule]
    if leftover and not rule.get("_allow_leftover", False):
        raise ApiError(422, "REFERENCE_ORDER_UNRESOLVABLE")
    out.sort(key=lambda p: (p[0], p[1].order_hint, p[1].global_asset_id))
    return [r for _, r in out]
```

三条实现约束：

1. **`rule.items()` 的迭代顺序必须稳定**。Python 3.7+ 保证插入序，但 JSON 反序列化后的 dict 依赖源文件字节顺序。因此 adapter 的 `rule` 存成**有序数组** `[{role, position}]`，不用 JSON object。这是 §1.6 纯函数要求的具体落点，也是最容易在实现时踩的坑。
2. **同角色的多张图**（例如两个角色的参考）按 `order_hint` 再按 `global_asset_id` 排序。`global_asset_id` 作最后一级，保证确定性；不用 `artifact_ref`（它可能随绑定集变）。
3. **`_allow_leftover`** 默认 `false`。允许时，剩余图放在最后并记 warning `UNMAPPED_REFERENCE_APPENDED`——这比静默丢弃安全，但仍要人看见。

token 映射：参考图在 prompt 里通常以占位符出现（`<image_1>`、`@ref:character_0`），具体语法由 adapter 的 `reference_token_format` 决定。**占位符的编号就是顺序位置**，不是 `global_asset_id` 的哈希。这条防止"顺序变了但占位符没变"。

### 3.5 步骤 ⑤：渲染（受限模板语法）

模板语言只支持六件东西：

| 语法 | 例 | 语义 |
|---|---|---|
| 路径引用 | `{{ characters[0].appearance_summary }}` | 取值，路径必须在 `body.sections[].from` 声明过 |
| 枚举映射 | `{{ framing \| phrase_table }}` | 查 adapter 词表 |
| 原样块 | `{{#verbatim dialogue}}…{{/verbatim}}` | 逐字节输出，禁任何内层插值 |
| 条件 | `{{#if lighting.mood}}…{{/if}}` | 只判断存在性，**不能做字符串比较** |
| 连接符 | `{{ join(characters, "，") }}` | 固定分隔符，来自模板 |
| 负向段 | `{{#negative}}…{{/negative}}` | 按模型要求的位置输出 |

**不支持的**：函数调用、算术、正则、字符串拼接/截断/替换、随机、时间、环境变量、任何 I/O。

`{{ join }}` 的第二个参数必须是模板里的字面量，不能来自数据——否则用户数据里的分隔符就能改变结构。

渲染完成后立刻做三件事：

1. `max_chars` / `token_budget` 检查 → 422 `RENDER_LIMIT_EXCEEDED`；
2. `verbatim` 段的逐段 `text_sha256` 比对 → 422 `DIALOGUE_TEXT_DRIFT`；
3. 每个非映射片段必须有 `derived_from` → 422 `UNSOURCED_COMPLETION`。

第 3 项的具体做法：模板里每个 `phrase_id`（枚举映射的产物）登记在 adapter 的词表里；渲染时记录 `(输出区间 → phrase_id 或 verbatim 来源)`。渲染结束后任何未登记的区间都是"模板正文里的固定文字"——那是**作者写的**，记 `derived_from: {kind:"template_literal", template_revision, span}` 即可，属于有来源。

所以 `UNSOURCED_COMPLETION` 只会出现在一种情况：**模板正文里插了数据但没走上面六种语法**（例如实现者自己加了 `{{ shot.summary }}` 这种未声明路径）。这是实现缺陷的守门员，不是业务校验。

### 3.6 步骤 ⑥：参数能力验证

`capability_snapshot_ref` 来自 S24（`CapabilitySnapshot`），声明该 `model_snapshot` 支持哪些参数与取值范围。

对每个**已渲染并使用**的字段：

| 情况 | `strict_capabilities=true` | `strict_capabilities=false` |
|---|---|---|
| 必需字段不支持 | 422 `REQUIRED_FIELD_UNSUPPORTED` | 同样 422（必需字段不许降级） |
| 可选字段不支持，`allow_optional_omission=true` | 省略 + warning `OPTIONAL_OMITTED` | 同上 |
| 可选字段不支持，`allow_optional_omission=false` | 422 `REQUIRED_FIELD_UNSUPPORTED` | — |
| 参数值超出范围 | 422 `CAPABILITY_RANGE_VIOLATION` | 422（不裁剪） |

**不许裁剪。** `intensity: 0.9` 而模型只接受 `[0, 0.8]` 时，不能静默改成 0.8——那改变了导演意图且没人知道。要么拒绝，要么由模板用 `phrase_table` 映射到该模型的分档词汇。

warning 必须进 manifest 的 `warnings[]`，且 `warnings` 进 `content_hash`（它是内容的一部分：两个 manifest 一个带 warning 一个不带，不是同一份产物）。

### 3.7 步骤 ⑦：保存与状态机

编译本身没有长状态机——它是同步的，从 `ACCEPTED` 到 `COMPILED` / `FAILED` 在一次请求内完成。真正的状态机在**模板版本**上：

```
DRAFT ──publish(回归通过)──► PUBLISHED ──retire──► RETIRED
  │                              │
  └──publish(回归失败)──► 422     └── 已有 manifest 仍可读
```

| 迁移 | 触发 | 守卫 |
|---|---|---|
| `DRAFT→PUBLISHED` | `POST .../revisions` | 回归集全部通过（§4.4）；`compatible_schema` 存在 |
| `PUBLISHED→RETIRED` | `:retire` | 无未完成编译（在途编译可用旧版完成） |
| `DRAFT→` 任何 | — | DRAFT 不可被编译引用（422 `TEMPLATE_NOT_PUBLISHED`） |

`RETIRED` 之后：`POST /prompt-compilations` 引用它 → 422 `TEMPLATE_RETIRED`；`GET` 仍可读；已有 manifest 不受影响（docs/08 §3："灰度和回退影响新请求，历史产物不自动重做"）。

### 3.8 不变量

1. `compiled_prompt_manifest` 一旦写入不可变。
2. `content_hash` 只由内容决定，不含时间戳、不含 `compiled_at`、不含 `warnings` 之外的运行信息（`warnings` 是内容，见 §3.6）。
3. 同一 `(shot_revision, template_revision, model_snapshot, adapter_binding_revision, binding_set_revision)` 只能有一份 manifest。
4. manifest 引用的所有 ref 的 `content_hash` 都必须与库中当前值一致——不一致说明有人改了不可变对象，属 P0。
5. `dialogue_segments[].text_sha256` 必须与 ShotSpec 对应段逐一相等。
6. 每个非映射输出的区间都有 `derived_from`。
7. `template_revision.state='PUBLISHED'` 是编译的前置条件，且编译期间不检查第二次（不引入"编译到一半模板被退役"的窗口）。

### 3.9 S20 与 `execution_fence` / `node_epoch`

S20 **不产生也不推进**这两个值。它读取它们用于打标：

- manifest 里存 `node_id`、`node_epoch`、`execution_fence`（来自 `execution_context`）；
- 这是为了让 S26 在派单前能判断"这个 manifest 是不是当前 epoch 的"（[S26 §4 步骤 4](S26-generation-jobs.md) 的实时复查）。

一个容易写错的实现：把 `node_epoch` 放进 `content_hash`。**不要**——那样同一份内容在两个 epoch 下会得到两份 manifest，"按内容缓存"失效，磁盘会被同一份 prompt 的十几个副本吃掉。epoch 是**使用上下文**，不是内容。

模板管理类命令没有节点上下文，这三个字段为空（§2.1 与 §11 C2）。

---

## 4. 输出契约

### 4.1 `CompiledPromptManifest`

```jsonc
{
  "schema_version": "2.0",
  "manifest_id": "cpm_01J...",
  "revision": 1,
  "tenant_id": "tnt_...",
  "modality": "video",
  "shot_ref": {"service_id":"S06","id":"shot_3_2","revision":5,"content_hash":"..."},
  "template_ref": {"id":"tpl_shot_video","revision":12,"content_hash":"..."},
  "adapter_ref": {"id":"adb_volcengine_seedance_video","revision":3,"content_hash":"..."},
  "model_snapshot": "seedance-1.0-pro@2026-10",
  "capability_snapshot_ref": {"service_id":"S24","id":"cap_...","revision":7,"content_hash":"..."},
  "binding_set_ref": {"service_id":"S10","id":"bs_...","revision":12,"content_hash":"..."},

  "prompt_text": "中近景，缓慢推镜，室内，黄昏侧逆光。林岚站在门口。\n林岚: 我不去",
  "negative_prompt": "低分辨率，多余手指",
  "tokens_estimate": 312,

  "references": [
    {"position":0,"role":"character","global_asset_id":"gas_linlan",
     "artifact_ref":{"service_id":"S11","id":"art_...","revision":3,"content_hash":"..."},
     "token":"<image_1>"},
    {"position":1,"role":"scene","global_asset_id":"gas_apartment",
     "artifact_ref":{"service_id":"S13","id":"art_...","revision":1,"content_hash":"..."},
     "token":"<image_2>"}
  ],
  "dialogue_segments": [
    {"line_id":"ln_88","speaker":"林岚","text":"我不去","text_sha256":"...",
     "emotion":"压抑克制","render":"verbatim"}
  ],
  "duration": {"target_seconds":4.0,"provider_tier_seconds":5,"overrun_seconds":1.0,
               "handling":"trim","trim_window":{"from":0.0,"to":4.0}},
  "parameters": {"aspect_ratio":"9:16","resolution":{"width":1080,"height":1920},
                 "audio_strategy":"silent_then_tts","motion_strength":0.6},

  "derived_from": [
    {"span":[0,6],"kind":"phrase_table","path":"/framing","value":"medium_close","phrase_id":"ph_mcu_cn"},
    {"span":[0,6],"kind":"template_literal","template_revision":12},
    {"span":[53,56],"kind":"verbatim","path":"/dialogue/0/text","line_id":"ln_88"}
  ],
  "warnings": [],
  "content_hash": "sha256:...",
  "compiled_at": "2026-11-05T09:20:03Z"
}
```

`content_hash` 覆盖：`shot_ref.content_hash`、`template_ref.content_hash`、`adapter_ref.content_hash`、`model_snapshot`、`capability_snapshot_ref.content_hash`、`binding_set_ref.content_hash`、`prompt_text`、`negative_prompt`、`references[]`（含 position 与 token）、`dialogue_segments[]`（含 `text_sha256`）、`duration`、`parameters`、`derived_from`、`warnings`。

**不覆盖**：`manifest_id`、`revision`、`compiled_at`、`node_id`、`node_epoch`、`execution_fence`、`tokens_estimate`（估算可能随 tokenizer 版本变）。

`tokens_estimate` 不进 hash 是有意的：tokenizer 升级后重算会得到不同数字，但那不改变产物内容。它的用途只是 `max_chars`/预算的粗筛。

### 4.2 Prompt 文本的四条硬约束

1. **对白逐字节**。`dialogue_segments` 里每一段的 `text` 与 ShotSpec 对应 `dialogue[].text` 完全相同，允许的唯一差别是说话人前缀的格式（由 adapter 决定，且前缀不在这段 `text` 里）。
2. **无未溯源文本**。`derived_from` 必须覆盖 `prompt_text` 的全部字符区间（允许区间之间只有模板字面量，且模板字面量也登记）。
3. **敏感内容不内联**。身份约束、发音词典、音色样本都只以 ref 形式出现在 `parameters` 里，prompt 文本中没有它们的原文。
4. **不出现租户标识**。`prompt_text` 里不得出现 `tenant_id`、`project_id`；出现即 422 `OUTPUT_CONTAINS_FORBIDDEN_FIELD`（与 [S19 §4.2](S19-agent-runtime.md) 同一条）。

### 4.3 `prompt.compiled` 事件

topic `drama.prompt-compiler.v2`，`aggregate_id = manifest_id`，`aggregate_sequence` 恒为 `1`（每份 manifest 是独立聚合，且只发布一次，见 §6.4），`partition_key = tenant_id:aggregate_id`。

```jsonc
{
  "specversion":"1.0","schema_version":"2.0",
  "id":"evt_...","source":"S20","type":"prompt.compiled",
  "subject":"compiled-prompt/cpm_01J...",
  "time":"2026-11-05T09:20:03Z",
  "tenant_id":"tnt_...","home_cell":"cell_a1",
  "aggregate_id":"cpm_01J...","aggregate_sequence":1,
  "correlation_id":"cmd_...","causation_id":"cmd_...",
  "traceparent":"00-4bf92f35-...-01",
  "data":{
    "manifest_ref":{"id":"cpm_01J...","revision":1,"content_hash":"sha256:..."},
    "shot_ref":{"service_id":"S06","id":"shot_3_2","revision":5,"content_hash":"..."},
    "template_ref":{"id":"tpl_shot_video","revision":12,"content_hash":"..."},
    "model_snapshot":"seedance-1.0-pro@2026-10",
    "warnings":[],
    "node_id":"shot_3_2.video","node_epoch":4,"execution_fence":2
  },
  "data_ref":{"service_id":"S20","id":"cpm_01J...","revision":1,"content_hash":"sha256:..."}
}
```

**事件不带 `prompt_text`。** 它是剧情内容 + 客户资产描述，进 Kafka 就等于进了 14 天的多副本留存（E31 数据驻留风险）。消费者需要正文时用 `data_ref` 去 S20 取，取时仍走租户校验。

消费者：S25（缓存失效）、S26（可选：预编译校验）、S47（投影）。**S29/S31/S33/S34 不消费**——它们的输入是生成命令里的 `template_ref`（指向 manifest），由 S26 派单时给出。

### 4.4 回归报告（模板发布的门）

docs/08 §3 要求模板含"回归报告"。结构：

```jsonc
{
  "template_ref": {"id":"tpl_shot_video","revision":12,"content_hash":"..."},
  "regression_set_ref": {"id":"reg_video_v3","revision":4,"content_hash":"..."},
  "started_at":"...", "finished_at":"...",
  "cases": [
    {"case_id":"rc_001","label":"中近景+侧逆光+单人对白",
     "assertions":[
       {"name":"dialogue_byte_identical","result":"pass"},
       {"name":"reference_order","result":"pass","detail":"character@0, scene@1"},
       {"name":"intent_preserved","result":"pass"},
       {"name":"token_budget","result":"pass","detail":{"estimate":312,"budget":900}}
     ]},
    {"case_id":"rc_007","label":"双人同框+无场景图",
     "assertions":[{"name":"reference_order","result":"fail",
                    "detail":"adapter 要求 scene@1，绑定集只有 character"}]}
  ],
  "verdict": "fail",
  "summary": {"total":12,"passed":11,"failed":1}
}
```

三条规则：

1. **回归是断言集合，不是"看一遍觉得还行"。** 每个 case 的断言必须是可机器判定的。`intent_preserved` 的判定方式是：`derived_from` 覆盖完整 + 所有枚举映射命中词表 + 无 `UNSOURCED_COMPLETION`（§3.5 第 3 项）。它不是人工评分，所以能进门。
2. **`verdict: "fail"` 阻塞发布**（§3.7）。
3. **回归集本身版本化**（`regression_set_ref`），且它的 `content_hash` 进 `template_revision.content_hash`。换回归集 = 新模板版本。否则"改了考题再声称通过"是可能的。

回归集里**不含**付费调用。全部走确定性渲染与静态断言。需要真实出图判断"视觉效果"的部分属于 S35/S44，不属于模板门。

---

## 5. 数据模型

schema `prompt_compiler`，独立库，单独账号。除本服务外无人可写。跨库只读一律走 API。

### 5.1 `template` 与 `template_revision`

```sql
CREATE TABLE prompt_compiler.template (
  tenant_id            text        NOT NULL,
  template_id          text        NOT NULL,
  name                 text        NOT NULL,
  modality             text        NOT NULL,       -- image | video | audio | tts | ...
  provider             text        NOT NULL,
  current_revision     integer     NOT NULL DEFAULT 0,
  state                text        NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | ARCHIVED
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, template_id),
  CHECK (modality IN ('image','video','audio','tts','music','sfx','lipsync','upscale','edit'))
);

CREATE TABLE prompt_compiler.template_revision (
  tenant_id            text        NOT NULL,
  template_id          text        NOT NULL,
  revision             integer     NOT NULL,
  state                text        NOT NULL,       -- DRAFT | PUBLISHED | RETIRED
  compatible_schema    jsonb       NOT NULL,       -- {id, revision, content_hash}
  body                 jsonb       NOT NULL,       -- sections / negative / max_chars
  adapter_revision_min integer     NOT NULL,
  adapter_revision_max integer     NOT NULL,
  model_snapshots      text[]      NOT NULL,
  regression_set_ref   jsonb       NOT NULL,
  regression_verdict   text        NOT NULL DEFAULT 'pending',  -- pending|pass|fail
  content_hash         bytea       NOT NULL,       -- sha256 原始 32 字节
  author_subject       text        NOT NULL,
  published_at         timestamptz,
  retired_at           timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, template_id, revision),
  FOREIGN KEY (tenant_id, template_id)
    REFERENCES prompt_compiler.template (tenant_id, template_id),
  CHECK (state IN ('DRAFT','PUBLISHED','RETIRED')),
  CHECK (regression_verdict IN ('pending','pass','fail')),
  CHECK (adapter_revision_max >= adapter_revision_min),
  CHECK ((state = 'PUBLISHED') = (published_at IS NOT NULL)),
  CHECK (state <> 'RETIRED' OR retired_at IS NOT NULL)
);

-- 同一模板至多一版 DRAFT：发布前只能有一个在改的版本
CREATE UNIQUE INDEX template_revision_one_draft
  ON prompt_compiler.template_revision (tenant_id, template_id)
  WHERE state = 'DRAFT';

CREATE INDEX template_revision_published
  ON prompt_compiler.template_revision (tenant_id, template_id, published_at DESC)
  WHERE state = 'PUBLISHED';
```

`current_revision` 与 `template_revision` 的最大值之间的差额由发布事务维护（`UPDATE ... SET current_revision = GREATEST(current_revision, :rev)`），不用触发器——触发器在同一事务里跑，回滚时一起回滚，但读路径更容易被误用。

`content_hash` 覆盖 `compatible_schema`、`body`、`adapter_revision_min/max`、`model_snapshots`（排序后）、`regression_set_ref.content_hash`。**不含** `state`、`published_at`、`author_subject`——同类里的 `model_snapshots` 必须排序，否则数组顺序会进 hash。

### 5.2 `adapter_binding`

```sql
CREATE TABLE prompt_compiler.adapter_binding (
  tenant_id              text        NOT NULL,
  adapter_id             text        NOT NULL,
  revision               integer     NOT NULL,
  provider               text        NOT NULL,
  model_snapshot         text        NOT NULL,   -- 精确快照；'*' 表示通配
  field_map              jsonb       NOT NULL,   -- 语义字段 → 参数名或词表
  reference_order_rule   jsonb       NOT NULL,   -- [{role, position}] 有序数组
  reference_token_format text        NOT NULL,   -- '<image_{n}>'
  token_budget           integer     NOT NULL,
  unsupported_optionals  text[]      NOT NULL DEFAULT '{}',
  content_hash           bytea       NOT NULL,
  created_at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, adapter_id, revision),
  CHECK (token_budget > 0),
  -- 通配只能用 '*'，不接受 'seedance-*' 之类的模式
  CHECK (model_snapshot = '*' OR model_snapshot ~ '^[a-z0-9.-]+@[0-9]{4}-[0-9]{2}$')
);

-- 精确快照优先于通配：查询按 model_snapshot <> '*' 排序
CREATE UNIQUE INDEX adapter_binding_exact
  ON prompt_compiler.adapter_binding (tenant_id, provider, model_snapshot, adapter_id)
  WHERE model_snapshot <> '*';

CREATE UNIQUE INDEX adapter_binding_wildcard
  ON prompt_compiler.adapter_binding (tenant_id, provider, adapter_id)
  WHERE model_snapshot = '*';
```

`reference_order_rule` 存**有序数组**而不是 JSON object，理由在 §3.4 第 1 条：object 的键顺序在反序列化后不可依赖，而位置映射必须确定性。

`CHECK` 里对 `model_snapshot` 的正则约束是刻意的：把"模糊匹配"关在门外。通配只有一种写法，语义只有一种解释。

### 5.3 `compiled_prompt_manifest`

```sql
CREATE TABLE prompt_compiler.compiled_prompt_manifest (
  tenant_id                 text        NOT NULL,
  manifest_id               text        NOT NULL,
  revision                  integer     NOT NULL DEFAULT 1,
  modality                  text        NOT NULL,

  shot_ref                  jsonb       NOT NULL,   -- {service_id,id,revision,content_hash}
  template_ref              jsonb       NOT NULL,
  adapter_ref               jsonb       NOT NULL,
  model_snapshot            text        NOT NULL,
  capability_snapshot_ref   jsonb       NOT NULL,
  binding_set_ref           jsonb       NOT NULL,
  dialogue_alignment_ref    jsonb,

  prompt_text               text        NOT NULL,
  negative_prompt           text,
  tokens_estimate           integer,
  references                jsonb       NOT NULL,   -- 有序，含 position/token
  dialogue_segments         jsonb       NOT NULL,
  duration                  jsonb       NOT NULL,
  parameters                jsonb       NOT NULL,
  derived_from              jsonb       NOT NULL,
  warnings                  jsonb       NOT NULL DEFAULT '[]'::jsonb,

  node_id                   text,
  node_epoch                bigint,
  execution_fence           bigint,

  content_hash              bytea       NOT NULL,
  compiled_at               timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, manifest_id)
);

-- 同一输入组只有一份 manifest（§2.8 第二层幂等）
CREATE UNIQUE INDEX manifest_input_once
  ON prompt_compiler.compiled_prompt_manifest (
    tenant_id,
    (shot_ref->>'id'),
    (template_ref->>'id'),
    (template_ref->>'revision'),
    model_snapshot,
    (adapter_ref->>'revision'),
    binding_set_ref->>'content_hash',
    content_hash
  );

CREATE INDEX manifest_by_shot
  ON prompt_compiler.compiled_prompt_manifest (tenant_id, (shot_ref->>'id'), compiled_at DESC);
```

`manifest_input_once` 用 `jsonb` 表达式而不是独立列，是为了不把 ref 的形状展开成十几个列——ref 是契约对象，展开会让每次契约加字段都要改 DDL。代价是索引更大、写放大更高；退路是把 `shot_id`/`template_id`/`template_revision`/`adapter_revision` 物化成 `GENERATED ALWAYS AS ... STORED` 列再建索引，**现在不做，没量**。

`prompt_text` 与 `dialogue_segments` 属于客户内容，进库前字段级加密（KMS，按 `tenant_id` 分密钥），密钥不落地在本服务。这是 E31 的前置条件，也是 §11 C7 的一部分。

### 5.4 `compile_regression_result`

```sql
CREATE TABLE prompt_compiler.compile_regression_result (
  tenant_id          text        NOT NULL,
  result_id          text        NOT NULL,
  template_id        text        NOT NULL,
  template_revision  integer     NOT NULL,
  regression_set_ref jsonb       NOT NULL,
  verdict            text        NOT NULL,       -- pass | fail
  case_total         integer     NOT NULL,
  case_passed        integer     NOT NULL,
  case_failed        integer     NOT NULL,
  report             jsonb       NOT NULL,       -- §4.4 全文
  started_at         timestamptz NOT NULL,
  finished_at        timestamptz NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, result_id),
  FOREIGN KEY (tenant_id, template_id, template_revision)
    REFERENCES prompt_compiler.template_revision (tenant_id, template_id, revision),
  CHECK (verdict IN ('pass','fail')),
  CHECK (case_passed + case_failed = case_total)
);

-- 一次发布对应唯一一次判定；重跑产生新 result，发布取最新
CREATE INDEX regression_by_template
  ON prompt_compiler.compile_regression_result
     (tenant_id, template_id, template_revision, created_at DESC);
```

`report` 存全文（§4.4 的结构），因为它是审计证据。不在发布后修改；重跑就是新行。

### 5.5 `idempotency_record`

```sql
CREATE TABLE prompt_compiler.idempotency_record (
  tenant_id      text        NOT NULL,
  command_id     text        NOT NULL,
  request_hash   bytea       NOT NULL,
  state          text        NOT NULL,        -- IN_PROGRESS | DONE
  status_code    integer,
  response_body  jsonb,
  response_headers jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, command_id),
  CHECK (state IN ('IN_PROGRESS','DONE')),
  CHECK (state <> 'DONE' OR status_code IS NOT NULL)
);

CREATE INDEX idempotency_expiry
  ON prompt_compiler.idempotency_record (expires_at);
```

`request_hash` 存 `bytea` 不存 hex 字符串：省一半空间，且比较更快。配对写在同一事务里（§6.1）。

### 5.6 outbox

```sql
CREATE TABLE prompt_compiler.outbox (
  id                 bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          text        NOT NULL,
  aggregate_id       text        NOT NULL,   -- 本服务固定装 manifest_id
  aggregate_sequence bigint      NOT NULL,   -- 恒为 1
  event_type         text        NOT NULL,   -- 目前只有 'prompt.compiled'
  partition_key      text        NOT NULL,   -- tenant_id || ':' || aggregate_id
  payload            jsonb       NOT NULL,
  headers            jsonb       NOT NULL,   -- correlation_id / causation_id / traceparent
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);

CREATE INDEX outbox_unpublished_idx
  ON prompt_compiler.outbox (id) WHERE published_at IS NULL;

CREATE INDEX outbox_partition_pending
  ON prompt_compiler.outbox (partition_key, id) WHERE published_at IS NULL;

CREATE INDEX outbox_archive_idx
  ON prompt_compiler.outbox (published_at) WHERE published_at IS NOT NULL;
```

形状与 [S08 §6.4](S08-revision-snapshot.md) 一致。三点说明：

1. **`aggregate_sequence` 恒为 1。** manifest 是不可变对象、只发布一次；同一 `aggregate_id` 不会有第二个序号。`UNIQUE (aggregate_id, aggregate_sequence, event_type)` 因此同时承担"这份 manifest 只发一次事件"的保证。若将来要发 `prompt.compilation.failed` 之类，需要 `aggregate_kind` 列——见 §11 C6（与 [S19 §11 C6](S19-agent-runtime.md) 同一条缺口）。
2. **`partition_key` 是 `tenant_id || ':' || aggregate_id`**，与 docs/02 的分区约定一致。relay 单线程处理同一 partition_key，保证同一 manifest 的事件不会交错。
3. **归档**：`published_at + 7d` 后移到归档表，保留 14 天（与 Kafka 一致），之后删除。relay 只在未发布集合上工作，归档不影响它。

**订阅侧不建 inbox。** S20 在 v2 契约里不消费任何事件（见 §10.4）。如果后续要订阅 S24 的 `capability.snapshot.published` 做能力缓存预热，那时再加附件表，并补 inbox。

### 5.7 不建的表

| 表 | 为什么不做 |
|---|---|
| `prompt_text_cache` | manifest 本身就是缓存（内容寻址 + 唯一索引）。再建一层是第二个权威，且必然不同步 |
| `template_usage_stat` | 使用量从 S26 的派单记录读，S20 不复算 |
| `dialogue_copy` | 对白权威在 S04/S06。S20 里只存 `text_sha256` 与渲染后的 prompt 字节（已在 manifest 里） |
| `model_capability_copy` | 能力权威在 S24。拷贝一份就是第二个权威 |
| `asset_metadata` | 资产权威在 S10/S11/S13。S20 只存 `artifact_ref` |

---

## 6. 幂等与并发

### 6.1 幂等键

统一为 `(tenant_id, command_id)`，`request_hash = sha256(JCS(body \ {command_id, request_hash}))`。

处理流程（与 [S01 §6](S01-tenant-access.md)、[S06 §6](S06-storyboard.md) 同形）：

```
BEGIN
  INSERT INTO idempotency_record (tenant_id, command_id, request_hash, state, expires_at)
    VALUES (..., 'IN_PROGRESS', now() + interval '30 days')
    ON CONFLICT (tenant_id, command_id) DO NOTHING;
  -- 影响 0 行 → 已存在，读出后走 §2.8 的分支
  ...
  业务写入（manifest + outbox）
  UPDATE idempotency_record SET state='DONE', status_code=..., response_body=...
COMMIT
```

关键点：`INSERT ... ON CONFLICT DO NOTHING` 在同一事务里持锁，并发的第二个请求会阻塞到第一个提交，然后读到 `DONE` 直接回放。不会出现"两个请求都以为自己是首次"。

`IN_PROGRESS` 行超过 60 秒未推进（进程崩了）由后台任务清理——**只清理 `IN_PROGRESS`**，`DONE` 行永远保留到 30 天过期。清理窗口必须大于单次编译的最长耗时（§8 的 p99 × 安全系数），否则会把还在跑的编译的幂等记录删掉，导致第二次执行。当前设定：最长编译 30 秒，清理阈值 60 秒。

### 6.2 第二层：内容寻址

`manifest_input_once` 唯一索引（§5.3）。冲突时：

```sql
INSERT INTO compiled_prompt_manifest (...) VALUES (...)
ON CONFLICT DO NOTHING RETURNING manifest_id;
-- 无返回行 → SELECT 既有行，附 replayed: true 返回 200
```

**这不是幂等键的替代，是补充**：不同的 `command_id` 但相同输入（例如用户点了两次"重新编译"、或两个上游各自编译同一镜头）也会收敛到同一份 manifest。这层能做到，唯一前提是 §1.6 的纯函数性。

如果实现破坏了纯函数性（用了 `now()`、随机数、不稳定的 dict 顺序），这一层会让两个本应不同的 manifest 撞成一份——**那是缺陷的放大器，不是保险**。测试 T20-07 专门盯这条。

### 6.3 并发写：模板发布

`POST /templates/{id}/revisions` 用乐观并发：

```sql
UPDATE prompt_compiler.template
   SET current_revision = GREATEST(current_revision, :new_revision), updated_at = now()
 WHERE tenant_id = :tid AND template_id = :tpl AND current_revision = :parent_revision;
-- 影响 0 行 → 409 TEMPLATE_REVISION_CONFLICT，带当前 current_revision
```

配合 `template_revision_one_draft` 部分唯一索引：同一模板不能有两个 DRAFT 同时存在，即使两个请求各自都通过了乐观检查（不同 `parent_revision` 的时钟漂移场景），索引也会拦住第二个。

### 6.4 `aggregate_sequence` 的来源

`prompt.compiled` 的 `aggregate_sequence` 不需要序列号计算——manifest 不可变、只发一次，所以恒为 1，直接在插入 outbox 时写死。

这与 [S06](S06-storyboard.md) 那种会多次发事件的聚合不同：那里 `aggregate_sequence` 从业务表的 `publish_sequence` 列取。S20 没有这一列，也不该有——有它就等于承认"同一 manifest 可能发两次"。

### 6.5 竞态：编译期间的模板退役

`TEMPLATE_RETIRED` 只在**编译开始时**检查一次（§3.8 不变量 7）。编译进行中模板被退役，编译照常完成并写 manifest。

理由：退役的语义是"新请求不可用"，不是"抹掉正在做的事"。若中途失败，用户得到的是失败而不是半份东西——不会更糟。

反向的竞态：模板在编译中被**回滚**（`PUBLISHED→RETIRED→` 恢复为 `PUBLISHED`）。实现上不支持"取消退役"，只支持"发一版新 revision"，所以这个竞态不存在。

### 6.6 并发：同一 ShotSpec 被多个调用方编译

三个上游同时为 `shot_3_2` 编译（S19 的导演 Agent、用户的"重编译"按钮、批处理回填）。三者 `command_id` 不同、输入相同 → 第一份写入，后两份命中 `manifest_input_once`，全部返回同一 `manifest_id`，其中两份带 `replayed: true`。

**结果正确，但要观测。** §8 的 `compile_dedup_total` 指标按来源分开计数：如果某个来源的 dedup 率持续很高，说明上游在重复调用（大概率是它自己的幂等没做好），需要去查那个服务，而不是在 S20 加缓存。

### 6.7 幂等的边界：不覆盖"重新编译"

`replay` 与"重新编译"必须能区分：

| 用户意图 | 做法 | 结果 |
|---|---|---|
| 网络重试 | 同 `command_id` 重发 | 回放，`replayed: true` |
| 想再编译一次（比如怀疑缓存脏了） | 新 `command_id`，同输入 | 命中唯一索引，返回同一 manifest |
| 输入确实变了（新 shot_revision / 新模板） | 新 `command_id`，新输入 | 新 manifest |

第二行是设计后果：**用户"再编译一次"不会得到新字节**。这是对的——纯函数不该有这个能力。如果用户想改变结果，他该改的是输入（模板或 ShotSpec），那才是真正的原因所在。

如果确实需要"强制重算"（例如怀疑内容寻址实现有 bug），那是运维操作，走 `POST /prompt-compilations:force` 并写审计，不进常规 API。

---

## 7. 失败分支

| # | 触发 | 现象 | 处理 | 状态与后果 | 可重试 |
|---|---|---|---|---|---|
| F1 | `shot_ref.content_hash` 与 S06 当前值不一致 | 409 `SHOT_REVISION_MOVED` | 返回当前 ref | 无写入 | 是 |
| F2 | 模板 `DRAFT` | 422 `TEMPLATE_NOT_PUBLISHED` | 拒绝 | 无写入 | 否 |
| F3 | 模板 `RETIRED` | 422 `TEMPLATE_RETIRED` | 拒绝，提示发新 revision | 无写入 | 否 |
| F4 | `ShotSpec` schema 与 `compatible_schema` 不同 | 422 `SCHEMA_INCOMPATIBLE` | 拒绝，不做兼容解析 | 无写入 | 否 |
| F5 | `beats` 是散文而非结构化 | 422 `SCHEMA_INCOMPATIBLE` | 拒绝 | 无写入 | 否 |
| F6 | 绑定集里缺某个 `global_asset_id` | 422 `REFERENCE_DANGLING` | 拒绝 | 无写入 | 否（要改 S10） |
| F7 | 无精确 adapter 也无通配 | 422 `NO_ADAPTER_FOR_SNAPSHOT` | 拒绝 | 无写入 | 否 |
| F8 | 通配降级命中 | 200 + warning `ADAPTER_FALLBACK` | 正常产出，warning 进 hash | manifest 已写 | — |
| F9 | adapter 角色位置要求无法满足 | 422 `REFERENCE_ORDER_UNRESOLVABLE` | 拒绝 | 无写入 | 否（要改绑定集或 adapter） |
| F10 | 可选字段无能力且 `allow_optional_omission=true` | 200 + warning `OPTIONAL_OMITTED` | 省略并记 warning | manifest 已写 | — |
| F11 | 可选字段无能力且 `allow_optional_omission=false` | 422 `REQUIRED_FIELD_UNSUPPORTED` | 拒绝 | 无写入 | 否 |
| F12 | 必需字段无能力（任何选项） | 422 `REQUIRED_FIELD_UNSUPPORTED` | 拒绝 | 无写入 | 否 |
| F13 | 参数值超能力范围 | 422 `CAPABILITY_RANGE_VIOLATION` | 拒绝，不裁剪 | 无写入 | 否 |
| F14 | 渲染后超 `max_chars` 或 `token_budget` | 422 `RENDER_LIMIT_EXCEEDED` | 拒绝，返回实际值与上限 | 无写入 | 否（要改模板） |
| F15 | 对白渲染字节与原文不一致 | 422 `DIALOGUE_TEXT_DRIFT` | 拒绝，返回不一致的 `line_id` | 无写入 | 否（P0，说明模板有 bug） |
| F16 | 原生对白镜头缺对齐结果 | 422 `DIALOGUE_ALIGNMENT_REQUIRED` | 拒绝 | 无写入 | 否 |
| F17 | 出现未声明路径插值 | 422 `UNSOURCED_COMPLETION` | 拒绝 | 无写入 | 否（实现缺陷） |
| F18 | `prompt_text` 含 `tenant_id` / `project_id` | 422 `OUTPUT_CONTAINS_FORBIDDEN_FIELD` | 拒绝 | 无写入 | 否 |
| F19 | S24 能力快照不可用 | 503 `CAPABILITY_SOURCE_UNAVAILABLE` | 拒绝，带 `Retry-After` | 无写入 | 是 |
| F20 | S10 绑定集不可用 | 503 `BINDING_SOURCE_UNAVAILABLE` | 拒绝 | 无写入 | 是 |
| F21 | 同 `command_id` 不同 body | 409 `IDEMPOTENCY_KEY_REUSED` | 拒绝 | 无写入 | 否 |
| F22 | 同 `command_id` 执行中 | 409 `COMMAND_IN_PROGRESS` | 拒绝，带 `Retry-After: 2` | 无写入 | 是（隔一会） |
| F23 | 回归未通过 | 422 `REGRESSION_NOT_PASSED`（发布路径） | 拒绝发布，返回报告 | 版本仍 DRAFT | 否 |
| F24 | `parent_revision` 落后 | 409 `TEMPLATE_REVISION_CONFLICT` | 拒绝，带当前值 | 无写入 | 是 |
| F25 | outbox 写入失败 | 500 | 事务回滚，manifest 不写 | 无写入 | 是 |
| F26 | 字段级加密密钥不可用 | 503 `KMS_UNAVAILABLE` | 拒绝 | 无写入 | 是 |
| F27 | `adapter_revision` 超出模板声明的区间 | 422 `ADAPTER_NOT_APPLICABLE` | 拒绝 | 无写入 | 否 |

几条判断原则：

- **401/403/404 与 4xx 业务错误的区分**保持不变：跨租户访问一律 404（E28），不泄漏对象是否存在。
- **F8/F10 是唯一允许"带 warning 成功"的路径。** 其余所有降级都拒绝。理由是这两条对应的是明确的、模板作者已经在 `unsupported_optionals` 里声明过的情形；声明之外的一切降级都是隐藏的意图损失。
- **F15 是 P0。** 对白漂移意味着编译器改写了文本，这是 §1.2 明令禁止的。它的出现不可能是"输入不好"，只能是实现或模板有 bug。告警级别与 F17 相同，进 on-call 页面。
- **不重试上游不可用的退避由 S20 自己不做**——它是同步 API，调用方重试。S20 只负责返回 `Retry-After`。

---

## 8. 可观测

### 8.1 指标

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `s20_compile_requests_total` | counter | `tenant`,`modality`,`outcome` | 总量与成功率 |
| `s20_compile_duration_seconds` | histogram | `modality`,`model_snapshot` | 延迟（p50/p95/p99） |
| `s20_compile_step_duration_seconds` | histogram | `step`(1..7) | 定位慢在哪一步 |
| `s20_compile_dedup_total` | counter | `source`(s19\|user\|batch),`reason` | §6.6 的重复调用发现 |
| `s20_replay_total` | counter | `outcome`(same_hash\|diff_hash) | 幂等命中率 |
| `s20_compile_failures_total` | counter | `code`,`modality` | 按错误码分布 |
| `s20_dialogue_drift_total` | counter | `tenant`,`template_id` | **P0 告警源**，非零即告警 |
| `s20_unsourced_completion_total` | counter | `tenant`,`template_id` | 同上 |
| `s20_warnings_total` | counter | `warning_code` | 降级频率 |
| `s20_token_estimate_ratio` | histogram | `modality`,`model_snapshot` | 实际/上限，观察模板肥胖 |
| `s20_render_limit_hits_total` | counter | `tenant`,`template_id` | 模板该收紧了 |
| `s20_template_publish_total` | counter | `outcome`(pass\|fail) | 发布质量趋势 |
| `s20_regression_case_failures_total` | counter | `template_id`,`assertion` | 哪类断言最常失败 |
| `s20_outbox_lag_seconds` | gauge | `partition_key` | relay 落后 |
| `s20_outbox_unpublished` | gauge | — | 堆积量 |
| `s20_outbox_archive_backlog` | gauge | — | 归档任务落后 |
| `s20_capability_source_latency` | histogram | `outcome` | S24 依赖健康度 |
| `s20_manifest_bytes` | histogram | `modality` | 存储增长 |

`s20_compile_step_duration_seconds` 的 `step` 用七步的编号（§3.1），不用自由字符串——否则不同实现会写出七种不同拼法，指标不可聚合。

### 8.2 trace 维度

按 docs/design-package 的 S20 条目，trace 维度为：`tenant`、`series`、`episode`、`branch`、`manifest_revision`、`run`、`epoch`、`node`、`attempt`、`command`、`trace`。

S20 在编译时能提供的是 `tenant`、`series`、`episode`、`branch`（来自 `execution_context`）、`run`、`node`、`epoch`（`node_epoch`）、`command`（`command_id`）、`manifest_revision`（编译后回填到 span）。`attempt` 由 S26 侧提供；S20 的编译与"尝试"无关，span 上留空。

### 8.3 日志与审计

| 事件 | 记录字段 | 保留 |
|---|---|---|
| 编译成功 | `tenant`,`manifest_id`,`content_hash`,`shot_ref`,`template_ref`,`warnings`,`duration_ms` | 90 天 |
| 编译失败 | 上述 + `code`,`detail` | 90 天 |
| 模板发布 | `template_id`,`revision`,`author`,`regression_result_id`,`verdict` | 永久 |
| 模板退役 | `template_id`,`revision`,`actor`,`retired_at` | 永久 |
| adapter 变更 | `adapter_id`,`revision`,`diff_summary`,`actor` | 永久 |
| 越权尝试 | `tenant`,`subject`,`path`,`verdict` | 180 天 |

**日志里不放 `prompt_text`。** 它是客户剧情内容，日志系统通常比业务库的留存期更长、访问面更宽。排障需要正文时按 `manifest_id` 走正常读路径（有租户校验、有审计）。

### 8.4 告警

| 条件 | 级别 | 处置 |
|---|---|---|
| `s20_dialogue_drift_total` 5 分钟内 > 0 | P1 | 立即回滚最近发布的模板版本 |
| `s20_unsourced_completion_total` > 0 | P1 | 同上 |
| 编译失败率 > 5%（5 分钟） | P2 | 查 `code` 分布 |
| `s20_outbox_lag_seconds` > 60 | P2 | 查 relay |
| `s20_outbox_unpublished` > 10000 | P2 | 查 Kafka |
| 编译 p99 > 5s | P2 | 查步骤分布 |
| `s20_compile_dedup_total{source="user"}` 突增 | P3 | 查前端/客户端幂等 |

前两条是 P1 而不是 P2：它们意味着"已经产出过错误的 prompt"，而错误的 prompt 会变成付费调用、变成客户看到的成片。发现得越晚，损失越实。

---

## 9. 测试清单

### 9.1 纯函数性与复现（E32、E34）

| 编号 | 用例 | 断言 |
|---|---|---|
| T20-01 | 同输入编译 100 次 | `content_hash` 全部相同 |
| T20-02 | 跨两个区域编译同一输入 | `content_hash` 相同 |
| T20-03 | 跨两个进程（冷启动）编译 | `content_hash` 相同 |
| T20-04 | 修改系统时区 / `locale` | `content_hash` 不变 |
| T20-05 | 修改 `now()`（注入假时钟） | `content_hash` 不变；`compiled_at` 变化 |
| T20-06 | 打乱 `adapter_binding.reference_order_rule` 的源 JSON 键序 | 顺序结果不变（因为存的是数组） |
| T20-07 | 破坏纯函数性（实现注入随机数） | 测试应失败——这是守门测试，不是产品测试 |
| T20-08 | 同一输入组两个不同 `command_id` | 同一 `manifest_id`，其一 `replayed: true` |

### 9.2 对白零变换（E26、E27）

| 编号 | 用例 | 断言 |
|---|---|---|
| T20-09 | 单人对白，正常编译 | `dialogue_segments[0].text` 与原句完全相等 |
| T20-10 | 多句对白，含标点 | 每句逐字节相等，标点不变 |
| T20-11 | 含语气词（"呃""啊"） | 不被删除 |
| T20-12 | 模板错误地把对白放进 `template_string` 段并做了截断 | 422 `DIALOGUE_TEXT_DRIFT` |
| T20-13 | 模板用 `join()` 合并两条对白 | 422 `DIALOGUE_TEXT_DRIFT` |
| T20-14 | 有译文但未请求时 | prompt 中只有原句 |
| T20-15 | 有译文且显式引用 | 原句与译文都在，各自带 revision |
| T20-16 | 长对白超出 `max_chars` | 422 `RENDER_LIMIT_EXCEEDED`，**不是**自动截断 |
| T20-17 | 含 emoji / 组合字符 / 双向文本 | 逐字节相等 |

### 9.3 导演意图与溯源（E26、E33）

| 编号 | 用例 | 断言 |
|---|---|---|
| T20-18 | 枚举值全部命中词表 | 每条映射都有 `phrase_id` |
| T20-19 | 枚举值未命中词表 | 422，不臆造短语 |
| T20-20 | 模板写了未声明路径的插值 | 422 `UNSOURCED_COMPLETION` |
| T20-21 | `derived_from` 覆盖 `prompt_text` 的全部字符 | 无空隙（模板字面量也算覆盖） |
| T20-22 | 反向验证：从 manifest 反推 ShotSpec 字段 | 每一个非字面量片段都能回指 |

### 9.4 参考图顺序（E26、E29）

| 编号 | 用例 | 断言 |
|---|---|---|
| T20-23 | 两角色 + 场景，标准 adapter | position 依次 0/1/2，token 编号与 position 一致 |
| T20-24 | adapter 要求 `style` 在末位 | `style` 的 position 是最大值 |
| T20-25 | 同角色两张图 | 按 `order_hint` 再按 `global_asset_id` 稳定排序 |
| T20-26 | 绑定集缺少必需角色 | 422 `REFERENCE_ORDER_UNRESOLVABLE` |
| T20-27 | `_allow_leftover=true` 且有多余图 | 追加到末尾 + warning |
| T20-28 | 顺序变化时 manifest 变化 | `content_hash` 不同 |
| T20-29 | 同一绑定集两份 artifact revision 不同 | `content_hash` 不同 |

### 9.5 能力与参数（E05、E08–E11）

| 编号 | 用例 | 断言 |
|---|---|---|
| T20-30 | 必需字段无能力 | 422，两种 `strict_capabilities` 都一样 |
| T20-31 | 可选字段无能力 + 允许省略 | 200 + `OPTIONAL_OMITTED` |
| T20-32 | 可选字段无能力 + 不允许省略 | 422 |
| T20-33 | 参数超范围 | 422，**不裁剪** |
| T20-34 | 通配 adapter 命中 | 200 + `ADAPTER_FALLBACK` |
| T20-35 | 无 adapter | 422 `NO_ADAPTER_FOR_SNAPSHOT` |
| T20-36 | `adapter_revision` 超模板声明区间 | 422 `ADAPTER_NOT_APPLICABLE` |
| T20-37 | S24 不可用 | 503，`Retry-After` 存在 |
| T20-38 | 同一模型能力快照升级 | 新 manifest（`capability_snapshot_ref.content_hash` 变） |

### 9.6 幂等与并发（E12、E13）

| 编号 | 用例 | 断言 |
|---|---|---|
| T20-39 | 同 `command_id` 同 hash 并发 10 次 | 1 次执行，9 次回放，1 份 manifest |
| T20-40 | 同 `command_id` 异 hash | 409 `IDEMPOTENCY_KEY_REUSED` |
| T20-41 | 执行中重发 | 409 `COMMAND_IN_PROGRESS` |
| T20-42 | 进程崩溃留下 `IN_PROGRESS` | 60 秒后清理，重发可成功 |
| T20-43 | 两个不同 `command_id` 并发编译同一输入 | 1 份 manifest，全部 200 |
| T20-44 | 并发发布两个 DRAFT | 一个成功，一个 409 或被部分索引拦住 |
| T20-45 | 幂等记录 30 天过期 | 过期后同 `command_id` 视为新请求 |
| T20-46 | `DONE` 行不被清理任务删除 | 清理只碰 `IN_PROGRESS` |

### 9.7 跨租户与安全（E28、E01）

| 编号 | 用例 | 断言 |
|---|---|---|
| T20-47 | 租户 A 读租户 B 的 manifest | 404，无存在性泄漏 |
| T20-48 | 租户 A 用租户 B 的 `template_id` 编译 | 404 |
| T20-49 | body 里自授 `tenant_id` | 被忽略，用 TCT 值 |
| T20-50 | 缺 `creative.prompt.compile` 权限 | 403 |
| T20-51 | `prompt_text` 含租户标识 | 422 `OUTPUT_CONTAINS_FORBIDDEN_FIELD` |
| T20-52 | 日志内容扫描 | 无 `prompt_text` 明文 |
| T20-53 | 库内 `prompt_text` 落盘 | 密文（字段级加密生效） |

### 9.8 事件与 outbox（E32）

| 编号 | 用例 | 断言 |
|---|---|---|
| T20-54 | 编译成功 | outbox 恰好 1 行，`aggregate_sequence=1` |
| T20-55 | 重复触发 relay | 消费者收到重复事件但不丢（at-least-once） |
| T20-56 | 业务事务回滚 | outbox 无残留行 |
| T20-57 | 事件 `data` 不含 `prompt_text` | 只有 ref |
| T20-58 | 事件 envelope 符合 `event-envelope.v2` | schema 校验通过 |
| T20-59 | relay 崩溃后恢复 | 未发布的继续发，不重复发已发布的 |
| T20-60 | 归档任务跑过 | 已发布行移入归档，14 天后删除 |

### 9.9 状态机与版本（E39、E04）

| 编号 | 用例 | 断言 |
|---|---|---|
| T20-61 | DRAFT 被编译引用 | 422 `TEMPLATE_NOT_PUBLISHED` |
| T20-62 | 回归 fail 时发布 | 422 `REGRESSION_NOT_PASSED` |
| T20-63 | 退役后编译 | 422 `TEMPLATE_RETIRED` |
| T20-64 | 退役后读历史 manifest | 200 |
| T20-65 | 退役后 `GET` 模板 | 200（可读） |
| T20-66 | 编译中途退役 | 编译正常完成 |
| T20-67 | 换回归集版本再发布 | 新 `template_revision.content_hash` |
| T20-68 | 模板 revision 内容改动 | `content_hash` 必变 |

### 9.10 时长（E03）

| 编号 | 用例 | 断言 |
|---|---|---|
| T20-69 | 目标 4.0s，档位 5s | `handling=trim`，`trim_window` 完整 |
| T20-70 | 目标 4.0s 但档位只支持 8s | 视配置 `retime` 或 `unacceptable` |
| T20-71 | `unacceptable` 的 manifest 交给 S25 | 该候选被过滤 |
| T20-72 | 对白时长对齐缺失 | 422 `DIALOGUE_ALIGNMENT_REQUIRED`（原生对白镜头） |

---

## 10. 与上下游的接口约定

### 10.1 上游

| 上游 | 提供 | 约定 |
|---|---|---|
| **S06** | `ShotSpec`（`framing`/`camera`/`lighting`/`beats`/`dialogue`/`reference_bindings`/`duration_seconds`） | 按 `content_hash` 取；`beats` 必须结构化（[S06 §1.5](S06-storyboard.md)）；对白文本是权威，S20 只读 |
| **S04** | 对白原句、译文引用 | 原句/词典/译文分别版本化（[S04 §1.2](S04-narrative.md)），S20 不合并 |
| **S10** | `binding_set`（`global_asset_id` → `artifact_ref`） | 只读；S20 不解析资产本体 |
| **S24** | `CapabilitySnapshot`（参数与取值范围） | 只读；S20 不缓存副本 |
| **S37** | `dialogue_alignment`（对白时长） | 只读；缺失就报错，不估算 |

**S20 不向这些服务写任何东西。** 编译发现 ShotSpec 有问题（例如 `beats` 是散文），返回 422 让调用方去找 S06，不代为修复。

### 10.2 下游

| 下游 | 用法 | 约定 |
|---|---|---|
| **S25** | `GET /prompt-compilations:probe` 做候选兼容性；编译成功后决定是否提交 | probe 纯读、无 manifest；`compatible_with_omissions` 必须被 S25 理解为"可用但会有损失" |
| **S26** | 生成命令里的 `template_ref` 指向 manifest | `template_ref` 的 `service_id` 必须是 `S20`；S26 提交前回读 manifest 校验 `content_hash` |
| **S29/S31/S33/S34** | 各模态执行 | 只读 manifest；不得修改 prompt 文本 |
| **S35** | 引用 manifest 做 QC | 只读 |
| **S47** | 投影 | 消费 `prompt.compiled` |

### 10.3 `template_ref` 的语义锁定

`contracts/generation-command.v2.schema.json` 里 `template_ref` 是 `required` 且 `oneOf [ref-object, null]`，ref 形状为 `{service_id, id, revision, content_hash}`，`additionalProperties: false`。

**约束清单**（S26 与模态执行服务必须照此解释）：

1. `service_id` 恒为 `"S20"`。
2. `id` 是 `manifest_id`，**不是** `template_id`。
3. `revision` 是 manifest 的 `revision`（当前恒为 1）。
4. `content_hash` 必须与 manifest 的 `content_hash` 一致。
5. `null` 表示该模态不使用编译产物（例如纯 TTS 直接用 S31 的参数）。**不允许**用 `null` 表达"模板暂时缺失"——那会让缺失变成隐式。

第 2 条是最容易写错的：`template_ref` 字面上像"模板引用"，但它必须指向 manifest。如果指向 `TemplateRevision`，S26 就无法证明"这次提交用的是哪段文字"，§1.1 的整条论证就塌了。

### 10.4 事件契约

| 项 | 值 |
|---|---|
| 事件 | `prompt.compiled` |
| topic | `drama.prompt-compiler.v2` |
| 分区键 | `tenant_id:aggregate_id` |
| 投递 | at-least-once |
| 保留 | 14 天 |
| `replay_side_effects` | `false` |
| PII | `references_only_by_default` |

**S20 不消费任何事件**（v2 范围内）。所有上游依赖都是同步读。理由：编译是同步路径，依赖事件会让"编译请求"需要等待异步状态收敛，引入一个不必要的最终一致窗口。如果将来要预热能力缓存，再引入订阅（届时补 inbox）。

`replay_side_effects=false` 意味着消费者重放必须幂等——S47 的投影按 `manifest_id` upsert。

### 10.5 与 S19 的边界

S19 的导演 Agent 会调用 S20 编译。两条硬约定：

1. **S19 传来的 `tenant_id` 不被信任**，一律以 TCT 为准（AGENTS.md 明写）。
2. **S19 的 `proposal_only` 不改变 S20 的校验强度。** 一个 Agent 提出的编译请求与用户提出的走同一套校验；"是 AI 提的"不是放行理由，也不是加严理由。

反过来，S20 的模板也可能是 S19 的 system prompt 模板来源（`TemplateRevision` 的 `modality='text'` 一类）。那种情况下 S20 仍只负责"渲染成字节"，S19 负责"怎么用"。模板不承载 Agent 的策略。

---

## 11. 契约缺口

下列缺口都属于"需要先行修改契约或外部服务，S20 不能自行补"。方案里凡涉及处均已标注临时处理。

| # | 缺口 | 建议 |
|---|---|---|
| **C1** | `prompt.compiled` 在 `event-topics.v2.json` 里只声明了 topic 与投递属性，`schema_ref` 指向 `event-envelope.v2.schema.json`（信封），**没有 `data` 的 payload schema** | 补 `contracts/events/prompt.compiled.v2.schema.json`。S20 按 §4.3 的形状实现，但 `data` 字段集合未经契约批准 |
| **C2** | `execution-context.v2` 全部字段 `required`，而模板管理/回归这类命令没有 `node_id`/`node_epoch`/`execution_fence` | 要么给 `execution-context` 加一个"无节点上下文"的变体，要么为管理类命令单开 `admin-context`。S20 暂按"管理路径不校验这三个字段"实现 |
| **C3** | catalog 只声明 `POST /prompt-compilations`。`/templates`、`/templates/{id}/revisions`、`:retire`、`:probe`、`GET /prompt-compilations/{id}` 全部未声明；`openapi.v2.yaml` 里没有任何 S20 路径 | catalog 与 openapi 需补齐。**`:probe` 是 S25 与 S20 解环的必要接口**，缺了它 S25 只能靠"先编译再丢弃"，成本与语义都错 |
| **C4** | `contracts/` 下没有模板/adapter 的 JSON Schema。`TemplateRevision.body` 的模板语法（§3.5 的六种）没有形式化定义 | 补 `contracts/prompt/template-revision.v2.schema.json` 与 `adapter-binding.v2.schema.json`；模板语法要么给 BNF，要么给 JSON AST——**建议 AST**，因为 BNF 解析器比"只支持六种节点"更宽，容易实现出 `llm_rewrite` |
| **C5** | `plan:compute` 这个 `purpose` 在 docs/05、docs/13、[S01](S01-tenant-access.md)、[S18](S18-context-retrieval.md) 里出现，但 S23 的 `POST /budget-authorizations` **没有 `purpose` 字段**，S26 用的是 `"purpose": "generation"` | 与 [S19 §11 C5](S19-agent-runtime.md) 同一条缺口。S20 的编译**不花预算**（纯本地计算，无模型调用），所以**不申请 `plan:compute`**——这反而让 S20 置身该缺口之外。跨服务统一时不需要考虑 S20 |
| **C6** | outbox 的 `aggregate_id` 没有 `aggregate_kind` 列。S20 当前只有一个聚合类型（manifest），将来若加 `prompt.compilation.failed` 之类，会与 manifest 的序号空间混淆 | 与 [S19 §11 C6](S19-agent-runtime.md) 同一缺口。建议统一在 outbox 标准形状里加 `aggregate_kind` |
| **C7** | manifest 里的 `prompt_text` 与 `dialogue_segments` 是客户内容，**没有**"受限存储引用"的通用规则（`data_ref` 可读范围 + 保留期 + 地域）。E31 无法验证 | 需要一份跨服务的"受限存储引用"规则，与 [S19 §11 C2](S19-agent-runtime.md) 同一条 |
| **C8** | `ShotSpec` 里 `reference_bindings[]` 的 `role` 枚举没有契约定义。adapter 的 `reference_order_rule` 用 `character`/`scene`/`style`/`first_frame` 这类角色名，但两边没有共享的枚举 | 需要一个 `contracts/reference-role.v2.json`（或塞进 `shot-spec` schema 的枚举）。当前由 S06 的 schema 隐式定义，S20 假定它稳定 |
| **C9** | S24 的 `CapabilitySnapshot` 没有声明"这个参数是必需还是可选"。S20 的 `REQUIRED_FIELD_UNSUPPORTED` 判断依赖模板侧声明（`body.sections[].optional`），而"字段是否必需"其实是镜头语义属性 | 建议在 `capability-snapshot` 里加 `required_parameters[]`，或者在 `ShotSpec` 里标字段必要性。当前用模板的 `optional` 位做代理判断，语义上不精确 |
| **C10** | 模板的"回归集"（`regression_set_ref`）没有契约形状。§4.4 的断言集合是 S20 自己发明的 | 需要 `contracts/prompt/regression-set.v2.schema.json` 与回归报告的 schema。当前两者都是内部结构，不跨服务 |
| **C11** | 没有 `contracts/model-snapshot.v2` 的正式定义。`model_snapshot` 的字符串格式（`name@YYYY-MM`）在 S20 的 DDL 里用正则硬编码，S24/S25/S26 各自假定 | 需要统一格式定义，否则跨服务的快照对比会失配 |

C1、C3、C4 是阻塞级——没有它们，S20 的接口无法被外部按契约消费，实现只能按本方案的内部约定走。C2、C8、C9 会影响正确性边界，但可以先用"模板侧声明"代理。C5–C7、C10、C11 是统一性缺口，不阻塞单服务实现。

---

## 12. 未决

1. **模板语法的形式化程度。** §2.5 列了四种 `render`、§3.5 列了六种语法。是否要把模板语法定成 JSON AST 进契约（C4 的建议），还是留给每个实现自己解析字符串模板？倾向 AST：字符串模板的解析器很难拦住 `llm_rewrite` 这类后门，而 AST 里根本没有那个节点类型。待定。

2. **`compatible_with_omissions` 是否该由 S20 判断。** 当前做法是 S20 看"这个模型缺哪些可选项"→ 判 `compatible_with_omissions`。但"可选项"是否可省，取决于**这个镜头**的语义（一个镜头的 `lighting.mood` 是关键，另一个可有可无），这更像 S25 或 S06 的判断。S20 现在用模板的 `optional` 位做代理（C9），不够准。要不要把这个判断整体交给 S25？

3. **按模型版本缓存的范围。** docs/design-package 的 S20 条目写了"按模型版本缓存"。当前 manifest 已经内容寻址（等价于缓存），但**词汇表/短语表**（`adapter_binding.field_map` 的产物）没有单独缓存。precompile 词表能省下不少渲染开销，但会把 adapter 的变更传播路径变复杂（缓存失效时机）。要不要做？

4. **回归集的规模。** §4.4 的例子是 12 个 case。多了发布变慢，少了漏检。是否需要按 `modality` 定不同的最小规模（例如 video ≥ 20、tts ≥ 10）？以及"哪些断言是必选"要不要进契约？

5. **`UNSOURCED_COMPLETION` 的误报风险。** §3.5 的实现依赖"渲染时记录每个输出区间的来源"。这要求渲染器在输出时维护 span 映射，而不仅是拼接字符串。如果实现用模板引擎的普通渲染（拿不到 span），这条检查就退化成"事后对比"——那样会漏掉"模板正文里混入的数据"。要不要在方案里强制"渲染器必须返回 span"作为接口约束？

6. **退役模板的历史 manifest 是否也要"退役"。** 当前约定是"退役只影响新请求，历史产物不动"（docs/08 §3）。但如果退役原因是**模板有严重缺陷**（例如它会篡改对白），那么历史 manifest 也是错的，是否应该标记为"不可用于新派单"？这涉及 S26 在派单前是否要检查 manifest 的模板状态。倾向：加一个 `manifest.blocked_at` 供运维标脏，由 S26 在派单前检查。但这是新概念，先不写。

7. **`GET /prompt-compilations:probe` 的批量上限。** S25 一次可能要给 10–30 个候选做探测。一次请求带 30 个 snapshot 会不会太慢（每个都要查 adapter + capability）？可以限 32 并让 S25 分批，也可以让 S20 并行查。当前未定上限。

8. **审计保留期。** §8.3 给了 90 天 / 永久 / 180 天。这些数字是拍的，没有客户合同或合规要求背书。正式定稿前需要与 S02（治理策略）对齐——保留期大概率该由策略服务下发，而不是每个服务自己写死。

