# S45 · 审计与证据保全 —— 实现方案

状态：实现方案（W4）。`maturity: design_only` 不变；本文描述"照着能写代码"的形态，不是已实现系统。

上游依据：[docs/01 §3](../docs/01-enterprise-architecture.md)、[docs/04](../docs/04-data-consistency.md)、[docs/13](../docs/13-api-product.md)、[docs/14 §2–§3](../docs/14-enterprise-operations.md)、[S45 规格](../docs/design-package/services/S45-audit.md)。

配套：[S28 归档](S28-artifact-ingest.md)（删除编排的引用方之一）、[S43 交付](S43-delivery.md)（审计意图的来源）、[S23 账本](S23-budget-ledger.md)。

---

## 1. 边界：写什么 / 不写什么 / 一条必须坚持的原则

### 1.1 写什么

S45 是**审计事实**的唯一写者。它持有的四类事实：

| 事实 | 表 | 语义 |
|---|---|---|
| 追加式审计记录 | `audit_record` | "谁在何时、以什么身份、依据什么、对什么、做了什么、结果如何" |
| 链头与封印分段 | `audit_stream` / `audit_segment` | 让"未被篡改"可被证明，而不是被声明 |
| 保留、法务扣留与销毁令 | `audit_retention_policy` / `legal_hold` / `destruction_order` | E39 的"例外保留"与"可证明销毁" |
| 导出与取证回执 | `audit_export` / `audit_access_log` | 谁导出了什么、谁读了什么 |

### 1.2 不写什么

| 不是 S45 的事实 | 归属 |
|---|---|
| 当前 accepted pointer、当前 manifest、epoch、锁 | **S08**。S45 只有"某时刻 S08 接受过 rev 13"这一条**历史**，没有"现在是 13" |
| 金额、余额、预留 | **S23** |
| 配额 permit | **S22** |
| 生成任务状态、未知提交 | **S26** |
| 制品 bytes、hash、存储位置 | **S28** |
| 审核决定本身 | **S40** |
| 政策内容与判定 | **S02**（S45 只存 `policy_ref`，不存政策全文，不重放判定） |
| 谁现在有权限 | **S01** |
| 合规结论（"这条违规了"） | **没有人**。见 §1.3 |

**S45 不建任何"当前状态"表。** 没有 `current_manifest`、没有 `balance_snapshot`、没有 `who_has_permission`。任何"现在是什么"的查询必须回源。S45 的查询接口只回答"历史上发生过什么"。

这条不是洁癖：一旦 S45 存了 `latest_accepted_revision`，它就成了第二权威，且是最危险的一种——因为审计表不可回滚，它会永远保留一个可能已经错误的"当前值"。

### 1.3 一条必须坚持的原则

> **S45 只记录observation，不做判定。**

具体到代码上，这是三条禁令：

1. **没有 `violation` / `compliant` / `severity` / `risk_score` 字段。** 审计记录里的 `outcome` 是**来源服务报告的**结果（`SUCCEEDED` / `DENIED` / `ERROR` / `REPORTED_UNKNOWN`），不是 S45 判断的结果。别人说"拒绝"，S45 记"别人报告了拒绝"。
2. **没有规则引擎、没有表达式求值、没有跨记录推理。** 检测"某人在 3 分钟内改了 50 个资产"属于 SIEM，不属于审计存储。S45 提供可导出的记录流，SIEM 在外部分析。
3. **没有自动升级、没有自动告警工单。** 只有一条机械告警例外：**链路校验失败**（§3.5）。那是 S45 自己数据的完整性故障，不是业务合规判断。

### 1.4 与其它服务的边界对话

| 对方可能想做的事 | 正确做法 |
|---|---|
| "查 S45 确认这个 manifest 当前接受的是哪一版" | 问 S08。S45 只能回答"截至 T 有哪些接受记录" |
| "让 S45 在写入时顺便校验这笔操作是否越权" | 不。授权是 S01/S02 在**写入前**做的事；S45 只记录其结果 |
| "业务补偿失败时把审计记录也删掉，保持一致" | **禁止**。见 §1.5 |
| "S45 挂了就跳过审计继续跑" | 取决于 S02 策略里的 `audit.blocking`；S45 不替调用方决定（§7.1） |
| "把审计记录当成事件重放，重建业务状态" | 不。审计记录不是事件流，顺序语义是"记录顺序"不是"状态迁移顺序" |

### 1.5 审计行不可被任何业务补偿删除

这是 AGENTS.md「让某个服务变成第二个权威」与 docs/04 §6 的交点，必须写成结构约束而不是约定：

1. **S45 不提供任何删除/修改单条记录的 API。** 没有 `DELETE /audit-records/{id}`，没有 `PATCH`。契约层面就不存在这个动词。
2. **数据库角色只有 `INSERT` 与 `SELECT`。** `audit_record` 的应用角色被 `REVOKE UPDATE, DELETE`；另加触发器 `trg_audit_record_immutable` 对任何 `UPDATE`/`DELETE` 抛 `AUDIT_RECORD_IMMUTABLE`。即使迁移脚本写错也会被挡住。
3. **跨服务根本到不了这张表。** 每个服务独立 database 与写身份（docs/01 §2、E01），S23 的 `settle` 补偿、S26 的任务放弃、S28 的删除作业都只能写自己的库。它们**没有**通往 `audit_record` 的连接串。
4. **唯一的移除路径是整分区销毁**（§3.7），它需要：分段已封印并校验、归档已完成并回读校验、`destruction_order` 已由有权主体签署、无 `legal_hold` 覆盖。销毁操作本身写一条审计记录，且该记录**不在被销毁的分区内**。

所以"补偿逻辑把审计删了"在 S45 里不需要靠代码审查来防——它在权限、触发器、网络三条线上都做不到。

---

## 2. 输入契约

### 2.1 两个入口，只有一个是主路径

| 入口 | 用途 | 是否在调用方的业务事务里 |
|---|---|---|
| **事件消费**（主路径） | 调用方把审计意图写进**自己的** outbox，与业务写入同一本地事务；S45 消费并追加 | 否。但它与业务写入**同生共死**，因为 outbox 行跟业务行一个事务 |
| `POST /audit-records`（补充路径） | 没有本地业务事务可搭车的事实：S01 的访问判定、S46 的外部调用、运维操作、S45 自己的段销毁 | 否 |

**"关键命令同事务审计"的正确含义**（docs/14 §3）：指的是**调用方**在自己的数据库里，把 `audit.record.intent` 写进自己的 `outbox`，和业务写入同一个事务提交。**不是**指 S45 加入调用方的事务——那需要跨库事务，docs/04 §3 明确不假设。

这条必须写死，否则实现者会试图把 S45 调用塞进业务事务，然后为了"保证成功"加同步重试，最后把业务写路径的可用性绑到审计服务的可用性上。

### 2.2 审计意图（`audit.record.intent`）的载荷

契约里没有这个 schema（C45-1），以下是**本文建议的形状**，最终以裁决为准。

```jsonc
{
  "intent_id": "aint_01J...",          // 调用方生成，幂等键
  "schema_version": "1.0",
  "occurred_at": "2026-10-30T08:00:00.123Z",   // 调用方时钟
  "source_service": "S08",
  "source_verb": "manifest.accept",            // 调用方内部命令名，不解释
  "correlation_id": "cmd_01H...",
  "causation_id": "plan_01G...",

  "actor": {
    "principal_id": "usr_linlan",
    "principal_type": "human",                 // human|service|api_client|mcp_client
    "delegation_id": "dlg_..."
  },
  "on_behalf_of": { "principal_id": "svc_orchestrator", "principal_type": "service", "delegation_id": "..." },

  "action": "manifest.accept",                 // 稳定的点分动词，见 §2.3
  "targets": [
    { "service_id": "S08", "kind": "manifest", "id": "mf_ep3", "revision": 13, "content_hash": "ab…" }
  ],
  "primary_target_index": 0,                   // 决定写入哪条链（§3.2）

  "policy_ref":   { "service_id": "S02", "id": "pd_…",  "revision": 7, "content_hash": "…" },
  "evidence_refs":[ { "service_id": "S40", "id": "dec_…", "revision": 2, "content_hash": "…" } ],

  "outcome": "SUCCEEDED",                      // SUCCEEDED|DENIED|ERROR|REPORTED_UNKNOWN
  "outcome_detail": { "reason_code": null, "message": null },

  "details": { "accepted_revision": 13, "node_ids": ["shot_3_2.video"] }
}
```

四条硬规则：

- `details` 是**引用与计数**，不是内容。剧本正文、Prompt 全文、密钥、媒体 URL 签名一律不进（docs/14 §2、docs/04 §4）。需要正文时给 `data_ref`，指向 S28/S04 的权限化接口。
- `outcome=DENIED` 与 `ERROR` 必须写。**被拒绝的操作是最需要审计的**，绝不能因为"没成功所以不用记"而丢。
- `occurred_at` 是调用方时钟；S45 另记 `recorded_at`（自己的时钟）。两者差值入指标，**不因时钟偏移拒收**（§7.6）。
- `schema_version` 必须存在。S45 拒绝未知 major 版本，不猜。

### 2.3 `action` 的取值纪律

`action` 是开放字符串（点分动词），但**必须来自调用方自己的动词表**，S45 不做词表校验。原因见 §1.3：做词表校验就成了一个会拒绝业务的组件。

给实现者的建议：各服务在自己方案里列出动词（如 S08 的 `manifest.commit` / `manifest.accept` / `branch.fence`，S23 的 `budget.reserve` / `budget.settle`，S43 的 `delivery.publish`）。S45 只做一件事：**同一 `(source_service, source_verb)` 的 `action` 必须稳定**，否则取证查询会漏。这条靠对账作业检查（§8.3），不靠运行时拒绝。

