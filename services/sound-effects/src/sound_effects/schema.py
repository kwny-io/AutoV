"""S33 自己的 schema。表来源是 `plans/S33-sound-effects.md` §5。

五条结构性事实，它们决定了这张库能证明什么、不能证明什么：

1. **`cue_placement_cache` 不是权威。** 它没有 `PRIMARY KEY` 意义上的业务身份，
   主键是 `(tenant, take, edl_revision)`——一个纯派生量。整张表可以 TRUNCATE，
   任何对外读取都必须现算（§4.2 / §5.2 行 1010）。把位置写进这张表再读出来
   回答"音效在第几毫秒"，就是让缓存变成了第二权威。
2. **`content_fingerprint` 不含时长。** `sha256(cue_intent || sfx_family ||
   loopable_required)`（§4.4）。时长进指纹的直接后果是剪辑动一下就重生成整集
   音效——正是本服务要防的那笔钱（§0）。
3. **替代必须有人授权。** `sfj_substitute_paired` 把"自动找个差不多的"从数据层
   堵死：`substitute_take_id` 与 `substitution_authorized_by` 要么都空要么都在
   （§4.6 第 2 条不可自动检验）。
4. **缺口不是失败。** `sfx_gap.state` 只有 `open/closed/waived`，且
   `sg_state_consistent` 保证"说已闭合"必有闭合依据（§4.6 / §5.3）。
   `GAP_RECORDED` 的 job 必须与它的 gap 同事务写入（§3.3）。
5. **空的许可集合是"什么都不能用"，不是"没有限制"。** `rpb_kinds_present`
   在构造 `RightsPolicyBinding` 时直接拒绝空集合（§5.3 行 1054）。

本仓服务层一律 SQLite，所以方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC
字符串、`text[]` 存 JSON 数组、`uuid` 存 TEXT，`->>'k'` 用 `json_extract`。
`outbox` / `inbox` / `idempotency_record` 由平台 SDK 建表，这里不重复 DDL；
方案 §5.2 的 `command_receipt` 与 `idempotency_record` 是同一张表，因此不建。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- cue 意图版本（不可变） ----------
-- 一行 = 一条 cue 意图的一个版本。`intent_hash` 唯一索引让同一 cue 的同一意图
-- 不会因为重复提交长出第二个版本号。
CREATE TABLE IF NOT EXISTS sfx_cue_revision (
  tenant_id        TEXT NOT NULL,
  cue_id           TEXT NOT NULL,
  cue_revision     INTEGER NOT NULL CHECK (cue_revision >= 1),
  shot_lineage_id  TEXT NOT NULL,
  cue_index        INTEGER NOT NULL CHECK (cue_index >= 0),
  beat_ref         TEXT,
  sfx_family       TEXT NOT NULL CHECK (sfx_family IN
                     ('foley','ambience','hard_effect','ui','stinger')),
  cue_intent       TEXT NOT NULL,
  intent_hash      TEXT NOT NULL,
  anchor           TEXT NOT NULL CHECK (anchor IN
                     ('shot_start','shot_end','beat','absolute')),
  anchor_offset_ms INTEGER NOT NULL DEFAULT 0,
  loopable_required INTEGER NOT NULL DEFAULT 0 CHECK (loopable_required IN (0,1)),
  source_ref       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, cue_id, cue_revision),
  CONSTRAINT scr_intent_not_blank CHECK (length(trim(cue_intent)) > 0),
  CONSTRAINT scr_hash_len CHECK (length(intent_hash) = 64),
  -- anchor='beat' 但没有 beat_ref，placement 无从计算（§5.3）。
  CONSTRAINT scr_beat_anchor CHECK (anchor <> 'beat' OR beat_ref IS NOT NULL),
  CONSTRAINT scr_source_known
    CHECK (json_extract(source_ref, '$.service_id') IN ('S06','S07'))
);

CREATE UNIQUE INDEX IF NOT EXISTS scr_intent_uniq
  ON sfx_cue_revision (tenant_id, cue_id, intent_hash);
CREATE INDEX IF NOT EXISTS scr_shot_idx
  ON sfx_cue_revision (tenant_id, shot_lineage_id, cue_index);

-- ---------- 许可策略快照（不可变） ----------
CREATE TABLE IF NOT EXISTS rights_policy_binding (
  rights_policy_binding_id TEXT PRIMARY KEY,
  tenant_id            TEXT NOT NULL,
  policy_ref           TEXT NOT NULL,
  allowed_license_kinds TEXT NOT NULL,
  attribution_required INTEGER NOT NULL CHECK (attribution_required IN (0,1)),
  territory_restrictions TEXT NOT NULL DEFAULT '[]',
  blocked_families     TEXT NOT NULL DEFAULT '[]',
  expires_at           TEXT,
  resolved_at          TEXT NOT NULL,
  CONSTRAINT rpb_ref_known
    CHECK (json_extract(policy_ref, '$.service_id') = 'S02'),
  CONSTRAINT rpb_hash_len CHECK (length(json_extract(policy_ref, '$.content_hash')) = 64),
  -- 空集合 = 没有任何许可可用。严格读（§5.3），构造时就拒。
  CONSTRAINT rpb_kinds_present CHECK (json_array_length(allowed_license_kinds) > 0)
);

CREATE INDEX IF NOT EXISTS rpb_policy_idx
  ON rights_policy_binding (tenant_id, json_extract(policy_ref, '$.id'),
                            json_extract(policy_ref, '$.revision'));

-- ---------- 生成意图 ----------
CREATE TABLE IF NOT EXISTS sfx_job (
  sfx_job_id         TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  project_id         TEXT NOT NULL,
  series_id          TEXT NOT NULL,
  episode_id         TEXT NOT NULL,
  branch_id          TEXT NOT NULL,

  cue_id             TEXT NOT NULL,
  cue_revision       INTEGER NOT NULL,
  shot_lineage_id    TEXT NOT NULL,
  cue_index          INTEGER NOT NULL,
  sfx_family         TEXT NOT NULL,

  requested_duration_ms INTEGER NOT NULL CHECK (requested_duration_ms > 0),
  loopable_required  INTEGER NOT NULL CHECK (loopable_required IN (0,1)),

  -- **可空**，只为容纳 POLICY_BLOCKED。方案 §5.2 写 NOT NULL，T33-08 又要求
  -- "空 allowed_license_kinds → DB 拒绝构造 binding → POLICY_BLOCKED"：
  -- binding 构造不出来，job 就不可能引用它。两句话唯一兼容的读法是
  -- "POLICY_BLOCKED 可以没有 binding，其余状态必须有"，由下面的
  -- sfj_binding_required 补回方案本职的那条不变量。
  rights_policy_binding_id TEXT
    REFERENCES rights_policy_binding (rights_policy_binding_id),

  -- 冻结：不含时长（§4.4）。
  content_fingerprint TEXT NOT NULL,

  substitute_take_id     TEXT,
  substitution_authorized_by TEXT,

  node_id            TEXT NOT NULL,
  node_epoch_at_create INTEGER NOT NULL,
  execution_fence    INTEGER NOT NULL,

  request_hash       TEXT NOT NULL,
  -- 命令级幂等键：一条命令最多落一个 job（§7.1）。
  idempotency_key    TEXT NOT NULL,
  -- 意图级幂等键：`(tenant, shot, cue_index, fingerprint, duration)`。
  -- 只在 state 未终结时唯一（下面的 sfx_job_open_uniq），所以它不能进
  -- sfj_tenant_idem —— 那条普通唯一约束会把"终结后同意图重提"也挡掉。
  business_key       TEXT NOT NULL,
  variation_nonce    TEXT NOT NULL,

  route_decision_ref TEXT,
  budget_scope_id    TEXT NOT NULL,
  deadline           TEXT NOT NULL,

  provider_job_id    TEXT,
  attempt_id         TEXT,
  business_attempt_no INTEGER NOT NULL DEFAULT 1,
  quote_upper_bound_micro INTEGER,

  state              TEXT NOT NULL,
  state_seq          INTEGER NOT NULL DEFAULT 0,
  failure_code       TEXT,
  failure_detail     TEXT,
  reconciled         INTEGER NOT NULL DEFAULT 0 CHECK (reconciled IN (0,1)),

  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  state_entered_at   TEXT NOT NULL,

  CONSTRAINT sfj_state_check CHECK (state IN (
    'CREATED','CHECKING_POLICY','POLICY_BLOCKED','PREPARING','REJECTED_LOCAL',
    'SUBMITTED','WAITING_PROVIDER','WAITING_UNKNOWN','INGESTING','READY',
    'FAILED','RECONCILED_READY','RECONCILED_FAILED','GAP_RECORDED','CANCELED')),
  CONSTRAINT sfj_tenant_idem UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT sfj_fingerprint_len CHECK (length(content_fingerprint) = 64),
  -- 方案 §5.2 的 NOT NULL 本想保证"任何花过钱/将要花钱的 job 都有许可依据"。
  -- 只有 POLICY_BLOCKED 那条路全程没有 binding（T33-08：binding 构造不出来），
  -- 它经过的三个状态因此都必须允许空：
  --   CREATED / CHECKING_POLICY —— 走这条路时它们只存在于 POLICY_BLOCKED 之前；
  --   POLICY_BLOCKED             —— 终点。
  -- 一旦 job 有 binding，从 CREATED 起就带着（`_bind_rights` 先于 INSERT）。
  -- 因此这条 CHECK 真正拦住的是"没有许可依据的 job 往 PREPARING 及以后走"，
  -- 也就是钱花出去之前那一刻 —— 这正是方案那条 NOT NULL 想守住的东西。
  CONSTRAINT sfj_binding_required CHECK (
    rights_policy_binding_id IS NOT NULL
    OR state IN ('CREATED', 'CHECKING_POLICY', 'POLICY_BLOCKED')),
  -- 替代必须同时带授权人（§4.6）。这条把"自动找一个差不多的"堵死。
  CONSTRAINT sfj_substitute_paired CHECK (
    (substitute_take_id IS NULL AND substitution_authorized_by IS NULL)
    OR (substitute_take_id IS NOT NULL AND substitution_authorized_by IS NOT NULL))
);

-- 业务幂等：同一内容 + 同一时长需求并发双提交只留一个未终结 job（§7.1）。
-- 键里含 requested_duration_ms —— 内容相同但时长不同不是同一次生产意图。
CREATE UNIQUE INDEX IF NOT EXISTS sfx_job_open_uniq
  ON sfx_job (tenant_id, business_key)
  WHERE state NOT IN ('READY','POLICY_BLOCKED','GAP_RECORDED','REJECTED_LOCAL',
                      'FAILED','RECONCILED_FAILED','CANCELED');

CREATE INDEX IF NOT EXISTS sfj_shot_idx
  ON sfx_job (tenant_id, shot_lineage_id, cue_index, created_at DESC);
CREATE INDEX IF NOT EXISTS sfj_state_idx
  ON sfx_job (state, state_entered_at)
  WHERE state IN ('CHECKING_POLICY','PREPARING','SUBMITTED',
                  'WAITING_UNKNOWN','INGESTING');
CREATE INDEX IF NOT EXISTS sfj_episode_idx
  ON sfx_job (tenant_id, episode_id, state);

-- ---------- 音效素材 ----------
CREATE TABLE IF NOT EXISTS sfx_take (
  sfx_take_id        TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  sfx_job_id         TEXT NOT NULL REFERENCES sfx_job (sfx_job_id),
  cue_id             TEXT NOT NULL,
  shot_lineage_id    TEXT NOT NULL,
  cue_index          INTEGER NOT NULL,
  sfx_family         TEXT NOT NULL,

  artifact_ref       TEXT NOT NULL,
  natural_duration_ms INTEGER NOT NULL CHECK (natural_duration_ms > 0),
  sample_rate_hz     INTEGER NOT NULL CHECK (sample_rate_hz >= 8000),
  channel_count      INTEGER NOT NULL CHECK (channel_count >= 1),
  container_format   TEXT NOT NULL,

  content_fingerprint TEXT NOT NULL,

  loopable           INTEGER NOT NULL CHECK (loopable IN (0,1)),
  loopable_verified  INTEGER NOT NULL DEFAULT 0 CHECK (loopable_verified IN (0,1)),

  rights_policy_binding_id TEXT NOT NULL
    REFERENCES rights_policy_binding (rights_policy_binding_id),
  rights_state       TEXT NOT NULL CHECK (rights_state IN
                       ('usable','void','unknown')),
  rights_checked_at  TEXT,

  provider_job_id    TEXT NOT NULL,
  attempt_id         TEXT NOT NULL,
  source_kind        TEXT NOT NULL CHECK (source_kind IN
                       ('generated','retrieved','substituted')),

  state              TEXT NOT NULL CHECK (state IN
                       ('READY','ACCEPTED','SUPERSEDED')),
  created_at         TEXT NOT NULL,

  -- 供应商直链不能进 artifact_ref（T33-38 / E01）。
  CONSTRAINT st_artifact_is_s28
    CHECK (json_extract(artifact_ref, '$.service_id') = 'S28'),
  CONSTRAINT st_hash_len
    CHECK (length(json_extract(artifact_ref, '$.content_hash')) = 64),
  CONSTRAINT st_fingerprint_len CHECK (length(content_fingerprint) = 64),
  -- 声明不可循环却标"已验循环"是无意义的（§5.3）。
  CONSTRAINT st_loop_verified CHECK (loopable_verified = 0 OR loopable = 1)
);

-- 方案 §5.2 写的是**不带条件**的 `(tenant, artifact_id, content_hash)`。
-- 那条会把 §4.6 的替代路径整个堵死：替代按定义复用来源 take 的 `artifact_ref`，
-- T33-29 又要求它为这条 cue 落一条 `source_kind='substituted'` 的新行，
-- 两行必然同 artifact，直接撞唯一约束。
-- 这条索引真正防的是**重复消费同一次 ingest**（同一条 `production.sfx.ready`
-- 或 `artifact.ingested` 被投两遍 → 行两遍）：这正是 E32 幂等消费要守的东西。
-- 替代行不经 ingest 产生，所以它不在这个守卫的射程里。收窄成 partial。
CREATE UNIQUE INDEX IF NOT EXISTS st_artifact_uniq
  ON sfx_take (tenant_id, json_extract(artifact_ref, '$.id'),
               json_extract(artifact_ref, '$.content_hash'))
  WHERE source_kind <> 'substituted';
CREATE INDEX IF NOT EXISTS st_cue_idx
  ON sfx_take (tenant_id, shot_lineage_id, cue_index, created_at DESC);
CREATE INDEX IF NOT EXISTS st_reuse_idx
  ON sfx_take (tenant_id, content_fingerprint)
  WHERE rights_state = 'usable' AND state IN ('READY','ACCEPTED');

-- ---------- 音效检查点 ----------
CREATE TABLE IF NOT EXISTS sfx_checkpoint (
  tenant_id       TEXT NOT NULL,
  shot_lineage_id TEXT NOT NULL,
  cue_index       INTEGER NOT NULL,
  sfx_take_id     TEXT NOT NULL REFERENCES sfx_take (sfx_take_id),
  checkpoint_seq  INTEGER NOT NULL,
  expected_revision INTEGER NOT NULL DEFAULT 0,
  updated_by_job  TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, shot_lineage_id, cue_index)
);

-- ---------- 缺口记录 ----------
CREATE TABLE IF NOT EXISTS sfx_gap (
  gap_id          TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  shot_lineage_id TEXT NOT NULL,
  cue_index       INTEGER NOT NULL,
  cue_id          TEXT NOT NULL,
  sfx_family      TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN (
    'no_licensed_route','provider_failed','submission_unrecoverable',
    'duration_mismatch','rights_lapsed','artifact_unavailable',
    'family_unavailable','no_substitute_authorized','loopability_unverified')),
  detail          TEXT NOT NULL,
  source_job_id   TEXT,
  state           TEXT NOT NULL CHECK (state IN ('open','closed','waived')),
  closed_by_take_id TEXT,
  waived_decision_id TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  -- 说"已闭合"必有闭合依据；说"豁免"必有裁决引用（T33-32）。
  CONSTRAINT sg_state_consistent CHECK (
    (state = 'open'   AND closed_by_take_id IS NULL AND waived_decision_id IS NULL)
    OR (state = 'closed' AND closed_by_take_id IS NOT NULL)
    OR (state = 'waived' AND waived_decision_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS sg_open_idx
  ON sfx_gap (tenant_id, shot_lineage_id, cue_index)
  WHERE state = 'open';
CREATE INDEX IF NOT EXISTS sg_episode_idx
  ON sfx_gap (tenant_id, cue_id, state);

-- ---------- placement 缓存（非权威，可丢弃） ----------
CREATE TABLE IF NOT EXISTS cue_placement_cache (
  tenant_id       TEXT NOT NULL,
  sfx_take_id     TEXT NOT NULL,
  edl_revision    INTEGER NOT NULL,
  placement_mode  TEXT NOT NULL CHECK (placement_mode IN
    ('fixed','trim','stretch','loop','unplaceable')),
  output_start_ms INTEGER,
  output_end_ms   INTEGER,
  source_in_ms    INTEGER,
  source_out_ms   INTEGER,
  stretch_ratio   REAL,
  reason          TEXT,
  computed_at     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, sfx_take_id, edl_revision)
);
-- 这张表可以被 TRUNCATE，不影响任何正确性（§4.2 / T33-49）。

-- ---------- 生产意图台账 ----------
-- 只在真正提交 S26 并产生付费意图时写（§8.1）。复用/裁切/重算/记缺口都不写。
-- 不构造 strategy_class：音效不在任何动作族里（§8.2）。
CREATE TABLE IF NOT EXISTS sfx_intent_ledger (
  entry_id        TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  sfx_job_id      TEXT NOT NULL,
  shot_lineage_id TEXT NOT NULL,
  cue_index       INTEGER NOT NULL,
  action          TEXT NOT NULL CHECK (action IN ('generate','fill_gap')),
  sfx_family      TEXT NOT NULL,
  run_id          TEXT,
  node_epoch      INTEGER NOT NULL,
  manifest_id     TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sil_shot_idx
  ON sfx_intent_ledger (tenant_id, shot_lineage_id, created_at DESC);

-- ---------- 状态转移历史 ----------
CREATE TABLE IF NOT EXISTS job_transition (
  sfx_job_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  from_state  TEXT,
  to_state    TEXT NOT NULL,
  reason_code TEXT,
  detail      TEXT,
  at          TEXT NOT NULL,
  PRIMARY KEY (sfx_job_id, seq)
);
"""

