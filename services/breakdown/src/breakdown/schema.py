"""S05 自己的 schema。表来源是 `plans/S05-breakdown.md` §5。

七条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **`scene` 没有 `scene_order` 列，`scene_revision` 才有。** 场的身份
   （`scene_id`）与"第几场"是两个东西（§1.1）。一旦有人把序号写进 `scene`，
   一次插入场次就变成全局重命名，S06 的分镜与 S09 的依赖边全部错位。
2. **`scene_revision` 没有正文列，只有 `opening_excerpt` 的复制字节 +
   `opening_text_hash`。** 摘录不是第二份剧本权威（§1.2）：写库时逐字节比对
   S04，任何改写走 422 `SCRIPT_LINE_DRIFT`。
3. **`requirement` 没有 `prop_id` / `asset_id` / `rights_ref` 列。** 需求不是
   绑定（§1.3）。绑定归 S10，且随时会变；写死在这里等于让一次道具换版本
   作废整集分镜。
4. **`requirement_resolution_cache` 表名里带 `cache`。** 它不是权威：任何
   "这个资产可用吗"的判定必须回 S10 实时复查（§5.8）。表名是给审阅者看的，
   也是给下一个人看的。
5. **`scene` 只退休、不删除。** `retired_at` 一旦落值不可清除（§3.6 第 5 条）。
   历史 manifest 仍然要能解析到它。
6. **没有 `shot_projection` / `asset_binding` / `scene_canon` / `continuity_state`
   / `inbox`。** 分别是 S06 / S10 / S16 的事实（§5.10）。建一张空的投影表，
   下一个人就会往里写。
7. **`breakdown` 上没有 `UNIQUE (episode_id, branch_id)`。** 同一集同一分支
   可以有多版分场（不同编剧、A/B 版），与 S04 的 `script` 同一考虑（§3.5.1）。

本仓服务层一律 SQLite（`isolation_level=None` + 显式 `Transaction`），所以方案里的
`jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`char(64)` 存裸十六进制、
`boolean` 存 INTEGER 0/1。
`outbox` / `idempotency_record` 由平台 SDK 建表，这里不重复 DDL。
SQLite 没有 `DEFERRABLE INITIALLY DEFERRED`，`scene_order` 的重排改用
"先加偏移再重写 1..N"（S03 重排的同一手法），这条差异登记在 S05-G16。
"""

# ---------------------------------------------------------------- 常量

# §5 行 524 点名的前缀。
ID_PREFIX = {
    "breakdown": "bkd_",
    "scene": "scn_",
    "requirement": "rq_",
    "local_event": "lev_",
}

SCHEMA_VERSION = "2.0"
SOURCE = "S05"
TOPIC = "drama.breakdown.v2"
# §4.3：所有查询响应必须带这个标记，否则未来就会有人往这里写数据。
AUTHORITATIVE_SOURCE = "s05.breakdown"

# §3.5.1。没有 `accepted` / `in_production`：分场接不接受是 S08 的 accepted
# pointer，不是 S05 的状态。
BREAKDOWN_STATES = ("active", "superseded", "archived")

# §3.5.2。技术上 revision 行一经插入即不可变，任何改动都是新行。
REVISION_STATES = ("DRAFT", "SEALED", "SUPERSEDED", "WITHDRAWN")

# §3.5.2 的状态迁移表。终态不再前进。
REVISION_TRANSITIONS = {
    "DRAFT": {"SEALED", "WITHDRAWN"},
    "SEALED": {"SUPERSEDED"},
    "SUPERSEDED": set(),
    "WITHDRAWN": set(),
}

# §2.5。`inherit_by_line_anchor` 是默认值（§12 U2 的临时立场）。
CONTINUITY_MODES = ("fresh", "inherit_by_line_anchor", "inherit_explicit")
DEFAULT_CONTINUITY = "inherit_by_line_anchor"

# §5.3 / §5.6 / §5.7。
SCENE_RETIRE_REASONS = ("merged_into", "removed", "split")
REQUIREMENT_KINDS = ("character", "prop", "scene", "costume", "vehicle",
                     "animal", "sfx")
RELATION_KINDS = ("adjacent", "same_location", "same_time", "flash_forward",
                  "flash_back", "parallel")
HEADING_PLACES = ("INT", "EXT", "INT-EXT")
CONTEXT_COVERAGES = ("full", "partial", "none")

