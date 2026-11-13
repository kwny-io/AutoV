"""S48 自己的 schema。表来源是 `plans/S48-adaptation-training.md` §5。

四条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **没有 `balance` / `quota_remaining` 列，也没有 `current_rights_state` 表。**
   钱归 S23、permit 归 S22、授权态归 S11–S15（AGENTS.md 第 18 行）。
   `training_slot_usage` 只是"本服务上报给 S23 的用量视图"，不是账本。
2. **`dataset_item` 的 `rights_grant_kind` 与 `rights_state_at_check` 是 NOT NULL。**
   没有授权引用的数据项在数据库层面就进不来（§1.3 第 2 条）。这不是运行时
   可选校验，所以 `INSERT` 直插会炸在约束上而不是悄悄放行（T48-18）。
3. **`lora_artifact` 没有"是否可挂载"列，也没有在线加载记录。**
   挂载是 S25 的路由 + S26 的执行；本表只回答"这个权重基于哪个基座、
   什么时候被判兼容"（§1.5、§10.5）。
4. **`base_model_compatibility` 是训练时刻的判断记录，不是当前兼容性。**
   它允许过期。S24 是当前兼容性的权威，读接口因此必须带
   `__hints.authority.current_compatibility = "S24"`（T48-23）。

方案里的 `jsonb` 存 TEXT（JCS），`timestamptz` 存定宽 UTC 字符串
（`"%Y-%m-%dT%H:%M:%S.%f"[:-3]+"Z"`）。所有主键/唯一约束含 `tenant_id`
（E28）。方案 §5 DDL 里没有、但状态机与事件必须有、这里补上的列与表，
逐条记在 CONTRACT_GAPS（C48-13 / C48-15 / C48-16）。
"""

ID_PREFIX = {
    "job": "job_",
    "dataset": "ds_",
    "compat": "cmp_",
    "artifact": "lora_",
    "audit": "aud_",
    "usage": "tsu_",
}

