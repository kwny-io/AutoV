"""S30 自己的 schema。表来源是 `plans/S30-video-production.md` §5.2。

七条结构性事实，它们决定了这张库能证明什么、不能证明什么：

1. **`shot_video_checkpoint` 不是 S08 的接受指针。** 它回答"下游 S41 该拿哪一段
   视频"，不回答"这一集被接受了吗"。把两者合并，不是 S41 拿不到片段，就是
   S30 越权推进接受指针——两者都会造出第二个权威。
2. **`video_job_open_uniq` 是 E16 落到数据库里的那一条。** 同一
   `(tenant, shot_lineage_id, segment_no, input_fingerprint)` 同时只能有一个
   未终结 job，`WAITING_UNKNOWN` 算未终结。想"再建一个 job 绕过 UNKNOWN 重提"
   的代码路径会撞唯一约束，而不是悄悄成功（§4.5 / T30-15）。
3. **`video_job_repair_uniq` 让同一个 `RepairAction` 只执行一次。** §7.1 的修复
   幂等键是 `(tenant, repair_action_id)`；重复投递是 409，不是第二次付费生成。
   方案 §5.2 的 `video_job` 既没有这一列也没有这个索引（C30-7）。
4. **`dm_*` 四条约束把"裁了还是不对"变成不可能。** `dm_math` 保证三段自洽；
   `dm_target_match` 保证裁完等于目标，另有一个只放行"不裁且按档位交付"的
   析取项（分支 B 的全部形态）；`dm_both_forbidden_default` 保证 `both` 只在
   `trim_policy='c2_minimal'` 下出现；`dm_head_only_forbidden` 让 `head` 单独
   存在成为不可能。见 C30-3 / C30-4 / C30-5 / C30-26。
5. **`vep_executor` 防的是"计划和执行合并"。** `executed_by` 只能是 `'S42'`。
   S30 写了 `executed_by='S30'` 就意味着 S30 在跑 ffmpeg——它不产生媒体字节
   （§5.3 / T30-25）。
6. **`observed_duration_ms > 0` 是硬门。** 允许"未知"会退化成假成功（§5.3 /
   T30-13）。归档事件不带时长时 job 停在 `INGESTING`，不写候选。
7. **检查点只允许指向 `READY` / `ACCEPTED` 的候选。** SQLite 的 CHECK 不能跨表，
   所以用两条触发器兜住（方案 §5.2 没写，C30-6）。

本仓服务层一律 SQLite：`jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、
`text[]` 存 JSON 数组、`bigint` 存 INTEGER、`numeric(8,3)` 存 NUMERIC，
`->>'k'` 用 `json_extract`。`outbox` / `inbox` / `idempotency_record` 由平台 SDK
建表，这里不重复 DDL；方案 §5.2 的 `command_receipt` 与 `idempotency_record`
是同一张表（S29 的既有结论），因此不建。`job_transition` 是本地状态历史，与 S29
同构。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 生产意图 ----------
CREATE TABLE IF NOT EXISTS video_job (
  video_job_id         TEXT PRIMARY KEY,
  tenant_id            TEXT NOT NULL,
  project_id           TEXT NOT NULL,
  series_id            TEXT NOT NULL,
  episode_id           TEXT NOT NULL,
  branch_id            TEXT NOT NULL,
  shot_ref             TEXT NOT NULL,
  shot_lineage_id      TEXT NOT NULL,
  segment_no           INTEGER NOT NULL DEFAULT 1 CHECK (segment_no >= 1),
  node_id              TEXT NOT NULL,
  node_epoch_at_create INTEGER NOT NULL,
  execution_fence      INTEGER NOT NULL,

  prompt_intent        TEXT NOT NULL,
  target_duration_ms   INTEGER NOT NULL CHECK (target_duration_ms > 0),
  duration_policy      TEXT NOT NULL CHECK (duration_policy IN
    ('native_only','allow_tier_snap','allow_tier_snap_with_trim')),
  aspect_ratio         TEXT NOT NULL,
  resolution           TEXT NOT NULL,      -- {width,height}
  audio_strategy       TEXT NOT NULL CHECK (audio_strategy IN
    ('silent_then_tts','native_locked_voice','non_dialogue_native')),
  required_capabilities TEXT NOT NULL,     -- JSON 数组
  allow_trim           INTEGER NOT NULL CHECK (allow_trim IN (0,1)),

  keyframe_baseline_id TEXT,               -- null = 无首帧（文生视频，U8）
  reference_combination_hash TEXT NOT NULL,

  -- C30-8：调用方下发、已签名的那份 context 原样存下。服务端现拼的就是伪造签名。
  execution_context    TEXT NOT NULL,
  -- C30-9：generation-command.v2 的必填键，且重试/重启要重新组装命令。
  route_policy_ref     TEXT NOT NULL,
  -- C30-10：每次付费尝试必须换 variation_nonce，否则 S26 认为是同一请求。
  variation_nonce      TEXT NOT NULL,
  -- C30-7：修复幂等键的另一半；`repair_action_type` 是台账 `action` 列的唯一
  -- 出处（dispatch 时再猜一次动作类型就是编数据）。
  repair_action_id     TEXT,
  repair_action_type   TEXT CHECK (repair_action_type IS NULL OR
    repair_action_type IN ('regenerate_video','split_shot_regenerate')),
  strategy_class       TEXT,

  -- 对白与动作窗口：S31 / S06 的上游事实（U3 / C30-25）。NULL = 未提供，不是 0。
  dialogue_duration_ms INTEGER CHECK (dialogue_duration_ms IS NULL
                                      OR dialogue_duration_ms > 0),
  dialogue_window      TEXT,               -- {start_ms,end_ms} | null
  action_window        TEXT,               -- {start_ms,end_ms} | null
  -- 生成途中 S29 推进了检查点（F17 / T30-19）：候选落定时要据此打 stale_basis。
  keyframe_drifted     INTEGER NOT NULL DEFAULT 0
    CHECK (keyframe_drifted IN (0,1)),

  input_fingerprint    TEXT NOT NULL,
  request_hash         TEXT NOT NULL,
  idempotency_key      TEXT NOT NULL,
  route_decision_ref   TEXT,
  budget_scope_id      TEXT NOT NULL,
  deadline             TEXT NOT NULL,

  provider_job_id      TEXT,
  attempt_id           TEXT,
  business_attempt_no  INTEGER NOT NULL DEFAULT 1,
  submitted_duration_ms INTEGER,           -- = normalized_input.duration_seconds*1000
  quote_upper_bound_micro INTEGER,         -- 审计用，S23 才是权威

  state                TEXT NOT NULL,
  state_seq            INTEGER NOT NULL DEFAULT 0,
  failure_code         TEXT,
  failure_detail       TEXT,
  reconciled           INTEGER NOT NULL DEFAULT 0 CHECK (reconciled IN (0,1)),

  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  state_entered_at     TEXT NOT NULL,

  CONSTRAINT vj_state_check CHECK (state IN (
    'CREATED','PREPARING','REJECTED_LOCAL','SUBMITTED','WAITING_PROVIDER',
    'WAITING_UNKNOWN','INGESTING','READY','FAILED','RECONCILED_READY',
    'RECONCILED_FAILED','CANCELED')),
  CONSTRAINT vj_tenant_idem UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT vj_submitted_dur_positive CHECK (
    submitted_duration_ms IS NULL OR submitted_duration_ms > 0),
  CONSTRAINT vj_prompt_not_blank CHECK (length(trim(prompt_intent)) > 0),
  CONSTRAINT vj_fingerprint_len CHECK (length(input_fingerprint) = 64),
  CONSTRAINT vj_segment_identity CHECK (
    length(shot_lineage_id) > 0 AND length(trim(shot_lineage_id)) > 0)
);

-- 同一 (shot_lineage, segment, fingerprint) 只能有一个未终结 job（§4.3 / E16）。
-- `RECONCILED_READY` 必须在排除列表里：对账成功是终局结论，否则那一格会永远
-- 占着业务幂等键，之后同输入的新 job 再也建不出来（C30-1，与 S29 C29-3 同型）。
CREATE UNIQUE INDEX IF NOT EXISTS video_job_open_uniq
  ON video_job (tenant_id, shot_lineage_id, segment_no, input_fingerprint)
  WHERE state NOT IN ('READY','RECONCILED_READY','REJECTED_LOCAL','FAILED',
                      'RECONCILED_FAILED','CANCELED');

-- 同一 repair_action 只能被执行一次（§7.1 / T30-29 的相邻约束）。
CREATE UNIQUE INDEX IF NOT EXISTS video_job_repair_uniq
  ON video_job (tenant_id, repair_action_id)
  WHERE repair_action_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS video_job_shot_idx
  ON video_job (tenant_id, shot_lineage_id, segment_no, created_at DESC);
CREATE INDEX IF NOT EXISTS video_job_state_idx
  ON video_job (state, state_entered_at)
  WHERE state IN ('PREPARING','SUBMITTED','WAITING_UNKNOWN','INGESTING');
CREATE INDEX IF NOT EXISTS video_job_provider_idx
  ON video_job (provider_job_id) WHERE provider_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS video_job_ref_idx
  ON video_job (tenant_id, json_extract(shot_ref, '$.id'));

-- ---------- 关键帧基线（不可变） ----------
CREATE TABLE IF NOT EXISTS keyframe_baseline_ref (
  keyframe_baseline_id TEXT PRIMARY KEY,
  tenant_id            TEXT NOT NULL,
  candidate_id         TEXT NOT NULL,       -- S29
  checkpoint_seq       INTEGER NOT NULL,
  artifact_ref         TEXT NOT NULL,       -- S28
  content_hash         TEXT NOT NULL,
  captured_at          TEXT NOT NULL,
  CONSTRAINT kbr_artifact_is_s28 CHECK (
    json_extract(artifact_ref, '$.service_id') = 'S28'),
  CONSTRAINT kbr_hash_uniq UNIQUE (tenant_id, candidate_id, checkpoint_seq)
);

-- ---------- 时长映射（不可变） ----------
CREATE TABLE IF NOT EXISTS duration_mapping (
  duration_mapping_id   TEXT PRIMARY KEY,
  tenant_id             TEXT NOT NULL,
  video_job_id          TEXT NOT NULL REFERENCES video_job(video_job_id),
  target_duration_ms    INTEGER NOT NULL CHECK (target_duration_ms > 0),
  tier_duration_ms      INTEGER NOT NULL CHECK (tier_duration_ms > 0),
  trim_mode             TEXT NOT NULL CHECK (trim_mode IN
                          ('none','head','tail','both')),
  trim_head_ms          INTEGER NOT NULL DEFAULT 0 CHECK (trim_head_ms >= 0),
  trim_tail_ms          INTEGER NOT NULL DEFAULT 0 CHECK (trim_tail_ms >= 0),
  resulting_duration_ms INTEGER NOT NULL CHECK (resulting_duration_ms > 0),
  cost_basis            TEXT NOT NULL DEFAULT 'tier' CHECK (cost_basis = 'tier'),
  -- 哪一条策略解出的这份映射。`c2_minimal` 只在
  -- `duration_policy = allow_tier_snap_with_trim` 时出现，是 `both` 的唯一合法来源
  -- （C30-26）。它同时是"调用方显式授权裁切"在这张表里的证据。
  trim_policy           TEXT NOT NULL DEFAULT 'tail_only'
                          CHECK (trim_policy IN ('tail_only','c2_minimal')),
  dialogue_duration_ms  INTEGER,
  action_window         TEXT,
  created_at            TEXT NOT NULL,
  CONSTRAINT dm_math CHECK (
    resulting_duration_ms = tier_duration_ms - trim_head_ms - trim_tail_ms),
  -- §5.2 写的是 `resulting = target`。分支 B（`allow_tier_snap`）按定义
  -- 接受档位、不裁切，那时 `resulting = tier <> target`——两条不可能同时成立。
  -- 第二个析取项只放行"不裁且按档位交付"这一种（C30-26）：`dm_math` 已保证
  -- `trim_mode='none'` 时 `resulting = tier`，所以它不是一句可以随便写的兜底。
  CONSTRAINT dm_target_match CHECK (
    resulting_duration_ms = target_duration_ms
    OR (trim_mode = 'none' AND resulting_duration_ms = tier_duration_ms)),
  CONSTRAINT dm_both_forbidden_default CHECK (
    trim_mode <> 'both' OR trim_policy = 'c2_minimal'),
  CONSTRAINT dm_head_only_forbidden CHECK (trim_mode <> 'head')
);
CREATE UNIQUE INDEX IF NOT EXISTS dm_job_uniq ON duration_mapping (video_job_id);

-- ---------- 编辑计划（裁切意图，不是执行） ----------
CREATE TABLE IF NOT EXISTS video_edit_plan (
  edit_plan_id          TEXT PRIMARY KEY,
  tenant_id             TEXT NOT NULL,
  clip_candidate_id     TEXT NOT NULL,
  revision              INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  trim                  TEXT NOT NULL,
  speed                 TEXT,              -- 恒 null（U6）
  first_frame_handling  TEXT NOT NULL DEFAULT 'keep'
    CHECK (first_frame_handling IN ('keep','trim_to_next_iframe')),
  last_frame_handling   TEXT NOT NULL DEFAULT 'keep',
  truncates_dialogue    INTEGER NOT NULL DEFAULT 0 CHECK (truncates_dialogue IN (0,1)),
  truncates_action      INTEGER NOT NULL DEFAULT 0 CHECK (truncates_action IN (0,1)),
  executed_by           TEXT,
  executed_at           TEXT,
  created_at            TEXT NOT NULL,
  CONSTRAINT vep_executor CHECK (executed_by IS NULL OR executed_by = 'S42'),
  CONSTRAINT vep_rev_uniq UNIQUE (clip_candidate_id, revision)
);

-- ---------- 候选（不可变） ----------
CREATE TABLE IF NOT EXISTS clip_candidate (
  clip_candidate_id     TEXT PRIMARY KEY,
  tenant_id             TEXT NOT NULL,
  video_job_id          TEXT NOT NULL REFERENCES video_job(video_job_id),
  shot_lineage_id       TEXT NOT NULL,
  segment_no            INTEGER NOT NULL CHECK (segment_no >= 1),
  artifact_ref          TEXT NOT NULL,
  observed_duration_ms  INTEGER NOT NULL CHECK (observed_duration_ms > 0),
  frame_rate            NUMERIC NOT NULL CHECK (frame_rate > 0),
  duration_mapping_id   TEXT NOT NULL REFERENCES duration_mapping(duration_mapping_id),
  edit_plan_id          TEXT REFERENCES video_edit_plan(edit_plan_id),
  keyframe_baseline_id  TEXT REFERENCES keyframe_baseline_ref(keyframe_baseline_id),
  provider_job_id       TEXT NOT NULL,
  attempt_id            TEXT NOT NULL,

  stale_basis            INTEGER NOT NULL DEFAULT 0 CHECK (stale_basis IN (0,1)),
  truncates_dialogue     INTEGER NOT NULL DEFAULT 0 CHECK (truncates_dialogue IN (0,1)),
  truncates_action       INTEGER NOT NULL DEFAULT 0 CHECK (truncates_action IN (0,1)),
  dialogue_window_unverified INTEGER NOT NULL DEFAULT 0
    CHECK (dialogue_window_unverified IN (0,1)),
  requires_recheck       INTEGER NOT NULL DEFAULT 0 CHECK (requires_recheck IN (0,1)),
  requires_rehuman_decision INTEGER NOT NULL DEFAULT 0
    CHECK (requires_rehuman_decision IN (0,1)),
  repair_action_id       TEXT,
  strategy_class         TEXT,

  state                  TEXT NOT NULL CHECK (state IN ('READY','ACCEPTED','REJECTED')),
  created_at             TEXT NOT NULL,
  CONSTRAINT cc_artifact_is_s28 CHECK (
    json_extract(artifact_ref, '$.service_id') = 'S28')
);

CREATE UNIQUE INDEX IF NOT EXISTS cc_artifact_uniq
  ON clip_candidate (tenant_id, json_extract(artifact_ref, '$.id'),
                     json_extract(artifact_ref, '$.content_hash'));
CREATE INDEX IF NOT EXISTS cc_shot_idx
  ON clip_candidate (tenant_id, shot_lineage_id, segment_no, created_at DESC);
CREATE INDEX IF NOT EXISTS cc_needs_attention_idx
  ON clip_candidate (tenant_id, created_at)
  WHERE requires_recheck OR requires_rehuman_decision
     OR truncates_dialogue OR truncates_action OR stale_basis;

-- ---------- 镜头视频检查点（§2.3：不是 S08 接受指针） ----------
CREATE TABLE IF NOT EXISTS shot_video_checkpoint (
  tenant_id         TEXT NOT NULL,
  shot_lineage_id   TEXT NOT NULL,
  segment_no        INTEGER NOT NULL CHECK (segment_no >= 1),
  clip_candidate_id TEXT NOT NULL REFERENCES clip_candidate(clip_candidate_id),
  checkpoint_seq    INTEGER NOT NULL CHECK (checkpoint_seq >= 1),
  expected_revision INTEGER NOT NULL DEFAULT 0,
  updated_by_job    TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, shot_lineage_id, segment_no)
);

-- 方案 §5.2 的 DDL 没有这两条触发器。没有它们，检查点可以指向一个 REJECTED
-- 候选——那就成了"下游该拿的片段"指向不存在的东西（C30-6）。
CREATE TRIGGER IF NOT EXISTS svc_candidate_ready_ins
BEFORE INSERT ON shot_video_checkpoint
WHEN (SELECT state FROM clip_candidate
      WHERE clip_candidate_id = NEW.clip_candidate_id) NOT IN ('READY','ACCEPTED')
BEGIN
  SELECT RAISE(ABORT, 'checkpoint_candidate_not_ready');
END;

CREATE TRIGGER IF NOT EXISTS svc_candidate_ready_upd
BEFORE UPDATE OF clip_candidate_id ON shot_video_checkpoint
WHEN (SELECT state FROM clip_candidate
      WHERE clip_candidate_id = NEW.clip_candidate_id) NOT IN ('READY','ACCEPTED')
BEGIN
  SELECT RAISE(ABORT, 'checkpoint_candidate_not_ready');
END;

-- ---------- 意图台账（只追加，不是费用台账） ----------
CREATE TABLE IF NOT EXISTS video_intent_ledger (
  entry_id         TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  video_job_id     TEXT NOT NULL,
  attempt_id       TEXT,
  provider_job_id  TEXT,
  shot_lineage_id  TEXT NOT NULL,
  segment_no       INTEGER NOT NULL,    -- 只作审计列，不进任何上限键（§8.1）
  action           TEXT NOT NULL CHECK (action IN
                     ('generate','regenerate_video','split_shot_regenerate')),
  strategy_class   TEXT,
  repair_action_id TEXT,
  run_id           TEXT NOT NULL,       -- 仅审计归因，不进任何上限键
  node_epoch       INTEGER NOT NULL,
  manifest_id      TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS vil_shot_idx
  ON video_intent_ledger (tenant_id, shot_lineage_id, created_at DESC);

-- ---------- 状态历史（只追加） ----------
CREATE TABLE IF NOT EXISTS job_transition (
  video_job_id  TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  from_state    TEXT,
  to_state      TEXT NOT NULL,
  reason_code   TEXT,
  detail        TEXT,
  at            TEXT NOT NULL,
  PRIMARY KEY (video_job_id, seq)
);
"""

