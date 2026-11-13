"""S11 单测。用例按行为分组，注释里引 `plans/S11-character-assets.md` §8
自己的编号（`E02-B1` 这种形如 `<验收项>-B<序号>`，**不是** `T11-xx`——
方案里没有 `T11-` 前缀）与 §4.6 的 F1–F15。

上游全部是 stub，**按真实的上游契约写**：

- S28 `GET /artifacts/{artifact_id}` 返回 `artifact_ingest.service.get_artifact`
  那个形状；这里只用到 `artifact_id` 与 `content_hash` 两项。
- S01 **没有 stub**：`service.yaml` 的 `depends_on` 里有它，但方案全文没有一处
  调用（C11-18），所以也没有它的适配器。测试里 `TestNoSecondAuthority` 断言
  内核上不存在任何会调它的属性。
- QC 的结论不是任何服务的真实响应（C11-11），所以用**命令内联的
  `quality_gate`**，与内核支持的唯一来源一致。

结构性断言用"库里查不到"而不是"响应里没有"：这个服务最严重的错误是变成
第二个正史权威（剧情状态、服装、声音），而响应体里少个字段不能证明什么，
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

from character_assets import (
    ALL_VIEWS,
    CANDIDATE_STATES,
    CharacterAssetsService,
    ServiceError,
    classify_drift,
    content_hash,
    identity_feature_hash,
)
from character_assets.schema import CONTRACT_GAPS, SCHEMA


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


# ------------------------------------------------------------------ 上游形状

def artifact(artifact_id="art_front", content_hash=HASH_A, **over):
    """S28 `get_artifact` 的字段集（只取本服务实际读的两项）。"""
    base = {"artifact_id": artifact_id, "content_hash": content_hash}
    base.update(over)
    return base


def identity(**over):
    """§2.3 的身份特征。分档值，不是自由文本。"""
    base = {
        "face_shape": "oval",
        "jaw": "soft",
        "eye_shape": "almond",
        "eye_distance_ratio": 0.45,
        "brow": "straight",
        "nose_bridge": "medium",
        "lip_fullness": "medium",
        "skin_tone_band": "3",
        "hair": {"length": "long", "texture": "straight",
                 "hairline": "straight"},
        "age_band": "20s",
        "build": "slim",
        "height_cm_band": "165-170",
        "distinguishing_marks": [
            {"kind": "mole", "location": "left_cheek", "size_mm_band": "1-2"}],
    }
    base.update(over)
    return base


# 每个 art_* 在 stub S28 里只有一个真实 hash。`ref()` 默认取它，
# 否则用例会撞上 ARTIFACT_HASH_MISMATCH 而不是它想测的东西。
ARTIFACT_HASH = {
    "art_front": HASH_A,
    "art_side": HASH_B,
    "art_back": HASH_B,
    "art_34": HASH_B,
}


def ref(name, content_hash=None):
    # 未知 id（如 art_ghost）用 HASH_A：它本来就不在 stub 里，用例要测的是
    # "S28 说没有"，hash 是多少无关。
    return {"artifact_id": name,
            "content_hash": content_hash or ARTIFACT_HASH.get(name, HASH_A)}


def views(**over):
    base = {"front": ref("art_front")}
    base.update(over)
    return base


def full_views():
    return {
        "front": ref("art_front"),
        "side_left": ref("art_side"),
        "back": ref("art_back"),
        "three_quarter": ref("art_34"),
    }


def body_profile(**over):
    base = {"height_cm_band": "165-170", "build": "slim", "age_band": "20s"}
    base.update(over)
    return base


def candidate_body(**over):
    base = {
        "expected_current_revision": 0,
        "source_kind": "generated",
        "identity_features": identity(),
        "body_profile": body_profile(),
        "view_manifest": full_views(),
        "rights_ref": {"rights_owner": "S02", "rights_id": "rights_1",
                       "rights_revision": 1, "rights_hash": HASH_A},
        "quality_gate_ref": {"id": "qg_1", "revision": 1},
        "quality_gate": {"identity_consistency": "pass"},
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
            "art_front": artifact("art_front", HASH_A),
            "art_side": artifact("art_side", HASH_B),
            "art_back": artifact("art_back", HASH_B),
            "art_34": artifact("art_34", HASH_B),
        })
        self.calls = {"artifact": 0}
        self.service = CharacterAssetsService(
            self.conn,
            artifact_source=self._artifact,
            quality_gate_source=over.get("quality_gate_source"),
            max_pending_candidates=over.get("max_pending_candidates", 64),
        )

    def _artifact(self, tenant_id, artifact_id):
        self.calls["artifact"] += 1
        value = self.artifacts.get(artifact_id)
        if isinstance(value, Exception):
            raise value
        # 未知 id → S28 的 404（STUB 的真实形状：不是 KeyError）。
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
                "display_name": "林岚", "identity_kind": "fictional"}
        body.update(over)
        return self.command(
            lambda t, c, b: self.service.create_character(t, c, b), body,
            tenant_id, command_id)

    def head(self, character_id, tenant_id="t1"):
        row = self.conn.execute(
            "SELECT current_revision FROM character WHERE tenant_id=?"
            " AND character_id=?", (tenant_id, character_id)).fetchone()
        return row[0] if row else 0

    def draft(self, character_id, tenant_id="t1", command_id="c2", **over):
        body = candidate_body()
        body["expected_current_revision"] = self.head(character_id, tenant_id)
        body.update(over)
        return self.command(
            lambda t, c, b: self.service.draft_candidate(
                t, c, character_id, b), body, tenant_id, command_id)

    def accept(self, character_id, candidate_id, tenant_id="t1",
               command_id="c3", **over):
        body = {"expected_current_revision": self.head(character_id,
                                                       tenant_id),
                "set_canonical": True, "decision_kind": "human_approved"}
        body.update(over)
        return self.command(
            lambda t, c, b: self.service.accept_candidate(
                t, c, character_id, candidate_id, b), body, tenant_id,
            command_id)

    def reject(self, character_id, candidate_id, tenant_id="t1",
               command_id="c3b", **over):
        body = dict(over)
        return self.command(
            lambda t, c, b: self.service.reject_candidate(
                t, c, character_id, candidate_id, b), body, tenant_id,
            command_id)

    def set_canonical(self, character_id, revision, tenant_id="t1",
                      command_id="c4", **over):
        body = {"revision": revision, "reason_text": "回退到第 9 版",
                "decision_kind": "human_approved"}
        body.update(over)
        return self.command(
            lambda t, c, b: self.service.set_canonical(
                t, c, character_id, b), body, tenant_id, command_id)

    def status(self, character_id, target, tenant_id="t1", command_id="c5",
               **over):
        body = dict(over)
        return self.command(
            lambda t, c, b: self.service.change_status(
                t, c, character_id, target, b), body, tenant_id, command_id)

    def alias(self, character_id, alias_text="林总", alias_kind="script_name",
              source="S03", tenant_id="t1", command_id="c6", **over):
        body = {"alias_text": alias_text, "alias_kind": alias_kind,
                "source": source}
        body.update(over)
        return self.command(
            lambda t, c, b: self.service.add_alias(t, c, character_id, b),
            body, tenant_id, command_id)

    def seeded(self, **over):
        """库里有 canonical v1 的角色。返回 (character_id, candidate_id)。"""
        character = self.create(**over)
        candidate = self.draft(character["character_id"])
        self.accept(character["character_id"], candidate["candidate_id"])
        return character["character_id"], candidate["candidate_id"]

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

    def accepted_events(self):
        return [e for e in self.events()
                if e["type"] == "asset.character.revision.accepted"]


def raises(status: int, code: str):
    """`pytest.raises` 的窄版：同时钉住状态码与错误码。

    `CommandConflict` 由 SDK 抛，没有 `status`（HTTP 层固定转 409），
    所以这里也接受它——同键不同 body 的语义在 SDK 与内核之间是同一个 409。
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


