"""S17 业务内核：只读的连续性校验。

这个文件要守住的五条，对应 `plans/S17-continuity.md` 的硬规则：

1. **不存事实**（§0 / §10 反模式 1）。库里唯一的"状态"是 `expected_state`，
   每行强制带 `source_claim_ref` + `checkpoint`。对正史的唯一接口是注入进来的
   `memory_query`；不 import S16、不连它的库、不缓存它的事实。
2. **水位不够就 abstain**（§6）。`MEMORY_WATERMARK_BEHIND` 与 `CHECKPOINT_MISMATCH`
   都把整次检查打成 `abstained`。绝不拿旧 checkpoint 凑合 —— 那会漏报知识越界，
   是产品事故级别的问题。
3. **低置信度 abstain，不降级**（§2.3 末段）。0.4 置信度的知识越界报成 low，
   等于训练团队忽略告警。
4. **先证明合法，再报缺陷**（§3）。闪回、主观镜头、世界规则、显式豁免四条都走
   同一条 `_classify` 管线，判断顺序写死在代码顺序里。suppress 的缺陷留在报告里，
   带 `suppression_reason` ——"压制"和"没检出"在审计上完全不同。
5. **没有覆盖率的 pass 不算 pass**（§7.1）。`coverage_ratio < 0.85` 一律不能 `pass`。

观测由注入的 `shot_source` 提供（S06 镜头 DSL / S10/S28 资产与关键帧的聚合结果）。
这是一条只读依赖，和 `memory_query` 同一个模式：**没注入就不检查**，而不是猜。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from .schema import (
    ABSTAIN_THRESHOLD,
    COVERAGE_FLOOR,
    DEFAULT_RULE,
    FRAME_VALUES,
    NEEDS_HUMAN_CATEGORIES,
    RAISE_RULES,
    SCHEMA,
)

SOURCE = "s17.continuity"
EVENT_TYPE = "continuity.check.completed"

PROFILES = ("text_logic", "visual", "audio", "full")
POOLS = ("text_logic", "visual", "audio")
VERDICTS = ("pass", "pass_with_warnings", "defects", "abstained")
SEVERITY_ORDER = {"none": 0, "info": 1, "low": 2, "medium": 3, "high": 4,
                  "blocking": 5}

# §5.3 的分派表在这个服务里的落点。S17 只给提示，不给修复计划。
FAULT_OWNER = {
    "KNOWLEDGE_VIOLATION": ("script", "s04", "rewrite_dialogue_or_add_reveal"),
    "UNMARKED_KNOWLEDGE_CONTRADICTION": ("script", "s04",
                                         "annotate_dialogue_intent"),
    "THREAD_DOUBLE_PAYOFF": ("script", "s04", "merge_or_reassign_payoff"),
    "FLASHBACK_MISFRAME": ("script", "s04", "tag_scene_intent_as_flashback"),
    "WORLD_RULE_BREACH": ("script", "s04", "align_event_with_world_rule"),
    "HOLDER_MISMATCH": ("script", "s04", "fix_prop_holder_or_annotate_action"),
    "INJURY_REGRESSION": ("render", "s29", "regenerate_shot_with_injury_state"),
    "VISUAL_STATE_MISMATCH": ("render", "s29",
                              "regenerate_shot_with_expected_state"),
    "COSTUME_SWITCH": ("asset", "s12", "restore_costume_or_add_change_event"),
    "PROP_VISUAL_MISMATCH": ("asset", "s14", "swap_to_canonical_prop_variant"),
    "SCREEN_DIRECTION_BREACH": ("storyboard", "s06", "review_axis_crossing"),
    "MISSING_EVIDENCE": ("unknown", None, "provide_keyframe_or_video"),
    "COVERAGE_GAP": ("unknown", None, "extend_check_coverage"),
}

# 这些类别靠剧本定位。§5.3：blocking 时没有 source span 就只能交人。
SCRIPT_OWNED = {c for c, (h, _o, _a) in FAULT_OWNER.items() if h == "script"}

# §3.1：知识类观测走 (knower, proposition) 两条支线，不按 (entity, predicate) 和正史
# 值比对。它们的命题靠观测自带的 `proposition_claim_id` 指定 —— 正史里没有
# `(角色, held_by)` 这种槽位，硬要一条期望才能判，就等于要求正史存一份"角色知道什么"
# 的副本。
KNOWLEDGE_CATEGORIES = ("KNOWLEDGE_VIOLATION", "UNMARKED_KNOWLEDGE_CONTRADICTION")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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


def _upstream_code(exc: BaseException) -> str | None:
    """上游错误码。按属性认，不按类型认。

    依赖是注入进来的 callback，它抛的是**它自己那个服务**的 `ServiceError` 类；
    按类型 `except` 会漏掉。§6 的水位门是这条链上唯一的硬分支，不能靠类型巧合成立。
    """
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else None


def _upstream_details(exc: BaseException) -> dict:
    details = getattr(exc, "details", None)
    return details if isinstance(details, dict) else {}


class Transaction:
    """`isolation_level=None` 下 `with conn` 不开事务，必须手写。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


def _str_value(value) -> str:
    """期望值/观测值统一成可读串。结构化的东西序列化，标量原样。"""
    if isinstance(value, str):
        return value
    return _jcs(value)


