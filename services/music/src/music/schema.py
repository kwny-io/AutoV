"""S34 自己的 schema。表来源是 `plans/S34-music.md` §5.2。

六条结构性事实，它们决定了这张库能证明什么、不能证明什么：

1. **内容层与位置层分离。** `music_take` 是花过钱的内容，`cut_plan_cache` 是
   免费可重算的位置。缓存整张表可以 TRUNCATE（§4.3 / T34-39），任何对外读取
   都必须现算。把 `CutPlan` 当权威读出来，就等于让 S34 成为剪辑相关事实的
   第二权威。
2. **`content_fingerprint` 不含时长、不含 `candidate_count`。** 只含内容维度
   （mood / 强度 / 编制 / 速度区间 / 调性 / 意图原文，§2.2）。时长进指纹的后果
   是剪辑动一下就把整集 BGM 重生成一遍——正是本服务要防的那笔钱（§0）。
3. **一个逻辑 job 有 N 个候选，全部或全无。** `music_job` 是逻辑对象，
   `music_job_candidate` 是它对应的 N 个 `provider_job_id`（§4.7）。中间失败
   不能交付半个候选集（F10 / T34-18）。
4. **许可四元组。** `license_binding` 记录作曲 / 录音 / 表演 / 同步四个**互相
   独立**的状态（§4.4）。DDL 允许 `unknown` 存在（快照要如实记录 S02 说的话），
   但 `music_take` **不允许引用含 `unknown` 的 binding** —— 这条要查另一张表，
   SQLite 的 CHECK 表达不了，由 `service._ensure_binding_usable` 在应用层守，
   并在 T34-14 覆盖。
5. **`CueSelection.selected` 必须有人。** `ms_selected_has_decider` 把"自动选择
   伪装成人选"堵死：`auto_single` 也要写 `selected_by='system:auto_single'`。
   否则事后无法区分"忘了记"和"只有一个所以自动选了它"（§5.3 行 1072）。
6. **`bar_extend` 有前提。** `tail_kind` 只有 `sustaining` / `fade_out` 允许
   延长尾音；`hard_stop` 延长会听起来像卡住（§4.3 行 452–454）。

本仓服务层一律 SQLite，所以方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC
字符串、`text[]` / `integer[]` 存 JSON 数组、`uuid` 存 TEXT，`->>'k'` 用
`json_extract`，`cardinality(x) > 0` / `array_length(x,1) > 0` 用
`json_array_length(x) > 0`，`btrim` 用 `trim`。

`outbox` / `inbox` / `idempotency_record` 由平台 SDK 建表，这里不重复 DDL；
方案 §5.2 的 `command_receipt` 与平台 `idempotency_record` 是同一张表
（`tenant_id + command_id + request_hash + result`），因此不建。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 音乐意图版本（不可变） ----------
CREATE TABLE IF NOT EXISTS music_cue_revision (
  tenant_id        TEXT NOT NULL,
  cue_id           TEXT NOT NULL,
  cue_revision     INTEGER NOT NULL CHECK (cue_revision >= 1),
  scene_lineage_id TEXT NOT NULL,
  cue_index        INTEGER NOT NULL CHECK (cue_index >= 0),
  beat_ref         TEXT,
  mood             TEXT NOT NULL CHECK (mood IN (
    'tender','melancholic','hopeful','tense','ominous','triumphant',
    'playful','somber','awe','urgency','dissolution','neutral')),
  mood_intensity   REAL NOT NULL CHECK (mood_intensity >= 0 AND mood_intensity <= 1),
  instrumentation  TEXT NOT NULL CHECK (instrumentation IN (
    'orchestral','chamber','synth','hybrid','solo_piano','solo_strings',
    'band','percussive','ambient')),
  tempo_hint_low_bpm  INTEGER CHECK (tempo_hint_low_bpm IS NULL OR tempo_hint_low_bpm >= 20),
  tempo_hint_high_bpm INTEGER CHECK (tempo_hint_high_bpm IS NULL OR tempo_hint_high_bpm >= 20),
  key_hint         TEXT,
  music_intent     TEXT NOT NULL,
  intent_hash      TEXT NOT NULL,
  instrumental_only INTEGER NOT NULL DEFAULT 1 CHECK (instrumental_only IN (0,1)),
  dialogue_aware   INTEGER NOT NULL DEFAULT 1 CHECK (dialogue_aware IN (0,1)),
  source_ref       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, cue_id, cue_revision),
  CONSTRAINT mcr_intent_not_blank CHECK (trim(music_intent) <> ''),
  CONSTRAINT mcr_hash_len CHECK (length(intent_hash) = 64),
  CONSTRAINT mcr_tempo_order CHECK (
    tempo_hint_low_bpm IS NULL OR tempo_hint_high_bpm IS NULL
    OR tempo_hint_low_bpm <= tempo_hint_high_bpm),
  CONSTRAINT mcr_source_known
    CHECK (json_extract(source_ref, '$.service_id') IN ('S06','S07'))
);

CREATE UNIQUE INDEX IF NOT EXISTS mcr_intent_uniq
  ON music_cue_revision (tenant_id, cue_id, intent_hash);
CREATE INDEX IF NOT EXISTS mcr_scene_idx
  ON music_cue_revision (tenant_id, scene_lineage_id, cue_index);

-- ---------- 许可四元组快照（不可变） ----------
CREATE TABLE IF NOT EXISTS license_binding (
  license_binding_id TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  policy_ref         TEXT NOT NULL,

  composition_status TEXT NOT NULL CHECK (composition_status IN
    ('licensed_generated','licensed_third_party','public_domain','unknown')),
  composition_holder TEXT,
  master_status      TEXT NOT NULL CHECK (master_status IN
    ('licensed_generated','licensed_third_party','public_domain','unknown')),
  master_holder      TEXT,
  performance_status TEXT NOT NULL CHECK (performance_status IN
    ('licensed','not_applicable','unknown')),
  sync_status        TEXT NOT NULL CHECK (sync_status IN
    ('licensed','restricted','expired','unknown')),
  sync_territories   TEXT NOT NULL DEFAULT '[]',
  sync_media         TEXT NOT NULL DEFAULT '[]',
  sync_expires_at    TEXT,

  work_title         TEXT,
  isrc               TEXT,
  iswc               TEXT,
  attribution_text   TEXT,
  training_basis_ref TEXT,

  resolved_at        TEXT NOT NULL,

  CONSTRAINT lb_ref_known CHECK (json_extract(policy_ref, '$.service_id') = 'S02'),
  CONSTRAINT lb_hash_len
    CHECK (length(json_extract(policy_ref, '$.content_hash')) = 64),
  -- `licensed` 但不知道在哪些地区可用 → 发行时才发现（§5.3）。
  CONSTRAINT lb_sync_has_territory CHECK (
    sync_status <> 'licensed' OR json_array_length(sync_territories) > 0),
  -- 声称 `licensed_generated` 但没有训练合规声明（§5.3 / T34-15）。
  -- 注意 SQL 的优先级：`(A AND B) OR C`，与方案原式同义。
  CONSTRAINT lb_generated_has_basis CHECK (
    composition_status <> 'licensed_generated' AND master_status <> 'licensed_generated'
    OR training_basis_ref IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS lb_policy_idx
  ON license_binding (tenant_id, json_extract(policy_ref, '$.id'),
                      json_extract(policy_ref, '$.revision'));

-- ---------- 生成意图 ----------
CREATE TABLE IF NOT EXISTS music_job (
  music_job_id       TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  project_id         TEXT NOT NULL,
  series_id          TEXT NOT NULL,
  episode_id         TEXT NOT NULL,
  branch_id          TEXT NOT NULL,

  cue_id             TEXT NOT NULL,
  cue_revision       INTEGER NOT NULL,
  scene_lineage_id   TEXT NOT NULL,
  cue_index          INTEGER NOT NULL,

  requested_duration_ms INTEGER NOT NULL CHECK (requested_duration_ms > 0),
  candidate_count    INTEGER NOT NULL CHECK (candidate_count BETWEEN 1 AND 3),
  candidate_index_base INTEGER NOT NULL DEFAULT 0,

  -- **可空**，只为容纳 POLICY_BLOCKED 之前的三个状态。方案 §5.2 写 NOT NULL，
  -- 但 §3.2 又要求 POLICY_BLOCKED 可以没有可用许可。两者唯一兼容的读法是
  -- "POLICY_BLOCKED 及之前可以没有可用 binding"，由下面的
  -- mj_binding_required 补回方案本意：没有许可依据的 job 不许走到 PREPARING。
  -- 注意：即使 S02 说"四元组有 unknown"，本服务也会写一行 binding 如实记录
  -- S02 的回答，只是这行 binding 的 usable=false（见 license_binding 注释 4）。
  license_binding_id TEXT REFERENCES license_binding (license_binding_id),

  -- 冻结：内容指纹不含时长（§2.2 / §8.1）。
  content_fingerprint TEXT NOT NULL,

  node_id            TEXT NOT NULL,
  node_epoch_at_create INTEGER NOT NULL,
  execution_fence    INTEGER NOT NULL,

  request_hash       TEXT NOT NULL,
  idempotency_key    TEXT NOT NULL,
  variation_nonce    TEXT NOT NULL,

  route_decision_ref TEXT,
  -- §5.2 没有这两列，但 `:retry` 与重启恢复都必须重新组装 GenerationCommand，
  -- 而 18 键的 execution-context 和 route_policy_ref 都是必填。丢了它们就只能
  -- 瞎填，所以把请求里那份**已签名上下文**原样留在 job 上。
  execution_context  TEXT NOT NULL DEFAULT '{}',
  route_policy_ref   TEXT,
  budget_scope_id    TEXT NOT NULL,
  deadline           TEXT NOT NULL,

  -- 逻辑 job 的汇总状态；provider job 明细在 music_job_candidate。
  quote_upper_bound_micro_total INTEGER,

  business_attempt_no INTEGER NOT NULL DEFAULT 1,
  state              TEXT NOT NULL,
  state_seq          INTEGER NOT NULL DEFAULT 0,
  failure_code       TEXT,
  failure_detail     TEXT,
  reconciled         INTEGER NOT NULL DEFAULT 0 CHECK (reconciled IN (0,1)),

  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  state_entered_at   TEXT NOT NULL,

  CONSTRAINT mj_state_check CHECK (state IN (
    'CREATED','CHECKING_POLICY','POLICY_BLOCKED','PREPARING','REJECTED_LOCAL',
    'SUBMITTED','WAITING_PROVIDER','WAITING_UNKNOWN','INGESTING','ANALYZING',
    'CANDIDATES_READY','FAILED','RECONCILED_CANDIDATES','RECONCILED_FAILED',
    'GAP_RECORDED','CANCELED')),
  CONSTRAINT mj_tenant_idem UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT mj_fingerprint_len CHECK (length(content_fingerprint) = 64),
  CONSTRAINT mj_binding_required CHECK (
    license_binding_id IS NOT NULL
    OR state IN ('CREATED','CHECKING_POLICY','POLICY_BLOCKED'))
);

-- 业务幂等：同一内容 + 同一时长需求只留一个未终结 job（§7.1）。
-- 键含 requested_duration_ms，**不含 candidate_count** —— 要 1 个还是要 3 个
-- 是偏好，不是内容需求（§7.1 行 1293–1295）。
CREATE UNIQUE INDEX IF NOT EXISTS music_job_open_uniq
  ON music_job (tenant_id, scene_lineage_id, cue_index,
                content_fingerprint, requested_duration_ms)
  WHERE state NOT IN ('CANDIDATES_READY','POLICY_BLOCKED','GAP_RECORDED',
                      'REJECTED_LOCAL','FAILED','RECONCILED_FAILED','CANCELED');

CREATE INDEX IF NOT EXISTS mj_cue_idx
  ON music_job (tenant_id, cue_id, created_at DESC);
CREATE INDEX IF NOT EXISTS mj_episode_idx
  ON music_job (tenant_id, episode_id, state);
CREATE INDEX IF NOT EXISTS mj_state_idx
  ON music_job (state, state_entered_at)
  WHERE state IN ('CHECKING_POLICY','PREPARING','SUBMITTED','WAITING_UNKNOWN',
                  'INGESTING','ANALYZING');

-- ---------- 逻辑 job ↔ provider job ----------
-- 方案 §5.2 的主键是 (music_job_id, candidate_index)。`:retry` 要求把失败后的
-- 候选重新提交，而 UNIQUE(provider_job_id) 与"候选行不可变"让同一主键无法再
-- 落一行——两者只能二选一。这里加 attempt_no 区分尝试（与 music_job 的
-- business_attempt_no 同义），历史因此保留，provider_job_id 仍全局唯一。
CREATE TABLE IF NOT EXISTS music_job_candidate (
  music_job_id    TEXT NOT NULL REFERENCES music_job (music_job_id),
  candidate_index INTEGER NOT NULL CHECK (candidate_index >= 0),
  attempt_no      INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no >= 1),
  provider_job_id TEXT NOT NULL,
  attempt_id      TEXT NOT NULL,
  quote_upper_bound_micro INTEGER,
  state           TEXT NOT NULL CHECK (state IN
    ('SUBMITTED','WAITING','ARCHIVED','FAILED','UNKNOWN','CANCELED')),
  failure_code    TEXT,
  -- 归档中间态。§3.2 的 INGESTING 要收齐 N 份归档才进 ANALYZING，而 §3.4 要求
  -- MusicTake 与 BeatGrid / LoopPointSet **同一个事务**写入。两者夹在一起，
  -- 归档结果就必须先停在候选行上——§5.2 的候选表没有这几列，所以补在这里，
  -- 不提前写半成品 take。
  artifact_ref      TEXT,
  natural_duration_ms INTEGER,
  sample_rate_hz    INTEGER,
  channel_count     INTEGER,
  container_format  TEXT,
  content_hash      TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (music_job_id, candidate_index, attempt_no),
  UNIQUE (provider_job_id)
);

-- ---------- 已归档候选 ----------
CREATE TABLE IF NOT EXISTS music_take (
  music_take_id      TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  music_job_id       TEXT NOT NULL REFERENCES music_job (music_job_id),
  candidate_index    INTEGER NOT NULL,
  cue_id             TEXT NOT NULL,
  scene_lineage_id   TEXT NOT NULL,
  cue_index          INTEGER NOT NULL,

  artifact_ref       TEXT NOT NULL,        -- 恒指向 S28
  natural_duration_ms INTEGER NOT NULL CHECK (natural_duration_ms > 0),
  sample_rate_hz     INTEGER NOT NULL CHECK (sample_rate_hz >= 8000),
  channel_count      INTEGER NOT NULL CHECK (channel_count >= 1),
  container_format   TEXT NOT NULL,

  detect_bpm         REAL CHECK (detect_bpm IS NULL OR detect_bpm > 0),
  detected_key       TEXT,
  tail_kind          TEXT NOT NULL CHECK (tail_kind IN
    ('sustaining','hard_stop','fade_out','unknown')),
  vocal_presence     INTEGER NOT NULL DEFAULT 0 CHECK (vocal_presence IN (0,1)),
  instrumental_required INTEGER NOT NULL CHECK (instrumental_required IN (0,1)),

  content_fingerprint TEXT NOT NULL,

  license_binding_id TEXT NOT NULL REFERENCES license_binding (license_binding_id),
  rights_state       TEXT NOT NULL CHECK (rights_state IN ('usable','void','unknown')),
  rights_checked_at  TEXT,

  dialogue_density_hint TEXT,
  dialogue_density_source TEXT CHECK (dialogue_density_source IN
    ('s37_segments','s06_script','provided','unknown')),

  provider_job_id    TEXT NOT NULL,
  attempt_id         TEXT NOT NULL,
  source_kind        TEXT NOT NULL CHECK (source_kind IN ('generated','licensed_library')),

  state              TEXT NOT NULL CHECK (state IN
    ('READY','SELECTED','REJECTED','SUPERSEDED')),
  created_at         TEXT NOT NULL,

  -- 供应商直链不能进 artifact_ref（T34-44 / E01）。
  CONSTRAINT mt_artifact_is_s28
    CHECK (json_extract(artifact_ref, '$.service_id') = 'S28'),
  CONSTRAINT mt_hash_len
    CHECK (length(json_extract(artifact_ref, '$.content_hash')) = 64),
  CONSTRAINT mt_fingerprint_len CHECK (length(content_fingerprint) = 64),
  -- 声明无人声的 take 若探测到人声，不允许进入候选池（§4.8 F9 / §11 U7）。
  CONSTRAINT mt_no_unwanted_vocal CHECK (
    NOT (instrumental_required = 1 AND vocal_presence = 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS mt_artifact_uniq
  ON music_take (tenant_id, json_extract(artifact_ref, '$.id'),
                 json_extract(artifact_ref, '$.content_hash'));
CREATE INDEX IF NOT EXISTS mt_cue_idx
  ON music_take (tenant_id, scene_lineage_id, cue_index, created_at DESC);
CREATE INDEX IF NOT EXISTS mt_group_idx
  ON music_take (tenant_id, music_job_id, candidate_index);

-- ---------- 节拍网格（不可变） ----------
CREATE TABLE IF NOT EXISTS beat_grid (
  beat_grid_id    TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  music_take_id   TEXT NOT NULL REFERENCES music_take (music_take_id),
  bpm             REAL NOT NULL CHECK (bpm > 0),
  meter_num       INTEGER NOT NULL CHECK (meter_num > 0),
  meter_den       INTEGER NOT NULL CHECK (meter_den > 0),
  downbeat_ms     TEXT NOT NULL,
  phrase_ms       TEXT NOT NULL DEFAULT '[]',
  confidence      REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  low_confidence  INTEGER NOT NULL DEFAULT 0 CHECK (low_confidence IN (0,1)),
  detector_model  TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  CONSTRAINT bg_downbeats_ascending CHECK (json_array_length(downbeat_ms) > 0),
  CONSTRAINT bg_unique_take UNIQUE (music_take_id)
);
CREATE INDEX IF NOT EXISTS bg_lowconf_idx
  ON beat_grid (tenant_id) WHERE low_confidence = 1;

-- ---------- 循环点（不可变） ----------
CREATE TABLE IF NOT EXISTS loop_point_set (
  loop_point_set_id TEXT PRIMARY KEY,
  tenant_id         TEXT NOT NULL,
  music_take_id     TEXT NOT NULL REFERENCES music_take (music_take_id),
  ranges            TEXT NOT NULL,
  best_seam_score   REAL,
  created_at        TEXT NOT NULL,
  CONSTRAINT lps_has_take UNIQUE (music_take_id),
  CONSTRAINT lps_ranges_array CHECK (json_array_length(ranges) >= 0
                                     AND json_type(ranges) = 'array')
);

-- ---------- 选定 ----------
CREATE TABLE IF NOT EXISTS cue_selection (
  selection_id    TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  scene_lineage_id TEXT NOT NULL,
  cue_index       INTEGER NOT NULL,
  cue_id          TEXT NOT NULL,
  cue_revision    INTEGER NOT NULL,
  music_job_id    TEXT NOT NULL REFERENCES music_job (music_job_id),
  music_take_id   TEXT REFERENCES music_take (music_take_id),

  state           TEXT NOT NULL CHECK (state IN
    ('pending','selected','superseded','rejected_all')),
  selected_by     TEXT,
  selection_kind  TEXT CHECK (selection_kind IN ('auto_single','human','fallback')),
  reason          TEXT,

  created_at      TEXT NOT NULL,
  decided_at      TEXT,
  superseded_by   TEXT REFERENCES cue_selection (selection_id),

  CONSTRAINT ms_selected_has_take CHECK (
    state <> 'selected' OR music_take_id IS NOT NULL),
  -- 防"已选定"但没人选过——自动选择伪装成人选（§5.3 行 1064/1072）。
  CONSTRAINT ms_selected_has_decider CHECK (
    state <> 'selected' OR selected_by IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS ms_current_idx
  ON cue_selection (tenant_id, scene_lineage_id, cue_index)
  WHERE state IN ('pending','selected');
CREATE INDEX IF NOT EXISTS ms_job_idx
  ON cue_selection (tenant_id, music_job_id, created_at DESC);

-- ---------- 检查点 ----------
CREATE TABLE IF NOT EXISTS music_checkpoint (
  tenant_id       TEXT NOT NULL,
  scene_lineage_id TEXT NOT NULL,
  cue_index       INTEGER NOT NULL,
  music_take_id   TEXT NOT NULL REFERENCES music_take (music_take_id),
  selection_id    TEXT NOT NULL REFERENCES cue_selection (selection_id),
  checkpoint_seq  INTEGER NOT NULL,
  expected_revision INTEGER NOT NULL DEFAULT 0,
  updated_by_job  TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, scene_lineage_id, cue_index)
);

-- ---------- 缺口 ----------
CREATE TABLE IF NOT EXISTS music_gap (
  gap_id          TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  scene_lineage_id TEXT NOT NULL,
  cue_index       INTEGER NOT NULL,
  cue_id          TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN (
    'no_licensed_route','license_incomplete','provider_failed',
    'submission_unrecoverable','beat_unanalyzable','duration_mismatch',
    'rights_lapsed','artifact_unavailable','all_candidates_rejected',
    'awaiting_selection','not_instrumental')),
  detail          TEXT NOT NULL,
  source_job_id   TEXT,
  state           TEXT NOT NULL CHECK (state IN ('open','closed','waived')),
  closed_by_take_id TEXT,
  waived_decision_id TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  -- 说"已闭合"必有闭合依据；说"豁免"必有裁决引用（§5.3）。
  CONSTRAINT mg_state_consistent CHECK (
    (state = 'open' AND closed_by_take_id IS NULL AND waived_decision_id IS NULL)
    OR (state = 'closed' AND closed_by_take_id IS NOT NULL)
    OR (state = 'waived' AND waived_decision_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS mg_open_idx
  ON music_gap (tenant_id, scene_lineage_id, cue_index)
  WHERE state = 'open';
CREATE INDEX IF NOT EXISTS mg_episode_idx
  ON music_gap (tenant_id, cue_id) WHERE state = 'open';

-- ---------- CutPlan 缓存（非权威） ----------
CREATE TABLE IF NOT EXISTS cut_plan_cache (
  tenant_id       TEXT NOT NULL,
  music_take_id   TEXT NOT NULL,
  edl_revision    INTEGER NOT NULL,
  requested_duration_ms INTEGER NOT NULL,
  end_strategy    TEXT NOT NULL CHECK (end_strategy IN
    ('bar','phrase','loop','bar_extend','hard','unplaceable')),
  segments        TEXT NOT NULL,
  transition      TEXT,
  fade_in_ms      INTEGER,
  fade_out_ms     INTEGER,
  confidence      REAL,
  reason          TEXT,
  computed_at     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, music_take_id, edl_revision, requested_duration_ms)
);
-- 这张表可以被 TRUNCATE，不影响任何正确性（§4.3 / T34-39）。

-- ---------- 生产意图台账 ----------
-- 一行 = 一个音乐意图（job），不是一行一个候选（§8.1）。`candidate_count`
-- 是字段：一次"生成 3 个候选"是一个决策，不是三个。
CREATE TABLE IF NOT EXISTS music_intent_ledger (
  entry_id        TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  music_job_id    TEXT NOT NULL,
  scene_lineage_id TEXT NOT NULL,
  cue_index       INTEGER NOT NULL,
  action          TEXT NOT NULL CHECK (action IN
    ('generate','resynthesize_for_duration','fill_gap','licensed_retrieval')),
  candidate_count INTEGER NOT NULL,
  run_id          TEXT,
  node_epoch      INTEGER NOT NULL,
  manifest_id     TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mil_scene_idx
  ON music_intent_ledger (tenant_id, scene_lineage_id, created_at DESC);

-- ---------- 对白活跃度提示（签名透传，S34 不解读） ----------
-- 提示天然属于 cue（S37 按场景/说话人分段产出），而 take 在生成完成前不存在。
-- 所以提示单独放，落 take 时再读。§4.5："收到就存、就透出，不解读"。
CREATE TABLE IF NOT EXISTS dialogue_hint (
  tenant_id  TEXT NOT NULL,
  cue_id     TEXT NOT NULL,
  hint       TEXT,
  source     TEXT NOT NULL CHECK (source IN
    ('s37_segments','s06_script','provided','unknown')),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, cue_id)
);

-- ---------- 状态转移历史 ----------
CREATE TABLE IF NOT EXISTS job_transition (
  music_job_id TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  from_state   TEXT,
  to_state     TEXT NOT NULL,
  reason_code  TEXT,
  detail       TEXT,
  at           TEXT NOT NULL,
  PRIMARY KEY (music_job_id, seq)
);
"""

