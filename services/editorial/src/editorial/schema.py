"""S41 自己的 schema。表来源是 `plans/S41-editorial.md` §5。

方案写的是 Postgres；这里逐条翻译到 SQLite：

- `jsonb` → `TEXT`（JCS 规范 JSON）
- `timestamptz` → `TEXT`（`_now()` 的定宽 UTC）
- `text[]` → `TEXT`（JSON 数组；轨道 id 可能含逗号，逗号分隔会丢边界）
- `char(64)` → `TEXT` + CHECK（本仓的 `^[0-9a-f]{64}$` 只在这一处 DDL 里落地）
- `bigint` / `integer` 保持 INTEGER（tick 不用浮点，§5 开头）
- `boolean` → INTEGER 0/1
- `uuid` → TEXT

四条结构性事实：

1. **`timeline` 是并发锚点，`timeline_revision` 完全不可变。** 回滚 = 提交一个
   新 revision，不是把 `current_revision` 拨回去（§2.1）。因此 revision 表没有
   UPDATE 路径（除了 §3.1 的两条出边：SUPERSEDED / VOIDED）。
2. **`edl_entry` 不是第二份真相。** 它是 revision 的确定性函数（§2.1），
   任何时候都能重算。不建 `edl_cache`（§5 末段）。
3. **没有 `clip_operation_log`。** 操作历史由 revision 链表达；建操作日志等于
   承认"当前状态 = 历史重放"，与 CAS 模型冲突（§5 末段）。
4. **没有 `accepted_pointer` 列，也没有任何写它的路径。** 接受归 S08（§1、§7.3）。
   `built_against.node_epoch` 只是"我基于哪一版剪的"，不是当前 epoch 的权威。

`outbox` / `inbox` 由平台 `event_sdk` 建表（不在这里），与业务事实同事务写。
方案 §5 的 `editorial.outbox` 比 SDK 多三列（`aggregate_sequence` /
`partition_key` / `headers`），登记在 CONTRACT_GAPS（E41-OUTBOX-DDL）。
"""

ID_PREFIX = {"timeline": "tl_"}

SCHEMA_VERSION = "2.0"
SOURCE = "S41"
TOPIC = "drama.editorial.v2"
EVENT_REVISION_CREATED = "post.timeline.revision.created"
CONSUMER = "s41.editorial"

# §2.2 规则 1/2：内部统一 90 kHz 整数 tick；取整规则参与 content_hash。
TICK_RATE = 90000
ROUNDING_RULE = "half_up_at_frame_boundary_v1"

# §5 的 CHECK 枚举。照抄，不扩。
TIMELINE_KINDS = ("master", "rough_cut", "trailer", "alt")
TIMELINE_STATES = ("ACTIVE", "FROZEN", "CLOSED")
TRACK_KINDS = ("video", "dialogue", "sfx", "bgm", "subtitle", "overlay")
REVISION_STATES = ("COMMITTED", "SUPERSEDED", "VOIDED")

# §4.4 的六种 gap。`missing_artifact` / `hash_mismatch` / `insufficient_handles`
# 在提交时是 422（§3.3），所以已提交 revision 的 gaps 里只会出现
# `missing_stem` / `missing_subtitle_track`（见 CONTRACT_GAPS E41-GAPS-422）。
GAP_KINDS = ("missing_artifact", "hash_mismatch", "missing_subtitle_track",
             "missing_stem", "insufficient_handles", "rights_expired")

# 需要导出 stem 的音轨种类。`video` / `subtitle` / `overlay` 不进 stem。
STEM_TRACK_KINDS = ("dialogue", "sfx", "bgm")

# §4.5：S28 读超时 500ms 就 503，不落库。
ARTIFACT_DEADLINE_SECONDS = 0.5

# §4.5 末段：diff 区间超过 500 个打 `diff_explosion`，降级为全量受影响。
DIFF_EXPLOSION_LIMIT = 500

# 事件 `data` 的键集合，与 `contracts/post.timeline-revision-created.v2.schema.json`
# 的 `required` 逐字相同（`additionalProperties: false`）。`gaps`、`tick_rate`、
# `change_causes` 都**不在**事件里（PTR-2 / PTR-5）。
EVENT_DATA_KEYS = ("schema_version", "timeline_id", "revision", "content_hash",
                   "duration_ticks", "completeness", "formal_master_eligible",
                   "built_against", "diff_ref", "affected_ranges",
                   "affected_tracks")


