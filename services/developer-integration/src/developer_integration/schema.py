"""S46 自己的 schema。表来源是 `plans/S46-developer-integration.md` §5。

方案写的是 Postgres；这里逐条翻译到 SQLite：

- `jsonb` → `TEXT`（JCS 规范 JSON）
- `timestamptz` → `TEXT`（`_now()` 的定宽 UTC）
- `text[]` → `TEXT`（JSON 数组；事件类型名里可能含点号，逗号分隔会丢边界）
- `char(64)` → `TEXT` + CHECK（裸 hex；`sha256:` 前缀在服务层剥掉）
- `smallint` / `integer` / `bigint` 保持 INTEGER
- `boolean` → INTEGER 0/1
- `GENERATED ALWAYS AS IDENTITY` → `INTEGER PRIMARY KEY AUTOINCREMENT`（但
  outbox 不在这里建，见下）

四条结构性事实：

1. **S46 不建任何业务状态机。** 没有 `run_status`、没有 `budget_balance`、
   没有 `approval_state`。`integration_command_receipt.state` 是**回执**状态
   （受理了没有、转发到哪一步），不是业务状态。上游状态只在查询时实时取，
   `upstream_state` 列存的是「上一次快照」，带 `upstream_state_as_of` 一起读，
   不是当前真相（§1.2、§4.2）。
2. **`params` 只存摘要与字节数。** 不建 `request_body_log`，也不存 params
   正文：那是剧集剧情/台词草稿，S46 不是它们的权威（§5.2、§5.6、C46-11）。
3. **`external_id_mapping` 没有跨租户唯一索引。** 不同租户可以让同一个外部
   `order_id` 指向不同内部 id；跨租户唯一会泄漏存在性（E28、§5.6）。
4. **`api_key` 表里不得出现任何供应商凭据。** BYOK 归 S24/S26（E30）；
   这里只有平台签发给客户的 `ak_...`（§3.4 末段、§10.4）。

`outbox` / `inbox` / `idempotency_record` 由平台 SDK 建表（不在这里），与业务
事实同事务写。方案 §5.5 的 outbox 比 SDK 多四列（`aggregate_sequence` /
`partition_key` / `headers` / `event_type` 的组合唯一），登记在 CONTRACT_GAPS
（C46-OUTBOX-DDL）。
"""

ID_PREFIX = {
    "operation": "op_",
    "receipt": "rcpt_",
    "client": "clic_",
    "key": "key_",
    "subscription": "sub_",
    "delivery": "wd_",
    "mapping": "eim_",
}

SCHEMA_VERSION = "2.0"
SOURCE = "S46"
TOPIC = "drama.developer-integration.v2"
EVENT_COMMAND_ACCEPTED = "integration.command.accepted"
EVENT_TENANT_PARAM_IGNORED = "integration.tenant_param_ignored"
EVENT_FORWARD_UNKNOWN = "integration.forward.unknown"

# 两个 inbox 消费者：一个把外部事件扇出到 webhook，一个跑对账。
CONSUMER_WEBHOOK = "s46.webhook"
CONSUMER_RECONCILE = "s46.reconcile"

# §5 的 CHECK 枚举。照抄，不扩。
SCOPES = ("read", "plan", "execute", "review", "admin")
PRINCIPAL_TYPES = ("api_client", "mcp_client")
PLANS = ("standard", "enterprise", "dedicated")
CLIENT_STATES = ("ACTIVE", "SUSPENDED", "DELETED")
KEY_STATES = ("ACTIVE", "RETIRING", "REVOKED")
RECEIPT_STATES = ("ACCEPTED", "FORWARD_RETRYING", "FORWARDED", "REJECTED",
                  "FORWARD_UNKNOWN", "STALE_UNKNOWN", "CANCELLED")
WEBHOOK_STATES = ("ACTIVE", "SUSPENDED", "DELETED")
DELIVERY_STATES = ("PENDING", "DELIVERING", "DELIVERED", "FAILED", "CANCELLED")
INTERNAL_KINDS = ("run", "project", "episode", "delivery")

# §3.4：同时最多 2 个 ACTIVE、1 个 RETIRING；再建就是 409。
MAX_KEYS_ACTIVE = 2
MAX_KEYS_LIVE = 3
KEY_RETIRE_DAYS = 14

# §3.3：7 天窗口。超过即 STALE_UNKNOWN，只读展示，不再自动处理。
RECONCILE_WINDOW_SECONDS = 7 * 24 * 3600

