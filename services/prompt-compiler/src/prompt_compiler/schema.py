"""S20 自己的 schema。表来源是 `plans/S20-prompt-compiler.md` §5。

四件结构性事实，它们决定这张库能证明什么：

1. **模板 revision 发布后不可变。** `content_hash` 覆盖 `compatible_schema`、
   `body`、adapter 区间、`model_snapshots`（排序后）、`regression_set_ref`，
   不含 `state` / `published_at` / `author_subject`。改模板 = 新 revision
   （§1.1、§3.7）。
2. **manifest 内容寻址。** `manifest_input_once` 唯一索引把「同一输入组」收敛成
   一份（§2.8 第二层幂等）。它能成立的前提只有一个：编译是纯函数（§1.6）。
   一旦渲染里混进 `now()` / 随机数 / 不稳的 dict 顺序，这个索引会把两份本应
   不同的产物撞成一份 —— 是缺陷的放大器，不是保险（§6.2）。
3. **`reference_order_rule` 存有序数组，不存 JSON object。** object 的键序在
   反序列化后不可依赖，而位置映射必须确定性（§3.4 第 1 条）。
4. **`node_epoch` / `execution_fence` 只打标，不进 `content_hash`。** 进了的话
   同一份 prompt 在每个 epoch 下各有一份 manifest，「按内容缓存」失效
   （§3.9）。AGENTS.md：两者绝不合并、绝不互换。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`bytea` 存 64 位
hex（与仓内其它服务一致）。
"""

ID_PREFIX = {
    "template": "tpl_",
    "adapter": "adb_",
    "manifest": "cpm_",
    "regression": "rgr_",
}

# §5.1 的 CHECK。新增模态要发版并改 contracts，不是运行时输入。
MODALITIES = ("image", "video", "audio", "tts", "music", "sfx", "lipsync",
              "upscale", "edit")

# §2.5：刻意只有四个。**没有 `llm_rewrite`** —— 加上它就是给编译器开了一条
# 改写事实的后门。
RENDER_TYPES = ("verbatim", "phrase_table", "template_string", "omit")

TEMPLATE_STATES = ("DRAFT", "PUBLISHED", "RETIRED")
REGRESSION_VERDICTS = ("pending", "pass", "fail")

# §3.4 / §3.6 / §7：只允许这三条带 warning 成功。其余降级一律拒绝。
WARNING_CODES = ("ADAPTER_FALLBACK", "OPTIONAL_OMITTED",
                 "UNMAPPED_REFERENCE_APPENDED")

# §2.6 / §12 第 7 条：S25 一次最多探测多少候选。方案没定上限，这里取 32 并把
# 它登记进缺口，而不是默默接受任意长度。
MAX_PROBE_SNAPSHOTS = 32

# 一次编译最多处理多少个回归用例。回归是发布门，不是压测。
MAX_REGRESSION_CASES = 200

# ShotSpec 的必填字段（`contracts/domain-v1/shot-spec.v1.schema.json` 的
# `required`）。方案 §3.2 写的是 `framing` / `camera_movement`，那是**旧字段名**，
# 见 CONTRACT_GAPS 的 S20-12。
SHOT_REQUIRED = (
    "schema_version", "shot_id", "revision", "episode_id", "scene_id",
    "display_locator", "narrative_purpose", "shot_type", "asset_bindings",
    "camera", "duration", "output_spec", "beats", "dialogue", "emotion",
    "lighting", "continuity", "constraints",
)

