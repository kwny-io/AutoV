"""S29 自己的 schema。表来源是 `plans/S29-image-production.md` §5.2。

六条结构性事实，它们决定了这张库能证明什么、不能证明什么：

1. **`shot_image_checkpoint` 不是 S08 的接受指针。** 它回答"下游 S30 该拿哪张图
   当首帧"，不回答"这一集被接受了吗"（§2.3）。把两者合并，不是 S30 拿不到图，
   就是 S29 越权推进接受指针——两者都是第二权威。
2. **`image_job_open_uniq` 是 E16 落到数据库里的那一条。** 同一
   `(tenant, shot_lineage_id, node_kind, input_fingerprint)` 同时只能有一个未终结
   job，`WAITING_UNKNOWN` 算未终结。想"再建一个 job 绕过 UNKNOWN 重提"的代码
   路径会撞唯一约束，而不是悄悄成功（§4.3）。
3. **`image_job_repair_uniq` 让同一个 `RepairAction` 只执行一次。** 重复投递会
   撞索引，不是第二次付费编辑（§7.2）。
4. **`kc_artifact_is_s28` 防的是供应商直链。** 候选的 `artifact_ref` 只能指向
   S28；把供应商 URL 塞进来，等于让 S29 成为字节的第二个权威（§5.3）。
5. **6 帧窗口落在 `em_window_max6`。** 应用层校验有 bug 时数据库仍然拒绝
   （§1.4 / C6）。
6. **检查点只允许指向 `READY` / `ACCEPTED` 的候选。** 这是最常见的一类 bug，
   SQLite 的 CHECK 不能跨表，所以用两条触发器兜住（§2.3）。

本仓服务层一律 SQLite：`jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、
`text[]` 存 JSON 数组、`uuid` 存 TEXT，`->>'k'` 用 `json_extract`。
`outbox` / `inbox` / `idempotency_record` 由平台 SDK 建表，这里不重复 DDL；
方案 §5.2 的 `command_receipt` 与 `idempotency_record` 是同一张表，因此不建。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 生产意图 ----------
CREATE TABLE IF NOT EXISTS image_job (
  image_job_id        TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  project_id          TEXT NOT NULL,
  series_id           TEXT NOT NULL,
  episode_id          TEXT NOT NULL,
  branch_id           TEXT NOT NULL,
  shot_ref            TEXT NOT NULL,          -- {service_id,id,revision}
  shot_lineage_id     TEXT NOT NULL,          -- 跨 Run 锚点，**上游传入**（§8.2）
  node_id             TEXT NOT NULL,
  node_epoch_at_create INTEGER NOT NULL,
  execution_fence     INTEGER NOT NULL,
  node_kind           TEXT NOT NULL CHECK (node_kind IN
                        ('first_frame','last_frame','keyframe_mid','reference_still')),

  mode                TEXT NOT NULL CHECK (mode IN ('generate','edit')),
  prompt_intent       TEXT NOT NULL,
  width               INTEGER NOT NULL CHECK (width > 0),
  height              INTEGER NOT NULL CHECK (height > 0),
  identity_constraints TEXT NOT NULL DEFAULT '[]',

  reference_combination_id TEXT NOT NULL,
  source_candidate_id TEXT,
  edit_mask_id        TEXT,
  repair_action_id    TEXT,
  -- S39 下发的策略类，原样透传，S29 **不计算**（§8.3）。
  strategy_class      TEXT,

  input_fingerprint   TEXT NOT NULL,
  request_hash        TEXT NOT NULL,
  idempotency_key     TEXT NOT NULL,

  route_decision_ref  TEXT,                   -- 只存 {id, revision, content_hash}
  -- 调用方下发的 S25 策略引用。方案 §5.2 的 DDL 没有这一列，但
  -- `generation-command.v2` 把它列为必填四键之一，重试/恢复时要原样再发给
  -- S26，所以必须先存下来（CONTRACT_GAPS C29-26）。
  route_policy_ref    TEXT NOT NULL,
  -- `generation-command.v2` 的必填键，S26 逐键查非空。方案 §5.2 的 DDL 漏了它
  -- （CONTRACT_GAPS C29-15）；缺了它每次提交都会被 S26 400。
  variation_nonce     TEXT NOT NULL,
  -- 已签名的 execution-context.v2 原样存储：重试/恢复要再发 S26，而
  -- `generation-command.v2.context` 的 18 个键只能来自平台签名。
  execution_context   TEXT NOT NULL,
  budget_scope_id     TEXT NOT NULL,
  deadline            TEXT NOT NULL,

  provider_job_id     TEXT,                   -- S26 引用，不是 FK
  attempt_id          TEXT,                   -- S26 引用，不是 FK
  business_attempt_no INTEGER NOT NULL DEFAULT 1,
  reconciled          INTEGER NOT NULL DEFAULT 0 CHECK (reconciled IN (0,1)),

  state               TEXT NOT NULL,
  state_seq           INTEGER NOT NULL DEFAULT 0,
  failure_code        TEXT,
  failure_detail      TEXT,

  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  state_entered_at    TEXT NOT NULL,

  CONSTRAINT ij_state_check CHECK (state IN (
    'CREATED','PREPARING','REJECTED_LOCAL','SUBMITTED','WAITING_PROVIDER',
    'WAITING_UNKNOWN','INGESTING','READY','FAILED','RECONCILED_READY',
    'RECONCILED_FAILED','CANCELED')),
  CONSTRAINT ij_edit_needs_mask CHECK (
    mode <> 'edit' OR (source_candidate_id IS NOT NULL AND edit_mask_id IS NOT NULL
                       AND repair_action_id IS NOT NULL)),
  CONSTRAINT ij_generate_no_mask CHECK (
    mode <> 'generate' OR (source_candidate_id IS NULL AND edit_mask_id IS NULL)),
  CONSTRAINT ij_tenant_scoped_idem UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT ij_prompt_not_blank CHECK (length(trim(prompt_intent)) > 0),
  CONSTRAINT ij_fingerprint_len CHECK (length(input_fingerprint) = 64)
);

-- 同一 (shot_lineage, node_kind, fingerprint) 只能有一个未终结 job（§4.3）。
-- RECONCILED_READY 也必须在排除列表里：对账成功是一个终局结论，否则那一格
-- 会永远占着业务幂等键，之后同输入的新 job 再也建不出来（CONTRACT_GAPS C29-3）。
CREATE UNIQUE INDEX IF NOT EXISTS image_job_open_uniq
  ON image_job (tenant_id, shot_lineage_id, node_kind, input_fingerprint)
  WHERE state NOT IN ('READY','RECONCILED_READY','REJECTED_LOCAL','FAILED',
                      'RECONCILED_FAILED','CANCELED');

-- 同一 repair_action 只能被执行一次（E12 的变体 / T29-18）。
CREATE UNIQUE INDEX IF NOT EXISTS image_job_repair_uniq
  ON image_job (tenant_id, repair_action_id)
  WHERE repair_action_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS image_job_shot_idx
  ON image_job (tenant_id, shot_lineage_id, created_at DESC);
CREATE INDEX IF NOT EXISTS image_job_state_idx
  ON image_job (state, state_entered_at)
  WHERE state IN ('PREPARING','SUBMITTED','WAITING_UNKNOWN','INGESTING');
CREATE INDEX IF NOT EXISTS image_job_provider_idx
  ON image_job (provider_job_id) WHERE provider_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS image_job_ref_idx
  ON image_job (tenant_id, json_extract(shot_ref, '$.id'));

-- ---------- 参考组合（不可变，顺序有语义） ----------
CREATE TABLE IF NOT EXISTS reference_combination (
  reference_combination_id TEXT PRIMARY KEY,
  tenant_id                TEXT NOT NULL,
  content_hash             TEXT NOT NULL,
  -- `references` 是 SQLite 保留字，必须加引号；下面所有语句同理。
  "references"             TEXT NOT NULL,      -- 有序 [{ordinal,artifact_id,sha256,role}]
  identity_constraints     TEXT NOT NULL,      -- 排序后参与 hash
  created_at               TEXT NOT NULL,
  CONSTRAINT rc_hash_uniq UNIQUE (tenant_id, content_hash),
  CONSTRAINT rc_refs_nonempty CHECK (json_array_length("references") > 0),
  CONSTRAINT rc_hash_len CHECK (length(content_hash) = 64)
);

CREATE INDEX IF NOT EXISTS rc_hash_idx
  ON reference_combination (tenant_id, content_hash);

-- ---------- 候选（不可变） ----------
CREATE TABLE IF NOT EXISTS keyframe_candidate (
  candidate_id             TEXT PRIMARY KEY,
  tenant_id                TEXT NOT NULL,
  image_job_id             TEXT NOT NULL REFERENCES image_job (image_job_id),
  shot_lineage_id          TEXT NOT NULL,
  node_kind                TEXT NOT NULL,
  artifact_ref             TEXT NOT NULL,      -- 必须 service_id="S28"
  reference_combination_id TEXT NOT NULL
                             REFERENCES reference_combination (reference_combination_id),
  source_candidate_id      TEXT REFERENCES keyframe_candidate (candidate_id),
  edit_mask_id             TEXT,
  provider_job_id          TEXT NOT NULL,
  attempt_id               TEXT NOT NULL,
  observed_output          TEXT NOT NULL,      -- {width,height,format,bytes}
  strategy_class           TEXT,
  repair_action_id         TEXT,
  requires_recheck         INTEGER NOT NULL DEFAULT 0
                             CHECK (requires_recheck IN (0,1)),
  state                    TEXT NOT NULL CHECK (state IN
                             ('READY','ACCEPTED','REJECTED')),
  created_at               TEXT NOT NULL,
  CONSTRAINT kc_artifact_is_s28
    CHECK (json_extract(artifact_ref, '$.service_id') = 'S28'),
  CONSTRAINT kc_artifact_hash_len
    CHECK (length(json_extract(artifact_ref, '$.content_hash')) = 64)
);

CREATE UNIQUE INDEX IF NOT EXISTS kc_artifact_uniq
  ON keyframe_candidate (tenant_id, json_extract(artifact_ref, '$.id'),
                         json_extract(artifact_ref, '$.content_hash'));
CREATE INDEX IF NOT EXISTS kc_shot_idx
  ON keyframe_candidate (tenant_id, shot_lineage_id, node_kind, created_at DESC);
CREATE INDEX IF NOT EXISTS kc_job_idx ON keyframe_candidate (image_job_id);

-- ---------- 编辑 mask（不可变，绑定源候选） ----------
CREATE TABLE IF NOT EXISTS edit_mask (
  edit_mask_id        TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  source_candidate_id TEXT NOT NULL
                        REFERENCES keyframe_candidate (candidate_id),
  artifact_ref        TEXT NOT NULL,
  mask_artifact_id    TEXT NOT NULL,
  mask_sha256         TEXT NOT NULL,
  geometry_digest     TEXT NOT NULL,
  instruction         TEXT NOT NULL,
  window_start_frame  INTEGER NOT NULL CHECK (window_start_frame >= 0),
  window_end_frame    INTEGER NOT NULL,
  window_fps          INTEGER NOT NULL CHECK (window_fps > 0),
  constraint_6frames  INTEGER NOT NULL DEFAULT 1
                        CHECK (constraint_6frames IN (0,1)),
  created_at          TEXT NOT NULL,
  CONSTRAINT em_window_order CHECK (window_end_frame >= window_start_frame),
  -- §1.4 / C6：6 帧上限落在数据库层，应用层 bug 也挡得住。
  CONSTRAINT em_window_max6 CHECK (window_end_frame - window_start_frame + 1 <= 6),
  CONSTRAINT em_same_tenant UNIQUE (tenant_id, edit_mask_id),
  CONSTRAINT em_artifact_is_s28
    CHECK (json_extract(artifact_ref, '$.service_id') = 'S28'),
  CONSTRAINT em_sha_len CHECK (length(mask_sha256) = 64),
  CONSTRAINT em_instruction_not_blank CHECK (length(trim(instruction)) > 0)
);

-- §2.5："mask 不能跨候选复用"。一枚 mask artifact 只绑定一张源候选
-- （CONTRACT_GAPS C29-5：§5.2 的 DDL 没写这条，按 §2.5 补）。
CREATE UNIQUE INDEX IF NOT EXISTS em_mask_single_binding
  ON edit_mask (tenant_id, mask_artifact_id, mask_sha256);

-- ---------- 镜头图像生产检查点（可变，CAS） ----------
CREATE TABLE IF NOT EXISTS shot_image_checkpoint (
  tenant_id          TEXT NOT NULL,
  shot_lineage_id    TEXT NOT NULL,
  node_kind          TEXT NOT NULL CHECK (node_kind IN
                       ('first_frame','last_frame','keyframe_mid','reference_still')),
  candidate_id       TEXT NOT NULL
                       REFERENCES keyframe_candidate (candidate_id),
  checkpoint_seq     INTEGER NOT NULL CHECK (checkpoint_seq >= 1),
  expected_revision  INTEGER NOT NULL DEFAULT 0 CHECK (expected_revision >= 0),
  updated_by_job     TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  PRIMARY KEY (tenant_id, shot_lineage_id, node_kind)
);

-- §2.3：检查点只允许指向 READY / ACCEPTED 的候选。CHECK 不能跨表，
-- 用触发器在写入时查一次。
CREATE TRIGGER IF NOT EXISTS sic_candidate_ready_ins
BEFORE INSERT ON shot_image_checkpoint
WHEN (SELECT state FROM keyframe_candidate
      WHERE candidate_id = NEW.candidate_id) NOT IN ('READY','ACCEPTED')
BEGIN
  SELECT RAISE(ABORT, 'checkpoint_candidate_not_ready');
END;

CREATE TRIGGER IF NOT EXISTS sic_candidate_ready_upd
BEFORE UPDATE OF candidate_id ON shot_image_checkpoint
WHEN (SELECT state FROM keyframe_candidate
      WHERE candidate_id = NEW.candidate_id) NOT IN ('READY','ACCEPTED')
BEGIN
  SELECT RAISE(ABORT, 'checkpoint_candidate_not_ready');
END;

-- ---------- 意图台账（只追加，不是费用台账） ----------
CREATE TABLE IF NOT EXISTS image_intent_ledger (
  entry_id         TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  image_job_id     TEXT NOT NULL,
  attempt_id       TEXT,
  provider_job_id  TEXT,
  shot_lineage_id  TEXT NOT NULL,
  node_kind        TEXT NOT NULL,
  action           TEXT NOT NULL CHECK (action IN
                     ('generate','keyframe_edit','regenerate_from_intent')),
  strategy_class   TEXT,
  repair_action_id TEXT,
  run_id           TEXT NOT NULL,   -- 仅审计归因，不进任何上限键（§8.1）
  node_epoch       INTEGER NOT NULL,
  manifest_id      TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS iil_shot_idx
  ON image_intent_ledger (tenant_id, shot_lineage_id, created_at DESC);

-- ---------- 状态历史（只追加） ----------
CREATE TABLE IF NOT EXISTS job_transition (
  image_job_id  TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  from_state    TEXT,
  to_state      TEXT NOT NULL,
  reason_code   TEXT,
  detail        TEXT,
  at            TEXT NOT NULL,
  PRIMARY KEY (image_job_id, seq)
);
"""