### 2.4 谁会写审计

按已完成方案的声明，至少以下服务会产出审计意图。这里是**接口约定**，不是依赖关系：

| 来源 | 典型记录 | 依据 |
|---|---|---|
| S01 | 授权判定、撤权、kill switch 翻转 | docs/14 §1 |
| S02 | 政策 revision 发布、`evaluate` 拒绝 | S02 方案 |
| S07 | ChangePlan 提交与应用 | S07 §11 |
| S08 | `manifest.commit` / `accept` / `fence`，含 `REJECTED_*` | S08 §1.4 |
| S16 | checkpoint 提交、正史合并 | S16 方案 |
| S23 | `reserve` / `settle` / `release` / `grant_escrow` | S23 §7 |
| S24 | 生命周期迁移、价格快照 | S24 §8 |
| S26 | 准入签发、`SUBMISSION_UNKNOWN` 进入与判定 | S26 §7 |
| S28 | 导入、tombstone、销毁完成 | S28 §4.8 |
| S40 | 审核决定（含理由文本） | S40 §3.4 |
| S41 | 时间线提交 | S41 §8.3 |
| S43 | 包封、外发 | S43 §4 |
| S44 | 口径变更（"这个季度成本口径调整为…"） | S44 方案 |
| S46 | 外部调用、API key 轮换 | 本组 S46 §11 |
| S48 | 训练启动、数据授权复核、artifact 注册 | 本组 S48 §11 |

**S45 不反向依赖这些服务。** `depends_on: [S01]` 只用于两件事：读写审计记录的授权判定，以及租户驻留/状态。审计的**写入**不需要回源任何业务服务——这正是"只记录、不判定"的工程含义。

---

## 3. 核心逻辑与状态机

### 3.1 追加的完整路径

```text
调用方本地事务:
  BEGIN
    ...业务写入...
    INSERT INTO <caller>.outbox(... event_type='audit.record.intent' ...)
    INSERT INTO <caller>.outbox_audit_tracking(intent_id, criticality)   -- 仅 strict 租户
  COMMIT
        │
        ▼  caller outbox relay（至少一次，14 天）
Kafka: drama.audit.ingress.v2          ← 提案 topic，C45-4
        │
        ▼
S45 consumer:
  BEGIN
    INSERT INTO inbox(consumer='s45.ingress', event_id) ON CONFLICT DO NOTHING
    -- 0 行 → 重复投递，直接 return
    SELECT ... FROM audit_stream WHERE stream_key=$k FOR UPDATE     -- 串行点
    prev_hash ← audit_stream.head_hash
    record_hash ← sha256(prev_hash || JCS(record_without_hash))
    INSERT INTO audit_record(..., stream_seq, prev_hash, record_hash)
    INSERT INTO audit_record_target(...)                            -- 索引，可重建
    UPDATE audit_stream SET head_seq=..., head_hash=record_hash
  COMMIT
```

**没有 outbox 的入账。** S45 追加成功后不需要再发事件——除 `audit.export.ready`（§3.6）。这是刻意的：如果每条审计记录都发一条事件，就会形成一个"审计自己的事件也要被审计"的回路，且事件总线承担了本该由查询承担的工作。

### 3.2 链的分段依据

`stream_key` 的选择直接决定取证查询能不能一次走完：

```text
tenant:<t>:subject:<kind>:<id>     例：tenant:t_demo:subject:manifest:mf_ep3
tenant:<t>:subject:budget:<id>
tenant:<t>:subject:episode:<id>
tenant:<t>:meta                    ← 租户级事件（成员、策略、保留、导出）
```

规则：

- 每条记录只进**一条**链，链由 `targets[primary_target_index]` 决定。
- `targets` 里的**所有**目标都写进 `audit_record_target`，所以对次要目标的查询走索引表，不重算链。索引表是 `audit_record` 的派生视图，**可随时全量重建**，不是第二份事实。
- 一条命令影响 N 个目标时，只产生 1 条记录（不是 N 条）。链上的顺序 = 记录的追加顺序，等价于"这一集这一分支上发生事情的顺序"，这正是 forensics 需要的。

为什么不用 `tenant:<t>` 单一链：租户内所有写会在同一行上串行，热租户的审计入口变成全局瓶颈。按 subject 分链后，不同集/不同预算的审计完全并行。

代价：**跨 subject 的全局时序不保证**。要回答"这个租户在 10:00–10:05 之间发生了什么"，走 `audit_record(tenant_id, recorded_at)` 索引扫描，而不是链。这是有意的：链证明完整性，索引提供检索，两件事分开。

### 3.3 链的算法

```text
canonical = JCS({ tenant_id, stream_key, stream_seq, recorded_at, occurred_at,
                  source_service, source_event_id, action, actor, targets,
                  policy_ref, evidence_refs, outcome, outcome_detail, details,
                  correlation_id, causation_id, schema_version })
record_hash = sha256_hex(prev_hash_hex_ascii || canonical_utf8)
genesis: prev_hash = "0" * 64
```

三条必须明确的实现细节：

1. **`record_hash` 自身不参与规范化。** 否则自指。
2. **`recorded_at` 参与哈希。** 它是 S45 的时钟，写死后不再变。用 `timestamptz` 的微秒精度固定字符串形态（`YYYY-MM-DDTHH:MM:SS.ffffffZ`），避免时区/精度差异导致重算不一致。
3. **重算必须能离线进行。** runbook `verify-audit-chain --segment <id>` 只读分段导出文件，不连业务库。E39 的"可证明"依赖这条能独立跑。

### 3.4 状态机：分段（`audit_segment`）

```text
   OPEN ──(达到 seal 条件)──► SEALING ──(root hash 写入)──► SEALED
                                                              │
                                                              │ 上传对象存储
                                                              ▼
                                                          ARCHIVED
                                                              │
                                                              │ TSA/外部锚定（可选）
                                                              ▼
                                                          ANCHORED
                                                              │
                                                              │ 分区销毁（§3.7）
                                                              ▼
                                                          DESTROYED
```

| 从 | 到 | 触发 | 失败处理 |
|---|---|---|---|
| （无） | OPEN | 该 stream 第一次追加时建段 | — |
| OPEN | SEALING | `record_count >= 10000` 或 `now - opened_at >= 1h` 或 `now - last_append >= 15min` | 封印失败退回 OPEN，下一轮重试 |
| SEALING | SEALED | 计算 `root_hash`（= 本地段最后一条的 `record_hash`）与 `record_count` 并在同事务写入 | 事务回滚则仍是 OPEN，可重算出不同 root hash 吗？**不能**——SEALING 期间该 stream 的追加被 `audit_stream` 行锁挡住，root hash 唯一 |
| SEALED | ARCHIVED | 对象存储写入成功且回读 checksum 一致 | 重试，指数退避，不跳步 |
| ARCHIVED | ANCHORED | 可选：把 `root_hash` 提交到外部时间戳服务/WORM 锚 | 失败不阻塞主流程，`anchor_state=PENDING` 告警 |
| ANCHORED / ARCHIVED | DESTROYED | 分区销毁令执行完毕 | 见 §3.7 |

**`SEALING` 期间的串行点**：`UPDATE audit_stream SET seal_state='SEALING' WHERE stream_key=$k`（已持有行锁）。任何新的追加在这个 stream 上 409 `STREAM_SEALING`（retryable），消费者退避重试。不要让追加绕过封印——那会让 root hash 与记录集合不一致，等于自己破坏自己的证据。

### 3.5 完整性校验

三种校验，粒度不同：

| 校验 | 范围 | 触发 | 成本 |
|---|---|---|---|
| **在线校验** | 单条：`record_hash` 与 `prev_hash` 衔接 | 每次追加时天然成立（事务内算） | 0 |
| **分段校验** | 一个分段的全部记录重算 | 封印时 + 每日抽检 1% + 导出时必做 | O(n) |
| **全量校验** | 一个 stream 从 genesis 到 head | 按需（司法取证、季度审计） | O(N)，在只读副本上跑 |

失败语义：

```text
CHAIN_BROKEN → 该段置 segment.state='INTEGRITY_FAILED'
             → 立即 P1 告警（这是 S45 唯一允许的自动告警）
             → 不自动修复、不重算覆盖（重算覆盖才是真正的篡改）
             → 保留原始字节与失败点 seq，供取证
```

**绝不允许在校验失败时"重建"这一段。** 重建意味着用当前数据库内容覆盖归档内容——那正好抹掉了要查的差异。正确动作是冻结并升级为安全事件。

### 3.6 导出（`audit_export`）

```text
REQUESTED ──► BUILDING ──► SEALED ──► READY
     │            │            │
     └────────────┴────────────┴──► FAILED（不可重试的错误）
                       │
                       └──► 重试（可恢复错误）
READY ──(expires_at 到)──► EXPIRED
```

导出物的形状（`audit-export-bundle.v1`，C45-2）：

```text
<bundle_id>/
  manifest.json          # 查询条件、记录数、时间范围、每个分段的 from_seq/to_seq/root_hash
  segments/
    seg_01J....jsonl     # 一行一条，与 audit_record 的行一一对应
  chain-proof.json       # 分段边界 hash 与相邻分段的衔接证明
  bundle.sha256          # 对上面全部文件按固定顺序的摘要
```

`manifest.json` 里**必须有** `chain_continuous: true|false` 与 `gaps: [{from_seq,to_seq,reason}]`。有缺口就如实写缺口——例如某段因 `INTEGRITY_FAILED` 被跳过。**导出不能假装完整**，这是 E39 与 E37「不伪造完整性」同一条纪律。