def _http_client(monkeypatch, tmp_path, **over):
    monkeypatch.setenv("S11_DB", str(tmp_path / "s11.db"))
    spec = importlib.util.spec_from_file_location(
        "s11_api_app",
        Path(__file__).resolve().parents[1] / "api" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s11_api_app"] = module
    h = Harness(conn=module._conn, **over)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h


def _headers(command_id="c1", tenant="t1", **extra):
    base = {"Idempotency-Key": command_id, "X-Tenant-Id": tenant}
    base.update(extra)
    return base


# ================================================================ 纯函数

class TestContentHash:
    """§2.2 行 139：`sha256(JCS(payload))`。"""

    def test_is_stable_and_order_independent(self):
        payload = {"a": {"x": 1, "y": 2}, "b": [1, 2, 3]}
        reordered = {"b": [1, 2, 3], "a": {"y": 2, "x": 1}}
        assert content_hash(payload) == content_hash(reordered)

    def test_differs_when_content_differs(self):
        assert content_hash({"a": 1}) != content_hash({"a": 2})

    def test_does_not_include_identity_or_clock(self):
        # 同样的内容无论给哪个角色、什么时候接受，hash 都一样——
        # 这正是"内容寻址"的含义，也是 C11-16 的成因。
        payload = {"view_manifest": views(), "source_kind": "generated"}
        assert content_hash(payload) == content_hash(dict(payload))


class TestIdentityFeatureHash:
    """§2.2 行 145。漂移检测的锚，算法必须可复算。"""

    def test_same_features_same_hash(self):
        assert identity_feature_hash(identity()) == identity_feature_hash(
            identity())

    def test_mark_order_does_not_change_the_hash(self):
        # 同一组痣换个数组顺序不是身份变化。
        a = identity(distinguishing_marks=[
            {"kind": "mole", "location": "left_cheek"},
            {"kind": "scar", "location": "right_brow"}])
        b = identity(distinguishing_marks=[
            {"kind": "scar", "location": "right_brow"},
            {"kind": "mole", "location": "left_cheek"}])
        assert identity_feature_hash(a) == identity_feature_hash(b)

    def test_changing_a_hard_anchor_changes_the_hash(self):
        assert identity_feature_hash(identity()) != identity_feature_hash(
            identity(face_shape="square"))

    def test_changing_a_soft_band_changes_the_hash_too(self):
        # hash 是全量的：soft 变化也让 hash 变。所以"hash 相同"这个
        # policy_auto 条件比 drift_class='none' 更严（§2.5 第二条件）。
        assert identity_feature_hash(identity()) != identity_feature_hash(
            identity(skin_tone_band="4"))

    def test_hash_is_64_hex(self):
        digest = identity_feature_hash(identity())
        assert len(digest) == 64
        int(digest, 16)


class TestClassifyDrift:
    """C11-23：方案没定义算法，这里把本实现的算法钉死，使它可以被复算。"""

    def test_identical_is_none(self):
        assert classify_drift(identity(), identity())["drift_class"] == "none"

    def test_hard_anchor_is_major(self):
        for field, value in (("face_shape", "square"), ("jaw", "angular"),
                             ("eye_shape", "round"), ("brow", "arched"),
                             ("nose_bridge", "high"),
                             ("lip_fullness", "full"),
                             ("hair", {"length": "short", "texture": "wavy",
                                       "hairline": "rounded"})):
            expected = identity()
            expected[field] = value
            result = classify_drift(identity(), expected)
            assert result["drift_class"] == "major", field
            assert field in result["changed_fields"]

    def test_distinguishing_marks_are_a_hard_anchor(self):
        # §2.3 行 190："distinguishing_marks 是身份锚"。
        changed = identity(distinguishing_marks=[])
        assert classify_drift(identity(), changed)["drift_class"] == "major"

    def test_soft_band_only_is_minor(self):
        for field, value in (("skin_tone_band", "5"), ("age_band", "30s"),
                             ("build", "athletic"),
                             ("height_cm_band", "170-175"),
                             ("eye_distance_ratio", 0.5)):
            expected = identity()
            expected[field] = value
            result = classify_drift(identity(), expected)
            assert result["drift_class"] == "minor", field

    def test_unknown_field_is_conservative_major(self):
        # §2.3 允许新增可选字段。一个未来的"骨点云 hash"若落进 soft，
        # 就等于把换人放行成 minor——所以未知字段保守判 major。
        expected = identity(bone_landmark_hash="x" * 8)
        result = classify_drift(identity(), expected)
        assert result["drift_class"] == "major"
        assert "bone_landmark_hash" in result.get("unknown_fields", [])

    def test_no_numeric_thresholds(self):
        # §2.3 要求分档。拿一个极小的浮点变化检验算法不理解"阈值"：
        # eye_distance_ratio 只要变了就是 minor，不管多小。
        near = identity(eye_distance_ratio=0.4500001)
        assert classify_drift(identity(), near)["drift_class"] == "minor"


# ================================================================ 结构边界

class TestNoSecondAuthority:
    """§0 / §0.2 / §2.8 / §9 反模式 1–12。断言的是**列不存在**。"""

    def columns(self, conn, table):
        return {r["name"] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}

    def test_revision_has_no_costume_story_or_voice_columns(self):
        # §0.2 第 1–3 条 + §2.8：服装归 S12、剧情状态归 S16、声音归 S15。
        h = Harness()
        cols = self.columns(h.conn, "character_revision")
        banned = {"costume", "costume_ref", "outfit", "makeup", "makeup_ref",
                  "injury", "injuries", "wound", "current_location",
                  "location", "scene_id", "scene_ids", "voice_id",
                  "voice_ref", "emotion", "mood", "time_of_day",
                  "status", "state"}
        assert not (cols & banned), cols & banned

    def test_character_table_has_no_costume_or_location_columns(self):
        h = Harness()
        cols = self.columns(h.conn, "character")
        assert not (cols & {"costume", "outfit", "location", "voice_id",
                            "injury"})

    def test_candidate_cannot_be_bound(self):
        # §1.3 / §2.4：候选在结构上不可被 S10 绑定。
        h = Harness()
        cols = self.columns(h.conn, "character_candidate")
        assert "revision" not in cols
        assert "canonical_eligible" not in cols
        assert "accepted_at" not in cols

    def test_canonical_pointer_has_no_identity_hash_copy(self):
        # 指针指向 revision，不复制它的事实。
        h = Harness()
        cols = self.columns(h.conn, "canonical_pointer")
        assert "identity_feature_hash" not in cols
        assert "content_hash" not in cols

    def test_no_forbidden_tables(self):
        # §2.8 的七张表一张都不建。
        h = Harness()
        names = {r["name"] for r in h.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        banned_prefixes = ("character_costume", "character_injury",
                           "character_location", "character_voice",
                           "character_usage", "character_series",
                           "character_reverse")
        assert not {n for n in names
                    if n.startswith(banned_prefixes)}, names
        assert names >= {"character", "character_alias", "character_revision",
                         "character_candidate", "canonical_pointer",
                         "revision_reference"}

    def test_no_merge_method(self):
        # C11-14：`:merge` 没有 body、没有列、没有验收项，**不实现**。
        assert not hasattr(CharacterAssetsService, "merge_characters")
        assert not hasattr(CharacterAssetsService, "merge")

    def test_no_model_or_rights_client(self):
        # §9 反模式 5 / 11：不判权利，不直接调模型。
        h = Harness()
        for banned in ("model_client", "generator", "rights_client",
                       "rights_verdict", "s02", "authorizer"):
            assert not hasattr(h.service, banned), banned


# ================================================================ 数据库约束

class TestDatabaseConstraints:
    """绕过内核用裸 SQL 撞约束。

    内核本来就不会产生坏行，所以走内核的断言只能证明"当前代码路径没事"。
    这一组钉的是：将来有人加一条绕过校验的写路径时，挡住他的必须是数据库。
    """

    @pytest.mark.parametrize("sql, fragment", [
        ("UPDATE character SET identity_kind='cartoon'", "identity_kind"),
        ("UPDATE character SET status='archived'", "status"),
        ("UPDATE character SET current_revision=-1", "current_revision"),
        ("UPDATE character_revision SET source_kind='invented'", "source_kind"),
        ("UPDATE character_revision SET content_hash='tooshort'",
         "cr_hash_len"),
        ("UPDATE character_candidate SET state='ZOMBIE'", "state"),
        ("UPDATE canonical_pointer SET decision_kind='robot'",
         "decision_kind"),
        ("UPDATE character_alias SET alias_kind='nick'", "alias_kind"),
        ("UPDATE revision_reference SET ref_role='borrowed'", "ref_role"),
        ("UPDATE revision_reference SET target_service='S99'",
         "target_service"),
    ])
    def test_enum_check_rejects_value_outside_vocabulary(self, sql, fragment):
        h = Harness()
        character_id, candidate_id = h.seeded()
        h.status(character_id, "deprecated", command_id="c7")
        # 让 candidate / alias / reference 都有行可改：UPDATE 不到行时
        # 什么都不撞，用例会"绿得毫无意义"。
        h.draft(character_id, command_id="c8", expected_current_revision=1)
        h.alias(character_id, command_id="c9")
        h.conn.execute(
            "INSERT INTO revision_reference (tenant_id, character_id,"
            " revision, ref_role, target_service, target_id, target_revision)"
            " VALUES ('t1',?,1,'base_for_lora','S48','art_x',1)",
            (character_id,))
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(sql)
        assert fragment in str(info.value), str(info.value)

    def test_parent_revision_must_precede_child(self):
        h = Harness()
        character_id, _ = h.seeded()
        candidate = h.draft(character_id, command_id="c10",
                            expected_current_revision=1,
                            identity_features=identity(),
                            view_manifest=views(**{"front": ref("art_front")}))
        h.accept(character_id, candidate["candidate_id"], command_id="c11",
                 expected_current_revision=1)
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "UPDATE character_revision SET parent_revision=2"
                " WHERE tenant_id='t1' AND character_id=? AND revision=2",
                (character_id,))
        assert "cr_parent_lt" in str(info.value)

    def test_content_hash_must_be_64_chars(self):
        h = Harness()
        character_id, _ = h.seeded()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "UPDATE character_revision SET content_hash='short'"
                " WHERE tenant_id='t1' AND character_id=? AND revision=1",
                (character_id,))
        assert "cr_hash_len" in str(info.value)

    def test_canonical_pointer_cannot_run_ahead_of_head(self):
        h = Harness()
        character_id, _ = h.seeded()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "UPDATE character SET canonical_revision=9"
                " WHERE tenant_id='t1' AND character_id=?", (character_id,))
        assert "character_canonical_within_head" in str(info.value)

    def test_only_one_live_canonical_pointer_per_character(self):
        # supersede 是"插入新行 + 旧行打 superseded_at"。两个都活着 =
        # 两个身份基准。要先造一个还没有指针行的 revision（v3 接受但
        # 不改指针），否则撞的是主键——那样即使部分索引被删用例也绿。
        h = Harness()
        character_id, _ = h.seeded()
        cand2 = h.draft(character_id, command_id="c20",
                        expected_current_revision=1,
                        identity_features=identity(face_shape="oval"),
                        view_manifest=views())
        h.accept(character_id, cand2["candidate_id"], command_id="c21",
                 expected_current_revision=1, set_canonical=False)
        assert h.rows("canonical_pointer", tenant_id="t1",
                      character_id=character_id, revision=2) == []
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "INSERT INTO canonical_pointer (tenant_id, character_id,"
                " revision, decided_at, decided_by, decision_kind)"
                " VALUES ('t1',?,2,'2026-01-01T00:00:00.000Z','x',"
                "'human_approved')", (character_id,))
        # 两列而不是三列：三列就是撞了主键，说明这条用例什么都没测到。
        assert str(info.value) == (
            "UNIQUE constraint failed: canonical_pointer.tenant_id, "
            "canonical_pointer.character_id")

    def test_same_display_name_in_one_series_is_rejected(self):
        h = Harness()
        h.create()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "INSERT INTO character (tenant_id, character_id, project_id,"
                " series_scope, display_name, identity_kind, status,"
                " canonical_revision, current_revision, series_of_record,"
                " created_by, created_at)"
                " VALUES ('t1','chr_other','prj_1','ser_1','林岚','fictional',"
                "'active',NULL,0,'ser_1','x','2026-01-01T00:00:00.000Z')")
        assert "UNIQUE" in str(info.value)

    def test_content_hash_is_unique_per_tenant(self):
        # E28-B3 的物理形式：**完全相同的 payload** 换个角色也不接受。
        h = Harness()
        character_id, candidate_id = h.seeded()
        other = h.create(display_name="另一个人", command_id="c30")
        candidate = h.draft(other["character_id"], command_id="c31",
                            identity_features=identity(),
                            view_manifest=full_views())
        with pytest.raises(ServiceError) as info:
            h.accept(other["character_id"], candidate["candidate_id"],
                     command_id="c32")
        assert info.value.code == "DUPLICATE_CHARACTER_CONTENT"
        assert info.value.status == 409

    def test_foreign_keys_are_enforced(self):
        # `PRAGMA foreign_keys` 默认 OFF。这一条钉的是"连上了"。
        h = Harness()
        assert h.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "INSERT INTO character_candidate (tenant_id, candidate_id,"
                " character_id, content_hash, payload, evaluation,"
                " identity_drift, state, created_by, created_at, expires_at)"
                " VALUES ('t1','cand_ghost','chr_ghost',?, '{}','{}','{}',"
                "'PENDING','x','2026-01-01T00:00:00.000Z',"
                "'2026-02-01T00:00:00.000Z')", (HASH_A,))
        assert "FOREIGN KEY" in str(info.value)

    def test_pointer_cannot_target_a_nonexistent_revision(self):
        # canonical_pointer 的 FK 指向 (tenant, character, revision)：
        # 指针不能指向不存在的 revision。用一个**还没有 canonical** 的角色
        # 来撞，否则先撞上的是 canonical_one_current（那条已在上面测过）。
        h = Harness()
        character = h.create()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "INSERT INTO canonical_pointer (tenant_id, character_id,"
                " revision, decided_at, decided_by, decision_kind)"
                " VALUES ('t1',?,99,'2026-01-01T00:00:00.000Z','x',"
                "'human_approved')", (character["character_id"],))
        assert "FOREIGN KEY" in str(info.value)