# §2.2 的六条命令 + 六条查询。`service.yaml` 只声明第一条与 `/breakdowns/{id}`
# （§11 C1），本实现按方案补全，不改契约。
COMMANDS = (
    "POST /breakdowns",
    "POST /breakdowns/{breakdown_id}/revisions",
    "POST /breakdowns/{breakdown_id}/revisions/{revision}:seal",
    "POST /scenes/{scene_id}:retire",
    "POST /breakdowns/{breakdown_id}/revisions/{revision}/scene-order",
    "POST /scenes/{scene_id}/requirement-resolutions",
)
QUERIES = (
    "GET /breakdowns/{breakdown_id}",
    "GET /breakdowns/{breakdown_id}/revisions/{revision}",
    "GET /episodes/{episode_id}/scenes",
    "GET /scenes/{scene_id}",
    "GET /scenes/{scene_id}/requires",
)

# §12 U1：单集场数上限没有依据，取 400 并登记（S05-G13）。
MAX_SCENES_DEFAULT = 400
MAX_SCENES_HARD = 400

# 重排时给 `scene_order` / `display_scene` 加的临时偏移。必须大于任何合法
# 场数，保证与未重写的行不撞唯一约束。
ORDER_OFFSET = 100000

# 本地审计事件。`breakdown.revision.created` 是唯一登记进 outbox 的类型；
# 其余写 `local_event`（§3.4 / §8.3）。把未登记的 event_type 塞进 outbox
# 等于让中继去投递一个没有 schema 的事件。
LOCAL_EVENT_SEALED = "breakdown.revision.sealed"
LOCAL_EVENT_SUPERSEDED = "breakdown.revision.superseded"
LOCAL_EVENT_RETIRED = "breakdown.scene.retired"
LOCAL_EVENT_REORDERED = "breakdown.scene_order_adjusted"
LOCAL_EVENT_TYPES = (LOCAL_EVENT_SEALED, LOCAL_EVENT_SUPERSEDED,
                     LOCAL_EVENT_RETIRED, LOCAL_EVENT_REORDERED)


