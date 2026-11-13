"""S47 自己的 schema。表来源是 `plans/S47-realtime-projection.md` §5。

五条结构性事实，它们决定这个库能证明什么、不能证明什么：

1. **这里是缓存，不是账本。** `run_dashboard_projection` 可以被任意丢弃、任意
   重建、任意滞后（§1.2）。它唯一不可省的义务是**如实标注自己滞后多少**：
   `applied_watermark` / `last_event_time` / `projection_state` 三个字段就是
   这个义务的物理形态。少了它们，投影就会伪装成权威。
2. **没有任何"剩余余额"列。** 只有 `spent_micros`（已结算累计，不倒退）与
   `remaining_ref`（一个指针，指向 S23）。§1.2 / §3.5 的最后一行是硬规则：
   余额是当前状态，会变，投影必然滞后，所以根本不放。
3. **也没有授权态、accepted pointer、正史、放行依据。** 见 §5.6 的"不建的表"。
   这张库里能存"审核发生过"的展示事实，但没有任何代码路径把它读成放行依据。
4. **`projection_state` 只有四个值，`ABSENT` 不是一个值。** 方案 §3.2 的状态图
   有五个节点，但 DDL 的 `CHECK` 只有四个（PCA-3）：`ABSENT` 是"行不存在"，
   用主键查不到来表达，不是一行状态为 ABSENT 的记录。多存一行 `ABSENT` 会让
   "这个 run 从来没有过投影"与"这个 run 被清理了"看起来一样。
5. **事件与投影同事务。** `outbox` / `inbox` 由平台 SDK 建表（不在本文件），
   但 `append` 必须发生在写投影行与 change_log 的同一个 `COMMIT` 里。分开写就
   有一致性空洞：投影提交了游标事件丢了、或游标发了投影回滚了。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`text[]` 存 JCS 化的
JSON 数组文本。`bigint`/`smallint` 在 SQLite 里都是 INTEGER，不额外收窄——收窄
会在 32 位边界上制造一个方案没要求的失败分支。
"""

from __future__ import annotations

ID_PREFIX = {
    "subscription": "sub_",
    "cursor": "cur_",
    "delivery": "ndl_",
    "notification": "ntf_",
}

# 消费侧的 consumer 名。§6.1 三处幂等里的第一处就是它，写成常量是为了让
# "投影"与"通知"两条去重线不会共用同一个键空间（一条事件对投影只应用一次，
# 但对多个用户/渠道要多条通知）。
CONSUMER_PROJECTION = "s47.projection"
CONSUMER_NOTIFY = "s47.notify"

# §3.3：只有 A 类 topic 做缺号检测。这张表必须是**显式白名单**，不能写成
# "有 sequence 就检测"——每个事件都有 sequence，而 §2.2 明说其它 topic 不保证
# 无洞。多检测一个 topic 的代价是满屏假的 GAPPED。
CLASS_A_EVENTS = frozenset({"orchestration.run.state-changed"})

# §3.3 的 B 类时钟容忍：供应商时钟不可靠，默认 30s。
CLOCK_SKEW_TOLERANCE_MS = 30_000

# §3.7 游标保留窗口。C47-5 说这个数字在契约里没有基准，这里是方案的默认值。
CURSOR_RETENTION_SECONDS = 15 * 60

# §3.8 回源频率上限：每 run 每分钟最多 6 次。
RESYNC_MAX_PER_MINUTE = 6

# §6.5 连接数上限。超过一律 429，不是排队。
CONNECTION_LIMITS = {"tenant": 500, "principal": 10, "run": 200}

# §3.9 通知白名单的默认值。表里写的是"默认"，用户偏好可以只收窄不加宽——
# 但静音不了"其它"类：那些本来就不通知，要收必须显式订阅。
DEFAULT_NOTIFY_TYPES = frozenset({
    "run.failed", "run.blocked", "run.completed",
    "review.requested", "delivery.sealed", "budget.threshold.reached",
})

