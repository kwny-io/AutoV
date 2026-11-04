# S43 · 交付与外部分发 —— 实现方案

状态：实现方案（W4）。`maturity: design_only` 不变。**外部分发一律默认关闭**（`contracts/examples/runtime-safety.yaml`: `external_publish_enabled: false`）。

上游依据：[docs/04 §5](../docs/04-data-consistency.md)、[docs/07 §1 step 9](../docs/07-production-workflows.md)、[docs/14 §3](../docs/14-enterprise-operations.md)、[S43 规格](../docs/design-package/services/S43-delivery.md)、[docs/16 E37](../docs/16-migration-acceptance.md)。

配套：[S42 合成渲染](S42-composition.md)（master 候选与证据）、[S28 媒体](S28-artifact-ingest.md)（bytes）、[S08 快照](S08-revision-snapshot.md)（accepted master）、[S02 策略](S02-governance-policy.md)（`external_publish` gate）、[S45 审计](S45-audit.md)。

---

## 0. 一句话边界

S43 把**已经被接受的 master** 打包成一个可验证的交付物，并在**单独授权**后把它发出去。

```
S42: 渲染出候选字节与证据
S08: 决定哪一版 master 被接受
S43: 打包 + 完整性签名 + （授权后）外发 + 记录回执
S45: 保存"谁在什么时候发了什么"的审计链
```

判据：

- "这一集现在交付了没有" → S43
- "这一集当前的有效 master 是哪版" → **S08**（不是 S43）
- "外发有没有被授权" → **S02**（不是 S43 自己判断的"审核已通过"）
- "这套交付包的字节是什么" → **S28**（S43 只有 ref 与签名）

**S43 不参与内容判定。** 它不判断"这集好不好"，只判断"包齐不齐、签名对不对、有没有被授权"。

---

## 1. 责任与明确不负责

**唯一写者：**

- `DeliveryPackage` —— 一个交付物及其类型（missing_material / preview / formal）、内容清单、状态
- `DeliveryItem` —— 包内每个文件/证据的角色与 artifact ref
- `DeliveryManifest` —— 规范化清单（含每项 content_hash），是签名的对象
- `IntegritySignature` —— 对 manifest 的签名与校验结果
- `PublishAuthorizationRef` —— 对某次外发授权的**引用**（授权本身在 S02）
- `DistributionAttempt` —— 一次外发尝试（渠道、回执、失败原因）
- `DeliveryReceipt` —— 渠道侧回执（外部系统给的 id / 状态）
- `DeliveryEvidenceBundle` —— 交付证据索引（指向 S45 的审计记录与 S42 的渲染证据）

**明确不写：**

| 事实 | 权威 | S43 的正确做法 |
|---|---|---|
| accepted master 指针 | **S08** | 只引用；打包前实时复查 |
| 媒体 bytes / hash / 存储 | **S28** | 只引用；签名时向 S28 取 hash 复核 |
| 渲染证据（滤镜图、响度、环境） | **S42** | 引用 `/master-candidates/{id}/evidence` |
| 字幕文本与文件 | **S38** | 引用 artifact |
| QC 结论 | **S35–S38** | 引用报告；不重算、不改写 |
| 策略判定与授权 | **S02 / S01** | 每次外发前 `evaluate`；自身不缓存"允许"结论 |
| 金额与成本 | **S23** | 引用 cost point-in-time；不计算 |
| 审计记录本体 | **S45** | 发审计意图；不自己写审计链 |
| 渠道凭据 | 平台密钥管理（非本服务） | 不持有；通过连接器/网关 |
| 客户合同条款 | 未定（§11 C1） | 不解释，只引用 `contract_ref` |

### 1.1 三种包类型必须在数据里明确

docs/16 E37 要求"包类型明确，正式 Master 满足完整性门"。S43 的类型不是 UI 标签，是**不可变的字段**：

| `package_type` | 用途 | 完整性门 | 可外发 |
|---|---|---|---|
| `missing_material` | 缺素材清单（告诉客户"还差什么"） | 不要求完整 | 可，但**必须**标记为不可播 |
| `preview` | 预览（可带水印/低码率） | 要求可播放，不要求正式规格 | 可，但**必须**标记 `not_for_broadcast` |
| `formal` | 正式交付 | **全部**完整性门 + 策略门 + 授权门 | 仅在单独授权后 |

**不存在"默认 formal"。** 创建包时必须显式给类型；缺省 → 422。

**`missing_material` 不是"失败包"**，它是正常产品：客户催片时，先给一个"还差 3 个镜头、2 条字幕轨"的清单，比给一个假装完整的包有用得多。E37 的另一半就是防止把它当成正式包。

### 1.2 一旦创建就不可变

`DeliveryPackage` **不可变**。修正 = 创建新包（新 `package_id`），不是改旧包。

理由：

- 客户可能已经下载了旧包。改包内容 = 客户手里的东西和服务端记录不一致，交付证据直接失效。
- 签名是对 manifest 的签名。改内容就要换签名，那就已经是另一个包了。
- 审计（E39）要能回答"3 月 5 日发给客户的是哪一版"，不可变是前提。

`DistributionAttempt` 可以多次，但每次指向同一个不可变包。重发不产生新包。

---

## 2. 实体与关键区分

### 2.1 `DeliveryPackage` vs `DeliveryManifest` vs `IntegritySignature`

```
DeliveryPackage   = 业务对象（谁、给谁、什么类型、什么状态）
DeliveryManifest  = 规范化清单（有序列表 + 每项 hash + 汇总 hash）
IntegritySignature = 对 manifest 汇总 hash 的签名
```

三者必须分层，否则"包的内容"和"包的签名"会纠缠：

