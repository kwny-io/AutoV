"""S40 自己的 schema。表来源是 `plans/S40-human-review.md` §10.1。

四条结构性事实，它们决定了这张库能证明什么、不能证明什么：

1. **这里没有 accepted pointer。** S40 记录的是「人说了什么」，不是「它生效了」。
   `review_decision.status` 的 `APPLIED` 只能由 S08 CAS 的成功结果赋；本库自己的
   写入路径永远不会把它写成 `APPLIED`（§5、§6.5）。
2. **`binding` 建后不可变。** 任务是冻结的快照，改 `binding` 等于换了一道题；
   触发器直接拒绝（§2.1）。
3. **一人一票是结构性的，不是应用层自觉。** 部分唯一索引
   `(tenant, task, actor) WHERE status IN ('RECORDED','APPLIED')` 让「同一人两张
   有效票」在 SQLite 层就写不进去（§10.1）。
4. **stale 与 invalid 是两件事。** `STALE` 是「你批的版本过去了」，`REJECTED_INVALID`
   是「你本来就不该批」。两者都不进 quorum，但审计含义相反（§3.3、§4）。

本仓服务层一律 SQLite（`isolation_level=None` + 显式 `Transaction`），所以方案里的
`jsonb` 存 TEXT、`timestamptz` 存定宽 UTC 字符串、`text[]` 存 JSON 数组。
`outbox` / `inbox` 由 `event_sdk` 建表，这里不重复 DDL。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

-- §10.1 任务。binding / pack_hash / deadline 建后不可变（触发器守）。
CREATE TABLE IF NOT EXISTS review_task (
  tenant_id        TEXT NOT NULL,
  review_task_id   TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN
                     ('gate_acceptance','quality_exception','budget_increase',
                      'external_publish','repair_choice','manual_takeover',
                      'evidence_abstain','conflict_arbitration')),
  state            TEXT NOT NULL CHECK (state IN
                     ('OPEN','IN_REVIEW','PARTIALLY_DECIDED','DECIDED',
                      'EXPIRED','SUPERSEDED','WITHDRAWN')),
  revision         INTEGER NOT NULL CHECK (revision >= 1),
  binding          TEXT NOT NULL,
  subject          TEXT NOT NULL,
  reason           TEXT NOT NULL,
  evidence         TEXT NOT NULL,
  options          TEXT NOT NULL,
  assignment       TEXT NOT NULL,
  pack_hash        TEXT NOT NULL CHECK (length(pack_hash) = 64),
  deadline         TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  created_by       TEXT NOT NULL,
  source_event     TEXT,
  PRIMARY KEY (tenant_id, review_task_id)
);

CREATE INDEX IF NOT EXISTS task_by_queue
  ON review_task (tenant_id, state, deadline);

-- §2.1：任务冻结。只有 state/revision/assignment 可改。
CREATE TRIGGER IF NOT EXISTS task_binding_is_frozen
BEFORE UPDATE OF binding, pack_hash, subject, reason, evidence, options,
                 deadline, kind, created_at
ON review_task
BEGIN
  SELECT RAISE(ABORT, 'review task binding is immutable');
END;

-- §10.1 决定。`decided_against` 是冻结副本，不是指向任务的引用。
CREATE TABLE IF NOT EXISTS review_decision (
  tenant_id        TEXT NOT NULL,
  decision_id      TEXT NOT NULL,
  review_task_id   TEXT NOT NULL,
  command_id       TEXT NOT NULL,
  request_hash     TEXT NOT NULL,
  decided_against  TEXT NOT NULL,
  decision         TEXT NOT NULL,
  option_id        TEXT NOT NULL,
  reason           TEXT NOT NULL,
  payload          TEXT NOT NULL DEFAULT '{}',
  actor            TEXT NOT NULL,
  authz_snapshot   TEXT NOT NULL,
  recorded_at      TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN
                     ('RECORDED','APPLIED','STALE','REJECTED_INVALID',
                      'WITHDRAWN')),
  stale_reason     TEXT,
  validation       TEXT,
  downstream       TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, decision_id),
  UNIQUE (tenant_id, command_id)
);

-- 一人一票。只约束"有效票"：STALE / REJECTED_INVALID 不占位，
-- 否则一个人投过一次废票就再也不能投票了。
CREATE UNIQUE INDEX IF NOT EXISTS one_live_vote_per_actor
  ON review_decision (tenant_id, review_task_id,
                      json_extract(actor, '$.principal_id'))
  WHERE status IN ('RECORDED', 'APPLIED');

CREATE INDEX IF NOT EXISTS decision_by_task
  ON review_decision (tenant_id, review_task_id, status);

-- §10.1 quorum 累计。S40 只存"现在数到几票"，规则本身在 S02。
CREATE TABLE IF NOT EXISTS review_quorum_state (
  tenant_id            TEXT NOT NULL,
  review_task_id       TEXT NOT NULL,
  required             INTEGER NOT NULL CHECK (required >= 1),
  separation_of_duties INTEGER NOT NULL CHECK (separation_of_duties IN (0,1)),
  votes_valid          INTEGER NOT NULL DEFAULT 0 CHECK (votes_valid >= 0),
  votes_invalid        INTEGER NOT NULL DEFAULT 0 CHECK (votes_invalid >= 0),
  must_include_roles   TEXT NOT NULL DEFAULT '[]',
  satisfied_at         TEXT,
  PRIMARY KEY (tenant_id, review_task_id)
);

-- §10.1 分派轨迹。只 INSERT。
CREATE TABLE IF NOT EXISTS review_assignment_event (
  tenant_id        TEXT NOT NULL,
  review_task_id   TEXT NOT NULL,
  seq              INTEGER NOT NULL,
  kind             TEXT NOT NULL,
  from_principal   TEXT,
  to_principal     TEXT,
  reason           TEXT,
  at               TEXT NOT NULL,
  PRIMARY KEY (tenant_id, review_task_id, seq)
);

-- §5.1 对 S08 的 CAS 重试队列。**不是 outbox**：重试的是 CAS 调用，不是重发事件。
-- 共表会让"事件没发出去"和"决定没释放"看起来一样（§10.1 末段）。
CREATE TABLE IF NOT EXISTS acceptance_retry (
  tenant_id        TEXT NOT NULL,
  review_task_id   TEXT NOT NULL,
  command_id       TEXT NOT NULL,
  attempt_count    INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at  TEXT NOT NULL,
  last_error       TEXT,
  PRIMARY KEY (tenant_id, review_task_id)
);
"""

