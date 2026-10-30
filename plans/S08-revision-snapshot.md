# S08 · 版本快照与接受状态 —— 实现方案

状态：实现方案（W1）。`maturity: design_only` 不变；本文描述"照着能写代码"的形态，不是已实现系统。

上游依据：[docs/01](../docs/01-enterprise-architecture.md)、[docs/04](../docs/04-data-consistency.md)、[docs/06](../docs/06-automation-governance.md)、[docs/11](../docs/11-quality-repair.md)、[S08 规格](../docs/design-package/services/S08-revision-snapshot.md)。

配套：[S22 调度与供应商配额](S22-scheduler-quota.md)、[S23 预算与成本账本](S23-budget-ledger.md)。

---

## 0. 边界与一句话职责

S08 是**当前生产状态**的唯一写者：manifest 版本、accepted pointer、`execution_fence`、`node_epoch`、节点锁、接受提交记录。

S08 **不**拥有：

| 事实 | 归属 |
|---|---|
| 剧本/分镜内容版本 | S04 / S06 |
| 审核决定本身 | S40（S08 只消费并使用它做 CAS） |
| 政策判定 | S02 |
| 金额 | S23 |
| 配额许可 | S22 |
| Run 执行历史 | Temporal；业务投影在 S21 |
| 制品 bytes | S28 |

S08 收到的是**引用**（`service_id/id/revision/content_hash`），从不复制下游内容。

判据：任何"当前这一集这一分支现在是什么"的疑问，答案只能来自 S08；任何"这条内容是什么"的疑问，回到内容所有者。

---

## 1. 数据模型

数据库 `revision_snapshot`，Postgres。金额无关，无浮点。全部表带 `tenant_id`，且**所有查询强制带 `tenant_id` 谓词**（见 §2.6 跨租户返回 404）。

### 1.1 `manifest` —— 每 (tenant, episode, branch) 一行，CAS 的锚点

```sql
CREATE TABLE manifest (
  manifest_id          text PRIMARY KEY,
  tenant_id            text NOT NULL,
  project_id           text NOT NULL,
  series_id            text NOT NULL,
  episode_id           text NOT NULL,
  branch_id            text NOT NULL,
  home_cell            text NOT NULL,
  current_revision     integer NOT NULL CHECK (current_revision >= 1),
  accepted_revision    integer,                       -- 整集接受水位，NULL = 从未接受
  execution_fence      bigint  NOT NULL CHECK (execution_fence >= 1),
  parent_manifest_id   text,                          -- 分支派生
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  lock_version         integer NOT NULL DEFAULT 0,
  CONSTRAINT manifest_identity_uniq
    UNIQUE (tenant_id, series_id, episode_id, branch_id),
  CONSTRAINT manifest_rev_not_ahead
    CHECK (accepted_revision IS NULL OR accepted_revision <= current_revision)
);
CREATE INDEX manifest_project_idx ON manifest (tenant_id, project_id, current_revision);
CREATE INDEX manifest_cell_idx    ON manifest (home_cell, updated_at);
```

`manifest_id` 与 `(tenant_id, series_id, episode_id, branch_id)` 一一对应，这是"按 episode+branch 串行"的物理基础：**锁这一行 = 锁这一集这一分支**（§5.1）。

### 1.2 `manifest_revision` —— 不可变版本

```sql
CREATE TABLE manifest_revision (
  manifest_id          text    NOT NULL REFERENCES manifest(manifest_id),
  revision             integer NOT NULL CHECK (revision >= 1),
  tenant_id            text    NOT NULL,
  content_hash         char(64) NOT NULL,
  parent_revision      integer,
  base_revision        integer NOT NULL,             -- 本次 CAS 的 expected_revision
  execution_fence      bigint  NOT NULL,
  state                text    NOT NULL
    CHECK (state IN ('COMMITTED','ACCEPTED','SUPERSEDED','VOIDED')),
  payload              jsonb   NOT NULL,             -- 完整 episode-manifest v2
  source_revisions     jsonb   NOT NULL,             -- 冗余存储，用于索引查询
  change_plan_ref      jsonb,
  policy_decision_ref  jsonb,
  committed_by         jsonb   NOT NULL,             -- {principal_id, principal_type, delegation_id}
  committed_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (manifest_id, revision),
  CONSTRAINT mr_hash_len CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT mr_parent_lt CHECK (parent_revision IS NULL OR parent_revision < revision)
);
CREATE UNIQUE INDEX mr_hash_uniq ON manifest_revision (tenant_id, manifest_id, content_hash);
CREATE INDEX mr_state_idx ON manifest_revision (manifest_id, state, revision DESC);
```

- `state` **没有**"PREPARED"。候选 manifest 在提交前根本不存在于本库——它是 S07/内容所有者手里的对象。提交即 `COMMITTED`。这直接满足 E13"准备内容可回收"：崩溃在提交前的候选，S08 从未写过它，无需 GC。
- `VOIDED` 仅用于 `execution_fence` 失效后把在途候选标记为作废，不删除行。
- `content_hash` 唯一约束是防重复提交的第二道闸（第一道是幂等记录）。同 hash 不同 revision 的提交是 bug，直接 409 `DUPLICATE_MANIFEST_CONTENT`。

### 1.3 `node_state` —— 节点 epoch 与 accepted pointer

```sql
CREATE TABLE node_state (
  manifest_id           text    NOT NULL REFERENCES manifest(manifest_id),
  node_id               text    NOT NULL,
  tenant_id             text    NOT NULL,
  node_epoch            bigint  NOT NULL CHECK (node_epoch >= 1),
  input_fingerprint     char(64) NOT NULL,
  locked                boolean NOT NULL DEFAULT false,
  lock_reason           text,
  accepted_artifact_ref jsonb,                        -- {service_id,id,revision,content_hash} | NULL
  accepted_at_revision  integer,
  accepted_at           timestamptz,
  updated_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (manifest_id, node_id),
  CONSTRAINT ns_accept_needs_rev
    CHECK ((accepted_artifact_ref IS NULL) = (accepted_at_revision IS NULL))
);
CREATE INDEX ns_accepted_idx ON node_state (tenant_id, manifest_id, accepted_at_revision);
```

`input_fingerprint` = sha256 对"该节点全部输入引用"做 JCS 规范化后的摘要。它是 `node_epoch` 的唯一推进依据（§3.2）。

### 1.4 `acceptance_commit` —— 接受提交审计