# 本服务**不解决**的缺口。`plans/S20-prompt-compiler.md` §11 的 C1–C11 加上
# 实现时新发现的。AGENTS.md：「契约与文档冲突 → 停下，报告冲突，不要自行选边」。
# 这里只登记偏差与临时处理，不修改 contracts、不代填 owner 决定。
CONTRACT_GAPS = [
    # ---------------------------------------------------------- 方案 §11 的 C1–C11
    {"id": "S20-C1", "kind": "conflict",
     "summary": "`event-topics.v2.json` 的 `prompt.compiled` 条目已指向 "
                "`prompt.compiled.v2.schema.json`（`data_schema_ref`），该文件"
                "也已存在；但它的 `required` 是 9 个字段而 `data_ref` 不在其中，"
                "而 `event-envelope.v2` 又把 `data_ref` 列为必填 —— 于是「事件"
                "只带 7+2 个字段」与「信封必须带 data_ref」只能同时满足",
     "handling": "按 `prompt.compiled.v2.schema.json` 的 9 个 `required` 逐个"
                 "填（含 `schema_version`）；`data_ref` 放信封层而不是 data 层。"
                 "方案 §4.3 的示例漏了 `schema_version`，以 schema 为准并在 "
                 "`_emit_compiled` 里注明。"},
    {"id": "S20-C2", "kind": "conflict",
     "summary": "`execution-context.v2` 全部字段 `required`，而模板管理/回归这类"
                "命令没有 `node_id`/`node_epoch`/`execution_fence`",
     "handling": "编译路径要求三者齐全；模板创建/发布/退役不校验这三个字段"
                 "（方案 §2.1 的临时处理）。"},
    {"id": "S20-C3", "kind": "conflict",
     "summary": "catalog 只声明 `POST /prompt-compilations`；`/templates`、"
                "`/templates/{id}/revisions`、`:retire`、`:probe`、"
                "`GET /prompt-compilations/{id}` 全部未声明，"
                "`openapi.v2.yaml` 里没有任何 S20 路径",
     "handling": "按方案 §2.2 / §2.3 实现这些路径；不改 catalog、不改 openapi，"
                 "等 owner 补契约。`:probe` 是 S25 与 S20 解环的必要接口。"},
    {"id": "S20-C4", "kind": "conflict",
     "summary": "`contracts/` 下没有模板/adapter 的 JSON Schema，"
                "`TemplateRevision.body` 的模板语法没有形式化定义",
     "handling": "模板 body 的结构校验在本服务内做（`RENDER_TYPES` 四值 + 六种"
                 "语法），失败用新码 422 `TEMPLATE_BODY_INVALID`；等 owner 定 "
                 "AST 后替换。**没有** `llm_rewrite` 节点，这是刻意的。"},
    {"id": "S20-C5", "kind": "gap",
     "summary": "`plan:compute` 这个 purpose 在 S23 的预算接口里没有字段",
     "handling": "S20 的编译是纯本地计算、无模型调用、不花预算，所以不申请"
                 "`plan:compute` —— 本服务置身该缺口之外。"},
    {"id": "S20-C6", "kind": "gap",
     "summary": "outbox 没有 `aggregate_kind` 列，将来加 "
                "`prompt.compilation.failed` 会与 manifest 的序号空间混淆",
     "handling": "`aggregate_sequence` 恒为 1；平台 SDK 的 outbox 也没有这条"
                 "唯一约束，所以「同聚合不重号」在本层靠「manifest 只写一次」保证，"
                 "不声称被数据库约束证明。"},
    {"id": "S20-C7", "kind": "needs_owner",
     "summary": "manifest 的 `prompt_text` / `dialogue_segments` 是客户内容，"
                "没有「受限存储引用」的通用规则（可读范围/保留期/地域），E31 无法验证",
     "handling": "本实现**明文落库**（没有可用 KMS 与加密格式），因此 T20-53 "
                 "未实现；`KMS_UNAVAILABLE` 只作为契约里的未来分支保留，"
                 "当前不会触发。等跨服务受限存储规则。"},
    {"id": "S20-C8", "kind": "conflict",
     "summary": "`ShotSpec.reference_bindings[].role` 的枚举没有契约定义，"
                "与 adapter `reference_order_rule` 的角色名没有共享枚举",
     "handling": "本实现用 S10 的 `asset_type` 词表（character/costume/scene/"
                 "prop/voice）作参考角色；adapter 规则只能用这些角色名。"
                 "其它角色（style/first_frame）在 S10 里存不进去，见 S20-13。"},
    {"id": "S20-C9", "kind": "conflict",
     "summary": "S24 的 `CapabilitySnapshot` 没有声明参数是必需还是可选；"
                "`REQUIRED_FIELD_UNSUPPORTED` 的判断依赖模板侧声明",
     "handling": "用 `body.sections[].optional` 与 adapter 的 "
                 "`unsupported_optionals` 做代理判断（方案 §11 C9 的临时处理），"
                 "语义上不精确，等 S24 补 `required_parameters[]`。"},
    {"id": "S20-C10", "kind": "gap",
     "summary": "回归集（`regression_set_ref`）没有契约形状，§4.4 的断言集合"
                "是 S20 自己发明的；回归集正文也没有读取路径",
     "handling": "发布请求体带 `regression_cases`，并要求 "
                 "`regression_set_ref.content_hash == sha256(JCS(cases))`；"
                 "不一致用新码 422 `REGRESSION_SET_MISMATCH` 拒绝。"
                 "回归报告是内部结构，不跨服务。"},
    {"id": "S20-C11", "kind": "gap",
     "summary": "没有 `contracts/model-snapshot.v2`，`name@YYYY-MM` 的格式在"
                "S20 的 DDL 里硬编码，S24/S25/S26 各自假定",
     "handling": "写入时用 Python 正则校验（SQLite 没有 REGEXP，DDL 里只留"
                 "一个弱 CHECK），避免「模糊匹配」进门。"},

    # ------------------------------------------------------ 实现时新发现的缺口
    {"id": "S20-12", "kind": "conflict",
     "summary": "方案 §3.2 / §10.1 用 `ShotSpec.reference_bindings`、"
                "`framing`、`camera_movement`、`duration_seconds`；"
                "`shot-spec.v1.schema.json` 实际是 `asset_bindings`、"
                "`camera.shot_size`/`camera.movement`、`duration.target_seconds`",
     "handling": "按**实际 schema** 实现：参考集从 `asset_bindings` 派生，"
                 "机位取 `camera.*`，时长取 `duration.target_seconds`。"
                 "不改契约、不改方案正文，等 owner 对齐（AGENTS.md）。"},
    {"id": "S20-13", "kind": "conflict",
     "summary": "`asset_bindings.style` 在 S10 的 `ASSET_TYPES` 里没有对应"
                "asset_type（只有 character/costume/scene/prop/voice/adaptation），"
                 "`role_key='style:...'` 写不进绑定集",
     "handling": "`style` **不作为参考图**解析；adapter 规则要求 `style` 角色时"
                 "判 422 `REFERENCE_ORDER_UNRESOLVABLE`，而不是找一张图顶上"
                 "（§1.4：「适配不了 → 该候选被拒绝」）。"},
    {"id": "S20-14", "kind": "conflict",
     "summary": "§3.2 的 `hit.identity_ref` 在 S10 的绑定条目里不存在"
                "（entry 只有 rights_ref / variant_selector / owner_* / required）",
     "handling": "不产出 `identity_constraint_ref`；身份一致性归 S35/S36，"
                 "S20 也不该判定它。"},
    {"id": "S20-15", "kind": "conflict",
     "summary": "§2.4 的 `POST /prompt-compilations` 示例体没有 "
                "`execution_context`，而 §3.9 要求从它取 "
                "`node_id`/`node_epoch`/`execution_fence`",
     "handling": "要求请求体带 `context`（与 `generation-command` 同形）；"
                 "缺字段 400 `INVALID_SCHEMA`。字段名待契约确认。"},
    {"id": "S20-16", "kind": "conflict",
     "summary": "`audio_strategy` 在 `generation-command.input` 里，"
                "`ShotSpec` 没有这个字段；§2.4 的编译请求也没有",
     "handling": "从 `options.audio_strategy` 读，缺省 `silent_then_tts`；"
                 "只有 `native_locked_voice` 且镜头有对白时才要求对齐结果。"},
    {"id": "S20-17", "kind": "conflict",
     "summary": "T20-19 要求「枚举值未命中词表」返回 422，但 §7 的失败表没有"
                "为这种情况分配错误码",
     "handling": "用 422 `REQUIRED_FIELD_UNSUPPORTED` + "
                 "`details.reason='PHRASE_NOT_FOUND'`；不臆造短语。"},
    {"id": "S20-18", "kind": "conflict",
     "summary": "§3.2 要求绑定集 hash 一致，但 §7 只给 `shot_ref` 的 hash "
                "不一致分配了 `SHOT_REVISION_MOVED`；能力快照同理",
     "handling": "绑定集/能力快照 hash 不一致用 409 `SOURCE_REVISION_MOVED`"
                 "（新码，`details.source` 指明是谁），可重试。"},
    {"id": "S20-19", "kind": "conflict",
     "summary": "§7 只为 S10 / S24 的不可用分配了 503 码；S06 与 S37 没有",
     "handling": "S06 不可用 → 503 `SHOT_SOURCE_UNAVAILABLE`；对齐结果不可用 → "
                 "503 `ALIGNMENT_SOURCE_UNAVAILABLE`（均为新码）。"},
    {"id": "S20-20", "kind": "conflict",
     "summary": "§2.5 的 `compatible_schema` 是 `{id, revision:3}`，而 "
                "`shot-spec.v1` 的版本标识是 `schema_version:「1.0」`，"
                "两者无法比较",
     "handling": "按 `{id, schema_version}` 比较；`revision` 若给了只原样存。"
                 "`compatible_schema` 缺 `schema_version` 时发布阶段就拒绝"
                 "（422 `TEMPLATE_BODY_INVALID`），不让它在编译期变成"
                 "永远不可达的检查。"},
    {"id": "S20-21", "kind": "conflict",
     "summary": "§2.3 把 `:probe` 列为 `GET`，§2.6 却给了请求体",
     "handling": "同一路径同时挂 GET 与 POST，行为一致；POST 是主用法。"},
    {"id": "S20-22", "kind": "conflict",
     "summary": "S24 的 `mode`（t2v/i2v/...）与模板的 `modality`"
                "（image/video/...）没有对应关系",
     "handling": "`options.mode` 指定；未指定时取快照 `mode_details[0]`（顺序由"
                 "上游查询决定，同一快照内稳定）。不自行发明映射表。"},
    {"id": "S20-23", "kind": "gap",
     "summary": "§2.2 没有 `adapter_binding` 的写命令，§5.2 只有表",
     "handling": "内核提供 `put_adapter()` 供种子导入与测试；**不**暴露 HTTP "
                 "命令，也不给它编 id（adapter_id 由调用方给出）。"},
    {"id": "S20-24", "kind": "gap",
     "summary": "方案没有定义「上一版回归失败留下的 DRAFT 如何让位给新 DRAFT」，"
                "而 `template_revision_one_draft` 部分唯一索引不允许两份",
     "handling": "新发布把旧 DRAFT 置 `RETIRED`（`retired_at` 非空）以释放索引；"
                 "DRAFT 从未发布过，所以不是抹掉历史。"},
    {"id": "S20-25", "kind": "gap",
     "summary": "`options.strict_capabilities` 在 §3.6 的表格里两列取值相同，"
                "没有可观察的行为差异",
     "handling": "接受并记录进 manifest，不做分支；按 F11（可选字段不支持且"
                 "不允许省略 → 422）实现唯一确定的那条。"},
    {"id": "S20-26", "kind": "conflict",
     "summary": "`shot-spec.v1` 的 `dialogue[]` 只有 `character_id`，没有说话人"
                "显示名；§1.2 的 `speaker` 前缀需要名字",
     "handling": "用 `character_id` 作 `speaker`。显示名是 S11 的资产内容，"
                 "S20 不读资产本体（§0 的「不写」清单）。"},
    {"id": "S20-27", "kind": "gap",
     "summary": "译文（S38 `dialogue_translation`）没有进入编译请求的契约字段，"
                "§1.2 的「原句+译文各自带 revision」无法表达",
     "handling": "T20-14（只有原句）成立；T20-15（显式引用译文）**未实现**，"
                 "等契约补 `translation_refs`。"},
    {"id": "S20-28", "kind": "gap",
     "summary": "§2.2 的权限（`creative.prompt.compile` 等）在本仓任何服务的 "
                "HTTP 层都没有实现，也没有权限来源",
     "handling": "本层不做鉴权，只做租户隔离（`X-Tenant-Id`）；登记为跨服务"
                 "缺口，不假装 T20-50 已覆盖。"},
    {"id": "S20-29", "kind": "conflict",
     "summary": "T20-49 说 body 里自授的 `tenant_id`「被忽略」，而 S10 等"
                "既有服务对同一情形返回 403 `TENANT_MISMATCH`",
     "handling": "按仓内既有形态**拒绝**（403），不静默忽略：忽略会让调用方"
                 "以为自授生效，拒绝才是可观测的。AGENTS.md：LLM 提供的 "
                 "`tenant_id` 不能当可信输入。"},
    {"id": "S20-30", "kind": "gap",
     "summary": "§3.4 说参考图「通常以占位符出现在 prompt 里」，但 §3.5 的六种"
                "语法没有引用块，占位符无法从模板正文里产出",
     "handling": "token 只写在 `references[].token`（编号 = 顺序位置），"
                 "不注入 `prompt_text`；这样「文本说 A 是主参考、参数把 B 放 "
                 "index 0」的分裂不可能出现。"},
    {"id": "S20-32", "kind": "conflict",
     "summary": "`template_ref` 在 §3.8 里是语义锁（`id` = manifest_id），"
                "而 `manifest_input_once` 需要输入模板的 `{id, revision}`；"
                "同一列无法同时承载两者",
     "handling": "manifest 增列 `template_source_ref` 存输入指针；对外的 "
                 "`template_ref` 一律按 §3.8 的语义锁形状返回。`content_hash` "
                 "覆盖 `template_source_ref.content_hash`，未被 §3.10 的排除清单"
                 "排除。"},
    {"id": "S20-31", "kind": "gap",
     "summary": "发布时 `regression_cases` 是 S20 自己发明的用例形状"
                "（内联 shot/binding/capability，离线确定性渲染）",
     "handling": "这是 C10 的实现形态；报告结构是内部的。付费调用不进回归。"
                 "等 `contracts/prompt/regression-set.v2.schema.json`。"},
    {"id": "S20-33", "kind": "gap",
     "summary": "§2.5 的 `body.sections[]` 只有 `from` / `render`，而 §3.5 的六种"
                "语法需要一段可插值的模板文本；两者之间缺一根线",
     "handling": "每段允许带可选的 `text`（受限模板语法，路径必须落在任一段的 "
                 "`from` 里），段间用可选的 `section_separator` 连接（默认换行，"
                 "必须是字面量）。没有 `text` 时按 `render` 的默认形态输出。"},
    {"id": "S20-34", "kind": "gap",
     "summary": "S24 `GET /capability-snapshots/{id}` 的响应里没有 `content_hash`，"
                "而 §2.4 的 `capability_snapshot_ref` 要求一个可核对的 hash",
     "handling": "用 `sha256(JCS(响应体))` 作为该快照的内容指纹；调用方给了 pin "
                 "就比对，不一致 → 409 `SOURCE_REVISION_MOVED`。这是 S20 侧的"
                 "派生，不是 S24 的承诺，等 S24 补 hash。"},
    {"id": "S20-35", "kind": "conflict",
     "summary": "`contracts/prompt.compiled.v2.schema.json` 把 `warnings` 收成 "
                "`string[]`（PCM-2），而 manifest 的 warning 需要带被省略的字段名",
     "handling": "manifest 内 `warnings` 存对象 `{code, field?}`（它进 "
                 "`content_hash`）；事件里编码成 `CODE` 或 `CODE:field` 字符串，"
                 "满足契约。两侧由 `_event_warnings()` 单点转换。"},
    {"id": "S20-36", "kind": "gap",
     "summary": "§7 只给模板发布/退役分配了 F23/F24，创建模板与「模板不存在」"
                "没有错误码",
     "handling": "新增 409 `TEMPLATE_EXISTS`、404 `TEMPLATE_NOT_FOUND`；"
                 "发布不存在的模板也用后者。"},
    {"id": "S20-37", "kind": "conflict",
     "summary": "F23 要求回归失败时「版本仍 DRAFT」，但失败若落库，同一 "
                "`command_id` 的重试会撞上 `template_revision_one_draft`；"
                "而幂等包装在异常时会回滚整个事务",
     "handling": "回归失败**不写任何 revision 行**，只在 422 的 `details` 里"
                 "返回完整报告。代价是「失败的 DRAFT」不可见；收益是重试可"
                 "复现。与 F23 的字面冲突记录在此。"},
    {"id": "S20-38", "kind": "gap",
     "summary": "§3.7 的退役守卫是「无未完成编译」，但 S20 没有在途编译登记表",
     "handling": "编译是同步的、manifest 一旦写入不可变，退役只拦新编译；"
                 "不假装检查了在途任务。"},
    {"id": "S20-39", "kind": "gap",
     "summary": "§1.5 的 `provider_tier_seconds` 来自 S24 的 "
                "`duration_tiers_s`，但能力快照里没有对应画幅的模式时取不到档位",
     "handling": "取不到档位时 `provider_tier_seconds` 为 `null`、"
                 "`handling='unacceptable'`——不发明一个档位让账算得漂亮。"},
    {"id": "S20-40", "kind": "gap",
     "summary": "§1.1 的模板 `content_hash` 覆盖清单没有 `adapter_scopes`，"
                "但它决定 probe 与 adapter 选择的结果",
     "handling": "把它与规范化后的 `compatible_schema` 一起纳入 hash；"
                 "其余清单逐条照 §1.1。"},
    {"id": "S20-41", "kind": "gap",
     "summary": "§2.6 的 probe 入参只有 `model_snapshot` 字符串，而能力绑定在 "
                "`capability_snapshot_ref` 上；S24 没有「按快照串查 id」的路由",
     "handling": "probe 只回答 adapter × 范围 × 可选省略三件事；"
                 "`requirement_set_ref` 原样回显、不参与判定。真正的能力"
                 "判定发生在编译步骤 ⑥，那里有 `capability_snapshot_ref`。"},
    {"id": "S20-42", "kind": "gap",
     "summary": "§3.5 示例的 `derived_from.kind` 只有 `phrase_table` / "
                 "`template_literal` / `verbatim` 三值，"
                 "而六种语法里的路径引用与 `join` 没有独立 kind",
     "handling": "路径插值与 `join` 记 `kind:'template_string'` 并附 `path`；"
                 "不新造 kind 值，避免下游按枚举解析时遇到未知项。"},
    {"id": "S20-43", "kind": "conflict",
     "summary": "§3.2 的 `global_asset_id` / `hit.identity_ref` 在 S10 的绑定条目"
                 "里都不存在；实际稳定标识是 `role_key = <asset_type>:<owner_local_id>`",
     "handling": "`references[].global_asset_id` 填 `role_key` 原值；"
                 "`artifact_ref` 由 `owner_service/owner_local_id/owner_revision/"
                 "owner_hash` 组装。不产出 `identity_constraint_ref`（同 S20-14）。"},
    {"id": "S20-44", "kind": "conflict",
     "summary": "F6（缺引用 → `REFERENCE_DANGLING`）与 T20-26（缺必需角色 → "
                 "`REFERENCE_ORDER_UNRESOLVABLE`）在同一情形上给了两个码",
     "handling": "按「adapter 是否点名该角色」分流：adapter 规则里出现且未标 "
                 "`required:false` 的角色缺失 → `REFERENCE_ORDER_UNRESOLVABLE`；"
                 "其余缺失 → `REFERENCE_DANGLING`。两个码都保留。"},
    {"id": "S20-45", "kind": "conflict",
     "summary": "§4.1 的 `content_hash` 覆盖清单不含 `dialogue_alignment_ref`，"
                 "而 §5.3 的 `manifest_input_once` 也不含它；两个不同的对齐结果"
                 "会收敛成同一份 manifest",
     "handling": "把它纳入 `content_hash`：对齐结果不同 = 输入不同。"
                 "这是对 §4.1 清单的增补，记录在此而不是悄悄改。"},
    {"id": "S20-46", "kind": "conflict",
     "summary": "参考图 token 编号两处写法互相矛盾：§3.4 说「编号就是顺序位置」，"
                 "而 §4.1 的示例是 `position: 0` 配 `<image_1>`；按前者会产出 "
                 "`<image_0>`，按后者是 position+1",
     "handling": "取 §4.1 的示例（`position + 1`），因为它是唯一给出了**具体"
                 "字节**的一处，而 adapter 的 `reference_token_format` 下游会照抄。"
                 "若 owner 判定 §3.4 为准，改 `order_references` 的一行即可；"
                 "这条登记是为了不让差异消失在实现里。"},
    {"id": "S20-47", "kind": "gap",
     "summary": "§5.2 的 `adapter_binding` 没有 `phrase_tables` / "
                 "`speaker_prefix` / `section_separator` / `parameter_ranges` / "
                 "`content_type` 这几列，但 §3.5 的 `phrase_table` 与 `join` "
                 "语法离开它们无法渲染，§3.1 步骤 ⑤ 的说话人前缀也无处存放",
     "handling": "在本服务的表里加这五列并在写入时校验形状。它们属于 adapter "
                 "契约的一部分，理应升到契约层；登记在此，不代 owner 决定列名。"},
    {"id": "S20-48", "kind": "conflict",
     "summary": "S24 的 `output_specs[]` 嵌在 `mode_details[]` 里，不在快照顶层；"
                 "而 `resolve_duration` / 宽高比校验天然按快照查，只读顶层会"
                 "「查不到」并据此报不可用",
     "handling": "`_output_specs(capability)` 两种形状都认（顶层优先，否则合并"
                 "各 mode_detail）。时长解析传**本模态的 detail**，避免 t2v 的"
                 "目标时长拿到 i2v 的档位。找不到才是真的未知。"},
    {"id": "S20-49", "kind": "conflict",
     "summary": "`event-envelope.v2.schema.json` 要求 `aggregate_sequence` 且 "
                 "`additionalProperties: false`，而 `platform/event-sdk` 的 "
                 "`new_event` 只写 `sequence`、不写 `aggregate_sequence`；"
                 "T20-58 要求信封通过该 schema 校验，按 SDK 现状无法通过",
     "handling": "不改平台 SDK（跨服务，且 audit 的 NEW-9 与 agent-runtime 的 "
                 "S19-18 已就同一冲突登记过）。本服务补 "
                 "`extra['aggregate_sequence']=1` 使契约要求的键存在，`sequence` "
                 "仍由 SDK 输出；因此**信封仍带一个 schema 未声明的键**，T20-58 "
                 "的「校验通过」在本层只能验到 `data` 部分。等 owner 统一。"},
    {"id": "S20-50", "kind": "conflict",
     "summary": "§5.2 的 `adapter_binding` 主键是 `(tenant_id, adapter_id, "
                 "revision)`，但同一张表上的 `adapter_binding_exact` / "
                 "`adapter_binding_wildcard` 唯一索引是 "
                 "`(tenant_id, provider, model_snapshot, adapter_id)` / "
                 "`(tenant_id, provider, adapter_id)`。两者不相容：主键允许同一 "
                 "adapter 对同一快照累积多个 revision，唯一索引不允许第二个",
     "handling": "保留方案给的两个唯一索引（它们表达的是「一个 adapter 对一个"
                 "快照只有一种映射」，这是 `select_adapter` 的前提），因此 "
                 "revision 实际不可累积，`put_adapter` 撞索引时返回 409 "
                 "`TEMPLATE_REVISION_CONFLICT` + 现有 revision，而不是让 "
                 "`sqlite3.IntegrityError` 冒成 500。若 owner 要保留多版本，"
                 "则需把索引改为包含 revision —— 但那会让 `select_adapter` 的 "
                 "「精确匹配唯一」失去保证。"},
    {"id": "S20-51", "kind": "conflict",
     "summary": "§5.5 / §6.1 要求 `idempotency_record` 带 `state "
                "(IN_PROGRESS|DONE)`、`expires_at`，并有「`IN_PROGRESS` 60 秒"
                "后清理、`DONE` 保留 30 天」的后台任务；平台 "
                "`idempotency_sdk` 的 DDL 只有 `(tenant_id, command_id, "
                "request_hash, result, created_at)`，用 `result IS NULL` 表示"
                "在途，没有任何时间/状态列，也没有清理任务",
     "handling": "沿用平台 SDK 的表结构（跨服务一致优先于方案里的更丰富"
                 "形状），因此 T20-42 / T20-45 描述的过期与清理语义"
                 "**未实现**：进程在 `begin` 之后崩溃会留下永久在途行，同一 "
                 "`command_id` 之后永远 409 `COMMAND_IN_PROGRESS`；`DONE` 行"
                 "也永不回收。缓解手段只有调用方换一个新的 `command_id`。"
                 "要做真正的清理，改 SDK 而不是在 S20 里私建第二张表 —— 否则"
                 "同一个 `command_id` 在不同服务上会有不同生命周期。"},
    {"id": "S20-52", "kind": "ambiguity",
     "summary": "§3.7 / F18 说 body 里的 `tenant_id`「一律以 TCT 为准，"
                "忽略之」；但「忽略」与「拒绝」在可观测行为上不同：忽略会"
                "返回 201，调用方拿到的结果属于另一个租户却以为是自己那次",
     "handling": "取更严的一侧：body `tenant_id` 与签名头不一致时返回 403 "
                 "`TENANT_SCOPE_DENIED`，一致时放行（只当回显）。理由是"
                 "「静默修正调用方的意图」比「明确告诉它写错了」更容易在生产"
                 "里变成一次跨租户事故。若 owner 要按原文「忽略」，把 "
                 "`api/app.py::_body` 里那段判断删掉即可，但请同时补一条"
                 "「写错租户仍返回 201」的显式测试，不要让这个差异只存在于"
                 "注释里。"},
    {"id": "S20-53", "kind": "missing",
     "summary": "§9.7 T20-50 要求「缺 `creative.prompt.compile` 权限 ⇒ 403」；"
                "本仓库没有授权层 —— 没有权限中间件、没有 scope 表，"
                "`X-Tenant-Id` 被当作已签名的事实直接采信（验签与 scope "
                "由网关负责，不在本服务内）",
     "handling": "只实现缺 `X-Tenant-Id` ⇒ 400 那一半。权限判定依赖 S02 / "
                 "网关，属平台组件；在服务内自造一套 scope 表会变成第二个"
                 "权威。上线前必须有网关注入并校验 scope，否则本端点对任何"
                 "能伪造头部的主体开放 —— 这是部署前置条件，不是本服务能"
                 "自己补的缺口。"},
    {"id": "S20-54", "kind": "missing",
     "summary": "§5.6 注 3 要求「`outbox` 行 `published_at + 7d` 移入归档表、"
                "再 14 天删除」；平台 `event_sdk` 的 outbox 只有 `published_at`，"
                "没有 `archived_at`，本服务也没有归档表或归档任务",
     "handling": "不实现（T20-60）。归档是 relay / 平台侧的职责，S20 只负责"
                 "「与业务事实同事务写入未发布行」。在服务内建归档任务会让"
                 "同一张 outbox 出现两个消费者（relay 与归档），从而需要额外"
                 "的协调才不丢事件 —— 收益（节省磁盘）远小于风险。若合规要求"
                 "到期删除，应在平台层统一做，并对 `prompt.compiled` 这类"
                 "不含客户正文的事件单独定策略（本服务的事件 `data` 只有 ref，"
                 "见 T20-57）。"},
    {"id": "S20-55", "kind": "gap",
     "summary": "§1.5 / T20-70 允许 `handling` 取 `retime` 或 `unacceptable`，"
                "并说「视配置」；但方案没给阈值、也没有配置项，本实现只产出 "
                "`trim` 与 `unacceptable`（后者仅在没有 ≥ 目标的档位时）。"
                "于是目标 4.0s、唯一档位 8s 与目标 4.0s、档位 4.2s 得到同一个 "
                "`handling=trim`",
     "handling": "不发明阈值。「剪掉 0.1 秒」与「剪掉一半」是否同一件事是创作/"
                 "成本判断，不是 S20 能定的数；写死一个 20% 会让 S25 的硬过滤"
                 "在某些模态上系统性放行不可用候选。因此 `handling` 只表达"
                 "「档位与目标的关系」，`overrun_seconds` 一并写进 manifest，"
                 "由 S25/S41 按各自策略判定。需要阈值时请 owner 在 catalog 里"
                 "给一个按模态的配置项，S20 照着执行 —— 不要在各服务里各写一份。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- 模板容器
CREATE TABLE IF NOT EXISTS template (
  tenant_id        TEXT NOT NULL,
  template_id      TEXT NOT NULL,
  name             TEXT NOT NULL CHECK (length(name) > 0),
  modality         TEXT NOT NULL CHECK (modality IN (
                     'image','video','audio','tts','music','sfx','lipsync',
                     'upscale','edit')),
  provider         TEXT NOT NULL CHECK (length(provider) > 0),
  current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
  state            TEXT NOT NULL DEFAULT 'ACTIVE'
                     CHECK (state IN ('ACTIVE','ARCHIVED')),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, template_id)
);

-- ---------------------------------------------------------------- 模板版本
CREATE TABLE IF NOT EXISTS template_revision (
  tenant_id            TEXT NOT NULL,
  template_id          TEXT NOT NULL,
  revision             INTEGER NOT NULL CHECK (revision >= 1),
  state                TEXT NOT NULL CHECK (state IN
                         ('DRAFT','PUBLISHED','RETIRED')),
  compatible_schema    TEXT NOT NULL,
  body                 TEXT NOT NULL,
  adapter_revision_min INTEGER NOT NULL CHECK (adapter_revision_min >= 1),
  adapter_revision_max INTEGER NOT NULL,
  model_snapshots      TEXT NOT NULL,
  -- §2.5 的 `adapter_scopes`。方案 §5.1 的 DDL 没有这一列，但 probe 与
  -- 「adapter 是否属于本模板」都靠它；不存就只能猜。
  adapter_scopes       TEXT NOT NULL DEFAULT '[]',
  regression_set_ref   TEXT NOT NULL,
  regression_verdict   TEXT NOT NULL DEFAULT 'pending'
                         CHECK (regression_verdict IN ('pending','pass','fail')),
  content_hash         TEXT NOT NULL CHECK (length(content_hash) = 64),
  author_subject       TEXT NOT NULL,
  published_at         TEXT,
  retired_at           TEXT,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, template_id, revision),
  FOREIGN KEY (tenant_id, template_id) REFERENCES template (tenant_id, template_id),
  CHECK (adapter_revision_max >= adapter_revision_min),
  -- §5.1 的配对约束：发布过就有发布时间，退役就有退役时间。
  --
  -- 曾经写成 `(state = 'PUBLISHED') = (published_at IS NOT NULL)`，双向等价。
  -- 那条约束让**退役永远不可能成功**：RETIRED 行必须保留 `published_at`
  -- （它是发布时刻的事实），于是左边 0、右边 1，CHECK 恒假。任何 RETIRED
  -- 写入都会 IntegrityError，`retire_revision` 一调就 500 —— T20-63…66 因此
  -- 全部不可达。单向后只剩"非 DRAFT 必须有发布时间"，正是原文想表达的。
  CHECK (state = 'DRAFT' OR published_at IS NOT NULL),
  CHECK (state <> 'DRAFT' OR published_at IS NULL),
  CHECK (state <> 'RETIRED' OR retired_at IS NOT NULL)
);

