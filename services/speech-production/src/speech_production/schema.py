"""S31 自己的 schema。表来源是 `plans/S31-speech-production.md` §5。

六条结构性事实决定了这张库能证明什么、不能证明什么：

1. **冻结列只能靠"没有 UPDATE 路径"证明。** `speech_job.voice_binding_id` /
   `voice_binding_revision` 是 NOT NULL，且全文件没有一条 UPDATE 碰它们。
   表上没有 `fallback_voice_id` 这种列——§4.3 的"不得换一个音色"在这里是
   *写不出来*，不是"不推荐"。
2. **快照不是放行依据。** `voice_binding_ref.authorization_state` 只用于事后
   解释；放行必须由 `rights_recheck_log` 里一条 `eligible=1` 的记录背书，
   而 `rrl_unreachable_not_eligible` 保证"查不到"永远不被记成可用（E30）。
3. **`VOICE_TAKE` 的字节属于 S28。** `vt_artifact_is_s28` 拒绝任何
   `service_id <> 'S28'` 的引用：供应商直链或本地路径进不来（E01）。
4. **`source_kind='none'` 与"带词典"互斥。** `lbr_none_consistent` 让
   "用了哪版词典"永远有一个答案，不会出现"有 id 但说没用"（E02）。
5. **计数键不是 `utterance_id`。** `speech_intent_ledger` 同时存
   `shot_lineage_id`（S39 的 `AttemptCounterKey`）与 `utterance_id`（仅审计），
   最后一列**不进任何上限键**（E26）。
6. **时长只有容器实测那一个数。** `voice_take.observed_duration_ms > 0` 且
   `dialogue_span_ms` 根本不建列——S31 不缓存一个它没有权威的数字（E04/E23）。

本仓服务层一律 SQLite，所以方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC
字符串、`uuid` 存 TEXT、`->>'k'` 用 `json_extract`。`outbox` / `inbox` /
`idempotency_record` 由平台 SDK 建表，这里不重复 DDL；方案 §5.2 的
`command_receipt` 与 `idempotency_record` 是同一件事的两张写法，因此不建
（拒绝/异体由 `cr_hash_uniq` 判 409 的语义由幂等 SDK 的 `begin` 承担）。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 台词文本版本（不可变） ----------
-- 一行 = 一句台词在一个语言下的一个文本版本。同 (tenant, utterance, locale)
-- 下同一份 text_hash 只能有一个版本号，否则"第几次改"就没有答案。
CREATE TABLE IF NOT EXISTS utterance_revision (
  tenant_id            TEXT NOT NULL,
  utterance_id         TEXT NOT NULL,
  text_revision        INTEGER NOT NULL CHECK (text_revision >= 1),
  locale               TEXT NOT NULL,
  speaker_character_id TEXT NOT NULL,
  text                 TEXT NOT NULL,
  text_hash            TEXT NOT NULL,
  emotion              TEXT NOT NULL,
  shot_lineage_id      TEXT,
  source_ref           TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, utterance_id, locale, text_revision),
  CONSTRAINT ur_text_not_blank CHECK (length(trim(text)) > 0),
  CONSTRAINT ur_hash_len CHECK (length(text_hash) = 64),
  -- 文本来源只有这两个：S06 的 ShotSpec、S04 的剧本。旁白可以没有镜头。
  CONSTRAINT ur_source_known
    CHECK (json_extract(source_ref, '$.service_id') IN ('S04','S06'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ur_hash_uniq
  ON utterance_revision (tenant_id, utterance_id, locale, text_hash);
CREATE INDEX IF NOT EXISTS ur_shot_idx
  ON utterance_revision (tenant_id, shot_lineage_id, utterance_id);

-- ---------- S15 音色绑定快照（不可变） ----------
CREATE TABLE IF NOT EXISTS voice_binding_ref (
  voice_binding_ref_id     TEXT PRIMARY KEY,
  tenant_id                TEXT NOT NULL,
  voice_id                 TEXT NOT NULL,
  voice_revision           INTEGER NOT NULL CHECK (voice_revision >= 1),
  authorization_state      TEXT NOT NULL
    CHECK (authorization_state IN ('granted','suspended','revoked','expired')),
  authorization_state_seq  INTEGER NOT NULL CHECK (authorization_state_seq >= 1),
  authorization_expires_at TEXT,
  captured_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vbr_voice_idx
  ON voice_binding_ref (tenant_id, voice_id);

-- ---------- 词典绑定（不可变） ----------
CREATE TABLE IF NOT EXISTS lexicon_binding_ref (
  lexicon_binding_ref_id TEXT PRIMARY KEY,
  tenant_id              TEXT NOT NULL,
  lexicon_id             TEXT,
  lexicon_revision       INTEGER CHECK (lexicon_revision IS NULL
                                        OR lexicon_revision >= 1),
  content_hash           TEXT,
  source_kind            TEXT NOT NULL
    CHECK (source_kind IN ('explicit','voice_default','none')),
  scope_kind             TEXT CHECK (scope_kind IS NULL
                                     OR scope_kind IN ('project','series','voice')),
  scope_id               TEXT,
  captured_at            TEXT NOT NULL,
  CONSTRAINT lbr_none_consistent CHECK (
    (source_kind = 'none' AND lexicon_id IS NULL
      AND lexicon_revision IS NULL AND content_hash IS NULL)
    OR
    (source_kind <> 'none' AND lexicon_id IS NOT NULL
      AND lexicon_revision IS NOT NULL AND content_hash IS NOT NULL)),
  CONSTRAINT lbr_hash_len CHECK (content_hash IS NULL OR length(content_hash) = 64)
);

-- ---------- 合成意图 ----------
CREATE TABLE IF NOT EXISTS speech_job (
  speech_job_id        TEXT PRIMARY KEY,
  tenant_id            TEXT NOT NULL,
  project_id           TEXT NOT NULL,
  series_id            TEXT NOT NULL,
  episode_id           TEXT NOT NULL,
  branch_id            TEXT NOT NULL,

  utterance_id         TEXT NOT NULL,
  utterance_index      INTEGER NOT NULL DEFAULT 0 CHECK (utterance_index >= 0),
  shot_lineage_id      TEXT,
  locale               TEXT NOT NULL,
  text_revision        INTEGER NOT NULL CHECK (text_revision >= 1),
  -- S06 ShotSpec / S04 script 里的版本号；只用于回读解释，不参与判断。
  source_revision      INTEGER,

  voice_binding_ref_id   TEXT NOT NULL REFERENCES voice_binding_ref (voice_binding_ref_id),
  lexicon_binding_ref_id TEXT NOT NULL REFERENCES lexicon_binding_ref (lexicon_binding_ref_id),

  -- ↓ 冻结列：NOT NULL 且本服务没有任何 UPDATE 路径碰它们（§4.3）
  voice_binding_id       TEXT NOT NULL,
  voice_binding_revision INTEGER NOT NULL CHECK (voice_binding_revision >= 1),

  emotion            TEXT NOT NULL,
  output_format      TEXT NOT NULL CHECK (output_format IN ('wav','flac','mp3')),
  sample_rate_hz     INTEGER NOT NULL CHECK (sample_rate_hz >= 8000),
  require_alignment  INTEGER NOT NULL CHECK (require_alignment IN (0,1)),

  node_id              TEXT NOT NULL,
  node_epoch_at_create INTEGER NOT NULL,
  execution_fence      INTEGER NOT NULL,

  input_fingerprint  TEXT NOT NULL,
  request_hash       TEXT NOT NULL,
  idempotency_key    TEXT NOT NULL,

  repair_action_id   TEXT,
  strategy_class     TEXT,
  variation_nonce    TEXT NOT NULL,

  -- 路由决策**引用**（S25 给的那份），与 `route_decision_ref` 不是一回事：
  -- 前者是调用方下发的策略，后者是 S25 实际做出的决策。
  route_policy_ref   TEXT,
  route_decision_ref TEXT,
  budget_scope_id    TEXT NOT NULL,
  deadline           TEXT NOT NULL,

  -- 平台签名的 execution-context.v2 原文。`generation-command.v2.context` 有
  -- 18 个必填键（`actor` / `trace_id` / `home_region` 只能来自签名），重试与
  -- 重启恢复都必须重放**这一份**，现拼出来的每个值都是伪造签名。
  execution_context  TEXT NOT NULL,

  provider_job_id        TEXT,
  attempt_id             TEXT,
  business_attempt_no    INTEGER NOT NULL DEFAULT 1,
  quote_upper_bound_micro INTEGER,

  state              TEXT NOT NULL,
  state_seq          INTEGER NOT NULL DEFAULT 0,
  failure_code       TEXT,
  failure_detail     TEXT,
  reconciled         INTEGER NOT NULL DEFAULT 0 CHECK (reconciled IN (0,1)),

  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  state_entered_at   TEXT NOT NULL,

  CONSTRAINT sj_state_check CHECK (state IN (
    'CREATED','CHECKING_RIGHTS','PREPARING','REJECTED_LOCAL','RIGHTS_BLOCKED',
    'SUBMITTED','WAITING_PROVIDER','WAITING_UNKNOWN','INGESTING','READY',
    'FAILED','RECONCILED_READY','RECONCILED_FAILED','CANCELED')),
  CONSTRAINT sj_tenant_idem UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT sj_fingerprint_len CHECK (length(input_fingerprint) = 64),
  -- 同一 RepairAction 被投两次不能产生两个付费 job（§5.3 / E24）。
  CONSTRAINT sj_repair_uniq UNIQUE (tenant_id, repair_action_id)
);

-- 业务幂等：同一意图并发双提交只留一个未终结 job（§7.1 / E16）。
CREATE UNIQUE INDEX IF NOT EXISTS speech_job_open_uniq
  ON speech_job (tenant_id, utterance_id, locale, input_fingerprint)
  WHERE state NOT IN ('READY','REJECTED_LOCAL','RIGHTS_BLOCKED','FAILED',
                      'RECONCILED_FAILED','CANCELED');

CREATE INDEX IF NOT EXISTS speech_job_shot_idx
  ON speech_job (tenant_id, shot_lineage_id, utterance_id, created_at DESC);
CREATE INDEX IF NOT EXISTS speech_job_state_idx
  ON speech_job (state, state_entered_at)
  WHERE state IN ('CHECKING_RIGHTS','PREPARING','SUBMITTED',
                  'WAITING_UNKNOWN','INGESTING');
CREATE INDEX IF NOT EXISTS speech_job_attempt_idx
  ON speech_job (tenant_id, attempt_id);

-- ---------- 音频候选 ----------
CREATE TABLE IF NOT EXISTS voice_take (
  voice_take_id        TEXT PRIMARY KEY,
  tenant_id            TEXT NOT NULL,
  speech_job_id        TEXT NOT NULL REFERENCES speech_job (speech_job_id),
  utterance_id         TEXT NOT NULL,
  locale               TEXT NOT NULL,
  shot_lineage_id      TEXT,

  artifact_ref         TEXT NOT NULL,
  observed_duration_ms INTEGER NOT NULL CHECK (observed_duration_ms > 0),
  container_format     TEXT NOT NULL,
  sample_rate_hz       INTEGER NOT NULL CHECK (sample_rate_hz >= 8000),

  voice_binding_id       TEXT NOT NULL,
  voice_binding_revision INTEGER NOT NULL,
  -- 故意的反规范化：词典绑定的冗余拷贝，**只用于事后解释**，不参与判断。
  -- 判断一律走 lexicon_binding_ref（§5.2 末段）。
  lex                  TEXT NOT NULL,
  input_fingerprint    TEXT NOT NULL,

  provider_job_id      TEXT NOT NULL,
  attempt_id           TEXT NOT NULL,
  alignment_report_ref TEXT,

  rights_state         TEXT NOT NULL
    CHECK (rights_state IN ('usable','void','unknown')),
  rights_state_seq     INTEGER,
  repair_action_id     TEXT,
  strategy_class       TEXT,

  state                TEXT NOT NULL CHECK (state IN ('READY','ACCEPTED','SUPERSEDED')),
  created_at           TEXT NOT NULL,

  CONSTRAINT vt_artifact_is_s28
    CHECK (json_extract(artifact_ref, '$.service_id') = 'S28'),
  CONSTRAINT vt_hash_len
    CHECK (length(json_extract(artifact_ref, '$.content_hash')) = 64),
  -- 标 void 必须说明依据哪个授权版本，否则不可审计（§5.3）。
  CONSTRAINT vt_void_has_seq CHECK (
    rights_state <> 'void' OR rights_state_seq IS NOT NULL)
);

-- 同一次 ingest 被投两遍只能落一个 take（E32）。
CREATE UNIQUE INDEX IF NOT EXISTS vt_artifact_uniq
  ON voice_take (tenant_id, json_extract(artifact_ref, '$.id'),
                 json_extract(artifact_ref, '$.content_hash'));
CREATE INDEX IF NOT EXISTS vt_utterance_idx
  ON voice_take (tenant_id, utterance_id, locale, created_at DESC);
CREATE INDEX IF NOT EXISTS vt_reuse_idx
  ON voice_take (tenant_id, input_fingerprint)
  WHERE rights_state = 'usable' AND state IN ('READY','ACCEPTED');

-- ---------- 配音检查点 ----------
CREATE TABLE IF NOT EXISTS speech_checkpoint (
  tenant_id         TEXT NOT NULL,
  shot_lineage_id   TEXT NOT NULL,
  utterance_id      TEXT NOT NULL,
  locale            TEXT NOT NULL,
  voice_take_id     TEXT NOT NULL REFERENCES voice_take (voice_take_id),
  checkpoint_seq    INTEGER NOT NULL,
  expected_revision INTEGER NOT NULL DEFAULT 0,
  updated_by_job    TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, shot_lineage_id, utterance_id, locale)
);

-- ---------- 权利复查审计（只追加） ----------
CREATE TABLE IF NOT EXISTS rights_recheck_log (
  recheck_id         TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  speech_job_id      TEXT NOT NULL,
  voice_id           TEXT NOT NULL,
  check_point        TEXT NOT NULL
    CHECK (check_point IN ('submit_sync','worker_start','pre_dispatch',
                           'post_ingest','event_driven')),
  observed_state     TEXT NOT NULL
    CHECK (observed_state IN ('granted','suspended','revoked','expired',
                              'unreachable')),
  observed_state_seq INTEGER,
  eligible           INTEGER NOT NULL CHECK (eligible IN (0,1)),
  deny_reason        TEXT,
  checked_at         TEXT NOT NULL,
  -- **全文最重要的一条 CHECK**：S15 不可达不构成"可用"的证据（E30）。
  CONSTRAINT rrl_unreachable_not_eligible CHECK (
    observed_state <> 'unreachable' OR eligible = 0)
);
CREATE INDEX IF NOT EXISTS rrl_job_idx
  ON rights_recheck_log (tenant_id, speech_job_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS rrl_voice_idx
  ON rights_recheck_log (tenant_id, voice_id, checked_at DESC);

-- ---------- 生产意图台账（只追加） ----------
-- 只在真正向 S26 提交付费意图时写（§8.1）。复用写 0 行。
CREATE TABLE IF NOT EXISTS speech_intent_ledger (
  entry_id         TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  speech_job_id    TEXT NOT NULL,
  utterance_id     TEXT NOT NULL,
  shot_lineage_id  TEXT,
  locale           TEXT NOT NULL,
  action           TEXT NOT NULL
    CHECK (action IN ('generate','re_synthesize_speech','locale_dub')),
  strategy_class   TEXT,
  repair_action_id TEXT,
  voice_id         TEXT NOT NULL,
  run_id           TEXT,
  node_epoch       INTEGER NOT NULL,
  manifest_id      TEXT,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sil_shot_idx
  ON speech_intent_ledger (tenant_id, shot_lineage_id, created_at DESC);

-- ---------- 状态转移历史（只追加） ----------
CREATE TABLE IF NOT EXISTS job_transition (
  speech_job_id TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  from_state    TEXT,
  to_state      TEXT NOT NULL,
  reason_code   TEXT,
  detail        TEXT,
  at            TEXT NOT NULL,
  PRIMARY KEY (speech_job_id, seq)
);
"""

