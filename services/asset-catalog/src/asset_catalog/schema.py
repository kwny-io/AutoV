"""S10 自己的 schema。表来源是 `plans/S10-asset-catalog.md` §2。

三条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **这张库里没有内容、没有 canonical、没有权利状态。** 方案 §2.9 点名不许建的
   四张表（`asset_content_cache` / `rights_status` / `asset_canonical_pointer` /
   `binding_set_working_copy`）一张都没有。`binding_entry` 只有 `owner_hash` 与
   `rights_ref` 两枚指针；`rights_ref` 被原样冻结进 revision，S10 从不解释它是否
   仍然有效（§0.2 第 1 条）。
2. **revision 不可变，草稿不落 revision。** `binding_set_revision.state` 只有
   `SEALED` / `SUPERSEDED` 两态（§2.3）。草稿是 `binding_entry` 里
   `revision_floor = head + 1` 的那一列，封存后就不再可写。
3. **展开是一次性的。** `binding_expansion` 按 revision 存最终结果，
   `resolved_from` 记哪一层胜出；`resolve` 只读它，不重算继承（§1.2、§9 反模式 3）。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`boolean` 存 INTEGER
0/1、`bigint` 存 INTEGER。`text[]` 存 JSON 数组文本。Postgres 的
`content_hash ~ '^[0-9a-f]{64}$'` 翻成 `length(...) = 64`（SQLite 没有正则 CHECK，
用长度兜住最常见的截断错误；字符集由写入路径校验）。
"""

ID_PREFIX = {
    "asset": "ast_",
    "binding_set": "bs_",
    "local_event": "lev_",
}

# ---------------------------------------------------------------- 类型注册表
#
# §2.1：这是**随服务发布版本化的常量**，不是租户可写表。新增资产类型要发版并改
# `contracts`；所以它同时以 Python 常量（写入路径据此校验）和一张表（供结构性
# 查询与 FK）存在，两边由 `_seed_registry` 对齐。
ASSET_TYPES = {
    "character": {
        "owner_service": "S11", "id_prefix": "chr_",
        "required_for": ["all_shots"], "max_active_per_role": 1,
    },
    "costume": {
        "owner_service": "S12", "id_prefix": "cos_",
        "required_for": ["all_shots"], "max_active_per_role": 1,
    },
    "scene": {
        "owner_service": "S13", "id_prefix": "scn_",
        "required_for": ["all_shots"], "max_active_per_role": 1,
    },
    "prop": {
        "owner_service": "S14", "id_prefix": "prp_",
        "required_for": ["dialogue_shot"], "max_active_per_role": 1,
    },
    "voice": {
        "owner_service": "S15", "id_prefix": "vox_",
        "required_for": ["dialogue_shot"], "max_active_per_role": 1,
    },
    "adaptation": {
        "owner_service": "S48", "id_prefix": "adp_",
        "required_for": ["series"], "max_active_per_role": 1,
    },
}

# 哪些 owner_service 有可用的 revision 读取契约。`deps.build_revision_source`
# 只认这几个；`S48` 没有 `GET .../revisions/{rev}`，所以在真实配置下必然 503，
# 见 `CONTRACT_GAPS` 的 `S10-18`。
REVISION_READABLE = ("S11", "S12", "S13", "S14", "S15")

SCOPE_SPECIFICITY = {"shot": 4, "scene": 3, "episode": 2, "series": 1}
SCOPES = ("series", "episode", "scene", "shot")
# §2.5：默认值表刻意不含 shot 层。
DEFAULT_SCOPES = ("series", "episode", "scene")

IDENTITY_STATUSES = ("active", "deprecated", "merged_into", "tombstoned")
REVISION_STATES = ("SEALED", "SUPERSEDED")
BINDING_SET_STATES = ("OPEN", "ARCHIVED")

LIFECYCLE_KINDS = ("registered", "deprecated", "merged", "tombstoned",
                   "un_tombstoned")

# §2.8 的 consumer 名。按用途分开，重放 usage 不会误挡别的消费者。
CONSUMER_MANIFEST_USAGE = "s10.manifest-usage"

# 一次封存最多多少条展开行。§11.2 第 3 条说上限未定，这里给一个明确值，
# 见 `CONTRACT_GAPS` 的 `S10-17`。
MAX_EXPANSION_ROWS = 5000