# ---------------------------------------------------------------- 闭集与策略

# §2.3：契约里没有闭集（C1），本服务内部定，且**不从 music_intent 猜**。
MOODS = ("tender", "melancholic", "hopeful", "tense", "ominous", "triumphant",
         "playful", "somber", "awe", "urgency", "dissolution", "neutral")

INSTRUMENTATIONS = ("orchestral", "chamber", "synth", "hybrid", "solo_piano",
                    "solo_strings", "band", "percussive", "ambient")

TAIL_KINDS = ("sustaining", "hard_stop", "fade_out", "unknown")

# §4.3 第 4 行：只有尾音可延续的曲子才允许 `bar_extend`。
EXTENDABLE_TAIL_KINDS = frozenset({"sustaining", "fade_out"})

GAP_KINDS = (
    "no_licensed_route", "license_incomplete", "provider_failed",
    "submission_unrecoverable", "beat_unanalyzable", "duration_mismatch",
    "rights_lapsed", "artifact_unavailable", "all_candidates_rejected",
    "awaiting_selection", "not_instrumental",
)

JOB_STATES = (
    "CREATED", "CHECKING_POLICY", "POLICY_BLOCKED", "PREPARING",
    "REJECTED_LOCAL", "SUBMITTED", "WAITING_PROVIDER", "WAITING_UNKNOWN",
    "INGESTING", "ANALYZING", "CANDIDATES_READY", "FAILED",
    "RECONCILED_CANDIDATES", "RECONCILED_FAILED", "GAP_RECORDED", "CANCELED",
)