# ================================================================ 登记与幂等

class TestCreateAndIdempotency:
    def test_create_returns_stable_id(self):
        # E28-B2 / §6.1
        h = Harness()
        first = h.create()
        assert first["character_id"].startswith("chr_")
        assert first["created"] is True
        assert first["canonical_revision"] is None
        assert first["current_revision"] == 0

    def test_same_key_same_body_replays(self):
        # E12-B1
        h = Harness()
        first = h.create()
        again = h.create()
        assert again["replayed"] is True
        assert again["character_id"] == first["character_id"]
        assert h.count("character") == 1

    def test_same_key_different_body_is_conflict(self):
        # E12-B2：同键不同 body 是 409，**不是覆盖**。
        h = Harness()
        h.create()
        with raises(409, "IDEMPOTENCY_KEY_REUSED"):
            h.create(display_name="别人")
        assert h.count("character") == 1

    def test_repeat_registration_returns_existing_id(self):
        # E12-B3：不同 command_id、同一自然键 → 返回既有 ID。
        h = Harness()
        first = h.create(command_id="c1")
        again = h.create(command_id="c2")
        assert again["character_id"] == first["character_id"]
        assert again["created"] is False
        assert h.count("character") == 1

    def test_in_flight_command_is_its_own_409(self):
        # SDK 的第三个状态：同键同 body、首次执行还没落结果。这不是"键被
        # 复用"——调用方应当重试而不是换键，所以 HTTP 层给它单独的码和
        # Retry-After，不能混进 IDEMPOTENCY_KEY_REUSED。
        from idempotency_sdk import CommandInProgress
        h = Harness()
        body = {"project_id": "prj_1", "series_scope": "ser_1",
                "display_name": "林岚", "identity_kind": "fictional"}
        h.idem.begin("t1", "c1", canonical_request_hash(
            {**body, "command_id": "c1"}))
        with pytest.raises(CommandInProgress):
            h.command(lambda t, c, b: h.service.create_character(t, c, b),
                      body, "t1", "c1")
        assert h.count("character") == 0

    def test_failed_command_releases_the_key(self):
        # 参数错 → abort → 同键可以用正确 body 重试（否则一次笔误就把
        # command_id 永久废掉）。
        h = Harness()
        with raises(400, "MALFORMED_REQUEST"):
            h.create(display_name="")
        retried = h.create(display_name="林岚")
        assert retried["created"] is True
        assert h.count("character") == 1
        assert h.count("idempotency_record") == 1

    def test_same_name_in_two_tenants_is_independent(self):
        # E28-B2
        h = Harness()
        a = h.create(tenant_id="t1", command_id="c1")
        b = h.create(tenant_id="t2", command_id="c2")
        assert a["character_id"] != b["character_id"]
        assert h.count("character") == 2

    def test_unknown_identity_kind_is_malformed(self):
        h = Harness()
        with raises(400, "MALFORMED_REQUEST"):
            h.create(identity_kind="cartoon")

    def test_missing_display_name_is_malformed(self):
        h = Harness()
        with raises(400, "MALFORMED_REQUEST"):
            h.create(display_name="")


# ================================================================ 候选校验

class TestCandidateValidation:
    def test_happy_path_creates_pending_candidate(self):
        # E25-B1 的前半段
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"])
        assert candidate["state"] == "PENDING"
        assert candidate["candidate_id"].startswith("cand_")
        assert len(candidate["content_hash"]) == 64
        assert h.count("character_revision") == 0  # 候选不是 revision

    def test_missing_artifact_is_422(self):
        # E29-B2
        h = Harness()
        character = h.create()
        with raises(422, "ARTIFACT_NOT_FOUND"):
            h.draft(character["character_id"],
                    view_manifest={"front": ref("art_ghost")})
        assert h.count("character_candidate") == 0

    def test_artifact_hash_mismatch_is_422(self):
        # F2
        h = Harness()
        character = h.create()
        with raises(422, "ARTIFACT_HASH_MISMATCH"):
            h.draft(character["character_id"],
                    view_manifest={"front": ref("art_front", HASH_C)})
        assert h.count("character_candidate") == 0

    def test_s28_timeout_is_503_and_persists_no_candidate(self):
        # F10
        h = Harness()
        h.artifacts["art_front"] = ConnectionError("s28 down")
        character = h.create()
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            h.draft(character["character_id"])
        assert h.count("character_candidate") == 0

    def test_unconfigured_s28_fails_closed(self):
        # 没有 S28 就一个候选都不接。
        from character_assets.deps import build_artifact_source
        conn = sqlite3.connect(":memory:", check_same_thread=False,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        service = CharacterAssetsService(
            conn, artifact_source=build_artifact_source(None))
        SqliteIdempotencyStore(conn)
        service.create_character("t1", "c1", {
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"})
        character_id = conn.execute(
            "SELECT character_id FROM character").fetchone()[0]
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            service.draft_candidate("t1", "c2", character_id,
                                    candidate_body())

    def test_view_manifest_incomplete_is_422(self):
        # F3 / E02-B1：front 永远必需。
        h = Harness()
        character = h.create()
        with raises(422, "VIEW_MANIFEST_INCOMPLETE") as info:
            h.draft(character["character_id"],
                    view_manifest={"side_left": ref("art_side")})
        assert info.value.details["missing"] == ["front"]

    def test_full_turnaround_requires_all_four_views(self):
        # §4.2 第 2 步
        h = Harness()
        character = h.create()
        with raises(422, "VIEW_MANIFEST_INCOMPLETE") as info:
            h.draft(character["character_id"], require_full_turnaround=True,
                    view_manifest={"front": ref("art_front"),
                                   "side_left": ref("art_side")})
        assert set(info.value.details["missing"]) == {"back", "three_quarter"}

    def test_full_turnaround_accepts_four_views(self):
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"],
                            require_full_turnaround=True,
                            view_manifest=full_views())
        assert candidate["state"] == "PENDING"

    def test_story_state_keys_in_view_manifest_are_rejected(self):
        # §0.2 第 3 条 / §9 反模式 3：`injured` 不是视图。
        h = Harness()
        character = h.create()
        with raises(400, "MALFORMED_REQUEST") as info:
            h.draft(character["character_id"],
                    view_manifest={"front": ref("art_front"),
                                   "injured_left_arm": ref("art_side")})
        assert "injured_left_arm" in info.value.details["unknown_views"]

    def test_free_text_identity_is_rejected(self):
        # §9 反模式 7
        h = Harness()
        character = h.create()
        with raises(400, "MALFORMED_REQUEST"):
            h.draft(character["character_id"],
                    identity_features={"description": "一个漂亮的女孩"})

    def test_out_of_band_identity_value_is_rejected(self):
        # §2.3：分档，不是自由值
        h = Harness()
        character = h.create()
        with raises(400, "MALFORMED_REQUEST"):
            h.draft(character["character_id"],
                    identity_features=identity(face_shape="很方的脸"))

    def test_eye_distance_must_be_in_unit_range(self):
        h = Harness()
        character = h.create()
        with raises(400, "MALFORMED_REQUEST"):
            h.draft(character["character_id"],
                    identity_features=identity(eye_distance_ratio=1.4))

    def test_mark_without_location_is_rejected(self):
        h = Harness()
        character = h.create()
        with raises(400, "MALFORMED_REQUEST"):
            h.draft(character["character_id"],
                    identity_features=identity(
                        distinguishing_marks=[{"kind": "mole"}]))

    def test_unknown_identity_field_is_preserved(self):
        # E01-B1：新增字段必须可选且能解析；内核不丢它。
        h = Harness()
        character = h.create()
        features = identity(bone_landmark_hash="d" * 64)
        candidate = h.draft(character["character_id"],
                            identity_features=features)
        payload = json.loads(h.rows(
            "character_candidate", tenant_id="t1",
            candidate_id=candidate["candidate_id"])[0]["payload"])
        assert payload["identity_features"]["bone_landmark_hash"] == "d" * 64

    def test_quality_gate_ref_is_required(self):
        h = Harness()
        character = h.create()
        with raises(400, "MALFORMED_REQUEST"):
            h.draft(character["character_id"], quality_gate_ref=None)

    def test_stale_expected_revision_is_conflict(self):
        # F5 / §6.2
        h = Harness()
        character_id, _ = h.seeded()
        with raises(409, "VERSION_CONFLICT") as info:
            h.draft(character_id, command_id="c40",
                    expected_current_revision=0)
        assert info.value.retryable is True
        assert info.value.details["current"] == 1

    def test_missing_expected_revision_is_malformed(self):
        # 从不"猜你要改哪一版"。
        h = Harness()
        character = h.create()
        with raises(400, "MALFORMED_REQUEST"):
            h.draft(character["character_id"],
                    expected_current_revision=None)

    def test_tombstoned_character_takes_no_new_candidate(self):
        # F9
        h = Harness()
        character_id, _ = h.seeded()
        h.status(character_id, "deprecated", command_id="c50")
        h.status(character_id, "tombstoned", command_id="c51")
        with raises(422, "CHARACTER_TOMBSTONED"):
            h.draft(character_id, command_id="c52",
                    expected_current_revision=1)
        assert h.count("character_candidate") == 1

    def test_pending_cap_is_429(self):
        # C11-13：方案没定义 429 触发条件，这里用 PENDING 数封顶。
        h = Harness(conn=None, max_pending_candidates=2)
        character = h.create()
        h.draft(character["character_id"], command_id="c60",
                view_manifest={"front": ref("art_front")},
                identity_features=identity(skin_tone_band="1"))
        h.draft(character["character_id"], command_id="c61",
                view_manifest={"front": ref("art_side")},
                identity_features=identity(skin_tone_band="2"))
        with raises(429, "WRITE_BACKPRESSURE") as info:
            h.draft(character["character_id"], command_id="c62",
                    view_manifest={"front": ref("art_back")},
                    identity_features=identity(skin_tone_band="4"))
        assert info.value.retryable is True
        assert info.value.details["limit"] == 2

    def test_expired_candidate_cannot_be_accepted(self):
        # §3.1 的 ttl──► EXPIRED
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"])
        h.conn.execute(
            "UPDATE character_candidate SET expires_at='2000-01-01T00:00:00.000Z'"
            " WHERE tenant_id='t1' AND candidate_id=?",
            (candidate["candidate_id"],))
        with raises(409, "CANDIDATE_NOT_PENDING") as info:
            h.accept(character["character_id"], candidate["candidate_id"],
                     command_id="c70")
        assert info.value.details["state"] == "EXPIRED"
        assert h.count("character_revision") == 0

    def test_expired_candidate_cannot_be_rejected_either(self):
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"])
        h.conn.execute(
            "UPDATE character_candidate SET expires_at='2000-01-01T00:00:00.000Z'"
            " WHERE tenant_id='t1' AND candidate_id=?",
            (candidate["candidate_id"],))
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.reject(character["character_id"], candidate["candidate_id"],
                     command_id="c71")
        # 关键：409 回滚之后，EXPIRED 必须仍然落库（见 `_sweep_expired`
        # 的注释）。否则下一次 draft 照样数它，用户被看不见的死候选 429。
        state = h.rows("character_candidate", tenant_id="t1",
                       candidate_id=candidate["candidate_id"])[0]["state"]
        assert state == "EXPIRED"

    def test_expired_candidates_stop_counting_against_the_cap(self):
        h = Harness(conn=None, max_pending_candidates=1)
        character = h.create()
        first = h.draft(character["character_id"], command_id="c80",
                        view_manifest={"front": ref("art_front")},
                        identity_features=identity(skin_tone_band="1"))
        h.conn.execute(
            "UPDATE character_candidate SET expires_at='2000-01-01T00:00:00.000Z'"
            " WHERE tenant_id='t1' AND candidate_id=?",
            (first["candidate_id"],))
        # 不 429：第一条在上一次 draft 之后过期了，扫描先把它落成 EXPIRED。
        second = h.draft(character["character_id"], command_id="c81",
                         view_manifest={"front": ref("art_side")},
                         identity_features=identity(skin_tone_band="2"))
        assert second["state"] == "PENDING"

    def test_artifact_hash_is_filled_from_s28_when_not_declared(self):
        # 调用方不必自带 hash；S28 的结论会补进来。
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"],
                            view_manifest={"front": {"artifact_id":
                                                     "art_front"}})
        payload = json.loads(h.rows(
            "character_candidate", tenant_id="t1",
            candidate_id=candidate["candidate_id"])[0]["payload"])
        assert payload["view_manifest"]["front"]["content_hash"] == HASH_A


# ================================================================ 依赖契约

class TestDependencyContract:
    def test_connection_error_becomes_503_retryable(self):
        h = Harness()
        character = h.create()
        h.artifacts["art_front"] = ConnectionError("timeout")
        with raises(503, "DEPENDENCY_UNAVAILABLE") as info:
            h.draft(character["character_id"])
        assert info.value.retryable is True

    def test_upstream_service_error_is_keyed_by_the_caller(self):
        # 上游 5xx → 503（重试有意义）；上游 4xx 原样上抛。
        h = Harness()
        character = h.create()
        h.artifacts["art_front"] = ServiceError(500, "S28_INTERNAL", "boom")
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            h.draft(character["character_id"])

    def test_artifact_404_is_not_retryable(self):
        # E29-B2：调用方的错，不该重试。
        h = Harness()
        character = h.create()
        h.artifacts["art_front"] = ServiceError(404, "NOT_FOUND", "nope")
        with raises(422, "ARTIFACT_NOT_FOUND") as info:
            h.draft(character["character_id"])
        assert info.value.retryable is False

    def test_build_artifact_source_maps_http_404_to_none(self):
        from character_assets.deps import build_artifact_source
        import urllib.error

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 404, "not found", {}, None)

        source = build_artifact_source("http://s28.invalid")
        import urllib.request
        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            assert source("t1", "art_x") is None
        finally:
            urllib.request.urlopen = original

    def test_build_artifact_source_wraps_transport_failure(self):
        from character_assets.deps import build_artifact_source
        import urllib.error
        import urllib.request

        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("refused")

        source = build_artifact_source("http://s28.invalid")
        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            with pytest.raises(ConnectionError):
                source("t1", "art_x")
        finally:
            urllib.request.urlopen = original

    def test_quality_gate_unconfigured_does_not_break_inline(self):
        # 没有 QC 源时，内联结论仍然有效（C11-11）：draft 根本不会去连上游。
        h = Harness(conn=None)
        assert h.service.quality_gate_source is None
        character = h.create()
        candidate = h.draft(
            character["character_id"],
            quality_gate={"identity_consistency": "pass"})
        assert candidate["state"] == "PENDING"
        # 内联的 fail 在没有 QC 源时同样生效——结论来源不影响判断。
        with raises(422, "CANDIDATE_QC_FAILED"):
            h.draft(character["character_id"], command_id="c90",
                    quality_gate={"identity_consistency": "fail"})

    def test_quality_gate_source_used_when_configured(self):
        # 配置了 QC 源时，quality_gate_ref 会去读上游，读不到 → 503。
        h = Harness(conn=None)
        calls = []

        def gate(tenant_id, ref):
            calls.append((tenant_id, ref))
            return {"identity_consistency": "pass"}

        h.service.quality_gate_source = gate
        character = h.create()
        # 不带内联结论，才会去读 quality_gate_ref。
        candidate = h.draft(character["character_id"], quality_gate=None)
        assert candidate["state"] == "PENDING"
        assert calls == [("t1", {"id": "qg_1", "revision": 1})]
        # 内联优先：同一条命令里给了结论就不打上游。
        h.draft(character["character_id"], command_id="c91",
                quality_gate={"identity_consistency": "pass"})
        assert len(calls) == 1


