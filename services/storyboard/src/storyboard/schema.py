"""S06 自己的 schema。表来源是 `plans/S06-storyboard.md` §5。

六条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **`shot` 没有 `shot_order` 列，`shot_revision` 才有。** 镜头的身份
   （`shot_id`）与"第几镜"是两个东西（§1.1 / §3.4）。一旦把序号写进 `shot`，
   插入一镜就变成整场重命名，而且历史 revision 的顺序无从保留。
2. **`shot_lineage_id` 用数据库触发器锁死。** 业务层没有 `UPDATE ... SET
   shot_lineage_id` 这一行代码，触发器是第二道、也是唯一一道不依赖纪律的防线
   （§3.2.5 / T06-13）。它是本服务唯一一条"事后无法补救"的约束。
3. **`shot_dialogue_ref` 的 `text_copy` 不是第二份剧本权威**，只是写库那一刻的
   字节副本，用于漂移检测与审计（§1.2 / §5.6）。任何读取"当前台词"的路径必须
   回 S04。
4. **`shot_revision.shot_spec` 一经插入不可变**（触发器只放行 `shot_order` /
   `display_shot` / `state` / `sealed_at`）。重排改的是顺序，不是内容（§6.4）。
5. **没有 `inbox` / `attempt_counter` / `cost_estimate` / `render_preview`。**
   分别是 S06（消费）、S26/S29、S25、S26/S28 的事实（§5.9）。尝试计数尤其不能
   建：S06 一旦自己记一次尝试，就变成第二个权威，E26 会静默失效。
6. **`shot_lineage` 只增不删。** 回滚不得重建或清空它（§7.6）——那会摧毁所有跨
   Run 的尝试锚点。任何涉及它的迁移必须是纯增量。

本仓服务层一律 SQLite（`isolation_level=None` + 显式 `Transaction`），所以方案里的
`jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`char(64)` 存裸十六进制、
`numeric(8,3)` 存 REAL、`boolean` 存 INTEGER 0/1。
`outbox` / `idempotency_record` 由平台 SDK 建表，这里不重复 DDL。
SQLite 没有 `DEFERRABLE INITIALLY DEFERRED`，重排改用"先加偏移再重写 1..N"
（与 S03 / S05 同一手法），差异登记在 S06-G05。
"""

# ---------------------------------------------------------------- 常量

# §5 行 587：ID 前缀 `sbd_ / sht_ / shl_`。
ID_PREFIX = {
    "storyboard": "sbd_",
    "shot": "sht_",
    "lineage": "shl_",
    "local_event": "lev_",
}

SCHEMA_VERSION = "2.0"
SOURCE = "S06"
TOPIC = "drama.storyboard.v2"
# §4.3：所有查询响应必须带这个标记，否则未来就会有人往这里写数据。
AUTHORITATIVE_SOURCE = "s06.storyboard"

STORYBOARD_LIFECYCLES = ("active", "superseded", "archived")
# §3.6.1。技术上 revision 行一经插入即不可变，任何改动都是新行。
REVISION_STATES = ("DRAFT", "SEALED", "SUPERSEDED", "WITHDRAWN")
SHOT_STATES = ("active", "retired")
LINEAGE_STATES = ("live", "retired")

# §3.6.1 的 Storyboard 与 ShotSpecRevision 迁移表。终态不再前进。
REVISION_TRANSITIONS = {
    "DRAFT": {"SEALED", "WITHDRAWN"},
    "SEALED": {"SUPERSEDED"},
    "SUPERSEDED": set(),
    "WITHDRAWN": set(),
}

# §3.3.2。`inherit_by_beat_anchor` 是默认值（§12 U3 的临时立场）。
CONTINUITY_MODES = ("fresh", "inherit_by_beat_anchor", "inherit_explicit",
                    "inherit_by_proposal")
DEFAULT_CONTINUITY = "inherit_by_beat_anchor"
# §3.3.3：`inherit_by_proposal` 只允许人工路径。
MANUAL_CONTINUITY = ("inherit_explicit", "inherit_by_proposal")

SHOT_TYPES = ("dialogue", "action", "camera_motion", "vfx", "establishing",
              "transition")
SHOT_SIZES = ("extreme_wide", "wide", "full", "medium", "medium_close",
              "close", "extreme_close")
CAMERA_ANGLES = ("eye_level", "low", "high", "overhead", "dutch")
CAMERA_MOVEMENTS = ("static", "push_in", "pull_out", "pan", "tilt", "tracking",
                    "orbit", "handheld")