```sql
CREATE TABLE acceptance_commit (
  commit_id            text PRIMARY KEY,
  tenant_id            text NOT NULL,
  manifest_id          text NOT NULL,
  revision             integer NOT NULL,
  node_ids             text[] NOT NULL,              -- 空数组 = 整集接受
  expected_revision    integer NOT NULL,
  expected_fence       bigint NOT NULL,
  decision_ref         jsonb NOT NULL,               -- S40 或 S02 的决定引用
  policy_ref           jsonb NOT NULL,
  qc_report_refs       jsonb NOT NULL DEFAULT '[]',
  evidence_refs        jsonb NOT NULL DEFAULT '[]',
  actor                jsonb NOT NULL,
  result               text NOT NULL
    CHECK (result IN ('ACCEPTED','REJECTED_STALE','REJECTED_EVIDENCE','REJECTED_POLICY','REJECTED_LOCKED')),
  reject_detail        jsonb,
  created_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ac_rev_fk FOREIGN KEY (manifest_id, revision)
    REFERENCES manifest_revision (manifest_id, revision)
);
CREATE INDEX ac_manifest_idx ON acceptance_commit (tenant_id, manifest_id, created_at DESC);
CREATE INDEX ac_reject_idx ON acceptance_commit (tenant_id, result, created_at)
  WHERE result <> 'ACCEPTED';
```

`REJECTED_*` 行**必须写入**。E07"旧审核晚到"要求可追溯，不能静默丢弃一个晚到的接受决定——它是审计证据，也是排查"为什么这条审核没生效"的唯一材料。

### 1.5 `branch_state` —— 分支级 fence 与写主租约

```sql
CREATE TABLE branch_state (
  tenant_id        text NOT NULL,
  series_id        text NOT NULL,
  branch_id        text NOT NULL,
  execution_fence  bigint NOT NULL CHECK (execution_fence >= 1),
  fence_reason     text,            -- RUN_CANCELED / RUN_FAILOVER / BRANCH_ABANDONED / MANUAL
  fence_ref        text,            -- run_id 或 operator command_id
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, series_id, branch_id)
);

-- 部署级写主栅栏，与业务 execution_fence 无关，勿混用
CREATE TABLE writer_lease (
  scope_key     text PRIMARY KEY,        -- 'manifest:<id>' 或 'branch:<tenant>/<series>/<branch>'
  leader_epoch  bigint NOT NULL,
  holder        text NOT NULL,           -- pod / instance id
  expires_at    timestamptz NOT NULL
);
```

两个 epoch 的区别再强调一次：

| | 作用域 | 谁递增 | 旧值的效果 |
|---|---|---|---|
| `execution_fence` | run / branch | S08 收到 S21 的 `:fence` 命令 | 该 fence 下的一切候选提交、接受、QC 结果全部 `STALE` |
| `node_epoch` | 单个 `logical_node` | S08 在 `input_fingerprint` 变化时自行 +1 | 仅该节点的在途结果失效 |
| `leader_epoch` | 部署单元 | 选主时 +1 | 旧实例的一切写入被数据库拒绝 |

### 1.6 幂等、Outbox、Inbox

```sql
CREATE TABLE idempotency_record (
  tenant_id      text NOT NULL,
  command_id     text NOT NULL,
  request_hash   char(64) NOT NULL,
  command_type   text NOT NULL,          -- 'manifest.commit' / 'manifest.accept' / 'branch.fence'
  state          text NOT NULL CHECK (state IN ('IN_PROGRESS','DONE')),
  response_code  integer,
  response_body  jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL DEFAULT now() + interval '30 days',
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE outbox (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id         text NOT NULL,
  aggregate_id      text NOT NULL,       -- manifest_id
  aggregate_sequence bigint NOT NULL,    -- = manifest revision
  event_type        text NOT NULL,
  partition_key     text NOT NULL,       -- tenant_id || ':' || aggregate_id
  payload           jsonb NOT NULL,
  headers           jsonb NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  published_at      timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;

CREATE TABLE inbox (
  consumer   text NOT NULL,
  event_id   text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
CREATE INDEX inbox_ttl_idx ON inbox (received_at);
```

`(aggregate_id, aggregate_sequence, event_type)` 唯一 —— 一次提交只可能产生一条 `snapshot.manifest.committed`，重复写 Outbox 会被约束挡住。

### 1.7 不建的表

- **不建 `manifest_projection`**。S08 自己就是权威，投影只在下游建。
- **不缓存 S02 政策全文**。只缓存 `policy_version → {valid, expires_at}`，政策本体回源。

---

## 2. 命令与查询 API

传输：内部 mTLS + workload identity，HTTP/JSON。所有写命令必须带：

```
Idempotency-Key: <command_id>
X-Tenant-Id: <tenant_id>        # 必须与认证上下文一致，否则 403
traceparent: 00-<trace_id>-<span>-01
```

`tenant_id` 从客户端证书/服务身份派生；header 只做一致性校验，**不能自授身份**。

### 2.1 幂等语义（全服务统一，S22/S23 同此定义）

```
request_hash = sha256( JCS( body 去掉 command_id 与 request_hash 两个键 ) )
```

JCS = RFC 8785 JSON Canonicalization Scheme。落库时 `(tenant_id, command_id)` 为主键：

| 已有记录 | 本次请求 | 结果 |
|---|---|---|
| 无 | — | 正常执行，先插 `IN_PROGRESS` |
| `IN_PROGRESS` | 同 hash | 409 `COMMAND_IN_PROGRESS`，`retryable: true`，`Retry-After: 1` |
| `DONE` | 同 hash | 原样返回首次的 `response_code` + `response_body`，不再执行 |
| 任意 | 不同 hash | 409 `IDEMPOTENCY_KEY_REUSED`，`retryable: false` |

"同键不同 body 拒绝，不是覆盖"——这条在 §7 E12 有专门用例。响应体里回显 `replayed: true`，便于排查。

### 2.2 `POST /manifests/{manifest_id}:commit`

提交一个新生产版本（**不**改变 accepted pointer）。

```jsonc
{
  "command_id": "cmd_01H...",
  "expected_revision": 12,
  "candidate_manifest": { /* episode-manifest.v2 完整对象 */ },
  "policy_decision_id": "pd_...",
  "change_plan_ref":  { "service_id": "S07", "id": "...", "revision": 3, "content_hash": "…" },
  "qc_report_refs":   [ { "service_id": "S35", "id": "...", "revision": 1, "content_hash": "…" } ],
  "review_decision_ref": null,
  "override_reason": null
}
```

