"""S03 自己的 schema。表来源是 `plans/S03-project-catalog.md` §2。

四条结构性事实，它们决定了这张库能证明什么、不能证明什么：

1. **`episode` 表没有 `episode_number` 列。** §2.3 行 197 刻意不建：任何"第几集"
   都必须从 `episode_order` 查。一个顺手能用的整数列会被下游当成权威，重排后
   全部错位（反模式 1）。
2. **`episode_order` 的序号是排期，不是身份。** 重排只动这张表，`episode` 行
   一个字节都不改 —— 这是 §1.1「稳定 ID 在重排后不变」在存储层的形态。
3. **规格是物化的。** 三层 `spec_revision` 在写入时合并进 `episode_spec_effective`，
   不是读时 join（§1.4）。改项目默认画幅不会悄悄改在跑的集（反模式 6）。
4. **这里没有 accepted 指针。** 剧本/分镜接不接受是 S08 的事实（反模式 4）。
   `episode.lifecycle='completed'` 只是它的投影，因此**可回退**（§3.2）。

本仓服务层一律 SQLite（`isolation_level=None` + 显式 `Transaction`），所以方案里的
`jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`tstzrange` 存一个 JSON 对象，
`DEFERRABLE INITIALLY DEFERRED` 用「整体 +1000000 偏移再写回 1..N」代替
（§4.3 行 548 已经说明显式偏移比依赖约束时机更可靠；SQLite 本来也没有延迟约束）。
`outbox` / `inbox` / `idempotency_record` 由平台 SDK 建表，这里不重复 DDL。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §2.1 项目。`slug` 可改、`project_id` 不可改；`head_revision` 是元数据 CAS 的
-- 乐观锁，与 S08 的 manifest revision 完全无关（§6.2）。
CREATE TABLE IF NOT EXISTS project (
  tenant_id       TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  slug            TEXT NOT NULL,
  display_name    TEXT NOT NULL,
  lifecycle       TEXT NOT NULL CHECK (lifecycle IN
                    ('draft','active','suspended','archived')),
  org_unit_id     TEXT NOT NULL,
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  head_revision   INTEGER NOT NULL DEFAULT 1 CHECK (head_revision >= 1),
  archived_at     TEXT,
  archive_reason  TEXT,
  PRIMARY KEY (tenant_id, project_id),
  UNIQUE (tenant_id, slug),
  -- 归档标记与归档时间是同一个事实的两种表示，不许只出现一个。
  CHECK ((lifecycle = 'archived') = (archived_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS project_by_org
  ON project (tenant_id, org_unit_id, lifecycle);

-- §2.2 剧集。`branch_default` 只是"新建集时建议用哪个分支"，不是分支权威。
CREATE TABLE IF NOT EXISTS series (
  tenant_id       TEXT NOT NULL,
  series_id       TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  title           TEXT NOT NULL,
  series_kind     TEXT NOT NULL CHECK (series_kind IN
                    ('drama','comic_drama','anthology')),
  target_episodes INTEGER,
  lifecycle       TEXT NOT NULL CHECK (lifecycle IN
                    ('draft','active','suspended','archived')),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  head_revision   INTEGER NOT NULL DEFAULT 1 CHECK (head_revision >= 1),
  branch_default  TEXT NOT NULL DEFAULT 'canon',
  PRIMARY KEY (tenant_id, series_id),
  FOREIGN KEY (tenant_id, project_id)
    REFERENCES project (tenant_id, project_id),
  UNIQUE (tenant_id, project_id, title),
  CHECK (target_episodes IS NULL OR target_episodes > 0)
);

CREATE INDEX IF NOT EXISTS series_by_project
  ON series (tenant_id, project_id, lifecycle);

-- §2.3 单集。**没有 episode_number 列**，见模块 docstring 第 1 条。
-- `project_id` 是冗余列：写入时由 series_id 派生并在同一事务内校验，让
-- "这一集属于哪个项目"可以单表回答（§2.3 行 199）。
CREATE TABLE IF NOT EXISTS episode (
  tenant_id         TEXT NOT NULL,
  episode_id        TEXT NOT NULL,
  series_id         TEXT NOT NULL,
  project_id        TEXT NOT NULL,
  title             TEXT,
  lifecycle         TEXT NOT NULL CHECK (lifecycle IN
                      ('draft','in_production','completed','archived')),
  story_window_ref  TEXT,
  release_window    TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  head_revision     INTEGER NOT NULL DEFAULT 1 CHECK (head_revision >= 1),
  archived_at       TEXT,
  PRIMARY KEY (tenant_id, episode_id),
  FOREIGN KEY (tenant_id, series_id)
    REFERENCES series (tenant_id, series_id),
  CHECK ((lifecycle = 'archived') = (archived_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS episode_by_series
  ON episode (tenant_id, series_id, lifecycle);

-- §2.4 顺序。两条唯一约束做的是不同的事：
--   * (series, kind, window, ordinal)  —— 一个位置只能放一集
--   * (series, kind, window, episode_id)—— 一集在一个顺序里只能出现一次
-- 方案把前者写成 DEFERRABLE，SQLite 没有延迟约束，改用显式 +1000000 偏移
-- （§4.3 行 535–548）。后者**不能延迟**：重复出现不是重排的中间态，是数据错误。
CREATE TABLE IF NOT EXISTS episode_order (
  tenant_id    TEXT NOT NULL,
  order_id     TEXT NOT NULL,
  series_id    TEXT NOT NULL,
  order_kind   TEXT NOT NULL CHECK (order_kind IN
                 ('release','shooting','rebroadcast')),
  window_key   TEXT NOT NULL DEFAULT '',
  ordinal      INTEGER NOT NULL CHECK (ordinal >= 1),
  episode_id   TEXT NOT NULL,
  assigned_by  TEXT NOT NULL,
  assigned_at  TEXT NOT NULL,
  PRIMARY KEY (tenant_id, order_id),
  FOREIGN KEY (tenant_id, episode_id)
    REFERENCES episode (tenant_id, episode_id),
  UNIQUE (tenant_id, series_id, order_kind, window_key, ordinal),
  UNIQUE (tenant_id, series_id, order_kind, window_key, episode_id),
  -- release 的 window_key 恒为 ''（§2.4 行 209、行 234）：
  -- 用 NULL 会让"同一集在 release 出现两次"绕过唯一约束。
  CHECK (order_kind <> 'release' OR window_key = '')
);

CREATE INDEX IF NOT EXISTS episode_order_by_episode
  ON episode_order (tenant_id, episode_id, order_kind);

-- §2.5 规格 revision。**审计表**：每一次 revision 的全文与作者都在这里。
CREATE TABLE IF NOT EXISTS spec_revision (
  tenant_id     TEXT NOT NULL,
  spec_id       TEXT NOT NULL,
  scope_kind    TEXT NOT NULL CHECK (scope_kind IN
                  ('project','series','episode')),
  scope_id      TEXT NOT NULL,
  revision      INTEGER NOT NULL CHECK (revision >= 1),
  body          TEXT NOT NULL,
  content_hash  TEXT NOT NULL CHECK (length(content_hash) = 64),
  author        TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, spec_id, revision),
  -- 同一作用域下内容相同的 revision 只存一次（T03-35 的幂等来源）。
  UNIQUE (tenant_id, scope_kind, scope_id, content_hash)
);

CREATE INDEX IF NOT EXISTS spec_head
  ON spec_revision (tenant_id, scope_kind, scope_id, revision DESC);

-- §2.5 物化后的生效规格。每集恰好一行；`derived_from` 记录三个来源各自
-- 用了哪个 revision，所以"为什么这集是 16:9"事后可解释（§6.5）。
CREATE TABLE IF NOT EXISTS episode_spec_effective (
  tenant_id       TEXT NOT NULL,
  episode_id      TEXT NOT NULL,
  derived_from    TEXT NOT NULL,
  body            TEXT NOT NULL,
  content_hash    TEXT NOT NULL CHECK (length(content_hash) = 64),
  materialized_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, episode_id),
  FOREIGN KEY (tenant_id, episode_id)
    REFERENCES episode (tenant_id, episode_id)
);

-- §2.6 业务目标。`budget_envelope` 只存引用与上界，金额权威在 S23。
CREATE TABLE IF NOT EXISTS business_goal (
  tenant_id   TEXT NOT NULL,
  goal_id     TEXT NOT NULL,
  scope_kind  TEXT NOT NULL CHECK (scope_kind IN ('project','series')),
  scope_id    TEXT NOT NULL,
  goal_kind   TEXT NOT NULL CHECK (goal_kind IN
                ('episode_count','milestone','budget_envelope','delivery_date')),
  target      TEXT NOT NULL,
  due_at      TEXT,
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                ('active','met','missed','canceled')),
  created_at  TEXT NOT NULL,
  PRIMARY KEY (tenant_id, goal_id)
);

CREATE INDEX IF NOT EXISTS goal_by_scope
  ON business_goal (tenant_id, scope_kind, scope_id, status);

-- §2.7 成员。**回答"谁被指派了"，不回答"谁现在有权限"**（反模式 8）：
-- 撤权、委派过期、企业 kill switch 都在 S01，S03 看不到（E27）。
CREATE TABLE IF NOT EXISTS member (
  tenant_id     TEXT NOT NULL,
  member_id     TEXT NOT NULL,
  project_id    TEXT NOT NULL,
  principal_id  TEXT NOT NULL,
  role_key      TEXT NOT NULL,
  scope_kind    TEXT NOT NULL DEFAULT 'project' CHECK (scope_kind IN
                  ('project','series','episode')),
  scope_id      TEXT,
  granted_by    TEXT NOT NULL,
  granted_at    TEXT NOT NULL,
  revoked_at    TEXT,
  PRIMARY KEY (tenant_id, member_id),
  FOREIGN KEY (tenant_id, project_id)
    REFERENCES project (tenant_id, project_id)
);

-- 同一 (principal, role, scope) 只有一条有效指派。SQLite 的表级 UNIQUE 不允许
-- 表达式，所以用独立索引表达 `COALESCE(scope_id,'')`。
CREATE UNIQUE INDEX IF NOT EXISTS member_identity
  ON member (tenant_id, project_id, principal_id, role_key, scope_kind,
             COALESCE(scope_id, ''));

CREATE INDEX IF NOT EXISTS member_by_principal
  ON member (tenant_id, principal_id, revoked_at);

-- §2.9 场次投影。**可重建、非权威**：任何业务判断都必须直接查 S05
-- （反模式 12：代理时不带 authoritative_source，未来重构者会往这里写数据）。
CREATE TABLE IF NOT EXISTS episode_scene_projection (
  tenant_id     TEXT NOT NULL,
  episode_id    TEXT NOT NULL,
  scene_id      TEXT NOT NULL,
  scene_ordinal INTEGER NOT NULL,
  breakdown_ref TEXT NOT NULL,
  applied_seq   INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, episode_id, scene_id)
);

-- §2.8 投影水位。`applied_seq` 只前进（T03-30：乱序到达不倒退）。
CREATE TABLE IF NOT EXISTS projection_watermark (
  tenant_id   TEXT NOT NULL,
  source      TEXT NOT NULL,
  sequence    INTEGER NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (tenant_id, source)
);

-- 本地事件的落点。**不是 outbox**：`project.episode.reordered` 与
-- `project.lifecycle.changed` 在 `event-topics.v2.json` 里没有登记（PEC-6 /
-- C03-3），`catalog.spec.changed` 方案自己说只发本地 topic（§4.5 行 588）。
-- 把未登记的 event_type 塞进 outbox 等于让中继去发一个没有契约的事件。
-- 本地消费方（S07）按 tenant + 游标拉这里，`registered` 永远为 false。
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

# ---------------------------------------------------------------- 常量

PROJECT_LIFECYCLES = ("draft", "active", "suspended", "archived")
SERIES_LIFECYCLES = PROJECT_LIFECYCLES
# `completed` **不是终态**：它是 S08 accepted master 的投影，可回退（§3.2）。
EPISODE_LIFECYCLES = ("draft", "in_production", "completed", "archived")
SERIES_KINDS = ("drama", "comic_drama", "anthology")
ORDER_KINDS = ("release", "shooting", "rebroadcast")

# §2 行 107 点名的六种前缀。`business_goal` 的前缀**方案没写**，
# 所以 `gol_` 是本服务的生成约定，登记在 G13。
ID_PREFIX = {
    "project": "prj_",
    "series": "ser_",
    "episode": "ep_",
    "order": "ord_",
    "member": "mem_",
    "spec": "spc_",
    "goal": "gol_",
    "local_event": "lev_",
}

# §2.5 行 269–284 的字段集。`output` 段与 shot-spec.v1 的 `output_spec` 同名同义
# ——刻意的：S06 用 `episode_spec_effective.output` 当默认值，两边字段名一致
# 就不会出现"目录说 1080×1920、分镜写 720×1280"的静默不一致。
SPEC_SEGMENTS = {
    "output": ("aspect_ratio", "width", "height", "fps",
               "max_episode_seconds", "min_episode_seconds"),
    "language": ("primary", "dub_targets"),
    "delivery": ("loudness_target_lufs", "color_primaries"),
    "default_branch": None,  # 标量，不是段
}

# 项目层是三层合并的基座，缺一段就没有"默认值"可继承。
SPEC_PROJECT_REQUIRED = ("output", "language", "delivery", "default_branch")

# ---------------------------------------------------------------- 权限键
# 抄自 `contracts/identity/permission-registry.v2.json`（40 个键）里 S03 真的
# 会问 S01 的那些。写入类动作的键**一个都不在里面** —— 那是缺口 G5 的事实。
REGISTERED_PERMISSIONS = frozenset({"project.get"})

# 方案 §5.1 的六个命令各自需要的键。名字照 docs 的动词命名法取，注册表里没有，
# 所以 `deps.build_authorizer` 对它们一律 503（G5）。列在这里是为了让缺口可被
# 机器检查：测试会逐条核对它们确实都不在注册表里。
WRITE_PERMISSIONS = {
    "create_project": "project.create",
    "append_episodes": "project.episode.create",
    "reorder_episodes": "project.order.write",
    "publish_spec": "project.spec.publish",
    "archive_project": "project.archive",
    "grant_member": "project.member.grant",
}

READ_PERMISSION = "project.get"

# ---------------------------------------------------------------- 缺口登记
# 每一条都是契约/上游能力的缺口，不是实现自由度。命令响应与查询响应都随身带出，
# 免得"能响应"被读成"契约完整"（与 S40 的 CONTRACT_GAPS 同构）。
CONTRACT_GAPS = [
    "S03 C03-1: service-catalog.v2 declares only POST /projects/{id}/episodes "
    "and GET /projects/{id}/episodes, and openapi.v2.yaml has no S03 path at "
    "all. This service also serves POST /projects, "
    "POST /series/{id}:reorder-episodes, POST /series/{id}/spec-revisions, "
    "POST /projects/{id}:archive, POST /projects/{id}/members and six more GET "
    "paths, all without a contract path.",
    "S03 PEC-1: the registered event project.episode.created requires "
    "spec_hash (^[0-9a-f]{64}$) but the plan never says which object is "
    "hashed. This service fills it with episode_spec_effective.content_hash -- "
    "the materialized effective spec -- because §1.4 makes inheritance "
    "materialized and §2.5 keeps output.* in sync with shot-spec.v1. The plan "
    "must still be edited (rename the field to effective_spec_hash per "
    "contracts/project.episode-created.v2.schema.json's fix_shape, or add an "
    "episode-level spec_revision). Not changed in code alone.",
    "S03 PEC-2: the event's release_ordinal / shooting_ordinal are snapshots of "
    "episode_order.ordinal taken at creation, and reordering emits no "
    "registered event, so a consumer that stores them silently expires on the "
    "first reorder. Consumers must re-read GET /episodes/{id}.",
    "S03 PEC-4: project.episode.created carries no branch_id, while S08's "
    "manifest unique key is (tenant, series, episode, branch). The payload "
    "schema is additionalProperties:false, so this service cannot add it; S08 "
    "must either default to series.branch_default's value or re-query.",
    "S03 G5: contracts/identity/permission-registry.v2.json has no "
    "project.create, no project.admin, and nothing for appending episodes, "
    "reordering, publishing specs or granting membership; the closest key is "
    "project.get (risk=low, the read permission). deps.build_authorizer() "
    "fails closed with 503 PERMISSION_KEY_UNREGISTERED rather than "
    "substituting a read key for a write action -- a caller credential that "
    "may read a project must not thereby be able to create or archive one.",
    "S03 G6: the platform idempotency SDK raises COMMAND_IN_PROGRESS while "
    "plan §5.4 names COMMAND_IN_FLIGHT. The HTTP layer maps it to the plan's "
    "code; the shared SDK is not forked (it is also used by S08/S10).",
    "S03 G7: the SDK idempotency row has no ABANDONED state -- result IS NULL "
    "is 'in progress'. §6.1 asks a 60s sweeper to mark stale rows ABANDONED so "
    "the next send is treated as new; sweep_stale_commands() deletes them, "
    "which produces exactly that stated post-state rather than a third state "
    "the SDK cannot represent.",
    "S03 G8: platform event-sdk's outbox DDL has no "
    "UNIQUE (tenant_id, aggregate_id, sequence) that §2.8 requires. This "
    "service computes the next sequence from the outbox itself inside the "
    "write transaction; the uniqueness is therefore guaranteed by the writer, "
    "not by the table.",
    "S03 G9: project.episode.reordered and project.lifecycle.changed are "
    "marked '新增，待登记' and are absent from event-topics.v2.json (PEC-6); "
    "catalog.spec.changed is explicitly a local topic (§4.5). All three are "
    "written to local_event, never to outbox, because publishing an "
    "unregistered event type is worse than not publishing it.",
    "S03 C03-4: service.yaml's acceptance says archiving a project stops new "
    "Runs, but S03 holds no Run state and cannot stop S21/S26. This service "
    "only exposes the fact (GET /episodes/{id} returns project_lifecycle); the "
    "refusal belongs to the caller's admission path.",
    "S03 G11: T03-26 expects archiving to return warnings:"
    "[{code:ACTIVE_RUNS_EXIST, count:N}]. S21 exposes no run-count query, so "
    "the count comes from an injected run_probe callback. When it is not "
    "configured this service reports no warning at all rather than "
    "ACTIVE_RUNS_EXIST with count 0 -- a fabricated zero would read as "
    "'verified: nothing running'.",
    "S03 G12: §2.5 says the spec field set is '全部必填'. Read literally that "
    "forces every series/episode override to restate all four segments, which "
    "makes the three-layer inheritance pointless. Implemented as: every "
    "segment present in a body must be complete and unknown keys are rejected "
    "(SPEC_FIELD_UNKNOWN), a project-level body must carry all four, "
    "series/episode bodies may be partial, and merging overrides per "
    "top-level segment.",
    "S03 G13: §2 row 107 names the ID prefixes prj_/ser_/ep_/ord_/mem_/spc_ "
    "but says nothing about business_goal or local_event ids. They are "
    "generated as gol_/lev_ here; the event schema deliberately leaves id "
    "charset at minLength 1 for the same reason.",
    "S03 C03-8: episode.lifecycle='completed' is a projection of an S08 "
    "accepted master node, not a fact S03 owns, so it is reversible. "
    "apply_episode_lifecycle_projection() is the only writer of that state; "
    "no S08 event consumer is wired to it yet, so this service never marks an "
    "episode completed on its own.",
]