TASK_KINDS = (
    "gate_acceptance", "quality_exception", "budget_increase",
    "external_publish", "repair_choice", "manual_takeover",
    "evidence_abstain", "conflict_arbitration",
)

TASK_STATES = ("OPEN", "IN_REVIEW", "PARTIALLY_DECIDED", "DECIDED",
               "EXPIRED", "SUPERSEDED", "WITHDRAWN")

# 终态。即使 late 票到达也不回退（§3.3「终态不回退」）。
TERMINAL_STATES = ("DECIDED", "EXPIRED", "SUPERSEDED", "WITHDRAWN")

DECISION_STATUSES = ("RECORDED", "APPLIED", "STALE", "REJECTED_INVALID",
                     "WITHDRAWN")

# 契约三值（openapi decideReview 的 enum）。`decision` 只许这三个。
CONTRACT_DECISIONS = ("accept", "reject", "request_repair")

# §3.1 / G1：枚举外的选项走 `option_id`（契约没有这个字段，见 C40-1）。
# `decision` 是契约语义，`option_id` 是 UI 上的那颗按钮 —— 两者可能不同：
# 例如"例外接受"的 decision=accept、option_id=exception_accept。
OPTION_IDS = CONTRACT_DECISIONS + ("replace", "change_spec", "exception_accept")