class ContinuityService:
    def __init__(self, conn: sqlite3.Connection, *, memory_query,
                 shot_source=None, clock=_now):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(conn)
        self._memory_query = memory_query
        self._shot_source = shot_source
        self._clock = clock

    # ---- 入口 ------------------------------------------------------------

    def run_check(self, tenant_id: str, body: dict) -> dict:
        request = self._validate(tenant_id, body)
        checkpoint = request["memory_checkpoint"]
        min_seq = checkpoint["commit_sequence"]
        branch_id = checkpoint["branch_id"]

        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO check_run (tenant_id, check_id, command_id, series_id,"
                " branch_id, episode_id, episode_order, manifest_ref,"
                " memory_checkpoint, context_pack_ref, check_profile, scope, state,"
                " started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'running', ?)",
                (tenant_id, request["check_id"], request["command_id"],
                 request["series_id"], branch_id, request["episode_id"],
                 request["episode_order"], _jcs(request["manifest_ref"]),
                 _jcs(checkpoint), request["context_pack_ref"],
                 request["profile"], _jcs(request["scope"]), self._clock()))

        try:
            query = self._memory_query(tenant_id, request["series_id"], {
                "branch_id": branch_id,
                "minimum_checkpoint": min_seq,
                "as_of_commit_sequence": min_seq,
            })
        except Exception as exc:
            # §6：S16 说水位不够，整次检查 abstain。不重试、不降级、不返回 pass。
            if _upstream_code(exc) == "MEMORY_WATERMARK_BEHIND":
                return self._abstain(tenant_id, request, "MEMORY_WATERMARK_BEHIND",
                                     _upstream_details(exc))
            raise

        actual_hash = query.get("checkpoint_hash")
        if query.get("checkpoint") != min_seq:
            return self._abstain(tenant_id, request, "CHECKPOINT_MISMATCH",
                                 {"declared": min_seq,
                                  "actual": query.get("checkpoint")})
        if checkpoint.get("content_hash") != actual_hash:
            return self._abstain(tenant_id, request, "CHECKPOINT_MISMATCH",
                                 {"declared_hash": checkpoint.get("content_hash"),
                                  "actual_hash": actual_hash})

        return self._evaluate(tenant_id, request, query)

    def get_report(self, tenant_id: str, report_id: str) -> dict:
        """§5.1。跨租户一律 404 —— 不泄漏存在性。"""
        report = self.conn.execute(
            "SELECT * FROM continuity_report WHERE tenant_id=? AND report_id=?",
            (tenant_id, report_id)).fetchone()
        if report is None:
            raise ServiceError(404, "REPORT_NOT_FOUND", report_id)
        defects = self.conn.execute(
            "SELECT * FROM defect WHERE tenant_id=? AND report_id=?"
            " ORDER BY defect_id", (tenant_id, report_id)).fetchall()
        expected = self.conn.execute(
            "SELECT * FROM expected_state WHERE tenant_id=? AND report_id=?"
            " ORDER BY unit_id, entity_id, predicate",
            (tenant_id, report_id)).fetchall()
        applications = self.conn.execute(
            "SELECT * FROM exemption_application WHERE tenant_id=? AND report_id=?",
            (tenant_id, report_id)).fetchall()
        return {
            "report": self._render_report(report),
            "defects": [self._render_defect(r) for r in defects],
            "expected_state": [dict(r) for r in expected],
            "exemption_applications": [dict(r) for r in applications],
        }

    # ---- 请求校验 --------------------------------------------------------

    def _validate(self, tenant_id: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ServiceError(400, "MALFORMED_REQUEST", "body must be an object")
        for field in ("command_id", "series_id", "episode_id", "manifest_ref",
                      "memory_checkpoint", "check_profile", "scope"):
            if field not in body:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"{field} is required")
        profile = body["check_profile"]
        if profile not in PROFILES:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"check_profile must be one of {PROFILES}")
        manifest = body["manifest_ref"]
        if not isinstance(manifest, dict) or not manifest.get("id") \
                or manifest.get("revision") is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "manifest_ref needs id and revision")
        checkpoint = body["memory_checkpoint"]
        if not isinstance(checkpoint, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "memory_checkpoint must be an object")
        branch_id = checkpoint.get("branch_id") or body.get("branch_id")
        if not branch_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "memory_checkpoint.branch_id or branch_id is required")
        seq = checkpoint.get("commit_sequence")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "memory_checkpoint.commit_sequence must be >= 0")
        # `content_hash` 不填就无从比对（§6 的 CHECKPOINT_MISMATCH 靠它），
        # 所以它是必需的；但 genesis 也有确定的 hash，不是特例。
        if not isinstance(checkpoint.get("content_hash"), str):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "memory_checkpoint.content_hash is required")
        scope = body["scope"]
        if not isinstance(scope, dict) or not isinstance(scope.get("shot_ids"), list):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "scope.shot_ids is required")
        episode_order = body.get("episode_order")
        if episode_order is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "episode_order is required")
        check_id = body.get("check_id") or ("chk_" + uuid.uuid4().hex[:24])
        return {
            "check_id": check_id,
            "command_id": body["command_id"],
            "series_id": body["series_id"],
            "episode_id": body["episode_id"],
            "episode_order": int(episode_order),
            "manifest_ref": manifest,
            "memory_checkpoint": {**checkpoint, "branch_id": branch_id,
                                  "commit_sequence": seq},
            "context_pack_ref": body.get("context_pack_ref"),
            "context_pack": body.get("context_pack"),
            "profile": profile,
            "scope": scope,
            "exemption_ids": body.get("exemption_ids") or [],
            "expected_report_revision": body.get("expected_report_revision"),
            "agent_task_ref": self._agent_ref(body),
            "home_cell": body.get("home_cell", "local"),
            "correlation_id": body.get("correlation_id") or body["command_id"],
        }

    @staticmethod
    def _agent_ref(body: dict) -> str | None:
        """§7.2：把 definition revision + model snapshot + seed 钉进报告。"""
        agent = body.get("agent")
        if not isinstance(agent, dict):
            return None
        parts = [f"{k}={agent.get(k)}" for k in
                 ("definition_revision", "model_snapshot", "seed")
                 if agent.get(k) is not None]
        return ";".join(parts) or None

    def _abstain(self, tenant_id: str, request: dict, reason: str,
                 details: dict) -> dict:
        """§6：整次检查 abstain。不产生任何 defect 行，也不发布 pass。"""
        candidates = self._candidate_count(request)
        with Transaction(self.conn):
            report_id = self._insert_report(
                tenant_id, request, verdict="abstained", severity_max="none",
                defect_count=0, abstain_count=candidates, coverage_ratio=0.0,
                covered_scope={"shot_ids": [], "shots_requested":
                               len(request["scope"]["shot_ids"])},
                abstain_reason=reason)
            self.conn.execute(
                "UPDATE check_run SET state='abstained', completed_at=?, report_id=?"
                " WHERE tenant_id=? AND check_id=?",
                (self._clock(), report_id, tenant_id, request["check_id"]))
            self._publish(tenant_id, request, report_id, "abstained", "none", 0,
                          candidates, 0.0, [], reason)
        return {"check_id": request["check_id"], "report_id": report_id,
                "verdict": "abstained", "severity_max": "none",
                "defect_count": 0, "abstain_count": candidates,
                "coverage_ratio": 0.0, "reason": reason, "details": details}

    @staticmethod
    def _candidate_count(request: dict) -> int:
        """abstain 时把"本来要查的量"记下来 —— 否则 abstain 和无缺陷长得一样。"""
        return len(request["scope"]["shot_ids"])

    # ---- 主流程 ----------------------------------------------------------

    def _evaluate(self, tenant_id: str, request: dict, query: dict) -> dict:
        shots = self._load_shots(tenant_id, request)
        expectations = self._materialize(tenant_id, request, shots, query)
        defects = self._judge(tenant_id, request, shots, expectations, query)
        coverage, covered = self._coverage(shots, request)

        if request.get("context_pack"):
            defects.extend(self._coverage_gap_from_pack(request, expectations))
        if coverage < COVERAGE_FLOOR and covered["unexamined_shot_ids"]:
            # 只补一条"有素材却没检查"的 gap。全部缺片时 `MISSING_EVIDENCE` 已经
            # 逐镜头说清楚为什么没检查，再叠一条 COVERAGE_GAP 是同义重复。
            defects.append(self._coverage_gap_defect(request, coverage))

        raised = [d for d in defects
                  if d["disposition"] in ("raise", "needs_human")]
        abstained = [d for d in defects if d["disposition"] == "abstain"]
        severity_max = "none"
        for d in raised:
            if SEVERITY_ORDER[d["severity"]] > SEVERITY_ORDER[severity_max]:
                severity_max = d["severity"]
        verdict = self._verdict(raised, abstained, coverage)

        with Transaction(self.conn):
            report_id = self._insert_report(
                tenant_id, request, verdict=verdict, severity_max=severity_max,
                defect_count=len(raised), abstain_count=len(abstained),
                coverage_ratio=coverage, covered_scope=covered)
            for expectation in expectations:
                self._insert_expected(tenant_id, report_id, expectation)
            for defect in defects:
                self._insert_defect(tenant_id, report_id, defect)
            for defect in defects:
                if defect.get("exemption_id"):
                    self.conn.execute(
                        "INSERT INTO exemption_application (tenant_id, report_id,"
                        " defect_id, exemption_id, applied_at)"
                        " VALUES (?,?,?,?,?)",
                        (tenant_id, report_id, defect["defect_id"],
                         defect["exemption_id"], self._clock()))
            self.conn.execute(
                "UPDATE check_run SET state='completed', completed_at=?, report_id=?"
                " WHERE tenant_id=? AND check_id=?",
                (self._clock(), report_id, tenant_id, request["check_id"]))
            self._publish(tenant_id, request, report_id, verdict, severity_max,
                          len(raised), len(abstained), coverage, raised, None)

        return {"check_id": request["check_id"], "report_id": report_id,
                "verdict": verdict, "severity_max": severity_max,
                "defect_count": len(raised), "abstain_count": len(abstained),
                "coverage_ratio": coverage}

    def _load_shots(self, tenant_id: str, request: dict) -> list[dict]:
        """读镜头。缺一个镜头 → 该镜头记 MISSING_EVIDENCE，不算覆盖。"""
        shots = []
        for shot_id in request["scope"]["shot_ids"]:
            shot = None
            if self._shot_source is not None:
                shot = self._shot_source(tenant_id, request["series_id"],
                                         request["memory_checkpoint"]["branch_id"],
                                         shot_id)
            if shot is None:
                shots.append({"shot_id": shot_id, "missing": True,
                              "duration_ms": 0, "observations": []})
            else:
                shots.append({"missing": False, "shot_id": shot_id, **shot})
        return shots

    def _materialize(self, tenant_id: str, request: dict, shots: list[dict],
                     query: dict) -> list[dict]:
        """从 S16 已取到的事实里挑出本次观测涉及的那些，写成 expected_state。

        只挑被观测的 `(entity, predicate)`：把整个正史落成本地副本就是反模式 1。
        """
        claims = query.get("claims") or []
        by_subject: dict[tuple[str, str], dict] = {}
        for claim in claims:
            fact = claim.get("fact") or {}
            by_subject[(fact.get("subject_id"), fact.get("predicate"))] = claim

        wanted = set()
        for shot in shots:
            for obs in shot.get("observations") or []:
                if not self._in_profile(obs, request["profile"]):
                    continue
                wanted.add((obs.get("entity_id"), obs.get("predicate")))

        out = []
        for shot in shots:
            for obs in shot.get("observations") or []:
                if not self._in_profile(obs, request["profile"]):
                    continue
                key = (obs.get("entity_id"), obs.get("predicate"))
                claim = by_subject.get(key)
                if claim is None:
                    continue
                fact = claim["fact"]
                out.append({
                    "unit_id": shot["shot_id"],
                    "entity_id": fact["subject_id"],
                    "predicate": fact["predicate"],
                    "expected_value": _str_value(fact.get("value")),
                    "source_claim_ref": {"service_id": "S16",
                                         "id": claim["claim_id"],
                                         "revision": claim["revision"],
                                         "content_hash": claim.get("content_hash")},
                    "checkpoint": request["memory_checkpoint"]["commit_sequence"],
                    "derivation": "direct",
                    "claim": claim,
                })
        return out

    @staticmethod
    def _in_profile(obs: dict, profile: str) -> bool:
        if profile == "full":
            return True
        return (obs.get("pool") or "text_logic") == profile

    # ---- 判定 ------------------------------------------------------------

    def _judge(self, tenant_id: str, request: dict, shots: list[dict],
               expectations: list[dict], query: dict) -> list[dict]:
        expected_at = {(e["unit_id"], e["entity_id"], e["predicate"]): e
                       for e in expectations}
        candidates: list[dict] = []

        for shot in shots:
            if shot["missing"]:
                candidates.append(self._mk_defect(
                    request, shot_id=shot["shot_id"], category="MISSING_EVIDENCE",
                    subcategory="shot_unavailable", confidence=1.0,
                    expected={"shot_id": shot["shot_id"]},
                    observed={"resolved": False},
                    evidence_refs=[{"kind": "shot_lookup",
                                    "shot_id": shot["shot_id"]}]))
                continue
            for obs in shot.get("observations") or []:
                if not self._in_profile(obs, request["profile"]):
                    continue
                # 知识类观测先于期望匹配：它比的是 (knower, proposition) 这条轴，
                # 正史里没有 `(角色, held_by)` 这种槽位可比。
                if obs.get("category") in KNOWLEDGE_CATEGORIES:
                    knowledge = self._judge_knowledge(tenant_id, request, shot,
                                                      obs, query)
                    if knowledge is not None:
                        candidates.append(knowledge)
                    continue
                # `resolved_by_event` 归 `_structural_checks`：它的语义是"又被另一个
                # 事件回收了一次"，不是"槽位值不符"。硬凑一个 VISUAL_STATE_MISMATCH
                # 会把一条 blocking 伏笔缺陷报成低级别画面问题。
                if obs.get("predicate") == "resolved_by_event":
                    continue
                expectation = expected_at.get(
                    (shot["shot_id"], obs.get("entity_id"), obs.get("predicate")))
                if expectation is None:
                    continue
                defect = self._judge_one(tenant_id, request, shot, obs,
                                         expectation, query)
                if defect is not None:
                    candidates.append(defect)

        candidates.extend(self._structural_checks(request, shots, query))
        # §3 的判定只在一个地方做：所有候选都要过同一条管线，包括结构性的那两类。
        return [self._classify(tenant_id, request, c, query) for c in candidates]

    def _judge_one(self, tenant_id: str, request: dict, shot: dict, obs: dict,
                   expectation: dict, query: dict) -> dict | None:
        """单个观测对一条期望。返回原始候选缺陷（已带证据与来源 claim）。"""
        claim = expectation["claim"]
        expected_value = expectation["expected_value"]
        observed_value = _str_value(obs.get("value"))
        confidence = float(obs.get("confidence", 0.0))

        category = obs.get("category")
        if category is None:
            if observed_value == expected_value:
                return None
            category = self._category_for(claim, obs)

        if category != "FLASHBACK_MISFRAME" and observed_value == expected_value:
            return None

        return self._mk_defect(
            request, shot_id=shot["shot_id"], category=category,
            subcategory=obs.get("subcategory") or "value_mismatch",
            confidence=confidence,
            expected={"entity_id": expectation["entity_id"],
                      "predicate": expectation["predicate"],
                      "value": expected_value},
            observed={"entity_id": obs.get("entity_id"),
                      "predicate": obs.get("predicate"),
                      "value": observed_value},
            evidence_refs=list(obs.get("evidence_refs") or []) + [
                {"kind": "canon_claim", **expectation["source_claim_ref"]}],
            claim_refs=[expectation["source_claim_ref"]],
            source_claim=claim,
            entity_ids=[expectation["entity_id"]],
            shot=shot,
            suggestion_category=category,
        )

    @staticmethod
    def _category_for(claim: dict, obs: dict) -> str:
        """观测没指定类别时按 (kind, predicate, pool) 落类。"""
        pool = obs.get("pool") or "text_logic"
        predicate = claim["fact"]["predicate"]
        kind = claim["kind"]
        entity_type = claim.get("entity_type") or ""
        if pool == "visual":
            if entity_type == "prop" and predicate in ("appearance",):
                return "PROP_VISUAL_MISMATCH"
            if predicate == "wears":
                return "COSTUME_SWITCH"
            if predicate == "injury":
                return "INJURY_REGRESSION"
            return "VISUAL_STATE_MISMATCH"
        if predicate == "held_by":
            return "HOLDER_MISMATCH"
        if predicate == "injury":
            return "INJURY_REGRESSION"
        if kind == "world_rule":
            return "WORLD_RULE_BREACH"
        return "VISUAL_STATE_MISMATCH"

    def _judge_knowledge(self, tenant_id: str, request: dict, shot: dict,
                         obs: dict, query: dict) -> dict | None:
        """§3.1。**S17 不猜意图，它要求意图被显式标注。**

        被比较的是 `(knower, proposition)`：`knower` 是观测里做动作的那个角色，
        `proposition` 是观测自己声明的命题（`proposition_claim_id`）。

        两个方向：
        - 角色表现出知道某命题，正史说不知道 → `KNOWLEDGE_VIOLATION`。
        - 正史说知道，台词却表现出不知道 → 撒谎。标了 `deception` 就 suppress；
          没标就 `UNMARKED_KNOWLEDGE_CONTRADICTION` —— 标注缺失本身就是缺陷。

        观测没给 `proposition_claim_id` 时**不猜**：这条观测证不了任何东西，
        落 `MISSING_EVIDENCE`（结构性不可判定），而不是按别人的槽位硬凑一个命题。
        """
        knower = obs.get("entity_id")
        proposition = obs.get("proposition_claim_id")
        belief = obs.get("belief") or obs.get("value")
        confidence = float(obs.get("confidence", 0.0))

        probe = self._character_knows(tenant_id, request, query, knower,
                                      proposition) if proposition else None
        if probe is None:
            return self._mk_defect(
                request, shot_id=shot["shot_id"], category="MISSING_EVIDENCE",
                subcategory="proposition_undeclared", confidence=confidence,
                expected={"knower_id": knower,
                          "proposition_claim_id": "<required>"},
                observed={"entity_id": knower, "belief": belief,
                          "proposition_claim_id": proposition},
                evidence_refs=list(obs.get("evidence_refs") or []),
                shot=shot, entity_ids=[knower] if knower else [],
                suggestion_category="MISSING_EVIDENCE")

        knows, claim_ref, claim = probe
        expects_know = knows is True
        believes_now = belief in ("knows", "believes")
        if believes_now == expects_know:
            return None

        if expects_know and belief == "unaware":
            # 撒谎台词。标注为 deception → suppress；没标注 → 缺陷。
            category = ("KNOWLEDGE_VIOLATION"
                        if shot.get("dialogue_intent") == "deception"
                        else "UNMARKED_KNOWLEDGE_CONTRADICTION")
        else:
            category = "KNOWLEDGE_VIOLATION"

        return self._mk_defect(
            request, shot_id=shot["shot_id"], category=category,
            subcategory=obs.get("subcategory") or "knowledge_mismatch",
            confidence=confidence,
            expected={"proposition": proposition, "knows": expects_know},
            observed={"belief": belief},
            evidence_refs=list(obs.get("evidence_refs") or []) + [
                {"kind": "canon_claim", **claim_ref}],
            claim_refs=[claim_ref], source_claim=claim, shot=shot,
            entity_ids=[knower] if knower else [],
            suggestion_category=category)

    def _character_knows(self, tenant_id: str, request: dict, query: dict,
                         knower: str, proposition: str) -> tuple[bool, dict, dict] | None:
        """这个角色此刻是否知道这条命题。**用 S16 自己的闭世界规则回答。**

        不读 S16 的 `knowledge_state` 表，也不复刻它的可见性公式（那就是第二权威）。
        问的是"以这个角色为视角，这条 claim 可见吗" —— 可见即可知。

        **故意不传 `audience_as_of_episode_order`**：那是观众第几集看到，与角色是否
        知道是两件事（§3 第 3 步，E09-5）。传了就会把"观众还没看到但角色已经知道"
        判成角色不知道，正好是这条规则要防的错。

        返回 `(knows, claim_ref, claim)`；命题在本 checkpoint 根本不存在时返回
        `None` —— 那不是一个"不知道"，是没有可判定的对象。

        「存在」用已经拿到的 omniscient 结果判，只对可见性再问一次：一次检查里
        对 S16 的调用次数不随观测数翻倍。
        """
        target = next((c for c in query.get("claims") or []
                       if c.get("claim_id") == proposition), None)
        if target is None:
            return None
        visible = self._memory_query(tenant_id, request["series_id"], {
            "branch_id": request["memory_checkpoint"]["branch_id"],
            "minimum_checkpoint": request["memory_checkpoint"]["commit_sequence"],
            "as_of_commit_sequence":
                request["memory_checkpoint"]["commit_sequence"],
            "perspective": {"mode": "character", "character_ids": [knower]},
        })
        known = {c.get("claim_id") for c in visible.get("claims") or []}
        ref = {"service_id": "S16", "id": target["claim_id"],
               "revision": target["revision"],
               "content_hash": target.get("content_hash")}
        return proposition in known, ref, target

    def _structural_checks(self, request: dict, shots: list[dict],
                           query: dict) -> list[dict]:
        """不靠值比对、只看结构就能断言的两类。

        `THREAD_DOUBLE_PAYOFF`：同一伏笔被两个不同事件回收。

        **比的是"剧本声称的回收事件" vs "正史的回收事件"**，不是正史内部的两条
        claim。S16 的 `claim` 表有 `UNIQUE (kind, subject_id, predicate)`，一个伏笔
        只有**一条** `resolved_by_event` 谱系；旧 revision 会被标记 superseded 并从
        `canon_effective_claim` 落选，`include_superseded` 也扩不到它（join 打的是
        effective 表）。所以"正史里躺两条互相矛盾的回收"这种形态在 S16 结构上
        就不存在 —— 拿它当判据，这条检查永远不可能触发。

        §2.1 给这条的观测来源写的是「剧本事件」，正史那条是期望。于是能判的正是：
        本次检查的剧本又在另一个事件上回收了已 resolved 的伏笔。

        §2.3 的 confidence 规则是给模型输出用的；这里是确定性推导，取 1.0。
        """
        defects = []
        canon: dict[str, dict] = {}
        for claim in query.get("claims") or []:
            fact = claim.get("fact") or {}
            if fact.get("predicate") == "resolved_by_event":
                canon[fact.get("subject_id")] = claim

        seen: dict[tuple[str, str], list[dict]] = {}
        for shot in shots:
            for obs in shot.get("observations") or []:
                if obs.get("predicate") != "resolved_by_event":
                    continue
                if not self._in_profile(obs, request["profile"]):
                    continue
                seen.setdefault((obs.get("entity_id"),
                                 _str_value(obs.get("value"))), []).append(
                    {"shot": shot, "obs": obs})

        for (subject, event), hits in sorted(seen.items(), key=lambda kv: str(kv[0])):
            claim = canon.get(subject)
            if claim is None:
                continue
            canon_event = _str_value((claim.get("fact") or {}).get("value"))
            if canon_event == event:
                continue
            # §5.3：script 类 blocking 缺陷必须带 span。来源是观测自带的剧本出处，
            # 键名必须是 `span`，否则 `_decide_raise` 认不出它、会降成 needs_human。
            spans = [{"kind": "script_span", "span": ref["span"],
                      "shot_id": hit["shot"]["shot_id"],
                      "ref": ref.get("ref")}
                     for hit in hits for ref in (hit["obs"].get("evidence_refs") or [])
                     if ref.get("span")]
            claim_ref = {"service_id": "S16", "id": claim["claim_id"],
                         "revision": claim.get("revision"),
                         "content_hash": claim.get("content_hash")}
            defects.append(self._mk_defect(
                request, shot_id=hits[0]["shot"]["shot_id"],
                category="THREAD_DOUBLE_PAYOFF",
                subcategory="second_resolution", confidence=1.0,
                expected={"entity_id": subject,
                          "resolved_by_event": canon_event},
                observed={"entity_id": subject, "resolved_by_event": event},
                evidence_refs=spans + [{"kind": "canon_claim", **claim_ref}],
                claim_refs=[claim_ref],
                source_claim=claim, entity_ids=[subject], shot=hits[0]["shot"],
                suggestion_category="THREAD_DOUBLE_PAYOFF"))
        defects.extend(self._axis_breaches(request, shots))
        return defects

    def _axis_breaches(self, request: dict, shots: list[dict]) -> list[dict]:
        """越轴：S17 自算（§2.2 `SCREEN_DIRECTION_BREACH` 的期望来源写的就是本服务）。

        只看**相邻且同场景**的两个镜头：跨场景换轴是正常的剪辑手法。
        """
        out = []
        for prev, cur in zip(shots, shots[1:]):
            if prev.get("missing") or cur.get("missing"):
                continue
            if prev.get("scene_id") != cur.get("scene_id"):
                continue
            a = (prev.get("camera") or {}).get("axis_degrees")
            b = (cur.get("camera") or {}).get("axis_degrees")
            if a is None or b is None:
                continue
            delta = abs(((b - a + 180) % 360) - 180)
            if delta < 150:
                continue
            out.append(self._mk_defect(
                request, shot_id=cur["shot_id"],
                category="SCREEN_DIRECTION_BREACH",
                subcategory="axis_crossing", confidence=1.0,
                expected={"axis_degrees": a},
                observed={"axis_degrees": b},
                evidence_refs=[{"kind": "camera_axis",
                                "from_shot": prev["shot_id"],
                                "to_shot": cur["shot_id"]}],
                shot=cur, suggestion_category="SCREEN_DIRECTION_BREACH"))
        return out

    # ---- 缺陷构造与 §3 判定 ----------------------------------------------

    def _mk_defect(self, request: dict, *, shot_id, category, subcategory,
                   confidence, expected, observed, evidence_refs, claim_refs=(),
                   source_claim=None, entity_ids=(), shot=None,
                   suggestion_category=None) -> dict:
        key = "|".join([category, shot_id or "",
                        (entity_ids[0] if entity_ids else ""),
                        (expected or {}).get("predicate", "")])
        return {
            "defect_id": "dft_" + _sha256_hex(key)[:24],
            "shot_id": shot_id,
            "scene_id": (shot or {}).get("scene_id"),
            "category": category,
            "subcategory": subcategory,
            "confidence": max(0.0, min(1.0, confidence)),
            "expected": expected,
            "observed": observed,
            "evidence_refs": evidence_refs,
            "entity_ids": list(entity_ids),
            "claim_refs": list(claim_refs),
            "source_claim": source_claim,
            "shot": shot,
            "suggestion_category": suggestion_category or category,
            "_raw": False,
        }

    def _classify(self, tenant_id: str, request: dict, defect: dict,
                  query: dict) -> dict:
        """§3 的判定顺序，写死在这个函数体的顺序里。

        1 叙事框架 → 2 主观视角/欺骗 → 4 世界规则 → 5 显式豁免 → 6 报。
        （第 3 步"观众先知不算缺陷"是负约束，落在 `_belief_of` 的注释与用例里。）
        """
        shot = defect.get("shot") or {}
        frame = shot.get("diegetic_frame")

        # --- 1. 叙事框架 ---------------------------------------------------
        if frame in FRAME_VALUES:
            if not shot.get("frame_declared", False):
                # 有框架但剧本没标 → 标注缺失算缺陷（E09-2）。
                return self._decide(defect, "raise", "medium", None,
                                    category="FLASHBACK_MISFRAME")
            return self._decide(defect, "suppress", None,
                                "DREAM_OR_FLASHBACK_FRAME")

        # --- 2. 主观镜头 / 欺骗 --------------------------------------------
        if shot.get("pov_of") and shot.get("dialogue_intent") == "misleading":
            return self._decide(defect, "suppress", None,
                                "SUBJECTIVE_MISREPRESENTATION")
        if defect["category"] == "KNOWLEDGE_VIOLATION" \
                and shot.get("dialogue_intent") == "deception":
            return self._decide(defect, "suppress", None,
                                "CHARACTER_DECEPTION")

        # --- 4. 世界规则许可 ------------------------------------------------
        if self._world_rule_allows(request, defect, query):
            return self._decide(defect, "suppress", None, "WORLD_RULE_ALLOWS")

        # --- 5. 显式豁免 ----------------------------------------------------
        exemption = self._match_exemption(tenant_id, request, defect)
        if exemption is not None:
            return self._decide(defect, "suppress", None,
                                f"EXEMPTION_{exemption['exemption_id']}",
                                exemption_id=exemption["exemption_id"])

        # --- 6. 报 ----------------------------------------------------------
        return self._decide_raise(defect)

    @staticmethod
    def _decide(defect: dict, disposition: str, severity, reason,
                category: str | None = None,
                exemption_id: str | None = None) -> dict:
        out = dict(defect)
        out["disposition"] = disposition
        out["severity"] = severity
        out["suppression_reason"] = reason
        out["exemption_id"] = exemption_id
        if category:
            out["category"] = category
        return out

    def _decide_raise(self, defect: dict) -> dict:
        """§2.3 固定规则表。低于 0.55 一律 abstain，不降级成 low。"""
        category = defect["category"]
        confidence = defect["confidence"]

        if confidence < ABSTAIN_THRESHOLD:
            return self._decide(defect, "abstain", None, None)

        if category in NEEDS_HUMAN_CATEGORIES:
            return self._decide(defect, "needs_human", "medium", None)

        floor, severity = RAISE_RULES.get(category, DEFAULT_RULE)
        if confidence >= floor:
            out = self._decide(defect, "raise", severity, None)
        else:
            # 「其余 raise 条件 → raise, low」：够过 0.55 但不够本类门槛。
            out = self._decide(defect, "raise", "low", None)

        # §5.3：剧本类阻塞缺陷必须带 source span，否则 S39 定位不到。
        if out["severity"] == "blocking" and self._fault(out)[0] == "script":
            if not any("span" in ref for ref in out["evidence_refs"]):
                out = self._decide(out, "needs_human", "medium", None)
                out["subcategory"] = "missing_source_span"
        return out

    @staticmethod
    def _fault(defect: dict) -> tuple[str, str | None, str]:
        return FAULT_OWNER.get(defect["category"],
                               ("unknown", None, "inspect_manually"))

    def _world_rule_allows(self, request: dict, defect: dict,
                           query: dict) -> bool:
        """§3 第 4 步：观测**援引了哪条规则、并断言落在例外内**，才算合法。

        判据不是"这个主体上有一条规则" —— 那样任何一条规则（例如"林岚会飞"）都会
        把同一角色上的伤势回退压成 pass。探针复现过两个方向：无关缺陷被误压，
        以及 `WORLD_RULE_BREACH` 压掉自己（`§2.3` 的 blocking 行成了死码）。

        `rule_statement` 是自由文本，S17 **不解析它**（解析自由文本等于让模型裁定
        合法性，§10 反模式 5）。合法性链是：抽取侧按 §7.3 只回答封闭问题 ——
        「这个动作是否在规则 R 的例外内」— 并把结论作为
        `{"kind": "world_rule_ref", "id": R, "within_exception": true}` 交上来。
        S17 只做确定性的事：R 在本次 checkpoint 可见、R 是 `world_rule`、
        例外断言是 `True`。任一不成立就不压制。

        **不给类别白名单**：同一个 `WORLD_RULE_BREACH` 既可能是合法特效（断言的
        `True`），也可能是真越界（断言 `False`）。类别分不开这两种，声明能。
        """
        rules = {c["claim_id"] for c in query.get("claims") or []
                 if c.get("kind") == "world_rule"}
        return any(ref.get("id") in rules
                   and ref.get("within_exception") is True
                   for ref in defect.get("evidence_refs") or []
                   if ref.get("kind") == "world_rule_ref")

    def _match_exemption(self, tenant_id: str, request: dict,
                         defect: dict) -> dict | None:
        """§1.5：作用域、理由、批准人、有效期。过期即失效，正常报缺陷。"""
        now = self._clock()
        rows = self.conn.execute(
            "SELECT * FROM exemption WHERE tenant_id=? AND series_id=?",
            (tenant_id, request["series_id"])).fetchall()
        for row in rows:
            if row["category"] != defect["category"]:
                continue
            if row["expires_at"] is not None and row["expires_at"] <= now:
                continue
            scope = json.loads(row["match_scope"])
            if not self._scope_matches(scope, request, defect):
                continue
            return dict(row)
        return None

    @staticmethod
    def _scope_matches(scope: dict, request: dict, defect: dict) -> bool:
        """所有写出来的键都要匹配；没写的键不约束。

        §1.5 明确拒绝"全局关闭某类检查" —— 所以 `match_scope` 不允许为空。
        """
        if not scope:
            return False
        checks = {
            "shot_ids": lambda v: defect.get("shot_id") in v,
            "scene_ids": lambda v: defect.get("scene_id") in v,
            "episode_ids": lambda v: request["episode_id"] in v,
            "entity_ids": lambda v: bool(set(defect.get("entity_ids") or []) & set(v)),
        }
        for key, test in checks.items():
            if key in scope and not test(scope[key]):
                return False
        return True

    # ---- 覆盖率与 verdict ------------------------------------------------

    def _coverage(self, shots: list[dict], request: dict) -> tuple[float, dict]:
        """§7.1：`Σ(已检查镜头时长) / 总时长`。

        "已检查" = 镜头读得到**且里面有本次 profile 要看的观测**。两个条件缺一不可：

        - 读不到镜头 → 没检查，记 `MISSING_EVIDENCE`；
        - 读到了但没有任何观测 → 也等于没检查内容，不能算覆盖。

        不能用 `expected_state` 的行数当分子：知识类观测比的是 `(knower, proposition)`，
        本来就不产出期望行，拿期望数当分子会把"检查过了"误报成"没检查"。
        """
        profile = request["profile"]
        examined, missing = [], []
        for shot in shots:
            if shot["missing"]:
                missing.append(shot["shot_id"])
                continue
            if any(self._in_profile(obs, profile)
                   for obs in shot.get("observations") or []):
                examined.append(shot)

        total = sum(int(s.get("duration_ms") or 0) for s in shots)
        if total <= 0:
            # 没给时长就没法按时长加权 —— 用镜头数当等权分母，仍是"检查了多少"。
            ratio = (len(examined) / len(shots)) if shots else 0.0
        else:
            ratio = sum(int(s.get("duration_ms") or 0) for s in examined) / total

        unchecked = sorted(s["shot_id"] for s in shots
                           if s not in examined and not s["missing"])
        return ratio, {
            "shot_ids": sorted(s["shot_id"] for s in examined),
            "shots_requested": len(shots),
            "unexamined_shot_ids": unchecked,
            "missing_shot_ids": sorted(missing),
        }

    @staticmethod
    def _verdict(raised: list[dict], abstained: list[dict],
                 coverage: float) -> str:
        # §7.1：`COVERAGE_GAP` 记的是"没看够"，不是内容有问题。它本身要留在
        # 报告里，但把它当内容缺陷就会让一次 coverage 不足升格成 `defects`，
        # 与 §7.1 的「覆盖不足最高只能到 pass_with_warnings」冲突。
        content = [d for d in raised if d["category"] != "COVERAGE_GAP"]
        if any(d["severity"] in ("medium", "high", "blocking") for d in content):
            return "defects"
        if raised or abstained:
            return "pass_with_warnings"
        if coverage < COVERAGE_FLOOR:
            # §7.1：没覆盖够就没有资格说 pass。
            return "pass_with_warnings"
        return "pass"

    def _coverage_gap_defect(self, request: dict, coverage: float) -> dict:
        defect = self._mk_defect(
            request, shot_id=None, category="COVERAGE_GAP",
            subcategory="coverage_below_floor", confidence=1.0,
            expected={"coverage_ratio_min": COVERAGE_FLOOR},
            observed={"coverage_ratio": round(coverage, 6)},
            evidence_refs=[{"kind": "scope",
                            "shot_ids": request["scope"]["shot_ids"]}],
            shot=None, suggestion_category="COVERAGE_GAP")
        return self._decide(defect, "raise", "medium", None)

    def _coverage_gap_from_pack(self, request: dict,
                                expectations: list[dict]) -> list[dict]:
        """§4.3：pack 声称的 `prohibited_assumptions` 没被本次检查覆盖 → 报 gap。"""
        covered = {(e["entity_id"], e["predicate"]) for e in expectations}
        out = []
        for item in (request["context_pack"] or {}).get(
                "prohibited_assumptions") or []:
            key = (item.get("entity_id"), item.get("predicate"))
            if key in covered:
                continue
            defect = self._mk_defect(
                request, shot_id=None, category="COVERAGE_GAP",
                subcategory="prohibited_assumption_uncovered", confidence=1.0,
                expected={"entity_id": key[0], "predicate": key[1],
                          "covered": True},
                observed={"entity_id": key[0], "predicate": key[1],
                          "covered": False},
                evidence_refs=[{"kind": "context_pack",
                                "ref": request.get("context_pack_ref")}],
                entity_ids=[key[0]] if key[0] else [],
                shot=None, suggestion_category="COVERAGE_GAP")
            out.append(self._decide(defect, "raise", "medium", None))
        return out

    # ---- 落库 ------------------------------------------------------------

    def _insert_report(self, tenant_id: str, request: dict, *, verdict,
                       severity_max, defect_count, abstain_count, coverage_ratio,
                       covered_scope, abstain_reason=None) -> str:
        report_id = "rep_" + uuid.uuid4().hex[:24]
        previous = self.conn.execute(
            "SELECT report_id, source_revision FROM continuity_report"
            " WHERE tenant_id=? AND series_id=? AND branch_id=? AND episode_id=?"
            " ORDER BY source_revision DESC LIMIT 1",
            (tenant_id, request["series_id"],
             request["memory_checkpoint"]["branch_id"],
             request["episode_id"])).fetchone()
        expected_revision = request.get("expected_report_revision")
        if expected_revision is not None and (
                previous is None and expected_revision != 0
                or previous is not None and previous["source_revision"] != expected_revision):
            raise ServiceError(
                409, "REPORT_REVISION_CONFLICT",
                "expected_report_revision does not match the current report",
                details={"current": None if previous is None
                         else previous["source_revision"]})
        revision = 1 if previous is None else previous["source_revision"] + 1
        self.conn.execute(
            "INSERT INTO continuity_report (tenant_id, report_id, check_id, series_id,"
            " branch_id, episode_id, manifest_ref, memory_checkpoint, verdict,"
            " severity_max, defect_count, abstain_count, covered_scope,"
            " coverage_ratio, source_revision, content_hash, created_at,"
            " supersedes_report_id, abstain_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, report_id, request["check_id"], request["series_id"],
             request["memory_checkpoint"]["branch_id"], request["episode_id"],
             _jcs(request["manifest_ref"]),
             _jcs(request["memory_checkpoint"]), verdict, severity_max,
             defect_count, abstain_count, _jcs(covered_scope), coverage_ratio,
             revision, _sha256_hex(report_id + verdict),
             self._clock(), previous["report_id"] if previous else None,
             abstain_reason))
        return report_id

    def _insert_expected(self, tenant_id: str, report_id: str,
                         expectation: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO expected_state (tenant_id, report_id, unit_id,"
            " entity_id, predicate, expected_value, source_claim_ref, checkpoint,"
            " derivation) VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, report_id, expectation["unit_id"],
             expectation["entity_id"], expectation["predicate"],
             expectation["expected_value"],
             _jcs(expectation["source_claim_ref"]), expectation["checkpoint"],
             expectation["derivation"]))

    def _insert_defect(self, tenant_id: str, report_id: str, d: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO defect (tenant_id, report_id, defect_id,"
            " category, subcategory, severity, confidence, disposition,"
            " suppression_reason, expected, observed, evidence_refs, entity_ids,"
            " claim_refs, shot_id, scene_id, timecode_start_ms, timecode_end_ms,"
            " fault_hypothesis, fault_owner_service, suggested_action)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, report_id, d["defect_id"], d["category"], d["subcategory"],
             d["severity"], d["confidence"], d["disposition"],
             d["suppression_reason"], _jcs(d["expected"]), _jcs(d["observed"]),
             _jcs(d["evidence_refs"]), _jcs(d["entity_ids"]),
             _jcs(d["claim_refs"]), d["shot_id"], d.get("scene_id"),
             (d.get("shot") or {}).get("timecode_start_ms"),
             (d.get("shot") or {}).get("timecode_end_ms"),
             *self._fault(d)[:2], self._fault(d)[2]))

    # ---- 发布 ------------------------------------------------------------

    def _publish(self, tenant_id: str, request: dict, report_id: str, verdict,
                 severity_max, defect_count, abstain_count, coverage_ratio,
                 raised: list[dict], reason: str | None) -> None:
        """§5.2。`defect_summary` 只带 S39 路由要的那几项，完整报告回查。"""
        summary = []
        for d in raised:
            summary.append({
                "defect_id": d["defect_id"],
                "category": d["category"],
                "severity": d["severity"] or "info",
                "shot_id": d["shot_id"] or "",
                "claim_ref": ({k: v for k, v in d["claim_refs"][0].items()
                               if k in ("id", "revision")}
                              if d["claim_refs"] else {"id": "none", "revision": 1}),
                "suggested_action": self._fault(d)[2],
                "fault_hypothesis": self._fault(d)[0],
                # CCT-3：阻塞缺陷的定位证据必须随事件走，不能只留在库里。
                "evidence_refs": [{"kind": ref.get("kind", "unknown")}
                                  for ref in d["evidence_refs"]],
                "fault_owner_service": self._fault(d)[1],
            })
        data = {
            "schema_version": "2.0",
            "check_id": request["check_id"],
            "report_id": report_id,
            "verdict": verdict,
            "severity_max": severity_max,
            "defect_count": defect_count,
            "abstain_count": abstain_count,
            "coverage_ratio": coverage_ratio,
            "memory_checkpoint": {
                "branch_id": request["memory_checkpoint"]["branch_id"],
                "commit_sequence": request["memory_checkpoint"]["commit_sequence"],
                "content_hash": request["memory_checkpoint"]["content_hash"],
            },
            "manifest_ref": request["manifest_ref"],
            "defect_summary": summary,
        }
        if reason:
            data["reason"] = reason
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_TYPE,
            subject=f"{request['series_id']}/"
                    f"{request['memory_checkpoint']['branch_id']}/"
                    f"{request['episode_id']}",
            tenant_id=tenant_id, home_cell=request["home_cell"],
            aggregate_id=f"{request['series_id']}:"
                         f"{request['memory_checkpoint']['branch_id']}:"
                         f"{request['episode_id']}",
            sequence=self._next_sequence(tenant_id, request),
            correlation_id=request["correlation_id"], data=data))

    def _next_sequence(self, tenant_id: str, request: dict) -> int:
        row = self.conn.execute(
            "SELECT MAX(source_revision) AS rev FROM continuity_report"
            " WHERE tenant_id=? AND series_id=? AND branch_id=? AND episode_id=?",
            (tenant_id, request["series_id"],
             request["memory_checkpoint"]["branch_id"],
             request["episode_id"])).fetchone()
        return int(row["rev"] or 0)

    @staticmethod
    def _render_report(row) -> dict:
        out = dict(row)
        out["manifest_ref"] = json.loads(out["manifest_ref"])
        out["memory_checkpoint"] = json.loads(out["memory_checkpoint"])
        out["covered_scope"] = json.loads(out["covered_scope"])
        return out

    @staticmethod
    def _render_defect(row) -> dict:
        out = dict(row)
        for key in ("expected", "observed", "evidence_refs", "entity_ids",
                    "claim_refs"):
            out[key] = json.loads(out[key])
        return out