# ================================================================ 接受

class TestAccept:
    def test_accept_creates_immutable_revision_and_event(self):
        # E02-B1
        h = Harness()
        character_id, candidate_id = h.seeded()
        revision = h.rows("character_revision", tenant_id="t1",
                          character_id=character_id)[0]
        assert revision["revision"] == 1
        assert revision["accepted_by"] == json.dumps(
            {"principal_id": "t1"}, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False)
        events = h.accepted_events()
        assert len(events) == 1
        assert events[0]["subject"] == f"character/{character_id}"

    def test_event_has_exactly_the_eight_contract_keys(self):
        # §5.3：`additionalProperties: false`，8 个键一个不多。
        h = Harness()
        h.seeded()
        data = h.accepted_events()[0]["data"]
        assert set(data) == {
            "schema_version", "character_ref", "identity_feature_hash",
            "became_canonical", "canonical_revision", "rights_ref",
            "view_count", "source_kind"}
        assert set(data["character_ref"]) == {
            "service_id", "id", "revision", "content_hash"}
        assert set(data["rights_ref"]) == {
            "rights_owner", "rights_id", "rights_revision", "rights_hash"}

    def test_event_values_are_the_facts_not_placeholders(self):
        h = Harness()
        character_id, _ = h.seeded()
        data = h.accepted_events()[0]["data"]
        assert data["schema_version"] == "2.0"
        assert data["character_ref"]["service_id"] == "S11"
        assert data["character_ref"]["id"] == character_id
        assert data["character_ref"]["revision"] == 1
        assert len(data["character_ref"]["content_hash"]) == 64
        assert data["identity_feature_hash"] == identity_feature_hash(
            identity())
        assert data["became_canonical"] is True
        assert data["canonical_revision"] == 1
        assert data["source_kind"] == "generated"
        # CRC-2 / C11-8：view_count 没有 DDL 依据，取 manifest 的视图数。
        assert data["view_count"] == 4

    def test_view_count_matches_manifest_length(self):
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"], view_manifest=views())
        h.accept(character["character_id"], candidate["candidate_id"],
                 command_id="c100")
        assert h.accepted_events()[0]["data"]["view_count"] == 1

    def test_event_sequence_equals_revision(self):
        # §5.3 行 542：aggregate_sequence = revision。
        h = Harness()
        character_id, _ = h.seeded()
        event = h.accepted_events()[0]
        assert event["sequence"] == 1
        assert event["sequence"] == event["data"]["character_ref"]["revision"]

    def test_became_canonical_false_keeps_pointer(self):
        # E02-B2：补图不改身份基准。
        h = Harness()
        character_id, _ = h.seeded()
        cand2 = h.draft(character_id, command_id="c110",
                        expected_current_revision=1,
                        view_manifest={"front": ref("art_front"),
                                       "back": ref("art_back")})
        result = h.accept(character_id, cand2["candidate_id"],
                          command_id="c111", expected_current_revision=1,
                          set_canonical=False)
        assert result["became_canonical"] is False
        assert result["canonical_revision"] == 1
        data = h.accepted_events()[-1]["data"]
        assert data["became_canonical"] is False
        assert data["canonical_revision"] == 1
        assert h.rows("character", tenant_id="t1",
                      character_id=character_id)[0]["current_revision"] == 2
        # 身份没变：两个 revision 的 hash 相同（E02-B2 第三句）。
        hashes = {r["identity_feature_hash"] for r in h.rows(
            "character_revision", tenant_id="t1", character_id=character_id)}
        assert len(hashes) == 1

    def test_candidate_cannot_be_accepted_twice(self):
        # §3.1：ACCEPTED 是终态。
        h = Harness()
        character_id, candidate_id = h.seeded()
        with raises(409, "CANDIDATE_NOT_PENDING") as info:
            h.accept(character_id, candidate_id, command_id="c120")
        assert info.value.details["state"] == "ACCEPTED"
        assert h.count("character_revision") == 1

    def test_concurrent_accept_one_wins(self):
        # 并发接受 100 次：1 成功，其余冲突；canonical 唯一。
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"])
        h.accept(character["character_id"], candidate["candidate_id"],
                 command_id="c130")
        for index in range(99):
            # 99 个竞争者都带着"我看到 head=0"的期望（真实并发就是这么来的）。
            # 第一步 CAS 就把它们挡在 409 VERSION_CONFLICT——这比
            # CANDIDATE_NOT_PENDING 更早，也正是 §6.3 的锁序。
            with raises(409, "VERSION_CONFLICT"):
                h.accept(character["character_id"], candidate["candidate_id"],
                         command_id=f"c13x{index}", set_canonical=True,
                         decision_kind="human_approved",
                         expected_current_revision=0)
        live = h.conn.execute(
            "SELECT COUNT(*) FROM canonical_pointer WHERE tenant_id='t1'"
            " AND superseded_at IS NULL").fetchone()[0]
        assert live == 1

    def test_duplicate_content_is_409(self):
        # F6 / E28-B3
        h = Harness()
        character_id, _ = h.seeded()
        other = h.create(display_name="同名不同人", command_id="c140")
        candidate = h.draft(other["character_id"], command_id="c141",
                            identity_features=identity(),
                            view_manifest=full_views())
        with raises(409, "DUPLICATE_CHARACTER_CONTENT") as info:
            h.accept(other["character_id"], candidate["candidate_id"],
                     command_id="c142")
        assert info.value.details["existing_revision"] == 1

    def test_pointer_records_decision_and_reason(self):
        h = Harness()
        character_id, _ = h.seeded()
        pointer = h.rows("canonical_pointer", tenant_id="t1",
                         character_id=character_id, revision=1)[0]
        assert pointer["decision_kind"] == "human_approved"
        assert pointer["prior_canonical"] is None
        assert pointer["superseded_at"] is None

    def test_reject_moves_candidate_out_of_pending(self):
        # §4.4：REJECTED 不写 revision、不发事件。
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"])
        events_before = len(h.events())
        result = h.reject(character["character_id"],
                          candidate["candidate_id"], command_id="c150",
                          reason_text="不像")
        assert result["state"] == "REJECTED"
        assert h.count("character_revision") == 0
        assert len(h.events()) == events_before

    def test_rejecting_twice_is_409(self):
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"])
        h.reject(character["character_id"], candidate["candidate_id"],
                 command_id="c160")
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.reject(character["character_id"], candidate["candidate_id"],
                     command_id="c161")

    def test_replay_writes_no_second_revision_or_event(self):
        # E12-B1 的后半段。**重放的 body 必须逐字节相同**——否则 SDK 判
        # 同键不同体，那测的是 E12-B2 而不是这一条。
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"])
        h.accept(character["character_id"], candidate["candidate_id"],
                 command_id="c170", expected_current_revision=0)
        again = h.accept(character["character_id"], candidate["candidate_id"],
                         command_id="c170", expected_current_revision=0)
        assert again["replayed"] is True
        assert h.count("character_revision") == 1
        assert len(h.accepted_events()) == 1

    def test_derived_reference_is_written_for_parent(self):
        # §2.6 / C11-22
        h = Harness()
        character_id, _ = h.seeded()
        cand2 = h.draft(character_id, command_id="c180",
                        expected_current_revision=1,
                        parent_revision=1,
                        view_manifest={"front": ref("art_front"),
                                       "back": ref("art_back")})
        h.accept(character_id, cand2["candidate_id"], command_id="c181",
                 expected_current_revision=1, set_canonical=False)
        refs = h.rows("revision_reference", tenant_id="t1",
                      character_id=character_id, revision=2)
        assert [(r["ref_role"], r["target_service"], r["target_revision"])
                for r in refs] == [("derived_from", "S11", 1)]

    def test_lora_refs_with_revision_become_references(self):
        h = Harness()
        character = h.create()
        candidate = h.draft(
            character["character_id"],
            lora_refs=[{"artifact_id": "lora_1", "revision": 3,
                        "content_hash": HASH_C},
                       {"artifact_id": "lora_2"}])
        h.accept(character["character_id"], candidate["candidate_id"],
                 command_id="c190")
        refs = h.rows("revision_reference", tenant_id="t1",
                      character_id=character["character_id"], revision=1)
        # 缺 revision 的那条写不进边（target_revision NOT NULL），
        # 只留在 payload 里——C11-22。
        assert [(r["ref_role"], r["target_id"]) for r in refs] == [
            ("base_for_lora", "lora_1")]
        payload = json.loads(h.rows(
            "character_revision", tenant_id="t1",
            character_id=character["character_id"], revision=1)[0]["payload"])
        assert len(payload["lora_refs"]) == 2

    def test_crashing_midway_leaves_no_half_write(self):
        # §8 通用："事务中崩溃 → 无半写；revision 与 outbox 一致"。
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"])

        original = h.service.outbox.append

        def boom(*_args, **_kwargs):
            raise RuntimeError("process died before commit")

        h.service.outbox.append = boom
        try:
            with pytest.raises(RuntimeError):
                h.accept(character["character_id"], candidate["candidate_id"],
                         command_id="c200")
        finally:
            h.service.outbox.append = original
        # revision 与 candidate 状态一起回滚；候选仍在 PENDING，可重试。
        assert h.count("character_revision") == 0
        assert h.count("canonical_pointer") == 0
        assert h.rows("character_candidate", tenant_id="t1",
                      candidate_id=candidate["candidate_id"])[0][
                          "state"] == "PENDING"
        assert h.count("outbox") == 1  # 只有 draft 那条本地事件