- `manifest_hash` = 对规范化 manifest 序列化后的 sha256。**规范化规则版本化**（键排序、数字格式、路径分隔符统一 `/`）。
- 签名对象是 `manifest_hash`，不是"文件集合的散列"——后者会随打包顺序变化。
- **签名失败不阻止包生成**。包可以先创建、后签名；`formal` 包没有有效签名时 `deliverable=false`，但记录保留。

这条取舍的理由：签名依赖外部 KMS/HSM，可能暂时不可用。如果签名不可用就拒绝创建包，会导致"素材都准备好了但包建不出来"。让包存在、让门去挡，更稳。

### 2.2 `PublishAuthorizationRef`：授权是引用，不是结论

```jsonc
{
  "policy_decision_ref": {
    "service_id": "S02",
    "decision_id": "pd_...",
    "decision_hash": "sha256:...",
    "verdict": "allow",              // allow | deny | abstain | require_human
    "gate_type": "external_publish",
    "evaluated_at": "2026-11-01T10:00:00Z",
    "valid_until": "2026-11-01T10:15:00Z"
  }
}
```

四条硬规则：

1. **每次外发尝试都重新 `evaluate`**，不复用上一次的 `allow`。AGENTS.md 原话："把'审核已通过'当成放行依据 —— 派单前必须实时复查撤销、kill switch、预算"。
2. **`abstain` 不是 `allow`**。`abstain` → 视为未授权，返回 403 并透传 `reasons[]`。
3. **`require_human` 不是 `allow`**。需要 S40 的决定引用；没有就拒绝。
4. **`valid_until` 过期即失效**。外发是个动作，不是一次判定。授权管到"这一次尝试"，不是"这一段时间"。

S43 **不解释** S02 的判定理由，也不做"本地兜底策略"。判定归 S02，S43 只执行或拒绝。

### 2.3 正式 Master 的完整性门（E37 落点）

`formal` 包必须同时满足：

| 门 | 判据 | 失败码 |
|---|---|---|
| G1 接受门 | S08 的 accepted pointer 指向的 master 就是这个包引用的 master，且 `execution_fence` 未推进 | `MASTER_NOT_ACCEPTED` / `STALE_FENCE` |
| G2 素材门 | S28 对所有引用的 artifact 实时校验 hash 一致、对象存在、未过期 | `ARTIFACT_MISSING` / `ARTIFACT_HASH_MISMATCH` |
| G3 完整性门 | S41 的 timeline `formal_master_eligible=true`；S42 候选 `deliverable=true`；`stale_inputs=[]` | `INCOMPLETE_TIMELINE` / `NOT_DELIVERABLE` |
| G4 QC 门 | 所有必需 QC 报告存在且无阻断性缺陷（`blocking=false` 的 open issue 为 0） | `QC_BLOCKING_ISSUE` |
| G5 响度/规格门 | S42 实测 `loudness_gate=PASSED` 且符合 `master_spec` 容差 | `LOUDNESS_GATE_FAILED` |
| G6 字幕门 | 要求的每个 locale 都有 subtitle artifact，且其 `timeline_revision` 与 master 一致 | `SUBTITLE_STALE` / `SUBTITLE_MISSING` |
| G7 权利门 | S15 的音色/素材授权在交付时有效；S02 的 rights 策略不拒绝 | `RIGHTS_REVOKED` |
| G8 策略门 | S02 `external_publish` 判定 `allow`（带 `valid_until`） | `POLICY_DENIED` |
| G9 签名门 | `IntegritySignature` 校验通过 | `SIGNATURE_INVALID` |
| G10 清单门 | manifest 覆盖 docs/04 §5 的全部固定内容（§2.4） | `MANIFEST_INCOMPLETE` |

**门是"与"关系，且全部实时**。任何一项无法判定（依赖 503）→ 整体失败，错误码 `DEPENDENCY_UNAVAILABLE`，**不是门通过**。这对应 E25 的精神："不将未知当 pass"。

**预览包只过 G2/G3(部分)/G6(部分)**；缺素材包只要求 G2 覆盖已存在的项。

### 2.4 交付包的固定内容（docs/04 §5）

docs/04 §5 明确规定交付证据包的固定内容。S43 的 `formal` manifest 必须逐项覆盖：

| # | 内容 | 来源 | 在 manifest 里的角色 |
|---|---|---|---|
| 1 | manifest（本清单自身） | S43 | `manifest`（自引用，排除在签名内容外，签名的是它自己的 hash） |
| 2 | memory checkpoint | **S16** | `memory_checkpoint` |
| 3 | asset bindings（资产版本绑定） | S10–S15 | `asset_bindings` |
| 4 | QC 证据 | S35–S38 | `qc_evidence` |
| 5 | policy 快照（本次判定） | S02 | `policy_snapshot` |
| 6 | cost point-in-time | S23 | `cost_snapshot` |

**"cost point-in-time" 是快照，不是实时余额。** 交付时给客户的成本报告是"截至交付时刻的累计"，之后发生的返工不计入这个包。S43 引用 S23 的 `cost-breakdown` 结果作为 artifact，不自己算。

**memory checkpoint 与 asset bindings 是必需的**，不是可选附件。缺了它们，交付包无法回答"这部剧当时的正史与资产是什么"，E02 的"有版本来源"就断链。

### 2.5 渠道与格式：S43 不转换内容

`DistributionChannel`（S43 自己的配置）：

```jsonc
{
  "channel_id": "ch_platform_a",
  "kind": "partner_api" | "ftp_drop" | "object_storage" | "manual_handoff",
  "endpoint_ref": {"secret_ref": "..."},   // 凭据在密钥管理，不在这里
  "accepted_formats": ["mp4_h264_aac", "mov_prores"],
  "required_artifacts": ["master_video","master_audio","subtitle_zh-CN"],
  "max_file_bytes": 8589934592,
  "supports_resume": true
}
```

