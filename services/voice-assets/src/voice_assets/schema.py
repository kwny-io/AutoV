"""S15 自己的 schema。表来源是 `plans/S15-voice-assets.md` §2。

四条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **授权状态不是 revision 的字段，而是独立单行。** §1.2 说得最直白：把
   `authorization` 放进 `voice_revision` 就等于"撤销对旧 revision 失效"，
   因为引用 `rev=2` 的调用方永远看到 `granted`。所以 `voice_authorization`
   的 PK 是 `(tenant_id, voice_id)`，`voice_revision` 只记内容。撤销一个音色，
   它的所有 revision **同时**不可新调用——这是那张单行表在结构上保证的。
2. **`revoked` 是终态，且有 DB 触发器兜底。** §2.3 第 4 条：即便应用层有 bug，
   数据库也不允许撤销过的音色复活。`deny_revoked_transition` 是最后一道防线；
   要恢复只能新建 `voice_id`（新授权对象，历史有清晰分界）。
3. **`expires_at` 的判定在应用层，且必须在读时做。** §2.3 第 3 条把
   `cloned`/`licensed_actor` 必须有期限写成应用层断言（跨表，SQLite 的 CHECK
   表达不了），§4.2 第 6 条把"到期但定时任务没跑"写成读时判定。两条都不在
   本文件里，但都以这里没有"过期清理后才能拒绝"的前提为基础。
4. **映射的粒度是 `model_snapshot`，不是 `model_id`。** §2.5 与反模式 #7。
   同一 provider 同一 model_id 升级到新快照声音可能变了，必须有独立映射与
   独立回归证据，所以快照是主键的一部分。映射只追加、不改（反模式 #8）。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串。`boolean` 存
INTEGER 0/1（SQLite 无原生布尔）。`bigint` 用 INTEGER。
"""

ID_PREFIX = {"voice": "vox_", "candidate": "vcn_", "lexicon": "lex_"}