TERMINAL_STATES = frozenset({
    "CANDIDATES_READY", "POLICY_BLOCKED", "GAP_RECORDED", "REJECTED_LOCAL",
    "FAILED", "RECONCILED_FAILED", "CANCELED",
})

# `:retry` 只允许这两种（§6.3）：POLICY_BLOCKED 要等策略、WAITING_UNKNOWN
# 必须先对账、GAP_RECORDED 是"这条 cue 缺了"的事实不是可重试的失败。
RETRYABLE_STATES = frozenset({"FAILED", "RECONCILED_FAILED"})

SELECTION_STATES = ("pending", "selected", "superseded", "rejected_all")

CANDIDATE_STATES = ("SUBMITTED", "WAITING", "ARCHIVED", "FAILED", "UNKNOWN",
                    "CANCELED")

# 候选入库阶段中，**会让整组作废**的失败（F10/F11/F12）与**只影响该候选**的
# 失败（F14/F15/F16/F17）。两者的分界是"这份音频本身能不能用"。
SET_KILLING_FAILURES = frozenset({"DURATION_UNKNOWN", "HASH_MISMATCH",
                                  "ARTIFACT_UNAVAILABLE"})
PER_CANDIDATE_FAILURES = frozenset({"BEAT_UNANALYZABLE", "RIGHTS_LAPSED",
                                    "NOT_INSTRUMENTAL"})

