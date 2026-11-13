"""S36 自己的 schema。表来源是 `plans/S36-semantic-qc.md` §8。

四条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **`evaluator_call` 是"模型到底答没答"的唯一凭据。** §3.3 要求超时后
   `status=failed, reason_code=evaluator_timeout` 并把该 check 归入 abstain。
   没有这张表，"模型说没问题"和"模型没回答"在库里长得一模一样——两者都是
   没有 defect 行。所以每一行必须记 `outcome`（ok/timeout/schema_error/error），
   且一次调用一行，即使结果为空。
2. **`report` 的主键是 `(report_id, report_generation)`，不是 `report_id`。**
   方案 §2.6 要求同一派生键超时重评后 `report_generation + 1`，S39 只认最大代；
   而 §8 DDL 把 `report_id` 写成 `PRIMARY KEY`，又加了一条
   `UNIQUE (report_id, report_generation)`——两者不能同时成立（SQC-6）。
   按 §2.6 的语义取复合主键，否则"重评"在库层面就不可能。
3. **`evaluator_call` 的唯一索引必须带 `report_generation`。**
   方案 §8 的索引是 `(report_id, check_id, window_index, model_snapshot)`；
   既然同一 `report_id` 可以有多代，第二代同一窗口同一模型就会撞上第一代的
   唯一约束，重评直接失败（SQC-7）。
4. **事件与报告同事务。** `outbox` 由平台 SDK 建表（不在本文件），但 `append`
   必须发生在写 `report` + `evaluator_call` 的同一个 `COMMIT` 里（§8.1）：
   分开写就有"报告落库了事件丢了"或"事件发了报告回滚了"的空洞。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`uuid` 存 TEXT。
`boolean` 存 INTEGER 0/1（SQLite 无原生布尔）。
"""

ID_PREFIX = {"report": "qc_", "call": "ec_", "operation": "op_"}

