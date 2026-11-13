"""S11 自己的 schema。表来源是 `plans/S11-character-assets.md` §2。

四条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **`character_revision` 没有 `costume` / `makeup` / `injury` / `location` /
   `voice_id` 列。** 服装归 S12、剧情状态归 S16、音色归 S15（§0、§1.1、§2.8）。
   在 S11 存"这一集穿红裙"会让服装事实出现两份；存"受伤了"会让 S11 变成第二
   个正史。所以 §2.8 的七张表一张都不建，连列都不留——没有列，就没有"谁来改它"
   的问题。
2. **候选表没有 `revision` / `canonical_eligible` / `accepted_at`。** §1.3 的要求
   是"未接受的东西在结构上无法被误用"。S10 只读
   `GET /characters/{id}/revisions/{revision}`，候选不在这条路径上；再加一个
   `canonical_eligible` 列，就又把"隔离"退回成一句 if 判断（§9 反模式 1）。
3. **`character.canonical_revision` 与 `current_revision` 是两列。**
   canonical ≠ 最新（§1.2）。`set_canonical=false` 的接受会让 revision 前进而
   指针不动；两者合成一列，就等于"最新生成图自动成为 canonical"。
4. **`canonical_one_current` 部分唯一索引是"一个角色只有一个当前 canonical"的
   物理形态**（§2.5）。supersede 不删旧行，所以历史 canonical 可回查——
   这是 E08-B2（回退 12→9 只改指针、留两条审计）能成立的前提。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串
（`"%Y-%m-%dT%H:%M:%S.%f"[:-3]+"Z"`）、`boolean` 存 INTEGER 0/1。
主键/唯一约束全部含 `tenant_id`（E28）。

方言差异（不是语义改动）：方案用 Postgres 的 `~ '^[0-9a-f]{64}$'` 做 hash 长度
检查，SQLite 没有内置正则，这里用 `length(...) = 64`。**`identity_feature_hash`
上刻意不加 CHECK**：契约的 `x-needs-owner` 第 5 条（CRC-5）正把"两个 hash 的
严格度不一致"留给 owner 裁决，补一条 CHECK 就等于替他决定了。
"""

ID_PREFIX = {"character": "chr_", "candidate": "cand_"}

