"""S40 业务内核：记录人说了什么，不让它变成已生效。

守住六条，对应 `plans/S40-human-review.md`：

1. **`APPLIED` 只能由 S08 的成功结果赋**（§5、§6.5）。本文件里写 `'APPLIED'` 的
   只有 `apply_downstream` 一处，而它只能由 `_release_to_s08` 收到 `committed`
   时调用。因本地判定通过就写 `APPLIED`，是 S40 最容易造第二权威的一步。
2. **binding 变了就 stale，且 stale 不进 quorum**（§6.3、§4）。`execution_fence`
   与 `node_epoch` 各自独立比较 —— 它们管的东西不同（run/branch 的主写切换 vs
   单个 `logical_node` 的局部重生成），一个变了不代表另一个变了。
3. **`deny` 与 `abstain` 都不计票**（§4）。否则没权限的人点接受能凑满 quorum，
   而"未知"被记成一张有效票等于把未知当通过（E25）。
4. **一人一票**（§10.1），由部分唯一索引兜底，不靠应用层自觉。
5. **超时不自动通过**（§7）。`expire_due` 只改状态，绝不生成 accept。
6. **幂等先于终态**。§6.2 的伪代码把"终态 → 历史留档"放在幂等之前，但那会让
   同 `command_id` 的重试撞 `UNIQUE(tenant_id, command_id)` 变成 500。平台幂等是
   更强的不变量，所以顺序调整为 闸B → 幂等 → 一人一票 → 终态 → 闸A → 闸D。
   这条偏离登记在方案 §13.1。

三条依赖都是注入的 callback，不是 import（AGENTS.md：不跨服务 import 业务 ORM）：
`authz_source`（S01 授权上下文）、`policy_validator`（S02 的
`validate_review_decision`）、`acceptance_cas`（S08 的接受）。任何一个没配都是
**显式失败**，不是静默放行。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from .schema import (
    CONTRACT_DECISIONS,
    CONTRACT_GAPS,
    HARD_BINDING_FIELDS,
    MIN_EXCEPTION_REASON,
    MIN_REASON,
    OPTION_IDS,
    SCHEMA,
    TASK_KINDS,
    TERMINAL_STATES,
)

SOURCE = "s40.human-review"
EVENT_TYPE = "review.decision.recorded"
SCHEMA_VERSION = "2.0"
TOPIC = "drama.human-review.v2"

# 授权判定用契约注册的权限键，不用角色名 —— S01 只返回 permissions（缺口 G8）。
REQUIRED_PERMISSION = "review.decide"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`isolation_level=None` 下的显式事务，与本仓其他服务一致。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def pack_hash(binding: dict, evidence: dict) -> str:
    """§2.3。任何一项被替换 → 哈希不匹配 → 决定 stale。

    这比逐字段比较更难漏：新增一类证据时忘了加进比较表，pack_hash 自己会变。
    由服务计算，不接受调用方传值 —— 否则调用方可以给一个"永远匹配"的哈希。
    """
    return _sha256_hex(_jcs({
        "manifest_ref": binding.get("manifest_ref"),
        "node_epoch": binding.get("node_epoch"),
        "lock_version": binding.get("lock_version"),
        "artifact_content_hash": (binding.get("artifact") or {}).get("content_hash"),
        "policy_ref": binding.get("policy_ref"),
        "qc_reports": [{"id": r.get("id"), "revision": r.get("revision"),
                        "content_hash": r.get("content_hash")}
                       for r in (evidence.get("qc_reports") or [])],
        "defects": [d.get("defect_id") for d in (evidence.get("defects") or [])],
        "continuity_ref": evidence.get("continuity_ref"),
    }))


def _get(mapping, dotted: str):
    value = mapping
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def compare_binding(task_binding: dict, ctx: dict,
                    node_diff=None) -> list[dict]:
    """§6.3 / §6.4。返回 mismatch 列表；空表示绑定仍有效。

    `execution_fence` 与 `node_epoch` 必须**各自**比较。它们是两种不同的版本
    语义（docs/04 §1、AGENTS.md）：fence 管 run/branch 的取消与主写切换，epoch
    管单个 `logical_node`。任一变，人批的就是另一件事了。

    未提供的字段不参与比较 —— 不能把"没给"当"没变"。

    `manifest_ref.revision` 不无条件 stale：改别的镜头只提升受影响的
    node_epoch（§6.4）。没有 node diff 能力时退化得更严 —— revision 前进即
    stale。更严可以接受，更松不行。
    """
    out = []

    def mismatch(field, reason, **extra):
        out.append({"field": field, "reason": reason, **extra})

    for field, reason in HARD_BINDING_FIELDS:
        actual = _get(ctx, field)
        if actual is not None and actual != task_binding.get(field):
            mismatch(field, reason, expected=task_binding.get(field),
                     actual=actual)

    ctx_artifact = _get(ctx, "artifact.content_hash")
    if ctx_artifact is not None \
            and ctx_artifact != _get(task_binding, "artifact.content_hash"):
        mismatch("artifact.content_hash", "ARTIFACT_REPLACED",
                 expected=_get(task_binding, "artifact.content_hash"),
                 actual=ctx_artifact)

    ctx_policy = _get(ctx, "policy_ref.content_hash")
    if ctx_policy is not None \
            and ctx_policy != _get(task_binding, "policy_ref.content_hash"):
        mismatch("policy_ref.content_hash", "POLICY_SUPERSEDED",
                 expected=_get(task_binding, "policy_ref.content_hash"),
                 actual=ctx_policy)

    if ctx.get("pack_hash") is not None \
            and ctx["pack_hash"] != task_binding.get("pack_hash"):
        mismatch("pack_hash", "EVIDENCE_CHANGED",
                 expected=task_binding.get("pack_hash"),
                 actual=ctx["pack_hash"])

    ctx_manifest = ctx.get("manifest_ref") or {}
    task_manifest = task_binding.get("manifest_ref") or {}
    if ctx_manifest.get("id") and ctx_manifest["id"] != task_manifest.get("id"):
        mismatch("manifest_ref.id", "MANIFEST_CHANGED",
                 expected=task_manifest.get("id"), actual=ctx_manifest["id"])
    elif ctx_manifest.get("revision") is not None \
            and ctx_manifest["revision"] != task_manifest.get("revision"):
        if node_diff is None:
            mismatch("manifest_ref.revision", "MANIFEST_AFFECTS_NODE",
                     degraded="no node-diff capability; a revision bump is "
                              "treated as affecting the node")
        else:
            changed = node_diff(task_manifest.get("id"),
                                task_manifest.get("revision"),
                                ctx_manifest["revision"],
                                task_binding.get("node_id"))
            if changed:
                # node_diff 只回答"本节点受不受影响"（bool）。受影响的就是任务
                # 冻结的那个节点，别把 True 当节点列表写进审计字段。
                mismatch("manifest_ref.revision", "MANIFEST_AFFECTS_NODE",
                         nodes=[task_binding.get("node_id")])
    return out


class HumanReviewService:
    def __init__(self, conn: sqlite3.Connection, *, authz_source=None,
                 policy_validator=None, acceptance_cas=None, node_diff=None,
                 clock=_now):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(conn)
        self._authz = authz_source
        self._policy = policy_validator
        self._cas = acceptance_cas
        self._node_diff = node_diff
        self._clock = clock

    # ---- 建任务 ----------------------------------------------------------

    def create_task(self, tenant_id: str, body: dict) -> dict:
        """§2。binding 与 pack_hash 在这里定死，之后不可变（触发器守）。"""
        kind = body.get("kind")
        if kind not in TASK_KINDS:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"kind must be one of {TASK_KINDS}")
        binding = body.get("binding")
        if not isinstance(binding, dict) or not binding:
            raise ServiceError(400, "MALFORMED_REQUEST", "binding is required")
        evidence = body.get("evidence") or {}
        options = body.get("options") or []
        # §5 / E25-07：只有 S08 能推进接受指针。任务选项里任何
        # `effect.advances_pointer=true` 都是一句 S40 兑现不了的承诺，
        # 存下来再回显就等于替它背书。直接拒绝，不静默抹掉（抹掉会藏起
        # 一个在撒谎的生产者）。
        for option in options:
            if not isinstance(option, dict):
                continue
            if (option.get("effect") or {}).get("advances_pointer"):
                raise ServiceError(
                    400, "OPTION_CLAIMS_POINTER_ADVANCE",
                    f"option {option.get('option_id')!r} claims to advance the "
                    "accepted pointer; only S08 may do that (S40 C40-5)")
        assignment = body.get("assignment") or {}
        quorum = assignment.get("quorum") or {}
        if int(quorum.get("required") or 0) < 1:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "assignment.quorum.required must be >= 1")

        task_id = body.get("review_task_id") or f"rt_{uuid.uuid4().hex[:20]}"
        digest = pack_hash(binding, evidence)
        binding = {**binding, "pack_hash": digest}
        now = self._clock()

        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO review_task (tenant_id, review_task_id, kind,"
                " state, revision, binding, subject, reason, evidence, options,"
                " assignment, pack_hash, deadline, created_at, created_by,"
                " source_event) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, task_id, kind, "OPEN", 1, _jcs(binding),
                 _jcs(body.get("subject") or {}),
                 _jcs(body.get("reason") or {}), _jcs(evidence),
                 _jcs(options), _jcs(assignment), digest,
                 (assignment.get("sla") or {}).get("deadline") or now,
                 now, _jcs(body.get("created_by") or {}),
                 _jcs(body["source_event"]) if body.get("source_event") else None))
            self.conn.execute(
                "INSERT INTO review_quorum_state (tenant_id, review_task_id,"
                " required, separation_of_duties, votes_valid, votes_invalid,"
                " must_include_roles) VALUES (?,?,?,?,0,0,?)",
                (tenant_id, task_id, int(quorum["required"]),
                 1 if quorum.get("separation_of_duties") else 0,
                 _jcs(quorum.get("must_include_roles") or [])))
            if assignment.get("assignee_principal_id"):
                self.conn.execute(
                    "INSERT INTO review_assignment_event (tenant_id,"
                    " review_task_id, seq, kind, from_principal, to_principal,"
                    " reason, at) VALUES (?,?,1,'ASSIGNED',NULL,?,?,?)",
                    (tenant_id, task_id, assignment["assignee_principal_id"],
                     "initial assignment", now))
        return self.get_task(tenant_id, task_id)

    # ---- 提交决定 --------------------------------------------------------

    def submit_decision(self, tenant_id: str, task_id: str, body: dict,
                        *, command_id: str) -> dict:
        """§6.2。闸 B → 幂等 → 一人一票 → 终态 → 闸 A → 闸 D → S02。"""
        task = self._task_row(tenant_id, task_id)

        # 闸 B：任务版本（乐观并发）。旧页面上的提交不落库。
        expected = body.get("expected_task_revision")
        if expected is not None and int(expected) != task["revision"]:
            raise ServiceError(
                409, "TASK_SUPERSEDED",
                "the task has moved on since this page was loaded",
                details={"current_revision": task["revision"],
                         "current_state": task["state"]})

        # `decision` 是契约三值（openapi enum），`option_id` 是 UI 上的那颗按钮。
        # 两者不能互相顶替：`replace` 的 decision 是 reject，但它的语义完全不同，
        # 混用会让 S02 把"换候选"当成"拒绝"来判。C40-1 登记了这个缺口。
        decision = body.get("decision")
        if decision not in CONTRACT_DECISIONS:
            raise ServiceError(
                400, "CONTRACT_DECISION_REQUIRED",
                f"decision must be one of {CONTRACT_DECISIONS}; the extended "
                "options (replace / change_spec / exception_accept) go in "
                "option_id (S40 C40-1)")
        option_id = body.get("option_id") or decision
        if option_id not in OPTION_IDS:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"option_id must be one of {OPTION_IDS}")

        actor = body.get("actor") or {}
        principal_id = actor.get("principal_id")
        if not principal_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "actor.principal_id is required")

        # 幂等：同 command_id 同 body → 原决定，不重复计票；不同 body → 409。
        # 放在终态判定之前：否则同命令重试会撞 UNIQUE(tenant_id, command_id)。
        prior = self.conn.execute(
            "SELECT * FROM review_decision WHERE tenant_id=? AND command_id=?",
            (tenant_id, command_id)).fetchone()
        if prior is not None:
            if prior["request_hash"] != _sha256_hex(_jcs(body)):
                raise ServiceError(409, "IDEMPOTENCY_MISMATCH",
                                   "same command_id, different body")
            return self._replay_or_release(tenant_id, task_id, prior, command_id)

        # 一人一票（§6.2）。同选项重复点是幂等，不同选项是冲突。
        existing = self.conn.execute(
            "SELECT * FROM review_decision WHERE tenant_id=? AND"
            " review_task_id=? AND json_extract(actor,'$.principal_id')=?"
            " AND status IN ('RECORDED','APPLIED')",
            (tenant_id, task_id, principal_id)).fetchone()
        if existing is not None:
            if existing["option_id"] == option_id:
                return self._replay_or_release(tenant_id, task_id, existing,
                                               command_id)
            raise ServiceError(
                409, "DUPLICATE_VOTE_DIFFERENT_OPTION",
                "this principal already voted differently on this task",
                details={"prior_option": existing["option_id"]})

        binding = json.loads(task["binding"])

        # 终态：晚到的票只留档，不回退状态、不进 quorum（§3.3）。
        if task["state"] in TERMINAL_STATES:
            return self._record(tenant_id, task, body, command_id, principal_id,
                                option_id, status="STALE",
                                stale_reason=f"TASK_{task['state']}",
                                validation={"task_state": task["state"]})

        # 闸 A：绑定。
        ctx = body.get("context") or {}
        mismatches = compare_binding(binding, ctx, self._node_diff)
        if mismatches:
            return self._record(tenant_id, task, body, command_id, principal_id,
                                option_id, status="STALE",
                                stale_reason=mismatches[0]["reason"],
                                validation={"mismatches": mismatches})

        reason = (body.get("reason") or "").strip()
        self._assert_reason(option_id, reason)

        # 闸 D + §8.2：实时授权，不缓存。撤权必须当场生效（E27）。
        authz = self._require_authorized(tenant_id, principal_id, task)
        if authz.get("problem") is not None:
            return self._record(
                tenant_id, task, body, command_id, principal_id, option_id,
                status="REJECTED_INVALID", stale_reason=None,
                validation={"code": "NOT_AUTHORIZED_FOR_TASK",
                            "authz": authz}, authz=authz)

        # §4：交给 S02 判权限/条件/quorum 规则。S40 不自己算。
        if self._policy is None:
            raise ServiceError(
                503, "POLICY_VALIDATOR_UNCONFIGURED",
                "no S02 validate_review_decision is configured; the decision "
                "is unknown, not allowed", retryable=True)
        verdict = self._policy(tenant_id, dict(task), body, authz)
        verdict_kind = verdict.get("verdict")

        if verdict_kind == "deny":
            return self._record(
                tenant_id, task, body, command_id, principal_id, option_id,
                status="REJECTED_INVALID", stale_reason=None,
                validation=verdict, authz=authz)
        if verdict_kind == "abstain":
            # 不落库：没有决定内容，只有一次失败的调用（§4 末段）。
            raise ServiceError(
                503, "POLICY_ABSTAINED",
                "S02 could not decide; no vote is recorded",
                retryable=True, details=verdict)

        recorded = self._record(
            tenant_id, task, body, command_id, principal_id, option_id,
            status="RECORDED", stale_reason=None, validation=verdict,
            authz=authz)
        self._count_vote(tenant_id, task_id, valid=True)

        if verdict_kind == "allow":
            if self._quorum_satisfied(tenant_id, task_id):
                recorded = self._release_or_503(tenant_id, task_id, command_id,
                                                recorded)
            elif task["state"] == "OPEN":
                self._set_task_state(tenant_id, task_id, "IN_REVIEW")
        else:  # require_human：这一票有效，但还差换人/复核
            self._set_task_state(tenant_id, task_id, "PARTIALLY_DECIDED")
        return recorded

    def _replay_or_release(self, tenant_id: str, task_id: str, row,
                           command_id: str) -> dict:
        """重放已有决定。若 quorum 已满而它仍是 RECORDED，补做释放。

        没有这一步，S08 不可达后客户端的重试会永远停在 RECORDED —— 重试路径
        走幂等分支，不会再尝试 CAS。
        """
        rendered = self._render_decision(row)
        if row["status"] != "RECORDED":
            return rendered
        if not self._quorum_satisfied(tenant_id, task_id):
            return rendered
        return self._release_or_503(tenant_id, task_id, command_id, rendered)

    def _release_or_503(self, tenant_id: str, task_id: str, command_id: str,
                        fallback: dict) -> dict:
        try:
            self._release_to_s08(tenant_id, task_id, command_id)
        except ServiceError as exc:
            if exc.status != 503:
                raise
            # §5.1：不可达保持 RECORDED，已进本地重试队列，不降级为本地接受。
            return {**fallback, "pending_acceptance": True,
                    "x-contract-gaps": CONTRACT_GAPS}
        return self.get_decision(tenant_id, fallback["decision_id"])

    def _assert_reason(self, option_id: str, reason: str) -> None:
        if option_id == "exception_accept":
            if len(reason) < MIN_EXCEPTION_REASON:
                raise ServiceError(
                    422, "REASON_TOO_SHORT",
                    f"exception_accept needs >= {MIN_EXCEPTION_REASON} chars")
            return
        if len(reason) < MIN_REASON:
            raise ServiceError(
                422, "REASON_TOO_SHORT",
                f"reason needs >= {MIN_REASON} chars (a punctuation mark is "
                "not a reason)")

    def _require_authorized(self, tenant_id: str, principal_id: str,
                            task) -> dict:
        if self._authz is None:
            raise ServiceError(
                503, "AUTHZ_UNCONFIGURED",
                "no S01 authorization source is configured; decisions cannot "
                "be validated", retryable=True)
        authz = self._authz(tenant_id, principal_id, dict(task)) or {}
        if authz.get("principal_status") not in (None, "active") \
                or authz.get("tenant_lifecycle") not in (None, "active"):
            return {**authz, "problem": "PRINCIPAL_OR_TENANT_INACTIVE"}
        if REQUIRED_PERMISSION in (authz.get("denied") or []):
            return {**authz, "problem": "EXPLICITLY_DENIED"}
        if REQUIRED_PERMISSION not in (authz.get("permissions") or []):
            return {**authz, "problem": "MISSING_PERMISSION"}
        return authz

    def _record(self, tenant_id, task, body, command_id, principal_id,
                option_id, *, status, stale_reason, validation,
                authz=None) -> dict:
        """写一条决定。`APPLIED` 永远不从这里产生。"""
        task_binding = json.loads(task["binding"])
        decided_against = {k: task_binding.get(k) for k in
                           ("manifest_ref", "execution_fence", "node_epoch",
                            "lock_version", "policy_ref", "pack_hash", "node_id")}
        decided_against["artifact_content_hash"] = _get(
            task_binding, "artifact.content_hash")
        decision_id = f"rd_{uuid.uuid4().hex[:20]}"
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO review_decision (tenant_id, decision_id,"
                " review_task_id, command_id, request_hash, decided_against,"
                " decision, option_id, reason, payload, actor, authz_snapshot,"
                " recorded_at, status, stale_reason, validation, downstream)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, decision_id, task["review_task_id"], command_id,
                 _sha256_hex(_jcs(body)), _jcs(decided_against),
                 body["decision"], option_id, body.get("reason") or "",
                 _jcs(body.get("payload") or {}),
                 _jcs(body.get("actor") or {}), _jcs(authz or {}),
                 self._clock(), status, stale_reason, _jcs(validation or {}),
                 _jcs({})))
            if status in ("STALE", "REJECTED_INVALID"):
                self.conn.execute(
                    "UPDATE review_quorum_state SET votes_invalid=votes_invalid+1"
                    " WHERE tenant_id=? AND review_task_id=?",
                    (tenant_id, task["review_task_id"]))
        return self.get_decision(tenant_id, decision_id)

    def _count_vote(self, tenant_id, task_id, *, valid: bool) -> None:
        column = "votes_valid" if valid else "votes_invalid"
        with Transaction(self.conn):
            self.conn.execute(
                f"UPDATE review_quorum_state SET {column}={column}+1"
                " WHERE tenant_id=? AND review_task_id=?",
                (tenant_id, task_id))

    def _quorum_satisfied(self, tenant_id, task_id) -> bool:
        row = self.conn.execute(
            "SELECT * FROM review_quorum_state WHERE tenant_id=?"
            " AND review_task_id=?", (tenant_id, task_id)).fetchone()
        if row is None or row["votes_valid"] < row["required"]:
            return False
        if not row["separation_of_duties"]:
            return True
        # 职责分离：每个 must_include_role 都得有一位投票人担任。
        # 角色来自投票时的 actor.roles —— S01 不返回角色（缺口 G8），
        # 所以缺这一项时判"不满足"，不是"默认满足"。
        roles = json.loads(row["must_include_roles"])
        if not roles:
            return True
        voters = self.conn.execute(
            "SELECT actor FROM review_decision WHERE tenant_id=? AND"
            " review_task_id=? AND status='RECORDED'",
            (tenant_id, task_id)).fetchall()
        held = set()
        for voter in voters:
            held |= set(json.loads(voter["actor"]).get("roles") or [])
        return set(roles) <= held

    # ---- 生效：S08 接受 ------------------------------------------------

    def _release_to_s08(self, tenant_id: str, task_id: str,
                        command_id: str) -> None:
        """§5。只有 S08 的 `committed` 能把决定变成 `APPLIED`。

        适配层（deps.py）负责把它翻译成 S08 真实的两步：先 `/evidence/review`
        登记决定，再 `/manifests/{id}:accept`。这里的 `result` 是适配层的归一化
        输出，不是 S08 的原样响应 —— 后者没有 `result` 字段（缺口 G9）。
        """
        task = self._task_row(tenant_id, task_id)
        if self._cas is None:
            # 不可达不是"大概能过"：留在 RECORDED，进本地重试队列。
            self._enqueue_retry(tenant_id, task_id, command_id,
                                "ACCEPTANCE_AUTHORITY_UNCONFIGURED")
            raise ServiceError(
                503, "ACCEPTANCE_AUTHORITY_UNCONFIGURED",
                "S08 accept is not configured; the decision stays RECORDED",
                retryable=True)
        decisions = [self._render_decision(r) for r in self.conn.execute(
            "SELECT * FROM review_decision WHERE tenant_id=? AND"
            " review_task_id=? AND status='RECORDED' ORDER BY recorded_at",
            (tenant_id, task_id)).fetchall()]
        try:
            result = self._cas(tenant_id, {**dict(task), "_decisions": decisions},
                               command_id) or {}
        except ServiceError:
            raise
        except Exception as exc:  # 上游不可达 / 非契约异常
            self._enqueue_retry(tenant_id, task_id, command_id, str(exc))
            raise ServiceError(
                503, "ACCEPTANCE_AUTHORITY_UNREACHABLE",
                "S08 is unreachable; the decision stays RECORDED",
                retryable=True)

        outcome = result.get("result")
        if outcome == "committed":
            self.apply_downstream(tenant_id, task_id, cas_result=result)
            return
        if outcome == "recorded_only":
            # reject / request_repair 不是接受动作。S08 不推进指针，这是对的：
            # 把任务标成 DECIDED 会让人以为"已生效"，而它只是"决定了不同意"。
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE review_decision SET downstream=?"
                    " WHERE tenant_id=? AND review_task_id=?"
                    " AND status='RECORDED'",
                    (_jcs({"cas_result": result}), tenant_id, task_id))
                self.conn.execute(
                    "UPDATE review_task SET state='DECIDED',"
                    " revision=revision+1 WHERE tenant_id=? AND review_task_id=?",
                    (tenant_id, task_id))
                self.conn.execute(
                    "DELETE FROM acceptance_retry WHERE tenant_id=?"
                    " AND review_task_id=?", (tenant_id, task_id))
                self._publish_recorded(tenant_id, task_id, result)
            return
        reason = {"conflict": "MANIFEST_ADVANCED",
                  "stale_epoch": "NODE_EPOCH_ADVANCED",
                  "locked": "NODE_LOCKED"}.get(outcome, "BINDING_MISMATCH")
        # t0 的本地判定没错，是窗口里变了（§6.5）。留下 S08 的原话。
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE review_decision SET status='STALE', stale_reason=?,"
                " downstream=? WHERE tenant_id=? AND review_task_id=?"
                " AND status='RECORDED'",
                (reason, _jcs({"cas_result": result}), tenant_id, task_id))
            self.conn.execute(
                "UPDATE review_task SET state='SUPERSEDED',"
                " revision=revision+1 WHERE tenant_id=? AND review_task_id=?",
                (tenant_id, task_id))
            # 不变式：每次状态终结都要发事件。少了这一条，本地任务页显示
            # SUPERSEDED 而下游投影还停在 IN_REVIEW（C-04 的反例）。
            self._publish_recorded(tenant_id, task_id, result)

    def apply_downstream(self, tenant_id: str, task_id: str,
                         *, cas_result: dict) -> dict:
        """把 S08 的成功结果落成 `APPLIED`。这是 `APPLIED` 的唯一入口。"""
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE review_decision SET status='APPLIED', downstream=?"
                " WHERE tenant_id=? AND review_task_id=? AND status='RECORDED'",
                (_jcs({"cas_result": cas_result}), tenant_id, task_id))
            self.conn.execute(
                "UPDATE review_quorum_state SET satisfied_at=?"
                " WHERE tenant_id=? AND review_task_id=? AND satisfied_at IS NULL",
                (self._clock(), tenant_id, task_id))
            self.conn.execute(
                "UPDATE review_task SET state='DECIDED', revision=revision+1"
                " WHERE tenant_id=? AND review_task_id=?",
                (tenant_id, task_id))
            self.conn.execute(
                "DELETE FROM acceptance_retry WHERE tenant_id=? AND"
                " review_task_id=?", (tenant_id, task_id))
            self._publish_recorded(tenant_id, task_id, cas_result)
        return self.get_task(tenant_id, task_id)

    def _enqueue_retry(self, tenant_id, task_id, command_id, error) -> None:
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO acceptance_retry (tenant_id, review_task_id,"
                " command_id, attempt_count, next_attempt_at, last_error)"
                " VALUES (?,?,?,1,?,?) ON CONFLICT(tenant_id, review_task_id)"
                " DO UPDATE SET attempt_count=attempt_count+1,"
                " next_attempt_at=excluded.next_attempt_at,"
                " last_error=excluded.last_error",
                (tenant_id, task_id, command_id, self._clock(), str(error)))

    def pending_acceptance(self, tenant_id: str, limit: int = 50) -> dict:
        """§5.1 重试队列。进程重启后靠这张表补做，不靠内存。"""
        rows = self.conn.execute(
            "SELECT * FROM acceptance_retry WHERE tenant_id=?"
            " ORDER BY next_attempt_at LIMIT ?", (tenant_id, limit)).fetchall()
        return {"pending": [{k: row[k] for k in row.keys()} for row in rows],
                "x-contract-gaps": CONTRACT_GAPS}

    def retry_pending(self, tenant_id: str, task_id: str) -> dict:
        """重试用同一个 command_id —— S08 幂等，不会重复接受。"""
        row = self.conn.execute(
            "SELECT * FROM acceptance_retry WHERE tenant_id=?"
            " AND review_task_id=?", (tenant_id, task_id)).fetchone()
        if row is None:
            raise ServiceError(404, "RETRY_NOT_FOUND",
                               "no pending acceptance for this task")
        self._release_to_s08(tenant_id, task_id, row["command_id"])
        return self.get_task(tenant_id, task_id)

    # ---- supersede / expire -------------------------------------------

    def supersede(self, tenant_id: str, task_id: str, reason: str) -> dict:
        """§3.3 `*→SUPERSEDED`。终态不回退。"""
        task = self._task_row(tenant_id, task_id)
        if task["state"] in TERMINAL_STATES:
            return self.get_task(tenant_id, task_id)
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE review_task SET state='SUPERSEDED', revision=revision+1"
                " WHERE tenant_id=? AND review_task_id=?", (tenant_id, task_id))
            self.conn.execute(
                "UPDATE review_decision SET status='STALE', stale_reason=?"
                " WHERE tenant_id=? AND review_task_id=? AND status='RECORDED'",
                (reason, tenant_id, task_id))
            self._publish_recorded(tenant_id, task_id, {"result": "superseded"})
        return self.get_task(tenant_id, task_id)

    def expire_due(self, tenant_id: str, now: str | None = None) -> dict:
        """§7。只改状态并作废旧票，**绝不生成 accept**。"""
        now = now or self._clock()
        rows = self.conn.execute(
            "SELECT review_task_id FROM review_task WHERE tenant_id=?"
            " AND state NOT IN ('DECIDED','EXPIRED','SUPERSEDED','WITHDRAWN')"
            " AND deadline <= ?", (tenant_id, now)).fetchall()
        expired = []
        for row in rows:
            task_id = row["review_task_id"]
            with Transaction(self.conn):
                changed = self.conn.execute(
                    "UPDATE review_task SET state='EXPIRED', revision=revision+1"
                    " WHERE tenant_id=? AND review_task_id=?"
                    " AND state NOT IN ('DECIDED','EXPIRED','SUPERSEDED',"
                    "'WITHDRAWN')", (tenant_id, task_id)).rowcount
                if changed:
                    # §6.4 第 3 条：绑定没变的过期票也仍不推进指针。
                    self.conn.execute(
                        "UPDATE review_decision SET status='STALE',"
                        " stale_reason='DEADLINE_PASSED' WHERE tenant_id=?"
                        " AND review_task_id=? AND status='RECORDED'",
                        (tenant_id, task_id))
                    self._publish_recorded(tenant_id, task_id,
                                           {"result": "expired"})
                    expired.append(task_id)
        return {"expired": expired, "x-contract-gaps": CONTRACT_GAPS}

    # ---- 查询 ----------------------------------------------------------

    def get_task(self, tenant_id: str, task_id: str) -> dict:
        return self._render_task(self._task_row(tenant_id, task_id))

    def get_decision(self, tenant_id: str, decision_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM review_decision WHERE tenant_id=? AND decision_id=?",
            (tenant_id, decision_id)).fetchone()
        if row is None:
            raise ServiceError(404, "DECISION_NOT_FOUND", "no such decision")
        return self._render_decision(row)

    def list_tasks(self, tenant_id: str, *, state: str | None = None,
                   limit: int = 50) -> dict:
        if state:
            rows = self.conn.execute(
                "SELECT * FROM review_task WHERE tenant_id=? AND state=?"
                " ORDER BY created_at DESC LIMIT ?",
                (tenant_id, state, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM review_task WHERE tenant_id=?"
                " ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit)).fetchall()
        return {"tasks": [self._render_task(r) for r in rows],
                "x-contract-gaps": CONTRACT_GAPS}

    def _task_row(self, tenant_id: str, task_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM review_task WHERE tenant_id=? AND review_task_id=?",
            (tenant_id, task_id)).fetchone()
        if row is None:
            raise ServiceError(404, "TASK_NOT_FOUND", "no such review task")
        return row

    def _set_task_state(self, tenant_id: str, task_id: str, state: str) -> None:
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE review_task SET state=?, revision=revision+1"
                " WHERE tenant_id=? AND review_task_id=?",
                (state, tenant_id, task_id))

    def _publish_recorded(self, tenant_id: str, task_id: str,
                          cas_result: dict) -> None:
        task = self._task_row(tenant_id, task_id)
        decisions = self.conn.execute(
            "SELECT * FROM review_decision WHERE tenant_id=? AND"
            " review_task_id=? AND status IN ('APPLIED','RECORDED')"
            " ORDER BY recorded_at",
            (tenant_id, task_id)).fetchall()
        binding = json.loads(task["binding"])
        advanced = cas_result.get("result") == "committed"
        data = {
            "schema_version": SCHEMA_VERSION,
            "review_task_id": task_id,
            # 事件消费者靠它复位自己的投影。没有它，一个 SUPERSEDED 的任务
            # 在下游看起来和 OPEN 一样。
            "task_state": task["state"],
            "task_revision": task["revision"],
            "decision_ids": [d["decision_id"] for d in decisions],
            # 语义是"记录了一个决定"，不是"已接受"。消费者不得把它当接受结果用
            # （§6.5 的事件命名陷阱）。
            "recorded": True,
            # 这条是唯一能被下游当"指针动没动"看的字段，且它来自 S08 的结果。
            "accepted_pointer_written_by": "S08" if advanced else None,
            "acceptance_commit_id": cas_result.get("acceptance_commit_id"),
            "decision_count": len(decisions),
            # 理由正文不进事件：评审意见可能含敏感内容（§10.2）。
        }
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_TYPE,
            subject=f"review-task/{task_id}",
            tenant_id=tenant_id,
            home_cell=binding.get("home_cell") or "cell_a",
            aggregate_id=task_id,
            sequence=task["revision"],
            correlation_id=task_id,
            data=data))

    def _render_task(self, row: sqlite3.Row) -> dict:
        quorum = self.conn.execute(
            "SELECT * FROM review_quorum_state WHERE tenant_id=?"
            " AND review_task_id=?",
            (row["tenant_id"], row["review_task_id"])).fetchone()
        decisions = self.conn.execute(
            "SELECT * FROM review_decision WHERE tenant_id=? AND"
            " review_task_id=? ORDER BY recorded_at",
            (row["tenant_id"], row["review_task_id"])).fetchall()
        return {
            "schema_version": SCHEMA_VERSION,
            "review_task_id": row["review_task_id"],
            "tenant_id": row["tenant_id"],
            "kind": row["kind"],
            "state": row["state"],
            "revision": row["revision"],
            "binding": json.loads(row["binding"]),
            "subject": json.loads(row["subject"]),
            "reason": json.loads(row["reason"]),
            "evidence": json.loads(row["evidence"]),
            "options": json.loads(row["options"]),
            "assignment": json.loads(row["assignment"]),
            "pack_hash": row["pack_hash"],
            "deadline": row["deadline"],
            "created_at": row["created_at"],
            "quorum": None if quorum is None else {
                "required": quorum["required"],
                "separation_of_duties": bool(quorum["separation_of_duties"]),
                "votes_valid": quorum["votes_valid"],
                "votes_invalid": quorum["votes_invalid"],
                "must_include_roles": json.loads(quorum["must_include_roles"]),
                "satisfied_at": quorum["satisfied_at"]},
            "decisions": [self._render_decision(d) for d in decisions],
            "x-contract-gaps": CONTRACT_GAPS,
        }

    def _render_decision(self, row: sqlite3.Row) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "decision_id": row["decision_id"],
            "review_task_id": row["review_task_id"],
            "tenant_id": row["tenant_id"],
            "command_id": row["command_id"],
            "decided_against": json.loads(row["decided_against"]),
            "decision": row["decision"],
            "option_id": row["option_id"],
            "reason": row["reason"],
            "payload": json.loads(row["payload"]),
            "actor": json.loads(row["actor"]),
            "authz_snapshot": json.loads(row["authz_snapshot"]),
            "recorded_at": row["recorded_at"],
            "status": row["status"],
            "stale_reason": row["stale_reason"],
            "validation": json.loads(row["validation"]) if row["validation"]
                          else None,
            "downstream": json.loads(row["downstream"]),
            "x-contract-gaps": CONTRACT_GAPS,
        }
