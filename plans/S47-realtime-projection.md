# S47 · 实时投影与通知 —— 实现方案

状态：实现方案（W4）。`maturity: design_only` 不变。

上游依据：[docs/04 §4–§5](../docs/04-data-consistency.md)、[docs/13 §1](../docs/13-api-product.md)、[docs/03 的服务等级目标](../docs/03-deployment-capacity.md)、[S47 规格](../docs/design-package/services/S47-realtime-projection.md)。

配套：[S21 编排](S21-orchestration.md)（`projection_watermark` 的定义方）、[S26](S26-generation-jobs.md)（`generation.job.state-changed`）、[S08](S08-revision-snapshot.md)（accepted pointer，**永远回源**）、[S44](S44-evaluation-analytics.md)（"数据截至"口径的同类问题）。

---

## 1. 边界：写什么 / 不写什么 / 一条必须坚持的原则

### 1.1 写什么

S47 是**只读投影 + 通知投递**。它持有的三类事实：

| 事实 | 表 | 语义 |
|---|---|---|
| 运行看板投影 | `run_dashboard_projection` | 某 run 在某水位时刻长什么样 |
| SSE 游标 | `sse_cursor` | 某个连接从哪个水位继续 |
| 通知偏好与投递回执 | `notification_preference` / `notification_delivery` | 谁要什么通知、送到了没有 |

### 1.2 不写什么，以及为什么这条比别的服务更严

| 不是 S47 的事实 | 归属 | 若 S47 存了会发生什么 |
|---|---|---|
| run/manifest 的当前状态 | **S08 / S21** | UI 会显示一个"看起来权威"但其实滞后的状态，且用它做决定 |
| 余额、配额 | **S23 / S22** | 用户看到余额 100 元，实际已经是 3 元 |
| 音色授权状态 | **S15** | 已撤销的音色在 UI 上显示"可用" |
| 正史 checkpoint | **S16** | 编剧基于过期正史改剧本 |
| 审核决定 | **S40** | "已通过"的旧投影被当成放行依据 |
| 制品内容 | **S28** | 第二份字节副本 |

**S47 的投影表是缓存的语义，不是账本的语义。** 它可以被任意丢弃、任意重建、任意滞后——唯一的义务是**如实标注它滞后多少**。

### 1.3 一条必须坚持的原则

> **权威永远赢；投影只解释"截至当时是什么样"。**

具体到三条：

1. **任何改变系统状态的动作，都不读 S47。** 生成、接受、付费、发布 —— 一律回源 S08/S23/S22/S15/S40。S47 的读模型只服务 UI 的**展示**。
2. **每个投影响应都带 `as_of` 与 `projection_watermark`。** 缺一个字段就是"假装实时"（docs/04 §5、docs/13）。
3. **出现分歧时，S47 无条件认输。** 发现投影与权威不一致时，S47 的动作是**刷新自己**，不是"报告冲突要求权威改"。唯一的例外是 §3.6 的 `STALE_FENCE` 防护（防止旧投影覆盖新投影），那仍不是质疑权威。

### 1.4 S47 绝不写回任何业务服务

这是本服务最容易被悄悄破坏的边界。破坏的方式通常很"合理"：

| 看似合理的写法 | 为什么禁止 |
|---|---|
| "UI 点了暂停 → S47 直接改投影状态为 PAUSED，再异步通知 S21" | 变成了乐观写入 + 第二权威。用户看到 PAUSED，S21 可能拒绝了 |
| "投影里存一份重试计数，达到阈值就调 S21 重试" | S47 变成了调度器。重试归 S21/S26 |
| "连接断开时 S47 把 run 标为 interrupted" | 断线是 UI 的事实，不是 run 的事实 |
| "通知投递失败 → 调用 S21 重新发事件" | 通知与事件是两条路；前者失败不代表后者 |

**结构保证**：S47 的运行身份**没有**任何业务服务的写权限。它的 outbound HTTP client 里只有 GET；任何非 GET 调用在代码里没有包装函数可用（不是靠 code review）。它自己的数据库账密只有 `realtime_projection` 库的权限。

UI 上的动作按钮（暂停、取消、接受）应该直接打**对应业务服务的公开端点**，不经过 S47。S47 只负责"这些动作发生后，页面看到什么"。

### 1.5 为什么需要一个独立服务而不是直接读权威

三个理由，都是实打实的：

1. **扇出**：一个 run 有 5 个并发观看者，10 个 run 同时跑，每个状态变更要推 50 条 SSE。让 S08/S21 直接承这个扇出会把写路径的可用性绑到 UI 连接数上。
2. **合并**：500,000 shot 任务/天（docs/03 规划），事件速率远高于人眼需求。S47 做节流与合并（§3.4），UI 才不闪。
3. **重放与游标**：SSE 断线重连需要"从上次的位置继续"，这需要一份可持续的游标语义，而权威服务不该为 UI 存这个。

---

## 2. 输入契约

### 2.1 订阅的事件（只读消费）

| topic | 事件类型 | 用途 | 来源 |
|---|---|---|---|
| `drama.orchestration.v2` | `orchestration.run.state-changed` | run 主状态、节点进度 | S21 |
| `drama.orchestration.v2` | `orchestration.node.state-changed` | 单节点进度（可节流） | S21 |
| `drama.generation-jobs.v2` | `generation.job.state-changed` | 供应商任务状态（`aggregate_id = provider_job_id`） | S26 |
| `drama.model-routing.v2` | `model.route.selected` | 走哪个模型（用于 UI 显示"由 X 生成"） | S25 |
| `drama.provider-connectors.v2` | `connector.observation.received` | 供应商观测（限流、配额水位） | S27 |
| `drama.artifact-ingest.v2` | `artifact.registered` / `artifact.deleted` | 制品可用性 | S28 |
| `drama.human-review.v2` | `review.decision.recorded` | 审核状态 | S40 |
| `drama.delivery.v2` | `delivery.package.sealed` | 交付进度 | S43 |
| `drama.developer-integration.v2` | `integration.command.accepted` | 外部调用列表 | S46 |
| `drama.budget-ledger.v2` | `budget.reservation.settled` | **只用于"已花费"展示，带水位** | S23 |

**S47 的消费是"投影"，不是"状态同步"。** 它拿事件里的事实更新自己的缓存行，不推演未来、不补齐缺失状态。事件里没有的字段，投影里就没有——而不是"按上一次的值保留"（那会让陈旧值伪装成当前值）。

### 2.2 事件的 `aggregate_sequence` 语义

S21 的 `projection_watermark` 对 `orchestration.run.state-changed` 是**每个 run 单调递增、无洞**的（S21 方案已定义：`aggregate_sequence = projection_watermark`）。

这给了 S47 两个能力：

1. **缺号检测**：收到 seq 17，但当前是 15 → 16 丢了。S47 不是"等 16 来"，而是**标记该 run 的投影为 `GAPPED`**，UI 显示"数据可能不完整"，并主动回源 S21 拉当前快照。
2. **乱序拒绝**：收到 seq 14，当前 15 → 丢弃（不是覆盖）。旧状态永远不能回退新状态（与 E07 同一纪律：旧 epoch 不推进接受指针）。

对其它 topic，`aggregate_sequence` 的语义由各自服务定义。若某 topic 不保证无洞（例如 `connector.observation.received` 可能是采样式），S47 **不做缺号检测**，只用 `time` 排序。这必须按 topic 配置，不能一套逻辑通吃。

### 2.3 命令入口 `POST /subscriptions`

