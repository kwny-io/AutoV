"""S42 混音与合成渲染的数据面（方案 `plans/S42-composition.md` §5）。

这个模块只有三样东西：**表**、**闭集**、**缺口**。没有 IO，没有业务分支。

SQL 方言适配（方案用 PostgreSQL）：

- `jsonb`       → TEXT（存 JCS 规范化后的 JSON），读用 `json_extract`
- `timestamptz` → 定宽 UTC 文本 `YYYY-MM-DDTHH:MM:SS.mmmZ`（字典序 == 时序）
- `numeric(p,s)`→ REAL，值域由 CHECK 卡住
- `text[]`      → TEXT，内容是 JSON 数组；`jsonb_array_length` 用
  `json_array_length()`
- `uuid`        → TEXT
- `boolean`     → INTEGER + `CHECK (col IN (0,1))`
- `CREATE SCHEMA` / `ALTER TABLE … ADD CONSTRAINT` 不支持 —— 表级约束写进
  `CREATE TABLE`

## 两列版本，两个作用域

`execution_fence` 属于 run / branch（取消、故障切换）；`node_epoch` 属于
单个 `logical_node`（局部重生成）。`render_job` / `mix_job` 各存两列，
`CHECK (>= 1)`，**不互相赋值、不比较、不换算、不 `max()` 合并**。

方案 §5 的 `master_candidate` 只有 `execution_fence`，没有 `node_epoch`。
候选是 run/branch 级的聚合产物，本来就没有「单个节点」可挂；本实现照方案
不添这一列，也**不**用 fence 去填 epoch。旧 epoch 的结果永远推不动 SC08 的
接受指针 —— 本服务连接受指针都不存（见 §1 的「明确不写」表）。

## 不建的表

- 不建 `render_cache`：缓存即 `render_segment` 里 `DONE` 的段，按
  `segment_key` 查；单独的表会与段记录不一致。
- 不建 `cost_ledger`：用量上报 S23，S42 只留自己的 `render_usage`，不是账本。
- 不建 `accepted_master`：那是 S08 的表。
- 不建 `command_receipt`：与 SDK 的 `idempotency_record` 是同一件事，
  再建一张就有两个权威。

## 与方案的字面偏差

全部登记在 `CONTRACT_GAPS`，顶部注释不重复论证，只列事实：

- `render_job` / `mix_job` 增加 `execution_fence` / `node_epoch` / `node_id`
  三列：`execution-context.v2` 强制它们，而 §5 的 DDL 没有（`S42-C1`）。
- `render_job` 增加 `command_id` / `request_hash` / `execution_context`，
  `master_candidate` 增加 `manifest` / `graph_hash` / `mix_plan_hash` /
  `subtitle_artifact_hash`（`S42-C2`）。
- `inbox` 在 SDK 三列之外补元数据列（与 S38 同一做法），在
  `IF NOT EXISTS` 下与 SDK 的 `INBOX_DDL` 兼容（`S42-C2`）。
- 所有 hash 列的长度检查都带 `IS NOT NULL`：SQLite 里 `length(NULL) = 64`
  求值为 NULL，会被当成「通过」。宽一格等于不检查。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 标识与版本

#: 本服务在所有事件里的 `service_id`。
SOURCE = "S42"

#: 落库对象的前缀。`candidate_id` 的 `mc_` 来自方案 §6.3 的示例。
ID_PREFIX = {
    "render_job": "rj_",
    "mix_job": "mj_",
    "candidate": "mc_",
    "measurement": "lm_",
    "usage": "us_",
}

SCHEMA_VERSION = "2.0"
TOPIC = "drama.composition.v2"

EVENT_MASTER_RENDERED = "post.master.rendered"
EVENT_MIX_COMPLETED = "post.mix.completed"
EVENT_RENDER_FAILED = "post.render.failed"

EVENT_TIMELINE = "post.timeline.revision.created"
EVENT_REPAIR_PLANNED = "quality.repair.planned"
EVENT_SNAPSHOT = "snapshot.manifest.committed"
EVENT_ARTIFACT_INGESTED = "artifact.ingested"

#: inbox 的消费者名。每个入口一个，互不吞事件。
CONSUMER_TIMELINE = "s42.timeline-revision"
CONSUMER_REPAIR = "s42.repair-planned"
CONSUMER_SNAPSHOT = "s42.snapshot-committed"
CONSUMER_ARTIFACT = "s42.artifact-ingested"

#: 滤镜图版本。新增 `kind` 必须同时升这个版本并改契约（方案 §2.2 规则 2）。
GRAPH_VERSION = "ffgraph-v1"

#: `request_hash` 不覆盖的上下文字段：重投时它们本来就会变。
VOLATILE_CONTEXT_KEYS = (
    "trace_id", "traceparent", "timestamp", "requested_at", "issued_at",
    "server_time",
)

# ---------------------------------------------------------------- 闭集

PURPOSES = ("formal", "preview", "draft")

#: `render_job.state`（方案 §3.1）。**没有 `SUBMISSION_UNKNOWN`**：
#: 渲染是本地执行，没有「供应商可能已经做了但没回执」这种状态。
JOB_STATES = (
    "QUEUED", "ADMITTED", "RUNNING", "CANCELLING", "CANCELLED",
    "SUCCEEDED", "FAILED", "REJECTED", "ORPHANED",
)
TERMINAL_JOB_STATES = ("CANCELLED", "SUCCEEDED", "FAILED", "REJECTED")
CANCELLABLE_JOB_STATES = ("QUEUED", "ADMITTED", "RUNNING", "CANCELLING")
RESUMABLE_JOB_STATES = ("QUEUED", "ADMITTED", "RUNNING", "ORPHANED")

SEGMENT_STATES = ("PENDING", "RUNNING", "DONE", "FAILED", "REUSED")
SEGMENT_TERMINAL_STATES = ("DONE", "REUSED")
SEGMENT_RETRYABLE_STATES = ("PENDING", "FAILED")

MIX_STATES = ("CREATED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")
TERMINAL_MIX_STATES = ("SUCCEEDED", "FAILED", "CANCELLED")
CANCELLABLE_MIX_STATES = ("CREATED", "RUNNING")

MIX_REASONS = ("initial_mix", "remix_defect", "spec_change", "manual")

CANDIDATE_STATES = (
    "CANDIDATE", "ACCEPTED_BY_SNAPSHOT", "SUPERSEDED", "QC_REJECTED",
    "VOIDED",
)
#: 允许被快照投影改写的状态。`VOIDED` 是终态：fence 推进过一次之后，
#: 旧候选不会再因为一条更旧的快照事件复活。
PROJECTABLE_CANDIDATE_STATES = ("CANDIDATE", "ACCEPTED_BY_SNAPSHOT",
                                "SUPERSEDED", "QC_REJECTED")

LOUDNESS_GATES = ("PASSED", "FAILED", "NOT_RUN")

ARTIFACT_ROLES = (
    "master_video", "master_audio", "stem_dialogue", "stem_sfx", "stem_bgm",
    "subtitle", "thumbnail", "qc_overlay",
)

RESOURCE_CLASSES = ("gpu_second", "cpu_media_second")

#: 方案 §5 的 `render_profile` 三组值。规格里出现别的值 = 422，不映射。
CONTAINERS = ("mp4", "mov", "mxf")
VIDEO_CODECS = ("h264", "hevc", "prores")
AUDIO_CODECS = ("aac", "pcm_s24le")

#: `FilterGraph` 的节点 kind 封闭集合（方案 §2.2 规则 2）。
#: 不存在 `denoise_auto` 这类「自己决定参数」的节点。
NODE_KINDS = (
    "input", "trim", "scale", "volume", "sidechaincompress", "amix",
    "concat", "loudnorm", "encode", "mux",
)

#: 失败码。只放本服务自己产生的；上游码原样透传。
FAILURE_CODES = (
    "INPUT_ARTIFACT_MISSING",       # §4.6：渲染前/段开始前的实时复查
    "SEGMENT_RENDER_FAILED",        # 编码器非零退出，重试耗尽
    "DURATION_MISMATCH",            # 拼接后时长/帧数与 EDL 不符
    "STEMS_UNAVAILABLE",            # §4.4：remix 不可行，交回 S39
    "LOUDNESS_MEASUREMENT_FAILED",  # §4.5：测不出 = 门失败
    "CANCELLED",                    # 取消；不是失败，但走同一条事件
    "WORKER_LOST",                  # 租约过期
)

#: 允许进入 `post.render.failed` 的终态（契约枚举）。
RENDER_FAILED_STATES = ("FAILED", "REJECTED", "ORPHANED", "CANCELLED")

# ---------------------------------------------------------------- DDL

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 渲染规格（引用 master_spec，不内联定义） ----------
CREATE TABLE IF NOT EXISTS render_profile (
  tenant_id            TEXT NOT NULL,
  profile_hash         TEXT NOT NULL,
  master_spec_ref      TEXT NOT NULL,
  container            TEXT NOT NULL,
  video_codec          TEXT NOT NULL,
  video_params         TEXT NOT NULL,
  audio_codec          TEXT NOT NULL,
  audio_params         TEXT NOT NULL,
  color_primaries      TEXT,
  transfer_function    TEXT,
  matrix_coefficients  TEXT,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, profile_hash),
  CONSTRAINT rp_hash_len CHECK (
    profile_hash IS NOT NULL AND length(profile_hash) = 64),
  CONSTRAINT rp_spec_known CHECK (
    json_valid(master_spec_ref)
    AND json_extract(master_spec_ref, '$.service_id') IS NOT NULL),
  CONSTRAINT rp_container CHECK (container IN ('mp4','mov','mxf')),
  CONSTRAINT rp_vcodec CHECK (video_codec IN ('h264','hevc','prores')),
  CONSTRAINT rp_acodec CHECK (audio_codec IN ('aac','pcm_s24le'))
);

-- ---------- 渲染作业 ----------
CREATE TABLE IF NOT EXISTS render_job (
  tenant_id             TEXT NOT NULL,
  render_job_id         TEXT NOT NULL,
  project_id            TEXT NOT NULL,
  series_id             TEXT NOT NULL,
  episode_id            TEXT NOT NULL,
  branch_id             TEXT NOT NULL,
  timeline_id           TEXT NOT NULL,
  timeline_revision     INTEGER NOT NULL CHECK (timeline_revision >= 1),
  timeline_content_hash TEXT NOT NULL,
  mix_plan_hash         TEXT NOT NULL,
  render_profile_hash   TEXT NOT NULL,
  render_env_hash       TEXT NOT NULL,
  -- S42-C2：证据包要能回答「在什么环境里编的」，只留 hash 答不了。
  render_env            TEXT NOT NULL,
  purpose               TEXT NOT NULL
    CHECK (purpose IN ('formal','preview','draft')),
  state                 TEXT NOT NULL CHECK (state IN (
    'QUEUED','ADMITTED','RUNNING','CANCELLING','CANCELLED',
    'SUCCEEDED','FAILED','REJECTED','ORPHANED')),
  permit_ref            TEXT,
  worker_id             TEXT,
  lease_expires_at      TEXT,
  estimated_gpu_ms      INTEGER,
  consumed_gpu_ms       INTEGER NOT NULL DEFAULT 0
    CHECK (consumed_gpu_ms >= 0),
  consumed_cpu_ms       INTEGER NOT NULL DEFAULT 0
    CHECK (consumed_cpu_ms >= 0),
  retry_count           INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  failure_code          TEXT,
  failure_detail        TEXT,
  stale_inputs          TEXT NOT NULL DEFAULT '[]',
  subtitle_ref          TEXT,
  subtitle_content_hash TEXT,
  master_spec_ref       TEXT NOT NULL,
  duration_ticks        INTEGER NOT NULL CHECK (duration_ticks >= 0),
  fps_num               INTEGER,
  fps_den               INTEGER,
  graph_hash            TEXT NOT NULL,
  -- 两个版本号，两列。永不合并（AGENTS.md「版本与一致性速查」）。
  execution_fence       INTEGER NOT NULL CHECK (execution_fence >= 1),
  node_epoch            INTEGER NOT NULL CHECK (node_epoch >= 1),
  node_id               TEXT NOT NULL,
  command_id            TEXT NOT NULL,
  request_hash          TEXT NOT NULL,
  execution_context     TEXT NOT NULL,
  output_candidate_id   TEXT,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  PRIMARY KEY (tenant_id, render_job_id),
  CONSTRAINT rj_state CHECK (state IN (
    'QUEUED','ADMITTED','RUNNING','CANCELLING','CANCELLED',
    'SUCCEEDED','FAILED','REJECTED','ORPHANED')),
  CONSTRAINT rj_hash_len CHECK (
    length(timeline_content_hash) = 64
    AND length(mix_plan_hash) = 64
    AND length(render_profile_hash) = 64
    AND length(render_env_hash) = 64
    AND length(graph_hash) = 64),
  CONSTRAINT rj_failure_terminal CHECK (
    failure_code IS NULL
    OR state IN ('FAILED','REJECTED','CANCELLED','ORPHANED')),
  CONSTRAINT rj_stale_is_array CHECK (json_valid(stale_inputs))
);
CREATE INDEX IF NOT EXISTS render_job_state_idx
  ON render_job (state, lease_expires_at)
  WHERE state IN ('ADMITTED','RUNNING','ORPHANED','CANCELLING');
CREATE INDEX IF NOT EXISTS render_job_episode_idx
  ON render_job (tenant_id, episode_id, branch_id, created_at DESC);

-- ---------- 渲染分段 ----------
CREATE TABLE IF NOT EXISTS render_segment (
  tenant_id           TEXT NOT NULL,
  render_job_id       TEXT NOT NULL,
  segment_no          INTEGER NOT NULL CHECK (segment_no >= 1),
  rec_in_ticks        INTEGER NOT NULL CHECK (rec_in_ticks >= 0),
  rec_out_ticks       INTEGER NOT NULL,
  segment_key         TEXT NOT NULL,
  input_refs          TEXT NOT NULL DEFAULT '[]',
  state               TEXT NOT NULL CHECK (state IN (
    'PENDING','RUNNING','DONE','FAILED','REUSED')),
  output_artifact_ref TEXT,
  attempts            INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  consumed_gpu_ms     INTEGER NOT NULL DEFAULT 0 CHECK (consumed_gpu_ms >= 0),
  started_at          TEXT,
  finished_at         TEXT,
  PRIMARY KEY (tenant_id, render_job_id, segment_no),
  FOREIGN KEY (tenant_id, render_job_id)
    REFERENCES render_job (tenant_id, render_job_id),
  CONSTRAINT seg_order CHECK (rec_out_ticks > rec_in_ticks),
  CONSTRAINT seg_key_len CHECK (length(segment_key) = 64),
  CONSTRAINT seg_inputs_is_array CHECK (json_valid(input_refs)),
  CONSTRAINT seg_done_has_ref CHECK (
    state NOT IN ('DONE','REUSED') OR output_artifact_ref IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS render_segment_cache_idx
  ON render_segment (tenant_id, segment_key)
  WHERE state IN ('DONE','REUSED') AND output_artifact_ref IS NOT NULL;

-- ---------- 滤镜图（规范化 JSON，可复算） ----------
CREATE TABLE IF NOT EXISTS filter_graph (
  tenant_id     TEXT NOT NULL,
  graph_hash    TEXT NOT NULL,
  graph_version TEXT NOT NULL,
  graph         TEXT NOT NULL,
  built_from    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, graph_hash),
  CONSTRAINT fg_hash_len CHECK (length(graph_hash) = 64),
  CONSTRAINT fg_graph_valid CHECK (json_valid(graph)),
  CONSTRAINT fg_built_valid CHECK (json_valid(built_from))
);

-- ---------- 混音作业 ----------
CREATE TABLE IF NOT EXISTS mix_job (
  tenant_id          TEXT NOT NULL,
  mix_job_id         TEXT NOT NULL,
  episode_id         TEXT NOT NULL,
  branch_id          TEXT NOT NULL,
  source_video_ref   TEXT NOT NULL,
  -- S42-C11：remix 请求不带时间线坐标，而候选与事件需要它们。这两列是从
  -- 「持有同一 source_video_ref 的既有候选」继承下来的，不是编的。
  timeline_id        TEXT NOT NULL,
  timeline_revision  INTEGER NOT NULL CHECK (timeline_revision >= 1),
  basis_candidate_id TEXT NOT NULL,
  stems              TEXT NOT NULL,
  mix_plan_hash      TEXT NOT NULL,
  graph_hash         TEXT NOT NULL,
  target_lufs        REAL NOT NULL,
  true_peak_ceiling  REAL NOT NULL,
  reason             TEXT NOT NULL CHECK (reason IN (
    'initial_mix','remix_defect','spec_change','manual')),
  repair_plan_ref    TEXT,
  state              TEXT NOT NULL CHECK (state IN (
    'CREATED','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  output_audio_ref   TEXT,
  consumed_cpu_ms    INTEGER NOT NULL DEFAULT 0
    CHECK (consumed_cpu_ms >= 0),
  failure_code       TEXT,
  failure_detail     TEXT,
  execution_fence    INTEGER NOT NULL CHECK (execution_fence >= 1),
  node_epoch         INTEGER NOT NULL CHECK (node_epoch >= 1),
  node_id            TEXT NOT NULL,
  command_id         TEXT NOT NULL,
  request_hash       TEXT NOT NULL,
  execution_context  TEXT NOT NULL,
  candidate_id       TEXT,
  created_at         TEXT NOT NULL,
  finished_at        TEXT,
  PRIMARY KEY (tenant_id, mix_job_id),
  CONSTRAINT mix_remix_needs_plan CHECK (
    reason <> 'remix_defect' OR repair_plan_ref IS NOT NULL),
  CONSTRAINT mix_hash_len CHECK (
    length(mix_plan_hash) = 64 AND length(graph_hash) = 64),
  CONSTRAINT mix_stems_is_array CHECK (json_valid(stems)),
  CONSTRAINT mix_video_is_s28 CHECK (
    json_extract(source_video_ref, '$.service_id') = 'S28'),
  CONSTRAINT mix_output_done CHECK (
    state <> 'SUCCEEDED' OR output_audio_ref IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS mix_job_episode_idx
  ON mix_job (tenant_id, episode_id, branch_id, created_at DESC);

-- ---------- 响度测量（实测值，不是判定） ----------
CREATE TABLE IF NOT EXISTS loudness_measurement (
  tenant_id           TEXT NOT NULL,
  measurement_id      TEXT NOT NULL,
  render_job_id       TEXT,
  mix_job_id          TEXT,
  artifact_ref        TEXT NOT NULL,
  integrated_lufs     REAL NOT NULL,
  true_peak_dbtp      REAL NOT NULL,
  lra_lu              REAL,
  measurement_tool    TEXT NOT NULL,
  measurement_version TEXT NOT NULL,
  conforms_to_spec    INTEGER NOT NULL CHECK (conforms_to_spec IN (0,1)),
  spec_ref            TEXT NOT NULL,
  measured_at         TEXT NOT NULL,
  PRIMARY KEY (tenant_id, measurement_id),
  CONSTRAINT loudness_one_parent CHECK (
    (render_job_id IS NULL) <> (mix_job_id IS NULL)),
  CONSTRAINT loudness_artifact_is_s28 CHECK (
    json_extract(artifact_ref, '$.service_id') = 'S28')
);

-- ---------- 成片候选 ----------
CREATE TABLE IF NOT EXISTS master_candidate (
  tenant_id             TEXT NOT NULL,
  candidate_id          TEXT NOT NULL,
  episode_id            TEXT NOT NULL,
  branch_id             TEXT NOT NULL,
  timeline_id           TEXT NOT NULL,
  timeline_revision     INTEGER NOT NULL CHECK (timeline_revision >= 1),
  manifest_content_hash TEXT NOT NULL,
  manifest              TEXT NOT NULL,
  render_job_id         TEXT,
  mix_job_id            TEXT,
  video_artifact_ref    TEXT NOT NULL,
  audio_artifact_ref    TEXT NOT NULL,
  duration_ticks        INTEGER NOT NULL CHECK (duration_ticks >= 0),
  frame_count           INTEGER NOT NULL CHECK (frame_count >= 0),
  loudness_measurement_id TEXT,
  loudness_gate         TEXT NOT NULL
    CHECK (loudness_gate IN ('PASSED','FAILED','NOT_RUN')),
  qc_report_refs        TEXT NOT NULL DEFAULT '[]',
  stale_inputs          TEXT NOT NULL DEFAULT '[]',
  deliverable           INTEGER NOT NULL CHECK (deliverable IN (0,1)),
  state                 TEXT NOT NULL CHECK (state IN (
    'CANDIDATE','ACCEPTED_BY_SNAPSHOT','SUPERSEDED','QC_REJECTED',
    'VOIDED')),
  execution_fence       INTEGER NOT NULL CHECK (execution_fence >= 1),
  render_env_hash       TEXT NOT NULL,
  graph_hash            TEXT NOT NULL,
  mix_plan_hash         TEXT NOT NULL,
  subtitle_artifact_hash TEXT,
  command_id            TEXT NOT NULL,
  created_at            TEXT NOT NULL,
  PRIMARY KEY (tenant_id, candidate_id),
  CONSTRAINT candidate_manifest_valid CHECK (json_valid(manifest)),
  CONSTRAINT candidate_hash_len CHECK (
    length(manifest_content_hash) = 64
    AND length(render_env_hash) = 64
    AND length(graph_hash) = 64
    AND length(mix_plan_hash) = 64),
  -- 方案 §5 行 555–556 的不变量，逐字照搬。
  CONSTRAINT candidate_deliverable_consistent CHECK (
    deliverable = 0
    OR (loudness_gate = 'PASSED' AND json_array_length(stale_inputs) = 0)),
  CONSTRAINT candidate_video_is_s28 CHECK (
    json_extract(video_artifact_ref, '$.service_id') = 'S28'),
  CONSTRAINT candidate_audio_is_s28 CHECK (
    json_extract(audio_artifact_ref, '$.service_id') = 'S28')
);
CREATE UNIQUE INDEX IF NOT EXISTS candidate_hash_uniq
  ON master_candidate (tenant_id, episode_id, branch_id,
                       manifest_content_hash);
CREATE INDEX IF NOT EXISTS candidate_episode_idx
  ON master_candidate (tenant_id, episode_id, branch_id, created_at DESC);

-- ---------- 候选包含的 artifact ----------
CREATE TABLE IF NOT EXISTS master_candidate_artifact (
  tenant_id    TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  role         TEXT NOT NULL CHECK (role IN (
    'master_video','master_audio','stem_dialogue','stem_sfx','stem_bgm',
    'subtitle','thumbnail','qc_overlay')),
  ordinal      INTEGER NOT NULL DEFAULT 0 CHECK (ordinal >= 0),
  artifact_ref TEXT NOT NULL,
  PRIMARY KEY (tenant_id, candidate_id, role, ordinal),
  FOREIGN KEY (tenant_id, candidate_id)
    REFERENCES master_candidate (tenant_id, candidate_id),
  CONSTRAINT mca_artifact_is_s28 CHECK (
    json_extract(artifact_ref, '$.service_id') = 'S28')
);

-- ---------- 本服务的资源用量（事实，不是账本） ----------
CREATE TABLE IF NOT EXISTS render_usage (
  tenant_id       TEXT NOT NULL,
  usage_id        TEXT NOT NULL,
  render_job_id   TEXT,
  mix_job_id      TEXT,
  resource_class  TEXT NOT NULL
    CHECK (resource_class IN ('gpu_second','cpu_media_second')),
  quantity_ms     INTEGER NOT NULL CHECK (quantity_ms >= 0),
  occurred_at     TEXT NOT NULL,
  reported_to_s23 INTEGER NOT NULL DEFAULT 0
    CHECK (reported_to_s23 IN (0,1)),
  PRIMARY KEY (tenant_id, usage_id),
  CONSTRAINT usage_one_parent CHECK (
    (render_job_id IS NULL) <> (mix_job_id IS NULL))
);
CREATE INDEX IF NOT EXISTS render_usage_unreported_idx
  ON render_usage (occurred_at) WHERE reported_to_s23 = 0;

-- ---------- 事件水位（按 aggregate 单调） ----------
CREATE TABLE IF NOT EXISTS event_watermark (
  consumer     TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  sequence     INTEGER NOT NULL CHECK (sequence >= 0),
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (consumer, aggregate_id)
);

-- ---------- 时间线信号（S41 diff 的受影响区间） ----------
CREATE TABLE IF NOT EXISTS timeline_signal (
  tenant_id       TEXT NOT NULL,
  timeline_id     TEXT NOT NULL,
  revision        INTEGER NOT NULL CHECK (revision >= 1),
  content_hash    TEXT NOT NULL,
  affected_ranges TEXT NOT NULL,
  received_at     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, timeline_id, revision),
  CONSTRAINT ts_ranges_is_array CHECK (json_valid(affected_ranges))
);

-- ---------- 修复信号（S39 的计划，只读镜像） ----------
CREATE TABLE IF NOT EXISTS repair_signal (
  tenant_id     TEXT NOT NULL,
  plan_id       TEXT NOT NULL,
  sequence      INTEGER NOT NULL CHECK (sequence >= 0),
  state         TEXT NOT NULL,
  top_action    TEXT NOT NULL,
  episode_id    TEXT,
  artifact_sha256 TEXT,
  repair_plan_ref TEXT,
  payload       TEXT NOT NULL,
  received_at   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, plan_id),
  CONSTRAINT rs_payload_valid CHECK (json_valid(payload))
);

-- ---------- inbox：SDK 三列 + 元数据列（与 S38 同一做法） ----------
CREATE TABLE IF NOT EXISTS inbox (
  consumer     TEXT NOT NULL,
  event_id     TEXT NOT NULL,
  tenant_id    TEXT,
  event_type   TEXT,
  aggregate_id TEXT,
  sequence     INTEGER,
  received_at  TEXT NOT NULL,
  PRIMARY KEY (consumer, event_id)
);
"""


