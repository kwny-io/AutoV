"""S25 内核。方案：`plans/S25-model-routing.md`。

一句话边界：**S25 算"如果现在派，会走哪条、大概多少钱"，不承诺任何东西。**

它没有余额列、没有 permit 列、不写任务（§1.2）。这是一个刻意的设计，因为
`RouteDecision` 一旦开始"记住"余额，它就变成 S23 的第二权威。下游（S26）
拿这条决策派单之前必须**实时**回查 S24 与 S23（§12）；决策只回答"当时
算出来是这样"。

三段结构，顺序不能换（§1.3）：

1. **硬过滤**（`filters.evaluate`）：19 条，第一个失败即唯一原因码。
   被拒的候选**不进评分**——不是"分数低排后面"。
2. **软评分**（`scoring.score_all`）：只对 eligible 打分，权重来自 S02 快照。
3. **落库 + 事件同事务**（`_persist`）：决策头、全部候选、`outbox` 行
   必须在同一个 `COMMIT`（§7.1）。

三个"读不到就不出决策"的分支（§9），它们看起来像过度保守，但每一条的
默认行为都会花错钱：S24 超时用缓存结论 = 拿过期能力派一次付费单；S02 读不到
就套默认策略 = 默认可能放宽了租户硬约束；S23 读不到就跳过预算过滤 =
明知可能超预算仍让候选进 `eligible[]`。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from . import degradation, filters, requirements, scoring
from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA

SOURCE = "S25"
SCHEMA_VERSION = "2.0"

EVENT_ROUTE_SELECTED = "model.route.selected"
TOPIC_ROUTE = "drama.model-routing.v2"

# §5.1：报价有效期 15 分钟。过期必须重新准入（§12 第 2 条）。
QUOTE_TTL_SECONDS = 15 * 60

# 计算上限：一次决策最多复核多少候选。超过说明上游没做过滤，硬拦比慢慢跑好。
MAX_CANDIDATES = 500

# `route_decision` 里 NOT NULL 的业务坐标。缺一个就无法回答"这条决策属于
# 哪个 run / 哪个节点"，也违背 AGENTS.md 的坐标分离——补默认值比报错更糟。
#
# `attempt_id` 不在这张表里：契约把它放在 `generation-command` 上，
# `execution-context.v2` 是 `additionalProperties:false` 且没有这个字段，
# 所以从 context 里读它等于要求调用方违约。两处分开校验，落库时合起来。
REQUIRED_CONTEXT = ("project_id", "series_id", "episode_id", "branch_id",
                    "node_id", "node_epoch", "execution_fence")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。不嵌套：一个命令一个事务。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------ 纯函数：内容哈希

def content_hash(payload: dict) -> str:
    """§8：决策输出的规范化 sha256，供 S26 引用校验（§12 第 1 条）。

    **只哈希决策本身的结论**，不含 `created_at` / `route_id`——那两个每次
    调用都不同，含进去会让"同样的输入给出同样的决策"这个事实无法核对。
    `quote_valid_to` 也不进：它是"这次算出来的有效期"，重算一次就变，
    把它冻进 content_hash 会让 S26 在有效期还没过时就判不一致。
    """
    projection = {
        "feasible": payload.get("feasible"),
        "infeasible_reason": payload.get("infeasible_reason"),
        "policy_snapshot_id": payload.get("policy_snapshot_id"),
        "capability_bundle_revision": payload.get("capability_bundle_revision"),
        "eligible": [
            {"rank": e["rank"], "candidate_key": e["candidate_key"],
             "snapshot_id": e["snapshot_id"],
             "expected_cost_micro": e["expected_cost_micro"],
             "quote_upper_bound_micro": e["quote_upper_bound_micro"],
             "normalized_input": e["normalized_input"]}
            for e in payload.get("eligible") or []
        ],
        "rejected": [
            {"candidate_key": r["candidate_key"], "code": r["code"]}
            for r in payload.get("rejected") or []
        ],
    }
    return _sha256_hex(_jcs(projection))


# ------------------------------------------------------------------ 服务

class RoutingService:
    def __init__(self, conn: sqlite3.Connection, *, capability_source,
                 validity_source, policy_source, budget_source,
                 telemetry_source, snapshot_source=None,
                 combination_source=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 隐式提交当前事务。只能在事务外建一次，之后复用同一实例，否则命令
        # 事务会在第一条事件写入时被悄悄提交，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        self.capability_source = capability_source
        self.validity_source = validity_source
        self.policy_source = policy_source
        self.budget_source = budget_source
        self.telemetry_source = telemetry_source
        # `mode_details` 与 `combination_key` 都不在候选体里，必须单独取。
        # 两者缺一个都会让 §3 的第 6、9–13 条过滤静默通过，所以默认值是
        # `None`（调用时 503），不是一个返回空 dict 的兜底。
        self.snapshot_source = snapshot_source
        self.combination_source = combination_source

    # ------------------------------------------------------------ 事务助手
    def _command(self, tenant_id: str, command_id: str, produce):
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    # ------------------------------------------------------------ 读上游
    def _upstream(self, call, code: str, *, empty_ok=False):
        """把"上游不可达"统一成 503，**绝不用缓存结论兜底**（§9）。

        `ConnectionError` 是 S33 起就有的上游不可达约定；这里再拦
        `ServiceError` 里 5xx 的那几种，让超时与"上游说了不行"分开。
        """
        try:
            result = call()
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(503, code, str(exc), retryable=True,
                                   details=exc.details) from exc
            raise
        if result is None and not empty_ok:
            raise ServiceError(503, code, "upstream returned no result",
                               retryable=True)
        return result

    def _resolve_policy(self, tenant_id: str, req: dict) -> dict:
        """§1.1：权重与开关由 S02 授权。读不到就 503，**不套默认策略**（§9）。"""
        policy = self._upstream(
            lambda: self.policy_source(tenant_id, req["policy_ref"]),
            "POLICY_UNAVAILABLE")
        if not isinstance(policy, dict) or "flags" not in policy:
            raise ServiceError(
                503, "POLICY_UNAVAILABLE",
                "policy snapshot is missing flags; S25 will not apply "
                "built-in defaults (a default may relax tenant hard constraints)")
        weights = policy.get("weights") or scoring.DEFAULT_WEIGHTS
        if not isinstance(weights, dict) or not weights:
            raise ServiceError(503, "POLICY_UNAVAILABLE",
                               "policy snapshot has no scoring weights")
        return policy

    def _budget(self, tenant_id: str, budget_id: str, currency_hint) -> dict:
        """§9：读不到余额 → 503，**不跳过 OVER_BUDGET**。"""
        if not budget_id:
            raise ServiceError(422, "INVALID_GENERATION_COMMAND",
                               "budget_scope_id is required")
        budget = self._upstream(
            lambda: self.budget_source(tenant_id, budget_id),
            "BUDGET_SOURCE_UNAVAILABLE")
        if not isinstance(budget, dict) or budget.get("available_micro") is None:
            raise ServiceError(
                503, "BUDGET_SOURCE_UNAVAILABLE",
                "budget availability has no available_micro; S25 will not "
                "skip the OVER_BUDGET filter")
        budget = dict(budget)
        budget["_read_at"] = _now()
        # 币种不一致时不能拿两个数直接比大小。这个分支宁可 503 也不猜汇率。
        if (currency_hint and budget.get("currency")
                and str(currency_hint).upper() != str(budget["currency"]).upper()):
            raise ServiceError(
                409, "CURRENCY_MISMATCH",
                f"budget is in {budget['currency']} but the quote is in "
                f"{currency_hint}; S25 does not convert currencies",
                details={"budget_currency": budget["currency"],
                         "quote_currency": currency_hint})
        return budget

    def _candidates(self, tenant_id: str, req: dict, flags: dict) -> dict:
        """S24 `GET /model-candidates`。空 candidates 是合法响应（§5.2）。

        `flags` 要传下去：策略要求的驻留标签与 `allow_byok` 是 S24 的预筛输入。
        不传的话 S25 会拿到一批注定被第 5、7 条拒掉的候选——结果不错，但
        "上游怎么筛的"这件事就没人能复算。
        """
        bundle = self._upstream(
            lambda: self.capability_source(tenant_id, req, flags),
            "CAPABILITY_SOURCE_UNAVAILABLE")
        if not isinstance(bundle, dict) or "candidates" not in bundle:
            raise ServiceError(
                503, "CAPABILITY_SOURCE_UNAVAILABLE",
                "capability source returned no candidate set")
        if len(bundle["candidates"]) > MAX_CANDIDATES:
            raise ServiceError(
                503, "CAPABILITY_SOURCE_UNAVAILABLE",
                f"{len(bundle['candidates'])} candidates exceeds the routing "
                f"limit of {MAX_CANDIDATES}; the registry is not filtering",
                retryable=True)
        return bundle

    def _mode_details(self, tenant_id: str, snapshot_id: str,
                      cache: dict) -> list:
        """§3 第 9–13 条的全部依据。取不到就是 503，**不是空列表**。

        把它降级成 `[]` 会让五条过滤静默通过，而它们正是"派了也用不了"里
        最常踩的那几条（参考形状、画幅、分辨率、时长、声音）。
        """
        if not self.snapshot_source:
            raise ServiceError(
                503, "CAPABILITY_SOURCE_UNAVAILABLE",
                "no snapshot source is configured; mode_details is required "
                "for filters 9-13 and cannot be defaulted")
        if snapshot_id in cache:
            return cache[snapshot_id]
        snapshot = self._upstream(
            lambda: self.snapshot_source(tenant_id, snapshot_id),
            "CAPABILITY_SOURCE_UNAVAILABLE")
        details = (snapshot or {}).get("mode_details")
        if not isinstance(details, list):
            raise ServiceError(
                503, "CAPABILITY_SOURCE_UNAVAILABLE",
                f"snapshot {snapshot_id} has no mode_details; filters 9-13 "
                f"cannot be evaluated")
        cache[snapshot_id] = details
        return details

    def _combination(self, tenant_id: str, command_id: str, candidate: dict,
                     req: dict) -> dict:
        """`POST /capability-checks` 换 `combination_key`（§12 第 3 条要用）。

        幂等键取 `command_id:snapshot_id`：必须是确定性的，否则同一条路由命令
        重放两次会拿到不同的 key，`content_hash` 跟着变（T25-22）。
        """
        if not self.combination_source:
            raise ServiceError(
                503, "CAPABILITY_SOURCE_UNAVAILABLE",
                "no combination source is configured; S25 cannot obtain the "
                "combination_key that S26 must verify before dispatch")
        key_id = f"{command_id}:{candidate['snapshot_id']}"
        result = self._upstream(
            lambda: self.combination_source(tenant_id, key_id,
                                            candidate["snapshot_id"], req),
            "CAPABILITY_SOURCE_UNAVAILABLE") or {}
        return {"combination_key": result.get("combination_key"),
                "combination_verdict": result.get("verdict"),
                "combination_unavailable": result.get("unavailable")}

    def _validity_batch(self, tenant_id: str, candidates: list[dict]) -> dict:
        """S24 `/capability-validity`：**这一条超时就是 503**（§9 第一行）。

        不用缓存的旧结论产生可付费决策——一次误判就是一次白花钱。
        """
        items = [{"snapshot_id": c["snapshot_id"],
                  "price_snapshot_id": c.get("price_snapshot_id"),
                  "mode": c.get("mode"),
                  "combination_key": c.get("combination_key")}
                 for c in candidates]
        if not items:
            return {}
        result = self._upstream(
            lambda: self.validity_source(tenant_id, items),
            "CAPABILITY_SOURCE_UNAVAILABLE")
        items_out = (result or {}).get("items") or []
        return {item.get("snapshot_id"): item for item in items_out}

    # ------------------------------------------------------------ 命令
    def route(self, tenant_id: str, command_id: str, body: dict,
              request_hash: str) -> dict:
        """`POST /route-decisions`。命令体是 `{generation_command, ...}`。"""
        generation_command = body.get("generation_command")
        try:
            req = requirements.derive(generation_command, tenant_id)
        except requirements.RequirementError as exc:
            raise ServiceError(422, exc.code, str(exc)) from exc

        context = generation_command["context"]
        if context.get("tenant_id") != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "context.tenant_id does not match the credential")
        missing = [k for k in REQUIRED_CONTEXT if context.get(k) is None]
        if not generation_command.get("attempt_id"):
            missing.append("attempt_id")
        if missing:
            # 缺坐标就不能落库——这两组坐标是"这条决策属于哪个 run/节点"的
            # 全部依据，补一个默认值会让版本语义失去意义（AGENTS.md）。
            raise ServiceError(
                422, "INVALID_GENERATION_COMMAND",
                f"missing {sorted(missing)}; these are the run and node "
                f"coordinates the decision is recorded against")
        # `attempt_id` 在命令上，不在 context 里（见 REQUIRED_CONTEXT 的注释）。
        # 校验之后并进来，下游只认一个 `context`。
        context = {**context, "attempt_id": str(generation_command["attempt_id"])}

        deadline_epoch = _parse_epoch(req.get("deadline"))
        now_epoch = _now_dt().timestamp()

        policy = self._resolve_policy(tenant_id, req)
        flags = policy.get("flags") or {}

        if deadline_epoch is not None and deadline_epoch <= now_epoch:
            # §9：deadline 已过直接 INFEASIBLE。**不读 S24/S23**——任何候选
            # 都来不及，读它们只是花钱换一个必然被拒的结论。
            return self._infeasible(
                tenant_id=tenant_id, command_id=command_id, req=req,
                context=context, code="DEADLINE_ALREADY_PASSED",
                summary="the deadline has already passed",
                rejected=[], policy=policy, bundle_revision=0,
                scoring_summary={}, degradation_options=[],
                request_hash=request_hash)

        if req.get("dialogue_duration_unknown"):
            # §2.2 / E04：对白时长未知是**决策结论**，不是请求错误。
            # 落一条 feasible=false 并照常发事件（T25-26）；返回 422 的话
            # 库里没行、事件也没有，S21 永远等不到结论。
            return self._infeasible(
                tenant_id=tenant_id, command_id=command_id, req=req,
                context=context, code="DIALOGUE_DURATION_UNKNOWN",
                summary="the command declares dialogue but no aligned "
                        "dialogue_duration_ms is available; S25 does not "
                        "estimate it",
                rejected=[], policy=policy, bundle_revision=0,
                scoring_summary={}, degradation_options=[],
                request_hash=request_hash)

        bundle = self._candidates(tenant_id, req, flags)
        candidates = bundle.get("candidates") or []
        # S24 已经排除的候选：S25 不重判、也不补造，原样带进 rejected。
        pre_excluded = [
            {"candidate_key": e.get("candidate_key"),
             "code": e.get("code") or "UNKNOWN",
             "detail": e.get("detail") if isinstance(e.get("detail"), dict)
                       else {"detail": e.get("detail")}}
            for e in (bundle.get("excluded") or [])]

        # 预算要在过滤前读：它是硬过滤的输入，读不到就整单 503。
        currency_hint = candidates[0].get("currency") if candidates else None
        budget = self._budget(tenant_id, req["budget_scope_id"], currency_hint)

        # 顺序见模块注释：mode_details → combination_key → validity。
        snapshot_cache: dict[str, list] = {}
        enriched: list[dict] = []
        for candidate in candidates:
            item = dict(candidate)
            item["mode_details"] = self._mode_details(
                tenant_id, candidate["snapshot_id"], snapshot_cache)
            item.update(self._combination(tenant_id, command_id, candidate, req))
            enriched.append(item)

        validity = self._validity_batch(tenant_id, enriched)
        telemetry = self._upstream(
            lambda: self.telemetry_source(tenant_id, req, enriched),
            "TELEMETRY_SOURCE_UNAVAILABLE", empty_ok=True) or {}

        base_facts = {
            "policy": policy,
            "budget": budget,
            # `generation-command.v2` 是 `additionalProperties:false` 且**没有**
            # `attempt_no`，所以只能从 `input`（契约里唯一的开放对象）读。
            # 与 `attempt_id` 一样：契约不改，调用方就没法表达"这是第几次"，
            # T25-13 的 `attempt_no=1` 条件就永远成立不了。缺省 1：首次尝试。
            "attempt_no": int(((generation_command or {}).get("input") or {})
                              .get("attempt_no") or 1),
            "now": _now(),
            "now_epoch": now_epoch,
            "deadline_epoch": deadline_epoch,
            "ingress_margin_s": int(flags.get("ingress_margin_s") or 0),
            "reference_ttl_safety_s": requirements.REFERENCE_TTL_SAFETY_SECONDS,
            "reference_url_ttl_insufficient_but_allowed": False,
        }

        eligible_raw: list[dict] = []
        rejected: list[dict] = list(pre_excluded)
        for candidate in enriched:
            per = dict(base_facts)
            per["validity"] = validity.get(candidate.get("snapshot_id"))
            per["mode_details"] = candidate.get("mode_details")
            per["telemetry"] = (telemetry.get(candidate.get("account_pool_id"))
                                or telemetry.get(candidate.get("candidate_key"))
                                or {})
            code, detail = filters.evaluate(req, candidate, per)
            if code is not None:
                rejected.append({"candidate_key": candidate["candidate_key"],
                                 "snapshot_id": candidate["snapshot_id"],
                                 "code": code, "detail": detail})
                continue
            p_first = scoring.smoothed_rate(
                per["telemetry"].get("first_pass_count"),
                per["telemetry"].get("sample_size"),
                scoring.P_FIRST_PASS_PRIOR)
            total, parts = scoring.expected_cost(
                candidate, modality=req["modality"],
                breakdown_costs={
                    "audio_strategy": (req.get("audio") or {}).get("strategy"),
                    "lipsync_micro": flags.get("lipsync_cost_micro"),
                    "qc_micro": flags.get("qc_cost_micro"),
                    "ingest_micro": flags.get("ingest_cost_micro")},
                p_first_pass=p_first)
            eligible_raw.append({
                "candidate_key": candidate["candidate_key"],
                "snapshot_id": candidate["snapshot_id"],
                "price_snapshot_id": candidate.get("price_snapshot_id"),
                "combination_key": candidate.get("combination_key"),
                "combination_verdict": (validity.get(candidate.get("snapshot_id"))
                                        or {}).get("combination")
                                       or candidate.get("combination_verdict"),
                "expected_cost_micro": total,
                "expected_cost_breakdown": parts,
                "quote_upper_bound_micro": candidate.get("quote_upper_bound_micro"),
                "currency": candidate.get("currency"),
                "normalized_input": filters.normalized_input(
                    req, filters.mode_detail_for(per, req.get("mode")), per),
                # S24 的候选体没有 `execution` 段（C24-5：url_ttl / idempotency
                # 这些字段全仓没有数据源）。这里只把**真有**的容量与地域搬
                # 过去，不编造 TTL 与回调语义——那些要等 S26 签 grant 时问
                # 供应商，填错就是重复扣费。
                "execution_profile": {
                    "provider": candidate.get("provider"),
                    "channel": candidate.get("channel"),
                    "model_snapshot": candidate.get("model_snapshot"),
                    "data_region": candidate.get("data_region"),
                    "max_inflight": candidate.get("max_inflight"),
                    "rpm": candidate.get("rpm"),
                },
                "quality_pass": per["telemetry"].get("first_pass_count"),
                "quality_n": per["telemetry"].get("sample_size"),
                "identity_pass": per["telemetry"].get("identity_pass_count"),
                "identity_n": per["telemetry"].get("identity_sample_size"),
                "motion_defect": per["telemetry"].get("motion_defect_count"),
                "motion_n": per["telemetry"].get("motion_sample_size"),
                "reliability": per["telemetry"].get("reliability", 0.5),
                "latency_p90_s": per["telemetry"].get("p90_latency_s"),
                "sample_size": per["telemetry"].get("sample_size") or 0,
                "watermark": per["telemetry"].get("watermark"),
                "degraded": candidate.get("lifecycle") == "degraded",
                "requires_refreshed_reference_urls": bool(
                    per.get("reference_url_ttl_insufficient_but_allowed")),
            })

        identity_required = "preserve_identity" in (
            req.get("required_capabilities") or [])
        scored, scoring_summary = scoring.score_all(
            eligible_raw, weights=policy.get("weights"),
            identity_required=identity_required)

        guard = _guard(req, policy)
        rejection_codes = sorted({r["code"].split(":")[0] for r in rejected})
        options = degradation.build(
            feasible=bool(scored), rejection_codes=rejection_codes,
            guard=guard, policy_flags=flags,
            degradation_level=int(body.get("degradation_level") or 0),
            identity_locked=bool(guard.get("identity_locked")),
            min_delivery_spec=guard.get("min_delivery_spec"),
            dialogue_exceeds_duration="DIALOGUE_EXCEEDS_DURATION" in rejection_codes)

        if not scored:
            return self._infeasible(
                tenant_id=tenant_id, command_id=command_id, req=req,
                context=context, code="NO_ELIGIBLE_CANDIDATE",
                summary=_summarize(rejected), rejected=rejected,
                policy=policy, bundle_revision=int(
                    bundle.get("capability_bundle_revision") or 0),
                scoring_summary=scoring_summary,
                degradation_options=options, dominant=_dominant(rejected),
                request_hash=request_hash,
                projection_lag=bool(bundle.get("projection_lag", False)))

        return self._persist(
            tenant_id=tenant_id, command_id=command_id, req=req,
            context=context, policy=policy, bundle=bundle,
            eligible=scored, rejected=rejected, scoring_summary=scoring_summary,
            degradation_options=options, feasible=True,
            infeasible_reason=None, request_hash=request_hash)

    # ------------------------------------------------------------ 落库
    def _infeasible(self, *, tenant_id, command_id, req, context, code,
                    summary, rejected, policy, bundle_revision,
                    scoring_summary, degradation_options, request_hash,
                    dominant=None, projection_lag=False):
        return self._persist(
            tenant_id=tenant_id, command_id=command_id, req=req,
            context=context, policy=policy, bundle={
                "capability_bundle_revision": bundle_revision,
                "projection_lag": projection_lag},
            eligible=[], rejected=rejected, scoring_summary=scoring_summary,
            degradation_options=degradation_options, feasible=False,
            infeasible_reason={"code": code, "summary": summary,
                               "dominant_cause": dominant},
            request_hash=request_hash)

    def _persist(self, *, tenant_id, command_id, req, context, policy, bundle,
                 eligible, rejected, scoring_summary, degradation_options,
                 feasible, infeasible_reason, request_hash) -> dict:
        route_id = _new_id("route")
        now = _now()
        quote_valid_to = (
            _now_dt().replace(microsecond=0).timestamp() + QUOTE_TTL_SECONDS)
        quote_valid_to = datetime.fromtimestamp(
            quote_valid_to, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        policy_snapshot_id = _policy_snapshot_key(policy)
        bundle_revision = int(bundle.get("capability_bundle_revision") or 0)
        projection_lag = bool(bundle.get("projection_lag", False))

        payload = {
            "route_id": route_id, "revision": 1, "created_at": now,
            "quote_valid_to": quote_valid_to, "feasible": feasible,
            "policy": {"policy_ref": req.get("policy_ref"),
                       "weights": scoring_summary.get("weights")
                                  or scoring.DEFAULT_WEIGHTS,
                       "allow_degraded_for_non_critical": bool(
                           (policy.get("flags") or {}).get(
                               "allow_degraded_for_non_critical")),
                       "allow_unverified_combination": bool(
                           (policy.get("flags") or {}).get(
                               "allow_unverified_combination"))},
            "policy_snapshot_id": policy_snapshot_id,
            "capability_bundle_revision": bundle_revision,
            "eligible": [_eligible_out(e) for e in eligible],
            "rejected": rejected,
            "degradation_options": degradation_options,
            "degradation_offer": (None if feasible
                                  else {"options": degradation_options}),
            "infeasible_reason": infeasible_reason,
        }
        digest = content_hash(payload)
        payload["content_hash"] = digest

        def _produce():
            self._save_policy_snapshot(tenant_id, req, policy, policy_snapshot_id)
            self.conn.execute(
                "INSERT INTO route_decision (route_id, revision, tenant_id,"
                " project_id, series_id, episode_id, branch_id, node_id,"
                " node_epoch, execution_fence, attempt_id, request_hash,"
                " command_id, policy_snapshot_id, capability_bundle_revision,"
                " feasible, requirement_set, scoring, quote_valid_to,"
                " content_hash, projection_lag, infeasible_reason, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (route_id, 1, tenant_id, context["project_id"],
                 context["series_id"], context["episode_id"],
                 context["branch_id"], context["node_id"],
                 int(context["node_epoch"]), int(context["execution_fence"]),
                 str(context["attempt_id"]), request_hash, command_id,
                 policy_snapshot_id,
                 bundle_revision, 1 if feasible else 0, _jcs(req),
                 _jcs(scoring_summary), quote_valid_to, digest,
                 1 if projection_lag else 0,
                 (infeasible_reason or {}).get("code"), now))
            for entry in eligible:
                self.conn.execute(
                    "INSERT INTO route_candidate (route_id, rank, candidate_key,"
                    " snapshot_id, price_snapshot_id, combination_key,"
                    " combination_verdict, decision, rejection_code,"
                    " rejection_detail, score, score_parts, sample_size,"
                    " watermark, expected_cost_micro, expected_cost_breakdown,"
                    " quote_upper_bound_micro, currency, normalized_input,"
                    " execution_profile) VALUES"
                    " (?,?,?,?,?,?,?,'eligible',NULL,NULL,?,?,?,?,?,?,?,?,?,?)",
                    (route_id, entry["rank"], entry["candidate_key"],
                     entry["snapshot_id"], entry.get("price_snapshot_id"),
                     entry.get("combination_key"),
                     entry.get("combination_verdict"), entry["score"],
                     _jcs(entry["score_parts"]), entry.get("sample_size"),
                     entry.get("watermark"), entry["expected_cost_micro"],
                     _jcs(entry["expected_cost_breakdown"]),
                     entry["quote_upper_bound_micro"], entry.get("currency"),
                     _jcs(entry["normalized_input"]),
                     _jcs(entry.get("execution_profile") or {})))
            for row in rejected:
                self.conn.execute(
                    "INSERT INTO route_candidate (route_id, rank, candidate_key,"
                    " snapshot_id, decision, rejection_code, rejection_detail)"
                    " VALUES (?,NULL,?,?,'rejected',?,?)",
                    (route_id, row["candidate_key"],
                     row.get("snapshot_id") or _snapshot_of(row), row["code"],
                     _jcs(row.get("detail") or {})))
            for option in degradation_options:
                self.conn.execute(
                    "INSERT INTO degradation_option (route_id, option_id, kind,"
                    " auto_executable, silent_degradation, blocked_reason,"
                    " cost_delta_micro, quality_risk, payload)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (route_id, option["option_id"], option["kind"],
                     1 if option["auto_executable"] else 0,
                     1 if option["silent_degradation"] else 0,
                     option.get("blocked_reason"),
                     option.get("cost_delta_micro"), option.get("quality_risk"),
                     _jcs(option.get("payload") or {})))
            # §7.1：事件与决策同事务。feasible=false **也要发**——
            # 否则 S21 会一直等一个不会到来的成功。
            self._emit(route_id=route_id, revision=1, tenant_id=tenant_id,
                       req=req, context=context, command_id=command_id,
                       payload=payload, digest=digest,
                       bundle_revision=bundle_revision,
                       projection_lag=projection_lag,
                       quote_valid_to=quote_valid_to,
                       eligible_count=len(eligible),
                       rejected_count=len(rejected),
                       degradation_options=degradation_options,
                       feasible=feasible, infeasible_reason=infeasible_reason,
                       policy_snapshot_id=policy_snapshot_id)

        self._command(tenant_id, command_id, _produce)
        return payload

    def _save_policy_snapshot(self, tenant_id: str, req: dict, policy: dict,
                              snapshot_id: str) -> None:
        """在**命令事务内**登记策略快照。

        原先这一步在事务外：`route_decision.policy_snapshot_id` 有外键指向
        这张表，事务外先写会在决策回滚后留下一行没人引用的快照，而
        `INSERT OR IGNORE` 让重试时那行仍是旧的 `effective_from`。放进来之后
        "这条决策引用的快照"与"快照本身"同生共死。
        """
        ref = req.get("policy_ref") or {}
        self.conn.execute(
            "INSERT OR IGNORE INTO routing_policy_snapshot (policy_snapshot_id,"
            " tenant_id, policy_ref_id, policy_ref_rev, policy_ref_hash,"
            " weights, thresholds, flags, effective_from)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (snapshot_id, tenant_id, str(ref.get("id")),
             int(ref.get("revision") or 0),
             str(ref.get("content_hash") or _policy_snapshot_hash(policy)),
             _jcs(policy.get("weights") or scoring.DEFAULT_WEIGHTS),
             _jcs(policy.get("thresholds") or {}),
             _jcs(policy.get("flags") or {}), _now()))

    def _emit(self, *, route_id, revision, tenant_id, req, context, command_id,
              payload, digest, bundle_revision, projection_lag, quote_valid_to,
              eligible_count, rejected_count, degradation_options, feasible,
              infeasible_reason, policy_snapshot_id) -> None:
        selected = None
        if feasible and payload["eligible"]:
            top = payload["eligible"][0]
            selected = {
                "candidate_key": top["candidate_key"],
                "snapshot_id": top["snapshot_id"],
                "combination_key": top.get("combination_key"),
                "normalized_input": top["normalized_input"],
                "expected_cost_micro": top["expected_cost_micro"],
                "quote_upper_bound_micro": top["quote_upper_bound_micro"],
                "currency": top.get("currency"),
            }
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_ROUTE_SELECTED,
            subject=f"route_decision/{route_id}",
            tenant_id=tenant_id,
            home_cell=str(context.get("home_cell") or "unknown"),
            aggregate_id=route_id, sequence=revision,
            correlation_id=str(context.get("correlation_id") or command_id),
            causation_id=command_id,
            data={
                "schema_version": SCHEMA_VERSION,
                "route_id": route_id, "revision": revision,
                "content_hash": digest,
                "project_id": context["project_id"],
                "series_id": context["series_id"],
                "episode_id": context["episode_id"],
                "branch_id": context["branch_id"],
                "node_id": context["node_id"],
                # 两个坐标分开透传，绝不合并不换算（AGENTS.md）。
                "node_epoch": int(context["node_epoch"]),
                "execution_fence": int(context["execution_fence"]),
                "attempt_id": str(context["attempt_id"]),
                "command_id": command_id,
                "feasible": feasible,
                "selected": selected,
                "infeasible_reason": (infeasible_reason or {}).get("code"),
                "eligible_count": eligible_count,
                "rejected_count": rejected_count,
                "degradation_offered": sorted({o["kind"]
                                               for o in degradation_options}),
                "policy_snapshot_id": policy_snapshot_id,
                "capability_bundle_revision": bundle_revision,
                "quote_valid_to": quote_valid_to,
                "projection_lag": bool(projection_lag),
            },
            # MRT-1：契约要 4 个键。3 键版本通不过本仓的事件校验。
            data_ref={"service_id": SOURCE, "id": route_id,
                      "revision": revision, "content_hash": digest},
            schema_version=SCHEMA_VERSION))

    # ------------------------------------------------------------ 查询
    def get_route(self, tenant_id: str, route_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM route_decision WHERE tenant_id=? AND route_id=?",
            (tenant_id, route_id)).fetchone())
        if row is None:
            # 跨租户 → 404（不确认存在）。
            raise ServiceError(404, "ROUTE_DECISION_NOT_FOUND",
                               f"no route decision {route_id}")
        candidates = [_candidate_out(r) for r in self.conn.execute(
            "SELECT * FROM route_candidate WHERE route_id=? ORDER BY"
            " CASE WHEN decision='eligible' THEN 0 ELSE 1 END, rank,"
            " candidate_key", (route_id,)).fetchall()]
        options = [_option_out(r) for r in self.conn.execute(
            "SELECT * FROM degradation_option WHERE route_id=? ORDER BY option_id",
            (route_id,)).fetchall()]
        policy = _row(self.conn.execute(
            "SELECT * FROM routing_policy_snapshot WHERE policy_snapshot_id=?",
            (row["policy_snapshot_id"],)).fetchone())
        return {
            "route": {
                "route_id": row["route_id"], "revision": row["revision"],
                "content_hash": row["content_hash"],
                "created_at": row["created_at"],
                "quote_valid_to": row["quote_valid_to"],
                "feasible": bool(row["feasible"]),
                "infeasible_reason": row["infeasible_reason"],
                "policy_snapshot_id": row["policy_snapshot_id"],
                "capability_bundle_revision": row["capability_bundle_revision"],
                "projection_lag": bool(row["projection_lag"]),
                "node_epoch": row["node_epoch"],
                "execution_fence": row["execution_fence"],
                "attempt_id": row["attempt_id"],
                "command_id": row["command_id"],
                "requirement_set": _loads(row["requirement_set"], {}),
                "scoring": _loads(row["scoring"], {}),
            },
            "policy": (None if policy is None else {
                "policy_ref_id": policy["policy_ref_id"],
                "policy_ref_revision": policy["policy_ref_rev"],
                "policy_ref_hash": policy["policy_ref_hash"],
                "weights": _loads(policy["weights"], {}),
                "thresholds": _loads(policy["thresholds"], {}),
                "flags": _loads(policy["flags"], {}),
            }),
            "eligible": [c for c in candidates if c["decision"] == "eligible"],
            "rejected": [c for c in candidates if c["decision"] == "rejected"],
            "degradation_options": options,
        }

    def list_routes(self, tenant_id: str, *, attempt_id=None, node_id=None,
                    limit: int = 50) -> dict:
        if not attempt_id and not node_id:
            raise ServiceError(422, "QUERY_SCOPE_REQUIRED",
                               "one of attempt_id or node_id is required")
        clause, params = [], [tenant_id]
        if attempt_id:
            clause.append("attempt_id=?")
            params.append(attempt_id)
        if node_id:
            clause.append("node_id=?")
            params.append(node_id)
        params.append(max(1, min(int(limit), 200)))
        rows = self.conn.execute(
            "SELECT route_id, revision, created_at, feasible, infeasible_reason,"
            " quote_valid_to, node_id, node_epoch, execution_fence, attempt_id"
            " FROM route_decision WHERE tenant_id=? AND " + " AND ".join(clause)
            + " ORDER BY created_at DESC LIMIT ?", params).fetchall()
        return {"items": [dict(r) for r in rows]}


# ------------------------------------------------------------ 纯函数：装配

def _parse_epoch(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _policy_snapshot_hash(policy: dict) -> str:
    return _sha256_hex(_jcs({
        "weights": policy.get("weights") or scoring.DEFAULT_WEIGHTS,
        "thresholds": policy.get("thresholds") or {},
        "flags": policy.get("flags") or {},
    }))


def _policy_snapshot_key(policy: dict) -> str:
    """内容寻址：同一份策略重复路由得到同一个 id。

    用内容哈希而不是自增 id，是因为"这次用的是哪一版"必须能从记录本身验证，
    而不是相信某次写入的顺序。
    """
    return ID_PREFIX["policy_snapshot"] + _policy_snapshot_hash(policy)[:20]


def _snapshot_of(row: dict) -> str:
    """被拒候选的 `snapshot_id` 列 NOT NULL。S24 的 excluded[] 不带它。

    这里**不猜**：填一个空串以外的占位会让"这条拒绝属于哪个快照"变成假事实。
    方案 §7 的 DDL 写的是 `snapshot_id text NOT NULL`，而真实上游不提供，
    所以填空串并在缺口里登记（MRT-14）。
    """
    return row.get("snapshot_id") or ""


def _guard(req: dict, policy: dict) -> dict:
    """§6.2 的显式清单。它决定"这个降级动了能不能动的"。"""
    flags = policy.get("flags") or {}
    capabilities = req.get("required_capabilities") or []
    audio = req.get("audio") or {}
    return {
        "identity_locked": "preserve_identity" in capabilities,
        "voice_lock": ({"voice_id": flags.get("voice_id"),
                        "revision": flags.get("voice_revision")}
                       if audio.get("needs_voice_lock") else None),
        "full_dialogue_required": bool(req.get("dialogue_duration_ms")),
        "dialogue_duration_ms": req.get("dialogue_duration_ms"),
        "permission_constraint": flags.get("permission_constraint"),
        "min_delivery_spec": flags.get("min_delivery_spec"),
    }


def _dominant(rejected: list[dict]) -> str | None:
    """§5.2：`dominant_cause`。按出现次数取最多的码；同数取字典序。

    确定性很重要：同一个输入两次路由必须给出同一个 `content_hash`
    （T25-22），而 `dominant_cause` 在里面。用 `Counter.most_common`
    在并列时顺序取决于插入序，会随上游候选顺序抖动。
    """
    counts: dict[str, int] = {}
    for row in rejected:
        base = str(row["code"]).split(":")[0]
        counts[base] = counts.get(base, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _summarize(rejected: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in rejected:
        base = str(row["code"]).split(":")[0]
        counts[base] = counts.get(base, 0) + 1
    if not counts:
        return "no candidates were offered by the registry"
    parts = [f"{n} by {code}" for code, n in
             sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return f"{len(rejected)} candidates rejected: " + ", ".join(parts)


def _eligible_out(entry: dict) -> dict:
    return {
        "rank": entry["rank"],
        "candidate_key": entry["candidate_key"],
        "snapshot_id": entry["snapshot_id"],
        "price_snapshot_id": entry.get("price_snapshot_id"),
        "combination_key": entry.get("combination_key"),
        "combination_verdict": entry.get("combination_verdict"),
        "normalized_input": entry["normalized_input"],
        "score": entry["score"],
        "score_parts": entry["score_parts"],
        "sample_size": entry.get("sample_size"),
        "watermark": entry.get("watermark"),
        "expected_cost_micro": entry["expected_cost_micro"],
        "expected_cost_breakdown": entry["expected_cost_breakdown"],
        "quote_upper_bound_micro": entry["quote_upper_bound_micro"],
        "currency": entry.get("currency"),
        "execution": entry.get("execution_profile"),
        "requires_refreshed_reference_urls": bool(
            entry.get("requires_refreshed_reference_urls")),
    }


def _candidate_out(row) -> dict:
    return {
        "candidate_key": row["candidate_key"],
        "snapshot_id": row["snapshot_id"],
        "decision": row["decision"],
        "rank": row["rank"],
        "rejection_code": row["rejection_code"],
        "rejection_detail": _loads(row["rejection_detail"], None),
        "score": row["score"],
        "score_parts": _loads(row["score_parts"], None),
        "sample_size": row["sample_size"],
        "watermark": row["watermark"],
        "expected_cost_micro": row["expected_cost_micro"],
        "expected_cost_breakdown": _loads(row["expected_cost_breakdown"], None),
        "quote_upper_bound_micro": row["quote_upper_bound_micro"],
        "currency": row["currency"],
        "normalized_input": _loads(row["normalized_input"], None),
        "execution_profile": _loads(row["execution_profile"], None),
    }


def _option_out(row) -> dict:
    return {
        "option_id": row["option_id"], "kind": row["kind"],
        "auto_executable": bool(row["auto_executable"]),
        "silent_degradation": bool(row["silent_degradation"]),
        "blocked_reason": row["blocked_reason"],
        "cost_delta_micro": row["cost_delta_micro"],
        "quality_risk": row["quality_risk"],
        "payload": _loads(row["payload"], {}),
    }
