"""S43 自己的 schema。表来源是 `plans/S43-delivery.md` §5。

四条结构性事实：

1. **包一旦创建就不可变。** `package` 没有 `revision` 列，`item` 没有 UPDATE
   路径，`manifest` 每个包只有一行。修正 = 新 `package_id`（§1.2）。唯一的
   例外是 `state` / `gate_results` / `sealed_at` / `published_at` 这些**进度**
   字段——它们不是包内容。`DistributionAttempt` 可以多次，每次都指向同一个
   不可变包。
2. **S43 不是任何东西的第二权威。** 它不存 accepted pointer（S08 的），不存
   artifact 字节或 hash 的结论（S28 的），不存策略判定（S02 的），不存审计链
   （S45 的），不算钱（S23 的）。它只存 ref、自己的状态机、自己的签名与回执。
3. **`execution_fence` 与 `node_epoch` 是两列，永不合并。**
   `execution_fence` 属于 run/branch（来自签名过的 execution context），
   `node_epoch` 属于单个 `logical_node`（master 候选的产出坐标，仅作引用）。
   本服务不比较、不换算、不拿任何一个推进接受指针（AGENTS.md）。
4. **门是 fail-closed 的。** 依赖不可达不是 `pass`。`gate_results` 的取值是
   闭集 `pass / fail / deferred / not_applicable`；`deferred`（创建时的 G8）
   绝不能被读成通过（`contracts/delivery.package-created.v2.schema.json`
   的 PDC-3 明写）。未知依赖直接让**命令** 503，不落包。

方案里的 `jsonb` 存 TEXT（JCS）、`timestamptz` 存定宽 UTC 字符串、
`text[]` 存 JSON 数组、`char(64)` 存 TEXT、`uuid` 存 TEXT。

与 §5 DDL 的偏差都在 `CONTRACT_GAPS` 里有条目，不自行选边：

- `package` 多 `node_epoch` 列（S43-11）：两列分开存，代码里没有任何比较。
- `distribution_attempt` 多 `auth_ref_id`（S43-12）：一次尝试用了哪张授权要
  可追，否则 E39 只能追到"这个包用过某判定"。
- `receipt` 多 `UNIQUE(tenant_id, attempt_id)`（S43-13）：回执重复投递不能
  产出第二条。
- 多出 `receipt_callback`（S43-14）：回调可能先于 attempt 落库（E17-S43-01），
  需要暂存区；丢掉载荷就再也拿不回来。
- 多出 `audit_intent`（S43-15）：E39 要求"每次 seal/publish 都有审计意图"，
  本表只存**意图与 S45 的 record_id**，不存审计链（链归 S45）。
- 四张 `*_projection`（S43-25）：消费事件只做"本地读到的比事件旧"的提示，
  每道门仍实时回查权威。它们是缓存，不是结论。
- 不建 `delivery.command_receipt`（S43-16）：平台 `idempotency_record` 同键
  同义，建两份会让"同键不同 body 拒绝"有两个权威。
- 不建 `accepted_master`（归 S08）、`audit_log`（归 S45）、
  `package_content_cache`（manifest 就是快照）。
"""

ID_PREFIX = {
    "package": "dp_",
    "attempt": "da_",
    "receipt": "dr_",
    "signature": "ds_",
    "auth_ref": "pa_",
    "callback": "cb_",
    "audit": "ai_",
    "operation": "op_",
}

SOURCE = "S43"
SCHEMA_VERSION = "2.0"
TOPIC = "drama.delivery.v2"
SERVICE_NAME = "delivery"

EVENT_CREATED = "delivery.package.created"
EVENT_SEALED = "delivery.package.sealed"
EVENT_PUBLISHED = "delivery.package.published"
EVENT_FAILED = "delivery.publish.failed"
EVENTS = (EVENT_CREATED, EVENT_SEALED, EVENT_PUBLISHED, EVENT_FAILED)

# 消费事件名按**实际产出方**登记（§6.3 末两段）。
EVENT_MASTER_RENDERED = "post.master.rendered"
EVENT_SNAPSHOT_COMMITTED = "snapshot.manifest.committed"
EVENT_VOICE_AUTH = "asset.voice.authorization.changed"
EVENT_SUBTITLE_READY = "post.subtitle.ready"

CONSUMERS = {
    EVENT_MASTER_RENDERED: "s43.master-candidate.v1",
    EVENT_SNAPSHOT_COMMITTED: "s43.manifest-hint.v1",
    EVENT_VOICE_AUTH: "s43.voice-authorization.v1",
    EVENT_SUBTITLE_READY: "s43.subtitle-hint.v1",
}