```jsonc
POST /subscriptions
{
  "channel": "sse",                      // 'sse' | 'webhook'(二期)
  "scope": { "kind": "run", "id": "run_01H..." },
  "event_filters": ["run.state", "node.state", "artifact.registered"],
  "since_watermark": 88200               // 可选，从某水位开始
}
→ 201 {
  "subscription_id": "sub_01J...",
  "stream_url": "/runs/run_01H.../events?subscription_id=sub_01J...",
  "cursor": "c_01J...",
  "as_of": "2026-10-30T08:00:00Z",
  "snapshot_watermark": 88213
}
```

**`snapshot_watermark` 必须返回。** 客户端拿到它之后才知道"我接下来看到的第一条事件的 seq 应该 ≥ 88213"。没有它，客户端无法判断自己是否漏了。

若 `since_watermark` 指定的位置已超出保留窗口（§3.7），返回 `410` 并附带 `snapshot_url`——让客户端先重新拉一次快照（这是 E36 要求的"重同步不重复提交"）。

### 2.4 通知偏好

```jsonc
POST /notification-preferences
{
  "event_types": ["run.failed", "run.completed", "review.requested"],
  "channels": [
    { "kind": "in_app" },
    { "kind": "email", "address_ref": "..." },
    { "kind": "webhook", "subscription_id": "sub_01J..." }
  ],
  "quiet_hours": { "from": "22:00", "to": "08:00", "tz": "Asia/Shanghai" }
}
```

`channels` 里的 `kind` 只能是已启用的渠道。`webhook` 必须指向 S46 的订阅（S47 不自己管 webhook 签名与重试——那是 S46 的职责，见 §10.4）。

---

## 3. 核心逻辑与状态机

### 3.1 消费 → 投影的路径

```text
Kafka event
  │
  ├─1. Inbox 去重 (consumer='s47.projection', event_id)
  │      已存在 → 直接 ack，不入投影
  │
  ├─2. 按 topic 选策略：
  │      · 有单调 watermark 的 → 走 §3.3 排序 + 缺号检测
  │      · 无的 → 走 §3.3 的 time 排序
  │
  ├─3. 取投影行 FOR UPDATE
  │      · 不存在 → 创建（若事件表达的是"首次出现"）
  │      · 存在且 watermark 更旧 → **丢弃**（旧不覆盖新）
  │
  ├─4. 合并到投影字段（§3.5 的字段规则）
  │
  ├─5. 写入 change_log（供 SSE 扩散，见 §3.4）
  │
  └─6. 异步：扇出到该 run 的所有活跃 SSE 连接
```

**步骤 3 的"旧不覆盖新"是本服务的核心保护。** 实现上：每条投影行有 `applied_watermark`；事件带的 watermark 小于等于它，一律丢弃。

### 3.2 状态机：投影行

```text
   ABSENT ──(首次事件)──► LIVE
                            │
                            ├──(超过 retention 且无观看者)──► EVICTED
                            │
                            ├──(检测到缺号)────────────────────► GAPPED
                            │        └──(回源成功)──► LIVE
                            │
                            └──(权威说 run 已终态)────────────► TERMINAL
```

| 状态 | UI 语义 | 是否还收事件 |
|---|---|---|
| `ABSENT` | 没有投影，UI 必须回源 | 是（等首条事件） |
| `LIVE` | 可信，带水位 | 是 |
| `GAPPED` | **"数据可能不完整"**，显示黄色提示，同时提供"刷新"按钮 | 是（等回源补齐） |
| `TERMINAL` | 终态，不再变更（但仍是投影，不是权威） | 只收迟到事件的诊断，不更新展示 |
| `EVICTED` | 已清理，下次访问时重建 | 否（重建时全量拉） |

**`GAPPED` 不是错误。** 它是"我知道我漏了"的诚实状态。**禁止**在缺号时"用上一个值继续显示成 LIVE"——那正是 E36 要防的。

### 3.3 排序与缺号检测

按 topic 分两类：

**A 类（单调 watermark，如 S21 run 事件）**

```text
if event.watermark > row.applied_watermark + 1:
    row.state = 'GAPPED'
    row.gap = { expected: row.applied_watermark + 1, got: event.watermark }
    → 仍应用该事件（因为它是更新的），但标记 gap，UI 知道中间缺了
    → 触发异步回源（§3.8）

elif event.watermark <= row.applied_watermark:
    drop  # 乱序或重复

else:  # 恰好 +1
    apply
```

注意：缺号时**仍然应用**，不是丢弃。丢弃会让投影停在更旧的位置，更糟。应用 + 标记 gap，让 UI 至少显示"最新已知的，但中间有洞"。

**B 类（无单调序，如观测事件）**

```text
if event.time < row.last_event_time - skew_tolerance: drop
else: apply, update last_event_time = max(...)
```

`skew_tolerance` 默认 30s（供应商时钟不可靠）。B 类不做 gap 检测——没有连续性的概念。

### 3.4 节流与合并

一个 run 在高频生成时，`orchestration.node.state-changed` 可能每秒几十条。人眼不需要。S47 做**按键合并**：

```text
合并窗口：
  · run 级状态：立即推（用户最关心）
  · node 级进度：200ms 窗口内合并，只推最后一条
  · 计数类（已完成 shot 数）：500ms 窗口内合并
  · 错误/终态：立即推，且不与其他合并
```

**合并必须在服务端做，不能在客户端做。** 客户端合并意味着带宽已经花掉了，且断线重连时客户端要处理更多乱序。

**合并不能改变事实。** 合并后推送的仍是完整事件（带 watermark），只是少推了几条中间态。客户端看到的最后一条永远是最新的。

### 3.5 投影字段的更新规则

三类字段，规则不同：

| 字段类 | 例子 | 规则 |
|---|---|---|
| **单调累加** | `completed_shots`, `spent_amount_micros` | 取 max（不累加——事件里带的是累计值，不是增量） |
| **最新覆盖** | `current_node`, `run_state`, `last_error` | 按 watermark 更新 |
| **只增不减集合** | `artifacts[]`, `warnings[]` | 并集，按 id 去重 |
| **永不投影** | 余额（只投影"已花费"）、accepted pointer、音色授权态 | 见 §1.2，UI 回源 |

最后一行是硬规则：**"已花费"可以投影（它是历史累计，不会倒退），"剩余余额"不能投影（它是当前状态，会变）。** 这个区分很细但很关键——docs/04 §5 说"需要强一致的余额…回源权威服务"。

### 3.6 `STALE_FENCE`：防旧投影覆盖新投影

一个容易漏的竞态：消费分区重平衡时，两个 consumer 可能同时处理同一 run 的事件（旧 consumer 还没释放）。若不加保护，旧事件可能覆盖新投影。

保护：

```text
投影行带 applied_watermark。任何更新必须满足 event.watermark > applied_watermark。
```

唯一约束 + 条件更新：

```sql
UPDATE run_dashboard_projection
   SET ..., applied_watermark = $w, updated_at = now()
 WHERE tenant_id = $t AND run_id = $r
   AND applied_watermark < $w;
-- 0 行 → 丢弃（说明有更新的已经应用）
```

**这是一次 CAS，不是锁。** 不用 `FOR UPDATE` 也能正确，因为条件在 `WHERE` 里。

`STALE_FENCE` 这个名字只在本服务内部使用，**不是** `execution_fence`。两者毫无关系：`execution_fence` 是 S08 的 run/branch 级取消与故障切换机制；S47 的 watermark 比较只是一个缓存一致性检查。**绝不能把两者混称**（AGENTS.md 第 38–40 行）。

### 3.7 SSE 游标与保留

```text
SSE 连接建立
  ├─ 带 Last-Event-ID? ──是──► 查 sse_cursor
  │                              · 存在且未过期 → 从该位置续
  │                              · 过期或不存在 → 410 + snapshot_url
  └─ 否 ──► 建新游标，从当前 snapshot_watermark 开始
```