# ---------------------------------------------------------------- 词汇表

JOB_STATES = frozenset({
    "CREATED", "PREPARING", "REJECTED_LOCAL", "SUBMITTED", "WAITING_PROVIDER",
    "WAITING_UNKNOWN", "INGESTING", "READY", "FAILED", "RECONCILED_READY",
    "RECONCILED_FAILED", "CANCELED",
})

# §3.2。`RECONCILED_READY` 不在方案的终态列表里，但 §3.1 / §4.6 把它当成功
# 终局；这里按终态实现（CONTRACT_GAPS C29-2）。
TERMINAL_STATES = frozenset({
    "READY", "RECONCILED_READY", "REJECTED_LOCAL", "FAILED",
    "RECONCILED_FAILED", "CANCELED",
})

RETRYABLE_STATES = frozenset({"FAILED", "RECONCILED_FAILED"})

# §3.1 表。`None` 是创建（无前置状态）。
TRANSITIONS = {
    None: frozenset({"CREATED"}),
    "CREATED": frozenset({"PREPARING", "CANCELED"}),
    "PREPARING": frozenset({"REJECTED_LOCAL", "SUBMITTED", "CANCELED"}),
    "SUBMITTED": frozenset({"WAITING_PROVIDER", "FAILED", "WAITING_UNKNOWN",
                            "CANCELED"}),
    "WAITING_PROVIDER": frozenset({"INGESTING", "FAILED", "WAITING_UNKNOWN",
                                   "CANCELED"}),
    "INGESTING": frozenset({"INGESTING", "READY", "FAILED", "CANCELED"}),
    # §4.3：UNKNOWN 期间**不发起 cancel**，也不是可取消状态。因此这里没有
    # CANCELED（CONTRACT_GAPS C29-4）。
    "WAITING_UNKNOWN": frozenset({"RECONCILED_READY", "RECONCILED_FAILED"}),
    "READY": frozenset(),
    "RECONCILED_READY": frozenset(),
    "REJECTED_LOCAL": frozenset(),
    "FAILED": frozenset(),
    "RECONCILED_FAILED": frozenset(),
    "CANCELED": frozenset(),
}