# 本轮**不解决**的缺口。`contracts/asset.voice-authorization-changed.v2.schema.json`
# 已登记 8 条 `x-conflicts`（VAC-1…VAC-8）与 3 条 `x-needs-owner`，方案 §10 另有
# C15-1…C15-8。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要自行选边」：
# 本服务按方案散文实现，把偏差记在这里，不修改契约、不代填决定。
CONTRACT_GAPS = [
    # --- 方案 §10 已登记 ---
    {"id": "C15-1", "kind": "conflict",
     "summary": "service.yaml 的命令只有 POST /voices/{id}/revisions，没有"
                "调用前资格检查；docs/10 却要求'S02/S15 对可撤销资格做实时检查'",
     "handling": "实现 §4.2 的 POST /voices/{id}:check-eligibility（本服务"
                 "最重要的接口）。契约面待补，catalog 未改。"},
    {"id": "C15-2", "kind": "conflict",
     "summary": "service.yaml 的查询是 GET /voices/{id}/bindings，与 S11–S14 的"
                "/revisions/{revision} 不一致；且只有 authorization.changed，"
                "没有 revision accepted 事件",
     "handling": "两条查询都提供；revision 接受**不发事件**，订阅方只能显式查询"
                 "（S10 投影 / S17 缓存无处订阅，这是缺口不是已完成）。"},
    {"id": "C15-3", "kind": "needs_owner",
     "summary": "docs/10 说'撤销事件禁止新调用'，未定义未来 effective_at 的语义",
     "handling": "按 §4.4 的选择：立即拒绝新调用，effective_at 仅入审计列"
                 "（authorization_event.effective_at）；宽限期由 S02 策略处理。"},
    {"id": "C15-4", "kind": "conflict",
     "summary": "供应商映射变更（下架/恢复）没有契约事件，S24/S25 如何得知未定义",
     "handling": "§4.5 要求发本地事件 `voice.provider-mapping.changed`，但本仓"
                 "没有本地 topic 注册表，outbox 只有契约 topic 的发布路径；"
                 "本轮只提供同步查询（映射行带 added_at 与 mapping_status），"
                 "不往 outbox 塞非契约 type。"},
    {"id": "C15-5", "kind": "conflict",
     "summary": "docs/10 说资格检查由 S02/S15 执行，plans/S02 的 rights_current "
                "来源却写 S10:binding_set@r12",
     "handling": "本服务按'判定归 S15、S02 实时调用'实现；S02 方案侧不一致由"
                 "契约方裁决，S15 不替 S02 存投影。"},
    {"id": "C15-6", "kind": "gap",
     "summary": "service.yaml 的 depends_on 只有 [S28,S01]，但 check-eligibility "
                "会被 S02/S26 高频调用（反向依赖），catalog 未表达",
     "handling": "按运行协作关系处理，不新增启动依赖；容量与 SLO 未实测"
                 "（service.yaml 的 slo.verified 仍为 false）。"},
    {"id": "C15-7", "kind": "needs_owner",
     "summary": "docs/10 的声线回归未说明样本与阈值",
     "handling": "只存调用方给的 regression{similarity_score,evaluated_against,"
                 "passed}；passed=false 的映射不得路由（§2.5）。阈值不在本服务定。"},
    {"id": "C15-8", "kind": "needs_owner",
     "summary": "克隆音色的授权时长 / 声纹样本保留期只有原则，没有数值",
     "handling": "只建模字段（expires_at、voice_sample_refs），不设默认值、"
                 "不做保留期清理。"},

    # --- 事件契约里已登记的（VAC-1…VAC-8） ---
    {"id": "VAC-1", "kind": "conflict",
     "summary": "aggregate_sequence 有两个定义：§5.3 说 = state_seq，§2.8 说"
                "= revision。按后者建音色事件是 seq=0（违约），第二次授权变更"
                "会撞 outbox 的 UNIQUE(aggregate_id,aggregate_sequence,event_type)",
     "handling": "取 §5.3 的 state_seq。理由：revision 定义让创建事件序列号为 0"
                 "（契约没定义 0），且撤销会因唯一约束回滚——那是合规事故。"
                 "event_sdk 的 OUTBOX_DDL 实际没有这条唯一约束，见 S15-4。"},
    {"id": "VAC-2", "kind": "needs_owner",
     "summary": "建音色事件必须写 reason_code（authorization_event 里 NOT NULL），"
                "而 §4.1 的 POST /voices 命令体没有这个参数",
     "handling": "服务端写 `INITIAL_GRANT`（不取调用方输入）。全仓词表只有"
                 "RIGHTS_WITHDRAWN，其余码待 owner 定；本服务用的码集中列在"
                 "service.py 的 REASON_CODES。"},
    {"id": "VAC-3", "kind": "conflict",
     "summary": "to_state 的枚举出处是 voice_authorization.state 的 CHECK，而"
                "authorization_event.to_state 是裸 text；从事件回放状态会绕过约束",
     "handling": "两张表都由本服务同一事务写，事件表的 to_state 也加 CHECK；"
                 "撤销的终态性由触发器保证（§2.3），消费方须把 revoked 当终态。"},
    {"id": "VAC-4", "kind": "conflict",
     "summary": "effective_at 在方案 DDL 里没有列，语义是'审计用且可能是未来'；"
                "payload 里又没有 occurred_at 可替代",
     "handling": "给 authorization_event 补 effective_at 列（方案要求保留它）；"
                 "无论 effective_at 是未来还是过去，**立刻**拒绝新调用（§4.4）。"},
    {"id": "VAC-5", "kind": "conflict",
     "summary": "authorized_uses_before 与 expires_at 都不在 authorization_event "
                "里，只能从当前行取快照；而状态机没有任何转移会改 authorized_uses",
     "handling": "补 authorized_uses_before / expires_at_before 两列，把事件时刻的"
                 "值冻住（否则回放事件会读到一个更晚的状态）。"},
    {"id": "VAC-6", "kind": "conflict",
     "summary": "evidence_ref 是 3 键 {service_id,id,revision}，同表 rights_ref 是"
                "4 键 {rights_owner,rights_id,rights_revision,rights_hash}；同一概念"
                "两套形状，且 service_id:S02 与 C15-5 的 S10 说法冲突",
     "handling": "事件里按契约写 3 键（rights_ref 转成 id/revision，service_id 取"
                 "rights_ref.service_id 或默认 S02）；rights_hash 留在库里不进事件。"
                 "事件契约不改。"},
    {"id": "VAC-7", "kind": "conflict",
     "summary": "事件 payload 没有 actor，而 authorization_event.actor 是 NOT NULL、"
                "E30-C6 要求撤销可追溯'谁做的'",
     "handling": "actor 只落 authorization_event 列（审计靠库不靠事件）；payload "
                 "是 additionalProperties:false，不能塞。"},
    {"id": "VAC-8", "kind": "gap",
     "summary": "六处漂移：partition key 写成列名、aggregate_id 与 DDL 的 vox_01J "
                "形态不符、envelope 的 tenant_id/subject 形态、无 revision accepted "
                "事件（C15-2）、映射变更仅本地事件（C15-4）、reason_text/scope_notes "
                "不进事件",
     "handling": "partition_key = tenant_id + ':' + voice_id；subject = "
                 "'voice/' + voice_id；tenant_id 取平台签名上下文；其余三条按"
                 "C15-2 / C15-4 / 本文件处理。"},

    # --- 实现时新发现的 ---
    {"id": "S15-1", "kind": "conflict",
     "summary": "§4.1 的注册命令体没有 project_id，而 voice.project_id 是 NOT NULL",
     "handling": "把 project_id 作为必填读（缺了 400 MALFORMED_REQUEST），不猜、"
                 "不用 series_scope 顶替；字段清单待契约确认。"},
    {"id": "S15-2", "kind": "conflict",
     "summary": "E29 要求样本'来自隔离导入器 + 过恶意媒体检查'，但 S28 的 "
                "GET /artifacts/{id}/revisions/{rev} 不返回 probe_ok / source_kind，"
                "按现有读面无法证明",
     "handling": "scanner 闭包读取返回体里若有的 probe_ok/scanned/source_kind；"
                 "字段缺失 = 无法核实 → 503（fail closed，不默默放行）。"
                 "这意味着接真实 S28 时**没有**可接受的路径，直到 S28 补字段。"},
    {"id": "S15-3", "kind": "needs_owner",
     "summary": "authorized_uses 是 ['series:x','modality:speech'] 这种字面串，而"
                "requested_use 是 6 字段对象；成员判定规则未定义",
     "handling": "按前缀判定：uses 里出现的每个前缀（series/modality/provider/"
                 "channel/model_snapshot/region），请求必须**给出对应维度且命中**"
                 "（缺维度视为不命中，不 fail-open）；modality 恒需命中；"
                 "uses 里没出现的前缀不约束。等 owner 定稿。"},
    {"id": "S15-4", "kind": "gap",
     "summary": "§2.8 的 idempotency_record / outbox DDL 与平台 SDK 不同"
                "（SDK 的 idempotency 只有 result 列、outbox 无 aggregate 唯一约束）",
     "handling": "用 SDK 的 DDL（跨服务一致优先）；§2.8 的 response_code / "
                 "response_body 不建列，响应体整体存在 result 里。"},
    {"id": "S15-5", "kind": "gap",
     "summary": "§5.4 的码表不含非法状态转移、词典内容重复、授权行缺失、"
                "映射 region 歧义、tombstoned 音色这几种情况",
     "handling": "补 STATE_TRANSITION_INVALID / DUPLICATE_LEXICON_CONTENT / "
                 "AUTHORIZATION_MISSING / MAPPING_REGION_AMBIGUOUS / "
                 "VOICE_TOMBSTONED，全列在 service.py 的 ERROR_CODES；契约待补。"},
    {"id": "S15-6", "kind": "gap",
     "summary": "§5.1 的映射子资源路径写 `{pk}`，没有说明复合主键怎么进路径",
     "handling": "用 '~' 连接 voice_revision/provider_id/channel_id/"
                 "model_snapshot/region，路径段做 URL 编码；解析失败 404。"},
    {"id": "S15-7", "kind": "gap",
     "summary": "§3.1 的合法转移表没有 expired → revoked，但到期后依据法务函件"
                "撤回是现实路径",
     "handling": "允许 expired → revoked（撤销是安全方向），仍要求 evidence_ref；"
                 "revoked 依旧是终态。"},
    {"id": "S15-8", "kind": "needs_owner",
     "summary": "permission registry（40 键）在音色域只有一个键 "
                "`asset.voice.authorize`（risk=high，S01 plan:46 指派给 S11–S15），"
                "没有独立的'写音色内容'键。本服务全部写命令都用它，于是同一个"
                "high-risk 键同时授权'处置授权'和'写入声纹内容'——有撤授权权限的"
                "主体因此也能替换音色内容",
     "handling": "按注册表实现：所有写命令问 `asset.voice.authorize`，S01 侧"
                 "可真正生效（不是 503）。**没有**自造 `voice.asset.write`——"
                 "那会让每条写命令在生产里不可用，是 S13 的取舍不是这里的。"
                 "粒度要不要拆由 owner 定；拆的时候只改 deps 里两个常量。"
                 "注意这不是'谁都能写'：仍然要求一条 high-risk 授权。"},
    {"id": "S15-9", "kind": "gap",
     "summary": "§4.1 的注册事务只列了 voice/authorization/event/outbox，"
                "但 language/emotion/rate/样本/权利这些字段只属于 voice_revision",
     "handling": "注册同时写 revision 1（source_kind 由 voice_kind 推出）与 "
                 "canonical revision 1（decision_kind='initial_import'），"
                 "否则注册出来的音色没有内容可被引用。"},
    {"id": "S15-10", "kind": "needs_owner",
     "summary": "§2.2 的 vr_hash_uniq 是 (tenant_id, content_hash)——租户级，"
                "不含 voice_id；F15 的措辞却是'同内容重复接受 revision'，读起来"
                "像单条音色内。按 DDL 字面实现，同租户下两个音色只要内容相同"
                "（两个 preset 共用一套 language/emotion/rate 就不罕见）就会被"
                "409 DUPLICATE_VOICE_CONTENT 挡在注册上，而 F15 的码表里没有"
                "注册场景",
     "handling": "按 DDL 字面实现（索引是写死的，不自行收窄成 (tenant_id, "
                 "voice_id, content_hash)），content_hash 也不掺 voice_id——"
                 "掺了就等于偷偷改了索引语义。行为在测试里显式钉住；要不要"
                 "收窄由 owner 定。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 音色身份（§2.1） ----------
-- voice_id 是平台 ID，不做全局唯一：E28-F3 要求同一 voice_id 在不同租户互相独立，
-- 所以唯一约束前缀一律是 tenant_id。
CREATE TABLE IF NOT EXISTS voice (
  tenant_id         TEXT NOT NULL,
  voice_id          TEXT NOT NULL,
  project_id        TEXT NOT NULL,
  series_scope      TEXT NOT NULL,
  display_name      TEXT NOT NULL,
  voice_kind        TEXT NOT NULL CHECK (voice_kind IN (
                      'platform_preset','cloned','licensed_actor','synthetic',
                      'narrator')),
  character_ref     TEXT,
  status            TEXT NOT NULL
                      CHECK (status IN ('active','deprecated','tombstoned')),
  canonical_revision INTEGER,
  current_revision  INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
  created_by        TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, voice_id),
  -- §2.1。同名两个音色在同一个 series_scope 里无法区分审计，先按硬冲突处理；
  -- §6.1 说的"软性约束 + 可 override"是产品语义，本轮不做 override。
  UNIQUE (tenant_id, series_scope, display_name),
  CONSTRAINT voice_canonical_within_head
    CHECK (canonical_revision IS NULL OR canonical_revision <= current_revision)
);
CREATE INDEX IF NOT EXISTS voice_by_series
  ON voice (tenant_id, series_scope, status, voice_kind);

-- ---------- 不可变 revision（§2.2） ----------
CREATE TABLE IF NOT EXISTS voice_revision (
  tenant_id          TEXT NOT NULL,
  voice_id           TEXT NOT NULL,
  revision           INTEGER NOT NULL CHECK (revision >= 1),
  content_hash       TEXT NOT NULL CHECK (length(content_hash) = 64),
  payload            TEXT NOT NULL,
  language_profile   TEXT NOT NULL,
  emotion_profile    TEXT NOT NULL,
  rate_profile       TEXT NOT NULL,
  voice_sample_refs  TEXT NOT NULL,
  clone_artifact_ref TEXT,
  rights_ref         TEXT NOT NULL,
  source_kind        TEXT NOT NULL CHECK (source_kind IN (
                       'preset_registered','cloned','imported','derived')),
  parent_revision    INTEGER,
  quality_gate_ref   TEXT NOT NULL,
  accepted_at        TEXT NOT NULL,
  accepted_by        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, voice_id, revision),
  CONSTRAINT vr_parent_lt
    CHECK (parent_revision IS NULL OR parent_revision < revision)
);
-- §2.2 的 vr_hash_uniq：同租户内同内容只接受一次（F15）。
CREATE UNIQUE INDEX IF NOT EXISTS vr_hash_uniq
  ON voice_revision (tenant_id, content_hash);