# `variant_selector` 的轴上界只为防止把 payload 撑爆；内容不解释（§2.4）。
MAX_VARIANT_AXES = 32

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §2.1 类型注册表（代码发布，不可租户自造）
CREATE TABLE IF NOT EXISTS asset_type_registry (
  asset_type          TEXT PRIMARY KEY,
  owner_service       TEXT NOT NULL,
  id_prefix           TEXT NOT NULL,
  required_for        TEXT NOT NULL,
  max_active_per_role INTEGER NOT NULL
);

-- §2.2 全局资产身份
CREATE TABLE IF NOT EXISTS asset_identity (
  tenant_id       TEXT NOT NULL,
  asset_id        TEXT NOT NULL,
  asset_type      TEXT NOT NULL REFERENCES asset_type_registry(asset_type),
  owner_service   TEXT NOT NULL,
  owner_local_id  TEXT NOT NULL,
  series_scope    TEXT NOT NULL,
  status          TEXT NOT NULL
                  CHECK (status IN ('active','deprecated','merged_into',
                                    'tombstoned')),
  merged_into     TEXT,
  created_at      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, asset_id),
  UNIQUE (tenant_id, owner_service, owner_local_id),
  CONSTRAINT merged_has_target
    CHECK ((status = 'merged_into') = (merged_into IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS asset_identity_lookup
  ON asset_identity (tenant_id, owner_service, owner_local_id);
CREATE INDEX IF NOT EXISTS asset_identity_series
  ON asset_identity (tenant_id, series_scope, asset_type, status);

-- §2.2 别名（只用于检索与导入对账，**不用于绑定解析**）
CREATE TABLE IF NOT EXISTS asset_alias (
  tenant_id   TEXT NOT NULL,
  asset_id    TEXT NOT NULL,
  alias_text  TEXT NOT NULL,
  alias_kind  TEXT NOT NULL
              CHECK (alias_kind IN ('display','legacy_id','import_alias')),
  source      TEXT NOT NULL,
  PRIMARY KEY (tenant_id, asset_id, alias_text, alias_kind)
);
CREATE INDEX IF NOT EXISTS asset_alias_lookup
  ON asset_alias (tenant_id, alias_text);

-- §2.3 BindingSet（稳定身份）。`state` 与 `archived_at` 是方案 DDL 缺的列，
-- 而 §3.1 / F11 要求 OPEN/ARCHIVED 的区分 —— 见 CONTRACT_GAPS S10-3。
CREATE TABLE IF NOT EXISTS binding_set (
  tenant_id        TEXT NOT NULL,
  binding_set_id   TEXT NOT NULL,
  project_id       TEXT NOT NULL,
  series_id        TEXT NOT NULL,
  episode_id       TEXT NOT NULL,
  branch_id        TEXT NOT NULL,
  head_revision    INTEGER NOT NULL DEFAULT 0,
  current_revision INTEGER,
  state            TEXT NOT NULL DEFAULT 'OPEN'
                   CHECK (state IN ('OPEN','ARCHIVED')),
  archived_at      TEXT,
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, binding_set_id),
  UNIQUE (tenant_id, series_id, episode_id, branch_id),
  CONSTRAINT head_not_ahead
    CHECK (current_revision IS NULL OR current_revision <= head_revision)
);
CREATE INDEX IF NOT EXISTS binding_set_by_episode
  ON binding_set (tenant_id, series_id, episode_id, branch_id);

-- §2.3 不可变 revision。payload 不含 revision 本身：把 revision 写进被哈希的
-- 对象会让"同一内容"永远算出不同 hash，`bsr_hash_uniq` 就永远不触发。
CREATE TABLE IF NOT EXISTS binding_set_revision (
  tenant_id          TEXT NOT NULL,
  binding_set_id     TEXT NOT NULL,
  revision           INTEGER NOT NULL CHECK (revision >= 1),
  state              TEXT NOT NULL CHECK (state IN ('SEALED','SUPERSEDED')),
  content_hash       TEXT NOT NULL CHECK (length(content_hash) = 64),
  payload            TEXT NOT NULL,
  entry_count        INTEGER NOT NULL,
  expanded_from      TEXT NOT NULL,
  rights_pointer_set TEXT NOT NULL,
  base_revision      INTEGER,
  sealed_by          TEXT NOT NULL,
  sealed_at          TEXT NOT NULL,
  PRIMARY KEY (tenant_id, binding_set_id, revision),
  CONSTRAINT bsr_base_lt
    CHECK (base_revision IS NULL OR base_revision < revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS bsr_hash_uniq
  ON binding_set_revision (tenant_id, content_hash);
CREATE INDEX IF NOT EXISTS bsr_state_idx
  ON binding_set_revision (tenant_id, binding_set_id, state, revision DESC);

-- §2.4 绑定条目（overlay / 草稿）。`scope_path` 与 `unverified` 是实现补充，
-- 见 CONTRACT_GAPS S10-4 / S10-5。
CREATE TABLE IF NOT EXISTS binding_entry (
  tenant_id        TEXT NOT NULL,
  binding_set_id   TEXT NOT NULL,
  revision_floor   INTEGER NOT NULL,
  scope_kind       TEXT NOT NULL
                   CHECK (scope_kind IN ('series','episode','scene','shot')),
  scope_id         TEXT NOT NULL,
  role_key         TEXT NOT NULL,
  asset_type       TEXT NOT NULL,
  owner_service    TEXT NOT NULL,
  owner_local_id   TEXT NOT NULL,
  owner_revision   INTEGER NOT NULL CHECK (owner_revision >= 1),
  owner_hash       TEXT NOT NULL CHECK (length(owner_hash) = 64),
  rights_ref       TEXT NOT NULL,
  variant_selector TEXT NOT NULL DEFAULT '{}',
  required         INTEGER NOT NULL DEFAULT 1,
  note             TEXT,
  scope_path       TEXT NOT NULL DEFAULT '{}',
  unverified       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, binding_set_id, revision_floor, scope_kind,
               scope_id, role_key)
);
CREATE INDEX IF NOT EXISTS binding_entry_scope
  ON binding_entry (tenant_id, binding_set_id, scope_kind, scope_id);

-- §2.4 一次性展开的派生表（与 revision.payload 同源，可重建）
CREATE TABLE IF NOT EXISTS binding_expansion (
  tenant_id        TEXT NOT NULL,
  binding_set_id   TEXT NOT NULL,
  revision         INTEGER NOT NULL,
  scope_kind       TEXT NOT NULL,
  scope_id         TEXT NOT NULL,
  role_key         TEXT NOT NULL,
  asset_type       TEXT NOT NULL,
  owner_service    TEXT NOT NULL,
  owner_local_id   TEXT NOT NULL,
  owner_revision   INTEGER NOT NULL,
  owner_hash       TEXT NOT NULL,
  rights_ref       TEXT NOT NULL,
  variant_selector TEXT NOT NULL,
  resolved_from    TEXT NOT NULL
                   CHECK (resolved_from IN ('shot','scene','episode','series')),
  required         INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, binding_set_id, revision, scope_kind, scope_id,
               role_key)
);

-- §2.5 scope 默认值（不含 shot 层）
CREATE TABLE IF NOT EXISTS binding_default (
  tenant_id        TEXT NOT NULL,
  series_id        TEXT NOT NULL,
  scope_kind       TEXT NOT NULL
                   CHECK (scope_kind IN ('series','episode','scene')),
  scope_id         TEXT NOT NULL,
  role_key         TEXT NOT NULL,
  asset_type       TEXT NOT NULL,
  owner_service    TEXT NOT NULL,
  owner_local_id   TEXT NOT NULL,
  owner_revision   INTEGER NOT NULL CHECK (owner_revision >= 1),
  owner_hash       TEXT NOT NULL CHECK (length(owner_hash) = 64),
  rights_ref       TEXT NOT NULL,
  variant_selector TEXT NOT NULL DEFAULT '{}',
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, series_id, scope_kind, scope_id, role_key)
);

