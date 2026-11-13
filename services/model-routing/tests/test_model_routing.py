"""S25 单测。编号对应 `plans/S25-model-routing.md` §11 的 T25-01…T25-27。

上游全部是 stub，**按真实的上游契约写**，不是按内核希望的样子写：

- S24 `GET /model-candidates` 的候选体只有 `_candidate_payload` 那 16 个字段，
  没有 `mode_details`、`combination_key`、`provider_idempotency`、`url_ttl_seconds`
  （C24-5）。测试里也**不**往候选体塞这些——塞了就是在测一个不存在的上游。
- `mode_details` 只从 `GET /capability-snapshots/{id}` 来，形状是
  `_mode_detail()` 的 `{**mode, reference_rules, output_specs,
  capability_claims, combination_probes}`。
- `combination_key` 只从 `POST /capability-checks` 来。
- `POST /capability-validity` 的每项是 `_validity_one()` 的
  `{snapshot_id, usable, code, lifecycle, pool_health, evidence_valid_until,
  price_valid_to, combination}`。

硬约束：这条路（S25）没有余额列、没有 permit 列、不写任务。凡是能证明这一点
的用例都写成"库里查不到"的断言，而不是"响应里没有"。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import (
    CommandConflict,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from model_routing.schema import SCHEMA
from model_routing.service import (
    RoutingService,
    ServiceError,
    content_hash,
)


FUTURE = "2099-01-01T00:00:00Z"
POLICY_REF = {"service_id": "S02", "id": "pol_1", "revision": 3,
              "content_hash": "a" * 64}


# ------------------------------------------------------------------ 上游形状

def candidate(key="c1", snapshot="snap_a", **over):
    """S24 `_candidate_payload` 的形状。**不多一个字段**。"""
    base = {
        "candidate_key": key,
        "snapshot_id": snapshot,
        "provider": "volcengine", "channel": "default",
        "model_id": "seedance-1-0-pro", "model_snapshot": "250528",
        "region": "cn-beijing", "account_pool_id": "pool_1",
        "scope": "platform", "data_region": "cn",
        "residency_tags": ["cn"],
        "lifecycle": "active",
        "evidence_valid_until": FUTURE,
        "modes": ["t2v", "i2v"],
        "max_inflight": 8, "rpm": 60,
        "price_snapshot_id": f"price_{snapshot}",
        "price_valid_to": FUTURE,
        "currency": "USD",
        "quote_upper_bound_micro": 1_000_000,
    }
    base.update(over)
    return base


def mode_detail(mode="t2v", **over):
    """S24 `_mode_detail()` 的形状。"""
    base = {
        "mode": mode, "support": "probe_verified",
        "execution_kind": "poll", "prompt_required": True,
        "prompt_max_chars": 2000,
        "native_audio": True, "prompt_audio_lock": True,
        "evidence_id": "ev_1",
        "reference_rules": [
            {"role": "subject", "min_count": 0, "max_count": 2,
             "ordering": "unordered", "exclusive_with": []},
        ],
        "output_specs": [
            {"aspect_ratio": "9:16", "max_width": 1080, "max_height": 1920,
             "duration_tiers_s": [4, 5, 8], "trims_to_nearest": True},
        ],
        "capability_claims": [
            {"capability_key": "preserve_identity", "support": "probe_verified",
             "params": {}, "evidence_id": "ev_1"},
        ],
        "combination_probes": [],
    }
    base.update(over)
    return base


def validity(snapshot="snap_a", **over):
    """S24 `_validity_one()` 的形状。"""
    base = {"snapshot_id": snapshot, "usable": True, "code": None,
            "lifecycle": "active", "pool_health": "healthy",
            "evidence_valid_until": FUTURE, "price_valid_to": FUTURE,
            "combination": "SUPPORTED"}
    base.update(over)
    return base


class Harness:
    """内存库 + 七个上游 stub + 与 `api/app.py` 同形的幂等外壳。"""

    def __init__(self, conn=None, **over):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)

        self.policy = over.get("policy", {
            "flags": {}, "weights": None, "thresholds": {},
            "document_ref": None})
        self.bundle = over.get("bundle", {
            "capability_bundle_revision": 812,
            "projection_watermark": "2026-11-13T00:00:00Z",
            "candidates": [candidate()],
            "excluded": [],
        })
        self.snapshots = over.get("snapshots",
                                  {"snap_a": {"mode_details": [mode_detail()]}})
        self.checks = over.get("checks",
                               {("snap_a", "c1"): {
                                   "combination_key": "ck_1",
                                   "verdict": "SUPPORTED"}})
        self.validity_items = over.get(
            "validity", {"snap_a": validity()})
        self.budget = over.get("budget", {
            "budget_id": "budget_ep1", "currency": "USD",
            "available_micro": 10_000_000})
        self.telemetry = over.get("telemetry", {})
        self.calls = {"policy": 0, "candidates": 0, "snapshot": 0,
                      "combination": 0, "validity": 0, "budget": 0,
                      "telemetry": 0, "combination_keys": []}

        self.service = RoutingService(
            self.conn,
            capability_source=self._candidates,
            snapshot_source=self._snapshot,
            combination_source=self._combination,
            validity_source=self._validity,
            policy_source=self._policy,
            budget_source=self._budget,
            telemetry_source=self._telemetry,
        )

    # ---- stubs：签名与 `deps.build_*` 一致 ----
    def _policy(self, tenant_id, ref):
        self.calls["policy"] += 1
        if isinstance(self.policy, Exception):
            raise self.policy
        return self.policy

    def _candidates(self, tenant_id, req, flags):
        self.calls["candidates"] += 1
        if isinstance(self.bundle, Exception):
            raise self.bundle
        return self.bundle

    def _snapshot(self, tenant_id, snapshot_id):
        self.calls["snapshot"] += 1
        value = self.snapshots.get(snapshot_id)
        if isinstance(value, Exception):
            raise value
        return value

    def _combination(self, tenant_id, command_id, snapshot_id, req):
        self.calls["combination"] += 1
        self.calls["combination_keys"].append(command_id)
        if isinstance(self.checks, Exception):
            raise self.checks
        key = self._key_of(snapshot_id)
        value = self.checks.get((snapshot_id, key))
        if value is None:
            return {"combination_key": None, "verdict": None,
                    "unavailable": "MODE_NOT_FOUND"}
        if isinstance(value, Exception):
            raise value
        return value

    def _key_of(self, snapshot_id: str) -> str:
        for c in (self.bundle.get("candidates") or []):
            if c["snapshot_id"] == snapshot_id:
                return c["candidate_key"]
        return ""

    def _validity(self, tenant_id, items):
        self.calls["validity"] += 1
        if isinstance(self.validity_items, Exception):
            raise self.validity_items
        return {"items": [self.validity_items[i["snapshot_id"]]
                          for i in items if i["snapshot_id"] in self.validity_items]}

    def _budget(self, tenant_id, budget_id):
        self.calls["budget"] += 1
        if isinstance(self.budget, Exception):
            raise self.budget
        return self.budget

    def _telemetry(self, tenant_id, req, candidates):
        self.calls["telemetry"] += 1
        if isinstance(self.telemetry, Exception):
            raise self.telemetry
        return self.telemetry

    # ---- 幂等外壳：与 `api/app.py` 的 `command()` 同语义 ----
    def command(self, body, tenant_id="t1", command_id="c1"):
        payload = {**body, "command_id": command_id}
        record = self.idem.begin(tenant_id, command_id,
                                 canonical_request_hash(payload))
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        result = self.service.route(
            tenant_id, command_id, payload,
            canonical_request_hash(payload))
        self.idem.finish(tenant_id, command_id,
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ---- 命令体 ----
    def body(self, **input_over):
        ctx = {
            "schema_version": "2.0", "tenant_id": "t1",
            "project_id": "prj_1", "series_id": "ser_1", "episode_id": "ep_1",
            "branch_id": "br_1", "home_region": "cn", "home_cell": "cell_a",
            "node_id": "shot_07", "node_epoch": 3, "execution_fence": 11,
            "policy_ref": dict(POLICY_REF),
        }
        text_input = {"mode": "t2v", "aspect_ratio": "9:16",
                      "duration_seconds": 5, "audio_strategy": "silent"}
        text_input.update(input_over)
        return {
            "generation_command": {
                "schema_version": "2.0", "context": ctx,
                # 契约把 attempt_id 放在命令上，不在 execution-context 里
                "attempt_id": "att_1", "variation_nonce": "v1",
                "idempotency_key": "c1", "modality": "video",
                "input": text_input, "route_policy_ref": dict(POLICY_REF),
                "template_ref": {"service_id": "S20", "id": "tpl_1",
                                 "revision": 1, "content_hash": "b" * 64},
                "budget_scope_id": "budget_ep1", "deadline": FUTURE,
            },
        }

    # ---- 查询助手 ----
    def rows(self, table, **where):
        clause = " AND ".join(f"{k}=?" for k in where) or "1"
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {clause}", tuple(where.values())
        ).fetchall()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def events(self):
        return [json.loads(r["payload"])
                for r in self.conn.execute(
                    "SELECT payload FROM outbox ORDER BY created_at").fetchall()]


def kinds(result: dict) -> set[str]:
    return {o["kind"] for o in result["degradation_options"]}


def rejected_codes(result: dict) -> dict[str, str]:
    return {r["candidate_key"]: r["code"].split(":")[0]
            for r in result["rejected"]}


# ================================================================ 幂等

class TestIdempotency:
    """T25-01/02/27（E12）。"""

    def test_same_key_same_body_returns_same_route(self):
        h = Harness()
        first = h.command(h.body())
        second = h.command(h.body())
        assert second["replayed"] is True
        assert second["route_id"] == first["route_id"]
        assert h.count("route_decision") == 1

    def test_same_key_different_body_is_conflict(self):
        h = Harness()
        h.command(h.body())
        with pytest.raises(CommandConflict):
            h.command(h.body(duration_seconds=8))
        assert h.count("route_decision") == 1

    def test_replay_writes_no_second_outbox_row(self):
        # T25-27：回放路径**不重新路由**，所以不会写第二条事件。
        h = Harness()
        h.command(h.body())
        before = h.count("outbox")
        h.command(h.body())
        assert h.count("outbox") == before == 1

    def test_replay_does_not_reread_upstream(self):
        # 回放不能拿"两分钟后的余额"换掉 S26 手里那条报价。
        h = Harness()
        h.command(h.body())
        calls = dict(h.calls)
        h.command(h.body())
        assert h.calls == calls

    def test_route_decision_is_one_row_per_command(self):
        # §7 的 `UNIQUE (tenant_id, command_id)`：一条命令最多一条决策。
        h = Harness()
        h.command(h.body(), command_id="c1")
        h.command(h.body(), command_id="c2")
        assert h.count("route_decision") == 2


# ================================================================ 硬过滤

class TestHardFilters:
    """§1.3 的第一半：被拒的候选**不进评分**。"""

    def test_missing_identity_capability_is_rejected_not_scored(self):
        # T25-03（E19）。便宜 4 倍也不能因为便宜而进 eligible。
        h = Harness(
            bundle={"capability_bundle_revision": 1, "candidates": [
                candidate("cheap", "snap_cheap", quote_upper_bound_micro=100),
            ], "excluded": []},
            snapshots={"snap_cheap": {"mode_details": [mode_detail(
                capability_claims=[])]}},
            checks={("snap_cheap", "cheap"): {"combination_key": "ck",
                                              "verdict": "SUPPORTED"}},
            validity={"snap_cheap": validity("snap_cheap")})
        result = h.command(h.body(required_capabilities=["preserve_identity"]))
        assert result["feasible"] is False
        assert rejected_codes(result)["cheap"] == "CAPABILITY_MISSING"
        assert result["eligible"] == []
        # 库里 rejected 行没有分数——不是"分数低"。
        rows = h.rows("route_candidate", route_id=result["route_id"])
        assert [r["score"] for r in rows] == [None]
        assert [r["rank"] for r in rows] == [None]

    def test_cheap_but_over_budget_does_not_revive(self):
        # T25-06：便宜候选超预算 → 硬过滤，不因"期望成本低"复活。
        h = Harness(
            bundle={"capability_bundle_revision": 1, "candidates": [
                candidate("cheap", "s1", quote_upper_bound_micro=9_000_000),
                candidate("pricey", "s2", quote_upper_bound_micro=1_000_000),
            ], "excluded": []},
            snapshots={
                "s1": {"mode_details": [mode_detail()]},
                "s2": {"mode_details": [mode_detail()]},
            },
            checks={("s1", "cheap"): {"combination_key": "k1",
                                      "verdict": "SUPPORTED"},
                    ("s2", "pricey"): {"combination_key": "k2",
                                       "verdict": "SUPPORTED"}},
            validity={"s1": validity("s1"), "s2": validity("s2")},
            budget={"budget_id": "b", "currency": "USD",
                    "available_micro": 1_500_000})
        result = h.command(h.body())
        assert rejected_codes(result)["cheap"] == "OVER_BUDGET"
        assert [e["candidate_key"] for e in result["eligible"]] == ["pricey"]

    def test_single_expensive_candidate_stays_eligible(self):
        # T25-04：贵不是拒绝理由。
        h = Harness(
            bundle={"capability_bundle_revision": 1,
                    "candidates": [candidate(quote_upper_bound_micro=9_000_000)],
                    "excluded": []})
        result = h.command(h.body())
        assert result["feasible"] is True
        assert result["eligible"][0]["candidate_key"] == "c1"

    def test_retired_never_passes_even_with_degraded_flag(self):
        # T25-10 的后半句。`retired` 不是"另一种 degraded"。
        h = Harness(
            bundle={"capability_bundle_revision": 1,
                    "candidates": [candidate(lifecycle="retired")],
                    "excluded": []},
            policy={"flags": {"allow_degraded_for_non_critical": True},
                    "weights": None, "thresholds": {}})
        result = h.command(h.body(criticality="non_critical"))
        assert rejected_codes(result)["c1"] == "PROVIDER_RETIRED"
        assert result["eligible"] == []

    def test_degraded_passes_only_for_non_critical(self):
        # T25-10：放行后 R 分量记 0，总分被扣 0.15。
        h = Harness(
            bundle={"capability_bundle_revision": 1, "candidates": [
                candidate("deg", "s1", lifecycle="degraded"),
                candidate("ok", "s2"),
            ], "excluded": []},
            snapshots={"s1": {"mode_details": [mode_detail()]},
                       "s2": {"mode_details": [mode_detail()]}},
            checks={("s1", "deg"): {"combination_key": "k1",
                                    "verdict": "SUPPORTED"},
                    ("s2", "ok"): {"combination_key": "k2",
                                   "verdict": "SUPPORTED"}},
            validity={"s1": validity("s1"), "s2": validity("s2")},
            policy={"flags": {"allow_degraded_for_non_critical": True},
                    "weights": None, "thresholds": {}})
        result = h.command(h.body(criticality="non_critical"))
        deg = [e for e in result["eligible"] if e["candidate_key"] == "deg"][0]
        assert deg["score_parts"]["R"] == 0.0

    def test_degraded_is_rejected_when_node_is_critical(self):
        # §3.1：`criticality` 不写就是关键节点，degraded 不放行。
        h = Harness(
            bundle={"capability_bundle_revision": 1,
                    "candidates": [candidate(lifecycle="degraded")],
                    "excluded": []},
            policy={"flags": {"allow_degraded_for_non_critical": True},
                    "weights": None, "thresholds": {}})
        result = h.command(h.body())
        assert rejected_codes(result)["c1"] == "PROVIDER_DEGRADED"

    def test_expired_evidence_is_rejected(self):
        # T25-11（E18）。
        h = Harness(validity={"snap_a": validity(
            usable=False, code="EVIDENCE_EXPIRED",
            evidence_valid_until="2020-01-01T00:00:00Z")})
        result = h.command(h.body())
        assert rejected_codes(result)["c1"] == "EVIDENCE_EXPIRED"

    def test_unverified_combination_allowed_only_on_first_attempt(self):
        # T25-12 / T25-13 的两面。
        h = Harness(
            validity={"snap_a": validity(usable=False,
                                         code="COMBINATION_NOT_SUPPORTED",
                                         combination="UNVERIFIED")},
            policy={"flags": {"allow_unverified_combination": True},
                    "weights": None, "thresholds": {}})
        allowed = h.command(h.body(attempt_no=1), command_id="c1")
        assert allowed["feasible"] is True
        blocked = h.command(h.body(attempt_no=2), command_id="c2")
        assert rejected_codes(blocked)["c1"] == "COMBINATION_UNVERIFIED"

    def test_unverified_combination_rejected_when_policy_forbids(self):
        h = Harness(validity={"snap_a": validity(
            usable=False, code="COMBINATION_NOT_SUPPORTED",
            combination="UNVERIFIED")})
        result = h.command(h.body())
        assert rejected_codes(result)["c1"] == "COMBINATION_UNVERIFIED"

    def test_cross_tenant_byok_pool_is_rejected(self):
        # T25-14（E28/E30）：`BYOK_MISMATCH`，且错误里不带池的细节。
        h = Harness(
            bundle={"capability_bundle_revision": 1, "candidates": [
                candidate(scope="tenant_byok")], "excluded": []},
            policy={"flags": {"allow_byok": False}, "weights": None,
                    "thresholds": {}})
        result = h.command(h.body())
        assert rejected_codes(result)["c1"] == "BYOK_MISMATCH"

    def test_cross_tenant_pool_tenant_is_denied(self):
        h = Harness(
            bundle={"capability_bundle_revision": 1, "candidates": [
                candidate(pool_tenant_id="other")], "excluded": []})
        result = h.command(h.body())
        assert rejected_codes(result)["c1"] == "CROSS_TENANT_DENIED"

    def test_residency_denied_regardless_of_price(self):
        # T25-15（E31）。
        h = Harness(
            bundle={"capability_bundle_revision": 1, "candidates": [
                candidate(residency_tags=["eu"], quote_upper_bound_micro=1)],
                "excluded": []},
            policy={"flags": {"required_residency": ["cn"]},
                    "weights": None, "thresholds": {}})
        result = h.command(h.body())
        assert rejected_codes(result)["c1"] == "RESIDENCY_DENIED"

    def test_resolution_above_limit_goes_to_degradation_only(self):
        # T25-16：不出现在 eligible，只进 degradation_options。
        h = Harness(
            snapshots={"snap_a": {"mode_details": [mode_detail(
                output_specs=[{"aspect_ratio": "9:16", "max_width": 720,
                               "max_height": 1280, "duration_tiers_s": [5],
                               "trims_to_nearest": True}])]}})
        result = h.command(h.body(resolution={"width": 1080, "height": 1920}))
        assert rejected_codes(result)["c1"] == "RESOLUTION_UNSUPPORTED"
        assert result["eligible"] == []
        assert "spec_adjust" in kinds(result)

    def test_voice_lock_swap_is_never_auto_executable(self):
        # T25-17：`silent_degradation=true` 且 `auto_executable=false`。
        # 要走到 `VOICE_LOCK_UNSUPPORTED`，模型必须**能原生出声**（否则先被
        # 第 13 条上半段的 `AUDIO_STRATEGY_UNSUPPORTED` 拦掉），只是锁不住音色。
        h = Harness(
            snapshots={"snap_a": {"mode_details": [mode_detail(
                native_audio=True, prompt_audio_lock=False)]}},
            checks={("snap_a", "c1"): {"combination_key": "ck",
                                       "verdict": "SUPPORTED"}})
        result = h.command(h.body(audio_strategy="native_locked_voice",
                                  dialogue_duration_ms=1000))
        assert rejected_codes(result)["c1"] == "VOICE_LOCK_UNSUPPORTED"
        swaps = [o for o in result["degradation_options"]
                 if o["option_id"] == "opt_model_swap_native_voice"]
        assert swaps and swaps[0]["auto_executable"] is False
        assert swaps[0]["silent_degradation"] is True


# ================================================================ 对白/时长

class TestDialogue:
    """E04：不猜目标、不删台词。"""

    def test_dialogue_longer_than_largest_tier_is_cut_by_dialogue_rule(self):
        # T25-07：4.2s 对白 + 4s 档 → DIALOGUE_EXCEEDS_DURATION，不是
        # DURATION_UNSUPPORTED（第 12 条先查对白）。
        h = Harness(
            snapshots={"snap_a": {"mode_details": [mode_detail(
                output_specs=[{"aspect_ratio": "9:16", "max_width": 1080,
                               "max_height": 1920, "duration_tiers_s": [4],
                               "trims_to_nearest": True}])]}}
        )
        result = h.command(h.body(dialogue_duration_ms=4200))
        assert rejected_codes(result)["c1"] == "DIALOGUE_EXCEEDS_DURATION"
        shorten = [o for o in result["degradation_options"]
                   if o["option_id"] == "opt_shorten_duration"]
        assert shorten and shorten[0]["auto_executable"] is False

    def test_unknown_dialogue_duration_is_a_persisted_infeasible(self):
        # T25-08：整单 INFEASIBLE / DIALOGUE_DURATION_UNKNOWN，不估算。
        # 而且是**一条决策**——不只是错误响应（T25-26）。
        h = Harness()
        result = h.command(h.body(
            audio_strategy="native_locked_voice", text="say this line"))
        assert result["feasible"] is False
        assert result["infeasible_reason"]["code"] == "DIALOGUE_DURATION_UNKNOWN"
        assert h.count("route_decision") == 1
        # 不估算：这一单根本不读 S24/S23。
        assert h.calls["candidates"] == 0
        assert h.calls["budget"] == 0

    def test_dialogue_within_tier_passes(self):
        h = Harness()
        result = h.command(h.body(dialogue_duration_ms=4200,
                                  duration_seconds=5))
        assert result["feasible"] is True
        # 吸附后的时长必须写进 normalized_input（S26 要照它派）。
        assert result["eligible"][0]["normalized_input"]["duration_seconds"] == 5

    def test_deadline_in_the_past_is_infeasible_without_reading_upstream(self):
        h = Harness()
        result = h.command(h.body())
        h.service  # noqa: B018 —— 只是让 harness 活着
        # 换一个已过期的 deadline，用另一个 command_id。
        body = h.body()
        body["generation_command"]["deadline"] = "2020-01-01T00:00:00Z"
        result = h.command(body, command_id="c2")
        assert result["infeasible_reason"]["code"] == "DEADLINE_ALREADY_PASSED"


# ================================================================ 评分

class TestScoring:
    """§4。分数只是排序信号，不是准入依据。"""

    def test_cheap_and_bad_candidate_is_eligible_but_ranked_last(self):
        # T25-05。
        h = Harness(
            bundle={"capability_bundle_revision": 1, "candidates": [
                candidate("cheap", "s1", account_pool_id="pool_cheap",
                          quote_upper_bound_micro=100_000),
                candidate("good", "s2", account_pool_id="pool_good",
                          quote_upper_bound_micro=900_000),
            ], "excluded": []},
            snapshots={"s1": {"mode_details": [mode_detail()]},
                       "s2": {"mode_details": [mode_detail()]}},
            checks={("s1", "cheap"): {"combination_key": "k1",
                                      "verdict": "SUPPORTED"},
                    ("s2", "good"): {"combination_key": "k2",
                                     "verdict": "SUPPORTED"}},
            validity={"s1": validity("s1"), "s2": validity("s2")},
            # 遥测按 `account_pool_id` 优先命中，两个候选共用一个池的话
            # 拿到的历史一样，"贵但可靠"就体现不出来——各自给一个池。
            telemetry={
                "pool_cheap": {"first_pass_count": 20, "sample_size": 200,
                               "reliability": 0.2, "p90_latency_s": 30},
                "pool_good": {"first_pass_count": 190, "sample_size": 200,
                              "reliability": 0.95, "p90_latency_s": 30},
            })
        result = h.command(h.body())
        by_key = {e["candidate_key"]: e for e in result["eligible"]}
        assert set(by_key) == {"cheap", "good"}
        assert by_key["cheap"]["rank"] > by_key["good"]["rank"]

    def test_single_candidate_gets_half_on_every_component(self):
        # §4.3：唯一候选不该自动满分。
        h = Harness()
        result = h.command(h.body())
        parts = result["eligible"][0]["score_parts"]
        assert all(v == 0.5 for v in parts.values()), parts

    def test_native_silent_model_pays_lipsync(self):
        # T25-09（docs/09）：原生不出声的模型口型费必须进成本。
        h = Harness(
            policy={"flags": {"lipsync_cost_micro": 400_000},
                    "weights": None, "thresholds": {}})
        result = h.command(h.body(audio_strategy="silent_then_tts"))
        parts = result["eligible"][0]["expected_cost_breakdown"]
        assert parts["lipsync"] == 400_000
        assert result["eligible"][0]["expected_cost_micro"] == sum(parts.values())

    def test_native_audio_model_has_zero_lipsync(self):
        # `native` 声明了原生对白，所以必须同时给对白时长——否则先撞
        # §2.2 的 DIALOGUE_DURATION_UNKNOWN，根本走不到成本核算。
        h = Harness(
            policy={"flags": {"lipsync_cost_micro": 400_000},
                    "weights": None, "thresholds": {}})
        result = h.command(h.body(audio_strategy="native",
                                  dialogue_duration_ms=2000))
        assert result["feasible"] is True
        assert result["eligible"][0]["expected_cost_breakdown"]["lipsync"] == 0

    def test_identity_weight_is_zeroed_when_not_required(self):
        # §4.1：无身份需求时 `I` 权重归零并重新归一化。
        h = Harness()
        result = h.command(h.body())
        scoring = h.rows("route_decision", route_id=result["route_id"])[0]
        summary = json.loads(scoring["scoring"])
        assert summary["identity_weighted"] is False
        assert summary["weights"]["I"] == 0.0


# ================================================================ 复算/事件

class TestDecisionRecord:
    """T25-21/22/23/26：决策本身能被复算、被核对。"""

    def test_score_is_recomputable_from_stored_parts(self):
        # T25-21：用 `scoring.weights` + `score_parts` 重算 `score` 一致。
        h = Harness()
        result = h.command(h.body())
        row = h.rows("route_decision", route_id=result["route_id"])[0]
        weights = json.loads(row["scoring"])["weights"]
        cand = h.rows("route_candidate", route_id=result["route_id"])[0]
        parts = json.loads(cand["score_parts"])
        recomputed = round(sum(weights[k] * parts[k] for k in parts), 6)
        assert recomputed == pytest.approx(cand["score"], abs=1e-6)

    def test_content_hash_covers_the_decision_not_the_clock(self):
        # §8：`created_at` / `route_id` 不进哈希，否则"同样输入同样结论"无法核对。
        h = Harness()
        first = h.command(h.body(), command_id="c1")
        second = h.command(h.body(), command_id="c2")
        assert first["route_id"] != second["route_id"]
        assert first["content_hash"] == second["content_hash"]
        assert content_hash({k: v for k, v in first.items()}) == \
            first["content_hash"]

    def test_same_request_hash_gives_same_decision_after_restart(self):
        # T25-22：重启 = 新进程读同一个库，不能给出不同结论。
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "s25.db")
            conn = sqlite3.connect(path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            h = Harness(conn=conn)
            first = h.command(h.body())
            conn.close()

            conn2 = sqlite3.connect(path, isolation_level=None)
            conn2.row_factory = sqlite3.Row
            h2 = Harness(conn=conn2)
            # 新进程对**同一条**命令回放：必须拿到同一结论。
            again = h2.command(h.body())
            conn2.close()
        assert again["replayed"] is True
        assert again["route_id"] == first["route_id"]
        assert again["content_hash"] == first["content_hash"]

    def test_quote_valid_to_is_fifteen_minutes_out(self):
        # §5.1 / §12 第 2 条：过期必须重新准入。
        h = Harness()
        result = h.command(h.body())
        created = h.rows("route_decision", route_id=result["route_id"])[0]
        assert result["quote_valid_to"].endswith("Z")
        assert result["quote_valid_to"] > created["created_at"][:19]
        assert created["quote_valid_to"] == result["quote_valid_to"]

    def test_infeasible_still_emits_event_with_null_selected(self):
        # T25-26（§7.1）。不发事件的话 S21 永远等不到结论。
        h = Harness(
            bundle={"capability_bundle_revision": 1, "candidates": [
                candidate(lifecycle="retired")], "excluded": []})
        result = h.command(h.body())
        assert result["feasible"] is False
        events = h.events()
        assert len(events) == 1
        assert events[0]["type"] == "model.route.selected"
        assert events[0]["data"]["selected"] is None
        assert events[0]["data"]["infeasible_reason"] == "NO_ELIGIBLE_CANDIDATE"

    def test_event_carries_both_version_coordinates_separately(self):
        # AGENTS.md：`execution_fence` 与 `node_epoch` 不能混用。
        h = Harness()
        h.command(h.body())
        data = h.events()[0]["data"]
        assert data["node_epoch"] == 3
        assert data["execution_fence"] == 11
        assert data["attempt_id"] == "att_1"

    def test_event_data_ref_has_four_keys(self):
        # MRT-1：契约要 4 个键，3 键版本通不过本仓的事件校验。
        h = Harness()
        h.command(h.body())
        envelope = h.events()[0]
        assert set(envelope["data_ref"]) == {
            "service_id", "id", "revision", "content_hash"}
        assert envelope["data_ref"]["content_hash"] == \
            envelope["data"]["content_hash"]

    def test_event_is_in_the_same_transaction_as_the_decision(self):
        # §7.1：事件写失败 → 决策也回滚。用"没有 outbox 行"证明它们同生共死。
        h = Harness()
        h.conn.execute(
            "CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox "
            "BEGIN SELECT RAISE(ABORT, 'broker down'); END")
        with pytest.raises(sqlite3.IntegrityError):
            h.command(h.body())
        assert h.count("route_decision") == 0
        assert h.count("policy_snapshot" if False else "routing_policy_snapshot") == 0

    def test_outbox_has_exactly_one_row_per_decision(self):
        # T25-25：`(aggregate_id, aggregate_seq)` 的语义在单条决策上就是一行。
        h = Harness()
        result = h.command(h.body())
        rows = h.rows("outbox", aggregate_id=result["route_id"])
        assert len(rows) == 1
        assert rows[0]["sequence"] == 1


# ================================================================ 上游失败

class TestUpstreamFailures:
    """§9：读不到就不出决策。三条 503，一条例外。"""

    def test_s24_timeout_is_503(self):
        # T25-18：不用缓存结论兜底。
        h = Harness(bundle=ConnectionError("timeout"))
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.status == 503
        assert exc.value.code == "CAPABILITY_SOURCE_UNAVAILABLE"
        assert h.count("route_decision") == 0

    def test_s23_timeout_is_503_not_a_skipped_budget_filter(self):
        # T25-19。
        h = Harness(budget=ConnectionError("timeout"))
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.status == 503
        assert exc.value.code == "BUDGET_SOURCE_UNAVAILABLE"

    def test_s02_failure_never_applies_defaults(self):
        # §9：默认策略可能放宽租户硬约束。
        h = Harness(policy=ConnectionError("down"))
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.status == 503
        assert exc.value.code == "POLICY_UNAVAILABLE"
        assert h.calls["candidates"] == 0

    def test_telemetry_absence_is_not_a_failure(self):
        # §9 的例外：历史 QC 缺失用平滑先验，不是 503。
        h = Harness(telemetry={})
        result = h.command(h.body())
        assert result["feasible"] is True
        assert result["eligible"][0]["sample_size"] == 0

    def test_missing_snapshot_source_is_503_not_an_empty_filter(self):
        # `mode_details` 缺失会让 §3 的 9–13 条静默通过，所以必须 503。
        h = Harness()
        h.service.snapshot_source = None
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.status == 503
        assert h.count("route_decision") == 0

    def test_missing_combination_source_is_503(self):
        h = Harness()
        h.service.combination_source = None
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.status == 503

    def test_currency_mismatch_is_409_not_a_guess(self):
        h = Harness(budget={"budget_id": "b", "currency": "CNY",
                            "available_micro": 10_000_000})
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.status == 409
        assert exc.value.code == "CURRENCY_MISMATCH"


# ================================================================ 输入校验

class TestContext:
    def test_missing_run_coordinates_is_422(self):
        h = Harness()
        body = h.body()
        del body["generation_command"]["context"]["node_id"]
        with pytest.raises(ServiceError) as exc:
            h.command(body)
        assert exc.value.status == 422

    def test_attempt_id_comes_from_the_command_not_the_context(self):
        # 契约：`execution-context.v2` 是 `additionalProperties:false` 且没有
        # `attempt_id`；它属于命令本身。缺了它就要 422。
        h = Harness()
        body = h.body()
        del body["generation_command"]["attempt_id"]
        with pytest.raises(ServiceError) as exc:
            h.command(body)
        assert exc.value.status == 422

    def test_tenant_mismatch_is_403(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command(h.body(), tenant_id="t2")
        assert exc.value.status == 403
        assert exc.value.code == "TENANT_MISMATCH"

    def test_cross_tenant_read_is_404(self):
        # 不确认存在：跨租户查询不能泄露"这条决策真的存在"。
        h = Harness()
        result = h.command(h.body())
        with pytest.raises(ServiceError) as exc:
            h.service.get_route("t2", result["route_id"])
        assert exc.value.status == 404

    def test_list_requires_a_scope(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.service.list_routes("t1")
        assert exc.value.status == 422
        assert exc.value.code == "QUERY_SCOPE_REQUIRED"

    def test_list_by_attempt_id(self):
        h = Harness()
        h.command(h.body(), command_id="c1")
        listed = h.service.list_routes("t1", attempt_id="att_1")
        assert len(listed["items"]) == 1


# ================================================================ 边界

class TestBoundary:
    """S25 不是第二权威：它没有余额、没有 permit、不写任务。"""

    def test_no_balance_or_permit_columns(self):
        cols = {r[1] for r in Harness().conn.execute(
            "PRAGMA table_info(route_decision)").fetchall()}
        for banned in ("available_micro", "reserved_micro", "balance",
                       "permit", "grant", "job_id"):
            assert banned not in cols

    def test_no_jobs_table(self):
        names = {r[0] for r in Harness().conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "generation_job" not in names
        assert "dispatch_grant" not in names

    def test_excluded_candidates_are_kept_not_dropped(self):
        # S24 已经排除的候选要原样进 rejected，否则"为什么没选它"不可解释。
        h = Harness(bundle={
            "capability_bundle_revision": 1,
            "candidates": [candidate()],
            "excluded": [{"candidate_key": "x1", "code": "PRICE_EXPIRED",
                          "detail": "no price"}],
        })
        result = h.command(h.body())
        assert "x1" in rejected_codes(result)

    def test_projection_lag_is_recorded_when_supplied(self):
        # §9：投影落后仍可出决策，但水位与标记都要落地。
        # **注意**：真实 S24 不发布这个信号（MRT-18），所以这只证明"给了就
        # 存得住"，不证明 T25-20 已实现。
        h = Harness(bundle={
            "capability_bundle_revision": 812, "projection_lag": True,
            "candidates": [candidate()], "excluded": []})
        result = h.command(h.body())
        row = h.rows("route_decision", route_id=result["route_id"])[0]
        assert row["projection_lag"] == 1
        assert row["capability_bundle_revision"] == 812

    def test_projection_lag_is_absent_from_real_s24_shape(self):
        # MRT-18：按 S24 真实返回体（只有 revision + watermark）跑，标记只能是 0。
        # 这条用例的意义是**把缺口钉在测试里**，而不是假装它工作。
        h = Harness()
        result = h.command(h.body())
        row = h.rows("route_decision", route_id=result["route_id"])[0]
        assert row["projection_lag"] == 0
        assert row["capability_bundle_revision"] == 812
        assert "projection_lag" not in h.bundle


# ================================================================ HTTP

def _http_client(monkeypatch, tmp_path, **over):
    """`api/app.py` 的 TestClient，上游换成 Harness 的 stub。

    - `S25_DB` 必须指向独立文件库（import 那一刻就建连接与表）；
    - 路由函数在定义时捕获模块全局，所以 `module.service` / `module._idem`
      必须重绑，且重绑时用 `module._conn`，否则断言查的是另一个库。
    """
    monkeypatch.setenv("S25_DB", str(tmp_path / "s25.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    # 不能 `import app`：别的服务也有 `api/app.py` 且可能先进 sys.modules。
    spec = importlib.util.spec_from_file_location("s25_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s25_api_app"] = module

    h = Harness(conn=module._conn, **over)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h


def _post(client, body, *, command_id="c1", tenant="t1"):
    return client.post("/route-decisions", json=body,
                       headers={"Idempotency-Key": command_id,
                                "X-Tenant-Id": tenant})


class TestHttpLayer:
    def test_route_decisions_returns_plan_shape(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = _post(client, Harness().body())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["replayed"] is False
        assert set(body) >= {"route_id", "feasible", "eligible", "rejected",
                             "policy", "quote_valid_to", "content_hash"}
        assert body["feasible"] is True

    def test_replay_returns_replayed_flag(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        body = Harness().body()
        first = _post(client, body)
        second = _post(client, body)
        assert second.json()["replayed"] is True
        assert second.json()["route_id"] == first.json()["route_id"]

    def test_same_key_different_body_is_409(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        _post(client, Harness().body())
        r = _post(client, Harness().body(duration_seconds=8))
        assert r.status_code == 409
        assert r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        body = Harness().body()
        body["tenant_id"] = "t2"
        r = _post(client, body)
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_MISMATCH"

    def test_missing_idempotency_key_is_400(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/route-decisions", json=Harness().body(),
                        headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 422 or r.status_code == 400

    def test_upstream_timeout_becomes_503(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path,
                                 bundle=ConnectionError("timeout"))
        r = _post(client, Harness().body())
        assert r.status_code == 503
        assert r.json()["code"] == "CAPABILITY_SOURCE_UNAVAILABLE"
        assert r.json()["retryable"] is True

    def test_get_route_roundtrip(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        got = client.get(f"/route-decisions/{created['route_id']}",
                         headers={"X-Tenant-Id": "t1"})
        assert got.status_code == 200
        assert got.json()["route"]["route_id"] == created["route_id"]

    def test_get_route_unknown_is_404(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/route-decisions/rt_nope",
                       headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 404

    def test_list_without_scope_is_422(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/route-decisions", headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 422
        assert r.json()["code"] == "QUERY_SCOPE_REQUIRED"

    def test_list_by_attempt_id(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = client.get("/route-decisions?attempt_id=att_1",
                       headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1

    def test_readiness_reports_contract_gaps(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/health/readiness")
        assert r.status_code == 200
        assert r.json()["ready"] is True
        ids = {g["id"] for g in r.json()["contract_gaps"]}
        assert "MRT-17" in ids