-- ---------- 授权状态（§2.3，唯一可变、强一致） ----------
CREATE TABLE IF NOT EXISTS voice_authorization (
  tenant_id        TEXT NOT NULL,
  voice_id         TEXT NOT NULL,
  state            TEXT NOT NULL
                     CHECK (state IN ('granted','suspended','revoked','expired')),
  rights_ref       TEXT NOT NULL,
  authorized_uses  TEXT NOT NULL,
  scope_notes      TEXT,
  granted_at       TEXT,
  expires_at       TEXT,
  state_changed_at TEXT NOT NULL,
  state_seq        INTEGER NOT NULL CHECK (state_seq >= 1),
  changed_by       TEXT NOT NULL,
  reason_code      TEXT,
  reason_text      TEXT,
  PRIMARY KEY (tenant_id, voice_id)
);
CREATE INDEX IF NOT EXISTS voice_auth_state
  ON voice_authorization (tenant_id, state);
CREATE INDEX IF NOT EXISTS voice_auth_expiry
  ON voice_authorization (tenant_id, expires_at)
  WHERE state = 'granted' AND expires_at IS NOT NULL;

-- §2.3 第 4 条：revoked 是终态。这是最后一道防线——应用层 bug 也不能让它复活。
CREATE TRIGGER IF NOT EXISTS deny_revoked_transition
BEFORE UPDATE ON voice_authorization
WHEN OLD.state = 'revoked' AND NEW.state <> 'revoked'
BEGIN
  SELECT RAISE(ABORT, 'revoked is terminal for voice_id');