# §3.4 理由长度。accept/reject ≥ 4 字；exception_accept ≥ 20 字。
MIN_REASON = 4
MIN_EXCEPTION_REASON = 20

# §6.3 硬绑定：变了就 stale，没有例外。
# manifest_ref.id 单列，因为 manifest_ref.revision 需要节点级差异（§6.4）。
HARD_BINDING_FIELDS = (
    ("execution_fence", "EXECUTION_FENCE_ADVANCED"),
    ("node_epoch", "NODE_EPOCH_ADVANCED"),
    ("lock_version", "NODE_LOCK_CHANGED"),
)

STALE_REASONS = (
    "MANIFEST_CHANGED", "MANIFEST_AFFECTS_NODE", "MANIFEST_ADVANCED",
    "EXECUTION_FENCE_ADVANCED", "NODE_EPOCH_ADVANCED", "NODE_LOCK_CHANGED",
    "NODE_LOCKED", "ARTIFACT_REPLACED", "POLICY_SUPERSEDED",
    "EVIDENCE_CHANGED", "BINDING_MISMATCH", "DEADLINE_PASSED",
    "TASK_SUPERSEDED", "TASK_EXPIRED", "TASK_WITHDRAWN", "TASK_DECIDED",
)

# ---------------------------------------------------------------- 缺口登记
# 每一条都是契约/上游能力的缺口，不是实现自由度。每次建任务都随响应带出，
# 免得"能响应"被误读成"契约完整"。
CONTRACT_GAPS = [
    "S40 G1: openapi.v2 decideReview accepts decision in {accept, reject, "
    "request_repair} plus context/reason/command_id, additionalProperties: "
    "false. docs/06 §3 and docs/11 §3 also require replace / change_spec / "
    "exception_accept, and this service needs option_id, payload and "
    "expected_task_revision. The contract envelope is accepted as-is; the "
    "extra fields are accepted at the service layer under this service's own "
    "request shape, so a strict client validating against openapi.v2 will be "
    "rejected before it reaches us. Needs an additive contract change.",
    "S40 G2: openapi.v2 declares only POST /review-tasks/{id}/decisions. "
    "GET /review-tasks/{id} and GET /review-tasks are served here but have no "
    "contract path.",
    "S40 G3 (partly resolved upstream): plan §6.4 wants a node-level manifest "
    "diff so a manifest revision bump that does not touch this node is NOT "
    "stale. S08 now serves GET /manifests/{id}/node-diff, so this service can "
    "use it when an appraiser is injected; without it the judgement degrades "
    "to 'revision changed => stale', which is stricter and therefore safe.",
    "S40 G4: only review.decision.recorded is declared for S40. Pending-queue "
    "and deadline alerts therefore cannot be event-driven; they need polling.",
    "S40 G5: there is no review-task.v2 / review-decision.v2 schema file, so "
    "the task and decision shapes in plan §2/§3 are still a proposal. This "
    "service implements them verbatim from the plan.",
    "S40 G6: quorum and separation-of-duties exist only in docs/06 §3 prose. "
    "S02's reviewer_roles carries roles but no quorum, so "
    "assignment.quorum is taken from the task payload and the rule check is "
    "delegated to S02's validate_review_decision.",
    "S40 G7: S02 has no validate_review_decision surface today (it serves "
    "/policy-decisions:resolve-effective and /permission-checks). The verdict "
    "is injected as a callback; when it is not configured every decision "
    "abstains with POLICY_VALIDATOR_UNCONFIGURED (503) rather than being "
    "treated as allow.",
    "S40 G8: S01's GET /authorization-context is scoped by scope_kind/"
    "scope_id and returns permissions, not review roles. Mapping 'this "
    "principal may vote on this review task' onto that surface needs a scope "
    "convention; this service asks for scope_kind='project' with the task's "
    "scope_id and checks the contract permission key `review.decide` rather "
    "than a role name. Separation of duties therefore cannot be enforced from "
    "S01 alone; it degrades to what actor.roles claims.",
    "S40 C40-1: docs/06 §3 and docs/11 §3 require replace / change_spec / "
    "exception_accept, but openapi.v2's decideReview pins decision to "
    "{accept, reject, request_repair} and forbids extra properties. This "
    "service keeps `decision` on the contract's three values and carries the "
    "UI button in `option_id`; sending e.g. decision='replace' is answered "
    "with CONTRACT_DECISION_REQUIRED (400) rather than silently coerced to "
    "reject. Needs an additive option_id/payload/expected_task_revision in "
    "the contract.",
    "S40 C40-2: openapi.v2 types the decideReview `context` as "
    "execution-context.v2, whose 18 required fields and "
    "additionalProperties:false include manifest_ref/node_id/node_epoch/"
    "execution_fence/policy_ref but have no place for lock_version, "
    "artifact.content_hash or pack_hash. Three of §6.3's seven hard binding "
    "dimensions are therefore unreachable through the contract envelope. "
    "This service accepts the envelope, checks the four dimensions it can "
    "carry, and reports the rest in binding_dimensions_unchecked (never as "
    "'passed'). Same class as S18 C18-1 / S09 C09-4.",
    "S40 C40-3: openapi.v2 declares the decideReview response as Operation "
    "(202, asynchronous). This service records the decision synchronously and "
    "returns the decision body, because a review decision is a human action "
    "with no async work of its own -- the only downstream step is the S08 "
    "accept, whose pending state is exposed as `pending_acceptance`. A "
    "contract-shaped client will not find the Operation envelope.",
    "S40 C40-4: there is no contract path for creating, listing or reading a "
    "review task, nor for the acceptance retry queue. POST /review-tasks, "
    "GET /review-tasks, GET /review-tasks/{id}, GET /decisions/{id}, "
    "GET /acceptance-retries and POST /review-tasks/{id}:retry-acceptance are "
    "all served here without a contract path.",
    "S40 G9: plan §5 draws the handoff as a single request_acceptance_CAS "
    "returning {result: committed|conflict|stale_epoch|locked}. S08 has no "
    "such surface: it requires the decision to be registered first via "
    "POST /evidence/review, then POST /manifests/{id}:accept with a "
    "decision_ref, and its response has no `result` field. deps.py performs "
    "both steps and normalises S08's error codes onto the plan's four "
    "outcomes; STALE_DECISION maps to stale_epoch and 5xx is re-raised so the "
    "kernel keeps the decision RECORDED.",
    "S40 G10: S08's review_evidence row is keyed by (decision_id, "
    "decision_revision) and stores content_hash as an opaque string, so the "
    "authority for a decision's content hash is S40. deps.py computes it as "
    "sha256 over the decision's canonical fields; any consumer that wants to "
    "verify must recompute it the same way.",
    "S40 C40-5: plan §11 E25-07 requires options[].effect.advances_pointer to "
    "never be true for an S40 task. There is no JSON Schema for the task or "
    "option shape (G5) and no contract field for `effect` at all, so this "
    "service enforces it in create_task: a request carrying "
    "effect.advances_pointer=true is refused with "
    "OPTION_CLAIMS_POINTER_ADVANCE (400) rather than stored and echoed back. "
    "Refusing instead of silently clearing the flag is deliberate -- clearing "
    "would hide a producer that believes S40 can accept on its behalf.",
    "S40 G11: reject and request_repair decisions are recorded but do not "
    "advance the accepted pointer -- S08's accept refuses a non-approved "
    "verdict (_check_decision_against). Those tasks reach DECIDED with "
    "accepted_pointer_written_by=null. There is no contract surface for 'a "
    "task was decided without a pointer change', so a consumer watching only "
    "review.decision.recorded can misread DECIDED as 'accepted'.",
]