# ================================================================ 漂移门

class TestDriftGate:
    """§2.5 的三个 policy_auto 条件 + F7。"""

    def test_major_drift_with_policy_auto_is_422(self):
        # F7 / E02-B3：策略不接受"评审"环节，它接受的是"没人看也安全"。
        h = Harness()
        character_id, _ = h.seeded()
        cand = h.draft(character_id, command_id="c210",
                       expected_current_revision=1,
                       identity_features=identity(face_shape="square"),
                       view_manifest={"front": ref("art_front"),
                                      "back": ref("art_back")})
        with raises(422, "IDENTITY_DRIFT_REQUIRES_HUMAN") as info:
            h.accept(character_id, cand["candidate_id"], command_id="c211",
                     expected_current_revision=1,
                     decision_kind="policy_auto")
        assert info.value.details["drift_class"] == "major"
        assert "face_shape" in info.value.details["changed_fields"]
        assert h.count("character_revision") == 1

    def test_major_drift_with_human_is_allowed(self):
        # 人审可以接受一次换人；挡住的只是"无人看"的那条路径。
        h = Harness()
        character_id, _ = h.seeded()
        cand = h.draft(character_id, command_id="c220",
                       expected_current_revision=1,
                       identity_features=identity(face_shape="square"),
                       view_manifest={"front": ref("art_front"),
                                      "back": ref("art_back")})
        result = h.accept(character_id, cand["candidate_id"],
                          command_id="c221", expected_current_revision=1,
                          decision_kind="human_approved")
        assert result["revision"] == 2
        assert result["canonical_revision"] == 2

    def test_soft_band_change_with_policy_auto_is_still_422(self):
        # §2.5 第二个条件：**hash 完全相同**，不是"没有 major 漂移"。
        h = Harness()
        character_id, _ = h.seeded()
        cand = h.draft(character_id, command_id="c230",
                       expected_current_revision=1,
                       identity_features=identity(skin_tone_band="5"),
                       view_manifest={"front": ref("art_front"),
                                      "back": ref("art_back")})
        assert cand["identity_drift"]["drift_class"] == "minor"
        with raises(422, "IDENTITY_DRIFT_REQUIRES_HUMAN") as info:
            h.accept(character_id, cand["candidate_id"], command_id="c231",
                     expected_current_revision=1,
                     decision_kind="policy_auto")
        assert "prior" in info.value.details
        assert info.value.details["prior"] != info.value.details["candidate"]

    def test_identical_identity_allows_policy_auto(self):
        # §2.5 三个条件同时满足：drift none + 同 hash + 只改视图。
        h = Harness()
        character_id, _ = h.seeded()
        cand = h.draft(character_id, command_id="c240",
                       expected_current_revision=1,
                       view_manifest={"front": ref("art_front"),
                                      "back": ref("art_back")})
        result = h.accept(character_id, cand["candidate_id"],
                          command_id="c241", expected_current_revision=1,
                          decision_kind="policy_auto")
        assert result["canonical_revision"] == 2

    def test_first_acceptance_drift_is_reported_as_no_canonical(self):
        # 还没有 canonical 就没有"漂移"可言——但也不能假称 none 是算出来的。
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"])
        assert candidate["identity_drift"]["drift_class"] == "none"
        assert candidate["identity_drift"]["reason"] == "no_canonical_yet"

    def test_drift_during_supplemental_acceptance_does_not_block(self):
        # set_canonical=false 时那一版不会成为基准，所以不挡；等真要设
        # canonical 时走 set_canonical 的门（下面那条）。
        h = Harness()
        character_id, _ = h.seeded()
        cand = h.draft(character_id, command_id="c250",
                       expected_current_revision=1,
                       identity_features=identity(face_shape="round"),
                       view_manifest={"front": ref("art_front"),
                                      "back": ref("art_back")})
        result = h.accept(character_id, cand["candidate_id"],
                          command_id="c251", expected_current_revision=1,
                          set_canonical=False, decision_kind="policy_auto")
        assert result["became_canonical"] is False

    def test_set_canonical_refuses_policy_auto(self):
        h = Harness()
        character_id, _ = h.seeded()
        cand = h.draft(character_id, command_id="c260",
                       expected_current_revision=1,
                       identity_features=identity(face_shape="round"),
                       view_manifest={"front": ref("art_front"),
                                      "back": ref("art_back")})
        h.accept(character_id, cand["candidate_id"], command_id="c261",
                 expected_current_revision=1, set_canonical=False)
        with raises(422, "IDENTITY_DRIFT_REQUIRES_HUMAN"):
            h.set_canonical(character_id, 2, command_id="c262",
                            decision_kind="policy_auto")


# ================================================================ 指针

class TestCanonicalPointer:
    def test_accept_without_set_canonical_keeps_pointer(self):
        # E02-B2
        h = Harness()
        character_id, _ = h.seeded()
        cand = h.draft(character_id, command_id="c270",
                       expected_current_revision=1,
                       view_manifest={"front": ref("art_front"),
                                      "back": ref("art_back")})
        result = h.accept(character_id, cand["candidate_id"],
                          command_id="c271", expected_current_revision=1,
                          set_canonical=False)
        assert result["canonical_revision"] == 1
        assert h.rows("character", tenant_id="t1",
                      character_id=character_id)[0]["canonical_revision"] == 1

    def test_canonical_not_set_is_404_with_its_own_code(self):
        # §4.5 硬约束 5：不是 200 + null。
        h = Harness()
        character = h.create()
        with raises(404, "CANONICAL_NOT_SET"):
            h.service.get_canonical("t1", character["character_id"])

    def test_rollback_moves_pointer_without_new_revision(self):
        # E08-B2
        h = Harness()
        character_id, _ = h.seeded()
        cand2 = h.draft(character_id, command_id="c280",
                        expected_current_revision=1,
                        view_manifest={"front": ref("art_front"),
                                       "back": ref("art_back")})
        h.accept(character_id, cand2["candidate_id"], command_id="c281",
                 expected_current_revision=1)
        result = h.set_canonical(character_id, 1, command_id="c282")
        assert result["canonical_revision"] == 1
        assert result["prior_canonical"] == 2
        assert h.count("character_revision") == 2  # 回退不产新 revision
        pointers = h.rows("canonical_pointer", tenant_id="t1",
                          character_id=character_id)
        # C11-26：§2.5 的 PK 装不下同一 revision 的两条决定，所以 v1 的那行
        # 被重新置为有效（决定字段更新），v2 的那行留下 superseded_at。
        assert len(pointers) == 2
        assert sum(1 for p in pointers if p["superseded_at"] is None) == 1
        live = next(p for p in pointers if p["superseded_at"] is None)
        assert live["revision"] == 1
        assert live["prior_canonical"] == 2
        assert live["decided_at"] > h.rows(
            "character_revision", tenant_id="t1",
            character_id=character_id, revision=1)[0]["accepted_at"]

    def test_set_canonical_emits_no_event(self):
        # C11-3 / CRC-4：契约里没有 canonical 变更事件。
        h = Harness()
        character_id, _ = h.seeded()
        cand2 = h.draft(character_id, command_id="c290",
                        expected_current_revision=1,
                        view_manifest={"front": ref("art_front"),
                                       "back": ref("art_back")})
        h.accept(character_id, cand2["candidate_id"], command_id="c291",
                 expected_current_revision=1)
        before = len(h.accepted_events())
        h.set_canonical(character_id, 1, command_id="c292")
        assert len(h.accepted_events()) == before

    def test_set_canonical_to_same_revision_is_noop(self):
        h = Harness()
        character_id, _ = h.seeded()
        result = h.set_canonical(character_id, 1, command_id="c300")
        assert result["changed"] is False
        assert len(h.rows("canonical_pointer", tenant_id="t1",
                          character_id=character_id)) == 1

    def test_set_canonical_requires_reason_text(self):
        # C11-19：契约没有专门的码，用 400。
        h = Harness()
        character_id, _ = h.seeded()
        with raises(400, "MALFORMED_REQUEST"):
            h.set_canonical(character_id, 1, command_id="c310",
                            reason_text="")

    def test_set_canonical_to_missing_revision_is_404(self):
        h = Harness()
        character_id, _ = h.seeded()
        with raises(404, "NOT_FOUND"):
            h.set_canonical(character_id, 9, command_id="c320")

    def test_set_canonical_on_tombstoned_character_is_422(self):
        h = Harness()
        character_id, _ = h.seeded()
        h.status(character_id, "deprecated", command_id="c330")
        h.status(character_id, "tombstoned", command_id="c331")
        with raises(422, "CHARACTER_TOMBSTONED"):
            h.set_canonical(character_id, 1, command_id="c332")


