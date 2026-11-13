"""S32 自己的 schema。表来源是 `plans/S32-lipsync.md` §5.2。

七条结构性事实决定了这张库能证明什么、不能证明什么：

1. **有效期是算出来的，不是存出来的。** 表上没有 `valid_until`、没有
   `expires_at`、没有 `refreshed_at`。一个 take 对
   `(video_hash, audio_hash, window)` 有效，这三项都在 `input_fingerprint`
   里，而指纹是不可变列。§4.2 的"不做时间戳过期"在这里是**写不出来**，
   不是"不推荐"。
2. **过期不是删除。** `lipsync_take` 只有 `stale_basis` + `stale_detected_at`
   两个标记列，没有 DELETE 路径、没有 `deleted_at`。被撤销的那一版仍然可查
   （E39）。
3. **`execution_fence` 与 `node_epoch_at_create` 是两列，永不合并。**
   `execution_fence` 属于 run/branch，`node_epoch` 属于单个 `logical_node`
   （AGENTS.md「版本与一致性速查」）。本服务只透传这两列，不比较、不换算，
   也**不**用它们推进任何接受指针 —— 接受决策不在 S32。
4. **没有接受指针表。** `lipsync_checkpoint` 是本服务自己的"当前基线"，键是
   `(tenant, shot_lineage_id, speaker_character_id)`，与 S08 的 accepted
   pointer / `node_epoch` 无关。它只回答"这一镜这一人的最新 take 是哪个"，
   不回答"这一 run 接受到了哪"。
5. **`preserve_other_faces` 由 CHECK 钉死。** `lj_preserve_faces` 让 `false`
   写不进去（E01）。它不是"默认开、可以关"的开关。
6. **模糊的缺陷没有资格。** `ies_defects_have_ids`：`defects_open` 必须带至少
   一个缺陷 id。想说"音频有问题"而不给 id，写不进去。
7. **媒体引用只能指向 S28。** `ftb_ref_known` 与 `lj_video_is_s28` /
   `lj_audio_is_s28` / `lt_artifact_is_s28` 一起，让供应商直链和本地路径在
   入库这一步就失败（E01）。

SQL 方言适配（方案用 PostgreSQL）：

- `jsonb`      → TEXT（存 JCS 规范化后的 JSON），读用 `json_extract(x, '$.k')`
- `timestamptz`→ 定宽 UTC 文本 `YYYY-MM-DDTHH:MM:SS.mmmZ`（字典序 == 时序）
- `numeric(p,s)`→ REAL，值域由 CHECK 卡住
- `text[]`     → TEXT，内容是 JSON 数组；`cardinality()` 用
  `json_array_length()`
- `uuid`       → TEXT
- `boolean`    → INTEGER + `CHECK (col IN (0,1))`；`= true` 在 SQLite 里就是
  `= 1`
- `CREATE SCHEMA` / `ALTER TABLE … ADD CONSTRAINT` 不支持 —— 表级约束写进
  `CREATE TABLE`（`lj_repair_uniq` 因此内联；SQLite 把 NULL 视为互不相同，
  正好就是"每个 repair_action_id 只许一次、为空的不算"这个语义）

`outbox` / `inbox` / `idempotency_record` 由平台 SDK 建表
（`event_sdk.OUTBOX_DDL` / `INBOX_DDL`、`idempotency_sdk` 自己那份），这里
不重复 DDL。§5.2 的 `command_receipt` 与 `idempotency_record` 是同一件事的
两种写法 —— 再建一张就会有两个权威，故不建。

与 §5.2 的偏差（全部登记进 `CONTRACT_GAPS`，不自行选边）：

- `lipsync_job` 多一列 `execution_context`：平台签名上下文的原文。组
  `generation-command` 时必须**原样重放**（§7.1 的 request_hash 也基于它），
  §5.2 漏了。
- `lipsync_intent_ledger` 多一列 `repeat_without_input_change`：§8.3 要求记
  这个布尔并据此发事件，但 §5.2 的台账 DDL 没有这一列。
- 所有 hash 列的长度检查都带 `IS NOT NULL`：SQLite 里 `length(NULL) = 64`
  求值为 NULL，会被当成"通过"。宽一格等于不检查。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 人脸轨快照（不可变） ----------
CREATE TABLE IF NOT EXISTS face_track_binding (
  face_track_binding_id text PRIMARY KEY,
  tenant_id             text NOT NULL,
  face_track_ref        text NOT NULL,      -- JCS: {service_id,id,revision,content_hash}
  track_id              text NOT NULL,      -- 同一对象里的第几条轨
  frame_range_start     integer NOT NULL CHECK (frame_range_start >= 0),
  frame_range_end       integer NOT NULL,
  visible_frames        integer NOT NULL CHECK (visible_frames >= 0),
  covered_frames        integer NOT NULL CHECK (covered_frames >= 0),
  min_face_px           integer NOT NULL CHECK (min_face_px >= 0),
  detector_model        text NOT NULL,
  detector_version      text NOT NULL,
  captured_at           text NOT NULL,
  CONSTRAINT ftb_range_order CHECK (frame_range_end > frame_range_start),
  -- 可见帧 + 被遮挡帧不得超过区间长度：统计自相矛盾的数据进不来
  CONSTRAINT ftb_frames_fit CHECK (
    visible_frames + covered_frames <= frame_range_end - frame_range_start + 1),
  CONSTRAINT ftb_ref_known CHECK (
    json_extract(face_track_ref, '$.service_id') IN ('S28')),
  CONSTRAINT ftb_hash_len CHECK (
    json_extract(face_track_ref, '$.content_hash') IS NOT NULL
    AND length(json_extract(face_track_ref, '$.content_hash')) = 64),
  CONSTRAINT ftb_track_id_nonempty CHECK (length(track_id) > 0)
);

-- ---------- 输入合格性快照（不可变） ----------
CREATE TABLE IF NOT EXISTS input_eligibility_snapshot (
  snapshot_id        text PRIMARY KEY,
  tenant_id          text NOT NULL,
  video_ref          text NOT NULL,
  audio_ref          text NOT NULL,
  audio_take_ref     text,                  -- 指向 S31 的 voice_take（可空）
  voice_defect_check text NOT NULL
    CHECK (voice_defect_check IN ('clean','defects_open','not_yet_available')),
  s37_report_ref     text,
  open_defect_ids    text NOT NULL DEFAULT '[]',
  audio_rights_state text NOT NULL
    CHECK (audio_rights_state IN ('usable','void','unknown')),
  video_referenceable integer NOT NULL CHECK (video_referenceable IN (0,1)),
  face_authorization_state text NOT NULL
    CHECK (face_authorization_state IN ('granted','lapsed','unknown')),
  captured_at        text NOT NULL,
  -- defects_open 必须带具体缺陷 id：不允许"我觉得有问题"而无证据
  CONSTRAINT ies_defects_have_ids CHECK (
    voice_defect_check <> 'defects_open'
    OR json_array_length(open_defect_ids) > 0),
  CONSTRAINT ies_ids_is_array CHECK (json_valid(open_defect_ids))
);

-- ---------- 口型意图 ----------
CREATE TABLE IF NOT EXISTS lipsync_job (
  lipsync_job_id     text PRIMARY KEY,
  tenant_id          text NOT NULL,
  project_id         text NOT NULL,
  series_id          text NOT NULL,
  episode_id         text NOT NULL,
  branch_id          text NOT NULL,

  shot_lineage_id      text NOT NULL,
  speaker_character_id text NOT NULL,

  -- ↓ 输入对：不可变（§4.2）
  video_ref          text NOT NULL,
  audio_ref          text NOT NULL,
  audio_take_id      text,                  -- S31 的 voice_take_id（若来源是 S31）

  -- ↓ 音频窗口：不可变（§2.3）
  window_start_ms    integer NOT NULL CHECK (window_start_ms >= 0),
  window_end_ms      integer NOT NULL,
  trim_head_ms       integer NOT NULL DEFAULT 0 CHECK (trim_head_ms >= 0),
  trim_tail_ms       integer NOT NULL DEFAULT 0 CHECK (trim_tail_ms >= 0),
  window_source      text NOT NULL
    CHECK (window_source IN ('explicit','s37_alignment','editorial')),

  -- ↓ 帧范围：不可变（§2.4）
  process_frame_start integer NOT NULL CHECK (process_frame_start >= 0),
  process_frame_end   integer NOT NULL,
  processed_frames    integer NOT NULL CHECK (processed_frames > 0),

  face_track_binding_id   text NOT NULL REFERENCES face_track_binding(face_track_binding_id),
  eligibility_snapshot_id text NOT NULL REFERENCES input_eligibility_snapshot(snapshot_id),

  preserve_other_faces integer NOT NULL DEFAULT 1
    CHECK (preserve_other_faces IN (0,1)),
  fps                  real NOT NULL CHECK (fps > 0),

  -- §4.7 的可见度分级。建 job 时算定，**存下来**而不是等归档时再猜：
  -- `artifact.ingested` 里没有人脸事实（C4），归档那一刻算不出它。
  -- 拒绝档（absent/too_small/sparse）也照存——被拒的 job 仍可查询，而
  -- "为什么被拒"应当和 job 一起躺着，而不是只出现在那一次的响应里。
  visibility_grade     text NOT NULL
    CHECK (visibility_grade IN ('absent','too_small','sparse','partial','full')),

  -- 两个版本号是两个作用域，分成两列。不合并，不互相赋值。
  node_id              text NOT NULL,
  node_epoch_at_create integer NOT NULL CHECK (node_epoch_at_create >= 1),
  execution_fence      integer NOT NULL CHECK (execution_fence >= 1),

  input_fingerprint  text NOT NULL,
  request_hash       text NOT NULL,
  idempotency_key    text NOT NULL,

  repair_action_id   text,
  strategy_class     text,
  variation_nonce    text NOT NULL,

  route_decision_ref text,
  execution_context  text NOT NULL,          -- 平台签名上下文原文，组命令时原样重放
  budget_scope_id    text NOT NULL,
  deadline           text NOT NULL,

  provider_job_id    text,
  attempt_id         text,
  business_attempt_no integer NOT NULL DEFAULT 1
    CHECK (business_attempt_no >= 1),
  quote_upper_bound_micro integer,

  state              text NOT NULL,
  state_seq          integer NOT NULL DEFAULT 0 CHECK (state_seq >= 0),
  failure_code       text,
  failure_detail     text,
  reconciled         integer NOT NULL DEFAULT 0 CHECK (reconciled IN (0,1)),

  created_at         text NOT NULL,
  updated_at         text NOT NULL,
  state_entered_at   text NOT NULL,

  CONSTRAINT lj_state_check CHECK (state IN (
    'CREATED','CHECKING_INPUTS','INPUTS_INELIGIBLE','PREPARING','REJECTED_LOCAL',
    'SUBMITTED','WAITING_PROVIDER','WAITING_UNKNOWN','INGESTING','READY',
    'FAILED','RECONCILED_READY','RECONCILED_FAILED','CANCELED')),
  CONSTRAINT lj_tenant_idem UNIQUE (tenant_id, idempotency_key),
  -- 一个 RepairAction 只许被应用一次；NULL 在 UNIQUE 里互不相同，
  -- 因此没有 repair_action_id 的请求不受影响。
  CONSTRAINT lj_repair_uniq UNIQUE (tenant_id, repair_action_id),
  CONSTRAINT lj_window_order CHECK (window_end_ms > window_start_ms),
  CONSTRAINT lj_frame_order CHECK (process_frame_end > process_frame_start),
  CONSTRAINT lj_fingerprint_len CHECK (length(input_fingerprint) = 64),
  -- 契约 const：不允许关闭
  CONSTRAINT lj_preserve_faces CHECK (preserve_other_faces = 1),
  CONSTRAINT lj_video_is_s28 CHECK (
    json_extract(video_ref, '$.service_id') = 'S28'),
  CONSTRAINT lj_audio_is_s28 CHECK (
    json_extract(audio_ref, '$.service_id') = 'S28')
);

CREATE UNIQUE INDEX IF NOT EXISTS lipsync_job_open_uniq
  ON lipsync_job
     (tenant_id, shot_lineage_id, speaker_character_id, input_fingerprint)
  WHERE state NOT IN ('READY','INPUTS_INELIGIBLE','REJECTED_LOCAL',
                      'FAILED','RECONCILED_FAILED','CANCELED');

CREATE INDEX IF NOT EXISTS lipsync_job_shot_idx
  ON lipsync_job (tenant_id, shot_lineage_id, created_at DESC);
CREATE INDEX IF NOT EXISTS lipsync_job_state_idx
  ON lipsync_job (state, state_entered_at)
  WHERE state IN ('CHECKING_INPUTS','PREPARING','SUBMITTED',
                  'WAITING_UNKNOWN','INGESTING');

-- ---------- 口型候选 ----------
CREATE TABLE IF NOT EXISTS lipsync_take (
  lipsync_take_id     text PRIMARY KEY,
  tenant_id           text NOT NULL,
  lipsync_job_id      text NOT NULL REFERENCES lipsync_job(lipsync_job_id),
  shot_lineage_id     text NOT NULL,
  speaker_character_id text NOT NULL,

  artifact_ref        text NOT NULL,        -- S28，content_class='generated_video'
  video_hash          text NOT NULL,        -- 输入视频 hash（有效期之一）
  audio_hash          text NOT NULL,        -- 输入音频 hash（有效期之一）
  input_fingerprint   text NOT NULL,        -- 含 window 与帧范围（有效期之三）

  output_frame_start  integer NOT NULL,
  output_frame_end    integer NOT NULL,
  output_frame_count  integer NOT NULL CHECK (output_frame_count > 0),
  output_fps          real NOT NULL CHECK (output_fps > 0),

  visibility_grade    text NOT NULL
    CHECK (visibility_grade IN ('full','partial')),
  other_region_max_diff    real NOT NULL CHECK (other_region_max_diff >= 0),
  other_face_drift         text NOT NULL
    CHECK (other_face_drift IN ('none','minor')),

  voice_defect_check_at_build text NOT NULL
    CHECK (voice_defect_check_at_build IN ('clean','not_yet_available')),
  s37_report_ref      text,

  provider_job_id     text NOT NULL,
  attempt_id          text NOT NULL,
  repair_action_id    text,
  strategy_class      text,

  stale_basis         text
    CHECK (stale_basis IS NULL OR stale_basis IN
      ('video_changed','audio_changed','window_changed',
       'audio_rights_void','video_tombstoned','face_track_changed')),
  stale_detected_at   text,

  state               text NOT NULL CHECK (state IN ('READY','ACCEPTED','SUPERSEDED')),
  created_at          text NOT NULL,

  CONSTRAINT lt_artifact_is_s28 CHECK (
    json_extract(artifact_ref, '$.service_id') = 'S28'),
  CONSTRAINT lt_artifact_hash_len CHECK (
    json_extract(artifact_ref, '$.content_hash') IS NOT NULL
    AND length(json_extract(artifact_ref, '$.content_hash')) = 64),
  CONSTRAINT lt_video_hash_len CHECK (length(video_hash) = 64),
  CONSTRAINT lt_audio_hash_len CHECK (length(audio_hash) = 64),
  CONSTRAINT lt_fingerprint_len CHECK (length(input_fingerprint) = 64),
  CONSTRAINT lt_frames_fit CHECK (output_frame_end >= output_frame_start),
  -- 过期标记与时间必须成对：有时间没依据、有依据没时间都解释不清
  CONSTRAINT lt_stale_has_time CHECK (
    (stale_basis IS NULL AND stale_detected_at IS NULL)
    OR (stale_basis IS NOT NULL AND stale_detected_at IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS lt_artifact_uniq
  ON lipsync_take
     (tenant_id, json_extract(artifact_ref, '$.id'),
      json_extract(artifact_ref, '$.content_hash'));
CREATE INDEX IF NOT EXISTS lt_shot_idx
  ON lipsync_take
     (tenant_id, shot_lineage_id, speaker_character_id, created_at DESC);
CREATE INDEX IF NOT EXISTS lt_live_idx
  ON lipsync_take (tenant_id, input_fingerprint)
  WHERE stale_basis IS NULL AND state IN ('READY','ACCEPTED');

-- ---------- 输出差异度量（不可变） ----------
CREATE TABLE IF NOT EXISTS output_diff_measurement (
  measurement_id     text PRIMARY KEY,
  tenant_id          text NOT NULL,
  lipsync_take_id    text NOT NULL REFERENCES lipsync_take(lipsync_take_id),
  eps_pixel          real NOT NULL,
  warn_pixel         real NOT NULL,
  mask_dilate_px     integer NOT NULL CHECK (mask_dilate_px >= 0),
  max_diff_complement        real NOT NULL,
  max_diff_target_region     real NOT NULL,
  changed_frame_ratio_complement real NOT NULL
    CHECK (changed_frame_ratio_complement >= 0
           AND changed_frame_ratio_complement <= 1),
  frames_compared    integer NOT NULL CHECK (frames_compared > 0),
  measured_at        text NOT NULL,
  CONSTRAINT odm_threshold_order CHECK (warn_pixel >= eps_pixel)
);

-- ---------- 口型检查点 ----------
-- 本服务的"当前基线"，不是平台 accepted pointer。没有 node_epoch 列，
-- 也不写 S08 的任何表。
CREATE TABLE IF NOT EXISTS lipsync_checkpoint (
  tenant_id            text NOT NULL,
  shot_lineage_id      text NOT NULL,
  speaker_character_id text NOT NULL,
  lipsync_take_id      text NOT NULL REFERENCES lipsync_take(lipsync_take_id),
  checkpoint_seq       integer NOT NULL CHECK (checkpoint_seq >= 1),
  expected_revision    integer NOT NULL DEFAULT 0,
  updated_by_job       text NOT NULL,
  updated_at           text NOT NULL,
  PRIMARY KEY (tenant_id, shot_lineage_id, speaker_character_id)
);

-- ---------- 生产意图台账 ----------
CREATE TABLE IF NOT EXISTS lipsync_intent_ledger (
  entry_id             text PRIMARY KEY,
  tenant_id            text NOT NULL,
  lipsync_job_id       text NOT NULL,
  shot_lineage_id      text NOT NULL,
  speaker_character_id text NOT NULL,
  action               text NOT NULL
    CHECK (action IN ('generate','redo_lipsync')),
  strategy_class       text,
  repair_action_id     text,
  audio_take_id        text,
  repeat_without_input_change integer NOT NULL DEFAULT 0
    CHECK (repeat_without_input_change IN (0,1)),
  -- §5.2 要求 NOT NULL，但 execution-context.v2 里没有 run_id 这个字段
  -- （缺口 G16）。写死 NOT NULL 会让每一次成功提交都在台账这一行失败，
  -- 而伪造一个 run_id 等于把 trace 冒充成 run。故可空，与 S31 同处理。
  run_id               text,
  node_epoch           integer NOT NULL,
  manifest_id          text NOT NULL,
  created_at           text NOT NULL
);
CREATE INDEX IF NOT EXISTS lil_shot_idx
  ON lipsync_intent_ledger (tenant_id, shot_lineage_id, created_at DESC);

-- ---------- 状态转移历史 ----------
CREATE TABLE IF NOT EXISTS job_transition (
  lipsync_job_id text NOT NULL,
  seq            integer NOT NULL,
  from_state     text,
  to_state       text NOT NULL,
  reason_code    text,
  detail         text,
  at             text NOT NULL,
  PRIMARY KEY (lipsync_job_id, seq)
);
"""