# ---------------------------------------------------------------- 闭集与策略

# §2.3 行 208：契约里没有闭集（C1），本服务内部定五类，且**不从 cue_intent 猜**。
SFX_FAMILIES = ("foley", "ambience", "hard_effect", "ui", "stinger")

ANCHORS = ("shot_start", "shot_end", "beat", "absolute")

# §4.2 行 412–418。`ambience` / `ui` 是 1.0：它们不用拉伸（用循环 / 长度是设计定的），
# 所以第 3 行的判据对它们恒不成立。
MAX_STRETCH = {
    "foley": 2.0,
    "ambience": 1.0,
    "hard_effect": 1.15,
    "ui": 1.0,
    "stinger": 1.1,
}

# §4.2 第 2 行「族允许裁切」。裁切是丢尾巴（或按 anchor 丢头），对点事件和
# 长底噪都安全；`ui` 例外——界面音效的长度是设计定的，裁短就换了一个音。
TRIM_ALLOWED = {
    "foley": True,
    "ambience": True,
    "hard_effect": True,
    "ui": False,
    "stinger": True,
}

GAP_KINDS = (
    "no_licensed_route", "provider_failed", "submission_unrecoverable",
    "duration_mismatch", "rights_lapsed", "artifact_unavailable",
    "family_unavailable", "no_substitute_authorized", "loopability_unverified",
)