PACKAGE_TYPES = ("missing_material", "preview", "formal")
PACKAGE_STATES = ("DRAFTING", "BLOCKED", "SEALED", "PUBLISHED", "PUBLISH_FAILED")
ATTEMPT_STATES = ("PENDING", "IN_FLIGHT", "SUCCEEDED", "FAILED", "UNKNOWN",
                  "FAILED_UNRESOLVED")
# `UNKNOWN` 不是失败；`SUCCEEDED` 吸收后续回执（E17-S43-04）。
TERMINAL_ATTEMPT_STATES = ("SUCCEEDED", "FAILED", "FAILED_UNRESOLVED")
LIVE_ATTEMPT_STATES = ("IN_FLIGHT", "UNKNOWN")

ITEM_ROLES = (
    "master_video", "master_audio", "stem_dialogue", "stem_sfx", "stem_bgm",
    "subtitle", "thumbnail", "edl", "timeline_json", "memory_checkpoint",
    "asset_bindings", "qc_evidence", "policy_snapshot", "cost_snapshot",
    "other",
)

# §2.4：formal manifest 必须逐项覆盖的固定内容。清单自身（role=`manifest`）
# 不是 item 行——它就是 `delivery.manifest`，且排除在签名内容外（§2.4 #1）。
FIXED_ROLES = ("memory_checkpoint", "asset_bindings", "qc_evidence",
               "policy_snapshot", "cost_snapshot")

CHANNEL_KINDS = ("partner_api", "ftp_drop", "object_storage", "manual_handoff")

GATE_IDS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10")
GATE_RESULTS = ("pass", "fail", "deferred", "not_applicable")

# 创建时**不能**判定的门（§6.1 行 612）。G8 是策略门：外发时评估。
DEFERRED_AT_CREATE = ("G8",)

CANONICAL_VERSION = "jcs-v1"
CANONICAL_VERSIONS = (CANONICAL_VERSION,)

# 每条包事件让 aggregate_sequence +1；created=1（§6.3）。
SEQUENCE_CREATED = 1

# 失败码分组。取值来自 §2.3（门）、§3.3（非法转移）与 §4.4（失败矩阵）。
GATE_FAILURE_CODES = (
    "MASTER_NOT_ACCEPTED", "STALE_FENCE",
    "ARTIFACT_MISSING", "ARTIFACT_HASH_MISMATCH",
    "INCOMPLETE_TIMELINE", "NOT_DELIVERABLE",
    "QC_BLOCKING_ISSUE", "LOUDNESS_GATE_FAILED",
    "SUBTITLE_STALE", "SUBTITLE_MISSING",
    "RIGHTS_REVOKED", "POLICY_DENIED",
    "SIGNATURE_INVALID", "MANIFEST_INCOMPLETE",
)
TRANSITION_FAILURE_CODES = (
    "PACKAGE_BLOCKED", "PACKAGE_NOT_SEALED", "SIGNATURE_REQUIRED",
    "MASTER_SUPERSEDED", "IDEMPOTENCY_MISMATCH",
)
CHANNEL_FAILURE_CODES = (
    "CHANNEL_DISABLED", "CHANNEL_NOT_FOUND", "CHANNEL_FORMAT_UNSUPPORTED",
    "CHANNEL_REQUIRED_ARTIFACT_MISSING", "CHANNEL_FILE_TOO_LARGE",
    "PUBLISH_REJECTED", "CHANNEL_AUTH_FAILED", "CHANNEL_RATE_LIMITED",
    "CHANNEL_UNAVAILABLE", "NETWORK_UNREACHABLE", "TRANSFER_INTERRUPTED",
)
ATTEMPT_FAILURE_CODES = ("UNKNOWN", "RECONCILE_TIMEOUT",
                         "RECONCILE_NOT_RECEIVED")

