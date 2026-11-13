"""S37 自己的 schema。表来源是 `plans/S37-audio-alignment.md` §5。

四条结构性事实：

1. **S37 只测量，不修复，也不改剧本。** `defects[].recommended_actions` 是给
   S39 的建议；本服务没有 artifact 写入口、没有重生成表。`AUD.TEXT.SCRIPT_CONFLICT`
   连 defect 都不产——ASR 说 B、脚本说 A 时本服务不下结论谁对（§3.1 末段、
   service.yaml recovery：「文本冲突不自动改剧本」）。
2. **三条证据流分开。** 词对不对（`AUD.TEXT.*`）、字在什么时间
   （`alignment_unit`）、嘴和声音对不对得上（`AUD.LIPSYNC.*`）各有自己的表与
   defect 类型，绝不合成一个"音频得分"（§1 首表）。合成之后 S39 就选不出动作
   ——重配、重对齐、重做口型是三件不同的事。
3. **`execution_fence` 与 `node_epoch` 是两列，永不合并。**
   `execution_fence` 属于 run/branch，`node_epoch` 属于单个 `logical_node`
   （AGENTS.md「版本与一致性速查」）。本服务只透传这两列，不比较、不换算，
   也**不**用它们推进任何接受指针——接受决策不在 S37。
4. **`node_epoch` 进的是报告坐标，不是接受指针。** 报告记录"这份对齐结果是在
   哪个 epoch 的哪个镜头上测出来的"，这样旧 epoch 的结论不会冒充当前结论。
   它既不是"当前 epoch"的权威值（那在 S08），也不是推进指针的依据。

方案里的 `jsonb` 存 TEXT（JCS）、`timestamptz` 存定宽 UTC 字符串、
`numeric(5,4)` 存 REAL（取值域由 CHECK 卡住）、`text[]` 存 JSON 数组、
`uuid` 存 TEXT。

与 §5 DDL 的偏差都在 `CONTRACT_GAPS` 里有条目，不自行选边：

- 主键带 `report_generation`：§2.6 的派生键重评会沿用同一 `report_id`、
  只升代数，两句话不能同时是"`report_id` 唯一"（S37-1）。
- `alignment_unit` / `speaker_segment` 的主键带 `report_generation` 与
  `tenant_id`：不带代数就会让第 2 代报告覆盖第 1 代的时间码（S37-2）。
- 多出 `report_check` 子表：`check_manifest` 的词表 CHECK 只有落在真实列上
  才拦得住东西（S37-3）。
- 多出 `asr_cache` 表：§8 要求"按 `audio_track_hash` 缓存复用 ASR"，
  缓存键按 QAC-3 补了 `lexicon_revision` 与 `voice_binding_ref`（S37-4）。
"""

ID_PREFIX = {"report": "qc_", "defect": "df_", "call": "call_",
             "operation": "op_"}

SOURCE = "S37"
SCHEMA_VERSION = "2.0"
QC_LAYER = "alignment"
EVENT_TYPE = "quality.alignment.completed"
TOPIC = "drama.audio-alignment.v2"

# 授权投影的消费者名。inbox 按 `consumer + event_id` 去重。
CONSUMER_VOICE_AUTH = "s37.voice-authorization.v1"

# §2.5 / §2.3 的词表（与 qc-report.v2 是同一套闭集，CHECK 与代码共用）。
DECISIONS = ("pass", "repair", "human_review", "abstain")
CHECK_STATUSES = ("evaluated", "not_applicable", "not_evaluated", "failed")
REASON_CODES = (
    "ok", "no_such_content", "no_dialogue", "no_face", "asset_unavailable",
    "evaluator_timeout", "evaluator_error", "dependency_unavailable",
    "sampling_not_selected", "policy_disabled",
)
ABSTAIN_REASON_CODES = (
    "incomplete_coverage", "evaluator_timeout", "evaluator_error",
    "model_disagreement", "low_confidence", "dependency_stale",
    "asset_unavailable",
)
SEVERITIES = ("blocker", "major", "minor", "info")
DEFECT_TYPES = (
    "identity", "anatomy", "hands", "costume", "prop", "continuity",
    "camera_adherence", "action_adherence", "temporal", "subtitle",
    "lipsync", "voice", "technical", "audio_mix",
)
RECOMMENDED_ACTIONS = (
    "reingest", "transcode", "keyframe_edit", "regenerate_video",
    "temporal_edit", "redo_lipsync", "realign_subtitles",
    "re_render_subtitles", "remix", "re_synthesize_speech", "human_review",
    "change_spec",
)