JOB_STATES = (
    "CREATED", "CHECKING_POLICY", "POLICY_BLOCKED", "PREPARING",
    "REJECTED_LOCAL", "SUBMITTED", "WAITING_PROVIDER", "WAITING_UNKNOWN",
    "INGESTING", "READY", "FAILED", "RECONCILED_READY", "RECONCILED_FAILED",
    "GAP_RECORDED", "CANCELED",
)

TERMINAL_STATES = frozenset({
    "READY", "POLICY_BLOCKED", "GAP_RECORDED", "REJECTED_LOCAL", "FAILED",
    "RECONCILED_FAILED", "CANCELED",
})

# 可以 `:retry` 的只有两种（§6.3）：POLICY_BLOCKED 要等策略变，
# WAITING_UNKNOWN 必须先对账，GAP_RECORDED 是"这条 cue 缺了"的事实不是可重试的失败。
RETRYABLE_STATES = frozenset({"FAILED", "RECONCILED_FAILED"})

# T33-06 / E35：上限是 DoS 防御，不是产品约束。契约只给了 exclusiveMinimum: 0。
MAX_DURATION_SECONDS = 600.0

ID_PREFIX = {
    "job": "sfj_",
    "take": "st_",
    "gap": "sgp_",
    "binding": "rpb_",
    "ledger": "sil_",
}