导出是**异步**的：`POST /audit-exports` 返回 `202 + export_id`；完成时发 `audit.export.ready`。原因：全量导出可能是几百万条，同步做会把读库打穿。

### 3.7 保留与销毁（E39 的主体）

四档保留，按租户配置：

| 档 | 落地 | 默认 | 可查性 |
|---|---|---|---|
| **hot** | Postgres 月分区 | 90 天 | 直接 SQL，P95 ≤ 500ms |
| **warm** | 对象存储（区域内部，标准类） | 90 天 – 2 年 | 经 `audit_segment` 定位后按对象读取；P95 ≤ 5s |
| **cold / WORM** | 对象存储（object lock，合规模式） | 2 年 – 合同期 | 同上，且不可删改 |
| **proof-only** | Postgres 元数据行（无正文） | 永久 | 只有分段元数据与销毁令，正文已不存在 |

**"归档后仍可追溯"的取证路径**（这是任务点名的要求，写清楚四步）：

```text
1. 定位候选分段
   SELECT seg_id, from_seq, to_seq, root_hash, storage_ref, storage_class
     FROM audit_segment
    WHERE tenant_id=$t AND stream_key=$k
      AND tstzrange(always_from, always_to) && $range
      AND state IN ('SEALED','ARCHIVED','ANCHORED','DESTROYED');
   -- 索引：(tenant_id, stream_key, always_from)

2. 对 DESTROYED 段：没有正文，但有 root_hash 与 destruction_order_ref。
   响应里如实返回 {records: [], proof_only: true, root_hash, destroyed_at, order_ref}。
   这回答的是"这段曾经存在且未被篡改，已于 T 依令销毁"，不是"查无此记录"。
   两者在取证上是不同结论，绝不能都返回空数组。

3. 对 ARCHIVED/ANCHORED 段：平台签发短时读票 → 读对象 → 逐行校验 record_hash 链
   → 校验首行 prev_hash 等于上一段的 root_hash
   → 响应带 verified: true 与 segment_root_hash。

4. 需要可交付的证据时走导出（§3.6），不是直接读对象。
```

**销毁的五个前置条件**（缺一即阻塞，`destruction_job.state='BLOCKED'`）：

1. 该分区所有分段 `state >= ARCHIVED`（归档完成，不是"正在归档"）；
2. 回读校验通过（从对象存储读回来重算 root hash，与 `audit_segment.root_hash` 一致）；
3. 无 `legal_hold` 覆盖该范围（按 tenant + subject + 时间范围匹配）；
4. `destruction_order` 已签署（`authorized_by` 必须是合同指定的角色，S01 授权，`action='audit.destroy'`）；
5. 该租户的 `retention_total_days` 已到期。

执行：

```sql
BEGIN;
  INSERT INTO destruction_order(...);                       -- 自身也是审计记录的对象
  INSERT INTO audit_record(... action='audit.segment.destroy' ...);  -- 写进 meta 链
  ALTER TABLE audit_record DETACH PARTITION audit_record_2026_01;
COMMIT;
-- 提交后才 DROP；崩溃则由 janitor 依据 destruction_order 清理已 DETACH 的孤儿表
DROP TABLE audit_record_2026_01;
```

`DETACH PARTITION` 在 Postgres 里是事务性的，所以"销毁令落库"与"数据脱离可查集合"是一个原子事实。若在 COMMIT 后、DROP 前崩溃：数据还在一个已 DETACH 的表里，janitor 检查 `destruction_order` 存在性后 DROP 并告警。**不会出现"销毁令写了但数据还在主查询集合里"**。

**销毁留什么**：`audit_segment` 行（含 `root_hash`、`record_count`、`from_seq/to_seq`）、`destruction_order`（含授权人、依据、范围、执行时间）、以及 meta 链上那条 `audit.segment.destroy` 记录。销毁**不保留**被销毁记录的正文——那是"删除"（docs/04 §6）。

### 3.8 读取本身也是审计事实

`GET /audit-records`、导出创建、导出下载都写 `audit_access_log`：

```text
who / when / query_hash / result_count / export_id? / purpose? / trace_id
```

**不写查询返回的记录正文**，只写 `query_hash`（JCS 后的 sha256）与命中集合的 `(stream_key, from_seq, to_seq)` 摘要。理由：把读到的内容再写进审计，会让敏感内容二次扩散，且会自我放大（读审计记录 → 产生审计记录 → 被读 → …）。

**递归在此截断**：读 `audit_access_log` 本身只写结构化日志，不写 `audit_access_log`。这是唯一的例外，必须写进 runbook，否则实现者会在此处死循环。

---

## 4. 输出契约

### 4.1 命令入口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/audit-records` | 补充路径的追加（§2.1）。成功返回 `201` + `record_id` + `stream_seq` + `record_hash` |
| POST | `/audit-exports` | 创建导出作业，`202` + `export_id` |
| POST | `/audit-exports/{id}:verify` | 对已 READY 的导出重算 bundle 校验，返回每段 `CHAIN_OK`/`CHAIN_BROKEN` |
| POST | `/retention-policies/{tenant_id}` | 修订保留策略（需 `action='audit.retention.change'` 授权） |
| POST | `/legal-holds` / `:release` | 法务扣留的建立与解除 |
| POST | `/destruction-orders` | 创建销毁令（不执行），执行由后台作业按 §3.7 五条前置条件判定 |

### 4.2 查询入口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/audit-records?stream_key=&from_seq=&to_seq=&action=&actor=&occurred_from=&occurred_to=&outcome=&limit=&cursor=` | 主检索 |
| GET | `/audit-records/{record_id}` | 单条（含 `prev_hash`/`record_hash`，可独立验算） |
| GET | `/records/by-target?service_id=&kind=&id=&from=&to=` | 走 `audit_record_target` 索引 |
| GET | `/audit-exports/{id}` | 导出作业状态与下载票 |
| GET | `/segments?stream_key=&state=` | 分段索引 |
| GET | `/segments/{id}/proof` | 分段证明：root hash、边界 hash、锚定信息、相邻衔接 |

**所有查询响应必须带三个字段**（docs/04 §5）：

```jsonc
{
  "source": "S45",                     // 声明这是历史事实的来源
  "as_of": "2026-10-30T08:05:00Z",     // 本次查询快照时间
  "coverage": {                         // 回答"你查的范围里有没有洞"
    "hot_partitions": ["2026_10"],
    "archived_segments_queried": 3,
    "proof_only_segments": 1,           // 已销毁，只有证明
    "gaps": []
  }
}
```

`coverage` 是 S45 查询契约里最重要的字段。一个取证查询返回 0 条，可能是"没发生"，也可能是"这段已归档但归档失败"——没有 `coverage` 就无法区分。

### 4.3 发布事件

只有一条：`audit.export.ready`（topic `drama.audit.v2`，`aggregate_id = export_id`）。

```jsonc
{
  "specversion": "1.0", "schema_version": "2.0",
  "id": "evt_...", "source": "S45", "type": "audit.export.ready",
  "subject": "audit-export/exp_01J...",
  "time": "2026-10-30T08:10:00Z",
  "tenant_id": "t_demo", "home_cell": "cell_a1",
  "aggregate_id": "exp_01J...", "aggregate_sequence": 1,
  "correlation_id": "cmd_...", "causation_id": null,
  "traceparent": "00-...-...-01",
  "data": {
    "export_id": "exp_01J...",
    "state": "READY",
    "record_count": 128774,
    "segment_count": 14,
    "chain_continuous": true,
    "bundle_ref": { "service_id": "S45", "id": "exp_01J...", "revision": 1, "content_hash": "…" },
    "expires_at": "2026-11-06T08:10:00Z"
  },
  "data_ref": "audit-export/exp_01J.../manifest.json"
}
```

**不发** `audit.record.appended`。原因在 §3.1 已述，另有一条：如果每条记录都发事件，S47 和 SIEM 会消费一个每秒数万的流，而它们真正需要的是"按需查询 + 导出"。这条登记为缺口 C45-4（如果上游裁决要求发，至少必须是 `data_ref` 指向导出，不能是每条记录）。

### 4.4 错误码

| HTTP | code | retryable | 场景 |
|---|---|---|---|
| 400 | `MALFORMED_REQUEST` | false | JCS 失败、缺 `schema_version`、`targets` 为空 |
| 401 | `UNAUTHENTICATED` | false | 无身份 |
| 403 | `TENANT_SCOPE_DENIED` | false | header 与认证租户不一致 |
| 403 | `AUDIT_READ_DENIED` | false | 无 `audit.read` scope（S01 判定） |
| 403 | `AUDIT_EXPORT_DENIED` | false | 无 `audit.export` scope |
| 403 | `SOURCE_MISMATCH` | false | 事件 `source` 与认证的 producer 身份不一致（防冒名写入） |
| 404 | `NOT_FOUND` | false | **含跨租户**：别的租户的 record/stream 一律 404（E28） |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同 `command_id` 不同 `request_hash` |
| 409 | `COMMAND_IN_PROGRESS` | true | 同键同 hash 正在执行 |
| 409 | `STREAM_SEALING` | true | 目标 stream 正在封印，`Retry-After: 2` |
| 409 | `EXPORT_NOT_READY` | true | 对非 READY 的导出做 verify |
| 409 | `LEGAL_HOLD_ACTIVE` | false | 试图销毁被扣留范围 |
| 422 | `SCHEMA_INVALID` | false | 载荷不符合 §2.2 |
| 422 | `PAYLOAD_TOO_LARGE` | false | `details` 超过 64KB（正文应走 `data_ref`） |
| 422 | `RESIDENCY_VIOLATION` | false | 记录所属 tenant 的 home_region 与本 cell 不符（E31） |
| 429 | `AUDIT_SINK_BACKPRESSURE` | true | 摄入队列满；调用方保留自己的 outbox 行（§7.1） |
| 503 | `DEPENDENCY_UNAVAILABLE` | true | S01 授权服务不可达且超出内联预算 |