TIMING_POLICIES = ("ripple", "preserve_episode_duration")
SCENE_ORDER_SCOPE = ("scene",)

# §3.5.1 朗读速率。**服务配置**，按 locale 与情绪查表，不硬编码在算法里；
# 默认值基于中文配音经验的估算，未经实测（§12 U2）。
CHARS_PER_SECOND = {"zh-CN": 4.2, "en-US": 14.0}
DEFAULT_CHARS_PER_SECOND = 4.2
EMOTION_FACTOR = {"whisper": 1.35, "shout": 1.15}
DEFAULT_EMOTION_FACTOR = 1.0
INTER_LINE_PAUSE_SECONDS = 0.25

# §3.5 / §12 U1：单镜最小时长 1.0s，没有实测依据。
MIN_SHOT_SECONDS = 1.0

# §2.4 的 options.max_shots_per_scene 默认 80；上限没有依据，取同一数值并
# 登记（S06-G06）。
MAX_SHOTS_PER_SCENE_DEFAULT = 80
MAX_SHOTS_PER_SCENE_HARD = 80

# 重排时给 `shot_order` / `display_shot` 加的临时偏移。必须大于任何合法镜头数，
# 保证与未重写的行不撞唯一约束。
ORDER_OFFSET = 100000

# §2.2 的六条命令 + 六条查询。`service.yaml` 只声明第一条与单镜查询（§11 C1），
# 本实现按方案补全，不改契约。
COMMANDS = (
    "POST /storyboards/{storyboard_id}/revisions",
    "POST /shots/{shot_id}/revisions",
    "POST /storyboards/{storyboard_id}/revisions/{revision}:seal",
    "POST /shots/{shot_id}:retire",
    "POST /storyboards/{storyboard_id}/revisions/{revision}/shot-order",
    "POST /shots/{shot_id}:split",
)
QUERIES = (
    "GET /storyboards/{storyboard_id}",
    "GET /storyboards/{storyboard_id}/revisions/{revision}",
    "GET /shots/{shot_id}",
    "GET /shots/{shot_id}/revisions/{revision}",
    "GET /scenes/{scene_id}/shots",
    "GET /shots/{shot_id}/lineage",
)

# 本地审计事件。`storyboard.shot.revision.created` 是唯一登记进 outbox 的类型
# （`contracts/event-topics.v2.json`）；其余写 `local_event`（§8.3）。把未登记的
# event_type 塞进 outbox 等于让中继去投递一个没有 schema 的事件。
LOCAL_EVENT_REVISION_CREATED = "storyboard.revision.created"   # §4.2，未登记
LOCAL_EVENT_SEALED = "storyboard.revision.sealed"
LOCAL_EVENT_SUPERSEDED = "storyboard.revision.superseded"
LOCAL_EVENT_RETIRED = "storyboard.shot.retired"
LOCAL_EVENT_SPLIT = "storyboard.shot.split"
LOCAL_EVENT_LINEAGE_MINTED = "storyboard.shot_lineage.minted"
LOCAL_EVENT_LINEAGE_DISCARDED = "storyboard.shot_lineage.discarded"
LOCAL_EVENT_REORDERED = "storyboard.shot_order_adjusted"
LOCAL_EVENT_TYPES = (
    LOCAL_EVENT_REVISION_CREATED, LOCAL_EVENT_SEALED, LOCAL_EVENT_SUPERSEDED,
    LOCAL_EVENT_RETIRED, LOCAL_EVENT_SPLIT, LOCAL_EVENT_LINEAGE_MINTED,
    LOCAL_EVENT_LINEAGE_DISCARDED, LOCAL_EVENT_REORDERED)


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- §5.1 容器。同一 (episode, branch) 允许多个 storyboard（§3.6.1）。
-- `head_revision` 是 CAS 的乐观锁；0 表示还没有任何版本。
CREATE TABLE IF NOT EXISTS storyboard (
  tenant_id       TEXT NOT NULL,
  storyboard_id   TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  series_id       TEXT NOT NULL,
  episode_id      TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  breakdown_id    TEXT NOT NULL,
  lifecycle       TEXT NOT NULL CHECK
                    (lifecycle IN ('active','superseded','archived')),
  head_revision   INTEGER NOT NULL DEFAULT 0 CHECK (head_revision >= 0),
  sealed_revision INTEGER,
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, storyboard_id),
  CONSTRAINT sbd_sealed_le_head
    CHECK (sealed_revision IS NULL OR sealed_revision <= head_revision),
  CONSTRAINT sbd_sealed_positive
    CHECK (sealed_revision IS NULL OR sealed_revision >= 1)
);