# 14 个状态，顺序与 §3.1 一致。CREATED 之后必须先过 CHECKING_INPUTS ——
# 它检查的不是权限，是"这对输入该不该做口型"（§3.2）。
JOB_STATES = (
    "CREATED", "CHECKING_INPUTS", "INPUTS_INELIGIBLE", "PREPARING",
    "REJECTED_LOCAL", "SUBMITTED", "WAITING_PROVIDER", "WAITING_UNKNOWN",
    "INGESTING", "READY", "FAILED", "RECONCILED_READY", "RECONCILED_FAILED",
    "CANCELED",
)

# INPUTS_INELIGIBLE 是终态且**没有自动出口**：它只能靠一次带合格新音频的
# 新请求离开（§3.2）。把它和 REJECTED_LOCAL 合并会让"音频念错词"变成一个
# 参数错误，归因给 S32。
#
# `RECONCILED_READY` 在这里而 §3.1 的终态清单没有列它 —— 是清单漏了。§3.1 的
# 转移表与状态图都把它画成汇点（`→ RECONCILED_READY`，无出边），而且它的含义
# 就是"S26 确认收单后走完了 `INGESTING→READY` 那一整套"，产物与 `READY` 同形。
# 不把它算终态的后果是实测出来的三条，不是猜的：取消它会在 `_transition` 里
# 撞断言（500 而不是 409 `JOB_NOT_CANCELABLE`）、迟到的 `RUNNING` 事件返回一个
# job 并不处于的状态、`recover` 把一笔健康的 job 塞进 `unhandled` 让人去查。
TERMINAL_STATES = frozenset({
    "READY", "INPUTS_INELIGIBLE", "REJECTED_LOCAL", "FAILED",
    "RECONCILED_FAILED", "RECONCILED_READY", "CANCELED",
})

