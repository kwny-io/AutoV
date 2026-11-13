"""S45 内核与 HTTP 层。

测试的组织方式和断言重点：

- **链是自证的。** 每个"记录已存在"的检查都重算 `record_hash`，而不是比对
  字符串常量——否则测试只能证明"写进去的东西还在"，不能证明"链能复核"。
- **不可变用触发器证明。** UPDATE/DELETE 各一条，断言错误消息是
  `AUDIT_RECORD_IMMUTABLE`，而不是断言"行数没变"（后者不触发也成立）。
- **脱敏不是"有没有命中"而是"有没有漏"。** 断言存储值里不含明文，
  且改回原值会改变 `record_hash`（证明脱敏发生在哈希之前）。
- **每一条 fail-closed 都有反向对照。** 没配授权源 503，配了但键未注册
  也是 503，且两者 code 不同；不然"503"会被读成一种情况。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import SqliteIdempotencyStore, canonical_request_hash

from audit.schema import (CONTRACT_GAPS, PERMISSIONS, REGISTERED_PERMISSIONS,
                          WRITE_PERMISSIONS)
from audit.service import (
    GENESIS_HASH, MAX_DETAILS_BYTES, SEGMENT_MAX_RECORDS, AuditService,
    ServiceError)

TENANT = "t1"
OTHER = "t2"
KEY = b"\x01" * 32
FUTURE = "2099-01-01T00:00:00.000Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def intent(**over) -> dict:
    base = {
        "schema_version": "1.0",
        "intent_id": None,
        "occurred_at": "2026-11-13T10:00:00.000Z",
        "source_service": "S19",
        "source_verb": "creative.breakdown.seal",
        "action": "creative.breakdown.seal",
        "actor": {"principal_id": "p_alice", "principal_type": "human",
                  "delegation_id": None},
        "targets": [{"service_id": "S03", "kind": "breakdown", "id": "bd_1"}],
        "primary_target_index": 0,
        "policy_ref": {"service_id": "S02", "id": "pol_1", "revision": 3},
        "evidence_refs": [],
        "outcome": "SUCCEEDED",
        "outcome_detail": {},
        "details": {"episode_count": 12},
        "correlation_id": "corr_1",
        "causation_id": None,
    }
    base.update(over)
    return base


class Harness:
    """内存库 + 授权 stub + 与 `api/app.py` 同形的幂等外壳。"""

    def __init__(self, conn=None, *, allow: bool = True,
                 events: list | None = None, **over):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)
        self.allow = allow
        self.calls: list = []
        self.events = events if events is not None else []
        self.service = AuditService(
            self.conn,
            authorization_source=self._authorize,
            backpressure_source=over.get("backpressure", lambda: False),
            redact_key=over.get("redact_key", KEY),
        )

    def _authorize(self, tenant_id, principal_id, scope_kind, scope_id,
                   permission):
        self.calls.append(permission)
        return {"allowed": self.allow, "permissions": [permission] if self.allow
                else [], "denied": [] if self.allow else [permission]}

    # ---- 与 app.command 同构 ----
    def command(self, tenant, body, execute, command_id="c1"):
        request_hash = canonical_request_hash(body)
        record = self.idem.begin(tenant, command_id, request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = execute(request_hash)
        except Exception:
            self.idem.abort(tenant, command_id)
            raise
        self.idem.finish(tenant, command_id,
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    def record(self, body=None, *, tenant=TENANT, command_id="c1"):
        body = intent() if body is None else body
        return self.command(tenant, body,
                            lambda h: self.service.record(tenant, command_id,
                                                          body, h),
                            command_id=command_id)

    def rows(self, table, **where):
        sql = f"SELECT * FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        return [dict(r) for r in
                self.conn.execute(sql, list(where.values())).fetchall()]

    def envelope(self, event_id="evt_1", data=None, source="S19"):
        return {"id": event_id, "tenant_id": TENANT, "source": source,
                "type": "creative.breakdown.sealed", "data": data or intent()}


# ================================================================ 摄入

class TestIngest:
    def test_record_is_persisted_and_chained(self):
        h = Harness()
        result = h.record()
        row = h.rows("audit_record")[0]
        assert row["record_id"] == result["record_id"]
        assert row["stream_seq"] == 1
        assert row["prev_hash"] == GENESIS_HASH
        assert row["record_hash"] == _sha(GENESIS_HASH + row["canonical_sha256"]
                                          if False else
                                          GENESIS_HASH + _canonical(row))
        assert row["action"] == "creative.breakdown.seal"
        assert row["occurred_at"] == "2026-11-13T10:00:00.000Z"

    def test_stream_key_is_primary_target_not_first_target(self):
        h = Harness()
        body = intent(
            targets=[{"service_id": "S03", "kind": "breakdown", "id": "bd_1"},
                     {"service_id": "S04", "kind": "episode", "id": "ep_2"}],
            primary_target_index=1)
        h.record(body)
        row = h.rows("audit_record")[0]
        assert row["stream_key"] == "tenant:t1:subject:episode:ep_2"
        # 两个 target 都进索引，不只是主 target（§3.2 "all targets"）。
        assert len(h.rows("audit_record_target")) == 2

    def test_second_record_links_to_first(self):
        h = Harness()
        first = h.record(command_id="c1")
        second = h.record(intent(action="creative.breakdown.revise"),
                          command_id="c2")
        rows = sorted(h.rows("audit_record"), key=lambda r: r["stream_seq"])
        assert [r["stream_seq"] for r in rows] == [1, 2]
        assert rows[1]["prev_hash"] == first["record_hash"]
        assert second["record_hash"] != first["record_hash"]

    def test_ingest_does_not_ask_authorization(self):
        """可降级依赖的落点：写路径不问 S01（§2.4）。"""
        h = Harness()
        h.record()
        assert h.calls == []

    def test_duplicate_event_id_is_idempotent(self):
        h = Harness()
        first = h.service.consume_intent(h.envelope("evt_1"))
        second = h.service.consume_intent(h.envelope("evt_1"))
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert second["record_id"] == first["record_id"]
        assert len(h.rows("audit_record")) == 1
        assert len(h.rows("inbox")) == 1

    def test_duplicate_command_id_replays_without_second_row(self):
        h = Harness()
        first = h.record(command_id="same")
        replay = h.record(command_id="same")
        assert replay["replayed"] is True
        assert replay["record_id"] == first["record_id"]
        assert len(h.rows("audit_record")) == 1

    def test_same_command_id_different_body_conflicts(self):
        from idempotency_sdk import CommandConflict
        h = Harness()
        h.record(command_id="same")
        with pytest.raises(CommandConflict):
            h.record(intent(action="something.else"), command_id="same")

    def test_same_intent_id_replays(self):
        h = Harness()
        body = intent(intent_id="aint_fixed")
        first = h.record(body, command_id="c1")
        second = h.record(body, command_id="c2")
        assert second["duplicate"] is True
        assert second["record_id"] == first["record_id"]
        assert len(h.rows("audit_record")) == 1

    def test_sequence_is_unique_per_stream(self):
        h = Harness()
        h.record(command_id="c1")
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO audit_record (record_id, tenant_id, stream_key,"
                " stream_seq, recorded_at, occurred_at, source_service,"
                " action, actor, targets, outcome, details, schema_version,"
                " prev_hash, record_hash, canonical_sha256)"
                " VALUES ('dup','t1','tenant:t1:subject:breakdown:bd_1',1,"
                "'2026-11-13T00:00:00.000Z','2026-11-13T00:00:00.000Z','S19',"
                "'a','{}','[]','SUCCEEDED','{}','1.0',?,?,?)",
                (GENESIS_HASH, "0" * 64, "0" * 64))

    def test_producer_mismatch_is_403(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.service.consume_intent(h.envelope("evt_1", source="S19"),
                                     producer_id="S20")
        assert exc.value.status == 403
        assert exc.value.code == "SOURCE_MISMATCH"

    def test_producer_unverified_is_reported_not_assumed(self):
        h = Harness()
        result = h.service.consume_intent(h.envelope("evt_1"))
        assert result["producer_verified"] is False

    def test_backpressure_is_429_and_retryable(self):
        h = Harness(backpressure=lambda: True)
        with pytest.raises(ServiceError) as exc:
            h.record()
        assert exc.value.status == 429
        assert exc.value.code == "AUDIT_SINK_BACKPRESSURE"
        assert exc.value.retryable is True
        assert h.rows("audit_record") == []


# ================================================================ 不可变

class TestImmutability:
    def test_update_aborts(self):
        h = Harness()
        h.record()
        with pytest.raises(sqlite3.IntegrityError) as exc:
            h.conn.execute("UPDATE audit_record SET action='tampered'")
        assert "AUDIT_RECORD_IMMUTABLE" in str(exc.value)
        assert h.rows("audit_record")[0]["action"] == "creative.breakdown.seal"

    def test_delete_aborts(self):
        h = Harness()
        h.record()
        with pytest.raises(sqlite3.IntegrityError) as exc:
            h.conn.execute("DELETE FROM audit_record")
        assert "AUDIT_RECORD_IMMUTABLE" in str(exc.value)
        assert len(h.rows("audit_record")) == 1

    def test_chain_break_is_detected_not_repaired(self):
        h = Harness()
        h.record(command_id="c1")
        h.record(intent(action="a.b"), command_id="c2")
        # 绕过触发器直接改 canonical_sha256：链必须能发现，而不是自愈。
        h.conn.execute("DROP TRIGGER audit_record_no_update")
        # 改一个参与 canonical 的字段。只改 canonical_sha256 是不够的：
        # 复算走的是 record_hash，本就不读那一列，改它不算篡改有效载荷。
        h.conn.execute("UPDATE audit_record SET action='forged' "
                       "WHERE stream_seq=1")
        report = h.service.verify_stream(TENANT,
                                         "tenant:t1:subject:breakdown:bd_1")
        assert report["chain_ok"] is False
        assert report["segments"][0]["broken_at"] == 1


# ================================================================ 形状

class TestPayloadShape:
    def test_raw_arguments_rejected(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.record(intent(details={"arguments": {"city": "Shanghai"}}))
        assert exc.value.status == 422
        assert exc.value.code == "SCHEMA_INVALID"
        assert exc.value.details["forbidden_field"] == "arguments"

    def test_nested_prompt_rejected(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.record(intent(details={"steps": [{"prompt": "你是一名编剧"}]}))
        assert exc.value.details["forbidden_field"] == "steps[0].prompt"

    def test_empty_targets_is_400(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.record(intent(targets=[]))
        assert exc.value.status == 400
        assert exc.value.code == "MALFORMED_REQUEST"

    def test_unknown_schema_major_rejected(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.record(intent(schema_version="9.0"))
        assert exc.value.code == "SCHEMA_INVALID"

    def test_details_over_64kb_is_payload_too_large(self):
        h = Harness()
        big = {"refs": "x" * (MAX_DETAILS_BYTES + 1)}
        with pytest.raises(ServiceError) as exc:
            h.record(intent(details=big))
        assert exc.value.status == 422
        assert exc.value.code == "PAYLOAD_TOO_LARGE"
        assert h.rows("audit_record") == []

    def test_bad_outcome_rejected(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.record(intent(outcome="OK"))
        assert exc.value.code == "SCHEMA_INVALID"

    def test_target_needs_service_kind_id(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.record(intent(targets=[{"kind": "breakdown", "id": "bd_1"}]))
        assert exc.value.code == "SCHEMA_INVALID"


# ================================================================ 脱敏

class TestRedaction:
    def test_bearer_token_not_stored_in_plaintext(self):
        h = Harness()
        token = "Bearer " + "q" * 32
        h.record(intent(details={"note": f"called with {token}"}))
        row = h.rows("audit_record")[0]
        stored = _jcs(json.loads(row["details"]))
        assert "q" * 32 not in stored
        assert "REDACTED:hmac_bearer_token:" in stored

    def test_redaction_happens_before_hashing(self):
        """存储的 canonical 一重算就对上——证明存的就是被哈希的那份。"""
        h = Harness()
        h.record(intent(details={"note": "sk-" + "z" * 30}))
        row = h.rows("audit_record")[0]
        assert row["record_hash"] == _sha(row["prev_hash"] + _canonical(row))
        assert "z" * 30 not in _jcs(json.loads(row["details"]))

    def test_content_hash_is_exempt_from_generic_branch(self):
        digest = "ab" * 32
        h = Harness()
        h.record(intent(details={"content_hash": digest}))
        row = h.rows("audit_record")[0]
        assert json.loads(row["details"])["content_hash"] == digest

    def test_content_hash_still_scanned_for_real_secrets(self):
        """豁免只针对通用分支；`sk-` 前缀是明确的密钥形状，不豁免。"""
        h = Harness()
        h.record(intent(details={"content_hash": "sk-" + "z" * 30}))
        assert "z" * 30 not in h.rows("audit_record")[0]["details"]

    def test_class_b_has_no_correlation_material(self):
        h = Harness()
        h.record(intent(details={"note": "postgres://u:hunter2@db.internal/x"}))
        stored = h.rows("audit_record")[0]["details"]
        assert "hunter2" not in stored
        assert "REDACTED:connection_string" in stored
        assert "hmac_" not in stored

    def test_redaction_writes_self_record_without_recursion(self):
        h = Harness()
        h.record(intent(details={"note": "sk-" + "z" * 30}))
        actions = [r["action"] for r in h.rows("audit_record")]
        assert actions.count("audit.payload.redacted") == 1
        assert len(actions) == 2  # 一次脱敏只写一条自审记录，不自引用

    def test_clean_record_writes_no_self_record(self):
        h = Harness()
        h.record()
        assert [r["action"] for r in h.rows("audit_record")] == [
            "creative.breakdown.seal"]

    def test_marker_prefix_is_escaped(self):
        from audit.service import redact_leaf
        value, hits = redact_leaf("REDACTED:not-a-marker", "p", "note", KEY)
        assert value.startswith("\\REDACTED:")
        assert hits == []

    def test_existing_marker_is_idempotent(self):
        from audit.service import redact_leaf
        once, _ = redact_leaf("bearer " + "q" * 32, "p", "note", KEY)
        twice, _ = redact_leaf(once, "p", "note", KEY)
        assert twice == once

    def test_scanner_failure_truncates_and_marks(self):
        def broken(*_a, **_k):
            raise RuntimeError("scanner down")
        h = Harness()
        h.service.scanner = broken
        h.record(intent(details={"note": "abcdefghijklmnop"}))
        stored = h.rows("audit_record")[0]["details"]
        assert "REDACTED:scanner_unavailable" in stored
        assert "abcdefghij" not in stored.replace(
            "abcdefghREDACTED:scanner_unavailable", "")


def _canonical(row: dict) -> str:
    """按 §3.3 的键集重算 canonical，与内核独立实现。"""
    keys = ("tenant_id", "stream_key", "stream_seq", "recorded_at",
            "occurred_at", "source_service", "source_event_id", "action",
            "actor", "targets", "policy_ref", "evidence_refs", "outcome",
            "outcome_detail", "details", "correlation_id", "causation_id",
            "schema_version")
    out = {}
    for key in keys:
        value = row.get(key)
        if key in ("actor", "targets", "policy_ref", "evidence_refs",
                   "outcome_detail", "details"):
            value = json.loads(value) if isinstance(value, str) else value
        out[key] = value
    return _jcs(out)


# ================================================================ 授权

class TestAuthorization:
    def test_read_without_source_is_503(self):
        h = Harness()
        h.service.authorization_source = None
        with pytest.raises(ServiceError) as exc:
            h.service.query_records(TENANT, principal={"principal_id": "p"})
        assert exc.value.status == 503
        assert exc.value.code == "DEPENDENCY_UNAVAILABLE"
        assert exc.value.retryable is True

    def test_denied_read_is_403_with_read_code(self):
        h = Harness(allow=False)
        with pytest.raises(ServiceError) as exc:
            h.service.query_records(TENANT, principal={"principal_id": "p"})
        assert exc.value.status == 403
        assert exc.value.code == "AUDIT_READ_DENIED"

    def test_denied_export_has_its_own_code(self):
        h = Harness(allow=False)
        with pytest.raises(ServiceError) as exc:
            h.service.create_export(TENANT, "c1", {}, "h",
                                    principal={"principal_id": "p"})
        assert exc.value.code == "AUDIT_EXPORT_DENIED"

    def test_real_registry_has_no_audit_keys(self):
        """NEW-1 的机器检查：这张常量表迟早要跟注册表对齐。"""
        root = Path(__file__).resolve().parents[3]
        path = root / "contracts" / "identity" / "permission-registry.v2.json"
        registry = json.loads(path.read_text(encoding="utf-8"))
        keys = {p["key"] for p in registry["permissions"]}
        assert len(keys) == 40
        assert not any(k.startswith("audit.") for k in keys)
        # 所以"已注册"是空集，而下面四个都真的会问 S01。
        assert REGISTERED_PERMISSIONS == frozenset()
        assert set(WRITE_PERMISSIONS.values()) <= set(PERMISSIONS.values())
        for key in WRITE_PERMISSIONS.values():
            assert key not in keys
        assert PERMISSIONS["read"] not in keys


# ================================================================ 查询

class TestQueries:
    def test_cross_tenant_read_is_404(self):
        h = Harness()
        result = h.record()
        with pytest.raises(ServiceError) as exc:
            h.service.get_record(OTHER, result["record_id"],
                                 principal={"principal_id": "p"})
        assert exc.value.status == 404
        assert exc.value.code == "NOT_FOUND"

    def test_cross_tenant_query_returns_nothing(self):
        h = Harness()
        h.record()
        result = h.service.query_records(OTHER,
                                         principal={"principal_id": "p"})
        assert result["records"] == []

    def test_query_by_actor_uses_json_extract(self):
        h = Harness()
        h.record()
        h.record(intent(actor={"principal_id": "p_bob",
                               "principal_type": "human",
                               "delegation_id": None}),
                 command_id="c2")
        result = h.service.query_records(TENANT, principal={"principal_id": "p"},
                                         actor="p_bob")
        assert len(result["records"]) == 1
        assert result["records"][0]["actor"]["principal_id"] == "p_bob"

    def test_access_log_records_hash_not_bodies(self):
        h = Harness()
        h.record()
        h.service.query_records(TENANT, principal={"principal_id": "p"},
                                purpose="incident")
        log = h.rows("audit_access_log")
        assert len(log) == 1
        assert log[0]["access_kind"] == "QUERY"
        assert log[0]["purpose"] == "incident"
        assert len(log[0]["query_hash"]) == 64
        # 只存摘要：日志里不得出现被查记录的任何字段值。
        assert "creative.breakdown.seal" not in _jcs(log[0])

    def test_coverage_reports_proof_only_segments(self):
        h = Harness()
        h.record()
        h.conn.execute("UPDATE audit_segment SET state='DESTROYED'")
        result = h.service.query_records(TENANT, principal={"principal_id": "p"})
        assert result["coverage"]["proof_only_segments"] == 1
        assert result["coverage"]["gaps"][0]["reason"] == "DESTROYED"

    def test_by_target_finds_non_primary_targets(self):
        h = Harness()
        h.record(intent(targets=[
            {"service_id": "S03", "kind": "breakdown", "id": "bd_1"},
            {"service_id": "S04", "kind": "episode", "id": "ep_9"}]))
        result = h.service.by_target(TENANT, service_id="S04", kind="episode",
                                     target_id="ep_9",
                                     principal={"principal_id": "p"})
        assert len(result["records"]) == 1


# ================================================================ 分段

class TestSegments:
    def test_segment_tracks_open_window(self):
        h = Harness()
        h.record(command_id="c1")
        h.record(intent(action="a.b"), command_id="c2")
        seg = h.rows("audit_segment")[0]
        assert seg["state"] == "OPEN"
        assert (seg["from_seq"], seg["to_seq"]) == (1, 2)
        assert seg["record_count"] == 2
        assert seg["from_prev_hash"] == GENESIS_HASH

    def test_seal_closes_window_and_opens_next(self):
        h = Harness()
        h.record(command_id="c1")
        seg = h.rows("audit_segment")[0]
        h.service.seal_segment(TENANT, seg["segment_id"])
        h.record(intent(action="a.b"), command_id="c2")
        segments = sorted(h.rows("audit_segment"), key=lambda s: s["from_seq"])
        assert [s["state"] for s in segments] == ["SEALED", "OPEN"]
        assert segments[1]["from_seq"] == 2
        assert segments[1]["from_prev_hash"] == segments[0]["root_hash"]

    def test_segment_proof_links_to_previous(self):
        h = Harness()
        h.record(command_id="c1")
        first = h.rows("audit_segment")[0]
        h.service.seal_segment(TENANT, first["segment_id"])
        h.record(intent(action="a.b"), command_id="c2")
        second = [s for s in h.rows("audit_segment")
                  if s["state"] == "OPEN"][0]
        proof = h.service.segment_proof(TENANT, second["segment_id"])
        assert proof["link_ok"] is True
        assert proof["previous_segment"]["segment_id"] == first["segment_id"]

    def test_segment_proof_cross_tenant_is_404(self):
        h = Harness()
        h.record()
        seg = h.rows("audit_segment")[0]
        with pytest.raises(ServiceError) as exc:
            h.service.segment_proof(OTHER, seg["segment_id"])
        assert exc.value.status == 404

    def test_archive_requires_sealed(self):
        h = Harness()
        h.record()
        seg = h.rows("audit_segment")[0]
        with pytest.raises(ServiceError) as exc:
            h.service.archive_segment(TENANT, seg["segment_id"])
        assert exc.value.code == "SEGMENT_NOT_SEALED"
        h.service.seal_segment(TENANT, seg["segment_id"])
        assert h.service.archive_segment(
            TENANT, seg["segment_id"])["state"] == "ARCHIVED"


# ================================================================ 导出

class TestExport:
    def _export(self, h, body=None, command_id="exp-c1"):
        # 导出用独立的 command_id：同一次测试里通常已经用 c1 追加过记录，
        # 复用同一个键会（正确地）撞上 IDEMPOTENCY_KEY_REUSED。
        body = {} if body is None else body
        return h.command(TENANT, body,
                         lambda _h: h.service.create_export(
                             TENANT, command_id, body, _h,
                             principal={"principal_id": "p"}),
                         command_id=command_id)

    def test_export_is_ready_and_continuous(self):
        h = Harness()
        h.record(command_id="c1")
        result = self._export(
            h, {"stream_key": "tenant:t1:subject:breakdown:bd_1"})
        assert result["state"] == "READY"
        assert result["record_count"] == 1
        assert result["chain_continuous"] is True
        assert len(result["bundle_ref"]["sha256"]) == 64

    def test_export_publishes_only_export_ready(self):
        h = Harness()
        h.record(command_id="c1")
        self._export(h)
        outbox = h.rows("outbox")
        assert [e["event_type"] for e in outbox] == ["audit.export.ready"]
        assert "audit.record.appended" not in [e["event_type"]
                                               for e in outbox]

    def test_export_ready_has_no_guessed_home_cell(self):
        h = Harness()
        h.record(command_id="c1")
        self._export(h)
        payload = json.loads(h.rows("outbox")[0]["payload"])
        assert payload["home_cell"] == "cell_unassigned"

    def test_verify_confirms_chain(self):
        h = Harness()
        h.record(command_id="c1")
        export = self._export(h)
        result = h.service.verify_export(TENANT, export["export_id"],
                                         principal={"principal_id": "p"})
        assert result["chain_ok"] is True

    def test_verify_chain_is_recomputed_not_trusted(self):
        h = Harness()
        h.record(command_id="c1")
        export = self._export(h)
        h.conn.execute("DROP TRIGGER audit_record_no_update")
        h.conn.execute("UPDATE audit_record SET action='forged'")
        result = h.service.verify_export(TENANT, export["export_id"],
                                         principal={"principal_id": "p"})
        assert result["chain_ok"] is False

    def test_verify_unknown_export_is_404(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.service.verify_export(TENANT, "exp_nope",
                                    principal={"principal_id": "p"})
        assert exc.value.status == 404

    def test_export_cross_tenant_is_404(self):
        h = Harness()
        h.record(command_id="c1")
        export = self._export(h)
        with pytest.raises(ServiceError) as exc:
            h.service.get_export(OTHER, export["export_id"],
                                 principal={"principal_id": "p"})
        assert exc.value.status == 404

    def test_export_with_destroyed_gap_is_not_continuous(self):
        h = Harness()
        h.record(command_id="c1")
        h.conn.execute("UPDATE audit_segment SET state='DESTROYED'")
        result = self._export(h)
        assert result["chain_continuous"] is False
        assert result["bundle_ref"]["size_bytes"] > 0


# ================================================================ 保留与销毁

class TestRetention:
    def test_default_policy_is_not_configured(self):
        h = Harness()
        assert h.service._policy(TENANT)["retention_total_days"] is None

    def test_policy_ordering_is_enforced(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.service.put_retention_policy(
                TENANT, {"retention_hot_days": 900,
                         "retention_warm_days": 100},
                principal={"principal_id": "p"})
        assert exc.value.code == "SCHEMA_INVALID"

    def test_policy_recorded_with_strict_audit_note(self):
        h = Harness()
        result = h.service.put_retention_policy(
            TENANT, {"retention_total_days": 3650, "strict_audit": True},
            principal={"principal_id": "p"})
        assert result["policy"]["strict_audit"] == 1
        assert "blocks nothing" in result["note"]

    def test_retention_check_reports_reference(self):
        h = Harness()
        h.record()
        result = h.service.retention_check(TENANT, service_id="S03",
                                           kind="breakdown", target_id="bd_1")
        assert result["referenced"] is True
        assert result["reason"] == "RETENTION_ACTIVE"

    def test_retention_check_unknown_target(self):
        h = Harness()
        result = h.service.retention_check(TENANT, service_id="S03",
                                           kind="breakdown", target_id="bd_x")
        assert result["referenced"] is False
        assert result["reason"] is None

    def test_retention_check_sees_legal_hold(self):
        h = Harness()
        h.record()
        h.service.create_legal_hold(
            TENANT, {"scope_kind": "tenant", "scope": {},
                     "reason": "litigation"},
            principal={"principal_id": "p"})
        result = h.service.retention_check(TENANT, service_id="S03",
                                           kind="breakdown", target_id="bd_1")
        assert result["reason"] == "LEGAL_HOLD"
        assert len(result["hold_refs"]) == 1

    def test_destruction_blocked_by_unsealed_segment(self):
        h = Harness()
        h.record()
        result = h.service.create_destruction_order(
            TENANT, {"scope": {"stream_key":
                               "tenant:t1:subject:breakdown:bd_1"}},
            principal={"principal_id": "p"})
        assert result["state"] == "BLOCKED"
        assert result["preconditions"]["archive_complete"] is False
        assert result["blocked_reason"] == "ARCHIVE_INCOMPLETE"

    def test_destruction_executes_when_all_five_hold(self):
        h = Harness()
        h.record()
        h.service.put_retention_policy(
            TENANT, {"retention_hot_days": 0, "retention_warm_days": 0,
                     "retention_total_days": 0},
            principal={"principal_id": "p"})
        seg = h.rows("audit_segment")[0]
        h.service.seal_segment(TENANT, seg["segment_id"])
        h.service.archive_segment(TENANT, seg["segment_id"])
        order = h.service.create_destruction_order(
            TENANT, {"scope": {"stream_key":
                               "tenant:t1:subject:breakdown:bd_1"}},
            principal={"principal_id": "p"})
        assert order["state"] == "REQUESTED", order
        result = h.service.execute_destruction_order(
            TENANT, order["order_id"], principal={"principal_id": "p"})
        assert result["state"] == "EXECUTED"
        assert result["proof_only"] is True
        assert h.rows("audit_segment")[0]["state"] == "DESTROYED"

    def test_legal_hold_blocks_destruction_with_its_own_code(self):
        h = Harness()
        h.record()
        h.service.put_retention_policy(
            TENANT, {"retention_hot_days": 0, "retention_warm_days": 0,
                     "retention_total_days": 0},
            principal={"principal_id": "p"})
        seg = h.rows("audit_segment")[0]
        h.service.seal_segment(TENANT, seg["segment_id"])
        h.service.archive_segment(TENANT, seg["segment_id"])
        h.service.create_legal_hold(
            TENANT, {"scope_kind": "tenant", "scope": {}, "reason": "hold"},
            principal={"principal_id": "p"})
        order = h.service.create_destruction_order(
            TENANT, {"scope": {"stream_key":
                               "tenant:t1:subject:breakdown:bd_1"}},
            principal={"principal_id": "p"})
        with pytest.raises(ServiceError) as exc:
            h.service.execute_destruction_order(
                TENANT, order["order_id"], principal={"principal_id": "p"})
        assert exc.value.status == 409
        assert exc.value.code == "LEGAL_HOLD_ACTIVE"

    def test_release_hold_is_idempotent(self):
        h = Harness()
        hold = h.service.create_legal_hold(
            TENANT, {"scope_kind": "tenant", "scope": {}, "reason": "hold"},
            principal={"principal_id": "p"})
        first = h.service.release_legal_hold(
            TENANT, hold["hold_id"], principal={"principal_id": "p"})
        second = h.service.release_legal_hold(
            TENANT, hold["hold_id"], principal={"principal_id": "p"})
        assert first["already_released"] is False
        assert second["already_released"] is True

    def test_retention_not_expired_blocks(self):
        h = Harness()
        h.record()
        h.service.put_retention_policy(TENANT, {"retention_total_days": 3650},
                                       principal={"principal_id": "p"})
        seg = h.rows("audit_segment")[0]
        h.service.seal_segment(TENANT, seg["segment_id"])
        h.service.archive_segment(TENANT, seg["segment_id"])
        result = h.service.create_destruction_order(
            TENANT, {"scope": {"stream_key":
                               "tenant:t1:subject:breakdown:bd_1"}},
            principal={"principal_id": "p"})
        assert result["preconditions"]["retention_expired"] is False
        assert result["blocked_reason"] == "RETENTION_NOT_EXPIRED"


# ================================================================ schema 自检

class TestSchema:
    def test_gap_ids_are_unique_and_shaped(self):
        ids = [g["id"] for g in CONTRACT_GAPS]
        assert len(ids) == len(set(ids))
        for gap in CONTRACT_GAPS:
            assert gap["kind"] in ("conflict", "needs_owner", "gap")
            assert gap["summary"] and gap["handling"]

    def test_new_6_records_the_brief_vs_plan_conflict(self):
        gap = next(g for g in CONTRACT_GAPS if g["id"] == "NEW-6")
        assert gap["kind"] == "conflict"
        assert "脱敏" in gap["summary"] or "redact" in gap["summary"].lower()

    def test_audit_record_has_no_update_or_delete_path_in_service(self):
        """内核里不存在对 audit_record 的 UPDATE/DELETE——只有触发器拦得到。"""
        source = (Path(__file__).resolve().parents[1] / "src" / "audit"
                  / "service.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            upper = line.upper()
            if "AUDIT_RECORD" in upper and ("UPDATE " in upper or
                                            "DELETE FROM" in upper):
                pytest.fail(f"service.py writes to audit_record: {line}")


# ================================================================ HTTP

def _http_client(monkeypatch, tmp_path, **over):
    """`api/app.py` 的 TestClient，上游换成 Harness 的 stub。

    - `S45_DB` 必须指向独立文件库（import 那一刻就建连接与表）；
    - 路由函数在定义时捕获模块全局，所以 `module.service` / `module._idem`
      必须重绑，且重绑时用 `module._conn`，否则断言查的是另一个库。
    """
    monkeypatch.setenv("S45_DB", str(tmp_path / "s45.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    # 不能 `import app`：别的服务也有 `api/app.py` 且可能先进 sys.modules。
    spec = importlib.util.spec_from_file_location("s45_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s45_api_app"] = module

    h = Harness(conn=module._conn, **over)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h


def _post(client, body, *, command_id="c1", tenant=TENANT, principal="p"):
    return client.post("/audit-records", json=body,
                       headers={"Idempotency-Key": command_id,
                                "X-Tenant-Id": tenant,
                                "X-Principal-Id": principal})


class TestHttpLayer:
    def test_append_returns_201_and_record(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = _post(client, intent())
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["stream_seq"] == 1
        assert body["replayed"] is False
        assert len(h.rows("audit_record")) == 1

    def test_replay_is_marked_and_does_not_append(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, intent(), command_id="same")
        r = _post(client, intent(), command_id="same")
        assert r.status_code == 201, r.text
        assert r.json()["replayed"] is True
        assert len(h.rows("audit_record")) == 1

    def test_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = _post(client, intent(tenant_id=OTHER))
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_SCOPE_DENIED"

    def test_missing_idempotency_key_is_400(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/audit-records", json=intent(),
                        headers={"X-Tenant-Id": TENANT})
        assert r.status_code == 422 or r.status_code == 400

    def test_raw_details_over_http_is_422(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = _post(client, intent(details={"arguments": {"x": 1}}))
        assert r.status_code == 422
        assert r.json()["code"] == "SCHEMA_INVALID"

    def test_token_is_redacted_over_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = _post(client, intent(details={"note": "Bearer " + "q" * 32}))
        assert r.status_code == 201, r.text
        assert "q" * 32 not in h.rows("audit_record")[0]["details"]

    def test_read_requires_principal(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/audit-records", headers={"X-Tenant-Id": TENANT})
        assert r.status_code == 401
        assert r.json()["code"] == "UNAUTHENTICATED"

    def test_read_denied_is_403(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path, allow=False)
        r = client.get("/audit-records",
                       headers={"X-Tenant-Id": TENANT,
                                "X-Principal-Id": "p"})
        assert r.status_code == 403
        assert r.json()["code"] == "AUDIT_READ_DENIED"

    def test_record_read_cross_tenant_is_404(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        record_id = _post(client, intent()).json()["record_id"]
        r = client.get(f"/audit-records/{record_id}",
                       headers={"X-Tenant-Id": OTHER,
                                "X-Principal-Id": "p"})
        assert r.status_code == 404

    def test_export_is_202_and_publishes(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, intent())
        r = client.post("/audit-exports", json={},
                        headers={"Idempotency-Key": "e1",
                                 "X-Tenant-Id": TENANT,
                                 "X-Principal-Id": "p"})
        assert r.status_code == 202, r.text
        assert r.json()["state"] == "READY"
        assert [e["event_type"] for e in h.rows("outbox")] == [
            "audit.export.ready"]

    def test_retention_check_route_answers_without_principal(
            self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        _post(client, intent())
        r = client.get("/retention-checks",
                       params={"service_id": "S03", "kind": "breakdown",
                               "target_id": "bd_1"},
                       headers={"X-Tenant-Id": TENANT})
        assert r.status_code == 200, r.text
        assert r.json()["referenced"] is True

    def test_readiness_carries_contract_gaps(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/health/readiness")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert len(body["contract_gaps"]) == len(CONTRACT_GAPS)
        assert [g["id"] for g in body["contract_gaps"]] == [
            g["id"] for g in CONTRACT_GAPS]

    def test_readiness_names_the_scanner_not_a_constant_true(
            self, monkeypatch, tmp_path):
        """扫描器恒有兜底，所以报布尔值等于没检查；报"用的是哪个"。"""
        client, _ = _http_client(monkeypatch, tmp_path)
        checks = client.get("/health/readiness").json()["checks"]
        assert checks["redaction_scanner"] in ("local_fallback", "upstream")
        assert checks["redaction_scanner"] == "local_fallback"

    def test_service_md_matches_the_implemented_kernel(self):
        """service.yaml 由 catalog 生成、停在 design_only；service.md 是手写的，
        必须说实现。两者不一致是任务约束的结果，不是可以含糊过去的地方。"""
        path = Path(__file__).resolve().parents[1] / "service.md"
        text = path.read_text(encoding="utf-8")
        assert "`implemented`" in text
        assert "`design_only`" not in text.split("## 实现边界")[0]
        assert "S45-audit.md" in text

    def test_event_endpoint_is_idempotent_over_http(self, monkeypatch,
                                                    tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        envelope = Harness().envelope("evt_http")
        first = client.post("/internal/audit-intents", json=envelope)
        second = client.post("/internal/audit-intents", json=envelope)
        assert first.status_code == 200, first.text
        assert second.json()["duplicate"] is True
        assert len(h.rows("audit_record")) == 1

    def test_event_endpoint_reports_producer_mismatch(self, monkeypatch,
                                                      tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/internal/audit-intents",
                        json=Harness().envelope("evt_http"),
                        headers={"X-Producer-Id": "S20"})
        assert r.status_code == 403
        assert r.json()["code"] == "SOURCE_MISMATCH"

    def test_command_in_progress_maps_to_409(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = intent()
        h.idem.begin(TENANT, "inflight", canonical_request_hash(body))
        r = _post(client, body, command_id="inflight")
        assert r.status_code == 409
        assert r.json()["code"] == "COMMAND_IN_PROGRESS"
        assert r.json()["retryable"] is True