VERDICTS = ("allow", "deny", "abstain", "require_human")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS package (
  tenant_id            TEXT NOT NULL,
  package_id           TEXT NOT NULL,
  project_id           TEXT NOT NULL,
  series_id            TEXT NOT NULL,
  episode_id           TEXT,
  branch_id            TEXT,
  package_type         TEXT NOT NULL CHECK (package_type IN
                       ('missing_material','preview','formal')),
  purpose              TEXT,
  -- 事件信封要带 home_cell（event-sdk 的 new_event 参数），§5 的 package
  -- 没有这一列；不存就只能给后续每条事件猜一个值（S43-27）。
  home_cell            TEXT NOT NULL DEFAULT 'unknown',
  -- G6 在**外发时**仍要按"要求的每个 locale"复查，不能只按已存在的
  -- subtitle item 反推——创建时缺失的 locale 在 item 里根本不存在（S43-28）。
  requested_locales    TEXT NOT NULL DEFAULT '[]',
  -- G7 是实时门，但"哪些音色"是创建时的输入；外发请求不带它时仍要复查，
  -- 否则省略 voice_refs 就成了绕过权利门的口子（S43-29）。
  voice_refs           TEXT NOT NULL DEFAULT '[]',
  master_candidate_id  TEXT,
  master_artifact_ref  TEXT,
  timeline_ref         TEXT,
  -- G1 要按 S08 的 manifest 头与 node 级接受指针复查；§5 的 package 没有
  -- 存这个 id 的地方（S43-32）。
  manifest_ref         TEXT,
  -- AGENTS.md：fence 属于 run/branch，epoch 属于单个 logical_node。
  -- 两列分开写，代码里没有任何一处比较或换算，也不推进任何接受指针。
  execution_fence      INTEGER NOT NULL,
  node_epoch           INTEGER,
  manifest_hash        TEXT,
  canonical_version    TEXT NOT NULL DEFAULT 'jcs-v1',
  package_hash         TEXT NOT NULL,
  state                TEXT NOT NULL CHECK (state IN
                       ('DRAFTING','BLOCKED','SEALED','PUBLISHED',
                        'PUBLISH_FAILED')),
  gaps                 TEXT NOT NULL DEFAULT '[]',
  gate_results         TEXT NOT NULL DEFAULT '{}',
  contract_ref         TEXT,
  created_by           TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  sealed_at            TEXT,
  published_at         TEXT,
  PRIMARY KEY (tenant_id, package_id),
  -- 一个 PUBLISHED 包必然有发布时间与签名；没有就是状态写错了。
  CHECK (state <> 'PUBLISHED' OR published_at IS NOT NULL),
  CHECK (state <> 'PUBLISHED' OR manifest_hash IS NOT NULL),
  CHECK (state <> 'SEALED' OR manifest_hash IS NOT NULL),
  CHECK (state <> 'SEALED' OR sealed_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS package_episode_idx ON package
  (tenant_id, episode_id, branch_id, created_at DESC);
CREATE INDEX IF NOT EXISTS package_state_idx ON package
  (tenant_id, state, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS package_hash_uniq ON package
  (tenant_id, package_hash);

CREATE TABLE IF NOT EXISTS item (
  tenant_id            TEXT NOT NULL,
  package_id           TEXT NOT NULL,
  role                 TEXT NOT NULL CHECK (role IN
                       ('master_video','master_audio','stem_dialogue',
                        'stem_sfx','stem_bgm','subtitle','thumbnail','edl',
                        'timeline_json','memory_checkpoint','asset_bindings',
                        'qc_evidence','policy_snapshot','cost_snapshot',
                        'other')),
  ordinal              INTEGER NOT NULL DEFAULT 0,
  artifact_ref         TEXT,
  content_hash         TEXT,
  byte_size            INTEGER,
  mime                 TEXT,
  locale               TEXT,
  external_ref         TEXT,
  PRIMARY KEY (tenant_id, package_id, role, ordinal),
  FOREIGN KEY (tenant_id, package_id)
    REFERENCES package (tenant_id, package_id),
  CONSTRAINT item_has_target
    CHECK (artifact_ref IS NOT NULL OR external_ref IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS item_hash_idx ON item (tenant_id, content_hash);

CREATE TABLE IF NOT EXISTS manifest (
  tenant_id            TEXT NOT NULL,
  package_id           TEXT NOT NULL,
  manifest_hash        TEXT NOT NULL,
  canonical_version    TEXT NOT NULL,
  manifest             TEXT NOT NULL,
  item_count           INTEGER NOT NULL,
  total_bytes          INTEGER NOT NULL,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, package_id),
  FOREIGN KEY (tenant_id, package_id)
    REFERENCES package (tenant_id, package_id)
);

CREATE TABLE IF NOT EXISTS integrity_signature (
  tenant_id            TEXT NOT NULL,
  signature_id         TEXT NOT NULL,
  package_id           TEXT NOT NULL,
  manifest_hash        TEXT NOT NULL,
  key_ref              TEXT NOT NULL,
  algorithm            TEXT NOT NULL,
  signature_b64        TEXT NOT NULL,
  verified             INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0,1)),
  verified_at          TEXT,
  verify_error         TEXT,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, signature_id),
  FOREIGN KEY (tenant_id, package_id)
    REFERENCES package (tenant_id, package_id)
);
CREATE INDEX IF NOT EXISTS signature_package_idx ON integrity_signature
  (tenant_id, package_id, created_at DESC);

CREATE TABLE IF NOT EXISTS publish_authorization_ref (
  tenant_id            TEXT NOT NULL,
  auth_ref_id          TEXT NOT NULL,
  package_id           TEXT NOT NULL,
  policy_decision_id   TEXT NOT NULL,
  decision_hash        TEXT,
  verdict              TEXT NOT NULL CHECK (verdict IN
                       ('allow','deny','abstain','require_human')),
  gate_type            TEXT NOT NULL DEFAULT 'external_publish',
  human_decision_ref   TEXT,
  reasons              TEXT NOT NULL DEFAULT '[]',
  valid_until          TEXT,
  evaluated_at         TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, auth_ref_id),
  FOREIGN KEY (tenant_id, package_id)
    REFERENCES package (tenant_id, package_id)
);