---

## 5. 数据模型

数据库 `audit`，Postgres 16。分区表用声明式分区。所有表带 `tenant_id`，所有查询强制带 `tenant_id` 谓词。

### 5.1 `audit_record` —— 追加式事实（按月分区）

```sql
CREATE TABLE audit_record (
  record_id        text NOT NULL,                    -- 'arec_01J...'
  tenant_id        text NOT NULL,
  stream_key       text NOT NULL,
  stream_seq       bigint NOT NULL CHECK (stream_seq >= 1),

  recorded_at      timestamptz NOT NULL,             -- S45 时钟，参与哈希
  occurred_at      timestamptz NOT NULL,             -- 调用方时钟，参与哈希
  clock_skew_ms    bigint,                           -- recorded_at - occurred_at，仅观测

  source_service   text NOT NULL,                    -- 'S08'
  source_verb      text NOT NULL,                    -- 'manifest.accept'
  source_event_id  text,                             -- 事件入口时非空
  source_intent_id text,                             -- 命令入口时非空
  correlation_id   text,
  causation_id     text,

  action           text NOT NULL,
  actor            jsonb NOT NULL,                   -- {principal_id,principal_type,delegation_id}
  on_behalf_of     jsonb,                            -- 代理调用时的委托链顶层
  targets          jsonb NOT NULL,                   -- [{service_id,kind,id,revision,content_hash}]
  primary_target_index smallint NOT NULL DEFAULT 0,

  policy_ref       jsonb,
  evidence_refs    jsonb NOT NULL DEFAULT '[]',
  outcome          text NOT NULL
    CHECK (outcome IN ('SUCCEEDED','DENIED','ERROR','REPORTED_UNKNOWN')),
  outcome_detail   jsonb NOT NULL DEFAULT '{}',
  details          jsonb NOT NULL DEFAULT '{}',

  schema_version   text NOT NULL,

  prev_hash        char(64) NOT NULL,
  record_hash      char(64) NOT NULL,
  canonical_sha256 char(64) NOT NULL,                -- 规范化后的摘要，便于独立复算

  CONSTRAINT ar_seq_positive CHECK (stream_seq >= 1),
  CONSTRAINT ar_hash_len CHECK (record_hash ~ '^[0-9a-f]{64}')
) PARTITION BY RANGE (recorded_at);

CREATE TABLE audit_record_2026_10 PARTITION OF audit_record
  FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');

-- 主键在分区表上必须含分区键
ALTER TABLE audit_record ADD PRIMARY KEY (tenant_id, recorded_at, record_id);
CREATE UNIQUE INDEX ar_stream_seq_uniq ON audit_record (tenant_id, stream_key, stream_seq);
CREATE UNIQUE INDEX ar_hash_uniq      ON audit_record (tenant_id, record_hash);
CREATE INDEX ar_time_idx  ON audit_record (tenant_id, recorded_at DESC);
CREATE INDEX ar_action_idx ON audit_record (tenant_id, action, recorded_at DESC);
CREATE INDEX ar_actor_idx  ON audit_record (tenant_id, (actor->>'principal_id'), recorded_at DESC);

-- 行级不可变：任何 UPDATE/DELETE 都被拒绝（§1.5）
CREATE OR REPLACE FUNCTION audit_record_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'AUDIT_RECORD_IMMUTABLE: % on % is forbidden', TG_OP, TG_TABLE_NAME;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_record_immutable
  BEFORE UPDATE OR DELETE ON audit_record
  FOR EACH ROW EXECUTE FUNCTION audit_record_immutable();
```

**`audit_record` 刻意不含 `segment_id` 列。** 分段归属若用外键表达，就必须在封印时回填——而回填是一次 `UPDATE`，与 §1.5 的行级不可变直接冲突。改成用 `audit_segment` 的 `(stream_key, from_seq, to_seq)` 范围表达：查询时按范围判定归属，记录本身永远不知道自己在哪一段。范围比外键更稳——分段边界可以重封，外键不能改。`audit_segment` 的 `EXCLUDE` 约束（§5.3）保证同 stream 的分段区间互不重叠，于是"给定 `stream_seq` 能唯一定位段"由数据库保证。

### 5.2 `audit_stream` —— 链头

```sql
CREATE TABLE audit_stream (
  tenant_id     text NOT NULL,
  stream_key    text NOT NULL,
  stream_kind   text NOT NULL
    CHECK (stream_kind IN ('subject','meta')),
  head_seq      bigint NOT NULL DEFAULT 0,
  head_hash     char(64) NOT NULL,
  seal_state    text NOT NULL DEFAULT 'OPEN'
    CHECK (seal_state IN ('OPEN','SEALING')),
  open_segment_id text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, stream_key),
  CONSTRAINT as_hash_len CHECK (head_hash ~ '^[0-9a-f]{64}')
);
CREATE INDEX as_kind_idx ON audit_stream (tenant_id, stream_kind);
```

`head_hash` 的初值是 `'0'*64`。`UPDATE ... SET head_seq=..., head_hash=...` 是这张表**唯一**允许的更新，且必须在持有 `SELECT ... FOR UPDATE` 的事务里做。

`audit_stream` 是**可变的**（这是它存在的原因），所以它**不是**证据本身。证据在 `audit_record` 与 `audit_segment.root_hash`。若 `audit_stream` 被破坏，可以用最后一段的 `root_hash` 与最大 `stream_seq` 重建；若两者矛盾，以 `audit_segment` 为准并告警。

### 5.3 `audit_segment` —— 封印分段

```sql
CREATE TABLE audit_segment (
  segment_id      text PRIMARY KEY,                  -- 'aseg_01J...'
  tenant_id       text NOT NULL,
  stream_key      text NOT NULL,
  stream_kind     text NOT NULL,

  from_seq        bigint NOT NULL CHECK (from_seq >= 1),
  to_seq          bigint NOT NULL CHECK (to_seq >= from_seq),
  from_prev_hash  char(64) NOT NULL,                 -- 段内首条记录的 prev_hash
  root_hash       char(64) NOT NULL,                 -- 段内末条记录的 record_hash
  record_count    integer NOT NULL CHECK (record_count >= 0),

  always_from     timestamptz NOT NULL,
  always_to       timestamptz NOT NULL,

  state           text NOT NULL DEFAULT 'OPEN'
    CHECK (state IN ('OPEN','SEALING','SEALED','ARCHIVED','ANCHORED',
                     'INTEGRITY_FAILED','DESTROYED')),
  storage_class   text NOT NULL DEFAULT 'HOT_PG'
    CHECK (storage_class IN ('HOT_PG','WARM_S3','COLD_WORM')),
  storage_ref     text,                              -- s3://…/seg_01J....jsonl.zst
  bytes_uncompressed bigint,
  sealed_at       timestamptz,
  archived_at     timestamptz,
  anchor_ref      text,                              -- TSA token / WORM 版本号
  anchored_at     timestamptz,
  integrity_checked_at timestamptz,
  destruction_order_id text,

  CONSTRAINT aseg_range_nonempty CHECK (to_seq >= from_seq),
  CONSTRAINT aseg_sealed_has_root CHECK (state IN ('OPEN','SEALING') OR root_hash IS NOT NULL),
  EXCLUDE USING gist (
    tenant_id WITH =, stream_key WITH =,
    int8range(from_seq, to_seq, '[]') WITH &&
  )
);
CREATE INDEX aseg_lookup_idx ON audit_segment (tenant_id, stream_key, always_from DESC);
CREATE INDEX aseg_state_idx  ON audit_segment (tenant_id, state, archived_at);
```

`EXCLUDE` 约束保证同一条 stream 上的分段区间**互不重叠**——这是"给定 `stream_seq` 能唯一定位段"的数据库级保证。没有它，重封会产生两个都声称覆盖 seq 500 的段，取证时结果取决于查询顺序。

### 5.4 `audit_record_target` —— 次要目标索引（可重建）

```sql
CREATE TABLE audit_record_target (
  tenant_id      text NOT NULL,
  target_service text NOT NULL,
  target_kind    text NOT NULL,
  target_id      text NOT NULL,
  recorded_at    timestamptz NOT NULL,
  record_id      text NOT NULL,
  stream_key     text NOT NULL,
  stream_seq     bigint NOT NULL,
  PRIMARY KEY (tenant_id, target_service, target_kind, target_id, recorded_at, record_id)
) PARTITION BY RANGE (recorded_at);

CREATE TABLE audit_record_target_2026_10 PARTITION OF audit_record_target
  FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
```

**这张表可以在任何时候从归档分段全量重建。** runbook `rebuild-target-index --from-segment <id>` 逐行读 JSONL 重放。它不是第二份事实——但如果它丢了一行，取证会漏，所以它的重建过程本身要写一条 meta 审计记录。

### 5.5 保留、扣留、销毁

