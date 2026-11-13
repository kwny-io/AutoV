"""S12 自己的 schema。表来源是 `plans/S12-costume-assets.md` §2.1–§2.6。

五条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **`costume_revision` 没有 `worn_by` / `scene_ids` / `current_damage` 列。**
   "这一场谁穿这套"归 S10 的 binding，"现在破成什么样"归 S16 的正史（§0、
   §1.1、§2.8）。方案把话说死了：S12 若也存"当前损坏程度"，这个字段要么由
   S12 消费 S16 事件来改（→ 第二正史 + 自己的状态机），要么永远错，没有第三
   种选择。所以这里连列都不建，§2.8 的五张表一张都不建。
2. **`variant_axes` / `variant_manifest` 是"能力"不是"事实"。**
   它们回答"这一版能画出哪几种样子"，不回答"现在是哪一种"（§1.1）。读不到
   请求的变体时**绝不退回默认**（§4.3）——那会让 S17 拿一张完好的图判"撕破
   肩膀"通过。
3. **`character_ref` 是软引用**（§1.2）。可空、不建外键、不参与身份校验。
   通用服装（`generic_wardrobe`）根本没有角色，硬约束会把 500 个背景角色卡死。
4. **canonical ≠ 最新**（§1.4）。`costume.canonical_revision` 与
   `current_revision` 分开存，接受新版本不自动移动指针。`costume_canonical_one`
   这条部分唯一索引是"一个 costume 只有一个当前 canonical"的数据库形态：
   supersede 是给旧行写 `superseded_at`，不是删行——历史指针必须可回查。
5. **`costume_material_ref` 与 `costume_unverified_ref` 都是 payload 的派生物。**
   前者为检索（"所有丝质长裙"），后者为"哪条配饰引用还悬空"的本地标记。
   权威永远在 `costume_revision.payload`，两张表都能从它重建。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串
（`"%Y-%m-%dT%H:%M:%S.%f"[:-3]+"Z"`）、`boolean` 存 INTEGER 0/1。
主键/唯一约束全部含 `tenant_id`（E28）。方案把表写在 Postgres 的
`costume_assets` schema 下；SQLite 没有 schema，前缀一律去掉。

**本文件不解决**设计层面的缺口。`contracts/asset.costume-revision-accepted.v2
.schema.json` 里已经登记了 6 条 `x-needs-owner` 与 CCA-1…CCA-7；方案 §10 另有
C12-1…C12-7。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要自行选边」，
本服务按方案散文实现，把偏差与实现时新发现的缺口记在 `CONTRACT_GAPS`，
**不修改契约、不代填决定**。
"""

ID_PREFIX = {"costume": "cos_", "candidate": "cand_"}

COSTUME_KINDS = ("character_outfit", "uniform", "group_costume",
                 "generic_wardrobe")
STATUSES = ("active", "deprecated", "tombstoned")
SOURCE_KINDS = ("manual_upload", "generated", "imported", "derived")
DECISION_KINDS = ("human_approved", "policy_auto", "initial_import")
SHEENS = ("matte", "satin", "metallic", "sheer")
REF_ROLES = ("worn_with", "derived_from", "merged_from", "accessory_of")
CANDIDATE_STATES = ("PENDING", "ACCEPTED", "REJECTED", "EXPIRED")

# 本服务**没有**敏感字段，因此不需要 S01 的读取作用域。§5.4 的
# `TENANT_SCOPE_DENIED` 只用于"请求体里的 tenant_id 与签名上下文不一致"。
REGISTERED_PERMISSIONS = frozenset()

