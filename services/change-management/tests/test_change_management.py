"""S07 单测。编号对应 `plans/S07-change-management.md` §8 的 T07-01…T07-45。

上游全部是 stub，**按真实的上游契约写**，不是按内核希望的样子写：

- S06 未实现（全仓没有 `services/storyboard/api/app.py`），所以"编号→shot"
  与"准备 revision"这两个请求形状是 `deps.build_shot_source` /
  `build_shot_preparer` 里 S07 侧写下的约定（S07-30）。测试照抄那个形状，
  **不**另造别名。
- S02 的 `POST /policy-decisions:resolve-effective` 返回 `effective`，没有
  `decision`；`build_policy_source` 已把它折成 deny/abstain/require_human/
  allow（S07-11）。stub 返回的是**折完**的形状，因为内核吃的是折完的。
- S09 的 `impact` 里多一个 `derived_adjustments`（S07-12）；`_impact_of`
  必须只留 schema 的 6 个键。测试带上它，正是为了让"多出来的被丢掉"可观测。
- S08 的 `GET /manifests/{id}/revisions` 列表**没有 payload**（S07-13）；
  恢复要靠逐条读 detail。stub 严格照这个形状。
- S16 没有 dry-run（S07-29）；preview stub 用 `warning` 复现"上游并不真的
  支持 dry_run"的情形。

硬约束：这条路上没有余额、没有 permit、没有 node_epoch、没有 accepted
pointer。凡是能证明这一点的用例都断言"库里查不到 / 表里没有这一列"，
而不是只断言"响应里没有"。
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

from change_management import ChangeManagementService, ServiceError
from change_management import deps
from change_management.schema import (
    CONTRACT_GAPS,
    PLAN_STATES,
    PLAN_TRANSITIONS,
    SAGA_STATES,
    SAGA_TRANSITIONS,
    SCHEMA,
    TERMINAL_PLAN_STATES,
)
from change_management.service import (
    SAGA_STEPS,
    parse_instruction,
    plan_hash_of,
    saga_command_id,
)


ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 13, 9, 0, 0, tzinfo=timezone.utc)

POLICY_REF = {"service_id": "S02", "id": "pol_1", "revision": 3,
              "content_hash": "a" * 64}
MANIFEST_REF = {"id": "man_1", "revision": 7, "content_hash": "b" * 64}


def shot(shot_id="shot_3_2", episode_id="ep_1", revision=4, fields=None,
         layer=None, summary="", content_hash="c" * 64):
    """S06 的 shot 形状（S07 侧约定的解析结果）。"""
    return {"shot_id": shot_id, "episode_id": episode_id,
            "revision": revision, "content_hash": content_hash,
            "fields": dict(fields if fields is not None else {
                "/camera/angle": "eye_level",
                "/duration/target_seconds": 6,
            }),
            "layer": layer, "summary": summary}


def context(**over):
    base = {
        "schema_version": "2.0", "tenant_id": "t1",
        "project_id": "prj_1", "series_id": "ser_1", "episode_id": "ep_1",
        "branch_id": "br_1", "home_region": "cn", "home_cell": "cell_a",
        "manifest_ref": dict(MANIFEST_REF), "execution_fence": 11,
        "memory_checkpoint": {"commit_sequence": 3, "content_hash": "d" * 64},
        "policy_ref": dict(POLICY_REF),
        "actor": {"principal_id": "u_1", "principal_type": "user",
                  "delegation_id": "dlg_1"},
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not ...}


class Harness:
    """内存库 + 十三个上游 stub + 与 `api/app.py` 同形的幂等外壳。"""

    def __init__(self, conn=None, **over):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)
        self.now = over.get("now", T0)

        self.shots = over.get("shots", [shot()])
        self.prepared = over.get("prepared", None)
        self.docs = over.get("docs", {})
        self.impact = over.get("impact", {
            "analysis_id": "an_1",
            "impact": {
                "regenerate_node_ids": ["shot_3_2.video", "shot_3_2.audio"],
                "recheck_node_ids": ["ep_1.rough_cut"],
                "reuse_artifact_refs": ["art_1"],
                "affected_episode_ids": ["ep_1"],
                "superseded_claim_refs": [],
                "lineage_watermark": 42,
                # S07-12：S09 多塞的键，内核必须丢掉。
                "derived_adjustments": ["S09 自己的叙述"],
            },
            "nodes": [
                {"episode_id": "ep_1", "decision": "regenerate", "delivered": False},
                {"episode_id": "ep_1", "decision": "recheck", "delivered": False},
            ],
            "by_stage": [{"stage": "video", "count": 2,
                          "cost_upper_micro": 900000}],
        })
        self.policy = over.get("policy", {"decision": "allow",
                                          "required_decisions": ["node_change_allowed"]})
        self.cost = over.get("cost", {"estimated_cost_upper_micro": 1200000,
                                      "currency": "USD"})
        self.manifest = over.get("manifest", {"current_revision": 7,
                                              "execution_fence": 11})
        self.committed = over.get("committed", {
            "manifest": {"id": "man_1", "revision": 8,
                         "content_hash": "e" * 64},
            "changed_node_epochs": [{"node_id": "shot_3_2",
                                     "node_epoch": 5}],
        })
        self.revisions = over.get("revisions", {"items": [{"revision": 7},
                                                          {"revision": 8}]})
        self.revision_payload = over.get("revision_payload", {})
        self.preview = over.get("preview", {
            "memory_migration_required": True,
            "delivered_episode_ids": ["ep_0"],
        })
        self.memory_commit = over.get("memory_commit", {
            "commit_sequence": 4, "checkpoint_hash": "f" * 64})

        self.calls = {"resolve": 0, "prepare": 0, "read": 0, "content": 0,
                      "impact": 0, "policy": 0, "cost": 0, "manifest": 0,
                      "commit": 0, "list": 0, "detail": 0, "preview": 0,
                      "memory": 0, "impact_queries": [], "policy_calls": [],
                      "commit_body": None, "memory_body": None}

        self.service = ChangeManagementService(
            self.conn,
            shot_resolver=self._resolve,
            shot_preparer=self._prepare,
            revision_reader=self._read,
            content_preparer=self._content,
            impact_source=self._impact,
            policy_source=self._policy,
            cost_source=self._cost,
            manifest_source=self._manifest,
            manifest_committer=self._commit,
            revision_lister=self._list,
            manifest_revision_reader=self._detail,
            memory_preview=self._preview,
            memory_committer=self._memory,
            clock=lambda: self.now,
            ttl_seconds=over.get("ttl_seconds", 1800),
        )

    # ---- stubs：签名与 `deps.build_*` 一致 ----
    def _resolve(self, tenant_id, series_id, episode_id, scene_no,
                 shot_no_in_scene, pinned):
        self.calls["resolve"] += 1
        if isinstance(self.shots, Exception):
            raise self.shots
        if pinned:
            return {"shots": [s for s in self.shots
                              if s["shot_id"] == pinned]}
        return {"shots": list(self.shots)}

    def _prepare(self, tenant_id, body):
        self.calls["prepare"] += 1
        if isinstance(self.prepared, Exception):
            raise self.prepared
        if self.prepared is not None:
            return self.prepared
        fields = dict(body["fields"])
        for change in body["changes"]:
            if change["op"] == "SET":
                fields[change["field"]] = change["value"]
            else:
                fields[change["field"]] = float(
                    fields[change["field"]]) - change["delta_seconds"]
        return {"prepared_ref": {"service_id": "S06", "id": "shot_3_2",
                                 "revision": int(body["revision"]) + 1,
                                 "content_hash": "9" * 64},
                "fields": fields,
                "allowed_values": {"/camera/angle": ["eye_level", "low",
                                                     "high", "overhead",
                                                     "dutch"]},
                "derived": [],
                "dialogue_min_duration_ms": None}

    def _read(self, tenant_id, service_id, object_id, revision):
        self.calls["read"] += 1
        if isinstance(self.docs, Exception):
            raise self.docs
        key = (object_id, int(revision))
        if key in self.docs:
            value = self.docs[key]
            if isinstance(value, Exception):
                raise value
            return value
        # 默认：基线与准备产物都可读，正文是 shot 的字段表。
        base = self.shots[0] if not isinstance(self.shots, Exception) else shot()
        return {"fields": dict(base["fields"]), "revision": int(revision)}

    def _content(self, tenant_id, body):
        self.calls["content"] += 1
        return {"prepared_refs": [{"service_id": "S06", "id": "node_a",
                                   "revision": 1, "content_hash": "8" * 64}]}

    def _impact(self, tenant_id, query):
        self.calls["impact"] += 1
        self.calls["impact_queries"].append(query)
        if isinstance(self.impact, Exception):
            raise self.impact
        return self.impact

    def _policy(self, tenant_id, policy_ref, kind, facts):
        self.calls["policy"] += 1
        self.calls["policy_calls"].append({"kind": kind, "facts": facts})
        value = self.policy
        if isinstance(value, list):
            value = value[min(self.calls["policy"] - 1, len(value) - 1)]
        if isinstance(value, Exception):
            raise value
        return value

    def _cost(self, tenant_id, query):
        self.calls["cost"] += 1
        if isinstance(self.cost, Exception):
            raise self.cost
        return self.cost

    def _manifest(self, tenant_id, manifest_id):
        self.calls["manifest"] += 1
        if isinstance(self.manifest, Exception):
            raise self.manifest
        return self.manifest

    def _commit(self, tenant_id, body):
        self.calls["commit"] += 1
        self.calls["commit_body"] = body
        if isinstance(self.committed, Exception):
            raise self.committed
        return self.committed

    def _list(self, tenant_id, manifest_id, limit):
        self.calls["list"] += 1
        if isinstance(self.revisions, Exception):
            raise self.revisions
        return self.revisions

    def _detail(self, tenant_id, manifest_id, revision):
        self.calls["detail"] += 1
        if isinstance(self.revision_payload, Exception):
            raise self.revision_payload
        return self.revision_payload.get(int(revision), {})

    def _preview(self, tenant_id, series_id, body):
        self.calls["preview"] += 1
        if isinstance(self.preview, Exception):
            raise self.preview
        return self.preview

    def _memory(self, tenant_id, series_id, body):
        self.calls["memory"] += 1
        self.calls["memory_body"] = body
        if isinstance(self.memory_commit, Exception):
            raise self.memory_commit
        return self.memory_commit

    # ---- 幂等外壳：与 `api/app.py` 的 `command()` 同语义 ----
    def command(self, body, tenant_id="t1", command_id="c1"):
        payload = {**body, "command_id": command_id}
        request_hash = canonical_request_hash(payload)
        record = self.idem.begin(tenant_id, command_id, request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = self.service.create_plan(tenant_id, command_id, payload)
        except Exception:
            self.idem.abort(tenant_id, command_id)
            raise
        self.idem.finish(tenant_id, command_id,
                         json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # ---- 命令体 ----
    def body(self, instruction="把第3场第2镜改成仰拍", **over):
        payload = {"context": context(**over.pop("context_over", {})),
                   "instruction": instruction}
        payload.update(over)
        return payload

    def create(self, instruction="把第3场第2镜改成仰拍", tenant_id="t1",
               command_id="c1", **over):
        return self.command(self.body(instruction, **over),
                            tenant_id=tenant_id, command_id=command_id)

    def apply(self, plan_id, plan_hash, tenant_id="t1", command_id="a1",
              expected=7, **over):
        payload = {"plan_hash": plan_hash,
                   "expected_manifest_revision": expected}
        payload.update(over)
        return self.service.apply(tenant_id, plan_id, command_id, payload)

    # ---- 查询助手 ----
    def rows(self, table, **where):
        clause = " AND ".join(f"{k}=?" for k in where) or "1"
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {clause}",
            tuple(where.values())).fetchall()

    def count(self, table):
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def column(self, table, name):
        return [r[1] for r in self.conn.execute(
            f"PRAGMA table_info({table})").fetchall()]

    def tables(self):
        return {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    def events(self, kind=None):
        out = [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM outbox ORDER BY created_at").fetchall()]
        if kind is None:
            return out
        return [e for e in out if e.get("type") == kind]


# `enterprise-change-plan.v2` 的 impact 是 additionalProperties:false，
# 只有这六个键。S09 多塞的 `derived_adjustments` 必须被丢掉（S07-12）。
SCHEMA_IMPACT_KEYS = {
    "regenerate_node_ids", "recheck_node_ids", "reuse_artifact_refs",
    "affected_episode_ids", "superseded_claim_refs", "lineage_watermark"}


def prepared_with(**over):
    """S06 准备 revision 的响应形状（`deps.build_shot_preparer` 的约定）。"""
    base = {
        "prepared_ref": {"service_id": "S06", "id": "shot_3_2", "revision": 5,
                         "content_hash": "9" * 64},
        "fields": {"/camera/angle": "low", "/duration/target_seconds": 4},
        "allowed_values": {"/camera/angle": ["eye_level", "low", "high",
                                             "overhead", "dutch"]},
        "derived": [],
        "dialogue_min_duration_ms": None,
    }
    base.update(over)
    return base


def expect(exc_info, status: int, code: str) -> None:
    assert exc_info.value.status == status, exc_info.value.code
    assert exc_info.value.code == code


# ================================================================ 幂等

class TestIdempotency:
    """T07-33 / T07-34 / T07-39，§6.1 的四行表。"""

    def test_same_key_same_body_returns_first_plan(self):
        # T07-33：同键同 body 返回首次结果，不生成第二份计划。
        h = Harness()
        first = h.create(command_id="c1")
        second = h.create(command_id="c1")
        assert second["replayed"] is True
        assert second["plan_id"] == first["plan_id"]
        assert second["plan_hash"] == first["plan_hash"]
        assert h.count("change_plan") == 1

    def test_replay_does_not_reread_upstream(self):
        # 重放不能拿"此刻的 manifest / 政策"换掉用户已确认的那份。
        h = Harness()
        h.create(command_id="c1")
        snapshot = dict(h.calls)
        h.create(command_id="c1")
        assert h.calls == snapshot

    def test_replay_does_not_write_a_second_event(self):
        h = Harness()
        h.create(command_id="c1")
        before = h.count("local_event")
        h.create(command_id="c1")
        assert h.count("local_event") == before == 1

    def test_same_key_different_body_is_conflict(self):
        # T07-34：409 IDEMPOTENCY_KEY_REUSED，不是覆盖。
        h = Harness()
        h.create(command_id="c1")
        with pytest.raises(CommandConflict) as info:
            h.create(instruction="把第3场第2镜缩到4秒", command_id="c1")
        assert info.value.code == "IDEMPOTENCY_KEY_REUSED"
        assert h.count("change_plan") == 1

    def test_conflict_leaves_the_original_plan_untouched(self):
        h = Harness()
        first = h.create(command_id="c1")
        with pytest.raises(CommandConflict):
            h.create(instruction="把第3场第2镜缩到4秒", command_id="c1")
        row = h.rows("change_plan", plan_id=first["plan_id"])[0]
        assert row["instruction"] == "把第3场第2镜改成仰拍"
        assert row["plan_hash"] == first["plan_hash"]

    def test_two_command_ids_make_two_plans(self):
        # 不同键 = 两次真实的生成请求（第二次会 supersede 第一次）。
        h = Harness()
        h.create(command_id="c1")
        h.create(command_id="c2")
        assert h.count("change_plan") == 2

    def test_failed_command_releases_the_key(self):
        # 失败不能把键永久占住：abort 之后同一个键可以重来。
        h = Harness(shots=[])
        with pytest.raises(ServiceError):
            h.create(command_id="c1")
        assert h.count("idempotency_record") == 0
        h.shots = [shot()]
        result = h.create(command_id="c1")
        assert result["state"] == "PLANNED"

    def test_in_progress_key_is_409_retryable(self):
        h = Harness()
        body = {**h.body(), "command_id": "c9"}
        h.idem.begin("t1", "c9", canonical_request_hash(body))
        with pytest.raises(Exception) as info:
            h.idem.begin("t1", "c9", canonical_request_hash(body))
        assert info.value.code == "COMMAND_IN_PROGRESS"
        assert info.value.retryable is True


# ================================================================ E03 生成

class TestE03PlanGeneration:
    """§8.1：T07-01…T07-09。三句人话 → 一份可核对的计划。"""

    def test_t07_01_angle_word_becomes_s06_token(self):
        h = Harness()
        plan = h.create("把第3场第2镜改成仰拍")
        assert plan["state"] == "PLANNED"
        patch = plan["patches"][0]
        assert patch["path"] == "/camera/angle"
        assert patch["before"] == "eye_level"
        assert patch["after"] == "low"       # 中文词 → S06 词元
        assert patch["target_ref"] == {
            "service_id": "S06", "id": "shot_3_2", "revision": 4,
            "content_hash": "c" * 64}

    def test_t07_01_token_must_be_in_s06_enum(self):
        # T07-24 的落点：枚举来自 S06，不是硬编码。把 S06 的枚举改成不含 low，
        # 同一个词元就必须 422，而不是照样写进去。
        h = Harness(prepared=prepared_with(
            allowed_values={"/camera/angle": ["eye_level", "high"]}))
        with pytest.raises(ServiceError) as info:
            h.create()
        expect(info, 422, "INSTRUCTION_UNPARSEABLE")
        assert info.value.details["allowed"] == ["eye_level", "high"]
        assert h.count("change_plan_patch") == 0

    def test_t07_02_absolute_duration(self):
        h = Harness(prepared=prepared_with(
            fields={"/duration/target_seconds": 4}))
        plan = h.create("把第3场第2镜缩到4秒")
        patch = plan["patches"][0]
        assert patch["path"] == "/duration/target_seconds"
        assert patch["before"] == 6
        assert patch["after"] == 4

    def test_t07_03_relative_duration_reads_current_value(self):
        # "缩2秒" 必须读当前值做算术（6-2=4），不能拿一个默认 6 去算。
        h = Harness(shots=[shot(fields={"/camera/angle": "eye_level",
                                        "/duration/target_seconds": 9})],
                    prepared=prepared_with(
                        fields={"/duration/target_seconds": 7}))
        plan = h.create("把第3场第2镜缩2秒")
        patch = plan["patches"][0]
        assert patch["before"] == 9
        assert patch["after"] == 7

    def test_t07_03_before_value_is_persisted(self):
        # CHECK_PATCH 依赖库里的 before_value 做前置校验，它必须是真值。
        h = Harness(prepared=prepared_with(
            fields={"/duration/target_seconds": 4}))
        h.create("把第3场第2镜缩2秒")
        row = h.rows("change_plan_patch",
                     path="/duration/target_seconds")[0]
        assert json.loads(row["before_value"]) == 6
        assert json.loads(row["after_value"]) == 4

    def test_t07_03_relative_duration_needs_a_numeric_current(self):
        h = Harness(shots=[shot(fields={"/camera/angle": "eye_level"})])
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜缩2秒")
        expect(info, 422, "INSTRUCTION_UNPARSEABLE")

    def test_t07_04_derived_adjustment_points_at_its_trigger(self):
        derived = [{"path": "/duration/end_pause",
                    "derived_from_path": "/duration/target_seconds",
                    "derivation_rule": "shrink_keeps_end_pause"}]
        h = Harness(prepared=prepared_with(
            fields={"/camera/angle": "eye_level",
                    "/duration/target_seconds": 4, "/duration/end_pause": 0.3},
            derived=derived))
        plan = h.create("把第3场第2镜缩到4秒")
        assert plan["patches"] and len(plan["patches"]) == 1  # 只发用户要的
        assert any("end_pause" in text for text in plan["derived_adjustments"])
        row = h.rows("change_plan_patch", path="/duration/end_pause")[0]
        assert row["derived_from"] == "1"              # 指向时长补丁的 seq
        assert row["derivation_rule"] == "shrink_keeps_end_pause"

    def test_t07_04_derived_row_is_auditable_but_not_in_patches(self):
        derived = [{"path": "/duration/end_pause",
                    "derived_from_path": "/duration/target_seconds",
                    "derivation_rule": "shrink_keeps_end_pause"}]
        h = Harness(prepared=prepared_with(
            fields={"/camera/angle": "eye_level",
                    "/duration/target_seconds": 4, "/duration/end_pause": 0.3},
            derived=derived))
        h.create("把第3场第2镜缩到4秒")
        assert h.count("change_plan_patch") == 2
        assert [r["path"] for r in h.rows("change_plan_patch")] == [
            "/duration/target_seconds", "/duration/end_pause"]

    def test_t07_05_derivation_rule_name_comes_from_s06(self):
        # 换个规则名，库里就跟着变：说明 S07 没有硬编码它。
        for rule in ("shrink_keeps_end_pause", "some_other_rule"):
            h = Harness(prepared=prepared_with(
                fields={"/camera/angle": "eye_level",
                        "/duration/target_seconds": 4,
                        "/duration/end_pause": 0.3},
                derived=[{"path": "/duration/end_pause",
                          "derived_from_path": "/duration/target_seconds",
                          "derivation_rule": rule}]))
            h.create("把第3场第2镜缩到4秒")
            assert h.rows("change_plan_patch",
                          path="/duration/end_pause")[0]["derivation_rule"] == rule

    def test_t07_06_impact_is_field_by_field_the_s09_answer(self):
        h = Harness()
        plan = h.create()
        assert set(plan["impact"]) == SCHEMA_IMPACT_KEYS
        assert plan["impact"]["regenerate_node_ids"] == [
            "shot_3_2.video", "shot_3_2.audio"]
        assert plan["impact"]["recheck_node_ids"] == ["ep_1.rough_cut"]
        assert plan["impact"]["reuse_artifact_refs"] == ["art_1"]
        assert plan["impact"]["affected_episode_ids"] == ["ep_1"]
        assert plan["impact"]["superseded_claim_refs"] == []
        assert plan["impact"]["lineage_watermark"] == 42

    def test_t07_06_extra_s09_keys_do_not_leak_into_the_contract(self):
        h = Harness()
        plan = h.create()
        assert "derived_adjustments" not in plan["impact"]
        assert "unused" not in plan["impact"]

    def test_t07_07_cost_comes_from_s25(self):
        h = Harness(cost={"estimated_cost_upper_micro": 7654321,
                          "currency": "USD"})
        plan = h.create()
        assert plan["estimated_cost_upper_micro"] == 7654321
        assert plan["currency"] == "USD"
        row = h.rows("change_plan", plan_id=plan["plan_id"])[0]
        assert row["estimated_cost_upper_micro"] == 7654321

    def test_t07_07_no_cost_upper_means_no_plan(self):
        # S25 答了，但没给上界：不给计划。这是语义失败，不是"不可达"。
        h = Harness(cost={"currency": "USD"})
        with pytest.raises(ServiceError) as info:
            h.create()
        expect(info, 503, "COST_SOURCE_UNAVAILABLE")
        assert h.count("change_plan_patch") == 0

    def test_unreachable_s25_is_dependency_unavailable(self):
        # §5.7：传输层不可达统一 DEPENDENCY_UNAVAILABLE、retryable。
        h = Harness(cost=ConnectionError("boom"))
        with pytest.raises(ServiceError) as info:
            h.create()
        expect(info, 503, "DEPENDENCY_UNAVAILABLE")
        assert info.value.retryable is True

    def test_t07_08_regenerate_list_is_not_extended_by_s07(self):
        impact = {
            "analysis_id": "an_2",
            "impact": {"regenerate_node_ids": ["shot_3_2.video"],
                       "recheck_node_ids": [], "reuse_artifact_refs": [],
                       "affected_episode_ids": ["ep_1"],
                       "superseded_claim_refs": [], "lineage_watermark": 7},
        }
        h = Harness(impact=impact)
        plan = h.create()
        assert plan["impact"]["regenerate_node_ids"] == ["shot_3_2.video"]
        assert not any(node.startswith("shot_3_4")
                       for node in plan["impact"]["regenerate_node_ids"])

    def test_t07_08_s07_does_not_add_nodes_for_other_shots(self):
        h = Harness()
        plan = h.create()
        assert all(node.startswith("shot_3_2.")
                   for node in plan["impact"]["regenerate_node_ids"])

    def test_t07_09_changed_node_epochs_are_recorded_verbatim(self):
        # S09 只谈重做集合；epoch 是 S08 的事实。两者本来就可以不同。
        h = Harness()
        plan = h.create()
        h.apply(plan["plan_id"], plan["plan_hash"])
        event = h.events("change.plan.applied")[0]
        assert event["data"]["changed_node_epochs"] == [
            {"node_id": "shot_3_2", "node_epoch": 5}]
        assert [n for n in event["data"]["regenerate_node_ids"]] == [
            "shot_3_2.video", "shot_3_2.audio"]

    def test_t07_09_s07_never_sends_epoch_as_an_instruction(self):
        # 交给 S08 的是"意图"（regenerate_node_ids）与准备产物，不是 epoch。
        h = Harness()
        plan = h.create()
        h.apply(plan["plan_id"], plan["plan_hash"])
        body = h.calls["commit_body"]
        assert "change_plan_ref" in body
        assert body["change_plan_ref"]["id"] == plan["plan_id"]
        assert "node_epoch" not in json.dumps(body)
        assert "accepted" not in json.dumps(body)


# ================================================================ E04 澄清

class TestE04Clarification:
    """§8.2：T07-10…T07-18。缺集 / 镜头歧义 / 对白超长。"""

    def _clarification(self, info) -> dict:
        expect(info, 409, "CLARIFICATION_REQUIRED")
        return info.value.details["clarification"]

    def test_t07_10_series_unique_match_is_allowed_with_a_warning(self):
        # 指令没说集，但 series 内只有一处能对上：按它继续，但要说出来。
        h = Harness()
        plan = h.create("把第3场第2镜改成仰拍",
                        context_over={"episode_id": ...})
        assert plan["state"] == "PLANNED"
        summary = h.service.get_impact_summary("t1", plan["plan_id"])
        assert any("SERIES_UNIQUE_MATCH" in w for w in summary["warnings"])

    def test_t07_11_missing_episode_context_is_a_clarification(self):
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1"),
            shot(shot_id="shot_b", episode_id="ep_2"),
        ])
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...})
        clarification = self._clarification(info)
        assert clarification["kind"] == "MISSING_EPISODE_CONTEXT"
        assert info.value.details["plan_id"].startswith("change_")
        assert len(clarification["options"]) >= 2

    def test_t07_11_missing_episode_options_are_real_episodes(self):
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1"),
            shot(shot_id="shot_b", episode_id="ep_2"),
            shot(shot_id="shot_c", episode_id="ep_3"),
        ])
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...})
        options = self._clarification(info)["options"]
        # 每个选项都是可量化的：label 指明集，consequence 带镜头与版本。
        assert len(options) == 3
        assert all(o["consequence"] for o in options)
        assert [o["option_id"] for o in options] == [
            "opt_episode_1", "opt_episode_2", "opt_episode_3"]

    def test_t07_11_plan_is_parked_not_failed(self):
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1"),
            shot(shot_id="shot_b", episode_id="ep_2"),
        ])
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...})
        plan_id = info.value.details["plan_id"]
        assert h.service.plan_state("t1", plan_id)["state"] == \
            "CLARIFICATION_REQUIRED"
        assert h.count("change_plan_patch") == 0

    def test_t07_12_ambiguous_shot_options_are_candidate_shots(self):
        h = Harness(shots=[
            shot(shot_id="shot_a", revision=2, summary="定稿 A"),
            shot(shot_id="shot_b", revision=3, summary="定稿 B"),
        ])
        with pytest.raises(ServiceError) as info:
            h.create()
        clarification = self._clarification(info)
        assert clarification["kind"] == "AMBIGUOUS_SHOT"
        assert [o["option_id"] for o in clarification["options"]] == [
            "opt_shot_1", "opt_shot_2"]
        assert "shot_a" in clarification["options"][0]["label"]

    def test_t07_12_ambiguous_inside_one_episode_is_still_ambiguous(self):
        # 六集都缺集号是 MISSING_EPISODE_CONTEXT；同一个集里两个同编号
        # 镜头是 AMBIGUOUS_SHOT。用错会提炼出只有一个选项的"澄清"。
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1"),
            shot(shot_id="shot_b", episode_id="ep_1"),
        ])
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...})
        assert self._clarification(info)["kind"] == "AMBIGUOUS_SHOT"

    def test_t07_13_dialogue_exceeds_duration_offers_three_real_options(self):
        h = Harness(prepared=prepared_with(
            fields={"/duration/target_seconds": 4},
            dialogue_min_duration_ms=5200))
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜缩到4秒")
        clarification = self._clarification(info)
        assert clarification["kind"] == "DIALOGUE_EXCEEDS_DURATION"
        ids = [o["option_id"] for o in clarification["options"]]
        assert ids == ["opt_extend_to_min", "opt_rewrite_dialogue", "opt_cancel"]

    def test_t07_13_each_option_has_a_quantified_consequence(self):
        h = Harness(prepared=prepared_with(
            fields={"/duration/target_seconds": 4},
            dialogue_min_duration_ms=5200))
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜缩到4秒")
        options = self._clarification(info)["options"]
        assert "5.2" in options[0]["label"]
        assert options[0]["cost_delta_micro"] > 0
        assert options[1]["cost_delta_micro"] > options[0]["cost_delta_micro"]
        assert options[2]["cost_delta_micro"] == 0

    def test_t07_14_no_option_deletes_or_truncates_dialogue(self):
        # 删台词会改剧情，需要剧本层决定（docs/06 §1）。S07 不给这个选项。
        h = Harness(prepared=prepared_with(
            fields={"/duration/target_seconds": 4},
            dialogue_min_duration_ms=5200))
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜缩到4秒")
        text = json.dumps(self._clarification(info)["options"],
                          ensure_ascii=False)
        for banned in ("删除", "删掉", "删台词", "截断", "裁剪对白", "delete"):
            assert banned not in text
        assert "对白原样保留" in text

    def test_t07_14_internal_apply_directives_are_not_exposed(self):
        # 选项的 `apply` 是服务端重走解析用的，不是契约的一部分。
        h = Harness(prepared=prepared_with(
            fields={"/duration/target_seconds": 4},
            dialogue_min_duration_ms=5200))
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜缩到4秒")
        for option in self._clarification(info)["options"]:
            assert set(option) == {"option_id", "label", "consequence",
                                   "cost_delta_micro"}

    def test_t07_15_unattended_mode_still_asks(self):
        # 无人值守不是"替我选"。入口根本不读 generation_mode，所以它只可能
        # 更保守，不会更激进。
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1"),
            shot(shot_id="shot_b", episode_id="ep_2"),
        ])
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...,
                                   "generation_mode": "unattended_batch"})
        clarification = self._clarification(info)
        assert clarification["kind"] == "MISSING_EPISODE_CONTEXT"
        assert h.count("change_plan_patch") == 0

    def test_t07_15_no_default_option_is_preselected(self):
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1"),
            shot(shot_id="shot_b", episode_id="ep_2"),
        ])
        with pytest.raises(ServiceError):
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...})
        row = h.rows("clarification_request")[0]
        assert row["selected_option_id"] is None
        assert row["answered_at"] is None

    def test_t07_16_answering_keeps_the_plan_id_and_recomputes_hash(self):
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1",
                 fields={"/camera/angle": "eye_level",
                         "/duration/target_seconds": 6}),
            shot(shot_id="shot_b", episode_id="ep_2"),
        ])
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...})
        plan_id = info.value.details["plan_id"]
        first_hash = h.service.plan_state("t1", plan_id)["plan_hash"]

        result = h.service.answer_clarification(
            "t1", plan_id,
            info.value.details["clarification"]["request_id"],
            {"selected_option_id": "opt_episode_1"})
        assert result["plan_id"] == plan_id
        assert result["state"] == "PLANNED"
        assert result["plan_hash"] != first_hash
        assert h.service.plan_state("t1", plan_id)["state"] == "PLANNED"

    def test_t07_16_answer_records_who_answered(self):
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1"),
            shot(shot_id="shot_b", episode_id="ep_2"),
        ])
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...})
        request_id = info.value.details["clarification"]["request_id"]
        h.service.answer_clarification(
            "t1", info.value.details["plan_id"], request_id,
            {"selected_option_id": "opt_episode_1"})
        row = h.rows("clarification_request", request_id=request_id)[0]
        assert row["selected_option_id"] == "opt_episode_1"
        assert row["answered_at"] is not None
        assert json.loads(row["answered_by"])["principal_id"] == "u_1"

    def test_t07_16_extend_option_yields_the_longer_duration(self):
        # 延到 5.2 秒：新的计划里时长就是 5.2，而不是被悄悄改成整数。
        h = Harness(prepared=prepared_with(
            fields={"/duration/target_seconds": 4},
            dialogue_min_duration_ms=5200))
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜缩到4秒")
        plan_id = info.value.details["plan_id"]
        result = h.service.answer_clarification(
            "t1", plan_id,
            info.value.details["clarification"]["request_id"],
            {"selected_option_id": "opt_extend_to_min"})
        assert result["state"] == "PLANNED"
        duration = next(p for p in result["patches"]
                        if p["path"] == "/duration/target_seconds")
        assert duration["after"] == 5.2

    def test_t07_16_rewrite_dialogue_is_infeasible_not_faked(self):
        # S07 不生成内容：选"重写对白"只能记成不可行，不能假装准备好了。
        h = Harness(prepared=prepared_with(
            fields={"/duration/target_seconds": 4},
            dialogue_min_duration_ms=5200))
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜缩到4秒")
        plan_id = info.value.details["plan_id"]
        result = h.service.answer_clarification(
            "t1", plan_id,
            info.value.details["clarification"]["request_id"],
            {"selected_option_id": "opt_rewrite_dialogue"})
        assert result["state"] == "INFEASIBLE"
        assert result["blocking_issues"]
        assert h.count("change_plan_patch") == 0

    def test_t07_16_answering_twice_is_rejected(self):
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1"),
            shot(shot_id="shot_b", episode_id="ep_2"),
        ])
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...})
        plan_id = info.value.details["plan_id"]
        request_id = info.value.details["clarification"]["request_id"]
        h.service.answer_clarification(
            "t1", plan_id, request_id,
            {"selected_option_id": "opt_episode_1"})
        with pytest.raises(ServiceError) as second:
            h.service.answer_clarification(
                "t1", plan_id, request_id,
                {"selected_option_id": "opt_episode_1"})
        expect(second, 409, "PLAN_NOT_PLANNED")

    def test_t07_16_unknown_option_is_rejected(self):
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1"),
            shot(shot_id="shot_b", episode_id="ep_2"),
        ])
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...})
        with pytest.raises(ServiceError) as bad:
            h.service.answer_clarification(
                "t1", info.value.details["plan_id"],
                info.value.details["clarification"]["request_id"],
                {"selected_option_id": "opt_made_up"})
        expect(bad, 400, "INVALID_CONTEXT")

    def test_t07_17_timeout_expires_the_plan_without_a_default(self):
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1"),
            shot(shot_id="shot_b", episode_id="ep_2"),
        ], ttl_seconds=600)
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...})
        plan_id = info.value.details["plan_id"]
        later = (T0 + timedelta(seconds=700)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        swept = h.service.sweep_expired(now=later)
        assert swept["expired"] == 1
        assert swept["clarifications_abandoned"] == 1
        assert h.service.plan_state("t1", plan_id)["state"] == "EXPIRED"
        row = h.rows("clarification_request", plan_id=plan_id)[0]
        assert row["selected_option_id"] is None
        assert row["abandoned_at"] is not None

    def test_t07_17_expired_clarification_cannot_be_answered(self):
        h = Harness(shots=[
            shot(shot_id="shot_a", episode_id="ep_1"),
            shot(shot_id="shot_b", episode_id="ep_2"),
        ], ttl_seconds=600)
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     context_over={"episode_id": ...})
        plan_id = info.value.details["plan_id"]
        request_id = info.value.details["clarification"]["request_id"]
        later = (T0 + timedelta(seconds=700)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        h.service.sweep_expired(now=later)
        with pytest.raises(ServiceError) as late:
            h.service.answer_clarification(
                "t1", plan_id, request_id,
                {"selected_option_id": "opt_episode_1"})
        expect(late, 409, "PLAN_NOT_PLANNED")

    def test_t07_18_single_target_never_creates_a_clarification(self):
        h = Harness()
        h.create()
        assert h.count("clarification_request") == 0

    def test_t07_18_option_floor_is_in_the_database(self):
        # §2.3：只有一个选项的"澄清"是命令。约束在库里，不在代码里。
        h = Harness()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO clarification_request (tenant_id, plan_id,"
                " request_id, kind, question, options, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                ("t1", "change_x", "clar_x", "MISSING_EPISODE_CONTEXT",
                 "只有一个？", json.dumps([{"option_id": "only"}]),
                 "2026-11-13T09:00:00.000Z"))

    def test_t07_18_kind_enum_is_closed(self):
        h = Harness()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO clarification_request (tenant_id, plan_id,"
                " request_id, kind, question, options, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                ("t1", "change_x", "clar_x", "MADE_UP_KIND", "?",
                 json.dumps([{"option_id": "a"}, {"option_id": "b"}]),
                 "2026-11-13T09:00:00.000Z"))


# ================================================================ E08 跨集

def narrative_harness(**over):
    """改 S04 剧本层字段的 harness：会走到 §4.5 的记忆迁移路径。

    `layer="S04"` 是 `_touches_narrative` 的第二个判据——丢掉它，整条跨集
    路径就成了永远走不到的死代码。
    """
    over.setdefault("shots", [shot(
        fields={"/camera/angle": "eye_level",
                "/script/line_3": "我走了", "/duration/target_seconds": 6},
        layer="S04")])
    return Harness(**over)


class TestE08CrossEpisode:
    """§8.3：T07-20…T07-27。先预览、再报价、绝不先提交。"""

    def test_t07_20_preview_runs_before_any_commit(self):
        h = narrative_harness()
        h.create("把第3场第2镜改成仰拍")
        assert h.calls["preview"] == 1
        assert h.calls["memory"] == 0     # preview 期间没有提交

    def test_t07_20_preview_is_asked_before_the_impact_query(self):
        # "看到影响之前任何事实都不许变"——顺序也是判据。
        order = []

        def _factory(name, original):
            def _wrap(*args, **kwargs):
                order.append(name)
                return original(*args, **kwargs)
            return _wrap

        h = narrative_harness()
        h.service.memory_preview = _factory("preview", h.service.memory_preview)
        h.service.impact_source = _factory("impact", h.service.impact_source)
        h.create("把第3场第2镜改成仰拍")
        assert order.index("preview") < order.index("impact")

    def test_t07_21_memory_migration_flag_is_on_the_plan(self):
        h = narrative_harness()
        plan = h.create("把第3场第2镜改成仰拍")
        assert plan["memory_migration_required"] is True
        row = h.rows("change_plan", plan_id=plan["plan_id"])[0]
        assert row["memory_migration_required"] == 1

    def test_t07_21_impact_summary_carries_cross_episode_information(self):
        h = narrative_harness()
        plan = h.create("把第3场第2镜改成仰拍")
        summary = h.service.get_impact_summary("t1", plan["plan_id"])
        assert summary["memory_migration_required"] is True
        assert summary["memory_disposition"] == "MARK_STALE"
        assert any("记忆" in w for w in summary["warnings"])

    def test_t07_22_delivered_episodes_are_visible_and_marked(self):
        h = narrative_harness(preview={
            "memory_migration_required": True,
            "delivered_episode_ids": ["ep_0", "ep_9"],
        })
        plan = h.create("把第3场第2镜改成仰拍")
        summary = h.service.get_impact_summary("t1", plan["plan_id"])
        assert summary["totals"]["delivered_episodes"] == 2
        assert any(w for w in summary["warnings"] if "ep_0" in w)

    def test_t07_22_delivered_episode_has_a_disposition(self):
        delivered = {"analysis_id": "an_3", "impact": {
            "regenerate_node_ids": ["ep_0.shot_1.video"],
            "recheck_node_ids": [], "reuse_artifact_refs": [],
            "affected_episode_ids": ["ep_0"],
            "superseded_claim_refs": [], "lineage_watermark": 9},
            "nodes": [{"episode_id": "ep_0", "decision": "regenerate",
                       "delivered": True}]}
        h = narrative_harness(impact=delivered)
        plan = h.create("把第3场第2镜改成仰拍")
        summary = h.service.get_impact_summary("t1", plan["plan_id"])
        bucket = next(e for e in summary["by_episode"]
                      if e["episode_id"] == "ep_0")
        assert bucket["delivered"] is True
        assert bucket["disposition"] == "MARK_STALE"

    def test_t07_23_default_disposition_does_not_redo_delivered(self):
        # 默认 MARK_STALE：已交付集不进重做集合，只被标 stale。
        h = narrative_harness()
        plan = h.create("把第3场第2镜改成仰拍")
        query = h.calls["impact_queries"][0]
        assert query["memory_stale_episode_ids"] == ["ep_0"]
        assert query["redo_delivered_episode_ids"] == []
        assert query["memory_disposition"] == "MARK_STALE"

    def test_t07_23_default_disposition_is_persisted(self):
        h = narrative_harness()
        plan = h.create("把第3场第2镜改成仰拍")
        row = h.rows("change_plan", plan_id=plan["plan_id"])[0]
        assert row["memory_disposition"] == "MARK_STALE"

    def test_t07_23_warning_says_no_redo(self):
        delivered = {"analysis_id": "an_4", "impact": {
            "regenerate_node_ids": [], "recheck_node_ids": [],
            "reuse_artifact_refs": [], "affected_episode_ids": ["ep_0"],
            "superseded_claim_refs": [], "lineage_watermark": 9},
            "nodes": [{"episode_id": "ep_0", "decision": "reuse",
                       "delivered": True}]}
        h = narrative_harness(impact=delivered)
        plan = h.create("把第3场第2镜改成仰拍")
        summary = h.service.get_impact_summary("t1", plan["plan_id"])
        assert any("不重做" in w for w in summary["warnings"])

    def test_t07_24_migrate_and_redo_needs_the_budget_gate(self):
        # 选择重做 → 策略看到 disposition 与成本后要求人工闸。生成阶段可以
        # 落计划（用户还没确认），但 apply 时必须停住。
        h = narrative_harness(
            policy={"decision": "require_human",
                    "required_decisions": ["budget_increase"]})
        plan = h.create("把第3场第2镜改成仰拍",
                        memory_disposition="MIGRATE_AND_REDO")
        assert plan["state"] == "PLANNED"
        assert plan["required_policy_decisions"] == ["budget_increase"]
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 409, "POLICY_ABSTAIN")
        assert info.value.retryable is True
        assert h.calls["memory"] == 0
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == "PLANNED"

    def test_t07_24_budget_gate_never_becomes_an_approval(self):
        # "审核已通过"不是放行依据：闸在名单里，apply 就必须停。
        h = narrative_harness(
            policy={"decision": "require_human",
                    "required_decisions": ["budget_increase"],
                    "reason": "cost increased beyond threshold"})
        plan = h.create("把第3场第2镜改成仰拍",
                        memory_disposition="MIGRATE_AND_REDO")
        with pytest.raises(ServiceError):
            h.apply(plan["plan_id"], plan["plan_hash"])
        assert h.calls["commit"] == 0
        assert h.count("outbox") == 0

    def test_t07_24_migrate_and_redo_cost_is_visible_to_policy(self):
        h = narrative_harness(cost={"estimated_cost_upper_micro": 9999999,
                                    "currency": "USD"})
        h.create("把第3场第2镜改成仰拍",
                 memory_disposition="MIGRATE_AND_REDO")
        facts = h.calls["policy_calls"][0]["facts"]
        assert facts["memory_disposition"] == "MIGRATE_AND_REDO"
        assert facts["estimated_cost_upper_micro"] == 9999999
        assert facts["redo_delivered_episode_ids"] == ["ep_0"]

    def test_t07_24_migrate_and_redo_expands_the_redo_set(self):
        h = narrative_harness()
        h.create("把第3场第2镜改成仰拍",
                 memory_disposition="MIGRATE_AND_REDO")
        query = h.calls["impact_queries"][0]
        assert query["redo_delivered_episode_ids"] == ["ep_0"]

    def test_t07_24_unknown_disposition_is_rejected(self):
        h = narrative_harness()
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍",
                     memory_disposition="DELETE_DELIVERED")
        expect(info, 400, "INVALID_CONTEXT")

    def test_t07_25_impact_is_computed_before_apply_is_possible(self):
        # apply 只从 PLANNED 进入，而 PLANNED 必须有 impact。
        h = Harness()
        plan = h.create()
        assert plan["state"] == "PLANNED"
        summary = h.service.get_impact_summary("t1", plan["plan_id"])
        assert summary["analysis_id"] == "an_1"

    def test_t07_26_cross_episode_saga_has_the_memory_step(self):
        h = narrative_harness()
        plan = h.create("把第3场第2镜改成仰拍")
        h.apply(plan["plan_id"], plan["plan_hash"])
        saga = h.service.get_saga("t1", plan["plan_id"])
        names = [s["step_name"] for s in saga["steps"]]
        assert "COMMIT_MEMORY" in names
        step = next(s for s in saga["steps"] if s["step_name"] == "COMMIT_MEMORY")
        assert step["state"] == "DONE"

    def test_t07_26_new_checkpoint_and_old_manifest_are_both_queryable(self):
        h = narrative_harness()
        plan = h.create("把第3场第2镜改成仰拍")
        h.apply(plan["plan_id"], plan["plan_hash"])
        event = h.events("change.plan.applied")[0]
        assert event["data"]["memory_checkpoint_before"]["commit_sequence"] == 3
        assert event["data"]["memory_checkpoint_after"]["commit_sequence"] == 4
        assert event["data"]["base_manifest_ref"]["revision"] == 7
        assert event["data"]["new_manifest_ref"]["revision"] == 8

    def test_t07_26_memory_step_is_skipped_when_not_required(self):
        h = Harness()   # 非 S04 层、非剧情字段
        plan = h.create()
        h.apply(plan["plan_id"], plan["plan_hash"])
        saga = h.service.get_saga("t1", plan["plan_id"])
        step = next(s for s in saga["steps"] if s["step_name"] == "COMMIT_MEMORY")
        assert step["state"] == "SKIPPED"
        assert h.calls["memory"] == 0

    def test_t07_27_cancel_leaves_s16_uncommitted(self):
        # 选"取消"：计划停在未应用、记忆没动，自然过期。
        h = narrative_harness(prepared=prepared_with(
            fields={"/script/line_3": "我走了",
                    "/duration/target_seconds": 4},
            dialogue_min_duration_ms=5200))
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜缩到4秒")
        plan_id = info.value.details["plan_id"]
        result = h.service.answer_clarification(
            "t1", plan_id,
            info.value.details["clarification"]["request_id"],
            {"selected_option_id": "opt_cancel"})
        # §3.1 没有 CANCELED：计划停在 PLANNING（不能 apply、也没过期），
        # 由 sweep_expired 收尾。它既不是 ANSWERED 的伪成功，也不是失败。
        assert result["cancelled"] is True
        assert result["state"] == "PLANNING"
        assert h.calls["memory"] == 0
        assert h.count("change_saga") == 0
        assert h.service.plan_state("t1", plan_id)["state"] == "PLANNING"
        row = h.rows("clarification_request", plan_id=plan_id)[0]
        # 用户答复了 → ANSWERED；ABANDONED 只留给"plan 过期/supersede"。
        assert row["answered_at"] is not None
        assert row["abandoned_at"] is None

    def test_t07_27_cancelled_plan_cannot_be_applied(self):
        h = narrative_harness(prepared=prepared_with(
            fields={"/script/line_3": "我走了",
                    "/duration/target_seconds": 4},
            dialogue_min_duration_ms=5200))
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜缩到4秒")
        plan_id = info.value.details["plan_id"]
        h.service.answer_clarification(
            "t1", plan_id,
            info.value.details["clarification"]["request_id"],
            {"selected_option_id": "opt_cancel"})
        state = h.service.plan_state("t1", plan_id)
        with pytest.raises(ServiceError) as blocked:
            h.apply(plan_id, state["plan_hash"])
        expect(blocked, 409, "PLAN_NOT_PLANNED")

    def test_t07_27_preview_that_does_not_honour_dry_run_is_surfaced(self):
        # S07-29：上游不认 dry_run 时要把它自己的话带给用户。
        h = narrative_harness(preview={
            "memory_migration_required": True,
            "delivered_episode_ids": [],
            "dry_run_honored": False,
            "warning": "S16 不支持 dry_run，本次 preview 仅为估算",
        })
        plan = h.create("把第3场第2镜改成仰拍")
        summary = h.service.get_impact_summary("t1", plan["plan_id"])
        assert any("dry_run" in w for w in summary["warnings"])


# ================================================================ E13 恢复

def _crash_at_commit(h, plan):
    """让 S08 commit 在飞的时候进程"死了"：Saga 步停在 RUNNING，plan 停在
    APPLYING，且**本地没有任何成功的证据**。这就是 E13 的那一行。
    """
    h.service.manifest_committer = _die
    with pytest.raises(RuntimeError, match="process died"):
        h.apply(plan["plan_id"], plan["plan_hash"])
    h.service.manifest_committer = h._commit
    assert h.service.plan_state("t1", plan["plan_id"])["state"] == "APPLYING"
    saga = h.service.get_saga("t1", plan["plan_id"])
    assert saga["saga"]["state"] == "RUNNING"
    step = next(s for s in saga["steps"] if s["step_name"] == "COMMIT_MANIFEST")
    assert step["state"] == "RUNNING"


def _die(*_args, **_kwargs):
    # 不是 ServiceError：进程直接没了，什么都没记。
    raise RuntimeError("process died mid-commit")


class TestE13Recovery:
    """§4.3 / T07-30…T07-32。恢复只能去 S08 查，不能在本地猜。"""

    def test_t07_30_commit_landed_so_recover_marks_applied(self):
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        # S08 的 revision 8 的 payload 里有指向本 plan 的 change_plan_ref。
        h.revision_payload = {
            8: {"manifest": {"revision": 8, "change_plan_ref": {
                "service_id": "S07", "id": plan["plan_id"],
                "revision": 1, "content_hash": plan["plan_hash"]}},
                "content_hash": "e" * 64}}
        result = h.service.recover_apply("t1", plan["plan_id"])
        assert result["action"] == "recovered"
        assert result["state"] == "APPLIED"
        assert result["manifest_revision"] == 8
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == "APPLIED"

    def test_t07_30_recovered_plan_records_the_commit_step_as_done(self):
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        h.revision_payload = {
            8: {"manifest": {"revision": 8, "change_plan_ref": {
                "id": plan["plan_id"]}}}}
        h.service.recover_apply("t1", plan["plan_id"])
        saga = h.service.get_saga("t1", plan["plan_id"])
        step = next(s for s in saga["steps"] if s["step_name"] == "COMMIT_MANIFEST")
        assert step["state"] == "DONE"
        assert saga["saga"]["state"] == "SUCCEEDED"

    def test_t07_30_recovery_reads_each_revision_payload(self):
        # S07-13：列表没有 payload，必须逐条读 detail。
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        before = h.calls["detail"]
        h.revision_payload = {8: {"manifest": {"change_plan_ref": {
            "id": plan["plan_id"]}}}}
        h.service.recover_apply("t1", plan["plan_id"])
        assert h.calls["detail"] > before

    def test_t07_30_a_foreign_change_plan_ref_is_not_mistaken_for_ours(self):
        # 找到了别的 plan 的 revision，不能认成"我提交成功了"。
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        h.revision_payload = {8: {"manifest": {"change_plan_ref": {
            "id": "change_someone_else"}}}}
        result = h.service.recover_apply("t1", plan["plan_id"])
        assert result["action"] == "retry_commit"
        assert result["state"] == "PLANNED"

    def test_t07_31_missing_commit_returns_the_same_command_id(self):
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        h.revisions = {"items": [{"revision": 7}, {"revision": 8}]}
        h.revision_payload = {8: {"manifest": {"revision": 8}}}
        result = h.service.recover_apply("t1", plan["plan_id"])
        assert result["action"] == "retry_commit"
        # 同 command_id：重试不能是"又提交一份"。
        assert result["commit_command_id"] == saga_command_id(
            plan["plan_id"], "COMMIT_MANIFEST")

    def test_t07_31_missing_commit_returns_the_plan_to_planned(self):
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        h.revision_payload = {}
        result = h.service.recover_apply("t1", plan["plan_id"])
        assert result["state"] == "PLANNED"
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == "PLANNED"
        saga = h.service.get_saga("t1", plan["plan_id"])
        assert saga["saga"]["state"] == "ABANDONED"
        assert saga["saga"]["failure_code"] == "COMMIT_NOT_FOUND"

    def test_t07_31_retry_after_recovery_reuses_the_same_saga_step_command(self):
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        h.revision_payload = {}
        h.service.recover_apply("t1", plan["plan_id"])
        h.apply(plan["plan_id"], plan["plan_hash"], command_id="a2")
        bodies = [r["command_id"] for r in h.rows(
            "change_saga_step",
            step_name="COMMIT_MANIFEST")]
        assert len(set(bodies)) == 1     # 两次 Saga 用同一个 commit 键

    def test_t07_32_stale_in_progress_record_is_swept(self):
        h = Harness()
        body = {**h.body(), "command_id": "c_stuck"}
        h.idem.begin("t1", "c_stuck", canonical_request_hash(body))
        assert h.count("idempotency_record") == 1
        # idempotency_sdk 自己写 `isoformat()`（带 `+00:00`，不是 S07 的
        # `...Z`），所以基准按它那套格式给，避免"格式不同 → 比较恒真"。
        result = h.service.sweep_stale_commands(
            60, now=(datetime.now(timezone.utc)
                     + timedelta(hours=1)).isoformat())
        assert result["deleted"] == 1
        assert h.count("idempotency_record") == 0

    def test_t07_32_fresh_in_progress_record_is_not_swept(self):
        h = Harness()
        body = {**h.body(), "command_id": "c_fresh"}
        h.idem.begin("t1", "c_fresh", canonical_request_hash(body))
        h.service.sweep_stale_commands(60)
        assert h.count("idempotency_record") == 1

    def test_t07_32_sweep_does_not_touch_plans(self):
        # 僵死命令可能落了半份计划；删幂等行不是"修计划"。
        h = Harness()
        plan = h.create()
        before = h.service.plan_state("t1", plan["plan_id"])
        patches_before = h.count("change_plan_patch")
        h.service.sweep_stale_commands(
            60, now=(datetime.now(timezone.utc)
                     + timedelta(hours=1)).isoformat())
        after = h.service.plan_state("t1", plan["plan_id"])
        assert after == before
        assert h.count("change_plan_patch") == patches_before >= 1

    def test_recover_on_a_planned_plan_is_a_noop(self):
        h = Harness()
        plan = h.create()
        result = h.service.recover_apply("t1", plan["plan_id"])
        assert result == {"plan_id": plan["plan_id"], "state": "PLANNED",
                          "action": "none"}
        assert h.calls["list"] == 0 and h.calls["detail"] == 0

    def test_recover_never_assumes_success_when_s08_is_unreachable(self):
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        h.revisions = ConnectionError("S08 down")
        with pytest.raises(ServiceError) as info:
            h.service.recover_apply("t1", plan["plan_id"])
        expect(info, 503, "DEPENDENCY_UNAVAILABLE")
        assert info.value.retryable is True
        # 问不到 S08 ≠ 没提交，也 ≠ 提交了：停在 APPLYING，不下结论。
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == "APPLYING"

    def test_recover_is_tenant_scoped(self):
        h = Harness()
        plan = h.create()
        with pytest.raises(ServiceError) as info:
            h.service.recover_apply("t2", plan["plan_id"])
        expect(info, 404, "PLAN_NOT_FOUND")


# ================================================================ 三道闸 / 并发

class TestApplyGates:
    """§4.1 步骤 5–7、§8.4：T07-35…T07-45。每道闸单独证一次。"""

    def test_t07_35_second_apply_on_a_live_saga_is_concurrent_change(self):
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)              # 第一个 apply 还在飞
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"], command_id="a2")
        expect(info, 409, "CONCURRENT_CHANGE")
        assert info.value.details["saga_id"]
        # 被拒的那次没有建第二个 Saga，也没跑新的步骤。
        assert h.count("change_saga") == 1
        assert h.count("change_saga_step") == len(SAGA_STEPS)

    def test_t07_35_concurrent_change_never_reaches_the_committer(self):
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        commits = h.calls["commit"]
        with pytest.raises(ServiceError):
            h.apply(plan["plan_id"], plan["plan_hash"], command_id="a2")
        assert h.calls["commit"] == commits

    def test_concurrent_change_is_checked_before_plan_state(self):
        # 崩溃留下的正是「APPLYING + Saga RUNNING」。若先判状态，会得到
        # 「计划不是 PLANNED」，把"该走 :recover"这个可操作的事实丢掉。
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == \
            "APPLYING"
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"], command_id="a2")
        expect(info, 409, "CONCURRENT_CHANGE")

    def test_t07_36_a_second_plan_at_the_same_revision_supersedes_the_first(self):
        # §3.1 的 PLANNED → SUPERSEDED：同 base/同集/同分支，旧计划在生成时
        # 就让位。对它的 apply 是 PLAN_NOT_PLANNED，不是 MANIFEST_REVISION_
        # CONFLICT（S07-35 登记了这处与 §6.2 散文的差异）。
        h = Harness()
        first = h.create(command_id="c1")
        second = h.create(command_id="c2")
        assert h.service.plan_state("t1", first["plan_id"])["state"] == \
            "SUPERSEDED"
        with pytest.raises(ServiceError) as info:
            h.apply(first["plan_id"], first["plan_hash"])
        expect(info, 409, "PLAN_NOT_PLANNED")
        assert info.value.details["state"] == "SUPERSEDED"
        h.apply(second["plan_id"], second["plan_hash"])
        assert h.service.plan_state("t1", second["plan_id"])["state"] == \
            "APPLIED"

    def test_t07_36_two_episodes_same_revision_the_second_plan_loses(self):
        # §6.2 的「两个 plan 同时打同一 revision」：不同集，所以生成时谁都
        # 没 supersede 谁。第一条提交后 revision 前进，第二条在 CHECK_MANIFEST
        # 里读出来 → 409，且只提交了一次。
        h = Harness(shots=[shot(shot_id="shot_a", episode_id="ep_1"),
                           shot(shot_id="shot_b", episode_id="ep_2")])
        a = h.create(command_id="c1", context_over={"episode_id": "ep_1"})
        b = h.create(command_id="c2", context_over={"episode_id": "ep_2"})
        assert h.service.plan_state("t1", a["plan_id"])["state"] == "PLANNED"
        assert h.service.plan_state("t1", b["plan_id"])["state"] == "PLANNED"
        h.apply(a["plan_id"], a["plan_hash"], command_id="a1")
        h.manifest = {"current_revision": 8, "execution_fence": 11}
        with pytest.raises(ServiceError) as info:
            h.apply(b["plan_id"], b["plan_hash"], command_id="a2")
        expect(info, 409, "MANIFEST_REVISION_CONFLICT")
        assert info.value.details["current_revision"] == 8
        assert h.calls["commit"] == 1
        # 明确的 4xx 不是「不知道」：落 FAILED，不留 APPLYING。
        assert h.service.plan_state("t1", b["plan_id"])["state"] == "FAILED"

    def test_t07_36_expected_revision_must_match_the_plans_base(self):
        h = Harness()
        plan = h.create()
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"], expected=8)
        expect(info, 409, "MANIFEST_REVISION_CONFLICT")
        assert info.value.details == {"expected": 8, "plan_base_revision": 7}
        assert h.count("change_saga") == 0

    def test_t07_36_manifest_moves_during_the_saga(self):
        # 闸 2 用的是计划里记的 base；Saga 第一步再读一次真值。
        h = Harness()
        plan = h.create()
        h.manifest = {"current_revision": 9, "execution_fence": 11}
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 409, "MANIFEST_REVISION_CONFLICT")
        saga = h.service.get_saga("t1", plan["plan_id"])
        step = next(s for s in saga["steps"]
                    if s["step_name"] == "CHECK_MANIFEST")
        assert step["state"] == "FAILED"
        assert step["error_code"] == "MANIFEST_REVISION_CONFLICT"
        assert saga["saga"]["state"] == "FAILED"
        assert h.calls["commit"] == 0
        # 失败的那步之后没有任何步被推进。
        assert saga["steps"][1]["state"] == "PENDING"

    def test_t07_37_cross_tenant_read_is_404(self):
        h = Harness()
        plan = h.create()
        with pytest.raises(ServiceError) as info:
            h.service.get_plan("t2", plan["plan_id"])
        expect(info, 404, "PLAN_NOT_FOUND")
        assert info.value.details == {}

    def test_t07_37_cross_tenant_apply_is_404_and_writes_nothing(self):
        h = Harness()
        plan = h.create()
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"], tenant_id="t2")
        expect(info, 404, "PLAN_NOT_FOUND")
        assert h.count("change_saga") == 0

    def test_t07_37_every_read_path_is_tenant_scoped(self):
        h = Harness()
        plan = h.create()
        for call in (h.service.get_saga, h.service.get_impact_summary,
                     h.service.get_clarifications, h.service.plan_state):
            with pytest.raises(ServiceError) as info:
                call("t2", plan["plan_id"])
            expect(info, 404, "PLAN_NOT_FOUND")

    def test_t07_37_recover_is_tenant_scoped(self):
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        with pytest.raises(ServiceError) as info:
            h.service.recover_apply("t2", plan["plan_id"])
        expect(info, 404, "PLAN_NOT_FOUND")
        assert h.calls["list"] == 0

    def test_t07_37_list_is_scoped_by_tenant(self):
        h = Harness()
        h.create()
        assert len(h.service.list_plans("t1")["items"]) == 1
        assert h.service.list_plans("t2")["items"] == []

    def test_t07_37_subrows_carry_the_tenant_they_belong_to(self):
        # 404 不是靠"查不到行"凑的：子表都按 tenant 建主键，跨租户的同名 id
        # 是另一行，不是同一行的越权视图。
        h = Harness()
        plan = h.create()
        h.apply(plan["plan_id"], plan["plan_hash"])
        assert h.rows("change_plan_patch", plan_id=plan["plan_id"])
        assert all(r["tenant_id"] == "t1" for r in h.rows(
            "change_plan_patch", plan_id=plan["plan_id"]))
        assert all(r["tenant_id"] == "t1" for r in h.rows(
            "change_saga", plan_id=plan["plan_id"]))
        assert h.count("change_saga_step") == len(SAGA_STEPS)
        assert all(r["tenant_id"] == "t1"
                   for r in h.rows("change_saga_step"))

    def test_t07_38_upgrade_needs_no_other_service_at_the_same_version(self):
        # E01：契约是 HTTP + 注入闭包。包里不许出现上游的 Python 模块，
        # 否则独立升级就不是独立升级。
        import change_management
        package_dir = Path(change_management.__file__).resolve().parent
        text = "\n".join(p.read_text(encoding="utf-8")
                         for p in package_dir.glob("*.py"))
        for forbidden in ("import storyboard", "from storyboard",
                          "import lineage", "from lineage",
                          "import episode_manifest", "from episode_manifest",
                          "import governance_policy", "from governance_policy"):
            assert forbidden not in text

    def test_t07_38_schema_has_no_upstream_tables(self):
        h = Harness()
        tables = h.tables()
        for foreign in ("shot", "manifest", "manifest_revision", "node_epoch",
                        "policy_decision", "memory_checkpoint", "balance",
                        "permit", "accepted_pointer"):
            assert foreign not in tables

    def test_t07_39_replaying_every_event_changes_nothing(self):
        h = Harness()
        plan = h.create()
        h.apply(plan["plan_id"], plan["plan_hash"])
        before_plan = h.service.plan_state("t1", plan["plan_id"])
        before_saga = h.service.get_saga("t1", plan["plan_id"])
        before_outbox = h.count("outbox")
        before_local = h.count("local_event")
        events = [
            {"type": "snapshot.manifest.committed", "id": "e1",
             "tenant_id": "t1",
             "data": {"manifest_ref": {"id": "man_1", "revision": 9},
                      "branch_id": "br_1", "series_id": "ser_1"}},
            {"type": "policy.revision.published", "id": "e2",
             "tenant_id": "t1", "data": {"policy_ref": {"revision": 4}}},
            {"type": "memory.canon.committed", "id": "e3", "tenant_id": "t1",
             "data": {"series_id": "ser_1"}},
        ]
        for _ in range(2):                      # 第二轮是重放
            for event in events:
                h.service.consume(event)
        assert h.service.plan_state("t1", plan["plan_id"]) == before_plan
        assert h.service.get_saga("t1", plan["plan_id"]) == before_saga
        assert h.count("outbox") == before_outbox
        assert h.count("local_event") == before_local

    def test_t07_39_duplicate_event_is_recognised_by_inbox(self):
        h = Harness()
        h.create()
        event = {"type": "memory.canon.committed", "id": "e1",
                 "tenant_id": "t1", "data": {"series_id": "ser_1"}}
        first = h.service.consume(event)
        second = h.service.consume(event)
        assert first["consumed"] is True
        assert second == {"consumed": False, "reason": "duplicate"}

    def test_t07_39_unknown_event_is_not_a_retryable_error(self):
        h = Harness()
        assert h.service.consume({"type": "lineage.impact.computed",
                                  "id": "e9", "tenant_id": "t1",
                                  "data": {}}) == {"consumed": False,
                                                   "reason": "unrouted"}

    def test_t07_39_consuming_an_event_writes_no_outbox_row(self):
        h = Harness()
        h.create()
        before = h.count("outbox")
        h.service.consume({"type": "snapshot.manifest.committed", "id": "e1",
                           "tenant_id": "t1",
                           "data": {"manifest_ref": {"id": "man_1",
                                                     "revision": 9}}})
        assert h.count("outbox") == before == 0

    def test_t07_39_manifest_event_supersedes_a_stale_planned_plan(self):
        # 事件不重建计划，只把「基于旧 revision 的 PLANNED」让位。
        h = Harness()
        stale = h.create(command_id="c1")
        result = h.service.consume({
            "type": "snapshot.manifest.committed", "id": "e1",
            "tenant_id": "t1",
            "data": {"manifest_ref": {"id": "man_1", "revision": 9},
                     "branch_id": "br_1", "series_id": "ser_1"}})
        assert result["superseded"] == 1
        assert h.service.plan_state("t1", stale["plan_id"])["state"] == \
            "SUPERSEDED"

    def test_t07_40_plan_is_immutable_across_apply(self):
        h = Harness()
        plan = h.create()
        first_hash = h.rows("change_plan", plan_id=plan["plan_id"])[0][
            "plan_hash"]
        patches = [(r["path"], r["before_value"], r["after_value"])
                   for r in h.rows("change_plan_patch",
                                   plan_id=plan["plan_id"])]
        assert patches
        h.apply(plan["plan_id"], plan["plan_hash"])
        row = h.rows("change_plan", plan_id=plan["plan_id"])[0]
        assert row["plan_hash"] == first_hash == plan["plan_hash"]
        assert [(r["path"], r["before_value"], r["after_value"])
                for r in h.rows("change_plan_patch",
                                plan_id=plan["plan_id"])] == patches
        assert h.service.get_plan("t1", plan["plan_id"])["plan_hash"] == \
            first_hash

    def test_t07_40_get_plan_reads_the_stored_body(self):
        # S07-17：不可变是存储层事实。改库里那份 plan_body 再看，读到的
        # 就该跟着变——证明 get_plan 没有按列现拼一份。
        h = Harness()
        plan = h.create()
        body = json.loads(
            h.rows("change_plan", plan_id=plan["plan_id"])[0]["plan_body"])
        body["instruction"] = "改过的"
        h.conn.execute("UPDATE change_plan SET plan_body=? WHERE tenant_id=?"
                       " AND plan_id=?",
                       (json.dumps(body, ensure_ascii=False, sort_keys=True),
                        "t1", plan["plan_id"]))
        assert h.service.get_plan("t1", plan["plan_id"])["instruction"] == \
            "改过的"

    def test_t07_40_replayed_generation_returns_the_same_hash(self):
        h = Harness()
        first = h.create(command_id="c1")
        h.manifest = {"current_revision": 7, "execution_fence": 11}
        second = h.create(command_id="c1")
        assert second["replayed"] is True
        assert second["plan_hash"] == first["plan_hash"]

    def test_t07_41_old_plan_hash_is_rejected(self):
        h = Harness()
        plan = h.create()
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], "0" * 64)
        expect(info, 409, "PLAN_HASH_MISMATCH")
        assert info.value.details["current_plan_hash"] == plan["plan_hash"]
        assert h.count("change_saga") == 0

    def test_t07_41_another_plans_hash_is_not_accepted(self):
        h = Harness(shots=[shot(shot_id="shot_a", episode_id="ep_1"),
                           shot(shot_id="shot_b", episode_id="ep_2")])
        a = h.create(command_id="c1", context_over={"episode_id": "ep_1"})
        b = h.create(instruction="把第3场第2镜缩到4秒", command_id="c2",
                     context_over={"episode_id": "ep_2"})
        with pytest.raises(ServiceError) as info:
            h.apply(a["plan_id"], b["plan_hash"])
        expect(info, 409, "PLAN_HASH_MISMATCH")

    def test_t07_42_expired_plan_is_rejected_and_marked(self):
        h = Harness()
        plan = h.create()
        h.now = T0 + timedelta(hours=1)
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 409, "PLAN_EXPIRED")
        assert info.value.retryable is True    # §5.7：过期 → 重新生成
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == \
            "EXPIRED"
        assert h.count("change_saga") == 0

    def test_t07_42_sweep_expires_the_plan(self):
        h = Harness()
        plan = h.create()
        h.now = T0 + timedelta(hours=1)
        result = h.service.sweep_expired(now=h.service._now())
        assert result == {"expired": 1, "clarifications_abandoned": 0}
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == \
            "EXPIRED"

    def test_t07_42_sweep_does_not_touch_applying(self):
        # S07-31：Saga 在飞时过期，会让已动的外部事实看起来没发生。
        h = Harness()
        plan = h.create()
        _crash_at_commit(h, plan)
        h.now = T0 + timedelta(hours=1)
        result = h.service.sweep_expired(now=h.service._now())
        assert result["expired"] == 0
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == \
            "APPLYING"

    def test_t07_42_sweep_abandons_unanswered_clarifications(self):
        # T07-17：澄清超时也过期，但绝不自动选默认。
        h = Harness(shots=[shot(shot_id="shot_a", episode_id="ep_1"),
                           shot(shot_id="shot_b", episode_id="ep_2")])
        with pytest.raises(ServiceError) as info:
            h.create(context_over={"episode_id": ...})
        plan_id = info.value.details["plan_id"]
        h.now = T0 + timedelta(hours=1)
        result = h.service.sweep_expired(now=h.service._now())
        assert result["expired"] == 1
        assert result["clarifications_abandoned"] == 1
        item = h.service.get_clarifications("t1", plan_id)["items"][0]
        assert item["abandoned_at"] is not None
        assert item["selected_option_id"] is None

    def test_t07_43_fence_advanced_between_plan_and_saga(self):
        h = Harness()
        plan = h.create()
        h.manifest = {"current_revision": 7, "execution_fence": 12}
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 409, "STALE_EXECUTION_FENCE")
        assert info.value.details == {"current_fence": 12, "plan_fence": 11}
        assert h.calls["commit"] == 0
        assert h.count("outbox") == 0

    def test_t07_43_s07_never_advances_the_fence_it_read(self):
        h = Harness()
        plan = h.create()
        h.apply(plan["plan_id"], plan["plan_hash"])
        row = h.rows("change_plan", plan_id=plan["plan_id"])[0]
        assert row["execution_fence"] == 11     # 生成时读到的值，原样留着
        # 上报给下游的也还是这个值，不是 S08 提交后可能变化的新值。
        event = h.events("change.plan.applied")[0]
        assert event["data"]["execution_fence"] == 11

    def test_t07_43_fence_is_not_a_node_epoch(self):
        # §4.4：两者作用域不同，S07 一个都不写。
        h = Harness()
        plan = h.create()
        h.apply(plan["plan_id"], plan["plan_hash"])
        for table in ("change_plan", "change_saga", "change_saga_step"):
            assert "node_epoch" not in h.column(table, "node_epoch")

    def test_t07_44_tightened_policy_denies_at_apply(self):
        h = Harness()
        plan = h.create()
        h.policy = {"decision": "deny", "reason": "rights revoked",
                    "blocking_issues": ["asset rights withdrawn"]}
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 409, "POLICY_DENIED")
        assert h.calls["policy"] == 2           # 生成一次 + apply 复查一次
        assert h.calls["commit"] == 0
        assert h.count("outbox") == 0

    def test_t07_44_apply_denial_fails_the_plan_and_keeps_the_reason(self):
        # S07-32：§3.1 里 POLICY_DENIED 只能从 PLANNING 进；apply 时 deny 走
        # FAILED，failure_code 保留原因（UI 靠它区分「不允许」与「依赖故障」）。
        h = Harness()
        plan = h.create()
        h.policy = {"decision": "deny", "reason": "rights revoked"}
        with pytest.raises(ServiceError):
            h.apply(plan["plan_id"], plan["plan_hash"])
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == "FAILED"
        saga = h.service.get_saga("t1", plan["plan_id"])["saga"]
        assert saga["state"] == "FAILED"
        assert saga["failure_code"] == "POLICY_DENIED"
        assert saga["failure_detail"]["reason"] == "rights revoked"

    def test_t07_44_recheck_is_given_the_same_facts_as_generation(self):
        # 复查必须看到同样的事实：少喂一个字段，「政策改严了」与「你漏传了」
        # 在策略眼里一模一样——那正是"已审核通过"被当成放行依据的路径。
        h = narrative_harness()
        plan = h.create("把第3场第2镜改成仰拍",
                        memory_disposition="MIGRATE_AND_REDO")
        h.apply(plan["plan_id"], plan["plan_hash"])
        generated = h.calls["policy_calls"][0]["facts"]
        recheck = h.calls["policy_calls"][-1]["facts"]
        for key in ("episode_id", "estimated_cost_upper_micro",
                    "memory_disposition", "redo_delivered_episode_ids"):
            assert key in generated, key
            assert recheck[key] == generated[key], key
        assert recheck["estimated_cost_upper_micro"] == 1200000
        assert recheck["redo_delivered_episode_ids"] == ["ep_0"]

    def test_t07_44_recheck_includes_cost_and_disposition_even_when_stale(self):
        # 成本与处置在 PLANNED 后不可变，所以复查从 plan 行取，不重问 S25：
        # 再问一次可能得到另一个数，那样「同一个策略」会因为问的时机而翻转。
        h = Harness()
        plan = h.create()
        h.cost = {"estimated_cost_upper_micro": 999, "currency": "USD"}
        h.apply(plan["plan_id"], plan["plan_hash"])
        assert h.calls["cost"] == 1             # 只有生成时问过一次
        assert h.calls["policy_calls"][-1]["facts"][
            "estimated_cost_upper_micro"] == 1200000

    def test_t07_45_kill_switch_is_abstain_not_approval(self):
        # "计划已批准"不是放行依据。复查 abstain → 409、可重试、无提交。
        h = Harness()
        plan = h.create()
        h.policy = {"decision": "abstain", "reason": "kill switch is on"}
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 409, "POLICY_ABSTAIN")
        assert info.value.retryable is True
        assert h.calls["commit"] == 0
        assert h.count("outbox") == 0

    def test_t07_45_require_human_is_also_not_an_approval(self):
        # S07-26：S40 没有可注入的建任务接口，所以「要求人工」= 不放行。
        h = Harness()
        plan = h.create()
        h.policy = {"decision": "require_human"}
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 409, "POLICY_ABSTAIN")
        assert info.value.details["decision"] == "require_human"
        assert h.calls["commit"] == 0

    def test_t07_45_abstain_returns_the_plan_to_planned(self):
        h = Harness()
        plan = h.create()
        h.policy = {"decision": "abstain"}
        with pytest.raises(ServiceError):
            h.apply(plan["plan_id"], plan["plan_hash"])
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == \
            "PLANNED"
        assert h.service.get_saga("t1", plan["plan_id"])["saga"]["state"] == \
            "ABANDONED"

    def test_t07_45_a_prior_allow_does_not_release_a_later_deny(self):
        h = Harness()
        plan = h.create()                       # 生成时 allow
        assert h.calls["policy_calls"][0]["facts"]   # 确实算过一遍
        h.policy = {"decision": "deny", "reason": "kill switch"}
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 409, "POLICY_DENIED")
        # 审计留痕，不是静默跳过：被拒的那一步有 error_code。
        saga = h.service.get_saga("t1", plan["plan_id"])
        step = next(s for s in saga["steps"]
                    if s["step_name"] == "RECHECK_POLICY")
        assert step["state"] == "FAILED"
        assert step["error_code"] == "POLICY_DENIED"

    def test_retry_after_abstain_succeeds_once_policy_recovers(self):
        h = Harness()
        plan = h.create()
        h.policy = {"decision": "abstain"}
        with pytest.raises(ServiceError):
            h.apply(plan["plan_id"], plan["plan_hash"], command_id="a1")
        h.policy = {"decision": "allow"}
        h.apply(plan["plan_id"], plan["plan_hash"], command_id="a2")
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == "APPLIED"
        assert h.count("change_saga") == 2
        assert h.count("change_saga_step") == 2 * len(SAGA_STEPS)

    def test_apply_returns_accepted_not_applied(self):
        # §4.1 步骤 10 / C07-7：Operation 没有 applied 态，结果靠 GET 查。
        h = Harness()
        plan = h.create()
        result = h.apply(plan["plan_id"], plan["plan_hash"])
        assert result["state"] == "accepted"
        assert result["operation_id"] == result["saga_id"]
        assert result["plan_id"] == plan["plan_id"]

    def test_missing_expected_revision_is_invalid_context(self):
        h = Harness()
        plan = h.create()
        with pytest.raises(ServiceError) as info:
            h.service.apply("t1", plan["plan_id"], "a1",
                            {"plan_hash": plan["plan_hash"]})
        expect(info, 400, "INVALID_CONTEXT")
        assert h.count("change_saga") == 0

    def test_stale_memory_blocks_apply(self):
        h = Harness()
        plan = h.create()
        h.service.consume({"type": "memory.canon.committed", "id": "e1",
                           "tenant_id": "t1", "data": {"series_id": "ser_1"}})
        assert h.service.plan_state("t1", plan["plan_id"])["memory_stale"]
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 409, "STALE_MEMORY")
        assert h.calls["commit"] == 0

    def test_patch_precondition_failure_stops_before_commit(self):
        h = Harness()
        plan = h.create()
        # S06 的当前值已经不是补丁的 before：基础 revision 被人改过。
        h.docs = {("shot_3_2", 4): {"fields": {"/camera/angle": "high"}}}
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 500, "PATCH_PRECONDITION_FAILED")
        assert h.calls["commit"] == 0
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == "FAILED"

    def test_garbage_collected_prepared_revision_stops_the_saga(self):
        h = Harness()
        plan = h.create()
        assert plan["prepared_revision_refs"]
        h.docs = {("shot_3_2", 5): None}
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 409, "PREPARED_REVISION_GONE")
        assert h.calls["commit"] == 0
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == "FAILED"

    def test_commit_409_fails_the_plan(self):
        # §4.3 第五行：明确的 4xx 不是"不知道"，落 FAILED。码原样透传
        # （S07-36：S08 在它自己的边界上说的话不改写）。
        h = Harness()
        plan = h.create()
        h.committed = ServiceError(409, "REVISION_CONFLICT", "moved")
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 409, "REVISION_CONFLICT")
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == "FAILED"
        saga = h.service.get_saga("t1", plan["plan_id"])["saga"]
        assert saga["state"] == "FAILED"
        assert saga["failure_code"] == "REVISION_CONFLICT"


# ================================================================ 状态机

class TestStateMachine:
    """§3.1 / §3.2。终态不回退，边不指向未知状态。"""

    def test_every_terminal_state_has_no_outgoing_edge(self):
        for state in TERMINAL_PLAN_STATES:
            assert PLAN_TRANSITIONS.get(state, set()) == set(), state

    def test_transitions_only_name_known_states(self):
        for source, targets in PLAN_TRANSITIONS.items():
            assert source in PLAN_STATES, source
            for target in targets:
                assert target in PLAN_STATES, (source, target)

    def test_every_non_applying_state_can_expire(self):
        # §3.1「任意非终态 → EXPIRED」，APPLYING 是唯一的例外（S07-31）。
        for state in set(PLAN_STATES) - TERMINAL_PLAN_STATES - {"APPLYING"}:
            assert "EXPIRED" in PLAN_TRANSITIONS[state], state
        assert "EXPIRED" not in PLAN_TRANSITIONS["APPLYING"]

    def test_no_state_points_at_itself(self):
        for state, targets in PLAN_TRANSITIONS.items():
            assert state not in targets, state

    def test_saga_transitions_are_closed(self):
        assert set(SAGA_TRANSITIONS) == set(SAGA_STATES)
        for source, targets in SAGA_TRANSITIONS.items():
            for target in targets:
                assert target in SAGA_STATES, (source, target)
        for state in ("SUCCEEDED", "FAILED", "ABANDONED"):
            assert SAGA_TRANSITIONS[state] == set()

    def test_set_state_refuses_to_leave_a_terminal_state(self):
        h = Harness()
        plan = h.create()
        h.apply(plan["plan_id"], plan["plan_hash"])
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == "APPLIED"
        with pytest.raises(ServiceError) as info:
            h.service._set_state("t1", plan["plan_id"], "APPLIED", "PLANNING")
        expect(info, 500, "STATE_MACHINE_VIOLATION")
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == "APPLIED"

    def test_expired_cannot_go_back_to_planned(self):
        h = Harness()
        plan = h.create()
        h.now = T0 + timedelta(hours=1)
        with pytest.raises(ServiceError):
            h.apply(plan["plan_id"], plan["plan_hash"])
        with pytest.raises(ServiceError) as info:
            h.service._set_state("t1", plan["plan_id"], "EXPIRED", "PLANNING")
        expect(info, 500, "STATE_MACHINE_VIOLATION")

    def test_applied_plan_cannot_be_applied_twice(self):
        h = Harness()
        plan = h.create()
        h.apply(plan["plan_id"], plan["plan_hash"], command_id="a1")
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"], command_id="a2")
        expect(info, 409, "PLAN_NOT_PLANNED")
        assert h.calls["commit"] == 1

    def test_failed_plan_cannot_be_retried(self):
        # FAILED 是终态：同一份计划再 apply 只会得到同一个答案。
        h = Harness()
        plan = h.create()
        h.committed = ServiceError(409, "REVISION_CONFLICT", "moved")
        with pytest.raises(ServiceError):
            h.apply(plan["plan_id"], plan["plan_hash"], command_id="a1")
        h.committed = {"manifest": {"id": "man_1", "revision": 8,
                                    "content_hash": "e" * 64}}
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"], command_id="a2")
        expect(info, 409, "PLAN_NOT_PLANNED")
        assert h.calls["commit"] == 1

    def test_superseded_plan_cannot_be_applied(self):
        h = Harness()
        first = h.create(command_id="c1")
        h.create(command_id="c2")
        with pytest.raises(ServiceError) as info:
            h.apply(first["plan_id"], first["plan_hash"], command_id="zz")
        expect(info, 409, "PLAN_NOT_PLANNED")
        assert info.value.details["state"] == "SUPERSEDED"

    def test_clarification_is_the_only_business_way_back_to_planning(self):
        # DRAFT → PLANNING 是生成的入口，不是"回退"；业务上只有答复澄清能回去。
        sources = [s for s, t in PLAN_TRANSITIONS.items() if "PLANNING" in t]
        assert sorted(sources) == ["CLARIFICATION_REQUIRED", "DRAFT"]

    def test_applying_to_planned_is_the_only_retry_edge(self):
        # S07-34：这条边只为「COMMIT 之前失败」的 retry 存在。
        assert "PLANNED" in PLAN_TRANSITIONS["APPLYING"]
        assert PLAN_TRANSITIONS["APPLYING"] == {"APPLIED", "FAILED", "PLANNED"}

    def test_cas_failure_is_plan_not_planned(self):
        # 另一个实例已经推进了状态：调用方读到的事实过期，不是"重试就好"。
        h = Harness()
        plan = h.create()
        h.service._set_state("t1", plan["plan_id"], "PLANNED", "SUPERSEDED")
        with pytest.raises(ServiceError) as info:
            h.service._set_state("t1", plan["plan_id"], "PLANNED", "APPLYING")
        expect(info, 409, "PLAN_NOT_PLANNED")


# ================================================================ 红线

class TestRedLines:
    """AGENTS.md 的每一条，能在库里证的就断言库。"""

    def test_no_table_caches_an_epoch_pointer_balance_or_permit(self):
        h = Harness()
        for table in h.tables():
            columns = h.column(table, "x")
            for forbidden in ("node_epoch", "accepted_pointer", "pointer",
                              "permit_id", "balance", "wallet"):
                assert forbidden not in columns, (table, forbidden)

    def test_no_column_holds_a_manifest_revision_s07_wrote(self):
        h = Harness()
        columns = h.column("change_plan", "x")
        # base_revision 是**读到的**基础，不是我们推进到的版本。
        assert "base_revision" in columns
        assert "current_revision" not in columns
        assert "manifest_revision" not in columns

    def test_the_new_revision_lives_only_in_the_saga_audit_row(self):
        h = Harness()
        plan = h.create()
        h.apply(plan["plan_id"], plan["plan_hash"])
        row = h.rows("change_plan", plan_id=plan["plan_id"])[0]
        assert row["base_revision"] == 7        # 计划仍是基于 7 生成的
        step = next(s for s in h.service.get_saga("t1", plan["plan_id"])[
            "steps"] if s["step_name"] == "COMMIT_MANIFEST")
        assert step["response"]["manifest"]["revision"] == 8

    def test_the_database_holds_only_own_tables_and_sdk_tables(self):
        h = Harness()
        assert h.tables() == {
            "change_plan", "change_plan_patch", "change_saga",
            "change_saga_step", "clarification_request", "impact_summary",
            "local_event", "outbox", "inbox", "idempotency_record"}

    def test_tenant_in_the_body_is_compared_not_trusted(self):
        # 签名租户是 t1，context 里写 t2 → 拒绝，而不是按 t2 落库。
        h = Harness()
        with pytest.raises(ServiceError) as info:
            h.create(context_over={"tenant_id": "t2"})
        expect(info, 403, "TENANT_MISMATCH")
        assert h.count("change_plan") == 0

    def test_a_missing_context_tenant_is_not_invented(self):
        h = Harness()
        plan = h.create(context_over={"tenant_id": ...})
        assert h.rows("change_plan", plan_id=plan["plan_id"])[0][
            "tenant_id"] == "t1"

    def test_no_paid_action_happens_before_the_commit_is_recorded(self):
        h = Harness()
        plan = h.create()
        assert h.calls["commit"] == 0
        assert h.calls["memory"] == 0
        h.apply(plan["plan_id"], plan["plan_hash"])
        assert h.calls["commit"] == 1

    def test_memory_commit_only_happens_for_narrative_changes(self):
        # §4.5：不涉及剧情事实的修改没有记忆迁移，如实记 SKIPPED。
        h = Harness()
        plan = h.create()
        result = h.apply(plan["plan_id"], plan["plan_hash"])
        assert result["state"] == "accepted"
        assert h.calls["memory"] == 0
        step = next(s for s in h.service.get_saga("t1", plan["plan_id"])[
            "steps"] if s["step_name"] == "COMMIT_MEMORY")
        assert step["state"] == "SKIPPED"

    def test_event_is_emitted_only_after_the_commit(self):
        h = Harness()
        plan = h.create()
        h.committed = ServiceError(409, "REVISION_CONFLICT", "moved")
        with pytest.raises(ServiceError):
            h.apply(plan["plan_id"], plan["plan_hash"])
        assert h.count("outbox") == 0
        assert h.events("change.plan.applied") == []

    def test_a_rejected_apply_emits_no_applied_event(self):
        h = Harness()
        plan = h.create()
        h.policy = {"decision": "deny"}
        with pytest.raises(ServiceError):
            h.apply(plan["plan_id"], plan["plan_hash"])
        assert h.events("change.plan.applied") == []
        assert h.count("local_event") == 1      # 只有生成时那一条 planned

    def test_every_saga_step_command_id_is_derived_from_the_step_name(self):
        # S07-19：按步骤名派生，崩溃恢复不因步数变化而换键。
        h = Harness()
        plan = h.create()
        h.apply(plan["plan_id"], plan["plan_hash"])
        steps = h.service.get_saga("t1", plan["plan_id"])["steps"]
        for step in steps:
            if step["target_service_id"] is None:
                continue
            assert step["command_id"] == saga_command_id(
                plan["plan_id"], step["step_name"])
        ids = {s["command_id"] for s in steps if s["command_id"]}
        assert len(ids) == len([s for s in steps if s["command_id"]])


# ================================================================ 上游不可达

class TestUpstreamOutage:
    """§5.7 / S07-34：503 retryable，绝不降级成默认值。"""

    def test_s06_resolver_down_is_503(self):
        h = Harness(shots=ConnectionError("S06 down"))
        with pytest.raises(ServiceError) as info:
            h.create()
        expect(info, 503, "DEPENDENCY_UNAVAILABLE")
        assert info.value.retryable is True

    def test_s06_prepare_down_is_503(self):
        h = Harness(prepared=ConnectionError("S06 down"))
        with pytest.raises(ServiceError) as info:
            h.create()
        expect(info, 503, "DEPENDENCY_UNAVAILABLE")

    def test_s09_down_is_503(self):
        h = Harness(impact=ConnectionError("S09 down"))
        with pytest.raises(ServiceError) as info:
            h.create()
        expect(info, 503, "DEPENDENCY_UNAVAILABLE")

    def test_s02_down_is_503(self):
        h = Harness(policy=ConnectionError("S02 down"))
        with pytest.raises(ServiceError) as info:
            h.create()
        expect(info, 503, "DEPENDENCY_UNAVAILABLE")

    def test_s16_preview_down_is_503(self):
        h = narrative_harness(preview=ConnectionError("S16 down"))
        with pytest.raises(ServiceError) as info:
            h.create("把第3场第2镜改成仰拍")
        expect(info, 503, "DEPENDENCY_UNAVAILABLE")
        assert h.calls["memory"] == 0       # preview 失败时绝不提交

    def test_s08_down_at_saga_start_is_503(self):
        h = Harness()
        plan = h.create()
        h.manifest = ConnectionError("S08 down")
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 503, "DEPENDENCY_UNAVAILABLE")
        # 可重试 ≠ 可以继续：计划回到 PLANNED，且没有任何提交。
        assert h.service.plan_state("t1", plan["plan_id"])["state"] == \
            "PLANNED"
        assert h.calls["commit"] == 0

    def test_s06_down_during_verify_is_503(self):
        h = Harness()
        plan = h.create()
        h.docs = ConnectionError("S06 down")
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 503, "DEPENDENCY_UNAVAILABLE")
        assert h.calls["commit"] == 0

    def test_upstream_500_becomes_503_not_500(self):
        # 上游 500 是上游的故障，不是 S07 的 500：把它当 500 会让调用方
        # 以为计划逻辑坏了，从而不再重试。
        h = Harness(impact=ServiceError(500, "INTERNAL", "boom"))
        with pytest.raises(ServiceError) as info:
            h.create()
        expect(info, 503, "DEPENDENCY_UNAVAILABLE")
        assert info.value.retryable is True

    def test_upstream_4xx_is_not_swallowed(self):
        # 吞掉一个 409 才是真事故：Saga 会把「明确的失败」记成「成功」。
        h = Harness(impact=ServiceError(409, "STALE_FENCE", "gone"))
        with pytest.raises(ServiceError) as info:
            h.create()
        expect(info, 409, "STALE_FENCE")
        assert info.value.retryable is False

    def test_missing_cost_upper_is_a_semantic_503_not_zero(self):
        # S25 答了但没给上界：不给计划，也不拿 0 顶替。
        h = Harness(cost={"currency": "USD"})
        with pytest.raises(ServiceError) as info:
            h.create()
        expect(info, 503, "COST_SOURCE_UNAVAILABLE")
        assert h.count("change_plan_patch") == 0

    def test_unusable_policy_decision_is_not_an_allow(self):
        h = Harness(policy={"decision": "maybe"})
        with pytest.raises(ServiceError) as info:
            h.create()
        expect(info, 503, "POLICY_UNAVAILABLE")
        assert h.count("change_plan_patch") == 0

    def test_unusable_policy_decision_at_apply_is_not_an_allow(self):
        h = Harness()
        plan = h.create()
        h.policy = {"decision": "maybe"}
        with pytest.raises(ServiceError) as info:
            h.apply(plan["plan_id"], plan["plan_hash"])
        expect(info, 503, "POLICY_UNAVAILABLE")
        assert info.value.retryable is True
        assert h.calls["commit"] == 0

    def test_every_builder_without_a_base_url_refuses(self):
        builders = sorted(n for n in dir(deps) if n.startswith("build_"))
        assert len(builders) == 13
        for name in builders:
            closure = getattr(deps, name)(None)
            with pytest.raises(ServiceError) as info:
                closure("t1")
            expect(info, 503, "DEPENDENCY_UNAVAILABLE")
            assert "is not configured" in str(info.value)

    def test_an_unconfigured_kernel_stays_out_of_planned(self):
        # 没有配置任何上游时不许"生成"一份计划：可以留下 DRAFT/PLANNING 的
        # 审计行，但绝不能到 PLANNED，也不能有补丁。
        conn = sqlite3.connect(":memory:", check_same_thread=False,
                              isolation_level=None)
        conn.row_factory = sqlite3.Row
        svc = ChangeManagementService(conn)
        with pytest.raises(ServiceError) as info:
            svc.create_plan("t1", "c1", Harness().body())
        expect(info, 503, "DEPENDENCY_UNAVAILABLE")
        assert conn.execute(
            "SELECT COUNT(*) FROM change_plan WHERE state='PLANNED'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM change_plan_patch").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM local_event").fetchone()[0] == 0


# ================================================================ HTTP

# `enterprise-change-plan.v2` 的 16 个必填键，照契约抄一份。`api/app.py` 不能
# 在 import 时读（它一 import 就建库），所以用一个独立常量对拍。
CONTRACT_PLAN_KEYS = (
    "schema_version", "plan_id", "plan_hash", "context", "base_manifest_ref",
    "instruction", "prepared_revision_refs", "patches", "derived_adjustments",
    "impact", "memory_migration_required", "required_policy_decisions",
    "blocking_issues", "estimated_cost_upper_micro", "currency", "expires_at",
)


def _http_client(monkeypatch, tmp_path, **over):
    """`api/app.py` 的 TestClient，上游换成 Harness 的 stub。

    - `S07_DB` 必须指向独立文件库（import 那一刻就建连接与表）；
    - 路由函数在定义时捕获模块全局，所以 `module.service` / `module._idem`
      必须重绑，且重绑时用 `module._conn`，否则断言查的是另一个库。
    """
    monkeypatch.setenv("S07_DB", str(tmp_path / "s07.db"))
    path = ROOT / "api" / "app.py"
    # 不能 `import app`：别的服务也有 `api/app.py` 且可能先进 sys.modules。
    spec = importlib.util.spec_from_file_location("s07_api_app", path)
    module = importlib.util.module_from_spec(spec)
    loader = spec.loader
    assert loader is not None
    loader.exec_module(module)
    sys.modules["s07_api_app"] = module
    h = Harness(conn=module._conn, **over)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h


def _post(client, body, *, command_id="c1", tenant="t1"):
    return client.post("/change-plans", json=body,
                       headers={"Idempotency-Key": command_id,
                                "X-Tenant-Id": tenant})


class TestHttpLayer:
    def test_create_returns_a_bare_plan_with_state_in_a_header(
            self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = _post(client, h.body())
        assert r.status_code == 200, r.text
        assert set(r.json()) == set(CONTRACT_PLAN_KEYS)   # additionalProperties:false
        assert r.headers["x-plan-state"] == "PLANNED"
        assert "x-idempotent-replay" not in r.headers

    def test_plan_keys_match_the_contract_projection(
            self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        module = sys.modules["s07_api_app"]
        assert tuple(module.PLAN_KEYS) == CONTRACT_PLAN_KEYS

    def test_replay_sets_the_replay_header(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = h.body()
        first = _post(client, body)
        second = _post(client, body)
        assert second.status_code == 200
        assert second.headers["x-idempotent-replay"] == "true"
        assert second.json()["plan_id"] == first.json()["plan_id"]
        assert h.count("change_plan") == 1

    def test_same_key_different_body_is_409(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        r = _post(client, h.body(instruction="把第3场第2镜缩到4秒"))
        assert r.status_code == 409
        assert r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert r.json()["retryable"] is False

    def test_missing_idempotency_key_is_rejected(self, monkeypatch, tmp_path):
        # FastAPI 的必填 Header 由 pydantic 先判：缺头是 422，不是本服务的
        # 400 INVALID_BODY（_body 只在头存在但为空时才有机会说话）。
        client, h = _http_client(monkeypatch, tmp_path)
        r = client.post("/change-plans", json=h.body(),
                        headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 422
        assert h.count("change_plan") == 0

    def test_missing_tenant_is_rejected(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = client.post("/change-plans", json=h.body(),
                        headers={"Idempotency-Key": "c1"})
        assert r.status_code == 422
        assert h.count("change_plan") == 0

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = h.body()
        body["tenant_id"] = "t2"
        r = _post(client, body)
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_MISMATCH"

    def test_header_and_body_command_id_must_agree(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        body = h.body()
        body["command_id"] = "other"
        r = _post(client, body, command_id="c1")
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_BODY"

    def test_non_object_body_is_rejected(self, monkeypatch, tmp_path):
        # `payload: dict` 由 FastAPI 校验，非对象进不到 `_body()`。
        client, h = _http_client(monkeypatch, tmp_path)
        r = client.post("/change-plans", json=[],
                        headers={"Idempotency-Key": "c1",
                                 "X-Tenant-Id": "t1"})
        assert r.status_code == 422
        assert h.count("change_plan") == 0

    def test_apply_is_202_and_carries_the_saga_id(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        r = client.post(f"/change-plans/{created['plan_id']}:apply",
                        json={"plan_hash": created["plan_hash"],
                              "expected_manifest_revision": 7},
                        headers={"Idempotency-Key": "a1",
                                 "X-Tenant-Id": "t1"})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["state"] == "accepted"
        assert body["operation_id"] == body["saga_id"]
        assert body["replayed"] is False

    def test_get_roundtrip_reports_applied(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        client.post(f"/change-plans/{created['plan_id']}:apply",
                    json={"plan_hash": created["plan_hash"],
                          "expected_manifest_revision": 7},
                    headers={"Idempotency-Key": "a1", "X-Tenant-Id": "t1"})
        got = client.get(f"/change-plans/{created['plan_id']}",
                         headers={"X-Tenant-Id": "t1"})
        assert got.status_code == 200
        assert got.headers["x-plan-state"] == "APPLIED"
        assert set(got.json()) == set(CONTRACT_PLAN_KEYS)

    def test_clarification_is_409_with_details(self, monkeypatch, tmp_path):
        client, h = _http_client(
            monkeypatch, tmp_path,
            shots=[shot(shot_id="shot_a", episode_id="ep_1"),
                   shot(shot_id="shot_b", episode_id="ep_2")])
        r = _post(client, h.body(context_over={"episode_id": ...}))
        assert r.status_code == 409
        payload = r.json()
        assert payload["code"] == "CLARIFICATION_REQUIRED"
        clar = payload["details"]["clarification"]
        assert clar["kind"] == "MISSING_EPISODE_CONTEXT"
        assert len(clar["options"]) >= 2
        assert h.service.plan_state("t1", payload["details"]["plan_id"])[
            "state"] == "CLARIFICATION_REQUIRED"

    def test_cross_tenant_get_is_404(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        r = client.get(f"/change-plans/{created['plan_id']}",
                       headers={"X-Tenant-Id": "t2"})
        assert r.status_code == 404
        assert r.json()["code"] == "PLAN_NOT_FOUND"
        # 不确认存在性：错误体里没有属于 t1 的事实。
        assert h.service.plan_state("t1", created["plan_id"])["state"] == \
            "PLANNED"

    def test_saga_and_summary_endpoints(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        client.post(f"/change-plans/{created['plan_id']}:apply",
                    json={"plan_hash": created["plan_hash"],
                          "expected_manifest_revision": 7},
                    headers={"Idempotency-Key": "a1", "X-Tenant-Id": "t1"})
        saga = client.get(f"/change-plans/{created['plan_id']}/saga",
                          headers={"X-Tenant-Id": "t1"})
        assert saga.status_code == 200
        assert saga.json()["saga"]["state"] == "SUCCEEDED"
        assert [s["step_name"] for s in saga.json()["steps"]] == [
            name for name, _ in SAGA_STEPS]
        impact = client.get(
            f"/change-plans/{created['plan_id']}/impact-summary",
            headers={"X-Tenant-Id": "t1"})
        assert impact.status_code == 200
        assert impact.json()["analysis_id"] == "an_1"
        clar = client.get(f"/change-plans/{created['plan_id']}/clarifications",
                          headers={"X-Tenant-Id": "t1"})
        assert clar.status_code == 200
        assert clar.json()["items"] == []

    def test_list_filters_by_state(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        planned = client.get("/change-plans?state=PLANNED",
                             headers={"X-Tenant-Id": "t1"})
        assert planned.status_code == 200
        assert [i["plan_id"] for i in planned.json()["items"]] == [
            created["plan_id"]]
        assert client.get("/change-plans?state=APPLIED",
                          headers={"X-Tenant-Id": "t1"}).json()["items"] == []

    def test_consume_event_does_not_need_an_idempotency_key(
            self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, h.body())
        payload = {"type": "memory.canon.committed", "id": "e1",
                   "data": {"series_id": "ser_1"}}
        r = client.post("/events:consume", json=payload,
                        headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert r.json()["consumed"] is True
        again = client.post("/events:consume", json=payload,
                            headers={"X-Tenant-Id": "t1"})
        assert again.json() == {"consumed": False, "reason": "duplicate"}

    def test_consume_event_with_foreign_tenant_is_403(self, monkeypatch,
                                                     tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/events:consume",
                        json={"type": "memory.canon.committed", "id": "e1",
                              "tenant_id": "t2", "data": {}},
                        headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_MISMATCH"

    def test_readiness_reports_contract_gaps(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/health/readiness")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        ids = [g["id"] for g in body["contract_gaps"]]
        assert len(ids) == len(CONTRACT_GAPS)
        assert len(ids) == len(set(ids))        # 编号不重复
        for expected in ("C07-1", "C07-10", "S07-11", "S07-37"):
            assert expected in ids, expected

    def test_upstream_outage_is_503_over_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path,
                                 impact=ConnectionError("S09 down"))
        r = _post(client, h.body())
        assert r.status_code == 503
        assert r.json()["code"] == "DEPENDENCY_UNAVAILABLE"
        assert r.json()["retryable"] is True

    def test_plan_hash_mismatch_is_409_over_http(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        r = client.post(f"/change-plans/{created['plan_id']}:apply",
                        json={"plan_hash": "0" * 64,
                              "expected_manifest_revision": 7},
                        headers={"Idempotency-Key": "a1",
                                 "X-Tenant-Id": "t1"})
        assert r.status_code == 409
        assert r.json()["code"] == "PLAN_HASH_MISMATCH"

    def test_recover_route_roundtrip(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        created = _post(client, h.body()).json()
        # 没在飞：recover 只报状态，不去问上游。
        r = client.post(f"/change-plans/{created['plan_id']}:recover",
                        headers={"X-Tenant-Id": "t1"})
        assert r.status_code == 200
        assert r.json()["action"] == "none"
        assert h.calls["list"] == 0