# 本轮**不解决**的缺口。`contracts/asset.character-revision-accepted.v2.schema.json`
# 里已有 CRC-1…CRC-6 六条 `x-conflicts` 与 6 条 `x-needs-owner`，那些是待 owner
# 决定的事项；按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要自行选边」，
# 本服务按方案散文实现，把偏差记在这里，不改契约、不代填决定。
CONTRACT_GAPS = [
    # --- 方案 §10 自己登记的冲突 ---
    {"id": "C11-1", "kind": "conflict",
     "summary": "service.yaml / openapi 只声明 POST /characters/{id}/revisions 与 "
                "GET /characters/{id}/revisions/{revision}；§5 却拆成 candidates / "
                "accept / reject / set-canonical / deprecate / tombstone / aliases "
                "一组命令",
     "handling": "按方案 §5 实现，不改契约。契约面的扩展等 owner 决定。"},
    {"id": "C11-2", "kind": "needs_owner",
     "summary": "docs/10 只说 accepted canonical，未区分「revision 已接受」与"
                "「canonical 指针指向它」（§3.4）",
     "handling": "按 §1.2 分成两层实现：revision 不可变、canonical 是独立指针，"
                 "允许 revision 存在而 canonical 指向另一版。docs/10 的表述要不要"
                 "补这一层，待设计方确认。"},
    {"id": "C11-3", "kind": "conflict",
     "summary": "event-topics.v2.json 只有 asset.character.revision.accepted，"
                "没有 canonical 变更事件；`:set-canonical` 移动指针时连这条事件"
                "都不发",
     "handling": "接受路径在同一事件里带 became_canonical + canonical_revision；"
                 "`:set-canonical` 不发事件（与 §5.1 一致）。按事件缓存 canonical "
                 "的消费方必须回读 GET /characters/{id}/canonical。"},
    {"id": "C11-4", "kind": "conflict",
     "summary": "§5.3 说消费 S28 的 artifact.archived 与 S48 的 "
                "training.lora.registered，但 event-topics.v2.json 里 S28 的事件叫 "
                "artifact.ingested（没有 artifact.archived），S48 完全没有主题；"
                "service.yaml 的 depends_on 也不含 S48",
     "handling": "**不实现任何事件消费者**：对不上的主题名不能猜，且 inbox 去重"
                 "（F15）要有主题才谈得上。等 catalog 与 topics 补齐。"},
    {"id": "C11-5", "kind": "needs_owner",
     "summary": "docs/10 说角色质量门含「权利引用」，但没说权利对象归谁；§2.1 又"
                "写着「见 §5.2 的约束触发器」，而 §5.2 是查询路由表，没有触发器",
     "handling": "按 C11-5 的假设：rights_ref 指向 S02 的 rights_* 对象，S11 只存"
                 "指针、只校验形态，**绝不判定有效性**（§9 反模式 5）。"
                 "「真人类必须有 rights_ref」由代码在起草时强制（422 "
                 "RIGHTS_REF_REQUIRED）——identity_kind 在另一张表，SQLite 的 "
                 "CHECK 跨不到，触发器方案在方案里并不存在。权利对象归谁**需**"
                 "设计方说明。"},
    {"id": "C11-6", "kind": "needs_owner",
     "summary": "发型归属在 docs/10 未明确（角色行含「发型」，服装行含「妆造」）",
     "handling": "按 §1.1 的判据写死在代码里：影响识别的（length/texture/hairline）"
                 "归 S11 的 identity_features.hair，纯造型（扎起/发饰）归 S12。"
                 "判据本身需要设计方确认。"},
    {"id": "C11-7", "kind": "gap",
     "summary": "docs/01 的 sole-authority 表未列 S11 的 canonical（§0.1）",
     "handling": "按 docs/10 的「内容/canonical 归 S11」实现；建议 docs/01 补行。"},

    # --- 实现时新发现的 ---
    {"id": "C11-8", "kind": "needs_owner",
     "summary": "事件的 view_count 在方案里没有任何定义：DDL 无此列、散文无此词，"
                "只在 §5.3 的事件示例出现一次（CRC-2 / x-needs-owner 第 1 条）",
     "handling": "事件契约要求它是 ≥1 的整数，所以必须发一个。本实现取 "
                 "len(view_manifest)——契约上唯一可派生的口径（front 必填，故恒 "
                 "≥1）。这个口径**不是** owner 的决定，待 CRC-2 裁决。"},
    {"id": "C11-9", "kind": "conflict",
     "summary": "事件契约把 rights_ref 收成闭合四键且 required，而 §2.1 / §4.2 "
                "只要求真人角色必须有 rights_ref，虚构角色可以为空"
                "（E30-B2 也只管真人）",
     "handling": "按方案存：虚构角色允许 `{}`。事件原样发 payload 里的对象，"
                 "于是虚构角色 + 空 rights_ref 的 payload 会被该 schema 拒。"
                 "**不替 owner 发明 rights_owner/rights_id**，也不给虚构角色伪造"
                 "权利指针。要收哪一边，等契约方裁决。"},
    {"id": "C11-10", "kind": "conflict",
     "summary": "§11.1 第 1 步要求先定型 "
                "contracts/assets/character-revision.v1.schema.json，仓库里没有这个"
                "文件；identity_features 的字段集也没有跨服务统一的契约"
                "（§11.2 第 1 条），而 identity_feature_hash 按 CRC-5 没有 CHECK",
     "handling": "payload 取 §4.2 请求体的九个字段，content_hash = "
                 "sha256(JCS(payload))，identity_feature_hash = "
                 "sha256(JCS(identity_features))（§2.2 的两行注释），全部可复算。"
                 "identity_features 按 §2.3 的枚举校验**已知字段**、未知字段原样"
                 "保留（§2.3：新增字段必须可选）；distinguishing_marks 按 JCS "
                 "排序后存储，使同一集合换序不产生假漂移。字段集与合同化待 owner。"},
    {"id": "C11-11", "kind": "gap",
     "summary": "§2.4 的 candidate.evaluation 是 NOT NULL（S33/S34 结果 + 身份一致"
                "性分数），但 §4.2 的请求体里没有它，评审结论的来源也没写",
     "handling": "接受可选的内联 `body.evaluation`，缺省存 `{}`。**不臆造分数**："
                 "读不到结论时记空对象，而不是记 pass=true。QC 结论为失败时起草"
                 "直接 422，不落候选。"},
    {"id": "C11-12", "kind": "conflict",
     "summary": "§5.4 的错误码表不全：§3.3 要求拒绝非法状态转移却没有对应码；"
                "E24 要求「QC 未通过」与「多角度一致性失败」有可辨识的拒绝，"
                "表里也没有",
     "handling": "补三个 409/422 非重试码：INVALID_STATUS_TRANSITION（§3.3）、"
                 "CANDIDATE_QC_FAILED 与 IDENTITY_VIEW_INCONSISTENT（E24-B1/B2，"
                 "后者 details.views 指明哪个视图）。另加 409 COMMAND_IN_FLIGHT"
                 "（幂等 SDK 语义）。这是「表不全」，不是「表错了」。"},
    {"id": "C11-13", "kind": "needs_owner",
     "summary": "§5.4 有 429 WRITE_BACKPRESSURE，但方案没写它在什么条件下触发",
     "handling": "按角色的 PENDING 候选数封顶（默认 64，构造参数可调）。这是"
                 "防止候选无限堆积的本地背压，不是业务配额——配额归 S23。"
                 "64 这个阈值由本实现选定，真正的触发条件待 owner 决定。"},
    {"id": "C11-14", "kind": "gap",
     "summary": "§5.1 列了 POST /characters/{id}:merge，§3.3 也画了 "
                "merged_into 边，但全文没有命令体、没有 merged_into 列、也没有"
                "任何验收用例说合并后 revision / canonical / 绑定怎么办",
     "handling": "**不实现该路由**（没有路由是最强的边界声明）。按方案 §9 不猜"
                 "语义；需要时由 owner 补契约与 DDL。"},
    {"id": "C11-15", "kind": "conflict",
     "summary": "§4.3 的事务体把 character_revision 的 INSERT 放在 "
                "canonical_pointer 之前（第 4、5 步），§6.3 的锁顺序却把 "
                "canonical_pointer 排在第 3、revision 排在第 4",
     "handling": "按 §4.3 的语句顺序写：canonical_pointer 有指向 "
                 "character_revision 的外键，先插指针会撞 FK。SQLite 的 "
                 "BEGIN IMMEDIATE 本来就把整段写事务串行化，§6.3 的排他顺序在这里"
                 "没有对应机制；Postgres 侧仍可按 §6.3 取锁。"},
    {"id": "C11-16", "kind": "conflict",
     "summary": "§2.2 的 cr_hash_uniq 是 (tenant_id, content_hash) 租户级唯一，"
                "于是两个不同角色若内容完全相同会互相冲突，而 F6 只描述了"
                "「同内容重复接受」",
     "handling": "按方案建租户级唯一索引；content_hash 不含 character_id（纯内容），"
                 "所以跨角色重复内容会 409 DUPLICATE_CHARACTER_CONTENT。若 owner "
                 "要的是按角色去重，索引要加 character_id。"},
    {"id": "C11-17", "kind": "needs_owner",
     "summary": "§4.2 第 2 步的 require_full_turnaround 是请求级开关，DDL 里没有"
                "任何列记录它；three_quarter 是否应该必需（§11.2 第 3 条）也没定",
     "handling": "按方案实现成请求级布尔：true 时 side_left / back / three_quarter "
                 "必须齐，false 时只要求 front。取值不落库——落库会让「这一版是不"
                 "是全转面」变成一条会被后来者误读的事实。three_quarter 是否应"
                 "必需，**待** owner 决定（§11.2 第 3 条）。"},
    {"id": "C11-18", "kind": "gap",
     "summary": "service.yaml 的 depends_on 含 S01，但方案全文没有对 S01 的任何"
                "调用；§2.2 行 150 把质量门来源写成「S33/S34/S01」，而 S01 是"
                "租户/权限服务，不产视觉 QC",
     "handling": "**不注入 S01**，deps.py 里没有它的工厂：凭空接一个「看起来"
                 "能判点什么」的上游，会让 S11 变成第二个权威。等 owner 说明 "
                 "S01 到底被用来做什么。"},
    {"id": "C11-19", "kind": "gap",
     "summary": "§5.1 要求 `:set-canonical`（回退）必须带 reason_text，但 §5.4 "
                "没有「缺 reason_text」的错误码",
     "handling": "缺 reason_text 返回 400 MALFORMED_REQUEST（与缺 command_id 同类）。"
                 "要不要单列一个码，等契约方定。"},
    {"id": "C11-20", "kind": "needs_owner",
     "summary": "expression_manifest 的枚举没有收敛（§11.2 第 6 条），"
                "open set 会让每个剧各有一套表情名",
     "handling": "按开放对象校验（键必须是非空字符串、值是 artifact 引用），"
                 "**不发明枚举**。建议的一组值在 §11.2 第 6 条，待与 S04/S19 对齐。"},
    {"id": "C11-21", "kind": "conflict",
     "summary": "§3.3 的状态图只画了 deprecated → tombstoned，而「非法转移」表只"
                "列了 tombstoned → active，没提 active → tombstoned",
     "handling": "按图实现：tombstone 只能从 deprecated 走（直接 active → "
                 "tombstoned 返回 409 INVALID_STATUS_TRANSITION）。一步下线是否"
                 "要允许，需要 owner 在 §3.3 上补一句。"},
    {"id": "C11-22", "kind": "gap",
     "summary": "§2.6 建了 revision_reference（只存出边），但没写谁在什么时候写 "
                "ref_role=derived_from / base_for_lora；merged_from 归未实现的 "
                ":merge",
     "handling": "接受时按可推导的事实写出边：parent_revision 非空写一条 "
                 "derived_from（target_service=S11）；lora_refs 里带 revision 的"
                 "写 base_for_lora（target_service=S48）。缺 revision 的 lora_ref "
                 "写不进边（target_revision NOT NULL），只留在 payload 里。"},
    {"id": "C11-23", "kind": "gap",
     "summary": "drift_class 的判定算法方案没定义：§2.3 只说身份特征要结构化，"
                "§2.5 的 policy_auto 三个条件依赖它，但没有 hard/soft 字段划分、"
                "没有阈值",
     "handling": "算法写死在 service.drift_between 并可被测试复算：hash 相同 → "
                 "none；hard 锚点（face_shape/jaw/eye_shape/brow/nose_bridge/"
                 "lip_fullness/hair/distinguishing_marks）任一不同 → major；"
                 "只有 soft 分档字段（eye_distance_ratio/skin_tone_band/age_band/"
                 "build/height_cm_band）不同 → minor。不引入任何数值阈值。"},
    {"id": "C11-24", "kind": "needs_owner",
     "summary": "跨剧复用角色是否允许未定（§11.2 第 2 条）；当前 series_scope 是"
                "单值",
     "handling": "按单值实现：同一角色不能登记在两个 series 下，复用要新建 "
                 "character_id。若允许复用，series_scope 要变数组且 canonical 是否"
                 "共享要一起定。跨剧复用是否允许**待** owner 决定。"},
    {"id": "C11-25", "kind": "conflict",
     "summary": "§2.1 要求别名歧义时「导入流程」报 AMBIGUOUS_ALIAS，但 §5.1/§5.2 "
                "里没有任何被点名会抛它的路由",
     "handling": "歧义在唯一可能发现它的地方判：GET /characters:lookup 同时按 "
                 "display_name 与 alias_text 在本 series 内查，命中两个以上角色就"
                 " 422 AMBIGUOUS_ALIAS 并列出候选 id，**绝不取第一个**。"
                 "POST /characters/{id}/aliases 本身不因歧义而拒——别名本来就是"
                 "用来对账的，冲突应该在与它相遇的查询上暴露。"},
    {"id": "C11-26", "kind": "conflict",
     "summary": "§2.5 的 canonical_pointer 主键是 "
                "(tenant_id, character_id, revision)，而 E08-B2 要求「回退 "
                "canonical（12→9）」并留下「审计记录两条」——回退到一个曾经是 "
                "canonical 的 revision 必然与它当初被 supersede 的那一行同主键",
     "handling": "主键照 §2.5 建。回退时用 upsert 把那一行重新置为有效并更新"
                 "决定字段（decided_at / decision_kind / reason_text / "
                 "prior_canonical），被顶下来的那一行保留自己的 superseded_at。"
                 "于是「同一 revision 的两次决定」在这张表里只留最后一次——"
                 "E08-B2 的「审计两条」要靠 S06 的审计流，不能靠这张表。"
                 "要不要为它加一张只追加的 canonical 审计表，待 owner 决定。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 角色头（§2.1） ----------
-- `canonical_revision` 与 `current_revision` 分开：canonical ≠ 最新（§1.2）。
-- 这张表**没有** costume / injury / location / voice_id：那些事实各有其主
-- （S12 / S16 / S15），存进来就是第二份正史（§2.8）。
CREATE TABLE IF NOT EXISTS character (
  tenant_id          TEXT NOT NULL,
  character_id       TEXT NOT NULL,
  project_id         TEXT NOT NULL,
  series_scope       TEXT NOT NULL,
  display_name       TEXT NOT NULL,
  identity_kind      TEXT NOT NULL CHECK (identity_kind IN
                       ('fictional','actor_likeness','real_person')),
  status             TEXT NOT NULL CHECK (status IN
                       ('active','deprecated','tombstoned')),
  canonical_revision INTEGER,
  current_revision   INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
  series_of_record   TEXT NOT NULL,
  created_by         TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  PRIMARY KEY (tenant_id, character_id),
  -- §6.1：POST /characters 的自然幂等键。
  UNIQUE (tenant_id, series_scope, display_name),
  -- §2.1 的 canonical_within_head：指针不能指向还不存在的修订。
  CONSTRAINT character_canonical_within_head CHECK (
    canonical_revision IS NULL OR canonical_revision <= current_revision)
);
CREATE INDEX IF NOT EXISTS character_by_series
  ON character (tenant_id, series_scope, status);

-- ---------- 别名（§2.1） ----------
-- 剧本名对账用（"林总" → chr_linlan）。歧义**不在写入时解析**：这里允许
-- 两个角色登记同一个 alias_text，由 :lookup 在相遇时报 AMBIGUOUS_ALIAS。
-- 在写入处拒绝会让"先导入的赢"，那正是 §9 反模式 9。
CREATE TABLE IF NOT EXISTS character_alias (
  tenant_id    TEXT NOT NULL,
  character_id TEXT NOT NULL,
  alias_text   TEXT NOT NULL,
  alias_kind   TEXT NOT NULL CHECK (alias_kind IN
                 ('script_name','nickname','legacy_id','import_alias')),
  source       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, character_id, alias_text, alias_kind),
  FOREIGN KEY (tenant_id, character_id)
    REFERENCES character (tenant_id, character_id)
);
CREATE INDEX IF NOT EXISTS character_alias_lookup
  ON character_alias (tenant_id, alias_text);

-- ---------- 不可变 revision（§2.2） ----------
-- 这 17 列里**没有** costume / makeup / injury / current_location / voice_id。
-- 也没有 status：revision 一旦写入即终态（§3.2）——可变 revision 会让
-- content_hash 变成谎言。
-- `payload` 存全量、冗余列服务查询；两者来自同一份对象（§2.2 行 166）。
CREATE TABLE IF NOT EXISTS character_revision (
  tenant_id             TEXT NOT NULL,
  character_id          TEXT NOT NULL,
  revision              INTEGER NOT NULL CHECK (revision >= 1),
  content_hash          TEXT NOT NULL,
  payload               TEXT NOT NULL,
  view_manifest         TEXT NOT NULL,
  expression_manifest   TEXT NOT NULL,
  body_profile          TEXT NOT NULL,
  identity_features     TEXT NOT NULL,
  -- 漂移检测的锚（§2.2 行 164）。**刻意不加 CHECK**：契约 CRC-5 正把
  -- "为什么 content_hash 有长度检查而它没有"留给 owner 裁决（C11-10）。
  identity_feature_hash TEXT NOT NULL,
  rights_ref            TEXT NOT NULL,
  lora_refs             TEXT NOT NULL DEFAULT '[]',
  source_kind           TEXT NOT NULL CHECK (source_kind IN
                          ('manual_upload','generated','imported','derived')),
  parent_revision       INTEGER,
  quality_gate_ref      TEXT NOT NULL,
  accepted_at           TEXT NOT NULL,
  accepted_by           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, character_id, revision),
  FOREIGN KEY (tenant_id, character_id)
    REFERENCES character (tenant_id, character_id),
  -- §2.2 行 154：长度 64（Postgres 侧是 ~ '^[0-9a-f]{64}$'，方言差异）。
  CONSTRAINT cr_hash_len CHECK (length(content_hash) = 64),
  -- §2.2 行 155：派生来源必须是更早的一版。
  CONSTRAINT cr_parent_lt CHECK (
    parent_revision IS NULL OR parent_revision < revision)
);
-- §2.2 / F6：租户级内容唯一。同一份内容不能有两个 revision，否则
-- "第 3 集用的是哪一版"会因重复注册而失去唯一答案（C11-16）。
CREATE UNIQUE INDEX IF NOT EXISTS cr_hash_uniq
  ON character_revision (tenant_id, content_hash);