`candidate_manifest.node_states[].node_epoch` 是**客户端读到的值**，只用于乐观校验；S08 提交后按 §3.2 自行决定新值。**客户端必须采用响应返回的 manifest，不能沿用自己提交的那份。**

响应 `200`：

```jsonc
{
  "manifest": { /* 权威 episode-manifest.v2 */ },
  "previous_revision": 12,
  "rebased": false,
  "changed_node_epochs": [
    { "node_id": "shot_3_2.video", "from": 4, "to": 5 },
    { "node_id": "shot_3_4.video", "from": 2, "to": 2 }
  ],
  "outbox_event_id": "evt_..."
}
```

`202` 仅用于超长校验（>`x-commit-inline-budget`=800ms）转为异步 operation，返回 `operation_id`；异步落地后再发事件。默认走同步 200。

### 2.3 `POST /manifests/{manifest_id}:accept`

按节点或整集更新 accepted pointer。

```jsonc
{
  "command_id": "cmd_02H...",
  "expected_revision": 13,
  "expected_fence": 2,
  "node_ids": ["shot_3_2.video"],          // [] = 整集接受
  "decision_ref":  { "service_id": "S40", "id": "...", "revision": 2, "content_hash": "…" },
  "policy_ref":    { "service_id": "S02", "id": "...", "revision": 7, "content_hash": "…" },
  "qc_report_refs": [ /* 必须覆盖被接受节点的 artifact hash */ ],
  "actor": { "principal_id": "...", "principal_type": "human", "delegation_id": "..." }
}
```

响应 `200`：`{ "accepted_revision": 13, "nodes": [{node_id, accepted_artifact_ref, accepted_at_revision}], "acceptance_commit_id": "ac_..." }`。

### 2.4 `POST /branches/{series_id}/{branch_id}:fence`

S21 取消 run 或故障切换时调用，递增 `execution_fence` 并把该 fence 下所有未接受 revision 置 `VOIDED`。

```jsonc
{ "command_id": "cmd_03H...", "expected_fence": 2, "reason": "RUN_CANCELED", "fence_ref": "run_..." }
```

响应 `{ "execution_fence": 3, "voided_revisions": [12, 13] }`。**注意 `expected_fence` 不匹配时返回 409 `FENCE_CONFLICT`**，同时把当前 fence 带回去——重复取消是幂等的（同 command_id），但两个 run 同时取消不同 fence 时不能互相覆盖。

### 2.5 查询

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/manifests/{manifest_id}` | 当前头：revision / accepted_revision / fence |
| GET | `/manifests/{manifest_id}/revisions/{revision}` | 单个版本全文 |
| GET | `/manifests/{manifest_id}/accepted-pointers` | 全部节点的 accepted pointer |
| GET | `/manifests/{manifest_id}/node-diff?from_revision=&to_revision=&node_id=` | 节点级变更集（见 §2.5.1） |
| GET | `/manifests/{manifest_id}/revisions?state=&after_revision=&limit=` | 列表 |
| GET | `/episodes/{episode_id}/manifests?branch_id=` | 一集所有分支头 |
| GET | `/acceptance-commits?manifest_id=&result=` | 审计查询（含被拒） |

全查询响应带 `source_revision`（= 读到的 `current_revision`）与 `read_at`；S08 无投影，故无 `projection_watermark`。

#### 2.5.1 节点级差异 `GET /manifests/{manifest_id}/node-diff`

**为什么需要这条：** S40 判定审核是否 stale、S26 判定在途任务是否还值得跑，都需要回答"revision 前进了，但**我这个节点**的输入变了没有"。没有这个能力，所有下游只能退化成"revision 一变就 stale"——更严，但会产生大量无谓重审，且会把"别人改了无关镜头"错算成本节点的作废。docs/04「不能单凭不是最新 manifest 丢掉所有无关任务」正是指这件事。

**不需要新增存储。** 比较依据全在 `manifest_revision.payload->'node_states'` 里：`input_fingerprint` 与 `accepted_artifact_ref` 都是 `episode-manifest.v2` 的 required 字段，每个 revision 的 `payload` 都是完整对象且不可变。`node_state` 表只是"当前 head 的物化视图"，diff 读的是历史 revision，不回表。

请求：`from_revision`（必需）、`to_revision`（缺省 = `current_revision`）、`node_id`（可选，缺省 = 全部节点）。

```
-- 单条只读查询，不加锁、不进事务（读到的是快照）
WITH a AS (SELECT payload FROM manifest_revision
           WHERE tenant_id=$t AND manifest_id=$m AND revision=$from),
     b AS (SELECT payload FROM manifest_revision
           WHERE tenant_id=$t AND manifest_id=$m AND revision=$to)