# 本轮**不解决**的缺口。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，
# 不要自行选边」处理：本服务按方案散文实现，把偏差记在这里，不修改契约、
# 不代填决定。凡是写到"C1/C2/C3"的，是 `plans/S36-semantic-qc.md` §11 的编号。
CONTRACT_GAPS = [
    # --- 方案 §11 已登记的 ---
    {"id": "SQC-1", "kind": "gap",
     "summary": "方案 C1：没有 `contracts/quality/s17-expected-state.v2.schema.json`；"
                "S17 实际返回 `{report, defects, expected_state, "
                "exemption_applications}`，其中 `expected_state` 行是 "
                "`{unit_id, entity_id, predicate, expected_value, source_claim_ref, "
                "checkpoint, derivation}`，没有 §7 的 `legal_exceptions[]`",
     "handling": "`expected_state` 行按 predicate 映射到 `VIS.SPACE.PROP_*` / "
                 "`VIS.STATE.*`；§7 形状的合法例外改从命令体 `legal_exceptions` "
                 "读（SQC-22）。接缝仍缺正式 schema，等 owner 补。"},
    {"id": "SQC-2", "kind": "conflict",
     "summary": "方案 C2：`check_manifest.reason_code` 闭集里没有 "
                "`legal_exception`，而 §7 明确要求合法例外用它",
     "handling": "**当前发 §2.4 行 73 明写允许的 `no_such_content`**，"
                 "并把被压制的原始观测（含 confidence 与 observed）留在 "
                 "dimensions 的 `suppressed_observation.<check>` 里。§7 想要的 "
                 "`legal_exception` 是更真的语义，但 qc-report.v2 的 "
                 "reason_code 是闭集：发枚举外的值会让整份报告被下游拒收，"
                 "那比一个略粗的理由码更糟。C2 扩枚举后改回 `legal_exception`。"},
    {"id": "SQC-3", "kind": "gap",
     "summary": "方案 C3：没有 `contracts/quality/vlm-response.v2.schema.json`，"
                "模型输出到 defect 的映射只能是散文",
     "handling": "内核自定一份**临时**响应形状（`service.VLM_REQUIRED` + "
                 "`service.VLM_OPTIONAL`），不合形状一律记 `schema_error` → "
                 "failed → abstain；契约落地后整体替换，不做兼容解析。"},
    {"id": "SQC-4", "kind": "gap",
     "summary": "方案 C4 已过期：`event-topics.v2.json` 现在有 topic 35 "
                "`quality.semantic.completed` → `quality.semantic-completed.v2.schema.json`。"
                "但那份事件要求 `manifest_ref.content_hash`（QSC-2），"
                "而 §8 DDL 只有 id + revision 两列",
     "handling": "report 表补 `manifest_content_hash` 列，落库时从 "
                 "`execution_context.manifest_ref` 一并存下；事件按完整 manifest_ref 发。"},
    {"id": "SQC-5", "kind": "conflict",
     "summary": "方案 C5：`expected_state_ref` 在请求体（完整 ref）、DDL "
                "（jsonb 注释）、事件（`{service_id, ref_revision}`）三处三种形状；"
                "事件还丢了 id 与 content_hash（契约 QSC-3）",
     "handling": "库里存完整 ref（请求体形状，不丢字段）；事件按契约只发 "
                 "`{service_id, ref_revision}`。两个名字指同一个量，"
                 "实现只在一处翻译。"},

    # --- 实现时新发现的 ---
    {"id": "SQC-6", "kind": "conflict",
     "summary": "§8 DDL：`report_id text PRIMARY KEY` 与 "
                "`UNIQUE (report_id, report_generation)` 互斥，而 §2.6 要求"
                "同一 report_id 多代",
     "handling": "复合主键 `(report_id, report_generation)`；`supersedes` "
                 "指向上一代。否则超时重评在库层面写不进去。"},
    {"id": "SQC-7", "kind": "conflict",
     "summary": "§8 DDL 的 `evaluator_call` 唯一索引 "
                "`(report_id, check_id, window_index, model_snapshot)` 漏了 "
                "`report_generation`，第二代同窗口同模型必然撞第一代",
     "handling": "索引加上 `report_generation`，并把它作为指向 report 的复合外键。"},
    {"id": "SQC-8", "kind": "conflict",
     "summary": "§3.2 的 VLM 请求体（`model_role/images/prompt_template_ref/"
                "response_schema_ref/allowed_vocab`）与 "
                "`contracts/generation-command.v2.schema.json` 的 "
                "`modality=understanding` 分支（`media_refs/question/"
                "context_pack_ref/output_schema_id`，且 `additionalProperties:false`）"
                "不是同一组字段",
     "handling": "按契约的 4 个字段打包：`media_refs` ← 采样帧的 S28 ref，"
                 "`question` ← 该窗口的检查问题，`context_pack_ref` ← "
                 "期望状态 ref（无则分镜 ref），`output_schema_id` ← "
                 "`quality/vlm-response.v2`。§3.2 的概念字段不再原样上送。"},
    {"id": "SQC-9", "kind": "conflict",
     "summary": "S26 `POST /generation-jobs` 要求 `route_decision_id`，且其 "
                "`create_job` 还要一条可行的 S25 决策（quote_valid_to / "
                "price_snapshot_id / selected.normalized_input）；"
                "S36 的 `depends_on` 里没有 S25，§3.2 也没提路由决策",
     "handling": "调用方在命令里带 `evaluator_route_decision_id`；没有就 "
                 "503 `ROUTE_DECISION_UNAVAILABLE`，**绝不自己编一条路由**"
                 "（与 S33 对 S25 的处理一致）。"},
    {"id": "SQC-10", "kind": "gap",
     "summary": "报告的 `context` 与事件都要 `shot_lineage_id` / `run_id`，"
                "但 §9 的请求体没有这两个字段，`execution-context.v2` 的 "
                "`required` 里也没有（且 `additionalProperties:false` 放不进 context）",
     "handling": "接受命令体顶层 `shot_lineage_id` / `run_id`；缺任一个就 "
                 "422 `EXECUTION_CONTEXT_INCOMPLETE`。血缘是 S39 归并键的一半，"
                 "拿分镜 id 顶替等于伪造身份，不做。"},
    {"id": "SQC-11", "kind": "gap",
     "summary": "S36 要做的是「画面和绑给它的 ShotSpec 对吗」，但 `depends_on` "
                "没有 S06，§9 的命令也只给 `shot_spec_ref` 一个引用",
     "handling": "命令体可选内联 `shot_spec`（shot_type / declared_axis_crossing 等）。"
                 "缺 shot_type 时采样退回 `dialogue` 策略，并在 `dimensions` 里"
                 "留 `sampling_shot_type_defaulted`；不假装知道镜头类型。"},
    {"id": "SQC-12", "kind": "gap",
     "summary": "`qc-report.v2` 的 `subject.media.video` 要求 codec / pix_fmt / "
                "color_range / is_vfr / frame_count，S28 的 revision 读只给 "
                "容器、时长、宽高、帧率、声道、采样率",
     "handling": "命令体可选 `artifact.media` 覆盖；否则 codec/pix_fmt 记空串、"
                 "color_range 记 `unknown`、frame_count 由时长×帧率推导、"
                 "is_vfr 记 false **并在 dimensions 标注 `media_facts_inferred`**。"
                 "`is_vfr` 这一项是推断值，不得用于技术层判定（那是 S35 的事）。"},
    {"id": "SQC-13", "kind": "conflict",
     "summary": "§8.1 要求「同 command_id、前次 IN_PROGRESS → 202 并返回同一 "
                "operation_id」，但 idempotency-sdk 的 `begin()` 只抛 "
                "`CommandInProgress`，拿不到在途记录的 result，同侪实现"
                "（S25/S33）一律 409 `COMMAND_IN_FLIGHT`",
     "handling": "按同侪：409 + `Retry-After: 1`。202 复用需要 SDK 暴露在途 "
                 "记录，不在本服务内绕过。"},
    {"id": "SQC-14", "kind": "conflict",
     "summary": "§8.1 把同键异体命名为 `409 COMMAND_PAYLOAD_MISMATCH`，"
                "SDK 抛的 `CommandConflict.code` 是 `IDEMPOTENCY_KEY_REUSED`",
     "handling": "响应码用方案的名字（这是本服务对外文档里的码），"
                 "`details.sdk_code` 记 SDK 的原值，不吞掉。"},
    {"id": "SQC-15", "kind": "gap",
     "summary": "§4 说连续窗口升级阈值「记在 `check-registry` 的 "
                "`promotion_windows`」，但 check-registry.v2 schema 没有这个字段，"
                "方案也没给数值",
     "handling": "值放在 `registry.PROMOTION_WINDOWS`，**只对 model 检测的 "
                 "minor 生效**。blocker 绝不做单窗口平滑——「isolated 所以不算」 "
                 "就是把未知读成通过。"},
    {"id": "SQC-16", "kind": "gap",
     "summary": "`contracts/openapi.v2.yaml` 里没有 `/semantic-checks` 与 "
                "`/semantic-reports/{id}`",
     "handling": "按方案 §9 实现 HTTP 层；契约待补。"},
    {"id": "SQC-17", "kind": "gap",
     "summary": "S26 `GET /generation-jobs/{id}` 只回 `output.artifact_refs`，"
                "要读到模型输出正文还需一个 S28「读 artifact 内容」的接口，"
                "仓库里没有",
     "handling": "内核接受 job 输出里内联的 `result` 对象（视作已从 S28 取回的"
                 "generated_metadata），并把这件事记在这里；真实接线时需要契约。"},
    {"id": "SQC-18", "kind": "gap",
     "summary": "§8 的 outbox DDL 有 `UNIQUE (aggregate_id, aggregate_sequence, "
                "event_type)`，平台 `event_sdk.OUTBOX_DDL` 只有 `event_id` 主键"
                "（同 MRT-20）",
     "handling": "依赖 `event_id` 主键 + `(report_id, report_generation)` 唯一；"
                 "不做平台表之外的补丁。"},
    {"id": "SQC-19", "kind": "needs_owner",
     "summary": "事件字段 `model_disagreement` 只说「同 S35 结构 + 额外字段」，"
                "没定义量纲（契约 x-needs-owner）",
     "handling": "取各高风险 check 的 `1 - Jaccard` 最大值，域 0..1；"
                 "同时把逐 check 的值写进 `dimensions`，量纲错了能回查。"},
    {"id": "SQC-21", "kind": "conflict",
     "summary": "同一个越轴检查在 §2.3 的职责表里叫 `VIS.AXIS.SHOT_CROSSING`，"
                "在 §5.5 的注册表里叫 `VIS.CONTINUITY.AXIS_CROSSING`",
     "handling": "取注册表节（§5.5）的名字——报名是注册表的权威面。"
                 "`VIS.AXIS.*` 会在 check-registry 校验里被当作另一条检查，"
                 "不能两个都登记（重复检查会被 S39 当成两条缺陷）。"},
    {"id": "SQC-22", "kind": "gap",
     "summary": "§7 的合法例外形状（`{exception_id, kind, applies_to, "
                "suppresses_check_ids, declared_by}`）在 S17 的现有响应里不存在："
                "S17 只有 `exemption_application`（`{exemption_id, defect_id}`）",
     "handling": "从命令体 `legal_exceptions` 读（调用方替 S17 传递），"
                 "且**只认带 `declared_by.service_id == \"S17\"` 的条目**"
                 "（§7 第 3 条）。没有 declared_by 的例外一律丢弃并判普通检查"
                 "（E25-S6）。"},
    {"id": "SQC-23", "kind": "conflict",
     "summary": "SQC-20 的 `VIS.SAFE_AREA.TEXT_OVERLAP` 登记了 "
                "`default_action=re_render_subtitles` 来满足族约束，而这个动作"
                "实际改不了烧录图形",
     "handling": "`never_auto_repair=true` 会把所有自动路由的候选集清空、"
                 "强制 human_review；`default_action` 只是让校验器能加载。"
                 "S39 读 `never_auto_repair` 而不是 `default_action`。"},
    {"id": "SQC-20", "kind": "conflict",
     "summary": "§5.5 把 `VIS.SAFE_AREA.TEXT_OVERLAP` 定为 `subtitle` 型，"
                "而 subtitle 动作族只有 `realign_subtitles` / "
                "`re_render_subtitles`——都改不了烧录进画面的图形",
     "handling": "登记 `never_auto_repair=true` + `no_auto_reason`，"
                 "`default_action` 取族内的 `re_render_subtitles` 以满足校验；"
                 "实际路由必然落 human_review。等 owner 决定它该不该是 subtitle 型。"},

    # --- 实现期关于「双评估」与「证据」的编号（service.py 引用）---
    {"id": "SQC-24", "kind": "gap",
     "summary": "§3.2 的幂等键公式里没有「评估器角色」，而双评估要给同一窗口"
                "投两次；两边的 key 相同就会被 S26 判成同一命令，第二次拿回"
                "第一个 job——所谓第二意见其实是同一个答案",
     "handling": "内核在实际 key 末尾加 `:{role}`（primary/secondary）。"
                 "这是内核自定的扩展，不写回契约；接缝需要 owner 认可。"},
    {"id": "SQC-25", "kind": "gap",
     "summary": "S35 §2.7 第 7 步写 `abstain(\"low_confidence\", [])`，而 "
                "qc-report.v2 的 `abstain.failed_check_ids` 是 minItems 1——"
                "照抄伪代码会让报告被 schema 拒收",
     "handling": "带上当时置信度最低的已评估 check；一个都没有时退回一个"
                 "登记在册的 check id。绝不发空数组。"},
    {"id": "SQC-26", "kind": "gap",
     "summary": "`defect.evidence_artifact_ids` 是 minItems 1，而 S36 目前没有"
                "「合成裁剪帧并落盘到 S28」的能力（见 SQC-17 的相邻缺口）",
     "handling": "回填**真实源素材 id + 窗口号**（`<artifact_id>.w<n>`），"
                 "满足格式但不伪造一个不存在的裁剪引用。真实证据需要 S28 写入，"
                 "等契约。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS report (
  report_id           TEXT NOT NULL,
  report_generation   INTEGER NOT NULL,
  tenant_id           TEXT NOT NULL,
  series_id           TEXT NOT NULL,
  episode_id          TEXT NOT NULL,
  branch_id           TEXT NOT NULL,
  shot_id             TEXT NOT NULL,
  shot_lineage_id     TEXT NOT NULL,
  logical_node_id     TEXT NOT NULL,
  node_epoch          INTEGER NOT NULL,
  manifest_id         TEXT NOT NULL,
  manifest_revision   INTEGER NOT NULL,
  -- QSC-4/QSC-2：事件要 manifest_ref.content_hash，§8 DDL 没有这一列。
  -- 落库时从 execution_context 一并存下，否则事件只能少发字段或回查 S05。
  manifest_content_hash TEXT NOT NULL,
  run_id              TEXT NOT NULL,
  -- execution_fence 是 run/branch 坐标，node_epoch 是单节点坐标。
  -- 两列分开存，永不互相换算（AGENTS.md）。
  execution_fence     INTEGER NOT NULL,
  artifact_sha256     TEXT NOT NULL,
  shot_spec_revision  INTEGER NOT NULL,
  -- S17 ref + revision 的完整形状（SQC-5）；NULL 表示本次没拿到期望状态，
  -- 依赖它的 check 记 dependency_unavailable 而不是跳过。
  expected_state_ref  TEXT,
  evaluator_version   TEXT NOT NULL,
  policy_version      TEXT NOT NULL,
  sampling_version    TEXT NOT NULL,
  decision            TEXT NOT NULL
                      CHECK (decision IN ('pass','repair','human_review','abstain')),
  coverage_ratio      REAL NOT NULL CHECK (coverage_ratio >= 0 AND coverage_ratio <= 1),
  attempt_slot_id     TEXT,
  -- QCReport v2 全文。别的服务靠它重算 decision 与 defects 的一致性，
  -- 所以这里的列只是索引用的投影，权威是 payload。
  payload             TEXT NOT NULL,
  content_hash        TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  -- SQC-6：§2.6 要求同一 report_id 多代；PK 只能是复合的。
  PRIMARY KEY (report_id, report_generation)
);

CREATE INDEX IF NOT EXISTS report_lineage_idx
  ON report (tenant_id, branch_id, shot_lineage_id, created_at DESC);

-- VLM 调用明细：费用归因、复现、以及"模型到底答没答"的取证。
CREATE TABLE IF NOT EXISTS evaluator_call (
  call_id             TEXT PRIMARY KEY,
  -- SQC-7：唯一索引与复合外键都要带 report_generation，否则第二代重评撞第一代。
  report_id           TEXT NOT NULL,
  report_generation   INTEGER NOT NULL,
  tenant_id           TEXT NOT NULL,
  check_id            TEXT NOT NULL,
  window_index        INTEGER NOT NULL,
  provider_job_id     TEXT NOT NULL,
  attempt_ref         TEXT NOT NULL,
  -- 主/副评估器必须可区分，它参与唯一索引。
  model_snapshot      TEXT NOT NULL,
  prompt_template_rev INTEGER NOT NULL,
  input_frame_count   INTEGER NOT NULL,
  latency_ms          INTEGER,
  outcome             TEXT NOT NULL
                      CHECK (outcome IN ('ok','timeout','schema_error','error')),
  raw_response_ref    TEXT,
  -- 从 S23 投影，非权威（AGENTS.md：钱的唯一权威是 S23）。
  cost_micro_units    INTEGER,
  created_at          TEXT NOT NULL,
  FOREIGN KEY (report_id, report_generation)
    REFERENCES report (report_id, report_generation)
);

CREATE UNIQUE INDEX IF NOT EXISTS evaluator_call_uniq
  ON evaluator_call (report_id, report_generation, check_id, window_index,
                     model_snapshot);
"""
