"""S28 测试。全部用本地 SQLite 与注入的替身，不接真实依赖、不付费。

对照 plans/S28-artifact-ingest.md §10。用例号沿用方案里的 T28-xx。

关键约束：**事件 payload 必须对着 `contracts/artifact-ingested.v2.schema.json`
校验**，不能只对着方案 §6.8 的示例文字。示例与 schema 不一致的地方已登记
（§6.8 缺 `schema_version`、`source` 缺 `kind`），以 schema 为准。
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
from idempotency_sdk import (
    CommandConflict,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from artifact_ingest import SCHEMA
from artifact_ingest.service import (
    ArtifactIngestService,
    ServiceError,
    Transaction,
)

ROOT = Path(__file__).resolve().parents[3]
PAYLOAD_SCHEMA = ROOT / "contracts" / "artifact-ingested.v2.schema.json"

TENANT_A = "t_aaa"
TENANT_B = "t_bbb"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    SqliteIdempotencyStore(conn)  # 建幂等表；HTTP 层才用它的 begin/finish
    return conn


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = _connect()
    yield connection
    connection.close()


@pytest.fixture
def svc(conn) -> ArtifactIngestService:
    return ArtifactIngestService(conn, host_allowlist={"cdn.provider.example"})


def _import_body(command_id: str, *, tenant_id=TENANT_A,
                 content_class="generated_video",
                 scope=None, expected_sha256=None,
                 target_artifact_id=None, expected_revision=None,
                 kind="internal_object", **source_extra) -> dict:
    source = {"kind": kind, **source_extra}
    return {
        "command_id": command_id,
        "tenant_id": tenant_id,
        "content_class": content_class,
        "source": source,
        "scope_ref": scope or {"project_id": "p_1"},
        "owner_ref": {"service_id": "S06", "id": "shot_3_2_1"},
        "retention": {"policy_ref": "ret_default_v1"},
        "expected_sha256": expected_sha256,
        "target_artifact_id": target_artifact_id,
        "expected_revision": expected_revision,
    }


def _publish(svc, tenant_id, body, payload="bytes", *, key=None, region="ap-northeast-1",
             probe=None):
    created = svc.create_import(tenant_id, body)
    sha = _sha(payload)
    result = svc.publish(
        tenant_id, created["import_id"],
        observed_size_bytes=len(payload), actual_sha256=sha,
        storage_key=key or f"t/{sha[:8]}/blob",
        storage_region=region,
        probe=probe or {"mime_type": "video/mp4", "probed": True})
    return created, result, sha


# ---- 幂等（E12） -----------------------------------------------------------


def test_duplicate_delivery_with_same_body_creates_one_import(svc):
    """T28-01：同一 command_id 同一 body 只落一条 import。

    重放是 HTTP 层 `command()` 的职责（同 hash 直接返回首次受理响应）；业务层
    直连时第二次撞唯一约束，必须是 409 而不是裸 IntegrityError 漏成 500。
    """
    body = _import_body("cmd_1")
    first = svc.create_import(TENANT_A, body)
    with pytest.raises(ServiceError) as e:
        svc.create_import(TENANT_A, body)
    assert (e.value.status, e.value.code) == (409, "IDEMPOTENCY_KEY_REUSED")
    rows = svc.conn.execute(
        "SELECT import_id FROM artifact_import WHERE tenant_id = ? AND command_id = ?",
        (TENANT_A, "cmd_1")).fetchall()
    assert len(rows) == 1
    assert rows[0]["import_id"] == first["import_id"]


def test_request_hash_excludes_command_id_and_request_hash():
    body = _import_body("cmd_1")
    same = {**body, "command_id": "cmd_2", "request_hash": "anything"}
    assert canonical_request_hash(body) == canonical_request_hash(same)


def test_request_hash_covers_provider_url():
    """§2.10：同 command_id 换 URL = 换命令，hash 必须变。"""
    a = _import_body("cmd_1", kind="provider_url",
                     provider_url="https://cdn.provider.example/a?sig=1",
                     provider_url_expires_at="2099-01-01T00:00:00Z")
    b = {**a, "source": {**a["source"],
                         "provider_url": "https://cdn.provider.example/a?sig=2"}}
    assert canonical_request_hash(a) != canonical_request_hash(b)


# ---- 版本与 CAS（§7.3） -----------------------------------------------------


def test_second_revision_bumps_current_revision(svc):
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "v1")
    body = _import_body("cmd_2", target_artifact_id=first["artifact_id"],
                        expected_revision=1)
    _, second, _ = _publish(svc, TENANT_A, body, "v2")
    assert second["revision"] == 2
    got = svc.get_artifact(TENANT_A, first["artifact_id"])
    assert got["current_revision"] == 2
    assert got["size_bytes"] == len("v2")


def test_stale_expected_revision_is_rejected(svc):
    """T28-10：expected_revision 落后 → 409 REVISION_CONFLICT，不覆盖。"""
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "v1")
    aid = first["artifact_id"]
    with pytest.raises(ServiceError) as e:
        svc.create_import(TENANT_A, _import_body(
            "cmd_2", target_artifact_id=aid, expected_revision=5))
    assert (e.value.status, e.value.code) == (409, "REVISION_CONFLICT")
    assert e.value.details["current_revision"] == 1


def test_cas_rejects_concurrent_publish(svc):
    """T28-26：两个导入都认领 revision 2，只有一个能落。"""
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "v1")
    aid = first["artifact_id"]
    # 两个导入都通过 §6.1 的 expected_revision 检查（当时都还是 1）
    left = svc.create_import(TENANT_A, _import_body(
        "cmd_2", target_artifact_id=aid, expected_revision=1))
    right = svc.create_import(TENANT_A, _import_body(
        "cmd_3", target_artifact_id=aid, expected_revision=1))

    svc.publish(TENANT_A, left["import_id"], observed_size_bytes=2,
                actual_sha256=_sha("v2a"), storage_key="k/a", storage_region="r")
    with pytest.raises(ServiceError) as e:
        svc.publish(TENANT_A, right["import_id"], observed_size_bytes=2,
                    actual_sha256=_sha("v2b"), storage_key="k/b", storage_region="r")
    assert (e.value.status, e.value.code) == (409, "REVISION_CONFLICT")
    assert svc.get_artifact(TENANT_A, aid)["current_revision"] == 2


def test_same_content_to_same_artifact_is_not_a_second_revision(svc):
    """(artifact_id, content_hash) 唯一：同字节同身份不能有两版。"""
    _, first, sha = _publish(svc, TENANT_A, _import_body("cmd_1"), "same")
    aid = first["artifact_id"]
    second = svc.create_import(TENANT_A, _import_body(
        "cmd_2", target_artifact_id=aid, expected_revision=1))
    with pytest.raises(ServiceError) as e:
        svc.publish(TENANT_A, second["import_id"], observed_size_bytes=4,
                    actual_sha256=sha, storage_key="k/x", storage_region="r")
    assert e.value.code == "REVISION_CONFLICT"


# ---- 去重（E28） -----------------------------------------------------------


def test_dedup_hit_only_within_scope(svc):
    """T28-36：同租户同 scope 去重命中；不同 scope 不暴露。"""
    _, first, sha = _publish(
        svc, TENANT_A, _import_body("cmd_1", scope={"project_id": "p_1"}), "dup")
    same_scope = _import_body("cmd_2", scope={"project_id": "p_1"})
    created = svc.create_import(TENANT_A, same_scope)
    hit = svc.publish(TENANT_A, created["import_id"], observed_size_bytes=3,
                      actual_sha256=sha, storage_key="k/other", storage_region="r")
    assert hit["dedup"] == {"hit": True, "artifact_id": first["artifact_id"]}
    # 后到的导入复用了先到的存储坐标
    rev = svc.get_revision(TENANT_A, hit["artifact_id"], 1)
    assert rev["content_hash"] == sha

    other_scope = _import_body("cmd_3", scope={"project_id": "p_2"})
    created = svc.create_import(TENANT_A, other_scope)
    miss = svc.publish(TENANT_A, created["import_id"], observed_size_bytes=3,
                       actual_sha256=sha, storage_key="k/other2", storage_region="r")
    assert miss["dedup"] == {"hit": False}


def test_dedup_never_crosses_tenants(svc):
    """T28-37：B 导入与 A 相同的字节，不可见 A 的存在，也不共享 blob 行。"""
    _, a_result, sha = _publish(svc, TENANT_A, _import_body("cmd_a"), "shared")
    body = _import_body("cmd_b", tenant_id=TENANT_B, scope={"project_id": "p_1"})
    created = svc.create_import(TENANT_B, body)
    b_result = svc.publish(TENANT_B, created["import_id"], observed_size_bytes=6,
                           actual_sha256=sha, storage_key="k/b", storage_region="r")
    assert b_result["dedup"] == {"hit": False}
    blobs = svc.conn.execute(
        "SELECT tenant_id FROM content_blob WHERE content_hash = ?", (sha,)).fetchall()
    assert {r["tenant_id"] for r in blobs} == {TENANT_A, TENANT_B}


def test_no_global_hash_index():
    """E28 的数据库级落点：不能存在不以 tenant_id 开头的 hash 唯一索引。"""
    conn = _connect()
    try:
        indexes = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index'").fetchall()
        for name, sql in indexes:
            if not sql or "content_hash" not in sql:
                continue
            assert "tenant_id" in sql.split("WHERE")[0], name
            assert sql.index("tenant_id") < sql.index("content_hash"), name
    finally:
        conn.close()


# ---- 跨租户（E28 / §8.5） ---------------------------------------------------


def test_cross_tenant_read_is_404_not_403(svc):
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "x")
    with pytest.raises(ServiceError) as e:
        svc.get_artifact(TENANT_B, first["artifact_id"])
    assert (e.value.status, e.value.code) == (404, "ARTIFACT_NOT_FOUND")
    # 不泄漏存在性：details 为空
    assert e.value.details == {}


def test_cross_tenant_import_target_is_404(svc):
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "x")
    with pytest.raises(ServiceError) as e:
        svc.create_import(TENANT_B, _import_body(
            "cmd_2", tenant_id=TENANT_B,
            target_artifact_id=first["artifact_id"], expected_revision=1))
    assert e.value.status == 404


def test_cross_scope_ticket_is_403(svc):
    """租户内跨 scope → 403 FORBIDDEN_SCOPE（与跨租户 404 区分）。"""
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1", scope={"project_id": "p_1"}), "x")
    with pytest.raises(ServiceError) as e:
        svc.issue_media_access_ticket(
            TENANT_A, {"artifact_ref": {"id": first["artifact_id"]},
                       "purpose": "qc", "scope_ref": {"project_id": "p_9"},
                       "ttl_seconds": 60}, principal_id="svc_a")
    assert (e.value.status, e.value.code) == (403, "FORBIDDEN_SCOPE")


# ---- tombstone（§6.4 / §3.5） ----------------------------------------------


def test_tombstone_then_read_returns_state_not_404(svc):
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "x")
    aid = first["artifact_id"]
    out = svc.tombstone(TENANT_A, aid, {"command_id": "cmd_t", "reason_code": "USER_REQUEST"},
                        requested_by="u_1")
    assert out["state"] == "TOMBSTONED"
    # 同租户能看出它曾存在（§6.2）
    got = svc.get_artifact(TENANT_A, aid)
    assert got["state"] == "TOMBSTONED" and "tombstoned_at" in got


def test_tombstoned_artifact_cannot_be_reused_as_target(svc):
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "x")
    aid = first["artifact_id"]
    svc.tombstone(TENANT_A, aid, {"command_id": "cmd_t"}, requested_by="u_1")
    with pytest.raises(ServiceError) as e:
        svc.create_import(TENANT_A, _import_body(
            "cmd_2", target_artifact_id=aid, expected_revision=1))
    assert (e.value.status, e.value.code) == (409, "ARTIFACT_TOMBSTONED")


def test_tombstone_does_not_revert(svc):
    """§3.5：TOMBSTONED 不回 AVAILABLE；重复 tombstone 不抛错也不改时间。"""
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "x")
    aid = first["artifact_id"]
    svc.tombstone(TENANT_A, aid, {"command_id": "cmd_t"}, requested_by="u_1")
    stamp = svc.get_artifact(TENANT_A, aid)["tombstoned_at"]
    svc.tombstone(TENANT_A, aid, {"command_id": "cmd_t2"}, requested_by="u_1")
    assert svc.get_artifact(TENANT_A, aid)["tombstoned_at"] == stamp
    assert svc.get_artifact(TENANT_A, aid)["state"] == "TOMBSTONED"


def test_tombstone_rejects_delete_bytes_until_orchestrator_exists(svc):
    """§4.8 未实现，不能返回一个假的 deletion_id。"""
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "x")
    with pytest.raises(ServiceError) as e:
        svc.tombstone(TENANT_A, first["artifact_id"],
                      {"command_id": "cmd_t", "delete_bytes": True},
                      requested_by="u_1")
    assert e.value.status == 422
    # 拒绝时不能留下半截状态
    assert svc.get_artifact(TENANT_A, first["artifact_id"])["state"] == "AVAILABLE"


def test_tombstone_revokes_live_tickets(svc):
    """§7.4：tombstone 撤销未到期 ticket，已签出的 URL 随之失效。"""
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "x")
    aid = first["artifact_id"]
    ticket = svc.issue_media_access_ticket(
        TENANT_A, {"artifact_ref": {"id": aid}, "purpose": "qc",
                   "scope_ref": {"project_id": "p_1"}, "ttl_seconds": 900},
        principal_id="svc_a")
    assert svc.is_ticket_live(TENANT_A, ticket["ticket_id"])
    svc.tombstone(TENANT_A, aid, {"command_id": "cmd_t"}, requested_by="u_1")
    assert not svc.is_ticket_live(TENANT_A, ticket["ticket_id"])


def test_ticket_for_tombstoned_artifact_is_409_not_404(svc):
    """§1.4：已停用 ≠ 从未存在。"""
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "x")
    aid = first["artifact_id"]
    svc.tombstone(TENANT_A, aid, {"command_id": "cmd_t"}, requested_by="u_1")
    with pytest.raises(ServiceError) as e:
        svc.issue_media_access_ticket(
            TENANT_A, {"artifact_ref": {"id": aid}, "purpose": "qc",
                       "scope_ref": {"project_id": "p_1"}, "ttl_seconds": 60},
            principal_id="svc_a")
    assert (e.value.status, e.value.code) == (409, "ARTIFACT_TOMBSTONED")


# ---- 校验顺序与错误码（§6.1 / §6.7） ----------------------------------------


@pytest.mark.parametrize("mutate,status,code", [
    (lambda b: {**b, "content_class": "nope"}, 400, "INVALID_REQUEST"),
    (lambda b: {**b, "source": {"kind": "ftp"}}, 400, "INVALID_REQUEST"),
    (lambda b: {**b, "expected_sha256": "zz"}, 400, "INVALID_REQUEST"),
    (lambda b: {**b, "scope_ref": {}}, 400, "INVALID_REQUEST"),
    (lambda b: {**b, "retention": {"policy_ref": 5}}, 400, "INVALID_REQUEST"),
])
def test_request_validation(svc, mutate, status, code):
    with pytest.raises(ServiceError) as e:
        svc.create_import(TENANT_A, mutate(_import_body("cmd_1")))
    assert (e.value.status, e.value.code) == (status, code)


def test_tenant_mismatch_is_rejected(svc):
    body = _import_body("cmd_1", tenant_id=TENANT_B)
    with pytest.raises(ServiceError) as e:
        svc.create_import(TENANT_A, body)
    assert e.value.status == 403


def test_expired_url_rejected_before_any_fetch(svc):
    """E23：TTL 已过 → 409 URL_EXPIRED，且没有落 import 行。"""
    body = _import_body("cmd_1", kind="provider_url",
                        provider_url="https://cdn.provider.example/a?sig=1",
                        provider_url_expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(ServiceError) as e:
        svc.create_import(TENANT_A, body)
    assert (e.value.status, e.value.code) == (409, "URL_EXPIRED")
    assert svc.conn.execute("SELECT count(*) FROM artifact_import").fetchone()[0] == 0


@pytest.mark.parametrize("url", [
    "http://cdn.provider.example/a",           # 非 https
    "https://127.0.0.1/a",                     # IP 字面量
    "https://evil.example/a",                  # 不在 allowlist
])
def test_unsafe_urls_rejected_before_network(svc, url):
    body = _import_body("cmd_1", kind="provider_url", provider_url=url,
                        provider_url_expires_at="2099-01-01T00:00:00Z")
    with pytest.raises(ServiceError) as e:
        svc.create_import(TENANT_A, body)
    assert (e.value.status, e.value.code) == (422, "UNSAFE_MEDIA")


def test_empty_allowlist_refuses_every_host():
    """§4.2 fail closed：没配 allowlist 时一个 host 都不接。"""
    conn = _connect()
    try:
        s = ArtifactIngestService(conn)
        with pytest.raises(ServiceError) as e:
            s.create_import(TENANT_A, _import_body(
                "cmd_1", kind="provider_url",
                provider_url="https://cdn.provider.example/a",
                provider_url_expires_at="2099-01-01T00:00:00Z"))
        assert e.value.code == "UNSAFE_MEDIA"
    finally:
        conn.close()


def test_observed_size_over_cap_rejects_import(svc):
    created = svc.create_import(
        TENANT_A, _import_body("cmd_1", content_class="generated_image"))
    with pytest.raises(ServiceError) as e:
        svc.publish(TENANT_A, created["import_id"],
                    observed_size_bytes=10 ** 10, actual_sha256=_sha("x"),
                    storage_key="k", storage_region="r")
    assert e.value.code == "SIZE_EXCEEDED"
    assert svc.get_import(TENANT_A, created["import_id"])["state"] == "REJECTED_SIZE"


def test_hash_mismatch_rejects_and_marks_import(svc):
    created = svc.create_import(
        TENANT_A, _import_body("cmd_1", expected_sha256=_sha("expected")))
    with pytest.raises(ServiceError) as e:
        svc.publish(TENANT_A, created["import_id"], observed_size_bytes=5,
                    actual_sha256=_sha("actual"), storage_key="k", storage_region="r")
    assert (e.value.status, e.value.code) == (422, "HASH_MISMATCH")
    got = svc.get_import(TENANT_A, created["import_id"])
    assert got["state"] == "HASH_MISMATCH"
    # 没有产出 artifact
    assert svc.conn.execute("SELECT count(*) FROM artifact").fetchone()[0] == 0


def test_mime_mismatch_rejects(svc):
    body = {**_import_body("cmd_1"), "mime_declared": "video/mp4"}
    created = svc.create_import(TENANT_A, body)
    with pytest.raises(ServiceError) as e:
        svc.publish(TENANT_A, created["import_id"], observed_size_bytes=1,
                    actual_sha256=_sha("x"), storage_key="k", storage_region="r",
                    probe={"mime_type": "text/html"})
    assert e.value.code == "FORMAT_MISMATCH"


# ---- 事件（E32） ------------------------------------------------------------


def _validate(instance, schema):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(instance, schema)


def test_emitted_payload_matches_published_schema(svc):
    """T28-40：落进 outbox 的 data 必须通过 contracts 里的 payload schema。

    这条不能用方案 §6.8 的示例代替——示例本身缺 schema_version。
    """
    _, result, sha = _publish(
        svc, TENANT_A, _import_body("cmd_1"),
        "abc", region="ap-northeast-1",
        probe={"mime_type": "video/mp4", "probed": True, "duration_ms": 4000,
               "width_px": 1080, "height_px": 1920,
               "frame_rate_num": 24, "frame_rate_den": 1})
    row = svc.conn.execute(
        "SELECT payload FROM outbox WHERE event_id = ?",
        (result["outbox_event_id"],)).fetchone()
    envelope = json.loads(row["payload"])
    _validate(envelope["data"], json.loads(PAYLOAD_SCHEMA.read_text(encoding="utf-8")))

    data = envelope["data"]
    assert data["artifact_ref"] == {"service_id": "S28", "id": result["artifact_id"],
                                   "revision": 1, "content_hash": sha}
    assert data["source"] == {"kind": "internal_object"}
    assert set(data) >= {"schema_version", "artifact_ref", "content_class",
                         "size_bytes", "source", "dedup", "derivative_hint"}


def test_event_sequence_equals_revision(svc):
    """§6.8：aggregate_sequence = revision。"""
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "v1")
    body = _import_body("cmd_2", target_artifact_id=first["artifact_id"],
                        expected_revision=1)
    _, second, _ = _publish(svc, TENANT_A, body, "v2")
    row = svc.conn.execute(
        "SELECT sequence, subject, aggregate_id FROM outbox WHERE event_id = ?",
        (second["outbox_event_id"],)).fetchone()
    assert row["sequence"] == 2
    assert row["subject"] == row["aggregate_id"] == first["artifact_id"]


def test_event_is_written_in_the_same_transaction(svc):
    """outbox 与事实同事务：publish 抛错时不留事件。"""
    created = svc.create_import(TENANT_A, _import_body(
        "cmd_1", expected_sha256=_sha("other")))
    with pytest.raises(ServiceError):
        svc.publish(TENANT_A, created["import_id"], observed_size_bytes=1,
                    actual_sha256=_sha("x"), storage_key="k", storage_region="r")
    assert svc.conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_transaction_actually_rolls_back(conn):
    """证明 Transaction 真的开了事务。

    `with conn:` 在 isolation_level=None 下是 no-op；若这里改成它，下面的断言会
    立刻失败——这正是本轮不用 S08 那种写法的原因。
    """
    conn.execute("INSERT INTO artifact (artifact_id, tenant_id, home_region, home_cell,"
                 " content_class, current_revision, state, created_at, updated_at)"
                 " VALUES ('art_x', 't', 'r', 'c', 'generated_video', 1,"
                 " 'AVAILABLE', 'now', 'now')")
    with pytest.raises(RuntimeError):
        with Transaction(conn):
            conn.execute("DELETE FROM artifact WHERE artifact_id = 'art_x'")
            raise RuntimeError("boom")
    assert conn.execute(
        "SELECT count(*) FROM artifact WHERE artifact_id = 'art_x'").fetchone()[0] == 1


def test_republish_of_published_import_is_not_a_second_event(svc):
    _, first, _ = _publish(svc, TENANT_A, _import_body("cmd_1"), "v1")
    again = svc.publish(TENANT_A, first["import_id"], observed_size_bytes=2,
                        actual_sha256=_sha("v1"), storage_key="k2", storage_region="r")
    assert again["replayed"] is True
    assert again["revision"] == first["revision"]
    assert svc.conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


# ---- HTTP 层 ---------------------------------------------------------------


def _http_client(monkeypatch, tmp_path):
    """每个用例一个独立库：app.py 在 import 时建表，所以要先指好路径再加载。

    不能 `import app`：别的服务也有 `api/app.py` 且可能先入 sys.modules，
    平名导入会拿到另一个服务的路由表。
    """
    monkeypatch.setenv("S28_DB", str(tmp_path / "s28.db"))
    monkeypatch.setenv("S28_HOST_ALLOWLIST", "cdn.provider.example")
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s28_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # 显式注册：`exec_module` 不会替我们写 sys.modules，而用例要从这里拿 service
    sys.modules["s28_api_app"] = module
    return TestClient(module.app)


def test_http_import_returns_202_and_replays(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    body = _import_body("cmd_1")
    headers = {"Idempotency-Key": "cmd_1", "X-Tenant-Id": TENANT_A}

    first = client.post("/artifact-imports", json=body, headers=headers)
    assert first.status_code == 202, first.text
    assert first.json()["state"] == "ACCEPTED"
    assert first.json()["artifact_id"] is None

    again = client.post("/artifact-imports", json=body, headers=headers)
    assert again.status_code == 202
    assert again.json()["replayed"] is True
    assert again.json()["import_id"] == first.json()["import_id"]


def test_http_same_key_different_body_is_409(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    headers = {"Idempotency-Key": "cmd_1", "X-Tenant-Id": TENANT_A}
    client.post("/artifact-imports", json=_import_body("cmd_1"), headers=headers)
    changed = _import_body("cmd_1", content_class="generated_image")
    response = client.post("/artifact-imports", json=changed, headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_http_command_id_mismatch_is_400(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    response = client.post(
        "/artifact-imports", json=_import_body("cmd_body"),
        headers={"Idempotency-Key": "cmd_header", "X-Tenant-Id": TENANT_A})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"


def test_http_missing_idempotency_key_is_400(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    response = client.post("/artifact-imports", json=_import_body("cmd_1"),
                           headers={"X-Tenant-Id": TENANT_A})
    assert response.status_code == 400


def test_http_cross_tenant_get_is_404(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    # 直接经 service 造一条属于 A 的数据，再用 B 的头读
    module = sys.modules["s28_api_app"]
    _, first, _ = _publish(module.service, TENANT_A, _import_body("cmd_1"), "x")
    response = client.get(f"/artifacts/{first['artifact_id']}",
                          headers={"X-Tenant-Id": TENANT_B})
    assert response.status_code == 404
    assert response.json()["code"] == "ARTIFACT_NOT_FOUND"
    assert response.json()["details"] == {}


def test_http_tombstone_then_read(monkeypatch, tmp_path):
    client = _http_client(monkeypatch, tmp_path)
    module = sys.modules["s28_api_app"]
    _, first, _ = _publish(module.service, TENANT_A, _import_body("cmd_1"), "x")
    aid = first["artifact_id"]
    headers = {"Idempotency-Key": "cmd_t", "X-Tenant-Id": TENANT_A}
    response = client.post(f"/artifacts/{aid}:tombstone",
                           json={"command_id": "cmd_t", "reason_code": "USER_REQUEST"},
                           headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "TOMBSTONED"

    got = client.get(f"/artifacts/{aid}", headers={"X-Tenant-Id": TENANT_A})
    assert got.status_code == 200
    assert got.json()["state"] == "TOMBSTONED"
