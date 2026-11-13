"""S45 自己的 schema。表来源是 `plans/S45-audit.md` §5。

四条结构性事实决定这张库能证明什么：

1. **`audit_record` 是唯一的事实，且只增不改。** 方案 §1.5 要求"业务补偿删不掉
   审计行"。SQLite 没有表级 REVOKE，所以用 `BEFORE UPDATE` / `BEFORE DELETE`
   触发器把两条写路径都挡死；任何"修一下这条记录"的想法都会在数据库层抛
   `AUDIT_RECORD_IMMUTABLE`，而不是留下一条被悄悄改过的证据。
2. **`audit_stream` 刻意可变。** 它是链头（head_seq/head_hash），每次追加都要
   推进；它**不是**证据。证据在 `audit_record.record_hash` 与
   `audit_segment.root_hash`。链头坏了可以从分段重建（§5.2）。
3. **`audit_record` 没有 `segment_id` 列。** 归属用 `audit_segment` 的
   `(stream_key, from_seq, to_seq)` 范围表达（§5.1）。改成外键就必须在封印时
   回填，而回填是一次 UPDATE——与不可变直接冲突。PG 侧用 `EXCLUDE USING gist`
   保证区间不重叠；SQLite 没有 EXCLUDE，改成 `UNIQUE (tenant_id, stream_key,
   from_seq)` + 服务层区间重叠检查（见 service.py `_touch_segment`）。
4. **`audit_record_target` 是可重建的索引，不是第二份事实。** 丢掉一行只影响
   取证检索的命中率，不影响链；重建过程本身要写一条 meta 记录（§5.4）。

方案里的 `jsonb` 存 TEXT（JCS 后的字符串）、`timestamptz` 存定宽 UTC 字符串、
`text[]` 存 JSON 数组字符串、`boolean` 存 INTEGER 0/1。分区表不建分区：
SQLite 没有声明式分区，"按月分区 + DETACH PARTITION 销毁"这一整套在单文件库
里无对应物——这是已登记的缺口（NEW-2），不是实现选择。
"""

ID_PREFIX = {
    "record": "arec_",
    "segment": "aseg_",
    "export": "exp_",
    "hold": "hold_",
    "order": "dor_",
    "access": "acc_",
    "intent": "aint_",
}