# §3.9：quiet hours 内仍然照发的紧急类型。
URGENT_NOTIFY_TYPES = frozenset({"run.failed", "budget.threshold.reached"})

# 能落成 notification_delivery 的渠道。`webhook` 在 C47-7 / C46-6 补上之前
# 不可用（§10.4），所以它不在这个集合里——写进来只会得到一条永远 PENDING 的行。
ENABLED_CHANNELS = frozenset({"in_app", "email"})
KNOWN_CHANNELS = frozenset({"in_app", "email", "webhook"})

# §4.3 的 `event_class` 枚举。C47-4 说契约里没有它，这里是方案的取值集合，
# 保守地只收 SSE 客户端 addEventListener 真正会用到的粗粒度类别。
EVENT_CLASSES = frozenset({
    "run.state", "node.state", "artifact", "review", "warning", "delivery",
})

# 不进投影的 topic/事件：消费但只做通知与审计，不改 run 行。
# 写成常量而不是散落在 if 里，是为了让"投影面"一眼可数。
NON_PROJECTING_EVENTS = frozenset({
    "model.route.selected",
    "connector.observation.received",
    "integration.command.accepted",
})

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §5.1 投影表。**缓存语义**：无余额列、无授权列、无 accepted pointer 列。
CREATE TABLE IF NOT EXISTS run_dashboard_projection (
  tenant_id           TEXT NOT NULL,
  run_id              TEXT NOT NULL,
  project_id          TEXT NOT NULL,
  episode_id          TEXT NOT NULL,
  branch_id           TEXT NOT NULL,

  projection_state    TEXT NOT NULL DEFAULT 'LIVE'
    CHECK (projection_state IN ('LIVE','GAPPED','TERMINAL','EVICTED')),
  applied_watermark   INTEGER NOT NULL DEFAULT 0,
  -- 不在 §5.1 的 DDL 里，见 C47-23。它是**展示序号**：每 run 单调 +1，
  -- 供 SSE 的 `id` 与 change_log 主键使用。A 类事件密集时它与
  -- applied_watermark 取同一个值（§4.3 的「id 就是 projection_watermark」
  -- 因此字面成立）；B 类事件（ledger / artifact / delivery…）没有上游序，
  -- 没有它就写不出 change_log 的 `(tenant, run, watermark)` 主键。
  change_seq          INTEGER NOT NULL DEFAULT 0,
  source_watermark    INTEGER,
  gap                 TEXT,
  last_event_time     TEXT,

  run_state           TEXT,
  state_since         TEXT,
  -- 仅展示用；权威在 S08/S21（§5.1）。读它的函数名叫
  -- `read_manifest_revision_for_display`，命名即约束。
  manifest_revision   INTEGER,
  current_node        TEXT,
  completed_shots     INTEGER NOT NULL DEFAULT 0,
  total_shots         INTEGER,
  spent_micros        INTEGER NOT NULL DEFAULT 0,
  spent_as_of         TEXT,
  warnings            TEXT NOT NULL DEFAULT '[]',
  artifacts           TEXT NOT NULL DEFAULT '[]',

  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  evicted_at          TEXT,

  PRIMARY KEY (tenant_id, run_id)
);
CREATE INDEX IF NOT EXISTS rdp_active_idx
  ON run_dashboard_projection (tenant_id, run_state, updated_at DESC)
  WHERE projection_state <> 'EVICTED';
CREATE INDEX IF NOT EXISTS rdp_gapped_idx
  ON run_dashboard_projection (tenant_id, updated_at)
  WHERE projection_state = 'GAPPED';

-- §5.2 变更日志。SSE 续连的回放源；只留 15 分钟。
-- `payload` 是**已过 PROJECTION_SECRET_SCAN 的**（§5.2 下文），命中只换叶子。
CREATE TABLE IF NOT EXISTS projection_change_log (
  tenant_id      TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  watermark      INTEGER NOT NULL,
  event_class    TEXT NOT NULL,
  payload        TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, run_id, watermark)
);
CREATE INDEX IF NOT EXISTS pcl_ttl_idx ON projection_change_log (created_at);