**游标保留窗口**：默认 **15 分钟**（可配）。理由：

- 短于 15 分钟会让短暂的网络抖动变成"必须重新拉快照"，体验差。
- 长于 15 分钟要在内存/Redis 里保留大量历史事件，成本高，且在 UI 无人在看时纯属浪费。

游标存的是 `(subscription_id, last_sent_watermark, created_at, last_activity_at)`，**不存事件内容**。续连时从投影/change_log 按 watermark 之后回放。

**410 之后客户端的正确动作**（E36）：

```text
1. GET /runs/{id}/snapshot  → 拿完整当前投影 + 新 watermark
2. 重建 UI
3. POST /subscriptions（或直接 GET stream）建立新连接
4. **绝不**重新提交用户刚才的操作（那会重复生成/重复付费）
```

第 4 条是 E36 的核心。S47 的责任是：410 响应的 body 里显式写 `"do_not_resubmit": [...]` 说明该怎么做，以及**不在 410 时丢失用户的未提交表单状态**——后者是前端的事，但 S47 的 snapshot 要能恢复"用户看到的是什么"。

### 3.8 回源（`GAPPED` 的处理）

```text
检测到 gap → 异步 job：
  · 带 tenant + run_id 调 S21 的 GET /runs/{id}
  · 拿到完整状态 + 上游 watermark
  · 若上游 watermark > 本地 applied_watermark → 覆盖（这是一次全量刷新，不是合并）
  · 清除 gap 标记，state = 'LIVE'
  · 若上游也不可达 → 保持 GAPPED，60s 后重试，指数退避
```

**回源是 S47 唯一的 outbound 调用，且只读。** 它不写任何业务服务（§1.4）。

回源的频率上限：每 run 每分钟最多 6 次。防雪崩（一个 run 频繁 gap → 疯狂回源 → 打挂 S21）。

### 3.9 通知投递

```text
事件到达 → 匹配 notification_preference（按 tenant + user + event_type）
         → 对每个 channel 建一条 notification_delivery
         → 按渠道投递
```

**通知 ≠ 事件。** 通知是"人类需要知道的事"，事件是"系统发生的事"。映射是白名单：

| 事件 | 是否默认通知 |
|---|---|
| `run.failed` / `run.blocked` | 是 |
| `run.completed` | 是 |
| `review.requested` | 是 |
| `delivery.sealed` | 是 |
| `node.state-changed` | **否**（太频繁） |
| `budget.threshold.reached`（80%） | 是 |
| 其它 | 否，需用户显式订阅 |

**`quiet_hours` 的处理**：在 quiet hours 内，非紧急通知延后到结束时统一投递；紧急的（`run.failed`、`budget` 超限）照发。

**投递去重**：`(tenant, user, event_id, channel)` 唯一。同一事件对同一用户的同一渠道只投一次。

---

## 4. 输出契约

### 4.1 查询端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/runs/{run_id}/snapshot` | 完整当前投影 + `as_of` + `projection_watermark`；**410 后重同步用** |
| GET | `/runs/{run_id}/events` | **SSE**（openapi 已有 `streamRun`）；`Last-Event-ID` 可选 |
| GET | `/runs?state=&from=&to=&cursor=` | run 列表（工作台首页） |
| GET | `/dashboard/summary` | 租户级汇总（活跃 run 数、今日完成数） |
| POST | `/subscriptions` | 建订阅 |
| DELETE | `/subscriptions/{id}` | 关闭 |
| GET | `/notification-preferences` / POST / PATCH | 通知偏好 |
| GET | `/notifications?unread=true` | 站内通知列表 |
| POST | `/notifications/{id}:read` | 标记已读 |

### 4.2 `GET /runs/{run_id}/snapshot`

```jsonc
{
  "run_id": "run_01H...",
  "projection_state": "LIVE",              // LIVE | GAPPED | TERMINAL
  "as_of": "2026-10-30T08:05:12Z",
  "projection_watermark": 88213,
  "source_watermark": 88213,               // 上游的 watermark（回源时对齐）

  "run": {
    "state": "RUNNING",
    "state_since": "2026-10-30T08:01:00Z",
    "episode_id": "ep_03",
    "branch_id": "br_main",
    "manifest_revision": 13
  },
  "progress": {
    "completed_shots": 42,
    "total_shots": 106,
    "current_node": { "node_id": "shot_3_45.video", "node_epoch": 2 }
  },
  "cost": {
    "spent_micros": 128440000,             // 累计已花费，可投影
    "spent_as_of": "2026-10-30T08:05:00Z",
    "remaining_ref": "/budget-authorizations/bud_01J..."   // 余额不投影
  },
  "warnings": [
    { "code": "NODE_RETRY", "node_id": "shot_3_41.video", "count": 2 },
    { "code": "PROJECTION_GAP", "expected": 88190, "got": 88210, "at": "..." }
  ],
  "artifacts": [
    { "artifact_id": "art_01J...", "kind": "video", "url_ref": "/artifacts/art_01J.../download" }
  ],
  "__hints": {
    "authority": {
      "run_state": "S21",
      "budget": "S23",
      "voice_authorization": "S15",
      "canon": "S16"
    },
    "do_not_resubmit_on_resync": true
  }
}
```

`__hints.authority` 是给前端与外部 Agent 的**机械提示**：告诉他们哪个字段不是权威、要去哪查。这不是文档，是 API 响应的一部分——因为 Agent 会读 JSON，不会读 wiki。

`remaining_ref` 而不是 `remaining_micros`：余额必须回源（§1.2、docs/04 §5）。

### 4.3 SSE 事件格式

```text
id: 88214
event: run.state
data: {"watermark":88214,"run_state":"RUNNING","completed_shots":43,"at":"2026-10-30T08:05:13Z","type":"orchestration.run.state-changed"}

id: 88215
event: node.state
data: {"watermark":88215,"node_id":"shot_3_45.video","state":"QUEUED","at":"..."}
```

- `id` 就是 `projection_watermark`。客户端把它作为 `Last-Event-ID` 回来。
- `event` 是粗粒度类别（`run.state` / `node.state` / `artifact` / `review` / `warning`），便于客户端用 `addEventListener` 分类处理。**不是** Kafka 的 `type`——那是内部细节，不该泄漏。
- 每个 `data` 都带 `watermark`。**没有 watermark 的 SSE 事件是 bug。**
- 心跳：每 15s 一条 `: keepalive` 注释行（不是事件）。反代可能需要。

### 4.4 发布事件

`projection.cursor.advanced`（topic `drama.realtime-projection.v2`，`aggregate_id = subscription_id`）：

```jsonc
{
  "specversion": "1.0", "schema_version": "2.0",
  "id": "evt_...", "source": "S47", "type": "projection.cursor.advanced",
  "subject": "subscription/sub_01J...",
  "time": "2026-10-30T08:05:15Z",
  "tenant_id": "t_demo", "home_cell": "cell_a1",
  "aggregate_id": "sub_01J...", "aggregate_sequence": 88215,
  "correlation_id": null, "causation_id": "evt_kafka_...",
  "traceparent": "00-...-...-01",
  "data": {
    "subscription_id": "sub_01J...",
    "run_id": "run_01H...",
    "watermark": 88215,
    "lag_ms": 2100,
    "state": "LIVE"
  },
  "data_ref": null
}
```

**只在游标前进时发，且必须节流**：每订阅每秒最多 1 条。理由：这是用来做"投影滞后监控"与"审计游标位置"的，不是用来流式同步的。若每条 SSE 事件都发一条 Kafka 事件，就形成了一个比原流更大的流。

`aggregate_sequence = watermark`，与 S21 的语义一致，便于下游做缺号检测。

### 4.5 错误码