规则：

- **S43 不做格式转换**。渠道不接受的格式 → 拒绝发布并报告 `CHANNEL_FORMAT_UNSUPPORTED`，让 S42 按该渠道的 `master_spec` 重新渲染。
- 这条很关键：如果 S43 内置转码，它就成了第二个编码权威，而且转码会破坏已签名的内容（签名是对原始字节的）。
- `manual_handoff` 是真实存在的渠道：人工拷贝到硬盘、走内部流程。它也需要 `DistributionAttempt` 与回执（确认人 + 时间 + 去向）。

---

## 3. 状态机

### 3.1 `DeliveryPackage`

```
        POST /delivery-packages
              │ 类型校验 + 实时门评估（创建时，非外发时）
              ▼
        ┌───────────┐
        │  DRAFTING │   收集 items / 校验引用
        └───────────┘
           │        │
           │        └── 门失败 ─────────────────► BLOCKED
           │                                      （gaps[] 可读；可建新包修正）
           ▼
        ┌───────────┐
        │  SEALED   │   manifest 冻结 + manifest_hash + 签名
        └───────────┘
           │
           ├── 外发成功 ─────────────────────────► PUBLISHED
           ├── 外发失败（可重试）───────────────► SEALED（可再试）
           └── 外发失败（终态，如渠道拒收）─────► PUBLISH_FAILED
```

- `SEALED` 是"包已定稿、可交付"的分界。**只有 SEALED 才能外发。**
- `BLOCKED` 可以停滞很久（等素材）。它不是因为错误而存在，而是因为"缺东西"而存在。
- `PUBLISHED` **不是**"客户已收到"。它是"渠道已确认接收"。客户签收在 `DeliveryReceipt` 里，可能晚得多。
- `DRAFTING → BLOCKED` 的门评估与 `SEALED → 外发` 的门评估**是两次**。创建时通过不代表外发时通过（素材可能刚被撤销）。

### 3.2 `DistributionAttempt`

```
PENDING → IN_FLIGHT → SUCCEEDED
                    │
                    ├── 可重试失败 ──────────► IN_FLIGHT（退避）
                    └── 终态失败 ────────────► FAILED
                    └── 未知（超时/连接断）──► UNKNOWN
```

**`UNKNOWN` 是独立状态，不是失败。** 这是本服务最容易做错的地方：

- 上传 8GB 文件，连接在第 7.9GB 断了。渠道可能已经收下，也可能没有。
- 正确行为：置 `UNKNOWN`，**先对账**（查渠道的 object listing / 回执查询接口），不要立即重传。
- 若渠道无查询能力（如一次性 FTP drop）→ 需要人工确认（`manual_handoff` 的回执），或等渠道侧的异步回执。

这和付费生成的 `SUBMISSION_UNKNOWN` 是同一个教训（AGENTS.md）：**未知不等于失败，重试前必须对账**。虽然外发一般不产生费用，但重复外发可能产生**业务后果**（客户收到两份、平台侧重复上架、结算重复计数）。

### 3.3 非法转移

| 尝试 | 结果 |
|---|---|
| 对 `BLOCKED` 包请求外发 | 409 `PACKAGE_BLOCKED` |
| 对 `DRAFTING` 包请求外发 | 409 `PACKAGE_NOT_SEALED` |
| `formal` 包缺签名 | 409 `SIGNATURE_REQUIRED` |
| 外发时 S02 `deny`/`abstain`/`require_human` | 403 + 透传 `reasons[]` |
| 外发时 S08 accepted pointer 已变 | 409 `MASTER_SUPERSEDED`（包已过期，需新建） |
| 外发时 artifact 缺失 | 409 `ARTIFACT_MISSING`，包转 `BLOCKED` |
| 同 `command_id` 不同 body | 409 `IDEMPOTENCY_MISMATCH` |

**`MASTER_SUPERSEDED` 不是错误，是正常业务。** 包封好三小时后，导演又改了一版并被接受。此时旧包仍然可读、可作为历史交付，但**不能用于新的外发**——除非显式 `allow_superseded: true` 且 S02 允许（用于"补发历史版本给客户"的场景）。默认不允许。

---

## 4. 关键流程

### 4.1 创建交付包

```
POST /delivery-packages
  1. 认证 + scope（需要 delivery:write）
  2. 幂等查
  3. package_type 必填；缺省 422
  4. 解析 master_ref：
       ├─ 指向 S42 候选
       └─ 向 S08 确认：这个 master 是不是当前 accepted？（G1）
  5. 收集固定内容（§2.4）：
       memory_checkpoint ← S16
       asset_bindings    ← S10–S15（经 S09 查询依赖闭包）
       qc_evidence       ← S35–S38
       policy_snapshot   ← S02（本次判定）
       cost_snapshot     ← S23 cost-breakdown
  6. 逐项向 S28 校验 artifact 存在 + hash（G2）
  7. 计算 gaps（缺什么）
  8. 若 formal 且 gaps 非空 → BLOCKED，返回 gaps
  9. 事务写 package(DRAFTING) + items + outbox
 10. 同步封包：规范化 manifest → manifest_hash → 请求签名
       ├─ 签名成功 → SEALED
       └─ 签名不可用 → 保持 DRAFTING，返回 202 + 可重试
```

第 9–10 步拆开是刻意的：**包的创建与签名分两个事务**。签名可能调用外部 KMS，不能把它塞进数据库事务里（长事务 + 外部依赖 = 锁等待）。

代价：可能出现"包已创建但签名失败"的中间态。这由 `DRAFTING` 状态 + 重试封包命令处理，且**不影响包的不可变性**——`DRAFTING` 期间 manifest 未冻结，可见但不可发。