# ---------------------------------------------------------------- 词汇表

JOB_STATES = frozenset({
    "CREATED", "PREPARING", "REJECTED_LOCAL", "SUBMITTED", "WAITING_PROVIDER",
    "WAITING_UNKNOWN", "INGESTING", "READY", "FAILED", "RECONCILED_READY",
    "RECONCILED_FAILED", "CANCELED",
})

# §3.2 的终态列表漏了 `RECONCILED_READY`，但 §3.1 / §4.6 把它当对账成功的终局；
# 这里按终态实现（C30-2）。
TERMINAL_STATES = frozenset({
    "READY", "RECONCILED_READY", "REJECTED_LOCAL", "FAILED",
    "RECONCILED_FAILED", "CANCELED",
})

RETRYABLE_STATES = frozenset({"FAILED", "RECONCILED_FAILED"})

# §3.1 表。`None` 是创建（无前置状态）。
#
# `PREPARING → FAILED` 不在 §3.1 的表里，但 §4.7 F5 / T30-04 要求派单前的
# 时长断言失败落 `FAILED(SPEC_ASSERTION)`（C30-29）。只有那条内部断言路径能
# 走这条边：供应商事件在 `PREPARING` 到达时一律视为"被重试取代的 attempt"，
# 不改状态（C30-30）。
TRANSITIONS = {
    None: frozenset({"CREATED"}),
    "CREATED": frozenset({"PREPARING", "CANCELED"}),
    "PREPARING": frozenset({"REJECTED_LOCAL", "SUBMITTED", "FAILED",
                            "CANCELED"}),
    "SUBMITTED": frozenset({"WAITING_PROVIDER", "FAILED", "WAITING_UNKNOWN",
                            "CANCELED"}),
    "WAITING_PROVIDER": frozenset({"INGESTING", "FAILED", "WAITING_UNKNOWN",
                                   "CANCELED"}),
    "INGESTING": frozenset({"INGESTING", "READY", "FAILED", "CANCELED"}),
    # §4.5：UNKNOWN 期间不发起 cancel，也不是可取消状态。这里刻意没有 CANCELED。
    "WAITING_UNKNOWN": frozenset({"RECONCILED_READY", "RECONCILED_FAILED"}),
    "READY": frozenset(),
    "RECONCILED_READY": frozenset(),
    "REJECTED_LOCAL": frozenset(),
    "FAILED": frozenset(),
    "RECONCILED_FAILED": frozenset(),
    "CANCELED": frozenset(),
}