END;

-- ---------- 授权变更历史（§2.4，审计必须保留） ----------
-- 比方案多三列：effective_at（VAC-4）与两个 before 快照（VAC-5）。没有它们，
-- 事件重放时只能读到"现在"的授权用途与到期时间，那不是事件发生时的值。
CREATE TABLE IF NOT EXISTS authorization_event (
  tenant_id             TEXT NOT NULL,
  voice_id              TEXT NOT NULL,
  seq                   INTEGER NOT NULL CHECK (seq >= 1),
  from_state            TEXT
                          CHECK (from_state IS NULL OR from_state IN (
                            'granted','suspended','revoked','expired')),
  to_state              TEXT NOT NULL
                          CHECK (to_state IN (
                            'granted','suspended','revoked','expired')),
  reason_code           TEXT NOT NULL,
  reason_text           TEXT,
  evidence_ref          TEXT,
  effective_at          TEXT,
  authorized_uses_before TEXT NOT NULL,
  expires_at_before     TEXT,
  actor                 TEXT NOT NULL,
  occurred_at           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, voice_id, seq)
);
CREATE INDEX IF NOT EXISTS auth_event_occurred
  ON authorization_event (tenant_id, occurred_at DESC);

-- ---------- 供应商映射（§2.5，只追加） ----------
CREATE TABLE IF NOT EXISTS provider_voice_mapping (
  tenant_id         TEXT NOT NULL,
  voice_id          TEXT NOT NULL,
  voice_revision    INTEGER NOT NULL,
  provider_id       TEXT NOT NULL,
  channel_id        TEXT NOT NULL,
  model_snapshot    TEXT NOT NULL,
  region            TEXT NOT NULL,
  provider_voice_id TEXT NOT NULL,
  mapping_status    TEXT NOT NULL
                      CHECK (mapping_status IN ('active','deprecated','unavailable')),
  regression        TEXT NOT NULL,
  added_at          TEXT NOT NULL,
  unavailable_reason TEXT,
  PRIMARY KEY (tenant_id, voice_id, voice_revision, provider_id, channel_id,
               model_snapshot, region)
);
CREATE INDEX IF NOT EXISTS pvm_lookup
  ON provider_voice_mapping (tenant_id, voice_id, mapping_status);