# ---------------------------------------------------------------- 闭集与策略

# §3.1 的 14 个状态，一字不多一字不少。
JOB_STATES = (
    "CREATED", "CHECKING_RIGHTS", "PREPARING", "REJECTED_LOCAL",
    "RIGHTS_BLOCKED", "SUBMITTED", "WAITING_PROVIDER", "WAITING_UNKNOWN",
    "INGESTING", "READY", "FAILED", "RECONCILED_READY", "RECONCILED_FAILED",
    "CANCELED",
)

# §3.1 终态。`RIGHTS_BLOCKED` 在这里，且**没有任何自动出口**。
TERMINAL_STATES = frozenset({
    "READY", "RIGHTS_BLOCKED", "REJECTED_LOCAL", "FAILED", "RECONCILED_FAILED",
    "CANCELED",
})

# §6.3：只有这两种能 `:retry`。WAITING_UNKNOWN 必须先对账（E16），
# RIGHTS_BLOCKED 只能由人或新建 voice_id 离开（E30）。
RETRYABLE_STATES = frozenset({"FAILED", "RECONCILED_FAILED"})

# §4.2 的五个复查时点，顺序即方案表格的顺序。
RECHECK_POINTS = ("submit_sync", "worker_start", "pre_dispatch",
                  "post_ingest", "event_driven")