CONTRACT_GAPS = [
    # ---------- 方案 §10 自己登记的 ----------
    {"id": "C12-1", "kind": "conflict",
     "summary": "service.yaml / catalog 只给 POST /costumes/{id}/revisions 与 "
                "GET /costumes/{id}/revisions/{revision}；方案 §5.1/§4.3 却拆成 "
                "登记 / 候选 / 接受 / 拒绝 / set-canonical / 状态 / 引用 / "
                "变体查询一组命令",
     "handling": "按方案 §5 实现，**不改 service.yaml/catalog**（生成物，改要改 "
                 "catalog）。契约面是扩还是收待 owner 决定。"},
    {"id": "C12-2", "kind": "needs_owner",
     "summary": "docs/10 服装质量门写「角色身份不变」，但没说是谁产出这个结论；"
                "方案假设 S33/S34 的 QC，而 S33 是音效、S34 是配乐，都不是 QC",
     "handling": "S12 只读结论、绝不做视觉比对（§4.2）。结论从命令内联的 "
                 "`evaluation` 读，或从可选的 `QUALITY_GATE_BASE_URL` 读；"
                 "产出方与结论形状待 owner 定。"},
    {"id": "C12-3", "kind": "needs_owner",
     "summary": "docs/10 说 S12 持有破损/湿度变体，但 S16 的期望状态与 S12 的 "
                "`variant_axes` 没有任何显式接口对齐；破损何时发生/持续多久在 "
                "S16，S12 只提供可选项",
     "handling": "按 §1.3 由 S12 定义轴与取值词表（`damage`/`wetness`/`makeup` "
                 "为例），S16 选值、S17 执行。两边词表如何同步 待 设计方确认；"
                 "本服务不订阅 S16。"},
    {"id": "C12-4", "kind": "gap",
     "summary": "event-topics.v2.json 只有 asset.costume.revision.accepted，"
                "没有独立的「变体轴变化」事件",
     "handling": "在同一事件的 `variant_axes` 字段承载（§5.3），不新增 topic、"
                 "不改契约。消费者按这个字段决定「这一版支持哪些档位」。"},
    {"id": "C12-5", "kind": "needs_owner",
     "summary": "配饰（项链、戒指）既可能是服装配饰、也可能在 S14 有独立道具"
                "定义，形成双重定义（§1.2、§11.2 第 1 条）",
     "handling": "S12 持有「搭配关系」，S14 持有「可被特写/被持物的定义」；"
                 "S12 只记 `unverified_refs`，**不校验存在、不写 S14、不自动"
                 "同步**。谁拥有配饰 未定，需设计方裁决。"},
    {"id": "C12-6", "kind": "needs_owner",
     "summary": "docs/10 未定义 `costume_kind`（制服/群像/背景服装）；方案 §2.1 "
                "新增四个枚举值",
     "handling": "按 §2.1 的四个 CHECK 值实现；是否进入契约 待 owner 确认。"
                 "`generic_wardrobe` 可以永久没有 canonical（§2.1、CCA-7）。"},
    {"id": "C12-7", "kind": "conflict",
     "summary": "service.yaml 的 depends_on 是 [S28,S01]，但 S12 实际还需要 "
                "QC 结论；若 QC 走同步调用，catalog 就要加依赖",
     "handling": "S28 是必需依赖（artifact 校验）；QC 结论做成**可选**注入，"
                 "未配置时只能随命令内联传入，**不拿 true 兜底**。是否把 QC "
                 "写进 depends_on 待 owner 定；未改 catalog。"},

    # ---------- 实现时新发现的 ----------
    {"id": "C12-8", "kind": "gap",
     "summary": "§5.1 列了 POST /costumes/{id}:merge，但全文没有它的命令体与"
                "语义（合并后 source 的 revision / canonical / 引用怎么办未定义）",
     "handling": "**不实现该路由**（与 S14 的 F16 同一思路：没有路由是最强的"
                 "边界声明）。按 §9 不猜语义；需要时由 owner 补契约。"},
    {"id": "C12-9", "kind": "conflict",
     "summary": "§4.3 的 GET /costumes:lookup?series_scope=&alias= 只按二元组"
                "查找，而 §2.1 的唯一键是 (series_scope, display_name, "
                "costume_kind)，同名不同类会命中多行",
     "handling": "按 display_name 精确匹配并返回 `items[]`（按 costume_kind "
                 "排序），不发明「取第一个」的规则——取第一个会让查询结果取决于"
                 "插入顺序。契约是否新增 costume_kind 参数 待 owner 定。"},
    {"id": "C12-10", "kind": "needs_owner",
     "summary": "§4.3 说回显的 `variant` 可能包含「未指定轴取默认」，但数据模型"
                "里没有任何「哪个取值是默认」的声明（`variant_axes` 只是数组）",
     "handling": "取每个轴**第一个声明值**为默认（`damage` 例子里正好是 "
                 "`none`），并在响应 `variant` 里显式回显解析结果。这个约定由"
                 "本实现选定，真正的默认值语义 待 owner 定。"},
    {"id": "C12-11", "kind": "gap",
     "summary": "§4.2 第 3 步的「`missing_variants` 非空且 policy_auto → 422」"
                "在正常路径上不可达：§4.1 第 2 步已经把缺变体的候选拒掉了",
     "handling": "两处都实现。起草是主闸门；接受时的复查读的是候选里**已存**的 "
                 "`variant_check`，只有旧代码/导入产生的候选才可能非空——这正是"
                 "它存在的意义（升级后旧候选不能被 policy_auto 悄悄放行）。"},
    {"id": "C12-12", "kind": "conflict",
     "summary": "§2.2 的 `costr_hash_uniq` 是 (tenant_id, content_hash) 租户级"
                "唯一，于是两个不同 costume 若内容完全相同会互相冲突；F6 只描述"
                "了「同内容重复接受」",
     "handling": "按方案建租户级唯一索引；`content_hash` 是纯内容（不含 "
                 "costume_id / revision / 时钟），跨 costume 重复同内容会 409 "
                 "DUPLICATE_COSTUME_CONTENT。若 owner 要按 costume 去重，索引要"
                 "加 costume_id。"},
    {"id": "C12-13", "kind": "conflict",
     "summary": "E24-C1 要求「图文匹配失败 → 候选 REJECTED、不写 revision」，"
                "但 §5.4 的错误码表没有对应的码，§4.2 也没有这一步",
     "handling": "接受前先看 `evaluation` 里的图文匹配结论；判负时在**独立事务**"
                 "里把候选落成 REJECTED，再抛 422 `QUALITY_GATE_REJECTED`"
                 "（独立事务是必须的：命令事务里写然后抛错会一起回滚，候选永远"
                 "停在 PENDING）。这个码不在 §5.4，契约待补。"},
    {"id": "C12-14", "kind": "gap",
     "summary": "§4.1 第 5 条/§5.3 要求记 `unverified_refs` 并在 S14 事件到达时"
                "标记已验证，但 §2 没有这张表，元素形状也没定义（CCA-3）",
     "handling": "建本地派生表 `costume_unverified_ref`（可从 payload 重建），"
                 "形状 {ref_key, target_service, target_id, target_revision}；"
                 "`verified_at/verified_by` 是本地标记，不改 revision、不发事件。"},
    {"id": "C12-15", "kind": "gap",
     "summary": "§2.4 说 `costume_material_ref` 是 payload 的派生物、写入时必须"
                "与 `payload.outfit` 一致，但没说从哪个字段派生、`sheen` 缺省时"
                "写什么",
     "handling": "接受 revision 时从 `outfit.pieces[]` 派生（piece_id/material/"
                "sheen/texture_note）；`sheen` 缺省写 NULL 而不是猜一个值。"
                 "`material_palette` 原样存 revision，不进派生表。"},
    {"id": "C12-16", "kind": "conflict",
     "summary": "§2.7 给的 idempotency_record / outbox / inbox DDL 与平台 SDK "
                "（idempotency_sdk / event_sdk）的实际列不同：没有 command_type / "
                "state / response_code / partition_key / aggregate_sequence / "
                "headers",
     "handling": "用平台 SDK 建表（全仓一致）。幂等语义不变：(tenant_id, "
                 "command_id) + JCS request_hash，同键不同 body 409；partition "
                 "key = tenant_id:costume_id 的语义由 tenant_id + aggregate_id "
                 "承载（S08 §6.4）。"},
    {"id": "C12-17", "kind": "gap",
     "summary": "§5.4 列了 429 `WRITE_BACKPRESSURE`，但全文没有触发条件（既没说"
                "按什么计数，也没说阈值）",
     "handling": "按单个 costume 的 PENDING 候选数封顶（`MAX_PENDING_CANDIDATES`="
                 "64，可注入），超出 429 且 retryable=true。这是**本地背压**，"
                 "不是业务配额——配额与计费归 S23，本服务不读它也不写它。"
                 "阈值待 owner 定。"},
    {"id": "C12-18", "kind": "conflict",
     "summary": "§4.3 的响应示例里 `artifacts` 的键是 artifact 自己的名字"
                "（`front` / `detail_shoulder`），但数据模型只有 "
                "`variant_manifest: {axis: {value: artifact_ref}}`，没有「名字」"
                "这一层",
     "handling": "`artifacts` 用 `axis:value` 作键（如 `damage:torn_shoulder`），"
                 "这是唯一能从 manifest 无损还原的键集合；示例里的名字是"
                 "描述性节选。若 owner 需要命名 artifact，manifest 要加一层映射。"},

    # ---------- 事件契约里已登记的 ----------
    {"id": "C12-19", "kind": "conflict",
     "summary": "§3.2 的 canonical 图与 E08-C2 要求「回退到用过的 revision」，"
                "但 `canonical_pointer` 的主键是 (tenant_id, costume_id, "
                "revision)，同一版只能有一行；§2.5 又说 supersede 不删旧行"
                "以便审计。两者一起意味着回退必须原地复活旧行，而那一行的 "
                "decided_at / decided_by / decision_kind 会被覆盖 —— "
                "「留审计」与实际 DDL 无法同时成立",
     "handling": "按主键做 upsert：已存在就 SET superseded_at=NULL 并覆盖"
                 "决策元数据，不存在才 INSERT。回退本身可用（E08-C2 通过），"
                 "但**每次移动的完整历史不可查**。要真正做到「留审计」需要 "
                 "canonical_pointer 改成追加表（主键去掉 revision，或加一张 "
                 "canonical_move 日志表），属于 schema 变更，待 owner 定。"},
    {"id": "CCA-1", "kind": "conflict",
     "summary": "方案与契约示例把身份 QC 写成 S33/S34，但 S33 是音效、S34 是"
                "配乐；真正的 S35/S36 也接不上——QCReport v2 要求 shot_ref，"
                "`artifact_kind` 没有资产图片这一值",
     "handling": "S12 不调任何 QC 服务，只读随命令内联（或可配置上游）的结论；"
                 "`identity_invariance_check` 缺失时按「不是 pass」处理（只影响 "
                 "policy_auto）。真实 QC 接线待 owner 定。"},
    {"id": "CCA-2", "kind": "conflict",
     "summary": "事件示例的 `variant_axes` 只有 damage 与 wetness，缺 makeup；"
                "而方案 §1.3/§2.2/§4.1 的例子三轴齐全，S17 对 makeup 零引用",
     "handling": "事件发**本版实际存下的** `variant_axes` 全量（含 makeup），"
                 "不按示例裁剪；示例只是节选。词表本身仍归 S12（C12-3）。"},
    {"id": "CCA-3", "kind": "conflict",
     "summary": "`unverified_refs` 的元素形状在事件、方案、S14 三处都没有定义；"
                "S14 全仓对它的引用为 0，§5.3 声称的「标记已验证」在 S14 侧不存在",
     "handling": "本服务自己定义元素形状并在事件里原样发出（C12-14）；「匹配 "
                 "S14 事件」只做**本地**标记，不假设 S14 知道这件事。"},
    {"id": "CCA-4", "kind": "conflict",
     "summary": "`:set-canonical` 能不产生 revision、不发任何事件地移动 "
                "canonical；事件里既没有 `decision_kind` 也没有 "
                "`prior_canonical`，而 `canonical_pointer` 两列都有",
     "handling": "接受 revision 时发事件；`:set-canonical` **不发**事件（方案"
                 "如此）。按事件缓存 canonical 的消费方必须回读 "
                 "GET /costumes/{id}/canonical；决策审计只在本库可查。"},
    {"id": "CCA-5", "kind": "conflict",
     "summary": "`identity_invariance_check` 与 `IDENTITY_CHANGED` 全仓各只出现"
                "一次（都在方案 §4.2 / §8），没有形状、没有取值枚举；"
                "`evaluation` 只是一个不限结构的 jsonb",
     "handling": "只认 `evaluation.identity_invariance_check == 'pass'`；其余"
                 "（含缺失、'fail'、'IDENTITY_CHANGED'）在 policy_auto 下都拒，"
                 "报 422 IDENTITY_CHECK_REQUIRED_FOR_AUTO。形状待 owner 定。"},
    {"id": "CCA-6", "kind": "conflict",
     "summary": "方案 §2.7 的 outbox UNIQUE (aggregate_id, aggregate_sequence, "
                "event_type) 与 inbox 主键 (consumer, event_id) 都不含 "
                "tenant_id，跨租户若撞同一个 aggregate_id 会互相顶掉；"
                "全仓 48 篇方案里 36 篇如此",
     "handling": "用平台 SDK 建表（event_id 主键、(consumer,event_id) 去重），"
                 "tenant 由 envelope 携带并在消费时校验；`aggregate_id` 是 "
                 "costume_id，跨租户同 id 的概率与影响都不被本服务扩大。"},
    {"id": "CCA-7", "kind": "conflict",
     "summary": "「`generic_wardrobe` 可以没有 canonical」只写在散文里"
                "（§2.1 行 141–143、§3.2），DDL 与事件都无法表达「为什么它可以」"
                "——`canonical_revision` 本来就可空",
     "handling": "允许**所有** costume_kind 的 canonical 为空；不建「非 "
                 "generic_wardrobe 必须有 canonical」的约束（会在登记后、接受前"
                 "把合法中间态挡住）。是否需要区分 待 owner 定。"},

]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 服装头（§2.1） ----------
-- `canonical_revision` 与 `current_revision` 分开：canonical ≠ 最新（§1.4）。
-- `character_ref` 是 TEXT 里的 JSON，不是外键：软引用（§1.2），可以指向一个
-- S11 还没建的角色，也可能为 NULL。
CREATE TABLE IF NOT EXISTS costume (
  tenant_id          TEXT NOT NULL,
  costume_id         TEXT NOT NULL,
  project_id         TEXT NOT NULL,
  series_scope       TEXT NOT NULL,
  display_name       TEXT NOT NULL,
  costume_kind       TEXT NOT NULL CHECK (costume_kind IN (
                       'character_outfit','uniform','group_costume',
                       'generic_wardrobe')),
  character_ref      TEXT,
  status             TEXT NOT NULL CHECK (status IN
                       ('active','deprecated','tombstoned')),
  canonical_revision INTEGER,
  current_revision   INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
  created_by         TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  PRIMARY KEY (tenant_id, costume_id),
  -- §6.1：POST /costumes 的自然幂等键。
  UNIQUE (tenant_id, series_scope, display_name, costume_kind),
  -- §2.1 的 canonical_within_head。
  CONSTRAINT canonical_within_head CHECK (
    canonical_revision IS NULL OR canonical_revision <= current_revision)
);
CREATE INDEX IF NOT EXISTS costume_by_series
  ON costume (tenant_id, series_scope, status, costume_kind);
