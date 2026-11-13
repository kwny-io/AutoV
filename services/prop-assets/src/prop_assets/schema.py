"""S14 自己的 schema。表来源是 `plans/S14-prop-assets.md` §2。

四条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **`prop_revision` 没有 `holder` / `current_state` / `location` 列。**
   持有者、位置、"现在是不是破的"都是 S16 的正史事实（§0、§1.1、§2.7）。
   方案 §1.1 把话说死了：S14 若也存"当前 broken"，这个字段要么由 S14 消费
   S16 事件来改（→ 第二正史 + 自己的状态机），要么永远错，**没有第三种选择**。
   所以这里连列都不建，§2.7 的八张表一张都不建。
2. **`variant_axes` / `variant_manifest` 是"能力"不是"事实"。**
   它们回答"这一版能画出几种样子"，不回答"现在是哪一种"（§1.1）。
3. **`text_fingerprint` 有两个作用域，语义不同。** `prop` 上的是"当前 canonical
   的文字指纹"，`prop_revision` 上的是"本版文字指纹"。§4.2 第 3 步拿新版本
   与 `prop.text_fingerprint` 比，判断的是"canonical 的文字变了没有"（C14-9）。
4. **`canonical_pointer` 的 partial unique index 是"一个道具只有一个当前
   canonical"的数据库形态**（§2.5 `prop_canonical_one`）。supersede 不删旧行，
   所以历史 canonical 可回查——这是"canonical ≠ 最新"（§1.5）能审计的前提。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串
（`"%Y-%m-%dT%H:%M:%S.%f"[:-3]+"Z"`）、`boolean` 存 INTEGER 0/1。
主键/唯一约束全部含 `tenant_id`（E28）。
"""

ID_PREFIX = {"prop": "prp_", "candidate": "cand_"}