# ---------------------------------------------------------------- 缺口

CONTRACT_GAPS = [
    {
        "id": "S42-C1",
        "kind": "conflict",
        "summary": "execution-context.v2 强制 execution_fence 与 node_epoch，"
                   "方案 §5 的 render_job / mix_job DDL 两列都没有。",
        "handling": "两张表各加两列（互不派生），CHECK (>= 1)；"
                    "master_candidate 照方案只存 execution_fence，"
                    "不补 node_epoch、不用 fence 填 epoch。",
    },
    {
        "id": "S42-C2",
        "kind": "gap",
        "summary": "方案 §5 没有 command_id / request_hash / execution_context"
                   " 列，也没有存候选 manifest 原文；而审计与幂等重放需要它们。",
        "handling": "render_job / mix_job 增三列；master_candidate 增 "
                    "manifest（原文）与 manifest_content_hash 可复算；"
                    "inbox 在 SDK 三列之外补元数据列。",
    },
    {
        "id": "S42-C3",
        "kind": "gap",
        "summary": "S41 的读 API（GET /timelines/{id}/revisions/{rev}）只返回"
                   " clips / completeness / payload，**不返回 output_plan 与 "
                   "mix_plan**，而方案 §4.1 第 3 步要求读它们。",
        "handling": "deps.build_timeline_source 在 revision 响应缺计划时读 "
                    "GET /timelines/{id}/revisions/{rev}/plans（提案路由）；"
                    "两条路径都拿不到就 502 TIMELINE_PLAN_UNAVAILABLE，"
                    "S42 绝不自行划分 segment 或发明响度目标。",
    },
    {
        "id": "S42-C4",
        "kind": "conflict",
        "summary": "方案 §11 C1：master_spec_ref 无权威服务，规格内容无处解析；"
                   "§4.5 又要求 conforms_to_spec 由实测值比较规格容差得出。",
        "handling": "新增 deps.build_master_spec_source（提案）；未配置 503 "
                    "MASTER_SPEC_UNAVAILABLE，规格不可解析时 422 "
                    "MASTER_SPEC_REQUIRED。内核不使用任何默认编码器或响度容差。",
    },
    {
        "id": "S42-C5",
        "kind": "conflict",
        "summary": "方案 §11 C2：service.yaml 只声明 POST /render-jobs；"
                   "remix 需要 POST /mix-jobs 入口。",
        "handling": "按方案 §6.1 实现 POST /mix-jobs 与查询/取消路由，"
                    "不改 service.yaml（生成物由 catalog 派生）。",
    },
    {
        "id": "S42-C6",
        "kind": "gap",
        "summary": "方案 §11 C3：段级进度如何进入 S47 实时投影没有定义；"
                   "渲染进度事件不存在。",
        "handling": "响应里只给 realtime_ref 占位（sse_topic 命名），"
                    "不额外发明 progress 事件；进度以 GET /render-jobs/{id} 轮询。",
    },
    {
        "id": "S42-C7",
        "kind": "gap",
        "summary": "方案 §11 C7：service.yaml 的 depends_on 不含 S41，"
                   "而实现必须只读 S41 的时间线。",
        "handling": "deps 只按内部契约路径读取 S41，不改 catalog；"
                    "依赖表由 owner 决定。",
    },
    {
        "id": "S42-C8",
        "kind": "gap",
        "summary": "snapshot.manifest.committed 的 accepted_pointers_delta "
                   "元素形状全文未定义（契约 SMC-3）。",
        "handling": "投影只匹配元素里出现的 S28 四键 ref，与候选的 "
                    "video/audio ref 逐字段相同才认；未匹配的元素原样计入 "
                    "unmatched，不猜形状、不推任何指针。",
    },
    {
        "id": "S42-C9",
        "kind": "gap",
        "summary": "POST /mix-jobs 的请求体没有编码规格（codec/container），"
                   "§5 的 mix_job 也没有 profile 列。",
        "handling": "滤镜图在缺 audio_codec 时不放 encode 节点（输出为"
                    "规范化后的音频流）；绝不默认 AAC。若 S41 的 mix_plan 提供 "
                    "audio_codec 则照用。",
    },
    {
        "id": "S42-C10",
        "kind": "gap",
        "summary": "mix_plan 只有 ducking_policy_ref，没有 threshold/ratio 等"
                   "数值；而 §2.2 禁止「自动决定参数」的节点。",
        "handling": "sidechaincompress 节点携带 policy_ref 而不编造数值；"
                    "执行器必须按策略解析（本内核不执行真实音频）。",
    },
    {
        "id": "S42-C11",
        "kind": "gap",
        "summary": "remix 要生成 MasterCandidate，但 /mix-jobs 请求不带 "
                   "timeline_id / timeline_revision / execution_fence，"
                   "而 master_candidate 与 post.master.rendered 需要它们。",
        "handling": "只从持有同一 source_video_ref 的既有候选继承时间线坐标；"
                    "找不到即 422 REMASTER_BASIS_MISSING，不编造 revision 或 fence。",
    },
    {
        "id": "S42-C12",
        "kind": "gap",
        "summary": "render_segment 的帧数自检需要 fps，而 S41 revision 响应"
                   "只在 payload 里带 fps，没有顶层字段。",
        "handling": "能从 payload 读到 fps 就做「帧数 == 时长×fps」自检；"
                    "读不到时只比对 duration_ticks（记入 failure_detail 的 "
                    "checks_run），不假设 25/30fps。",
    },
    {
        "id": "S42-C13",
        "kind": "gap",
        "summary": "§6.2 要求 /master-candidates/{id}/evidence 给出"
                   "「用的什么环境」，而 §5 只存 render_env_hash，环境本身"
                   "无处可查。",
        "handling": "render_job 增 render_env（JCS 原文）；证据包同时返回 "
                    "hash 与原文。候选自己也存 render_env_hash，两者必须相等。",
    },
    {
        "id": "S42-C14",
        "kind": "gap",
        "summary": "ORPHANED 对账要「查 S28 有无产物」，但 S28 契约没有"
                   "按 job 列对象的接口，也没有 render_job 关联字段。",
        "handling": "对账只复核本服务**已记录**的段 ref（逐段 HEAD）；"
                    "未记录的段一律视为「未完成」。不扫全租户 artifact 列表"
                    "去猜哪些属于本 job。",
    },
    {
        "id": "S42-C15",
        "kind": "gap",
        "summary": "「同段重试 3 次」「S28 上传重试 8 次」「GPU OOM 重试 2 次」"
                   "（§4.6）是执行器策略，本内核不执行编码，无法在此兑现。",
        "handling": "内核只提供 record_segment_uploaded / mark_segment_retry "
                    "两个回报口；attempts 到 3 即把 job 置 FAILED "
                    "（SEGMENT_RENDER_FAILED，§4.6 的上限），真正的退避与"
                    "S28 上传重试由 worker 负责。",
    },
]


def is_hash(value) -> bool:
    """裸 64 位小写 hex（契约里的事件摘要是这个形状，不带 `sha256:`）。"""
    return isinstance(value, str) and len(value) == 64 \
        and all(c in "0123456789abcdef" for c in value)