CREATE INDEX IF NOT EXISTS costume_by_character
  ON costume (tenant_id, json_extract(character_ref, '$.id'))
  WHERE character_ref IS NOT NULL;

-- §3.3：tombstoned 不能复活（复活走新 costume_id）。内核会拒，这里再加一道
-- 数据库门——将来多一条写路径（回填脚本、迁移）时挡住它的必须是库。
CREATE TRIGGER IF NOT EXISTS deny_costume_resurrection
BEFORE UPDATE OF status ON costume
FOR EACH ROW WHEN OLD.status = 'tombstoned' AND NEW.status <> 'tombstoned'
BEGIN
  SELECT RAISE(ABORT, 'costume is tombstoned; create a new costume_id instead');
END;

-- ---------- 不可变 revision（§2.2） ----------
-- 这 16 列里**没有** worn_by / scene_ids / current_damage / location。
-- 这是刻意的结构防御（§1.1、§2.8）。`variant_axes` 随 revision 存，是为了
-- 历史可解析：将来加 `weather_stain` 轴时，旧 revision 的轴集合仍是旧的。
-- `accessories` 是 JSON 列表，配饰引用同时派生到 costume_unverified_ref。
CREATE TABLE IF NOT EXISTS costume_revision (
  tenant_id        TEXT NOT NULL,
  costume_id       TEXT NOT NULL,
  revision         INTEGER NOT NULL CHECK (revision >= 1),
  content_hash     TEXT NOT NULL,
  payload          TEXT NOT NULL,
  outfit           TEXT NOT NULL,
  material_palette TEXT NOT NULL,
  accessories      TEXT NOT NULL,
  variant_manifest TEXT NOT NULL,
  variant_axes     TEXT NOT NULL,
  rights_ref       TEXT NOT NULL,
  source_kind      TEXT NOT NULL CHECK (source_kind IN
                     ('manual_upload','generated','imported','derived')),
  parent_revision  INTEGER,
  quality_gate_ref TEXT NOT NULL,
  accepted_at      TEXT NOT NULL,
  accepted_by      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, costume_id, revision),
  FOREIGN KEY (tenant_id, costume_id) REFERENCES costume (tenant_id, costume_id),
  -- §2.2 的 costr_hash_len：64 位小写 hex，不只是"长度 64"。
  CONSTRAINT costr_hash_len CHECK (
    length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
  CONSTRAINT costr_parent_lt CHECK (
    parent_revision IS NULL OR parent_revision < revision)
);
-- §2.2 / C12-12：租户级内容唯一。"同内容再接受一次"是 409，不是新 revision。
CREATE UNIQUE INDEX IF NOT EXISTS costr_hash_uniq
  ON costume_revision (tenant_id, content_hash);

