"""S13 单测。用例编号对应 `plans/S13-scene-assets.md` §8 的 E02/E05/E08/E09/
E11/E12/E24/E28/E30 与 §4.4 的 F1–F18。

上游 S28 是注入的 stub，**按 `services/artifact-ingest` 的真实响应形状写**
（`{artifact_id, revision, content_hash, …}`），不按内核希望的样子写——把
stub 写成"总是返回一个 hash"，就永远测不出 F16 与 F13 的分界。

几处刻意"把缺口钉进测试"的地方：

- `TOPOLOGY_INVALID_FOR_PRIMARY` 今天**无法**经 draft 路径触发：draft 的第 1
  步已经把非法拓扑拒了，而真正的 `spatial_check` 应由 QC 服务产出（C13-2，
  那个服务不存在）。所以用例直接插一行 `topology_valid=false` 的候选，
  证明 accept 侧的门是活的——而不是假装端到端能走到。
- `S13-12`：接受一个非 canonical 的 revision 后 `topology_hash` 仍然描述
  **旧的 canonical**。这条是行为断言，不是注释。
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

from scene_assets import geometry
from scene_assets.service import (
    EVENT_SCENE_REVISION_ACCEPTED,
    SOURCE,
    SceneService,
    ServiceError,
    revision_content_hash,
    topology_hash,
)

GOOD_HASH = "a" * 64
RIGHTS = {"service_id": "S02", "id": "rg_1", "revision": 1,
          "content_hash": "b" * 64}
QUALITY = {"service_id": "S36", "id": "qg_1", "revision": 1}


# ------------------------------------------------------------------ 夹具形状

def topology(**over) -> dict:
    """§2.3 的示例拓扑，两块相邻空间 + 一扇门 + 一扇窗。"""
    base = {
        "units": [
            {"unit_id": "office_main", "kind": "room",
             "polygon": [[0, 0], [6, 0], [6, 4], [0, 4]]},
            {"unit_id": "corridor_a", "kind": "corridor",
             "polygon": [[6, 1], [9, 1], [9, 3], [6, 3]]},
        ],
        "openings": [
            {"opening_id": "door_1", "kind": "door", "unit_a": "office_main",
             "unit_b": "corridor_a", "wall_segment": [[6, 1.2], [6, 2.2]],
             "width_cm": 100, "swing": "inward_left"},
            {"opening_id": "win_1", "kind": "window", "unit_a": "office_main",
             "wall_segment": [[2, 0], [4, 0]], "sill_cm": 90,
             "height_cm": 140},
        ],
        "orientation": {"north_axis_deg": 0, "unit_north_ref": "office_main"},
        "structures": [],
        "walkable": {"office_main": "polygon_ref", "corridor_a": "polygon_ref"},
        "scale_unit": "meter",
        "source_trace": {"method": "floorplan_import", "ref": None},
    }
    base.update(over)
    return base


def moved_door() -> dict:
    """把 door_1 沿墙向南挪 0.8m。仍在两块空间的边界上，所以拓扑合法，
    但 `topology_hash` 变了——E08-D1 需要的正是这种改动。"""
    t = topology()
    t["openings"][0]["wall_segment"] = [[6, 2.2], [6, 3.0]]
    return t


def anchor(anchor_id: str, *, x=3.0, y=2.0, yaw=0.0, unit="office_main",
           **over) -> dict:
    base = {
        "position_m": {"x": x, "y": y, "z": 1.6},
        "yaw_deg": yaw, "pitch_deg": 0.0, "hfov_deg": 60.0,
        "unit_id": unit,
        "artifact_ref": {"service_id": "S28", "id": f"art_{anchor_id}",
                         "revision": 1, "content_hash": GOOD_HASH},
    }
    base.update(over)
    return base


def four_anchors() -> dict:
    """primary_set 的四个正交方向。`yaw_spread` 得 180°，过 S13-8 的 30° 门槛。"""
    return {
        "front": anchor("front", x=3, y=1, yaw=180),
        "back": anchor("back", x=3, y=3, yaw=0),
        "left": anchor("left", x=1, y=2, yaw=90),
        "right": anchor("right", x=5, y=2, yaw=270),
    }


def lighting_for(anchors: dict, axes=("day", "night")) -> dict:
    """每个 anchor 在非基准轴上各有一个变体。基准轴（day）用 anchor 自己的
    `artifact_ref`，所以 manifest 里只出现 night。"""
    return {
        anchor_id: {
            axis: {"service_id": "S28", "id": f"art_{anchor_id}_{axis}",
                   "revision": 1, "content_hash": GOOD_HASH}
            for axis in axes if axis != "day"
        }
        for anchor_id in anchors
    }


def desk(**over) -> dict:
    base = {"object_id": "desk_1", "label": "办公桌", "unit_id": "office_main",
            "footprint": [[1, 1], [2, 1], [2, 2], [1, 2]],
            "height_cm": 75, "is_load_bearing": False, "blocks_walkable": True}
    base.update(over)
    return base


def register_body(**over) -> dict:
    body = {"project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "办公室", "scene_kind": "interior",
            "era": {"label": "contemporary"},
            "location_class": "primary_set", "created_by": "u_1"}
    body.update(over)
    return body


def draft_body(**over) -> dict:
    anchors = over.pop("anchor_manifest", four_anchors())
    body = {
        "expected_current_revision": 0,
        "source_kind": "imported",
        "era_snapshot": {"label": "contemporary"},
        "topology": topology(),
        "anchor_manifest": anchors,
        "lighting_manifest": lighting_for(anchors),
        "lighting_axes": ["day", "night"],
        "rights_ref": dict(RIGHTS),
        "quality_gate_ref": dict(QUALITY),
        "fixed_objects": [desk()],
        "topology_changed": False,
    }
    body.update(over)
    return body


def stub_artifact_source(_tenant_id, ref):
    """只返回成功头的 S28 stub，供不走 `Harness` 的用例（并发 / 重启）使用。"""
    return {"artifact_id": ref.get("id"), "revision": 1,
            "content_hash": GOOD_HASH, "size_bytes": 1024,
            "mime_type": "image/png"}


class Harness:
    """内存库 + S28 stub + 与 `api/app.py` 同形的幂等外壳。"""

    def __init__(self, conn=None, *, artifact_source="default", **over):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)
        self.artifacts = over.get("artifacts", {})
        self.calls = {"artifact": 0, "artifact_ids": []}
        if artifact_source == "default":
            artifact_source = self._artifact
        elif artifact_source == "unconfigured":
            artifact_source = None
        self.service = SceneService(
            self.conn, artifact_source=artifact_source,
            authorizer=over.get("authorizer"))

    # ---- S28 stub：`{artifact_id, revision, content_hash, …}` ----
    def _artifact(self, tenant_id, ref):
        self.calls["artifact"] += 1
        self.calls["artifact_ids"].append(ref.get("id"))
        value = self.artifacts.get(ref.get("id"), GOOD_HASH)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ServiceError(404, "ARTIFACT_NOT_FOUND",
                               f"artifact {ref.get('id')} is gone")
        return {"artifact_id": ref.get("id"), "revision": 1,
                "content_hash": value, "size_bytes": 1024,
                "mime_type": "image/png"}

    # ---- 场景与命令体 ----
    def create(self, *, tenant_id="t1", command_id="c1", **over):
        return self.service.create_scene(tenant_id, command_id,
                                         register_body(**over))

    def scene(self, **over) -> str:
        return self.create(**over)["scene_asset_id"]

    def draft_body(self, **over) -> dict:
        return draft_body(**over)

    def draft(self, scene_id, *, tenant_id="t1", command_id="c2", **over):
        return self.service.draft_candidate(
            tenant_id, command_id, scene_id, draft_body(**over))

    def accept(self, scene_id, candidate_id, *, tenant_id="t1",
               command_id="c3", **over):
        body = {"expected_current_revision": over.pop("expected", 0),
                "set_canonical": over.pop("set_canonical", True),
                "decision_kind": over.pop("decision_kind", "initial_import")}
        body.update(over)
        return self.service.accept_candidate(
            tenant_id, command_id, scene_id, candidate_id, body)

    def rows(self, table, **where):
        clause = " AND ".join(f"{k}=?" for k in where) or "1"
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {clause}",
            tuple(where.values())).fetchall()

    def events(self):
        return [json.loads(r["payload"]) for r in
                self.conn.execute("SELECT payload FROM outbox").fetchall()]


def code_of(exc: ServiceError) -> str:
    return exc.code


# ================================================================ 登记场景

class TestCreateScene:
    def test_new_scene_starts_at_revision_zero(self):
        h = Harness()
        result = h.create()
        assert result["existing"] is False
        assert result["revision"] == 0
        assert result["scene_asset_id"].startswith("scn_")
        # 没有 canonical、也没有 topology_hash：这两列描述的是"当前 canonical
        # 长什么样"，一个还没接受过 revision 的场景没有答案。
        row = h.rows("scene_asset")[0]
        assert row["canonical_revision"] is None
        assert row["topology_hash"] is None

    def test_duplicate_registration_returns_existing_id(self):
        """E12：重复登记返回既有 ID，不是错误。"""
        h = Harness()
        first = h.create()
        second = h.create(command_id="c2")
        assert second["scene_asset_id"] == first["scene_asset_id"]
        assert second["existing"] is True
        assert len(h.rows("scene_asset")) == 1

    def test_same_name_different_identity_is_409(self):
        """S13-4：同名但换了地点/年代的是另一个空间，不能悄悄复用一行。"""
        h = Harness()
        h.create()
        with pytest.raises(ServiceError) as info:
            h.create(command_id="c2", era={"label": "1990s"})
        assert code_of(info.value) == "SCENE_IDENTITY_CONFLICT"
        assert info.value.status == 409

    def test_same_name_two_tenants_are_independent(self):
        """E28-D2。"""
        h = Harness()
        a = h.create(tenant_id="t1")
        b = h.create(tenant_id="t2")
        assert a["scene_asset_id"] != b["scene_asset_id"]

    @pytest.mark.parametrize("over", [
        {"scene_kind": "space"},          # 不在 §2.1 的 CHECK 里
        {"location_class": "hero"},
        {"era": {"label": "x", "period": "y"}},  # 事件的 era 不认这个键
        {"display_name": ""},
        {"created_by": None},
    ])
    def test_malformed_registration(self, over):
        h = Harness()
        with pytest.raises(ServiceError) as info:
            h.create(**over)
        assert info.value.status == 400
        assert code_of(info.value) == "MALFORMED_REQUEST"


# ================================================================ 起草候选

class TestDraftCandidate:
    def test_happy_path_records_both_hashes_and_a_spatial_check(self):
        h = Harness()
        sid = h.scene()
        result = h.draft(sid)
        assert result["state"] == "PENDING"
        assert result["content_hash"] == revision_content_hash(
            h.draft_body())
        assert result["topology_hash"] == topology_hash(topology())
        assert result["spatial_check"]["topology_valid"] is True
        assert result["spatial_check"]["openings_attached"] is True
        # S13 不判越轴：`axis_check` 只带输入（北向），不带结论。
        assert result["spatial_check"]["axis_check"]["checked_by"] is None
        assert result["spatial_check"]["axis_check"]["north_axis_deg"] == 0
        # 候选不落 revision。
        assert h.rows("scene_revision") == []

    def test_candidate_expires_in_thirty_days(self):
        h = Harness()
        sid = h.scene()
        result = h.draft(sid)
        created = h.rows("scene_candidate")[0]["created_at"]
        assert result["expires_at"] > created

    def test_artifact_source_is_asked_once_per_reference(self):
        # 4 个 anchor × (day 用自身 ref) + 4 个 night 变体 = 8 次。
        h = Harness()
        h.draft(h.scene())
        assert h.calls["artifact"] == 8
        assert len(set(h.calls["artifact_ids"])) == 8

    def test_topology_invalid_names_the_offending_object(self):
        """F1 / §11.1 第 2 条：错误要定位到具体 opening。"""
        h = Harness()
        bad = topology()
        bad["openings"][0]["wall_segment"] = [[1, 1], [2, 2]]  # 不再贴墙
        with pytest.raises(ServiceError) as info:
            h.draft(h.scene(), topology=bad)
        assert info.value.status == 422
        assert code_of(info.value) == "TOPOLOGY_INVALID"
        assert info.value.details["target"] == "door_1"

    @pytest.mark.parametrize("mutate,target", [
        (lambda t: t["units"][0].update(polygon=[[0, 0], [6, 0], [3, 0]]),
         "office_main"),
        (lambda t: t["units"][1].update(
            polygon=[[5, 0], [9, 0], [9, 3], [5, 3]]), "office_main"),
        (lambda t: t["openings"][0].update(unit_b="nowhere"), "door_1"),
        (lambda t: t["openings"][1].update(unit_a="nowhere"), "win_1"),
        (lambda t: t["walkable"].pop("corridor_a"), "walkable"),
        (lambda t: t.pop("orientation"), "orientation"),
        (lambda t: t.update(scale_unit="centimeter"), "scale_unit"),
        (lambda t: t["structures"].append(
            {"structure_id": "col_9", "kind": "column",
             "footprint": [[100, 100], [101, 100], [101, 101], [100, 101]]}),
         "col_9"),
    ])
    def test_topology_variants_are_rejected_with_a_target(self, mutate, target):
        h = Harness()
        bad = topology()
        mutate(bad)
        with pytest.raises(ServiceError) as info:
            h.draft(h.scene(), topology=bad)
        assert code_of(info.value) == "TOPOLOGY_INVALID"
        assert info.value.details["target"] == target

    def test_anchor_count_depends_on_location_class(self):
        """F2 / E02-D4：`one_off` 一个 anchor 就够，`primary_set` 要四个。"""
        h = Harness()
        sid = h.scene(location_class="one_off")
        one = {"front": anchor("front", x=3, y=1, yaw=0)}
        assert h.draft(sid, anchor_manifest=one,
                       lighting_manifest=lighting_for(one))["state"] == "PENDING"

        h2 = Harness()
        sid2 = h2.scene(location_class="primary_set")
        with pytest.raises(ServiceError) as info:
            h2.draft(sid2, anchor_manifest=one,
                     lighting_manifest=lighting_for(one))
        assert code_of(info.value) == "ANCHOR_COVERAGE_INSUFFICIENT"
        assert info.value.details["required"] == 4
        assert info.value.details["actual"] == 1

    def test_primary_set_anchors_must_span_360(self):
        """S13-8：四个 anchor 全朝同一面墙不算「覆盖 360°」。"""
        h = Harness()
        same_way = {name: anchor(name, x=x, y=2, yaw=0)
                    for name, x in (("a", 1), ("b", 2), ("c", 4), ("d", 5))}
        with pytest.raises(ServiceError) as info:
            h.draft(h.scene(), anchor_manifest=same_way,
                    lighting_manifest=lighting_for(same_way))
        assert code_of(info.value) == "ANCHOR_COVERAGE_INSUFFICIENT"
        assert info.value.details["yaw_spread_deg"] == 0.0

    def test_anchor_outside_its_unit(self):
        """F3。注意 anchor 可能落在**另一个**合法 unit 里，仍然算错——
        `unit_id` 说的是相机在哪，不是「附近某个空间」。"""
        h = Harness()
        anchors = four_anchors()
        anchors["front"] = anchor("front", x=7.5, y=2, yaw=180,
                                  unit="office_main")
        with pytest.raises(ServiceError) as info:
            h.draft(h.scene(), anchor_manifest=anchors,
                    lighting_manifest=lighting_for(anchors))
        assert code_of(info.value) == "ANCHOR_OUTSIDE_UNIT"
        assert info.value.details["anchor_id"] == "front"

    def test_anchor_pointing_at_a_unit_that_does_not_exist(self):
        h = Harness()
        anchors = four_anchors()
        anchors["left"] = anchor("left", x=1, y=2, yaw=90, unit="attic")
        with pytest.raises(ServiceError) as info:
            h.draft(h.scene(), anchor_manifest=anchors,
                    lighting_manifest=lighting_for(anchors))
        assert code_of(info.value) == "ANCHOR_OUTSIDE_UNIT"

    def test_lighting_matrix_must_be_complete(self):
        """F4 / E02-D2：缺一格就拒，不在接受后再发现。"""
        h = Harness()
        manifest = lighting_for(four_anchors())
        manifest["back"].pop("night")
        with pytest.raises(ServiceError) as info:
            h.draft(h.scene(), lighting_manifest=manifest)
        assert code_of(info.value) == "LIGHTING_COVERAGE_INCOMPLETE"
        assert info.value.details["anchor_id"] == "back"
        assert info.value.details["lighting"] == "night"

    def test_duplicate_lighting_axis_is_malformed(self):
        h = Harness()
        with pytest.raises(ServiceError) as info:
            h.draft(h.scene(), lighting_axes=["day", "night", "day"])
        assert info.value.status == 400

    def test_fixed_object_out_of_bounds(self):
        """F5。"""
        h = Harness()
        with pytest.raises(ServiceError) as info:
            h.draft(h.scene(),
                    fixed_objects=[desk(footprint=[[5, 3], [7, 3], [7, 4],
                                                   [5, 4]])])
        assert code_of(info.value) == "FIXED_OBJECT_OUT_OF_BOUNDS"
        assert info.value.details["object_id"] == "desk_1"

    def test_topology_change_undeclared(self):
        """F6。"""
        h = Harness()
        sid = h.scene()
        candidate_id = h.draft(sid)["candidate_id"]
        h.accept(sid, candidate_id)
        with pytest.raises(ServiceError) as info:
            h.draft(sid, command_id="c4", expected_current_revision=1,
                    topology=moved_door(), topology_changed=False)
        assert code_of(info.value) == "TOPOLOGY_CHANGE_NOT_DECLARED"

    def test_topology_change_declared_but_none(self):
        """F7。"""
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        with pytest.raises(ServiceError) as info:
            h.draft(sid, command_id="c4", expected_current_revision=1,
                    topology_changed=True)
        assert code_of(info.value) == "TOPOLOGY_CHANGE_DECLARED_BUT_NONE"

    def test_backfill_that_keeps_topology_needs_no_declaration(self):
        """E02-D4 / E09-D2：补一张 from_door anchor，拓扑不变。"""
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        anchors = four_anchors()
        anchors["from_door"] = anchor("from_door", x=5.5, y=2, yaw=270)
        result = h.draft(sid, command_id="c4", expected_current_revision=1,
                         anchor_manifest=anchors,
                         lighting_manifest=lighting_for(anchors),
                         topology_changed=False)
        assert result["topology_changed"] is False
        assert len(result["spatial_check"]["anchor_coverage"]) == 3

    def test_artifact_missing_in_s28_is_422(self):
        """F13：S28 说没有，是调用方的错（换素材），不是基础设施故障。"""
        h = Harness(artifacts={"art_front": None})
        with pytest.raises(ServiceError) as info:
            h.draft(h.scene())
        assert info.value.status == 422
        assert code_of(info.value) == "ARTIFACT_NOT_FOUND"
        assert info.value.details["artifact_id"] == "art_front"

    def test_artifact_hash_mismatch_is_422(self):
        h = Harness(artifacts={"art_left": "c" * 64})
        with pytest.raises(ServiceError) as info:
            h.draft(h.scene())
        assert code_of(info.value) == "ARTIFACT_HASH_MISMATCH"
        assert info.value.details["declared"] == GOOD_HASH
        assert info.value.details["actual"] == "c" * 64

    def test_s28_unreachable_is_503_and_persists_nothing(self):
        """F16：一次 S28 抖动不能变成一次 422，也不能留下半截候选。"""
        h = Harness(artifacts={"art_back": ConnectionError("timed out")})
        sid = h.scene()
        with pytest.raises(ServiceError) as info:
            h.draft(sid)
        assert info.value.status == 503
        assert info.value.retryable is True
        assert code_of(info.value) == "DEPENDENCY_UNAVAILABLE"
        assert h.rows("scene_candidate") == []
        # 场景头没有被 CAS 推走。
        assert h.rows("scene_asset")[0]["current_revision"] == 0

    def test_s28_unconfigured_fails_closed(self):
        """没有 `S28_BASE_URL` 时整条 draft 路径关闭，不是"跳过校验"。"""
        h = Harness(artifact_source="unconfigured")
        with pytest.raises(ServiceError) as info:
            h.draft(h.scene())
        assert info.value.status == 503
        assert h.rows("scene_candidate") == []

    def test_reference_without_content_hash_is_recorded_as_unverified(self):
        # 没有 content_hash 就没法比对。记成 `hash_verified=false`，而不是
        # 默认"一致"——这两者在 audit 里是完全不同的两件事。
        h = Harness()
        anchors = four_anchors()
        anchors["front"]["artifact_ref"] = {"service_id": "S28",
                                            "id": "art_front", "revision": 1}
        result = h.draft(h.scene(), anchor_manifest=anchors,
                         lighting_manifest=lighting_for(anchors))
        assert result["evaluation"]["artifacts_checked"] == 8
        assert result["evaluation"]["artifacts_hash_verified"] == 7
        assert result["evaluation"]["artifacts_without_hash"] == 1

    def test_era_snapshot_mismatch_is_noted_but_not_rejected(self):
        # C13-6：era 是场景定义级。revision 带一个不同的快照会被记下来，
        # 但方案没有给对应的拒绝码，所以不发明一个。
        h = Harness()
        result = h.draft(h.scene(), era_snapshot={"label": "1990s"})
        assert result["evaluation"]["era_label_matches_scene"] is False

    def test_missing_artifact_source_revision_is_allowed(self):
        # 引用不带 revision 时 S28 适配层会去取 artifact 头；这里直接证明
        # 内核不要求 revision 字段。
        h = Harness()
        anchors = four_anchors()
        anchors["right"]["artifact_ref"] = {"service_id": "S28",
                                            "id": "art_right"}
        assert h.draft(h.scene(), anchor_manifest=anchors,
                       lighting_manifest=lighting_for(anchors))["state"] \
            == "PENDING"

    def test_version_conflict_on_draft(self):
        """F15：还有人在旧版本上起草。"""
        h = Harness()
        sid = h.scene()
        with pytest.raises(ServiceError) as info:
            h.draft(sid, expected_current_revision=7)
        assert info.value.status == 409
        assert info.value.retryable is True
        assert code_of(info.value) == "VERSION_CONFLICT"

    def test_tombstoned_scene_takes_no_candidate(self):
        h = Harness()
        sid = h.scene()
        h.service.tombstone("t1", "c9", sid, {})
        with pytest.raises(ServiceError) as info:
            h.draft(sid)
        assert code_of(info.value) == "SCENE_TOMBSTONED"

    def test_cross_tenant_draft_is_404(self):
        """E28：跨租户连存在性都不确认。"""
        h = Harness()
        sid = h.scene()
        with pytest.raises(ServiceError) as info:
            h.draft(sid, tenant_id="t2")
        assert info.value.status == 404
        assert code_of(info.value) == "NOT_FOUND"


# ================================================================ 接受候选

class TestAcceptCandidate:
    def test_accept_writes_revision_derivations_and_event(self):
        h = Harness()
        sid = h.scene()
        candidate_id = h.draft(sid)["candidate_id"]
        result = h.accept(sid, candidate_id)

        assert result["became_canonical"] is True
        assert result["canonical_revision"] == 1
        assert result["parent_revision"] is None
        assert result["previous_topology_hash"] is None
        assert result["topology_changed"] is False

        scene = h.service.get_scene("t1", sid)
        assert scene["current_revision"] == 1
        assert scene["canonical_revision"] == 1
        assert scene["topology_hash"] == topology_hash(topology())

        # 派生表按 payload 展开：4 anchor × 2 lighting，4×3 关系，1 个固定物。
        assert len(h.rows("scene_anchor")) == 4
        assert len(h.rows("scene_lighting_variant")) == 8
        assert len(h.rows("anchor_relationship")) == 12
        assert len(h.rows("scene_fixed_object")) == 1
        assert len(h.rows("canonical_pointer")) == 1

        events = h.events()
        assert len(events) == 1
        assert events[0]["type"] == EVENT_SCENE_REVISION_ACCEPTED
        assert events[0]["source"] == SOURCE
        assert events[0]["subject"] == f"scene/{sid}"
        assert events[0]["aggregate_id"] == sid
        # 契约 §5.3：`aggregate_sequence = revision`；envelope 的字段名是
        # `sequence`，不是 `aggregate_sequence`。
        assert events[0]["sequence"] == 1
        assert events[0]["data_ref"] is None

    def test_event_carries_exactly_the_contracted_keys(self):
        """契约是 `additionalProperties: false`；多一个键消费方就挂。"""
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        data = h.events()[0]["data"]
        assert set(data) == {
            "schema_version", "scene_ref", "topology_hash", "topology_changed",
            "impact_plan_ref", "became_canonical", "canonical_revision",
            "location_class", "era", "lighting_axes", "anchor_ids",
            "rights_ref"}
        assert len(data) == 12
        assert data["scene_ref"]["service_id"] == "S13"
        assert data["scene_ref"]["revision"] == 1
        assert data["anchor_ids"] == ["back", "front", "left", "right"]
        assert data["lighting_axes"] == ["day", "night"]
        assert data["era"] == {"label": "contemporary"}
        assert data["rights_ref"] == RIGHTS

    def test_candidate_state_moves_to_accepted(self):
        h = Harness()
        sid = h.scene()
        candidate_id = h.draft(sid)["candidate_id"]
        h.accept(sid, candidate_id)
        assert h.rows("scene_candidate")[0]["state"] == "ACCEPTED"

    def test_second_accept_of_same_candidate_is_409(self):
        h = Harness()
        sid = h.scene()
        candidate_id = h.draft(sid)["candidate_id"]
        h.accept(sid, candidate_id)
        with pytest.raises(ServiceError) as info:
            h.accept(sid, candidate_id, command_id="c4",
                     expected_current_revision=1)
        assert code_of(info.value) == "CANDIDATE_NOT_PENDING"

    def test_expired_candidate_cannot_be_accepted(self):
        # S13-10：TTL 是终态。过期判定是惰性的——读路径上没有定时器。
        h = Harness()
        sid = h.scene()
        candidate_id = h.draft(sid)["candidate_id"]
        h.conn.execute(
            "UPDATE scene_candidate SET expires_at='2000-01-01T00:00:00.000Z'")
        with pytest.raises(ServiceError) as info:
            h.accept(sid, candidate_id)
        assert code_of(info.value) == "CANDIDATE_NOT_PENDING"
        assert h.rows("scene_candidate")[0]["state"] == "EXPIRED"

    def test_stale_expected_revision_is_409(self):
        """F15 / F18。"""
        h = Harness()
        sid = h.scene()
        candidate_id = h.draft(sid)["candidate_id"]
        with pytest.raises(ServiceError) as info:
            h.accept(sid, candidate_id, expected_current_revision=3)
        assert code_of(info.value) == "VERSION_CONFLICT"
        assert info.value.retryable is True
        assert h.rows("scene_revision") == []

    def test_duplicate_content_is_409(self):
        """F14。同内容第二次接受会撞 `sr_hash_uniq`，也必须给 409 而不是 500。"""
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        second = h.draft(sid, command_id="c4", expected_current_revision=1)
        with pytest.raises(ServiceError) as info:
            h.accept(sid, second["candidate_id"], command_id="c5",
                     expected_current_revision=1)
        assert info.value.status == 409
        assert code_of(info.value) == "DUPLICATE_SCENE_CONTENT"

    def test_topology_change_requires_human(self):
        """F8 / §3.3。"""
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        candidate_id = h.draft(
            sid, command_id="c4", expected_current_revision=1,
            topology=moved_door(), topology_changed=True)["candidate_id"]
        with pytest.raises(ServiceError) as info:
            h.accept(sid, candidate_id, command_id="c5",
                     expected_current_revision=1,
                     decision_kind="policy_auto",
                     impact_plan_ref={"service_id": "S09", "id": "impact_1",
                                      "revision": 1})
        assert code_of(info.value) == "TOPOLOGY_CHANGE_REQUIRES_HUMAN"

    def test_topology_change_requires_an_impact_plan(self):
        """F9：拓扑变了却没有影响计划引用。"""
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        candidate_id = h.draft(
            sid, command_id="c4", expected_current_revision=1,
            topology=moved_door(), topology_changed=True)["candidate_id"]
        with pytest.raises(ServiceError) as info:
            h.accept(sid, candidate_id, command_id="c5",
                     expected_current_revision=1,
                     decision_kind="human_approved")
        assert code_of(info.value) == "IMPACT_PLAN_REQUIRED"

    def test_topology_change_with_both_gates_passes(self):
        """E08-D1。"""
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        previous = topology_hash(topology())
        candidate_id = h.draft(
            sid, command_id="c4", expected_current_revision=1,
            topology=moved_door(), topology_changed=True)["candidate_id"]
        result = h.accept(sid, candidate_id, command_id="c5",
                          expected_current_revision=1,
                          decision_kind="human_approved",
                          impact_plan_ref={"service_id": "S09",
                                           "id": "impact_1", "revision": 1})
        assert result["topology_changed"] is True
        # SCN-5：旧 hash 只能从响应里拿；事件仍只有新 hash。
        assert result["previous_topology_hash"] == previous
        assert result["topology_hash"] == topology_hash(moved_door())
        data = h.events()[-1]["data"]
        assert data["impact_plan_ref"] == {"service_id": "S09",
                                           "id": "impact_1", "revision": 1}
        assert "previous_topology_hash" not in data

    def test_set_canonical_on_a_topology_change_needs_the_same_gate(self):
        """只在 accept 判而 set-canonical 不判，就是留了一条绕过。"""
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        candidate_id = h.draft(
            sid, command_id="c4", expected_current_revision=1,
            topology=moved_door(), topology_changed=True)["candidate_id"]
        h.accept(sid, candidate_id, command_id="c5",
                 expected_current_revision=1, decision_kind="human_approved",
                 impact_plan_ref={"service_id": "S09", "id": "impact_1",
                                  "revision": 1},
                 set_canonical=False)
        with pytest.raises(ServiceError) as info:
            h.service.set_canonical(
                "t1", "c6", sid,
                {"revision": 2, "decision_kind": "policy_auto"})
        assert code_of(info.value) == "TOPOLOGY_CHANGE_REQUIRES_HUMAN"

    def test_primary_set_needs_a_valid_spatial_check_for_canonical(self):
        """F10 / E05-D2。

        经 draft 走不到这里（第 1 步已经拒了非法拓扑，而真正的
        `spatial_check` 本该由 QC 服务产出——C13-2 说那个服务不存在）。
        所以直接插一行 `topology_valid=false` 的候选，证明 accept 侧的门是活的。
        """
        h = Harness()
        sid = h.scene()
        candidate = h.draft(sid)
        check = dict(candidate["spatial_check"])
        check["topology_valid"] = False
        h.conn.execute(
            "UPDATE scene_candidate SET spatial_check=? WHERE candidate_id=?",
            (json.dumps(check), candidate["candidate_id"]))
        with pytest.raises(ServiceError) as info:
            h.accept(sid, candidate["candidate_id"])
        assert code_of(info.value) == "TOPOLOGY_INVALID_FOR_PRIMARY"

    def test_non_primary_scene_may_become_canonical_without_topology(self):
        """§2.1 允许 `background` 无 canonical；但这里证明的是**另一个**面：
        非 primary_set 不受 F10 那条门约束。"""
        h = Harness()
        sid = h.scene(location_class="one_off")
        anchors = {"front": anchor("front", x=3, y=1, yaw=0)}
        candidate = h.draft(sid, anchor_manifest=anchors,
                            lighting_manifest=lighting_for(anchors))
        check = dict(candidate["spatial_check"])
        check["topology_valid"] = False
        h.conn.execute(
            "UPDATE scene_candidate SET spatial_check=? WHERE candidate_id=?",
            (json.dumps(check), candidate["candidate_id"]))
        assert h.accept(sid, candidate["candidate_id"])["became_canonical"]

    def test_accept_without_set_canonical_leaves_topology_hash_alone(self):
        """S13-12。`topology_hash` 描述的是**当前 canonical**，不是最新版。"""
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"], set_canonical=False)
        scene = h.service.get_scene("t1", sid)
        assert scene["current_revision"] == 1
        assert scene["canonical_revision"] is None
        assert scene["topology_hash"] is None
        # E05-D1：没有 canonical 时是 404，不是空对象。
        with pytest.raises(ServiceError) as info:
            h.service.get_canonical("t1", sid)
        assert code_of(info.value) == "CANONICAL_NOT_SET"

    def test_set_canonical_moves_pointer_and_keeps_history(self):
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"], set_canonical=False)
        # 第二版必须换内容，否则接受时会撞 `DUPLICATE_SCENE_CONTENT`；
        # 这里换一个 anchor 的素材（拓扑不变）。
        anchors = four_anchors()
        anchors["front"]["artifact_ref"]["id"] = "art_front_v2"
        second = h.draft(sid, command_id="c4", expected_current_revision=1,
                         anchor_manifest=anchors,
                         lighting_manifest=lighting_for(anchors))[
                             "candidate_id"]
        h.accept(sid, second, command_id="c5", expected_current_revision=1,
                 set_canonical=False)
        result = h.service.set_canonical(
            "t1", "c6", sid, {"revision": 2, "decision_kind": "human_approved",
                              "reason_text": "第二版更好"})
        assert result["prior_canonical"] is None
        scene = h.service.get_scene("t1", sid)
        assert scene["canonical_revision"] == 2
        # SCN-2：set-canonical 不发事件。只有两次 accept 的两条。
        assert len(h.events()) == 2
        # 历史指针留着：只有一行 superseded_at IS NULL。
        pointers = h.rows("canonical_pointer")
        assert len([p for p in pointers if p["superseded_at"] is None]) == 1

    def test_accept_rolls_back_entirely_when_derivation_fails(self):
        """revision 行写入之后才炸，也不能留下半套派生行。

        直接注入一个会在第 6 步抛错的展开函数，比构造一个"恰好只在下标
        N 失败"的 payload 更可靠——如果事务没生效，这里的断言会立刻看到
        一行孤儿 revision。
        """
        h = Harness()
        sid = h.scene()
        candidate_id = h.draft(sid)["candidate_id"]

        def boom(*_args, **_kwargs):
            raise RuntimeError("derivation exploded")

        h.service._expand_derivations = boom
        with pytest.raises(RuntimeError):
            h.accept(sid, candidate_id)
        assert h.rows("scene_revision") == []
        assert h.rows("scene_anchor") == []
        assert h.rows("canonical_pointer") == []
        # 场景头必须还停在 0：CAS 的 UPDATE 也在同一个事务里。
        assert h.rows("scene_asset")[0]["current_revision"] == 0
        assert h.service.get_scene("t1", sid)["current_revision"] == 0


# ================================================================ 读路径

class TestReadPaths:
    def _scene_with_revision(self):
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        return h, sid

    def test_revision_payload_round_trips(self):
        h, sid = self._scene_with_revision()
        result = h.service.get_revision("t1", sid, 1)
        assert result["topology"] == topology()
        assert result["payload"]["topology"] == topology()
        assert result["topology_changed"] is False
        assert result["is_canonical"] is True
        # E30-D1：撤销后仍返回，但永远要求复查。
        assert result["rights_recheck_required"] is True
        assert result["rights_ref"] == RIGHTS

    def test_unknown_revision_is_404(self):
        h, sid = self._scene_with_revision()
        with pytest.raises(ServiceError) as info:
            h.service.get_revision("t1", sid, 9)
        assert info.value.status == 404

    def test_anchors_carry_every_lighting_variant(self):
        h, sid = self._scene_with_revision()
        one = h.service.get_anchors("t1", sid, 1, direction="front")["anchor"]
        assert one["anchor_id"] == "front"
        assert set(one["artifacts"]) == {"day", "night"}
        assert one["artifacts"]["night"]["id"] == "art_front_night"

    def test_listing_all_anchors_is_sorted(self):
        h, sid = self._scene_with_revision()
        listed = h.service.get_anchors("t1", sid, 1)["anchors"]
        assert [a["anchor_id"] for a in listed] == \
            ["back", "front", "left", "right"]
        for item in listed:
            assert set(item["artifacts"]) == {"day", "night"}

    def test_missing_lighting_is_422_not_a_fallback_to_day(self):
        """F11 / §9 第 8 条。退回 day 会让夜景镜头用白天光照生成。"""
        h, sid = self._scene_with_revision()
        with pytest.raises(ServiceError) as info:
            h.service.get_anchors("t1", sid, 1, direction="front",
                                  lighting="golden_hour")
        assert info.value.status == 422
        assert code_of(info.value) == "LIGHTING_NOT_AVAILABLE"
        assert info.value.details["available"] == ["day", "night"]

    def test_missing_anchor_is_404(self):
        """F12。"""
        h, sid = self._scene_with_revision()
        with pytest.raises(ServiceError) as info:
            h.service.get_anchors("t1", sid, 1, direction="from_ceiling")
        assert info.value.status == 404
        assert code_of(info.value) == "ANCHOR_NOT_FOUND"
        assert info.value.details["available"] == ["back", "front", "left",
                                                  "right"]

    def test_topology_response_shape(self):
        h, sid = self._scene_with_revision()
        result = h.service.get_topology("t1", sid, 1)
        assert result["topology_hash"] == topology_hash(topology())
        assert result["era"] == {"label": "contemporary"}
        assert set(result["anchor_cameras"]) == {"back", "front", "left",
                                                 "right"}
        assert result["fixed_objects"][0]["object_id"] == "desk_1"
        # 固定物没有状态（§2.5 / §9 第 4 条）。
        assert "is_open" not in result["fixed_objects"][0]
        assert result["rights_recheck_required"] is True

    def test_fixed_objects_can_be_filtered_by_unit(self):
        h, sid = self._scene_with_revision()
        assert len(h.service.get_fixed_objects("t1", sid, 1,
                                               unit_id="office_main")["items"]) == 1
        assert h.service.get_fixed_objects("t1", sid, 1,
                                           unit_id="corridor_a")["items"] == []

    def test_canonical_response(self):
        h, sid = self._scene_with_revision()
        result = h.service.get_canonical("t1", sid)
        assert result["canonical_revision"] == 1
        assert result["decision_kind"] == "initial_import"
        assert result["status"] == "active"

    def test_lookup_by_display_name(self):
        h, sid = self._scene_with_revision()
        assert h.service.lookup("t1", "ser_1", "办公室")["scene_asset_id"] == sid
        with pytest.raises(ServiceError) as info:
            h.service.lookup("t1", "ser_1", "天台")
        assert info.value.status == 404

    @pytest.mark.parametrize("call", [
        lambda s, sid: s.get_scene("t2", sid),
        lambda s, sid: s.get_revision("t2", sid, 1),
        lambda s, sid: s.get_anchors("t2", sid, 1),
        lambda s, sid: s.get_topology("t2", sid, 1),
        lambda s, sid: s.get_fixed_objects("t2", sid, 1),
        lambda s, sid: s.get_canonical("t2", sid),
        lambda s, sid: s.lookup("t2", "ser_1", "办公室"),
    ])
    def test_cross_tenant_reads_are_404(self, call):
        """E28-D1 / E28-D3。"""
        h, sid = self._scene_with_revision()
        with pytest.raises(ServiceError) as info:
            call(h.service, sid)
        assert info.value.status == 404


# ================================================================ 生命周期

class TestLifecycle:
    def test_deprecate_then_tombstone(self):
        h = Harness()
        sid = h.scene()
        assert h.service.deprecate("t1", "c2", sid, {})["status"] == "deprecated"
        # 再下线一次是幂等的，不是错误。
        assert h.service.deprecate("t1", "c3", sid, {})["status"] == "deprecated"
        assert h.service.tombstone("t1", "c4", sid, {})["status"] == "tombstoned"

    def test_tombstoned_is_terminal(self):
        """§3.4：复活走新 ID。"""
        h = Harness()
        sid = h.scene()
        h.service.tombstone("t1", "c2", sid, {})
        with pytest.raises(ServiceError) as info:
            h.service.deprecate("t1", "c3", sid, {})
        assert code_of(info.value) == "SCENE_TOMBSTONED"
        with pytest.raises(ServiceError) as info:
            h.service.tombstone("t2", "c4", sid, {})
        assert info.value.status == 404

    def test_active_can_be_tombstoned_directly(self):
        """S13-11：紧急下架不该强求先走一步 deprecate。"""
        h = Harness()
        sid = h.scene()
        assert h.service.tombstone("t1", "c2", sid, {})["status"] == "tombstoned"

    def test_tombstone_keeps_the_canonical_history(self):
        """S13-14：下线不删指针。历史决定不该因为当下不可用而消失。"""
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        h.service.tombstone("t1", "c9", sid, {})
        assert h.service.get_canonical("t1", sid)["status"] == "tombstoned"
        assert len(h.rows("canonical_pointer")) == 1

    def test_merge_deprecates_the_source_and_never_moves_revisions(self):
        h = Harness()
        source = h.scene(display_name="办公室A")
        target = h.scene(command_id="c2", display_name="办公室B")
        result = h.service.merge("t1", "c3", source,
                                 {"target_scene_asset_id": target})
        assert result["merged_into"] == target
        assert result["status"] == "deprecated"
        assert h.rows("scene_revision", scene_asset_id=target) == []
        assert h.rows("scene_revision", scene_asset_id=source) == []

    def test_merge_into_unknown_target_is_404(self):
        h = Harness()
        source = h.scene()
        with pytest.raises(ServiceError) as info:
            h.service.merge("t1", "c2", source,
                            {"target_scene_asset_id": "scn_missing"})
        assert info.value.status == 404

    def test_merge_cannot_be_retargeted(self):
        h = Harness()
        source = h.scene(display_name="A")
        first = h.scene(command_id="c2", display_name="B")
        second = h.scene(command_id="c3", display_name="C")
        h.service.merge("t1", "c4", source, {"target_scene_asset_id": first})
        with pytest.raises(ServiceError) as info:
            h.service.merge("t1", "c5", source,
                            {"target_scene_asset_id": second})
        assert code_of(info.value) == "SCENE_ALREADY_MERGED"

    def test_merge_into_self_is_malformed(self):
        h = Harness()
        sid = h.scene()
        with pytest.raises(ServiceError) as info:
            h.service.merge("t1", "c2", sid, {"target_scene_asset_id": sid})
        assert info.value.status == 400


# ================================================================ 几何纯函数

class TestGeometry:
    """§11.1 第 2 条："这是 S13 最容易出 bug 的地方"。"""

    def test_valid_topology_has_no_problems(self):
        problems, facts = geometry.validate_topology(topology())
        assert problems == []
        assert facts["topology_valid"] is True
        assert facts["units"] == 2
        assert facts["openings"] == 2
        assert facts["openings_attached"] is True

    def test_polygon_may_be_open_and_gets_closed(self):
        assert geometry.polygon([[0, 0], [1, 0], [1, 1]]) is not None
        assert geometry.is_closed(
            geometry.polygon([[0, 0], [1, 0], [1, 1], [0, 0]]))

    def test_degenerate_polygon_is_rejected(self):
        assert geometry.polygon([[0, 0], [1, 0]]) is None
        assert geometry.polygon("nope") is None

    def test_shared_edge_is_allowed_but_shared_area_is_not(self):
        left = geometry.polygon([[0, 0], [1, 0], [1, 1], [0, 1]])
        touching = geometry.polygon([[1, 0], [2, 0], [2, 1], [1, 1]])
        overlapping = geometry.polygon([[0.5, 0], [2, 0], [2, 1], [0.5, 1]])
        assert geometry.polygons_overlap(left, touching) is False
        assert geometry.polygons_overlap(left, overlapping) is True

    def test_point_in_polygon_handles_interior_exterior_and_edge(self):
        square = geometry.polygon([[0, 0], [4, 0], [4, 4], [0, 4]])
        assert geometry.point_in_polygon((2, 2), square) is True
        assert geometry.point_in_polygon((9, 2), square) is False
        assert geometry.point_in_polygon((0, 2), square) is True   # 边界算在内

    def test_a_camera_on_the_wall_is_still_inside(self):
        # 方案 §4.1 第 3 条说「或边界容差内」。贴墙的机位是常态。
        anchors = {"front": anchor("front", x=0, y=2, yaw=90)}
        problems, _ = geometry.validate_anchor_position(
            anchors["front"], {"office_main": geometry.polygon(
                topology()["units"][0]["polygon"])})
        assert problems == []

    def test_openings_must_touch_both_units(self):
        t = topology()
        t["openings"][0]["wall_segment"] = [[6, 2.5], [6, 3.5]]
        problems, _ = geometry.validate_topology(t)
        assert any(p["target"] == "door_1" for p in problems)

    def test_a_door_between_the_same_unit_twice_is_invalid(self):
        t = topology()
        t["openings"][0]["unit_b"] = "office_main"
        problems, _ = geometry.validate_topology(t)
        assert any("same unit" in p["reason"] for p in problems)

    def test_yaw_spread_uses_circular_distance(self):
        assert geometry.yaw_spread([350, 10]) == 20.0
        # 「两两最小夹角的最大值」：0°/180° 这对本身就隔半圈，所以四个正交
        # 方向得 180°，不是 90°。90° 那个数只在问「相邻两台的间隔」时成立，
        # 那是另一个量。
        assert geometry.yaw_spread([0, 90, 180, 270]) == 180.0
        assert geometry.yaw_spread([0, 120, 240]) == 120.0
        assert geometry.yaw_spread([0, 0, 0]) == 0.0
        assert geometry.yaw_spread([42]) == 0.0

    def test_relative_yaw_is_normalised_to_a_half_turn(self):
        assert geometry.relative_yaw(0, 90) == 90.0
        assert geometry.relative_yaw(0, 270) == -90.0
        assert geometry.relative_yaw(350, 10) == 20.0

    def test_anchor_coverage_thresholds_come_from_location_class(self):
        assert geometry.anchor_coverage(3, "primary_set")["ok"] is False
        assert geometry.anchor_coverage(4, "primary_set")["ok"] is True
        assert geometry.anchor_coverage(2, "recurring_location")["ok"] is True
        assert geometry.anchor_coverage(0, "background")["ok"] is True

    def test_twenty_generated_rectangles_that_do_not_overlap(self):
        """20 个随机正交矩形：彼此不重叠的一律通过。"""
        import random
        rng = random.Random(13)
        for _ in range(20):
            x = rng.randint(0, 40)
            y = rng.randint(0, 40)
            w = rng.randint(1, 3)
            hgt = rng.randint(1, 3)
            t = {
                "units": [
                    {"unit_id": "a", "kind": "room",
                     "polygon": [[0, 0], [3, 0], [3, 3], [0, 3]]},
                    {"unit_id": "b", "kind": "room",
                     "polygon": [[x + 10, y + 10], [x + 10 + w, y + 10],
                                 [x + 10 + w, y + 10 + hgt],
                                 [x + 10, y + 10 + hgt]]},
                ],
                "openings": [],
                "orientation": {"north_axis_deg": 0},
                "structures": [],
                "walkable": {"a": "ref", "b": "ref"},
                "scale_unit": "meter",
            }
            problems, facts = geometry.validate_topology(t)
            assert problems == [], (t, problems)
            assert facts["units"] == 2

    def test_twenty_generated_overlaps_are_all_caught(self):
        import random
        rng = random.Random(29)
        for _ in range(20):
            x = rng.uniform(0.5, 2.0)
            y = rng.uniform(0.5, 2.0)
            t = {
                "units": [
                    {"unit_id": "a", "kind": "room",
                     "polygon": [[0, 0], [3, 0], [3, 3], [0, 3]]},
                    {"unit_id": "b", "kind": "room",
                     "polygon": [[x, y], [x + 2, y], [x + 2, y + 2],
                                 [x, y + 2]]},
                ],
                "openings": [],
                "orientation": {"north_axis_deg": 0},
                "structures": [],
                "walkable": {"a": "ref", "b": "ref"},
                "scale_unit": "meter",
            }
            problems, _ = geometry.validate_topology(t)
            assert problems, "overlap at (%s,%s) was not caught" % (x, y)
            assert any("overlaps" in p["reason"] for p in problems)


# ================================================================ 边界

class TestBoundary:
    """S13 不是第二权威：没有镜头、没有在场者、没有轴判定。"""

    def test_there_is_no_shot_or_occupant_table(self):
        h = Harness()
        names = {r[0] for r in h.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for banned in ("shot", "shot_ref", "occupant", "axis_violation",
                       "camera_position", "binding"):
            assert banned not in names

    def test_fixed_objects_have_no_state_columns(self):
        h = Harness()
        cols = {r[1] for r in h.conn.execute(
            "PRAGMA table_info(scene_fixed_object)").fetchall()}
        for banned in ("is_open", "is_damaged", "state", "status",
                       "occupant_ref", "owner_ref"):
            assert banned not in cols

    def test_there_is_no_balance_or_budget_column(self):
        h = Harness()
        for table in ("scene_asset", "scene_revision", "scene_candidate"):
            cols = {r[1] for r in h.conn.execute(
                f"PRAGMA table_info({table})").fetchall()}
            for banned in ("balance", "cost_micro", "permit"):
                assert banned not in cols

    def test_axis_judgement_is_not_ours(self):
        """§0.2 第 1 条 / §9 第 1 条：越轴归 S17。拓扑带北向，但没有结论。"""
        h = Harness()
        sid = h.scene()
        check = h.draft(sid)["spatial_check"]
        assert check["axis_check"]["checked_by"] is None
        cols = {r[1] for r in h.conn.execute(
            "PRAGMA table_info(scene_candidate)").fetchall()}
        assert "axis_violation" not in cols


# ================================================================ HTTP

def _http_client(monkeypatch, tmp_path, **over):
    """`api/app.py` 的 TestClient，S28 换成 Harness 的 stub。

    `S13_DB` 必须在 import 前指向独立文件库（那一刻就建连接与表）；路由
    函数按模块全局名 `service` 查找，所以重绑 `module.service` 即可，
    且必须用 `module._conn`，否则断言查的是另一个库。
    """
    monkeypatch.setenv("S13_DB", str(tmp_path / "s13.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s13_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s13_api_app"] = module

    h = Harness(conn=module._conn, **over)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h


def _post(client, path, body, *, command_id="c1", tenant="t1"):
    return client.post(path, json=body,
                       headers={"Idempotency-Key": command_id,
                                "X-Tenant-Id": tenant})


def _get(client, path, *, tenant="t1", **params):
    return client.get(path, params=params, headers={"X-Tenant-Id": tenant})


_register_body = register_body


class TestHttpLayer:
    def test_register_then_draft_then_accept_then_read(self, monkeypatch,
                                                       tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, "/scene-assets", _register_body())
        assert created.status_code == 200, created.text
        sid = created.json()["scene_asset_id"]
        assert created.json()["replayed"] is False

        drafted = _post(client, f"/scene-assets/{sid}/candidates",
                        h.draft_body(), command_id="c2")
        assert drafted.status_code == 200, drafted.text
        candidate_id = drafted.json()["candidate_id"]

        accepted = _post(
            client, f"/scene-assets/{sid}/candidates/{candidate_id}:accept",
            {"expected_current_revision": 0, "set_canonical": True,
             "decision_kind": "initial_import"}, command_id="c3")
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["canonical_revision"] == 1

        topology_r = _get(client, f"/scene-assets/{sid}/revisions/1/topology")
        assert topology_r.status_code == 200
        assert topology_r.json()["topology_hash"] == topology_hash(topology())

        anchors_r = _get(client, f"/scene-assets/{sid}/revisions/1/anchors",
                         direction="front", lighting="night")
        assert anchors_r.status_code == 200
        assert anchors_r.json()["anchor"]["artifacts"]["night"]["id"] == \
            "art_front_night"

        canonical_r = _get(client, f"/scene-assets/{sid}/canonical")
        assert canonical_r.status_code == 200
        assert canonical_r.json()["canonical_revision"] == 1

        lookup_r = _get(client, "/scene-assets:lookup", series_scope="ser_1",
                        alias="办公室")
        assert lookup_r.status_code == 200
        assert lookup_r.json()["scene_asset_id"] == sid

    def test_replay_returns_the_first_result_and_the_flag(self, monkeypatch,
                                                          tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        first = _post(client, "/scene-assets", _register_body())
        second = _post(client, "/scene-assets", _register_body())
        assert second.json()["replayed"] is True
        assert second.json()["scene_asset_id"] == first.json()["scene_asset_id"]

    def test_same_key_different_body_is_409(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        _post(client, "/scene-assets", _register_body())
        clash = _post(client, "/scene-assets",
                      _register_body(display_name="天台"))
        assert clash.status_code == 409
        assert clash.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_between_tenant_disagreement_is_403(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        result = _post(client, "/scene-assets",
                       _register_body(tenant_id="t2"))
        assert result.status_code == 403
        assert result.json()["code"] == "TENANT_SCOPE_DENIED"

    def test_missing_idempotency_key_is_refused(self, monkeypatch, tmp_path):
        """FastAPI 的 header 校验先于我们的 `_body`，所以是 422 而不是 400。

        记下来是因为它说明「`MALFORMED_REQUEST` 覆盖所有缺参」这句话不成立：
        必填 header 缺失在生产上返回的是框架的 422 形状（`{"detail": [...]}`），
        与本服务的错误信封不同形。要统一得改成 `Header(None)` + 自己判。
        """
        client, _ = _http_client(monkeypatch, tmp_path)
        result = client.post("/scene-assets", json=_register_body(),
                             headers={"X-Tenant-Id": "t1"})
        assert result.status_code == 422
        assert "Idempotency-Key" in result.text

    def test_command_in_progress_retries(self, monkeypatch, tmp_path):
        """同键同 body、首次执行还没落结果 → 409 可重试（`Retry-After: 1`）。

        这条只能从 HTTP 层测：SDK 只在 `result IS NULL` 时抛，而 `command()`
        在没有并发时总会在同一轮里 finish 掉。
        """
        client, h = _http_client(monkeypatch, tmp_path)
        body = _register_body()
        import idempotency_sdk

        digest = idempotency_sdk.canonical_request_hash(body)
        h.idem.begin("t1", "c1", digest)  # 占位，不 finish

        result = _post(client, "/scene-assets", body)
        assert result.status_code == 409
        assert result.json()["code"] == "COMMAND_IN_FLIGHT"
        assert result.json()["retryable"] is True
        assert result.headers["Retry-After"] == "1"

    def test_error_envelope_shape(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        result = _get(client, "/scene-assets/scn_missing")
        assert result.status_code == 404
        body = result.json()
        assert set(body) == {"code", "message", "retryable", "correlation_id",
                             "details"}
        assert body["code"] == "NOT_FOUND"
        assert body["retryable"] is False

    def test_lighting_not_available_surfaces_as_422(self, monkeypatch,
                                                    tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        sid = _post(client, "/scene-assets",
                    _register_body()).json()["scene_asset_id"]
        candidate_id = _post(client, f"/scene-assets/{sid}/candidates",
                             h.draft_body(), command_id="c2").json()[
                                 "candidate_id"]
        _post(client, f"/scene-assets/{sid}/candidates/{candidate_id}:accept",
              {"expected_current_revision": 0, "set_canonical": True,
               "decision_kind": "initial_import"}, command_id="c3")
        result = _get(client, f"/scene-assets/{sid}/revisions/1/anchors",
                      lighting="night_vision")
        assert result.status_code == 422
        assert result.json()["code"] == "LIGHTING_NOT_AVAILABLE"

    def test_s28_unreachable_surfaces_as_503(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path,
                                 artifacts={"art_front":
                                            ConnectionError("timeout")})
        sid = _post(client, "/scene-assets",
                    _register_body()).json()["scene_asset_id"]
        result = _post(client, f"/scene-assets/{sid}/candidates",
                       h.draft_body(), command_id="c2")
        assert result.status_code == 503
        assert result.json()["retryable"] is True

    def test_readiness_lists_the_contract_gaps(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        body = client.get("/health/readiness").json()
        assert body["ready"] is True
        ids = {gap["id"] for gap in body["contract_gaps"]}
        assert {"C13-1", "SCN-1", "S13-12"} <= ids
        for gap in body["contract_gaps"]:
            assert set(gap) == {"id", "kind", "summary", "handling"}
            assert gap["kind"] in {"conflict", "needs_owner", "gap"}

    def test_cross_tenant_read_is_404_over_http(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        sid = _post(client, "/scene-assets",
                    _register_body()).json()["scene_asset_id"]
        assert _get(client, f"/scene-assets/{sid}", tenant="t2").status_code \
            == 404

    def test_lifecycle_commands_are_reachable(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        sid = _post(client, "/scene-assets",
                    _register_body()).json()["scene_asset_id"]
        other = _post(client, "/scene-assets",
                      _register_body(display_name="走廊"),
                      command_id="c2").json()["scene_asset_id"]

        merged = _post(client, f"/scene-assets/{sid}:merge",
                       {"target_scene_asset_id": other}, command_id="c3")
        assert merged.status_code == 200
        assert merged.json()["merged_into"] == other

        gone = _post(client, f"/scene-assets/{other}:tombstone", {},
                     command_id="c4")
        assert gone.status_code == 200
        assert gone.json()["status"] == "tombstoned"


# ================================================================ 不变量

class TestInvariants:
    """§8「通用」段里需要跨事务/跨连接才看得见的那几条。"""

    def test_two_accepts_of_one_candidate_only_one_wins(self, tmp_path):
        """§8「并发接受 | 1 成功」。

        两个独立连接（各自一个 `SceneService`）同时接受同一个候选、同一个
        `expected_current_revision=0`。SQLite 的写锁把两者排成串行，CAS 与
        候选状态检查都在 `BEGIN IMMEDIATE` 之后，所以第二个不可能也成功。
        失败的那次可以是 `VERSION_CONFLICT`（先撞 CAS）或
        `CANDIDATE_NOT_PENDING`（先撞候选状态）——两者都证明「只有一次生效」。
        """
        import sqlite3 as _sqlite3
        import threading

        path = tmp_path / "concurrent.db"
        seed = _sqlite3.connect(path, isolation_level=None)
        seed.row_factory = _sqlite3.Row
        setup = SceneService(seed, artifact_source=stub_artifact_source)
        sid = setup.create_scene("t1", "c1", {
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "办公室", "scene_kind": "interior",
            "era": {"label": "contemporary"},
            "location_class": "primary_set", "created_by": "u_1",
        })["scene_asset_id"]
        candidate_id = setup.draft_candidate(
            "t1", "c2", sid, Harness().draft_body())["candidate_id"]
        seed.close()

        results, errors = [], []
        barrier = threading.Barrier(2)

        def attempt(command_id):
            conn = _sqlite3.connect(path, isolation_level=None, timeout=10)
            conn.row_factory = _sqlite3.Row
            svc = SceneService(conn, artifact_source=stub_artifact_source)
            barrier.wait()
            try:
                results.append(svc.accept_candidate(
                    "t1", command_id, sid, candidate_id,
                    {"expected_current_revision": 0, "set_canonical": True,
                     "decision_kind": "initial_import"}))
            except ServiceError as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=attempt, args=(f"c{i}",))
                   for i in range(3, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 1, (results, errors)
        assert len(errors) == 1
        assert errors[0].code in {"VERSION_CONFLICT", "CANDIDATE_NOT_PENDING"}
        # 库里只有一版 revision、一条事件。
        check = _sqlite3.connect(path, isolation_level=None)
        check.row_factory = _sqlite3.Row
        assert len(check.execute("SELECT 1 FROM scene_revision").fetchall()) == 1
        assert len(check.execute("SELECT 1 FROM outbox").fetchall()) == 1
        check.close()

    def test_derivations_match_payload_after_reopening(self, tmp_path):
        """§8「崩溃恢复 | 派生表与 payload 一致」。

        走**文件库**：写完 accept 关连接、重开一个新的 `SceneService`，再
        按 `payload` 重算一遍展开，与库里的派生行逐项比对。内存库做不到
        这一点（换连接就是另一个库），而这正是「重启后还对不对」要问的。
        """
        import sqlite3 as _sqlite3

        path = tmp_path / "restart.db"
        first = _sqlite3.connect(path, isolation_level=None)
        first.row_factory = _sqlite3.Row
        svc = SceneService(first, artifact_source=stub_artifact_source)
        sid = svc.create_scene("t1", "c1", {
            "project_id": "prj_1", "series_scope": "ser_1",
            "display_name": "办公室", "scene_kind": "interior",
            "era": {"label": "contemporary"},
            "location_class": "primary_set", "created_by": "u_1",
        })["scene_asset_id"]
        candidate_id = svc.draft_candidate(
            "t1", "c2", sid, draft_body())["candidate_id"]
        svc.accept_candidate("t1", "c3", sid, candidate_id,
                             {"expected_current_revision": 0,
                              "set_canonical": True,
                              "decision_kind": "initial_import"})
        first.close()

        second = _sqlite3.connect(path, isolation_level=None)
        second.row_factory = _sqlite3.Row
        reopened = SceneService(second, artifact_source=stub_artifact_source)
        try:
            row = second.execute(
                "SELECT payload FROM scene_revision WHERE revision=1").fetchone()
            payload = json.loads(row["payload"])
            anchors = reopened._parse_anchors(payload["anchor_manifest"])
            assert [a["anchor_id"] for a in anchors] == [
                r["anchor_id"] for r in second.execute(
                    "SELECT anchor_id FROM scene_anchor ORDER BY anchor_id")]
            variants = reopened._parse_lighting(
                anchors, payload["lighting_axes"], payload["lighting_manifest"])
            assert sorted((v["anchor_id"], v["lighting"]) for v in variants) == [
                (r["anchor_id"], r["lighting"]) for r in second.execute(
                    "SELECT anchor_id, lighting FROM scene_lighting_variant"
                    " ORDER BY anchor_id, lighting")]
            assert [o["object_id"] for o in payload["fixed_objects"]] == [
                r["object_id"] for r in second.execute(
                    "SELECT object_id FROM scene_fixed_object")]
            # 读路径在重启后照样可用。
            assert reopened.get_topology("t1", sid, 1)["topology_hash"] == \
                topology_hash(topology())
        finally:
            second.close()

    def test_revisions_are_append_only(self):
        """§3.4：没有任何 UPDATE 路径可以改一条 revision。

        用连接层的授权证明不了「将来没人写 UPDATE」，所以这里改成检查
        服务的公开面：只有写命令是 create/draft/accept/reject/set_canonical/
        deprecate/tombstone/merge，没有一个以 revision 内容为入参。
        """
        import inspect

        methods = {name for name, _ in inspect.getmembers(
            SceneService, inspect.isfunction) if not name.startswith("_")}
        assert methods == {
            "create_scene", "draft_candidate", "accept_candidate",
            "reject_candidate", "set_canonical", "deprecate", "tombstone",
            "merge", "get_scene", "get_revision", "get_anchors",
            "get_topology", "get_fixed_objects", "get_canonical", "lookup"}

    def test_canonical_pointer_is_unique_where_live(self):
        """§2.6 的部分唯一索引（`WHERE superseded_at IS NULL`）。

        路径测试覆盖了「换一次 canonical」，这里补一条 DB 层的证明：插一条
        **不同 revision** 的活指针（主键不冲突、外键也成立），只有那条
        部分唯一索引能拦下它。若约束只写在内核的 UPDATE 顺序里，这条会过。
        """
        import sqlite3 as _sqlite3

        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        anchors = four_anchors()
        anchors["front"]["artifact_ref"]["id"] = "art_front_v2"
        second = h.draft(sid, command_id="c4", expected_current_revision=1,
                         anchor_manifest=anchors,
                         lighting_manifest=lighting_for(anchors))["candidate_id"]
        h.accept(sid, second, command_id="c5", expected_current_revision=1,
                 set_canonical=False)
        with pytest.raises(_sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO canonical_pointer (tenant_id, scene_asset_id,"
                " revision, topology_hash, decided_at, decided_by,"
                " decision_kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("t1", sid, 2, topology_hash(topology()), "now", "x",
                 "policy_auto"))


# ================================================================ 哈希

class TestHashing:
    def test_topology_hash_ignores_everything_but_topology(self):
        a = {"topology": topology(), "note": "x"}
        b = {"topology": topology(), "note": "y"}
        assert topology_hash(a["topology"]) == topology_hash(b["topology"])

    def test_topology_hash_is_stable_under_key_order(self):
        t1 = topology()
        t2 = {k: t1[k] for k in reversed(list(t1))}
        assert topology_hash(t1) == topology_hash(t2)

    def test_moving_a_door_changes_only_the_topology_hash(self):
        """§9 第 6 条：「换了光照」与「门挪了」必须能分开回答。"""
        h = Harness()
        sid = h.scene()
        h.accept(sid, h.draft(sid)["candidate_id"])
        anchors = four_anchors()
        anchors["front"]["artifact_ref"]["id"] = "art_front_v2"
        result = h.draft(sid, command_id="c4", expected_current_revision=1,
                         anchor_manifest=anchors,
                         lighting_manifest=lighting_for(anchors))
        assert result["topology_hash"] == topology_hash(topology())

    def test_content_hash_ignores_envelope_fields(self):
        body = Harness().draft_body()
        base = revision_content_hash(body)
        for key in ("command_id", "expected_current_revision", "tenant_id"):
            extended = {**body, key: "noise"}
            assert revision_content_hash(extended) == base