-- §5.3 游标。**不存事件内容**，只存位置（§3.7）。续连时从 change_log 回放。
CREATE TABLE IF NOT EXISTS sse_cursor (
  subscription_id     TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  principal_id        TEXT NOT NULL,
  run_id              TEXT NOT NULL,
  channel             TEXT NOT NULL DEFAULT 'sse',
  last_sent_watermark INTEGER NOT NULL,
  created_at          TEXT NOT NULL,
  last_activity_at    TEXT NOT NULL,
  expires_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sc_expiry_idx ON sse_cursor (expires_at);
CREATE INDEX IF NOT EXISTS sc_run_idx ON sse_cursor (tenant_id, run_id);

-- §5.3 订阅登记。
CREATE TABLE IF NOT EXISTS subscription_registration (
  subscription_id TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  principal_id    TEXT NOT NULL,
  scope_kind      TEXT NOT NULL CHECK (scope_kind IN ('run','tenant')),
  scope_id        TEXT,
  event_filters   TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'ACTIVE'
    CHECK (state IN ('ACTIVE','CLOSED','EXPIRED')),
  since_watermark INTEGER,
  created_at      TEXT NOT NULL,
  closed_at       TEXT
);
CREATE INDEX IF NOT EXISTS sr_principal_idx
  ON subscription_registration (tenant_id, principal_id, state);

-- §5.4 通知偏好。
CREATE TABLE IF NOT EXISTS notification_preference (
  tenant_id    TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  event_types  TEXT NOT NULL,
  channels     TEXT NOT NULL,
  quiet_hours  TEXT,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, principal_id)
);

-- §5.4 投递回执。`nd_uniq` 是 §6.1 的第三处幂等。
CREATE TABLE IF NOT EXISTS notification_delivery (
  delivery_id    TEXT PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  principal_id   TEXT NOT NULL,
  event_id       TEXT NOT NULL,
  event_type     TEXT NOT NULL,
  channel_kind   TEXT NOT NULL CHECK (channel_kind IN ('in_app','email','webhook')),
  channel_ref    TEXT,
  state          TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (state IN ('PENDING','DEFERRED','SENT','FAILED','CANCELLED')),
  deferred_until TEXT,
  attempt_count  INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT,
  sent_at        TEXT,
  created_at     TEXT NOT NULL,
  CONSTRAINT nd_uniq UNIQUE (tenant_id, principal_id, event_id, channel_kind)
);
CREATE INDEX IF NOT EXISTS nd_pending_idx
  ON notification_delivery (state, deferred_until)
  WHERE state IN ('PENDING','DEFERRED');
CREATE INDEX IF NOT EXISTS nd_user_idx
  ON notification_delivery (tenant_id, principal_id, created_at DESC);

