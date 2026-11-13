"""S04 自己的 schema。表来源是 `plans/S04-narrative.md` §2。

六条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **没有 `accepted` 列，也没有 `APPROVED` 状态。** 剧本接不接受是 S08 的
   accepted pointer（§3.2、§9 反模式 1/2）。同一个 revision 可能在分支 A 已
   接受、分支 B 未接受 —— script 的状态机放不下这个事实，所以它不存在。
   任何"给 script 加一列 accepted"的改动都会让 S16 的写入触发源变成两个。
2. **正文不在主库表里，只有 `body_chunk` 的引用。** 正文进 S04 自己的私有
   对象存储（§2.6），库里只留 `bytes_ref / sha256 / byte_size`。读不出来时
   接口必须显式说 `body_unavailable`，**不能返回空字符串**（§4.6 第 18 条）。
3. **`script_revision_line` 是 seal 那一刻的行集合快照。** 单行后续改字
   （§4.5）产生新的 `line_revision`，但**不改任何已 seal 的 script revision**
   —— 所以那一版的 `line_set_hash` 只描述 seal 那一刻（NAR-4 把这个二选一
   登记为未决，本实现选的是"sealed 快照冻结"）。
4. **`line` 只退休、不删除。** `CHECK ((life_state='retired') = (retired_at
   IS NOT NULL))` 让"标记退休"与"退休时间"成为同一个事实的两种表示。
   S16 的 claim 可能还引用着它（§9 反模式 6）。
5. **`structure.scene_markers` 是文本标记，不是 `scene_id`。** 它只用于 UI
   跳转与 S05 的参考；本库没有任何列能表示场次身份（§2.2 行 216、C04-5）。
6. **`script_reference_projection` 只增不删、可重建、非权威。** 它唯一的
   消费者是撤回守卫（§3.1）。它**不是**接受状态：读它来判断"剧本被接受了"
   就是把 S08 的事实抄成第二份（§4.4 最后一段）。

本仓服务层一律 SQLite（`isolation_level=None` + 显式 `Transaction`），所以方案里的
`jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`text[]` 存 JSON 数组、
`numeric(4,3)` 存 REAL、`boolean` 存 INTEGER 0/1。
`outbox` / `inbox` / `idempotency_record` 由平台 SDK 建表，这里不重复 DDL。
"""

# ---------------------------------------------------------------- 常量

# §2 行 107 / §2.3 / §2.4 / §2.5 点名的前缀。
ID_PREFIX = {
    "brief": "brf_",
    "script": "scr_",
    "line": "ln_",
    "proposal": "arc_",
    "extraction": "xr_",
    "local_event": "lev_",
}

SCRIPT_KINDS = ("episode_script", "series_outline", "scene_sheet")

# §3.1。没有 accepted / published / in_production（§3.4）。
REVISION_STATES = ("DRAFT", "SEALED", "SUPERSEDED", "WITHDRAWN")

# §3.2：只有 active → retired。终态是 retired，但**不是**"完成"。
LINE_STATES = ("active", "retired")

# §3.3。
ARC_STATES = ("PROPOSED", "ADOPTED", "REJECTED", "SUPERSEDED")
ARC_DECISIONS = {"adopt": "ADOPTED", "reject": "REJECTED",
                 "supersede": "SUPERSEDED"}
# 从当前状态可以走到的下一个状态。
ARC_TRANSITIONS = {
    "PROPOSED": {"ADOPTED", "REJECTED", "SUPERSEDED"},
    "ADOPTED": {"SUPERSEDED"},
    "REJECTED": set(),
    "SUPERSEDED": set(),
}

# §2.5。`BUDGET_EXCEEDED` 由 S19/S26 侧判定，S04 只记录结果。
EXTRACTION_STATES = ("PENDING", "SUCCEEDED", "FAILED", "BUDGET_EXCEEDED")

# §2.1：`brief_revision.body` 的键集固定，`additionalProperties: false`。
BRIEF_BODY_KEYS = ("logline", "genre", "tone", "audience", "constraints",
                   "references")

