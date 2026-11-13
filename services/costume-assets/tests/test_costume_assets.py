"""S12 单测。对照 `plans/S12-costume-assets.md` §8 的验收清单与 §4.4 的
F1–F15；每个验收行落在哪个测试类上见下方映射，映射本身由
`TestAcceptanceMap` 自证（方案里的每个 ID 都必须出现在这张表里）。

上游全部是 stub，**按真实的上游契约写**：

- S28 `GET /artifacts/{artifact_id}` 用 `artifact_ingest.service.get_artifact`
  的形状，这里只读 `artifact_id` 与 `content_hash` 两项，测试里也只塞这两项。
- QC 结论不是任何服务的真实响应（C12-2 / CCA-1：仓库里没有产出
  `identity_invariance_check` / 图文匹配的服务），所以测试用**命令内联的
  `evaluation`**，与内核支持的唯一来源一致。

结构性断言一律用「库里没有」而不是「响应里没有」：这一服务最严重的错误是
变成第二正史（谁穿这套、现在破成什么样），而响应体少个字段证明不了什么，
少一列才能。
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

from costume_assets.schema import CONTRACT_GAPS, SCHEMA  # noqa: F401
from costume_assets.service import (
    CostumeAssetsService,
    ServiceError,
    content_hash,
    variant_check,
    variant_default,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


# ------------------------------------------------------------------ 上游形状

def artifact(artifact_id="art_none", content_hash=HASH_A, **over):
    """S28 `get_artifact` 的字段集；本服务只读这两项。"""
    base = {"artifact_id": artifact_id, "content_hash": content_hash}
    base.update(over)
    return base


def piece(piece_id="top", color="#B23A48", material="silk", **over):
    base = {"piece_id": piece_id, "garment": "blouse",
            "color_primary": color, "color_secondary": None,
            "material": material, "pattern": "solid", "fit": "fitted",
            "coverage": "long_sleeve"}
    base.update(over)
    return base


def outfit(**over):
    base = {
        "pieces": [
            piece("top"),
            piece("bottom", color="#1A1A1A", material="wool",
                  garment="skirt", fit="a_line"),
            piece("shoes", color="#000000", material="leather",
                  garment="pumps", heel_cm_band="5-8"),
        ],
        "layering_order": ["top", "bottom", "shoes"],
        "silhouette": "structured_waist",
        "era_style": "contemporary_2010s_office",
    }
    base.update(over)
    return base


AXES = {
    "damage": ["none", "torn_shoulder"],
    "wetness": ["dry", "damp"],
    "makeup": ["natural", "injured_smudged"],
}


def manifest(**over):
    base = {
        "damage": {
            "none": {"artifact_id": "art_damage_none", "content_hash": HASH_A},
            "torn_shoulder": {"artifact_id": "art_damage_torn",
                              "content_hash": HASH_B},
        },
        "wetness": {
            "dry": {"artifact_id": "art_wet_dry", "content_hash": HASH_A},
            "damp": {"artifact_id": "art_wet_damp", "content_hash": HASH_B},
        },
        "makeup": {
            "natural": {"artifact_id": "art_makeup_natural",
                        "content_hash": HASH_A},
            "injured_smudged": {"artifact_id": "art_makeup_injured",
                                "content_hash": HASH_C},
        },
    }
    base.update(over)
    return base


ACCESSORIES = [
    {"name": "珍珠项链", "target_service": "S14", "target_id": "prp_necklace",
     "target_revision": 1},
    {"name": "婚戒"},  # 纯描述性，不引用任何服务
]


def candidate_body(**over):
    base = {
        "expected_current_revision": 0,
        "source_kind": "generated",
        "rights_ref": {"service_id": "S02", "id": "rights_1"},
        "quality_gate_ref": {"service_id": "S35", "id": "qc_1", "revision": 1},
        "outfit": outfit(),
        "material_palette": {"primary": "#B23A48", "accent": "#1A1A1A"},
        "accessories": list(ACCESSORIES),
        "variant_axes": {k: list(v) for k, v in AXES.items()},
        "variant_manifest": manifest(),
        "evaluation": {"identity_invariance_check": "pass",
                       "image_text_match": True},
    }
    base.update(over)
    return base


# ------------------------------------------------------------------ harness

class Harness:
    def __init__(self, conn=None, **over):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)
        self.artifacts = over.get("artifacts", {
            "art_damage_none": artifact("art_damage_none", HASH_A),
            "art_damage_torn": artifact("art_damage_torn", HASH_B),
            "art_wet_dry": artifact("art_wet_dry", HASH_A),
            "art_wet_damp": artifact("art_wet_damp", HASH_B),
            "art_makeup_natural": artifact("art_makeup_natural", HASH_A),
            "art_makeup_injured": artifact("art_makeup_injured", HASH_C),
        })
        self.calls = {"artifact": 0}
        self.service = CostumeAssetsService(
            self.conn,
            artifact_source=over.get("artifact_source") or self._artifact,
            quality_gate_source=over.get("quality_gate_source"),
            max_pending_candidates=over.get("max_pending_candidates", 64),
        )

    def _artifact(self, tenant_id, artifact_id):
        self.calls["artifact"] += 1
        value = self.artifacts.get(artifact_id)
        if isinstance(value, Exception):
            raise value
        return value

    # ---- 幂等外壳：与 api/app.py 的 command() 同语义 ----
    def command(self, fn, body, tenant_id="t1", command_id="c1"):
        payload = {**body, "command_id": command_id}
        digest = canonical_request_hash(payload)
        record = self.idem.begin(tenant_id, command_id, digest)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = fn(tenant_id, command_id, payload)
        except Exception:
            self.idem.abort(tenant_id, command_id)
            raise
        self.idem.finish(tenant_id, command_id,
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ---- 命令助手 ----
    def create(self, tenant_id="t1", command_id="c1", **over):
        body = {"project_id": "prj_1", "series_scope": "ser_1",
                "display_name": "林岚·红裙",
                "costume_kind": "character_outfit",
                "character_ref": {"service_id": "S11", "id": "chr_linlan"}}
        body.update(over)
        return self.command(
            lambda t, c, b: self.service.create_costume(t, c, b), body,
            tenant_id, command_id)

    def draft(self, costume_id, tenant_id="t1", command_id="c2", **over):
        body = candidate_body(**over)
        return self.command(
            lambda t, c, b: self.service.draft_candidate(t, c, costume_id, b),
            body, tenant_id, command_id)

    def accept(self, costume_id, candidate_id, tenant_id="t1",
               command_id="c3", **over):
        body = {"expected_current_revision": 0, "set_canonical": True,
                "decision_kind": "human_approved"}
        body.update(over)
        return self.command(
            lambda t, c, b: self.service.accept_candidate(
                t, c, costume_id, candidate_id, b), body, tenant_id, command_id)

    def seeded(self, **over):
        """库里有 canonical v1 的服装。返回 (costume_id, candidate_id)。"""
        costume = self.create(**over)
        cand = self.draft(costume["costume_id"])
        self.accept(costume["costume_id"], cand["candidate_id"])
        return costume["costume_id"], cand["candidate_id"]

    # ---- 查询助手 ----
    def rows(self, table, **where):
        clause = " AND ".join(f"{k}=?" for k in where) or "1"
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {clause}", tuple(where.values())
        ).fetchall()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def events(self):
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM outbox ORDER BY created_at, sequence")]


def raises(status: int, code: str):
    """`pytest.raises` 的窄版：同时钉住状态码与错误码。

    `CommandConflict` 由 SDK 抛、没有 `status`（HTTP 层固定转 409），
    所以这里也接受它——同键不同 body 在两个实现里是同一个 409。
    """
    import contextlib

    @contextlib.contextmanager
    def _cm():
        with pytest.raises((ServiceError, CommandConflict)) as info:
            yield info
        value = info.value
        assert getattr(value, "status", 409) == status, (
            getattr(value, "status", 409), value.code)
        assert value.code == code, value.code
    return _cm()


# ================================================================ 纯函数

class TestContentHash:
    def test_ignores_costume_identity_and_clock(self):
        payload = candidate_body()
        payload.pop("expected_current_revision")
        # 同样的内容得出同样的 hash —— 与哪套服装、什么时候接受无关。
        assert content_hash(payload) == content_hash(dict(payload))

    def test_quality_gate_ref_is_not_content(self):
        """同一套服装换一次 QC 结果，内容没变就不该产生第二条 revision。"""
        a = candidate_body()
        b = candidate_body(quality_gate_ref={"service_id": "S35", "id": "qc_2"})
        for body in (a, b):
            body.pop("expected_current_revision")
            body.pop("evaluation", None)
        assert content_hash(a) == content_hash(b)

    def test_e26_c2_one_variant_changing_changes_the_whole_hash(self):
        """E26-C2：修一个变体，其余变体 hash 不变，但整版 `content_hash` 变。"""
        a = candidate_body()
        a.pop("expected_current_revision")
        new_manifest = manifest()
        new_manifest["damage"]["torn_shoulder"] = {
            "artifact_id": "art_damage_torn_2", "content_hash": "d" * 64}
        b = candidate_body(variant_manifest=new_manifest)
        b.pop("expected_current_revision")
        assert content_hash(a) != content_hash(b)
        # 未改动的两个轴逐字节相同 —— 这是"只有一处变"的证据
        assert a["variant_manifest"]["wetness"] == \
            b["variant_manifest"]["wetness"]
        assert a["variant_manifest"]["makeup"] == b["variant_manifest"]["makeup"]

    def test_hash_is_64_lowercase_hex(self):
        body = candidate_body()
        body.pop("expected_current_revision")
        digest = content_hash(body)
        assert len(digest) == 64
        assert all(ch in "0123456789abcdef" for ch in digest)


class TestVariantCheck:
    def test_complete_manifest_has_no_missing(self):
        check = variant_check(AXES, manifest())
        assert check["missing_variants"] == []
        assert check["extra_variants"] == []

    def test_missing_value_is_named_with_axis(self):
        m = manifest()
        del m["wetness"]["damp"]
        check = variant_check(AXES, m)
        assert check["missing_variants"] == [{"axis": "wetness",
                                              "value": "damp"}]

    def test_extra_value_is_recorded_not_rejected(self):
        # 多画一个没声明的档位不是错误，但要能解释"为什么图比轴多"。
        m = manifest()
        m["damage"]["bloodied"] = {"artifact_id": "art_bloodied",
                                   "content_hash": HASH_C}
        check = variant_check(AXES, m)
        assert check["missing_variants"] == []
        assert check["extra_variants"] == [{"axis": "damage",
                                            "value": "bloodied"}]

    def test_default_is_first_declared_value(self):
        # C12-10：方案没定义默认值语义，本实现取第一个声明值。
        assert variant_default(AXES) == {"damage": "none",
                                         "wetness": "dry",
                                         "makeup": "natural"}


# ================================================================ 结构边界

class TestNoSecondAuthority:
    """§0 / §1.1 / §2.8 / §9 反模式 2、3、11。断言的是**列不存在**。"""

    def columns(self, conn, table):
        return {r["name"] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}

    def test_revision_has_no_wearer_or_scene_columns(self):
        # §8 E02-C1..C3 只有在 S12 不存"这一场谁穿"时才成立：
        # 一存就变成第二正史，S10 的 binding 与它必然对不上。
        h = Harness()
        cols = self.columns(h.conn, "costume_revision")
        banned = {"worn_by", "wearer", "character_id", "character_ref_direct",
                  "scene_ids", "scene_id", "shots", "shot_ids",
                  "current_damage", "current_wetness", "current_variant",
                  "current_makeup", "location", "story_reason",
                  "damage_history", "state"}
        assert not (cols & banned), cols & banned

    def test_no_forbidden_tables(self):
        h = Harness()
        names = {r["name"] for r in h.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        # §2.8 的五张表一张都不建；换个别名也不该出现。
        banned_prefixes = ("costume_character_identity", "costume_story_state",
                           "costume_damage_history", "costume_wearing_binding",
                           "costume_rights_status", "costume_scene_compat")
        assert not {n for n in names if n.startswith(banned_prefixes)}
        assert names >= {"costume", "costume_revision", "costume_material_ref",
                         "costume_candidate", "canonical_pointer",
                         "revision_reference", "costume_unverified_ref"}

    def test_character_ref_is_not_a_foreign_key(self):
        # §1.2：软引用。没有 REFERENCES，也不在 S12 的库里解析 S11。
        h = Harness()
        ddl = h.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table'"
            " AND name='costume'").fetchone()[0]
        assert "character_ref" in ddl
        refs = [r for r in h.conn.execute(
            "PRAGMA foreign_key_list(costume)").fetchall()]
        assert all(dict(r)["from"] != "character_ref" for r in refs)

    def test_variant_axes_are_capabilities_not_history(self):
        h = Harness()
        costume_id, _ = h.seeded()
        row = h.rows("costume_revision", tenant_id="t1",
                     costume_id=costume_id, revision=1)[0]
        axes = json.loads(row["variant_axes"])
        assert axes == AXES
        # 接受之后没有哪个轴被"选中"成当前状态
        assert "current" not in json.dumps(axes)

    def test_no_rights_verdict_column(self):
        # §2.8 / §9 第 10 条：S12 只存 `rights_ref` 指针，不判权利。
        h = Harness()
        cols = self.columns(h.conn, "costume_revision")
        assert not ({c for c in cols if "right" in c} - {"rights_ref"}), \
            {c for c in cols if "right" in c} - {"rights_ref"}


class TestDatabaseConstraints:
    """裸 SQL 撞约束。

    内核本来就不会产生坏行，所以走内核的用例只能证明「当前代码路径没事」。
    这一组绕开内核，证明「库里写不进坏行」——将来多一条写路径时，挡住它的
    必须是库。每种形状都要撞到**它该撞的那一条**，撞错约束等于没测。
    """

    def test_parent_revision_must_precede_child(self):
        h = Harness()
        costume_id, _ = h.seeded()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "UPDATE costume_revision SET parent_revision=2"
                " WHERE tenant_id='t1' AND costume_id=? AND revision=1",
                (costume_id,))
        assert "costr_parent_lt" in str(info.value)

    def test_canonical_pointer_cannot_run_ahead_of_head(self):
        h = Harness()
        costume_id, _ = h.seeded()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "UPDATE costume SET canonical_revision=9"
                " WHERE tenant_id='t1' AND costume_id=?", (costume_id,))
        assert "canonical_within_head" in str(info.value)

    def test_only_one_live_canonical_pointer_per_costume(self):
        # 要撞的是**部分唯一索引**，不是主键：主键是
        # (tenant_id, costume_id, revision)，所以必须先造出一个还没有指针行的
        # 修订（v2 接受但不改指针），再给它插一条"活着的"指针。
        h = Harness()
        costume_id, _ = h.seeded()
        cand2 = h.draft(costume_id, command_id="c50",
                        expected_current_revision=1,
                        outfit=outfit(pieces=[
                            piece("top", color="#223344")] +
                            outfit()["pieces"][1:],
                            layering_order=["top", "bottom", "shoes"]))
        h.accept(costume_id, cand2["candidate_id"], command_id="c51",
                 expected_current_revision=1, set_canonical=False)
        assert h.rows("canonical_pointer", tenant_id="t1",
                      costume_id=costume_id, revision=2) == []
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "INSERT INTO canonical_pointer (tenant_id, costume_id, revision,"
                " decided_at, decided_by, decision_kind)"
                " VALUES ('t1',?,2,'2026-01-01T00:00:00.000Z','x',"
                "'human_approved')", (costume_id,))
        # 两列而不是三列：三列就是撞了主键，说明这条用例什么都没测到。
        assert str(info.value) == (
            "UNIQUE constraint failed: canonical_pointer.tenant_id, "
            "canonical_pointer.costume_id")

    def test_tombstoned_costume_cannot_be_resurrected_by_sql(self):
        # §3.3：复活走新 costume_id。库里有触发器，不依赖内核。
        h = Harness()
        costume_id, _ = h.seeded()
        h.command(lambda t, c, b: h.service.change_status(
            t, c, costume_id, "tombstoned", b), {}, command_id="c60")
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "UPDATE costume SET status='active' WHERE tenant_id='t1'"
                " AND costume_id=?", (costume_id,))
        assert "tombstoned" in str(info.value)
        # 反向对照：tombstoned → tombstoned 不是复活，不该被挡。
        h.conn.execute(
            "UPDATE costume SET status='tombstoned' WHERE tenant_id='t1'"
            " AND costume_id=?", (costume_id,))

    def test_same_name_in_one_series_is_rejected(self):
        # §6.1：POST /costumes 天然幂等的数据库形态。只靠内核的"已存在就返回"
        # 时，两条并发注册都读到"不存在"，就都插进去了。
        h = Harness()
        h.create()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "INSERT INTO costume (tenant_id, costume_id, project_id,"
                " series_scope, display_name, costume_kind, status,"
                " current_revision, created_by, created_at)"
                " VALUES ('t1','cos_other','prj_1','ser_1','林岚·红裙',"
                "'character_outfit','active',0,'x','2026-01-01T00:00:00.000Z')")
        assert "UNIQUE" in str(info.value)

    def test_same_name_different_kind_is_allowed(self):
        # 唯一键含 costume_kind：同名制服与角色造型是两条记录。
        h = Harness()
        h.create()
        h.create(command_id="c2", costume_kind="uniform")
        assert h.count("costume") == 2

    @pytest.mark.parametrize("sql, fragment", [
        ("UPDATE costume SET costume_kind='invented'", "costume_kind"),
        ("UPDATE costume SET status='archived'", "status"),
        ("UPDATE costume_revision SET source_kind='invented'", "source_kind"),
        ("UPDATE costume_revision SET content_hash='tooshort'",
         "costr_hash_len"),
        # 大写 hex 也不算"64 位小写 hex"
        ("UPDATE costume_revision SET content_hash="
         "'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'",
         "costr_hash_len"),
        ("UPDATE costume_candidate SET state='ZOMBIE'", "state"),
        ("UPDATE canonical_pointer SET decision_kind='robot'", "decision_kind"),
        ("UPDATE costume_material_ref SET sheen='glossy'", "sheen"),
    ])
    def test_enum_check_rejects_value_outside_vocabulary(self, sql, fragment):
        h = Harness()
        h.seeded()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(sql)
        assert fragment in str(info.value), str(info.value)

    def test_revision_reference_role_check_rejects_unknown_role(self):
        # §2.6 的四值 CHECK。表里有行才谈得上撞它。
        h = Harness()
        costume_id, _ = h.seeded()
        h.command(lambda t, c, b: h.service.add_revision_references(
            t, c, costume_id, b), {
            "revision": 1,
            "references": [{"ref_role": "worn_with", "target_service": "S12",
                            "target_id": "cos_other", "target_revision": 1}],
        }, command_id="c40")
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute("UPDATE revision_reference SET ref_role='owned_by'")
        assert "ref_role" in str(info.value)

    def test_foreign_keys_are_enforced(self):
        # `PRAGMA foreign_keys` 默认 OFF：不显式打开，所有 REFERENCES 都只是注释。
        h = Harness()
        assert h.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "INSERT INTO costume_candidate (tenant_id, candidate_id,"
                " costume_id, content_hash, payload, evaluation, variant_check,"
                " state, created_by, created_at, expires_at)"
                " VALUES ('t1','cand_ghost','cos_ghost',?, '{}','{}','{}',"
                "'PENDING','x','2026-01-01T00:00:00.000Z',"
                "'2026-02-01T00:00:00.000Z')", (HASH_A,))
        assert "FOREIGN KEY" in str(info.value)


# ================================================================ 登记与幂等

class TestCreateAndIdempotency:
    def test_create_returns_stable_id(self):
        h = Harness()
        first = h.create()
        assert first["costume_id"].startswith("cos_")
        assert first["created"] is True
        assert first["status"] == "active"
        assert first["canonical_revision"] is None
        assert first["current_revision"] == 0

    def test_same_key_same_body_replays(self):
        h = Harness()
        first = h.create(command_id="c1")
        again = h.create(command_id="c1")
        assert again["replayed"] is True
        assert again["costume_id"] == first["costume_id"]
        assert h.count("costume") == 1

    def test_same_key_different_body_is_conflict(self):
        # E12：同键不同 body 是 409，不是覆盖。
        h = Harness()
        h.create(command_id="c1")
        with raises(409, "IDEMPOTENCY_KEY_REUSED"):
            h.create(command_id="c1", display_name="别的名字")
        assert h.count("costume") == 1

    def test_repeat_registration_returns_existing_id(self):
        # E12：重复登记返回既有 ID，不是第二条。
        h = Harness()
        first = h.create(command_id="c1")
        again = h.create(command_id="c2")
        assert again["costume_id"] == first["costume_id"]
        assert again["created"] is False
        assert h.count("costume") == 1

    def test_e28_c2_same_name_in_two_tenants_is_independent(self):
        h = Harness()
        a = h.create(tenant_id="t1")
        b = h.create(tenant_id="t2")
        assert a["costume_id"] != b["costume_id"]
        assert h.count("costume") == 2

    def test_unknown_costume_kind_is_malformed(self):
        h = Harness()
        with raises(400, "MALFORMED_REQUEST"):
            h.create(costume_kind="invented")

    def test_character_ref_may_be_null(self):
        # §1.2 / CCA-7：通用服装根本没有角色。
        h = Harness()
        out = h.create(costume_kind="generic_wardrobe", character_ref=None)
        assert out["character_ref"] is None
        assert out["canonical_revision"] is None

    def test_character_ref_shape_is_checked_but_not_resolved(self):
        h = Harness()
        with raises(400, "MALFORMED_REQUEST"):
            h.create(character_ref={"id": "chr_linlan"})
        # 指向一个 S11 不存在的角色也照样登记：软引用。
        out = h.create(character_ref={"service_id": "S11", "id": "chr_ghost"})
        assert out["character_ref"] == {"service_id": "S11", "id": "chr_ghost"}


# ================================================================ 起草校验

class TestCandidateValidation:
    def test_happy_path_creates_pending_candidate(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        assert cand["candidate_id"].startswith("cand_")
        assert cand["state"] == "PENDING"
        assert cand["variant_check"] == {"missing_variants": [],
                                         "extra_variants": []}
        assert h.count("costume_candidate") == 1
        assert h.count("costume_revision") == 0

    def test_f1_missing_artifact_is_422(self):
        # F1
        h = Harness()
        costume = h.create()
        m = manifest()
        m["damage"]["torn_shoulder"] = {"artifact_id": "art_ghost",
                                        "content_hash": HASH_B}
        with raises(422, "ARTIFACT_NOT_FOUND"):
            h.draft(costume["costume_id"], variant_manifest=m)
        assert h.count("costume_candidate") == 0

    def test_f1_artifact_hash_mismatch_is_422(self):
        # F1
        h = Harness()
        costume = h.create()
        m = manifest()
        m["damage"]["torn_shoulder"] = {"artifact_id": "art_damage_torn",
                                        "content_hash": HASH_C}
        with raises(422, "ARTIFACT_HASH_MISMATCH"):
            h.draft(costume["costume_id"], variant_manifest=m)

    def test_f11_s28_timeout_is_503_and_persists_no_candidate(self):
        # F11：拿不到校验结论就存下来，等于把伪造的 id 写进不可变 revision。
        h = Harness(artifacts={
            k: ConnectionError("timed out") for k in (
                "art_damage_none", "art_damage_torn", "art_wet_dry",
                "art_wet_damp", "art_makeup_natural", "art_makeup_injured")})
        costume = h.create()
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            h.draft(costume["costume_id"])
        assert h.count("costume_candidate") == 0

    def test_unconfigured_s28_fails_closed(self):
        from costume_assets.deps import build_artifact_source
        source = build_artifact_source(None)
        with pytest.raises(ServiceError) as info:
            source("t1", "art_1")
        assert info.value.status == 503
        assert info.value.code == "ARTIFACT_SOURCE_UNAVAILABLE"
        h = Harness(artifact_source=source)
        costume = h.create()
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            h.draft(costume["costume_id"])

    def test_f2_variant_coverage_incomplete_is_422(self):
        # F2 / §2.2 / §9 反模式 8：声明了 torn_shoulder 却没有图。
        h = Harness()
        costume = h.create()
        m = manifest()
        del m["damage"]["torn_shoulder"]
        with raises(422, "VARIANT_COVERAGE_INCOMPLETE"):
            h.draft(costume["costume_id"], variant_manifest=m)
        assert h.count("costume_candidate") == 0

    def test_variant_coverage_details_name_the_gap(self):
        h = Harness()
        costume = h.create()
        m = manifest()
        del m["wetness"]["damp"]
        with pytest.raises(ServiceError) as info:
            h.draft(costume["costume_id"], variant_manifest=m)
        assert info.value.details["axis"] == "wetness"
        assert info.value.details["missing_values"] == ["damp"]

    def test_f3_outfit_layering_must_cover_every_piece(self):
        # F3
        h = Harness()
        costume = h.create()
        with raises(422, "OUTFIT_INCONSISTENT"):
            h.draft(costume["costume_id"],
                    outfit=outfit(layering_order=["top", "bottom"]))

    def test_outfit_duplicate_piece_id_is_422(self):
        # F3：两件同 id 的部件，读路径不知道 layering_order 指哪一件。
        h = Harness()
        costume = h.create()
        bad = outfit()
        bad["pieces"] = [piece("top"), piece("top", color="#000000")]
        bad["layering_order"] = ["top"]
        with raises(422, "OUTFIT_INCONSISTENT"):
            h.draft(costume["costume_id"], outfit=bad)

    def test_outfit_layering_referencing_unknown_piece_is_422(self):
        h = Harness()
        costume = h.create()
        with raises(422, "OUTFIT_INCONSISTENT"):
            h.draft(costume["costume_id"],
                    outfit=outfit(layering_order=["top", "bottom", "shoes",
                                                  "hat"]))

    def test_f4_free_text_color_is_422(self):
        # F4 / §2.3 / §9 反模式 7。
        h = Harness()
        costume = h.create()
        bad = outfit()
        bad["pieces"][0]["color_primary"] = "深红色"
        with raises(422, "COLOR_FORMAT_INVALID"):
            h.draft(costume["costume_id"], outfit=bad)

    def test_short_hex_is_422(self):
        # `#fff` 不是 `#RRGGBB`。接受它会让下游拿到两种长度。
        h = Harness()
        costume = h.create()
        bad = outfit()
        bad["pieces"][0]["color_primary"] = "#fff"
        with raises(422, "COLOR_FORMAT_INVALID"):
            h.draft(costume["costume_id"], outfit=bad)

    def test_piece_without_material_is_422(self):
        h = Harness()
        costume = h.create()
        bad = outfit()
        del bad["pieces"][0]["material"]
        with raises(422, "OUTFIT_INCONSISTENT"):
            h.draft(costume["costume_id"], outfit=bad)

    def test_f7_stale_expected_revision_is_conflict(self):
        # F7
        h = Harness()
        costume = h.create()
        with raises(409, "VERSION_CONFLICT"):
            h.draft(costume["costume_id"], expected_current_revision=5)

    def test_missing_expected_revision_is_malformed(self):
        h = Harness()
        costume = h.create()
        body = candidate_body()
        del body["expected_current_revision"]
        with raises(400, "MALFORMED_REQUEST"):
            h.command(lambda t, c, b: h.service.draft_candidate(
                t, c, costume["costume_id"], b), body, command_id="c9")

    def test_tombstoned_costume_takes_no_new_candidate(self):
        h = Harness()
        costume_id, _ = h.seeded()
        h.command(lambda t, c, b: h.service.change_status(
            t, c, costume_id, "tombstoned", b), {}, command_id="c60")
        with raises(422, "COSTUME_TOMBSTONED"):
            h.draft(costume_id, command_id="c61",
                    expected_current_revision=1,
                    outfit=outfit(pieces=[piece("top", color="#334455")],
                                  layering_order=["top"]))

    def test_pending_cap_is_429(self):
        # C12-17
        h = Harness(max_pending_candidates=2)
        costume = h.create()
        h.draft(costume["costume_id"], command_id="c10")
        h.draft(costume["costume_id"], command_id="c11",
                outfit=outfit(pieces=[piece("top", color="#111111")],
                              layering_order=["top"]))
        with raises(429, "WRITE_BACKPRESSURE"):
            h.draft(costume["costume_id"], command_id="c12",
                    outfit=outfit(pieces=[piece("top", color="#222222")],
                                  layering_order=["top"]))
        assert h.count("costume_candidate") == 2

    def test_expired_candidate_cannot_be_accepted(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        h.conn.execute(
            "UPDATE costume_candidate SET expires_at='2000-01-01T00:00:00.000Z'"
            " WHERE tenant_id='t1' AND candidate_id=?", (cand["candidate_id"],))
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.accept(costume["costume_id"], cand["candidate_id"],
                     command_id="c30")
        # 过期必须**落库**为 EXPIRED：告警说候选过期、库里还是 PENDING 的话，
        # 下一次 draft 的 64 上限照样数它。
        row = h.rows("costume_candidate", tenant_id="t1",
                     candidate_id=cand["candidate_id"])[0]
        assert row["state"] == "EXPIRED"

    def test_expired_candidate_cannot_be_rejected_either(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        h.conn.execute(
            "UPDATE costume_candidate SET expires_at='2000-01-01T00:00:00.000Z'"
            " WHERE tenant_id='t1' AND candidate_id=?", (cand["candidate_id"],))
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.command(lambda t, c, b: h.service.reject_candidate(
                t, c, costume["costume_id"], cand["candidate_id"], b), {},
                command_id="c31")
        row = h.rows("costume_candidate", tenant_id="t1",
                     candidate_id=cand["candidate_id"])[0]
        assert row["state"] == "EXPIRED"

    def test_expired_candidates_stop_counting_against_the_cap(self):
        # 回收发生在命令事务**之前**：写在同一事务里然后 409 会一起回滚。
        h = Harness(max_pending_candidates=2)
        costume = h.create()
        for i in range(2):
            h.draft(costume["costume_id"], command_id=f"c1{i}",
                    outfit=outfit(pieces=[piece("top", color=f"#2{i}2233")],
                                  layering_order=["top"]))
        h.conn.execute(
            "UPDATE costume_candidate SET expires_at='2000-01-01T00:00:00.000Z'"
            " WHERE tenant_id='t1'")
        # 两条都过期了，第三条应该能进
        out = h.draft(costume["costume_id"], command_id="c20",
                      outfit=outfit(pieces=[piece("top", color="#999999")],
                                    layering_order=["top"]))
        assert out["state"] == "PENDING"
        assert h.count("costume_candidate") == 3

    def test_unexpired_candidate_still_accepts(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        out = h.accept(costume["costume_id"], cand["candidate_id"])
        assert out["revision"] == 1

    def test_f14_accessory_to_s14_is_allowed_and_recorded(self):
        # F14：允许引用 S14 还没有的道具，但必须显式告诉调用方。
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        assert cand["unverified_refs"] == [{
            "ref_key": "S14:prp_necklace@1", "target_service": "S14",
            "target_id": "prp_necklace", "target_revision": 1}]

    def test_descriptive_accessory_is_not_an_unverified_ref(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"], accessories=[
            {"name": "白色手套"}])
        assert cand["unverified_refs"] == []

    def test_parent_revision_must_exist(self):
        h = Harness()
        costume = h.create()
        with raises(404, "NOT_FOUND"):
            h.draft(costume["costume_id"], parent_revision=3)


# ================================================================ 依赖分类

class TestDependencyContract:
    """`deps.py` 的失败分类是 503/422 的分界线，必须逐条钉住。

    分类错了，一个"别的租户的 artifact"会变成 503 重试风暴，或者一个
    "上游挂了"会变成 422 让调用方去改参数。
    """

    def test_connection_error_becomes_503_retryable(self):
        h = Harness(artifacts={
            "art_damage_none": ConnectionError("timed out")})
        costume = h.create()
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            h.draft(costume["costume_id"])
        assert h.count("costume_candidate") == 0

    def test_upstream_5xx_is_keyed_by_the_caller(self):
        from costume_assets.service import ServiceError as SE
        h = Harness(artifacts={
            "art_damage_none": SE(502, "S28_INTERNAL", "boom")})
        costume = h.create()
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            h.draft(costume["costume_id"])

    def test_artifact_404_is_not_retryable(self):
        # 404 = "S28 说没有"，是调用方的错：422，且不可重试。
        h = Harness()
        costume = h.create()
        m = manifest()
        m["damage"]["none"] = {"artifact_id": "art_ghost",
                               "content_hash": HASH_A}
        with pytest.raises(ServiceError) as info:
            h.draft(costume["costume_id"], variant_manifest=m)
        assert (info.value.status, info.value.code) == (422, "ARTIFACT_NOT_FOUND")
        assert info.value.retryable is False

    def test_build_artifact_source_maps_http_404_to_none(self):
        # 真发一次 HTTP：service 依靠 None 来区分"没有"与"不可达"。
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from costume_assets.deps import build_artifact_source

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":"NOT_FOUND"}')

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            source = build_artifact_source(
                f"http://127.0.0.1:{server.server_port}")
            assert source("t1", "art_whatever") is None
        finally:
            server.shutdown()
            server.server_close()

    def test_build_artifact_source_returns_the_hash(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from costume_assets.deps import build_artifact_source

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(
                    artifact("art_x", HASH_A)).encode("utf-8"))

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            source = build_artifact_source(
                f"http://127.0.0.1:{server.server_port}")
            assert source("t1", "art_x")["content_hash"] == HASH_A
        finally:
            server.shutdown()
            server.server_close()

    def test_build_artifact_source_wraps_transport_failure(self):
        # 端口刚关掉 → URLError → 必须是 ConnectionError（503 那条路），
        # 不能是 ServiceError 或者 urllib 的原始异常。
        import socket
        from costume_assets.deps import build_artifact_source

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        source = build_artifact_source(f"http://127.0.0.1:{port}")
        with pytest.raises(ConnectionError):
            source("t1", "art_1")

    def test_quality_gate_unconfigured_does_not_break_inline(self):
        # C12-2 / CCA-1 的默认姿势：结论随命令内联传入。
        h = Harness(quality_gate_source=None)
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        assert cand["candidate_id"].startswith("cand_")

    def test_configured_quality_gate_is_read(self):
        h = Harness(quality_gate_source=lambda tenant, ref: {
            "identity_invariance_check": "fail"})
        costume = h.create()
        cand = h.draft(costume["costume_id"], evaluation=None,
                       quality_gate_ref={"service_id": "S35", "id": "qc_9"})
        with raises(422, "IDENTITY_CHECK_REQUIRED_FOR_AUTO"):
            h.accept(costume["costume_id"], cand["candidate_id"],
                     command_id="c30", decision_kind="policy_auto")


# ================================================================ 接受

class TestAccept:
    def test_accept_creates_immutable_revision_and_event(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        result = h.accept(costume["costume_id"], cand["candidate_id"])
        assert result["revision"] == 1
        assert result["became_canonical"] is True
        assert result["canonical_revision"] == 1
        assert h.count("costume_revision") == 1
        assert h.count("outbox") == 1

    def test_event_has_exactly_the_nine_contract_keys(self):
        h = Harness()
        costume_id, _ = h.seeded()
        (event,) = h.events()
        assert event["type"] == "asset.costume.revision.accepted"
        assert event["source"] == "S12"
        assert event["sequence"] == 1
        assert event["aggregate_id"] == costume_id
        assert event["subject"] == f"costume/{costume_id}"
        assert set(event["data"]) == {
            "schema_version", "costume_ref", "character_ref", "costume_kind",
            "became_canonical", "canonical_revision", "variant_axes",
            "unverified_refs", "rights_ref"}
        assert set(event["data"]["costume_ref"]) == {
            "service_id", "id", "revision", "content_hash"}

    def test_event_values_are_the_facts_not_placeholders(self):
        """键齐了不等于话说对了。`variant_axes` 是下游据此行动的唯一字段：
        S17 不知道"这一版支持哪些破损档"就只能猜（§5.3、CCA-2）。"""
        h = Harness()
        costume_id, cand_id = h.seeded()
        data = h.events()[0]["data"]
        assert data["schema_version"] == "2.0"
        assert data["costume_kind"] == "character_outfit"
        assert data["character_ref"] == {"service_id": "S11",
                                         "id": "chr_linlan"}
        assert data["became_canonical"] is True
        assert data["canonical_revision"] == 1
        # 三个轴全发，不按契约示例裁剪（CCA-2 的例子只有两轴）
        assert data["variant_axes"] == AXES
        assert data["rights_ref"] == {"service_id": "S02", "id": "rights_1"}
        assert data["unverified_refs"] == [{
            "ref_key": "S14:prp_necklace@1", "target_service": "S14",
            "target_id": "prp_necklace", "target_revision": 1}]
        # costume_ref 的 hash 必须是**这一版**的，不是空串
        revision = h.rows("costume_revision", tenant_id="t1",
                          costume_id=costume_id, revision=1)[0]
        assert data["costume_ref"]["content_hash"] == revision["content_hash"]
        assert len(revision["content_hash"]) == 64

    def test_event_sequence_equals_revision(self):
        h = Harness()
        costume_id, _ = h.seeded()
        cand2 = h.draft(costume_id, command_id="c10",
                        expected_current_revision=1,
                        outfit=outfit(pieces=[piece("top", color="#123456"),
                                              piece("bottom", color="#654321",
                                                    material="wool")],
                                      layering_order=["top", "bottom"]))
        h.accept(costume_id, cand2["candidate_id"], command_id="c11",
                 expected_current_revision=1)
        assert [e["sequence"] for e in h.events()] == [1, 2]

    def test_e08_c1_accept_without_canonical_keeps_pointer(self):
        # E08-C1：补一张细节图（set_canonical=false），canonical 不动。
        h = Harness()
        costume_id, _ = h.seeded()
        cand2 = h.draft(costume_id, command_id="c10",
                        expected_current_revision=1,
                        outfit=outfit(pieces=[piece("top", color="#123456"),
                                              piece("bottom", color="#654321",
                                                    material="wool")],
                                      layering_order=["top", "bottom"]))
        out = h.accept(costume_id, cand2["candidate_id"], command_id="c11",
                       expected_current_revision=1, set_canonical=False)
        assert out["became_canonical"] is False
        assert out["canonical_revision"] == 1
        costume = h.rows("costume", tenant_id="t1", costume_id=costume_id)[0]
        assert costume["current_revision"] == 2
        assert costume["canonical_revision"] == 1

    def test_f9_candidate_cannot_be_accepted_twice(self):
        # F9
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        h.accept(costume["costume_id"], cand["candidate_id"], command_id="c3")
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.accept(costume["costume_id"], cand["candidate_id"],
                     command_id="c4", expected_current_revision=1)

    def test_f13_concurrent_accept_one_wins(self):
        # F13：同一个 expected_current_revision 的第二个写者必须 409。
        h = Harness()
        costume = h.create()
        a = h.draft(costume["costume_id"], command_id="c2")
        b = h.draft(costume["costume_id"], command_id="c3",
                    outfit=outfit(pieces=[piece("top", color="#987654")],
                                  layering_order=["top"]))
        h.accept(costume["costume_id"], a["candidate_id"], command_id="c4")
        with raises(409, "VERSION_CONFLICT"):
            h.accept(costume["costume_id"], b["candidate_id"], command_id="c5")
        assert h.count("costume_revision") == 1

    def test_f6_duplicate_content_is_409(self):
        # F6 / C12-12：租户级 content_hash 唯一。
        h = Harness()
        first = h.create()
        cand = h.draft(first["costume_id"])
        h.accept(first["costume_id"], cand["candidate_id"])
        other = h.create(command_id="c4", display_name="另一条同款")
        cand2 = h.draft(other["costume_id"], command_id="c5")
        with raises(409, "DUPLICATE_COSTUME_CONTENT"):
            h.accept(other["costume_id"], cand2["candidate_id"],
                     command_id="c6")

    def test_new_revision_with_new_content_is_not_duplicate(self):
        h = Harness()
        costume_id, _ = h.seeded()
        cand2 = h.draft(costume_id, command_id="c10",
                        expected_current_revision=1,
                        outfit=outfit(pieces=[piece("top", color="#ABCDEF")],
                                      layering_order=["top"]))
        out = h.accept(costume_id, cand2["candidate_id"], command_id="c11",
                       expected_current_revision=1)
        assert out["revision"] == 2

    def test_reason_and_decision_are_recorded_on_pointer(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        h.accept(costume["costume_id"], cand["candidate_id"],
                 decision_kind="human_approved", reason_text="导演确认")
        (ptr,) = h.rows("canonical_pointer", tenant_id="t1",
                        costume_id=costume["costume_id"])
        assert ptr["decision_kind"] == "human_approved"
        assert ptr["reason_text"] == "导演确认"

    def test_reject_moves_candidate_out_of_pending(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        out = h.command(lambda t, c, b: h.service.reject_candidate(
            t, c, costume["costume_id"], cand["candidate_id"], b),
            {"reason_text": "颜色不对"}, command_id="c7")
        assert out["state"] == "REJECTED"
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.accept(costume["costume_id"], cand["candidate_id"],
                     command_id="c8")

    def test_rejecting_twice_is_409(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        fn = lambda t, c, b: h.service.reject_candidate(  # noqa: E731
            t, c, costume["costume_id"], cand["candidate_id"], b)
        h.command(fn, {}, command_id="c7")
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.command(fn, {}, command_id="c8")

    def test_failed_accept_leaves_candidate_pending(self):
        # 失败必须整条回滚：candidate 还是 PENDING，没有 revision、没有事件。
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        with raises(409, "VERSION_CONFLICT"):
            h.accept(costume["costume_id"], cand["candidate_id"],
                     expected_current_revision=7)
        row = h.rows("costume_candidate", tenant_id="t1",
                     candidate_id=cand["candidate_id"])[0]
        assert row["state"] == "PENDING"
        assert h.count("costume_revision") == 0
        assert h.count("outbox") == 0


class TestPolicyAutoGate:
    """§4.2 第 3 步 / F8 / CCA-5 / E24-C2。"""

    def test_f8_e24_c2_identity_check_fail_blocks_policy_auto(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"],
                       evaluation={"identity_invariance_check": "IDENTITY_CHANGED"})
        with raises(422, "IDENTITY_CHECK_REQUIRED_FOR_AUTO"):
            h.accept(costume["costume_id"], cand["candidate_id"],
                     command_id="c30", decision_kind="policy_auto")
        assert h.count("costume_revision") == 0

    def test_identity_check_missing_blocks_policy_auto(self):
        # CCA-5：缺失不是"通过"。
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"], evaluation={})
        with raises(422, "IDENTITY_CHECK_REQUIRED_FOR_AUTO"):
            h.accept(costume["costume_id"], cand["candidate_id"],
                     command_id="c30", decision_kind="policy_auto")

    def test_identity_check_pass_allows_policy_auto(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"],
                       evaluation={"identity_invariance_check": "pass"})
        out = h.accept(costume["costume_id"], cand["candidate_id"],
                       command_id="c30", decision_kind="policy_auto")
        assert out["revision"] == 1

    def test_human_approval_needs_no_identity_check(self):
        # §4.2：这道门只挡 policy_auto。人审可以带任何结论。
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"],
                       evaluation={"identity_invariance_check": "fail"})
        out = h.accept(costume["costume_id"], cand["candidate_id"],
                       command_id="c30", decision_kind="human_approved")
        assert out["revision"] == 1

    def test_auto_accept_with_missing_variants_is_blocked_on_stored_check(self):
        """C12-11：正常路径上不可达——起草已经拒了。这里直接改库造出
        "升级前入库的旧候选"，验证接受时的复查真的读**已存**的
        `variant_check`，而不是重新算一次（重算就永远为空，门就形同虚设）。"""
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        h.conn.execute(
            "UPDATE costume_candidate SET variant_check=? WHERE tenant_id='t1'"
            " AND candidate_id=?",
            (json.dumps({"missing_variants": [{"axis": "damage",
                                               "value": "bloodied"}],
                         "extra_variants": []}), cand["candidate_id"]))
        with raises(422, "VARIANT_COVERAGE_INCOMPLETE"):
            h.accept(costume["costume_id"], cand["candidate_id"],
                     command_id="c30", decision_kind="policy_auto",
                     set_canonical=True)

    def test_stored_missing_variants_do_not_block_human_accept(self):
        # 同一条旧候选，人审接受是可以的——§4.2 只禁止 policy_auto。
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        h.conn.execute(
            "UPDATE costume_candidate SET variant_check=? WHERE tenant_id='t1'"
            " AND candidate_id=?",
            (json.dumps({"missing_variants": [{"axis": "damage",
                                               "value": "bloodied"}],
                         "extra_variants": []}), cand["candidate_id"]))
        out = h.accept(costume["costume_id"], cand["candidate_id"],
                       command_id="c30", decision_kind="human_approved")
        assert out["revision"] == 1


class TestQualityGateRejection:
    """E24-C1 / C12-13：图文匹配失败 → 候选 REJECTED、不写 revision。"""

    def test_e24_c1_image_text_mismatch_rejects_the_candidate(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"],
                       evaluation={"identity_invariance_check": "pass",
                                   "image_text_match": False})
        with raises(422, "QUALITY_GATE_REJECTED"):
            h.accept(costume["costume_id"], cand["candidate_id"],
                     command_id="c30")
        row = h.rows("costume_candidate", tenant_id="t1",
                     candidate_id=cand["candidate_id"])[0]
        # 必须是落库的 REJECTED，不是"抛错然后回滚成 PENDING"
        assert row["state"] == "REJECTED"
        assert h.count("costume_revision") == 0
        assert h.count("outbox") == 0

    def test_mismatch_accepts_the_three_documented_shapes(self):
        for value in (False, {"pass": False}, "fail"):
            h = Harness()
            costume = h.create()
            cand = h.draft(costume["costume_id"],
                           evaluation={"image_text_match": value})
            with raises(422, "QUALITY_GATE_REJECTED"):
                h.accept(costume["costume_id"], cand["candidate_id"],
                         command_id="c30")

    def test_absent_conclusion_is_not_a_failure(self):
        # 缺失不能当成失败：会拒掉每一条没有 QC 的草稿。
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"], evaluation={})
        out = h.accept(costume["costume_id"], cand["candidate_id"],
                       command_id="c30")
        assert out["revision"] == 1

    def test_mismatch_does_not_consume_the_idempotency_key(self):
        # 422 之后同 key 重放不该返回旧结果：命令根本没成功。
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"],
                       evaluation={"image_text_match": False})
        with raises(422, "QUALITY_GATE_REJECTED"):
            h.accept(costume["costume_id"], cand["candidate_id"],
                     command_id="c30")
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.accept(costume["costume_id"], cand["candidate_id"],
                     command_id="c30")


# ================================================================ 指针与状态

class TestCanonicalAndStatus:
    def test_e08_c2_canonical_rollback_moves_pointer_without_new_revision(self):
        # E08-C2
        h = Harness()
        costume_id, _ = h.seeded()
        cand2 = h.draft(costume_id, command_id="c10",
                        expected_current_revision=1,
                        outfit=outfit(pieces=[piece("top", color="#0F0F0F")],
                                      layering_order=["top"]))
        h.accept(costume_id, cand2["candidate_id"], command_id="c11",
                 expected_current_revision=1)
        assert h.count("costume_revision") == 2
        out = h.command(lambda t, c, b: h.service.set_canonical(
            t, c, costume_id, b),
            {"revision": 1, "decision_kind": "human_approved",
             "reason_text": "第 2 集的整洁造型"}, command_id="c12")
        assert out["changed"] is True
        assert out["prior_canonical"] == 2
        assert h.count("costume_revision") == 2   # 没有新 revision
        costume = h.rows("costume", tenant_id="t1", costume_id=costume_id)[0]
        assert costume["canonical_revision"] == 1
        assert costume["current_revision"] == 2   # 头还在 2：canonical ≠ 最新

    def test_superseded_pointer_is_kept_for_audit(self):
        h = Harness()
        costume_id, _ = h.seeded()
        cand2 = h.draft(costume_id, command_id="c10",
                        expected_current_revision=1,
                        outfit=outfit(pieces=[piece("top", color="#0F0F0F")],
                                      layering_order=["top"]))
        h.accept(costume_id, cand2["candidate_id"], command_id="c11",
                 expected_current_revision=1)
        rows = h.rows("canonical_pointer", tenant_id="t1",
                      costume_id=costume_id)
        assert len(rows) == 2
        assert sum(1 for r in rows if r["superseded_at"] is None) == 1

    def test_set_canonical_to_same_revision_is_noop(self):
        h = Harness()
        costume_id, _ = h.seeded()
        out = h.command(lambda t, c, b: h.service.set_canonical(
            t, c, costume_id, b), {"revision": 1}, command_id="c10")
        assert out["changed"] is False
        assert len(h.rows("canonical_pointer", tenant_id="t1",
                          costume_id=costume_id)) == 1

    def test_set_canonical_emits_no_event(self):
        # CCA-4：方案如此。按事件缓存 canonical 的消费方必须回读。
        h = Harness()
        costume_id, _ = h.seeded()
        h.command(lambda t, c, b: h.service.set_canonical(
            t, c, costume_id, b), {"revision": 1}, command_id="c10")
        assert h.count("outbox") == 1   # 只有接受那一条

    def test_set_canonical_to_missing_revision_is_404(self):
        h = Harness()
        costume_id, _ = h.seeded()
        with raises(404, "NOT_FOUND"):
            h.command(lambda t, c, b: h.service.set_canonical(
                t, c, costume_id, b), {"revision": 9}, command_id="c10")

    def test_deprecate_then_tombstone(self):
        h = Harness()
        costume_id, _ = h.seeded()
        out = h.command(lambda t, c, b: h.service.change_status(
            t, c, costume_id, "deprecated", b), {}, command_id="c10")
        assert out["status"] == "deprecated"
        out = h.command(lambda t, c, b: h.service.change_status(
            t, c, costume_id, "tombstoned", b), {}, command_id="c11")
        assert out["status"] == "tombstoned"

    def test_tombstoned_cannot_go_back_to_deprecated(self):
        h = Harness()
        costume_id, _ = h.seeded()
        h.command(lambda t, c, b: h.service.change_status(
            t, c, costume_id, "tombstoned", b), {}, command_id="c10")
        with raises(409, "INVALID_STATUS_TRANSITION"):
            h.command(lambda t, c, b: h.service.change_status(
                t, c, costume_id, "deprecated", b), {}, command_id="c11")

    def test_f10_canonical_on_tombstoned_costume_is_422(self):
        # F10
        h = Harness()
        costume_id, _ = h.seeded()
        h.command(lambda t, c, b: h.service.change_status(
            t, c, costume_id, "tombstoned", b), {}, command_id="c10")
        with raises(422, "COSTUME_TOMBSTONED"):
            h.command(lambda t, c, b: h.service.set_canonical(
                t, c, costume_id, b), {"revision": 1}, command_id="c11")

    def test_generic_wardrobe_can_have_no_canonical_forever(self):
        # CCA-7 / §2.1：背景服装只要求"能用"。
        h = Harness()
        costume = h.create(costume_kind="generic_wardrobe", character_ref=None)
        assert costume["canonical_revision"] is None
        with raises(404, "CANONICAL_NOT_SET"):
            h.service.get_canonical("t1", costume["costume_id"])


class TestCrossEpisode:
    """§8 E02：三集连续。S12 只回答"这一版长什么样"，不回答"这一场穿什么"。"""

    def test_e02_c1_canonical_is_stable_across_episodes(self):
        # 三集读同一个 costume → 同一个 costume_ref。中间补的细节图不改指针。
        h = Harness()
        costume_id, _ = h.seeded()
        cand2 = h.draft(costume_id, command_id="c10",
                        expected_current_revision=1,
                        outfit=outfit(pieces=[piece("top", color="#303030")],
                                      layering_order=["top"]))
        h.accept(costume_id, cand2["candidate_id"], command_id="c11",
                 expected_current_revision=1, set_canonical=False)
        refs = [h.service.get_canonical("t1", costume_id)["costume_ref"]
                for _ in range(3)]
        assert {r["revision"] for r in refs} == {1}
        assert {r["content_hash"] for r in refs} == {refs[0]["content_hash"]}

    def test_e02_c2_new_costume_for_a_later_episode_gets_a_new_id(self):
        # 第 3 集换新服装 = 新 costume_id；第 1、2 集的旧 ID 不变，
        # 也不会去 S11 产生新角色 revision（S12 没有第二个写者）。
        h = Harness()
        old_id, _ = h.seeded()
        old_row = dict(h.rows("costume", tenant_id="t1", costume_id=old_id)[0])
        old_events = h.count("outbox")
        new = h.create(command_id="c70", display_name="林岚·夜行装")
        assert new["costume_id"] != old_id
        # 旧服装整行不变（含它的 revision 数、canonical 指针）
        assert dict(h.rows("costume", tenant_id="t1", costume_id=old_id)[0]) \
            == old_row
        # 同一角色：新服装只登记了一个引用，没有让 S11 产生新 revision，
        # 也没有替 S11 记"这个角色换过衣服"。
        assert new["character_ref"] == {"service_id": "S11",
                                        "id": "chr_linlan"}
        assert h.count("outbox") == old_events

    def test_e02_c3_same_revision_answers_the_same_variant_every_time(self):
        # 第 6、7 场用同一 binding revision 问 torn_shoulder，结果逐字节相同。
        # 持续性归 S16；S12 只要不随调用次数变化。
        h = Harness()
        costume_id, _ = h.seeded()
        a = h.service.get_revision("t1", costume_id, 1,
                                   variant="damage:torn_shoulder")
        b = h.service.get_revision("t1", costume_id, 1,
                                   variant="damage:torn_shoulder")
        assert a == b
        assert a["variant"]["damage"] == "torn_shoulder"

    def test_e02_c4_qc_conclusion_is_recorded_but_identity_is_untouched(self):
        # S33 报 IDENTITY_CHANGED 时，S12 记下结论，不改 character_ref，
        # 也不升级成"这个角色换人了"。
        h = Harness()
        costume = h.create()
        h.draft(costume["costume_id"],
                evaluation={"identity_invariance_check": "IDENTITY_CHANGED"})
        (cand,) = h.rows("costume_candidate", tenant_id="t1")
        assert json.loads(cand["evaluation"])["identity_invariance_check"] == \
            "IDENTITY_CHANGED"
        costume_row = h.rows("costume", tenant_id="t1",
                             costume_id=costume["costume_id"])[0]
        assert json.loads(costume_row["character_ref"])["id"] == "chr_linlan"
        assert h.count("costume_revision") == 0


# ================================================================ 读路径

class TestReadPaths:
    def test_variant_selector_returns_only_that_artifact(self):
        h = Harness()
        costume_id, _ = h.seeded()
        out = h.service.get_revision("t1", costume_id, 1,
                                     variant="damage:torn_shoulder")
        assert out["variant"] == {"damage": "torn_shoulder",
                                  "wetness": "dry", "makeup": "natural"}
        assert out["artifacts"]["damage:torn_shoulder"]["artifact_id"] == \
            "art_damage_torn"
        assert "wetness:damp" not in out["artifacts"]

    def test_unspecified_axes_echo_the_resolved_default(self):
        # C12-10：回显解析结果，让调用方知道自己拿到的是什么。
        h = Harness()
        costume_id, _ = h.seeded()
        out = h.service.get_revision("t1", costume_id, 1)
        assert out["variant"] == variant_default(AXES)

    def test_multi_axis_selector(self):
        h = Harness()
        costume_id, _ = h.seeded()
        out = h.service.get_revision(
            "t1", costume_id, 1,
            variant="damage:torn_shoulder,wetness:damp")
        assert out["variant"]["damage"] == "torn_shoulder"
        assert out["variant"]["wetness"] == "damp"
        assert set(out["artifacts"]) == {"damage:torn_shoulder",
                                         "wetness:damp", "makeup:natural"}

    def test_f5_missing_variant_is_422_with_available_values(self):
        # F5 / §4.3 硬约束：**不静默退回 none**。
        h = Harness()
        costume_id, _ = h.seeded()
        with pytest.raises(ServiceError) as info:
            h.service.get_revision("t1", costume_id, 1,
                                   variant="damage:bloodied")
        assert (info.value.status, info.value.code) == (422,
                                                        "VARIANT_NOT_AVAILABLE")
        assert info.value.details == {"axis": "damage", "value": "bloodied",
                                      "available": ["none", "torn_shoulder"]}

    def test_unknown_axis_is_also_422(self):
        h = Harness()
        costume_id, _ = h.seeded()
        with pytest.raises(ServiceError) as info:
            h.service.get_revision("t1", costume_id, 1,
                                   variant="weather_stain:muddy")
        assert info.value.code == "VARIANT_NOT_AVAILABLE"
        assert info.value.details["available"] == []

    def test_bad_selector_syntax_is_malformed(self):
        h = Harness()
        costume_id, _ = h.seeded()
        with raises(400, "MALFORMED_REQUEST"):
            h.service.get_revision("t1", costume_id, 1, variant="damage")

    def test_revision_marks_canonical(self):
        h = Harness()
        costume_id, _ = h.seeded()
        out = h.service.get_revision("t1", costume_id, 1)
        assert out["is_canonical"] is True
        assert out["canonical_revision"] == 1
        assert out["costume_ref"]["service_id"] == "S12"
        assert out["character_ref"] == {"service_id": "S11",
                                        "id": "chr_linlan"}
        assert out["rights_recheck_required"] is True

    def test_read_nonexistent_revision_is_404(self):
        h = Harness()
        costume_id, _ = h.seeded()
        with raises(404, "NOT_FOUND"):
            h.service.get_revision("t1", costume_id, 9)

    def test_f12_e28_c1_cross_tenant_read_is_404(self):
        # F12 / E28-C1
        h = Harness()
        costume_id, _ = h.seeded()
        with raises(404, "NOT_FOUND"):
            h.service.get_revision("t2", costume_id, 1)
        with raises(404, "NOT_FOUND"):
            h.service.get_costume("t2", costume_id)

    def test_cross_tenant_read_leaks_no_foreign_identifiers(self):
        h = Harness()
        costume_id, _ = h.seeded()
        with pytest.raises(ServiceError) as info:
            h.service.get_costume("t2", costume_id)
        blob = json.dumps(info.value.details) + str(info.value)
        assert "chr_linlan" not in blob
        assert "林岚" not in blob

    def test_e28_c3_cross_tenant_variant_query_is_404(self):
        # E28-C3
        h = Harness()
        costume_id, _ = h.seeded()
        with raises(404, "NOT_FOUND"):
            h.service.get_revision("t2", costume_id, 1,
                                   variant="damage:torn_shoulder")

    def test_lookup_is_exact_name_match(self):
        h = Harness()
        costume = h.create()
        out = h.service.lookup("t1", series_scope="ser_1",
                               alias="林岚·红裙")
        assert [c["costume_id"] for c in out["items"]] == \
            [costume["costume_id"]]
        with raises(404, "NOT_FOUND"):
            h.service.lookup("t1", series_scope="ser_1", alias="林岚")

    def test_lookup_returns_all_kinds_for_the_same_name(self):
        # C12-9：唯一键含 costume_kind，同名不同类是两条记录；不发明"取第一个"。
        h = Harness()
        a = h.create()
        b = h.create(command_id="c2", costume_kind="uniform")
        out = h.service.lookup("t1", series_scope="ser_1",
                               alias="林岚·红裙")
        assert {c["costume_id"] for c in out["items"]} == {
            a["costume_id"], b["costume_id"]}
        kinds = [c["costume_kind"] for c in out["items"]]
        assert kinds == sorted(kinds)

    def test_lookup_is_tenant_scoped(self):
        h = Harness()
        h.create(tenant_id="t1")
        with raises(404, "NOT_FOUND"):
            h.service.lookup("t2", series_scope="ser_1", alias="林岚·红裙")

    def test_revision_references_attach_to_an_immutable_revision(self):
        h = Harness()
        costume_id, _ = h.seeded()
        out = h.command(lambda t, c, b: h.service.add_revision_references(
            t, c, costume_id, b), {
            "revision": 1,
            "references": [{"ref_role": "worn_with", "target_service": "S12",
                            "target_id": "cos_other", "target_revision": 1}],
        }, command_id="c40")
        assert out["created"] == 1
        # 重复登记无副作用
        again = h.command(lambda t, c, b: h.service.add_revision_references(
            t, c, costume_id, b), {
            "revision": 1,
            "references": [{"ref_role": "worn_with", "target_service": "S12",
                            "target_id": "cos_other", "target_revision": 1}],
        }, command_id="c41")
        assert again["created"] == 0
        assert h.count("revision_reference") == 1

    def test_e30_c1_rights_revocation_does_not_hide_the_revision(self):
        # E30-C1：权利撤销是 S02 的事，S12 只把"你得自己复查"挂在响应上。
        # 若这里改成 403 或过滤掉 revision，S12 就成了权利判官（§9 第 10 条）。
        h = Harness()
        costume_id, _ = h.seeded()
        out = h.service.get_revision("t1", costume_id, 1)
        assert out["rights_recheck_required"] is True
        assert out["costume_ref"]["revision"] == 1
        # 只存指针，不存判决：库里没有 rights 状态列可被撤
        cols = {r["name"] for r in h.conn.execute(
            "PRAGMA table_info(costume_revision)").fetchall()}
        assert not ({c for c in cols if "right" in c} - {"rights_ref"})

    def test_e30_c2_rights_version_change_does_not_rewrite_the_revision(self):
        # E30-C2：换权利版本 = 新 revision 带新 rights_ref，不是原地改。
        h = Harness()
        costume_id, _ = h.seeded()
        old = h.rows("costume_revision", tenant_id="t1",
                     costume_id=costume_id, revision=1)[0]
        cand2 = h.draft(costume_id, command_id="c10",
                        expected_current_revision=1,
                        rights_ref={"service_id": "S02", "id": "rights_2"},
                        outfit=outfit(pieces=[piece("top", color="#101010")],
                                      layering_order=["top"]))
        h.accept(costume_id, cand2["candidate_id"], command_id="c11",
                 expected_current_revision=1)
        assert json.loads(old["rights_ref"]) == {"service_id": "S02",
                                                 "id": "rights_1"}
        new = h.rows("costume_revision", tenant_id="t1",
                     costume_id=costume_id, revision=2)[0]
        assert json.loads(new["rights_ref"])["id"] == "rights_2"

    def test_e26_c1_repair_creates_a_new_revision_and_keeps_the_old_one(self):
        # E26-C1：S39 提议替换某变体 artifact → 新 revision，旧的不动。
        h = Harness()
        costume_id, _ = h.seeded()
        before = dict(h.rows("costume_revision", tenant_id="t1",
                             costume_id=costume_id, revision=1)[0])
        m = manifest()
        m["damage"]["torn_shoulder"] = {"artifact_id": "art_damage_torn",
                                        "content_hash": HASH_C}
        h.artifacts["art_damage_torn"] = artifact("art_damage_torn", HASH_C)
        cand2 = h.draft(costume_id, command_id="c10",
                        expected_current_revision=1, variant_manifest=m,
                        outfit=outfit(pieces=[piece("top", color="#232323")],
                                      layering_order=["top"]))
        out = h.accept(costume_id, cand2["candidate_id"], command_id="c11",
                       expected_current_revision=1)
        assert out["revision"] == 2
        after = dict(h.rows("costume_revision", tenant_id="t1",
                            costume_id=costume_id, revision=1)[0])
        assert before == after   # 旧 revision 一个字节都没动

    def test_e25_c1_repair_after_rejection_gets_a_new_candidate_id(self):
        # E25-C1
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"], command_id="c2")
        h.command(lambda t, c, b: h.service.reject_candidate(
            t, c, costume["costume_id"], cand["candidate_id"], b), {},
            command_id="c3")
        cand2 = h.draft(costume["costume_id"], command_id="c4")
        assert cand2["candidate_id"] != cand["candidate_id"]

    def test_revision_reference_must_name_an_existing_revision(self):
        h = Harness()
        costume_id, _ = h.seeded()
        with raises(404, "NOT_FOUND"):
            h.command(lambda t, c, b: h.service.add_revision_references(
                t, c, costume_id, b), {
                "revision": 9,
                "references": [{"ref_role": "worn_with",
                                "target_service": "S12",
                                "target_id": "cos_x",
                                "target_revision": 1}],
            }, command_id="c40")


# ================================================================ 事件消费

class TestEventConsumption:
    def _envelope(self, event_type, data, event_id="evt_1", tenant="t1"):
        return {"id": event_id, "type": event_type, "tenant_id": tenant,
                "data": data}

    def test_prop_revision_event_marks_the_ref_verified(self):
        h = Harness()
        costume_id, _ = h.seeded()
        out = h.service.consume(self._envelope(
            "asset.prop.revision.accepted",
            {"prop_ref": {"service_id": "S14", "id": "prp_necklace",
                          "revision": 1, "content_hash": HASH_A}}))
        assert out["verified"] == 1
        (ref,) = h.rows("costume_unverified_ref", tenant_id="t1",
                        costume_id=costume_id, revision=1)
        assert ref["verified_at"] is not None
        assert ref["verified_by"] == "S14"

    def test_verification_does_not_touch_the_revision(self):
        # C12-14 / CCA-3：只做本地标记，不改 revision、不发事件。
        h = Harness()
        costume_id, _ = h.seeded()
        before = json.loads(h.rows("costume_revision", tenant_id="t1",
                                   costume_id=costume_id, revision=1)[0]
                            ["payload"])
        events_before = h.count("outbox")
        h.service.consume(self._envelope(
            "asset.prop.revision.accepted",
            {"prop_ref": {"service_id": "S14", "id": "prp_necklace",
                          "revision": 1, "content_hash": HASH_A}}))
        after = json.loads(h.rows("costume_revision", tenant_id="t1",
                                  costume_id=costume_id, revision=1)[0]
                           ["payload"])
        assert before == after
        assert h.count("outbox") == events_before

    def test_other_revision_does_not_verify_a_pinned_ref(self):
        # 引用了第 1 版，S14 出了第 2 版 —— 仍然悬空。
        h = Harness()
        h.seeded()
        out = h.service.consume(self._envelope(
            "asset.prop.revision.accepted",
            {"prop_ref": {"service_id": "S14", "id": "prp_necklace",
                          "revision": 2, "content_hash": HASH_B}}))
        assert out["verified"] == 0

    def test_duplicate_delivery_is_deduped(self):
        # 乱序/重复投递：至少一次语义，第二次是 duplicate 而不是再标一次。
        h = Harness()
        h.seeded()
        env = self._envelope(
            "asset.prop.revision.accepted",
            {"prop_ref": {"service_id": "S14", "id": "prp_necklace",
                          "revision": 1, "content_hash": HASH_A}})
        first = h.service.consume(env)
        second = h.service.consume(env)
        assert first["consumed"] is True
        assert second == {"consumed": False, "reason": "duplicate"}

    def test_artifact_archived_is_audited_and_changes_nothing(self):
        h = Harness()
        costume_id, _ = h.seeded()
        revisions_before = h.count("costume_revision")
        out = h.service.consume(self._envelope(
            "artifact.archived",
            {"artifact_ref": {"service_id": "S28", "id": "art_damage_none"}}))
        assert out["consumed"] is True
        assert out["changed"] == 0
        assert h.count("costume_revision") == revisions_before

    def test_unknown_event_is_not_an_error(self):
        # 不认识的事件不能回 4xx：上游会永远重试。
        h = Harness()
        out = h.service.consume({"id": "evt_9", "type": "asset.prop.archived",
                                 "tenant_id": "t1", "data": {}})
        assert out["consumed"] is False
        assert out["reason"] == "unrouted"
        assert h.count("inbox") == 0

    def test_event_without_tenant_is_rejected(self):
        h = Harness()
        with raises(422, "INVALID_CONTEXT"):
            h.service.consume({"id": "evt_1",
                               "type": "asset.prop.revision.accepted",
                               "data": {}})

    def test_s11_events_are_not_consumed(self):
        # §5.3：角色换人是 S10 的 binding 要重指，不是 S12 自动跟着改。
        h = Harness()
        out = h.service.consume({"id": "evt_1",
                                 "type": "asset.character.revision.accepted",
                                 "tenant_id": "t1", "data": {}})
        assert out["consumed"] is False


# ================================================================ 崩溃恢复

class TestCrashRecovery:
    def test_material_table_matches_payload(self):
        # §2.4：派生表必须与 payload.outfit 一致（可重建）。
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        h.accept(costume["costume_id"], cand["candidate_id"])
        payload = json.loads(h.rows("costume_revision", tenant_id="t1",
                                    costume_id=costume["costume_id"],
                                    revision=1)[0]["payload"])
        derived = {r["piece_id"]: r for r in h.rows(
            "costume_material_ref", tenant_id="t1",
            costume_id=costume["costume_id"], revision=1)}
        assert set(derived) == {p["piece_id"] for p in payload["outfit"]["pieces"]}
        for p in payload["outfit"]["pieces"]:
            assert derived[p["piece_id"]]["material"] == p["material"]

    def test_sheen_absent_is_null_not_guessed(self):
        # C12-15：缺省写 NULL，不猜一个值。
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        h.accept(costume["costume_id"], cand["candidate_id"])
        rows = h.rows("costume_material_ref", tenant_id="t1",
                      costume_id=costume["costume_id"], revision=1)
        assert all(r["sheen"] is None for r in rows)

    def test_sheen_is_preserved_when_given(self):
        h = Harness()
        costume = h.create()
        bad = outfit()
        bad["pieces"][0]["sheen"] = "satin"
        cand = h.draft(costume["costume_id"], outfit=bad)
        h.accept(costume["costume_id"], cand["candidate_id"])
        (row,) = h.rows("costume_material_ref", tenant_id="t1",
                        costume_id=costume["costume_id"], revision=1,
                        piece_id="top")
        assert row["sheen"] == "satin"

    def test_replay_writes_no_second_revision_or_event(self):
        h = Harness()
        costume = h.create()
        cand = h.draft(costume["costume_id"])
        first = h.accept(costume["costume_id"], cand["candidate_id"],
                         command_id="c3")
        again = h.accept(costume["costume_id"], cand["candidate_id"],
                         command_id="c3")
        assert again["replayed"] is True
        assert again["revision"] == first["revision"]
        assert h.count("costume_revision") == 1
        assert h.count("outbox") == 1

    def test_f15_outbox_row_survives_a_restart_unpublished(self, tmp_path):
        # F15：事件在**同一事务**里落库，进程重启后它还在，等补发。
        # 这里用文件库真的关掉连接再打开，证明不是内存里的假象。
        path = str(tmp_path / "s12_restart.db")
        conn = sqlite3.connect(path, check_same_thread=False,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        h = Harness(conn=conn)
        costume_id, _ = h.seeded(command_id="c1")
        conn.close()

        conn2 = sqlite3.connect(path, check_same_thread=False,
                                isolation_level=None)
        conn2.row_factory = sqlite3.Row
        pending = conn2.execute(
            "SELECT aggregate_id, sequence, published_at FROM outbox"
            " WHERE published_at IS NULL").fetchall()
        assert len(pending) == 1
        assert pending[0]["aggregate_id"] == costume_id
        assert pending[0]["sequence"] == 1
        # 重启后仍可读回那一版
        svc2 = CostumeAssetsService(
            conn2, artifact_source=lambda t, a: artifact(a, HASH_A))
        assert svc2.get_revision("t1", costume_id, 1)["costume_ref"][
            "revision"] == 1
        conn2.close()

    def test_replay_does_not_reread_s28(self):
        h = Harness()
        costume = h.create()
        h.draft(costume["costume_id"])
        calls = h.calls["artifact"]
        h.draft(costume["costume_id"], command_id="c2")
        assert h.calls["artifact"] == calls

    def test_old_payload_stays_readable_after_new_axis_vocabulary(self):
        # §2.2 / §9 反模式 12：`variant_axes` 随 revision 存，旧版可解析。
        h = Harness()
        costume_id, _ = h.seeded()
        cand2 = h.draft(costume_id, command_id="c10",
                        expected_current_revision=1,
                        variant_axes={"damage": ["none"], "wetness": ["dry"]},
                        variant_manifest={"damage": {
                            "none": {"artifact_id": "art_damage_none",
                                     "content_hash": HASH_A}},
                            "wetness": {
                                "dry": {"artifact_id": "art_wet_dry",
                                        "content_hash": HASH_A}}},
                        outfit=outfit(pieces=[piece("top", color="#778899")],
                                      layering_order=["top"]))
        h.accept(costume_id, cand2["candidate_id"], command_id="c11",
                 expected_current_revision=1)
        v1 = h.service.get_revision("t1", costume_id, 1)
        v2 = h.service.get_revision("t1", costume_id, 2)
        assert set(v1["variant_axes"]) == {"damage", "wetness", "makeup"}
        assert set(v2["variant_axes"]) == {"damage", "wetness"}
        # 问 v2 要 makeup 必须是 422，而不是"退回 v1 的 makeup"
        with raises(422, "VARIANT_NOT_AVAILABLE"):
            h.service.get_revision("t1", costume_id, 2,
                                   variant="makeup:natural")


# ================================================================ HTTP

def _http_client(monkeypatch, tmp_path, **over):
    monkeypatch.setenv("S12_DB", str(tmp_path / "s12.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s12_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s12_api_app"] = module
    h = Harness(conn=module._conn, **over)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h


def _headers(command_id="c1", tenant="t1", **extra):
    out = {"Idempotency-Key": command_id, "X-Tenant-Id": tenant}
    out.update(extra)
    return out


class TestHttpLayer:
    def test_create_and_read(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/costumes", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚·红裙",
            "costume_kind": "character_outfit",
            "character_ref": {"service_id": "S11", "id": "chr_linlan"}},
            headers=_headers())
        assert r.status_code == 201, r.text
        costume_id = r.json()["costume_id"]
        got = client.get(f"/costumes/{costume_id}", headers=_headers())
        assert got.status_code == 200
        assert got.json()["costume"]["display_name"] == "林岚·红裙"
        assert got.json()["canonical"] is None
        assert got.json()["rights_recheck_required"] is True

    def test_missing_idempotency_key_is_400(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/costumes", json={"project_id": "prj_1"},
                        headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 400
        assert r.json()["code"] == "MALFORMED_REQUEST"

    def test_missing_tenant_header_is_400(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/costumes", json={"project_id": "prj_1"},
                        headers={"Idempotency-Key": "c1"})
        assert r.status_code == 400
        assert r.json()["code"] == "MALFORMED_REQUEST"
        r = client.get("/costumes/cos_x", headers={})
        assert r.status_code == 400

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/costumes", json={
            "tenant_id": "t2", "project_id": "prj_1",
            "series_scope": "ser_1", "display_name": "林岚·红裙",
            "costume_kind": "character_outfit"}, headers=_headers())
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_SCOPE_DENIED"

    def test_command_id_must_match_header(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/costumes", json={
            "command_id": "other", "project_id": "prj_1",
            "series_scope": "ser_1", "display_name": "林岚·红裙",
            "costume_kind": "character_outfit"}, headers=_headers())
        assert r.status_code == 400

    def test_merge_route_does_not_exist(self, monkeypatch, tmp_path):
        # C12-8：没有路由是最强的边界声明。`:merge` 挂在 `/costumes/{id}`
        # 这条**同名**路径上，所以 FastAPI 给出 405（路径在、方法不对），
        # 而不是 404 —— 无论哪个，都不是一个能合并的端点。
        client, h = _http_client(monkeypatch, tmp_path)
        costume = h.create()
        r = client.post(f"/costumes/{costume['costume_id']}:merge",
                        json={}, headers=_headers())
        assert r.status_code in (404, 405), r.status_code
        # 不是服务的错误形状：没有 code 字段，说明请求没进到内核。
        assert "code" not in r.json()

    def test_full_flow_over_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        costume_id = client.post("/costumes", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚·红裙",
            "costume_kind": "character_outfit"}, headers=_headers())
        assert costume_id.status_code == 201
        costume_id = costume_id.json()["costume_id"]

        cand = client.post(f"/costumes/{costume_id}/candidates",
                           json=candidate_body(),
                           headers=_headers(command_id="c2"))
        assert cand.status_code == 200, cand.text
        candidate_id = cand.json()["candidate_id"]

        acc = client.post(
            f"/costumes/{costume_id}/candidates/{candidate_id}:accept",
            json={"expected_current_revision": 0, "set_canonical": True,
                  "decision_kind": "human_approved"},
            headers=_headers(command_id="c3"))
        assert acc.status_code == 200, acc.text
        assert acc.json()["revision"] == 1

        rev = client.get(f"/costumes/{costume_id}/revisions/1",
                         params={"variant": "damage:torn_shoulder"},
                         headers=_headers())
        assert rev.status_code == 200
        assert rev.json()["artifacts"]["damage:torn_shoulder"]["artifact_id"] \
            == "art_damage_torn"

        can = client.get(f"/costumes/{costume_id}/canonical", headers=_headers())
        assert can.status_code == 200
        assert can.json()["canonical"]["revision"] == 1

        lookup = client.get("/costumes:lookup",
                            params={"series_scope": "ser_1",
                                    "alias": "林岚·红裙"},
                            headers=_headers())
        assert lookup.status_code == 200
        assert lookup.json()["items"][0]["costume_id"] == costume_id

    def test_variant_not_available_over_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        costume_id, _ = h.seeded()
        r = client.get(f"/costumes/{costume_id}/revisions/1",
                       params={"variant": "damage:bloodied"},
                       headers=_headers())
        assert r.status_code == 422
        body = r.json()
        assert body["code"] == "VARIANT_NOT_AVAILABLE"
        assert body["retryable"] is False
        assert body["details"]["available"] == ["none", "torn_shoulder"]

    def test_cross_tenant_over_http_is_404(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        costume_id, _ = h.seeded()
        r = client.get(f"/costumes/{costume_id}/revisions/1",
                       headers=_headers(tenant="t2"))
        assert r.status_code == 404
        assert r.json()["code"] == "NOT_FOUND"

    def test_idempotent_replay_over_http(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        body = {"project_id": "prj_1", "series_scope": "ser_1",
                "display_name": "林岚·红裙",
                "costume_kind": "character_outfit"}
        first = client.post("/costumes", json=body, headers=_headers())
        again = client.post("/costumes", json=body, headers=_headers())
        assert first.json()["replayed"] is False
        assert again.json()["replayed"] is True
        assert again.json()["costume_id"] == first.json()["costume_id"]

    def test_same_key_different_body_over_http_is_409(self, monkeypatch,
                                                      tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        body = {"project_id": "prj_1", "series_scope": "ser_1",
                "display_name": "林岚·红裙",
                "costume_kind": "character_outfit"}
        client.post("/costumes", json=body, headers=_headers())
        r = client.post("/costumes", json={**body, "display_name": "林岚·白裙"},
                        headers=_headers())
        assert r.status_code == 409
        assert r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_upstream_down_over_http_is_503(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path, artifacts={
            "art_damage_none": ConnectionError("timed out")})
        costume = h.create()
        r = client.post(f"/costumes/{costume['costume_id']}/candidates",
                        json=candidate_body(), headers=_headers(command_id="c2"))
        assert r.status_code == 503
        assert r.json()["code"] == "DEPENDENCY_UNAVAILABLE"
        assert r.json()["retryable"] is True

    def test_readiness_reports_contract_gaps(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/health/readiness")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        ids = {g["id"] for g in body["contract_gaps"]}
        assert {"C12-1", "C12-17", "CCA-1"} <= ids
        assert ids == {g["id"] for g in CONTRACT_GAPS}


# ================================================================ 验收映射

class TestAcceptanceMap:
    """验收表不是注释，是断言。

    §8 的 16 个 `Exx-Cx` 与 §4.4 的 15 条 `Fxx` 都要有一个测试名认领；
    测试名里的 ID 也必须真的存在于方案里。任何一边漏了，这组就红——
    否则「照着验收表写测试」只是一句话。
    """

    PLAN = (Path(__file__).resolve().parents[3]
            / "plans" / "S12-costume-assets.md")

    @staticmethod
    def _plan_ids() -> tuple[set[str], set[str]]:
        import re
        lines = TestAcceptanceMap.PLAN.read_text(encoding="utf-8").splitlines()
        # §8 从 "### E02 · 三集连续" 到 "## 9. 反模式"；§4.4 到 "---" 为止。
        i8 = next(n for n, line in enumerate(lines)
                  if line.startswith("### E02"))
        i9 = next(n for n, line in enumerate(lines)
                  if line.startswith("## 9."))
        e_ids = set(re.findall(r"E\d{2}-C\d", "\n".join(lines[i8:i9])))
        i44 = next(n for n, line in enumerate(lines)
                   if line.startswith("### 4.4"))
        j44 = next(n for n in range(i44, len(lines))
                   if lines[n].strip() == "---")
        f_ids = set(re.findall(r"^\| (F\d{1,2}) ", "\n".join(lines[i44:j44]),
                               re.MULTILINE))
        return e_ids, f_ids

    def _claimed(self) -> set[str]:
        import inspect
        import re
        found = set()
        for klass in globals().values():
            if not (inspect.isclass(klass) and klass.__module__ == __name__):
                continue
            for name in vars(klass):
                if not name.startswith("test_"):
                    continue
                # 测试名是小写 snake_case；ID 要大写才能和方案里的写法对齐。
                for e, c in re.findall(r"(?:^|_)(e\d{2})_c(\d)(?:_|$)", name):
                    found.add(f"{e.upper()}-C{c}")
                for f in re.findall(r"(?:^|_)f(\d{1,2})(?:_|$)", name):
                    found.add(f"F{f}")
        return found

    def test_plan_ids_are_discovered(self):
        # 先证明探针看得见方案：分母不是 0 才谈得上覆盖率。
        e_ids, f_ids = self._plan_ids()
        assert len(e_ids) == 16, sorted(e_ids)
        assert len(f_ids) == 15, sorted(f_ids)
        assert "E02-C1" in e_ids and "E26-C2" in e_ids
        assert "F1" in f_ids and "F15" in f_ids

    def test_every_acceptance_row_has_a_named_test(self):
        e_ids, f_ids = self._plan_ids()
        claimed = self._claimed()
        missing = sorted((e_ids | f_ids) - claimed)
        assert missing == [], missing

    def test_no_test_names_an_invented_id(self):
        e_ids, f_ids = self._plan_ids()
        invented = sorted(self._claimed() - (e_ids | f_ids))
        assert invented == [], invented


# ================================================================ 契约缺口

class TestContractGaps:
    def test_entries_have_the_required_shape(self):
        for gap in CONTRACT_GAPS:
            assert set(gap) == {"id", "kind", "summary", "handling"}
            assert gap["kind"] in {"conflict", "needs_owner", "gap"}
            assert gap["summary"] and gap["handling"]

    def test_ids_are_unique_and_ordered(self):
        ids = [g["id"] for g in CONTRACT_GAPS]
        assert len(ids) == len(set(ids))
        assert ids[:19] == [f"C12-{i}" for i in range(1, 20)]
        assert ids[19:] == [f"CCA-{i}" for i in range(1, 8)]

    def test_no_owner_decision_is_filled_in(self):
        # AGENTS.md：冲突要报告，不要自行选边。每条 handling 都要指出
        # "按方案哪一节实现"或"待 owner 定"，而不是宣布裁决。
        for gap in CONTRACT_GAPS:
            if gap["kind"] == "needs_owner":
                assert ("待" in gap["handling"] or "未定" in gap["handling"]
                        or "需" in gap["handling"]), gap["id"]

    def test_merge_route_is_not_implemented(self):
        # C12-8：体与语义都没定义，按 §9 不猜。
        assert not hasattr(CostumeAssetsService, "merge_costumes")

    def test_open_questions_of_the_event_contract_are_registered(self):
        # 事件契约里 6 条 x-needs-owner / CCA-1…CCA-7 不能默默丢掉。
        kinds = {g["id"]: g["kind"] for g in CONTRACT_GAPS}
        assert kinds["CCA-1"] == "conflict"
        assert kinds["CCA-7"] == "conflict"