# 只有这两个可以 retry（§6.3）。WAITING_UNKNOWN 必须先对账。
RETRYABLE_STATES = frozenset({"FAILED", "RECONCILED_FAILED"})

# 重启时必须重新做事的四个状态（§7.3）。INPUTS_INELIGIBLE / WAITING_UNKNOWN
# 重启后什么都不做。
RECHECK_STATES = frozenset({
    "CHECKING_INPUTS", "PREPARING", "SUBMITTED", "WAITING_PROVIDER",
    "INGESTING",
})

# §2.4：前后各留 2 帧。不留余量会在窗口边界产生突跳，被 S36 报成 temporal。
HANDLE_FRAMES = 2
# §4.5：默认 900 帧（约 30 秒 @30fps）。超限拒绝，**不自动拆**。
MAX_FRAMES_PER_JOB = 900
# §4.6：逐像素阈值（/255）。
EPS_PIXEL = 2.0
WARN_PIXEL = 8.0
# §4.6：掩膜膨胀半径。模型改嘴必然会碰到嘴边一圈像素。
MASK_DILATE_PX = 32
# §4.7：人脸太小直接拒绝。
MIN_FACE_PX = 48
# §4.7：可见帧比例分档。
VISIBILITY_FULL = 0.9
VISIBILITY_SPARSE = 0.5
VISIBILITY_ABSENT = 0.1