```sql
CREATE TABLE audit_retention_policy (
  tenant_id            text PRIMARY KEY,
  retention_hot_days   integer NOT NULL DEFAULT 90,
  retention_warm_days  integer NOT NULL DEFAULT 730,
  retention_total_days integer,                       -- NULL = 合同期内不销毁
  worm_required        boolean NOT NULL DEFAULT false,
  anchor_required      boolean NOT NULL DEFAULT false,
  strict_audit         boolean NOT NULL DEFAULT false, -- 影响 §7.1 的阻塞口径
  updated_by           jsonb NOT NULL,
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT arp_hot_lt_warm CHECK (retention_total_days IS NULL
                                    OR retention_hot_days <= retention_warm_days),
  CONSTRAINT arp_warm_lt_total CHECK (retention_total_days IS NULL
                                    OR retention_warm_days <= retention_total_days)
);

CREATE TABLE legal_hold (
  hold_id       text PRIMARY KEY,
  tenant_id     text NOT NULL,
  scope_kind    text NOT NULL CHECK (scope_kind IN ('tenant','subject','time_range')),
  scope         jsonb NOT NULL,                       -- {stream_key?|time_range?}
  reason        text NOT NULL,
  case_ref      text,                                 -- 法务单号
  authorized_by jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  released_at   timestamptz,
  released_by   jsonb
);
CREATE INDEX lh_active_idx ON legal_hold (tenant_id) WHERE released_at IS NULL;

CREATE TABLE destruction_order (
  order_id      text PRIMARY KEY,
  tenant_id     text NOT NULL,
  scope         jsonb NOT NULL,                       -- {partition:'2026_01'} 或 {stream_key,to_seq}
  authorized_by jsonb NOT NULL,
  policy_ref    jsonb NOT NULL,
  hold_check    jsonb NOT NULL,                       -- 查询时刻的扣留检查结果快照
  preconditions jsonb NOT NULL,                       -- §3.7 五条的逐条结果
  state         text NOT NULL
    CHECK (state IN ('REQUESTED','BLOCKED','EXECUTING','EXECUTED','FAILED')),
  executed_at   timestamptz,
  record_count_destroyed bigint,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX do_state_idx ON destruction_order (tenant_id, state);
```

### 5.6 导出与访问

```sql
CREATE TABLE audit_export (
  export_id      text PRIMARY KEY,
  tenant_id      text NOT NULL,
  query          jsonb NOT NULL,                      -- 规范化后的查询条件
  query_hash     char(64) NOT NULL,
  state          text NOT NULL
    CHECK (state IN ('REQUESTED','BUILDING','SEALED','READY','EXPIRED','FAILED')),
  segment_ids    text[] NOT NULL DEFAULT '{}',
  record_count   bigint NOT NULL DEFAULT 0,
  bundle_ref     jsonb,                               -- {storage_ref, sha256, size_bytes}
  chain_continuous boolean,
  gaps           jsonb NOT NULL DEFAULT '[]',
  failure_reason jsonb,
  requested_by   jsonb NOT NULL,
  purpose        text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz,
  expires_at     timestamptz
);
CREATE INDEX ae_tenant_idx ON audit_export (tenant_id, created_at DESC);
CREATE UNIQUE INDEX ae_idem_idx ON audit_export (tenant_id, query_hash, created_at)
  WHERE state IN ('REQUESTED','BUILDING');

CREATE TABLE audit_access_log (
  access_id     text PRIMARY KEY,
  tenant_id     text NOT NULL,
  accessed_at   timestamptz NOT NULL DEFAULT now(),
  principal     jsonb NOT NULL,
  access_kind   text NOT NULL
    CHECK (access_kind IN ('QUERY','EXPORT_CREATE','EXPORT_DOWNLOAD','VERIFY')),
  query_hash    char(64),
  export_id     text,
  result_count  integer,
  coverage      jsonb,
  purpose       text,
  trace_id      text
) PARTITION BY RANGE (accessed_at);

CREATE TABLE audit_access_log_2026_10 PARTITION OF audit_access_log
  FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE INDEX aal_principal_idx ON audit_access_log (tenant_id, accessed_at DESC);
```

### 5.7 幂等、Inbox、Outbox

```sql
CREATE TABLE idempotency_record (
  tenant_id      text NOT NULL,
  command_id     text NOT NULL,
  request_hash   char(64) NOT NULL,
  command_type   text NOT NULL,        -- 'audit.record.append' | 'audit.export.create' | ...
  state          text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code  integer,
  response_body  jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE inbox (
  consumer    text NOT NULL,           -- 's45.ingress' | 's45.access' | 's45.retention'
  event_id    text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
CREATE INDEX inbox_ttl_idx ON inbox (received_at);

CREATE TABLE outbox (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id         text NOT NULL,
  aggregate_id      text NOT NULL,     -- export_id
  aggregate_sequence bigint NOT NULL,
  event_type        text NOT NULL,
  partition_key     text NOT NULL,     -- tenant_id || ':' || aggregate_id
  payload           jsonb NOT NULL,
  headers           jsonb NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  published_at      timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;
```

`outbox` 只承载 `audit.export.ready`。审计记录本身**不经 outbox**——它们是直接追加的本地事实。

### 5.8 不建的表

| 不建 | 为什么 |
|---|---|
| `audit_current_state` / `latest_*` | 第二权威（§1.2） |
| `compliance_violation` / `risk_score` | S45 不判定（§1.3） |
| `audit_record_archive_copy` | 归档正文在对象存储，DB 里再存一份等于两份都可能不一致 |
| `siem_forwarded_offset` | 外发游标属于 SIEM 连接器（未来服务），不在 S45 |
| 全局跨租户索引 | E28 + E31：跨租户聚合会泄漏存在性，跨地域聚合违反驻留 |

---

## 6. 幂等与并发

### 6.1 三套幂等键，各有明确用途

| 键 | 作用域 | 用于 | 冲突语义 |
|---|---|---|---|
| `(tenant_id, command_id)` + `request_hash` | 命令入口 | 重试同一 `POST` | 同 hash 回放首次响应；异 hash 409 |
| `(consumer, event_id)` | 事件入口 | 事件重复投递 | 静默跳过 |
| `(tenant_id, stream_key, stream_seq)` | 链内 | 防御性：同一 seq 不能写两次 | 唯一约束拒绝 |

三者的关系：**事件/命令入口的去重是"不重复追加"，链内唯一约束是"追加错了也进不去"。** 前者是性能与语义，后者是完整性兜底。两者都要有。

### 6.2 同键不同 body

```text
request_hash = sha256(JCS(body 去掉 command_id 与 request_hash))
```

与 S08 §2.1、S22/S23 同一算法，RFC 8785。`POST /audit-records` 的响应体带 `replayed: true` 以便排查。

### 6.3 追加的串行点只有一处

```text
SELECT head_seq, head_hash FROM audit_stream
 WHERE tenant_id=$t AND stream_key=$k FOR UPDATE;
```

这一行锁同时提供三个保证：

1. `stream_seq` 单调无洞（`head_seq + 1`）；
2. `prev_hash` 唯一（读到的一定是最新 head）；
3. 封印期间（`seal_state='SEALING'`）追加被拒（§3.4）。

**不加 advisory lock。** 行锁已经在这个 key 上，再加 advisory 只是重复，且 advisory 不参与 MVCC，回滚语义更难推理（与 S08 §5.1 同一理由）。

**锁持有时间必须极短**：追加事务里除了 3 条写入不能再有任何网络调用。哈希计算在事务外先做（`canonical` 与 `record_hash` 的最后一步拼接在事务内，因为需要 `prev_hash`）。若一段数据需要回源 S28 取 hash 校验，那是另一条独立路径，不能进这个事务。

### 6.4 锁顺序

单事务最多持有一个 `audit_stream` 行锁。涉及多行时（导出、销毁）：

```text
1. audit_export 行（按 export_id 升序）
2. audit_segment 行（按 segment_id 升序）
```

固定升序避免死锁。**任何事务都不允许同时持有两个 `audit_stream` 锁**——跨两个 stream 的"原子写入"是设计错误，说明 `stream_key` 选错了。

### 6.5 隔离级别与热点

`READ COMMITTED`。全部竞争显式落在 `audit_stream` 行锁上；唯一需要额外注意的是 `audit_segment` 的 `EXCLUDE` 约束在并发重封时会串行——这正确且罕见。

热点分离：

- 一个热租户的单个 manifest 高频返工 → 该 `stream_key` 串行。缓解：按 `subject` 分链本身已经把热点切碎；若单个 subject 仍热到 P95 超预算，唯一正确的解法是**上游降频**（不要为每个 UI 事件写审计），不是拆链。
- `audit_access_log` 与 `audit_record` 在不同分区、不同表，读写不互相争。
- 导出读只走只读副本，且 `statement_timeout` 60s。

### 6.6 并发场景清单

| 场景 | 期望 |
|---|---|
| 同一 `stream_key` 100 并发追加 | 全部成功，`stream_seq` 连续无洞，链无分叉 |
| 同一 `command_id` 10 并发 | 1 个执行，9 个 409 `COMMAND_IN_PROGRESS`（或 DONE 后回放） |
| 追加与封印并发 | 追加方要么在封印前成功，要么 409 `STREAM_SEALING` |
| 两个导出作业查同一范围 | 都成功，产生两个 bundle，内容按各自 `query_hash` 独立 |
| 导出与 partition DETACH 并发 | 导出持段行锁；DETACH 等；导出拿到的是已归档数据 |
| 销毁令与 legal_hold 建立并发 | 任一时刻只有一方生效；销毁方必须在同一事务内重查 `legal_hold`，并在 `hold_check` 快照里留证 |

最后一条是 E39 的关键竞态：法律扣留如果在销毁事务的 `COMMIT` 前一刻建立，销毁**不能**继续。做法：`legal_hold` 建立时对该租户的 `destruction_order` 行加 `FOR UPDATE`；销毁执行时先 `FOR UPDATE` 自己的 order 行再查 hold。两侧都锁同一行，顺序天然一致。

---

## 7. 失败分支

### 7.1 摄入失败：谁承担后果