-- ---------- 材质派生表（§2.4；payload 才是权威） ----------
-- 单独成表只为检索（"所有丝质长裙"）。`sheen` 缺省写 NULL，不猜值。
CREATE TABLE IF NOT EXISTS costume_material_ref (
  tenant_id    TEXT NOT NULL,
  costume_id   TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  piece_id     TEXT NOT NULL,
  material     TEXT NOT NULL,
  sheen        TEXT CHECK (sheen IS NULL OR sheen IN
                 ('matte','satin','metallic','sheer')),
  texture_note TEXT,
  PRIMARY KEY (tenant_id, costume_id, revision, piece_id),
  FOREIGN KEY (tenant_id, costume_id, revision)
    REFERENCES costume_revision (tenant_id, costume_id, revision)
);
CREATE INDEX IF NOT EXISTS costume_material_lookup
  ON costume_material_ref (tenant_id, material, sheen);

-- ---------- 候选（§2.5） ----------
CREATE TABLE IF NOT EXISTS costume_candidate (
  tenant_id     TEXT NOT NULL,
  candidate_id  TEXT NOT NULL,
  costume_id    TEXT NOT NULL,
  content_hash  TEXT NOT NULL CHECK (length(content_hash) = 64),
  payload       TEXT NOT NULL,
  evaluation    TEXT NOT NULL,
  variant_check TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN
                  ('PENDING','ACCEPTED','REJECTED','EXPIRED')),
  created_by    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, candidate_id),
  FOREIGN KEY (tenant_id, costume_id) REFERENCES costume (tenant_id, costume_id)
);
CREATE INDEX IF NOT EXISTS costume_candidate_pending
  ON costume_candidate (tenant_id, costume_id, state, created_at);