# ================================================================ 状态机

class TestStateMachine:
    """§3.3：active → deprecated → tombstoned，终态不复活。"""

    def test_deprecate_then_tombstone(self):
        h = Harness()
        character_id, _ = h.seeded()
        first = h.status(character_id, "deprecated", command_id="c340")
        assert first["status"] == "deprecated"
        assert first["previous_status"] == "active"
        second = h.status(character_id, "tombstoned", command_id="c341")
        assert second["status"] == "tombstoned"

    def test_active_cannot_jump_to_tombstoned(self):
        # C11-21：§3.3 的图里没有这条边。
        h = Harness()
        character_id, _ = h.seeded()
        with raises(409, "INVALID_STATUS_TRANSITION") as info:
            h.status(character_id, "tombstoned", command_id="c350")
        assert info.value.details["allowed_from"] == ["deprecated"]

    def test_tombstoned_never_comes_back(self):
        h = Harness()
        character_id, _ = h.seeded()
        h.status(character_id, "deprecated", command_id="c360")
        h.status(character_id, "tombstoned", command_id="c361")
        with raises(409, "INVALID_STATUS_TRANSITION"):
            h.status(character_id, "deprecated", command_id="c362")
        assert h.rows("character", tenant_id="t1",
                      character_id=character_id)[0]["status"] == "tombstoned"

    def test_deprecating_twice_is_409(self):
        # 终态不回归：deprecated 不能"再降级"。
        h = Harness()
        character_id, _ = h.seeded()
        h.status(character_id, "deprecated", command_id="c370")
        with raises(409, "INVALID_STATUS_TRANSITION"):
            h.status(character_id, "deprecated", command_id="c371")

    def test_deprecated_character_still_reads(self):
        # 下线不等于删除：旧 manifest 仍要能解析（E08-B3 的同一逻辑）。
        h = Harness()
        character_id, _ = h.seeded()
        h.status(character_id, "deprecated", command_id="c380")
        out = h.service.get_revision("t1", character_id, 1)
        assert out["character_ref"]["revision"] == 1


# ================================================================ 读路径

class TestReadPaths:
    def test_revision_response_shape(self):
        # §4.5
        h = Harness()
        character_id, _ = h.seeded()
        out = h.service.get_revision("t1", character_id, 1)
        assert out["character_ref"]["service_id"] == "S11"
        assert out["is_canonical"] is True
        assert out["canonical_revision"] == 1
        assert out["rights_recheck_required"] is True
        assert out["etag"] == out["character_ref"]["content_hash"]
        assert out["parent_revision"] is None

    def test_artifact_refs_carry_no_url(self):
        # §4.5 硬约束 3 / §9 反模式 6
        h = Harness()
        character_id, _ = h.seeded()
        out = h.service.get_revision("t1", character_id, 1)
        for view, entry in out["view_manifest"].items():
            assert "url" not in entry
            assert "signed_url" not in entry
            assert entry["service_id"] == "S28"
            assert entry["id"]

    def test_caller_supplied_url_is_dropped_at_draft(self):
        # 即使调用方塞了 url，也不会被存下来。
        h = Harness()
        character = h.create()
        h.draft(character["character_id"],
                view_manifest={"front": {"artifact_id": "art_front",
                                         "url": "https://leak.example/x.png"}})
        payload = json.loads(h.rows(
            "character_candidate", tenant_id="t1",
            character_id=character["character_id"])[0]["payload"])
        assert "url" not in payload["view_manifest"]["front"]

    def test_is_canonical_always_comes_with_canonical_revision(self):
        # §4.5 硬约束 1：这两个必须同时返回。
        h = Harness()
        character_id, _ = h.seeded()
        cand2 = h.draft(character_id, command_id="c390",
                        expected_current_revision=1,
                        view_manifest={"front": ref("art_front"),
                                       "back": ref("art_back")})
        h.accept(character_id, cand2["candidate_id"], command_id="c391",
                 expected_current_revision=1, set_canonical=False)
        out = h.service.get_revision("t1", character_id, 2)
        assert out["is_canonical"] is False
        assert out["canonical_revision"] == 1

    def test_cross_tenant_read_is_indistinguishable_from_a_miss(self):
        # E28-B1 / F11：「存在但你没权限」与「根本不存在」必须**逐字段同形**，
        # 否则问一次就探测出对方租户有没有这个 id。比"响应里没有 X"更强：
        # 两条错误必须一模一样。
        h = Harness()
        character_id, _ = h.seeded()

        def error(tenant_id, target):
            try:
                h.service.get_revision(tenant_id, target, 1)
            except ServiceError as exc:
                return exc.status, exc.code, str(exc), exc.details
            raise AssertionError("expected 404")

        foreign = error("t2", character_id)
        missing = error("t2", "chr_does_not_exist")
        assert foreign[:2] == missing[:2] == (404, "NOT_FOUND")
        assert foreign[3] == missing[3] == {}
        # 错误里只出现调用方自己传进来的那个 id（它不是外来标识），
        # 不出现对方的 display_name / project_id / 任何 hash。
        assert "林岚" not in foreign[2]
        assert "prj_1" not in foreign[2]
        assert HASH_A not in foreign[2]

    def test_cross_tenant_character_read_is_404(self):
        h = Harness()
        character_id, _ = h.seeded()
        with raises(404, "NOT_FOUND"):
            h.service.get_character("t2", character_id)

    def test_old_revision_stays_readable_with_same_hash(self):
        # E08-B3
        h = Harness()
        character_id, _ = h.seeded()
        before = h.service.get_revision("t1", character_id, 1)
        cand2 = h.draft(character_id, command_id="c400",
                        expected_current_revision=1,
                        view_manifest={"front": ref("art_front"),
                                       "back": ref("art_back")})
        h.accept(character_id, cand2["candidate_id"], command_id="c401",
                 expected_current_revision=1)
        after = h.service.get_revision("t1", character_id, 1)
        assert before["character_ref"]["content_hash"] == \
            after["character_ref"]["content_hash"]
        assert after["is_canonical"] is False

    def test_list_revisions_pages_forward(self):
        h = Harness()
        character_id, _ = h.seeded()
        cand2 = h.draft(character_id, command_id="c410",
                        expected_current_revision=1,
                        view_manifest={"front": ref("art_front"),
                                       "back": ref("art_back")})
        h.accept(character_id, cand2["candidate_id"], command_id="c411",
                 expected_current_revision=1, set_canonical=False)
        page = h.service.list_revisions("t1", character_id, after=1)
        assert [item["revision"] for item in page["items"]] == [2]

    def test_get_character_reports_pointer_and_aliases(self):
        h = Harness()
        character_id, _ = h.seeded()
        h.alias(character_id, command_id="c420")
        out = h.service.get_character("t1", character_id)
        assert out["canonical"]["revision"] == 1
        assert out["aliases"][0]["alias_text"] == "林总"
        assert out["rights_recheck_required"] is True

    def test_get_canonical_returns_hash_of_pointer_target(self):
        h = Harness()
        character_id, _ = h.seeded()
        out = h.service.get_canonical("t1", character_id)
        assert out["character_ref"]["revision"] == 1
        assert len(out["character_ref"]["content_hash"]) == 64
        assert out["rights_recheck_required"] is True

    def test_drift_endpoint_compares_two_revisions(self):
        h = Harness()
        character_id, _ = h.seeded()
        cand2 = h.draft(character_id, command_id="c430",
                        expected_current_revision=1,
                        identity_features=identity(skin_tone_band="5"),
                        view_manifest={"front": ref("art_front"),
                                       "back": ref("art_back")})
        h.accept(character_id, cand2["candidate_id"], command_id="c431",
                 expected_current_revision=1, set_canonical=False)
        out = h.service.drift("t1", character_id, from_revision=1,
                              to_revision=2)
        assert out["drift_class"] == "minor"
        assert out["from_identity_feature_hash"] != \
            out["to_identity_feature_hash"]

    def test_drift_unknown_revision_is_404(self):
        h = Harness()
        character_id, _ = h.seeded()
        with raises(404, "NOT_FOUND"):
            h.service.drift("t1", character_id, from_revision=1,
                            to_revision=9)

    def test_list_candidates_filters_by_state(self):
        h = Harness()
        character = h.create()
        first = h.draft(character["character_id"], command_id="c440",
                        view_manifest={"front": ref("art_front")},
                        identity_features=identity(skin_tone_band="1"))
        h.draft(character["character_id"], command_id="c441",
                view_manifest={"front": ref("art_side")},
                identity_features=identity(skin_tone_band="2"))
        h.reject(character["character_id"], first["candidate_id"],
                 command_id="c442")
        rejected = h.service.list_candidates(
            "t1", character["character_id"], state="REJECTED")
        assert [x["candidate_id"] for x in rejected["items"]] == [
            first["candidate_id"]]
        pending = h.service.list_candidates(
            "t1", character["character_id"], state="PENDING")
        assert len(pending["items"]) == 1

    def test_list_candidates_rejects_unknown_state(self):
        h = Harness()
        character = h.create()
        with raises(400, "MALFORMED_REQUEST"):
            h.service.list_candidates("t1", character["character_id"],
                                      state="ZOMBIE")


# ================================================================ 别名