**S45 不是业务路径上的关键依赖，但它承担"证明"的责任。** 后果的分担必须写清楚，否则实现者会把两者混在一起。

| 情况 | S45 行为 | 调用方行为 | 结果 |
|---|---|---|---|
| S45 完全不可达 | Kafka 保留（至少一次，14 天）；重启后消费 | 调用方 outbox 继续重试；**业务已提交** | 审计延迟，不丢（在 14 天内） |
| S45 摄入队列满 | 429 `AUDIT_SINK_BACKPRESSURE` | 调用方 outbox 退避重试；**不删 outbox 行** | 同上 |
| 超过 14 天仍不可达 | 调用方 outbox 行滞留，`oldest_audit_outbox_age` 告警 | 若租户 `strict_audit=true`，S02 策略要求新关键命令不得受理（§7.2） | 降级为"停止新写入"，不是"丢审计" |
| 载荷 schema 非法 | 422，事件进 DLQ | 调用方修 bug；DLQ 重放不重复追加（幂等） | 该条审计缺失，**必须**有人工补齐记录 |
| `source` 与认证身份不符 | 403 `SOURCE_MISMATCH`，进 DLQ | 调用方修 | 防冒名 |
| 事件属于别的 `home_region` | 422 `RESIDENCY_VIOLATION` | 调用方路由错了；修正 producer 的 cell 绑定 | E31 |

**绝不"丢一条继续"。** 但也要承认：**在 14 天窗口之外，丢审计是可能的**。诚实写法：S45 的保证是"在总线保留期内至少一次送达，超出保留期后需人工对账"。写进 SLO 与 runbook，不假装是无限保证。

### 7.2 `strict_audit`：什么时候审计阻塞业务

docs/14 §3 要求"严格客户达到保留缓冲上限时停止关键写"。实现方式必须避免"S45 变成业务关键依赖"：

```text
调用方（如 S26）在受理关键付费命令前：
  1. 读 S02 策略，取 audit.blocking（提案字段，C45-5）
  2. 若 blocking=true 且自己也持有未确认的审计 outbox 行超过阈值
     → 返回 503 AUDIT_UNCONFIRMED，不受理新命令
  3. 否则正常受理；审计异步送达
```

**S45 不主动"通知谁去停"。** 原因：那会让 S45 成为业务控制面，违反 §1.3。判断在调用方，依据在 S02，信号是调用方自己 outbox 的滞留时长——这是它唯一可靠掌握的事实。

`audit_retention_policy.strict_audit` 只是 S45 侧的一个标记，用于：导出必须带完整证明、不允许 `chain_continuous=false`、销毁需双人签署。**它不是阻塞开关。**

### 7.3 崩溃与重启

| 崩溃点 | 后果 | 恢复 |
|---|---|---|
| 追加事务 COMMIT 前 kill | 无记录、无 stream 头前进 | 调用方重试（事件未 ack 则重投；命令幂等回放） |
| 追加 COMMIT 后、Kafka ack 前 | 记录已写、事件重投 | Inbox 去重，第二次静默跳过 |
| 幂等记录 `IN_PROGRESS` 后崩溃 | 僵死记录 | 60s 后 janitor 依 `audit_stream.head_seq` 是否前进判定并重置 |
| 封印事务中崩溃 | `seal_state='SEALING'` 滞留 | janitor 60s 后重算：若记录数与 `to_seq-from_seq+1` 一致则完成封印，否则回退 `OPEN` 并告警 |
| 归档上传后、`state=ARCHIVED` 前崩溃 | 对象在，状态是 SEALED | 重试上传（同 key，幂等覆盖）后置 ARCHIVED |
| `DETACH PARTITION` COMMIT 后、`DROP` 前崩溃 | 孤儿 DETACH 表 | janitor 校验 `destruction_order` 存在后 DROP 并写 meta 记录 |
| 导出 BUILDING 中崩溃 | 停在 BUILDING | 重启扫 `state='BUILDING'` 超 30min 的作业，从分段重做（导出是纯读，天然幂等） |

**"恢复不改历史"**：任何恢复路径都不修改 `audit_record` 的既有行，也不重算既有 `record_hash`。恢复只会推进 `audit_stream.head_*`、`audit_segment.state`、`audit_export.state`。

### 7.4 归档与对象存储故障

| 情况 | 行为 |
|---|---|
| 上传超时 | 指数退避重试（最长 24h），`audit_archive_failures_total` 计数 |
| 上传成功、回读 checksum 不符 | 重传；连续 3 次不符 → `INTEGRITY_FAILED` + P1 |
| 对象被外部删除（WORM 桶未被启用） | 回读失败 → P1；该段禁止被任何后续销毁引用 |
| 桶不可用超过 `retention_hot_days` | **不销毁本地分区**（前置条件 1/2 不满足），`audit_archive_backlog_days` 告警 |
| 区域切换 | 归档目标必须是 tenant `home_region` 内的桶；跨区域归档需 E31 授权并记入审计 |

**"归档失败就不销毁"** 是这里唯一的硬规则。宁可热存储膨胀，不可出现"归档没成功但本地删了"。

### 7.5 时钟

| 情况 | 行为 |
|---|---|
| `occurred_at` 在未来（> 5min） | 接受，`clock_skew_ms < 0`，指标计数，不拒收 |
| `occurred_at` 比旧记录还早（乱序到达） | 接受。链序是 `recorded_at` 顺序，不是 `occurred_at` 顺序 |
| `recorded_at` 本身回跳（NTP 校正） | 极少见；链仍按 `stream_seq` 排，`recorded_at` 只影响分区归属。若跨月跳变，可能落到"过去"的分区——分区按 `recorded_at` 范围，Postgres 会拒绝插入不存在分区的行 → 预建未来 2 个月分区 |

**审计不排序 `occurred_at`。** 取证查询若按 `occurred_at` 排序，必须同时显示 `recorded_at`，并在响应里标注 `ordering: "recorded"`。把两者混为一谈会让"谁先动的手"这个问题得到错误答案。

### 7.6 失败矩阵汇总

| # | 场景 | 检测点 | 行为 | 对外 |
|---|---|---|---|---|
| F45-01 | 总线不可用 | producer 连不上 | 调用方 outbox 积压 | 业务照常 |
| F45-02 | S45 不可用 | 消费 lag | 同上 | 同上 |
| F45-03 | 摄入背压 | 队列深度 | 429 | 调用方退避 |
| F45-04 | 载荷非法 | schema 校验 | DLQ | 人工补齐 |
| F45-05 | 冒名 source | mTLS 身份比对 | 403 + DLQ | 安全事件 |
| F45-06 | 链断裂 | 段校验 | 冻结段 + P1 | 导出标 `chain_continuous=false` |
| F45-07 | 归档失败 | 上传/回读 | 重试，禁销毁 | `archive_backlog` 告警 |
| F45-08 | 导出超时 | statement_timeout | FAILED，不部分 READY | 重试 |
| F45-09 | 销毁被 hold 挡 | 前置条件 3 | BLOCKED | 需法务解除 |
| F45-10 | 分区缺失（时钟跳变） | 插入失败 | 拒绝该条 | 运维建分区 |

---

## 8. 可观测

### 8.1 指标

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `audit_ingest_records_total` | counter | `source_service`, `outcome` | 谁在写、写多少 |
| `audit_ingest_lag_seconds` | histogram | `source_service` | `recorded_at - occurred_at`，发现积压 |
| `audit_ingest_rejected_total` | counter | `reason_code` | 拒收分类，DLQ 前置信号 |
| `audit_append_latency_seconds` | histogram | `stream_kind` | 追加 P95/P99 |
| `audit_append_contention_total` | counter | `stream_kind` | 行锁等待超 10ms 的次数，热点信号 |
| `audit_stream_seal_lag_seconds` | gauge | — | OPEN 段最老时长，超过 2h 告警 |
| `audit_chain_verify_failures_total` | counter | `segment_state` | **P1 唯一来源** |
| `audit_chain_verify_duration_seconds` | histogram | `scope` | 全量校验的成本 |
| `audit_archive_pending_segments` | gauge | `storage_class` | 待归档积压 |
| `audit_archive_failures_total` | counter | `reason` | 归档健康 |
| `audit_archive_backlog_days` | gauge | — | 最老未归档分段的年龄，逼近 `retention_hot_days` 即告警 |
| `audit_export_duration_seconds` | histogram | `record_count_bucket` | 导出耗时 |
| `audit_export_failures_total` | counter | `reason` | |
| `audit_export_chain_discontinuous_total` | counter | — | 导出带缺口，E39 关注 |
| `audit_query_latency_seconds` | histogram | `source`(hot/warm/worm) | 取证查询体验 |
| `audit_query_scan_partitions` | histogram | — | 查询是否退化成全表扫 |
| `audit_sink_backpressure_total` | counter | `tenant_class` | 摄入过载 |
| `audit_access_reads_total` | counter | `access_kind` | 谁在读审计（本身就是敏感指标） |
| `audit_destruction_blocked_total` | counter | `reason` | 销毁因何受阻 |
| `audit_residency_violation_total` | counter | `source_region` | E31 |

**不进 metrics label 的**：`principal_id`、`target_id`、`action` 的自由文本部分、任何理由正文（docs/14 §3 禁高基数）。这些进 trace/log。

### 8.2 追踪

span：`s45.ingest`（consumer，带 `source_service`/`event_id`）→ `s45.append`（DB，带 `stream_key`/`stream_seq`）；导出侧 `s45.export` → `s45.segment.read` ×n → `s45.bundle.write`。trace 维度：`tenant/stream_key/stream_seq/source_service/action/correlation_id/trace_id`。`target_id` 进日志不进 label。