-- §2.6 使用关系投影（可重建，非权威）
CREATE TABLE IF NOT EXISTS binding_usage (
  tenant_id         TEXT NOT NULL,
  binding_set_id    TEXT NOT NULL,
  revision          INTEGER NOT NULL,
  manifest_id       TEXT NOT NULL,
  manifest_revision INTEGER NOT NULL,
  project_id        TEXT NOT NULL,
  series_id         TEXT NOT NULL,
  episode_id        TEXT NOT NULL,
  branch_id         TEXT NOT NULL,
  first_seen_at     TEXT NOT NULL,
  source_event_id   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, binding_set_id, revision, manifest_id,
               manifest_revision)
);
CREATE INDEX IF NOT EXISTS binding_usage_by_owner
  ON binding_usage (tenant_id, series_id, episode_id);

-- 投影水位与缺口标记。方案 §4.4 要在 resolve 里返回 projection_watermark，
-- 但 §2 没有这张表 —— 见 CONTRACT_GAPS S10-6。
CREATE TABLE IF NOT EXISTS usage_projection_state (
  tenant_id        TEXT NOT NULL,
  series_id        TEXT NOT NULL,
  episode_id       TEXT NOT NULL,
  branch_id        TEXT NOT NULL,
  applied_sequence INTEGER NOT NULL DEFAULT 0,
  last_event_at    TEXT NOT NULL,
  stale            INTEGER NOT NULL DEFAULT 0,
  stale_reason     TEXT,
  PRIMARY KEY (tenant_id, series_id, episode_id, branch_id)
);