# §5.1 的十条命令 + 建 brief 容器的第十一条（C04-4 的延伸，见 G7）。
COMMANDS = (
    "POST /scripts",
    "POST /scripts/{script_id}/revisions",
    "POST /scripts/{script_id}/revisions/{revision}:seal",
    "POST /scripts/{script_id}/revisions/{revision}:withdraw",
    "POST /lines/{line_id}/revisions",
    "POST /lines/{line_id}:retire",
    "POST /briefs",
    "POST /briefs/{brief_id}/revisions",
    "POST /story-arcs",
    "POST /story-arcs/{proposal_id}/revisions/{revision}:decide",
    "POST /scripts/{script_id}/revisions/{revision}:extract-claims",
)

# §2.6：每块 ≤1MB。整版上限取 §11 未决问题 2 里那个"假设"的 5MB —— 计划自己
# 说"上限需要一个数字"，本实现先把假设写成常量并登记为 G2。
MAX_CHUNK_BYTES = 1024 * 1024
MAX_BODY_BYTES = 5 * 1024 * 1024

# §5.2：`/lines:batch` 上限 500。
MAX_BATCH_IDS = 500
MAX_LIST_LIMIT = 200

# 撤回守卫的投影新鲜度阈值。方案只写了"投影落后时保守拒绝"，没有说多旧算
# 落后（G12）。取 5 分钟：比 S08 的 outbox 告警阈值（60s，§7.1）宽松得多，
# 但足以让一次正常的事件投递延迟不把撤回全部挡掉。
REFERENCE_MAX_LAG_SECONDS = 300