NODE_KINDS = frozenset({
    "first_frame", "last_frame", "keyframe_mid", "reference_still",
})

REFERENCE_ROLES = frozenset({
    "character", "scene", "style", "first_frame", "last_frame",
    "video_reference", "audio_reference", "mask",
})

# C3：图像模态不接受音频/视频参考。S29 在本地拒，不选边。
ROLES_NOT_APPLICABLE_FOR_IMAGE = frozenset({
    "audio_reference", "video_reference",
})

MAX_EDIT_WINDOW_FRAMES = 6

ID_PREFIX = {
    "job": "ij_",
    "combo": "rfc_",
    "mask": "em_",
    "candidate": "kfc_",
    "ledger": "iil_",
}

# 方案写"6 帧窗口"，bash 之外没有任何地方需要帧率；这里只作为提示常量。
FRAME_TOLERANCE = 0


CONTRACT_GAPS = [
    # --- 方案内部冲突（实现时按一处，另一处登记） ---
    {"id": "C29-1", "kind": "conflict",
     "summary": "§6.1 校验第 6 条说参考 sha256 不符就『失败即返回，不建 job』，"
                "但 §4.6 F2 / T29-04 要求状态 `REJECTED_LOCAL"
                "(REFERENCE_HASH_MISMATCH)`",
     "handling": "建 job 并立刻转 REJECTED_LOCAL；不产生 attempt、不调 S26。"
                 "两种读法都满足『不产生费用』，但只有留下 job 才能让 T29-04 "
                 "的状态断言成立。"},
    {"id": "C29-2", "kind": "conflict",
     "summary": "§3.2 的终态列表漏了 `RECONCILED_READY`，而 §3.1 与 §4.6 "
                "把它当对账成功后的终局",
     "handling": "按终态实现（TERMINAL_STATES 含它）。否则『任意非终态→"
                 "CANCELED』会让对账成功的 job 还能被取消。"},
    {"id": "C29-3", "kind": "conflict",
     "summary": "§4.3 的 `image_job_open_uniq` 排除列表没有 "
                "`RECONCILED_READY`；对账成功后那一格会永远占着业务幂等键",
     "handling": "把 `RECONCILED_READY` 加进排除列表，与 READY 同义。"},
    {"id": "C29-4", "kind": "conflict",
     "summary": "§3.1 说『任意非终态 → CANCELED』，§3.2/§4.3 又说 "
                "`WAITING_UNKNOWN` 不是可取消状态",
     "handling": "WAITING_UNKNOWN 的 TRANSITIONS 不含 CANCELED；cancel 接口"
                 "返回 409 MUST_RECONCILE_FIRST，不向 S26 发取消。"},
    {"id": "C29-5", "kind": "gap",
     "summary": "§5.2 的 `edit_mask` 只有 `em_same_tenant UNIQUE(tenant, "
                "edit_mask_id)`，而 §2.5 要求『mask 不能跨候选复用』",
     "handling": "加 `em_mask_single_binding UNIQUE(tenant_id, "
                 "mask_artifact_id, mask_sha256)`：一枚 mask artifact 只绑定"
                 "一张源候选；重复绑定 409 MASK_ALREADY_BOUND。"},
    {"id": "C29-6", "kind": "gap",
     "summary": "§5.2 的 `image_job` 没有任何列能存已签名的 execution-context，"
                "但提交/重试都要原样回传给 S26",
     "handling": "加 `image_job.execution_context`（TEXT NOT NULL）。服务端"
                 "补出来的 context 每个值都是伪造签名，不能现拼。"},
    {"id": "C29-7", "kind": "gap",
     "summary": "`edit_mask` 的 DDL 没有帧率列，而 §1.4 要求『shot 帧率已知』"
                "才能解释 6 帧窗口",
     "handling": "加 `window_fps`（>0）随 mask 一起冻结；入参缺 fps 即 422 "
                 "KEYFRAME_EDIT_FPS_UNKNOWN。"},
    {"id": "C29-8", "kind": "gap",
     "summary": "`image_intent_ledger.run_id` 是 NOT NULL，但 "
                "execution-context.v2 没有 `run_id` 字段（只有 manifest_ref / "
                "node_epoch / execution_fence）",
     "handling": "有可选 `context.run_id` 就用它；没有就存空串，"
                 "不拿 trace_id/manifest_id 冒充 run_id。"},
    {"id": "C29-9", "kind": "gap",
     "summary": "§6.1 的 edit 请求体没有 `strategy_class`，但 §8.3 要求把 "
                "S39 下发的值原样写进台账",
     "handling": "接受可选顶层 `strategy_class` 并原样存储；S29 不计算、"
                 "不推导（计算规则归 S39）。"},
    {"id": "C29-10", "kind": "conflict",
     "summary": "`production.image.ready` 的 schema 没有 `requires_recheck`，"
                "§4.4 正文却要求 repaired_from != null 时必须带",
     "handling": "按 schema（additionalProperties=false）不发该键；修复语义由 "
                 "`repaired_from != null` 表达，候选表的 `requires_recheck` "
                 "列保留供 GET 读取。等 owner 补 schema（PIR-1）。"},
    {"id": "C29-11", "kind": "conflict",
     "summary": "§6.2 的 GET 响应有 `provider_job_state`，但 DDL 没有存它的列；"
                "真正的权威值只有 S26 知道",
     "handling": "GET 不额外请求 S26，按本地状态给出确定映射"
                 "（WAITING_UNKNOWN→SUBMISSION_UNKNOWN 等），并在响应里同时"
                 "给出 `provider_state_source: \"local_map\"`。"},
    {"id": "C29-12", "kind": "conflict",
     "summary": "`reference_combination.rc_refs_nonempty` 要求 references 非空，"
                "但纯文生图（无参考）是合法需求（对照 C5 的『无图』问题）",
     "handling": "generate 要求至少一个参考；零参考 422 REFERENCES_REQUIRED。"
                 "不发明占位参考图。"},
    {"id": "C29-13", "kind": "conflict",
     "summary": "§1.4 写 `production.image.keyframe-refresh-failed`，§6.4 的"
                "事件表写 `image.keyframe-refresh-failed`",
     "handling": "按 §6.4 的表（`image.keyframe-refresh-failed`）发；两个名字"
                 "都未在 event-topics.v2.json 登记（C2）。"},
    {"id": "C29-15", "kind": "gap",
     "summary": "§5.2 的 `image_job` 没有 `variation_nonce` 列，但 "
                "`generation-command.v2` 把它列为 11 个必填键之一，S26 逐键查",
     "handling": "建 job 时生成一次并冻结；重试复用同一个 nonce（重试是同"
                 "意图的另一次尝试，换 nonce 等于换输入）。"},
    {"id": "C29-16", "kind": "conflict",
     "summary": "§3.2/§4.6 让 `WAITING_UNKNOWN` 只能经对账出口，§6.3 又允许 "
                "`POST /image-jobs/{id}:cancel` 取消任意非终态",
     "handling": "与 C29-4 同一处理：UNKNOWN 期间取消返回 409 "
                 "MUST_RECONCILE_FIRST，且不向 S26 发 cancel。"},
    {"id": "C29-17", "kind": "gap",
     "summary": "§4.5 要求 URL 过期时『先向 S26 查询新 URL』，但 S26 目前"
                "没有公开的浏览器可调用的 URL 刷新端点；URL 只在最后一次观测"
                "（`output_snapshot`）里带出",
     "handling": "`deps.build_artifact_importer` 先按 S26 的 `GET "
                "/generation-jobs/{id}` 取 `output` 里的 URL 重试一次；拿不到"
                "新 URL 就停在 INGESTING 并暴露 `awaiting_fresh_url=true`，不"
                "重生成、不自造 URL。"},
    {"id": "C29-18", "kind": "gap",
     "summary": "§6.1 的 `references[]` 每项带 `ordinal`，但 "
                "`generation-command.v2` 的 image `input.references.items` 是 "
                "`additionalProperties: false` 且只允许 artifact_id/sha256/role",
     "handling": "`ordinal` 只在 S29 内部用来排序与求 hash；发给 S26 前剥掉。"
                 "顺序语义靠数组顺序表达，不靠一个 S26 不接受的键。"},
    {"id": "C29-19", "kind": "gap",
     "summary": "§6.1 校验第 4 条要查 `shot_ref.revision` 在 S06 存在且属于本"
                "租户，但 S29 的 `depends_on` 是 {S26,S28,S10}，没有 S06",
     "handling": "只校验 `shot_ref` 形状（service_id/id/revision 齐且类型对）；"
                 "存在性/归属校验无法在声明的依赖内完成，登记而不发明一个未"
                 "声明的跨服务调用。"},
    {"id": "C29-20", "kind": "gap",
     "summary": "§6.1 校验第 5 条『node_kind 与 shot_ref 时间结构一致』需要"
                "镜头的时间结构与帧区间，S29 不持有（S06/S35 才有）",
     "handling": "只校验 `node_kind` 在闭集内；时间结构一致性留给 S35 的技术"
                 "检查与 S39 的计划生成。"},
    {"id": "C29-21", "kind": "conflict",
     "summary": "§3.2 的终态集合与 `image_job_open_uniq` 排除列表不一致"
                "（READY 在终态里，RECONCILED_READY 不在排除列表里）",
     "handling": "同 C29-3：以『终态即释放业务键』为准，两处对齐。"},
    {"id": "C29-22", "kind": "gap",
     "summary": "§6.1 的 edit 请求体有 `instruction`（『把左手手指数量改成五根』），"
                "但 `generation-command.v2` 的 image `input` 是 "
                "`additionalProperties: false`，六个允许键里没有编辑指令的位置",
     "handling": "accept 可选 `instruction` 存进 `edit_mask.instruction` 作为"
                 "本地审计证据；真正发给 S26 的文字只有 `prompt_intent`。"
                 "缺省取 `prompt_intent`，不另拼一份可能与它不一致的文本。"},
    {"id": "C29-23", "kind": "conflict",
     "summary": "§6.3 说 `:retry`『新建 job（新 `image_job_id`）』，但 §7.2 的 "
                "`image_job_repair_uniq` 不允许同一 `repair_action_id` 建第二个"
                " job——两条同时成立就不可能",
     "handling": "**原地重试**：bump `business_attempt_no`、换 "
                 "`variation_nonce`、清失败码、`FAILED -> PREPARING`，再走一次"
                 " S26（attempt_id 带新序号，S26 侧是新 attempt）。业务幂等键与"
                 "修复幂等键都保持成立。"},
    {"id": "C29-24", "kind": "gap",
     "summary": "§6.1 的请求示例带 `budget_scope_id`，但校验九步里没有它；"
                "`image_job.budget_scope_id` 是 `NOT NULL`",
     "handling": "按必填处理：缺失/空白 → 422 `INVALID_BUDGET_SCOPE`。"
                 "不默认成 `\"default\"`——那会让所有漏传的租户共用一个预算桶，"
                 "而预算归属是 S23 的决定，S29 不能替它造一个。"},
    {"id": "C29-25", "kind": "conflict",
     "summary": "§4.6 F5『报价过期 → 停在 PREPARING』与 §3.1 状态表冲突："
                "`PREPARING` 的转移集合里没有它自己，写一个自环就违表",
     "handling": "不写状态转移，只发一条 `from == to` 的 "
                 "`image.job.state-changed`（`reason_code=QUOTE_EXPIRED`），"
                 "`state_seq` 不变。重取到的报价必须是新的（`quote_valid_to` "
                 "未过期），否则 503，绝不按过期价提交。"},
    {"id": "C29-26", "kind": "gap",
     "summary": "§5.2 的 `image_job` DDL 只有 `route_decision_ref`，没有 "
                "`route_policy_ref` 列；但 `generation-command.v2` 把 "
                "`route_policy_ref` 列为 11 个必填键之一（四键、"
                "`additionalProperties: false`），而重试/重启恢复要重新组装命令",
     "handling": "加 `image_job.route_policy_ref TEXT NOT NULL`，建 job 时把"
                 "调用方下发的那份（已校验为完整四键）原样存下，"
                 "`_generation_command` 从列里读。它与 `route_decision_ref` "
                 "不是一回事：后者是 S25 派单时的决定，前者是调用方指定的策略。"
                 "不存的后果是每次重试都拼不出合法命令，只能重路由一次——"
                 "那等于让 S29 替 S25 记一份选型。"},
    {"id": "C29-27", "kind": "gap",
     "summary": "§4.5 / T29-15 要求『S28 归档返回 hash 不符 → "
                "FAILED(ARTIFACT_HASH_MISMATCH)』，但 S28 在 HASH_MISMATCH 路径上"
                "只更新自己的 import 行并返回 422，**不发任何事件**（只有发布"
                "成功才发 `artifact.ingested`）",
     "handling": "把 `_request_import` 收到的 422 `HASH_MISMATCH` 当作"
                 "F12 的检测点：打 `FAILED(ARTIFACT_HASH_MISMATCH)` 并**不重试**。"
                 "其余导入失败（`URL_EXPIRED` / 5xx / 不可达）仍然只标注、"
                 "不改状态——它们说的是『还没取到字节』，不是『字节不对』。"},
    {"id": "C29-14", "kind": "needs_owner",
     "summary": "`reference_still` 的候选优先级规则未定（§11 U3）；§4.1 只说"
                "『指向更低 priority 的候选』才推进",
     "handling": "当前按 created_at 后到者优先；不引入第二个 priority 列，"
                 "等导演策略给出规则。"},
]