-- ---------- canonical 指针（§2.5） ----------
-- supersede 不删旧行：canonical 的每次移动都要能回查（§1.4 的可审计性）。
CREATE TABLE IF NOT EXISTS canonical_pointer (
  tenant_id        TEXT NOT NULL,
  costume_id       TEXT NOT NULL,
  revision         INTEGER NOT NULL,
  decided_at       TEXT NOT NULL,
  decided_by       TEXT NOT NULL,
  decision_kind    TEXT NOT NULL CHECK (decision_kind IN
                     ('human_approved','policy_auto','initial_import')),
  reason_text      TEXT,
  prior_canonical  INTEGER,
  superseded_at    TEXT,
  PRIMARY KEY (tenant_id, costume_id, revision),
  FOREIGN KEY (tenant_id, costume_id, revision)
    REFERENCES costume_revision (tenant_id, costume_id, revision)
);
-- §2.5 / §6.2：一个 costume 同一时刻只有一个 canonical。并发设 canonical 的
-- 第二个写者会撞这条索引，而不是让两个指针同时"有效"。
CREATE UNIQUE INDEX IF NOT EXISTS costume_canonical_one
  ON canonical_pointer (tenant_id, costume_id) WHERE superseded_at IS NULL;

-- ---------- 引用关系（§2.6） ----------
-- `worn_with` 是**推荐组合**，不是强制；S10 的 binding 决定实际组合。
CREATE TABLE IF NOT EXISTS revision_reference (
  tenant_id       TEXT NOT NULL,
  costume_id      TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  ref_role        TEXT NOT NULL CHECK (ref_role IN
                    ('worn_with','derived_from','merged_from','accessory_of')),
  target_service  TEXT NOT NULL,
  target_id       TEXT NOT NULL,
  target_revision INTEGER NOT NULL,
  target_hash     TEXT CHECK (target_hash IS NULL OR
                    (length(target_hash) = 64
                     AND target_hash NOT GLOB '*[^0-9a-f]*')),
  PRIMARY KEY (tenant_id, costume_id, revision, ref_role, target_service,
               target_id),
  FOREIGN KEY (tenant_id, costume_id, revision)
    REFERENCES costume_revision (tenant_id, costume_id, revision)
);
CREATE INDEX IF NOT EXISTS revision_reference_by_target
  ON revision_reference (tenant_id, target_service, target_id);

-- ---------- 未验证引用（§4.1 第 5 条、§5.3；C12-14 / CCA-3） ----------
-- 派生自 `costume_revision.accessories`：S12 不校验 S14 是否存在该 revision
-- （F14：配饰常常先在服装侧出现、后进道具库）。`verified_at/verified_by` 只是
-- 本地标记——收到 asset.prop.revision.accepted 时填，不改 revision、不发事件。
CREATE TABLE IF NOT EXISTS costume_unverified_ref (
  tenant_id       TEXT NOT NULL,
  costume_id      TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  ref_key         TEXT NOT NULL,
  target_service  TEXT NOT NULL,
  target_id       TEXT NOT NULL,
  target_revision INTEGER,
  ref_payload     TEXT NOT NULL,
  verified_at     TEXT,
  verified_by     TEXT,
  PRIMARY KEY (tenant_id, costume_id, revision, ref_key),
  FOREIGN KEY (tenant_id, costume_id, revision)
    REFERENCES costume_revision (tenant_id, costume_id, revision)
);
CREATE INDEX IF NOT EXISTS costume_unverified_open
  ON costume_unverified_ref (tenant_id, target_service, target_id)
  WHERE verified_at IS NULL;
"""
