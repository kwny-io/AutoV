"""S13 自己的 schema。表来源是 `plans/S13-scene-assets.md` §2.1–§2.7。

六条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **拓扑是权威，anchor 是证据，光照是派生**（§1.1）。三层的物理分表不是
   拆表癖：`scene_revision.topology` 是唯一能被机器校验的那一层，
   `scene_anchor` 存的是"从某个方向看过去长什么样"，`scene_lighting_variant`
   是同一空间在别的光照下的渲染。只存图就没有"门在哪"的机器答案；只存拓扑
   模型就没法直接吃参考图。
2. **revision 不可变，anchor 也就跟着不可变**（§1.2）。换 anchor 必须接受
   新 revision，旧 revision 的 anchor 仍然可读。所以 `scene_anchor` 的主键带
   `revision`，没有任何 UPDATE 路径。
3. **`topology_hash` 与 `content_hash` 是两个问题**。"换了光照"和"门挪了"
   必须能分开回答，否则所有已生成的镜头可能和新空间不一致而系统不报错。
   `topology_hash` 只覆盖 `topology` 这一块。
4. **`topology_changed` 是显式声明，不是自动比对**（§2.2 行 177）。封存时
   校验它与 parent 的 `topology_hash` 是否自洽；不一致就拒。这样 S07/S09
   可以信任这个布尔值，而不必自己重算一遍几何。
5. **`fixed_object` 没有状态字段**（§2.5）。没有 `is_open`、没有 `is_damaged`：
   有这些就说明它其实是道具，应该归 S14（§1.4 判据）。这张表的列就是这条
   边界的可执行版本。
6. **`canonical_pointer` 用部分唯一索引保证"只有一个当前 canonical"**
   （§2.6 `scene_canonical_one`）。supersede 是"写 `superseded_at`"，不是
   "删旧行"——历史指针必须留着，否则"这一版曾经是 canonical"无从追溯。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串 `…Z`、
`boolean` 存 INTEGER 0/1（SQLite 无原生布尔）。方案把表写在 Postgres 的
`scene_assets` schema 下；SQLite 没有 schema，前缀一律去掉。

**本文件不解决**设计层面的缺口。`contracts/asset.scene-revision-accepted.v2
.schema.json` 里已经登记了 5 条 `x-needs-owner` 与 SCN-1…SCN-8；方案 §10 另有
C13-1…C13-8。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要自行选边」，
本服务按方案散文实现，把偏差与实现时新发现的缺口记在 `CONTRACT_GAPS`，
**不修改契约、不代填决定**。
"""

ID_PREFIX = {"scene": "scn_", "candidate": "cnd_"}

# S01 的权限注册表（`contracts/identity/permission-registry.v2.json`）里
# **没有**任何 scene.* 键，§5.4 也没有一条授权失败码。所以本内核不发明写权限门
# ——那会让每一条写命令在生产里 503。`deps.build_authorizer` 只提供注入点，
# 主路径不调用它（见 CONTRACT_GAPS 的 S13-5）。
REGISTERED_PERMISSIONS = frozenset()