SELECT ... FROM a, b
```

响应 `200`：

```jsonc
{
  "manifest_id": "mf_...",
  "from_revision": 12,
  "to_revision": 13,
  "source_revision": 13,          // 读时的 current_revision
  "read_at": "2026-10-30T…",
  "nodes": [
    {
      "node_id": "shot_3_2.video",
      "change": "INPUT_CHANGED",     // UNCHANGED | INPUT_CHANGED | ACCEPTED_MOVED
                                     // | INPUT_AND_ACCEPTED_CHANGED | ADDED | REMOVED
      "from_input_fingerprint": "ab…",
      "to_input_fingerprint":   "cd…",
      "from_accepted_artifact_ref": { … } ,
      "to_accepted_artifact_ref":   { … },
      "from_locked": false,
      "to_locked": true
    }
  ],
  "summary": { "unchanged": 41, "input_changed": 2, "accepted_moved": 0,
               "input_and_accepted_changed": 1, "added": 0, "removed": 0 }
}
```

`change` 的判定（按此顺序，先命中先定）：

1. 只在 `from` 有 → `REMOVED`；只在 `to` 有 → `ADDED`
2. `input_fingerprint` 不同且 `accepted_artifact_ref` 不同 → `INPUT_AND_ACCEPTED_CHANGED`
3. `input_fingerprint` 不同 → `INPUT_CHANGED`
4. `accepted_artifact_ref` 不同 → `ACCEPTED_MOVED`
5. 其余 → `UNCHANGED`（`locked` 翻转不影响 `change`，单独用 `from_locked`/`to_locked` 表达）

**语义边界（必须写清楚，否则会被误用）：**

- 这条接口**只说"变了没有"，不说"该不该重做"**。判定在途任务是否作废的是 S26/S21，S08 只提供事实。S08 一旦开始解释"这个变化意味着要重跑"，就成了第二个影响分析权威。
- `change=UNCHANGED` **不等于**"该节点可以沿用旧审核"。审核还有效的前提是 `execution_fence`、`node_epoch`、`decision_ref` 里的证据都对得上——那套判定在 `:accept` 里（§3.3），不在这里。
- `from_revision` / `to_revision` 都必须 `<= current_revision` 且属于同一 `manifest_id`；否则 404 `REVISION_NOT_FOUND`（与跨租户同码，不区分）。
- 单次返回上限 2000 个节点；超过则 413 `NODE_DIFF_TOO_LARGE`，要求带 `node_id` 或缩小范围。**不做分页**——分页会让调用方拿到一个可能不自洽的变更集。

这条接口是**只读、无副作用、可缓存**的：`(manifest_id, from, to)` 确定后结果永不变（两个 revision 都不可变），响应带 `ETag = sha256(from_to_payloads)`，调用方可长期缓存。

### 2.6 错误码

错误体固定为 openapi 的 `Error`：`{code, message, retryable, correlation_id, details}`。

| HTTP | code | retryable | 场景 |
|---|---|---|---|
| 400 | `MALFORMED_REQUEST` | false | JCS 失败、schema 不合法 |
| 401 | `UNAUTHENTICATED` | false | 身份缺失 |
| 403 | `TENANT_SCOPE_DENIED` | false | header tenant 与认证不一致 |
| 404 | `NOT_FOUND` | false | **含跨租户**：别的租户的 manifest 一律 404，不泄漏存在性（E28） |
| 409 | `VERSION_CONFLICT` | true | `expected_revision` 不匹配且无法 rebase；`details.head_revision` |
| 409 | `STALE_EXECUTION_FENCE` | false | 候选 fence 落后；`details.current_fence` |
| 409 | `STALE_NODE_EPOCH` | false | 候选节点 epoch 落后；`details.nodes[]` |
| 409 | `STALE_DECISION` | false | 审核/QC 引用的 revision 或 fence 已过期（E07） |
| 409 | `FENCE_CONFLICT` | true | `:fence` 的 expected 不匹配 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | false | 同键不同 body |
| 409 | `COMMAND_IN_PROGRESS` | true | 同键同 body 正在执行 |
| 409 | `DUPLICATE_MANIFEST_CONTENT` | false | 同 content_hash 已在别的 revision |
| 409 | `ILLEGAL_STATE_TRANSITION` | false | 对 `VOIDED`/`SUPERSEDED` 版本做接受 |
| 422 | `SCHEMA_INVALID` | false | 候选不符合 episode-manifest v2 |
| 422 | `SOURCE_REVISION_STALE` | false | `source_revisions` 里有已不存在的引用 |
| 422 | `POLICY_DENIED` | false | S02 判定不放行 |
| 422 | `QC_EVIDENCE_MISMATCH` | false | QC 报告的 artifact hash ≠ 候选的 |
| 422 | `NODE_LOCKED` | false | 节点被人工锁定且未带 `override_reason` |
| 422 | `NODE_EPOCH_SKIP` | false | 候选 epoch > 当前 +1 |
| 422 | `MEMORY_CHECKPOINT_BEHIND` | false | `memory_checkpoint.commit_sequence` 低于该集要求 |
| 429 | `WRITE_BACKPRESSURE` | true | 该 manifest 的提交队列超限 |
| 503 | `DEPENDENCY_UNAVAILABLE` | true | S02/S03 不可用且超出内联预算 |

**409 与 422 的界线**：409 = 状态/版本冲突，客户端拿最新状态重试可能成功；422 = 这个请求本身不可行，同样的 body 重试也不会成功。

---

## 3. 状态机

### 3.1 manifest revision

```
                      ┌─────────────┐
     commit ────────► │  COMMITTED  │
                      └──────┬──────┘
                             │ 被更高 revision 取代
                             ▼
                      ┌─────────────┐
                      │ SUPERSEDED  │
                      └─────────────┘

     commit ────────► COMMITTED ──accept 全部必需节点──► ACCEPTED
     COMMITTED/ACCEPTED ──fence 失效──► VOIDED
```

| 从 | 到 | 触发 |
|---|---|---|
| （无） | COMMITTED | `:commit` |
| COMMITTED | SUPERSEDED | 提交了 revision+1 |
| COMMITTED | ACCEPTED | `:accept` 且该 revision 的所有节点都已接受 |
| COMMITTED / ACCEPTED | VOIDED | `:fence` 且该 revision 的 `execution_fence < 新 fence` |

非法转移一律 409 `ILLEGAL_STATE_TRANSITION`：

- `SUPERSEDED → ACCEPTED`：**旧版本永远不能变成接受态**。晚到的审核想接受旧 revision 时，走的不是这个转移，而是 §3.3 的 rebase-or-reject。
- `VOIDED → *`：作废是终态。
- `ACCEPTED → COMMITTED`：不存在——接受不回退。要回退就提交一个新 revision 并接受它（"回滚生成新 manifest 而非改历史"）。

### 3.2 `node_epoch` 推进规则

只有一条规则，写在提交事务内：

```
for each node in candidate.node_states:
    stored = node_state[node_id]
    if node.input_fingerprint != stored.input_fingerprint:
        new_epoch = stored.node_epoch + 1
    else:
        new_epoch = stored.node_epoch
```

- 客户端提交的 `node_epoch` 只做比较：`candidate.node_epoch > stored.node_epoch` → 422 `NODE_EPOCH_SKIP`（客户端臆造了 epoch）；`<` → 409 `STALE_NODE_EPOCH`。
- 因此**改一个镜头只推进那一个节点的 epoch**，其余节点 epoch 不动，在途任务继续有效。这直接实现 docs/04 "改一个镜头只提升受影响节点的 node_epoch"。
- 提交事件里务必发 `changed_node_epochs`（含 `from`/`to`），S21 据此只取消真正的过期任务。

### 3.3 accepted pointer CAS（核心并发路径）

`:accept` 的事务逻辑：

```
BEGIN;
-- 0) 幂等
-- 1) 写主租约
SELECT leader_epoch FROM writer_lease WHERE scope_key = 'manifest:'||$id FOR SHARE;
-- 不等于请求携带的 leader_epoch → 503 WRITER_EPOCH_STALE（旧写主）
-- 2) 串行点
SELECT current_revision, accepted_revision, execution_fence
  FROM manifest WHERE manifest_id=$id AND tenant_id=$t FOR UPDATE;
