"""S35 自己的 schema。表来源是 `plans/S35-technical-qc.md` §5。

四条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **报告只测量，不修复。** S35 没有任何"执行修复"的列、表或 callback。
   `report.defect` 里的 `recommended_actions` 是**给 S39 的建议**（§2.3 末段：
   "输入，不是指令"）。本服务不写 artifact 状态、不触发重生成——那会让 S35
   变成 artifact 内容的第二权威（AGENTS.md）。
2. **判定词表是 CHECK 约束，不是约定。** `decision` 只允许
   `pass / repair / human_review / abstain`；`report_check.status` 只允许
   `evaluated / not_applicable / not_evaluated / failed`；`reason_code` 是 §2.3
   的 10 值闭集。**"查不成"落不进 `evaluated`**，所以它不可能被静默读成 pass。
3. **`detector` 只允许 `deterministic`。** §3.1：S35 不调任何模型。把这条写进
   CHECK 而不是只写在文档里，是因为"某个 check 偷偷接了 VLM"在运行期完全
   看不出来，在插入期会直接失败。
4. **事件与报告同事务。** `outbox` 由平台 SDK 建表（不在本文件），`append`
   必须发生在写 `report` 的同一个 `COMMIT` 里（§8）。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`numeric(5,4)` 存
REAL（SQLite 无定点小数，取值域由 CHECK 卡在 [0,1]）。

与 §5 DDL 的偏差（都在 `CONTRACT_GAPS` 里有条目，不自行选边）：

- `report_id` 不是主键，`(report_id, report_generation)` 才是。§2.6 要求同一个
  派生键的第 N 次评估沿用同一个 `report_id`、只升 generation，两句话不能同时
  是"report_id 唯一"（S35-1）。
- 唯一索引带 `tenant_id`。§5 的六列索引没有它，于是两个租户上传同一份字节会
  互相卡住（S35-2）。
- 多出 `derived_key` 列：§2.6 的查重要按派生键而不是六列比对，少一列
  （`evaluator.name` / `model_snapshot`）就会把两代不同评估器混成一代。
- 多出 `report_check` / `report_defect` 两张子表：词表 CHECK 只有落在真实列上
  才拦得住东西；payload 里的 JSON 字符串什么都能装。
"""

ID_PREFIX = {"report": "qc_", "defect": "df_", "operation": "op_"}

# §2.5 的判定词表。CHECK 约束与 decide() 引用同一份常量。
DECISIONS = ("pass", "repair", "human_review", "abstain")

# §2.3 check_manifest.status。
CHECK_STATUSES = ("evaluated", "not_applicable", "not_evaluated", "failed")

# §2.3 check_manifest.reason_code：10 值闭集。
REASON_CODES = (
    "ok", "no_such_content", "no_dialogue", "no_face", "asset_unavailable",
    "evaluator_timeout", "evaluator_error", "dependency_unavailable",
    "sampling_not_selected", "policy_disabled",
)

# §2.3 abstain.reason_code：7 值闭集。
ABSTAIN_REASON_CODES = (
    "incomplete_coverage", "evaluator_timeout", "evaluator_error",
    "model_disagreement", "low_confidence", "dependency_stale",
    "asset_unavailable",
)

SEVERITIES = ("blocker", "major", "minor", "info")

# §2.2 type 闭集（contracts/quality/action-families.v2.json 的 closed_type_set）。
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

# §2.3 artifact_kind 闭集里 S35 能处理的三种。image / subtitle_track 的检查项
# 在 §4 里一条都没有，接进来只会产出一份"没查任何东西的 pass"（S35-16）。
SUPPORTED_ARTIFACT_KINDS = ("video", "audio", "master")

# status → 允许的 reason_code。这是 §2.4 的"not_applicable 必须给出可验证理由"
# 与 §3.2 的"超时是 failed 不是没检出问题"两句话在数据层的落点。
STATUS_REASON_PAIRS = {
    "evaluated": ("ok",),
    "not_applicable": ("no_such_content", "no_dialogue", "no_face"),
    "not_evaluated": ("sampling_not_selected", "policy_disabled",
                      "dependency_unavailable", "asset_unavailable"),
    "failed": ("evaluator_timeout", "evaluator_error",
               "dependency_unavailable", "asset_unavailable"),
}