### 8.3 对账作业（每日）

| 检查 | 不通过的含义 | 动作 |
|---|---|---|
| `audit_stream.head_seq == max(audit_record.stream_seq)` 逐 stream | 有记录写了但头没前进，或反之 | 告警 + 不自动修（修会掩盖问题） |
| `audit_record.stream_seq` 在 `[1..head_seq]` 内无洞 | 链有缺口 | P1 |
| `audit_segment` 区间覆盖每个 stream 的 `[1..head]` 且不重叠 | 有记录不属于任何段 | 告警，触发重封 |
| 抽检 1% 分段重算 root hash | 篡改或存储损坏 | P1 |
| 每个 `(source_service, source_verb)` 的 `action` 集合稳定（与基线比对） | 调用方悄悄改了动词 | 告警给对应服务 owner |
| `outbox` 未发布条数与最老滞留 | relay 挂了 | 告警 |
| 每个租户最老未归档分段的年龄 vs `retention_hot_days` | 归档追不上 | 扩容或调策略 |
| `legal_hold` 覆盖的范围内是否有 `destruction_order` 处于 EXECUTING | 竞态突破 | P1 |

### 8.4 告警分级

| 级别 | 条件 | 值班动作 |
|---|---|---|
| P1 | `audit_chain_verify_failures_total > 0`；`legal_hold` 越界销毁 | 立即冻结相关段与销毁作业，启动安全事件流程 |
| P1 | `audit_archive_backlog_days > retention_hot_days` | 扩容/修桶；**人工**临时延长 hot 保留 |
| P2 | 摄入 lag P95 > 5min；`sink_backpressure` 持续 10min | 扩容 consumer |
| P2 | 导出失败率 > 5%（1h） | 查对象存储与只读副本 |
| P3 | 单 stream 争用告警 | 通知上游降频 |

---

## 9. 测试清单

标注 `[E##]` 的对应 docs/16 验收项。全部用 mock 依赖，不接真实模型、不接外部 TSA。

### 9.1 E39 —— 审计、保留、删除、训练数据的证明链（本服务核心）

| # | 用例 | 期望 |
|---|---|---|
| T45-01 `[E39]` | 一次 `manifest.accept` 全链路 | 记录含 actor/action/targets/policy_ref/outcome；可用 `record_id` 独立复算 `record_hash` |
| T45-02 `[E39]` | 同一 stream 连续 1000 条 | `stream_seq` 连续无洞；任取一条，`prev_hash` 等于前一条的 `record_hash` |
| T45-03 `[E39]` | 伪造一条中间记录（改一个字节） | 段校验 `CHAIN_BROKEN`，后续记录全部验不过；不自动修复 |
| T45-04 `[E39]` | 归档后查询（warm） | 经 `audit_segment` 定位，返回记录并带 `verified:true` 与 `segment_root_hash` |
| T45-05 `[E39]` | 归档后全量重算链 | 从 genesis 到 head 无断点；耗时在预算内（只读副本） |
| T45-06 `[E39]` | 已销毁分段的查询 | 返回 `proof_only:true` + `root_hash` + `destroyed_at` + `order_ref`，**不是空数组** |
| T45-07 `[E39]` | 销毁前置条件 3 不满足（有 legal_hold） | `destruction_job.state='BLOCKED'`，分区未 DETACH |
| T45-08 `[E39]` | 归档未完成就请求销毁 | BLOCKED，原因 `ARCHIVE_INCOMPLETE` |
| T45-09 `[E39]` | 销毁执行中途崩溃 | 重启后 `destruction_order` 与 DETACH/DROP 状态一致，无"令已下但数据可查" |
| T45-10 `[E39]` | 销毁后查 `audit_segment` | 行仍在，`root_hash`/`record_count` 保留，`state='DESTROYED'` |
| T45-11 `[E39]` | 导出物完整性 | `bundle.sha256` 可独立验证；`chain-proof.json` 与各段 root hash 一致 |
| T45-12 `[E39]` | 导出范围含已销毁段 | `chain_continuous=false`，`gaps[]` 明确标出该段与原因，**不假装连续** |
| T45-13 `[E39]` | 训练数据授权链（S48 场景） | 训练相关记录含 `rights_ref` 与 `policy_ref`，可回答"这批数据依据哪次授权" |
| T45-14 `[E39]` | 媒体已删除但审计保留 | 审计只有 hash/时间/主体，无媒体正文（与 S28 §4.8 Step 7 对齐） |

### 9.2 追加不可变

| # | 用例 | 期望 |
|---|---|---|
| T45-15 | 直接对 `audit_record` 执行 `UPDATE` | 数据库抛 `AUDIT_RECORD_IMMUTABLE` |
| T45-16 | 直接对 `audit_record` 执行 `DELETE` | 同上 |
| T45-17 | 应用角色尝试 `TRUNCATE` | 权限拒绝 |
| T45-18 | 业务服务 A 的补偿逻辑（模拟 S23 `settle` 回滚） | S45 无任何变化；A 的补偿不跨库 |
| T45-19 | 契约层枚举全部路由 | 不存在任何 `DELETE`/`PATCH /audit-records*` |
| T45-20 | 运维试图改 `record_hash` | 触发器拒绝；即使超级用户改，段校验也会暴露 |

### 9.3 幂等与并发

| # | 用例 | 期望 |
|---|---|---|
| T45-21 `[E12]` | 同 `command_id` 同 body 追加两次 | 第二次回放首次响应，`replayed:true`，只 1 条记录 |
| T45-22 `[E12]` | 同 `command_id` 改一个字段 | 409 `IDEMPOTENCY_KEY_REUSED`，无写入 |
| T45-23 `[E12]` | 同一事件投递 3 次 | Inbox 去重，只 1 条记录 |
| T45-24 | 同一 stream 100 并发追加 | 全部成功，seq 连续，链无分叉 |
| T45-25 | 追加与封印并发 | 追加方 409 `STREAM_SEALING` 或成功；无"半封"状态 |
| T45-26 | 两个导出同范围并发 | 都成功，各自 bundle 独立可验 |
| T45-27 | 销毁与 legal_hold 并发建立 | 只有一方生效；`hold_check` 快照可证明 |

### 9.4 崩溃恢复

| # | 用例 | 期望 |
|---|---|---|
| T45-28 `[E13]` | 追加事务 COMMIT 前 kill | 无记录、head 未动；重试成功 |
| T45-29 `[E13]` | COMMIT 后、Kafka ack 前 kill | 事件重投，Inbox 去重 |
| T45-30 `[E13]` | 封印中 kill | 60s 后 janitor 一致化；不产生重叠段 |
| T45-31 `[E13]` | 归档上传后 kill、状态未置 | 重启重传同 key，最终 ARCHIVED |
| T45-32 | 导出 BUILDING 中 kill | 重启后重做，`record_count` 与 bundle 一致 |
| T45-33 | `IN_PROGRESS` 僵死命令 | 60s 后重置；重试成功 |

### 9.5 安全与租户

| # | 用例 | 期望 |
|---|---|---|
| T45-34 `[E28]` | 租户 B 查租户 A 的 `record_id` / `stream_key` / 导出 | 404，响应不含 A 的任何标识 |
| T45-35 `[E28]` | 跨租户 `legal_hold` / 销毁令 | 404 |
| T45-36 `[E27]` | 撤权后立即查询审计 | 403 `AUDIT_READ_DENIED`，无数据 |
| T45-37 | 事件 `source` 与认证 producer 不符 | 403 `SOURCE_MISMATCH`，进 DLQ |
| T45-38 `[E31]` | 记录目标 cell 与 tenant home_cell 不符 | 422 `RESIDENCY_VIOLATION` |
| T45-39 `[E31]` | 导出下载票跨区域兑换 | 拒绝；票只能在本 region 内使用 |
| T45-40 | 读审计产生 `audit_access_log` | 有；且读 `audit_access_log` **不**再产生记录（递归截断） |

### 9.6 事件与升级（E32 / E01 / E40）

| # | 用例 | 期望 |
|---|---|---|
| T45-41 `[E32]` | Kafka 重放 1 小时 | 不重复追加；不重发 `audit.export.ready` |
| T45-42 `[E32]` | DLQ 重放一条已成功的意图 | 幂等回放，无第二条记录 |
| T45-43 | Outbox relay 崩溃后恢复 | `audit.export.ready` 最终送达，不重复 |
| T45-44 `[E01]` | 只 import 契约 SDK | 构建通过；依赖图无其他服务 domain 包 |
| T45-45 `[E01]` | 回滚到旧版本（schema 只加可选列） | 旧版本可读写；不认识的 `action` 照常记录 |
| T45-46 `[E40]` | 客户验收导出（一段完整生产期） | 链连续、`coverage` 无缺口、可解释每条接受与每笔授权费用 |

---

## 10. 与上下游服务的接口约定

### 10.1 对生产方（所有会写审计的服务）

| 约定 | 内容 |
|---|---|
| **写在自己事务里** | 审计意图进**调用方自己的** outbox，与业务同一本地事务。不走同步 HTTP |
| **必须写拒绝** | `outcome=DENIED`/`ERROR` 一定要写。只记成功等于没有审计 |
| **不写正文** | `details` 只放引用与计数；正文走 `data_ref` |
| **`action` 稳定** | 同一 `source_verb` 的 `action` 不随版本漂移（对账作业会抓） |
| **`occurred_at` 用自己时钟** | 不需要对齐 S45；偏差由 S45 记录 |
| **`intent_id` 全局唯一** | 用于幂等，不能复用 |

### 10.2 对 S01