CONTRACT_GAPS = [
    # ---------- 方案 §10 自己登记的 ----------
    {"id": "C13-1", "kind": "conflict",
     "summary": "service.yaml / catalog 只给 POST /scene-assets/{id}/revisions "
                "与 GET /…/revisions/{revision}，没有拓扑/anchors 子资源，"
                "也没有候选与接受命令",
     "handling": "按方案 §5 拆分命令与查询；**不改 service.yaml/catalog**"
                 "（生成物，改要改 catalog）。契约面待 owner 决定是扩还是收。"},
    {"id": "C13-2", "kind": "needs_owner",
     "summary": "docs/10 的场景质量门「空间可解释、门窗与轴线一致」没说是谁判；"
                "方案假设 S33/S34 的 QC 产出 spatial_check，但 S33 是音效、"
                "S34 是配乐，都不是 QC",
     "handling": "S13 只做**结构化**校验（polygon/挂接/walkable/anchor 位置），"
                 "那部分不需要视觉模型；`quality_gate_ref` 当作不透明引用原样存。"
                 "真正的 QC 服务与结论形状待 owner 定。"},
    {"id": "C13-3", "kind": "needs_owner",
     "summary": "docs/10 未说明是否支持 3D/点云",
     "handling": "明确只做 2D 顶视拓扑 + anchor 相机参数（§2.3）。"
                 "`scale_unit != 'meter'` 直接 422，不做单位换算。"},
    {"id": "C13-4", "kind": "gap",
     "summary": "event-topics.v2.json 只有 asset.scene.revision.accepted，"
                "没有独立的「拓扑变更」事件",
     "handling": "在同一事件里带 `topology_hash` / `topology_changed` / "
                 "`impact_plan_ref`（§5.3）。消费者按这三个字段决定要不要重算。"},
    {"id": "C13-5", "kind": "needs_owner",
     "summary": "docs/10 说「多角度 anchor」，未定义角度集合与相机参数",
     "handling": "按 location_class 分级要求 4/2/1/0 向（§2.1、§4.1 第 2 条），"
                 "并强制 yaw/pitch/hfov/position。`anchor_id` 不收 enum。"},
    {"id": "C13-6", "kind": "needs_owner",
     "summary": "docs/10 把「年代」列为场景内容，未说明 era 变化是 revision "
                "还是新场景",
     "handling": "按 §1.3 判为**场景定义级**：era 存在 `scene_asset` 上，改动"
                 "等于新建 scene_asset_id，不是新 revision。另外每条 revision "
                 "存 `era_snapshot` 供历史解析。需设计方确认。"},
    {"id": "C13-7", "kind": "needs_owner",
     "summary": "S10 的 variant_selector 由 owner 解释；S13 的 lighting 轴与"
                "它的语义需对齐",
     "handling": "约定 `lighting` 字符串就是 selector 键，原样透传、不映射、"
                 "不改写大小写。两边契约统一前，这里是两个词表。"},
    {"id": "C13-8", "kind": "conflict",
     "summary": "service.yaml 的 depends_on 是 [S28,S01]，但拓扑变更实际依赖 "
                "S09 的影响计划",
     "handling": "只校验 `impact_plan_ref` 的**形态**（有值、三键），"
                 "**不做**同步依赖、不 dereference（§6.2 F18 补充）。"
                 "若要 S13 阻断式等待需改 catalog。"},

    # ---------- 事件契约里已登记的 ----------
    {"id": "SCN-1", "kind": "conflict",
     "summary": "impact_plan_ref 写 service_id=S09，但 S09 只有 "
                "impact_analysis，impact_plan 是 S07 的 change_plan；三个 id "
                "列名，示例用第四个前缀 impact_",
     "handling": "原样存、原样发，不解析 id、不 dereference。只要求三键齐且"
                 "id 非空——F18 补充说得很清楚：这道闸门在数据上就是"
                 "「非空引用」。"},
    {"id": "SCN-2", "kind": "conflict",
     "summary": "`:set-canonical` 能不产生 revision、不发任何事件地移动 "
                "canonical；而 S13 只有一个 topic。`became_canonical=false` 时 "
                "`canonical_revision` 的含义也未定义",
     "handling": "事件只在接受 revision 时发；`:set-canonical` 不发事件（方案"
                 "如此）。按事件缓存 canonical 的消费方必须回读 "
                 "`GET /scene-assets/{id}/canonical`。这条限制写进 service.md。"},
    {"id": "SCN-3", "kind": "conflict",
     "summary": "era 在注释、事件、读响应里有三种形状；被 `scene_by_era` "
                "索引查询的 `year_band` 不进事件；era 与 era_snapshot 的关系"
                "没写",
     "handling": "库里存完整 era；事件只带 label/year_band/region（契约 "
                 "additionalProperties:false）；读响应带完整 era。"
                 "`era_snapshot` 是封存时的副本，与 scene_asset.era 分开存。"},
    {"id": "SCN-4", "kind": "conflict",
     "summary": "`rights_recheck_required` 只出现在读响应，不在事件里；"
                "`rights_ref` 的键集全仓未定义",
     "handling": "读响应恒为 true（S10/S11 同口径），事件只带 `rights_ref` "
                 "原样。不发明键集，schema 只要求非空对象。"},
    {"id": "SCN-5", "kind": "conflict",
     "summary": "E09-D1 要「拿到新旧 topology_hash 做差异」，事件只有新的一个，"
                "也没有 parent_revision",
     "handling": "接受响应里返回 `previous_topology_hash` 与 "
                 "`parent_revision`（S13 本地读得到），事件不扩键"
                 "（契约 additionalProperties:false）。S09 要做差异必须回读 "
                 "上一版 revision。"},
    {"id": "SCN-6", "kind": "conflict",
     "summary": "impact_plan_ref 必须在 accept 时就有值，而 §4.3 的七条读路径"
                "没有一条能读候选——外部服务推不出这次改了什么",
     "handling": "严格照 §4.3 实现，不新增候选读路径（新增就是改契约面）。"
                 "影响计划的生产方只能拿 accept 命令里调用方转述的内容。"},
    {"id": "SCN-7", "kind": "conflict",
     "summary": "S09/S17 方案从不订阅本事件；唯一确定的消费方 S10 只刷新"
                "集级投影并读两个键",
     "handling": "事件仍按 §5.3 发全 11 键——它是对外承诺，不按已知消费方"
                 "裁剪。不改别的服务的订阅。"},
    {"id": "SCN-8", "kind": "conflict",
     "summary": "scene id 前缀撞车：S05 的分场 scene 与 S13 的场景资产都用 "
                "`scn_`；S10 示例里还有 `loc_`",
     "handling": "本服务按 §2.1 用 `scn_`。跨服务引用一律带 `service_id`"
                 "（`scene_ref` 四键），不靠前缀分辨归属。"},

    # ---------- 实现时新发现的 ----------
    {"id": "S13-1", "kind": "gap",
     "summary": "§2.2 的 `payload` 是「完整 scene-revision.v1」，而 "
                "`contracts/assets/scene-revision.v1.schema.json` 不存在"
                "（§11.1 第 1 条把它列为必须先写的东西）",
     "handling": "`content_hash = sha256(JCS(payload))`，payload 就是候选命令里"
                 "除信封字段外的 revision 内容（source_kind / era_snapshot / "
                 "topology / anchor_manifest / lighting_manifest / "
                 "lighting_axes / fixed_objects / rights_ref / "
                 "quality_gate_ref / topology_changed）。**这是本仓库自定的"
                 "投影**：契约落地后两个实现应该算出同一个 hash，今天不能保证。"},
    {"id": "S13-2", "kind": "gap",
     "summary": "§5.1 列了 `POST /scene-assets/{id}:merge`，而 §2.1 没有"
                "`merged_into` 列、§3 也没有合并后的状态",
     "handling": "补 `scene_asset.merged_into` 列；合并 = 源场景 status 置 "
                 "`deprecated` 并记下目标 id，**不搬 revision、不改目标**。"
                 "指向不存在的目标时 404，不建悬挂引用。"},
    {"id": "S13-3", "kind": "conflict",
     "summary": "`UNIQUE (tenant_id, series_scope, display_name)` 与 §1.3"
                "「改 era = 新场景」叠在一起：同一地点的 1990 版与 2020 版"
                "必须是两个 scene_asset_id，却只能有一个用这个 display_name",
     "handling": "保留唯一约束（§6.1 的「自然幂等」正靠它）；era 变体必须"
                 "用不同 display_name 登记。命名规则方案没给，属于调用方约定，"
                 "不在这里硬编码后缀。"},
    {"id": "S13-4", "kind": "gap",
     "summary": "同一 (series_scope, display_name) 已存在、但 scene_kind / "
                "project_id / location_class / era.label 不同时，§5.4 的 409 "
                "码表没有对应项",
     "handling": "身份字段全同 → 返回既有 scene_asset_id（E12「重复登记返回"
                 "既有 ID」）；有任一不同 → 409 `SCENE_IDENTITY_CONFLICT`。"
                 "新增码待契约补。"},
    {"id": "S13-5", "kind": "gap",
     "summary": "catalog 的 depends_on 含 S01，但 §5.4 没有任何授权失败码，"
                "权限注册表里也没有 scene.* 键（全仓只有 project.get 等 12 个）",
     "handling": "不发明写权限门（发明了就等于让每条写命令在生产里 503）。"
                 "`deps.build_authorizer` 保留注入点与 fail-closed 实现，"
                 "主路径不调用。租户上下文只认 `X-Tenant-Id`。"},
    {"id": "S13-6", "kind": "gap",
     "summary": "`lighting` 与 `anchor_id` 的取值集只存在于 §2.4 的注释里，"
                "两张派生表都没有 CHECK（契约按本仓口径也不收 enum）",
     "handling": "两者都按任意非空字符串处理，不校验取值、不改写大小写。"
                 "新增光照值不需要迁移。"},
    {"id": "S13-7", "kind": "gap",
     "summary": "§4.1 第 1 条要求 opening 的 `wall_segment` 落在 unit 边界，"
                "但拓扑没有墙厚，也没有定义「线段穿过顶点」的判法",
     "handling": "判定为「两端点与中点都贴边」。已知局限：一条两端贴边、"
                 "穿过顶点且中点恰好落在第三条边上的线段会被误判为合法。"
                 "方案没给更细口径，不用未定义的墙厚去补。"},
    {"id": "S13-8", "kind": "needs_owner",
     "summary": "§4.1 第 2 条对 `primary_set` 要求「覆盖 360°」，但没给"
                "角度跨度门槛",
     "handling": "取「两两最小夹角的最大值 >= 30°」作为门槛并写成常量。"
                 "四个正交方向（90°）通过；四个都朝同一面墙（0°）不通过。"
                 "数值待 owner 定。"},
    {"id": "S13-9", "kind": "conflict",
     "summary": "§2.8 的 outbox DDL 声称 `UNIQUE (aggregate_id, "
                "aggregate_sequence, event_type)`，event_sdk 的 OUTBOX_DDL "
                "只有 `event_id` 主键",
     "handling": "同聚合不重号由本服务的 `scene_asset.current_revision` 行锁"
                 "保证，不由 SDK 的约束证明。不把「库里有唯一约束」写成事实。"},
    {"id": "S13-10", "kind": "gap",
     "summary": "§2.6 的 `expires_at DEFAULT now() + interval '30 days'` "
                "在 SQLite 里没有等价物，且 DEFAULT 表达式的输出格式与本服务"
                "的 `…Z` 不一致",
     "handling": "不写 DEFAULT；服务层显式写入 `_now()` 加 30 天的同格式串。"
                 "候选过期（EXPIRED）由读路径的惰性判定处理，不做后台定时器。"},
    {"id": "S13-11", "kind": "gap",
     "summary": "§3.2 的状态机图是 active → deprecated → tombstoned，"
                "而 §3.4 的非法转移表只禁止 tombstoned → active，没说"
                "active 能否直接 tombstone",
     "handling": "deprecate 只从 active 出发（图上的那条边）；tombstone 从"
                 "任何非 tombstoned 状态都允许（它是终态，紧急下架不该要求"
                 "先走一步 deprecate）。tombstoned → 任何状态返回 422 "
                 "`SCENE_TOMBSTONED`。"},
    {"id": "S13-12", "kind": "gap",
     "summary": "§4.2 第 7 步「更新 scene_asset」没说更新哪些列；"
                "§2.1 又把 `topology_hash` 注释成「当前 canonical 的拓扑 hash」",
     "handling": "接受 revision 时只更新 `current_revision`；"
                 "`canonical_revision` 与 `topology_hash` **只有**在 "
                 "`set_canonical=true` 成功时才跟着动。否则新 revision 会"
                 "让 `topology_hash` 指向一个还不是 canonical 的拓扑，"
                 "而它的全部意义就是「当前 canonical 长什么样」。"},
    {"id": "S13-13", "kind": "gap",
     "summary": "§4.1 说「事务外校验（全部在 S13 本地完成，不调下游）」，"
                "但第 5 条 artifact 校验是一次 S28 往返",
     "handling": "S28 调用放在事务**外**（§4.1 的第 5 条是那一节的例外），"
                 "随后在事务内重新锁行并做一次 CAS。代价是「校验通过」与"
                 "「写候选」之间有一个窗口；收益是一次 5 秒超时不会长期持有 "
                 "`scene_asset` 行锁。窗口由事务内的 CAS 兜住：期间推进了 "
                 "current_revision 的候选会被 409 退回重起草。"},
    {"id": "S13-14", "kind": "gap",
     "summary": "§3.4 禁止 `tombstoned` 场景「新 canonical」，但没说下线时"
                "现有的 canonical 指针要不要 supersede",
     "handling": "**不 supersede**。`canonical_pointer` 的历史是「这一版曾经"
                 "是 canonical」的证据，它记录的是过去的决定，不是当前的可用性；"
                 "下线是 `scene_asset.status` 的事。读 `GET /canonical` 时"
                 "响应里的 `status` 让调用方自己决定要不要用（E05-D1 的 gate "
                 "本来就要自己判），而不是由 S13 删掉历史。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- §2.1 场景定义 ----------
-- `era` 是场景定义级属性（§1.3）：改 era 等于新建一行，不是新 revision。
CREATE TABLE IF NOT EXISTS scene_asset (
  tenant_id          TEXT NOT NULL,
  scene_asset_id     TEXT NOT NULL,
  project_id         TEXT NOT NULL,
  series_scope       TEXT NOT NULL,
  display_name       TEXT NOT NULL,
  scene_kind         TEXT NOT NULL CHECK (scene_kind IN (
                       'interior','exterior','hybrid','abstract','vehicle')),
  era                TEXT NOT NULL,
  location_class     TEXT NOT NULL CHECK (location_class IN (
                       'primary_set','recurring_location','one_off','background')),
  status             TEXT NOT NULL CHECK (status IN (
                       'active','deprecated','tombstoned')),
  canonical_revision INTEGER,
  current_revision   INTEGER NOT NULL DEFAULT 0,
  -- §2.1：这份冗余只服务一个问题——「当前 canonical 的空间长什么样」。
  -- 所以它**只在 canonical 移动时**变（见 CONTRACT_GAPS S13-12）。
  topology_hash      TEXT,
  created_by         TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  -- S13-2：`:merge` 需要一个目标列，§2.1 没给。合并后本行 status 置
  -- deprecated，目标 id 存在这里；不搬 revision、不改目标。
  merged_into        TEXT,
  PRIMARY KEY (tenant_id, scene_asset_id),
  UNIQUE (tenant_id, series_scope, display_name),
  CONSTRAINT scene_canonical_within_head CHECK (
    canonical_revision IS NULL OR canonical_revision <= current_revision),
  CONSTRAINT scene_topohash_len CHECK (
    topology_hash IS NULL OR (length(topology_hash) = 64
                              AND topology_hash NOT GLOB '*[^0-9a-f]*')),
  -- 合并只能从非 tombstoned 到另一行；tombstoned 的不能复活也不能被合并走。
  CONSTRAINT scene_merge_not_tombstoned CHECK (
    merged_into IS NULL OR status = 'deprecated')
);
CREATE INDEX IF NOT EXISTS scene_by_series
  ON scene_asset (tenant_id, series_scope, status, location_class);
-- §2.1 的 `(era->>'label')` / `(era->>'year_band')` 表达式索引。
-- SQLite 用 json_extract 表达同一个东西；缺 year_band 时索引键为 NULL。
CREATE INDEX IF NOT EXISTS scene_by_era
  ON scene_asset (tenant_id, json_extract(era, '$.label'),
                  json_extract(era, '$.year_band'));

-- ---------- §2.2 不可变 revision ----------
-- 没有任何 UPDATE 路径：改内容必须新 revision（§3.4「revision 覆写」非法）。
CREATE TABLE IF NOT EXISTS scene_revision (
  tenant_id         TEXT NOT NULL,
  scene_asset_id    TEXT NOT NULL,
  revision          INTEGER NOT NULL CHECK (revision >= 1),
  content_hash      TEXT NOT NULL,
  topology_hash     TEXT NOT NULL,
  payload           TEXT NOT NULL,
  topology          TEXT NOT NULL,
  anchor_manifest   TEXT NOT NULL,
  fixed_objects     TEXT NOT NULL,
  lighting_axes     TEXT NOT NULL,
  lighting_manifest TEXT NOT NULL,
  era_snapshot      TEXT NOT NULL,
  rights_ref        TEXT NOT NULL,
  source_kind       TEXT NOT NULL CHECK (source_kind IN (
                      'manual_upload','generated','imported','derived',
                      'scanned_floorplan')),
  parent_revision   INTEGER,
  -- §2.2 行 177：显式声明，封存时校验（§4.1 第 7 条）。
  topology_changed  INTEGER NOT NULL CHECK (topology_changed IN (0, 1)),
  -- `impact_plan_ref` 在拓扑变更时必填。方案把它放在 accept 命令上而不是
  -- revision 的 DDL 里；没有它，「这次拓扑变更经过了影响分析」在库里
  -- 无处可查，而 §7 的 topology-audit runbook 正是要查这件事。
  impact_plan_ref   TEXT,
  quality_gate_ref  TEXT NOT NULL,
  accepted_at       TEXT NOT NULL,
  accepted_by       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, scene_asset_id, revision),
  FOREIGN KEY (tenant_id, scene_asset_id)
    REFERENCES scene_asset (tenant_id, scene_asset_id),
  CONSTRAINT sr_hash_len CHECK (
    length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
  CONSTRAINT sr_topohash_len CHECK (
    length(topology_hash) = 64 AND topology_hash NOT GLOB '*[^0-9a-f]*'),
  CONSTRAINT sr_parent_lt CHECK (
    parent_revision IS NULL OR parent_revision < revision),
  -- C13-8 / §3.3：拓扑变更必须有人审与影响计划引用。这条在库层面绑住，
  -- 让「policy_auto 的拓扑变更」不可能只是一次应用层疏忽。
  CONSTRAINT sr_topology_change_gated CHECK (
    topology_changed = 0
    OR (impact_plan_ref IS NOT NULL AND impact_plan_ref <> ''))
);
CREATE UNIQUE INDEX IF NOT EXISTS sr_hash_uniq
  ON scene_revision (tenant_id, content_hash);
CREATE INDEX IF NOT EXISTS sr_topology_hash
  ON scene_revision (tenant_id, scene_asset_id, topology_hash);

-- ---------- §2.4 anchor 与光照 ----------
-- 主键带 revision：anchor 一旦封存永不替换（§1.2）。
CREATE TABLE IF NOT EXISTS scene_anchor (
  tenant_id      TEXT NOT NULL,
  scene_asset_id TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  anchor_id      TEXT NOT NULL,
  position_m     TEXT NOT NULL,
  yaw_deg        REAL NOT NULL,
  pitch_deg      REAL NOT NULL DEFAULT 0,
  hfov_deg       REAL NOT NULL,
  unit_id        TEXT NOT NULL,
  looks_at       TEXT,
  artifact_ref   TEXT NOT NULL,
  lighting_kind  TEXT NOT NULL DEFAULT 'day',
  PRIMARY KEY (tenant_id, scene_asset_id, revision, anchor_id),
  FOREIGN KEY (tenant_id, scene_asset_id, revision)
    REFERENCES scene_revision (tenant_id, scene_asset_id, revision)
);
CREATE INDEX IF NOT EXISTS scene_anchor_by_unit
  ON scene_anchor (tenant_id, scene_asset_id, revision, unit_id);

CREATE TABLE IF NOT EXISTS scene_lighting_variant (
  tenant_id      TEXT NOT NULL,
  scene_asset_id TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  anchor_id      TEXT NOT NULL,
  lighting       TEXT NOT NULL,
  artifact_ref   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, scene_asset_id, revision, anchor_id, lighting),
  FOREIGN KEY (tenant_id, scene_asset_id, revision, anchor_id)
    REFERENCES scene_anchor (tenant_id, scene_asset_id, revision, anchor_id)
);

-- ---------- §2.5 固定物 ----------
-- **没有状态列。** 有 is_open / is_damaged 就说明它是道具，归 S14（§1.4）。
CREATE TABLE IF NOT EXISTS scene_fixed_object (
  tenant_id       TEXT NOT NULL,
  scene_asset_id  TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  object_id       TEXT NOT NULL,
  label           TEXT NOT NULL,
  unit_id         TEXT NOT NULL,
  footprint       TEXT NOT NULL,
  height_cm       INTEGER,
  is_load_bearing INTEGER NOT NULL DEFAULT 0
                    CHECK (is_load_bearing IN (0, 1)),
  blocks_walkable INTEGER NOT NULL DEFAULT 1
                    CHECK (blocks_walkable IN (0, 1)),
  description     TEXT,
  PRIMARY KEY (tenant_id, scene_asset_id, revision, object_id),
  FOREIGN KEY (tenant_id, scene_asset_id, revision)
    REFERENCES scene_revision (tenant_id, scene_asset_id, revision)
);
CREATE INDEX IF NOT EXISTS scene_fixed_object_by_unit
  ON scene_fixed_object (tenant_id, scene_asset_id, revision, unit_id);

-- ---------- §2.6 候选与 canonical ----------
CREATE TABLE IF NOT EXISTS scene_candidate (
  tenant_id      TEXT NOT NULL,
  candidate_id   TEXT NOT NULL,
  scene_asset_id TEXT NOT NULL,
  content_hash   TEXT NOT NULL CHECK (length(content_hash) = 64),
  topology_hash  TEXT NOT NULL CHECK (length(topology_hash) = 64),
  payload        TEXT NOT NULL,
  spatial_check  TEXT NOT NULL,
  evaluation     TEXT NOT NULL,
  state          TEXT NOT NULL CHECK (state IN (
                   'PENDING','ACCEPTED','REJECTED','EXPIRED')),
  created_by     TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  expires_at     TEXT NOT NULL,
  PRIMARY KEY (tenant_id, candidate_id),
  FOREIGN KEY (tenant_id, scene_asset_id)
    REFERENCES scene_asset (tenant_id, scene_asset_id)
);
CREATE INDEX IF NOT EXISTS scene_candidate_by_scene
  ON scene_candidate (tenant_id, scene_asset_id, state, created_at DESC);

CREATE TABLE IF NOT EXISTS canonical_pointer (
  tenant_id       TEXT NOT NULL,
  scene_asset_id  TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  topology_hash   TEXT NOT NULL
                    CHECK (length(topology_hash) = 64
                           AND topology_hash NOT GLOB '*[^0-9a-f]*'),
  decided_at      TEXT NOT NULL,
  decided_by      TEXT NOT NULL,
  decision_kind   TEXT NOT NULL CHECK (decision_kind IN (
                    'human_approved','policy_auto','initial_import')),
  reason_text     TEXT,
  prior_canonical INTEGER,
  superseded_at   TEXT,
  PRIMARY KEY (tenant_id, scene_asset_id, revision),
  FOREIGN KEY (tenant_id, scene_asset_id, revision)
    REFERENCES scene_revision (tenant_id, scene_asset_id, revision)
);
-- §3.3 / F8「拓扑变更的 revision 只能靠人审定 canonical」**不**是库约束。
-- 想写成 CHECK 就必须在 CHECK 里查 scene_revision，而 SQLite 明令禁止
-- CHECK 中的子查询（`subqueries prohibited in CHECK constraints`）。
-- 触发器和 CHECK 都不适合跨表：触发器在 `executescript` 建表阶段就要存在，
-- 且会与「接受 revision + 写指针同事务」的顺序耦合。所以这条门在服务层
-- `_assert_topology_change_gated` 里判，**两侧都判**（accept 与 set-canonical），
-- 并用测试把它钉住（test_topology_change_requires_human 等）。
-- §2.6：任何时刻至多一个未 supersede 的 canonical。
CREATE UNIQUE INDEX IF NOT EXISTS scene_canonical_one
  ON canonical_pointer (tenant_id, scene_asset_id)
  WHERE superseded_at IS NULL;

-- ---------- §2.7 anchor 关系（可重建，不是连续性结论） ----------
CREATE TABLE IF NOT EXISTS anchor_relationship (
  tenant_id        TEXT NOT NULL,
  scene_asset_id   TEXT NOT NULL,
  revision         INTEGER NOT NULL,
  from_anchor      TEXT NOT NULL,
  to_anchor        TEXT NOT NULL,
  relative_yaw_deg REAL NOT NULL,
  shared_features  TEXT NOT NULL,
  PRIMARY KEY (tenant_id, scene_asset_id, revision, from_anchor, to_anchor),
  FOREIGN KEY (tenant_id, scene_asset_id, revision, from_anchor)
    REFERENCES scene_anchor (tenant_id, scene_asset_id, revision, anchor_id),
  FOREIGN KEY (tenant_id, scene_asset_id, revision, to_anchor)
    REFERENCES scene_anchor (tenant_id, scene_asset_id, revision, anchor_id),
  -- 自己到自己没有「相对偏航」。留一行 from=to 会让 S17 的遍历多一圈空转。
  CONSTRAINT ar_distinct CHECK (from_anchor <> to_anchor)
);
"""
