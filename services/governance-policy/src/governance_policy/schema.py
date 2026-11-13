"""S02 自己的 schema。别的服务不 import 这里，只透过 HTTP 契约交互。

表的来源是 `plans/S02-governance-policy.md` §10.1（行 646-711）。三个区别要
先说清楚，因为它们决定了本轮代码能证明什么：

1. **没有 `jsonb`/`timestamptz`。** 本仓服务层一律 SQLite（`isolation_level=None`
   自动提交 + 显式 `Transaction`），所以 JSON 存 TEXT、时间存定宽 UTC 字符串
   （见 `service._ts`）。字符串比较等价于时间比较这件事必须在写入前保证，
   否则 `effective_window` 的区间求交会静默错。
2. **`outbox`/`inbox` 由 `event_sdk` 建表**，这里不重复 DDL。
3. **`policy_scope_binding` 保留 `revision` + `content_hash` 两列**（§10.1 行 659）：
   缓存与解析都按「指针指向哪一版」取，只存 `policy_id` 无法发现指针漂移，
   而 §4.4 行 210-212 明确要求漂移时 `RevisionSuperseded` 而不是猜。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §10.1 行 647：只 INSERT，永不 UPDATE/DELETE。
CREATE TABLE IF NOT EXISTS automation_policy_revision (
  tenant_id    TEXT NOT NULL,
  policy_id    TEXT NOT NULL,
  revision     INTEGER NOT NULL CHECK (revision >= 1),
  content_hash TEXT NOT NULL,
  document     TEXT NOT NULL,
  effective_at TEXT NOT NULL,
  created_by   TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, policy_id, revision)
);

-- §10.1 行 653：发布指针。CAS 就打在 current_revision 这一行上。
CREATE TABLE IF NOT EXISTS policy_publish_pointer (
  tenant_id        TEXT NOT NULL,
  policy_id        TEXT NOT NULL,
  current_revision INTEGER NOT NULL,
  content_hash     TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, policy_id)
);

-- §10.1 行 658。scope_type ∈ {tenant, project, episode, stage, node}；
-- 方案里的 overrides 枚举只有后四种，`tenant` 是「企业层文档」的绑定方式
-- （§4.2 行 146 的 A 类 layer）。
CREATE TABLE IF NOT EXISTS policy_scope_binding (
  tenant_id    TEXT NOT NULL,
  scope_type   TEXT NOT NULL CHECK (scope_type IN
                 ('tenant','project','episode','stage','node')),
  scope_id     TEXT NOT NULL,
  policy_id    TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, scope_type, scope_id)
);

CREATE INDEX IF NOT EXISTS policy_revision_by_policy
  ON automation_policy_revision (tenant_id, policy_id, revision DESC);
"""

# §5.1 行 380-384 的缓存策略在代码里是「进程内 TTL 字典」。键里带 content_hash，
# 所以指针一变就自然未命中，不需要显式失效——这也是为什么指针表要存 hash。
CACHE_TTL_SECONDS = 5.0