class TestAliases:
    def test_lookup_matches_display_name(self):
        h = Harness()
        character = h.create()
        out = h.service.lookup("t1", series_scope="ser_1", alias="林岚")
        assert out["character"]["character_id"] == character["character_id"]

    def test_lookup_matches_alias_text(self):
        h = Harness()
        character = h.create()
        h.alias(character["character_id"], command_id="c450")
        out = h.service.lookup("t1", series_scope="ser_1", alias="林总")
        assert out["character"]["character_id"] == character["character_id"]

    def test_ambiguous_alias_is_422_and_lists_both(self):
        # §2.1 行 130 / C11-25 / §9 反模式 9：绝不取第一个。
        h = Harness()
        first = h.create(command_id="c460")
        second = h.create(display_name="林岚(替身)", command_id="c461")
        h.alias(first["character_id"], "林总", command_id="c462")
        h.alias(second["character_id"], "林总", command_id="c463")
        with raises(422, "AMBIGUOUS_ALIAS") as info:
            h.service.lookup("t1", series_scope="ser_1", alias="林总")
        assert set(info.value.details["candidates"]) == {
            first["character_id"], second["character_id"]}

    def test_lookup_misses_are_404(self):
        h = Harness()
        h.create()
        with raises(404, "NOT_FOUND"):
            h.service.lookup("t1", series_scope="ser_1", alias="查无此人")

    def test_lookup_is_series_scoped(self):
        h = Harness()
        h.create()
        with raises(404, "NOT_FOUND"):
            h.service.lookup("t1", series_scope="ser_2", alias="林岚")

    def test_lookup_is_tenant_scoped(self):
        h = Harness()
        h.create()
        with raises(404, "NOT_FOUND"):
            h.service.lookup("t2", series_scope="ser_1", alias="林岚")

    def test_adding_the_same_alias_twice_is_idempotent(self):
        # §6.2：主键冲突视为幂等成功。
        h = Harness()
        character = h.create()
        first = h.alias(character["character_id"], command_id="c470")
        again = h.alias(character["character_id"], command_id="c471")
        assert first["created"] is True
        assert again["created"] is False
        assert h.count("character_alias") == 1

    def test_alias_on_tombstoned_character_is_422(self):
        h = Harness()
        character_id, _ = h.seeded()
        h.status(character_id, "deprecated", command_id="c480")
        h.status(character_id, "tombstoned", command_id="c481")
        with raises(422, "CHARACTER_TOMBSTONED"):
            h.alias(character_id, "旧名", command_id="c482")

    def test_alias_kind_is_validated(self):
        h = Harness()
        character = h.create()
        with raises(400, "MALFORMED_REQUEST"):
            h.alias(character["character_id"], alias_kind="nick")


# ================================================================ 权利

class TestRights:
    """§9 反模式 5 / 8：S11 只存指针，不判有效性。"""

    def test_real_person_candidate_without_rights_ref_is_422(self):
        # E30-B2
        h = Harness()
        character = h.create(identity_kind="real_person",
                             rights_ref={"rights_owner": "S02",
                                         "rights_id": "r0",
                                         "rights_revision": 1,
                                         "rights_hash": HASH_A})
        with raises(422, "RIGHTS_REF_REQUIRED") as info:
            h.draft(character["character_id"], rights_ref={})
        assert info.value.details["identity_kind"] == "real_person"
        assert h.count("character_candidate") == 0

    def test_actor_likeness_without_rights_ref_is_422(self):
        h = Harness()
        character = h.create(identity_kind="actor_likeness")
        with raises(422, "RIGHTS_REF_REQUIRED"):
            h.draft(character["character_id"], rights_ref={})

    def test_fictional_character_may_have_empty_rights_ref(self):
        # §2.1 行 117 只管真人；虚构角色允许空对象（CRC-3 的占位问题
        # 是 owner 的事，不是这里发明一个假指针的理由）。
        h = Harness()
        character = h.create(identity_kind="fictional")
        candidate = h.draft(character["character_id"], rights_ref={})
        assert candidate["state"] == "PENDING"
        data = None
        h.accept(character["character_id"], candidate["candidate_id"],
                 command_id="c490")
        data = h.accepted_events()[0]["data"]
        assert data["rights_ref"] == {}

    def test_read_path_never_asserts_rights_validity(self):
        # E30-B1：读路径只说"要复查"，不说"有效"。
        h = Harness()
        character_id, _ = h.seeded()
        for out in (h.service.get_character("t1", character_id),
                    h.service.get_revision("t1", character_id, 1),
                    h.service.get_canonical("t1", character_id)):
            assert out["rights_recheck_required"] is True
            assert "rights_valid" not in out
            assert "rights_verdict" not in out

    def test_new_revision_requires_its_own_rights_ref(self):
        # E30-B3：S11 不自动沿用上一版的权利指针——它也不"自动改"，
        # 每次接受都要一份属于这一版的值。
        h = Harness()
        character = h.create(identity_kind="real_person",
                             rights_ref={"rights_owner": "S02",
                                         "rights_id": "r1",
                                         "rights_revision": 1,
                                         "rights_hash": HASH_A})
        first = h.draft(character["character_id"], command_id="c500",
                        view_manifest={"front": ref("art_front")})
        h.accept(character["character_id"], first["candidate_id"],
                 command_id="c501")
        with raises(422, "RIGHTS_REF_REQUIRED"):
            h.draft(character["character_id"], command_id="c502",
                    expected_current_revision=1,
                    view_manifest={"front": ref("art_side")},
                    rights_ref={})


# ================================================================ QC 门

class TestQualityGate:
    def test_identity_inconsistency_is_422(self):
        # E24-B1
        h = Harness()
        character = h.create()
        with raises(422, "CANDIDATE_QC_FAILED") as info:
            h.draft(character["character_id"],
                    quality_gate={"identity_consistency": "fail"})
        assert info.value.details["identity_consistency"] == "fail"
        assert h.count("character_candidate") == 0
        assert h.count("character_revision") == 0

    def test_view_inconsistency_names_the_views(self):
        # E24-B2：必须指明哪个视图，否则人审只能肉眼找不同。
        h = Harness()
        character = h.create()
        with raises(422, "IDENTITY_VIEW_INCONSISTENT") as info:
            h.draft(character["character_id"],
                    quality_gate={"view_consistency": {
                        "views": ["back", "three_quarter"]}})
        assert info.value.details["views"] == ["back", "three_quarter"]

    def test_absent_gate_conclusion_is_empty_not_pass(self):
        # C11-11：读不到结论就记空；绝不预填 pass。
        h = Harness()
        character = h.create()
        candidate = h.draft(character["character_id"],
                            quality_gate=None)
        payload = json.loads(h.rows(
            "character_candidate", tenant_id="t1",
            candidate_id=candidate["candidate_id"])[0]["evaluation"])
        assert payload == {"quality_gate": {}}

    def test_fixed_candidate_is_a_new_candidate(self):
        # E25-B1：修复后重新起草 → 新 candidate_id，旧候选保留。
        h = Harness()
        character = h.create()
        first = h.draft(character["character_id"], command_id="c510",
                        view_manifest={"front": ref("art_front")})
        h.reject(character["character_id"], first["candidate_id"],
                 command_id="c511")
        second = h.draft(character["character_id"], command_id="c512",
                         view_manifest={"front": ref("art_side")})
        assert second["candidate_id"] != first["candidate_id"]
        assert h.count("character_candidate") == 2
        assert h.rows("character_candidate", tenant_id="t1",
                      candidate_id=first["candidate_id"])[0]["state"] == \
            "REJECTED"


# ================================================================ 隔离与兼容