# 本轮**不解决**的缺口。contracts/asset.prop-revision-accepted.v2.schema.json
# 里已有 PRP-1…PRP-6 六条 `x-conflicts` 与 6 条 `x-needs-owner`，那些是待 owner
# 决定的事项；按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要自行选边」，
# 本服务按方案散文实现，把偏差记在这里，不改契约、不代填决定。
CONTRACT_GAPS = [
    # --- 方案 §10 自己登记的冲突 ---
    {"id": "C14-1", "kind": "conflict",
     "summary": "service.yaml / openapi 只声明 POST /props/{id}/revisions 与 "
                "GET /props/{id}/revisions/{revision}；§5 却拆成 candidates / "
                "accept / variant / text / required-variants 一组命令",
     "handling": "按方案 §5 实现，不改契约。契约面的扩展等 owner 决定。"},
    {"id": "C14-2", "kind": "needs_owner",
     "summary": "docs/10 说 S16 持有道具当前持有者，但没有任何机制告诉 S14 "
                "「S16 需要哪些视觉变体」；§2.3 的 required_variant 是方案新增概念",
     "handling": "实现 required_variant 表与同步登记接口 "
                 "POST /props/{id}/required-variants；登记方到底是谁（S09 / S16 "
                 "／S02）未定（§11.2 第 1 条），本服务只记录 required_by 字符串。"},
    {"id": "C14-3", "kind": "needs_owner",
     "summary": "道具与服装配饰（S12 accessories）存在双重定义（§1.2），"
                "docs/10 未裁决",
     "handling": "S14 与 S12 各持一份引用，**不自动同步**；同一枚戒指在两处"
                 "各有定义这件事留在两个服务的数据里，不互相写库。"
                 "双重定义本身需裁决。"},
    {"id": "C14-4", "kind": "gap",
     "summary": "contracts/event-topics.v2.json 没有 "
                "continuity.required_variant.registered 这类事件，S09 如何向 "
                "S14 登记需求未定义",
     "handling": "只提供同步命令接口；**不订阅任何事件**（也刻意不订阅 S16，"
                 "§5.3：订阅会让资产随剧情自动变，无法审计）。"},
    {"id": "C14-5", "kind": "conflict",
     "summary": "docs/10 的质量门「角色持用可行性」需要 S11 的体型，但 S14 的 "
                "depends_on 只有 [S28,S01]",
     "handling": "S14 只读 QC 结论（quality_gate 里带回来的 "
                 "holder_feasibility），**绝不自己判**（§1.4、§9 第 7 条）。"},
    {"id": "C14-6", "kind": "needs_owner",
     "summary": "docs/10 未提敏感文字/脱敏；方案 §1.3 新增 sensitive 语义，而 "
                "S01 的权限注册表里没有对应的读取权限键（本仓只有 project.get）",
     "handling": "敏感明文只存 payload；派生表 text_plain 恒为 NULL。特权判定"
                 "取平台签名注入的 X-Principal-Scopes（含 "
                 "`prop.text.sensitive_read`），**绝不读请求体里的任何 "
                 "is_admin/redact=false**。权限键待 S01 注册。"},
    {"id": "C14-7", "kind": "conflict",
     "summary": "event-topics.v2.json 只有 asset.prop.revision.accepted 一条；"
                "没有「文字变更」独立事件，也没有 canonical 变更事件（PRP-4）",
     "handling": "文字变更用 text_changed / text_fingerprint 字段承载。"
                 "`:set-canonical` 移动指针时**不发事件**（方案如此），"
                 "按事件缓存 canonical 的消费方必须回读 "
                 "GET /props/{id}/canonical。"},
    {"id": "C14-8", "kind": "needs_owner",
     "summary": "docs/10 的道具表未区分 hero_prop/story_prop/set_dressing；"
                "prop_kind 与 story_critical 是方案新增（§11.2 第 4 条还问要不要 "
                "单独的 weapon）",
     "handling": "按方案 §2.1 的五个 CHECK 值实现，不加 weapon；契约的 enum "
                 "暂与 DDL 一致。要不要 weapon、是否需要第六个值待 owner 裁决"
                 "（§11.2 第 4 条）。"},

    # --- 实现时新发现的 ---
    {"id": "C14-9", "kind": "conflict",
     "summary": "§4.2 第 3 步拿 new_fp 比 prop.text_fingerprint（canonical 的），"
                "而 §2.1 定义 text_fingerprint = sha256(canonical_text)（单数），"
                "§2.2 的 text_details 却是数组；NULL 语义也没写（PRP-3）",
     "handling": "按方案伪代码与 **prop.text_fingerprint** 比；比较用 "
                 "IS DISTINCT FROM 语义（None→非 None 与反向都算**变更**，避免 "
                 "「给钥匙加刻字」绕过人审）。单面文字指纹 = sha256(原文)；"
                 "多面按 surface 排序后 JCS 再 hash（算法写死在 "
                 "service.text_fingerprint，可复算）。"},
    {"id": "C14-10", "kind": "conflict",
     "summary": "§2.2 的 quality_gate_ref 是 NOT NULL，而 S14 全文七处说的 "
                "「S33/S34 的 QC」其实是音效与音乐服务；OCR / "
                "HOLDER_FEASIBILITY_FAIL / VARIANT_INCONSISTENT 三个结论在质检"
                "方案里都没有登记（PRP-6）",
     "handling": "只读结论，不跑 OCR。结论以可选的内联 `quality_gate` 文档随"
                 "候选命令传入（形状：{text_ocr:[{surface,text_hash|text}], "
                 "holder_feasibility, variant_consistency}）。读不到 OCR 结论时 "
                 "prop_check.text_ocr_pass = null，story_critical 且存在 "
                 "must_be_readable 面的道具设 canonical 会被 422 "
                 "TEXT_OCR_INCOMPLETE 挡住——**不用 true 兜底**。"},
    {"id": "C14-11", "kind": "conflict",
     "summary": "§2.2 的 pr_hash_uniq 是 (tenant_id, content_hash) 租户级唯一，"
                "于是两个不同道具若内容完全相同会互相冲突，而 F11 只描述了"
                "「同内容重复接受」",
     "handling": "按方案建租户级唯一索引；content_hash 不含 prop_id（纯内容），"
                 "所以跨道具重复内容会 409 DUPLICATE_PROP_CONTENT。若 owner 要"
                 "的是按道具去重，索引要加 prop_id。"},
    {"id": "C14-12", "kind": "gap",
     "summary": "§5.1 列了 POST /props/{id}:merge，但全文没有它的命令体和语义"
                "（合并后 source 的 revision / canonical / 绑定怎么处理未定义）",
     "handling": "**不实现该路由**（F16 的同一思路：没有路由是最强的边界声明）。"
                 "按方案 §9 不猜语义；需要时由 owner 补契约。"},
    {"id": "C14-13", "kind": "conflict",
     "summary": "§5.4 的错误码表漏了 E24 点名要 S14 拒的两个 QC 结论码"
                "（HOLDER_FEASIBILITY_FAIL / VARIANT_INCONSISTENT），"
                "TEXT_OCR_INCOMPLETE 只出现在 §4.2 第 4 步、不在汇总表里",
     "handling": "三个码都实现，422 非重试。同类的「表里没有但语义上必须有」还有五个，"
                "一并登记：503 DEPENDENCY_UNAVAILABLE 的三种具体来源"
                "（ARTIFACT_SOURCE_UNAVAILABLE / AUTHORIZER_UNAVAILABLE / "
                "QUALITY_GATE_UNAVAILABLE，未配置上游时用，不能都叫 "
                "DEPENDENCY_UNAVAILABLE——运维要能一眼看出缺哪个环境变量）；"
                "409 INVALID_STATUS_TRANSITION（§3.2 要拒绝但表里没有）；"
                "409 COMMAND_IN_FLIGHT（幂等 SDK 语义，表里没有）。"
                "这些是「表不全」，不是「表错了」，契约的错误码 enum 待补。"},
    {"id": "C14-14", "kind": "needs_owner",
     "summary": "§4.3 有 GET /props:lookup?series_scope=&alias=，但 §2.1 的 DDL "
                "没有 alias 列",
     "handling": "alias 按 display_name 精确匹配（display_name 在 "
                 "(tenant_id, series_scope) 内唯一，正好是查找键）。没有模糊"
                 "匹配、没有第二张别名表。契约要不要单列 alias 列，待 owner 定。"},
    {"id": "C14-15", "kind": "needs_owner",
     "summary": "§5.4 有 429 WRITE_BACKPRESSURE，但方案没写它在什么条件下触发",
     "handling": "按道具的 PENDING 候选数封顶（默认 64，构造参数可调）。"
                 "这只是防止候选无限堆积的本地背压，不是业务配额——配额归 S23。"
                 "64 这个阈值由本实现选定，真正的触发条件待 owner 决定。"},
    {"id": "C14-16", "kind": "conflict",
     "summary": "§2.6 给的 idempotency_record / outbox DDL 与平台 SDK "
                "（idempotency_sdk / event_sdk）的实际列不同：没有 state / "
                "response_code / headers / partition_key / aggregate_sequence",
     "handling": "用平台 SDK 建表（全仓一致）。幂等语义不变："
                 "(tenant_id, command_id) + JCS request_hash，同键不同 body 409；"
                 "partition key 的语义由 subject/aggregate_id 承载。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 道具头（§2.1） ----------
-- `canonical_revision` 与 `current_revision` 分开：canonical ≠ 最新（§1.5）。
-- `story_critical` 是门强度，不是类别：一个 story_prop 完全可以是
-- story_critical=false，这不算矛盾（事件契约里已说明这一点）。
CREATE TABLE IF NOT EXISTS prop (
  tenant_id          TEXT NOT NULL,
  prop_id            TEXT NOT NULL,
  project_id         TEXT NOT NULL,
  series_scope       TEXT NOT NULL,
  display_name       TEXT NOT NULL,
  prop_kind          TEXT NOT NULL CHECK (prop_kind IN (
                       'hero_prop','story_prop','set_dressing',
                       'background_prop','wearable_prop')),
  story_critical     INTEGER NOT NULL DEFAULT 0 CHECK (story_critical IN (0,1)),
  status             TEXT NOT NULL CHECK (status IN
                       ('active','deprecated','tombstoned')),
  canonical_revision INTEGER,
  current_revision   INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
  -- canonical 那一版的文字指纹。NULL 表示 canonical 版本没有文字，或者还没有
  -- canonical。两者在 §4.2 第 3 步的比较里都要当成"与有文字不同"，
  -- 所以比较用 IS DISTINCT FROM 语义（C14-9）。
  text_fingerprint   TEXT,
  created_by         TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  PRIMARY KEY (tenant_id, prop_id),
  -- §6.1：POST /props 的自然幂等键。
  UNIQUE (tenant_id, series_scope, display_name),
  -- §2.1 的 prop_canonical_within_head。
  CONSTRAINT prop_canonical_within_head CHECK (
    canonical_revision IS NULL OR canonical_revision <= current_revision)
);
CREATE INDEX IF NOT EXISTS prop_by_series
  ON prop (tenant_id, series_scope, status, prop_kind);
CREATE INDEX IF NOT EXISTS prop_story_critical
  ON prop (tenant_id, series_scope) WHERE story_critical = 1;

-- ---------- 不可变 revision（§2.2） ----------
-- 这 18 列里**没有** condition / open_state / current_holder / location。
-- 这是刻意的结构防御（§1.1）：没有列，就没有"谁来改它"的问题。
-- `variant_axes` 随 revision 存，是为了历史可解析——将来加 `burnt` 轴时，
-- 旧 revision 的轴集合仍然是旧的，不需要回填。
CREATE TABLE IF NOT EXISTS prop_revision (
  tenant_id        TEXT NOT NULL,
  prop_id          TEXT NOT NULL,
  revision         INTEGER NOT NULL CHECK (revision >= 1),
  content_hash     TEXT NOT NULL CHECK (length(content_hash) = 64),
  text_fingerprint TEXT,
  payload          TEXT NOT NULL,
  physical         TEXT NOT NULL,
  appearance       TEXT NOT NULL,
  text_details     TEXT NOT NULL,
  variant_manifest TEXT NOT NULL,
  variant_axes     TEXT NOT NULL,
  rights_ref       TEXT NOT NULL,
  source_kind      TEXT NOT NULL CHECK (source_kind IN
                     ('manual_upload','generated','imported','derived')),
  parent_revision  INTEGER,
  quality_gate_ref TEXT NOT NULL,
  accepted_at      TEXT NOT NULL,
  accepted_by      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, prop_id, revision),
  FOREIGN KEY (tenant_id, prop_id) REFERENCES prop (tenant_id, prop_id),
  CONSTRAINT pr_parent_lt CHECK (
    parent_revision IS NULL OR parent_revision < revision)
);
-- §2.2 / C14-11：租户级内容唯一。"同内容再接受一次"是 409，不是新 revision。
CREATE UNIQUE INDEX IF NOT EXISTS pr_hash_uniq
  ON prop_revision (tenant_id, content_hash);
CREATE INDEX IF NOT EXISTS pr_text_fp
  ON prop_revision (tenant_id, prop_id, text_fingerprint);

-- ---------- 外部登记的必需变体（§2.3） ----------
-- 这张表解决的是"剧情说碎了、资产侧画不出碎样"的早期发现：缺口在资产侧被
-- 拒掉，而不是等 S17 报画面不符。`required_by` 是字符串不是外键，因为需求方
-- 可能是 S09（镜头）、S02（政策）或人工，S14 不解析它。
CREATE TABLE IF NOT EXISTS required_variant (
  tenant_id      TEXT NOT NULL,
  prop_id        TEXT NOT NULL,
  axis           TEXT NOT NULL,
  required_value TEXT NOT NULL,
  required_by    TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, prop_id, axis, required_value, required_by),
  FOREIGN KEY (tenant_id, prop_id) REFERENCES prop (tenant_id, prop_id)
);