-- 同一模板至多一版 DRAFT：发布前只能有一个在改的版本（§5.1）。
CREATE UNIQUE INDEX IF NOT EXISTS template_revision_one_draft
  ON template_revision (tenant_id, template_id) WHERE state = 'DRAFT';

CREATE INDEX IF NOT EXISTS template_revision_published
  ON template_revision (tenant_id, template_id, published_at DESC)
  WHERE state = 'PUBLISHED';

-- ---------------------------------------------------------------- 供应商映射
CREATE TABLE IF NOT EXISTS adapter_binding (
  tenant_id              TEXT NOT NULL,
  adapter_id             TEXT NOT NULL,
  revision               INTEGER NOT NULL CHECK (revision >= 1),
  provider               TEXT NOT NULL,
  model_snapshot         TEXT NOT NULL,
  field_map              TEXT NOT NULL,
  -- 有序数组 `[{role, position, required?}]`，或
  -- `{"roles":[...], "allow_leftover": bool}`（§3.4 的 `_allow_leftover` 在
  -- 数组形状里没有位置，见 S20-的说明）。
  reference_order_rule   TEXT NOT NULL,
  reference_token_format TEXT NOT NULL,
  token_budget           INTEGER NOT NULL CHECK (token_budget > 0),
  unsupported_optionals  TEXT NOT NULL DEFAULT '[]',
  -- 方案 §5.2 把这些塞在 `field_map`（「参数名或词表」）。在 SQLite 里把
  -- 「词表」「前缀」「区间」混在一个 JSON 列里，就没法在写入时校验形状，
  -- 而形状错误会在编译期才炸。分开存，形状各自校验（S20 内部扩展）。
  phrase_tables          TEXT NOT NULL DEFAULT '{}',
  speaker_prefix         TEXT,
  section_separator      TEXT,
  parameter_ranges       TEXT NOT NULL DEFAULT '{}',
  content_type           TEXT,
  content_hash           TEXT NOT NULL CHECK (length(content_hash) = 64),
  created_at             TEXT NOT NULL,
  PRIMARY KEY (tenant_id, adapter_id, revision),
  -- SQLite 没有 REGEXP，格式校验在 Python 写入路径里做（C11）。
  CHECK (length(model_snapshot) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS adapter_binding_exact
  ON adapter_binding (tenant_id, provider, model_snapshot, adapter_id)
  WHERE model_snapshot <> '*';

CREATE UNIQUE INDEX IF NOT EXISTS adapter_binding_wildcard
  ON adapter_binding (tenant_id, provider, adapter_id)
  WHERE model_snapshot = '*';

-- ---------------------------------------------------------------- 编译产物
CREATE TABLE IF NOT EXISTS compiled_prompt_manifest (
  tenant_id               TEXT NOT NULL,
  manifest_id             TEXT NOT NULL,
  revision                INTEGER NOT NULL DEFAULT 1 CHECK (revision = 1),
  modality                TEXT NOT NULL,

  shot_ref                TEXT NOT NULL,
  -- §3.8：`template_ref` 是**语义锁**，`id` 是 manifest_id，给 S26 与各模态
  -- 服务反查"这段产物由哪版模板编译"。它不能同时充当输入指针，所以输入模板的
  -- `{id, revision, content_hash}` 另存一列（见 CONTRACT_GAPS S20-32）。
  template_ref            TEXT NOT NULL,
  template_source_ref     TEXT NOT NULL,
  adapter_ref             TEXT NOT NULL,
  model_snapshot          TEXT NOT NULL,
  capability_snapshot_ref TEXT NOT NULL,
  binding_set_ref         TEXT NOT NULL,
  dialogue_alignment_ref  TEXT,

  prompt_text             TEXT NOT NULL,
  negative_prompt         TEXT,
  tokens_estimate         INTEGER,
  -- `references` 是 SQL 关键字，列名加后缀。
  refs                    TEXT NOT NULL,
  dialogue_segments       TEXT NOT NULL,
  duration                TEXT NOT NULL,
  parameters              TEXT NOT NULL,
  derived_from            TEXT NOT NULL,
  warnings                TEXT NOT NULL DEFAULT '[]',

  -- §3.9：使用上下文，**不进 content_hash**。
  node_id                 TEXT,
  node_epoch              INTEGER,
  execution_fence         INTEGER,

  content_hash            TEXT NOT NULL CHECK (length(content_hash) = 64),
  compiled_at             TEXT NOT NULL,
  PRIMARY KEY (tenant_id, manifest_id)
);

-- §2.8 第二层：同一输入组只有一份 manifest。
CREATE UNIQUE INDEX IF NOT EXISTS manifest_input_once
  ON compiled_prompt_manifest (
    tenant_id,
    json_extract(shot_ref, '$.id'),
    json_extract(template_source_ref, '$.id'),
    json_extract(template_source_ref, '$.revision'),
    model_snapshot,
    json_extract(adapter_ref, '$.revision'),
    json_extract(binding_set_ref, '$.content_hash'),
    content_hash
  );

CREATE INDEX IF NOT EXISTS manifest_by_shot
  ON compiled_prompt_manifest (tenant_id, json_extract(shot_ref, '$.id'),
                               compiled_at DESC);

-- ---------------------------------------------------------------- 回归结果
CREATE TABLE IF NOT EXISTS compile_regression_result (
  tenant_id          TEXT NOT NULL,
  result_id          TEXT NOT NULL,
  template_id        TEXT NOT NULL,
  template_revision  INTEGER NOT NULL,
  regression_set_ref TEXT NOT NULL,
  verdict            TEXT NOT NULL CHECK (verdict IN ('pass','fail')),
  case_total         INTEGER NOT NULL CHECK (case_total >= 0),
  case_passed        INTEGER NOT NULL CHECK (case_passed >= 0),
  case_failed        INTEGER NOT NULL CHECK (case_failed >= 0),
  report             TEXT NOT NULL,
  started_at         TEXT NOT NULL,
  finished_at        TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  PRIMARY KEY (tenant_id, result_id),
  FOREIGN KEY (tenant_id, template_id, template_revision)
    REFERENCES template_revision (tenant_id, template_id, revision),
  CHECK (case_passed + case_failed = case_total)
);

CREATE INDEX IF NOT EXISTS regression_by_template
  ON compile_regression_result (tenant_id, template_id, template_revision,
                                created_at DESC);
"""