### 4.2 封包与签名

```
POST /delivery-packages/{id}:seal
  1. 幂等（seal 也带 command_id）
  2. 校验 package 处于 DRAFTING
  3. 重新校验 G2（签名前的最后一次素材复查）
  4. 规范化 manifest：
       - 按 role, ordinal 排序
       - 每项：role / artifact_ref / content_hash / byte_size / mime / locale
       - 排除自身 manifest 项
  5. manifest_hash = sha256(canonical_json(manifest) || canonical_version)
  6. 签名请求（KMS/HSM）：sign(manifest_hash, key_ref)
  7. 事务写 manifest + signature → SEALED
  8. 发 post.delivery.package.sealed（供 S45 审计、S47 更新工作台）
```

**规范化版本进 hash。** `canonical_version = "jcs-v1"`（RFC 8785）或项目自定规范；换规范化实现 = 换 hash = 旧签名失效。必须显式版本化，否则"同一清单在不同版本服务上算出不同 hash"。

**签名 key 的租户隔离**：`key_ref` 按 tenant 解析，跨租户绝不共用签名密钥。E28/E30 的直接落点。

### 4.3 外发

```
POST /delivery-packages/{id}/publish
  1. 幂等查
  2. 校验 package 状态 = SEALED
  3. **实时**重新评估：
       a. S08：accepted pointer 仍是本包 master？execution_fence 未推进？
       b. S28：所有 artifact 仍存在且 hash 一致？
       c. S15：音色/素材授权仍有效？
       d. S02：external_publish gate → 必须 allow 且未过期
       e. 渠道：格式/大小/必需项匹配
     任一失败 → 拒绝，附具体门与原因
  4. 生成 DistributionAttempt(PENDING)
  5. 通过连接器外发（不在本服务内直连渠道）
  6. 结果：
       SUCCEEDED → package PUBLISHED + receipt
       UNKNOWN   → 进对账，不重传
       FAILED    → 按可重试性决定 IN_FLIGHT 或 PUBLISH_FAILED
  7. 发审计意图到 S45
```

第 5 步的"通过连接器"是关键架构选择：

- S43 **不持有**渠道凭据、不直连第三方 API。
- 渠道连接器（`connectors/*/*`）持有凭据、实现协议。S43 调平台网关/连接器接口。
- 理由与 S26/S27 同构：**凭据与出站通道集中管理**，S43 只做编排与状态。

### 4.4 外发失败矩阵

| 失败 | 分类 | 行为 |
|---|---|---|
| 401/403 渠道认证失败 | 终态 | `FAILED`，告警；不重试（凭据问题重试无意义） |
| 429 限流 | 可重试 | 指数退避，最多 8 次 |
| 5xx | 可重试 | 退避重试 |
| 连接中断在中途 | **UNKNOWN** | 先对账；支持断点续传的渠道查已上传字节 |
| 文件超限 | 终态 | `CHANNEL_FORMAT_UNSUPPORTED`，需 S42 重渲染 |
| 渠道拒收（内容违规） | 终态 | `PUBLISH_REJECTED`，附渠道给出的事由 ref |
| DNS/网络不可达 | 可重试（有上限） | 退避 |
| 签名校验失败（渠道侧） | 终态 | `SIGNATURE_INVALID`，说明包被篡改或签名 key 不匹配 |
| 对账后确认渠道已收 | 成功 | 补登 `SUCCEEDED` + receipt |
| 对账后确认渠道未收 | 恢复 | 重新 `PENDING` |

**`UNKNOWN` 的对账必须有截止时间**：超过 TTL（如 24h）仍无法确认 → 转 `FAILED_UNRESOLVED` 并告警，**不自动重发**。人工决定。

### 4.5 重发与版本

- 重发同一包 = 新的 `DistributionAttempt`，`package` 不变。
- 材料变了（新 master / 新字幕）= 新包。
- 客户要"补发 3 月 5 日那一版"：用旧包的 `package_id`，走 `allow_superseded` + S02 判定。

**为什么允许发历史版本？** 这是真实需求：客户存档、法务取证、渠道对账。但必须显式 + 有授权，默认不允许，否则会变成"绕过最新审核发旧版"的口子。

### 4.6 一次性下载链接（若采用）

如果客户通过链接下载而不是渠道推送：

- 链接的签发由 S43 请求（`object_storage` 类渠道），**不自行生成**签名 URL。
- 链接有效期短（如 15 分钟）、绑定租户与包、一次性（可选）。
- **链接不是交付事实**。`PUBLISHED` 需要 `DeliveryReceipt`（下载完成 / 渠道确认），不是"链接发出去了"。

---

## 5. 数据模型

数据库 `delivery`，Postgres。