-- ---------- 文字细节派生表（§2.4；payload 才是权威） ----------
-- `text_plain` 在 sensitive=true 时**恒为 NULL**：这张表即使被误查也不泄漏
-- 明文（§1.3 第 3 条、§9 反模式 9）。原文只存在 prop_revision.payload 里。
CREATE TABLE IF NOT EXISTS prop_text_detail (
  tenant_id    TEXT NOT NULL,
  prop_id      TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  surface      TEXT NOT NULL,
  content_kind TEXT NOT NULL,
  language     TEXT NOT NULL,
  legibility   TEXT NOT NULL CHECK (legibility IN
                 ('must_be_readable','may_be_blurred','background_only')),
  sensitive    INTEGER NOT NULL DEFAULT 0 CHECK (sensitive IN (0,1)),
  text_hash    TEXT NOT NULL CHECK (length(text_hash) = 64),
  text_plain   TEXT,
  PRIMARY KEY (tenant_id, prop_id, revision, surface),
  FOREIGN KEY (tenant_id, prop_id, revision)
    REFERENCES prop_revision (tenant_id, prop_id, revision),
  -- sensitive 的行不许有明文。把"派生表不含敏感明文"变成数据库事实，
  -- 而不是一句实现约定。
  CONSTRAINT ptd_sensitive_has_no_plaintext CHECK (
    sensitive = 0 OR text_plain IS NULL)
);
CREATE INDEX IF NOT EXISTS prop_text_hash
  ON prop_text_detail (tenant_id, text_hash);