CONTRACT_GAPS = [
    # ---------- 契约里已登记、直接影响本服务产出的事件冲突 ----------
    {"id": "PTR-1", "kind": "conflict",
     "summary": "同一个区间集合有三个名字：§4.3/§4.2 的完整 diff 叫 "
                "affected_output_ranges，§5 的 DDL 列与 §6.3 的事件叫 "
                "affected_ranges；S42 的方案同一段里两个名都写",
     "handling": "库列与事件按契约发 affected_ranges；完整 diff payload 里两个"
                 "键都给（同值），让按任一名字读的消费者都不静默拿到 undefined。"},
    {"id": "PTR-2", "kind": "conflict",
     "summary": "事件发 duration_ticks / affected_ranges，但不发 tick_rate 与 "
                "fps_num/fps_den；tick_rate 是 DEFAULT 不是常量",
     "handling": "事件不放 tick_rate（契约 additionalProperties:false）；"
                 "本服务 tick_rate 恒为 90000 并写进 content_hash，"
                 "读事件的消费方必须回查 revision 才能换算成秒。"},
    {"id": "PTR-3", "kind": "conflict",
     "summary": "tr_hash_uniq 唯一索引约束 (tenant,timeline,content_hash)，"
                "但方案没写两个不同 command_id 产出相同内容时该当幂等还是拒绝",
     "handling": "提交前查一次同 hash 的已有 revision；命中即 409 "
                 "CONTENT_HASH_COLLISION 并带 existing_revision。不复用别人的"
                 " revision 号——那会让调用方拿到一个不是自己提交的序号。"},
    {"id": "PTR-4", "kind": "conflict",
     "summary": "TimelineRevision 有三态两条出边，但只发一条 created 事件；"
                "VOIDED 是「永远不能用于正式 master」，事件流不传播",
     "handling": "本服务只发 post.timeline.revision.created；"
                 "`render_eligibility` 显式拒绝 VOIDED/SUPERSEDED，"
                 "供 S42 在渲染前回查（PTR-4 的另一个选项是补事件）。"},
    {"id": "PTR-5", "kind": "conflict",
     "summary": "completeness 只有两值，造成 incomplete 的 gap 明细不进事件",
     "handling": "事件只发 completeness / formal_master_eligible；"
                 "要理由必须回查 GET /timelines/{id}/revisions/{revision}。"},
    {"id": "PTR-6", "kind": "conflict",
     "summary": "built_against 同时带 execution_fence 与 node_epoch，"
                "但 fence 变了是整条分支作废、epoch 变了只是节点重生成",
     "handling": "本服务只用 fence 判作废（§3.1/§3.2）；node_epoch 原样透传，"
                 "绝不据它推进或作废任何本地状态。"},
    {"id": "PTR-7", "kind": "conflict",
     "summary": "revision 1 没有前驱，而事件 schema 的 diff_ref 要求 "
                "from_revision>=1 且 to_revision>=2 —— 首次提交的事件在任何"
                "取值下都无法通过契约校验",
     "handling": "照实发 from_revision=0 / to_revision=1（0 就是「没有前驱」），"
                 "并在 timeline_diff 落 (0,1) 行。这是契约无法表达的情形，"
                 "不伪造一个 1→2 的差分。"},
    {"id": "E41-HASH", "kind": "conflict",
     "summary": "§6.1/§4.3/§6.3 的示例都写 content_hash=\"sha256:...\"，"
                "而 §5 的 DDL CHECK 与事件 schema 都是裸 64 位小写 hex",
     "handling": "库里与线上事件一律裸 hex；响应体同。接受调用方在 "
                 "src_artifact_ref 里带 sha256: 前缀，比对与落库前剥掉。"},
    {"id": "E41-REC-OUT", "kind": "conflict",
     "summary": "§6.1 的命令体只给 src_in/src_out/rec_in/speed，"
                "§5 的 clip 表却要求 rec_out_ticks NOT NULL",
     "handling": "rec_out_ticks = rec_in + floor((src_out-src_in)*speed_den/"
                 "speed_num)。整数 tick 只做一次截断，中间不取整到帧（§2.2）。"},
    {"id": "E41-TRACK-KIND", "kind": "conflict",
     "summary": "§6.1 的 tracks 元素是 {track_id, kind}，clip 行却要 "
                "track_kind；clip 自己不带 kind",
     "handling": "track_kind 由 tracks 表按 track_id 连接得到；"
                 "clip 引用未声明的 track_id 即 422 SCHEMA_INVALID。"},
    {"id": "E41-AV-CHECK", "kind": "conflict",
     "summary": "§5 的 clip_av_drift_needs_reason 字面是 "
                "`drift_reason IS NOT NULL OR link_group_id IS NULL OR "
                "unlinked_at_revision IS NOT NULL`，照抄会让**每一个**正常"
                "同步的 linked clip 都被迫写 drift_reason，与 §2.4「只有显式"
                "声明 drift 时才写」相反",
     "handling": "DDL 只保留弱形式（drift_reason 非空串）；"
                 "组内 rec_in/speed 一致性在服务层校验（§2.4）。"},
    {"id": "E41-GAPS-422", "kind": "conflict",
     "summary": "§3.3 把 missing_artifact / hash_mismatch / insufficient_handles "
                "定为 422 且不落库，§4.4 又把它们列为已提交 revision 的 gap —— "
                "两者不能同时成立",
     "handling": "按 §3.3 与 E37-S41-01/02/05：这三种一律 422，"
                 "明细放错误 details.gaps（带 clip_id）。已提交 revision 的 "
                 "gaps 只可能含 missing_stem / missing_subtitle_track。"},
    {"id": "E41-RIGHTS", "kind": "gap",
     "summary": "rights_expired gap 没有产出方：S41 的 depends_on 是 "
                "S08/S28/S19，没有 S15；本服务也不缓存「已授权」结论",
     "handling": "不产出该 kind；权利判定留给 S42 渲染前复查 S15/S02（§4.4）。"},
    {"id": "E41-STALE", "kind": "needs_owner",
     "summary": "§4.2 要求收到 manifest 事件后标 timeline_stale 并生成候选、"
                "发 post.timeline.stale；但该事件不在 event-topics.v2.json，"
                "且 built_against.shot_revisions 的形状是契约的 x-needs-owner",
     "handling": "不发未登记事件；不持久化 stale 标记（§5 DDL 没有该列）；"
                 "消费事件只推进 fence 投影。等 owner 定 shot_revisions 形状。"},
    {"id": "E41-FENCE-EVENT", "kind": "gap",
     "summary": "§3.2 说 execution_fence 由 S08 推进并经 "
                "snapshot.manifest.committed 投影到本地，但 S08 的 "
                "POST /branches/...:fence 不发任何事件；只有 manifest 提交通知"
                "带 execution_fence",
     "handling": "消费 snapshot.manifest.committed 时按 data.manifest_ref.id "
                 "匹配本地 revision（即 §5 的 tr_manifest_idx 的用途），"
                 "只单调前进、不回退。没有 manifest 提交的纯 fence 推进"
                 "到不了 S41——这是上游缺口。"},
    {"id": "E41-IDEM-TX", "kind": "conflict",
     "summary": "§7.1 要求幂等记录与 revision 同事务；平台 SDK 的 begin() 在"
                "事务外先落占位行，finish() 才写结果",
     "handling": "结果 UPDATE 与 command_receipt 都写在提交事务内；事务回滚时"
                 "api 层 abort() 删占位行，重投可以成功（E12-S41-04）。"},
    {"id": "E41-IDEM-CODE", "kind": "conflict",
     "summary": "§3.3 的码是 IDEMPOTENCY_MISMATCH，SDK 的 CommandConflict.code "
                "是 IDEMPOTENCY_KEY_REUSED，同仓其它服务直接透传后者",
     "handling": "HTTP 层返 409 与 IDEMPOTENCY_MISMATCH（方案点名的码），"
                 "SDK 原名放 details.sdk_code。"},
    {"id": "E41-TRACE-HASH", "kind": "conflict",
     "summary": "§7.1 要求 request_hash 不含 trace_id 与时间戳；"
                "canonical_request_hash 只剥 command_id / request_hash",
     "handling": "先剥 context.trace_id / traceparent / timestamp / request_hash "
                 "再交给 canonical_request_hash；同一个逻辑命令换个 trace 仍是"
                 "同一次幂等重放。"},
    {"id": "E41-OUTBOX-DDL", "kind": "conflict",
     "summary": "§5 的 outbox 有 aggregate_sequence / partition_key / headers，"
                "平台 event_sdk 的 OUTBOX_DDL 只有 event_id 主键与 payload",
     "handling": "用 SDK 表（与其它服务一致）。partition key "
                 "tenant_id:timeline_id 由 event 的 tenant/aggregate 推出，不存副本。"},
    {"id": "E41-CODES", "kind": "gap",
     "summary": "方案没有为这些情形给码：timeline 非 ACTIVE、同一身份已被"
                "另一个 timeline_id 占用、content_hash 撞唯一键",
     "handling": "分别用 409 TIMELINE_NOT_ACTIVE / TIMELINE_IDENTITY_CONFLICT / "
                 "CONTENT_HASH_COLLISION，并在此登记，等 owner 并入 §3.3。"},
    {"id": "E41-SEGMENTS", "kind": "gap",
     "summary": "§5 的 output_plan.segments 要 [{segment_no, rec_in, rec_out, "
                "est_seconds}]，但没有任何一处定义 segment 怎么切",
     "handling": "取所有片段记录区间的并集（已合并）按时间排序编号；"
                 "只作展示与 S42 的粗粒度定位，不参与 content_hash。"},
    {"id": "E41-C1", "kind": "needs_owner",
     "summary": "§11 C1：项目输出规格（分辨率/色彩/响度/编码）没有权威服务",
     "handling": "timeline.master_spec_ref 与 output_plan.master_spec_ref 只存"
                 "调用方给的引用，不给默认值——给默认值就是造第二权威。"},
    {"id": "E41-C2", "kind": "conflict",
     "summary": "§11 C2：S41 data_owner 含「混音计划」，S42 data_owner 含 MixJob；"
                 "计划与执行的切分是解释而非文档裁决",
     "handling": "按 §2.5 实现：MixPlan 只存计划（目标响度、峰值上限、"
                 "ducking 策略引用、stem 增益），不存任何实测值。"},
    {"id": "E41-AUTHZ", "kind": "gap",
     "summary": "§4.1 第 1 步与 §3.3 的 S02 403 无法实现：catalog 的 depends_on "
                "是 S08/S28/S19，没有 S02；权限注册表 40 个键里没有任何 "
                "editorial/timeline 动作",
     "handling": "不注入授权源、不自行解释权限；租户只来自 X-Tenant-Id，"
                 "请求体里的 tenant_id 只用于比对（403）。"},
    {"id": "E41-DURATION", "kind": "gap",
     "summary": "S28 的 artifact 摘要可能没有 duration_ms（如静帧、外部引用），"
                "此时 out 侧 handle 无法计算",
     "handling": "有 transition_out 且 duration 未知 → 422 INSUFFICIENT_HANDLES，"
                 "details.available_ticks=null、reason=artifact_duration_unknown；"
                 "不假设 0，也不假设计得够。"},
    {"id": "E41-MIN", "kind": "gap",
     "summary": "方案没写空片段列表与「短于一帧的片段」怎么拒；"
                "edl_entry 的 CHECK 会让后者在写库时炸成 500",
     "handling": "空 clips → 422 SCHEMA_INVALID；某片段按 fps 取整后不足一帧 → "
                 "422 CLIP_OUT_OF_RANGE（带 clip_id），不把 IntegrityError 漏成 500。"},
    {"id": "E41-C6", "kind": "conflict",
     "summary": "§11 C6：事件叫 created，语义是 committed",
     "handling": "响应与事件都不含「已接受」字样；201 只表示新 revision 已提交。"},
    {"id": "E41-FPS-CHANGE", "kind": "gap",
     "summary": "方案没写已有 timeline 的 fps 能不能在后续 revision 改；"
                "DDL 把 fps 放在 timeline 行上（不是 revision 上），"
                "说明它按时间线固定",
     "handling": "fps 与 tick_rate 一旦建立即固定；换帧率 → 409 "
                 "TIMELINE_FPS_CONFLICT / TIMELINE_TICK_RATE_CONFLICT。"
                 "真要换就是新 timeline，不是一次剪辑。",
     },
    {"id": "E41-DIFF-NAMES", "kind": "conflict",
     "summary": "§4.3 的 diff 示例字段用短名（src_in / rec_out / delta_ticks），"
                "§5 的 DDL 列用 _ticks 后缀；两套名字没有对照表",
     "handling": "落库用 DDL 列名；diff payload 用 §4.3 的短名（下游照示例"
                 "写代码）；`shifted[].delta_ticks` 保持方案原样。",
     },
    {"id": "E41-RETIRE", "kind": "gap",
     "summary": "§3.1 定义 COMMITTED→SUPERSEDED / VOIDED 两条出边，"
                "§6 的对外契约里没有任何触发它们的入口；SUPERSEDED 在提交时"
                "自动发生，VOIDED 却没有路径",
     "handling": "补 POST /timelines/{id}/revisions/{revision}:retire，"
                 "只接受 target=SUPERSEDED|VOIDED，终态不可回退。"
                 "退役不是剪辑命令：它不改内容，也不碰接受指针。",
     },
    {"id": "E41-SUPERSEDE", "kind": "gap",
     "summary": "「被新 revision 取代」的时机方案没写死：是提交新版本时自动"
                "把旧的标 SUPERSEDED，还是另有流程",
     "handling": "提交新 revision 的同一事务里把该 timeline 上所有 COMMITTED "
                 "标成 SUPERSEDED。于是任一时刻至多一版 COMMITTED，"
                 "`formal_master_eligible` 据此判断。",
     },
    {"id": "E41-PROJECTION", "kind": "gap",
     "summary": "§6.2 说查询「带 source_revision 与 projection_watermark"
                 "（若读投影）」，但本服务不持有投影表",
     "handling": "source_revision 取 built_against.manifest_ref.revision，"
                 "projection_watermark 取 timeline.execution_fence（本服务"
                 "唯一持久化的上游投影）。两者都是回查而非第二权威。",
     },
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 时间线：一集一分支一种 kind 一行，CAS 锚点 ----------
CREATE TABLE IF NOT EXISTS timeline (
  tenant_id          TEXT NOT NULL,
  timeline_id        TEXT NOT NULL,
  project_id         TEXT NOT NULL,
  series_id          TEXT NOT NULL,
  episode_id         TEXT NOT NULL,
  branch_id          TEXT NOT NULL,
  timeline_kind      TEXT NOT NULL DEFAULT 'master'
                     CHECK (timeline_kind IN ('master','rough_cut','trailer','alt')),
  current_revision   INTEGER NOT NULL CHECK (current_revision >= 1),
  lock_version       INTEGER NOT NULL DEFAULT 0,
  -- AGENTS.md：execution_fence 作用域是 run/branch。放在 timeline 上是刻意的
  -- ——Timeline 本身就是「一集一分支」的行。**本表没有 node_epoch 列**：
  -- node_epoch 属于单个 logical_node，不属于一条 run/branch 级的行。
  execution_fence    INTEGER NOT NULL CHECK (execution_fence >= 1),
  master_spec_ref    TEXT,
  fps_num            INTEGER NOT NULL CHECK (fps_num > 0),
  fps_den            INTEGER NOT NULL CHECK (fps_den > 0),
  tick_rate          INTEGER NOT NULL DEFAULT 90000 CHECK (tick_rate > 0),
  state              TEXT NOT NULL DEFAULT 'ACTIVE'
                     CHECK (state IN ('ACTIVE','FROZEN','CLOSED')),
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  PRIMARY KEY (tenant_id, timeline_id),
  CONSTRAINT timeline_identity_uniq
    UNIQUE (tenant_id, series_id, episode_id, branch_id, timeline_kind)
);
CREATE INDEX IF NOT EXISTS timeline_episode_idx
  ON timeline (tenant_id, episode_id, branch_id, timeline_kind);

-- ---------- 不可变版本 ----------
CREATE TABLE IF NOT EXISTS timeline_revision (
  tenant_id          TEXT NOT NULL,
  timeline_id        TEXT NOT NULL,
  revision           INTEGER NOT NULL CHECK (revision >= 1),
  content_hash       TEXT NOT NULL
                     CHECK (length(content_hash) = 64
                            AND content_hash NOT GLOB '*[^0-9a-f]*'),
  base_revision      INTEGER NOT NULL,
  state              TEXT NOT NULL
                     CHECK (state IN ('COMMITTED','SUPERSEDED','VOIDED')),
  -- 绑定：这个 revision 是基于哪一版生产状态剪出来的。
  -- 只放契约放行的三个键（manifest_ref / execution_fence / node_epoch）；
  -- shot_revisions 与 memo_checkpoint 没有形状，是契约的 x-needs-owner。
  built_against      TEXT NOT NULL,
  payload            TEXT NOT NULL,
  edl_hash           TEXT NOT NULL
                     CHECK (length(edl_hash) = 64
                            AND edl_hash NOT GLOB '*[^0-9a-f]*'),
  duration_ticks     INTEGER NOT NULL CHECK (duration_ticks >= 0),
  completeness       TEXT NOT NULL CHECK (completeness IN ('complete','incomplete')),
  gaps               TEXT NOT NULL DEFAULT '[]',
  rounding_rule      TEXT NOT NULL DEFAULT 'half_up_at_frame_boundary_v1',
  change_cause       TEXT,
  committed_by       TEXT NOT NULL,
  committed_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, timeline_id, revision),
  CONSTRAINT tr_base_lt CHECK (base_revision < revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS tr_hash_uniq
  ON timeline_revision (tenant_id, timeline_id, content_hash);
CREATE INDEX IF NOT EXISTS tr_state_idx
  ON timeline_revision (tenant_id, timeline_id, state, revision DESC);
-- 方案在 Postgres 里用 gin 索引 built_against -> 'manifest_ref'。SQLite 没有 gin，
-- 用表达式索引达到同一目的：按 manifest_ref.id 找「哪些 timeline 基于这份
-- manifest 剪过」，fence 投影就是靠它定位的。
CREATE INDEX IF NOT EXISTS tr_manifest_idx
  ON timeline_revision (tenant_id, json_extract(built_against, '$.manifest_ref.id'));

-- ---------- 片段 ----------
CREATE TABLE IF NOT EXISTS clip (
  tenant_id          TEXT NOT NULL,
  timeline_id        TEXT NOT NULL,
  revision           INTEGER NOT NULL,
  clip_id            TEXT NOT NULL,
  track_id           TEXT NOT NULL,
  track_kind         TEXT NOT NULL
                     CHECK (track_kind IN
                       ('video','dialogue','sfx','bgm','subtitle','overlay')),
  link_group_id      TEXT,
  ordinal            INTEGER NOT NULL,
  src_artifact_ref   TEXT NOT NULL,
  src_in_ticks       INTEGER NOT NULL CHECK (src_in_ticks >= 0),
  src_out_ticks      INTEGER NOT NULL,
  rec_in_ticks       INTEGER NOT NULL CHECK (rec_in_ticks >= 0),
  rec_out_ticks      INTEGER NOT NULL,
  speed_num          INTEGER NOT NULL DEFAULT 1 CHECK (speed_num > 0),
  speed_den          INTEGER NOT NULL DEFAULT 1 CHECK (speed_den > 0),
  transition_in      TEXT,
  transition_out     TEXT,
  unlinked_at_revision INTEGER,
  drift_reason       TEXT,
  PRIMARY KEY (tenant_id, timeline_id, revision, clip_id),
  FOREIGN KEY (tenant_id, timeline_id, revision)
    REFERENCES timeline_revision (tenant_id, timeline_id, revision),
  CONSTRAINT clip_src_order CHECK (src_out_ticks > src_in_ticks),
  CONSTRAINT clip_rec_order CHECK (rec_out_ticks > rec_in_ticks),
  -- 方案原本的谓词见 CONTRACT_GAPS E41-AV-CHECK：字面照抄会要求每个正常
  -- 同步的 linked clip 都写 drift_reason。这里只保留能独立成立的那半句，
  -- 组内一致性交给服务层（它能看到整个组）。
  CONSTRAINT clip_drift_reason_shape
    CHECK (drift_reason IS NULL OR length(drift_reason) > 0)
);
CREATE INDEX IF NOT EXISTS clip_track_idx
  ON clip (tenant_id, timeline_id, revision, track_id, ordinal);
CREATE INDEX IF NOT EXISTS clip_link_idx
  ON clip (tenant_id, timeline_id, revision, link_group_id)
  WHERE link_group_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS clip_track_pos_uniq
  ON clip (tenant_id, timeline_id, revision, track_id, rec_in_ticks);

-- ---------- EDL：revision 的确定性派生 ----------
CREATE TABLE IF NOT EXISTS edl_entry (
  tenant_id          TEXT NOT NULL,
  timeline_id        TEXT NOT NULL,
  revision           INTEGER NOT NULL,
  entry_no           INTEGER NOT NULL,
  clip_id            TEXT NOT NULL,
  track_id           TEXT NOT NULL,
  record_in_frame    INTEGER NOT NULL,
  record_out_frame   INTEGER NOT NULL,
  src_in_ticks       INTEGER NOT NULL,
  src_out_ticks      INTEGER NOT NULL,
  speed_num          INTEGER NOT NULL,
  speed_den          INTEGER NOT NULL,
  transition_in      TEXT,
  transition_out     TEXT,
  PRIMARY KEY (tenant_id, timeline_id, revision, track_id, entry_no),
  CONSTRAINT edl_frame_order CHECK (record_out_frame > record_in_frame)
);
CREATE INDEX IF NOT EXISTS edl_revision_idx
  ON edl_entry (tenant_id, timeline_id, revision);

-- ---------- 差分 ----------
CREATE TABLE IF NOT EXISTS timeline_diff (
  tenant_id          TEXT NOT NULL,
  timeline_id        TEXT NOT NULL,
  from_revision      INTEGER NOT NULL,
  to_revision        INTEGER NOT NULL,
  diff_hash          TEXT NOT NULL
                     CHECK (length(diff_hash) = 64
                            AND diff_hash NOT GLOB '*[^0-9a-f]*'),
  affected_ranges    TEXT NOT NULL,
  affected_tracks    TEXT NOT NULL,
  payload            TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  PRIMARY KEY (tenant_id, timeline_id, from_revision, to_revision),
  CONSTRAINT diff_forward CHECK (to_revision > from_revision)
);

-- ---------- 计划 ----------
CREATE TABLE IF NOT EXISTS output_plan (
  tenant_id          TEXT NOT NULL,
  timeline_id        TEXT NOT NULL,
  revision           INTEGER NOT NULL,
  plan_hash          TEXT NOT NULL,
  segments           TEXT NOT NULL,
  stems              TEXT NOT NULL,
  subtitle_track_ref TEXT,
  master_spec_ref    TEXT,
  PRIMARY KEY (tenant_id, timeline_id, revision)
);

CREATE TABLE IF NOT EXISTS mix_plan (
  tenant_id          TEXT NOT NULL,
  timeline_id        TEXT NOT NULL,
  revision           INTEGER NOT NULL,
  plan_hash          TEXT NOT NULL,
  target_lufs        REAL,
  true_peak_ceiling  REAL,
  ducking_policy_ref TEXT,
  stem_gains         TEXT NOT NULL,
  notes              TEXT,
  PRIMARY KEY (tenant_id, timeline_id, revision)
);

-- ---------- 命令回执：与 revision 同事务写 ----------
CREATE TABLE IF NOT EXISTS command_receipt (
  tenant_id          TEXT NOT NULL,
  command_id         TEXT NOT NULL,
  request_hash       TEXT NOT NULL CHECK (length(request_hash) = 64),
  timeline_id        TEXT NOT NULL,
  revision           INTEGER NOT NULL,
  response_body      TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  PRIMARY KEY (tenant_id, command_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS command_receipt_hash_uniq
  ON command_receipt (tenant_id, command_id, request_hash);
"""