```sql
CREATE TABLE delivery.package (
  tenant_id            text NOT NULL,
  package_id           text NOT NULL,
  project_id           text NOT NULL,
  series_id            text NOT NULL,
  episode_id           text,
  branch_id            text,
  package_type         text NOT NULL CHECK (package_type IN
                       ('missing_material','preview','formal')),
  purpose              text,                     -- broadcast | archive | review | partner
  master_candidate_id  text,                     -- S42 候选
  master_artifact_ref  jsonb,
  timeline_ref         jsonb,
  execution_fence      bigint NOT NULL,
  manifest_hash        char(64),
  canonical_version    text NOT NULL DEFAULT 'jcs-v1',
  package_hash         char(64) NOT NULL,        -- 包的规范哈希（含 items 快照）
  state                text NOT NULL CHECK (state IN
                       ('DRAFTING','BLOCKED','SEALED','PUBLISHED','PUBLISH_FAILED')),
  gaps                 jsonb NOT NULL DEFAULT '[]'::jsonb,
  gate_results         jsonb NOT NULL DEFAULT '{}'::jsonb,
  contract_ref         jsonb,                    -- 客户合同引用（不解释）
  created_by           jsonb NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  sealed_at            timestamptz,
  published_at         timestamptz,
  PRIMARY KEY (tenant_id, package_id)
);
CREATE INDEX package_episode_idx ON delivery.package
  (tenant_id, episode_id, branch_id, created_at DESC);
CREATE INDEX package_state_idx ON delivery.package (tenant_id, state, created_at DESC);
CREATE UNIQUE INDEX package_hash_uniq ON delivery.package (tenant_id, package_hash);

CREATE TABLE delivery.item (
  tenant_id            text NOT NULL,
  package_id           text NOT NULL,
  role                 text NOT NULL CHECK (role IN
                       ('master_video','master_audio','stem_dialogue','stem_sfx',
                        'stem_bgm','subtitle','thumbnail','edl','timeline_json',
                        'memory_checkpoint','asset_bindings','qc_evidence',
                        'policy_snapshot','cost_snapshot','other')),
  ordinal              integer NOT NULL DEFAULT 0,
  artifact_ref         jsonb,                    -- S28 ref；或外部内容引用
  content_hash         char(64),
  byte_size            bigint,
  mime                 text,
  locale               text,
  external_ref         jsonb,                    -- memory_checkpoint 等非 S28 内容
  PRIMARY KEY (tenant_id, package_id, role, ordinal),
  FOREIGN KEY (tenant_id, package_id)
    REFERENCES delivery.package (tenant_id, package_id),
  CONSTRAINT item_has_target
    CHECK (artifact_ref IS NOT NULL OR external_ref IS NOT NULL)
);
CREATE INDEX item_hash_idx ON delivery.item (tenant_id, content_hash);

CREATE TABLE delivery.manifest (
  tenant_id            text NOT NULL,
  package_id           text NOT NULL,
  manifest_hash        char(64) NOT NULL,
  canonical_version    text NOT NULL,
  manifest             jsonb NOT NULL,
  item_count           integer NOT NULL,
  total_bytes          bigint NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, package_id)
);

CREATE TABLE delivery.integrity_signature (
  tenant_id            text NOT NULL,
  signature_id         text NOT NULL,
  package_id           text NOT NULL,
  manifest_hash        char(64) NOT NULL,
  key_ref              jsonb NOT NULL,
  algorithm            text NOT NULL,            -- ed25519 | rsa-pss-sha256 | ...
  signature_b64        text NOT NULL,
  verified             boolean NOT NULL DEFAULT false,
  verified_at          timestamptz,
  verify_error         text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, signature_id)
);
CREATE INDEX signature_package_idx ON delivery.integrity_signature
  (tenant_id, package_id, created_at DESC);

CREATE TABLE delivery.publish_authorization_ref (
  tenant_id            text NOT NULL,
  auth_ref_id          text NOT NULL,
  package_id           text NOT NULL,
  policy_decision_id   text NOT NULL,
  decision_hash        char(64) NOT NULL,
  verdict              text NOT NULL CHECK (verdict IN
                       ('allow','deny','abstain','require_human')),
  gate_type            text NOT NULL DEFAULT 'external_publish',
  human_decision_ref   jsonb,                    -- S40 决定（require_human 时）
  valid_until          timestamptz,
  evaluated_at         timestamptz NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, auth_ref_id)
);

CREATE TABLE delivery.distribution_attempt (
  tenant_id            text NOT NULL,
  attempt_id           text NOT NULL,
  package_id           text NOT NULL,
  channel_id           text NOT NULL,
  state                text NOT NULL CHECK (state IN
                       ('PENDING','IN_FLIGHT','SUCCEEDED','FAILED','UNKNOWN',
                        'FAILED_UNRESOLVED')),
  attempt_no           integer NOT NULL,
  bytes_sent           bigint NOT NULL DEFAULT 0,
  remote_ref           text,                     -- 渠道侧 id（对账用）
  failure_code         text,
  failure_detail       jsonb,
  reconcile_after      timestamptz,              -- UNKNOWN 时的对账时间
  started_at           timestamptz,
  finished_at          timestamptz,
  PRIMARY KEY (tenant_id, attempt_id),
  UNIQUE (tenant_id, package_id, channel_id, attempt_no)
);
CREATE INDEX attempt_unknown_idx ON delivery.distribution_attempt (reconcile_after)
  WHERE state = 'UNKNOWN';
CREATE INDEX attempt_package_idx ON delivery.distribution_attempt
  (tenant_id, package_id, started_at DESC);

CREATE TABLE delivery.receipt (
  tenant_id            text NOT NULL,
  receipt_id           text NOT NULL,
  attempt_id           text NOT NULL,
  package_id           text NOT NULL,
  channel_id           text NOT NULL,
  remote_id            text,
  acknowledged_at      timestamptz NOT NULL,
  signature_valid      boolean,
  raw_ref              jsonb,                    -- 原始回执的引用（可能有个推之外的内容）
  PRIMARY KEY (tenant_id, receipt_id)
);
CREATE INDEX receipt_package_idx ON delivery.receipt (tenant_id, package_id);

CREATE TABLE delivery.channel (
  tenant_id            text NOT NULL,
  channel_id           text NOT NULL,
  kind                 text NOT NULL CHECK (kind IN
                       ('partner_api','ftp_drop','object_storage','manual_handoff')),
  endpoint_ref         jsonb NOT NULL,           -- 指向密钥管理，不含凭据
  accepted_formats     text[] NOT NULL,
  required_artifacts   text[] NOT NULL,
  max_file_bytes       bigint,
  supports_resume      boolean NOT NULL DEFAULT false,
  supports_query       boolean NOT NULL DEFAULT false,  -- 决定 UNKNOWN 能否自动对账
  enabled              boolean NOT NULL DEFAULT false,  -- 默认关闭
  PRIMARY KEY (tenant_id, channel_id)
);

CREATE TABLE delivery.command_receipt (
  tenant_id            text NOT NULL,
  command_id           text NOT NULL,
  request_hash         char(64) NOT NULL,
  response_body        jsonb NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE delivery.outbox (
  event_id             uuid PRIMARY KEY,
  tenant_id            text NOT NULL,
  aggregate_id         text NOT NULL,
  aggregate_sequence   bigint NOT NULL,
  event_type           text NOT NULL,
  partition_key        text NOT NULL,     -- tenant_id || ':' || aggregate_id
  payload              jsonb NOT NULL,
  headers              jsonb NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  published_at         timestamptz
);
CREATE INDEX outbox_unpublished_idx ON delivery.outbox (created_at)
  WHERE published_at IS NULL;

CREATE TABLE delivery.inbox (
  consumer             text NOT NULL,
  event_id             uuid NOT NULL,
  received_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

**不建的表：**

- 不建 `accepted_master` —— 归 S08。
- 不建 `audit_log` —— 归 S45。
- 不建 `package_content_cache` —— manifest 就是快照。

**`channel.enabled` 默认 false**，与所有生产开关默认关闭一致。

---

## 6. 对外契约

### 6.1 命令

`POST /delivery-packages`（`contracts/openapi.v2.yaml` 已定义）

```jsonc
{
  "context": {...execution-context v2...},
  "package_type": "formal",
  "purpose": "broadcast",
  "episode_id": "ep_3",
  "branch_id": "main",
  "master_candidate_id": "mc_...",
  "timeline_ref": {"timeline_id":"tl_ep3","revision":10,"content_hash":"sha256:..."},
  "contract_ref": {"id":"contract_a","revision":3,"content_hash":"..."},
  "requested_locales": ["zh-CN"],
  "command_id": "...",
  "request_hash": "sha256:..."
}
```

响应 `201`：

```jsonc
{
  "package_id":"dp_...",
  "package_type":"formal",
  "state":"SEALED",           // 或 BLOCKED
  "package_hash":"sha256:...",
  "manifest_hash":"sha256:...",
  "gaps": [],
  "gate_results": {"G1":"pass","G2":"pass","G3":"pass","G4":"pass","G5":"pass",
                   "G6":"pass","G7":"pass","G8":"deferred","G9":"pass","G10":"pass"},
  "deliverable": true,
  "operation_id":"..."
}
```

注意 `G8: "deferred"`：策略门在**外发时**评估（§2.2 规则 1），创建时不判定。这是刻意的——创建包可以慢慢准备，外发必须实时授权。

`POST /delivery-packages/{id}:seal`、`POST /delivery-packages/{id}/publish`（后者 OpenAPI 已定义）

```jsonc
{
  "context": {...},
  "channel_id": "ch_platform_a",
  "publish_authorization_ref": {"policy_decision_id":"pd_...","decision_hash":"sha256:..."},
  "allow_superseded": false,
  "command_id": "...",
  "request_hash": "sha256:..."
}
```

响应 `202`：

```jsonc
{
  "attempt_id":"da_...",
  "state":"IN_FLIGHT",
  "channel_id":"ch_platform_a",
  "operation_id":"..."
}
```

**`publish_authorization_ref` 是调用方提供的 S02 判定引用，但 S43 必须自己再调一次 S02 校验该判定的 `decision_hash` 与 `valid_until`。** 不能让调用方"自带一张允许票"——那等于授权可以伪造。正确顺序：调用方先取判定 → 传给 S43 → S43 用同一个 `decision_id` 回查 S02，确认判定存在、未过期、结论一致。

### 6.2 查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/delivery-packages/{id}` | 包 + gaps + gate_results |
| GET | `/delivery-packages/{id}/manifest` | 规范化清单 |
| GET | `/delivery-packages/{id}/manifest?signed=true` | 带签名 |
| GET | `/delivery-packages?episode_id=&package_type=&state=` | 列表 |
| GET | `/delivery-packages/{id}/evidence` | 交付证据索引（含审计 ref、渲染证据 ref） |
| GET | `/delivery-packages/{id}/attempts` | 外发尝试与回执 |