-- ---------- 候选（§2.5） ----------
CREATE TABLE IF NOT EXISTS prop_candidate (
  tenant_id    TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  prop_id      TEXT NOT NULL,
  content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
  payload      TEXT NOT NULL,
  prop_check   TEXT NOT NULL,
  evaluation   TEXT NOT NULL,
  state        TEXT NOT NULL CHECK (state IN
                 ('PENDING','ACCEPTED','REJECTED','EXPIRED')),
  created_by   TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, candidate_id),
  FOREIGN KEY (tenant_id, prop_id) REFERENCES prop (tenant_id, prop_id)
);
CREATE INDEX IF NOT EXISTS prop_candidate_pending
  ON prop_candidate (tenant_id, prop_id, state, created_at);

-- ---------- canonical 指针（§2.5） ----------
-- supersede 不删旧行：canonical 的每次移动都要能回查（§1.5 的可审计性）。
CREATE TABLE IF NOT EXISTS canonical_pointer (
  tenant_id        TEXT NOT NULL,
  prop_id          TEXT NOT NULL,
  revision         INTEGER NOT NULL,
  text_fingerprint TEXT,
  decided_at       TEXT NOT NULL,
  decided_by       TEXT NOT NULL,
  decision_kind    TEXT NOT NULL CHECK (decision_kind IN
                     ('human_approved','policy_auto','initial_import')),
  reason_text      TEXT,
  prior_canonical  INTEGER,
  superseded_at    TEXT,
  PRIMARY KEY (tenant_id, prop_id, revision),
  FOREIGN KEY (tenant_id, prop_id, revision)
    REFERENCES prop_revision (tenant_id, prop_id, revision)
);
-- §2.5 / §6.2：一个道具同一时刻只有一个 canonical。并发设 canonical 的
-- 第二个写者会撞这条索引，而不是让两个指针同时"有效"。
CREATE UNIQUE INDEX IF NOT EXISTS prop_canonical_one
  ON canonical_pointer (tenant_id, prop_id) WHERE superseded_at IS NULL;
"""