# §4.3：S37 的 AUD.TEXT.* 里这两级才算"音频真的错了"。
BLOCKING_SEVERITIES = frozenset({"major", "blocker"})
# §4.3：SCRIPT_CONFLICT 明确"不产生缺陷"，不得据此拒绝。
NON_BLOCKING_TEXT_CODES = frozenset({"AUD.TEXT.SCRIPT_CONFLICT"})

# §4.9：视频来源。S32 只有带 repair_action_id 时才允许接自己的产物。
ALLOWED_VIDEO_ORIGINS = frozenset({"S30", "S32"})
SELF_ORIGIN = "S32"

WINDOW_SOURCES = ("explicit", "s37_alignment", "editorial")

STALE_BASES = (
    "video_changed", "audio_changed", "window_changed",
    "audio_rights_void", "video_tombstoned", "face_track_changed",
)

# 本服务只认这一个动作类。收到 voice 族的 re_synthesize_speech 是越权，
# 收到 voice 类的 redo_lipsync 是填错了（§4.10 F28 / F31）。
OWNED_ACTIONS = ("redo_lipsync",)
VOICE_ACTION = "re_synthesize_speech"

ID_PREFIX = {
    "job": "lj_",
    "take": "lt_",
    "ftb": "ftb_",
    "ies": "ies_",
    "odm": "odm_",
    "ledger": "lil_",
    "operation": "op_",
}