### 6.3 事件

| 方向 | 事件 |
|---|---|
| 发布 | `delivery.package.created`、`delivery.package.sealed`、`delivery.package.published`、`delivery.publish.failed` |
| 消费 | `post.master.rendered`（S42）、`snapshot.manifest.committed`（S08）、`governance.asset.rights.changed`（S15）、`production.subtitle.revision.created`（S38） |
| 调用 | S08 查询 accepted pointer、S28 校验 artifact、S02 `policy-decisions:evaluate`、S16/S09/S23 取固定内容、连接器外发、S45 审计 |

`delivery.package.published` 的 `data`：

```jsonc
{
  "package_id":"dp_...","package_type":"formal",
  "episode_id":"ep_3","master_candidate_id":"mc_...",
  "package_hash":"sha256:...","manifest_hash":"sha256:...",
  "channel_id":"ch_platform_a","attempt_id":"da_...",
  "remote_id":"partner_...",
  "published_at":"...",
  "policy_decision_id":"pd_..."
}
```

**事件不含签名内容、不含下载链接、不含渠道凭据。** 下游需要签名去查 API。

---

## 7. 幂等与并发

### 7.1 幂等

| 操作 | 键 | 说明 |
|---|---|---|
| 创建包 | `tenant + command_id + request_hash` | 同键同 body 返回原包 |
| 封包 | 同上 | 重复封包不重算（manifest 已冻结） |
| 外发 | 同上 + 每次尝试是新 `attempt_id` | 同 command 重投不产生第二个分发 |