# §7.2：有界退避，最多 5 次、总计 ≤ 2 分钟。
FORWARD_BACKOFF_SECONDS = (5, 10, 20, 40, 60)
MAX_FORWARD_ATTEMPTS = len(FORWARD_BACKOFF_SECONDS)

# §3.5：8 次、总计约 24h 的退避。
WEBHOOK_BACKOFF_SECONDS = (30, 120, 600, 1800, 3600, 10800, 21600, 43200)
MAX_WEBHOOK_ATTEMPTS = len(WEBHOOK_BACKOFF_SECONDS)

# §3.5 / T46-47：DELIVERING 中崩溃的租约。5 分钟后 janitor 重置为 PENDING。
DELIVERY_LEASE_SECONDS = 300

# §2.4：每个租户的订阅数上限（默认 20）。
MAX_WEBHOOK_SUBSCRIPTIONS = 20

# §4.3：`GET /operations/{id}` 每次实时查上游，不缓存超过 5s。
UPSTREAM_READ_TTL_SECONDS = 5.0

# §3.6 的三维限流。`standard` 是方案给的值；`enterprise` / `dedicated` 的
# 具体阈值是 C46-7（方案 §12.4 明说「需要商业与容量共同定」）。这里给的是
# **占位倍数**，不是承诺值，登记在 CONTRACT_GAPS。
QUOTA_DEFAULTS = {
    "standard": {"qps_total": 50, "qps_command": 10, "qps_query": 40,
                 "qps_webhook_admin": 5, "concurrency_execute": 10,
                 "concurrency_plan": 20, "concurrency_read": 50},
    "enterprise": {"qps_total": 200, "qps_command": 40, "qps_query": 160,
                   "qps_webhook_admin": 20, "concurrency_execute": 40,
                   "concurrency_plan": 80, "concurrency_read": 200},
    "dedicated": {"qps_total": 500, "qps_command": 100, "qps_query": 400,
                  "qps_webhook_admin": 50, "concurrency_execute": 100,
                  "concurrency_plan": 200, "concurrency_read": 500},
}

# 端点族。§3.6 的三维桶之一。
ENDPOINT_FAMILIES = ("command", "query", "webhook_admin")

# §4.5 的 data 键集合，与 `contracts/integration.command-accepted.v2.schema.json`
# 的 `required` 逐字相同（`additionalProperties: false`）。params 全文、URL
# 签名、任何凭据都不在事件里。
EVENT_DATA_KEYS = ("schema_version", "operation_id", "receipt_id", "command",
                   "state", "target", "api_client_id", "principal_type",
                   "client_trace_id")


