"""S24 测试。全部用本地 SQLite 与注入的替身，不接真实供应商、不付费。

对照 plans/S24-model-registry.md 的 T24-xx。事件 payload 对着已发布的
`contracts/model.capability-changed.v2.schema.json` 校验，不只看方案的示例文字。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import SqliteIdempotencyStore, canonical_request_hash

from model_registry import SCHEMA
from model_registry.combination import key_for
from model_registry.service import ModelRegistryService, ServiceError

ROOT = Path(__file__).resolve().parents[3]
EVENT_SCHEMA = ROOT / "contracts" / "model.capability-changed.v2.schema.json"

TA = "t_aaa"
TB = "t_bbb"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    SqliteIdempotencyStore(conn)
    return conn


def _svc(clock=None) -> ModelRegistryService:
    conn = _connect()
    return ModelRegistryService(conn, clock=clock) if clock else ModelRegistryService(conn)


def _seed(svc: ModelRegistryService, *, provider="seedance", channel="v1",
          pool="pool_a", pool_tenant="*", scope="shared"):
    svc.register_channel({"provider": provider, "channel": channel,
                          "connector_deployment": "c1",
                          "egress_identity": "e1", "credential_ref": "secret://a",
                          "lifecycle": "active"})
    svc.register_pool({"pool_id": pool, "tenant_id": pool_tenant,
                       "provider": provider, "channel": channel,
                       "region": "ap-east-1", "scope": scope,
                       "credential_ref": None if scope == "shared" else "secret://b",
                       "data_region": "ap-east-1", "max_inflight": 4, "rpm": 60,
                       "health": "healthy", "enabled": True})


def _snap(*, lifecycle="active", durations=(5, 10), trims=True,
          provider="seedance", channel="v1", pool="pool_a",
          model="seedance-1.0", effective="2026-01-01T00:00:00Z",
          maxes="2026-12-31T00:00:00Z", evidence=None, support="documented",
          claims=None, rules=None):
    return {
        "provider": provider, "channel": channel, "model_id": model,
        "model_snapshot": "snap-250528", "region": "ap-east-1",
        "account_pool_id": pool, "lifecycle": lifecycle,
        "data_region": "ap-east-1", "residency_tags": ["sg"],
        "effective_from": effective,
        "evidence": evidence if evidence is not None else [
            {"evidence_id": "ev1", "method": "account_probe",
             "result": "supported", "valid_until": maxes,
             "fetched_at": "2026-01-01T00:00:00Z"}],
        "modes": [{
            "mode": "image_to_video", "support": support,
            "execution_kind": "poll", "prompt_required": True,
            "evidence_id": "ev1",
            "reference_rules": rules if rules is not None else [
                {"role": "character", "min_count": 0, "max_count": 3}],
            "output_specs": [{"aspect_ratio": "9:16", "max_width": 1080,
                              "max_height": 1920,
                              "duration_tiers_s": list(durations),
                              "trims_to_nearest": trims}],
            "capability_claims": claims if claims is not None else [
                {"capability_key": "preserve_identity", "support": "documented"}],
        }],
    }


def _price(units=None, *, valid_to="2026-12-31T00:00:00Z", tiers=None):
    return {"currency": "USD", "tax_mode": "exclusive",
            "billing_units": units or {"request": 0, "generated_second": 30_000},
            "min_charge_micro": 0, "failure_charge": "none",
            "margin_bps": 0, "quotable": True,
            "valid_from": "2026-01-01T00:00:00Z", "valid_to": valid_to,
            "source_ref": "https://example/pricing",
            "source_hash": hashlib.sha256(b"pricing").hexdigest(),
            "tiers": tiers or []}


def _app():
    """`api/app.py` 与其他服务同名，只能按路径加载（同 S28 的做法）。

    返回 (client, service)；`app` 进程内共享一个连接，测试要先在同一个 service
    上登记渠道与池，否则快照的外键无主。
    """
    import importlib.util
    path = ROOT / "services" / "model-registry" / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s24_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return TestClient(module.app), module.service


def _project(svc, tenant=TA):
    svc.apply_authorization_projection(
        tenant, home_region="ap-east-1", allowed_residency_tags=["sg"],
        source_revision="rev-1", resolved_at="2026-01-01T00:00:00Z")


# -- T24-01 内容寻址 -------------------------------------------------------

def test_same_content_replays_without_new_revision():
    svc = _svc()
    _seed(svc)
    first = svc.register_snapshot({"snapshot": _snap(), "prices": [_price()]})
    second = svc.register_snapshot({"snapshot": _snap(), "prices": [_price()]})
    assert first["snapshot_id"] == second["snapshot_id"]
    assert second["replayed_content"] is True
    assert second["capability_bundle_revision"] == first["capability_bundle_revision"]
    assert svc.bundle_revision() == first["capability_bundle_revision"]


def test_content_change_makes_a_new_snapshot():
    svc = _svc()
    _seed(svc)
    a = svc.register_snapshot({"snapshot": _snap()})
    b = svc.register_snapshot({"snapshot": _snap(
        durations=(5, 10, 15), effective="2026-02-01T00:00:00Z")})
    assert a["snapshot_id"] != b["snapshot_id"]
    assert b["capability_bundle_revision"] > a["capability_bundle_revision"]


def test_lifecycle_is_not_part_of_content_identity():
    svc = _svc()
    _seed(svc)
    a = svc.register_snapshot({"snapshot": _snap(lifecycle="validating")})
    b = svc.register_snapshot({"snapshot": _snap(lifecycle="validating")})
    assert a["snapshot_id"] == b["snapshot_id"]


# -- 守卫 ------------------------------------------------------------------

def test_active_without_probe_evidence_is_rejected():
    svc = _svc()
    _seed(svc)
    with pytest.raises(ServiceError) as exc:
        svc.register_snapshot({"snapshot": _snap(evidence=[])})
    assert (exc.value.status, exc.value.code) == (422, "ACTIVE_REQUIRES_PROBE_EVIDENCE")


def test_documented_evidence_does_not_unlock_active():
    svc = _svc()
    _seed(svc)
    with pytest.raises(ServiceError) as exc:
        svc.register_snapshot({"snapshot": _snap(evidence=[
            {"evidence_id": "d1", "method": "doc_read", "result": "supported",
             "valid_until": "2026-12-31T00:00:00Z"}])})
    assert exc.value.code == "ACTIVE_REQUIRES_PROBE_EVIDENCE"


def test_expired_evidence_does_not_unlock_active():
    svc = _svc(clock=lambda: "2026-06-01T00:00:00Z")
    _seed(svc)
    with pytest.raises(ServiceError) as exc:
        svc.register_snapshot({"snapshot": _snap(
            effective="2026-01-01T00:00:00Z",
            evidence=[{"evidence_id": "old", "method": "account_probe",
                       "result": "supported",
                       "valid_until": "2026-02-01T00:00:00Z"}])})
    assert exc.value.code == "ACTIVE_REQUIRES_PROBE_EVIDENCE"


def test_historical_endpoint_cannot_be_active():
    svc = _svc()
    _seed(svc, provider="sora", channel="historical")
    with pytest.raises(ServiceError) as exc:
        svc.register_snapshot({"snapshot": _snap(
            provider="sora", channel="historical")})
    assert (exc.value.status, exc.value.code) == (
        422, "HISTORICAL_ENDPOINT_CANNOT_BE_ACTIVATED")


def test_historical_endpoint_may_be_registered_retired():
    svc = _svc()
    _seed(svc, provider="sora", channel="historical")
    out = svc.register_snapshot({"snapshot": _snap(
        provider="sora", channel="historical", lifecycle="retired")})
    assert out["lifecycle"] == "retired"


def test_probe_verified_claim_without_evidence_is_downgraded():
    svc = _svc()
    _seed(svc)
    out = svc.register_snapshot({"snapshot": _snap(claims=[
        {"capability_key": "preserve_identity", "support": "probe_verified",
         "evidence_id": "ghost"}])})
    assert out["rejected_claims"] == [{
        "mode": "image_to_video", "capability_key": "preserve_identity",
        "reason": "EVIDENCE_MISSING_FOR_PROBE_VERIFIED"}]
    detail = svc.snapshot(TA, out["snapshot_id"])["mode_details"][0]
    assert detail["capability_claims"][0]["support"] == "documented"


def test_exclusive_with_is_stored_symmetrically():
    svc = _svc()
    _seed(svc)
    out = svc.register_snapshot({"snapshot": _snap(rules=[
        {"role": "first_frame", "min_count": 0, "max_count": 1,
         "exclusive_with": ["last_frame"]},
        {"role": "last_frame", "min_count": 0, "max_count": 1}])})
    rules = {r["role"]: r for r in
             svc.snapshot(TA, out["snapshot_id"])["mode_details"][0]["reference_rules"]}
    assert rules["first_frame"]["exclusive_with"] == ["last_frame"]
    assert rules["last_frame"]["exclusive_with"] == ["first_frame"]


# -- 幂等与版本 ------------------------------------------------------------

def test_expected_revision_conflict():
    svc = _svc()
    _seed(svc)
    svc.register_snapshot({"snapshot": _snap()})
    with pytest.raises(ServiceError) as exc:
        svc.register_snapshot({"snapshot": _snap(durations=(5,)), "expected_revision": 99})
    assert (exc.value.status, exc.value.code) == (409, "REVISION_CONFLICT")


def test_expected_revision_match_is_accepted():
    svc = _svc()
    _seed(svc)
    first = svc.register_snapshot({"snapshot": _snap()})
    out = svc.register_snapshot({"snapshot": _snap(
        durations=(5,), effective="2026-02-01T00:00:00Z"),
        "expected_revision": first["capability_bundle_revision"]})
    assert out["capability_bundle_revision"] == first["capability_bundle_revision"] + 1


def test_effective_from_collision_is_a_conflict_not_a_crash():
    svc = _svc()
    _seed(svc)
    svc.register_snapshot({"snapshot": _snap()})
    with pytest.raises(ServiceError) as exc:
        svc.register_snapshot({"snapshot": _snap(durations=(5,))})
    assert (exc.value.status, exc.value.code) == (409, "REVISION_CONFLICT")


def test_new_snapshot_closes_previous_effective_range():
    svc = _svc()
    _seed(svc)
    old = svc.register_snapshot({"snapshot": _snap(
        effective="2026-01-01T00:00:00Z")})
    svc.register_snapshot({"snapshot": _snap(effective="2026-03-01T00:00:00Z")})
    detail = svc.snapshot(TA, old["snapshot_id"])
    assert detail["effective_to"] == "2026-03-01T00:00:00.000000Z"


# -- 候选 ------------------------------------------------------------------

def test_candidates_require_authorization_projection():
    svc = _svc()
    _seed(svc)
    svc.register_snapshot({"snapshot": _snap()})
    with pytest.raises(ServiceError) as exc:
        svc.candidates(TA, {})
    assert (exc.value.status, exc.value.code) == (503, "AUTH_DEPENDENCY_UNAVAILABLE")


def test_candidates_are_empty_but_explained_when_nothing_usable():
    svc = _svc()
    _seed(svc, pool="pool_a", pool_tenant=TB, scope="tenant_byok")
    _project(svc, TA)
    svc.register_snapshot({"snapshot": _snap(pool="pool_a")})
    out = svc.candidates(TA, {})
    assert out["candidates"] == []
    assert out["excluded"][0]["code"] == "CROSS_TENANT_DENIED"


def test_candidate_payload_has_no_bound_without_price():
    svc = _svc()
    _seed(svc)
    _project(svc)
    svc.register_snapshot({"snapshot": _snap()})
    out = svc.candidates(TA, {"mode": "image_to_video", "aspect_ratio": "9:16"})
    assert out["candidates"] == []
    assert out["excluded"][0]["code"] == "PRICE_EXPIRED"


def test_candidate_includes_upper_bound_when_priced():
    svc = _svc()
    _seed(svc)
    _project(svc)
    svc.register_snapshot({"snapshot": _snap(), "prices": [_price()]})
    out = svc.candidates(TA, {"mode": "image_to_video", "aspect_ratio": "9:16"})
    assert len(out["candidates"]) == 1
    cand = out["candidates"][0]
    # 10 秒 × 30000 微 = 300000
    assert cand["quote_upper_bound_micro"] == 300_000
    assert cand["provider"] == "seedance"


@pytest.mark.parametrize("lifecycle,code", [
    ("disabled", "PROVIDER_DISABLED"),
    ("validating", "PROVIDER_VALIDATING"),
    ("degraded", "PROVIDER_DEGRADED"),
    ("retired", "PROVIDER_RETIRED"),
])
def test_non_active_lifecycles_are_all_excluded(lifecycle, code):
    svc = _svc()
    _seed(svc)
    _project(svc)
    svc.register_snapshot({"snapshot": _snap(lifecycle=lifecycle),
                           "prices": [_price()]})
    out = svc.candidates(TA, {})
    assert out["candidates"] == []
    assert out["excluded"][0]["code"] == code


def test_unhealthy_pool_is_excluded():
    svc = _svc()
    _seed(svc)
    _project(svc)
    svc.register_snapshot({"snapshot": _snap(), "prices": [_price()]})
    svc.set_pool_health("pool_a", "unauthorized")
    out = svc.candidates(TA, {})
    assert out["excluded"][0]["code"] == "ACCOUNT_UNAVAILABLE"


def test_missing_mode_is_excluded_not_silently_allowed():
    svc = _svc()
    _seed(svc)
    _project(svc)
    svc.register_snapshot({"snapshot": _snap(), "prices": [_price()]})
    out = svc.candidates(TA, {"mode": "text_to_video"})
    assert out["candidates"] == []
    assert out["excluded"][0]["code"] == "MODE_UNSUPPORTED"


def test_missing_capability_is_excluded_not_silently_allowed():
    svc = _svc()
    _seed(svc)
    _project(svc)
    svc.register_snapshot({"snapshot": _snap(), "prices": [_price()]})
    out = svc.candidates(TA, {"required_capabilities": "no_such_cap"})
    assert out["candidates"] == []
    assert out["excluded"][0]["code"] == "CAPABILITY_MISSING:no_such_cap"


def test_expired_price_is_excluded():
    svc = _svc(clock=lambda: "2027-01-01T00:00:00Z")
    _seed(svc)
    _project(svc)
    svc.register_snapshot({"snapshot": _snap(
        effective="2026-01-01T00:00:00Z",
        evidence=[{"evidence_id": "ev1", "method": "account_probe",
                   "result": "supported", "valid_until": "2027-06-01T00:00:00Z"}])})
    out = svc.candidates(TA, {})
    assert out["excluded"][0]["code"] == "PRICE_EXPIRED"


def test_unbounded_units_are_not_quoted_as_zero():
    svc = _svc()
    _seed(svc)
    _project(svc)
    svc.register_snapshot({"snapshot": _snap(),
                           "prices": [_price({"request": 1000, "output_token": 5})]})
    out = svc.candidates(TA, {"mode": "image_to_video"})
    assert out["candidates"] == []
    assert out["excluded"][0]["code"] == "NO_VERIFIABLE_UPPER_BOUND"


# -- 判定 ------------------------------------------------------------------

def _supported_candidate(svc, *, trims=True, durations=(5, 10)):
    svc.register_snapshot({"snapshot": _snap(trims=trims, durations=durations),
                           "prices": [_price()],
                           "command_id": "c1", "tenant_id": TA})
    return svc.candidates(TA, {})["candidates"][0]["snapshot_id"]


def test_unverified_is_not_usable():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    out = svc.check_capability(TA, {"snapshot_id": sid, "mode": "image_to_video",
                                    "aspect_ratio": "9:16", "duration_seconds": 5})
    assert out["verdict"] == "UNVERIFIED"
    assert {r["code"] for r in out["reasons"]} == {"COMBINATION_UNVERIFIED"}


def test_verdict_shape_matches_contract_exactly():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    out = svc.check_capability(TA, {"snapshot_id": sid, "mode": "image_to_video",
                                    "aspect_ratio": "9:16", "duration_seconds": 5})
    assert set(out) == {"verdict", "combination_key", "normalized_shape", "checks",
                        "reasons", "evidence_id"}


def _add_probe(svc, sid, *, result="supported", mode="image_to_video",
               duration_seconds=5, valid_until="2026-12-31T00:00:00Z",
               probed_at="2026-01-01T00:00:00Z"):
    key, _ = key_for(mode=mode, refs=[], audio_strategy="silent",
                     duration_seconds=duration_seconds, aspect_ratio="9:16",
                     width=1080, height=1920, lora=False,
                     output_spec={"duration_tiers_s": [5, 10],
                                  "max_width": 1080, "max_height": 1920})
    svc.conn.execute("BEGIN IMMEDIATE")
    svc.conn.execute(
        "INSERT INTO capability_evidence (evidence_id, snapshot_id, method, result,"
        " fetched_at, valid_until, created_at) VALUES (?, ?, 'account_probe', ?, ?,"
        " ?, ?) ON CONFLICT (evidence_id) DO NOTHING",
        (f"probe-{key[:8]}", sid, result, probed_at, valid_until, probed_at))
    svc.conn.execute("COMMIT")
    svc._write_probe(sid, {"mode": mode, "result": result,
                           "evidence_id": f"probe-{key[:8]}",
                           "probed_at": probed_at, "valid_until": valid_until,
                           "shape": {"duration_seconds": duration_seconds,
                                     "aspect_ratio": "9:16",
                                     "resolution": {"width": 1080, "height": 1920}}},
                     now=probed_at)
    return key


def test_probe_then_supported():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    key = _add_probe(svc, sid)
    out = svc.check_capability(TA, {"snapshot_id": sid, "mode": "image_to_video",
                                    "aspect_ratio": "9:16", "duration_seconds": 5})
    assert out["verdict"] == "SUPPORTED"
    assert out["combination_key"] == key
    assert out["evidence_id"] == f"probe-{key[:8]}"


def test_snap_to_nearest_uses_the_snapped_tier_for_the_key():
    """§3.3：4s 请求在档位 [5,10] 且 trims_to_nearest → SUPPORTED，键落在 5s。"""
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    key = _add_probe(svc, sid, duration_seconds=5)
    out = svc.check_capability(TA, {"snapshot_id": sid, "mode": "image_to_video",
                                    "aspect_ratio": "9:16", "duration_seconds": 4})
    assert out["verdict"] == "SUPPORTED"
    assert out["combination_key"] == key
    tier = [c for c in out["checks"] if c["rule"] == "output.duration_tier"][0]
    assert tier["note"] == "snap_to=5s"


def test_snap_to_nearest_false_is_unsupported():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc, trims=False)
    _add_probe(svc, sid, duration_seconds=5)
    out = svc.check_capability(TA, {"snapshot_id": sid, "mode": "image_to_video",
                                    "aspect_ratio": "9:16", "duration_seconds": 4})
    assert out["verdict"] == "UNSUPPORTED"
    assert {r["code"] for r in out["reasons"]} == {"OUTPUT_DURATION_UNSUPPORTED"}


def test_duration_beyond_all_tiers_is_unsupported():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    out = svc.check_capability(TA, {"snapshot_id": sid, "mode": "image_to_video",
                                    "aspect_ratio": "9:16", "duration_seconds": 30})
    assert out["verdict"] == "UNSUPPORTED"
    assert {r["code"] for r in out["reasons"]} == {"OUTPUT_DURATION_UNSUPPORTED"}


def test_reference_count_over_limit_is_unsupported():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    out = svc.check_capability(TA, {
        "snapshot_id": sid, "mode": "image_to_video", "aspect_ratio": "9:16",
        "duration_seconds": 5,
        "refs": [{"role": "character", "count": 4}]})
    assert out["verdict"] == "UNSUPPORTED"
    assert {r["code"] for r in out["reasons"]} == {"REFERENCE_COUNT_OUT_OF_RANGE"}


def test_exclusive_roles_conflict_is_unsupported():
    svc = _svc()
    _seed(svc)
    _project(svc)
    svc.register_snapshot({"snapshot": _snap(rules=[
        {"role": "first_frame", "min_count": 0, "max_count": 1,
         "exclusive_with": ["last_frame"]},
        {"role": "last_frame", "min_count": 0, "max_count": 1}]),
        "prices": [_price()]})
    sid = svc.candidates(TA, {})["candidates"][0]["snapshot_id"]
    out = svc.check_capability(TA, {
        "snapshot_id": sid, "mode": "image_to_video", "aspect_ratio": "9:16",
        "duration_seconds": 5,
        "refs": [{"role": "first_frame", "count": 1},
                 {"role": "last_frame", "count": 1}]})
    assert out["verdict"] == "UNSUPPORTED"
    assert "REFERENCE_EXCLUSIVE_CONFLICT" in {r["code"] for r in out["reasons"]}


def test_unsupported_mode_is_unsupported_even_with_probe():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    _add_probe(svc, sid)
    svc.conn.execute("UPDATE capability_mode SET support = 'field_failed'"
                     " WHERE snapshot_id = ?", (sid,))
    out = svc.check_capability(TA, {"snapshot_id": sid, "mode": "image_to_video",
                                    "aspect_ratio": "9:16", "duration_seconds": 5})
    assert out["verdict"] == "UNSUPPORTED"
    assert "MODE_UNSUPPORTED" in {r["code"] for r in out["reasons"]}


def test_expired_probe_declines_to_unverified():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    _add_probe(svc, sid, valid_until="2026-02-01T00:00:00Z")
    svc.clock = lambda: "2026-03-01T00:00:00Z"
    out = svc.check_capability(TA, {"snapshot_id": sid, "mode": "image_to_video",
                                    "aspect_ratio": "9:16", "duration_seconds": 5})
    assert out["verdict"] == "UNVERIFIED"
    assert "COMBINATION_UNVERIFIED" in {r["code"] for r in out["reasons"]}


def test_out_of_order_probe_does_not_overwrite_newer():
    """T24-15：乱序到达的旧探测不能覆盖新结论。"""
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    key = _add_probe(svc, sid, result="unsupported",
                     probed_at="2026-05-01T00:00:00Z",
                     valid_until="2026-12-31T00:00:00Z")
    svc._write_probe(sid, {
        "mode": "image_to_video", "result": "supported",
        "evidence_id": f"probe-{key[:8]}",
        "probed_at": "2026-01-01T00:00:00Z",
        "valid_until": "2026-12-31T00:00:00Z",
        "shape": {"duration_seconds": 5, "aspect_ratio": "9:16",
                  "resolution": {"width": 1080, "height": 1920}}},
        now="2026-01-01T00:00:00Z")
    row = svc.conn.execute(
        "SELECT result FROM mode_combination_probe WHERE snapshot_id = ?"
        " AND combination_key = ?", (sid, key)).fetchone()
    assert row["result"] == "unsupported"


def test_cross_tenant_snapshot_is_not_found():
    svc = _svc()
    _seed(svc)
    out = svc.register_snapshot({"snapshot": {**_snap(), "tenant_id": TB}})
    with pytest.raises(ServiceError) as exc:
        svc.check_capability(TA, {"snapshot_id": out["snapshot_id"],
                                  "mode": "image_to_video"})
    assert (exc.value.status, exc.value.code) == (404, "SNAPSHOT_NOT_FOUND")


def test_unknown_snapshot_is_404():
    svc = _svc()
    with pytest.raises(ServiceError) as exc:
        svc.check_capability(TA, {"snapshot_id": "0" * 64, "mode": "text_to_image"})
    assert (exc.value.status, exc.value.code) == (404, "SNAPSHOT_NOT_FOUND")


def test_unknown_mode_is_404():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    with pytest.raises(ServiceError) as exc:
        svc.check_capability(TA, {"snapshot_id": sid, "mode": "tts"})
    assert (exc.value.status, exc.value.code) == (404, "MODE_NOT_FOUND")


# -- 生命周期 --------------------------------------------------------------

def test_retired_is_terminal():
    svc = _svc()
    _seed(svc)
    out = svc.register_snapshot({"snapshot": _snap(lifecycle="validating", evidence=[
        {"evidence_id": "official", "method": "doc_read", "result": "supported",
         "raw_ref": "s3://docs/sunset.pdf", "source_url": "https://example/sunset",
         "valid_until": "2026-12-31T00:00:00Z"}])})
    sid = out["snapshot_id"]
    svc.transition(TA, {"snapshot_id": sid, "to_state": "retired", "reason": "gone",
                        "evidence_id": "official"})
    with pytest.raises(ServiceError) as exc:
        svc.transition(TA, {"snapshot_id": sid, "to_state": "active",
                            "reason": "back"})
    assert (exc.value.status, exc.value.code) == (422, "INVALID_TRANSITION")


def test_activate_requires_this_snapshots_evidence():
    """守卫必须限定本快照：别的快照的探测不能给这条解锁 active。"""
    svc = _svc()
    _seed(svc)
    good = svc.register_snapshot({"snapshot": _snap(model="model-a")})
    poor = svc.register_snapshot({"snapshot": _snap(
        model="model-b", lifecycle="validating", evidence=[
            {"evidence_id": "d-b", "method": "doc_read", "result": "supported",
             "valid_until": "2026-12-31T00:00:00Z"}])})
    assert good["snapshot_id"] != poor["snapshot_id"]
    with pytest.raises(ServiceError) as exc:
        svc.transition(TA, {"snapshot_id": poor["snapshot_id"],
                            "to_state": "active", "reason": "promote"})
    assert (exc.value.status, exc.value.code) == (
        422, "ACTIVE_REQUIRES_PROBE_EVIDENCE")


def test_retire_requires_official_evidence():
    svc = _svc()
    _seed(svc)
    out = svc.register_snapshot({"snapshot": _snap()})
    with pytest.raises(ServiceError) as exc:
        svc.transition(TA, {"snapshot_id": out["snapshot_id"],
                            "to_state": "retired", "reason": "discontinued"})
    assert (exc.value.status, exc.value.code) == (
        422, "RETIRE_REQUIRES_OFFICIAL_EVIDENCE")


def test_no_op_transition_is_a_conflict():
    svc = _svc()
    _seed(svc)
    out = svc.register_snapshot({"snapshot": _snap()})
    with pytest.raises(ServiceError) as exc:
        svc.transition(TA, {"snapshot_id": out["snapshot_id"],
                            "to_state": "active", "reason": "again"})
    assert (exc.value.status, exc.value.code) == (409, "NO_STATE_CHANGE")


def test_expiry_scan_degrades_and_records_history():
    svc = _svc()
    _seed(svc)
    out = svc.register_snapshot({"snapshot": _snap()})
    degraded = svc.expire_evidence(now="2027-01-01T00:00:00Z")
    assert degraded == [{"snapshot_id": out["snapshot_id"],
                         "code": "EVIDENCE_EXPIRED"}]
    detail = svc.snapshot(TA, out["snapshot_id"])
    assert detail["lifecycle"] == "degraded"
    history = svc.lifecycle_history(TA, out["snapshot_id"])["items"]
    assert [h["to_state"] for h in history] == ["active", "degraded"]


# -- 批量有效性（S26 用） ---------------------------------------------------

def test_validity_reports_not_usable_after_revocation():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    svc.set_pool_health("pool_a", "unauthorized")
    out = svc.validity(TA, {"items": [{"snapshot_id": sid}]})
    assert out["items"][0]["usable"] is False
    assert out["items"][0]["code"] == "ACCOUNT_UNAVAILABLE"


def test_validity_flags_evidence_expiry():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    svc.clock = lambda: "2027-01-01T00:00:00Z"
    out = svc.validity(TA, {"items": [{"snapshot_id": sid}]})
    assert out["items"][0]["usable"] is False
    assert out["items"][0]["code"] == "EVIDENCE_EXPIRED"


def test_validity_reports_combination_verdict():
    svc = _svc()
    _seed(svc)
    _project(svc)
    sid = _supported_candidate(svc)
    key = _add_probe(svc, sid)
    usable = svc.validity(TA, {"items": [{"snapshot_id": sid, "mode": "image_to_video",
                                          "combination_key": key}]})
    assert usable["items"][0]["usable"] is True
    assert usable["items"][0]["combination"] == "SUPPORTED"
    missing = svc.validity(TA, {"items": [{"snapshot_id": sid,
                                           "mode": "image_to_video",
                                           "combination_key": "9" * 64}]})
    assert missing["items"][0]["usable"] is False


def test_validity_is_bounded():
    svc = _svc()
    with pytest.raises(ServiceError) as exc:
        svc.validity(TA, {"items": [{"snapshot_id": "x"}] * 201})
    assert exc.value.status == 400


# -- 价格 ------------------------------------------------------------------

def test_price_upper_bound_uses_highest_tier_not_last_row():
    svc = _svc()
    _seed(svc)
    price = _price({"request": 0, "generated_second": 10_000}, tiers=[
        {"dimension": "duration_s", "from_inclusive": "0", "to_exclusive": "5",
         "unit_price_micro": 10_000},
        {"dimension": "duration_s", "from_inclusive": "5", "to_exclusive": None,
         "unit_price_micro": 30_000}])
    out = svc.register_snapshot({"snapshot": _snap(), "prices": [price]})
    pid = out["price_snapshot_ids"][0]
    detail = svc.price_snapshot(TA, pid)
    # 最坏情况 10 秒落在第二档：10 × 30000 = 300000
    assert detail["quote_upper_bound_micro"] == 300_000


def test_price_rounds_up_to_currency_minor():
    svc = _svc()
    _seed(svc)
    out = svc.register_snapshot({"snapshot": _snap(durations=(1,)), "prices": [
        _price({"request": 0, "generated_second": 10_000}, tiers=[
            {"dimension": "duration_s", "from_inclusive": "0", "to_exclusive": None,
             "unit_price_micro": 10_001}])]})
    detail = svc.price_snapshot(TA, out["price_snapshot_ids"][0])
    # 10001 微向上取整到美分（1 美分 = 10000 微）是 20000，只许偏高
    assert detail["quote_upper_bound_micro"] == 20_000


def test_not_quotable_price_has_no_bound():
    svc = _svc()
    _seed(svc)
    price = _price()
    price["quotable"] = False
    out = svc.register_snapshot({"snapshot": _snap(), "prices": [price]})
    detail = svc.price_snapshot(TA, out["price_snapshot_ids"][0])
    assert detail["quote_upper_bound_micro"] is None
    assert detail["unbounded_units"] == ["quotable=false"]


def test_price_without_request_or_gpu_unit_is_rejected():
    svc = _svc()
    _seed(svc)
    with pytest.raises(ServiceError) as exc:
        svc.register_snapshot({"snapshot": _snap(),
                               "prices": [_price({"generated_second": 10_000})]})
    assert exc.value.status == 400


def test_cross_tenant_price_is_denied():
    svc = _svc()
    _seed(svc)
    out = svc.register_snapshot({"snapshot": {**_snap(), "tenant_id": TB},
                                 "prices": [_price()]})
    with pytest.raises(ServiceError) as exc:
        svc.price_snapshot(TA, out["price_snapshot_ids"][0])
    assert (exc.value.status, exc.value.code) == (403, "CROSS_TENANT_DENIED")


def test_failure_reserve_covers_full_charge():
    svc = _svc()
    _seed(svc)
    price = _price()
    price["failure_charge"] = "full"
    out = svc.register_snapshot({"snapshot": _snap(), "prices": [price]})
    detail = svc.price_snapshot(TA, out["price_snapshot_ids"][0])
    # 10s × 30000 = 300000 的基础费 + 同额失败储备
    assert detail["quote_upper_bound_micro"] == 600_000


# -- 事件 ------------------------------------------------------------------

def _event_payloads(svc) -> list[dict]:
    rows = svc.conn.execute("SELECT payload FROM outbox ORDER BY rowid").fetchall()
    return [json.loads(r["payload"]) for r in rows]


def test_published_event_matches_the_contract_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(EVENT_SCHEMA.read_text(encoding="utf-8"))
    svc = _svc()
    _seed(svc)
    svc.register_snapshot({"snapshot": _snap(), "prices": [_price()]})
    payloads = _event_payloads(svc)
    assert payloads, "expected at least one outbox row"
    for envelope in payloads:
        jsonschema.validate(envelope["data"], schema)


def test_event_aggregate_is_provider_channel_model_snapshot():
    svc = _svc()
    _seed(svc)
    svc.register_snapshot({"snapshot": _snap()})
    envelope = _event_payloads(svc)[0]
    assert envelope["aggregate_id"] == "seedance/v1/seedance-1.0/snap-250528"
    assert envelope["data"]["change_kind"] == "snapshot_published"


def test_lifecycle_change_emits_its_own_event():
    svc = _svc()
    _seed(svc)
    out = svc.register_snapshot({"snapshot": _snap()})
    svc.transition(TA, {"snapshot_id": out["snapshot_id"], "to_state": "degraded",
                        "reason": "supplier incident"})
    kinds = [e["data"]["change_kind"] for e in _event_payloads(svc)]
    assert kinds == ["snapshot_published", "lifecycle_changed"]


def test_pool_health_does_not_emit_an_unlocatable_event():
    svc = _svc()
    _seed(svc)
    before = len(_event_payloads(svc))
    svc.set_pool_health("pool_a", "suspect", "5 consecutive 500s")
    assert len(_event_payloads(svc)) == before


# -- 渠道与池 --------------------------------------------------------------

def test_byok_pool_cannot_belong_to_everyone():
    svc = _svc()
    svc.register_channel({"provider": "p", "channel": "c", "connector_deployment": "d",
                          "egress_identity": "e", "credential_ref": "s://k"})
    with pytest.raises(ServiceError) as exc:
        svc.register_pool({"pool_id": "x", "tenant_id": "*", "provider": "p",
                           "channel": "c", "region": "r", "scope": "tenant_byok",
                           "credential_ref": "s://t", "data_region": "r",
                           "max_inflight": 1, "rpm": 1})
    assert exc.value.code == "CROSS_TENANT_DENIED"


def test_pool_requires_an_existing_channel():
    svc = _svc()
    with pytest.raises(ServiceError) as exc:
        svc.register_pool({"pool_id": "x", "tenant_id": "*", "provider": "p",
                           "channel": "c", "region": "r", "scope": "shared",
                           "data_region": "r", "max_inflight": 1, "rpm": 1})
    assert exc.value.code == "CHANNEL_NOT_FOUND"


def test_retired_channel_needs_a_reason():
    svc = _svc()
    with pytest.raises(ServiceError) as exc:
        svc.register_channel({"provider": "p", "channel": "c",
                              "connector_deployment": "d", "egress_identity": "e",
                              "credential_ref": "s://k", "lifecycle": "retired"})
    assert exc.value.status == 400


# -- HTTP ------------------------------------------------------------------

def test_http_idempotent_replay_and_conflict():
    client, service = _app()
    _seed(service)
    body = {"snapshot": _snap()}
    first = client.post("/capability-snapshots", json=body,
                        headers={"Idempotency-Key": "k1", "X-Tenant-Id": TA})
    assert first.status_code == 200, first.text
    replay = client.post("/capability-snapshots", json=body,
                         headers={"Idempotency-Key": "k1", "X-Tenant-Id": TA})
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    conflict = client.post("/capability-snapshots",
                           json={"snapshot": _snap(durations=(5,))},
                           headers={"Idempotency-Key": "k1", "X-Tenant-Id": TA})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_http_rejects_body_tenant_override():
    client, _service = _app()
    resp = client.post("/capability-snapshots",
                       json={"snapshot": _snap(), "tenant_id": TB},
                       headers={"Idempotency-Key": "k2", "X-Tenant-Id": TA})
    assert resp.status_code == 403
    assert resp.json()["code"] == "TENANT_MISMATCH"


def test_http_candidates_requires_projection():
    client = _app()[0]
    resp = client.get("/model-candidates",
                      params={"tenant_id": "t_nobody"},
                      headers={"X-Tenant-Id": "t_nobody"})
    assert resp.status_code == 503
    assert resp.json()["code"] == "AUTH_DEPENDENCY_UNAVAILABLE"


def test_http_capability_checks_wrapper():
    client = _app()[0]
    resp = client.post("/capability-checks",
                       json={"snapshot_id": "0" * 64, "mode": "image_to_video"},
                       headers={"Idempotency-Key": "k3", "X-Tenant-Id": TA})
    assert resp.status_code == 404
    assert resp.json()["code"] == "SNAPSHOT_NOT_FOUND"
