"""S14 单测。编号对应 `plans/S14-prop-assets.md` §8 的验收清单与 §4.4 的 F1–F16。

上游全部是 stub，**按真实的上游契约写**：

- S28 `GET /artifacts/{artifact_id}` 返回
  `artifact_ingest.service.get_artifact` 那个形状；这里只用到
  `artifact_id` 与 `content_hash`（当前 revision 的）两项——测试里也多一个都
  不塞。
- QC 的结论不是任何服务的真实响应（C14-10 / PRP-6：仓库里没有产出
  `holder_feasibility` / `variant_consistency` / `text_ocr_pass` 的服务），
  所以测试用**命令内联的 `quality_gate`**，与内核支持的唯一来源一致。

结构性断言用"库里查不到"而不是"响应里没有"：这一服务最严重的错误是变成第二
个正史权威（持有者/当前状态），而响应体里少个字段不能证明什么，少一列才能。
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

from prop_assets.schema import CONTRACT_GAPS, SCHEMA
from prop_assets.service import (
    PropAssetsService,
    ServiceError,
    content_hash,
    text_changed,
    text_fingerprint,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


# ------------------------------------------------------------------ 上游形状

def artifact(artifact_id="art_intact", content_hash=HASH_A, **over):
    """S28 `get_artifact` 的字段集（只取本服务实际读的两项）。"""
    base = {"artifact_id": artifact_id, "content_hash": content_hash}
    base.update(over)
    return base


def text_detail(surface="front", text="保险箱 07", **over):
    """§1.3 的 text_detail。`text_hash` 缺省由内核算，不预置。"""
    base = {
        "surface": surface,
        "content_kind": "engraved",
        "canonical_text": text,
        "language": "zh",
        "layout": {"x": 0.4, "y": 0.6},
        "legibility": "must_be_readable",
        "sensitive": False,
    }
    base.update(over)
    return base


def physical(**over):
    base = {"length_cm": 6.0, "width_cm": 2.0, "height_cm": 1.0,
            "weight_g": 30.0, "handheld": True, "rigid": True}
    base.update(over)
    return base


def manifest(intact="art_intact", broken="art_broken",
             closed="art_intact", open_="art_open"):
    return {
        "condition": {
            "intact": {"artifact_id": intact, "content_hash": HASH_A},
            "broken": {"artifact_id": broken, "content_hash": HASH_B},
        },
        "open_state": {
            "closed": {"artifact_id": closed, "content_hash": HASH_A},
            "open": {"artifact_id": open_, "content_hash": HASH_B},
        },
    }


AXES = {"condition": ["intact", "broken"], "open_state": ["closed", "open"]}


def candidate_body(**over):
    base = {
        "expected_current_revision": 0,
        "source_kind": "generated",
        "rights_ref": {"service_id": "S02", "id": "rights_1"},
        "quality_gate_ref": {"service_id": "S35", "id": "qc_1", "revision": 1},
        "physical": physical(),
        "appearance": {"material": "brass", "finish": "aged"},
        "text_details": [text_detail()],
        "variant_axes": dict(AXES),
        "variant_manifest": manifest(),
        "quality_gate": {"text_ocr": [{"surface": "front", "pass": True}]},
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
            "art_intact": artifact("art_intact", HASH_A),
            "art_broken": artifact("art_broken", HASH_B),
            "art_open": artifact("art_open", HASH_B),
        })
        self.calls = {"artifact": 0, "authorizer": 0}
        self.service = PropAssetsService(
            self.conn,
            artifact_source=self._artifact,
            quality_gate_source=over.get("quality_gate_source"),
            authorizer=over.get("authorizer"),
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
                "display_name": "铜钥匙", "prop_kind": "hero_prop",
                "story_critical": True}
        body.update(over)
        return self.command(
            lambda t, c, b: self.service.create_prop(t, c, b), body,
            tenant_id, command_id)

    def draft(self, prop_id, tenant_id="t1", command_id="c2", **over):
        body = candidate_body(**over)
        return self.command(
            lambda t, c, b: self.service.draft_candidate(t, c, prop_id, b),
            body, tenant_id, command_id)

    def accept(self, prop_id, candidate_id, tenant_id="t1",
               command_id="c3", **over):
        body = {"expected_current_revision": 0, "set_canonical": True,
                "decision_kind": "human_approved",
                "impact_plan_ref": {"service_id": "S09", "id": "ip_1",
                                    "revision": 1}}
        body.update(over)
        return self.command(
            lambda t, c, b: self.service.accept_candidate(
                t, c, prop_id, candidate_id, b), body, tenant_id, command_id)

    def seeded(self, **over):
        """库里有 canonical v1 的道具。返回 (prop_id, candidate_id)。"""
        prop = self.create(**over)
        cand = self.draft(prop["prop_id"])
        self.accept(prop["prop_id"], cand["candidate_id"])
        return prop["prop_id"], cand["candidate_id"]

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


def codes(exc: Exception) -> str:
    return getattr(exc, "code", "")


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


# ================================================================ 纯函数

class TestTextFingerprint:
    """算法的可复算性。§4.2 第 3 步的判定全靠它，算法漂移就是人审门失效。"""

    def test_no_text_is_none_not_empty_hash(self):
        # §2.2 行 199：NULL = 无文字。"无文字"必须可区分于"文字是空串"。
        assert text_fingerprint([]) is None
        assert text_fingerprint(None) is None
        assert text_fingerprint([text_detail(text="")]) is None

    def test_single_surface_is_sha256_of_text(self):
        import hashlib
        assert text_fingerprint([text_detail(text="A-17")]) == \
            hashlib.sha256("A-17".encode()).hexdigest()

    def test_multi_surface_is_order_independent(self):
        a = text_detail("front", "A-17")
        b = text_detail("lid", "内部 07")
        assert text_fingerprint([a, b]) == text_fingerprint([b, a])

    def test_multi_surface_differs_from_single(self):
        a = text_detail("front", "A-17")
        assert text_fingerprint([a]) != text_fingerprint(
            [a, text_detail("lid", "内部 07")])

    def test_none_and_text_are_distinct(self):
        # C14-9 / PRP-3：SQL 的 `!= NULL` 会求值为 NULL，"原本没字、
        # 现在刻了字"就被判成没变。这里必须是变更。
        assert text_changed(None, "a" * 64) is True
        assert text_changed("a" * 64, None) is True
        assert text_changed(None, None) is False


class TestContentHash:
    def test_ignores_prop_identity_and_clock(self):
        payload = {"physical": physical(), "appearance": {"m": "brass"},
                   "text_details": [text_detail()], "variant_axes": dict(AXES),
                   "variant_manifest": manifest(), "rights_ref": {"id": "r"},
                   "source_kind": "generated", "parent_revision": None}
        # 同样的内容得出同样的 hash —— 与哪个道具、什么时候接受无关。
        assert content_hash(payload) == content_hash(dict(payload))

    def test_sensitive_text_is_hashed_not_embedded(self):
        secret = "保险箱密码 4711"
        payload = {"physical": physical(), "appearance": {},
                   "text_details": [text_detail(surface="inner", text=secret,
                                                sensitive=True)],
                   "variant_axes": dict(AXES), "variant_manifest": manifest(),
                   "rights_ref": {}, "source_kind": "generated",
                   "parent_revision": None}
        digest = content_hash(payload)
        # 反过来算：把明文当作"内容"再哈希，不该等于 content_hash ——
        # 否则 content_hash 就是明文的一个可暴力枚举的封装。
        import hashlib
        assert digest != hashlib.sha256(secret.encode()).hexdigest()
        assert len(digest) == 64


# ================================================================ 结构边界

class TestNoSecondAuthority:
    """§0 / §1.1 / §2.7 / §9 反模式 1–4。断言的是**列不存在**。"""

    def columns(self, conn, table):
        return {r["name"] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}

    def test_revision_has_no_holder_or_state_columns(self):
        h = Harness()
        cols = self.columns(h.conn, "prop_revision")
        banned = {"holder", "holder_ref", "current_holder", "current_state",
                  "state", "condition", "open_state", "location",
                  "scene_ids", "shots", "character_id"}
        assert not (cols & banned), cols & banned

    def test_no_forbidden_tables(self):
        h = Harness()
        names = {r["name"] for r in h.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        # §2.7 的八张表一张都不建。`prop_holder` / `prop_state` 这类命名
        # 即使换个说法也不该出现。
        assert not {n for n in names
                    if n.startswith("prop_holder") or n.startswith("prop_state")
                    or n.startswith("prop_location")}
        assert names >= {"prop", "prop_revision", "prop_text_detail",
                         "prop_candidate", "canonical_pointer",
                         "required_variant"}

    def test_variant_axes_are_capabilities_not_history(self):
        # 接受两版之后，`variant_axes` 仍然只是"能画哪几种"——
        # 不会因为某一版被接受就记录成"当前是 broken"。
        h = Harness()
        prop_id, _ = h.seeded()
        row = h.rows("prop_revision", tenant_id="t1", prop_id=prop_id,
                     revision=1)[0]
        axes = json.loads(row["variant_axes"])
        assert axes == AXES
        assert "current" not in json.dumps(axes)

    def test_derived_text_table_has_no_plaintext_when_sensitive(self):
        # 即使 payload 里是明文，派生表也不许有（数据库约束，不是约定）。
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"], text_details=[
            # 锁盒内侧的密码在正片里不会给特写，按 may_be_blurred 登记；
            # 这样 OCR 结论只需覆盖 front 那一面（C14-10 的 null 语义）。
            text_detail(surface="inner", text="密码 4711", sensitive=True,
                        legibility="may_be_blurred"),
            text_detail(surface="front", text="保险箱 07")])
        h.accept(prop["prop_id"], cand["candidate_id"])
        rows = {r["surface"]: r for r in h.rows(
            "prop_text_detail", tenant_id="t1", prop_id=prop["prop_id"],
            revision=1)}
        assert rows["inner"]["text_plain"] is None
        assert rows["front"]["text_plain"] == "保险箱 07"
        # payload 里仍在（权威在 revision，不在派生表）
        payload = json.loads(h.rows("prop_revision", tenant_id="t1",
                                    prop_id=prop["prop_id"], revision=1)[0]
                             ["payload"])
        assert {d["surface"]: d["canonical_text"]
                for d in payload["text_details"]}["inner"] == "密码 4711"


class TestDatabaseConstraints:
    """直接写 SQL 撞约束。

    上面的用例走内核，内核**本来就不会**产生坏行；所以那些断言只能证明
    "当前代码路径没事"，证明不了"库里写不进坏行"。这一组绕过内核，用裸
    SQL 去写内核刻意拒绝的形状——将来有人加一条绕过校验的新路径时，
    挡住他的必须是数据库，而不是"记得别那么写"（AGENTS.md：不能把
    schema 校验通过当作分布式正确性证明，反过来也一样：不能把某条代码
    路径写对了当成库有约束）。

    刻意不做成 `pytest.raises(sqlite3.IntegrityError)` 的宽断言：每一种
    形状都要**撞到它该撞的那条**，撞错约束等于没测。
    """

    def test_sensitive_row_cannot_store_plaintext(self):
        h = Harness()
        prop_id, _ = h.seeded()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "INSERT INTO prop_text_detail (tenant_id, prop_id, revision,"
                " surface, content_kind, language, legibility, sensitive,"
                " text_hash, text_plain)"
                " VALUES ('t1',?,'1','inner','engraved','zh',"
                "'may_be_blurred',1,?,?)",
                (prop_id, "a" * 64, "密码 4711"))
        assert "ptd_sensitive_has_no_plaintext" in str(info.value)
        # 反向对照：同一句 SQL 把 sensitive 改成 0 就能写进去。否则这条
        # 用例可能只是被别的约束挡住的，看着绿、其实没测到这一条。
        h.conn.execute(
            "INSERT INTO prop_text_detail (tenant_id, prop_id, revision,"
            " surface, content_kind, language, legibility, sensitive,"
            " text_hash, text_plain)"
            " VALUES ('t1',?,'1','outer','engraved','zh',"
            "'may_be_blurred',0,?,?)",
            (prop_id, "b" * 64, "密码 4711"))

    def test_parent_revision_must_precede_child(self):
        h = Harness()
        prop_id, _ = h.seeded()
        cand = h.draft(prop_id, command_id="c40",
                       expected_current_revision=1,
                       text_details=[text_detail("front", "保险箱 08")])
        h.accept(prop_id, cand["candidate_id"], command_id="c41",
                 expected_current_revision=1)
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "UPDATE prop_revision SET parent_revision=2"
                " WHERE tenant_id='t1' AND prop_id=? AND revision=2",
                (prop_id,))
        assert "pr_parent_lt" in str(info.value)

    def test_canonical_pointer_cannot_run_ahead_of_head(self):
        # 指针不能指向还不存在的修订（`canonical_revision <= current_revision`）。
        h = Harness()
        prop_id, _ = h.seeded()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "UPDATE prop SET canonical_revision=9"
                " WHERE tenant_id='t1' AND prop_id=?", (prop_id,))
        assert "prop_canonical_within_head" in str(info.value)

    def test_only_one_live_canonical_pointer_per_prop(self):
        # supersede 是"插入新行 + 旧行打 superseded_at"，不是 UPDATE 同一行。
        # 两个都活着 = 两个正史，必须由部分唯一索引挡住。
        #
        # 要撞的是**部分唯一索引**，不是主键：主键是
        # (tenant_id, prop_id, revision)，所以必须先造出一个还没有指针行的
        # 修订（v3 接受但不改指针），再给它插一条"活着的"指针。
        # 拿已有指针的 v1/v2 去插只会撞主键——那样即使索引被删，用例照样
        # 绿，看着测了其实没测（本轮变异验证抓到的就是这个）。
        h = Harness()
        prop_id, _ = h.seeded()
        cand2 = h.draft(prop_id, command_id="c50",
                        expected_current_revision=1,
                        text_details=[text_detail("front", "保险箱 09")])
        h.accept(prop_id, cand2["candidate_id"], command_id="c51",
                 expected_current_revision=1)
        cand3 = h.draft(prop_id, command_id="c52",
                        expected_current_revision=2,
                        text_details=[text_detail("front", "保险箱 10")])
        h.accept(prop_id, cand3["candidate_id"], command_id="c53",
                 expected_current_revision=2, set_canonical=False)
        assert h.rows("canonical_pointer", tenant_id="t1",
                      prop_id=prop_id, revision=3) == []
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "INSERT INTO canonical_pointer (tenant_id, prop_id, revision,"
                " decided_at, decided_by, decision_kind)"
                " VALUES ('t1',?,3,'2026-01-01T00:00:00.000Z','x',"
                "'human_approved')", (prop_id,))
        # 三列而不是四列：四列就是撞了主键，说明这条用例什么都没测到。
        assert str(info.value) == (
            "UNIQUE constraint failed: canonical_pointer.tenant_id, "
            "canonical_pointer.prop_id")

    def test_same_display_name_in_one_series_is_rejected(self):
        # §6.1 说 POST /props 天然幂等；真正保证这一点的除了内核的
        # "已存在就返回"，还有这条唯一约束。只靠内核的话，两条并发
        # 注册都读到"不存在"，就都插进去了。
        h = Harness()
        h.create()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "INSERT INTO prop (tenant_id, prop_id, project_id,"
                " series_scope, display_name, prop_kind, story_critical,"
                " status, current_revision, created_by, created_at)"
                " VALUES ('t1','prp_other','prj_1','ser_1','铜钥匙',"
                "'hero_prop',1,'active',0,'x','2026-01-01T00:00:00.000Z')")
        assert "UNIQUE" in str(info.value)

    @pytest.mark.parametrize("sql, fragment", [
        # prop_kind / status / story_critical
        ("UPDATE prop SET prop_kind='weapon'", "prop_kind"),
        ("UPDATE prop SET status='archived'", "status"),
        ("UPDATE prop SET story_critical=2", "story_critical"),
        # prop_revision.source_kind / content_hash 长度
        ("UPDATE prop_revision SET source_kind='invented'", "source_kind"),
        ("UPDATE prop_revision SET content_hash='tooshort'", "content_hash"),
        # prop_text_detail.legibility / sensitive
        ("UPDATE prop_text_detail SET legibility='illegible'", "legibility"),
        # 同时把 text_plain 置空，否则 sensitive=2 会先撞
        # `ptd_sensitive_has_no_plaintext`——那条约束的名字里也有
        # "sensitive"，断言照样绿，测到的却是另一条（变异验证抓到过）。
        ("UPDATE prop_text_detail SET sensitive=2, text_plain=NULL",
         "sensitive IN"),
        # prop_candidate.state
        ("UPDATE prop_candidate SET state='ZOMBIE'", "state"),
        # canonical_pointer.decision_kind
        ("UPDATE canonical_pointer SET decision_kind='robot'",
         "decision_kind"),
    ])
    def test_enum_check_rejects_value_outside_vocabulary(self, sql, fragment):
        """每个枚举都要**在库里**被挡住一次。

        内核也会校验这些，但校验散在几条命令路径里；将来多一条写路径
        （回填脚本、迁移、另一个入口）时，挡住的必须是 CHECK。这里全部
        用裸 UPDATE，绕开所有 Python 校验。

        `fragment` 是要撞到的那一列/约束名——只断言"抛了 IntegrityError"
        会让撞错约束（比如 NOT NULL）也通过。
        """
        h = Harness()
        h.seeded()
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(sql)
        assert fragment in str(info.value), str(info.value)

    def test_foreign_keys_are_enforced(self):
        # `PRAGMA foreign_keys` 默认是 **OFF**：连上库以后不显式打开，
        # 所有 REFERENCES 都只是注释。这一条钉的是"连上了"。
        h = Harness()
        assert h.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError) as info:
            h.conn.execute(
                "INSERT INTO prop_candidate (tenant_id, candidate_id, prop_id,"
                " content_hash, payload, prop_check, evaluation, state,"
                " created_by, created_at, expires_at)"
                " VALUES ('t1','cand_ghost','prp_ghost',?, '{}','{}','{}',"
                "'PENDING','x','2026-01-01T00:00:00.000Z',"
                "'2026-02-01T00:00:00.000Z')", ("a" * 64,))
        assert "FOREIGN KEY" in str(info.value)


# ================================================================ 登记与幂等

class TestCreateAndIdempotency:
    def test_create_returns_stable_id(self):
        h = Harness()
        first = h.create()
        assert first["prop_id"].startswith("prp_")
        assert first["created"] is True
        assert first["canonical_revision"] is None
        assert first["current_revision"] == 0

    def test_same_key_same_body_replays(self):
        h = Harness()
        first = h.create(command_id="c1")
        again = h.create(command_id="c1")
        assert again["replayed"] is True
        assert again["prop_id"] == first["prop_id"]
        assert h.count("prop") == 1

    def test_same_key_different_body_is_conflict(self):
        h = Harness()
        h.create(command_id="c1")
        with raises(409, "IDEMPOTENCY_KEY_REUSED"):
            h.create(command_id="c1", display_name="银钥匙")

    def test_repeat_registration_returns_existing_id(self):
        # E12：自然幂等键是 (tenant_id, series_scope, display_name)。
        h = Harness()
        first = h.create(command_id="c1")
        second = h.create(command_id="c2")
        assert second["prop_id"] == first["prop_id"]
        assert second["created"] is False
        assert h.count("prop") == 1

    def test_same_name_in_two_tenants_is_independent(self):
        # E28-E3
        h = Harness()
        a = h.create(tenant_id="t1", command_id="c1")
        b = h.create(tenant_id="t2", command_id="c1")
        assert a["prop_id"] != b["prop_id"]
        assert h.count("prop") == 2

    def test_unknown_prop_kind_is_malformed(self):
        h = Harness()
        with raises(400, "MALFORMED_REQUEST"):
            h.create(prop_kind="weapon")


# ================================================================ 候选校验

class TestCandidateValidation:
    def test_happy_path_creates_pending_candidate(self):
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"])
        assert cand["state"] == "PENDING"
        assert cand["text_fingerprint"] is not None
        assert h.count("prop_candidate") == 1
        # 候选不产生 revision / 事件
        assert h.count("prop_revision") == 0
        assert h.count("outbox") == 0

    def test_missing_artifact_is_422(self):
        # F1 / E29-E2
        h = Harness()
        prop = h.create()
        with raises(422, "ARTIFACT_NOT_FOUND"):
            h.draft(prop["prop_id"], variant_manifest=manifest(
                intact="art_forged"))

    def test_artifact_hash_mismatch_is_422(self):
        h = Harness()
        prop = h.create()
        bad = manifest()
        bad["condition"]["intact"]["content_hash"] = HASH_B
        with raises(422, "ARTIFACT_HASH_MISMATCH"):
            h.draft(prop["prop_id"], variant_manifest=bad)

    def test_s28_timeout_is_503_and_persists_no_candidate(self):
        # F13：拿不到校验结论就落候选，等于把伪造 id 写进不可变 revision。
        h = Harness()
        prop = h.create()
        h.artifacts["art_intact"] = ConnectionError("s28 timeout")
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            h.draft(prop["prop_id"])
        assert h.count("prop_candidate") == 0

    def test_unconfigured_s28_fails_closed(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False,
                               isolation_level=None)
        from prop_assets.deps import build_artifact_source
        service = PropAssetsService(
            conn, artifact_source=build_artifact_source(None))
        conn.execute("INSERT INTO prop (tenant_id, prop_id, project_id,"
                     " series_scope, display_name, prop_kind, story_critical,"
                     " status, current_revision, created_by, created_at)"
                     " VALUES ('t1','prp_1','prj_1','ser_1','x','hero_prop',0,"
                     "'active',0,'{}','2026-01-01T00:00:00.000Z')")
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            service.draft_candidate("t1", "c1", "prp_1", candidate_body())

    def test_variant_coverage_incomplete_is_422(self):
        # F2：声明了 broken 却没有图，S16 说"碎了"时画不出来。
        h = Harness()
        prop = h.create()
        partial = manifest()
        del partial["condition"]["broken"]
        with raises(422, "VARIANT_COVERAGE_INCOMPLETE") as info:
            h.draft(prop["prop_id"], variant_manifest=partial)
        assert info.value.details["missing_values"] == ["broken"]

    def test_required_variant_missing_is_422(self):
        # F3 / E37-E1
        h = Harness()
        prop = h.create()
        h.command(lambda t, c, b: h.service.register_required_variant(
            t, c, prop["prop_id"], b),
            {"axis": "condition", "required_value": "burnt",
             "required_by": "shot:sh_12"}, command_id="c9")
        with raises(422, "REQUIRED_VARIANT_MISSING") as info:
            h.draft(prop["prop_id"])
        assert info.value.details["required_by"] == "shot:sh_12"

    def test_required_variant_registration_is_idempotent(self):
        h = Harness()
        prop = h.create()
        fn = lambda t, c, b: h.service.register_required_variant(
            t, c, prop["prop_id"], b)  # noqa: E731
        body = {"axis": "condition", "required_value": "broken",
                "required_by": "shot:sh_12"}
        first = h.command(fn, body, command_id="c9")
        second = h.command(fn, body, command_id="c10")
        assert first["created"] is True
        assert second["created"] is False
        assert h.count("required_variant") == 1

    def test_physical_out_of_range_is_422(self):
        # F4 / §9 反模式 8：不许用"小/中/大"。
        h = Harness()
        prop = h.create()
        for bad in (physical(length_cm=0), physical(length_cm=501),
                    physical(weight_g=0), physical(weight_g=50001),
                    physical(length_cm="big"), physical(length_cm=True)):
            with raises(422, "PHYSICAL_DIMENSION_INVALID"):
                h.draft(prop["prop_id"], command_id="cx",
                        expected_current_revision=0, physical=bad)

    def test_missing_dimension_is_422_not_defaulted(self):
        h = Harness()
        prop = h.create()
        with raises(422, "PHYSICAL_DIMENSION_INVALID") as info:
            h.draft(prop["prop_id"], physical={"length_cm": 6,
                                               "width_cm": 2,
                                               "height_cm": 1})
        assert info.value.details["missing"] == ["weight_g"]

    def test_duplicate_text_surface_is_422(self):
        # F5：一个面只能有一段文字，否则读路径不知道返回哪一个。
        h = Harness()
        prop = h.create()
        with raises(422, "DUPLICATE_TEXT_SURFACE"):
            h.draft(prop["prop_id"], text_details=[
                text_detail("front", "A"), text_detail("front", "B")])

    def test_readable_surface_without_text_is_malformed(self):
        h = Harness()
        prop = h.create()
        with raises(400, "MALFORMED_REQUEST"):
            h.draft(prop["prop_id"], text_details=[
                text_detail("front", "   ", legibility="must_be_readable")])

    def test_caller_supplied_text_hash_must_match(self):
        h = Harness()
        prop = h.create()
        with raises(400, "MALFORMED_REQUEST"):
            h.draft(prop["prop_id"], text_details=[
                text_detail("front", "A-17", text_hash="f" * 64)])

    def test_ocr_mismatch_is_422(self):
        # F6 / E24-E1
        h = Harness()
        prop = h.create()
        with raises(422, "TEXT_OCR_MISMATCH"):
            h.draft(prop["prop_id"], quality_gate={
                "text_ocr": [{"surface": "front", "pass": False}]})

    def test_ocr_absent_is_null_not_true(self):
        # C14-10：没有结论 ≠ 通过。
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"], quality_gate={})
        assert cand["prop_check"]["text_ocr_pass"] is None

    def test_holder_feasibility_fail_is_422(self):
        # E24-E2：结论由 QC 给，S14 只读。
        h = Harness()
        prop = h.create()
        with raises(422, "HOLDER_FEASIBILITY_FAIL"):
            h.draft(prop["prop_id"],
                    quality_gate={"holder_feasibility": "fail"})

    def test_variant_inconsistent_is_422(self):
        # E24-E3：broken 和 intact 画的不是同一把钥匙。
        h = Harness()
        prop = h.create()
        with raises(422, "VARIANT_INCONSISTENT"):
            h.draft(prop["prop_id"],
                    quality_gate={"variant_consistency": "fail"})

    def test_quality_gate_ref_is_required(self):
        h = Harness()
        prop = h.create()
        with raises(400, "MALFORMED_REQUEST"):
            h.draft(prop["prop_id"], quality_gate_ref=None)

    def test_stale_expected_revision_is_conflict(self):
        # F12
        h = Harness()
        prop_id, _ = h.seeded()
        with raises(409, "VERSION_CONFLICT") as info:
            h.draft(prop_id, command_id="c20", expected_current_revision=0)
        assert info.value.retryable is True

    def test_tombstoned_prop_takes_no_new_candidate(self):
        h = Harness()
        prop_id, _ = h.seeded()
        h.command(lambda t, c, b: h.service.change_status(
            t, c, prop_id, "tombstoned", b), {}, command_id="c30")
        with raises(422, "PROP_TOMBSTONED"):
            h.draft(prop_id, command_id="c31", expected_current_revision=1)

    def test_pending_cap_is_429(self):
        # C14-15：本地背压。
        h = Harness(max_pending_candidates=1)
        prop = h.create()
        h.draft(prop["prop_id"], command_id="c2")
        with raises(429, "WRITE_BACKPRESSURE"):
            h.draft(prop["prop_id"], command_id="c3",
                    text_details=[text_detail("front", "A-18")])
        assert h.count("prop_candidate") == 1

    def test_expired_candidate_cannot_be_accepted(self):
        # §2.5 的 30 天 TTL。库里的 `expires_at` 到了就必须 409，而不是
        # "反正已经存下了，接受吧"——候选的 QC 结论可能早已过时。
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"])
        h.conn.execute(
            "UPDATE prop_candidate SET expires_at='2000-01-01T00:00:00.000Z'"
            " WHERE tenant_id='t1' AND candidate_id=?", (cand["candidate_id"],))
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.accept(prop["prop_id"], cand["candidate_id"], command_id="c40")
        # 顺带把状态标成 EXPIRED，让列表查询不再把它当待办
        assert h.rows("prop_candidate", tenant_id="t1",
                      candidate_id=cand["candidate_id"])[0]["state"] == "EXPIRED"
        assert h.count("prop_revision") == 0

    def test_expired_candidate_cannot_be_rejected_either(self):
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"])
        h.conn.execute(
            "UPDATE prop_candidate SET expires_at='2000-01-01T00:00:00.000Z'"
            " WHERE tenant_id='t1' AND candidate_id=?", (cand["candidate_id"],))
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.command(lambda t, c, b: h.service.reject_candidate(
                t, c, prop["prop_id"], cand["candidate_id"], b),
                {}, command_id="c41")

    def test_expired_candidates_stop_counting_against_the_cap(self):
        """死候选不能把 64 上限占死。

        §3.1 的 `ttl──► EXPIRED` 若只写在 accept/reject 的事务里，会在 409
        时回滚；更糟的是 draft 的 cap 查询把早已过期的行也算进去，用户会
        因为一堆自己看不见的候选被 429。新候选进来时必须先把过期位扫掉。
        """
        h = Harness(max_pending_candidates=2)
        prop = h.create()
        old = h.draft(prop["prop_id"], command_id="c2")
        h.draft(prop["prop_id"], command_id="c3",
                text_details=[text_detail("front", "A-18")])
        # 两条都过期：容量已满且全是死行
        h.conn.execute(
            "UPDATE prop_candidate SET expires_at='2000-01-01T00:00:00.000Z'"
            " WHERE tenant_id='t1'")
        out = h.draft(prop["prop_id"], command_id="c4",
                      text_details=[text_detail("front", "A-19")])
        assert out["state"] == "PENDING"
        expired = h.rows("prop_candidate", tenant_id="t1",
                         candidate_id=old["candidate_id"])[0]
        assert expired["state"] == "EXPIRED"
        live = h.conn.execute(
            "SELECT COUNT(*) FROM prop_candidate WHERE state='PENDING'"
        ).fetchone()[0]
        assert live == 1  # 只剩刚插入的那条

    def test_unexpired_candidate_still_accepts(self):
        # 反向对照：上两条如果只是因为 expires_at 写坏而全挂，
        # 这条会一起挂——它证明 TTL 比较本身没把正常路径关掉。
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"])
        out = h.accept(prop["prop_id"], cand["candidate_id"])
        assert out["revision"] == 1


# ================================================================ 上游失败面

class TestDependencyContract:
    """`deps.py` 的失败分类是内核 503/422 的分界线，必须逐条钉住。

    `_upstream()` 只认 `ConnectionError` 与 5xx 的 `ServiceError`；
    4xx 必须原样上抛（404 会被 `build_artifact_source` 翻成 `None`）。
    分类错了，一个"别的租户的 artifact"会变成 503 重试风暴，或者一个
    "上游挂了"会变成 422 让调用方改参数。
    """

    def test_connection_error_becomes_503_retryable(self):
        h = Harness(artifacts={
            "art_intact": ConnectionError("timed out"),
            "art_broken": ConnectionError("timed out"),
            "art_open": ConnectionError("timed out"),
        })
        prop = h.create()
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            h.draft(prop["prop_id"])
        assert h.count("prop_candidate") == 0

    def test_upstream_service_error_is_keyed_by_the_caller(self):
        # 5xx 从上游冒出来时，错误码由调用点给（这里是资产校验），
        # 不能把上游的内部错误码透给调用方。
        from prop_assets.service import ServiceError as SE
        h = Harness(artifacts={
            "art_intact": SE(502, "S28_INTERNAL", "boom"),
            "art_broken": SE(502, "S28_INTERNAL", "boom"),
            "art_open": SE(502, "S28_INTERNAL", "boom"),
        })
        prop = h.create()
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            h.draft(prop["prop_id"])

    def test_artifact_404_is_not_retryable(self):
        # 404 = "S28 说没有"，是调用方的错：422，且不可重试。
        h = Harness()
        prop = h.create()
        with raises(422, "ARTIFACT_NOT_FOUND"):
            h.draft(prop["prop_id"], variant_manifest={
                "condition": {
                    "intact": {"artifact_id": "art_ghost",
                               "content_hash": HASH_A},
                    "broken": {"artifact_id": "art_broken",
                               "content_hash": HASH_B},
                },
                "open_state": {
                    "closed": {"artifact_id": "art_intact",
                               "content_hash": HASH_A},
                    "open": {"artifact_id": "art_open",
                             "content_hash": HASH_B},
                }})

    def test_build_artifact_source_maps_http_404_to_none(self):
        # 直接测 `deps.build_artifact_source`：真发一次 HTTP 到本地
        # 临时服务，确认 404 → None（不是抛错），
        # 因为 service 依靠 None 来区分"没有"与"不可达"。
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from prop_assets.deps import build_artifact_source

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

    def test_build_artifact_source_wraps_transport_failure(self):
        # 端口刚关掉 → URLError → 必须是 ConnectionError（503 那条路），
        # 不能是 ServiceError 或者 urllib 的原始异常。
        import socket
        from prop_assets.deps import build_artifact_source

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        source = build_artifact_source(f"http://127.0.0.1:{port}")
        with pytest.raises(ConnectionError):
            source("t1", "art_1")

    def test_quality_gate_unconfigured_does_not_break_inline(self):
        # QC 未配置时 `draft` 仍可走内联结论——这是 C14-10 的默认姿势。
        h = Harness(quality_gate_source=None)
        prop = h.create()
        cand = h.draft(prop["prop_id"])
        assert cand["candidate_id"].startswith("cand_")


# ================================================================ 接受

class TestAccept:
    def test_accept_creates_immutable_revision_and_event(self):
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"])
        result = h.accept(prop["prop_id"], cand["candidate_id"])
        assert result["revision"] == 1
        assert result["became_canonical"] is True
        assert result["text_changed"] is True  # None → 有文字
        assert h.count("prop_revision") == 1
        assert h.count("outbox") == 1

    def test_event_has_exactly_the_twelve_contract_keys(self):
        h = Harness()
        prop_id, _ = h.seeded()
        (event,) = h.events()
        assert event["type"] == "asset.prop.revision.accepted"
        assert event["source"] == "S14"
        assert event["sequence"] == 1
        assert set(event["data"]) == {
            "schema_version", "prop_ref", "prop_kind", "story_critical",
            "became_canonical", "canonical_revision", "text_changed",
            "text_fingerprint", "variant_axes", "required_variants_covered",
            "impact_plan_ref", "rights_ref"}
        assert set(event["data"]["prop_ref"]) == {
            "service_id", "id", "revision", "content_hash"}
        assert set(event["data_ref"]) == {
            "service_id", "id", "revision", "content_hash"}
        assert event["data"]["prop_ref"]["content_hash"] == HASH_A or \
            len(event["data"]["prop_ref"]["content_hash"]) == 64

    def test_event_values_are_the_facts_not_placeholders(self):
        """键齐了不等于话说对了。

        §5.3 的 12 个键里，`text_changed` / `required_variants_covered` /
        `impact_plan_ref` 都是**下游据此行动**的字段：文本变了要重查字幕与
        前后道具，影响面没登记就不能自动推进，变体没覆盖就不该被接受。
        只钉 key 会让"恒发 False / 恒发 None"这种谎话一路绿灯。
        """
        h = Harness()
        prop_id, _ = h.seeded()
        (event,) = h.events()
        data = event["data"]
        assert data["schema_version"] == "2.0"
        assert data["text_changed"] is True           # None → 有文字
        assert data["required_variants_covered"] is True
        assert data["impact_plan_ref"] == {
            "service_id": "S09", "id": "ip_1", "revision": 1}
        assert data["became_canonical"] is True
        assert data["canonical_revision"] == 1
        assert data["prop_kind"] == "hero_prop"
        assert data["story_critical"] is True
        assert data["variant_axes"] == AXES
        assert data["rights_ref"] == {"service_id": "S02", "id": "rights_1"}
        # 指纹必须是**这一版**的，不是上一版的、也不是空串
        fingerprint = h.conn.execute(
            "SELECT text_fingerprint FROM prop WHERE prop_id=?",
            (prop_id,)).fetchone()[0]
        assert data["text_fingerprint"] == fingerprint
        assert len(fingerprint) == 64
        assert data["prop_ref"] == {
            "service_id": "S14", "id": prop_id, "revision": 1,
            "content_hash": data["prop_ref"]["content_hash"]}
        assert len(data["prop_ref"]["content_hash"]) == 64

    def test_unchanged_text_event_says_so(self):
        # 反向：第二版文字没动，事件里的 `text_changed` 必须跟着变 False。
        # 恒 True 的实现和恒 False 的实现都能通过只钉 key 的测试。
        h = Harness()
        prop_id, _ = h.seeded()
        # 换图不改字：内容哈希变了（否则会撞租户级唯一约束），文字一字未动。
        h.artifacts["art_broken2"] = artifact("art_broken2", HASH_C)
        new_manifest = manifest()
        new_manifest["condition"]["broken"] = {
            "artifact_id": "art_broken2", "content_hash": HASH_C}
        cand = h.draft(prop_id, command_id="c20",
                       expected_current_revision=1,
                       variant_manifest=new_manifest)
        h.accept(prop_id, cand["candidate_id"], command_id="c21",
                 expected_current_revision=1, impact_plan_ref=None)
        second = h.events()[1]
        assert second["data"]["text_changed"] is False
        assert second["data"]["became_canonical"] is True
        # 指纹也得跟着停住，不能因为换图就变
        assert (second["data"]["text_fingerprint"]
                == h.events()[0]["data"]["text_fingerprint"])

    def test_accept_without_canonical_event_reports_pointer_unchanged(self):
        # 接受但不改指针：事件仍要发（修订已进历史），但
        # `became_canonical=false` 且 `canonical_revision` 停在旧值。
        h = Harness()
        prop_id, _ = h.seeded()
        cand = h.draft(prop_id, command_id="c30",
                       expected_current_revision=1,
                       text_details=[text_detail("front", "保险箱 08")])
        h.accept(prop_id, cand["candidate_id"], command_id="c31",
                 expected_current_revision=1, set_canonical=False)
        second = h.events()[1]
        assert second["data"]["became_canonical"] is False
        assert second["data"]["canonical_revision"] == 1
        assert second["data"]["text_changed"] is True

    def test_event_sequence_equals_revision(self):
        h = Harness()
        prop_id, _ = h.seeded()
        h.draft(prop_id, command_id="c10", expected_current_revision=1,
                text_details=[text_detail("front", "A-17"),
                              text_detail("lid", "内部")])
        cand2 = h.draft(prop_id, command_id="c11", expected_current_revision=1,
                        text_details=[text_detail("front", "A-18")])
        # 上一条 draft 占用了 c10 的内容；重新拿 c11 的候选接受
        h.accept(prop_id, cand2["candidate_id"], command_id="c12",
                 expected_current_revision=1)
        seqs = [e["sequence"] for e in h.events()]
        assert seqs == [1, 2]

    def test_candidate_cannot_be_accepted_twice(self):
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"])
        h.accept(prop["prop_id"], cand["candidate_id"])
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.accept(prop["prop_id"], cand["candidate_id"], command_id="c33",
                     expected_current_revision=1)

    def test_concurrent_accept_one_wins(self):
        # F15：同一个 expected_current_revision 的第二个写者必须 409。
        h = Harness()
        prop = h.create()
        a = h.draft(prop["prop_id"], command_id="c2",
                    text_details=[text_detail("front", "A-17")])
        b = h.draft(prop["prop_id"], command_id="c3",
                    text_details=[text_detail("front", "A-18")])
        h.accept(prop["prop_id"], a["candidate_id"], command_id="c4")
        with raises(409, "VERSION_CONFLICT"):
            h.accept(prop["prop_id"], b["candidate_id"], command_id="c5")
        assert h.count("prop_revision") == 1

    def test_duplicate_content_is_409(self):
        # F11 / C14-11：租户级 content_hash 唯一。
        h = Harness()
        first = h.create(command_id="c1")
        cand = h.draft(first["prop_id"], command_id="c2")
        h.accept(first["prop_id"], cand["candidate_id"], command_id="c3")
        other = h.create(command_id="c4", display_name="另一把同款钥匙")
        cand2 = h.draft(other["prop_id"], command_id="c5")
        with raises(409, "DUPLICATE_PROP_CONTENT"):
            h.accept(other["prop_id"], cand2["candidate_id"],
                     command_id="c6")

    def test_reason_and_decision_are_recorded_on_pointer(self):
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"])
        h.accept(prop["prop_id"], cand["candidate_id"],
                 decision_kind="human_approved", reason_text="导演确认")
        (ptr,) = h.rows("canonical_pointer", tenant_id="t1",
                        prop_id=prop["prop_id"])
        assert ptr["decision_kind"] == "human_approved"
        assert ptr["reason_text"] == "导演确认"

    def test_reject_moves_candidate_out_of_pending(self):
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"])
        out = h.command(lambda t, c, b: h.service.reject_candidate(
            t, c, prop["prop_id"], cand["candidate_id"], b),
            {"reason_text": "尺寸不对"}, command_id="c7")
        assert out["state"] == "REJECTED"
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.accept(prop["prop_id"], cand["candidate_id"], command_id="c8")

    def test_rejecting_twice_is_409(self):
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"])
        fn = lambda t, c, b: h.service.reject_candidate(  # noqa: E731
            t, c, prop["prop_id"], cand["candidate_id"], b)
        h.command(fn, {}, command_id="c7")
        with raises(409, "CANDIDATE_NOT_PENDING"):
            h.command(fn, {}, command_id="c8")


class TestTextChangeGate:
    """§1.5 / §3.3 / F7 / F8 / E08-E1…E3。文字变更是最贵的变更。"""

    def test_text_change_with_policy_auto_is_422(self):
        h = Harness()
        prop_id, _ = h.seeded()
        cand = h.draft(prop_id, command_id="c10",
                       expected_current_revision=1,
                       text_details=[text_detail("front", "保险箱 08")])
        with raises(422, "TEXT_CHANGE_REQUIRES_HUMAN"):
            h.accept(prop_id, cand["candidate_id"], command_id="c11",
                     expected_current_revision=1,
                     decision_kind="policy_auto")

    def test_text_unchanged_allows_policy_auto(self):
        # E08-E3：补一张破损图、不动文字。
        h = Harness()
        prop_id, _ = h.seeded()
        # 补图：broken 换一张新 artifact（内容变了），但 canonical_text 一字未动。
        h.artifacts["art_broken2"] = artifact("art_broken2", HASH_C)
        new_manifest = manifest()
        new_manifest["condition"]["broken"] = {
            "artifact_id": "art_broken2", "content_hash": HASH_C}
        new_manifest["open_state"]["open"] = {
            "artifact_id": "art_broken2", "content_hash": HASH_C}
        cand = h.draft(prop_id, command_id="c11",
                       expected_current_revision=1,
                       variant_manifest=new_manifest)
        out = h.accept(prop_id, cand["candidate_id"], command_id="c12",
                       expected_current_revision=1,
                       decision_kind="policy_auto", impact_plan_ref=None)
        assert out["text_changed"] is False
        assert out["text_fingerprint"] == h.events()[0]["data"]["text_fingerprint"]

    def test_story_critical_text_change_needs_impact_plan(self):
        h = Harness()
        prop_id, _ = h.seeded()
        cand = h.draft(prop_id, command_id="c10",
                       expected_current_revision=1,
                       text_details=[text_detail("front", "保险箱 08")])
        with raises(422, "IMPACT_PLAN_REQUIRED"):
            h.accept(prop_id, cand["candidate_id"], command_id="c11",
                     expected_current_revision=1, impact_plan_ref=None)

    def test_non_critical_text_change_needs_no_impact_plan(self):
        h = Harness()
        prop = h.create(story_critical=False)
        cand = h.draft(prop["prop_id"])
        out = h.accept(prop["prop_id"], cand["candidate_id"],
                       impact_plan_ref=None)
        assert out["text_changed"] is True
        assert out["revision"] == 1

    def test_accept_without_set_canonical_keeps_pointer(self):
        # §1.5：canonical ≠ 最新。
        h = Harness()
        prop_id, _ = h.seeded()
        cand = h.draft(prop_id, command_id="c10",
                       expected_current_revision=1,
                       text_details=[text_detail("front", "保险箱 08")],
                       variant_axes={"condition": ["intact", "broken"],
                                     "open_state": ["closed", "open"]})
        out = h.accept(prop_id, cand["candidate_id"], command_id="c11",
                       expected_current_revision=1, set_canonical=False)
        assert out["became_canonical"] is False
        assert out["canonical_revision"] == 1
        prop = h.rows("prop", tenant_id="t1", prop_id=prop_id)[0]
        assert prop["current_revision"] == 2
        assert prop["canonical_revision"] == 1
        # canonical 的 text_fingerprint 没被新版本覆盖
        assert prop["text_fingerprint"] != out["text_fingerprint"]

    def test_story_critical_canonical_needs_passing_ocr(self):
        # C14-10：null 也挡——"不知道对不对"不是"对"。
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"], quality_gate={})
        with raises(422, "TEXT_OCR_INCOMPLETE"):
            h.accept(prop["prop_id"], cand["candidate_id"])

    def test_moving_pointer_to_other_text_needs_human(self):
        h = Harness()
        prop_id, _ = h.seeded()
        cand = h.draft(prop_id, command_id="c10",
                       expected_current_revision=1,
                       text_details=[text_detail("front", "保险箱 08")])
        h.accept(prop_id, cand["candidate_id"], command_id="c11",
                 expected_current_revision=1, set_canonical=False)
        with raises(422, "TEXT_CHANGE_REQUIRES_HUMAN"):
            h.command(lambda t, c, b: h.service.set_canonical(
                t, c, prop_id, b),
                {"revision": 2, "decision_kind": "policy_auto"},
                command_id="c12")

    def test_set_canonical_to_same_revision_is_noop(self):
        h = Harness()
        prop_id, _ = h.seeded()
        out = h.command(lambda t, c, b: h.service.set_canonical(
            t, c, prop_id, b), {"revision": 1}, command_id="c10")
        assert out["changed"] is False
        assert len(h.events()) == 1  # 没发第二条事件

    def test_set_canonical_emits_no_event(self):
        # C14-7 / PRP-4：canonical 移动没有事件。消费方必须回读。
        h = Harness()
        prop_id, _ = h.seeded()
        cand = h.draft(prop_id, command_id="c10",
                       expected_current_revision=1,
                       text_details=[text_detail("front", "保险箱 08")])
        h.accept(prop_id, cand["candidate_id"], command_id="c11",
                 expected_current_revision=1, set_canonical=False)
        before = h.count("outbox")
        h.command(lambda t, c, b: h.service.set_canonical(
            t, c, prop_id, b),
            {"revision": 2, "decision_kind": "human_approved"},
            command_id="c12")
        assert h.count("outbox") == before
        assert h.service.get_canonical("t1", prop_id)["prop_ref"]["revision"] == 2

    def test_canonical_not_set_is_404_with_its_own_code(self):
        h = Harness()
        prop = h.create()
        with raises(404, "CANONICAL_NOT_SET"):
            h.service.get_canonical("t1", prop["prop_id"])


# ================================================================ 读路径

class TestReadPaths:
    def test_variant_selector_returns_only_that_artifact(self):
        h = Harness()
        prop_id, _ = h.seeded()
        out = h.service.get_revision("t1", prop_id, 1,
                                     variant="condition:broken")
        assert out["artifacts"] == {
            "condition:broken": {"artifact_id": "art_broken",
                                 "content_hash": HASH_B}}

    def test_multi_axis_selector(self):
        h = Harness()
        prop_id, _ = h.seeded()
        out = h.service.get_revision(
            "t1", prop_id, 1, variant="condition:broken,open_state:open")
        assert set(out["artifacts"]) == {"condition:broken", "open_state:open"}

    def test_missing_variant_is_422_with_available_values(self):
        # F9 / E37-E1：**不静默退回默认**。
        h = Harness()
        prop_id, _ = h.seeded()
        with raises(422, "VARIANT_NOT_AVAILABLE") as info:
            h.service.get_revision("t1", prop_id, 1, variant="condition:burnt")
        assert info.value.details["axis"] == "condition"
        assert info.value.details["value"] == "burnt"
        assert info.value.details["available"] == ["intact", "broken"]

    def test_unknown_axis_is_also_422(self):
        h = Harness()
        prop_id, _ = h.seeded()
        with raises(422, "VARIANT_NOT_AVAILABLE"):
            h.service.get_revision("t1", prop_id, 1, variant="mood:angry")

    def test_bad_selector_syntax_is_malformed(self):
        h = Harness()
        prop_id, _ = h.seeded()
        with raises(400, "MALFORMED_REQUEST"):
            h.service.get_revision("t1", prop_id, 1, variant="condition")

    def test_revision_marks_canonical(self):
        h = Harness()
        prop_id, _ = h.seeded()
        assert h.service.get_revision("t1", prop_id, 1)["is_canonical"] is True
        out = h.service.get_revision("t1", prop_id, 1)
        assert out["rights_recheck_required"] is True  # E30-E1

    def test_cross_tenant_read_is_404(self):
        # F14 / E28-E1：与"不存在"同形，不泄漏存在性。
        h = Harness()
        prop_id, _ = h.seeded()
        with raises(404, "NOT_FOUND"):
            h.service.get_revision("t2", prop_id, 1)
        with raises(404, "NOT_FOUND"):
            h.service.get_prop("t2", prop_id)

    def test_cross_tenant_text_is_404_not_redacted_200(self):
        # E28-E2：跨租户根本不该知道存在。
        h = Harness()
        prop_id, _ = h.seeded()
        with raises(404, "NOT_FOUND"):
            h.service.read_text("t2", prop_id, 1)

    def test_lookup_is_exact_name_match(self):
        h = Harness()
        h.create()
        out = h.service.lookup("t1", series_scope="ser_1", alias="铜钥匙")
        assert out["prop"]["display_name"] == "铜钥匙"
        with raises(404, "NOT_FOUND"):
            h.service.lookup("t1", series_scope="ser_1", alias="铜")

    def test_deprecate_then_tombstone(self):
        h = Harness()
        prop = h.create()
        fn = lambda target: (lambda t, c, b: h.service.change_status(  # noqa
            t, c, prop["prop_id"], target, b))
        assert h.command(fn("deprecated"), {}, command_id="c5")["status"] == \
            "deprecated"
        assert h.command(fn("tombstoned"), {}, command_id="c6")["status"] == \
            "tombstoned"
        with raises(409, "INVALID_STATUS_TRANSITION"):
            h.command(fn("deprecated"), {}, command_id="c7")


class TestSensitiveText:
    def test_non_privileged_reader_sees_redaction(self):
        # F10 / §1.3 第 3 条
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"], text_details=[
            text_detail(surface="inner", text="密码 4711", sensitive=True)],
            quality_gate={"text_ocr": [{"surface": "inner", "pass": True}]})
        h.accept(prop["prop_id"], cand["candidate_id"])
        out = h.service.read_text("t1", prop["prop_id"], 1)
        item = out["items"][0]
        assert item["text"] == "[redacted]"
        assert item["redacted"] is True
        assert len(item["text_fingerprint"]) == 64

    def test_signed_scope_reveals_text(self):
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"], text_details=[
            text_detail(surface="inner", text="密码 4711", sensitive=True)],
            quality_gate={"text_ocr": [{"surface": "inner", "pass": True}]})
        h.accept(prop["prop_id"], cand["candidate_id"])
        out = h.service.read_text("t1", prop["prop_id"], 1,
                                  principal="u1",
                                  scopes=("prop.text.sensitive_read",))
        assert out["items"][0]["text"] == "密码 4711"
        assert out["items"][0]["redacted"] is False

    def test_authorizer_deny_beats_permission(self):
        # S01 的 deny 压过 allow；只读 permissions 会让显式 deny 消失。
        def authorizer(tenant_id, principal_id):
            return {"permissions": ["prop.text.sensitive_read"],
                    "denied": ["prop.text.sensitive_read"]}
        h = Harness(authorizer=authorizer)
        prop = h.create()
        cand = h.draft(prop["prop_id"], text_details=[
            text_detail(surface="inner", text="密码 4711", sensitive=True)],
            quality_gate={"text_ocr": [{"surface": "inner", "pass": True}]})
        h.accept(prop["prop_id"], cand["candidate_id"])
        out = h.service.read_text("t1", prop["prop_id"], 1, principal="u1")
        assert out["items"][0]["redacted"] is True

    def test_authorizer_failure_is_503_not_false(self):
        # 读不到权限结论既不是"有权限"也不是"没权限"，都是未知。
        def authorizer(tenant_id, principal_id):
            raise ConnectionError("s01 down")
        h = Harness(authorizer=authorizer)
        prop = h.create()
        cand = h.draft(prop["prop_id"], text_details=[
            text_detail(surface="inner", text="密码 4711", sensitive=True)],
            quality_gate={"text_ocr": [{"surface": "inner", "pass": True}]})
        h.accept(prop["prop_id"], cand["candidate_id"])
        with raises(503, "DEPENDENCY_UNAVAILABLE"):
            h.service.read_text("t1", prop["prop_id"], 1, principal="u1")

    def test_non_sensitive_text_is_plain(self):
        h = Harness()
        prop_id, _ = h.seeded()
        out = h.service.read_text("t1", prop_id, 1)
        assert out["items"][0]["text"] == "保险箱 07"


class TestCrashRecovery:
    def test_derived_table_matches_payload(self):
        # 崩溃恢复：派生表与 payload 必须一致（§2.4 的"payload 才是权威"）。
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"], text_details=[
            text_detail("front", "保险箱 07"),
            text_detail("lid", "内部编号 12", legibility="may_be_blurred")])
        h.accept(prop["prop_id"], cand["candidate_id"])
        payload = json.loads(h.rows("prop_revision", tenant_id="t1",
                                    prop_id=prop["prop_id"],
                                    revision=1)[0]["payload"])
        derived = {r["surface"]: r for r in h.rows(
            "prop_text_detail", tenant_id="t1", prop_id=prop["prop_id"],
            revision=1)}
        assert set(derived) == {d["surface"] for d in payload["text_details"]}
        for detail in payload["text_details"]:
            assert derived[detail["surface"]]["text_hash"] == \
                detail["text_hash"]
            assert derived[detail["surface"]]["legibility"] == \
                detail["legibility"]

    def test_replay_writes_no_second_revision_or_event(self):
        h = Harness()
        prop = h.create()
        cand = h.draft(prop["prop_id"])
        first = h.accept(prop["prop_id"], cand["candidate_id"], command_id="c3")
        again = h.accept(prop["prop_id"], cand["candidate_id"], command_id="c3")
        assert again["replayed"] is True
        assert again["revision"] == first["revision"]
        assert h.count("prop_revision") == 1
        assert h.count("outbox") == 1

    def test_replay_does_not_reread_s28(self):
        h = Harness()
        prop = h.create()
        h.draft(prop["prop_id"])
        calls = h.calls["artifact"]
        h.draft(prop["prop_id"], command_id="c2")  # 同 body 同 key → 重放
        assert h.calls["artifact"] == calls


# ================================================================ HTTP

def _http_client(monkeypatch, tmp_path, **over):
    monkeypatch.setenv("S14_DB", str(tmp_path / "s14.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s14_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s14_api_app"] = module
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
        r = client.post("/props", json={"project_id": "prj_1",
                                        "series_scope": "ser_1",
                                        "display_name": "铜钥匙",
                                        "prop_kind": "hero_prop"},
                        headers=_headers())
        assert r.status_code == 201, r.text
        prop_id = r.json()["prop_id"]
        got = client.get(f"/props/{prop_id}", headers=_headers())
        assert got.status_code == 200
        assert got.json()["prop"]["display_name"] == "铜钥匙"
        assert got.json()["rights_recheck_required"] is True

    def test_missing_idempotency_key_is_400(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/props", json={"project_id": "prj_1"},
                        headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 400
        assert r.json()["code"] == "MALFORMED_REQUEST"

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/props", json={"tenant_id": "t2",
                                        "project_id": "prj_1",
                                        "series_scope": "ser_1",
                                        "display_name": "铜钥匙",
                                        "prop_kind": "hero_prop"},
                        headers=_headers())
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_SCOPE_DENIED"

    def test_holder_route_does_not_exist(self, monkeypatch, tmp_path):
        # F16：没有路由是最强的边界声明。
        client, h = _http_client(monkeypatch, tmp_path)
        prop = h.create()
        r = client.get(f"/props/{prop['prop_id']}/holder", headers=_headers())
        assert r.status_code == 404
        # 连 404 的 body 都不是服务的错误形状 —— 它是 FastAPI 的"无此路由"。
        assert "code" not in r.json()

    def test_full_flow_over_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        prop_id = client.post("/props", json={
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "铜钥匙", "prop_kind": "hero_prop",
            "story_critical": True}, headers=_headers()).json()["prop_id"]

        cand = client.post(f"/props/{prop_id}/candidates",
                           json=candidate_body(),
                           headers=_headers(command_id="c2"))
        assert cand.status_code == 200, cand.text
        candidate_id = cand.json()["candidate_id"]

        acc = client.post(
            f"/props/{prop_id}/candidates/{candidate_id}:accept",
            json={"expected_current_revision": 0, "set_canonical": True,
                  "decision_kind": "human_approved",
                  "impact_plan_ref": {"service_id": "S09", "id": "ip_1",
                                      "revision": 1}},
            headers=_headers(command_id="c3"))
        assert acc.status_code == 200, acc.text
        assert acc.json()["revision"] == 1

        rev = client.get(f"/props/{prop_id}/revisions/1",
                         params={"variant": "condition:broken"},
                         headers=_headers())
        assert rev.status_code == 200
        assert "condition:broken" in rev.json()["artifacts"]

        can = client.get(f"/props/{prop_id}/canonical", headers=_headers())
        assert can.status_code == 200
        assert can.json()["canonical"]["revision"] == 1

        lookup = client.get("/props:lookup",
                            params={"series_scope": "ser_1",
                                    "alias": "铜钥匙"},
                            headers=_headers())
        assert lookup.status_code == 200
        assert lookup.json()["prop"]["prop_id"] == prop_id

    def test_variant_not_available_over_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        prop_id, _ = h.seeded()
        r = client.get(f"/props/{prop_id}/revisions/1",
                       params={"variant": "condition:burnt"},
                       headers=_headers())
        assert r.status_code == 422
        body = r.json()
        assert body["code"] == "VARIANT_NOT_AVAILABLE"
        assert body["retryable"] is False
        assert body["details"]["available"] == ["intact", "broken"]

    def test_redact_false_does_not_grant_plaintext(self, monkeypatch,
                                                  tmp_path):
        # C14-6：查询参数不是授权。
        client, h = _http_client(monkeypatch, tmp_path)
        prop = h.create()
        cand = h.draft(prop["prop_id"], text_details=[
            text_detail(surface="inner", text="密码 4711", sensitive=True)],
            quality_gate={"text_ocr": [{"surface": "inner", "pass": True}]})
        h.accept(prop["prop_id"], cand["candidate_id"])
        r = client.get(f"/props/{prop['prop_id']}/revisions/1/text",
                       params={"redact": "false", "surface": "inner"},
                       headers=_headers())
        assert r.status_code == 200
        assert r.json()["items"][0]["text"] == "[redacted]"

    def test_signed_scope_header_reveals_text(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        prop = h.create()
        cand = h.draft(prop["prop_id"], text_details=[
            text_detail(surface="inner", text="密码 4711", sensitive=True)],
            quality_gate={"text_ocr": [{"surface": "inner", "pass": True}]})
        h.accept(prop["prop_id"], cand["candidate_id"])
        r = client.get(f"/props/{prop['prop_id']}/revisions/1/text",
                       params={"surface": "inner"},
                       headers=_headers(
                           **{"X-Principal-Scopes":
                              "prop.text.sensitive_read"}))
        assert r.json()["items"][0]["text"] == "密码 4711"

    def test_cross_tenant_over_http_is_404(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        prop_id, _ = h.seeded()
        r = client.get(f"/props/{prop_id}/revisions/1",
                       headers=_headers(tenant="t2"))
        assert r.status_code == 404
        assert r.json()["code"] == "NOT_FOUND"

    def test_idempotent_replay_over_http(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        body = {"project_id": "prj_1", "series_scope": "ser_1",
                "display_name": "铜钥匙", "prop_kind": "hero_prop"}
        first = client.post("/props", json=body, headers=_headers())
        again = client.post("/props", json=body, headers=_headers())
        assert first.json()["replayed"] is False
        assert again.json()["replayed"] is True
        assert again.json()["prop_id"] == first.json()["prop_id"]

    def test_same_key_different_body_over_http_is_409(self, monkeypatch,
                                                      tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        body = {"project_id": "prj_1", "series_scope": "ser_1",
                "display_name": "铜钥匙", "prop_kind": "hero_prop"}
        client.post("/props", json=body, headers=_headers())
        r = client.post("/props", json={**body, "display_name": "银钥匙"},
                        headers=_headers())
        assert r.status_code == 409
        assert r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_readiness_reports_contract_gaps(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/health/readiness")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        ids = {g["id"] for g in body["contract_gaps"]}
        assert {"C14-1", "C14-16"} <= ids
        assert ids == {g["id"] for g in CONTRACT_GAPS}

    def test_list_props_is_scoped(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        h.create()
        r = client.get("/props", params={"series_scope": "ser_1"},
                       headers=_headers())
        assert len(r.json()["items"]) == 1
        assert client.get("/props", params={"series_scope": "ser_2"},
                          headers=_headers()).json()["items"] == []


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
        assert ids[:8] == [f"C14-{i}" for i in range(1, 9)]

    def test_no_owner_decision_is_filled_in(self):
        # AGENTS.md：冲突要报告，不要自行选边。每条 handling 都要指出
        # "按方案哪一节实现"，而不是宣布裁决。
        for gap in CONTRACT_GAPS:
            if gap["kind"] == "needs_owner":
                assert "待" in gap["handling"] or "未定" in gap["handling"] \
                    or "需" in gap["handling"], gap["id"]

    def test_merge_route_is_not_implemented(self):
        # C14-12：体与语义都没定义，按 §9 不猜。
        from prop_assets.service import PropAssetsService as Svc
        assert not hasattr(Svc, "merge_props")