-- §2.2 行 158：漂移检测的快速比对。
CREATE INDEX IF NOT EXISTS cr_identity_hash
  ON character_revision (tenant_id, character_id, identity_feature_hash);

-- ---------- 候选隔离区（§2.4） ----------
-- **没有** revision、**没有** canonical_eligible、**没有** accepted_at。
-- 未接受的东西在结构上无法被 S10 绑定：S10 只调
-- GET /characters/{id}/revisions/{revision}，候选不在这条路径上（§1.3）。
CREATE TABLE IF NOT EXISTS character_candidate (
  tenant_id      TEXT NOT NULL,
  candidate_id   TEXT NOT NULL,
  character_id   TEXT NOT NULL,
  content_hash   TEXT NOT NULL CHECK (length(content_hash) = 64),
  payload        TEXT NOT NULL,
  evaluation     TEXT NOT NULL,
  identity_drift TEXT NOT NULL,
  state          TEXT NOT NULL CHECK (state IN
                   ('PENDING','ACCEPTED','REJECTED','EXPIRED')),
  created_by     TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  expires_at     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, candidate_id),
  FOREIGN KEY (tenant_id, character_id)
    REFERENCES character (tenant_id, character_id)
);
-- §2.4 行 213：部分索引，只索引 PENDING（清理与背压都数它）。
CREATE INDEX IF NOT EXISTS candidate_pending
  ON character_candidate (tenant_id, character_id, state, created_at DESC)
  WHERE state = 'PENDING';