CONTRACT_GAPS = [
    # ---------- 方案 §11 已登记的契约缺口（阻塞级在前） ----------
    {"id": "C46-1", "kind": "blocking",
     "summary": "`contracts/openapi.v2.yaml` 没有 `/integrations/commands`、"
                "`/operations/{id}`、`/webhook-subscriptions`、`/api-keys` 中的"
                "任何一条路径，而 `service.yaml` 声明了前两条",
     "handling": "本层按方案 §4 实现，路径与响应形状取自方案；不改契约。"
                 "外部 SDK 无法从此仓生成，是契约侧要补的活。"},
    {"id": "C46-2", "kind": "blocking",
     "summary": "没有「外部命令目录」的机器可读定义；§2.3 是方案里的表格",
     "handling": "命令目录落成 `service.COMMANDS` 代码常量（§2.3 末段明确要求"
                 "它不能在数据库里，防止运维改错）。新增命令要改代码。"},
    {"id": "C46-5", "kind": "blocking",
     "summary": "没有 `X-Drama-Signature` 的正式算法定义",
     "handling": "按 §3.5 的提案实现 `t=<ts>,v1=hex(hmac_sha256(secret, "
                 "'<ts>.' + raw_body))`，并在测试里用独立复算验证 T46-30。"},
    {"id": "C46-3", "kind": "gap",
     "summary": "没有 `api_client` / `api_key` / `client_scope_grant` 的实体"
                " schema，也没有它们与 S01 `principal` 的对应关系",
     "handling": "表结构取自方案 §5.1；`api_client` 与 S01 principal 的映射"
                 "缺省按 1:1 处理，`principal_id` 需由调用方在创建时提供。"},
    {"id": "C46-4", "kind": "gap",
     "summary": "`docs/13 §2` 列了 MCP scopes 但没定义 scope→命令的映射表",
     "handling": "`required_scope` 取自方案 §2.3 的表格，并与 "
                 "`contracts/mcp-tools.v2.design.json` 的 `required_scope` 对拍；"
                 "两边不一致时以方案为准并在此登记。"},
    {"id": "C46-6", "kind": "gap",
     "summary": "`event-topics.v2.json` 只登记了 `integration.command.accepted`，"
                "没有命令状态流转事件",
     "handling": "只发 `accepted`（§4.5 末段）。回执状态变化靠轮询，不靠事件；"
                 "S46 不发第二份状态事件，否则变成第二个状态源（§1.2）。"},
    {"id": "C46-7", "kind": "gap",
     "summary": "限流维度与 plan 分级没有正式定义",
     "handling": "`QUOTA_DEFAULTS` 里 `standard` 取方案的值，"
                 "`enterprise` / `dedicated` 是占位倍数；E35 的验收基准"
                 "需要 docs/03 或 customer-configs 的正式定义。"},
    {"id": "C46-8", "kind": "gap",
     "summary": "`service.yaml` 的 `depends_on` 只有 S01/S02/S07/S21，"
                "命令目录还调 S40/S43/S16/S18/S23/S24",
     "handling": "本服务不读 catalog 决定依赖；`COMMANDS` 的 `upstream_service`"
                 " 是真实调用面。部署依赖图不全由 catalog 侧修。"},
    {"id": "C46-9", "kind": "gap",
     "summary": "没有外部 API 的版本策略（`/v2/` 路径？`Accept` 头？）",
     "handling": "路径不带版本前缀；`schema_version` 只描述事件/内部契约，"
                 "与 HTTP API 版本是两套。保持现状直到 docs/13 补齐。"},
    {"id": "C46-10", "kind": "gap",
     "summary": "`x-expected-tenant` 头没有正式名字与错误码",
     "handling": "实现取 `X-Expected-Tenant`，不符返回 403 `TENANT_MISMATCH`，"
                 "响应不含任何租户标识。"},
    {"id": "C46-11", "kind": "gap",
     "summary": "回执只存 `params_sha256` / `params_bytes`，若下游需要重放"
                "入参则无法从 `upstream_ref` 重建",
     "handling": "按 §5.2 只存摘要；重放靠上游幂等（转发带 "
                 "`Idempotency-Key: <receipt.command_id>`），不靠回执里的正文。"},

    # ---------- 实现时新发现的缺口 ----------
    {"id": "C46-AUTHZ", "kind": "gap",
     "summary": "S01 的 `GET /authorization-context` 尚未实现；没有它就无法在"
                "每次判定时回源确认撤权（§10.1 要求 ≤30s 缓存 + 撤回即失效）",
     "handling": "`authorization_source` 可注入；未配置时只用本服务自己的 "
                 "`client_scope_grant`（S46 持有的授权凭证，§1.1）。配置了就"
                 "**fail closed**：源不可达 → 503，绝不放行。"},
    {"id": "C46-TARGET-404", "kind": "decision",
     "summary": "§2.2 要求 target 里的 id 必须在认证租户内存在，但 S46 不持有"
                "project/run 状态（§1.2 明令不得建第二权威）",
     "handling": "不在本地查存在性。上游 404 原样映射成 404（`NOT_FOUND`），"
                 "跨租户在上游同样是 404，因此不泄漏存在性（E28）。"},
    {"id": "C46-KMS", "kind": "gap",
     "summary": "Webhook secret 应在 KMS（§5.3 的 `secret_ref`），仓里没有 KMS",
     "handling": "用进程主密钥做 HMAC-SHA256 计数器流加密后存 `secret_ref`"
                 "（`local-v1:` 前缀），主密钥来自 `S46_SECRET_KEY`。满足"
                 "「明文不落库」与「可验签」，不满足跨实例/跨轮换托管。"},
    {"id": "C46-RECONCILE-PROBE", "kind": "gap",
     "summary": "对账需要回问上游「这条业务命令存在吗」（§3.3），但没有任何"
                "上游登记了这样的查询契约",
     "handling": "`upstream.probe()` 是可注入的端口；未配置时一律返回 "
                 "`unknown`，回执留在 `FORWARD_UNKNOWN` 等人工——**绝不猜成"
                 "「未执行」然后重提**（AGENTS.md 的重复付费禁令）。"},
    {"id": "C46-AUDIT-EVENT", "kind": "gap",
     "summary": "§10.5 的审计意图（`integration.tenant_param_ignored` 等）走"
                "本服务 outbox，但这些 event_type 不在 `event-topics.v2.json`",
     "handling": "按方案发；它们是审计意图不是领域事件。登记在此，等 S45/"
                 "contracts 侧给它们正式 topic。"},
    {"id": "C46-OUTBOX-DDL", "kind": "conflict",
     "summary": "§5.5 的 outbox 有 `aggregate_sequence` / `partition_key` / "
                "`headers` 与组合唯一约束；平台 `event_sdk` 的 outbox 没有",
     "handling": "用 SDK 的表；`sequence` 承担 `aggregate_sequence` 的角色，"
                 "分区键由 `partition_key = tenant_id:aggregate_id` 的规则在"
                 "消费方复算。不另建表——那会变成第二个 outbox。"},
    {"id": "C46-JWT", "kind": "gap",
     "summary": "OAuth2 access token（JWT `tid`）的验签在本服务外（S01 签、"
                "S01 给公钥），仓里没有可用的公钥来源",
     "handling": "HTTP 层只实现 API key 验凭据（哈希查表）。`AuthContext` 是"
                 "统一入口，JWT 路径接上时只换 `verify_credential` 的实现。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 客户端与凭据（§5.1） ----------
CREATE TABLE IF NOT EXISTS api_client (
  api_client_id   TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  name            TEXT NOT NULL,
  plan            TEXT NOT NULL DEFAULT 'standard'
                  CHECK (plan IN ('standard','enterprise','dedicated')),
  state           TEXT NOT NULL DEFAULT 'ACTIVE'
                  CHECK (state IN ('ACTIVE','SUSPENDED','DELETED')),
  contact_email   TEXT,
  principal_id    TEXT,
  principal_type  TEXT NOT NULL DEFAULT 'api_client'
                  CHECK (principal_type IN ('api_client','mcp_client')),
  home_region     TEXT NOT NULL,
  home_cell       TEXT NOT NULL,
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  UNIQUE (tenant_id, name)
);
CREATE INDEX IF NOT EXISTS ac_tenant_idx ON api_client (tenant_id, state);

CREATE TABLE IF NOT EXISTS api_key (
  kid             TEXT PRIMARY KEY,
  api_client_id   TEXT NOT NULL REFERENCES api_client(api_client_id),
  tenant_id       TEXT NOT NULL,
  secret_hash     TEXT NOT NULL
                  CHECK (length(secret_hash) = 64
                         AND secret_hash NOT GLOB '*[^0-9a-f]*'),
  kid_prefix      TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'ACTIVE'
                  CHECK (state IN ('ACTIVE','RETIRING','REVOKED')),
  scopes_snapshot TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  created_by      TEXT NOT NULL,
  retire_at       TEXT,
  revoked_at      TEXT,
  last_used_at    TEXT,
  CONSTRAINT ak_retiring_has_time
    CHECK (state <> 'RETIRING' OR retire_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ak_client_idx ON api_key (api_client_id, state);
CREATE INDEX IF NOT EXISTS ak_tenant_idx ON api_key (tenant_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS ak_hash_uniq ON api_key (secret_hash);

CREATE TABLE IF NOT EXISTS client_scope_grant (
  api_client_id   TEXT NOT NULL REFERENCES api_client(api_client_id),
  scope           TEXT NOT NULL
                  CHECK (scope IN ('read','plan','execute','review','admin')),
  granted_by      TEXT NOT NULL,
  granted_at      TEXT NOT NULL,
  expires_at      TEXT,
  revoked_at      TEXT,
  PRIMARY KEY (api_client_id, scope)
);

-- ---------- 命令回执（§5.2） ----------
CREATE TABLE IF NOT EXISTS integration_command_receipt (
  receipt_id      TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  operation_id    TEXT NOT NULL,
  command_id      TEXT NOT NULL,
  request_hash    TEXT NOT NULL
                  CHECK (length(request_hash) = 64
                         AND request_hash NOT GLOB '*[^0-9a-f]*'),
  api_client_id   TEXT NOT NULL,
  kid             TEXT NOT NULL,
  principal_type  TEXT NOT NULL
                  CHECK (principal_type IN ('api_client','mcp_client')),
  delegation_id   TEXT,
  command         TEXT NOT NULL,
  target          TEXT NOT NULL,
  params_sha256   TEXT NOT NULL
                  CHECK (length(params_sha256) = 64
                         AND params_sha256 NOT GLOB '*[^0-9a-f]*'),
  params_bytes    INTEGER NOT NULL CHECK (params_bytes >= 0),
  params_redacted INTEGER NOT NULL DEFAULT 0,
  state           TEXT NOT NULL
                  CHECK (state IN ('ACCEPTED','FORWARD_RETRYING','FORWARDED',
                                   'REJECTED','FORWARD_UNKNOWN','STALE_UNKNOWN',
                                   'CANCELLED')),
  attempt_count   INTEGER NOT NULL DEFAULT 0,
  reconcile_count INTEGER NOT NULL DEFAULT 0,
  upstream_service TEXT,
  upstream_ref    TEXT,
  upstream_state  TEXT,
  upstream_state_as_of TEXT,
  upstream_watermark INTEGER,
  rejection       TEXT,
  client_trace_id TEXT,
  external_id     TEXT,
  accepted_at     TEXT NOT NULL,
  forwarded_at    TEXT,
  last_attempt_at TEXT,
  next_attempt_at TEXT,
  CONSTRAINT icr_op_uniq UNIQUE (tenant_id, operation_id),
  CONSTRAINT icr_cmd_uniq UNIQUE (tenant_id, command_id)
);
CREATE INDEX IF NOT EXISTS icr_state_idx
  ON integration_command_receipt (state, next_attempt_at)
  WHERE state IN ('ACCEPTED','FORWARD_RETRYING','FORWARD_UNKNOWN');
CREATE INDEX IF NOT EXISTS icr_tenant_idx
  ON integration_command_receipt (tenant_id, accepted_at);
CREATE INDEX IF NOT EXISTS icr_upstream_idx
  ON integration_command_receipt (tenant_id, upstream_service, forwarded_at)
  WHERE upstream_ref IS NOT NULL;

-- ---------- Webhook（§5.3） ----------
CREATE TABLE IF NOT EXISTS webhook_subscription (
  subscription_id TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  api_client_id   TEXT,
  url             TEXT NOT NULL,
  event_types     TEXT NOT NULL,
  secret_ref      TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'ACTIVE'
                  CHECK (state IN ('ACTIVE','SUSPENDED','DELETED')),
  description     TEXT,
  created_at      TEXT NOT NULL,
  rotated_at      TEXT,
  previous_secret_ref TEXT,
  previous_secret_expires_at TEXT
);
CREATE INDEX IF NOT EXISTS ws_tenant_idx ON webhook_subscription (tenant_id, state);

CREATE TABLE IF NOT EXISTS webhook_delivery (
  delivery_id     TEXT PRIMARY KEY,
  subscription_id TEXT NOT NULL
                  REFERENCES webhook_subscription(subscription_id),
  tenant_id       TEXT NOT NULL,
  event_id        TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  partition_key   TEXT NOT NULL,
  payload         TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'PENDING'
                  CHECK (state IN ('PENDING','DELIVERING','DELIVERED','FAILED',
                                   'CANCELLED')),
  attempt_count   INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  -- 认领 `DELIVERING` 时写入的租约。janitor 只回收过期租约（T46-47），
  -- 不用 `created_at` 推断——那会把「刚认领、正在发」的行也当垃圾。
  lease_expires_at TEXT,
  last_response_code INTEGER,
  last_error      TEXT,
  delivered_at    TEXT,
  created_at      TEXT NOT NULL,
  CONSTRAINT wd_uniq UNIQUE (subscription_id, event_id)
);
CREATE INDEX IF NOT EXISTS wd_pending_idx
  ON webhook_delivery (next_attempt_at) WHERE state = 'PENDING';
CREATE INDEX IF NOT EXISTS wd_tenant_idx
  ON webhook_delivery (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS wd_partition_idx
  ON webhook_delivery (partition_key, created_at)
  WHERE state IN ('PENDING','DELIVERING');

-- ---------- 外部 ID 映射与限流参数（§5.4） ----------
CREATE TABLE IF NOT EXISTS external_id_mapping (
  tenant_id       TEXT NOT NULL,
  external_system TEXT NOT NULL,
  external_id     TEXT NOT NULL,
  internal_kind   TEXT NOT NULL
                  CHECK (internal_kind IN ('run','project','episode','delivery')),
  internal_id     TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, external_system, external_id)
);
CREATE INDEX IF NOT EXISTS eim_internal_idx
  ON external_id_mapping (tenant_id, internal_kind, internal_id);

CREATE TABLE IF NOT EXISTS tenant_quota_profile (
  tenant_id           TEXT PRIMARY KEY,
  plan                TEXT NOT NULL
                      CHECK (plan IN ('standard','enterprise','dedicated')),
  qps_total           INTEGER NOT NULL,
  qps_command         INTEGER NOT NULL,
  qps_query           INTEGER NOT NULL,
  qps_webhook_admin   INTEGER NOT NULL,
  concurrency_execute INTEGER NOT NULL,
  concurrency_plan    INTEGER NOT NULL,
  concurrency_read    INTEGER NOT NULL,
  updated_at          TEXT NOT NULL,
  updated_by          TEXT NOT NULL
);
"""