# S37 只处理带音轨的素材。image / subtitle_track 没有一条 AUD.* 检查适用，
# 硬接会产出一份"没查任何东西的 pass"。
SUPPORTED_ARTIFACT_KINDS = ("video", "audio", "master")

AUDIO_SOURCE_MODES = ("embedded", "external")

# status → 允许的 reason_code（§2.5「not_applicable 必须给出可验证理由」+
# §3.3「口型检查没跑完不是通过」）。
STATUS_REASON_PAIRS = {
    "evaluated": ("ok",),
    "not_applicable": ("no_such_content", "no_dialogue", "no_face"),
    "not_evaluated": ("sampling_not_selected", "policy_disabled",
                      "dependency_unavailable", "asset_unavailable"),
    "failed": ("evaluator_timeout", "evaluator_error",
               "dependency_unavailable", "asset_unavailable"),
}

# 本轮**不解决**的缺口。`contracts/quality.alignment-completed.v2.schema.json`
# 里已有 QAC-1/2/3 三条 `x-conflicts` 与四条 `x-needs-owner`，那些是待 owner
# 决定的事项，按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要自行选边」。
CONTRACT_GAPS = [
    {"id": "S37-1", "kind": "conflict",
     "summary": "§5 DDL 把 report_id 设成 PRIMARY KEY，而 §2.6/§8 要求同一派生键"
                "的第 N 次评估沿用同一 report_id、只把 report_generation 升到 N",
     "handling": "主键改为 (report_id, report_generation)；GET 默认取最大代数。"
                 "报告层幂等由 (tenant_id, derived_key, report_generation) 唯一索引"
                 "兜底，命令层幂等仍是 tenant_id + command_id + request_hash。"},
    {"id": "S37-2", "kind": "conflict",
     "summary": "§5 的 alignment_unit / speaker_segment 主键只有 (report_id, "
                "unit_index)，没有代数也没有租户；第 2 代报告会覆盖第 1 代的时间码，"
                "S38 拿到的字级时间码可能属于已被 supersede 的那一版",
     "handling": "两张子表的主键都加上 report_generation，并加 tenant_id 列与"
                 "外键；units 查询默认只返回最大代数。§6 的响应形状不变。"},
    {"id": "S37-3", "kind": "gap",
     "summary": "§5 没有 check_manifest 的落库表，manifest 只存在于 payload JSON；"
                "而「评估器超时不是 pass」要靠 status/reason_code 的配对约束才拦得住",
     "handling": "新增 report_check 子表，把 status / reason_code 的词表与配对写成"
                 "CHECK（与 S35 的 report_check 同构）。§5 其余字段不改。"},
    {"id": "S37-4", "kind": "conflict",
     "summary": "QAC-3：§8 的 ASR 缓存键 `qc:s37:{audio_track_hash}:{utterance_id}:"
                "{asr_model_snapshot}` 不含 lexicon_revision 与 voice_binding_ref；"
                "换了发音词典或音色绑定后 audio_track_hash 不变，缓存仍会命中旧结果",
     "handling": "缓存键（`asr_cache.cache_key`）按 QAC-3 补上 lexicon_revision 与"
                 "voice_binding_ref 的摘要；同一段音频在词典换版后会重新对齐，"
                 "不会复用旧 token。契约里的键格式待 owner 同步改。"},
    {"id": "S37-5", "kind": "gap",
     "summary": "§2 的 ASR / forced alignment / 说话人分离 / 口型 / 音色评估全部"
                "通过 S26 的 understanding job 完成，但 contracts 里没有"
                "`alignment-analysis.v2.schema.json`，S26 的读面也只有 "
                "`/generation-jobs/{id}`，没有 understanding 的产物形状",
     "handling": "S37 与 S26 之间用一份**私有分析文档**（`analysis_version: "
                 "s37-analysis.v1`，字段见 service.py 模块头）。它是注入式的："
                 "未配置 S26 时命令 503，绝不产出没有分析的空报告。文档升成契约"
                 "或并入 S26 读面，由 owner 决定。"},
    {"id": "S37-6", "kind": "conflict",
     "summary": "§9 E25-A1 断言 `evaluator_call.outcome='timeout'`（小写），"
                "§5 DDL 的 CHECK 写的是 ('OK','ERROR','TIMEOUT')（大写）",
     "handling": "按验收用例存小写 'ok'/'error'/'timeout'，CHECK 也只收小写——"
                 "两处只能有一处是对的，验收清单优先。DDL 措辞待 owner 更正。"},
    {"id": "S37-7", "kind": "conflict",
     "summary": "§3.1 的 AUD.TEXT.SCRIPT_CONFLICT 与 §3.3 的 "
                "AUD.LIPSYNC.FACE_NOT_FOUND 在表里 type 与严重度都是「—」，"
                "而 check-registry.v2 的每条注册项都必须有 defect_type / "
                "severity_default / default_action",
     "handling": "两条都按**观测型**登记：type=voice（前者）/ lipsync（后者）、"
                 "severity=info、never_auto_repair=true。内核给它们产出的结论是"
                 "`not_applicable`/`evaluated` 而没有 defect——注册表只表达"
                 "「这条检查归本服务、动作链是哪一族」，不强迫它产 defect。"},
    {"id": "S37-8", "kind": "conflict",
     "summary": "§3.4 的表把 AUD.VOICE.EMBEDDING_DRIFT 的严重度写成 blocker，"
                "同一节的散文又说「连续 3 窗口命中才升级」；"
                "check-registry.v2 每条只有一对 severity_default / severity_max",
     "handling": "登记 severity_default=major、severity_max=blocker，内核按"
                 "连续窗口数决定实际档位：单窗口超阈仍出 major（不能因为「只有一窗」"
                 "就当没看见），连续 ≥3 窗口升 blocker，escalation_rule 记在注册表。"},
    {"id": "S37-9", "kind": "conflict",
     "summary": "§3.3 把 AUD.LIPSYNC.OFFSET 写成「blocker（>120ms）/ major（>60ms）」，"
                "同一行两个严重度；schema 只有单值",
     "handling": "登记 severity_default=major、severity_max=blocker；内核按实测"
                 "|offset| 分档（>60ms major、>120ms blocker），实测值进 "
                 "`measured`，上限记在注册表。E24-A1/A2 正是测这两档。"},
    {"id": "S37-10", "kind": "conflict",
     "summary": "§3.1 把 AUD.TEXT.SUBSTITUTION 写成「blocker（关键台词）」，"
                "关键与否是脚本属性，不是检查的固定严重度",
     "handling": "登记 severity_default=major、severity_max=blocker；命令体对白项"
                 "带 `critical: true` 时升 blocker。缺这个标记不猜——按 major 出，"
                 "并把判断依据写进 defect.observed。"},
    {"id": "S37-11", "kind": "gap",
     "summary": "§2.1 的「口型检查必须用连续音视频」在 schema 里没有可表达的字段；"
                "external 音频（配音 stem）没有同基准确视频，做不了口型",
     "handling": "`audio_source.mode=external`、或分析文档没有 video 段、或人脸"
                 "缺失时，五条 AUD.LIPSYNC.* 一律 not_applicable（理由分别是 "
                 "no_such_content / no_face），绝不按「没发现偏移」记 pass。"},
    {"id": "S37-12", "kind": "gap",
     "summary": "§2.1 的 VFR → abstain(`evaluator_error`, 理由 `vfr_unsupported`)："
                "`vfr_unsupported` 不在 abstain.reason_code 的 7 值闭集里，"
                "manifest 的 reason_code 10 值闭集里也没有",
     "handling": "manifest 记 `failed / evaluator_error`，abstain 记 "
                 "`evaluator_error`，具体理由 `vfr_unsupported` 写进 "
                 "`abstain.detail` 与对应 check 的 dimension。帧-时间映射不唯一时"
                 "不硬算，也不把没算的那一段读成通过。"},
    {"id": "S37-13", "kind": "gap",
     "summary": "§6 的 `GET /alignment-reports/{id}/units` 承诺返回 time_base 与"
                "字级区间，但 contracts 里没有 `alignment-units.v2.schema.json`"
                "（方案 C1 已承认），S38 无法按契约校验这份响应",
     "handling": "响应形状按 §6 的示例实现（`report_id` / `time_base` / `units` / "
                 "`speaker_segments`），字段名与示例逐字一致；新增契约由 owner 做，"
                 "本服务不改 contracts。"},
    {"id": "S37-14", "kind": "needs_owner",
     "summary": "QAC-2：§7 的事件示例比 §5 的 DDL 少 node_epoch / run_id / "
                "execution_fence / manifest_id / manifest_revision / "
                "report_generation 六个 NOT NULL 字段",
     "handling": "以 DDL 与 "
                 "`contracts/quality.alignment-completed.v2.schema.json` 为准，"
                 "20 个 required 字段全部发；示例缺字段是实现者最容易照抄错的地方，"
                 "建议 owner 补齐 §7 的示例。"},
    {"id": "S37-15", "kind": "needs_owner",
     "summary": "QAC-1：S37 的事件用扁平 manifest_id + manifest_revision，"
                "S35/S36 用嵌套 manifest_ref（带 content_hash）；S39 要为同一条"
                "信息写两套解析，且 S37 那份没有 content_hash",
     "handling": "事件按 S37 自己的形状发（扁平两列，不加 content_hash——"
                 "凭空补一个值等于伪造「这份报告针对的就是当前 manifest 内容」的"
                 "证据）。报告 payload 的 context.manifest_ref 仍按 QCReport v2 "
                 "要求带三字段。"},
    {"id": "S37-16", "kind": "needs_owner",
     "summary": "QAC-3 的另一半：事件体没有 lexicon_revision 与 voice_binding_ref，"
                "而这两列决定同一段音频为什么得到同一个结论（§5 行 165–166）",
     "handling": "两列都落库并在报告 payload 的 evaluator/dimensions 里留痕，"
                 "但**不**往事件里加字段（schema 是 additionalProperties:false，"
                 "加字段就是违约）。S39 需要复算时回查报告。"},
    {"id": "S37-17", "kind": "gap",
     "summary": "§3.1 的 AUD.TEXT.MISSING_WORD / EXTRA_WORD 需要「脚本 vs ASR」"
                "的比对，但 S37 不拥有对白文本（§1 末段：权威在 S06/S04）；"
                "命令体只带 `dialogue.utterances[].text` 的副本，没有 text_revision "
                "对应的内容哈希",
     "handling": "只用命令体给的对白做逐字比对，把 `text_revision` 记进 dimension；"
                 "不向 S06 回读、不缓存对白。比对结论永远以「ASR 听到的 vs 这次命令"
                 "声明的文本」表述，不宣称「剧本错了」。"},
    {"id": "S37-18", "kind": "gap",
     "summary": "§3.4 的 AUD.VOICE.CHARACTER_MIXUP 是跨镜比较（同一角色在不同"
                "镜头的音色），而一次命令只处理一个 artifact",
     "handling": "跨镜距离由分析文档提供（`voice.cross_shot_distance`，分析器在"
                 "同一角色的其他镜头上算好后回传）。本服务不读别的镜头、不跨库 "
                 "JOIN；单镜头内无法比较时该条 not_applicable(no_such_content)。"},
    {"id": "S37-19", "kind": "conflict",
     "summary": "§11 之外的开关问题：方案没有定义 enable 开关，而仓库约定"
                "「所有生产开关默认关闭」。同时 check_manifest 是 minItems 1，"
                "白名单为空时执行命令会产出一份空清单的 pass",
     "handling": "`AUDIO_ALIGNMENT_ENABLED` 默认 false，关闭时命令 503 "
                 "SERVICE_DISABLED；开关打开但 `AUDIO_ALIGNMENT_CHECKS` 为空时 422 "
                 "NO_CHECKS_ENABLED。两种情况都不产报告。开关名与默认值待 owner 确认。"},
    {"id": "S37-20", "kind": "gap",
     "summary": "§4.1 承诺字级时间码精度 ±40ms（置信度 ≥0.8）/ 音素级 ±20ms，"
                "但精度取决于 ASR 与分析器的实现，S37 无法在自己的库里验证",
     "handling": "报告随 `alignment_unit.ci_low/ci_high` 与 `confidence` 一起交出，"
                 "让 S38 按区间设计字幕卡拉长（§4.1 末段要求「不能让 S38 猜」）；"
                 "本服务只保证透传，不在库里断言 ±40ms 成立。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 对齐质检报告（不可变，按代数追加） ----------
CREATE TABLE IF NOT EXISTS report (
  report_id             TEXT NOT NULL,
  report_generation     INTEGER NOT NULL CHECK (report_generation >= 1),
  -- §2.6 的派生键；按它查重而不是按列比对（列少一列就会把两代混成一代）。
  derived_key           TEXT NOT NULL CHECK (length(derived_key) = 64),
  tenant_id             TEXT NOT NULL,
  project_id            TEXT NOT NULL,
  series_id             TEXT NOT NULL,
  episode_id            TEXT NOT NULL,
  branch_id             TEXT NOT NULL,
  shot_id               TEXT NOT NULL,
  shot_lineage_id       TEXT NOT NULL,
  logical_node_id       TEXT NOT NULL,
  -- AGENTS.md：execution_fence 管 run/branch，node_epoch 管单个 logical_node。
  -- 两列分开存、绝不合并、绝不换算；本服务只记录，不推进任何接受指针。
  node_epoch            INTEGER NOT NULL CHECK (node_epoch >= 1),
  manifest_id           TEXT NOT NULL,
  manifest_revision     INTEGER NOT NULL CHECK (manifest_revision >= 1),
  run_id                TEXT NOT NULL,
  execution_fence       INTEGER NOT NULL CHECK (execution_fence >= 1),
  artifact_id           TEXT NOT NULL,
  artifact_sha256       TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
  -- §8：音轨单独 hash。视频重编码而音轨没变时，ASR 结果可以复用。
  audio_track_hash      TEXT NOT NULL CHECK (length(audio_track_hash) >= 1),
  artifact_kind         TEXT NOT NULL CHECK (artifact_kind IN (
                          'video', 'audio', 'master')),
  language              TEXT NOT NULL CHECK (length(language) >= 1),
  asr_model_snapshot    TEXT NOT NULL,
  align_model_version   TEXT NOT NULL,
  -- §5：null = 未使用发音词典。
  lexicon_revision      INTEGER CHECK (
                          lexicon_revision IS NULL OR lexicon_revision >= 1),
  voice_binding_ref     TEXT,                -- JCS 原文
  evaluator_version     TEXT NOT NULL,
  policy_version        TEXT NOT NULL,
  sampling_version      TEXT NOT NULL,
  decision              TEXT NOT NULL CHECK (decision IN (
                          'pass', 'repair', 'human_review', 'abstain')),
  decision_reason       TEXT NOT NULL CHECK (length(decision_reason) >= 1),
  confidence            REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  coverage_ratio        REAL NOT NULL CHECK (coverage_ratio >= 0 AND coverage_ratio <= 1),
  -- 合规线随报告一起存：只存 0.70 而不存那条线，"覆盖不足所以弃权"在库层不可复核。
  required_coverage_min REAL NOT NULL CHECK (
                          required_coverage_min >= 0 AND required_coverage_min <= 1),
  defect_count          INTEGER NOT NULL CHECK (defect_count >= 0),
  major_count           INTEGER NOT NULL CHECK (major_count >= 0),
  blocker_count         INTEGER NOT NULL CHECK (blocker_count >= 0),
  failed_check_count    INTEGER NOT NULL CHECK (failed_check_count >= 0),
  attempt_slot_id       TEXT,
  supersedes_report_id  TEXT,
  supersedes_generation INTEGER CHECK (
                          supersedes_generation IS NULL
                          OR supersedes_generation >= 1),
  payload               TEXT NOT NULL,        -- 完整 QCReport v2
  content_hash          TEXT NOT NULL CHECK (length(content_hash) = 64),
  created_at            TEXT NOT NULL,
  PRIMARY KEY (report_id, report_generation),
  CONSTRAINT report_supersedes_paired CHECK (
    (report_generation = 1 AND supersedes_report_id IS NULL
       AND supersedes_generation IS NULL)
    OR (report_generation > 1 AND supersedes_report_id IS NOT NULL
       AND supersedes_generation = report_generation - 1)),
  -- 「未知不能当 pass」在库里的形状：弃权必须有数据层可验证的理由。
  CONSTRAINT report_abstain_has_failed CHECK (
    decision <> 'abstain'
    OR failed_check_count > 0
    OR coverage_ratio < required_coverage_min),
  CONSTRAINT report_pass_has_no_finding CHECK (
    decision <> 'pass' OR (blocker_count = 0 AND major_count = 0)),
  CONSTRAINT report_repair_has_finding CHECK (
    decision <> 'repair' OR (blocker_count + major_count) > 0),
  -- 无对白镜头的 no-op（§8.1）必须留下"音频没分析"的痕迹，否则它会被误读成
  -- "分析了且没问题"。
  CONSTRAINT report_analysis_provenance CHECK (
    coverage_ratio > 0 OR decision = 'pass' OR decision = 'abstain')
);
CREATE INDEX IF NOT EXISTS ix_report_lookup
  ON report (tenant_id, branch_id, shot_lineage_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_report_artifact
  ON report (tenant_id, artifact_sha256);
CREATE INDEX IF NOT EXISTS ix_report_audio_track
  ON report (tenant_id, audio_track_hash);
CREATE UNIQUE INDEX IF NOT EXISTS ux_report_derived_generation
  ON report (tenant_id, derived_key, report_generation);

-- ---------- check_manifest（"该查的都查了"的唯一证明） ----------
CREATE TABLE IF NOT EXISTS report_check (
  report_id         TEXT NOT NULL,
  report_generation INTEGER NOT NULL,
  ordinal           INTEGER NOT NULL CHECK (ordinal >= 0),
  check_id          TEXT NOT NULL,
  status            TEXT NOT NULL CHECK (status IN (
                      'evaluated', 'not_applicable', 'not_evaluated', 'failed')),
  reason_code       TEXT NOT NULL CHECK (reason_code IN (
                      'ok', 'no_such_content', 'no_dialogue', 'no_face',
                      'asset_unavailable', 'evaluator_timeout',
                      'evaluator_error', 'dependency_unavailable',
                      'sampling_not_selected', 'policy_disabled')),
  defect_count      INTEGER NOT NULL CHECK (defect_count >= 0),
  PRIMARY KEY (report_id, report_generation, check_id),
  FOREIGN KEY (report_id, report_generation)
    REFERENCES report (report_id, report_generation) ON DELETE CASCADE,
  -- 状态与理由必须配套：`evaluated` 只能是 ok；`not_applicable` 必须给得出
  -- "本来就没有这个内容"的理由；`failed` 必须是评估器/依赖/素材的问题。
  CONSTRAINT rc_status_reason_paired CHECK (
    (status = 'evaluated'      AND reason_code = 'ok')
    OR (status = 'not_applicable' AND reason_code IN
        ('no_such_content', 'no_dialogue', 'no_face'))
    OR (status = 'not_evaluated' AND reason_code IN
        ('sampling_not_selected', 'policy_disabled', 'dependency_unavailable',
         'asset_unavailable'))
    OR (status = 'failed' AND reason_code IN
        ('evaluator_timeout', 'evaluator_error', 'dependency_unavailable',
         'asset_unavailable'))),
  CONSTRAINT rc_failed_has_no_defect CHECK (
    status = 'evaluated' OR defect_count = 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_report_check_ordinal
  ON report_check (report_id, report_generation, ordinal);

-- ---------- 字级时间码（S38 的唯一时间码来源） ----------
CREATE TABLE IF NOT EXISTS alignment_unit (
  tenant_id        TEXT NOT NULL,
  report_id        TEXT NOT NULL,
  report_generation INTEGER NOT NULL,
  unit_index       INTEGER NOT NULL CHECK (unit_index >= 0),
  utterance_index  INTEGER NOT NULL CHECK (utterance_index >= 0),
  utterance_id     TEXT NOT NULL,
  speaker_id       TEXT,
  -- 在 utterance 文本（脚本副本）中的偏移与长度。
  char_offset      INTEGER NOT NULL CHECK (char_offset >= 0),
  char_length      INTEGER NOT NULL CHECK (char_length >= 1),
  start_seconds    REAL NOT NULL CHECK (start_seconds >= 0),
  end_seconds      REAL NOT NULL CHECK (end_seconds > start_seconds),
  ci_low_seconds   REAL CHECK (ci_low_seconds IS NULL OR ci_low_seconds >= 0),
  ci_high_seconds  REAL CHECK (ci_high_seconds IS NULL OR ci_high_seconds >= 0),
  confidence       REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  phoneme_seq      TEXT,                    -- JSON 数组，口型驱动用
  PRIMARY KEY (report_id, report_generation, unit_index),
  FOREIGN KEY (report_id, report_generation)
    REFERENCES report (report_id, report_generation) ON DELETE CASCADE,
  CONSTRAINT au_ci_ordered CHECK (
    ci_low_seconds IS NULL OR ci_high_seconds IS NULL
    OR (ci_low_seconds <= start_seconds AND ci_high_seconds >= end_seconds))
);
-- §5 的区间查询索引；带 tenant_id 是为了跨租户不回表。
CREATE INDEX IF NOT EXISTS ix_alignment_unit_range
  ON alignment_unit (tenant_id, report_id, report_generation, start_seconds);

CREATE TABLE IF NOT EXISTS speaker_segment (
  tenant_id       TEXT NOT NULL,
  report_id       TEXT NOT NULL,
  report_generation INTEGER NOT NULL,
  segment_index   INTEGER NOT NULL CHECK (segment_index >= 0),
  speaker_id      TEXT NOT NULL,
  start_seconds   REAL NOT NULL CHECK (start_seconds >= 0),
  end_seconds     REAL NOT NULL CHECK (end_seconds > start_seconds),
  confidence      REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  PRIMARY KEY (report_id, report_generation, segment_index),
  FOREIGN KEY (report_id, report_generation)
    REFERENCES report (report_id, report_generation) ON DELETE CASCADE
);

-- ---------- 评估器调用（超时要有据可查） ----------
CREATE TABLE IF NOT EXISTS evaluator_call (
  call_id        TEXT PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  report_id      TEXT NOT NULL,
  report_generation INTEGER NOT NULL,
  check_id       TEXT NOT NULL,
  window_index   INTEGER,
  model_snapshot TEXT NOT NULL,
  prompt_hash    TEXT NOT NULL CHECK (length(prompt_hash) = 64),
  tokens_in      INTEGER NOT NULL CHECK (tokens_in >= 0),
  tokens_out     INTEGER NOT NULL CHECK (tokens_out >= 0),
  latency_ms     INTEGER NOT NULL CHECK (latency_ms >= 0),
  -- §5 的 DDL 写大写，§9 E25-A1 断言小写；按验收用例存小写（S37-6）。
  outcome        TEXT NOT NULL CHECK (outcome IN ('ok', 'error', 'timeout')),
  created_at     TEXT NOT NULL,
  FOREIGN KEY (report_id, report_generation)
    REFERENCES report (report_id, report_generation) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_evaluator_call_report
  ON evaluator_call (tenant_id, report_id, report_generation, check_id);

-- ---------- ASR 复用缓存（§8，键按 QAC-3 修正） ----------
CREATE TABLE IF NOT EXISTS asr_cache (
  tenant_id          TEXT NOT NULL,
  cache_key          TEXT NOT NULL CHECK (length(cache_key) = 64),
  audio_track_hash   TEXT NOT NULL,
  asr_model_snapshot TEXT NOT NULL,
  lexicon_revision   INTEGER,
  voice_binding_ref  TEXT,
  payload            TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  PRIMARY KEY (tenant_id, cache_key)
);

-- ---------- S15 授权事件投影 ----------
-- 只存"最近一次听到的授权状态与它的序号"。它不是授权权威：判定前仍要实时
-- 回查 S15，投影只用于发现"读到的状态比事件旧"。
CREATE TABLE IF NOT EXISTS voice_authorization_projection (
  tenant_id       TEXT NOT NULL,
  voice_id        TEXT NOT NULL,
  state           TEXT NOT NULL,
  effective_state TEXT NOT NULL,
  state_seq       INTEGER NOT NULL CHECK (state_seq >= 0),
  state_changed_at TEXT,
  event_id        TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, voice_id)
);
"""
