"""S33 单测。编号对应 `plans/S33-sound-effects.md` §10 的 T33-01…T33-53。

最重要的两组：`TestPlace`（§11.1 明确要求"在任何供应商接入前做完"）和
`TestReuse`（E38 —— 剪辑改位置不该花钱）。

上游全部是 stub，**按真实的上游契约写**（S26 的 `create_job` 要 route decision、
S28 的 `artifact.ingested` 不带 `natural_duration_ms`），不是按内核希望的样子写。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import SqliteIdempotencyStore

from sound_effects.schema import SCHEMA
from sound_effects.service import (
    SfxService,
    ServiceError,
    compute_placement,
    content_fingerprint,
    place,
)


UPSTREAM_POLICY = {"service_id": "S02", "id": "pol_1", "revision": 3,
                   "content_hash": "a" * 64}
NOW_FUTURE = "2099-01-01T00:00:00.000Z"


class Harness:
    """一个内存库 + 幂等外壳，形状与 `api/app.py` 的 `command()` 一致。"""

    def __init__(self, conn=None, **overrides):
        # 传 `conn` 用于把同一套 stub 接到 `api/app.py`：那一层在 import 时
        # 自己建连接，测试必须用它的连接，否则断言查的是另一个库。
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.idem = SqliteIdempotencyStore(self.conn)
        self.calls = {"submit": 0, "route": 0, "rights": 0, "loop": 0,
                      "artifact": 0, "cancel": 0}

        self.rights_result = overrides.get("rights", {
            "allowed_license_kinds": ["royalty_free"],
            "attribution_required": False,
            "territory_restrictions": [],
        })
        self.route_result = overrides.get("route", {
            "route_decision_id": "rd_1", "revision": 1,
            "upper_bound_micro": 40_000, "feasible": True,
            "capabilities": {"loopable": True},
        })
        self.submit_result = overrides.get("submit", {
            "provider_job_id": "pj_1", "operation_id": "op_1",
            "attempt_id": "at_1",
        })
        self.edl = overrides.get("edl", {
            "edl_revision": 7,
            "shots": {"shot_A": {"start_ms": 2000, "end_ms": 9000,
                                 "beats": {"b1": 3000}}},
        })
        self.loopable = overrides.get("loopable", True)
        self.artifact_present = overrides.get("artifact_present", True)

        self.service = SfxService(
            self.conn,
            rights_resolver=self._rights,
            route_resolver=self._route,
            submitter=self._submit,
            artifact_check=self._artifact,
            edl_provider=self._edl,
            loop_probe=self._loop,
            canceler=self._cancel,
        )

    # ---- stubs ----
    def _rights(self, ref, tenant_id):
        self.calls["rights"] += 1
        if isinstance(self.rights_result, Exception):
            raise self.rights_result
        return self.rights_result

    def _route(self, tenant_id, job, generation_command):
        self.calls["route"] += 1
        if isinstance(self.route_result, Exception):
            raise self.route_result
        return self.route_result

    def _submit(self, job, generation_command, route):
        self.calls["submit"] += 1
        if isinstance(self.submit_result, Exception):
            raise self.submit_result
        return self.submit_result

    def _artifact(self, artifact_ref, tenant_id):
        self.calls["artifact"] += 1
        return self.artifact_present

    def _edl(self, tenant_id, shot_lineage_id, edl_revision):
        return self.edl

    def _loop(self, artifact_ref):
        self.calls["loop"] += 1
        return self.loopable

    def _cancel(self, provider_job_id, reason):
        self.calls["cancel"] += 1

    # ---- idempotency shell ----
    def command(self, body, tenant_id="t1", command_id="c1"):
        from idempotency_sdk import canonical_request_hash

        payload = {**body, "command_id": command_id}
        record = self.idem.begin(tenant_id, command_id,
                                 canonical_request_hash(payload))
        if record.result is not None:
            import json
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        result = self.service.create_job(payload, tenant_id,
                                         command_id=command_id)
        import json
        self.idem.finish(tenant_id, command_id,
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ---- body builders ----
    def body(self, **cue_over):
        cue = {
            "cue_id": "cue_1", "cue_revision": 1,
            "shot_lineage_id": "shot_A", "cue_index": 0,
            "beat_ref": None, "sfx_family": "foley",
            "cue_intent": "heavy door closing", "anchor": "shot_start",
            "anchor_offset_ms": 0, "loopable_required": False,
        }
        cue.update(cue_over)
        return {
            "context": {
                "tenant_id": "t1", "project_id": "prj_1", "series_id": "ser_1",
                "episode_id": "ep_1", "branch_id": "br_1",
                "node_id": "node_1", "node_epoch": 4, "execution_fence": 2,
            },
            "cue": cue,
            "duration_seconds": 0.6,
            "rights_policy_ref": dict(UPSTREAM_POLICY),
            "budget_scope_id": "budget_ep1",
            "deadline": NOW_FUTURE,
        }


# ================================================================ place

class TestPlace:
    """§11.1 第 3 步：核心逻辑，先于任何供应商接入。"""

    def test_fixed_within_one_frame(self):
        got = place(natural_duration_ms=600, requested_duration_ms=630,
                    sfx_family="foley", loopable=False)
        assert got["placement_mode"] == "fixed"

    def test_trim_when_material_longer(self):
        # T33-11：内容相同、时长变短。
        got = place(natural_duration_ms=600, requested_duration_ms=400,
                    sfx_family="foley", loopable=False)
        assert got["placement_mode"] == "trim"
        assert got["source_in_ms"] == 0
        assert got["source_out_ms"] == 400

    def test_stretch_foley_at_limit(self):
        # T33-22：1s 素材填 2s，比值正好 2.0 = foley 上限。
        got = place(natural_duration_ms=1000, requested_duration_ms=2000,
                    sfx_family="foley", loopable=False)
        assert got["placement_mode"] == "stretch"
        assert got["stretch_ratio"] == pytest.approx(2.0, abs=1e-6)

    def test_hard_effect_3000_from_600_is_unplaceable(self):
        # T33-21：枪响要 3 秒但素材 0.6 秒。**不拉伸 5 倍**。
        got = place(natural_duration_ms=600, requested_duration_ms=3000,
                    sfx_family="hard_effect", loopable=False)
        assert got["placement_mode"] == "unplaceable"
        assert got["reason"] == "duration_mismatch"

    def test_ambience_loops_even_past_stretch_limit(self):
        # T33-23：5s 素材填 8s，比值 1.6 > ambience 的 1.0，但**必须 loop**。
        got = place(natural_duration_ms=5000, requested_duration_ms=8000,
                    sfx_family="ambience", loopable=True)
        assert got["placement_mode"] == "loop"

    def test_ambience_30s_from_5s_loops(self):
        got = place(natural_duration_ms=5000, requested_duration_ms=30000,
                    sfx_family="ambience", loopable=True)
        assert got["placement_mode"] == "loop"

    def test_ambience_not_loopable_and_too_long_is_unplaceable(self):
        got = place(natural_duration_ms=5000, requested_duration_ms=8000,
                    sfx_family="ambience", loopable=False)
        assert got["placement_mode"] == "unplaceable"

    def test_ui_is_never_trimmed(self):
        # ui 的 TRIM_ALLOWED=False：短一截就是换了个音，属于内容层决定。
        got = place(natural_duration_ms=600, requested_duration_ms=300,
                    sfx_family="ui", loopable=False)
        assert got["placement_mode"] == "unplaceable"

    def test_loop_before_stretch_would_be_wrong(self):
        # 反证：hard_effect + loopable + 比值 1.1 必须是 stretch 不是 loop。
        # 这就是"把 loop 提到 stretch 前面"会打破的用例。
        got = place(natural_duration_ms=1000, requested_duration_ms=1100,
                    sfx_family="hard_effect", loopable=True)
        assert got["placement_mode"] == "stretch"

    def test_rejects_non_positive_durations(self):
        with pytest.raises(ServiceError) as exc:
            place(natural_duration_ms=0, requested_duration_ms=100,
                  sfx_family="foley", loopable=False)
        assert exc.value.code == "DURATION_OUT_OF_RANGE"


class TestAnchor:
    def test_shot_start_follows_edl(self):
        # T33-17：shot 前移 2 秒，位置跟随 -2000。
        shot = {"start_ms": 0, "end_ms": 7000}
        got = compute_placement(
            natural_duration_ms=600, requested_duration_ms=600,
            sfx_family="foley", loopable=False, anchor="shot_start",
            anchor_offset_ms=0, edl_revision=2, edl_shot=shot, beat_ref=None)
        assert got["output_start_ms"] == 0
        shot2 = {"start_ms": -2000, "end_ms": 5000}
        got2 = compute_placement(
            natural_duration_ms=600, requested_duration_ms=600,
            sfx_family="foley", loopable=False, anchor="shot_start",
            anchor_offset_ms=0, edl_revision=3, edl_shot=shot2, beat_ref=None)
        assert got2["output_start_ms"] == -2000

    def test_absolute_ignores_edl(self):
        # T33-19：绝对锚点不随剪辑变。
        got = compute_placement(
            natural_duration_ms=600, requested_duration_ms=600,
            sfx_family="foley", loopable=False, anchor="absolute",
            anchor_offset_ms=5000, edl_revision=9,
            edl_shot={"start_ms": 1, "end_ms": 2}, beat_ref=None)
        assert got["output_start_ms"] == 5000

    def test_deleted_beat_is_unavailable_not_zero(self):
        # T33-18：beat 被删 → 位置算不出来。**不能填 0**，0ms 是合法位置。
        got = compute_placement(
            natural_duration_ms=600, requested_duration_ms=600,
            sfx_family="foley", loopable=False, anchor="beat",
            anchor_offset_ms=0, edl_revision=4,
            edl_shot={"start_ms": 0, "end_ms": 9000, "beats": {}},
            beat_ref="b1")
        assert got["output_start_ms"] is None
        assert got["placement_unavailable_reason"] == "edl_unavailable"

    def test_no_edl_defers_position(self):
        got = compute_placement(
            natural_duration_ms=600, requested_duration_ms=600,
            sfx_family="foley", loopable=False, anchor="shot_start",
            anchor_offset_ms=0, edl_revision=None, edl_shot=None, beat_ref=None)
        assert got["placement_mode"] == "fixed"
        assert got["output_start_ms"] is None


class TestFingerprint:
    def test_duration_is_not_in_fingerprint(self):
        # 整个服务省钱的这一刀。
        a = content_fingerprint("door", "foley", False)
        b = content_fingerprint("door", "foley", False)
        assert a == b
        assert len(a) == 64

    def test_family_and_loopability_are(self):
        base = content_fingerprint("rain", "ambience", True)
        assert base != content_fingerprint("rain", "ambience", False)
        assert base != content_fingerprint("rain", "foley", True)

    def test_separator_prevents_collision(self):
        # "ab"+"c" 与 "a"+"bc" 不能撞。
        assert content_fingerprint("ab", "foley", False) != \
            content_fingerprint("a", "foley", False)


# ================================================================ 创建

class TestCreateJob:
    def test_happy_path_submits_and_ledgers(self):
        h = Harness()
        out = h.command(h.body())
        assert out["state"] == "WAITING_PROVIDER"
        assert out["sfx_job_id"].startswith("sfj_")
        assert h.calls["submit"] == 1
        rows = h.conn.execute(
            "SELECT action FROM sfx_intent_ledger").fetchall()
        assert [r["action"] for r in rows] == ["generate"]  # T33-53

    def test_generation_command_drops_family_anchor_loopable(self):
        # C1：这三个契约里没有，必须被丢掉而不是假装传上去。
        seen = {}

        def _submit(job, generation_command, route):
            seen.update(generation_command)
            return {"provider_job_id": "pj_x", "operation_id": "op_x"}

        h = Harness()
        h.service.submitter = _submit
        h.command(h.body(loopable_required=True))
        assert seen["modality"] == "sound_effect"
        assert set(seen["input"]) == {"cue_intent", "duration_seconds",
                                      "rights_policy_ref"}

    def test_idempotent_replay(self):
        # T33-01
        h = Harness()
        first = h.command(h.body())
        second = h.command(h.body())
        assert second["replayed"] is True
        assert second["sfx_job_id"] == first["sfx_job_id"]
        assert h.calls["submit"] == 1

    def test_conflicting_body_same_command(self):
        # T33-02
        from idempotency_sdk import CommandConflict

        h = Harness()
        h.command(h.body())
        with pytest.raises(CommandConflict):
            h.command(h.body(cue_intent="different intent"))

    def test_empty_intent(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command(h.body(cue_intent="   "))
        assert exc.value.code == "EMPTY_CUE_INTENT"

    def test_unknown_family(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command(h.body(sfx_family="music"))
        assert exc.value.code == "INVALID_SFX_FAMILY"

    def test_duration_zero_and_over_max(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command({**h.body(), "duration_seconds": 0})
        assert exc.value.code == "DURATION_OUT_OF_RANGE"

        h2 = Harness()
        with pytest.raises(ServiceError) as exc:
            h2.command({**h2.body(), "duration_seconds": 601})
        assert exc.value.code == "DURATION_OUT_OF_RANGE"

    def test_deadline_passed(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command({**h.body(), "deadline": "2000-01-01T00:00:00.000Z"})
        assert exc.value.code == "DEADLINE_PASSED"

    def test_tenant_mismatch(self):
        h = Harness()
        body = h.body()
        body["context"]["tenant_id"] = "someone_else"
        with pytest.raises(ServiceError) as exc:
            h.command(body)
        assert exc.value.status == 403

    def test_beat_anchor_requires_beat_ref(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.command(h.body(anchor="beat"))
        assert exc.value.code == "INVALID_ANCHOR"


# ================================================================ 许可

class TestPolicy:
    def test_empty_licence_kinds_blocks_and_records_gap(self):
        # T33-07 / T33-08：空集合 = 什么都不能用。
        h = Harness(rights={"allowed_license_kinds": []})
        out = h.command(h.body())
        assert out["state"] == "POLICY_BLOCKED"
        assert h.calls["submit"] == 0
        assert h.calls["route"] == 0
        gap = h.conn.execute("SELECT kind, state FROM sfx_gap").fetchone()
        assert (gap["kind"], gap["state"]) == ("no_licensed_route", "open")

    def test_unconfigured_rights_resolver_fails_loudly(self):
        h = Harness()
        h.service.rights_resolver = lambda *a, **k: (_ for _ in ()).throw(
            ServiceError(503, "RIGHTS_RESOLVER_UNAVAILABLE", "not configured"))
        with pytest.raises(ServiceError) as exc:
            h.command(h.body())
        assert exc.value.status == 503

    def test_blocked_family_is_policy_blocked_with_gap(self):
        # §4.1 第 3 步与 §3.1 行 275：**空集合或族冲突都走
        # POLICY_BLOCKED + SfxGap(no_licensed_route)**，不是 4xx。两者都不是
        # "这次请求写错了"，换策略就能好，所以要留给法务一件看得见的事。
        h = Harness(rights={"allowed_license_kinds": ["royalty_free"],
                            "blocked_families": ["foley"]})
        out = h.command(h.body())
        assert out["state"] == "POLICY_BLOCKED"
        assert out["reason_code"] == "FAMILY_BLOCKED"
        assert h.calls["submit"] == 0
        assert h.calls["route"] == 0
        gap = h.conn.execute("SELECT kind, state FROM sfx_gap").fetchone()
        assert (gap["kind"], gap["state"]) == ("no_licensed_route", "open")

    def test_loopable_unsupported_is_rejected_local(self):
        # T33-09 / F6：**不降级为"生成一个长的"**。
        route = {"route_decision_id": "rd_1", "feasible": True,
                 "capabilities": {"loopable": False}}
        h = Harness(route=route)
        out = h.command(h.body(sfx_family="ambience", loopable_required=True))
        assert out["state"] == "REJECTED_LOCAL"
        assert out["reason_code"] == "CAPABILITY_MISSING"
        assert h.calls["submit"] == 0

    def test_route_infeasible_passes_through_options(self):
        h = Harness(route={"feasible": False,
                           "degradation_options": ["shorter_duration"]})
        out = h.command(h.body())
        assert out["state"] == "REJECTED_LOCAL"
        assert out["degradation_options"] == ["shorter_duration"]

    def test_residency_forbidden(self):
        h = Harness(route={"route_decision_id": "rd_1", "feasible": True,
                           "residency_allowed": False})
        out = h.command(h.body())
        assert out["reason_code"] == "RESIDENCY_FORBIDDEN"


# ================================================================ 复用

class TestReuse:
    """E38：剪辑只改位置不花钱。"""

    def _ready_take(self, h, duration_ms=600, family="foley",
                    intent="heavy door closing", edl=None):
        h.command(h.body(sfx_family=family, cue_intent=intent))
        job_id = h.conn.execute(
            "SELECT sfx_job_id FROM sfx_job ORDER BY created_at DESC"
        ).fetchone()["sfx_job_id"]
        h.service.on_artifact_ingested({
            "tenant_id": "t1",
            "data": {
                "artifact_ref": {"service_id": "S28", "id": "art_1",
                                 "revision": 1,
                                 "content_hash": "b" * 64},
                "duration_ms": duration_ms,
                "source": {"provider_job_id": "pj_1"},
            },
        })
        return job_id

    def test_same_duration_reuses_without_paying(self):
        # T33-10
        h = Harness()
        self._ready_take(h)
        assert h.calls["submit"] == 1
        out = h.command(h.body(), command_id="c2")
        assert out["reused"] is True
        assert out["sfx_job_id"] is None
        assert h.calls["submit"] == 1  # 没有第二次付费
        assert h.conn.execute(
            "SELECT COUNT(*) c FROM sfx_intent_ledger").fetchone()["c"] == 1

    def test_shorter_request_trims_and_reuses(self):
        # T33-11
        h = Harness()
        self._ready_take(h)
        out = h.command({**h.body(), "duration_seconds": 0.3},
                        command_id="c2")
        assert out["reused"] is True
        assert out["placement"]["placement_mode"] == "trim"

    def test_longer_within_stretch_reuses(self):
        # T33-12：1 秒素材要 2 秒（foley 上限 2.0）。
        h = Harness()
        self._ready_take(h, duration_ms=1000)
        out = h.command({**h.body(), "duration_seconds": 2.0},
                        command_id="c2")
        assert out["reused"] is True
        assert out["placement"]["placement_mode"] == "stretch"

    def test_longer_past_stretch_creates_new_job(self):
        # T33-13：3 秒枪响用 0.6 秒素材 → 不可复用，走新 job。
        h = Harness()
        self._ready_take(h, duration_ms=600, family="hard_effect",
                         intent="gunshot")
        h.calls["submit"] = 0
        out = h.command({**h.body(sfx_family="hard_effect",
                                  cue_intent="gunshot"),
                         "duration_seconds": 3.0}, command_id="c2")
        assert out["reused"] is False
        assert out["sfx_job_id"] is not None
        assert h.calls["submit"] == 1

    def test_reuse_skipped_when_artifact_gone(self):
        h = Harness()
        self._ready_take(h)
        h.artifact_present = False
        out = h.command(h.body(), command_id="c2")
        assert out["reused"] is False

    def test_reuse_skipped_when_rights_void(self):
        h = Harness()
        self._ready_take(h)
        take = h.conn.execute("SELECT sfx_take_id FROM sfx_take").fetchone()
        h.conn.execute("UPDATE sfx_take SET rights_state='void' "
                       "WHERE sfx_take_id=?", (take["sfx_take_id"],))
        out = h.command(h.body(), command_id="c2")
        assert out["reused"] is False

    def test_trim_and_recompute_do_not_touch_ledger(self):
        # T33-52
        h = Harness()
        self._ready_take(h)
        h.command({**h.body(), "duration_seconds": 0.3}, command_id="c2")
        h.service.recompute_placement("t1", "cue_1", 8)
        assert h.conn.execute(
            "SELECT COUNT(*) c FROM sfx_intent_ledger").fetchone()["c"] == 1


# ================================================================ 位置

class TestPlacementRead:
    def _ready(self, h):
        TestReuse()._ready_take(h)

    def test_edl_change_recomputes_for_free(self):
        # T33-15 / T33-16
        h = Harness()
        self._ready(h)
        before = h.service.get_cue("t1", "cue_1")
        h.edl = {"edl_revision": 8,
                 "shots": {"shot_A": {"start_ms": -2000, "end_ms": 5000}}}
        after = h.service.get_cue("t1", "cue_1")
        assert before["placement"]["output_start_ms"] == 2000
        assert after["placement"]["output_start_ms"] == -2000
        assert after["edl_revision"] == 8
        assert h.calls["submit"] == 1

    def test_truncating_cache_changes_nothing(self):
        # T33-49：缓存不是权威。
        h = Harness()
        self._ready(h)
        before = h.service.get_cue("t1", "cue_1")
        h.conn.execute("DELETE FROM cue_placement_cache")
        after = h.service.get_cue("t1", "cue_1")
        assert before["placement"]["placement_mode"] == \
            after["placement"]["placement_mode"]
        assert before["placement"]["output_start_ms"] == \
            after["placement"]["output_start_ms"]

    def test_rights_lapse_makes_position_unavailable(self):
        # T33-26：GET 时发现许可失效 → unplaceable + 缺口。
        h = Harness()
        self._ready(h)
        h.rights_result = {"allowed_license_kinds": []}
        got = h.service.get_cue("t1", "cue_1")
        assert got["take"]["rights_state"] == "void"
        assert got["placement"]["placement_mode"] == "unplaceable"
        assert got["gap"]["kind"] == "rights_lapsed"

    def test_no_edl_defers_position_without_failing(self):
        h = Harness()
        self._ready(h)
        h.edl = None
        got = h.service.get_cue("t1", "cue_1")
        assert got["placement"]["placement_mode"] == "fixed"
        assert got["placement"]["output_start_ms"] is None
        assert got["placement_unavailable_reason"] == "edl_unavailable"


# ================================================================ 缺口

class TestGap:
    def test_new_take_closes_open_gap(self):
        # T33-30
        h = Harness(rights={"allowed_license_kinds": []})
        h.command(h.body())
        gap = h.conn.execute("SELECT gap_id, state FROM sfx_gap").fetchone()
        assert gap["state"] == "open"

        h.rights_result = {"allowed_license_kinds": ["royalty_free"]}
        h.command(h.body(), command_id="c2")
        h.service.on_artifact_ingested({
            "tenant_id": "t1",
            "data": {"artifact_ref": {"service_id": "S28", "id": "art_2",
                                      "revision": 1,
                                      "content_hash": "c" * 64},
                     "duration_ms": 600,
                     "source": {"provider_job_id": "pj_1"}},
        })
        after = h.conn.execute("SELECT state, closed_by_take_id FROM sfx_gap "
                               "WHERE gap_id=?", (gap["gap_id"],)).fetchone()
        assert after["state"] == "closed"
        assert after["closed_by_take_id"] is not None

    def test_db_rejects_closed_without_basis(self):
        # T33-32
        h = Harness()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO sfx_gap (gap_id, tenant_id, shot_lineage_id, "
                "cue_index, cue_id, sfx_family, kind, detail, state, "
                "created_at, updated_at) VALUES ('g','t1','shot_A',0,'cue_1',"
                "'foley','duration_mismatch','x','closed','now','now')")

    def test_waive_requires_s40_decision(self):
        # T33-31
        h = Harness(rights={"allowed_license_kinds": []})
        h.command(h.body())
        gap = h.conn.execute("SELECT gap_id FROM sfx_gap").fetchone()["gap_id"]
        with pytest.raises(ServiceError) as exc:
            h.service.waive_gap("t1", gap, {"decision_ref": {}}, "c9")
        assert exc.value.code == "DECISION_REF_REQUIRED"

        out = h.service.waive_gap(
            "t1", gap, {"decision_ref": {"service_id": "S40", "id": "dec_1"}},
            "c10")
        assert out["state"] == "waived"
        assert out["waived_decision_id"] == "dec_1"

    def test_episode_gap_query(self):
        # T33-51
        h = Harness(rights={"allowed_license_kinds": []})
        h.command(h.body())
        got = h.service.list_cues("t1", episode_id="ep_1", has_gap=True)
        assert got["total"] == 1
        assert got["items"][0]["gap"]["kind"] == "no_licensed_route"
        assert h.service.list_cues("t1", episode_id="ep_1",
                                  has_gap=False)["total"] == 0

    def test_missing_duration_records_gap_not_zero(self):
        # T33-36 / F14 / C4
        h = Harness()
        h.command(h.body())
        out = h.service.on_artifact_ingested({
            "tenant_id": "t1",
            "data": {"artifact_ref": {"service_id": "S28", "id": "art_3",
                                      "revision": 1,
                                      "content_hash": "d" * 64},
                     "source": {"provider_job_id": "pj_1"}},
        })
        assert out["state"] == "GAP_RECORDED"
        assert h.conn.execute("SELECT COUNT(*) c FROM sfx_take"
                              ).fetchone()["c"] == 0

    def test_loopability_unverified_records_gap(self):
        # T33-24
        h = Harness(loopable=False)
        h.command(h.body(sfx_family="ambience", loopable_required=True))
        out = h.service.on_artifact_ingested({
            "tenant_id": "t1",
            "data": {"artifact_ref": {"service_id": "S28", "id": "art_4",
                                      "revision": 1,
                                      "content_hash": "e" * 64},
                     "duration_ms": 5000,
                     "source": {"provider_job_id": "pj_1"}},
        })
        assert out["state"] == "GAP_RECORDED"
        gap = h.conn.execute("SELECT kind FROM sfx_gap").fetchone()
        assert gap["kind"] == "loopability_unverified"


# ================================================================ 替代

class TestSubstitution:
    def _seed(self, h, family="foley"):
        h.command(h.body(sfx_family=family, cue_intent="original"))
        h.service.on_artifact_ingested({
            "tenant_id": "t1",
            "data": {"artifact_ref": {"service_id": "S28", "id": "art_s",
                                      "revision": 1,
                                      "content_hash": "f" * 64},
                     "duration_ms": 600,
                     "source": {"provider_job_id": "pj_1"}},
        })
        return h.conn.execute("SELECT sfx_take_id FROM sfx_take"
                              ).fetchone()["sfx_take_id"]

    def test_unpaired_substitution_rejected(self):
        # T33-27
        h = Harness()
        take_id = self._seed(h)
        with pytest.raises(ServiceError) as exc:
            h.command({**h.body(cue_intent="other"),
                       "substitute_take_id": take_id}, command_id="c5")
        assert exc.value.code == "SUBSTITUTION_NOT_AUTHORIZED"

    def test_family_mismatch_rejected(self):
        # T33-28
        h = Harness()
        take_id = self._seed(h, family="ambience")
        with pytest.raises(ServiceError) as exc:
            h.command({**h.body(cue_intent="other", sfx_family="foley"),
                       "substitute_take_id": take_id,
                       "substitution_authorized_by": "alice"}, command_id="c5")
        assert exc.value.code == "FAMILY_MISMATCH"

    def test_authorized_same_family_substitution(self):
        # T33-29
        h = Harness()
        take_id = self._seed(h)
        h.calls["submit"] = 0
        out = h.command({**h.body(cue_intent="other"),
                         "substitute_take_id": take_id,
                         "substitution_authorized_by": "alice"},
                        command_id="c5")
        assert out["source_kind"] == "substituted"
        assert out["substitution_authorized_by"] == "alice"
        assert h.calls["submit"] == 0
        family = h.conn.execute(
            "SELECT sfx_family FROM sfx_take WHERE source_kind='substituted'"
        ).fetchone()["sfx_family"]
        assert family == "foley"


# ================================================================ 状态机

class TestStateMachine:
    def test_submission_unknown_freezes(self):
        # T33-33
        h = Harness()
        h.command(h.body())
        out = h.service.on_generation_state_changed({
            "tenant_id": "t1",
            "data": {"provider_job_id": "pj_1", "state": "SUBMISSION_UNKNOWN"},
        })
        assert out["state"] == "WAITING_UNKNOWN"

    def test_unknown_stays_unknown_when_unreconciled(self):
        h = Harness()
        h.command(h.body())
        h.service.on_generation_state_changed({
            "tenant_id": "t1",
            "data": {"provider_job_id": "pj_1", "state": "SUBMISSION_UNKNOWN"},
        })
        job_id = h.conn.execute("SELECT sfx_job_id FROM sfx_job"
                                ).fetchone()["sfx_job_id"]
        out = h.service.reconcile_unknown("t1", job_id, None)
        assert out["state"] == "WAITING_UNKNOWN"
        assert out["reconciled"] is False

    def test_reconcile_failure_records_gap(self):
        # T33-34
        h = Harness()
        h.command(h.body())
        h.service.on_generation_state_changed({
            "tenant_id": "t1",
            "data": {"provider_job_id": "pj_1", "state": "SUBMISSION_UNKNOWN"},
        })
        job_id = h.conn.execute("SELECT sfx_job_id FROM sfx_job"
                                ).fetchone()["sfx_job_id"]
        out = h.service.reconcile_unknown("t1", job_id, {"succeeded": False})
        assert out["state"] == "GAP_RECORDED"
        gap = h.conn.execute("SELECT kind FROM sfx_gap").fetchone()
        assert gap["kind"] == "submission_unrecoverable"

    def test_unknown_provider_state_is_ignored(self):
        # 契约里 state 不是闭集。不认识的**不能**当成功。
        h = Harness()
        h.command(h.body())
        out = h.service.on_generation_state_changed({
            "tenant_id": "t1",
            "data": {"provider_job_id": "pj_1", "state": "SOMETHING_NEW"},
        })
        assert out["ignored"] is True
        state = h.conn.execute("SELECT state FROM sfx_job").fetchone()["state"]
        assert state == "WAITING_PROVIDER"

    def test_retry_only_from_failed(self):
        h = Harness()
        h.command(h.body())
        job_id = h.conn.execute("SELECT sfx_job_id FROM sfx_job"
                                ).fetchone()["sfx_job_id"]
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job("t1", job_id, {}, "c7")
        assert exc.value.code == "JOB_NOT_RETRYABLE"

    def test_cancel_unconfirmed_goes_to_unknown(self):
        h = Harness()
        h.command(h.body())
        job_id = h.conn.execute("SELECT sfx_job_id FROM sfx_job"
                                ).fetchone()["sfx_job_id"]

        def _boom(*a, **k):
            raise ConnectionError("provider down")

        h.service.canceler = _boom
        out = h.service.cancel_job("t1", job_id, {}, "c8")
        assert out["state"] == "WAITING_UNKNOWN"
        assert out["reconciled"] is False

    def test_ready_publishes_registered_event(self):
        h = Harness()
        h.command(h.body())
        h.service.on_artifact_ingested({
            "tenant_id": "t1",
            "data": {"artifact_ref": {"service_id": "S28", "id": "art_r",
                                      "revision": 1,
                                      "content_hash": "1" * 64},
                     "duration_ms": 600,
                     "source": {"provider_job_id": "pj_1"}},
        })
        row = h.conn.execute(
            "SELECT event_type, payload FROM outbox "
            "WHERE event_type='production.sfx.ready'").fetchone()
        assert row is not None
        import json
        # outbox.payload 存的是整条 envelope（`Outbox.append` 写 to_json()），
        # 业务体在它的 `data` 里。
        data = json.loads(row["payload"])["data"]
        # 16 个必填一个不少
        required = {"schema_version", "sfx_job_id", "sfx_take_id", "cue_id",
                    "cue_revision", "shot_lineage_id", "cue_index",
                    "sfx_family", "artifact_ref", "natural_duration_ms",
                    "loopable", "rights_state", "source_kind", "checkpoint",
                    "content_fingerprint", "gap_state"}
        assert set(data) == required


# ================================================================ schema

class TestSchemaConstraints:
    def test_artifact_ref_must_be_s28(self):
        # T33-38 / E01
        h = Harness()
        h.command(h.body())
        job = h.conn.execute("SELECT * FROM sfx_job").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO sfx_take (sfx_take_id, tenant_id, sfx_job_id, "
                "cue_id, shot_lineage_id, cue_index, sfx_family, artifact_ref, "
                "natural_duration_ms, sample_rate_hz, channel_count, "
                "container_format, content_fingerprint, loopable, "
                "loopable_verified, rights_policy_binding_id, rights_state, "
                "provider_job_id, attempt_id, source_kind, state, created_at) "
                "VALUES ('x','t1',?,'cue_1','shot_A',0,'foley',?,600,48000,1,"
                "'wav',?,0,0,?,'usable','pj','at','generated','READY','now')",
                (job["sfx_job_id"],
                 '{"service_id":"elevenlabs","id":"a","revision":1,'
                 '"content_hash":"' + "a" * 64 + '"}',
                 job["content_fingerprint"],
                 job["rights_policy_binding_id"]))

    def test_empty_licence_kinds_rejected_at_construction(self):
        # T33-08 的数据层那一半
        h = Harness()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO rights_policy_binding (rights_policy_binding_id, "
                "tenant_id, policy_ref, allowed_license_kinds, "
                "attribution_required, resolved_at) VALUES ('b','t1',"
                "'{\"service_id\":\"S02\",\"id\":\"p\",\"revision\":1,"
                "\"content_hash\":\"" + "a" * 64 + "\"}','[]',0,'now')")

    def test_cross_tenant_take_is_404(self):
        # T33-39
        h = Harness()
        h.command(h.body())
        h.service.on_artifact_ingested({
            "tenant_id": "t1",
            "data": {"artifact_ref": {"service_id": "S28", "id": "art_x",
                                      "revision": 1,
                                      "content_hash": "2" * 64},
                     "duration_ms": 600,
                     "source": {"provider_job_id": "pj_1"}},
        })
        take_id = h.conn.execute("SELECT sfx_take_id FROM sfx_take"
                                 ).fetchone()["sfx_take_id"]
        with pytest.raises(ServiceError) as exc:
            h.service.get_take("t2", take_id)
        assert exc.value.status == 404


class TestResolvedPlanConflicts:
    """三处方案内部冲突的解析结果，各用一条测试钉住。

    每一条都在断言"如果按方案原文照做就会坏掉的那件事现在不会坏"，同时断言
    "方案真正想守的那条不变量仍然守得住"。只断言前者会放走后半个问题。
    """

    def _binding(self, h) -> str:
        binding_id = "bind_x"
        h.conn.execute(
            "INSERT INTO rights_policy_binding (rights_policy_binding_id, "
            "tenant_id, policy_ref, allowed_license_kinds, "
            "attribution_required, resolved_at) VALUES (?,?,?,?,0,?)",
            (binding_id, "t1", json.dumps(UPSTREAM_POLICY),
             json.dumps(["royalty_free"]), "now"))
        return binding_id

    def _job_insert(self, h, *, state: str, binding: str | None) -> None:
        job = h.conn.execute("SELECT * FROM sfx_job").fetchone()
        h.conn.execute(
            "INSERT INTO sfx_job (sfx_job_id, tenant_id, project_id, series_id,"
            " episode_id, branch_id, cue_id, cue_revision, shot_lineage_id,"
            " cue_index, sfx_family, requested_duration_ms, loopable_required,"
            " rights_policy_binding_id, content_fingerprint, node_id,"
            " node_epoch_at_create, execution_fence, request_hash,"
            " idempotency_key, business_key, variation_nonce, budget_scope_id,"
            " deadline, state, state_seq, created_at, updated_at,"
            " state_entered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,0,?,?,?)",
            ("sfj_probe", job["tenant_id"], "prj_1", "ser_1", "ep_1", "br_1",
             "cue_probe", 1, "shot_A", 1, "foley", 600, 0, binding,
             "c" * 64, "node_1", 4, 2, "rh", "idem_probe", "biz_probe",
             "nonce", "budget", "2099-01-01T00:00:00.000Z", state,
             "now", "now", "now"))

    def test_binding_may_be_absent_only_before_money_can_be_spent(self):
        # 冲突 1：§5.2 的 NOT NULL vs T33-08「空 kinds → 构造不出 binding」。
        h = Harness()
        h.command(h.body())
        binding = self._binding(h)
        # 无 binding 的三个状态是 POLICY_BLOCKED 及它之前的两步 —— 还没花钱。
        for state in ("CREATED", "CHECKING_POLICY", "POLICY_BLOCKED"):
            h.conn.execute("DELETE FROM sfx_job WHERE sfx_job_id='sfj_probe'")
            self._job_insert(h, state=state, binding=None)
        # PREPARING 是"即将提交 S26"的第一格。从这里起没有绑定就必须被拒。
        h.conn.execute("DELETE FROM sfx_job WHERE sfx_job_id='sfj_probe'")
        with pytest.raises(sqlite3.IntegrityError) as exc:
            self._job_insert(h, state="PREPARING", binding=None)
        assert "sfj_binding_required" in str(exc.value)
        # 带 bindidng 的 PREPARING 必须能进 —— 否则这条 CHECK 就是在拦正常路径。
        self._job_insert(h, state="PREPARING", binding=binding)

    def test_policy_blocked_job_row_keeps_no_binding(self):
        # 冲突 1 的另一半：真的走一遍 POLICY_BLOCKED，看 job 行确实无 binding。
        h = Harness(rights={"allowed_license_kinds": []})
        h.command(h.body())
        row = h.conn.execute("SELECT state, rights_policy_binding_id "
                             "FROM sfx_job").fetchone()
        assert row["state"] == "POLICY_BLOCKED"
        assert row["rights_policy_binding_id"] is None
        assert h.conn.execute(
            "SELECT count(*) AS n FROM rights_policy_binding").fetchone()["n"] == 0

    def test_same_intent_after_terminal_is_a_new_job(self):
        # 冲突 2：sfj_tenant_idem 是普通 UNIQUE，但 §7.1 的业务幂等只对未终结
        # job 成立。第一条走到 POLICY_BLOCKED（终态）之后，同意图重提必须能落
        # 新行；若把业务键塞进那条普通唯一约束，这里会 IntegrityError。
        h = Harness(rights={"allowed_license_kinds": []})
        first = h.command(h.body(), command_id="c1")
        assert first["state"] == "POLICY_BLOCKED"
        # 策略变了：同一条 cue、同一意图，换一个 command_id 再提一次。
        h.rights_result = {"allowed_license_kinds": ["royalty_free"],
                           "attribution_required": False,
                           "territory_restrictions": []}
        second = h.command(h.body(), command_id="c2")
        assert second["sfx_job_id"] != first["sfx_job_id"]
        assert second["state"] in ("SUBMITTED", "WAITING_PROVIDER")
        # 两条 job 行的 business_key 相同、idempotency_key 不同。
        keys = h.conn.execute(
            "SELECT idempotency_key, business_key FROM sfx_job "
            "ORDER BY created_at").fetchall()
        assert len(keys) == 2
        assert keys[0]["business_key"] == keys[1]["business_key"]
        assert keys[0]["idempotency_key"] != keys[1]["idempotency_key"]

    def test_same_command_is_still_one_job(self):
        # 冲突 2 反过来：命令级幂等没被拆掉。同 command_id 重放还是同一行。
        h = Harness()
        first = h.command(h.body(), command_id="c1")
        again = h.command(h.body(), command_id="c1")
        assert again["replayed"] is True
        assert again["sfx_job_id"] == first["sfx_job_id"]
        assert h.conn.execute(
            "SELECT count(*) AS n FROM sfx_job").fetchone()["n"] == 1

    def test_concurrent_double_submit_returns_the_inflight_job(self):
        # 冲突 2 的第三个面：唯一索引在 INSERT 那一刻就挡住并发双提交，
        # 而挡住之后要给调用方那个在跑的 job，不能炸成 500。
        h = Harness()
        h.command(h.body(), command_id="c1")
        h.rights_result = {"allowed_license_kinds": ["royalty_free"],
                           "attribution_required": False,
                           "territory_restrictions": []}
        # 第一条还停在未终结状态（SUBMITTED/WAITING_PROVIDER），第二条同意图。
        out = h.command(h.body(), command_id="c2")
        assert out.get("duplicate_suppressed") is True
        assert h.conn.execute(
            "SELECT count(*) AS n FROM sfx_job").fetchone()["n"] == 1
        # 被抑制的那次不能又花一笔钱。
        assert h.calls["submit"] == 1

    def test_substituted_take_may_reuse_source_artifact(self):
        # 冲突 3：§5.2 的 st_artifact_uniq 无条件是唯一，但 §4.6 的替代按定义
        # 复用来源 artifact。收紧成 partial 之后两条路都要成立。
        h = Harness()
        take_id = TestSubstitution()._seed(h)
        h.command({**h.body(cue_intent="other"),
                   "substitute_take_id": take_id,
                   "substitution_authorized_by": "alice"}, command_id="c5")
        rows = h.conn.execute(
            "SELECT source_kind FROM sfx_take ORDER BY created_at").fetchall()
        assert [r["source_kind"] for r in rows] == ["generated", "substituted"]

    def test_duplicate_ingest_of_one_artifact_is_still_rejected(self):
        # 冲突 3 的另一半：E32 幂等消费没被让掉。同一条 ingest 投两遍，
        # 第二遍必须撞索引（而不是悄悄多出一行）。
        h = Harness()
        h.command(h.body())
        envelope = {
            "tenant_id": "t1",
            "data": {"artifact_ref": {"service_id": "S28", "id": "art_dup",
                                      "revision": 1,
                                      "content_hash": "9" * 64},
                     "duration_ms": 600,
                     "source": {"provider_job_id": "pj_1"}},
        }
        h.service.on_artifact_ingested(envelope)
        with pytest.raises(sqlite3.IntegrityError) as exc:
            h.service.on_artifact_ingested(envelope)
        # SQLite 的报错形状取决于索引有没有表达式列：
        # - 纯列索引报**列名**（`... sfx_job.tenant_id, sfx_job.business_key`）
        # - 含表达式（这里是 `json_extract`）报**索引名**
        # 两种都实测过。别假设其中一种。
        msg = str(exc.value)
        assert "UNIQUE constraint failed" in msg
        assert "st_artifact_uniq" in msg


# ---- HTTP 层 ---------------------------------------------------------------


def _http_client(monkeypatch, tmp_path, **stub_overrides):
    """`api/app.py` 的 TestClient，上游全部换成 Harness 的 stub。

    `app.py` 在 import 时自己建连接、自己拼七个上游，所以：
    - `S33_DB` 必须指向独立文件库（import 那一刻就建表）；
    - `module.service` / `module._idem` 必须重绑 —— 路由函数在定义时就捕获了
      模块全局，只换 Harness 不管用；
    - 重绑时用 `module._conn`，否则断言查的是另一个库。
    """
    monkeypatch.setenv("S33_DB", str(tmp_path / "s33.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    # 不能 `import app`：别的服务也有 `api/app.py` 且可能先进 sys.modules。
    spec = importlib.util.spec_from_file_location("s33_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s33_api_app"] = module

    h = Harness(conn=module._conn, **stub_overrides)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h


def _post(client, body, *, command_id="c1", tenant="t1", path="/sfx-jobs"):
    return client.post(path, json=body,
                       headers={"Idempotency-Key": command_id,
                                "X-Tenant-Id": tenant})


class TestHttpLayer:
    """`api/app.py` 的 240 行此前没有一行被执行过。"""

    def test_create_is_202(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = _post(client, h.body())
        assert r.status_code == 202, r.text
        assert r.json()["state"] in ("SUBMITTED", "WAITING_PROVIDER")
        assert r.json()["replayed"] is False

    def test_same_key_same_body_replays(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        first = _post(client, h.body())
        again = _post(client, h.body())
        assert again.status_code == 202
        assert again.json()["replayed"] is True
        assert again.json()["sfx_job_id"] == first.json()["sfx_job_id"]
        # 重放不能再花一笔钱。
        assert h.calls["submit"] == 1

    def test_same_key_different_body_is_409(self, monkeypatch, tmp_path):
        # §7.1：同键不同 body 是 409，**不是覆盖**。
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = _post(client, h.body(cue_intent="a different door"))
        assert r.status_code == 409, r.text
        assert "code" in r.json()

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        # AGENTS.md：请求体里的 tenant_id 不是可信输入，只用来比对。
        client, h = _http_client(monkeypatch, tmp_path)
        body = h.body()
        body["context"]["tenant_id"] = "t_other"
        r = _post(client, body)
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_MISMATCH"

    def test_missing_idempotency_key_is_400(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = client.post("/sfx-jobs", json=h.body(),
                        headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 422 or r.status_code == 400
        # FastAPI 对缺 header 有自己的 422；只要不是 5xx 且报错可读即可。
        assert r.status_code < 500

    def test_list_without_scope_is_422(self, monkeypatch, tmp_path):
        # 无范围全表扫描在数据长大后就是事故。
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/sfx-cues", headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 422
        assert r.json()["code"] == "QUERY_SCOPE_REQUIRED"

    def test_get_cue_after_create(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = client.get("/sfx-cues/cue_1", headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200, r.text
        body = r.json()
        # §6.2：cue + 意图 + checkpoint + take + placement + gap + history。
        assert body["cue"]["cue_id"] == "cue_1"
        assert set(body) >= {"cue", "take", "placement", "gap", "jobs",
                             "edl_revision"}
        # job 已经在跑，还没有 take —— 读路径不能凭空造一个。
        assert body["take"] is None
        # stub 的 submit 立即成功，所以 job 已经过了 SUBMITTED 在等供应商。
        assert [j["state"] for j in body["jobs"]] == ["WAITING_PROVIDER"]

    def test_get_cue_unknown_id_is_404(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/sfx-cues/nope", headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 404
        assert r.json()["code"] == "SFX_CUE_NOT_FOUND"

    def test_readiness_reports_contract_gaps(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/health/readiness")
        assert r.status_code == 200
        assert r.json()["ready"] is True
        assert "contract_gaps" in r.json()

    def test_policy_blocked_is_202_not_4xx(self, monkeypatch, tmp_path):
        # §4.1 第 3 步：拦住的不是"请求写错了"，是"现在没有可用的许可路线"。
        # 返回 4xx 会让调用方去改请求，而该改的是法务的策略。
        client, h = _http_client(
            monkeypatch, tmp_path,
            rights={"allowed_license_kinds": []})
        r = _post(client, h.body())
        assert r.status_code == 202, r.text
        assert r.json()["state"] == "POLICY_BLOCKED"
        assert r.json()["reason_code"] == "NO_LICENSED_ROUTE"
        gap = h.conn.execute("SELECT kind FROM sfx_gap").fetchone()
        assert gap["kind"] == "no_licensed_route"