-- ---------- canonical 指针（§2.5） ----------
-- supersede 不删旧行：canonical 的每次移动都要能回查（E08-B2 的两条审计）。
-- 这张表**没有** identity_feature_hash 列：漂移判定要读 revision，复制一份
-- 就等于把"canonical 那一版是什么"拆成两处事实。
CREATE TABLE IF NOT EXISTS canonical_pointer (
  tenant_id       TEXT NOT NULL,
  character_id    TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  decided_at      TEXT NOT NULL,
  decided_by      TEXT NOT NULL,
  decision_kind   TEXT NOT NULL CHECK (decision_kind IN
                    ('human_approved','policy_auto','initial_import')),
  reason_text     TEXT,
  prior_canonical INTEGER,
  superseded_at   TEXT,
  PRIMARY KEY (tenant_id, character_id, revision),
  FOREIGN KEY (tenant_id, character_id, revision)
    REFERENCES character_revision (tenant_id, character_id, revision)
);
-- §2.5 行 237 / §6.2：一个角色同一时刻只有一个 canonical。并发接受的两个
-- 写者中，后者撞这条索引，而不是让两个身份基准同时有效。
CREATE UNIQUE INDEX IF NOT EXISTS canonical_one_current
  ON canonical_pointer (tenant_id, character_id) WHERE superseded_at IS NULL;

-- ---------- 引用与派生关系（§2.6） ----------
-- 只记录**出边**。入边（谁引用了这个角色）归 S10 的 binding_usage 与 S09
-- 的依赖图——反向表就是第二份影响分析（§9 反模式 10）。
CREATE TABLE IF NOT EXISTS revision_reference (
  tenant_id       TEXT NOT NULL,
  character_id    TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  ref_role        TEXT NOT NULL CHECK (ref_role IN
                    ('base_for_lora','derived_from','merged_from')),
  target_service  TEXT NOT NULL CHECK (target_service IN ('S48','S11')),
  target_id       TEXT NOT NULL,
  target_revision INTEGER NOT NULL,
  target_hash     TEXT,
  PRIMARY KEY (tenant_id, character_id, revision, ref_role,
               target_service, target_id),
  FOREIGN KEY (tenant_id, character_id, revision)
    REFERENCES character_revision (tenant_id, character_id, revision)
);
"""
