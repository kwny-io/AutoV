"""S48 单测。编号对应 `plans/S48-adaptation-training.md` §9.1–§9.8 的
T48-01…T48-50，以及 §9 末尾那张 P0 覆盖表里的 E 编号。

上游全部是 stub，**按各服务自己的契约形状写**：

- S28 的制品：`artifact_id` + `content_hash`（本服务只读这两项）。
- S24 的基座：`{found, compatible, reason}`——`compatible` 缺省即"没回答"，
  测试**不**把它当成 True（T48-23 的形态）。
- S22 的 permit：`{granted, permit_id}`。
- S23 的预算：`{valid}`。
- S11–S15 的授权：`{state, grant_kind, content_hash, ...}`。

结构性断言优先查库（"库里查不到"），因为本服务最严重的错误是变成第二个
权威——响应体少个字段证明不了什么，少一列、少一张表才能。

**没有任何一次真实模型调用、GPU 调用或网络请求。** 训练是状态机 +
记录；runner 的所有推进都通过内核方法（`prepare` / `start` / `finish` /
`evaluate`）由测试直接驱动。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import (CommandConflict, SqliteIdempotencyStore,
                             canonical_request_hash)

from adaptation_training import (CONTRACT_GAPS, SCHEMA,
                                 AdaptationTrainingService, ServiceError,
                                 evaluate_metrics, holdout_items_hash,
                                 items_hash, stable_request_hash)
from adaptation_training.schema import ID_PREFIX

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "contracts" / "adaptation.training-completed.v2.schema.json"

H = lambda char: char * 64  # noqa: E731
HA, HB, HC, HD, HE, HF = (H(c) for c in "abcdef")


# ------------------------------------------------------------------ 上游形状

def rights(state="active", grant_kind="generation,training", content_hash=HA,
           rights_id="rt_1", revision=3):
    return {"service_id": "S11", "id": rights_id, "revision": revision,
            "content_hash": content_hash, "state": state,
            "grant_kind": grant_kind}


def dataset_body(**over):
    body = {
        "adaptation_type": "character_lora",
        "target_ref": {"service_id": "S11", "id": "char_linlan",
                       "revision": 4, "content_hash": HC},
        "dataset": {"mode": "explicit", "artifact_refs": [
            {"id": "art_1", "content_hash": HA},
            {"id": "art_2", "content_hash": HB},
        ]},
        "base_model": {"id": "mdl_x", "revision": 7, "content_hash": HD},
        "hyperparams": {"profile": "lora-standard", "steps": 1200,
                        "lora_rank": 32},
        "evaluation": {"rubric_ref": {"service_id": "S44", "id": "rub_1"},
                       "holdout_refs": [{"id": "hold_1",
                                         "content_hash": HE}]},
        "budget_authorization_ref": {"service_id": "S23", "id": "ba_1"},
    }
    body.update(over)
    return body


# ------------------------------------------------------------------ harness

class Harness:
    def __init__(self, conn=None, **over):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)
        self.calls = {"rights": 0, "base": 0, "budget": 0, "permit": 0,
                      "artifact": 0, "registry": 0}
        # 默认：两项素材都合法、基座兼容、预算有效、permit 已给。
        self.rights_state = over.get("rights_state", {
            "art_1": rights(content_hash=HA, rights_id="rt_1"),
            "art_2": rights(content_hash=HB, rights_id="rt_2"),
        })
        self.base = over.get("base", {"found": True, "compatible": True})
        self.budget = over.get("budget", {"valid": True})
        self.permit = over.get("permit", {"granted": True, "permit_id": "pmt_1"})
        self.derived = over.get("derived", [
            {"artifact_id": "art_1", "content_hash": HA},
            {"artifact_id": "art_2", "content_hash": HB},
        ])
        self.artifacts = over.get("artifacts", {
            "art_1": {"artifact_id": "art_1", "content_hash": HA},
            "art_2": {"artifact_id": "art_2", "content_hash": HB},
        })
        self.registry = over.get("registry", {"artifact_id": "art_s28_1"})
        self.service = AdaptationTrainingService(
            self.conn,
            rights_source=self._rights,
            base_model_source=self._base,
            budget_source=self._budget,
            permit_source=self._permit,
            artifact_source=self._artifact,
            registry_sink=None if over.get("no_registry") else self._registry,
            training_enabled=over.get("training_enabled", True),
            tenant_active_cap=over.get("tenant_active_cap", 2),
            max_items=over.get("max_items", 5000),
            min_ratio=over.get("min_ratio", 0.9),
        )

    # ---- 五个上游 ----
    def _value(self, table, key, counter):
        self.calls[counter] += 1
        value = table.get(key)
        if isinstance(value, Exception):
            raise value
        return value

    def _rights(self, tenant_id, service_id, artifact_id):
        return self._value(self.rights_state, artifact_id, "rights")

    def _base(self, tenant_id, base_id, revision, content_hash=None):
        self.calls["base"] += 1
        value = self.base
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(base_id, revision, content_hash)
        return value

    def _budget(self, tenant_id, ref):
        self.calls["budget"] += 1
        value = self.budget
        if isinstance(value, Exception):
            raise value
        return value

    def _permit(self, tenant_id, modality, resource_class, job_id, budget):
        self.calls["permit"] += 1
        value = self.permit
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(job_id)
        return value

    def _artifact(self, tenant_id, mode, source):
        self.calls["artifact"] += 1
        value = self.derived if mode != "artifact" else self.artifacts.get(source)
        if isinstance(value, Exception):
            raise value
        return value

    def _registry(self, tenant_id, weights_hash, blob, size, meta):
        self.calls["registry"] += 1
        value = self.registry
        if isinstance(value, Exception):
            raise value
        return value

    # ---- 幂等外壳：与 api/app.py 的 _command 同语义 ----
    def command(self, fn, body, tenant_id="t1", command_id="c1"):
        payload = {**body, "command_id": command_id}
        digest = canonical_request_hash(payload)
        record = self.idem.begin(tenant_id, command_id, digest)
        if record.result is not None:
            return {**json.loads(record.result), "replayed": True}
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
        body = dataset_body(**over)
        return self.command(
            lambda t, c, b: self.service.create_job(
                t, b, c, canonical_request_hash({**b, "command_id": c}),
                requested_by={"actor": "editor"}, home_region="cn-east"),
            body, tenant_id, command_id)

    def run_to_candidate(self, tenant_id="t1", **over):
        """create → prepare → start → finish → evaluate(达标)。"""
        job = self.create(tenant_id=tenant_id, **over)
        self.service.prepare(tenant_id, job["job_id"])
        self.service.start(tenant_id, job["job_id"])
        self.service.finish(tenant_id, job["job_id"], outcome="trained",
                            weights_blob_ref="s3://s48/weights/1",
                            weights_hash=HF, size_bytes=1024, gpu_seconds=600)
        return self.service.evaluate(
            tenant_id, job["job_id"],
            metrics={"identity_preservation": 0.92, "overfit_score": 0.10,
                     "holdout_loss": 0.30})

    def run_to_published(self, tenant_id="t1", **over):
        job = self.run_to_candidate(tenant_id=tenant_id, **over)
        return self.service.accept(tenant_id, job["job_id"])

    # ---- 读库 ----
    def count(self, table, where="", params=()):
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return self.conn.execute(sql, params).fetchone()[0]

    def row(self, table, where, params=()):
        got = self.conn.execute(f"SELECT * FROM {table} WHERE {where}",
                                params).fetchone()
        return None if got is None else dict(got)

    def events(self, kind="adaptation.training.completed"):
        out = []
        for payload in self.service.outbox.unpublished(1000):
            if kind in ("*", payload["type"]):
                out.append(payload)
        return out


@pytest.fixture()
def h():
    return Harness()


def code_of(exc_info) -> str:
    return exc_info.value.code


# ================================================================== 9.1 授权链

def test_t48_01_revoked_item_excluded_and_shortfall_rejects(h):
    """T48-01 [E30]：撤销项被排除；剩余低于 min_ratio → REJECTED。"""
    h.rights_state["art_2"] = rights(state="revoked", **{"content_hash": HB})
    job = h.create()
    assert job["state"] == "REJECTED"
    assert job["rejection"]["reason"] == "EMPTY_DATASET_AFTER_FILTER"
    # 1/2 = 0.5 < 0.9
    assert job["rejection"]["details"]["remaining"] == 1
    # 排除项写进了库，不是只在响应里
    manifest = h.row("dataset_manifest", "tenant_id='t1'")
    assert manifest is None or manifest["state"] == "FROZEN"
    # 未被冻结的作业不应有 dataset_item
    assert h.count("dataset_item") == 0


def test_t48_01b_revoked_item_alone_is_excluded_from_frozen_manifest(h):
    """撤销项不入册，剩余项不受影响（10 项里 1 项撤销 = 90%，刚好通过）。"""
    refs = [{"id": f"art_{n}", "content_hash": H("a")} for n in range(1, 11)]
    for n in range(1, 11):
        h.rights_state[f"art_{n}"] = rights(content_hash=HA)
    h.rights_state["art_5"] = rights(state="revoked", content_hash=HA)
    job = h.create(dataset={"mode": "explicit", "artifact_refs": refs})
    assert job["state"] == "QUEUED"
    assert job["dataset_ref"]["item_count"] == 9
    assert job["dataset_ref"]["excluded_count"] == 1
    dataset = h.service.get_dataset("t1", job["dataset_ref"]["id"])
    assert [x["artifact_id"] for x in dataset["exclusions"]] == ["art_5"]
    assert dataset["exclusions"][0]["reason"] == "REVOKED"


def test_t48_02_suspended_is_technical_not_legal(h):
    """T48-02 [E30]：suspended 技术下架 ≠ 撤销。**不排除**。"""
    h.rights_state["art_1"] = rights(state="suspended", content_hash=HA)
    job = h.create()
    assert job["state"] == "QUEUED"
    assert job["dataset_ref"]["item_count"] == 2
    assert job["dataset_ref"]["excluded_count"] == 0
    dataset = h.service.get_dataset("t1", job["dataset_ref"]["id"])
    assert dataset["exclusions"] == []


def test_t48_03_generation_only_grant_is_excluded(h):
    """T48-03 [E30]：授权只有 generation → NO_TRAINING_GRANT（C48-3）。"""
    h.rights_state["art_1"] = rights(grant_kind="generation", content_hash=HA)
    h.rights_state["art_2"] = rights(grant_kind=["generation", "display"],
                                     content_hash=HB)
    job = h.create()
    assert job["state"] == "REJECTED"
    assert job["rejection"]["reason"] == "EMPTY_DATASET_AFTER_FILTER"
    assert job["rejection"]["details"]["remaining"] == 0


def test_t48_03b_missing_grant_kind_denies_by_default(h):
    """C48-3：`grant_kind` 缺失/None → 默认拒绝，不是"没说不行就算行"。"""
    h.rights_state["art_1"] = rights(grant_kind=None, content_hash=HA)
    h.rights_state["art_2"] = rights(grant_kind={"generation": True},
                                     content_hash=HB)
    job = h.create()
    assert job["state"] == "REJECTED"


def test_t48_04_rights_authority_unreachable_rejects_whole_job(h):
    """T48-04 [E30]：权威不可达 → 整单 REJECTED（fail closed），不是跳过。"""
    h.rights_state["art_2"] = ConnectionError("rights gateway down")
    job = h.create()
    assert job["state"] == "REJECTED"
    assert job["rejection"]["reason"] == "RIGHTS_UNVERIFIABLE"
    # 没有"用剩下的 1 项凑合" —— 一项都没入册
    assert h.count("dataset_item") == 0


def test_t48_04b_rights_5xx_also_rejects_rather_than_thinning(h):
    """权威 503 与连接失败同义：证明它说不出来 ≠ 证明它说不。"""
    h.rights_state["art_1"] = ServiceError(503, "DEPENDENCY_UNAVAILABLE", "x")
    job = h.create()
    assert job["state"] == "REJECTED"
    assert job["rejection"]["reason"] == "RIGHTS_UNVERIFIABLE"


def test_t48_05_revocation_after_publish_blocks_artifact(h):
    """T48-05 [E30]：发布后撤销 → artifact BLOCKED（§3.8）。"""
    job = h.run_to_published()
    artifact_id = job["artifact_ref"]["id"]
    h.service.on_rights_revoked("t1", {
        "event": {"id": "evt_1", "type": "rights.revoked", "tenant_id": "t1",
                  "data": {"rights_id": "rt_1", "state": "revoked"}}})
    artifact = h.service.get_artifact("t1", artifact_id)
    assert artifact["state"] == "BLOCKED"
    assert artifact["__hints"]["usable_for_generation"] is False
    # job 也跟着 BLOCKED（同一事实的两个视角）
    assert h.service.get_job("t1", job["job_id"])["state"] == "BLOCKED"


def test_t48_06_blocked_artifact_cannot_be_revived(h):
    """T48-06 [E30]：BLOCKED 不能复活——重新授权也不改（§3.8）。"""
    job = h.run_to_published()
    artifact_id = job["artifact_ref"]["id"]
    h.service.on_rights_revoked("t1", {
        "event": {"id": "evt_1", "type": "rights.revoked", "tenant_id": "t1",
                  "data": {"rights_id": "rt_1", "state": "revoked"}}})
    # 重新授权：权威现在说 active
    h.service.on_rights_revoked("t1", {
        "event": {"id": "evt_2", "type": "rights.restored", "tenant_id": "t1",
                  "data": {"rights_id": "rt_1", "state": "active"}}})
    assert h.service.get_artifact("t1", artifact_id)["state"] == "BLOCKED"
    # 也不能被 :accept 拉回 PUBLISHED
    with pytest.raises(ServiceError) as info:
        h.service.accept("t1", job["job_id"])
    assert code_of(info) == "JOB_STATE_CONFLICT"


def test_t48_07_revocation_during_training_does_not_interrupt(h):
    """T48-07 [E30]：TRAINING 中撤销 → 不中断，完成后 BLOCKED（§6.2）。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    held = h.service.on_rights_revoked("t1", {
        "event": {"id": "evt_1", "type": "rights.revoked", "tenant_id": "t1",
                  "data": {"rights_id": "rt_1", "state": "revoked"}}})
    assert held["held"] == [job["job_id"]]
    # 训练没被打断
    assert h.service.get_job("t1", job["job_id"])["state"] == "TRAINING"
    h.service.finish("t1", job["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    assert h.service.get_job("t1", job["job_id"])["state"] == "EVALUATING"
    result = h.service.evaluate(
        "t1", job["job_id"],
        metrics={"identity_preservation": 0.95, "overfit_score": 0.05,
                 "holdout_loss": 0.1})
    assert result["state"] == "BLOCKED"
    # 产物不能是 CANDIDATE/PUBLISHED
    assert result["rejection"]["reason"] == "RIGHTS_REVOKED_DURING_TRAINING"


def test_t48_08_dataset_manifest_answers_which_rights(h):
    """T48-08 [E39]：逐项 rights_ref 可查，含复核时刻与状态。"""
    job = h.create()
    dataset = h.service.get_dataset("t1", job["dataset_ref"]["id"])
    items = h.service.get_dataset_items("t1", job["dataset_ref"]["id"])
    assert items["next_cursor"] is None
    first = items["items"][0]
    assert first["rights_ref"]["service_id"] == "S11"
    assert first["rights_ref"]["id"] == "rt_1"
    assert first["rights_ref"]["revision"] == 3
    assert first["rights_ref"]["grant_kind"] == "generation,training"
    assert first["rights_ref"]["state"] == "active"
    assert first["rights_ref"]["checked_at"]
    assert dataset["items_hash"] == job["dataset_ref"]["items_hash"]


def test_t48_09_audit_chain_is_local_and_complete(h):
    """T48-09 [E39]：create/freeze/start/complete 全链可查（C48-16）。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.finish("t1", job["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    chain = h.service.list_audit("t1", job["job_id"])
    actions = [entry["action"] for entry in chain["items"]]
    for expected in ("training.job.create", "training.dataset.freeze",
                     "training.job.training", "training.job.start",
                     "training.job.complete"):
        assert expected in actions, (expected, actions)
    # **不替代 S45**：它是本地表，不是跨服务上送
    assert any(gap["id"] == "C48-16" for gap in CONTRACT_GAPS)


def test_t48_10_no_provider_credentials_in_schema(h):
    """T48-10 [E30]：本服务不存 BYOK/供应商凭据。"""
    tables = {r[0] for r in h.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    columns = set()
    for table in tables:
        for col in h.conn.execute(f"PRAGMA table_info({table})").fetchall():
            columns.add(col[1].lower())
    for forbidden in ("api_key", "access_key", "secret", "credential",
                      "byok", "vendor_token", "provider_key"):
        assert not any(forbidden in name for name in columns), forbidden


# ================================================================== 9.2 数据集

def test_t48_11_explicit_ten_items_all_active(h):
    """T48-11：`explicit` 10 项全 active → 10 行 `dataset_item`，hash 稳定。"""
    refs = [{"id": f"art_{n}", "content_hash": H("a")} for n in range(1, 11)]
    for n in range(1, 11):
        h.rights_state[f"art_{n}"] = rights(content_hash=HA)
    job = h.create(dataset={"mode": "explicit", "artifact_refs": refs})
    assert job["state"] == "QUEUED"
    assert h.count("dataset_item", "dataset_id=?", (job["dataset_ref"]["id"],)) == 10


def test_t48_12_items_hash_is_deterministic(h):
    """T48-12：同输入两次解析 → 同一 items_hash；顺序无关。"""
    a = [{"artifact_id": "art_1", "content_hash": HA},
         {"artifact_id": "art_2", "content_hash": HB}]
    b = list(reversed(a))
    assert items_hash(a) == items_hash(b)
    # 不同内容必然不同
    assert items_hash(a) != items_hash(a[:1])
    # 只取 (artifact_id, content_hash) 两个字段：多余的键（ordinal、
    # 中间解析出来的 service_id/rights_ref）不该改变摘要。这项断言防的是
    # "往投影里塞了 ordinal，于是同一批数据换个顺序就变成另一批"。
    noisy = [{"artifact_id": "art_1", "content_hash": HA, "ordinal": 9,
              "service_id": "S28", "rights_ref": {"id": "rt_1"}},
             {"artifact_id": "art_2", "content_hash": HB, "ordinal": 3,
              "service_id": "S28", "rights_ref": {"id": "rt_2"}}]
    assert items_hash(noisy) == items_hash(a)
    # 用真实的库值再对一次
    job = h.create()
    assert job["dataset_ref"]["items_hash"] == items_hash(a)


def test_t48_13_one_of_ten_excluded_passes(h):
    """T48-13：1/10 被排除 = 0.9，**边界通过**。"""
    refs = [{"id": f"art_{n}", "content_hash": H("a")} for n in range(1, 11)]
    for n in range(1, 11):
        h.rights_state[f"art_{n}"] = rights(content_hash=HA)
    h.rights_state["art_3"] = rights(state="revoked", content_hash=HA)
    job = h.create(dataset={"mode": "explicit", "artifact_refs": refs})
    assert job["state"] == "QUEUED"


def test_t48_14_two_of_ten_excluded_rejects(h):
    """T48-14：2/10 = 0.8 < 0.9 → REJECTED。"""
    refs = [{"id": f"art_{n}", "content_hash": H("a")} for n in range(1, 11)]
    for n in range(1, 11):
        h.rights_state[f"art_{n}"] = rights(content_hash=HA)
    for n in (3, 7):
        h.rights_state[f"art_{n}"] = rights(state="revoked", content_hash=HA)
    job = h.create(dataset={"mode": "explicit", "artifact_refs": refs})
    assert job["state"] == "REJECTED"
    assert job["rejection"]["details"]["remaining"] == 8
    assert job["rejection"]["details"]["original"] == 10


def test_t48_15_derived_mode_returns_full_resolution(h):
    """T48-15：derived_from_run 返回完整 resolved_items 供确认（§2.2）。"""
    job = h.create(dataset={"mode": "derived_from_run",
                            "source_ref": {"run_id": "run_1"}})
    assert job["state"] in ("QUEUED", "ADMITTED")
    resolved = job["resolved_items"]
    assert {item["artifact_ref"]["id"] for item in resolved} == {"art_1", "art_2"}
    # 推导来源留在 manifest 里
    dataset = h.service.get_dataset("t1", job["dataset_ref"]["id"])
    assert dataset["mode"] == "derived_from_run"
    assert dataset["source_ref"]["run_id"] == "run_1"


def test_t48_15b_derived_union_exclude_semantics(h):
    """C48-18：并集 − 排除项，显式项与推导项一起去重。"""
    job = h.create(dataset={
        "mode": "derived_from_artifact_set",
        "source_ref": {"set_id": "set_1"},
        "artifact_refs": [{"id": "art_1", "content_hash": HA},
                          {"id": "art_9", "content_hash": HC}],
        "exclude_artifact_refs": [{"id": "art_9"}]})
    h.rights_state["art_9"] = rights(content_hash=HC)
    job = h.create(command_id="c2", dataset={
        "mode": "derived_from_artifact_set",
        "source_ref": {"set_id": "set_1"},
        "artifact_refs": [{"id": "art_1", "content_hash": HA},
                          {"id": "art_9", "content_hash": HC}],
        "exclude_artifact_refs": [{"id": "art_9"}]})
    assert job["state"] in ("QUEUED", "ADMITTED")
    ids = {item["artifact_ref"]["id"] for item in job["resolved_items"]}
    assert ids == {"art_1", "art_2"}  # art_9 被显式排除


def test_t48_16_holdout_overlap_rejects(h):
    """T48-16：holdout 与训练集有交集 → REJECTED HOLDOUT_OVERLAP。"""
    job = h.create(evaluation={
        "rubric_ref": {"service_id": "S44", "id": "rub_1"},
        "holdout_refs": [{"id": "art_1", "content_hash": HA}]})
    assert job["state"] == "REJECTED"
    assert job["rejection"]["reason"] == "HOLDOUT_OVERLAP"
    assert job["rejection"]["details"]["overlapping_ids"] == ["art_1"]


def test_t48_17_hash_changed_after_review_rejects(h):
    """T48-17：复核后字节变了 → PREPARING 时 REJECTED HASH_MISMATCH。"""
    job = h.create()
    h.artifacts["art_2"] = {"artifact_id": "art_2", "content_hash": H("9")}
    result = h.service.prepare("t1", job["job_id"])
    assert result["state"] == "REJECTED"
    assert result["rejection"]["reason"] == "HASH_MISMATCH"


def test_t48_18_dataset_item_direct_insert_without_rights_rejected(h):
    """T48-18：直插无 rights 的 dataset_item 被数据库拒绝（NOT NULL）。"""
    job = h.create()
    dataset_id = job["dataset_ref"]["id"]
    with pytest.raises(sqlite3.IntegrityError):
        h.conn.execute(
            "INSERT INTO dataset_item (tenant_id, dataset_id, artifact_id,"
            " content_hash, rights_service, rights_id, rights_revision,"
            " rights_content_hash, ordinal) VALUES"
            " ('t1', ?, 'art_x', ?, 'S11', 'rt_x', 1, ?, 0)",
            (dataset_id, HA, HA))
    # grant_kind 与 state 都是 NOT NULL，单插也进不来
    with pytest.raises(sqlite3.IntegrityError):
        h.conn.execute(
            "INSERT INTO dataset_item (tenant_id, dataset_id, artifact_id,"
            " content_hash, rights_service, rights_id, rights_revision,"
            " rights_content_hash, rights_grant_kind, ordinal) VALUES"
            " ('t1', ?, 'art_x', ?, 'S11', 'rt_x', 1, ?, 'training', 0)",
            (dataset_id, HA, HA))


# ================================================================== 9.3 基座

def test_t48_19_base_revision_and_hash_recorded(h):
    """T48-19：训练记录同时保存 revision 与 content_hash（§2.1）。"""
    job = h.create()
    row = h.row("training_job", "job_id=?", (job["job_id"],))
    assert row["base_revision"] == 7
    assert row["base_content_hash"] == HD
    assert job["base_model"]["revision"] == 7
    assert job["base_model"]["content_hash"] == HD


def test_t48_20_missing_revision_is_400_not_latest(h):
    """T48-20：不指定 revision（"最新"）→ 400，绝不取默认。"""
    body = dataset_body()
    del body["base_model"]["revision"]
    with pytest.raises(ServiceError) as info:
        h.service.create_job("t1", body, "c1", "h" * 64)
    assert info.value.status == 400
    assert code_of(info) == "MALFORMED_REQUEST"
    assert "latest" in str(info.value)


def test_t48_20b_missing_content_hash_is_400(h):
    body = dataset_body()
    del body["base_model"]["content_hash"]
    with pytest.raises(ServiceError) as info:
        h.service.create_job("t1", body, "c1", "h" * 64)
    assert info.value.status == 400


def test_t48_21_incompatible_base_rejected_before_gpu(h):
    """T48-21：S24 说不兼容 → 训练前 REJECTED（省 GPU）。"""
    h.base = {"found": True, "compatible": False, "reason": "ARC_MISMATCH"}
    job = h.create()
    assert job["state"] == "REJECTED"
    assert job["rejection"]["reason"] == "BASE_MODEL_INCOMPATIBLE"
    assert h.count("dataset_item") == 0  # 没花钱也没冻数据


def test_t48_21b_unknown_base_is_not_treated_as_compatible(h):
    """T48-23 的前半：`compatible` 缺省 = 没回答，不是 True。"""
    h.base = {"found": True}
    job = h.create()
    assert job["state"] in ("QUEUED", "ADMITTED")
    # 兼容性记录（accept 时写）会落 UNKNOWN，而不是 COMPATIBLE
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.finish("t1", job["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    h.service.evaluate("t1", job["job_id"],
                       metrics={"identity_preservation": 0.95,
                                "overfit_score": 0.05, "holdout_loss": 0.1})
    published = h.service.accept("t1", job["job_id"])
    record = h.row("base_model_compatibility", "artifact_id=?",
                   (published["artifact_ref"]["id"],))
    assert record["verdict"] == "UNKNOWN"


def test_t48_22_base_retired_during_training_blocks_after(h):
    """T48-22：基座训练中退役 → 完成后 BLOCKED，verdict=INCOMPATIBLE。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.on_base_retired("t1", {
        "event": {"id": "evt_b1", "type": "model.retired", "tenant_id": "t1",
                  "data": {"base_id": "mdl_x", "revision": 7,
                           "reason": "EOL"}}})
    h.service.finish("t1", job["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    h.service.evaluate("t1", job["job_id"],
                       metrics={"identity_preservation": 0.95,
                                "overfit_score": 0.05, "holdout_loss": 0.1})
    result = h.service.accept("t1", job["job_id"])
    assert result["state"] == "BLOCKED"
    record = h.row("base_model_compatibility", "base_id='mdl_x'")
    assert record["verdict"] == "INCOMPATIBLE"
    assert record["reason"] == "BASE_RETIRED"


def test_t48_23_compatibility_record_is_not_current(h):
    """T48-23：兼容性记录被当作"当前"读时必须带权威提示（§1.5）。"""
    job = h.run_to_published()
    artifact_id = job["artifact_ref"]["id"]
    compat = h.service.get_compatibility("t1", artifact_id)
    assert compat["__hints"]["authority"]["current_compatibility"] == "S24"
    assert "stale" in compat["__hints"]["authority"]["note"]
    # 产物查询也有同样的提示
    artifact = h.service.get_artifact("t1", artifact_id)
    assert artifact["__hints"]["authority"]["current_compatibility"] == "S24"
    assert artifact["__hints"]["snapshot_not_verdict"] is True


# ================================================================== 9.4 准入

def test_t48_24_budget_ok_permit_missing_stays_admitted(h):
    """T48-24 [E14]：S23 成功、S22 无 permit → 保持 ADMITTED，不占 GPU。"""
    h.permit = {"granted": False}
    job = h.create()
    assert job["state"] == "ADMITTED"
    assert job["dataset_ref"]["state"] == "FROZEN"  # 数据已冻结
    # 没有 permit 不能 start
    with pytest.raises(ServiceError) as info:
        h.service.start("t1", job["job_id"])
    assert code_of(info) == "JOB_STATE_CONFLICT"  # 还在 ADMITTED
    # 库里有 permit 引用吗？没有
    row = h.row("training_job", "job_id=?", (job["job_id"],))
    assert row["quota_permit_ref"] is None


def test_t48_24b_admit_retry_gets_permit_later(h):
    """T48-24 续：permit 稍后释放，重试准入 → QUEUED（不重新复核数据）。"""
    h.permit = {"granted": False}
    job = h.create()
    assert job["state"] == "ADMITTED"
    h.permit = {"granted": True, "permit_id": "pmt_2"}
    admitted = h.service.admit("t1", job["job_id"])
    assert admitted["state"] == "QUEUED"


def test_t48_24c_invalid_budget_rejects(h):
    """S23 说不有效 → REJECTED（不是停在 ADMITTED）。"""
    h.budget = {"valid": False}
    job = h.create()
    assert job["state"] == "REJECTED"
    assert job["rejection"]["reason"] == "BUDGET_AUTH_INVALID"


def test_t48_25_permit_marked_budget_timeout_does_not_start(h):
    """T48-25 [E15]：permit 有效但 S23 复查超时 → 不开始训练。"""
    job = h.create()
    assert job["state"] == "QUEUED"
    h.service.prepare("t1", job["job_id"])
    # 开始前再验 permit：上游超时
    h.permit = ConnectionError("S22 timeout")
    with pytest.raises(ServiceError) as info:
        h.service.start("t1", job["job_id"])
    assert info.value.status == 503
    assert info.value.retryable is True
    assert h.service.get_job("t1", job["job_id"])["state"] == "PREPARING"


def test_t48_25b_permit_revoked_at_start_returns_to_admitted(h):
    """§7.2：`granted:false` 不是异常，是"配额被收走" → 退回 ADMITTED。

    与 T48-25 区别：那条是 S22 **说不出来**（503，停在 PREPARING 等重试）；
    这条是 S22 **明确说不再给你了**（409，退回等 permit，不是失败）。
    """
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.permit = {"granted": False, "reason": "quota_reclaimed"}
    with pytest.raises(ServiceError) as info:
        h.service.start("t1", job["job_id"])
    assert info.value.status == 409
    assert code_of(info) == "PERMIT_INVALID"
    assert info.value.retryable is True
    row = h.row("training_job", "job_id=?", (job["job_id"],))
    assert row["state"] == "ADMITTED"
    assert row["quota_permit_ref"] is None


def test_t48_26_training_pool_does_not_borrow_inference(h):
    """T48-26 [E35]：训练不借用推理池——计数器恒 0（§8.1）。"""
    h.run_to_candidate()
    metrics = h.service.readiness()
    assert metrics["inference_pool_borrow_total"] == 0
    assert h.service.readiness()["metrics"].get(
        "s48_inference_pool_borrow_total", 0) == 0


def test_t48_27_per_tenant_cap_does_not_starve_others(h):
    """T48-27 [E35]：单租户 20 个 job 被上限挡住，别的租户照常。"""
    for n in range(2):
        h.create(command_id=f"c{n}")
    with pytest.raises(ServiceError) as info:
        h.create(command_id="c_over")
    assert info.value.status == 429
    assert code_of(info) == "TRAINING_QUEUE_FULL"
    assert info.value.details["cap"] == 2
    # 另一个租户不受影响
    other = h.create(tenant_id="t2", command_id="c_other")
    assert other["state"] in ("QUEUED", "ADMITTED")


def test_t48_28_admitted_over_24h_expires(h):
    """T48-28：ADMITTED 超 24h → EXPIRED，释放预占。"""
    h.permit = {"granted": False}
    job = h.create()
    h.conn.execute(
        "UPDATE training_job SET admitted_at='2020-01-01T00:00:00.000Z'"
        " WHERE job_id=?", (job["job_id"],))
    swept = h.service.expire_stale_admissions("t1")
    assert swept["expired"] == [job["job_id"]]
    expired = h.service.get_job("t1", job["job_id"])
    assert expired["state"] == "EXPIRED"
    assert expired["rejection"]["reason"] == "ADMISSION_TIMEOUT"
    # 终态：不能再去准入
    with pytest.raises(ServiceError):
        h.service.admit("t1", job["job_id"])


def test_t48_29_inference_pool_borrow_counter_is_zero(h):
    """T48-29：`s48_inference_pool_borrow_total` 恒为 0（物理隔离的证明）。"""
    assert h.service.readiness()["inference_pool_borrow_total"] == 0
    h.run_to_candidate()
    assert h.service.readiness()["inference_pool_borrow_total"] == 0
    # 这个键甚至不该被写过
    assert "s48_inference_pool_borrow_total" not in h.service.metrics


# ================================================================== 9.5 训练

def test_t48_30_heartbeat_timeout_resumes_from_checkpoint(h):
    """T48-30 [E13]：训练中 kill → 心跳超时 → RESUMING。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.progress("t1", job["job_id"], gpu_seconds=60,
                       checkpoint_ref="ckpt_step_100")
    h.conn.execute(
        "UPDATE training_job SET last_heartbeat_at='2020-01-01T00:00:00.000Z'"
        " WHERE job_id=?", (job["job_id"],))
    swept = h.service.sweep_stalled("t1")
    assert swept["resumed"] == [job["job_id"]]
    resumed = h.service.get_job("t1", job["job_id"])
    assert resumed["state"] == "RESUMING"
    # checkpoint 还在，能被接着用
    row = h.row("training_job", "job_id=?", (job["job_id"],))
    assert row["checkpoint_ref"] == "ckpt_step_100"
    assert row["resume_count"] == 1


def test_t48_30b_progress_outside_training_is_rejected(h):
    """§6.2：TRAINING 之外不接受进度/心跳。"""
    job = h.create()
    with pytest.raises(ServiceError) as info:
        h.service.progress("t1", job["job_id"], gpu_seconds=10)
    assert code_of(info) == "JOB_STATE_CONFLICT"


def test_t48_31_checkpoint_failure_fails_without_silent_resume(h):
    """T48-31 [E13]：checkpoint 写失败 → FAILED，不静默续（§7.3）。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    result = h.service.finish("t1", job["job_id"], outcome="failed",
                              reason="CHECKPOINT_WRITE_FAILED",
                              checkpoint_ref="ckpt_step_50")
    assert result["state"] == "FAILED"
    assert result["rejection"]["reason"] == "CHECKPOINT_WRITE_FAILED"
    row = h.row("training_job", "job_id=?", (job["job_id"],))
    # 最后的 checkpoint 保留下来，便于人工排查
    assert row["checkpoint_ref"] == "ckpt_step_50"
    # 失败后没有任何 RESUMING
    assert h.count("training_job", "state='RESUMING'") == 0


def test_t48_32_two_runners_cas_resume(h):
    """T48-32 [E13]：两个 runner 同时续同一 checkpoint → 只有一个成功。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.progress("t1", job["job_id"], gpu_seconds=30,
                       checkpoint_ref="ckpt_10")
    first = h.service.resume("t1", job["job_id"], checkpoint_ref="ckpt_10",
                             expected_checkpoint="ckpt_10")
    assert first["state"] == "RESUMING"
    with pytest.raises(ServiceError) as info:
        h.service.resume("t1", job["job_id"], checkpoint_ref="ckpt_10",
                         expected_checkpoint="ckpt_10")
    # 第二次进来时状态已经是 RESUMING，CAS 让其中一个赢
    assert code_of(info) == "JOB_STATE_CONFLICT"


def test_t48_32b_stale_checkpoint_expectation_conflicts(h):
    """另一个 runner 已经推进了 checkpoint → 旧期望值必须 409。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.progress("t1", job["job_id"], gpu_seconds=30,
                       checkpoint_ref="ckpt_20")
    with pytest.raises(ServiceError) as info:
        h.service.resume("t1", job["job_id"], checkpoint_ref="ckpt_20",
                         expected_checkpoint="ckpt_10")
    assert info.value.details["expected"] == "ckpt_10"


def test_t48_33_all_runners_down_then_resume_not_restart(h):
    """T48-33 [E33]：全部停机后恢复 → 从 checkpoint 续，不重头。

    §6.4 的路径是：janitor 发现心跳停 → 置 `RESUMING`（**不自动续**），
    队列挑一个 runner → runner 调 `start_resumed`。`resume()` 只在 runner
    主动声明"我看到的 checkpoint 是 X"时用，两者不要混。
    """
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.progress("t1", job["job_id"], gpu_seconds=120,
                       checkpoint_ref="ckpt_300")
    h.conn.execute(
        "UPDATE training_job SET last_heartbeat_at='2020-01-01T00:00:00.000Z'"
        " WHERE job_id=?", (job["job_id"],))
    swept = h.service.sweep_stalled("t1")
    assert swept["resumed"] == [job["job_id"]]
    assert h.service.get_job("t1", job["job_id"])["state"] == "RESUMING"
    started = h.service.start_resumed("t1", job["job_id"])
    assert started["state"] == "TRAINING"
    row = h.row("training_job", "job_id=?", (job["job_id"],))
    assert row["resume_count"] == 1
    # 之前的 GPU 秒没被重置（不是重头训）
    assert row["gpu_seconds_consumed"] == 120
    assert row["checkpoint_ref"] == "ckpt_300"


def test_t48_34_timeout_fails_and_keeps_checkpoint(h):
    """T48-34：超时 → FAILED，保留最后 checkpoint。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.progress("t1", job["job_id"], gpu_seconds=10,
                       checkpoint_ref="ckpt_last")
    result = h.service.finish("t1", job["job_id"], outcome="failed",
                              reason="TRAINING_TIMEOUT")
    assert result["state"] == "FAILED"
    assert h.row("training_job", "job_id=?", (job["job_id"],))[
        "checkpoint_ref"] == "ckpt_last"


# ================================================================== 9.6 评测

def test_t48_35_metrics_pass_candidate(h):
    """T48-35：指标达标 → `CANDIDATE`（不是直接 PUBLISHED）。"""
    job = h.run_to_candidate()
    assert job["state"] == "CANDIDATE"
    assert job["evaluation"]["passed"] is True
    assert job["evaluation"]["holdout_items_hash"] == holdout_items_hash(
        [{"id": "hold_1", "content_hash": HE}])


def test_t48_36_metrics_fail_is_eval_failed_not_failed(h):
    """T48-36：不达标 → EVAL_FAILED，**不是** FAILED（§3.1）。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.finish("t1", job["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    result = h.service.evaluate(
        "t1", job["job_id"],
        metrics={"identity_preservation": 0.61, "overfit_score": 0.48,
                 "holdout_loss": 0.60})
    assert result["state"] == "EVAL_FAILED"
    assert result["evaluation"]["passed"] is False
    assert "identity_preservation" in result["evaluation"]["failed_metrics"]
    # 事件里的 artifact_ref 必须是 null
    event = [e for e in h.events() if e["data"]["state"] == "EVAL_FAILED"][0]
    assert event["data"]["artifact_ref"] is None
    assert event["data"]["evaluation_passed"] is False


def test_t48_36b_missing_metric_is_a_failure_not_a_pass(h):
    """C48-21：缺指标 ≠ 通过。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.finish("t1", job["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    result = h.service.evaluate("t1", job["job_id"],
                                metrics={"identity_preservation": 0.99})
    assert result["state"] == "EVAL_FAILED"
    assert set(result["evaluation"]["failed_metrics"]) >= {
        "overfit_score", "holdout_loss"}


def test_t48_36c_voice_clone_needs_voice_metrics(h):
    """§3.6：voice_clone 多两项指标，缺了就不达标。"""
    passed, failures = evaluate_metrics("voice_clone", {
        "identity_preservation": 0.9, "overfit_score": 0.1,
        "holdout_loss": 0.2})
    assert passed is False
    assert set(failures) == {"voice_similarity", "intelligibility"}
    passed, failures = evaluate_metrics("voice_clone", {
        "identity_preservation": 0.9, "overfit_score": 0.1,
        "holdout_loss": 0.2, "voice_similarity": 0.88,
        "intelligibility": 0.95})
    assert passed is True and failures == []


def test_t48_37_eval_infrastructure_retry_once_then_failed(h):
    """T48-37：评测基础设施失败重试 1 次；再失败 → FAILED（§6.5）。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.finish("t1", job["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    first = h.service.evaluate("t1", job["job_id"], metrics={},
                               infrastructure_failure=True)
    assert first["state"] == "EVALUATING"  # 还在等重试
    second = h.service.evaluate("t1", job["job_id"], metrics={},
                                infrastructure_failure=True)
    assert second["state"] == "FAILED"
    assert second["rejection"]["reason"] == "EVALUATION_INFRASTRUCTURE_FAILURE"


def test_t48_38_human_reject_is_eval_failed_with_reason(h):
    """T48-38：人工否决 → EVAL_FAILED，理由落库。"""
    job = h.run_to_candidate()
    result = h.service.reject_candidate(
        "t1", job["job_id"], reason={"message": "脸不像"})
    assert result["state"] == "EVAL_FAILED"
    assert result["rejection"]["reason"] == "HUMAN_REJECTED"
    assert result["rejection"]["message"] == "脸不像"
    # 候选 artifact 不再可用
    assert h.row("lora_artifact", "artifact_id=?",
                 (job["artifact_ref"]["id"],))["state"] == "SUPERSEDED"


def test_t48_39_registry_failure_keeps_candidate(h):
    """T48-39：接受时 S28 注册失败 → 保持 CANDIDATE，不置 PUBLISHED。"""
    h.registry = ConnectionError("S28 down")
    job = h.run_to_candidate()
    assert job["state"] == "CANDIDATE"
    with pytest.raises(ServiceError) as info:
        h.service.accept("t1", job["job_id"])
    assert info.value.status == 503
    assert h.service.get_job("t1", job["job_id"])["state"] == "CANDIDATE"
    assert h.row("lora_artifact", "artifact_id=?",
                 (job["artifact_ref"]["id"],))["state"] == "CANDIDATE"


def test_t48_39b_no_registry_configured_refuses_to_publish(h):
    """C48-15：没有注册通道时绝不伪造 published。"""
    h.service.registry_sink = None
    job = h.run_to_candidate()
    with pytest.raises(ServiceError) as info:
        h.service.accept("t1", job["job_id"])
    assert info.value.status == 503
    assert "registry" in str(info.value)


def test_t48_40_same_weights_hash_reuses_artifact(h):
    """T48-40：同权重 hash 二次注册 → 复用同一 artifact_id（§5.3）。"""
    first = h.create(command_id="c1")
    h.service.prepare("t1", first["job_id"])
    h.service.start("t1", first["job_id"])
    h.service.finish("t1", first["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w1", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    first_artifact = h.row("training_job", "job_id=?",
                           (first["job_id"],))["artifact_id"]

    second = h.create(command_id="c2")
    h.service.prepare("t1", second["job_id"])
    h.service.start("t1", second["job_id"])
    h.service.finish("t1", second["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w2", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    second_artifact = h.row("training_job", "job_id=?",
                            (second["job_id"],))["artifact_id"]
    assert first_artifact == second_artifact
    # 库里只有一行权重
    assert h.count("lora_artifact") == 1
    assert h.service.metrics["s48_artifact_reuse_total"] == 1


def test_t48_40b_weights_hash_unique_is_per_tenant(h):
    """E28/E31：跨租户绝不共享权重（`la_weights_uniq` 带 tenant_id）。"""
    first = h.create(tenant_id="t1", command_id="c1")
    h.service.prepare("t1", first["job_id"])
    h.service.start("t1", first["job_id"])
    h.service.finish("t1", first["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    second = h.create(tenant_id="t2", command_id="c2")
    h.service.prepare("t2", second["job_id"])
    h.service.start("t2", second["job_id"])
    h.service.finish("t2", second["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    assert h.count("lora_artifact") == 2
    assert h.count("lora_artifact", "tenant_id='t1'") == 1
    assert h.count("lora_artifact", "tenant_id='t2'") == 1


# ================================================================== 9.7 建议

def test_t48_41_lora_registered_is_advisory_only(h):
    """T48-41 [C11-4]：只给建议，不自动接受（C48-4 说事件未注册）。"""
    job = h.run_to_published()
    advisories = job["advisories"]
    assert advisories[0]["event_type"] == "training.lora.registered"
    assert advisories[0]["effect"] == "advisory_only"
    assert "auto-accept" in advisories[0]["note"]
    # 没有发一个未注册的事件到 outbox
    assert h.events("training.lora.registered") == []
    # 本服务没有 S11 的资产表
    tables = {r[0] for r in h.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert not any("character" in name or "voice" in name for name in tables)


def test_t48_42_voice_clone_advisory_targets_s15(h):
    """T48-42：voice_clone 的建议指向 S15，不自动接受。"""
    job = h.create(adaptation_type="voice_clone", target_ref={
        "service_id": "S15", "id": "voice_1", "revision": 2,
        "content_hash": HC})
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.finish("t1", job["job_id"], outcome="trained",
                     weights_blob_ref="s3://s48/w", weights_hash=HF,
                     size_bytes=1, gpu_seconds=10)
    h.service.evaluate("t1", job["job_id"], metrics={
        "identity_preservation": 0.9, "overfit_score": 0.1,
        "holdout_loss": 0.2, "voice_similarity": 0.9, "intelligibility": 0.95})
    published = h.service.accept("t1", job["job_id"])
    advisory = published["advisories"][0]
    assert advisory["event_type"] == "training.voice_clone.ready"
    assert advisory["target"] == "S15"
    assert advisory["effect"] == "advisory_only"


def test_t48_43_no_cross_service_writes(h):
    """T48-43：没有任何跨服务的 DB 写路径（E01）。

    证据是结构性的：本连接里只有本服务的表 + 两个 SDK 表。
    """
    tables = {r[0] for r in h.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    allowed_prefixes = ("training_", "dataset_", "base_", "lora_",
                        "outbox", "inbox", "idempotency_")
    for table in tables:
        assert table.startswith(allowed_prefixes), table


# ================================================================== 9.8 跨租户

def test_t48_44_cross_tenant_reads_are_404(h):
    """T48-44 [E28]：租户 B 查 A 的 job/dataset/artifact → 404，无泄漏。"""
    job = h.run_to_published(tenant_id="t1")
    artifact_id = job["artifact_ref"]["id"]
    dataset_id = job["dataset_ref"]["id"]
    for call in (lambda: h.service.get_job("t2", job["job_id"]),
                 lambda: h.service.get_job("t2", dataset_id),
                 lambda: h.service.get_dataset("t2", dataset_id),
                 lambda: h.service.get_dataset_items("t2", dataset_id),
                 lambda: h.service.get_artifact("t2", artifact_id),
                 lambda: h.service.get_compatibility("t2", artifact_id),
                 lambda: h.service.list_audit("t2", job["job_id"])):
        with pytest.raises(ServiceError) as info:
            call()
        assert info.value.status == 404
        assert code_of(info) == "NOT_FOUND"
        # 消息里不能出现目标租户的标识
        assert "t1" not in str(info.value)


def test_t48_44b_cross_tenant_cancel_and_events_denied(h):
    """跨租户写命令同形 404；事件 tenant 不符 → 403。"""
    job = h.create(tenant_id="t1")
    with pytest.raises(ServiceError) as info:
        h.service.cancel("t2", job["job_id"])
    assert info.value.status == 404
    with pytest.raises(ServiceError) as info:
        h.service.on_rights_revoked("t2", {
            "event": {"id": "e1", "type": "rights.revoked", "tenant_id": "t1",
                      "data": {"rights_id": "rt_1"}}})
    assert info.value.status == 403


def test_t48_45_datasets_are_not_shared_across_tenants(h):
    """T48-45 [E28]：数据集结构上不共享——`dataset_manifest` 带 tenant_id，
    `dataset_item` 主键是 (dataset_id, artifact_id) 且每条都有 tenant 约束。

    这里直接验证两个租户同 artifact 也各有一份 manifest。
    """
    one = h.create(tenant_id="t1", command_id="c1")
    two = h.create(tenant_id="t2", command_id="c2")
    assert one["dataset_ref"]["id"] != two["dataset_ref"]["id"]
    assert h.count("dataset_manifest") == 2
    # 同 artifact 在两个 dataset 里都能查，但只能在自己租户里
    with pytest.raises(ServiceError):
        h.service.get_dataset("t2", one["dataset_ref"]["id"])


def test_t48_45b_exclusion_rows_are_tenant_scoped():
    """E28/E39：`dataset_exclusion` 的读也带租户过滤。

    这条单独测，因为排除项是**另一张表**：只守 `dataset_manifest` 不够，
    一个漏 `tenant_id=?` 的查询会让租户 B 读到租户 A 的撤销原因。
    """
    h = Harness(min_ratio=0.4)  # 让 1/2 的排除不触发整单拒绝
    refs = [{"id": "art_1", "content_hash": HA},
            {"id": "art_2", "content_hash": HB}]
    h.rights_state["art_2"] = rights(state="revoked", content_hash=HB,
                                     rights_id="rt_2")
    one = h.create(tenant_id="t1", command_id="c1",
                   dataset={"mode": "explicit", "artifact_refs": refs})
    dataset_id = one["dataset_ref"]["id"]
    assert h.row("dataset_exclusion", "dataset_id=?", (dataset_id,)) is not None

    h.rights_state["art_2"] = rights(content_hash=HB, rights_id="rt_2")
    h.create(tenant_id="t2", command_id="c2",
             dataset={"mode": "explicit", "artifact_refs": refs})

    # 租户 B 用 A 的 dataset_id 读 → 404（manifest 那一层就挡住）
    with pytest.raises(ServiceError):
        h.service.get_dataset("t2", dataset_id)
    # 直接数表：排除行确实挂在 t1 下，没被记到别人头上
    assert h.count("dataset_exclusion",
                   "dataset_id=? AND tenant_id='t1'", (dataset_id,)) == 1
    assert h.count("dataset_exclusion",
                   "dataset_id=? AND tenant_id<>'t1'", (dataset_id,)) == 0


def test_t48_46_home_region_is_recorded_not_guessed(h):
    """T48-46 [E31]：home_region 落库，来源是 context，不是推断（C48-23）。"""
    job = h.create()
    assert job["home_region"] == "cn-east"
    # 没给就写 unspecified，绝不从别的字段推
    body = dataset_body()
    bare = h.service.create_job("t3", body, "c_bare", "h" * 64)
    assert bare["home_region"] == "unspecified"


def test_t48_46b_home_region_missing_column_is_not_nullable(h):
    """C48-23：home_region 列 NOT NULL——直插空值被拒。"""
    job = h.create()
    with pytest.raises(sqlite3.IntegrityError):
        h.conn.execute(
            "INSERT INTO training_job (job_id, tenant_id, command_id,"
            " request_hash, adaptation_type, target_service, target_id,"
            " target_revision, target_content_hash, base_id, base_revision,"
            " base_content_hash, hyperparams, evaluation_spec, state,"
            " state_since, budget_auth_ref, requested_by, home_region,"
            " created_at) VALUES ('job_x','t1','c_x',?, 'character_lora',"
            " 'S11','a',1,?,'mdl',1,?,'{}','{}','REQUESTED','now','{}','{}',"
            " NULL,'now')", (HA, HA, HA))


# ================================================================== E01 / E40

def test_t48_47_imports_only_contract_sdks(h):
    """T48-47 [E01]：只 import 平台 SDK，不 import 别的服务的包。"""
    import adaptation_training
    import adaptation_training.schema
    import adaptation_training.service
    for module in (adaptation_training, adaptation_training.schema,
                   adaptation_training.service):
        for name, value in vars(module).items():
            origin = getattr(value, "__module__", "") or ""
            if origin.startswith("services."):
                pytest.fail(f"{name} comes from {origin}")


def test_t48_48_old_versions_can_read_new_rows(h):
    """T48-48 [E01]：回滚兼容——新列都有默认值，旧代码路径不读到 NULL。

    检查 C48-13 新增六列都有 DEFAULT 或允许 NULL，且迁移不需要重写旧行。
    """
    info = {col[1]: col for col in h.conn.execute(
        "PRAGMA table_info(training_job)").fetchall()}
    for column in ("aggregate_sequence", "resume_count", "eval_attempts",
                   "rights_hold"):
        assert info[column][4] is not None, column  # dflt_value
    for column in ("last_heartbeat_at", "home_region"):
        assert column in info
    # 旧行（C48-13 之前的 schema）在新 schema 下也能读出
    job = h.create()
    row = h.row("training_job", "job_id=?", (job["job_id"],))
    assert row["rights_hold"] == "[]"
    assert row["home_region"]


def test_t48_49_not_enabled_tenant_gets_403(h):
    """T48-49：未订购 S48 → 403 TRAINING_NOT_ENABLED。"""
    h.service.training_enabled = False
    with pytest.raises(ServiceError) as info:
        h.create()
    assert info.value.status == 403
    assert code_of(info) == "TRAINING_NOT_ENABLED"


def test_t48_50_full_training_is_traceable(h):
    """T48-50 [E40]：一次完整训练——数据来源、授权、基座、评测、产物全可查。"""
    job = h.run_to_published()
    artifact_id = job["artifact_ref"]["id"]
    artifact = h.service.get_artifact("t1", artifact_id)
    dataset = h.service.get_dataset("t1", job["dataset_ref"]["id"])
    items = h.service.get_dataset_items("t1", job["dataset_ref"]["id"])
    compat = h.service.get_compatibility("t1", artifact_id)
    audit = h.service.list_audit("t1", job["job_id"])

    # 1. 数据来源：items_hash + 逐项 rights
    assert dataset["items_hash"] == items_hash([
        {"artifact_id": item["artifact_ref"]["id"],
         "content_hash": item["artifact_ref"]["content_hash"]}
        for item in items["items"]])
    assert all(item["rights_ref"]["id"] for item in items["items"])
    # 2. 授权：每项的 grant_kind 明确含 training
    assert all("training" in str(item["rights_ref"]["grant_kind"])
               for item in items["items"])
    # 3. 基座：revision + content_hash + 兼容性记录
    assert artifact["base_model"]["revision"] == 7
    assert artifact["base_model"]["content_hash"] == HD
    assert compat["verdict"] == "COMPATIBLE"
    # 4. 评测：指标 + holdout 摘要 + 阈值
    assert artifact["evaluation"]["passed"] is True
    assert artifact["evaluation"]["holdout_items_hash"]
    # 5. 产物：权重 hash + S28 引用
    assert artifact["weights"]["weights_hash"] == HF
    assert artifact["weights"]["artifact_ref"]["id"] == "art_s28_1"
    # 6. 链路：审计完整
    actions = [entry["action"] for entry in audit["items"]]
    for expected in ("training.job.create", "training.dataset.freeze",
                     "training.job.start", "training.job.complete",
                     "training.artifact.publish"):
        assert expected in actions, expected


# ================================================================== 契约与事件

def test_event_payload_matches_contract(h):
    """§4.3：真实产出的事件能过契约的 JSON Schema（additionalProperties:false）。"""
    jsonschema = pytest.importorskip("jsonschema")
    h.run_to_candidate()
    with io.open(CONTRACT, encoding="utf-8") as handle:
        schema = json.load(handle)
    data = h.events()[0]["data"]
    jsonschema.validate(data, schema)
    # 事件里不含训练内容
    for forbidden in ("hyperparams", "dataset_items", "weights_url", "prompt"):
        assert forbidden not in data


def test_event_target_ref_has_three_keys_per_contract(h):
    """C48-19：DDL 有四列，事件只有三键——不塞契约没允许的键。"""
    h.run_to_candidate()
    data = h.events()[0]["data"]
    assert set(data["target_ref"]) == {"service_id", "id", "revision"}
    # 四列里的 hash 仍在库里可查
    row = h.row("training_job", "job_id=?", (data["job_id"],))
    assert row["target_content_hash"] == HC


def test_event_sequence_monotonic_across_transitions(h):
    """§4.3：aggregate_sequence 每状态迁移 +1，单调。"""
    job = h.create()
    assert h.service.get_job("t1", job["job_id"])["aggregate_sequence"] == 2
    h.service.prepare("t1", job["job_id"])
    assert h.service.get_job("t1", job["job_id"])["aggregate_sequence"] == 3
    h.service.start("t1", job["job_id"])
    assert h.service.get_job("t1", job["job_id"])["aggregate_sequence"] == 4


def test_event_data_ref_is_null(h):
    """ATR-1：training_job 没有 revision/content_hash，所以 data_ref=null。"""
    h.run_to_candidate()
    assert h.events()[0]["data_ref"] is None


def test_illegal_transition_is_409(h):
    """§3.1 的白名单：REQUESTED 不能直接到 TRAINING（实际由内核拒绝）。"""
    job = h.create()
    with pytest.raises(ServiceError) as info:
        h.service.start("t1", job["job_id"])
    assert info.value.status == 409
    assert code_of(info) == "JOB_STATE_CONFLICT"
    assert info.value.details["from"] == "QUEUED"


def test_cancel_before_start_goes_rejected(h):
    """C48-12：未开训取消 → REJECTED（不是 CANCELLED 状态，那不存在）。"""
    job = h.create()
    result = h.service.cancel("t1", job["job_id"],
                              reason={"message": "客户改需求"})
    assert result["state"] == "REJECTED"
    assert result["rejection"]["reason"] == "CANCELLED"
    assert result["rejection"]["cancelled_from"] == "QUEUED"


def test_cancel_during_training_goes_failed_keeping_checkpoint(h):
    """C48-12：已开训取消 → FAILED，保留 checkpoint。"""
    job = h.create()
    h.service.prepare("t1", job["job_id"])
    h.service.start("t1", job["job_id"])
    h.service.progress("t1", job["job_id"], gpu_seconds=5,
                       checkpoint_ref="ckpt_c")
    result = h.service.cancel("t1", job["job_id"])
    assert result["state"] == "FAILED"
    assert result["rejection"]["cancelled_from"] == "TRAINING"
    assert h.row("training_job", "job_id=?", (job["job_id"],))[
        "checkpoint_ref"] == "ckpt_c"


def test_terminal_states_have_no_exit(h):
    """§3.1：终态不能离开（BLOCKED 尤其不能复活）。

    `PUBLISHED` 不是终态：它有一条通往 `BLOCKED` 的边（撤销/基座退役），
    而 `BLOCKED` 自己没有任何出边。所以判据是"状态自身没有出边"，
    不是"没在迁移表里出现过"。
    """
    transitions = __import__("adaptation_training").TRANSITIONS
    for state in ("REJECTED", "EVAL_FAILED", "BLOCKED", "FAILED", "EXPIRED"):
        assert not transitions.get(state), state
    # PUBLISHED 唯一的出边就是 BLOCKED——没别的
    assert transitions["PUBLISHED"] == frozenset({"BLOCKED"})
    # BLOCKED 是终态：重新授权、再次 accept 都不能把它拉回来
    assert "BLOCKED" not in transitions


def test_request_hash_ignores_volatile_context(h):
    """§6.1：同逻辑命令换 trace 仍是同一次重放。"""
    base = dataset_body()
    one = {**base, "context": {"trace_id": "a", "home_region": "cn-east"}}
    two = {**base, "context": {"trace_id": "b", "home_region": "cn-east"}}
    assert stable_request_hash(one) == stable_request_hash(two)
    three = {**base, "context": {"trace_id": "a", "home_region": "us-west"}}
    assert stable_request_hash(one) != stable_request_hash(three)


def test_idempotent_replay_returns_same_job(h):
    """E12：同 command 同 body → 同一 job；同 command 异 body → 409。"""
    first = h.create(command_id="c1")
    replay = h.create(command_id="c1")
    assert replay["replayed"] is True
    assert replay["job_id"] == first["job_id"]
    assert h.count("training_job") == 1
    with pytest.raises(CommandConflict):
        h.create(command_id="c1", adaptation_type="style_lora")


def test_retention_check_reports_both_kinds(h):
    """§10.3：S28 删字节前问本服务——产物与数据源分别回答。"""
    job = h.run_to_published()
    artifact_id = job["artifact_ref"]["id"]
    published = h.service.retention_check("t1", kind="lora_artifact",
                                          identifier=artifact_id)
    assert published["referenced"] is True
    assert published["reason"] == "PUBLISHED_LORA"
    source = h.service.retention_check("t1", kind="dataset_artifact",
                                       identifier="art_1")
    assert source["referenced"] is True
    assert source["reason"] == "DATASET_SOURCE"
    stray = h.service.retention_check("t1", kind="dataset_artifact",
                                      identifier="art_nope")
    assert stray["referenced"] is False


def test_contract_gaps_are_registered(h):
    """C48 系列缺口都写进了 CONTRACT_GAPS（不是代码里的隐式假设）。"""
    ids = {gap["id"] for gap in CONTRACT_GAPS}
    for expected in ("C48-1", "C48-3", "C48-4", "C48-12", "C48-13", "C48-15",
                     "C48-16", "C48-19", "C48-22", "C48-23"):
        assert expected in ids, expected
    assert len(CONTRACT_GAPS) >= 20


def test_id_prefixes_are_stable(h):
    assert ID_PREFIX["job"] == "job_"
    assert ID_PREFIX["dataset"] == "ds_"
    assert ID_PREFIX["artifact"] == "lora_"
    assert ID_PREFIX["compat"] == "cmp_"


# ================================================================== HTTP 层

@pytest.fixture()
def client(tmp_path):
    """独立进程内 DB，避免与内存 fixture 互相影响。

    按文件路径加载：不能 `import app`，别的服务也有 `api/app.py` 且可能先入
    `sys.modules`；也不能往 `sys.path` 塞目录，那是进程级全局状态，会污染
    同一次 session 里后跑的别的服务（两者都由 `pyproject.toml` 的 `pythonpath`
    统一提供，这里不需要再补）。
    """
    import os
    os.environ["S48_DB"] = str(tmp_path / "s48.db")
    os.environ["S48_ENABLED"] = "1"
    for name in [n for n in sys.modules if n.startswith("adaptation_training")]:
        del sys.modules[name]
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s48_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s48_api_app"] = module
    return TestClient(module.app), module


def test_http_requires_tenant_header(client):
    api, _module = client
    response = api.post("/training-jobs", json=dataset_body(),
                        headers={"Idempotency-Key": "c1"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_SCOPE_DENIED"


def test_http_requires_idempotency_key(client):
    api, _module = client
    response = api.post("/training-jobs", json=dataset_body(),
                        headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 400


def test_http_body_tenant_must_match_header(client):
    api, _module = client
    body = {**dataset_body(), "tenant_id": "t2"}
    response = api.post("/training-jobs", json=body,
                        headers={"X-Tenant-Id": "t1",
                                 "Idempotency-Key": "c1"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_SCOPE_DENIED"


def test_http_command_id_must_equal_header(client):
    api, _module = client
    body = {**dataset_body(), "command_id": "c_other"}
    response = api.post("/training-jobs", json=body,
                        headers={"X-Tenant-Id": "t1",
                                 "Idempotency-Key": "c1"})
    assert response.status_code == 400


def test_http_same_key_different_body_is_409(client):
    api, _module = client
    headers = {"X-Tenant-Id": "t1", "Idempotency-Key": "c1"}
    first = api.post("/training-jobs", json=dataset_body(), headers=headers)
    assert first.status_code == 201
    second = api.post("/training-jobs",
                      json=dataset_body(adaptation_type="style_lora"),
                      headers=headers)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_http_readiness_exposes_gaps(client):
    api, _module = client
    payload = api.get("/health/readiness").json()
    assert payload["ready"] is True
    assert any(gap["id"] == "C48-1" for gap in payload["contract_gaps"])
    assert payload["production_switch_on"] is True


def test_http_404_for_unknown_job(client):
    api, _module = client
    response = api.get("/training-jobs/job_missing",
                       headers={"X-Tenant-Id": "t1"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
