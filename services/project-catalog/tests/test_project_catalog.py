"""S03 测试。对照 `plans/S03-project-catalog.md` §11.1 的分步验收。

两条最要紧的断言：

- `test_reorder_never_changes_episode_id` —— 稳定 ID 在重排后不变，且
  `episode` 表里根本不存在 `episode_number` 列（结构性，不是自觉）。
- `test_completed_is_reversible` —— `completed` 是 S08 的投影，不是 S03 的
  终态；只有 `archived` 不可回退。

桩按**真实接口**写，不按内核希望的样子写：S01 的 40 个权限键里没有
`project.create`，所以 `_s01_like` 对写入动作抛 503 —— 这正是生产里的行为，
用来证明"没人把读权限顶成写权限"。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from idempotency_sdk import SqliteIdempotencyStore, canonical_request_hash

from project_catalog.deps import REGISTERED_PERMISSIONS
from project_catalog.schema import (
    CONTRACT_GAPS,
    WRITE_PERMISSIONS,
)
from project_catalog.service import (
    ProjectCatalogService,
    ServiceError,
    merge_specs,
    normalize_spec,
)

TA = "tnt_aaa"
TB = "tnt_bbb"

SPEC = {
    "output": {"aspect_ratio": "9:16", "width": 1080, "height": 1920,
               "fps": 24, "max_episode_seconds": 180,
               "min_episode_seconds": 60},
    "language": {"primary": "zh-CN", "dub_targets": ["ja-JP"]},
    "delivery": {"loudness_target_lufs": -16, "color_primaries": "bt709"},
    "default_branch": "canon",
}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


class Harness:
    """服务 + 幂等 + 与 `api/app.py` 同形的 `command()` 外壳。"""

    def __init__(self, *, authorizer=None, run_probe=None,
                 clock_validator=None, clock=None):
        self.conn = _conn()
        kwargs = {"authorizer": authorizer or _allow_all(),
                  "run_probe": run_probe, "clock_validator": clock_validator}
        if clock is not None:
            kwargs["clock"] = clock
        self.service = ProjectCatalogService(self.conn, **kwargs)
        self.idem = SqliteIdempotencyStore(self.conn)
        self.calls = 0

    def command(self, tenant_id: str, body: dict, execute):
        self.calls += 1
        request_hash = canonical_request_hash(body)
        record = self.idem.begin(tenant_id, body["command_id"], request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = execute()
        except Exception:
            self.idem.abort(tenant_id, body["command_id"])
            raise
        self.idem.finish(tenant_id, body["command_id"],
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    def create(self, tenant_id: str = TA, *, episodes=None, series=None,
               principal_id: str = "p_1", command_id: str = "cmd_create",
               **over):
        body = {
            "command_id": command_id,
            "name": "测试短剧",
            "org_unit_id": "org_1",
            "principal": {"principal_id": principal_id},
            "spec": SPEC,
            "series": series if series is not None else [{"title": "第一季"}],
            "episodes": episodes if episodes is not None else [],
        }
        body.update(over)
        return self.command(tenant_id, body,
                            lambda: self.service.create_project(
                                tenant_id, body, principal=body["principal"]))


def _allow_all(granted: set[str] | None = None):
    """桩 S01：对给定权限说 allow。默认放行写入键。

    这不是"忘了配"——测试要验的是内核行为。`_s01_like` 才是生产形状。
    """
    seen = []

    def _authorize(tenant_id, principal_id, scope_kind, scope_id, permission):
        seen.append((tenant_id, scope_kind, scope_id, permission))
        allowed = granted is None or permission in granted
        return {"allowed": allowed, "permissions": [permission] if allowed
                else [], "denied": [] if allowed else [permission],
                "seen": seen}
    _authorize.seen = seen
    return _authorize


def _s01_like(base_url_marker=None):
    """桩 S01，**按真实注册表**：未注册的键一律 503（缺口 G5）。"""

    def _authorize(tenant_id, principal_id, scope_kind, scope_id, permission):
        if permission not in REGISTERED_PERMISSIONS:
            raise ServiceError(
                503, "PERMISSION_KEY_UNREGISTERED",
                f"permission {permission!r} is not registered")
        return {"allowed": True, "permissions": [permission], "denied": []}
    return _authorize


# ---------------------------------------------------------------- 结构

def test_episode_table_has_no_number_column():
    """§1.1 / 反模式 1：没有 `episode_number` 列。

    一个顺手能用的整数列会被下游当权威，重排后全部错位。这条必须是结构性的：
    查 `PRAGMA table_info`，不是查代码里有没有人读它。
    """
    h = Harness()
    columns = {row["name"] for row in h.conn.execute(
        "PRAGMA table_info(episode)")}
    assert "episode_number" not in columns
    assert "ordinal" not in columns
    # 反面对照：确实查到了表，不是查了个空集合。
    assert {"episode_id", "series_id", "lifecycle"} <= columns


def test_write_permissions_are_all_unregistered():
    """缺口 G5 是**可核对的事实**，不是一句注释。

    如果哪天有人给写入动作注册了权限键，这条会红，从而迫使那份注册表被
    重新审视，而不是悄悄把 `project.get` 顶成写权限。
    """
    assert WRITE_PERMISSIONS, "expected six write permissions"
    for action, permission in WRITE_PERMISSIONS.items():
        assert permission not in REGISTERED_PERMISSIONS, action
    assert REGISTERED_PERMISSIONS == frozenset({"project.get"})


def test_write_with_real_s01_fails_closed():
    """生产形状下建项目是 503，**不是放行**。

    503 且不落任何行：读权限的凭证不能因此建项目。
    """
    h = Harness(authorizer=_s01_like())
    with pytest.raises(ServiceError) as caught:
        h.create(episodes=[{"title": "第 1 集"}])
    assert caught.value.status == 503
    assert caught.value.code == "PERMISSION_KEY_UNREGISTERED"
    assert h.conn.execute("SELECT COUNT(*) c FROM project").fetchone()["c"] == 0


def test_no_authorizer_configured_never_allows():
    h = Harness(authorizer=None)
    h.service._authorize = None
    with pytest.raises(ServiceError) as caught:
        h.create()
    assert caught.value.code == "AUTH_CONTEXT_UNAVAILABLE"
    assert caught.value.status == 503


def test_denied_principal_cannot_create():
    h = Harness(authorizer=_allow_all(granted=set()))
    with pytest.raises(ServiceError) as caught:
        h.create()
    assert caught.value.status == 403
    assert caught.value.code == "PERMISSION_DENIED"


# ---------------------------------------------------------------- 建项目

def test_create_materializes_spec_and_emits_one_event_per_episode():
    h = Harness()
    result = h.create(episodes=[{"title": "第 1 集"}, {"title": "第 2 集"}])
    assert result["lifecycle"] == "draft"
    assert result["head_revision"] == 1
    assert len(result["episodes"]) == 2

    # 规格在写入时物化（§1.4）。
    effective = h.conn.execute(
        "SELECT * FROM episode_spec_effective WHERE tenant_id=?",
        (TA,)).fetchall()
    assert len(effective) == 2
    body = json.loads(effective[0]["body"])
    assert body["output"]["width"] == 1080
    assert body["output"]["height"] == 1920
    assert len(effective[0]["content_hash"]) == 64

    # 每个 episode 一条事件（§5.3），aggregate_id=episode_id。
    events = h.conn.execute(
        "SELECT * FROM outbox WHERE tenant_id=? ORDER BY rowid", (TA,)).fetchall()
    assert len(events) == 2
    for row in events:
        payload = json.loads(row["payload"])
        assert payload["type"] == "project.episode.created"
        assert payload["data"]["schema_version"] == "2.0"
        assert payload["data"]["spec_hash"] == effective[0]["content_hash"]
        assert set(payload["data"]) == {
            "schema_version", "episode_id", "series_id", "project_id",
            "release_ordinal", "shooting_ordinal", "spec_hash"}
    # 首批集默认按 1..N 排 release。
    assert json.loads(events[0]["payload"])["data"]["release_ordinal"] == 1
    assert json.loads(events[1]["payload"])["data"]["release_ordinal"] == 2


def test_event_payload_matches_registered_schema():
    """事件 data 必须正好是契约 schema 的字段集（additionalProperties:false）。

    用真的 schema 文件校验，不是对照我记得的字段名。
    """
    from pathlib import Path
    # 仓库根由本文件位置推出，不靠 cwd：从 services/project-catalog 里跑
    # pytest 时相对路径会指向不存在的文件，测试就会「红得莫名其妙」。
    root = Path(__file__).resolve().parents[3]
    schema = json.loads(
        (root / "contracts" / "project.episode-created.v2.schema.json")
        .read_text("utf-8"))
    h = Harness()
    h.create(episodes=[{"title": "第 1 集"}])
    payload = json.loads(h.conn.execute(
        "SELECT payload FROM outbox").fetchone()["payload"])
    data = payload["data"]
    assert set(data) == set(schema["required"])
    assert schema["additionalProperties"] is False
    assert data["spec_hash"].count("-") == 0 and len(data["spec_hash"]) == 64


def test_replay_returns_original_without_second_write():
    h = Harness()
    first = h.create(episodes=[{"title": "第 1 集"}])
    second = h.create(episodes=[{"title": "第 1 集"}])
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["project_id"] == first["project_id"]
    assert h.conn.execute(
        "SELECT COUNT(*) c FROM project").fetchone()["c"] == 1
    assert h.conn.execute(
        "SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 1


def test_unknown_spec_key_is_rejected_not_ignored():
    """拼错的键被吞掉 = "改了配置但没生效"，且没有任何信号。"""
    broken = {**SPEC, "output": {**SPEC["output"], "aspect_ratios": "9:16"}}
    with pytest.raises(ServiceError) as caught:
        normalize_spec(broken, full=True)
    assert caught.value.status == 422
    assert caught.value.code == "SPEC_FIELD_UNKNOWN"
    assert caught.value.details["unknown"] == ["aspect_ratios"]


def test_partial_segment_is_rejected():
    """半个 output（只有 width）合进基座会产出谁也解释不了的画幅。"""
    with pytest.raises(ServiceError) as caught:
        normalize_spec({"output": {"width": 1080}}, full=False)
    assert caught.value.status == 400
    assert "incomplete" in str(caught.value)


def test_series_override_may_be_partial_by_segment():
    """剧层只覆盖 delivery 时，其余段落继承项目（G12）。"""
    override = normalize_spec(
        {"delivery": {"loudness_target_lufs": -14, "color_primaries": "bt2020"}},
        full=False)
    merged = merge_specs(SPEC, override)
    assert merged["delivery"]["color_primaries"] == "bt2020"
    assert merged["output"]["width"] == 1080


# ---------------------------------------------------------------- 重排

def _three_episode_project(h: Harness) -> tuple[str, list[str]]:
    result = h.create(episodes=[{"title": "第 1 集"}, {"title": "第 2 集"},
                                {"title": "第 3 集"}])
    return result["series_ids"][0], [e["episode_id"] for e in result["episodes"]]


def test_reorder_never_changes_episode_id():
    """§1.1 的核心断言：重排只动 `episode_order`，`episode` 行一个字节不改。"""
    h = Harness()
    series_id, episodes = _three_episode_project(h)
    before = h.conn.execute(
        "SELECT episode_id, head_revision, lifecycle FROM episode"
        " WHERE tenant_id=? ORDER BY episode_id", (TA,)).fetchall()
    before_rows = [tuple(r) for r in before]

    body = {"command_id": "cmd_reorder", "order_kind": "release",
            "window_key": "", "expected_head_revision": 1,
            "order": list(reversed(episodes))}
    result = h.command(TA, body, lambda: h.service.reorder_episodes(
        TA, series_id, body, principal={"principal_id": "p_1"}))

    assert [o["episode_id"] for o in result["order"]] == list(reversed(episodes))
    assert [o["ordinal"] for o in result["order"]] == [1, 2, 3]
    after = h.conn.execute(
        "SELECT episode_id, head_revision, lifecycle FROM episode"
        " WHERE tenant_id=? ORDER BY episode_id", (TA,)).fetchall()
    assert [tuple(r) for r in after] == before_rows

    # 序号确实换了人。
    orders = h.conn.execute(
        "SELECT episode_id, ordinal FROM episode_order WHERE tenant_id=?"
        " AND order_kind='release' ORDER BY ordinal", (TA,)).fetchall()
    assert [o["episode_id"] for o in orders] == list(reversed(episodes))


def test_reorder_requires_full_list():
    """delta 式请求不能被当成完整列表接受（§4.3）。"""
    h = Harness()
    series_id, episodes = _three_episode_project(h)
    body = {"command_id": "cmd_partial", "order_kind": "release",
            "window_key": "", "order": episodes[:2]}
    with pytest.raises(ServiceError) as caught:
        h.command(TA, body, lambda: h.service.reorder_episodes(
            TA, series_id, body, principal={"principal_id": "p_1"}))
    assert caught.value.status == 422
    assert caught.value.code == "ORDER_SET_MISMATCH"
    assert caught.value.details["missing"] == [episodes[2]]
    assert caught.value.details["extra"] == []


def test_reorder_rejects_duplicates_immediately():
    h = Harness()
    series_id, episodes = _three_episode_project(h)
    body = {"command_id": "cmd_dup", "order_kind": "release", "window_key": "",
            "order": [episodes[0], episodes[0], episodes[1]]}
    with pytest.raises(ServiceError) as caught:
        h.command(TA, body, lambda: h.service.reorder_episodes(
            TA, series_id, body, principal={"principal_id": "p_1"}))
    assert caught.value.status == 422
    assert caught.value.code == "DUPLICATE_EPISODE_IN_ORDER"
    assert caught.value.details["duplicates"] == [episodes[0]]


def test_reorder_cas_conflict_reports_current_revision():
    h = Harness()
    series_id, episodes = _three_episode_project(h)
    body = {"command_id": "cmd_stale_reorder", "order_kind": "release",
            "window_key": "", "expected_head_revision": 99,
            "order": list(reversed(episodes))}
    with pytest.raises(ServiceError) as caught:
        h.command(TA, body, lambda: h.service.reorder_episodes(
            TA, series_id, body, principal={"principal_id": "p_1"}))
    assert caught.value.status == 409
    assert caught.value.code == "REVISION_CONFLICT"
    assert caught.value.details["current_revision"] == 1
    # 冲突时不许写：序号还是原样。
    orders = h.conn.execute(
        "SELECT episode_id FROM episode_order WHERE tenant_id=?"
        " AND order_kind='release' ORDER BY ordinal", (TA,)).fetchall()
    assert [o["episode_id"] for o in orders] == episodes


def test_reorder_emits_local_event_not_created_event():
    """重排不是"新集"：发 `project.episode.created` 会让下游重复建 Run 模板。"""
    h = Harness()
    series_id, episodes = _three_episode_project(h)
    created_before = h.conn.execute(
        "SELECT COUNT(*) c FROM outbox").fetchone()["c"]
    body = {"command_id": "cmd_reorder_evt", "order_kind": "release",
            "window_key": "", "order": list(reversed(episodes))}
    h.command(TA, body, lambda: h.service.reorder_episodes(
        TA, series_id, body, principal={"principal_id": "p_1"}))
    assert h.conn.execute(
        "SELECT COUNT(*) c FROM outbox").fetchone()["c"] == created_before
    local = h.conn.execute(
        "SELECT event_type, payload FROM local_event WHERE tenant_id=?"
        " ORDER BY created_at DESC", (TA,)).fetchall()
    types = [r["event_type"] for r in local]
    assert "project.episode.reordered" in types
    reordered = json.loads(next(
        r["payload"] for r in local
        if r["event_type"] == "project.episode.reordered"))
    assert reordered["order"] == list(reversed(episodes))
    assert reordered["head_revision"] == 2


# ---------------------------------------------------------------- 追加与读

def test_append_without_ordinal_is_unscheduled_not_error():
    """纯草稿集合法（§4.2）：它在 `episode_order` 里没有行。"""
    h = Harness()
    created = h.create(episodes=[{"title": "第 1 集"}])
    project_id = created["project_id"]
    body = {"command_id": "cmd_append", "series_id": created["series_ids"][0],
            "episodes": [{"title": "待排期"}]}
    result = h.command(TA, body, lambda: h.service.append_episodes(
        TA, project_id, body, principal={"principal_id": "p_1"}))
    assert result["unsequenced"] == [result["episodes"][0]["episode_id"]]

    listing = h.service.list_episodes(TA, project_id)
    assert listing["unscheduled"] == result["unsequenced"]
    scheduled = [e for e in listing["episodes"] if e["sequenced"]]
    assert len(scheduled) == 1


def test_cross_tenant_episode_is_404_same_code_as_missing():
    """E28：别的租户的集看起来就是"不存在"，不是"无权"。"""
    h = Harness()
    created = h.create(episodes=[{"title": "第 1 集"}])
    episode_id = created["episodes"][0]["episode_id"]

    own = h.service.get_episode_context(TA, episode_id)
    assert own["episode_id"] == episode_id

    with pytest.raises(ServiceError) as other_tenant:
        h.service.get_episode_context(TB, episode_id)
    with pytest.raises(ServiceError) as nonexistent:
        h.service.get_episode_context(TB, "ep_does_not_exist")
    assert other_tenant.value.status == 404
    assert nonexistent.value.status == 404
    # 同码：响应里不能有"这个 ID 存在但属于别人"的信号。
    assert other_tenant.value.code == nonexistent.value.code == "REF_NOT_FOUND"


def test_episode_context_returns_orders_and_no_scene_ids():
    h = Harness()
    created = h.create(episodes=[{"title": "第 1 集",
                                  "shooting_ordinal": 1}])
    episode_id = created["episodes"][0]["episode_id"]
    ctx = h.service.get_episode_context(TA, episode_id)
    assert ctx["orders"]["release"] == {"ordinal": 1, "window_key": ""}
    assert ctx["orders"]["shooting"] == {"ordinal": 1, "window_key": ""}
    assert ctx["spec"]["output"]["aspect_ratio"] == "9:16"
    assert ctx["project_lifecycle"] == "draft"
    assert ctx["source_revision"] == 1
    # 反模式 12 / §4.4：不返回 scene_ids，避免调用方误当权威。
    assert "scene_ids" not in ctx
    assert "scenes" not in ctx


def test_scenes_proxy_carries_authoritative_source_and_stale():
    """代理必须自报"我不是权威"，且空列表 + stale=true ≠ 没有场次。"""
    h = Harness()
    created = h.create(episodes=[{"title": "第 1 集"}])
    episode_id = created["episodes"][0]["episode_id"]
    result = h.service.get_episode_scenes(TA, episode_id)
    assert result["authoritative_source"] == "S05"
    assert result["scenes"] == []
    assert result["stale"] is True
    assert result["projection_watermark"] is None
    assert "not projected yet" in result["note"]


# ---------------------------------------------------------------- 生命周期

def test_project_lifecycle_does_not_cascade_on_archive():
    """归档只标记项目：`episode.lifecycle` 不跟着变（C03-4 / C03-8）。"""
    h = Harness()
    created = h.create(episodes=[{"title": "第 1 集"}])
    body = {"command_id": "cmd_archive", "reason": "项目结项",
            "expected_head_revision": 1}
    h.command(TA, body, lambda: h.service.archive_project(
        TA, created["project_id"], body, principal={"principal_id": "p_1"}))

    project = h.conn.execute(
        "SELECT lifecycle, archived_at FROM project WHERE tenant_id=?",
        (TA,)).fetchone()
    assert project["lifecycle"] == "archived"
    assert project["archived_at"] is not None
    episode = h.conn.execute(
        "SELECT lifecycle FROM episode WHERE tenant_id=?", (TA,)).fetchone()
    assert episode["lifecycle"] == "draft"


def test_archive_without_run_probe_reports_no_warning():
    """run_probe 没配 = 没查。**不许**报 ACTIVE_RUNS_EXIST count=0。"""
    h = Harness(run_probe=None)
    created = h.create()
    body = {"command_id": "cmd_arch_noprobe", "reason": "结项"}
    result = h.command(TA, body, lambda: h.service.archive_project(
        TA, created["project_id"], body, principal={"principal_id": "p_1"}))
    assert result["warnings"] == []
    assert result["active_runs_counted"] is False


def test_archive_with_active_runs_warns_but_does_not_block():
    h = Harness(run_probe=lambda tenant_id, project_id: 3)
    created = h.create()
    body = {"command_id": "cmd_arch_runs", "reason": "结项"}
    result = h.command(TA, body, lambda: h.service.archive_project(
        TA, created["project_id"], body, principal={"principal_id": "p_1"}))
    assert result["lifecycle"] == "archived"
    assert result["warnings"] == [{"code": "ACTIVE_RUNS_EXIST", "count": 3}]
    assert result["active_runs_counted"] is True


def test_archived_project_rejects_new_episodes():
    h = Harness()
    created = h.create()
    body = {"command_id": "cmd_arch_2", "reason": "结项"}
    h.command(TA, body, lambda: h.service.archive_project(
        TA, created["project_id"], body, principal={"principal_id": "p_1"}))
    body2 = {"command_id": "cmd_append_archived",
             "series_id": created["series_ids"][0],
             "episodes": [{"title": "新集"}]}
    with pytest.raises(ServiceError) as caught:
        h.command(TA, body2, lambda: h.service.append_episodes(
            TA, created["project_id"], body2,
            principal={"principal_id": "p_1"}))
    assert caught.value.status == 409
    assert caught.value.code == "PROJECT_ARCHIVED"


def test_completed_is_reversible():
    """§3.2：`completed` 是 S08 的投影，可回退。只有 `archived` 不可。"""
    h = Harness()
    created = h.create(episodes=[{"title": "第 1 集"}])
    episode_id = created["episodes"][0]["episode_id"]

    first = h.service.apply_acceptance_projection(
        TA, episode_id, has_accepted_master=True)
    assert first["lifecycle"] == "completed"
    # S08 把 master 退回未接受 → 回到 in_production，不是卡在 completed。
    second = h.service.apply_acceptance_projection(
        TA, episode_id, has_accepted_master=False)
    assert second["lifecycle"] == "in_production"
    assert second["changed"] is True

    # 归档之后投影不许拉回来。
    h.service.conn.execute(
        "UPDATE episode SET lifecycle='archived', archived_at='2026-01-01T00:00:00.000Z'"
        " WHERE tenant_id=? AND episode_id=?", (TA, episode_id))
    stuck = h.service.apply_acceptance_projection(
        TA, episode_id, has_accepted_master=True)
    assert stuck["lifecycle"] == "archived"
    assert stuck["changed"] is False


def test_spec_change_does_not_touch_existing_episodes():
    """§4.5：改剧规格只影响之后物化的集。"""
    h = Harness()
    created = h.create(episodes=[{"title": "第 1 集"}])
    series_id = created["series_ids"][0]
    episode_id = created["episodes"][0]["episode_id"]
    before = h.conn.execute(
        "SELECT content_hash FROM episode_spec_effective WHERE tenant_id=?"
        " AND episode_id=?", (TA, episode_id)).fetchone()["content_hash"]

    body = {"command_id": "cmd_spec", "expected_head_revision": 1,
            "body": {"delivery": {"loudness_target_lufs": -23,
                                  "color_primaries": "bt2020"}}}
    result = h.command(TA, body, lambda: h.service.publish_spec_revision(
        TA, series_id, body, principal={"principal_id": "p_1"}))
    assert result["applied_to_existing"] == 0

    after = h.conn.execute(
        "SELECT content_hash FROM episode_spec_effective WHERE tenant_id=?"
        " AND episode_id=?", (TA, episode_id)).fetchone()["content_hash"]
    assert after == before

    # 之后追加的集用新规格。
    body2 = {"command_id": "cmd_append_after_spec", "series_id": series_id,
             "episodes": [{"title": "第 2 集"}]}
    h.command(TA, body2, lambda: h.service.append_episodes(
        TA, created["project_id"], body2, principal={"principal_id": "p_1"}))
    recent = h.conn.execute(
        "SELECT body FROM episode_spec_effective WHERE tenant_id=?"
        " ORDER BY materialized_at DESC, rowid DESC LIMIT 1", (TA,)).fetchone()
    assert json.loads(recent["body"])["delivery"]["color_primaries"] == "bt2020"


def test_apply_to_existing_is_refused():
    h = Harness()
    created = h.create()
    body = {"command_id": "cmd_spec_all",
            "body": {"default_branch": "alt"}, "apply_to_existing": True}
    with pytest.raises(ServiceError) as caught:
        h.command(TA, body, lambda: h.service.publish_spec_revision(
            TA, created["series_ids"][0], body,
            principal={"principal_id": "p_1"}))
    assert caught.value.status == 422
    assert caught.value.code == "APPLY_TO_EXISTING_UNSUPPORTED"


# ---------------------------------------------------------------- 其它

def test_story_clock_unknown_warns_but_does_not_block():
    """§4.6 第 11 条：S16 不认识的时钟只告警，不拦。"""
    h = Harness(clock_validator=lambda tenant_id, clock_id: False)
    result = h.create(episodes=[{"title": "第 1 集",
                                 "story_window_ref": {"clock_id": "story_x"}}])
    assert len(result["episodes"]) == 1
    assert result["warnings"][0]["code"] == "STORY_CLOCK_UNKNOWN"
    assert result["warnings"][0]["clock_id"] == "story_x"


def test_story_clock_unchecked_produces_no_warning():
    """没配校验器 = 没查，不是"查了不存在"。"""
    h = Harness()
    result = h.create(episodes=[{"title": "第 1 集",
                                 "story_window_ref": {"clock_id": "story_x"}}])
    assert result["warnings"] == []


def test_membership_is_assignment_not_authorization():
    h = Harness()
    created = h.create()
    body = {"command_id": "cmd_member", "principal_id": "p_2",
            "role_key": "director"}
    result = h.command(TA, body, lambda: h.service.add_member(
        TA, created["project_id"], body, principal={"principal_id": "p_1"}))
    assert result["already_assigned"] is False
    assert "not a permission grant" in result["note"]

    again = {"command_id": "cmd_member_2", "principal_id": "p_2",
             "role_key": "director"}
    replay = h.command(TA, again, lambda: h.service.add_member(
        TA, created["project_id"], again, principal={"principal_id": "p_1"}))
    assert replay["already_assigned"] is True
    assert h.conn.execute(
        "SELECT COUNT(*) c FROM member").fetchone()["c"] == 1


def test_sweep_removes_abandoned_in_progress_rows():
    """§6.1 清扫：卡住的 IN_PROGRESS 行删掉，重发才可能成功（G7）。"""
    h = Harness()
    h.idem.begin(TA, "cmd_stuck", "hash")
    h.conn.execute(
        "UPDATE idempotency_record SET created_at='2000-01-01T00:00:00.000Z'"
        " WHERE command_id='cmd_stuck'")
    removed = h.service.sweep_stale_commands(older_than_seconds=60)
    assert removed == 1
    assert h.conn.execute(
        "SELECT COUNT(*) c FROM idempotency_record").fetchone()["c"] == 0


def test_every_response_carries_contract_gaps():
    """能响应不等于契约完整：缺口必须随响应离开服务。"""
    h = Harness()
    created = h.create(episodes=[{"title": "第 1 集"}])
    assert created["x-contract-gaps"] == CONTRACT_GAPS
    episode_id = created["episodes"][0]["episode_id"]
    for payload in (h.service.get_project(TA, created["project_id"]),
                    h.service.list_episodes(TA, created["project_id"]),
                    h.service.get_episode_context(TA, episode_id),
                    h.service.get_episode_scenes(TA, episode_id)):
        assert payload["x-contract-gaps"] == CONTRACT_GAPS
    assert any("PEC-1" in gap for gap in CONTRACT_GAPS)
    assert any("no S03 path" in gap for gap in CONTRACT_GAPS)