**外发的幂等的微妙处**：重试同一个 `command_id` 应返回**同一个 attempt**，不是再发一次。如果 attempt 已经 `UNKNOWN`，返回 `UNKNOWN` 状态，不再发。这是防止"客户端超时重试导致重复外发"的关键。

### 7.2 并发

| 场景 | 机制 |
|---|---|
| 两个客户端同时封包 | `UPDATE package SET state='SEALED' WHERE state='DRAFTING' RETURNING *`；后到者看到已 SEALED，返回原 manifest |
| 外发与 accepted pointer 变化竞争 | 外发前查 S08；外发中变化不回滚（已发出去了），但记录 `superseded_during_publish` |
| 两个渠道同时发同一包 | **允许**。不同渠道不同 attempt，各自独立 |
| 同一渠道并发两个 attempt | 拒绝第二个，返回第一个（渠道侧可能不接受重复） |
| UNKNOWN 对账与人工重发竞争 | 对账任务先占 `reconcile_after`，用 `FOR UPDATE SKIP LOCKED` |

**"允许并发发不同渠道"是刻意的**：同一部片要同时上 A 平台和 B 平台，串行会让总时长翻倍。风险是某个渠道失败需要单独重试——这正是 attempt 粒度的意义。

### 7.3 与 S08 的边界

- S43 **不写** accepted pointer。
- S43 的 `master_candidate_id` 是**引用**。如果 S08 之后接受了一个新 master，旧包仍然是"历史交付"，但不能再用于新外发（除非 `allow_superseded`）。
- `MASTER_SUPERSEDED` 的判定依赖 S08 的实时查询，**不依赖 S42 的本地投影**（S42 的 `ACCEPTED_BY_SNAPSHOT` 可能落后）。

---

## 8. 可观测

| 指标 | 类型 | 用途 |
|---|---|---|
| `delivery_package_created_total` | counter（按 type） | 交付量 |
| `delivery_gate_fail_total` | counter（按 gate, code） | 哪道门最常挡 |
| `delivery_publish_success_total` | counter（按 channel） | 渠道健康 |
| `delivery_publish_unknown_total` | counter（按 channel） | 对账压力 |
| `delivery_publish_latency_seconds` | histogram（按 channel, size bucket） | 外发耗时 |
| `delivery_manifest_verify_latency_ms` | histogram | 签名/校验 |
| `delivery_blocked_age_seconds` | gauge（按 tenant） | 卡住的包 |
| `delivery_superseded_publish_total` | counter | 过期包外发尝试 |

trace span：

```
delivery.publish
  ├─ package.state_check
  ├─ s08.get_accepted_pointer
  ├─ s28.verify_artifacts
  ├─ s15.check_rights
  ├─ s02.evaluate_external_publish
  ├─ connector.upload
  └─ s45.audit_intent
```

**日志不含**：下载链接、签名原文、渠道凭据、客户合同内容。

---

## 9. 测试清单

### E37 —— 包类型明确，正式 Master 满足完整性门

| 用例 | 期望 |
|---|---|
| E37-S43-01 | 缺 3 个素材 → `missing_material` 包创建成功、`gaps` 明细、`deliverable=false` |
| E37-S43-02 | 同素材建 `formal` → `BLOCKED`，不进 SEALED |
| E37-S43-03 | 十道门逐项注入失败：每道门都能独立挡住 |
| E37-S43-04 | 依赖 503 → 门为"未知"，整体拒绝（不当作 pass） |
| E37-S43-05 | `preview` 包在 `loudness_gate=FAILED` 时仍可创建，但 `deliverable=false` 且不可当正式包 |
| E37-S43-06 | manifest 缺 `memory_checkpoint` → `MANIFEST_INCOMPLETE` |
| E37-S43-07 | master 在创建后被 supersede → 外发 `MASTER_SUPERSEDED` |
| E37-S43-08 | `allow_superseded=true` + S02 allow → 历史版本可补发；无授权 → 拒绝 |

### E27 —— 撤权与越权

| 用例 | 期望 |
|---|---|
| E27-S43-01 | 外发时 S02 `deny` → 403，附 `reasons[]` |
| E27-S43-02 | 调用方伪造 `publish_authorization_ref`（decision_id 不存在） → 403 |
| E27-S43-03 | 判定已过 `valid_until` → 403 |
| E27-S43-04 | 外发前 kill switch 打开 → 拒绝，且不缓存"上次允许" |
| E27-S43-05 | `require_human` 无 S40 决定 → 403 |

### E28 —— 跨租户

| 用例 | 期望 |
|---|---|
| E28-S43-01 | 跨租户读包 → 404 |
| E28-S43-02 | 跨租户引用 master/artifact → S08/S28 拒绝；S43 不泄漏存在性 |
| E28-S43-03 | 签名 key 按租户隔离，A 租户的 key 不能验 B 的包 |
| E28-S43-04 | 渠道配置按租户隔离 |

### E17 —— 回执乱序/重复/伪签名

| 用例 | 期望 |
|---|---|
| E17-S43-01 | 渠道回调先于 attempt 落库 → inbox 暂存，attempt 出现后处理 |
| E17-S43-02 | 重复回执 → 幂等，不产生第二个 receipt |
| E17-S43-03 | 伪签名回执 → 拒绝，记审计 |
| E17-S43-04 | 回执乱序（旧状态覆盖新状态） → 终态不倒退 |

### E12 —— 幂等

| 用例 | 期望 |
|---|---|
| E12-S43-01 | 同 command 重投 publish → 同一 attempt，不重复外发 |
| E12-S43-02 | 同 command 不同 body → 409 |
| E12-S43-03 | 重复 seal → 返回原 manifest，不重算 hash |