SCHEMA = """
PRAGMA foreign_keys = ON;

-- §2.1 创意简报容器。一剧一份（UNIQUE tenant+series），`head_revision`
-- 是 CAS 的乐观锁。0 表示"还没有任何版本"。
CREATE TABLE IF NOT EXISTS brief (
  tenant_id      TEXT NOT NULL,
  brief_id       TEXT NOT NULL,
  series_id      TEXT NOT NULL,
  head_revision  INTEGER NOT NULL DEFAULT 0 CHECK (head_revision >= 0),
  created_at     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, brief_id),
  UNIQUE (tenant_id, series_id)
);

-- §2.1 简报版本。`body` 的键集固定（BRIEF_BODY_KEYS），由内核校验；
-- 同一内容只存一次（content_hash 唯一），重复提交命中原版本。
CREATE TABLE IF NOT EXISTS brief_revision (
  tenant_id     TEXT NOT NULL,
  brief_id      TEXT NOT NULL,
  revision      INTEGER NOT NULL CHECK (revision >= 1),
  body          TEXT NOT NULL,
  content_hash  TEXT NOT NULL CHECK (length(content_hash) = 64),
  author        TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, brief_id, revision),
  UNIQUE (tenant_id, brief_id, content_hash),
  FOREIGN KEY (tenant_id, brief_id)
    REFERENCES brief (tenant_id, brief_id)
);

-- §2.2 剧本文档身份（容器）。**只有元数据，没有正文。**
-- `episode_id` 可空：系列级总纲剧本（NAR-5 说事件装不下它）。
-- `retired_at` 表示"这集换用别的剧本了"，不是删除；所有 revision 仍可读。
CREATE TABLE IF NOT EXISTS script (
  tenant_id        TEXT NOT NULL,
  script_id        TEXT NOT NULL,
  series_id        TEXT NOT NULL,
  episode_id       TEXT,
  script_kind      TEXT NOT NULL CHECK (script_kind IN
                     ('episode_script','series_outline','scene_sheet')),
  language         TEXT NOT NULL DEFAULT 'zh-CN',
  head_revision    INTEGER NOT NULL DEFAULT 0 CHECK (head_revision >= 0),
  sealed_revision  INTEGER,
  retired_at       TEXT,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, script_id),
  -- §2.2：sealed_revision 只能落后于 head，不能领先。
  CHECK (sealed_revision IS NULL OR sealed_revision <= head_revision),
  CHECK (sealed_revision IS NULL OR sealed_revision >= 1)
);

CREATE INDEX IF NOT EXISTS script_by_episode
  ON script (tenant_id, series_id, episode_id, retired_at);

-- §2.2 剧本版本。不可变（除状态与 sealed_at 外不再赋值）。
-- `structure` = {scene_markers:[...], characters:[...], line_count, word_count}；
-- scene_markers 是**文本标记**，不是 scene_id。
-- `content_hash` 的算式见 service.revision_content_hash()（§2.2 行 218）。
-- DDL 只有方案写的弱约束：`state <> 'DRAFT' OR sealed_at IS NULL`。
-- "SEALED 必须有 sealed_at" 由 seal 流程保证（它在同一个事务里一起写）。
CREATE TABLE IF NOT EXISTS script_revision (
  tenant_id        TEXT NOT NULL,
  script_id        TEXT NOT NULL,
  revision         INTEGER NOT NULL CHECK (revision >= 1),
  state            TEXT NOT NULL CHECK (state IN
                     ('DRAFT','SEALED','SUPERSEDED','WITHDRAWN')),
  title            TEXT,
  body_ref         TEXT NOT NULL,
  body_bytes       INTEGER NOT NULL CHECK (body_bytes >= 0),
  structure        TEXT NOT NULL,
  line_set_hash    TEXT NOT NULL CHECK (length(line_set_hash) = 64),
  content_hash     TEXT NOT NULL CHECK (length(content_hash) = 64),
  author           TEXT NOT NULL,
  produced_by      TEXT,
  parent_revision  INTEGER,
  sealed_at        TEXT,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, script_id, revision),
  UNIQUE (tenant_id, script_id, content_hash),
  FOREIGN KEY (tenant_id, script_id)
    REFERENCES script (tenant_id, script_id),
  CONSTRAINT srev_sealed_chk CHECK ((state <> 'DRAFT') OR sealed_at IS NULL)
);

CREATE INDEX IF NOT EXISTS script_revision_by_state
  ON script_revision (tenant_id, script_id, state, revision DESC);

-- §2.3 对白行身份。跨改稿不变；删除 = life_state='retired'。
CREATE TABLE IF NOT EXISTS line (
  tenant_id      TEXT NOT NULL,
  line_id        TEXT NOT NULL,
  script_id      TEXT NOT NULL,
  episode_id     TEXT,
  life_state     TEXT NOT NULL DEFAULT 'active'
                   CHECK (life_state IN ('active','retired')),
  head_revision  INTEGER NOT NULL DEFAULT 0 CHECK (head_revision >= 0),
  created_at     TEXT NOT NULL,
  retired_at     TEXT,
  PRIMARY KEY (tenant_id, line_id),
  FOREIGN KEY (tenant_id, script_id)
    REFERENCES script (tenant_id, script_id),
  CONSTRAINT line_retired_chk
    CHECK ((life_state = 'retired') = (retired_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS line_by_script
  ON line (tenant_id, script_id, life_state);

-- §2.3 行版本。不可变：改字追加 revision。
-- `UNIQUE (tenant, line, text_hash)` 的作用是"同一行同一文本不重复建版本"。
-- `speaker_id` 可空（C04-3：C04 不铸造 chr_*，未绑定时用 speaker_label）。
CREATE TABLE IF NOT EXISTS line_revision (
  tenant_id        TEXT NOT NULL,
  line_id          TEXT NOT NULL,
  revision         INTEGER NOT NULL CHECK (revision >= 1),
  text             TEXT NOT NULL,
  text_hash        TEXT NOT NULL CHECK (length(text_hash) = 64),
  speaker_id       TEXT,
  speaker_label    TEXT,
  parenthetical    TEXT,
  locale           TEXT NOT NULL DEFAULT 'zh-CN',
  scene_marker     TEXT,
  order_in_scene   INTEGER,
  author           TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, line_id, revision),
  UNIQUE (tenant_id, line_id, text_hash),
  FOREIGN KEY (tenant_id, line_id) REFERENCES line (tenant_id, line_id),
  -- §2.3 行 265：说话人二选一，不能都空。
  CHECK (speaker_id IS NOT NULL OR speaker_label IS NOT NULL)
);

-- §2.3 seal 时的行集合快照。`seq` 是本版中的出现顺序；`line_set_hash`
-- 对 (line_id, line_revision, text_hash) 排序后求，**顺序不影响 hash**。
CREATE TABLE IF NOT EXISTS script_revision_line (
  tenant_id      TEXT NOT NULL,
  script_id      TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  line_id        TEXT NOT NULL,
  line_revision  INTEGER NOT NULL CHECK (line_revision >= 1),
  text_hash      TEXT NOT NULL CHECK (length(text_hash) = 64),
  seq            INTEGER NOT NULL CHECK (seq >= 1),
  PRIMARY KEY (tenant_id, script_id, revision, line_id),
  UNIQUE (tenant_id, script_id, revision, seq),
  FOREIGN KEY (tenant_id, script_id, revision)
    REFERENCES script_revision (tenant_id, script_id, revision),
  FOREIGN KEY (tenant_id, line_id) REFERENCES line (tenant_id, line_id)
);

CREATE INDEX IF NOT EXISTS srl_by_line
  ON script_revision_line (tenant_id, line_id, script_id, revision);

-- §2.4 故事弧候选。**候选，不是正史。** ADOPTED 只影响 UI 与 S18 pack。
-- 决策写新 revision（G8），所以 PK 含 revision。
CREATE TABLE IF NOT EXISTS story_arc_proposal (
  tenant_id        TEXT NOT NULL,
  proposal_id      TEXT NOT NULL,
  series_id        TEXT NOT NULL,
  revision         INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  state            TEXT NOT NULL CHECK (state IN
                     ('PROPOSED','ADOPTED','REJECTED','SUPERSEDED')),
  body             TEXT NOT NULL,
  produced_by      TEXT NOT NULL,
  source_revision  TEXT,
  confidence       REAL CHECK (confidence IS NULL
                     OR (confidence >= 0 AND confidence <= 1)),
  unresolved       TEXT NOT NULL DEFAULT '[]',
  reviewed_by      TEXT,
  created_at       TEXT NOT NULL,
  decided_at       TEXT,
  PRIMARY KEY (tenant_id, proposal_id, revision)
);

CREATE INDEX IF NOT EXISTS arc_by_series
  ON story_arc_proposal (tenant_id, series_id, state);

-- §2.5 抽取任务引用。**只存"跑过没有、结果去哪了"，不存候选内容。**
-- 候选在 S16 的 claim_candidate 里；这里存一份就是第二候选池（§9 反模式 9）。
CREATE TABLE IF NOT EXISTS claim_extraction_run (
  tenant_id       TEXT NOT NULL,
  extraction_id   TEXT NOT NULL,
  script_id       TEXT NOT NULL,
  script_revision INTEGER NOT NULL,
  agent_task_ref  TEXT NOT NULL,
  result_state    TEXT NOT NULL CHECK (result_state IN
                    ('PENDING','SUCCEEDED','FAILED','BUDGET_EXCEEDED')),
  candidate_count INTEGER,
  handoff_ref     TEXT,
  created_at      TEXT NOT NULL,
  completed_at    TEXT,
  PRIMARY KEY (tenant_id, extraction_id)
);

CREATE INDEX IF NOT EXISTS extraction_by_revision
  ON claim_extraction_run (tenant_id, script_id, script_revision);

-- §2.6 正文分块索引。**没有正文列**：字节在 S04 的私有对象存储里，
-- `bytes_ref` 带租户前缀（§2.6）。sha256 是读取时的完整性校验。
CREATE TABLE IF NOT EXISTS body_chunk (
  tenant_id   TEXT NOT NULL,
  script_id   TEXT NOT NULL,
  revision    INTEGER NOT NULL,
  chunk_no    INTEGER NOT NULL CHECK (chunk_no >= 0),
  bytes_ref   TEXT NOT NULL,
  sha256      TEXT NOT NULL CHECK (length(sha256) = 64),
  byte_size   INTEGER NOT NULL CHECK (byte_size >= 0),
  PRIMARY KEY (tenant_id, script_id, revision, chunk_no)
);

-- §3.1 引用投影。只增不删（manifest 变 VOIDED 也保留），可重建，非权威。
-- **它不是接受状态**：`applied_seq` 只说明"我消费到 S08 的哪条事件"。
CREATE TABLE IF NOT EXISTS script_reference_projection (
  tenant_id         TEXT NOT NULL,
  script_id         TEXT NOT NULL,
  script_revision   INTEGER NOT NULL CHECK (script_revision >= 1),
  manifest_id       TEXT NOT NULL,
  manifest_revision INTEGER NOT NULL CHECK (manifest_revision >= 1),
  execution_fence   INTEGER NOT NULL,
  applied_seq       INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, script_id, script_revision, manifest_id,
               manifest_revision)
);

CREATE INDEX IF NOT EXISTS srp_by_revision
  ON script_reference_projection (tenant_id, script_id, script_revision);

-- §3.1 的"投影落后"判据。方案只说 stale=true 时保守拒绝，没说怎么判（G12）。
-- 这张表记录每个来源已消费到的事件水位与最后更新时间；**没有行 = 从未消费
-- 过 = 落后**（不能把"没收到任何事件"读成"没有任何引用"）。
CREATE TABLE IF NOT EXISTS projection_watermark (
  tenant_id   TEXT NOT NULL,
  source      TEXT NOT NULL,
  sequence    INTEGER NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (tenant_id, source)
);

-- 本地事件的落点。**不是 outbox**：`narrative.line.revision.created` 与
-- `narrative.script.withdrawn` 在 `event-topics.v2.json` 里没有登记
-- （C04-2），把未登记的 event_type 塞进 outbox 等于让中继去投递一个没有
-- schema 的事件。本地消费方按 tenant + 时间拉这里，`registered` 永远为 false。
CREATE TABLE IF NOT EXISTS local_event (
  tenant_id      TEXT NOT NULL,
  local_event_id TEXT NOT NULL,
  event_type     TEXT NOT NULL,
  aggregate_id   TEXT NOT NULL,
  payload        TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  consumed_at    TEXT,
  PRIMARY KEY (tenant_id, local_event_id)
);

CREATE INDEX IF NOT EXISTS local_event_unconsumed
  ON local_event (tenant_id, created_at) WHERE consumed_at IS NULL;
"""