# 本轮**不解决**的缺口。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要
# 自行选边」：本服务按方案散文实现，把偏差记在这里，不修改契约、不代填决定。
CONTRACT_GAPS = [
    # --- 方案 §11 自己登记的 ---
    {"id": "C45-1", "kind": "conflict", "blocking": True,
     "summary": "无 audit-record.v1 schema；openapi.v2.yaml 里没有 "
                "POST /audit-records；event-topics.v2.json 只有 "
                "audit.export.ready，没有审计意图的入站 topic",
     "handling": "按方案 §2.2 的建议形状实现两个入口；请求/事件的字段集"
                 "只在本仓库内有定义，跨服务一致性测试无法进行。"},
    {"id": "C45-2", "kind": "conflict",
     "summary": "无 audit-export-bundle.v1 与 chain-proof 的形状定义",
     "handling": "按 §3.6 的目录清单自建：manifest.json / segments/*.jsonl / "
                 "chain-proof.json / bundle.sha256，摘要算法与拼接顺序写在 "
                 "service.py `_bundle_digest` 的 docstring 里。"},
    {"id": "C45-3", "kind": "needs_owner",
     "summary": "catalog 的 depends_on 只有 S01，但 S28 删除编排必须调 "
                "GET /retention-checks，S28 侧已登记「引用方清单需裁决」",
     "handling": "实现 /retention-checks 且不把 S28 变成硬依赖（纯查本库索引，"
                 "200ms 内返回）；部署顺序仍由 catalog 决定，未改。"},
    {"id": "C45-4", "kind": "conflict", "blocking": True,
     "summary": "没有提议的入站 topic drama.audit.ingress.v2，生产方无处投递",
     "handling": "事件入口实现为 `consume_intent(envelope)`，由未来的消费者"
                 "适配层调用；不自行往 event-topics 里加 topic。"},
    {"id": "C45-5", "kind": "needs_owner",
     "summary": "automation-policy.v2 无 audit.blocking 字段，"
                "§7.2 的 strict_audit 阻塞口径没有策略载体",
     "handling": "retention_policy.strict_audit 只存标记，本内核**不**据此"
                 "阻塞任何业务写——阻塞必须由调用方按自己 outbox 滞留时长决定。"},
    {"id": "C45-6", "kind": "needs_owner",
     "summary": "hot/warm/total 保留档位在契约里没有承载位置",
     "handling": "保留策略存本库 audit_retention_policy；对外只在 "
                 "readiness.contract_gaps 里报告，不宣布已可配置化。"},
    {"id": "C45-7", "kind": "needs_owner",
     "summary": "脱敏规则表 contracts/redaction-policy.v2.json 仍是 proposal，"
                "R-1（k_redact_scope 的 KMS 归属）未裁决",
     "handling": "按规则表实现六个类；class_a 的 HMAC 键取 "
                 "S45_REDACT_KEY，未配置时退化为进程内固定键——只用于本内核"
                 "自测，生产必须先接 KMS（R-1）。"},
    {"id": "C45-8", "kind": "needs_owner",
     "summary": "GET /audit-exports/{id} 的下载票（短签名）机制未定义",
     "handling": "只返回 bundle_ref 与 expires_at，不签发下载票；"
                 "交付方式留空，不发明凭证格式。"},
    {"id": "C45-9", "kind": "needs_owner",
     "summary": "审计保留是否阻塞 S28 删除未裁决（docs/04 §6 只说保留最小证明）",
     "handling": "本服务如实回答 referenced=true + reason；是否阻塞由 S28 决定，"
                 "S45 不替它做删除判定。"},
    {"id": "C45-10", "kind": "needs_owner",
     "summary": "外部时间戳锚定（TSA）是否需要、锚到哪，全无定义",
     "handling": "audit_segment.anchor_ref / anchored_at 留空；"
                 "ARCHIVED→ANCHORED 的迁移只在有外部锚时由运维触发。"},

    # --- contracts/audit.export-ready.v2.schema.json 已登记的 ---
    {"id": "AEX-1", "kind": "conflict",
     "summary": "bundle_ref 在方案 §4.3 示例（四键引用）与 §5.6 DDL 注释"
                "（storage_ref/sha256/size_bytes）里是两个形状；chain_continuous "
                "在 DDL 可空而 §3.6 要求必为 true|false",
     "handling": "事件体按四键引用形状；库里按 storage_ref/sha256/size_bytes 存；"
                 "chain_continuous 在导出完成时必写，未完成时不出事件。"},
    {"id": "AEX-2", "kind": "gap",
     "summary": "FAILED / EXPIRED 没有事件，调用方只能轮询",
     "handling": "只发布 audit.export.ready（与 event-topics 一致）；失败态"
                 "靠 GET /audit-exports/{id} 暴露，并登记为通知路径缺口。"},
    {"id": "AEX-3", "kind": "conflict",
     "summary": "audit_export / audit_segment / audit_access_log 的主键是裸 "
                "id，跨租户读这些 id 必须 404（E28）",
     "handling": "所有查询强制 tenant_id 谓词；id 本身不含租户，所以跨租户"
                 "命中只有 404，不泄露存在性。"},
    {"id": "AEX-4", "kind": "needs_owner",
     "summary": "§3.6 的目录名 <bundle_id> 与 audit_export 无 bundle_id 列",
     "handling": "bundle_id 取 export_id，目录名与 storage_ref 同源；"
                 "不新增列。"},
    {"id": "AEX-5", "kind": "conflict",
     "summary": "§4.3 示例的 data_ref 是裸字符串、causation_id 为 null，"
                "与本仓 event validator 的四键 data_ref 不一致",
     "handling": "data_ref 写四键引用（与 project-catalog / model-routing 一致），"
                 "causation_id 用导出命令的 command_id。"},

    # --- 实现时新发现的 ---
    {"id": "NEW-1", "kind": "conflict", "blocking": True,
     "summary": "contracts/identity/permission-registry.v2.json 的 40 个键里"
                "没有任何 audit.* 键，§10.2 的 audit.read / audit.export / "
                "audit.destroy 无法被 S01 判定",
     "handling": "读取/导出/销毁一律经注入的授权源判定；真实 S01 会返回"
                 "未注册键（deps 抛 503 PERMISSION_KEY_UNREGISTERED），"
                 "因此本内核在这些路径上只能对 stub 验证，不能对真实 S01 验证。"},
    {"id": "NEW-2", "kind": "gap",
     "summary": "SQLite 无声明式分区、无 EXCLUDE、无表级 REVOKE；"
                "且不可变触发器挡住 DELETE，所以§3.7 的「销毁后正文不再存在」"
                "在本内核里做不到",
     "handling": "分段状态置 DESTROYED 后查询按 seq 区间排除正文、返回 "
                 "proof_only；字节仍留在库里，属于已知未实现，不是已销毁。"
                 "生产必须换 PG 分区表 + DETACH。"},
    {"id": "NEW-3", "kind": "conflict",
     "summary": "422 RESIDENCY_VIOLATION 需要 tenant.home_region/home_cell，"
                "而 S01 的 tenant 表只有 tenant_id/slug/lifecycle/"
                "revocation_epoch/created_at",
     "handling": "本内核不判定驻留，也不根据调用方自报的 region 判定；"
                 "错误码保留在契约里但不可达。"},
    {"id": "NEW-4", "kind": "gap",
     "summary": "403 SOURCE_MISMATCH 需要「认证的 producer 身份」，"
                "EventEnvelope 里没有生产者身份字段，HTTP 也没有对应头",
     "handling": "consume_intent 接受 producer_id 参数；有则比对，无则跳过"
                 "并把该事实写进返回值（producer_verified=false）。"},
    {"id": "NEW-5", "kind": "conflict",
     "summary": "§3.3 的 canonical 键集漏了 on_behalf_of / source_verb / "
                "primary_target_index / source_intent_id：它们落库、可查询，"
                "却不在哈希覆盖范围内",
     "handling": "严格按 §3.3 的键集计算（否则方案自己的复算公式对不上），"
                 "并在 get_record 返回里标注 covered_by_hash=false 的字段。"},
    {"id": "NEW-6", "kind": "conflict",
     "summary": "任务书要求「含明文密钥的记录应拒收」，"
                "而 §2.5 与 redaction-policy.v2 定死 redact_and_store、"
                "「任何情况下都不因脱敏拒收」",
     "handling": "按 §2.5 实现（替换后照常入库）。拒收会让生产方用一串密钥"
                 "形状的字符串抹掉自己的审计行。密钥形状的值不拒收；"
                 "**原始载荷字段**（arguments/payload/prompt 等）仍按形状违规"
                 "422 拒收——那不是脱敏问题，是 details 只放引用与计数（§2.2）。"},
    {"id": "NEW-7", "kind": "conflict",
     "summary": "§2.5 说 action/actor/targets/outcome 一律不动，"
                "redaction-policy.v2 的 applies_to_fields 却列了 actor",
     "handling": "按 §2.5：不扫描 action/targets/outcome/actor；只扫 "
                 "details / outcome_detail / on_behalf_of / policy_ref / "
                 "evidence_refs。"},
    {"id": "NEW-8", "kind": "gap",
     "summary": "audit.payload.redacted 自审记录在契约里没有 schema，"
                "且自己也是一条审计记录",
     "handling": "action='audit.payload.redacted' 写进 meta 链，只含受影响 "
                 "record_id 与命中的类名（不含被替换的值）；该 action 不再"
                 "触发自审记录，递归在此截断。"},
    {"id": "NEW-9", "kind": "conflict",
     "summary": "§5.7 的 outbox 有 UNIQUE(aggregate_id, aggregate_sequence, "
                "event_type)，event_sdk 的 OUTBOX_DDL 只有 event_id 主键",
     "handling": "用平台 SDK 的表，不另建；export.ready 的 aggregate_sequence "
                 "固定为 1，重发由调用方的 relay 负责去重。"},
    {"id": "NEW-10", "kind": "conflict",
     "summary": "§5.7 的 idempotency_record 有 command_type/state/"
                "response_code/response_body 四列，idempotency_sdk 只有 result",
     "handling": "用 SDK 的表（同键同 body 回放、异 body 409 已由 SDK 保证）；"
                 "四列不落库，需要的命令类型由 action 本身表达。"},
    {"id": "NEW-11", "kind": "gap",
     "summary": "§3.6 要求导出异步（202 + export_id），本内核没有后台 worker",
     "handling": "接口返回 202，构建在请求内同步完成；导出状态仍按 "
                 "REQUESTED→SEALED→READY 落库，FAILED 路径可测。"
                 "生产需要真正的构建队列。"},
    {"id": "NEW-12", "kind": "gap",
     "summary": "audit.export.ready 的 home_cell 无可信来源（同 NEW-3）",
     "handling": "事件体的 home_cell 取 `cell_unassigned` 并在返回值里暴露；"
                 "宁可显式未知，也不按调用方自报值填。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 追加式事实（§5.1） ----------
-- 没有 segment_id：归属由 audit_segment 的区间表达，避免封印时回填。
-- 没有任何可空"摘要/正文"列：details 只放引用与计数，正文走 data_ref。
CREATE TABLE IF NOT EXISTS audit_record (
  record_id            TEXT NOT NULL,
  tenant_id            TEXT NOT NULL,
  stream_key           TEXT NOT NULL,
  stream_seq           INTEGER NOT NULL CHECK (stream_seq >= 1),

  recorded_at          TEXT NOT NULL,          -- S45 时钟，参与哈希
  occurred_at          TEXT NOT NULL,          -- 调用方时钟，参与哈希
  clock_skew_ms        INTEGER,                -- 仅观测，不参与哈希

  source_service       TEXT NOT NULL,
  source_verb          TEXT NOT NULL,
  -- 两个入口各有自己的幂等键：事件入口用它的事件 id，命令入口用意图 id。
  -- 至少有一个，否则这条记录无法回答"它对应哪次投递/哪条命令"。
  source_event_id      TEXT,
  source_intent_id     TEXT,
  correlation_id       TEXT,
  causation_id         TEXT,

  action               TEXT NOT NULL,
  actor                TEXT NOT NULL,          -- {principal_id,principal_type,delegation_id}
  on_behalf_of         TEXT,
  targets              TEXT NOT NULL,          -- [{service_id,kind,id,revision,content_hash}]
  primary_target_index INTEGER NOT NULL DEFAULT 0
                         CHECK (primary_target_index >= 0),

  policy_ref           TEXT,
  evidence_refs        TEXT NOT NULL DEFAULT '[]',
  outcome              TEXT NOT NULL
                         CHECK (outcome IN ('SUCCEEDED','DENIED','ERROR',
                                            'REPORTED_UNKNOWN')),
  outcome_detail       TEXT NOT NULL DEFAULT '{}',
  details              TEXT NOT NULL DEFAULT '{}',

  schema_version       TEXT NOT NULL,
  prev_hash            TEXT NOT NULL CHECK (length(prev_hash) = 64),
  record_hash          TEXT NOT NULL CHECK (length(record_hash) = 64),
  -- 规范化后的摘要，便于独立复算（§5.1）。
  canonical_sha256     TEXT NOT NULL CHECK (length(canonical_sha256) = 64),

  PRIMARY KEY (tenant_id, record_id),
  -- §6.1 第三套幂等键：同一个 stream 上 seq 不重号。
  UNIQUE (tenant_id, stream_key, stream_seq),
  -- §5.1：同一个租户内 record_hash 不重复；这是"两条记录完全相同"的硬信号。
  UNIQUE (tenant_id, record_hash),
  CHECK (source_event_id IS NOT NULL OR source_intent_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ar_time_idx
  ON audit_record (tenant_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS ar_action_idx
  ON audit_record (tenant_id, action, recorded_at DESC);
-- actor 是 jsonb；SQLite 用 JSON1 的 json_extract 建表达式索引
-- （已确认 3.45.3 可用，见 tests/test_audit.py 的探针用例）。
CREATE INDEX IF NOT EXISTS ar_actor_idx
  ON audit_record (tenant_id, json_extract(actor, '$.principal_id'),
                   recorded_at DESC);

-- §1.5 / T45-15 / T45-16：任何 UPDATE / DELETE 都被拒绝。
-- 只挡应用路径；T45-20 的"超级用户改哈希"由段校验暴露，不靠这里。
CREATE TRIGGER IF NOT EXISTS audit_record_no_update
BEFORE UPDATE ON audit_record
BEGIN
  SELECT RAISE(ABORT, 'AUDIT_RECORD_IMMUTABLE');
END;

CREATE TRIGGER IF NOT EXISTS audit_record_no_delete
BEFORE DELETE ON audit_record
BEGIN
  SELECT RAISE(ABORT, 'AUDIT_RECORD_IMMUTABLE');
END;

-- ---------- 链头（§5.2） ----------
-- 这张表刻意可变：它是"下一条接在哪"的指针，不是证据。
CREATE TABLE IF NOT EXISTS audit_stream (
  tenant_id       TEXT NOT NULL,
  stream_key      TEXT NOT NULL,
  stream_kind     TEXT NOT NULL CHECK (stream_kind IN ('subject', 'meta')),
  head_seq        INTEGER NOT NULL DEFAULT 0 CHECK (head_seq >= 0),
  head_hash       TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'
                    CHECK (length(head_hash) = 64),
  seal_state      TEXT NOT NULL DEFAULT 'OPEN'
                    CHECK (seal_state IN ('OPEN', 'SEALING')),
  open_segment_id TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, stream_key)
);
CREATE INDEX IF NOT EXISTS as_kind_idx ON audit_stream (tenant_id, stream_kind);

-- ---------- 封印分段（§5.3） ----------
-- PG 用 EXCLUDE USING gist 保证区间互不重叠；SQLite 没有 EXCLUDE，
-- 用 UNIQUE(tenant_id, stream_key, from_seq) 挡住"两条从头开始的段"，
-- 剩下的尾部重叠由 service._touch_segment 在写入前判定。
CREATE TABLE IF NOT EXISTS audit_segment (
  segment_id       TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  stream_key       TEXT NOT NULL,
  stream_kind      TEXT NOT NULL,
  from_seq         INTEGER NOT NULL CHECK (from_seq >= 1),
  to_seq           INTEGER NOT NULL CHECK (to_seq >= from_seq),
  from_prev_hash   TEXT NOT NULL CHECK (length(from_prev_hash) = 64),
  root_hash        TEXT NOT NULL CHECK (length(root_hash) = 64),
  record_count     INTEGER NOT NULL CHECK (record_count >= 0),
  always_from      TEXT NOT NULL,
  always_to        TEXT NOT NULL,
  state            TEXT NOT NULL DEFAULT 'OPEN'
                     CHECK (state IN ('OPEN','SEALING','SEALED','ARCHIVED',
                                      'ANCHORED','INTEGRITY_FAILED','DESTROYED')),
  storage_class    TEXT NOT NULL DEFAULT 'HOT_PG'
                     CHECK (storage_class IN ('HOT_PG','WARM_S3','COLD_WORM')),
  storage_ref      TEXT,
  bytes_uncompressed INTEGER,
  sealed_at        TEXT,
  archived_at      TEXT,
  anchor_ref       TEXT,
  anchored_at      TEXT,
  integrity_checked_at TEXT,
  destruction_order_id TEXT,
  UNIQUE (tenant_id, stream_key, from_seq)
);
CREATE INDEX IF NOT EXISTS aseg_lookup_idx
  ON audit_segment (tenant_id, stream_key, always_from DESC);
CREATE INDEX IF NOT EXISTS aseg_state_idx
  ON audit_segment (tenant_id, state, archived_at);

-- ---------- 次要目标索引（§5.4，可重建） ----------
CREATE TABLE IF NOT EXISTS audit_record_target (
  tenant_id      TEXT NOT NULL,
  target_service TEXT NOT NULL,
  target_kind    TEXT NOT NULL,
  target_id      TEXT NOT NULL,
  recorded_at    TEXT NOT NULL,
  record_id      TEXT NOT NULL,
  stream_key     TEXT NOT NULL,
  stream_seq     INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, target_service, target_kind, target_id,
               recorded_at, record_id)
);
CREATE INDEX IF NOT EXISTS art_seq_idx
  ON audit_record_target (tenant_id, stream_key, stream_seq);

-- ---------- 保留策略（§5.5） ----------
CREATE TABLE IF NOT EXISTS audit_retention_policy (
  tenant_id            TEXT PRIMARY KEY,
  retention_hot_days   INTEGER NOT NULL DEFAULT 90
                         CHECK (retention_hot_days >= 0),
  retention_warm_days  INTEGER NOT NULL DEFAULT 730
                         CHECK (retention_warm_days >= 0),
  retention_total_days INTEGER CHECK (retention_total_days IS NULL
                                      OR retention_total_days >= 0),
  worm_required        INTEGER NOT NULL DEFAULT 0
                         CHECK (worm_required IN (0, 1)),
  anchor_required      INTEGER NOT NULL DEFAULT 0
                         CHECK (anchor_required IN (0, 1)),
  -- C45-5：这个标记**不**用来阻塞业务写。阻塞口径没有策略载体。
  strict_audit         INTEGER NOT NULL DEFAULT 0
                         CHECK (strict_audit IN (0, 1)),
  updated_by           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  CONSTRAINT arp_hot_lt_warm CHECK (retention_total_days IS NULL
                                    OR retention_hot_days <= retention_warm_days),
  CONSTRAINT arp_warm_lt_total CHECK (retention_total_days IS NULL
                                      OR retention_warm_days <= retention_total_days)
);

CREATE TABLE IF NOT EXISTS legal_hold (
  hold_id       TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  scope_kind    TEXT NOT NULL
                  CHECK (scope_kind IN ('tenant', 'subject', 'time_range')),
  scope         TEXT NOT NULL,
  reason        TEXT NOT NULL,
  case_ref      TEXT,
  authorized_by TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  released_at   TEXT,
  released_by   TEXT
);
CREATE INDEX IF NOT EXISTS lh_active_idx
  ON legal_hold (tenant_id) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS destruction_order (
  order_id       TEXT PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  scope          TEXT NOT NULL,
  authorized_by  TEXT NOT NULL,
  policy_ref     TEXT NOT NULL,
  hold_check     TEXT NOT NULL,
  preconditions  TEXT NOT NULL,
  state          TEXT NOT NULL
                   CHECK (state IN ('REQUESTED','BLOCKED','EXECUTING',
                                    'EXECUTED','FAILED')),
  blocked_reason TEXT,
  executed_at    TEXT,
  record_count_destroyed INTEGER,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS do_state_idx
  ON destruction_order (tenant_id, state);

-- ---------- 导出与访问（§5.6） ----------
CREATE TABLE IF NOT EXISTS audit_export (
  export_id       TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  query           TEXT NOT NULL,
  query_hash      TEXT NOT NULL CHECK (length(query_hash) = 64),
  state           TEXT NOT NULL
                    CHECK (state IN ('REQUESTED','BUILDING','SEALED','READY',
                                     'EXPIRED','FAILED')),
  segment_ids     TEXT NOT NULL DEFAULT '[]',
  record_count    INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),
  bundle_ref      TEXT,
  chain_continuous INTEGER CHECK (chain_continuous IS NULL
                                  OR chain_continuous IN (0, 1)),
  gaps            TEXT NOT NULL DEFAULT '[]',
  failure_reason  TEXT,
  requested_by    TEXT NOT NULL,
  purpose         TEXT,
  created_at      TEXT NOT NULL,
  completed_at    TEXT,
  expires_at      TEXT
);
CREATE INDEX IF NOT EXISTS ae_tenant_idx
  ON audit_export (tenant_id, created_at DESC);
-- §5.6：同租户同查询在构建中不重复建作业。
CREATE UNIQUE INDEX IF NOT EXISTS ae_idem_idx
  ON audit_export (tenant_id, query_hash, created_at)
  WHERE state IN ('REQUESTED', 'BUILDING');

CREATE TABLE IF NOT EXISTS audit_access_log (
  access_id    TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  accessed_at  TEXT NOT NULL,
  principal    TEXT NOT NULL,
  access_kind  TEXT NOT NULL
                 CHECK (access_kind IN ('QUERY','EXPORT_CREATE',
                                        'EXPORT_DOWNLOAD','VERIFY')),
  -- 只存查询的摘要，不存返回的记录正文（§3.8：否则敏感内容二次扩散）。
  query_hash   TEXT,
  export_id    TEXT,
  result_count INTEGER,
  coverage     TEXT,
  purpose      TEXT,
  trace_id     TEXT
);
CREATE INDEX IF NOT EXISTS aal_principal_idx
  ON audit_access_log (tenant_id, accessed_at DESC);
"""

# ---------------------------------------------------------------- 权限键
# `contracts/identity/permission-registry.v2.json` 共 40 个键，其中 `audit.*`
# 的数量是 **0**。所以下面这张表里**没有一个是已注册的**——包括只读的
# `audit.read`。这不是漏抄，是 NEW-1 的事实：本服务问 S01 的每一个动作，
# 在真实 S01 上都会得到 `PERMISSION_KEY_UNREGISTERED`（503），而不是"被拒"。
#
# 把"因为键不存在所以没法判"和"这个人没权限"分成两个错误，是为了不让 503
# 被读成 403 的另一种写法；也让注册表补上这些键之后，只需改这一处常量。
PERMISSIONS = {
    "read": "audit.read",
    "export": "audit.export",
    "destroy": "audit.destroy",
    "retention_change": "audit.retention.change",
}

# 与 project-catalog 的 WRITE_PERMISSIONS 同构：留下的是机器可检查的期望，
# 不是可用性。测试会核对它们确实都不在注册表里，直到注册表变。
WRITE_PERMISSIONS = {
    "create_export": PERMISSIONS["export"],
    "destroy_segments": PERMISSIONS["destroy"],
    "put_retention_policy": PERMISSIONS["retention_change"],
}

READ_PERMISSION = PERMISSIONS["read"]

# 真的会去问 S01 的那些。空集合是"当前一个也问不了"的准确表达。
REGISTERED_PERMISSIONS = frozenset()