CREATE TABLE IF NOT EXISTS distribution_attempt (
  tenant_id            TEXT NOT NULL,
  attempt_id           TEXT NOT NULL,
  package_id           TEXT NOT NULL,
  channel_id           TEXT NOT NULL,
  state                TEXT NOT NULL CHECK (state IN
                       ('PENDING','IN_FLIGHT','SUCCEEDED','FAILED','UNKNOWN',
                        'FAILED_UNRESOLVED')),
  attempt_no           INTEGER NOT NULL,
  bytes_sent           INTEGER NOT NULL DEFAULT 0,
  remote_ref           TEXT,
  failure_code         TEXT,
  failure_detail       TEXT,
  reconcile_after      TEXT,
  auth_ref_id          TEXT,
  started_at           TEXT,
  finished_at          TEXT,
  PRIMARY KEY (tenant_id, attempt_id),
  UNIQUE (tenant_id, package_id, channel_id, attempt_no),
  FOREIGN KEY (tenant_id, package_id)
    REFERENCES package (tenant_id, package_id),
  -- UNKNOWN 必须带对账时间：没有对账时间的 UNKNOWN 永远不会被处理。
  CHECK (state <> 'UNKNOWN' OR reconcile_after IS NOT NULL),
  CHECK (state NOT IN ('FAILED','FAILED_UNRESOLVED') OR failure_code IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS attempt_unknown_idx ON distribution_attempt
  (reconcile_after) WHERE state = 'UNKNOWN';
CREATE INDEX IF NOT EXISTS attempt_package_idx ON distribution_attempt
  (tenant_id, package_id, started_at DESC);

CREATE TABLE IF NOT EXISTS receipt (
  tenant_id            TEXT NOT NULL,
  receipt_id           TEXT NOT NULL,
  attempt_id           TEXT NOT NULL,
  package_id           TEXT NOT NULL,
  channel_id           TEXT NOT NULL,
  remote_id            TEXT,
  acknowledged_at      TEXT NOT NULL,
  -- 只存验过签名的回执。伪签名在入库前就被拒（E17-S43-03）。
  signature_valid      INTEGER NOT NULL CHECK (signature_valid = 1),
  raw_ref              TEXT,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, receipt_id),
  UNIQUE (tenant_id, attempt_id),
  FOREIGN KEY (tenant_id, attempt_id)
    REFERENCES distribution_attempt (tenant_id, attempt_id)
);
CREATE INDEX IF NOT EXISTS receipt_package_idx ON receipt
  (tenant_id, package_id);

-- 回调先于 attempt 落库时的暂存区（E17-S43-01）。libpq 没有这种表就只剩
-- "丢掉载荷"或"500 让渠道重投"两种错误做法。
CREATE TABLE IF NOT EXISTS receipt_callback (
  tenant_id            TEXT NOT NULL,
  callback_id          TEXT NOT NULL,
  package_id           TEXT,
  channel_id           TEXT NOT NULL,
  attempt_id           TEXT,
  remote_id            TEXT,
  acknowledged_at      TEXT NOT NULL,
  payload              TEXT NOT NULL,
  received_at          TEXT NOT NULL,
  applied_at           TEXT,
  PRIMARY KEY (tenant_id, callback_id)
);

CREATE TABLE IF NOT EXISTS channel (
  tenant_id            TEXT NOT NULL,
  channel_id           TEXT NOT NULL,
  kind                 TEXT NOT NULL CHECK (kind IN
                       ('partner_api','ftp_drop','object_storage',
                        'manual_handoff')),
  endpoint_ref         TEXT NOT NULL,
  accepted_formats     TEXT NOT NULL,
  required_artifacts   TEXT NOT NULL,
  max_file_bytes       INTEGER,
  supports_resume      INTEGER NOT NULL DEFAULT 0
                       CHECK (supports_resume IN (0,1)),
  supports_query       INTEGER NOT NULL DEFAULT 0
                       CHECK (supports_query IN (0,1)),
  -- 生产开关默认关闭（AGENTS.md / §5 行 569）。
  enabled              INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, channel_id)
);

