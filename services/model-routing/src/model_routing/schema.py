"""S25 自己的 schema。表来源是 `plans/S25-model-routing.md` §7。

四条结构性事实，它们决定这张库能证明什么、不能证明什么：

1. **`route_decision` 是决策的权威，不是资金或配额的承诺。** 它没有余额列，
   也没有 permit 列（§1.2）。S26 拿它派单时必须**实时**回查 S24 与 S23
   （§12 第 1、2 条），所以这张表回答的是"当时算出来是这样"，
   不是"现在还能这么派"。
2. **硬过滤与评分在数据层就是两段。** `route_candidate.decision` 只有
   `eligible` / `rejected` 两个值，被硬过滤的候选**根本不进评分**。
   方案 §1.3 要求下游"无法用取排名第一的方式意外选中一个被硬过滤的候选"——
   这靠的是 `eligible[]` 与 `rejected[]` 是两个字段，不是两个分数。
3. **`silent_degradation` 与 `auto_executable` 不能同时为真。** 方案 §7 行 467
   的 `CHECK (auto_executable = false OR silent_degradation = false)`：
   "既要求人工确认又能自动执行"的选项在数据库层面不该存在。
4. **事件与决策同事务。** `outbox` 由平台 SDK 建表（不在本文件），
   但 `append` 必须发生在写 `route_decision` 的同一个 `COMMIT` 里（§7.1）。
   分开写就有一致性空洞：决策提交了事件丢了、或事件发了决策回滚了。

方案里的 `jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`uuid` 存 TEXT。
`boolean` 存 INTEGER 0/1（SQLite 无原生布尔）。
"""

ID_PREFIX = {"route": "rt_", "policy_snapshot": "rps_", "option": "opt_"}