-- §2.7 生命周期（追加写，不更新）
CREATE TABLE IF NOT EXISTS asset_lifecycle_event (
  tenant_id   TEXT NOT NULL,
  asset_id    TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  event_kind  TEXT NOT NULL
              CHECK (event_kind IN ('registered','deprecated','merged',
                                    'tombstoned','un_tombstoned')),
  reason_code TEXT NOT NULL,
  reason_text TEXT,
  actor       TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, asset_id, seq)
);

-- 未在 `event-topics.v2.json` 登记的事件不发中继。`asset.identity.registered`
-- 与 `binding.*` 的生命周期事件只进这张表（C10-3 的结论：不新增 topic）。
-- 与 project-catalog 的 `local_event` 同一形态。
CREATE TABLE IF NOT EXISTS local_event (
  tenant_id      TEXT NOT NULL,
  local_event_id TEXT NOT NULL,
  event_type     TEXT NOT NULL,
  aggregate_id   TEXT NOT NULL,
  payload        TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  consumed_at    TEXT,
  PRIMARY KEY (tenant_id, local_event_id)
);
CREATE INDEX IF NOT EXISTS local_event_unconsumed
  ON local_event (tenant_id, created_at) WHERE consumed_at IS NULL;
"""


def _seed_registry(conn) -> None:
    """把常量表同步进 `asset_type_registry`。

    **只增不改**：已存在的行不覆盖，这样一次错误的代码回滚不会把注册表改回旧
    值。多出来的行（代码里删掉一个类型）也不删 —— 删了会让历史 binding 的 FK
    找不到父行。差异由 `check_all` 之外的 runbook 比对，不在这里静默修。
    """
    for asset_type, spec in ASSET_TYPES.items():
        conn.execute(
            "INSERT OR IGNORE INTO asset_type_registry"
            " (asset_type, owner_service, id_prefix, required_for,"
            "  max_active_per_role) VALUES (?,?,?,?,?)",
            (asset_type, spec["owner_service"], spec["id_prefix"],
             _jcs(spec["required_for"]), spec["max_active_per_role"]))


def _jcs(value) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


# ------------------------------------------------------------------- 缺口
#
# 本轮**不解决**的缺口。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要
# 自行选边」：本服务按方案散文实现，把偏差记在这里，不改契约、不代填决定。
#
# 前缀含义：`C10-*` 是方案 §10 自己登记的；`ABS-*` 是
# `contracts/asset.binding-set-created.v2.schema.json` 的 `x-conflicts`；
# `S10-*` 是实现时新发现的。
CONTRACT_GAPS = [
    {"id": "C10-1", "kind": "conflict",
     "summary": "service.yaml / catalog 只给了一个命令 `POST /binding-sets`，而封存是独立动作",
     "handling": "拆成 `POST /binding-sets`（建容器）与 `POST /binding-sets/{id}/revisions`（封存）；契约未改。"},
    {"id": "C10-2", "kind": "conflict",
     "summary": "`openapi.v2.yaml` 完全没有 S10 的任何路径",
     "handling": "按方案 §5 新增 HTTP 层；未改契约。"},
    {"id": "C10-3", "kind": "conflict",
     "summary": "topic 只登记 `asset.binding-set.created`，但 S07/S09 需要感知绑定变化；每次封存都发同一个 `created`",
     "handling": "复用 `created`（每次封存都是「新 revision created」）；身份/生命周期/归档/草稿变更只写 `local_event`，不发中继。"},
    {"id": "C10-4", "kind": "needs_owner",
     "summary": "docs/10 说绑定引用 `rights_revision`，但没定义它的拥有者与形态",
     "handling": "定义为 `rights_ref{rights_owner,rights_id,rights_revision,rights_hash}` 四键；音色指向 S15，其余指向 S02 政策。S10 只存不判。"},
    {"id": "C10-5", "kind": "needs_owner",
     "summary": "docs/01 与 docs/10 对「检索投影可否含 owner 的标签/别名」未定义",
     "handling": "投影只含 ID、别名、owner revision 指针、使用集；**不含外观描述、参考图 URL、音色样例**。"},
    {"id": "C10-6", "kind": "conflict",
     "summary": "`episode-manifest.v2` 的 `binding_set_ref` 不要求 `rights_revision`，而 docs/10 要求绑定固定权利版本",
     "handling": "权利指针集合的 hash 折进 binding revision 的 `content_hash`，manifest 间接固定权利版本；不新增 manifest 字段。"},
    {"id": "C10-7", "kind": "conflict",
     "summary": "`depends_on` 是 S11–S15，但 usage 投影需要读 S08",
     "handling": "事件消费不算启动依赖；回源 S08 的读路径由 `manifest_source` 注入，未配置时只影响 usage 投影，不影响封存与 resolve。"},
    {"id": "ABS-1", "kind": "needs_owner",
     "summary": "`binding-set.v1.schema.json` 不存在，而 `content_hash = sha256(JCS(payload))` 要进 manifest 当溯源锚点",
     "handling": "本服务把 payload 的字段集固定在 `seal` 的 payload 构造里（排序后的展开行 + 坐标 + rights_pointer_set + expanded_from），并把它当作暂定形态；不新建 `contracts/assets/`。"},
    {"id": "ABS-2", "kind": "conflict",
     "summary": "S09 订阅 `asset.binding.changed`，该 topic 不存在；S07 的消费表里也没有 S10",
     "handling": "本服务只发 `asset.binding-set.created`，不发 `asset.binding.changed`。S09 若按错名字订阅会收不到 —— 这是契约侧待裁决项，不在本服务内补一个假 topic。"},
    {"id": "ABS-3", "kind": "needs_owner",
     "summary": "`rights_revision_set_hash` 没有算法、没有去重键、没有排序规则",
     "handling": "本服务取「四键全等去重 + 按 (rights_owner,rights_id,rights_revision,rights_hash) 排序 + sha256(JCS(数组))」。S02 必须采纳同一算法，否则 E30-A3 恒判不一致；契约待写。"},
    {"id": "ABS-4", "kind": "needs_owner",
     "summary": "`entry_count` / `owner_service_counts` 数的是 overlay 还是展开结果，事件里看不出",
     "handling": "取**展开结果**：episode 目标层的展开行数，按 owner_service 分组。事件不带条目级细节，S09 无法据此建 `ASSET_BINDING` 边。"},
    {"id": "ABS-5", "kind": "conflict",
     "summary": "`superseded_revision` 在契约里必填且 `minimum:1`，而首次封存没有旧版本可作废",
     "handling": "首版**省略该字段**（不发编造的 0），因此首版事件会因 `required` 被契约拒。这是契约缺陷，不是实现遗漏。"},
    {"id": "ABS-6", "kind": "conflict",
     "summary": "partition key 在方案正文写 `tenant_id:binding_set_id`，在 catalog 与 outbox DDL 写 `tenant_id:aggregate_id`",
     "handling": "按 catalog 的 `tenant_id:aggregate_id` 拼，因为 `binding_set_id` 在事件侧不作为独立字段出现。"},
    {"id": "ABS-7", "kind": "conflict",
     "summary": "`episode_id` 的写法在各方案不一致（`ep3` / `ep003`），而它跨服务 join",
     "handling": "S10 只收非空字符串，不规定形态；join 统一性要等 S03 定词法。"},
    {"id": "S10-1", "kind": "conflict",
     "summary": "§4.6 F9 要求校验「shot 属于本 episode/scene」，但 S10 没有任何 shot→scene→episode 的权威映射，`episode-manifest.v2` 也不带这个层级",
     "handling": "改为记录并校验**调用方自报**的 `scope_path`：同一 `shot_id` 被声明到两个 scene 时 422 `SCOPE_INCONSISTENT`；未声明时不做任何推断（不猜父子关系）。"},
    {"id": "S10-2", "kind": "conflict",
     "summary": "§4.2 的 entry 按 `revision_floor` 继承，§5.1 的 DELETE 只允许删未封存的 floor —— 一个 role 一旦封存就再也无法移除",
     "handling": "保留继承语义（否则 E02-A2 的「改服装、角色不变」要求每次重写全部条目）；移除只能靠新 revision 里换一个 owner，不能删。DELETE 对已封存的 floor 返回 409 `ENTRY_NOT_EDITABLE`。"},
    {"id": "S10-3", "kind": "conflict",
     "summary": "§2.3 的 `binding_set` DDL 没有 state 列，而 §3.1 与 F11 要求 OPEN/ARCHIVED",
     "handling": "补 `state` 与 `archived_at` 两列。"},
    {"id": "S10-4", "kind": "conflict",
     "summary": "`binding_entry` 的 DDL 无法表达 S10-1 需要的父子 scope 关系",
     "handling": "补 `scope_path` 列（JSON，`{scene_id,episode_id,series_id}`），仅用于一致性校验，不参与 hash 之外的任何语义。"},
    {"id": "S10-5", "kind": "conflict",
     "summary": "§4.2 说草稿阶段「记录 unverified: true」，但 DDL 没有这一列",
     "handling": "补 `unverified` 列；封存时 `verify_owners=false` 的条目一律置 1，并在 revision 的 `expanded_from.owner_verification` 记 `deferred`。"},
    {"id": "S10-6", "kind": "conflict",
     "summary": "§4.4 要求 resolve 返回 `projection_watermark` 与 stale 语义，§2 没有承载它的表",
     "handling": "补 `usage_projection_state`（含 `stale` / `stale_reason`）。乱序事件照常落库并置 stale，不假装按序处理。"},
    {"id": "S10-7", "kind": "conflict",
     "summary": "§2.8 的 `idempotency_record` 有 command_type/state/response_code/expires_at，平台 `idempotency-sdk` 只建 (tenant_id,command_id,request_hash,result,created_at)",
     "handling": "用平台 SDK 的表（与其他服务一致），不另建一张同名不同形的表；`expires_at` 的 30 天回收没有实现。"},
    {"id": "S10-8", "kind": "conflict",
     "summary": "§5.1 的命令表里没有写默认值的入口，而 §2.5 定义了 `binding_default`、E08-A4 要求「全剧默认换外观」",
     "handling": "新增 `POST /binding-sets/{id}/defaults`（写入该 binding set 的 series 级默认），契约未登记。"},
    {"id": "S10-9", "kind": "conflict",
     "summary": "§2.5 的 `binding_default` 没有 `required` 列，而 `unresolved_required_roles` 需要判定「这个 role 是不是必需的」",
     "handling": "默认值一律视为必需；条目按 `binding_entry.required` 覆盖。两者冲突时以条目为准。"},
    {"id": "S10-10", "kind": "conflict",
     "summary": "§8.7 要求乱序 manifest 事件「按 aggregate_sequence 缓冲」，但缓冲需要缺口超时与回源 S08 的契约，两者都不存在",
     "handling": "照常落库（`manifest_revision` 在 PK 里，两个版本都留），把该集的投影置 `stale=true` 并在 resolve 里如实返回；不实现一个会静默丢事件的假缓冲。"},
    {"id": "S10-11", "kind": "conflict",
     "summary": "§5.3 消费表说按 `binding_set_ref` upsert，但 `snapshot.manifest.committed` 的 payload 里没有这个字段（S08 的 x-needs-owner 与 SMC 条目也未补）",
     "handling": "`data.binding_set_ref` 存在就直接用；不存在时通过注入的 `manifest_source` 回源 `GET /manifests/{id}/revisions/{rev}`。未配置时该事件只记缺口（该集置 stale），不影响封存与 resolve。"},
    {"id": "S10-12", "kind": "conflict",
     "summary": "§2.1 的 `required_for` 用了 `{dialogue_shot}` / `{all_shots}` 这类值，但全仓没有镜头种类词表，S10 也不知道哪个镜头是对话镜头",
     "handling": "该列只落库、**不参与任何计算**；`unresolved_required_roles` 改为从 `binding_entry.required` 与实际展开结果求差。词表待 owner 定。"},
    {"id": "S10-13", "kind": "conflict",
     "summary": "§4.5 的 usage 响应含 `episode_order`，但 S10 能读到的来源（自己的 binding_set、S08 事件）都没有它",
     "handling": "不返回该字段，也不猜。要它得由 S03 的 episode 顺序提供，S10 不复制。"},
    {"id": "S10-14", "kind": "conflict",
     "summary": "§3.3 只给 `deprecated → tombstoned` 这条边，而 docs/10 的「授权撤销 → 禁止新调用」经常发生在 active 资产上",
     "handling": "按方案只允许 `deprecated → tombstoned`；`active → tombstoned` 返回 409 `ILLEGAL_TRANSITION`。与 S11 在 §3.3 上遇到的同一处缺边一致，等 owner 补。"},
    {"id": "S10-15", "kind": "conflict",
     "summary": "§3.1 的 `OPEN → ARCHIVED` 要求「S08 确认该集没有未完成分支」，而 S10 没有这条查询，catalog 也没有 S08 依赖",
     "handling": "归档命令必须带 `confirm_no_open_branches: true`，否则 409 `ARCHIVE_NOT_CONFIRMED`。S10 不假装自己验证过。"},
    {"id": "S10-16", "kind": "conflict",
     "summary": "`merged_into` 的目标约束（同类型、必须 active、不能是自身）方案没写，§5.4 也没有对应错误码",
     "handling": "新增 422 `MERGE_TARGET_INVALID`；要求同 `asset_type` 且目标 `status='active'`。"},
    {"id": "S10-17", "kind": "needs_owner",
     "summary": "一次封存的条目上限未定（§11.2 第 3 条）",
     "handling": "取 `MAX_EXPANSION_ROWS = 5000`，超过返回 422 `EXPANSION_TOO_LARGE`；与 S08 的 manifest 体量一起定。"},
    {"id": "S10-18", "kind": "conflict",
     "summary": "注册表含 `adaptation → S48`，但 S48 没有 `GET .../revisions/{rev}` 形状可读",
     "handling": "`deps.build_revision_source` 只映射 S11–S15；S48 的条目在 `verify_owners=true` 时 503 `DEPENDENCY_UNAVAILABLE`，不伪造一个 content_hash。"},
    {"id": "S10-19", "kind": "conflict",
     "summary": "`series_scope='*'`（跨剧复用）在 §11.2 第 2 条仍是未决问题",
     "handling": "接受并原样存储，不解释语义；检索与 resolve 不因 `*` 放宽任何租户或 series 边界。"},
    {"id": "S10-20", "kind": "conflict",
     "summary": "`role_key` 的规范化只在 §1.3 的例子里出现，没有 schema 或正则契约",
     "handling": "强制 `role_key == '<asset_type>:<owner_local_id>'`，不符则 422 `ROLE_KEY_INVALID`；这是 §1.3 的逐字要求，但契约侧没有落地。"},
    {"id": "S10-21", "kind": "conflict",
     "summary": "§4.4 的路径是 `GET /assets/{id}/resolve`，而响应体是一整个 scope 的 `entries[]`/`unresolved_required_roles`，两者语义不同",
     "handling": "路径里的资产只用来标注 `asset_ref`（该资产在这个 scope 里对应哪一条），不裁剪 `entries[]`；`variant_role` 才是裁剪参数。"},
    {"id": "S10-22", "kind": "conflict",
     "summary": "§5.2 没有检索入口，而 §0.1 说 S10 持有检索投影、§2.2 建了 `asset_alias`",
     "handling": "新增 `GET /assets?alias_text=&asset_type=&series_scope=&status=`；别名只用于检索，**绝不用于绑定解析**（§9 反模式 7）。"},
    {"id": "S10-23", "kind": "conflict",
     "summary": "§4.3 的 F7「两个前端并发封存相同内容」在 payload 含 `binding_set_id` 时不可达：两个不同容器的 hash 永不相等，唯一索引只能挡住同一容器重封同一份草稿",
     "handling": "保留 `bsr_hash_uniq`（它挡的正是重投/重试与「改回上一版」）；但把 F7 的可达形态登记为「同一容器草稿未变再封一次」。若真要跨容器查重，得把 `binding_set_id` 从被哈希的 payload 里拿掉 —— 那会让同一个 binding set 的两版也可能撞 hash，需要 owner 裁决，本服务不擅自改 payload 形状。"},
]