| HTTP | code | 说明 |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | |
| 403 | `TENANT_SCOPE_DENIED` | header 与认证租户不符 |
| 404 | `RUN_NOT_FOUND` | **含跨租户**（E28） |
| 404 | `SUBSCRIPTION_NOT_FOUND` | |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 订阅创建 |
| **410** | **`CURSOR_EXPIRED`** | 游标超保留窗口；body 含 `snapshot_url` 与 `do_not_resubmit` 说明 |
| 422 | `SUBSCRIPTION_LIMIT_REACHED` | 单用户/单 run 订阅数超限 |
| 429 | `CONNECTION_LIMIT_REACHED` | 单租户并发 SSE 连接超限 |
| 503 | `PROJECTION_UNAVAILABLE` | 投影存储不可用；**不要**退回直读 S21（那会打挂权威） |

最后一条要强调：投影挂了就是挂了，UI 显示"暂时无法加载"，不要"降级直连 S08/S21"。降级直连会把 UI 的连接数乘到权威服务上，而权威服务没有为此设计。

---

## 5. 数据模型

数据库 `realtime_projection`。

### 5.1 投影表

```sql
CREATE TABLE run_dashboard_projection (
  tenant_id           text NOT NULL,
  run_id              text NOT NULL,
  project_id          text NOT NULL,
  episode_id          text NOT NULL,
  branch_id           text NOT NULL,

  projection_state    text NOT NULL DEFAULT 'LIVE'
    CHECK (projection_state IN ('LIVE','GAPPED','TERMINAL','EVICTED')),
  applied_watermark   bigint NOT NULL DEFAULT 0,
  source_watermark    bigint,                 -- 最近一次回源时上游的 watermark
  gap                 jsonb,                  -- {expected,got,detected_at}
  last_event_time     timestamptz,

  run_state           text,
  state_since         timestamptz,
  manifest_revision   integer,                -- 仅展示用；权威在 S08
  current_node        jsonb,                  -- {node_id,node_epoch}
  completed_shots     integer NOT NULL DEFAULT 0,
  total_shots         integer,
  spent_micros        bigint NOT NULL DEFAULT 0,
  spent_as_of         timestamptz,
  warnings            jsonb NOT NULL DEFAULT '[]',
  artifacts           jsonb NOT NULL DEFAULT '[]',   -- 只增，按 artifact_id 去重

  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  evicted_at          timestamptz,

  PRIMARY KEY (tenant_id, run_id)
);
CREATE INDEX rdp_active_idx ON run_dashboard_projection (tenant_id, run_state, updated_at DESC)
  WHERE projection_state <> 'EVICTED';
CREATE INDEX rdp_gapped_idx ON run_dashboard_projection (tenant_id, updated_at)
  WHERE projection_state = 'GAPPED';
```

`manifest_revision` 在这里只用于**显示**。任何"按这个 revision 做决定"的逻辑都是错的——那是 S08 的事。代码里这个字段的读函数叫 `read_manifest_revision_for_display()`，命名即约束。

### 5.2 变更日志（SSE 回放用）

```sql
CREATE TABLE projection_change_log (
  tenant_id      text NOT NULL,
  run_id         text NOT NULL,
  watermark      bigint NOT NULL,
  event_class    text NOT NULL,          -- 'run.state'|'node.state'|'artifact'|...
  payload        jsonb NOT NULL,         -- 已过 PROJECTION_SECRET_SCAN（§5.2 下文）
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, run_id, watermark)
);
CREATE INDEX pcl_ttl_idx ON projection_change_log (created_at);

-- 分区按月；保留期 15 分钟由 janitor 清理（见 §3.7）
```

**为什么存 payload 而不只是 watermark**：游标续连时需要回放"游标之后发生了什么"，如果只存 watermark，就得回源重建——那会丢掉中间态。存一份已脱敏的小 payload（几百字节）成本可控。

**`payload` 的"已脱敏"是有出处的，不是上游自觉。** 事件进 S47 后、写进这张表之前、开 SSE 推送之前，跑一次 `PROJECTION_SECRET_SCAN`，命中按 [redaction-policy.v2](../contracts/redaction-policy.v2.json) 就地替换（`class_a` → `REDACTED:hmac_<scope>:<first16hex>`，`class_b` → `REDACTED:<class>`）。**命中只换叶子，不拒、不丢、不改 `event_class` 或 `watermark`**：契约兜底是 `redact_and_store`；这里若丢事件，就是 §3.3 明令禁止的"silently introduced gap"，且下游 SSE 客户端无从知道漏了一条。

这一条对 S47 比别的服务更要紧：`projection_change_log` 虽然只留 15 分钟，但 `run_dashboard_projection` 的字段是**长期**的，SSE 又是"一次推给所有订阅者"的广播——在这里漏出去的密钥，比在别处更难追回。所以 §3.5 的字段合并规则里，凡是直接透传上游 raw payload 的字段，都要在这里过一遍。

**保留期短（15 分钟）是关键**：它让这张表永远很小，不需要分区归档，`DELETE WHERE created_at < now() - interval '15 minutes'` 就够了。

### 5.3 游标与订阅

```sql
CREATE TABLE sse_cursor (
  subscription_id  text PRIMARY KEY,
  tenant_id        text NOT NULL,
  principal_id     text NOT NULL,
  run_id           text NOT NULL,
  channel          text NOT NULL DEFAULT 'sse',
  last_sent_watermark bigint NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  last_activity_at timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL
);
CREATE INDEX sc_expiry_idx ON sse_cursor (expires_at);
CREATE INDEX sc_run_idx    ON sse_cursor (tenant_id, run_id);

CREATE TABLE subscription_registration (
  subscription_id  text PRIMARY KEY,
  tenant_id        text NOT NULL,
  principal_id     text NOT NULL,
  scope_kind       text NOT NULL CHECK (scope_kind IN ('run','tenant')),
  scope_id         text,
  event_filters    text[] NOT NULL,
  state            text NOT NULL DEFAULT 'ACTIVE'
    CHECK (state IN ('ACTIVE','CLOSED','EXPIRED')),
  created_at       timestamptz NOT NULL DEFAULT now(),
  closed_at        timestamptz
);
CREATE INDEX sr_principal_idx ON subscription_registration (tenant_id, principal_id, state);
```

### 5.4 通知

```sql
CREATE TABLE notification_preference (
  tenant_id      text NOT NULL,
  principal_id   text NOT NULL,
  event_types    text[] NOT NULL,
  channels       jsonb NOT NULL,          -- [{kind,address_ref?,subscription_id?}]
  quiet_hours    jsonb,                   -- {from,to,tz}
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, principal_id)
);

CREATE TABLE notification_delivery (
  delivery_id    text PRIMARY KEY,
  tenant_id      text NOT NULL,
  principal_id   text NOT NULL,
  event_id       text NOT NULL,
  event_type     text NOT NULL,
  channel_kind   text NOT NULL CHECK (channel_kind IN ('in_app','email','webhook')),
  channel_ref    text,
  state          text NOT NULL DEFAULT 'PENDING'
    CHECK (state IN ('PENDING','DEFERRED','SENT','FAILED','CANCELLED')),
  deferred_until timestamptz,
  attempt_count  smallint NOT NULL DEFAULT 0,
  last_error     text,
  sent_at        timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT nd_uniq UNIQUE (tenant_id, principal_id, event_id, channel_kind)
);
CREATE INDEX nd_pending_idx ON notification_delivery (state, deferred_until)
  WHERE state IN ('PENDING','DEFERRED');
CREATE INDEX nd_user_idx    ON notification_delivery (tenant_id, principal_id, created_at DESC);

CREATE TABLE in_app_notification (
  notification_id text PRIMARY KEY,
  tenant_id       text NOT NULL,
  principal_id    text NOT NULL,
  event_id        text NOT NULL,
  event_type      text NOT NULL,
  title           text NOT NULL,
  body_ref        text,                   -- 指向详情；不存正文全文
  run_id          text,
  read_at         timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ian_unread_idx ON in_app_notification (tenant_id, principal_id, created_at DESC)
  WHERE read_at IS NULL;
```