# 本轮**不解决**的缺口。`contracts/model.route-selected.v2.schema.json` 里已有
# 4 条 `x-needs-owner` 与 MRT-1…MRT-10 的 `x-conflicts`，那些是待 owner 决定
# 的事项，按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要自行选边」处理：
# 本服务按方案散文实现，把偏差记在这里，不修改契约、不代填决定。
CONTRACT_GAPS = [
    # --- 已在 contracts/model.route-selected.v2.schema.json 里登记的 ---
    {"id": "MRT-1", "kind": "conflict",
     "summary": "§7.1 的 data_ref 只给 3 个键，本仓 event validator 要求 4 个"
                "（缺 content_hash）",
     "handling": "按 4 个键写，与 project-catalog 的 data_ref 一致；契约待改。"},
    {"id": "MRT-2", "kind": "conflict",
     "summary": "§7.1 示例 expected_cost 1180000 > quote_upper_bound 1080000，"
                "而上界按定义是上限",
     "handling": "两个值分开存、分开算；示例本身不采信，等 owner 修正示例。"},
    {"id": "MRT-3", "kind": "conflict",
     "summary": "revision 同时被 payload / aggregate_seq / outbox 唯一约束使用，"
                "而一条决策只有 revision=1；报价过期后重新路由需要新 route_id",
     "handling": "revision 固定为 1，重新路由产出新 route_id；不原地改 revision。"},
    {"id": "MRT-4", "kind": "conflict",
     "summary": "§9 要求响应带 projection_lag，§7 的 22 列 DDL 没有这一列",
     "handling": "补 route_decision.projection_lag 列。"},
    {"id": "MRT-5", "kind": "conflict",
     "summary": "T25-21 声称决策可复算，但输入含三个可变外部量"
                "（S24 能力、S02 策略、S23 余额）",
     "handling": "scoring 记录权重/区间/水位；外部输入的水位一起存，"
                 "复算时能看出差异来自哪一侧。不承诺跨时间复算一致。"},
    {"id": "MRT-6", "kind": "conflict",
     "summary": "四个业务坐标词典（modality / mode / role / audio_strategy）"
                "全仓未定义",
     "handling": "本服务不校验取值，按上游传入原样使用；不自行发明枚举。"},
    {"id": "MRT-7", "kind": "conflict",
     "summary": "feasible / selected / infeasible_reason 的配对只在散文里，"
                "schema 没用 if/then",
     "handling": "用 route_decision 的 rd_reason_paired CHECK 兜住；契约待补。"},
    {"id": "MRT-8", "kind": "conflict",
     "summary": "§7.1 的 selected 是必填对象，但 feasible=false 时为 null；"
                "且 selected.combination_key 必填而 DDL 可空",
     "handling": "feasible=false 时 selected=null；非视频模态 combination_key"
                 "记 null 并在事件体里如实带出。契约待改。"},
    {"id": "MRT-9", "kind": "needs_owner",
     "summary": "normalized_input 的完整字段集与规范化规则未定义"
                "（generation-command.v2 的 input 只是 {\"type\":\"object\"}）",
     "handling": "按 §12 第 4 条给出最小字段集（时长/画幅/分辨率/参考/模式/"
                 "音频策略），吸附后的时长必填。等 owner 定稿。"},
    {"id": "MRT-10", "kind": "needs_owner",
     "summary": "currency 词表只有 char(3)，无 ISO-4217、无大小写约束",
     "handling": "本服务不改写大小写，原样透传；不改契约。"},
    {"id": "MRT-11", "kind": "needs_owner",
     "summary": "rejection_code 码表散在三张表（§3 的 19 行 + §9 的三个 + "
                "§2.2 的一个），没有汇总、没有 enum",
     "handling": "本服务把实际用到的码集中定义在 filters.FILTER_ORDER，"
                 "并在事件里透传主导原因；契约的 enum 待 owner 定稿。"},

    # --- 实现时新发现的 ---
    {"id": "MRT-12", "kind": "conflict",
     "summary": "§2.2 要求从 template_ref / 上游读 dialogue_duration_ms，"
                "但 generation-command.v2 的 input 与 template_ref 都没声明"
                "这个字段",
     "handling": "先读 input.dialogue_duration_ms，再读 "
                 "template_ref.dialogue_duration_ms，两处都没有即 "
                 "DIALOGUE_DURATION_UNKNOWN。字段名待契约确认。"},
    {"id": "MRT-13", "kind": "conflict",
     "summary": "S24 的 /model-candidates 已经把 §3 第 1–8 条（及价格过期）"
                "过滤掉，且与 S25 的码字面相同；S25 若再判一次，"
                "同一候选会被两处拒绝，但不是同一个权威",
     "handling": "S25 仍按 §3 全表复核——它是准入的权威方，S24 的排除"
                 "只是输入。S24 不返回的候选，S25 无从复核，也不补造。"},
    {"id": "MRT-14", "kind": "conflict",
     "summary": "S24 的 excluded[] 只有 candidate_key + code + detail，"
                "没有 snapshot_id；§3.1 的 degraded 放行需要规格才能打分",
     "handling": "S25 能把自己的 degraded 例外逻辑写在 filters 里，但真实"
                 "上游拿不到载荷，走不到这条分支——需要 S24 在 excluded[] "
                 "里补 snapshot_id，或另开接口。"},
    {"id": "MRT-15", "kind": "conflict",
     "summary": "§3 第 14/17/18 条需要 p90 排队/生成延迟与 "
                "provider_idempotency，而 S24 的候选体明确不发布这些"
                "（C24-5，填错就是重复扣费）",
     "handling": "数据缺失时跳过该条过滤并在评分里记 *_known=false，"
                 "不拿 0 或默认值替代；由 S26 在签发 grant 前实时复查。"},
    {"id": "MRT-16", "kind": "needs_owner",
     "summary": "§2.3 的 required_ref_ttl 需要 p90_queue_wait / p90_generation，"
                "全仓没有提供方；安全余量也没有数值",
     "handling": "接口留 telemetry 注入；余量取 60s 并写在这里，等 owner 定。"},
    {"id": "MRT-17", "kind": "conflict",
     "summary": "contracts/openapi.v2.yaml 的 /route-decisions 响应是 "
                "additionalProperties:false 且只列 route_id / feasible / "
                "rejection_reasons，与方案 §5.1 的响应体（policy / eligible / "
                "rejected / degradation_offer / quote_valid_to …）不相容；"
                "请求体同样 additionalProperties:false，只允许 "
                "generation_command + command_id，方案 §5.1 与 §6.3 却要读 "
                "degradation_level",
     "handling": "本服务按方案 §5.1 的响应体返回，并按方案读 "
                 "degradation_level；不修改契约、不自行选边。等 owner 决定是"
                 "扩契约还是收方案（AGENTS.md：契约与文档冲突 → 停下报告）。"},
    {"id": "MRT-18", "kind": "conflict",
     "summary": "§9 / T25-20 要读 `projection_lag`，但 S24 的 "
                "`GET /model-candidates` 只返回 capability_bundle_revision 与 "
                "projection_watermark，没有任何滞后信号",
     "handling": "`bool(bundle.get('projection_lag'))` 对真实上游恒为 False，"
                 "列照样落库（方案要求可追溯），但不得据此声称已实现 T25-20。"
                 "需要 owner 定：S24 补信号、由调用方传标记，还是删掉这条。"},
    {"id": "MRT-19", "kind": "conflict",
     "summary": "§3 第 19 条的 `lora_compatible` 与 T25-13 的 `attempt_no` 都"
                "不在上游/契约里：S24 的 capability_mode 没有 lora 列，"
                "`generation-command.v2` 是 additionalProperties:false 也没有 "
                "attempt_no",
     "handling": "`lora_compatible` 缺失时按拒绝处理（安全但恒拒）；"
                 "`attempt_no` 从 `input`（契约里唯一的开放对象）读，缺省 1。"},
    {"id": "MRT-20", "kind": "conflict",
     "summary": "方案 §7 行 482 / §8 行 667 声称 outbox 有 "
                "`UNIQUE (aggregate_id, sequence)`，但 event_sdk 的 OUTBOX_DDL "
                "只有 `event_id` 主键，没有这条唯一约束",
     "handling": "可观测事实仍然成立：回放路径不重新路由、不写第二条事件"
                 "（T25-27 已测）。但「同聚合不重号」由 SDK 保证不了，"
                 "不能把 T25-25 说成已被约束证明。"},
    {"id": "MRT-21", "kind": "gap",
     "summary": "§9 行 611 要求 S20 模板缺失/不兼容时拒该候选"
                "（`COMPILED_PROMPT_INCOMPATIBLE`），但 S25 从不读 S20："
                "`template_ref` 只用来取 `dialogue_duration_ms`，"
                "deps 里没有 S20 源；catalog 却把 S20 列为 depends_on",
     "handling": "没有实现这条拒绝，也没有 T25-0x 覆盖它（T25 表里没有一项）。"
                 "要么补一个 S20 编译检查源并加用例，要么把 S20 从依赖里去掉——"
                 "现在是两个都没做，属于已知未实现，不是已完成。"},
]


SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------- 策略快照（S02 授权，S25 只存生效快照） ----------
-- §1.1：权重与策略版本由 S02 授权。S25 不自己决定权重，只记录"这一次用的是哪一版"。
CREATE TABLE IF NOT EXISTS routing_policy_snapshot (
  policy_snapshot_id TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  policy_ref_id      TEXT NOT NULL,
  policy_ref_rev     INTEGER NOT NULL,
  policy_ref_hash    TEXT NOT NULL CHECK (length(policy_ref_hash) = 64),
  weights            TEXT NOT NULL,
  thresholds         TEXT NOT NULL,
  flags              TEXT NOT NULL,
  effective_from     TEXT NOT NULL,
  UNIQUE (tenant_id, policy_ref_id, policy_ref_rev, policy_ref_hash)
);

-- ---------- 路由决策头 ----------
CREATE TABLE IF NOT EXISTS route_decision (
  route_id              TEXT PRIMARY KEY,
  revision              INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  tenant_id             TEXT NOT NULL,
  project_id            TEXT NOT NULL,
  series_id             TEXT NOT NULL,
  episode_id            TEXT NOT NULL,
  branch_id             TEXT NOT NULL,
  node_id               TEXT NOT NULL,
  -- AGENTS.md：`execution_fence` 管 run/branch，`node_epoch` 管单个
  -- `logical_node`。两列分开存，绝不合并；本服务只把它们的值透传出去。
  node_epoch            INTEGER NOT NULL CHECK (node_epoch >= 0),
  execution_fence       INTEGER NOT NULL CHECK (execution_fence >= 0),
  attempt_id            TEXT NOT NULL,
  request_hash          TEXT NOT NULL CHECK (length(request_hash) = 64),
  command_id            TEXT NOT NULL,
  policy_snapshot_id    TEXT NOT NULL
                          REFERENCES routing_policy_snapshot (policy_snapshot_id),
  capability_bundle_revision INTEGER NOT NULL,
  feasible              INTEGER NOT NULL CHECK (feasible IN (0, 1)),
  requirement_set       TEXT NOT NULL,
  scoring               TEXT NOT NULL,
  quote_valid_to        TEXT NOT NULL,
  content_hash          TEXT NOT NULL CHECK (length(content_hash) = 64),
  -- §9 行 610 要求响应带 `projection_lag`，而 §7 DDL 的 22 列里没有它。
  -- 补这一列：没有它，"当时 S24 投影是落后的"这个事实在决策之后无处可查，
  -- 而它决定了这条决策有多可信（下游据此决定要不要更保守地复查）。
  projection_lag        INTEGER NOT NULL DEFAULT 0
                          CHECK (projection_lag IN (0, 1)),
  -- §5.2 / MRT-7：`feasible=false` 时要给原因码。方案 §7 的 DDL 没有这一列，
  -- 于是这个值只存在于事件里、不可回放（contracts 的 MRT 冲突已指出）。
  -- 补上：否则 T25-26 的"infeasible_reason 有值"在库里查不到。
  infeasible_reason     TEXT,
  created_at            TEXT NOT NULL,
  -- §7 行 425：一条命令最多一条决策。
  UNIQUE (tenant_id, command_id),
  -- MRT-7 的另一半：配对关系在这里绑住。方案散文写了"feasible=false ⇒
  -- selected=null 且 infeasible_reason 非空"，schema 没绑。这条把它变成
  -- 数据库事实，同时补上散文没说的反方向。
  CONSTRAINT rd_reason_paired CHECK (
    (feasible = 1 AND infeasible_reason IS NULL)
    OR (feasible = 0 AND infeasible_reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_route_lookup
  ON route_decision (tenant_id, attempt_id, created_at DESC);

-- ---------- 候选（含被拒的） ----------
CREATE TABLE IF NOT EXISTS route_candidate (
  route_id             TEXT NOT NULL
                         REFERENCES route_decision (route_id),
  -- `rank` 为 NULL 表示 rejected。§7 DDL 行 430。
  rank                 INTEGER CHECK (rank IS NULL OR rank >= 1),
  candidate_key        TEXT NOT NULL,
  snapshot_id          TEXT NOT NULL,
  price_snapshot_id    TEXT,
  combination_key      TEXT,
  combination_verdict  TEXT,
  decision             TEXT NOT NULL CHECK (decision IN ('eligible', 'rejected')),
  rejection_code       TEXT,
  rejection_detail     TEXT,
  score                REAL,
  score_parts          TEXT,
  sample_size          INTEGER,
  watermark            TEXT,
  expected_cost_micro  INTEGER,
  expected_cost_breakdown TEXT,
  quote_upper_bound_micro INTEGER,
  currency             TEXT CHECK (currency IS NULL OR length(currency) = 3),
  normalized_input     TEXT,
  execution_profile    TEXT,
  PRIMARY KEY (route_id, candidate_key),
  -- §7 行 452：拒绝必须带原因码。
  CHECK (decision <> 'rejected' OR rejection_code IS NOT NULL),
  -- 硬过滤与评分的分界（§1.3）：rejected 不参与评分，eligible 必须有分数与排名。
  -- 只写方案那半句会让"eligible 但没分数"通过，那种行下游没法取 rank=1。
  CHECK (
    (decision = 'rejected' AND rank IS NULL AND score IS NULL)
    OR (decision = 'eligible' AND rank IS NOT NULL AND score IS NOT NULL
        AND rejection_code IS NULL))
);
-- 一条决策内 rank 唯一（否则"取 rank=1"有两个答案）。
CREATE UNIQUE INDEX IF NOT EXISTS rc_rank_uniq
  ON route_candidate (route_id, rank)
  WHERE decision = 'eligible';

-- ---------- 降级选项 ----------
CREATE TABLE IF NOT EXISTS degradation_option (
  route_id           TEXT NOT NULL REFERENCES route_decision (route_id),
  option_id          TEXT NOT NULL,
  kind               TEXT NOT NULL CHECK (kind IN (
    'pool', 'model_swap', 'spec_adjust', 'split_shot', 'layered', 'human_review')),
  auto_executable    INTEGER NOT NULL CHECK (auto_executable IN (0, 1)),
  silent_degradation INTEGER NOT NULL CHECK (silent_degradation IN (0, 1)),
  blocked_reason     TEXT,
  cost_delta_micro   INTEGER,
  quality_risk       TEXT,
  payload            TEXT NOT NULL,
  PRIMARY KEY (route_id, option_id),
  -- §7 行 467 的硬约束：既要求人工确认又能自动执行的选项不该存在。
  CONSTRAINT do_no_silent_auto CHECK (
    auto_executable = 0 OR silent_degradation = 0)
);
"""