-- 只存"发给了 S45 的意图"与回执 id，不存审计链（链归 S45）。
CREATE TABLE IF NOT EXISTS audit_intent (
  tenant_id            TEXT NOT NULL,
  intent_id            TEXT NOT NULL,
  package_id           TEXT NOT NULL,
  attempt_id           TEXT,
  verb                 TEXT NOT NULL,
  action               TEXT NOT NULL,
  outcome              TEXT NOT NULL,
  record_id            TEXT,
  -- 0 = 意图已在本地落库但尚未送达 S45。这不是失败，也不是"已审计"。
  delivered            INTEGER NOT NULL DEFAULT 0 CHECK (delivered IN (0,1)),
  payload_digest       TEXT NOT NULL,
  sent_at              TEXT NOT NULL,
  PRIMARY KEY (tenant_id, intent_id)
);
CREATE INDEX IF NOT EXISTS audit_intent_package_idx ON audit_intent
  (tenant_id, package_id, sent_at DESC);

-- ---- 消费事件投影：只是"本地读到的比事件旧"的提示，不是判定依据 ----

CREATE TABLE IF NOT EXISTS voice_authorization_projection (
  tenant_id            TEXT NOT NULL,
  voice_id             TEXT NOT NULL,
  state                TEXT NOT NULL,
  state_seq            INTEGER NOT NULL,
  effective_at         TEXT,
  event_id             TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, voice_id)
);

CREATE TABLE IF NOT EXISTS subtitle_projection (
  tenant_id            TEXT NOT NULL,
  track_id             TEXT NOT NULL,
  target_locale        TEXT NOT NULL,
  timeline_revision    INTEGER NOT NULL,
  subtitle_revision    INTEGER NOT NULL,
  qc_decision          TEXT,
  defect_count         INTEGER NOT NULL DEFAULT 0,
  artifact_ref         TEXT,
  event_id             TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, track_id)
);

CREATE TABLE IF NOT EXISTS manifest_projection (
  tenant_id            TEXT NOT NULL,
  manifest_id          TEXT NOT NULL,
  revision             INTEGER NOT NULL,
  execution_fence      INTEGER NOT NULL,
  accepted_revision    INTEGER,
  event_id             TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, manifest_id)
);