CREATE INDEX IF NOT EXISTS pvm_by_snapshot
  ON provider_voice_mapping (tenant_id, provider_id, model_snapshot, mapping_status);

-- ---------- 发音词典（§2.6） ----------
CREATE TABLE IF NOT EXISTS lexicon (
  tenant_id     TEXT NOT NULL,
  lexicon_id    TEXT NOT NULL,
  scope_kind    TEXT NOT NULL CHECK (scope_kind IN ('project','series','voice')),
  scope_id      TEXT NOT NULL,
  language      TEXT NOT NULL DEFAULT 'zh-CN',
  head_revision INTEGER NOT NULL DEFAULT 0 CHECK (head_revision >= 0),
  PRIMARY KEY (tenant_id, lexicon_id)
);
CREATE INDEX IF NOT EXISTS lexicon_by_scope
  ON lexicon (tenant_id, scope_kind, scope_id, language);

CREATE TABLE IF NOT EXISTS lexicon_revision (
  tenant_id    TEXT NOT NULL,
  lexicon_id   TEXT NOT NULL,
  revision     INTEGER NOT NULL CHECK (revision >= 1),
  content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
  entries      TEXT NOT NULL,
  entry_count  INTEGER NOT NULL CHECK (entry_count >= 0),
  created_by   TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, lexicon_id, revision)
);
-- 词典内容重复同样拒（S15-5 补的码：DUPLICATE_LEXICON_CONTENT）。
CREATE UNIQUE INDEX IF NOT EXISTS lexr_hash_uniq
  ON lexicon_revision (tenant_id, content_hash);