### 5.5 幂等、Inbox、Outbox

```sql
CREATE TABLE idempotency_record (
  tenant_id      text NOT NULL,
  command_id     text NOT NULL,
  request_hash   char(64) NOT NULL,
  command_type   text NOT NULL,
  state          text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code  integer,
  response_body  jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE inbox (
  consumer    text NOT NULL,     -- 's47.projection' | 's47.notify'
  event_id    text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
CREATE INDEX inbox_ttl_idx ON inbox (received_at);

CREATE TABLE outbox (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id         text NOT NULL,
  aggregate_id      text NOT NULL,              -- subscription_id
  aggregate_sequence bigint NOT NULL,   -- = watermark
  event_type        text NOT NULL,
  partition_key     text NOT NULL,      -- tenant_id || ':' || aggregate_id
  payload           jsonb NOT NULL,
  headers           jsonb NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  published_at      timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;
```

`outbox` 只承载节流后的 `projection.cursor.advanced`。同一订阅同 watermark 只写一条（`UNIQUE` 保证）。

### 5.6 不建的表

| 不建 | 为什么 |
|---|---|
| `balance_projection` / `remaining_budget` | 第二权威（§1.2） |
| `voice_authorization_projection` | 授权态必须回源 S15（E30） |
| `accepted_manifest_projection` | accepted pointer 归 S08（AGENTS.md 第 18 行） |
| `canon_checkpoint_projection` | 归 S16 |
| `review_decision_projection`（作为"放行依据"） | 可以做展示，但不能被任何逻辑当依据（§1.3） |
| `event_payload_archive`（长期） | 15 分钟后就该没了；长期归档是 S45/S28 的事 |

---

## 6. 幂等与并发

### 6.1 三处幂等

| 位置 | 键 | 作用 |
|---|---|---|
| 事件消费 | `(consumer='s47.projection', event_id)` | 重复投递不重复应用 |
| 订阅创建 | `(tenant_id, command_id)` | 客户端重试不重复建订阅 |
| 通知投递 | `(tenant, principal, event_id, channel)` | 同一事件不重复通知 |

### 6.2 投影更新的正确性是 CAS，不是锁

```sql
UPDATE run_dashboard_projection
   SET completed_shots = GREATEST(completed_shots, $n),
       spent_micros    = GREATEST(spent_micros, $spent),
       run_state       = $state,
       current_node    = $node,
       artifacts       = (artifacts || $new_artifacts)::jsonb,   -- 见下
       applied_watermark = $w,
       updated_at      = now()
 WHERE tenant_id = $t AND run_id = $r
   AND applied_watermark < $w;
```

**不用 `SELECT ... FOR UPDATE`。** 条件在 `WHERE` 里已经保证正确性：只有更高的 watermark 能更新。这比行锁好在：

- 无等待，无死锁；
- 分区重平衡时两个 consumer 并发写，一个必然 0 行；
- 回源的全量刷新也走同一语句（带上游 watermark）。

`artifacts` 的追加需要去重。`jsonb ||` 是数组拼接，不做去重。正确做法：

```sql
UPDATE ... SET artifacts = (
  SELECT jsonb_agg(DISTINCT x)
    FROM jsonb_array_elements(artifacts || $new) x
)
```

或者应用层先读后写（在同一个 CAS 事务里）。**建议应用层做**，因为 `DISTINCT` 对整个数组的语义是"按值去重"，而 artifact 对象会随状态变化——按 `artifact_id` 去重才是对的。这需要应用侧的小函数，SQL 表达不了。

### 6.3 并发场景

| 场景 | 期望 |
|---|---|
| 同一 run 的 seq 17 与 seq 16 并发 | 17 先到并应用；16 到后被 `applied_watermark < $w` 挡住 |
| 分区重平衡，两个 consumer 处理同一 run | 只有 watermark 更大的那次生效 |
| 回源与事件并发 | 回源带上游 watermark，只有更大才生效 |
| 同一订阅 2 个 SSE 连接（同一用户两个标签页） | 各自独立游标；各自从自己的 `last_sent_watermark` 续 |
| 1000 个订阅同时收同一 run 的事件 | 扇出到 1000 个连接的 Redis pub/sub；DB 只写一次 |
| 通知偏好并发修改 | `PATCH` 幂等；last-write-wins |

### 6.4 SSE 扇出的实现

```text
应用事件 → 更新投影 (DB, 一次)
        → LPUSH 到 Redis channel: "run:<tenant>:<run_id>"
        → 每个 SSE 连接一个 subscriber，各自读自己的游标

若 Redis 不可用：
  → 退化为轮询：SSE 连接每 2s 查一次 projection_change_log
  → 延迟增加，但不丢（change_log 有 15 分钟）
```

**扇出不在 DB 里。** 1000 个连接订阅一个 run，若每个连接都查一次 DB，DB 会被打穿。Redis pub/sub 是唯一合理的扇出层。

代价：Redis pub/sub 是 fire-and-forget，离线连接会丢消息。这正是 `projection_change_log` 存在的理由——重连时从游标回放。

### 6.5 连接数限制

| 维度 | 默认上限 | 超限 |
|---|---|---|
| 单租户并发 SSE | 500 | 429 `CONNECTION_LIMIT_REACHED` |
| 单用户并发 SSE | 10 | 同上 |
| 单 run 的订阅者 | 200 | 同上（防止一个 run 被 1000 个页面围观） |

这些上限是**保护**而不是商业限制。真实数字需要按 docs/03 的 2,000 峰值在线人数校准。

---

## 7. 失败分支

### 7.1 消费失败

| 场景 | 行为 |
|---|---|
| 事件 schema 不合法 | 进 DLQ，不改投影。**不崩溃**——一条坏事件不该停掉整个消费者 |
| 未知的事件类型 | 忽略（记录指标），不报错。上游加新事件类型时 S47 不该挂 |
| 缺号 | `GAPPED` + 异步回源（§3.3、§3.8） |
| 乱序 | 丢弃（`applied_watermark` 保护） |
| 投影行不存在且事件不是"首现" | 创建行，从事件内容初始化；标 `GAPPED`（因为中间可能缺了） |
| 回源失败 | 保持 `GAPPED`，退避重试（最多 6 次/分钟） |

### 7.2 SSE 断线

| 场景 | 行为 |
|---|---|
| 客户端网络断开 | 服务端 15s 后心跳失败，关连接，保留游标 15 分钟 |
| 客户端带 `Last-Event-ID` 重连 | 从 `projection_change_log` 回放该 watermark 之后的事件 |
| 游标过期（>15 分钟） | **410** + `snapshot_url` |
| 游标存在但 change_log 里没有对应记录 | 410（数据比游标旧，无法续） |
| 客户端不带 `Last-Event-ID` | 新建游标，从当前 watermark 开始（会漏中间态，但那是客户端的选择） |

**410 必须是 410，不是 404 或 500。** 客户端需要能机械区分"你的游标过期了，重拉快照"与"你访问的资源不存在"。

### 7.3 投影存储故障

| 场景 | 行为 |
|---|---|
| DB 不可用 | 503 `PROJECTION_UNAVAILABLE`；**不降级直连权威**（§4.5） |
| DB 慢（P99 > 2s） | 熔断读，返回 503；SSE 连接保持但暂停推送（发 `: degraded` 注释） |
| Redis 不可用 | SSE 退化为 2s 轮询（§6.4），仍可用 |
| change_log 被清空 | 所有游标 410，客户端重拉快照 |