# 本轮**不解决**的缺口。`contracts/quality/*.json` 里已有 2 条 `x-needs-owner`
# 与 QTC-1 的 `x-conflicts`，那些是待 owner 决定的事项，按 AGENTS.md
# 「契约与文档冲突 → 停下，报告冲突，不要自行选边」处理：本服务按方案散文实现，
# 把偏差记在这里，不修改契约、不代填决定。
CONTRACT_GAPS = [
    {"id": "S35-1", "kind": "conflict",
     "summary": "§5 DDL 把 report_id 设成 PRIMARY KEY，而 §2.6 要求同一派生键"
                "的第 N 次评估沿用同一 report_id、只把 report_generation 升到 N",
     "handling": "主键改为 (report_id, report_generation)；GET 默认取同 id 的"
                 "最大代数，?generation=N 取指定代数（§6.2 本就如此）。"},
    {"id": "S35-2", "kind": "conflict",
     "summary": "§5 的派生键唯一索引是六列、不含 tenant_id；两个租户上传同一份"
                "字节（同 sha256）时第二家会被第一家的报告挡住",
     "handling": "唯一索引改为 (tenant_id, derived_key, report_generation)，"
                 "语义不变且不跨租户。契约待改。"},
    {"id": "S35-3", "kind": "conflict",
     "summary": "§2.4 写 fingerprint 取 first16hex，而 qc-report.v2 的 "
                "defect_fingerprint pattern 是 ^[0-9a-f]{32}$",
     "handling": "按 schema 出 32 个十六进制字符（sha256 前 16 字节）。"
                 "方案那四个字应是「16 字节」，待 owner 改措辞。"},
    {"id": "S35-4", "kind": "conflict",
     "summary": "§2.7 第 7 步返回 abstain('low_confidence', [])，但 §2.5 规则 1/2"
                "与 schema 都要求 failed_check_ids 非空、且对应 manifest 项状态"
                "不能是 evaluated——全部查完却说没有失败项，这两条无法同时成立",
     "handling": "按 §2.5 的判定表处理：该表把「置信度落在灰区」定义为 "
                 "human_review 的适用情形，故此处输出 human_review 并将 "
                 "decision_reason 记为 low_confidence。abstain 只用于真的没查完"
                 "（第 1–3 步）。"},
    {"id": "S35-5", "kind": "needs_owner",
     "summary": "§2.3 的 context 必填 run_id 与 shot_lineage_id，但 "
                "contracts/execution-context.v2.schema.json 是 "
                "additionalProperties:false 且没有这两个字段，调用方无法合规地传",
     "handling": "从命令体顶层读 `run_id` / `shot_lineage_id`（与 execution_context "
                 "并列），缺失即 422；不塞进 execution_context 去违约。"},
    {"id": "S35-6", "kind": "conflict",
     "summary": "§6.1 说 artifact 不可达是 422，§10.4 说 S28 不可达要 abstain",
     "handling": "区分两种：S28 明确回答 404/409（这个 artifact 就是取不到）→ 422，"
                 "命令不产生报告；S28 超时/5xx（暂时读不到）→ 仍生成报告，"
                 "decision=abstain 且 abstain.reason_code=asset_unavailable。"},
    {"id": "S35-7", "kind": "gap",
     "summary": "service-catalog 的 S35 depends_on 只有 S28，但 TECH.DURATION.MISMATCH "
                "与 TECH.AUDIO.MISSING 必须读 ShotSpec（S06）；方案 C3 已承认",
     "handling": "S06 作为查询级依赖注入；读不到时这两条 check 落 "
                 "not_evaluated/dependency_unavailable，整份报告 abstain——"
                 "不猜「大概没对白」。不改 catalog。"},
    {"id": "S35-8", "kind": "gap",
     "summary": "check-registry.v2.json 数据文件不存在（方案 C5 只落了一半），"
                "而 schema 必填的 default_action / window_scope / escalation_rule "
                "在 §4 的表里没有出处",
     "handling": "内核只编码 §4 实有的列（check_id / defect_type / severity / "
                 "allowed_actions / recheck_scope），不凭空补路由字段；"
                 "S39 需要的登记项等四个 QC 服务各自补。"},
    {"id": "S35-9", "kind": "needs_owner",
     "summary": "§4 的允许动作列有两处不在枚举里：`remux→transcode` 与 `—`；"
                "而 defect.recommended_actions 是 minItems 1",
     "handling": "`remux→transcode` 归一为 transcode（remux 不在枚举，transcode 是"
                 "同族里最接近的落地动作）；`—` 的 TECH.META.MISSING 属 info，"
                 "只登记测量值、不产 defect（否则会被迫编一个动作）。"},
    {"id": "S35-10", "kind": "conflict",
     "summary": "quality.technical-completed.v2 把 defect_count / blocker_count "
                "列进 required（自身标了 x-needs-owner），而 §2.3 的 QCReport "
                "没有这两个字段、§5 DDL 也没有对应列",
     "handling": "事件里发这两个数（由 defects[] 现算，不另存权威），报告里不发；"
                 "报告与事件的计数由同一函数产出，不会各说各话。"},
    {"id": "S35-11", "kind": "conflict",
     "summary": "契约 QTC-1：§7 的事件示例同时写 envelope.subject 与 data.shot_id，"
                "schema 无法强制两者一致",
     "handling": "subject 取 shot_id（与 §7 示例同），data.shot_id 由同一个变量"
                 "赋值；代码里两者不可能分叉，但契约层仍无约束。"},
    {"id": "S35-12", "kind": "conflict",
     "summary": "§11 说 TECHNICAL_QC_CHECKS 默认空 = 不查任何项，但 "
                "check_manifest 是 minItems 1，且 decide() 第 1 步会把空的必查集"
                "判成 pass——那正是「没查就说通过」",
     "handling": "开关打开但白名单为空时拒绝命令（422 NO_CHECKS_ENABLED），"
                 "不产出一份空清单的 pass。开关本身仍默认 false。"},
    {"id": "S35-13", "kind": "gap",
     "summary": "§4 的严重度有四处带条件（如 TECH.LOUDNESS.LUFS「master 时 "
                "blocker」），schema 的 severity 只有一个值；§2.3 也没定义 "
                "confidence 在确定性评估器下怎么算",
     "handling": "严重度按 artifact_kind 从 §4 的表推导（master → blocker，"
                 "否则按表里的默认档）；confidence 定义为「全部必查项都 "
                 "evaluated 时 1.0，否则 0.0」——确定性测量没有中间态。"},
    {"id": "S35-14", "kind": "gap",
     "summary": "evidence_bundle_refs 是 minItems 1，但内核不向 S28 归档探针 JSON / "
                "拼板图（那是 §3.2 第 5 步的沙箱产出，尚未实现），本库也不存证据",
     "handling": "引用被测量的 artifact 本身作为证据束（service_id S28 + 其 "
                 "revision + content_hash）；这是诚实的下界，不伪造 artifact id。"},
    {"id": "S35-15", "kind": "conflict",
     "summary": "tools/check_all.py 的 check_maturity 在 services/technical-qc/src/ "
                "一有文件就要求把 catalog 的 maturity 从 design_only 升上去，"
                "而本轮任务明令 catalog 保持 design_only",
     "handling": "两条约束直接冲突，报告给 owner；本服务不动 catalog、不动 tools。"},
    {"id": "S35-16", "kind": "gap",
     "summary": "artifact_kind 枚举含 image / subtitle_track，但 §4 的 23 条检查"
                "没有一条适用于它们；硬接会产出空清单 pass",
     "handling": "只接受 video / audio / master，其余 422 "
                 "UNSUPPORTED_ARTIFACT_KIND。枚举是否该给 S35 收窄，等 owner。"},
    {"id": "S35-17", "kind": "conflict",
     "summary": "§4 的三条 check id 只有两段（TECH.BLACK_FRAME / TECH.FREEZE_FRAME / "
                "TECH.FLASH_FRAME），而 qc-report.v2 的 check_id pattern 是 "
                "`^(TECH|VIS|AUD|SUB)\\.[A-Z0-9_]+\\.[A-Z0-9_]+$`；同一份 plan 的 §2.3"
                "内嵌 schema 用的也是三段式。tools/check_all.py 的 KNOWN_BAD_CHECK_IDS "
                "已把这三个列入待 owner 决定",
     "handling": "注册表**保留方案的两段式原文**，不改名：改名会同时改 E24-T4/T5 的"
                 "断言与下游 S39 的 registry 匹配键，那是 owner 的决定。代价是这三项"
                 "过不了 qc-report.v2 的整份校验；测试只允许这三项不匹配且逐条点名，"
                 "其余 20 项必须通过（见 tests 的 known-divergence 用例）。"
                 "建议走 TECH.FRAME.BLACK 等三段式，但本服务不自行实施。"},
    {"id": "S35-18", "kind": "gap",
     "summary": "§3.2 第 2–3 步的媒体沙箱（短签名 URL + ffprobe/ffmpeg/ebur128）"
                "在本仓不存在，S28 也没有探测接口；探针文档的字段形状因此是 "
                "S35 与未来沙箱之间的私有约定，不是契约",
     "handling": "`deps.build_probe_source` 注入式，未配置时**必然** 503 → 全部依赖"
                 "探针的 check 落 failed → 报告 abstain。绝不用空探针假装测过。"
                 "沙箱落地后此协议要么升成契约、要么并入 S28 的读面，由 owner 决定。"},
    {"id": "S35-19", "kind": "conflict",
     "summary": "§2.7 第 2 步把覆盖不足的理由写死成 `incomplete_coverage`，而 §10.1 "
                "E24-T8（沙箱 OOM）与 §10.2 E25-T1（ffprobe 超时）同时要求覆盖率下降"
                "**且** reason 为 evaluator_error / evaluator_timeout——这两种故障必然"
                "先让覆盖率掉下去，第 2 步会先把更具体的理由吞掉",
     "handling": "第 2 步在 `failed` 非空时理由取 `reason_for(failed)`，其余情况仍是"
                 "`incomplete_coverage`；判定与 `failed_check_ids` 不变，§2.7 两句话的"
                 "语义都保住。哪一句是准的，等三个 QC 服务的共用 decide() owner 定。"},
    {"id": "S35-20", "kind": "gap",
     "summary": "§5 的 report 表只存 `coverage_ratio`，不存它对照的合规线，于是"
                "「覆盖率不足所以弃权」在库层不可复核——同一行 0.70 在不同 policy 下"
                "含义相反",
     "handling": "report 表加 `required_coverage_min`（值来自本次评估的 policy），"
                 "并让 `report_abstain_has_failed` 接受「覆盖率低于本条报告自己的"
                 "合规线」作为弃权的第二种数据层证据。列的最终归属（report 还是"
                 "policy 快照表）等 owner，本服务只增列、不改 §5 其余字段。"},
    {"id": "S35-21", "kind": "conflict",
     "summary": "§6.1 的 `POST /technical-checks` 是 202 + `operation_id` 的异步形态，"
                "§6.2 的读面又承诺 `source_revision` / `projection_watermark` 两个"
                "投影字段；本内核同步评估、没有独立投影，§5 的 DDL 里这两个字段"
                "也没有出处",
     "handling": "同步执行完成后返回 `operation_id` + `status=accepted` 以保住 HTTP "
                 "形状；`source_revision` 取报告代数、`projection_watermark` 取落库"
                 "时刻，都是**派生**值而不是投影水位。真做异步与投影时这两个字段"
                 "要重新定义，等 owner（§6 vs §5 的冲突不由本服务选边）。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 技术质检报告（不可变，按代数追加） ----------
CREATE TABLE IF NOT EXISTS report (
  report_id            TEXT NOT NULL,
  report_generation    INTEGER NOT NULL CHECK (report_generation >= 1),
  -- §2.6 的派生键原文。查重按它比对，不按六列——少一列就会把两代不同
  -- 评估器（或不同 model_snapshot）混成一代。
  derived_key          TEXT NOT NULL CHECK (length(derived_key) = 64),
  tenant_id            TEXT NOT NULL,
  project_id           TEXT NOT NULL,
  series_id            TEXT NOT NULL,
  episode_id           TEXT NOT NULL,
  branch_id            TEXT NOT NULL,
  shot_id              TEXT NOT NULL,
  shot_lineage_id      TEXT NOT NULL,
  logical_node_id      TEXT NOT NULL,
  -- AGENTS.md：`execution_fence` 管 run/branch，`node_epoch` 管单个
  -- `logical_node`。两列分开存，绝不合并；本服务只透传，不比较。
  node_epoch           INTEGER NOT NULL CHECK (node_epoch >= 1),
  manifest_id          TEXT NOT NULL,
  manifest_revision    INTEGER NOT NULL CHECK (manifest_revision >= 1),
  run_id               TEXT NOT NULL,
  execution_fence      INTEGER NOT NULL CHECK (execution_fence >= 1),
  artifact_id          TEXT NOT NULL,
  artifact_sha256      TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
  artifact_kind        TEXT NOT NULL CHECK (artifact_kind IN (
                         'video', 'audio', 'master')),
  evaluator_name       TEXT NOT NULL,
  evaluator_version    TEXT NOT NULL,
  policy_version       TEXT NOT NULL,
  sampling_version     TEXT NOT NULL,
  -- §2.5 的判定词表。不属于这四个值的东西在数据库层就不存在。
  decision             TEXT NOT NULL CHECK (decision IN (
                         'pass', 'repair', 'human_review', 'abstain')),
  decision_reason      TEXT NOT NULL CHECK (length(decision_reason) >= 1),
  confidence           REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  coverage_ratio       REAL NOT NULL CHECK (coverage_ratio >= 0 AND coverage_ratio <= 1),
  -- §2.7 第 2 步的合规线要跟着报告一起存：`report_abstain_has_failed` 必须能
  -- 在库层区分"覆盖不足导致的弃权"与"没人查却宣称弃权"。只存实际覆盖率的
  -- 话，0.70 到底合不合规取决于一份没有落库的策略快照（S35-17）。
  required_coverage_min REAL NOT NULL CHECK (
                         required_coverage_min >= 0 AND required_coverage_min <= 1),
  defect_count         INTEGER NOT NULL CHECK (defect_count >= 0),
  major_count          INTEGER NOT NULL CHECK (major_count >= 0),
  blocker_count        INTEGER NOT NULL CHECK (blocker_count >= 0),
  failed_check_count   INTEGER NOT NULL CHECK (failed_check_count >= 0),
  attempt_slot_id      TEXT,
  supersedes_report_id TEXT,
  supersedes_generation INTEGER CHECK (
                         supersedes_generation IS NULL
                         OR supersedes_generation >= 1),
  payload              TEXT NOT NULL,        -- 完整 QCReport v2
  content_hash         TEXT NOT NULL CHECK (length(content_hash) = 64),
  created_at           TEXT NOT NULL,
  PRIMARY KEY (report_id, report_generation),
  -- §2.6：第 1 代没有前身，第 N 代必须指向前一代。只写前半句会让
  -- "generation=3 但 supersedes 为空"通过，那正是 S39 追不到复检链的情形。
  CONSTRAINT report_supersedes_paired CHECK (
    (report_generation = 1 AND supersedes_report_id IS NULL
       AND supersedes_generation IS NULL)
    OR (report_generation > 1 AND supersedes_report_id IS NOT NULL
       AND supersedes_generation = report_generation - 1)),
  -- 「未知不能当 pass」在库里的形状。弃权必须有一个**数据层可验证**的理由，
  -- 二选一：有没查成的 check，或者实际覆盖低于本条报告所依据的合规线。
  -- 后者要存下 `required_coverage_min` 才成立——只存 0.70 而不存那条线，
  -- "覆盖不足"就成了断言的、不可复核的说法（S35-20）。
  CONSTRAINT report_abstain_has_failed CHECK (
    decision <> 'abstain'
    OR failed_check_count > 0
    OR coverage_ratio < required_coverage_min),
  -- pass 的含义是"所有必查项都已评估且无 blocker/major"（§2.5）。
  CONSTRAINT report_pass_has_no_finding CHECK (
    decision <> 'pass' OR (blocker_count = 0 AND major_count = 0)),
  CONSTRAINT report_repair_has_finding CHECK (
    decision <> 'repair' OR (blocker_count + major_count) > 0)
);
CREATE INDEX IF NOT EXISTS ix_report_lookup
  ON report (tenant_id, series_id, branch_id, shot_lineage_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_report_artifact
  ON report (tenant_id, artifact_sha256);
-- §2.6 幂等兜底 + §2.5 跨租户隔离。见 S35-2。
CREATE UNIQUE INDEX IF NOT EXISTS ux_report_derived_generation
  ON report (tenant_id, derived_key, report_generation);

-- ---------- check_manifest（"该查的都查了"的唯一证明） ----------
CREATE TABLE IF NOT EXISTS report_check (
  report_id         TEXT NOT NULL,
  report_generation INTEGER NOT NULL,
  ordinal           INTEGER NOT NULL CHECK (ordinal >= 0),
  check_id          TEXT NOT NULL,
  -- §2.3 的状态闭集。`evaluated` 之外的三个都意味着"这条结论不成立"，
  -- 区别只在于能不能被豁免（只有 not_applicable 能，见 §2.5）。
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
  -- 状态与理由必须配套：evaluated 只能是 ok；not_applicable 必须给得出
  -- "本来就没有这个内容"的理由；failed 必须是评估器/依赖的问题。
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
  -- 没评估成的那条不可能已经有缺陷被量出来。
  CONSTRAINT rc_failed_has_no_defect CHECK (
    status = 'evaluated' OR defect_count = 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_report_check_ordinal
  ON report_check (report_id, report_generation, ordinal);

-- ---------- defects（只测量出来的，不含修复结果） ----------
CREATE TABLE IF NOT EXISTS report_defect (
  report_id          TEXT NOT NULL,
  report_generation  INTEGER NOT NULL,
  defect_id          TEXT NOT NULL,
  -- §2.4：与 artifact hash 无关的稳定身份，跨复检用来判"同类重复"。
  defect_fingerprint TEXT NOT NULL CHECK (length(defect_fingerprint) = 32),
  check_id           TEXT NOT NULL,
  defect_type        TEXT NOT NULL CHECK (defect_type IN (
                       'identity', 'anatomy', 'hands', 'costume', 'prop',
                       'continuity', 'camera_adherence', 'action_adherence',
                       'temporal', 'subtitle', 'lipsync', 'voice',
                       'technical', 'audio_mix')),
  severity           TEXT NOT NULL CHECK (severity IN
                       ('blocker', 'major', 'minor', 'info')),
  confidence         REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  -- §3.1 / service.yaml acceptance：报告不依赖 VLM 主观判断。
  -- 这条 CHECK 让"某天有人接了个模型探测器"在写库那一刻失败。
  detector           TEXT NOT NULL CHECK (detector = 'deterministic'),
  start_seconds      REAL NOT NULL CHECK (start_seconds >= 0),
  end_seconds        REAL NOT NULL CHECK (end_seconds > start_seconds),
  recommended_actions TEXT NOT NULL,   -- JSON 数组，仅供 S39 读取
  payload            TEXT NOT NULL,
  PRIMARY KEY (report_id, report_generation, defect_id),
  FOREIGN KEY (report_id, report_generation)
    REFERENCES report (report_id, report_generation) ON DELETE CASCADE,
  FOREIGN KEY (report_id, report_generation, check_id)
    REFERENCES report_check (report_id, report_generation, check_id)
);
CREATE INDEX IF NOT EXISTS ix_report_defect_fingerprint
  ON report_defect (defect_fingerprint);
"""
