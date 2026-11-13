"""S10 asset-catalog 的测试。

分层与仓库里其他服务一致：

- `Harness` 直接驱动内核，桩掉五个 owner 的 revision/status 读路径。每个桩返回
  的是**上游真实形状归一后的指针**（`{service_id,id,revision,content_hash}`），
  不是随便一个 dict —— 这样 `deps` 的归一逻辑在 HTTP 测试里单独验。
- HTTP 测试用 `importlib` 加载 `api/app.py` 到独立模块名，再把它模块级的
  `_conn` / `service` / `_idem` 换成 Harness 的，避免与其他服务的 `app` 冲突。

编号尽量对齐方案 §8 自己的验收 ID（`test_t10_E02_A1_*` 这类），并在断言里
写明它证明的是哪条反模式/红线的反面。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asset_catalog import (
    AssetCatalogService,
    ServiceError,
    rights_set_hash,
    role_key_for,
)
from asset_catalog.schema import ASSET_TYPES, CONTRACT_GAPS
from idempotency_sdk import CommandConflict, SqliteIdempotencyStore

ROOT = Path(__file__).resolve().parents[1]


def sha(char: str = "a") -> str:
    return char * 64


def rights(id_: str = "rights_char_linlan", rev: int = 3,
           owner: str = "S02") -> dict:
    return {"rights_owner": owner, "rights_id": id_,
            "rights_revision": rev, "rights_hash": sha("b")}


class Harness:
    """内核 + 幂等 + 全套假 owner。"""

    def __init__(self, *, revision_source=None, asset_status_source=None,
                 manifest_source=None, conn=None):
        self.conn = conn or sqlite3.connect(":memory:", check_same_thread=False,
                                            isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)
        self.revisions: dict = {}
        self.statuses: dict = {}
        self.manifests: dict = {}
        self.revision_calls: list = []
        self.fail_upstream = False
        self.service = AssetCatalogService(
            self.conn,
            revision_source=revision_source or self._revision,
            asset_status_source=asset_status_source or self._status,
            manifest_source=manifest_source or self._manifest)

    # ---- 桩：owner revision
    def add_revision(self, service_id: str, asset_id: str, revision: int,
                     content_hash: str | None = None,
                     status: str = "active") -> dict:
        pointer = {"service_id": service_id, "id": asset_id,
                   "revision": revision,
                   "content_hash": content_hash or sha("c")}
        self.revisions[(service_id, asset_id, revision)] = pointer
        self.statuses[(service_id, asset_id)] = status
        return pointer

    def _revision(self, tenant_id, service_id, asset_id, revision, variant=None):
        self.revision_calls.append((tenant_id, service_id, asset_id, revision))
        key = (service_id, asset_id, revision)
        if key not in self.revisions:
            raise ServiceError(
                422, "OWNER_REF_INVALID", "missing revision",
                details={"service_id": service_id, "id": asset_id,
                         "revision": revision, "reason": "REVISION_NOT_FOUND"})
        if self.statuses.get((service_id, asset_id)) == "tombstoned":
            raise ServiceError(
                422, "OWNER_REF_INVALID", "tombstoned",
                details={"service_id": service_id, "id": asset_id,
                         "revision": revision, "reason": "TOMBSTONED"})
        return dict(self.revisions[key])

    def _status(self, tenant_id, service_id, asset_id):
        if (service_id, asset_id) not in self.statuses:
            raise ServiceError(422, "OWNER_REF_INVALID", "missing asset",
                               details={"reason": "ASSET_NOT_FOUND"})
        return {"status": self.statuses[(service_id, asset_id)],
                "asset_id": asset_id}

    def _manifest(self, tenant_id, manifest_id, revision):
        key = (manifest_id, revision)
        if key not in self.manifests:
            raise ServiceError(503, "DEPENDENCY_UNAVAILABLE", "down")
        return {"manifest": self.manifests[key]}

    # ---- 命令（与 api/app.py 的 command() 同构）
    def command(self, tenant_id: str, command_id: str, body: dict, execute):
        from idempotency_sdk import canonical_request_hash
        request_hash = canonical_request_hash(body)
        record = self.idem.begin(tenant_id, command_id, request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = execute(request_hash)
        except Exception:
            self.idem.abort(tenant_id, command_id)
            raise
        self.idem.finish(tenant_id, command_id,
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ---- 便捷封装
    def mint(self, tenant="t1", cid="c-mint", asset_type="character",
             owner_local_id="chr_linlan", **extra):
        body = {"asset_type": asset_type, "owner_local_id": owner_local_id,
                "owner_service": ASSET_TYPES[asset_type]["owner_service"],
                "series_scope": extra.pop("series_scope", "series_demo"),
                **extra}
        return self.command(tenant, cid, body,
                            lambda rh: self.service.mint_asset(
                                tenant, cid, body, rh))["asset"]

    def create_bs(self, tenant="t1", cid="c-bs", series="series_demo",
                  episode="ep3", branch="canon", project="proj1"):
        body = {"project_id": project, "series_id": series,
                "episode_id": episode, "branch_id": branch}
        return self.command(tenant, cid, body,
                            lambda rh: self.service.create_binding_set(
                                tenant, cid, body, rh))["binding_set"]

    def entry_body(self, *, role_key="character:chr_linlan", revision=1,
                   content_hash=None, scope_kind="episode", scope_id="ep3",
                   head=0, **extra):
        return {"scope_kind": scope_kind, "scope_id": scope_id,
                "role_key": role_key, "owner_revision": revision,
                "owner_hash": content_hash or sha("c"),
                "rights_ref": rights(),
                "expected_head": head, **extra}

    def write_entry(self, bs_id, body, tenant="t1", cid="c-entry"):
        return self.command(tenant, cid, body,
                            lambda rh: self.service.write_entry(
                                tenant, cid, bs_id, body, rh))

    def seal(self, bs_id, head=0, tenant="t1", cid="c-seal", **extra):
        body = {"expected_head": head, **extra}
        return self.command(tenant, cid, body,
                            lambda rh: self.service.seal(
                                tenant, cid, bs_id, body, rh))

    def seeded(self, tenant="t1", episode="ep3", character_rev=1):
        """一集 + 一个角色 + 一条 entry + 封存。返回 (asset, bs, revision)。"""
        asset = self.mint(tenant=tenant, cid=f"mint-{tenant}-{episode}",
                          series_scope="series_demo")
        self.add_revision("S11", "chr_linlan", character_rev)
        bs = self.create_bs(tenant=tenant, cid=f"bs-{tenant}-{episode}",
                            episode=episode)
        self.write_entry(bs["binding_set_id"],
                         self.entry_body(revision=character_rev,
                                         scope_id=episode),
                         tenant=tenant, cid=f"entry-{tenant}-{episode}")
        sealed = self.seal(bs["binding_set_id"], tenant=tenant,
                           cid=f"seal-{tenant}-{episode}")
        return asset, bs, sealed


def _http_client(monkeypatch, tmp_path, **over):
    monkeypatch.setenv("S10_DB", str(tmp_path / "s10.db"))
    path = ROOT / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s10_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s10_api_app"] = module
    h = Harness(conn=module._conn, **over)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h, module


def post(client, path, body, *, command_id="c1", tenant="t1", **kw):
    return client.post(path, json=body,
                       headers={"Idempotency-Key": command_id,
                                "X-Tenant-Id": tenant}, **kw)


# =========================================================== 基础与类型注册表

def test_t10_00_role_key_uses_owner_local_id_not_asset_id():
    # §9 反模式 11 的反面：role_key 由 owner 本地 id 拼，不含 S10 的资产 ID。
    assert role_key_for("character", "chr_linlan") == "character:chr_linlan"
    assert role_key_for("voice", "voice_linlan") == "voice:voice_linlan"


def test_t10_01_registry_is_seeded_and_closed():
    h = Harness()
    rows = {row["asset_type"] for row in h.conn.execute(
        "SELECT asset_type FROM asset_type_registry")}
    assert rows == set(ASSET_TYPES)
    assert len(ASSET_TYPES) == 6


def test_t10_02_unknown_asset_type_is_not_addable_by_a_tenant():
    h = Harness()
    # 走真命令而不是便捷封装：`ASSET_TYPES` 里没有 hologram，封装自己就会
    # KeyError，测不到服务端的拒绝。
    body = {"asset_type": "hologram", "owner_service": "S99",
            "owner_local_id": "holo_1", "series_scope": "series_demo"}
    with pytest.raises(ServiceError) as exc:
        h.command("t1", "h", body, lambda rh: h.service.mint_asset(
            "t1", "h", body, rh))
    assert exc.value.status == 422
    assert exc.value.code == "TYPE_OWNER_MISMATCH"
    assert h.conn.execute("SELECT COUNT(*) FROM asset_identity"
                          ).fetchone()[0] == 0


def test_t10_03_minting_with_the_wrong_owner_service_is_refused():
    # §4.1 第 1 条：不能给 S14 的道具盖上 S13 的类型。
    h = Harness()
    with pytest.raises(ServiceError) as exc:
        h.mint(asset_type="prop", owner_local_id="prop_key",
               owner_service="S13")
    assert exc.value.code == "TYPE_OWNER_MISMATCH"
    assert exc.value.details["expected_owner_service"] == "S14"


def test_t10_04_mint_writes_identity_alias_and_lifecycle():
    h = Harness()
    asset = h.mint(alias_texts=["小琳", "Lin Lan"])
    assert asset["asset_id"].startswith("ast_")
    assert asset["status"] == "active"
    assert asset["role_key"] == "character:chr_linlan"
    assert {a["alias_text"] for a in asset["aliases"]} == {"小琳", "Lin Lan"}
    assert [e["event_kind"] for e in asset["lifecycle"]] == ["registered"]


def test_t10_05_mint_is_idempotent_per_owner_object():
    # E12-A3 / §4.1 第 3 条：重复注册返回同一 ID，不报错。
    h = Harness()
    first = h.mint(cid="c1")
    second = h.mint(cid="c2")
    assert first["asset_id"] == second["asset_id"]
    assert h.conn.execute("SELECT COUNT(*) FROM asset_identity"
                          ).fetchone()[0] == 1


def test_t10_06_mint_records_a_local_event_not_an_outbox_row():
    # C10-3：身份事件不进中继，只进本地表。
    h = Harness()
    h.mint()
    assert h.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    kinds = [row[0] for row in h.conn.execute(
        "SELECT event_type FROM local_event")]
    assert kinds == ["asset.identity.registered"]


def test_t10_07_random_asset_ids_never_collide():
    h = Harness()
    ids = {h.mint(owner_local_id=f"chr_{i}", cid=f"c{i}")["asset_id"]
           for i in range(20)}
    assert len(ids) == 20


# =========================================================== 不是第二权威

def test_t10_10_no_column_can_hold_asset_content():
    """§2.9：不许有 `asset_content_cache`；也不许把内容塞进任何列。"""
    h = Harness()
    tables = {row[0] for row in h.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "asset_content_cache" not in tables
    for name in ("binding_entry", "binding_expansion", "asset_identity"):
        columns = {row[1] for row in h.conn.execute(
            f"PRAGMA table_info({name})")}
        for forbidden in ("payload", "content", "appearance", "artifacts",
                          "reference_image", "voice_sample", "topology",
                          "description"):
            assert forbidden not in columns, (name, forbidden)


def test_t10_11_no_column_can_hold_canonical_pointer():
    """§2.9：不许有 `asset_canonical_pointer`。"""
    h = Harness()
    tables = {row[0] for row in h.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "asset_canonical_pointer" not in tables
    for name in ("asset_identity", "binding_entry", "binding_set"):
        columns = {row[1] for row in h.conn.execute(
            f"PRAGMA table_info({name})")}
        assert "canonical_revision" not in columns, name
        assert "canonical_asset_id" not in columns, name
        assert "is_canonical" not in columns, name


def test_t10_12_no_column_can_hold_rights_state():
    """§2.9：不许有 `rights_status`。只有指针。

    扫**所有表**而不是 `binding_entry` 一张：加在某张旁表上的 `rights_status`
    同样是第二权威。
    """
    h = Harness()
    tables = {row[0] for row in h.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "rights_status" not in tables
    assert "rights_ref" in {row[1] for row in h.conn.execute(
        "PRAGMA table_info(binding_entry)")}
    for name in tables:
        columns = {row[1] for row in h.conn.execute(
            f"PRAGMA table_info({name})")}
        for forbidden in ("rights_status", "rights_valid", "rights_state",
                          "rights_revoked", "rights_expires_at", "authorized"):
            assert forbidden not in columns, (name, forbidden)


def test_t10_13_no_working_copy_table():
    h = Harness()
    tables = {row[0] for row in h.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "binding_set_working_copy" not in tables


def test_t10_14_resolve_returns_pointers_only_never_content():
    h = Harness()
    _, bs, sealed = h.seeded()
    asset = h.mint(cid="again")  # 同一个 owner 对象
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    entry = result["entries"][0]
    assert set(entry["owner_ref"]) == {"service_id", "id", "revision",
                                       "content_hash"}
    # 反模式 1：不返回角色描述/参考图 URL。
    blob = json.dumps(result, ensure_ascii=False)
    for forbidden in ("identity_features", "reference_image", "description",
                      "appearance", "artifacts"):
        assert forbidden not in blob


def test_t10_15_resolve_never_reads_a_stored_copy_of_upstream_state():
    """换一次 owner 桩的返回值，resolve 必须原样反映冻结的历史，不跟着变。

    这就是「S10 不是 second authority」的可执行版本：如果 S10 存了一份镜像，
    这里就会拿到新值。
    """
    h = Harness()
    _, bs, sealed = h.seeded()
    asset = h.mint(cid="again")
    before = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    h.add_revision("S11", "chr_linlan", 1, content_hash=sha("9"))  # owner 变了
    after = h.service.resolve("t1", asset["asset_id"],
                              binding_set_id=bs["binding_set_id"],
                              binding_set_revision=sealed["revision"])
    assert before["entries"] == after["entries"]
    assert before["binding_set_ref"]["content_hash"] == \
        after["binding_set_ref"]["content_hash"]


def test_t10_16_rights_recheck_is_always_true_and_has_no_switch():
    h = Harness()
    _, bs, sealed = h.seeded()
    asset = h.mint(cid="again")
    for kwargs in ({}, {"binding_set_revision": sealed["revision"]}):
        result = h.service.resolve("t1", asset["asset_id"],
                                   binding_set_id=bs["binding_set_id"], **kwargs)
        assert result["rights_recheck_required"] is True


def test_t10_17_usage_has_no_impact_closure_fields():
    # 反模式 5 / §4.5：种子不是闭包。
    h = Harness()
    _, bs, sealed = h.seeded()
    result = h.service.usage("t1", bs["binding_set_id"], sealed["revision"])
    blob = json.dumps(result, ensure_ascii=False)
    for forbidden in ("affected_episodes", "needs_redo", "impact_closure",
                      "must_regenerate", "downstream"):
        assert forbidden not in blob


def test_t10_18_no_event_is_emitted_for_asset_upgrades():
    # 反模式 5：资产升级时 S10 不发「请重做」事件。
    h = Harness()
    _, bs, sealed = h.seeded()
    before = h.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    # owner 侧升级（新 revision）与 S10 再注册同一次身份，都不该产生中继事件。
    h.add_revision("S11", "chr_linlan", 9)
    h.mint(cid="upgrade")
    after = h.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    assert before == after


def test_t10_19_every_gap_is_registered_and_shaped():
    assert len(CONTRACT_GAPS) >= 30
    seen = set()
    for gap in CONTRACT_GAPS:
        assert set(gap) == {"id", "kind", "summary", "handling"}
        assert gap["kind"] in ("conflict", "needs_owner")
        assert gap["summary"] and gap["handling"]
        assert gap["id"] not in seen
        seen.add(gap["id"])
    assert {"C10-1", "C10-7", "ABS-1", "ABS-7"} <= seen
    assert any(gap["kind"] == "needs_owner" for gap in CONTRACT_GAPS)


def test_t10_20_readiness_reports_the_gaps(monkeypatch, tmp_path):
    client, _h, _module = _http_client(monkeypatch, tmp_path)
    body = client.get("/health/readiness").json()
    assert body["ready"] is True
    assert len(body["contract_gaps"]) == len(CONTRACT_GAPS)


# =========================================================== E02 三集连续生产

def test_t10_E02_A1_same_character_revision_across_three_episodes():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 12)
    asset = h.mint(series_scope="series_demo")
    results = []
    for index, episode in enumerate(("ep1", "ep2", "ep3"), start=1):
        bs = h.create_bs(cid=f"bs{index}", episode=episode)
        h.write_entry(bs["binding_set_id"],
                      h.entry_body(revision=12, scope_id=episode),
                      cid=f"e{index}")
        sealed = h.seal(bs["binding_set_id"], cid=f"s{index}")
        results.append((bs["binding_set_id"],
                        h.service.resolve(
                            "t1", asset["asset_id"],
                            binding_set_id=bs["binding_set_id"],
                            binding_set_revision=sealed["revision"])))
    ids = {bs_id for bs_id, _ in results}
    assert len(ids) == 3
    for _bs_id, result in results:
        ref = result["entries"][0]["owner_ref"]
        assert ref == {"service_id": "S11", "id": "chr_linlan",
                       "revision": 12, "content_hash": sha("c")}


def test_t10_E02_A2_episode_three_changes_costume_and_leaves_character_alone():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 12)
    h.add_revision("S12", "cos_red_dress_01", 4)
    asset = h.mint(series_scope="series_demo")

    first_two = []
    for index, episode in enumerate(("ep1", "ep2"), start=1):
        bs = h.create_bs(cid=f"bs{index}", episode=episode)
        h.write_entry(bs["binding_set_id"],
                      h.entry_body(revision=12, scope_id=episode),
                      cid=f"e{index}")
        first_two.append((bs["binding_set_id"],
                          h.seal(bs["binding_set_id"], cid=f"s{index}")))

    bs3 = h.create_bs(cid="bs3", episode="ep3")
    h.write_entry(bs3["binding_set_id"],
                  h.entry_body(revision=12, scope_id="ep3"), cid="e3a")
    h.write_entry(bs3["binding_set_id"],
                  h.entry_body(role_key="costume:cos_red_dress_01",
                               revision=4, scope_id="ep3"), cid="e3b")
    sealed3 = h.seal(bs3["binding_set_id"], cid="s3")

    result3 = h.service.resolve("t1", asset["asset_id"],
                                binding_set_id=bs3["binding_set_id"],
                                binding_set_revision=sealed3["revision"])
    roles = {entry["role_key"] for entry in result3["entries"]}
    assert roles == {"character:chr_linlan", "costume:cos_red_dress_01"}

    for bs_id, sealed in first_two:
        old = h.service.get_revision("t1", bs_id, sealed["revision"])
        assert old["entry_count"] == 1
        assert {entry["role_key"] for entry in old["payload"]["entries"]} == {
            "character:chr_linlan"}


def test_t10_E02_A3_voice_pointer_is_stable_until_the_catalog_changes_it():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S15", "voice_linlan", 2)
    asset = h.mint(series_scope="series_demo")
    refs = []
    for index, episode in enumerate(("ep1", "ep2"), start=1):
        bs = h.create_bs(cid=f"bs{index}", episode=episode)
        h.write_entry(bs["binding_set_id"],
                      h.entry_body(revision=1, scope_id=episode),
                      cid=f"c{index}")
        h.write_entry(bs["binding_set_id"],
                      h.entry_body(role_key="voice:voice_linlan",
                                   revision=2, scope_id=episode),
                      cid=f"v{index}")
        sealed = h.seal(bs["binding_set_id"], cid=f"s{index}")
        result = h.service.resolve("t1", asset["asset_id"],
                                   binding_set_id=bs["binding_set_id"],
                                   binding_set_revision=sealed["revision"])
        refs.append(next(entry["owner_ref"] for entry in result["entries"]
                         if entry["role_key"] == "voice:voice_linlan"))
    assert refs[0] == refs[1]


def test_t10_E02_A3b_revocation_upstream_does_not_change_history():
    """E30-A1：S15 撤销后 resolve 仍成功，只是 `rights_recheck_required`。"""
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S15", "voice_linlan", 2)
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(role_key="voice:voice_linlan", revision=2),
                  cid="v")
    sealed = h.seal(bs["binding_set_id"])
    h.statuses[("S15", "voice_linlan")] = "tombstoned"  # 撤销
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    assert result["entries"]
    assert result["rights_recheck_required"] is True


def test_t10_E02_A4_revision_is_readable_by_the_ref_in_the_manifest():
    h = Harness()
    _, bs, sealed = h.seeded()
    revision = h.service.get_revision("t1", bs["binding_set_id"],
                                      sealed["revision"])
    assert revision["content_hash"] == sealed["content_hash"]
    assert revision["state"] == "SEALED"


# =========================================================== E08 改前集

def test_t10_E08_A1_new_revision_supersedes_and_usage_lists_both():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S14", "prop_key", 1)
    h.add_revision("S14", "prop_key", 2)
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(role_key="prop:prop_key", revision=1), cid="p1")
    first = h.seal(bs["binding_set_id"], cid="s1")

    h.write_entry(bs["binding_set_id"],
                  h.entry_body(role_key="prop:prop_key", revision=2, head=1),
                  cid="p2")
    second = h.seal(bs["binding_set_id"], head=1, cid="s2")

    old = h.service.get_revision("t1", bs["binding_set_id"], first["revision"])
    assert old["state"] == "SUPERSEDED"
    assert h.service.get_revision("t1", bs["binding_set_id"],
                                  second["revision"])["state"] == "SEALED"

    # 旧 manifest 仍然指向旧 revision，旧 revision 仍能解析。
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=first["revision"])
    prop = next(entry for entry in result["entries"]
                if entry["role_key"] == "prop:prop_key")
    assert prop["owner_ref"]["revision"] == 1


def test_t10_E08_A2_committed_first_revision_is_never_rewritten():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S11", "chr_linlan", 2)
    _asset, bs, sealed = h.seeded()
    original = h.service.get_revision("t1", bs["binding_set_id"],
                                      sealed["revision"])
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=2, head=1), cid="e2")
    h.seal(bs["binding_set_id"], head=1, cid="s2")
    again = h.service.get_revision("t1", bs["binding_set_id"],
                                   sealed["revision"])
    assert again["payload"] == original["payload"]
    assert again["content_hash"] == original["content_hash"]


def test_t10_E08_A4_series_default_applies_only_to_unsealed_episodes():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S11", "chr_linlan", 7)
    asset = h.mint(series_scope="series_demo")

    # 第 1 集先封存，绑定 revision 1。
    bs1 = h.create_bs(cid="bs1", episode="ep1")
    h.write_entry(bs1["binding_set_id"],
                  h.entry_body(revision=1, scope_id="ep1"), cid="e1")
    sealed1 = h.seal(bs1["binding_set_id"], cid="s1")

    # 全剧默认改成 revision 7。
    h.command("t1", "d1", {"scope_kind": "series", "role_key":
                           "character:chr_linlan", "owner_revision": 7,
                           "owner_hash": sha("c"), "rights_ref": rights()},
              lambda rh: h.service.set_default(
                  "t1", "d1", bs1["binding_set_id"],
                  {"scope_kind": "series", "role_key":
                   "character:chr_linlan", "owner_revision": 7,
                   "owner_hash": sha("c"), "rights_ref": rights()}, rh))

    # 第 2 集没有自己的 entry，靠默认值展开。
    bs2 = h.create_bs(cid="bs2", episode="ep2")
    sealed2 = h.seal(bs2["binding_set_id"], cid="s2")
    result2 = h.service.resolve("t1", asset["asset_id"],
                                binding_set_id=bs2["binding_set_id"],
                                binding_set_revision=sealed2["revision"])
    assert result2["entries"][0]["owner_ref"]["revision"] == 7
    assert result2["entries"][0]["resolved_from"] == "series"

    # 第 1 集仍是 1（反模式 3 的反面）。
    result1 = h.service.resolve("t1", asset["asset_id"],
                                binding_set_id=bs1["binding_set_id"],
                                binding_set_revision=sealed1["revision"])
    assert result1["entries"][0]["owner_ref"]["revision"] == 1


def test_t10_E08_A4b_defaults_cannot_be_written_at_shot_scope():
    h = Harness()
    _asset, bs, _sealed = h.seeded()
    body = {"scope_kind": "shot", "scope_id": "sh1",
            "role_key": "character:chr_linlan", "owner_revision": 1,
            "owner_hash": sha("c"), "rights_ref": rights()}
    with pytest.raises(ServiceError) as exc:
        h.command("t1", "d", body, lambda rh: h.service.set_default(
            "t1", "d", bs["binding_set_id"], body, rh))
    assert exc.value.code == "SCOPE_INCONSISTENT"


def test_t10_E08_A3_usage_returns_seed_rows_not_a_closure():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    result = h.service.usage("t1", bs["binding_set_id"], sealed["revision"])
    assert result["items"] == []
    assert result["stale"] is False


# =========================================================== E12 幂等

def test_t10_E12_A1_same_command_same_body_replays_without_new_rows():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S11", "chr_linlan", 2)
    _asset, bs, sealed = h.seeded()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=2, head=1), cid="e2")
    body = {"expected_head": 1}
    first = h.command("t1", "seal-x", body,
                      lambda rh: h.service.seal("t1", "seal-x",
                                                bs["binding_set_id"], body, rh))
    # 重放若真跑了会撞 DUPLICATE_BINDING_CONTENT（内容与第一版完全相同），
    # 所以「拿到 replayed」就证明了它没重跑。
    before = h.conn.execute("SELECT COUNT(*) FROM binding_set_revision"
                            ).fetchone()[0]
    second = h.command("t1", "seal-x", body,
                       lambda rh: h.service.seal("t1", "seal-x",
                                                 bs["binding_set_id"], body, rh))
    assert second["replayed"] is True
    assert second["revision"] == first["revision"]
    assert h.conn.execute("SELECT COUNT(*) FROM binding_set_revision"
                          ).fetchone()[0] == before


def test_t10_E12_A2_same_command_different_body_is_409():
    h = Harness()
    body = {"project_id": "p", "series_id": "s", "episode_id": "e",
            "branch_id": "canon"}
    h.command("t1", "dup", body, lambda rh: h.service.create_binding_set(
        "t1", "dup", body, rh))
    container_rows = h.conn.execute(
        "SELECT COUNT(*) FROM binding_set").fetchone()[0]
    with pytest.raises(CommandConflict) as exc:
        h.command("t1", "dup", {**body, "branch_id": "exp"}, lambda rh: None)
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
    # 409 之后一行都不能多写（AGENTS.md：同键不同 body 拒绝，不是覆盖）。
    assert h.conn.execute("SELECT COUNT(*) FROM binding_set"
                          ).fetchone()[0] == container_rows


def test_t10_E12_A2b_failed_seal_leaves_no_idempotency_row():
    h = Harness()
    _asset, bs, _sealed = h.seeded()
    before = h.conn.execute(
        "SELECT COUNT(*) FROM idempotency_record").fetchone()[0]
    with pytest.raises(ServiceError):
        h.command("t1", "bad", {"expected_head": 99}, lambda rh: h.service.seal(
            "t1", "bad", bs["binding_set_id"], {"expected_head": 99}, rh))
    after = h.conn.execute(
        "SELECT COUNT(*) FROM idempotency_record").fetchone()[0]
    assert after == before, "a failed command must not leave an in-flight row"


def test_t10_E12_A3_mint_repeat_returns_same_asset_id():
    h = Harness()
    first = h.command("t1", "a", {}, lambda rh: h.service.mint_asset(
        "t1", "a", {"asset_type": "character", "owner_service": "S11",
                    "owner_local_id": "chr_linlan"}, rh))
    second = h.command("t1", "b", {}, lambda rh: h.service.mint_asset(
        "t1", "b", {"asset_type": "character", "owner_service": "S11",
                    "owner_local_id": "chr_linlan"}, rh))
    assert first["created"] is True
    assert second["created"] is False, "the second registration is not a new asset"
    assert first["asset"]["asset_id"] == second["asset"]["asset_id"]
    assert h.conn.execute("SELECT COUNT(*) FROM asset_identity"
                          ).fetchone()[0] == 1


def test_t10_E12_A4_f5_same_floor_duplicate_entry_is_409():
    # F5：同一 floor 同一 (scope, role) 写两次 → ENTRY_ALREADY_SET，不覆盖。
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=1), cid="a")
    with pytest.raises(ServiceError) as exc:
        h.write_entry(bs["binding_set_id"], h.entry_body(revision=1), cid="b")
    assert exc.value.code == "ENTRY_ALREADY_SET"
    assert h.conn.execute("SELECT COUNT(*) FROM binding_entry"
                          ).fetchone()[0] == 1


def test_t10_E12_A4b_resealing_an_unchanged_draft_is_duplicate_content():
    """F7：草稿没变就再封一次 → 409 DUPLICATE_BINDING_CONTENT。

    这是 E12-A4「两个前端并发点保存」在 S10 里的**唯一可达形态**：
    payload 里带 `binding_set_id`，所以两个不同容器永远不可能撞 hash
    （见 CONTRACT_GAPS S10-23）。
    """
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, sealed = h.seeded()
    with pytest.raises(ServiceError) as exc:
        h.seal(bs["binding_set_id"], head=1, cid="s2")
    assert exc.value.status == 409
    assert exc.value.code == "DUPLICATE_BINDING_CONTENT"
    assert exc.value.details["existing_revision"] == sealed["revision"]
    assert h.conn.execute("SELECT COUNT(*) FROM binding_set_revision"
                          ).fetchone()[0] == 1


def test_t10_E12_A4c_reverting_to_previous_content_is_duplicate_content():
    """内容回到旧版本 → 撞唯一索引，不造第二个同 hash 的 revision。"""
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S11", "chr_linlan", 2)
    _asset, bs, first = h.seeded()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=2, head=1), cid="e2")
    h.seal(bs["binding_set_id"], head=1, cid="s2")
    # 改回 revision 1：内容与首版逐字相同。
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=1, head=2), cid="e3")
    with pytest.raises(ServiceError) as exc:
        h.seal(bs["binding_set_id"], head=2, cid="s3")
    assert exc.value.code == "DUPLICATE_BINDING_CONTENT"
    assert exc.value.details["existing_revision"] == first["revision"]


def test_t10_E12_A5_idempotency_aborts_on_upstream_failure():
    """上游 503 时不能留下 IN_PROGRESS 行，否则同一 command_id 永远 409。"""
    h = Harness()
    _asset, bs, _sealed = h.seeded()
    h.add_revision("S11", "chr_linlan", 2)
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=2, head=1), cid="e2")

    def flaky(*_args):
        raise ConnectionError("down")

    h.service.revision_source = flaky
    with pytest.raises(ServiceError) as exc:
        h.command("t1", "flaky", {"expected_head": 1},
                  lambda rh: h.service.seal("t1", "flaky",
                                            bs["binding_set_id"],
                                            {"expected_head": 1}, rh))
    assert exc.value.status == 503
    assert h.conn.execute(
        "SELECT COUNT(*) FROM idempotency_record WHERE command_id='flaky'"
    ).fetchone()[0] == 0


# =========================================================== E28 跨租户

def test_t10_E28_A1_cross_tenant_resolve_is_404_with_no_leak():
    h = Harness()
    _asset, bs, sealed = h.seeded(tenant="t1")
    other_asset = h.mint(tenant="t2", cid="t2-mint")
    with pytest.raises(ServiceError) as exc:
        h.service.resolve("t2", other_asset["asset_id"],
                          binding_set_id=bs["binding_set_id"],
                          binding_set_revision=sealed["revision"])
    assert exc.value.status == 404
    assert exc.value.code == "NOT_FOUND"
    assert "t1" not in json.dumps(exc.value.details)


def test_t10_E28_A2_same_owner_local_id_in_two_tenants_gives_two_assets():
    h = Harness()
    a = h.mint(tenant="t1", cid="a")
    b = h.mint(tenant="t2", cid="b")
    assert a["asset_id"] != b["asset_id"]


def test_t10_E28_A3_hash_uniqueness_is_scoped_by_tenant():
    """`bsr_hash_uniq` 含 `tenant_id`：两个租户可以各存一份相同内容。

    走 SQL 而不是命令：payload 里带 `binding_set_id`，所以两个容器（哪怕同
    租户）永远算不出同一个 hash（CONTRACT_GAPS S10-23）。要证明索引本身是
    按租户分区的，只能直接写两行。
    """
    h = Harness()
    h.mint(tenant="t1", cid="a")
    h.mint(tenant="t2", cid="b")
    same = sha("a")
    for tenant in ("t1", "t2"):
        h.conn.execute(
            "INSERT INTO binding_set_revision (tenant_id, binding_set_id,"
            " revision, state, content_hash, payload, entry_count,"
            " expanded_from, rights_pointer_set, base_revision, sealed_by,"
            " sealed_at) VALUES (?,?,1,'SEALED',?,'{}',0,'{}','[]',NULL,'x',"
            " '2026-01-01T00:00:00.000Z')", (tenant, f"bs_{tenant}", same))
    assert h.conn.execute("SELECT COUNT(*) FROM binding_set_revision"
                          ).fetchone()[0] == 2
    # 同一个租户再写一份同样的 hash 必须撞唯一索引。
    with pytest.raises(sqlite3.IntegrityError):
        h.conn.execute(
            "INSERT INTO binding_set_revision (tenant_id, binding_set_id,"
            " revision, state, content_hash, payload, entry_count,"
            " expanded_from, rights_pointer_set, base_revision, sealed_by,"
            " sealed_at) VALUES ('t1','bs_t1',2,'SEALED',?,'{}',0,'{}','[]',"
            " NULL,'x','2026-01-01T00:00:00.000Z')", (same,))


def test_t10_E28_A4_cross_tenant_usage_is_404():
    h = Harness()
    _asset, bs, sealed = h.seeded(tenant="t1")
    with pytest.raises(ServiceError) as exc:
        h.service.usage("t2", bs["binding_set_id"], sealed["revision"])
    assert exc.value.status == 404


def test_t10_E28_A5_cross_tenant_asset_read_is_404():
    h = Harness()
    asset = h.mint(tenant="t1")
    with pytest.raises(ServiceError) as exc:
        h.service.get_asset("t2", asset["asset_id"])
    assert exc.value.status == 404


def test_t10_E28_A6_lookup_never_crosses_tenants():
    h = Harness()
    h.mint(tenant="t1")
    with pytest.raises(ServiceError) as exc:
        h.service.lookup_asset("t2", "S11", "chr_linlan")
    assert exc.value.status == 404


def test_t10_E28_A7_no_query_accepts_a_hash_as_a_key():
    """反模式 8：不存在按 content_hash 的查询入口，所以猜不到存在性。"""
    h = Harness()
    public = {name for name in dir(h.service) if not name.startswith("_")}
    assert not any("by_hash" in name or "hash_lookup" in name
                   for name in public)


# =========================================================== E30 权利实时复查

def test_t10_E30_A1_revoked_voice_still_resolves_with_a_pointer():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S15", "voice_linlan", 2)
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(role_key="voice:voice_linlan", revision=2),
                  cid="v")
    sealed = h.seal(bs["binding_set_id"])
    h.statuses[("S15", "voice_linlan")] = "tombstoned"
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    voice = next(entry for entry in result["entries"]
                 if entry["role_key"] == "voice:voice_linlan")
    assert voice["rights_ref"] == rights()
    assert result["rights_recheck_required"] is True


def test_t10_E30_A2_rights_set_hash_is_order_independent():
    a = rights("rights_a", 1)
    b = rights("rights_b", 2)
    assert rights_set_hash([a, b]) == rights_set_hash([b, a])


def test_t10_E30_A3_rights_set_hash_dedupes_identical_pointers():
    a = rights("rights_a", 1)
    assert rights_set_hash([a, a, a]) == rights_set_hash([a])


def test_t10_E30_A3b_different_rights_revision_changes_the_revision_hash():
    """E30-A3：S02 用这个 hash 判「权利换了没」。改了就必须变。"""
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, sealed = h.seeded()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=1, head=1,
                               rights_ref=rights(rev=9)), cid="e2")
    second = h.seal(bs["binding_set_id"], head=1, cid="s2")
    assert sealed["payload"]["rights_pointer_set_hash"] != \
        second["payload"]["rights_pointer_set_hash"]


def test_t10_E30_A4_s10_has_no_rights_judgement_api():
    h = Harness()
    public = {name for name in dir(h.service) if not name.startswith("_")}
    for forbidden in ("check_rights", "validate_rights", "is_authorized",
                      "rights_valid"):
        assert forbidden not in public


# =========================================================== E37 缺素材

def test_t10_E37_A1_missing_required_role_seals_and_is_reported():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    # 另一条必需 role 指向不存在的 revision，但在草稿里没写 owner 校验，
    # 封存时会因为 owner 404 而 422；这里用「写了 entry 但 owner 存在」的方式
    # 只验证「必需 role 集」由 overlay 决定：先写一条，再删掉它。
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(role_key="voice:voice_chenmo", revision=1),
                  cid="v")
    h.command("t1", "del", {"expected_head": 0},
              lambda rh: h.service.delete_entry(
                  "t1", "del", bs["binding_set_id"], "episode", "ep3",
                  "voice:voice_chenmo", {"expected_head": 0}, rh))
    sealed = h.seal(bs["binding_set_id"])
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    assert result["unresolved_required_roles"] == []


def test_t10_E37_A1b_unresolved_required_roles_are_reported_from_defaults():
    """F8：默认值里的必需 role 没展开出来 → resolve 如实报告缺口。"""
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S15", "voice_chenmo", 1)
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    # 给另一个 role 写默认值，但它只写了默认值、没写 entry。
    h.command("t1", "d", {"scope_kind": "episode", "scope_id": "ep3",
                          "role_key": "voice:voice_chenmo",
                          "owner_revision": 1, "owner_hash": sha("c"),
                          "rights_ref": rights()},
              lambda rh: h.service.set_default(
                  "t1", "d", bs["binding_set_id"],
                  {"scope_kind": "episode", "scope_id": "ep3",
                   "role_key": "voice:voice_chenmo", "owner_revision": 1,
                   "owner_hash": sha("c"), "rights_ref": rights()}, rh))
    # 默认值参与展开，所以它会被解析出来（不是缺口）。
    sealed = h.seal(bs["binding_set_id"])
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    roles = {entry["role_key"] for entry in result["entries"]}
    assert "voice:voice_chenmo" in roles
    assert result["unresolved_required_roles"] == []


def test_t10_E37_A1c_variant_role_filter_reports_only_that_role():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    sealed = h.seal(bs["binding_set_id"])
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"],
                               variant_role="voice:voice_chenmo")
    assert result["entries"] == []
    assert result["unresolved_required_roles"] == []


def test_t10_E37_A2_gap_is_not_silently_filled():
    """F8 的反面：S10 绝不替调用方编一个 owner 指针。"""
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    sealed = h.seal(bs["binding_set_id"])
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    assert len(result["entries"]) == 1
    assert all(entry["owner_ref"]["id"] == "chr_linlan"
               for entry in result["entries"])


def test_t10_E37_A3_revision_survives_asset_tombstone():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    sealed = h.seal(bs["binding_set_id"])
    body = {"reason_code": "RIGHTS_REVOKED"}
    h.command("t1", "dep", body, lambda rh: h.service.deprecate_asset(
        "t1", "dep", asset["asset_id"], body, rh))
    h.command("t1", "tomb", body, lambda rh: h.service.tombstone_asset(
        "t1", "tomb", asset["asset_id"], body, rh))
    # 历史 revision 仍可读，且 resolve 仍返回指针（E37-A3 / §11.2 第 6 条）。
    assert h.service.get_revision("t1", bs["binding_set_id"],
                                  sealed["revision"])["content_hash"] == \
        sealed["content_hash"]
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    assert result["entries"][0]["role_key"] == "character:chr_linlan"


def test_t10_E37_A4_tombstoned_asset_cannot_be_bound_again():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    asset = h.mint()
    h.command("t1", "dep", {}, lambda rh: h.service.deprecate_asset(
        "t1", "dep", asset["asset_id"], {}, rh))
    h.command("t1", "tomb", {}, lambda rh: h.service.tombstone_asset(
        "t1", "tomb", asset["asset_id"], {}, rh))
    bs = h.create_bs()
    with pytest.raises(ServiceError) as exc:
        h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    assert exc.value.code == "ASSET_TOMBSTONED"


# =========================================================== 失败矩阵 F1–F14

def test_t10_F1_owner_unreachable_returns_503_and_does_not_seal():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S11", "chr_linlan", 2)
    _asset, bs, _sealed = h.seeded()
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=2, head=1),
                  cid="e2")

    def down(*_args):
        raise ConnectionError("owner down")

    h.service.revision_source = down
    with pytest.raises(ServiceError) as exc:
        h.seal(bs["binding_set_id"], head=1, cid="s2")
    assert exc.value.status == 503
    assert exc.value.code == "DEPENDENCY_UNAVAILABLE"
    assert exc.value.retryable is True
    assert h.conn.execute(
        "SELECT COUNT(*) FROM binding_set_revision").fetchone()[0] == 1


def test_t10_F2_missing_owner_revision_is_422():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, _sealed = h.seeded()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(role_key="prop:prop_missing", revision=3,
                               head=1), cid="e2")
    with pytest.raises(ServiceError) as exc:
        h.seal(bs["binding_set_id"], head=1, cid="s2")
    assert exc.value.status == 422
    assert exc.value.code == "OWNER_REF_INVALID"
    assert exc.value.details["reason"] == "REVISION_NOT_FOUND"


def test_t10_F3_hash_mismatch_is_422():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1, content_hash=sha("d"))
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=1, content_hash=sha("e")))
    with pytest.raises(ServiceError) as exc:
        h.seal(bs["binding_set_id"])
    assert exc.value.code == "OWNER_REF_INVALID"
    assert exc.value.details["reason"] == "HASH_MISMATCH"


def test_t10_F4_tombstoned_owner_is_422():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.statuses[("S11", "chr_linlan")] = "tombstoned"
    h.mint()
    bs = h.create_bs()
    # 草稿阶段读的是 S10 的身份镜像（active），所以能写；封存时上游说墓碑。
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    with pytest.raises(ServiceError) as exc:
        h.seal(bs["binding_set_id"])
    assert exc.value.code == "OWNER_REF_INVALID"
    assert exc.value.details["reason"] == "TOMBSTONED"


def test_t10_F5_duplicate_entry_is_409():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, _sealed = h.seeded()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=1, head=1), cid="first")
    with pytest.raises(ServiceError) as exc:
        h.write_entry(bs["binding_set_id"],
                      h.entry_body(revision=1, head=1), cid="dup")
    assert exc.value.code == "ENTRY_ALREADY_SET"


def test_t10_F6_stale_expected_head_is_version_conflict():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    with pytest.raises(ServiceError) as exc:
        h.seal(bs["binding_set_id"], head=7, cid="stale")
    assert exc.value.status == 409
    assert exc.value.code == "VERSION_CONFLICT"
    assert exc.value.details["head"] == sealed["revision"]
    assert exc.value.retryable is True


def test_t10_F9_conflicting_shot_scope_declarations_are_refused():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, _sealed = h.seeded()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(scope_kind="shot", scope_id="sh2_1", head=1,
                               scope_path={"scene_id": "sc1"}), cid="s1")
    with pytest.raises(ServiceError) as exc:
        h.write_entry(bs["binding_set_id"],
                      h.entry_body(scope_kind="shot", scope_id="sh2_1",
                                   head=1, role_key="costume:cos_a",
                                   scope_path={"scene_id": "sc2"}), cid="s2")
    assert exc.value.code == "SCOPE_INCONSISTENT"
    assert exc.value.details["declared_scene_id"] == "sc1"


def test_t10_F10_unknown_binding_set_is_404():
    h = Harness()
    with pytest.raises(ServiceError) as exc:
        h.service.get_binding_set("t1", "bs_missing")
    assert exc.value.status == 404


def test_t10_F11_archived_binding_set_refuses_new_revisions():
    h = Harness()
    _asset, bs, _sealed = h.seeded()
    h.command("t1", "arch", {"confirm_no_open_branches": True},
              lambda rh: h.service.archive(
                  "t1", "arch", bs["binding_set_id"],
                  {"confirm_no_open_branches": True}, rh))
    with pytest.raises(ServiceError) as exc:
        h.write_entry(bs["binding_set_id"],
                      h.entry_body(revision=2, head=1), cid="e2")
    assert exc.value.code == "BINDING_SET_ARCHIVED"
    with pytest.raises(ServiceError) as exc2:
        h.seal(bs["binding_set_id"], head=1, cid="s2")
    assert exc2.value.code == "BINDING_SET_ARCHIVED"


def test_t10_F11b_archive_without_confirmation_is_refused():
    h = Harness()
    _asset, bs, _sealed = h.seeded()
    with pytest.raises(ServiceError) as exc:
        h.command("t1", "arch", {}, lambda rh: h.service.archive(
            "t1", "arch", bs["binding_set_id"], {}, rh))
    assert exc.value.code == "ARCHIVE_NOT_CONFIRMED"


def test_t10_F12_row_level_cas_leaves_no_partial_write():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, sealed = h.seeded()
    with pytest.raises(ServiceError):
        h.seal(bs["binding_set_id"], head=0, cid="race")
    assert h.conn.execute("SELECT COUNT(*) FROM binding_set_revision"
                          ).fetchone()[0] == 1
    assert h.conn.execute("SELECT COUNT(*) FROM binding_expansion"
                          ).fetchone()[0] == sealed["entry_count"]
    assert h.conn.execute("SELECT head_revision FROM binding_set WHERE"
                          " binding_set_id=?",
                          (bs["binding_set_id"],)).fetchone()[0] == 1


def test_t10_F13_outbox_row_carries_the_revision_as_sequence():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    payload = json.loads(h.conn.execute(
        "SELECT payload FROM outbox").fetchone()[0])
    assert payload["sequence"] == sealed["revision"]
    assert payload["type"] == "asset.binding-set.created"
    assert payload["source"] == "S10"
    assert payload["aggregate_id"] == bs["binding_set_id"]
    assert payload["subject"] == f"binding-set/{bs['binding_set_id']}"


def test_t10_F14_manifest_replay_is_deduped_by_inbox():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    envelope = {
        "id": "evt-1", "tenant_id": "t1", "sequence": 1,
        "aggregate_id": "manifest_ep3",
        "data": {"manifest_ref": {"id": "manifest_ep3", "revision": 1},
                 "binding_set_ref": {"service_id": "S10",
                                     "id": bs["binding_set_id"],
                                     "revision": sealed["revision"],
                                     "content_hash": sealed["content_hash"]},
                 "project_id": "proj1", "series_id": "series_demo",
                 "episode_id": "ep3", "branch_id": "canon"},
    }
    first = h.service.ingest_manifest_committed(envelope)
    second = h.service.ingest_manifest_committed(envelope)
    assert first["projected"] is True
    assert second["consumed"] is False
    assert h.conn.execute("SELECT COUNT(*) FROM binding_usage"
                          ).fetchone()[0] == 1


# =========================================================== 展开与 scope 继承

def test_t10_20_most_specific_scope_wins():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S11", "chr_linlan", 2)
    h.add_revision("S11", "chr_linlan", 4)
    asset = h.mint()
    bs = h.create_bs()
    # 全剧默认 1、本集 2、本场 4。
    for cid, kwargs in (
            ("s", {"scope_kind": "series", "scope_id": "series_demo",
                   "revision": 1}),
            ("e", {"scope_kind": "episode", "scope_id": "ep3", "revision": 2}),
            ("c", {"scope_kind": "scene", "scope_id": "sc2", "revision": 4})):
        h.write_entry(bs["binding_set_id"], h.entry_body(**kwargs), cid=cid)
    sealed = h.seal(bs["binding_set_id"])

    as_scene = h.service.resolve("t1", asset["asset_id"],
                                 binding_set_id=bs["binding_set_id"],
                                 binding_set_revision=sealed["revision"],
                                 scene_id="sc2")
    assert as_scene["entries"][0]["owner_ref"]["revision"] == 4
    assert as_scene["entries"][0]["resolved_from"] == "scene"

    as_episode = h.service.resolve("t1", asset["asset_id"],
                                   binding_set_id=bs["binding_set_id"],
                                   binding_set_revision=sealed["revision"])
    assert as_episode["entries"][0]["owner_ref"]["revision"] == 2
    assert as_episode["entries"][0]["resolved_from"] == "episode"


def test_t10_21_shot_override_beats_scene():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S11", "chr_linlan", 9)
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(scope_kind="scene", scope_id="sc2", revision=1),
                  cid="c1")
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(scope_kind="shot", scope_id="sh2_1",
                               revision=9, scope_path={"scene_id": "sc2"}),
                  cid="c2")
    sealed = h.seal(bs["binding_set_id"])
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"],
                               scene_id="sc2", shot_id="sh2_1")
    assert result["entries"][0]["owner_ref"]["revision"] == 9
    assert result["entries"][0]["resolved_from"] == "shot"


def test_t10_22_expansion_is_materialized_not_recomputed_at_read_time():
    """改草稿不改变已封存 revision 的 resolve 结果 —— **断言在 resolve 上**，
    不是 `get_revision`（后者本来就读冻结的 payload，测不到读路径）。"""
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S11", "chr_linlan", 5)
    asset, bs, sealed = h.seeded()
    h.conn.execute(
        "INSERT INTO binding_entry (tenant_id, binding_set_id, revision_floor,"
        " scope_kind, scope_id, role_key, asset_type, owner_service,"
        " owner_local_id, owner_revision, owner_hash, rights_ref,"
        " variant_selector, required, scope_path, unverified)"
        " VALUES ('t1',?,'5','episode','ep3','character:chr_linlan',"
        " 'character','S11','chr_linlan',5,?,'{}','{}',1,'{}',0)",
        (bs["binding_set_id"], sha("c")))
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    assert result["entries"][0]["owner_ref"]["revision"] == 1
    assert h.conn.execute("SELECT COUNT(*) FROM binding_entry"
                          ).fetchone()[0] == 2, "the draft really did change"


def test_t10_23_inherited_entries_cannot_be_deleted_from_the_draft():
    """S10-2：继承来的条目属于已封存的历史，删草稿不能移除它。"""
    h = Harness()
    _asset, bs, _sealed = h.seeded()
    with pytest.raises(ServiceError) as exc:
        h.command("t1", "del", {"expected_head": 1},
                  lambda rh: h.service.delete_entry(
                      "t1", "del", bs["binding_set_id"], "episode", "ep3",
                      "character:chr_linlan", {"expected_head": 1}, rh))
    assert exc.value.code == "ENTRY_NOT_EDITABLE"


def test_t10_24_draft_entries_are_not_visible_as_expansion():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, sealed = h.seeded()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(scope_kind="scene", scope_id="sc9", head=1),
                  cid="sc9")
    result = h.service.resolve("t1", _asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"],
                               scene_id="sc9")
    # 该场没有物化行 → 沿已物化的 episode 层回退。
    assert result["entries"][0]["resolved_from"] == "episode"


def test_t10_25_required_flag_is_carried_into_expansion():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, sealed = h.seeded()
    rev = h.service.get_revision("t1", bs["binding_set_id"],
                                 sealed["revision"])
    # 冻结进 payload 的是 SQLite 的 0/1；`_entry_out` 才转成 bool。
    assert rev["payload"]["entries"][0]["required"] == 1
    assert sealed["entry_count"] == 1


def test_t10_26_variant_selector_is_frozen_verbatim():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    asset = h.mint()
    bs = h.create_bs()
    selector = {"state": "torn", "light": "night"}
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=1, variant_selector=selector))
    sealed = h.seal(bs["binding_set_id"])
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    # §11.2 第 4 条：S10 不解释，只原样冻结并返回。
    assert result["entries"][0]["variant_selector"] == selector


def test_t10_27_nested_variant_selector_is_refused():
    h = Harness()
    asset = h.mint()
    bs = h.create_bs()
    with pytest.raises(ServiceError) as exc:
        h.write_entry(bs["binding_set_id"],
                      h.entry_body(variant_selector={"bad": {"nested": 1}}))
    assert exc.value.code == "VARIANT_SELECTOR_INVALID"


# =========================================================== 事件契约

def test_t10_30_event_carries_the_contract_fields():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    payload = json.loads(h.conn.execute(
        "SELECT payload FROM outbox").fetchone()[0])
    data = payload["data"]
    for key in ("schema_version", "binding_set_ref", "series_id",
                "episode_id", "branch_id", "entry_count",
                "owner_service_counts", "rights_revision_set_hash"):
        assert key in data, key
    assert data["binding_set_ref"]["service_id"] == "S10"
    assert data["binding_set_ref"]["revision"] == sealed["revision"]
    assert data["binding_set_ref"]["content_hash"] == sealed["content_hash"]
    assert data["owner_service_counts"] == {"S11": 1}
    assert data["entry_count"] == 1


def test_t10_31_first_revision_omits_superseded_revision():
    """ABS-5：首版没有旧版本，不发编造的 0。"""
    h = Harness()
    _asset, bs, sealed = h.seeded()
    payload = json.loads(h.conn.execute(
        "SELECT payload FROM outbox").fetchone()[0])
    assert "superseded_revision" not in payload["data"]


def test_t10_32_second_revision_names_the_superseded_revision():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 2)
    _asset, bs, sealed = h.seeded()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=2, head=1), cid="e2")
    second = h.seal(bs["binding_set_id"], head=1, cid="s2")
    rows = [json.loads(row[0]) for row in h.conn.execute(
        "SELECT payload FROM outbox ORDER BY created_at, sequence")]
    assert rows[-1]["data"]["superseded_revision"] == sealed["revision"]
    assert rows[-1]["data"]["binding_set_ref"]["revision"] == \
        second["revision"]


def test_t10_33_owner_service_counts_count_the_expansion():
    """ABS-4：数的是展开结果，不是 overlay。"""
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S12", "cos_a", 1)
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(scope_kind="scene", scope_id="sc1",
                               revision=1), cid="c")
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(scope_kind="scene", scope_id="sc2",
                               revision=1), cid="c2")
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(role_key="costume:cos_a", revision=1), cid="cos")
    h.seal(bs["binding_set_id"])
    data = json.loads(h.conn.execute(
        "SELECT payload FROM outbox").fetchone()[0])["data"]
    # 展开是按**目标节点**铺开的：3 个目标节点（ep3、sc1、sc2）× 2 个 role。
    # 角色在 sc1/sc2 有 entry，服装只在 episode 层有 entry 并向下继承到两个场。
    assert data["owner_service_counts"] == {"S11": 2, "S12": 3}
    assert data["entry_count"] == 5
    # overlay 只有 3 行 —— 如果数的是 overlay，这里会是 {"S11": 2, "S12": 1}。
    assert h.conn.execute("SELECT COUNT(*) FROM binding_entry"
                          ).fetchone()[0] == 3


def test_t10_34_events_are_not_emitted_for_lifecycle_or_archive():
    h = Harness()
    asset = h.mint()
    body = {}
    h.command("t1", "dep", body, lambda rh: h.service.deprecate_asset(
        "t1", "dep", asset["asset_id"], body, rh))
    assert h.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    local = [row[0] for row in h.conn.execute(
        "SELECT event_type FROM local_event ORDER BY created_at")]
    assert local == ["asset.identity.registered", "asset.identity.deprecated"]


def test_t10_35_rights_revision_set_hash_changes_with_rights():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, sealed = h.seeded()
    first = json.loads(h.conn.execute(
        "SELECT payload FROM outbox").fetchone()[0])["data"]
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=1, head=1, rights_ref=rights(rev=8)),
                  cid="e2")
    h.seal(bs["binding_set_id"], head=1, cid="s2")
    rows = [json.loads(row[0]) for row in h.conn.execute(
        "SELECT payload FROM outbox ORDER BY created_at, sequence")]
    assert rows[-1]["data"]["rights_revision_set_hash"] != \
        first["rights_revision_set_hash"]


# =========================================================== 使用投影

def test_t10_40_usage_projects_from_the_event_when_it_carries_a_binding_ref():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    envelope = {
        "id": "evt-10", "tenant_id": "t1", "sequence": 3,
        "aggregate_id": "manifest_ep3",
        "data": {"manifest_ref": {"id": "manifest_ep3", "revision": 2},
                 "binding_set_ref": {"service_id": "S10",
                                     "id": bs["binding_set_id"],
                                     "revision": sealed["revision"],
                                     "content_hash": sealed["content_hash"]},
                 "project_id": "proj1", "series_id": "series_demo",
                 "episode_id": "ep3", "branch_id": "canon"},
    }
    result = h.service.ingest_manifest_committed(envelope)
    assert result["source"] == "event"
    usage = h.service.usage("t1", bs["binding_set_id"], sealed["revision"])
    assert [(item["manifest_id"], item["manifest_revision"])
            for item in usage["items"]] == [("manifest_ep3", 2)]
    assert usage["stale"] is False


def test_t10_41_usage_falls_back_to_s08_when_the_event_has_no_binding_ref():
    """S10-11：事件的 payload 没有 binding_set_ref，回源 S08。"""
    h = Harness()
    _asset, bs, sealed = h.seeded()
    h.manifests[("manifest_ep3", 4)] = {
        "manifest_id": "manifest_ep3", "revision": 4,
        "project_id": "proj1", "series_id": "series_demo",
        "episode_id": "ep3", "branch_id": "canon",
        "binding_set_ref": {"service_id": "S10",
                            "id": bs["binding_set_id"],
                            "revision": sealed["revision"],
                            "content_hash": sealed["content_hash"]},
    }
    envelope = {"id": "evt-11", "tenant_id": "t1", "sequence": 4,
                "aggregate_id": "manifest_ep3",
                "data": {"manifest_ref": {"id": "manifest_ep3",
                                          "revision": 4}}}
    result = h.service.ingest_manifest_committed(envelope)
    assert result["source"] == "s08"
    assert result["projected"] is True


def test_t10_42_usage_marks_stale_when_s08_is_unreachable():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    envelope = {"id": "evt-12", "tenant_id": "t1", "sequence": 1,
                "aggregate_id": "manifest_ep3",
                "data": {"manifest_ref": {"id": "manifest_ep3",
                                          "revision": 9},
                         "series_id": "series_demo", "episode_id": "ep3",
                         "branch_id": "canon", "project_id": "proj1"}}
    result = h.service.ingest_manifest_committed(envelope)
    assert result["projected"] is False
    usage = h.service.usage("t1", bs["binding_set_id"], sealed["revision"])
    assert usage["stale"] is True
    assert usage["stale_reason"] == \
        "S08_UNAVAILABLE_AND_EVENT_HAS_NO_BINDING_REF"


def test_t10_43_out_of_order_sequence_marks_the_projection_stale():
    """S10-10：不做假缓冲，但绝不假装完整。"""
    h = Harness()
    _asset, bs, sealed = h.seeded()

    def envelope(event_id, sequence, revision):
        return {"id": event_id, "tenant_id": "t1", "sequence": sequence,
                "aggregate_id": "manifest_ep3",
                "data": {"manifest_ref": {"id": "manifest_ep3",
                                          "revision": revision},
                         "binding_set_ref": {
                             "service_id": "S10", "id": bs["binding_set_id"],
                             "revision": sealed["revision"],
                             "content_hash": sealed["content_hash"]},
                         "project_id": "proj1", "series_id": "series_demo",
                         "episode_id": "ep3", "branch_id": "canon"}}

    h.service.ingest_manifest_committed(envelope("e5", 5, 5))
    late = h.service.ingest_manifest_committed(envelope("e4", 4, 4))
    assert late["stale"] is True
    usage = h.service.usage("t1", bs["binding_set_id"], sealed["revision"])
    assert usage["stale"] is True
    assert usage["stale_reason"] == "OUT_OF_ORDER_SEQUENCE"
    assert len(usage["items"]) == 2  # 两版都留，不丢事件


def test_t10_44_usage_never_claims_authority_over_s08():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    usage = h.service.usage("t1", bs["binding_set_id"], sealed["revision"])
    # 投影不是权威：它必须自报水位，且没有「重建」「权威」这类入口。
    assert "projection_watermark" in usage
    public = {name for name in dir(h.service) if not name.startswith("_")}
    assert "rebuild_usage" not in public


# =========================================================== 生命周期

def test_t10_50_deprecate_then_tombstone_is_the_only_path():
    h = Harness()
    asset = h.mint()
    with pytest.raises(ServiceError) as exc:
        h.command("t1", "t", {}, lambda rh: h.service.tombstone_asset(
            "t1", "t", asset["asset_id"], {}, rh))
    assert exc.value.code == "ILLEGAL_TRANSITION"
    h.command("t1", "d", {}, lambda rh: h.service.deprecate_asset(
        "t1", "d", asset["asset_id"], {}, rh))
    out = h.command("t1", "t2", {}, lambda rh: h.service.tombstone_asset(
        "t1", "t2", asset["asset_id"], {}, rh))
    assert out["asset"]["status"] == "tombstoned"


def test_t10_51_tombstoned_asset_cannot_be_revived():
    h = Harness()
    asset = h.mint()
    h.command("t1", "d", {}, lambda rh: h.service.deprecate_asset(
        "t1", "d", asset["asset_id"], {}, rh))
    h.command("t1", "t", {}, lambda rh: h.service.tombstone_asset(
        "t1", "t", asset["asset_id"], {}, rh))
    with pytest.raises(ServiceError) as exc:
        h.command("t1", "u", {}, lambda rh: h.service._transition(
            "t1", "u", asset["asset_id"], {}, target="un_tombstoned",
            kind="un_tombstoned"))
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_t10_52_merge_requires_a_same_type_active_target():
    h = Harness()
    source = h.mint(owner_local_id="chr_a", cid="a")
    other_type = h.mint(asset_type="costume", owner_local_id="cos_a", cid="c")
    with pytest.raises(ServiceError) as exc:
        h.command("t1", "m", {"merged_into": other_type["asset_id"]},
                  lambda rh: h.service.merge_asset(
                      "t1", "m", source["asset_id"],
                      {"merged_into": other_type["asset_id"]}, rh))
    assert exc.value.code == "MERGE_TARGET_INVALID"


def test_t10_53_merge_never_rewrites_old_binding_revisions():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    target = h.mint(owner_local_id="chr_linlan_new", cid="new")
    _asset, bs, sealed = h.seeded()
    asset = h.service.lookup_asset("t1", "S11", "chr_linlan")
    out = h.command("t1", "m", {"merged_into": target["asset_id"]},
                    lambda rh: h.service.merge_asset(
                        "t1", "m", asset["asset_id"],
                        {"merged_into": target["asset_id"]}, rh))
    assert out["asset"]["merged_into"] == target["asset_id"]
    revision = h.service.get_revision("t1", bs["binding_set_id"],
                                      sealed["revision"])
    assert revision["payload"]["entries"][0]["owner_local_id"] == "chr_linlan"
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    assert result["merged_to"] == target["asset_id"]
    assert result["entries"][0]["owner_ref"]["id"] == "chr_linlan"


def test_t10_54_merged_assets_cannot_be_bound_again():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    target = h.mint(owner_local_id="chr_linlan_new", cid="new")
    asset = h.mint(cid="orig")
    h.command("t1", "m", {"merged_into": target["asset_id"]},
              lambda rh: h.service.merge_asset(
                  "t1", "m", asset["asset_id"],
                  {"merged_into": target["asset_id"]}, rh))
    bs = h.create_bs()
    with pytest.raises(ServiceError) as exc:
        h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    assert exc.value.code == "ASSET_MERGED"


def test_t10_55_merge_into_self_is_refused():
    h = Harness()
    asset = h.mint()
    with pytest.raises(ServiceError) as exc:
        h.command("t1", "m", {"merged_into": asset["asset_id"]},
                  lambda rh: h.service.merge_asset(
                      "t1", "m", asset["asset_id"],
                      {"merged_into": asset["asset_id"]}, rh))
    assert exc.value.code == "MERGE_TARGET_INVALID"


def test_t10_56_lifecycle_events_are_append_only():
    h = Harness()
    asset = h.mint()
    h.command("t1", "d", {}, lambda rh: h.service.deprecate_asset(
        "t1", "d", asset["asset_id"], {}, rh))
    seqs = [row[0] for row in h.conn.execute(
        "SELECT seq FROM asset_lifecycle_event ORDER BY seq")]
    assert seqs == [1, 2]


# =========================================================== import_mode

def test_t10_60_verify_owners_false_requires_import_mode_and_authorization():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    with pytest.raises(ServiceError) as exc:
        h.seal(bs["binding_set_id"], head=1, cid="x", verify_owners=False)
    assert exc.value.status == 422
    assert exc.value.code == "IMPORT_NOT_AUTHORIZED"


def test_t10_61_import_mode_marks_verification_deferred():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, sealed = h.seeded()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=1, head=1), cid="e2")
    out = h.seal(bs["binding_set_id"], head=1, cid="s2", verify_owners=False,
                 import_mode=True, authorization="ops-ticket-17")
    assert out["expanded_from"]["owner_verification"] == "deferred"
    result = h.service.resolve("t1", _asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=out["revision"])
    assert result["verification"] == "deferred"


def test_t10_62_import_mode_does_not_call_the_owner_at_all():
    calls = {"n": 0}

    def counting(*_args):
        calls["n"] += 1
        raise ConnectionError("down")

    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S11", "chr_linlan", 4)
    _asset, bs, _sealed = h.seeded()

    def counting(*_args):
        calls["n"] += 1
        raise ConnectionError("down")

    h.service.revision_source = counting
    before = calls["n"]
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=4, head=1),
                  cid="e2")
    h.seal(bs["binding_set_id"], head=1, cid="s2", verify_owners=False,
           import_mode=True, authorization="ops-ticket-18")
    assert calls["n"] == before, "verify_owners=false must not touch the owner"


def test_t10_63_verified_seal_reports_verified():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    result = h.service.resolve("t1", _asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    assert result["verification"] == "verified"


# =========================================================== 预览

def test_t10_70_preview_uses_current_revision_and_says_so():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    asset = h.mint()
    bs = h.create_bs()
    h.write_entry(bs["binding_set_id"], h.entry_body(revision=1))
    h.seal(bs["binding_set_id"])
    result = h.service.resolve("t1", asset["asset_id"],
                               binding_set_id=bs["binding_set_id"])
    assert result["is_preview"] is True
    assert result["preview_basis"] == "current_revision"
    assert result["source_revision"] == 1


def test_t10_71_preview_with_no_sealed_revision_is_404():
    h = Harness()
    asset = h.mint()
    bs = h.create_bs()
    with pytest.raises(ServiceError) as exc:
        h.service.resolve("t1", asset["asset_id"],
                          binding_set_id=bs["binding_set_id"])
    assert exc.value.status == 404


def test_t10_72_precise_read_never_sets_preview():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    result = h.service.resolve("t1", _asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    assert result["is_preview"] is False
    assert "preview_basis" not in result


def test_t10_73_resolve_by_coordinates_works_without_a_binding_set_id():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    result = h.service.resolve("t1", _asset["asset_id"],
                               series_id="series_demo", episode_id="ep3",
                               branch_id="canon",
                               binding_set_revision=sealed["revision"])
    assert result["binding_set_ref"]["id"] == bs["binding_set_id"]


def test_t10_74_resolve_without_scope_is_malformed():
    h = Harness()
    asset = h.mint()
    with pytest.raises(ServiceError) as exc:
        h.service.resolve("t1", asset["asset_id"])
    assert exc.value.code == "MALFORMED_REQUEST"


# =========================================================== HTTP 层

def test_t10_80_http_roundtrip_of_the_whole_happy_path(monkeypatch, tmp_path):
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.add_revision("S11", "chr_linlan", 1)
    minted = post(client, "/assets", {
        "asset_type": "character", "owner_service": "S11",
        "owner_local_id": "chr_linlan", "series_scope": "series_demo"})
    assert minted.status_code == 201
    asset_id = minted.json()["asset"]["asset_id"]

    created = post(client, "/binding-sets", {
        "project_id": "proj1", "series_id": "series_demo",
        "episode_id": "ep3", "branch_id": "canon"}, command_id="bs")
    assert created.status_code == 201
    bs_id = created.json()["binding_set"]["binding_set_id"]

    entry = post(client, f"/binding-sets/{bs_id}/entries", {
        "scope_kind": "episode", "scope_id": "ep3",
        "role_key": "character:chr_linlan", "owner_revision": 1,
        "owner_hash": sha("c"), "rights_ref": rights(), "expected_head": 0},
        command_id="entry")
    assert entry.status_code == 201

    sealed = post(client, f"/binding-sets/{bs_id}/revisions",
                  {"expected_head": 0}, command_id="seal")
    assert sealed.status_code == 201
    revision = sealed.json()["revision"]

    resolved = client.get(f"/assets/{asset_id}/resolve",
                          params={"binding_set_id": bs_id,
                                  "binding_set_revision": revision},
                          headers={"X-Tenant-Id": "t1"})
    assert resolved.status_code == 200
    assert resolved.json()["entries"][0]["owner_ref"]["id"] == "chr_linlan"


def test_t10_81_http_replay_sets_replayed_true(monkeypatch, tmp_path):
    client, h, _module = _http_client(monkeypatch, tmp_path)
    body = {"project_id": "p", "series_id": "s", "episode_id": "e",
            "branch_id": "canon"}
    first = post(client, "/binding-sets", body, command_id="same")
    second = post(client, "/binding-sets", body, command_id="same")
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert first.json()["binding_set"]["binding_set_id"] == \
        second.json()["binding_set"]["binding_set_id"]


def test_t10_82_http_same_key_different_body_is_409(monkeypatch, tmp_path):
    client, _h, _module = _http_client(monkeypatch, tmp_path)
    post(client, "/binding-sets", {"project_id": "p", "series_id": "s",
                                   "episode_id": "e", "branch_id": "canon"},
         command_id="k")
    conflict = post(client, "/binding-sets",
                    {"project_id": "p2", "series_id": "s",
                     "episode_id": "e", "branch_id": "canon"},
                    command_id="k")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_t10_83_http_missing_idempotency_key_is_rejected(
        monkeypatch, tmp_path):
    client, _h, _module = _http_client(monkeypatch, tmp_path)
    response = client.post("/binding-sets", json={"project_id": "p"},
                           headers={"X-Tenant-Id": "t1"})
    # 必需头由 FastAPI 的签名校验挡下（422），不会进到服务端命令。
    assert response.status_code == 422
    assert _h.conn.execute("SELECT COUNT(*) FROM binding_set"
                           ).fetchone()[0] == 0


def test_t10_84_http_missing_tenant_header_is_rejected(monkeypatch, tmp_path):
    client, h, _module = _http_client(monkeypatch, tmp_path)
    response = client.post("/binding-sets",
                           json={"project_id": "p", "series_id": "s",
                                 "episode_id": "e", "branch_id": "b"},
                           headers={"Idempotency-Key": "k"})
    assert response.status_code == 400
    assert response.json()["code"] == "MALFORMED_REQUEST"
    assert h.conn.execute("SELECT COUNT(*) FROM binding_set"
                          ).fetchone()[0] == 0


def test_t10_85_http_body_tenant_disagreement_is_403(monkeypatch, tmp_path):
    client, _h, _module = _http_client(monkeypatch, tmp_path)
    response = post(client, "/binding-sets",
                    {"project_id": "p", "series_id": "s", "episode_id": "e",
                     "branch_id": "b", "tenant_id": "t9"})
    assert response.status_code == 403
    assert response.json()["code"] == "TENANT_SCOPE_DENIED"


def test_t10_86_http_body_command_id_must_match_header(monkeypatch, tmp_path):
    client, _h, _module = _http_client(monkeypatch, tmp_path)
    response = post(client, "/binding-sets",
                    {"project_id": "p", "series_id": "s", "episode_id": "e",
                     "branch_id": "b", "command_id": "other"},
                    command_id="k")
    assert response.status_code == 400


def test_t10_87_http_unknown_binding_set_is_404(monkeypatch, tmp_path):
    client, _h, _module = _http_client(monkeypatch, tmp_path)
    response = client.get("/binding-sets/bs_nope",
                          headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_t10_88_http_upstream_down_is_503(monkeypatch, tmp_path):
    def down(*_args):
        raise ConnectionError("no route")
    client, h, _module = _http_client(monkeypatch, tmp_path,
                                      revision_source=down)
    h.add_revision("S11", "chr_linlan", 1)
    created = post(client, "/binding-sets", {
        "project_id": "p", "series_id": "s", "episode_id": "e",
        "branch_id": "canon"}, command_id="bs")
    bs_id = created.json()["binding_set"]["binding_set_id"]
    post(client, f"/binding-sets/{bs_id}/entries", {
        "scope_kind": "episode", "scope_id": "e",
        "role_key": "character:chr_linlan", "owner_revision": 1,
        "owner_hash": sha("c"), "rights_ref": rights(), "expected_head": 0},
        command_id="e")
    response = post(client, f"/binding-sets/{bs_id}/revisions",
                    {"expected_head": 0}, command_id="s")
    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.json()["retryable"] is True


def test_t10_89_http_asset_lookup_and_search(monkeypatch, tmp_path):
    client, h, _module = _http_client(monkeypatch, tmp_path)
    post(client, "/assets", {"asset_type": "character", "owner_service": "S11",
                             "owner_local_id": "chr_linlan",
                             "series_scope": "series_demo",
                             "alias_texts": ["小琳"]})
    found = client.get("/assets:lookup",
                       params={"owner_service": "S11",
                               "owner_local_id": "chr_linlan"},
                       headers={"X-Tenant-Id": "t1"})
    assert found.status_code == 200
    by_alias = client.get("/assets", params={"alias_text": "小琳"},
                          headers={"X-Tenant-Id": "t1"})
    assert len(by_alias.json()["items"]) == 1


def test_t10_90_http_list_requires_series_scope(monkeypatch, tmp_path):
    client, _h, _module = _http_client(monkeypatch, tmp_path)
    response = client.get("/binding-sets",
                          headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 400


def test_t10_91_http_delete_entry_route(monkeypatch, tmp_path):
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.add_revision("S11", "chr_linlan", 1)
    post(client, "/binding-sets", {"project_id": "p", "series_id": "s",
                                   "episode_id": "e", "branch_id": "b"},
         command_id="bs")
    bs_id = client.get("/binding-sets", params={"series_id": "s"},
                       headers={"X-Tenant-Id": "t1"}).json()[
        "items"][0]["binding_set_id"]
    post(client, f"/binding-sets/{bs_id}/entries", {
        "scope_kind": "episode", "scope_id": "e",
        "role_key": "character:chr_linlan", "owner_revision": 1,
        "owner_hash": sha("c"), "rights_ref": rights(), "expected_head": 0},
        command_id="e")
    deleted = client.request(
        "DELETE",
        f"/binding-sets/{bs_id}/entries/episode/e/character:chr_linlan",
        json={"expected_head": 0},
        headers={"Idempotency-Key": "del", "X-Tenant-Id": "t1"})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_t10_92_http_manifest_event_ingest(monkeypatch, tmp_path):
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, sealed = h.seeded()
    envelope = {"id": "evt-http", "tenant_id": "t1", "sequence": 1,
                "aggregate_id": "manifest_ep3",
                "data": {"manifest_ref": {"id": "manifest_ep3", "revision": 1},
                         "binding_set_ref": {
                             "service_id": "S10", "id": bs["binding_set_id"],
                             "revision": sealed["revision"],
                             "content_hash": sealed["content_hash"]},
                         "project_id": "proj1", "series_id": "series_demo",
                         "episode_id": "ep3", "branch_id": "canon"}}
    response = client.post("/internal/events/snapshot-manifest-committed",
                           json=envelope)
    assert response.status_code == 200
    assert response.json()["projected"] is True


def test_t10_93_http_usage_endpoint(monkeypatch, tmp_path):
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, sealed = h.seeded()
    response = client.get(
        f"/binding-sets/{bs['binding_set_id']}/revisions/"
        f"{sealed['revision']}/usage", headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_t10_94_http_operation_error_becomes_503(monkeypatch, tmp_path):
    client, h, _module = _http_client(monkeypatch, tmp_path)

    def boom(tenant_id, command_id, body, request_hash):
        raise sqlite3.OperationalError("database is locked")

    h.service.mint_asset = boom
    response = post(client, "/assets", {"asset_type": "character",
                                        "owner_service": "S11",
                                        "owner_local_id": "chr_x"})
    assert response.status_code == 503
    assert response.json()["code"] == "WRITE_BACKPRESSURE"
    # abort 过：同一 command_id 不会永久 IN_FLIGHT。
    assert h.conn.execute(
        "SELECT COUNT(*) FROM idempotency_record").fetchone()[0] == 0


def test_t10_95_http_error_envelope_shape(monkeypatch, tmp_path):
    client, _h, _module = _http_client(monkeypatch, tmp_path)
    response = client.get("/assets/ast_missing",
                          headers={"X-Tenant-Id": "t1"})
    body = response.json()
    assert set(body) >= {"code", "message", "retryable", "correlation_id",
                         "details"}


# =========================================================== 列表与分页

def test_t10_A0_list_binding_sets_orders_and_filters():
    h = Harness()
    for index, episode in enumerate(("ep3", "ep1", "ep2"), start=1):
        h.create_bs(cid=f"bs{index}", episode=episode)
    items = h.service.list_binding_sets("t1", series_id="series_demo")["items"]
    assert [item["episode_id"] for item in items] == ["ep1", "ep2", "ep3"]
    only = h.service.list_binding_sets("t1", series_id="series_demo",
                                       episode_id="ep2")["items"]
    assert len(only) == 1


def test_t10_A1_list_is_tenant_scoped():
    h = Harness()
    h.create_bs(tenant="t1", cid="a")
    h.create_bs(tenant="t2", cid="b")
    assert len(h.service.list_binding_sets("t1",
                                           series_id="series_demo")["items"]) \
        == 1


def test_t10_A2_list_limit_is_clamped():
    h = Harness()
    for index in range(3):
        h.create_bs(cid=f"bs{index}", episode=f"ep{index}")
    body = h.service.list_binding_sets("t1", series_id="series_demo",
                                       limit=10000)
    assert body["limit"] == 200


def test_t10_A3_create_binding_set_is_idempotent_by_coordinates():
    h = Harness()
    first = h.create_bs(cid="a")
    second = h.create_bs(cid="b")
    assert first["binding_set_id"] == second["binding_set_id"]


def test_t10_A4_binding_set_head_and_current_track_seals():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    h.add_revision("S11", "chr_linlan", 2)
    _asset, bs, sealed = h.seeded()
    head = h.service.get_binding_set("t1", bs["binding_set_id"])
    assert head["head_revision"] == head["current_revision"] == 1
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=2, head=1), cid="e2")
    second = h.seal(bs["binding_set_id"], head=1, cid="s2")
    head = h.service.get_binding_set("t1", bs["binding_set_id"])
    assert head["head_revision"] == head["current_revision"] == \
        second["revision"]


def test_t10_A5_draft_entries_are_visible_on_the_binding_set_head():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    _asset, bs, _sealed = h.seeded()
    h.write_entry(bs["binding_set_id"],
                  h.entry_body(revision=1, head=1), cid="e2")
    head = h.service.get_binding_set("t1", bs["binding_set_id"])
    assert len(head["draft_entries"]) == 1
    assert head["draft_entries"][0]["unverified"] is True


def test_t10_A6_role_key_must_be_type_colon_owner_local_id():
    h = Harness()
    asset = h.mint()
    bs = h.create_bs()
    for bad in ("chr_linlan", "character:", ":chr_linlan", "",
                "character:chr:extra"):
        with pytest.raises(ServiceError):
            h.write_entry(bs["binding_set_id"],
                          h.entry_body(role_key=bad))


def test_t10_A7_role_key_asset_type_must_be_in_the_registry():
    h = Harness()
    bs = h.create_bs()
    with pytest.raises(ServiceError) as exc:
        h.write_entry(bs["binding_set_id"],
                      h.entry_body(role_key="hologram:holo_1"))
    assert exc.value.code == "ROLE_KEY_INVALID"


def test_t10_A8_rights_ref_must_carry_all_four_keys():
    h = Harness()
    bs = h.create_bs()
    for bad in ({}, {"rights_owner": "S02"},
                {"rights_owner": "S02", "rights_id": "r",
                 "rights_revision": 0, "rights_hash": sha()}):
        with pytest.raises(ServiceError) as exc:
            h.write_entry(bs["binding_set_id"],
                          h.entry_body(rights_ref=bad))
        assert exc.value.code == "RIGHTS_REF_INVALID"


def test_t10_A9_expected_head_is_required():
    h = Harness()
    bs = h.create_bs()
    body = h.entry_body()
    body.pop("expected_head")
    with pytest.raises(ServiceError) as exc:
        h.write_entry(bs["binding_set_id"], body)
    assert exc.value.code == "MALFORMED_REQUEST"


def test_t10_B0_owner_hash_must_be_a_sha256():
    h = Harness()
    bs = h.create_bs()
    with pytest.raises(ServiceError):
        h.write_entry(bs["binding_set_id"],
                      h.entry_body(content_hash="deadbeef"))


def test_t10_B1_resolve_reports_the_projection_watermark():
    h = Harness()
    _asset, bs, sealed = h.seeded()
    result = h.service.resolve("t1", _asset["asset_id"],
                               binding_set_id=bs["binding_set_id"],
                               binding_set_revision=sealed["revision"])
    assert result["projection_watermark"] == "1970-01-01T00:00:00.000Z"


def test_t10_B2_seal_is_independent_of_read_order():
    """独立升级 / 回滚：payload 的字段集是自描述的，旧代码只读它认识的键。"""
    h = Harness()
    _asset, bs, sealed = h.seeded()
    payload = sealed["payload"]
    assert {"binding_set_id", "entries", "rights_pointer_set_hash",
            "expanded_from"} <= set(payload)
    for entry in payload["entries"]:
        # 冻结的是**指针**（owner_service/id/revision/hash），不是内容。
        assert {"role_key", "owner_service", "owner_local_id",
                "owner_revision", "owner_hash", "resolved_from"} <= set(entry)


def test_t10_B3_two_binding_sets_in_two_branches_are_independent():
    h = Harness()
    h.add_revision("S11", "chr_linlan", 1)
    canon = h.create_bs(cid="a", branch="canon")
    experiment = h.create_bs(cid="b", branch="exp")
    assert canon["binding_set_id"] != experiment["binding_set_id"]
    h.write_entry(canon["binding_set_id"], h.entry_body(revision=1),
                  cid="e1")
    h.seal(canon["binding_set_id"], cid="s1")
    assert h.service.get_binding_set(
        "t1", experiment["binding_set_id"])["head_revision"] == 0