### E13 / E16 —— 崩溃与 UNKNOWN

| 用例 | 期望 |
|---|---|
| E13-S43-01 | 封包事务中崩溃 → 无 manifest、无 signature，包仍 DRAFTING |
| E13-S43-02 | 外发中断 → `UNKNOWN`，不自动重传 |
| E16-S43-01 | 对账确认已收 → `SUCCEEDED` |
| E16-S43-02 | 对账确认未收 → 回 `PENDING` 重发 |
| E16-S43-03 | 对账超 TTL → `FAILED_UNRESOLVED` + 告警，不自动重发 |

### E39 —— 审计可追溯

| 用例 | 期望 |
|---|---|
| E39-S43-01 | 每次 seal/publish 都有 S45 审计意图，与包状态一致 |
| E39-S43-02 | 从包可追到 policy decision、master candidate、rendering evidence |
| E39-S43-03 | 包删除（若策略要求）后审计仍显示"曾经发过什么" |

### 一致性与恢复

| 用例 | 期望 |
|---|---|
| C-S43-01 | 同 manifest 重算 hash 100 次一致（canonical_version 固定） |
| C-S43-02 | 包不可变：任何"修改包内容"的路径不存在 |
| C-S43-03 | 升级：新版本读旧 `canonical_version` 的 manifest 可校验 |
| C-S43-04 | 回滚：旧版本不认识新 item role → 拒绝该包，不静默忽略 |

---

## 10. 实施顺序

| 步骤 | 内容 | 前置 |
|---|---|---|
| 1 | 固化 `delivery-package.v2.schema.json` / `delivery-manifest.v1.schema.json` / 事件 payload | W0 |
| 2 | `package` + `item` + 门框架（G1–G10 骨架，先全部返回 `not_implemented`） | 1 |
| 3 | manifest 规范化 + hash | 2 |
| 4 | 固定内容收集（S16/S09/S35/S23 mock） | 3 |
| 5 | 签名（本地测试 key → 后续 KMS） | 3 |
| 6 | 外发（`manual_handoff` 渠道先行，无网络依赖） | 4, 5 |
| 7 | `partner_api` / `object_storage` 渠道 + UNKNOWN 对账 | 6 |
| 8 | S02 实时门 + S08 复查 | 2 |
| 9 | 审计意图到 S45 | 6 |

**第 6 步用 `manual_handoff` 先行**：它没有外部依赖，能把"封包 → 外发 → 回执 → 审计"整条链路先跑通。真实渠道接入需要实施会话授权（AGENTS.md："需要部署或外部分发 → 停下，等授权"）。

**外发开关默认关闭**：`channel.enabled=false`，且平台级 `external_publish_enabled=false`。双层关闭。

---

## 11. 冲突（报告，不选边）

| # | 冲突 | 双方原文 | 影响 |
|---|---|---|---|
| C1 | 客户合同/输出规格无权威 | docs/07 §5 说由"客户项目规格"确定；`customer-configs/` 是版本化配置但非服务 | `contract_ref` / `master_spec_ref` 无可靠寻址目标 |
| C2 | S43 `depends_on` 不含 S42 | service.yaml 列 S08/S28/S35/S36/S37；但 master 从 S42 来 | 本方案假定经 S28 artifact id 引用，不直连 S42 的候选 API。需 owner 确认 |
| C3 | 交付包固定内容与 S43 `data_owner` 不完全对齐 | docs/04 §5 要求 memory checkpoint / asset bindings / qc / policy / cost；S43 `data_owner` 只说 DeliveryPackage/外发/签名/清单 | 本方案把后五项作为 item 引用，不拥有其内容。需确认"引用"是否满足 docs/04 |
| C4 | `publish_authorization_ref` 的语义 | OpenAPI 的 `/publish` body 含 `publish_authorization_ref`，但未定义其结构；S02 方案的四值判定与 `valid_until` 是提案 | 本方案要求 S43 回查 S02，不信任调用方传入的结论（安全取舍）。需契约明确 |
| C5 | 外发是否算"付费/计费动作"无明文 | docs/12 只列生成成本；渠道分发可能有平台侧费用 | 本方案不记账；若渠道收费，需要 S23 归集维度 |
| C6 | 签名 key 的归属无明文 | service.yaml 写"签名与完整性"归 S43；但密钥管理不在 48 服务表中 | 本方案用 `key_ref` 引用外部 KMS |
| C7 | `DeliveryPackage` 是否覆盖系列级交付 | S43 `data_owner` 未限制粒度；OpenAPI 的创建 body 无 `series_id`，但有 `master_artifact_id` | 本方案支持 `episode_id` 与 `series_id` 二选一（系列包为多集聚合）；需契约确认 |

---

## 12. 未决问题

1. **系列级交付包**：一次交付 24 集，是 24 个包还是 1 个系列包？本方案支持两者，但清单结构与签名对象需要明确。
2. **发票/结算文件**是否属于交付包？如果是，谁生成？S23 有 cost-breakdown，但"发票"是财务对象，不在 48 服务里。
3. **交付后的撤回**：客户要求"下架已交付内容"，S43 能做的只有记录撤回请求与尝试通知渠道。是否需要 `PackageRevoked` 状态与对外通知，待定。
4. **UNKNOWN 的人工对账流程**：`FAILED_UNRESOLVED` 之后谁处理、在哪个界面？S47 的工作台可能需要一个"待人工确认的外发"队列。
5. **DRM / 加密交付**：若渠道要求 DRM，密钥管理与打包属于谁？本方案假设不属于 S43。
6. **包的最大规模**：一部 24 集 × 4K 母版可能几十 TB。manifest 与签名的处理上限需明确（是否支持分卷包）。