### 7.4 通知失败

| 场景 | 行为 |
|---|---|
| 站内通知 | 几乎不会失败（同库写入） |
| 邮件发送失败 | 重试 3 次，然后 `FAILED`；**不影响其它渠道** |
| Webhook 通知 | 交给 S46 的投递机制（§10.4），S47 只负责"发一条给 S46" |
| quiet hours 内到达 | `DEFERRED`，到点重排 |
| 用户偏好已删除 | `CANCELLED` |

### 7.5 崩溃与重启

| 崩溃点 | 后果 | 恢复 |
|---|---|---|
| 事件应用前 kill | 未 ack，重投 | Inbox 去重 |
| 应用后、ack 前 kill | 重投 | Inbox 去重 |
| 游标更新后、SSE 发出前 kill | 客户端可能漏一条 | change_log 有；重连时按 `Last-Event-ID` 补 |
| SSE 发出后、游标更新前 kill | 客户端可能收重复 | 客户端按 `id` 去重（文档要求） |
| change_log 清理与写入并发 | 不可能（清理只删 15 分钟前的） | — |

### 7.6 失败矩阵

| # | 场景 | 检测 | 行为 |
|---|---|---|---|
| F47-01 | 分区重平衡 | Kafka consumer rebalance | 投影行不变；CAS 保护 |
| F47-02 | 事件洪水 | 消费 lag | 节流合并；不丢最后态 |
| F47-03 | 某 run 事件源失效 | 长时间无事件 | `stale_projection` 指标；不猜状态 |
| F47-04 | SSE 连接风暴 | 连接数指标 | 429 |
| F47-05 | 投影陈旧（回源失败） | `GAPPED` 计数 | UI 显示"数据可能不完整" |
| F47-06 | 通知渠道全挂 | 投递失败率 | 站内通知保底 |

---

## 8. 可观测

### 8.1 指标

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `s47_projection_lag_seconds` | histogram | `event_class` | **核心 SLO**：事件到投影的延迟（docs/03: P95 ≤ 5s） |
| `s47_projection_apply_total` | counter | `event_class`, `result`(applied/stale/dropped) | 应用结果分布 |
| `s47_projection_gapped_runs` | gauge | — | 当前有多少 run 在 GAPPED |
| `s47_projection_gap_events_total` | counter | `event_class` | 缺号发生的频率 |
| `s47_resync_total` | counter | `result`(ok/failed) | 回源成效 |
| `s47_resync_latency_seconds` | histogram | — | 回源耗时 |
| `s47_sse_connections` | gauge | `channel` | 当前连接数 |
| `s47_sse_connection_churn_total` | counter | `reason` | 断线频率 |
| `s47_sse_events_sent_total` | counter | `event_class` | 推送量 |
| `s47_sse_cursor_expired_total` | counter | — | **E36 核心**：多少 410 |
| `s47_sse_replay_events_total` | counter | — | 游标续连回放量 |
| `s47_sse_fanout_targets` | histogram | — | 单事件扇出几个连接 |
| `s47_notification_delivery_total` | counter | `channel_kind`, `state` | 通知质量 |
| `s47_notification_latency_seconds` | histogram | `channel_kind` | 通知时效 |
| `s47_notification_deferred_total` | counter | — | quiet hours 延迟 |
| `s47_projection_row_count` | gauge | `state` | 容量 |
| `s47_change_log_age_seconds` | gauge | — | 最老记录年龄，应 ≈ 15 分钟 |
| `s47_writeback_attempts_total` | counter | — | **必须恒为 0**（§1.4） |
| `s47_secret_redactions_total` | counter | `class`（契约闭集） | 事件里带密钥的事故量（§5.2） |
| `s47_scan_unavailable_total` | counter | — | 扫描器不可用，走了粗粒度兜底 |

`event_class` 是有限的枚举（`run.state`/`node.state`/`artifact`/`review`/`delivery`/`integration`），不是 Kafka 的完整 type 列表——后者会爆炸。

**不进 label**：`run_id`、`tenant_id`、`principal_id`、`node_id`。这些进 trace。

### 8.2 追踪

```text
kafka.consume (s47.projection)
  ├─ s47.inbox.check
  ├─ s47.projection.apply (DB)
  └─ s47.fanout (Redis)
  
sse.connection
  ├─ s47.cursor.load
  ├─ s47.replay (if resuming)
  └─ s47.stream (long-lived)
```

SSE 连接是长生命周期的，**不要**为整个连接建一个 span。用 span event 记录"推送了 watermark X"，或用 `Start/End` 的短 span 包每次推送批次。

### 8.3 `s47_writeback_attempts_total` 的意义

这个指标应当**永远是 0**。它的实现：在 outbound HTTP client 的拦截器里，任何 `method != GET` 的调用计数 + 拒绝。这不是防御性编程——是**把 §1.4 的约束变成可观测事实**。如果它变成非 0，说明有人加了写回，且被拦住了；P1 告警。

### 8.4 告警

| 级别 | 条件 |
|---|---|
| P1 | `s47_writeback_attempts_total > 0` |
| P1 | `s47_projection_lag_seconds` P95 > 30s 持续 5min |
| P2 | `s47_projection_gapped_runs` 占活跃 run > 5% |
| P2 | `s47_sse_cursor_expired_total` 突增（客户端网络问题或服务端卡顿） |
| P2 | `s47_projection_row_count` 增长异常（可能在内爆缓存） |
| P2 | `s47_secret_redactions_total` 突增（某上游在事件里带密钥） |
| P3 | `s47_notification_delivery_total{state="FAILED"}` > 10%（1h） |

---

## 9. 测试清单

### 9.1 投影正确性

| # | 用例 | 期望 |
|---|---|---|
| T47-01 | 事件按序到达 | 投影逐条更新，`applied_watermark` 前进 |
| T47-02 | 乱序（seq 17 后到 16） | 16 被丢弃，不进 change_log |
| T47-03 | 缺号（16 没来，17 到） | `GAPPED`，gap 记录 `{expected:16,got:17}`，仍应用 17 |
| T47-04 | 重复事件 | Inbox 去重，投影不变 |
| T47-05 | `completed_shots` 回退值 | `GREATEST` 保护，不减少 |
| T47-06 | `spent_micros` 回退值 | 同上 |
| T47-07 | 未知事件类型 | 忽略，不崩 |
| T47-08 | 事件 schema 非法 | DLQ，投影不变 |
| T47-09 | 投影行不存在，收到中间态事件 | 创建行 + `GAPPED` |
| T47-10 | TERMINAL 状态后收到迟到事件 | 不更新展示（只记诊断） |

### 9.2 投影滞后与水位

| # | 用例 | 期望 |
|---|---|---|
| T47-11 | `GET /snapshot` | 必带 `as_of`、`projection_watermark`、`projection_state` |
| T47-12 | 投影落后 10s 时查 snapshot | `as_of` 反映真实滞后；不假装实时 |
| T47-13 | `remaining_micros` 字段 | **不存在**；只有 `remaining_ref`（§1.2） |
| T47-14 | snapshot 的 `__hints.authority` | 明确列出哪些字段归哪个服务 |
| T47-15 | 投影与权威分歧 | 下次回源/事件到达时投影被覆盖；**S47 不做任何写回** |

### 9.3 SSE 与断线（E36）