SCHEMA = """
PRAGMA foreign_keys = ON;

-- §5.1 容器。同一 (episode, branch) 允许多个 breakdown（§3.5.1）。
-- `head_revision` 是 CAS 的乐观锁；0 表示还没有任何版本。
CREATE TABLE IF NOT EXISTS breakdown (
  tenant_id       TEXT NOT NULL,
  breakdown_id    TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  series_id       TEXT NOT NULL,
  episode_id      TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  script_id       TEXT NOT NULL,
  lifecycle       TEXT NOT NULL CHECK
                    (lifecycle IN ('active','superseded','archived')),
  head_revision   INTEGER NOT NULL DEFAULT 0 CHECK (head_revision >= 0),
  sealed_revision INTEGER,
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, breakdown_id),
  CONSTRAINT bkd_sealed_le_head
    CHECK (sealed_revision IS NULL OR sealed_revision <= head_revision),
  CONSTRAINT bkd_sealed_positive
    CHECK (sealed_revision IS NULL OR sealed_revision >= 1)
);

CREATE INDEX IF NOT EXISTS bkd_by_episode
  ON breakdown (tenant_id, episode_id, branch_id, lifecycle);

-- §5.2 分场版本。不可变：任何改动都是新行（§3.5.2）。
-- `retired_scene_ids` 是这一版相对父版退休掉的场，seal 时随事件发出（§4.2）。
-- `scene_id_continuity` 记下这一版的继承策略，审计用。
CREATE TABLE IF NOT EXISTS breakdown_revision (
  tenant_id             TEXT NOT NULL,
  breakdown_id          TEXT NOT NULL,
  revision              INTEGER NOT NULL CHECK (revision >= 1),
  state                 TEXT NOT NULL CHECK
                          (state IN ('DRAFT','SEALED','SUPERSEDED','WITHDRAWN')),
  episode_id            TEXT NOT NULL,
  branch_id             TEXT NOT NULL,
  script_id             TEXT NOT NULL,
  script_revision       INTEGER NOT NULL CHECK (script_revision >= 1),
  script_content_hash   TEXT NOT NULL CHECK (length(script_content_hash) = 64),
  script_line_set_hash  TEXT NOT NULL CHECK (length(script_line_set_hash) = 64),
  context_pack_id       TEXT,
  context_pack_hash     TEXT,
  context_coverage      TEXT NOT NULL CHECK
                          (context_coverage IN ('full','partial','none')),
  produced_by           TEXT NOT NULL,
  parent_revision       INTEGER,
  scene_id_continuity   TEXT NOT NULL CHECK
                          (scene_id_continuity IN
                           ('fresh','inherit_by_line_anchor','inherit_explicit')),
  scene_count           INTEGER NOT NULL CHECK (scene_count > 0),
  requirement_count     INTEGER NOT NULL CHECK (requirement_count >= 0),
  retired_scene_ids     TEXT NOT NULL DEFAULT '[]',
  content_hash          TEXT NOT NULL CHECK (length(content_hash) = 64),
  content_fingerprint   TEXT NOT NULL CHECK (length(content_fingerprint) = 64),
  sealed_at             TEXT,
  created_at            TEXT NOT NULL,
  PRIMARY KEY (tenant_id, breakdown_id, revision),
  UNIQUE (tenant_id, breakdown_id, content_hash),
  UNIQUE (tenant_id, breakdown_id, content_fingerprint),
  FOREIGN KEY (tenant_id, breakdown_id)
    REFERENCES breakdown (tenant_id, breakdown_id),
  CONSTRAINT br_state_sealed_chk
    CHECK ((state = 'DRAFT') = (sealed_at IS NULL)),
  CONSTRAINT br_context_full_chk
    CHECK (context_coverage <> 'full' OR context_pack_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS br_by_state
  ON breakdown_revision (tenant_id, breakdown_id, state, revision DESC);

-- §5.3 场次身份。**没有 scene_order 列**（§1.1）。
-- `retired_at` 一旦落值不可清除（§3.6 第 5 条）—— 没有代码路径会把它写回 NULL。
CREATE TABLE IF NOT EXISTS scene (
  tenant_id            TEXT NOT NULL,
  scene_id             TEXT NOT NULL,
  project_id           TEXT NOT NULL,
  series_id            TEXT NOT NULL,
  episode_id           TEXT NOT NULL,
  branch_id            TEXT NOT NULL,
  first_breakdown_id   TEXT NOT NULL,
  first_revision       INTEGER NOT NULL CHECK (first_revision >= 1),
  retired_at           TEXT,
  retire_reason        TEXT CHECK (retire_reason IS NULL OR
                          retire_reason IN ('merged_into','removed','split')),
  merged_into_scene_id TEXT,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, scene_id),
  FOREIGN KEY (tenant_id, merged_into_scene_id)
    REFERENCES scene (tenant_id, scene_id),
  CONSTRAINT scene_merged_chk
    CHECK ((retire_reason = 'merged_into') = (merged_into_scene_id IS NOT NULL)),
  CONSTRAINT scene_retired_chk
    CHECK ((retired_at IS NULL) = (retire_reason IS NULL))
);

CREATE INDEX IF NOT EXISTS scene_by_episode
  ON scene (tenant_id, episode_id, branch_id, retired_at);

-- §5.4 场次内容（每个 revision 一行）。`scene_order` 与 `display_scene` 分开：
-- 前者是叙事顺序，后者是 UI 上印出来的数字（§5.4 行 660）。
-- 摘录三列要么都有、要么都空（§5.4 的 scene_rev_excerpt_chk）。
CREATE TABLE IF NOT EXISTS scene_revision (
  tenant_id        TEXT NOT NULL,
  breakdown_id     TEXT NOT NULL,
  revision         INTEGER NOT NULL CHECK (revision >= 1),
  scene_id         TEXT NOT NULL,
  scene_order      INTEGER NOT NULL CHECK (scene_order >= 1),
  display_episode  INTEGER NOT NULL CHECK (display_episode >= 1),
  display_scene    INTEGER NOT NULL CHECK (display_scene >= 1),
  heading_place    TEXT NOT NULL CHECK
                     (heading_place IN ('INT','EXT','INT-EXT')),
  heading_location TEXT NOT NULL,
  heading_tod      TEXT,
  start_line_id    TEXT NOT NULL,
  end_line_id      TEXT NOT NULL,
  line_count       INTEGER NOT NULL CHECK (line_count > 0),
  opening_line_id  TEXT,
  opening_text     TEXT,
  opening_text_hash TEXT,
  summary          TEXT NOT NULL,
  continuity_hints TEXT NOT NULL DEFAULT '[]',
  advisory         TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, breakdown_id, revision, scene_id),
  CONSTRAINT scene_rev_order_uq
    UNIQUE (tenant_id, breakdown_id, revision, scene_order),
  CONSTRAINT scene_rev_display_uq
    UNIQUE (tenant_id, breakdown_id, revision, display_episode, display_scene),
  CONSTRAINT scene_rev_excerpt_chk
    CHECK ((opening_line_id IS NULL) = (opening_text IS NULL)
       AND (opening_line_id IS NULL) = (opening_text_hash IS NULL)),
  CONSTRAINT scene_rev_excerpt_hash_chk
    CHECK (opening_text_hash IS NULL OR length(opening_text_hash) = 64),
  FOREIGN KEY (tenant_id, breakdown_id, revision)
    REFERENCES breakdown_revision (tenant_id, breakdown_id, revision),
  FOREIGN KEY (tenant_id, scene_id)
    REFERENCES scene (tenant_id, scene_id)
);

CREATE INDEX IF NOT EXISTS scene_rev_by_scene
  ON scene_revision (tenant_id, scene_id, revision DESC);

-- §5.5 场次参与。`speaking` / `enters` / `exits` 是观察，不是 S06 的指令。
CREATE TABLE IF NOT EXISTS scene_participant (
  tenant_id      TEXT NOT NULL,
  breakdown_id   TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  scene_id       TEXT NOT NULL,
  requirement_id TEXT NOT NULL,
  speaking       INTEGER NOT NULL CHECK (speaking IN (0,1)),
  enters         INTEGER NOT NULL CHECK (enters IN (0,1)),
  exits          INTEGER NOT NULL CHECK (exits IN (0,1)),
  PRIMARY KEY (tenant_id, breakdown_id, revision, scene_id, requirement_id),
  FOREIGN KEY (tenant_id, breakdown_id, revision, scene_id)
    REFERENCES scene_revision (tenant_id, breakdown_id, revision, scene_id),
  FOREIGN KEY (tenant_id, breakdown_id, revision, requirement_id)
    REFERENCES requirement (tenant_id, breakdown_id, revision, requirement_id)
);

-- §5.6 需求清单。**没有 asset_id / rights_ref 列**（§1.3）。
-- `label` 是人写的稳定键，本服务不规范化、不保证跨 revision 稳定（§10.4）。
CREATE TABLE IF NOT EXISTS requirement (
  tenant_id      TEXT NOT NULL,
  breakdown_id   TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  requirement_id TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK
                   (kind IN ('character','prop','scene','costume','vehicle',
                             'animal','sfx')),
  label          TEXT NOT NULL,
  spec_hint      TEXT NOT NULL DEFAULT '{}',
  first_scene_id TEXT NOT NULL,
  notes          TEXT,
  PRIMARY KEY (tenant_id, breakdown_id, revision, requirement_id),
  FOREIGN KEY (tenant_id, breakdown_id, revision)
    REFERENCES breakdown_revision (tenant_id, breakdown_id, revision)
);

CREATE INDEX IF NOT EXISTS requirement_by_label
  ON requirement (tenant_id, breakdown_id, revision, kind, label);

-- §5.7 场间关系。story clock 归 S16，这里只记"叙事上是什么关系"（§11 C10）。
CREATE TABLE IF NOT EXISTS scene_relation (
  tenant_id     TEXT NOT NULL,
  breakdown_id  TEXT NOT NULL,
  revision      INTEGER NOT NULL,
  from_scene_id TEXT NOT NULL,
  to_scene_id   TEXT NOT NULL,
  relation      TEXT NOT NULL CHECK
                  (relation IN ('adjacent','same_location','same_time',
                                'flash_forward','flash_back','parallel')),
  note          TEXT,
  PRIMARY KEY (tenant_id, breakdown_id, revision, from_scene_id,
               to_scene_id, relation),
  FOREIGN KEY (tenant_id, breakdown_id, revision, from_scene_id)
    REFERENCES scene_revision (tenant_id, breakdown_id, revision, scene_id),
  FOREIGN KEY (tenant_id, breakdown_id, revision, to_scene_id)
    REFERENCES scene_revision (tenant_id, breakdown_id, revision, scene_id),
  CONSTRAINT scene_relation_no_self CHECK (from_scene_id <> to_scene_id)
);

-- §5.8 需求解析缓存。**非权威**。任何"资产是否可用"的判定不得读它。
CREATE TABLE IF NOT EXISTS requirement_resolution_cache (
  tenant_id      TEXT NOT NULL,
  breakdown_id   TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  requirement_id TEXT NOT NULL,
  resolved_json  TEXT NOT NULL,
  resolved_at    TEXT NOT NULL,
  valid_until    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, breakdown_id, revision, requirement_id)
);

-- §3.4 / §8.3 的本地审计。**不是 outbox**：这些 event_type 没有登记在
-- `event-topics.v2.json`，塞进 outbox 会让中继投递一个没有 schema 的事件。
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
# 免得"能响应"被读成"契约完整"（与 S03 / S04 / S25 的 CONTRACT_GAPS 同构）。
CONTRACT_GAPS = [
    # ---- 方案 §11 的十一条 ----
    {"id": "C1", "kind": "gap",
     "summary": "service.yaml / service-catalog.v2.json 只声明 POST /breakdowns 与 "
                "GET /breakdowns/{id}；:seal、/revisions、scene-order、:retire、"
                "/requires 全部没有契约路径",
     "handling": "按方案 §2.2/§2.3 实现全部路径；不改 catalog、不改 openapi。"},
    {"id": "C2", "kind": "gap",
     "summary": "没有 SceneBreakdownRevision 的 JSON Schema（contracts/domain-v1/ "
                "下只有 shot-spec / change-plan / generation-request / qc-report）",
     "handling": "按方案 §4.1 实现，响应结构以本文为准；S06/S09/S10 缺少可校验的"
                 "共同基线。"},
    {"id": "C3", "kind": "gap",
     "summary": "breakdown.revision.created 的 data 最小字段集 schema 在方案 §11 里"
                "标记为待新增；仓库里实际已存在 "
                "contracts/breakdown.revision-created.v2.schema.json",
     "handling": "按已存在的那个 schema 发事件（12 个键、additionalProperties:false）；"
                 "§11 C3 描述的是写方案那一刻的状态。"},
    {"id": "C4", "kind": "gap",
     "summary": "没有契约定义 scene_id 由谁铸造、如何跨 revision 继承，catalog 的 "
                "data_owner 说明里也没有",
     "handling": "本服务铸造 scn_ 并实现 §3.3 的三种连续性策略；不改 catalog。"},
    {"id": "C5", "kind": "conflict",
     "summary": "重排不发事件（§6.4），但 S09 相邻边与 S41 EDL 依赖顺序；没有文件"
                "说明谁负责重排后刷新",
     "handling": "取方案立场 (b)：S09/S41 主动查 GET /episodes/{id}/scenes。"
                 "重排只写 local_event，不发 outbox。"},
    {"id": "C6", "kind": "gap",
     "summary": "catalog 的 depends_on 只有 S04/S19，缺 S18/S02/S01；depends_on "
                "是启动依赖还是运行协作未定义",
     "handling": "运行期依赖按注入 callback 表达；S04 未配置即 503，S18 未配置降级"
                 "为 partial，不假装成功。"},
    {"id": "C7", "kind": "needs_owner",
     "summary": "line_span 用 start_line_id / end_line_id 表达，但没有任何契约保证"
                "两个 ID 在 S04 里可比较顺序；行顺序目前只隐含在 seq 里",
     "handling": "本服务要求 S04 的行列表带唯一、单调的 seq，并据此实现校验②；"
                 "缺 seq 或 seq 重复时返回 503 SCRIPT_SOURCE_UNAVAILABLE（不猜顺序）。"
                 "需要 S04 明确 line_ordinal 单调且 (script_id, revision, "
                 "line_ordinal) 唯一。"},
    {"id": "C8", "kind": "gap",
     "summary": "requirements[].label 的匹配规则没有契约；S10 resolve 的输入契约里"
                "没有\"需求清单\"这个形态",
     "handling": "label 原样存、不做规范化；S05 与 S10 的匹配规则见 S05-G4。"
                 "需要新增 asset-requirement schema。"},
    {"id": "C9", "kind": "needs_owner",
     "summary": "generation-command.v2 与 event-envelope.v2 都要求 manifest_ref，"
                "但分场发生在 manifest 创建之前；人工预分场无法进行",
     "handling": "本实现不要求命令体带 manifest_ref（方案 §2.4 的示例也没有它）；"
                 "这是登记给 docs/07 与 execution-context 的缺口，不在代码里伪造。"},
    {"id": "C10", "kind": "gap",
     "summary": "scene_relation 的 flash_back/flash_forward 与 S16 story clock 语义"
                "重叠，没有文档说明谁是权威",
     "handling": "S05 只记录叙事关系，story clock 归 S16；不改 S16。"},
    {"id": "C11", "kind": "needs_owner",
     "summary": "max_scenes 上限（方案取 400）没有依据",
     "handling": "常量 MAX_SCENES_HARD = 400，超过即 400 INVALID_SCHEMA；"
                 "数字待产品裁决。"},

    # ---- contracts/breakdown.revision-created.v2.schema.json 的九条 x-conflicts ----
    {"id": "BRK-1", "kind": "conflict",
     "summary": "事件 data 带 branch_id，但 breakdown / breakdown_revision / scene "
                "的脚本 DDL 主键都没有 branch_id",
     "handling": "本实现的表在 PK 之外存 branch_id 并在查询里用它；事件照发。"
                 "BRK-1 指出的是契约层面的不一致，未替 owner 决定。"},
    {"id": "BRK-2", "kind": "conflict",
     "summary": "retired_scene_ids 丢掉了 retire_reason / merged_into_scene_id，"
                "而 S09 关闭边的归属无人负责",
     "handling": "事件只发 ID（schema additionalProperties:false）；"
                 "retire_reason 落在 scene 行与 local_event，消费方要区分需回源 "
                 "GET /scenes/{id}。"},
    {"id": "BRK-3", "kind": "conflict",
     "summary": "事件 state 枚举有 4 个值，实际只可能发 SEALED；SUPERSEDED "
                "永不单独发事件",
     "handling": "只在 seal 时发，state 恒为 SEALED（§6.3）；superseded 写 "
                 "local_event。"},
    {"id": "BRK-4", "kind": "conflict",
     "summary": "方案正文写 sha256: 前缀，事件 schema 与 char(64) 列要裸十六进制",
     "handling": "库里存裸 hex；查询响应按 §4.1 渲染 sha256: 前缀；事件发裸 hex。"},
    {"id": "BRK-5", "kind": "conflict",
     "summary": "事件不含 scene_order，S03 的 episode_scene_projection 需要 "
                "scene_ordinal",
     "handling": "事件照 schema 发；顺序由 GET /episodes/{id}/scenes 提供。"
                 "下游不得从事件推断顺序。"},
    {"id": "BRK-6", "kind": "conflict",
     "summary": "aggregate_sequence 有缺口（DRAFT 不发事件），消费方若假设连续会"
                "卡在缺口上",
     "handling": "按 §6.5 的规则写给下游：缓冲 30s 后回源；本服务不补发 DRAFT。"},
    {"id": "BRK-7", "kind": "conflict",
     "summary": "事件 source_script 三个键、响应四个键；事件的 content_hash 无法"
                "从事件自带字段复算",
     "handling": "事件按 schema 发三键；需要复算的消费方回源 "
                 "GET /breakdowns/{id}/revisions/{revision}。"},
    {"id": "BRK-8", "kind": "conflict",
     "summary": "方案示例的 envelope 与 SDK 约定有偏差：home_cell 'cell-a1' vs "
                "本仓 'cell_a1'；source 's05.breakdown' vs 'S05'；毫秒时间戳",
     "handling": "按本仓 SDK 约定：source=S05、home_cell=cell_a1、毫秒 UTC。"
                 "不改共享 SDK。"},
    {"id": "BRK-9", "kind": "conflict",
     "summary": "content_hash 包含 revision，导致 UNIQUE(tenant, breakdown, "
                "content_hash) 只在同一 revision 内生效，跨版本重复内容挡不住",
     "handling": "按方案算式实现（revision 进 hash）；重复提交同一 revision 时由"
                 "主键与幂等表兜底。要跨版本去重就必须改 content_hash 算式，"
                 "不在实现里擅自改。"},

    # ---- 实现时新发现的 ----
    {"id": "S05-G1", "kind": "gap",
     "summary": "§2.4 / §2.5 的请求体没有任何字段承载场次内容；人工分场"
                "（proposal_ref 为 null）无处传 scenes/requirements",
     "handling": "命令体接受内联 `proposal: {scenes, requirements, relations}`；"
                 "有 proposal_ref 且无内联时从 S19 取 result.proposal。这是补的"
                 "输入通道，不是新契约。"},
    {"id": "S05-G2", "kind": "gap",
     "summary": "方案 §2.6 没有给这几条校验失败码：定位不一致、line_count 非法、"
                "场次顺序列表不完整、引用已退休场、scene_id 冲突、需求种类不符、"
                "revision 非 DRAFT / 非活动",
     "handling": "本实现用 DISPLAY_LOCATOR_INVALID / INVALID_LINE_SPAN / "
                 "INVALID_SCENE_ORDER / SCENE_RETIRED / SCENE_ID_CONFLICT / "
                 "REQUIREMENT_KIND_MISMATCH / REVISION_NOT_DRAFT / "
                 "REVISION_NOT_ACTIVE（均 422，INVALID_SCENE_ORDER 为 400）。"},
    {"id": "S05-G3", "kind": "gap",
     "summary": "§7.2 要求 line_count = 0 拒绝，但没给错误码；DDL 只有 CHECK",
     "handling": "先返回 422 INVALID_LINE_SPAN（details.rule='line_count'）；"
                 "DB CHECK 是第二道。"},
    {"id": "S05-G4", "kind": "gap",
     "summary": "校验③的\"发言人属于本场角色\"没有匹配规则：是比 label、"
                "speaker_id，还是 spec_hint.character_id",
     "handling": "label == line.speaker_label，或 spec_hint.character_id == "
                 "line.speaker_id，任一成立即通过。规则需要与 S10 的 label 匹配"
                 "一起定（C8）。"},
    {"id": "S05-G5", "kind": "gap",
     "summary": "display_locator 的\"与 scene_order 一致\"没有定义方向："
                "display_scene 是必须等于 scene_order，还是只约束唯一",
     "handling": "要求 display_scene == 该场在本次提交里的 1-based 位置，"
                 "display_episode 在整版内一致；不一致 422 DISPLAY_LOCATOR_INVALID。"},
    {"id": "S05-G6", "kind": "gap",
     "summary": "T05-22 要求\"retired 场在新 revision 中被引用\"被拒，但 §3.3 只说"
                "继承时跳过未被继承的场，没给拒绝码",
     "handling": "inherit_explicit 把已退休的 scene_id 映射进新版本 → 422 "
                 "SCENE_RETIRED；自动继承路径遇到已退休场直接 mint 新 ID。"
                 "\"已退休\"读 `scene` 身份表而不是父 revision 的行 —— "
                 "`POST /scenes/{id}:retire` 直接改身份表，父行里看不出 "
                 "`retired_at`，只读父行会让这场在新版本里复活（T05-22 的"
                 "实现要点，回归用例 test_t05_22_retired_by_command_*）。"},
    {"id": "S05-G7", "kind": "gap",
     "summary": "inherit_explicit 的 scene_id_map 键空间没有定义（按新场下标？"
                "按行？按提案里的临时键？）",
     "handling": "键为新场的 `line_span.start_line_id`（与默认策略同一个锚），"
                 "值为要继承的旧 scene_id。未覆盖的场铸造新 ID。"},
    {"id": "S05-G8", "kind": "conflict",
     "summary": "T05-21 说两场继承同一个旧 scene_id 应\"拒绝\"，§3.3 的算法说"
                "第二个场\"铸造新 ID\"——两处对同一输入给出不同结果",
     "handling": "按 §3.3 的算法实现（第二个场 mint），因为它是不变量\"一个旧 ID "
                 "最多被一个新场继承\"的直接保证；测试钉住\"没有任何两个场共享 "
                 "scene_id\"。不替 owner 在\"拒绝 vs 静默 mint\"之间选边。"},
    {"id": "S05-G9", "kind": "gap",
     "summary": "§6.2 说并发 :seal 的 0 行更新\"→ 409\"，但没给错误码",
     "handling": "已 SEALED / SUPERSEDED / WITHDRAWN 时返回 409 REVISION_NOT_DRAFT"
                 "（details.state 给当前状态）。"},
    {"id": "S05-G10", "kind": "gap",
     "summary": "方案没有定义 merged_into / split 的退休理由从哪里来；"
                "§3.3 只说\"未继承的旧场标记 retired_at\"",
     "handling": "命令体接受可选 `retire_map: {old_scene_id: {reason, "
                 "merged_into_scene_id}}`；缺省 reason='removed'。缺省值不推断"
                 "合并关系。"},
    {"id": "S05-G11", "kind": "conflict",
     "summary": "§3.4 允许在同一 revision 内改 scene_order（不推进 revision），"
                "§3.5.2 说 SEALED 不可修改——同一行的两个说法",
     "handling": "允许对 SEALED revision 重排（§3.4 的存在意义就是不改内容也能"
                 "调整顺序），只禁止对 SUPERSEDED / WITHDRAWN 重排（409 "
                 "REVISION_NOT_ACTIVE）。重排不碰 content_hash。"},
    {"id": "S05-G12", "kind": "conflict",
     "summary": "§4.1 的 content_hash 含 scenes，而 scenes 含 scene_order / "
                "display_locator；T05-19 又要求重排后 content_hash 不变",
     "handling": "算 hash 时对 scenes 取内容投影，显式排除 scene_order 与 "
                 "display_locator（它们在 §1.1 就是显示值）。算式登记在此。"},
    {"id": "S05-G13", "kind": "needs_owner",
     "summary": "options.max_scenes 允许调用方放宽到多少没有定义（C11 只给了"
                "默认值 400）",
     "handling": "接受 1..400，超过 400 返回 400 INVALID_SCHEMA；不放宽上限。"},
    {"id": "S05-G14", "kind": "gap",
     "summary": "characters[] 引用的 requirement 必须是 kind='character'，"
                "方案没有明说，也没有错误码",
     "handling": "引用到非 character 需求 → 422 REQUIREMENT_KIND_MISMATCH。"},
    {"id": "S05-G15", "kind": "gap",
     "summary": "requirements[].requirement_id 由谁铸造没有定义（§3.2 只点名 "
                "scene_id / breakdown_id / revision / tenant_id 要丢弃）",
     "handling": "沿用 proposal 里的 requirement_id，但要求在本 revision 内唯一、"
                 "且所有 requirement_ref 都能解析；S05 不用它做跨 revision 身份。"},
    {"id": "S05-G16", "kind": "gap",
     "summary": "§5.9 的 outbox 有 UNIQUE(aggregate_id, aggregate_sequence, "
                "event_type) 与 published_at 索引；event_sdk 的 OUTBOX_DDL 没有"
                "这条唯一约束；SQLite 也没有 DEFERRABLE 唯一约束",
     "handling": "aggregate_sequence = revision（一版一条事件，唯一性由 seal "
                 "只能成功一次保证）；重排用\"加偏移再重写 1..N\"代替 DEFERRABLE。"
                 "不改共享 SDK。"},
    {"id": "S05-G17", "kind": "conflict",
     "summary": "§4.1 的 content_hash 含 breakdown_id 与 revision（BRK-9），因此"
                "它只能在同一个 revision 内挡重复；T05-03 / T05-06 要求\"同内容"
                "不新建 revision\"，两者不是同一个对象",
     "handling": "另存一个与容器/版本/身份无关的 content_fingerprint"
                 "（去掉 scene_id 与显示值），并加 UNIQUE(tenant_id, "
                 "breakdown_id, content_fingerprint)。content_hash 算式不变。"},
    {"id": "S05-G18", "kind": "gap",
     "summary": "§2.4 的 options.preview=true 要求\"返回不持久化的结果\"，但"
                "没有定义返回什么形状、也没有说不落库时能不能拿到场景 ID",
     "handling": "接受 preview 选项并原样回显；**本实现不做不落库预览**——"
                 "预览需要一条与落库路径不同的返回结构，方案没有给，不伪造。"
                 "调用方要走预览应先在 S19 侧拿 proposal。"},
    {"id": "S05-G19", "kind": "conflict",
     "summary": "方案 §4.2 的 data_ref 是对象 {service_id,id,revision,"
                "content_hash}，event-sdk 的 new_event 把 data_ref 标成 str|None",
     "handling": "与 S04 一致地传对象（SDK 不做运行时校验）；若将来 SDK 加上"
                 "运行时校验，这条会立刻炸，需要先改 SDK 或改契约，不在实现里选边。"},
    {"id": "S05-G20", "kind": "gap",
     "summary": "§1.2 要求摘录与 S04 逐字节比对，但没有任何契约保证 S04 的行列表"
                "带正文（line_revision 的 text 在 S04 是正文存储，行摘要可能不带）",
     "handling": "S04 返回 text 时逐字节比对 + 校验 text_hash；只返回 text_hash "
                 "时只比 hash，并在代码注释里写明\"没有正文就没有逐字节相等这个"
                 "事实\"，不假装验过。需要 S04 明确行列表是否带正文。"},
    {"id": "S05-G21", "kind": "gap",
     "summary": "§3.2 只点名 tenant_id 出现即拒绝，但 project_id / series_id "
                "同样属于平台签名绑定的租户上下文；方案没说它们出现在 proposal "
                "里算不算污染",
     "handling": "三者任一出现即 422 AGENT_SUPPLIED_TENANT（比\"只拒 tenant_id"
                 "\"更严格），details 里点名命中的字段。这与 AGENTS.md"
                 "\"租户上下文必须由平台签名绑定\"一致。"},
    {"id": "S05-G22", "kind": "gap",
     "summary": "breakdown.created_by 与审计里的\"谁\"没有来源：本仓 HTTP 层没有"
                "TCT 验签中间件，actor 缺失时无法确定操作者",
     "handling": "actor 缺失时如实记 principal_id='unspecified' / "
                 "principal_type='unknown'，不编一个看起来像真人的 ID；"
                 "有 X-Principal-Id 时以它为准。这是过渡态，不是安全边界。"},
    {"id": "S05-G23", "kind": "gap",
     "summary": "§2.4 的 context_pack_ref 与 §4.1 的 context.context_pack_ref "
                "同形，但 §7.1 说\"缺失允许、coverage=partial\"，没有说 coverage "
                "为 none 的场景",
     "handling": "context_pack_ref 缺失 → coverage='partial'（方案 §7.1 明写）；"
                 "CONTEXT_COVERAGES 仍保留 'none' 供将来使用，本实现不产出它。"},
    {"id": "S05-G24", "kind": "gap",
     "summary": "T05-17 要求\"一场拆成两场\"时原场退休，但 §3.3 的继承算法只按"
                "首行锚点继承——拆出的第一个新场会继承原 ID，于是原场永远不会"
                "被退休，split 这个 retire_reason 也无处产生",
     "handling": "命令体接受 retire_map 显式点名要退休的旧场；被点名的旧场不"
                 "参与自动继承，从而让拆分表达得出来。缺省不推断拆分。"},
    {"id": "S05-G25", "kind": "gap",
     "summary": "§3.6 第 7 条要求 source_script 的 content_hash / line_set_hash "
                "与写入时的 S04 状态一致，但没有说 S04 自报的 line_set_hash 与"
                "它返回的行列表不一致时怎么办",
     "handling": "用返回的行列表复算 line_set_hash，不一致即 503 "
                 "SCRIPT_SOURCE_UNAVAILABLE（不照抄上游声明）。与 S04 同码同"
                 "算法，见 C7。"},
    {"id": "S05-G26", "kind": "gap",
     "summary": "inherit_by_line_anchor 跨 revision 时，父版的 start/end line_id "
                "可能已不在新剧本里；方案没有定义此时如何判断\"包含\"",
     "handling": "能比 seq 才比（两端都在当前行索引里），比不了就判\"不包含\""
                 "并铸造新 ID —— 宁可断一次身份，也不猜一个可能错的继承。"},
]