-- 3) 版本闸
IF req.expected_fence <> manifest.execution_fence THEN 409 STALE_EXECUTION_FENCE
IF req.expected_revision <> manifest.current_revision THEN
      -- 见下方 "晚到的接受"
-- 4) 证据闸
载入 decision_ref / policy_ref / qc_report_refs 的不可变内容（本地证据表或回源 S40/S02/S35）
IF decision.context.manifest_ref.revision <> req.expected_revision
   OR decision.context.execution_fence <> req.expected_fence THEN 409 STALE_DECISION
IF decision.context.node_epoch <> node_state[node].node_epoch THEN 409 STALE_DECISION
IF qc.artifact_hash <> candidate.node_states[node].accepted_artifact_ref.content_hash THEN 422 QC_EVIDENCE_MISMATCH
IF policy 判定不放行 THEN 422 POLICY_DENIED
IF node_state[node].locked AND req.override_reason IS NULL THEN 422 NODE_LOCKED
-- 5) 写
UPDATE node_state SET accepted_artifact_ref=..., accepted_at_revision=manifest.current_revision,
       accepted_at=now() WHERE manifest_id=$id AND node_id = ANY($nodes);
UPDATE manifest SET accepted_revision = CASE WHEN 全部节点已接受 THEN current_revision ELSE accepted_revision END;
INSERT INTO acceptance_commit(... result='ACCEPTED' ...);
-- 6) outbox：snapshot.manifest.committed 的 data 里带 accepted_pointers_delta
COMMIT;
```

**晚到的接受（`expected_revision` 落后）**——这是 E07 的正题，处理成"要么 rebase，要么明确拒绝"，不能含糊：

```
IF 仅 req.expected_revision 落后，但：
   - decision.context.execution_fence == manifest.execution_fence
   - 对该节点：decision.node_epoch == node_state.node_epoch
   - 该节点在新 revision 里的 input_fingerprint 与 decision 时相同
   - 该节点的 accepted_artifact_ref 在新 revision 里仍是 decision 指向的 artifact
THEN
   允许一次 rebase：把 accepted pointer 写在新 head 上，
   acceptance_commit.result='ACCEPTED' 并记 rebased_from_revision
   返回 200 + rebased: true
ELSE
   INSERT acceptance_commit(result='REJECTED_STALE', reject_detail={...})
   ROLLBACK 只剩这条审计记录（用独立事务写）
   409 STALE_DECISION
