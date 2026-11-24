"""S02 测试。对照 `plans/S02-governance-policy.md` §12 的用例表。

分两层：

- `merge` 是纯函数，直接测代数性质（幂等 / 交换 / 结合）与 §4.6 的完整示例。
- `GovernancePolicyService` 用内存 SQLite，测发布期校验与解析路径。

所有用例都不接真实模型、不付费；跨服务的上下文（S01 授权、S23 余额、S08
manifest）**一律不 mock 成"已通过"**——本服务根本不读它们，只回显绑定。
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import SqliteIdempotencyStore

from governance_policy import SCHEMA
from governance_policy import merge as M
from governance_policy.service import (
    GovernancePolicyService,
    ServiceError,
    content_hash,
)

ROOT = Path(__file__).resolve().parents[3]
POLICY_SCHEMA = ROOT / "contracts" / "automation-policy.v2.schema.json"
EVENT_SCHEMA = ROOT / "contracts" / "policy.revision-published.v2.schema.json"

TA = "t_aaa"
TB = "t_bbb"


# -- 构造 ----------------------------------------------------------------

def _doc(**over) -> dict:
    """一份能过 schema 的最小企业文档。

    `mode=exception_assisted` + `generation_mode=auto` 是 §2 矩阵里的合法组合，
    避免默认样例自己撞上 422。
    """
    doc = {
        "schema_version": "2.0",
        "policy_id": "policy_base",
        "revision": 1,
        "tenant_id": TA,
        "mode": "exception_assisted",
        "generation_mode": "auto",
        "acceptance_mode": "auto_if_qc_pass",
        "overrides": [],
        "repair_limits": {"max_business_attempts_per_shot": 5,
                          "max_identical_strategy_repeats": 2},
        "exception_behavior": "wait_review",
        "required_gates": ["canonical_asset", "final_master"],
        "hard_rules": {"budget_scope_id": "budget_enterprise",
                       "rights_required": True,
                       "enforce_data_residency": True,
                       "no_incomplete_formal_delivery": True,
                       "allow_silent_identity_downgrade": False},
        "effective_at": "2026-01-01T00:00:00Z",
        "created_by": "principal_ops",
    }
    doc.update(over)
    return doc


def _ov(**over) -> dict:
    ov = {"scope_type": "node", "scope_id": "shot_3.video",
          "generation_mode": "auto", "acceptance_mode": "auto_if_qc_pass",
          "reviewer_roles": [], "locked": False}
    ov.update(over)
    return ov


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    SqliteIdempotencyStore(conn)
    return conn


def _svc() -> GovernancePolicyService:
    return GovernancePolicyService(_connect())


def _publish(svc: GovernancePolicyService, doc: dict, *, policy_id=None,
             scope=None, expected=None) -> dict:
    policy_id = policy_id or doc["policy_id"]
    return svc.create_revision(TA, policy_id, {
        "policy": doc,
        "expected_revision": expected if expected is not None else doc["revision"] - 1,
        "scope": scope,
        "command_id": f"cmd_{doc['policy_id']}_{doc['revision']}",
    })


def _err_code(fn, *a, **kw) -> str:
    with pytest.raises(ServiceError) as ei:
        fn(*a, **kw)
    return ei.value.code


# -- §4.3 算子代数性质 ---------------------------------------------------

ENUM_CASES = [
    ("mode", ["guided", "stage_review", "exception_assisted", "unattended_batch"]),
    ("generation_mode", ["manual", "auto"]),
    ("acceptance_mode", ["manual", "auto_if_qc_pass"]),
    ("exception_behavior", ["wait_review", "isolate_branch"]),
]


@pytest.mark.parametrize("field,values", ENUM_CASES)
def test_enum_join_is_idempotent_commutative_associative(field, values):
    """§4.3 行 158：三条性质保证「层缺失、乱序重放、投影重建不改结果」。"""
    fn = M.MERGE[field]
    for a, b, c in itertools.product(values, repeat=3):
        assert fn(a, a) == a
        assert fn(a, b) == fn(b, a)
        assert fn(fn(a, b), c) == fn(a, fn(b, c))


def test_enum_join_picks_the_stricter_value():
    assert M.MERGE["mode"]("unattended_batch", "stage_review") == "stage_review"
    assert M.MERGE["generation_mode"]("auto", "manual") == "manual"
    assert M.MERGE["acceptance_mode"]("auto_if_qc_pass", "manual") == "manual"


def test_set_operators_go_opposite_directions():
    """§4.3 行 195：写反一个就等于放行，所以这条单独钉住。"""
    # required_gates 并集 = 更多门 = 更严
    assert M.MERGE["required_gates"]({"script"}, {"final_master"}) == {
        "script", "final_master"}
    # allowed_actions / reviewer_roles 交集 = 更少选择 = 更严
    assert M.MERGE["reviewer_roles"]({"director", "finance"}, {"director"}) == {
        "director"}
    assert M.MERGE["allowed_actions"]({"generate", "publish"},
                                      {"generate"}) == {"generate"}


def test_none_is_the_identity_element():
    """§4.3 行 190：`join(x, None) == x`，这是「缺省不参与合并」的实现方式。"""
    for field, fn in M.MERGE.items():
        sample = {"mode": "guided", "generation_mode": "manual",
                  "acceptance_mode": "manual", "exception_behavior": "wait_review",
                  "required_gates": {"script"}, "allowed_actions": {"a"},
                  "reviewer_roles": {"r"}, "approved_evaluators": {"e"},
                  "max_business_attempts_per_shot": 3,
                  "max_identical_strategy_repeats": 1,
                  "qc_min_coverage": 0.8, "quorum": 2,
                  "separation_of_duties": True, "locked": True}[field]
        assert fn(sample, None) == sample, field
        assert fn(None, sample) == sample, field


# -- §4.4 合并与 §4.6 示例 ------------------------------------------------

def _layer(fields: dict, scope: str, policy="p") -> dict:
    return {**fields, "_source": {"scope_type": "x", "scope_id": scope,
                                  "policy_id": policy, "revision": 1,
                                  "content_hash": "sha256:x",
                                  "effective_at": "2026-01-01T00:00:00.000000Z"}}


def test_plan_section_4_6_merge_example():
    """§4.6 行 301-311 的完整例子，逐字段对照。"""
    enterprise = _layer({
        "mode": "exception_assisted", "generation_mode": "auto",
        "acceptance_mode": "auto_if_qc_pass",
        "required_gates": {"canonical_asset", "final_master"},
        "max_business_attempts_per_shot": 5,
        "max_identical_strategy_repeats": 2,
        "reviewer_roles": {"director", "producer", "finance"},
    }, "tenant")
    project = _layer({"mode": "stage_review", "required_gates": {"script"},
                      "max_business_attempts_per_shot": 3}, "project")
    episode = _layer({"acceptance_mode": "manual"}, "episode")
    # 节点那条想放宽 acceptance_mode —— 发布期会 422，这里单独验证 join 的效果
    node = _layer({"generation_mode": "auto",
                   "acceptance_mode": "auto_if_qc_pass",
                   "max_business_attempts_per_shot": 8}, "node")

    effective, prov = M.merge_layers([enterprise, project, episode, node])

    assert effective["mode"] == "stage_review"
    assert prov["fields"]["mode"]["source"]["scope_id"] == "project"
    assert effective["generation_mode"] == "auto"
    # 集层胜：节点的放宽被 join 吃掉
    assert effective["acceptance_mode"] == "manual"
    assert prov["fields"]["acceptance_mode"]["source"]["scope_id"] == "episode"
    assert effective["required_gates"] == ["canonical_asset", "final_master",
                                           "script"]
    assert effective["max_business_attempts_per_shot"] == 3
    assert effective["max_identical_strategy_repeats"] == 2
    assert effective["reviewer_roles"] == ["director", "finance", "producer"]


def test_merge_result_is_independent_of_layer_order():
    """E05-07 / E05-12：乱序重放 100 次结果一致，policy_hash 相同。"""
    layers = [
        _layer({"mode": "stage_review"}, "a"),
        _layer({"required_gates": {"script"}}, "b"),
        _layer({"required_gates": {"final_master"},
                "max_business_attempts_per_shot": 3}, "c"),
        _layer({"mode": "exception_assisted",
                "max_business_attempts_per_shot": 5}, "d"),
    ]
    base, _ = M.merge_layers(layers)
    base_hash = content_hash(base)
    for perm in itertools.permutations(layers):
        got, _ = M.merge_layers(perm)
        assert content_hash(got) == base_hash


def test_defaults_materialize_only_when_no_layer_sets_the_field():
    """§2 行 91：一旦某层显式设置，就只做并集，不回落 mode 默认值。"""
    no_gates, prov = M.merge_layers([_layer({"mode": "unattended_batch"}, "t")])
    assert no_gates["required_gates"] == ["final_master"]
    assert prov["fields"]["required_gates"]["rule"] == "DEFAULT"

    explicit, prov2 = M.merge_layers([
        _layer({"mode": "unattended_batch"}, "t"),
        _layer({"required_gates": {"script"}}, "p"),
    ])
    assert explicit["required_gates"] == ["script"]
    assert prov2["fields"]["required_gates"]["rule"] == "UNION"


def test_unattended_plus_manual_acceptance_demotes_mode_but_keeps_manual():
    """§2 行 73：方向上这是收紧，所以保留 manual、降级 mode、并记一条归一化。"""
    effective, prov = M.merge_layers([
        _layer({"mode": "unattended_batch", "acceptance_mode": "manual"}, "t")])
    assert effective["acceptance_mode"] == "manual"
    assert effective["mode"] == "exception_assisted"
    assert [n["code"] for n in prov["normalized"]] == ["NORMALIZED_MODE_DEMOTED"]
    assert prov["normalized"][0]["from"] == "unattended_batch"


def test_unattended_forces_isolate_branch():
    effective, _ = M.merge_layers([
        _layer({"mode": "unattended_batch",
                "exception_behavior": "wait_review"}, "t")])
    assert effective["exception_behavior"] == "isolate_branch"


def test_missing_mode_yields_no_defaults_instead_of_guessing():
    """企业层没给 mode 时不能编一个档位；由调用方按 ENTERPRISE_POLICY_MISSING 拒。"""
    effective, _ = M.merge_layers([_layer({"generation_mode": "auto"}, "p")])
    assert "mode" not in effective
    assert "acceptance_mode" not in effective


# -- 发布期校验 §4.5 -----------------------------------------------------

def test_publish_stores_revision_and_moves_the_pointer():
    svc = _svc()
    out = _publish(svc, _doc())
    assert out["policy_ref"]["revision"] == 1
    row = svc._pointer(TA, "policy_base")
    assert row["current_revision"] == 1
    assert row["content_hash"] == content_hash(_doc())


def test_e05_02_node_cannot_loosen_mode():
    """v2 的 overrides 六键里没有 `mode`，节点连"想升档"都表达不出来。

    能表达的是两个开关，所以放宽测试打在 `acceptance_mode` 上（E05-03）。
    这条钉住的是**表达面本身**：谁往 overrides 里加 `mode` 都会被 422。
    """
    svc = _svc()
    _publish(svc, _doc(mode="stage_review"))
    doc = _doc(revision=2, mode="stage_review",
               overrides=[{**_ov(), "mode": "unattended_batch"}])
    assert _err_code(_publish, svc, doc) == "POLICY_SCHEMA_VIOLATION"


def test_e05_03_node_cannot_loosen_acceptance_mode():
    svc = _svc()
    _publish(svc, _doc(acceptance_mode="manual"))
    doc = _doc(revision=2, acceptance_mode="manual",
               overrides=[_ov(acceptance_mode="auto_if_qc_pass")])
    with pytest.raises(ServiceError) as ei:
        _publish(svc, doc)
    assert ei.value.code == "LOOSENING_NOT_ALLOWED"
    assert ei.value.details["enforced"] == "manual"
    assert ei.value.details["requested"] == "auto_if_qc_pass"


def test_e05_04_node_can_tighten_generation_mode():
    svc = _svc()
    _publish(svc, _doc(generation_mode="auto"))
    out = _publish(svc, _doc(revision=2, generation_mode="auto",
                             overrides=[_ov(generation_mode="manual",
                                            acceptance_mode="auto_if_qc_pass")]))
    assert out["policy_ref"]["revision"] == 2


def test_e05_08_reviewer_role_escape_guard_rejects_extra_roles():
    """§4.5 (c) / E05-08：覆盖写进来的角色必须是祖先允许的子集。

    **这是单元级用例，不是端到端。** v2 文档根级没有 `reviewer_roles`
    （plans/S02 §11 G2），`overrides[].scope_type` 又不含 `tenant`，所以目前
    没有任何合法文档能给祖先层填上这个字段——`_parent_effective` 给出的
    `allow` 恒为空集，端到端路径走不到这条守卫。守卫保留（G2 修好后立即
    生效），这里直接喂一个带角色范围的 parent 钉住判定与错误形状。
    """
    svc = _svc()
    parent = {"reviewer_roles": ["director", "producer"],
              "_provenance": {"reviewer_roles": {
                  "source": {"scope_type": "tenant", "scope_id": TA}}}}
    with pytest.raises(ServiceError) as ei:
        svc._validate_override(_ov(reviewer_roles=["finance"]), parent)
    assert ei.value.code == "REVIEWER_ROLE_ESCAPE"
    assert ei.value.details["extra"] == ["finance"]
    assert ei.value.details["allowed"] == ["director", "producer"]


def test_reviewer_role_subset_of_the_ancestor_is_allowed():
    """`[director]` ⊆ `[director, producer]` 是收紧，守卫必须放行。"""
    svc = _svc()
    parent = {"reviewer_roles": ["director", "producer"]}
    svc._validate_override(_ov(reviewer_roles=["director"]), parent)



def test_reviewer_roles_are_not_constrained_when_ancestor_declares_none():
    """祖先没说角色范围时不编造一个上界——那会把合法配置变成 422。"""
    svc = _svc()
    out = _publish(svc, _doc(overrides=[
        _ov(scope_type="project", scope_id="proj_1",
            reviewer_roles=["finance"])]))
    assert out["policy_ref"]["revision"] == 1


def test_e05_05_guided_plus_auto_is_422_not_silently_corrected():
    """§2 行 74。修正会放宽，所以发布期直接拒，让人看见。"""
    svc = _svc()
    assert _err_code(_publish, svc,
                     _doc(mode="guided", generation_mode="auto",
                          acceptance_mode="manual")) == "POLICY_MODE_INCONSISTENT"


def test_hard_rules_are_immutable():
    svc = _svc()
    for field, bad in (("rights_required", False),
                       ("enforce_data_residency", False),
                       ("no_incomplete_formal_delivery", False),
                       ("allow_silent_identity_downgrade", True)):
        doc = _doc()
        doc["hard_rules"] = {**doc["hard_rules"], field: bad}
        assert _err_code(_publish, svc, doc) == "HARD_RULE_IMMUTABLE", field


def test_missing_budget_scope_is_rejected():
    svc = _svc()
    doc = _doc()
    doc["hard_rules"] = {k: v for k, v in doc["hard_rules"].items()
                         if k != "budget_scope_id"}
    assert _err_code(_publish, svc, doc) == "POLICY_SCHEMA_VIOLATION"


def test_e12_04_expected_revision_mismatch_is_409_without_new_revision():
    svc = _svc()
    _publish(svc, _doc())
    with pytest.raises(ServiceError) as ei:
        _publish(svc, _doc(revision=2), expected=7)
    assert ei.value.code == "REVISION_CONFLICT"
    assert svc.conn.execute(
        "SELECT COUNT(*) FROM automation_policy_revision").fetchone()[0] == 1


def test_revision_must_be_one_past_the_pointer():
    svc = _svc()
    with pytest.raises(ServiceError) as ei:
        _publish(svc, _doc(revision=5), expected=0)
    assert ei.value.code == "REVISION_CONFLICT"


# -- schema 对拍 ---------------------------------------------------------

def test_valid_documents_pass_the_contract_schema():
    """`_validate_document_shape` 接受的东西，jsonschema 也必须接受。

    反方向（jsonschema 拒、本函数收）会在解析期才炸，所以两个方向都要对拍。
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(POLICY_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    samples = [_doc(), _doc(mode="guided", generation_mode="manual",
                            acceptance_mode="manual"),
               _doc(overrides=[_ov()])]
    for sample in samples:
        validator.validate(sample)
        _svc()._validate_document_shape(sample)


def test_published_event_matches_the_contract_schema():
    jsonschema = pytest.importorskip("jsonschema")
    svc = _svc()
    _publish(svc, _doc())
    row = svc.conn.execute("SELECT payload FROM outbox").fetchone()
    envelope = json.loads(row["payload"])
    schema = json.loads(EVENT_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(envelope["data"])
    assert "document" not in envelope["data"]


# -- 解析路径 §5 ---------------------------------------------------------

def _publish_base(svc: GovernancePolicyService):
    return _publish(svc, _doc(), scope={"type": "tenant", "id": TA})


def test_resolve_returns_fields_provenance_and_gate_plan():
    svc = _svc()
    _publish_base(svc)
    out = svc.resolve_effective(TA, {"context": {
        "tenant_id": TA, "project_id": "proj_1", "node_epoch": 4,
        "execution_fence": 2}})
    assert out["effective"]["mode"] == "exception_assisted"
    assert out["effective"]["repair_limits"]["max_business_attempts_per_shot"] == 5
    assert out["provenance"]["fields"]["mode"]["rule"] == "MIN_RANK"
    assert {g["gate"] for g in out["gate_plan"]} == {"canonical_asset",
                                                     "final_master"}
    phases = {g["gate"]: g["phase"] for g in out["gate_plan"]}
    assert phases["canonical_asset"] == "pre_dispatch"
    assert phases["final_master"] == "post_generation"
    assert {g["stop_at"] for g in out["gate_plan"]} == {"WAITING_GATE"}
    # §5 行 349：`hard_rules` 必须出现在 `effective` 里。它是 v2 的必填键，
    # 发布期 (e) 已经校验过；不吐出来的话 S26 派单前读不到 rights_required /
    # budget_scope_id —— 那正是 §6.1 各 gate 放行条件的输入。
    assert out["effective"]["hard_rules"]["rights_required"] is True
    assert out["effective"]["hard_rules"]["budget_scope_id"] == "budget_enterprise"


def test_resolve_echoes_epoch_without_merging_it():
    """§5 行 373：epoch 不参与合并，但必须回显绑定。"""
    svc = _svc()
    _publish_base(svc)
    a = svc.resolve_effective(TA, {"context": {
        "tenant_id": TA, "project_id": "p", "node_epoch": 3,
        "execution_fence": 1}})
    b = svc.resolve_effective(TA, {"context": {
        "tenant_id": TA, "project_id": "p", "node_epoch": 4,
        "execution_fence": 1}})
    assert a["effective"] == b["effective"]
    assert a["evaluated_for"]["node_epoch"] == 3
    assert b["evaluated_for"]["node_epoch"] == 4


def test_node_override_reaches_resolution():
    svc = _svc()
    _publish_base(svc)
    _publish(svc, _doc(policy_id="policy_proj", revision=1, mode="stage_review",
                       generation_mode="manual", acceptance_mode="manual",
                       overrides=[_ov(scope_type="node", scope_id="shot_3.*",
                                      generation_mode="manual",
                                      acceptance_mode="manual",
                                      reviewer_roles=["director"])]),
             policy_id="policy_proj", scope={"type": "project", "id": "proj_1"})
    out = svc.resolve_effective(TA, {"context": {
        "tenant_id": TA, "project_id": "proj_1", "node_id": "shot_3.video"}})
    assert out["effective"]["mode"] == "stage_review"
    assert out["effective"]["reviewer_roles"] == ["director"]


def test_wildcard_does_not_apply_outside_the_node_layer():
    """§4.2 行 149 的通配只在 node 层：别的层用通配会把覆盖范围扩大。"""
    assert M.SCOPE_CHAIN  # keep import used
    from governance_policy.service import scope_matches
    assert scope_matches("shot_3.*", "shot_3.video", "node") is True
    assert scope_matches("shot_3.*", "shot_4.video", "node") is False
    assert scope_matches("proj_*", "proj_1", "project") is False
    assert scope_matches("proj_1", "proj_1", "project") is True


def test_e12_03_cross_tenant_resolution_is_404():
    svc = _svc()
    _publish_base(svc)
    with pytest.raises(ServiceError) as ei:
        svc.resolve_effective(TB, {"context": {"tenant_id": TB,
                                               "project_id": "proj_1"}})
    assert ei.value.code == "POLICY_NOT_FOUND"


def test_s5_pinned_policy_ref_must_be_current():
    svc = _svc()
    _publish_base(svc)
    _publish(svc, _doc(revision=2))
    with pytest.raises(ServiceError) as ei:
        svc.resolve_effective(TA, {"context": {
            "tenant_id": TA, "project_id": "p",
            "policy_ref": {"id": "policy_base", "revision": 1}}})
    assert ei.value.code == "POLICY_SUPERSEDED"
    assert ei.value.details == {"pinned": 1, "current": 2}


def test_resolve_without_enterprise_binding_is_404():
    svc = _svc()
    with pytest.raises(ServiceError) as ei:
        svc.resolve_effective(TA, {"context": {"tenant_id": TA,
                                               "project_id": "p"}})
    assert ei.value.code == "POLICY_NOT_FOUND"


def test_drifted_scope_binding_is_superseded_not_guessed():
    """§4.4 行 210-212：指针漂移时拒绝，不猜哪一版。"""
    svc = _svc()
    _publish_base(svc)
    svc.conn.execute("UPDATE policy_scope_binding SET content_hash = 'sha256:other'")
    with pytest.raises(ServiceError) as ei:
        svc.resolve_effective(TA, {"context": {"tenant_id": TA,
                                               "project_id": "p"}})
    assert ei.value.code == "POLICY_SUPERSEDED"


# -- HTTP 层 -------------------------------------------------------------

def _load_app(monkeypatch, tmp_path):
    """每个用例一个独立库；先指好路径再加载模块（app.py 在 import 时建表）。

    **不能平名 `import app`**：别的服务也有 `api/app.py` 且可能先入
    `sys.modules`，平名导入会拿到另一份路由表（`artifact-ingest` 的测试里
    记着同一个坑）。
    """
    monkeypatch.setenv("S02_DB", str(tmp_path / "s02.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s02_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s02_api_app"] = module
    return module


def test_http_publish_is_idempotent_and_moves_pointer_once(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    client = TestClient(module.app)
    body = {"policy": _doc(), "expected_revision": 0, "command_id": "cmd_1",
            "scope": {"type": "tenant", "id": TA}}
    headers = {"Idempotency-Key": "cmd_1", "X-Tenant-Id": TA}
    first = client.post("/policies/policy_base/revisions", json=body,
                        headers=headers)
    assert first.status_code == 202, first.text
    second = client.post("/policies/policy_base/revisions", json=body,
                         headers=headers)
    assert second.status_code == 202
    assert second.json()["replayed"] is True
    assert module.service.conn.execute(
        "SELECT COUNT(*) FROM automation_policy_revision").fetchone()[0] == 1


def test_http_same_command_id_different_body_is_409(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    client = TestClient(module.app)
    headers = {"Idempotency-Key": "cmd_2", "X-Tenant-Id": TA}
    client.post("/policies/policy_x/revisions",
                json={"policy": _doc(policy_id="policy_x", revision=1),
                      "expected_revision": 0, "command_id": "cmd_2"},
                headers=headers)
    clash = client.post("/policies/policy_x/revisions",
                        json={"policy": _doc(policy_id="policy_x", revision=1,
                                             created_by="other"),
                              "expected_revision": 0, "command_id": "cmd_2"},
                        headers=headers)
    assert clash.status_code == 409


def test_http_tenant_mismatch_is_403(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    client = TestClient(module.app)
    r = client.post("/policies/policy_base/revisions",
                    json={"policy": _doc(tenant_id=TB), "expected_revision": 0,
                          "command_id": "cmd_3"},
                    headers={"Idempotency-Key": "cmd_3", "X-Tenant-Id": TA})
    assert r.status_code == 403
    assert r.json()["code"] == "TENANT_MISMATCH"


def test_http_resolve_does_not_require_an_idempotency_key(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    module.service.create_revision(TA, "policy_base", {
        "policy": _doc(), "expected_revision": 0, "command_id": "cmd_seed",
        "scope": {"type": "tenant", "id": TA}})
    client = TestClient(module.app)
    r = client.post("/policy-decisions:resolve-effective",
                    json={"context": {"tenant_id": TA, "project_id": "p"}},
                    headers={"X-Tenant-Id": TA})
    assert r.status_code == 200, r.text
    assert r.json()["effective"]["mode"] == "exception_assisted"


def test_http_effective_policy_path_version_is_a_coarse_query(monkeypatch,
                                                              tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    module.service.create_revision(TA, "policy_base", {
        "policy": _doc(), "expected_revision": 0, "command_id": "cmd_seed2",
        "scope": {"type": "tenant", "id": TA}})
    client = TestClient(module.app)
    r = client.get("/projects/proj_1/effective-policy",
                   headers={"X-Tenant-Id": TA})
    assert r.status_code == 200, r.text
    assert r.json()["evaluated_for"]["node_epoch"] is None


# -- POL-* 契约缺口：登记，不解决 -----------------------------------------

def test_pol_1_the_declared_query_endpoint_does_not_exist(monkeypatch,
                                                          tmp_path):
    """catalog 声明 `:evaluate`，代码只有 `:resolve-effective`。

    两边都是事实：这条**不**断言谁对，只钉住「声明与实现在当前不一致」，
    这样下一轮补齐 `:evaluate` 时测试会红，逼着一起改（或改 catalog）。
    """
    module = _load_app(monkeypatch, tmp_path)
    module.service.create_revision(TA, "policy_base", {
        "policy": _doc(), "expected_revision": 0, "command_id": "cmd_pol1",
        "scope": {"type": "tenant", "id": TA}})
    client = TestClient(module.app)
    # 声明的入口不存在（405/404 都算，取决于路由匹配方式）
    r = client.post("/policy-decisions:evaluate", json={"kind": "dispatch_admission"},
                    headers={"Idempotency-Key": "cmd_e", "X-Tenant-Id": TA})
    assert r.status_code in (404, 405), r.text
    # 真实存在的是另一条
    r2 = client.post("/policy-decisions:resolve-effective",
                     json={"context": {"tenant_id": TA, "project_id": "p"}},
                     headers={"X-Tenant-Id": TA})
    assert r2.status_code == 200, r2.text


def test_pol_2_readiness_publishes_the_gaps():
    """readiness 必须把缺口吐出来，否则运维会把「只有 resolve」当成「面齐了」。"""
    from governance_policy import CONTRACT_GAPS
    ids = {g["id"] for g in CONTRACT_GAPS}
    assert {"POL-1", "POL-2", "POL-3"} <= ids
    for gap in CONTRACT_GAPS:
        assert gap["kind"] in ("conflict", "gap", "needs_owner")
        assert gap["summary"] and gap["handling"]


def test_pol_3_shape_validation_accepts_keys_the_contract_forbids():
    """`additionalProperties: false` 没有被运行期校验——S25 就这样读到了 routing。

    这是一个**已知的不一致**，不是想要的行为：契约拒、本服务收。用例把它写成
    断言而不是注释，等到某轮补上多余键检查时会立刻红。
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(POLICY_SCHEMA.read_text(encoding="utf-8"))
    doc = _doc(routing={"flags": {"kill_switch": True}})
    # 契约拒绝
    with pytest.raises(jsonschema.ValidationError) as exc:
        jsonschema.Draft202012Validator(schema).validate(doc)
    assert "routing" in exc.value.message
    # 本服务接受（只查必填键）
    _svc()._validate_document_shape(doc)