# 本轮**不解决**的缺口。C48-1…C48-11 是方案 §11 自己登记的；C48-12 起是
# 实现时发现的。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要自行选边」，
# 本服务按方案散文实现，把偏差记在这里，不改契约、不代填决定。
CONTRACT_GAPS = [
    # --- 方案 §11 自己登记的 ---
    {"id": "C48-1", "kind": "gap",
     "summary": "docs/16 的 E01–E40 没有专门的训练验收条目，只有 E39 顺带提到"
                "「训练数据」；service.yaml 的 acceptance 没有对应 E 编号",
     "handling": "测试按 §9.7 的映射挂到 E30/E39/E14/E15/E35/E13/E33/E28/E31/"
                 "E01/E40，并对每条测试标注它覆盖的是哪一条既有 E。"
                 "「训练」本身的验收基准仍需 docs/16 补一条。"},
    {"id": "C48-2", "kind": "gap",
     "summary": "没有 rights-ref v1 schema；grant_kind（生成 vs 训练）不存在于"
                "任何契约，§2.3 的形状是方案提案",
     "handling": "数据项从命令的 artifact_refs[].rights_ref 或 S28 artifact "
                 "文档里的 rights_ref 取引用，**不自造字段**；写入前逐项回查"
                 "权威，快照落 dataset_item.rights_* 五列。"},
    {"id": "C48-3", "kind": "needs_owner",
     "summary": "S15/S11–S14 的授权模型没有「可用于生成」与「可用于训练」的区分",
     "handling": "**默认拒绝**：权威返回的 grant_kind 里不含 `training`"
                 "（含完全没有 grant_kind）→ 该项排除，reason=NO_TRAINING_GRANT。"
                 "绝不假设「没说不行就是允许」。"},
    {"id": "C48-4", "kind": "gap",
     "summary": "event-topics.v2.json 只登记 adaptation.training.completed；"
                "training.lora.registered / training.voice_clone.ready / "
                "lora.blocked 都未登记",
     "handling": "**不发未登记事件**（与 S41 E41-STALE 同一纪律）。建议动作以"
                 "响应里的 `advisories` 返回；BLOCKED 只能由查询方回读状态。"
                 "事件注册后应改为 outbox，届时 S25 才能被动感知撤销。"},
    {"id": "C48-5", "kind": "conflict",
     "summary": "openapi.v2.yaml 没有 /training-jobs、/datasets/{id}、"
                "/adaptation-artifacts/{id} 任何一条路径；service.yaml 声明了其中两条",
     "handling": "按方案 §4.1 实现，不改契约。契约面的路径等 owner 补。"},
    {"id": "C48-6", "kind": "conflict",
     "summary": "service.yaml 的 depends_on 不含 S15，但 voice_clone 必须有 S15 授权",
     "handling": "授权源按 service_id 路由，S15 与 S11–S14 同等对待；"
                 "voice_clone 的目标服务只接受 S15。部署依赖需 catalog 补 S15。"},
    {"id": "C48-7", "kind": "gap",
     "summary": "dataset-manifest / evaluation-report（训练侧）没有 schema",
     "handling": "两本账按 §5.2/§6 的列实现，不另造 JSON 契约；"
                 "查询接口直接返回行形状。"},
    {"id": "C48-8", "kind": "needs_owner",
     "summary": "hyperparams.profile 的命名档位（character_default_v3）没有定义",
     "handling": "只校验 profile 是非空字符串，不解析档位内容；档位到具体参数的"
                 "映射属于部署配置，不在本服务里发明。"},
    {"id": "C48-9", "kind": "needs_owner",
     "summary": "S24 是否提供「基座 revision 间兼容性」查询未定义",
     "handling": "通过注入的 base_model_source 读 `found` / `compatible` / "
                 "`reason` 三件事；查不到 verdict 时写 UNKNOWN，"
                 "**不把 UNKNOWN 当 COMPATIBLE**。"},
    {"id": "C48-10", "kind": "gap",
     "summary": "S48 的 retention-check 在 S28 的引用方清单里没有",
     "handling": "按 §10.3 提供 GET /retention-checks?kind=&id=，"
                 "把 DATASET_SOURCE 与 PUBLISHED_LORA 两种引用分开回答。"
                 "S28 侧是否调用需它自己接。"},
    {"id": "C48-11", "kind": "needs_owner",
     "summary": "S10 的分支隔离问题会牵动 S48（是否按分支索引 LoRA）",
     "handling": "本轮**不建**分支索引；artifact 按 target + base 索引。"
                 "需要时再补，避免先造一个没人用的维度。"},

    # --- 实现时新发现的 ---
    {"id": "C48-12", "kind": "gap",
     "summary": "§3.1 说 REQUESTED/ADMITTED/QUEUED/PREPARING/TRAINING/RESUMING "
                "都可取消，但 14 个状态里没有 CANCELLED，§6「终态」也没写取消去哪",
     "handling": "未开训（REQUESTED/ADMITTED/QUEUED/PREPARING）→ REJECTED，"
                 "rejection.reason=CANCELLED（什么都没训，按「请求未推进」记）；"
                 "已开训（TRAINING/RESUMING）→ FAILED，reason=CANCELLED，"
                 "保留最后 checkpoint（§3.1「部分 checkpoint 保留」）。"
                 "新增状态需改 DDL CHECK 与事件 enum，属 owner 决定。"},
    {"id": "C48-13", "kind": "conflict",
     "summary": "§4.3 的 aggregate_sequence 是「每个状态迁移 +1」的阶段序号，"
                "§5.1 的 training_job 却没有承载它的列；§6.4 的心跳超时与 "
                "§6.3 的 per-tenant 上限也需要列，方案未给",
     "handling": "补 aggregate_sequence / last_heartbeat_at / resume_count / "
                 "eval_attempts / rights_hold / home_region 六列，全部带默认值。"
                 "前两列是事件与 janitor 的必需品，不是新权威。"},
    {"id": "C48-14", "kind": "needs_owner",
     "summary": "§12.5 明说评测阈值（identity_preservation > 0.85 之类）没有给出",
     "handling": "默认阈值写在 service.DEFAULT_THRESHOLDS 并按 adaptation_type "
                 "分档；请求的 evaluation.thresholds 可覆盖。阈值是部署配置，"
                 "不是本服务发明的业务事实。"},
    {"id": "C48-15", "kind": "conflict",
     "summary": "§3.7 说「PUBLISHED 之前才把权重注册到 S28」，而 §5.3 的 "
                "lora_artifact.storage_ref 是 NOT NULL 的「S28 的 artifact ref」；"
                "CANDIDATE 阶段还没有 S28 id",
     "handling": "补一列 weights_blob_ref（S48 自己桶里的权重对象，NOT NULL），"
                 "storage_ref 改为可空、只在 PUBLISHED 时填 S28 的 artifact_id。"
                 "两个引用都留着，训练产物到注册产物的链路才可追溯。"},
    {"id": "C48-16", "kind": "gap",
     "summary": "§8.3 的 S45 审计动作没有可调用的 S45 接口（本仓 S45 只有 "
                "service.yaml，没有 API 契约）",
     "handling": "本地 append-only 表 training_audit 记录同一组动作（create/"
                 "reject/freeze/start/complete/fail/publish/block）。"
                 "它**不替代 S45**：跨服务审计链仍缺上送通道。"},
    {"id": "C48-17", "kind": "gap",
     "summary": "§4.1 只列了 cancel/accept/reject，状态机的 admit/prepare/start/"
                "progress/finish/evaluate/resume 没有任何入口；没有这些入口"
                "作业只能停在 ADMITTED",
     "handling": "补一组 runner 端点（:admit / :prepare / :start / :progress / "
                 ":finish / :evaluate / :resume）。它们不改业务语义，只是"
                 "把方案 §3.2–§3.6 的步骤变成可调用命令。"},
    {"id": "C48-18", "kind": "gap",
     "summary": "§2.2 说 derived 模式「与 artifact_refs 求交/差」，没有说清"
                "求交还是求差，也没有 derived_from_run 的来源服务（S28 的 run "
                "输入集查询未在契约里）",
     "handling": "按「推导成员 ∪ 显式附加成员 − exclude_artifact_refs」实现，"
                 "并在响应里返回完整 resolved_items 让调用方确认；"
                 "求交/求差由 owner 裁决，改一处即可。"},
    {"id": "C48-19", "kind": "conflict",
     "summary": "contracts/adaptation.training-completed.v2.schema.json 的 "
                "target_ref 只有三键（缺 content_hash），DDL 有第四列；"
                "事件里也不发 dataset 的 exclusions",
     "handling": "事件按 schema 发三键 target_ref（ATR-4 已登记），"
                 "content_hash 留在 training_job 行与查询接口里；"
                 "不改契约，也不在事件里加 schema 没允许的键。"},
    {"id": "C48-20", "kind": "gap",
     "summary": "§10.4 说 training_slot_usage 定期上报给 S23，但没有上报接口",
     "handling": "只写本地 usage 行并在查询里暴露；**不推送**、不预扣、"
                 "不缓存余额。上报通道等 S23 侧定。"},
    {"id": "C48-21", "kind": "gap",
     "summary": "方案没有给「取消一个 CANDIDATE」「对 PUBLISHED 发 cancel」"
                "这类组合的错误码，只说 JOB_STATE_CONFLICT；"
                "评测指标不全、permit 失效也没有码",
     "handling": "所有非法迁移统一 409 JOB_STATE_CONFLICT，details.from/to 给出"
                 "实际状态；评测缺必需指标 → 422 EVALUATION_INCOMPLETE；"
                 "permit 复核失效 → 409 PERMIT_INVALID 并退回 ADMITTED。"
                 "三个码都登记在此，等 owner 并入 §4.5。"},
    {"id": "C48-22", "kind": "conflict",
     "summary": "429 TRAINING_QUEUE_FULL 的触发条件三处不一致：§4.5 说是队列满，"
                "§3.9 说队列满时新作业在 QUEUED 等待，F48-05 又写 429；"
                "§6.3 的每租户并发上限（2 小 / 1 大）也没给默认值",
     "handling": "按「每租户活跃作业数上限」（默认 2，可配置）在 create 时返回 "
                 "429 TRAINING_QUEUE_FULL + Retry-After，**只压本租户**；"
                 "admission 拿不到 permit 时仍保持 ADMITTED（不是 429）。"
                 "这样「单租户刷 20 个」不会饿死别人（T48-27），"
                 "而「GPU 队列深度」是 S22 的事实，本服务不复制。"},
    {"id": "C48-23", "kind": "gap",
     "summary": "E31 要求训练数据与 checkpoint 留在租户 home_region，但方案的 "
                "DDL 没有 region 列，也没有说 region 由谁签注",
     "handling": "补 home_region 列（NOT NULL），取值顺序：body.context."
                 "home_region → X-Home-Region 头 → 'unspecified'。**不按"
                 "「最近机房」猜**。签名绑定仍缺，属平台契约缺口。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 训练作业（§5.1 + C48-13 的六列） ----------
-- 这张表没有 accepted 指针、没有 balance、没有 current_rights_state：
-- 接受归 S08，钱归 S23，授权态归 S11–S15。`artifact_id` 只是本服务
-- 产出的 LoRA 的本地 id，不是任何东西的放行依据。
CREATE TABLE IF NOT EXISTS training_job (
  job_id             TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  command_id         TEXT NOT NULL,
  request_hash       TEXT NOT NULL CHECK (length(request_hash) = 64),

  adaptation_type    TEXT NOT NULL CHECK (adaptation_type IN
                       ('character_lora','style_lora','voice_clone')),
  target_service     TEXT NOT NULL,
  target_id          TEXT NOT NULL,
  target_revision    INTEGER NOT NULL CHECK (target_revision >= 1),
  target_content_hash TEXT NOT NULL CHECK (length(target_content_hash) = 64),

  base_service       TEXT NOT NULL DEFAULT 'S24',
  base_id            TEXT NOT NULL,
  base_revision      INTEGER NOT NULL CHECK (base_revision >= 1),
  base_content_hash  TEXT NOT NULL CHECK (length(base_content_hash) = 64),

  dataset_id         TEXT,
  hyperparams        TEXT NOT NULL,
  evaluation_spec    TEXT NOT NULL,

  state              TEXT NOT NULL CHECK (state IN
                       ('REQUESTED','REJECTED','ADMITTED','QUEUED','PREPARING',
                        'TRAINING','RESUMING','EVALUATING','EVAL_FAILED',
                        'CANDIDATE','PUBLISHED','BLOCKED','FAILED','EXPIRED')),
  state_since        TEXT NOT NULL,
  rejection          TEXT,

  budget_auth_ref    TEXT NOT NULL,
  quota_permit_ref   TEXT,
  gpu_seconds_consumed INTEGER NOT NULL DEFAULT 0 CHECK (gpu_seconds_consumed >= 0),
  checkpoint_ref     TEXT,

  artifact_id        TEXT,
  evaluation         TEXT,
  requested_by       TEXT NOT NULL,
  trace_id           TEXT,

  -- C48-13：事件序号、心跳、续跑次数、评测重试、撤销挂账、地域。
  aggregate_sequence INTEGER NOT NULL DEFAULT 0 CHECK (aggregate_sequence >= 0),
  last_heartbeat_at  TEXT,
  resume_count       INTEGER NOT NULL DEFAULT 0 CHECK (resume_count >= 0),
  eval_attempts      INTEGER NOT NULL DEFAULT 0 CHECK (eval_attempts >= 0),
  rights_hold        TEXT NOT NULL DEFAULT '[]',
  home_region        TEXT NOT NULL,

  created_at         TEXT NOT NULL,
  admitted_at        TEXT,
  started_at         TEXT,
  finished_at        TEXT,

  -- §6.1：幂等键的数据库形态。同键同 body 回放，不同 body 走 409。
  CONSTRAINT tj_cmd_uniq UNIQUE (tenant_id, command_id)
);
CREATE INDEX IF NOT EXISTS tj_state_idx
  ON training_job (tenant_id, state, created_at DESC);
CREATE INDEX IF NOT EXISTS tj_active_idx
  ON training_job (state, state_since)
  WHERE state IN ('QUEUED','PREPARING','TRAINING','RESUMING','EVALUATING');

-- ---------- 数据集清单（§5.2） ----------
-- `items_hash` 是「这 812 张图」的可独立复算摘要：只取
-- (artifact_id, content_hash)，排序后 JCS 再 sha256。训练完成后
-- 仅凭 artifact id 无法证明字节没换过。
CREATE TABLE IF NOT EXISTS dataset_manifest (
  dataset_id         TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  job_id             TEXT NOT NULL,
  mode               TEXT NOT NULL CHECK (mode IN
                       ('explicit','derived_from_run','derived_from_artifact_set')),
  source_ref         TEXT,
  items_hash         TEXT NOT NULL CHECK (length(items_hash) = 64),
  item_count         INTEGER NOT NULL CHECK (item_count >= 0),
  excluded_count     INTEGER NOT NULL DEFAULT 0 CHECK (excluded_count >= 0),
  exclusions         TEXT NOT NULL DEFAULT '[]',
  state              TEXT NOT NULL DEFAULT 'DRAFT' CHECK (state IN
                       ('DRAFT','FROZEN','SUPERSEDED')),
  frozen_at          TEXT,
  created_at         TEXT NOT NULL,
  UNIQUE (tenant_id, job_id)
);
CREATE INDEX IF NOT EXISTS dm_job_idx ON dataset_manifest (job_id);

-- 被排除的项单独记录：它们也是证据（§3.3 第 3 条）。
CREATE TABLE IF NOT EXISTS dataset_item (
  tenant_id          TEXT NOT NULL,
  dataset_id         TEXT NOT NULL,
  artifact_id        TEXT NOT NULL,
  artifact_service   TEXT NOT NULL DEFAULT 'S28',
  content_hash       TEXT NOT NULL CHECK (length(content_hash) = 64),

  rights_service     TEXT NOT NULL,
  rights_id          TEXT NOT NULL,
  rights_revision    INTEGER NOT NULL,
  rights_content_hash TEXT NOT NULL CHECK (length(rights_content_hash) = 64),
  -- 这两列 NOT NULL 是 §1.3 第 2 条的结构保证：没有授权的项进不来。
  rights_grant_kind  TEXT NOT NULL,
  rights_state_at_check TEXT NOT NULL,
  rights_checked_at  TEXT NOT NULL,

  ordinal            INTEGER NOT NULL,
  PRIMARY KEY (dataset_id, artifact_id),
  FOREIGN KEY (dataset_id) REFERENCES dataset_manifest (dataset_id)
);
CREATE INDEX IF NOT EXISTS di_rights_idx
  ON dataset_item (rights_service, rights_id);

CREATE TABLE IF NOT EXISTS dataset_exclusion (
  tenant_id          TEXT NOT NULL,
  dataset_id         TEXT NOT NULL,
  artifact_id        TEXT NOT NULL,
  reason             TEXT NOT NULL CHECK (reason IN
                       ('REVOKED','SUSPENDED','EXPIRED','NO_TRAINING_GRANT',
                        'HASH_MISMATCH','MISSING','EXPLICITLY_EXCLUDED')),
  rights_ref         TEXT,
  PRIMARY KEY (dataset_id, artifact_id),
  FOREIGN KEY (dataset_id) REFERENCES dataset_manifest (dataset_id)
);

-- ---------- 兼容性与产物（§5.3） ----------
-- verdict 是训练时刻的判断。`checked_at` 越旧越可疑，读接口必须提示
-- 「当前兼容性问 S24」。
CREATE TABLE IF NOT EXISTS base_model_compatibility (
  compat_id          TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  artifact_id        TEXT NOT NULL,
  base_service       TEXT NOT NULL DEFAULT 'S24',
  base_id            TEXT NOT NULL,
  base_revision      INTEGER NOT NULL CHECK (base_revision >= 1),
  base_content_hash  TEXT NOT NULL CHECK (length(base_content_hash) = 64),
  verdict            TEXT NOT NULL CHECK (verdict IN
                       ('COMPATIBLE','INCOMPATIBLE','UNKNOWN')),
  reason             TEXT,
  checked_by         TEXT NOT NULL DEFAULT 'S48',
  checked_at         TEXT NOT NULL,
  CONSTRAINT bmc_uniq UNIQUE (artifact_id, base_id, base_revision)
);
CREATE INDEX IF NOT EXISTS bmc_base_idx
  ON base_model_compatibility (tenant_id, base_id, base_revision);

-- C48-15：weights_blob_ref 是 S48 桶里的权重对象，storage_ref 是 S28 的
-- artifact id（PUBLISHED 才有）。两列都在，链路才从「训练的权重」到
-- 「注册的制品」都可追溯。
CREATE TABLE IF NOT EXISTS lora_artifact (
  artifact_id        TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  job_id             TEXT NOT NULL,
  adaptation_type    TEXT NOT NULL,
  target_service     TEXT NOT NULL,
  target_id          TEXT NOT NULL,
  target_revision    INTEGER NOT NULL CHECK (target_revision >= 1),

  weights_blob_ref   TEXT NOT NULL,
  storage_ref        TEXT,
  weights_hash       TEXT NOT NULL CHECK (length(weights_hash) = 64),
  size_bytes         INTEGER NOT NULL CHECK (size_bytes >= 0),
  dataset_id         TEXT NOT NULL,
  base_service       TEXT NOT NULL DEFAULT 'S24',
  base_id            TEXT NOT NULL,
  base_revision      INTEGER NOT NULL CHECK (base_revision >= 1),
  base_content_hash  TEXT NOT NULL CHECK (length(base_content_hash) = 64),
  evaluation         TEXT,

  state              TEXT NOT NULL DEFAULT 'CANDIDATE' CHECK (state IN
                       ('CANDIDATE','PUBLISHED','BLOCKED','SUPERSEDED')),
  blocked_reason     TEXT,
  published_at       TEXT,
  created_at         TEXT NOT NULL,
  -- §5.3：同租户相同权重只注册一次；确定性训练复现同一权重时复用。
  CONSTRAINT la_weights_uniq UNIQUE (tenant_id, weights_hash)
);
CREATE INDEX IF NOT EXISTS la_target_idx
  ON lora_artifact (tenant_id, target_service, target_id, state);
CREATE INDEX IF NOT EXISTS la_base_idx
  ON lora_artifact (tenant_id, base_id, base_revision, state);

-- ---------- 训练用量视图（§5.4） ----------
-- **不是账本。** 金额与余额永远以 S23 为准；这里只是本服务上报过的秒数。
CREATE TABLE IF NOT EXISTS training_slot_usage (
  usage_id           TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  job_id             TEXT NOT NULL,
  permit_ref         TEXT NOT NULL,
  gpu_seconds        INTEGER NOT NULL CHECK (gpu_seconds > 0),
  resource_class     TEXT NOT NULL DEFAULT 'gpu_second',
  started_at         TEXT NOT NULL,
  recorded_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tsu_job_idx ON training_slot_usage (job_id);
CREATE INDEX IF NOT EXISTS tsu_tenant_idx
  ON training_slot_usage (tenant_id, recorded_at DESC);

-- ---------- 基座退役投影（§7.3 / F48-02） ----------
-- S24 是基座可用性的权威；这里只记「我收到过它的退役通知」，用来在
-- 训练完成时把 verdict 落成 INCOMPATIBLE。没有它就只能等 S24 回源。
CREATE TABLE IF NOT EXISTS base_retirement (
  tenant_id          TEXT NOT NULL,
  base_service       TEXT NOT NULL,
  base_id            TEXT NOT NULL,
  base_revision      INTEGER NOT NULL,
  reason             TEXT,
  retired_at         TEXT NOT NULL,
  recorded_at        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, base_service, base_id, base_revision)
);

-- ---------- 本地审计留痕（C48-16） ----------
-- 与 S45 同一组动作名。**不替代 S45**：这是本服务自己的追加日志，
-- 没有跨服务上送通道。details 里不放超参全文、数据集内容、权重 URL。
CREATE TABLE IF NOT EXISTS training_audit (
  tenant_id          TEXT NOT NULL,
  audit_id           TEXT NOT NULL,
  job_id             TEXT,
  action             TEXT NOT NULL,
  actor              TEXT,
  details            TEXT NOT NULL,
  recorded_at        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, audit_id)
);
CREATE INDEX IF NOT EXISTS ta_job_idx
  ON training_audit (tenant_id, job_id, recorded_at);
"""
