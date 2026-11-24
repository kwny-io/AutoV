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

# 本轮**不解决**的缺口。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要
# 自行选边」：本服务按方案散文实现，把偏差记在这里，不修改契约、不代填决定。
CONTRACT_GAPS = [
    {"id": "POL-1", "kind": "conflict",
     "summary": "`contracts/service-catalog.v2.json`、生成出的 "
                "`service.yaml` 与 `service.md` 都把本服务的查询入口写成 "
                "`POST /policy-decisions:evaluate`，而代码只有 "
                "`POST /policy-decisions:resolve-effective`。§7 的 "
                "`PolicyDecision`（四值 verdict、`single_use`、`expires_at`"
                "≤60s）根本不存在。消费方已经各自登记了同一件事："
                "S19 C4、S43 deps、S07 S07-11、S26 §4 的三条复查也不含 S02",
     "handling": "方案 §13.1 把 §7 的 `evaluate` 与 §8 的派单前实时复查、"
                 "§10.2 的 kill-switch 旁路表登记为「本轮没做，留给下一轮」；"
                 "代码按这个边界实现，只提供 `:resolve-effective`。"
                 "**不改 catalog、不改 service.yaml**（那是生成物）——"
                 "要么下一轮补齐 `:evaluate`，要么 owner 决定把 catalog 改成"
                 "声明 `:resolve-effective`。当前两者都不是本服务能单方面选的。"},
    {"id": "POL-2", "kind": "gap",
     "summary": "派单前的实时复查没有任何一处由本服务承台。§8 的 12 条里，"
                "本服务只承第 1、2、10 条（策略未被收紧取代 / window 未过期 / "
                "gate 已满足），而这三条都挂在尚未实现的 `dispatch_admission` "
                "上；第 3、4 条的权威是 S01（第 4 条 provider 级读 S22），"
                "本服务连 `safety_tightening` 表（§10.1 行 683）都没有——"
                "它是「紧急收紧绕过常规 checkpoint」的承载物（§10.2 行 723），"
                "缺了就只剩 revision 解析这一条慢路径。S26 的派单前复查"
                "（§4 步骤 4）只覆盖 S23 预算（第 6 条）、S22 配额（第 7 条）"
                "与 S24 能力（§8 里没有对应行），其余十行一条都没走。"
                "`budget-ledger:1404` "
                "写的「全停由 S02 的 kill switch 令 S26 停发 Saga」"
                "**尚无落点**",
     "handling": "不伪造 kill switch：没有 `safety_tightening` 表、没有 "
                 "`evaluate`、没有 `dispatch_admission`，就不声称有。"
                 "本服务不做「第 4 条我代 S01 答」这种事（那是让 S02 变成"
                 "第二个权威）。全停目前只能靠 S01 的 "
                 "`POST /tenants/{id}:suspend`：它 bump `revocation_epoch` 并"
                 "拒绝签发新 TCT；但 `X-Tenant-Id` 现在是各服务直接信任的明文"
                 "头（S26 只做 body 比对、不验签），所以在途调用拦不住。"
                 "这是已知未实现，补它的前置条件是 POL-1 的 `:evaluate` 面。"},
    {"id": "POL-3", "kind": "conflict",
     "summary": "`_validate_document_shape` 只查必填键与枚举，不拒绝"
                "契约 `additionalProperties: false` 禁止的多余键。"
                "实测：一份带 `routing.flags` 的文档能发布、入库、并按 "
                "`GET /policies/{id}/revisions/{rev}` 原样回源——而 "
                "`automation-policy.v2.schema.json` 拒绝该键。这是 S25 "
                "MRT-22 的上游一半",
     "handling": "运行期不 import jsonschema（其余服务同样只用 stdlib），"
                 "所以只做方案 §4.5 列出的那些语义校验。是否补 "
                 "`additionalProperties` 检查要 owner 定，而两边都要看见："
                 "现在补，S25 会读不到 `routing`，退回 `scoring.DEFAULT_WEIGHTS`"
                 "与空 flags —— 它不会 503（`deps.py:93` 把「没有 routing 段」"
                 "当成「策略没说」），于是一条恒定读不到 kill_switch；"
                 "不补，则「文档过了 schema」不能当作契约满足，带 `routing` "
                 "的文档在严格消费方那里一样过不去。不修改 contracts/。"},
]