DURATION_POLICIES = frozenset({
    "native_only", "allow_tier_snap", "allow_tier_snap_with_trim",
})

AUDIO_STRATEGIES = frozenset({
    "silent_then_tts", "native_locked_voice", "non_dialogue_native",
})

TRIM_MODES = frozenset({"none", "head", "tail", "both"})

LEDGER_ACTIONS = frozenset({
    "generate", "regenerate_video", "split_shot_regenerate",
})

CANDIDATE_STATES = frozenset({"READY", "ACCEPTED", "REJECTED"})

# §4.7 F14：允许 ±100ms 的容器封装误差（B 帧、音频 priming）。
DURATION_TOLERANCE_MS = 100

ID_PREFIX = {
    "job": "vj_",
    "baseline": "kbr_",
    "mapping": "dm_",
    "edit_plan": "vep_",
    "candidate": "cc_",
    "ledger": "vil_",
}


CONTRACT_GAPS = [
    # --- 方案内部冲突（实现时取一处，另一处登记） ---
    {"id": "C30-1", "kind": "conflict",
     "summary": "§5.2 的 `video_job_open_uniq` 排除列表没有 `RECONCILED_READY`；"
                "对账成功后那一格会永远占着业务幂等键",
     "handling": "把 `RECONCILED_READY` 加进排除列表，与 READY 同义"
                 "（与 S29 C29-3 同一处缺陷）。"},
    {"id": "C30-2", "kind": "conflict",
     "summary": "§3.2 的终态列表漏了 `RECONCILED_READY`，而 §3.1 / §4.6 把它"
                "当对账成功后的终局",
     "handling": "按终态实现。否则『任意非终态 → CANCELED』会让对账成功的 job "
                 "还能被取消。"},
    {"id": "C30-3", "kind": "conflict",
     "summary": "§4.3 说 `trim_mode = head` 默认禁止、C2 失败应 "
                "`REJECTED_LOCAL(DIALOGUE_WOULD_BE_TRUNCATED)`；§4.7 F15 与 "
                "T30-07 又说同一情形允许『候选标 `truncates_dialogue`』",
     "handling": "两条都实现，由 `duration_policy` 决定：`allow_tier_snap_with_trim` "
                 "下裁切被允许，C2 无法满足时**保留候选**并标 "
                 "`truncates_dialogue` + `requires_rehuman_decision`（F15，钱已花）；"
                 "`allow_tier_snap`（不裁）下同一情形是 `REJECTED_LOCAL"
                 "(DIALOGUE_WOULD_BE_TRUNCATED)`。两种落点都满足 T30-07 的『或』。"},
    {"id": "C30-4", "kind": "conflict",
     "summary": "`dm_target_match` 要求 `resulting_duration_ms = target`，而 F14 "
                "允许 `observed_duration_ms` 与 `tier_duration_ms` 差 ≤100ms 并按"
                "实际值建映射——两者同时成立时 tier 取哪个值没有写",
     "handling": "映射的 `tier_duration_ms` 取 **`observed_duration_ms`**（实际交付），"
                 "提交时断言的是 `submitted_duration_ms`（档位）。这样 "
                 "`dm_math` 与 `dm_target_match` 都按实际字节成立，容差内的差异"
                 "留在 `submitted_duration_ms` 与 `tier_duration_ms` 的差里，"
                 "可被 `vid_quote_vs_tier_drift_ratio` 看见。分支 B 那一格另见 "
                 "C30-26。"},
    {"id": "C30-5", "kind": "gap",
     "summary": "§5.2 的 `dm_both_forbidden_default` 只有 DB 层禁止，但 §4.3 / U5 "
                "说『放开需要显式 policy』——该 policy 的名字与位置均未定义",
     "handling": "不发明外部策略名（不臆造 `allow_both_trim`）。`both` 的唯一合法"
                 "来源收窄成调用方在 S30 自己的 API 上显式给的 "
                 "`duration_policy = allow_tier_snap_with_trim`，写入 "
                 "`trim_policy='c2_minimal'`；不是该策略时 DB 拒绝（C30-26）。"
                 "S02 若将来定义真开关，只需改这一处约束。"},

    # --- DDL 缺列（重试 / 重启 / 契约必填键） ---
    {"id": "C30-6", "kind": "gap",
     "summary": "§5.2 的 `shot_video_checkpoint` 没有任何触发器，而 §2.3 要求"
                "『检查点只能指向可用候选』",
     "handling": "加 `svc_candidate_ready_ins` / `svc_candidate_ready_upd` 两条"
                 "触发器，只允许指向 `READY` / `ACCEPTED`（与 S29 同构）。"},
    {"id": "C30-7", "kind": "gap",
     "summary": "§7.1 把 `(tenant, repair_action_id)` 定为修复幂等键，但 §5.2 的 "
                "`video_job` 没有 `repair_action_id` 列，`clip_candidate` 有",
     "handling": "在 `video_job` 加 `repair_action_id` 与 `video_job_repair_uniq`；"
                 "否则同一 `RepairAction` 重投会建出第二个付费 job。"},
    {"id": "C30-8", "kind": "gap",
     "summary": "§5.2 的 `video_job` 没有列能存已签名的 execution-context，但提交"
                "与重试都要原样回传给 S26",
     "handling": "加 `video_job.execution_context`（TEXT NOT NULL）。服务端补出来的"
                 "context 每个值都是伪造签名，不能现拼。"},
    {"id": "C30-9", "kind": "gap",
     "summary": "§5.2 的 `video_job` 只有 `route_decision_ref`，没有 "
                "`route_policy_ref`；但 `generation-command.v2` 把 "
                "`route_policy_ref` 列为 11 个必填键之一",
     "handling": "加 `video_job.route_policy_ref TEXT NOT NULL`，建 job 时把调用方"
                 "下发的那份（已校验为完整四键）原样存下。它与 "
                 "`route_decision_ref` 不是一回事：后者是 S25 派单时的决定，前者是"
                 "调用方指定的策略。不存就每次重试都拼不出合法命令。"},
    {"id": "C30-10", "kind": "gap",
     "summary": "`generation-command.v2` 强制 `variation_nonce`，§5.2 的 "
                "`video_job` 没有该列；重试若复用同一个 nonce，S26 会认为是同一"
                "请求",
     "handling": "加 `video_job.variation_nonce TEXT NOT NULL`，建 job 时生成，"
                 "每次 `business_attempt_no` 递增时换新值。"},

    # --- 契约与方案之间的口径差 ---
    {"id": "C30-11", "kind": "gap",
     "summary": "C1：`video.input.allow_trim` 是 bool，装不下 `duration_policy` "
                "三态；`native_only` 与 `allow_tier_snap` 都映射成 "
                "`allow_trim=false`",
     "handling": "S30 自己的 API 收三值；映射到契约时 `native_only` 与 "
                 "`allow_tier_snap` 都是 `allow_trim=false`，`_with_trim` 是 "
                 "`true`。`native_only` 的语义靠『`normalized_input.duration_seconds` "
                 "== target』事后判定（F4 / T30-05），因为 `require_native_duration` "
                 "这个 route policy 字段尚不存在。"},
    {"id": "C30-12", "kind": "gap",
     "summary": "C2：`artifact.ingested` 在 `event-topics.v2.json` 里只引用信封 "
                "schema；`contracts/artifact-ingested.v2.schema.json` 存在，但 "
                "`duration_ms` 与 `frame_rate_num`/`frame_rate_den` 都是**可选**",
     "handling": "按S28会报实现，字段名归一放在 `deps`：`duration_ms` → "
                 "`observed_duration_ms`，`frame_rate_num/den` → `frame_rate`。"
                 "缺任一项时 job 停在 `INGESTING` 并告警，**不写候选、不伪造时长**"
                 "（T30-13）。"},
    {"id": "C30-13", "kind": "gap",
     "summary": "C3：`expected_cost_breakdown` 有 `{generation, lipsync, qc, "
                "expected_retry, ingest}`，没有 `trim` / `post_processing`",
     "handling": "S30 不自己算成本，只记 `cost_basis: \"tier\"`；裁切成本归 "
                 "S42/S23 的实际计量。"},
    {"id": "C30-14", "kind": "gap",
     "summary": "C4：`service.yaml` 的 `depends_on` 只有 `[S26, S28, S29]`，"
                "实际还依赖 S25（路由）、S31（对白时长）、S06（shot / 节拍）",
     "handling": "按实际需要实现，但保持 `service.yaml` 不变（生成物不能手改，"
                 "AGENTS.md：改 catalog 再重新生成）。缺口在此记录。"},
    {"id": "C30-15", "kind": "gap",
     "summary": "C6：`docs/09 §4` 的降级链由 S25/S26 执行，但其中的『拆镜』需要 "
                "S30 派生多个 segment job",
     "handling": "S30 **不执行** `degradation_options` 里的任何选项，只透传；"
                 "`feasible=false` 时交 S21/S39 决策。拆镜必须由调用方新下发带 "
                 "`segment_no` 的请求（§8.2 / T30-17）。"},
    {"id": "C30-16", "kind": "gap",
     "summary": "C7：`GET /shots/{shot_lineage_id}/video-checkpoint`（S41 的主读口）"
                "不在 `service.yaml` 的 `api` 里，`contracts/openapi.v2.yaml` 也"
                "没有 video 路径",
     "handling": "HTTP 层按方案 §6.3 实现，路径即契约缺口的落点；不改契约、不改 "
                 "`service.yaml`。"},
    {"id": "C30-17", "kind": "gap",
     "summary": "C8：`event-topics.v2.json` 只为 S30 声明了 "
                "`production.video.ready`；`video.job.state-changed`、"
                "`video.duration-mismatch`、`video.clip-attention-needed` 都没有"
                "topic 与 schema",
     "handling": "前两者按方案 §6.4 以本地事件名发出（与 S29 的 "
                 "`image.job.state-changed` 同构），`video.clip-attention-needed` "
                 "与 `video.duration-mismatch` 同样本地命名；上线前需要在 "
                 "catalog / event-topics 里补声明。"},
    {"id": "C30-18", "kind": "gap",
     "summary": "`production.video.ready.v2` 根节点 `additionalProperties: false`，"
                "`flags` 只有三个键；F15/F16 要求候选带 "
                "`requires_rehuman_decision`，它进不了事件",
     "handling": "`requires_rehuman_decision` 只存在于 `clip_candidate` 表与 "
                 "`GET /clip-candidates/{id}`；事件用 `truncates_dialogue` / "
                 "`truncates_action` 两个 flag 表达同一事实（下游据任一为真即可）。"},
    {"id": "C30-19", "kind": "gap",
     "summary": "`production.video.ready.v2` 的 `duration_mapping` 只有 8 个键、"
                "`additionalProperties: false`，没有 `action_window`；"
                "`edit_plan_ref.last_frame_handling` 是自由字符串，表里也没有 CHECK",
     "handling": "`action_window` 只留在 `duration_mapping` 表（本地证据）；事件按"
                 "8 键发。`last_frame_handling` 不引入臆造的闭集，按方案保留 "
                 "`'keep'` 默认且不做枚举校验。"},
    {"id": "C30-20", "kind": "gap",
     "summary": "`production.video.ready.v2` 的 `checkpoint` 键是 `from` / `to`，"
                "与 S29 的 `from_candidate_id` / `to_candidate_id` 不同名",
     "handling": "对外只发 `from` / `to`（契约优先）；内核内部仍用 "
                 "`from_candidate_id` 表达同一事实，发布处显式改名。"},
    {"id": "C30-21", "kind": "needs_owner",
     "summary": "U1：`observed_duration_ms` 由谁提供尚未裁决（方案标『阻塞(C2)』）；"
                "S28 的 `acceptance` 没有『探测时长』这一条",
     "handling": "当前按 S28 提供实现，缺失即停在 `INGESTING`（T30-13）。不自行"
                 "探测字节（那需要拉媒体），也不调 S35（会让 READY 依赖 QC）。"},
    {"id": "C30-22", "kind": "needs_owner",
     "summary": "U5：`both` trim 的策略开关未定，需 S02 定义；目前没有任何外部"
                "policy 字段能表达『允许两头都裁』",
     "handling": "不发明开关名。当前只认调用方在 S30 API 上给的 "
                 "`duration_policy=allow_tier_snap_with_trim` 作为显式授权，"
                 "落成 `trim_policy='c2_minimal'`；DB 拒绝其它 `both`（C30-5 / "
                 "C30-26）。"},
    {"id": "C30-23", "kind": "needs_owner",
     "summary": "U6：变速 `speed` 是否属于 `VideoEditPlan` 未定；变速会改变时长"
                "语义，可能与『对白不被截断』冲突",
     "handling": "字段存在但恒为 `null`，不接受入参、不做计算。"},
    {"id": "C30-24", "kind": "needs_owner",
     "summary": "U8：关键帧缺失时是否允许文生视频未裁决（与 S29 C5 一起）",
     "handling": "当前允许（`keyframe_baseline_id` 可空），但此时必须由调用方声明"
                 "对应的文生视频能力；服务端不替调用方补 `required_capabilities`。"},
    {"id": "C30-25", "kind": "gap",
     "summary": "方案 §5.2 的 `video_job` DDL 没有 `dialogue_window`（C2 只看区间，"
                "`dialogue_duration_ms` 是长度，无法判『对白是否落在裁切窗内』）"
                "与 `keyframe_drifted`（F17/T30-19 要求候选落定时知道生成途中基线"
                "是否漂移过，晚到的事件判不出来）",
     "handling": "两个本地列：`dialogue_window` 存 `{start_ms,end_ms}` 的 JSON 或 "
                 "NULL（NULL = 上游没给，不是 0）；`keyframe_drifted` 由 "
                 "`production.image.ready` 在本 job 非终态时置 1，只在候选落定时读，"
                 "不进任何对外契约。事件里仍只发契约声明的键（C30-18/C30-19）。"},
    {"id": "C30-26", "kind": "gap",
     "summary": "§5.2 的 `duration_mapping` 只有 `dm_math` + `dm_target_match` "
                "（`resulting = target`），无法表达 §4.2 分支 B（`allow_tier_snap` "
                "接受档位、不裁切，`resulting = tier <> target`）；DDL 也没有任何"
                "列能说明『这份映射是调用方授权裁切解出来的』，于是 `both` 的"
                "合法性在库里无法判定",
     "handling": "加 `trim_policy TEXT NOT NULL DEFAULT 'tail_only'` "
                 "（`'tail_only' | 'c2_minimal'`），并给 `dm_target_match` 增加第二个"
                 "析取项 `trim_mode='none' AND resulting = tier`——它只放行分支 B，"
                 "`dm_math` 仍然管住算术。`both` 只允许 `trim_policy='c2_minimal'`，"
                 "而服务层只在 `duration_policy=allow_tier_snap_with_trim` 时写该值；"
                 "另加 `dm_head_only_forbidden`（`trim_mode <> 'head'`），与 §4.3 "
                 "『head 默认禁止』一致——head 只在 `both` 里作为 `c2_minimal` 的"
                 "一半出现。三处都只放宽/收窄本服务自己的表，不改对外契约。"},
    {"id": "C30-27", "kind": "gap",
     "summary": "§2.4 的伪代码把 `resolution_source: \"s25_normalized_input\"` 写进"
                "返回的时长映射，但（a）它不在 §2.4 自己的五个必填字段里，（b）"
                "§5.2 的 `duration_mapping` DDL 没有这一列，（c）"
                "`production.video.ready.v2` 的 `duration_mapping` 是 8 键 + "
                "`additionalProperties: false`，发不出去。仓内仅此一处出现",
     "handling": "不新增列、不发事件，也不把它算进五个必填字段。理由是它的值在当前"
                 "唯一实现里恒为常量 `'s25_normalized_input'`——S30 的时长全部来自 "
                 "S25 的 `normalized_input`，写成列等于把常量存一遍；一旦有第二个"
                 "来源（例如 S28 实测反推）再补列才有信息量。`GET "
                 "/video-jobs/{id}` 的映射块里以同名字段回显该常量，保持方案 §2.4 "
                 "的读形，不落库。"},
    {"id": "C30-28", "kind": "needs_owner",
     "summary": "文生视频（U8 / C30-24）允许创建 `keyframe_baseline_id IS NULL` 的"
                "合法 job，但 `production.video.ready.v2` 的 `keyframe_baseline` "
                "不可空（`candidate_id` minLength 1、`checkpoint_seq` minimum 1），"
                "且 `additionalProperties: false`。S30 没有任何取值能填出这个必填对象",
     "handling": "不回滚、不补造。钱已经花在一条有效候选上（它是一份有权属的库），"
                 "所以只写入 `clip_candidate` + `shot_video_checkpoint`，"
                 "**不发 `production.video.ready`**：在落库结果里回 "
                 "`ready_published: false`、"
                 "`ready_blocked_by: \"KEYFRAME_BASELINE_ABSENT\"`、"
                 "`contract_gap: \"C30-28\"`。既不用一份假的基线去骗过必填校验，"
                 "也不把已付费的产物从库里回滚掉。在 owner 裁决前，"
                 "文生视频的消费方只能直接读 `shot_video_checkpoint`，并应视为"
                 "“事件缺一份”而不是“任务失败”。"},
    {"id": "C30-29", "kind": "conflict",
     "summary": "§4.7 F5 / T30-04 要求派单前的时长断言失败落 "
                "`FAILED(SPEC_ASSERTION)`，而 §3.1 的转移表没有 "
                "`PREPARING → FAILED` 这条边（`PREPARING` 的三条出边是 "
                "`REJECTED_LOCAL` / `SUBMITTED` / `CANCELED`）",
     "handling": "把 `FAILED` 加进 `TRANSITIONS['PREPARING']`。理由：这条边只有 "
                 "`_fail_spec_assertion` 会走，而 F5 又要求必须留一个可巡检的终态 "
                 "job；不加快这条边，唯一实现 F5 的方式是让 job 停在 `PREPARING` "
                 "假装还在派单——那会让巡检永远等不到终态，也会让 "
                 "`video_job_open_uniq` 一直占着业务幂等键。这是方案内部冲突"
                 "（与 C30-3 同类），没有动任何对外契约。"},
    {"id": "C30-30", "kind": "gap",
     "summary": "§3.1 只描述每个 job 状态*应当*接收哪一条供应商状态，没有规定"
                "乱序 / 迟到 / 换过 event id 的重复投递该怎么处理。S26 的 "
                "`state` 是自由字符串，且 `attribution` 换 retry 时同一 "
                "`provider_job_id` 会继续发状态，所以 `CREATED` / `PREPARING` / "
                "`SUBMITTED` / `INGESTING` 都可能收到“下一阶段”或“上一阶段”的事件",
     "handling": "消费者一律不改状态、不抛断言、不重投，返回 `ignored` 并带 "
                 "`reason`：`SUBMITTED`/`CREATED`/`PREPARING` 收到成功类事件时 "
                 "`provider_state_arrived_before_job_was_submitted`；`INGESTING` "
                 "收到 `SUBMISSION_UNKNOWN`/`RECONCILING` 时 "
                 "`unknown_state_after_ingest_started`；`CREATED`/`PREPARING` 收到 "
                 "失败类事件时 `job_not_yet_submitted`（没提交就没有可失败的钱）。"
                 "终态 job 的迟到事件仍按 `job_already_terminal` 忽略。"
                 "让任何一条无害的迟到事件把消费者打成 500、进而无限重投，"
                 "比丢弃它危险得多。"},
    {"id": "C30-31", "kind": "gap",
     "summary": "`cc_artifact_uniq` 只保证同一份字节对应一个候选，没有规定"
                "『候选已存在、且检查点已经指向它』时该怎么回应换过 event id 的"
                "重复 `artifact.ingested`。§7.2 只说 CAS，没有幂等出口",
     "handling": "实现按 `checkpoint_already_points_at_candidate` 停住——不推进 "
                 "`checkpoint_seq`、不重发 `production.video.ready`，返回 "
                 "`checkpoint_advanced: false` 与 "
                 "`reason: checkpoint_already_points_at_candidate`。否则重放会造出 "
                 "`checkpoint: {from: X, to: X}` 这种并不存在的 revision，下游会"
                 "以为镜头动过（T30-38）。这条与 `docs/04` 的『无条件终态不被"
                 "旧事件倒退』同向，但那边没有覆盖『不前进也不后退』这一格。"},
]