# §2.4 / §4.3：低于阈值时小节线不可信，禁用 `bar_trim`。
BEAT_CONFIDENCE_THRESHOLD = 0.85

# §4.3 行 435：90 BPM 下一拍 667ms，80ms 是 1/8 拍。方案说"按帧率换算"，
# 24fps 一帧 41.7ms，两点帧 ≈ 83ms，取 80ms 与方案一致（§11 U4）。
BAR_TOLERANCE_MS = 80

# 位置层的淡入淡出是"防爆音"的机械值，不是创作参数；创作参数在 S42。
FADE_OUT_MS = 300

# §4.6：超过 24h 无人选择只记缺口 + 告警，**不自动选第一个**。
SELECTION_TIMEOUT_SECONDS = 24 * 3600

# 契约只给了 exclusiveMinimum: 0；上限是 DoS 防御，不是产品约束。
MAX_DURATION_SECONDS = 600.0

MAX_CANDIDATES = 3

ID_PREFIX = {
    "job": "mj_",
    "binding": "lb_",
    "take": "mt_",
    "beat": "bg_",
    "loop": "lps_",
    "selection": "sel_",
    "gap": "mg_",
    "ledger": "mil_",
}

CONTRACT_GAPS = [
    {
        "id": "MUS-1",
        "kind": "gap",
        "summary": "music.input 是 {music_intent, duration_seconds, instrumental_only, "
                   "rights_policy_ref} 且 additionalProperties:false；mood / 强度 / "
                   "instrumentation / tempo_hint / key_hint / beat_ref / scene_lineage_id / "
                   "cue_index / candidate_count 都不存在（§12 C1）。",
        "handling": "在本服务 API 上要求这些字段，组 GenerationCommand 时只传契约允许的"
                    "四项并丢弃其余（与 §12 C1 '不选边' 一致）。组命令处见 "
                    "service._compose_command。",
    },
    {
        "id": "MUS-2",
        "kind": "gap",
        "summary": "generation-command 没有 candidate_count，N 个候选 = N 次独立准入；"
                   "N 份 reserve / permit / grant，中途失败会留下半个候选集（§4.7 / §12 C2）。",
        "handling": "取路径 A：发 N 次独立 POST /generation-jobs，共享 music_job_id 作为 "
                    "selection_group，PREPARING 阶段按 N × quote_upper_bound_micro 检查预算，"
                    "任何一份失败整组作废（FAILED(PARTIAL_CANDIDATES)），不降级候选数。",
    },
    {
        "id": "MUS-3",
        "kind": "needs_owner",
        "summary": "docs/ 没有定义 S02 如何回答'给定曲目 → 作曲/录音/表演/同步四元组'"
                   "（§12 C3）。没有它，composition_status 等只能填 unknown，而 unknown "
                   "会让所有 take 不可用，service.yaml 的 acceptance'可追溯许可'无法达成。",
        "handling": "注入 rights_resolver callback，不猜 endpoint；未配置 → 503。"
                    "四元组任一 unknown / sync 过期 → POLICY_BLOCKED + 缺口 license_incomplete。",
    },
    {
        "id": "MUS-4",
        "kind": "gap",
        "summary": "generation-command.context（execution-context.v2）与 input 都是 "
                   "additionalProperties:false，candidate_index 没有任何合法位置可放（§4.7 建议"
                   "'放进 context 或 metadata'，但两处都关着）。",
        "handling": "用互不相同的 attempt_id / idempotency_key 区分 N 个子 job，映射关系落在"
                    " music_job_candidate；事件关联只能靠 provider_job_id（见 MUS-5）。",
    },
    {
        "id": "MUS-5",
        "kind": "gap",
        "summary": "artifact.ingested 没有 payload schema，而多候选归档必须能归到 "
                   "(music_job_id, candidate_index)，且需要 natural_duration_ms / "
                   "sample_rate_hz / channel_count（§12 C4）。",
        "handling": "按 data.provider_job_id（或 data.source.provider_job_id）反查 "
                    "music_job_candidate；缺时长不按 0 处理，记缺口并整组失败，绝不当'未知但先放着'。",
    },
    {
        "id": "MUS-6",
        "kind": "gap",
        "summary": "S41 gaps[].kind 闭集里没有'音乐缺失'，一整条 BGM 缺失时 formal_master "
                   "可能仍为真（§12 C6）。",
        "handling": "缺口经 GET /music-cues?episode_id=&has_gap=true 暴露给 S41 的 completeness，"
                    "**不**映射进 S41 的 kind 闭集。",
    },
    {
        "id": "MUS-7",
        "kind": "gap",
        "summary": "service.yaml 的 depends_on 只有 [S26, S28]，实际还依赖 S02（许可）、"
                   "S25（route_decision_id）、S41（EDL）、S37（对白密度）、S43（申报）（§12 C5）。",
        "handling": "按实际依赖实现为注入式 closure；service.yaml 是 catalog 生成物，不改。",
    },
    {
        "id": "MUS-8",
        "kind": "conflict",
        "summary": "production.music.ready 的 aggregate 在方案 §6.4 写 scene_lineage_id，"
                   "契约 description 里同时出现 scene 与 shot 两种说法（PMR-1）；事件的 "
                   "selection_state/gap_state 又是 cue 级。",
        "handling": "按 §6.4 用 scene_lineage_id 作 aggregate，sequence 取该 cue 的 "
                    "checkpoint_seq（没有则 1）。契约待统一。",
    },
    {
        "id": "MUS-9",
        "kind": "conflict",
        "summary": "production.music.ready 的候选要求 loop_available，但 music_take 与 "
                   "loop_point_set 都没有这一列（PMR-2）。",
        "handling": "从 loop_point_set.ranges 非空推导，不新增 DDL 列（与方案表结构保持一致）。",
    },
    {
        "id": "MUS-10",
        "kind": "conflict",
        "summary": "content_fingerprint 在事件里是 cue 级，在 §5.2 的 music_take 上是 "
                   "take 级（PMR-3）。",
        "handling": "同一 job 的每个 take 写同一个指纹值（内容由 cue 意图决定，不因候选不同"
                    "而不同），事件取 job 的值。",
    },
    {
        "id": "MUS-11",
        "kind": "gap",
        "summary": "contracts/openapi.v2.yaml 里没有任何 music 路径，而 catalog / "
                   "service.yaml 声明了 POST /music-jobs 与 GET /music-cues/{id}。",
        "handling": "按方案 §6.1–§6.3 与 service.yaml 实现；不修改 contracts。",
    },
    {
        "id": "MUS-12",
        "kind": "needs_owner",
        "summary": "§11 U3/U4/U7 未定：节拍检测器选型、±80ms 容差是否合适、人声探测阈值。",
        "handling": "检测器做成注入式 analysis_probe；阈值 0.85 与容差 80ms 放本文件常量"
                    "（BEAT_CONFIDENCE_THRESHOLD / BAR_TOLERANCE_MS），换检测器时只改这两处。",
    },
    {
        "id": "MUS-13",
        "kind": "gap",
        "summary": "music.input.duration_seconds 是必填，但它主要是时间线约束而非创作输入"
                   "（§12 C9）：39 秒与 42 秒的 BGM 是同一首曲子，只是收尾位置不同。",
        "handling": "时长不进 content_fingerprint；改时长只重算 CutPlan（零花费、零台账），"
                    "只有 CutPlan 落到 Unplaceable 才走 resynthesize_for_duration。",
    },
    {
        "id": "MUS-14",
        "kind": "needs_owner",
        "summary": "§11 U2 未定：先要 1 个候选、后要 3 个怎么处理。",
        "handling": "业务幂等键不含 candidate_count（§7.1）：同内容同时长会复用未终结的 job；"
                    "已在终结状态时不拦，允许新 job 共享 cue_id。'扩展已有 job' 未实现。",
    },
    {
        "id": "MUS-15",
        "kind": "gap",
        "summary": "§2.5 说 measured_seam_score'越小越好'，§4.3 又写 "
                   "confidence = min(seam_score 归一, beat_confidence)，两处对方向的假设相反。",
        "handling": "按 §4.3 实现：ranges[].seam_score 取 0..1 的**质量分（越大越好）**；"
                    "若检测器实际输出距离度量，需在 analysis_probe 里转成质量分。",
    },
    {
        "id": "MUS-16",
        "kind": "gap",
        "summary": "contracts 没有 S06 的场景查询契约，`S06_BASE_URL` 未配置时 "
                   "`scene_check` 为 None，`_validate` 直接跳过场景归属与 cue_index 校验。",
        "handling": "未配置即跳过（不阻断创建：场景检查是旁证，不是付费前提）。这是"
                    "**已知的松**：此时一条 cue 可以指向任意 scene_lineage_id，跨租户的"
                    "场景 id 不会被发现。配置后按 F28 统一 404，不区分 404/403。",
    },
]
