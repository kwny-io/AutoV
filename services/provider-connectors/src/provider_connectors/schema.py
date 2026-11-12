"""S27 自己的 schema。别的服务不 import 这里，只透过事件与 HTTP 契约交互。

与 `plans/S27-provider-connectors.md` §5.2 / §5.4 / §8.1 / §11 的 DDL 一一对应，
Postgres 写法换 SQLite（同 S21/S22/S23/S26 的规则）：

- `timestamptz` → `TEXT`（固定宽度 27 字符 ISO-8601 UTC，见 service 的 `_ts`）
- `jsonb` → `TEXT`（JCS 文本）
- `bigint` / `integer` → `INTEGER`（SQLite 的 INTEGER 是 64 位）
- `bytea` → `BLOB`

**服务自己建的表**：`connector_command_inbox` / `connector_callback_inbox` /
`connector_request_receipt` / `connector_client_key_index` / `channel_health`。

**平台 SDK 建的表**：`outbox` / `inbox`（`event_sdk`）。方案 §11 又手写了一份
`connector_observation_outbox`，与 `event_sdk` 的真实列名不同（`aggregate_seq` vs
`sequence`、`partition_key`/`headers`/`published_at` 只在那份草稿里存在）。以 SDK
为准，这里不重复声明 —— 同 S26 的 C26-1。S27 只发一种事件，没有需要额外保留的
出站元数据，SDK 的 `outbox` 足够。

## 与方案的偏离（登记为 C27-*）

- **`connector_command_inbox` 多两列** `attempts` / `processed_at`：§5.1 要求
  "claim 网络失败 → 重试 claim，**不** submit"。平台 `Inbox` 一见到 `event_id`
  就返回 False，没有"这次没处理完、下次再来"的口子。所以命令去重用自己的表，
  `processed_at IS NULL` 表示"投递过但没走完"，可被再次投递推进。见 C27-1。
- **`connector_request_receipt` 的 `outcome` 加 CHECK**：§5.2 的 DDL 只约束了
  `state`，`outcome` 是自由文本。四种结论是 §5.3 的闭集（`accepted|rejected|
  unknown|not_sent`），而 S26 会**按这个名字**分派状态机（`SUBMIT_OUTCOMES`），
  拼错一个字母就变成"未知结论"静默丢弃。所以收窄成枚举。见 C27-2。
- **`channel_health` 的 `retry_after_ms` 兼作"是否可自动恢复"**：§8.1 说 `auth`
  引起的 open **不自动恢复**，但 DDL 里没有列能区分它和 30 秒后自愈的那种。
  `retry_after_ms IS NULL` 表示"等人处理"，非空表示"到点转 half_open"。见 C27-3。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §11 的命令去重。`aggregate_id` 存上游给的 dispatch_id（该事件的聚合键），
-- 仅用于排障，不参与去重 —— 去重的键是 (consumer_id, event_id)。
CREATE TABLE IF NOT EXISTS connector_command_inbox (
  consumer_id   TEXT NOT NULL,
  event_id      TEXT NOT NULL,
  event_type    TEXT NOT NULL,
  aggregate_id  TEXT,
  payload_hash  TEXT NOT NULL,
  received_at   TEXT NOT NULL,
  processed_at  TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (consumer_id, event_id)
);

-- §5.2 的回执。**这是幂等的锚点**：`dispatch_id` 唯一，`stable_client_key`
-- 再加一道（§5.2 的"第二重保护"）。重发时重新生成 nonce 会在这里撞墙。
CREATE TABLE IF NOT EXISTS connector_request_receipt (
  receipt_id           TEXT PRIMARY KEY,
  dispatch_id          TEXT NOT NULL UNIQUE,
  provider_job_id      TEXT NOT NULL,
  tenant_id            TEXT NOT NULL,
  channel_route        TEXT NOT NULL,
  stable_client_key    TEXT NOT NULL,
  fencing_token        INTEGER NOT NULL,
  request_hash         TEXT NOT NULL,
  state                TEXT NOT NULL,
  outcome              TEXT,
  http_status          INTEGER,
  provider_job_ref     TEXT,
  error_class          TEXT,
  request_bytes_sha256 TEXT NOT NULL,
  sent_at              TEXT,
  responded_at         TEXT,
  -- §5.3 / §5.4：字节有没有离开本机。运行中的进程能可靠判定；崩溃恢复后的
  -- SENDING 一律按 unknown 处理，**哪怕这一列是 NULL**。
  bytes_written_at     TEXT,
  created_at           TEXT NOT NULL,
  CHECK (state IN ('SENDING','SENT','RESPONDED','ABANDONED')),
  CHECK (outcome IS NULL
         OR outcome IN ('accepted','rejected','unknown','not_sent'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_receipt_client_key
  ON connector_request_receipt (tenant_id, stable_client_key);
CREATE INDEX IF NOT EXISTS ix_receipt_stale
  ON connector_request_receipt (created_at) WHERE state = 'SENDING';
CREATE INDEX IF NOT EXISTS ix_receipt_ref
  ON connector_request_receipt (tenant_id, provider_job_ref);

-- §5.4：`provider_idempotency=unsupported` 的渠道里，这张表是 S26 对账时
-- **唯一**的本地证据 —— "这个 key 我发出过吗"。它不能证明供应商收到了。
CREATE TABLE IF NOT EXISTS connector_client_key_index (
  stable_client_key TEXT PRIMARY KEY,
  dispatch_id       TEXT NOT NULL,
  provider_job_ref  TEXT,
  channel_route     TEXT NOT NULL,
  tenant_id         TEXT NOT NULL,
  first_seen_at     TEXT NOT NULL
);

-- §11 的回调原文。`signature_ok` 恒为 true（验签失败不入库，§6.3）。
CREATE TABLE IF NOT EXISTS connector_callback_inbox (
  event_key     TEXT PRIMARY KEY,
  channel_route TEXT NOT NULL,
  tenant_id     TEXT,
  raw_body      BLOB NOT NULL,
  signature_ok  INTEGER NOT NULL,
  correlation   TEXT NOT NULL,
  state         TEXT NOT NULL,
  received_at   TEXT NOT NULL,
  mapped_at     TEXT,
  CHECK (signature_ok IN (0, 1)),
  CHECK (state IN ('RECEIVED','MAPPED','UNMAPPED','DEAD_LETTER'))
);
CREATE INDEX IF NOT EXISTS ix_callback_pending
  ON connector_callback_inbox (state, received_at);

-- §8.1 的本地熔断。这是**瞬时的传输层状态**，不是 S24 的账号能力事实。
-- S27 不拿它做路由，只上报。
CREATE TABLE IF NOT EXISTS channel_health (
  channel_route       TEXT PRIMARY KEY,
  breaker_state       TEXT NOT NULL,
  consecutive_fail    INTEGER NOT NULL DEFAULT 0,
  opened_at           TEXT,
  retry_after_ms      INTEGER,
  last_error_class    TEXT,
  updated_at          TEXT NOT NULL,
  CHECK (breaker_state IN ('closed','open','half_open'))
);
"""