CREATE TABLE IF NOT EXISTS candidate_projection (
  tenant_id            TEXT NOT NULL,
  candidate_id         TEXT NOT NULL,
  deliverable          INTEGER NOT NULL DEFAULT 0
                       CHECK (deliverable IN (0,1)),
  timeline_revision    INTEGER,
  event_id             TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, candidate_id)
);
"""

# 本轮**不解决**的缺口。`contracts/delivery.*.v2.schema.json` 里已有
# PDC-1/PDC-3、PDS-1、PDL-1/PDL-4/PDL-5、PDF-1/PDF-2 等 `x-conflicts` 与
# `x-needs-owner`，那些是待 owner 决定的事项。按 AGENTS.md「契约与文档冲突
# → 停下，报告冲突，不要自行选边」，这里只登记处理方式，不替 owner 裁决。
CONTRACT_GAPS = [
    {"id": "S43-1", "kind": "conflict",
     "summary": "OpenAPI 的 `/delivery-packages/{id}/publish` 把 "
                "`publish_authorization_ref` 定义成 string（§11 C4），方案 §6.1 "
                "用的是带 policy_decision_id/decision_hash 的对象",
     "handling": "对象与字符串两种都接受；字符串视为只有 decision_id、无 "
                 "decision_hash，此时 decision_hash 一致性检查跳过，其余（存在、"
                 "未过期、结论一致）照做。契约面不改。"},
    {"id": "S43-2", "kind": "gap",
     "summary": "S02 目前只有 `:resolve-effective` 与按 policy 取正文，没有 "
                "`:evaluate`，也没有按 decision_id 取判定的接口；§2.2 的实时门与 "
                "§6.1 的『回查同一 decision_id』都无处可调",
     "handling": "`policy_source` 是注入回调（evaluate / lookup / effective 三种 "
                 "op）；未配置即 503 POLICY_SOURCE_UNAVAILABLE。门失败不会被"
                 "改写成通过。"},
    {"id": "S43-3", "kind": "gap",
     "summary": "S08 没有『这个候选是不是当前 accepted master』的接口，只有 "
                "manifest 级 `accepted-pointers`（按 node）与 manifest 头",
     "handling": "G1 用 node 级 accepted pointer 比对 master_ref，并用 manifest "
                 "头的 execution_fence 判 STALE_FENCE；因此 formal/preview 的 "
                 "master_ref 必须带 node_id，缺了就是 422 而不是猜。"},
    {"id": "S43-4", "kind": "gap",
     "summary": "S42（master 候选与渲染证据）与 S38（字幕）当前是 design_only，"
                "G3/G5 与 G6 的实时输入没有已实现的读面",
     "handling": "`candidate_source` / `subtitle_source` 都是注入回调；未配置即 "
                 "503，门记为 unknown 且命令整体拒绝，不当 pass。"},
    {"id": "S43-5", "kind": "conflict",
     "summary": "OpenAPI 的 create body 只有 context/master_artifact_id/"
                "command_id，方案 §6.1 另有 package_type/episode_id/branch_id/"
                "purpose/master_candidate_id/timeline_ref/contract_ref/"
                "requested_locales",
     "handling": "实现按方案 §6.1 收字段（`master_artifact_id` 仍接受并归一成 "
                 "master_ref）；OpenAPI 未同步属契约面待办，不在本服务内改。"},
    {"id": "S43-6", "kind": "conflict",
     "summary": "方案 §6.1 示例返回 201，OpenAPI 的 `prepareDelivery` 返回 202 "
                "`Operation`",
     "handling": "终态（SEALED / BLOCKED）返回 201；签名或审计 sink 暂不可用、"
                 "包停在 DRAFTING 时返回 202 并带 retryable（§4.1 步骤 10 明确"
                 "要求 202）。响应体同时带 `operation_id`。"},
    {"id": "S43-7", "kind": "gap",
     "summary": "`contracts/openapi.v2.yaml` 没有 S43 的 GET 路径，而 "
                "service.yaml 的 query 与方案 §6.2 定义了六个",
     "handling": "按方案 §6.2 实现查询路径；契约同步不在本服务内做。"},
    {"id": "S43-8", "kind": "conflict",
     "summary": "方案 §4.2 行 302 写的是 `post.delivery.package.sealed`，"
                "`event-topics.v2.json` 登记的是 `delivery.package.sealed`"
                "（事件 schema 的 PDS-1 同一条）",
     "handling": "按已登记的 topic 目录发 `delivery.package.sealed`（无 `post.` "
                 "前缀），与 §6.3 行 655 一致。"},
    {"id": "S43-9", "kind": "gap",
     "summary": "`package_hash` 在 §5 是 NOT NULL 且 UNIQUE，但方案全文没有"
                "给出算法（事件 schema 的 PDL-5 同一条）。方案示例里的 "
                "`sha256:` 前缀与契约的 `^[0-9a-f]{64}$` 也不一致",
     "handling": "`package_hash = sha256(JCS(package 快照))`，快照含 package_id"
                 "（否则两次合法的同内容交付会撞 UNIQUE 索引）；一律裸小写 "
                 "hex，不带前缀。"},
    {"id": "S43-10", "kind": "gap",
     "summary": "`gaps` 的元素形状在 §5（裸 jsonb）、§4.1、§6.1（空数组）里"
                "从未定义，事件 schema 也刻意不约束",
     "handling": "本实现使用 `{role, code, detail}`；不去猜别的键，也不把它"
                 "升成契约。"},
    {"id": "S43-11", "kind": "gap",
     "summary": "§5 的 `package` 只有 `execution_fence`，没有 `node_epoch`；"
                "但创建体带完整 execution context，其中两者都有",
     "handling": "加 `node_epoch` 列存 master 候选的产出坐标，仅作引用；"
                 "两列分别读写，代码中无比较、无换算、不推进任何指针。"},
    {"id": "S43-12", "kind": "gap",
     "summary": "§5 的 `distribution_attempt` 没有指向授权的列，E39 无法回答"
                "『第 2 次尝试用的是哪张判定』",
     "handling": "加 `auth_ref_id` 指 `publish_authorization_ref`。"},
    {"id": "S43-13", "kind": "gap",
     "summary": "§5 的 `receipt` 只有 receipt_id 主键，重复回调会产出第二条",
     "handling": "加 `UNIQUE(tenant_id, attempt_id)`：一次尝试只有一条终态回执。"},
    {"id": "S43-14", "kind": "gap",
     "summary": "E17-S43-01 要求回调先于 attempt 落库时『inbox 暂存』，但 inbox "
                "只存 (consumer, event_id)，丢载荷",
     "handling": "加 `receipt_callback` 暂存原始载荷；attempt 出现后应用并标记 "
                 "`applied_at`。"},
    {"id": "S43-15", "kind": "gap",
     "summary": "E39 要求每次 seal/publish 都有 S45 审计意图，但 §5 没有存"
                "『意图已发出』的地方",
     "handling": "加 `audit_intent` 只存意图摘要与 S45 返回的 record_id；"
                 "审计链仍在 S45，本表不是第二权威。"},
    {"id": "S43-16", "kind": "gap",
     "summary": "§5 的 `delivery.command_receipt` 与平台 `idempotency_record` "
                "同键（tenant+command）同义",
     "handling": "只建平台那张表：两份幂等记录会让『同键不同 body 拒绝』有"
                 "两个权威，也会在崩溃时给出两种重放结果。"},
    {"id": "S43-17", "kind": "needs_owner",
     "summary": "§5 的 `purpose` 只有 SQL 注释（broadcast|archive|review|"
                "partner），没有 CHECK；`failure_code` 也没有闭集",
     "handling": "`purpose` 按自由文本接受（不自行加 CHECK）；`failure_code` "
                 "使用 §2.3/§3.3/§4.4 已命名的码，上游给出的未知码原样记录。"},
    {"id": "S43-18", "kind": "needs_owner",
     "summary": "§11 C2：service.yaml 的 depends_on 不含 S42，但 master 与"
                "渲染证据来自 S42；本方案假定经 S28 artifact id 引用",
     "handling": "G3/G5 通过注入的 `candidate_source` 取，不直连 S42 的业务表；"
                 "依赖清单是否补 S42 由 owner 定。"},
    {"id": "S43-19", "kind": "needs_owner",
     "summary": "§11 C1/C3：客户合同/输出规格无权威寻址目标；docs/04 §5 的"
                "固定内容与 S43 的 data_owner 不完全对齐",
     "handling": "`contract_ref` 只存引用不解释；固定内容以 item 引用形式覆盖，"
                 "『引用是否满足 docs/04』由 owner 确认。"},
    {"id": "S43-20", "kind": "needs_owner",
     "summary": "§11 C7：`DeliveryPackage` 是否覆盖系列级交付未定；OpenAPI 的 "
                "create body 没有 series_id",
     "handling": "结构上允许 `episode_id` 为空（系列包），但 formal 目前要求 "
                 "`episode_id`（G3/G4/G6 与 cost 快照都按集寻址）；系列级规则"
                 "待 owner 定。"},
    {"id": "S43-21", "kind": "needs_owner",
     "summary": "§11 C4：S02 的四值判定（allow/deny/abstain/require_human）与 "
                "`valid_until` 目前只是提案；§2.2 的四条硬规则依赖它们",
     "handling": "按提案实现四条规则（abstain/require_human 均非 allow，"
                 "过期即失效，每次尝试重评）；判定语义最终由 owner 定。"},
    {"id": "S43-22", "kind": "needs_owner",
     "summary": "§11 C6：签名 key 的归属无明文，密钥管理不在 48 服务表里",
     "handling": "用 `key_ref` 引用外部 KMS，`key_ref` 带 tenant_id 做隔离；"
                 "具体 KMS 由 owner 指定。"},
    {"id": "S43-23", "kind": "gap",
     "summary": "§4.4 的 UNKNOWN TTL（『如 24h』）是示例值，不是规范",
     "handling": "默认 24h，构造参数可覆盖；超过 TTL 转 FAILED_UNRESOLVED 并"
                 "告警，不自动重发。"},
    {"id": "S43-24", "kind": "gap",
     "summary": "§4.2 说『RFC 8785 或项目自定规范』，规范化实现未定；"
                "`canonical_version` 决定 hash",
     "handling": "当前实现 `jcs-v1` = 排序键 + 紧凑分隔符 + UTF-8 的 JSON，"
                 "与 canonical_version 串拼接后取 sha256；换实现就换版本串。"},
    {"id": "S43-25", "kind": "conflict",
     "summary": "§2.3 说门『全部实时』，§6.3 又说 G6 用 `post.subtitle.ready` "
                "携带的 timeline_revision 比对——事件是通知，不是实时读面",
     "handling": "G6 实时回查 `subtitle_source`；消费到的事件只更新本地投影，"
                 "用于发现『读到的比事件旧』。投影永远不能放行一门。"},
    {"id": "S43-27", "kind": "gap",
     "summary": "§5 的 `package` 没有 `home_cell`，但 event-sdk 的每条信封都"
                "要求 `home_cell`；创建之后所有事件都要用它",
     "handling": "加 `home_cell` 列，值取签名 execution context 的 "
                 "`home_cell`（不取请求体顶层的同名字段）；缺值时默认 "
                 "`unknown`，与其它服务一致。"},
    {"id": "S43-28", "kind": "needs_owner",
     "summary": "§2.3 G6 说『要求的每个 locale 都有 subtitle artifact』，"
                "但 `package` / `item` 都没有登记『要求了哪些 locale』的列；"
                "只按已有 item 反推会让『创建时就缺的那个 locale』永远消失",
     "handling": "create 体接受 `requested_locales` 并存成列；G6 按它逐项"
                 "检查。为空时按已存在的 subtitle item 的 locale 检查（无法"
                 "凭空要求）。空集语义待 owner 确认。"},
    {"id": "S43-29", "kind": "gap",
     "summary": "§2.3 G7 是实时门，但需要知道『哪些音色』；§4.3 的外发请求体"
                "没有 `voice_refs`，省略它就等于跳过权利门",
     "handling": "create 时把 `voice_refs` 记成 package 的输入；publish 请求"
                 "可覆盖，但不会因为省略而跳过——创建时登记过的音色每次外发"
                 "都实时复查。"},
    {"id": "S43-32", "kind": "gap",
     "summary": "§5 的 `package` 没有 `manifest_ref`（S08 的 manifest id），"
                "但 G1 要在创建与每次外发时按 manifest 头与 node 级接受指针"
                "复查；不存就只能每次让调用方重新给，等于把复查的坐标交给"
                "调用方",
     "handling": "加 `manifest_ref` 列，取签名 execution context 的 "
                 "`manifest_ref`；外发请求不允许改写它。"},
    {"id": "S43-31", "kind": "gap",
     "summary": "§4.1 第 4 步要求 `master_ref` 能指向 S42 候选或 S28 "
                "artifact，但 §5 的 `package` 没有 `master_ref`，只有 "
                "`master_artifact_ref`",
     "handling": "`master_artifact_ref` 存 `{id, revision, content_hash, "
                 "node_id?}` 这一份引用；带 `node_id` 才能按 S08 的 node 级"
                 "接受指针复查（S43-3），formal/preview 缺它就是 422。"},
    {"id": "S43-30", "kind": "needs_owner",
     "summary": "§3.3 与 §4.4 的失败矩阵里，`FAILED`（终态失败）与"
                "`FAILED_UNRESOLVED`（对账超时）都让包进 `PUBLISH_FAILED`，"
                "但『可重试失败』该回到 `SEALED` 还是留在同一个 attempt 的"
                "`IN_FLIGHT` 重试，两处说法不同（§3.2 画的是回 IN_FLIGHT，"
                "§3.1 画的是回 SEALED，§4.4 写『最多 8 次』）",
     "handling": "按 §4.5 实现：可重试失败让包回 `SEALED`，attempt 记"
                 "`FAILED` + `retryable=true`，下次外发是**新的** attempt"
                 "（包不变）。§3.2 的『同一 attempt 内 IN_FLIGHT 退避』"
                 "需要本服务自己持有退避计时，属调度职责，未实现。"},
    {"id": "S43-26", "kind": "gap",
     "summary": "方案没有定义渠道配置的写入接口，而 `channel` 表是 S43 自己的",
     "handling": "加 `POST /channels` / `GET /channels`（按租户隔离，"
                 "`enabled` 默认 false）；契约面同步不在本服务内做。"},
    {"id": "S43-35", "kind": "conflict",
     "summary": "`contracts/delivery.package-published.v2.schema.json` 把 "
                "`master_candidate_id` 定为 `string, minLength 1`（不可为 null），"
                "而 `delivery-package.created` / `.sealed` 允许它为 null，"
                "§1.1 也不要求每个包都有 master 候选（例如 missing_material 包）",
     "handling": "外发前检查：包没有 `master_candidate_id` 就 409 "
                 "`MASTER_CANDIDATE_REQUIRED`，在调用任何上游之前拒绝。"
                 "不伪造候选 id、不把 null 塞进事件——那会让 schema 校验通过"
                 "但语义是假的。是否允许『无候选的包也能外发』（若可以，"
                 "published 事件需要改契约）由 owner 定。"},
    {"id": "S43-36", "kind": "gap",
     "summary": "方案 §6.1 只定义了 create / seal / publish 三条命令，"
                "但 E16 要求『对账』、E17 要求『渠道回执』，两者都没有路径",
     "handling": "加 `POST /delivery-packages/{id}:reconcile` 与 "
                 "`POST /receipts`（渠道回调）。两者都不外发：对账只查询或"
                 "应用已到的回执，回执入口先验签再落库。契约面同步不在本"
                 "服务内做。"},
]