CREATE TABLE IF NOT EXISTS voice_lexicon_binding (
  tenant_id        TEXT NOT NULL,
  voice_id         TEXT NOT NULL,
  lexicon_id       TEXT NOT NULL,
  lexicon_revision INTEGER NOT NULL,
  is_default       INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
  PRIMARY KEY (tenant_id, voice_id, lexicon_id)
);
-- §2.6：一个音色最多一个默认词典。
CREATE UNIQUE INDEX IF NOT EXISTS voice_default_lexicon
  ON voice_lexicon_binding (tenant_id, voice_id)
  WHERE is_default = 1;

-- ---------- 候选与 canonical（§2.7） ----------
CREATE TABLE IF NOT EXISTS voice_candidate (
  tenant_id     TEXT NOT NULL,
  candidate_id  TEXT NOT NULL,
  voice_id      TEXT NOT NULL,
  content_hash  TEXT NOT NULL CHECK (length(content_hash) = 64),
  payload       TEXT NOT NULL,
  voice_check   TEXT NOT NULL,
  state         TEXT NOT NULL
                  CHECK (state IN ('PENDING','ACCEPTED','REJECTED','EXPIRED')),
  created_by    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS vc_by_voice
  ON voice_candidate (tenant_id, voice_id, state);

CREATE TABLE IF NOT EXISTS canonical_pointer (
  tenant_id       TEXT NOT NULL,
  voice_id        TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  decided_at      TEXT NOT NULL,
  decided_by      TEXT NOT NULL,
  decision_kind   TEXT NOT NULL CHECK (decision_kind IN (
                    'human_approved','policy_auto','initial_import')),
  reason_text     TEXT,
  prior_canonical INTEGER,
  superseded_at   TEXT,
  PRIMARY KEY (tenant_id, voice_id, revision)
);
-- §2.7：一个音色同时只能有一个未失效的 canonical。
CREATE UNIQUE INDEX IF NOT EXISTS voice_canonical_one
  ON canonical_pointer (tenant_id, voice_id)
  WHERE superseded_at IS NULL;
"""
