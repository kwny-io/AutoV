"""S15 单测。编号对应 `plans/S15-voice-assets.md` §5.4 的错误码表与 §9 的
验收矩阵（F1–F18 / E28–E31 / E39）。测试按方案散文写，不按实现写。

上游两个依赖全部是 stub，**按真实上游契约写**：

- S28 `GET /artifacts/{id}/revisions/{rev}` 返回体里**没有** `probe_ok` 也
  **没有** `source_kind`（`artifact_revision.probe_ok` 列存在但不被读取路径
  发布；`source_kind` 只在 `artifact_import` 上）。所以这里的默认 stub 就是
  "答得出 404 / 答不出扫描状态"，而**不是**伪造一个带 `probe_ok` 的响应。
  带 `probe_ok` 的 stub 只在显式 `scanned=True` 时出现，用来证明"如果上游
  将来补了这两个字段，内核能正确用它"。
- S01 `GET /authorization-context` 返回 `{permissions[], denied[]}`。

三条硬约束各自都有专门的用例，写成"行为"而不是"字段存在"：

1. 静默不是授权 —— 删掉 `voice_authorization` 行后资格检查必须拒。
2. 撤销后 ingest 过的资产也拒 —— 先注册成功，再撤销，再 `check-eligibility`。
3. 到期在读时判定 —— 直接改列到过去，不跑 sweep，资格检查必须拒。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import (
    CommandConflict,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from voice_assets.schema import SCHEMA
from voice_assets.service import (
    AUTHORIZATION_PERMISSION,
    CONTENT_PERMISSION,
    DENY_REASONS,
    ERROR_CODES,
    ServiceError,
    VoiceAssetService,
    effective_state,
    revision_content_hash,
    use_is_authorized,
)
from voice_assets.deps import REGISTERED_PERMISSIONS


# 服务把外部时间统一成定宽毫秒 UTC 形态（`_norm_time`），所以这里的常量
# 也写成同一形态——否则断言比的是"格式"，不是"时刻"。
FUTURE = "2099-01-01T00:00:00.000Z"
PAST = "2000-01-01T00:00:00.000Z"
RIGHTS = {"service_id": "S02", "id": "rights_1", "revision": 2,
          "content_hash": "c" * 64}


def _iso(delta_days: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(days=delta_days)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ------------------------------------------------------------------ 上游形状

def scan_ok(hash_="d" * 64):
    """S28 **补齐后**的响应形状（当前实现不发布后两个字段，见 S15-2）。"""
    return {"found": True, "content_hash": hash_, "probe_ok": True,
            "source_kind": "upload_session"}


def _registry_faithful_authorizer(permissions=None):
    """与 `deps.build_authorizer` 的**真实判据**一致：先查注册表，未注册即 503。

    `permissions=None` = 授予已注册的键（正常路径）；`[]` = 不授予任何键。
    """
    granted = ([AUTHORIZATION_PERMISSION] if permissions is None
               else list(permissions))

    def _authorize(tenant_id, principal_id, scope_kind, scope_id, permission):
        if permission not in REGISTERED_PERMISSIONS:
            raise ServiceError(
                503, "PERMISSION_KEY_UNREGISTERED",
                f"permission {permission!r} is not registered (S15-8)")
        return {"permissions": [p for p in granted if p == permission],
                "denied": [], "allowed": permission in granted,
                "permission": permission}
    return _authorize


class Harness:
    """内存库 + 两个上游 stub + 与 `api/app.py` 同形的幂等外壳。"""

    def __init__(self, conn=None, **over):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)

        self.scans = over.get("scans", {})
        self.scan_default = over.get("scan_default", None)
        self.permissions = over.get("permissions", [AUTHORIZATION_PERMISSION])
        self.denied = over.get("denied", [])
        self.calls = {"scan": 0, "authorize": 0, "authorize_purposes": []}

        self.service = VoiceAssetService(
            self.conn,
            artifact_scanner=self._scan,
            authorizer=over.get("authorizer") or self._authorize,
        )

    # ---- stubs：签名与 `deps.build_*` 一致 ----
    def _scan(self, ref, tenant_id):
        self.calls["scan"] += 1
        if isinstance(self.scan_default, Exception):
            raise self.scan_default
        key = (ref.get("id") or ref.get("artifact_id"), ref.get("revision"))
        if key in self.scans:
            value = self.scans[key]
            if isinstance(value, Exception):
                raise value
            return value
        return self.scan_default

    def _authorize(self, tenant_id, principal_id, scope_kind, scope_id,
                   permission):
        self.calls["authorize"] += 1
        self.calls["authorize_purposes"].append(permission)
        granted = permission in self.permissions
        denied = permission in self.denied
        return {"permissions": list(self.permissions),
                "denied": list(self.denied),
                "allowed": granted and not denied,
                "permission": permission}

    # ---- 幂等外壳：与 `api/app.py` 的 `command()` 同语义 ----
    def command(self, body, tenant_id="t1", command_id="c1", execute=None):
        payload = {**body, "command_id": command_id}
        record = self.idem.begin(tenant_id, command_id,
                                 canonical_request_hash(payload))
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = execute(payload)
        except Exception:
            self.idem.abort(tenant_id, command_id)
            raise
        self.idem.finish(tenant_id, command_id,
                         json.dumps(result, ensure_ascii=False, default=str))
        return {**result, "replayed": False}

    # ---- 命令体 ----
    def body(self, **over):
        base = {
            "project_id": "prj_1",
            "series_scope": "ser_1",
            "display_name": "旁白甲",
            "voice_kind": "cloned",
            "rights_ref": dict(RIGHTS),
            "authorization": {
                "authorized_uses": ["modality:speech", "series:ser_1"],
                "expires_at": FUTURE,
                "scope_notes": "episodes 1-12 only",
            },
            "language_profile": {"primary": "zh-CN"},
            "emotion_profile": {"neutral": "ok"},
            "rate_profile": {"baseline_wpm": 260},
            "voice_sample_refs": [],
            "home_cell": "cell_a",
        }
        base.update(over)
        return base

    def register(self, tenant_id="t1", command_id="c1", **over):
        body = self.body(**over)
        return self.command(
            body, tenant_id=tenant_id, command_id=command_id,
            execute=lambda payload: self.service.register_voice(
                tenant_id, command_id, payload))

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
                    "SELECT payload FROM outbox ORDER BY created_at, sequence"
                ).fetchall()]


def _http_client(monkeypatch, tmp_path, **over):
    """与 `api/app.py` 同构的 TestClient。DB 落临时文件，不共享内存库。"""
    monkeypatch.setenv("S15_DB", str(tmp_path / "s15.db"))
    monkeypatch.delenv("S28_BASE_URL", raising=False)
    monkeypatch.delenv("S01_BASE_URL", raising=False)
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s15_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s15_api_app"] = module
    harness = Harness(conn=module._conn, **over)
    module.service = harness.service
    module._idem = harness.idem
    return TestClient(module.app), harness


# ------------------------------------------------------------------ 纯函数

def test_effective_state_judges_expiry_at_read_time():
    """§4.2 第 6 条：列里还是 granted，但 expires_at 已过 → expired。"""
    assert effective_state("granted", PAST, _iso(0)) == "expired"
    assert effective_state("granted", FUTURE, _iso(0)) == "granted"
    # 撤销与暂停不受到期影响：安全方向的状态不被时间覆盖。
    assert effective_state("revoked", PAST, _iso(0)) == "revoked"
    assert effective_state("suspended", PAST, _iso(0)) == "suspended"


def test_use_matcher_requires_modality_and_named_dimensions():
    uses = ["modality:speech", "series:ser_1", "provider:volcengine"]
    assert use_is_authorized({"modality": "speech", "series_id": "ser_1",
                              "provider_id": "volcengine"}, uses)
    # 缺 modality：无法解释用途 → 拒（不 fail-open）。
    assert not use_is_authorized({"series_id": "ser_1"}, uses)
    # 有 provider 约束但请求没说 provider → 不命中。
    assert not use_is_authorized({"modality": "speech", "series_id": "ser_1"},
                                 uses)
    # series 越界。
    assert not use_is_authorized({"modality": "speech", "series_id": "ser_9",
                                  "provider_id": "volcengine"}, uses)
    # 空列表 = 没声明用途。
    assert not use_is_authorized({"modality": "speech"}, [])


def test_revision_hash_ignores_accept_metadata():
    content = {"language_profile": {"a": 1}, "rate_profile": {"b": 2},
               "voice_sample_refs": [], "rights_ref": RIGHTS,
               "source_kind": "cloned", "emotion_profile": {},
               "clone_artifact_ref": None, "quality_gate_ref": {},
               "payload": {}}
    first = revision_content_hash(content)
    assert first == revision_content_hash(dict(content))
    # 内容变了就变。
    assert first != revision_content_hash({**content,
                                           "rate_profile": {"b": 3}})


def test_contract_gap_and_error_tables_are_consistent():
    assert DENY_REASONS
    # 每个错误码都在表里且只属于一个状态码。
    seen = [code for codes in ERROR_CODES.values() for code in codes]
    assert len(seen) == len(set(seen))
    assert "MALFORMED_REQUEST" in ERROR_CODES[400]
    assert "TENANT_SCOPE_DENIED" in ERROR_CODES[403]
    assert "VERSION_CONFLICT" in ERROR_CODES[409]
    assert "REVOCATION_TERMINAL" in ERROR_CODES[422]
    assert "DEPENDENCY_UNAVAILABLE" in ERROR_CODES[503]


# ------------------------------------------------------------------ 注册

def test_register_writes_voice_revision_authorization_event_and_outbox():
    h = Harness()
    result = h.register()

    assert result["replayed"] is False
    voice_id = result["voice_id"]
    assert voice_id.startswith("vox_")
    assert result["current_revision"] == 1
    assert result["canonical_revision"] == 1
    assert result["authorization"]["state"] == "granted"
    assert result["authorization"]["state_seq"] == 1

    assert h.count("voice") == 1
    assert h.count("voice_revision") == 1
    assert h.count("canonical_pointer") == 1
    assert h.count("voice_authorization") == 1
    assert h.count("authorization_event") == 1

    pointer = h.rows("canonical_pointer")[0]
    assert pointer["decision_kind"] == "initial_import"
    assert pointer["superseded_at"] is None

    events = h.events()
    assert len(events) == 1
    data = events[0]["data"]
    assert events[0]["type"] == "asset.voice.authorization.changed"
    assert events[0]["aggregate_id"] == voice_id
    # VAC-1：aggregate_sequence = state_seq（不是 revision）。
    assert events[0]["sequence"] == 1
    assert data["state_seq"] == 1
    assert data["from_state"] is None
    assert data["to_state"] == "granted"
    assert data["reason_code"] == "INITIAL_GRANT"
    assert data["authorized_uses_before"] == []
    assert data["expires_at"] == FUTURE
    # 契约 `additionalProperties: false`：一个多余字段都不许有。
    assert set(data) == {
        "schema_version", "voice_id", "from_state", "to_state", "state_seq",
        "reason_code", "effective_at", "evidence_ref",
        "authorized_uses_before", "expires_at"}
    assert events[0]["subject"] == f"voice/{voice_id}"


def test_register_requires_project_id_though_plan_omits_it():
    """S15-1：§4.1 的请求体没有 project_id，而列是 NOT NULL。"""
    h = Harness()
    body = h.body()
    body.pop("project_id")
    with pytest.raises(ServiceError) as caught:
        h.command(body, execute=lambda p: h.service.register_voice("t1", "c1", p))
    assert caught.value.status == 400
    assert caught.value.code == "MALFORMED_REQUEST"
    assert h.count("voice") == 0


def test_register_rejects_empty_use_scope():
    """F10 / USE_SCOPE_REQUIRED：空用途无法审计。"""
    h = Harness()
    with pytest.raises(ServiceError) as caught:
        h.register(authorization={"authorized_uses": [], "expires_at": FUTURE})
    assert caught.value.code == "USE_SCOPE_REQUIRED"
    assert caught.value.status == 422
    assert h.count("voice") == 0


def test_register_rejects_cloned_without_expiry():
    """F9 / AUTHORIZATION_EXPIRY_REQUIRED：克隆音色必须有期限。"""
    h = Harness()
    with pytest.raises(ServiceError) as caught:
        h.register(authorization={"authorized_uses": ["modality:speech"]})
    assert caught.value.code == "AUTHORIZATION_EXPIRY_REQUIRED"
    assert caught.value.status == 422
    assert h.count("voice") == 0


def test_register_allows_platform_preset_without_expiry():
    h = Harness()
    result = h.register(voice_kind="platform_preset",
                        authorization={"authorized_uses": ["modality:speech"]})
    assert result["voice_id"].startswith("vox_")
    assert result["authorization"]["expires_at"] is None


def test_register_refuses_when_sample_scan_state_is_unknown():
    """S15-2 / E29：S28 的读接口不发布 probe_ok / source_kind。

    这是**最重要的一条 fail-closed**：声纹样本是生物特征，问不到扫描状态时
    注册必须 503，不能当成"没问题"。默认 stub 返回 `None`（当前真实形状）。
    """
    h = Harness(scan_default=None)
    with pytest.raises(ServiceError) as caught:
        h.register(voice_sample_refs=[{"id": "art_1", "revision": 1}])
    assert caught.value.status == 503
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
    assert caught.value.retryable is True
    assert h.count("voice") == 0
    assert h.count("voice_authorization") == 0
    assert h.events() == []


def test_register_refuses_when_scanner_returns_non_dict():
    h = Harness(scan_default=True)
    with pytest.raises(ServiceError) as caught:
        h.register(voice_sample_refs=[{"id": "art_1", "revision": 1}])
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"


def test_register_rejects_sample_that_failed_scan():
    h = Harness(scans={("art_1", 1): {**scan_ok(), "probe_ok": False}})
    with pytest.raises(ServiceError) as caught:
        h.register(voice_sample_refs=[{"id": "art_1", "revision": 1}])
    assert caught.value.code == "SAMPLE_NOT_SCANNED"
    assert caught.value.status == 422


def test_register_rejects_sample_not_from_isolated_importer():
    h = Harness(scans={("art_1", 1): {**scan_ok(),
                                      "source_kind": "provider_url"}})
    with pytest.raises(ServiceError) as caught:
        h.register(voice_sample_refs=[{"id": "art_1", "revision": 1}])
    assert caught.value.code == "SAMPLE_NOT_SCANNED"


def test_register_rejects_missing_artifact():
    h = Harness(scan_default={"found": False, "content_hash": None,
                              "probe_ok": None, "source_kind": None})
    with pytest.raises(ServiceError) as caught:
        h.register(voice_sample_refs=[{"id": "art_9", "revision": 1}])
    assert caught.value.code == "ARTIFACT_NOT_FOUND"
    assert caught.value.status == 422


def test_register_rejects_hash_mismatch_with_s28():
    """F17：登记的期望 hash 与 S28 读数不一致。"""
    h = Harness(scans={("art_1", 1): scan_ok("e" * 64)})
    with pytest.raises(ServiceError) as caught:
        h.register(voice_sample_refs=[{"id": "art_1", "revision": 1,
                                       "content_hash": "d" * 64}])
    assert caught.value.code == "ARTIFACT_HASH_MISMATCH"
    assert caught.value.status == 422


def test_register_accepts_fully_verified_sample():
    h = Harness(scans={("art_1", 1): scan_ok()})
    result = h.register(voice_sample_refs=[{"id": "art_1", "revision": 1,
                                            "content_hash": "d" * 64}])
    revision = h.service.get_revision("t1", result["voice_id"], 1)
    assert revision["voice_sample_refs"] == [
        {"id": "art_1", "revision": 1, "content_hash": "d" * 64}]


def test_register_rejects_duplicate_display_name_in_series_scope():
    h = Harness()
    h.register()
    with pytest.raises(ServiceError) as caught:
        h.register(command_id="c2")
    assert caught.value.status == 409
    assert caught.value.code == "NAME_CONFLICT"
    assert h.count("voice") == 1


def test_register_allows_same_name_in_other_series_scope():
    h = Harness()
    h.register()
    # 内容必须不同：`vr_hash_uniq` 是 `(tenant_id, content_hash)`，见 S15-10。
    h.register(command_id="c2", series_scope="ser_2",
               rate_profile={"baseline_wpm": 300})
    assert h.count("voice") == 2


def test_register_is_tenant_isolated():
    h = Harness()
    first = h.register(tenant_id="t1")
    second = h.register(tenant_id="t2")
    assert first["voice_id"] != second["voice_id"]
    # 另一租户看不到。
    with pytest.raises(ServiceError) as caught:
        h.service.get_voice("t2", first["voice_id"])
    assert caught.value.status == 404


# ------------------------------------------------------------------ 资格检查

def test_eligibility_happy_path_returns_mapping():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"voice_revision": 1, "provider_id": "volcengine",
               "channel_id": "default", "model_snapshot": "250528",
               "region": "cn-beijing", "provider_voice_id": "zh-CN-Xiaoxiao",
               "regression": {"passed": True, "similarity_score": 0.94}},
              command_id="map1",
              execute=lambda p: h.service.add_provider_mapping(
                  "t1", voice_id, "map1", p))

    result = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech",
                          "provider_id": "volcengine"},
    })
    assert result["eligible"] is True
    assert result["deny_reason"] is None
    assert result["authorization_state"] == "granted"
    assert result["state_seq"] == 1
    assert result["provider_mapping"]["provider_voice_id"] == "zh-CN-Xiaoxiao"
    assert result["provider_mapping"]["regression_passed"] is True
    assert "detail" not in result


def test_eligibility_missing_authorization_refuses_not_404():
    """静默不是授权：删掉授权行后必须拒，而且是 200 + eligible=false。"""
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.conn.execute("DELETE FROM voice_authorization WHERE voice_id = ?",
                   (voice_id,))
    result = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert result["eligible"] is False
    assert result["deny_reason"] == "AUTHORIZATION_MISSING"
    assert result["authorization_state"] is None
    assert result["provider_mapping"] is None


def test_eligibility_after_revoke_refuses_for_already_ingested_asset():
    """consent 可以在 ingest 之后撤回：撤销后立刻再检查必须拒。"""
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"voice_revision": 1, "provider_id": "volcengine",
               "channel_id": "default", "model_snapshot": "250528",
               "region": "cn-beijing", "provider_voice_id": "pv",
               "regression": {"passed": True}}, command_id="map1",
              execute=lambda p: h.service.add_provider_mapping(
                  "t1", voice_id, "map1", p))
    before = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert before["eligible"] is True

    h.command({"evidence_ref": {"id": "letter_1", "revision": 1},
               "reason_code": "RIGHTS_WITHDRAWN"}, command_id="rev1",
              execute=lambda p: h.service.revoke("t1", voice_id, "rev1", p))

    after = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert after["eligible"] is False
    assert after["deny_reason"] == "AUTHORIZATION_REVOKED"
    assert after["authorization_state"] == "revoked"
    assert after["state_seq"] == 2


def test_eligibility_expired_is_judged_at_read_time_without_sweep():
    """§4.2 第 6 条：不跑 sweep，列里还是 granted，也要按过期拒。"""
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.conn.execute(
        "UPDATE voice_authorization SET expires_at = ? WHERE voice_id = ?",
        (PAST, voice_id))
    assert h.rows("voice_authorization")[0]["state"] == "granted"
    result = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert result["eligible"] is False
    assert result["deny_reason"] == "AUTHORIZATION_EXPIRED"
    assert result["authorization_state"] == "expired"


def test_eligibility_suspended_refuses():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"reason_code": "SUPPLIER_UNAVAILABLE"}, command_id="s1",
              execute=lambda p: h.service.suspend("t1", voice_id, "s1", p))
    result = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert result["deny_reason"] == "AUTHORIZATION_SUSPENDED"


def test_eligibility_use_mismatch_refuses():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"voice_revision": 1, "provider_id": "volcengine",
               "channel_id": "default", "model_snapshot": "250528",
               "region": "cn-beijing", "provider_voice_id": "pv",
               "regression": {"passed": True}}, command_id="map1",
              execute=lambda p: h.service.add_provider_mapping(
                  "t1", voice_id, "map1", p))
    result = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_other", "modality": "speech"}})
    assert result["deny_reason"] == "USE_NOT_AUTHORIZED"


def test_eligibility_stale_state_seq_refuses():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"reason_code": "REVIEW_HOLD"}, command_id="s1",
              execute=lambda p: h.service.suspend("t1", voice_id, "s1", p))
    h.command({"reason_code": "REVIEW_CLEARED"}, command_id="r1",
              execute=lambda p: h.service.resume("t1", voice_id, "r1", p))
    result = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1, "if_state_seq": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert result["deny_reason"] == "STATE_SEQ_STALE"
    assert result["state_seq"] == 3


def test_eligibility_no_mapping_never_returns_eligible_true():
    h = Harness()
    voice_id = h.register()["voice_id"]
    result = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert result["eligible"] is False
    assert result["deny_reason"] == "MAPPING_UNAVAILABLE"
    assert result["provider_mapping"] is None


def test_eligibility_regression_failed_is_distinct_from_no_mapping():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"voice_revision": 1, "provider_id": "volcengine",
               "channel_id": "default", "model_snapshot": "250528",
               "region": "cn-beijing", "provider_voice_id": "pv",
               "regression": {"passed": False}}, command_id="map1",
              execute=lambda p: h.service.add_provider_mapping(
                  "t1", voice_id, "map1", p))
    result = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert result["deny_reason"] == "REGRESSION_FAILED"


def test_eligibility_region_ambiguous_refuses_instead_of_guessing():
    h = Harness()
    voice_id = h.register()["voice_id"]
    for index, region in enumerate(("cn-beijing", "cn-shanghai"), start=1):
        h.command({"voice_revision": 1, "provider_id": "volcengine",
                   "channel_id": "default", "model_snapshot": "250528",
                   "region": region, "provider_voice_id": f"pv_{index}",
                   "regression": {"passed": True}},
                  command_id=f"map{index}",
                  execute=lambda p, i=index: h.service.add_provider_mapping(
                      "t1", voice_id, f"map{i}", p))
    result = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert result["deny_reason"] == "MAPPING_REGION_AMBIGUOUS"
    # 指定 region 就能拿到确定答案。
    exact = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech",
                          "region": "cn-shanghai"}})
    assert exact["eligible"] is True
    assert exact["provider_mapping"]["region"] == "cn-shanghai"


def test_eligibility_unavailable_mapping_refuses():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"voice_revision": 1, "provider_id": "volcengine",
               "channel_id": "default", "model_snapshot": "250528",
               "region": "cn-beijing", "provider_voice_id": "pv",
               "regression": {"passed": True}}, command_id="map1",
              execute=lambda p: h.service.add_provider_mapping(
                  "t1", voice_id, "map1", p))
    h.command({"reason": "supplier outage"}, command_id="u1",
              execute=lambda p: h.service.change_mapping_status(
                  "t1", voice_id, "u1", "1~volcengine~default~250528~cn-beijing",
                  "unavailable", p))
    result = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert result["deny_reason"] == "MAPPING_UNAVAILABLE"


def test_eligibility_cross_tenant_is_404():
    h = Harness()
    voice_id = h.register(tenant_id="t1")["voice_id"]
    with pytest.raises(ServiceError) as caught:
        h.service.check_eligibility("t2", voice_id, {
            "voice_revision": 1,
            "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert caught.value.status == 404
    assert caught.value.code == "NOT_FOUND"


def test_eligibility_requires_modality():
    h = Harness()
    voice_id = h.register()["voice_id"]
    with pytest.raises(ServiceError) as caught:
        h.service.check_eligibility("t1", voice_id, {
            "voice_revision": 1, "requested_use": {"series_id": "ser_1"}})
    assert caught.value.status == 400
    assert caught.value.code == "MALFORMED_REQUEST"


def test_eligibility_unknown_revision_is_404():
    h = Harness()
    voice_id = h.register()["voice_id"]
    with pytest.raises(ServiceError) as caught:
        h.service.check_eligibility("t1", voice_id, {
            "voice_revision": 9,
            "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert caught.value.status == 404


def test_eligibility_tombstoned_voice_refuses():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.conn.execute("UPDATE voice SET status = 'tombstoned' WHERE voice_id = ?",
                   (voice_id,))
    result = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert result["deny_reason"] == "VOICE_TOMBSTONED"


def test_eligibility_never_writes_idempotency_or_outbox():
    """只读语义：没有权限、没有事件、不调 S01。"""
    h = Harness()
    voice_id = h.register()["voice_id"]
    before_events = h.count("outbox")
    before_auth_calls = h.calls["authorize"]
    h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    assert h.count("outbox") == before_events
    assert h.calls["authorize"] == before_auth_calls


# ------------------------------------------------------------------ 状态机

def test_revoke_requires_evidence_ref():
    h = Harness()
    voice_id = h.register()["voice_id"]
    with pytest.raises(ServiceError) as caught:
        h.command({"reason_code": "RIGHTS_WITHDRAWN"}, command_id="rev1",
                  execute=lambda p: h.service.revoke("t1", voice_id, "rev1", p))
    assert caught.value.code == "REVOCATION_EVIDENCE_REQUIRED"
    assert caught.value.status == 422
    assert h.rows("voice_authorization")[0]["state"] == "granted"


def test_revoke_is_terminal_for_every_later_transition():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"evidence_ref": {"id": "letter_1"}, "reason_code":
               "RIGHTS_WITHDRAWN"}, command_id="rev1",
              execute=lambda p: h.service.revoke("t1", voice_id, "rev1", p))
    with pytest.raises(ServiceError) as caught:
        h.command({"reason_code": "RENEWED", "expires_at": FUTURE},
                  command_id="rn1",
                  execute=lambda p: h.service.renew("t1", voice_id, "rn1", p))
    assert caught.value.code == "REVOCATION_TERMINAL"
    assert caught.value.status == 422


def test_repeated_revoke_is_409_already_revoked():
    h = Harness()
    voice_id = h.register()["voice_id"]
    body = {"evidence_ref": {"id": "letter_1"},
            "reason_code": "RIGHTS_WITHDRAWN"}
    h.command(body, command_id="rev1",
              execute=lambda p: h.service.revoke("t1", voice_id, "rev1", p))
    with pytest.raises(ServiceError) as caught:
        h.command(body, command_id="rev2",
                  execute=lambda p: h.service.revoke("t1", voice_id, "rev2", p))
    assert caught.value.code == "ALREADY_REVOKED"
    assert caught.value.status == 409


def test_db_trigger_is_last_line_of_defense_against_revoked_resurrection():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"evidence_ref": {"id": "letter_1"},
               "reason_code": "RIGHTS_WITHDRAWN"}, command_id="rev1",
              execute=lambda p: h.service.revoke("t1", voice_id, "rev1", p))
    with pytest.raises(sqlite3.IntegrityError) as caught:
        h.conn.execute(
            "UPDATE voice_authorization SET state = 'granted'"
            " WHERE tenant_id = 't1' AND voice_id = ?", (voice_id,))
    assert "revoked is terminal" in str(caught.value)


def test_suspend_then_resume_keeps_expiry():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"reason_code": "REVIEW_HOLD"}, command_id="s1",
              execute=lambda p: h.service.suspend("t1", voice_id, "s1", p))
    assert h.rows("voice_authorization")[0]["state"] == "suspended"
    resumed = h.command({"reason_code": "REVIEW_CLEARED"}, command_id="r1",
                        execute=lambda p: h.service.resume(
                            "t1", voice_id, "r1", p))
    assert resumed["state"] == "granted"
    assert resumed["expires_at"] == FUTURE
    assert resumed["state_seq"] == 3


def test_resume_from_granted_is_invalid():
    h = Harness()
    voice_id = h.register()["voice_id"]
    with pytest.raises(ServiceError) as caught:
        h.command({"reason_code": "REVIEW_CLEARED"}, command_id="r1",
                  execute=lambda p: h.service.resume("t1", voice_id, "r1", p))
    assert caught.value.code == "STATE_TRANSITION_INVALID"
    assert caught.value.status == 409


def test_renew_moves_expired_back_to_granted_with_new_expiry():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.conn.execute(
        "UPDATE voice_authorization SET expires_at = ? WHERE voice_id = ?",
        (PAST, voice_id))
    result = h.command({"expires_at": FUTURE, "reason_code": "RENEWED"},
                       command_id="rn1",
                       execute=lambda p: h.service.renew(
                           "t1", voice_id, "rn1", p))
    assert result["state"] == "granted"
    assert result["expires_at"] == FUTURE
    assert result["state_seq"] == 2
    eligible = h.service.check_eligibility("t1", voice_id, {
        "voice_revision": 1,
        "requested_use": {"series_id": "ser_1", "modality": "speech"}})
    # 没有映射，但授权这一层已经恢复。
    assert eligible["deny_reason"] == "MAPPING_UNAVAILABLE"


def test_renew_requires_future_expiry():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.conn.execute(
        "UPDATE voice_authorization SET expires_at = ? WHERE voice_id = ?",
        (PAST, voice_id))
    with pytest.raises(ServiceError) as caught:
        h.command({"expires_at": PAST}, command_id="rn1",
                  execute=lambda p: h.service.renew(
                      "t1", voice_id, "rn1", p))
    assert caught.value.status == 400


def test_expire_sweep_only_touches_expired_rows_and_emits_events():
    h = Harness()
    expired_voice = h.register()["voice_id"]
    live_voice = h.register(command_id="c2", display_name="旁白乙",
                            rate_profile={"baseline_wpm": 300})["voice_id"]
    h.conn.execute(
        "UPDATE voice_authorization SET expires_at = ? WHERE voice_id = ?",
        (PAST, expired_voice))
    result = h.service.expire_sweep("t1")
    assert [item["voice_id"] for item in result["expired"]] == [expired_voice]
    states = {row["voice_id"]: row["state"]
              for row in h.rows("voice_authorization")}
    assert states[expired_voice] == "expired"
    assert states[live_voice] == "granted"
    emitted = [e["data"]["to_state"] for e in h.events()]
    assert emitted.count("expired") == 1


def test_state_changes_append_history_with_before_snapshots():
    """VAC-5：事件重放要能读到变更前的值，不是"现在"的值。"""
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"reason_code": "REVIEW_HOLD"}, command_id="s1",
              execute=lambda p: h.service.suspend("t1", voice_id, "s1", p))
    history = h.service.get_authorization("t1", voice_id)["history"]
    assert [row["seq"] for row in history] == [1, 2]
    assert history[1]["from_state"] == "granted"
    assert history[1]["to_state"] == "suspended"
    assert history[1]["authorized_uses_before"] == [
        "modality:speech", "series:ser_1"]
    assert history[1]["expires_at_before"] == FUTURE


def test_event_evidence_ref_has_exactly_three_keys():
    """VAC-6：契约是 3 键，rights_ref 是 4 键。库里保留 hash。"""
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"evidence_ref": {"service_id": "S02", "id": "letter_1",
                                "revision": 3, "content_hash": "f" * 64},
               "reason_code": "RIGHTS_WITHDRAWN"}, command_id="rev1",
              execute=lambda p: h.service.revoke("t1", voice_id, "rev1", p))
    event = [e for e in h.events() if e["data"]["to_state"] == "revoked"][0]
    assert set(event["data"]["evidence_ref"]) == {"service_id", "id",
                                                  "revision"}
    assert event["data"]["evidence_ref"]["id"] == "letter_1"
    # 库里的审计行仍是 4 键。
    audit = h.rows("authorization_event")[-1]
    assert json.loads(audit["evidence_ref"])["content_hash"] == "f" * 64


# ------------------------------------------------------------------ 授权（S01）

def test_write_command_is_denied_when_s01_says_no():
    h = Harness(permissions=[])
    with pytest.raises(ServiceError) as caught:
        h.register()
    assert caught.value.status == 403
    assert caught.value.code == "TENANT_SCOPE_DENIED"
    assert h.count("voice") == 0


def test_write_command_is_denied_when_s01_explicitly_denies():
    # deny 压过 allow：S01 的语义里显式拒绝不能被 permissions 里的同名项冲掉。
    h = Harness(permissions=[AUTHORIZATION_PERMISSION],
                denied=[AUTHORIZATION_PERMISSION])
    with pytest.raises(ServiceError) as caught:
        h.register()
    assert caught.value.code == "TENANT_SCOPE_DENIED"


def test_revoke_asks_for_authorization_permission():
    """撤销走的是授权键，且**每次都问**——不是"注册时问过就算了"。

    S15-8 说今天全服务只有一个 voice 键；这条测试钉住的是"撤销这条命令确实
    走授权门"，等 owner 拆出更细的键时，它仍然该问 `AUTHORIZATION_PERMISSION`。
    """
    h = Harness()
    voice_id = h.register(tenant_id="t1", command_id="c0")["voice_id"]
    h.calls["authorize_purposes"].clear()
    h.command({"evidence_ref": {"id": "l1"}, "reason_code": "RIGHTS_WITHDRAWN"},
              command_id="rev1",
              execute=lambda p: h.service.revoke("t1", voice_id, "rev1", p))
    assert h.calls["authorize_purposes"] == [AUTHORIZATION_PERMISSION]


def test_authorization_permission_is_the_registered_key():
    """S15-8 是**可核对的事实**，不是注释。

    注册表里与音色相关的键只有 `asset.voice.authorize`，本服务全部写命令都问
    它。这条测试直接读契约文件——如果哪天 owner 注册了更细的写键，它会红，
    从而迫使 `deps` 里的两个常量与缺口登记一起被重新审视。
    """
    registry = json.loads(
        (Path(__file__).resolve().parents[3] / "contracts" / "identity"
         / "permission-registry.v2.json").read_text(encoding="utf-8"))
    registered = {entry["key"] for entry in registry["permissions"]}
    voice_keys = {key for key in registered if "voice" in key}
    assert voice_keys == {AUTHORIZATION_PERMISSION}
    assert AUTHORIZATION_PERMISSION in REGISTERED_PERMISSIONS
    # 所有写命令的 purpose 都必须是这一个**已注册**的键：未注册的键在 S01 侧
    # 必然 503，那就等于服务不可用。
    assert CONTENT_PERMISSION == AUTHORIZATION_PERMISSION
    assert CONTENT_PERMISSION in registered


def test_missing_permission_refuses_without_writing():
    """没有任何 voice 权限的调用方，注册必须 403 且一行都不落。"""
    h = Harness(authorizer=_registry_faithful_authorizer(permissions=[]))
    with pytest.raises(ServiceError) as caught:
        h.register()
    assert caught.value.status == 403
    assert caught.value.code == "TENANT_SCOPE_DENIED"
    assert h.count("voice") == 0


def test_registered_permission_allows_the_write_through_real_deps_gate():
    """真实判据（先查注册表）下，拿到 `asset.voice.authorize` 就能注册。

    反过来证明：上一条的 403 是"没有权限"，不是"键没注册"——两件事在这里
    分开断言，免得把缺口当成不可用。
    """
    h = Harness(authorizer=_registry_faithful_authorizer())
    voice_id = h.register()["voice_id"]
    assert h.service.get_authorization("t1", voice_id)["state"] == "granted"


# ------------------------------------------------------------------ 幂等（HTTP）

def test_http_replay_returns_same_result_and_replayed_true(monkeypatch,
                                                           tmp_path):
    client, h = _http_client(monkeypatch, tmp_path)
    body = h.body()
    headers = {"Idempotency-Key": "cmd_1", "X-Tenant-Id": "t1",
               "X-Principal-Id": "u1"}
    first = client.post("/voices", json=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["replayed"] is False
    second = client.post("/voices", json=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["voice_id"] == first.json()["voice_id"]
    assert h.count("voice") == 1
    assert h.count("outbox") == 1


def test_http_same_key_different_body_is_409(monkeypatch, tmp_path):
    client, h = _http_client(monkeypatch, tmp_path)
    headers = {"Idempotency-Key": "cmd_1", "X-Tenant-Id": "t1"}
    first = client.post("/voices", json=h.body(), headers=headers)
    assert first.status_code == 200
    other = {**h.body(), "display_name": "旁白丙"}
    second = client.post("/voices", json=other, headers=headers)
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert h.count("voice") == 1


def test_http_cross_tenant_lookup_is_404(monkeypatch, tmp_path):
    client, h = _http_client(monkeypatch, tmp_path)
    created = client.post("/voices", json=h.body(),
                          headers={"Idempotency-Key": "c1",
                                   "X-Tenant-Id": "t1"}).json()
    response = client.get(f"/voices/{created['voice_id']}",
                          headers={"X-Tenant-Id": "t2"})
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_http_body_tenant_mismatch_is_403(monkeypatch, tmp_path):
    client, h = _http_client(monkeypatch, tmp_path)
    body = {**h.body(), "tenant_id": "t9"}
    response = client.post("/voices", json=body,
                           headers={"Idempotency-Key": "c1",
                                    "X-Tenant-Id": "t1"})
    assert response.status_code == 403
    assert response.json()["code"] == "TENANT_MISMATCH"


def test_http_check_eligibility_is_200_for_refusals(monkeypatch, tmp_path):
    """不可用不是请求错误：200 + eligible=false。"""
    client, h = _http_client(monkeypatch, tmp_path)
    created = client.post("/voices", json=h.body(),
                          headers={"Idempotency-Key": "c1",
                                   "X-Tenant-Id": "t1"}).json()
    voice_id = created["voice_id"]
    response = client.post(
        f"/voices/{voice_id}:check-eligibility",
        json={"voice_revision": 1,
              "requested_use": {"series_id": "ser_1", "modality": "speech"}},
        headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["eligible"] is False
    assert payload["deny_reason"] == "MAPPING_UNAVAILABLE"


def test_http_check_eligibility_after_revoke_refuses_without_idempotency(
        monkeypatch, tmp_path):
    client, h = _http_client(monkeypatch, tmp_path)
    voice_id = client.post("/voices", json=h.body(),
                           headers={"Idempotency-Key": "c1",
                                    "X-Tenant-Id": "t1"}).json()["voice_id"]
    client.post(f"/voices/{voice_id}:revoke",
                json={"evidence_ref": {"id": "letter_1"},
                      "reason_code": "RIGHTS_WITHDRAWN"},
                headers={"Idempotency-Key": "rev1", "X-Tenant-Id": "t1"})
    payload = {"voice_revision": 1,
               "requested_use": {"series_id": "ser_1",
                                 "modality": "speech"}}
    # 注册与撤销各写一条；资格检查一条都不该写。
    before = h.count("idempotency_record")
    for _ in range(2):
        response = client.post(f"/voices/{voice_id}:check-eligibility",
                               json=payload,
                               headers={"X-Tenant-Id": "t1"})
        assert response.status_code == 200
        assert response.json()["deny_reason"] == "AUTHORIZATION_REVOKED"
    assert h.count("idempotency_record") == before


def test_http_suspended_then_check_reads_latest_state(monkeypatch, tmp_path):
    """幂等只保护写命令；读路径每次都看最新状态。"""
    client, h = _http_client(monkeypatch, tmp_path)
    voice_id = client.post("/voices", json=h.body(),
                           headers={"Idempotency-Key": "c1",
                                    "X-Tenant-Id": "t1"}).json()["voice_id"]
    client.post(f"/voices/{voice_id}:suspend",
                json={"reason_code": "REVIEW_HOLD"},
                headers={"Idempotency-Key": "s1", "X-Tenant-Id": "t1"})
    response = client.post(
        f"/voices/{voice_id}:check-eligibility",
        json={"voice_revision": 1,
              "requested_use": {"series_id": "ser_1", "modality": "speech"}},
        headers={"X-Tenant-Id": "t1"})
    assert response.json()["deny_reason"] == "AUTHORIZATION_SUSPENDED"


def test_http_readiness_reports_contract_gaps(monkeypatch, tmp_path):
    client, _ = _http_client(monkeypatch, tmp_path)
    response = client.get("/health/readiness")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert {gap["id"] for gap in payload["contract_gaps"]} >= {"C15-1",
                                                               "VAC-1",
                                                               "S15-2"}


# ------------------------------------------------------------------ 映射

def test_mapping_duplicate_is_409_and_append_only():
    h = Harness()
    voice_id = h.register()["voice_id"]
    body = {"voice_revision": 1, "provider_id": "volcengine",
            "channel_id": "default", "model_snapshot": "250528",
            "region": "cn-beijing", "provider_voice_id": "pv",
            "regression": {"passed": True}}
    h.command(body, command_id="m1",
              execute=lambda p: h.service.add_provider_mapping(
                  "t1", voice_id, "m1", p))
    with pytest.raises(ServiceError) as caught:
        h.command({**body, "provider_voice_id": "pv_other"}, command_id="m2",
                  execute=lambda p: h.service.add_provider_mapping(
                      "t1", voice_id, "m2", p))
    assert caught.value.code == "MAPPING_EXISTS"
    assert caught.value.status == 409
    # provider_voice_id 没被改写。
    assert h.rows("provider_voice_mapping")[0]["provider_voice_id"] == "pv"


def test_mapping_with_failed_regression_is_stored_deprecated():
    """§4.5 第 2 条：passed=false 仍可存，但不可路由。"""
    h = Harness()
    voice_id = h.register()["voice_id"]
    result = h.command(
        {"voice_revision": 1, "provider_id": "volcengine",
         "channel_id": "default", "model_snapshot": "250528",
         "region": "cn-beijing", "provider_voice_id": "pv",
         "regression": {"passed": False}}, command_id="m1",
        execute=lambda p: h.service.add_provider_mapping(
            "t1", voice_id, "m1", p))
    assert result["mapping_status"] == "deprecated"


def test_mapping_restore_requires_passing_regression():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"voice_revision": 1, "provider_id": "volcengine",
               "channel_id": "default", "model_snapshot": "250528",
               "region": "cn-beijing", "provider_voice_id": "pv",
               "regression": {"passed": False}}, command_id="m1",
              execute=lambda p: h.service.add_provider_mapping(
                  "t1", voice_id, "m1", p))
    with pytest.raises(ServiceError) as caught:
        h.command({}, command_id="r1",
                  execute=lambda p: h.service.change_mapping_status(
                      "t1", voice_id, "r1",
                      "1~volcengine~default~250528~cn-beijing", "restore", p))
    assert caught.value.code == "REGRESSION_FAILED"
    assert caught.value.status == 409


def test_mapping_change_emits_no_contract_event():
    """C15-4：映射变更不在契约主题上，本服务只写本地事实。"""
    h = Harness()
    voice_id = h.register()["voice_id"]
    before = h.count("outbox")
    h.command({"voice_revision": 1, "provider_id": "volcengine",
               "channel_id": "default", "model_snapshot": "250528",
               "region": "cn-beijing", "provider_voice_id": "pv",
               "regression": {"passed": True}}, command_id="m1",
              execute=lambda p: h.service.add_provider_mapping(
                  "t1", voice_id, "m1", p))
    assert h.count("outbox") == before


# ------------------------------------------------------------------ 内容与词典

def test_revision_duplicate_content_is_409():
    h = Harness()
    voice_id = h.register()["voice_id"]
    # 与注册时写下的 revision 1 内容不同，否则第一次 add_revision 就撞 F15。
    body = {k: h.body(**{})[k] for k in ("language_profile", "rights_ref",
                                         "emotion_profile")}
    body["rate_profile"] = {"baseline_wpm": 300}
    h.command(body, command_id="rev2",
              execute=lambda p: h.service.add_revision(
                  "t1", voice_id, "rev2", p))
    with pytest.raises(ServiceError) as caught:
        h.command(body, command_id="rev3",
                  execute=lambda p: h.service.add_revision(
                      "t1", voice_id, "rev3", p))
    assert caught.value.code == "DUPLICATE_VOICE_CONTENT"
    assert caught.value.status == 409


def test_revision_rechecks_sample_and_fails_closed():
    h = Harness()
    voice_id = h.register()["voice_id"]
    body = {**h.body(voice_sample_refs=[{"id": "art_2", "revision": 1}]),
            }
    with pytest.raises(ServiceError) as caught:
        h.command(body, command_id="rev2",
                  execute=lambda p: h.service.add_revision(
                      "t1", voice_id, "rev2", p))
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
    assert h.count("voice_revision") == 1


def test_candidate_accept_then_reject_is_not_pending():
    h = Harness()
    voice_id = h.register()["voice_id"]
    candidate = h.command(
        {"language_profile": {"primary": "zh-CN"},
         "rate_profile": {"wpm": 240},
         "rights_ref": dict(RIGHTS)}, command_id="cand1",
        execute=lambda p: h.service.add_candidate(
            "t1", voice_id, "cand1", p))
    assert candidate["voice_check"]["authorization_present"] is True
    accepted = h.command({}, command_id="acc1",
                         execute=lambda p: h.service.accept_candidate(
                             "t1", voice_id, candidate["candidate_id"],
                             "acc1", p))
    assert accepted["state"] == "ACCEPTED"
    assert accepted["revision"]["revision"] == 2
    with pytest.raises(ServiceError) as caught:
        h.command({}, command_id="rej1",
                  execute=lambda p: h.service.reject_candidate(
                      "t1", voice_id, candidate["candidate_id"], "rej1", p))
    assert caught.value.code == "CANDIDATE_NOT_PENDING"


def test_set_canonical_keeps_single_live_pointer():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"language_profile": {"primary": "zh-CN"},
               "rate_profile": {"wpm": 240},
               "rights_ref": dict(RIGHTS)}, command_id="rev2",
              execute=lambda p: h.service.add_revision(
                  "t1", voice_id, "rev2", p))
    result = h.command({"revision": 2}, command_id="set1",
                       execute=lambda p: h.service.set_canonical(
                           "t1", voice_id, "set1", p))
    assert result["revision"] == 2
    live = [row for row in h.rows("canonical_pointer")
            if row["superseded_at"] is None]
    assert len(live) == 1
    assert live[0]["revision"] == 2
    assert h.rows("voice")[0]["canonical_revision"] == 2


def test_set_canonical_for_revoked_voice_is_refused():
    h = Harness()
    voice_id = h.register()["voice_id"]
    h.command({"language_profile": {"primary": "zh-CN"},
               "rate_profile": {"wpm": 240},
               "rights_ref": dict(RIGHTS)}, command_id="rev2",
              execute=lambda p: h.service.add_revision(
                  "t1", voice_id, "rev2", p))
    h.command({"evidence_ref": {"id": "l1"}, "reason_code": "RIGHTS_WITHDRAWN"},
              command_id="rev1",
              execute=lambda p: h.service.revoke("t1", voice_id, "rev1", p))
    with pytest.raises(ServiceError) as caught:
        h.command({"revision": 2}, command_id="set1",
                  execute=lambda p: h.service.set_canonical(
                      "t1", voice_id, "set1", p))
    assert caught.value.code == "STATE_TRANSITION_INVALID"


def test_lexicon_rejects_malformed_entries():
    h = Harness()
    with pytest.raises(ServiceError) as caught:
        h.command({"scope_kind": "series", "scope_id": "ser_1",
                   "entries": [{"term": "重庆"}]}, command_id="lex1",
                  execute=lambda p: h.service.create_lexicon("t1", "lex1", p))
    assert caught.value.code == "LEXICON_ENTRY_INVALID"
    assert caught.value.status == 422


def test_lexicon_duplicate_content_is_409():
    h = Harness()
    entries = [{"term": "重庆",
                "readings": [{"pinyin": "chong qing"}]}]
    h.command({"scope_kind": "series", "scope_id": "ser_1",
               "entries": entries}, command_id="lex1",
              execute=lambda p: h.service.create_lexicon("t1", "lex1", p))
    with pytest.raises(ServiceError) as caught:
        h.command({"scope_kind": "series", "scope_id": "ser_1",
                   "entries": entries}, command_id="lex2",
                  execute=lambda p: h.service.create_lexicon("t1", "lex2", p))
    assert caught.value.code == "DUPLICATE_LEXICON_CONTENT"
    assert caught.value.status == 409


def test_lexicon_binding_keeps_at_most_one_default():
    h = Harness()
    voice_id = h.register()["voice_id"]
    entries_a = [{"term": "重", "readings": [{"pinyin": "zhong"}]}]
    entries_b = [{"term": "行", "readings": [{"pinyin": "xing"}]}]
    first = h.command({"scope_kind": "voice", "scope_id": voice_id,
                       "entries": entries_a}, command_id="l1",
                      execute=lambda p: h.service.create_lexicon(
                          "t1", "l1", p))
    second = h.command({"scope_kind": "voice", "scope_id": voice_id,
                        "entries": entries_b}, command_id="l2",
                       execute=lambda p: h.service.create_lexicon(
                           "t1", "l2", p))
    for lexicon, command in ((first, "b1"), (second, "b2")):
        h.command({"lexicon_id": lexicon["lexicon_id"],
                   "lexicon_revision": 1, "is_default": True},
                  command_id=command,
                  execute=lambda p, c=command: h.service.bind_lexicon(
                      "t1", voice_id, c, p))
    defaults = [row for row in h.rows("voice_lexicon_binding")
                if row["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["lexicon_id"] == second["lexicon_id"]


def test_bindings_query_returns_voice_scope_and_lexicons():
    """C15-2：`/bindings` 由本服务自己的行答复（S10 未实现）。"""
    h = Harness()
    voice_id = h.register()["voice_id"]
    entries = [{"term": "重", "readings": [{"pinyin": "zhong"}]}]
    lexicon = h.command({"scope_kind": "voice", "scope_id": voice_id,
                         "entries": entries}, command_id="l1",
                        execute=lambda p: h.service.create_lexicon(
                            "t1", "l1", p))
    h.command({"lexicon_id": lexicon["lexicon_id"], "lexicon_revision": 1,
               "is_default": True}, command_id="b1",
              execute=lambda p: h.service.bind_lexicon(
                  "t1", voice_id, "b1", p))
    bindings = h.service.get_bindings("t1", voice_id)
    assert bindings["project_id"] == "prj_1"
    assert bindings["series_scope"] == "ser_1"
    assert bindings["lexicon_bindings"] == [
        {"lexicon_id": lexicon["lexicon_id"], "lexicon_revision": 1,
         "is_default": True}]


def test_lookup_requires_scope_and_alias():
    h = Harness()
    h.register()
    found = h.service.lookup_voice("t1", series_scope="ser_1",
                                    alias="旁白甲")
    assert found["display_name"] == "旁白甲"
    with pytest.raises(ServiceError) as caught:
        h.service.lookup_voice("t1", series_scope="ser_1", alias="")
    assert caught.value.status == 400