# ---------------------------------------------------------------- 缺口登记
# 每一条都是契约/上游能力的缺口，不是实现自由度。命令响应与查询响应都随身带出，
# 免得"能响应"被读成"契约完整"（与 S03 / S25 的 CONTRACT_GAPS 同构）。
CONTRACT_GAPS = [
    # ---- 方案 §10 的十条 ----
    {"id": "C04-1", "kind": "conflict",
     "summary": "shot-spec.v1 的 dialogue[].text 内嵌字符串，S04 也持有同一句的"
                " text；契约里没有 script_line_ref 表达派生关系",
     "handling": "S04 是文字权威；S06 写 ShotSpec 时必须字节相等校验并自存 "
                 "{script_id, revision, line_id, text_hash}。S04 侧不改契约，"
                 "也不把 text 复制给 S06。"},
    {"id": "C04-2", "kind": "gap",
     "summary": "event-topics.v2.json 没有行级变更事件；改一句台词不产生新 seal，"
                "S38/S09/S06 无法从 topic 感知",
     "handling": "`narrative.line.revision.created` 写 local_event，不写 outbox；"
                 "登记为待新增事件。"},
    {"id": "C04-3", "kind": "gap",
     "summary": "line.speaker_id 的 ID 空间属于 S11（chr_*），写剧本时角色常未铸造",
     "handling": "speaker_id 可空 + speaker_label 文本；S04 绝不铸造 chr_*；"
                 "绑定解析在 S06/S10。"},
    {"id": "C04-4", "kind": "conflict",
     "summary": "catalog 只声明 POST /scripts/{id}/revisions；seal/withdraw/"
                "line revision/retire/brief/story-arc/extract 全部无契约路径，"
                "openapi.v2.yaml 也没有 S04 的任何路径",
     "handling": "按方案 §5.1/§5.2 实现全部路径；不改契约。"},
    {"id": "C04-5", "kind": "conflict",
     "summary": "S09 的字段依赖注册表登记了 ('S04','/scene_count','number')，"
                "但场次总数是 S05 的事实；S04 只有 scene_markers.length",
     "handling": "S04 不提供 /scene_count 作为业务事实；本服务从任何接口都不"
                 "把 scene_markers 解释成 scene 列表。"},
    {"id": "C04-6", "kind": "gap",
     "summary": "没有「按 source_revision 反查 manifest」的查询，撤回守卫没有"
                "权威依据",
     "handling": "用 snapshot.manifest.committed 维护 script_reference_projection"
                 "（apply_manifest_committed），投影不新鲜时保守拒绝。"},
    {"id": "C04-7", "kind": "conflict",
     "summary": "台词正文是否进事件有取舍：进则下游免回源，但 topic 保留期内可"
                "重建剧本",
     "handling": "不进事件，只发 text_hash；下游回源 GET /lines/{id}/revisions/{rev}。"},
    {"id": "C04-8", "kind": "conflict",
     "summary": "S08 :commit 校验 source_revisions[].content_hash 时若只做 schema "
                "校验，等于把内容版本权威外包给调用方",
     "handling": "S04 提供 GET /scripts/{id}/revisions/{rev} 且返回 content_hash；"
                 "要求 S08 回源比对。S04 侧不做别的。"},
    {"id": "C04-9", "kind": "gap",
     "summary": "S05 的 SceneBreakdownRevision 缺少「基于哪一版剧本」的强绑定字段",
     "handling": "S04 提供查询；S05 的命令体需要带 source_script_ref。S04 侧"
                 "不写 S05 的表。"},
    {"id": "C04-10", "kind": "gap",
     "summary": "catalog 的 depends_on 是 S03/S18/S19，但撤回守卫要 S08、抽取"
                "交接要 S16、seal 权限判定要 S02",
     "handling": "运行期依赖按注入的 callback 表达；未配置即 503，不假装成功。"},

    # ---- contracts/narrative.script-revision-created.v2.schema.json 的七条 ----
    {"id": "NAR-1", "kind": "needs_owner",
     "summary": "produced_by 的键集在 §2.2 / §2.4 / §4.2 三处定义互不相同",
     "handling": "接受五个键名的并集、不要求具体键；但**没有 task_id 就无法"
                 "回查 S19**，所以非 null 的 produced_by 缺 task_id 时返回 422 "
                 "UNKNOWN_AGENT_TASK（details 指向本条）。不替 owner 选键集。"},
    {"id": "NAR-2", "kind": "needs_owner",
     "summary": "structure_hash 全篇只出现在事件示例一次，无算法、无输入键集、"
                "DDL 无此列",
     "handling": "事件要求它是非空字符串，所以发 sha256(JCS(structure)) ——"
                 "这是本实现选的定义，计划没说，下游也无法复算（事件不含 "
                 "structure）。等 owner 定义或删除该字段。"},
    {"id": "NAR-3", "kind": "conflict",
     "summary": "事件的 content_hash 无法用事件自带的字段复算（缺 title/"
                "language/structure）",
     "handling": "S04 的 GET /scripts/{id}/revisions/{rev} 返回 title / structure /"
                 " language / parent_revision，让 R1 至少能回源复算；事件本身"
                 "不改（契约是 additionalProperties:false）。"},
    {"id": "NAR-4", "kind": "needs_owner",
     "summary": "line_set_hash 描述哪一刻的行集合：seal 后改一句台词是否要产生"
                "新 script revision，方案没答",
     "handling": "本实现选「sealed 快照冻结」：POST /lines/{id}/revisions 只写新 "
                 "line_revision + 本地事件，不改任何 script_revision 的 hash。"
                 "需要 owner 确认（另一条路是强制新 script revision）。"},
    {"id": "NAR-5", "kind": "needs_owner",
     "summary": "episode_id 在 DDL 可空（系列级总纲），事件 schema 收成必需；"
                "事件也不携带 script_kind",
     "handling": "seal 时如实发 episode_id（series_outline 就是 null），"
                 "并让 GET 查询返回 script_kind。事件对大纲剧本会不合 schema ——"
                 "这是登记的冲突，不在代码里伪造 episode_id。"},
    {"id": "NAR-6", "kind": "needs_owner",
     "summary": "produced_by 示例为 null、DDL 可空、散文说必填；S09 的 "
                "SCRIPT_DERIVED_FROM_PACK 边唯一依据是它",
     "handling": "人写路径允许 null（示例就是 null）；Agent 路径要求非 null 且"
                 "可核验。S09 必须对人写剧本跳过该边，否则会写悬空边。"},
    {"id": "NAR-7", "kind": "gap",
     "summary": "事件不含 state；SEALED→SUPERSEDED 与 SEALED→WITHDRAWN 都不发"
                "登记事件，下游无法知道一版是否仍有效",
     "handling": "superseded 不发任何事件（方案没打算发）；withdrawn 写 "
                 "local_event。下游要判断有效性必须查 GET /revisions/{rev}。"},

    # ---- 实现时新发现的 ----
    {"id": "S04-G1", "kind": "needs_owner",
     "summary": "正文存储介质未定（§11 未决 1：S04 私有桶 vs S28）",
     "handling": "正文存储是注入的 body_store 适配器（put/get/delete/"
                 "list_prefix）；未配置即 503 BODY_STORE_UNAVAILABLE。不把剧本"
                 "正文写进 S28（那是制品字节的权威）。"},
    {"id": "S04-G2", "kind": "needs_owner",
     "summary": "单 revision 正文上限没有数字（§11 未决 2）",
     "handling": "取方案假设的 5 MiB（MAX_BODY_BYTES），分块 1 MiB。超限 413 "
                 "BODY_TOO_LARGE。数字待 owner 与 S05 的分解超时一起定。"},
    {"id": "S04-G3", "kind": "gap",
     "summary": "line_set_hash 的算式写了 sorted([line_id, line_revision, "
                "text_hash]) 但没有说行内用什么分隔、排序键是什么",
     "handling": "每行编成「<line_id>:<line_revision>:<text_hash>」，按整个字符串"
                 "字典序排序，再用 \\n 连接后 sha256。定义在 "
                 "service.line_set_digest()，必须与 S05/S06/S08 共用。"},
    {"id": "S04-G4", "kind": "gap",
     "summary": "§2.6 的 idempotency_record 有 operation/state/response_* 列，"
                "平台 SDK 的表只有 (tenant, command, request_hash, result, "
                "created_at)，也没有 ABANDONED 状态",
     "handling": "直接用 SDK 表，不为一个 SDK 三处共用而分叉；IN_PROGRESS 的"
                 "陈旧行由 sweep_stale_commands() 删除（等价于方案里的 ABANDONED "
                 "后重试被当成新请求）。"},
    {"id": "S04-G5", "kind": "gap",
     "summary": "§2.6 的 outbox 有 UNIQUE (tenant, aggregate_id, aggregate_seq)，"
                "event_sdk 的 OUTBOX_DDL 没有这条约束",
     "handling": "序号在写事务内从 outbox 自己算（MAX(sequence)+1）—— 单写者"
                 "下成立，但不是数据库约束。"},
    {"id": "S04-G6", "kind": "needs_owner",
     "summary": ":extract-claims 的请求体没有定义（S19 的 POST /agent-tasks 需要 "
                "context/role/limits/input_refs/output_schema_ref 等）",
     "handling": "命令接受 {task_request: {...}} 原样转给 S19，只把返回的 "
                 "task_id 存进 agent_task_ref。S04 不构造这些字段、不碰预算。"},
    {"id": "S04-G7", "kind": "gap",
     "summary": "方案没有「创建 brief 容器」的命令，但 brief_revision 有外键指向 "
                "它，POST /briefs/{id}/revisions 又要求容器已存在",
     "handling": "补 POST /briefs（铸造 brf_，body {series_id}）。第二条同 series "
                 "的 brief 返回 409 ILLEGAL_STATE_TRANSITION + 现有 brief_id。"},
    {"id": "S04-G8", "kind": "gap",
     "summary": "story-arc 的 decide 是原地改 state 还是追加 revision，方案没说；"
                "PK 含 revision，原地改会让 revision 失去意义",
     "handling": "decide 追加 revision+1 的新行（body/produced_by 复制，state 变，"
                 "decided_at 落值）。旧行不动。"},
    {"id": "S04-G9", "kind": "gap",
     "summary": "§4.1 只在 DRAFT 里拒绝**文本**变更；对已有行的 speaker/"
                "parenthetical/locale/scene_marker/order_in_scene 变更没说",
     "handling": "任何字段与原 revision 不同都返回 422 LINE_MUTATION_IN_DRAFT"
                 "（details.fields 列出差异），因为同样会让 line_revision 编号"
                 "混乱；退休行出现在新 revision 里返回 409 ILLEGAL_STATE_TRANSITION。"},
    {"id": "S04-G10", "kind": "gap",
     "summary": "§4.1/§5.4 没有「校验 episode 属于 series」这一步，S03 也没有 "
                "GET /series/{id}",
     "handling": "给 episode_id 时调注入的 S03 GET /episodes/{id} 校验存在且 "
                 "series_id 相符（不符 422 EPISODE_SERIES_MISMATCH）；未配置即 "
                 "503 CATALOG_UNAVAILABLE。series_id 本身只能当不透明引用存。"},
    {"id": "S04-G11", "kind": "gap",
     "summary": "§6.1 要求正文这种大字段先 sha256 再进 request_hash，"
                "canonical_request_hash 直接对完整 body 求 JCS",
     "handling": "不改共享 SDK（S08/S10 也在用）。后果只是 hash 体积与 CPU，"
                 "不影响幂等语义：同 body 仍同 hash、异 body 仍异 hash。"},
    {"id": "S04-G12", "kind": "gap",
     "summary": "REFERENCE_CHECK_STALE 的「落后」没有判据",
     "handling": "projection_watermark 记录每个来源最后消费到的事件水位与时间；"
                 "没有水位行（从未消费）= 落后，距上次更新超过 "
                 "REFERENCE_MAX_LAG_SECONDS（300s）= 落后。宁可多拒撤回。"},
    {"id": "S04-G13", "kind": "gap",
     "summary": "§4.1 第 2 步要求「从签名上下文取 tenant/principal」，但本仓库"
                "的 HTTP 层没有 TCT 验签中间件；author / principal_type 只能由"
                "调用方自报",
     "handling": "HTTP 层优先用 X-Principal-Id / X-Principal-Type 覆盖 body "
                 "author，两者不一致 → 403 PERMISSION_DENIED；缺失时沿用 body。"
                 "**这不是安全边界**：没有验签时，把 principal_type 写成 human "
                 "的 agent 仍能 seal。中间件就位前 seal 的 PERMISSION_DENIED "
                 "只能挡住如实上报的 agent。"},
]