```

两条硬线：

1. **旧 epoch 的结果永远不能推进当前接受指针**。`execution_fence` 或 `node_epoch` 对不上，没有 rebase 余地。
2. **别人改了无关节点，不应让本节点的审核作废**。这正是 docs/04 "不能单凭不是最新 manifest 丢掉所有无关任务"的落地。

### 3.4 回滚

没有 `:rollback` 命令。回滚 = `:commit` 一个新 revision，其 `parent_manifest_ref` 指向要回退到的旧 revision，内容等于旧 revision。原因：历史不可改写，事件序列连续，`aggregate_sequence` 单调。

### 3.5 节点锁

`node_state.locked` 由 `POST /manifests/{id}/nodes/{node_id}:lock` 设置，权限在 S02。语义：

- 自动生成可以继续产出候选，但**自动接受**跳过该节点。
- 人工接受必须带 `override_reason`，且 `acceptance_commit.actor` 有对应角色。
- 解锁同样走 S02 授权。

---

## 4. Saga 与补偿

S08 在两条 Saga 里的位置不同：**修改接受 Saga** 里它是提交点；**付费准入 Saga** 里它不是参与者，只是被读的上下文。

### 4.1 修改接受 Saga（S07 → S09 → S02 → 内容所有者 → S08 → S21）

| 步 | 动作 | S08 侧结果 | 失败补偿 |
|---|---|---|---|
| 1 | S07 生成 ChangePlan（`base_manifest`、source hashes、impact watermark、policy version、expiry） | 无 | — |
| 2 | S09 计算影响闭包 | 无 | — |
| 3 | S02 判定 | 无 | — |
| 4 | 内容所有者准备新 revision（**未接受**） | 无 | 产物无人引用，保留期后由所有者 GC |
| 5 | **S08 `:commit`** | 新 revision + Outbox，同事务 | 见 §4.2 |
| 6 | S21 消费 `snapshot.manifest.committed` 调度子图 | 无 | S21 可重放该事件重建 |
| 7 | 各 Stage 产出 → S35–S37 QC → S40 决定 → S02 → **S08 `:accept`** | accepted pointer | 见 §3.3 |

注意步 5 失败时**不回滚已提交的事实为"原地旧值"**：候选产物保持 unreferenced，不写任何补偿记录到历史里。S08 的原则是"只追加，不改写"。

### 4.2 S08 提交自身的失败分支（E13）

| 崩溃点 | 结果 | 恢复 |
|---|---|---|
| 进入事务前 | 无任何写入 | 客户端同 command_id 重试 |
| 幂等记录 `IN_PROGRESS` 后 | 有僵死记录 | 重试返回 409 `COMMAND_IN_PROGRESS`；`IN_PROGRESS` 超过 `command_stale_ttl`(60s) 后由 janitor 依据 `outbox` 是否已写来判定并重置 |
| manifest_revision 已写、commit 未提交 | 事务回滚，无痕迹 | 同上 |
| 已 COMMIT、Outbox 未发 | 本地事实在，事件未出 | Outbox relay 扫描 `published_at IS NULL` 补发；顺序由 `aggregate_sequence` 保证 |
| 已 COMMIT、事件已发、响应丢失 | 事实全在 | 客户端重试同 command_id → 幂等记录 `DONE` 返回首次响应 |
| 客户端拿到 200 但内容与自己的候选不同 | 正常（epoch 由 S08 决定） | 客户端以响应为准 |

**"恢复无假接受"**：任何恢复路径都不会写 `accepted_revision`，只有 `:accept` 会。这是把"提交"与"接受"拆成两个命令的主要动因。

### 4.3 与付费准入的关系（不拥有，但必须正确）

S26 在准入时读取 `execution_context`，其中 `manifest_ref` 与 `node_epoch`、`execution_fence` 来自 S08。S08 的责任边界：

- 对外提供 `GET /execution-contexts/{run_id}` 的权威片段？**不**。上下文由 S21/S26 组装，S08 只提供 manifest 与节点 epoch 的读接口。
- 当 `execution_fence` 递增，S08 **必须**在同一事务里把该 fence 下未接受的 revision 置 `VOIDED` 并发布事件。S22/S23 不需要读它，但 S26 必须据此把自己的 attempt 标 `ABANDONED`（费用与 permit 的对账由 S26/S22/S23 完成）。
- S08 不记录"这个 attempt 花了多少钱"，也不持有 `SUBMISSION_UNKNOWN`。**S08 不对钱做任何判断。**

### 4.4 `SUBMISSION_UNKNOWN` / `SUBSESSION_UNKNOWN` 下 S08 该做什么

一句话：**什么都不做，且不能做错**。

- 处于未知态的 attempt 不产生新的 manifest revision，也不会被接受。S08 收不到这类请求（S26 不会发）。
- 若未知态最终判定"供应商确实已生成"，产物经 S28 归档后，S08 `:commit` 一个新 revision 引用它——**新 attempt 的 normal 路径，不是特殊路径**。
- 若判定"从未提交"，没有任何 manifest 变化；`node_epoch` 不动，因此不会误伤别的节点。
- 若该 attempt 的 `node_epoch` 在未知期间被别的提交推进了，那这次产物回来后是 `STALE_NODE_EPOCH`，只能作为候选留存，不能接受。这是正确的：节点输入已经变了。

禁止行为：S08 不得为"未知任务"预留 accepted pointer，不得把未知态写进 manifest。

---

## 5. 并发与锁

### 5.1 按 episode+branch 串行 CAS

三层，从粗到细：

1. **部署层**：`writer_lease` 保证同一 `scope_key` 只有一个写主持有未过期的 `leader_epoch`。每个写事务开头 `FOR SHARE` 读一次并比对；旧写主（分区切换后）的所有写被拒。这解决"两个 cell 都以为自己是主"。
2. **聚合层**：`SELECT ... FROM manifest WHERE manifest_id=$id FOR UPDATE`。因为 `manifest_id` 与 `(tenant, series, episode, branch)` 一一对应，**这一行锁就是 episode+branch 锁**。同一集的并发提交在这里排队，跨集完全并行。
3. **行版本层**：`expected_revision` 与 `current_revision` 比较，附带 `lock_version` 递增，供乐观读方检测。

为什么不用 advisory lock：行锁已经在同一行上，再加 advisory 只是重复；而且 advisory lock 不参与 MVCC，回滚语义更难推理。只有在需要"跨多个 manifest 的原子操作"（例如整分支操作）时才额外用 `pg_advisory_xact_lock(hashtextextended($branch_key, 0))`，目前只有 `:fence` 分支级操作需要，且它同时锁 `branch_state` 行即可。

### 5.2 锁顺序

单个事务最多持有一个 manifest 行锁。` :fence` 触及一个分支下多个 manifest 时：

```
1. branch_state 行锁
2. manifest 行锁（按 manifest_id 升序，逐个 FOR UPDATE）
```

固定升序避免死锁。分支级操作频率极低（取消/故障切换），可接受串行。

### 5.3 隔离级别

`READ COMMITTED`。理由是全部竞争都显式落在行锁上，不需要 `SERIALIZABLE` 的额外重试成本。唯一例外：`acceptance_commit` 的"全部节点已接受 → 置 ACCEPTED"判断必须在持有 manifest 行锁时算，否则两笔并发接受各自以为"对方还没接受"。

### 5.4 热点与扩容

- 热键是单一 manifest；同一集的高频提交（自动返工循环）会串行。缓解：S08 水平分片按 `project_id`，同一 project 的一切落在同一分片（`manifest` 的 `project_id` 索引 + 分片路由表）。
- 大租户可把不同 series 分配到不同 cell，但**同一 series 的记忆权威仍单 home-cell**；S08 的 manifest 分片键与之一致。
- 写入 P95 目标 1s 是规划值，未验证；不要据此承诺。

---

## 6. 事件

### 6.1 发布：`snapshot.manifest.committed`

Topic `drama.revision-snapshot.v2`，partition key `tenant_id:aggregate_id`，`aggregate_id = manifest_id`，`aggregate_sequence = revision`。envelope 严格符合 `event-envelope.v2.schema.json`。

```jsonc
{
  "specversion": "1.0", "schema_version": "2.0",
  "id": "evt_...", "source": "S08", "type": "snapshot.manifest.committed",
  "subject": "manifest/manifest_ep3",
  "time": "2026-10-30T08:00:00Z",
  "tenant_id": "tenant_demo", "home_cell": "cell_a1",
  "aggregate_id": "manifest_ep3", "aggregate_sequence": 13,
  "correlation_id": "cmd_...", "causation_id": "plan_...",
  "traceparent": "00-...-...-01",
  "data": {
    "manifest_ref": { "id": "manifest_ep3", "revision": 13, "content_hash": "…" },
    "parent_manifest_ref": { "id": "manifest_ep3", "revision": 12, "content_hash": "…" },
    "execution_fence": 2,
    "changed_node_epochs": [ { "node_id": "shot_3_2.video", "from": 4, "to": 5 } ],
    "accepted_pointers_delta": [],
    "accepted_revision": 12,
    "source_revisions": [ /* 有变化的引用 */ ],
    "trigger": "COMMIT"        // COMMIT | ACCEPT | FENCE_VOID
  },
  "data_ref": null
}
```

`trigger = ACCEPT` 时 `accepted_pointers_delta` 非空、`accepted_revision` 可能变化。S21 据此区分"要跑子图"与"只是指针变了"。

`data_ref` 用 `null`：manifest 体量受控（引用而非内容），不需外置。若某集节点数超过 2000，改发 `data_ref` 指向 S08 的查询接口，`data` 只留摘要。

### 6.2 消费

| 事件 | 来源 | S08 的处理 | 失败策略 |
|---|---|---|---|
| `review.decision.recorded` | S40 | 只落 `review_evidence`（不可变），**不**自动接受。等待 S02 判定后由显式 `:accept` 触发 | 重试；乱序则按 `decision.manifest_ref.revision` 缓冲 |
| `quality.technical.completed` / `semantic` / `alignment` | S35/S36/S37 | 落 `qc_evidence`（artifact_hash 为键） | 同上 |
| `policy.revision.published` | S02 | 失效本地政策缓存；已在使用旧版本的接受提交保持有效（证据固定） | 重试；政策缓存 miss 时回源 |
| `memory.canon.committed` | S16 | 更新 `series` 级的 `max_checkpoint_sequence` 投影，用于 `MEMORY_CHECKPOINT_BEHIND` 校验 | 缺口缓冲 + 回源 S16 |
| `access.grant.changed` | S01 | 撤销的 principal 立即失去写权限；不影响已提交事实 | 重试 |
| `project.episode.created` | S03 | 建 manifest 壳（`current_revision=1` 的初始版本） | 重试；重复事件幂等 |

**S08 不消费** `generation.job.state-changed`。生成任务状态与生产状态是两回事，混在一起就会出现"生成成功即被接受"的假接受。

### 6.3 Inbox 与乱序

```python
# 与业务写入同一个事务
cur = tx.execute("""
  INSERT INTO inbox(consumer, event_id) VALUES ('s08.evidence', %s)
  ON CONFLICT DO NOTHING RETURNING event_id
""", [event_id])
if cur.rowcount == 0:
    return  # 重复投递，已处理