CREATE INDEX IF NOT EXISTS sbd_by_episode
  ON storyboard (tenant_id, episode_id, branch_id, lifecycle);

-- §5.2 分镜版本。不可变：任何改动都是新行。
-- `content_fingerprint` 与 S05-G17 同一考虑：`content_hash` 含 revision，
-- 只能挡同一版本的重复内容；T06-04 要的是"同内容不新建"，所以另存一个与
-- 容器 / 版本 / 身份都无关的指纹（S06-G07）。
CREATE TABLE IF NOT EXISTS storyboard_revision (
  tenant_id            TEXT NOT NULL,
  storyboard_id        TEXT NOT NULL,
  revision             INTEGER NOT NULL CHECK (revision >= 1),
  state                TEXT NOT NULL CHECK
                         (state IN ('DRAFT','SEALED','SUPERSEDED','WITHDRAWN')),
  episode_id           TEXT NOT NULL,
  branch_id            TEXT NOT NULL,
  breakdown_id         TEXT NOT NULL,
  breakdown_revision   INTEGER NOT NULL CHECK (breakdown_revision >= 1),
  breakdown_hash       TEXT NOT NULL CHECK (length(breakdown_hash) = 64),
  script_id            TEXT NOT NULL,
  script_revision      INTEGER NOT NULL CHECK (script_revision >= 1),
  script_hash          TEXT NOT NULL CHECK (length(script_hash) = 64),
  binding_set_id       TEXT,
  binding_set_revision INTEGER,
  binding_set_hash     TEXT,
  context_pack_id      TEXT,
  context_pack_hash    TEXT,
  context_coverage     TEXT NOT NULL CHECK
                         (context_coverage IN ('full','partial','none')),
  policy_ref           TEXT NOT NULL DEFAULT '{}',
  proposal_ref         TEXT NOT NULL DEFAULT '{}',
  options              TEXT NOT NULL DEFAULT '{}',
  produced_by          TEXT NOT NULL,
  parent_revision      INTEGER,
  scene_ids            TEXT NOT NULL DEFAULT '[]',
  shot_count           INTEGER NOT NULL CHECK (shot_count >= 0),
  changed_shot_ids     TEXT NOT NULL DEFAULT '[]',
  retired_shot_ids     TEXT NOT NULL DEFAULT '[]',
  lineage_mints        TEXT NOT NULL DEFAULT '[]',
  content_hash         TEXT NOT NULL CHECK (length(content_hash) = 64),
  content_fingerprint  TEXT NOT NULL CHECK (length(content_fingerprint) = 64),
  sealed_at            TEXT,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, storyboard_id, revision),
  UNIQUE (tenant_id, storyboard_id, content_hash),
  UNIQUE (tenant_id, storyboard_id, content_fingerprint),
  FOREIGN KEY (tenant_id, storyboard_id)
    REFERENCES storyboard (tenant_id, storyboard_id),
  CONSTRAINT sbr_state_sealed_chk
    CHECK ((state = 'DRAFT') = (sealed_at IS NULL)),
  CONSTRAINT sbr_context_full_chk
    CHECK (context_coverage <> 'full' OR context_pack_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS sbr_by_state
  ON storyboard_revision (tenant_id, storyboard_id, state, revision DESC);

-- §5.3 镜头血统。scope=(tenant, series)，永不复用；只增不删（§7.6）。
CREATE TABLE IF NOT EXISTS shot_lineage (
  tenant_id        TEXT NOT NULL,
  shot_lineage_id  TEXT NOT NULL,
  series_id        TEXT NOT NULL,
  origin_branch_id TEXT NOT NULL,
  origin_shot_id   TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('live','retired')),
  retired_at       TEXT,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, shot_lineage_id),
  CONSTRAINT lineage_retired_chk
    CHECK ((state = 'retired') = (retired_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS shot_lineage_by_series
  ON shot_lineage (tenant_id, series_id, state);

-- §5.4 镜头（分支内身份）。**没有 shot_order 列**（§1.1）。
-- `shot_lineage_id` 的不可变由下面的触发器强制，不靠代码纪律。
CREATE TABLE IF NOT EXISTS shot (
  tenant_id        TEXT NOT NULL,
  branch_id        TEXT NOT NULL,
  shot_id          TEXT NOT NULL,
  shot_lineage_id  TEXT NOT NULL,
  storyboard_id    TEXT NOT NULL,
  episode_id       TEXT NOT NULL,
  scene_id         TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('active','retired')),
  origin_shot_id   TEXT,
  first_revision   INTEGER NOT NULL CHECK (first_revision >= 1),
  retired_at       TEXT,
  retire_reason    TEXT CHECK (retire_reason IS NULL OR
                               retire_reason IN ('removed','merged','replaced')),
  created_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, branch_id, shot_id),
  UNIQUE (tenant_id, branch_id, shot_id, shot_lineage_id),
  FOREIGN KEY (tenant_id, shot_lineage_id)
    REFERENCES shot_lineage (tenant_id, shot_lineage_id),
  CONSTRAINT shot_retired_chk
    CHECK ((state = 'retired') = (retired_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS shot_by_scene
  ON shot (tenant_id, branch_id, scene_id, state);
CREATE INDEX IF NOT EXISTS shot_by_lineage
  ON shot (tenant_id, shot_lineage_id);

-- §3.2.5：lineage 一经写入永不变更。业务代码里没有任何一行 UPDATE 会碰这一列；
-- 这个触发器是为了挡住"顺手修一下"——那种改动不报错，只会让 E26 静默失效。
CREATE TRIGGER IF NOT EXISTS shot_lineage_immutable
BEFORE UPDATE OF shot_lineage_id ON shot
FOR EACH ROW
WHEN NEW.shot_lineage_id <> OLD.shot_lineage_id
BEGIN
  -- SQLite 的 RAISE() 只接受字面量消息（不接受表达式拼接），所以错误里没有
  -- shot_id；调用方要定位就查 OLD 行本身 —— 一条 UPDATE 只有一个 shot。
  SELECT RAISE(ABORT, 'shot_lineage_id is immutable');
END;

-- §5.5 镜头内容（每个 storyboard revision 一行）。不可变，只有顺序与状态可改。
-- `shot_order` 与 `display_shot` 是这一版分镜里的排列，属于内容快照。
CREATE TABLE IF NOT EXISTS shot_revision (
  tenant_id           TEXT NOT NULL,
  branch_id           TEXT NOT NULL,
  shot_id             TEXT NOT NULL,
  revision            INTEGER NOT NULL CHECK (revision >= 1),
  state               TEXT NOT NULL CHECK
                        (state IN ('DRAFT','SEALED','SUPERSEDED','WITHDRAWN')),
  storyboard_id       TEXT NOT NULL,
  storyboard_revision INTEGER NOT NULL CHECK (storyboard_revision >= 1),
  scene_id            TEXT NOT NULL,
  shot_order          INTEGER NOT NULL CHECK (shot_order >= 1),
  display_shot        INTEGER NOT NULL CHECK (display_shot >= 1),
  shot_spec           TEXT NOT NULL,
  content_hash        TEXT NOT NULL CHECK (length(content_hash) = 64),
  target_seconds      REAL NOT NULL CHECK (target_seconds > 0),
  min_seconds_ok      INTEGER NOT NULL CHECK (min_seconds_ok IN (0,1)),
  feasibility         TEXT NOT NULL,
  dialogue_texts      TEXT NOT NULL,
  parent_revision     INTEGER,
  produced_by         TEXT NOT NULL,
  sealed_at           TEXT,
  created_at          TEXT NOT NULL,
  PRIMARY KEY (tenant_id, branch_id, shot_id, revision),
  -- `shot_order` 是**一版分镜内的全局序号**（按场块、场内顺序展平后的 1..N），
  -- `display_shot` 是**场内序号**（C7）。两者分开存是因为 §3.4 的顺序是两层：
  -- 场块之间按场序，场内按 shot_order。若把 shot_order 也做成场内序号，两场
  -- 的第 1 镜就会撞下面这条唯一约束——而"跨场不允许重排"正是靠场块固定成立。
  CONSTRAINT shot_rev_order_uq UNIQUE
    (tenant_id, branch_id, storyboard_id, storyboard_revision, shot_order),
  CONSTRAINT shot_rev_display_uq UNIQUE
    (tenant_id, branch_id, storyboard_id, storyboard_revision, scene_id,
     display_shot),
  -- 方案 §5.5 写的是 (tenant, branch, shot_id, content_hash) 全局唯一；那会让
  -- "新分镜版本复制未改动的镜头"无法实现（同一个 hash 第二次插入必然撞），
  -- 而 E03 局部改镜与整场快照都依赖复制。这里收窄到"同一 storyboard revision
  -- 内同镜同内容只许一行"，去重交给 storyboard 级 fingerprint（S06-G04）。
  CONSTRAINT shot_rev_content_uq UNIQUE
    (tenant_id, branch_id, shot_id, storyboard_revision, content_hash),
  CONSTRAINT shot_rev_sealed_chk CHECK ((state = 'DRAFT') = (sealed_at IS NULL)),
  FOREIGN KEY (tenant_id, branch_id, shot_id)
    REFERENCES shot (tenant_id, branch_id, shot_id),
  FOREIGN KEY (tenant_id, storyboard_id, storyboard_revision)
    REFERENCES storyboard_revision (tenant_id, storyboard_id, revision)
);

CREATE INDEX IF NOT EXISTS shot_rev_by_storyboard
  ON shot_revision (tenant_id, storyboard_id, storyboard_revision, shot_order);
CREATE INDEX IF NOT EXISTS shot_rev_by_shot
  ON shot_revision (tenant_id, branch_id, shot_id, revision DESC);

-- 内容不可变：只放行顺序、显示序、状态与 sealed_at。重排（§6.4）走 shot_order，
-- 任何"顺手改一下 shot_spec"的路径会在这里炸。
--
-- `display_locator` 是**例外**，而且是必须的例外：§1.1 说它是显示值，S06-G07
-- 的 `content_hash` 投影也显式把它排除在外（否则 T06-47"重排后 content_hash
-- 不变"与"重排要改 display_locator.shot"会直接矛盾）。所以比较时先把它摘掉，
-- 只锁真正的内容字段。不摘的话，重排就只能改 `display_shot` 列、让库里的
-- `shot_spec.display_locator` 停在旧值上——同一份文档里两个"场内序号"打架。
CREATE TRIGGER IF NOT EXISTS shot_revision_content_immutable
BEFORE UPDATE ON shot_revision
FOR EACH ROW
WHEN json_remove(NEW.shot_spec, '$.display_locator')
     <> json_remove(OLD.shot_spec, '$.display_locator')
  OR NEW.content_hash <> OLD.content_hash
  OR NEW.scene_id <> OLD.scene_id
  OR NEW.shot_id <> OLD.shot_id
  OR NEW.revision <> OLD.revision
  OR NEW.storyboard_revision <> OLD.storyboard_revision
  OR NEW.target_seconds <> OLD.target_seconds
  OR NEW.dialogue_texts <> OLD.dialogue_texts
BEGIN
  SELECT RAISE(ABORT, 'shot_revision content is immutable');
END;

-- §5.6 对白行引用（防漂移）。`text_copy` 只用于校验与审计（S35 / S45），
-- 任何读取"当前台词"的路径必须去 S04（§1.2）。
CREATE TABLE IF NOT EXISTS shot_dialogue_ref (
  tenant_id       TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  shot_id         TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  line_id         TEXT NOT NULL,
  script_id       TEXT NOT NULL,
  script_revision INTEGER NOT NULL,
  text_hash       TEXT NOT NULL CHECK (length(text_hash) = 64),
  text_copy       TEXT NOT NULL,
  emotion         TEXT,
  PRIMARY KEY (tenant_id, branch_id, shot_id, revision, line_id),
  FOREIGN KEY (tenant_id, branch_id, shot_id, revision)
    REFERENCES shot_revision (tenant_id, branch_id, shot_id, revision)
);

CREATE INDEX IF NOT EXISTS shot_dialogue_by_line
  ON shot_dialogue_ref (tenant_id, script_id, line_id);

-- §5.7 节拍。重叠检查在应用层做（§5.7 末段），不用 EXCLUDE。
CREATE TABLE IF NOT EXISTS shot_beat (
  tenant_id     TEXT NOT NULL,
  branch_id     TEXT NOT NULL,
  shot_id       TEXT NOT NULL,
  revision      INTEGER NOT NULL,
  beat_index    INTEGER NOT NULL CHECK (beat_index >= 0),
  start_seconds REAL NOT NULL CHECK (start_seconds >= 0),
  end_seconds   REAL NOT NULL CHECK (end_seconds > start_seconds),
  action        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, branch_id, shot_id, revision, beat_index),
  FOREIGN KEY (tenant_id, branch_id, shot_id, revision)
    REFERENCES shot_revision (tenant_id, branch_id, shot_id, revision)
);

-- §8.3 的本地审计。**不是 outbox**：这些 event_type 没有登记在
-- `event-topics.v2.json`，塞进 outbox 会让中继投递一个没有 schema 的事件。
-- lineage 铸造单独留痕（§8.3），因为它是唯一不可逆的身份事件。
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
"""


# ---------------------------------------------------------------- 契约缺口

CONTRACT_GAPS = [
    {"id": "C1", "kind": "gap",
     "summary": "service.yaml 只声明 POST /storyboards/{id}/revisions 与 "
                "GET /shots/{id}/revisions/{revision}；:seal / /shots/{id}/"
                "revisions / :retire / :split / shot-order / /lineage / "
                "/scenes/{id}/shots 都没有",
     "handling": "按 §2.2 / §2.3 全部实现，不改 catalog、不改 openapi；"
                 "缺口留在契约侧由维护方补 s06-storyboard.v2.yaml。"},
    {"id": "C2", "kind": "conflict",
     "summary": "shot-spec.v1 没有 shot_lineage_id，也没有 script_line_ref，"
                "且 additionalProperties:false 让两者都放不进去",
     "handling": "lineage 与 script_line_refs 放在响应包装层 s06_meta（§4.1）；"
                 "shot-spec.v1 本身不掺入任何非契约字段。v2 落定后必须移除"
                 "包装层，否则同一信息有两处权威。"},
    {"id": "C3", "kind": "gap",
     "summary": "event-topics.v2.json 只登记 storyboard.shot.revision.created，"
                "§4.2 的 storyboard.revision.created 没有 topic 也没有 payload "
                "schema；§4.3 的括号自相矛盾（说'不存在'，又说沿用该名字）",
     "handling": "整场事件写 local_event（完整 v2 信封），只有已登记的单镜事件"
                 "进 outbox。不为未登记类型编 topic——那会让中继投递一个没有 "
                 "schema 的事件。"},
    {"id": "C4", "kind": "needs_owner",
     "summary": "shot_lineage_id 的铸造权威全仓未定义（S03 / S09 / S39 三处"
                "登记）。方案确立 S06 铸造，但没写进 service-catalog 的 "
                "data_owner",
     "handling": "本实现按 §3.2 铸造；catalog 未改（本任务禁止）。"},
    {"id": "C5", "kind": "gap",
     "summary": "重排不发事件，S41 EDL 与 S09 相邻边的刷新责任未定义",
     "handling": "取 §6.4 的 (b)：S41 / S09 主动查；重排只写本地审计。"},
    {"id": "C6", "kind": "gap",
     "summary": "S06 的 depends_on 是 [S05, S18, S19]，缺 S04（对白权威）、"
                "S10（绑定集）、S01、S02",
     "handling": "按 §7.5 处理：S05 / S04 不可用即 503，S10 / S18 可降级，"
                 "S19 缺失走人工路径。"},
    {"id": "C7", "kind": "gap",
     "summary": "display_locator.shot 没有唯一性约束，也没说明是场内序号还是"
                "全集序号",
     "handling": "取场内序号（§3.4 两层顺序），并在同一 storyboard revision 内"
                 "加 UNIQUE 约束把这条含义变成事实。"},
    {"id": "C8", "kind": "gap",
     "summary": "constraints.locked_fields 的解除路径未定义，而 §10.5 要求同一"
                "lineage 上单调不减",
     "handling": "本实现不接受任何移除 locked_fields 的路径（新 revision 若比"
                 "父 revision 少了任一项 → 422 LOCKED_FIELD_REMOVED）；:unlock "
                 "命令未实现（契约里不存在）。"},
    {"id": "C9", "kind": "gap",
     "summary": "beats.time_range 是秒但没有 fps 精度约定；target_seconds 也是"
                "浮点",
     "handling": "全部以秒（REAL）存储，比较用 1e-6 容差；不做任何帧对齐。"},
    {"id": "C10", "kind": "gap",
     "summary": "camera 的 movement / framing 枚举不完整，shot-spec.v1 的 "
                "camera 要求 shot_size / angle / movement / movement_intensity / "
                "composition 五键，而 §4.1 的示例用 focal_length_mm / framing",
     "handling": "严格按 shot-spec.v1 校验（schema 是权威）；§4.1 示例里的 "
                 "focal_length_mm / framing 不被接受，登记为方案的示例过期。"},
    {"id": "C11", "kind": "gap",
     "summary": "shot-spec.v1 没有'episode_id / scene_id 必须与 breakdown_ref "
                "一致'的约束",
     "handling": "本服务在 §3.1 的校验里自加：不一致即 422 SCENE_NOT_IN_"
                 "BREAKDOWN。"},
    {"id": "C12", "kind": "needs_owner",
     "summary": "min_shot_seconds 取 1.0s 无依据（§12 U1）",
     "handling": "按方案取值，常量集中在 schema.py，等创作与供应商裁决。"},
    {"id": "S06-G01", "kind": "gap",
     "summary": "§3.1 一处说'每一行有对白的行'，另一处说'line_span 内的每一行'",
     "handling": "以第一句为准：只有带 speaker 的行必须被覆盖；无 speaker 的行"
                 "（纯动作行）不要求出现在 dialogue 里。行列表缺失 speaker 信息"
                 "时按无 speaker 处理，不猜。"},
    {"id": "S06-G02", "kind": "gap",
     "summary": "§2.4 说 S06 只接受已解析的 BindingSet，但没给 BindingSet 到 "
                "ShotSpec.asset_bindings 的映射形状",
     "handling": "BindingSet 只校验存在性与 hash 并记录；asset_bindings 由 "
                 "proposal 或 patch 内联给出。缺失即 400 INVALID_SCHEMA，"
                 "不编一份映射。"},
    {"id": "S06-G03", "kind": "gap",
     "summary": "§2.5 的单镜重写没定义新 ShotSpecRevision 的 state，也没定义"
                "它属于哪个 storyboard revision",
     "handling": "单镜重写生成一个新的 storyboard revision（head+1）作为整场快照，"
                 "被改镜头在其中拿到新 revision；该镜头 revision 立即 SEALED 并"
                 "发已登记的单镜事件，storyboard revision 保持 DRAFT 直到 :seal。"},
    {"id": "S06-G04", "kind": "conflict",
     "summary": "方案 §5.5 的 UNIQUE(tenant,branch,shot_id,content_hash) 与"
                "'新版本复制未改动镜头'直接矛盾：同一份 ShotSpec 第二次插入必然"
                "撞唯一约束，而 §2.5 的局部改镜又要求整场快照",
     "handling": "唯一约束收窄到 (tenant,branch,shot_id,storyboard_revision,"
                 "content_hash)；去重改由 storyboard_revision 级的 "
                 "content_fingerprint 承担（T06-04）。"},
    {"id": "S06-G05", "kind": "gap",
     "summary": "§5.5 / §3.4 要求 shot_order 唯一且 DEFERRABLE INITIALLY "
                "DEFERRED；SQLite 没有 DEFERRABLE",
     "handling": "重排用'先整体加 ORDER_OFFSET 再重写 1..N'；要求提交完整列表，"
                 "不接受 delta。"},
    {"id": "S06-G06", "kind": "needs_owner",
     "summary": "options.max_shots_per_scene 允许放宽到多少没有定义（§2.4 只给"
                "默认 80）",
     "handling": "接受 1..80，超过即 400 INVALID_SCHEMA；不放宽上限。"},
    {"id": "S06-G07", "kind": "conflict",
     "summary": "§4.1 的 content_hash 含整个 ShotSpec，而 ShotSpec 含 "
                "display_locator.shot；T06-47 又要求重排后 content_hash 不变",
     "handling": "算 hash 时对 ShotSpec 取内容投影，显式排除 display_locator"
                 "（它在 §1.1 就是显示值）；另存一个去掉身份与版本的 "
                 "content_fingerprint 供去重。算式登记在此。"},
    {"id": "S06-G08", "kind": "gap",
     "summary": "§3.2.3 说'退役后重建不复用旧 shot_id'，但没给错误码；"
                "§3.3.1 的 UNKNOWN_SHOT_ID 也不在 §2.6 的错误表里",
     "handling": "复用已退役 shot_id → 422 UNKNOWN_SHOT_ID；凭空指定不存在的 "
                 "shot_id → 同一错误码。"},
    {"id": "S06-G09", "kind": "gap",
     "summary": "Storyboard revision 的 CAS 冲突没有错误码（§6.2 只给单镜的 "
                "SHOT_REVISION_CONFLICT）",
     "handling": "新增 409 STORYBOARD_REVISION_CONFLICT，语义与单镜同构。"},
    {"id": "S06-G10", "kind": "gap",
     "summary": "分支分叉时'父分支'从哪来：§3.2.2 的签名有 parent_branch_id，"
                "但 §2.4 的请求体没有任何父分支字段",
     "handling": "options.parent_storyboard_id 指定分叉源；不提供即视为同分支"
                 "重规划。父分支由父 storyboard 的 branch_id 决定。"},
    {"id": "S06-G11", "kind": "gap",
     "summary": "读取时的 stale 判定需要回 S04，但查询路径没有定义 S04 不可用"
                "时返回什么",
     "handling": "S04 可用且能读到时给 stale=true/false；不可用给 stale=null 并"
                 "附 warning，不阻断读取（§9.5 T06-37 的'不阻断'）。"},
    {"id": "S06-G12", "kind": "conflict",
     "summary": "§4.1 的 ShotSpec 示例与 domain-v1/shot-spec.v1.schema.json 有"
                "四处不符：shot_type 用了枚举外的 medium_close_up、lighting 是"
                "对象、output_spec 用 resolution 字符串、continuity.state_in 是"
                "对象而不是字符串数组",
     "handling": "以 schema 为准（契约优先于方案里的示例），示例写法一律 400 "
                 "INVALID_SCHEMA。示例过期不等于契约错。"},
    {"id": "S06-G13", "kind": "gap",
     "summary": "GET /scenes/{id}/shots 在场存在但还没有镜头时返回空列表，与"
                "'该场不存在'无法区分；场景的权威在 S05，S06 只存 scene_id",
     "handling": "返回空列表 + notice，不 404、不查 S05。把'我这里没有'说成"
                 "'上游没有'会制造第二个权威。"},
    {"id": "S06-G14", "kind": "gap",
     "summary": "§3.1 校验⑥要求核对 asset_binding 的 revision 存在，但 S10 的 "
                "GET /assets/{id} 不暴露 revision 列表（资产版本归 owner 服务）",
     "handling": "能证明的是'资产存在且未被 tombstone'，不是'第 n 版存在'；"
                 "revision 作为引用原样写进 ShotSpec，由 S25/S26 派单前解析。"},
    {"id": "S06-G15", "kind": "gap",
     "summary": "inherit_by_beat_anchor 的锚点写成'首个 beat 覆盖的 line_id'，"
                "但 shot-spec.v1 的 beat 只有 time_range 与 action，没有行引用",
     "handling": "退到 dialogue[0].line_id——它同样是'这一镜从哪一行开始'，"
                 "且在边界移动时比时间段稳定。命中即沿用 lineage 与 shot_id。"},
    {"id": "S06-G16", "kind": "gap",
     "summary": "§4.2 的 aggregate_id 取 shot_id，但 shot_id 的作用域是 "
                "(tenant, branch)；跨分支同名会撞同一个事件流",
     "handling": "按契约发 aggregate_id=shot_id，branch_id 放进 data_ref；"
                 "缺口登记，不在实现里偷偷改事件键。"},
    {"id": "S06-G17", "kind": "gap",
     "summary": "§7.3 只说'丢弃 agent 给的 shot_lineage_id'，没说 agent 输出"
                "里混入 tenant_id / project_id / series_id 时怎么办",
     "handling": "422 AGENT_SUPPLIED_TENANT：那不是'多一个字段'，而是提示里"
                 "混进了租户上下文，意味着这份输出可能属于另一个租户。"},
    {"id": "S06-G18", "kind": "gap",
     "summary": "E03 的 patch 键与 changed_fields 的写法没有定义语法；JSON "
                "Pointer、点分路径、裸字段名三种都可能被调用方送来",
     "handling": "只接受点分标识符（camera.angle、beats[2].action），其余 400 "
                 "INVALID_SCHEMA。同一份'改了什么'只能有一种写法流到 S09。"},
    {"id": "S06-G19", "kind": "gap",
     "summary": "候选镜头自带一个已存在但锚点没命中的 shot_id 时，方案没说"
                "是'改铸新 ID'还是'拒绝'",
     "handling": "拒绝（422 UNKNOWN_SHOT_ID），唯一的例外是 fresh 模式按定义"
                 "全部重铸。静默改铸会让调用方以为改的是 A 镜，实际新建了 B。"},
    {"id": "S06-G20", "kind": "gap",
     "summary": "actor（produced_by）的来源未定：本仓还没有 TCT 验签中间件",
     "handling": "X-Principal-Id / X-Principal-Type 优先，缺失时如实记 "
                 "unspecified；两者冲突 403。过渡态，不是安全边界。"},
    {"id": "S06-G21", "kind": "gap",
     "summary": "event_sdk.new_event 的 data_ref 注解是 str|None，但本仓既有"
                "服务（S05 / S19 / S24）一律传 {service_id, id, revision, "
                "content_hash} 字典",
     "handling": "随既有服务传字典，不擅自改 SDK、不擅自改兄弟服务的调用；"
                 "缺口留在平台侧，S06 按实际通行形状写入。"},
]