| 用途 | 接口 | 失败语义 |
|---|---|---|
| 读审计授权 | `GET /authorization-context`，`action='audit.read'`，scope=tenant/stream | 不可达 → 503，fail closed（E27） |
| 导出授权 | `action='audit.export'` | 同上 |
| 销毁授权 | `action='audit.destroy'`，需双人 | 同上 |
| 租户状态 | 随授权上下文返回（suspended/kill switch） | suspended → 拒收写入（§10.6） |

**不缓存完整授权。** 只允许短期、有界、按 `access.grant.changed` 失效的判定缓存，并记录其 `source_revision`（与 S08/S28 同一口径）。

### 10.3 对 S28（删除编排）

S28 §4.8 Step 3 的 `REFERENCE_CHECK` 会向 S45 询问"这个 artifact 是否被审计保留引用"。S45 的回答：

```text
GET /retention-checks?service_id=S28&kind=artifact&id=<artifact_id>
→ { "referenced": true|false,
    "reason": "LEGAL_HOLD"|"RETENTION_ACTIVE"|"PROOF_ONLY"|null,
    "hold_refs": [...],
    "earliest_releasable_at": "..."|null,
    "checked_at": "..." }
```

- `proof_only` 的情形：正文已销毁但证明还在 → `referenced=true`（证明仍在用这个 id）。S28 要能区分"媒体可以删但审计记录还在"，这是 E39 的正常状态。
- **超时 = 未确认 = 阻塞**（与 S28 §4.8 第 1 条硬规则一致）。S45 的 `retention-check` 必须给 deadline 语义，且自身在 200ms 内返回（纯查 `audit_record_target` 的 `(tenant, 'S28', 'artifact', id)`，命中即停，不做全量扫描）。

### 10.4 对 S43 / S26 / S23 / S08 等（消费方）

它们**只在出事时**查 S45：

| 场景 | 查询 | 注意 |
|---|---|---|
| "这次外发是谁批的" | `by-target?service_id=S43&kind=delivery&id=<pkg>` | 返回历史；当前状态仍需问 S43 |
| "这笔超支怎么来的" | `by-target?service_id=S23&kind=budget&id=<budget>` | 审计记录不替代账本；金额以 S23 为准 |
| "为什么这条审核没生效" | `stream_key=tenant:t:subject:manifest:<mf>`，筛 `outcome=DENIED` | S08 的 `REJECTED_STALE` 必须有对应审计 |

**不允许**任何服务把 S45 当作"补偿依据"的查询源（例如"审计说成功所以我也算成功"）。审计记录是**别的服务的报告**，不是事实本身。

### 10.5 对 S47（实时投影）

S47 **不订阅** S45。审计不进入实时面板：它既不适合高频流，也会把敏感操作暴露到 UI 层。S47 的"管理与审计"页签走的是：查询 S45 的导出列表 + 发起导出 + 下载。水位字段显示 `audit_export.completed_at`，**不是**"最新审计时间"（S45 不提供那种实时读数）。

### 10.6 对 S46（外部调用）

S46 的外部命令必须写审计，且 `actor.principal_type='api_client'`，`on_behalf_of` 填租户内被代理的主体（若有）。S45 要求：

- `api_client` 的 `principal_id` 是 S46 的 `APIClient` id，不是外部系统自报的名字；
- `delegation_id` 非空，指向 S46 授予的 scoped grant；
- 记录里**不含** API key、bearer token、签名（S46 负责脱敏，S45 做最后一道 `PAYLOAD_SECRET_SCAN` 拒绝，见 C45-7）。

---

## 11. 契约缺口（如实报告，不自行补）

按 AGENTS.md「契约与文档冲突 → 停下、报告、不自行选边」。本文的写法是**建议值**。

| # | 缺什么 | 影响 | 需要谁改 |
|---|---|---|---|
| **C45-1** | 无 `audit-record.v1.schema.json`。`POST /audit-records` 在 `openapi.v2.yaml` 里**完全没有路径**，`event-topics.v2.json` 只有 `audit.export.ready` | **阻塞实现**：没有契约就无法生成 SDK，也无法做跨服务一致性测试 | `contracts/openapi.v2.yaml` + 新增 schema |
| **C45-2** | 无 `audit-export-bundle.v1` 与 `chain-proof` 的形状定义 | 导出物无法被第三方独立验证，"可校验完整性"（`service.yaml` acceptance）流于口头 | `contracts/` 新增 |
| **C45-3** | `service.yaml` 的 `depends_on: [S01]` 不含 S28，但 `retention-check` 是 S28 删除编排的必需引用方（S28 §4.8、C28-5 已登记"引用方清单需裁决"） | 部署顺序与故障域会漏；S28 删除可能把仍在审计保留的 artifact 判为可删 | catalog |
| **C45-4** | `event-topics.v2.json` 里 S45 只有 `audit.export.ready`；**没有审计意图的入站 topic**（本文提案 `drama.audit.ingress.v2`） | 生产方无法知道往哪发 | `contracts/event-topics.v2.json` |
| **C45-5** | `automation-policy.v2.schema.json` 无 `audit.blocking` 字段 | docs/14 §3 的"严格客户停止关键写"没有策略载体，只能散落在各服务硬编码 | `contracts/automation-policy.v2.schema.json` |
| **C45-6** | 保留期档位（hot/warm/total）在契约里没有承载位置；`service.yaml` 只说"可配置WORM" | E39 的"保留"无法配置化；每个客户合同的差异只能靠工单 | catalog / 新增 `retention-policy.v1` |
| **C45-7** | 无"载密钥/PII 扫描"的共享规则 | 生产方可能把 token 写进 `details`；S45 侧需要一道规则，但规则内容属于安全标准 | `docs/14` 或 `platform/` 规范 |
| **C45-8** | `GET /audit-exports/{id}` 在 `service.yaml` 声明但 openapi 无路径；且未定义下载票（短签名）机制 | 导出物如何安全交付未定 | `contracts/openapi.v2.yaml` |
| **C45-9** | 审计记录与 S28 删除的先后语义：docs/04 §6 说"审计保留最小证明"，但没定义 S45 是否要把 `artifact_id` 当作保留引用参与删除阻塞 | S28 与 S45 可能各自理解不同 | `docs/04` 补一句 + 本文 §10.3 的建议 |
| **C45-10** | 外部时间戳锚定（TSA）是否需要、锚到哪，全无定义 | "不可篡改"在单库层面成立，在被有超级用户权限的内部人篡改层面只靠链自证 | 安全/合规裁决 |

**C45-1 与 C45-4 是阻塞级**：没有契约，S45 无法开工，生产方也无法对接。

---

## 12. 未决

1. **`strict_audit` 到底阻塞什么。** 本文的设计是"调用方依据自己 outbox 的滞留时长 + S02 策略决定是否受理新关键命令"。备选是"S45 主动发布降级事件，网关拒绝新写"。前者不让 S45 变成控制面（符合 §1.3），后者反应更快但把 S45 变成业务关键依赖。需要产品与合规共同签认。

2. **`stream_key` 的粒度是否足够。** 按 `subject` 分链在单 subject 高频时会串行。若实测某集返工循环写审计超过 200 条/分钟，可考虑 `tenant:subject:manifest:<mf>:branch:<br>`。但更可能正确的做法是上游降频（不是每条 UI 事件都写审计）。这需要 S26/S21 侧确认自己的审计粒度。

3. **WORM 桶到底要不要。** `service.yaml` 写"可配置WORM"，但 object lock 的成本与运维复杂度不低，且合规模式（compliance mode）一旦开启连运维都删不掉。是否只对 `worm_required=true` 的租户启用、以及默认值，需要商业判断。

4. **全量链校验多久跑一次。** 每日全量在 1000 租户下可能不可行（O(总记录数)）。本文的默认是"封印时 + 日抽检 1% + 导出时必做 + 按需全量"。这个组合是否满足 E39 的"可证明"，取决于客户合同怎么写。

5. **审计记录的 PII 边界。** `actor.principal_id` 是员工标识，属个人信息（尤其在欧洲）。保留期与删除权（GDPR 第 17 条）与审计保留义务（通常 7 年）冲突。本文按"审计义务优先"处理，但需要法务明确哪些字段属于"最小证明"。这是 C45-7 的上位问题。

6. **`audit_access_log` 的保留期是否与 `audit_record` 一致。** 本文按一致处理（它也是审计事实）。但它的量级可能更大（每次查询一条），成本需实测。

7. **S47 是否需要"审计导出进度"之外的更多审计能力。** 本文假设只有导出。若「管理与审计」页要展示"最近 100 条审计"，那需要一条低延迟的读路径（当前查询走 PG 分区，冷数据不在 PG），会与 `retention_hot_days` 冲突。需要 S47 明确需求。

---

## 13. 明确不做的

- **不做合规判定。** 没有规则引擎、没有风险分、没有违规标记（§1.3）。
- **不做"当前状态"查询。** 任何 `latest_*` 表都不建（§1.2）。
- **不做单条记录的删除或修改。** 契约层、权限层、触发器层三重封死（§1.5）。
- **不做实时审计看板。** 那需要把审计流送进实时系统，成本与敏感度都不可接受（§10.5）。
- **不做跨租户聚合。** 即使"运营方想看全平台审计"，也走**逐租户授权导出**，不建全局索引（E28/E31）。
- **不做自动修复链断裂。** 检测到断链只冻结与升级（§3.5）。
- **不把 Kafka 当审计账本。** 总线 14 天保留，长期事实靠 `audit_record` + 分段归档（docs/04 §4）。
- **不绕过 S23/S22 去"记录"任何费用。** S45 只记"别人报告了一笔授权"，金额与余额永远以 S23 为准。