| # | 用例 | 期望 |
|---|---|---|
| T47-16 `[E36]` | 建立 SSE，收到 5 条事件，断开重连带 `Last-Event-ID` | 从第 6 条继续，无重复无遗漏 |
| T47-17 `[E36]` | 断开 20 分钟（> 15 分钟游标保留） | **410** + `snapshot_url` + `do_not_resubmit` 说明 |
| T47-18 `[E36]` | 410 后按提示拉 snapshot | 拿到完整状态 + 新 watermark；**无需重新提交任何命令** |
| T47-19 `[E36]` | 410 后客户端错误地重新提交 → 验证 S47 不参与 | S47 无任何写入口；重复提交发生在 S21/S26 侧并被它们幂等 |
| T47-20 | 同一用户两个标签页 | 各自独立游标，互不影响 |
| T47-21 | 心跳 | 每 15s 一条 `: keepalive` |
| T47-22 | 每条 SSE 事件的 `id` | 等于 watermark，且单调递增 |
| T47-23 | 每条 SSE `data` | 必含 `watermark` |
| T47-24 | 连接数超限 | 429 `CONNECTION_LIMIT_REACHED` |
| T47-25 `[E32]` | Kafka 重放 1 小时 | 投影结果一致；不重复推送（游标未前进则不发） |

### 9.4 绝不写回

| # | 用例 | 期望 |
|---|---|---|
| T47-26 | outbound 调用审计（拦截器） | 全部为 GET；`s47_writeback_attempts_total == 0` |
| T47-27 | 代码静态检查：outbound client 包装 | 只有 `get()`，无 `post/put/patch/delete` |
| T47-28 | 模拟"允许写回"的注入 | 拦截器拒绝 + P1 指标 |
| T47-29 | 连接断开事件 | S47 不发送任何"run interrupted"到任何服务 |
| T47-30 | 通知失败 | S47 不调用上游重发事件 |

### 9.5 跨租户（E28）

| # | 用例 | 期望 |
|---|---|---|
| T47-31 `[E28]` | 租户 B 订阅租户 A 的 run | 404 `RUN_NOT_FOUND`，不含 A 的任何标识 |
| T47-32 `[E28]` | 租户 B 的 SSE 连接收到 A 的事件 | 不可能：扇出 channel 按 tenant 隔离 |
| T47-33 `[E28]` | `GET /runs` 列表 | 只返回本租户 |
| T47-34 `[E28]` | `projection_change_log` 直查 | 强制 `tenant_id` 谓词（行级安全策略） |

### 9.6 崩溃与恢复

| # | 用例 | 期望 |
|---|---|---|
| T47-35 `[E13]` | 消费中 kill | 重投，Inbox 去重 |
| T47-36 `[E13]` | 投影更新后、游标更新前 kill | 客户端可能收重复；按 id 去重 |
| T47-37 `[E13]` | Redis 挂 | SSE 退化为轮询，仍可用 |
| T47-38 `[E13]` | DB 挂 | 503，**不降级直连权威** |
| T47-39 `[E13]` | change_log 定期清理 | 15 分钟前的被删，游标随之 410 |

### 9.7 通知

| # | 用例 | 期望 |
|---|---|---|
| T47-40 | `run.failed` 触发通知 | 按偏好渠道投递 |
| T47-41 | `node.state-changed` | **默认不通知** |
| T47-42 | quiet hours 内到达 | `DEFERRED`，到点投递 |
| T47-43 | 紧急事件（`run.failed`）在 quiet hours | 立即投递 |
| T47-44 | 同一事件重复消费 | 只投一次（`nd_uniq`） |
| T47-45 | 邮件渠道失败 | 其它渠道不受影响 |
| T47-46 `[E30]` | 音色撤销后 | S47 的 UI 显示回源 S15 的当前状态；投影里没有"授权可用"（§1.2） |

### 9.8 独立升级

| # | 用例 | 期望 |
|---|---|---|
| T47-47 `[E01]` | 只 import 契约 SDK | 构建通过 |
| T47-48 `[E01]` | 回滚到旧版本 | 新字段可选；旧版本不认识的事件照常忽略 |
| T47-49 `[E01]` | 投影表清空后重建 | 从事件流重放可恢复（所有字段都有事件来源） |
| T47-50 `[E40]` | 演示：UI 断线 → 重连 → 看到一致的进度 | 用户不会误以为任务失败了 |

---

## 10. 与上下游服务的接口约定

### 10.1 对 S21（编排，`projection_watermark` 的定义方）

| 约定 | 内容 |
|---|---|
| `aggregate_sequence = projection_watermark` | 每 run 单调、无洞。**这是 S47 缺号检测的前提** |
| 事件字段 | `run_id, state, completed_shots, total_shots, current_node, manifest_revision, at` |
| 回源端点 | `GET /runs/{id}`，响应必带 `watermark` |
| 不提供 | S21 不提供"给我 seq 16"的补拉端点。缺口只能靠回源全量（§3.8） |

**若 S21 的 watermark 有一天变成"有洞"**，S47 的缺口检测失效，会误报大量 `GAPPED`。这是 C47-2，必须在标准里锁死。

### 10.2 对 S08（accepted pointer）

S47 **不订阅 S08 的接受事件**做"当前 manifest"投影（§1.2、§5.6）。UI 需要"当前接受哪个 revision"时，`__hints.authority.manifest` 指向 S08，前端直连。

S47 的 `run_dashboard_projection.manifest_revision` 只是"S21 事件里提到的那个 revision"，可能已过期。字段名与注释都必须让人意识到这点。

### 10.3 对 S26 / S25 / S27（生成与路由）

| 来源 | 用途 | 注意 |
|---|---|---|
| S26 `generation.job.state-changed` | UI 显示"供应商任务进度" | `aggregate_id = provider_job_id`；**不投影供应商原始响应**（S26 方案已规定事件体不含） |
| S25 `model.route.selected` | UI 显示"由 X 模型生成" | 只展示，不作为路由依据 |
| S27 `connector.observation.received` | 限流/配额水位展示 | 必须显示水位时间（S22 方案要求） |

### 10.4 对 S46（Webhook 投递）

S47 **不自己实现 Webhook 签名与重试**。通知渠道 `webhook` 时：

```text
S47 → S46: POST /integrations/commands { command: 'notify.dispatch', params: {...} }
  或更直接：S47 发一条内部事件，S46 消费并投递（若契约允许）
```

目前的契约缺口是 C46-6（S46 没有"通知投递"命令）。**在缺口补上之前，S47 的 webhook 通知不可用，只有 `in_app` 与 `email`。** 这一限制必须写在 UI 上，不能静默失败。

### 10.5 对 S23 / S22（钱与配额）

| 约定 | 内容 |
|---|---|
| **余额不投影** | UI 显示余额时回源 S23 |
| **已花费可投影** | `spent_micros` 是累计值，不会倒退，带 `spent_as_of` |
| **水位展示** | `GET /capacity` 的响应带 watermark（S22 已定义），S47 原样透传 |

`spent_micros` 的语义要写死：它是"已结算"的累计，不是"已预留"。用户看到的数字会比实际占用小。UI 必须同时显示"进行中任务可能产生更多费用"。

### 10.6 对 S15（音色授权，E30）

**S47 不投影授权态。** UI 的"这个音色可用吗"必须实时问 S15，或在使用时由 S15 拒绝。理由：授权撤销必须立即生效（E30），而投影必然滞后。

若产品要求"人物卡片上显示音色状态"，正确做法是**按需回源 + 短缓存（≤30s）+ 明确标注缓存时间**，不是往投影表里放一列。

### 10.7 对 S45（审计）

S47 只写少量审计意图：

| 动作 | 内容 |
|---|---|
| `projection.subscription.create` | 谁订阅了什么 |
| `notification.preference.change` | 通知偏好变更 |

**不写**"推送了一条 SSE"。那是海量、低价值的。S47 的 `projection.cursor.advanced` 事件已足以证明游标可达性。