# ...业务写入...
tx.commit()
```

- `consumer` 名字按用途分开：`s08.evidence`、`s08.policy`、`s08.memory`。不能用同一个，否则重放 `s08.memory` 会误挡 `s08.evidence`。
- **sequence 缺口**：同一 `aggregate_id` 上的 `aggregate_sequence` 不连续时，缓冲最多 `gap_timeout`(30s)，超时后回源查询该聚合的当前状态；**不无限等待**。证据类事件（QC/审核）本身不带聚合序列，靠 `(artifact_hash, revision)` 去重。
- **终态不倒退**：`qc_evidence` 只增不改；`review_evidence` 只增不改；晚到的事件永远只是"多一条证据"，不能反转已接受指针。真正的指针变化只能由 `:accept` 命令产生——**事件永远不能直接推动 accepted pointer**。这是防 E07 的结构性保证。

### 6.4 Outbox relay

独立进程（或 sidecar），每实例 `SELECT ... FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED`，按 `partition_key` 保证同一聚合的顺序，发布成功后 `UPDATE published_at`。

- 至少一次；下游按 `event_id` 去重。
- 同一 `partition_key` 必须单线程发布（或按 key hash 分 worker），否则 `aggregate_sequence` 会乱。若乱序已发生，下游按 sequence 缓冲。
- 保留：Kafka 14 天；`outbox` 表保留至 `published_at + 7d` 后归档，不删。

---

## 7. 测试清单

标注 `[E##]` 的对应 docs/16 验收项。全部用 mock 依赖，不接真实模型。

### 7.1 幂等与版本

| # | 用例 | 期望 |
|---|---|---|
| T08-01 `[E12]` | 同 `command_id` + 同 body 提交两次 | 第二次原样返回第一次响应，`replayed:true`，`manifest_revision` 只 +1，Outbox 只 1 条 |
| T08-02 `[E12]` | 同 `command_id` + 改一个字段 | 409 `IDEMPOTENCY_KEY_REUSED`，无任何写入 |
| T08-03 | 不同 `command_id` + 完全相同的 body | 第二笔 409 `DUPLICATE_MANIFEST_CONTENT`（content_hash 相同） |
| T08-04 | `expected_revision` 落后且改动节点不相交 | 200 `rebased:true`，head = 原 head +1，双方改动都在 |
| T08-05 | `expected_revision` 落后且改动节点相交 | 409 `VERSION_CONFLICT`，`details.head_revision` |
| T08-06 | 并发 20 个提交同一 manifest | 全部成功或明确 409；最终 revision 单调，无跳号 |
| T08-07 | `expected_revision` 领先（客户端臆造） | 409 `VERSION_CONFLICT` |

### 7.2 崩溃恢复

| # | 用例 | 期望 |
|---|---|---|
| T08-08 `[E13]` | 在 `manifest_revision` INSERT 后、COMMIT 前 kill -9 | 无 revision、无 Outbox、无幂等 DONE；重试成功 |
| T08-09 `[E13]` | COMMIT 后、Outbox relay 前 kill | 重启后 relay 补发；`published_at` 由 NULL 变有值 |
| T08-10 `[E13]` | COMMIT + 发布后、响应前 kill | 客户端重试同 command_id 得到原响应；不产生第二个 revision |
| T08-11 `[E13]` | 提交事务中数据库连接断开后重连重试 | 同上，无半写 |
| T08-12 | `IN_PROGRESS` 僵死记录（进程崩溃） | 60s 后 janitor 重置；重试可执行 |
| T08-13 | 旧写主（`leader_epoch` 落后）尝试提交 | 503 `WRITER_EPOCH_STALE`，无写入 |

### 7.3 epoch 与接受（E07 重点）

| # | 用例 | 期望 |
|---|---|---|
| T08-14 `[E07]` | 节点 A 已接受；提交只改节点 B | A 的 accepted pointer 不变、`node_epoch` 不变；B epoch +1；事件 `changed_node_epochs` 只含 B |
| T08-15 `[E07]` | 用旧 `execution_fence` 的 QC 报告发起接受 | 409 `STALE_DECISION`，`acceptance_commit` 有 `REJECTED_STALE` 行 |
| T08-16 `[E07]` | 用旧 `node_epoch` 的审核决定发起接受 | 409 `STALE_DECISION` |
| T08-17 `[E07]` | 审核决定指向 revision 12，当前 head 是 13，但节点未变 | 200 `rebased:true`，指针写在 13 上 |
| T08-18 `[E07]` | `:fence` 后，旧 fence 下未接受的 revision | 全部 `VOIDED`；对其接受 → 409 `ILLEGAL_STATE_TRANSITION` |
| T08-19 | 对 `SUPERSEDED` 版本接受 | 409 `ILLEGAL_STATE_TRANSITION` |
| T08-20 | QC 报告 artifact_hash 与候选不一致 | 422 `QC_EVIDENCE_MISMATCH` |
| T08-21 | 节点被锁定，无 `override_reason` 接受 | 422 `NODE_LOCKED` |
| T08-22 | 两个审核并发接受同一节点 | 一个成功，一个拿到 200 `rebased` 或 409；指针指向同一 artifact |
| T08-23 | 全部节点逐个接受后 | `manifest.accepted_revision` 恰好在最后一个成功接受时更新 |

### 7.3b 节点级差异（`node-diff`）

| # | 用例 | 期望 |
|---|---|---|
| T08-23a | rev 12→13 只改节点 B | 节点 A `change=UNCHANGED`，B `INPUT_CHANGED`；`summary` 与 `nodes` 计数自洽 |
| T08-23b | rev 12→13 改 B 的输入且 B 的 accepted ref 同时变化 | B `change=INPUT_AND_ACCEPTED_CHANGED`（不被拆成两条） |
| T08-23c | 同名节点在 `to` 中不存在 | `change=REMOVED`；反向查询为 `ADDED` |
| T08-23d | 只翻转 `locked`，不动输入 | `change=UNCHANGED`，但 `from_locked`/`to_locked` 如实反映 |
| T08-23e | `to_revision` 缺省 | 等于读时的 `current_revision`，响应里显式回填 |
| T08-23f `[E28]` | 租户 B 查租户 A 的 manifest | 404 `REVISION_NOT_FOUND`，不泄漏存在性 |
| T08-23g | `from_revision` 不存在或 `> current_revision` | 404 `REVISION_NOT_FOUND` |
| T08-23h | 节点数超 2000 | 413 `NODE_DIFF_TOO_LARGE`，提示带 `node_id` 缩小范围；**不分页返回部分结果** |
| T08-23i | 同一 `(from,to)` 反复查询 | 结果与 `ETag` 完全一致；两次查询之间 head 前进不影响 rev 12→13 的结果 |
| T08-23j | 对 `VOIDED` 的 revision 做 diff | 正常返回（历史不可变，作废不等于不可读） |
| T08-23k `[E07]` | 节点 `change=UNCHANGED` 但 `execution_fence` 已变 | diff 仍报 `UNCHANGED`；用该节点旧决定去 `:accept` 必须 409 `STALE_DECISION` —— 证明 diff 不替代接受闸 |

### 7.4 安全与租户

| # | 用例 | 期望 |
|---|---|---|
| T08-24 `[E28]` | 租户 B 读租户 A 的 manifest | 404 `NOT_FOUND`，响应体不含 A 的任何标识 |
| T08-25 `[E28]` | 租户 B 提交到租户 A 的 manifest_id | 404，且不写入 |
| T08-26 `[E28]` | header `X-Tenant-Id` 与证书租户不一致 | 403 `TENANT_SCOPE_DENIED` |
| T08-27 | 已撤销的 principal 发起提交 | 403，无写入 |

### 7.5 事件

| # | 用例 | 期望 |
|---|---|---|
| T08-28 `[E32]` | 同一事件投递 3 次 | Inbox 去重，证据表只 1 行 |
| T08-29 `[E32]` | 证据事件乱序（revision 13 先于 12 到） | 缓冲后按序处理或回源；不丢证据 |
| T08-30 `[E32]` | Kafka 重放 1 小时 | 不产生新 revision、不改指针、不重发外部消息 |
| T08-31 | `aggregate_sequence` 缺号 | 30s 后回源，不永久阻塞 |
| T08-32 | Outbox 同一 partition_key 并发发布 | 下游看到的 sequence 单调（或下游能按 sequence 纠序） |

### 7.6 独立升级（E01）

| # | 用例 | 期望 |
|---|---|---|
| T08-33 | 只 import 契约 SDK，无其他服务 ORM | 构建通过，依赖图无跨服务 domain 包 |
| T08-34 | 新版本回滚到旧版本（schema 只加可选列） | 旧版本可正常读写（双读兼容窗口内） |

---

## 8. 契约冲突与待裁决（W0 必修）

按 AGENTS.md「契约与文档冲突 → 停下、报告、不自行选边」，以下是本方案落地前必须统一的点。本文档内的写法是**建议值**，最终以裁决为准。

| # | 冲突 | 本文采用 | 需要谁改 |
|---|---|---|---|
| C08-1 | `service.yaml`/catalog 写 `POST /manifests/{id}:commit`；`contracts/openapi.v2.yaml:443` 写 `/manifests/{manifest_id}/commits` | catalog 形态 | `contracts/openapi.v2.yaml` |
| C08-2 | openapi **没有** `:accept` 与 `:fence` 命令，但 docs/04、docs/06 明确要求 S08 CAS 更新 accepted pointer，且 fence 由 S08 持有 | 新增两条命令 | `contracts/openapi.v2.yaml` |
| C08-3 | openapi `commit` 请求体 `additionalProperties:false` 且无 `qc_report_refs` / `review_decision_ref` / `change_plan_ref` / `override_reason` | 增加为**可选**字段（符合"字段新增为可选"的兼容规则） | `contracts/openapi.v2.yaml` |
| C08-4 | `Idempotency-Key` 头与 body `command_id` 的关系未定义 | 头 = `command_id`，不等则 400 | 契约注释 |
| C08-5 | `request_hash` 的计算基准未定义 | RFC 8785 JCS + sha256，见 §2.1 | 契约注释 / 幂等 SDK |
| C08-6 | openapi `commit` 响应 `202 operation_id`，未定义同步 `200` 形态 | 默认 200 同步；超预算才 202 | `contracts/openapi.v2.yaml` |
| C08-7 | openapi 无节点级差异查询；S40 的精确 stale 判定与 S26 的"是否还值得跑"都需要它（见 §2.5.1） | 新增 `GET /manifests/{id}/node-diff`，纯读、不新增存储 | `contracts/openapi.v2.yaml` |

---

## 9. 运行与运维

- **部署**：独立 Deployment、ServiceAccount、连接池（写池 8、只读池 4/副本）、独立迁移流水线。
- **HPA**：按 `pg_locks` 等待时长与提交 P95；max replicas 受 `writer_lease` 串行度限制，加副本不提升单 manifest 吞吐。
- **runbook 必备**：`writer_lease` 卡死如何判定与强制回收；`IN_PROGRESS` 僵死命令清理；Outbox 积压排查；`VOIDED` 误伤恢复（只能新提交，不能反悔）；跨租户 404 的责任边界。
- **对账作业**（每日）：
  - `manifest.current_revision` 与 `max(manifest_revision.revision)` 是否一致；
  - `node_state.accepted_at_revision ≤ manifest.current_revision`；
  - `outbox` 未发布条数与最长滞留时长；
  - `acceptance_commit` 中 `REJECTED_*` 的比例突增告警（通常意味着某客户端在死循环重试旧审核）。

---

## 10. 明确不做的

- 不做内容 diff 与合并——那是 S07/S09 的事。S08 只做 CAS 与指针。
- 不做"生成成功即接受"。
- 不持有金额、配额、未知提交态。
- 不把 Redis 放进写路径。S08 没有 Redis 依赖；读缓存可以加，但读不到必须回源，且缓存不得承载任何"当前 revision"的判断。
- 不提供跨租户的任何聚合查询。
- `node-diff`（§2.5.1）只报"变了没有"，**不报"该不该重做"**。影响分析归 S09，任务作废归 S26。S08 一旦开始回答"这个变化意味着要重跑"，就成了第二个影响分析权威——这正是 AGENTS.md 禁止的。