CONTRACT_GAPS = [
    "S33 C1: sound_effect.input is {cue_intent, duration_seconds, "
    "rights_policy_ref} with additionalProperties:false. It carries no "
    "sfx_family, no anchor, no loopable. This service requires all three on its "
    "own API and DISCARDS them when composing the GenerationCommand, so the "
    "provider never sees them and family/anchor/loopable reuse cannot be "
    "requested upstream. Placement correctness depends on them.",
    "S33 C2: generation-command.v2 modality has no value for retrieving an "
    "existing asset. This implementation does generation only; source_kind "
    "'retrieved' is representable in the schema but no retrieval path exists.",
    "S33 C3: duration_seconds is required and is a timeline constraint, not a "
    "creative input. This service keeps it out of content_fingerprint and caps "
    "it by the shot's available space at the requested EDL revision, so a "
    "shorter shot does not trigger a new generation.",
    "S33 C4: artifact.ingested carries duration_ms but no loopability probe. "
    "This service verifies loopability itself after ingest; a take that "
    "declares loopable but fails verification is voided and gapped "
    "(loopability_unverified).",
    "S33 C5: there is no defined S02 read endpoint that resolves a "
    "rights_policy_ref into allowed_license_kinds. The rights resolver is an "
    "injected callback; unconfigured it fails 503 rather than passing the ref "
    "through to S26 and letting the provider decide compliance.",
    "S33 C6: S41's gaps[].kind closed set has no 'missing_sfx_cue'. Gaps are "
    "exposed through GET /sfx-cues?episode_id=&has_gap=true and are NOT mapped "
    "onto S41's kinds.",
    "S33 C7: service-catalog declares depends_on [S26, S28] but this service "
    "also needs S02 (rights), S25 (route decision), S41 (EDL for placement). "
    "service.yaml is generated from the catalog; not hand-edited here.",
    "S33 C9: action-families.v2 has no sfx defect_type. This service executes "
    "no action family and produces no QC defect; gaps are SfxGap records.",
]