`payload` 里的密钥命中由 S47 自己就地替换（§5.2），写 `audit` 时用的是替换后的形态，**不因命中而放弃写这条审计**——讳疾忌医。S45 的 `PAYLOAD_SECRET_SCAN` 是第二道，不是唯一一道；两边都以 [redaction-policy.v2](../contracts/redaction-policy.v2.json) 为准，不各自定义"什么算密钥"。

---

## 11. 契约缺口（如实报告，不自行补）

| # | 缺什么 | 影响 | 需要谁改 |
|---|---|---|---|
| **C47-1** | `openapi.v2.yaml` 有 `GET /runs/{run_id}/events`（`streamRun`），但**没有 `POST /subscriptions`、`GET /runs/{run_id}/snapshot`、`GET /runs`、`/notification-preferences`、`/notifications`** | **阻塞实现**：§4.1 的多数端点在契约里不存在 | `contracts/openapi.v2.yaml` |
| **C47-2** | `projection_watermark` 的"每 run 单调无洞"性质只在 S21 的方案文档里，**不在任何契约/schema 里** | S47 的缺口检测依赖它；若上游改了语义，S47 会静默误报 | `contracts/event-envelope.v2.schema.json` 的 `aggregate_sequence` 语义说明，或 `docs/04` |
| **C47-3** | 无 `run-snapshot.v1` 的 schema；`__hints.authority` 是本文提案 | 前端/Agent 无法按契约消费 | `contracts/` |
| **C47-4** | SSE 事件的格式（`id`/`event`/`data` 的具体形状、`event_class` 枚举）未定义 | 客户端无法机械解析 | `contracts/` + `docs/13` |
| **C47-5** | 游标保留窗口（15 分钟）、连接数上限、`410` 的 body 形状，全无定义 | E36 的验收没有基准 | `docs/03` 或 S47 的 SLO 条目 |
| **C47-6** | `event-topics.v2.json` 里 S47 只登记 `projection.cursor.advanced`。其节流语义（每订阅每秒 ≤1）没写 | 下游可能期待"每条 SSE 一条事件" | `contracts/event-topics.v2.json` |
| **C47-7** | 通知渠道的枚举（`in_app`/`email`/`webhook`）与 `webhook` 走 S46 的路径未定义（§10.4） | Webhook 通知不可用 | `contracts/` + C46-6 |
| **C47-8** | `service.yaml` 的 `depends_on` 只写了 `[S01]`，但 S47 消费 10 个 topic | 部署顺序与版本兼容无依据 | catalog |
| **C47-9** | S42 `C3`（段级渲染进度事件需与 S47 共同定义）仍未决 | S47 无法显示渲染进度条 | S42 + S47 共同定义 |
| **C47-10** | 无"投影可重建"的验收标准（重建耗时、重建期间的行为） | `service.yaml` acceptance 说"投影可重建"但没有量化 | `docs/16` E 条目或 S47 SLO |
| **C47-11** | `GET /runs/{run_id}/events` 在 openapi 的 `x-service-id` 是 S47（S21 方案 C21-4 已确认），但 `service.yaml` 的 `api` 声明与 openapi 的 operation 定义字段可能不一致 | SDK 生成可能归错服务 | 核对 `service.yaml` 与 openapi |
| **C47-12** | §2.1 原先写消费 `orchestration.node.state-changed`，但 `event-topics.v2.json` 里 S21 只登记 `orchestration.run.state-changed` 一条（S21 方案 C21-5 写明"把全部信息塞进这一个事件的 `data`"） | §3.4 的"node 级进度 200ms 合并"没有可消费的事件 | S21 要么单列 node 事件，要么 §3.4 改按 run 事件的 `data.node_states[]` 合并 |
| **C47-13** | §2.1 原先写消费 `artifact.registered` / `artifact.deleted`，注册表里 S28 只有 `artifact.ingested`（S28 方案 C28-3 写明 tombstone/派生完成/访问拒绝全部折叠进 `artifact.ingested`） | "制品可用性"投影拿不到删除事实 | S28 + `contracts/event-topics.v2.json` |
| **C47-14** | §2.1 原先写消费 `budget.reservation.settled`，注册表里 `drama.budget-ledger.v2` 只登记 `finance.ledger.entry-posted`（S23 方案 §1180 写明"只发这一种 event_type"，耗尽/告警走 `data.entry_type` + `data.budget_after.state`） | "已花费"水位要靠解析 `data.entry_type` 才能得到 | `contracts/event-topics.v2.json` 或 §5.6 明说 S47 不投影金额 |

**C47-1、C47-2、C47-4 是阻塞级。** C47-2 尤其危险——它不影响编译，只会在运行时静默失效。

C47-12/13/14 是同一类：**§2.1 的表里写出的事件名，在注册表中不存在，也不在 S21/S28/S23 任一方案里出现过。**
三份上游方案都明说"只发一个事件、其余语义折叠进 `data`"，所以这不是注册表漏登，是本方案的订阅面写得比上游实际发布的多。
处理方式三种，需 owners 定：上游拆事件 / 本方案改按 `data` 字段合并 / 明说不投影该项。**在此之前 §2.1 那三行按现状保留，不改写成读不出来的东西。**

---

## 12. 未决

1. **游标保留 15 分钟是否合适。** 太短（移动端切后台可能超时），太长（change_log 变大）。备选：按订阅活跃度动态调整（活跃的保留更久）。需要实测移动端行为。

2. **`GAPPED` 状态在 UI 上怎么呈现。** 本文的设计是"黄色提示 + 刷新按钮"。但若 gap 很频繁，UI 会一直有黄条，用户会习惯性忽略。备选：只在 gap 影响当前可见进度时提示。这需要前端与产品共同定。

3. **投影该不该缓存"当前节点耗时"。** 用户很关心"这一镜跑多久了"，但这是**推算值**（`now - node_started_at`），不是事件里的事实。本文没投影它，让前端用 `state_since` 自己算。若要在服务端算，就要接受"服务端时钟 ≠ 用户时钟"。

4. **多 run 的租户级看板（`GET /dashboard/summary`）的实时性要求。** 本文假设它可以是 30s 级缓存。若产品要求"10 个 run 的进度条都实时"，扇出成本会涨一个量级。

5. **S42 的段级渲染进度（C47-9）。** 这是真实的 UI 需求，但事件契约未定。可能的方向是 S42 发 `render.segment.progress`，S47 合并。需要 S42 先定义。

6. **通知的"已读"语义。** 本文只有 `read_at`。是否需要"稍后提醒"、是否需要跨设备同步已读？目前假设单设备足够。

7. **投影重建的时间预算。** 若 5000 个活跃 run 全部要重建，从事件流重放要多久？本文没算。E32 的"投影可恢复"需要这个数字。备选：重建只做"当前活跃的"（几百个），历史 run 按需重建。

8. **`spent_micros` 的"已结算"是否会让用户困惑。** 用户看到 12.8 元已花，但 S23 说预留了 20 元。UI 需要同时显示两者，而 S47 只有前者。这是产品决定。

---

## 13. 明确不做的

- **不做权威。** 任何"当前是什么"的强一致读都回源（§1.2）。
- **不做写回。** 结构上禁（§1.4、§8.3）。
- **不降级直连权威。** 投影挂了就是 503（§4.5）。
- **不投影余额、授权态、accepted pointer、正史**（§5.6）。
- **不自己实现 Webhook 签名与重试。** 归 S46（§10.4）。
- **不推送"无 watermark"的事件。** 一条都不行（§4.3）。
- **不把断线当成 run 故障。** 断线是 UI 的事实（§7.2）。
- **不做长期事件归档。** change_log 只留 15 分钟；长期留存在 S45/S28。
- **不绕过 S26/S23/S22。** S47 没有任何付费路径（§1.4）。