# §4.3 / §4.7 F3–F6：权利类拒绝 → RIGHTS_BLOCKED（终态）。
RIGHTS_DENY_REASONS = frozenset({
    "AUTHORIZATION_REVOKED", "AUTHORIZATION_EXPIRED",
    "AUTHORIZATION_SUSPENDED", "USE_NOT_AUTHORIZED",
    "VOICE_TOMBSTONED",
})

# §4.7 F7/F8：技术类拒绝 → REJECTED_LOCAL（换渠道/改配置就能好）。
TECHNICAL_DENY_REASONS = frozenset({"MAPPING_UNAVAILABLE", "REGRESSION_FAILED"})

# §4.1 步 h 的闭集。
OUTPUT_FORMATS = ("wav", "flac", "mp3")

# §4.6：本服务只执行这一族。
OWNED_ACTIONS = ("re_synthesize_speech",)
OWNED_DEFECT_TYPES = frozenset({"voice"})

ID_PREFIX = {
    "job": "sj_",
    "take": "vt_",
    "utterance": "utt_",
    "vbr": "vbr_",
    "lbr": "lbr_",
    "recheck": "rrl_",
    "ledger": "sil_",
    "operation": "op_",
}

CONTRACT_GAPS = [
    "S31 C1: contracts/openapi.v2.yaml has no S31 paths. The catalog only "
    "declares POST /speech-jobs and GET /speech-jobs/{id}; every other route "
    "in api/app.py is from plan §6.3 and is not in the OpenAPI document.",
    "S31 C2: artifact.ingested carries `duration_ms` (optional, integer >= 0), "
    "not `observed_duration_ms`, and `audio_channels`, not `channel_count`. "
    "This service maps duration_ms to its internal observed duration and "
    "blocks in INGESTING when it is absent (F14) rather than inventing one.",
    "S31 C3: pronunciation_lexicon in the request has no content_hash or "
    "scope_kind, and generation-command.v2 speech input does not define their "
    "shape either. This service resolves them from S15 at binding time and "
    "drops them when composing the GenerationCommand (it sends {id, revision} "
    "only), so the provider never sees S31-only fields.",
    "S31 C4: speech.input has no voice-authorization field, so S25/S26 cannot "
    "re-check it. The five-point recheck in plan §4.2 is enforced entirely "
    "inside this service; upstream 「审核已通过」 is never treated as clearance.",
    "S31 C5: emotion is a free string with no closed set. This service only "
    "requires it non-blank and puts it in the fingerprint; it never interprets "
    "it, so a vocabulary change is not a code change here.",
    "S31 C6: production.speech.ready is the only registered event. "
    "speech.job.state-changed / speech.voice-authorization-lapsed / "
    "speech.utterance-revision.recorded are proposed by plan §6.4 and are NOT "
    "in contracts/event-topics.v2.json; they are emitted with local type names.",
    "S31 C7: service.yaml depends_on is [S15, S26, S28]; this service also "
    "needs S25 (route decision) and consumes S08/S37/S39/S41 events. "
    "strategy_class is temporarily `re_synthesize_speech:<emotion>` because "
    "S39's registry has no speech entry. service.yaml is generated; not "
    "hand-edited here.",
    "S31 C8: dialogue_duration_ms has no contract field and S30/S41 each guess "
    "it. This service publishes audio_duration_ms (container-measured, an "
    "upper bound) and dialogue_span_ms = null with source 'pending_s37'.",
    "S31 C9: output_spec has format and sample_rate_hz only; no channel count "
    "or loudness. This service defaults to mono when S28 reports no channel "
    "count and stores what S28 measured.",
    "S31 C10: production.speech-ready.v2.schema.json requires lexicon_ref "
    "{id, revision, source_kind} with additionalProperties:false, but plan "
    "§2.4 permits source_kind='none' with no lexicon at all. A schema-valid "
    "event for that case uses the LexiconBindingRef id with revision 1 and "
    "source_kind='none' — a structurally valid stand-in, not a fabricated "
    "lexicon.",
    "S31 C11: generation.job.state-changed v2 carries attempt_id and does not "
    "carry provider_job_id. This service correlates on attempt_id (stored from "
    "the S26 response) and falls back to provider_job_id when a producer does "
    "include it.",
    "S31 C12: plan T31-31 says an expired archive URL is answered by asking "
    "S26 for a new URL. S26 has start_ingest(rearchive=True) in-process but "
    "exposes no HTTP route for it, and its state-changed event carries neither "
    "provider_job_id nor any URL. S31 therefore cannot request a re-archive; "
    "it stays in INGESTING, never re-synthesises, and records the permanent "
    "gap as FAILED(ARTIFACT_UNAVAILABLE) when S26 reports INGEST_FAILED.",
]