CONTRACT_GAPS = [
    "C1 lipsync.input.face_track_ref 指向 S28，但 S28 的 content_class 闭集里"
    "没有 face_track；track_id / visibility 在 generation-command.v2 里没有"
    "位置（additionalProperties: false），组命令时被丢弃，S26/S27 侧无法验证"
    "S32 处理的是哪张脸。建议 S28 补 content_class 或允许 service_id=S10。",

    "C2 lipsync.input 没有任何时间字段：audio_window（start_ms / "
    "trim_head_ms / trim_tail_ms / source）无处表达。系统只能假设音频从 0ms "
    "开始，对'角色中途开口'的镜头是稳定偏移来源。S32 在自建 API 上把 "
    "audio_window 设为必填（缺则 422 AUDIO_WINDOW_REQUIRED），组命令时丢弃。",

    "C3 命令里没有 processed_frames：S25 按音频时长报价会系统性低估口型成本"
    "（30 秒视频里只有 2 秒在说话，与全程说话差 15 倍）。S32 只在本地按帧"
    "记账，不假设 S25 知道。建议 lipsync.input 增加 processed_frame_count。",

    "C4 artifact.ingested.v2 没有 frame_count / timecode_start / fps / "
    "ingesting_service 字段。S32 的 INGESTING→READY 依赖前三个；§4.9 的链式"
    "判断依赖第四个。按 S28 提供实现，不提供时阻塞在 INGESTING 并告警"
    "（T32-38），绝不伪造 READY。video_ref 的 origin_service 因此改为读调用方"
    "传入的 video_ref，而不是从归档事件推断。",

    "C5 S39 给 redo_lipsync 的 strategy_class 归一参数只有 "
    "lipsync_model_family，无法区分'改了窗口再试'与'什么都没改又试了一次'。"
    "S32 按 model_family + window_source + window_changed + "
    "face_track_revision 归一，并在台账记 repeat_without_input_change；不改"
    "S39 的方案。",

    "C6 同镜多说话人、嘴部区域重叠时没有规则：两个 job 会改同一批帧里的"
    "不同嘴，后完成的覆盖先完成的。preserve_other_faces 救不了——另一个"
    "说话人是这次生产的另一个合法目标。当前只靠检查点 CAS 让后完成者获胜，"
    "并依赖 S36 事后发现鬼影。",

    "C7 路由能力没有'支持局部重绘'这一位：S24 的能力登记里没有 "
    "face_region_localized_edit / editable_region。S32 只能在 INGESTING 靠"
    "§4.6 的度量事后发现越界，那时钱已经花了。建议 S24 增加该能力位，S25 "
    "在口型路由时必须要求它。",

    "C8 service.yaml 的 depends_on = [S26, S28, S31]，实际还依赖 S30（视频"
    "来源）、S37（AUD.TEXT.* 判定）、S25（路由）、S10/S13（人脸授权）、"
    "S36（视觉复检）、S39（RepairAction）、S41（时间线）。按实际依赖实现，"
    "生成物不手改。",

    "C9 '何时必须做口型'没有判定标准。S32 只回答'能不能做'，不回答'该不该"
    "做'；need_lipsync 是 S06/S07 或 S21 的上游字段，S32 不猜。",

    "G10 production.lipsync.ready.v2 的 source_video_ref / source_audio_ref "
    "只有 {id, content_hash}，没有 service_id / revision；audio_window 只接受 "
    "{start_ms, end_ms}（不含 trim 与 source）；frames 只接受 "
    "{process_start, process_end, fps}。事件按契约的最窄形状发，被丢掉的"
    "字段只能从 job 查询接口取回。",

    "G11 production.lipsync.ready.v2 的 frames 没有声明帧号基准（0-based 与 "
    "1-based 都读得通），x-needs-owner 已标注。S32 按 0-based 实现（与 "
    "face_track.frame_range、process_frame_start 的 >= 0 一致），未获 owner "
    "确认。",

    "G12 §6.3 的 8 条路由（列表、检查点、take 详情、validity、cancel、"
    "retry、sweep、事件入口）在 contracts/openapi.v2.yaml 里不存在，catalog "
    "只声明了 POST /lipsync-jobs 与 GET /lipsync-jobs/{id}。按 service.yaml "
    "实现，不改生成物。",

    "G13 本服务发布的 lipsync.job.state-changed / lipsync.inputs-ineligible / "
    "lipsync.take.stale / lipsync.repeat-without-change，以及消费的 "
    "speech.voice-authorization-lapsed，均不在已注册事件类型里。按 §6.4 的"
    "建议名实现，注册需改 contracts。",

    "G14 §5.2 的 lipsync_job 没有 execution_context 列，但组 "
    "generation-command 必须原样重放平台签名上下文；lipsync_intent_ledger "
    "没有 repeat_without_input_change 列，但 §8.3 要求记这个布尔。两列均补，"
    "语义与 S31 同名列为准。",

    "G15 lipsync_checkpoint 的 expected_revision 在 §5.2 里只声明为 "
    "integer DEFAULT 0，没有说明谁递增、按什么语义比较。S32 把它当作本表行"
    "自身的版本号（每次 CAS 成功 +1），与 S08 的 accepted pointer 版本无关。",

    "G16 lipsync_intent_ledger.run_id 在 §5.2 里是 text NOT NULL，但 "
    "execution-context.v2 的 18 个必填键里没有 run_id（只有 trace_id、"
    "node_epoch、execution_fence）。按 NOT NULL 实现会让每一次成功提交都在"
    "写台账时失败；伪造一个 run_id 则等于把 trace 冒充成 run。S32 把它改"
    "为可空，有则原样记、没有就留空。需要 owner 决定 run_id 从哪来"
    "（S08 的 run 记录？还是 execution-context.v2 补字段）。",

    "G17 §3.1 的终态清单漏了 `RECONCILED_READY`：同一节的转移表与状态图都"
    "把 `WAITING_UNKNOWN → RECONCILED_READY` 画成汇点，产物语义也写明"
    "「同 `INGESTING→READY`」。S32 按状态图把它算作终态（并入 "
    "`TERMINAL_STATES`），不改转移表——`ALLOWED_TRANSITIONS` 里它本来就是"
    "空集。若不按终态处理，取消它会撞 `_transition` 的断言（500 而非 409）、"
    "迟到事件会报一个 job 并不处于的状态、`recover` 会把健康 job 归入 "
    "`unhandled`。需要 owner 确认是清单漏写，而不是 `RECONCILED_READY` 另有"
    "出边。",

    "G18 `/lipsync-takes/{id}/validity` 的 `basis` 只有 `audio_rights_void` "
    "一个权利侧的取值（§4.2 / T32-54），但 §4.2 的合取条件是 "
    "`audio_rights_state == 'usable'`，还有第三种事实：S31 读不到（超时、"
    "503）。把「读不到」写成 `audio_rights_void` 是伪造因果，写成 "
    "`valid: true` 则与本响应自己的 `checks.audio_rights_usable: false` 打架。"
    "S32 返回 `basis='audio_rights_unknown'`，与 void 分开。需要 owner 确认"
    "这个取值，或为「未知」另定一个统一码（其他消费 S31 授权的服务也会遇到）。",
]