class TestIsolationAndCompatibility:
    def test_episode_bindings_are_not_stored_here(self):
        # E02-B4 / E08-B1：换装、改前集都**不该**让 S11 长出新东西，而
        # "第几集用了哪一版"归 S10。这里从库结构上证明 S11 没有地方记它：
        # 没有剧集表、没有绑定表，revision 上也没有集号列。
        h = Harness()
        tables = {r["name"] for r in h.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        banned = {"episode", "episode_binding", "shot", "shot_binding",
                  "manifest", "episode_character"}
        assert not (tables & banned), tables & banned
        cols = {r["name"] for r in h.conn.execute(
            "PRAGMA table_info(character_revision)")}
        assert not (cols & {"episode", "episode_id", "episode_no",
                            "bound_at_episode"}), cols

    def test_candidate_never_appears_in_revision_reads(self):
        # §1.3：S10 只走 revision 路径，候选不在这条路径上。
        h = Harness()
        character = h.create()
        h.draft(character["character_id"])
        with raises(404, "NOT_FOUND"):
            h.service.get_revision("t1", character["character_id"], 1)
        assert h.service.list_revisions(
            "t1", character["character_id"])["items"] == []

    def test_old_payload_reads_with_new_fields_present(self):
        # E01-B1 / E01-B2：旧 revision 的 payload 里多出新字段时，
        # 读路径原样返回，不报错也不丢。
        h = Harness()
        character_id, _ = h.seeded()
        # 模拟"上一版写入时还没有这个字段、现在有人加上了"：读路径必须
        # 原样返回，既不能报错，也不能把它静默丢掉。
        future = identity(future_field={"a": 1})
        h.conn.execute(
            "UPDATE character_revision SET identity_features=?, payload=?"
            " WHERE tenant_id='t1' AND character_id=? AND revision=1",
            (json.dumps(future),
             json.dumps({"view_manifest": views(),
                         "identity_features": future,
                         "body_profile": body_profile(),
                         "rights_ref": {},
                         "lora_refs": [], "source_kind": "generated",
                         "parent_revision": None,
                         "quality_gate_ref": {"id": "qg_1"},
                         "expression_manifest": {}}), character_id))
        out = h.service.get_revision("t1", character_id, 1)
        assert out["identity_features"]["future_field"] == {"a": 1}
        # 旧行里存的 hash 是接受当时算的，读路径不重算它（重算就等于改写历史）。
        # 未知字段**参与**下一次漂移判定，且保守判 major（C11-23）。
        assert classify_drift(identity(), future)["drift_class"] == "major"

    def test_s11_reads_no_bytes_from_s28(self):
        # E29-B1：只读 manifest 摘要，不解析字节。
        h = Harness()
        character = h.create()
        h.draft(character["character_id"])
        # stub 返回的就是"摘要"：没有 bytes、没有 body、没有 url。
        assert set(h.artifacts["art_front"]) == {"artifact_id",
                                                 "content_hash"}

    def test_payload_and_columns_come_from_the_same_object(self):
        # §2.2 行 166：写入时来自同一份对象，且有测试断言
        # sha256(JCS(payload)) == content_hash。
        h = Harness()
        character_id, _ = h.seeded()
        row = h.rows("character_revision", tenant_id="t1",
                     character_id=character_id, revision=1)[0]
        payload = json.loads(row["payload"])
        assert content_hash(payload) == row["content_hash"]
        assert identity_feature_hash(payload["identity_features"]) == \
            row["identity_feature_hash"]
        assert json.loads(row["identity_features"]) == \
            payload["identity_features"]

    def test_view_manifest_column_matches_payload(self):
        h = Harness()
        character_id, _ = h.seeded()
        row = h.rows("character_revision", tenant_id="t1",
                     character_id=character_id, revision=1)[0]
        payload = json.loads(row["payload"])
        assert json.loads(row["view_manifest"]) == payload["view_manifest"]


# ================================================================ HTTP 层

class TestHttpLayer:
    def test_create_and_read(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        response = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"},
            headers=_headers("c1"))
        assert response.status_code == 201
        character_id = response.json()["character_id"]
        read = client.get(f"/characters/{character_id}",
                          headers={"X-Tenant-Id": "t1"})
        assert read.status_code == 200
        assert read.json()["character"]["character_id"] == character_id

    def test_missing_idempotency_key_is_400(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        response = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"},
            headers={"X-Tenant-Id": "t1"})
        assert response.status_code == 400
        assert response.json()["code"] == "MALFORMED_REQUEST"

    def test_missing_tenant_header_is_400(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        response = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"},
            headers={"Idempotency-Key": "c1"})
        assert response.status_code == 400
        assert response.json()["code"] == "MALFORMED_REQUEST"

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        # 跨租户**请求**：凭据与 body 不一致 → 403，且不落任何东西。
        client, h = _http_client(monkeypatch, tmp_path)
        response = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional",
            "tenant_id": "t2"}, headers=_headers("c1"))
        assert response.status_code == 403
        assert response.json()["code"] == "TENANT_SCOPE_DENIED"
        assert h.count("character") == 0

    def test_body_command_id_mismatch_is_400(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        response = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional",
            "command_id": "c2"}, headers=_headers("c1"))
        assert response.status_code == 400

    def test_idempotent_replay_over_http(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        payload = {"project_id": "prj_1", "series_scope": "ser_1",
                   "display_name": "林岚", "identity_kind": "fictional"}
        first = client.post("/characters", json=payload, headers=_headers("c1"))
        again = client.post("/characters", json=payload, headers=_headers("c1"))
        assert first.json()["replayed"] is False
        assert again.json()["replayed"] is True
        assert first.json()["character_id"] == again.json()["character_id"]

    def test_same_key_different_body_over_http_is_409(self, monkeypatch,
                                                     tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"},
            headers=_headers("c1"))
        response = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "别人", "identity_kind": "fictional"},
            headers=_headers("c1"))
        assert response.status_code == 409
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_full_flow_over_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"},
            headers=_headers("c1")).json()
        character_id = created["character_id"]
        drafted = client.post(
            f"/characters/{character_id}/candidates",
            json=candidate_body(), headers=_headers("c2")).json()
        accepted = client.post(
            f"/characters/{character_id}/candidates/"
            f"{drafted['candidate_id']}:accept",
            json={"expected_current_revision": 0, "set_canonical": True},
            headers=_headers("c3"))
        assert accepted.status_code == 200
        assert accepted.json()["revision"] == 1
        revision = client.get(f"/characters/{character_id}/revisions/1",
                              headers={"X-Tenant-Id": "t1"})
        assert revision.status_code == 200
        # §6.4：ETag = content_hash
        assert revision.headers["ETag"] == \
            revision.json()["character_ref"]["content_hash"]
        canonical = client.get(f"/characters/{character_id}/canonical",
                               headers={"X-Tenant-Id": "t1"})
        assert canonical.status_code == 200
        assert canonical.json()["character_ref"]["revision"] == 1

    def test_canonical_not_set_over_http_is_404(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        created = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"},
            headers=_headers("c1")).json()
        response = client.get(
            f"/characters/{created['character_id']}/canonical",
            headers={"X-Tenant-Id": "t1"})
        assert response.status_code == 404
        assert response.json()["code"] == "CANONICAL_NOT_SET"

    def test_cross_tenant_over_http_is_indistinguishable(self, monkeypatch,
                                                         tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        created = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"},
            headers=_headers("c1")).json()
        foreign = client.get(f"/characters/{created['character_id']}",
                             headers={"X-Tenant-Id": "t2"})
        missing = client.get("/characters/chr_does_not_exist",
                             headers={"X-Tenant-Id": "t2"})
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json()["code"] == missing.json()["code"] == "NOT_FOUND"
        # 只差调用方自己传进来的 id，其余逐字段相同。
        assert foreign.text.replace(created["character_id"], "X") == \
            missing.text.replace("chr_does_not_exist", "X")
        assert "林岚" not in foreign.text

    def test_service_error_maps_to_status_and_code(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        created = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"},
            headers=_headers("c1")).json()
        response = client.post(
            f"/characters/{created['character_id']}/candidates",
            json=candidate_body(view_manifest={"front": ref("art_ghost")}),
            headers=_headers("c2"))
        assert response.status_code == 422
        assert response.json()["code"] == "ARTIFACT_NOT_FOUND"

    def test_lookup_over_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"},
            headers=_headers("c1")).json()
        response = client.get("/characters:lookup",
                              params={"series_scope": "ser_1",
                                      "alias": "林岚"},
                              headers={"X-Tenant-Id": "t1"})
        assert response.status_code == 200
        assert response.json()["character"]["character_id"] == \
            created["character_id"]

    def test_drift_route_accepts_from_to_query_names(self, monkeypatch,
                                                     tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"},
            headers=_headers("c1")).json()
        character_id = created["character_id"]
        drafted = client.post(
            f"/characters/{character_id}/candidates",
            json=candidate_body(), headers=_headers("c2")).json()
        client.post(
            f"/characters/{character_id}/candidates/"
            f"{drafted['candidate_id']}:accept",
            json={"expected_current_revision": 0, "set_canonical": True},
            headers=_headers("c3"))
        response = client.get(f"/characters/{character_id}/drift",
                              params={"from": 1, "to": 1},
                              headers={"X-Tenant-Id": "t1"})
        assert response.status_code == 200
        assert response.json()["drift_class"] == "none"

    def test_merge_route_does_not_exist(self, monkeypatch, tmp_path):
        # C11-14
        client, _ = _http_client(monkeypatch, tmp_path)
        response = client.post("/characters/chr_x:merge", json={},
                               headers=_headers("c1"))
        assert response.status_code in (404, 405)

    def test_reset_route_does_not_exist(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        response = client.post("/characters/chr_x:reset", json={},
                               headers=_headers("c1"))
        assert response.status_code in (404, 405)

    def test_in_flight_maps_to_retry_after(self, monkeypatch, tmp_path):
        # COMMAND_IN_FLIGHT 只在 HTTP 层产生（SDK 抛 CommandInProgress）。
        client, h = _http_client(monkeypatch, tmp_path)
        body = {"project_id": "prj_1", "series_scope": "ser_1",
                "display_name": "林岚", "identity_kind": "fictional"}
        h.idem.begin("t1", "c1", canonical_request_hash(
            {**body, "command_id": "c1"}))
        response = client.post("/characters", json=body, headers=_headers("c1"))
        assert response.status_code == 409
        assert response.json()["code"] == "COMMAND_IN_FLIGHT"
        assert response.json()["retryable"] is True
        assert response.headers["Retry-After"] == "1"
        assert h.count("character") == 0

    def test_readiness_reports_contract_gaps(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        response = client.get("/health/readiness")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert len(body["contract_gaps"]) == len(CONTRACT_GAPS)

    def test_list_revisions_over_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = client.post("/characters", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "林岚", "identity_kind": "fictional"},
            headers=_headers("c1")).json()
        response = client.get(
            f"/characters/{created['character_id']}/revisions",
            headers={"X-Tenant-Id": "t1"})
        assert response.status_code == 200
        assert response.json()["items"] == []


# ================================================================ 契约缺口

class TestContractGaps:
    def test_entries_have_the_required_shape(self):
        for gap in CONTRACT_GAPS:
            assert set(gap) == {"id", "kind", "summary", "handling"}, gap
            assert gap["kind"] in {"conflict", "needs_owner", "gap"}, gap
            assert gap["summary"].strip()
            assert gap["handling"].strip()

    def test_ids_are_unique_and_ordered(self):
        ids = [gap["id"] for gap in CONTRACT_GAPS]
        assert len(ids) == len(set(ids))
        assert ids[:8] == [f"C11-{index}" for index in range(1, 9)]

    def test_no_owner_decision_is_filled_in(self):
        # 用户明令：need_owner 一律不许代答。handling 只能写"待/未定/需"。
        for gap in CONTRACT_GAPS:
            if gap["kind"] == "needs_owner":
                assert any(word in gap["handling"]
                           for word in ("待", "未定", "需")), gap

    def test_every_gap_names_a_real_plan_section(self):
        for gap in CONTRACT_GAPS:
            assert "§" in gap["summary"] or "§" in gap["handling"], gap

    def test_missing_topics_are_registered(self):
        # C11-4：方案 §5.3 的消费表点名 artifact.archived 与
        # training.lora.registered，topics 里都没有——所以不实现消费者。
        ids = {gap["id"] for gap in CONTRACT_GAPS}
        assert "C11-4" in ids

    def test_merge_is_registered_as_not_implemented(self):
        gap = next(g for g in CONTRACT_GAPS if g["id"] == "C11-14")
        assert ":merge" in gap["summary"] or "merge" in gap["summary"]
        assert "不实现" in gap["handling"] or "未实现" in gap["handling"]

    def test_s01_is_registered_as_unused(self):
        gap = next(g for g in CONTRACT_GAPS if g["id"] == "C11-18")
        assert "S01" in gap["summary"]


# ================================================================ 边界常量

def test_module_constants_match_the_plan():
    from character_assets.service import (
        CANDIDATE_STATES as states, DECISION_KINDS, IDENTITY_KINDS,
        RIGHTED_IDENTITY_KINDS, SOURCE_KINDS, STATUSES)
    assert STATUSES == ("active", "deprecated", "tombstoned")
    assert IDENTITY_KINDS == ("fictional", "actor_likeness", "real_person")
    assert RIGHTED_IDENTITY_KINDS == ("actor_likeness", "real_person")
    assert SOURCE_KINDS == ("manual_upload", "generated", "imported",
                            "derived")
    assert DECISION_KINDS == ("human_approved", "policy_auto",
                              "initial_import")
    assert states == ("PENDING", "ACCEPTED", "REJECTED", "EXPIRED")
    assert ALL_VIEWS == ("front", "side_left", "back", "three_quarter")
    assert CANDIDATE_STATES == states


def test_schema_string_is_the_only_source_of_ddl():
    # 库里有的表必须都在 SCHEMA 里出现过，反之亦然。
    h = Harness()
    names = {r["name"] for r in h.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for table in ("character", "character_alias", "character_revision",
                  "character_candidate", "canonical_pointer",
                  "revision_reference"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SCHEMA, table
        assert table in names, table