-- §5.4 站内通知。`body_ref` 是指针，不存正文（§10.7 的同一条纪律）。
CREATE TABLE IF NOT EXISTS in_app_notification (
  notification_id TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  principal_id    TEXT NOT NULL,
  event_id        TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  title           TEXT NOT NULL,
  body_ref        TEXT,
  run_id          TEXT,
  read_at         TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ian_unread_idx
  ON in_app_notification (tenant_id, principal_id, created_at DESC)
  WHERE read_at IS NULL;
"""

# 本轮**不解决**的缺口。`contracts/projection.cursor-advanced.v2.schema.json`
# 里已有 PCA-1…PCA-8 的 `x-conflicts` 与四条 `x-needs-owner`；方案 §11 又
# 登记了 C47-1…C47-14。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要
# 自行选边」处理：本服务按方案散文实现，把偏差记在这里，不改契约、不代填决定。
CONTRACT_GAPS = [
    # --- 方案 §11 已登记的 ---
    {"id": "C47-1", "kind": "conflict",
     "summary": "openapi.v2.yaml 只有 GET /runs/{run_id}/events（streamRun），"
                "没有 POST /subscriptions、GET /runs/{run_id}/snapshot、GET /runs、"
                "/notification-preferences、/notifications",
     "handling": "按方案 §4.1 全量实现，不改契约；这些路径在 SDK 生成前都在"
                 "契约外，调用方只能按方案用。"},
    {"id": "C47-2", "kind": "conflict",
     "summary": "projection_watermark 的「每 run 单调无洞」只在 S21 方案散文里，"
                "不在任何 schema；上游改语义时 S47 会静默误报 GAPPED",
     "handling": "把该性质写成 CLASS_A_EVENTS 白名单的显式前提，并让缺号真的"
                 "产出 GAPPED + PROJECTION_GAP 告警（可见，不是静默）。语义本身"
                 "待 contracts/event-envelope.v2.schema.json 或 docs/04 锁死。"},
    {"id": "C47-3", "kind": "gap",
     "summary": "无 run-snapshot.v1 schema；__hints.authority 是方案 §4.2 的提案",
     "handling": "按 §4.2 的示例形状实现，字段名与层级不改；契约待补。"},
    {"id": "C47-4", "kind": "conflict",
     "summary": "SSE 的 id/event/data 形状与 event_class 枚举未定义",
     "handling": "用方案 §4.3 的五个类别（run.state/node.state/artifact/review/"
                 "warning）加 delivery，定义在 EVENT_CLASSES；契约与 docs/13 待补。"},
    {"id": "C47-5", "kind": "gap",
     "summary": "游标保留 15 分钟、连接数上限、410 的 body 形状全无契约定义",
     "handling": "常量取方案默认值（CURSOR_RETENTION_SECONDS / CONNECTION_LIMITS）；"
                 "410 body 按 §3.7 带 snapshot_url 与 do_not_resubmit。基准待 docs/03。"},
    {"id": "C47-6", "kind": "gap",
     "summary": "projection.cursor.advanced 的节流语义（每订阅每秒 ≤1）没写进"
                "event-topics.v2.json",
     "handling": "实现里按订阅做 1 秒桶；下游若期待「每条 SSE 一条事件」会看到"
                 "跳号，这是 PCA-1 的同一件事，登记不改。"},
    {"id": "C47-7", "kind": "gap",
     "summary": "通知渠道枚举与 webhook 走 S46 的路径未定义（见 C46-6）",
     "handling": "webhook 可存但不可投：ENABLED_CHANNELS 只有 in_app/email，"
                 "webhook 投递记 FAILED + last_error=CHANNEL_NOT_AVAILABLE，"
                 "不静默丢弃（§10.4 要求写在 UI 上）。"},
    {"id": "C47-8", "kind": "gap",
     "summary": "service.yaml 的 depends_on 只有 [S01]，但本服务消费 10 个 topic",
     "handling": "不改编排元数据（catalog 派生，不手改）；运行期按事件到达处理，"
                 "缺哪个上游就只是那一类投影不更新，不阻塞其它。"},
    {"id": "C47-9", "kind": "needs_owner",
     "summary": "S42 的段级渲染进度事件未定义，S47 无法显示渲染进度条",
     "handling": "不实现段级进度；未知事件类型按 §7.1 忽略并计数。"},
    {"id": "C47-10", "kind": "gap",
     "summary": "无「投影可重建」的量化验收标准（重建耗时、重建期间行为）",
     "handling": "实现 replay 入口与 EVICTED 重建路径，能证明可从事件流恢复；"
                 "耗时预算待 docs/16 或 SLO 条目。"},
    {"id": "C47-11", "kind": "conflict",
     "summary": "openapi 的 streamRun 标了 x-service-id S47，但 service.yaml 的"
                "api 声明只有 command/query 两栏，字段集不同",
     "handling": "以 openapi 的 operation 为准实现该路径；service.yaml 由 catalog"
                 "派生，不在此改。"},
    {"id": "C47-12", "kind": "conflict",
     "summary": "§2.1 写消费 orchestration.node.state-changed，注册表里 S21 只登记"
                "orchestration.run.state-changed（C21-5：全部信息塞进 data）",
     "handling": "不订阅不存在的类型；node.state 从 run 事件的 data 里派生"
                 "（completed_nodes/blocked_nodes/failed_nodes/current_stage），"
                 "§3.4 的 node 级合并因此退化到 run 级。上游拆事件或改合并规则"
                 "需 owner 定。"},
    {"id": "C47-13", "kind": "conflict",
     "summary": "§2.1 写消费 artifact.registered / artifact.deleted，注册表里 S28"
                "只有 artifact.ingested（C28-3：tombstone 折叠进 data）",
     "handling": "只消费 artifact.ingested。删除事实在 data 里若出现就取，"
                 "没出现就不知道——不把「没有删除事件」读成「没被删」。"},
    {"id": "C47-14", "kind": "conflict",
     "summary": "§2.1 写消费 budget.reservation.settled，注册表里只有"
                "finance.ledger.entry-posted（S23：只发这一种，语义在 data）",
     "handling": "消费 finance.ledger.entry-posted，只取 SETTLE/ADJUST 的累计"
                 "consumed（data.budget_after.consumed_micro）与 settled_micro；"
                 "RESERVE/RELEASE 不进 spent_micros。余额仍不投影。"},

    # --- 实现时新发现的 ---
    {"id": "C47-15", "kind": "conflict",
     "summary": "run_dashboard_projection 的 project_id/episode_id/branch_id 是"
                "NOT NULL，而 orchestration.run.state-changed 的 data 13 个字段里"
                "一个都没有这三个坐标",
     "handling": "接收消费时从事件信封/可选字段尽力取；取不到就写空串并在"
                 "warnings 里记 PROJECTION_COORDINATE_MISSING，而不是伪造一个"
                 "看起来合法的 id（伪造会把跨 run 的看板混在一起）。坐标来源需"
                 "S21 在 data 里补，或 S47 增加一次只读的 GET /runs/{id} 回源。"},
    {"id": "C47-16", "kind": "conflict",
     "summary": "§4.2 的 progress.completed_shots / total_shots / current_node 在"
                "S21 事件里对应的是 completed_nodes / 无 total / 无 current_node",
     "handling": "completed_shots 存 data.completed_nodes 的值（语义是「完成数」，"
                 "名字按 §4.2 的对外形状）；total_shots 与 current_node 没有来源就"
                 "保持 NULL/None，不拿 blocked_nodes 之类的近义字段凑。"},
    {"id": "C47-17", "kind": "gap",
     "summary": "event_sdk 的 outbox 只有 event_id 主键，没有方案 §5.5 的"
                "UNIQUE(aggregate_id, aggregate_sequence, event_type)",
     "handling": "节流在应用层保证（同订阅同 watermark 只 append 一次）；"
                 "库层无第二道约束，登记不补平台表结构。"},
    {"id": "C47-18", "kind": "needs_owner",
     "summary": "§3.9 的 budget.threshold.reached 与 §2.1 的 budget 事件都不在"
                "注册表；S23 说告警走 data.budget_after.state",
     "handling": "通知白名单保留该类型（未来语义），当前没有事件能触发它；"
                 "从 finance.ledger.entry-posted 的 budget_after 判阈值属于"
                 "S23 的语义，不在此自行发明。"},
    {"id": "C47-19", "kind": "conflict",
     "summary": "PCA-1：aggregate_sequence = watermark 且每订阅每秒 ≤1，使序号"
                "必然跳号；PCA-2：lag_ms 无算式；PCA-4：水位 0 帧发不出",
     "handling": "lag_ms 取「消费者处理时刻 − 事件 time」（可自证、不受 GAPPED "
                 "影响的那一种读法，PCA-2 两个读法里更保守的）；watermark=0 不"
                 "发（信封 sequence 最小 1），改由 snapshot 的 projection_watermark=0 "
                 "表达同一事实；跳号如实发生，下游不得按它做缺号检测。"},
    {"id": "C47-20", "kind": "needs_owner",
     "summary": "PCA-5：subscription_registration 允许 scope_kind='tenant'，而"
                "sse_cursor.run_id NOT NULL、cursor 事件 run_id 必需",
     "handling": "scope_kind='tenant' 的订阅可创建（登记），但不建游标、不发 "
                 "cursor 事件——租户级流用 GET /runs 轮询或 /dashboard/summary，"
                 "不假装能在这个形状里跑 SSE。"},
    {"id": "C47-21", "kind": "conflict",
     "summary": "PCA-8：§4.4 示例的 correlation_id 是 null，而信封要求 "
                "string minLength 1；PCA-7：TERMINAL/EVICTED 在「游标前进才发」"
                "下只剩一帧且是否算前进未写",
     "handling": "correlation_id 用 subscription_id（每帧都有、非空、可追溯）；"
                 "TERMINAL 那一帧算前进（state 变了就是事实），EVICTED 不发"
                 "（被清理不是游标前进）。"},
    {"id": "C47-22", "kind": "gap",
     "summary": "redaction-policy 要求 k_redact_scope 由 KMS 按租户/类持有并"
                "随类轮换，仓库里没有 KMS",
     "handling": "进程内派生（HMAC(root, tenant:class)）并让 root 稳定在实例上："
                 "同值同 token 成立，跨实例/跨轮换的稳定性不成立。待 KMS 接入。"},
    {"id": "C47-23", "kind": "conflict",
     "summary": "§3.2 步骤 5 对**所有**事件写 change_log，而 §5.2 的主键是 "
                "(tenant_id, run_id, watermark)、§4.3 说 id 就是 "
                "projection_watermark；B 类事件（ledger/artifact/delivery…）"
                "根本没有 projection_watermark，同一 run 上两条 B 类事件会撞"
                "同一行，SSE 只回放得到最后一条",
     "handling": "run_dashboard_projection 增一列 change_seq（每 run 单调 +1）"
                 "作为展示序号，SSE 的 id 与 change_log 主键都用它；"
                 "applied_watermark 仍**只**放 S21 的 projection_watermark，"
                 "只服务缺号检测与乱序丢弃。A 类事件密集时 change_seq 取 "
                 "max(seq, prev+1)，与 §4.3 字面一致。change_seq 不在 §5.1 的 "
                 "DDL 里，补列需 owner 认可或改契约。"},
    {"id": "C47-24", "kind": "needs_owner",
     "summary": "§3.4 的「计数类 500ms 窗口内合并」没有对应的 event_class——"
                "计数（completed_shots）挂在 run.state 事件上，而同一节又要求"
                "「run 级状态立即推」，两条规则对同一帧给出相反指令",
     "handling": "只实现 node.state 的 200ms 合并；run.state 一律立即推"
                 "（不让计数合并吞掉状态变化，那会让 UI 晚 500ms 才知道"
                 "run 失败了）。合并与否不影响 change_log 里的完整事实。"},
    {"id": "C47-25", "kind": "gap",
     "summary": "S01 的授权作用域只有 tenant/org_unit/project/series/episode，"
                "没有 run；perform 参数是自由文本，权限表里也没有 "
                "projection.read，只有 run.get",
     "handling": "run 级订阅用 tenant 作用域 + run.get 去问 S01，问不出「这个 "
                 "run 归不归他看」；实际隔离由每条 SQL 的 tenant_id 谓词保证。"
                 "run 级细粒度授权待 S01 增加 scope_kind='run'。"},
    {"id": "C47-26", "kind": "gap",
     "summary": "S47 不持有主体名录（那是 S01 的），无法枚举「该收到通知的所有"
                "人」；方案 §3.9 只定义了类型白名单，没定义收件人集合",
     "handling": "收件人 = 显式写了通知偏好的人 ∪ 当前对该 run 有活跃游标的"
                 "观看者；两处都没出现的 principal 不会收到。要广播给 actor 之外"
                 "的人需要 S01 提供 run 的订阅者查询。"},
]
